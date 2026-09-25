"""Tests for the opt-in prepared buyout session of the taker CLI.

A buyout is never discovered and never enabled by accident: only an explicit
``--buyout-config`` plus ``--buyout-session`` pair turns it on, and every rule
that could disqualify the file is decided before a runtime opens a journal, a
node connection or a channel signature. What happens afterwards is equally
bounded: the adapter reaches exactly one CoinJoin, the settlement monitor is
released on every exit, and neither a failed round nor an interrupt ever
resolves a session behind the operator.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer
from bitcointx.core.key import CKey
from jmcore.bitcoin import scriptpubkey_to_address
from jmcore.models import NetworkType, OfferType
from jmswap import buyout_config as buyout_config_module
from jmswap.buyout_config import BuyoutSettings
from typer.testing import CliRunner

from taker.cli import PreparedBuyoutSession, _run_coinjoin, app

runner = CliRunner()

FINGERPRINT = "0123abcd"
SESSION_ID = "ab" * 32


def _pubkey(tag: str) -> str:
    return bytes(CKey(hashlib.sha256(tag.encode()).digest()).pub).hex()


_PAYOUT_SCRIPT = b"\x51\x20" + bytes(CKey(hashlib.sha256(b"payout").digest()).xonly_pub)
PAYOUT_ADDRESS = scriptpubkey_to_address(_PAYOUT_SCRIPT, "regtest")
SIGNET_PAYOUT_ADDRESS = scriptpubkey_to_address(_PAYOUT_SCRIPT, "signet")


def _settings(tmp_path: Path, **overrides: object) -> BuyoutSettings:
    for name in ("tls.cert", "peer.macaroon", "escrow.macaroon"):
        (tmp_path / name).write_bytes(b"credential")
    values: dict[str, object] = {
        "enabled": True,
        "network": "regtest",
        "journal": tmp_path / "journal" / "sessions.sqlite",
        "lnd_endpoint": "127.0.0.1:10009",
        "lnd_identity": _pubkey("taker-identity"),
        "lnd_tls_cert": tmp_path / "tls.cert",
        "lnd_peer_macaroon": tmp_path / "peer.macaroon",
        "lnd_escrow_macaroon": tmp_path / "escrow.macaroon",
        "bitcoin_rpc_url": "http://127.0.0.1:18443/",
        "bitcoin_rpc_user": "buyout",
        "bitcoin_rpc_password": "regtest-placeholder",
        "allowed_peers": (_pubkey("taker-peer"),),
        "payout_address": PAYOUT_ADDRESS,
        "mixdepth": 0,
        "wallet_fingerprint": FINGERPRINT,
        "poll_interval_seconds": 0.01,
    }
    values.update(overrides)
    return BuyoutSettings.model_validate(values)


class FakePrepared:
    """Stands in for the runtime, the adapter and the settlement monitor."""

    opened: list[FakePrepared] = []
    interrupt = False

    def __init__(self, settings: BuyoutSettings, session_id: str) -> None:
        self.settings = settings
        self.session_id = session_id
        self.adapter = MagicMock(name="channel-buyout")
        self.cancel = AsyncMock()
        self.entered = False
        self.exited = False
        self.waits = 0
        self.settled = "COMPLETED"
        FakePrepared.opened.append(self)

    async def __aenter__(self) -> FakePrepared:
        self.entered = True
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exited = True

    async def wait_for_settlement(self) -> str:
        self.waits += 1
        if self.interrupt:
            raise asyncio.CancelledError
        return self.settled


@pytest.fixture(autouse=True)
def prepared_sessions() -> Any:
    FakePrepared.opened = []
    with patch("taker.cli.PreparedBuyoutSession", FakePrepared):
        yield FakePrepared.opened


@pytest.fixture
def taker() -> MagicMock:
    result = MagicMock()
    result.nick = "J5test"
    result.sync_wallet = AsyncMock()
    result.check_utxo_eligibility = AsyncMock(return_value=None)
    result.connect = AsyncMock()
    result.do_coinjoin = AsyncMock(return_value="c" * 64)
    result.stop = AsyncMock()
    result.last_broadcast_method = "self"
    result.last_broadcast_fallback_reason = ""
    return result


def _config(address_type: str = "p2tr") -> MagicMock:
    config = MagicMock()
    config.network = NetworkType.REGTEST
    config.bitcoin_network = None
    config.backend_type = "descriptor_wallet"
    config.address_type = address_type
    config.mixdepth_count = 5
    config.preferred_offer_type = OfferType.TR0_ABSOLUTE
    config.mnemonic.get_secret_value.return_value = "test mnemonic"
    config.passphrase.get_secret_value.return_value = ""
    return config


async def _run(
    taker: MagicMock,
    settings: BuyoutSettings | None,
    *,
    address_type: str = "p2tr",
    remove_state: MagicMock | None = None,
    **overrides: Any,
) -> None:
    """Run the CLI coroutine with every external dependency replaced."""
    backend = MagicMock()
    backend.get_block_height = AsyncMock()
    wallet = MagicMock()
    wallet.wallet_fingerprint = FINGERPRINT
    notifier = MagicMock()
    notifier.notify_startup = AsyncMock()
    arguments: dict[str, Any] = {
        "amount": 1_000_000,
        "destination": "INTERNAL",
        "mixdepth": 0,
        "counterparties": 3,
        "skip_confirmation": True,
        "buyout_config": Path("/nonexistent/buyout.toml"),
        "buyout_session": SESSION_ID,
    }
    arguments.update(overrides)
    loader = MagicMock(return_value=settings)
    with (
        patch("taker.cli.create_backend", return_value=backend),
        patch("taker.cli.WalletService", return_value=wallet),
        patch("taker.cli.get_notifier", return_value=notifier),
        patch("taker.cli.write_nick_state"),
        patch("taker.cli.remove_nick_state", remove_state or MagicMock()),
        patch("taker.taker.Taker", return_value=taker),
        patch.object(buyout_config_module, "load_buyout_settings", loader),
    ):
        await _run_coinjoin(settings=MagicMock(), config=_config(address_type), **arguments)


@pytest.mark.parametrize(
    "arguments",
    [
        ["--buyout-config", "buyout.toml"],
        ["--buyout-session", SESSION_ID],
    ],
)
def test_buyout_options_are_only_accepted_as_a_pair(arguments: list[str]) -> None:
    with patch("taker.cli.setup_cli") as setup:
        result = runner.invoke(app, ["coinjoin", "--amount", "1000000", *arguments])

    assert result.exit_code == 1
    setup.assert_not_called()


def test_coinjoin_help_documents_the_opt_in_pair() -> None:
    result = runner.invoke(app, ["coinjoin", "--help"], prog_name="jm-taker")

    assert result.exit_code == 0
    assert "--buyout-config" in result.stdout
    assert "--buyout-session" in result.stdout


async def test_ordinary_coinjoin_never_loads_or_opens_a_buyout(taker: MagicMock) -> None:
    await _run(taker, None, buyout_config=None, buyout_session=None)

    assert FakePrepared.opened == []
    assert taker.do_coinjoin.await_args.kwargs["buyout"] is None
    assert taker.check_utxo_eligibility.await_args.kwargs["buyout"] is None


@pytest.mark.parametrize(
    ("overrides", "arguments", "expected"),
    [
        ({"enabled": False}, {}, "disabled"),
        (
            {"network": "signet", "payout_address": SIGNET_PAYOUT_ADDRESS},
            {},
            "networks differ",
        ),
        ({"wallet_fingerprint": "deadbeef"}, {}, "different wallet fingerprint"),
        ({"wallet_fingerprint": None}, {}, "wallet_fingerprint"),
        ({"mixdepth": 2}, {"mixdepth": 1}, "configured buyout mixdepth 2"),
        ({"mixdepth": 9}, {}, "outside this wallet's 5 mixdepths"),
        ({}, {"amount": 0}, "non-sweep Taproot"),
        ({}, {"address_type": "p2wpkh"}, "non-sweep Taproot"),
    ],
)
async def test_a_disqualified_file_never_reaches_a_buyout_resource(
    taker: MagicMock,
    tmp_path: Path,
    overrides: dict[str, object],
    arguments: dict[str, Any],
    expected: str,
) -> None:
    if not overrides.get("enabled", True):
        settings = BuyoutSettings()
    else:
        settings = _settings(tmp_path, **overrides)
    messages: list[str] = []
    from loguru import logger

    handler = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        with pytest.raises(typer.Exit) as failure:
            await _run(taker, settings, **arguments)
    finally:
        logger.remove(handler)

    assert failure.value.exit_code == 1
    assert any(expected in message for message in messages)
    assert FakePrepared.opened == []
    taker.sync_wallet.assert_not_called()
    taker.do_coinjoin.assert_not_called()


async def test_a_rejected_file_still_unwinds_the_taker(taker: MagicMock, tmp_path: Path) -> None:
    remove_state = MagicMock()

    with pytest.raises(typer.Exit):
        await _run(taker, _settings(tmp_path, mixdepth=9), remove_state=remove_state)

    # Validation runs inside the protected lifetime: the taker that was already
    # built is stopped and its nick state removed, and no runtime was opened.
    taker.stop.assert_awaited_once()
    remove_state.assert_called_once()
    taker.do_coinjoin.assert_not_called()
    assert FakePrepared.opened == []


@pytest.mark.parametrize("failing", ["stop", "remove_nick_state"])
async def test_a_failing_shutdown_step_still_releases_the_buyout(
    taker: MagicMock, tmp_path: Path, failing: str
) -> None:
    remove_state = MagicMock()
    if failing == "stop":
        taker.stop = AsyncMock(side_effect=RuntimeError("shutdown failed"))
    else:
        remove_state.side_effect = RuntimeError("shutdown failed")

    with pytest.raises(RuntimeError, match="shutdown failed"):
        await _run(taker, _settings(tmp_path), remove_state=remove_state)

    (prepared,) = FakePrepared.opened
    assert prepared.exited


async def test_an_omitted_mixdepth_is_pinned_to_the_configured_one(
    taker: MagicMock, tmp_path: Path
) -> None:
    await _run(taker, _settings(tmp_path, mixdepth=3), mixdepth=None)

    assert taker.check_utxo_eligibility.await_args.args[1] == 3
    assert taker.do_coinjoin.await_args.kwargs["mixdepth"] == 3


async def test_the_adapter_funds_one_round_and_the_runtime_is_released(
    taker: MagicMock, tmp_path: Path
) -> None:
    await _run(taker, _settings(tmp_path))

    (prepared,) = FakePrepared.opened
    assert prepared.session_id == SESSION_ID
    assert prepared.entered and prepared.exited
    assert taker.do_coinjoin.await_args.kwargs["buyout"] is prepared.adapter
    assert taker.check_utxo_eligibility.await_args.kwargs["buyout"] is prepared.adapter


async def test_a_broadcast_round_waits_for_settlement(
    taker: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    await _run(taker, _settings(tmp_path))

    (prepared,) = FakePrepared.opened
    assert prepared.waits == 1
    output = capsys.readouterr().out
    assert SESSION_ID in output
    assert "resolved: COMPLETED" in output


async def test_a_failed_round_never_cancels_the_session(taker: MagicMock, tmp_path: Path) -> None:
    taker.do_coinjoin = AsyncMock(return_value=None)

    with pytest.raises(typer.Exit) as failure:
        await _run(taker, _settings(tmp_path))

    (prepared,) = FakePrepared.opened
    assert failure.value.exit_code == 1
    assert prepared.waits == 0
    assert prepared.exited
    prepared.cancel.assert_not_called()
    prepared.adapter.cancel.assert_not_called()


class FakeRuntime:
    """A buyout runtime whose journal, nodes and polling loop are observable."""

    last: FakeRuntime | None = None
    next_monitor_error: Exception | None = None
    next_rejects = False

    def __init__(self, settings: BuyoutSettings) -> None:
        self.settings = settings
        self.buyer = MagicMock(name="buyer")
        self.store = MagicMock(name="store")
        self.store.get.return_value = MagicMock(state="AUTHORIZED")
        self.closed = False
        self.polls = 0
        self.monitor_error = FakeRuntime.next_monitor_error
        self.rejects = FakeRuntime.next_rejects
        self.events: list[str] = []
        FakeRuntime.last = self

    async def __aenter__(self) -> FakeRuntime:
        self.events.append("open")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True
        self.events.append("close")

    def require_buyer_session(self, session_id: str) -> MagicMock:
        if self.rejects:
            raise RuntimeError("buyer session is not bound to this runtime")
        self.events.append(f"require:{session_id}")
        return MagicMock()

    async def run(self, stop: asyncio.Event) -> None:
        self.events.append("monitor")
        if self.monitor_error is not None:
            raise self.monitor_error
        await stop.wait()
        self.polls += 1
        self.events.append("monitor-stopped")


def _prepared_session(settings: BuyoutSettings) -> PreparedBuyoutSession:
    """The real helper, not the double the CLI tests patch in."""
    return PreparedBuyoutSession(settings, SESSION_ID)


def _patched_runtime() -> Any:
    from jmswap import buyout_runtime

    from taker import buyout as buyout_adapter

    adapter = MagicMock(name="channel-buyout")
    return (
        patch.object(buyout_runtime, "BuyoutRuntime", FakeRuntime),
        patch.object(buyout_adapter, "ChannelBuyout", MagicMock(return_value=adapter)),
        adapter,
    )


async def test_prepared_session_opens_validates_monitors_and_releases(tmp_path: Path) -> None:
    runtime_patch, adapter_patch, adapter = _patched_runtime()
    with runtime_patch, adapter_patch:
        async with _prepared_session(_settings(tmp_path)) as prepared:
            assert prepared.adapter is adapter
            runtime = FakeRuntime.last
            assert runtime is not None
            await asyncio.sleep(0)

    assert runtime.events[:3] == ["open", f"require:{SESSION_ID}", "monitor"]
    # The monitor is stopped and awaited before the journal and nodes close.
    assert runtime.events[-2:] == ["monitor-stopped", "close"]
    assert runtime.polls == 1


async def test_a_session_this_runtime_does_not_own_releases_everything(tmp_path: Path) -> None:
    runtime_patch, adapter_patch, _adapter = _patched_runtime()
    FakeRuntime.next_rejects = True
    try:
        with runtime_patch, adapter_patch, pytest.raises(RuntimeError, match="not bound"):
            async with _prepared_session(_settings(tmp_path)):
                pass
    finally:
        FakeRuntime.next_rejects = False

    assert FakeRuntime.last is not None and FakeRuntime.last.closed
    assert FakeRuntime.last.events == ["open", "close"]


async def test_a_dead_monitor_takes_the_round_down_with_it(tmp_path: Path) -> None:
    runtime_patch, adapter_patch, _adapter = _patched_runtime()
    FakeRuntime.next_monitor_error = RuntimeError("the private message subscription ended")
    reached_end = False
    try:
        with runtime_patch, adapter_patch, pytest.raises(BaseExceptionGroup) as failure:
            async with _prepared_session(_settings(tmp_path)):
                # Stands in for the CoinJoin: it must not run on behind a
                # monitor that can no longer answer the counterparty.
                await asyncio.sleep(5)
                reached_end = True
    finally:
        FakeRuntime.next_monitor_error = None

    assert not reached_end
    assert FakeRuntime.last is not None and FakeRuntime.last.closed
    assert any(isinstance(error, RuntimeError) for error in failure.value.exceptions)


async def test_wait_for_settlement_returns_the_resolved_state(tmp_path: Path) -> None:
    runtime_patch, adapter_patch, _adapter = _patched_runtime()
    with runtime_patch, adapter_patch:
        async with _prepared_session(_settings(tmp_path)) as prepared:
            runtime = FakeRuntime.last
            assert runtime is not None
            states = iter(["AUTHORIZED", "AUTHORIZED", "COMPLETED"])
            runtime.store.get.side_effect = lambda _sid: MagicMock(state=next(states))
            assert await prepared.wait_for_settlement() == "COMPLETED"


async def test_an_interrupted_wait_keeps_the_session_and_reports_how_to_resume(
    taker: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    FakePrepared.opened = []
    settings = _settings(tmp_path)

    with patch.object(FakePrepared, "interrupt", True), pytest.raises(asyncio.CancelledError):
        await _run(taker, settings)

    (prepared,) = FakePrepared.opened
    assert prepared.exited
    prepared.cancel.assert_not_called()
    assert "jm-buyout --config" in capsys.readouterr().out
