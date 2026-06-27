"""
Data models for the bidirectional Taproot Lightning swap protocol.

Direction conventions (the *provider* is always the swap "server"):

* ``SWAP_IN``  - provider receives on-chain, pays Lightning. The client funds an
  on-chain HTLC and receives a Lightning payment. Mechanically a *submarine*
  swap; the client generates the preimage and the Lightning invoice it wants
  paid, the provider claims the on-chain HTLC with the preimage learned from
  settling that payment.
* ``SWAP_OUT`` - provider pays on-chain, receives Lightning. The client pays a
  Lightning hold invoice and claims an on-chain HTLC. Mechanically a *reverse*
  swap; the client generates the preimage, the provider funds the on-chain HTLC
  to the client's claim key and settles the hold invoice with the revealed
  preimage.

The same ``[provider, client]`` MuSig2 aggregate keys a single P2TR funding
output in both directions (see :mod:`jmswap.swap_tree`).
"""

from __future__ import annotations

import time
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SwapDirection(StrEnum):
    """Direction of a swap, named from the provider's point of view."""

    SWAP_IN = "swapin"  # provider receives on-chain, pays Lightning (submarine)
    SWAP_OUT = "swapout"  # provider pays on-chain, receives Lightning (reverse)

    @property
    def is_reverse(self) -> bool:
        """True for the reverse-swap (swap-out) tree layout."""
        return self is SwapDirection.SWAP_OUT


class SwapState(StrEnum):
    """Lifecycle of a single swap, shared by client and provider records."""

    CREATED = "created"  # negotiated, nothing on-chain or paid yet
    AWAITING_LOCKUP = "awaiting_lockup"  # funding tx not yet seen
    LOCKUP_SEEN = "lockup_seen"  # funding tx in mempool
    LOCKUP_CONFIRMED = "lockup_confirmed"  # funding tx confirmed
    INVOICE_PAID = "invoice_paid"  # the Lightning leg has been paid/held
    CLAIMED = "claimed"  # on-chain HTLC claimed with preimage (success)
    REFUNDED = "refunded"  # on-chain HTLC refunded after timeout
    FAILED = "failed"  # aborted before completion
    EXPIRED = "expired"  # locktime reached without completion

    @property
    def is_terminal(self) -> bool:
        return self in (
            SwapState.CLAIMED,
            SwapState.REFUNDED,
            SwapState.FAILED,
            SwapState.EXPIRED,
        )


# Allowed forward transitions (defensive state-machine guard).
_TRANSITIONS: dict[SwapState, set[SwapState]] = {
    SwapState.CREATED: {SwapState.AWAITING_LOCKUP, SwapState.FAILED, SwapState.EXPIRED},
    SwapState.AWAITING_LOCKUP: {SwapState.LOCKUP_SEEN, SwapState.FAILED, SwapState.EXPIRED},
    SwapState.LOCKUP_SEEN: {SwapState.LOCKUP_CONFIRMED, SwapState.FAILED, SwapState.EXPIRED},
    SwapState.LOCKUP_CONFIRMED: {
        SwapState.INVOICE_PAID,
        SwapState.CLAIMED,
        SwapState.REFUNDED,
        SwapState.FAILED,
        SwapState.EXPIRED,
    },
    SwapState.INVOICE_PAID: {SwapState.CLAIMED, SwapState.REFUNDED, SwapState.EXPIRED},
    SwapState.CLAIMED: set(),
    SwapState.REFUNDED: set(),
    SwapState.FAILED: set(),
    SwapState.EXPIRED: set(),
}


def can_transition(src: SwapState, dst: SwapState) -> bool:
    """Return True if ``src -> dst`` is a permitted swap state transition."""
    return dst in _TRANSITIONS.get(src, set())


