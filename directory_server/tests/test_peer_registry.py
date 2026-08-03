"""
Tests for peer registry.
"""

import pytest
from jmcore.models import NetworkType, PeerInfo, PeerStatus

from directory_server.peer_registry import PeerOwnershipConflictError, PeerRegistry


@pytest.fixture
def registry():
    return PeerRegistry(max_peers=10)


@pytest.fixture
def sample_peer():
    return PeerInfo(
        nick="test_peer",
        onion_address="abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion",
        port=5222,
        network=NetworkType.MAINNET,
    )


def test_register_peer(registry, sample_peer):
    registry.register(sample_peer)

    assert registry.count() == 1
    retrieved = registry.get_by_nick("test_peer")
    assert retrieved is not None
    assert retrieved.nick == "test_peer"


def test_register_duplicate_nick(registry, sample_peer):
    registry.register(sample_peer, "first")

    peer2 = PeerInfo(
        nick="test_peer",
        onion_address="abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvw2.onion",
        port=5222,
    )
    with pytest.raises(PeerOwnershipConflictError):
        registry.register(peer2, "second")

    assert registry.count() == 1
    assert registry.get_connection_id(sample_peer.nick) == "first"


def test_verified_peer_atomically_displaces_legacy_owner(registry, sample_peer):
    first = registry.register(sample_peer, "legacy")
    replacement = sample_peer.model_copy(deep=True)

    result = registry.register(replacement, "verified", verified_pubkey=b"full-pubkey")

    assert result.peer_key == first.peer_key
    assert result.displaced[0].connection_id == "legacy"
    assert registry.get_by_key(first.peer_key) is replacement
    assert registry.get_connection_id(first.peer_key) == "verified"


def test_same_verified_pubkey_reconnect_replaces_owner(registry, sample_peer):
    key = registry.register(sample_peer, "first", verified_pubkey=b"same-key").peer_key
    replacement = sample_peer.model_copy(
        update={"onion_address": f"{'b' * 56}.onion"},
        deep=True,
    )

    result = registry.register(replacement, "second", verified_pubkey=b"same-key")

    assert result.displaced[0].connection_id == "first"
    assert registry.get_connection_id(key) == "second"
    assert registry.get_by_key(key) is replacement


def test_different_verified_pubkey_for_same_nick_is_rejected(registry, sample_peer):
    key = registry.register(sample_peer, "first", verified_pubkey=b"first-key").peer_key

    with pytest.raises(PeerOwnershipConflictError):
        registry.register(
            sample_peer.model_copy(deep=True), "second", verified_pubkey=b"different-key"
        )

    assert registry.get_connection_id(key) == "first"


@pytest.mark.parametrize("verified_pubkey", [None, b"second-key"])
def test_different_nicks_can_share_location(registry, sample_peer, verified_pubkey):
    first = registry.register(sample_peer, "first")
    second_peer = sample_peer.model_copy(update={"nick": "second-peer"})

    second = registry.register(second_peer, "second", verified_pubkey=verified_pubkey)

    assert first.peer_key == sample_peer.nick
    assert second.peer_key == second_peer.nick
    assert second.displaced == ()
    assert registry.count() == 2
    assert registry.get_by_nick(sample_peer.nick) is sample_peer
    assert registry.get_by_nick(second_peer.nick) is second_peer

    registry.update_status(first.peer_key, PeerStatus.HANDSHAKED, "first")
    registry.update_status(second.peer_key, PeerStatus.HANDSHAKED, "second")
    assert set(registry.get_peerlist_for_network(NetworkType.MAINNET)) == {
        (sample_peer.nick, sample_peer.location_string),
        (second_peer.nick, second_peer.location_string),
    }


def test_stale_generation_cannot_mutate_or_unregister_owner(registry, sample_peer):
    key = registry.register(sample_peer, "old", verified_pubkey=b"same-key").peer_key
    registry.register(sample_peer.model_copy(deep=True), "new", verified_pubkey=b"same-key")
    current = registry.get_by_key(key)
    assert current is not None
    last_seen = current.last_seen

    assert registry.update_status(key, PeerStatus.DISCONNECTED, "old") is False
    assert registry.update_last_seen(key, "old") is False
    assert registry.unregister(key, "old") is False

    assert registry.get_by_key(key) is current
    assert current.status != PeerStatus.DISCONNECTED
    assert current.last_seen == last_seen


