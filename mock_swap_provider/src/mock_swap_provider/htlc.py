"""HTLC script construction for the mock swap provider.

Builds the same Electrum-compatible HTLC witness scripts as the taker's
SwapScript, but from the server (refund) perspective. This is a standalone
implementation so the mock provider doesn't depend on the taker package.

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
"""

from __future__ import annotations

import hashlib
import struct

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
    """Encode a data push for Bitcoin script."""
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
    """Encode a CScriptNum for Bitcoin script."""
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

    data = bytes(result)
    return _push_data(data)


def build_witness_script(
    preimage_hash: bytes,
    claim_pubkey: bytes,
    refund_pubkey: bytes,
    timeout_blockheight: int,
) -> bytes:
    """Build the HTLC witness script.

    Args:
        preimage_hash: SHA256 hash of the preimage (32 bytes).
        claim_pubkey: Client's compressed public key (33 bytes).
        refund_pubkey: Server's compressed public key (33 bytes).
        timeout_blockheight: CLTV timeout block height.

    Returns:
        Raw witness script bytes.
    """
    ripemd160_hash = hashlib.new("ripemd160", preimage_hash).digest()

    script = b""
    script += bytes([OP_SIZE])
    script += _push_data(bytes([32]))
    script += bytes([OP_EQUAL])
    script += bytes([OP_IF])
    script += bytes([OP_HASH160])
    script += _push_data(ripemd160_hash)
    script += bytes([OP_EQUALVERIFY])
    script += _push_data(claim_pubkey)
    script += bytes([OP_ELSE])
    script += bytes([OP_DROP])
    script += _push_int(timeout_blockheight)
    script += bytes([OP_CHECKLOCKTIMEVERIFY])
    script += bytes([OP_DROP])
    script += _push_data(refund_pubkey)
    script += bytes([OP_ENDIF])
    script += bytes([OP_CHECKSIG])

    return script


def script_to_p2wsh_scriptpubkey(witness_script: bytes) -> bytes:
    """Derive P2WSH scriptPubKey: OP_0 <SHA256(witness_script)>."""
    script_hash = hashlib.sha256(witness_script).digest()
    return b"\x00\x20" + script_hash


def script_to_p2wsh_address(witness_script: bytes, network: str = "regtest") -> str:
    """Derive bech32 P2WSH address from witness script."""
    import bech32 as bech32_lib

    script_hash = hashlib.sha256(witness_script).digest()
    hrp = "bcrt" if network == "regtest" else ("tb" if network in ("testnet", "signet") else "bc")
    result = bech32_lib.encode(hrp, 0, list(script_hash))
    if result is None:
        raise ValueError("Failed to encode bech32 P2WSH address")
    return result
