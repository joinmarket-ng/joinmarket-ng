"""
Tests for jmcore.models
"""

from typing import Any

import pytest

from jmcore.models import (
    DIRECTORY_NODES_MAINNET,
    DIRECTORY_NODES_SIGNET,
    HandshakeRequest,
    HandshakeResponse,
    MessageEnvelope,
    MessageParsingError,
    NetworkType,
    Offer,
    OfferType,
    OrderBook,
    PeerInfo,
    PeerStatus,
    get_default_directory_nodes,
    validate_json_nesting_depth,
)


def test_peer_info_valid():
    peer = PeerInfo(
        nick="test_peer",
        onion_address="abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion",
        port=5222,
        network=NetworkType.MAINNET,
    )
    assert peer.nick == "test_peer"
    assert peer.status == PeerStatus.UNCONNECTED
    assert not peer.is_directory


def test_peer_info_location_string():
    peer = PeerInfo(
        nick="test",
        onion_address="abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion",
        port=5222,
    )
    assert (
        peer.location_string
        == "abcdefghijklmnopqrstuvwxyz234567abcdefghijklmnopqrstuvwx.onion:5222"
    )


def test_peer_info_not_serving():
    peer = PeerInfo(nick="test", onion_address="NOT-SERVING-ONION", port=-1)
    assert peer.location_string == "NOT-SERVING-ONION"


def test_peer_info_invalid_port():
    with pytest.raises(ValueError):
        PeerInfo(
            nick="test",
            onion_address="example1234567890abcdefghijklmnopqrstuvwxyz234567890abcd.onion",
            port=0,
        )


def test_message_envelope_serialization():
    envelope = MessageEnvelope(message_type=793, payload="test message")
    data = envelope.to_bytes()
    assert b'"type": 793' in data
    assert b'"line": "test message"' in data

    restored = MessageEnvelope.from_bytes(data)
    assert restored.message_type == envelope.message_type
    assert restored.payload == envelope.payload


def test_handshake_request():
    hs = HandshakeRequest(
        location_string="test.onion:5222", proto_ver=9, nick="tester", network=NetworkType.MAINNET
    )
    assert hs.app_name == "JoinMarket"
    assert not hs.directory
    assert hs.proto_ver == 9


def test_handshake_response():
    hs = HandshakeResponse(
        proto_ver_min=9,
        proto_ver_max=9,
        accepted=True,
        nick="directory",
        network=NetworkType.MAINNET,
    )
    assert hs.app_name == "JoinMarket"
    assert hs.directory
    assert hs.accepted


def test_message_envelope_line_length_limit():
    """Test that messages exceeding max_line_length are rejected."""
    # Create a message that's too long (default limit is 64KB)
    long_payload = "x" * 70000
    envelope = MessageEnvelope(message_type=793, payload=long_payload)
    data = envelope.to_bytes()

    # Should raise MessageParsingError with default limit (65536 bytes)
    with pytest.raises(MessageParsingError, match="exceeds maximum"):
        MessageEnvelope.from_bytes(data)

    # Should succeed with higher limit
    result = MessageEnvelope.from_bytes(data, max_line_length=100000)
    assert result.payload == long_payload


def test_message_envelope_nesting_depth_limit():
    """Test that deeply nested JSON is rejected."""
    import json

    # Create deeply nested JSON (15 levels)
    nested: dict[str, Any] = {"a": {}}
    current = nested["a"]
    for _ in range(14):
        current["b"] = {}
        current = current["b"]

    data = json.dumps({"type": 793, "line": "test", "nested": nested}).encode()

    # Should raise MessageParsingError with default limit (10 levels)
    with pytest.raises(MessageParsingError, match="nesting depth exceeds"):
        MessageEnvelope.from_bytes(data)

    # Should succeed with higher limit
    result = MessageEnvelope.from_bytes(data, max_json_nesting_depth=20)
    assert result.message_type == 793


def test_validate_json_nesting_depth_dict():
    """Test nesting depth validation for dictionaries."""
    # Shallow structure (3 levels) - should pass
    shallow = {"a": {"b": {"c": 1}}}
    validate_json_nesting_depth(shallow, max_depth=5)

    # Deep structure (6 levels) - should fail with max_depth=5
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    with pytest.raises(MessageParsingError):
        validate_json_nesting_depth(deep, max_depth=5)


