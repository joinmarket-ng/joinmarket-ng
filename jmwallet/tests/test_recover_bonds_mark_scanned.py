"""Offline CLI confirmation of previously completed fidelity bond recovery."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from jmcore.settings import reset_settings
from typer.testing import CliRunner

from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
from jmwallet.cli import app
from jmwallet.cli.mnemonic import (
    FidelityBondRecoveryInProgressError,
    load_mnemonic_meta,
    mnemonic_requires_fidelity_bond_recovery,
    save_mnemonic_meta,
)

MNEMONIC = "abandon " * 11 + "about"
runner = CliRunner()


@pytest.fixture
def wallet_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    for name in (
        "MNEMONIC",
        "MNEMONIC_FILE",
        "MNEMONIC_PASSWORD",
        "BIP39_PASSPHRASE",
        "JOINMARKET_CONFIG_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
    reset_settings()
    source = tmp_path / "wallet.mnemonic"
    source.write_text(MNEMONIC)
    with patch(
        "jmwallet.cli.bonds.resolve_backend_settings",
        side_effect=AssertionError("Marking scanned must not access a backend"),
    ):
        yield source
    reset_settings()


@pytest.mark.parametrize("passphrase", ["", "test-passphrase"])
@pytest.mark.parametrize("initial_state", [None, "pending", "not_required", "complete"])
def test_mark_scanned_records_only_selected_wallet_without_scanning(
    wallet_file: Path, passphrase: str, initial_state: str | None
) -> None:
    save_mnemonic_meta(
        wallet_file,
        creation_height=123,
        fingerprint="deadbeef",
        fidelity_bond_recovery=initial_state,
    )
    before = load_mnemonic_meta(wallet_file)
    fingerprint = get_mnemonic_fingerprint(MNEMONIC, passphrase)
    result = runner.invoke(
        app,
        ["recover-bonds", "--mark-scanned", "--mnemonic-file", str(wallet_file)],
        input="y\n",
        env={"BIP39_PASSPHRASE": passphrase},
    )
    assert result.exit_code == 0, result.output
    assert str(wallet_file) in result.output
    assert fingerprint in result.output
    assert "does not prove all 960 bond addresses were scanned" in result.output
    assert "No scan was performed" in result.output
    assert load_mnemonic_meta(wallet_file) == {
        **before,
        f"fidelity_bond_recovery.{fingerprint}": "complete",
    }
    assert not mnemonic_requires_fidelity_bond_recovery(wallet_file, fingerprint)
    if passphrase and initial_state == "pending":
        assert mnemonic_requires_fidelity_bond_recovery(
            wallet_file, get_mnemonic_fingerprint(MNEMONIC)
        )


@pytest.mark.parametrize("answer", ["n\n", "\n", ""])
def test_mark_scanned_requires_affirmative_confirmation(wallet_file: Path, answer: str) -> None:
    result = runner.invoke(
        app,
        ["recover-bonds", "--mark-scanned", "--mnemonic-file", str(wallet_file)],
        input=answer,
    )
    assert result.exit_code != 0
    assert load_mnemonic_meta(wallet_file) == {}
    assert not wallet_file.with_suffix(".mnemonic.meta").exists()


def test_mark_scanned_requires_file_backed_mnemonic(wallet_file: Path) -> None:
    result = runner.invoke(
        app,
        ["recover-bonds", "--mark-scanned"],
        env={"MNEMONIC": MNEMONIC},
        input="y\n",
    )
    assert result.exit_code == 1
    assert "requires a mnemonic file" in result.output
    assert load_mnemonic_meta(wallet_file) == {}


def test_mark_scanned_creates_missing_sidecar_for_configured_wallet(wallet_file: Path) -> None:
    fingerprint = get_mnemonic_fingerprint(MNEMONIC)
    for _ in range(2):
        result = runner.invoke(
            app,
            ["recover-bonds", "--mark-scanned"],
            env={"MNEMONIC_FILE": str(wallet_file)},
            input="y\n",
        )
        assert result.exit_code == 0, result.output
        assert load_mnemonic_meta(wallet_file) == {
            f"fidelity_bond_recovery.{fingerprint}": "complete"
        }
    assert wallet_file.with_suffix(".mnemonic.meta").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "error",
    [OSError("disk full"), FidelityBondRecoveryInProgressError("Recovery is already running")],
)
def test_mark_scanned_reports_failed_write(wallet_file: Path, error: Exception) -> None:
    with patch(
        "jmwallet.cli.mnemonic.acknowledge_fidelity_bond_recovery_scanned", side_effect=error
    ):
        result = runner.invoke(
            app,
            ["recover-bonds", "--mark-scanned", "--mnemonic-file", str(wallet_file)],
            input="y\n",
        )
    assert result.exit_code == 1
    assert str(error) in result.output
    assert "No scan was performed" not in result.output
    assert load_mnemonic_meta(wallet_file) == {}
