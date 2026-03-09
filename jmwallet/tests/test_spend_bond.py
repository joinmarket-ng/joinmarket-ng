"""Tests for the spend-bond CLI command."""

from __future__ import annotations

import base64
import tempfile
import time
from pathlib import Path

from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    save_registry,
)

runner = CliRunner()

# Deterministic test data -- uses a valid compressed pubkey so btc_script works
TEST_PUBKEY_HEX = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
# Locktime in the past so the bond is spendable
TEST_LOCKTIME = 1672531200  # 2023-01-01 00:00:00 UTC
TEST_NETWORK = "regtest"


def _create_funded_bond(data_dir: Path) -> str:
    """Create a funded bond in the registry and return its address."""
    from jmcore.btc_script import mk_freeze_script

    from jmwallet.wallet.address import script_to_p2wsh_address

    witness_script = mk_freeze_script(TEST_PUBKEY_HEX, TEST_LOCKTIME)
    address = script_to_p2wsh_address(witness_script, TEST_NETWORK)

    bond = FidelityBondInfo(
        address=address,
        locktime=TEST_LOCKTIME,
        locktime_human="2023-01-01 00:00:00",
        index=0,
        path="external",
        pubkey=TEST_PUBKEY_HEX,
        witness_script_hex=witness_script.hex(),
        network=TEST_NETWORK,
        created_at="2025-01-01T00:00:00",
        txid="aa" * 32,
        vout=0,
        value=100_000,
        confirmations=1000,
    )

    registry = BondRegistry(bonds=[bond])
    save_registry(registry, data_dir)
    return address


def _create_unfunded_bond(data_dir: Path) -> str:
    """Create an unfunded bond in the registry and return its address."""
    from jmcore.btc_script import mk_freeze_script

    from jmwallet.wallet.address import script_to_p2wsh_address

    witness_script = mk_freeze_script(TEST_PUBKEY_HEX, TEST_LOCKTIME)
    address = script_to_p2wsh_address(witness_script, TEST_NETWORK)

    bond = FidelityBondInfo(
        address=address,
        locktime=TEST_LOCKTIME,
        locktime_human="2023-01-01 00:00:00",
        index=0,
        path="external",
        pubkey=TEST_PUBKEY_HEX,
        witness_script_hex=witness_script.hex(),
        network=TEST_NETWORK,
        created_at="2025-01-01T00:00:00",
        # No UTXO info -> unfunded
    )

    registry = BondRegistry(bonds=[bond])
    save_registry(registry, data_dir)
    return address


# A valid regtest P2WPKH destination address (derived from 2*G on secp256k1)
DEST_ADDRESS = "bcrt1qq6hag67dl53wl99vzg42z8eyzfz2xlkvwk6f7m"


