"""
Tests for jmcore.protocol
"""

import pytest

from jmcore.protocol import (
    FEATURE_NEUTRINO_COMPAT,
    FEATURE_PEERLIST_FEATURES,
    FEATURE_PUSH_ENCRYPTED,
    JM_VERSION,
    JM_VERSION_MIN,
    NOT_SERVING_ONION_HOSTNAME,
    FeatureSet,
    MessageType,
    ProtocolMessage,
    RequiredFeatures,
    UTXOMetadata,
    create_handshake_request,
    create_handshake_response,
    create_peerlist_entry,
    format_jm_message,
    format_utxo_list,
    get_nick_version,
    parse_jm_message,
    parse_peer_location,
    parse_peerlist_entry,
    parse_utxo_list,
    peer_supports_neutrino_compat,
)


def test_protocol_message_serialization():
    msg = ProtocolMessage(type=MessageType.HANDSHAKE, payload={"test": "data"})
    json_str = msg.to_json()
    assert "793" in json_str or '"type": 793' in json_str

    restored = ProtocolMessage.from_json(json_str)
    assert restored.type == MessageType.HANDSHAKE
    assert restored.payload == {"test": "data"}


def test_parse_peer_location_valid():
    host, port = parse_peer_location("test.onion:5222")
    assert host == "test.onion"
    assert port == 5222


def test_parse_peer_location_not_serving():
    host, port = parse_peer_location(NOT_SERVING_ONION_HOSTNAME)
    assert host == NOT_SERVING_ONION_HOSTNAME
    assert port == -1


def test_parse_peer_location_invalid():
    with pytest.raises(ValueError):
        parse_peer_location("invalid")

    with pytest.raises(ValueError):
        parse_peer_location("test.onion:99999")


def test_peerlist_entry_creation():
    entry = create_peerlist_entry("nick1", "test.onion:5222", disconnected=False)
    assert entry == "nick1;test.onion:5222"

    entry_disco = create_peerlist_entry("nick2", "test.onion:5222", disconnected=True)
    assert entry_disco == "nick2;test.onion:5222;D"


def test_peerlist_entry_parsing():
    nick, location, disco, features = parse_peerlist_entry("nick1;test.onion:5222")
    assert nick == "nick1"
    assert location == "test.onion:5222"
    assert not disco
    assert len(features) == 0

    nick, location, disco, features = parse_peerlist_entry("nick2;test.onion:5222;D")
    assert nick == "nick2"
    assert disco
    assert len(features) == 0


def test_jm_message_formatting():
    msg = format_jm_message("alice", "bob", "fill", "12345 100 pubkey")
    assert msg == "alice!bob!fill 12345 100 pubkey"


def test_jm_message_parsing():
    result = parse_jm_message("alice!bob!fill 12345")
    assert result is not None
    from_nick, to_nick, rest = result
    assert from_nick == "alice"
    assert to_nick == "bob"
    assert rest == "fill 12345"


def test_jm_message_public():
    result = parse_jm_message("alice!PUBLIC!absorder 12345")
    assert result is not None
    from_nick, to_nick, rest = result
    assert from_nick == "alice"
    assert to_nick == "PUBLIC"


# ==============================================================================
# Extended Format Tests - UTXO Metadata (neutrino_compat feature)
# ==============================================================================


