"""
Maker bot configuration.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum

from jmcore.config import TorControlConfig, WalletConfig, create_tor_control_config_from_env
from jmcore.models import OfferType
from jmcore.tor_control import HiddenServiceDoSConfig
from pydantic import BaseModel, Field, field_validator, model_validator

from maker.mixdepth_selection import MixdepthSelectionPolicy


def normalize_decimal_string(v: str | float | int) -> str:
    """
    Normalize a decimal value to avoid scientific notation.

    Pydantic may coerce float values (from env vars, TOML, or JSON) to strings,
    which can result in scientific notation for small values (e.g., 1e-05).
    The JoinMarket protocol expects decimal notation (e.g., 0.00001).
    """
    if isinstance(v, (int, float)):
        # Use Decimal to preserve precision and avoid scientific notation
        return format(Decimal(str(v)), "f")
    # Already a string - check if it contains scientific notation
    if "e" in v.lower():
        try:
            return format(Decimal(v), "f")
        except InvalidOperation:
            pass  # Let pydantic handle the validation error
    return v


def validate_relative_cj_fee(value: str, randomization_factor: float = 0.0) -> None:
    """Validate the full relative-fee range produced for protocol offers."""
    try:
        fee = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"cj_fee_relative must be a valid number, got {value}") from exc
    if not fee.is_finite():
        raise ValueError(f"cj_fee_relative must be a valid number, got {value}")
    if fee <= 0:
        raise ValueError(f"cj_fee_relative must be > 0 for relative offer types, got {value}")
    if fee >= 1:
        raise ValueError(f"cj_fee_relative must be < 1 for relative offer types, got {value}")
    factor = Decimal(str(randomization_factor))
    if factor > 1:
        raise ValueError(
            "cjfee_factor must be <= 1 for relative offer types because "
            "cj_fee_relative * (1 - cjfee_factor) must not be negative, "
            f"got {randomization_factor}"
        )
    maximum_fee = fee * (Decimal(1) + factor)
    if maximum_fee >= 1:
        raise ValueError(
            "cj_fee_relative * (1 + cjfee_factor) must be < 1 for relative "
            f"offer types, got {value} and {randomization_factor}"
        )


class OfferConfig(BaseModel):
    """
    Configuration for a single offer.

    This model represents an individual offer that the maker will advertise.
    Multiple OfferConfigs can be used to create multiple offers simultaneously
    (e.g., one relative and one absolute fee offer).

    The offer_id is assigned automatically based on position in the list.
    """

    offer_type: OfferType = Field(
        default=OfferType.SW0_RELATIVE,
        description="Offer type (sw0reloffer for relative, sw0absoffer for absolute)",
    )
    min_size: int = Field(
        default=100_000,
        ge=0,
        description=(
            "Minimum CoinJoin amount in satoshis. "
            "Default 100_000 matches the upstream JoinMarket reference."
        ),
    )
    cj_fee_relative: str = Field(
        default="0.0001",
        description=(
            "Relative CJ fee as decimal. Default 0.0001 (0.01%) is a public "
            "fee-quantization quantum."
        ),
    )
    cj_fee_absolute: int = Field(
        default=500,
        ge=0,
        description="Absolute CJ fee in satoshis. Used when offer_type is absolute.",
    )
    tx_fee_contribution: int = Field(
        default=0,
        ge=0,
        description="Transaction fee contribution in satoshis",
    )
    cjfee_factor: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Randomization factor applied to the CoinJoin fee on each offer "
            "announcement. The advertised fee is sampled uniformly from "
            "[cjfee*(1-f), cjfee*(1+f)]. Default 0 (no randomization) keeps a "
            "default maker exactly on its quantization quantum so it blends with "
            "other default makers. Only enable randomization (e.g. 0.1) when you "
            "use a non-quantized fee, where an exact value would otherwise be a "
            "fingerprint. The upstream JoinMarket yg-privacyenhanced default is 0.1."
        ),
    )
    txfee_contribution_factor: float = Field(
        default=0.3,
        ge=0.0,
        description=(
            "Randomization factor applied to tx_fee_contribution on each offer "
            "announcement. Set to 0 to disable. Default 0.3 matches the "
            "upstream JoinMarket reference."
        ),
    )
    size_factor: float = Field(
        default=0.1,
        ge=0.0,
        description=(
            "Randomization factor applied to minsize and maxsize on each offer "
            "announcement. Set to 0 to disable. Default 0.1 matches the "
            "upstream JoinMarket reference."
        ),
    )

    @field_validator("cj_fee_relative", mode="before")
    @classmethod
    def normalize_cj_fee_relative(cls, v: str | float | int) -> str:
        """Normalize cj_fee_relative to avoid scientific notation."""
        return normalize_decimal_string(v)

    @model_validator(mode="after")
    def validate_fee_config(self) -> OfferConfig:
        """Validate fee configuration based on offer type."""
        if self.offer_type in (OfferType.SWA_RELATIVE, OfferType.SWA_ABSOLUTE):
            raise ValueError(
                "Wrapped SegWit maker offers are not supported by the P2WPKH wallet signer"
            )
        if self.offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE):
            validate_relative_cj_fee(self.cj_fee_relative, self.cjfee_factor)
        return self

    def get_cjfee(self) -> str | int:
        """Get the appropriate cjfee value based on offer type."""
        if self.offer_type in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE):
            return self.cj_fee_absolute
        return self.cj_fee_relative

    model_config = {"frozen": False}


class MergeAlgorithm(StrEnum):
    """
    UTXO selection algorithm for makers.

    Determines how many UTXOs to use when participating in a CoinJoin.
    Since takers pay all tx fees, makers can add extra inputs "for free"
    which helps consolidate UTXOs and improves taker privacy.

    - default: Select minimum UTXOs needed (frugal)
    - gradual: Select 1 additional UTXO beyond minimum
    - greedy: Select ALL UTXOs from the mixdepth (max consolidation)
    - random: Select between 0-2 additional UTXOs randomly

    Reference: joinmarket-clientserver policy.py merge_algorithm
    """

    DEFAULT = "default"
    GRADUAL = "gradual"
    GREEDY = "greedy"
    RANDOM = "random"


class MakerConfig(WalletConfig):
    """
    Configuration for maker bot.

    Inherits base wallet configuration from jmcore.config.WalletConfig
    and adds maker-specific settings for offers, hidden services, and
    UTXO selection.

    Offer Configuration:
    - Simple single-offer: use offer_type, min_size, cj_fee_relative/absolute, tx_fee_contribution
    - Multi-offer setup: use offer_configs list (overrides single-offer fields when non-empty)

    The multi-offer system allows running both relative and absolute fee offers simultaneously,
    each with a unique offer ID. This is extensible to support N offers in the future.
    """

    # Hidden service configuration for direct peer connections
    max_fee_rate_sat_vb: float = Field(
        default=1_000.0,
        gt=0.0,
        allow_inf_nan=False,
        description="Safety cap for the resolved CoinJoin miner fee floor in sat/vB",
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

    # If onion_host is set, maker will serve on a hidden service
    # If tor_control is enabled and onion_host is None, it will be auto-generated
    onion_host: str | None = Field(
        default=None, description="Hidden service address (e.g., 'mymaker...onion')"
    )
    onion_serving_host: str = Field(
        default="127.0.0.1", description="Local bind address for incoming connections"
    )
    onion_serving_port: int = Field(
        default=5222, ge=0, le=65535, description="Default JoinMarket port (0 = auto-assign)"
    )
    tor_target_host: str = Field(
        default="127.0.0.1",
        description="Target host for Tor hidden service (use service name in Docker Compose)",
    )

    # Tor control port configuration for dynamic hidden service creation
    tor_control: TorControlConfig = Field(
        default_factory=create_tor_control_config_from_env,
        description="Tor control port configuration",
    )

    # Tor hidden service DoS defense configuration
    # These settings are applied at the Tor level for protection before traffic reaches the app
    hidden_service_dos: HiddenServiceDoSConfig = Field(
        default_factory=HiddenServiceDoSConfig,
        description=(
            "Tor-level DoS defense for the hidden service. "
            "Includes intro point rate limiting and optional Proof-of-Work. "
            "See https://community.torproject.org/onion-services/advanced/dos/"
        ),
    )

    # Multi-offer configuration (takes precedence over single-offer fields when non-empty)
    # Each OfferConfig gets a unique offer_id (0, 1, 2, ...) based on position
    offer_configs: list[OfferConfig] = Field(
        default_factory=list,
        description=(
            "List of offer configurations. When non-empty, overrides single-offer fields. "
            "Allows running multiple offers (e.g., relative + absolute) simultaneously."
        ),
    )

    # Single offer configuration (legacy, used when offer_configs is empty)
    offer_type: OfferType = Field(
        default=OfferType.SW0_RELATIVE, description="Offer type (relative/absolute fee)"
    )
    min_size: int = Field(default=100_000, ge=0, description="Minimum CoinJoin amount in satoshis")
    cj_fee_relative: str = Field(
        default="0.0001",
        description=(
            "Relative CJ fee. Default 0.0001 (0.01%) is a public fee-quantization "
            "quantum. See OfferConfig.cj_fee_relative."
        ),
    )
    cj_fee_absolute: int = Field(default=500, ge=0, description="Absolute CJ fee in satoshis")
    tx_fee_contribution: int = Field(
        default=0, ge=0, description="Transaction fee contribution in satoshis"
    )
    cjfee_factor: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Randomization factor for the CoinJoin fee in legacy single-offer mode. "
            "Default 0 keeps a default maker exactly on its quantization quantum. "
            "See OfferConfig.cjfee_factor."
        ),
    )
    txfee_contribution_factor: float = Field(
        default=0.3,
        ge=0.0,
        description=(
            "Randomization factor for the tx fee contribution in legacy "
            "single-offer mode. See OfferConfig.txfee_contribution_factor."
        ),
    )
    size_factor: float = Field(
        default=0.1,
        ge=0.0,
        description=(
            "Randomization factor for minsize/maxsize in legacy single-offer mode. "
            "See OfferConfig.size_factor."
        ),
    )

    # Base-protocol takers reject unconfirmed maker inputs. PoDLE commitments
    # independently require taker_utxo_age confirmations on a taker UTXO.
    min_confirmations: int = Field(default=1, ge=1, description="Minimum confirmations for UTXOs")

    # Fidelity bond configuration
    # List of locktimes (Unix timestamps) to scan for fidelity bonds
    # These should match locktimes used when creating bond UTXOs
    fidelity_bond_locktimes: list[int] = Field(
        default_factory=list, description="List of locktimes to scan for fidelity bonds"
    )

    # Manual fidelity bond specification (bypasses registry)
    # Use this when you don't have a registry or want to specify a bond directly
    fidelity_bond_index: int | None = Field(
        default=None, description="Fidelity bond derivation index (bypasses registry)"
    )

    # Selected fidelity bond (txid, vout) - if not set, largest bond is used automatically
    selected_fidelity_bond: tuple[str, int] | None = Field(
        default=None, description="Selected fidelity bond UTXO (txid, vout)"
    )

    # Explicitly disable fidelity bonds - skips registry lookup and bond proof generation
    # even when bonds exist in the registry
    no_fidelity_bond: bool = Field(
        default=False, description="Disable fidelity bond usage (run without bond proof)"
    )

    # Timeouts
    session_timeout_sec: int = Field(
        default=300,
        ge=60,
        le=86_400,
        description="Maximum time for a CoinJoin session to complete (all states)",
    )
    pre_sign_timeout_sec: int = Field(
        default=180,
        ge=60,
        le=3_600,
        description=(
            "Maximum time to wait for the taker's transaction after maker inputs "
            "have been reserved and disclosed"
        ),
    )
    identity_renewal_min_sec: int = Field(
        default=43_200,
        ge=60,
        description="Minimum randomized maker identity renewal interval in seconds",
    )
    identity_renewal_max_sec: int = Field(
        default=86_400,
        ge=60,
        description="Maximum randomized maker identity renewal interval in seconds",
    )
    identity_grace_sec: int = Field(
        default=300,
        ge=60,
        description="Fixed grace period for continuations on a retired maker identity",
    )
    identity_rotation_quiet_min_sec: int = Field(
        default=60,
        ge=0,
        description="Minimum quiet interval between old and replacement directory identities",
    )
    identity_rotation_quiet_max_sec: int = Field(
        default=600,
        ge=0,
        description="Maximum quiet interval between old and replacement directory identities",
    )

    # Pending transaction timeout
    pending_tx_timeout_min: int = Field(
        default=60,
        ge=10,
        le=1440,
        description=(
            "Minutes to monitor an attempt without a recorded transaction ID. "
            "Also sets the maker input reservation lifetime. Expiration stops local "
            "monitoring, not transaction validity."
        ),
    )
    pending_tx_abandon_hours: int = Field(
        default=72,
        ge=1,
        le=8760,
        description=(
            "Hours to monitor a recorded transaction for confirmation before a local "
            "monitoring timeout. Explicit wallet refresh can still reconcile later "
            "confirmation. Default 72 h."
        ),
    )

    # Wallet rescan configuration
    post_coinjoin_rescan_delay: int = Field(
        default=60,
        ge=5,
        description="Seconds to wait before rescanning wallet after CoinJoin completion",
    )
    rescan_interval_sec: int = Field(
        default=600,
        ge=60,
        description="Interval in seconds for periodic wallet rescans (default: 10 minutes)",
    )

    offer_reannounce_delay_max: int = Field(
        default=600,
        ge=0,
        description=(
            "Maximum random delay in seconds before re-announcing offers after "
            "a balance change (0 = immediate, default: 600s = 10 minutes)"
        ),
    )

    # Mixdepth 0 privacy restriction
    allow_mixdepth_zero_merge: bool = Field(
        default=False,
        description=(
            "When False (default), mixdepth 0 UTXOs are restricted to prevent "
            "linking deposits/fidelity bonds via UTXO merging. Exact CoinJoin outputs "
            "and recursively proven CoinJoin-only change remain usable as maker "
            "rotation liquidity. Set to True to disable the restriction entirely "
            "(experienced makers only, reduces privacy)."
        ),
    )

    # UTXO merge algorithm - how many UTXOs to use
    merge_algorithm: MergeAlgorithm = Field(
        default=MergeAlgorithm.DEFAULT,
        description=(
            "UTXO selection strategy: default (minimum), gradual (+1), "
            "greedy (all), random (0-2 extra)"
        ),
    )

    mixdepth_selection_policy: MixdepthSelectionPolicy = Field(
        default=MixdepthSelectionPolicy.BALANCED,
        description=(
            "Source mixdepth policy: balanced (largest eligible balance) or "
            "concentrated (legacy cyclic-gap liquidity heuristic)"
        ),
    )

    # Generic message rate limiting (protects against spam/DoS)
    message_rate_limit: int = Field(
        default=10,
        ge=1,
        description="Maximum messages per second per peer (sustained)",
    )
    message_burst_limit: int = Field(
        default=100,
        ge=1,
        description="Maximum burst messages per peer (default: 100, allows ~10s at max rate)",
    )

    # Rate limiting for orderbook requests (protects against spam attacks)
    orderbook_rate_limit: int = Field(
        default=1,
        ge=1,
        description="Maximum orderbook responses per peer per interval",
    )
    orderbook_rate_interval: float = Field(
        default=10.0,
        ge=1.0,
        description="Interval in seconds for orderbook rate limiting (default: 10s)",
    )
    orderbook_violation_ban_threshold: int = Field(
        default=100,
        ge=1,
        description="Ban peer after this many rate limit violations",
    )
    orderbook_violation_warning_threshold: int = Field(
        default=10,
        ge=1,
        description="Start exponential backoff after this many violations",
    )
    orderbook_violation_severe_threshold: int = Field(
        default=50,
        ge=1,
        description="Severe backoff threshold (higher penalty)",
    )
    orderbook_ban_duration: float = Field(
        default=3600.0,
        ge=60.0,
        description="Ban duration in seconds (default: 1 hour)",
    )

    # Directory reconnection configuration
    directory_reconnect_interval: int = Field(
        default=300,
        ge=60,
        description="Interval between reconnection attempts for failed directories (5 min)",
    )
    directory_reconnect_max_retries: int = Field(
        default=0,
        ge=0,
        description="Maximum reconnection attempts per directory (0 = unlimited)",
    )
    directory_startup_timeout: int = Field(
        default=120,
        ge=10,
        description=(
            "Seconds to keep retrying directory connections at startup before giving up "
            "and letting the background reconnect task take over (default: 120s)"
        ),
    )

    model_config = {"frozen": False}

    @field_validator("cj_fee_relative", mode="before")
    @classmethod
    def normalize_cj_fee_relative(cls, v: str | float | int) -> str:
        """Normalize cj_fee_relative to avoid scientific notation."""
        return normalize_decimal_string(v)

    @model_validator(mode="after")
    def validate_config(self) -> MakerConfig:
        """Validate configuration after initialization."""
        # Set bitcoin_network default (handled by parent WalletConfig)
        if self.bitcoin_network is None:
            object.__setattr__(self, "bitcoin_network", self.network)
        if self.min_fee_rate_sat_vb > self.max_fee_rate_sat_vb:
            raise ValueError("min_fee_rate_sat_vb must not exceed max_fee_rate_sat_vb")
        if self.identity_renewal_min_sec > self.identity_renewal_max_sec:
            raise ValueError("identity_renewal_min_sec must not exceed identity_renewal_max_sec")
        if self.identity_rotation_quiet_min_sec > self.identity_rotation_quiet_max_sec:
            raise ValueError(
                "identity_rotation_quiet_min_sec must not exceed identity_rotation_quiet_max_sec"
            )

        # Only validate single-offer fields if offer_configs is empty
        # (when offer_configs is set, those fields are ignored)
        if not self.offer_configs:
            if self.offer_type in (OfferType.SWA_RELATIVE, OfferType.SWA_ABSOLUTE):
                raise ValueError(
                    "Wrapped SegWit maker offers are not supported by the P2WPKH wallet signer"
                )
            # Validate cj_fee_relative for relative offer types
            if self.offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE):
                validate_relative_cj_fee(self.cj_fee_relative, self.cjfee_factor)

        return self

    def get_effective_offer_configs(self) -> list[OfferConfig]:
        """
        Get the effective list of offer configurations.

        If offer_configs is set (non-empty), returns it directly.
        Otherwise, creates a single OfferConfig from the legacy single-offer fields.

        This provides backward compatibility while supporting the new multi-offer system.

        Returns:
            List of OfferConfig objects to use for creating offers.
        """
        if self.offer_configs:
            return self.offer_configs

        # Create single OfferConfig from legacy fields
        return [
            OfferConfig(
                offer_type=self.offer_type,
                min_size=self.min_size,
                cj_fee_relative=self.cj_fee_relative,
                cj_fee_absolute=self.cj_fee_absolute,
                tx_fee_contribution=self.tx_fee_contribution,
                cjfee_factor=self.cjfee_factor,
                txfee_contribution_factor=self.txfee_contribution_factor,
                size_factor=self.size_factor,
            )
        ]
