"""CLI tests for offline PSBT review and signing."""

from __future__ import annotations

import base64
from hashlib import sha256
from pathlib import Path

import pytest
from _jmwallet_test_helpers import TEST_MNEMONIC
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    encode_varint,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)
from typer.testing import CliRunner

from jmwallet.backends.offline import OfflineBackend
from jmwallet.cli import app
from jmwallet.wallet.psbt import (
    PSBT_GLOBAL_FALLBACK_LOCKTIME,
    PSBT_GLOBAL_INPUT_COUNT,
    PSBT_GLOBAL_OUTPUT_COUNT,
    PSBT_GLOBAL_TX_VERSION,
    PSBT_GLOBAL_VERSION,
    PSBT_IN_BIP32_DERIVATION,
    PSBT_IN_OUTPUT_INDEX,
    PSBT_IN_PARTIAL_SIG,
    PSBT_IN_PREVIOUS_TXID,
    PSBT_IN_SEQUENCE,
    PSBT_IN_WITNESS_SCRIPT,
    PSBT_IN_WITNESS_UTXO,
    PSBT_MAGIC,
    PSBT_OUT_AMOUNT,
    PSBT_OUT_SCRIPT,
    PSBT_VERSION_2,
    parse_psbt,
)
from jmwallet.wallet.service import WalletService

HARDENED = 0x80000000
BOND_LOCKTIME = 1_577_836_800


def _pair(key: bytes, value: bytes) -> bytes:
    return encode_varint(len(key)) + key + encode_varint(len(value)) + value


def _map(*records: tuple[bytes, bytes]) -> bytes:
    return b"".join(_pair(key, value) for key, value in records) + b"\x00"


def _witness_utxo(value: int, script_pubkey: bytes) -> bytes:
    return value.to_bytes(8, "little") + encode_varint(len(script_pubkey)) + script_pubkey


def _psbt(
    tx_input: TxInput,
    tx_output: TxOutput,
    input_records: list[tuple[bytes, bytes]],
    *,
    locktime: int = 0,
) -> bytes:
    unsigned_tx = serialize_transaction(2, [tx_input], [tx_output], locktime)
    return PSBT_MAGIC + _map((b"\x00", unsigned_tx)) + _map(*input_records) + _map()


def _wallet() -> WalletService:
    return WalletService(TEST_MNEMONIC, OfflineBackend(), network="mainnet")


def _regular_psbt(*, value: int = 100_000, output_value: int = 99_000) -> bytes:
    wallet = _wallet()
    address = wallet.get_address(0, 0, 0)
    key = wallet.get_key_for_address(address)
    assert key is not None
    pubkey = key.get_public_key_bytes(compressed=True)
    script_pubkey = pubkey_to_p2wpkh_script(pubkey)
    path = (84 | HARDENED, 0 | HARDENED, 0 | HARDENED, 0, 0)
    origin = wallet.master_key.fingerprint + b"".join(child.to_bytes(4, "little") for child in path)
    return _psbt(
        TxInput.from_hex("aa" * 32, 0),
        TxOutput(output_value, b"\x00\x14" + b"\x22" * 20),
        [
            (bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(value, script_pubkey)),
            (bytes([PSBT_IN_BIP32_DERIVATION]) + pubkey, origin),
        ],
    )


def _invoke_args(raw: bytes, data_dir: Path) -> list[str]:
    return [
        "sign-psbt",
        base64.b64encode(raw).decode("ascii"),
        "--network",
        "mainnet",
        "--scan-range",
        "0",
        "--data-dir",
        str(data_dir),
    ]


def _extract_signed_psbt(stdout: str) -> bytes:
    lines = stdout.splitlines()
    marker_index = lines.index("Signed PSBT (base64):")
    return base64.b64decode(lines[marker_index + 1], validate=True)


def test_sign_psbt_reviews_and_signs_regular_input(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        _invoke_args(_regular_psbt(), tmp_path) + ["--yes"],
        env={"MNEMONIC": TEST_MNEMONIC},
    )

    assert result.exit_code == 0, result.stdout
    assert "PSBT REVIEW" in result.stdout
    assert "WALLET REGULAR" in result.stdout
    assert "Fee:               1,000 sats" in result.stdout
    assert "Signed 1 input(s)" in result.stdout
    signed = parse_psbt(_extract_signed_psbt(result.stdout))
    assert any(record.key[0] == PSBT_IN_PARTIAL_SIG for record in signed.input_maps[0].records)


