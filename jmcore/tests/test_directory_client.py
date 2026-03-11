import asyncio
import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from jmcore.directory_client import DirectoryClient, MessageType
from jmcore.protocol import FEATURE_PEERLIST_FEATURES


def test_directory_client_default_timeout():
    """Test that DirectoryClient default timeout is 120s (matches Tor circuit timeout).

    The timeout covers the entire SOCKS5 connection lifecycle including
    Tor circuit building and PoW solving. Under PoW defense, Tor clients
    solve proof-of-work challenges that can take significantly longer.
    """
    client = DirectoryClient("host", 1234, "mainnet")
    assert client.timeout == 120.0


def test_directory_client_custom_timeout():
    """Test that DirectoryClient accepts custom timeout."""
    client = DirectoryClient("host", 1234, "mainnet", timeout=60.0)
    assert client.timeout == 60.0


@pytest.mark.asyncio
async def test_get_peerlist_with_features_logs_correctly():
    """Test that get_peerlist_with_features logs the correct message."""

    from loguru import logger

    # Capture logs
    logs = []
    logger.add(lambda msg: logs.append(msg))

    # Mock the connection
    mock_connection = AsyncMock()

    # Setup the response - use side_effect to return data then timeout
    response_data = {
        "type": MessageType.PEERLIST.value,
        "line": f"nick1;location1;F:{FEATURE_PEERLIST_FEATURES}",
    }
    mock_connection.receive.side_effect = [
        json.dumps(response_data).encode("utf-8"),
        TimeoutError(),  # Signal end of chunks
    ]

    # Initialize client
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client._peerlist_chunk_timeout = 0.1  # Very short for testing

    # Run the method
    peers = await client.get_peerlist_with_features()

    # Verify the log message
    log_text = "".join(str(log) for log in logs)
    assert "Sending GETPEERLIST request" in log_text
    assert "with features support" not in log_text

    # Verify the request was sent
    mock_connection.send.assert_called_once()
    sent_msg = json.loads(mock_connection.send.call_args[0][0].decode("utf-8"))
    assert sent_msg["type"] == MessageType.GETPEERLIST.value
    assert sent_msg["line"] == ""

    # Verify return value
    assert len(peers) == 1
    nick, loc, features = peers[0]
    assert nick == "nick1"
    assert loc == "location1"
    assert features.supports_peerlist_features()


@pytest.mark.asyncio
async def test_peerlist_timeout_with_announced_features_does_not_disable():
    """
    Test that when directory announced peerlist_features during handshake,
    timeout does not permanently disable peerlist requests (it may just be slow).
    """
    # Mock the connection
    mock_connection = AsyncMock()
    mock_connection.receive.side_effect = TimeoutError("simulated timeout")

    # Initialize client with directory that announced peerlist_features
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client.directory_peerlist_features = True  # Directory announced feature
    client._peerlist_timeout = 0.1  # Very short timeout for test

    # First request - should timeout but NOT disable peerlist
    peers = await client.get_peerlist_with_features()
    assert peers == []
    assert client._peerlist_supported is not False  # Should NOT be disabled
    assert client._peerlist_timeout_count == 1

    # Reset rate limit to allow another request
    client._last_peerlist_request_time = 0

    # Second request - should also timeout but still NOT disable peerlist
    peers = await client.get_peerlist_with_features()
    assert peers == []
    assert client._peerlist_supported is not False  # Still NOT disabled
    assert client._peerlist_timeout_count == 2


@pytest.mark.asyncio
async def test_peerlist_timeout_without_announced_features_disables():
    """
    Test that when directory did NOT announce peerlist_features,
    timeout permanently disables peerlist requests (likely reference impl).
    """
    # Mock the connection
    mock_connection = AsyncMock()
    mock_connection.receive.side_effect = TimeoutError("simulated timeout")

    # Initialize client without peerlist_features announcement
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client.directory_peerlist_features = False  # Directory did NOT announce feature
    client.timeout = 0.1  # Very short timeout for test

    # First request - should timeout AND disable peerlist
    peers = await client.get_peerlist_with_features()
    assert peers == []
    assert client._peerlist_supported is False  # Should be disabled

    # Reset rate limit
    client._last_peerlist_request_time = 0

    # Second request - should be skipped because peerlist is disabled
    mock_connection.send.reset_mock()
    peers = await client.get_peerlist_with_features()
    assert peers == []
    mock_connection.send.assert_not_called()  # Should skip the request entirely


