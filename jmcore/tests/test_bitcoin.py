"""
Tests for jmcore.bitcoin module.
"""

import base64
import os
import struct

import pytest

from jmcore.bitcoin import (
    PSBT_MAGIC,
    BIP32Derivation,
    PSBTInput,
    TxInput,
    TxOutput,
    address_to_scriptpubkey,
    btc_to_sats,
    calculate_relative_fee,
    calculate_sweep_amount,
    calculate_tx_vsize,
    create_p2wpkh_script_code,
    create_psbt,
    decode_varint,
    encode_varint,
    estimate_vsize,
    format_amount,
    get_address_type,
    get_txid,
    hash160,
    hash256,
    parse_derivation_path,
    parse_transaction,
    psbt_to_base64,
    pubkey_to_p2wpkh_address,
    pubkey_to_p2wpkh_script,
    sats_to_btc,
    script_to_p2wsh_address,
    script_to_p2wsh_scriptpubkey,
    scriptpubkey_to_address,
    serialize_input,
    serialize_outpoint,
    serialize_transaction,
    validate_satoshi_amount,
)


def create_synthetic_segwit_tx(num_inputs: int, num_outputs: int) -> bytes:
    """
    Create a synthetic SegWit transaction for testing vsize calculation.

    This creates a valid transaction structure with random data for testing.
    """
    parts = []

    # Version (4 bytes)
    parts.append(b"\x02\x00\x00\x00")

    # SegWit marker and flag
    parts.append(b"\x00\x01")

    # Input count (varint)
    parts.append(bytes([num_inputs]))

    # Inputs: each has txid(32) + vout(4) + scriptSig_len(1, =0 for segwit) + seq(4)
    for _ in range(num_inputs):
        parts.append(os.urandom(32))  # Random txid
        parts.append(b"\x00\x00\x00\x00")  # vout = 0
        parts.append(b"\x00")  # Empty scriptSig
        parts.append(b"\xff\xff\xff\xff")  # sequence

    # Output count (varint)
    parts.append(bytes([num_outputs]))

    # Outputs: each has value(8) + script_len(1) + P2WPKH script(22)
    for _ in range(num_outputs):
        parts.append(os.urandom(8))  # Random value
        parts.append(b"\x16")  # Script length = 22
        parts.append(b"\x00\x14")  # OP_0 PUSH20
        parts.append(os.urandom(20))  # Random pubkey hash

    # Witness data: for each input, standard P2WPKH witness
    for _ in range(num_inputs):
        parts.append(b"\x02")  # 2 stack items
        # Signature (~71-72 bytes, use 71)
        parts.append(b"\x47")  # 71 bytes
        parts.append(os.urandom(71))
        # Compressed pubkey (33 bytes)
        parts.append(b"\x21")  # 33 bytes
        parts.append(b"\x02")  # Compressed pubkey prefix
        parts.append(os.urandom(32))

    # Locktime (4 bytes)
    parts.append(b"\x00\x00\x00\x00")

    return b"".join(parts)


def create_synthetic_legacy_tx(num_inputs: int, num_outputs: int) -> bytes:
    """
    Create a synthetic legacy (non-SegWit) transaction for testing.
    """
    parts = []

    # Version (4 bytes)
    parts.append(b"\x01\x00\x00\x00")

    # Input count (varint)
    parts.append(bytes([num_inputs]))

    # Inputs: each has txid(32) + vout(4) + scriptSig + seq(4)
    for _ in range(num_inputs):
        parts.append(os.urandom(32))  # Random txid
        parts.append(b"\x00\x00\x00\x00")  # vout = 0
        # P2PKH scriptSig: sig(~71) + pubkey(33) + push opcodes
        parts.append(b"\x6a")  # Script length = 106
        parts.append(b"\x47")  # Push 71 bytes (signature)
        parts.append(os.urandom(71))
        parts.append(b"\x21")  # Push 33 bytes (pubkey)
        parts.append(b"\x02")  # Compressed pubkey prefix
        parts.append(os.urandom(32))
        parts.append(b"\xff\xff\xff\xff")  # sequence

    # Output count (varint)
    parts.append(bytes([num_outputs]))

    # Outputs: each has value(8) + script_len(1) + P2PKH script(25)
    for _ in range(num_outputs):
        parts.append(os.urandom(8))  # Random value
        parts.append(b"\x19")  # Script length = 25
        parts.append(b"\x76\xa9\x14")  # OP_DUP OP_HASH160 PUSH20
        parts.append(os.urandom(20))  # Random pubkey hash
        parts.append(b"\x88\xac")  # OP_EQUALVERIFY OP_CHECKSIG

    # Locktime (4 bytes)
    parts.append(b"\x00\x00\x00\x00")

    return b"".join(parts)


