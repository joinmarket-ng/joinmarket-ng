"""Tests for wallet-owned PSBT input discovery and partial signing."""

from __future__ import annotations

from hashlib import sha256

import pytest
from bitcointx.core.key import CKey
from bitcointx.core.script import OP_2, OP_CHECKMULTISIG, CScript
from jmcore.bitcoin import (
    BIP32Derivation,
    PSBTInput,
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    create_psbt,
    encode_varint,
    estimate_vsize,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)
from jmcore.btc_script import mk_freeze_script

from jmwallet.wallet.psbt import (
    PSBT_GLOBAL_FALLBACK_LOCKTIME,
    PSBT_GLOBAL_INPUT_COUNT,
    PSBT_GLOBAL_OUTPUT_COUNT,
    PSBT_GLOBAL_TX_MODIFIABLE,
    PSBT_GLOBAL_TX_VERSION,
    PSBT_GLOBAL_VERSION,
    PSBT_IN_BIP32_DERIVATION,
    PSBT_IN_OUTPUT_INDEX,
    PSBT_IN_PARTIAL_SIG,
    PSBT_IN_PREVIOUS_TXID,
    PSBT_IN_REQUIRED_TIME_LOCKTIME,
    PSBT_IN_SEQUENCE,
    PSBT_IN_SIGHASH_TYPE,
    PSBT_IN_WITNESS_SCRIPT,
    PSBT_IN_WITNESS_UTXO,
    PSBT_MAGIC,
    PSBT_OUT_AMOUNT,
    PSBT_OUT_SCRIPT,
    PSBT_VERSION_0,
    PSBT_VERSION_2,
    TX_MODIFIABLE_HAS_SIGHASH_SINGLE,
    TX_MODIFIABLE_INPUTS,
    TX_MODIFIABLE_OUTPUTS,
    PSBTError,
    PSBTKeyValue,
    parse_psbt,
)
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.signing import (
    TransactionSigningError,
    verify_p2wpkh_signature,
    verify_p2wsh_signature,
)

BOND_LOCKTIME = 1_577_836_800
HARDENED = 0x80000000


def _pair(key: bytes, value: bytes) -> bytes:
    return encode_varint(len(key)) + key + encode_varint(len(value)) + value


def _map(records: list[tuple[bytes, bytes]]) -> bytes:
    return b"".join(_pair(key, value) for key, value in records) + b"\x00"


def _witness_utxo(value: int, script_pubkey: bytes) -> bytes:
    return value.to_bytes(8, "little") + encode_varint(len(script_pubkey)) + script_pubkey


def _origin(fingerprint: bytes, path: tuple[int, ...]) -> bytes:
    return fingerprint + b"".join(index.to_bytes(4, "little") for index in path)


def _build_psbt(
    inputs: list[TxInput],
    outputs: list[TxOutput],
    input_records: list[list[tuple[bytes, bytes]]],
    *,
    locktime: int = 0,
    global_unknown: tuple[bytes, bytes] | None = None,
    output_records: list[list[tuple[bytes, bytes]]] | None = None,
) -> bytes:
    unsigned_tx = serialize_transaction(2, inputs, outputs, locktime)
    global_records = [(b"\x00", unsigned_tx)]
    if global_unknown is not None:
        global_records.append(global_unknown)
    result = bytearray(PSBT_MAGIC)
    result.extend(_map(global_records))
    for records in input_records:
        result.extend(_map(records))
    for records in output_records or [[] for _ in outputs]:
        result.extend(_map(records))
    return bytes(result)


def _regular_input_records(
    wallet_service,
    mixdepth: int,
    change: int,
    index: int,
    value: int,
    *,
    include_origin: bool = True,
    sighash_type: int = 1,
) -> tuple[list[tuple[bytes, bytes]], bytes, bytes]:
    address = wallet_service.get_address(mixdepth, change, index)
    key = wallet_service.get_key_for_address(address)
    assert key is not None
    pubkey = key.get_public_key_bytes(compressed=True)
    script_pubkey = pubkey_to_p2wpkh_script(pubkey)
    records = [
        (bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(value, script_pubkey)),
        (bytes([PSBT_IN_SIGHASH_TYPE]), sighash_type.to_bytes(4, "little")),
    ]
    if include_origin:
        path = (
            84 | HARDENED,
            0 | HARDENED,
            mixdepth | HARDENED,
            change,
            index,
        )
        records.append(
            (
                bytes([PSBT_IN_BIP32_DERIVATION]) + pubkey,
                _origin(wallet_service.master_key.fingerprint, path),
            )
        )
    return records, pubkey, script_pubkey


