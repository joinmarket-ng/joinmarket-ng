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
    blacklist_error: bool = False  # True if any maker rejected due to blacklisted commitment
    # Subset of failed_makers that specifically rejected with a "blacklist" error.
    # Used so the taker can tell "minority blacklist rejection" (probably a lying
    # or out-of-sync maker -> ignore + replace the maker) from "majority blacklist
    # rejection" (commitment really is known -> rotate commitment).
    blacklist_makers: list[str] = Field(default_factory=list)

    @property
    def needs_replacement(self) -> bool:
        """True if phase failed due to non-responsive makers (not other errors)."""
        return not self.success and len(self.failed_makers) > 0