def test_max_peers_limit(registry):
    for i in range(10):
        peer = PeerInfo(nick=f"peer{i}", onion_address=f"{'a' * 56}.onion", port=5222 + i)
        registry.register(peer)

    assert registry.count() == 10

    with pytest.raises(ValueError, match="Maximum peers reached"):
        extra_peer = PeerInfo(nick="extra", onion_address=f"{'b' * 56}.onion", port=6000)
        registry.register(extra_peer)


def test_unregister_peer(registry, sample_peer):
    registry.register(sample_peer)

    registry.unregister(sample_peer.nick)

    assert registry.count() == 0
    assert registry.get_by_nick("test_peer") is None


def test_update_status(registry, sample_peer):
    registry.register(sample_peer)

    registry.update_status(sample_peer.nick, PeerStatus.HANDSHAKED)

    peer = registry.get_by_nick(sample_peer.nick)
    assert peer.status == PeerStatus.HANDSHAKED


def test_get_all_connected(registry):
    for i in range(3):
        peer = PeerInfo(
            nick=f"peer{i}",
            onion_address=f"{'a' * 56}.onion",
            port=5220 + i,
            network=NetworkType.MAINNET,
        )
        registry.register(peer)
        registry.update_status(peer.nick, PeerStatus.HANDSHAKED)

    connected = registry.get_all_connected(NetworkType.MAINNET)
    assert len(connected) == 3


def test_get_all_connected_filters_network(registry):
    mainnet_peer = PeerInfo(
        nick="mainnet", onion_address=f"{'a' * 56}.onion", port=5222, network=NetworkType.MAINNET
    )
    testnet_peer = PeerInfo(
        nick="testnet", onion_address=f"{'b' * 56}.onion", port=5222, network=NetworkType.TESTNET
    )

    registry.register(mainnet_peer)
    registry.register(testnet_peer)
    registry.update_status(mainnet_peer.nick, PeerStatus.HANDSHAKED)
    registry.update_status(testnet_peer.nick, PeerStatus.HANDSHAKED)

    mainnet_peers = registry.get_all_connected(NetworkType.MAINNET)
    assert len(mainnet_peers) == 1
    assert mainnet_peers[0].nick == "mainnet"


def test_get_peerlist_for_network(registry):
    peer = PeerInfo(
        nick="peer1", onion_address=f"{'a' * 56}.onion", port=5222, network=NetworkType.MAINNET
    )
    registry.register(peer)
    registry.update_status(peer.nick, PeerStatus.HANDSHAKED)

    peerlist = registry.get_peerlist_for_network(NetworkType.MAINNET)
    assert len(peerlist) == 1
    assert peerlist[0] == ("peer1", peer.location_string)