def test_signpsbt_compatibility_alias(tmp_path: Path) -> None:
    args = _invoke_args(_regular_psbt(), tmp_path)
    args[0] = "signpsbt"

    result = CliRunner().invoke(app, args + ["--yes"], env={"MNEMONIC": TEST_MNEMONIC})

    assert result.exit_code == 0, result.stdout
    assert "Signed 1 input(s)" in result.stdout


def test_sign_psbt_requires_confirmation(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        _invoke_args(_regular_psbt(), tmp_path),
        input="n\n",
        env={"MNEMONIC": TEST_MNEMONIC},
    )

    assert result.exit_code == 1
    assert "Signing cancelled" in result.stdout
    assert "Signed PSBT (base64):" not in result.stdout


def test_sign_psbt_rejects_excessive_fee(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        _invoke_args(_regular_psbt(value=200_000, output_value=1), tmp_path) + ["--yes"],
        env={"MNEMONIC": TEST_MNEMONIC},
    )

    assert result.exit_code == 1
    assert "exceeds safety cap" in result.stderr
    assert "Signed PSBT (base64):" not in result.stdout


def test_sign_psbt_reads_binary_and_writes_base64_file(tmp_path: Path) -> None:
    input_file = tmp_path / "unsigned.psbt"
    output_file = tmp_path / "signed.psbt"
    input_file.write_bytes(_regular_psbt())
    result = CliRunner().invoke(
        app,
        [
            "sign-psbt",
            "--input",
            str(input_file),
            "--output",
            str(output_file),
            "--network",
            "mainnet",
            "--scan-range",
            "0",
            "--yes",
            "--data-dir",
            str(tmp_path),
        ],
        env={"MNEMONIC": TEST_MNEMONIC},
    )

    assert result.exit_code == 0, result.stdout
    signed = parse_psbt(base64.b64decode(output_file.read_text().strip(), validate=True))
    assert any(record.key[0] == PSBT_IN_PARTIAL_SIG for record in signed.input_maps[0].records)


def test_sign_psbt_signs_fidelity_bond_input(tmp_path: Path) -> None:
    wallet = _wallet()
    witness_script = wallet.get_fidelity_bond_script(0, BOND_LOCKTIME)
    script_pubkey = b"\x00\x20" + sha256(witness_script).digest()
    raw = _psbt(
        TxInput.from_hex("bb" * 32, 1, sequence=0xFFFFFFFE),
        TxOutput(198_000, b"\x00\x14" + b"\x33" * 20),
        [
            (bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(200_000, script_pubkey)),
            (bytes([PSBT_IN_WITNESS_SCRIPT]), witness_script),
        ],
        locktime=BOND_LOCKTIME,
    )

    result = CliRunner().invoke(
        app,
        _invoke_args(raw, tmp_path) + ["--yes"],
        env={"MNEMONIC": TEST_MNEMONIC},
    )

    assert result.exit_code == 0, result.stdout
    assert "WALLET FIDELITY BOND" in result.stdout
    signed = parse_psbt(_extract_signed_psbt(result.stdout))
    assert any(record.key[0] == PSBT_IN_PARTIAL_SIG for record in signed.input_maps[0].records)


# --- BIP370 (PSBT v2) and foreign P2WSH review ------------------------------


def _to_v2(raw: bytes) -> bytes:
    """Convert a single-output v0 fixture PSBT into BIP370 v2 bytes.

    The BIP370 fields are prepended so the records already present keep their
    order, and the fallback locktime reproduces the same transaction.
    """
    parsed = parse_psbt(raw)
    transaction = parsed.transaction
    global_records = [
        (bytes([PSBT_GLOBAL_VERSION]), PSBT_VERSION_2.to_bytes(4, "little")),
        (bytes([PSBT_GLOBAL_TX_VERSION]), transaction.version.to_bytes(4, "little")),
        (bytes([PSBT_GLOBAL_INPUT_COUNT]), encode_varint(len(transaction.inputs))),
        (bytes([PSBT_GLOBAL_OUTPUT_COUNT]), encode_varint(len(transaction.outputs))),
        (bytes([PSBT_GLOBAL_FALLBACK_LOCKTIME]), transaction.locktime.to_bytes(4, "little")),
    ]
    result = bytearray(PSBT_MAGIC) + _map(*global_records)
    for tx_input, input_map in zip(transaction.inputs, parsed.input_maps, strict=True):
        result += _map(
            (bytes([PSBT_IN_PREVIOUS_TXID]), tx_input.txid_le),
            (bytes([PSBT_IN_OUTPUT_INDEX]), tx_input.vout.to_bytes(4, "little")),
            (bytes([PSBT_IN_SEQUENCE]), tx_input.sequence.to_bytes(4, "little")),
            *((record.key, record.value) for record in input_map.records),
        )
    for tx_output in transaction.outputs:
        result += _map(
            (bytes([PSBT_OUT_AMOUNT]), tx_output.value.to_bytes(8, "little")),
            (bytes([PSBT_OUT_SCRIPT]), tx_output.script),
        )
    return bytes(result)


