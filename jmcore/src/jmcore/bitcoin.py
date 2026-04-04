"""
Bitcoin utilities for JoinMarket.

This module provides consolidated Bitcoin operations:
- Address encoding/decoding (bech32, base58)
- Hash functions (hash160, hash256)
- Transaction parsing/serialization
- Varint encoding/decoding

Uses external libraries for security-critical operations:
- bech32: BIP173 bech32 encoding
- base58: Base58Check encoding
"""

from __future__ import annotations

import hashlib
import struct
from enum import StrEnum
from typing import Any

import base58
import bech32 as bech32_lib
from pydantic import validate_call
from pydantic.dataclasses import dataclass

from jmcore.constants import MAX_MONEY, SATS_PER_BTC


class NetworkType(StrEnum):
    """Bitcoin network types."""

    MAINNET = "mainnet"
    TESTNET = "testnet"
    SIGNET = "signet"
    REGTEST = "regtest"


# Network prefixes for address encoding
HRP_MAP = {
    NetworkType.MAINNET: "bc",
    NetworkType.TESTNET: "tb",
    NetworkType.SIGNET: "tb",
    NetworkType.REGTEST: "bcrt",
}

# Base58 version bytes
P2PKH_VERSION = {
    NetworkType.MAINNET: 0x00,
    NetworkType.TESTNET: 0x6F,
    NetworkType.SIGNET: 0x6F,
    NetworkType.REGTEST: 0x6F,
}

P2SH_VERSION = {
    NetworkType.MAINNET: 0x05,
    NetworkType.TESTNET: 0xC4,
    NetworkType.SIGNET: 0xC4,
    NetworkType.REGTEST: 0xC4,
}


# =============================================================================
# Amount Utilities
# =============================================================================


def btc_to_sats(btc: float) -> int:
    """
    Convert BTC to satoshis safely.

    Uses round() instead of int() to avoid floating point precision errors
    that can truncate values (e.g. 0.0003 * 1e8 = 29999.999...).

    Args:
        btc: Amount in BTC

    Returns:
        Amount in satoshis
    """
    return round(btc * SATS_PER_BTC)


def sats_to_btc(sats: int) -> float:
    """
    Convert satoshis to BTC. Only use for display/output.

    Args:
        sats: Amount in satoshis

    Returns:
        Amount in BTC
    """
    return sats / SATS_PER_BTC


def format_amount(sats: int, include_unit: bool = True) -> str:
    """
    Format satoshi amount as string.
    Default: '1,000,000 sats (0.01000000 BTC)'

    Args:
        sats: Amount in satoshis
        include_unit: Whether to include units and BTC conversion

    Returns:
        Formatted string
    """
    if include_unit:
        btc_val = sats_to_btc(sats)
        return f"{sats:,} sats ({btc_val:.8f} BTC)"
    return f"{sats:,}"


def validate_satoshi_amount(sats: int) -> None:
    """
    Validate that amount is a non-negative integer.

    Args:
        sats: Amount to validate

    Raises:
        TypeError: If amount is not an integer
        ValueError: If amount is negative
    """
    if not isinstance(sats, int):
        raise TypeError(f"Amount must be an integer (satoshis), got {type(sats)}")
    if sats < 0:
        raise ValueError(f"Amount cannot be negative, got {sats}")


def calculate_relative_fee(amount_sats: int, fee_rate: str) -> int:
    """
    Calculate relative fee in satoshis from a fee rate string.

    Uses Decimal arithmetic with banker's rounding (ROUND_HALF_EVEN) to match
    the reference JoinMarket implementation. This is critical for sweep mode
    where the maker expects the exact same fee calculation.

    Args:
        amount_sats: Amount in satoshis
        fee_rate: Fee rate as decimal string (e.g., "0.001" = 0.1%)

    Returns:
        Fee in satoshis (rounded to nearest integer)

    Examples:
        >>> calculate_relative_fee(100_000_000, "0.001")
        100000  # 0.1% of 1 BTC
        >>> calculate_relative_fee(50_000_000, "0.002")
        100000  # 0.2% of 0.5 BTC
        >>> calculate_relative_fee(9994243, "0.000022")
        220  # matches reference implementation's Decimal rounding
    """
    from decimal import Decimal

    validate_satoshi_amount(amount_sats)

    # Handle integer strings like "0" or "1"
    if "." not in fee_rate:
        try:
            val = int(fee_rate)
            return int(amount_sats * val)
        except ValueError as e:
            raise ValueError(f"Fee rate must be decimal string or integer, got {fee_rate}") from e

    # Use Decimal for exact arithmetic, matching reference implementation
    # Reference uses: int((Decimal(cjfee) * Decimal(cj_amount)).quantize(Decimal(1)))
    # quantize(Decimal(1)) uses ROUND_HALF_EVEN (banker's rounding) by default
    return int((Decimal(fee_rate) * Decimal(amount_sats)).quantize(Decimal(1)))


