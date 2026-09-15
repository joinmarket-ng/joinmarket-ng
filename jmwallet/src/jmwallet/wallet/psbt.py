"""Strict BIP174 PSBT v0 and BIP370 PSBT v2 parsing and record-preserving updates."""

from __future__ import annotations

from dataclasses import dataclass, field

from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    encode_varint,
    parse_transaction_bytes,
    serialize_transaction,
)
from jmcore.constants import MAX_MONEY

PSBT_MAGIC = b"psbt\xff"

# Supported PSBT versions.
PSBT_VERSION_0 = 0
PSBT_VERSION_2 = 2

# Global key types.
PSBT_GLOBAL_UNSIGNED_TX = 0x00
PSBT_GLOBAL_XPUB = 0x01
PSBT_GLOBAL_TX_VERSION = 0x02
PSBT_GLOBAL_FALLBACK_LOCKTIME = 0x03
PSBT_GLOBAL_INPUT_COUNT = 0x04
PSBT_GLOBAL_OUTPUT_COUNT = 0x05
PSBT_GLOBAL_TX_MODIFIABLE = 0x06
PSBT_GLOBAL_VERSION = 0xFB

# Input key types.
PSBT_IN_NON_WITNESS_UTXO = 0x00
PSBT_IN_WITNESS_UTXO = 0x01
PSBT_IN_PARTIAL_SIG = 0x02
PSBT_IN_SIGHASH_TYPE = 0x03
PSBT_IN_REDEEM_SCRIPT = 0x04
PSBT_IN_WITNESS_SCRIPT = 0x05
PSBT_IN_BIP32_DERIVATION = 0x06
PSBT_IN_FINAL_SCRIPTSIG = 0x07
PSBT_IN_FINAL_SCRIPTWITNESS = 0x08
PSBT_IN_PREVIOUS_TXID = 0x0E
PSBT_IN_OUTPUT_INDEX = 0x0F
PSBT_IN_SEQUENCE = 0x10
PSBT_IN_REQUIRED_TIME_LOCKTIME = 0x11
PSBT_IN_REQUIRED_HEIGHT_LOCKTIME = 0x12
PSBT_IN_PROPRIETARY = 0xFC

# Output key types.
PSBT_OUT_REDEEM_SCRIPT = 0x00
PSBT_OUT_WITNESS_SCRIPT = 0x01
PSBT_OUT_BIP32_DERIVATION = 0x02
PSBT_OUT_AMOUNT = 0x03
PSBT_OUT_SCRIPT = 0x04
PSBT_OUT_PROPRIETARY = 0xFC

# PSBT_GLOBAL_TX_MODIFIABLE bit flags (BIP370).
TX_MODIFIABLE_INPUTS = 0x01
TX_MODIFIABLE_OUTPUTS = 0x02
TX_MODIFIABLE_HAS_SIGHASH_SINGLE = 0x04

# Locktime values at or above this threshold are Unix timestamps, below are block heights.
LOCKTIME_THRESHOLD = 500_000_000
SEQUENCE_FINAL = 0xFFFFFFFF

# Key types defined by BIP370 only, rejected in PSBT v0 maps.
_V2_ONLY_KEY_TYPES: dict[str, frozenset[int]] = {
    "global": frozenset(
        {
            PSBT_GLOBAL_TX_VERSION,
            PSBT_GLOBAL_FALLBACK_LOCKTIME,
            PSBT_GLOBAL_INPUT_COUNT,
            PSBT_GLOBAL_OUTPUT_COUNT,
            PSBT_GLOBAL_TX_MODIFIABLE,
        }
    ),
    "input": frozenset(
        {
            PSBT_IN_PREVIOUS_TXID,
            PSBT_IN_OUTPUT_INDEX,
            PSBT_IN_SEQUENCE,
            PSBT_IN_REQUIRED_TIME_LOCKTIME,
            PSBT_IN_REQUIRED_HEIGHT_LOCKTIME,
        }
    ),
    "output": frozenset({PSBT_OUT_AMOUNT, PSBT_OUT_SCRIPT}),
}


class PSBTError(ValueError):
    """Raised when a PSBT violates BIP174 serialization requirements."""


@dataclass(frozen=True)
class PSBTKeyValue:
    """An ordered raw PSBT key/value record."""

    key: bytes
    value: bytes