def test_validate_json_nesting_depth_list():
    """Test nesting depth validation for lists."""
    # Shallow structure (3 levels) - should pass
    shallow = [[[1, 2, 3]]]
    validate_json_nesting_depth(shallow, max_depth=5)

    # Deep structure (6 levels) - should fail with max_depth=5
    deep = [[[[[[1]]]]]]
    with pytest.raises(MessageParsingError):
        validate_json_nesting_depth(deep, max_depth=5)


def test_validate_json_nesting_depth_mixed():
    """Test nesting depth validation for mixed dict/list structures."""
    # Mixed structure (5 levels)
    mixed = {"a": [{"b": [{"c": 1}]}]}

    # Should pass with max_depth=5
    validate_json_nesting_depth(mixed, max_depth=5)

    # Should fail with max_depth=3
    with pytest.raises(MessageParsingError):
        validate_json_nesting_depth(mixed, max_depth=3)


def test_message_envelope_parsing_order():
    """Test that line length is checked before JSON parsing."""
    # Create invalid JSON that's too long
    long_invalid_json = b'{"type": 793, "invalid' + b"x" * 70000

    # Should raise MessageParsingError (line length), not JSONDecodeError
    with pytest.raises(MessageParsingError, match="line length"):
        MessageEnvelope.from_bytes(long_invalid_json)


# ==============================================================================
# get_default_directory_nodes Tests
# ==============================================================================


class TestGetDefaultDirectoryNodes:
    """Tests for get_default_directory_nodes function."""

    def test_mainnet_returns_nodes(self):
        """Mainnet returns the predefined directory nodes."""
        nodes = get_default_directory_nodes(NetworkType.MAINNET)
        assert nodes == DIRECTORY_NODES_MAINNET
        assert len(nodes) > 0

    def test_mainnet_returns_copy(self):
        """Mainnet returns a copy, not the original list."""
        nodes = get_default_directory_nodes(NetworkType.MAINNET)
        nodes.append("extra.onion:5222")
        assert "extra.onion:5222" not in get_default_directory_nodes(NetworkType.MAINNET)

    def test_signet_returns_nodes(self):
        """Signet returns signet directory nodes."""
        nodes = get_default_directory_nodes(NetworkType.SIGNET)
        assert nodes == DIRECTORY_NODES_SIGNET
        assert len(nodes) > 0

    def test_testnet_returns_empty(self):
        """Testnet has no default directory nodes."""
        nodes = get_default_directory_nodes(NetworkType.TESTNET)
        assert nodes == []

    def test_regtest_returns_empty(self):
        """Regtest has no default directory nodes."""
        nodes = get_default_directory_nodes(NetworkType.REGTEST)
        assert nodes == []


# ==============================================================================
# Offer Tests
# ==============================================================================


class TestOffer:
    """Tests for Offer model methods."""

    def test_is_absolute_fee_absolute(self):
        """Absolute offer types return True."""
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee=250,
        )
        assert offer.is_absolute_fee()

    def test_is_absolute_fee_relative(self):
        """Relative offer types return False."""
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.0003",
        )
        assert not offer.is_absolute_fee()

    def test_calculate_fee_absolute(self):
        """Absolute fee is returned directly."""
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee=500,
        )
        assert offer.calculate_fee(1_000_000) == 500

    def test_calculate_fee_relative(self):
        """Relative fee is calculated from amount."""
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.001",
        )
        # 0.1% of 1_000_000 = 1000
        fee = offer.calculate_fee(1_000_000)
        assert fee == 1000

    def test_swa_absolute_offer(self):
        """SWA absolute offer type is also absolute."""
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SWA_ABSOLUTE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee=300,
        )
        assert offer.is_absolute_fee()
        assert offer.calculate_fee(5_000_000) == 300

    def test_swa_relative_offer(self):
        """SWA relative offer type is relative."""
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SWA_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee="0.0005",
        )
        assert not offer.is_absolute_fee()

    def test_cjfee_scientific_notation_normalized(self):
        """Relative offer cjfee in scientific notation is normalized to fixed-point."""
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee="1E-9",
        )
        assert offer.cjfee == "0.000000001"
        assert "E" not in str(offer.cjfee)
        assert "e" not in str(offer.cjfee)

    def test_cjfee_scientific_notation_fee_calculation(self):
        """Offer with scientific notation cjfee can calculate fees without error."""
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee="5E-7",
        )
        # 5E-7 * 100_000_000 = 50
        assert offer.calculate_fee(100_000_000) == 50

    def test_cjfee_lowercase_scientific_notation(self):
        """Lowercase 'e' in scientific notation is also normalized."""
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee="1e-5",
        )
        assert offer.cjfee == "0.00001"

    def test_cjfee_very_small_value_regression(self):
        """Regression: very small cjfee parsed via Decimal must not crash fee calculation.

        Reproduces the exact issue where str(Decimal("0.000000001")) produces "1E-9",
        which then fails in calculate_relative_fee with:
        "Fee rate must be decimal string or integer, got 1E-9"
        """
        from decimal import Decimal

        # Simulate what directory_client does when parsing a relative offer
        raw_cjfee = "0.000000001"
        parsed_via_decimal = str(Decimal(raw_cjfee))  # produces "1E-9"
        assert parsed_via_decimal == "1E-9"  # confirm the problematic behavior

        # Creating an Offer with this value should normalize it
        offer = Offer(
            counterparty="J5TestMaker",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee=parsed_via_decimal,
        )
        assert "E" not in str(offer.cjfee)
        # Fee calculation should succeed without ValueError
        fee = offer.calculate_fee(100_000_000)
        assert isinstance(fee, int)