class TestUTXOMetadata:
    """Tests for UTXOMetadata class."""

    def test_legacy_format_parse(self):
        """Parse legacy txid:vout format."""
        txid = "a" * 64
        utxo = UTXOMetadata.from_str(f"{txid}:0")
        assert utxo.txid == txid
        assert utxo.vout == 0
        assert utxo.scriptpubkey is None
        assert utxo.blockheight is None
        assert not utxo.has_neutrino_metadata()

    def test_extended_format_parse(self):
        """Parse extended txid:vout:scriptpubkey:blockheight format."""
        txid = "b" * 64
        utxo = UTXOMetadata.from_str(f"{txid}:1:0014a1b2c3d4e5f6:750000")
        assert utxo.txid == txid
        assert utxo.vout == 1
        assert utxo.scriptpubkey == "0014a1b2c3d4e5f6"
        assert utxo.blockheight == 750000
        assert utxo.has_neutrino_metadata()

    def test_legacy_format_output(self):
        """Output legacy format."""
        txid = "c" * 64
        utxo = UTXOMetadata(txid=txid, vout=2)
        assert utxo.to_legacy_str() == f"{txid}:2"

    def test_extended_format_output(self):
        """Output extended format."""
        txid = "d" * 64
        utxo = UTXOMetadata(txid=txid, vout=2, scriptpubkey="0014deadbeef", blockheight=800000)
        assert utxo.to_extended_str() == f"{txid}:2:0014deadbeef:800000"

    def test_extended_format_fallback_to_legacy(self):
        """Extended format falls back to legacy when metadata missing."""
        txid = "e" * 64
        utxo = UTXOMetadata(txid=txid, vout=2)
        assert utxo.to_extended_str() == f"{txid}:2"

        utxo_partial = UTXOMetadata(txid=txid, vout=2, scriptpubkey="0014deadbeef")
        assert utxo_partial.to_extended_str() == f"{txid}:2"

    def test_invalid_format_raises(self):
        """Invalid formats raise ValueError."""
        with pytest.raises(ValueError):
            UTXOMetadata.from_str("invalid")

        txid = "f" * 64
        with pytest.raises(ValueError):
            UTXOMetadata.from_str(f"{txid}:1:2")  # 3 parts

        with pytest.raises(ValueError):
            UTXOMetadata.from_str(f"{txid}:1:2:3:4")  # 5 parts

    def test_strict_validation_failures(self):
        """Test validation failures during construction/parsing."""
        # TXID length failure
        with pytest.raises(ValueError, match="Invalid TXID length"):
            UTXOMetadata(txid="abc", vout=0)

        # TXID hex failure
        txid_bad_hex = "g" * 64
        with pytest.raises(ValueError, match="Invalid TXID hex"):
            UTXOMetadata(txid=txid_bad_hex, vout=0)

        # Vout negative failure
        txid_valid = "a" * 64
        with pytest.raises(ValueError, match="non-negative"):
            UTXOMetadata(txid=txid_valid, vout=-1)

        # Vout overflow failure (uint32 max 4294967295)
        with pytest.raises(ValueError, match="4294967295"):
            UTXOMetadata(txid=txid_valid, vout=2**32)

    def test_scriptpubkey_validation(self):
        """Validate scriptPubKey format."""
        # Valid P2WPKH (22 bytes = 44 hex chars)
        assert UTXOMetadata.is_valid_scriptpubkey("0014" + "a" * 40)

        # Valid P2WSH (34 bytes = 68 hex chars)
        assert UTXOMetadata.is_valid_scriptpubkey("0020" + "b" * 64)

        # Invalid: not hex
        assert not UTXOMetadata.is_valid_scriptpubkey("0014xyz123")

        # Invalid: too short
        assert not UTXOMetadata.is_valid_scriptpubkey("00")

        # Invalid: empty
        assert not UTXOMetadata.is_valid_scriptpubkey("")

    def test_roundtrip_legacy(self):
        """Round-trip legacy format."""
        txid = "0" * 64
        original = f"{txid}:5"
        utxo = UTXOMetadata.from_str(original)
        assert utxo.to_legacy_str() == original

    def test_roundtrip_extended(self):
        """Round-trip extended format."""
        txid = "1" * 64
        original = f"{txid}:5:0014deadbeef1234:850000"
        utxo = UTXOMetadata.from_str(original)
        assert utxo.to_extended_str() == original


