"""
Tests for jmcore.podle module.

Tests both PoDLE generation (taker side) and verification (maker side).
"""

import hashlib

import pytest

from jmcore.constants import SECP256K1_P
from jmcore.podle import (
    G_COMPRESSED,
    G_UNCOMPRESSED,
    NUMS_TEST_VECTORS,
    SECP256K1_N,
    PoDLECommitment,
    PoDLEError,
    deserialize_revelation,
    generate_nums_point,
    generate_podle,
    get_nums_point,
    parse_podle_revelation,
    point_add,
    point_mult,
    point_to_bytes,
    scalar_mult_g,
    serialize_revelation,
    verify_podle,
)


class TestConstants:
    """Tests for PoDLE constants."""

    def test_secp256k1_n(self) -> None:
        """Test curve order is correct."""
        assert (
            int("FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16)
            == SECP256K1_N
        )

    def test_secp256k1_p(self) -> None:
        """Test field prime is correct."""
        assert SECP256K1_P == 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F

    def test_g_compressed(self) -> None:
        """Test generator point is compressed."""
        assert len(G_COMPRESSED) == 33
        assert G_COMPRESSED[0] in (0x02, 0x03)

    def test_g_uncompressed(self) -> None:
        """Test uncompressed generator point format."""
        assert len(G_UNCOMPRESSED) == 65
        assert G_UNCOMPRESSED[0] == 0x04

    def test_g_compressed_matches_uncompressed(self) -> None:
        """
        Verify that G_COMPRESSED and G_UNCOMPRESSED represent the same point.

        This ensures we can trust both constants and that G_UNCOMPRESSED is not
        tampered with, minimizing the need for trust in hardcoded values.
        """
        # Convert uncompressed to compressed using coincurve
        from coincurve import PublicKey

        # Parse uncompressed point
        uncompressed_point = PublicKey(G_UNCOMPRESSED)
        # Get compressed representation
        compressed_from_uncompressed = uncompressed_point.format(compressed=True)

        # Should match G_COMPRESSED
        assert compressed_from_uncompressed == G_COMPRESSED

        # Also verify x and y coordinates match what's documented
        # Uncompressed format is: 0x04 || x (32 bytes) || y (32 bytes)
        x_coord = G_UNCOMPRESSED[1:33]
        y_coord = G_UNCOMPRESSED[33:65]

        # x should match the compressed form (minus the 0x02 prefix)
        assert x_coord == G_COMPRESSED[1:]

        # Verify these are the standard secp256k1 generator coordinates
        assert x_coord.hex() == "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
        assert y_coord.hex() == "483ada7726a3c4655da4fbfc0e1108a8fd17b448a68554199c47d08ffb10d4b8"

    def test_nums_test_vectors_format(self) -> None:
        """Test NUMS test vectors are valid hex strings."""
        for idx, hex_str in NUMS_TEST_VECTORS.items():
            assert len(hex_str) == 66, f"NUMS test vector {idx} wrong length"
            assert hex_str.startswith("02") or hex_str.startswith("03"), (
                f"NUMS test vector {idx} wrong prefix"
            )


class TestGetNumsPoint:
    """Tests for get_nums_point and generate_nums_point functions."""

    def test_valid_index_range(self) -> None:
        """Test getting NUMS points in valid range 0-255."""
        # Test first few and some specific indices
        for i in [0, 1, 5, 9, 100, 255]:
            point = get_nums_point(i)
            assert point is not None
            compressed = point_to_bytes(point)
            assert len(compressed) == 33

    def test_nums_generation_matches_test_vectors(self) -> None:
        """
        Test that dynamically generated NUMS points match known test vectors.

        This validates that the NUMS generation algorithm produces the correct
        deterministic values as documented in the original JoinMarket spec.
        """
        for idx, expected_hex in NUMS_TEST_VECTORS.items():
            point = generate_nums_point(idx)
            actual_hex = point_to_bytes(point).hex()
            assert actual_hex == expected_hex, (
                f"NUMS point {idx} mismatch: expected {expected_hex}, got {actual_hex}"
            )

    def test_nums_caching(self) -> None:
        """Test that NUMS points are cached after generation."""
        # First call generates the point
        point1 = get_nums_point(42)
        # Second call should return cached point
        point2 = get_nums_point(42)
        # Should be the exact same object
        assert point1 is point2

    def test_invalid_index_negative(self) -> None:
        """Test negative index raises error."""
        with pytest.raises(PoDLEError, match="must be in range"):
            get_nums_point(-1)

    def test_invalid_index_too_high(self) -> None:
        """Test index > 255 raises error."""
        with pytest.raises(PoDLEError, match="must be in range"):
            get_nums_point(256)


class TestECOperations:
    """Tests for elliptic curve operations."""

    def test_scalar_mult_g(self) -> None:
        """Test scalar multiplication with generator."""
        # Private key 1 should give generator point
        result = scalar_mult_g(1)
        compressed = point_to_bytes(result)
        assert compressed == G_COMPRESSED

    def test_scalar_mult_g_modulo(self) -> None:
        """Test scalar is taken modulo N."""
        # Scalar = N should give same as scalar = 0 (but 0 is invalid)
        # Scalar = N + 1 should give same as scalar = 1
        result = scalar_mult_g(SECP256K1_N + 1)
        compressed = point_to_bytes(result)
        assert compressed == G_COMPRESSED

    def test_point_add(self) -> None:
        """Test point addition."""
        g = scalar_mult_g(1)
        g2 = scalar_mult_g(2)

        # G + G should equal 2*G
        result = point_add(g, g)
        assert point_to_bytes(result) == point_to_bytes(g2)

    def test_point_mult(self) -> None:
        """Test point scalar multiplication."""
        j0 = get_nums_point(0)

        # 2 * J0
        result = point_mult(2, j0)
        # This should be a valid point
        compressed = point_to_bytes(result)
        assert len(compressed) == 33

    def test_point_to_bytes(self) -> None:
        """Test point serialization."""
        g = scalar_mult_g(1)
        compressed = point_to_bytes(g)
        assert len(compressed) == 33
        assert compressed[0] in (0x02, 0x03)


class TestGeneratePoDLE:
    """Tests for PoDLE generation."""

    def test_generate_valid(self) -> None:
        """Test generating a valid PoDLE commitment."""
        # Use a known private key
        private_key = bytes([1] * 32)
        utxo_str = "a" * 64 + ":0"

        commitment = generate_podle(private_key, utxo_str, index=0)

        assert isinstance(commitment, PoDLECommitment)
        assert len(commitment.commitment) == 32
        assert len(commitment.p) == 33
        assert len(commitment.p2) == 33
        assert len(commitment.sig) == 32
        assert len(commitment.e) == 32
        assert commitment.utxo == utxo_str
        assert commitment.index == 0

    def test_commitment_is_hash_of_p2(self) -> None:
        """Test commitment = H(P2)."""
        private_key = bytes([2] * 32)
        utxo_str = "b" * 64 + ":1"

        commitment = generate_podle(private_key, utxo_str)

        expected_commitment = hashlib.sha256(commitment.p2).digest()
        assert commitment.commitment == expected_commitment

    def test_different_indices_give_different_p2(self) -> None:
        """Test different NUMS indices give different P2."""
        private_key = bytes([3] * 32)
        utxo_str = "c" * 64 + ":2"

        c0 = generate_podle(private_key, utxo_str, index=0)
        c1 = generate_podle(private_key, utxo_str, index=1)

        assert c0.p == c1.p  # Same P (derived from same private key)
        assert c0.p2 != c1.p2  # Different P2 (different J point)

    def test_invalid_private_key_length(self) -> None:
        """Test invalid private key length."""
        with pytest.raises(PoDLEError, match="Invalid private key length"):
            generate_podle(b"short", "a" * 64 + ":0")

    def test_invalid_nums_index(self) -> None:
        """Test invalid NUMS index (must be 0-255)."""
        with pytest.raises(PoDLEError, match="Invalid NUMS index"):
            generate_podle(bytes([1] * 32), "a" * 64 + ":0", index=256)

    def test_zero_private_key(self) -> None:
        """Test zero private key is rejected."""
        with pytest.raises(PoDLEError, match="Invalid private key value"):
            generate_podle(bytes(32), "a" * 64 + ":0")


class TestVerifyPoDLE:
    """Tests for PoDLE verification."""

    def test_verify_valid_proof(self) -> None:
        """Test verification of valid proof."""
        private_key = bytes([5] * 32)
        utxo_str = "d" * 64 + ":3"

        commitment = generate_podle(private_key, utxo_str, index=0)

        is_valid, error = verify_podle(
            p=commitment.p,
            p2=commitment.p2,
            sig=commitment.sig,
            e=commitment.e,
            commitment=commitment.commitment,
            index_range=range(10),
        )

        assert is_valid, f"Verification should succeed: {error}"
        assert error == ""

    def test_verify_fails_wrong_commitment(self) -> None:
        """Test verification fails with wrong commitment."""
        private_key = bytes([6] * 32)
        utxo_str = "e" * 64 + ":4"

        commitment = generate_podle(private_key, utxo_str)

        is_valid, error = verify_podle(
            p=commitment.p,
            p2=commitment.p2,
            sig=commitment.sig,
            e=commitment.e,
            commitment=bytes(32),  # Wrong commitment
            index_range=range(10),
        )

        assert not is_valid
        assert "Commitment does not match" in error

    def test_verify_fails_wrong_signature(self) -> None:
        """Test verification fails with wrong signature."""
        private_key = bytes([7] * 32)
        utxo_str = "f" * 64 + ":5"

        commitment = generate_podle(private_key, utxo_str)

        is_valid, error = verify_podle(
            p=commitment.p,
            p2=commitment.p2,
            sig=bytes(32),  # Wrong signature
            e=commitment.e,
            commitment=commitment.commitment,
            index_range=range(10),
        )

        assert not is_valid

    def test_verify_fails_invalid_lengths(self) -> None:
        """Test verification fails with invalid input lengths."""
        is_valid, error = verify_podle(
            p=b"short",
            p2=bytes(33),
            sig=bytes(32),
            e=bytes(32),
            commitment=bytes(32),
        )
        assert not is_valid
        assert "Invalid P length" in error


class TestRevelationParsing:
    """Tests for revelation parsing and serialization."""

    def test_parse_valid_revelation(self) -> None:
        """Test parsing a valid revelation dict."""
        revelation = {
            "P": "02" + "aa" * 32,
            "P2": "03" + "bb" * 32,
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "ee" * 32 + ":0",
        }

        parsed = parse_podle_revelation(revelation)

        assert parsed is not None
        assert len(parsed["P"]) == 33
        assert len(parsed["P2"]) == 33
        assert len(parsed["sig"]) == 32
        assert len(parsed["e"]) == 32
        assert parsed["txid"] == "ee" * 32
        assert parsed["vout"] == 0

    def test_parse_missing_field(self) -> None:
        """Test parsing fails with missing field."""
        revelation = {
            "P": "02" + "aa" * 32,
            # Missing P2
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "ee" * 32 + ":0",
        }

        parsed = parse_podle_revelation(revelation)
        assert parsed is None

    def test_parse_invalid_utxo_format(self) -> None:
        """Test parsing fails with invalid UTXO format."""
        revelation = {
            "P": "02" + "aa" * 32,
            "P2": "03" + "bb" * 32,
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "invalid_utxo",  # Missing :vout
        }

        parsed = parse_podle_revelation(revelation)
        assert parsed is None

    def test_deserialize_valid_revelation(self) -> None:
        """Test deserializing wire format."""
        wire_format = "|".join(
            [
                "02" + "aa" * 32,  # P
                "03" + "bb" * 32,  # P2
                "cc" * 32,  # sig
                "dd" * 32,  # e
                "ee" * 32 + ":0",  # utxo
            ]
        )

        parsed = deserialize_revelation(wire_format)

        assert parsed is not None
        assert parsed["P"] == "02" + "aa" * 32
        assert parsed["utxo"] == "ee" * 32 + ":0"

    def test_deserialize_wrong_parts(self) -> None:
        """Test deserialization fails with wrong number of parts."""
        wire_format = "part1|part2|part3"  # Only 3 parts
        parsed = deserialize_revelation(wire_format)
        assert parsed is None


class TestPoDLECommitment:
    """Tests for PoDLECommitment dataclass."""

    def test_to_revelation(self) -> None:
        """Test converting commitment to revelation dict."""
        commitment = PoDLECommitment(
            commitment=bytes(32),
            p=b"\x02" + bytes(32),
            p2=b"\x03" + bytes(32),
            sig=bytes(32),
            e=bytes(32),
            utxo="a" * 64 + ":0",
            index=0,
        )

        revelation = commitment.to_revelation()

        assert "P" in revelation
        assert "P2" in revelation
        assert "sig" in revelation
        assert "e" in revelation
        assert "utxo" in revelation
        assert revelation["utxo"] == "a" * 64 + ":0"

    def test_to_commitment_str(self) -> None:
        """Test getting commitment as hex string with P prefix.

        JoinMarket requires PoDLE commitments to have a 'P' prefix indicating
        standard PoDLE commitment type. Format: 'P' + hex(commitment)
        """
        commitment = PoDLECommitment(
            commitment=bytes.fromhex("aa" * 32),
            p=b"\x02" + bytes(32),
            p2=b"\x03" + bytes(32),
            sig=bytes(32),
            e=bytes(32),
            utxo="b" * 64 + ":0",
            index=0,
        )

        hex_str = commitment.to_commitment_str()
        # Should be 'P' + 64 hex chars = 65 chars total
        assert hex_str == "P" + "aa" * 32
        assert len(hex_str) == 65


class TestSerializeRevelation:
    """Tests for revelation serialization."""

    def test_serialize_revelation(self) -> None:
        """Test serializing commitment to wire format."""
        commitment = PoDLECommitment(
            commitment=bytes(32),
            p=bytes.fromhex("02" + "aa" * 32),
            p2=bytes.fromhex("03" + "bb" * 32),
            sig=bytes.fromhex("cc" * 32),
            e=bytes.fromhex("dd" * 32),
            utxo="ee" * 32 + ":0",
            index=0,
        )

        wire = serialize_revelation(commitment)

        parts = wire.split("|")
        assert len(parts) == 5
        assert parts[0] == "02" + "aa" * 32
        assert parts[4] == "ee" * 32 + ":0"

    def test_roundtrip(self) -> None:
        """Test serialization roundtrip."""
        private_key = bytes([8] * 32)
        utxo_str = "0" * 64 + ":6"

        original = generate_podle(private_key, utxo_str)
        wire = serialize_revelation(original)
        parsed = deserialize_revelation(wire)

        assert parsed is not None
        assert parsed["P"] == original.p.hex()
        assert parsed["P2"] == original.p2.hex()
        assert parsed["sig"] == original.sig.hex()
        assert parsed["e"] == original.e.hex()
        assert parsed["utxo"] == original.utxo


class TestFullFlow:
    """Integration tests for full PoDLE flow."""

    def test_generate_and_verify(self) -> None:
        """Test full flow: generate commitment, serialize, parse, verify."""
        # Taker generates PoDLE
        private_key = bytes([9] * 32)
        utxo_str = "f" * 64 + ":7"

        commitment = generate_podle(private_key, utxo_str, index=0)

        # Taker sends commitment to maker
        # Commitment string format is: 'P' + hex(commitment) = 65 chars
        commitment_hex = commitment.to_commitment_str()
        assert len(commitment_hex) == 65
        assert commitment_hex.startswith("P")

        # Maker accepts, taker sends revelation
        wire = serialize_revelation(commitment)

        # Maker parses and verifies
        parsed_wire = deserialize_revelation(wire)
        assert parsed_wire is not None

        parsed_revelation = parse_podle_revelation(parsed_wire)
        assert parsed_revelation is not None

        is_valid, error = verify_podle(
            p=parsed_revelation["P"],
            p2=parsed_revelation["P2"],
            sig=parsed_revelation["sig"],
            e=parsed_revelation["e"],
            commitment=commitment.commitment,
            index_range=range(10),
        )

        assert is_valid, f"Full flow verification failed: {error}"

    def test_all_nums_indices(self) -> None:
        """Test PoDLE works with various NUMS indices including higher values."""
        private_key = bytes([10] * 32)
        utxo_str = "e" * 64 + ":8"

        # Test first 10 indices (commonly used)
        for idx in range(10):
            commitment = generate_podle(private_key, utxo_str, index=idx)

            is_valid, error = verify_podle(
                p=commitment.p,
                p2=commitment.p2,
                sig=commitment.sig,
                e=commitment.e,
                commitment=commitment.commitment,
                index_range=range(256),  # Full range support
            )

            assert is_valid, f"Index {idx} verification failed: {error}"

    def test_high_nums_indices(self) -> None:
        """Test PoDLE works with higher NUMS indices (100, 200, 255)."""
        private_key = bytes([11] * 32)
        utxo_str = "d" * 64 + ":9"

        for idx in [100, 200, 255]:
            commitment = generate_podle(private_key, utxo_str, index=idx)

            # Verify with a range that includes the index
            is_valid, error = verify_podle(
                p=commitment.p,
                p2=commitment.p2,
                sig=commitment.sig,
                e=commitment.e,
                commitment=commitment.commitment,
                index_range=range(idx, idx + 1),  # Only check the specific index
            )

            assert is_valid, f"High index {idx} verification failed: {error}"


class TestVerifyPoDLEEdgeCases:
    """Edge cases for PoDLE verification."""

    def test_verify_invalid_p2_length(self) -> None:
        """Test P2 length validation."""
        is_valid, error = verify_podle(
            p=b"\x02" + bytes(32),
            p2=b"short",  # Invalid P2 length
            sig=bytes(32),
            e=bytes(32),
            commitment=bytes(32),
        )
        assert not is_valid
        assert "Invalid P2 length" in error

    def test_verify_invalid_sig_length(self) -> None:
        """Test sig length validation."""
        is_valid, error = verify_podle(
            p=b"\x02" + bytes(32),
            p2=b"\x03" + bytes(32),
            sig=b"short",  # Invalid sig length
            e=bytes(32),
            commitment=bytes(32),
        )
        assert not is_valid
        assert "Invalid sig length" in error

    def test_verify_invalid_e_length(self) -> None:
        """Test e length validation."""
        is_valid, error = verify_podle(
            p=b"\x02" + bytes(32),
            p2=b"\x03" + bytes(32),
            sig=bytes(32),
            e=b"short",  # Invalid e length
            commitment=bytes(32),
        )
        assert not is_valid
        assert "Invalid e length" in error

    def test_verify_invalid_commitment_length(self) -> None:
        """Test commitment length validation."""
        is_valid, error = verify_podle(
            p=b"\x02" + bytes(32),
            p2=b"\x03" + bytes(32),
            sig=bytes(32),
            e=bytes(32),
            commitment=b"short",  # Invalid commitment length
        )
        assert not is_valid
        assert "Invalid commitment length" in error

    def test_verify_sig_out_of_range(self) -> None:
        """Test that signature values >= N are rejected."""
        private_key = bytes([5] * 32)
        utxo_str = "d" * 64 + ":3"
        commitment = generate_podle(private_key, utxo_str, index=0)

        # Set sig to SECP256K1_N (out of range)
        bad_sig = SECP256K1_N.to_bytes(32, "big")

        is_valid, error = verify_podle(
            p=commitment.p,
            p2=commitment.p2,
            sig=bad_sig,
            e=commitment.e,
            commitment=commitment.commitment,
            index_range=range(10),
        )
        assert not is_valid
        assert "out of range" in error

    def test_verify_fails_for_all_indices(self) -> None:
        """Test verification fails when proof index is outside checked range."""
        private_key = bytes([5] * 32)
        utxo_str = "d" * 64 + ":3"
        # Generate with index 5
        commitment = generate_podle(private_key, utxo_str, index=5)

        # Verify with range that doesn't include index 5
        is_valid, error = verify_podle(
            p=commitment.p,
            p2=commitment.p2,
            sig=commitment.sig,
            e=commitment.e,
            commitment=commitment.commitment,
            index_range=range(0, 3),  # Only check 0, 1, 2
        )
        assert not is_valid
        assert "failed for all indices" in error

    def test_verify_invalid_point(self) -> None:
        """Test verification with invalid EC point data."""
        # Use bytes that look right (33 bytes, 0x02 prefix) but aren't a valid point
        # This should cause an exception in PublicKey() constructor
        bad_p = b"\x02" + b"\xff" * 32  # likely not on curve

        # We need p2 to match commitment: commitment = sha256(p2)
        # Use a valid p2 with matching commitment
        private_key = bytes([5] * 32)
        commitment = generate_podle(private_key, "a" * 64 + ":0", index=0)

        is_valid, error = verify_podle(
            p=bad_p,
            p2=commitment.p2,
            sig=commitment.sig,
            e=commitment.e,
            commitment=commitment.commitment,
            index_range=range(10),
        )
        # Should either fail verification or catch the exception
        assert not is_valid


class TestScalarMultGEdgeCases:
    """Edge cases for scalar operations."""

    def test_scalar_mult_g_zero_raises(self) -> None:
        """Zero scalar raises PoDLEError."""
        with pytest.raises(PoDLEError, match="Scalar cannot be zero"):
            scalar_mult_g(0)

    def test_point_mult_zero_raises(self) -> None:
        """Zero scalar in point_mult raises PoDLEError."""
        j = get_nums_point(0)
        with pytest.raises(PoDLEError, match="Scalar cannot be zero"):
            point_mult(0, j)

    def test_scalar_mult_g_with_n(self) -> None:
        """Scalar = N should be reduced to 0 mod N and raise."""
        with pytest.raises(PoDLEError, match="Scalar cannot be zero"):
            scalar_mult_g(SECP256K1_N)


class TestParsePodleRevelationExtended:
    """Tests for extended UTXO format in revelation parsing."""

    def test_parse_extended_utxo_format(self) -> None:
        """Test parsing revelation with extended UTXO format (4 parts)."""
        revelation = {
            "P": "02" + "aa" * 32,
            "P2": "03" + "bb" * 32,
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "ee" * 32 + ":0:0014deadbeef:750000",
        }

        parsed = parse_podle_revelation(revelation)
        assert parsed is not None
        assert parsed["txid"] == "ee" * 32
        assert parsed["vout"] == 0
        assert parsed["scriptpubkey"] == "0014deadbeef"
        assert parsed["blockheight"] == 750000

    def test_parse_three_part_utxo_fails(self) -> None:
        """Three-part UTXO format is invalid."""
        revelation = {
            "P": "02" + "aa" * 32,
            "P2": "03" + "bb" * 32,
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "ee" * 32 + ":0:extra",
        }

        parsed = parse_podle_revelation(revelation)
        assert parsed is None

    def test_parse_invalid_hex_returns_none(self) -> None:
        """Invalid hex in fields returns None."""
        revelation = {
            "P": "not_valid_hex",
            "P2": "03" + "bb" * 32,
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "ee" * 32 + ":0",
        }

        parsed = parse_podle_revelation(revelation)
        assert parsed is None


class TestDeserializeRevelationEdgeCases:
    """Edge cases for deserialize_revelation."""

    def test_empty_string(self) -> None:
        """Empty string returns None."""
        parsed = deserialize_revelation("")
        assert parsed is None

    def test_too_many_parts(self) -> None:
        """Too many pipe-separated parts returns None."""
        wire = "a|b|c|d|e|f"
        parsed = deserialize_revelation(wire)
        assert parsed is None
