"""
HTLC witness script construction and claim witness generation.

Implements the Electrum-compatible submarine swap script used for both
forward and reverse swaps. The script uses P2WSH with a preimage-hash
conditional (HTLC pattern).

Script structure:
    OP_SIZE <32> OP_EQUAL
    OP_IF
        OP_HASH160 <ripemd160(sha256(preimage))> OP_EQUALVERIFY
        <claim_pubkey>
    OP_ELSE
        OP_DROP
        <timeout_blockheight> OP_CHECKLOCKTIMEVERIFY OP_DROP
        <refund_pubkey>
    OP_ENDIF
    OP_CHECKSIG

Claim path (with preimage): witness = <signature> <preimage> <witness_script>
Refund path (after timeout): witness = <signature> <0x00> <witness_script>
"""

from __future__ import annotations

import hashlib
import struct

from jmcore.bitcoin import (
    script_to_p2wsh_address,
    script_to_p2wsh_scriptpubkey,
)

# Bitcoin script opcodes
OP_SIZE = 0x82
OP_EQUAL = 0x87
OP_IF = 0x63
OP_HASH160 = 0xA9
OP_EQUALVERIFY = 0x88
OP_ELSE = 0x67
OP_DROP = 0x75
OP_CHECKLOCKTIMEVERIFY = 0xB1
OP_ENDIF = 0x68
OP_CHECKSIG = 0xAC


def _push_data(data: bytes) -> bytes:
    """Encode a data push for Bitcoin script.

    Handles the various push opcodes based on data length:
    - 0-75 bytes: direct push (length byte + data)
    - 76-255 bytes: OP_PUSHDATA1 + 1-byte length + data
    - 256-65535 bytes: OP_PUSHDATA2 + 2-byte length + data

    Args:
        data: Raw bytes to push onto the stack.

    Returns:
        Script bytes for the push operation.
    """
    length = len(data)
    if length <= 75:
        return bytes([length]) + data
    elif length <= 255:
        return bytes([0x4C, length]) + data  # OP_PUSHDATA1
    elif length <= 65535:
        return bytes([0x4D]) + struct.pack("<H", length) + data  # OP_PUSHDATA2
    else:
        raise ValueError(f"Data too large for script push: {length} bytes")


def _push_int(n: int) -> bytes:
    """Encode a small integer or CScriptNum for Bitcoin script.

    For block heights (typically 6 digits), this produces a minimal
    CScriptNum encoding.

    Args:
        n: Non-negative integer to encode.

    Returns:
        Script bytes for the integer push.
    """
    if n == 0:
        return bytes([0x00])  # OP_0
    if 1 <= n <= 16:
        return bytes([0x50 + n])  # OP_1 through OP_16

    # CScriptNum: little-endian with sign bit
    # For positive numbers, encode as little-endian and add 0x00 if high bit set
    result = []
    val = n
    while val > 0:
        result.append(val & 0xFF)
        val >>= 8
    # If the high bit of the last byte is set, append 0x00 (positive sign)
    if result[-1] & 0x80:
        result.append(0x00)

    data = bytes(result)
    return _push_data(data)


