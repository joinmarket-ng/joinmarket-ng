"""
End-to-end tests for transaction history recording.

Tests that maker and taker properly record CoinJoin transactions in their history
files during real CoinJoin operations.

Requires: docker compose --profile e2e up -d
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from jmwallet.history import (
    append_history_entry,
    create_taker_history_entry,
    get_used_addresses,
    read_history,
)
from jmwallet.wallet.service import WalletService

# Mark all tests in this module as requiring Docker e2e profile
pytestmark = pytest.mark.e2e

TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)


@pytest.mark.asyncio
async def test_history_recording_mechanism(bitcoin_core_backend):
    """
    Test that history recording works correctly.

    This test verifies the history recording mechanism without requiring
    a full CoinJoin to complete. It tests:
    - History entry creation
    - History file persistence
    - Address tracking
    """
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)

        # Create a wallet
        wallet = WalletService(
            mnemonic=TAKER_MNEMONIC,
            backend=bitcoin_core_backend,
            network="regtest",
            mixdepth_count=5,
        )

        try:
            await wallet.sync_all()

            # Get some addresses from the wallet
            dest_address = wallet.get_receive_address(1, 0)
            change_address = wallet.get_receive_address(0, 1)

            # Create a history entry manually (simulating successful CoinJoin)
            history_entry = create_taker_history_entry(
                maker_nicks=["J5Maker1Test1234", "J5Maker2Test5678"],
                cj_amount=50_000_000,
                total_maker_fees=5000,
                mining_fee=1500,
                destination=dest_address,
                change_address=change_address,
                source_mixdepth=0,
                selected_utxos=[("a" * 64, 0)],
                txid="b" * 64,
                network="regtest",
                success=True,
                failure_reason="",
            )

            # Write to history
            append_history_entry(history_entry, data_dir=data_dir)

            # Verify history file was created
            history_file = data_dir / "coinjoin_history.csv"
            assert history_file.exists(), "History file should be created"

            # Read back and verify
            entries = read_history(data_dir=data_dir)
            assert len(entries) == 1, "Should have one history entry"

            entry = entries[0]
            assert entry.txid == "b" * 64
            assert entry.role == "taker"
            assert entry.cj_amount == 50_000_000
            assert entry.destination_address == dest_address
            assert entry.change_address == change_address
            assert entry.peer_count == 2

            # Verify address tracking
            used = get_used_addresses(data_dir=data_dir)
            assert dest_address in used, "Destination address should be tracked"
            assert change_address in used, "Change address should be tracked"

            print("✓ History recording mechanism verified:")
            print(f"  - History file: {history_file}")
            print(f"  - Entries: {len(entries)}")
            print(f"  - Tracked addresses: {len(used)}")
            print(f"  - Destination: {dest_address}")
            print(f"  - Change: {change_address}")

        finally:
            await wallet.close()


@pytest.mark.asyncio
@pytest.mark.slow
async def test_coinjoin_creates_history_entry(
    bitcoin_core_backend,
    fresh_docker_makers,
):
    """
    Test that a complete CoinJoin properly records history.

    This test performs a real CoinJoin with Docker makers and verifies
    that the taker's history file is properly updated.

    Requires: docker compose --profile e2e up -d

    Note: This test may fail if CoinJoin execution fails, which can happen
    due to various reasons (maker funds, network issues, etc.). The test
    test_history_recording_mechanism verifies the core functionality.
    """
    import subprocess

    from jmcore.models import NetworkType
    from taker.config import TakerConfig
    from taker.taker import Taker
    from tests.e2e.rpc_utils import mine_blocks

    # Check if Docker makers are running
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "jm-maker1"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip() != "true":
            pytest.skip("Docker maker1 not running")
    except Exception:
        pytest.skip("Docker not available or makers not running")

    # Create temporary data directory
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        data_dir = Path(temp_dir)

        # Create taker config with temp data dir
        config = TakerConfig(
            mnemonic=TAKER_MNEMONIC,
            network=NetworkType.TESTNET,
            bitcoin_network=NetworkType.REGTEST,
            backend_type="scantxoutset",
            backend_config={
                "rpc_url": "http://127.0.0.1:18443",
                "rpc_user": "test",
                "rpc_password": "test",
            },
            directory_servers=["127.0.0.1:5222"],
            counterparty_count=2,
            minimum_makers=2,
            data_dir=str(data_dir),
        )

        # Mine blocks for coinbase maturity
        addr = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
        await mine_blocks(10, addr)

        # Create taker wallet
        taker_wallet = WalletService(
            mnemonic=TAKER_MNEMONIC,
            backend=bitcoin_core_backend,
            network="regtest",
            mixdepth_count=5,
        )

        await taker_wallet.sync_all()
        taker_balance = await taker_wallet.get_total_balance()

        if taker_balance < 100_000_000:
            await taker_wallet.close()
            pytest.skip("Taker needs at least 1 BTC")

        # Create taker
        taker = Taker(taker_wallet, bitcoin_core_backend, config)

        try:
            # Start taker
            await taker.start()

            # Fetch orderbook
            offers = await taker.directory_client.fetch_orderbook(
                max_wait=15.0, min_wait=15.0, quiet_period=0.0
            )
            if len(offers) < 2:
                pytest.skip(f"Need at least 2 offers, found {len(offers)}")

            taker.orderbook_manager.update_offers(offers)

            # Get destination address
            dest_address = taker_wallet.get_receive_address(1, 0)

            # Perform CoinJoin
            cj_amount = 50_000_000
            print(f"Initiating CoinJoin for {cj_amount:,} sats...")
            print(f"Taker balance: {taker_balance:,} sats")
            print(f"Available offers: {len(offers)}")

            txid = await taker.do_coinjoin(
                amount=cj_amount,
                destination=dest_address,
                mixdepth=0,
            )

            if txid is None:
                # CoinJoin failed - check if history recorded the failure
                print("CoinJoin returned None (failed)")

                # Wait for history to be written
                await asyncio.sleep(2)

                # Check if failure was recorded in history
                history_file = data_dir / "coinjoin_history.csv"
                if history_file.exists():
                    entries = read_history(data_dir=data_dir)
                    print(f"History entries after failed CoinJoin: {len(entries)}")
                    for entry in entries:
                        print(f"  - Entry: success={entry.success}, txid={entry.txid}")

                pytest.skip(
                    "CoinJoin failed to complete. This may be due to maker issues, "
                    "insufficient funds, or network problems. The core history recording "
                    "functionality is tested in test_history_recording_mechanism."
                )

            print(f"CoinJoin successful! txid: {txid}")

            # Mine a block to confirm
            await mine_blocks(1, dest_address)

            # Wait for history to be written
            await asyncio.sleep(2)

            # Verify history was recorded
            history_file = data_dir / "coinjoin_history.csv"
            assert history_file.exists(), "History file should be created"

            entries = read_history(data_dir=data_dir)
            assert len(entries) > 0, "Should have at least one history entry"

            # Find the CoinJoin entry
            cj_entry = next((e for e in entries if e.txid == txid), None)
            assert cj_entry is not None, f"Should have history entry for txid {txid}"

            # Verify entry details
            assert cj_entry.role == "taker", "Should be a taker entry"
            assert cj_entry.cj_amount == cj_amount, "CJ amount should match"
            # Entry is initially marked as pending (success=False)
            # Need to update after confirmation
            assert cj_entry.peer_count == 2, "Should have 2 peers"
            assert cj_entry.destination_address is not None, "Should have destination"

            # Update confirmation status after mining a block
            from jmwallet.history import update_transaction_confirmation

            updated = update_transaction_confirmation(
                txid=txid,
                confirmations=1,
                data_dir=data_dir,
            )
            assert updated, "Should update history entry"

            # Re-read and verify entry is now marked as successful
            entries = read_history(data_dir=data_dir)
            cj_entry = next((e for e in entries if e.txid == txid), None)
            assert cj_entry is not None, f"Should have history entry for txid {txid}"
            assert cj_entry.success is True, (
                "CoinJoin should be marked successful after confirmation"
            )

            print("✓ Complete CoinJoin history recording verified:")
            print(f"  - TXID: {cj_entry.txid}")
            print(f"  - Amount: {cj_entry.cj_amount:,} sats")
            print(f"  - Peers: {cj_entry.peer_count}")

            # Verify address tracking
            used = get_used_addresses(data_dir=data_dir)

            assert cj_entry.destination_address in used, "Destination should be tracked"
            print(f"  - Tracked addresses: {len(used)}")

        finally:
            await taker.stop()
            await taker_wallet.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