@dataclass
class PSBTMap:
    """A PSBT map retaining record order and unknown records."""

    records: list[PSBTKeyValue] = field(default_factory=list)

    def append(self, key: bytes, value: bytes) -> None:
        """Append a unique, nonempty raw record to this map."""
        if not key:
            raise PSBTError("PSBT map keys must not be empty")
        if any(record.key == key for record in self.records):
            raise PSBTError(f"Duplicate PSBT key: {key.hex()}")
        self.records.append(PSBTKeyValue(key=key, value=value))

    def serialize(self) -> bytes:
        """Serialize this map with canonical CompactSize lengths."""
        result = bytearray()
        for record in self.records:
            result.extend(encode_varint(len(record.key)))
            result.extend(record.key)
            result.extend(encode_varint(len(record.value)))
            result.extend(record.value)
        result.append(0)
        return bytes(result)


@dataclass(frozen=True)
class WitnessUTXO:
    """The amount and scriptPubKey contained in a PSBT witness UTXO record."""

    value: int
    script_pubkey: bytes


@dataclass(frozen=True)
class BIP32KeyOrigin:
    """A BIP32 public key and its master fingerprint and derivation path."""

    pubkey: bytes
    fingerprint: bytes
    path: tuple[int, ...]


@dataclass
class ParsedPSBT:
    """A parsed BIP174 PSBT v0 or BIP370 PSBT v2, retaining all raw map records.

    ``transaction`` is the unsigned transaction described by the PSBT: parsed from
    ``PSBT_GLOBAL_UNSIGNED_TX`` for v0 and reconstructed from the BIP370 fields for
    v2, with the locktime derived as BIP370 specifies. ``unsigned_tx`` is its
    serialization; for v2 it is a reconstruction and is not part of the PSBT bytes.
    """

    unsigned_tx: bytes
    transaction: ParsedTransaction
    global_map: PSBTMap
    input_maps: list[PSBTMap]
    output_maps: list[PSBTMap]
    version: int = PSBT_VERSION_0

    def serialize(self) -> bytes:
        """Serialize the PSBT while preserving all map record order."""
        result = bytearray(PSBT_MAGIC)
        result.extend(self.global_map.serialize())
        for input_map in self.input_maps:
            result.extend(input_map.serialize())
        for output_map in self.output_maps:
            result.extend(output_map.serialize())
        return bytes(result)

    def append_input_key_value(self, input_index: int, key: bytes, value: bytes) -> None:
        """Append a valid unique record to an input map."""
        if input_index < 0:
            raise PSBTError(f"PSBT input index out of range: {input_index}")
        try:
            input_map = self.input_maps[input_index]
        except IndexError as error:
            raise PSBTError(f"PSBT input index out of range: {input_index}") from error
        _validate_key("input", key)
        _validate_map_records(
            "input", PSBTMap(records=[PSBTKeyValue(key=key, value=value)]), self.version
        )
        input_map.append(key, value)


def parse_psbt(data: bytes) -> ParsedPSBT:
    """Parse a strict, complete BIP174 PSBT v0 or BIP370 PSBT v2 binary payload."""
    if not data.startswith(PSBT_MAGIC):
        raise PSBTError("Invalid PSBT magic bytes")

    offset = len(PSBT_MAGIC)
    global_map, offset = _read_map(data, offset, "global")
    version = _read_psbt_version(global_map)
    _validate_map_records("global", global_map, version)

    if version == PSBT_VERSION_2:
        return _parse_psbt_v2(data, offset, global_map)
    return _parse_psbt_v0(data, offset, global_map)


def _parse_psbt_v0(data: bytes, offset: int, global_map: PSBTMap) -> ParsedPSBT:
    unsigned_tx = _get_unsigned_transaction(global_map)

    try:
        transaction = parse_transaction_bytes(unsigned_tx)
    except Exception as error:
        raise PSBTError(f"Invalid PSBT unsigned transaction: {error}") from error
    if transaction.has_witness:
        raise PSBTError("PSBT unsigned transaction must not contain witness data")
    if any(tx_input.scriptsig for tx_input in transaction.inputs):
        raise PSBTError("PSBT unsigned transaction must have empty scriptSigs")

    input_maps, output_maps = _read_remaining_maps(
        data, offset, len(transaction.inputs), len(transaction.outputs), PSBT_VERSION_0
    )
    return ParsedPSBT(
        unsigned_tx=unsigned_tx,
        transaction=transaction,
        global_map=global_map,
        input_maps=input_maps,
        output_maps=output_maps,
        version=PSBT_VERSION_0,
    )


