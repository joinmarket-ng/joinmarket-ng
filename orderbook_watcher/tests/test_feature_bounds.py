"""Feature metadata stays bounded through probe caches and served offer merges."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer, OfferType, OrderBook
from jmcore.protocol import FeatureSet

from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.health_checker import MakerHealthChecker, MakerHealthStatus


def _offer(nick: str, features: dict[str, bool] | None = None, directory: str = "one") -> Offer:
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=100_000,
        maxsize=10_000_000,
        txfee=1000,
        cjfee="0.001",
        features=features or {},
        directory_node=directory,
    )


def _status(nick: str, names: set[str]) -> MakerHealthStatus:
    # Model metadata already present in a cache, including malformed values
    # that cannot enter through the newly validated probe parser.
    features = FeatureSet()
    features.features.update(names)
    return MakerHealthStatus(
        location=f"{nick}.onion:5222",
        nick=nick,
        reachable=True,
        last_check_time=1.0,
        last_success_time=1.0,
        consecutive_failures=0,
        features=features,
    )


def test_probe_cache_validates_before_replacing_existing_status() -> None:
    checker = MakerHealthChecker(network="regtest")
    original = _status("J5maker", {"future.v1"})
    checker._store_status(original)
    with pytest.raises(ValueError):
        checker._store_status(_status("J5maker", {"€" * 43}))
    assert checker.health_status[original.location] is original


@pytest.mark.parametrize("violation", ["bytes", "count", "syntax", "merged-count"])
def test_cached_probe_metadata_cannot_amplify_offer_enrichment(violation: str) -> None:
    aggregator = OrderbookAggregator([], "regtest", "")
    names = {
        "bytes": {"€" * 43},
        "count": {f"f{i}" for i in range(65)},
        "syntax": {"unsafe+flag"},
        "merged-count": {"new"},
    }[violation]
    existing = (
        {f"f{i}": True for i in range(64)} if violation == "merged-count" else {"known": True}
    )
    bad = _offer("J5bad", dict(existing))
    good = _offer("J5good", {"known": True})
    book = OrderBook(timestamp=datetime.now(UTC), offers=[bad, good])
    client = DirectoryClient("host", 1234, "regtest")
    client._active_peers = {nick: f"{nick}.onion:5222" for nick in ("J5bad", "J5good")}
    aggregator.clients = {"one": client}
    statuses = [_status("J5bad", names), _status("J5good", {"future.v1"})]
    aggregator.health_checker.health_status = {status.location: status for status in statuses}
    aggregator._enrich_offers_with_health_status(book)
    assert bad.features == existing
    assert bad.directly_reachable is None
    assert good.features == {"known": True, "future.v1": True}
    assert good.directly_reachable is True
    assert max(len(offer.features) for offer in book.offers) <= 64


@pytest.mark.asyncio
async def test_feature_discovery_isolates_invalid_statuses_and_client_merges() -> None:
    aggregator = OrderbookAggregator([], "regtest", "")
    blocked, accepting = (DirectoryClient("host", 1234, "regtest") for _ in range(2))
    existing = {f"f{i}": True for i in range(64)}
    for client in (blocked, accepting):
        client._active_peers = {nick: f"{nick}.onion:5222" for nick in ("J5bad", "J5good")}
        for nick in ("J5bad", "J5good"):
            flags = existing if client is blocked and nick == "J5good" else {}
            client._store_offer((nick, 0), _offer(nick, dict(flags)))
    aggregator.clients = {"blocked": blocked, "accepting": accepting}
    statuses = [_status("J5bad", {"€" * 43}), _status("J5good", {"future.v1"})]
    aggregator.health_checker.check_makers_batch = AsyncMock(
        return_value={status.location: status for status in statuses}
    )
    await aggregator._check_makers_without_features()
    assert blocked.peer_features == {}
    assert blocked.offers[("J5good", 0)].offer.features == existing
    assert accepting.peer_features == {"J5good": {"future.v1": True}}
    assert accepting.offers[("J5good", 0)].offer.features == {"future.v1": True}
    assert accepting.offers[("J5bad", 0)].offer.features == {}


@pytest.mark.parametrize("count", [63, 64])
def test_cross_directory_offer_merge_validates_prospective_feature_count(count: int) -> None:
    aggregator = OrderbookAggregator([], "regtest", "")
    first = _offer("J5maker", {f"f{i}": True for i in range(count)})
    second = _offer("J5maker", {"future.v1": True}, directory="two")
    book = OrderBook(timestamp=datetime.now(UTC))
    aggregator._merge_offers_into_orderbook(book, [first, second])
    assert len(book.offers) == 1
    assert len(book.offers[0].features) == 64
    assert ("future.v1" in book.offers[0].features) is (count == 63)
    assert book.offers[0].directory_nodes == ["one", "two"]


def test_invalid_cached_offer_metadata_is_not_served_or_mutated() -> None:
    aggregator = OrderbookAggregator([], "regtest", "")
    original = _offer("J5maker", {"€" * 43: True})
    book = OrderBook(timestamp=datetime.now(UTC))
    aggregator._merge_offers_into_orderbook(book, [original])
    assert len(book.offers) == 1
    assert book.offers[0].features == {}
    assert original.features == {"€" * 43: True}
