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
from loguru import logger as loguru_logger
from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.cli._wallet_selection import validate_fingerprint
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


def test_invalid_fingerprint_detail_log_is_sensitive() -> None:
    records: list[tuple[str, dict[str, object]]] = []
    sink_id = loguru_logger.add(
        lambda message: records.append((message.record["message"], dict(message.record["extra"])))
    )
    try:
        with pytest.raises(typer.Exit):
            validate_fingerprint("not-hex!")
    finally:
        loguru_logger.remove(sink_id)

    detail_records = [record for record in records if "not-hex!" in record[0]]
    assert detail_records
    assert all(extra.get("sensitive") is True for _, extra in detail_records)


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
                input="y\n",
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
    actionable guidance rather than silently picking one or returning empty
    results. Known fingerprints remain in sensitive log metadata."""
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
        assert fp_a not in combined
        assert fp_b not in combined
        assert "multiple wallets" in combined.lower()
        assert "--wallet-fingerprint <fp>" in combined


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
# Active-wallet scoping (issue #523)
# ---------------------------------------------------------------------------


def _make_active_default_wallet(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> Path:
    """Point settings at ``data_dir`` and return its default wallet path.

    The configured/active wallet is resolved from the default wallet
    location (``<data_dir>/wallets/default.mnemonic``), mirroring how the
    TUI and CLI select the active wallet, rather than ``--mnemonic-file``.
    """
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(data_dir))
    return data_dir / "wallets" / "default.mnemonic"


def test_history_scopes_to_configured_wallet_via_meta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #523: a freshly created (configured) wallet must show only its
    own history, not CoinJoins recorded by a previously active wallet. The
    companion ``.meta`` fingerprint keeps the scoping passwordless."""
    from jmwallet.cli.mnemonic import save_mnemonic_meta

    fp_other = _fingerprint_for("")  # wallet A that ran the CoinJoins
    fp_active = "11223344"  # the new, currently active wallet B
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_history(data_dir, fp_other, "a")

        # Active wallet B: the default wallet, whose .meta records its identity.
        active = _make_active_default_wallet(monkeypatch, data_dir)
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text(_MNEMONIC)
        save_mnemonic_meta(active, fingerprint=fp_active)

        result = runner.invoke(app, ["history", "--data-dir", str(data_dir)])

        assert result.exit_code == 0, result.stdout
        assert "No CoinJoin history found." in result.stdout
        assert "a" * 16 not in result.stdout  # wallet A's row must NOT leak
        assert "--all-wallets" in result.stdout  # hidden-rows notice present


def test_history_scopes_passwordless_for_encrypted_active_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the ``.meta`` fingerprint present, scoping the active wallet's
    history never decrypts the mnemonic, so an encrypted wallet with no
    configured password resolves without prompting or failing."""
    from jmwallet.cli.mnemonic import save_mnemonic_file, save_mnemonic_meta

    fp_other = _fingerprint_for("")
    fp_active = "11223344"
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_history(data_dir, fp_other, "a")

        active = _make_active_default_wallet(monkeypatch, data_dir)
        save_mnemonic_file(_MNEMONIC, active, password="s3cret")
        save_mnemonic_meta(active, fingerprint=fp_active)

        result = runner.invoke(app, ["history", "--data-dir", str(data_dir)])

        assert result.exit_code == 0, result.stdout
        assert "No CoinJoin history found." in result.stdout
        assert "Decryption failed" not in result.output
        assert "a" * 16 not in result.stdout


def test_history_backfills_meta_fingerprint_from_configured_mnemonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy active wallet without a ``.meta`` fingerprint is resolved by
    decrypting the configured mnemonic once; the derived identity is then
    cached so subsequent reads are passwordless."""
    from jmwallet.cli.mnemonic import load_mnemonic_meta_fingerprint

    fp_active = _fingerprint_for("")
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        # History recorded under a different (old) wallet only.
        _seed_history(data_dir, "deadbeef", "a")

        active = _make_active_default_wallet(monkeypatch, data_dir)
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text(_MNEMONIC)  # plaintext: no password needed to derive

        result = runner.invoke(app, ["history", "--data-dir", str(data_dir)])

        assert result.exit_code == 0, result.stdout
        assert "No CoinJoin history found." in result.stdout
        # The fingerprint was derived and cached into the .meta sidecar.
        assert load_mnemonic_meta_fingerprint(active) == fp_active


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


def test_list_bonds_offline_uses_configured_mnemonic_when_no_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list-bonds`` must work for the configured wallet without flags,
    mirroring ``jm-wallet info`` (which resolves the mnemonic from config /
    env). Previously it errored when no per-wallet registry file existed yet,
    even though the wallet identity was determinable from config."""
    monkeypatch.setenv("MNEMONIC", _MNEMONIC)
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)  # no registry files at all

        result = runner.invoke(
            app,
            ["list-bonds", "--data-dir", str(data_dir)],
        )

        assert result.exit_code == 0, result.output
        assert "No fidelity bonds found in registry" in result.output


def test_list_bonds_offline_uses_configured_mnemonic_to_show_bond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With a configured mnemonic, ``list-bonds`` (offline) selects that
    wallet's registry without needing --mnemonic-file/--wallet-fingerprint."""
    monkeypatch.setenv("MNEMONIC", _MNEMONIC)
    fingerprint = _fingerprint_for("")
    address = "bcrt1qconfiguredwalletbond000000000000000000xyz"
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)
        _seed_bond_registry(data_dir, fingerprint, address)

        result = runner.invoke(
            app,
            ["list-bonds", "--data-dir", str(data_dir), "--json"],
        )

        assert result.exit_code == 0, result.output
        json_start = result.stdout.find("[")
        bonds = json.loads(result.stdout[json_start:])
        assert any(b["address"] == address for b in bonds)


def test_list_bonds_offline_multi_wallet_requires_disambiguation() -> None:
    """Multiple per-wallet registries produce guidance without exposing fingerprints."""
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
        assert fp_a not in result.output
        assert fp_b not in result.output
        assert "--wallet-fingerprint <fp>" in result.output


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