def _parse_psbt_v2(data: bytes, offset: int, global_map: PSBTMap) -> ParsedPSBT:
    if _find_value(global_map, PSBT_GLOBAL_UNSIGNED_TX) is not None:
        raise PSBTError("PSBT v2 must not contain an unsigned transaction")
    tx_version = _require_global(global_map, PSBT_GLOBAL_TX_VERSION, "transaction version")
    input_count = _read_exact_compact_size(
        _require_global(global_map, PSBT_GLOBAL_INPUT_COUNT, "input count"), "global input count"
    )
    output_count = _read_exact_compact_size(
        _require_global(global_map, PSBT_GLOBAL_OUTPUT_COUNT, "output count"), "global output count"
    )

    input_maps, output_maps = _read_remaining_maps(
        data, offset, input_count, output_count, PSBT_VERSION_2
    )
    transaction = ParsedTransaction(
        version=int.from_bytes(tx_version, "little", signed=False),
        inputs=[_v2_input(input_map, index) for index, input_map in enumerate(input_maps)],
        outputs=[_v2_output(output_map, index) for index, output_map in enumerate(output_maps)],
        witnesses=[],
        locktime=_derive_locktime(global_map, input_maps),
        has_witness=False,
    )
    unsigned_tx = serialize_transaction(
        transaction.version, transaction.inputs, transaction.outputs, transaction.locktime
    )
    return ParsedPSBT(
        unsigned_tx=unsigned_tx,
        transaction=transaction,
        global_map=global_map,
        input_maps=input_maps,
        output_maps=output_maps,
        version=PSBT_VERSION_2,
    )


def _read_remaining_maps(
    data: bytes, offset: int, input_count: int, output_count: int, version: int
) -> tuple[list[PSBTMap], list[PSBTMap]]:
    input_maps, offset = _read_expected_maps(data, offset, input_count, "input")
    output_maps, offset = _read_expected_maps(data, offset, output_count, "output")
    for input_map in input_maps:
        _validate_map_records("input", input_map, version)
    for output_map in output_maps:
        _validate_map_records("output", output_map, version)
    if offset != len(data):
        raise PSBTError("Trailing data after PSBT maps")
    return input_maps, output_maps


def _v2_input(input_map: PSBTMap, index: int) -> TxInput:
    txid = _require_input(input_map, index, PSBT_IN_PREVIOUS_TXID, "previous txid")
    vout = _require_input(input_map, index, PSBT_IN_OUTPUT_INDEX, "spent output index")
    sequence = _find_value(input_map, PSBT_IN_SEQUENCE)
    return TxInput(
        txid_le=txid,
        vout=int.from_bytes(vout, "little", signed=False),
        sequence=(
            SEQUENCE_FINAL if sequence is None else int.from_bytes(sequence, "little", signed=False)
        ),
    )


def _v2_output(output_map: PSBTMap, index: int) -> TxOutput:
    amount = _find_value(output_map, PSBT_OUT_AMOUNT)
    if amount is None:
        raise PSBTError(f"PSBT v2 output {index} is missing the output amount")
    script = _find_value(output_map, PSBT_OUT_SCRIPT)
    if script is None:
        raise PSBTError(f"PSBT v2 output {index} is missing the output script")
    return TxOutput(value=int.from_bytes(amount, "little", signed=True), script=script)


def _derive_locktime(global_map: PSBTMap, input_maps: list[PSBTMap]) -> int:
    """Derive the BIP370 transaction locktime from required and fallback locktimes."""
    heights: list[int] = []
    times: list[int] = []
    heights_allowed = True
    times_allowed = True
    for input_map in input_maps:
        height = _find_value(input_map, PSBT_IN_REQUIRED_HEIGHT_LOCKTIME)
        time = _find_value(input_map, PSBT_IN_REQUIRED_TIME_LOCKTIME)
        if height is None and time is None:
            continue
        if height is None:
            heights_allowed = False
        else:
            heights.append(int.from_bytes(height, "little", signed=False))
        if time is None:
            times_allowed = False
        else:
            times.append(int.from_bytes(time, "little", signed=False))

    if not heights and not times:
        fallback = _find_value(global_map, PSBT_GLOBAL_FALLBACK_LOCKTIME)
        return 0 if fallback is None else int.from_bytes(fallback, "little", signed=False)
    if heights_allowed:
        return max(heights)
    if times_allowed:
        return max(times)
    raise PSBTError("PSBT v2 inputs require incompatible locktime types")


