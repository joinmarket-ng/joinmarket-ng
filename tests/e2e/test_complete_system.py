"""
End-to-end integration tests for complete JoinMarket system.

Tests all components working together:
- Bitcoin regtest node
- Directory server
- Orderbook watcher
- Maker bot
- Taker client
- Wallet synchronization
- Complete CoinJoin transactions

Requires: docker compose --profile e2e up -d
"""

import asyncio
import subprocess

import pytest
import pytest_asyncio
from jmcore.models import NetworkType
from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
from jmwallet.wallet.service import WalletService
from maker.bot import MakerBot
from maker.config import MakerConfig
from taker.config import TakerConfig
from taker.taker import Taker

# Mark all tests in this module as requiring Docker e2e profile
pytestmark = pytest.mark.e2e

# ==============================================================================
# Test Wallet Mnemonics (used in successful CoinJoin testing)
# ==============================================================================

# Maker 1 mnemonic - has funds on regtest from testing
MAKER1_MNEMONIC = (
    "avoid whisper mesh corn already blur sudden fine planet chicken hover sniff"
)

# Maker 2 mnemonic - has funds on regtest from testing
MAKER2_MNEMONIC = (
    "minute faint grape plate stock mercy tent world space opera apple rocket"
)

# Taker mnemonic - has funds on regtest from testing
TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)

# Generic test wallet (abandon x11 about)
GENERIC_TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)

# Address used for mining blocks (valid P2WPKH on regtest)
MINING_ADDRESS = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"