def test_get_peerlist_includes_not_serving_onion(registry):
    """Test that NOT-SERVING-ONION peers are included in peerlist on all networks."""
    passive_peer = PeerInfo(
        nick="taker1",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    active_peer = PeerInfo(
        nick="maker1",
        onion_address=f"{'a' * 56}.onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )

    registry.register(passive_peer)
    registry.register(active_peer)

    # Peerlist should include both - even NOT-SERVING-ONION peers are useful
    # as they can be reached via the directory for private messages
    peerlist = registry.get_peerlist_for_network(NetworkType.MAINNET)
    assert len(peerlist) == 2
    nicks = {p[0] for p in peerlist}
    assert nicks == {"taker1", "maker1"}


def test_get_peerlist_with_features_includes_not_serving_onion(registry):
    """Test that NOT-SERVING-ONION peers are included in peerlist with features."""
    passive_peer = PeerInfo(
        nick="taker1",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
        features={"peerlist_features": True},
    )
    active_peer = PeerInfo(
        nick="maker1",
        onion_address=f"{'a' * 56}.onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
        features={"peerlist_features": True, "neutrino_compat": True},
    )

    registry.register(passive_peer)
    registry.register(active_peer)

    # Peerlist should include both types
    peerlist = registry.get_peerlist_with_features(NetworkType.MAINNET)
    assert len(peerlist) == 2
    nicks = {p[0] for p in peerlist}
    assert nicks == {"taker1", "maker1"}
    # Verify both have features
    for _nick, _location, features in peerlist:
        assert "peerlist_features" in features.features


def test_clear(registry, sample_peer):
    registry.register(sample_peer)
    registry.clear()

    assert registry.count() == 0
    assert registry.get_by_nick("test_peer") is None


def test_get_passive_peers(registry):
    passive_peer1 = PeerInfo(
        nick="taker1",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    passive_peer2 = PeerInfo(
        nick="taker2",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    active_peer = PeerInfo(
        nick="maker1",
        onion_address=f"{'a' * 56}.onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )

    registry.register(passive_peer1)
    registry.register(passive_peer2)
    registry.register(active_peer)

    passive_peers = registry.get_passive_peers()
    assert len(passive_peers) == 2
    assert all(p.onion_address == "NOT-SERVING-ONION" for p in passive_peers)
    assert "taker1" in [p.nick for p in passive_peers]
    assert "taker2" in [p.nick for p in passive_peers]


def test_get_active_peers(registry):
    passive_peer = PeerInfo(
        nick="taker1",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    active_peer1 = PeerInfo(
        nick="maker1",
        onion_address=f"{'a' * 56}.onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    active_peer2 = PeerInfo(
        nick="maker2",
        onion_address=f"{'b' * 56}.onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )

    registry.register(passive_peer)
    registry.register(active_peer1)
    registry.register(active_peer2)

    active_peers = registry.get_active_peers()
    assert len(active_peers) == 2
    assert all(p.onion_address != "NOT-SERVING-ONION" for p in active_peers)
    assert "maker1" in [p.nick for p in active_peers]
    assert "maker2" in [p.nick for p in active_peers]


def test_get_passive_peers_filters_network(registry):
    mainnet_passive = PeerInfo(
        nick="taker1",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    testnet_passive = PeerInfo(
        nick="taker2",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.TESTNET,
        status=PeerStatus.HANDSHAKED,
    )

    registry.register(mainnet_passive)
    registry.register(testnet_passive)

    mainnet_peers = registry.get_passive_peers(NetworkType.MAINNET)
    assert len(mainnet_peers) == 1
    assert mainnet_peers[0].nick == "taker1"


def test_get_active_peers_filters_network(registry):
    mainnet_active = PeerInfo(
        nick="maker1",
        onion_address=f"{'a' * 56}.onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    testnet_active = PeerInfo(
        nick="maker2",
        onion_address=f"{'b' * 56}.onion",
        port=5222,
        network=NetworkType.TESTNET,
        status=PeerStatus.HANDSHAKED,
    )

    registry.register(mainnet_active)
    registry.register(testnet_active)

    mainnet_peers = registry.get_active_peers(NetworkType.MAINNET)
    assert len(mainnet_peers) == 1
    assert mainnet_peers[0].nick == "maker1"


def test_get_stats_includes_passive_and_active(registry):
    passive_peer = PeerInfo(
        nick="taker1",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )
    active_peer = PeerInfo(
        nick="maker1",
        onion_address=f"{'a' * 56}.onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
    )

    registry.register(passive_peer)
    registry.register(active_peer)

    stats = registry.get_stats()
    assert stats["total_peers"] == 2
    assert stats["connected_peers"] == 2
    assert stats["passive_peers"] == 1
    assert stats["active_peers"] == 1


def test_passive_peers_exclude_directories(registry):
    passive_peer = PeerInfo(
        nick="taker1",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
        is_directory=False,
    )
    directory_peer = PeerInfo(
        nick="directory",
        onion_address="NOT-SERVING-ONION",
        port=-1,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
        is_directory=True,
    )

    registry.register(passive_peer)
    registry.register(directory_peer)

    passive_peers = registry.get_passive_peers()
    assert len(passive_peers) == 1
    assert passive_peers[0].nick == "taker1"


def test_active_peers_exclude_directories(registry):
    active_peer = PeerInfo(
        nick="maker1",
        onion_address=f"{'a' * 56}.onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
        is_directory=False,
    )
    directory_peer = PeerInfo(
        nick="directory",
        onion_address=f"{'b' * 56}.onion",
        port=5222,
        network=NetworkType.MAINNET,
        status=PeerStatus.HANDSHAKED,
        is_directory=True,
    )

    registry.register(active_peer)
    registry.register(directory_peer)

    active_peers = registry.get_active_peers()
    assert len(active_peers) == 1
    assert active_peers[0].nick == "maker1"


# ---------------------------------------------------------------------------
# Heartbeat-related methods
# ---------------------------------------------------------------------------


class TestUpdateLastSeen:
    """Tests for update_last_seen()."""

    def test_updates_timestamp(self, registry):
        from datetime import UTC, datetime, timedelta

        peer = PeerInfo(
            nick="maker1",
            onion_address=f"{'a' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
        )
        registry.register(peer)
        key = peer.nick

        # Backdate last_seen
        p = registry.get_by_key(key)
        old_time = datetime.now(UTC) - timedelta(hours=1)
        p.last_seen = old_time

        registry.update_last_seen(key)

        assert p.last_seen > old_time

    def test_noop_for_unknown_key(self, registry):
        # Should not raise for non-existent key
        registry.update_last_seen("nonexistent_key")


class TestGetPeersIdleSince:
    """Tests for get_peers_idle_since()."""

    def test_returns_peers_older_than_cutoff(self, registry):
        from datetime import UTC, datetime, timedelta

        old_time = datetime.now(UTC) - timedelta(minutes=20)
        recent_time = datetime.now(UTC) - timedelta(seconds=30)

        old_peer = PeerInfo(
            nick="old_maker",
            onion_address=f"{'a' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(old_peer)
        old_peer.last_seen = old_time

        recent_peer = PeerInfo(
            nick="recent_maker",
            onion_address=f"{'b' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(recent_peer)
        recent_peer.last_seen = recent_time

        cutoff = datetime.now(UTC) - timedelta(minutes=10)
        idle = registry.get_peers_idle_since(cutoff)

        assert len(idle) == 1
        assert idle[0][1].nick == "old_maker"

    def test_excludes_non_handshaked(self, registry):
        from datetime import UTC, datetime, timedelta

        old_time = datetime.now(UTC) - timedelta(minutes=20)

        peer = PeerInfo(
            nick="connecting_peer",
            onion_address=f"{'a' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.CONNECTED,
        )
        registry.register(peer)
        peer.last_seen = old_time

        cutoff = datetime.now(UTC) - timedelta(minutes=10)
        idle = registry.get_peers_idle_since(cutoff)

        assert len(idle) == 0

    def test_excludes_directories(self, registry):
        from datetime import UTC, datetime, timedelta

        old_time = datetime.now(UTC) - timedelta(minutes=20)

        dir_peer = PeerInfo(
            nick="directory_node",
            onion_address=f"{'a' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
            is_directory=True,
        )
        registry.register(dir_peer)
        dir_peer.last_seen = old_time

        cutoff = datetime.now(UTC) - timedelta(minutes=10)
        idle = registry.get_peers_idle_since(cutoff)

        assert len(idle) == 0

    def test_excludes_peers_without_last_seen(self, registry):
        from datetime import UTC, datetime, timedelta

        peer = PeerInfo(
            nick="no_seen_peer",
            onion_address=f"{'a' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
            status=PeerStatus.HANDSHAKED,
        )
        registry.register(peer)
        # register() sets last_seen, so clear it
        peer.last_seen = None

        cutoff = datetime.now(UTC) - timedelta(minutes=10)
        idle = registry.get_peers_idle_since(cutoff)

        assert len(idle) == 0


class TestSupportsPing:
    """Tests for supports_ping()."""

    def test_peer_with_ping_feature(self, registry):
        peer = PeerInfo(
            nick="ping_maker",
            onion_address=f"{'a' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
            features={"ping": True},
        )
        registry.register(peer)
        key = peer.nick

        assert registry.supports_ping(key) is True

    def test_peer_without_ping_feature(self, registry):
        peer = PeerInfo(
            nick="legacy_maker",
            onion_address=f"{'a' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
            features={},
        )
        registry.register(peer)
        key = peer.nick

        assert registry.supports_ping(key) is False

    def test_peer_with_ping_false(self, registry):
        peer = PeerInfo(
            nick="ping_off_maker",
            onion_address=f"{'a' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
            features={"ping": False},
        )
        registry.register(peer)
        key = peer.nick

        assert registry.supports_ping(key) is False

    def test_unknown_key(self, registry):
        assert registry.supports_ping("nonexistent") is False


class TestIsMaker:
    """Tests for is_maker()."""

    def test_peer_with_onion_address(self, registry):
        peer = PeerInfo(
            nick="real_maker",
            onion_address=f"{'a' * 56}.onion",
            port=5222,
            network=NetworkType.MAINNET,
        )
        registry.register(peer)
        key = peer.nick

        assert registry.is_maker(key) is True

    def test_peer_not_serving_onion(self, registry):
        peer = PeerInfo(
            nick="taker_nick",
            onion_address="NOT-SERVING-ONION",
            port=-1,
            network=NetworkType.MAINNET,
        )
        registry.register(peer)
        # NOT-SERVING-ONION peers are keyed by nick
        key = peer.nick

        assert registry.is_maker(key) is False

    def test_unknown_key(self, registry):
        assert registry.is_maker("nonexistent") is False
