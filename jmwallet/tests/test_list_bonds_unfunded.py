"""``list-bonds`` must surface registered-but-unfunded fidelity bonds.

``list-bonds`` is registry-only (offline); it never scans the blockchain
(use ``recover-bonds`` for that). Bonds created with ``generate-bond-address``
/ ``import-bond`` but not yet funded live in the per-wallet registry and must
be shown with an ``UNFUNDED`` status.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jmwallet.cli.bonds import _list_bonds_offline
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    BondUtxo,
    FidelityBondInfo,
    save_registry,
)

UNFUNDED_ADDRESS = "bcrt1qunfundedbond00000000000000000000000000xyz"
FINGERPRINT = "deadbeef"


def _seed_unfunded_registry(data_dir: Path) -> None:
    registry = BondRegistry()
    registry.add_bond(
        FidelityBondInfo(
            address=UNFUNDED_ADDRESS,
            locktime=1893456000,
            locktime_human="2030-01-01 00:00:00",
            index=0,
            path="m/84'/1'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="regtest",
            created_at="2025-01-01T00:00:00",
        )
    )
    save_registry(registry, data_dir, FINGERPRINT)


def test_list_bonds_offline_shows_unfunded_registered_bond(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_unfunded_registry(tmp_path)

    _list_bonds_offline(data_dir=tmp_path, fingerprint=FINGERPRINT)

    out = capsys.readouterr().out
    assert UNFUNDED_ADDRESS in out
    assert "UNFUNDED" in out


def test_list_bonds_offline_shows_expired_unfunded_bond_without_stale_value(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = BondRegistry()
    bond = FidelityBondInfo(
        address=UNFUNDED_ADDRESS,
        locktime=1577836800,
        locktime_human="2020-01-01 00:00:00",
        index=0,
        path="m/84'/1'/0'/2/0",
        pubkey="02" + "00" * 32,
        witness_script_hex="00" * 50,
        network="regtest",
        created_at="2025-01-01T00:00:00",
        txid="aa" * 32,
        vout=0,
        value=123_456,
        confirmations=5,
        extra_utxos=[BondUtxo(txid="bb" * 32, vout=1, value=10_000, confirmations=4)],
    )
    registry.add_bond(bond)
    registry.set_bond_utxos(UNFUNDED_ADDRESS, [])
    save_registry(registry, tmp_path, FINGERPRINT)

    _list_bonds_offline(data_dir=tmp_path, fingerprint=FINGERPRINT)

    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if UNFUNDED_ADDRESS in line)
    assert "EXPIRED" in row
    assert "(funded)" not in row
    assert "123,456 sats" not in out
    assert "10,000 sats" not in out
    assert row.split()[-2] == "-"


FUNDED_MULTI_ADDRESS = "bcrt1qmultibond000000000000000000000000000000xyz"


def _seed_multi_utxo_registry(data_dir: Path) -> None:
    """A funded bond whose address holds a second, smaller locked UTXO."""
    registry = BondRegistry()
    bond = FidelityBondInfo(
        address=FUNDED_MULTI_ADDRESS,
        locktime=1893456000,
        locktime_human="2030-01-01 00:00:00",
        index=0,
        path="m/84'/1'/0'/2/0",
        pubkey="02" + "00" * 32,
        witness_script_hex="00" * 50,
        network="regtest",
        created_at="2025-01-01T00:00:00",
        txid="aa" * 32,
        vout=0,
        value=20_000,
        confirmations=5,
        extra_utxos=[BondUtxo(txid="bb" * 32, vout=1, value=10_000, confirmations=5)],
    )
    registry.add_bond(bond)
    save_registry(registry, data_dir, FINGERPRINT)


def test_list_bonds_offline_shows_extra_locked_utxos(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bond address with more than one UTXO must surface the extra locked
    UTXO(s) and the total locked amount, so the coins are not invisible in the
    offline view (they show in ``info --extended`` but were missing here)."""
    _seed_multi_utxo_registry(tmp_path)

    _list_bonds_offline(data_dir=tmp_path, fingerprint=FINGERPRINT)

    out = capsys.readouterr().out
    assert FUNDED_MULTI_ADDRESS in out
    # Announced bond value (largest UTXO).
    assert "20,000 sats" in out
    # The extra locked UTXO and the combined total are both surfaced.
    assert "10,000 sats" in out
    assert "30,000 sats locked total" in out


def test_bond_json_output_omits_cert_privkey(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    from typer.testing import CliRunner

    from jmwallet.cli import app

    registry = BondRegistry()
    registry.add_bond(
        FidelityBondInfo(
            address=UNFUNDED_ADDRESS,
            locktime=1893456000,
            locktime_human="2030-01-01 00:00:00",
            index=0,
            path="m/84'/1'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="regtest",
            created_at="2025-01-01T00:00:00",
            cert_pubkey="03" + "11" * 32,
            cert_privkey="22" * 32,
        )
    )
    save_registry(registry, tmp_path, FINGERPRINT)

    _list_bonds_offline(data_dir=tmp_path, fingerprint=FINGERPRINT, json_output=True)
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["cert_pubkey"] == "03" + "11" * 32
    assert "cert_privkey" not in listed[0]

    result = CliRunner().invoke(
        app,
        [
            "registry-show",
            UNFUNDED_ADDRESS,
            "--data-dir",
            str(tmp_path),
            "--wallet-fingerprint",
            FINGERPRINT,
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    shown = json.loads(result.stdout[result.stdout.find("{") :])
    assert shown["cert_pubkey"] == "03" + "11" * 32
    assert "cert_privkey" not in shown
    assert "22" * 32 not in result.stdout
