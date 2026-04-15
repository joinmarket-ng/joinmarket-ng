"""
E2E test for importing a reference implementation wallet with fidelity bond.

This test validates that:
1. A wallet created in the reference JAM with BIP39 passphrase can be imported
2. All mixdepth balances are correctly recovered
3. Fidelity bonds are auto-discovered from just the mnemonic+passphrase

Prerequisites:
- Docker and Docker Compose installed
- Run: docker compose --profile reference up -d

Usage:
    pytest tests/e2e/test_reference_bond_import.py -v -s --timeout=300 -m reference
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from tests.e2e.docker_utils import get_compose_cmd_prefix

# Mark all tests in this module as requiring Docker reference profile
pytestmark = pytest.mark.reference


# =============================================================================
# Test Configuration
# =============================================================================

# Standard test mnemonic (12 words) - well-known test vector
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)
# BIP39 passphrase (13th word)
TEST_BIP39_PASSPHRASE = "testpassphrase"
# Wallet encryption password
WALLET_PASSWORD = "testpassword123"
# Wallet filename for reference implementation
REF_WALLET_NAME = "bond_import_test.jmdat"

# Funding amounts
MIXDEPTH_FUND_AMOUNT_BTC = 0.5  # Amount to fund each tested mixdepth
BOND_FUND_AMOUNT_BTC = 1.0  # Amount to fund the fidelity bond

# Timeouts
WALLET_SYNC_TIMEOUT = 60  # seconds


# =============================================================================
# Helper Functions
# =============================================================================


def get_compose_file() -> Path:
    """Get path to docker-compose file."""
    from tests.e2e.docker_utils import get_compose_file as _get_compose_file

    return _get_compose_file()


def run_compose_cmd(
    args: list[str], check: bool = True, timeout: int = 30
) -> subprocess.CompletedProcess[str]:
    """Run a docker compose command."""
    cmd = get_compose_cmd_prefix() + args
    logger.debug(f"Running: {' '.join(cmd)}")
    return subprocess.run(
        cmd, capture_output=True, text=True, check=check, timeout=timeout
    )


def run_jam_cmd(
    args: list[str], timeout: int = 60, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a command inside the jam container."""
    cmd = get_compose_cmd_prefix() + ["exec", "-T", "jam"] + args
    logger.debug(f"Running in jam: {' '.join(args)}")
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        input=input_text,
    )


