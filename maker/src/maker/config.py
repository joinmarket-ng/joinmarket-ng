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
        default="0.00002",
        description=(
            "Relative CJ fee as decimal. Default 0.00002 (0.002%) matches the "
            "upstream JoinMarket reference and avoids fingerprinting jm-ng "
            "makers via different fee defaults."
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
        default=0.1,
        ge=0.0,
        description=(
            "Randomization factor applied to the CoinJoin fee on each offer "
            "announcement. The advertised fee is sampled uniformly from "
            "[cjfee*(1-f), cjfee*(1+f)]. Set to 0 to disable. "
            "Default 0.1 matches the upstream JoinMarket yg-privacyenhanced."
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
        if self.offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE):
            try:
                cj_fee_float = float(self.cj_fee_relative)
                if cj_fee_float <= 0:
                    raise ValueError(
                        f"cj_fee_relative must be > 0 for relative offer types, "
                        f"got {self.cj_fee_relative}"
                    )
            except ValueError as e:
                if "could not convert" in str(e):
                    raise ValueError(
                        f"cj_fee_relative must be a valid number, got {self.cj_fee_relative}"
                    ) from e
                raise
        return self

    def get_cjfee(self) -> str | int:
        """Get the appropriate cjfee value based on offer type."""
        if self.offer_type in (OfferType.SW0_ABSOLUTE, OfferType.SWA_ABSOLUTE):
            return self.cj_fee_absolute
        return self.cj_fee_relative

    model_config = {"frozen": False}


class SwapProviderConfig(BaseModel):
    """Runtime configuration for the maker's bidirectional swap-provider role.

    Mirrors :class:`jmcore.settings.SwapProviderSettings`; the runtime builds a
    :class:`jmswap.ProviderTerms` and a :class:`jmswap.LdkServerBackend` from it
    when ``enabled`` is set.
    """

    enabled: bool = False
    directions: list[str] = Field(default_factory=lambda: ["swapin", "swapout"])
    nostr_relays: list[str] = Field(default_factory=lambda: ["ws://localhost:7000"])
    min_amount: int = Field(default=50_000, ge=0)
    max_amount: int = Field(default=25_000_000, ge=0)
    percentage_fee: str = Field(default="0.1")
    base_fee: int = Field(default=500, ge=0)
    cltv_delta: int = Field(default=144, ge=1)
    min_confirmations: int = Field(default=1, ge=1)
    bond_value: int = Field(default=0, ge=0)
    accepts_whole_coin: bool = False
    channel_change: bool = False
    channel_peer: str | None = None
    ldk_grpc_url: str | None = None
    ldk_tls_cert: str | None = None
    ldk_api_key: str | None = None
    ldk_api_key_path: str | None = None
    # LND backend (taproot channel-as-change). When lnd_grpc_url is set it is preferred
    # over LDK for channel funding, giving a real simple-taproot (P2TR) channel.
    lnd_grpc_url: str | None = None
    lnd_tls_cert_path: str | None = None
    lnd_macaroon_path: str | None = None

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
    taker_utxo_age: int = Field(
        default=5,
        ge=1,
        description="Minimum confirmations required on the taker's PoDLE commitment UTXO.",
    )
    cj_fee_relative: str = Field(
        default="0.00002",
        description=(
            "Relative CJ fee. Default 0.00002 (0.002%) matches the upstream JoinMarket reference."
        ),
    )
    cj_fee_absolute: int = Field(default=500, ge=0, description="Absolute CJ fee in satoshis")
    tx_fee_contribution: int = Field(
        default=0, ge=0, description="Transaction fee contribution in satoshis"
    )
    cjfee_factor: float = Field(
        default=0.1,
        ge=0.0,
        description=(
            "Randomization factor for the CoinJoin fee in legacy single-offer mode. "
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

    # Minimum confirmations for UTXOs offered into coinjoins. Default 0
    # lets makers offer unconfirmed UTXOs; PoDLE commitments still require
    # taker_utxo_age confirmations on a separate UTXO.
    min_confirmations: int = Field(default=0, ge=0, description="Minimum confirmations for UTXOs")

    # Knapsack channel-as-change capability (taker-arranged, maker-to-maker). When
    # enabled and an LN node is configured, the maker advertises that the taker may
    # pair it with a fellow maker into grid-quantized shared funding outputs.
    knapsack_capable: bool = Field(
        default=False,
        description=(
            "Advertise that this maker can take part in taker-arranged knapsack "
            "channel-as-change (maker-to-maker shared outputs). Requires ln_node."
        ),
    )
    allow_legacy_channel_change: bool = Field(
        default=False,
        description=(
            "Explicitly acknowledge legacy channel-as-change constructions that do not "
            "provide confirmation-gated channel-input escrow recovery."
        ),
    )
    ln_node: str | None = Field(
        default=None,
        description="The maker's Lightning node 'node_id@host:port' for channel-as-change.",
    )

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
        description="Maximum time for a CoinJoin session to complete (all states)",
    )

    # Pending transaction timeout
    pending_tx_timeout_min: int = Field(
        default=60,
        ge=10,
        le=1440,
        description=(
            "Minutes to wait for a pending CoinJoin transaction to appear on-chain "
            "before marking it as failed. If the taker doesn't broadcast the transaction "
            "within this time, we assume it was abandoned."
        ),
    )
    pending_tx_abandon_hours: int = Field(
        default=72,
        ge=1,
        le=8760,
        description=(
            "Hours after which a broadcast but unconfirmed transaction is "
            "marked as abandoned and removed from the pending-monitoring list. "
            "Bitcoin transactions not confirmed within ~3 days are typically "
            "dropped from mempools."
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
            "linking deposits/fidelity bonds via UTXO merging. CoinJoin outputs "
            "(label 'cj-out') are always exempt from this restriction since they "
            "already have CoinJoin privacy. Set to True to disable the restriction "
            "entirely (experienced makers only)."
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

    # Bidirectional Lightning swap-provider role (see jmswap).
    swap_provider: SwapProviderConfig = Field(
        default_factory=lambda: SwapProviderConfig(),
        description="Runtime configuration for the maker's swap-provider role.",
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

        if (
            self.knapsack_capable or self.swap_provider.channel_change
        ) and not self.allow_legacy_channel_change:
            raise ValueError(
                "legacy channel-as-change requires allow_legacy_channel_change=true; "
                "current backends do not implement confirmation-gated channel-input escrow"
            )

        # Only validate single-offer fields if offer_configs is empty
        # (when offer_configs is set, those fields are ignored)
        if not self.offer_configs:
            # Validate cj_fee_relative for relative offer types
            if self.offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE):
                try:
                    cj_fee_float = float(self.cj_fee_relative)
                    if cj_fee_float <= 0:
                        raise ValueError(
                            f"cj_fee_relative must be > 0 for relative offer types, "
                            f"got {self.cj_fee_relative}"
                        )
                except ValueError as e:
                    if "could not convert" in str(e):
                        raise ValueError(
                            f"cj_fee_relative must be a valid number, got {self.cj_fee_relative}"
                        ) from e
                    raise

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

    def capability_features(self) -> set[str]:
        """Capability flags this maker advertises in its handshake feature map.

        Maps the three deployable maker-clustering fixes to handshake features so a
        taker can pick makers by role from the orderbook:

        * ``swap_provider`` when the bidirectional swap-provider role is enabled;
        * ``whole_coin`` when the provider also accepts a maker's whole coin;
        * ``channel_change`` when knapsack channel-as-change is enabled with an
          ``ln_node`` (an external-peer channel is never used, so a node is required).
        """
        from jmcore.protocol import (
            FEATURE_CHANNEL_CHANGE,
            FEATURE_SWAP_PROVIDER,
            FEATURE_WHOLE_COIN,
        )

        features: set[str] = set()
        sp = self.swap_provider
        if sp is not None and sp.enabled:
            features.add(FEATURE_SWAP_PROVIDER)
            if sp.accepts_whole_coin:
                features.add(FEATURE_WHOLE_COIN)
        if self.knapsack_capable and self.ln_node:
            features.add(FEATURE_CHANNEL_CHANGE)
        return features
