"""
Tests for handshake handler.
"""

import json

import pytest
from jmcore.models import NetworkType
from jmcore.nick_auth import NickAuthMode
from jmcore.protocol import FEATURE_NICK_AUTH, JM_VERSION, JM_VERSION_MIN

from directory_server.handshake_handler import HandshakeError, HandshakeHandler


@pytest.fixture
def handler():
    return HandshakeHandler(
        network=NetworkType.MAINNET, server_nick="test_directory", motd="Test Server"
    )


def test_valid_handshake(handler):
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222",
            "proto-ver": JM_VERSION,
            "features": {},
            "nick": "test_client",
            "network": "mainnet",
        }
    )

    peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

    assert peer_info.nick == "test_client"
    assert peer_info.network == NetworkType.MAINNET
    assert response["accepted"] is True
    assert response["app-name"] == "joinmarket"


def test_handshake_wrong_app_name(handler):
    handshake_data = json.dumps(
        {
            "app-name": "WrongApp",
            "directory": False,
            "location-string": "test.onion:5222",
            "proto-ver": JM_VERSION,
            "features": {},
            "nick": "test",
            "network": "mainnet",
        }
    )

    with pytest.raises(HandshakeError, match="Invalid app name"):
        handler.process_handshake(handshake_data, "127.0.0.1:12345")


def test_handshake_wrong_protocol_version(handler):
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "test.onion:5222",
            "proto-ver": 999,
            "features": {},
            "nick": "test",
            "network": "mainnet",
        }
    )

    with pytest.raises(HandshakeError, match="Protocol version 999 not in supported range"):
        handler.process_handshake(handshake_data, "127.0.0.1:12345")


def test_handshake_protocol_version_too_old(handler):
    """Test that protocol versions below JM_VERSION_MIN are rejected."""
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "test.onion:5222",
            "proto-ver": JM_VERSION_MIN - 1,
            "features": {},
            "nick": "test",
            "network": "mainnet",
        }
    )

    with pytest.raises(HandshakeError, match="Protocol version .* not in supported range"):
        handler.process_handshake(handshake_data, "127.0.0.1:12345")


def test_handshake_protocol_version_min_accepted(handler):
    """Test that the minimum supported protocol version is accepted."""
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222",
            "proto-ver": JM_VERSION_MIN,
            "features": {},
            "nick": "test_client",
            "network": "mainnet",
        }
    )

    peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")
    assert peer_info.nick == "test_client"
    assert response["accepted"] is True


def test_handshake_protocol_version_max_accepted(handler):
    """Test that the maximum supported protocol version is accepted."""
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222",
            "proto-ver": JM_VERSION,
            "features": {},
            "nick": "test_client",
            "network": "mainnet",
        }
    )

    peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")
    assert peer_info.nick == "test_client"
    assert response["accepted"] is True


def test_handshake_wrong_network(handler):
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "test.onion:5222",
            "proto-ver": JM_VERSION,
            "features": {},
            "nick": "test",
            "network": "testnet",
        }
    )

    with pytest.raises(HandshakeError, match="Network mismatch"):
        handler.process_handshake(handshake_data, "127.0.0.1:12345")


def test_handshake_directory_client_rejected(handler):
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": True,
            "location-string": "test.onion:5222",
            "proto-ver": JM_VERSION,
            "features": {},
            "nick": "test",
            "network": "mainnet",
        }
    )

    with pytest.raises(HandshakeError, match="Directory nodes not accepted"):
        handler.process_handshake(handshake_data, "127.0.0.1:12345")


def test_handshake_not_serving(handler):
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "NOT-SERVING-ONION",
            "proto-ver": JM_VERSION,
            "features": {},
            "nick": "test_client",
            "network": "mainnet",
        }
    )

    peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

    assert peer_info.onion_address == "NOT-SERVING-ONION"
    assert peer_info.port == -1


def test_handshake_invalid_json(handler):
    with pytest.raises(HandshakeError, match="Invalid handshake format"):
        handler.process_handshake("not json", "127.0.0.1:12345")


def test_handshake_missing_fields(handler):
    handshake_data = json.dumps({"app-name": "joinmarket", "proto-ver": JM_VERSION})

    with pytest.raises(HandshakeError, match="Missing required"):
        handler.process_handshake(handshake_data, "127.0.0.1:12345")


