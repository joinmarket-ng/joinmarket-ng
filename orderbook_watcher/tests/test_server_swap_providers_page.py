"""Tests for swap providers static page route."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from jmcore.settings import OrderbookWatcherSettings

from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.server import OrderbookServer


@pytest.mark.asyncio
async def test_swap_providers_page_handler_serves_static_file() -> None:
    settings = OrderbookWatcherSettings()
    aggregator = MagicMock(spec=OrderbookAggregator)
    aggregator.directory_nodes = []
    aggregator.node_statuses = {}
    aggregator.clients = {}

    server = OrderbookServer(settings, aggregator)

    response = await server._handle_swap_providers(MagicMock())  # type: ignore[attr-defined]

    assert isinstance(response, web.FileResponse)
    assert response._path.name == "swap_providers.html"