class TestCalculateTxVsize:
    """Tests for calculate_tx_vsize function."""

    def test_calculate_vsize_segwit_single_input_output(self) -> None:
        """Test vsize calculation for minimal SegWit transaction."""
        tx_bytes = create_synthetic_segwit_tx(1, 1)

        vsize = calculate_tx_vsize(tx_bytes)

        # For a SegWit transaction, vsize should be less than serialized size
        assert vsize < len(tx_bytes)

        # 1 P2WPKH input: ~68 vbytes, 1 P2WPKH output: ~31 vbytes, overhead: ~11
        # Expected: ~110 vbytes
        expected = estimate_vsize(["p2wpkh"], ["p2wpkh"])
        # Allow some variance due to signature size differences
        assert abs(vsize - expected) < 15, f"vsize {vsize} too far from expected {expected}"

    def test_calculate_vsize_segwit_coinjoin_like(self) -> None:
        """Test vsize calculation for CoinJoin-like transaction (10 in, 13 out)."""
        tx_bytes = create_synthetic_segwit_tx(10, 13)

        vsize = calculate_tx_vsize(tx_bytes)

        # For a SegWit transaction, vsize should be less than serialized size
        assert vsize < len(tx_bytes)

        # Expected: 10*68 + 13*31 + 11 = 1094 vbytes
        expected = estimate_vsize(["p2wpkh"] * 10, ["p2wpkh"] * 13)
        # Allow some variance
        assert abs(vsize - expected) < 30, f"vsize {vsize} too far from expected {expected}"

    def test_calculate_vsize_scales_with_inputs(self) -> None:
        """Test that vsize scales properly with number of inputs."""
        vsize_1 = calculate_tx_vsize(create_synthetic_segwit_tx(1, 1))
        vsize_2 = calculate_tx_vsize(create_synthetic_segwit_tx(2, 1))
        vsize_5 = calculate_tx_vsize(create_synthetic_segwit_tx(5, 1))

        # Each additional P2WPKH input adds ~68 vbytes
        diff_1_to_2 = vsize_2 - vsize_1
        diff_2_to_5 = vsize_5 - vsize_2

        assert 60 < diff_1_to_2 < 80, f"Input diff {diff_1_to_2} outside range"
        # 3 inputs difference
        assert 180 < diff_2_to_5 < 240, f"3-input diff {diff_2_to_5} outside range"

    def test_calculate_vsize_scales_with_outputs(self) -> None:
        """Test that vsize scales properly with number of outputs."""
        vsize_1 = calculate_tx_vsize(create_synthetic_segwit_tx(1, 1))
        vsize_2 = calculate_tx_vsize(create_synthetic_segwit_tx(1, 2))
        vsize_5 = calculate_tx_vsize(create_synthetic_segwit_tx(1, 5))

        # Each additional P2WPKH output adds ~31 vbytes
        diff_1_to_2 = vsize_2 - vsize_1
        diff_2_to_5 = vsize_5 - vsize_2

        assert 25 < diff_1_to_2 < 40, f"Output diff {diff_1_to_2} outside range"
        # 3 outputs difference
        assert 80 < diff_2_to_5 < 120, f"3-output diff {diff_2_to_5} outside range"

    def test_calculate_vsize_legacy_transaction(self) -> None:
        """Test vsize calculation for legacy (non-SegWit) transaction."""
        tx_bytes = create_synthetic_legacy_tx(1, 1)

        vsize = calculate_tx_vsize(tx_bytes)

        # For legacy transactions, vsize equals serialized size
        assert vsize == len(tx_bytes)

    def test_calculate_vsize_legacy_multiple_inputs(self) -> None:
        """Test legacy transaction vsize scales with inputs."""
        vsize_1 = calculate_tx_vsize(create_synthetic_legacy_tx(1, 1))
        vsize_3 = calculate_tx_vsize(create_synthetic_legacy_tx(3, 1))

        # For legacy, each P2PKH input adds ~148 bytes
        diff = vsize_3 - vsize_1
        # 2 additional inputs
        assert 280 < diff < 320, f"Legacy input diff {diff} outside range"


class TestEstimateVsize:
    """Tests for estimate_vsize function."""

    def test_estimate_vsize_p2wpkh(self) -> None:
        """Test vsize estimation for P2WPKH inputs/outputs."""
        vsize = estimate_vsize(["p2wpkh"], ["p2wpkh"])
        # 1 input (68) + 1 output (31) + overhead (~11) = ~110 vbytes
        assert 100 < vsize < 120

    def test_estimate_vsize_multiple_inputs(self) -> None:
        """Test vsize estimation scales with inputs."""
        vsize_1 = estimate_vsize(["p2wpkh"], ["p2wpkh"])
        vsize_2 = estimate_vsize(["p2wpkh", "p2wpkh"], ["p2wpkh"])

        # Adding one input should add ~68 vbytes
        diff = vsize_2 - vsize_1
        assert 60 < diff < 75

    def test_estimate_vsize_coinjoin_like(self) -> None:
        """Test vsize estimation for CoinJoin-like transaction."""
        # 10 inputs, 13 outputs
        vsize = estimate_vsize(["p2wpkh"] * 10, ["p2wpkh"] * 13)

        # 10 * 68 + 13 * 31 + 11 = 680 + 403 + 11 = 1094 vbytes
        expected = 10 * 68 + 13 * 31 + 11
        assert vsize == expected


# =============================================================================
# PSBT Tests
# =============================================================================

# Deterministic test data for reproducible PSBT tests
TEST_TXID = "a" * 64  # 32 bytes of 0xaa when reversed
TEST_PUBKEY_HEX = "02" + "bb" * 32  # Fake compressed pubkey
TEST_LOCKTIME = 1672531200  # 2023-01-01 00:00:00 UTC


def _make_witness_script() -> bytes:
    """Create a deterministic CLTV witness script for testing."""
    from jmcore.btc_script import mk_freeze_script

    return mk_freeze_script(TEST_PUBKEY_HEX, TEST_LOCKTIME)


def _make_p2wsh_scriptpubkey(witness_script: bytes) -> bytes:
    """Derive P2WSH scriptPubKey from witness script."""
    return script_to_p2wsh_scriptpubkey(witness_script)


class TestPSBTMagic:
    """Verify PSBT magic constant."""

    def test_magic_bytes(self) -> None:
        assert PSBT_MAGIC == b"psbt\xff"

    def test_magic_length(self) -> None:
        assert len(PSBT_MAGIC) == 5


