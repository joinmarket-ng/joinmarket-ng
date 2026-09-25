from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from jmcore.channel_ring import (
    RING_SETUP_HOLD_SLACK_SECONDS,
    ChannelRingConfig,
    ChannelRingNodeConfig,
)
from jmcore.config import TorControlConfig
from jmcore.models import NetworkType, Offer, OfferType
from jmcore.protocol import FEATURE_PRIVATE_CHANNEL_RING

from maker.bot import MakerBot
from maker.coinjoin import CoinJoinSession
from maker.config import MakerConfig
from maker.directory_pool import MakerDirectoryPool
from maker.offers import OfferManager

TEST_MNEMONIC = "abandon " * 11 + "about"
# An explicit Tor control block keeps these tests off the operator's config
# file, which the environment-derived default would otherwise read.
ISOLATED_TOR_CONTROL = TorControlConfig(enabled=False)


def test_maker_capability_is_disabled_until_backend_validation() -> None:
    config = MakerConfig(
        mnemonic=TEST_MNEMONIC,
        directory_servers=[],
        tor_control=ISOLATED_TOR_CONTROL,
    )
    pool = MakerDirectoryPool(
        config=config,
        nick_identity=object(),
        neutrino_compat=False,
    )
    before = pool._build_client_kwargs("directory", 5222)
    assert before[FEATURE_PRIVATE_CHANNEL_RING] is False

    pool.enable_private_channel_ring()
    after = pool._build_client_kwargs("directory", 5222)
    assert after[FEATURE_PRIVATE_CHANNEL_RING] is True


def _ring_maker_config(data_dir: Path, *, enabled: bool) -> MakerConfig:
    return MakerConfig(
        mnemonic=TEST_MNEMONIC,
        directory_servers=["localhost:5222"],
        network=NetworkType.REGTEST,
        address_type="p2tr",
        offer_type=OfferType.TR0_RELATIVE,
        data_dir=data_dir,
        tor_control=ISOLATED_TOR_CONTROL,
        channel_ring=ChannelRingConfig(
            enabled=enabled,
            nodes={
                "local": ChannelRingNodeConfig(
                    lnd_grpc_url="https://127.0.0.1:10009",
                    lnd_tls_cert_path=data_dir / "tls.cert",
                    lnd_macaroon_path=data_dir / "admin.macaroon",
                    onion_endpoint="a" * 56 + ".onion:9735",
                )
            },
            mixdepth_nodes={0: "local"},
            node_binding_directory=data_dir / "node-bindings",
            persistence_directory=data_dir / "rings",
        ),
    )


def _taproot_maker_dependencies() -> tuple[MagicMock, MagicMock]:
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    wallet.address_type = "p2tr"
    backend = MagicMock()
    backend.can_resolve_foreign_prevouts.return_value = True
    return wallet, backend


def test_explicit_buyout_with_enabled_ring_is_rejected_before_advertising(tmp_path: Path) -> None:
    """One round funds its change from a buyout escrow or a ring, never both."""
    wallet, backend = _taproot_maker_dependencies()
    config = _ring_maker_config(tmp_path, enabled=True)

    with pytest.raises(ValueError, match="cannot be used by the same maker"):
        MakerBot(wallet=wallet, backend=backend, config=config, buyout=MagicMock())


def test_enabled_ring_without_buyout_is_accepted(tmp_path: Path) -> None:
    wallet, backend = _taproot_maker_dependencies()
    bot = MakerBot(
        wallet=wallet, backend=backend, config=_ring_maker_config(tmp_path, enabled=True)
    )

    assert bot.buyout is None
    assert bot._channel_ring_store is not None
    # The capability is only advertised after backend validation in start().
    assert bot.channel_ring_capability_validated is False


def test_ring_setup_hold_is_explicit_and_ordinary_pre_sign_default_is_unchanged(
    tmp_path: Path,
) -> None:
    ordinary = MakerConfig(
        mnemonic=TEST_MNEMONIC,
        directory_servers=[],
        tor_control=ISOLATED_TOR_CONTROL,
    )
    ring = _ring_maker_config(tmp_path, enabled=True)

    assert ordinary.pre_sign_timeout_sec == 180
    assert ring.channel_ring.maker_setup_hold_seconds == 660
    assert ring.channel_ring.maker_setup_hold_seconds == (
        ring.channel_ring.setup_timeout_seconds
        + ring.channel_ring.hold_safety_margin_seconds
        + RING_SETUP_HOLD_SLACK_SECONDS
    )


def test_ring_setup_hold_must_cover_the_enabled_ring_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must cover"):
        _ring_maker_config(tmp_path, enabled=True).channel_ring.model_copy(
            update={"maker_setup_hold_seconds": 630}
        ).validate_policy()


