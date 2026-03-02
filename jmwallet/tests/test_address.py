"""Tests for bech32 address encoding, decoding, and checksum verification.

These tests ensure that:
- Valid bech32 addresses are accepted and decoded correctly.
- Addresses with invalid checksums (e.g. single-char typos) are rejected.
- Addresses with wrong HRP (wrong network) are rejected.
- Case-insensitive decoding works (uppercase addresses from QR codes).
- The encoding round-trips correctly with decoding.
"""

from __future__ import annotations

import pytest

from jmwallet.wallet.address import bech32_decode, bech32_encode, convertbits

# ---------------------------------------------------------------------------
# Known-good test vectors from BIP173 / BIP350
# ---------------------------------------------------------------------------

# (hrp, address, expected_witness_version, expected_witness_program_hex)
VALID_ADDRESSES: list[tuple[str, str, int, str]] = [
    # Mainnet P2WPKH
    (
        "bc",
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",
        0,
        "751e76e8199196d454941c45d1b3a323f1433bd6",
    ),
    # Regtest P2WPKH
    (
        "bcrt",
        "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
        0,
        "751e76e8199196d454941c45d1b3a323f1433bd6",
    ),
    # Mainnet P2WSH
    (
        "bc",
        "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3",
        0,
        "1863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262",
    ),
]

# Addresses with a single-character substitution (checksum should fail)
INVALID_CHECKSUM_ADDRESSES: list[tuple[str, str]] = [
    # Last char changed from '4' equivalent to something else
    ("bc", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t5"),
    # One char changed in the middle
    ("bc", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3s4"),
    # Regtest - last char changed
    ("bcrt", "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt081"),
]


class TestBech32Decode:
    """Tests for bech32_decode (checksum-verifying address decoding)."""

    @pytest.mark.parametrize(
        ("hrp", "address", "expected_version", "expected_program_hex"),
        VALID_ADDRESSES,
        ids=["mainnet-p2wpkh", "regtest-p2wpkh", "mainnet-p2wsh"],
    )
    def test_valid_address_decodes(
        self,
        hrp: str,
        address: str,
        expected_version: int,
        expected_program_hex: str,
    ) -> None:
        witver, witprog = bech32_decode(hrp, address)
        assert witver == expected_version
        assert witprog is not None
        assert bytes(witprog).hex() == expected_program_hex

    @pytest.mark.parametrize(
        ("hrp", "address"),
        INVALID_CHECKSUM_ADDRESSES,
        ids=["mainnet-last-char", "mainnet-mid-char", "regtest-last-char"],
    )
    def test_invalid_checksum_rejected(self, hrp: str, address: str) -> None:
        """A single-character typo must be caught by checksum verification.

        This is the core security property: the old code silently stripped
        the checksum and would have accepted these addresses, sending funds
        to an unspendable output.
        """
        witver, witprog = bech32_decode(hrp, address)
        assert witver is None
        assert witprog is None

    def test_wrong_hrp_rejected(self) -> None:
        """A mainnet address must not decode with a regtest HRP."""
        witver, witprog = bech32_decode("bcrt", "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
        assert witver is None

    def test_case_insensitive(self) -> None:
        """Uppercase addresses (from QR decoders) must decode correctly."""
        witver, witprog = bech32_decode("bc", "BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4")
        assert witver == 0
        assert witprog is not None
        assert bytes(witprog).hex() == "751e76e8199196d454941c45d1b3a323f1433bd6"

    def test_garbage_input_rejected(self) -> None:
        witver, witprog = bech32_decode("bc", "notanaddress")
        assert witver is None

    def test_empty_string_rejected(self) -> None:
        witver, witprog = bech32_decode("bc", "")
        assert witver is None

    def test_mixed_case_rejected(self) -> None:
        """Mixed case is invalid per BIP173."""
        witver, witprog = bech32_decode("bc", "bc1Qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4")
        assert witver is None


class TestBech32Encode:
    """Tests for bech32_encode."""

    def test_encode_p2wpkh(self) -> None:
        program = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
        addr = bech32_encode("bc", 0, program)
        assert addr == "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"

    def test_encode_regtest(self) -> None:
        program = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
        addr = bech32_encode("bcrt", 0, program)
        assert addr == "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"

    def test_encode_p2wsh(self) -> None:
        program = bytes.fromhex("1863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262")
        addr = bech32_encode("bc", 0, program)
        assert addr == "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"


class TestRoundTrip:
    """Encoding then decoding must produce the original witness program."""

    @pytest.mark.parametrize(
        ("hrp", "address", "expected_version", "expected_program_hex"),
        VALID_ADDRESSES,
        ids=["mainnet-p2wpkh", "regtest-p2wpkh", "mainnet-p2wsh"],
    )
    def test_decode_encode_roundtrip(
        self,
        hrp: str,
        address: str,
        expected_version: int,
        expected_program_hex: str,
    ) -> None:
        witver, witprog = bech32_decode(hrp, address)
        assert witver is not None and witprog is not None
        re_encoded = bech32_encode(hrp, witver, bytes(witprog))
        assert re_encoded == address


class TestConvertbits:
    """Tests for the convertbits compatibility wrapper."""

    def test_8_to_5_roundtrip(self) -> None:
        original = bytes.fromhex("751e76e8199196d454941c45d1b3a323f1433bd6")
        five_bit = convertbits(original, 8, 5)
        recovered = convertbits(bytes(five_bit), 5, 8, pad=False)
        assert bytes(recovered) == original
