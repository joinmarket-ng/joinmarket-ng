"""Bond lookups use the Bitcoin network, which can differ from the protocol network."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest
from jmcore.btc_script import derive_bond_address
from jmcore.models import FidelityBond, OrderBook

from orderbook_watcher.aggregator import OrderbookAggregator

TEST_PUBKEY_HEX = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
CURRENT_BLOCK_HEIGHT = 900_000
CERT_EXPIRY = 901_152


@pytest.mark.asyncio
async def test_regtest_bond_lookup_uses_bitcoin_network() -> None:
    # Regtest setups speak the testnet protocol but settle on regtest (docker-compose.yml).
    locktime = int(time.time()) + 31_536_000
    bond = FidelityBond(
        counterparty="Maker1",
        utxo_txid="a" * 64,
        utxo_vout=0,
        bond_value=None,
        locktime=locktime,
        amount=0,
        script=TEST_PUBKEY_HEX,
        utxo_confirmations=0,
        cert_expiry=CERT_EXPIRY,
        fidelity_bond_data={
            "maker_nick": "Maker1",
            "utxo_txid": "a" * 64,
            "utxo_vout": 0,
            "locktime": locktime,
            "utxo_pub": TEST_PUBKEY_HEX,
            "cert_expiry": CERT_EXPIRY,
        },
    )
    backend = AsyncMock()
    backend.get_block_height.return_value = CURRENT_BLOCK_HEIGHT
    backend.verify_bonds.return_value = []
    aggregator = OrderbookAggregator(
        directory_nodes=[],
        network="testnet",
        bitcoin_network="regtest",
        mempool_api_url="",
        blockchain_backend=backend,
    )

    await aggregator._calculate_bond_values(OrderBook(fidelity_bonds=[bond]))

    (requests,) = backend.verify_bonds.await_args.args
    expected = derive_bond_address(bytes.fromhex(TEST_PUBKEY_HEX), locktime, "regtest")
    assert requests[0].address == expected.address
    assert requests[0].address.startswith("bcrt1")
