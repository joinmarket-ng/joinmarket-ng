"""Tests for the operator command line of the standalone buyout service.

The command line is where a human decides that this node may sign, so these
tests are mostly about what a command refuses to do: a plain ``serve`` must stay
a buyer-only monitor, a disabled configuration must produce no runtime and no
file, a malformed channel must be rejected before anything is loaded or opened,
and ``status`` must read an existing journal without connecting to a node and
without creating the journal it was asked about. What it does do is checked at
the same boundary: repeated ``--channel`` arguments reach the runtime in order,
``prepare`` prints the session id, a signal ends ``serve`` and puts the
process's own handlers back, and every expected failure is reduced to a fixed
sentence plus an exception class name.

The runtime is replaced by a fake, because nothing here may open a socket. The
journal is real, because what ``status`` is allowed to print is exactly a
property of the stored record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import tomllib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import scriptpubkey_to_address

from jmswap import buyout_cli
from jmswap.buyout_cli import (
    CANCEL_CONTEXT,
    DISABLED_CONTEXT,
    MISSING_JOURNAL_CONTEXT,
    NO_JOURNAL_CONTEXT,
    PREPARE_CONTEXT,
    SERVE_CONTEXT,
    STATUS_CONTEXT,
    main,
    summarize,
)
from jmswap.buyout_messages import Outpoint
from jmswap.buyout_runtime import BuyoutRuntimeError
from jmswap.buyout_store import BuyoutStore, StoredSession
from jmswap.buyout_transport import PrivateTransportError
from jmswap.lnd_escrow import LndEscrowError
from jmswap.lnd_peer import LndPeerError


def _without_experimental_warning(stderr: str) -> str:
    return "".join(
        line for line in stderr.splitlines(keepends=True) if not line.startswith("EXPERIMENTAL")
    )


def _pubkey(tag: str) -> str:
    return bytes(CKey(hashlib.sha256(tag.encode()).digest()).pub).hex()


def _p2tr(tag: str, network: str) -> str:
    key = CKey(hashlib.sha256(tag.encode()).digest())
    return scriptpubkey_to_address(b"\x51\x20" + bytes(key.pub)[1:], network)


IDENTITY = _pubkey("cli-identity")
PEER = _pubkey("cli-peer")
PAYOUT = _p2tr("cli-payout", "regtest")
SESSION_A = "a1" * 32
SESSION_B = "b2" * 32
CHANNEL_A = "11" * 32 + ":0"
CHANNEL_B = "22" * 32 + ":7"
SECRET_INVOICE = "lnbcrt-secret-invoice"
FINGERPRINT = "0123abcd"
BINDING: dict[str, object] = {
    "network": "regtest",
    "lnd_identity": IDENTITY,
    "mixdepth": 0,
    "wallet_fingerprint": FINGERPRINT,
}

ENABLED_TOML = f"""
[buyout]
enabled = true
network = "regtest"
journal = "journal/buyout.sqlite"
lnd_endpoint = "127.0.0.1:10009"
lnd_identity = "{IDENTITY}"
lnd_tls_cert = "tls.cert"
lnd_peer_macaroon = "peer.macaroon"
lnd_escrow_macaroon = "escrow.macaroon"
bitcoin_rpc_url = "http://127.0.0.1:18443/"
bitcoin_rpc_user = "buyout"
bitcoin_rpc_password = "regtest-placeholder"
allowed_peers = ["{PEER}"]
payout_addresses = ["{PAYOUT}"]
mixdepth = 0
wallet_fingerprint = "0123abcd"
"""

DISABLED_TOML = '[buyout]\nenabled = false\njournal = "journal/buyout.sqlite"\n'


def _config(tmp_path: Path, text: str = ENABLED_TOML) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def _journal(tmp_path: Path) -> Path:
    return tmp_path / "journal" / "buyout.sqlite"


@dataclass
class Recorder:
    """Everything the fake runtime was asked to do, and what it should answer."""

    counterparty: list[bool] = field(default_factory=list)
    entered: int = 0
    exited: int = 0
    prepared: list[tuple[str, tuple[Outpoint, ...]]] = field(default_factory=list)
    canceled: list[str] = field(default_factory=list)
    stops: list[asyncio.Event] = field(default_factory=list)
    session_id: str = SESSION_A
    error: BaseException | None = None
    run_hook: Callable[[asyncio.Event], Awaitable[None]] | None = None


class FakeRuntime:
    """The runtime surface this command line is allowed to use."""

    def __init__(self, recorder: Recorder, settings: Any, *, counterparty: bool = False) -> None:
        self.recorder = recorder
        self.settings = settings
        recorder.counterparty.append(counterparty)

    async def __aenter__(self) -> FakeRuntime:
        self.recorder.entered += 1
        return self

    async def __aexit__(self, *args: object) -> None:
        self.recorder.exited += 1

    def _raise(self) -> None:
        if self.recorder.error is not None:
            raise self.recorder.error

    async def run(self, stop: asyncio.Event) -> None:
        self.recorder.stops.append(stop)
        self._raise()
        if self.recorder.run_hook is not None:
            await self.recorder.run_hook(stop)

    async def prepare(self, peer: str, points: Sequence[Outpoint]) -> str:
        self._raise()
        self.recorder.prepared.append((peer, tuple(points)))
        return self.recorder.session_id

    async def cancel(self, session_id: str) -> None:
        self._raise()
        self.recorder.canceled.append(session_id)


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    recorder = Recorder()

    def factory(settings: Any, *, counterparty: bool = False) -> FakeRuntime:
        return FakeRuntime(recorder, settings, counterparty=counterparty)

    monkeypatch.setattr(buyout_cli, "BuyoutRuntime", factory)
    return recorder


@pytest.fixture
def no_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a command that must stay offline builds a runtime."""

    def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError("this command must not build a runtime")

    monkeypatch.setattr(buyout_cli, "BuyoutRuntime", refuse)