@pytest.mark.asyncio
async def test_peerlist_success_resets_timeout_count():
    """Test that successful peerlist response resets the timeout counter."""

    # Mock the connection
    mock_connection = AsyncMock()

    # Initialize client
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client.directory_peerlist_features = True
    client._peerlist_timeout_count = 5  # Simulate previous timeouts
    client._peerlist_chunk_timeout = 0.1  # Very short for testing

    # Setup successful response - use side_effect for chunked handling
    response_data = {
        "type": MessageType.PEERLIST.value,
        "line": "nick1;location1",
    }
    mock_connection.receive.side_effect = [
        json.dumps(response_data).encode("utf-8"),
        TimeoutError(),  # Signal end of chunks
    ]

    # Run the method
    await client.get_peerlist_with_features()

    # Verify timeout count was reset
    assert client._peerlist_timeout_count == 0
    assert client._peerlist_supported is True


@pytest.mark.asyncio
async def test_privmsg_fidelity_bond_taker_nick():
    """
    Test that when receiving an offer via PRIVMSG, the fidelity bond is verified
    against the recipient's nick (us), not the maker's nick.
    """
    import asyncio

    # Setup
    client = DirectoryClient("host", 1234, "mainnet")
    client.nick = "MyNick"
    client.connection = AsyncMock()

    # Maker sending the offer
    maker_nick = "MakerNick"
    offer_msg = f"{maker_nick}!MyNick!sw0reloffer 0 1000 100000 500 0.001!tbond BOND_PROOF_BASE64"

    msg_data = {"type": MessageType.PRIVMSG.value, "line": offer_msg}

    # Mock receive to return the message then raise CancelledError to stop loop
    client.connection.receive.side_effect = [
        json.dumps(msg_data).encode("utf-8"),
        asyncio.CancelledError("Stop"),
    ]

    # Mock get_peerlist_with_features to do nothing
    client.get_peerlist_with_features = AsyncMock()

    # Mock parse_fidelity_bond_proof to verify arguments
    with patch("jmcore.directory_client.parse_fidelity_bond_proof") as mock_parse:
        # Pre-populate peer features
        client.peer_features = {maker_nick: {}}

        # Return a dummy bond dict so the code proceeds
        mock_parse.return_value = {
            "utxo_txid": "a" * 64,
            "utxo_vout": 0,
            "locktime": 1234567890,
            "utxo_pub": "02" + "a" * 64,
            "cert_expiry": 1000,
            "maker_nick": maker_nick,
            "taker_nick": "MyNick",
        }

        # Run listen_continuously for a short time
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(client.listen_continuously(request_orderbook=False), timeout=0.1)

        # Verify parse_fidelity_bond_proof was called with correct arguments
        # args: (proof_base64, maker_nick, taker_nick)
        # We expect taker_nick to be "MyNick" because it was a PRIVMSG to us
        mock_parse.assert_called_with("BOND_PROOF_BASE64", maker_nick, "MyNick")


