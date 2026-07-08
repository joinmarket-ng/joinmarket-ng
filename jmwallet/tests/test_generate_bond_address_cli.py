"""CLI tests for ``jm-wallet generate-bond-address``.

The command derives a timelocked P2WSH fidelity bond address from the wallet
mnemonic and records it in the per-wallet bond registry. Funds sent to a
wrongly derived address would be unspendable, so the derivation is pinned to
a cross-implementation known-answer vector: the standard BIP39 test mnemonic
("abandon ... about") produces pubkey ``BOND_PUBKEY_2026_02`` at
``m/84'/0'/0'/2/73`` (locktime 2026-02) in the reference JoinMarket
implementation (see ``test_derive_bond_pubkey.py`` for the xpub-side vector).
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from click.testing import Result
from typer.testing import CliRunner

from jmwallet.cli import app

runner = CliRunner()

# Standard BIP39 test mnemonic (no passphrase).
BIP39_TEST_MNEMONIC = "abandon " * 11 + "about"

# Reference-implementation pubkey for the 2026-02 bond of the test mnemonic
# at m/84'/0'/0'/2/73 (mainnet). Must match test_derive_bond_pubkey.py.
BOND_PUBKEY_2026_02 = "03a30ac2cbcd6cafae59a6077893fe1aad0605efa7b98cd9c68cff754a13fe4d48"

# parse_locktime_date("2026-02") -> timenumber 73
LOCKTIME_2026_02 = 1769904000
TIMENUMBER_2026_02 = 73


def _write_mnemonic(tmp_path: Path) -> Path:
    mnemonic_file = tmp_path / "wallet.mnemonic"
    mnemonic_file.write_text(BIP39_TEST_MNEMONIC + "\n")
    return mnemonic_file


def _invoke(tmp_path: Path, *extra: str) -> Result:
    return runner.invoke(
        app,
        [
            "generate-bond-address",
            "--mnemonic-file",
            str(_write_mnemonic(tmp_path)),
            "--data-dir",
            str(tmp_path),
            *extra,
        ],
    )


def _registry_files(tmp_path: Path) -> list[Path]:
    return list(tmp_path.rglob("*.json"))


class TestGenerateBondAddressDerivation:
    def test_mainnet_known_answer_vector(self, tmp_path: Path) -> None:
        """Derivation must match the reference implementation for 2026-02."""
        result = _invoke(tmp_path, "--network", "mainnet", "--locktime-date", "2026-02")
        output = click.unstyle(result.stdout)

        assert result.exit_code == 0, output
        # The witness script embeds the derived pubkey; a mismatch here means
        # funds sent to the printed address would not be recoverable with the
        # reference tooling.
        assert BOND_PUBKEY_2026_02 in output
        assert f"Timenumber:   {TIMENUMBER_2026_02}" in output
        assert f"m/84'/0'/0'/2/{TIMENUMBER_2026_02}" in output
        assert f"Locktime:     {LOCKTIME_2026_02}" in output

        # The printed address must be the P2WSH of the freeze script built
        # from the reference pubkey and locktime.
        from jmcore.btc_script import mk_freeze_script

        from jmwallet.wallet.address import script_to_p2wsh_address

        expected_address = script_to_p2wsh_address(
            mk_freeze_script(BOND_PUBKEY_2026_02, LOCKTIME_2026_02), "mainnet"
        )
        assert expected_address in output

    def test_testnet_networks_use_coin_type_1(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "--network", "regtest", "--locktime-date", "2026-02")
        output = click.unstyle(result.stdout)

        assert result.exit_code == 0, output
        assert f"m/84'/1'/0'/2/{TIMENUMBER_2026_02}" in output
        # Regtest P2WSH addresses use the bcrt1 HRP.
        assert "bcrt1" in output


class TestGenerateBondAddressRegistry:
    def test_bond_is_saved_to_registry(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "--network", "mainnet", "--locktime-date", "2026-02")
        output = click.unstyle(result.stdout)

        assert result.exit_code == 0, output
        assert "Saved to registry" in output

        registry_files = _registry_files(tmp_path)
        assert registry_files, "expected a bond registry file to be created"
        payload = json.loads(registry_files[0].read_text())
        serialized = json.dumps(payload)
        assert BOND_PUBKEY_2026_02 in serialized

    def test_rerun_is_idempotent(self, tmp_path: Path) -> None:
        first = _invoke(tmp_path, "--network", "mainnet", "--locktime-date", "2026-02")
        assert first.exit_code == 0

        second = _invoke(tmp_path, "--network", "mainnet", "--locktime-date", "2026-02")
        output = click.unstyle(second.stdout)
        assert second.exit_code == 0, output
        assert "already in registry" in output

        # Still exactly one bond recorded.
        (registry_file,) = _registry_files(tmp_path)
        payload = json.loads(registry_file.read_text())
        assert len(payload.get("bonds", payload)) == 1

    def test_no_save_skips_registry(self, tmp_path: Path) -> None:
        result = _invoke(
            tmp_path, "--network", "mainnet", "--locktime-date", "2026-02", "--no-save"
        )
        output = click.unstyle(result.stdout)

        assert result.exit_code == 0, output
        assert "Not saved to registry" in output
        assert not _registry_files(tmp_path)


class TestGenerateBondAddressValidation:
    def test_missing_locktime_exits_nonzero(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "--network", "mainnet")
        assert result.exit_code == 1

    def test_invalid_locktime_date_exits_nonzero(self, tmp_path: Path) -> None:
        result = _invoke(tmp_path, "--network", "mainnet", "--locktime-date", "2026-13")
        assert result.exit_code == 1

    def test_mid_month_locktime_is_rejected(self, tmp_path: Path) -> None:
        # 2030-01-15 00:00 UTC: not a valid timenumber (must be the 1st).
        result = _invoke(tmp_path, "--network", "mainnet", "--locktime", "1894665600")
        assert result.exit_code == 1

    def test_missing_mnemonic_exits_nonzero(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "generate-bond-address",
                "--mnemonic-file",
                str(tmp_path / "does-not-exist.mnemonic"),
                "--data-dir",
                str(tmp_path),
                "--locktime-date",
                "2026-02",
            ],
        )
        assert result.exit_code == 1