def _find_value(psbt_map: PSBTMap, key_type: int) -> bytes | None:
    """Return the value of a singleton record, or None when it is absent."""
    for record in psbt_map.records:
        if record.key == bytes([key_type]):
            return record.value
    return None


def _require_global(global_map: PSBTMap, key_type: int, context: str) -> bytes:
    value = _find_value(global_map, key_type)
    if value is None:
        raise PSBTError(f"PSBT v2 global map is missing the {context}")
    return value


def _require_input(input_map: PSBTMap, index: int, key_type: int, context: str) -> bytes:
    value = _find_value(input_map, key_type)
    if value is None:
        raise PSBTError(f"PSBT v2 input {index} is missing the {context}")
    return value


def _read_exact_compact_size(value: bytes, context: str) -> int:
    count, offset = _read_compact_size(value, 0, context)
    if offset != len(value):
        raise PSBTError(f"Trailing data after {context}")
    return count


def parse_witness_utxo(value: bytes) -> WitnessUTXO:
    """Parse a BIP174 witness UTXO value with exact consumption."""
    if len(value) < 8:
        raise PSBTError("Truncated witness UTXO value")
    amount = int.from_bytes(value[:8], "little", signed=False)
    if amount > MAX_MONEY:
        raise PSBTError("Witness UTXO amount exceeds Bitcoin MAX_MONEY")
    script_length, offset = _read_compact_size(value, 8, "witness UTXO script length")
    remaining = len(value) - offset
    if script_length > remaining:
        raise PSBTError("Truncated witness UTXO scriptPubKey")
    if script_length != remaining:
        raise PSBTError("Trailing data after witness UTXO scriptPubKey")
    return WitnessUTXO(value=amount, script_pubkey=value[offset:])


def parse_bip32_derivation(key: bytes, value: bytes) -> BIP32KeyOrigin:
    """Parse a BIP174 BIP32 derivation key/value pair."""
    if len(key) != 34:
        raise PSBTError("BIP32 derivation key must contain a type byte and 33-byte public key")
    pubkey = key[1:]
    if pubkey[0] not in (0x02, 0x03):
        raise PSBTError("BIP32 derivation key must contain a compressed public key")
    _validate_key_origin_value(value, "BIP32 derivation")
    path = tuple(
        int.from_bytes(value[offset : offset + 4], "little", signed=False)
        for offset in range(4, len(value), 4)
    )
    return BIP32KeyOrigin(pubkey=pubkey, fingerprint=value[:4], path=path)


def _read_expected_maps(
    data: bytes, offset: int, count: int, map_kind: str
) -> tuple[list[PSBTMap], int]:
    maps: list[PSBTMap] = []
    for index in range(count):
        if offset == len(data):
            raise PSBTError(f"Truncated PSBT: missing {map_kind} map {index}")
        parsed_map, offset = _read_map(data, offset, map_kind)
        maps.append(parsed_map)
    return maps, offset


def _read_map(data: bytes, offset: int, map_kind: str) -> tuple[PSBTMap, int]:
    parsed_map = PSBTMap()
    while True:
        key_length, offset = _read_compact_size(data, offset, f"{map_kind} map key length")
        if key_length == 0:
            return parsed_map, offset
        key, offset = _read_bytes(data, offset, key_length, f"{map_kind} map key")
        _validate_key(map_kind, key)
        value_length, offset = _read_compact_size(data, offset, f"{map_kind} map value length")
        value, offset = _read_bytes(data, offset, value_length, f"{map_kind} map value")
        parsed_map.append(key, value)


def _read_compact_size(data: bytes, offset: int, context: str) -> tuple[int, int]:
    if offset >= len(data):
        raise PSBTError(f"Truncated {context}")
    first = data[offset]
    offset += 1
    if first < 0xFD:
        return first, offset
    length = {0xFD: 2, 0xFE: 4, 0xFF: 8}[first]
    encoded, offset = _read_bytes(data, offset, length, context)
    value = int.from_bytes(encoded, "little", signed=False)
    minimum = {0xFD: 0xFD, 0xFE: 0x10000, 0xFF: 0x100000000}[first]
    if value < minimum:
        raise PSBTError(f"Noncanonical CompactSize for {context}")
    return value, offset


