"""Test individual UTXO display in extended wallet info."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from jmwallet.cli import app

runner = CliRunner()


@pytest.fixture
def wallet_with_multi_utxo():
    """Create a wallet mock with multiple UTXOs on same address."""
    from jmwallet.wallet.models import UTXOInfo

    # Two UTXOs on the same address
    utxo1 = UTXOInfo(
        txid="a" * 64,
        vout=0,
        value=100_000,
        address="bc1qtestaddressreuse0000000000000000000000000",
        confirmations=3,
        scriptpubkey="0014" + "11" * 20,
        path="m/84'/0'/0'/0/0",
        mixdepth=0,
    )

    utxo2 = UTXOInfo(
        txid="b" * 64,
        vout=1,
        value=150_000,
        address="bc1qtestaddressreuse0000000000000000000000000",
        confirmations=5,
        scriptpubkey="0014" + "11" * 20,
        path="m/84'/0'/0'/0/0",
        mixdepth=0,
    )

    # Create mock wallet
    mock_wallet = MagicMock()
    mock_wallet.wallet_fingerprint = "5525def6"
    mock_wallet.network = "mainnet"
    mock_wallet.root_path = "m/84'/0'"
    mock_wallet.mixdepth_count = 5
    mock_wallet.data_dir = Path("/tmp")

    # UTXO cache - key is mixdepth
    mock_wallet.utxo_cache = {0: [utxo1, utxo2]}

    # Address cache - maps address to (mixdepth, change, index)
    mock_wallet.address_cache = {
        "bc1qtestaddressreuse0000000000000000000000000": (0, 0, 0),
    }

    # Empty sets for history
    mock_wallet.addresses_with_history = set()

    # Mock methods
    mock_wallet.sync_all = AsyncMock()
    mock_wallet.sync_with_registered_bonds = AsyncMock(return_value={})
    mock_wallet.is_descriptor_wallet_ready = AsyncMock(return_value=True)
    mock_wallet.sync_with_descriptor_wallet = AsyncMock(return_value=[])
    mock_wallet.close = AsyncMock()
    mock_wallet.get_total_balance = AsyncMock(return_value=250_000)
    mock_wallet.get_fidelity_bond_balance = AsyncMock(return_value=0)
    mock_wallet.get_balance = AsyncMock(return_value=250_000)
    mock_wallet.get_account_zpub = MagicMock(return_value="zpub" + "x" * 100)

    # get_address_info_for_mixdepth needs to return AddressInfo with utxos
    def mock_get_address_info(md, change, gap, used_addrs=None, hist_addrs=None):
        from jmwallet.wallet.models import AddressInfo

        if md == 0 and change == 0:
            # External addresses in mixdepth 0
            return [
                AddressInfo(
                    address="bc1qtestaddressreuse0000000000000000000000000",
                    index=0,
                    balance=250_000,
                    status="deposit",
                    path="m/84'/0'/0'/0/0",
                    is_external=True,
                    has_unconfirmed=False,
                    utxos=[utxo1, utxo2],  # Both UTXOs here!
                )
            ]
        return []

    mock_wallet.get_address_info_for_mixdepth = mock_get_address_info

    # Mock fidelity bond addresses
    mock_wallet.get_fidelity_bond_addresses_info = MagicMock(return_value=[])

    return mock_wallet, [utxo1, utxo2]


def test_extended_info_shows_individual_utxos(wallet_with_multi_utxo):
    """Test that multiple UTXOs on same address are displayed individually."""
    mock_wallet, _ = wallet_with_multi_utxo

    with tempfile.TemporaryDirectory() as tmpdir:
        mnemonic_file = Path(tmpdir) / "test.mnemonic"
        mnemonic_file.write_text("abandon " * 11 + "about")

        with patch("jmwallet.wallet.service.WalletService", return_value=mock_wallet):
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
                    "--extended",
                ],
            )

            assert result.exit_code == 0, f"Command failed: {result.stdout}"

            # Check both UTXO values appear
            assert "100,000 sats" in result.stdout, "First UTXO not shown"
            assert "150,000 sats" in result.stdout, "Second UTXO not shown"

            # Check confirmation counts
            assert "(3 conf)" in result.stdout or "(5+ conf)" in result.stdout


def test_extended_info_indents_subsequent_utxos(wallet_with_multi_utxo):
    """Test that subsequent UTXOs are indented."""
    mock_wallet, _ = wallet_with_multi_utxo

    with tempfile.TemporaryDirectory() as tmpdir:
        mnemonic_file = Path(tmpdir) / "test.mnemonic"
        mnemonic_file.write_text("abandon " * 11 + "about")

        with patch("jmwallet.wallet.service.WalletService", return_value=mock_wallet):
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
                    "--extended",
                ],
            )

            assert result.exit_code == 0

            lines = result.stdout.split("\n")

            # Find lines with our values
            value_lines = [
                line for line in lines if "100,000 sats" in line or "150,000 sats" in line
            ]

            assert len(value_lines) >= 2, f"Expected 2 lines, got: {value_lines}"


def test_extended_info_shows_frozen_per_utxo(wallet_with_multi_utxo):
    """Test that freeze status is shown per UTXO, not per address."""
    mock_wallet, (utxo1, utxo2) = wallet_with_multi_utxo

    # Freeze only the first UTXO
    utxo1.frozen = True
    utxo2.frozen = False

    with tempfile.TemporaryDirectory() as tmpdir:
        mnemonic_file = Path(tmpdir) / "test.mnemonic"
        mnemonic_file.write_text("abandon " * 11 + "about")

        with patch("jmwallet.wallet.service.WalletService", return_value=mock_wallet):
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
                    "--extended",
                ],
            )

            assert result.exit_code == 0, f"Command failed: {result.stdout}"

            lines = result.stdout.split("\n")

            # Find UTXO lines (contain address and confirmation count)
            # The UTXO line has format: path + address + value + status with conf
            lines_100k_utxo = [
                line for line in lines if "100,000 sats" in line and "(3 conf)" in line
            ]
            lines_150k_utxo = [
                line for line in lines if "150,000 sats" in line and "(5+ conf)" in line
            ]

            assert len(lines_100k_utxo) == 1, (
                f"Expected 1 UTXO line for 100k, got: {lines_100k_utxo}"
            )
            assert len(lines_150k_utxo) == 1, (
                f"Expected 1 UTXO line for 150k, got: {lines_150k_utxo}"
            )

            # Only the 100k UTXO (first one) should be marked frozen
            assert "[FROZEN]" in lines_100k_utxo[0], "Frozen UTXO should show [FROZEN]"
            assert "[FROZEN]" not in lines_150k_utxo[0], "Non-frozen UTXO should not show [FROZEN]"
