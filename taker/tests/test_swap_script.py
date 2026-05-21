"""
Tests for the swap script module (HTLC construction and parsing).
"""

from __future__ import annotations

import hashlib
import secrets

import pytest

from taker.swap.script import SwapScript, _push_data, _push_int


class TestPushData:
    """Tests for Bitcoin script data push encoding."""

    def test_small_push(self) -> None:
        """Direct push for data <= 75 bytes."""
        data = bytes([0x42] * 20)
        result = _push_data(data)
        assert result[0] == 20  # length byte
        assert result[1:] == data

    def test_empty_push(self) -> None:
        """Zero-length push."""
        result = _push_data(b"")
        assert result == b"\x00"

    def test_pushdata1(self) -> None:
        """OP_PUSHDATA1 for 76-255 bytes."""
        data = bytes([0xAB] * 100)
        result = _push_data(data)
        assert result[0] == 0x4C  # OP_PUSHDATA1
        assert result[1] == 100
        assert result[2:] == data

    def test_pushdata2(self) -> None:
        """OP_PUSHDATA2 for 256-65535 bytes."""
        data = bytes([0xCD] * 300)
        result = _push_data(data)
        assert result[0] == 0x4D  # OP_PUSHDATA2
        length = int.from_bytes(result[1:3], "little")
        assert length == 300
        assert result[3:] == data

    def test_too_large(self) -> None:
        """Reject data > 65535 bytes."""
        with pytest.raises(ValueError, match="too large"):
            _push_data(bytes(70000))


class TestPushInt:
    """Tests for CScriptNum encoding."""

    def test_zero(self) -> None:
        assert _push_int(0) == bytes([0x00])

    def test_small_ints(self) -> None:
        """OP_1 through OP_16."""
        for i in range(1, 17):
            assert _push_int(i) == bytes([0x50 + i])

    def test_two_byte_int(self) -> None:
        """CScriptNum encoding for integers > 16."""
        result = _push_int(100)
        # 100 = 0x64, fits in one byte, no sign extension needed
        assert result == bytes([1, 0x64])

    def test_block_height(self) -> None:
        """Typical block height encoding."""
        height = 800_000  # 0x0C3500
        result = _push_int(height)
        # little-endian: 00 35 0C
        data = result[1:]  # skip length byte
        assert result[0] == 3  # 3-byte push
        assert int.from_bytes(data, "little") == 800_000

    def test_high_bit_needs_sign_byte(self) -> None:
        """If high bit is set, append 0x00 for positive."""
        n = 128  # 0x80 -- high bit set
        result = _push_int(n)
        # Should be: push_data([0x80, 0x00])
        assert result[0] == 2  # 2-byte push
        assert result[1] == 0x80
        assert result[2] == 0x00