def test_update_offer_features_updates_cached_offers():
    """
    Test that _update_offer_features correctly updates features on cached offers.

    This tests the fix for the race condition where offers are stored before
    peerlist response arrives with features.
    """
    from jmcore.directory_client import OfferWithTimestamp
    from jmcore.models import Offer, OfferType

    client = DirectoryClient("host", 1234, "mainnet")

    # Create some cached offers with empty features
    offer1 = Offer(
        counterparty="maker1",
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=1000,
        maxsize=100000,
        txfee=500,
        cjfee="0.001",
        features={},  # Empty features
    )
    offer2 = Offer(
        counterparty="maker1",
        oid=1,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=2000,
        maxsize=200000,
        txfee=600,
        cjfee="1000",
        features={},  # Empty features
    )
    offer3 = Offer(
        counterparty="maker2",  # Different maker
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=3000,
        maxsize=300000,
        txfee=700,
        cjfee="0.002",
        features={},  # Empty features
    )

    # Store offers in cache
    client.offers[("maker1", 0)] = OfferWithTimestamp(offer=offer1, received_at=1.0)
    client.offers[("maker1", 1)] = OfferWithTimestamp(offer=offer2, received_at=2.0)
    client.offers[("maker2", 0)] = OfferWithTimestamp(offer=offer3, received_at=3.0)

    # Update features for maker1
    updated_count = client._update_offer_features(
        "maker1", {"neutrino_compat": True, "other_feature": True}
    )

    # Verify count
    assert updated_count == 2

    # Verify maker1's offers have updated features
    assert offer1.features == {"neutrino_compat": True, "other_feature": True}
    assert offer2.features == {"neutrino_compat": True, "other_feature": True}

    # Verify maker2's offer is unchanged
    assert offer3.features == {}


def test_update_offer_features_no_matching_offers():
    """Test _update_offer_features when no offers match the nick."""
    client = DirectoryClient("host", 1234, "mainnet")

    # No offers cached
    updated_count = client._update_offer_features("nonexistent_maker", {"neutrino_compat": True})

    assert updated_count == 0


def test_update_offer_features_only_sets_true_values():
    """Test that _update_offer_features only sets features with True values."""
    from jmcore.directory_client import OfferWithTimestamp
    from jmcore.models import Offer, OfferType

    client = DirectoryClient("host", 1234, "mainnet")

    # Create offer with one existing feature
    offer = Offer(
        counterparty="maker1",
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=1000,
        maxsize=100000,
        txfee=500,
        cjfee="0.001",
        features={"existing_feature": True},
    )
    client.offers[("maker1", 0)] = OfferWithTimestamp(offer=offer, received_at=1.0)

    # Update with mixed true/false features
    client._update_offer_features("maker1", {"neutrino_compat": True, "disabled_feature": False})

    # Only true features should be added
    assert offer.features == {"existing_feature": True, "neutrino_compat": True}
    assert "disabled_feature" not in offer.features


def test_peerlist_response_updates_cached_offer_features():
    """
    Integration test: verify that processing a peerlist response updates
    features on previously cached offers.

    This tests the full fix for the race condition bug.
    """
    from jmcore.directory_client import OfferWithTimestamp
    from jmcore.models import Offer, OfferType
    from jmcore.protocol import FEATURE_NEUTRINO_COMPAT

    client = DirectoryClient("host", 1234, "mainnet")

    # Create an offer with empty features (simulating race condition)
    offer = Offer(
        counterparty="J57wPBk1VfjSP5Te",
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=30000,
        maxsize=643786,
        txfee=0,
        cjfee="0.0001",
        features={},  # Empty - offer stored before peerlist arrived
    )
    client.offers[("J57wPBk1VfjSP5Te", 0)] = OfferWithTimestamp(offer=offer, received_at=1.0)

    # Simulate peerlist response with features
    peerlist_str = f"J57wPBk1VfjSP5Te;maker.onion:62780;F:{FEATURE_NEUTRINO_COMPAT}"

    # Process peerlist response
    peers = client._handle_peerlist_response(peerlist_str)

    # Verify peer was parsed
    assert len(peers) == 1
    nick, location, features = peers[0]
    assert nick == "J57wPBk1VfjSP5Te"
    assert features.supports_neutrino_compat()

    # Verify peer_features cache was updated
    assert client.peer_features["J57wPBk1VfjSP5Te"] == {"neutrino_compat": True}

    # Verify cached offer's features were updated (THE FIX)
    assert offer.features == {"neutrino_compat": True}


