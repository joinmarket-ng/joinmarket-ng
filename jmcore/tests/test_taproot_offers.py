"""
Tests for Taproot offer types and associated helper functions.
"""

from __future__ import annotations

import pytest
from jmcore.models import (
    OfferType,
    get_default_offer_types,
    is_absolute_offer_type,
    is_taproot_offer_type,
)


def test_is_taproot_offer_type() -> None:
    """Test is_taproot_offer_type helper."""
    assert is_taproot_offer_type(OfferType.TR0_ABSOLUTE) is True
    assert is_taproot_offer_type(OfferType.TR0_RELATIVE) is True
    assert is_taproot_offer_type(OfferType.TRA_ABSOLUTE) is True
    assert is_taproot_offer_type(OfferType.TRA_RELATIVE) is True

    assert is_taproot_offer_type(OfferType.SW0_ABSOLUTE) is False
    assert is_taproot_offer_type(OfferType.SW0_RELATIVE) is False
    assert is_taproot_offer_type(OfferType.SWA_ABSOLUTE) is False
    assert is_taproot_offer_type(OfferType.SWA_RELATIVE) is False


def test_is_absolute_offer_type() -> None:
    """Test is_absolute_offer_type helper with new types."""
    assert is_absolute_offer_type(OfferType.TR0_ABSOLUTE) is True
    assert is_absolute_offer_type(OfferType.TRA_ABSOLUTE) is True
    assert is_absolute_offer_type(OfferType.TR0_RELATIVE) is False
    assert is_absolute_offer_type(OfferType.TRA_RELATIVE) is False

    # Check legacy types too
    assert is_absolute_offer_type(OfferType.SW0_ABSOLUTE) is True
    assert is_absolute_offer_type(OfferType.SW0_RELATIVE) is False


def test_get_default_offer_types() -> None:
    """Test get_default_offer_types helper."""
    # Taproot wallet defaults to TR offers
    tr_defaults = get_default_offer_types("p2tr")
    assert OfferType.TR0_ABSOLUTE in tr_defaults
    assert OfferType.TR0_RELATIVE in tr_defaults
    assert OfferType.SW0_ABSOLUTE not in tr_defaults

    # Legacy (native segwit) defaults to SW offers
    sw_defaults = get_default_offer_types("p2wpkh")
    assert OfferType.SW0_ABSOLUTE in sw_defaults
    assert OfferType.SW0_RELATIVE in sw_defaults
    assert OfferType.TR0_ABSOLUTE not in sw_defaults