class TestSwapScript:
    """Tests for SwapScript HTLC construction and parsing."""

    @pytest.fixture
    def preimage(self) -> bytes:
        return secrets.token_bytes(32)

    @pytest.fixture
    def preimage_hash(self, preimage: bytes) -> bytes:
        return hashlib.sha256(preimage).digest()

    @pytest.fixture
    def claim_privkey(self) -> bytes:
        return secrets.token_bytes(32)

    @pytest.fixture
    def claim_pubkey(self, claim_privkey: bytes) -> bytes:
        from coincurve import PrivateKey

        return PrivateKey(claim_privkey).public_key.format(compressed=True)

    @pytest.fixture
    def refund_privkey(self) -> bytes:
        return secrets.token_bytes(32)

    @pytest.fixture
    def refund_pubkey(self, refund_privkey: bytes) -> bytes:
        from coincurve import PrivateKey

        return PrivateKey(refund_privkey).public_key.format(compressed=True)

    @pytest.fixture
    def timeout_height(self) -> int:
        return 800_080

    @pytest.fixture
    def swap_script(
        self,
        preimage_hash: bytes,
        claim_pubkey: bytes,
        refund_pubkey: bytes,
        timeout_height: int,
    ) -> SwapScript:
        return SwapScript(
            preimage_hash=preimage_hash,
            claim_pubkey=claim_pubkey,
            refund_pubkey=refund_pubkey,
            timeout_blockheight=timeout_height,
        )

    def test_witness_script_not_empty(self, swap_script: SwapScript) -> None:
        """Witness script should be non-empty bytes."""
        ws = swap_script.witness_script()
        assert isinstance(ws, bytes)
        assert len(ws) > 0

    def test_witness_script_deterministic(self, swap_script: SwapScript) -> None:
        """Same parameters produce the same witness script."""
        ws1 = swap_script.witness_script()
        ws2 = swap_script.witness_script()
        assert ws1 == ws2

    def test_witness_script_starts_with_op_size(self, swap_script: SwapScript) -> None:
        """Script should start with OP_SIZE."""
        ws = swap_script.witness_script()
        assert ws[0] == 0x82  # OP_SIZE

    def test_witness_script_ends_with_op_checksig(self, swap_script: SwapScript) -> None:
        """Script should end with OP_CHECKSIG."""
        ws = swap_script.witness_script()
        assert ws[-1] == 0xAC  # OP_CHECKSIG

    def test_witness_script_contains_claim_pubkey(
        self, swap_script: SwapScript, claim_pubkey: bytes
    ) -> None:
        """Script should contain the claim pubkey."""
        ws = swap_script.witness_script()
        assert claim_pubkey in ws

    def test_witness_script_contains_refund_pubkey(
        self, swap_script: SwapScript, refund_pubkey: bytes
    ) -> None:
        """Script should contain the refund pubkey."""
        ws = swap_script.witness_script()
        assert refund_pubkey in ws

    def test_witness_script_contains_preimage_ripemd160(
        self, swap_script: SwapScript, preimage_hash: bytes
    ) -> None:
        """Script should contain RIPEMD160(preimage_hash)."""
        ws = swap_script.witness_script()
        ripemd = hashlib.new("ripemd160", preimage_hash).digest()
        assert ripemd in ws

    def test_p2wsh_address_regtest(self, swap_script: SwapScript) -> None:
        """P2WSH address should be bech32 with bcrt prefix for regtest."""
        addr = swap_script.p2wsh_address("regtest")
        assert addr.startswith("bcrt1q")
        assert len(addr) > 40

    def test_p2wsh_address_mainnet(self, swap_script: SwapScript) -> None:
        """P2WSH address should have bc1 prefix for mainnet."""
        addr = swap_script.p2wsh_address("mainnet")
        assert addr.startswith("bc1q")

    def test_p2wsh_scriptpubkey(self, swap_script: SwapScript) -> None:
        """ScriptPubKey should be 34 bytes: OP_0 <32-byte-hash>."""
        spk = swap_script.p2wsh_scriptpubkey()
        assert len(spk) == 34
        assert spk[0] == 0x00  # OP_0 (witness version)
        assert spk[1] == 0x20  # 32-byte push

    def test_p2wsh_scriptpubkey_matches_witness_script_hash(self, swap_script: SwapScript) -> None:
        """ScriptPubKey hash should be SHA256 of the witness script."""
        ws = swap_script.witness_script()
        spk = swap_script.p2wsh_scriptpubkey()
        expected_hash = hashlib.sha256(ws).digest()
        assert spk[2:] == expected_hash

    def test_build_claim_witness(self) -> None:
        """Claim witness should be [signature, preimage, witness_script]."""
        sig = b"\x30\x45" + bytes(69)  # fake DER sig
        preimage = secrets.token_bytes(32)
        ws = b"\x82" + bytes(100)  # fake witness script
        result = SwapScript.build_claim_witness(sig, preimage, ws)
        assert result == [sig, preimage, ws]

    def test_build_refund_witness(self) -> None:
        """Refund witness should be [signature, 0x00, witness_script]."""
        sig = b"\x30\x44" + bytes(68)  # fake DER sig
        ws = b"\x82" + bytes(100)  # fake witness script
        result = SwapScript.build_refund_witness(sig, ws)
        assert result == [sig, b"\x00", ws]