class TestCreatePSBT:
    """Tests for create_psbt function."""

    def test_psbt_starts_with_magic(self) -> None:
        """PSBT must begin with the BIP-174 magic bytes."""
        ws = _make_witness_script()
        spk = _make_p2wsh_scriptpubkey(ws)

        tx_in = TxInput.from_hex(TEST_TXID, 0, sequence=0xFFFFFFFE, value=100_000)
        tx_out = TxOutput(value=99_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        pi = PSBTInput(
            witness_utxo_value=100_000,
            witness_utxo_script=spk,
            witness_script=ws,
        )

        psbt = create_psbt(
            version=2,
            inputs=[tx_in],
            outputs=[tx_out],
            locktime=TEST_LOCKTIME,
            psbt_inputs=[pi],
        )
        assert psbt[:5] == PSBT_MAGIC

    def test_psbt_contains_unsigned_tx(self) -> None:
        """Global map must contain the unsigned transaction."""
        ws = _make_witness_script()
        spk = _make_p2wsh_scriptpubkey(ws)

        tx_in = TxInput.from_hex(TEST_TXID, 0, sequence=0xFFFFFFFE, value=100_000)
        tx_out = TxOutput(value=99_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        pi = PSBTInput(
            witness_utxo_value=100_000,
            witness_utxo_script=spk,
            witness_script=ws,
        )

        psbt = create_psbt(
            version=2,
            inputs=[tx_in],
            outputs=[tx_out],
            locktime=TEST_LOCKTIME,
            psbt_inputs=[pi],
        )

        # After magic (5 bytes), the first key-value pair should be the unsigned tx
        # Key: <varint 1> <0x00>  (type 0x00, global unsigned tx)
        assert psbt[5] == 0x01  # key length = 1
        assert psbt[6] == 0x00  # key type = PSBT_GLOBAL_UNSIGNED_TX

        # The unsigned tx should be parseable
        # Read value length
        val_len, offset = decode_varint(psbt, 7)
        unsigned_tx_bytes = psbt[offset : offset + val_len]

        # Parse it and verify structure
        parsed = parse_transaction(unsigned_tx_bytes.hex())
        assert parsed.version == 2
        assert len(parsed.inputs) == 1
        assert len(parsed.outputs) == 1
        assert parsed.locktime == TEST_LOCKTIME
        assert parsed.inputs[0].sequence == 0xFFFFFFFE
        assert parsed.outputs[0].value == 99_000

    def test_psbt_roundtrip_base64(self) -> None:
        """PSBT should survive base64 encode/decode roundtrip."""
        ws = _make_witness_script()
        spk = _make_p2wsh_scriptpubkey(ws)

        tx_in = TxInput.from_hex(TEST_TXID, 0, sequence=0xFFFFFFFE, value=50_000)
        tx_out = TxOutput(value=49_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        pi = PSBTInput(
            witness_utxo_value=50_000,
            witness_utxo_script=spk,
            witness_script=ws,
        )

        psbt = create_psbt(
            version=2,
            inputs=[tx_in],
            outputs=[tx_out],
            locktime=TEST_LOCKTIME,
            psbt_inputs=[pi],
        )

        b64 = psbt_to_base64(psbt)
        decoded = base64.b64decode(b64)
        assert decoded == psbt

    def test_psbt_mismatched_inputs_raises(self) -> None:
        """create_psbt must raise ValueError when input counts mismatch."""
        import pytest

        tx_in = TxInput.from_hex(TEST_TXID, 0, sequence=0xFFFFFFFE, value=100_000)
        tx_out = TxOutput(value=99_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)

        with pytest.raises(ValueError, match="same length"):
            create_psbt(
                version=2,
                inputs=[tx_in],
                outputs=[tx_out],
                locktime=0,
                psbt_inputs=[],  # Empty - mismatch!
            )

    def test_psbt_multiple_inputs(self) -> None:
        """PSBT with multiple inputs should have per-input maps for each."""
        ws = _make_witness_script()
        spk = _make_p2wsh_scriptpubkey(ws)

        tx_in1 = TxInput.from_hex("aa" * 32, 0, sequence=0xFFFFFFFE, value=50_000)
        tx_in2 = TxInput.from_hex("bb" * 32, 1, sequence=0xFFFFFFFE, value=60_000)
        tx_out = TxOutput(value=109_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)

        pi1 = PSBTInput(
            witness_utxo_value=50_000,
            witness_utxo_script=spk,
            witness_script=ws,
        )
        pi2 = PSBTInput(
            witness_utxo_value=60_000,
            witness_utxo_script=spk,
            witness_script=ws,
        )

        psbt = create_psbt(
            version=2,
            inputs=[tx_in1, tx_in2],
            outputs=[tx_out],
            locktime=TEST_LOCKTIME,
            psbt_inputs=[pi1, pi2],
        )

        # Verify the unsigned tx has 2 inputs
        # Skip magic, read global unsigned tx
        assert psbt[5] == 0x01  # key len
        assert psbt[6] == 0x00  # key type
        val_len, offset = decode_varint(psbt, 7)
        unsigned_tx_bytes = psbt[offset : offset + val_len]

        parsed = parse_transaction(unsigned_tx_bytes.hex())
        assert len(parsed.inputs) == 2
        assert parsed.inputs[0].txid == "aa" * 32
        assert parsed.inputs[1].txid == "bb" * 32
        assert parsed.inputs[1].vout == 1

    def test_psbt_witness_script_included(self) -> None:
        """Per-input map must contain the witness script."""
        ws = _make_witness_script()
        spk = _make_p2wsh_scriptpubkey(ws)

        tx_in = TxInput.from_hex(TEST_TXID, 0, sequence=0xFFFFFFFE, value=100_000)
        tx_out = TxOutput(value=99_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        pi = PSBTInput(
            witness_utxo_value=100_000,
            witness_utxo_script=spk,
            witness_script=ws,
        )

        psbt = create_psbt(
            version=2,
            inputs=[tx_in],
            outputs=[tx_out],
            locktime=TEST_LOCKTIME,
            psbt_inputs=[pi],
        )

        # The witness script bytes must appear in the PSBT
        assert ws in psbt

    def test_psbt_locktime_in_unsigned_tx(self) -> None:
        """The unsigned tx inside the PSBT must have the correct nLockTime."""
        ws = _make_witness_script()
        spk = _make_p2wsh_scriptpubkey(ws)
        custom_locktime = 1735689600  # 2025-01-01

        tx_in = TxInput.from_hex(TEST_TXID, 0, sequence=0xFFFFFFFE, value=100_000)
        tx_out = TxOutput(value=99_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        pi = PSBTInput(
            witness_utxo_value=100_000,
            witness_utxo_script=spk,
            witness_script=ws,
        )

        psbt = create_psbt(
            version=2,
            inputs=[tx_in],
            outputs=[tx_out],
            locktime=custom_locktime,
            psbt_inputs=[pi],
        )

        # Parse the embedded unsigned tx to verify locktime
        val_len, offset = decode_varint(psbt, 7)
        unsigned_tx_bytes = psbt[offset : offset + val_len]
        parsed = parse_transaction(unsigned_tx_bytes.hex())
        assert parsed.locktime == custom_locktime

    def test_psbt_sequence_enables_locktime(self) -> None:
        """Input sequence in unsigned tx must be < 0xFFFFFFFF for locktime."""
        ws = _make_witness_script()
        spk = _make_p2wsh_scriptpubkey(ws)

        tx_in = TxInput.from_hex(TEST_TXID, 0, sequence=0xFFFFFFFE, value=100_000)
        tx_out = TxOutput(value=99_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        pi = PSBTInput(
            witness_utxo_value=100_000,
            witness_utxo_script=spk,
            witness_script=ws,
        )

        psbt = create_psbt(
            version=2,
            inputs=[tx_in],
            outputs=[tx_out],
            locktime=TEST_LOCKTIME,
            psbt_inputs=[pi],
        )

        val_len, offset = decode_varint(psbt, 7)
        unsigned_tx_bytes = psbt[offset : offset + val_len]
        parsed = parse_transaction(unsigned_tx_bytes.hex())
        assert parsed.inputs[0].sequence == 0xFFFFFFFE


class TestPSBTToBase64:
    """Tests for psbt_to_base64 function."""

    def test_returns_valid_base64(self) -> None:
        """Output must be valid base64 that decodes back to original."""
        raw = PSBT_MAGIC + b"\x00" * 10
        b64 = psbt_to_base64(raw)
        assert base64.b64decode(b64) == raw

    def test_returns_ascii_string(self) -> None:
        """Output must be a pure ASCII string."""
        raw = PSBT_MAGIC + os.urandom(50)
        b64 = psbt_to_base64(raw)
        assert isinstance(b64, str)
        b64.encode("ascii")  # Should not raise


# ---------------------------------------------------------------------------
# Test parse_derivation_path
# ---------------------------------------------------------------------------


class TestParseDerivationPath:
    """Test BIP32 derivation path parsing."""

    def test_standard_bip84_path(self) -> None:
        """Parse m/84'/0'/0'/0/0."""
        result = parse_derivation_path("m/84'/0'/0'/0/0")
        assert result == [
            84 | 0x80000000,  # 84'
            0 | 0x80000000,  # 0'
            0 | 0x80000000,  # 0'
            0,  # 0
            0,  # 0
        ]

    def test_hardened_with_h_suffix(self) -> None:
        """The 'h' suffix should work the same as apostrophe."""
        result = parse_derivation_path("m/84h/0h/0h/0/0")
        assert result == parse_derivation_path("m/84'/0'/0'/0/0")

    def test_without_m_prefix(self) -> None:
        """Path without m/ prefix should still work."""
        result = parse_derivation_path("84'/0'/0'/0/0")
        assert result == parse_derivation_path("m/84'/0'/0'/0/0")

    def test_empty_path(self) -> None:
        """m alone should return empty list."""
        assert parse_derivation_path("m") == []

    def test_non_hardened_path(self) -> None:
        """Non-hardened indices should not have bit 31 set."""
        result = parse_derivation_path("m/0/1/2")
        assert result == [0, 1, 2]

    def test_fidelity_bond_path(self) -> None:
        """Parse the fidelity bond derivation path m/84'/0'/0'/2/0."""
        result = parse_derivation_path("m/84'/0'/0'/2/0")
        expected = [84 | 0x80000000, 0 | 0x80000000, 0 | 0x80000000, 2, 0]
        assert result == expected

    def test_invalid_component_raises(self) -> None:
        """Non-numeric path component should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid path component"):
            parse_derivation_path("m/84'/abc/0'")

    def test_negative_index_raises(self) -> None:
        """Negative indices should raise ValueError."""
        with pytest.raises(ValueError, match="Path index out of range"):
            parse_derivation_path("m/-1/0")


# ---------------------------------------------------------------------------
# Test BIP32Derivation
# ---------------------------------------------------------------------------


class TestBIP32Derivation:
    """Test BIP32Derivation dataclass."""

    def test_valid_derivation(self) -> None:
        """Construct a valid BIP32Derivation."""
        pubkey = bytes.fromhex("02" + "bb" * 32)
        fingerprint = bytes.fromhex("aabbccdd")
        path = [84 | 0x80000000, 0 | 0x80000000, 0 | 0x80000000, 0, 0]
        deriv = BIP32Derivation(pubkey=pubkey, fingerprint=fingerprint, path=path)
        assert deriv.pubkey == pubkey
        assert deriv.fingerprint == fingerprint
        assert deriv.path == path

    def test_invalid_pubkey_length_raises(self) -> None:
        """Pubkey must be exactly 33 bytes."""
        with pytest.raises(ValueError, match="pubkey must be 33 bytes"):
            BIP32Derivation(
                pubkey=b"\x02" + b"\xbb" * 31,
                fingerprint=b"\xaa\xbb\xcc\xdd",
                path=[0],
            )

    def test_invalid_fingerprint_length_raises(self) -> None:
        """Fingerprint must be exactly 4 bytes."""
        with pytest.raises(ValueError, match="fingerprint must be 4 bytes"):
            BIP32Derivation(
                pubkey=b"\x02" + b"\xbb" * 32,
                fingerprint=b"\xaa\xbb",
                path=[0],
            )


# ---------------------------------------------------------------------------
# Test PSBT with BIP32 derivation
# ---------------------------------------------------------------------------


class TestPSBTWithBIP32Derivation:
    """Test that BIP32 derivation info is correctly serialized in PSBTs."""

    def _make_psbt_with_derivation(self, fingerprint: bytes, path: list[int]) -> bytes:
        """Helper: create a PSBT with BIP32 derivation info."""
        pubkey = bytes.fromhex(TEST_PUBKEY_HEX)
        witness_script = _make_witness_script()
        p2wsh_scriptpubkey = _make_p2wsh_scriptpubkey(witness_script)

        deriv = BIP32Derivation(
            pubkey=pubkey,
            fingerprint=fingerprint,
            path=path,
        )

        tx_input = TxInput.from_hex(
            txid=TEST_TXID,
            vout=0,
            sequence=0xFFFFFFFE,
            value=100_000,
            scriptpubkey=p2wsh_scriptpubkey.hex(),
        )
        tx_output = TxOutput.from_hex(
            value=99_000,
            scriptpubkey=p2wsh_scriptpubkey.hex(),
        )

        psbt_input = PSBTInput(
            witness_utxo_value=100_000,
            witness_utxo_script=p2wsh_scriptpubkey,
            witness_script=witness_script,
            sighash_type=1,
            bip32_derivations=[deriv],
        )

        return create_psbt(
            version=2,
            inputs=[tx_input],
            outputs=[tx_output],
            locktime=TEST_LOCKTIME,
            psbt_inputs=[psbt_input],
        )

    def test_psbt_contains_bip32_derivation_key(self) -> None:
        """PSBT should contain the BIP32 derivation key type (0x06)."""
        fingerprint = b"\xaa\xbb\xcc\xdd"
        path = [84 | 0x80000000, 0 | 0x80000000, 0 | 0x80000000, 0, 0]
        raw = self._make_psbt_with_derivation(fingerprint, path)

        # The key for BIP32 derivation is: <varint key_len> <0x06> <33-byte pubkey>
        # key_len = 1 + 33 = 34
        pubkey_bytes = bytes.fromhex(TEST_PUBKEY_HEX)
        bip32_key = bytes([34, 0x06]) + pubkey_bytes
        assert bip32_key in raw

    def test_psbt_contains_fingerprint_and_path(self) -> None:
        """PSBT value should contain the master fingerprint and derivation indices."""
        fingerprint = b"\xaa\xbb\xcc\xdd"
        path = [84 | 0x80000000, 0 | 0x80000000, 0 | 0x80000000, 0, 0]
        raw = self._make_psbt_with_derivation(fingerprint, path)

        # The value is: fingerprint + path indices as LE uint32
        expected_value = fingerprint + b"".join(struct.pack("<I", idx) for idx in path)
        assert expected_value in raw

    def test_psbt_without_derivation_unchanged(self) -> None:
        """PSBT without BIP32 derivation should not contain key type 0x06."""
        witness_script = _make_witness_script()
        p2wsh_scriptpubkey = _make_p2wsh_scriptpubkey(witness_script)

        tx_input = TxInput.from_hex(
            txid=TEST_TXID,
            vout=0,
            sequence=0xFFFFFFFE,
            value=100_000,
            scriptpubkey=p2wsh_scriptpubkey.hex(),
        )
        tx_output = TxOutput.from_hex(
            value=99_000,
            scriptpubkey=p2wsh_scriptpubkey.hex(),
        )

        psbt_input = PSBTInput(
            witness_utxo_value=100_000,
            witness_utxo_script=p2wsh_scriptpubkey,
            witness_script=witness_script,
            sighash_type=1,
            # No bip32_derivations
        )

        raw = create_psbt(
            version=2,
            inputs=[tx_input],
            outputs=[tx_output],
            locktime=TEST_LOCKTIME,
            psbt_inputs=[psbt_input],
        )

        # Key type 0x06 followed by pubkey should NOT appear
        pubkey_bytes = bytes.fromhex(TEST_PUBKEY_HEX)
        bip32_key = bytes([34, 0x06]) + pubkey_bytes
        assert bip32_key not in raw

    def test_roundtrip_with_derivation(self) -> None:
        """PSBT with BIP32 derivation should survive base64 roundtrip."""
        fingerprint = b"\x12\x34\x56\x78"
        path = [84 | 0x80000000, 0, 0]
        raw = self._make_psbt_with_derivation(fingerprint, path)
        b64 = psbt_to_base64(raw)

        decoded = base64.b64decode(b64)
        assert decoded == raw
        assert decoded.startswith(PSBT_MAGIC)


# =============================================================================
# Amount utility tests
# =============================================================================


class TestBtcToSats:
    """Tests for btc_to_sats and sats_to_btc."""

    def test_btc_to_sats_one_btc(self) -> None:
        assert btc_to_sats(1.0) == 100_000_000

    def test_btc_to_sats_fractional(self) -> None:
        """0.0003 BTC should give 30000 sats, not 29999 due to float precision."""
        assert btc_to_sats(0.0003) == 30_000

    def test_sats_to_btc(self) -> None:
        assert sats_to_btc(100_000_000) == 1.0

    def test_sats_to_btc_zero(self) -> None:
        assert sats_to_btc(0) == 0.0


class TestFormatAmount:
    """Tests for format_amount."""

    def test_format_with_unit(self) -> None:
        result = format_amount(1_000_000)
        assert "1,000,000 sats" in result
        assert "0.01000000 BTC" in result

    def test_format_without_unit(self) -> None:
        result = format_amount(1_000_000, include_unit=False)
        assert result == "1,000,000"
        assert "sats" not in result
        assert "BTC" not in result

    def test_format_zero(self) -> None:
        result = format_amount(0, include_unit=False)
        assert result == "0"


class TestValidateSatoshiAmount:
    """Tests for validate_satoshi_amount."""

    def test_valid_amount(self) -> None:
        validate_satoshi_amount(0)
        validate_satoshi_amount(100_000_000)

    def test_non_integer_raises(self) -> None:
        with pytest.raises(TypeError, match="Amount must be an integer"):
            validate_satoshi_amount(1.5)  # type: ignore[arg-type]

    def test_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="Amount cannot be negative"):
            validate_satoshi_amount(-1)


class TestCalculateRelativeFee:
    """Tests for calculate_relative_fee."""

    def test_decimal_fee_rate(self) -> None:
        assert calculate_relative_fee(100_000_000, "0.001") == 100_000

    def test_integer_fee_rate_string(self) -> None:
        """Integer fee_rate like '0' (no decimal point)."""
        assert calculate_relative_fee(100_000, "0") == 0

    def test_integer_fee_rate_multiplier(self) -> None:
        """Integer fee_rate '1' means multiply by 1."""
        assert calculate_relative_fee(100_000, "1") == 100_000

    def test_invalid_integer_fee_rate_raises(self) -> None:
        with pytest.raises(ValueError, match="Fee rate must be decimal string or integer"):
            calculate_relative_fee(100_000, "abc")


class TestCalculateSweepAmount:
    """Tests for calculate_sweep_amount."""

    def test_empty_fees_returns_full_amount(self) -> None:
        assert calculate_sweep_amount(1_000_000, []) == 1_000_000

    def test_single_relative_fee(self) -> None:
        # available / (1 + 0.001) = 1000000 / 1.001 = 999000 (floor)
        result = calculate_sweep_amount(1_000_000, ["0.001"])
        assert result == 999000

    def test_integer_fee_rates(self) -> None:
        """Test sweep with integer fee rate strings (no decimal point)."""
        # With fee_rate "1", total = available / (1 + 1) = 500000
        result = calculate_sweep_amount(1_000_000, ["1"])
        assert result == 500_000

    def test_mixed_decimal_and_integer(self) -> None:
        """Test sweep with a mix of fees including integer fee rates."""
        result = calculate_sweep_amount(1_000_000, ["0.001", "0.002"])
        # available / (1 + 0.001 + 0.002) = 1000000 / 1.003 = 997008 (floor)
        assert result == 997008


# =============================================================================
# Address / scriptPubKey tests
# =============================================================================


class TestPubkeyToP2wpkhAddress:
    """Tests for pubkey_to_p2wpkh_address."""

    def test_invalid_pubkey_length(self) -> None:
        """Non-33-byte pubkey should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid compressed pubkey length"):
            pubkey_to_p2wpkh_address(b"\x02" + b"\x00" * 31)  # 32 bytes

    def test_hex_string_input(self) -> None:
        """Hex string pubkey input should work."""
        pubkey_hex = "02" + "aa" * 32
        addr = pubkey_to_p2wpkh_address(pubkey_hex, "regtest")
        assert addr.startswith("bcrt1q")

    def test_mainnet_address(self) -> None:
        pubkey = b"\x02" + b"\xbb" * 32
        addr = pubkey_to_p2wpkh_address(pubkey, "mainnet")
        assert addr.startswith("bc1q")


class TestAddressToScriptpubkey:
    """Tests for address_to_scriptpubkey."""

    def test_p2wpkh_address(self) -> None:
        """Roundtrip: pubkey -> address -> scriptpubkey."""
        pubkey = b"\x02" + b"\xcc" * 32
        addr = pubkey_to_p2wpkh_address(pubkey, "regtest")
        spk = address_to_scriptpubkey(addr)
        assert spk[0] == 0x00 and spk[1] == 0x14
        assert len(spk) == 22

    def test_p2tr_address(self) -> None:
        """P2TR address should produce OP_1 <32-byte> scriptpubkey."""
        import bech32 as bech32_lib

        # Create a synthetic P2TR address (witness version 1, 32-byte program)
        pubkey_x = b"\xdd" * 32
        addr = bech32_lib.encode("bcrt", 1, pubkey_x)
        assert addr is not None

        spk = address_to_scriptpubkey(addr)
        assert spk[0] == 0x51  # OP_1
        assert spk[1] == 0x20  # PUSH32
        assert spk[2:] == pubkey_x

    def test_p2pkh_address(self) -> None:
        """P2PKH (legacy) address produces correct scriptpubkey."""
        import base58 as b58

        payload = bytes([0x6F]) + b"\xee" * 20  # testnet P2PKH
        addr = b58.b58encode_check(payload).decode("ascii")
        spk = address_to_scriptpubkey(addr)
        # P2PKH: OP_DUP OP_HASH160 PUSH20 <hash> OP_EQUALVERIFY OP_CHECKSIG
        assert spk[0] == 0x76 and spk[1] == 0xA9 and spk[2] == 0x14
        assert spk[-2] == 0x88 and spk[-1] == 0xAC
        assert len(spk) == 25

    def test_p2sh_address(self) -> None:
        """P2SH address produces correct scriptpubkey."""
        import base58 as b58

        payload = bytes([0xC4]) + b"\xff" * 20  # testnet P2SH
        addr = b58.b58encode_check(payload).decode("ascii")
        spk = address_to_scriptpubkey(addr)
        # P2SH: OP_HASH160 PUSH20 <hash> OP_EQUAL
        assert spk[0] == 0xA9 and spk[1] == 0x14
        assert spk[-1] == 0x87
        assert len(spk) == 23


class TestScriptpubkeyToAddress:
    """Tests for scriptpubkey_to_address."""

    def test_p2wpkh_roundtrip(self) -> None:
        pubkey = b"\x02" + b"\xaa" * 32
        addr = pubkey_to_p2wpkh_address(pubkey, "regtest")
        spk = address_to_scriptpubkey(addr)
        recovered = scriptpubkey_to_address(spk, "regtest")
        assert recovered == addr

    def test_p2wsh_scriptpubkey(self) -> None:
        """P2WSH scriptpubkey -> address -> scriptpubkey roundtrip."""
        script_hash = b"\xab" * 32
        spk = bytes([0x00, 0x20]) + script_hash
        addr = scriptpubkey_to_address(spk, "regtest")
        assert addr.startswith("bcrt1q")
        recovered_spk = address_to_scriptpubkey(addr)
        assert recovered_spk == spk

    def test_p2tr_scriptpubkey(self) -> None:
        """P2TR scriptpubkey -> address."""
        x_only_key = b"\xcd" * 32
        spk = bytes([0x51, 0x20]) + x_only_key
        addr = scriptpubkey_to_address(spk, "regtest")
        assert addr.startswith("bcrt1p")

    def test_p2pkh_scriptpubkey(self) -> None:
        """P2PKH scriptpubkey -> address."""
        pkh = b"\xee" * 20
        spk = bytes([0x76, 0xA9, 0x14]) + pkh + bytes([0x88, 0xAC])
        addr = scriptpubkey_to_address(spk, "mainnet")
        assert addr.startswith("1")

    def test_p2sh_scriptpubkey(self) -> None:
        """P2SH scriptpubkey -> address."""
        sh = b"\xff" * 20
        spk = bytes([0xA9, 0x14]) + sh + bytes([0x87])
        addr = scriptpubkey_to_address(spk, "mainnet")
        assert addr.startswith("3")

    def test_unsupported_scriptpubkey_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported scriptPubKey"):
            scriptpubkey_to_address(bytes([0xDE, 0xAD]), "mainnet")


# =============================================================================
# TxInput / TxOutput dict-like access tests
# =============================================================================


class TestTxInputDictAccess:
    """Tests for TxInput.__getitem__ and .get() backward compatibility."""

    def test_getitem_txid(self) -> None:
        inp = TxInput.from_hex("aa" * 32, 0, value=50_000)
        assert inp["txid"] == "aa" * 32

    def test_getitem_vout(self) -> None:
        inp = TxInput.from_hex("bb" * 32, 3)
        assert inp["vout"] == 3

    def test_getitem_scriptsig(self) -> None:
        inp = TxInput.from_hex("cc" * 32, 0, scriptsig="deadbeef")
        assert inp["scriptsig"] == "deadbeef"

    def test_getitem_sequence(self) -> None:
        inp = TxInput.from_hex("dd" * 32, 0, sequence=0xFFFFFFFE)
        assert inp["sequence"] == 0xFFFFFFFE

    def test_getitem_value(self) -> None:
        inp = TxInput.from_hex("ee" * 32, 0, value=12345)
        assert inp["value"] == 12345

    def test_getitem_scriptpubkey(self) -> None:
        inp = TxInput.from_hex("ff" * 32, 0, scriptpubkey="0014" + "ab" * 20)
        assert inp["scriptpubkey"] == "0014" + "ab" * 20

    def test_getitem_unknown_raises(self) -> None:
        inp = TxInput.from_hex("aa" * 32, 0)
        with pytest.raises(KeyError):
            inp["nonexistent"]

    def test_get_with_default(self) -> None:
        inp = TxInput.from_hex("aa" * 32, 0)
        assert inp.get("nonexistent", "fallback") == "fallback"

    def test_get_existing_key(self) -> None:
        inp = TxInput.from_hex("aa" * 32, 5)
        assert inp.get("vout") == 5

    def test_scriptsig_hex_property(self) -> None:
        inp = TxInput.from_hex("aa" * 32, 0, scriptsig="cafe")
        assert inp.scriptsig_hex == "cafe"


class TestTxOutputDictAccess:
    """Tests for TxOutput.__getitem__, .get(), and .scriptpubkey."""

    def test_getitem_value(self) -> None:
        out = TxOutput(value=50_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        assert out["value"] == 50_000

    def test_getitem_scriptpubkey(self) -> None:
        script = bytes([0x00, 0x14]) + b"\xab" * 20
        out = TxOutput(value=50_000, script=script)
        assert out["scriptpubkey"] == script.hex()

    def test_getitem_unknown_raises(self) -> None:
        out = TxOutput(value=1, script=b"\x00")
        with pytest.raises(KeyError):
            out["nonexistent"]

    def test_get_with_default(self) -> None:
        out = TxOutput(value=1, script=b"\x00")
        assert out.get("nonexistent", 42) == 42

    def test_get_existing_key(self) -> None:
        out = TxOutput(value=99, script=b"\x00")
        assert out.get("value") == 99

    def test_scriptpubkey_property(self) -> None:
        script = bytes([0x00, 0x14]) + b"\xcc" * 20
        out = TxOutput(value=1, script=script)
        assert out.scriptpubkey == script.hex()

    def test_address_method(self) -> None:
        """TxOutput.address() should resolve to bech32 address."""
        script = bytes([0x00, 0x14]) + b"\xdd" * 20
        out = TxOutput(value=1, script=script)
        addr = out.address("regtest")
        assert addr.startswith("bcrt1q")


# =============================================================================
# Serialization tests
# =============================================================================


class TestSerializeInputWithScriptsig:
    """Test serialize_input with a non-empty scriptsig."""

    def test_serialize_with_scriptsig(self) -> None:
        inp = TxInput.from_hex("aa" * 32, 0, scriptsig="deadbeef")
        serialized = serialize_input(inp, include_scriptsig=True)
        # Should contain the scriptsig bytes
        assert bytes.fromhex("deadbeef") in serialized

    def test_serialize_without_scriptsig(self) -> None:
        inp = TxInput.from_hex("aa" * 32, 0, scriptsig="deadbeef")
        serialized = serialize_input(inp, include_scriptsig=False)
        # Should have 0x00 for empty scriptsig
        # 32 bytes txid_le + 4 bytes vout + 1 byte (0x00) + 4 bytes sequence = 41 bytes
        assert len(serialized) == 41


class TestGetTxid:
    """Tests for get_txid."""

    def test_txid_from_serialized_tx(self) -> None:
        """Build a tx, serialize, compute txid, verify it's deterministic."""
        inp = TxInput.from_hex("aa" * 32, 0)
        out = TxOutput(value=50_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)

        tx_bytes = serialize_transaction(
            version=2,
            inputs=[inp],
            outputs=[out],
            locktime=0,
        )
        tx_hex = tx_bytes.hex()
        txid = get_txid(tx_hex)

        # Should be a 64-char hex string
        assert len(txid) == 64
        assert all(c in "0123456789abcdef" for c in txid)

        # Computing again should be deterministic
        assert get_txid(tx_hex) == txid

    def test_txid_differs_for_different_txs(self) -> None:
        """Different transactions should have different txids."""
        inp1 = TxInput.from_hex("aa" * 32, 0)
        inp2 = TxInput.from_hex("bb" * 32, 0)
        out = TxOutput(value=50_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)

        tx1 = serialize_transaction(2, [inp1], [out], 0).hex()
        tx2 = serialize_transaction(2, [inp2], [out], 0).hex()

        assert get_txid(tx1) != get_txid(tx2)


# =============================================================================
# Script code tests
# =============================================================================


class TestCreateP2wpkhScriptCode:
    """Tests for create_p2wpkh_script_code."""

    def test_from_bytes(self) -> None:
        pubkey = b"\x02" + b"\xaa" * 32
        sc = create_p2wpkh_script_code(pubkey)
        assert sc[:3] == b"\x76\xa9\x14"
        assert sc[-2:] == b"\x88\xac"
        assert len(sc) == 25

    def test_from_hex(self) -> None:
        """Hex string input should produce same result as bytes."""
        pubkey_bytes = b"\x02" + b"\xbb" * 32
        pubkey_hex = pubkey_bytes.hex()
        assert create_p2wpkh_script_code(pubkey_hex) == create_p2wpkh_script_code(pubkey_bytes)


# =============================================================================
# get_address_type tests
# =============================================================================


class TestGetAddressType:
    """Tests for get_address_type."""

    def test_p2wpkh(self) -> None:
        pubkey = b"\x02" + b"\xaa" * 32
        addr = pubkey_to_p2wpkh_address(pubkey, "regtest")
        assert get_address_type(addr) == "p2wpkh"

    def test_p2wsh(self) -> None:
        """P2WSH address should be detected."""
        import bech32 as bech32_lib

        script_hash = b"\xab" * 32
        addr = bech32_lib.encode("bcrt", 0, script_hash)
        assert addr is not None
        assert get_address_type(addr) == "p2wsh"

    def test_p2tr(self) -> None:
        """P2TR address should be detected."""
        import bech32 as bech32_lib

        x_only = b"\xcd" * 32
        addr = bech32_lib.encode("bcrt", 1, x_only)
        assert addr is not None
        assert get_address_type(addr) == "p2tr"

    def test_p2pkh(self) -> None:
        import base58 as b58

        payload = bytes([0x00]) + b"\xee" * 20
        addr = b58.b58encode_check(payload).decode("ascii")
        assert get_address_type(addr) == "p2pkh"

    def test_p2sh(self) -> None:
        import base58 as b58

        payload = bytes([0x05]) + b"\xff" * 20
        addr = b58.b58encode_check(payload).decode("ascii")
        assert get_address_type(addr) == "p2sh"

    def test_invalid_address_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown address type"):
            get_address_type("not_a_valid_address")


# =============================================================================
# Hash function tests
# =============================================================================


class TestHashFunctions:
    """Tests for hash160, hash256."""

    def test_hash160_length(self) -> None:
        result = hash160(b"test")
        assert len(result) == 20

    def test_hash256_length(self) -> None:
        result = hash256(b"test")
        assert len(result) == 32

    def test_hash256_deterministic(self) -> None:
        assert hash256(b"hello") == hash256(b"hello")

    def test_hash256_different_inputs(self) -> None:
        assert hash256(b"hello") != hash256(b"world")


class TestParseDerivationPathEmpty:
    """Test edge case: empty component in derivation path."""

    def test_trailing_slash(self) -> None:
        """m/84'/0'/ has trailing slash -> empty component should be skipped."""
        result = parse_derivation_path("m/84'/0'/")
        assert result == [84 | 0x80000000, 0 | 0x80000000]


# =============================================================================
# Varint encode/decode all branches
# =============================================================================


class TestVarintAllBranches:
    """Tests for encode_varint/decode_varint covering all size branches."""

    def test_encode_decode_small(self) -> None:
        """Values < 0xFD use 1-byte encoding."""
        for val in [0, 1, 127, 252]:
            encoded = encode_varint(val)
            assert len(encoded) == 1
            decoded, offset = decode_varint(encoded)
            assert decoded == val
            assert offset == 1

    def test_encode_decode_2byte(self) -> None:
        """Values 0xFD..0xFFFF use 3-byte encoding (0xFD prefix)."""
        for val in [0xFD, 0xFE, 0x1234, 0xFFFF]:
            encoded = encode_varint(val)
            assert len(encoded) == 3
            assert encoded[0] == 0xFD
            decoded, offset = decode_varint(encoded)
            assert decoded == val
            assert offset == 3

    def test_encode_decode_4byte(self) -> None:
        """Values 0x10000..0xFFFFFFFF use 5-byte encoding (0xFE prefix)."""
        for val in [0x10000, 0x12345678, 0xFFFFFFFF]:
            encoded = encode_varint(val)
            assert len(encoded) == 5
            assert encoded[0] == 0xFE
            decoded, offset = decode_varint(encoded)
            assert decoded == val
            assert offset == 5

    def test_encode_decode_8byte(self) -> None:
        """Values > 0xFFFFFFFF use 9-byte encoding (0xFF prefix)."""
        for val in [0x100000000, 0x123456789ABCDEF0]:
            encoded = encode_varint(val)
            assert len(encoded) == 9
            assert encoded[0] == 0xFF
            decoded, offset = decode_varint(encoded)
            assert decoded == val
            assert offset == 9


# =============================================================================
# More address / script coverage
# =============================================================================


class TestPubkeyToP2wpkhScript:
    """Tests for pubkey_to_p2wpkh_script."""

    def test_bytes_input(self) -> None:
        pubkey = b"\x02" + b"\xaa" * 32
        script = pubkey_to_p2wpkh_script(pubkey)
        assert script[0] == 0x00 and script[1] == 0x14
        assert len(script) == 22

    def test_hex_input(self) -> None:
        pubkey_hex = "02" + "bb" * 32
        script = pubkey_to_p2wpkh_script(pubkey_hex)
        assert script[0] == 0x00 and script[1] == 0x14
        assert len(script) == 22


class TestScriptToP2wshAddress:
    """Tests for script_to_p2wsh_address."""

    def test_basic(self) -> None:
        script = b"\x01\x02\x03"
        addr = script_to_p2wsh_address(script, "regtest")
        assert addr.startswith("bcrt1q")

    def test_mainnet(self) -> None:
        script = b"\x04\x05\x06"
        addr = script_to_p2wsh_address(script, "mainnet")
        assert addr.startswith("bc1q")


class TestSerializeOutpoint:
    """Tests for serialize_outpoint."""

    def test_basic(self) -> None:
        result = serialize_outpoint("aa" * 32, 0)
        assert len(result) == 36
        # Last 4 bytes are vout
        assert struct.unpack("<I", result[32:])[0] == 0

    def test_nonzero_vout(self) -> None:
        result = serialize_outpoint("bb" * 32, 7)
        assert struct.unpack("<I", result[32:])[0] == 7


class TestParsedTransactionAccessors:
    """Tests for ParsedTransaction bytes accessors."""

    def test_version_bytes(self) -> None:
        """Construct a tx, parse it, check version_bytes."""
        inp = TxInput.from_hex("aa" * 32, 0)
        out = TxOutput(value=50_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        tx = serialize_transaction(2, [inp], [out], 0)
        parsed = parse_transaction(tx.hex())
        assert parsed.version_bytes == struct.pack("<I", 2)

    def test_locktime_bytes(self) -> None:
        inp = TxInput.from_hex("aa" * 32, 0)
        out = TxOutput(value=50_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        tx = serialize_transaction(2, [inp], [out], 500_000)
        parsed = parse_transaction(tx.hex())
        assert parsed.locktime_bytes == struct.pack("<I", 500_000)

    def test_sequence_bytes(self) -> None:
        inp = TxInput.from_hex("aa" * 32, 0, sequence=0xFFFFFFFE)
        out = TxOutput(value=50_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        tx = serialize_transaction(2, [inp], [out], 0)
        parsed = parse_transaction(tx.hex())
        assert parsed.inputs[0].sequence_bytes == struct.pack("<I", 0xFFFFFFFE)


class TestTxOutputFromAddress:
    """Tests for TxOutput.from_address factory."""

    def test_from_bech32_address(self) -> None:
        pubkey = b"\x02" + b"\xcc" * 32
        addr = pubkey_to_p2wpkh_address(pubkey, "regtest")
        out = TxOutput.from_address(addr, 100_000)
        assert out.value == 100_000
        assert out.script[0] == 0x00 and out.script[1] == 0x14


class TestSerializeTransactionWithWitness:
    """Tests for serialize_transaction with witness data."""

    def test_with_witness(self) -> None:
        """Serialize a witness tx and parse it back."""
        inp = TxInput.from_hex("aa" * 32, 0)
        out = TxOutput(value=50_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)
        witness = [[b"\x30" * 71, b"\x02" + b"\xaa" * 32]]

        tx = serialize_transaction(2, [inp], [out], 0, witnesses=witness)
        parsed = parse_transaction(tx.hex())
        assert parsed.has_witness
        assert len(parsed.witnesses) == 1
        assert len(parsed.witnesses[0]) == 2

    def test_without_witness(self) -> None:
        """Non-witness tx should not have witness marker."""
        inp = TxInput.from_hex("aa" * 32, 0)
        out = TxOutput(value=50_000, script=bytes([0x00, 0x14]) + b"\x00" * 20)

        tx = serialize_transaction(2, [inp], [out], 0, witnesses=None)
        parsed = parse_transaction(tx.hex())
        assert not parsed.has_witness
