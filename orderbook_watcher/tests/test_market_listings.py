"""Credential advertisements remain separate from CoinJoin orderbook state."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from bitcointx.core.key import CKey
from jmcore.credential_market import MarketListing, Network, Product, canonical, sign_document
from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer, OfferType, OrderBook
from jmcore.network import ONION_HOSTID
from jmcore.protocol import MessageType
from jmcore.settings import OrderbookWatcherSettings

from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.market import MAX_MARKET_OFFERS, MarketListingCache
from orderbook_watcher.server import OrderbookServer


def listing_raw(
    *,
    products: list[Product] | None = None,
    expires_at: int = 160,
    network: Network = "regtest",
    price_sats: int = 1000,
) -> bytes:
    key = CKey(b"\x17" * 32)
    listing = MarketListing(
        network=network,
        period=0,
        seller_pubkey=bytes(key.pub).hex(),
        encryption_pubkey="18" * 32,
        products=products if products is not None else ["podle", "bond"],
        price_sats=price_sats,
        expires_at=expires_at,
    )
    return canonical(sign_document(listing, key))


def test_cache_separates_products_and_merges_directory_observations() -> None:
    cache = MarketListingCache("regtest")
    raw = listing_raw()
    assert cache.observe("seller", raw, "directory-b", now=100)
    assert cache.observe("seller", raw, "directory-a", now=100)
    result = cache.snapshot(now=100)
    assert len(result["podle_offers"]) == len(result["bond_offers"]) == 1
    assert result["podle_offers"] == result["bond_offers"]
    assert result["podle_offers"][0]["directory_nodes"] == ["directory-a", "directory-b"]
    assert result["podle_offers"][0]["listing"]["body"]["price_sats"] == 1000


def test_cache_only_attributes_newest_document_to_its_actual_source() -> None:
    cache = MarketListingCache("regtest")
    old = listing_raw(products=["podle"], expires_at=150)
    new = listing_raw(products=["bond"], expires_at=160, price_sats=2000)
    assert cache.observe("seller", old, "old-directory", now=100)
    assert cache.observe("seller", new, "new-directory", now=100)
    assert not cache.observe("seller", old, "old-directory", now=100)
    result = cache.snapshot(now=100)
    assert result["podle_offers"] == []
    assert len(result["bond_offers"]) == 1
    assert result["bond_offers"][0]["directory_nodes"] == ["new-directory"]
    assert result["bond_offers"][0]["listing"]["body"]["price_sats"] == 2000


@pytest.mark.parametrize(
    "raw",
    [
        listing_raw(network="signet"),
        listing_raw(expires_at=100),
        listing_raw(expires_at=221),
        b'{"invalid":"document"}',
        listing_raw().replace(b'"price_sats":1000', b'"price_sats":1001'),
    ],
)
def test_cache_rejects_invalid_advertisement(raw: bytes) -> None:
    cache = MarketListingCache("regtest")
    assert not cache.observe("seller", raw, "directory", now=100)
    assert cache.snapshot(now=100) == {"podle_offers": [], "bond_offers": []}


def test_cache_expires_without_new_messages_and_is_bounded() -> None:
    cache = MarketListingCache("regtest")
    raw = listing_raw(products=["podle", "podle"])
    for index in range(MAX_MARKET_OFFERS + 1):
        assert cache.observe(f"seller-{index}", raw, "directory", now=100)
    offers = cache.snapshot(now=159)["podle_offers"]
    assert len(offers) == MAX_MARKET_OFFERS
    assert all(offer["seller_nick"] != "seller-0" for offer in offers)
    assert cache.snapshot(now=160) == {"podle_offers": [], "bond_offers": []}


async def test_directory_advertisement_reaches_separate_http_collections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("orderbook_watcher.aggregator.time.time", lambda: 100)
    aggregator = OrderbookAggregator([], "regtest", mempool_api_url="")
    seller = NickIdentity(private_key_bytes=b"\x19" * 32)
    directory = DirectoryClient("directory", 5222, "regtest")
    signed = seller.sign_message(base64.b64encode(listing_raw()).decode("ascii"), ONION_HOSTID)
    directory._capture_market_listing(
        {"type": MessageType.PUBMSG.value, "line": f"{seller.nick}!PUBLIC!moffer {signed}"}
    )
    aggregator.clients["directory:5222"] = directory
    orderbook = OrderBook(
        timestamp=datetime.now(UTC),
        offers=[
            Offer(
                counterparty="coinjoin-maker",
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=100_000,
                maxsize=1_000_000,
                txfee=0,
                cjfee="0.0001",
            )
        ],
    )
    monkeypatch.setattr(aggregator, "get_live_orderbook", AsyncMock(return_value=orderbook))
    server = OrderbookServer(OrderbookWatcherSettings(), aggregator)
    async with TestClient(TestServer(server.app)) as client:
        response = await client.get("/orderbook.json")
        assert response.status == 200
        data = await response.json()
    assert len(data["offers"]) == 1
    assert data["offers"][0]["counterparty"] == "coinjoin-maker"
    assert data["fidelitybonds"] == []
    assert data["credential_market"]["podle_offers"][0]["seller_nick"] == seller.nick
    assert data["credential_market"]["bond_offers"][0]["seller_nick"] == seller.nick
    assert directory.offers == {}
    assert directory.bonds == {}
    assert directory.drain_market_listings() == []


async def test_discovery_requests_survive_one_directory_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aggregator = OrderbookAggregator([], "regtest", mempool_api_url="")
    good = DirectoryClient("good", 5222, "regtest")
    bad = DirectoryClient("bad", 5222, "regtest")
    send_good = AsyncMock()
    send_bad = AsyncMock(side_effect=ConnectionError("disconnected"))
    monkeypatch.setattr(good, "send_public_message", send_good)
    monkeypatch.setattr(bad, "send_public_message", send_bad)
    aggregator.clients = {"good": good, "bad": bad}
    await aggregator._request_market_listings()
    send_good.assert_awaited_once_with("mbook")
    send_bad.assert_awaited_once_with("mbook")


async def test_discovery_loop_stops_with_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    aggregator = OrderbookAggregator([], "regtest", mempool_api_url="")
    requested = asyncio.Event()

    async def request() -> None:
        requested.set()

    monkeypatch.setattr(aggregator, "_request_market_listings", request)
    task = asyncio.create_task(aggregator._periodic_market_discovery())
    aggregator.listener_tasks.append(task)
    await asyncio.wait_for(requested.wait(), timeout=1)
    await aggregator.stop_listening()
    assert task.cancelled()
    assert aggregator.listener_tasks == []