class TestSwapScriptParsing:
    """Tests for parsing witness scripts back into SwapScript objects."""

    def _make_script(
        self,
    ) -> tuple[SwapScript, bytes, bytes, bytes, int]:
        """Create a script with known parameters for round-trip testing."""
        preimage = secrets.token_bytes(32)
        preimage_hash = hashlib.sha256(preimage).digest()

        from coincurve import PrivateKey

        claim_key = PrivateKey(secrets.token_bytes(32))
        claim_pubkey = claim_key.public_key.format(compressed=True)
        refund_key = PrivateKey(secrets.token_bytes(32))
        refund_pubkey = refund_key.public_key.format(compressed=True)
        timeout = 850_000

        script = SwapScript(preimage_hash, claim_pubkey, refund_pubkey, timeout)
        return script, preimage_hash, claim_pubkey, refund_pubkey, timeout

    def test_round_trip_parse(self) -> None:
        """Parse a generated witness script and verify fields match."""
        original, preimage_hash, claim_pub, refund_pub, timeout = self._make_script()
        ws_hex = original.witness_script().hex()

        parsed = SwapScript.from_redeem_script(ws_hex)
        assert parsed.claim_pubkey == claim_pub
        assert parsed.refund_pubkey == refund_pub
        assert parsed.timeout_blockheight == timeout

    def test_verify_preimage_hash_correct(self) -> None:
        """Verify correct preimage hash passes."""
        original, preimage_hash, _, _, _ = self._make_script()
        ws_hex = original.witness_script().hex()

        parsed = SwapScript.from_redeem_script(ws_hex)
        assert parsed.verify_preimage_hash(preimage_hash) is True

    def test_verify_preimage_hash_wrong(self) -> None:
        """Verify wrong preimage hash fails."""
        original, _, _, _, _ = self._make_script()
        ws_hex = original.witness_script().hex()

        parsed = SwapScript.from_redeem_script(ws_hex)
        wrong_hash = secrets.token_bytes(32)
        assert parsed.verify_preimage_hash(wrong_hash) is False

    def test_verify_against_provider_valid(self) -> None:
        """Full provider verification with valid parameters."""
        original, preimage_hash, claim_pub, _, timeout = self._make_script()
        ws_hex = original.witness_script().hex()

        parsed = SwapScript.from_redeem_script(ws_hex)
        current_height = timeout - 80  # Within acceptable range
        # Should not raise
        parsed.verify_against_provider(
            expected_preimage_hash=preimage_hash,
            expected_claim_pubkey=claim_pub,
            timeout_blockheight=timeout,
            current_block_height=current_height,
        )

    def test_verify_against_provider_wrong_claim_key(self) -> None:
        """Provider verification fails with wrong claim pubkey."""
        original, preimage_hash, _, _, timeout = self._make_script()
        ws_hex = original.witness_script().hex()

        from coincurve import PrivateKey

        wrong_pubkey = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=True)

        parsed = SwapScript.from_redeem_script(ws_hex)
        current_height = timeout - 80
        with pytest.raises(ValueError, match="claim pubkey"):
            parsed.verify_against_provider(
                expected_preimage_hash=preimage_hash,
                expected_claim_pubkey=wrong_pubkey,
                timeout_blockheight=timeout,
                current_block_height=current_height,
            )

    def test_verify_against_provider_timeout_too_soon(self) -> None:
        """Provider verification fails when timeout is too close."""
        original, preimage_hash, claim_pub, _, timeout = self._make_script()
        ws_hex = original.witness_script().hex()

        parsed = SwapScript.from_redeem_script(ws_hex)
        current_height = timeout - 10  # Only 10 blocks away (< MIN_LOCKTIME_DELTA=60)
        with pytest.raises(ValueError, match="too soon"):
            parsed.verify_against_provider(
                expected_preimage_hash=preimage_hash,
                expected_claim_pubkey=claim_pub,
                timeout_blockheight=timeout,
                current_block_height=current_height,
            )

    def test_verify_against_provider_timeout_too_far(self) -> None:
        """Provider verification fails when timeout is too far."""
        original, preimage_hash, claim_pub, _, timeout = self._make_script()
        ws_hex = original.witness_script().hex()

        parsed = SwapScript.from_redeem_script(ws_hex)
        current_height = timeout - 200  # 200 blocks away (> MAX_LOCKTIME_DELTA=100)
        with pytest.raises(ValueError, match="too far"):
            parsed.verify_against_provider(
                expected_preimage_hash=preimage_hash,
                expected_claim_pubkey=claim_pub,
                timeout_blockheight=timeout,
                current_block_height=current_height,
            )

    def test_parse_invalid_script_too_short(self) -> None:
        """Parsing an incomplete script should fail."""
        with pytest.raises(ValueError):
            SwapScript.from_redeem_script("82")  # Just OP_SIZE

    def test_parse_invalid_script_bad_opcode(self) -> None:
        """Parsing a script with wrong opcodes should fail."""
        with pytest.raises(ValueError):
            SwapScript.from_redeem_script("FF" * 50)  # Garbage