def test_signs_regular_input_from_verified_key_origin(wallet_service) -> None:
    records, pubkey, _ = _regular_input_records(wallet_service, 1, 1, 3, 100_000)
    inputs = [TxInput.from_hex("aa" * 32, 2)]
    outputs = [TxOutput(value=98_000, script=b"\x00\x14" + b"\x33" * 20)]
    raw = _build_psbt(inputs, outputs, [records])

    plan = wallet_service.prepare_psbt_signing(raw, scan_range=0)
    result = wallet_service.sign_psbt(plan)

    assert plan.owned_count == 1
    assert plan.signable_count == 1
    assert plan.fee == 2_000
    assert result.signed_indices == (0,)
    signed = parse_psbt(result.psbt)
    signature = next(
        record.value
        for record in signed.input_maps[0].records
        if record.key == bytes([PSBT_IN_PARTIAL_SIG]) + pubkey
    )
    assert verify_p2wpkh_signature(
        signed.transaction,
        0,
        create_p2wpkh_script_code(pubkey),
        100_000,
        signature,
        pubkey,
    )


@pytest.mark.parametrize("legacy_empty_script", [False, True])
def test_signs_jmcore_psbt_preserving_input_records(
    wallet_service: WalletService, legacy_empty_script: bool
) -> None:
    """New exports omit empty scripts; signing still preserves older imported records."""
    records, pubkey, script_pubkey = _regular_input_records(wallet_service, 1, 1, 3, 100_000)
    raw = create_psbt(
        version=2,
        inputs=[TxInput.from_hex("aa" * 32, 2)],
        outputs=[TxOutput(value=98_000, script=b"\x00\x14" + b"\x33" * 20)],
        locktime=0,
        psbt_inputs=[
            PSBTInput(
                witness_utxo_value=100_000,
                witness_utxo_script=script_pubkey,
                witness_script=b"",
                bip32_derivations=[
                    BIP32Derivation(
                        pubkey=pubkey,
                        fingerprint=wallet_service.master_key.fingerprint,
                        path=[84 | HARDENED, 0 | HARDENED, 1 | HARDENED, 1, 3],
                    )
                ],
            )
        ],
    )
    parsed = parse_psbt(raw)
    assert [(record.key, record.value) for record in parsed.input_maps[0].records] == records
    if legacy_empty_script:
        parsed.append_input_key_value(0, bytes([PSBT_IN_WITNESS_SCRIPT]), b"")
        raw = parsed.serialize()

    plan = wallet_service.prepare_psbt_signing(raw, scan_range=0)
    result = wallet_service.sign_psbt(plan)

    assert result.signed_indices == (0,)
    signed = parse_psbt(result.psbt)
    assert signed.unsigned_tx == parsed.unsigned_tx
    assert signed.input_maps[0].records[:-1] == parsed.input_maps[0].records
    signature_record = signed.input_maps[0].records[-1]
    assert signature_record.key == bytes([PSBT_IN_PARTIAL_SIG]) + pubkey
    assert verify_p2wpkh_signature(
        signed.transaction,
        0,
        create_p2wpkh_script_code(pubkey),
        100_000,
        signature_record.value,
        pubkey,
    )


def test_fallback_scan_finds_regular_input_without_origin(wallet_service) -> None:
    records, _, _ = _regular_input_records(wallet_service, 0, 0, 2, 50_000, include_origin=False)
    raw = _build_psbt(
        [TxInput.from_hex("bb" * 32, 0)],
        [TxOutput(value=49_000, script=b"\x00\x14" + b"\x44" * 20)],
        [records],
    )

    assert wallet_service.prepare_psbt_signing(raw, scan_range=2).owned_count == 0
    assert wallet_service.prepare_psbt_signing(raw, scan_range=3).owned_count == 1


