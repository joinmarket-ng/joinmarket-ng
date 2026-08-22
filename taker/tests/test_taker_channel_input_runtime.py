"""Taker channel-input backend admission and recovery identity."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_taker_config
from jmcore.channel_input_config import ChannelInputConfig
from jmcore.models import OfferType

from taker.taker import Taker


def _channel_input_config(tmp_path: Path) -> ChannelInputConfig:
    return ChannelInputConfig(
        enabled=True,
        lnd_grpc_url="https://127.0.0.1:10009",
        lnd_tls_cert_path=tmp_path / "tls.cert",
        lnd_macaroon_path=tmp_path / "admin.macaroon",
        lnd_escrow_macaroon_path=tmp_path / "escrow.macaroon",
        allowed_channel_points=("11" * 32 + ":0",),
    )


def test_channel_input_taker_reuses_durable_outer_identity(
    tmp_path: Path,
) -> None:
    wallet = AsyncMock()
    wallet.address_type = "p2tr"
    wallet.mixdepth_count = 5
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=True)
    config = make_taker_config(
        data_dir=str(tmp_path),
        address_type="p2tr",
        preferred_offer_type=OfferType.TR0_RELATIVE,
        channel_input=_channel_input_config(tmp_path),
    )

    first = Taker(wallet, backend, config)
    second = Taker(wallet, backend, config)

    assert first.nick == second.nick
    assert first.directory_client.channel_input_v1 is False
    assert second.directory_client.channel_input_v1 is False
    assert first._channel_input_store is not None
    assert first._channel_input_store.load_all().safe


@pytest.mark.asyncio
async def test_channel_input_capability_enables_only_after_backend_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = AsyncMock()
    wallet.address_type = "p2tr"
    wallet.mixdepth_count = 5
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=True)
    config = make_taker_config(
        data_dir=str(tmp_path),
        address_type="p2tr",
        preferred_offer_type=OfferType.TR0_RELATIVE,
        channel_input=_channel_input_config(tmp_path),
    )
    taker = Taker(wallet, backend, config)
    initialized_backend = SimpleNamespace(
        backend=SimpleNamespace(close=AsyncMock()),
        node_info=SimpleNamespace(identity_pubkey="02" + "22" * 32),
        candidates=(),
    )
    initialize = AsyncMock(return_value=initialized_backend)
    monkeypatch.setattr(
        "jmswap.channel_input.initialize_channel_input_backend",
        initialize,
    )
    taker.directory_client.connect_all = AsyncMock(return_value=1)

    assert taker.directory_client.channel_input_v1 is False
    await taker.connect()

    assert taker.directory_client.channel_input_v1 is True
    initialize.assert_awaited_once_with(
        config.channel_input,
        network="regtest",
        offer_type="tr0reloffer",
        recovery_outpoints=(),
    )
    await taker.stop(close_wallet=False)
    initialized_backend.backend.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_input_reconciles_before_capability_advertisement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = AsyncMock()
    wallet.address_type = "p2tr"
    wallet.mixdepth_count = 5
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=True)
    config = make_taker_config(
        data_dir=str(tmp_path),
        address_type="p2tr",
        preferred_offer_type=OfferType.TR0_RELATIVE,
        channel_input=_channel_input_config(tmp_path),
    )
    events: list[str] = []
    registry = SimpleNamespace(
        recovery_outpoints=(),
        reconcile_all=AsyncMock(side_effect=RuntimeError("recovery conflict")),
    )
    monkeypatch.setattr(
        "taker.channel_input_runtime.TakerChannelInputRegistry.load", lambda **_kwargs: registry
    )
    taker = Taker(wallet, backend, config)
    initialized_backend = SimpleNamespace(
        backend=SimpleNamespace(close=AsyncMock()),
        node_info=SimpleNamespace(identity_pubkey="02" + "22" * 32),
        candidates=(),
    )
    monkeypatch.setattr(
        "jmswap.channel_input.initialize_channel_input_backend",
        AsyncMock(return_value=initialized_backend),
    )
    original_enable = taker.directory_client.enable_channel_input_v1

    def enable() -> None:
        events.append("enable")
        original_enable()

    taker.directory_client.enable_channel_input_v1 = enable

    with pytest.raises(RuntimeError, match="recovery conflict"):
        await taker.connect()
    registry.reconcile_all.assert_awaited_once_with(
        initialized_backend.backend, initialized_backend.node_info.identity_pubkey
    )
    assert events == []
    assert taker.directory_client.channel_input_v1 is False
    assert taker._channel_input_backend is None
    initialized_backend.backend.close.assert_awaited_once()
    await taker.stop(close_wallet=False)
    initialized_backend.backend.close.assert_awaited_once()
