"""
Tests for ``directly_reachable`` exposure in the ``/orderbook.json`` payload.

Issue #105: the orderbook watcher already tracks whether each maker is
directly reachable via its onion address (set on ``Offer.directly_reachable``
by ``OrderbookAggregator``). This test pins the server-side wiring so that
field is included in the JSON payload that drives the web UI's "Direct" badge.
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
    directly_reachable: bool | None = None,
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
        fidelity_bond_value=0,
        features={},
        directly_reachable=directly_reachable,
    )


def _make_server() -> OrderbookServer:
    settings = OrderbookWatcherSettings()
    aggregator = MagicMock(spec=OrderbookAggregator)
    aggregator.directory_nodes = []
    aggregator.node_statuses = {}
    aggregator.clients = {}
    aggregator.mempool_api_url = "http://dummy.api"
    return OrderbookServer(settings, aggregator)


def test_offer_payload_includes_directly_reachable_true() -> None:
    """Makers reached via direct onion connection are marked True."""
    server = _make_server()
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[_make_offer("J5direct", directly_reachable=True)],
    )

    result = server._format_orderbook(orderbook)

    assert len(result["offers"]) == 1
    assert result["offers"][0]["directly_reachable"] is True


def test_offer_payload_includes_directly_reachable_false() -> None:
    """Makers checked but unreachable are marked False (distinct from unchecked)."""
    server = _make_server()
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[_make_offer("J5offline", directly_reachable=False)],
    )

    result = server._format_orderbook(orderbook)

    assert result["offers"][0]["directly_reachable"] is False


def test_offer_payload_preserves_unchecked_as_none() -> None:
    """Unchecked makers must surface as null so the UI can hide the badge."""
    server = _make_server()
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[_make_offer("J5unknown", directly_reachable=None)],
    )

    result = server._format_orderbook(orderbook)

    assert result["offers"][0]["directly_reachable"] is None