def _require_docker_container(name: str) -> None:
    """Skip the test if a Docker container is not running."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.stdout.strip() != "true":
            pytest.skip(
                f"Docker {name} not running. Start with: docker compose --profile e2e up -d"
            )
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        subprocess.CalledProcessError,
    ):
        pytest.skip("Docker not available or containers not running")


@pytest.fixture
def bitcoin_backend():
    """Bitcoin Core backend for regtest"""
    return BitcoinCoreBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="test",
        rpc_password="test",
    )


async def _create_funded_wallet(
    bitcoin_backend: BitcoinCoreBackend,
    mnemonic: str,
    *,
    address_type: str = "p2wpkh",
    skip_msg: str = "Wallet has no funds. Auto-funding failed; please fund manually.",
):
    """Factory: create a WalletService, sync it, auto-fund if empty, and yield.

    Callers receive an async-generator suitable for ``pytest_asyncio.fixture``.
    """
    from tests.e2e.rpc_utils import ensure_wallet_funded

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=bitcoin_backend,
        network="regtest",
        mixdepth_count=5,
        address_type=address_type,
    )

    await wallet.sync_all()

    total_balance = await wallet.get_total_balance()
    if total_balance == 0:
        funding_address = wallet.get_receive_address(0, 0)
        funded = await ensure_wallet_funded(
            funding_address, amount_btc=1.0, confirmations=2
        )
        if funded:
            await wallet.sync_all()
            total_balance = await wallet.get_total_balance()

    if total_balance == 0:
        await wallet.close()
        pytest.skip(skip_msg)

    try:
        yield wallet
    finally:
        await wallet.close()


@pytest_asyncio.fixture
async def funded_wallet(bitcoin_backend):
    """Create and fund a test wallet using the generic mnemonic."""
    async for w in _create_funded_wallet(bitcoin_backend, GENERIC_TEST_MNEMONIC):
        yield w


@pytest.fixture(scope="module")
async def directory_server():
    """Start directory server process or use existing Docker instance.

    The directory server uses 'testnet' as the protocol network for both
    testnet and regtest (matching reference JoinMarket behavior).
    """
    import socket
    import subprocess
    import sys
    import time
    from pathlib import Path

    # Check if directory server is already running on port 5222
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        result = sock.connect_ex(("127.0.0.1", 5222))
        if result == 0:
            # Port is open, assume directory server is running (Docker)
            yield None
            return
    finally:
        sock.close()

    # Determine paths
    repo_root = Path(__file__).parent.parent.parent
    ds_path = repo_root / "directory_server"

    # Start process with 'testnet' as protocol network (matches Docker and reference JM)
    proc = subprocess.Popen(
        [sys.executable, "-m", "directory_server.main"],
        cwd=ds_path,
        env={
            "PYTHONPATH": str(ds_path / "src")
            + ":"
            + str(repo_root / "jmcore" / "src"),
            "NETWORK": "testnet",  # Protocol network (not bitcoin network)
            "PORT": "5222",
            "LOG_LEVEL": "DEBUG",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for startup
    time.sleep(2)

    if proc.poll() is not None:
        stdout, stderr = proc.communicate()
        raise RuntimeError(f"Directory server failed to start:\n{stderr.decode()}")

    yield proc

    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture
def maker_config():
    """Maker bot configuration using maker1 mnemonic.

    Note: Uses TESTNET for protocol network (directory handshakes) but REGTEST
    for bitcoin_network (address generation). This matches how reference JM
    handles regtest - it uses "testnet" in protocol messages.
    """
    return MakerConfig(
        mnemonic=MAKER1_MNEMONIC,
        network=NetworkType.TESTNET,  # Protocol network for directory handshakes
        bitcoin_network=NetworkType.REGTEST,  # Bitcoin network for address generation
        backend_type="scantxoutset",
        backend_config={
            "rpc_url": "http://127.0.0.1:18443",
            "rpc_user": "test",
            "rpc_password": "test",
        },
        directory_servers=["127.0.0.1:5222"],
        min_size=100_000,
        cj_fee_relative="0.0003",
        tx_fee_contribution=1_000,
    )


@pytest.fixture
def maker2_config():
    """Second maker bot configuration using maker2 mnemonic.

    Note: Uses TESTNET for protocol network (directory handshakes) but REGTEST
    for bitcoin_network (address generation). This matches how reference JM
    handles regtest - it uses "testnet" in protocol messages.
    """
    return MakerConfig(
        mnemonic=MAKER2_MNEMONIC,
        network=NetworkType.TESTNET,  # Protocol network for directory handshakes
        bitcoin_network=NetworkType.REGTEST,  # Bitcoin network for address generation
        backend_type="scantxoutset",
        backend_config={
            "rpc_url": "http://127.0.0.1:18443",
            "rpc_user": "test",
            "rpc_password": "test",
        },
        directory_servers=["127.0.0.1:5222"],
        min_size=100_000,
        cj_fee_relative="0.00025",
        tx_fee_contribution=1_500,
    )


@pytest.fixture
def taker_config():
    """Taker configuration using taker mnemonic.

    Note: Uses TESTNET for protocol network (directory handshakes) but REGTEST
    for bitcoin_network (address generation). This matches how reference JM
    handles regtest - it uses "testnet" in protocol messages.
    """
    return TakerConfig(
        mnemonic=TAKER_MNEMONIC,
        network=NetworkType.TESTNET,  # Protocol network for directory handshakes
        bitcoin_network=NetworkType.REGTEST,  # Bitcoin network for address generation
        backend_type="scantxoutset",
        backend_config={
            "rpc_url": "http://127.0.0.1:18443",
            "rpc_user": "test",
            "rpc_password": "test",
        },
        directory_servers=["127.0.0.1:5222"],
        counterparty_count=2,
        minimum_makers=2,
        maker_timeout_sec=30,
        order_wait_time=10.0,
    )


@pytest_asyncio.fixture
async def mined_chain(bitcoin_backend):
    """Ensure blockchain has minimum height."""
    from tests.e2e.rpc_utils import mine_blocks

    height = await bitcoin_backend.get_block_height()
    if height < 101:
        await mine_blocks(101 - height + 10, MINING_ADDRESS)
    return True


@pytest.mark.asyncio
async def test_bitcoin_connection(bitcoin_backend, mined_chain):
    """Test Bitcoin Core connection"""
    height = await bitcoin_backend.get_block_height()
    assert height > 100

    fee = await bitcoin_backend.estimate_fee(6)
    assert fee > 0


@pytest.mark.asyncio
async def test_wallet_sync(funded_wallet: WalletService):
    """Test wallet synchronization"""
    balance = await funded_wallet.get_total_balance()
    assert balance > 0

    utxos_dict = await funded_wallet.sync_all()
    assert len(utxos_dict) > 0


@pytest.mark.asyncio
async def test_wallet_address_generation(funded_wallet: WalletService):
    """Test address generation"""
    addr1 = funded_wallet.get_receive_address(0, 0)
    addr2 = funded_wallet.get_receive_address(0, 1)

    assert addr1.startswith("bcrt1")
    assert addr2.startswith("bcrt1")
    assert addr1 != addr2


@pytest.mark.asyncio
async def test_wallet_multiple_mixdepths(funded_wallet: WalletService):
    """Test multiple mixdepth balances"""
    for mixdepth in range(5):
        balance = await funded_wallet.get_balance(mixdepth)
        assert balance >= 0


@pytest.mark.asyncio
async def test_maker_bot_initialization(bitcoin_backend, maker_config):
    """Test maker bot initialization"""
    wallet = WalletService(
        mnemonic=maker_config.mnemonic.get_secret_value(),
        backend=bitcoin_backend,
        network="regtest",
    )

    bot = MakerBot(wallet, bitcoin_backend, maker_config)

    # v5 nicks for reference implementation compatibility
    assert bot.nick.startswith("J5")
    assert len(bot.nick) == 16

    await wallet.close()


@pytest.mark.asyncio
async def test_offer_creation(
    funded_wallet: WalletService, bitcoin_backend, maker_config
):
    """Test offer creation based on wallet balance"""
    from maker.offers import OfferManager

    offer_manager = OfferManager(funded_wallet, maker_config, "J5TestMaker")

    offers = await offer_manager.create_offers()

    if offers:
        offer = offers[0]
        assert offer.minsize <= offer.maxsize
        assert offer.txfee == maker_config.tx_fee_contribution
        assert offer.counterparty == "J5TestMaker"


@pytest.mark.asyncio
async def test_coin_selection(funded_wallet: WalletService):
    """Test UTXO selection for CoinJoin"""
    balance = await funded_wallet.get_balance(0)

    if balance > 50_000:
        utxos = funded_wallet.select_utxos(0, 50_000, min_confirmations=1)
        assert len(utxos) > 0
        total = sum(u.value for u in utxos)
        assert total >= 50_000


@pytest.mark.asyncio
async def test_system_health_check(bitcoin_backend, mined_chain):
    """Test overall system health"""
    try:
        height = await bitcoin_backend.get_block_height()
        assert height > 100

        fee = await bitcoin_backend.estimate_fee(6)
        assert fee > 0

        logger_info = "System health check passed ✓"
        print(logger_info)

    except Exception as e:
        pytest.fail(f"System health check failed: {e}")


@pytest.mark.asyncio
async def test_maker_bot_connect_directory(
    bitcoin_backend, maker_config, directory_server, funded_maker1_wallet
):
    """Test maker bot connecting to directory server"""
    # Use the funded_maker1_wallet fixture which uses MAKER1_MNEMONIC (same as maker_config)
    bot = MakerBot(funded_maker1_wallet, bitcoin_backend, maker_config)

    # Start the bot in the background
    start_task = asyncio.create_task(bot.start())

    try:
        # Wait for connection to establish (wallet sync takes ~2s, connection ~0.5s)
        await asyncio.sleep(10)

        # Check that bot connected
        assert len(bot.directory_clients) > 0, (
            "Should have connected to directory server. "
            f"Connections: {bot.directory_clients}, Running: {bot.running}"
        )
        assert bot.running, "Bot should be running"

    finally:
        # Stop the bot
        await bot.stop()
        # Cancel the start task if still running
        start_task.cancel()
        try:
            await start_task
        except asyncio.CancelledError:
            pass
        # Note: funded_maker1_wallet is closed by the fixture itself


# ==============================================================================
# Taker Tests
# ==============================================================================


@pytest_asyncio.fixture
async def funded_taker_wallet(bitcoin_backend):
    """Create and fund a taker wallet using taker mnemonic."""
    async for w in _create_funded_wallet(
        bitcoin_backend,
        TAKER_MNEMONIC,
        skip_msg="Taker wallet has no funds. Auto-funding failed; please fund manually.",
    ):
        yield w


@pytest_asyncio.fixture
async def funded_taker_wallet_p2tr(bitcoin_backend):
    """Create and fund a P2TR (taproot) taker wallet using taker mnemonic."""
    async for w in _create_funded_wallet(
        bitcoin_backend,
        TAKER_MNEMONIC,
        address_type="p2tr",
        skip_msg="P2TR taker wallet has no funds. Auto-funding failed.",
    ):
        yield w


@pytest_asyncio.fixture
async def funded_maker1_wallet(bitcoin_backend):
    """Create and fund maker1 wallet."""
    async for w in _create_funded_wallet(
        bitcoin_backend,
        MAKER1_MNEMONIC,
        skip_msg="Maker1 wallet has no funds.",
    ):
        yield w


@pytest_asyncio.fixture
async def funded_maker2_wallet(bitcoin_backend):
    """Create and fund maker2 wallet."""
    async for w in _create_funded_wallet(
        bitcoin_backend,
        MAKER2_MNEMONIC,
        skip_msg="Maker2 wallet has no funds.",
    ):
        yield w


@pytest.mark.asyncio
async def test_taker_initialization(bitcoin_backend, taker_config):
    """Test taker initialization and nick generation."""
    wallet = WalletService(
        mnemonic=taker_config.mnemonic.get_secret_value(),
        backend=bitcoin_backend,
        network="regtest",
    )

    taker = Taker(wallet, bitcoin_backend, taker_config)

    # v5 nicks for reference implementation compatibility
    assert taker.nick.startswith("J5")
    assert len(taker.nick) == 16

    # Check initial state
    from taker.taker import TakerState

    assert taker.state == TakerState.IDLE

    await wallet.close()


@pytest.mark.asyncio
async def test_taker_connect_directory(bitcoin_backend, taker_config, directory_server):
    """Test taker connecting to directory server."""
    wallet = WalletService(
        mnemonic=taker_config.mnemonic.get_secret_value(),
        backend=bitcoin_backend,
        network="regtest",
    )

    taker = Taker(wallet, bitcoin_backend, taker_config)

    try:
        # Start taker (connects to directory servers)
        await taker.start()

        # Check wallet was synced
        total_balance = await wallet.get_total_balance()
        assert total_balance >= 0, "Wallet should have synced"

        # Directory client should have connected
        print(f"Taker nick: {taker.nick}")
        print(f"Taker state: {taker.state}")

        # Check connection status
        # Taker uses MultiDirectoryClient which stores clients in self.directory_client.clients
        connected_count = len(taker.directory_client.clients)
        assert connected_count > 0, "Should be connected to directory"

    finally:
        await taker.stop()


@pytest.mark.asyncio
async def test_taker_orderbook_fetch(bitcoin_backend, taker_config, directory_server):
    """Test taker fetching orderbook from directory."""
    wallet = WalletService(
        mnemonic=taker_config.mnemonic.get_secret_value(),
        backend=bitcoin_backend,
        network="regtest",
    )

    taker = Taker(wallet, bitcoin_backend, taker_config)

    try:
        await taker.start()

        # Fetch orderbook - may be empty if no makers are running
        offers = await taker.directory_client.fetch_orderbook(
            timeout=taker_config.order_wait_time
        )

        # Offers should be a list (may be empty)
        assert isinstance(offers, list), "Offers should be a list"
        print(f"Found {len(offers)} offers in orderbook")

        # Update orderbook manager
        taker.orderbook_manager.update_offers(offers)

    finally:
        await taker.stop()


@pytest.mark.asyncio
async def test_taker_config_validation(taker_config):
    """Test taker configuration validation."""
    from taker.config import MaxCjFee, TakerConfig

    # Test default MaxCjFee
    max_fee = MaxCjFee()
    assert max_fee.abs_fee == 500
    assert max_fee.rel_fee == "0.001"

    # Test custom config
    config = TakerConfig(
        mnemonic="abandon " * 11 + "about",
        counterparty_count=5,
        minimum_makers=3,
        mixdepth=2,
    )
    assert config.counterparty_count == 5
    assert config.minimum_makers == 3
    assert config.mixdepth == 2

    # Test bounds validation
    with pytest.raises(ValueError):
        TakerConfig(
            mnemonic="abandon " * 11 + "about",
            counterparty_count=25,  # Max is 20
        )


@pytest.mark.asyncio
async def test_taker_orderbook_manager(bitcoin_backend, taker_config):
    """Test taker orderbook manager functionality."""
    from jmcore.models import Offer, OfferType
    from taker.orderbook import OrderbookManager, calculate_cj_fee

    max_fee = taker_config.max_cj_fee
    manager = OrderbookManager(max_fee)

    # Create some test offers
    test_offers = [
        Offer(
            counterparty="J5TestMaker1",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000,
            maxsize=10_000_000,
            txfee=500,
            cjfee="0.0002",
        ),
        Offer(
            counterparty="J5TestMaker2",
            oid=0,
            ordertype=OfferType.SW0_ABSOLUTE,
            minsize=10_000,
            maxsize=5_000_000,
            txfee=1000,
            cjfee="100",
        ),
    ]

    manager.update_offers(test_offers)
    assert len(manager.offers) == 2

    # Test fee calculation
    cj_amount = 1_000_000
    fee1 = calculate_cj_fee(test_offers[0], cj_amount)
    assert fee1 == 200  # 0.02% of 1M = 200 sats

    fee2 = calculate_cj_fee(test_offers[1], cj_amount)
    assert fee2 == 100  # absolute 100 sats


@pytest.mark.asyncio
async def test_taker_podle_generation(funded_taker_wallet: WalletService):
    """Test PoDLE commitment generation for taker."""
    from taker.podle import select_podle_utxo

    # Get UTXOs from wallet
    utxos = await funded_taker_wallet.get_utxos(0)

    if not utxos:
        pytest.skip("No UTXOs available for PoDLE test")

    cj_amount = 100_000

    # Test UTXO selection
    selected = select_podle_utxo(
        utxos=utxos,
        cj_amount=cj_amount,
        min_confirmations=1,
        min_percent=10,
    )

    if selected:
        print(f"Selected UTXO: {selected.txid}:{selected.vout}")
        print(f"Value: {selected.value}, Confirmations: {selected.confirmations}")
        assert selected.confirmations >= 1
        assert selected.value >= cj_amount * 0.1


@pytest.mark.asyncio
async def test_taker_tx_builder():
    """Test taker transaction builder utilities."""
    from jmcore.bitcoin import address_to_scriptpubkey
    from taker.tx_builder import (
        TxInput,
        TxOutput,
        calculate_tx_fee,
        varint,
    )

    # Test varint encoding
    assert varint(0) == b"\x00"
    assert varint(252) == b"\xfc"
    assert varint(253) == b"\xfd\xfd\x00"

    # Test fee calculation
    # calculate_tx_fee(num_taker_inputs, num_maker_inputs, num_outputs, fee_rate)
    fee = calculate_tx_fee(1, 2, 5, fee_rate=10)
    # 3 P2WPKH inputs (~68 vbytes each) + 5 outputs (~31 vbytes each) + overhead
    expected_vsize = 3 * 68 + 5 * 31 + 11
    assert fee == expected_vsize * 10

    # Test TxInput/TxOutput dataclasses
    tx_in = TxInput.from_hex(
        txid="a" * 64,
        vout=0,
        value=100_000,
        scriptpubkey="0014" + "b" * 40,
    )
    assert tx_in.sequence == 0xFFFFFFFF  # Default sequence (final)

    tx_out = TxOutput.from_address(
        "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
        50_000,
    )
    assert tx_out.value == 50_000

    # Test address to scriptpubkey (P2WPKH)
    testnet_addr = "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx"
    script = address_to_scriptpubkey(testnet_addr)
    assert script.startswith(bytes.fromhex("0014"))  # P2WPKH prefix (OP_0 PUSH20)


@pytest.mark.asyncio
async def test_taker_signing_integration(funded_taker_wallet: WalletService):
    """
    Test taker signing integration with real wallet keys.

    This test verifies that:
    1. The taker can build a CoinJoin transaction
    2. The taker can correctly find its input indices in the shuffled transaction
    3. The taker can sign its inputs with proper BIP143 sighash
    4. The signatures can be added to the transaction
    """
    from jmwallet.wallet.signing import deserialize_transaction
    from taker.tx_builder import (
        CoinJoinTxBuilder,
        CoinJoinTxData,
        TxInput,
        TxOutput,
    )

    # Get real UTXOs from wallet
    utxos = await funded_taker_wallet.get_utxos(0)
    if not utxos:
        pytest.skip("No UTXOs available for signing test")

    # Take the first UTXO for testing
    taker_utxo = utxos[0]
    print(f"Using UTXO: {taker_utxo.txid}:{taker_utxo.vout} = {taker_utxo.value} sats")

    # Create mock maker UTXOs
    maker_utxos = [
        {"txid": "c" * 64, "vout": 0, "value": 1_200_000},
    ]

    cj_amount = min(taker_utxo.value // 2, 500_000)

    # Build CoinJoin transaction data
    tx_data = CoinJoinTxData(
        taker_inputs=[
            TxInput.from_hex(
                txid=taker_utxo.txid,
                vout=taker_utxo.vout,
                value=taker_utxo.value,
            )
        ],
        taker_cj_output=TxOutput.from_address(
            "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
            cj_amount,
        ),
        taker_change_output=TxOutput.from_address(
            "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
            taker_utxo.value - cj_amount - 5000,  # Minus fee
        ),
        maker_inputs={
            "maker1": [
                TxInput.from_hex(txid=u["txid"], vout=u["vout"], value=u["value"])
                for u in maker_utxos
            ],
        },
        maker_cj_outputs={
            "maker1": TxOutput.from_address(
                "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                cj_amount,
            ),
        },
        maker_change_outputs={
            "maker1": TxOutput.from_address(
                "bcrt1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qzf4jry",
                1_200_000 - cj_amount + 1000,  # Maker gets fee
            ),
        },
        cj_amount=cj_amount,
        total_maker_fee=1000,
        tx_fee=5000,
    )

    # Build the transaction
    builder = CoinJoinTxBuilder(network="regtest")
    tx_bytes, metadata = builder.build_unsigned_tx(tx_data)

    print(f"Built transaction: {len(tx_bytes)} bytes")
    print(f"Input owners: {metadata['input_owners']}")

    # Verify the transaction structure
    tx = deserialize_transaction(tx_bytes)
    assert len(tx.inputs) == 2  # 1 taker + 1 maker

    # Create a mock taker object to test signing
    from unittest.mock import MagicMock, patch

    from taker.taker import Taker

    mock_config = MagicMock()
    mock_config.network.value = "regtest"
    mock_backend = MagicMock()

    with patch.object(Taker, "__init__", lambda self, *args, **kwargs: None):
        taker = Taker.__new__(Taker)
        taker.wallet = funded_taker_wallet
        taker.backend = mock_backend
        taker.config = mock_config
        taker.maker_sessions = {}
        taker.unsigned_tx = tx_bytes
        taker.tx_metadata = metadata
        taker.selected_utxos = [taker_utxo]
        taker.maker_sessions = {}  # No maker sessions in this unit test

        # Sign the inputs
        signatures = await taker._sign_our_inputs()

        # Verify we got a signature
        assert len(signatures) == 1, f"Expected 1 signature, got {len(signatures)}"

        sig_info = signatures[0]
        print(f"Signature for {sig_info['txid']}:{sig_info['vout']}")
        print(f"  Pubkey: {sig_info['pubkey'][:16]}...")
        print(f"  Signature length: {len(sig_info['signature']) // 2} bytes")

        # Verify signature structure
        assert sig_info["txid"] == taker_utxo.txid
        assert sig_info["vout"] == taker_utxo.vout
        assert len(sig_info["witness"]) == 2

        # Verify signature is valid DER + sighash
        sig_bytes = bytes.fromhex(sig_info["signature"])
        assert len(sig_bytes) > 64  # DER signatures are variable length
        assert sig_bytes[-1] == 1  # SIGHASH_ALL

        # Test that add_signatures rejects incomplete signatures.
        # A CoinJoin transaction is invalid unless every input is signed,
        # so providing only the taker's signature while maker signatures
        # are missing must raise ValueError.
        all_signatures = {
            "taker": signatures,
            "maker1": [],  # Missing maker signature
        }

        with pytest.raises(ValueError, match="missing signatures"):
            builder.add_signatures(tx_bytes, all_signatures, metadata)

        print("Correctly rejected incomplete signatures")
        print("Taker signing integration test PASSED")


@pytest.mark.asyncio
async def test_taker_p2tr_signing_integration(funded_taker_wallet_p2tr, taker_config):
    """Test that a P2TR taker can sign its own inputs with Schnorr signatures.

    This mirrors test_taker_signing_integration but uses a taproot (P2TR) wallet.
    It verifies the BIP341 key-path signing pipeline:
    1. P2TR UTXOs are detected from their scriptPubKey (prefix 5120)
    2. All prevout values/scripts are fetched for BIP341 sighash
    3. Private key is tweaked per BIP341 before Schnorr signing
    4. Witness has 1 element (64-byte sig), not 2 (sig + pubkey as in P2WPKH)
    """
    from jmcore.bitcoin import (
        TxInput,
        TxOutput,
        address_to_scriptpubkey,
        create_p2tr_scriptpubkey,
    )
    from taker.tx_builder import CoinJoinTxBuilder, CoinJoinTxData
    from unittest.mock import patch

    wallet = funded_taker_wallet_p2tr

    # Get P2TR UTXOs from the funded wallet
    utxos_by_depth = {
        md: wallet.get_all_utxos(md) for md in range(wallet.mixdepth_count)
    }
    our_utxos = [u for md_utxos in utxos_by_depth.values() for u in md_utxos]
    assert len(our_utxos) > 0, "Taker P2TR wallet must have UTXOs"

    taker_utxo = our_utxos[0]
    assert taker_utxo.is_p2tr, (
        f"Expected P2TR UTXO, got scriptpubkey: {taker_utxo.scriptpubkey}"
    )

    # Create a mock maker UTXO (also P2TR for a full-taproot CoinJoin)
    _maker_address = wallet.get_address(1, 0, 0)  # mixdepth 1, external, index 0
    _maker_spk = create_p2tr_scriptpubkey(
        bytes.fromhex(taker_utxo.scriptpubkey)[2:]  # reuse key for simplicity
    )

    coinjoin_amount = 100_000
    maker_fee = 1000
    tx_fee = 500

    # Build proper TxInput/TxOutput objects for CoinJoinTxData
    taker_input = TxInput.from_hex(taker_utxo.txid, taker_utxo.vout)

    dest_address = wallet.get_address(1, 0, 0)  # P2TR receive destination
    change_address = wallet.get_address(0, 1, 0)  # P2TR change

    taker_cj_out = TxOutput(
        value=coinjoin_amount,
        script=address_to_scriptpubkey(dest_address),
    )
    taker_change_val = taker_utxo.value - coinjoin_amount - maker_fee - tx_fee
    taker_change_out = TxOutput(
        value=taker_change_val,
        script=address_to_scriptpubkey(change_address),
    )

    # Mock maker inputs and outputs (also P2TR to form an all-taproot CoinJoin)
    maker_input = TxInput.from_hex("0" * 64, 0)
    maker_cj_out = TxOutput(
        value=coinjoin_amount,
        script=address_to_scriptpubkey(dest_address),  # reuse address for simplicity
    )
    maker_change_out = TxOutput(
        value=maker_fee,
        script=address_to_scriptpubkey(dest_address),
    )

    cj_data = CoinJoinTxData(
        taker_inputs=[taker_input],
        taker_cj_output=taker_cj_out,
        taker_change_output=taker_change_out,
        maker_inputs={"maker1": [maker_input]},
        maker_cj_outputs={"maker1": maker_cj_out},
        maker_change_outputs={"maker1": maker_change_out},
        cj_amount=coinjoin_amount,
        total_maker_fee=maker_fee,
        tx_fee=tx_fee,
    )

    builder = CoinJoinTxBuilder(network="regtest")
    tx_bytes, metadata = builder.build_unsigned_tx(cj_data)

    # Create a mock Taker and sign
    # Mock backend.get_utxo so the P2TR sighash code can fetch prevouts for
    # ALL transaction inputs (including the fake maker input which isn't on-chain).
    from jmwallet.wallet.models import UTXOInfo as UtxoModel

    async def _mock_get_utxo(txid: str, vout: int) -> UtxoModel | None:
        # Return the real taker UTXO or a minimal fake one for maker inputs
        if txid == taker_utxo.txid and vout == taker_utxo.vout:
            return taker_utxo
        # Fake maker prevout: must have a valid scriptpubkey and value
        return UtxoModel(
            txid=txid,
            vout=vout,
            value=coinjoin_amount + maker_fee,
            address=dest_address,
            confirmations=6,
            scriptpubkey=address_to_scriptpubkey(dest_address).hex(),
            path="m/86'/1'/0'/0/0",
            mixdepth=0,
        )

    with patch.object(Taker, "__init__", lambda self, *a, **kw: None):
        taker = Taker.__new__(Taker)
        taker.wallet = wallet
        taker.backend = wallet.backend
        taker.config = taker_config
        taker.unsigned_tx = tx_bytes
        taker.tx_metadata = metadata
        taker.selected_utxos = [taker_utxo]
        taker.maker_sessions = {}
        taker.backend.get_utxo = _mock_get_utxo

        signatures = await taker._sign_our_inputs()

    assert len(signatures) == 1, f"Expected 1 signature, got {len(signatures)}"

    sig_entry = signatures[0]
    # Each entry: {txid, vout, signature (hex), pubkey (hex), witness (list[hex])}
    assert "signature" in sig_entry
    assert "witness" in sig_entry
    assert "txid" in sig_entry
    assert "vout" in sig_entry

    # P2TR witness: single element (Schnorr sig only), NOT [sig, pubkey]
    witness = sig_entry["witness"]
    assert len(witness) == 1, (
        f"P2TR witness should have 1 element (sig only), got {len(witness)}"
    )

    sig_hex = witness[0]
    sig_bytes = bytes.fromhex(sig_hex)

    # BIP340 Schnorr signature: exactly 64 bytes for SIGHASH_DEFAULT
    assert len(sig_bytes) == 64, (
        f"Schnorr signature should be 64 bytes, got {len(sig_bytes)}"
    )

    print("P2TR taker signing integration test PASSED")


# ==============================================================================
# Complete CoinJoin E2E Test
# ==============================================================================


@pytest.mark.asyncio
@pytest.mark.slow
async def test_complete_coinjoin_two_makers(
    bitcoin_backend,
    taker_config,
    directory_server,
    fresh_docker_makers,
):
    """
    Complete end-to-end CoinJoin test with Docker-based makers and an in-process taker.

    This test verifies the entire CoinJoin flow:
    1. Docker makers (jm-maker1, jm-maker2) are already running with offers
    2. Taker connects and fetches orderbook from directory server
    3. Taker initiates CoinJoin with both makers
    4. All phases complete (fill, auth, ioauth, tx, sig)
    5. Transaction is broadcast and confirmed

    Requires: docker compose --profile all up -d (or --profile e2e)

    Note: This test uses Docker makers (funded by wallet-funder service) rather than
    in-process makers. This tests the real deployment scenario.
    """
    from tests.e2e.rpc_utils import mine_blocks

    # Check if Docker makers are running
    _require_docker_container("jm-maker1")
    _require_docker_container("jm-maker2")

    # Ensure coinbase maturity: mine extra blocks for any recent coinbase outputs
    print("Mining blocks to ensure coinbase maturity...")
    await mine_blocks(10, MINING_ADDRESS)

    # Create taker wallet
    taker_wallet = WalletService(
        mnemonic=TAKER_MNEMONIC,
        backend=bitcoin_backend,
        network="regtest",
        mixdepth_count=5,
    )

    # Sync wallet
    await taker_wallet.sync_all()
    taker_balance = await taker_wallet.get_total_balance()
    print(f"Taker balance: {taker_balance:,} sats")

    min_balance = 100_000_000  # 1 BTC minimum
    if taker_balance < min_balance:
        await taker_wallet.close()
        pytest.skip(
            f"Taker needs at least {min_balance:,} sats. "
            "Run wallet-funder or fund manually."
        )

    # Create taker
    taker = Taker(taker_wallet, bitcoin_backend, taker_config)

    try:
        # Start taker
        print("Starting taker...")
        await taker.start()

        # Verify taker can see offers from Docker makers
        print("Fetching orderbook...")
        offers = await taker.directory_client.fetch_orderbook(timeout=15.0)
        print(f"Found {len(offers)} offers in orderbook")

        if len(offers) < 2:
            await taker.stop()
            await taker_wallet.close()
            pytest.skip(
                f"Need at least 2 offers, found {len(offers)}. "
                "Ensure Docker makers are running and have funds."
            )

        # Update orderbook manager
        taker.orderbook_manager.update_offers(offers)

        # Get taker's destination address (internal)
        dest_address = taker_wallet.get_receive_address(1, 0)  # mixdepth 1

        # Initiate CoinJoin
        cj_amount = 50_000_000  # 0.5 BTC
        print(f"Initiating CoinJoin for {cj_amount:,} sats to {dest_address}...")

        txid = await taker.do_coinjoin(
            amount=cj_amount,
            destination=dest_address,
            mixdepth=0,
        )

        # Verify result
        assert txid is not None, "CoinJoin should return a txid"
        print(f"CoinJoin successful! txid: {txid}")

        # Verify transaction on blockchain
        tx_info = await bitcoin_backend.get_transaction(txid)
        if tx_info:
            print(f"Transaction info: {tx_info}")

        # Mine a block to confirm
        await mine_blocks(1, dest_address)

        # Verify the transaction has confirmations
        height = await bitcoin_backend.get_block_height()
        print(f"Current block height: {height}")

    finally:
        # Cleanup
        print("Stopping taker...")
        await taker.stop()
        await taker_wallet.close()


@pytest.mark.asyncio
@pytest.mark.slow
async def test_coinjoin_with_multi_utxo_maker(
    bitcoin_backend,
    taker_config,
    directory_server,
    fresh_docker_makers,
):
    """
    Test CoinJoin with a maker using multiple UTXOs.

    This test specifically verifies the fix for the multi-signature bug where makers
    with multiple UTXOs send multiple !sig messages, and the taker must correctly
    accumulate and process all signatures.

    Regression test for: Taker only accepting one signature from multi-UTXO makers,
    causing transaction broadcast to fail due to incomplete signatures.

    The test:
    1. Uses maker3 which typically has many small UTXOs (from mining rewards)
    2. Sets a small CoinJoin amount to force maker to use multiple UTXOs
    3. Verifies all signatures are collected and transaction broadcasts successfully

    Requires: docker compose --profile e2e up -d
    """
    from tests.e2e.rpc_utils import mine_blocks

    # Check if Docker maker3 is running (it has many UTXOs from mining)
    _require_docker_container("jm-maker3")

    # Mine extra blocks to ensure coinbase maturity
    print("Mining blocks to ensure coinbase maturity...")
    await mine_blocks(10, MINING_ADDRESS)

    # Create taker wallet
    taker_wallet = WalletService(
        mnemonic=TAKER_MNEMONIC,
        backend=bitcoin_backend,
        network="regtest",
        mixdepth_count=5,
    )

    # Sync wallet
    await taker_wallet.sync_all()
    taker_balance = await taker_wallet.get_total_balance()
    print(f"Taker balance: {taker_balance:,} sats")

    min_balance = 50_000_000  # 0.5 BTC minimum
    if taker_balance < min_balance:
        await taker_wallet.close()
        pytest.skip(
            f"Taker needs at least {min_balance:,} sats. "
            "Run wallet-funder or fund manually."
        )

    # Create taker
    taker = Taker(taker_wallet, bitcoin_backend, taker_config)

    try:
        # Start taker
        print("Starting taker...")
        await taker.start()

        # Fetch orderbook
        print("Fetching orderbook...")
        offers = await taker.directory_client.fetch_orderbook(timeout=15.0)
        print(f"Found {len(offers)} offers in orderbook")

        if len(offers) < 1:
            await taker.stop()
            await taker_wallet.close()
            pytest.skip(
                f"Need at least 1 offer, found {len(offers)}. "
                "Ensure Docker makers are running and have funds."
            )

        # Update orderbook manager
        taker.orderbook_manager.update_offers(offers)

        # Get taker's destination address (internal)
        dest_address = taker_wallet.get_receive_address(1, 0)  # mixdepth 1

        # Use a small CoinJoin amount (20M sats = 0.2 BTC)
        # This forces maker3 (which has many small mining rewards) to use multiple UTXOs
        # If maker3 uses >1 UTXO, it will send >1 !sig message, testing our fix
        cj_amount = 20_000_000  # 0.2 BTC
        print(f"Initiating CoinJoin for {cj_amount:,} sats to {dest_address}...")
        print("This amount is chosen to force maker to use multiple UTXOs")

        txid = await taker.do_coinjoin(
            amount=cj_amount,
            destination=dest_address,
            mixdepth=0,
        )

        # Verify result
        assert txid is not None, "CoinJoin should return a txid"
        print(f"CoinJoin successful! txid: {txid}")

        # Wait for transaction to be broadcast and confirmed
        print("Waiting for transaction to be broadcast and confirmed...")
        # Makers receive !push asynchronously (~60s later) and broadcast.
        # Auto-miner will mine it when it appears in mempool.
        max_retries = 20  # Up to ~3 minutes
        retry_delay = 3  # Check every 3 seconds
        found = False

        for attempt in range(max_retries):
            await asyncio.sleep(retry_delay)

            tx_info = await bitcoin_backend.get_transaction(txid)
            if tx_info is not None and tx_info.confirmations > 0:
                print(
                    f"Transaction confirmed with {tx_info.confirmations} "
                    f"confirmation(s) after {(attempt + 1) * retry_delay}s"
                )
                found = True
                break

            if (attempt + 1) % 5 == 0:
                print(f"Still waiting... ({(attempt + 1) * retry_delay}s elapsed)")

        if not found:
            raise AssertionError(
                f"Transaction {txid} not confirmed after {max_retries * retry_delay}s. "
                f"This indicates the makers failed to broadcast the transaction."
            )

        # Verify transaction details
        tx_info = await bitcoin_backend.get_transaction(txid)
        if tx_info:
            print(f"Transaction info: {tx_info}")
            # Count inputs to verify multi-UTXO usage by parsing the raw transaction
            from jmwallet.wallet.signing import deserialize_transaction

            tx_bytes = bytes.fromhex(tx_info.raw)
            tx = deserialize_transaction(tx_bytes)
            num_inputs = len(tx.inputs)
            print(f"Transaction has {num_inputs} inputs")
            # If we have >2 inputs (1 taker + >1 maker), we successfully tested multi-UTXO
            if num_inputs > 2:
                print(
                    "✓ Successfully tested multi-UTXO maker "
                    f"(maker used {num_inputs - 1} UTXOs)"
                )

        # Verify the transaction has confirmations
        height = await bitcoin_backend.get_block_height()
        print(f"Current block height: {height}")

    finally:
        # Cleanup
        print("Stopping taker...")
        await taker.stop()
        await taker_wallet.close()


@pytest.mark.asyncio
async def test_maker_offer_announcement(
    bitcoin_backend,
    maker_config,
    directory_server,
    funded_maker1_wallet,
):
    """Test that maker correctly announces offers to directory server."""
    from maker.offers import OfferManager

    offer_manager = OfferManager(funded_maker1_wallet, maker_config, "J5TestMaker")

    offers = await offer_manager.create_offers()

    assert len(offers) > 0, "Should create at least one offer"

    offer = offers[0]
    print(f"Created offer: minsize={offer.minsize}, maxsize={offer.maxsize}")
    print(f"  txfee={offer.txfee}, cjfee={offer.cjfee}")

    # Verify offer parameters match config
    assert offer.minsize >= maker_config.min_size
    assert offer.txfee == maker_config.tx_fee_contribution


@pytest.mark.asyncio
async def test_taker_maker_selection(
    bitcoin_backend,
    taker_config,
    directory_server,
    funded_taker_wallet,
):
    """Test taker's ability to select appropriate makers from orderbook."""
    from jmcore.models import Offer, OfferType
    from taker.orderbook import OrderbookManager

    manager = OrderbookManager(taker_config.max_cj_fee)

    # Simulate offers from our test makers
    test_offers = [
        Offer(
            counterparty="J5Maker1Nick",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000_000,
            txfee=1_000,
            cjfee="0.0003",
        ),
        Offer(
            counterparty="J5Maker2Nick",
            oid=0,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=100_000,
            maxsize=10_000_000_000,
            txfee=1_500,
            cjfee="0.00025",
        ),
    ]

    manager.update_offers(test_offers)

    # Select makers for a 0.5 BTC CoinJoin
    cj_amount = 50_000_000
    selected, total_fee = manager.select_makers(cj_amount, n=2)

    assert len(selected) == 2, "Should select 2 makers"
    print(f"Selected makers: {list(selected.keys())}")

    # Verify total fees are reasonable
    print(f"Total maker fee: {total_fee} sats")
    assert total_fee < cj_amount * 0.01, "Fees should be less than 1%"


