"""Tests for the opt-in prepared buyout lifetime of the maker CLI.

A maker never discovers, adopts or migrates a buyout: only an explicit
``--buyout-config`` plus ``--buyout-session`` pair turns one on, and every rule
that could disqualify the file is decided before a runtime opens a journal, a
node connection or a channel signature. What happens afterwards is equally
bounded: the adapter reaches exactly one maker, the settlement monitor spans
the maker's whole lifetime, and every exit stops the maker before the runtime
is released.
"""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import scriptpubkey_to_address
from jmswap import buyout_config as buyout_config_module
from jmswap import buyout_runtime as buyout_runtime_module
from jmswap import coinjoin_funding as coinjoin_funding_module
from jmswap.buyout_config import BuyoutSettings
from loguru import logger
from typer.testing import CliRunner

from maker import cli as cli_module
from maker.cli import app
from maker.config import MakerConfig
from maker.fidelity import ExpiredFidelityBondCertificateError

runner = CliRunner()

FINGERPRINT = "0123abcd"
SESSION_ID = "ab" * 32

EVENTS: list[str] = []
"""Ordered lifetime events of one CLI run (runtime, monitor and maker)."""


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
        "lnd_identity": _pubkey("maker-identity"),
        "lnd_tls_cert": tmp_path / "tls.cert",
        "lnd_peer_macaroon": tmp_path / "peer.macaroon",
        "lnd_escrow_macaroon": tmp_path / "escrow.macaroon",
        "bitcoin_rpc_url": "http://127.0.0.1:18443/",
        "bitcoin_rpc_user": "buyout",
        "bitcoin_rpc_password": "regtest-placeholder",
        "allowed_peers": (_pubkey("maker-peer"),),
        "payout_address": PAYOUT_ADDRESS,
        "mixdepth": 0,
        "wallet_fingerprint": FINGERPRINT,
        "poll_interval_seconds": 0.01,
    }
    values.update(overrides)
    return BuyoutSettings.model_validate(values)


class FakeRuntime:
    """A buyer runtime whose journal, binding check and monitor are observable."""

    last: FakeRuntime | None = None
    monitor_error: Exception | None = None
    rejects = False

    def __init__(self, settings: BuyoutSettings) -> None:
        self.settings = settings
        self.buyer = MagicMock(name="buyer")
        self.closed = False
        FakeRuntime.last = self

    async def __aenter__(self) -> FakeRuntime:
        EVENTS.append("runtime-open")
        return self

    async def __aexit__(self, *args: object) -> None:
        self.closed = True
        EVENTS.append("runtime-close")

    def require_buyer_session(self, session_id: str) -> MagicMock:
        if FakeRuntime.rejects:
            raise RuntimeError("buyer session is not bound to this runtime")
        EVENTS.append(f"require:{session_id}")
        return MagicMock()

    async def run(self, stop: asyncio.Event) -> None:
        EVENTS.append("monitor")
        if FakeRuntime.monitor_error is not None:
            raise FakeRuntime.monitor_error
        try:
            await stop.wait()
        except asyncio.CancelledError:
            # Records whether the maker signalled the monitor before the task
            # group tore it down.
            EVENTS.append(f"monitor-stopped:{stop.is_set()}")
            raise
        EVENTS.append("monitor-stopped:True")


@pytest.fixture(autouse=True)
def _clean_lifetime() -> Any:
    EVENTS.clear()
    FakeRuntime.last = None
    FakeRuntime.monitor_error = None
    FakeRuntime.rejects = False
    yield
    FakeRuntime.last = None
    FakeRuntime.monitor_error = None
    FakeRuntime.rejects = False


