"""Compatibility tests for the relocated credential-market store."""

from __future__ import annotations

import jmcore.market_store as core_market_store

import taker.market_store as taker_market_store


def test_taker_market_store_exports_core_objects() -> None:
    assert taker_market_store.MarketStore is core_market_store.MarketStore
    assert taker_market_store.MarketStoreError is core_market_store.MarketStoreError
    assert taker_market_store.MarketStoreConflictError is core_market_store.MarketStoreConflictError
    assert taker_market_store.MarketStoreCorruptError is core_market_store.MarketStoreCorruptError
    assert taker_market_store.MarketStoreExpiredError is core_market_store.MarketStoreExpiredError
    assert (
        taker_market_store.MarketStoreUnavailableError
        is core_market_store.MarketStoreUnavailableError
    )
    assert taker_market_store.PaymentRail is core_market_store.PaymentRail
    assert taker_market_store.Product is core_market_store.Product