def test_partially_signs_mixed_wallet_and_foreign_inputs(wallet_service) -> None:
    owned_records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 60_000)
    foreign_pubkey = bytes(CKey.from_secret_bytes((2).to_bytes(32, "big")).pub)
    foreign_script = pubkey_to_p2wpkh_script(foreign_pubkey)
    foreign_records = [(bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(40_000, foreign_script))]
    raw = _build_psbt(
        [TxInput.from_hex("cc" * 32, 0), TxInput.from_hex("dd" * 32, 1)],
        [TxOutput(value=98_000, script=b"\x00\x14" + b"\x55" * 20)],
        [owned_records, foreign_records],
        global_unknown=(b"\xfcglobal", b"keep"),
        output_records=[[(b"\xfcout", b"keep")]],
    )

    plan = wallet_service.prepare_psbt_signing(raw, scan_range=0)
    result = wallet_service.sign_psbt(plan)
    signed = parse_psbt(result.psbt)

    assert plan.owned_count == 1
    assert result.signed_indices == (0,)
    assert not any(record.key[0] == PSBT_IN_PARTIAL_SIG for record in signed.input_maps[1].records)
    assert signed.global_map.records[-1].value == b"keep"
    assert signed.output_maps[0].records[-1].value == b"keep"


def test_signs_canonical_fidelity_bond_without_key_origin(wallet_service) -> None:
    address = wallet_service.get_fidelity_bond_address(0, BOND_LOCKTIME)
    key = wallet_service.get_key_for_address(address)
    assert key is not None
    pubkey = key.get_public_key_bytes(compressed=True)
    witness_script = wallet_service.get_fidelity_bond_script(0, BOND_LOCKTIME)
    script_pubkey = b"\x00\x20" + sha256(witness_script).digest()
    records = [
        (bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(200_000, script_pubkey)),
        (bytes([PSBT_IN_WITNESS_SCRIPT]), witness_script),
        (bytes([PSBT_IN_SIGHASH_TYPE]), (1).to_bytes(4, "little")),
    ]
    raw = _build_psbt(
        [TxInput.from_hex("ee" * 32, 0, sequence=0xFFFFFFFE)],
        [TxOutput(value=198_000, script=b"\x00\x14" + b"\x66" * 20)],
        [records],
        locktime=BOND_LOCKTIME,
    )

    plan = wallet_service.prepare_psbt_signing(raw, scan_range=0)
    result = wallet_service.sign_psbt(plan)
    signed = parse_psbt(result.psbt)
    signature = next(
        record.value
        for record in signed.input_maps[0].records
        if record.key == bytes([PSBT_IN_PARTIAL_SIG]) + pubkey
    )

    assert plan.inputs[0].wallet_input_type == "fidelity-bond"
    assert verify_p2wsh_signature(signed.transaction, 0, witness_script, 200_000, signature, pubkey)


def test_valid_existing_wallet_signature_is_preserved(wallet_service) -> None:
    records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 100_000)
    raw = _build_psbt(
        [TxInput.from_hex("12" * 32, 0)],
        [TxOutput(value=99_000, script=b"\x00\x14" + b"\x77" * 20)],
        [records],
    )
    first = wallet_service.sign_psbt(wallet_service.prepare_psbt_signing(raw, 0))

    second_plan = wallet_service.prepare_psbt_signing(first.psbt, 0)
    second = wallet_service.sign_psbt(second_plan)

    assert second_plan.inputs[0].already_signed
    assert second.signed_indices == ()
    assert second.already_signed_indices == (0,)
    assert second.psbt == first.psbt


@pytest.mark.parametrize(
    ("locktime", "sequence", "message"),
    [
        (BOND_LOCKTIME - 1, 0xFFFFFFFE, "below fidelity bond"),
        (BOND_LOCKTIME, 0xFFFFFFFF, "final sequence"),
    ],
)
def test_rejects_invalid_fidelity_bond_cltv_fields(
    wallet_service, locktime: int, sequence: int, message: str
) -> None:
    address = wallet_service.get_fidelity_bond_address(0, BOND_LOCKTIME)
    witness_script = wallet_service.get_fidelity_bond_script(0, BOND_LOCKTIME)
    script_pubkey = b"\x00\x20" + sha256(witness_script).digest()
    records = [
        (bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(200_000, script_pubkey)),
        (bytes([PSBT_IN_WITNESS_SCRIPT]), witness_script),
    ]
    raw = _build_psbt(
        [TxInput.from_hex("34" * 32, 0, sequence=sequence)],
        [TxOutput(value=199_000, script=b"\x00\x14" + b"\x88" * 20)],
        [records],
        locktime=locktime,
    )
    assert address

    with pytest.raises(PSBTError, match=message):
        wallet_service.prepare_psbt_signing(raw, 0)