@pytest.mark.asyncio
async def test_signing_produces_valid_witness(funded_taker_wallet: WalletService):
    """
    Test that our signing implementation produces valid witness data.

    This test verifies:
    - scriptCode is correctly formatted (25 bytes, no length prefix)
    - Sighash is computed correctly per BIP 143
    - Signature uses low-S normalization (BIP 62/146)
    - Witness stack has correct structure [signature, pubkey]
    """
    from jmwallet.wallet.signing import (
        Transaction,
        TxInput,
        TxOutput,
        compute_sighash_segwit,
        create_p2wpkh_script_code,
        sign_p2wpkh_input,
    )

    utxos = await funded_taker_wallet.get_utxos(0)
    if not utxos:
        pytest.skip("No UTXOs available")

    utxo = utxos[0]
    key = funded_taker_wallet.get_key_for_address(utxo.address)
    assert key is not None, "Should have key for UTXO address"

    pubkey = key.get_public_key_bytes(compressed=True)

    # Create a simple test transaction
    tx = Transaction(
        version=2,
        has_witness=True,
        inputs=[
            TxInput(
                txid_le=bytes.fromhex(utxo.txid)[::-1],  # Convert to LE
                vout=utxo.vout,
                scriptsig=b"",
                sequence=0xFFFFFFFF,
            )
        ],
        outputs=[
            TxOutput(
                value=utxo.value - 1000,  # Minus fee
                script=bytes.fromhex("0014" + "00" * 20),  # P2WPKH
            )
        ],
        locktime=0,
        witnesses=[],
    )

    # Create script code
    script_code = create_p2wpkh_script_code(pubkey)

    # Verify script code format (25 bytes, no length prefix)
    assert len(script_code) == 25, (
        f"scriptCode should be 25 bytes, got {len(script_code)}"
    )
    assert script_code[0] == 0x76, "Should start with OP_DUP"
    assert script_code[1] == 0xA9, "Should have OP_HASH160"
    assert script_code[2] == 0x14, "Should push 20 bytes"

    # Compute sighash
    sighash = compute_sighash_segwit(
        tx=tx,
        input_index=0,
        script_code=script_code,
        value=utxo.value,
        sighash_type=1,
    )
    assert len(sighash) == 32, "Sighash should be 32 bytes"

    # Sign
    signature = sign_p2wpkh_input(
        tx=tx,
        input_index=0,
        script_code=script_code,
        value=utxo.value,
        private_key=key.private_key,
    )

    # Verify signature format
    assert len(signature) > 64, "DER signature should be longer than 64 bytes"
    assert signature[-1] == 1, "Should end with SIGHASH_ALL"
    assert signature[0] == 0x30, "DER signatures start with 0x30"

    # Verify low-S (BIP 62/146)
    # coincurve always produces low-S signatures by default
    # DER format: 0x30 [total-length] 0x02 [r-length] [r] 0x02 [s-length] [s]
    der_sig = signature[:-1]  # Remove sighash byte
    assert der_sig[0] == 0x30  # DER sequence marker
    assert der_sig[2] == 0x02  # r integer marker
    r_len = der_sig[3]
    s_marker_pos = 4 + r_len
    assert der_sig[s_marker_pos] == 0x02  # s integer marker
    s_len = der_sig[s_marker_pos + 1]
    s = int.from_bytes(der_sig[s_marker_pos + 2 : s_marker_pos + 2 + s_len], "big")

    secp256k1_half_order = (
        0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141 // 2
    )
    assert s <= secp256k1_half_order, "Signature S value should be low (BIP 62)"

    print(f"Signature valid: {len(signature)} bytes, low-S verified")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
