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
GENERATOR_PUBKEY_HEX = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
CURRENT_BLOCK_HEIGHT = 900_000
VALID_CERT_EXPIRY = 901_152


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
    backend.get_block_height = AsyncMock(return_value=CURRENT_BLOCK_HEIGHT)
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
        "cert_expiry": VALID_CERT_EXPIRY,
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
        "cert_expiry": VALID_CERT_EXPIRY,
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
        "cert_expiry": VALID_CERT_EXPIRY,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_block_height", "should_be_valid"),
    [
        (VALID_CERT_EXPIRY - 1, True),
        (VALID_CERT_EXPIRY, True),
        (VALID_CERT_EXPIRY + 1, False),
    ],
)
async def test_certificate_expiry_reference_boundary(
    mock_wallet,
    mock_backend,
    mock_config,
    current_block_height,
    should_be_valid,
):
    """Certificates expire only after their absolute boundary block."""
    taker = Taker(mock_wallet, mock_backend, mock_config)
    current_time = int(time.time())
    txid = "d" * 64
    offer = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="Maker1",
        fidelity_bond_data={
            "utxo_txid": txid,
            "utxo_vout": 0,
            "locktime": current_time + 31536000,
            "utxo_pub": TEST_PUBKEY_HEX,
            "cert_expiry": VALID_CERT_EXPIRY,
        },
    )
    mock_backend.get_block_height.return_value = current_block_height
    mock_backend.verify_bonds.return_value = [
        BondVerificationResult(
            txid=txid,
            vout=0,
            value=1_000_000_000,
            confirmations=10000,
            block_time=current_time - (10000 * 600),
            valid=True,
        )
    ]

    await taker._update_offers_with_bond_values([offer])

    if should_be_valid:
        assert offer.fidelity_bond_value > 0
        mock_backend.verify_bonds.assert_awaited_once()
    else:
        assert offer.fidelity_bond_value == 0
        mock_backend.verify_bonds.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_offer_cannot_inherit_shared_bond_value(
    mock_wallet, mock_backend, mock_config
):
    """Only eligible proofs receive value when offers share a bond outpoint."""
    taker = Taker(mock_wallet, mock_backend, mock_config)
    current_time = int(time.time())
    txid = "e" * 64
    shared_bond_data = {
        "utxo_txid": txid,
        "utxo_vout": 0,
        "locktime": current_time + 31536000,
        "utxo_pub": TEST_PUBKEY_HEX,
    }
    expired_offer = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="ExpiredMaker",
        fidelity_bond_data={**shared_bond_data, "cert_expiry": CURRENT_BLOCK_HEIGHT - 1},
    )
    valid_offer = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=1,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="ValidMaker",
        fidelity_bond_data={**shared_bond_data, "cert_expiry": VALID_CERT_EXPIRY},
    )
    mock_backend.verify_bonds.return_value = [
        BondVerificationResult(
            txid=txid,
            vout=0,
            value=1_000_000_000,
            confirmations=10000,
            block_time=current_time - (10000 * 600),
            valid=True,
        )
    ]

    await taker._update_offers_with_bond_values([expired_offer, valid_offer])

    assert expired_offer.fidelity_bond_value == 0
    assert valid_offer.fidelity_bond_value > 0
    requests = mock_backend.verify_bonds.call_args.args[0]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_certificate_expiry_height_failure_is_fail_closed(
    mock_wallet, mock_backend, mock_config
):
    """A bond remains unvalued when its certificate expiry cannot be checked."""
    taker = Taker(mock_wallet, mock_backend, mock_config)
    offer = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="Maker1",
        fidelity_bond_data={
            "utxo_txid": "f" * 64,
            "utxo_vout": 0,
            "locktime": int(time.time()) + 31536000,
            "utxo_pub": TEST_PUBKEY_HEX,
            "cert_expiry": VALID_CERT_EXPIRY,
        },
    )
    mock_backend.get_block_height.side_effect = RuntimeError("backend unavailable")

    await taker._update_offers_with_bond_values([offer])

    assert offer.fidelity_bond_value == 0
    mock_backend.verify_bonds.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_height", [None, "900000", -1, True])
async def test_invalid_certificate_expiry_height_is_fail_closed(
    mock_wallet, mock_backend, mock_config, invalid_height
):
    """Malformed backend heights do not allow bond valuation."""
    taker = Taker(mock_wallet, mock_backend, mock_config)
    offer = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="Maker1",
        fidelity_bond_data={
            "utxo_txid": "1" * 64,
            "utxo_vout": 0,
            "locktime": int(time.time()) + 31536000,
            "utxo_pub": TEST_PUBKEY_HEX,
            "cert_expiry": VALID_CERT_EXPIRY,
        },
        fidelity_bond_value=123,
    )
    mock_backend.get_block_height.return_value = invalid_height

    await taker._update_offers_with_bond_values([offer])

    assert offer.fidelity_bond_value == 0
    mock_backend.verify_bonds.assert_not_awaited()


