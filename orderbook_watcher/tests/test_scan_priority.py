"""
Tests for maker scan prioritization in OrderbookAggregator.

The orderbook watcher should scan bonded makers first (descending bond value),
then bondless makers in ascending fee order.  This prevents sybil attacks from
consuming all scan slots before legitimate makers are checked.
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.directory_client import DirectoryClient, OfferWithTimestamp
from jmcore.models import Offer, OfferType
from jmcore.protocol import FEATURE_NEUTRINO_COMPAT, MessageType

from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.health_checker import MakerHealthChecker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_offer(
    nick: str,
    *,
    oid: int = 0,
    ordertype: str = "sw0reloffer",
    cjfee: str | int = "0.003",
    minsize: int = 100_000,
    maxsize: int = 10_000_000,
    bond_value: int = 0,
) -> Offer:
    return Offer(
        counterparty=nick,
        oid=oid,
        ordertype=OfferType(ordertype),
        minsize=minsize,
        maxsize=maxsize,
        txfee=0,
        cjfee=cjfee,
        fidelity_bond_value=bond_value,
    )


def _build_aggregator_with_offers(
    offers: list[tuple[str, str, Offer]],
) -> OrderbookAggregator:
    """Build an aggregator with a fake client containing the given offers.

    Each element of *offers* is ``(nick, location, Offer)`` .
    """
    agg = OrderbookAggregator(
        directory_nodes=[],
        network="regtest",
        mempool_api_url="",
        socks_host="127.0.0.1",
        socks_port=9050,
    )
    # Create a mock client with offers and _active_peers
    client = MagicMock(spec=DirectoryClient)
    client.offers = {}
    client._active_peers = {}
    client.peer_features = {}
    client._peerlist_supported = False

    for nick, location, offer in offers:
        key = (nick, offer.oid)
        client.offers[key] = OfferWithTimestamp(
            offer=offer,
            received_at=time.time(),
            bond_utxo_key=None,
        )
        client._active_peers[nick] = location

    agg.clients = {"node1:5222": client}
    return agg


# ---------------------------------------------------------------------------
# _prioritize_makers_for_scan tests
# ---------------------------------------------------------------------------


class TestPrioritizeMakersForScan:
    """Tests for OrderbookAggregator._prioritize_makers_for_scan()."""

    def test_bonded_before_bondless(self) -> None:
        """Bonded makers should always come before bondless ones."""
        offers = [
            ("bondless1", "a.onion:5222", _make_offer("bondless1", bond_value=0, cjfee="0.001")),
            ("bonded1", "b.onion:5222", _make_offer("bonded1", bond_value=100_000)),
        ]
        agg = _build_aggregator_with_offers(offers)

        result = agg._prioritize_makers_for_scan(
            [("bondless1", "a.onion:5222"), ("bonded1", "b.onion:5222")]
        )

        assert result[0][0] == "bonded1"
        assert result[1][0] == "bondless1"

    def test_bonded_sorted_descending_by_value(self) -> None:
        """Among bonded makers, highest bond value should come first."""
        offers = [
            ("low_bond", "a.onion:5222", _make_offer("low_bond", bond_value=1_000)),
            ("mid_bond", "b.onion:5222", _make_offer("mid_bond", bond_value=50_000)),
            ("high_bond", "c.onion:5222", _make_offer("high_bond", bond_value=1_000_000)),
        ]
        agg = _build_aggregator_with_offers(offers)

        result = agg._prioritize_makers_for_scan(
            [
                ("low_bond", "a.onion:5222"),
                ("mid_bond", "b.onion:5222"),
                ("high_bond", "c.onion:5222"),
            ]
        )

        assert [nick for nick, _ in result] == ["high_bond", "mid_bond", "low_bond"]

    def test_bondless_sorted_ascending_by_fee(self) -> None:
        """Among bondless makers, lowest fee should come first."""
        offers = [
            ("high_fee", "a.onion:5222", _make_offer("high_fee", cjfee="0.01")),
            ("low_fee", "b.onion:5222", _make_offer("low_fee", cjfee="0.0001")),
            ("mid_fee", "c.onion:5222", _make_offer("mid_fee", cjfee="0.003")),
        ]
        agg = _build_aggregator_with_offers(offers)

        result = agg._prioritize_makers_for_scan(
            [("high_fee", "a.onion:5222"), ("low_fee", "b.onion:5222"), ("mid_fee", "c.onion:5222")]
        )

        assert [nick for nick, _ in result] == ["low_fee", "mid_fee", "high_fee"]

    def test_absolute_fee_offers_sorted_by_normalised_rate(self) -> None:
        """Absolute fee offers should be sorted by fee/maxsize ratio."""
        offers = [
            (
                "abs_high",
                "a.onion:5222",
                _make_offer(
                    "abs_high",
                    ordertype="sw0absoffer",
                    cjfee=5000,
                    minsize=100_000,
                    maxsize=200_000_000,
                ),
            ),
            (
                "abs_low",
                "b.onion:5222",
                _make_offer(
                    "abs_low",
                    ordertype="sw0absoffer",
                    cjfee=100,
                    minsize=100_000,
                    maxsize=100_000,
                ),
            ),
        ]
        agg = _build_aggregator_with_offers(offers)

        result = agg._prioritize_makers_for_scan(
            [("abs_high", "a.onion:5222"), ("abs_low", "b.onion:5222")]
        )

        assert result[0][0] == "abs_high"
        assert result[1][0] == "abs_low"

    def test_mixed_bonded_and_bondless(self) -> None:
        """Full mixed scenario: bonded first by value desc, bondless after by fee asc."""
        offers = [
            ("sybil1", "s1.onion:5222", _make_offer("sybil1", cjfee="0.03", minsize=27300)),
            ("sybil2", "s2.onion:5222", _make_offer("sybil2", cjfee="0.035", minsize=27300)),
            ("legit_cheap", "l1.onion:5222", _make_offer("legit_cheap", cjfee="0.0001")),
            ("bonded_big", "b1.onion:5222", _make_offer("bonded_big", bond_value=50_000_000)),
            ("bonded_small", "b2.onion:5222", _make_offer("bonded_small", bond_value=1_000)),
        ]
        agg = _build_aggregator_with_offers(offers)

        input_makers = [(nick, loc) for nick, loc, _ in offers]
        result = agg._prioritize_makers_for_scan(input_makers)

        nicks = [nick for nick, _ in result]
        # Bonded first (desc bond value)
        assert nicks[0] == "bonded_big"
        assert nicks[1] == "bonded_small"
        # Then bondless (asc fee)
        assert nicks[2] == "legit_cheap"
        assert nicks[3] == "sybil1"
        assert nicks[4] == "sybil2"

    def test_empty_list(self) -> None:
        """Empty input should return empty list."""
        agg = _build_aggregator_with_offers([])
        assert agg._prioritize_makers_for_scan([]) == []

    def test_unknown_nick_sorted_last(self) -> None:
        """Nicks not in any client's offers get inf fee / 0 bond, sorted last."""
        offers = [
            ("known", "a.onion:5222", _make_offer("known", cjfee="0.001")),
        ]
        agg = _build_aggregator_with_offers(offers)

        result = agg._prioritize_makers_for_scan(
            [("unknown", "x.onion:5222"), ("known", "a.onion:5222")]
        )

        assert result[0][0] == "known"
        assert result[1][0] == "unknown"

    def test_multiple_offers_per_nick_uses_best(self) -> None:
        """When a nick has multiple offers, use highest bond and lowest fee."""
        agg = OrderbookAggregator(
            directory_nodes=[],
            network="regtest",
            mempool_api_url="",
        )
        client = MagicMock(spec=DirectoryClient)
        client.offers = {}
        client._active_peers = {"multi": "m.onion:5222"}
        client.peer_features = {}

        # Two offers: one with bond, one with lower fee
        offer1 = _make_offer("multi", oid=0, bond_value=100_000, cjfee="0.01")
        offer2 = _make_offer("multi", oid=1, bond_value=0, cjfee="0.0001")
        client.offers[("multi", 0)] = OfferWithTimestamp(offer=offer1, received_at=time.time())
        client.offers[("multi", 1)] = OfferWithTimestamp(offer=offer2, received_at=time.time())

        agg.clients = {"node1:5222": client}

        # Also add a bondless maker with higher fee
        client2 = MagicMock(spec=DirectoryClient)
        client2.offers = {
            ("expensive", 0): OfferWithTimestamp(
                offer=_make_offer("expensive", cjfee="0.05"),
                received_at=time.time(),
            )
        }
        client2._active_peers = {"expensive": "e.onion:5222"}
        client2.peer_features = {}
        agg.clients["node2:5222"] = client2

        result = agg._prioritize_makers_for_scan(
            [("expensive", "e.onion:5222"), ("multi", "m.onion:5222")]
        )

        # multi has bond_value=100_000 so it's bonded -> comes first
        assert result[0][0] == "multi"
        assert result[1][0] == "expensive"