class TestSwapScriptValidation:
    """Tests for SwapScript constructor validation."""

    def test_reject_wrong_preimage_hash_length(self) -> None:
        from coincurve import PrivateKey

        claim_pub = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=True)
        refund_pub = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=True)
        with pytest.raises(ValueError, match="preimage_hash must be 32 bytes"):
            SwapScript(b"\x00" * 16, claim_pub, refund_pub, 100)

    def test_reject_uncompressed_pubkey(self) -> None:
        from coincurve import PrivateKey

        claim_pub = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=False)
        refund_pub = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=True)
        with pytest.raises(ValueError, match="claim_pubkey must be 33 bytes"):
            SwapScript(b"\x00" * 32, claim_pub, refund_pub, 100)

    def test_reject_negative_timeout(self) -> None:
        from coincurve import PrivateKey

        claim_pub = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=True)
        refund_pub = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=True)
        with pytest.raises(ValueError, match="timeout_blockheight must be positive"):
            SwapScript(b"\x00" * 32, claim_pub, refund_pub, -1)


class TestSwapScriptCrossCompatibility:
    """Test that our SwapScript produces the same output as the mock provider's HTLC module."""

    # Inline reference implementation of build_witness_script from mock_swap_provider/htlc.py
    # so this unit test has no dependency on the mock_swap_provider package.
    # Opcodes: SIZE=0x82 EQUAL=0x87 IF=0x63 HASH160=0xA9 EQUALVERIFY=0x88
    #          ELSE=0x67 DROP=0x75 CLTV=0xB1 ENDIF=0x68 CHECKSIG=0xAC
    @staticmethod
    def _ref_build_witness_script(
        preimage_hash: bytes,
        claim_pubkey: bytes,
        refund_pubkey: bytes,
        timeout_blockheight: int,
    ) -> bytes:
        import hashlib
        import struct

        def _push_data(data: bytes) -> bytes:
            length = len(data)
            if length <= 75:
                return bytes([length]) + data
            elif length <= 255:
                return bytes([0x4C, length]) + data
            elif length <= 65535:
                return bytes([0x4D]) + struct.pack("<H", length) + data
            else:
                raise ValueError(f"Data too large for script push: {length} bytes")

        def _push_int(n: int) -> bytes:
            if n == 0:
                return bytes([0x00])
            if 1 <= n <= 16:
                return bytes([0x50 + n])
            result = []
            val = n
            while val > 0:
                result.append(val & 0xFF)
                val >>= 8
            if result[-1] & 0x80:
                result.append(0x00)
            return _push_data(bytes(result))

        ripemd160_hash = hashlib.new("ripemd160", preimage_hash).digest()

        script = b""
        script += bytes([0x82])  # OP_SIZE
        script += _push_data(bytes([32]))
        script += bytes([0x87])  # OP_EQUAL
        script += bytes([0x63])  # OP_IF
        script += bytes([0xA9])  # OP_HASH160
        script += _push_data(ripemd160_hash)
        script += bytes([0x88])  # OP_EQUALVERIFY
        script += _push_data(claim_pubkey)
        script += bytes([0x67])  # OP_ELSE
        script += bytes([0x75])  # OP_DROP
        script += _push_int(timeout_blockheight)
        script += bytes([0xB1])  # OP_CHECKLOCKTIMEVERIFY
        script += bytes([0x75])  # OP_DROP
        script += _push_data(refund_pubkey)
        script += bytes([0x68])  # OP_ENDIF
        script += bytes([0xAC])  # OP_CHECKSIG

        return script

    def test_same_witness_script_as_mock_provider(self) -> None:
        """The taker's SwapScript and mock provider's build_witness_script
        must produce identical witness scripts for the same parameters."""
        from coincurve import PrivateKey

        preimage_hash = secrets.token_bytes(32)
        claim_pub = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=True)
        refund_pub = PrivateKey(secrets.token_bytes(32)).public_key.format(compressed=True)
        timeout = 900_000

        taker_script = SwapScript(preimage_hash, claim_pub, refund_pub, timeout)
        taker_ws = taker_script.witness_script()

        provider_ws = self._ref_build_witness_script(preimage_hash, claim_pub, refund_pub, timeout)

        assert taker_ws == provider_ws, (
            "Taker and provider witness scripts differ! This would cause lockup address mismatches."
        )
