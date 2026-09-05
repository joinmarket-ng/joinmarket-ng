"""
Configuration for JoinMarket Taker.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from jmcore.config import WalletConfig
from jmcore.models import OfferType, normalize_relative_fee
from jmcore.randomness import secure_random
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

# Default counterparty count is randomized per CoinJoin in [MIN, MAX] when no
# explicit value is configured.  The 8-10 range matches the upstream
# JoinMarket sendpayment default and avoids fingerprinting jm-ng takers via a
# fixed counterparty count (see issue #468).
DEFAULT_COUNTERPARTY_COUNT_MIN = 8
DEFAULT_COUNTERPARTY_COUNT_MAX = 10


def resolve_counterparty_count(value: int | None) -> int:
    """Resolve an effective counterparty count for one CoinJoin attempt.

    When ``value`` is ``None``, draws a uniformly random integer from
    ``[DEFAULT_COUNTERPARTY_COUNT_MIN, DEFAULT_COUNTERPARTY_COUNT_MAX]``.
    Otherwise the explicit value is returned unchanged.
    """
    if value is None:
        return secure_random.randint(DEFAULT_COUNTERPARTY_COUNT_MIN, DEFAULT_COUNTERPARTY_COUNT_MAX)
    return value


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

    @field_validator("rel_fee", mode="before")
    @classmethod
    def normalize_rel_fee(cls, v: str | float | int) -> str:
        """Validate and normalize the serialized relative-fee limit."""
        return normalize_relative_fee(v, "rel_fee")


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
    counterparty_count: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description=(
            "Number of makers to select. When unset, a random value in "
            "[8, 10] is drawn for every CoinJoin (matches the upstream "
            "JoinMarket sendpayment default and avoids fingerprinting via a "
            "fixed counterparty count)."
        ),
    )

    # Fee settings
    max_cj_fee: MaxCjFee = Field(
        default_factory=MaxCjFee, description="Maximum CoinJoin fee limits"
    )
    require_quantized_cj_fees: bool = Field(
        default=True,
        description="Only consider offers whose advertised CoinJoin fee is on the public grid",
    )
    round_up_cj_fees: bool = Field(
        default=False,
        description="Round each selected maker fee up to the next public fee quantum",
    )
    equalize_cj_fees: bool = Field(
        default=False,
        description="Pay every selected maker the highest realized fee in the selected set",
    )
    max_sweep_fee_change: float = Field(
        default=0.8,
        ge=0.0,
        allow_inf_nan=False,
        description="Maximum relative amount a sweep fee estimate may exceed its selected budget",
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
    max_fee_rate_sat_vb: float = Field(
        default=1_000.0,
        gt=0.0,
        allow_inf_nan=False,
        description=(
            "Safety cap on the resolved fee rate (sat/vB) for the CoinJoin "
            "transaction. Manual rates and backend estimates above this cap "
            "are rejected, and randomization is limited to this cap, protecting against "
            "runaway-fee bugs and malicious fee oracles."
        ),
    )
    min_fee_rate_sat_vb: float = Field(
        default=1.0,
        gt=0.0,
        allow_inf_nan=False,
        description="Minimum CoinJoin miner fee rate in sat/vB",
    )
    min_fee_block_target: int = Field(
        default=10,
        ge=1,
        le=1008,
        description="Block target for the conservative CoinJoin miner-fee floor",
    )
    bondless_makers_allowance: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Per-slot probability of selecting uniformly from zero-fee offers",
    )
    bond_value_exponent: float = Field(
        default=1.3,
        gt=0.0,
        description="Exponent for fidelity bond value calculation (default 1.3)",
    )
    bondless_makers_allowance_require_zero_fee: bool = Field(
        default=True,
        description=(
            "Restrict allowance spots to zero-fee offers and reject fee-charging bondless offers"
        ),
    )
    max_maker_utxos: int = Field(
        default=15,
        ge=0,
        description=(
            "Maximum number of inputs a single maker may contribute. The taker "
            "pays the mining fee for every input in the CoinJoin, so an "
            "unbounded input count lets a counterparty consolidate its UTXOs at "
            "the taker's expense. Makers exceeding the cap are dropped (and "
            "replaced when possible). 0 disables the cap (not recommended)."
        ),
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
    external_podle_mode: Literal["disabled", "only"] = Field(
        default="disabled",
        description=(
            "Use externally imported PoDLE credentials only. Their backing UTXOs are "
            "verified but never selected as CoinJoin inputs."
        ),
    )

    # Timeouts
    maker_timeout_sec: int = Field(
        default=60, ge=10, le=3600, description="Timeout for maker responses"
    )
    initial_confirmation_timeout_sec: int = Field(
        default=300,
        ge=0,
        le=3600,
        description=(
            "Maximum seconds to approve the initial maker and fee preview before the "
            "prepared CoinJoin expires. 0 disables expiry."
        ),
    )
    order_wait_time: float = Field(
        default=120.0,
        ge=1.0,
        le=3600.0,
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
        le=3600,
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
    minimum_makers: int = Field(
        default=4,
        ge=1,
        description=(
            "Minimum number of makers required for the CoinJoin to proceed. "
            "Default 4 matches the upstream JoinMarket POLICY default."
        ),
    )
    max_maker_replacement_attempts: int = Field(
        default=3,
        ge=0,
        le=10,
        description=(
            "Maximum attempts to restore the requested maker count after fill or auth "
            "failures (0 = disabled). The CoinJoin may proceed at minimum_makers only "
            "after replacement attempts are exhausted or no candidates remain."
        ),
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

    # Pending transaction monitoring
    pending_tx_abandon_hours: int = Field(
        default=24,
        ge=1,
        le=336,
        description=(
            "Hours to monitor a recorded CoinJoin for confirmation before a local "
            "monitoring timeout. Also sets the pending allowance in the input reservation "
            "lifetime. Explicit wallet refresh can reconcile later confirmation. Default 24 h."
        ),
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
        if self.min_fee_rate_sat_vb > self.max_fee_rate_sat_vb:
            raise ValueError("min_fee_rate_sat_vb must not exceed max_fee_rate_sat_vb")
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