def _start(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    buyout_settings: BuyoutSettings | None = None,
    address_type: str = "p2tr",
    arguments: list[str] | None = None,
    start_error: BaseException | None = None,
    build_error: BaseException | None = None,
    offers: list[Any] | None = None,
    record_states: list[str] | Exception | None = None,
    loop_ticks: int | None = None,
) -> SimpleNamespace:
    """Run ``jm-maker start`` with every external dependency replaced."""
    settings = MagicMock()
    settings.get_data_dir.return_value = tmp_path
    config = MakerConfig(
        mnemonic="test " * 12,
        directory_servers=["localhost:5222"],
        network="regtest",
        data_dir=tmp_path,
        address_type=address_type,
        offer_type="tr0reloffer" if address_type == "p2tr" else "sw0reloffer",
    )
    wallet = MagicMock()
    wallet.backend = MagicMock()
    wallet.wallet_fingerprint = FINGERPRINT

    bot = MagicMock()
    bot.nick = "J5BuyoutMaker"
    # The default run reaches the idle loop; the caller decides how it ends.
    bot.start = AsyncMock(side_effect=start_error)

    async def stop() -> None:
        EVENTS.append("maker-stopped")

    bot.stop = AsyncMock(side_effect=stop)
    bot.current_offers = list(offers or [])

    async def update_offers(expected_balance: int | None = None) -> None:
        # Stands in for the real refresh: zero liquidity withdraws everything,
        # which is what stops the idle loop from refreshing again.
        EVENTS.append(f"offers-updated:{expected_balance}")
        bot.current_offers = []

    bot._update_offers = AsyncMock(side_effect=update_offers)

    maker_kwargs: dict[str, Any] = {}

    def make_bot(*_args: object, **kwargs: object) -> MagicMock:
        maker_kwargs.update(kwargs)
        if build_error is not None:
            raise build_error
        return bot

    notifier = MagicMock()

    async def notify_startup(**_kwargs: object) -> None:
        # A real notification yields, which is when the monitor starts.
        await asyncio.sleep(0)

    notifier.notify_startup = notify_startup
    write_nick_state = MagicMock()
    remove_nick_state = MagicMock()
    loader = MagicMock(return_value=buyout_settings)
    adapter = MagicMock(name="channel-buyout")
    adapter.session_id = SESSION_ID
    if isinstance(record_states, Exception):
        adapter.buyer.store.get.side_effect = record_states
    elif record_states is not None:
        states = list(record_states)

        def read_record(session_id: str) -> MagicMock:
            EVENTS.append(f"journal-read:{session_id}")
            return MagicMock(state=states.pop(0) if len(states) > 1 else states[0])

        adapter.buyer.store.get.side_effect = read_record
    channel_buyout = MagicMock(return_value=adapter)

    monkeypatch.setattr(cli_module, "setup_cli", lambda *_a, **_k: settings)
    monkeypatch.setattr(cli_module, "ensure_config_file", lambda _data_dir: None)
    monkeypatch.setattr(
        cli_module,
        "resolve_mnemonic",
        lambda *_a, **_k: SimpleNamespace(
            mnemonic="test " * 12,
            bip39_passphrase="",
            creation_height=None,
            mnemonic_file=tmp_path / "wallets" / "imported.mnemonic",
        ),
    )
    monkeypatch.setattr(cli_module, "build_maker_config", lambda **_k: config)
    monkeypatch.setattr(cli_module, "create_wallet_service", lambda _config: wallet)
    monkeypatch.setattr(cli_module, "MakerBot", make_bot)
    monkeypatch.setattr(cli_module, "get_notifier", lambda *_a, **_k: notifier)
    monkeypatch.setattr(cli_module, "write_nick_state", write_nick_state)
    monkeypatch.setattr(cli_module, "remove_nick_state", remove_nick_state)
    monkeypatch.setattr(buyout_config_module, "load_buyout_settings", loader)
    monkeypatch.setattr(buyout_runtime_module, "BuyoutRuntime", FakeRuntime)
    monkeypatch.setattr(coinjoin_funding_module, "ChannelBuyout", channel_buyout)

    if loop_ticks is not None:
        # Bounds the maker's idle loop: every tick of its one-second wait runs
        # immediately, and the run ends after ``loop_ticks`` of them.
        real_sleep = asyncio.sleep

        async def bounded_sleep(delay: float, *args: Any, **kwargs: Any) -> Any:
            if delay < 1:
                return await real_sleep(delay, *args, **kwargs)
            EVENTS.append("tick")
            if EVENTS.count("tick") > loop_ticks:
                raise ExpiredFidelityBondCertificateError("test clock exhausted")
            return await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", bounded_sleep)

    if arguments is None:
        arguments = [
            "--buyout-config",
            str(tmp_path / "buyout.toml"),
            "--buyout-session",
            SESSION_ID,
        ]

    messages: list[str] = []
    handler = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        result = runner.invoke(app, ["start", *arguments], prog_name="jm-maker")
    finally:
        logger.remove(handler)

    return SimpleNamespace(
        result=result,
        messages=messages,
        bot=bot,
        maker_kwargs=maker_kwargs,
        loader=loader,
        adapter=adapter,
        channel_buyout=channel_buyout,
        write_nick_state=write_nick_state,
        remove_nick_state=remove_nick_state,
        config=config,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["--buyout-config", "buyout.toml"],
        ["--buyout-session", SESSION_ID],
    ],
)
def test_buyout_options_are_only_accepted_as_a_pair(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    setup = MagicMock()
    monkeypatch.setattr(cli_module, "setup_cli", setup)

    result = runner.invoke(app, ["start", *arguments], prog_name="jm-maker")

    assert result.exit_code == 1
    setup.assert_not_called()


@pytest.mark.parametrize("session", ["", "AB" * 32, "ab" * 31, "zz" * 32, SESSION_ID + "ab"])
def test_a_malformed_session_id_is_refused_before_anything_loads(
    monkeypatch: pytest.MonkeyPatch, session: str
) -> None:
    setup = MagicMock()
    monkeypatch.setattr(cli_module, "setup_cli", setup)

    result = runner.invoke(
        app,
        ["start", "--buyout-config", "buyout.toml", "--buyout-session", session],
        prog_name="jm-maker",
    )

    assert result.exit_code == 1
    setup.assert_not_called()


def test_start_help_documents_the_opt_in_pair() -> None:
    result = runner.invoke(app, ["start", "--help"], prog_name="jm-maker")

    assert result.exit_code == 0
    assert "--buyout-config" in result.stdout
    assert "--buyout-session" in result.stdout


@pytest.mark.parametrize(
    ("overrides", "address_type", "expected"),
    [
        ({"enabled": False}, "p2tr", "disabled"),
        (
            {"network": "signet", "payout_address": SIGNET_PAYOUT_ADDRESS},
            "p2tr",
            "networks differ",
        ),
        ({"wallet_fingerprint": "deadbeef"}, "p2tr", "different wallet fingerprint"),
        ({"wallet_fingerprint": None}, "p2tr", "wallet_fingerprint"),
        ({"mixdepth": 99}, "p2tr", "outside this"),
        ({}, "p2wpkh", "Taproot"),
    ],
)
def test_a_disqualified_file_never_reaches_a_buyout_resource(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    overrides: dict[str, object],
    address_type: str,
    expected: str,
) -> None:
    if not overrides.get("enabled", True):
        settings = BuyoutSettings()
    else:
        settings = _settings(tmp_path, **overrides)

    run = _start(monkeypatch, tmp_path, buyout_settings=settings, address_type=address_type)

    assert run.result.exit_code == 1
    assert any(expected in message for message in run.messages)
    # Nothing was opened, and the maker itself was never built.
    assert FakeRuntime.last is None
    assert EVENTS == []
    assert run.maker_kwargs == {}
    run.write_nick_state.assert_not_called()


def test_the_prepared_session_reaches_the_maker_and_is_released(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _start(
        monkeypatch,
        tmp_path,
        buyout_settings=_settings(tmp_path),
        start_error=ExpiredFidelityBondCertificateError("renew the certificate"),
    )

    assert run.result.exit_code == 1
    assert run.maker_kwargs["buyout"] is run.adapter
    runtime = FakeRuntime.last
    assert runtime is not None
    assert run.channel_buyout.call_args.args == (runtime.buyer, SESSION_ID)
    assert EVENTS[:3] == ["runtime-open", f"require:{SESSION_ID}", "monitor"]
    # The maker stops before the monitor is signalled and released, and both
    # before the journal and node connections close.
    assert EVENTS[-3:] == ["maker-stopped", "monitor-stopped:True", "runtime-close"]
    assert runtime.closed
    run.bot.stop.assert_awaited_once()
    run.remove_nick_state.assert_called_once_with(tmp_path, "maker_taproot")
    assert any("certificate expired" in message for message in run.messages)


def test_a_dead_monitor_stops_the_maker_and_releases_the_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeRuntime.monitor_error = RuntimeError("the private message subscription ended")

    run = _start(monkeypatch, tmp_path, buyout_settings=_settings(tmp_path))

    assert run.result.exit_code == 1
    runtime = FakeRuntime.last
    assert runtime is not None and runtime.closed
    assert EVENTS[-2:] == ["maker-stopped", "runtime-close"]
    run.bot.stop.assert_awaited_once()
    run.remove_nick_state.assert_called_once()
    assert any("buyout lifetime failed" in message for message in run.messages)


def test_a_session_this_runtime_does_not_own_releases_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    FakeRuntime.rejects = True

    run = _start(monkeypatch, tmp_path, buyout_settings=_settings(tmp_path))

    assert run.result.exit_code == 1
    runtime = FakeRuntime.last
    assert runtime is not None and runtime.closed
    assert EVENTS == ["runtime-open", "runtime-close"]
    # No maker was built, so no nick was published for an unusable session.
    assert run.maker_kwargs == {}
    run.write_nick_state.assert_not_called()
    assert any("could not be opened" in message for message in run.messages)


def test_a_rejected_binding_stops_the_maker_before_it_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _start(
        monkeypatch,
        tmp_path,
        buyout_settings=_settings(tmp_path),
        build_error=ValueError("Buyout is not bound to this wallet"),
    )

    assert run.result.exit_code == 1
    runtime = FakeRuntime.last
    assert runtime is not None and runtime.closed
    assert run.maker_kwargs["buyout"] is run.adapter
    run.bot.start.assert_not_awaited()
    run.write_nick_state.assert_not_called()
    assert any("cannot fund this maker" in message for message in run.messages)


def test_an_ordinary_start_never_loads_or_opens_a_buyout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _start(
        monkeypatch,
        tmp_path,
        arguments=[],
        start_error=ExpiredFidelityBondCertificateError("renew"),
    )

    assert run.result.exit_code == 1
    run.loader.assert_not_called()
    run.channel_buyout.assert_not_called()
    assert FakeRuntime.last is None
    assert EVENTS == ["maker-stopped"]
    assert run.maker_kwargs["buyout"] is None
    run.bot.stop.assert_awaited_once()


@pytest.mark.parametrize("state", ["ROUND_RESERVED", "CANCELED", "COMPLETED", "CONFLICTED"])
def test_a_consumed_buyout_withdraws_the_advertised_offers_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, state: str
) -> None:
    """Offers backed by channels that are gone do not wait for the next rescan."""
    run = _start(
        monkeypatch,
        tmp_path,
        buyout_settings=_settings(tmp_path),
        offers=[MagicMock(name="offer")],
        record_states=[state],
        loop_ticks=4,
    )

    assert run.result.exit_code == 1
    run.bot._update_offers.assert_awaited_once_with(expected_balance=0)
    # One journal read withdrew the offers; with none left there is nothing to
    # withdraw, so the loop stops reading and refreshing every second.
    assert EVENTS.count(f"journal-read:{SESSION_ID}") == 1
    assert EVENTS.count("offers-updated:0") == 1
    assert EVENTS.count("tick") == 5
    assert run.bot.current_offers == []


def test_an_accepted_buyout_keeps_its_offers_advertised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _start(
        monkeypatch,
        tmp_path,
        buyout_settings=_settings(tmp_path),
        offers=[MagicMock(name="offer")],
        record_states=["ACCEPTED"],
        loop_ticks=3,
    )

    assert run.result.exit_code == 1
    run.bot._update_offers.assert_not_awaited()
    assert EVENTS.count(f"journal-read:{SESSION_ID}") == 3
    assert len(run.bot.current_offers) == 1


def test_an_ordinary_maker_never_reads_a_buyout_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    run = _start(
        monkeypatch,
        tmp_path,
        arguments=[],
        offers=[MagicMock(name="offer")],
        record_states=["CANCELED"],
        loop_ticks=3,
    )

    assert run.result.exit_code == 1
    run.adapter.buyer.store.get.assert_not_called()
    run.bot._update_offers.assert_not_awaited()
    assert len(run.bot.current_offers) == 1


def test_an_unreadable_journal_stops_the_maker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unknown record state is never guessed: the maker shuts down instead."""
    run = _start(
        monkeypatch,
        tmp_path,
        buyout_settings=_settings(tmp_path),
        offers=[MagicMock(name="offer")],
        record_states=RuntimeError("the buyout journal is unreadable"),
        loop_ticks=4,
    )

    assert run.result.exit_code == 1
    run.bot._update_offers.assert_not_awaited()
    runtime = FakeRuntime.last
    assert runtime is not None and runtime.closed
    assert EVENTS[-3:] == ["maker-stopped", "monitor-stopped:True", "runtime-close"]
    run.bot.stop.assert_awaited_once()
    assert any("buyout lifetime failed" in message for message in run.messages)


def test_importing_the_maker_cli_never_imports_jmswap() -> None:
    """An ordinary maker must not pull the buyout stack into its process."""
    code = (
        "import sys, maker.cli;"
        "assert not [name for name in sys.modules if name.split('.')[0] == 'jmswap'], "
        "sorted(name for name in sys.modules if name.split('.')[0] == 'jmswap')"
    )

    subprocess.run([sys.executable, "-c", code], check=True)
