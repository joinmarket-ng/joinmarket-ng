"""
Tests for the per-directory peerlist cleanup behaviour in
OrderbookAggregator._periodic_peerlist_refresh().

The watcher trusts each directory's view: an offer announced through
directory D is dropped from that directory's cache as soon as D no longer
lists the maker in its peerlist (whether via an explicit ;D broadcast or
a refresh that omits the nick). Cleanup is purely per-directory; offers
held by other directories are unaffected.

Reference implementation directories (no GETPEERLIST support) fall back
to age-based pruning via cleanup_stale_offers().
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer, OfferType
from jmcore.protocol import MessageType

import orderbook_watcher.aggregator as agg_mod
from orderbook_watcher.aggregator import OrderbookAggregator


def _make_offer(nick: str, oid: int = 0) -> Offer:
    return Offer(
        counterparty=nick,
        oid=oid,
        ordertype=OfferType("sw0reloffer"),
        minsize=100_000,
        maxsize=10_000_000,
        txfee=0,
        cjfee="0.001",
        fidelity_bond_value=0,
    )


def _make_client(
    *,
    nicks_with_offers: list[str],
    peerlist_chunks: list[str] | None,
    announces_peerlist_features: bool = False,
) -> DirectoryClient:
    """Build a client whose peerlist fetch uses mock transport responses."""
    client = DirectoryClient("directory", 5222, "regtest")
    for nick in nicks_with_offers:
        client._store_offer((nick, 0), _make_offer(nick), None)

    connection = AsyncMock()
    if peerlist_chunks is None:
        connection.receive.side_effect = TimeoutError()
    else:
        connection.receive.side_effect = [
            *[
                json.dumps({"type": MessageType.PEERLIST.value, "line": chunk}).encode("utf-8")
                for chunk in peerlist_chunks
            ],
            TimeoutError(),
        ]
    client.connection = connection
    client.directory_peerlist_features = announces_peerlist_features
    client.timeout = 0.01
    client._peerlist_timeout = 0.01
    client._peerlist_chunk_timeout = 0.01

    return client


def _build_aggregator() -> OrderbookAggregator:
    return OrderbookAggregator(
        directory_nodes=[],
        network="regtest",
        mempool_api_url="",
    )


async def _run_one_refresh_iteration(agg: OrderbookAggregator) -> None:
    """Run _periodic_peerlist_refresh just long enough to perform one pass."""
    # Patch sleep so the task ticks immediately and exits via cancel after one pass.
    original_sleep = asyncio.sleep

    sleep_calls: list[float] = []

    async def fast_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        # First sleep is the 120s startup wait; second sleep is the 300s loop
        # interval. Cancel after the loop sleep to give one iteration.
        if len(sleep_calls) >= 2:
            raise asyncio.CancelledError
        await original_sleep(0)

    # Replace _check_makers_without_features with a no-op to isolate cleanup
    # behaviour from feature-discovery side effects.
    agg._check_makers_without_features = AsyncMock()  # type: ignore[method-assign]

    original = agg_mod.asyncio.sleep
    agg_mod.asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        # The task catches CancelledError internally and returns normally.
        await agg._periodic_peerlist_refresh()
        agg._check_makers_without_features.assert_not_awaited()
    finally:
        agg_mod.asyncio.sleep = original  # type: ignore[assignment]


class TestPerDirectoryPeerlistCleanup:
    @pytest.mark.asyncio
    async def test_removes_offers_for_nicks_not_in_directory_peerlist(self) -> None:
        """Offers from nicks the directory no longer reports must be dropped."""
        client = _make_client(
            nicks_with_offers=["alice", "bob", "carol"],
            peerlist_chunks=["alice;alice.onion:5222,carol;carol.onion:5222"],
        )
        remove_offers_for_nick = MagicMock(wraps=client.remove_offers_for_nick)
        client.remove_offers_for_nick = remove_offers_for_nick  # type: ignore[method-assign]
        agg = _build_aggregator()
        agg.clients = {"node1:5222": client}

        await _run_one_refresh_iteration(agg)

        remove_offers_for_nick.assert_called_once_with("bob")
        assert ("bob", 0) not in client.offers
        assert ("alice", 0) in client.offers
        assert ("carol", 0) in client.offers

    @pytest.mark.asyncio
    async def test_per_directory_isolation(self) -> None:
        """A nick missing from one directory must NOT be removed from another."""
        # Same maker "shared" on two directories. On node1 it disconnected;
        # node2 still lists it. node1 should drop it, node2 should keep it.
        node1 = _make_client(
            nicks_with_offers=["shared", "alice"],
            peerlist_chunks=["alice;alice.onion:5222"],
        )
        node2 = _make_client(
            nicks_with_offers=["shared", "alice"],
            peerlist_chunks=["shared;shared.onion:5222,alice;alice.onion:5222"],
        )
        remove_from_node1 = MagicMock(wraps=node1.remove_offers_for_nick)
        node1.remove_offers_for_nick = remove_from_node1  # type: ignore[method-assign]
        remove_from_node2 = MagicMock(wraps=node2.remove_offers_for_nick)
        node2.remove_offers_for_nick = remove_from_node2  # type: ignore[method-assign]
        agg = _build_aggregator()
        agg.clients = {"node1:5222": node1, "node2:5222": node2}

        await _run_one_refresh_iteration(agg)

        remove_from_node1.assert_called_once_with("shared")
        assert ("shared", 0) not in node1.offers
        assert ("shared", 0) in node2.offers
        # node2 had nothing to remove
        remove_from_node2.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_failure_skips_cleanup_for_that_directory(self) -> None:
        """If GETPEERLIST fails for a directory we keep its current state."""
        client = _make_client(
            nicks_with_offers=["alice", "bob"],
            peerlist_chunks=None,
        )
        client.get_authoritative_peerlist_snapshot = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("boom")
        )
        remove_offers_for_nick = MagicMock(wraps=client.remove_offers_for_nick)
        client.remove_offers_for_nick = remove_offers_for_nick  # type: ignore[method-assign]
        agg = _build_aggregator()
        agg.clients = {"node1:5222": client}

        await _run_one_refresh_iteration(agg)

        # Refresh failed -> we don't trust the active list -> no removals.
        remove_offers_for_nick.assert_not_called()
        assert ("alice", 0) in client.offers
        assert ("bob", 0) in client.offers

    @pytest.mark.asyncio
    async def test_rate_limited_refresh_skips_cleanup_for_that_directory(self) -> None:
        """A rate-limited fetch has no authoritative snapshot and keeps offers."""
        client = _make_client(
            nicks_with_offers=["alice", "bob"],
            peerlist_chunks=None,
            announces_peerlist_features=True,
        )
        client._last_peerlist_request_time = time.time()
        remove_offers_for_nick = MagicMock(wraps=client.remove_offers_for_nick)
        client.remove_offers_for_nick = remove_offers_for_nick  # type: ignore[method-assign]
        agg = _build_aggregator()
        agg.clients = {"node1:5222": client}

        await _run_one_refresh_iteration(agg)

        remove_offers_for_nick.assert_not_called()
        client.connection.send.assert_not_awaited()
        assert ("alice", 0) in client.offers
        assert ("bob", 0) in client.offers

    @pytest.mark.asyncio
    async def test_empty_authoritative_snapshot_removes_all_directory_offers(self) -> None:
        """A completed empty peerlist is safe to use for per-directory cleanup."""
        client = _make_client(
            nicks_with_offers=["alice", "bob"],
            peerlist_chunks=[""],
        )
        agg = _build_aggregator()
        agg.clients = {"node1:5222": client}

        await _run_one_refresh_iteration(agg)

        assert client.offers == {}

    @pytest.mark.asyncio
    async def test_directory_without_getpeerlist_falls_back_to_stale_cleanup(self) -> None:
        """Reference-impl directories use age-based cleanup, not peerlist diff."""
        client = _make_client(
            nicks_with_offers=["alice", "bob"],
            peerlist_chunks=None,
        )
        remove_offers_for_nick = MagicMock(wraps=client.remove_offers_for_nick)
        client.remove_offers_for_nick = remove_offers_for_nick  # type: ignore[method-assign]
        cleanup_stale_offers = MagicMock(wraps=client.cleanup_stale_offers)
        client.cleanup_stale_offers = cleanup_stale_offers  # type: ignore[method-assign]
        agg = _build_aggregator()
        agg.clients = {"node1:5222": client}

        await _run_one_refresh_iteration(agg)

        # Must NOT call remove_offers_for_nick (we don't have a trusted list).
        remove_offers_for_nick.assert_not_called()
        # Must call cleanup_stale_offers as the fallback.
        cleanup_stale_offers.assert_called_once_with(max_age_seconds=1800.0)

    @pytest.mark.asyncio
    async def test_no_op_when_directory_state_matches(self) -> None:
        """When peerlist matches the offer cache, nothing is removed."""
        client = _make_client(
            nicks_with_offers=["alice", "bob"],
            peerlist_chunks=["alice;alice.onion:5222,bob;bob.onion:5222"],
        )
        remove_offers_for_nick = MagicMock(wraps=client.remove_offers_for_nick)
        client.remove_offers_for_nick = remove_offers_for_nick  # type: ignore[method-assign]
        agg = _build_aggregator()
        agg.clients = {"node1:5222": client}

        await _run_one_refresh_iteration(agg)

        remove_offers_for_nick.assert_not_called()
        assert ("alice", 0) in client.offers
        assert ("bob", 0) in client.offers