def test_rejects_unsupported_sighash_for_owned_input(wallet_service) -> None:
    records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 100_000, sighash_type=2)
    raw = _build_psbt(
        [TxInput.from_hex("56" * 32, 0)],
        [TxOutput(value=99_000, script=b"\x00\x14" + b"\x99" * 20)],
        [records],
    )

    with pytest.raises(PSBTError, match="only SIGHASH_ALL"):
        wallet_service.prepare_psbt_signing(raw, 0)


def test_rejects_missing_witness_utxo_and_negative_fee(wallet_service) -> None:
    missing = _build_psbt(
        [TxInput.from_hex("78" * 32, 0)],
        [TxOutput(value=1, script=b"\x00\x14" + b"\xaa" * 20)],
        [[]],
    )
    with pytest.raises(PSBTError, match="missing witness_utxo"):
        wallet_service.prepare_psbt_signing(missing, 0)

    records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 10_000)
    negative_fee = _build_psbt(
        [TxInput.from_hex("9a" * 32, 0)],
        [TxOutput(value=10_001, script=b"\x00\x14" + b"\xbb" * 20)],
        [records],
    )
    with pytest.raises(PSBTError, match="outputs exceed inputs"):
        wallet_service.prepare_psbt_signing(negative_fee, 0)


def test_rejects_mutation_between_review_and_signing(wallet_service) -> None:
    records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 100_000)
    raw = _build_psbt(
        [TxInput.from_hex("bc" * 32, 0)],
        [TxOutput(value=99_000, script=b"\x00\x14" + b"\xcc" * 20)],
        [records],
    )
    plan = wallet_service.prepare_psbt_signing(raw, 0)
    plan.psbt.transaction.outputs[0].script = b"\x00\x14" + b"\xdd" * 20

    with pytest.raises(TransactionSigningError, match="changed after review"):
        wallet_service.sign_psbt(plan)


def test_rejects_unsupported_foreign_input_and_output_scripts(wallet_service) -> None:
    owned_records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 100_000)
    legacy_script = b"\x76\xa9\x14" + b"\xee" * 20 + b"\x88\xac"
    foreign_records = [(bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(50_000, legacy_script))]
    unsupported_input = _build_psbt(
        [TxInput.from_hex("de" * 32, 0), TxInput.from_hex("ef" * 32, 0)],
        [TxOutput(value=149_000, script=b"\x00\x14" + b"\xff" * 20)],
        [owned_records, foreign_records],
    )
    with pytest.raises(PSBTError, match="unsupported prevout script"):
        wallet_service.prepare_psbt_signing(unsupported_input, 0)

    unsupported_output = _build_psbt(
        [TxInput.from_hex("fa" * 32, 0)],
        [TxOutput(value=99_000, script=b"\x6a\x01\x01")],
        [owned_records],
    )
    with pytest.raises(PSBTError, match="unsupported script type"):
        wallet_service.prepare_psbt_signing(unsupported_output, 0)


@pytest.mark.parametrize("large_dimension", ["inputs", "outputs"])
def test_fee_estimate_accounts_for_large_compact_size_counts(
    wallet_service, large_dimension: str
) -> None:
    owned_records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 1_000_000)
    foreign_script = pubkey_to_p2wpkh_script(
        bytes(CKey.from_secret_bytes((3).to_bytes(32, "big")).pub)
    )
    inputs = [TxInput.from_hex("01" * 32, 0)]
    input_records = [owned_records]
    outputs = [TxOutput(value=1_000, script=b"\x00\x14" + b"\x01" * 20)]
    if large_dimension == "inputs":
        for index in range(1, 253):
            inputs.append(TxInput.from_hex(f"{index + 1:064x}", 0))
            input_records.append(
                [
                    (
                        bytes([PSBT_IN_WITNESS_UTXO]),
                        _witness_utxo(1_000, foreign_script),
                    )
                ]
            )
        outputs[0].value = 1_251_000
    else:
        outputs = [
            TxOutput(value=1_000, script=b"\x00\x14" + index.to_bytes(20, "big"))
            for index in range(253)
        ]

    raw = _build_psbt(inputs, outputs, input_records)
    plan = wallet_service.prepare_psbt_signing(raw, 0)
    expected = estimate_vsize(["p2wpkh"] * len(inputs), ["p2wpkh"] * len(outputs)) + 2

    assert plan.estimated_vsize == expected