def _read_bytes(data: bytes, offset: int, length: int, context: str) -> tuple[bytes, int]:
    if length > len(data) - offset:
        raise PSBTError(f"Truncated {context}")
    return data[offset : offset + length], offset + length


def _get_unsigned_transaction(global_map: PSBTMap) -> bytes:
    matching = [record for record in global_map.records if record.key == b"\x00"]
    if not matching:
        raise PSBTError("PSBT global map is missing the unsigned transaction")
    if len(matching) != 1:
        raise PSBTError("PSBT global map has multiple unsigned transactions")
    return matching[0].value


def _read_psbt_version(global_map: PSBTMap) -> int:
    """Return the PSBT version, defaulting to 0 when the record is absent."""
    versions = [
        record for record in global_map.records if record.key == bytes([PSBT_GLOBAL_VERSION])
    ]
    if not versions:
        return PSBT_VERSION_0
    if len(versions) != 1:
        raise PSBTError("PSBT global map has multiple version records")
    encoded = versions[0].value
    if len(encoded) != 4:
        raise PSBTError("PSBT version must be a 4-byte uint32")
    version = int.from_bytes(encoded, "little", signed=False)
    if version not in (PSBT_VERSION_0, PSBT_VERSION_2):
        raise PSBTError("Only PSBT version 0 or 2 is supported")
    return version


def _validate_key(map_kind: str, key: bytes) -> None:
    if not key:
        raise PSBTError("PSBT map keys must not be empty")
    key_type = key[0]
    singleton_types = {
        "global": {
            PSBT_GLOBAL_UNSIGNED_TX,
            PSBT_GLOBAL_TX_VERSION,
            PSBT_GLOBAL_FALLBACK_LOCKTIME,
            PSBT_GLOBAL_INPUT_COUNT,
            PSBT_GLOBAL_OUTPUT_COUNT,
            PSBT_GLOBAL_TX_MODIFIABLE,
            PSBT_GLOBAL_VERSION,
        },
        "input": {
            PSBT_IN_NON_WITNESS_UTXO,
            PSBT_IN_WITNESS_UTXO,
            PSBT_IN_SIGHASH_TYPE,
            PSBT_IN_REDEEM_SCRIPT,
            PSBT_IN_WITNESS_SCRIPT,
            PSBT_IN_FINAL_SCRIPTSIG,
            PSBT_IN_FINAL_SCRIPTWITNESS,
            0x09,
            PSBT_IN_PREVIOUS_TXID,
            PSBT_IN_OUTPUT_INDEX,
            PSBT_IN_SEQUENCE,
            PSBT_IN_REQUIRED_TIME_LOCKTIME,
            PSBT_IN_REQUIRED_HEIGHT_LOCKTIME,
        },
        "output": {
            PSBT_OUT_REDEEM_SCRIPT,
            PSBT_OUT_WITNESS_SCRIPT,
            PSBT_OUT_AMOUNT,
            PSBT_OUT_SCRIPT,
        },
    }
    if key_type in singleton_types[map_kind] and len(key) != 1:
        raise PSBTError(f"PSBT {map_kind} singleton key type {key_type:#x} must not carry key data")

    if map_kind == "global" and key_type == PSBT_GLOBAL_XPUB and len(key) != 79:
        raise PSBTError("PSBT global xpub key must contain a 78-byte extended public key")
    keyed_pubkey_types = {
        "global": set(),
        "input": {PSBT_IN_PARTIAL_SIG, PSBT_IN_BIP32_DERIVATION},
        "output": {PSBT_OUT_BIP32_DERIVATION},
    }
    if key_type in keyed_pubkey_types[map_kind]:
        if len(key) != 34 or key[1] not in (0x02, 0x03):
            raise PSBTError(
                f"PSBT {map_kind} key type {key_type:#x} must contain a compressed public key"
            )