def calculate_sweep_amount(available_sats: int, relative_fees: list[str]) -> int:
    """
    Calculate CoinJoin amount for a sweep (no change output).

    The taker must pay maker fees from the swept amount:
    available = cj_amount + fees
    fees = sum(fee_rate * cj_amount for each maker)

    Solving for cj_amount:
    available = cj_amount * (1 + sum(fee_rates))
    cj_amount = available / (1 + sum(fee_rates))

    Args:
        available_sats: Total available balance in satoshis
        relative_fees: List of relative fee strings (e.g., ["0.001", "0.002"])

    Returns:
        CoinJoin amount in satoshis (maximum amount after paying all fees)
    """
    validate_satoshi_amount(available_sats)

    if not relative_fees:
        return available_sats

    # Parse all fee rates as fractions with common denominator
    # Example: ["0.001", "0.0015"] -> numerators=[1, 15], denominator=10000
    try:
        max_decimals = 0
        for fee in relative_fees:
            if "." in fee:
                max_decimals = max(max_decimals, len(fee.split(".")[1]))
    except IndexError as e:
        raise ValueError(f"Invalid fee format in {relative_fees}") from e

    denominator = 10**max_decimals

    sum_numerators = 0
    for fee_rate in relative_fees:
        if "." in fee_rate:
            parts = fee_rate.split(".")
            # Normalize to common denominator
            # "0.001" with max_decimals=4 -> 10 (because 0.001 = 10/10000)
            numerator = int(parts[0] + parts[1]) * (10 ** (max_decimals - len(parts[1])))
            sum_numerators += numerator
        else:
            # Handle integer fee rates (unlikely for relative fees but good for robustness)
            numerator = int(fee_rate) * denominator
            sum_numerators += numerator

    # cj_amount = available / (1 + sum_rel_fees)
    #           = available / ((denominator + sum_numerators) / denominator)
    #           = (available * denominator) / (denominator + sum_numerators)
    return (available_sats * denominator) // (denominator + sum_numerators)


# =============================================================================
# Hash Functions
# =============================================================================


def hash160(data: bytes) -> bytes:
    """
    RIPEMD160(SHA256(data)) - Used for Bitcoin addresses.

    Args:
        data: Input data to hash

    Returns:
        20-byte hash
    """
    return hashlib.new("ripemd160", hashlib.sha256(data).digest()).digest()


def hash256(data: bytes) -> bytes:
    """
    SHA256(SHA256(data)) - Used for Bitcoin txids and block hashes.

    Args:
        data: Input data to hash

    Returns:
        32-byte hash
    """
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def sha256(data: bytes) -> bytes:
    """
    Single SHA256 hash.

    Args:
        data: Input data to hash

    Returns:
        32-byte hash
    """
    return hashlib.sha256(data).digest()


# =============================================================================
# Varint Encoding/Decoding
# =============================================================================


def encode_varint(n: int) -> bytes:
    """
    Encode integer as Bitcoin varint.

    Args:
        n: Integer to encode

    Returns:
        Encoded bytes
    """
    if n < 0xFD:
        return bytes([n])
    elif n <= 0xFFFF:
        return bytes([0xFD]) + struct.pack("<H", n)
    elif n <= 0xFFFFFFFF:
        return bytes([0xFE]) + struct.pack("<I", n)
    else:
        return bytes([0xFF]) + struct.pack("<Q", n)


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    """
    Decode Bitcoin varint from bytes.

    Args:
        data: Input bytes
        offset: Starting offset in data

    Returns:
        (value, new_offset) tuple
    """
    first = data[offset]
    if first < 0xFD:
        return first, offset + 1
    elif first == 0xFD:
        return struct.unpack("<H", data[offset + 1 : offset + 3])[0], offset + 3
    elif first == 0xFE:
        return struct.unpack("<I", data[offset + 1 : offset + 5])[0], offset + 5
    else:
        return struct.unpack("<Q", data[offset + 1 : offset + 9])[0], offset + 9


# =============================================================================
# Address Encoding/Decoding
# =============================================================================


def get_hrp(network: str | NetworkType) -> str:
    """
    Get bech32 human-readable part for network.

    Args:
        network: Network type (string or enum)

    Returns:
        HRP string (bc, tb, bcrt)
    """
    if isinstance(network, str):
        network = NetworkType(network)
    return HRP_MAP[network]


@validate_call
def pubkey_to_p2wpkh_address(pubkey: bytes | str, network: str | NetworkType = "mainnet") -> str:
    """
    Convert compressed public key to P2WPKH (native SegWit) address.

    Args:
        pubkey: 33-byte compressed public key (bytes or hex string)
        network: Network type

    Returns:
        Bech32 P2WPKH address
    """
    if isinstance(pubkey, str):
        pubkey = bytes.fromhex(pubkey)

    if len(pubkey) != 33:
        raise ValueError(f"Invalid compressed pubkey length: {len(pubkey)}")

    pubkey_hash = hash160(pubkey)
    hrp = get_hrp(network)

    result = bech32_lib.encode(hrp, 0, pubkey_hash)
    if result is None:
        raise ValueError("Failed to encode bech32 address")
    return result


def pubkey_to_p2wpkh_script(pubkey: bytes | str) -> bytes:
    """
    Create P2WPKH scriptPubKey from public key.

    Args:
        pubkey: 33-byte compressed public key (bytes or hex string)

    Returns:
        22-byte P2WPKH scriptPubKey (OP_0 <20-byte-hash>)
    """
    if isinstance(pubkey, str):
        pubkey = bytes.fromhex(pubkey)

    pubkey_hash = hash160(pubkey)
    return bytes([0x00, 0x14]) + pubkey_hash


@validate_call
def script_to_p2wsh_address(script: bytes, network: str | NetworkType = "mainnet") -> str:
    """
    Convert witness script to P2WSH address.

    Args:
        script: Witness script bytes
        network: Network type

    Returns:
        Bech32 P2WSH address
    """
    script_hash = sha256(script)
    hrp = get_hrp(network)

    result = bech32_lib.encode(hrp, 0, script_hash)
    if result is None:
        raise ValueError("Failed to encode bech32 address")
    return result


