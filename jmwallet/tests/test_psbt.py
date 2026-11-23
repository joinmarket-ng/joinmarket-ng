"""Focused tests for strict BIP174 PSBT v0 parsing."""

from __future__ import annotations

import time

import pytest
from jmcore.bitcoin import TxInput, TxOutput, encode_varint, serialize_transaction
from jmcore.constants import MAX_MONEY

from jmwallet.wallet.psbt import (
    PSBT_GLOBAL_UNSIGNED_TX,
    PSBT_GLOBAL_VERSION,
    PSBT_GLOBAL_XPUB,
    PSBT_IN_BIP32_DERIVATION,
    PSBT_IN_PARTIAL_SIG,
    PSBT_IN_WITNESS_UTXO,
    PSBT_MAGIC,
    PSBT_OUT_BIP32_DERIVATION,
    PSBTError,
    parse_bip32_derivation,
    parse_psbt,
    parse_witness_utxo,
)


def _pair(key: bytes, value: bytes) -> bytes:
    return encode_varint(len(key)) + key + encode_varint(len(value)) + value


def _unsigned_transaction(*, scriptsig: bytes = b"", witness: bool = False) -> bytes:
    inputs = [TxInput(txid_le=b"\x01" * 32, vout=0, scriptsig=scriptsig)]
    outputs = [TxOutput(value=50_000, script=b"\x00\x14" + b"\x02" * 20)]
    witnesses = [[b"\x01"]] if witness else None
    return serialize_transaction(2, inputs, outputs, 0, witnesses=witnesses)


def _psbt(
    unsigned_tx: bytes,
    input_maps: list[bytes],
    output_maps: list[bytes],
    global_records: list[tuple[bytes, bytes]] | None = None,
) -> bytes:
    global_map = _pair(bytes([PSBT_GLOBAL_UNSIGNED_TX]), unsigned_tx)
    for key, value in global_records or []:
        global_map += _pair(key, value)
    return PSBT_MAGIC + global_map + b"\x00" + b"".join(input_maps + output_maps)


def _map(*records: tuple[bytes, bytes]) -> bytes:
    return b"".join(_pair(key, value) for key, value in records) + b"\x00"


def test_roundtrip_preserves_ordered_unknown_records_in_every_map() -> None:
    unsigned_tx = _unsigned_transaction()
    raw = _psbt(
        unsigned_tx,
        [_map((b"\xfcinput", b"input unknown"))],
        [_map((b"\xfcout", b"output unknown"))],
        global_records=[(b"\xfcglobal", b"global unknown")],
    )

    parsed = parse_psbt(raw)

    assert parsed.serialize() == raw
    assert parsed.global_map.records[1].key == b"\xfcglobal"
    assert parsed.input_maps[0].records[0].value == b"input unknown"
    assert parsed.output_maps[0].records[0].key == b"\xfcout"


def test_rejects_duplicate_full_key_within_a_map() -> None:
    unsigned_tx = _unsigned_transaction()
    raw = _psbt(unsigned_tx, [_map((b"\xfcx", b"one"), (b"\xfcx", b"two"))], [_map()])

    with pytest.raises(PSBTError, match="Duplicate PSBT key"):
        parse_psbt(raw)


def test_many_distinct_records_parse_in_linear_time() -> None:
    # A quadratic duplicate scan needs about 1.8e9 comparisons here, which takes
    # far longer than the bound; a set-based check finishes in well under a second.
    records = [(b"\xfc" + index.to_bytes(3, "big"), b"") for index in range(60_000)]
    raw = _psbt(_unsigned_transaction(), [_map(*records)], [_map()])

    started = time.perf_counter()
    parsed = parse_psbt(raw)
    elapsed = time.perf_counter() - started

    assert len(parsed.input_maps[0].records) == len(records)
    assert parsed.serialize() == raw
    assert elapsed < 10


def test_rejects_duplicate_key_after_many_distinct_records() -> None:
    records = [(b"\xfc" + index.to_bytes(2, "big"), b"") for index in range(1_000)]
    raw = _psbt(_unsigned_transaction(), [_map(*records, records[0])], [_map()])

    with pytest.raises(PSBTError, match="Duplicate PSBT key"):
        parse_psbt(raw)


def test_rejects_noncanonical_compact_size() -> None:
    raw = PSBT_MAGIC + b"\xfd\x01\x00\x00"

    with pytest.raises(PSBTError, match="Noncanonical CompactSize"):
        parse_psbt(raw)


@pytest.mark.parametrize(
    "raw",
    [
        PSBT_MAGIC + b"\x01\x00",
        PSBT_MAGIC + b"\x01\x00\x05abc",
    ],
)
def test_rejects_truncated_map_keys_or_values(raw: bytes) -> None:
    with pytest.raises(PSBTError, match="Truncated"):
        parse_psbt(raw)


def test_rejects_missing_or_extra_maps_and_trailing_data() -> None:
    unsigned_tx = _unsigned_transaction()
    missing_output_map = _psbt(unsigned_tx, [_map()], [])
    trailing_data = _psbt(unsigned_tx, [_map()], [_map()]) + b"\x00"

    with pytest.raises(PSBTError, match="missing output map"):
        parse_psbt(missing_output_map)
    with pytest.raises(PSBTError, match="Trailing data"):
        parse_psbt(trailing_data)


