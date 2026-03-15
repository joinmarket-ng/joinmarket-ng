"""
End-to-end integration tests for Neutrino backend.

Tests neutrino light client backend functionality:
- Basic blockchain operations (height, transactions, fees)
- UTXO discovery and watching addresses
- Maker and taker operation with neutrino backend
- Cross-backend compatibility (scantxoutset + neutrino)
- Fidelity bonds with neutrino backend

Requires: docker compose --profile neutrino up -d

The neutrino backend uses BIP157/BIP158 compact block filters for
privacy-preserving SPV operation. These tests verify that the neutrino
backend works correctly with the JoinMarket wallet implementation.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from jmcore.models import NetworkType
from jmwallet.backends.neutrino import NeutrinoBackend
from jmwallet.wallet.service import WalletService
from loguru import logger
from maker.bot import MakerBot
from maker.config import MakerConfig
from taker.config import TakerConfig
from taker.taker import Taker

# Mark all tests in this module as requiring Docker neutrino profile
pytestmark = pytest.mark.neutrino

# Test wallet mnemonics (same as in test_complete_system.py for consistency)
MAKER1_MNEMONIC = (
    "avoid whisper mesh corn already blur sudden fine planet chicken hover sniff"
)
MAKER2_MNEMONIC = (
    "minute faint grape plate stock mercy tent world space opera apple rocket"
)
TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)
GENERIC_TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture(scope="module")
def neutrino_url() -> str:
    """Neutrino server URL."""
    return "http://127.0.0.1:8335"


@pytest_asyncio.fixture
async def neutrino_backend(neutrino_url: str):
    """Create and verify neutrino backend connection."""
    backend = NeutrinoBackend(
        neutrino_url=neutrino_url,
        network="regtest",
    )

    # Verify neutrino is available
    try:
        height = await backend.get_block_height()
        if height <= 0:
            pytest.skip(f"Neutrino not ready, height: {height}")
    except Exception as e:
        pytest.skip(f"Neutrino server not available: {e}")

    yield backend
    await backend.close()


@pytest_asyncio.fixture
async def neutrino_wallet(neutrino_backend):
    """Create wallet service with neutrino backend."""
    wallet = WalletService(
        mnemonic=GENERIC_TEST_MNEMONIC,
        backend=neutrino_backend,
        network=NetworkType.REGTEST,
    )
    yield wallet


# ==============================================================================
# Basic Neutrino Backend Tests
# ==============================================================================


class TestNeutrinoBasicOperations:
    """Test basic neutrino backend operations."""

    async def test_get_block_height(self, neutrino_backend):
        """Test getting block height from neutrino."""
        height = await neutrino_backend.get_block_height()
        assert height > 0, "Block height should be positive"

    async def test_get_fee_estimate(self, neutrino_backend):
        """Test getting fee estimate from neutrino."""
        fee = await neutrino_backend.estimate_fee(target_blocks=6)
        # On regtest, fee estimation may return 0 or -1
        assert fee is not None, "Fee estimate should not be None"

    async def test_get_network(self, neutrino_backend):
        """Test network identification."""
        assert neutrino_backend.network == "regtest"


class TestNeutrinoUTXOOperations:
    """Test UTXO operations with neutrino backend."""

    async def test_get_utxos_for_address(self, neutrino_backend):
        """Test getting UTXOs for a specific address."""
        # Use a known funded address from the test setup
        # This address should have received funds from the miner
        address = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"

        utxos = await neutrino_backend.get_utxos([address])
        # May or may not have UTXOs depending on test state
        assert isinstance(utxos, list)


class TestNeutrinoWalletIntegration:
    """Test wallet operations with neutrino backend."""

    async def test_wallet_sync(self, neutrino_wallet):
        """Test wallet synchronization with neutrino."""
        # Sync should complete without error
        await neutrino_wallet.sync()
        # Balance may be 0 if wallet hasn't received funds
        balance = await neutrino_wallet.get_total_balance()
        assert balance >= 0

    async def test_derive_addresses(self, neutrino_wallet):
        """Test address derivation works with neutrino wallet."""
        address = neutrino_wallet.get_new_address(mixdepth=0)
        assert address.startswith("bcrt1")


# ==============================================================================
# Neutrino Maker/Taker Tests
# ==============================================================================


class TestNeutrinoMaker:
    """Test maker functionality with neutrino backend."""

    async def test_maker_config_with_neutrino(self):
        """Test creating maker config for neutrino backend."""
        config = MakerConfig(
            mnemonic=MAKER1_MNEMONIC,
            network="regtest",
            directory_nodes=["localhost:5222"],
            offer_fee_percentage=0.001,
            min_coinjoin_amount=100000,
        )
        assert config.network == "regtest"

    async def test_maker_initialization(self, neutrino_backend):
        """Test maker bot initialization with neutrino."""
        config = MakerConfig(
            mnemonic=MAKER1_MNEMONIC,
            network="regtest",
            directory_nodes=["localhost:5222"],
            offer_fee_percentage=0.001,
            min_coinjoin_amount=100000,
        )

        wallet = WalletService(
            mnemonic=config.mnemonic.get_secret_value(),
            backend=neutrino_backend,
            network=NetworkType.REGTEST,
        )

        # Just verify initialization works
        bot = MakerBot(wallet=wallet, backend=neutrino_backend, config=config)
        assert bot is not None


class TestNeutrinoTaker:
    """Test taker functionality with neutrino backend."""

    async def test_taker_config_with_neutrino(self):
        """Test creating taker config for neutrino backend."""
        config = TakerConfig(
            mnemonic=TAKER_MNEMONIC,
            network="regtest",
            directory_nodes=["localhost:5222"],
            coinjoin_amount=1_000_000,
            num_makers=2,
        )
        assert config.network == "regtest"

    async def test_taker_initialization(self, neutrino_backend):
        """Test taker initialization with neutrino."""
        config = TakerConfig(
            mnemonic=TAKER_MNEMONIC,
            network="regtest",
            directory_nodes=["localhost:5222"],
            coinjoin_amount=1_000_000,
            num_makers=2,
        )

        wallet = WalletService(
            mnemonic=config.mnemonic.get_secret_value(),
            backend=neutrino_backend,
            network=NetworkType.REGTEST,
        )

        taker = Taker(wallet=wallet, backend=neutrino_backend, config=config)
        assert taker is not None


# ==============================================================================
# Cross-Backend Compatibility Tests
# ==============================================================================


class TestCrossBackendCompatibility:
    """Test that operations work identically across backends."""

    @pytest.fixture
    def bitcoin_rpc_config(self):
        """Bitcoin Core RPC configuration."""
        import os

        return {
            "rpc_url": os.environ.get("BITCOIN_RPC_URL", "http://127.0.0.1:18443"),
            "rpc_user": os.environ.get("BITCOIN_RPC_USER", "test"),
            "rpc_password": os.environ.get("BITCOIN_RPC_PASSWORD", "test"),
        }

    @pytest_asyncio.fixture
    async def bitcoin_core_backend(self, bitcoin_rpc_config):
        """Bitcoin Core backend for comparison."""
        from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

        backend = BitcoinCoreBackend(
            rpc_url=bitcoin_rpc_config["rpc_url"],
            rpc_user=bitcoin_rpc_config["rpc_user"],
            rpc_password=bitcoin_rpc_config["rpc_password"],
        )

        try:
            await backend.get_block_height()
        except Exception as e:
            pytest.skip(f"Bitcoin Core not available: {e}")

        yield backend
        await backend.close()

    async def test_block_height_matches(self, neutrino_backend, bitcoin_core_backend):
        """Test that block height is consistent across backends."""
        neutrino_height = await neutrino_backend.get_block_height()
        core_height = await bitcoin_core_backend.get_block_height()

        # Allow for slight sync delay (neutrino may be 1-2 blocks behind)
        assert abs(neutrino_height - core_height) <= 2


# ==============================================================================
# End-to-End CoinJoin with Neutrino (requires full setup)
# ==============================================================================


class TestNeutrinoCoinJoin:
    """Full CoinJoin test with neutrino backend.

    This requires the full e2e Docker setup with neutrino profile:
    docker compose --profile neutrino up -d
    """

    @pytest.mark.slow
    async def test_coinjoin_with_neutrino_maker(
        self, neutrino_backend, fresh_docker_makers
    ):
        """Test that a maker using neutrino can participate in CoinJoin.

        This test verifies:
        - Neutrino backend is operational
        - Docker neutrino maker (jm-maker-neutrino) is running and has offers
        - Taker can initiate CoinJoin with the neutrino-based maker
        - Complete CoinJoin transaction succeeds

        The fresh_docker_makers fixture clears both maker commitment blacklists
        and the taker's used commitments to ensure fresh PoDLE indices are available.

        Requires: docker compose --profile neutrino up -d
        """
        import asyncio
        import subprocess

        from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

        from tests.e2e.rpc_utils import ensure_wallet_funded, mine_blocks

        # Verify neutrino is synced
        height = await neutrino_backend.get_block_height()
        if height < 100:
            pytest.skip("Need sufficient blockchain height for coinbase maturity")

        # Check if Docker neutrino maker is running
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", "jm-maker-neutrino"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip() != "true":
                pytest.skip(
                    "Docker neutrino maker not running. Start with: "
                    "docker compose --profile neutrino up -d"
                )
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ):
            pytest.skip("Docker not available or neutrino maker not running")

        # Create Bitcoin Core backend for taker
        bitcoin_backend = BitcoinCoreBackend(
            rpc_url="http://127.0.0.1:18443",
            rpc_user="test",
            rpc_password="test",
        )

        try:
            # Verify Bitcoin Core is available
            core_height = await bitcoin_backend.get_block_height()
            logger.info(
                f"Bitcoin Core height: {core_height}, Neutrino height: {height}"
            )
        except Exception as e:
            pytest.skip(f"Bitcoin Core not available: {e}")

        # Create taker wallet with Bitcoin Core backend
        taker_wallet = WalletService(
            mnemonic=TAKER_MNEMONIC,
            backend=bitcoin_backend,
            network=NetworkType.REGTEST,
        )

        # Sync taker wallet
        logger.info("Syncing taker wallet (bitcoin core)...")
        await taker_wallet.sync()
        taker_balance = await taker_wallet.get_total_balance()
        logger.info(f"Taker balance: {taker_balance:,} sats")

        # Fund taker wallet if needed
        min_balance = 100_000_000  # 1 BTC minimum
        if taker_balance < min_balance:
            logger.info("Funding taker wallet...")
            taker_addr = taker_wallet.get_new_address(mixdepth=0)
            logger.info(f"Taker address for funding: {taker_addr}")
            funded = await ensure_wallet_funded(taker_addr, confirmations=2)
            if funded:
                await taker_wallet.sync()
                taker_balance = await taker_wallet.get_total_balance()
                logger.info(f"Taker balance after funding: {taker_balance:,} sats")

        # Verify we have enough funds
        if taker_balance < min_balance:
            await taker_wallet.close()
            await bitcoin_backend.close()
            pytest.skip(
                f"Taker needs at least {min_balance:,} sats, has {taker_balance:,} sats"
            )

        # Mine some blocks to ensure coinbase maturity
        logger.info("Mining blocks for coinbase maturity...")
        await mine_blocks(10, "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080")

        # Create taker with Bitcoin Core backend
        # Note: Uses TESTNET for protocol network (directory handshakes) but
        # wallet was created with REGTEST for bitcoin address generation
        taker_config = TakerConfig(
            mnemonic=TAKER_MNEMONIC,
            network=NetworkType.TESTNET,  # Protocol network for directory server
            directory_servers=["127.0.0.1:5222"],
            coinjoin_amount=50_000_000,  # 0.5 BTC
            counterparty_count=1,  # Only need 1 maker for this test
            minimum_makers=1,  # Allow single maker CoinJoin
        )

        taker = Taker(
            wallet=taker_wallet,
            backend=bitcoin_backend,
            config=taker_config,
        )

        try:
            # Start taker
            logger.info("Starting taker with Bitcoin Core backend...")
            await taker.start()

            # Wait for directory server and makers to be ready
            await asyncio.sleep(15)

            # Fetch orderbook
            logger.info("Fetching orderbook...")
            offers = await taker.directory_client.fetch_orderbook(
                max_wait=15.0, min_wait=15.0, quiet_period=0.0
            )
            logger.info(f"Found {len(offers)} offers in orderbook")

            if len(offers) < 1:
                logger.warning("No offers found from neutrino maker")
                pytest.skip(
                    "No offers available. Ensure jm-maker-neutrino container is running "
                    "and has funds"
                )

            # Filter for neutrino maker offers (if we can identify them)
            logger.info(f"Available offers: {[o.counterparty for o in offers]}")

            # Update orderbook
            taker.orderbook_manager.update_offers(offers)

            # Get destination address
            dest_address = taker_wallet.get_new_address(mixdepth=1)
            logger.info(f"Destination address: {dest_address}")

            # Execute CoinJoin
            cj_amount = 20_000_000  # 0.2 BTC
            logger.info(f"Initiating CoinJoin for {cj_amount:,} sats...")

            txid = await taker.do_coinjoin(
                amount=cj_amount,
                destination=dest_address,
                mixdepth=0,
                counterparty_count=1,
            )

            # Verify success
            if txid:
                logger.info(f"CoinJoin successful! txid: {txid}")

                # Wait for transaction to be broadcast and confirmed
                logger.info("Waiting for transaction to be broadcast and confirmed...")
                # The taker returns txid immediately after receiving signatures.
                # We check mempool and mine manually if found, or wait for confirmation.
                import httpx

                max_retries = 30  # Up to ~1.5 minutes total wait time
                retry_delay = 3  # Check every 3 seconds
                found = False

                for attempt in range(max_retries):
                    await asyncio.sleep(retry_delay)

                    # 1. Check if already confirmed
                    try:
                        tx_info = await bitcoin_backend.get_transaction(txid)
                        if tx_info is not None and tx_info.confirmations > 0:
                            logger.info(
                                f"Transaction confirmed with {tx_info.confirmations} "
                                f"confirmation(s) after {(attempt + 1) * retry_delay}s"
                            )
                            found = True
                            break
                    except Exception:
                        pass  # Tx might not be known yet

                    # 2. Check mempool and mine if found
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            response = await client.post(
                                "http://127.0.0.1:18443",
                                auth=("test", "test"),
                                json={
                                    "jsonrpc": "1.0",
                                    "id": "test",
                                    "method": "getrawmempool",
                                    "params": [],
                                },
                            )
                            result = response.json()
                            mempool = result.get("result", []) if result else []

                            if txid in mempool:
                                logger.info(
                                    "Transaction found in mempool, mining block..."
                                )
                                await mine_blocks(1, dest_address)
                                # Next loop iteration will find it confirmed
                                continue
                    except Exception as e:
                        logger.warning(f"Failed to check mempool: {e}")

                    if (attempt + 1) % 5 == 0:
                        logger.info(
                            f"Still waiting... ({(attempt + 1) * retry_delay}s elapsed)"
                        )

                if not found:
                    raise AssertionError(
                        f"Transaction {txid} not confirmed after {max_retries * retry_delay}s. "
                        f"This indicates the makers failed to broadcast the transaction."
                    )

                logger.info(
                    "CoinJoin with neutrino-based maker completed successfully!"
                )
            else:
                pytest.fail("CoinJoin failed to return a txid")

        finally:
            # Cleanup
            logger.info("Stopping taker...")
            await taker.stop()
            await taker_wallet.close()
            await bitcoin_backend.close()

    @pytest.mark.slow
    async def test_coinjoin_with_neutrino_taker(
        self, neutrino_backend, fresh_docker_makers
    ):
        """Test that a taker using neutrino can initiate CoinJoin.

        This test verifies:
        - Neutrino backend works for taker operations
        - Taker can sync wallet, select UTXOs, and build transactions with neutrino
        - Complete CoinJoin transaction succeeds with neutrino-based taker
        - Docker Bitcoin Core maker is running and can participate

        This complements test_coinjoin_with_neutrino_maker by testing the
        opposite configuration: neutrino taker + Bitcoin Core maker.

        The fresh_docker_makers fixture clears both maker commitment blacklists
        and the taker's used commitments to ensure fresh PoDLE indices are available.

        Requires: docker compose --profile neutrino up -d (for both neutrino backend
        and jm-maker1/jm-maker2 makers)
        """
        import asyncio
        import subprocess

        from tests.e2e.rpc_utils import ensure_wallet_funded, mine_blocks

        # Verify neutrino is synced
        height = await neutrino_backend.get_block_height()
        if height < 100:
            pytest.skip("Need sufficient blockchain height for coinbase maturity")

        # Check if Docker makers are running (we'll use Bitcoin Core-based makers)
        try:
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", "jm-maker1"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip() != "true":
                pytest.skip(
                    "Docker maker1 not running. Start with: "
                    "docker compose --profile e2e up -d"
                )
        except (
            subprocess.TimeoutExpired,
            FileNotFoundError,
            subprocess.CalledProcessError,
        ):
            pytest.skip("Docker not available or makers not running")

        logger.info("Docker makers are running, proceeding with neutrino taker test")

        # Create taker wallet with neutrino backend
        taker_wallet = WalletService(
            mnemonic=TAKER_MNEMONIC,
            backend=neutrino_backend,
            network=NetworkType.REGTEST,
        )

        # Sync taker wallet
        logger.info("Syncing taker wallet (neutrino)...")
        await taker_wallet.sync()
        taker_balance = await taker_wallet.get_total_balance()
        logger.info(f"Taker balance: {taker_balance:,} sats")

        # Fund taker wallet if needed
        min_balance = 100_000_000  # 1 BTC minimum
        if taker_balance < min_balance:
            logger.info("Funding taker wallet with neutrino backend...")
            taker_addr = taker_wallet.get_new_address(mixdepth=0)
            logger.info(f"Taker address for funding: {taker_addr}")
            funded = await ensure_wallet_funded(taker_addr, confirmations=2)
            if funded:
                # Give neutrino time to sync the new blocks
                logger.info("Waiting for neutrino to sync new blocks...")
                await asyncio.sleep(10)

                # Re-sync wallet multiple times if needed
                for i in range(5):
                    await taker_wallet.sync()
                    taker_balance = await taker_wallet.get_total_balance()
                    logger.info(
                        f"Taker balance after funding (attempt {i + 1}): {taker_balance:,} sats"
                    )
                    if taker_balance >= min_balance:
                        break
                    logger.info("Balance still low, waiting and retrying...")
                    await asyncio.sleep(5)

        # Verify we have enough funds
        if taker_balance < min_balance:
            await taker_wallet.close()
            pytest.skip(
                f"Taker needs at least {min_balance:,} sats, has {taker_balance:,} sats. "
                "Neutrino backend may need more time to sync, or funding failed."
            )

        logger.info(
            f"Taker wallet funded with {taker_balance:,} sats via neutrino backend"
        )

        # Mine some blocks to ensure coinbase maturity for makers
        logger.info("Mining blocks for coinbase maturity...")
        await mine_blocks(10, "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080")

        # Create taker with neutrino backend
        # Note: Uses TESTNET for protocol network (directory handshakes)
        taker_config = TakerConfig(
            mnemonic=TAKER_MNEMONIC,
            network=NetworkType.TESTNET,  # Protocol network for directory server
            directory_servers=["127.0.0.1:5222"],
            coinjoin_amount=50_000_000,  # 0.5 BTC
            counterparty_count=1,  # Only need 1 maker for this test
            minimum_makers=1,  # Allow single maker CoinJoin
        )

        taker = Taker(
            wallet=taker_wallet,
            backend=neutrino_backend,
            config=taker_config,
        )

        try:
            # Start taker
            logger.info("Starting taker with neutrino backend...")
            await taker.start()

            # Wait for directory server and makers to be ready
            await asyncio.sleep(15)

            # Fetch orderbook
            logger.info("Fetching orderbook...")
            offers = await taker.directory_client.fetch_orderbook(
                max_wait=15.0, min_wait=15.0, quiet_period=0.0
            )
            logger.info(f"Found {len(offers)} offers in orderbook")

            if len(offers) < 1:
                logger.warning("No offers found from makers")
                pytest.skip(
                    "No offers available. Ensure Docker makers are running and have funds"
                )

            # Log available offers
            logger.info(f"Available offers: {[o.counterparty for o in offers]}")

            # Update orderbook
            taker.orderbook_manager.update_offers(offers)

            # Get destination address (using neutrino backend)
            dest_address = taker_wallet.get_new_address(mixdepth=1)
            logger.info(f"Destination address (neutrino): {dest_address}")

            # Execute CoinJoin with neutrino taker
            cj_amount = 20_000_000  # 0.2 BTC
            logger.info(
                f"Initiating CoinJoin for {cj_amount:,} sats with neutrino taker..."
            )

            txid = await taker.do_coinjoin(
                amount=cj_amount,
                destination=dest_address,
                mixdepth=0,
                counterparty_count=1,
            )

            # Verify success
            if txid:
                logger.info(f"CoinJoin successful with neutrino taker! txid: {txid}")

                # Verify transaction using Bitcoin Core to ensure it was actually broadcast
                # Even though we're testing neutrino taker, Bitcoin Core should see the tx
                # because the makers broadcast to Bitcoin Core
                from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

                bitcoin_backend = BitcoinCoreBackend(
                    rpc_url="http://127.0.0.1:18443",
                    rpc_user="test",
                    rpc_password="test",
                )
                try:
                    logger.info(
                        "Waiting for transaction to be broadcast and confirmed..."
                    )
                    # The taker returns txid immediately after receiving signatures,
                    # but makers receive !push and broadcast asynchronously (~60s later).
                    # We check mempool and mine manually if found, or wait for confirmation.
                    import httpx

                    max_retries = 30  # Up to ~1.5 minutes total wait time
                    retry_delay = 3  # Check every 3 seconds
                    found = False

                    for attempt in range(max_retries):
                        await asyncio.sleep(retry_delay)

                        # 1. Check if already confirmed
                        try:
                            tx_info = await bitcoin_backend.get_transaction(txid)
                            if tx_info is not None and tx_info.confirmations > 0:
                                logger.info(
                                    f"Transaction confirmed with {tx_info.confirmations} "
                                    f"confirmation(s) after {(attempt + 1) * retry_delay}s"
                                )
                                found = True
                                break
                        except Exception:
                            pass  # Tx might not be known yet

                        # 2. Check mempool and mine if found
                        try:
                            async with httpx.AsyncClient(timeout=5.0) as client:
                                response = await client.post(
                                    "http://127.0.0.1:18443",
                                    auth=("test", "test"),
                                    json={
                                        "jsonrpc": "1.0",
                                        "id": "test",
                                        "method": "getrawmempool",
                                        "params": [],
                                    },
                                )
                                result = response.json()
                                mempool = result.get("result", []) if result else []

                                if txid in mempool:
                                    logger.info(
                                        "Transaction found in mempool, mining block..."
                                    )
                                    await mine_blocks(1, dest_address)
                                    # Next loop iteration will find it confirmed
                                    continue
                        except Exception as e:
                            logger.warning(f"Failed to check mempool: {e}")

                        if (attempt + 1) % 5 == 0:
                            logger.info(
                                f"Still waiting... ({(attempt + 1) * retry_delay}s elapsed)"
                            )

                    if not found:
                        raise AssertionError(
                            f"Transaction {txid} not confirmed after {max_retries * retry_delay}s. "
                            f"This indicates the makers failed to broadcast the transaction."
                        )

                    # Now verify the transaction is confirmed
                    logger.info("Verifying transaction on Bitcoin Core...")
                    tx_info = await bitcoin_backend.get_transaction(txid)
                    assert tx_info is not None, (
                        f"Transaction {txid} should exist on Bitcoin Core after mining"
                    )
                    assert tx_info.confirmations >= 1, (
                        f"Transaction should have at least 1 confirmation, "
                        f"got {tx_info.confirmations}"
                    )
                    logger.info(
                        f"Transaction confirmed on Bitcoin Core: {tx_info.confirmations} confirmations"
                    )

                    # Diagnostic: Verify the destination address received funds via Bitcoin Core
                    # This confirms the CoinJoin output is at the expected address
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        response = await client.post(
                            "http://127.0.0.1:18443",
                            auth=("test", "test"),
                            json={
                                "jsonrpc": "1.0",
                                "id": "test",
                                "method": "scantxoutset",
                                "params": ["start", [f"addr({dest_address})"]],
                            },
                        )
                        result = response.json()
                        unspents = result.get("result", {}).get("unspents", [])
                        total_amount = result.get("result", {}).get("total_amount", 0)
                        logger.info(
                            f"Bitcoin Core scan for {dest_address}: "
                            f"{len(unspents)} UTXO(s), total {total_amount} BTC"
                        )
                        if unspents:
                            for u in unspents:
                                logger.info(
                                    f"  UTXO: {u.get('txid')}:{u.get('vout')} = {u.get('amount')} BTC"
                                )
                finally:
                    await bitcoin_backend.close()

                # Verify on neutrino backend
                logger.info("Waiting for neutrino to sync new block...")
                await asyncio.sleep(5)
                neutrino_height = await neutrino_backend.get_block_height()
                logger.info(f"Neutrino height after CoinJoin: {neutrino_height}")

                # Re-sync taker wallet to see the new balance
                logger.info("Re-syncing taker wallet after CoinJoin...")

                # Retry loop for balance check (neutrino might be slow to update)
                max_retries = 10
                retry_delay = 2
                mixdepth1_balance = 0

                for attempt in range(max_retries):
                    await taker_wallet.sync()
                    mixdepth1_balance = await taker_wallet.get_balance(mixdepth=1)

                    if mixdepth1_balance > 0:
                        break

                    logger.info(
                        f"Balance is 0, retrying sync in {retry_delay}s... ({attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(retry_delay)

                new_balance = await taker_wallet.get_total_balance()
                logger.info(f"Taker new balance: {new_balance:,} sats")

                # Verify mixdepth 1 (destination) received funds
                logger.info(
                    f"Mixdepth 1 balance after CoinJoin: {mixdepth1_balance:,} sats"
                )
                assert mixdepth1_balance > 0, (
                    f"Destination mixdepth should have received CoinJoin output. "
                    f"Balance: {mixdepth1_balance:,} sats"
                )

                logger.info(
                    f"CoinJoin with neutrino-based taker completed successfully! "
                    f"Received {mixdepth1_balance:,} sats in destination mixdepth"
                )
            else:
                pytest.fail("CoinJoin failed to return a txid")

        finally:
            # Cleanup
            logger.info("Stopping taker...")
            await taker.stop()
            await taker_wallet.close()
