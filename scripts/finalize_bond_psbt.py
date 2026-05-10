#!/usr/bin/env python3
"""Finalize a signed fidelity bond spending PSBT.

This script is for hardware-wallet flows where the device returns a signed
PSBT containing a partial signature, but Bitcoin Core's ``finalizepsbt`` does
not finalize the custom CLTV P2WSH witness script.

It builds the final witness stack for the single-input, single-output bond
sweep:

    [signature, witness_script]

and outputs the final raw transaction hex ready for inspection and broadcast.

Usage:
  python scripts/finalize_bond_psbt.py <signed_psbt_base64>
  python scripts/finalize_bond_psbt.py --file signed-bond.psbt

Broadcast:
  bitcoin-cli testmempoolaccept '["<signed_tx_hex>"]'
  bitcoin-cli sendrawtransaction "<signed_tx_hex>"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import struct
import sys
from pathlib import Path


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a Bitcoin compact size integer."""
    if pos >= len(data):
        raise ValueError("Unexpected end of data while reading compact size")

    first = data[pos]
    if first < 0xFD:
        return first, pos + 1
    if first == 0xFD:
        if pos + 3 > len(data):
            raise ValueError("Truncated compact size uint16")
        return struct.unpack("<H", data[pos + 1 : pos + 3])[0], pos + 3
    if first == 0xFE:
        if pos + 5 > len(data):
            raise ValueError("Truncated compact size uint32")
        return struct.unpack("<I", data[pos + 1 : pos + 5])[0], pos + 5
    if pos + 9 > len(data):
        raise ValueError("Truncated compact size uint64")
    return struct.unpack("<Q", data[pos + 1 : pos + 9])[0], pos + 9


def _encode_varint(n: int) -> bytes:
    """Encode a Bitcoin compact size integer."""
    if n < 0xFD:
        return bytes([n])
    if n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    if n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    return b"\xff" + struct.pack("<Q", n)


def _read_psbt_pair(data: bytes, pos: int) -> tuple[bytes | None, bytes | None, int]:
    """Read one PSBT key-value pair. Empty key means map separator."""
    key_len, pos = _read_varint(data, pos)
    if key_len == 0:
        return None, None, pos
    if pos + key_len > len(data):
        raise ValueError("Truncated PSBT key")

    key = data[pos : pos + key_len]
    pos += key_len

    value_len, pos = _read_varint(data, pos)
    if pos + value_len > len(data):
        raise ValueError("Truncated PSBT value")
    value = data[pos : pos + value_len]
    pos += value_len

    return key, value, pos


def _parse_unsigned_tx(unsigned_tx: bytes) -> None:
    """Validate the unsigned transaction shape supported by this finalizer."""
    if len(unsigned_tx) < 10:
        raise ValueError("Unsigned transaction is too short")

    pos = 4
    if unsigned_tx[pos] == 0x00 and unsigned_tx[pos + 1] != 0x00:
        raise ValueError("PSBT unsigned transaction unexpectedly contains witness data")

    input_count, pos = _read_varint(unsigned_tx, pos)
    if input_count != 1:
        raise ValueError(f"Expected exactly 1 input, got {input_count}")

    if pos + 36 > len(unsigned_tx):
        raise ValueError("Unsigned transaction input outpoint is truncated")
    pos += 36

    script_len, pos = _read_varint(unsigned_tx, pos)
    if pos + script_len + 4 > len(unsigned_tx):
        raise ValueError("Unsigned transaction input script or sequence is truncated")
    pos += script_len + 4

    output_count, pos = _read_varint(unsigned_tx, pos)
    if output_count != 1:
        raise ValueError(f"Expected exactly 1 output, got {output_count}")

    if pos + 8 > len(unsigned_tx):
        raise ValueError("Unsigned transaction output value is truncated")
    pos += 8

    output_script_len, pos = _read_varint(unsigned_tx, pos)
    if pos + output_script_len > len(unsigned_tx):
        raise ValueError("Unsigned transaction output script is truncated")
    pos += output_script_len

    if pos != len(unsigned_tx) - 4:
        raise ValueError("Unsigned transaction has trailing data before locktime")


def _validate_p2wsh_script(witness_utxo_script: bytes, witness_script: bytes) -> None:
    """Verify witness_utxo is OP_0 SHA256(witness_script)."""
    expected = b"\x00\x20" + hashlib.sha256(witness_script).digest()
    if witness_utxo_script != expected:
        raise ValueError(
            "witness_script does not match the P2WSH witness_utxo scriptPubKey"
        )


def _extract_freeze_script_pubkey(witness_script: bytes) -> bytes:
    """Extract the pubkey from <locktime> OP_CLTV OP_DROP <pubkey> OP_CHECKSIG."""
    pos = 0
    if pos >= len(witness_script):
        raise ValueError("witness_script is empty")

    locktime_len = witness_script[pos]
    pos += 1
    if locktime_len < 1 or locktime_len > 5:
        raise ValueError("witness_script has invalid locktime push")
    if pos + locktime_len + 2 > len(witness_script):
        raise ValueError("witness_script locktime push is truncated")
    pos += locktime_len

    if pos >= len(witness_script):
        raise ValueError("witness_script missing OP_CHECKLOCKTIMEVERIFY")
    if witness_script[pos] != 0xB1:
        raise ValueError("witness_script missing OP_CHECKLOCKTIMEVERIFY")
    pos += 1

    if pos >= len(witness_script):
        raise ValueError("witness_script missing OP_DROP")
    if witness_script[pos] != 0x75:
        raise ValueError("witness_script missing OP_DROP")
    pos += 1

    if pos >= len(witness_script):
        raise ValueError("witness_script missing pubkey push")
    pubkey_len = witness_script[pos]
    pos += 1
    if pubkey_len != 33:
        raise ValueError("witness_script pubkey must be compressed")
    if pos + pubkey_len > len(witness_script):
        raise ValueError("witness_script pubkey is truncated")
    if pos + pubkey_len + 1 != len(witness_script):
        raise ValueError("witness_script has unexpected trailing data")

    pubkey = witness_script[pos : pos + pubkey_len]
    pos += pubkey_len
    if witness_script[pos] != 0xAC:
        raise ValueError("witness_script missing OP_CHECKSIG")

    return pubkey


