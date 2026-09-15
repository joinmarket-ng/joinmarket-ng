"""Tests for BIP370 (PSBT v2) parsing, including the official BIP test vectors.

The vectors in ``data/bip370_vectors.json`` are copied verbatim from the "Test Vectors"
section of the BIP370 source (provenance recorded in the fixture); no test touches the
network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jmcore.bitcoin import encode_varint
from jmcore.constants import MAX_MONEY

from jmwallet.wallet.psbt import (
    LOCKTIME_THRESHOLD,
    PSBT_GLOBAL_FALLBACK_LOCKTIME,
    PSBT_GLOBAL_INPUT_COUNT,
    PSBT_GLOBAL_OUTPUT_COUNT,
    PSBT_GLOBAL_TX_MODIFIABLE,
    PSBT_GLOBAL_TX_VERSION,
    PSBT_GLOBAL_UNSIGNED_TX,
    PSBT_GLOBAL_VERSION,
    PSBT_IN_OUTPUT_INDEX,
    PSBT_IN_PREVIOUS_TXID,
    PSBT_IN_REQUIRED_HEIGHT_LOCKTIME,
    PSBT_IN_REQUIRED_TIME_LOCKTIME,
    PSBT_IN_SEQUENCE,
    PSBT_MAGIC,
    PSBT_OUT_AMOUNT,
    PSBT_OUT_SCRIPT,
    PSBT_VERSION_0,
    PSBT_VERSION_2,
    SEQUENCE_FINAL,
    TX_MODIFIABLE_HAS_SIGHASH_SINGLE,
    TX_MODIFIABLE_INPUTS,
    TX_MODIFIABLE_OUTPUTS,
    PSBTError,
    parse_psbt,
)

_VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).parent / "data" / "bip370_vectors.json").read_text()
)

TXID = bytes.fromhex("0b0ad921419c1c8719735d72dc739f9ea9e0638d1fe4c1eef0f9944084815fc8")
SCRIPT = bytes.fromhex("0014c430f64c4756da310dbd1a085572ef299926272c")


def _vectors(group: str) -> list[dict[str, Any]]:
    return list(_VECTORS[group])


def _ids(group: str) -> list[str]:
    return [entry["case"] for entry in _VECTORS[group]]


def _pair(key: bytes, value: bytes) -> bytes:
    return encode_varint(len(key)) + key + encode_varint(len(value)) + value


def _map(*records: tuple[bytes, bytes]) -> bytes:
    return b"".join(_pair(key, value) for key, value in records) + b"\x00"


def _global_records(
    *,
    tx_version: bytes | None = (2).to_bytes(4, "little"),
    input_count: bytes | None = b"\x01",
    output_count: bytes | None = b"\x01",
    extra: list[tuple[bytes, bytes]] | None = None,
) -> list[tuple[bytes, bytes]]:
    records: list[tuple[bytes, bytes]] = [(bytes([PSBT_GLOBAL_VERSION]), (2).to_bytes(4, "little"))]
    if tx_version is not None:
        records.append((bytes([PSBT_GLOBAL_TX_VERSION]), tx_version))
    if input_count is not None:
        records.append((bytes([PSBT_GLOBAL_INPUT_COUNT]), input_count))
    if output_count is not None:
        records.append((bytes([PSBT_GLOBAL_OUTPUT_COUNT]), output_count))
    records.extend(extra or [])
    return records


def _input_records(*extra: tuple[bytes, bytes]) -> list[tuple[bytes, bytes]]:
    return [
        (bytes([PSBT_IN_PREVIOUS_TXID]), TXID),
        (bytes([PSBT_IN_OUTPUT_INDEX]), (1).to_bytes(4, "little")),
        *extra,
    ]


def _output_records(*, amount: bytes = (50_000).to_bytes(8, "little")) -> list[tuple[bytes, bytes]]:
    return [(bytes([PSBT_OUT_AMOUNT]), amount), (bytes([PSBT_OUT_SCRIPT]), SCRIPT)]


def _psbt_v2(
    global_records: list[tuple[bytes, bytes]],
    input_maps: list[list[tuple[bytes, bytes]]],
    output_maps: list[list[tuple[bytes, bytes]]],
) -> bytes:
    return (
        PSBT_MAGIC
        + _map(*global_records)
        + b"".join(_map(*records) for records in input_maps)
        + b"".join(_map(*records) for records in output_maps)
    )


def _minimal_v2() -> bytes:
    return _psbt_v2(_global_records(), [_input_records()], [_output_records()])


# --- Official BIP370 vectors -------------------------------------------------


@pytest.mark.parametrize("vector", _vectors("invalid"), ids=_ids("invalid"))
def test_rejects_official_invalid_vectors(vector: dict[str, Any]) -> None:
    with pytest.raises(PSBTError):
        parse_psbt(bytes.fromhex(vector["hex"]))


@pytest.mark.parametrize("vector", _vectors("valid"), ids=_ids("valid"))
def test_parses_official_valid_vectors_and_round_trips(vector: dict[str, Any]) -> None:
    raw = bytes.fromhex(vector["hex"])

    parsed = parse_psbt(raw)

    assert parsed.version == PSBT_VERSION_2
    assert parsed.serialize() == raw
    assert parsed.transaction.version == 2
    assert len(parsed.transaction.inputs) == len(parsed.input_maps)
    assert len(parsed.transaction.outputs) == len(parsed.output_maps)


@pytest.mark.parametrize("vector", _vectors("locktime"), ids=_ids("locktime"))
def test_official_locktime_determination_vectors(vector: dict[str, Any]) -> None:
    raw = bytes.fromhex(vector["hex"])

    if vector["locktime"] is None:
        with pytest.raises(PSBTError, match="incompatible locktime"):
            parse_psbt(raw)
    else:
        assert parse_psbt(raw).transaction.locktime == vector["locktime"]


# --- Reconstructed transaction ----------------------------------------------


def test_reconstructs_transaction_from_v2_fields() -> None:
    raw = _psbt_v2(
        _global_records(
            output_count=b"\x02",
            extra=[(bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME]), (7).to_bytes(4, "little"))],
        ),
        [_input_records((bytes([PSBT_IN_SEQUENCE]), (0xFFFFFFFD).to_bytes(4, "little")))],
        [_output_records(), _output_records(amount=(1).to_bytes(8, "little"))],
    )

    parsed = parse_psbt(raw)

    assert parsed.transaction.version == 2
    assert parsed.transaction.locktime == 7
    assert parsed.transaction.inputs[0].txid_le == TXID
    assert parsed.transaction.inputs[0].vout == 1
    assert parsed.transaction.inputs[0].sequence == 0xFFFFFFFD
    assert parsed.transaction.inputs[0].scriptsig == b""
    assert [out.value for out in parsed.transaction.outputs] == [50_000, 1]
    assert parsed.transaction.outputs[0].script == SCRIPT
    assert parsed.transaction.has_witness is False
    assert parsed.unsigned_tx.startswith((2).to_bytes(4, "little"))
    assert parsed.serialize() == raw


def test_defaults_missing_sequence_to_final() -> None:
    parsed = parse_psbt(_minimal_v2())

    assert parsed.transaction.inputs[0].sequence == SEQUENCE_FINAL


def test_parses_creator_psbt_with_no_inputs_or_outputs() -> None:
    raw = _psbt_v2(_global_records(input_count=b"\x00", output_count=b"\x00"), [], [])

    parsed = parse_psbt(raw)

    assert parsed.transaction.inputs == []
    assert parsed.transaction.outputs == []
    assert parsed.transaction.locktime == 0
    assert parsed.serialize() == raw


def test_preserves_unknown_and_proprietary_records_in_every_map() -> None:
    raw = _psbt_v2(
        _global_records(extra=[(b"\xfcglobal", b"global unknown"), (b"\x77", b"unknown type")]),
        [_input_records((b"\xfcinput", b"input unknown"))],
        [[*_output_records(), (b"\xfcout", b"output unknown")]],
    )

    parsed = parse_psbt(raw)

    assert parsed.serialize() == raw
    assert (b"\xfcglobal", b"global unknown") in [
        (record.key, record.value) for record in parsed.global_map.records
    ]
    assert parsed.input_maps[0].records[-1].key == b"\xfcinput"
    assert parsed.output_maps[0].records[-1].value == b"output unknown"


def test_tx_modifiable_flag_constants_match_bip370_bits() -> None:
    assert (TX_MODIFIABLE_INPUTS, TX_MODIFIABLE_OUTPUTS, TX_MODIFIABLE_HAS_SIGHASH_SINGLE) == (
        0x01,
        0x02,
        0x04,
    )

    flags = TX_MODIFIABLE_INPUTS | TX_MODIFIABLE_OUTPUTS | TX_MODIFIABLE_HAS_SIGHASH_SINGLE
    raw = _psbt_v2(
        _global_records(extra=[(bytes([PSBT_GLOBAL_TX_MODIFIABLE]), bytes([flags]))]),
        [_input_records()],
        [_output_records()],
    )

    parsed = parse_psbt(raw)

    assert parsed.global_map.records[-1].value == bytes([flags])


# --- Strict v2 validation ----------------------------------------------------


@pytest.mark.parametrize(
    ("global_records", "message"),
    [
        (_global_records(tx_version=None), "missing the transaction version"),
        (_global_records(input_count=None), "missing the input count"),
        (_global_records(output_count=None), "missing the output count"),
        (_global_records(tx_version=b"\x02\x00"), "transaction version must be a 4-byte"),
        (_global_records(input_count=b"\xfd\x01\x00"), "Noncanonical CompactSize"),
        (_global_records(input_count=b"\x01\x00"), "Trailing data after global input count"),
        (
            _global_records(extra=[(bytes([PSBT_GLOBAL_TX_MODIFIABLE]), b"\x00\x00")]),
            "modifiable flags must be a single byte",
        ),
        (
            _global_records(extra=[(bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME]), b"\x00")]),
            "fallback locktime must be a 4-byte",
        ),
    ],
)
def test_rejects_malformed_v2_global_map(
    global_records: list[tuple[bytes, bytes]], message: str
) -> None:
    raw = _psbt_v2(global_records, [_input_records()], [_output_records()])

    with pytest.raises(PSBTError, match=message):
        parse_psbt(raw)


def test_rejects_unsigned_transaction_in_v2() -> None:
    raw = _psbt_v2(
        _global_records(extra=[(bytes([PSBT_GLOBAL_UNSIGNED_TX]), b"\x02\x00\x00\x00")]),
        [_input_records()],
        [_output_records()],
    )

    with pytest.raises(PSBTError, match="must not contain an unsigned transaction"):
        parse_psbt(raw)


@pytest.mark.parametrize(
    ("input_records", "message"),
    [
        ([(bytes([PSBT_IN_OUTPUT_INDEX]), (0).to_bytes(4, "little"))], "missing the previous txid"),
        ([(bytes([PSBT_IN_PREVIOUS_TXID]), TXID)], "missing the spent output index"),
        ([(bytes([PSBT_IN_PREVIOUS_TXID]), TXID[:31])], "previous txid must be 32 bytes"),
        (
            _input_records((bytes([PSBT_IN_SEQUENCE]), b"\xff\xff\xff")),
            "sequence must be a 4-byte",
        ),
        (
            _input_records(
                (
                    bytes([PSBT_IN_REQUIRED_TIME_LOCKTIME]),
                    (LOCKTIME_THRESHOLD - 1).to_bytes(4, "little"),
                )
            ),
            "time-based locktime must be at least",
        ),
        (
            _input_records(
                (
                    bytes([PSBT_IN_REQUIRED_HEIGHT_LOCKTIME]),
                    LOCKTIME_THRESHOLD.to_bytes(4, "little"),
                )
            ),
            "height-based locktime must be between",
        ),
        (
            _input_records((bytes([PSBT_IN_REQUIRED_HEIGHT_LOCKTIME]), (0).to_bytes(4, "little"))),
            "height-based locktime must be between",
        ),
        (
            _input_records((bytes([PSBT_IN_PREVIOUS_TXID]) + b"x", b"")),
            "singleton key type",
        ),
    ],
)
def test_rejects_malformed_v2_input_map(
    input_records: list[tuple[bytes, bytes]], message: str
) -> None:
    raw = _psbt_v2(_global_records(), [input_records], [_output_records()])

    with pytest.raises(PSBTError, match=message):
        parse_psbt(raw)


@pytest.mark.parametrize(
    ("output_records", "message"),
    [
        ([(bytes([PSBT_OUT_SCRIPT]), SCRIPT)], "missing the output amount"),
        ([(bytes([PSBT_OUT_AMOUNT]), (1).to_bytes(8, "little"))], "missing the output script"),
        (_output_records(amount=(1).to_bytes(4, "little")), "amount must be an 8-byte"),
        (
            _output_records(amount=(MAX_MONEY + 1).to_bytes(8, "little")),
            "outside the Bitcoin money range",
        ),
        (_output_records(amount=(-1).to_bytes(8, "little", signed=True)), "money range"),
    ],
)
def test_rejects_malformed_v2_output_map(
    output_records: list[tuple[bytes, bytes]], message: str
) -> None:
    raw = _psbt_v2(_global_records(), [_input_records()], [output_records])

    with pytest.raises(PSBTError, match=message):
        parse_psbt(raw)


def test_rejects_map_counts_that_disagree_with_declared_counts() -> None:
    too_few = _psbt_v2(
        _global_records(input_count=b"\x02", output_count=b"\x00"), [_input_records()], []
    )
    too_many = _psbt_v2(
        _global_records(), [_input_records()], [_output_records(), _output_records()]
    )

    with pytest.raises(PSBTError, match="missing input map 1"):
        parse_psbt(too_few)
    with pytest.raises(PSBTError, match="Trailing data after PSBT maps"):
        parse_psbt(too_many)


@pytest.mark.parametrize("version", [1, 3, 0xFFFFFFFF])
def test_rejects_unsupported_psbt_versions(version: int) -> None:
    raw = _psbt_v2(
        [(bytes([PSBT_GLOBAL_VERSION]), version.to_bytes(4, "little"))],
        [_input_records()],
        [_output_records()],
    )

    with pytest.raises(PSBTError, match="Only PSBT version 0 or 2 is supported"):
        parse_psbt(raw)


# --- v0 compatibility --------------------------------------------------------


@pytest.mark.parametrize(
    ("map_kind", "key"),
    [
        ("global", bytes([PSBT_GLOBAL_TX_VERSION])),
        ("global", bytes([PSBT_GLOBAL_INPUT_COUNT])),
        ("input", bytes([PSBT_IN_SEQUENCE])),
        ("output", bytes([PSBT_OUT_AMOUNT])),
    ],
)
def test_rejects_v2_only_records_inside_v0(map_kind: str, key: bytes) -> None:
    unsigned_tx = bytes.fromhex(
        "0200000001"
        + TXID.hex()
        + "00000000"
        + "00"
        + "ffffffff"
        + "01"
        + (50_000).to_bytes(8, "little").hex()
        + "16"
        + SCRIPT.hex()
        + "00000000"
    )
    value = (2).to_bytes(4, "little") if map_kind != "output" else (1).to_bytes(8, "little")
    global_records = [(bytes([PSBT_GLOBAL_UNSIGNED_TX]), unsigned_tx)]
    input_records: list[tuple[bytes, bytes]] = []
    output_records: list[tuple[bytes, bytes]] = []
    if map_kind == "global":
        global_records.append((key, b"\x01" if key == bytes([PSBT_GLOBAL_INPUT_COUNT]) else value))
    elif map_kind == "input":
        input_records.append((key, value))
    else:
        output_records.append((key, value))
    raw = PSBT_MAGIC + _map(*global_records) + _map(*input_records) + _map(*output_records)

    with pytest.raises(PSBTError, match="BIP370 field that is not allowed in PSBT v0"):
        parse_psbt(raw)


def test_v0_psbt_reports_version_zero() -> None:
    unsigned_tx = bytes.fromhex(
        "0200000001" + TXID.hex() + "0000000000ffffffff0100000000000000000000000000"
    )
    raw = PSBT_MAGIC + _map((bytes([PSBT_GLOBAL_UNSIGNED_TX]), unsigned_tx)) + _map() + _map()

    parsed = parse_psbt(raw)

    assert parsed.version == PSBT_VERSION_0
    assert parsed.unsigned_tx == unsigned_tx
    assert parsed.serialize() == raw
