"""Tests for orderbook watcher proxy endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app: TestClient) -> TestClient:
    return app


class TestObWatch:
    @patch("jmwalletd.routers.obwatch.aiohttp.ClientSession.get")
    def test_get_orderbook(self, mock_get: AsyncMock, client: TestClient) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"offers": [], "fidelitybonds": []}
        mock_get.return_value.__aenter__.return_value = mock_resp

        resp = client.get("/api/v1/obwatch/orderbook.json")
        assert resp.status_code == 200
        assert resp.json() == {"offers": [], "fidelitybonds": []}

    @patch("jmwalletd.routers.obwatch.aiohttp.ClientSession.get")
    def test_get_orderbook_error(self, mock_get: AsyncMock, client: TestClient) -> None:
        mock_get.side_effect = Exception("Connection error")

        resp = client.get("/api/v1/obwatch/orderbook.json")
        assert resp.status_code == 502
        assert resp.json() == {"offers": [], "fidelitybonds": []}

    @patch("jmwalletd.routers.obwatch.aiohttp.ClientSession.get")
    def test_refresh_orderbook(self, mock_get: AsyncMock, client: TestClient) -> None:
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"offers": []}
        mock_get.return_value.__aenter__.return_value = mock_resp

        resp = client.get("/api/v1/obwatch/refreshorderbook")
        assert resp.status_code == 200

    @patch("jmwalletd.routers.obwatch.aiohttp.ClientSession.get")
    def test_refresh_orderbook_post(self, mock_get: AsyncMock, client: TestClient) -> None:
        """JAM calls POST /obwatch/refreshorderbook; ensure it is accepted."""
        mock_resp = AsyncMock()
        mock_resp.status = 200
        mock_resp.json.return_value = {"offers": [], "fidelitybonds": []}
        mock_get.return_value.__aenter__.return_value = mock_resp

        resp = client.post("/api/v1/obwatch/refreshorderbook")
        assert resp.status_code == 200
