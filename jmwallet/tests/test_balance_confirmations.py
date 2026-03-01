"""
Tests for min_confirmations filtering in balance calculation methods.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService


@pytest.fixture
def mock_backend():
    backend = MagicMock()
    backend.get_utxos = AsyncMock(return_value=[])
    backend.close = AsyncMock()
    return backend


@pytest.fixture
def wallet_service(test_mnemonic: str, mock_backend) -> WalletService:
    ws = WalletService(
        mnemonic=test_mnemonic,
        backend=mock_backend,
        network="regtest",
        mixdepth_count=2,
    )
    # UTXOs with varying confirmations: 0, 1, 5, 10
    ws.utxo_cache = {
        0: [
            UTXOInfo(
                txid="o",
                vout=0,
                value=1000,
                address="addr0",
                confirmations=0,
                scriptpubkey="",
                path="m/84'/0'/0'/0/0",
                mixdepth=0,
            ),
            UTXOInfo(
                txid="a",
                vout=0,
                value=2000,
                address="addr1",
                confirmations=1,
                scriptpubkey="",
                path="m/84'/0'/0'/0/1",
                mixdepth=0,
            ),
            UTXOInfo(
                txid="b",
                vout=0,
                value=4000,
                address="addr5",
                confirmations=5,
                scriptpubkey="",
                path="m/84'/0'/0'/0/2",
                mixdepth=0,
            ),
            UTXOInfo(
                txid="c",
                vout=0,
                value=8000,
                address="addr10",
                confirmations=10,
                scriptpubkey="",
                path="m/84'/0'/0'/0/3",
                mixdepth=0,
            ),
        ],
        1: [
            UTXOInfo(
                txid="d",
                vout=0,
                value=10000,
                address="addr_unconf",
                confirmations=0,
                scriptpubkey="",
                path="m/84'/0'/1'/0/0",
                mixdepth=1,
            ),
            UTXOInfo(
                txid="e",
                vout=0,
                value=20000,
                address="addr_conf",
                confirmations=1,
                scriptpubkey="",
                path="m/84'/0'/1'/0/1",
                mixdepth=1,
            ),
        ],
    }
    return ws


@pytest.mark.asyncio
async def test_get_balance_respects_min_confirmations(wallet_service):
    # Total at MD 0: 1000+2000+4000+8000 = 15000
    assert await wallet_service.get_balance(0) == 15000
    assert await wallet_service.get_balance(0, min_confirmations=0) == 15000

    # min_confirmations=1: 2000+4000+8000 = 14000
    assert await wallet_service.get_balance(0, min_confirmations=1) == 14000

    # min_confirmations=5: 4000+8000 = 12000
    assert await wallet_service.get_balance(0, min_confirmations=5) == 12000

    # min_confirmations=11: 0
    assert await wallet_service.get_balance(0, min_confirmations=11) == 0


@pytest.mark.asyncio
async def test_get_balance_for_offers_respects_min_confirmations(wallet_service):
    # Tests that get_balance_for_offers passes min_confirmations to get_balance
    assert await wallet_service.get_balance_for_offers(0, min_confirmations=0) == 15000
    assert await wallet_service.get_balance_for_offers(0, min_confirmations=1) == 14000
    assert await wallet_service.get_balance_for_offers(0, min_confirmations=5) == 12000


@pytest.mark.asyncio
async def test_get_total_balance_respects_min_confirmations(wallet_service):
    # MD 0: 15000 (all), 14000 (conf)
    # MD 1: 30000 (all), 20000 (conf)
    # Total: 45000 (all), 34000 (conf)
    assert await wallet_service.get_total_balance() == 45000
    assert await wallet_service.get_total_balance(min_confirmations=0) == 45000
    assert await wallet_service.get_total_balance(min_confirmations=1) == 34000
