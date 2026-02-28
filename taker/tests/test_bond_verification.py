"""
Unit tests for fidelity bond verification in Taker.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, Mock

import pytest
from jmcore.models import NetworkType, Offer, OfferType
from jmwallet.backends.base import BondVerificationResult

from taker.config import TakerConfig
from taker.taker import Taker

# Valid 33-byte compressed pubkey (hex) for tests
TEST_PUBKEY_HEX = "02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"


@pytest.fixture
def mock_wallet():
    """Mock wallet service."""
    wallet = AsyncMock()
    wallet.mixdepth_count = 5
    return wallet


@pytest.fixture
def mock_backend():
    """Mock blockchain backend."""
    backend = AsyncMock()
    # Default to mainnet-like behavior
    backend.can_provide_neutrino_metadata = Mock(return_value=True)
    return backend


@pytest.fixture
def mock_config():
    """Mock taker config."""
    config = TakerConfig(
        mnemonic="abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about",
        network=NetworkType.REGTEST,
        directory_servers=["localhost:5222"],
    )
    return config


@pytest.mark.asyncio
async def test_update_offers_with_bond_values(mock_wallet, mock_backend, mock_config):
    """Test that fidelity bond values are correctly calculated and updated."""

    # Setup Taker
    taker = Taker(mock_wallet, mock_backend, mock_config)

    # Mock current time
    current_time = int(time.time())

    # Create bond data
    # Bond 1: Valid bond, locked for 1 year in future
    txid1 = "a" * 64
    vout1 = 0
    locktime1 = current_time + 31536000  # +1 year
    conf_time = current_time - (10000 * 600)  # approx 10000 blocks ago
    bond_data1 = {
        "utxo_txid": txid1,
        "utxo_vout": vout1,
        "locktime": locktime1,
        "utxo_pub": TEST_PUBKEY_HEX,
        "cert_expiry": current_time + 100000,
    }

    # Create Offers
    offer1 = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="Maker1",
        fidelity_bond_data=bond_data1,
    )

    offer2 = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="Maker2",
        # No bond data
        fidelity_bond_data=None,
    )

    offers = [offer1, offer2]

    # Mock verify_bonds to return a valid result for Bond 1
    mock_backend.verify_bonds = AsyncMock(
        return_value=[
            BondVerificationResult(
                txid=txid1,
                vout=vout1,
                value=1_000_000_000,
                confirmations=10000,
                block_time=conf_time,
                valid=True,
            )
        ]
    )

    # Run the method
    await taker._update_offers_with_bond_values(offers)

    # Assertions

    # Offer 1 should have updated fidelity_bond_value
    assert offer1.fidelity_bond_value > 0
    print(f"Calculated bond value: {offer1.fidelity_bond_value}")

    # Offer 2 should remain 0
    assert offer2.fidelity_bond_value == 0

    # Verify verify_bonds was called once with 1 bond
    mock_backend.verify_bonds.assert_called_once()
    bond_requests = mock_backend.verify_bonds.call_args[0][0]
    assert len(bond_requests) == 1
    assert bond_requests[0].txid == txid1
    assert bond_requests[0].vout == vout1


@pytest.mark.asyncio
async def test_update_offers_bond_missing_utxo(mock_wallet, mock_backend, mock_config):
    """Test handling of missing UTXO (spent or invalid)."""
    taker = Taker(mock_wallet, mock_backend, mock_config)

    txid = "b" * 64
    bond_data = {
        "utxo_txid": txid,
        "utxo_vout": 0,
        "locktime": int(time.time()) + 10000,
        "utxo_pub": TEST_PUBKEY_HEX,
        "cert_expiry": 0,
    }

    offer = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="Maker1",
        fidelity_bond_data=bond_data,
    )

    # Backend verify_bonds returns invalid result
    mock_backend.verify_bonds = AsyncMock(
        return_value=[
            BondVerificationResult(
                txid=txid,
                vout=0,
                value=0,
                confirmations=0,
                block_time=0,
                valid=False,
                error="UTXO not found or spent",
            )
        ]
    )

    await taker._update_offers_with_bond_values([offer])

    assert offer.fidelity_bond_value == 0


@pytest.mark.asyncio
async def test_update_offers_bond_unconfirmed_utxo(mock_wallet, mock_backend, mock_config):
    """Test handling of unconfirmed UTXO."""
    taker = Taker(mock_wallet, mock_backend, mock_config)

    txid = "c" * 64
    bond_data = {
        "utxo_txid": txid,
        "utxo_vout": 0,
        "locktime": int(time.time()) + 10000,
        "utxo_pub": TEST_PUBKEY_HEX,
        "cert_expiry": 0,
    }

    offer = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="Maker1",
        fidelity_bond_data=bond_data,
    )

    # Backend verify_bonds returns unconfirmed result
    mock_backend.verify_bonds = AsyncMock(
        return_value=[
            BondVerificationResult(
                txid=txid,
                vout=0,
                value=100000000,
                confirmations=0,
                block_time=0,
                valid=False,
                error="UTXO unconfirmed",
            )
        ]
    )

    await taker._update_offers_with_bond_values([offer])

    assert offer.fidelity_bond_value == 0