# ---------------------------------------------------------------------------
# check_makers_batch chunked ordering tests
# ---------------------------------------------------------------------------


class TestCheckMakersBatchPriority:
    """Verify that check_makers_batch processes makers in priority order (chunked)."""

    @pytest.mark.asyncio
    async def test_chunked_processing_order(self) -> None:
        """Makers should be processed chunk by chunk in the order provided."""
        checker = MakerHealthChecker(
            network="regtest",
            timeout=5.0,
            max_concurrent_checks=2,  # small chunks
        )

        processing_order: list[str] = []

        async def mock_check(
            nick: str,
            location: str,  # noqa: ARG001
            force: bool = False,  # noqa: ARG001
        ) -> MagicMock:
            processing_order.append(nick)
            # Small delay to simulate network I/O
            await asyncio.sleep(0.01)
            status = MagicMock()
            status.reachable = True
            status.features = MagicMock()
            status.features.features = set()
            return status

        checker.check_maker = mock_check  # type: ignore[assignment]

        makers = [
            ("first", "a.onion:5222"),
            ("second", "b.onion:5222"),
            ("third", "c.onion:5222"),
            ("fourth", "d.onion:5222"),
            ("fifth", "e.onion:5222"),
        ]

        await checker.check_makers_batch(makers, force=True)

        # All makers should be processed
        assert set(processing_order) == {"first", "second", "third", "fourth", "fifth"}

        # First chunk (first, second) should be processed before third+
        # Since gather within a chunk is concurrent, we check chunk boundaries:
        first_chunk = processing_order[:2]
        assert set(first_chunk) == {"first", "second"}

        second_chunk = processing_order[2:4]
        assert set(second_chunk) == {"third", "fourth"}

        third_chunk = processing_order[4:]
        assert set(third_chunk) == {"fifth"}

    @pytest.mark.asyncio
    async def test_exception_in_chunk_does_not_block_next(self) -> None:
        """Exceptions within a chunk should not prevent subsequent chunks."""
        checker = MakerHealthChecker(
            network="regtest",
            timeout=5.0,
            max_concurrent_checks=2,
        )

        call_count = 0

        async def mock_check(
            nick: str,
            location: str,  # noqa: ARG001
            force: bool = False,  # noqa: ARG001
        ) -> MagicMock:
            nonlocal call_count
            call_count += 1
            if nick == "fail":
                raise RuntimeError("simulated failure")
            status = MagicMock()
            status.reachable = True
            return status

        checker.check_maker = mock_check  # type: ignore[assignment]

        makers = [
            ("fail", "a.onion:5222"),
            ("ok1", "b.onion:5222"),
            ("ok2", "c.onion:5222"),
        ]

        results = await checker.check_makers_batch(makers, force=True)

        # All three should have results
        assert len(results) == 3
        assert not results["a.onion:5222"].reachable
        assert results["b.onion:5222"].reachable
        assert results["c.onion:5222"].reachable

    @pytest.mark.asyncio
    async def test_single_maker_batch(self) -> None:
        """Single maker should work fine with chunked processing."""
        checker = MakerHealthChecker(
            network="regtest",
            timeout=5.0,
            max_concurrent_checks=5,
        )

        def create_peer_handshake_response() -> bytes:
            response_data = {
                "app-name": "joinmarket",
                "directory": False,
                "location-string": "test.onion:5222",
                "proto-ver": 5,
                "features": {FEATURE_NEUTRINO_COMPAT: True},
                "nick": "J5maker",
                "network": "regtest",
            }
            response = {"type": MessageType.HANDSHAKE.value, "line": json.dumps(response_data)}
            return json.dumps(response).encode("utf-8")

        mock_conn = MagicMock()
        mock_conn.send = AsyncMock()
        mock_conn.receive = AsyncMock(return_value=create_peer_handshake_response())
        mock_conn.close = AsyncMock()

        with patch("orderbook_watcher.health_checker.connect_via_tor", return_value=mock_conn):
            results = await checker.check_makers_batch(
                [("J5maker", "testaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222")],
                force=True,
            )

        assert len(results) == 1
        loc = "testaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"
        assert results[loc].reachable

    @pytest.mark.asyncio
    async def test_empty_batch(self) -> None:
        """Empty maker list should return empty dict."""
        checker = MakerHealthChecker(network="regtest", timeout=5.0)
        results = await checker.check_makers_batch([], force=True)
        assert results == {}


