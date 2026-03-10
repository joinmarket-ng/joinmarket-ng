"""
Unit tests for Taker address-type filtering (Taproot support).

Tests that the Taker correctly rejects Makers who provide CoinJoin
or change addresses that do not match the Taker's address type
(to avoid privacy leaks from mixed address types).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from _taker_test_helpers import make_taker_config
from jmcore.models import Offer, OfferType
from jmwallet.wallet.models import UTXOInfo

from taker.taker import MakerSession, Taker


@pytest.fixture
def mock_wallet_p2tr():
    wallet = AsyncMock()
    wallet.address_type = "p2tr"
    return wallet


@pytest.fixture
def mock_wallet_p2wpkh():
    wallet = AsyncMock()
    wallet.address_type = "p2wpkh"
    return wallet


@pytest.fixture
def mock_backend():
    backend = AsyncMock()
    backend.get_utxo = AsyncMock(
        return_value=UTXOInfo(
            txid="a" * 64,
            vout=0,
            value=10_000_000,
            address="bcrt1qmaker",
            confirmations=100,
            scriptpubkey="0014" + "00" * 20,
            path="m/84'/1'/0'/0/0",
            mixdepth=0,
        )
    )
    return backend


@pytest.fixture
def taker_config():
    return make_taker_config()


def create_mock_maker_session(nick: str) -> MakerSession:
    offer = Offer(
        ordertype=OfferType.SW0_RELATIVE,
        oid=0,
        minsize=10000,
        maxsize=100_000_000,
        txfee=500,
        cjfee=250,
        counterparty=nick,
    )
    session = MakerSession(nick=nick, offer=offer)
    session.responded_fill = True
    session.responded_auth = False
    return session


@pytest.mark.asyncio
async def test_ioauth_rejects_p2wpkh_maker_when_taker_is_p2tr(
    mock_wallet_p2tr, mock_backend, taker_config
):
    """Test that a P2TR taker rejects a P2WPKH maker."""
    taker = Taker(mock_wallet_p2tr, mock_backend, taker_config)
    nick = "J5MakerP2WPKH"

    cj_addr_p2wpkh = "bcrt1qqyqszqgpqyqszqgpqyqszqgpqyqszqgpvxat9t"
    change_addr_p2wpkh = "bcrt1qqgpqyqszqgpqyqszqgpqyqszqgpqyqszazmwwa"

    with patch("taker.taker.logger") as mock_logger:
        is_valid = taker._validate_maker_address_types(nick, cj_addr_p2wpkh, change_addr_p2wpkh)

        mock_logger.warning.assert_called_with(
            f"Rejecting maker {nick}: taker uses P2TR but maker "
            f"sent non-Taproot addresses (cj={cj_addr_p2wpkh[:16]}..., "
            f"change={change_addr_p2wpkh[:16]}...)"
        )
        assert is_valid is False


@pytest.mark.asyncio
async def test_ioauth_accepts_p2tr_maker_when_taker_is_p2tr(
    mock_wallet_p2tr, mock_backend, taker_config
):
    """Test that a P2TR taker accepts a P2TR maker."""
    taker = Taker(mock_wallet_p2tr, mock_backend, taker_config)
    nick = "J5MakerP2TR"

    cj_addr_p2tr = "bcrt1p9qszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqsza0t9j"
    change_addr_p2tr = "bcrt1p8qszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszy7vj2"

    is_valid = taker._validate_maker_address_types(nick, cj_addr_p2tr, change_addr_p2tr)
    assert is_valid is True


@pytest.mark.asyncio
async def test_ioauth_rejects_p2tr_maker_when_taker_is_p2wpkh(
    mock_wallet_p2wpkh, mock_backend, taker_config
):
    """Test that a P2WPKH taker rejects a P2TR maker."""
    taker = Taker(mock_wallet_p2wpkh, mock_backend, taker_config)
    nick = "J5MakerP2TR"

    cj_addr_p2tr = "bcrt1p9qszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqsza0t9j"
    change_addr_p2tr = "bcrt1p8qszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszqgpqyqszy7vj2"

    with patch("taker.taker.logger") as mock_logger:
        is_valid = taker._validate_maker_address_types(nick, cj_addr_p2tr, change_addr_p2tr)

        mock_logger.warning.assert_called_with(
            f"Rejecting maker {nick}: taker uses P2WPKH but maker "
            f"sent Taproot addresses (cj={cj_addr_p2tr[:16]}..., "
            f"change={change_addr_p2tr[:16]}...)"
        )
        assert is_valid is False


@pytest.mark.asyncio
async def test_ioauth_accepts_p2wpkh_maker_when_taker_is_p2wpkh(
    mock_wallet_p2wpkh, mock_backend, taker_config
):
    """Test that a P2WPKH taker accepts a P2WPKH maker."""
    taker = Taker(mock_wallet_p2wpkh, mock_backend, taker_config)
    nick = "J5MakerP2WPKH"

    cj_addr_p2wpkh = "bcrt1qqyqszqgpqyqszqgpqyqszqgpqyqszqgpvxat9t"
    change_addr_p2wpkh = "bcrt1qqgpqyqszqgpqyqszqgpqyqszqgpqyqszazmwwa"

    is_valid = taker._validate_maker_address_types(nick, cj_addr_p2wpkh, change_addr_p2wpkh)
    assert is_valid is True