class TestParseUtxoList:
    """Tests for parse_utxo_list function."""

    def test_empty_string(self):
        """Empty string returns empty list."""
        assert parse_utxo_list("") == []

    def test_single_legacy_utxo(self):
        """Parse single legacy UTXO."""
        txid = "2" * 64
        utxos = parse_utxo_list(f"{txid}:0")
        assert len(utxos) == 1
        assert utxos[0].txid == txid
        assert utxos[0].vout == 0

    def test_multiple_legacy_utxos(self):
        """Parse multiple legacy UTXOs."""
        txid1 = "3" * 64
        txid2 = "4" * 64
        txid3 = "5" * 64
        utxos = parse_utxo_list(f"{txid1}:0,{txid2}:1,{txid3}:2")
        assert len(utxos) == 3
        assert utxos[1].txid == txid2
        assert utxos[2].vout == 2

    def test_multiple_extended_utxos(self):
        """Parse multiple extended UTXOs."""
        txid1 = "6" * 64
        txid2 = "7" * 64
        txid3 = "8" * 64
        utxos = parse_utxo_list(
            f"{txid1}:0:0014aaa:100,{txid2}:1:0014bbb:200,{txid3}:2:0014ccc:300"
        )
        assert len(utxos) == 3
        assert all(u.has_neutrino_metadata() for u in utxos)
        assert utxos[0].blockheight == 100
        assert utxos[2].scriptpubkey == "0014ccc"

    def test_mixed_formats(self):
        """Parse mixed legacy and extended UTXOs."""
        txid1 = "9" * 64
        txid2 = "a" * 64
        utxos = parse_utxo_list(f"{txid1}:0,{txid2}:1:0014bbb:200")
        assert len(utxos) == 2
        assert not utxos[0].has_neutrino_metadata()
        assert utxos[1].has_neutrino_metadata()

    def test_require_metadata_success(self):
        """require_metadata=True succeeds when all have metadata."""
        txid1 = "b" * 64
        txid2 = "c" * 64
        utxos = parse_utxo_list(
            f"{txid1}:0:0014aaa:100,{txid2}:1:0014bbb:200", require_metadata=True
        )
        assert len(utxos) == 2

    def test_require_metadata_failure(self):
        """require_metadata=True raises when metadata missing."""
        txid1 = "d" * 64
        txid2 = "e" * 64
        with pytest.raises(ValueError, match="missing Neutrino metadata"):
            parse_utxo_list(f"{txid1}:0,{txid2}:1:0014bbb:200", require_metadata=True)


class TestFormatUtxoList:
    """Tests for format_utxo_list function."""

    def test_format_legacy(self):
        """Format UTXOs in legacy format."""
        txid1 = "f" * 64
        txid2 = "0" * 64
        utxos = [
            UTXOMetadata(txid=txid1, vout=0, scriptpubkey="0014aaa", blockheight=100),
            UTXOMetadata(txid=txid2, vout=1, scriptpubkey="0014bbb", blockheight=200),
        ]
        result = format_utxo_list(utxos, extended=False)
        assert result == f"{txid1}:0,{txid2}:1"

    def test_format_extended(self):
        """Format UTXOs in extended format."""
        txid1 = "f" * 64
        txid2 = "0" * 64
        utxos = [
            UTXOMetadata(txid=txid1, vout=0, scriptpubkey="0014aaa", blockheight=100),
            UTXOMetadata(txid=txid2, vout=1, scriptpubkey="0014bbb", blockheight=200),
        ]
        result = format_utxo_list(utxos, extended=True)
        assert result == f"{txid1}:0:0014aaa:100,{txid2}:1:0014bbb:200"


# ==============================================================================
# Feature Negotiation Tests - Handshake and neutrino_compat
# ==============================================================================


class TestProtocolVersion:
    """Tests for protocol version constants."""

    def test_version_numbers(self):
        """Verify version constants - v5 for reference compatibility."""
        assert JM_VERSION == 5
        assert JM_VERSION_MIN == 5  # min == max since we only support v5

    def test_feature_flag_constant(self):
        """Verify feature flag constant."""
        assert FEATURE_NEUTRINO_COMPAT == "neutrino_compat"


class TestHandshakeRequest:
    """Tests for create_handshake_request function."""

    def test_basic_handshake(self):
        """Create basic handshake without neutrino_compat."""
        hs = create_handshake_request(
            nick="J5TestNick", location="test.onion:5222", network="mainnet"
        )
        assert hs["nick"] == "J5TestNick"
        assert hs["proto-ver"] == 5  # v5 for reference compatibility
        assert hs["features"] == {}
        assert hs["directory"] is False

    def test_handshake_with_neutrino_compat(self):
        """Create handshake with neutrino_compat feature."""
        hs = create_handshake_request(
            nick="J5TestNick",
            location="test.onion:5222",
            network="mainnet",
            neutrino_compat=True,
        )
        assert hs["features"][FEATURE_NEUTRINO_COMPAT] is True

    def test_directory_handshake(self):
        """Create directory server handshake."""
        hs = create_handshake_request(
            nick="J5DirServer",
            location="dir.onion:5222",
            network="mainnet",
            directory=True,
        )
        assert hs["directory"] is True