# --- BIP370 (PSBT v2) and foreign P2WSH regression coverage ------------------


def _to_v2(
    raw: bytes,
    *,
    tx_modifiable: int | None = None,
    required_time_locktimes: dict[int, int] | None = None,
) -> bytes:
    """Convert a v0 fixture PSBT into the equivalent BIP370 v2 serialization.

    BIP370 fields are prepended to every map so the records already present keep
    their relative order and stay easy to compare after signing. The locktime is
    carried either by per-input required time locktimes or by the global
    fallback locktime, so the reconstructed transaction is byte-identical to the
    v0 unsigned transaction.
    """
    parsed = parse_psbt(raw)
    transaction = parsed.transaction
    global_records: list[tuple[bytes, bytes]] = [
        (bytes([PSBT_GLOBAL_VERSION]), PSBT_VERSION_2.to_bytes(4, "little")),
        (bytes([PSBT_GLOBAL_TX_VERSION]), transaction.version.to_bytes(4, "little")),
        (bytes([PSBT_GLOBAL_INPUT_COUNT]), encode_varint(len(transaction.inputs))),
        (bytes([PSBT_GLOBAL_OUTPUT_COUNT]), encode_varint(len(transaction.outputs))),
    ]
    if not required_time_locktimes:
        global_records.append(
            (bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME]), transaction.locktime.to_bytes(4, "little"))
        )
    if tx_modifiable is not None:
        global_records.append((bytes([PSBT_GLOBAL_TX_MODIFIABLE]), bytes([tx_modifiable])))
    global_records.extend(
        (record.key, record.value) for record in parsed.global_map.records if record.key != b"\x00"
    )

    input_records: list[list[tuple[bytes, bytes]]] = []
    for index, (tx_input, input_map) in enumerate(
        zip(transaction.inputs, parsed.input_maps, strict=True)
    ):
        records: list[tuple[bytes, bytes]] = [
            (bytes([PSBT_IN_PREVIOUS_TXID]), tx_input.txid_le),
            (bytes([PSBT_IN_OUTPUT_INDEX]), tx_input.vout.to_bytes(4, "little")),
            (bytes([PSBT_IN_SEQUENCE]), tx_input.sequence.to_bytes(4, "little")),
        ]
        locktime = (required_time_locktimes or {}).get(index)
        if locktime is not None:
            records.append(
                (bytes([PSBT_IN_REQUIRED_TIME_LOCKTIME]), locktime.to_bytes(4, "little"))
            )
        records.extend((record.key, record.value) for record in input_map.records)
        input_records.append(records)

    output_records: list[list[tuple[bytes, bytes]]] = []
    for tx_output, output_map in zip(transaction.outputs, parsed.output_maps, strict=True):
        output_records.append(
            [
                (bytes([PSBT_OUT_AMOUNT]), tx_output.value.to_bytes(8, "little")),
                (bytes([PSBT_OUT_SCRIPT]), tx_output.script),
                *((record.key, record.value) for record in output_map.records),
            ]
        )

    result = bytearray(PSBT_MAGIC)
    result.extend(_map(global_records))
    for records in input_records:
        result.extend(_map(records))
    for records in output_records:
        result.extend(_map(records))
    return bytes(result)


def _foreign_multisig_script() -> bytes:
    """Build a 2-of-2 P2WSH witness script that no wallet key can satisfy."""
    first = bytes(CKey.from_secret_bytes((7).to_bytes(32, "big")).pub)
    second = bytes(CKey.from_secret_bytes((8).to_bytes(32, "big")).pub)
    return bytes(CScript([OP_2, first, second, OP_2, OP_CHECKMULTISIG]))


def _foreign_partial_signature() -> tuple[bytes, bytes]:
    pubkey = bytes(CKey.from_secret_bytes((7).to_bytes(32, "big")).pub)
    signature = b"\x30\x44\x02\x20" + b"\x11" * 32 + b"\x02\x20" + b"\x22" * 32 + b"\x01"
    return pubkey, signature


