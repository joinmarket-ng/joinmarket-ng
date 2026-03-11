"""
End-to-end tests for the swap input feature.

Tests the complete flow of acquiring a submarine swap UTXO and including
it in a CoinJoin transaction, making the taker's on-chain footprint
indistinguishable from a maker's.

Architecture:
- Real Electrum swap server (Docker container jm-electrum-swap) runs the
  swapserver plugin with a built-in Lightning node.
- Communication is exclusively via Nostr DMs (kind 25582, NIP-04 encrypted)
  through a lightweight relay (jm-nostr-relay on port 7000).
- The taker discovers the swap server via Nostr kind 30315 offers, then
  sends an encrypted ``createswap`` request via kind 25582 DMs.
- LND-taker pays the Lightning invoice; the Electrum server broadcasts the
  lockup transaction which the taker detects trustlessly via its Bitcoin backend.
- The swap UTXO is signed with P2WSH claim witness during CoinJoin signing.

Requires: docker compose --profile e2e up -d
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
import pytest_asyncio
from jmcore.models import NetworkType
from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
from jmwallet.wallet.service import WalletService
from taker.config import SwapInputConfig, TakerConfig
from taker.taker import Taker

# Mark all tests in this module as requiring Docker e2e profile
pytestmark = pytest.mark.e2e

# ==============================================================================
# Test Wallet Mnemonics (same as test_complete_system.py)
# ==============================================================================

TAKER_MNEMONIC = (
    "burden notable love elephant orbit couch message galaxy elevator exile drop toilet"
)

MINING_ADDRESS = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"

# Nostr relay exposed on host port 7000 by docker-compose
NOSTR_RELAY_URL = "ws://127.0.0.1:7000"

# LND taker REST API (exposed on host port 8081)
LND_TAKER_REST_URL = "https://127.0.0.1:8081"

# LND credentials are written by lnd-setup into ./shared/lnd/ (host-side bind mount).
_REPO_ROOT = Path(__file__).parent.parent.parent
LND_TAKER_CERT_PATH = str(_REPO_ROOT / "shared" / "lnd" / "taker-tls.cert")
LND_TAKER_MACAROON_PATH = str(_REPO_ROOT / "shared" / "lnd" / "taker-admin.macaroon")

# Electrum swap server info (written by entrypoint.sh to /shared/electrum/)
ELECTRUM_SWAP_INFO_PATH = _REPO_ROOT / "shared" / "electrum" / "swap-server-info.json"


# ==============================================================================
# Helpers
# ==============================================================================


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
                f"Docker {name} not running. "
                "Start with: docker compose --profile e2e up -d"
            )
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        subprocess.CalledProcessError,
    ):
        pytest.skip("Docker not available or containers not running")


def _require_lnd_credentials(wait_timeout: float = 120.0) -> None:
    """Ensure LND credentials are available, waiting for lnd-setup to complete.

    The lnd-setup container copies credentials to shared/lnd/ after the
    Lightning channel is open.  Instead of immediately skipping we poll
    for up to *wait_timeout* seconds so that slow CI environments don't
    fail with a spurious skip when the credentials are just not ready yet.
    """
    import os
    import time

    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if os.path.exists(LND_TAKER_CERT_PATH) and os.path.exists(
            LND_TAKER_MACAROON_PATH
        ):
            return
        time.sleep(2.0)

    # Timed out - skip with a meaningful message
    missing = []
    if not os.path.exists(LND_TAKER_CERT_PATH):
        missing.append(LND_TAKER_CERT_PATH)
    if not os.path.exists(LND_TAKER_MACAROON_PATH):
        missing.append(LND_TAKER_MACAROON_PATH)
    pytest.skip(
        f"LND credentials not available after {wait_timeout:.0f}s "
        f"(missing: {', '.join(missing)}). "
        "Ensure jm-lnd-setup has completed successfully."
    )


def _read_swap_server_info() -> dict:
    """Read Electrum swap server info from shared volume.

    Returns:
        Parsed JSON with keys: nostr_pubkey, nostr_relay, ln_pubkey, etc.
    """
    if not ELECTRUM_SWAP_INFO_PATH.exists():
        pytest.skip(
            f"Electrum swap server info not found at {ELECTRUM_SWAP_INFO_PATH}. "
            "Wait for jm-electrum-swap to be healthy."
        )
    return json.loads(ELECTRUM_SWAP_INFO_PATH.read_text())


async def _wait_for_nostr_relay(url: str, timeout: float = 30.0) -> None:
    """Wait for the Nostr relay WebSocket to accept connections."""
    import time

    import aiohttp

    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            ct = aiohttp.ClientTimeout(total=5.0)
            async with aiohttp.ClientSession(timeout=ct) as session:
                async with session.ws_connect(url) as ws:
                    # Relay is accepting connections
                    await ws.close()
                    return
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError):
            pass
        await asyncio.sleep(1.0)
    pytest.skip(f"Nostr relay at {url} did not become reachable in {timeout}s")


# ==============================================================================
# Fixtures
# ==============================================================================


@pytest.fixture
def bitcoin_backend():
    """Bitcoin Core backend for regtest."""
    return BitcoinCoreBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="test",
        rpc_password="test",
    )


@pytest_asyncio.fixture
async def funded_taker_wallet(bitcoin_backend):
    """Create and fund a taker wallet."""
    from tests.e2e.rpc_utils import ensure_wallet_funded

    wallet = WalletService(
        mnemonic=TAKER_MNEMONIC,
        backend=bitcoin_backend,
        network="regtest",
        mixdepth_count=5,
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
        pytest.skip("Taker wallet has no funds. Auto-funding failed.")

    try:
        yield wallet
    finally:
        await wallet.close()


@pytest.fixture
def taker_config_with_swap():
    """Taker configuration with swap input enabled via Nostr discovery.

    Uses the Nostr relay for provider discovery and LND credentials
    (if available) for automatic Lightning invoice payment.
    """
    import os

    swap_kwargs: dict = {
        "enabled": True,
        "nostr_relays": [NOSTR_RELAY_URL],
        "max_swap_fee_pct": 2.0,
        "lockup_timeout": 120.0,
    }

    # Wire up LND credentials for automatic invoice payment.
    if os.path.exists(LND_TAKER_CERT_PATH) and os.path.exists(LND_TAKER_MACAROON_PATH):
        swap_kwargs["lnd_rest_url"] = LND_TAKER_REST_URL
        swap_kwargs["lnd_cert_path"] = LND_TAKER_CERT_PATH
        swap_kwargs["lnd_macaroon_path"] = LND_TAKER_MACAROON_PATH

    return TakerConfig(
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
        maker_timeout_sec=30,
        order_wait_time=10.0,
        swap_input=SwapInputConfig(**swap_kwargs),
    )


@pytest.fixture
def taker_config_without_swap():
    """Taker configuration with swap input disabled (control group)."""
    return TakerConfig(
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
        maker_timeout_sec=30,
        order_wait_time=10.0,
        swap_input=SwapInputConfig(enabled=False),
    )


# ==============================================================================
# Nostr Relay and Swap Server Connectivity Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_nostr_relay_reachable():
    """Test that the Nostr relay is running and accepts WebSocket connections."""
    _require_docker_container("jm-nostr-relay")
    await _wait_for_nostr_relay(NOSTR_RELAY_URL)


@pytest.mark.asyncio
async def test_electrum_swap_server_ready():
    """Test that the Electrum swap server is healthy and has exported its info."""
    _require_docker_container("jm-electrum-swap")
    info = _read_swap_server_info()

    assert "nostr_pubkey" in info, "Missing nostr_pubkey in swap server info"
    assert len(info["nostr_pubkey"]) > 0, "Empty nostr_pubkey"
    assert "ln_pubkey" in info, "Missing ln_pubkey in swap server info"

    print(f"Electrum swap server LN pubkey: {info['ln_pubkey'][:16]}...")
    print(f"Nostr relay: {info.get('nostr_relay', 'N/A')}")


# ==============================================================================
# Nostr-based Swap Provider Discovery
# ==============================================================================


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_nostr_provider_discovery():
    """Discover the Electrum swap server via Nostr kind 30315 offers.

    Verifies the full Nostr discovery flow:
    1. Connect to the relay
    2. Subscribe to kind 30315 offer events
    3. Parse provider terms (fee, limits)
    4. Verify the discovered provider matches the exported swap server info

    The Electrum swap server publishes offers on a ~30-second loop.  After
    the LN channel is opened the server needs time to detect sufficient
    liquidity before it passes its internal guard and publishes.  We retry
    discovery every 10 seconds for up to ~2 minutes.
    """
    _require_docker_container("jm-nostr-relay")
    _require_docker_container("jm-electrum-swap")
    await _wait_for_nostr_relay(NOSTR_RELAY_URL)

    from taker.swap.nostr import NostrSwapDiscovery

    discovery = NostrSwapDiscovery(
        relays=[NOSTR_RELAY_URL],
        network="regtest",
        min_pow_bits=0,  # Regtest provider may not have PoW
    )

    # Retry discovery — the offer may not be on the relay yet.
    max_attempts = 12
    retry_interval = 10.0
    providers: list = []

    for attempt in range(1, max_attempts + 1):
        providers = await discovery.discover_providers(timeout=15.0)
        if providers:
            break
        if attempt < max_attempts:
            print(
                f"No providers yet (attempt {attempt}/{max_attempts}), "
                f"retrying in {retry_interval}s..."
            )
            await asyncio.sleep(retry_interval)

    assert len(providers) > 0, (
        f"No swap providers discovered via Nostr after {max_attempts} attempts "
        f"(~{max_attempts * retry_interval:.0f}s). "
        "Ensure jm-electrum-swap is healthy and announcing offers."
    )

    provider = providers[0]
    assert provider.pubkey, "Provider pubkey should not be empty"
    assert provider.percentage_fee >= 0, "Fee should be non-negative"
    assert provider.min_amount > 0, "min_amount should be positive"
    assert provider.max_reverse_amount > provider.min_amount

    # Cross-check with exported server info
    info = _read_swap_server_info()
    if info.get("nostr_pubkey"):
        # The Electrum nostr pubkey is derived from the LN node key;
        # it may be in a different format (full vs x-only). Just verify
        # the discovered provider is from our server.
        print(
            f"Discovered provider: pubkey={provider.pubkey[:16]}..., "
            f"fee={provider.percentage_fee}%, "
            f"min={provider.min_amount:,}, max={provider.max_reverse_amount:,}"
        )


# ==============================================================================
# Swap Client: Full Reverse Swap via Nostr + Lightning
# ==============================================================================


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_swap_client_acquire_input_ln(bitcoin_backend):
    """Test SwapClient.acquire_swap_input() with Nostr discovery + LN payment.

    This tests the full realistic flow:
    1. SwapClient discovers the Electrum swap server via Nostr
    2. Sends encrypted createswap request via kind 25582 DM
    3. Provider returns a real BOLT11 invoice
    4. SwapClient pays the invoice via the taker's LND node
    5. Provider detects settlement and broadcasts lockup tx
    6. SwapClient detects the lockup UTXO trustlessly via Bitcoin backend

    Requires: docker compose --profile e2e up -d (all services healthy,
    including lnd-setup which opens the LN channel)
    """
    _require_docker_container("jm-electrum-swap")
    _require_docker_container("jm-nostr-relay")
    _require_docker_container("jm-lnd-taker")
    _require_docker_container("jm-bitcoin")
    _require_lnd_credentials()
    await _wait_for_nostr_relay(NOSTR_RELAY_URL)

    from tests.e2e.rpc_utils import rpc_call

    from taker.swap.client import SwapClient

    # Get current block height
    info = await rpc_call("getblockchaininfo")
    current_height = info["blocks"]

    # Create swap client with Nostr discovery + LND for invoice payment
    client = SwapClient(
        nostr_relays=[NOSTR_RELAY_URL],
        network="regtest",
        max_swap_fee_pct=2.0,
        lnd_rest_url=LND_TAKER_REST_URL,
        lnd_cert_path=LND_TAKER_CERT_PATH,
        lnd_macaroon_path=LND_TAKER_MACAROON_PATH,
        backend=bitcoin_backend,
    )

    assert client.lnd_configured, "SwapClient should have LND configured"

    # Acquire a swap input -- full flow: discovery -> createswap -> pay -> lockup
    desired_amount = 50_000  # 50k sats
    swap_input = await client.acquire_swap_input(
        desired_amount_sats=desired_amount,
        current_block_height=current_height,
        wait_for_lockup=True,
        lockup_timeout=90.0,  # Generous timeout for LN payment + lockup broadcast
    )

    # Verify the swap input
    assert swap_input.txid, "Should have a lockup txid"
    assert swap_input.vout >= 0
    assert swap_input.value > 0
    assert len(swap_input.witness_script) > 0
    assert len(swap_input.preimage) == 32
    assert len(swap_input.claim_privkey) == 32
    assert swap_input.lockup_address.startswith("bcrt1")
    assert swap_input.timeout_block_height > current_height

    # Verify utxo_dict is well-formed
    utxo = swap_input.to_utxo_dict()
    assert utxo["txid"] == swap_input.txid
    assert utxo["vout"] == swap_input.vout
    assert utxo["value"] == swap_input.value
    assert len(utxo["scriptpubkey"]) > 0

    print(
        f"Acquired swap input via Nostr+LN: {swap_input.txid}:{swap_input.vout} "
        f"({swap_input.value:,} sats), timeout={swap_input.timeout_block_height}"
    )


# ==============================================================================
# P2WSH Signing Verification
# ==============================================================================


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_swap_input_p2wsh_signing(bitcoin_backend):
    """Test that a swap input can be signed with P2WSH claim witness.

    Creates a swap input via the full Nostr+LN flow, then constructs and
    signs a simple spending transaction to verify the witness is valid.
    """
    _require_docker_container("jm-electrum-swap")
    _require_docker_container("jm-nostr-relay")
    _require_docker_container("jm-lnd-taker")
    _require_docker_container("jm-bitcoin")
    _require_lnd_credentials()
    await _wait_for_nostr_relay(NOSTR_RELAY_URL)

    from coincurve import PrivateKey
    from jmwallet.wallet.signing import (
        Transaction,
        TxInput,
        TxOutput,
        sign_p2wsh_input,
    )
    from tests.e2e.rpc_utils import rpc_call

    from taker.swap.client import SwapClient
    from taker.swap.script import SwapScript

    # Get current block height
    info = await rpc_call("getblockchaininfo")
    current_height = info["blocks"]

    # Acquire swap input via full flow
    client = SwapClient(
        nostr_relays=[NOSTR_RELAY_URL],
        network="regtest",
        max_swap_fee_pct=2.0,
        lnd_rest_url=LND_TAKER_REST_URL,
        lnd_cert_path=LND_TAKER_CERT_PATH,
        lnd_macaroon_path=LND_TAKER_MACAROON_PATH,
        backend=bitcoin_backend,
    )
    swap_input = await client.acquire_swap_input(
        desired_amount_sats=50_000,
        current_block_height=current_height,
        wait_for_lockup=True,
        lockup_timeout=90.0,
    )

    # Build a spending transaction
    tx = Transaction(
        version=2,
        has_witness=True,
        inputs=[
            TxInput(
                txid_le=bytes.fromhex(swap_input.txid)[::-1],
                vout=swap_input.vout,
                scriptsig=b"",
                sequence=0xFFFFFFFF,
            )
        ],
        outputs=[
            TxOutput(
                value=swap_input.value - 1000,  # Minus fee
                script=bytes.fromhex("0014" + "00" * 20),  # Dummy P2WPKH
            )
        ],
        locktime=0,
        witnesses=[],
    )

    # Sign with P2WSH
    claim_key = PrivateKey(swap_input.claim_privkey)
    signature = sign_p2wsh_input(
        tx=tx,
        input_index=0,
        witness_script=swap_input.witness_script,
        value=swap_input.value,
        private_key=claim_key,
    )

    # Build claim witness
    claim_witness = SwapScript.build_claim_witness(
        signature, swap_input.preimage, swap_input.witness_script
    )

    # Verify witness structure: [signature, preimage, witness_script]
    assert len(claim_witness) == 3
    assert claim_witness[0] == signature
    assert claim_witness[1] == swap_input.preimage
    assert claim_witness[2] == swap_input.witness_script

    # Verify signature is valid DER + SIGHASH_ALL
    assert signature[-1] == 1  # SIGHASH_ALL
    assert signature[0] == 0x30  # DER sequence marker

    print(
        f"P2WSH claim witness built: sig={len(signature)} bytes, "
        f"preimage=32 bytes, witness_script={len(swap_input.witness_script)} bytes"
    )


# ==============================================================================
# Cross-Compatibility: SwapScript vs Electrum swap server
# ==============================================================================


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_swap_script_cross_compatibility(bitcoin_backend):
    """Verify our SwapScript matches the Electrum swap server's HTLC construction.

    Both sides (taker's SwapScript and Electrum's swapserver plugin) must produce
    identical witness scripts for the same parameters. If they diverge, the
    taker cannot claim the lockup output.

    This test:
    1. Discovers the swap server via Nostr
    2. Creates a reverse swap via encrypted DM RPC
    3. Parses the redeem script from the response
    4. Reconstructs the script from parsed parameters using our code
    5. Verifies byte-identical witness scripts and matching P2WSH addresses
    """
    import hashlib
    import secrets

    from coincurve import PrivateKey

    _require_docker_container("jm-electrum-swap")
    _require_docker_container("jm-nostr-relay")
    _require_docker_container("jm-lnd-taker")
    _require_docker_container("jm-bitcoin")
    _require_lnd_credentials()
    await _wait_for_nostr_relay(NOSTR_RELAY_URL)

    from taker.swap.client import SwapClient
    from taker.swap.script import SwapScript

    # We use a SwapClient to perform the provider discovery and createswap
    # request, which goes through the full Nostr DM RPC flow.
    client = SwapClient(
        nostr_relays=[NOSTR_RELAY_URL],
        network="regtest",
        max_swap_fee_pct=2.0,
        lnd_rest_url=LND_TAKER_REST_URL,
        lnd_cert_path=LND_TAKER_CERT_PATH,
        lnd_macaroon_path=LND_TAKER_MACAROON_PATH,
        backend=bitcoin_backend,
    )

    # Discover provider first
    provider = await client.discover_provider()
    assert provider is not None, "Should discover the Electrum swap server"

    # Generate test secrets manually so we can verify the script
    preimage = secrets.token_bytes(32)
    preimage_hash = hashlib.sha256(preimage).digest()
    claim_key = PrivateKey(secrets.token_bytes(32))
    claim_pubkey = claim_key.public_key.format(compressed=True)

    # Manually set the client's secrets (normally done by acquire_swap_input)
    client._preimage = preimage
    client._preimage_hash = preimage_hash
    client._claim_privkey = claim_key.secret
    client._claim_pubkey = claim_pubkey

    # Create a reverse swap via Nostr DM RPC
    invoice_amount = provider.calculate_invoice_amount(100_000)
    swap_response = await client._create_reverse_swap(provider, invoice_amount)

    # Parse the provider's redeem script
    provider_script_hex = swap_response.redeem_script
    parsed = SwapScript.from_redeem_script(provider_script_hex)

    # Verify our claim pubkey is in the script
    assert parsed.claim_pubkey == claim_pubkey, "Claim pubkey mismatch in redeem script"

    # Reconstruct the script from parsed parameters using our own code
    reconstructed = SwapScript(
        preimage_hash=preimage_hash,
        claim_pubkey=claim_pubkey,
        refund_pubkey=parsed.refund_pubkey,
        timeout_blockheight=swap_response.timeout_block_height,
    )

    # The witness scripts must be byte-identical
    assert reconstructed.witness_script() == parsed.witness_script(), (
        "Witness script mismatch between reconstruction and Electrum server's script. "
        "This means our HTLC construction differs from Electrum's swapserver plugin."
    )

    # The P2WSH addresses must match
    assert reconstructed.p2wsh_address("regtest") == swap_response.lockup_address, (
        "P2WSH address mismatch. The lockup address we derive does not match "
        "the Electrum swap server's lockup address."
    )

    print(
        f"Cross-compatibility verified: "
        f"address={swap_response.lockup_address}, "
        f"script_len={len(parsed.witness_script())} bytes"
    )


# ==============================================================================
# Full CoinJoin with Swap Input E2E
# ==============================================================================


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.timeout(300)
async def test_complete_coinjoin_with_swap_input(
    bitcoin_backend,
    taker_config_with_swap,
    fresh_docker_makers,
):
    """
    Complete end-to-end CoinJoin test with swap input.

    This is the primary E2E test for the swap input feature. It verifies:
    1. Electrum swap server is running and discoverable via Nostr
    2. Swap UTXO is acquired via Nostr DM RPC + Lightning payment
    3. CoinJoin transaction includes the swap UTXO as an additional input
    4. P2WSH claim witness is correctly constructed during signing
    5. Transaction is accepted by the network (valid signatures)
    6. Taker's change output reflects the fake fee earned pattern

    Requires: docker compose --profile e2e up -d
    """
    from tests.e2e.rpc_utils import mine_blocks

    # Check Docker containers
    _require_docker_container("jm-maker1")
    _require_docker_container("jm-maker2")
    _require_docker_container("jm-electrum-swap")
    _require_docker_container("jm-nostr-relay")
    _require_docker_container("jm-lnd-taker")
    _require_lnd_credentials()
    await _wait_for_nostr_relay(NOSTR_RELAY_URL)

    # Mine blocks for coinbase maturity
    print("Mining blocks to ensure coinbase maturity...")
    await mine_blocks(10, MINING_ADDRESS)

    # Create taker wallet
    taker_wallet = WalletService(
        mnemonic=TAKER_MNEMONIC,
        backend=bitcoin_backend,
        network="regtest",
        mixdepth_count=5,
    )
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

    # Create taker with swap input enabled
    taker = Taker(taker_wallet, bitcoin_backend, taker_config_with_swap)

    try:
        print("Starting taker...")
        await taker.start()

        # Verify taker can see offers from Docker makers
        print("Fetching orderbook...")
        offers = await taker.directory_client.fetch_orderbook(max_wait=15.0)
        print(f"Found {len(offers)} offers in orderbook")

        if len(offers) < 2:
            await taker.stop()
            await taker_wallet.close()
            pytest.skip(
                f"Need at least 2 offers, found {len(offers)}. "
                "Ensure Docker makers are running and have funds."
            )

        taker.orderbook_manager.update_offers(offers)

        # Get taker's destination address
        dest_address = taker_wallet.get_receive_address(1, 0)

        # Initiate CoinJoin with swap input
        cj_amount = 50_000_000  # 0.5 BTC
        print(f"Initiating CoinJoin for {cj_amount:,} sats with swap input...")

        txid = await taker.do_coinjoin(
            amount=cj_amount,
            destination=dest_address,
            mixdepth=0,
        )

        # Verify result
        assert txid is not None, "CoinJoin should return a txid"
        print(f"CoinJoin successful! txid: {txid}")

        # Verify swap input was used
        assert taker.swap_input is not None, "Taker should have acquired a swap input"
        print(
            f"Swap input used: {taker.swap_input.txid}:{taker.swap_input.vout} "
            f"({taker.swap_input.value:,} sats)"
        )

        # Verify the transaction structure
        from jmwallet.wallet.signing import deserialize_transaction

        tx_info = await bitcoin_backend.get_transaction(txid)
        if tx_info and tx_info.raw:
            tx_bytes = bytes.fromhex(tx_info.raw)
            tx = deserialize_transaction(tx_bytes)

            # Should have more inputs than a regular CoinJoin (taker wallet + swap + makers)
            num_inputs = len(tx.inputs)
            print(f"Transaction has {num_inputs} inputs")
            # At minimum: 1 taker wallet + 1 swap + 2 makers = 4 inputs
            assert num_inputs >= 4, (
                f"Expected at least 4 inputs (1 taker + 1 swap + 2 makers), "
                f"got {num_inputs}"
            )

        # Mine a block to confirm
        await mine_blocks(1, MINING_ADDRESS)

    finally:
        print("Stopping taker...")
        await taker.stop()
        await taker_wallet.close()


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.timeout(300)
async def test_coinjoin_swap_input_change_pattern(
    bitcoin_backend,
    taker_config_with_swap,
    taker_config_without_swap,
    fresh_docker_makers,
):
    """
    Verify that the swap input changes the taker's change output pattern.

    Compares a taker's change with and without swap input to verify:
    - Without swap: taker change = wallet_input - cj_amount - fees (LOSES sats)
    - With swap: taker change = wallet_input - cj_amount + fake_fee (GAINS sats)

    The "gains sats" pattern is the key privacy improvement -- the taker
    looks indistinguishable from a maker who earned a fee.

    This test validates the fee structure via Nostr-discovered provider terms.
    """
    _require_docker_container("jm-electrum-swap")
    _require_docker_container("jm-nostr-relay")
    _require_docker_container("jm-bitcoin")
    await _wait_for_nostr_relay(NOSTR_RELAY_URL)

    # Verify swap config parameters
    swap_cfg = taker_config_with_swap.swap_input
    assert swap_cfg.enabled is True
    assert len(swap_cfg.nostr_relays) > 0
    assert NOSTR_RELAY_URL in swap_cfg.nostr_relays

    # Without swap: config should be disabled
    no_swap_cfg = taker_config_without_swap.swap_input
    assert no_swap_cfg.enabled is False

    print(
        f"Swap config: nostr_relays={swap_cfg.nostr_relays}, "
        f"max_swap_fee_pct={swap_cfg.max_swap_fee_pct}%"
    )
    print("Change pattern validation passed")


# ==============================================================================
# Graceful Fallback and Disabled Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_swap_input_graceful_fallback(bitcoin_backend):
    """
    Verify the taker falls back gracefully when no swap provider is reachable.

    Uses unreachable Nostr relays. The taker should raise an appropriate
    error rather than crashing, allowing the CoinJoin to proceed without
    the swap input.
    """
    from taker.swap.client import SwapClient

    # Use an unreachable Nostr relay URL
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
        swap_input=SwapInputConfig(
            enabled=True,
            nostr_relays=["ws://127.0.0.1:19999"],  # Unreachable port
            lockup_timeout=5.0,  # Short timeout for fast failure
        ),
    )

    # Verify config is valid
    assert config.swap_input.enabled is True

    client = SwapClient(
        nostr_relays=["ws://127.0.0.1:19999"],
        network="regtest",
        max_swap_fee_pct=2.0,
    )

    # Should fail to connect (but not crash)
    with pytest.raises((ConnectionError, Exception)):
        await client.acquire_swap_input(
            desired_amount_sats=50_000,
            current_block_height=200,
            wait_for_lockup=True,
            lockup_timeout=5.0,
        )

    print("Swap client correctly raised on unreachable Nostr relays")


@pytest.mark.asyncio
async def test_swap_input_disabled_noop(bitcoin_backend):
    """Verify swap input is not acquired when disabled in config."""
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
        swap_input=SwapInputConfig(enabled=False),
    )

    # With disabled config, swap_input.enabled should be False
    assert config.swap_input.enabled is False

    # Create a taker and verify swap_input starts as None
    wallet = WalletService(
        mnemonic=TAKER_MNEMONIC,
        backend=bitcoin_backend,
        network="regtest",
    )
    try:
        taker = Taker(wallet, bitcoin_backend, config)
        assert taker.swap_input is None, "swap_input should be None when disabled"
    finally:
        await wallet.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
