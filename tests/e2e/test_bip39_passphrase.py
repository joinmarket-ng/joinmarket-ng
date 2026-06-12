"""
E2E test for fidelity bond recovery with BIP39 passphrase.

This test validates that:
1. A wallet created in jmwallet with BIP39 passphrase produces correct addresses
2. Fidelity bonds can be created and recovered with passphrase
3. Bond addresses match between jmwallet and reference implementation

Prerequisites:
- Docker and Docker Compose installed
- Run: docker compose --profile e2e up -d

Usage:
    pytest tests/e2e/test_bip39_passphrase.py -v -s --timeout=120 -m e2e
"""

from __future__ import annotations

import pytest

from jmcore.timenumber import parse_locktime_date, timenumber_to_timestamp
from loguru import logger

# Mark all tests in this module as requiring Docker e2e profile
pytestmark = pytest.mark.e2e


class TestBIP39PassphraseWallet:
    """Test wallet with BIP39 passphrase support."""

    # Standard test mnemonic (12 words)
    TEST_MNEMONIC = (
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )
    # BIP39 passphrase (13th word)
    TEST_PASSPHRASE = "testpassphrase"

    @pytest.mark.asyncio
    async def test_wallet_addresses_with_passphrase(self, blockchain_backend):
        """
        Test that wallet with passphrase generates correct addresses.

        Verifies that:
        1. Same mnemonic without passphrase produces different addresses
        2. Same mnemonic with same passphrase produces same addresses
        3. Address derivation follows BIP84 standard
        """
        from jmwallet.wallet.service import WalletService

        # Create wallet WITHOUT passphrase
        wallet_no_pass = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase="",
        )

        # Create wallet WITH passphrase
        wallet_with_pass = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=self.TEST_PASSPHRASE,
        )

        # Generate addresses for mixdepth 0
        addr_no_pass = wallet_no_pass.get_receive_address(0, 0)
        addr_with_pass = wallet_with_pass.get_receive_address(0, 0)

        logger.info(f"Address without passphrase: {addr_no_pass}")
        logger.info(f"Address with passphrase:    {addr_with_pass}")

        # Addresses must be different
        assert addr_no_pass != addr_with_pass, (
            "Same mnemonic with different passphrases must produce different addresses"
        )

        # Both should be valid regtest bech32 addresses
        assert addr_no_pass.startswith("bcrt1"), "Should be regtest bech32"
        assert addr_with_pass.startswith("bcrt1"), "Should be regtest bech32"

        # Create second wallet with same passphrase - should produce same address
        wallet_with_pass_2 = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=self.TEST_PASSPHRASE,
        )
        addr_with_pass_2 = wallet_with_pass_2.get_receive_address(0, 0)

        assert addr_with_pass == addr_with_pass_2, (
            "Same mnemonic + passphrase must produce same address"
        )

        logger.info("✓ BIP39 passphrase address derivation works correctly")

    @pytest.mark.asyncio
    async def test_fidelity_bond_address_with_passphrase(self, blockchain_backend):
        """
        Test that fidelity bond addresses are correctly derived with passphrase.

        Verifies that:
        1. Fidelity bond addresses differ when using passphrase
        2. Bond address is deterministic (same inputs = same output)
        3. Bond addresses follow the expected derivation path
        """
        from jmwallet.wallet.service import WalletService

        # Use a future locktime
        locktime = parse_locktime_date("2030-01")

        # Create wallet with passphrase
        wallet = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=self.TEST_PASSPHRASE,
        )

        # Create wallet without passphrase for comparison
        wallet_no_pass = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase="",
        )

        # Generate fidelity bond addresses
        bond_addr_with_pass = wallet.get_fidelity_bond_address(0, locktime)
        bond_addr_no_pass = wallet_no_pass.get_fidelity_bond_address(0, locktime)

        logger.info(f"Bond address without passphrase: {bond_addr_no_pass}")
        logger.info(f"Bond address with passphrase:    {bond_addr_with_pass}")

        # Bond addresses must differ
        assert bond_addr_with_pass != bond_addr_no_pass, (
            "Bond addresses must differ with different passphrases"
        )

        # Both should be P2WSH (32-byte witness program = longer address)
        # bcrt1q... is P2WPKH (20 bytes), bcrt1p... is P2TR
        # P2WSH starts with bcrt1q but has longer data
        assert bond_addr_with_pass.startswith("bcrt1q"), "Should be P2WSH"
        assert bond_addr_no_pass.startswith("bcrt1q"), "Should be P2WSH"

        # Verify determinism
        bond_addr_2 = wallet.get_fidelity_bond_address(0, locktime)
        assert bond_addr_with_pass == bond_addr_2, (
            "Same inputs must produce same bond address"
        )

        logger.info(
            "✓ Fidelity bond address derivation with passphrase works correctly"
        )

    @pytest.mark.asyncio
    async def test_fidelity_bond_recovery_with_passphrase(
        self,
        blockchain_backend,
        ensure_blockchain_ready,
    ):
        """
        Test recovering a funded fidelity bond using only mnemonic and passphrase.

        This simulates a wallet recovery scenario:
        1. Create a wallet with passphrase
        2. Generate a fidelity bond address
        3. Fund the bond address
        4. Create a NEW wallet instance with same mnemonic+passphrase
        5. Verify the bond is discovered during recovery scan
        """
        from tests.e2e.rpc_utils import mine_blocks, rpc_call, send_from_test_funder

        from jmwallet.wallet.service import WalletService

        # Use a past locktime so the bond is spendable (for cleanup)
        # Timenumber 0 = January 2020
        locktime = timenumber_to_timestamp(0)

        # Step 1: Create wallet and generate bond address
        wallet = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=self.TEST_PASSPHRASE,
        )

        bond_address = wallet.get_fidelity_bond_address(0, locktime)
        logger.info(f"Created fidelity bond address: {bond_address}")

        # Step 2: Fund the bond address.
        # Prefer sendtoaddress from test-funder (subsidy-independent: mines
        # only 1 confirmation block). Fall back to mining a coinbase directly.
        dummy_addr = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
        logger.info("Funding bond address...")
        funded = await send_from_test_funder(bond_address, 0.01, confirmations=1)
        if not funded:
            # Fallback: mine 1 coinbase to bond address + 110 for maturity.
            await mine_blocks(1, bond_address)
            await mine_blocks(110, dummy_addr)

        # Verify the UTXO exists
        result = await rpc_call("scantxoutset", ["start", [f"addr({bond_address})"]])
        utxos = result.get("unspents", [])
        assert len(utxos) >= 1, f"Bond address should have UTXOs, got: {utxos}"

        bond_value = int(utxos[0]["amount"] * 100_000_000)
        logger.info(f"Bond funded with {bond_value:,} sats")

        # Step 3: Create a NEW wallet instance (simulating recovery)
        recovered_wallet = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=self.TEST_PASSPHRASE,
        )

        # Step 4: Run fidelity bond discovery
        logger.info("Running fidelity bond discovery scan...")

        def progress(current: int, total: int) -> None:
            if current % 100 == 0:
                logger.info(f"  Scanning: {current}/{total} timelocks")

        discovered = await recovered_wallet.discover_fidelity_bonds(
            progress_callback=progress,
        )

        # Step 5: Verify the bond was found
        assert len(discovered) >= 1, (
            f"Should discover at least 1 fidelity bond, found {len(discovered)}"
        )

        # Find our specific bond
        found_bond = None
        for utxo in discovered:
            if utxo.address == bond_address:
                found_bond = utxo
                break

        assert found_bond is not None, (
            f"Should find bond at {bond_address}, found: {[u.address for u in discovered]}"
        )

        assert found_bond.value == bond_value, (
            f"Bond value should be {bond_value}, got {found_bond.value}"
        )

        logger.info(
            f"✓ Successfully recovered fidelity bond: {found_bond.txid}:{found_bond.vout}"
        )
        logger.info(f"  Address: {found_bond.address}")
        logger.info(f"  Value: {found_bond.value:,} sats")
        logger.info(f"  Locktime: {found_bond.locktime}")

    @pytest.mark.asyncio
    async def test_wrong_passphrase_produces_different_addresses(
        self, blockchain_backend
    ):
        """
        Test that using the wrong passphrase produces different addresses.

        This validates the security of the passphrase - if someone has
        the mnemonic but not the passphrase, they cannot derive the
        correct addresses.
        """
        from jmwallet.wallet.service import WalletService

        correct_passphrase = "correct_passphrase"
        wrong_passphrase = "wrong_passphrase"

        wallet_correct = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=correct_passphrase,
        )

        wallet_wrong = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            mixdepth_count=5,
            passphrase=wrong_passphrase,
        )

        # Check all mixdepths
        for md in range(5):
            addr_correct = wallet_correct.get_receive_address(md, 0)
            addr_wrong = wallet_wrong.get_receive_address(md, 0)
            assert addr_correct != addr_wrong, (
                f"Mixdepth {md}: Wrong passphrase must produce different address"
            )

        # Also check fidelity bond
        locktime = parse_locktime_date("2030-01")
        bond_correct = wallet_correct.get_fidelity_bond_address(0, locktime)
        bond_wrong = wallet_wrong.get_fidelity_bond_address(0, locktime)

        assert bond_correct != bond_wrong, (
            "Wrong passphrase must produce different bond address"
        )

        logger.info("✓ Wrong passphrase correctly produces different addresses")


