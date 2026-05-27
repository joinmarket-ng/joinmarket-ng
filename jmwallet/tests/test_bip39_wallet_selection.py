"""
Tests for BIP39 passphrase / wallet-fingerprint selection in offline
``jm-wallet`` commands.

Covers the user-visible bug where ``jm-wallet history`` had no
``--prompt-bip39-passphrase`` flag (so a passphrase-protected wallet
appeared to have no history) and where ``jm-wallet list-bonds`` /
``jm-wallet registry-show`` could silently target the wrong per-wallet
registry when the BIP39 passphrase was omitted.

The tests deliberately exercise the CLI surface (typer + click) rather
than the underlying helpers so they catch regressions in argument
wiring, error messages, and the new auto-detect fallback.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.history import append_history_entry, create_taker_history_entry
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    save_registry,
)


@pytest.fixture(autouse=True)
def _isolate_user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point JoinMarket settings at a clean, empty data directory so the
    real user's ``~/.joinmarket-ng/config.toml`` (which often configures
    a wallet mnemonic) cannot leak into these tests and short-circuit
    auto-detection of the on-disk wallet identity."""
    fresh = tmp_path / "jm-home"
    fresh.mkdir()
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(fresh))
    monkeypatch.delenv("MNEMONIC", raising=False)
    monkeypatch.delenv("MNEMONIC_FILE", raising=False)
    monkeypatch.delenv("BIP39_PASSPHRASE", raising=False)
    return fresh


runner = CliRunner()

# 24-word BIP39 mnemonic reused across tests. Fixed so the derived
# fingerprints (and therefore on-disk filenames) are deterministic.
_MNEMONIC = (
    "actress inmate filter october eagle floor conduct issue rail nominee mixture kid "
    "tunnel thought list tower lobster route ghost cigar bundle oak fiscal pulse"
)
_PASSPHRASE = "test"


def _fingerprint_for(passphrase: str) -> str:
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

    return get_mnemonic_fingerprint(_MNEMONIC, passphrase)


def _seed_history(data_dir: Path, fingerprint: str, txid_seed: str) -> None:
    entry = create_taker_history_entry(
        maker_nicks=["J5maker"],
        cj_amount=100_000,
        total_maker_fees=500,
        mining_fee=100,
        destination="bc1qdest...",
        change_address="bc1qchange...",
        source_mixdepth=0,
        selected_utxos=[("utxo", 0)],
        txid=txid_seed * 64,
        success=True,
        wallet_fingerprint=fingerprint,
    )
    entry.confirmations = 3
    entry.failure_reason = ""
    append_history_entry(entry, data_dir)


def _seed_bond_registry(data_dir: Path, fingerprint: str, address: str) -> None:
    """Persist a minimal bond entry under ``fidelity_bonds_<fp>.json``."""
    registry = BondRegistry()
    registry.add_bond(
        FidelityBondInfo(
            address=address,
            locktime=1893456000,
            locktime_human="2030-01-01",
            index=0,
            path="m/84h/1h/0h/3/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00",
            network="regtest",
            created_at="2025-01-01T00:00:00Z",
        )
    )
    save_registry(registry, data_dir, fingerprint)


# ---------------------------------------------------------------------------
# history --prompt-bip39-passphrase
# ---------------------------------------------------------------------------


def test_history_accepts_prompt_bip39_passphrase() -> None:
    """``jm-wallet history`` exposes ``--prompt-bip39-passphrase`` and uses
    the prompted passphrase to derive the wallet fingerprint, so history
    written by the passphrase-protected wallet is visible."""
    fingerprint = _fingerprint_for(_PASSPHRASE)
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_history(data_dir, fingerprint, "a")
        mnemonic_file = data_dir / "wallet.mnemonic"
        mnemonic_file.write_text(_MNEMONIC)

        with patch.object(typer, "prompt", return_value=_PASSPHRASE):
            result = runner.invoke(
                app,
                [
                    "history",
                    "--data-dir",
                    str(data_dir),
                    "--mnemonic-file",
                    str(mnemonic_file),
                    "--prompt-bip39-passphrase",
                ],
            )

        assert result.exit_code == 0, result.stdout
        assert "a" * 16 in result.stdout, (
            "History row written under the passphrase fingerprint should be "
            "visible when --prompt-bip39-passphrase is supplied"
        )


