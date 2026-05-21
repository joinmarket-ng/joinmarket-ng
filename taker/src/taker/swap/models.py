"""
Data models for the swap input feature.

Defines swap provider discovery, reverse submarine swap request/response,
swap state tracking, and the SwapInput that gets injected into a CoinJoin.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field
from pydantic.dataclasses import dataclass


class SwapState(StrEnum):
    """State of a reverse submarine swap."""

    IDLE = "idle"
    DISCOVERING = "discovering"  # Querying Nostr relays for providers
    REQUESTING = "requesting"  # Sending createswap RPC to provider
    WAITING_LOCKUP = "waiting_lockup"  # Waiting for provider's on-chain lockup tx
    READY = "ready"  # Swap UTXO confirmed/visible, ready for CoinJoin inclusion
    CLAIMED = "claimed"  # Swap UTXO spent in CoinJoin (preimage revealed)
    REFUND_NEEDED = "refund_needed"  # Swap failed, need to wait for timeout + refund
    REFUNDED = "refunded"  # Successfully refunded after failure
    FAILED = "failed"  # Unrecoverable failure


class SwapProvider(BaseModel):
    """A swap provider discovered via Nostr or configured directly.

    Providers announce their terms via Nostr kind 30315 events (NIP-38).
    The content contains fee rates, limits, and relay URLs for DM-based RPCs.
    """

    # Identity
    offer_id: str = Field(description="Nostr offer event id (hex)")
    pubkey: str = Field(description="Provider's Nostr public key (hex)")

    # Terms
    percentage_fee: float = Field(description="Swap fee as percentage (e.g. 0.5 = 0.5%)")
    mining_fee: int = Field(description="Flat miner fee in sats for the provider's on-chain tx")
    min_amount: int = Field(default=20_000, description="Minimum swap amount in sats")
    max_reverse_amount: int = Field(
        default=5_000_000, description="Maximum reverse swap amount in sats"
    )

    # Communication
    relays: list[str] = Field(
        default_factory=list, description="Nostr relay URLs where the provider listens for DMs"
    )

    # Optional: direct HTTP endpoint (Boltz-compatible)
    http_url: str | None = Field(
        default=None, description="Direct HTTP API URL (alternative to Nostr DMs)"
    )

    # Proof of work (spam prevention)
    pow_bits: int = Field(default=0, description="Proof-of-work bits for this provider's offer")

    def calculate_fee(self, amount_sats: int) -> int:
        """Calculate total swap fee for a given amount.

        Returns:
            Total fee in sats (percentage fee + mining fee).
        """
        pct_fee = int(amount_sats * self.percentage_fee / 100)
        return pct_fee + self.mining_fee

    def calculate_invoice_amount(self, desired_onchain_sats: int) -> int:
        """Calculate the LN invoice amount needed to receive desired on-chain sats.

        The provider deducts fees from the invoice amount to determine the on-chain output.
        So we need to pay: desired_onchain + fees(invoice_amount).

        Since fees are a percentage of the invoice amount (not the output),
        we solve: onchain = invoice - fee(invoice)
                  onchain = invoice * (1 - pct/100) - mining_fee
                  invoice = (onchain + mining_fee) / (1 - pct/100)

        Returns:
            Invoice amount in sats.
        """
        import math

        return math.ceil((desired_onchain_sats + self.mining_fee) / (1 - self.percentage_fee / 100))


class ReverseSwapRequest(BaseModel):
    """Request to create a reverse submarine swap (LN -> on-chain).

    Sent as an encrypted Nostr DM to the swap provider.
    """

    method: str = Field(default="createswap", description="RPC method name")
    type: str = Field(default="reversesubmarine", description="Swap type")
    pair_id: str = Field(default="BTC/BTC", alias="pairId", description="Trading pair")
    invoice_amount: int = Field(alias="invoiceAmount", description="LN invoice amount in sats")
    preimage_hash: str = Field(
        alias="preimageHash", description="SHA256 hash of the preimage (64-char hex)"
    )
    claim_public_key: str = Field(
        alias="claimPublicKey", description="Client's public key for the claim path (66-char hex)"
    )

    model_config = {"populate_by_name": True}


class ReverseSwapResponse(BaseModel):
    """Response from creating a reverse submarine swap.

    Contains the LN invoice to pay and the on-chain lockup details.
    """

    id: str = Field(description="Swap identifier (payment hash hex)")
    invoice: str = Field(description="BOLT11 Lightning invoice to pay")
    miner_fee_invoice: str | None = Field(
        default=None, alias="minerFeeInvoice", description="Optional prepayment invoice for fees"
    )
    lockup_address: str = Field(
        alias="lockupAddress", description="On-chain P2WSH address where provider locks funds"
    )
    redeem_script: str = Field(alias="redeemScript", description="HTLC witness script (hex)")
    timeout_block_height: int = Field(
        alias="timeoutBlockHeight", description="Block height after which provider can refund"
    )
    onchain_amount: int = Field(
        alias="onchainAmount", description="Amount in sats the provider will lock on-chain"
    )

    model_config = {"populate_by_name": True}


@dataclass
class SwapInput:
    """A swap-derived UTXO ready to be injected into a CoinJoin transaction.

    This represents the provider's lockup output that the taker will claim
    by revealing the preimage in the CoinJoin transaction's witness.

    The witness stack for spending this P2WSH output is:
        <signature> <preimage> <witness_script>

    Attributes:
        txid: Transaction ID of the provider's lockup transaction.
        vout: Output index in the lockup transaction.
        value: Output value in satoshis.
        witness_script: The HTLC witness script (raw bytes).
        preimage: The 32-byte preimage for the HTLC.
        claim_privkey: Private key for signing the claim (raw 32 bytes).
        lockup_address: The P2WSH address (for verification).
        timeout_block_height: Block height after which the provider can refund.
        swap_id: Swap identifier for tracking.
        redeem_script_hex: Original hex of the redeem script from the provider.
    """

    txid: str
    vout: int
    value: int
    witness_script: bytes
    preimage: bytes
    claim_privkey: bytes
    lockup_address: str
    timeout_block_height: int
    swap_id: str
    redeem_script_hex: str = ""

    @property
    def scriptpubkey(self) -> bytes:
        """Compute the P2WSH scriptPubKey from the witness script."""
        from jmcore.bitcoin import script_to_p2wsh_scriptpubkey

        return script_to_p2wsh_scriptpubkey(self.witness_script)

    @property
    def scriptpubkey_hex(self) -> str:
        """Hex-encoded scriptPubKey."""
        return self.scriptpubkey.hex()

    def to_utxo_dict(self) -> dict[str, int | str]:
        """Convert to the dict format expected by build_coinjoin_tx()."""
        return {
            "txid": self.txid,
            "vout": self.vout,
            "value": self.value,
            "scriptpubkey": self.scriptpubkey_hex,
        }


# Constants for the Electrum-compatible swap protocol
NOSTR_EVENT_KIND_OFFER = 30315  # NIP-38 User Status (replaceable)
NOSTR_EVENT_KIND_DM = 25582  # Ephemeral encrypted DM for RPCs
NOSTR_EVENT_VERSION = 5  # Protocol version (d-tag: "electrum-swapserver-5")
NOSTR_D_TAG = f"electrum-swapserver-{NOSTR_EVENT_VERSION}"

# Swap limits (provider's actual min_amount takes precedence at runtime)
SWAP_TX_VSIZE = 150  # Estimated vsize for fee calculation

# Locktime safety bounds (in blocks)
MIN_LOCKTIME_DELTA = 60  # Minimum blocks the client needs to claim
MAX_LOCKTIME_DELTA = 100  # Reject if locktime > current_height + this

# Proof of work
MIN_POW_BITS = 5  # Minimum PoW bits to accept a provider offer (mainnet providers use 4–8)

# Default Nostr relays for provider discovery
DEFAULT_SWAP_RELAYS = [
    "wss://relay.getalby.com/v1",
    "wss://nos.lol",
    "wss://relay.damus.io",
    "wss://brb.io",
    "wss://relay.primal.net",
]
