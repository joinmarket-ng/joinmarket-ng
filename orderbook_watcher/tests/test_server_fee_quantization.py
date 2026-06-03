"""Tests for the ``fee_quantization`` grid exposed in the orderbook payload (issue #508)."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

from jmcore.fee_quantization import QUANT_ABS, QUANT_REL
from jmcore.models import Offer, OfferType, OrderBook
from jmcore.settings import OrderbookWatcherSettings

from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.server import OrderbookServer


def _make_server() -> OrderbookServer:
    settings = OrderbookWatcherSettings()
    aggregator = MagicMock(spec=OrderbookAggregator)
    aggregator.directory_nodes = []
    aggregator.node_statuses = {}
    aggregator.clients = {}
    aggregator.mempool_api_url = "http://dummy.api"
    return OrderbookServer(settings, aggregator)


def test_payload_exposes_fee_quantization_grid() -> None:
    server = _make_server()
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[
            Offer(
                counterparty="J5maker1",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=10_000_000,
                txfee=1_000,
                cjfee="0.0003",
                fidelity_bond_value=10**14,
            )
        ],
    )

    result = server._format_orderbook(orderbook)

    assert "fee_quantization" in result
    grid = result["fee_quantization"]
    # The grid mirrors the shared jmcore grid so the chart stays in sync.
    assert grid["rel_grid"] == [str(q) for q in QUANT_REL]
    assert grid["abs_grid"] == list(QUANT_ABS)
    # JSON-serializable primitives only.
    assert all(isinstance(v, str) for v in grid["rel_grid"])
    assert all(isinstance(v, int) for v in grid["abs_grid"])
