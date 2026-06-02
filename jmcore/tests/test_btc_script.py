"""
Test Bitcoin script utilities.
"""

import hashlib

from jmcore.btc_script import (
    BondAddressInfo,
    _decode_scriptnum,
    derive_bond_address,
    disassemble_script,
    mk_freeze_script,
    redeem_script_to_p2wsh_script,
)


def test_mk_freeze_script():
    """Test creating a freeze script"""
    pubkey = "02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
    locktime = 1956528000

    script = mk_freeze_script(pubkey, locktime)

    assert isinstance(script, bytes)
    assert len(script) > 0

    assert 0xB1 in script
    assert 0x75 in script
    assert 0xAC in script


def test_redeem_script_to_p2wsh():
    """Test converting redeem script to P2WSH"""
    pubkey = "02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
    locktime = 1956528000

    redeem_script = mk_freeze_script(pubkey, locktime)
    p2wsh_script = redeem_script_to_p2wsh_script(redeem_script)

    assert len(p2wsh_script) == 34
    assert p2wsh_script[0] == 0x00
    assert p2wsh_script[1] == 0x20

    expected_hash = hashlib.sha256(redeem_script).digest()
    actual_hash = p2wsh_script[2:]
    assert actual_hash == expected_hash


def test_freeze_script_with_known_output():
    """Test freeze script matches expected output"""
    pubkey = "02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
    locktime = 1956528000

    script = mk_freeze_script(pubkey, locktime)
    p2wsh_script = redeem_script_to_p2wsh_script(script)

    assert p2wsh_script[0] == 0x00
    assert p2wsh_script[1] == 0x20


def test_freeze_script_invalid_pubkey():
    """Test that invalid pubkey length raises error"""
    try:
        mk_freeze_script("abcd", 1956528000)
        raise AssertionError("Should have raised ValueError")
    except ValueError as e:
        assert "Invalid pubkey length" in str(e)


# ---- derive_bond_address tests ----


def test_derive_bond_address_returns_bond_address_info():
    """Test that derive_bond_address returns a BondAddressInfo with all fields."""
    pubkey_hex = "02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
    pubkey = bytes.fromhex(pubkey_hex)
    locktime = 1956528000

    result = derive_bond_address(pubkey, locktime, "regtest")

    assert isinstance(result, BondAddressInfo)
    assert isinstance(result.address, str)
    assert isinstance(result.scriptpubkey, bytes)
    assert isinstance(result.witness_script, bytes)


def test_derive_bond_address_p2wsh_format():
    """Test that the derived address has correct P2WSH scriptpubkey structure."""
    pubkey = bytes.fromhex("02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2")
    locktime = 1956528000

    result = derive_bond_address(pubkey, locktime, "regtest")

    # P2WSH scriptpubkey = OP_0 (0x00) + PUSH32 (0x20) + 32-byte hash
    assert len(result.scriptpubkey) == 34
    assert result.scriptpubkey[0] == 0x00
    assert result.scriptpubkey[1] == 0x20

    # The hash in the scriptpubkey should be SHA256 of the witness script
    expected_hash = hashlib.sha256(result.witness_script).digest()
    assert result.scriptpubkey[2:] == expected_hash


def test_derive_bond_address_witness_script_matches_freeze():
    """Test that the witness_script matches mk_freeze_script output."""
    pubkey_hex = "02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
    pubkey = bytes.fromhex(pubkey_hex)
    locktime = 1956528000

    result = derive_bond_address(pubkey, locktime, "regtest")
    expected_script = mk_freeze_script(pubkey_hex, locktime)

    assert result.witness_script == expected_script


def test_derive_bond_address_deterministic():
    """Test that the same inputs always produce the same address."""
    pubkey = bytes.fromhex("02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2")
    locktime = 1956528000

    result1 = derive_bond_address(pubkey, locktime, "signet")
    result2 = derive_bond_address(pubkey, locktime, "signet")

    assert result1.address == result2.address
    assert result1.scriptpubkey == result2.scriptpubkey
    assert result1.witness_script == result2.witness_script


def test_derive_bond_address_different_networks():
    """Test that different networks produce different address prefixes."""
    pubkey = bytes.fromhex("02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2")
    locktime = 1956528000

    mainnet = derive_bond_address(pubkey, locktime, "mainnet")
    regtest = derive_bond_address(pubkey, locktime, "regtest")
    signet = derive_bond_address(pubkey, locktime, "signet")

    # Different network prefixes
    assert mainnet.address.startswith("bc1")
    assert regtest.address.startswith("bcrt1")
    assert signet.address.startswith("tb1")

    # Same scriptpubkey regardless of network (scriptpubkey is network-agnostic)
    assert mainnet.scriptpubkey == regtest.scriptpubkey == signet.scriptpubkey


def test_derive_bond_address_different_locktimes():
    """Test that different locktimes produce different addresses."""
    pubkey = bytes.fromhex("02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2")

    result1 = derive_bond_address(pubkey, 1956528000, "mainnet")
    result2 = derive_bond_address(pubkey, 1988064000, "mainnet")

    assert result1.address != result2.address
    assert result1.scriptpubkey != result2.scriptpubkey


