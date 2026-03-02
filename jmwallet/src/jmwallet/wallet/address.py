"""
Bitcoin address generation and validation utilities.

This module provides thin wrappers around the ``bech32`` library and
re-exports address utilities from ``jmcore.bitcoin`` for backward
compatibility.
"""

from __future__ import annotations

import bech32 as bech32_lib

# Re-export from jmcore.bitcoin for backward compatibility
from jmcore.bitcoin import (
    hash160,
    pubkey_to_p2wpkh_address,
    pubkey_to_p2wpkh_script,
    script_to_p2wsh_address,
    script_to_p2wsh_scriptpubkey,
)


def bech32_decode(hrp: str, addr: str) -> tuple[int | None, list[int] | None]:
    """Decode a bech32/bech32m address with full checksum verification.

    Returns ``(witness_version, witness_program)`` on success, or
    ``(None, None)`` if the address is malformed or the checksum is
    invalid.

    This is a thin wrapper around :func:`bech32.decode` to keep a
    single call-site convention across the codebase.
    """
    return bech32_lib.decode(hrp, addr)


def bech32_encode(hrp: str, witness_version: int, witness_program: bytes) -> str:
    """Encode a witness program as a bech32 address.

    Raises :class:`ValueError` if encoding fails (e.g. invalid
    witness version or program length).
    """
    result = bech32_lib.encode(hrp, witness_version, witness_program)
    if result is None:
        raise ValueError(
            f"Failed to encode bech32: version={witness_version}, program={witness_program.hex()}"
        )
    return result


def convertbits(data: bytes, frombits: int, tobits: int, pad: bool = True) -> list[int]:
    """Convert between bit groups.

    Thin wrapper around :func:`bech32.convertbits`.  Kept for backward
    compatibility.
    """
    result = bech32_lib.convertbits(data, frombits, tobits, pad)
    if result is None:
        raise ValueError("convertbits failed")
    return result


__all__ = [
    "bech32_decode",
    "bech32_encode",
    "convertbits",
    "hash160",
    "pubkey_to_p2wpkh_address",
    "pubkey_to_p2wpkh_script",
    "script_to_p2wsh_address",
    "script_to_p2wsh_scriptpubkey",
]