def script_to_p2wsh_scriptpubkey(script: bytes) -> bytes:
    """
    Create P2WSH scriptPubKey from witness script.

    Args:
        script: Witness script bytes

    Returns:
        34-byte P2WSH scriptPubKey (OP_0 <32-byte-hash>)
    """
    script_hash = sha256(script)
    return bytes([0x00, 0x20]) + script_hash


def address_to_scriptpubkey(address: str) -> bytes:
    """
    Convert Bitcoin address to scriptPubKey.

    Supports:
    - P2WPKH (bc1q..., tb1q..., bcrt1q...)
    - P2WSH (bc1q... 62 chars)
    - P2TR (bc1p... taproot)
    - P2PKH (1..., m..., n...)
    - P2SH (3..., 2...)

    Args:
        address: Bitcoin address string

    Returns:
        scriptPubKey bytes
    """
    # Bech32 (SegWit) addresses
    if address.startswith(("bc1", "tb1", "bcrt1")):
        hrp_end = 4 if address.startswith("bcrt") else 2
        hrp = address[:hrp_end]

        bech32_decoded = bech32_lib.decode(hrp, address)
        if bech32_decoded[0] is None or bech32_decoded[1] is None:
            raise ValueError(f"Invalid bech32 address: {address}")

        witver = bech32_decoded[0]
        witprog = bytes(bech32_decoded[1])

        if witver == 0:
            if len(witprog) == 20:
                # P2WPKH: OP_0 <20-byte-pubkeyhash>
                return bytes([0x00, 0x14]) + witprog
            elif len(witprog) == 32:
                # P2WSH: OP_0 <32-byte-scripthash>
                return bytes([0x00, 0x20]) + witprog
        elif witver == 1 and len(witprog) == 32:
            # P2TR: OP_1 <32-byte-pubkey>
            return bytes([0x51, 0x20]) + witprog

        raise ValueError(f"Unsupported witness version: {witver}")

    # Base58 addresses (legacy)
    decoded = base58.b58decode_check(address)
    version = decoded[0]
    payload = decoded[1:]

    if version in (0x00, 0x6F):  # Mainnet/Testnet P2PKH
        # P2PKH: OP_DUP OP_HASH160 <20-byte-pubkeyhash> OP_EQUALVERIFY OP_CHECKSIG
        return bytes([0x76, 0xA9, 0x14]) + payload + bytes([0x88, 0xAC])
    elif version in (0x05, 0xC4):  # Mainnet/Testnet P2SH
        # P2SH: OP_HASH160 <20-byte-scripthash> OP_EQUAL
        return bytes([0xA9, 0x14]) + payload + bytes([0x87])

    raise ValueError(f"Unknown address version: {version}")


@validate_call
def scriptpubkey_to_address(scriptpubkey: bytes, network: str | NetworkType = "mainnet") -> str:
    """
    Convert scriptPubKey to address.

    Supports P2WPKH, P2WSH, P2TR, P2PKH, P2SH.

    Args:
        scriptpubkey: scriptPubKey bytes
        network: Network type

    Returns:
        Bitcoin address string
    """
    if isinstance(network, str):
        network = NetworkType(network)

    hrp = get_hrp(network)

    # P2WPKH
    if len(scriptpubkey) == 22 and scriptpubkey[0] == 0x00 and scriptpubkey[1] == 0x14:
        result = bech32_lib.encode(hrp, 0, scriptpubkey[2:])
        if result is None:
            raise ValueError(f"Failed to encode P2WPKH address: {scriptpubkey.hex()}")
        return result

    # P2WSH
    if len(scriptpubkey) == 34 and scriptpubkey[0] == 0x00 and scriptpubkey[1] == 0x20:
        result = bech32_lib.encode(hrp, 0, scriptpubkey[2:])
        if result is None:
            raise ValueError(f"Failed to encode P2WSH address: {scriptpubkey.hex()}")
        return result

    # P2TR
    if len(scriptpubkey) == 34 and scriptpubkey[0] == 0x51 and scriptpubkey[1] == 0x20:
        result = bech32_lib.encode(hrp, 1, scriptpubkey[2:])
        if result is None:
            raise ValueError(f"Failed to encode P2TR address: {scriptpubkey.hex()}")
        return result

    # P2PKH
    if (
        len(scriptpubkey) == 25
        and scriptpubkey[0] == 0x76
        and scriptpubkey[1] == 0xA9
        and scriptpubkey[2] == 0x14
        and scriptpubkey[23] == 0x88
        and scriptpubkey[24] == 0xAC
    ):
        payload = bytes([P2PKH_VERSION[network]]) + scriptpubkey[3:23]
        return base58.b58encode_check(payload).decode("ascii")

    # P2SH
    if (
        len(scriptpubkey) == 23
        and scriptpubkey[0] == 0xA9
        and scriptpubkey[1] == 0x14
        and scriptpubkey[22] == 0x87
    ):
        payload = bytes([P2SH_VERSION[network]]) + scriptpubkey[2:22]
        return base58.b58encode_check(payload).decode("ascii")

    raise ValueError(f"Unsupported scriptPubKey: {scriptpubkey.hex()}")


# =============================================================================
# Transaction Models
# =============================================================================