@pytest.mark.parametrize(
    ("unsigned_tx", "message"),
    [
        (_unsigned_transaction(scriptsig=b"\x51"), "empty scriptSigs"),
        (_unsigned_transaction(witness=True), "witness data"),
    ],
)
def test_rejects_non_unsigned_transactions(unsigned_tx: bytes, message: str) -> None:
    raw = _psbt(unsigned_tx, [_map()], [_map()])

    with pytest.raises(PSBTError, match=message):
        parse_psbt(raw)


@pytest.mark.parametrize(
    ("version_record", "message"),
    [
        (None, None),
        (bytes(4), None),
        ((1).to_bytes(4, "little"), "version 0"),
        (b"\x00", "4-byte uint32"),
    ],
)
def test_accepts_only_absent_or_zero_version(
    version_record: bytes | None, message: str | None
) -> None:
    unsigned_tx = _unsigned_transaction()
    records = [] if version_record is None else [(bytes([PSBT_GLOBAL_VERSION]), version_record)]
    raw = _psbt(unsigned_tx, [_map()], [_map()], global_records=records)

    if message is None:
        assert parse_psbt(raw).serialize() == raw
    else:
        with pytest.raises(PSBTError, match=message):
            parse_psbt(raw)


def test_rejects_key_data_on_known_singleton_key() -> None:
    unsigned_tx = _unsigned_transaction()
    raw = _psbt(unsigned_tx, [_map((bytes([PSBT_IN_WITNESS_UTXO]) + b"x", b""))], [_map()])

    with pytest.raises(PSBTError, match="singleton key"):
        parse_psbt(raw)


def test_parses_witness_utxo_with_exact_consumption() -> None:
    value = (123_456).to_bytes(8, "little") + encode_varint(2) + b"\x00\x14"

    parsed = parse_witness_utxo(value)

    assert parsed.value == 123_456
    assert parsed.script_pubkey == b"\x00\x14"
    with pytest.raises(PSBTError, match="Trailing data"):
        parse_witness_utxo(value + b"\x00")
    with pytest.raises(PSBTError, match="Truncated"):
        parse_witness_utxo((1).to_bytes(8, "little") + b"\x02\x00")


def test_rejects_witness_utxo_amount_above_max_money() -> None:
    value = (MAX_MONEY + 1).to_bytes(8, "little") + encode_varint(0)

    with pytest.raises(PSBTError, match="exceeds Bitcoin MAX_MONEY"):
        parse_witness_utxo(value)


def test_parses_bip32_origin_with_exact_path() -> None:
    pubkey = b"\x02" + b"\x11" * 32
    key = bytes([PSBT_IN_BIP32_DERIVATION]) + pubkey
    value = b"\xaa\xbb\xcc\xdd" + (0x80000054).to_bytes(4, "little") + (7).to_bytes(4, "little")

    origin = parse_bip32_derivation(key, value)

    assert origin.pubkey == pubkey
    assert origin.fingerprint == b"\xaa\xbb\xcc\xdd"
    assert origin.path == (0x80000054, 7)
    with pytest.raises(PSBTError, match="compressed"):
        parse_bip32_derivation(b"\x06\x04" + b"\x11" * 32, value)
    with pytest.raises(PSBTError, match="fingerprint"):
        parse_bip32_derivation(key, b"\x00" * 5)


def test_append_input_record_rejects_duplicates() -> None:
    unsigned_tx = _unsigned_transaction()
    parsed = parse_psbt(_psbt(unsigned_tx, [_map((b"\xfcfirst", b"one"))], [_map()]))

    parsed.append_input_key_value(0, b"\xfcsecond", b"two")

    assert parsed.input_maps[0].records[-1].key == b"\xfcsecond"
    with pytest.raises(PSBTError, match="Duplicate PSBT key"):
        parsed.append_input_key_value(0, b"\xfcsecond", b"again")
    with pytest.raises(PSBTError, match="out of range"):
        parsed.append_input_key_value(-1, b"\xfclast", b"invalid")


@pytest.mark.parametrize(
    ("global_records", "input_map", "output_map", "message"),
    [
        ([(bytes([PSBT_GLOBAL_XPUB]) + b"x", b"\x00" * 4)], _map(), _map(), "xpub key"),
        (
            [],
            _map((bytes([PSBT_IN_PARTIAL_SIG]) + b"\x02" * 32, b"signature")),
            _map(),
            "compressed public key",
        ),
        (
            [],
            _map(),
            _map(
                (
                    bytes([PSBT_OUT_BIP32_DERIVATION]) + b"\x02" + b"\x11" * 32,
                    b"\x00" * 5,
                )
            ),
            "fingerprint and uint32 path",
        ),
    ],
)
def test_rejects_malformed_known_keyed_records(
    global_records: list[tuple[bytes, bytes]],
    input_map: bytes,
    output_map: bytes,
    message: str,
) -> None:
    raw = _psbt(_unsigned_transaction(), [input_map], [output_map], global_records)

    with pytest.raises(PSBTError, match=message):
        parse_psbt(raw)
