"""Daemon takers share the wallet's state directory, not global settings' path."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jmcore.models import NetworkType
from jmcore.settings import JoinMarketSettings
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService
from jmwalletd.routers.coinjoin import build_coinjoin_taker_config
from jmwalletd.routers.tumbler import build_tumbler_taker_config
from taker.config import TakerConfig
from taker.taker import Taker


@pytest.mark.parametrize("operation", ["coinjoin", "tumbler"])
async def test_custom_daemon_directory_constructs_a_wallet_bound_taker(
    tmp_path: Path, operation: str
) -> None:
    settings = JoinMarketSettings()
    settings.data_dir = tmp_path / "global-settings"
    settings.network_config.network = NetworkType.REGTEST
    settings.network_config.bitcoin_network = NetworkType.REGTEST
    mnemonic = "all " * 11 + "all"
    wallet_dir = tmp_path / "daemon-wallet"
    backend = AsyncMock(spec=BlockchainBackend)
    wallet = WalletService(mnemonic, backend, network="regtest", data_dir=wallet_dir)
    kwargs = dict(
        mnemonic=mnemonic,
        jm_settings=settings,
        taker_config_cls=TakerConfig,
        data_dir=wallet_dir,
    )
    if operation == "coinjoin":
        config = build_coinjoin_taker_config(
            body=SimpleNamespace(
                amount_sats=100_000,
                destination="INTERNAL",
                mixdepth=0,
                counterparties=3,
                txfee=None,
            ),
            **kwargs,
        )
    else:
        config = build_tumbler_taker_config(
            phase=SimpleNamespace(amount=100_000, mixdepth=0, counterparty_count=3),
            **kwargs,
        )
    try:
        taker = Taker(wallet, backend, config)
        try:
            assert taker.config.data_dir == wallet_dir
            assert taker.podle_manager._wallet_id == wallet.market_wallet_id
            assert not settings.data_dir.exists()
        finally:
            taker.podle_manager.close()
    finally:
        await wallet.close()