def run_bitcoin_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a bitcoin-cli command."""
    cmd = (
        get_compose_cmd_prefix()
        + [
            "exec",
            "-T",
            "bitcoin",
            "bitcoin-cli",
            "-regtest",
            "-rpcuser=test",
            "-rpcpassword=test",
        ]
        + args
    )
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def is_jam_running() -> bool:
    """Check if the JAM container is running."""
    result = run_compose_cmd(["ps", "-q", "jam"], check=False)
    return bool(result.stdout.strip())


def cleanup_wallet_lock(wallet_name: str) -> None:
    """Remove stale wallet lock file if it exists."""
    lock_file = f"/root/.joinmarket-ng/wallets/.{wallet_name}.lock"
    result = run_jam_cmd(["rm", "-f", lock_file], timeout=10)
    if result.returncode == 0:
        logger.debug(f"Cleaned up lock file: {lock_file}")


def ensure_expect_installed() -> bool:
    """Ensure expect is installed in the jam container."""
    result = run_jam_cmd(["which", "expect"], timeout=10)
    if result.returncode != 0:
        logger.info("Installing expect in jam container...")
        run_jam_cmd(["apt-get", "update"], timeout=60)
        run_jam_cmd(["apt-get", "install", "-y", "expect"], timeout=60)
        result = run_jam_cmd(["which", "expect"], timeout=10)
    return result.returncode == 0


def copy_expect_script_to_jam() -> bool:
    """Copy the expect script to the jam container."""
    script_path = (
        Path(__file__).parent / "reference" / "recover_wallet_with_passphrase.exp"
    )
    if not script_path.exists():
        logger.error(f"Expect script not found: {script_path}")
        return False

    # Copy script to container
    cmd = get_compose_cmd_prefix() + [
        "cp",
        str(script_path),
        "jam:/scripts/recover_wallet_with_passphrase.exp",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        logger.error(f"Failed to copy script: {result.stderr}")
        return False

    # Make executable
    run_jam_cmd(["chmod", "+x", "/scripts/recover_wallet_with_passphrase.exp"])
    return True


def recover_wallet_with_passphrase(
    mnemonic: str,
    bip39_passphrase: str,
    wallet_password: str,
    wallet_name: str,
) -> bool:
    """
    Recover a wallet in JAM using the expect script.

    Args:
        mnemonic: 12-word BIP39 recovery phrase
        bip39_passphrase: BIP39 passphrase (13th word)
        wallet_password: Encryption password for wallet file
        wallet_name: Wallet filename

    Returns:
        True if wallet was recovered successfully
    """
    cleanup_wallet_lock(wallet_name)

    # Check if wallet already exists and remove it
    result = run_jam_cmd(
        ["ls", f"/root/.joinmarket-ng/wallets/{wallet_name}"],
        timeout=10,
    )
    if result.returncode == 0:
        logger.info(f"Removing existing wallet: {wallet_name}")
        run_jam_cmd(["rm", f"/root/.joinmarket-ng/wallets/{wallet_name}"])

    if not ensure_expect_installed():
        logger.error("Could not install expect")
        return False

    if not copy_expect_script_to_jam():
        logger.error("Could not copy expect script")
        return False

    # Run the expect script
    logger.info(f"Recovering wallet {wallet_name} with BIP39 passphrase...")
    result = run_jam_cmd(
        [
            "expect",
            "/scripts/recover_wallet_with_passphrase.exp",
            mnemonic,
            bip39_passphrase,
            wallet_password,
            wallet_name,
        ],
        timeout=180,
    )

    if result.returncode != 0:
        logger.error(f"Wallet recovery failed: {result.stderr}")
        logger.error(f"Output: {result.stdout}")
        return False

    logger.info(f"Wallet recovered successfully: {wallet_name}")
    logger.debug(f"Output: {result.stdout}")
    return True


def get_jam_wallet_address(
    wallet_name: str,
    password: str,
    mixdepth: int = 0,
) -> str | None:
    """Get a receive address from jam wallet."""
    cleanup_wallet_lock(wallet_name)
    result = run_jam_cmd(
        [
            "python3",
            "/src/scripts/wallet-tool.py",
            "--datadir=/root/.joinmarket-ng",
            "--wallet-password-stdin",
            f"/root/.joinmarket-ng/wallets/{wallet_name}",
            "display",
        ],
        timeout=60,
        input_text=f"{password}\n",
    )

    if result.returncode != 0:
        logger.error(f"Failed to get wallet info: {result.stderr}")
        return None

    # Parse output to find first NEW address in external branch of mixdepth
    lines = result.stdout.split("\n")
    for line in lines:
        if f"/{mixdepth}'/0/" in line and "new" in line.lower():
            parts = line.split()
            for part in parts:
                if part.startswith("bcrt1") or part.startswith("bc1"):
                    logger.info(f"Found new address for mixdepth {mixdepth}: {part}")
                    return part

    # Fallback: just find any address in the right mixdepth
    for line in lines:
        if f"/{mixdepth}'/0/" in line:
            parts = line.split()
            for part in parts:
                if part.startswith("bcrt1") or part.startswith("bc1"):
                    return part

    logger.warning(f"Could not find address in wallet output for mixdepth {mixdepth}")
    return None


def get_jam_fidelity_bond_address(
    wallet_name: str,
    password: str,
    locktime_date: str,  # Format: YYYY-MM
) -> str | None:
    """
    Get a fidelity bond (timelocked) address from jam wallet.

    Args:
        wallet_name: Wallet filename
        password: Wallet password
        locktime_date: Locktime in YYYY-MM format (e.g., "2026-01")

    Returns:
        P2WSH timelocked address or None on failure
    """
    cleanup_wallet_lock(wallet_name)

    result = run_jam_cmd(
        [
            "python3",
            "/src/scripts/wallet-tool.py",
            "--datadir=/root/.joinmarket-ng",
            "--wallet-password-stdin",
            f"/root/.joinmarket-ng/wallets/{wallet_name}",
            "gettimelockaddress",
            locktime_date,
        ],
        timeout=60,
        input_text=f"{password}\n",
    )

    if result.returncode != 0:
        logger.error(f"Failed to get timelock address: {result.stderr}")
        logger.debug(f"Output: {result.stdout}")
        return None

    # Parse output for the P2WSH address
    # Output format is typically just the address
    lines = result.stdout.strip().split("\n")
    for line in lines:
        line = line.strip()
        # P2WSH addresses on regtest start with bcrt1q and are longer than P2WPKH
        if line.startswith("bcrt1q") and len(line) > 50:
            logger.info(f"Got fidelity bond address: {line}")
            return line
        # Also check for mainnet format
        if line.startswith("bc1q") and len(line) > 50:
            logger.info(f"Got fidelity bond address: {line}")
            return line

    logger.warning(f"Could not find bond address in output: {result.stdout}")
    return None


def get_jam_wallet_balance(wallet_name: str, password: str) -> dict[int, int]:
    """
    Get balances for all mixdepths from jam wallet.

    Returns:
        Dict mapping mixdepth -> balance in satoshis
    """
    cleanup_wallet_lock(wallet_name)

    result = run_jam_cmd(
        [
            "python3",
            "/src/scripts/wallet-tool.py",
            "--datadir=/root/.joinmarket-ng",
            "--wallet-password-stdin",
            f"/root/.joinmarket-ng/wallets/{wallet_name}",
            "display",
        ],
        timeout=60,
        input_text=f"{password}\n",
    )

    if result.returncode != 0:
        logger.error(f"Failed to get wallet balance: {result.stderr}")
        return {}

    # Parse balances from output
    # Look for lines like "mixdepth 0 balance: 0.50000000 BTC"
    balances: dict[int, int] = {}
    for line in result.stdout.split("\n"):
        line_lower = line.lower()
        if "mixdepth" in line_lower and "balance" in line_lower:
            # Extract mixdepth number and balance
            parts = line.split()
            try:
                # Find mixdepth number
                for i, part in enumerate(parts):
                    if part.lower() == "mixdepth" and i + 1 < len(parts):
                        md = int(parts[i + 1])
                        # Find balance
                        for j, p in enumerate(parts):
                            if p.lower() == "balance:" and j + 1 < len(parts):
                                balance_btc = float(parts[j + 1])
                                balances[md] = int(balance_btc * 100_000_000)
                                break
                        break
            except (ValueError, IndexError):
                continue

    logger.info(f"JAM wallet balances: {balances}")
    return balances


def fund_address(address: str, amount_btc: float) -> bool:
    """
    Fund an address using the fidelity_funder wallet.

    Args:
        address: Address to fund
        amount_btc: Amount in BTC

    Returns:
        True if funding successful
    """
    funder_wallet = "fidelity_funder"

    # Check funder wallet balance
    result = run_bitcoin_cmd(["-rpcwallet=" + funder_wallet, "getbalance"])
    if result.returncode != 0:
        logger.warning(f"Could not get funder wallet balance: {result.stderr}")
        # Try to mine some coins to funder
        result = run_bitcoin_cmd(["-rpcwallet=" + funder_wallet, "getnewaddress"])
        if result.returncode == 0:
            funder_addr = result.stdout.strip()
            run_bitcoin_cmd(["generatetoaddress", "111", funder_addr])

    # Send to target address
    logger.info(f"Funding {address} with {amount_btc} BTC...")
    result = run_bitcoin_cmd(
        ["-rpcwallet=" + funder_wallet, "sendtoaddress", address, str(amount_btc)]
    )
    if result.returncode != 0:
        logger.error(f"Failed to send to address: {result.stderr}")
        return False

    txid = result.stdout.strip()
    logger.info(f"Funded {address} with {amount_btc} BTC, txid: {txid}")

    # Mine blocks to confirm
    result = run_bitcoin_cmd(["-rpcwallet=" + funder_wallet, "getnewaddress"])
    if result.returncode == 0:
        funder_addr = result.stdout.strip()
        run_bitcoin_cmd(["generatetoaddress", "6", funder_addr])
        logger.info("Mined 6 blocks to confirm transaction")

    return True


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(scope="module")
def reference_services():
    """Ensure reference services are running."""
    if not is_jam_running():
        pytest.skip(
            "JAM container not running. "
            "Start with: docker compose --profile reference up -d"
        )

    # Verify services are healthy
    services = ["bitcoin", "jam"]
    for service in services:
        result = run_compose_cmd(["ps", "-q", service], check=False)
        if not result.stdout.strip():
            pytest.skip(f"Service {service} not running")

    yield

    # No cleanup - leave services running


@pytest.fixture(scope="module")
def funded_reference_wallet(reference_services) -> dict[str, Any]:
    """
    Create and fund a reference wallet with BIP39 passphrase and fidelity bond.

    This fixture:
    1. Recovers a wallet in JAM using mnemonic + BIP39 passphrase
    2. Funds multiple mixdepths
    3. Creates and funds a fidelity bond
    4. Returns wallet details for verification

    Returns:
        Dict with wallet details: mnemonic, passphrase, balances, bond info
    """
    # Step 1: Recover wallet with BIP39 passphrase
    logger.info("=" * 60)
    logger.info("Step 1: Recovering wallet in reference implementation")
    logger.info("=" * 60)

    wallet_recovered = recover_wallet_with_passphrase(
        mnemonic=TEST_MNEMONIC,
        bip39_passphrase=TEST_BIP39_PASSPHRASE,
        wallet_password=WALLET_PASSWORD,
        wallet_name=REF_WALLET_NAME,
    )

    if not wallet_recovered:
        pytest.skip("Could not recover wallet in reference implementation")

    # Step 2: Fund multiple mixdepths
    logger.info("=" * 60)
    logger.info("Step 2: Funding mixdepths")
    logger.info("=" * 60)

    # Fund mixdepths 0 and 2 with different amounts
    mixdepth_funding = {
        0: MIXDEPTH_FUND_AMOUNT_BTC,
        2: MIXDEPTH_FUND_AMOUNT_BTC * 0.75,
    }

    for mixdepth, amount in mixdepth_funding.items():
        address = get_jam_wallet_address(REF_WALLET_NAME, WALLET_PASSWORD, mixdepth)
        if not address:
            pytest.skip(f"Could not get address for mixdepth {mixdepth}")
        assert address is not None  # mypy: pytest.skip is NoReturn

        if not fund_address(address, amount):
            pytest.skip(f"Could not fund mixdepth {mixdepth}")

    # Wait for wallet to see funds
    time.sleep(5)

    # Step 3: Create and fund fidelity bond
    logger.info("=" * 60)
    logger.info("Step 3: Creating and funding fidelity bond")
    logger.info("=" * 60)

    # Use a past locktime so the bond is spendable (for cleanup)
    # 2020-01 is timenumber 0
    bond_locktime_date = "2020-01"

    bond_address = get_jam_fidelity_bond_address(
        REF_WALLET_NAME, WALLET_PASSWORD, bond_locktime_date
    )
    if not bond_address:
        pytest.skip("Could not get fidelity bond address")
    assert bond_address is not None  # mypy: pytest.skip is NoReturn

    logger.info(f"Fidelity bond address: {bond_address}")

    if not fund_address(bond_address, BOND_FUND_AMOUNT_BTC):
        pytest.skip("Could not fund fidelity bond")

    # Wait for confirmations
    time.sleep(5)

    # Step 4: Verify balances in reference wallet
    logger.info("=" * 60)
    logger.info("Step 4: Verifying reference wallet balances")
    logger.info("=" * 60)

    ref_balances = get_jam_wallet_balance(REF_WALLET_NAME, WALLET_PASSWORD)
    logger.info(f"Reference wallet balances: {ref_balances}")

    return {
        "mnemonic": TEST_MNEMONIC,
        "bip39_passphrase": TEST_BIP39_PASSPHRASE,
        "wallet_name": REF_WALLET_NAME,
        "wallet_password": WALLET_PASSWORD,
        "mixdepth_funding": mixdepth_funding,
        "bond_address": bond_address,
        "bond_locktime_date": bond_locktime_date,
        "bond_amount_btc": BOND_FUND_AMOUNT_BTC,
        "ref_balances": ref_balances,
    }


# =============================================================================
# Tests
# =============================================================================


class TestReferenceBondImport:
    """Test importing a reference wallet with fidelity bond into jm-wallet."""

    @pytest.mark.asyncio
    async def test_import_wallet_recovers_balances(
        self, funded_reference_wallet: dict[str, Any], blockchain_backend
    ):
        """
        Test that importing a reference wallet recovers all mixdepth balances.

        This validates:
        1. Same mnemonic + passphrase produces same addresses
        2. All funded mixdepths are discovered
        3. Balances match between implementations
        """
        from jmwallet.wallet.service import WalletService

        logger.info("=" * 60)
        logger.info("Test: Importing wallet and verifying balances")
        logger.info("=" * 60)

        # Create wallet with same mnemonic and passphrase
        wallet = WalletService(
            mnemonic=funded_reference_wallet["mnemonic"],
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=funded_reference_wallet["bip39_passphrase"],
        )

        # Sync all mixdepths
        logger.info("Syncing wallet...")
        utxos_by_mixdepth = await wallet.sync_all()

        # Calculate balances
        our_balances: dict[int, int] = {}
        for mixdepth in range(5):
            utxos = utxos_by_mixdepth.get(mixdepth, [])
            our_balances[mixdepth] = sum(u.value for u in utxos)

        logger.info(f"Our wallet balances: {our_balances}")

        # Verify funded mixdepths have correct balances
        for mixdepth, expected_btc in funded_reference_wallet[
            "mixdepth_funding"
        ].items():
            expected_sats = int(expected_btc * 100_000_000)
            actual_sats = our_balances.get(mixdepth, 0)

            logger.info(
                f"Mixdepth {mixdepth}: expected={expected_sats:,} sats, "
                f"actual={actual_sats:,} sats"
            )

            # Verify we have at least the expected amount
            # (may have more from previous test runs on the same regtest chain)
            assert actual_sats >= expected_sats, (
                f"Mixdepth {mixdepth} has less than expected: "
                f"expected at least {expected_sats:,}, got {actual_sats:,}"
            )

        logger.info("✓ All mixdepth balances recovered correctly")

    @pytest.mark.asyncio
    async def test_import_wallet_discovers_fidelity_bond(
        self, funded_reference_wallet: dict[str, Any], blockchain_backend
    ):
        """
        Test that fidelity bonds are auto-discovered from mnemonic+passphrase.

        This validates:
        1. Bond discovery scan finds the funded bond
        2. Bond address matches between implementations
        3. Bond value is correct
        """
        from jmwallet.wallet.service import WalletService

        logger.info("=" * 60)
        logger.info("Test: Discovering fidelity bond")
        logger.info("=" * 60)

        # Create wallet with same mnemonic and passphrase
        wallet = WalletService(
            mnemonic=funded_reference_wallet["mnemonic"],
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=funded_reference_wallet["bip39_passphrase"],
        )

        # Run fidelity bond discovery
        logger.info("Running fidelity bond discovery scan...")

        def progress(current: int, total: int) -> None:
            if current % 100 == 0:
                logger.info(f"  Scanning: {current}/{total} timelocks")

        discovered = await wallet.discover_fidelity_bonds(
            max_index=1,
            progress_callback=progress,
        )

        logger.info(f"Discovered {len(discovered)} fidelity bond(s)")

        # Verify we found the bond
        assert len(discovered) >= 1, (
            f"Should discover at least 1 fidelity bond, found {len(discovered)}"
        )

        # Find our specific bond by address
        expected_address = funded_reference_wallet["bond_address"]
        expected_value = int(funded_reference_wallet["bond_amount_btc"] * 100_000_000)

        found_bond = None
        for utxo in discovered:
            logger.info(
                f"  Found bond: address={utxo.address}, "
                f"value={utxo.value:,} sats, locktime={utxo.locktime}"
            )
            if utxo.address == expected_address:
                found_bond = utxo
                break

        assert found_bond is not None, (
            f"Should find bond at {expected_address}, "
            f"found addresses: {[u.address for u in discovered]}"
        )

        # Verify bond value is at least what we funded
        # (there may be multiple UTXOs at the same bond address from previous runs)
        total_bond_value = sum(
            u.value for u in discovered if u.address == expected_address
        )
        assert total_bond_value >= expected_value, (
            f"Bond value should be at least {expected_value:,}, "
            f"got {total_bond_value:,} across {len([u for u in discovered if u.address == expected_address])} UTXOs"
        )

        logger.info("✓ Fidelity bond discovered successfully")
        logger.info(f"  Address: {found_bond.address}")
        logger.info(f"  Value: {found_bond.value:,} sats")
        logger.info(f"  Locktime: {found_bond.locktime}")

    @pytest.mark.asyncio
    async def test_bond_address_matches_reference(
        self, funded_reference_wallet: dict[str, Any], blockchain_backend
    ):
        """
        Test that our derived bond address matches the reference implementation.

        This is a critical compatibility check - if addresses don't match,
        bonds created in the reference implementation won't be recoverable.
        """
        from jmcore.timenumber import parse_locktime_date
        from jmwallet.wallet.service import WalletService

        logger.info("=" * 60)
        logger.info("Test: Verifying bond address derivation")
        logger.info("=" * 60)

        # Create wallet with same mnemonic and passphrase
        wallet = WalletService(
            mnemonic=funded_reference_wallet["mnemonic"],
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=funded_reference_wallet["bip39_passphrase"],
        )

        # Parse the locktime from date format
        locktime_date = funded_reference_wallet["bond_locktime_date"]
        locktime = parse_locktime_date(locktime_date)

        # Generate bond address with our implementation
        our_address = wallet.get_fidelity_bond_address(0, locktime)
        ref_address = funded_reference_wallet["bond_address"]

        logger.info(f"Reference bond address: {ref_address}")
        logger.info(f"Our derived bond address: {our_address}")
        logger.info(f"Locktime date: {locktime_date}")
        logger.info(f"Locktime timestamp: {locktime}")

        assert our_address == ref_address, (
            f"Bond address mismatch!\n"
            f"Reference: {ref_address}\n"
            f"Ours: {our_address}\n"
            f"This indicates a derivation path incompatibility"
        )

        logger.info("✓ Bond addresses match between implementations")

    @pytest.mark.asyncio
    async def test_full_wallet_import_with_all_utxos(
        self, funded_reference_wallet: dict[str, Any], blockchain_backend
    ):
        """
        Test complete wallet import including regular UTXOs and fidelity bond.

        This is the comprehensive test that verifies:
        1. All mixdepth balances
        2. Fidelity bond discovery
        3. Total wallet value matches
        """
        from jmwallet.wallet.service import WalletService

        logger.info("=" * 60)
        logger.info("Test: Full wallet import verification")
        logger.info("=" * 60)

        # Create wallet with same mnemonic and passphrase
        wallet = WalletService(
            mnemonic=funded_reference_wallet["mnemonic"],
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=funded_reference_wallet["bip39_passphrase"],
        )

        # Sync all mixdepths
        logger.info("Syncing all mixdepths...")
        utxos_by_mixdepth = await wallet.sync_all()

        # Discover fidelity bonds
        logger.info("Discovering fidelity bonds...")
        discovered_bonds = await wallet.discover_fidelity_bonds(max_index=1)

        # Calculate total balance
        total_regular = sum(
            sum(u.value for u in utxos) for utxos in utxos_by_mixdepth.values()
        )
        total_bonds = sum(u.value for u in discovered_bonds)
        total_wallet = total_regular + total_bonds

        logger.info(f"Regular UTXOs total: {total_regular:,} sats")
        logger.info(f"Fidelity bonds total: {total_bonds:,} sats")
        logger.info(f"Total wallet value: {total_wallet:,} sats")

        # Calculate expected total
        expected_regular = sum(
            int(amount * 100_000_000)
            for amount in funded_reference_wallet["mixdepth_funding"].values()
        )
        expected_bonds = int(funded_reference_wallet["bond_amount_btc"] * 100_000_000)
        expected_total = expected_regular + expected_bonds

        logger.info(f"Expected regular: {expected_regular:,} sats")
        logger.info(f"Expected bonds: {expected_bonds:,} sats")
        logger.info(f"Expected total: {expected_total:,} sats")

        # Verify we have at least what we expect
        # (blockchain may have more from previous test runs)
        assert total_wallet >= expected_total, (
            f"Total wallet value less than expected: "
            f"expected at least {expected_total:,}, got {total_wallet:,}"
        )

        # Verify we have the expected number of UTXOs
        total_utxo_count = sum(len(utxos) for utxos in utxos_by_mixdepth.values())
        total_utxo_count += len(discovered_bonds)

        # We funded 2 mixdepths + 1 bond = at least 3 UTXOs
        assert total_utxo_count >= 3, (
            f"Expected at least 3 UTXOs (2 mixdepths + 1 bond), "
            f"found {total_utxo_count}"
        )

        logger.info("✓ Full wallet import verified successfully")
        logger.info(f"  Total UTXOs: {total_utxo_count}")
        logger.info(
            f"  Total value: {total_wallet:,} sats ({total_wallet / 100_000_000:.8f} BTC)"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--timeout=300", "-m", "reference"])
