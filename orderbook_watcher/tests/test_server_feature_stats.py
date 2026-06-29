"""
Tests for the ``feature_stats`` orderbook output (issue #483).

Feature share is computed over bonded makers (``fidelity_bond_value > 0``)
only. Bondless makers are sybil-cheap — a single operator can announce an
unbounded number of nicks — so including them in the denominator lets one
actor skew "% of makers supporting feature X" arbitrarily.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from jmcore.models import Offer, OfferType, OrderBook
from jmcore.settings import OrderbookWatcherSettings

from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.server import OrderbookServer


def _make_offer(
    nick: str,
    *,
    bond: int = 0,
    features: dict[str, bool] | None = None,
    oid: int = 0,
) -> Offer:
    return Offer(
        counterparty=nick,
        oid=oid,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=100_000,
        maxsize=10_000_000,
        txfee=1_000,
        cjfee="0.0003",
        fidelity_bond_value=bond,
        features=features if features is not None else {},
    )


def _make_server() -> OrderbookServer:
    settings = OrderbookWatcherSettings()
    aggregator = MagicMock(spec=OrderbookAggregator)
    aggregator.directory_nodes = []
    aggregator.node_statuses = {}
    aggregator.clients = {}
    aggregator.mempool_api_url = "http://dummy.api"
    return OrderbookServer(settings, aggregator)


def test_feature_stats_excludes_bondless_makers() -> None:
    """Bondless makers must not contribute to feature counts or denominator."""
    server = _make_server()
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[
            _make_offer("J5bonded1", bond=10**14, features={"ping": True}),
            _make_offer("J5bonded2", bond=10**14, features={"ping": True}),
            # Bondless makers — must be ignored even though they "support" ping.
            _make_offer("J5sybil1", bond=0, features={"ping": True}),
            _make_offer("J5sybil2", bond=0, features={"ping": True}),
            _make_offer("J5sybil3", bond=0, features={"ping": True}),
        ],
    )

    result = server._format_orderbook(orderbook)

    assert result["feature_stats"] == {"ping": 2}
    assert result["feature_stats_denominator"] == 2


def test_feature_stats_legacy_only_for_bonded_makers() -> None:
    """The synthetic ``legacy`` bucket must also be bonded-only."""
    server = _make_server()
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[
            _make_offer("J5bondedLegacy", bond=10**14, features={}),
            _make_offer("J5bondedFeat", bond=10**14, features={"ping": True}),
            # Bondless legacy maker: must not bump the legacy count.
            _make_offer("J5sybilLegacy", bond=0, features={}),
        ],
    )

    result = server._format_orderbook(orderbook)

    assert result["feature_stats"] == {"legacy": 1, "ping": 1}
    assert result["feature_stats_denominator"] == 2


def test_feature_stats_denominator_zero_with_no_bonded_makers() -> None:
    """No bonded makers => empty stats, zero denominator (avoid div-by-zero on UI)."""
    server = _make_server()
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[
            _make_offer("J5sybil1", bond=0, features={"ping": True}),
            _make_offer("J5sybil2", bond=0, features={}),
        ],
    )

    result = server._format_orderbook(orderbook)

    assert result["feature_stats"] == {}
    assert result["feature_stats_denominator"] == 0


def test_feature_stats_counts_each_maker_once() -> None:
    """A bonded maker with multiple offers must be counted once in the denominator."""
    server = _make_server()
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[
            _make_offer("J5bonded", bond=10**14, features={"ping": True}, oid=0),
            _make_offer("J5bonded", bond=10**14, features={"ping": True}, oid=1),
            _make_offer("J5bonded", bond=10**14, features={"ping": True}, oid=2),
        ],
    )

    result = server._format_orderbook(orderbook)

    assert result["feature_stats"] == {"ping": 1}


def test_feature_stats_counts_capability_flags() -> None:
    """The three deployable-fix capability flags are surfaced and counted per role."""
    server = _make_server()
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[
            _make_offer(
                "J5swap",
                bond=10**14,
                features={"swap_provider": True, "whole_coin": True},
            ),
            _make_offer("J5chan", bond=10**14, features={"channel_change": True}),
            _make_offer("J5plain", bond=10**14, features={"ping": True}),
        ],
    )

    result = server._format_orderbook(orderbook)

    stats = result["feature_stats"]
    assert stats["swap_provider"] == 1
    assert stats["whole_coin"] == 1
    assert stats["channel_change"] == 1
    assert result["feature_stats_denominator"] == 3
    # The roles ride through to the per-offer payload so a taker can pick by role.
    swap = next(o for o in result["offers"] if o["counterparty"] == "J5swap")
    assert swap["features"]["swap_provider"] is True
