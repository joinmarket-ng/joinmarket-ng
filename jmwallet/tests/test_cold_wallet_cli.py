"""CLI tests for cold-wallet commands."""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

import pytest
from bitcointx.core.key import CKey
from jmcore.crypto import bitcoin_message_hash_bytes
from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    BondUtxo,
    FidelityBondInfo,
    get_registry_path,
    load_registry,
    save_registry,
)

runner = CliRunner()

SEEDSIGNER_REGISTRATION = json.dumps(
    {
        "type": "seedsigner-bip46",
        "version": 1,
        "network": "mainnet",
        "master_fingerprint": "73c5da0a",
        "derivation_path": "m/84'/0'/0'/2/240",
        "index": 240,
        "locktime": 2208988800,
        "locktime_date": "2040-01",
        "pubkey": "03ec8067418537bbb52d5d3e64e2868e67635c33cfeadeb9a46199f89ebfaab226",
        "address": "bc1qul0q45njptsadnymdtv34at7karyva3v7k2vj8qc7m2702rnvddq0z20u5",
    },
    separators=(",", ":"),
)


def _key(value: int) -> CKey:
    return CKey.from_secret_bytes(value.to_bytes(32, "big"))


def test_generate_hot_keypair_creates_private_data_dir(tmp_path: Path) -> None:
    import os
    import stat

    data_dir = tmp_path / "missing" / "data"
    previous_umask = os.umask(0o022)
    try:
        result = runner.invoke(app, ["generate-hot-keypair", "--data-dir", str(data_dir)])
    finally:
        os.umask(previous_umask)

    assert result.exit_code == 0, result.output
    assert stat.S_IMODE(data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(data_dir.parent.stat().st_mode) == 0o700


def test_generate_hot_keypair_does_not_print_private_key_and_writes_key_file():
    """generate-hot-keypair should avoid stdout key leakage and write a 0600 file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        result = runner.invoke(
            app,
            [
                "generate-hot-keypair",
                "--data-dir",
                str(data_dir),
            ],
        )

        assert result.exit_code == 0
        assert "Public Key (hex):" in result.stdout
        assert "Private Key (hex):" not in result.stdout

        key_files = list(data_dir.glob("hot_certificate_key_*.json"))
        assert len(key_files) == 1

        file_mode = key_files[0].stat().st_mode & 0o777
        assert file_mode == 0o600

        payload = json.loads(key_files[0].read_text())
        assert "cert_pubkey" in payload
        assert "cert_privkey" in payload
        assert len(payload["cert_pubkey"]) == 66
        assert len(payload["cert_privkey"]) == 64


def test_create_bond_address_rejects_invalid_curve_pubkey():
    """A compressed prefix alone is not enough for a valid public key."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = runner.invoke(
            app,
            [
                "create-bond-address",
                "03" + "11" * 32,
                "--locktime-date",
                "2030-01",
                "--data-dir",
                tmpdir,
                "--wallet-fingerprint",
                "deadbeef",
            ],
        )

    assert result.exit_code == 1


def test_create_bond_address_rejects_nonfuture_locktime_without_explicit_override(
    tmp_path: Path,
) -> None:
    pubkey = bytes(_key(1).pub).hex()
    base_args = [
        "create-bond-address",
        pubkey,
        "--locktime-date",
        "2020-01",
        "--data-dir",
        str(tmp_path),
        "--wallet-fingerprint",
        "deadbeef",
    ]

    rejected = runner.invoke(app, base_args)
    assert rejected.exit_code == 1
    assert not get_registry_path(tmp_path, "deadbeef").exists()

    allowed = runner.invoke(app, [*base_args, "--allow-expired"])
    assert allowed.exit_code == 0, allowed.stdout


def test_import_bond_registration_persists_verified_signer_metadata(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "import-bond-registration",
            SEEDSIGNER_REGISTRATION,
            "--data-dir",
            str(tmp_path),
            "--wallet-fingerprint",
            "deadbeef",
        ],
    )

    assert result.exit_code == 0, result.stdout
    registry = load_registry(tmp_path, "deadbeef", allow_legacy_fallback=False)
    assert len(registry.bonds) == 1
    bond = registry.bonds[0]
    assert bond.address == "bc1qul0q45njptsadnymdtv34at7karyva3v7k2vj8qc7m2702rnvddq0z20u5"
    assert bond.pubkey == "03ec8067418537bbb52d5d3e64e2868e67635c33cfeadeb9a46199f89ebfaab226"
    assert bond.witness_script_hex == (
        "05807eaa8300b1752103ec8067418537bbb52d5d3e64e2868e67635c33cfeadeb9a46199f89ebfaab226ac"
    )
    assert bond.path == "m/84'/0'/0'/2/240"
    assert bond.signer_master_fingerprint == "73c5da0a"
    assert bond.index == -1


