"""
Tests for server send failure cleanup to prevent zombie peers.

These tests verify that when sending to a peer fails, the peer is properly
cleaned up from both the connection mapping and the peer registry.
"""

from unittest.mock import AsyncMock

import pytest
from jmcore.models import MessageEnvelope, NetworkType, PeerInfo, PeerStatus
from jmcore.protocol import MessageType
from jmcore.settings import DirectoryServerSettings

from directory_server.server import DirectoryServer


@pytest.fixture
def settings():
    return DirectoryServerSettings(
        host="127.0.0.1",
        port=0,  # Let OS assign port
        max_peers=100,
    )


@pytest.fixture
def server(settings):
    return DirectoryServer(settings, NetworkType.MAINNET, "J5TestNickOOOOOO")


@pytest.fixture
def sample_peer():
    """Create a sample peer with a valid onion address."""
    return PeerInfo(
        nick="test_peer",
        onion_address="a" * 56 + ".onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )


class TestHandleSendFailed:
    """Tests for _handle_send_failed cleanup behavior."""

    @pytest.mark.anyio
    async def test_handle_send_failed_removes_connection_mapping(self, server, sample_peer):
        """When send fails, the peer_key_to_conn_id mapping should be removed."""
        # Register the peer
        server.peer_registry.register(sample_peer)
        peer_key = sample_peer.nick

        # Simulate connection mapping
        server.peer_key_to_conn_id[peer_key] = "conn_123"

        # Trigger send failure cleanup
        await server._handle_send_failed(peer_key)

        # Verify mapping is removed
        assert peer_key not in server.peer_key_to_conn_id

    @pytest.mark.anyio
    async def test_handle_send_failed_unregisters_peer_from_registry(self, server, sample_peer):
        """When send fails, the peer should be unregistered from the registry."""
        # Register the peer
        server.peer_registry.register(sample_peer)
        peer_key = sample_peer.nick

        # Verify peer is registered
        assert server.peer_registry.get_by_key(peer_key) is not None
        assert server.peer_registry.get_by_nick(sample_peer.nick) is not None

        # Trigger send failure cleanup
        await server._handle_send_failed(peer_key)

        # Verify peer is unregistered
        assert server.peer_registry.get_by_key(peer_key) is None
        assert server.peer_registry.get_by_nick(sample_peer.nick) is None

    @pytest.mark.anyio
    async def test_handle_send_failed_handles_missing_peer(self, server):
        """_handle_send_failed should handle non-existent peer gracefully."""
        # Should not raise for non-existent peer
        await server._handle_send_failed("nonexistent_peer")

    @pytest.mark.anyio
    async def test_handle_send_failed_handles_missing_mapping(self, server, sample_peer):
        """_handle_send_failed should handle missing connection mapping gracefully."""
        # Register the peer but don't create a connection mapping
        server.peer_registry.register(sample_peer)
        peer_key = sample_peer.nick

        # Should not raise despite missing mapping
        await server._handle_send_failed(peer_key)

        # Peer should still be unregistered
        assert server.peer_registry.get_by_key(peer_key) is None

    @pytest.mark.anyio
    async def test_handle_send_failed_removes_both_mapping_and_registry(self, server, sample_peer):
        """_handle_send_failed should clean up both mapping and registry."""
        # Register the peer
        server.peer_registry.register(sample_peer)
        peer_key = sample_peer.nick

        # Create connection mapping
        server.peer_key_to_conn_id[peer_key] = "conn_456"

        # Trigger send failure cleanup
        await server._handle_send_failed(peer_key)

        # Verify complete cleanup
        assert peer_key not in server.peer_key_to_conn_id
        assert server.peer_registry.get_by_key(peer_key) is None
        assert server.peer_registry.get_by_nick(sample_peer.nick) is None

    @pytest.mark.anyio
    async def test_peer_not_in_iter_connected_after_send_failure(self, server, sample_peer):
        """After send failure, peer should not appear in iter_connected."""
        # Register the peer
        server.peer_registry.register(sample_peer)
        peer_key = sample_peer.nick

        # Verify peer appears in connected list
        connected_before = list(server.peer_registry.iter_connected(NetworkType.MAINNET))
        assert any(p.nick == sample_peer.nick for p in connected_before)

        # Trigger send failure cleanup
        await server._handle_send_failed(peer_key)

        # Verify peer no longer appears in connected list
        connected_after = list(server.peer_registry.iter_connected(NetworkType.MAINNET))
        assert not any(p.nick == sample_peer.nick for p in connected_after)

    @pytest.mark.anyio
    async def test_stale_send_failure_does_not_remove_replacement(self, server, sample_peer):
        peer_key = server.peer_registry.register(
            sample_peer, "old", verified_pubkey=b"same-pubkey"
        ).peer_key
        replacement = sample_peer.model_copy(deep=True)
        server.peer_registry.register(replacement, "new", verified_pubkey=b"same-pubkey")
        server.peer_key_to_conn_id[peer_key] = "new"

        await server._handle_send_failed(peer_key, "old")

        assert server.peer_registry.get_by_key(peer_key) is replacement
        assert server.peer_key_to_conn_id[peer_key] == "new"

    @pytest.mark.anyio
    async def test_stale_connection_cleanup_does_not_remove_replacement(self, server, sample_peer):
        peer_key = server.peer_registry.register(
            sample_peer, "old", verified_pubkey=b"same-pubkey"
        ).peer_key
        replacement = sample_peer.model_copy(deep=True)
        server.peer_registry.register(replacement, "new", verified_pubkey=b"same-pubkey")
        server.peer_key_to_conn_id[peer_key] = "new"
        server.message_router._peer_offers[(peer_key, "new")] = {"new-offer"}
        old_connection = AsyncMock()

        await server._cleanup_peer(old_connection, "old", peer_key)

        assert server.peer_registry.get_by_key(peer_key) is replacement
        assert server.peer_key_to_conn_id[peer_key] == "new"
        assert server.message_router.get_offer_stats()["total_offers"] == 1

    @pytest.mark.anyio
    async def test_send_failure_cleans_generation_state_and_broadcasts_disconnect(
        self, server, sample_peer
    ):
        failed_connection = AsyncMock()
        observer_connection = AsyncMock()
        peer_key = server.peer_registry.register(sample_peer, "failed").peer_key
        server.peer_key_to_conn_id[peer_key] = "failed"
        server.connections.add("failed", failed_connection)
        server.message_router._peer_offers[(peer_key, "failed")] = {"offer"}
        server.heartbeat._pong_pending.add((peer_key, "failed"))
        observer = PeerInfo(
            nick="observer",
            onion_address="b" * 56 + ".onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        observer_key = server.peer_registry.register(observer, "observer").peer_key
        server.peer_key_to_conn_id[observer_key] = "observer"
        server.connections.add("observer", observer_connection)

        await server._handle_send_failed(peer_key, "failed")

        assert server.peer_registry.get_by_key(peer_key) is None
        assert server.message_router.get_offer_stats()["total_offers"] == 0
        assert (peer_key, "failed") not in server.heartbeat.pong_pending
        assert server.connections.get("failed") is None
        failed_connection.close.assert_awaited_once()
        sent = MessageEnvelope.from_bytes(observer_connection.send.await_args.args[0])
        assert sent.message_type == MessageType.PEERLIST
        assert sent.payload == f"{sample_peer.nick};{sample_peer.location_string};D"

    @pytest.mark.anyio
    async def test_send_failure_preserves_peer_with_same_location(self, server, sample_peer):
        failed_connection = AsyncMock()
        surviving_connection = AsyncMock()
        peer_key = server.peer_registry.register(sample_peer, "failed").peer_key
        server.peer_key_to_conn_id[peer_key] = "failed"
        server.connections.add("failed", failed_connection)
        survivor = sample_peer.model_copy(update={"nick": "survivor"}, deep=True)
        survivor_key = server.peer_registry.register(survivor, "survivor").peer_key
        server.peer_key_to_conn_id[survivor_key] = "survivor"
        server.connections.add("survivor", surviving_connection)

        await server._handle_send_failed(peer_key, "failed")

        assert server.peer_registry.get_by_key(peer_key) is None
        assert server.peer_registry.get_by_key(survivor_key) is survivor
        assert server.peer_key_to_conn_id[survivor_key] == "survivor"
        sent = MessageEnvelope.from_bytes(surviving_connection.send.await_args.args[0])
        assert sent.payload == f"{sample_peer.nick};{sample_peer.location_string};D"


class TestPassivePeerSendFailure:
    """Tests for send failure cleanup with passive peers (NOT-SERVING-ONION)."""

    @pytest.fixture
    def passive_peer(self):
        """Create a passive peer (taker/watcher)."""
        return PeerInfo(
            nick="passive_taker",
            onion_address="NOT-SERVING-ONION",
            port=5222,  # Port is validated even for passive peers
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )

    @pytest.mark.anyio
    async def test_handle_send_failed_for_passive_peer(self, server, passive_peer):
        """Passive peers (keyed by nick) should be cleaned up correctly."""
        # Register the peer
        server.peer_registry.register(passive_peer)
        # Passive peers use nick as key
        peer_key = passive_peer.nick

        # Create connection mapping
        server.peer_key_to_conn_id[peer_key] = "conn_789"

        # Verify peer is registered
        assert server.peer_registry.get_by_key(peer_key) is not None

        # Trigger send failure cleanup
        await server._handle_send_failed(peer_key)

        # Verify cleanup
        assert peer_key not in server.peer_key_to_conn_id
        assert server.peer_registry.get_by_key(peer_key) is None
        assert server.peer_registry.get_by_nick(passive_peer.nick) is None