class TestSpendBondCommand:
    """Tests for the spend-bond CLI command."""

    def test_basic_psbt_generation(self) -> None:
        """spend-bond should produce a valid PSBT in base64."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--fee-rate",
                    "1.0",
                ],
            )
            assert result.exit_code == 0, f"Command failed: {result.stdout}"
            assert "SPEND BOND PSBT" in result.stdout
            assert "PSBT (base64)" in result.stdout

            # Extract the base64 PSBT from output
            lines = result.stdout.split("\n")
            psbt_b64 = _extract_psbt_from_output(lines)
            assert psbt_b64 is not None, "Could not find PSBT in output"

            # Verify it's valid base64 that starts with PSBT magic
            psbt_bytes = base64.b64decode(psbt_b64)
            assert psbt_bytes[:5] == b"psbt\xff"

    def test_output_file(self) -> None:
        """spend-bond --output should save PSBT to a file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)
            output_file = data_dir / "spend.psbt"

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--output",
                    str(output_file),
                ],
            )
            assert result.exit_code == 0, f"Command failed: {result.stdout}"
            assert output_file.exists()

            # File should contain valid base64 PSBT
            psbt_b64 = output_file.read_text().strip()
            psbt_bytes = base64.b64decode(psbt_b64)
            assert psbt_bytes[:5] == b"psbt\xff"

    def test_bond_not_found(self) -> None:
        """spend-bond should fail gracefully when bond address is unknown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            # Don't create any bonds

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    "bcrt1qfake_address_that_does_not_exist",
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                ],
            )
            assert result.exit_code != 0

    def test_bond_not_funded(self) -> None:
        """spend-bond should fail when bond has no UTXO info."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_unfunded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                ],
            )
            assert result.exit_code != 0

    def test_test_unfunded_mode_allows_psbt_generation(self) -> None:
        """--test-unfunded should create a dry-run PSBT for unfunded bonds."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_unfunded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--test-unfunded",
                ],
            )

            assert result.exit_code == 0
            assert "MODE:             TEST-UNFUNDED" in result.stdout
            assert "synthetic UTXO" in result.stdout
            psbt_b64 = _extract_psbt_from_output(result.stdout.split("\n"))
            assert psbt_b64 is not None
            assert base64.b64decode(psbt_b64)[:5] == b"psbt\xff"

    def test_test_unfunded_requires_positive_value(self) -> None:
        """--test-utxo-value must be positive in --test-unfunded mode."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_unfunded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--test-unfunded",
                    "--test-utxo-value",
                    "0",
                ],
            )

            assert result.exit_code != 0

    def test_invalid_destination_address(self) -> None:
        """spend-bond should fail with an invalid destination."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    "not_a_valid_address",
                    "--data-dir",
                    str(data_dir),
                ],
            )
            assert result.exit_code != 0

    def test_zero_fee_rate(self) -> None:
        """spend-bond should reject zero or negative fee rate."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--fee-rate",
                    "0",
                ],
            )
            assert result.exit_code != 0

    def test_fee_deducted_from_value(self) -> None:
        """The send amount in the PSBT should be bond value minus fee."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--fee-rate",
                    "10.0",
                ],
            )
            assert result.exit_code == 0, f"Command failed: {result.stdout}"

            # Extract PSBT and verify the output amount
            lines = result.stdout.split("\n")
            psbt_b64 = _extract_psbt_from_output(lines)
            assert psbt_b64 is not None

            psbt_bytes = base64.b64decode(psbt_b64)

            # Parse the unsigned tx from the PSBT to verify amounts
            from jmcore.bitcoin import decode_varint, parse_transaction

            # Skip magic (5), key-len (1), key-type (1)
            val_len, offset = decode_varint(psbt_bytes, 7)
            unsigned_tx_hex = psbt_bytes[offset : offset + val_len].hex()
            parsed = parse_transaction(unsigned_tx_hex)

            assert len(parsed.outputs) == 1
            # Bond value is 100,000 sats, fee at 10 sat/vB for ~112 vB ≈ 1120 sats
            # So output should be less than 100,000 but more than 97,000
            assert 97_000 < parsed.outputs[0].value < 100_000

    def test_future_locktime_warning(self) -> None:
        """spend-bond should warn (but still succeed) if locktime is in the future."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)

            from jmcore.btc_script import mk_freeze_script

            from jmwallet.wallet.address import script_to_p2wsh_address

            future_locktime = int(time.time()) + 86400 * 365  # 1 year from now
            witness_script = mk_freeze_script(TEST_PUBKEY_HEX, future_locktime)
            address = script_to_p2wsh_address(witness_script, TEST_NETWORK)

            bond = FidelityBondInfo(
                address=address,
                locktime=future_locktime,
                locktime_human="2027-01-01 00:00:00",
                index=0,
                path="external",
                pubkey=TEST_PUBKEY_HEX,
                witness_script_hex=witness_script.hex(),
                network=TEST_NETWORK,
                created_at="2025-01-01T00:00:00",
                txid="aa" * 32,
                vout=0,
                value=100_000,
                confirmations=100,
            )
            registry = BondRegistry(bonds=[bond])
            save_registry(registry, data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                ],
            )
            # Should still succeed (creates the PSBT even if locktime not expired)
            assert result.exit_code == 0
            assert "PSBT (base64)" in result.stdout
            # Should show a warning about broadcasting
            assert "NOT expired" in result.stdout or "not expired" in result.stdout.lower()

    def test_psbt_contains_correct_locktime(self) -> None:
        """The unsigned tx in the PSBT must set nLockTime to the bond locktime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                ],
            )
            assert result.exit_code == 0

            lines = result.stdout.split("\n")
            psbt_b64 = _extract_psbt_from_output(lines)
            assert psbt_b64 is not None

            psbt_bytes = base64.b64decode(psbt_b64)

            from jmcore.bitcoin import decode_varint, parse_transaction

            val_len, offset = decode_varint(psbt_bytes, 7)
            unsigned_tx_hex = psbt_bytes[offset : offset + val_len].hex()
            parsed = parse_transaction(unsigned_tx_hex)

            assert parsed.locktime == TEST_LOCKTIME

    def test_psbt_input_sequence(self) -> None:
        """Input sequence must be 0xFFFFFFFE to enable nLockTime."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                ],
            )
            assert result.exit_code == 0

            lines = result.stdout.split("\n")
            psbt_b64 = _extract_psbt_from_output(lines)
            assert psbt_b64 is not None
            psbt_bytes = base64.b64decode(psbt_b64)

            from jmcore.bitcoin import decode_varint, parse_transaction

            val_len, offset = decode_varint(psbt_bytes, 7)
            unsigned_tx_hex = psbt_bytes[offset : offset + val_len].hex()
            parsed = parse_transaction(unsigned_tx_hex)

            assert parsed.inputs[0].sequence == 0xFFFFFFFE