def test_import_bond_registration_accepts_file_and_requires_exactly_one_source(
    tmp_path: Path,
) -> None:
    registration_file = tmp_path / "seedsigner-registration.json"
    registration_file.write_text(SEEDSIGNER_REGISTRATION + "\n")

    file_result = runner.invoke(
        app,
        [
            "import-bond-registration",
            "--file",
            str(registration_file),
            "--data-dir",
            str(tmp_path),
            "--wallet-fingerprint",
            "deadbeef",
        ],
    )
    assert file_result.exit_code == 0, file_result.stdout

    both_result = runner.invoke(
        app,
        [
            "import-bond-registration",
            SEEDSIGNER_REGISTRATION,
            "--file",
            str(registration_file),
            "--data-dir",
            str(tmp_path),
            "--wallet-fingerprint",
            "deadbeef",
        ],
    )
    assert both_result.exit_code == 1


def test_import_bond_registration_does_not_copy_legacy_or_overwrite_invalid_registry(
    tmp_path: Path,
) -> None:
    legacy_bond = FidelityBondInfo(
        address="bc1qlegacy",
        locktime=2208988800,
        locktime_human="2040-01-01 00:00:00",
        index=240,
        path="m/84'/0'/0'/2/240",
        pubkey="03ec8067418537bbb52d5d3e64e2868e67635c33cfeadeb9a46199f89ebfaab226",
        witness_script_hex="aa",
        network="mainnet",
        created_at="2025-01-01T00:00:00",
    )
    save_registry(BondRegistry(bonds=[legacy_bond]), tmp_path)

    result = runner.invoke(
        app,
        [
            "import-bond-registration",
            SEEDSIGNER_REGISTRATION,
            "--data-dir",
            str(tmp_path),
            "--wallet-fingerprint",
            "deadbeef",
        ],
    )
    assert result.exit_code == 0, result.stdout
    per_wallet = load_registry(tmp_path, "deadbeef", allow_legacy_fallback=False)
    assert [bond.address for bond in per_wallet.bonds] == [
        "bc1qul0q45njptsadnymdtv34at7karyva3v7k2vj8qc7m2702rnvddq0z20u5"
    ]
    assert [bond.address for bond in load_registry(tmp_path).bonds] == ["bc1qlegacy"]

    registry_path = get_registry_path(tmp_path, "cafebabe")
    registry_path.write_text("invalid registry")
    invalid_result = runner.invoke(
        app,
        [
            "import-bond-registration",
            SEEDSIGNER_REGISTRATION,
            "--data-dir",
            str(tmp_path),
            "--wallet-fingerprint",
            "cafebabe",
        ],
    )
    assert invalid_result.exit_code == 1
    assert registry_path.read_text() == "invalid registry"


def test_import_bond_registration_preserves_duplicate_operational_metadata(tmp_path: Path) -> None:
    existing = FidelityBondInfo(
        address="bc1qul0q45njptsadnymdtv34at7karyva3v7k2vj8qc7m2702rnvddq0z20u5",
        locktime=1,
        locktime_human="1970-01-01 00:00:01",
        index=12,
        path="external",
        pubkey="02" + "11" * 32,
        witness_script_hex="aa",
        network="regtest",
        created_at="2025-01-01T00:00:00",
        txid="11" * 32,
        vout=2,
        value=123_456,
        confirmations=42,
        extra_utxos=[BondUtxo(txid="22" * 32, vout=3, value=456, confirmations=4)],
        cert_pubkey="02" + "22" * 32,
        cert_privkey="33" * 32,
        cert_signature="44" * 64,
        cert_expiry=500,
    )
    save_registry(BondRegistry(bonds=[existing]), tmp_path, "deadbeef")

    result = runner.invoke(
        app,
        [
            "import-bond-registration",
            SEEDSIGNER_REGISTRATION,
            "--data-dir",
            str(tmp_path),
            "--wallet-fingerprint",
            "deadbeef",
        ],
    )
    assert result.exit_code == 0, result.stdout

    imported = load_registry(tmp_path, "deadbeef", allow_legacy_fallback=False).bonds[0]
    assert imported.path == "m/84'/0'/0'/2/240"
    assert imported.signer_master_fingerprint == "73c5da0a"
    assert imported.index == -1
    assert imported.created_at == "2025-01-01T00:00:00"
    assert (imported.txid, imported.vout, imported.value, imported.confirmations) == (
        "11" * 32,
        2,
        123_456,
        42,
    )
    assert [(utxo.txid, utxo.value) for utxo in imported.extra_utxos] == [("22" * 32, 456)]
    assert (
        imported.cert_pubkey,
        imported.cert_privkey,
        imported.cert_signature,
        imported.cert_expiry,
    ) == (
        "02" + "22" * 32,
        "33" * 32,
        "44" * 64,
        500,
    )


