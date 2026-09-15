"""Wallet lock revokes retained market capabilities before awaiting shutdown."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jmcore.market_keys import MarketKeysClosedError, MarketKeyScope
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService
from jmwalletd.state import DaemonState


@pytest.mark.asyncio
async def test_lock_revokes_real_capability_before_stopping_maker(tmp_path: Path) -> None:
    wallet = WalletService(
        "all " * 11 + "all",
        AsyncMock(spec=BlockchainBackend),
        network="regtest",
        data_dir=tmp_path,
    )
    state = DaemonState(data_dir=tmp_path)
    state.wallet_service = wallet
    retained = wallet.market_keys
    scope = MarketKeyScope(network="regtest", chain_hash="11" * 32, role="buyer", period=4)
    retained.signing_public_key(scope)

    async def stop() -> None:
        with pytest.raises(MarketKeysClosedError):
            retained.signing_public_key(scope)

    state._maker_ref = MagicMock()
    state._maker_ref.stop = AsyncMock(side_effect=stop)
    assert await state.lock_wallet() is False
    assert state.wallet_service is None
    with pytest.raises(MarketKeysClosedError):
        retained.encryption_public_key(scope)
    assert await state.lock_wallet() is True