def _mixed_owned_and_foreign_p2wsh(wallet_service) -> tuple[bytes, bytes, bytes]:
    """Build a v0 PSBT with one owned P2WPKH input and one foreign 2-of-2 P2WSH input."""
    owned_records, pubkey, _ = _regular_input_records(wallet_service, 0, 0, 0, 60_000)
    witness_script = _foreign_multisig_script()
    script_pubkey = b"\x00\x20" + sha256(witness_script).digest()
    foreign_pubkey, foreign_signature = _foreign_partial_signature()
    foreign_records = [
        (bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(40_000, script_pubkey)),
        (bytes([PSBT_IN_WITNESS_SCRIPT]), witness_script),
        (bytes([PSBT_IN_PARTIAL_SIG]) + foreign_pubkey, foreign_signature),
        (b"\xfcforeign", b"keep"),
    ]
    raw = _build_psbt(
        [TxInput.from_hex("a1" * 32, 0), TxInput.from_hex("b2" * 32, 3)],
        [TxOutput(value=98_000, script=b"\x00\x14" + b"\x21" * 20)],
        [owned_records, foreign_records],
        global_unknown=(b"\xfcglobal", b"keep"),
        output_records=[[(b"\xfcout", b"keep")]],
    )
    return raw, pubkey, witness_script


@pytest.mark.parametrize("psbt_version", [PSBT_VERSION_0, PSBT_VERSION_2])
def test_signs_owned_input_beside_foreign_multisig_p2wsh(wallet_service, psbt_version: int) -> None:
    """A foreign 2-of-2 P2WSH input stays untouched while the wallet input is signed."""
    v0_raw, pubkey, _ = _mixed_owned_and_foreign_p2wsh(wallet_service)
    raw = v0_raw if psbt_version == PSBT_VERSION_0 else _to_v2(v0_raw)
    original = parse_psbt(raw)

    plan = wallet_service.prepare_psbt_signing(raw, scan_range=0)
    result = wallet_service.sign_psbt(plan)
    signed = parse_psbt(result.psbt)

    assert original.version == psbt_version
    assert original.unsigned_tx == parse_psbt(v0_raw).unsigned_tx
    assert plan.owned_count == 1
    assert plan.inputs[1].input_type == "p2wsh"
    assert not plan.inputs[1].owned
    assert plan.inputs[1].wallet_input_type is None
    assert plan.fee_rate_is_upper_bound
    assert plan.estimated_vsize == len(original.unsigned_tx)
    assert plan.fee == 2_000
    assert result.signed_indices == (0,)
    assert signed.version == psbt_version

    # The foreign input map, including its partial signature and proprietary
    # record, must survive signing byte for byte.
    assert signed.input_maps[1].records == original.input_maps[1].records
    assert signed.output_maps[0].records == original.output_maps[0].records
    assert signed.global_map.records == original.global_map.records
    assert signed.input_maps[0].records[:-1] == original.input_maps[0].records

    signature_record = signed.input_maps[0].records[-1]
    assert signature_record.key == bytes([PSBT_IN_PARTIAL_SIG]) + pubkey
    # The signature must commit to the transaction that was reviewed, not to a
    # transaction rebuilt from the signed PSBT.
    assert verify_p2wpkh_signature(
        parse_psbt(v0_raw).transaction,
        0,
        create_p2wpkh_script_code(pubkey),
        60_000,
        signature_record.value,
        pubkey,
    )


def test_foreign_p2wsh_without_witness_script_is_unowned_and_untouched(wallet_service) -> None:
    owned_records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 60_000)
    script_pubkey = b"\x00\x20" + sha256(b"unknown script").digest()
    foreign_records = [(bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(40_000, script_pubkey))]
    raw = _build_psbt(
        [TxInput.from_hex("c3" * 32, 0), TxInput.from_hex("d4" * 32, 1)],
        [TxOutput(value=98_000, script=b"\x00\x14" + b"\x31" * 20)],
        [owned_records, foreign_records],
    )
    original = parse_psbt(raw)

    plan = wallet_service.prepare_psbt_signing(raw, scan_range=0)
    result = wallet_service.sign_psbt(plan)

    assert not plan.inputs[1].owned
    assert plan.fee_rate_is_upper_bound
    assert plan.estimated_vsize == len(original.unsigned_tx)
    assert result.signed_indices == (0,)
    assert parse_psbt(result.psbt).input_maps[1].records == original.input_maps[1].records