# ---------------------------------------------------------------------------
# Integration: _check_makers_without_features uses priority
# ---------------------------------------------------------------------------


class TestCheckMakersWithoutFeaturesPriority:
    """Verify _check_makers_without_features passes sorted list to batch."""

    @pytest.mark.asyncio
    async def test_bonded_makers_checked_before_sybil(self) -> None:
        """Bonded makers must be scanned before high-fee bondless sybil offers."""
        # Setup: 1 bonded maker + 2 sybil-like makers
        offers = [
            ("sybil1", "s1.onion:5222", _make_offer("sybil1", cjfee="0.035", minsize=27300)),
            ("sybil2", "s2.onion:5222", _make_offer("sybil2", cjfee="0.030", minsize=27300)),
            ("bonded", "b.onion:5222", _make_offer("bonded", bond_value=50_000_000, cjfee="0.001")),
        ]
        agg = _build_aggregator_with_offers(offers)

        checked_order: list[str] = []

        async def mock_batch(
            makers: list[tuple[str, str]],
            force: bool = False,  # noqa: ARG001
        ) -> dict:
            for nick, _loc in makers:
                checked_order.append(nick)
            return {}

        agg.health_checker.check_makers_batch = mock_batch  # type: ignore[assignment]

        await agg._check_makers_without_features()

        assert checked_order[0] == "bonded"
        # sybil2 has lower fee than sybil1
        assert checked_order.index("sybil2") < checked_order.index("sybil1")