class TestHandshakeResponse:
    """Tests for create_handshake_response function."""

    def test_basic_response(self):
        """Create basic handshake response."""
        hs = create_handshake_response(nick="J5DirServer", network="mainnet")
        assert hs["proto-ver-min"] == 5
        assert hs["proto-ver-max"] == 5  # min == max since we only support v5
        assert hs["accepted"] is True
        assert hs["features"] == {}

    def test_response_with_neutrino_compat(self):
        """Create response with neutrino_compat feature."""
        hs = create_handshake_response(nick="J5DirServer", network="mainnet", neutrino_compat=True)
        assert hs["features"][FEATURE_NEUTRINO_COMPAT] is True


class TestPeerSupportsNeutrinoCompat:
    """Tests for peer_supports_neutrino_compat function."""

    def test_v5_peer_no_support(self):
        """v5 peer does not support neutrino_compat."""
        handshake = {"proto-ver": 5, "features": {}}
        assert peer_supports_neutrino_compat(handshake) is False

    def test_v5_peer_with_feature(self):
        """v5 peer with feature flag supports neutrino_compat."""
        handshake = {"proto-ver": 5, "features": {FEATURE_NEUTRINO_COMPAT: True}}
        assert peer_supports_neutrino_compat(handshake) is True

    def test_peer_without_feature(self):
        """Peer without feature flag does not support."""
        handshake = {"proto-ver": 5, "features": {}}
        assert peer_supports_neutrino_compat(handshake) is False

    def test_missing_features_key(self):
        """Handle missing features key gracefully."""
        handshake = {"proto-ver": 5}
        assert peer_supports_neutrino_compat(handshake) is False

    def test_missing_proto_ver_with_feature(self):
        """Feature detection works even without proto-ver."""
        handshake = {"features": {FEATURE_NEUTRINO_COMPAT: True}}
        assert peer_supports_neutrino_compat(handshake) is True


class TestNickVersionDetection:
    """Tests for nick-based version detection functions.

    NOTE: Nick version detection is reserved for potential future reference compatibility.
    Feature detection (like neutrino_compat) uses handshake features, not nick versions.
    """

    def test_get_nick_version_v5(self):
        """Detect version 5 from J5 nick."""
        assert get_nick_version("J5abc123defOOOO") == 5

    def test_get_nick_version_v6(self):
        """Detect hypothetical future version from nick (reserved for future compat)."""
        assert get_nick_version("J6xyz789ghiOOOO") == 6

    def test_get_nick_version_v7(self):
        """Detect hypothetical future version from nick."""
        assert get_nick_version("J7future123OOOO") == 7

    def test_get_nick_version_empty(self):
        """Empty nick returns default."""
        assert get_nick_version("") == JM_VERSION_MIN

    def test_get_nick_version_too_short(self):
        """Too short nick returns default."""
        assert get_nick_version("J") == JM_VERSION_MIN

    def test_get_nick_version_no_j_prefix(self):
        """Nick without J prefix returns default."""
        assert get_nick_version("X6abcdef") == JM_VERSION_MIN

    def test_get_nick_version_non_digit(self):
        """Nick with non-digit version returns default."""
        assert get_nick_version("JXabcdef") == JM_VERSION_MIN


# ==============================================================================
# FeatureSet Tests
# ==============================================================================