@pytest.mark.asyncio
async def test_get_peerlist_with_features_handles_chunked_response():
    """
    Test that get_peerlist_with_features accumulates peers from multiple PEERLIST chunks.
    """
    # Mock the connection
    mock_connection = AsyncMock()

    # Setup chunked responses - 3 chunks with 2 peers each
    chunk1 = {
        "type": MessageType.PEERLIST.value,
        "line": "nick1;loc1.onion:5222,nick2;loc2.onion:5222",
    }
    chunk2 = {
        "type": MessageType.PEERLIST.value,
        "line": "nick3;loc3.onion:5222,nick4;loc4.onion:5222",
    }
    chunk3 = {
        "type": MessageType.PEERLIST.value,
        "line": "nick5;loc5.onion:5222,nick6;loc6.onion:5222",
    }

    # Return chunks, then timeout to signal end

    mock_connection.receive.side_effect = [
        json.dumps(chunk1).encode("utf-8"),
        json.dumps(chunk2).encode("utf-8"),
        json.dumps(chunk3).encode("utf-8"),
        TimeoutError(),  # Signal end of chunks
    ]

    # Initialize client with short chunk timeout for fast tests
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client._peerlist_chunk_timeout = 0.1  # Very short for testing

    # Run the method
    peers = await client.get_peerlist_with_features()

    # Should have accumulated all 6 peers from 3 chunks
    assert len(peers) == 6
    nicks = [p[0] for p in peers]
    assert "nick1" in nicks
    assert "nick6" in nicks


@pytest.mark.asyncio
async def test_get_peerlist_handles_chunked_response():
    """
    Test that get_peerlist accumulates peers from multiple PEERLIST chunks.
    """

    mock_connection = AsyncMock()

    # Two chunks with peers
    chunk1 = {
        "type": MessageType.PEERLIST.value,
        "line": "peer1;onion1.onion:5222,peer2;onion2.onion:5222",
    }
    chunk2 = {"type": MessageType.PEERLIST.value, "line": "peer3;onion3.onion:5222"}

    mock_connection.receive.side_effect = [
        json.dumps(chunk1).encode("utf-8"),
        json.dumps(chunk2).encode("utf-8"),
        TimeoutError(),
    ]

    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client._peerlist_chunk_timeout = 0.1  # Very short for testing

    peers = await client.get_peerlist()

    assert peers is not None
    assert len(peers) == 3
    assert "peer1" in peers
    assert "peer2" in peers
    assert "peer3" in peers


@pytest.mark.asyncio
async def test_get_peerlist_single_chunk_backward_compatible():
    """
    Test that get_peerlist still works with single-chunk responses (backward compatibility).
    """

    mock_connection = AsyncMock()

    # Single chunk with all peers (old behavior)
    response = {
        "type": MessageType.PEERLIST.value,
        "line": "nick1;loc1.onion:5222,nick2;loc2.onion:5222",
    }

    mock_connection.receive.side_effect = [
        json.dumps(response).encode("utf-8"),
        TimeoutError(),  # End of chunks
    ]

    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client._peerlist_chunk_timeout = 0.1  # Very short for testing

    peers = await client.get_peerlist()

    assert peers is not None
    assert len(peers) == 2


@pytest.mark.asyncio
async def test_get_peerlist_empty_first_chunk():
    """
    Test that empty first chunk is handled correctly.
    """

    mock_connection = AsyncMock()

    # Empty first chunk (edge case)
    response = {"type": MessageType.PEERLIST.value, "line": ""}

    mock_connection.receive.side_effect = [
        json.dumps(response).encode("utf-8"),
        TimeoutError(),
    ]

    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client._peerlist_chunk_timeout = 0.1  # Very short for testing

    peers = await client.get_peerlist()

    assert peers is not None
    assert len(peers) == 0