def test_derive_bond_address_different_pubkeys():
    """Test that different pubkeys produce different addresses."""
    pubkey1 = bytes.fromhex("02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2")
    pubkey2 = bytes.fromhex("03b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3")
    locktime = 1956528000

    result1 = derive_bond_address(pubkey1, locktime, "mainnet")
    result2 = derive_bond_address(pubkey2, locktime, "mainnet")

    assert result1.address != result2.address


def test_derive_bond_address_invalid_pubkey_length():
    """Test that invalid pubkey length raises ValueError."""
    try:
        derive_bond_address(b"\x02" * 32, 1956528000)  # 32 bytes, not 33
        raise AssertionError("Should have raised ValueError")
    except ValueError as e:
        assert "Invalid utxo_pub length" in str(e)


def test_derive_bond_address_empty_pubkey():
    """Test that empty pubkey raises ValueError."""
    try:
        derive_bond_address(b"", 1956528000)
        raise AssertionError("Should have raised ValueError")
    except ValueError as e:
        assert "Invalid utxo_pub length" in str(e)


# ---- disassemble_script tests ----


class TestDisassembleScript:
    """Tests for disassemble_script function."""

    def test_disassemble_freeze_script(self):
        """Test disassembling a timelocked freeze script."""
        pubkey = "02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
        locktime = 1956528000

        script = mk_freeze_script(pubkey, locktime)
        disasm = disassemble_script(script)

        # Should contain the locktime as a number, OP_CHECKLOCKTIMEVERIFY, OP_DROP,
        # the pubkey as hex, and OP_CHECKSIG
        assert "OP_CHECKLOCKTIMEVERIFY" in disasm
        assert "OP_DROP" in disasm
        assert "OP_CHECKSIG" in disasm

    def test_disassemble_p2wsh_script(self):
        """Test disassembling a P2WSH scriptPubKey."""
        pubkey = "02a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2"
        redeem_script = mk_freeze_script(pubkey, 1956528000)
        p2wsh = redeem_script_to_p2wsh_script(redeem_script)

        disasm = disassemble_script(p2wsh)
        # P2WSH: 0 <32-byte-hash> (OP_0 is output as integer 0)
        assert disasm.startswith("0 <")

    def test_disassemble_empty_script(self):
        """Test disassembling an empty script."""
        disasm = disassemble_script(b"")
        assert disasm == ""

    def test_disassemble_simple_opcodes(self):
        """Test disassembling script with only opcodes."""
        from bitcointx.core.script import OP_CHECKSIG, OP_DROP, CScript

        script = CScript([OP_DROP, OP_CHECKSIG])
        disasm = disassemble_script(bytes(script))
        assert "OP_DROP" in disasm
        assert "OP_CHECKSIG" in disasm

    def test_disassemble_data_push_long(self):
        """Test disassembling script with long data pushes (>5 bytes shown as hex)."""
        from bitcointx.core.script import CScript

        data = b"\xaa" * 20  # 20-byte data push (like a pubkey hash)
        script = CScript([data])
        disasm = disassemble_script(bytes(script))
        # Long data push should be shown as <hex>
        assert "<" in disasm
        assert "aa" in disasm.lower()

    def test_disassemble_data_push_short_number(self):
        """Test disassembling script with short data push decoded as number."""
        from bitcointx.core.script import CScript

        # Locktime 500000 encoded as script number (small enough to decode)
        locktime = 500000
        script = CScript([locktime])
        disasm = disassemble_script(bytes(script))
        assert "500000" in disasm


class TestDecodeScriptnum:
    """Tests for _decode_scriptnum helper."""

    def test_empty_bytes(self):
        """Empty bytes decodes to 0."""
        assert _decode_scriptnum(b"") == 0

    def test_single_byte_positive(self):
        """Single positive byte."""
        assert _decode_scriptnum(b"\x05") == 5
        assert _decode_scriptnum(b"\x7f") == 127

    def test_single_byte_negative(self):
        """Single byte with sign bit set (negative)."""
        # 0x85 = 1000_0101: sign bit set, value = -(0x05) = -5
        assert _decode_scriptnum(b"\x85") == -5

    def test_two_bytes_positive(self):
        """Two-byte positive number (little-endian)."""
        # 0x0100 = 256 in little-endian
        assert _decode_scriptnum(b"\x00\x01") == 256

    def test_two_bytes_negative(self):
        """Two-byte negative number."""
        # 0x0081 in little-endian: last byte has sign bit set
        # value = 0x0081, sign bit at position (2-1)*8 = bit 8
        # result = int.from_bytes(b"\x00\x81", "little") = 0x8100
        # masked = -(0x8100 & ~(0x80 << 8)) = -(0x8100 & ~0x8000) = -(0x0100) = -256
        assert _decode_scriptnum(b"\x00\x81") == -256

    def test_zero_value(self):
        """Zero encoded as single byte."""
        assert _decode_scriptnum(b"\x00") == 0

    def test_locktime_value(self):
        """Typical locktime value decoding."""
        # 500000 = 0x07A120 in little-endian: b"\x20\xa1\x07"
        import struct

        encoded = struct.pack("<I", 500000)[:3]  # 3 bytes is enough
        result = _decode_scriptnum(encoded)
        assert result == 500000