class TestSpendBondBIP32Derivation:
    """Tests for BIP32 derivation info in the spend-bond PSBT."""

    def test_bip32_derivation_included_in_psbt(self) -> None:
        """PSBT should contain BIP32 derivation when fingerprint and path provided."""
        import struct

        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)
            fingerprint = "aabbccdd"
            deriv_path = "m/84'/0'/0'/0/0"

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--master-fingerprint",
                    fingerprint,
                    "--derivation-path",
                    deriv_path,
                ],
            )

            assert result.exit_code == 0

            # BIP32 derivation should be in the PSBT binary data
            lines = result.stdout.split("\n")
            psbt_b64 = _extract_psbt_from_output(lines)
            assert psbt_b64 is not None
            psbt_bytes = base64.b64decode(psbt_b64)

            # Verify the fingerprint is in the PSBT
            fp_bytes = bytes.fromhex(fingerprint)
            assert fp_bytes in psbt_bytes

            # Verify the derivation path indices are present
            # m/84'/0'/0'/0/0 -> [84|0x80000000, 0|0x80000000, 0|0x80000000, 0, 0]
            path_bytes = b"".join(
                struct.pack("<I", idx)
                for idx in [
                    84 | 0x80000000,
                    0 | 0x80000000,
                    0 | 0x80000000,
                    0,
                    0,
                ]
            )
            assert fp_bytes + path_bytes in psbt_bytes

    def test_fingerprint_only_fails(self) -> None:
        """Providing --master-fingerprint without --derivation-path should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--master-fingerprint",
                    "aabbccdd",
                ],
            )

            assert result.exit_code != 0

    def test_path_only_fails(self) -> None:
        """Providing --derivation-path without --master-fingerprint should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--derivation-path",
                    "m/84'/0'/0'/0/0",
                ],
            )

            assert result.exit_code != 0

    def test_invalid_fingerprint_hex(self) -> None:
        """Invalid hex for master fingerprint should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--master-fingerprint",
                    "not-hex",
                    "--derivation-path",
                    "m/84'/0'/0'/0/0",
                ],
            )

            assert result.exit_code != 0

    def test_invalid_fingerprint_length(self) -> None:
        """Fingerprint with wrong byte count should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--master-fingerprint",
                    "aabb",  # Only 2 bytes, need 4
                    "--derivation-path",
                    "m/84'/0'/0'/0/0",
                ],
            )

            assert result.exit_code != 0

    def test_invalid_derivation_path(self) -> None:
        """Invalid derivation path should fail."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--master-fingerprint",
                    "aabbccdd",
                    "--derivation-path",
                    "m/84'/abc/0'",
                ],
            )

            assert result.exit_code != 0

    def test_hwi_limitation_shown_with_derivation(self) -> None:
        """When BIP32 derivation is provided, HW limitation warning should be shown."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                    "--master-fingerprint",
                    "aabbccdd",
                    "--derivation-path",
                    "m/84'/0'/0'/0/0",
                ],
            )

            assert result.exit_code == 0
            assert "hardware wallets" in result.stdout
            assert "CANNOT sign" in result.stdout
            assert "sign_bond_mnemonic.py" in result.stdout
            assert "Sparrow Wallet" in result.stdout

    def test_no_derivation_shows_hint(self) -> None:
        """Without BIP32 derivation, output should hint to use the flags."""
        with tempfile.TemporaryDirectory() as tmpdir:
            data_dir = Path(tmpdir)
            address = _create_funded_bond(data_dir)

            result = runner.invoke(
                app,
                [
                    "spend-bond",
                    address,
                    DEST_ADDRESS,
                    "--data-dir",
                    str(data_dir),
                ],
            )

            assert result.exit_code == 0
            assert "--derivation-path" in result.stdout
            assert "sign_bond_mnemonic.py" in result.stdout


def _extract_psbt_from_output(lines: list[str]) -> str | None:
    """Extract the PSBT base64 string from CLI output.

    The PSBT is printed between two dashed separator lines:
        ----- ...
        <base64 PSBT>
        ----- ...
    """
    in_psbt_section = False
    for line in lines:
        stripped = line.strip()
        if "PSBT (base64)" in stripped:
            in_psbt_section = True
            continue
        if in_psbt_section:
            if stripped.startswith("---"):
                # Skip separator lines
                continue
            if stripped and not stripped.startswith("="):
                # This should be the PSBT base64
                return stripped
    return None