def _owned_input_records(wallet: WalletService, value: int) -> list[tuple[bytes, bytes]]:
    address = wallet.get_address(0, 0, 0)
    key = wallet.get_key_for_address(address)
    assert key is not None
    pubkey = key.get_public_key_bytes(compressed=True)
    path = (84 | HARDENED, 0 | HARDENED, 0 | HARDENED, 0, 0)
    origin = wallet.master_key.fingerprint + b"".join(child.to_bytes(4, "little") for child in path)
    return [
        (bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(value, pubkey_to_p2wpkh_script(pubkey))),
        (bytes([PSBT_IN_BIP32_DERIVATION]) + pubkey, origin),
    ]


def _foreign_p2wsh_psbt(*, value: int, output_value: int) -> bytes:
    """Build a PSBT with one owned P2WPKH input and one unowned P2WSH input."""
    wallet = _wallet()
    foreign_script = b"\x00\x20" + sha256(b"foreign witness script").digest()
    unsigned_tx = serialize_transaction(
        2,
        [TxInput.from_hex("cc" * 32, 0), TxInput.from_hex("dd" * 32, 1)],
        [TxOutput(output_value, b"\x00\x14" + b"\x44" * 20)],
        0,
    )
    return (
        PSBT_MAGIC
        + _map((b"\x00", unsigned_tx))
        + _map(*_owned_input_records(wallet, value))
        + _map((bytes([PSBT_IN_WITNESS_UTXO]), _witness_utxo(value, foreign_script)))
        + _map()
    )


def test_sign_psbt_round_trips_a_v2_input(tmp_path: Path) -> None:
    raw = _to_v2(_regular_psbt())
    assert parse_psbt(raw).version == PSBT_VERSION_2

    result = CliRunner().invoke(
        app,
        _invoke_args(raw, tmp_path) + ["--yes"],
        env={"MNEMONIC": TEST_MNEMONIC},
    )

    assert result.exit_code == 0, result.stdout
    assert "WALLET REGULAR" in result.stdout
    assert "Signed 1 input(s)" in result.stdout
    signed = parse_psbt(_extract_signed_psbt(result.stdout))
    assert signed.version == PSBT_VERSION_2
    assert signed.unsigned_tx == parse_psbt(raw).unsigned_tx
    assert any(record.key[0] == PSBT_IN_PARTIAL_SIG for record in signed.input_maps[0].records)


def test_sign_psbt_displays_foreign_p2wsh_fee_rate_upper_bound(tmp_path: Path) -> None:
    raw = _foreign_p2wsh_psbt(value=100_000, output_value=198_000)

    result = CliRunner().invoke(
        app,
        _invoke_args(raw, tmp_path) + ["--yes"],
        env={"MNEMONIC": TEST_MNEMONIC},
    )

    assert result.exit_code == 0, result.stdout
    assert "Minimum vsize:     " in result.stdout
    assert "Fee rate upper bound:" in result.stdout
    assert "Estimated fee rate:" not in result.stdout
    assert "Foreign P2WSH witness sizes are unknown" in result.stdout
    assert "Signed 1 input(s)" in result.stdout
    signed = parse_psbt(_extract_signed_psbt(result.stdout))
    assert any(record.key[0] == PSBT_IN_PARTIAL_SIG for record in signed.input_maps[0].records)
    assert not any(record.key[0] == PSBT_IN_PARTIAL_SIG for record in signed.input_maps[1].records)


@pytest.mark.parametrize("psbt_version", [0, PSBT_VERSION_2])
def test_sign_psbt_enforces_fee_cap_on_foreign_p2wsh_upper_bound(
    tmp_path: Path, psbt_version: int
) -> None:
    raw = _foreign_p2wsh_psbt(value=1_000_000, output_value=1)
    if psbt_version == PSBT_VERSION_2:
        raw = _to_v2(raw)

    result = CliRunner().invoke(
        app,
        _invoke_args(raw, tmp_path) + ["--yes"],
        env={"MNEMONIC": TEST_MNEMONIC},
    )

    assert result.exit_code == 1
    assert "PSBT upper bound fee rate" in result.stderr
    assert "exceeds safety cap" in result.stderr
    assert "Signed PSBT (base64):" not in result.stdout