@pytest.mark.parametrize("reject", [False, True])
def test_force_close_is_explicit_and_runtime_guarded(
    tmp_path: Path,
    runtime: Recorder,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reject: bool,
) -> None:
    closed: list[str] = []

    async def force_close(owner: FakeRuntime, sid: str) -> None:
        owner._raise()
        closed.append(sid)

    monkeypatch.setattr(FakeRuntime, "force_close", force_close, raising=False)
    if reject:
        runtime.error = BuyoutRuntimeError("private recovery details")
    result = main(["--config", str(_config(tmp_path)), "force-close", "--session", SESSION_A])
    assert result == int(reject)
    assert closed == ([] if reject else [SESSION_A])
    assert runtime.entered == runtime.exited == 1
    assert runtime.canceled == []
    assert "private recovery details" not in capsys.readouterr().err


def test_disabled_force_close_never_opens_runtime(tmp_path: Path, no_runtime: None) -> None:
    assert (
        main(
            [
                "--config",
                str(_config(tmp_path, DISABLED_TOML)),
                "force-close",
                "--session",
                SESSION_A,
            ]
        )
        == 1
    )


class TestServe:
    def test_serve_monitors_as_a_buyer_only_by_default(
        self, tmp_path: Path, runtime: Recorder
    ) -> None:
        status = main(["--config", str(_config(tmp_path)), "serve"])

        assert status == 0
        assert runtime.counterparty == [False]
        assert (runtime.entered, runtime.exited) == (1, 1)

    def test_counterparty_signing_requires_the_explicit_flag(
        self, tmp_path: Path, runtime: Recorder
    ) -> None:
        status = main(["--config", str(_config(tmp_path)), "serve", "--counterparty"])

        assert status == 0
        assert runtime.counterparty == [True]

    def test_signal_stops_serve_and_restores_the_process_handlers(
        self, tmp_path: Path, runtime: Recorder
    ) -> None:
        async def interrupt(stop: asyncio.Event) -> None:
            os.kill(os.getpid(), signal.SIGINT)
            await asyncio.wait_for(stop.wait(), 5)

        runtime.run_hook = interrupt
        installed: dict[signal.Signals, Any] = {
            number: (lambda *args: None) for number in (signal.SIGINT, signal.SIGTERM)
        }
        original: dict[signal.Signals, Any] = {
            number: signal.getsignal(number) for number in installed
        }
        for number, handler in installed.items():
            signal.signal(number, handler)
        try:
            status = main(["--config", str(_config(tmp_path)), "serve"])

            assert status == 0
            assert runtime.stops[0].is_set()
            assert {number: signal.getsignal(number) for number in installed} == installed
        finally:
            for number, handler in original.items():
                signal.signal(number, handler)

    def test_cancellation_propagates_and_still_releases_the_runtime(
        self, tmp_path: Path, runtime: Recorder
    ) -> None:
        async def cancel(stop: asyncio.Event) -> None:
            raise asyncio.CancelledError

        runtime.run_hook = cancel

        with pytest.raises(asyncio.CancelledError):
            main(["--config", str(_config(tmp_path)), "serve"])

        assert (runtime.entered, runtime.exited) == (1, 1)

    def test_runtime_failure_is_reported_without_its_message(
        self, tmp_path: Path, runtime: Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime.error = LndPeerError(f"rpc failed reading {SECRET_INVOICE}")

        status = main(["--config", str(_config(tmp_path)), "serve"])

        assert status == 1
        error = capsys.readouterr().err
        assert SERVE_CONTEXT in error
        assert "LndPeerError" in error
        assert SECRET_INVOICE not in error
        assert "Traceback" not in error
        assert runtime.exited == 1


def _leaves(group: BaseExceptionGroup[Any]) -> list[BaseException]:
    found: list[BaseException] = []
    for exception in group.exceptions:
        if isinstance(exception, BaseExceptionGroup):
            found.extend(_leaves(exception))
        else:
            found.append(exception)
    return found


class TestTaskGroupFailures:
    """The runtime prepares inside a task group, so failures arrive nested."""

    def _prepare(self, tmp_path: Path) -> int:
        return main(
            ["--config", str(_config(tmp_path)), "prepare", "--peer", PEER, "--channel", CHANNEL_A]
        )

    def test_a_nested_group_of_expected_errors_is_sanitized(
        self, tmp_path: Path, runtime: Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime.error = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [
                LndEscrowError(f"escrow rpc failed for {SECRET_INVOICE}"),
                ExceptionGroup("nested", [PrivateTransportError(f"peer refused {SECRET_INVOICE}")]),
            ],
        )

        status = self._prepare(tmp_path)

        assert status == 1
        error = capsys.readouterr().err
        assert PREPARE_CONTEXT in error
        assert "LndEscrowError" in error
        assert "PrivateTransportError" in error
        assert SECRET_INVOICE not in error
        assert "Traceback" not in error
        assert "TaskGroup" not in error
        assert runtime.exited == 1

    def test_a_programming_error_in_a_group_keeps_only_the_bug(
        self, tmp_path: Path, runtime: Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime.error = ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [
                LndEscrowError(f"escrow rpc failed for {SECRET_INVOICE}"),
                TypeError("prepare() got an unexpected keyword argument"),
            ],
        )

        with pytest.raises(BaseExceptionGroup) as failure:
            self._prepare(tmp_path)

        leaves = _leaves(failure.value)
        assert [type(leaf) for leaf in leaves] == [TypeError]
        assert "unexpected keyword argument" in str(leaves[0])
        assert SECRET_INVOICE not in repr(failure.value)
        assert _without_experimental_warning(capsys.readouterr().err) == ""
        assert runtime.exited == 1

    def test_a_plain_programming_error_is_not_sanitized(
        self, tmp_path: Path, runtime: Recorder
    ) -> None:
        runtime.error = TypeError("prepare() got an unexpected keyword argument")

        with pytest.raises(TypeError, match="unexpected keyword argument"):
            self._prepare(tmp_path)

    def test_a_cancellation_in_a_group_still_propagates(
        self, tmp_path: Path, runtime: Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime.error = BaseExceptionGroup(
            "unhandled errors in a TaskGroup",
            [LndEscrowError(f"escrow rpc failed for {SECRET_INVOICE}"), asyncio.CancelledError()],
        )

        with pytest.raises(BaseExceptionGroup) as failure:
            self._prepare(tmp_path)

        assert [type(leaf) for leaf in _leaves(failure.value)] == [asyncio.CancelledError]
        assert SECRET_INVOICE not in repr(failure.value)
        assert _without_experimental_warning(capsys.readouterr().err) == ""


class TestDisabledConfiguration:
    @pytest.mark.parametrize(
        "command",
        [
            ["serve"],
            ["prepare", "--peer", PEER, "--channel", CHANNEL_A],
            ["cancel", "--session", SESSION_A],
        ],
    )
    def test_disabled_configuration_acts_on_nothing(
        self,
        tmp_path: Path,
        no_runtime: None,
        capsys: pytest.CaptureFixture[str],
        command: list[str],
    ) -> None:
        path = _config(tmp_path, DISABLED_TOML)

        status = main(["--config", str(path), *command])

        assert status == 1
        assert DISABLED_CONTEXT in capsys.readouterr().err
        assert not _journal(tmp_path).exists()
        assert not _journal(tmp_path).parent.exists()


class TestPrepare:
    def test_repeated_channels_reach_the_runtime_in_order(
        self, tmp_path: Path, runtime: Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = main(
            [
                "--config",
                str(_config(tmp_path)),
                "prepare",
                "--peer",
                PEER,
                "--channel",
                CHANNEL_A,
                "--channel",
                CHANNEL_B,
            ]
        )

        assert status == 0
        assert runtime.prepared == [
            (
                PEER,
                (Outpoint(txid="11" * 32, vout=0), Outpoint(txid="22" * 32, vout=7)),
            )
        ]
        assert capsys.readouterr().out == f"{SESSION_A}\n"
        assert runtime.counterparty == [False]

    @pytest.mark.parametrize(
        "channel",
        ["deadbeef", "11" * 32, "11" * 32 + ":", "11" * 32 + ":-1", ("AB" * 32) + ":0"],
    )
    def test_invalid_channel_is_rejected_before_any_resource(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_runtime: None, channel: str
    ) -> None:
        def refuse(path: Path) -> None:
            raise AssertionError("the configuration must not be loaded for invalid arguments")

        monkeypatch.setattr(buyout_cli, "load_buyout_settings", refuse)

        with pytest.raises(SystemExit) as failure:
            main(
                [
                    "--config",
                    str(_config(tmp_path)),
                    "prepare",
                    "--peer",
                    PEER,
                    "--channel",
                    channel,
                ]
            )

        assert failure.value.code == 2

    def test_invalid_peer_is_rejected(self, tmp_path: Path, no_runtime: None) -> None:
        with pytest.raises(SystemExit) as failure:
            main(
                [
                    "--config",
                    str(_config(tmp_path)),
                    "prepare",
                    "--peer",
                    "not-a-key",
                    "--channel",
                    CHANNEL_A,
                ]
            )

        assert failure.value.code == 2

    def test_a_channel_is_required(self, tmp_path: Path, no_runtime: None) -> None:
        with pytest.raises(SystemExit) as failure:
            main(["--config", str(_config(tmp_path)), "prepare", "--peer", PEER])

        assert failure.value.code == 2


class TestCancel:
    def test_cancel_passes_the_session_to_the_runtime(
        self, tmp_path: Path, runtime: Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = main(["--config", str(_config(tmp_path)), "cancel", "--session", SESSION_B])

        assert status == 0
        assert runtime.canceled == [SESSION_B]
        assert capsys.readouterr().out == ""

    def test_a_guarded_cancel_is_refused_and_sanitized(
        self, tmp_path: Path, runtime: Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime.error = BuyoutRuntimeError("buyer session is not bound to this runtime")

        status = main(["--config", str(_config(tmp_path)), "cancel", "--session", SESSION_B])

        assert status == 1
        error = capsys.readouterr().err
        assert CANCEL_CONTEXT in error
        assert "BuyoutRuntimeError" in error
        assert "not bound" not in error
        assert runtime.canceled == []
        assert runtime.exited == 1

    def test_invalid_session_id_is_rejected(self, tmp_path: Path, no_runtime: None) -> None:
        with pytest.raises(SystemExit) as failure:
            main(["--config", str(_config(tmp_path)), "cancel", "--session", "nope"])

        assert failure.value.code == 2


class TestStatus:
    def _populate(self, path: Path) -> None:
        with BuyoutStore(path) as store:
            store.create(
                SESSION_A,
                PEER,
                "buyer",
                [CHANNEL_A],
                {
                    "settlement_authorized": True,
                    "runtime_binding": dict(BINDING),
                    "invoice": SECRET_INVOICE,
                },
            )
            store.create(SESSION_B, PEER, "counterparty", [CHANNEL_B], {"invoice": SECRET_INVOICE})

    def test_status_prints_only_the_permitted_summary_fields(
        self, tmp_path: Path, no_runtime: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _config(tmp_path)
        self._populate(_journal(tmp_path))

        status = main(["--config", str(path), "status"])

        assert status == 0
        output = capsys.readouterr().out
        assert json.loads(output) == [
            {
                "session_id": SESSION_A,
                "role": "buyer",
                "state": "CREATED",
                "parent_signing_started": False,
                "settlement_authorized": True,
                "runtime_binding_present": True,
            },
            {
                "session_id": SESSION_B,
                "role": "counterparty",
                "state": "CREATED",
                "parent_signing_started": False,
                "settlement_authorized": False,
                "runtime_binding_present": False,
            },
        ]
        assert SECRET_INVOICE not in output
        assert PEER not in output
        assert CHANNEL_A not in output

    def test_status_reports_last_parent_observation_offline(
        self, tmp_path: Path, no_runtime: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _config(tmp_path)
        journal = _journal(tmp_path)
        self._populate(journal)
        with BuyoutStore(journal) as store:
            record = store.get(SESSION_A)
            store.update(
                record,
                state="PARENT_OBSERVED",
                parent_signing_started=True,
                data=record.data,
            )

        assert main(["--config", str(path), "status"]) == 0
        output = capsys.readouterr().out
        records = json.loads(output)
        assert records[0]["state"] == "PARENT_OBSERVED"
        assert records[0]["parent_signing_started"] is True
        assert SECRET_INVOICE not in output

    def test_status_never_creates_a_missing_journal(
        self, tmp_path: Path, no_runtime: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _config(tmp_path)

        status = main(["--config", str(path), "status"])

        assert status == 1
        assert MISSING_JOURNAL_CONTEXT in capsys.readouterr().err
        assert not _journal(tmp_path).exists()
        assert not _journal(tmp_path).parent.exists()

    def test_status_requires_a_configured_journal(
        self, tmp_path: Path, no_runtime: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _config(tmp_path, "[buyout]\nenabled = false\n")

        status = main(["--config", str(path), "status"])

        assert status == 1
        assert NO_JOURNAL_CONTEXT in capsys.readouterr().err

    def test_an_unusable_journal_is_reported_without_its_message(
        self, tmp_path: Path, no_runtime: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = _config(tmp_path)
        journal = _journal(tmp_path)
        journal.parent.mkdir(mode=0o700)
        journal.write_bytes(b"not a database")
        journal.chmod(0o600)

        status = main(["--config", str(path), "status"])

        assert status == 1
        error = capsys.readouterr().err
        assert STATUS_CONTEXT in error
        assert "JournalError" in error
        assert "Traceback" not in error


class TestArgumentSurface:
    @pytest.mark.parametrize(
        "argv",
        [[], ["serve"], ["--config"], ["--config", "x"], ["--config", "x", "unknown"]],
    )
    def test_missing_or_unknown_arguments_exit_non_zero(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as failure:
            main(argv)

        assert failure.value.code != 0

    def test_a_missing_configuration_file_is_reported(
        self, tmp_path: Path, no_runtime: None, capsys: pytest.CaptureFixture[str]
    ) -> None:
        status = main(["--config", str(tmp_path / "absent.toml"), "status"])

        assert status == 1
        error = capsys.readouterr().err
        assert "BuyoutConfigError" in error
        assert "absent.toml" not in error

    def test_the_console_script_points_at_this_module(self) -> None:
        manifest = Path(__file__).resolve().parents[1] / "pyproject.toml"

        with manifest.open("rb") as handle:
            document = tomllib.load(handle)

        assert document["project"]["scripts"]["jm-buyout"] == "jmswap.buyout_cli:main"

    def test_prepare_never_reports_the_configured_secret(
        self, tmp_path: Path, runtime: Recorder, capsys: pytest.CaptureFixture[str]
    ) -> None:
        runtime.error = OSError(f"credential {SECRET_INVOICE} is unreadable")

        status = main(
            ["--config", str(_config(tmp_path)), "prepare", "--peer", PEER, "--channel", CHANNEL_A]
        )

        assert status == 1
        error = capsys.readouterr().err
        assert PREPARE_CONTEXT in error
        assert "OSError" in error
        assert SECRET_INVOICE not in error


def _record(data: dict[str, object]) -> StoredSession:
    return StoredSession(
        session_id=SESSION_A,
        peer_pubkey=PEER,
        role="buyer",
        channel_points=(CHANNEL_A,),
        revision=0,
        state="CREATED",
        data=data,
        parent_signing_started=False,
    )


def _binding(**overrides: object) -> dict[str, object]:
    values = dict(BINDING)
    values.update(overrides)
    return values


class TestRuntimeBindingSummary:
    """``runtime_binding_present`` is an affirmative fact about the stored shape."""

    @pytest.mark.parametrize(
        ("binding", "present"),
        [
            pytest.param(None, False, id="absent"),
            pytest.param({}, False, id="empty"),
            pytest.param({"network": "regtest"}, False, id="network-only"),
            pytest.param(
                {"network": "regtest", "lnd_identity": IDENTITY}, False, id="without-mixdepth"
            ),
            pytest.param([BINDING], False, id="not-a-mapping"),
            pytest.param("regtest", False, id="a-string"),
            pytest.param(_binding(mixdepth=True), False, id="boolean-mixdepth"),
            pytest.param(_binding(mixdepth=-1), False, id="negative-mixdepth"),
            pytest.param(_binding(mixdepth="0"), False, id="string-mixdepth"),
            pytest.param(_binding(network="bitcoin"), False, id="unknown-network"),
            pytest.param(
                _binding(lnd_identity="04" + "ab" * 32), False, id="uncompressed-identity"
            ),
            pytest.param(_binding(lnd_identity=IDENTITY.upper()), False, id="uppercase-identity"),
            pytest.param(
                _binding(wallet_fingerprint="nothex12"), False, id="malformed-fingerprint"
            ),
            pytest.param(_binding(wallet_fingerprint=None), False, id="null-fingerprint"),
            pytest.param(_binding(extra="value"), False, id="unknown-key"),
            pytest.param(dict(BINDING), True, id="valid-with-fingerprint"),
            pytest.param(
                {"network": "regtest", "lnd_identity": IDENTITY, "mixdepth": 3},
                True,
                id="valid-without-fingerprint",
            ),
        ],
    )
    def test_only_a_well_formed_binding_is_reported_as_present(
        self, binding: object, present: bool
    ) -> None:
        data: dict[str, object] = {} if binding is None else {"runtime_binding": binding}

        assert summarize(_record(data))["runtime_binding_present"] is present

    def test_the_summary_still_carries_only_the_permitted_keys(self) -> None:
        summary = summarize(_record({"runtime_binding": dict(BINDING), "invoice": SECRET_INVOICE}))

        assert set(summary) == {
            "session_id",
            "role",
            "state",
            "parent_signing_started",
            "settlement_authorized",
            "runtime_binding_present",
        }
        assert SECRET_INVOICE not in json.dumps(summary)


def test_enabled_commands_print_the_experimental_warning(
    tmp_path: Path, runtime: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--config", str(_config(tmp_path)), "cancel", "--session", SESSION_B]) == 0
    assert "EXPERIMENTAL features enabled: private channel buyouts" in capsys.readouterr().err


def test_status_does_not_print_the_experimental_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["--config", str(_config(tmp_path, DISABLED_TOML)), "status"])
    assert "EXPERIMENTAL" not in capsys.readouterr().err
