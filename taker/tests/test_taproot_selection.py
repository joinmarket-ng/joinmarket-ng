"""
Tests for Taproot offer selection and filtering in the taker.
"""

from __future__ import annotations

import pytest
from jmcore.models import Offer, OfferType
from taker.config import MaxCjFee
from taker.orderbook import filter_offers, OrderbookManager


@pytest.fixture
def mixed_offers() -> list[Offer]:
    """Mix of legacy and Taproot offers."""
    return [
        Offer(
            counterparty="maker_sw0",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=1000,
            cjfee="0.001",
        ),
        Offer(
            counterparty="maker_tr0",
            oid=0,
            ordertype=OfferType.TR0_RELATIVE,
            minsize=10_000,
            maxsize=1_000_000,
            txfee=1000,
            cjfee="0.001",
        ),
    ]


def test_filter_offers_by_taproot_type(mixed_offers: list[Offer]) -> None:
    """Test that filter_offers correctly filters by Taproot offer types."""
    max_cj_fee = MaxCjFee(abs_fee=10000, rel_fee="0.01")
    
    # Filter for Taproot offers only
    filtered = filter_offers(
        mixed_offers, 
        100_000, 
        max_cj_fee, 
        allowed_types={OfferType.TR0_RELATIVE, OfferType.TR0_ABSOLUTE}
    )
    
    assert len(filtered) == 1
    assert filtered[0].counterparty == "maker_tr0"
    assert filtered[0].ordertype == OfferType.TR0_RELATIVE


def test_orderbook_manager_select_makers_p2tr(mixed_offers: list[Offer], tmp_path) -> None:
    """Test that OrderbookManager respects allowed_types when selecting makers."""
    max_cj_fee = MaxCjFee(abs_fee=10000, rel_fee="0.01")
    manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
    manager.update_offers(mixed_offers)
    
    # Select makers with Taproot allowance
    orders, total_fee = manager.select_makers(
        cj_amount=100_000, 
        n=1, 
        allowed_types={OfferType.TR0_RELATIVE}
    )
    
    assert len(orders) == 1
    nick = list(orders.keys())[0]
    assert nick == "maker_tr0"


def test_orderbook_manager_select_makers_legacy(mixed_offers: list[Offer], tmp_path) -> None:
    """Test that OrderbookManager respects allowed_types for legacy selection."""
    max_cj_fee = MaxCjFee(abs_fee=10000, rel_fee="0.01")
    manager = OrderbookManager(max_cj_fee, data_dir=tmp_path)
    manager.update_offers(mixed_offers)
    
    # Select makers with legacy allowance
    orders, total_fee = manager.select_makers(
        cj_amount=100_000, 
        n=1, 
        allowed_types={OfferType.SW0_RELATIVE}
    )
    
    assert len(orders) == 1
    nick = list(orders.keys())[0]
    assert nick == "maker_sw0"