@pytest.mark.asyncio
async def test_get_peerlist_with_features_buffers_unexpected_messages():
    """
    Test that unexpected messages during peerlist reception are buffered, not lost.
    """

    mock_connection = AsyncMock()

    # Interleaved with PUBMSG
    pubmsg = {"type": MessageType.PUBMSG.value, "line": "maker!PUBLIC!offer some_offer_data"}
    chunk1 = {"type": MessageType.PEERLIST.value, "line": "nick1;loc1.onion:5222"}

    mock_connection.receive.side_effect = [
        json.dumps(pubmsg).encode("utf-8"),  # Unexpected PUBMSG
        json.dumps(chunk1).encode("utf-8"),
        TimeoutError(),
    ]

    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client._peerlist_chunk_timeout = 0.1  # Very short for testing

    peers = await client.get_peerlist_with_features()

    # Should have the peer from the chunk
    assert len(peers) == 1

    # PUBMSG should be in the buffer
    assert not client._message_buffer.empty()
    buffered_msg = await client._message_buffer.get()
    assert buffered_msg["type"] == MessageType.PUBMSG.value


# ---------------------------------------------------------------------------
# Adaptive orderbook listening tests
# ---------------------------------------------------------------------------


def _make_offer_msg(nick: str = "maker1", offer_type: str = "sw0reloffer") -> dict[str, Any]:
    """Create a mock PUBMSG containing an offer."""
    # Absolute offers (absoffer) use integer satoshi fees; relative use float fractions
    cjfee = "1000" if "abs" in offer_type else "0.001"
    return {
        "type": MessageType.PUBMSG.value,
        "line": f"{nick}!PUBLIC!{offer_type} 0 750000 790107726787 500 {cjfee}",
    }


def _make_non_offer_msg() -> dict[str, Any]:
    """Create a mock PUBMSG that is NOT an offer (e.g. peerlist)."""
    return {
        "type": MessageType.PEERLIST.value,
        "line": "nick1;loc1.onion:5222",
    }


@pytest.mark.asyncio
async def test_adaptive_fetch_exits_early_after_quiet_period():
    """
    When offers arrive in the first chunk and then stop, fetch_orderbooks should
    exit after min_wait + quiet_period instead of waiting the full max_wait.
    """
    mock_connection = AsyncMock()
    mock_connection.is_connected.return_value = True

    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection

    # Mock get_peerlist_with_features to return empty
    client.get_peerlist_with_features = AsyncMock(return_value=[])

    # Track call count to listen_for_messages
    call_count = 0
    offer_msg = _make_offer_msg()

    async def mock_listen(duration: float = 5.0) -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        # First call: return an offer
        if call_count == 1:
            return [offer_msg]
        # Subsequent calls: simulate passage of time with no offers
        await asyncio.sleep(duration)
        return []

    client.listen_for_messages = mock_listen  # type: ignore[assignment]

    start = asyncio.get_event_loop().time()
    offers, bonds = await client.fetch_orderbooks(max_wait=30.0, min_wait=1.0, quiet_period=2.0)
    elapsed = asyncio.get_event_loop().time() - start

    # Should have exited well before max_wait (30s)
    # Expected: ~min_wait(1) + quiet_period(2) = ~3s
    assert elapsed < 10.0, f"Should exit early but took {elapsed:.1f}s"
    # Should have found the offer
    assert len(offers) == 1


@pytest.mark.asyncio
async def test_adaptive_fetch_respects_min_wait():
    """
    Even if offers arrive immediately and then stop, fetch_orderbooks should
    not exit before min_wait has elapsed.
    """
    mock_connection = AsyncMock()
    mock_connection.is_connected.return_value = True

    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client.get_peerlist_with_features = AsyncMock(return_value=[])

    call_count = 0
    offer_msg = _make_offer_msg()

    async def mock_listen(duration: float = 5.0) -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [offer_msg]
        await asyncio.sleep(duration)
        return []

    client.listen_for_messages = mock_listen  # type: ignore[assignment]

    start = asyncio.get_event_loop().time()
    offers, bonds = await client.fetch_orderbooks(max_wait=30.0, min_wait=3.0, quiet_period=1.0)
    elapsed = asyncio.get_event_loop().time() - start

    # Must wait at least min_wait (3s), even though quiet_period (1s) would trigger sooner
    assert elapsed >= 3.0, f"Should wait at least min_wait but exited at {elapsed:.1f}s"
    # But should still exit well before max_wait
    assert elapsed < 10.0, f"Should exit early but took {elapsed:.1f}s"
    assert len(offers) == 1


