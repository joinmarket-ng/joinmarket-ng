"""
Tests for jmcore.podle module.

Tests both PoDLE generation (taker side) and verification (maker side).
"""

import hashlib
from typing import Any
from unittest.mock import Mock, patch

import pytest

import jmcore.podle as podle
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
    verify_podle_binding,
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

    def test_proof_is_deterministic_for_same_transcript(self) -> None:
        private_key = bytes([4] * 32)
        utxo_str = "d" * 64 + ":3"

        first = generate_podle(private_key, utxo_str, index=4)
        second = generate_podle(private_key, utxo_str, index=4)

        assert first == second

    def test_nonce_is_bound_to_utxo_reference(self) -> None:
        private_key = bytes([4] * 32)

        first = generate_podle(private_key, "d" * 64 + ":3", index=4)
        second = generate_podle(private_key, "e" * 64 + ":3", index=4)

        assert first.p == second.p
        assert first.p2 == second.p2
        assert first.commitment == second.commitment
        assert first.sig != second.sig
        assert first.e != second.e

    def test_generation_does_not_require_runtime_entropy(self) -> None:
        with patch("secrets.token_bytes", side_effect=OSError("CSPRNG unavailable")):
            commitment = generate_podle(bytes([5] * 32), "f" * 64 + ":5", index=5)

        is_valid, error = verify_podle(
            p=commitment.p,
            p2=commitment.p2,
            sig=commitment.sig,
            e=commitment.e,
            commitment=commitment.commitment,
            index_range=range(10),
        )
        assert is_valid, error

    def test_generation_retries_zero_challenge(self) -> None:
        real_sha256 = hashlib.sha256
        challenge_calls = 0

        def force_first_zero_challenge(data: bytes = b"") -> Any:
            nonlocal challenge_calls
            if len(data) == 132:
                challenge_calls += 1
                if challenge_calls == 1:
                    return Mock(digest=Mock(return_value=SECP256K1_N.to_bytes(32, "big")))
            return real_sha256(data)

        with (
            patch("jmcore.podle._podle_nonce_candidates", return_value=iter((1, 2))),
            patch("jmcore.podle.hashlib.sha256", side_effect=force_first_zero_challenge),
        ):
            commitment = generate_podle(bytes([5] * 32), "f" * 64 + ":5", index=5)

        assert challenge_calls == 2
        assert int.from_bytes(commitment.e, "big") % SECP256K1_N != 0
        is_valid, error = verify_podle(
            p=commitment.p,
            p2=commitment.p2,
            sig=commitment.sig,
            e=commitment.e,
            commitment=commitment.commitment,
            index_range=range(10),
        )
        assert is_valid, error

    def test_rfc6979_retries_rejected_candidate(self) -> None:
        private_key = bytes.fromhex("01" * 32)
        message_hash = bytes(32)
        candidates = podle._rfc6979_nonce_candidates(private_key, message_hash)
        rejected = next(candidates)
        expected = next(candidates)
        assert expected < rejected

        with patch("jmcore.podle.SECP256K1_N", rejected):
            actual = next(podle._rfc6979_nonce_candidates(private_key, message_hash))

        assert actual == expected

    def test_rfc6979_secp256k1_bits2octets_vector(self) -> None:
        """Cover RFC 6979 bits2octets with the secp256k1 group order.

        This vector applies RFC 6979 Sections 2.3.4 and 3.2 to an all-ones hash and
        the SEC 2 v2 Section 2.7.1 secp256k1 order. The expected candidate was
        independently cross-checked with python-ecdsa 0.19.1 ``generate_k``.
        """
        private_key = (1).to_bytes(32, "big")
        message_hash = b"\xff" * 32
        assert int.from_bytes(message_hash, "big") >= SECP256K1_N

        candidate = next(podle._rfc6979_nonce_candidates(private_key, message_hash))

        assert candidate == int(
            "71139aac71b52f7d5961915af1b30f94baf35e39b0043c33d41a57d476a8905c", 16
        )

    def test_deterministic_proof_vector(self) -> None:
        commitment = generate_podle(bytes.fromhex("01" * 32), "00" * 32 + ":0", index=0)

        assert commitment.commitment.hex() == (
            "0699ef3695ee2050f6d0cfcdb8ef0b49d118d6b0b0bc9f56c8621639ad05bec6"
        )
        assert commitment.p.hex() == (
            "031b84c5567b126440995d3ed5aaba0565d71e1834604819ff9c17f5e9d5dd078f"
        )
        assert commitment.p2.hex() == (
            "0263c89959c452d2310684bc07b9f39d9f4d08a641e28e7ee0e6f69a27bc75dcbd"
        )
        assert commitment.sig.hex() == (
            "4ef60eba328882679d63eccebc463b7e98683b846344181ff754ed77c95ca5af"
        )
        assert commitment.e.hex() == (
            "b643f3799fb7a793c95c5500e8395cdeabe87ddeffa22e9bf203b427e953c3e4"
        )

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
                "ee" * 32 + ":0",  # utxo
                "02" + "aa" * 32,  # P
                "03" + "bb" * 32,  # P2
                "cc" * 32,  # sig
                "dd" * 32,  # e
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
        assert parts[0] == "ee" * 32 + ":0"
        assert parts[1] == "02" + "aa" * 32

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

    def test_verify_zero_sig_is_out_of_range(self) -> None:
        commitment = generate_podle(bytes([7] * 32), "f" * 64 + ":5", index=0)

        is_valid, error = verify_podle(
            p=commitment.p,
            p2=commitment.p2,
            sig=bytes(32),
            e=commitment.e,
            commitment=commitment.commitment,
            index_range=range(1),
        )

        assert not is_valid
        assert "out of range" in error

    def test_verify_raw_challenge_is_not_range_rejected(self) -> None:
        """The SHA256 challenge stays raw and is reduced only for arithmetic."""
        commitment = generate_podle(bytes([6] * 32), "e" * 64 + ":4", index=0)

        is_valid, error = verify_podle(
            p=commitment.p,
            p2=commitment.p2,
            sig=commitment.sig,
            e=SECP256K1_N.to_bytes(32, "big"),
            commitment=commitment.commitment,
            index_range=range(1),
        )

        assert not is_valid
        assert "out of range" not in error

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

    def test_parse_short_txid_rejected(self) -> None:
        """TXID shorter than 64 hex chars is rejected."""
        revelation = {
            "P": "02" + "aa" * 32,
            "P2": "03" + "bb" * 32,
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "abcd:0",
        }
        assert parse_podle_revelation(revelation) is None

    def test_parse_non_hex_txid_rejected(self) -> None:
        """TXID with non-hex characters is rejected."""
        revelation = {
            "P": "02" + "aa" * 32,
            "P2": "03" + "bb" * 32,
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "g" * 64 + ":0",
        }
        assert parse_podle_revelation(revelation) is None

    def test_parse_negative_vout_rejected(self) -> None:
        """Negative vout is rejected."""
        revelation = {
            "P": "02" + "aa" * 32,
            "P2": "03" + "bb" * 32,
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "aa" * 32 + ":-1",
        }
        assert parse_podle_revelation(revelation) is None

    def test_parse_vout_overflow_rejected(self) -> None:
        """Vout exceeding uint32 max is rejected."""
        revelation = {
            "P": "02" + "aa" * 32,
            "P2": "03" + "bb" * 32,
            "sig": "cc" * 32,
            "e": "dd" * 32,
            "utxo": "aa" * 32 + ":4294967296",
        }
        assert parse_podle_revelation(revelation) is None


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