class TestFeatureSet:
    """Tests for FeatureSet class."""

    def test_empty_featureset(self):
        """Empty FeatureSet has no features."""
        fs = FeatureSet()
        assert len(fs) == 0
        assert not fs
        assert FEATURE_NEUTRINO_COMPAT not in fs

    def test_from_list(self):
        """Create FeatureSet from list."""
        fs = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT, FEATURE_PUSH_ENCRYPTED])
        assert len(fs) == 2
        assert fs.supports_neutrino_compat()
        assert fs.supports_push_encrypted()

    def test_from_comma_string_with_commas(self):
        """Parse FeatureSet from comma-separated string (legacy format)."""
        fs = FeatureSet.from_comma_string("neutrino_compat,push_encrypted")
        assert len(fs) == 2
        assert FEATURE_NEUTRINO_COMPAT in fs
        assert FEATURE_PUSH_ENCRYPTED in fs

    def test_from_comma_string_with_plus(self):
        """Parse FeatureSet from plus-separated string (peerlist format)."""
        fs = FeatureSet.from_comma_string("neutrino_compat+push_encrypted+peerlist_features")
        assert len(fs) == 3
        assert FEATURE_NEUTRINO_COMPAT in fs
        assert FEATURE_PUSH_ENCRYPTED in fs
        assert FEATURE_PEERLIST_FEATURES in fs

    def test_from_comma_string_empty(self):
        """Parse empty string returns empty FeatureSet."""
        fs = FeatureSet.from_comma_string("")
        assert len(fs) == 0

        fs2 = FeatureSet.from_comma_string("   ")
        assert len(fs2) == 0

    def test_from_comma_string_single(self):
        """Parse single feature (no separator)."""
        fs = FeatureSet.from_comma_string("neutrino_compat")
        assert len(fs) == 1
        assert FEATURE_NEUTRINO_COMPAT in fs

    def test_to_comma_string_uses_plus(self):
        """to_comma_string outputs plus-separated format."""
        fs = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT, FEATURE_PUSH_ENCRYPTED])
        result = fs.to_comma_string()
        # Should use + separator, sorted alphabetically
        assert result == "neutrino_compat+push_encrypted"
        assert "," not in result

    def test_to_dict(self):
        """Convert to dict for JSON serialization."""
        fs = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT, FEATURE_PEERLIST_FEATURES])
        d = fs.to_dict()
        assert d[FEATURE_NEUTRINO_COMPAT] is True
        assert d[FEATURE_PEERLIST_FEATURES] is True
        assert FEATURE_PUSH_ENCRYPTED not in d

    def test_from_handshake(self):
        """Extract features from handshake payload."""
        handshake = {
            "proto-ver": 5,
            "features": {
                FEATURE_NEUTRINO_COMPAT: True,
                FEATURE_PUSH_ENCRYPTED: False,  # Should be ignored
                "unknown_feature": True,  # Unknown features included
            },
        }
        fs = FeatureSet.from_handshake(handshake)
        assert FEATURE_NEUTRINO_COMPAT in fs
        assert FEATURE_PUSH_ENCRYPTED not in fs  # Was False
        assert "unknown_feature" in fs

    def test_intersection(self):
        """Intersection of two FeatureSets."""
        fs1 = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT, FEATURE_PUSH_ENCRYPTED])
        fs2 = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT, FEATURE_PEERLIST_FEATURES])
        result = fs1.intersection(fs2)
        assert len(result) == 1
        assert FEATURE_NEUTRINO_COMPAT in result
        assert FEATURE_PUSH_ENCRYPTED not in result
        assert FEATURE_PEERLIST_FEATURES not in result

    def test_roundtrip_plus_separator(self):
        """Roundtrip through plus-separated string."""
        original = FeatureSet.from_list(
            [FEATURE_NEUTRINO_COMPAT, FEATURE_PUSH_ENCRYPTED, FEATURE_PEERLIST_FEATURES]
        )
        serialized = original.to_comma_string()
        restored = FeatureSet.from_comma_string(serialized)
        assert original.features == restored.features


# ==============================================================================
# Peerlist Entry with Features Tests
# ==============================================================================