@dataclass
class TxInput:
    """Unified transaction input model.

    Stores data in canonical byte form internally.  Provides dual accessors for
    the two dominant usage patterns in the codebase:

    * **String pattern** (RPC / human-readable): ``txid`` (big-endian hex),
      ``scriptsig_hex``, ``scriptpubkey_hex``, ``sequence`` (int).
    * **Bytes pattern** (BIP-143 signing): ``txid_le`` (little-endian bytes),
      ``scriptsig`` (bytes), ``sequence_bytes`` (4-byte LE bytes).

    Construction helpers
    --------------------
    * ``TxInput.from_hex(txid_hex, vout, ...)`` — build from big-endian hex
      txid (the format returned by Bitcoin Core RPC).
    * Direct ``TxInput(txid_le=..., vout=..., ...)`` — build from raw LE bytes
      (the format found inside serialised transactions).
    """

    # --- canonical fields (stored as-is) ------------------------------------
    txid_le: bytes  # 32-byte txid in little-endian (wire / internal format)
    vout: int
    scriptsig: bytes = b""
    sequence: int = 0xFFFFFFFF
    value: int = 0  # Optional: UTXO value (needed by tx builder / sighash)
    scriptpubkey: bytes = b""  # Optional: prevout scriptPubKey

    # --- string accessors (big-endian hex) ----------------------------------

    @property
    def txid(self) -> str:
        """Transaction ID as big-endian hex (RPC / display format)."""
        return self.txid_le[::-1].hex()

    @property
    def scriptsig_hex(self) -> str:
        """ScriptSig as hex string."""
        return self.scriptsig.hex()

    @property
    def scriptpubkey_hex(self) -> str:
        """ScriptPubKey of the prevout as hex string."""
        return self.scriptpubkey.hex()

    # --- bytes accessors (for BIP-143 sighash) ------------------------------

    @property
    def sequence_bytes(self) -> bytes:
        """Sequence as 4-byte little-endian bytes (for BIP-143 preimage)."""
        return struct.pack("<I", self.sequence)

    # --- dict-like access (backward compat during migration) ----------------

    def __getitem__(self, key: str) -> Any:
        """Allow ``inp["txid"]`` style access for backward compatibility."""
        if key == "txid":
            return self.txid
        if key == "vout":
            return self.vout
        if key == "scriptsig":
            return self.scriptsig_hex
        if key == "sequence":
            return self.sequence
        if key == "value":
            return self.value
        if key == "scriptpubkey":
            return self.scriptpubkey_hex
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Allow ``inp.get("key", default)`` for backward compatibility."""
        try:
            return self[key]
        except KeyError:
            return default

    # --- factories ----------------------------------------------------------

    @classmethod
    def from_hex(
        cls,
        txid: str,
        vout: int,
        *,
        scriptsig: str = "",
        sequence: int = 0xFFFFFFFF,
        value: int = 0,
        scriptpubkey: str = "",
    ) -> TxInput:
        """Create from big-endian hex txid (the RPC / display format).

        Args:
            txid: 64-char hex string (big-endian, as returned by RPC)
            vout: Output index
            scriptsig: ScriptSig hex (default empty)
            sequence: Sequence number (default 0xFFFFFFFF)
            value: UTXO value in satoshis (optional, for tx builder)
            scriptpubkey: Prevout scriptPubKey hex (optional)
        """
        return cls(
            txid_le=bytes.fromhex(txid)[::-1],
            vout=vout,
            scriptsig=bytes.fromhex(scriptsig) if scriptsig else b"",
            sequence=sequence,
            value=value,
            scriptpubkey=bytes.fromhex(scriptpubkey) if scriptpubkey else b"",
        )


@dataclass
class TxOutput:
    """Unified transaction output model.

    Stores ``value`` and ``script`` (scriptPubKey) in canonical byte form.
    Provides convenience accessors for hex and address representations.
    """

    value: int
    script: bytes  # scriptPubKey bytes

    # --- string accessors ---------------------------------------------------

    @property
    def scriptpubkey(self) -> str:
        """ScriptPubKey as hex string (backward compat alias)."""
        return self.script.hex()

    def address(self, network: str | NetworkType = "mainnet") -> str:
        """Derive address from scriptPubKey.

        Args:
            network: Network type for bech32/base58 encoding.

        Returns:
            Address string.

        Raises:
            ValueError: If scriptPubKey is an unsupported type.
        """
        return scriptpubkey_to_address(self.script, network)

    # --- dict-like access (backward compat during migration) ----------------

    def __getitem__(self, key: str) -> Any:
        """Allow ``out["value"]`` style access for backward compatibility."""
        if key == "value":
            return self.value
        if key == "scriptpubkey":
            return self.scriptpubkey
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        """Allow ``out.get("key", default)`` for backward compatibility."""
        try:
            return self[key]
        except KeyError:
            return default

    # --- factories ----------------------------------------------------------

    @classmethod
    def from_address(
        cls,
        address: str,
        value: int,
    ) -> TxOutput:
        """Create from address string (resolves to scriptPubKey).

        Args:
            address: Bitcoin address (any supported format)
            value: Output value in satoshis
        """
        return cls(value=value, script=address_to_scriptpubkey(address))

    @classmethod
    def from_hex(cls, scriptpubkey: str, value: int) -> TxOutput:
        """Create from hex scriptPubKey.

        Args:
            scriptpubkey: ScriptPubKey as hex string
            value: Output value in satoshis
        """
        return cls(value=value, script=bytes.fromhex(scriptpubkey))


@dataclass
class ParsedTransaction:
    """Parsed Bitcoin transaction with typed inputs and outputs.

    Provides dual accessors for int and bytes representations of version
    and locktime (needed by BIP-143 sighash construction).
    """

    version: int
    inputs: list[TxInput]
    outputs: list[TxOutput]
    witnesses: list[list[bytes]]
    locktime: int
    has_witness: bool

    # --- bytes accessors (for BIP-143 sighash) ------------------------------

    @property
    def version_bytes(self) -> bytes:
        """Version as 4-byte little-endian bytes."""
        return struct.pack("<I", self.version)

    @property
    def locktime_bytes(self) -> bytes:
        """Locktime as 4-byte little-endian bytes."""
        return struct.pack("<I", self.locktime)


# =============================================================================
# Transaction Serialization/Parsing
# =============================================================================


def serialize_outpoint(txid: str, vout: int) -> bytes:
    """
    Serialize outpoint (txid:vout).

    Args:
        txid: Transaction ID in RPC format (big-endian hex)
        vout: Output index

    Returns:
        36-byte outpoint (little-endian txid + 4-byte vout)
    """
    txid_bytes = bytes.fromhex(txid)[::-1]
    return txid_bytes + struct.pack("<I", vout)


def serialize_input(inp: TxInput, include_scriptsig: bool = True) -> bytes:
    """
    Serialize a transaction input.

    Args:
        inp: TxInput instance
        include_scriptsig: Whether to include scriptSig

    Returns:
        Serialized input bytes
    """
    result = inp.txid_le + struct.pack("<I", inp.vout)

    if include_scriptsig and inp.scriptsig:
        result += encode_varint(len(inp.scriptsig)) + inp.scriptsig
    else:
        result += bytes([0x00])  # Empty scriptSig

    result += struct.pack("<I", inp.sequence)
    return result


def serialize_output(out: TxOutput) -> bytes:
    """
    Serialize a transaction output.

    Args:
        out: TxOutput instance

    Returns:
        Serialized output bytes
    """
    result = struct.pack("<Q", out.value)
    result += encode_varint(len(out.script))
    result += out.script
    return result


def parse_transaction(tx_hex: str) -> ParsedTransaction:
    """
    Parse a Bitcoin transaction from hex.

    Handles both SegWit and non-SegWit formats.

    Args:
        tx_hex: Transaction hex string

    Returns:
        ParsedTransaction object with typed TxInput/TxOutput lists
    """
    tx_bytes = bytes.fromhex(tx_hex)
    return parse_transaction_bytes(tx_bytes)


def parse_transaction_bytes(tx_bytes: bytes) -> ParsedTransaction:
    """
    Parse a Bitcoin transaction from raw bytes.

    Handles both SegWit and non-SegWit formats.

    Args:
        tx_bytes: Raw transaction bytes

    Returns:
        ParsedTransaction object with typed TxInput/TxOutput lists
    """
    offset = 0

    # Version
    version = struct.unpack("<I", tx_bytes[offset : offset + 4])[0]
    offset += 4

    # Check for SegWit marker
    marker = tx_bytes[offset]
    flag = tx_bytes[offset + 1]
    has_witness = marker == 0x00 and flag == 0x01
    if has_witness:
        offset += 2

    # Inputs
    input_count, offset = decode_varint(tx_bytes, offset)
    inputs: list[TxInput] = []
    for _ in range(input_count):
        txid_le = tx_bytes[offset : offset + 32]
        offset += 32
        vout = struct.unpack("<I", tx_bytes[offset : offset + 4])[0]
        offset += 4
        script_len, offset = decode_varint(tx_bytes, offset)
        scriptsig = tx_bytes[offset : offset + script_len]
        offset += script_len
        sequence = struct.unpack("<I", tx_bytes[offset : offset + 4])[0]
        offset += 4
        inputs.append(TxInput(txid_le=txid_le, vout=vout, scriptsig=scriptsig, sequence=sequence))

    # Outputs
    output_count, offset = decode_varint(tx_bytes, offset)
    outputs: list[TxOutput] = []
    for _ in range(output_count):
        value = struct.unpack("<q", tx_bytes[offset : offset + 8])[0]
        if value < 0 or value > MAX_MONEY:
            msg = f"Output value {value} outside valid range [0, {MAX_MONEY}]"
            raise ValueError(msg)
        offset += 8
        script_len, offset = decode_varint(tx_bytes, offset)
        script = tx_bytes[offset : offset + script_len]
        offset += script_len
        outputs.append(TxOutput(value=value, script=script))

    # Witnesses
    witnesses: list[list[bytes]] = []
    if has_witness:
        for _ in range(input_count):
            wit_count, offset = decode_varint(tx_bytes, offset)
            wit_items = []
            for _ in range(wit_count):
                item_len, offset = decode_varint(tx_bytes, offset)
                wit_items.append(tx_bytes[offset : offset + item_len])
                offset += item_len
            witnesses.append(wit_items)

    # Locktime
    locktime = struct.unpack("<I", tx_bytes[offset : offset + 4])[0]

    return ParsedTransaction(
        version=version,
        inputs=inputs,
        outputs=outputs,
        witnesses=witnesses,
        locktime=locktime,
        has_witness=has_witness,
    )


def serialize_transaction(
    version: int,
    inputs: list[TxInput],
    outputs: list[TxOutput],
    locktime: int,
    witnesses: list[list[bytes]] | None = None,
) -> bytes:
    """
    Serialize a Bitcoin transaction.

    Args:
        version: Transaction version
        inputs: List of TxInput objects
        outputs: List of TxOutput objects
        locktime: Transaction locktime
        witnesses: Optional list of witness stacks

    Returns:
        Serialized transaction bytes
    """
    has_witness = witnesses is not None and any(w for w in witnesses)

    result = struct.pack("<I", version)

    if has_witness:
        result += bytes([0x00, 0x01])  # SegWit marker and flag

    # Inputs
    result += encode_varint(len(inputs))
    for inp in inputs:
        result += serialize_input(inp)

    # Outputs
    result += encode_varint(len(outputs))
    for out in outputs:
        result += serialize_output(out)

    # Witnesses
    if has_witness and witnesses:
        for witness in witnesses:
            result += encode_varint(len(witness))
            for item in witness:
                result += encode_varint(len(item))
                result += item

    result += struct.pack("<I", locktime)
    return result


def get_txid(tx_hex: str) -> str:
    """
    Calculate transaction ID (double SHA256 of non-witness data).

    Args:
        tx_hex: Transaction hex

    Returns:
        Transaction ID as hex string
    """
    parsed = parse_transaction(tx_hex)

    # Serialize without witness for txid calculation
    data = serialize_transaction(
        version=parsed.version,
        inputs=parsed.inputs,
        outputs=parsed.outputs,
        locktime=parsed.locktime,
        witnesses=None,  # No witnesses for txid
    )

    return hash256(data)[::-1].hex()


# =============================================================================
# Script Code (for signing)
# =============================================================================


def create_p2wpkh_script_code(pubkey: bytes | str) -> bytes:
    """
    Create scriptCode for P2WPKH signing (BIP143).

    For P2WPKH, the scriptCode is the P2PKH script:
    OP_DUP OP_HASH160 <20-byte-pubkeyhash> OP_EQUALVERIFY OP_CHECKSIG

    Args:
        pubkey: Public key bytes or hex

    Returns:
        25-byte scriptCode
    """
    if isinstance(pubkey, str):
        pubkey = bytes.fromhex(pubkey)

    pubkey_hash = hash160(pubkey)
    # OP_DUP OP_HASH160 PUSH20 <pkh> OP_EQUALVERIFY OP_CHECKSIG
    return b"\x76\xa9\x14" + pubkey_hash + b"\x88\xac"


# =============================================================================
# Size Estimation
# =============================================================================


def get_address_type(address: str) -> str:
    """
    Determine address type from string.

    Args:
        address: Bitcoin address

    Returns:
        Address type: "p2wpkh", "p2wsh", "p2tr", "p2pkh", "p2sh"

    Raises:
        ValueError: If address is invalid or unknown type
    """
    # Bech32 (SegWit)
    if address.startswith(("bc1", "tb1", "bcrt1")):
        hrp_end = 4 if address.startswith("bcrt") else 2
        hrp = address[:hrp_end]

        decoded = bech32_lib.decode(hrp, address)
        if decoded[0] is None or decoded[1] is None:
            raise ValueError(f"Invalid bech32 address: {address}")

        witver = decoded[0]
        witprog = bytes(decoded[1])

        if witver == 0:
            if len(witprog) == 20:
                return "p2wpkh"
            elif len(witprog) == 32:
                return "p2wsh"
        elif witver == 1 and len(witprog) == 32:
            return "p2tr"

        raise ValueError(f"Unknown SegWit address type: version={witver}, len={len(witprog)}")

    # Base58
    try:
        decoded = base58.b58decode_check(address)
        version = decoded[0]
        if version in (0x00, 0x6F):  # P2PKH
            return "p2pkh"
        elif version in (0x05, 0xC4):  # P2SH
            return "p2sh"
    except Exception:
        pass

    raise ValueError(f"Unknown address type: {address}")


def estimate_vsize(input_types: list[str], output_types: list[str]) -> int:
    """
    Estimate transaction virtual size (vbytes).

    Based on JoinMarket reference implementation logic.

    Args:
        input_types: List of input types (e.g. ["p2wpkh", "p2wsh"])
        output_types: List of output types (e.g. ["p2wpkh", "p2wsh"])

    Returns:
        Estimated vsize in bytes
    """
    # Sizes in weight units (wu) = 4 * vbytes
    # Base transaction overhead: version(4) + locktime(4) + input_count(1) + output_count(1)
    # SegWit marker(1) + flag(1)
    # Total base: 10 bytes -> 40 wu
    # We assume varints for counts are 1 byte (up to 252 inputs/outputs)
    base_weight = 40 + 2  # +2 for marker/flag weight (witness data)

    # Input sizes (weight units)
    # P2WPKH:
    #   Non-witness: 32(txid) + 4(vout) + 1(script_len) + 4(seq) = 41 bytes -> 164 wu
    #   Witness: 1(stack_len) + 1(sig_len) + 72(sig) + 1(pub_len) + 33(pub) = 108 wu
    #   Total: 272 wu (68 vbytes)
    # P2WSH (fidelity bond):
    #   Non-witness: 41 bytes -> 164 wu
    #   Witness: 1(stack_len) + 1(sig_len) + 72(sig) + 1(script_len) + 43(script) = 118 wu
    #   Total: 282 wu (70.5 vbytes) - Ref impl uses slightly different calc, let's stick to calculated
    input_weights = {
        "p2wpkh": 41 * 4 + 108,
        "p2wsh": 41 * 4 + 118,  # Using 72 byte sig + 43 byte script (fidelity bond)
    }

    # Output sizes (weight units)
    # P2WPKH: 8(val) + 1(len) + 22(script) = 31 bytes -> 124 wu
    # P2WSH:  8(val) + 1(len) + 34(script) = 43 bytes -> 172 wu
    # P2TR:   8(val) + 1(len) + 34(script) = 43 bytes -> 172 wu
    output_weights = {
        "p2wpkh": 31 * 4,
        "p2wsh": 43 * 4,
        "p2tr": 43 * 4,
        "p2pkh": 34 * 4,
        "p2sh": 32 * 4,
    }

    weight = base_weight

    for inp in input_types:
        weight += input_weights.get(inp, 272)  # Default to P2WPKH if unknown

    for out in output_types:
        weight += output_weights.get(out, 124)  # Default to P2WPKH

    # vsize = ceil(weight / 4)
    return (weight + 3) // 4


def calculate_tx_vsize(tx_bytes: bytes) -> int:
    """
    Calculate actual virtual size (vbytes) from a signed transaction.

    For SegWit transactions: vsize = ceil((3 * non_witness_size + total_size) / 4)
    For legacy transactions: vsize = total_size

    Args:
        tx_bytes: Serialized transaction bytes

    Returns:
        Virtual size in vbytes
    """
    total_size = len(tx_bytes)

    # Check if this is a SegWit transaction (has marker 0x00 and flag 0x01 after version)
    if len(tx_bytes) > 6 and tx_bytes[4] == 0x00 and tx_bytes[5] == 0x01:
        # SegWit transaction - need to calculate non-witness size
        # Parse to find witness data boundaries
        offset = 4  # Skip version

        # Skip marker and flag
        offset += 2

        # Read input count
        input_count, offset = decode_varint(tx_bytes, offset)

        # Skip inputs (each has: 32 txid + 4 vout + varint script_len + script + 4 sequence)
        for _ in range(input_count):
            offset += 32 + 4  # txid + vout
            script_len, offset = decode_varint(tx_bytes, offset)
            offset += script_len + 4  # script + sequence

        # Read output count
        output_count, offset = decode_varint(tx_bytes, offset)

        # Skip outputs (each has: 8 value + varint script_len + script)
        for _ in range(output_count):
            offset += 8  # value
            script_len, offset = decode_varint(tx_bytes, offset)
            offset += script_len

        # Now offset points to the start of witness data
        witness_start = offset

        # Skip witness data (one stack per input)
        for _ in range(input_count):
            stack_count, offset = decode_varint(tx_bytes, offset)
            for _ in range(stack_count):
                item_len, offset = decode_varint(tx_bytes, offset)
                offset += item_len

        # After witness comes locktime (4 bytes)
        witness_end = offset

        # Non-witness size = total - witness_data - marker(1) - flag(1)
        witness_size = witness_end - witness_start
        non_witness_size = total_size - witness_size - 2  # -2 for marker and flag

        # Weight = non_witness_size * 4 + witness_size (witness counts as 1 weight unit per byte)
        # But we also need to add marker+flag to witness weight (they're part of witness)
        weight = non_witness_size * 4 + witness_size + 2  # +2 for marker/flag at 1 wu each

        # vsize = ceil(weight / 4)
        return (weight + 3) // 4
    else:
        # Legacy transaction - vsize equals byte size
        return total_size


# =============================================================================
# PSBT (BIP-174) Serialization
# =============================================================================

# PSBT magic bytes: "psbt" in ASCII + 0xff separator
PSBT_MAGIC = b"\x70\x73\x62\x74\xff"

# Global types
PSBT_GLOBAL_UNSIGNED_TX = 0x00

# Per-input types
PSBT_IN_WITNESS_UTXO = 0x01
PSBT_IN_SIGHASH_TYPE = 0x03
PSBT_IN_WITNESS_SCRIPT = 0x05
PSBT_IN_BIP32_DERIVATION = 0x06


def _serialize_psbt_key(key_type: int, key_data: bytes = b"") -> bytes:
    """Serialize a PSBT key (type byte + optional key data) with length prefix.

    BIP-174 format: <varint key-len> <key-type> [<key-data>]
    """
    key_bytes = bytes([key_type]) + key_data
    return encode_varint(len(key_bytes)) + key_bytes


def _serialize_psbt_value(value: bytes) -> bytes:
    """Serialize a PSBT value with length prefix.

    BIP-174 format: <varint value-len> <value>
    """
    return encode_varint(len(value)) + value


def _serialize_psbt_pair(key_type: int, value: bytes, key_data: bytes = b"") -> bytes:
    """Serialize a single PSBT key-value pair."""
    return _serialize_psbt_key(key_type, key_data) + _serialize_psbt_value(value)


# Separator byte marking end of a PSBT map
PSBT_SEPARATOR = b"\x00"


@dataclass
class BIP32Derivation:
    """BIP32 key origin information for a PSBT input/output.

    Used to tell signing devices which key to use (PSBT_IN_BIP32_DERIVATION).

    Attributes:
        pubkey: The compressed public key (33 bytes).
        fingerprint: Master key fingerprint (4 bytes).
        path: BIP32 derivation path as list of uint32 indices
              (e.g. [0x80000054, 0x80000000, 0x80000000, 0, 0] for m/84'/0'/0'/0/0).
    """

    pubkey: bytes
    fingerprint: bytes
    path: list[int]

    def __post_init__(self) -> None:
        if len(self.pubkey) != 33:
            raise ValueError(f"pubkey must be 33 bytes, got {len(self.pubkey)}")
        if len(self.fingerprint) != 4:
            raise ValueError(f"fingerprint must be 4 bytes, got {len(self.fingerprint)}")


def parse_derivation_path(path_str: str) -> list[int]:
    """Parse a BIP32 derivation path string into a list of uint32 indices.

    Handles hardened notation with ' or h suffix.

    Examples:
        >>> parse_derivation_path("m/84'/0'/0'/0/0")
        [2147483732, 2147483648, 2147483648, 0, 0]

    Args:
        path_str: Derivation path like "m/84'/0'/0'/0/0".

    Returns:
        List of uint32 path indices (hardened indices have bit 31 set).

    Raises:
        ValueError: If the path format is invalid.
    """
    path_str = path_str.strip()
    if path_str.startswith("m/"):
        path_str = path_str[2:]
    elif path_str == "m":
        return []

    indices: list[int] = []
    for component in path_str.split("/"):
        component = component.strip()
        if not component:
            continue
        hardened = component.endswith("'") or component.endswith("h")
        if hardened:
            component = component[:-1]
        try:
            index = int(component)
        except ValueError:
            raise ValueError(f"Invalid path component: {component!r}") from None
        if index < 0 or index >= 0x80000000:
            raise ValueError(f"Path index out of range: {index}")
        if hardened:
            index |= 0x80000000
        indices.append(index)
    return indices


@dataclass
class PSBTInput:
    """Data needed for a PSBT per-input map.

    Attributes:
        witness_utxo_value: Value of the UTXO in satoshis.
        witness_utxo_script: scriptPubKey of the UTXO (e.g. P2WSH 34-byte script).
        witness_script: The full witness script (redeem script) for P2WSH inputs.
        sighash_type: Sighash type (default SIGHASH_ALL = 0x01).
        bip32_derivations: Optional BIP32 key origin info for signing devices.
    """

    witness_utxo_value: int
    witness_utxo_script: bytes
    witness_script: bytes
    sighash_type: int = 1
    bip32_derivations: list[BIP32Derivation] | None = None


def create_psbt(
    version: int,
    inputs: list[TxInput],
    outputs: list[TxOutput],
    locktime: int,
    psbt_inputs: list[PSBTInput],
) -> bytes:
    """Create a PSBT (BIP-174) from unsigned transaction components.

    Builds a complete PSBT with:
    - Global map: the unsigned transaction (no witness data, empty scriptSigs)
    - Per-input maps: WITNESS_UTXO, WITNESS_SCRIPT, SIGHASH_TYPE
    - Per-output maps: empty (no metadata needed for spending)

    The resulting PSBT can be imported into hardware wallet software
    (e.g. Sparrow, Coldcard) for signing.

    Args:
        version: Transaction version (typically 2).
        inputs: Transaction inputs (empty scriptSigs, appropriate sequences).
        outputs: Transaction outputs.
        locktime: Transaction nLockTime.
        psbt_inputs: Per-input metadata for the PSBT.

    Returns:
        Serialized PSBT bytes.

    Raises:
        ValueError: If inputs and psbt_inputs lengths don't match.
    """
    if len(inputs) != len(psbt_inputs):
        raise ValueError(
            f"inputs ({len(inputs)}) and psbt_inputs ({len(psbt_inputs)}) must have the same length"
        )

    # 1. Serialize the unsigned transaction (no witness)
    unsigned_tx = serialize_transaction(
        version=version,
        inputs=inputs,
        outputs=outputs,
        locktime=locktime,
        witnesses=None,
    )

    # 2. Build global map
    result = bytearray(PSBT_MAGIC)
    result.extend(_serialize_psbt_pair(PSBT_GLOBAL_UNSIGNED_TX, unsigned_tx))
    result.extend(PSBT_SEPARATOR)

    # 3. Build per-input maps
    for pi in psbt_inputs:
        # PSBT_IN_WITNESS_UTXO: serialized as <value 8-byte LE> + <varint scriptlen> + <script>
        witness_utxo = (
            struct.pack("<Q", pi.witness_utxo_value)
            + encode_varint(len(pi.witness_utxo_script))
            + pi.witness_utxo_script
        )
        result.extend(_serialize_psbt_pair(PSBT_IN_WITNESS_UTXO, witness_utxo))

        # PSBT_IN_SIGHASH_TYPE: 4-byte LE uint32
        sighash_bytes = struct.pack("<I", pi.sighash_type)
        result.extend(_serialize_psbt_pair(PSBT_IN_SIGHASH_TYPE, sighash_bytes))

        # PSBT_IN_WITNESS_SCRIPT: the full witness script
        result.extend(_serialize_psbt_pair(PSBT_IN_WITNESS_SCRIPT, pi.witness_script))

        # PSBT_IN_BIP32_DERIVATION: key origin info for signing devices
        # BIP-174: key = <0x06> <pubkey>, value = <4-byte fingerprint> <4-byte LE index>...
        if pi.bip32_derivations:
            for deriv in pi.bip32_derivations:
                value = deriv.fingerprint + b"".join(struct.pack("<I", idx) for idx in deriv.path)
                result.extend(_serialize_psbt_pair(PSBT_IN_BIP32_DERIVATION, value, deriv.pubkey))

        result.extend(PSBT_SEPARATOR)

    # 4. Build per-output maps (empty for each output)
    for _ in outputs:
        result.extend(PSBT_SEPARATOR)

    return bytes(result)


def psbt_to_base64(psbt_bytes: bytes) -> str:
    """Encode PSBT bytes as base64 string (standard PSBT exchange format).

    Args:
        psbt_bytes: Raw PSBT bytes.

    Returns:
        Base64-encoded PSBT string.
    """
    import base64 as b64

    return b64.b64encode(psbt_bytes).decode("ascii")