# ==============================================================================
# OrderBook Tests
# ==============================================================================


class TestOrderBook:
    """Tests for OrderBook model methods."""

    def _make_offer(self, counterparty: str, oid: int = 0) -> Offer:
        """Helper to create a test offer."""
        return Offer(
            counterparty=counterparty,
            oid=oid,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=100_000,
            maxsize=10_000_000,
            txfee=0,
            cjfee=250,
        )

    def test_add_offers_sets_directory_node(self):
        """add_offers sets directory_node on each offer."""
        ob = OrderBook()
        offers = [self._make_offer("maker1"), self._make_offer("maker2")]
        ob.add_offers(offers, "dir1.onion:5222")
        assert all(o.directory_node == "dir1.onion:5222" for o in ob.offers)

    def test_add_offers_appends_to_directory_nodes_list(self):
        """add_offers records the directory node in the orderbook's directory list."""
        ob = OrderBook()
        ob.add_offers([self._make_offer("maker1")], "dir1.onion:5222")
        ob.add_offers([self._make_offer("maker2")], "dir2.onion:5222")
        assert "dir1.onion:5222" in ob.directory_nodes
        assert "dir2.onion:5222" in ob.directory_nodes

    def test_add_offers_deduplicates_directory_nodes(self):
        """Adding offers from the same directory doesn't duplicate."""
        ob = OrderBook()
        ob.add_offers([self._make_offer("maker1")], "dir1.onion:5222")
        ob.add_offers([self._make_offer("maker2")], "dir1.onion:5222")
        assert ob.directory_nodes.count("dir1.onion:5222") == 1

    def test_get_offers_by_directory_with_directory_node(self):
        """get_offers_by_directory groups by directory_node."""
        ob = OrderBook()
        ob.add_offers([self._make_offer("maker1")], "dir1.onion:5222")
        ob.add_offers([self._make_offer("maker2")], "dir2.onion:5222")
        grouped = ob.get_offers_by_directory()
        assert "dir1.onion:5222" in grouped
        assert "dir2.onion:5222" in grouped
        assert len(grouped["dir1.onion:5222"]) == 1
        assert len(grouped["dir2.onion:5222"]) == 1

    def test_get_offers_by_directory_with_directory_nodes_plural(self):
        """get_offers_by_directory uses directory_nodes (plural) when populated."""
        ob = OrderBook()
        offer = self._make_offer("maker1")
        offer.directory_nodes = ["dir1.onion:5222", "dir2.onion:5222"]
        ob.offers.append(offer)
        grouped = ob.get_offers_by_directory()
        # Offer appears under both directories
        assert "dir1.onion:5222" in grouped
        assert "dir2.onion:5222" in grouped

    def test_get_offers_by_directory_unknown_fallback(self):
        """Offers with no directory info are grouped under 'unknown'."""
        ob = OrderBook()
        offer = self._make_offer("maker1")
        # Don't set any directory info
        offer.directory_node = None
        offer.directory_nodes = []
        ob.offers.append(offer)
        grouped = ob.get_offers_by_directory()
        assert "unknown" in grouped
        assert len(grouped["unknown"]) == 1