@pytest.mark.asyncio
async def test_adaptive_fetch_respects_max_wait():
    """
    If offers keep arriving, fetch_orderbooks should stop at max_wait.
    """
    mock_connection = AsyncMock()
    mock_connection.is_connected.return_value = True

    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client.get_peerlist_with_features = AsyncMock(return_value=[])

    counter = 0

    async def mock_listen(duration: float = 5.0) -> list[dict[str, Any]]:
        nonlocal counter
        counter += 1
        # Always return an offer (never quiet)
        await asyncio.sleep(min(duration, 0.1))
        return [_make_offer_msg(nick=f"maker{counter}")]

    client.listen_for_messages = mock_listen  # type: ignore[assignment]

    start = asyncio.get_event_loop().time()
    offers, bonds = await client.fetch_orderbooks(max_wait=3.0, min_wait=1.0, quiet_period=1.0)
    elapsed = asyncio.get_event_loop().time() - start

    # Should stop at max_wait (3s), not run forever
    assert elapsed < 5.0, f"Should respect max_wait but took {elapsed:.1f}s"
    assert len(offers) > 0


@pytest.mark.asyncio
async def test_adaptive_fetch_no_offers_exits_after_min_wait_plus_quiet():
    """
    When no offers arrive at all, exit after min_wait + quiet_period.
    last_offer_time is initialized to start_time, so silence is measured from start.
    """
    mock_connection = AsyncMock()
    mock_connection.is_connected.return_value = True

    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client.get_peerlist_with_features = AsyncMock(return_value=[])

    async def mock_listen(duration: float = 5.0) -> list[dict[str, Any]]:
        await asyncio.sleep(duration)
        return []

    client.listen_for_messages = mock_listen  # type: ignore[assignment]

    start = asyncio.get_event_loop().time()
    offers, bonds = await client.fetch_orderbooks(max_wait=30.0, min_wait=2.0, quiet_period=2.0)
    elapsed = asyncio.get_event_loop().time() - start

    # Should exit after min_wait (2) when silence >= quiet_period (2).
    # Since last_offer_time = start, silence = elapsed, so exits when elapsed >= max(2, 2) ~= 2-3s
    assert elapsed < 10.0, f"Should exit early with no offers but took {elapsed:.1f}s"
    assert len(offers) == 0


@pytest.mark.asyncio
async def test_adaptive_fetch_counts_different_offer_types():
    """
    The lightweight offer detection should count all offer types
    (sw0reloffer, sw0absoffer, swreloffer, swabsoffer).
    """
    mock_connection = AsyncMock()
    mock_connection.is_connected.return_value = True

    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = mock_connection
    client.get_peerlist_with_features = AsyncMock(return_value=[])

    call_count = 0

    async def mock_listen(duration: float = 5.0) -> list[dict[str, Any]]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [
                _make_offer_msg("maker1", "sw0reloffer"),
                _make_offer_msg("maker2", "sw0absoffer"),
                _make_offer_msg("maker3", "swreloffer"),
                _make_offer_msg("maker4", "swabsoffer"),
                _make_non_offer_msg(),  # peerlist, not an offer
            ]
        await asyncio.sleep(duration)
        return []

    client.listen_for_messages = mock_listen  # type: ignore[assignment]

    offers, bonds = await client.fetch_orderbooks(max_wait=30.0, min_wait=1.0, quiet_period=2.0)

    # Should have parsed 4 offers (not the peerlist)
    assert len(offers) == 4
