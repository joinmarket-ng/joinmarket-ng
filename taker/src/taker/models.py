"""
Taker data models for CoinJoin protocol state management.

Contains the state enum, session data, and phase result types
used throughout the CoinJoin protocol execution.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from jmcore.encryption import CryptoSession
from jmcore.models import Offer
from pydantic import ConfigDict, Field
from pydantic.dataclasses import dataclass


class TakerState(StrEnum):
    """Taker protocol states."""

    IDLE = "idle"
    FETCHING_ORDERBOOK = "fetching_orderbook"
    SELECTING_MAKERS = "selecting_makers"
    FILLING = "filling"
    AUTHENTICATING = "authenticating"
    RING_INVITING = "ring_inviting"
    RING_PLANNING = "ring_planning"
    RING_OPENING = "ring_opening"
    RING_VERIFYING = "ring_verifying"
    RING_READINESS = "ring_readiness"
    RING_AUTHORIZING_SIGNATURES = "ring_authorizing_signatures"
    BUILDING_TX = "building_tx"
    COLLECTING_SIGNATURES = "collecting_signatures"
    BROADCASTING = "broadcasting"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"  # User cancelled the operation


@dataclass(config=ConfigDict(arbitrary_types_allowed=True))
class MakerSession:
    """Session data for a single maker."""

    nick: str
    offer: Offer
    utxos: list[dict[str, Any]] = Field(default_factory=list)
    cj_address: str = ""
    change_address: str = ""
    pubkey: str = ""  # Maker's NaCl public key (hex)
    auth_pubkey: str = ""  # Maker's EC auth public key from !ioauth (hex)
    crypto: CryptoSession | None = None  # Encryption session with this maker
    signature: dict[str, Any] | None = None
    responded_fill: bool = False
    responded_auth: bool = False
    responded_sig: bool = False
    supports_neutrino_compat: bool = False  # Supports extended UTXO metadata for Neutrino
    # Communication channel used for this session (must be consistent throughout)
    # "direct" = peer-to-peer onion connection
    # "directory:<host>:<port>" = relayed through specific directory
    comm_channel: str = ""


@dataclass
class PhaseResult:
    """Result from a CoinJoin phase with failed maker tracking.

    Used to communicate phase outcomes and enable maker replacement logic.
    """

    success: bool
    failed_makers: list[str] = Field(default_factory=list)
    # Makers whose UTXOs could not be verified because the local backend was
    # unavailable. They are excluded for this round but must not be blacklisted.
    unavailable_makers: list[str] = Field(default_factory=list)
    blacklist_error: bool = False  # True if any maker rejected due to blacklisted commitment
    # Subset of failed_makers that specifically rejected with a "blacklist" error.
    # Used so the taker can tell "minority blacklist rejection" (probably a lying
    # or out-of-sync maker -> ignore + replace the maker) from "majority blacklist
    # rejection" (commitment really is known -> rotate commitment).
    blacklist_makers: list[str] = Field(default_factory=list)
    # True once this auth pass has sent a PoDLE revelation to at least one maker.
    # Auth-stage replacements must use a fresh commitment after that point.
    podle_revealed: bool = False

    @property
    def needs_replacement(self) -> bool:
        """True if phase failed due to makers that can be replaced."""
        return not self.success and bool(self.failed_makers or self.unavailable_makers)
