#!/usr/bin/env python3
"""Sign a fidelity bond spending PSBT using a BIP39 mnemonic seed phrase.

This script does not import from jmcore or jmwallet, so it works even when
pydantic or other project dependencies have version conflicts. Its external
dependencies are ``python-bitcointx`` with native ``libsecp256k1`` and
``mnemonic``.

This script is for users who have their bond wallet seed phrase (e.g., from
Sparrow) but do NOT have a hardware wallet. It derives the private key from
the mnemonic and derivation path, signs the bond spending transaction, and
outputs a fully signed raw transaction.

SECURITY NOTES:
  - The mnemonic is read interactively (never as a CLI argument)
  - The mnemonic is never written to disk or logs
  - After signing, the key material is discarded

USAGE:
  python scripts/sign_bond_mnemonic.py <psbt_base64>
  python scripts/sign_bond_mnemonic.py --file psbt.txt
  python scripts/sign_bond_mnemonic.py --file psbt.txt --derivation-path "m/..."

The PSBT may contain BIP32 derivation information, which supplies the default
derivation path. An explicit --derivation-path also works without BIP32 PSBT
metadata.

REQUIREMENTS:
  pip install "python-bitcointx @ https://github.com/m0wer/python-bitcointx/releases/download/python-bitcointx-v2.2.0/python_bitcointx-2.2.0-py3-none-any.whl#sha256=c14297225f11ded3bb6220d520e74a3c144ca28a25455e621aa21b5db31fe36b" mnemonic
  # Install native libsecp256k1 through your operating system package manager.

"""

from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import struct
import sys
from pathlib import Path

from bitcointx.core.key import CKey
from mnemonic import Mnemonic

# secp256k1 curve order -- used for BIP32 child key derivation
SECP256K1_N = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16
)