def test_handshake_lenient_location(handler):
    # Test case where location-string is just a port or invalid format
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "9050",  # Invalid format (legacy/buggy client)
            "proto-ver": JM_VERSION,
            "features": {},
            "nick": "test_client",
            "network": "mainnet",
        }
    )

    peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

    # Should default to NOT-SERVING-ONION instead of raising error
    assert peer_info.onion_address == "NOT-SERVING-ONION"
    assert peer_info.port == -1
    assert response["accepted"] is True


def test_create_rejection_response(handler):
    response = handler.create_rejection_response("Test rejection")

    assert response["accepted"] is False
    assert "Rejected: Test rejection" in response["motd"]


def test_advertises_nick_auth_only_with_enabled_mode_and_identity():
    enabled = HandshakeHandler(
        network=NetworkType.MAINNET,
        server_nick="directory",
        motd="test",
        nick_auth_mode=NickAuthMode.PREFER_VERIFIED,
        nick_auth_directory_id="test:directory",
    )
    missing_identity = HandshakeHandler(
        network=NetworkType.MAINNET,
        server_nick="directory",
        motd="test",
        nick_auth_mode=NickAuthMode.PREFER_VERIFIED,
    )
    disabled = HandshakeHandler(
        network=NetworkType.MAINNET,
        server_nick="directory",
        motd="test",
        nick_auth_mode=NickAuthMode.DISABLED,
        nick_auth_directory_id="test:directory",
    )
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "NOT-SERVING-ONION",
            "proto-ver": JM_VERSION,
            "features": {FEATURE_NICK_AUTH: True},
            "nick": "test_client",
            "network": "mainnet",
        }
    )

    _, enabled_response = enabled.process_handshake(handshake_data, "connection")
    _, missing_response = missing_identity.process_handshake(handshake_data, "connection")
    _, disabled_response = disabled.process_handshake(handshake_data, "connection")

    assert enabled_response["features"][FEATURE_NICK_AUTH] is True
    assert FEATURE_NICK_AUTH not in missing_response["features"]
    assert FEATURE_NICK_AUTH not in disabled_response["features"]


def test_require_verified_rejects_legacy_client_with_ordinary_response():
    handler = HandshakeHandler(
        network=NetworkType.MAINNET,
        server_nick="directory",
        motd="test",
        nick_auth_mode=NickAuthMode.REQUIRE_VERIFIED,
        nick_auth_directory_id="test:directory",
    )
    handshake_data = json.dumps(
        {
            "app-name": "joinmarket",
            "directory": False,
            "location-string": "NOT-SERVING-ONION",
            "proto-ver": JM_VERSION,
            "features": {},
            "nick": "legacy_client",
            "network": "mainnet",
        }
    )

    _, response = handler.process_handshake(handshake_data, "connection")

    assert response["accepted"] is False
    assert response["features"][FEATURE_NICK_AUTH] is True


class TestVersionNegotiation:
    """Tests for protocol version negotiation during handshake.

    Note: With the feature-flag approach, we only support v5 for reference
    compatibility. Features like neutrino_compat are negotiated separately.
    """

    @pytest.fixture
    def handler(self):
        return HandshakeHandler(
            network=NetworkType.MAINNET, server_nick="test_directory", motd="Test Server"
        )

    def test_v5_client_negotiates_v5(self, handler):
        """A v5 client connecting to v5 server should negotiate v5."""
        handshake_data = json.dumps(
            {
                "app-name": "joinmarket",
                "directory": False,
                "location-string": "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222",
                "proto-ver": 5,  # Client is v5
                "features": {},
                "nick": "v5_client",
                "network": "mainnet",
            }
        )

        peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

        # Should negotiate v5
        assert peer_info.protocol_version == 5
        assert peer_info.neutrino_compat is False

    def test_v5_client_with_neutrino_compat_feature(self, handler):
        """A v5 client with neutrino_compat feature should have it recorded."""
        handshake_data = json.dumps(
            {
                "app-name": "joinmarket",
                "directory": False,
                "location-string": "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222",
                "proto-ver": 5,
                "features": {"neutrino_compat": True},
                "nick": "neutrino_client",
                "network": "mainnet",
            }
        )

        peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

        assert peer_info.protocol_version == 5
        # Feature-based: neutrino_compat is independent of version
        assert peer_info.neutrino_compat is True

    def test_v5_client_without_neutrino_compat(self, handler):
        """A v5 client without neutrino_compat should have it as False."""
        handshake_data = json.dumps(
            {
                "app-name": "joinmarket",
                "directory": False,
                "location-string": "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222",
                "proto-ver": 5,
                "features": {},
                "nick": "legacy_client",
                "network": "mainnet",
            }
        )

        peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

        assert peer_info.protocol_version == 5
        assert peer_info.neutrino_compat is False


