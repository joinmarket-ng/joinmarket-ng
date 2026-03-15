"""Submarine swap provider for regtest testing.

Implements the Electrum-compatible swap protocol HTTP endpoints:
- GET  /getpairs    -- Provider terms (fees, limits)
- POST /createswap  -- Create a reverse submarine swap
- POST /swapstatus  -- Poll swap lockup transaction status

Two modes of operation:

**Realistic mode** (LND available):
  On createswap, creates a real BOLT11 invoice via LND. The swap stays in
  "invoice.pending" until the taker pays the invoice. A background task monitors
  invoice settlement. Once paid, the provider broadcasts the lockup tx and
  transitions to "transaction.mempool".

**Mock mode** (LND not available):
  On createswap, immediately generates a fake invoice, creates and broadcasts
  the lockup transaction. No Lightning payment is required.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from coincurve import PrivateKey
from fastapi import FastAPI, HTTPException
from loguru import logger
from pydantic import BaseModel, Field

from mock_swap_provider.htlc import (
    build_witness_script,
    script_to_p2wsh_address,
    script_to_p2wsh_scriptpubkey,
)
from mock_swap_provider.lnd import LndProviderClient
from mock_swap_provider.rpc import BitcoinRPC

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------
RPC_URL = os.environ.get("BITCOIN_RPC_URL", "http://localhost:18443")
RPC_USER = os.environ.get("BITCOIN_RPC_USER", "test")
RPC_PASSWORD = os.environ.get("BITCOIN_RPC_PASSWORD", "test")
WALLET_NAME = os.environ.get("SWAP_WALLET_NAME", "swap_provider")
NETWORK = os.environ.get("BITCOIN_NETWORK", "regtest")
HOST = os.environ.get("SWAP_PROVIDER_HOST", "0.0.0.0")
PORT = int(os.environ.get("SWAP_PROVIDER_PORT", "9999"))

# Fee terms
PERCENTAGE_FEE = float(os.environ.get("SWAP_PERCENTAGE_FEE", "0.5"))
MINING_FEE = int(os.environ.get("SWAP_MINING_FEE", "150"))
MIN_AMOUNT = int(os.environ.get("SWAP_MIN_AMOUNT", "20000"))
MAX_REVERSE_AMOUNT = int(os.environ.get("SWAP_MAX_REVERSE_AMOUNT", "5000000"))
LOCKTIME_DELTA = int(os.environ.get("SWAP_LOCKTIME_DELTA", "80"))

# Auto-fund: mine blocks to the provider wallet on startup if balance is low
AUTO_FUND = os.environ.get("SWAP_AUTO_FUND", "true").lower() in ("true", "1", "yes")
AUTO_FUND_MIN_BALANCE = float(os.environ.get("SWAP_AUTO_FUND_MIN_BALANCE", "10.0"))

# LND connection (optional -- enables realistic Lightning invoices)
LND_REST_URL = os.environ.get("LND_REST_URL", "")
LND_CERT_PATH = os.environ.get("LND_CERT_PATH", "")
LND_MACAROON_PATH = os.environ.get("LND_MACAROON_PATH", "")

# ---------------------------------------------------------------------------
# Swap state storage (in-memory, ephemeral)
# ---------------------------------------------------------------------------
swaps: dict[str, dict[str, Any]] = {}

# Background tasks for monitoring invoice settlements
_settlement_tasks: dict[str, asyncio.Task[None]] = {}

# ---------------------------------------------------------------------------
# RPC and LND clients (initialized at startup)
# ---------------------------------------------------------------------------
rpc: BitcoinRPC | None = None
lnd: LndProviderClient | None = None
lnd_available: bool = False


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------
class CreateSwapRequest(BaseModel):
    """Request body for POST /createswap."""

    method: str = "createswap"
    type: str = "reversesubmarine"
    pair_id: str = Field(default="BTC/BTC", alias="pairId")
    invoice_amount: int = Field(alias="invoiceAmount")
    preimage_hash: str = Field(alias="preimageHash")
    claim_public_key: str = Field(alias="claimPublicKey")

    model_config = {"populate_by_name": True}


class SwapStatusRequest(BaseModel):
    """Request body for POST /swapstatus."""

    id: str


# ---------------------------------------------------------------------------
# Background task: monitor invoice settlement and broadcast lockup
# ---------------------------------------------------------------------------
async def _monitor_invoice_settlement(swap_id: str) -> None:
    """Background task that waits for an LN invoice to be settled.

    When the invoice is paid:
    1. Creates and broadcasts the lockup transaction
    2. Updates the swap status to "transaction.mempool"

    If the invoice expires or is canceled, marks the swap as failed.
    """
    swap = swaps.get(swap_id)
    if swap is None:
        logger.error(f"Settlement monitor: swap {swap_id} not found")
        return

    assert lnd is not None
    assert rpc is not None

    ln_invoice_hash = swap.get("ln_invoice_hash", "")
    if not ln_invoice_hash:
        logger.error(f"Settlement monitor: no LN invoice hash for swap {swap_id}")
        return

    logger.info(f"Monitoring invoice settlement for swap {swap_id[:16]}...")

    # Wait for the invoice to be settled (up to 10 minutes)
    settled = await lnd.wait_for_invoice_settlement(
        r_hash_hex=ln_invoice_hash,
        timeout=600.0,
        poll_interval=1.0,
    )

    if not settled:
        logger.warning(f"Invoice not settled for swap {swap_id[:16]}... (timeout/canceled)")
        swap["status"] = "swap.expired"
        return

    # Invoice paid! Now broadcast the lockup transaction.
    logger.info(f"Invoice settled for swap {swap_id[:16]}..., broadcasting lockup tx...")

    try:
        txid, signed_hex, lockup_vout = await _create_and_broadcast_lockup(swap)
        swap["status"] = "transaction.mempool"
        swap["lockup_txid"] = txid
        swap["lockup_hex"] = signed_hex
        swap["lockup_vout"] = lockup_vout
        logger.info(f"Lockup tx broadcast for swap {swap_id[:16]}...: txid={txid}")
    except Exception as e:
        logger.error(f"Failed to broadcast lockup for swap {swap_id[:16]}...: {e}")
        swap["status"] = "transaction.failed"


async def _create_and_broadcast_lockup(swap: dict[str, Any]) -> tuple[str, str, int]:
    """Create, fund, sign, and broadcast a lockup transaction.

    Returns:
        (txid, signed_hex, lockup_vout) tuple.
    """
    assert rpc is not None

    onchain_amount = swap["onchain_amount"]
    lockup_address = swap["lockup_address"]
    witness_script_hex = swap["redeem_script"]
    witness_script = bytes.fromhex(witness_script_hex)

    # Create the lockup transaction
    onchain_btc = onchain_amount / 1e8
    raw_tx = await rpc.create_raw_transaction(
        inputs=[],
        outputs=[{lockup_address: round(onchain_btc, 8)}],
    )

    # Fund from the provider wallet
    funded = await rpc.fund_raw_transaction(raw_tx, {"fee_rate": 10})
    funded_hex = funded["hex"]

    # Sign
    signed = await rpc.sign_raw_transaction(funded_hex)
    if not signed.get("complete"):
        raise RuntimeError(f"Transaction signing incomplete: {signed}")
    signed_hex = signed["hex"]

    # Broadcast
    txid = await rpc.send_raw_transaction(signed_hex)
    logger.info(f"Lockup tx broadcast: txid={txid}, amount={onchain_amount} sats")

    # Find the vout
    decoded = await rpc.decode_raw_transaction(signed_hex)
    expected_spk = script_to_p2wsh_scriptpubkey(witness_script).hex()
    lockup_vout = -1
    for vout_data in decoded.get("vout", []):
        spk_hex = vout_data.get("scriptPubKey", {}).get("hex", "")
        if spk_hex == expected_spk:
            lockup_vout = vout_data["n"]
            break

    if lockup_vout < 0:
        raise RuntimeError("Lockup output not found in transaction")

    return txid, signed_hex, lockup_vout


# ---------------------------------------------------------------------------
# Startup / shutdown
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:  # type: ignore[misc]
    """Initialize Bitcoin RPC, LND client, and fund the provider wallet."""
    global rpc, lnd, lnd_available

    rpc = BitcoinRPC(
        url=RPC_URL,
        user=RPC_USER,
        password=RPC_PASSWORD,
        wallet=WALLET_NAME,
    )

    # Create/load the provider wallet
    logger.info(f"Connecting to Bitcoin Core at {RPC_URL} (wallet: {WALLET_NAME})")
    try:
        await rpc.create_wallet(WALLET_NAME)
        logger.info(f"Wallet '{WALLET_NAME}' ready")
    except Exception as e:
        logger.error(f"Failed to create/load wallet: {e}")
        raise

    # Auto-fund if needed
    if AUTO_FUND:
        try:
            balance = await rpc.get_balance()
            if balance < AUTO_FUND_MIN_BALANCE:
                logger.info(
                    f"Balance {balance:.8f} BTC below threshold "
                    f"{AUTO_FUND_MIN_BALANCE:.8f} BTC, mining blocks..."
                )
                addr = await rpc.get_new_address("funding")
                # Mine 110 blocks (100 for coinbase maturity + 10 extra)
                await rpc.generate_to_address(110, addr)
                new_balance = await rpc.get_balance()
                logger.info(f"Funded provider wallet: {new_balance:.8f} BTC")
            else:
                logger.info(f"Provider wallet balance: {balance:.8f} BTC")
        except Exception as e:
            logger.warning(f"Auto-fund failed (provider may not have funds): {e}")

    # Initialize LND client if configured
    if LND_REST_URL and LND_CERT_PATH and LND_MACAROON_PATH:
        lnd = LndProviderClient(
            rest_url=LND_REST_URL,
            cert_path=LND_CERT_PATH,
            macaroon_path=LND_MACAROON_PATH,
        )
        lnd_available = await lnd.is_available()
        if lnd_available:
            logger.info("LND connected -- using REALISTIC mode (real Lightning invoices)")
        else:
            logger.warning("LND configured but not available -- falling back to MOCK mode")
    else:
        logger.info("No LND configured -- using MOCK mode (fake invoices, immediate lockup)")

    block_height = await rpc.get_block_count()
    mode = "REALISTIC" if lnd_available else "MOCK"
    logger.info(f"Swap provider ready ({mode} mode). Block height: {block_height}")

    yield

    # Cleanup
    logger.info("Shutting down swap provider")
    for task in _settlement_tasks.values():
        task.cancel()
    _settlement_tasks.clear()
    if lnd:
        await lnd.close()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Swap Provider",
    description="Electrum-compatible reverse submarine swap provider for regtest",
    lifespan=lifespan,
)


@app.get("/getpairs")
async def get_pairs() -> dict[str, Any]:
    """Return provider terms (fees and limits).

    Response matches the Electrum swap server format expected by
    HTTPSwapTransport.provider_from_pairs().
    """
    return {
        "percentage_fee": PERCENTAGE_FEE,
        "mining_fee": MINING_FEE,
        "min_amount": MIN_AMOUNT,
        "max_reverse_amount": MAX_REVERSE_AMOUNT,
    }


@app.post("/createswap")
async def create_swap(request: CreateSwapRequest) -> dict[str, Any]:
    """Create a reverse submarine swap.

    Realistic mode (LND available):
      1. Validate the request
      2. Generate server keypair (refund key)
      3. Build HTLC witness script
      4. Create a real BOLT11 invoice via LND
      5. Store swap as "invoice.pending"
      6. Start background task to monitor invoice settlement
      7. Return swap details (taker must pay the invoice)

    Mock mode (no LND):
      1-3. Same as above
      4. Create and broadcast lockup transaction immediately
      5. Return a fake invoice + lockup details
    """
    assert rpc is not None, "RPC not initialized"

    # Validate
    if request.type != "reversesubmarine":
        raise HTTPException(400, f"Unsupported swap type: {request.type}")

    preimage_hash = bytes.fromhex(request.preimage_hash)
    if len(preimage_hash) != 32:
        raise HTTPException(400, f"preimageHash must be 32 bytes, got {len(preimage_hash)}")

    claim_pubkey = bytes.fromhex(request.claim_public_key)
    if len(claim_pubkey) != 33:
        raise HTTPException(400, f"claimPublicKey must be 33 bytes, got {len(claim_pubkey)}")

    if request.invoice_amount < MIN_AMOUNT:
        raise HTTPException(400, f"invoiceAmount below minimum ({MIN_AMOUNT})")
    if request.invoice_amount > MAX_REVERSE_AMOUNT:
        raise HTTPException(400, f"invoiceAmount above maximum ({MAX_REVERSE_AMOUNT})")

    # Calculate on-chain amount (deduct fees from invoice amount)
    pct_fee = int(request.invoice_amount * PERCENTAGE_FEE / 100)
    onchain_amount = request.invoice_amount - pct_fee - MINING_FEE
    if onchain_amount <= 0:
        raise HTTPException(400, "Fees exceed invoice amount")

    # Generate server keypair (for refund path)
    refund_privkey_bytes = secrets.token_bytes(32)
    refund_privkey = PrivateKey(refund_privkey_bytes)
    refund_pubkey = refund_privkey.public_key.format(compressed=True)

    # Calculate timeout
    current_height = await rpc.get_block_count()
    timeout_block_height = current_height + LOCKTIME_DELTA

    # Build the HTLC witness script
    witness_script = build_witness_script(
        preimage_hash=preimage_hash,
        claim_pubkey=claim_pubkey,
        refund_pubkey=refund_pubkey,
        timeout_blockheight=timeout_block_height,
    )

    # Derive P2WSH address
    lockup_address = script_to_p2wsh_address(witness_script, NETWORK)
    redeem_script_hex = witness_script.hex()

    # Generate a swap ID (use the preimage hash as the swap ID, like Electrum does)
    swap_id = preimage_hash.hex()

    if lnd_available and lnd is not None:
        # ---- REALISTIC MODE: Create real LN invoice ----
        return await _create_swap_realistic(
            request=request,
            swap_id=swap_id,
            onchain_amount=onchain_amount,
            lockup_address=lockup_address,
            redeem_script_hex=redeem_script_hex,
            timeout_block_height=timeout_block_height,
            refund_privkey_bytes=refund_privkey_bytes,
            preimage_hash=preimage_hash,
            claim_pubkey=claim_pubkey,
            witness_script=witness_script,
        )
    else:
        # ---- MOCK MODE: Immediate lockup broadcast ----
        return await _create_swap_mock(
            request=request,
            swap_id=swap_id,
            onchain_amount=onchain_amount,
            lockup_address=lockup_address,
            redeem_script_hex=redeem_script_hex,
            timeout_block_height=timeout_block_height,
            refund_privkey_bytes=refund_privkey_bytes,
            witness_script=witness_script,
        )


async def _create_swap_realistic(
    *,
    request: CreateSwapRequest,
    swap_id: str,
    onchain_amount: int,
    lockup_address: str,
    redeem_script_hex: str,
    timeout_block_height: int,
    refund_privkey_bytes: bytes,
    preimage_hash: bytes,
    claim_pubkey: bytes,
    witness_script: bytes,
) -> dict[str, Any]:
    """Create a swap with a real LN invoice (realistic mode)."""
    assert lnd is not None

    # Create a real BOLT11 invoice via LND
    invoice_result = await lnd.add_invoice(
        r_hash=preimage_hash,
        value_sats=request.invoice_amount,
        memo=f"Reverse swap {swap_id[:16]}",
        expiry=3600,
    )

    bolt11_invoice = invoice_result["payment_request"]
    ln_invoice_hash = invoice_result.get("r_hash_hex", "")

    # Store swap state as pending (lockup not yet broadcast)
    swaps[swap_id] = {
        "id": swap_id,
        "status": "invoice.pending",
        "onchain_amount": onchain_amount,
        "lockup_address": lockup_address,
        "redeem_script": redeem_script_hex,
        "timeout_block_height": timeout_block_height,
        "refund_privkey": refund_privkey_bytes.hex(),
        "claim_pubkey": request.claim_public_key,
        "preimage_hash": request.preimage_hash,
        "invoice": bolt11_invoice,
        "ln_invoice_hash": ln_invoice_hash,
        # Lockup fields populated after payment
        "lockup_txid": "",
        "lockup_hex": "",
        "lockup_vout": -1,
    }

    # Start background task to monitor invoice settlement
    task = asyncio.create_task(
        _monitor_invoice_settlement(swap_id),
        name=f"settlement-{swap_id[:16]}",
    )
    _settlement_tasks[swap_id] = task

    logger.info(
        f"Swap created (REALISTIC): id={swap_id[:16]}..., "
        f"invoice_amount={request.invoice_amount}, "
        f"onchain_amount={onchain_amount}, lockup={lockup_address}, "
        f"timeout={timeout_block_height}, "
        f"ln_hash={ln_invoice_hash[:16]}..."
    )

    return {
        "id": swap_id,
        "invoice": bolt11_invoice,
        "lockupAddress": lockup_address,
        "redeemScript": redeem_script_hex,
        "timeoutBlockHeight": timeout_block_height,
        "onchainAmount": onchain_amount,
    }


async def _create_swap_mock(
    *,
    request: CreateSwapRequest,
    swap_id: str,
    onchain_amount: int,
    lockup_address: str,
    redeem_script_hex: str,
    timeout_block_height: int,
    refund_privkey_bytes: bytes,
    witness_script: bytes,
) -> dict[str, Any]:
    """Create a swap with immediate lockup broadcast (mock mode)."""
    assert rpc is not None

    # Create the lockup transaction immediately (no LN payment needed)
    swap_data: dict[str, Any] = {
        "id": swap_id,
        "onchain_amount": onchain_amount,
        "lockup_address": lockup_address,
        "redeem_script": redeem_script_hex,
    }

    txid, signed_hex, lockup_vout = await _create_and_broadcast_lockup(swap_data)

    # Store swap state
    swaps[swap_id] = {
        "id": swap_id,
        "status": "transaction.mempool",
        "lockup_txid": txid,
        "lockup_vout": lockup_vout,
        "lockup_hex": signed_hex,
        "onchain_amount": onchain_amount,
        "lockup_address": lockup_address,
        "redeem_script": redeem_script_hex,
        "timeout_block_height": timeout_block_height,
        "refund_privkey": refund_privkey_bytes.hex(),
        "claim_pubkey": request.claim_public_key,
        "preimage_hash": request.preimage_hash,
    }

    # Generate a fake BOLT11 invoice (not used in regtest mock mode)
    fake_invoice = f"lnbcrt{request.invoice_amount}n1pswap{secrets.token_hex(16)}mock{swap_id[:16]}"

    logger.info(
        f"Swap created (MOCK): id={swap_id[:16]}..., "
        f"invoice_amount={request.invoice_amount}, "
        f"onchain_amount={onchain_amount}, lockup={lockup_address}, "
        f"timeout={timeout_block_height}"
    )

    return {
        "id": swap_id,
        "invoice": fake_invoice,
        "lockupAddress": lockup_address,
        "redeemScript": redeem_script_hex,
        "timeoutBlockHeight": timeout_block_height,
        "onchainAmount": onchain_amount,
    }


@app.post("/swapstatus")
async def swap_status(request: SwapStatusRequest) -> dict[str, Any]:
    """Poll the status of a swap.

    Returns the lockup transaction details when available.
    Status progression:
      - "invoice.pending"       : Invoice created, waiting for payment
      - "invoice.settled"       : Invoice paid, lockup being broadcast (transient)
      - "transaction.mempool"   : Lockup tx broadcast, in mempool
      - "transaction.confirmed" : Lockup tx confirmed
      - "swap.expired"          : Invoice expired or swap timed out
      - "transaction.failed"    : Lockup broadcast failed
    """
    swap = swaps.get(request.id)
    if swap is None:
        raise HTTPException(404, f"Swap not found: {request.id}")

    status = swap["status"]

    # Check if the lockup tx has been confirmed
    if status == "transaction.mempool" and rpc is not None:
        try:
            tx_info = await rpc.get_raw_transaction(swap["lockup_txid"], verbose=True)
            confirmations = tx_info.get("confirmations", 0)
            if confirmations > 0:
                swap["status"] = "transaction.confirmed"
                status = "transaction.confirmed"
        except Exception:
            pass  # tx might not be in mempool yet

    result: dict[str, Any] = {"status": status}

    if status in ("transaction.mempool", "transaction.confirmed"):
        result["transaction"] = {
            "id": swap["lockup_txid"],
            "hex": swap["lockup_hex"],
        }

    return result


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    mode = "realistic" if lnd_available else "mock"
    return {"status": "ok", "mode": mode}


@app.get("/mode")
async def mode() -> dict[str, Any]:
    """Return the current operating mode and LND status."""
    return {
        "mode": "REALISTIC" if lnd_available else "MOCK",
        "lnd_available": lnd_available,
        "lnd_configured": bool(LND_REST_URL),
        "active_swaps": len(swaps),
        "pending_settlements": len(_settlement_tasks),
    }


def main() -> None:
    """Entry point for the swap provider."""
    logger.info(f"Starting swap provider on {HOST}:{PORT}")
    uvicorn.run(
        "mock_swap_provider.main:app",
        host=HOST,
        port=PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
