"""Tests for UI cache headers.

Release images normalize static file mtimes for reproducible builds, so
``Last-Modified``/``ETag`` validators never change across releases; without
``no-store`` a browser that cached ``app.js`` once keeps getting ``304``s and
serves a stale frontend forever.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from jmcore.models import OrderBook
from jmcore.settings import OrderbookWatcherSettings

import orderbook_watcher.server as server_module
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
        for path in ("/", "/static/app.js", "/static/style.css", "/static/favicon.ico"):
            resp = await client.get(path)
            assert resp.status == 200, path
            assert resp.headers.get("Cache-Control") == "no-store", path
            assert await resp.read(), path

        index_resp = await client.get("/")
        assert "<title>JoinMarket NG Orderbook Watcher</title>" in await index_resp.text()


def test_server_fails_fast_when_frontend_asset_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for asset in server_module._REQUIRED_STATIC_ASSETS:
        if asset != "favicon.ico":
            (tmp_path / asset).write_text("test", encoding="utf-8")

    monkeypatch.setattr(server_module, "_STATIC_DIR", tmp_path)

    with pytest.raises(RuntimeError, match=r"frontend assets are missing \(favicon\.ico\)"):
        _make_server()


async def test_non_ui_responses_are_not_marked_no_store() -> None:
    server = _make_server()
    async with TestClient(TestServer(server.app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        assert resp.headers.get("Cache-Control") != "no-store"


async def test_responses_carry_security_headers() -> None:
    server = _make_server()
    async with TestClient(TestServer(server.app)) as client:
        for path in ("/", "/static/app.js", "/health", "/static/missing.js"):
            resp = await client.get(path)
            assert resp.headers.get("X-Content-Type-Options") == "nosniff", path
            csp = resp.headers.get("Content-Security-Policy", "")
            assert "default-src 'self'" in csp, path
            assert "frame-ancestors 'none'" in csp, path


async def test_csp_allows_block_height_fetch_from_configured_mempool_only() -> None:
    server = _make_server()
    server.settings.mempool_web_url = "https://mempool.space/signet"
    async with TestClient(TestServer(server.app)) as client:
        resp = await client.get("/")
        csp = resp.headers["Content-Security-Policy"]
        assert "connect-src 'self' https://mempool.space;" in csp


def test_frontend_needs_no_inline_script_or_style() -> None:
    static = Path(server_module.__file__).parent / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<style|\sstyle=|\son[a-z]+=|<script(?![^>]*\ssrc=)", html)
    assert "style=" not in (static / "app.js").read_text(encoding="utf-8")
