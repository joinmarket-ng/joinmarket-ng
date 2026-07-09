"""Tests for UI cache headers.

Release images normalize static file mtimes for reproducible builds, so
``Last-Modified``/``ETag`` validators never change across releases; without
``no-store`` a browser that cached ``app.js`` once keeps getting ``304``s and
serves a stale frontend forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer
from jmcore.models import OrderBook
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
    aggregator.get_orderbook = AsyncMock(
        return_value=OrderBook(timestamp=datetime.now(UTC), offers=[])
    )
    return OrderbookServer(settings, aggregator)


async def test_ui_and_static_responses_are_not_cached() -> None:
    server = _make_server()
    async with TestClient(TestServer(server.app)) as client:
        for path in ("/", "/static/app.js", "/static/style.css"):
            resp = await client.get(path)
            assert resp.status == 200, path
            assert resp.headers.get("Cache-Control") == "no-store", path


async def test_non_ui_responses_are_not_marked_no_store() -> None:
    server = _make_server()
    async with TestClient(TestServer(server.app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        assert resp.headers.get("Cache-Control") != "no-store"