MAX_MONEY = 21_000_000 * 100_000_000
MAX_PSBT_SIZE = 4_000_000
MAX_PSBT_BASE64_SIZE = ((MAX_PSBT_SIZE + 2) // 3) * 4
MAX_PSBT_TEXT_SIZE = MAX_PSBT_BASE64_SIZE * 2
MAX_TX_OUTPUTS = 100_000
MAX_SCRIPT_SIZE = 10_000
MAX_PSBT_MAP_ENTRIES = 1_000
MAX_BIP32_PATH_DEPTH = 255
UINT32_MAX = 0xFFFFFFFF
LOCKTIME_THRESHOLD = 500_000_000


# ---------------------------------------------------------------------------
# Bitcoin primitives (inline to avoid pydantic dependency)
# ---------------------------------------------------------------------------


def _hash256(data: bytes) -> bytes:
    """Double SHA-256 (Bitcoin's standard hash)."""
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _require_bytes(data: bytes, pos: int, length: int, field: str) -> None:
    """Ensure a bounded read is entirely contained in data."""
    if pos < 0 or length < 0 or pos > len(data) or length > len(data) - pos:
        raise ValueError(f"Truncated {field}")


def _read_varint(data: bytes, pos: int, field: str = "compact size") -> tuple[int, int]:
    """Read a Bitcoin compact size varint. Returns (value, new_position)."""
    _require_bytes(data, pos, 1, field)
    first = data[pos]
    if first < 0xFD:
        return first, pos + 1
    if first == 0xFD:
        _require_bytes(data, pos + 1, 2, field)
        value = struct.unpack_from("<H", data, pos + 1)[0]
        if value < 0xFD:
            raise ValueError(f"Non-canonical {field}")
        return value, pos + 3
    if first == 0xFE:
        _require_bytes(data, pos + 1, 4, field)
        value = struct.unpack_from("<I", data, pos + 1)[0]
        if value <= 0xFFFF:
            raise ValueError(f"Non-canonical {field}")
        return value, pos + 5

    _require_bytes(data, pos + 1, 8, field)
    value = struct.unpack_from("<Q", data, pos + 1)[0]
    if value <= 0xFFFFFFFF:
        raise ValueError(f"Non-canonical {field}")
    return value, pos + 9


def _encode_varint(n: int) -> bytes:
    """Encode an integer as a Bitcoin compact size varint."""
    if n < 0xFD:
        return bytes([n])
    elif n <= 0xFFFF:
        return b"\xfd" + struct.pack("<H", n)
    elif n <= 0xFFFFFFFF:
        return b"\xfe" + struct.pack("<I", n)
    else:
        return b"\xff" + struct.pack("<Q", n)


def _path_to_string(indices: list[int]) -> str:
    """Convert BIP32 uint32 indices to human-readable path string."""
    parts = ["m"]
    for idx in indices:
        if idx >= 0x80000000:
            parts.append(f"{idx - 0x80000000}'")
        else:
            parts.append(str(idx))
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Minimal transaction parsing/serialization (no pydantic dataclasses)
# ---------------------------------------------------------------------------


class _TxInput:
    """Minimal transaction input representation."""

    __slots__ = ("txid_le", "vout", "scriptsig", "sequence")

    def __init__(
        self,
        txid_le: bytes,
        vout: int,
        scriptsig: bytes = b"",
        sequence: int = 0xFFFFFFFF,
    ) -> None:
        self.txid_le = txid_le
        self.vout = vout
        self.scriptsig = scriptsig
        self.sequence = sequence


class _TxOutput:
    """Minimal transaction output representation."""

    __slots__ = ("value", "script")

    def __init__(self, value: int, script: bytes) -> None:
        self.value = value
        self.script = script


class _ParsedTx:
    """Minimal parsed transaction."""

    __slots__ = ("version", "inputs", "outputs", "witnesses", "locktime")

    def __init__(
        self,
        version: int,
        inputs: list[_TxInput],
        outputs: list[_TxOutput],
        witnesses: list[list[bytes]],
        locktime: int,
    ) -> None:
        self.version = version
        self.inputs = inputs
        self.outputs = outputs
        self.witnesses = witnesses
        self.locktime = locktime


def _parse_tx(data: bytes) -> _ParsedTx:
    """Strictly parse a non-witness unsigned transaction."""
    if len(data) > MAX_PSBT_SIZE:
        raise ValueError("Unsigned transaction exceeds size limit")
    pos = 0
    _require_bytes(data, pos, 4, "transaction version")
    version = struct.unpack_from("<I", data, pos)[0]
    pos += 4

    _require_bytes(data, pos, 1, "transaction input count")
    if data[pos] == 0x00:
        _require_bytes(data, pos, 2, "transaction marker and flag")
        if data[pos + 1] != 0x00:
            raise ValueError("PSBT unsigned transaction must not contain witness data")

    in_count, pos = _read_varint(data, pos, "transaction input count")
    if in_count != 1:
        raise ValueError(
            f"Expected exactly one unsigned transaction input, got {in_count}"
        )
    inputs: list[_TxInput] = []
    for _ in range(in_count):
        _require_bytes(data, pos, 32, "transaction input txid")
        txid_le = data[pos : pos + 32]
        pos += 32
        _require_bytes(data, pos, 4, "transaction input index")
        vout = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        ss_len, pos = _read_varint(data, pos, "transaction input script length")
        if ss_len != 0:
            raise ValueError("PSBT unsigned transaction input scriptSig must be empty")
        _require_bytes(data, pos, ss_len, "transaction input scriptSig")
        scriptsig = data[pos : pos + ss_len]
        pos += ss_len
        _require_bytes(data, pos, 4, "transaction input sequence")
        sequence = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        inputs.append(_TxInput(txid_le, vout, scriptsig, sequence))

    out_count, pos = _read_varint(data, pos, "transaction output count")
    if out_count > MAX_TX_OUTPUTS:
        raise ValueError(f"Transaction output count exceeds limit: {out_count}")
    outputs: list[_TxOutput] = []
    for _ in range(out_count):
        _require_bytes(data, pos, 8, "transaction output value")
        value = struct.unpack_from("<Q", data, pos)[0]
        pos += 8
        if value > MAX_MONEY:
            raise ValueError(f"Transaction output value exceeds MAX_MONEY: {value}")
        sc_len, pos = _read_varint(data, pos, "transaction output script length")
        if sc_len > MAX_SCRIPT_SIZE:
            raise ValueError(f"Transaction output script exceeds size limit: {sc_len}")
        _require_bytes(data, pos, sc_len, "transaction output script")
        script = data[pos : pos + sc_len]
        pos += sc_len
        outputs.append(_TxOutput(value, script))

    _require_bytes(data, pos, 4, "transaction locktime")
    locktime = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    if pos != len(data):
        raise ValueError("Unsigned transaction has trailing bytes")
    return _ParsedTx(version, inputs, outputs, [], locktime)


def _serialize_tx(
    version: int,
    inputs: list[_TxInput],
    outputs: list[_TxOutput],
    locktime: int,
    witnesses: list[list[bytes]] | None = None,
) -> bytes:
    """Serialize a transaction to raw bytes."""
    parts: list[bytes] = [struct.pack("<I", version)]

    if witnesses:
        parts.append(b"\x00\x01")  # segwit marker + flag

    # Inputs
    parts.append(_encode_varint(len(inputs)))
    for inp in inputs:
        parts.append(inp.txid_le)
        parts.append(struct.pack("<I", inp.vout))
        if inp.scriptsig:
            parts.append(_encode_varint(len(inp.scriptsig)))
            parts.append(inp.scriptsig)
        else:
            parts.append(b"\x00")
        parts.append(struct.pack("<I", inp.sequence))

    # Outputs
    parts.append(_encode_varint(len(outputs)))
    for out in outputs:
        parts.append(struct.pack("<Q", out.value))
        parts.append(_encode_varint(len(out.script)))
        parts.append(out.script)

    # Witnesses
    if witnesses:
        for stack in witnesses:
            parts.append(_encode_varint(len(stack)))
            for item in stack:
                parts.append(_encode_varint(len(item)))
                parts.append(item)

    parts.append(struct.pack("<I", locktime))
    return b"".join(parts)


# ---------------------------------------------------------------------------
# BIP143 sighash (segwit)
# ---------------------------------------------------------------------------


def _compute_sighash_segwit(
    tx: _ParsedTx,
    input_index: int,
    script_code: bytes,
    value: int,
    sighash_type: int = 1,
) -> bytes:
    """Compute BIP143 sighash for a segwit input.

    For P2WSH spending, script_code is the witness script.
    """
    # hashPrevouts
    prevouts = b"".join(inp.txid_le + struct.pack("<I", inp.vout) for inp in tx.inputs)
    hash_prevouts = _hash256(prevouts)

    # hashSequence
    sequences = b"".join(struct.pack("<I", inp.sequence) for inp in tx.inputs)
    hash_sequence = _hash256(sequences)

    # hashOutputs
    outputs_data = b"".join(
        struct.pack("<Q", out.value) + _encode_varint(len(out.script)) + out.script
        for out in tx.outputs
    )
    hash_outputs = _hash256(outputs_data)

    # The input being signed
    inp = tx.inputs[input_index]

    preimage = (
        struct.pack("<I", tx.version)  # nVersion
        + hash_prevouts  # hashPrevouts
        + hash_sequence  # hashSequence
        + inp.txid_le  # outpoint txid
        + struct.pack("<I", inp.vout)  # outpoint index
        + _encode_varint(len(script_code))  # scriptCode length
        + script_code  # scriptCode
        + struct.pack("<Q", value)  # value
        + struct.pack("<I", inp.sequence)  # nSequence
        + hash_outputs  # hashOutputs
        + struct.pack("<I", tx.locktime)  # nLocktime
        + struct.pack("<I", sighash_type)  # sighash type
    )

    return _hash256(preimage)


# ---------------------------------------------------------------------------
# PSBT parser
# ---------------------------------------------------------------------------


def _parse_psbt_map(
    raw: bytes, pos: int, map_name: str
) -> tuple[list[tuple[bytes, bytes]], int]:
    """Parse one PSBT key-value map with explicit size and count bounds."""
    records: list[tuple[bytes, bytes]] = []
    seen_keys: set[bytes] = set()

    while True:
        _require_bytes(raw, pos, 1, f"{map_name} map")
        if raw[pos] == 0:
            return records, pos + 1
        if len(records) >= MAX_PSBT_MAP_ENTRIES:
            raise ValueError(f"{map_name} map has too many records")

        key_len, pos = _read_varint(raw, pos, f"{map_name} key length")
        if key_len == 0:
            raise ValueError(f"{map_name} map has an empty key")
        _require_bytes(raw, pos, key_len, f"{map_name} key")
        key = raw[pos : pos + key_len]
        pos += key_len

        value_len, pos = _read_varint(raw, pos, f"{map_name} value length")
        _require_bytes(raw, pos, value_len, f"{map_name} value")
        value = raw[pos : pos + value_len]
        pos += value_len

        if key in seen_keys:
            raise ValueError(f"{map_name} map has a duplicate key")
        seen_keys.add(key)
        records.append((key, value))


def _parse_witness_utxo(value: bytes) -> tuple[int, bytes]:
    """Parse a witness UTXO value exactly."""
    _require_bytes(value, 0, 8, "witness UTXO value")
    utxo_value = struct.unpack_from("<Q", value, 0)[0]
    if utxo_value > MAX_MONEY:
        raise ValueError(f"Witness UTXO value exceeds MAX_MONEY: {utxo_value}")
    script_len, pos = _read_varint(value, 8, "witness UTXO script length")
    if script_len > MAX_SCRIPT_SIZE:
        raise ValueError(f"Witness UTXO script exceeds size limit: {script_len}")
    _require_bytes(value, pos, script_len, "witness UTXO script")
    script = value[pos : pos + script_len]
    pos += script_len
    if pos != len(value):
        raise ValueError("Witness UTXO has trailing bytes")
    return utxo_value, script


def _decode_minimal_script_number(data: bytes) -> int:
    """Decode a minimally encoded non-negative CLTV script number."""
    if len(data) > 5:
        raise ValueError("Bond locktime script number is too large")
    if data and (data[-1] & 0x7F) == 0 and (len(data) == 1 or not (data[-2] & 0x80)):
        raise ValueError("Bond locktime script number is not minimally encoded")

    value = int.from_bytes(data, "little")
    if data and data[-1] & 0x80:
        value &= ~(0x80 << (8 * (len(data) - 1)))
        value = -value
    return value


def _parse_canonical_bond_script(witness_script: bytes) -> tuple[int, bytes]:
    """Parse ``<locktime> CLTV DROP <pubkey> CHECKSIG`` without extensions."""
    if not witness_script:
        raise ValueError("Bond witness script is empty")

    opcode = witness_script[0]
    pos = 1
    if opcode == 0x00:  # OP_0
        locktime = 0
    elif 0x51 <= opcode <= 0x60:  # OP_1 through OP_16
        locktime = opcode - 0x50
    elif 1 <= opcode <= 5:
        _require_bytes(witness_script, pos, opcode, "bond locktime push")
        encoded_locktime = witness_script[pos : pos + opcode]
        pos += opcode
        if encoded_locktime == b"\x00" or (
            len(encoded_locktime) == 1 and 1 <= encoded_locktime[0] <= 16
        ):
            raise ValueError("Bond locktime uses a non-minimal push")
        locktime = _decode_minimal_script_number(encoded_locktime)
    else:
        raise ValueError("Bond witness script has a non-canonical locktime push")

    if locktime <= 0 or locktime > UINT32_MAX:
        raise ValueError("Bond locktime must be a positive uint32")
    _require_bytes(witness_script, pos, 2, "bond CLTV opcodes")
    if witness_script[pos : pos + 2] != b"\xb1\x75":
        raise ValueError("Bond witness script is not canonical CLTV")
    pos += 2
    _require_bytes(witness_script, pos, 1, "bond public key push")
    if witness_script[pos] != 33:
        raise ValueError("Bond witness script must use a 33-byte public key push")
    pos += 1
    _require_bytes(witness_script, pos, 33, "bond public key")
    pubkey = witness_script[pos : pos + 33]
    pos += 33
    if pubkey[0] not in (0x02, 0x03):
        raise ValueError("Bond witness script has an invalid compressed public key")
    _require_bytes(witness_script, pos, 1, "bond CHECKSIG opcode")
    if witness_script[pos] != 0xAC:
        raise ValueError("Bond witness script is missing CHECKSIG")
    pos += 1
    if pos != len(witness_script):
        raise ValueError("Bond witness script has trailing opcodes")
    return locktime, pubkey


def _parse_bip32_derivation(key: bytes, value: bytes) -> tuple[bytes, bytes, list[int]]:
    """Parse one standard BIP32 derivation record."""
    if len(key) != 34 or key[0] != 0x06:
        raise ValueError("Invalid BIP32 derivation key")
    pubkey = key[1:]
    if pubkey[0] not in (0x02, 0x03):
        raise ValueError("BIP32 derivation has an invalid compressed public key")
    if len(value) < 4 or (len(value) - 4) % 4:
        raise ValueError("BIP32 derivation has an invalid path length")

    fingerprint = value[:4]
    depth = (len(value) - 4) // 4
    if depth > MAX_BIP32_PATH_DEPTH:
        raise ValueError(f"BIP32 derivation path exceeds depth limit: {depth}")
    path_indices = [
        struct.unpack_from("<I", value, offset)[0] for offset in range(4, len(value), 4)
    ]
    return pubkey, fingerprint, path_indices


def parse_psbt(psbt_b64: str) -> dict:
    """Strictly parse and validate a one-input v0 fidelity-bond PSBT.

    The standalone signer deliberately supports only the transaction shape it
    can fully review and sign. All lengths, maps, and transaction fields are
    bounded before secret material is requested.

    Returns:
        Dict with keys:
          - unsigned_tx_bytes: Raw unsigned transaction bytes
          - witness_utxo_value: UTXO value in satoshis
          - witness_utxo_script: UTXO scriptPubKey bytes
          - witness_script: The P2WSH witness script bytes
          - bip32_pubkey: Public key from BIP32 derivation (33 bytes)
          - bip32_fingerprint: Master fingerprint (4 bytes)
          - bip32_path: Derivation path as list of uint32 indices
          - bip32_path_str: Human-readable derivation path string
    """
    if not isinstance(psbt_b64, str):
        raise ValueError("PSBT must be a Base64 string")
    if len(psbt_b64) > MAX_PSBT_TEXT_SIZE:
        raise ValueError("Base64 PSBT input exceeds size limit")
    encoded_psbt = "".join(psbt_b64.split())
    if len(encoded_psbt) > MAX_PSBT_BASE64_SIZE:
        raise ValueError("Base64 PSBT input exceeds size limit")
    try:
        raw = base64.b64decode(encoded_psbt, validate=True)
    except (AttributeError, UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("Invalid PSBT base64") from exc
    if len(raw) > MAX_PSBT_SIZE:
        raise ValueError("PSBT exceeds size limit")

    # Verify magic
    if raw[:5] != b"psbt\xff":
        raise ValueError("Invalid PSBT: missing magic bytes")

    pos = 5
    global_records, pos = _parse_psbt_map(raw, pos, "global")
    unsigned_transactions = [value for key, value in global_records if key == b"\x00"]
    if len(unsigned_transactions) != 1:
        raise ValueError("PSBT must contain exactly one unsigned transaction")
    for key, _ in global_records:
        if key[0] == 0x00 and key != b"\x00":
            raise ValueError("PSBT unsigned transaction field has key data")

    unsigned_tx_bytes = unsigned_transactions[0]
    tx = _parse_tx(unsigned_tx_bytes)
    input_records, pos = _parse_psbt_map(raw, pos, "input 0")
    for output_index in range(len(tx.outputs)):
        _, pos = _parse_psbt_map(raw, pos, f"output {output_index}")
    if pos != len(raw):
        raise ValueError("PSBT has extra input or output maps")

    witness_utxo_records = [value for key, value in input_records if key == b"\x01"]
    if len(witness_utxo_records) != 1:
        raise ValueError("PSBT missing required field: witness_utxo")
    witness_script_records = [value for key, value in input_records if key == b"\x05"]
    if len(witness_script_records) != 1:
        raise ValueError("PSBT missing required field: witness_script")
    for key, _ in input_records:
        if key[0] in (0x01, 0x05) and len(key) != 1:
            raise ValueError("PSBT input field has unexpected key data")

    derivation_records = [
        (key, value) for key, value in input_records if key[0] == 0x06
    ]
    if len(derivation_records) > 1:
        raise ValueError("PSBT has ambiguous BIP32 derivation records")

    utxo_value, utxo_script = _parse_witness_utxo(witness_utxo_records[0])
    witness_script = witness_script_records[0]
    expected_script = b"\x00\x20" + hashlib.sha256(witness_script).digest()
    if utxo_script != expected_script:
        raise ValueError("Witness UTXO scriptPubKey does not match witness script")
    bond_locktime, bond_pubkey = _parse_canonical_bond_script(witness_script)

    if tx.locktime < bond_locktime:
        raise ValueError(
            f"Transaction locktime {tx.locktime} is below bond locktime {bond_locktime}"
        )
    if (tx.locktime < LOCKTIME_THRESHOLD) != (bond_locktime < LOCKTIME_THRESHOLD):
        raise ValueError("Transaction and bond locktimes use different locktime types")
    if tx.inputs[0].sequence == UINT32_MAX:
        raise ValueError("Bond input has a final sequence and cannot satisfy CLTV")

    total_output_value = 0
    for output in tx.outputs:
        total_output_value += output.value
        if total_output_value > utxo_value:
            raise ValueError("Transaction outputs exceed witness UTXO value")
    fee = utxo_value - total_output_value

    result: dict = {
        "unsigned_tx_bytes": unsigned_tx_bytes,
        "witness_utxo_value": utxo_value,
        "witness_utxo_script": utxo_script,
        "witness_script": witness_script,
        "transaction": tx,
        "bond_locktime": bond_locktime,
        "bond_pubkey": bond_pubkey,
        "total_output_value": total_output_value,
        "fee": fee,
    }
    if derivation_records:
        pubkey, fingerprint, path_indices = _parse_bip32_derivation(
            *derivation_records[0]
        )
        if pubkey != bond_pubkey:
            raise ValueError(
                "BIP32 derivation public key does not match bond witness script"
            )
        result["bip32_pubkey"] = pubkey
        result["bip32_fingerprint"] = fingerprint
        result["bip32_path"] = path_indices
        result["bip32_path_str"] = _path_to_string(path_indices)
    return result


# ---------------------------------------------------------------------------
# BIP32 key derivation (inline to avoid pydantic dependency via jmwallet)
# ---------------------------------------------------------------------------


def _parse_derivation_path(path: str) -> list[int]:
    """Parse a BIP32 derivation path string into uint32 indices.

    Accepts: m/84'/0'/0'/0/0 or m/84h/0h/0h/0/0
    """
    if not isinstance(path, str):
        raise ValueError("Derivation path must be a string")
    parts = path.strip().split("/")
    if parts[0] != "m":
        raise ValueError(f"Path must start with 'm': {path}")
    if len(parts) - 1 > MAX_BIP32_PATH_DEPTH:
        raise ValueError(f"Derivation path exceeds depth limit: {len(parts) - 1}")

    indices: list[int] = []
    for part in parts[1:]:
        if not part:
            raise ValueError("Derivation path contains an empty component")
        hardened = part.endswith("'") or part.endswith("h")
        index_text = part[:-1] if hardened else part
        if not index_text or not index_text.isascii() or not index_text.isdecimal():
            raise ValueError(f"Invalid derivation path component: {part}")
        if len(index_text) > 10:
            raise ValueError(f"Derivation path component is out of range: {part}")
        idx = int(index_text)
        if idx >= 0x80000000:
            raise ValueError(f"Derivation path component is out of range: {part}")
        if hardened:
            idx += 0x80000000
        indices.append(idx)
    return indices


def _validate_derivation_indices(indices: list[int]) -> None:
    """Validate BIP32 uint32 child indices before deriving with them."""
    if len(indices) > MAX_BIP32_PATH_DEPTH:
        raise ValueError(f"Derivation path exceeds depth limit: {len(indices)}")
    for index in indices:
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or not 0 <= index <= UINT32_MAX
        ):
            raise ValueError(f"Invalid BIP32 child index: {index!r}")


def derive_key_from_mnemonic(
    mnemonic: str,
    path: str | list[int],
    passphrase: str = "",
) -> tuple[bytes, bytes]:
    """Derive a private key and public key from a BIP39 mnemonic and path.

    Uses standalone BIP39 validation and BIP32 key derivation without project
    imports. Depends on ``python-bitcointx`` with native ``libsecp256k1`` and
    ``mnemonic``.

    Args:
        mnemonic: BIP39 mnemonic phrase (12 or 24 words).
        path: Derivation path as string ("m/84'/0'/0'/0/0") or list of uint32.
        passphrase: Optional BIP39 passphrase.

    Returns:
        Tuple of (private_key_bytes, compressed_public_key_bytes).
    """
    normalized_mnemonic = " ".join(mnemonic.strip().split())
    bip39 = Mnemonic("english")
    if not bip39.check(normalized_mnemonic):
        raise ValueError("Invalid English BIP39 mnemonic or checksum")
    seed = bip39.to_seed(normalized_mnemonic, passphrase=passphrase)

    # BIP32: seed -> master key
    master_hmac = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    master_key_bytes = master_hmac[:32]
    master_chain_code = master_hmac[32:]
    master_key_int = int.from_bytes(master_key_bytes, "big")
    if not 1 <= master_key_int < SECP256K1_N:
        raise ValueError("BIP32 produced an invalid master private key")

    # Derive child keys along the path
    if isinstance(path, list):
        indices = path
    else:
        indices = _parse_derivation_path(path)
    _validate_derivation_indices(indices)

    key_bytes = master_key_bytes
    chain_code = master_chain_code

    for index in indices:
        hardened = index >= 0x80000000
        if hardened:
            # Hardened: HMAC-SHA512(chain_code, 0x00 + key + index)
            data = b"\x00" + key_bytes + struct.pack(">I", index)
        else:
            # Normal: HMAC-SHA512(chain_code, compressed_pubkey + index)
            pubkey = bytes(CKey(key_bytes).pub)
            data = pubkey + struct.pack(">I", index)

        child_hmac = hmac.new(chain_code, data, hashlib.sha512).digest()
        child_key_offset = int.from_bytes(child_hmac[:32], "big")
        if child_key_offset >= SECP256K1_N:
            raise ValueError(f"BIP32 produced an invalid child offset at index {index}")
        parent_key_int = int.from_bytes(key_bytes, "big")
        child_key_int = (parent_key_int + child_key_offset) % SECP256K1_N
        if child_key_int == 0:
            raise ValueError(
                f"BIP32 produced an invalid zero child key at index {index}"
            )

        key_bytes = child_key_int.to_bytes(32, "big")
        chain_code = child_hmac[32:]

    key = CKey(key_bytes)
    return key.secret_bytes, bytes(key.pub)


# ---------------------------------------------------------------------------
# Transaction signing
# ---------------------------------------------------------------------------


def sign_bond_transaction(
    unsigned_tx_bytes: bytes,
    witness_script: bytes,
    utxo_value: int,
    private_key_bytes: bytes,
) -> str:
    """Sign the bond spending transaction and return the signed tx hex.

    Args:
        unsigned_tx_bytes: The raw unsigned transaction from the PSBT.
        witness_script: The P2WSH witness script (CLTV timelock script).
        utxo_value: The UTXO value in satoshis.
        private_key_bytes: The 32-byte private key.

    Returns:
        Hex string of the fully signed transaction, ready to broadcast.
    """
    # Parse the unsigned transaction
    tx = _parse_tx(unsigned_tx_bytes)
    if len(tx.inputs) != 1:
        raise ValueError("Expected exactly one unsigned transaction input")

    # Compute the BIP143 segwit sighash
    sighash_type = 1  # SIGHASH_ALL
    sighash = _compute_sighash_segwit(
        tx=tx,
        input_index=0,
        script_code=witness_script,
        value=utxo_value,
        sighash_type=sighash_type,
    )

    # Sign with the private key (DER-encoded + sighash byte)
    key = CKey(private_key_bytes)
    signature = key.sign(sighash, _ecdsa_sig_grind_low_r=False) + bytes([sighash_type])

    # Witness stack for P2WSH: [signature, witness_script]
    witness_stack = [signature, witness_script]

    # Serialize the fully signed transaction with witness data
    signed_tx = _serialize_tx(
        version=tx.version,
        inputs=tx.inputs,
        outputs=tx.outputs,
        locktime=tx.locktime,
        witnesses=[witness_stack],
    )

    return signed_tx.hex()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sign a fidelity bond spending PSBT with a BIP39 mnemonic.",
        epilog=(
            "The mnemonic is read interactively (never as a CLI argument). "
            "After signing, the signed raw transaction hex is printed to stdout."
        ),
    )
    parser.add_argument(
        "psbt",
        nargs="?",
        help="Base64-encoded PSBT string",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="Read PSBT from file instead of argument",
    )
    parser.add_argument(
        "--derivation-path",
        help=(
            "Override the BIP32 derivation path "
            "(default: extracted from PSBT's BIP32 derivation field)"
        ),
    )
    parser.add_argument(
        "--passphrase",
        action="store_true",
        help="Prompt for a BIP39 passphrase (default: no passphrase)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip interactive transaction confirmation for controlled automation",
    )
    args = parser.parse_args()

    # Read PSBT
    if args.file:
        psbt_b64 = args.file.read_text().strip()
    elif args.psbt:
        psbt_b64 = args.psbt.strip()
    else:
        parser.error("Provide a PSBT as an argument or via --file")

    # Parse PSBT
    print("Parsing PSBT...", file=sys.stderr)
    try:
        psbt_data = parse_psbt(psbt_b64)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # Determine derivation path
    if args.derivation_path:
        try:
            deriv_path = _parse_derivation_path(args.derivation_path)
        except ValueError as e:
            print(f"ERROR: Invalid derivation path: {e}", file=sys.stderr)
            sys.exit(1)
        print(
            f"Using provided derivation path: {_path_to_string(deriv_path)}",
            file=sys.stderr,
        )
    elif "bip32_path" in psbt_data:
        deriv_path = psbt_data["bip32_path"]
        assert isinstance(deriv_path, list)
        fingerprint_hex = psbt_data["bip32_fingerprint"].hex()
        print(
            f"Found BIP32 derivation in PSBT: {_path_to_string(deriv_path)} "
            f"(fingerprint: {fingerprint_hex})",
            file=sys.stderr,
        )
    else:
        print(
            "ERROR: No BIP32 derivation path found in the PSBT.\n"
            "  Either:\n"
            "  - Re-generate the PSBT with --master-fingerprint and --derivation-path\n"
            "  - Or provide --derivation-path to this script",
            file=sys.stderr,
        )
        sys.exit(1)

    # Show every committed transaction field before prompting for secret material.
    witness_script = psbt_data["witness_script"]
    utxo_value = psbt_data["witness_utxo_value"]
    tx = psbt_data["transaction"]
    assert isinstance(witness_script, bytes)
    assert isinstance(utxo_value, int)
    assert isinstance(tx, _ParsedTx)
    print(f"Witness script: {witness_script.hex()}", file=sys.stderr)
    print(f"UTXO value: {utxo_value} sats", file=sys.stderr)
    print(f"Transaction locktime: {tx.locktime}", file=sys.stderr)
    for output_index, output in enumerate(tx.outputs):
        print(f"Output {output_index}: {output.value} sats", file=sys.stderr)
        print(f"  scriptPubKey: {output.script.hex()}", file=sys.stderr)
    print(f"Total output: {psbt_data['total_output_value']} sats", file=sys.stderr)
    print(f"Fee: {psbt_data['fee']} sats", file=sys.stderr)

    if args.force:
        print("WARNING: --force skips transaction confirmation", file=sys.stderr)
    elif not sys.stdin.isatty():
        print(
            "ERROR: Refusing to sign without an interactive confirmation "
            "(use --force only for controlled automation)",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        try:
            confirmation = input("Sign this transaction? [y/N] ")
        except EOFError:
            print("ERROR: Transaction confirmation was not received", file=sys.stderr)
            sys.exit(1)
        if confirmation.strip().lower() not in {"y", "yes"}:
            print("Signing cancelled", file=sys.stderr)
            sys.exit(1)

    # Read mnemonic securely
    print(file=sys.stderr)
    print("Enter your BIP39 mnemonic (12 or 24 words):", file=sys.stderr)
    print("(input is hidden)", file=sys.stderr)
    mnemonic = getpass.getpass(prompt="> ")

    if not mnemonic.strip():
        print("ERROR: Empty mnemonic", file=sys.stderr)
        sys.exit(1)

    # Reject malformed mnemonics before accepting an optional passphrase.
    words = mnemonic.strip().split()
    if len(words) not in (12, 15, 18, 21, 24):
        print(
            f"ERROR: Expected 12-24 words, got {len(words)}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not Mnemonic("english").check(" ".join(words)):
        print("ERROR: Invalid English BIP39 mnemonic or checksum", file=sys.stderr)
        sys.exit(1)

    # Optional passphrase
    passphrase = ""
    if args.passphrase:
        passphrase = getpass.getpass(prompt="BIP39 passphrase: ")

    # Derive key
    print(f"\nDeriving key from path {_path_to_string(deriv_path)}...", file=sys.stderr)
    try:
        privkey_bytes, pubkey_bytes = derive_key_from_mnemonic(
            mnemonic.strip(), deriv_path, passphrase
        )
    except Exception as e:
        print(f"ERROR: Key derivation failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Clear mnemonic from memory (best-effort in Python)
    mnemonic = "x" * len(mnemonic)  # noqa: F841
    del mnemonic

    # The canonical witness script, not optional PSBT metadata, identifies the signing key.
    expected_pubkey = psbt_data["bond_pubkey"]
    assert isinstance(expected_pubkey, bytes)
    if pubkey_bytes != expected_pubkey:
        print(
            f"ERROR: Derived pubkey does not match bond witness script!\n"
            f"  Derived:  {pubkey_bytes.hex()}\n"
            f"  Expected: {expected_pubkey.hex()}\n"
            f"  Check: mnemonic, passphrase, and derivation path",
            file=sys.stderr,
        )
        sys.exit(1)
    print("Pubkey verified: matches bond witness script", file=sys.stderr)

    # Sign
    print("Signing transaction...", file=sys.stderr)
    try:
        signed_tx_hex = sign_bond_transaction(
            unsigned_tx_bytes=psbt_data["unsigned_tx_bytes"],
            witness_script=witness_script,
            utxo_value=utxo_value,
            private_key_bytes=privkey_bytes,
        )
    except Exception as e:
        print(f"ERROR: Signing failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        # Clear key material
        privkey_bytes = b"\x00" * 32  # noqa: F841
        del privkey_bytes

    # Output the signed transaction
    print("\n" + "=" * 80, file=sys.stderr)
    print("SIGNED TRANSACTION (raw hex):", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print(signed_tx_hex)  # stdout -- can be piped
    print("=" * 80, file=sys.stderr)


if __name__ == "__main__":
    main()