class TestVerifyPodleBinding:
    """Tests for binding a PoDLE pubkey P to a UTXO scriptPubKey."""

    @staticmethod
    def _pubkey(secret: int = 12345) -> bytes:
        return scalar_mult_g(secret).format(compressed=True)

    def test_p2wpkh_binding_matches(self) -> None:
        from jmcore.bitcoin import hash160

        p = self._pubkey()
        spk = b"\x00\x14" + hash160(p)
        assert verify_podle_binding(p, spk) == (True, "")
        # hex form is accepted too
        assert verify_podle_binding(p, spk.hex()) == (True, "")

    def test_p2wpkh_binding_mismatch(self) -> None:
        p = self._pubkey()
        spk = b"\x00\x14" + b"\xab" * 20
        bound, err = verify_podle_binding(p, spk)
        assert not bound
        assert "P2WPKH" in err

    def test_p2pkh_binding_matches(self) -> None:
        from jmcore.bitcoin import hash160

        p = self._pubkey(222)
        spk = b"\x76\xa9\x14" + hash160(p) + b"\x88\xac"
        assert verify_podle_binding(p, spk) == (True, "")

    def test_p2sh_p2wpkh_binding_matches(self) -> None:
        from jmcore.bitcoin import hash160

        p = self._pubkey(333)
        redeem = b"\x00\x14" + hash160(p)
        spk = b"\xa9\x14" + hash160(redeem) + b"\x87"
        assert verify_podle_binding(p, spk) == (True, "")

    def test_unsupported_script_type_rejected(self) -> None:
        p = self._pubkey()
        bound, err = verify_podle_binding(p, b"\x6a\x04dead")
        assert not bound
        assert "Unsupported" in err

    def test_invalid_pubkey_length_rejected(self) -> None:
        bound, err = verify_podle_binding(b"\x02" * 32, b"\x00\x14" + b"\x00" * 20)
        assert not bound
        assert "length" in err

    def test_invalid_hex_scriptpubkey_rejected(self) -> None:
        bound, err = verify_podle_binding(self._pubkey(), "zz")
        assert not bound
        assert "hex" in err


