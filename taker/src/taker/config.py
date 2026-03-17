"""
Configuration for JoinMarket Taker.
"""

from __future__ import annotations

from enum import StrEnum

from jmcore.config import WalletConfig
from jmcore.models import OfferType
from pydantic import BaseModel, Field, SecretStr, model_validator


class BroadcastPolicy(StrEnum):
    """
    Policy for how to broadcast the final CoinJoin transaction.

    Privacy implications:
    - SELF: Taker broadcasts via own node. Links taker's IP to the transaction (even via Tor).
    - RANDOM_PEER: Random maker selected. If verification fails, tries next maker, falls back
                   to self as last resort. Good balance of privacy and reliability.
    - MULTIPLE_PEERS: Broadcast to N random makers simultaneously (default 3). Redundant and
                      reliable without excessive network footprint. Falls back to self if all fail.
    - NOT_SELF: Try makers sequentially, never self. Maximum privacy - taker never broadcasts.
                WARNING: No fallback if all makers fail!

    Neutrino considerations:
    - Neutrino cannot verify mempool transactions (only confirmed blocks)
    - MULTIPLE_PEERS is recommended and default: sends to multiple makers for redundancy
    - Self-fallback allowed but verification skipped (trusts broadcast succeeded)
    """

    SELF = "self"
    RANDOM_PEER = "random-peer"
    MULTIPLE_PEERS = "multiple-peers"
    NOT_SELF = "not-self"


class MaxCjFee(BaseModel):
    """Maximum CoinJoin fee limits."""

    abs_fee: int = Field(default=500, ge=0, description="Maximum absolute fee in sats")
    rel_fee: str = Field(default="0.001", description="Maximum relative fee (0.001 = 0.1%)")


class TakerConfig(WalletConfig):
    """
    Configuration for taker bot.

    Inherits base wallet configuration from jmcore.config.WalletConfig
    and adds taker-specific settings for CoinJoin execution, PoDLE,
    and broadcasting.
    """

    # CoinJoin settings
    destination_address: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        description="Target address for CJ output, empty = INTERNAL",
    )
    amount: int = Field(default=0, ge=0, description="Amount in sats (0 = sweep)")
    mixdepth: int = Field(default=0, ge=0, description="Source mixdepth")
    counterparty_count: int = Field(
        default=10, ge=1, le=20, description="Number of makers to select"
    )

    # Fee settings
    max_cj_fee: MaxCjFee = Field(
        default_factory=MaxCjFee, description="Maximum CoinJoin fee limits"
    )
    tx_fee_factor: float = Field(
        default=0.2,
        ge=0.0,
        description="Randomization factor for fees (randomized between base and base*(1+factor))",
    )
    fee_rate: float | None = Field(
        default=None,
        gt=0.0,
        description="Manual fee rate in sat/vB (mutually exclusive with fee_block_target)",
    )
    fee_block_target: int | None = Field(
        default=None,
        ge=1,
        le=1008,
        description="Target blocks for fee estimation (mutually exclusive with fee_rate). "
        "Defaults to 3 when connected to full node.",
    )
    bondless_makers_allowance: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of time to choose makers randomly (not by fidelity bond)",
    )
    bond_value_exponent: float = Field(
        default=1.3,
        gt=0.0,
        description="Exponent for fidelity bond value calculation (default 1.3)",
    )
    bondless_makers_allowance_require_zero_fee: bool = Field(
        default=True,
        description="For bondless maker spots, require zero absolute fee (percentage fee OK)",
    )

    # PoDLE settings
    taker_utxo_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum PoDLE index retries per UTXO (reference: 3)",
    )
    taker_utxo_age: int = Field(default=5, ge=1, description="Minimum UTXO confirmations")
    taker_utxo_amtpercent: int = Field(
        default=20, ge=1, le=100, description="Min UTXO value as % of CJ amount"
    )

    # Timeouts
    maker_timeout_sec: int = Field(default=60, ge=10, description="Timeout for maker responses")
    order_wait_time: float = Field(
        default=120.0,
        ge=1.0,
        description=(
            "Maximum seconds to wait for orderbook responses (hard ceiling). "
            "Empirical testing shows 95th percentile response time over Tor is ~101s. "
            "Default 120s provides a 20% buffer."
        ),
    )
    orderbook_min_wait: float = Field(
        default=30.0,
        ge=0.0,
        description=(
            "Minimum seconds to listen before allowing early exit. "
            "Prevents cutting off slow Tor responses during the initial burst."
        ),
    )
    orderbook_quiet_period: float = Field(
        default=15.0,
        ge=1.0,
        description=(
            "Seconds without new offers before exiting early. "
            "After orderbook_min_wait, if no new offers arrive for this long, "
            "all responsive makers are assumed to have replied."
        ),
    )

    # Broadcast policy (privacy vs reliability tradeoff)
    tx_broadcast: BroadcastPolicy = Field(
        default=BroadcastPolicy.MULTIPLE_PEERS,
        description="How to broadcast: self, random-peer, multiple-peers, or not-self",
    )
    broadcast_timeout_sec: int = Field(
        default=30,
        ge=5,
        description="Timeout waiting for maker to broadcast when delegating",
    )
    broadcast_peer_count: int = Field(
        default=3,
        ge=1,
        description="Number of random peers to use for MULTIPLE_PEERS policy",
    )

    # Advanced options
    preferred_offer_type: OfferType = Field(
        default=OfferType.SW0_RELATIVE, description="Preferred offer type"
    )
    minimum_makers: int = Field(default=1, ge=1, description="Minimum number of makers required")
    min_directory_confirmations: int = Field(
        default=1,
        ge=1,
        description="Minimum number of independent directories an offer must appear on to be "
        "accepted (default 1). Set this to >= 2 to prevent single-directory sybil attacks.",
    )
    max_maker_replacement_attempts: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Max attempts to replace non-responsive makers (0 = disabled)",
    )
    select_utxos: bool = Field(
        default=False,
        description="Interactively select UTXOs before CoinJoin (CLI only)",
    )

    # Wallet rescan configuration
    rescan_interval_sec: int = Field(
        default=600,
        ge=60,
        description="Interval in seconds for periodic wallet rescans (default: 10 minutes)",
    )

    @model_validator(mode="after")
    def set_bitcoin_network_default(self) -> TakerConfig:
        """If bitcoin_network is not set, default to the protocol network."""
        if self.bitcoin_network is None:
            object.__setattr__(self, "bitcoin_network", self.network)
        return self

    @model_validator(mode="after")
    def validate_fee_options(self) -> TakerConfig:
        """Ensure fee_rate and fee_block_target are mutually exclusive."""
        if self.fee_rate is not None and self.fee_block_target is not None:
            raise ValueError(
                "Cannot specify both fee_rate and fee_block_target. "
                "Use fee_rate for manual rate, or fee_block_target for estimation."
            )
        return self