class ProviderTerms(BaseModel):
    """A swap provider's advertised terms (the payload behind a Nostr advert)."""

    model_config = ConfigDict(frozen=True)

    provider_id: str  # stable identifier (e.g. Nostr x-only pubkey hex)
    provider_pubkey: str  # 33-byte compressed swap key, hex (MuSig2 first key)
    network: str = "mainnet"
    directions: list[SwapDirection] = Field(
        default_factory=lambda: [SwapDirection.SWAP_IN, SwapDirection.SWAP_OUT]
    )
    min_amount: int = 10_000  # sats
    max_amount: int = 25_000_000  # sats
    percentage_fee: Decimal = Decimal("0.1")  # percent of the swapped amount
    base_fee: int = 500  # flat sats, covers the provider's on-chain footprint
    cltv_delta: int = 144  # refund timelock distance in blocks
    accepts_whole_coin: bool = False  # third-role: absorb a whole UTXO, no change
    name: str | None = None

    @field_validator("provider_pubkey")
    @classmethod
    def _check_pubkey(cls, value: str) -> str:
        raw = bytes.fromhex(value)
        if len(raw) != 33 or raw[0] not in (0x02, 0x03):
            raise ValueError("provider_pubkey must be 33-byte compressed hex")
        return value

    def supports(self, direction: SwapDirection) -> bool:
        return direction in self.directions

    def within_limits(self, amount: int) -> bool:
        return self.min_amount <= amount <= self.max_amount

    def fee_for(self, amount: int) -> int:
        """Total provider fee for swapping ``amount`` sats (percentage + base)."""
        pct = int((Decimal(amount) * self.percentage_fee / Decimal(100)).to_integral_value())
        return pct + self.base_fee


class SwapRequest(BaseModel):
    """Client -> provider request to open a swap (either direction)."""

    direction: SwapDirection
    amount: int  # the swap amount in sats (what the client moves between layers)
    preimage_hash: str  # 32-byte hex; the client generates the preimage
    client_pubkey: str  # 33-byte compressed hex; second MuSig2 key
    # swap-in only: the Lightning invoice the client wants the provider to pay.
    invoice: str | None = None
    whole_coin: bool = False  # third-role hint (no client change)

    @field_validator("preimage_hash")
    @classmethod
    def _check_hash(cls, value: str) -> str:
        if len(bytes.fromhex(value)) != 32:
            raise ValueError("preimage_hash must be 32-byte hex")
        return value

    @field_validator("client_pubkey")
    @classmethod
    def _check_pubkey(cls, value: str) -> str:
        raw = bytes.fromhex(value)
        if len(raw) != 33 or raw[0] not in (0x02, 0x03):
            raise ValueError("client_pubkey must be 33-byte compressed hex")
        return value


class SwapResponse(BaseModel):
    """Provider -> client response establishing the on-chain contract."""

    swap_id: str
    direction: SwapDirection
    server_pubkey: str  # provider's 33-byte compressed swap key, hex
    funding_address: str  # the P2TR swap address both sides compute
    onchain_amount: int  # sats locked in the funding output
    locktime: int  # absolute block height for the refund leaf
    swap_tree: dict[str, dict[str, object]]  # Boltz v2 swapTree JSON for verification
    # swap-out only: the Lightning hold invoice the client pays.
    invoice: str | None = None


class SwapContract(BaseModel):
    """The fully negotiated swap parameters, sufficient to rebuild the tree.

    Persisted by both parties so a swap can be recovered/refunded after a crash.
    """

    swap_id: str
    direction: SwapDirection
    network: str
    server_pubkey: str
    client_pubkey: str
    preimage_hash: str
    locktime: int
    onchain_amount: int
    funding_address: str
    state: SwapState = SwapState.CREATED
    created_at: float = Field(default_factory=time.time)
    # known only to the preimage owner; persisted only on that side.
    preimage: str | None = None
    invoice: str | None = None
    lockup_txid: str | None = None
    lockup_vout: int | None = None
    spend_txid: str | None = None

    def advance(self, new_state: SwapState) -> None:
        """Move to ``new_state``, enforcing the state machine."""
        from jmswap.errors import SwapStateError

        if new_state == self.state:
            return
        if not can_transition(self.state, new_state):
            raise SwapStateError(f"illegal swap transition {self.state} -> {new_state}")
        self.state = new_state