def _validate_map_records(map_kind: str, psbt_map: PSBTMap, version: int) -> None:
    for record in psbt_map.records:
        key_type = record.key[0]
        if version == PSBT_VERSION_0 and key_type in _V2_ONLY_KEY_TYPES[map_kind]:
            raise PSBTError(
                f"PSBT {map_kind} key type {key_type:#x} is a BIP370 field "
                "that is not allowed in PSBT v0"
            )
        if map_kind == "global":
            _validate_global_value(key_type, record.value)
        elif map_kind == "input":
            _validate_input_value(record)
        elif map_kind == "output":
            _validate_output_value(record)


def _validate_global_value(key_type: int, value: bytes) -> None:
    if key_type == PSBT_GLOBAL_XPUB:
        _validate_key_origin_value(value, "global xpub")
    elif key_type == PSBT_GLOBAL_TX_VERSION and len(value) != 4:
        raise PSBTError("PSBT transaction version must be a 4-byte integer")
    elif key_type == PSBT_GLOBAL_FALLBACK_LOCKTIME and len(value) != 4:
        raise PSBTError("PSBT fallback locktime must be a 4-byte uint32")
    elif key_type == PSBT_GLOBAL_INPUT_COUNT:
        _read_exact_compact_size(value, "global input count")
    elif key_type == PSBT_GLOBAL_OUTPUT_COUNT:
        _read_exact_compact_size(value, "global output count")
    elif key_type == PSBT_GLOBAL_TX_MODIFIABLE and len(value) != 1:
        raise PSBTError("PSBT transaction modifiable flags must be a single byte")


def _validate_input_value(record: PSBTKeyValue) -> None:
    key_type = record.key[0]
    if key_type == PSBT_IN_WITNESS_UTXO:
        parse_witness_utxo(record.value)
    elif key_type == PSBT_IN_SIGHASH_TYPE and len(record.value) != 4:
        raise PSBTError("PSBT input sighash type must be a 4-byte uint32")
    elif key_type == PSBT_IN_PARTIAL_SIG and not record.value:
        raise PSBTError("PSBT input partial signature must not be empty")
    elif key_type == PSBT_IN_BIP32_DERIVATION:
        parse_bip32_derivation(record.key, record.value)
    elif key_type == PSBT_IN_PREVIOUS_TXID and len(record.value) != 32:
        raise PSBTError("PSBT input previous txid must be 32 bytes")
    elif key_type == PSBT_IN_OUTPUT_INDEX and len(record.value) != 4:
        raise PSBTError("PSBT input spent output index must be a 4-byte uint32")
    elif key_type == PSBT_IN_SEQUENCE and len(record.value) != 4:
        raise PSBTError("PSBT input sequence must be a 4-byte uint32")
    elif key_type == PSBT_IN_REQUIRED_TIME_LOCKTIME:
        _validate_required_locktime(record.value, height_based=False)
    elif key_type == PSBT_IN_REQUIRED_HEIGHT_LOCKTIME:
        _validate_required_locktime(record.value, height_based=True)


def _validate_output_value(record: PSBTKeyValue) -> None:
    key_type = record.key[0]
    if key_type == PSBT_OUT_BIP32_DERIVATION:
        parse_bip32_derivation(record.key, record.value)
    elif key_type == PSBT_OUT_AMOUNT:
        if len(record.value) != 8:
            raise PSBTError("PSBT output amount must be an 8-byte integer")
        amount = int.from_bytes(record.value, "little", signed=True)
        if amount < 0 or amount > MAX_MONEY:
            raise PSBTError("PSBT output amount is outside the Bitcoin money range")


def _validate_required_locktime(value: bytes, *, height_based: bool) -> None:
    kind = "height" if height_based else "time"
    if len(value) != 4:
        raise PSBTError(f"PSBT input required {kind}-based locktime must be a 4-byte uint32")
    locktime = int.from_bytes(value, "little", signed=False)
    if height_based and not 0 < locktime < LOCKTIME_THRESHOLD:
        raise PSBTError(
            "PSBT input required height-based locktime must be between 1 and "
            f"{LOCKTIME_THRESHOLD - 1}"
        )
    if not height_based and locktime < LOCKTIME_THRESHOLD:
        raise PSBTError(
            f"PSBT input required time-based locktime must be at least {LOCKTIME_THRESHOLD}"
        )


def _validate_key_origin_value(value: bytes, context: str) -> None:
    if len(value) < 4 or (len(value) - 4) % 4 != 0:
        raise PSBTError(f"PSBT {context} must contain a fingerprint and uint32 path")