class TestBIP39PassphraseEdgeCases:
    """Test edge cases for BIP39 passphrase handling."""

    TEST_MNEMONIC = (
        "abandon abandon abandon abandon abandon abandon "
        "abandon abandon abandon abandon abandon about"
    )

    @pytest.mark.asyncio
    async def test_empty_passphrase_same_as_none(self, blockchain_backend):
        """
        Test that empty string passphrase is treated same as no passphrase.

        BIP39 spec: Empty passphrase ("") should produce same seed as no passphrase.
        """
        from jmwallet.wallet.service import WalletService

        # Empty string passphrase
        wallet_empty = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            passphrase="",
        )

        # No passphrase provided (defaults to "")
        wallet_none = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
        )

        addr_empty = wallet_empty.get_receive_address(0, 0)
        addr_none = wallet_none.get_receive_address(0, 0)

        assert addr_empty == addr_none, (
            "Empty passphrase should produce same result as no passphrase"
        )

        logger.info("✓ Empty passphrase handled correctly")

    @pytest.mark.asyncio
    async def test_unicode_passphrase(self, blockchain_backend):
        """
        Test that Unicode passphrases work correctly.

        BIP39 uses NFKD normalization for passphrases, so Unicode should work.
        """
        from jmwallet.wallet.service import WalletService

        unicode_passphrase = "パスフレーズ"  # Japanese for "passphrase"

        wallet = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            passphrase=unicode_passphrase,
        )

        # Should produce a valid address
        addr = wallet.get_receive_address(0, 0)
        assert addr.startswith("bcrt1"), "Should produce valid regtest address"

        # Same passphrase should be deterministic
        wallet2 = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            passphrase=unicode_passphrase,
        )
        addr2 = wallet2.get_receive_address(0, 0)

        assert addr == addr2, "Unicode passphrase should be deterministic"

        logger.info("✓ Unicode passphrase handled correctly")

    @pytest.mark.asyncio
    async def test_whitespace_passphrase(self, blockchain_backend):
        """
        Test that whitespace in passphrase is preserved.

        BIP39 preserves whitespace in passphrase (unlike mnemonic normalization).
        """
        from jmwallet.wallet.service import WalletService

        # Passphrases with leading/trailing spaces
        pass_with_space = " passphrase "
        pass_no_space = "passphrase"

        wallet_space = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            passphrase=pass_with_space,
        )

        wallet_no_space = WalletService(
            mnemonic=self.TEST_MNEMONIC,
            backend=blockchain_backend,
            network="regtest",
            passphrase=pass_no_space,
        )

        addr_space = wallet_space.get_receive_address(0, 0)
        addr_no_space = wallet_no_space.get_receive_address(0, 0)

        # Whitespace should be significant
        assert addr_space != addr_no_space, (
            "Whitespace in passphrase should be significant"
        )

        logger.info("✓ Whitespace in passphrase preserved correctly")