def test_rejects_foreign_p2wsh_witness_script_mismatch(wallet_service) -> None:
    owned_records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 60_000)
    foreign_records = [
        (
            bytes([PSBT_IN_WITNESS_UTXO]),
            _witness_utxo(40_000, b"\x00\x20" + sha256(b"other script").digest()),
        ),
        (bytes([PSBT_IN_WITNESS_SCRIPT]), _foreign_multisig_script()),
    ]
    raw = _build_psbt(
        [TxInput.from_hex("e5" * 32, 0), TxInput.from_hex("f6" * 32, 1)],
        [TxOutput(value=98_000, script=b"\x00\x14" + b"\x41" * 20)],
        [owned_records, foreign_records],
    )

    with pytest.raises(PSBTError, match="witness_script does not match its P2WSH witness_utxo"):
        wallet_service.prepare_psbt_signing(raw, 0)


def test_foreign_canonical_bond_stays_unowned_despite_wallet_key_origin(wallet_service) -> None:
    """A canonical bond script for a foreign key is never claimed by the wallet."""
    foreign_pubkey = bytes(CKey.from_secret_bytes((9).to_bytes(32, "big")).pub)
    witness_script = mk_freeze_script(foreign_pubkey.hex(), BOND_LOCKTIME)
    script_pubkey = b"\x00\x20" + sha256(witness_script).digest()
    wallet_path = (84 | HARDENED, 0 | HARDENED, 0 | HARDENED, 0, 0)
    records = [
        (bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(200_000, script_pubkey)),
        (bytes([PSBT_IN_WITNESS_SCRIPT]), witness_script),
        (
            bytes([PSBT_IN_BIP32_DERIVATION]) + foreign_pubkey,
            _origin(wallet_service.master_key.fingerprint, wallet_path),
        ),
    ]
    # A final sequence and a zero locktime would be rejected for an owned bond,
    # so reaching a plan at all proves the input was treated as foreign.
    raw = _build_psbt(
        [TxInput.from_hex("07" * 32, 0, sequence=0xFFFFFFFF)],
        [TxOutput(value=199_000, script=b"\x00\x14" + b"\x51" * 20)],
        [records],
    )
    original = parse_psbt(raw)

    plan = wallet_service.prepare_psbt_signing(raw, scan_range=3)
    result = wallet_service.sign_psbt(plan)

    assert plan.owned_count == 0
    assert plan.inputs[0].wallet_input_type is None
    assert plan.fee_rate_is_upper_bound
    assert plan.estimated_vsize == len(original.unsigned_tx)
    assert result.signed_indices == ()
    assert result.psbt == raw


def test_v2_bond_signs_using_required_time_locktime(wallet_service) -> None:
    address = wallet_service.get_fidelity_bond_address(0, BOND_LOCKTIME)
    key = wallet_service.get_key_for_address(address)
    assert key is not None
    pubkey = key.get_public_key_bytes(compressed=True)
    witness_script = wallet_service.get_fidelity_bond_script(0, BOND_LOCKTIME)
    script_pubkey = b"\x00\x20" + sha256(witness_script).digest()
    records = [
        (bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(200_000, script_pubkey)),
        (bytes([PSBT_IN_WITNESS_SCRIPT]), witness_script),
    ]
    v0_raw = _build_psbt(
        [TxInput.from_hex("18" * 32, 0, sequence=0xFFFFFFFE)],
        [TxOutput(value=198_000, script=b"\x00\x14" + b"\x61" * 20)],
        [records],
        locktime=BOND_LOCKTIME,
    )
    raw = _to_v2(v0_raw, required_time_locktimes={0: BOND_LOCKTIME})
    original = parse_psbt(raw)

    plan = wallet_service.prepare_psbt_signing(raw, scan_range=0)
    result = wallet_service.sign_psbt(plan)
    signed = parse_psbt(result.psbt)

    assert original.version == PSBT_VERSION_2
    assert not any(
        record.key == bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME])
        for record in original.global_map.records
    )
    assert original.transaction.locktime == BOND_LOCKTIME
    assert plan.inputs[0].wallet_input_type == "fidelity-bond"
    assert not plan.fee_rate_is_upper_bound
    assert result.signed_indices == (0,)
    signature = signed.input_maps[0].records[-1].value
    assert signed.input_maps[0].records[-1].key == bytes([PSBT_IN_PARTIAL_SIG]) + pubkey
    assert verify_p2wsh_signature(
        original.transaction, 0, witness_script, 200_000, signature, pubkey
    )