def _extract_witness_utxo_script(witness_utxo: bytes) -> bytes:
    """Extract scriptPubKey from a serialized PSBT witness_utxo value."""
    if len(witness_utxo) < 9:
        raise ValueError("witness_utxo is too short")
    script_len, pos = _read_varint(witness_utxo, 8)
    script = witness_utxo[pos : pos + script_len]
    if len(script) != script_len:
        raise ValueError("witness_utxo scriptPubKey is truncated")
    return script


def parse_signed_bond_psbt(psbt_b64: str) -> dict[str, bytes]:
    """Extract the fields needed to finalize a signed single-input bond PSBT."""
    psbt_clean = "".join(psbt_b64.split())
    try:
        raw = base64.b64decode(psbt_clean, validate=True)
    except Exception as e:
        raise ValueError(f"Invalid base64 PSBT: {e}") from e

    if not raw.startswith(b"psbt\xff"):
        raise ValueError("Invalid PSBT: missing magic bytes")

    pos = 5
    unsigned_tx: bytes | None = None
    global_map_complete = False

    # Global map.
    while pos < len(raw):
        key, value, pos = _read_psbt_pair(raw, pos)
        if key is None:
            global_map_complete = True
            break
        assert value is not None
        if key[0] == 0x00:  # PSBT_GLOBAL_UNSIGNED_TX
            unsigned_tx = value

    if not global_map_complete:
        raise ValueError("PSBT global map is truncated")
    if unsigned_tx is None:
        raise ValueError("PSBT missing unsigned transaction")

    _parse_unsigned_tx(unsigned_tx)

    partial_sigs: list[bytes] = []
    partial_sig_pubkeys: list[bytes] = []
    witness_script: bytes | None = None
    witness_utxo_script: bytes | None = None
    input_map_complete = False

    # Input map for the single input.
    while pos < len(raw):
        key, value, pos = _read_psbt_pair(raw, pos)
        if key is None:
            input_map_complete = True
            break
        assert value is not None
        key_type = key[0]
        if key_type == 0x01:  # PSBT_IN_WITNESS_UTXO
            witness_utxo_script = _extract_witness_utxo_script(value)
        elif key_type == 0x02:  # PSBT_IN_PARTIAL_SIG
            pubkey = key[1:]
            if len(pubkey) != 33:
                raise ValueError("Partial signature key does not contain a compressed pubkey")
            partial_sig_pubkeys.append(pubkey)
            partial_sigs.append(value)
        elif key_type == 0x05:  # PSBT_IN_WITNESS_SCRIPT
            witness_script = value

    if not input_map_complete:
        raise ValueError("PSBT input map is truncated")
    if not partial_sigs:
        raise ValueError("PSBT missing partial signature")
    if len(partial_sigs) > 1:
        raise ValueError("Expected exactly 1 partial signature")
    if witness_script is None:
        raise ValueError("PSBT missing witness_script")
    if witness_utxo_script is None:
        raise ValueError("PSBT missing witness_utxo")

    _validate_p2wsh_script(witness_utxo_script, witness_script)
    script_pubkey = _extract_freeze_script_pubkey(witness_script)

    signature = partial_sigs[0]
    if partial_sig_pubkeys[0] != script_pubkey:
        raise ValueError("Partial signature pubkey does not match witness_script pubkey")
    if not signature or signature[-1] != 0x01:
        raise ValueError("Partial signature is missing SIGHASH_ALL byte")

    return {
        "unsigned_tx": unsigned_tx,
        "signature": signature,
        "witness_script": witness_script,
    }


def finalize_bond_psbt(psbt_b64: str) -> str:
    """Return final raw transaction hex from a signed bond PSBT."""
    data = parse_signed_bond_psbt(psbt_b64)
    unsigned_tx = data["unsigned_tx"]
    signature = data["signature"]
    witness_script = data["witness_script"]

    witness = (
        _encode_varint(2)
        + _encode_varint(len(signature))
        + signature
        + _encode_varint(len(witness_script))
        + witness_script
    )

    # PSBT unsigned txs are non-witness serializations. For the single-input
    # bond spend, insert marker/flag after nVersion and one witness stack before
    # nLockTime.
    signed_tx = unsigned_tx[:4] + b"\x00\x01" + unsigned_tx[4:-4] + witness + unsigned_tx[-4:]
    return signed_tx.hex()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Finalize a signed fidelity bond PSBT into raw transaction hex.",
    )
    parser.add_argument("psbt", nargs="?", help="Base64-encoded signed PSBT")
    parser.add_argument("--file", "-f", type=Path, help="Read signed PSBT from file")
    args = parser.parse_args()

    if args.file is not None:
        psbt_b64 = args.file.read_text().strip()
    elif args.psbt:
        psbt_b64 = args.psbt.strip()
    else:
        parser.error("Provide a signed PSBT argument or --file")

    try:
        print(finalize_bond_psbt(psbt_b64))
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