async def test_ring_offer_and_input_selection_use_only_mapped_mixdepth(tmp_path: Path) -> None:
    from jmwallet.wallet.models import UTXOInfo

    wallet, backend = _taproot_maker_dependencies()
    wallet.get_locked_input_outpoints.return_value = set()
    wallet.get_maker_rotation_lineage_outpoints = AsyncMock(return_value=set())
    wallet.get_balance_for_offers = AsyncMock(return_value=5_000_000)
    wallet.reserve_coinjoin_inputs.return_value = True
    wallet.get_new_internal_address.side_effect = ["equal-output", "change-output"]
    selected = UTXOInfo(
        txid="11" * 32,
        vout=0,
        value=5_000_000,
        address="bcrt1ptest",
        confirmations=6,
        scriptpubkey="5120" + "22" * 32,
        path="m/86'/1'/1'/0/0",
        mixdepth=1,
    )
    wallet.select_utxos_with_merge.return_value = [selected]
    config = _ring_maker_config(tmp_path, enabled=True)
    config.channel_ring = config.channel_ring.model_copy(update={"mixdepth_nodes": {1: "local"}})
    manager = OfferManager(wallet, config, "J5Maker")
    balances = await manager.get_mixdepth_offer_balances()
    assert balances == {0: 0, 1: 5_000_000, 2: 0, 3: 0, 4: 0}
    assert [item.args[0] for item in wallet.get_balance_for_offers.call_args_list] == [1]
    wallet.get_balance_for_offers.reset_mock()

    session = CoinJoinSession(
        taker_nick="J5Taker",
        wallet=wallet,
        backend=backend,
        offer=Offer(
            counterparty="J5Maker",
            oid=0,
            ordertype=OfferType.TR0_ABSOLUTE,
            minsize=10_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee=500,
        ),
        allowed_mixdepths=frozenset(config.channel_ring.mixdepth_nodes),
    )
    session.amount = 500_000
    utxos, equal_address, change_address, mixdepth = await session._select_our_utxos()
    assert mixdepth == 1
    assert utxos == {(selected.txid, selected.vout): selected}
    assert (equal_address, change_address) == ("equal-output", "change-output")
    assert wallet.get_new_internal_address.call_args_list == [call(2), call(1)]
    assert [item.args[0] for item in wallet.get_balance_for_offers.call_args_list] == [1]


async def test_shutdown_keeps_journal_lease_until_late_detached_handler_exits(
    tmp_path: Path,
) -> None:
    from jmswap.channel_ring_nodes import initialize_channel_ring_nodes

    wallet, backend = _taproot_maker_dependencies()
    bot = MakerBot(
        wallet=wallet, backend=backend, config=_ring_maker_config(tmp_path, enabled=True)
    )
    bot._channel_ring_nodes = await initialize_channel_ring_nodes(
        bot.config.channel_ring,
        network="regtest",
        offer_type="tr0absoffer",
        wallet_identity="a" * 64,
        mixdepth_count=5,
        data_directory=tmp_path,
    )
    release = asyncio.Event()
    started = asyncio.Event()
    detached = asyncio.Event()

    async def delayed_handler() -> None:
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue

    handler = asyncio.create_task(delayed_handler())

    async def listener() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            bot._register_detached_handler_task(handler)
            detached.set()

    bot.listen_tasks.append(asyncio.create_task(listener()))
    await started.wait()
    await asyncio.sleep(0)
    stop = asyncio.create_task(bot.stop())
    code = """
import sys
from pathlib import Path
from jmcore.secure_files import exclusive_file_lock
try:
    with exclusive_file_lock(Path(sys.argv[1]), blocking=False):
        pass
except BlockingIOError:
    raise SystemExit(7)
"""
    lock = tmp_path / "rings" / "runtime.lock"
    try:
        await asyncio.wait_for(detached.wait(), timeout=5)
        assert not stop.done()
        probe = subprocess.run([sys.executable, "-c", code, str(lock)], timeout=10, check=False)
        assert probe.returncode == 7
        release.set()
        await asyncio.wait_for(stop, timeout=5)
        assert handler.done()
        probe = subprocess.run([sys.executable, "-c", code, str(lock)], timeout=10, check=False)
        assert probe.returncode == 0
    finally:
        release.set()
        await asyncio.gather(handler, stop, return_exceptions=True)


async def test_shutdown_rejects_new_fill_and_handler_admission(tmp_path: Path) -> None:
    wallet, backend = _taproot_maker_dependencies()
    bot = MakerBot(
        wallet=wallet, backend=backend, config=_ring_maker_config(tmp_path, enabled=True)
    )
    bot._stopping = True
    session = MagicMock()
    handler = AsyncMock()
    await bot._handle_fill("peer", "not parsed during shutdown")
    await bot._dispatch_session_handler(session, handler, name="must-not-start")
    assert not bot.active_sessions
    assert not bot._session_handler_tasks
    session.run_handler.assert_not_called()
    handler.assert_not_awaited()


def test_buyout_without_ring_is_accepted(tmp_path: Path) -> None:
    wallet, backend = _taproot_maker_dependencies()
    buyout = MagicMock()

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("maker.bot.bound_buyout_mixdepth", lambda *args, **kwargs: 2)
        bot = MakerBot(
            wallet=wallet,
            backend=backend,
            config=_ring_maker_config(tmp_path, enabled=False),
            buyout=buyout,
        )

    assert bot.buyout is buyout
    assert bot.buyout_mixdepth == 2
    assert bot._channel_ring_store is None