def test_prepare_certificate_message_accepts_current_block_override():
    """prepare-certificate-message should work offline with --current-block."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        utxo_key = _key(1)
        cert_key = _key(2)
        bond = FidelityBondInfo(
            address="bc1qtestpreparecert",
            locktime=1893456000,
            locktime_human="2030-01-01 00:00:00",
            index=0,
            path="external",
            pubkey=bytes(utxo_key.pub).hex(),
            witness_script_hex="aa" * 20,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey=bytes(cert_key.pub).hex(),
        )
        save_registry(BondRegistry(bonds=[bond]), data_dir, "deadbeef")

        result = runner.invoke(
            app,
            [
                "prepare-certificate-message",
                bond.address,
                "--data-dir",
                str(data_dir),
                "--wallet-fingerprint",
                "deadbeef",
                "--current-block",
                "850000",
            ],
        )

    assert result.exit_code == 0
    assert "Current Block:         850000" in result.stdout
    assert "MESSAGE TO SIGN" in result.stdout


@pytest.mark.parametrize("validity_periods", [0, -1])
def test_prepare_certificate_message_rejects_nonpositive_validity(
    validity_periods: int,
) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        result = runner.invoke(
            app,
            [
                "prepare-certificate-message",
                "bc1qinvalid",
                "--data-dir",
                str(data_dir),
                "--validity-periods",
                str(validity_periods),
                "--current-block",
                "850000",
            ],
        )

        assert result.exit_code == 1
        assert not (data_dir / "certificate_message.txt").exists()


@pytest.mark.parametrize(
    ("cert_expiry", "current_block", "expected_success"),
    [
        (600, 1000, True),
        (600, 600 * 2016, True),
        (600, 600 * 2016 + 1, False),
        (65535, 1000, True),
        (0, 1000, False),
        (-1, 1000, False),
        (65536, 1000, False),
    ],
)
def test_import_certificate_expiry_validation(cert_expiry, current_block, expected_success):
    """Import enforces the wire range and reference expiry boundary."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        utxo_key = _key(1)
        cert_key = _key(2)
        cert_pubkey_hex = bytes(cert_key.pub).hex()

        message = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}".encode()
        msg_hash = bitcoin_message_hash_bytes(message)
        compact_sig, recovery_id = utxo_key.sign_compact(msg_hash)
        electrum_sig = bytes([31 + recovery_id]) + compact_sig
        sig_b64 = base64.b64encode(electrum_sig).decode()

        bond = FidelityBondInfo(
            address="bc1qtestimportcert",
            locktime=1893456000,
            locktime_human="2030-01-01 00:00:00",
            index=0,
            path="external",
            pubkey=bytes(utxo_key.pub).hex(),
            witness_script_hex="bb" * 20,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            cert_pubkey=cert_pubkey_hex,
            cert_privkey=cert_key.secret_bytes.hex(),
        )
        save_registry(BondRegistry(bonds=[bond]), data_dir, "deadbeef")

        result = runner.invoke(
            app,
            [
                "import-certificate",
                bond.address,
                "--data-dir",
                str(data_dir),
                "--wallet-fingerprint",
                "deadbeef",
                "--cert-signature",
                sig_b64,
                "--cert-expiry",
                str(cert_expiry),
                "--current-block",
                str(current_block),
            ],
        )

        loaded = load_registry(data_dir, "deadbeef")
        loaded_bond = loaded.get_bond_by_address(bond.address)

    assert loaded_bond is not None
    if expected_success:
        assert result.exit_code == 0
        assert "CERTIFICATE IMPORTED SUCCESSFULLY" in result.stdout
        assert loaded_bond.cert_expiry == cert_expiry
        assert loaded_bond.cert_signature is not None
    else:
        assert result.exit_code == 1
        assert loaded_bond.cert_expiry is None
        assert loaded_bond.cert_signature is None