class TestPeerlistEntryFeatures:
    """Tests for peerlist entries with F: feature suffix."""

    def test_create_entry_with_features(self):
        """Create peerlist entry with features."""
        features = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT, FEATURE_PEERLIST_FEATURES])
        entry = create_peerlist_entry("J5TestNick", "test.onion:5222", features=features)
        # Should contain F: prefix with plus-separated features
        assert "J5TestNick;test.onion:5222;F:" in entry
        assert "neutrino_compat" in entry
        assert "peerlist_features" in entry
        # Should NOT use comma separator (would conflict with peerlist separator)
        assert ",neutrino" not in entry
        assert ",peerlist" not in entry

    def test_create_entry_with_features_and_disconnected(self):
        """Create disconnected peerlist entry with features."""
        features = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT])
        entry = create_peerlist_entry(
            "J5TestNick", "test.onion:5222", disconnected=True, features=features
        )
        assert ";D;" in entry
        assert ";F:neutrino_compat" in entry

    def test_parse_entry_with_plus_features(self):
        """Parse peerlist entry with plus-separated features."""
        entry = "J5TestNick;test.onion:5222;F:neutrino_compat+peerlist_features"
        nick, location, disco, features = parse_peerlist_entry(entry)
        assert nick == "J5TestNick"
        assert location == "test.onion:5222"
        assert not disco
        assert len(features) == 2
        assert FEATURE_NEUTRINO_COMPAT in features
        assert FEATURE_PEERLIST_FEATURES in features

    def test_parse_entry_disconnected_with_features(self):
        """Parse disconnected entry with features."""
        entry = "J5TestNick;test.onion:5222;D;F:neutrino_compat"
        nick, location, disco, features = parse_peerlist_entry(entry)
        assert disco
        assert FEATURE_NEUTRINO_COMPAT in features

    def test_parse_entry_no_features(self):
        """Parse legacy entry without features."""
        entry = "J5TestNick;test.onion:5222"
        nick, location, disco, features = parse_peerlist_entry(entry)
        assert nick == "J5TestNick"
        assert location == "test.onion:5222"
        assert not disco
        assert len(features) == 0

    def test_roundtrip_peerlist_entry_with_features(self):
        """Roundtrip peerlist entry creation and parsing."""
        original_features = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT, FEATURE_PUSH_ENCRYPTED])
        entry = create_peerlist_entry(
            "J5RoundTrip", "round.onion:5222", disconnected=False, features=original_features
        )
        nick, location, disco, parsed_features = parse_peerlist_entry(entry)
        assert nick == "J5RoundTrip"
        assert location == "round.onion:5222"
        assert not disco
        assert parsed_features.features == original_features.features

    def test_invalid_entry_no_separator(self):
        """Entry without separator raises ValueError."""
        with pytest.raises(ValueError):
            parse_peerlist_entry("invalid_entry_no_separator")

    def test_entry_with_empty_features(self):
        """Entry with empty FeatureSet has no F: suffix."""
        entry = create_peerlist_entry("J5TestNick", "test.onion:5222", features=FeatureSet())
        assert ";F:" not in entry
        assert entry == "J5TestNick;test.onion:5222"


# ==============================================================================
# RequiredFeatures Tests
# ==============================================================================


class TestRequiredFeatures:
    """Tests for RequiredFeatures class."""

    def test_none_has_no_requirements(self):
        """RequiredFeatures.none() has no requirements."""
        rf = RequiredFeatures.none()
        assert not rf
        assert len(rf.required) == 0

    def test_for_neutrino_taker(self):
        """RequiredFeatures.for_neutrino_taker() requires neutrino_compat."""
        rf = RequiredFeatures.for_neutrino_taker()
        assert rf
        assert FEATURE_NEUTRINO_COMPAT in rf.required

    def test_is_compatible_with_matching_features(self):
        """Peer with required features is compatible."""
        rf = RequiredFeatures.for_neutrino_taker()
        fs = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT, FEATURE_PUSH_ENCRYPTED])
        ok, msg = rf.is_compatible(fs)
        assert ok
        assert msg == ""

    def test_is_compatible_with_missing_features(self):
        """Peer missing required features is incompatible."""
        rf = RequiredFeatures.for_neutrino_taker()
        fs = FeatureSet.from_list([FEATURE_PUSH_ENCRYPTED])
        ok, msg = rf.is_compatible(fs)
        assert not ok
        assert "Missing required features" in msg

    def test_is_compatible_with_empty_peer_features(self):
        """Peer with no features is incompatible when requirements exist."""
        rf = RequiredFeatures.for_neutrino_taker()
        fs = FeatureSet()
        ok, msg = rf.is_compatible(fs)
        assert not ok

    def test_no_requirements_always_compatible(self):
        """No requirements means any peer is compatible."""
        rf = RequiredFeatures.none()
        fs = FeatureSet()
        ok, msg = rf.is_compatible(fs)
        assert ok
        assert msg == ""

    def test_bool_reflects_required_set(self):
        """__bool__ returns True when there are requirements."""
        assert bool(RequiredFeatures.for_neutrino_taker())
        assert not bool(RequiredFeatures.none())


# ==============================================================================
# ProtocolMessage to_bytes/from_bytes Tests
# ==============================================================================


