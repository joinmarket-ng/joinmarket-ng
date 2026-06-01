"""
Tests for message router, focusing on failed send cleanup and offer tracking.
"""

import pytest
from jmcore.models import MessageEnvelope, NetworkType, PeerInfo, PeerStatus
from jmcore.protocol import MessageType

from directory_server.message_router import MessageRouter
from directory_server.peer_registry import PeerRegistry


@pytest.fixture
def registry():
    return PeerRegistry(max_peers=100)


@pytest.fixture
def sample_peers(registry):
    """Create and register sample peers."""
    peers = []
    # Use different base characters for each peer to get unique onion addresses
    base_chars = ["a", "b", "c", "d", "e"]
    for i, char in enumerate(base_chars):
        peer = PeerInfo(
            nick=f"peer{i}",
            onion_address=f"{char * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(peer)
        peers.append(peer)
    return peers


class TestMessageRouterFailedSendCleanup:
    """Tests for cleanup behavior when sends fail."""

    @pytest.mark.anyio
    async def test_safe_send_calls_on_send_failed_callback(self, registry, sample_peers):
        """When a send fails, the on_send_failed callback should be invoked."""
        failed_peers = []

        async def failing_send(peer_key: str, data: bytes) -> None:
            raise ConnectionError("Connection closed")

        async def on_failed(peer_key: str) -> None:
            failed_peers.append(peer_key)

        router = MessageRouter(
            peer_registry=registry,
            send_callback=failing_send,
            on_send_failed=on_failed,
        )

        # Attempt to send - should fail and trigger callback
        await router._safe_send("peer0", b"test data", "peer0")

        assert "peer0" in failed_peers

    @pytest.mark.anyio
    async def test_safe_send_skips_already_failed_peers(self, registry, sample_peers):
        """Peers that have already failed should be skipped on subsequent attempts."""
        send_attempts = []

        async def failing_send(peer_key: str, data: bytes) -> None:
            send_attempts.append(peer_key)
            raise ConnectionError("Connection closed")

        router = MessageRouter(
            peer_registry=registry,
            send_callback=failing_send,
        )

        # First attempt - should try to send
        await router._safe_send("peer0", b"test data", "peer0")
        assert len(send_attempts) == 1

        # Second attempt - should skip because peer is in _failed_peers
        await router._safe_send("peer0", b"test data", "peer0")
        assert len(send_attempts) == 1  # No additional attempt

    @pytest.mark.anyio
    async def test_batched_broadcast_clears_failed_peers_on_new_broadcast(
        self, registry, sample_peers
    ):
        """Each new broadcast should clear the failed peers set."""
        send_attempts = []

        async def failing_send(peer_key: str, data: bytes) -> None:
            send_attempts.append(peer_key)
            raise ConnectionError("Connection closed")

        router = MessageRouter(
            peer_registry=registry,
            send_callback=failing_send,
        )

        targets = [(sample_peers[0].location_string, sample_peers[0].nick)]

        # First broadcast - peer fails
        await router._batched_broadcast(targets, b"test data")
        assert len(send_attempts) == 1

        # Second broadcast - should try again because _failed_peers was cleared
        await router._batched_broadcast(targets, b"test data")
        assert len(send_attempts) == 2

    @pytest.mark.anyio
    async def test_batched_broadcast_filters_failed_peers_within_batch(
        self, registry, sample_peers
    ):
        """Failed peers should be filtered out within the same broadcast."""
        send_attempts = []
        fail_peer = sample_peers[0].location_string

        async def selective_failing_send(peer_key: str, data: bytes) -> None:
            send_attempts.append(peer_key)
            if peer_key == fail_peer:
                raise ConnectionError("Connection closed")

        router = MessageRouter(
            peer_registry=registry,
            send_callback=selective_failing_send,
            broadcast_batch_size=2,  # Small batch to test filtering across batches
        )

        # Create targets with the failing peer appearing in multiple batches conceptually
        # (in practice they're unique, but the failed set should prevent retries)
        targets = [(p.location_string, p.nick) for p in sample_peers]

        await router._batched_broadcast(targets, b"test data")

        # Each peer should only be attempted once
        unique_attempts = set(send_attempts)
        assert len(unique_attempts) == len(send_attempts)

    @pytest.mark.anyio
    async def test_on_send_failed_callback_error_is_handled(self, registry, sample_peers):
        """Errors in the on_send_failed callback should not propagate."""

        async def failing_send(peer_key: str, data: bytes) -> None:
            raise ConnectionError("Connection closed")

        async def broken_callback(peer_key: str) -> None:
            raise RuntimeError("Callback error")

        router = MessageRouter(
            peer_registry=registry,
            send_callback=failing_send,
            on_send_failed=broken_callback,
        )

        # Should not raise despite callback error
        await router._safe_send("peer0", b"test data", "peer0")

    @pytest.mark.anyio
    async def test_successful_send_does_not_trigger_callback(self, registry, sample_peers):
        """Successful sends should not trigger the on_send_failed callback."""
        failed_peers = []

        async def successful_send(peer_key: str, data: bytes) -> None:
            pass  # Success

        async def on_failed(peer_key: str) -> None:
            failed_peers.append(peer_key)

        router = MessageRouter(
            peer_registry=registry,
            send_callback=successful_send,
            on_send_failed=on_failed,
        )

        await router._safe_send("peer0", b"test data", "peer0")

        assert len(failed_peers) == 0


class TestMessageRouterPrivateMessageFailedSend:
    """Tests for private message routing with failed sends."""

    @pytest.mark.anyio
    async def test_private_message_failure_triggers_cleanup(self, registry, sample_peers):
        """When private message routing fails, cleanup callback should be called."""
        failed_peers = []
        from_peer = sample_peers[0]
        to_peer = sample_peers[1]

        async def failing_send(peer_key: str, data: bytes) -> None:
            raise ConnectionError("Connection closed")

        async def on_failed(peer_key: str) -> None:
            failed_peers.append(peer_key)

        router = MessageRouter(
            peer_registry=registry,
            send_callback=failing_send,
            on_send_failed=on_failed,
        )

        # Create a valid private message (format: from_nick!to_nick!command message)
        payload = f"{from_peer.nick}!{to_peer.nick}!test message"
        envelope = MessageEnvelope(message_type=MessageType.PRIVMSG, payload=payload)

        await router._handle_private_message(envelope, from_peer.location_string)

        # The target peer should have been marked as failed
        assert to_peer.location_string in failed_peers


class TestOfferTracking:
    """Tests for offer tracking functionality."""

    @pytest.mark.anyio
    async def test_tracks_sw0absoffer(self, registry):
        """Should track sw0absoffer messages."""
        sent_messages = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            sent_messages.append((peer_key, data))

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
        )

        # Create maker peer
        maker = PeerInfo(
            nick="maker1",
            onion_address="a" * 56 + ".onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(maker)

        # Send offer message
        payload = f"{maker.nick}!PUBLIC!sw0absoffer 0 30000 72590 0 1000"
        envelope = MessageEnvelope(message_type=MessageType.PUBMSG, payload=payload)

        await router._handle_public_message(envelope, maker.location_string)

        # Check offer was tracked
        stats = router.get_offer_stats()
        assert stats["total_offers"] == 1
        assert stats["peers_with_offers"] == 1

    @pytest.mark.anyio
    async def test_tracks_tr0offer(self, registry):
        """Should track taproot (tr0) offer messages."""
        sent_messages = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            sent_messages.append((peer_key, data))

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
        )

        maker = PeerInfo(
            nick="maker1",
            onion_address="a" * 56 + ".onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(maker)

        payload = f"{maker.nick}!PUBLIC!tr0reloffer 0 30000 72590 0 0.001"
        envelope = MessageEnvelope(message_type=MessageType.PUBMSG, payload=payload)

        await router._handle_public_message(envelope, maker.location_string)

        stats = router.get_offer_stats()
        assert stats["total_offers"] == 1
        assert stats["peers_with_offers"] == 1

    @pytest.mark.anyio
    async def test_tracks_multiple_offers_per_peer(self, registry):
        """Should track multiple offers from same peer."""
        sent_messages = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            sent_messages.append((peer_key, data))

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
        )

        # Create maker peer
        maker = PeerInfo(
            nick="maker1",
            onion_address="a" * 56 + ".onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(maker)

        # Send multiple offer messages
        for i in range(3):
            payload = f"{maker.nick}!PUBLIC!sw0absoffer {i} 30000 72590 0 1000"
            envelope = MessageEnvelope(message_type=MessageType.PUBMSG, payload=payload)
            await router._handle_public_message(envelope, maker.location_string)

        # Check offers were tracked
        stats = router.get_offer_stats()
        assert stats["total_offers"] == 3
        assert stats["peers_with_offers"] == 1

    @pytest.mark.anyio
    async def test_peers_with_many_offers(self, registry):
        """Should identify peers with more than 2 offers."""
        sent_messages = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            sent_messages.append((peer_key, data))

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
        )

        # Create maker peers
        for idx in range(2):
            maker = PeerInfo(
                nick=f"maker{idx}",
                onion_address=chr(ord("a") + idx) * 56 + ".onion",
                port=5222,
                network=NetworkType.MAINNET,
                status=PeerStatus.HANDSHAKED,
            )
            registry.register(maker)

            # First maker has 5 offers, second has 2
            num_offers = 5 if idx == 0 else 2
            for i in range(num_offers):
                payload = f"{maker.nick}!PUBLIC!sw0reloffer {i} 30000 72590 0 0.001"
                envelope = MessageEnvelope(message_type=MessageType.PUBMSG, payload=payload)
                await router._handle_public_message(envelope, maker.location_string)

        # Check stats
        stats = router.get_offer_stats()
        assert stats["total_offers"] == 7
        assert stats["peers_with_offers"] == 2
        assert len(stats["peers_many_offers"]) == 1  # Only maker0 has >2 offers
        assert stats["peers_many_offers"][0] == ("maker0", 5)

    @pytest.mark.anyio
    async def test_remove_peer_offers(self, registry):
        """Should remove offers when peer disconnects."""
        sent_messages = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            sent_messages.append((peer_key, data))

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
        )

        # Create maker peer
        maker = PeerInfo(
            nick="maker1",
            onion_address="a" * 56 + ".onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(maker)

        # Send offer message
        payload = f"{maker.nick}!PUBLIC!sw0absoffer 0 30000 72590 0 1000"
        envelope = MessageEnvelope(message_type=MessageType.PUBMSG, payload=payload)
        await router._handle_public_message(envelope, maker.location_string)

        # Verify offer is tracked
        stats = router.get_offer_stats()
        assert stats["total_offers"] == 1

        # Remove peer offers
        router.remove_peer_offers(maker.location_string)

        # Verify offers were removed
        stats = router.get_offer_stats()
        assert stats["total_offers"] == 0
        assert stats["peers_with_offers"] == 0


class TestChunkedPeerlist:
    """Tests for chunked peerlist sending."""

    @pytest.mark.anyio
    async def test_send_peerlist_chunks_large_list(self, registry: PeerRegistry) -> None:
        """Should send peerlist in chunks for large peer lists."""
        sent_messages: list[tuple[str, bytes]] = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            sent_messages.append((peer_key, data))

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
        )

        # Create 50 peers (more than default chunk_size=20)
        # Use valid onion address format: 56 chars of [a-z2-7]
        # Valid chars in base32: a-z and 2-7 (no 0, 1, 8, 9)
        valid_chars = "abcdefghijklmnopqrstuvwxyz234567"
        for i in range(50):
            # Generate valid onion address using only valid base32 chars
            # Use different starting chars to make unique addresses
            char1 = valid_chars[i % 32]
            char2 = valid_chars[(i // 32) % 32]
            onion = f"{char1}{char2}{'a' * 54}.onion"
            peer = PeerInfo(
                nick=f"peer{i:02d}",
                onion_address=onion,
                port=5222,
                network=NetworkType.MAINNET,
                status=PeerStatus.HANDSHAKED,
            )
            registry.register(peer)

        # Create requesting peer
        requester = PeerInfo(
            nick="requester",
            onion_address="r" * 56 + ".onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(requester)

        # Send peerlist
        await router.send_peerlist(requester.location_string, NetworkType.MAINNET, chunk_size=20)

        # Should have sent 3 chunks (50 peers / 20 = 2.5, rounded up)
        # Note: requester is also in the registry so it's 51 total, but requester
        # isn't excluded from peerlist by default
        assert len(sent_messages) >= 3

        # Parse messages to verify content
        total_peers = 0
        for peer_key, data in sent_messages:
            assert peer_key == requester.location_string
            envelope = MessageEnvelope.from_bytes(data)
            assert envelope.message_type == MessageType.PEERLIST
            # Count comma-separated entries (each peer is an entry)
            if envelope.payload:
                entries = envelope.payload.split(",")
                total_peers += len(entries)

        # Should have all peers (including requester since it's in the registry)
        assert total_peers >= 50

    @pytest.mark.anyio
    async def test_send_peerlist_single_chunk_for_small_list(self, registry: PeerRegistry) -> None:
        """Should send single chunk for small peer lists."""
        sent_messages: list[tuple[str, bytes]] = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            sent_messages.append((peer_key, data))

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
        )

        # Create 5 peers (less than chunk_size)
        for i in range(5):
            peer = PeerInfo(
                nick=f"peer{i}",
                onion_address=f"{chr(ord('a') + i) * 56}.onion",
                port=5222,
                network=NetworkType.MAINNET,
                status=PeerStatus.HANDSHAKED,
            )
            registry.register(peer)

        # Create requesting peer
        requester = PeerInfo(
            nick="requester",
            onion_address="r" * 56 + ".onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(requester)

        # Send peerlist
        await router.send_peerlist(requester.location_string, NetworkType.MAINNET, chunk_size=20)

        # Should have sent exactly 1 chunk (6 peers including requester < 20)
        assert len(sent_messages) == 1

    @pytest.mark.anyio
    async def test_send_peerlist_empty_registry(self, registry: PeerRegistry) -> None:
        """Should send empty peerlist response when registry is empty."""
        sent_messages: list[tuple[str, bytes]] = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            sent_messages.append((peer_key, data))

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
        )

        # Send peerlist to a non-existent peer (simulating empty registry scenario)
        # We need a valid location string format
        await router.send_peerlist(
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222",
            NetworkType.MAINNET,
            chunk_size=20,
        )

        # Should still send one response (empty)
        assert len(sent_messages) == 1
        envelope = MessageEnvelope.from_bytes(sent_messages[0][1])
        assert envelope.message_type == MessageType.PEERLIST
        assert envelope.payload == ""


class TestPingPongRouting:
    """Tests for PING/PONG message routing."""

    @pytest.mark.anyio
    async def test_ping_sends_pong_response(self, registry, sample_peers):
        """When a PING is received, the router should respond with a PONG."""
        sent_messages: list[tuple[str, bytes]] = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            sent_messages.append((peer_key, data))

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
        )

        from_key = sample_peers[0].location_string
        envelope = MessageEnvelope(message_type=MessageType.PING, payload="")

        await router.route_message(envelope, from_key)

        assert len(sent_messages) == 1
        peer_key, data = sent_messages[0]
        assert peer_key == from_key
        response = MessageEnvelope.from_bytes(data)
        assert response.message_type == MessageType.PONG
        assert response.payload == ""

    @pytest.mark.anyio
    async def test_pong_calls_on_pong_callback(self, registry, sample_peers):
        """When a PONG is received, the on_pong callback should be invoked."""
        pong_keys: list[str] = []

        async def mock_send(peer_key: str, data: bytes) -> None:
            pass

        def on_pong(peer_key: str) -> None:
            pong_keys.append(peer_key)

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
            on_pong=on_pong,
        )

        from_key = sample_peers[0].location_string
        envelope = MessageEnvelope(message_type=MessageType.PONG, payload="")

        await router.route_message(envelope, from_key)

        assert pong_keys == [from_key]

    @pytest.mark.anyio
    async def test_pong_without_callback_does_not_raise(self, registry, sample_peers):
        """When a PONG is received with no on_pong callback, nothing should happen."""

        async def mock_send(peer_key: str, data: bytes) -> None:
            pass

        router = MessageRouter(
            peer_registry=registry,
            send_callback=mock_send,
            on_pong=None,
        )

        from_key = sample_peers[0].location_string
        envelope = MessageEnvelope(message_type=MessageType.PONG, payload="")

        # Should not raise
        await router.route_message(envelope, from_key)

    @pytest.mark.anyio
    async def test_ping_send_failure_does_not_raise(self, registry, sample_peers):
        """If sending the PONG response fails, it should be handled gracefully."""

        async def failing_send(peer_key: str, data: bytes) -> None:
            raise ConnectionError("Connection closed")

        router = MessageRouter(
            peer_registry=registry,
            send_callback=failing_send,
        )

        from_key = sample_peers[0].location_string
        envelope = MessageEnvelope(message_type=MessageType.PING, payload="")

        # Should not raise
        await router.route_message(envelope, from_key)