class SwapScript:
    """Constructs and verifies submarine swap HTLC witness scripts.

    The script enables two spending paths:
    1. Claim path: the client (taker) reveals the preimage and signs
    2. Refund path: the server refunds after the timeout block height

    For reverse swaps (LN -> on-chain):
    - claim_pubkey = client's key (taker claims with preimage)
    - refund_pubkey = server's key (server refunds after timeout)
    """

    # When created via from_redeem_script(), stores the RIPEMD160 hash extracted
    # from the script. Used by witness_script() to reconstruct the exact script
    # even when preimage_hash is a placeholder.
    _parsed_ripemd160: bytes | None = None

    def __init__(
        self,
        preimage_hash: bytes,
        claim_pubkey: bytes,
        refund_pubkey: bytes,
        timeout_blockheight: int,
    ) -> None:
        """Initialize swap script parameters.

        Args:
            preimage_hash: SHA256 hash of the 32-byte preimage.
            claim_pubkey: Compressed public key for the claim path (33 bytes).
            refund_pubkey: Compressed public key for the refund path (33 bytes).
            timeout_blockheight: Block height for CLTV timeout.

        Raises:
            ValueError: If parameters are invalid.
        """
        if len(preimage_hash) != 32:
            raise ValueError(f"preimage_hash must be 32 bytes, got {len(preimage_hash)}")
        if len(claim_pubkey) != 33:
            raise ValueError(f"claim_pubkey must be 33 bytes (compressed), got {len(claim_pubkey)}")
        if len(refund_pubkey) != 33:
            raise ValueError(
                f"refund_pubkey must be 33 bytes (compressed), got {len(refund_pubkey)}"
            )
        if timeout_blockheight <= 0:
            raise ValueError(f"timeout_blockheight must be positive, got {timeout_blockheight}")

        self.preimage_hash = preimage_hash
        self.claim_pubkey = claim_pubkey
        self.refund_pubkey = refund_pubkey
        self.timeout_blockheight = timeout_blockheight

    def witness_script(self) -> bytes:
        """Build the HTLC witness script.

        Returns:
            Raw witness script bytes.
        """
        # RIPEMD160 of the SHA256 hash (for OP_HASH160 comparison).
        # If this script was parsed (via from_redeem_script), use the stored
        # ripemd160 hash directly since preimage_hash is a placeholder.
        ripemd160_hash = (
            getattr(self, "_parsed_ripemd160", None)
            or hashlib.new("ripemd160", self.preimage_hash).digest()
        )

        script = b""
        # OP_SIZE <32> OP_EQUAL
        script += bytes([OP_SIZE])
        script += _push_data(bytes([32]))
        script += bytes([OP_EQUAL])
        # OP_IF
        script += bytes([OP_IF])
        #   OP_HASH160 <ripemd160(preimage_hash)> OP_EQUALVERIFY
        script += bytes([OP_HASH160])
        script += _push_data(ripemd160_hash)
        script += bytes([OP_EQUALVERIFY])
        #   <claim_pubkey>
        script += _push_data(self.claim_pubkey)
        # OP_ELSE
        script += bytes([OP_ELSE])
        #   OP_DROP
        script += bytes([OP_DROP])
        #   <timeout> OP_CHECKLOCKTIMEVERIFY OP_DROP
        script += _push_int(self.timeout_blockheight)
        script += bytes([OP_CHECKLOCKTIMEVERIFY])
        script += bytes([OP_DROP])
        #   <refund_pubkey>
        script += _push_data(self.refund_pubkey)
        # OP_ENDIF
        script += bytes([OP_ENDIF])
        # OP_CHECKSIG
        script += bytes([OP_CHECKSIG])

        return script

    def p2wsh_address(self, network: str = "mainnet") -> str:
        """Derive the P2WSH address for this swap script.

        Args:
            network: Bitcoin network name.

        Returns:
            Bech32 P2WSH address.
        """
        return script_to_p2wsh_address(self.witness_script(), network)

    def p2wsh_scriptpubkey(self) -> bytes:
        """Derive the P2WSH scriptPubKey.

        Returns:
            scriptPubKey bytes (OP_0 <32-byte-hash>).
        """
        return script_to_p2wsh_scriptpubkey(self.witness_script())

    @staticmethod
    def build_claim_witness(
        signature: bytes,
        preimage: bytes,
        witness_script: bytes,
    ) -> list[bytes]:
        """Build the witness stack for claiming a swap output.

        The witness stack for the claim path is:
            <signature> <preimage> <witness_script>

        Args:
            signature: DER-encoded signature with sighash byte.
            preimage: The 32-byte preimage.
            witness_script: The full witness script.

        Returns:
            List of witness stack items.
        """
        return [signature, preimage, witness_script]

    @staticmethod
    def build_refund_witness(
        signature: bytes,
        witness_script: bytes,
    ) -> list[bytes]:
        """Build the witness stack for refunding a swap output.

        The witness stack for the refund path is:
            <signature> <0x00> <witness_script>

        The 0x00 byte is a dummy value that fails the OP_SIZE check,
        causing execution to take the OP_ELSE (refund) branch.

        Args:
            signature: DER-encoded signature with sighash byte.
            witness_script: The full witness script.

        Returns:
            List of witness stack items.
        """
        return [signature, b"\x00", witness_script]

    @classmethod
    def from_redeem_script(cls, script_hex: str) -> SwapScript:
        """Parse a swap script from its hex representation.

        This is used to verify the redeem script returned by the swap provider
        matches our expected parameters.

        Args:
            script_hex: Hex-encoded witness script from the provider.

        Returns:
            SwapScript with extracted parameters.

        Raises:
            ValueError: If the script doesn't match the expected HTLC pattern.
        """
        script = bytes.fromhex(script_hex)
        return cls._parse_script(script)

    @classmethod
    def _parse_script(cls, script: bytes) -> SwapScript:
        """Parse a raw witness script into its components.

        Expected structure:
            OP_SIZE <1-byte: 0x20> OP_EQUAL
            OP_IF
                OP_HASH160 <20-byte hash> OP_EQUALVERIFY
                <33-byte claim_pubkey>
            OP_ELSE
                OP_DROP
                <n-byte timeout> OP_CHECKLOCKTIMEVERIFY OP_DROP
                <33-byte refund_pubkey>
            OP_ENDIF
            OP_CHECKSIG

        Args:
            script: Raw witness script bytes.

        Returns:
            SwapScript with parsed parameters.

        Raises:
            ValueError: If parsing fails.
        """
        pos = 0

        def read_byte() -> int:
            nonlocal pos
            if pos >= len(script):
                raise ValueError(f"Script too short at position {pos}")
            b = script[pos]
            pos += 1
            return b

        def read_push() -> bytes:
            """Read a data push and return the pushed data."""
            nonlocal pos
            length = read_byte()
            if length <= 75:
                data = script[pos : pos + length]
                pos += length
                return data
            elif length == 0x4C:  # OP_PUSHDATA1
                data_len = read_byte()
                data = script[pos : pos + data_len]
                pos += data_len
                return data
            elif length == 0x4D:  # OP_PUSHDATA2
                data_len = struct.unpack_from("<H", script, pos)[0]
                pos += 2
                data = script[pos : pos + data_len]
                pos += data_len
                return data
            else:
                raise ValueError(f"Unexpected opcode 0x{length:02x} at position {pos - 1}")

        def read_scriptnum() -> int:
            """Read a CScriptNum push and decode to integer."""
            data = read_push()
            if len(data) == 0:
                return 0
            # Decode little-endian with sign bit
            result = int.from_bytes(data, "little")
            # Check sign bit
            if data[-1] & 0x80:
                # Negative (shouldn't happen for block heights)
                result -= 1 << (8 * len(data))
            return result

        try:
            # OP_SIZE
            if read_byte() != OP_SIZE:
                raise ValueError("Expected OP_SIZE")
            # <32>
            size_data = read_push()
            if size_data != bytes([32]):
                raise ValueError(f"Expected push of 0x20, got {size_data.hex()}")
            # OP_EQUAL
            if read_byte() != OP_EQUAL:
                raise ValueError("Expected OP_EQUAL")
            # OP_IF
            if read_byte() != OP_IF:
                raise ValueError("Expected OP_IF")
            # OP_HASH160
            if read_byte() != OP_HASH160:
                raise ValueError("Expected OP_HASH160")
            # <20-byte ripemd160 hash>
            ripemd160_hash = read_push()
            if len(ripemd160_hash) != 20:
                raise ValueError(f"Expected 20-byte hash, got {len(ripemd160_hash)}")
            # OP_EQUALVERIFY
            if read_byte() != OP_EQUALVERIFY:
                raise ValueError("Expected OP_EQUALVERIFY")
            # <33-byte claim_pubkey>
            claim_pubkey = read_push()
            if len(claim_pubkey) != 33:
                raise ValueError(f"Expected 33-byte pubkey, got {len(claim_pubkey)}")
            # OP_ELSE
            if read_byte() != OP_ELSE:
                raise ValueError("Expected OP_ELSE")
            # OP_DROP
            if read_byte() != OP_DROP:
                raise ValueError("Expected OP_DROP")
            # <timeout blockheight>
            timeout = read_scriptnum()
            # OP_CHECKLOCKTIMEVERIFY
            if read_byte() != OP_CHECKLOCKTIMEVERIFY:
                raise ValueError("Expected OP_CHECKLOCKTIMEVERIFY")
            # OP_DROP
            if read_byte() != OP_DROP:
                raise ValueError("Expected OP_DROP")
            # <33-byte refund_pubkey>
            refund_pubkey = read_push()
            if len(refund_pubkey) != 33:
                raise ValueError(f"Expected 33-byte pubkey, got {len(refund_pubkey)}")
            # OP_ENDIF
            if read_byte() != OP_ENDIF:
                raise ValueError("Expected OP_ENDIF")
            # OP_CHECKSIG
            if read_byte() != OP_CHECKSIG:
                raise ValueError("Expected OP_CHECKSIG")

            if pos != len(script):
                raise ValueError(f"Unexpected trailing data at position {pos}")

        except (IndexError, struct.error) as e:
            raise ValueError(f"Failed to parse swap script: {e}") from e

        # We have the RIPEMD160 hash, not the SHA256 preimage hash directly.
        # We store a synthetic 32-byte value that will need to be verified
        # by the caller using the actual preimage hash.
        # The caller should verify: ripemd160(preimage_hash) == ripemd160_hash
        #
        # For reconstruction, we store the ripemd160 hash and let the caller
        # verify against their known preimage_hash.
        #
        # We create the SwapScript with a placeholder preimage_hash and
        # provide a method to verify it.
        return cls._from_parsed(
            ripemd160_hash=ripemd160_hash,
            claim_pubkey=claim_pubkey,
            refund_pubkey=refund_pubkey,
            timeout_blockheight=timeout,
        )

    @classmethod
    def _from_parsed(
        cls,
        ripemd160_hash: bytes,
        claim_pubkey: bytes,
        refund_pubkey: bytes,
        timeout_blockheight: int,
    ) -> SwapScript:
        """Create a SwapScript from parsed components.

        Since the script contains RIPEMD160(SHA256(preimage)) but we need
        SHA256(preimage) for the constructor, we store the ripemd160 hash
        as an attribute for verification purposes.

        Args:
            ripemd160_hash: The 20-byte RIPEMD160 hash from the script.
            claim_pubkey: Compressed public key for the claim path.
            refund_pubkey: Compressed public key for the refund path.
            timeout_blockheight: CLTV timeout block height.

        Returns:
            SwapScript instance. The preimage_hash field contains a
            placeholder; use verify_preimage_hash() to validate.
        """
        # Use a placeholder preimage_hash; the actual hash will be verified separately
        placeholder_hash = b"\x00" * 32
        instance = cls.__new__(cls)
        instance.preimage_hash = placeholder_hash
        instance.claim_pubkey = claim_pubkey
        instance.refund_pubkey = refund_pubkey
        instance.timeout_blockheight = timeout_blockheight
        instance._parsed_ripemd160 = ripemd160_hash
        return instance

    def verify_preimage_hash(self, preimage_hash: bytes) -> bool:
        """Verify that a preimage hash matches the script's RIPEMD160 hash.

        Args:
            preimage_hash: SHA256(preimage) to verify.

        Returns:
            True if RIPEMD160(preimage_hash) matches the script's hash.
        """
        expected_ripemd = getattr(self, "_parsed_ripemd160", None)
        if expected_ripemd is None:
            # Script was constructed, not parsed. Compute from our hash.
            expected_ripemd = hashlib.new("ripemd160", self.preimage_hash).digest()

        actual_ripemd = hashlib.new("ripemd160", preimage_hash).digest()
        return actual_ripemd == expected_ripemd

    def verify_against_provider(
        self,
        expected_preimage_hash: bytes,
        expected_claim_pubkey: bytes,
        timeout_blockheight: int,
        current_block_height: int,
    ) -> None:
        """Verify a provider's redeem script against expected parameters.

        Checks:
        1. Preimage hash matches (via RIPEMD160)
        2. Claim pubkey matches our key
        3. Timeout is within acceptable range
        4. Timeout gives us enough time to claim

        Args:
            expected_preimage_hash: Our SHA256(preimage).
            expected_claim_pubkey: Our claim public key.
            timeout_blockheight: The timeout from the provider response.
            current_block_height: Current blockchain height.

        Raises:
            ValueError: If any verification fails.
        """
        # Verify preimage hash
        if not self.verify_preimage_hash(expected_preimage_hash):
            raise ValueError("Redeem script preimage hash does not match our preimage")

        # Verify claim pubkey
        if self.claim_pubkey != expected_claim_pubkey:
            raise ValueError(
                f"Redeem script claim pubkey {self.claim_pubkey.hex()} "
                f"does not match our key {expected_claim_pubkey.hex()}"
            )

        # Verify timeout is reasonable
        from taker.swap.models import MAX_LOCKTIME_DELTA, MIN_LOCKTIME_DELTA

        delta = self.timeout_blockheight - current_block_height
        if delta < MIN_LOCKTIME_DELTA:
            raise ValueError(
                f"Timeout too soon: {delta} blocks (minimum {MIN_LOCKTIME_DELTA}). "
                f"Not enough time to claim the swap output."
            )
        if delta > MAX_LOCKTIME_DELTA:
            raise ValueError(
                f"Timeout too far: {delta} blocks (maximum {MAX_LOCKTIME_DELTA}). "
                f"Funds would be locked for too long."
            )

        # Verify timeout matches what provider told us
        if self.timeout_blockheight != timeout_blockheight:
            raise ValueError(
                f"Redeem script timeout {self.timeout_blockheight} "
                f"does not match provider's claimed timeout {timeout_blockheight}"
            )
