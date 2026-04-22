"""
Tests for :meth:`taker.taker.Taker.stop`.

Focus on the ``close_wallet`` kwarg: by default the wallet is closed, but
callers that share a ``WalletService`` across multiple taker phases (for
example the jm-tumbler runner inside jmwalletd) must be able to opt out.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from _taker_test_helpers import make_taker_config, make_utxo

from taker.taker import Taker


@pytest.fixture
def mock_wallet() -> AsyncMock:
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    wallet.sync_all = AsyncMock()
    wallet.get_total_balance = AsyncMock(return_value=0)
    wallet.get_utxos = AsyncMock(return_value=[make_utxo(txid_char="a")])
    wallet.get_next_address_index = Mock(return_value=0)
    wallet.get_receive_address = Mock(return_value="bcrt1qdest")
    wallet.get_change_address = Mock(return_value="bcrt1qchange")
    wallet.close = AsyncMock()
    return wallet


@pytest.fixture
def mock_backend() -> AsyncMock:
    backend = AsyncMock()
    backend.can_provide_neutrino_metadata = Mock(return_value=True)
    return backend


@pytest.fixture
def mock_config() -> MagicMock:
    return make_taker_config()


@pytest.mark.asyncio
async def test_stop_closes_wallet_by_default(
    mock_wallet: AsyncMock, mock_backend: AsyncMock, mock_config: MagicMock
) -> None:
    taker = Taker(mock_wallet, mock_backend, mock_config)
    taker.directory_client = AsyncMock()

    await taker.stop()

    mock_wallet.close.assert_awaited_once()
    taker.directory_client.close_all.assert_awaited_once()
    assert taker.running is False


@pytest.mark.asyncio
async def test_stop_skips_wallet_close_when_opted_out(
    mock_wallet: AsyncMock, mock_backend: AsyncMock, mock_config: MagicMock
) -> None:
    taker = Taker(mock_wallet, mock_backend, mock_config)
    taker.directory_client = AsyncMock()

    await taker.stop(close_wallet=False)

    mock_wallet.close.assert_not_called()
    taker.directory_client.close_all.assert_awaited_once()
    assert taker.running is False