class ScheduleEntry(BaseModel):
    """A single entry in a CoinJoin schedule."""

    mixdepth: int = Field(..., ge=0, le=9)
    amount: int | None = Field(
        default=None,
        ge=0,
        description="Amount in satoshis (mutually exclusive with amount_fraction)",
    )
    amount_fraction: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Fraction of balance (0.0-1.0, mutually exclusive with amount)",
    )
    counterparty_count: int = Field(..., ge=1, le=20)
    destination: str = Field(..., description="Destination address or 'INTERNAL'")
    wait_time: float = Field(default=0.0, ge=0.0, description="Wait time after completion")
    rounding: int = Field(default=16, ge=1, description="Significant figures for rounding")
    completed: bool = False

    @model_validator(mode="after")
    def validate_amount_fields(self) -> ScheduleEntry:
        """Ensure exactly one of amount or amount_fraction is set."""
        if self.amount is None and self.amount_fraction is None:
            raise ValueError("Must specify either 'amount' or 'amount_fraction'")
        if self.amount is not None and self.amount_fraction is not None:
            raise ValueError("Cannot specify both 'amount' and 'amount_fraction'")
        return self


class Schedule(BaseModel):
    """CoinJoin schedule for tumbler-style operations."""

    entries: list[ScheduleEntry] = Field(default_factory=list)
    current_index: int = Field(default=0, ge=0)

    def current_entry(self) -> ScheduleEntry | None:
        """Get current schedule entry."""
        if self.current_index >= len(self.entries):
            return None
        return self.entries[self.current_index]

    def advance(self) -> bool:
        """Advance to next entry. Returns True if more entries remain."""
        if self.current_index < len(self.entries):
            self.entries[self.current_index].completed = True
            self.current_index += 1
        return self.current_index < len(self.entries)

    def is_complete(self) -> bool:
        """Check if all entries are complete."""
        return self.current_index >= len(self.entries)