@pytest.mark.parametrize("mutation", ["transaction", "map"])
def test_rejects_v2_mutation_between_review_and_signing(wallet_service, mutation: str) -> None:
    """v2 serialization omits the transaction, so both views must be rechecked."""
    records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 100_000)
    raw = _to_v2(
        _build_psbt(
            [TxInput.from_hex("29" * 32, 0)],
            [TxOutput(value=99_000, script=b"\x00\x14" + b"\x71" * 20)],
            [records],
        )
    )
    plan = wallet_service.prepare_psbt_signing(raw, 0)
    if mutation == "transaction":
        plan.psbt.transaction.outputs[0].value = 1
    else:
        plan.psbt.output_maps[0].records[0] = PSBTKeyValue(
            key=bytes([PSBT_OUT_AMOUNT]), value=(1).to_bytes(8, "little")
        )

    with pytest.raises(TransactionSigningError, match="changed after review"):
        wallet_service.sign_psbt(plan)


def test_signing_v2_clears_only_input_and_output_modifiable_bits(wallet_service) -> None:
    records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 100_000)
    unknown_bit = 0x80
    flags = (
        TX_MODIFIABLE_INPUTS
        | TX_MODIFIABLE_OUTPUTS
        | TX_MODIFIABLE_HAS_SIGHASH_SINGLE
        | unknown_bit
    )
    raw = _to_v2(
        _build_psbt(
            [TxInput.from_hex("3a" * 32, 0)],
            [TxOutput(value=99_000, script=b"\x00\x14" + b"\x81" * 20)],
            [records],
        ),
        tx_modifiable=flags,
    )
    original = parse_psbt(raw)

    result = wallet_service.sign_psbt(wallet_service.prepare_psbt_signing(raw, 0))
    signed = parse_psbt(result.psbt)

    assert result.signed_indices == (0,)
    assert [record.key for record in signed.global_map.records] == [
        record.key for record in original.global_map.records
    ]
    modifiable = next(
        record.value
        for record in signed.global_map.records
        if record.key == bytes([PSBT_GLOBAL_TX_MODIFIABLE])
    )
    assert modifiable == bytes([TX_MODIFIABLE_HAS_SIGHASH_SINGLE | unknown_bit])


def test_absent_tx_modifiable_stays_absent_after_signing_v2(wallet_service) -> None:
    records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 100_000)
    raw = _to_v2(
        _build_psbt(
            [TxInput.from_hex("4b" * 32, 0)],
            [TxOutput(value=99_000, script=b"\x00\x14" + b"\x91" * 20)],
            [records],
        )
    )
    original = parse_psbt(raw)

    result = wallet_service.sign_psbt(wallet_service.prepare_psbt_signing(raw, 0))
    signed = parse_psbt(result.psbt)

    assert result.signed_indices == (0,)
    assert not any(
        record.key == bytes([PSBT_GLOBAL_TX_MODIFIABLE]) for record in signed.global_map.records
    )
    assert signed.global_map.records == original.global_map.records


def test_v2_psbt_is_byte_identical_when_nothing_new_is_signed(wallet_service) -> None:
    """Modifiable flags are only cleared by this wallet when it adds a signature."""
    records, _, _ = _regular_input_records(wallet_service, 0, 0, 0, 100_000)
    v0_raw = _build_psbt(
        [TxInput.from_hex("5c" * 32, 0)],
        [TxOutput(value=99_000, script=b"\x00\x14" + b"\xa1" * 20)],
        [records],
    )
    already_signed_v0 = wallet_service.sign_psbt(
        wallet_service.prepare_psbt_signing(v0_raw, 0)
    ).psbt
    flags = TX_MODIFIABLE_INPUTS | TX_MODIFIABLE_OUTPUTS
    raw = _to_v2(already_signed_v0, tx_modifiable=flags)

    plan = wallet_service.prepare_psbt_signing(raw, 0)
    result = wallet_service.sign_psbt(plan)

    assert plan.inputs[0].already_signed
    assert result.signed_indices == ()
    assert result.already_signed_indices == (0,)
    assert result.psbt == raw
