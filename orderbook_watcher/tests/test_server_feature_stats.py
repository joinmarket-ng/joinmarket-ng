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
    bond_data: dict[str, object] | None = None,
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
        fidelity_bond_data=bond_data,
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
    assert result["feature_stats_denominator"] == 1


def test_feature_stats_counts_advertised_bond_without_value() -> None:
    """Makers that advertise a bond count as bonded even when the value is 0.

    Without a blockchain/mempool backend the bond value cannot be computed
    (``fidelity_bond_value == 0``), but the advertised bond is still
    sybil-resistant and must feed feature stats. See issue #508 follow-up.
    """
    server = _make_server()
    bond_data = {"utxo_txid": "ab" * 32, "utxo_vout": 0}
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[
            _make_offer("J5advertised", bond=0, features={"ping": True}, bond_data=bond_data),
            # Truly bondless maker (no value, no advertised bond) stays excluded.
            _make_offer("J5sybil", bond=0, features={"ping": True}),
        ],
    )

    result = server._format_orderbook(orderbook)

    assert result["feature_stats"] == {"ping": 1}
    assert result["feature_stats_denominator"] == 1