class TestNeutrinoCompatServer:
    """Tests for directory server with neutrino_compat enabled."""

    @pytest.fixture
    def neutrino_handler(self):
        return HandshakeHandler(
            network=NetworkType.MAINNET,
            server_nick="neutrino_directory",
            motd="Neutrino Server",
            neutrino_compat=True,
        )

    def test_server_advertises_neutrino_compat(self, neutrino_handler):
        """Server with neutrino_compat should advertise it in response."""
        handshake_data = json.dumps(
            {
                "app-name": "joinmarket",
                "directory": False,
                "location-string": "NOT-SERVING-ONION",
                "proto-ver": 5,
                "features": {},
                "nick": "test_client",
                "network": "mainnet",
            }
        )

        peer_info, response = neutrino_handler.process_handshake(handshake_data, "127.0.0.1:12345")

        assert response["features"]["neutrino_compat"] is True

    def test_server_without_neutrino_compat(self, handler):
        """Server without neutrino_compat should not advertise it."""
        handshake_data = json.dumps(
            {
                "app-name": "joinmarket",
                "directory": False,
                "location-string": "NOT-SERVING-ONION",
                "proto-ver": 5,
                "features": {},
                "nick": "test_client",
                "network": "mainnet",
            }
        )

        peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

        # No neutrino_compat in features
        assert response.get("features", {}).get("neutrino_compat") is not True


class TestPeerInfoVersionSupport:
    """Tests for PeerInfo feature-related methods.

    With feature-based approach, supports_extended_utxo is based solely
    on the neutrino_compat flag, not the protocol version.
    """

    def test_supports_extended_utxo_with_neutrino_compat(self):
        """PeerInfo with neutrino_compat should support extended UTXO."""
        from jmcore.models import PeerInfo, PeerStatus

        peer = PeerInfo(
            nick="test",
            onion_address="NOT-SERVING-ONION",
            port=-1,
            status=PeerStatus.CONNECTED,
            protocol_version=5,
            neutrino_compat=True,
        )

        assert peer.supports_extended_utxo() is True

    def test_not_supports_extended_utxo_without_neutrino_compat(self):
        """PeerInfo without neutrino_compat should not support extended UTXO."""
        from jmcore.models import PeerInfo, PeerStatus

        peer = PeerInfo(
            nick="test",
            onion_address="NOT-SERVING-ONION",
            port=-1,
            status=PeerStatus.CONNECTED,
            protocol_version=5,
            neutrino_compat=False,
        )

        assert peer.supports_extended_utxo() is False


class TestPingFeatureNegotiation:
    """Tests for ping feature negotiation during handshake."""

    @pytest.fixture
    def handler(self):
        return HandshakeHandler(
            network=NetworkType.MAINNET, server_nick="test_directory", motd="Test Server"
        )

    def test_server_response_includes_ping_feature(self, handler):
        """Server handshake response should always advertise ping feature."""
        handshake_data = json.dumps(
            {
                "app-name": "joinmarket",
                "directory": False,
                "location-string": "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222",
                "proto-ver": JM_VERSION,
                "features": {},
                "nick": "test_client",
                "network": "mainnet",
            }
        )

        _peer_info, response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

        assert response["features"]["ping"] is True

    def test_client_ping_feature_stored_in_peer_info(self, handler):
        """Client advertising ping feature should have it stored in PeerInfo."""
        handshake_data = json.dumps(
            {
                "app-name": "joinmarket",
                "directory": False,
                "location-string": "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222",
                "proto-ver": JM_VERSION,
                "features": {"ping": True},
                "nick": "ping_client",
                "network": "mainnet",
            }
        )

        peer_info, _response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

        assert peer_info.features.get("ping") is True

    def test_client_without_ping_feature(self, handler):
        """Client not advertising ping feature should not have it in PeerInfo."""
        handshake_data = json.dumps(
            {
                "app-name": "joinmarket",
                "directory": False,
                "location-string": "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222",
                "proto-ver": JM_VERSION,
                "features": {},
                "nick": "legacy_client",
                "network": "mainnet",
            }
        )

        peer_info, _response = handler.process_handshake(handshake_data, "127.0.0.1:12345")

        assert peer_info.features.get("ping") is not True