class TestProtocolMessageBytes:
    """Tests for ProtocolMessage.to_bytes and from_bytes."""

    def test_to_bytes_returns_utf8(self):
        """to_bytes returns UTF-8 encoded JSON."""
        msg = ProtocolMessage(type=MessageType.PRIVMSG, payload={"nick": "alice"})
        data = msg.to_bytes()
        assert isinstance(data, bytes)
        assert b"685" in data  # PRIVMSG value
        assert b"alice" in data

    def test_from_bytes_roundtrip(self):
        """Round-trip through to_bytes/from_bytes preserves message."""
        original = ProtocolMessage(type=MessageType.PEERLIST, payload={"peers": ["a", "b"]})
        data = original.to_bytes()
        restored = ProtocolMessage.from_bytes(data)
        assert restored.type == MessageType.PEERLIST
        assert restored.payload == {"peers": ["a", "b"]}

    def test_from_bytes_all_message_types(self):
        """Verify roundtrip for all MessageType variants."""
        for mt in MessageType:
            msg = ProtocolMessage(type=mt, payload={"t": mt.name})
            restored = ProtocolMessage.from_bytes(msg.to_bytes())
            assert restored.type == mt
            assert restored.payload["t"] == mt.name


# ==============================================================================
# FeatureSet.validate_dependencies and supports Tests
# ==============================================================================


class TestFeatureSetEdgeCases:
    """Additional FeatureSet edge case tests for coverage."""

    def test_validate_dependencies_all_satisfied(self):
        """All current features have no deps, so validation always passes."""
        fs = FeatureSet.from_list(
            [FEATURE_NEUTRINO_COMPAT, FEATURE_PUSH_ENCRYPTED, FEATURE_PEERLIST_FEATURES]
        )
        ok, msg = fs.validate_dependencies()
        assert ok
        assert msg == ""

    def test_validate_dependencies_empty(self):
        """Empty feature set has no dependency issues."""
        fs = FeatureSet()
        ok, msg = fs.validate_dependencies()
        assert ok
        assert msg == ""

    def test_supports_generic(self):
        """FeatureSet.supports() checks for arbitrary feature strings."""
        fs = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT])
        assert fs.supports(FEATURE_NEUTRINO_COMPAT)
        assert not fs.supports(FEATURE_PUSH_ENCRYPTED)
        assert not fs.supports("nonexistent_feature")

    def test_supports_peerlist_features(self):
        """FeatureSet.supports_peerlist_features() returns correct value."""
        fs_with = FeatureSet.from_list([FEATURE_PEERLIST_FEATURES])
        assert fs_with.supports_peerlist_features()

        fs_without = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT])
        assert not fs_without.supports_peerlist_features()

    def test_iter_and_contains(self):
        """Test __iter__ and __contains__ dunder methods."""
        fs = FeatureSet.from_list([FEATURE_NEUTRINO_COMPAT, FEATURE_PUSH_ENCRYPTED])
        features_list = list(fs)
        assert len(features_list) == 2
        assert FEATURE_NEUTRINO_COMPAT in features_list
        assert FEATURE_PUSH_ENCRYPTED in features_list


# ==============================================================================
# parse_jm_message Edge Cases
# ==============================================================================


class TestParseJmMessageEdgeCases:
    """Edge case tests for parse_jm_message."""

    def test_returns_none_for_empty_string(self):
        """Empty string returns None."""
        assert parse_jm_message("") is None

    def test_returns_none_for_no_separator(self):
        """String without ! separator returns None."""
        assert parse_jm_message("no_separator_here") is None

    def test_returns_none_for_single_separator(self):
        """String with only one ! returns None (need at least 3 parts)."""
        assert parse_jm_message("alice!bob") is None

    def test_handles_multiple_separators_in_command(self):
        """Multiple ! in the command part are preserved."""
        result = parse_jm_message("alice!bob!cmd!with!bangs")
        assert result is not None
        from_nick, to_nick, rest = result
        assert from_nick == "alice"
        assert to_nick == "bob"
        assert rest == "cmd!with!bangs"


# ==============================================================================
# parse_peer_location Edge Cases
# ==============================================================================


class TestParsePeerLocationEdgeCases:
    """Edge case tests for parse_peer_location."""

    def test_port_zero_raises(self):
        """Port 0 is invalid."""
        with pytest.raises(ValueError, match="Invalid location"):
            parse_peer_location("test.onion:0")

    def test_negative_port_raises(self):
        """Negative port is invalid."""
        with pytest.raises(ValueError, match="Invalid location"):
            parse_peer_location("test.onion:-1")

    def test_non_numeric_port_raises(self):
        """Non-numeric port raises ValueError."""
        with pytest.raises(ValueError, match="Invalid location"):
            parse_peer_location("test.onion:abc")

    def test_no_colon_raises(self):
        """Location without colon raises ValueError."""
        with pytest.raises(ValueError, match="Invalid location"):
            parse_peer_location("test.onion")