def test_history_without_passphrase_misses_passphrase_entries() -> None:
    """Without ``--prompt-bip39-passphrase``, a passphrase-protected wallet
    derives a different fingerprint than the one used to write its history
    and therefore the rendered table is empty. This is exactly the user-
    reported symptom; the test pins the behavior so we never regress."""
    fingerprint = _fingerprint_for(_PASSPHRASE)
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_history(data_dir, fingerprint, "a")
        mnemonic_file = data_dir / "wallet.mnemonic"
        mnemonic_file.write_text(_MNEMONIC)

        # No --prompt-bip39-passphrase, no env, no config. The derived
        # fingerprint will be the no-passphrase one, which doesn't match.
        result = runner.invoke(
            app,
            [
                "history",
                "--data-dir",
                str(data_dir),
                "--mnemonic-file",
                str(mnemonic_file),
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "No CoinJoin history found." in result.stdout


# ---------------------------------------------------------------------------
# Auto-detect single wallet
# ---------------------------------------------------------------------------


def test_history_auto_detects_single_wallet() -> None:
    """When exactly one wallet has written history, ``jm-wallet history``
    selects it without requiring ``--mnemonic-file``."""
    fingerprint = _fingerprint_for("")
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_history(data_dir, fingerprint, "a")

        result = runner.invoke(
            app,
            ["history", "--data-dir", str(data_dir), "--log-level", "INFO"],
        )

        assert result.exit_code == 0, result.stdout
        assert "a" * 16 in result.stdout


def test_history_multiple_wallets_requires_disambiguation() -> None:
    """When several wallets have written, the command must abort with an
    error listing the known fingerprints rather than silently picking one
    or returning empty results."""
    fp_a = _fingerprint_for("")
    fp_b = _fingerprint_for(_PASSPHRASE)
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_history(data_dir, fp_a, "a")
        _seed_history(data_dir, fp_b, "b")

        result = runner.invoke(
            app,
            ["history", "--data-dir", str(data_dir)],
        )

        assert result.exit_code == 1
        combined = result.output
        assert fp_a in combined
        assert fp_b in combined
        assert "multiple wallets" in combined.lower()


def test_history_wallet_fingerprint_option() -> None:
    """``--wallet-fingerprint`` selects the wallet without needing the
    mnemonic, even when several wallets are present."""
    fp_a = _fingerprint_for("")
    fp_b = _fingerprint_for(_PASSPHRASE)
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_history(data_dir, fp_a, "a")
        _seed_history(data_dir, fp_b, "b")

        result = runner.invoke(
            app,
            [
                "history",
                "--data-dir",
                str(data_dir),
                "--wallet-fingerprint",
                fp_b,
            ],
        )

        assert result.exit_code == 0, result.stdout
        assert "b" * 16 in result.stdout
        assert "a" * 16 not in result.stdout


def test_history_all_wallets_disables_filter() -> None:
    """``--all-wallets`` short-circuits identity resolution and includes
    every recorded fingerprint (including legacy untagged rows). The
    flag must not trigger the multi-wallet error path."""
    fp_a = _fingerprint_for("")
    fp_b = _fingerprint_for(_PASSPHRASE)
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_history(data_dir, fp_a, "a")
        _seed_history(data_dir, fp_b, "b")

        result = runner.invoke(
            app,
            ["history", "--data-dir", str(data_dir), "--all-wallets"],
        )

        assert result.exit_code == 0, result.stdout
        assert "a" * 16 in result.stdout
        assert "b" * 16 in result.stdout


def test_history_invalid_wallet_fingerprint_rejected() -> None:
    """A malformed ``--wallet-fingerprint`` value must be rejected with a
    clear validation error, never silently fall through to auto-detect."""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = runner.invoke(
            app,
            [
                "history",
                "--data-dir",
                tmpdir,
                "--wallet-fingerprint",
                "not-hex!",
            ],
        )

        assert result.exit_code == 1
        assert "8 hex chars" in result.output or "valid hex" in result.output


# ---------------------------------------------------------------------------
# list-bonds offline
# ---------------------------------------------------------------------------


def test_list_bonds_offline_auto_detects_single_wallet() -> None:
    """When the data dir contains a single ``fidelity_bonds_<fp>.json``
    file, ``list-bonds`` (offline) picks it without a mnemonic."""
    fingerprint = _fingerprint_for("")
    address = "bcrt1qexampleaddressbond0000000000000000000xyz"
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_bond_registry(data_dir, fingerprint, address)

        result = runner.invoke(
            app,
            [
                "list-bonds",
                "--data-dir",
                str(data_dir),
                "--json",
                "--log-level",
                "INFO",
            ],
        )

        assert result.exit_code == 0, result.stdout
        # JSON output goes to stdout; locate the array containing the bond.
        # Output may be prefixed with log lines; isolate the JSON.
        json_start = result.stdout.find("[")
        bonds = json.loads(result.stdout[json_start:])
        assert any(b["address"] == address for b in bonds)


def test_list_bonds_offline_multi_wallet_requires_disambiguation() -> None:
    """Multiple per-wallet registries → error with the fingerprint list."""
    fp_a = _fingerprint_for("")
    fp_b = _fingerprint_for(_PASSPHRASE)
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_bond_registry(data_dir, fp_a, "bcrt1qaddra")
        _seed_bond_registry(data_dir, fp_b, "bcrt1qaddrb")

        result = runner.invoke(
            app,
            ["list-bonds", "--data-dir", str(data_dir)],
        )

        assert result.exit_code == 1
        assert fp_a in result.output
        assert fp_b in result.output


def test_list_bonds_offline_with_wallet_fingerprint() -> None:
    """``--wallet-fingerprint`` selects the registry directly even when
    multiple are present, and does NOT require the mnemonic."""
    fp_a = _fingerprint_for("")
    fp_b = _fingerprint_for(_PASSPHRASE)
    addr_b = "bcrt1qaddrb_for_test_passphrase_wallet000000"
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_bond_registry(data_dir, fp_a, "bcrt1qaddra")
        _seed_bond_registry(data_dir, fp_b, addr_b)

        result = runner.invoke(
            app,
            [
                "list-bonds",
                "--data-dir",
                str(data_dir),
                "--wallet-fingerprint",
                fp_b,
                "--json",
            ],
        )

        assert result.exit_code == 0, result.stdout
        json_start = result.stdout.find("[")
        bonds = json.loads(result.stdout[json_start:])
        addrs = [b["address"] for b in bonds]
        assert addr_b in addrs
        assert "bcrt1qaddra" not in addrs


def test_list_bonds_offline_passphrase_wallet_via_wallet_fingerprint() -> None:
    """A BIP39 passphrase-protected wallet has its own
    ``fidelity_bonds_<fp>.json`` file. The user can read it offline
    without typing the passphrase by passing ``--wallet-fingerprint``
    (which they learn from ``jm-wallet info``). This is the documented
    answer to the user-reported bug where ``list-bonds`` silently
    returned no bonds because the no-passphrase fingerprint was used."""
    fp_pass = _fingerprint_for(_PASSPHRASE)
    addr = "bcrt1qaddr_passphrase_wallet_offline_listbonds"
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        # Seed registries for BOTH the no-passphrase and the passphrase
        # wallet to make sure the wrong one isn't picked accidentally.
        _seed_bond_registry(data_dir, _fingerprint_for(""), "bcrt1qwrong")
        _seed_bond_registry(data_dir, fp_pass, addr)

        result = runner.invoke(
            app,
            [
                "list-bonds",
                "--data-dir",
                str(data_dir),
                "--wallet-fingerprint",
                fp_pass,
                "--json",
            ],
        )

        assert result.exit_code == 0, result.stdout
        json_start = result.stdout.find("[")
        bonds = json.loads(result.stdout[json_start:])
        addrs = [b["address"] for b in bonds]
        assert addr in addrs
        assert "bcrt1qwrong" not in addrs


# ---------------------------------------------------------------------------
# registry-show
# ---------------------------------------------------------------------------


def test_registry_show_auto_detects_single_wallet() -> None:
    """``registry-show`` should pick the only available registry when no
    wallet identity is provided."""
    fingerprint = _fingerprint_for("")
    address = "bcrt1qregistryshowautosinglewallet"
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_bond_registry(data_dir, fingerprint, address)

        result = runner.invoke(
            app,
            [
                "registry-show",
                address,
                "--data-dir",
                str(data_dir),
                "--json",
                "--log-level",
                "INFO",
            ],
        )

        assert result.exit_code == 0, result.stdout
        json_start = result.stdout.find("{")
        bond = json.loads(result.stdout[json_start:])
        assert bond["address"] == address


def test_registry_show_with_wallet_fingerprint() -> None:
    """``--wallet-fingerprint`` is sufficient to select the registry."""
    fingerprint = _fingerprint_for(_PASSPHRASE)
    address = "bcrt1qregistryshowwithfpflag"
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_bond_registry(data_dir, _fingerprint_for(""), "bcrt1qother")
        _seed_bond_registry(data_dir, fingerprint, address)

        result = runner.invoke(
            app,
            [
                "registry-show",
                address,
                "--data-dir",
                str(data_dir),
                "--wallet-fingerprint",
                fingerprint,
                "--json",
            ],
        )

        assert result.exit_code == 0, result.stdout
        json_start = result.stdout.find("{")
        bond = json.loads(result.stdout[json_start:])
        assert bond["address"] == address