@pytest.mark.asyncio
async def test_prevalued_expired_offer_is_revalidated(mock_wallet, mock_backend, mock_config):
    """Previously assigned bond values cannot bypass a later expiry check."""
    taker = Taker(mock_wallet, mock_backend, mock_config)
    offer = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=1000000,
        txfee=1000,
        cjfee="0.001",
        counterparty="Maker1",
        fidelity_bond_data={
            "utxo_txid": "2" * 64,
            "utxo_vout": 0,
            "locktime": int(time.time()) + 31536000,
            "utxo_pub": TEST_PUBKEY_HEX,
            "cert_expiry": CURRENT_BLOCK_HEIGHT - 1,
        },
        fidelity_bond_value=123,
    )

    await taker._update_offers_with_bond_values([offer])

    assert offer.fidelity_bond_value == 0
    mock_backend.verify_bonds.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflicting_claims_are_verified_independently(
    mock_wallet, mock_backend, mock_config
):
    """An invalid claim cannot suppress a valid script claim for the same outpoint."""
    taker = Taker(mock_wallet, mock_backend, mock_config)
    current_time = int(time.time())
    txid = "3" * 64

    def make_offer(counterparty: str, pubkey: str) -> Offer:
        return Offer(
            ordertype=OfferType.SW0_RELATIVE,
            oid=0,
            minsize=10000,
            maxsize=1000000,
            txfee=1000,
            cjfee="0.001",
            counterparty=counterparty,
            fidelity_bond_data={
                "utxo_txid": txid,
                "utxo_vout": 0,
                "locktime": current_time + 31536000,
                "utxo_pub": pubkey,
                "cert_expiry": VALID_CERT_EXPIRY,
            },
        )

    invalid_offer = make_offer("InvalidMaker", TEST_PUBKEY_HEX)
    valid_offer = make_offer("ValidMaker", GENERATOR_PUBKEY_HEX)
    mock_backend.verify_bonds.return_value = [
        BondVerificationResult(
            txid=txid,
            vout=0,
            value=1_000_000_000,
            confirmations=10000,
            block_time=0,
            valid=False,
            error="ScriptPubKey mismatch",
        ),
        BondVerificationResult(
            txid=txid,
            vout=0,
            value=1_000_000_000,
            confirmations=10000,
            block_time=current_time - (10000 * 600),
            valid=True,
        ),
    ]

    await taker._update_offers_with_bond_values([invalid_offer, valid_offer])

    assert invalid_offer.fidelity_bond_value == 0
    assert valid_offer.fidelity_bond_value > 0
    requests = mock_backend.verify_bonds.call_args.args[0]
    assert len(requests) == 2
    assert requests[0].scriptpubkey != requests[1].scriptpubkey


@pytest.mark.asyncio
async def test_shared_bond_claim_is_verified_once_across_nicks_and_offers(
    mock_wallet, mock_backend, mock_config
):
    """Reference makers may publish several offers per bond, and a copied bond
    certificate may appear under another nick. Either way the backend sees one
    claim, and the cheapest offer per bond is chosen later at selection time."""
    taker = Taker(mock_wallet, mock_backend, mock_config)
    current_time = int(time.time())
    txid = "4" * 64
    bond_data = {
        "utxo_txid": txid,
        "utxo_vout": 1,
        "locktime": current_time + 31536000,
        "utxo_pub": TEST_PUBKEY_HEX,
        "cert_expiry": VALID_CERT_EXPIRY,
    }
    offers = [
        Offer(
            ordertype=OfferType.SW0_RELATIVE,
            oid=oid,
            minsize=10000,
            maxsize=1000000,
            txfee=1000,
            cjfee="0.001",
            counterparty=nick,
            fidelity_bond_data=dict(bond_data),
        )
        for nick, oid in (("MakerA", 0), ("MakerA", 1), ("MakerB", 0))
    ]
    mock_backend.verify_bonds.return_value = [
        BondVerificationResult(
            txid=txid,
            vout=1,
            value=1_000_000_000,
            confirmations=10000,
            block_time=current_time - (10000 * 600),
            valid=True,
        )
    ]

    await taker._update_offers_with_bond_values(offers)

    assert len(mock_backend.verify_bonds.call_args.args[0]) == 1
    assert offers[0].fidelity_bond_value > 0
    assert {offer.fidelity_bond_value for offer in offers} == {offers[0].fidelity_bond_value}
