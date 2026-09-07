"""Tests for wallet-owned PSBT input discovery and partial signing."""

from __future__ import annotations

from hashlib import sha256

import pytest
from bitcointx.core.key import CKey
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

from jmwallet.wallet.psbt import (
    PSBT_IN_BIP32_DERIVATION,
    PSBT_IN_PARTIAL_SIG,
    PSBT_IN_SIGHASH_TYPE,
    PSBT_IN_WITNESS_SCRIPT,
    PSBT_IN_WITNESS_UTXO,
    PSBT_MAGIC,
    PSBTError,
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
