"""Credential advertisements expire from the HTTP cache without a refresh."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from jmcore.models import OrderBook
from jmcore.settings import OrderbookWatcherSettings

import orderbook_watcher.server as server_module
from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.server import OrderbookServer


def _base_payload(name: str = "cached") -> dict[str, Any]:
    return {
        "timestamp": "2026-09-07T00:00:00+00:00",
        "current_block_height": 123,
        "offers": [{"counterparty": name, "oid": 0}],
        "fidelitybonds": [{"counterparty": "bonded-maker", "amount": 1}],
        "directory_nodes": ["directory:5222"],
        "directory_stats": {"directory:5222": {"offer_count": 1}},
        "feature_stats": {"legacy": 1},
        "feature_stats_denominator": 1,
        "fee_quantization": {"rel_grid": ["0.0001"], "abs_grid": [100]},
        "mempool_url": None,
    }


def _credential_offer(seller: str, expires_at: int) -> dict[str, Any]:
    return {
        "seller_nick": seller,
        "listing": {"body": {"expires_at": expires_at, "price_sats": 1_000}},
        "directory_nodes": ["directory:5222"],
    }


def _payload(
    *,
    name: str = "cached",
    podle_offers: list[dict[str, Any]] | None = None,
    bond_offers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _base_payload(name) | {
        "credential_market": {
            "podle_offers": podle_offers if podle_offers is not None else [],
            "bond_offers": bond_offers if bond_offers is not None else [],
        }
    }


def _orderbook() -> OrderBook:
    return OrderBook(timestamp=datetime(2026, 9, 7, tzinfo=UTC))


def _make_server(payloads: list[dict[str, Any]]) -> tuple[OrderbookServer, MagicMock]:
    aggregator = MagicMock(spec=OrderbookAggregator)
    aggregator.directory_nodes = []
    aggregator.node_statuses = {}
    aggregator.clients = {}
    aggregator.get_live_orderbook = AsyncMock(side_effect=[_orderbook() for _ in payloads])
    server = OrderbookServer(OrderbookWatcherSettings(), aggregator)
    server._format_orderbook = MagicMock(side_effect=payloads)
    return server, aggregator


def _assert_base_payload(data: dict[str, Any], name: str) -> None:
    assert {key: data[key] for key in _base_payload(name)} == _base_payload(name)


async def test_initial_cache_filters_expired_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100]
    monkeypatch.setattr(server_module.time, "time", lambda: now[0])
    server, aggregator = _make_server(
        [
            _payload(
                podle_offers=[_credential_offer("expired", 100)],
                bond_offers=[_credential_offer("live", 150)],
            )
        ]
    )

    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/orderbook.json")
        assert response.status == 200
        data = await response.json()

    assert data["credential_market"]["podle_offers"] == []
    assert data["credential_market"]["bond_offers"] == [_credential_offer("live", 150)]
    assert server._cached_credential_expiry == 150
    aggregator.get_live_orderbook.assert_awaited_once()
    _assert_base_payload(data, "cached")


async def test_cache_prunes_at_expiry_without_reading_aggregator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [159]
    monkeypatch.setattr(server_module.time, "time", lambda: now[0])
    server, aggregator = _make_server(
        [
            _payload(
                podle_offers=[_credential_offer("podle", 160)],
                bond_offers=[_credential_offer("bond", 180)],
            )
        ]
    )

    async with TestClient(TestServer(server.app)) as client:
        before_expiry = await (await client.get("/orderbook.json")).json()
        now[0] = 160
        at_first_expiry = await (await client.get("/orderbook.json")).json()
        now[0] = 180
        at_last_expiry = await (await client.get("/orderbook.json")).json()

    assert before_expiry["credential_market"] == {
        "podle_offers": [_credential_offer("podle", 160)],
        "bond_offers": [_credential_offer("bond", 180)],
    }
    assert at_first_expiry["credential_market"] == {
        "podle_offers": [],
        "bond_offers": [_credential_offer("bond", 180)],
    }
    assert at_last_expiry["credential_market"] == {"podle_offers": [], "bond_offers": []}
    assert server._cached_credential_expiry is None
    aggregator.get_live_orderbook.assert_awaited_once()
    _assert_base_payload(at_first_expiry, "cached")
    _assert_base_payload(at_last_expiry, "cached")


async def test_expiry_pruning_uses_serialized_noncredential_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100]
    monkeypatch.setattr(server_module.time, "time", lambda: now[0])
    payload = _payload(podle_offers=[_credential_offer("expires", 110)])
    server, aggregator = _make_server([payload])

    async with TestClient(TestServer(server.app)) as client:
        initial = await (await client.get("/orderbook.json")).json()
        noncredential_snapshot = {
            key: value for key, value in initial.items() if key != "credential_market"
        }
        payload["offers"][0]["counterparty"] = "mutated-maker"
        payload["fidelitybonds"][0]["amount"] = 999
        payload["directory_stats"]["directory:5222"]["offer_count"] = 999

        now[0] = 110
        expired = await (await client.get("/orderbook.json")).json()

    assert expired["credential_market"] == {"podle_offers": [], "bond_offers": []}
    assert {key: value for key, value in expired.items() if key != "credential_market"} == (
        noncredential_snapshot
    )
    aggregator.get_live_orderbook.assert_awaited_once()


async def test_cache_without_credentials_does_not_refresh_or_reserialize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100]
    monkeypatch.setattr(server_module.time, "time", lambda: now[0])
    server, aggregator = _make_server([_payload()])

    async with TestClient(TestServer(server.app)) as client:
        first = await (await client.get("/orderbook.json")).json()
        cached_response = server._cached_orderbook
        now[0] = 10_000
        second = await (await client.get("/orderbook.json")).json()

    assert first == second
    assert server._cached_orderbook is cached_response
    assert server._cached_credential_expiry is None
    aggregator.get_live_orderbook.assert_awaited_once()
    _assert_base_payload(second, "cached")


async def test_background_replacement_updates_credential_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100]
    monkeypatch.setattr(server_module.time, "time", lambda: now[0])
    server, aggregator = _make_server(
        [
            _payload(name="initial", podle_offers=[_credential_offer("initial", 200)]),
            _payload(name="replacement", bond_offers=[_credential_offer("replacement", 120)]),
        ]
    )

    async with TestClient(TestServer(server.app)) as client:
        await client.get("/orderbook.json")

        async def stop_after_refresh(delay: float) -> None:
            if delay == 30:
                raise asyncio.CancelledError

        with monkeypatch.context() as context:
            context.setattr(server_module.asyncio, "sleep", stop_after_refresh)
            with pytest.raises(asyncio.CancelledError):
                await server._update_cache_loop()

        assert server._cached_credential_expiry == 120
        now[0] = 120
        data = await (await client.get("/orderbook.json")).json()

    assert data["credential_market"] == {"podle_offers": [], "bond_offers": []}
    assert aggregator.get_live_orderbook.await_count == 2
    _assert_base_payload(data, "replacement")


async def test_background_failure_leaves_base_cache_and_http_prunes_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100]
    monkeypatch.setattr(server_module.time, "time", lambda: now[0])
    server, aggregator = _make_server(
        [_payload(podle_offers=[_credential_offer("expired-after-failure", 110)])]
    )
    aggregator.get_live_orderbook.side_effect = [
        _orderbook(),
        ConnectionError("directory unavailable"),
    ]

    async with TestClient(TestServer(server.app)) as client:
        await client.get("/orderbook.json")

        async def stop_after_failure(delay: float) -> None:
            if delay == 30:
                raise asyncio.CancelledError

        with monkeypatch.context() as context:
            context.setattr(server_module.asyncio, "sleep", stop_after_failure)
            with pytest.raises(asyncio.CancelledError):
                await server._update_cache_loop()

        now[0] = 110
        data = await (await client.get("/orderbook.json")).json()

    assert data["credential_market"] == {"podle_offers": [], "bond_offers": []}
    assert aggregator.get_live_orderbook.await_count == 2
    _assert_base_payload(data, "cached")
