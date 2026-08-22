"""MakerBot lifecycle tests for channel-input startup recovery."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.channel_input_config import ChannelInputConfig
from jmcore.models import NetworkType, Offer, OfferType

from maker.bot import MakerBot
from maker.config import MakerConfig


def _config(tmp_path: Path) -> MakerConfig:
    cert = tmp_path / "tls.cert"
    admin = tmp_path / "admin.macaroon"
    escrow = tmp_path / "escrow.macaroon"
    cert.write_bytes(b"cert")
    admin.write_bytes(b"admin")
    escrow.write_bytes(b"escrow")
    return MakerConfig(
        mnemonic="test " * 12,
        data_dir=tmp_path,
        directory_servers=["localhost:5222"],
        network=NetworkType.REGTEST,
        address_type="p2tr",
        offer_type=OfferType.TR0_ABSOLUTE,
        no_fidelity_bond=True,
        channel_input=ChannelInputConfig(
            enabled=True,
            lnd_grpc_url="https://127.0.0.1:10009",
            lnd_tls_cert_path=cert,
            lnd_macaroon_path=admin,
            lnd_escrow_macaroon_path=escrow,
            allowed_channel_points=(f"{'11' * 32}:0",),
        ),
    )


def _bot(tmp_path: Path) -> MakerBot:
    wallet = MagicMock()
    wallet.address_type = "p2tr"
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    wallet.wallet_fingerprint = "deadbeef"
    wallet.network = "regtest"
    backend = MagicMock()
    backend.can_resolve_foreign_prevouts.return_value = True
    backend.can_provide_neutrino_metadata.return_value = False
    return MakerBot(wallet=wallet, backend=backend, config=_config(tmp_path))


def test_channel_input_maker_restores_outer_nick_identity(tmp_path: Path) -> None:
    first = _bot(tmp_path)
    second = _bot(tmp_path)

    assert first.nick == second.nick
    assert first.nick_identity.public_key_hex == second.nick_identity.public_key_hex


@pytest.mark.asyncio
async def test_runtime_reconciles_before_capability_activation(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    events: list[str] = []
    recovery_outpoint = SimpleNamespace(txid="11" * 32, vout=0)
    registry = SimpleNamespace(
        recovery_outpoints=(recovery_outpoint,),
        session_count=1,
    )

    async def reconcile(backend: object, node_key: str) -> None:
        assert backend is lnd
        assert node_key == "02" + "22" * 32
        events.append("reconcile")

    registry.reconcile_all = reconcile
    lnd = SimpleNamespace(close=AsyncMock())
    initialized = SimpleNamespace(
        backend=lnd,
        node_info=SimpleNamespace(identity_pubkey="02" + "22" * 32),
        candidates=(SimpleNamespace(funding_outpoint=SimpleNamespace(txid="33" * 32, vout=1)),),
    )

    def load(**kwargs: object) -> object:
        assert kwargs["store"] is bot._channel_input_store
        events.append("load")
        return registry

    async def initialize(*args: object, **kwargs: object) -> object:
        assert kwargs["recovery_outpoints"] == (recovery_outpoint,)
        events.append("initialize")
        return initialized

    bot._directory_pool.enable_channel_input_v1 = MagicMock(
        side_effect=lambda: events.append("enable")
    )
    with (
        patch("maker.bot.MakerChannelInputRegistry.load", side_effect=load),
        patch(
            "jmswap.channel_input.initialize_channel_input_backend",
            new=AsyncMock(side_effect=initialize),
        ),
    ):
        await bot._initialize_channel_input_runtime()

    assert events == ["load", "initialize", "reconcile", "enable"]
    assert bot._channel_input_backend is initialized
    assert bot._channel_input_registry is registry
    assert bot.channel_input_capability_validated is True


def test_runtime_capability_disables_after_candidate_admission(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    candidate_outpoint = SimpleNamespace(txid="33" * 32, vout=1)
    bot._channel_input_backend = SimpleNamespace(
        candidates=(SimpleNamespace(funding_outpoint=candidate_outpoint),)
    )
    bot._channel_input_registry = SimpleNamespace(recovery_outpoints=(), session_count=0)
    bot._directory_pool.enable_channel_input_v1 = MagicMock()
    bot._directory_pool.disable_channel_input_v1 = MagicMock()

    assert bot._refresh_channel_input_capability() is True
    bot._directory_pool.enable_channel_input_v1.assert_called_once()

    bot._channel_input_registry = SimpleNamespace(
        recovery_outpoints=(candidate_outpoint,), session_count=1
    )
    assert bot._refresh_channel_input_capability() is False
    assert bot.channel_input_capability_validated is False
    bot._directory_pool.disable_channel_input_v1.assert_called_once()


@pytest.mark.asyncio
async def test_recovery_only_runtime_does_not_advertise_capacity(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    registry = SimpleNamespace(recovery_outpoints=(), session_count=1, reconcile_all=AsyncMock())
    initialized = SimpleNamespace(
        backend=SimpleNamespace(close=AsyncMock()),
        node_info=SimpleNamespace(identity_pubkey="02" + "22" * 32),
        candidates=(),
    )
    bot._directory_pool.enable_channel_input_v1 = MagicMock()

    with (
        patch("maker.bot.MakerChannelInputRegistry.load", return_value=registry),
        patch(
            "jmswap.channel_input.initialize_channel_input_backend",
            new=AsyncMock(return_value=initialized),
        ),
    ):
        await bot._initialize_channel_input_runtime()

    assert bot._channel_input_backend is initialized
    assert bot.channel_input_capability_validated is False
    bot._directory_pool.enable_channel_input_v1.assert_not_called()


@pytest.mark.asyncio
async def test_reconciliation_failure_closes_backend_without_activation(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    registry = SimpleNamespace(
        recovery_outpoints=(),
        session_count=0,
        reconcile_all=AsyncMock(side_effect=RuntimeError("recovery conflict")),
    )
    close = AsyncMock()
    initialized = SimpleNamespace(
        backend=SimpleNamespace(close=close),
        node_info=SimpleNamespace(identity_pubkey="02" + "22" * 32),
        candidates=(object(),),
    )

    with (
        patch("maker.bot.MakerChannelInputRegistry.load", return_value=registry),
        patch(
            "jmswap.channel_input.initialize_channel_input_backend",
            new=AsyncMock(return_value=initialized),
        ),
        pytest.raises(RuntimeError, match="recovery conflict"),
    ):
        await bot._initialize_channel_input_runtime()

    close.assert_awaited_once()
    assert bot._channel_input_backend is None
    assert bot._channel_input_registry is None
    assert bot.channel_input_capability_validated is False


@pytest.mark.asyncio
async def test_start_initializes_channel_input_before_directory_connection(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot.wallet.sync_all = AsyncMock()
    bot.wallet.reconstruct_imported_state_safe = AsyncMock()
    bot.wallet.get_total_balance = AsyncMock(return_value=1_000_000)
    bot.backend.get_block_height = AsyncMock(return_value=100)
    bot.current_offers = [
        Offer(
            counterparty=bot.nick,
            oid=0,
            ordertype=OfferType.TR0_ABSOLUTE,
            minsize=100_000,
            maxsize=1_000_000,
            txfee=0,
            cjfee="500",
        )
    ]
    bot.offer_manager.create_offers = AsyncMock(return_value=bot.current_offers)
    bot._setup_tor_hidden_service = AsyncMock(return_value=None)
    bot._initialize_channel_input_runtime = AsyncMock(
        side_effect=RuntimeError("stop after channel-input initialization")
    )
    bot._connect_to_directories_with_retry = AsyncMock()

    with pytest.raises(RuntimeError, match="stop after channel-input initialization"):
        await bot.start()

    bot._initialize_channel_input_runtime.assert_awaited_once()
    bot._connect_to_directories_with_retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_later_startup_failure_closes_initialized_channel_input(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    bot.wallet.sync_all = AsyncMock()
    bot.wallet.reconstruct_imported_state_safe = AsyncMock()
    bot.wallet.get_total_balance = AsyncMock(return_value=1_000_000)
    bot.backend.get_block_height = AsyncMock(return_value=100)
    bot.current_offers = [
        Offer(
            counterparty=bot.nick,
            oid=0,
            ordertype=OfferType.TR0_ABSOLUTE,
            minsize=100_000,
            maxsize=1_000_000,
            txfee=0,
            cjfee="500",
        )
    ]
    bot.offer_manager.create_offers = AsyncMock(return_value=bot.current_offers)
    bot._setup_tor_hidden_service = AsyncMock(return_value=None)
    close = AsyncMock()

    async def initialize() -> None:
        bot._channel_input_backend = SimpleNamespace(backend=SimpleNamespace(close=close))
        bot._channel_input_registry = MagicMock()
        bot.channel_input_capability_validated = True

    bot._initialize_channel_input_runtime = AsyncMock(side_effect=initialize)
    bot._connect_to_directories_with_retry = AsyncMock(
        side_effect=RuntimeError("directory startup failed")
    )

    with pytest.raises(RuntimeError, match="directory startup failed"):
        await bot.start()

    close.assert_awaited_once()
    assert bot._channel_input_backend is None
    assert bot._channel_input_registry is None
    assert bot.channel_input_capability_validated is False


@pytest.mark.asyncio
async def test_stop_closes_and_disables_channel_input_runtime(tmp_path: Path) -> None:
    bot = _bot(tmp_path)
    close = AsyncMock()
    bot._channel_input_backend = SimpleNamespace(backend=SimpleNamespace(close=close))
    bot._channel_input_registry = MagicMock()
    bot.channel_input_capability_validated = True
    bot._directory_pool.disable_channel_input_v1 = MagicMock()

    await bot.stop()

    close.assert_awaited_once()
    bot._directory_pool.disable_channel_input_v1.assert_called_once_with()
    assert bot._channel_input_backend is None
    assert bot._channel_input_registry is None
    assert bot.channel_input_capability_validated is False
