"""Tests for the ``--show-utxos`` basic wallet-info UTXO breakdown.

Covers the per-mixdepth, per-type grouping (cj-out, cj-change, deposit,
reg-change) plus the separate fidelity-bonds section, the lowercase row
state (``frozen`` / ``locked`` / ``redeemable`` / ``spendable``), ``N conf``
markers, address elision on continuation rows, and the ``none`` placeholder
for empty categories.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from jmwallet.cli import app

runner = CliRunner()


def _make_utxo(txid: str, vout: int, value: int, address: str, md: int, **kw) -> object:
    from jmwallet.wallet.models import UTXOInfo

    return UTXOInfo(
        txid=txid,
        vout=vout,
        value=value,
        address=address,
        confirmations=kw.pop("confirmations", 5),
        scriptpubkey=kw.pop("scriptpubkey", "0014" + "11" * 20),
        path=kw.pop("path", f"m/84'/0'/{md}'/0/0"),
        mixdepth=md,
        **kw,
    )


@pytest.fixture
def categorized_wallet():
    """Mock wallet with UTXOs spanning every category plus a fidelity bond."""
    cj_out = _make_utxo("11" * 32, 0, 27_350, "bc1qcjout0t0est00000000000000000000000000000", 1)
    deposit = _make_utxo(
        "33" * 32, 0, 9_812, "bc1qdep0s1t00000000000000000000000000000000", 0, frozen=True
    )
    noncj1 = _make_utxo(
        "44" * 32, 0, 11_000, "bc1qn0ncjch4nge0000000000000000000000000000", 3, frozen=True
    )
    # Same address as noncj1 -> exercises address elision on the continuation row.
    noncj2 = _make_utxo("44" * 32, 1, 24_309, "bc1qn0ncjch4nge0000000000000000000000000000", 3)
    # Fidelity bond: locktime set makes ``is_fidelity_bond`` True.
    fb = _make_utxo(
        "55" * 32,
        0,
        44_866,
        "bc1qf1d3l1tyb0nd00000000000000000000000000000",
        0,
        locktime=2_000_000_000,
    )

    label_map = {
        cj_out.address: "cj-out",
        deposit.address: "deposit",
        noncj1.address: "non-cj-change",
    }

    mock_wallet = MagicMock()
    mock_wallet.wallet_fingerprint = "a2dd0565"
    mock_wallet.network = "mainnet"
    mock_wallet.root_path = "m/84'/0'"
    mock_wallet.mixdepth_count = 5
    mock_wallet.data_dir = Path("/tmp")

    # cj-change intentionally left empty so the ``none`` placeholder is shown.
    mock_wallet.utxo_cache = {
        0: [deposit, fb],
        1: [cj_out],
        3: [noncj1, noncj2],
    }
    mock_wallet.address_cache = {
        cj_out.address: (1, 0, 0),
        deposit.address: (0, 0, 0),
        noncj1.address: (3, 0, 0),
        fb.address: (0, 0, 0),
    }
    mock_wallet.addresses_with_history = set()
    mock_wallet.get_utxo_label_from_wallet = lambda addr: label_map[addr]

    mock_wallet.sync_all = AsyncMock()
    mock_wallet.sync_with_registered_bonds = AsyncMock(return_value={})
    mock_wallet.is_descriptor_wallet_ready = AsyncMock(return_value=True)
    mock_wallet.sync_with_descriptor_wallet = AsyncMock(return_value=[])
    mock_wallet.close = AsyncMock()
    mock_wallet.get_total_balance = AsyncMock(return_value=112_357)
    mock_wallet.get_fidelity_bond_balance = AsyncMock(return_value=44_866)
    mock_wallet.get_balance = AsyncMock(return_value=67_491)
    mock_wallet.get_account_zpub = MagicMock(return_value="zpub" + "x" * 100)
    mock_wallet.get_address_info_for_mixdepth = MagicMock(return_value=[])
    mock_wallet.get_fidelity_bond_addresses_info = MagicMock(return_value=[])

    return mock_wallet, {
        "cj_out": cj_out,
        "deposit": deposit,
        "nancj1": noncj1,
        "nancj2": noncj2,
        "fb": fb,
    }


def test_categorized_utxos_sections_and_headers(categorized_wallet, capsys):
    """Every category header is printed, including empty ones as ``none``."""
    from jmwallet.cli import wallet as wallet_cli

    mock_wallet, _ = categorized_wallet

    with patch("sys.stdout.isatty", return_value=False):
        wallet_cli._print_categorized_utxos(mock_wallet)
        out = capsys.readouterr().out

    for header in (
        "cj-out UTXOs:",
        "cj-change UTXOs:",
        "deposit UTXOs:",
        "reg-change UTXOs:",
        "fidelity bonds:",
    ):
        assert header in out, f"Missing section header: {header}"

    # cj-change is empty -> single ``none`` line, not per-mixdepth.
    assert "  none" in out


def test_categorized_utxos_row_state_and_frozen(categorized_wallet, capsys):
    """Rows carry lowercase states (frozen/spendable/locked/redeemable)."""
    from jmwallet.cli import wallet as wallet_cli

    mock_wallet, _ = categorized_wallet

    with patch("sys.stdout.isatty", return_value=False):
        wallet_cli._print_categorized_utxos(mock_wallet)
        out = capsys.readouterr().out

    # Frozen UTXOs are marked with the lowercase state.
    assert "frozen" in out
    # Non-frozen UTXOs show spendable, and the locked bond shows locked.
    assert "spendable" in out
    assert "locked" in out
    # Confirmations rendered as 5+ conf for confirmed UTXOs.
    assert "5+ conf" in out
    # Row fields are separated by pipes, like the UTXO selector.
    assert "| 5+ conf |" in out


def test_categorized_utxos_expired_bond_is_redeemable(capsys):
    """An expired (non-locked) fidelity bond shows ``redeemable``, not ``spendable``."""
    from jmwallet.cli import wallet as wallet_cli

    # Locktime in the past: ``is_fidelity_bond`` True, ``is_locked`` False.
    fb = _make_utxo(
        "66" * 32,
        0,
        12_000,
        "bc1qex1red3b0nd0000000000000000000000000000000",
        0,
        locktime=1_000_000_000,
    )
    mock_wallet = MagicMock()
    mock_wallet.mixdepth_count = 1
    mock_wallet.utxo_cache = {0: [fb]}
    mock_wallet.address_cache = {fb.address: (0, 0, 0)}
    mock_wallet.get_utxo_label_from_wallet = lambda addr: "deposit"

    with patch("sys.stdout.isatty", return_value=False):
        wallet_cli._print_categorized_utxos(mock_wallet)
        out = capsys.readouterr().out

    fb_section = out.split("fidelity bonds:")[1]
    assert "redeemable" in fb_section
    assert "spendable" not in fb_section


def test_categorized_utxos_fidelity_bond_isolated(categorized_wallet, capsys):
    """Fidelity bond appears only in its own section, not in the 4 categories."""
    from jmwallet.cli import wallet as wallet_cli

    mock_wallet, utxos = categorized_wallet

    with patch("sys.stdout.isatty", return_value=False):
        wallet_cli._print_categorized_utxos(mock_wallet)
        out = capsys.readouterr().out

    # The bond value must be inside the fidelity-bonds section.
    bonds_section = out.split("fidelity bonds:")[1]
    assert f"{utxos['fb'].value:,}" in bonds_section

    # The bond address/value must NOT appear in the cj-out/deposit/non-cj sections.
    cj_out_section = out.split("cj-out UTXOs:")[1].split("cj-change UTXOs:")[0]
    deposit_section = out.split("deposit UTXOs:")[1].split("reg-change UTXOs:")[0]
    noncj_section = out.split("reg-change UTXOs:")[1].split("fidelity bonds:")[0]
    for section in (cj_out_section, deposit_section, noncj_section):
        assert utxos["fb"].address not in section
        assert f"{utxos['fb'].value:,}" not in section


def test_categorized_utxos_address_elision(categorized_wallet, capsys):
    """Continuation rows on the same address elide the address column."""
    from jmwallet.cli import wallet as wallet_cli

    mock_wallet, utxos = categorized_wallet

    with patch("sys.stdout.isatty", return_value=False):
        wallet_cli._print_categorized_utxos(mock_wallet)
        out = capsys.readouterr().out

    noncj_section = out.split("reg-change UTXOs:")[1].split("fidelity bonds:")[0]
    # First row shows the address, the continuation row (same address) elides it.
    lines = [line for line in noncj_section.splitlines() if line.strip()]
    addr_rows = [line for line in lines if utxos["nancj1"].address in line]
    assert len(addr_rows) == 1, f"Expected address on exactly one row: {addr_rows}"


def test_categorized_utxos_groups_same_address_and_sorts_by_path(capsys):
    """Same-address UTXOs are grouped (address printed once) and ordered by
    (mixdepth, branch, index) like the extended view, even when the wallet
    cache stores them out of path order."""
    from jmwallet.cli import wallet as wallet_cli

    # One address (external, path 0/0/0) holds two UTXOs; a different address
    # (internal branch, path 0/1/0) sits between them in cache order. Sorting
    # by derivation path must group the shared address and print it once.
    a = _make_utxo("aa" * 32, 0, 10_000, "bc1qaaa0000000000000000000000000000000000000", 0)
    a2 = _make_utxo("aa" * 32, 1, 30_000, "bc1qaaa0000000000000000000000000000000000000", 0)
    b = _make_utxo("bb" * 32, 0, 20_000, "bc1qbbb0000000000000000000000000000000000000", 0)

    mock_wallet = MagicMock()
    mock_wallet.mixdepth_count = 1
    # Out of path order: b (0/1/0) between a (0/0/0) and a2 (0/0/0).
    mock_wallet.utxo_cache = {0: [b, a, a2]}
    mock_wallet.address_cache = {
        a.address: (0, 0, 0),
        a2.address: (0, 0, 0),
        b.address: (0, 1, 0),
    }
    mock_wallet.get_utxo_label_from_wallet = lambda addr: "deposit"

    with patch("sys.stdout.isatty", return_value=False):
        wallet_cli._print_categorized_utxos(mock_wallet)
        out = capsys.readouterr().out

    deposit_section = out.split("deposit UTXOs:")[1].split("reg-change UTXOs:")[0]
    lines = [ln for ln in deposit_section.splitlines() if ln.strip()]

    # The shared address is printed on exactly one row (grouped together).
    addr_rows = [ln for ln in lines if a.address in ln]
    assert len(addr_rows) == 1, f"Expected grouped address once: {addr_rows}"

    # Ordering follows the derivation path: both a/a2 rows (external 0/0/0)
    # come before b (internal 0/1/0).
    outpoints = [ln.split("|")[-1].strip() for ln in lines]
    assert outpoints == [
        f"{'aa' * 32}:0",
        f"{'aa' * 32}:1",
        f"{'bb' * 32}:0",
    ], f"Unexpected row order: {outpoints}"


def test_show_utxos_flag_end_to_end(categorized_wallet):
    """The --show-utxos flag adds the breakdown; default keeps it hidden."""
    mock_wallet, _ = categorized_wallet

    with tempfile.TemporaryDirectory() as tmpdir:
        mnemonic_file = Path(tmpdir) / "test.mnemonic"
        mnemonic_file.write_text("abandon " * 11 + "about")

        with patch("jmwallet.wallet.service.WalletService", return_value=mock_wallet):
            # With the flag: sections present.
            result = runner.invoke(
                app,
                [
                    "info",
                    "--mnemonic-file",
                    str(mnemonic_file),
                    "--network",
                    "mainnet",
                    "--backend",
                    "descriptor_wallet",
                    "--show-utxos",
                ],
            )
            assert result.exit_code == 0, f"Command failed: {result.stdout}"
            for header in (
                "cj-out UTXOs:",
                "cj-change UTXOs:",
                "deposit UTXOs:",
                "reg-change UTXOs:",
                "fidelity bonds:",
            ):
                assert header in result.stdout, f"Missing header with flag: {header}"

            # Without the flag: breakdown hidden (default off, backward compatible).
            result = runner.invoke(
                app,
                [
                    "info",
                    "--mnemonic-file",
                    str(mnemonic_file),
                    "--network",
                    "mainnet",
                    "--backend",
                    "descriptor_wallet",
                ],
            )
            assert result.exit_code == 0, f"Command failed: {result.stdout}"
            assert "cj-out UTXOs:" not in result.stdout
            assert "fidelity bonds:" not in result.stdout


def test_categorized_utxos_ansi_via_isatty(categorized_wallet, capsys):
    """Patching ``isatty()`` to True enables ANSI codes in the breakdown.

    The TTY guard in ``_color_enabled`` is what switches colors on: section
    titles render bold cyan and the mixdepth labels render cyan, each wrapped
    in the matching reset sequence.
    """
    from jmwallet.cli import wallet as wallet_cli

    mock_wallet, _ = categorized_wallet

    with patch("sys.stdout.isatty", return_value=True):
        wallet_cli._print_categorized_utxos(mock_wallet)
        out = capsys.readouterr().out

    assert "\033[1;36m" in out, "Bold-cyan section title missing"
    assert "\033[0;36m" in out, "Cyan mixdepth label missing"
    assert "\033[0m" in out, "Reset sequence missing"


def test_show_utxos_headers_colored_on_tty(categorized_wallet):
    """On a TTY the info command colors the yellow header and cyan MD labels."""
    from jmwallet.cli import wallet as wallet_cli

    mock_wallet, _ = categorized_wallet

    with tempfile.TemporaryDirectory() as tmpdir:
        mnemonic_file = Path(tmpdir) / "test.mnemonic"
        mnemonic_file.write_text("abandon " * 11 + "about")

        with patch("jmwallet.wallet.service.WalletService", return_value=mock_wallet):
            # Force the TTY code path regardless of the CliRunner's captured
            # (non-tty) stdout, which swaps ``sys.stdout`` during invoke.
            with patch.object(wallet_cli, "_color_enabled", return_value=True):
                result = runner.invoke(
                    app,
                    [
                        "info",
                        "--mnemonic-file",
                        str(mnemonic_file),
                        "--network",
                        "mainnet",
                        "--backend",
                        "descriptor_wallet",
                        "--show-utxos",
                    ],
                )
            assert result.exit_code == 0, f"Command failed: {result.stdout}"
            # Bold-yellow headers, e.g. ``Total Wallet Balance:``.
            assert "\033[1;33m" in result.stdout, "Bold-yellow header missing"
            # Cyan mixdepth labels in the breakdown rows.
            assert "\033[0;36m" in result.stdout, "Cyan mixdepth label missing"
            # Reset sequences follow every colored span.
            assert "\033[0m" in result.stdout, "Reset sequence missing"
            # Plain text is still present underneath the ANSI codes.
            assert "Total Wallet Balance:" in result.stdout
            assert "Spendable Balance by Mixdepth:" in result.stdout