class TestPoDLENonceSecurity:
    """Security properties of the deterministic nonce derivation (ff157b24).

    A deterministic nonce in a Schnorr-style proof is only safe while it stays
    bound to every field the challenge depends on. If two proofs over different
    challenges ever share a nonce, the UTXO private key falls out of the pair.
    """

    PRIVATE_KEY = hashlib.sha256(b"podle-nonce-security").digest()

    @staticmethod
    def _recover_nonce(commitment: PoDLECommitment, private_key: bytes) -> int:
        """Recover k_proof from a proof, given the private key.

        s = k_proof + e * k (mod n), so k_proof = s - e * k (mod n).
        """
        k = int.from_bytes(private_key, "big")
        e = int.from_bytes(commitment.e, "big")
        s = int.from_bytes(commitment.sig, "big")
        return (s - e * k) % SECP256K1_N

    @pytest.mark.parametrize(
        ("message", "expected_nonce"),
        [
            (
                b"sample",
                0xA6E3C57DD01ABE90086538398355DD4C3B17AA873382B0F24D6129493D8AAD60,
            ),
            (
                b"test",
                0xD16B6AE827F17175E040871A1C7EC3500192C4C92677336EC2537ACAEE0008E0,
            ),
        ],
    )
    def test_rfc6979_p256_vectors_validate_shared_hmac_ladder(
        self, message: bytes, expected_nonce: int
    ) -> None:
        """Validate the HMAC ladder against RFC 6979 Appendix A.2.5 NIST P-256 vectors.

        The private scalar, SHA-256 hashes, and candidates are below both the P-256 and
        secp256k1 orders, so these unreduced inputs validate the shared 256-bit ladder.
        """
        private_key = (0xC9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721).to_bytes(
            32, "big"
        )

        nonce = next(podle._rfc6979_nonce_candidates(private_key, hashlib.sha256(message).digest()))

        assert nonce == expected_nonce

    def test_nonce_is_bound_to_nums_index(self) -> None:
        """Two indices reuse one key over different J points, so the nonce must differ.

        This is the key-recovery case. Both proofs share P, and each NUMS index
        yields a different P2 and therefore a different challenge.
        """
        utxo_str = "a" * 64 + ":0"

        first = generate_podle(self.PRIVATE_KEY, utxo_str, index=0)
        second = generate_podle(self.PRIVATE_KEY, utxo_str, index=1)

        assert first.p == second.p
        assert first.p2 != second.p2
        assert self._recover_nonce(first, self.PRIVATE_KEY) != self._recover_nonce(
            second, self.PRIVATE_KEY
        )

    def test_private_key_is_not_recoverable_from_two_proofs(self) -> None:
        """Spell out the attack that nonce reuse would enable.

        Given s1 = k1 + e1*x and s2 = k2 + e2*x, an attacker who knows k1 == k2
        computes x = (s1 - s2) / (e1 - e2) mod n. Assert that solving for x this
        way does not return the real private key.
        """
        utxo_str = "b" * 64 + ":1"
        first = generate_podle(self.PRIVATE_KEY, utxo_str, index=0)
        second = generate_podle(self.PRIVATE_KEY, utxo_str, index=1)

        e1 = int.from_bytes(first.e, "big")
        e2 = int.from_bytes(second.e, "big")
        s1 = int.from_bytes(first.sig, "big")
        s2 = int.from_bytes(second.sig, "big")
        assert e1 != e2, "challenges must differ or the recovery below is undefined"

        recovered = ((s1 - s2) * pow(e1 - e2, -1, SECP256K1_N)) % SECP256K1_N

        assert recovered != int.from_bytes(self.PRIVATE_KEY, "big")

    def test_nonces_are_unique_across_every_transcript(self) -> None:
        """Sweep the realistic transcript space for a single key and find no repeats."""
        nonces: dict[int, tuple[str, int]] = {}

        for utxo_str in ("c" * 64 + ":0", "c" * 64 + ":1", "d" * 64 + ":0"):
            for index in range(10):
                commitment = generate_podle(self.PRIVATE_KEY, utxo_str, index=index)
                nonce = self._recover_nonce(commitment, self.PRIVATE_KEY)
                assert nonce not in nonces, (
                    f"nonce reused between {nonces.get(nonce)} and {(utxo_str, index)}"
                )
                nonces[nonce] = (utxo_str, index)

        assert len(nonces) == 30

    def test_nonce_depends_on_every_transcript_field(self) -> None:
        """Each field in the transcript must change the nonce on its own."""
        p_bytes = b"\x02" + bytes([0x11]) * 32
        p2_bytes = b"\x02" + bytes([0x22]) * 32
        baseline_args = (self.PRIVATE_KEY, "e" * 64 + ":0", 0, p_bytes, p2_bytes)
        baseline = next(podle._podle_nonce_candidates(*baseline_args))

        variants = {
            "utxo": (self.PRIVATE_KEY, "e" * 64 + ":1", 0, p_bytes, p2_bytes),
            "index": (self.PRIVATE_KEY, "e" * 64 + ":0", 1, p_bytes, p2_bytes),
            "p": (self.PRIVATE_KEY, "e" * 64 + ":0", 0, b"\x02" + bytes([0x33]) * 32, p2_bytes),
            "p2": (self.PRIVATE_KEY, "e" * 64 + ":0", 0, p_bytes, b"\x02" + bytes([0x44]) * 32),
            "key": (bytes([7] * 32), "e" * 64 + ":0", 0, p_bytes, p2_bytes),
        }

        for field, args in variants.items():
            assert next(podle._podle_nonce_candidates(*args)) != baseline, (
                f"nonce ignores the {field} field"
            )
