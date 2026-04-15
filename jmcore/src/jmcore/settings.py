"""
Unified settings management for JoinMarket components.

This module provides a centralized configuration system using pydantic-settings
that supports:
1. TOML configuration file (~/.joinmarket-ng/config.toml)
2. Environment variables
3. CLI arguments (via typer, handled by components)

Priority (highest to lowest):
1. CLI arguments
2. Environment variables
3. Config file
4. Default values

The config file is auto-generated on first run with all settings commented out,
allowing users to selectively override only the settings they want to change.
This approach facilitates software updates since unchanged defaults can be
updated without user intervention.

Usage:
    from jmcore.settings import get_settings, JoinMarketSettings

    # Get settings (loads from all sources with proper priority)
    settings = get_settings()

    # Access common settings
    print(settings.tor.socks_host)
    print(settings.bitcoin.rpc_url)

Environment Variable Naming:
    - Use uppercase with double underscore for nested settings
    - Examples: TOR__SOCKS_HOST, BITCOIN__RPC_URL, MAKER__MIN_SIZE
    - Maps to TOML sections: TOR__SOCKS_HOST -> [tor] socks_host
"""

from __future__ import annotations

import importlib.resources
import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, ClassVar, Self

from loguru import logger
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from jmcore.constants import DUST_THRESHOLD
from jmcore.models import NetworkType
from jmcore.paths import get_default_data_dir

# Default directory servers per network
DEFAULT_DIRECTORY_SERVERS: dict[str, list[str]] = {
    "mainnet": [
        "satoshi2vcg5e2ept7tjkzlkpomkobqmgtsjzegg6wipnoajadissead.onion:5222",
        "coinjointovy3eq5fjygdwpkbcdx63d7vd4g32mw7y553uj3kjjzkiqd.onion:5222",
        "nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion:5222",
        "odpwaf67rs5226uabcamvypg3y4bngzmfk7255flcdodesqhsvkptaid.onion:5222",
        "jmarketxf5wc4aldf3slm5u6726zsky52bqnfv6qyxe5hnafgly6yuyd.onion:5222",
        "jmrust7bgdbdl6skkvuzhqost4jkikrluj6alemspeifm5hvgqz2qaad.onion:5222",
    ],
    "signet": [
        "signetvaxgd3ivj4tml4g6ed3samaa2rscre2gyeyohncmwk4fbesiqd.onion:5222",
        "u5oj5etqex3vh7jagljf3e2lo4awmmtcw3klbrlt2fonzyozpn5txrqd.onion:5222",
    ],
    "testnet": [],
    "regtest": [],
}


class TorSettings(BaseModel):
    """Tor proxy and control port configuration."""

    # SOCKS proxy settings
    socks_host: str = Field(
        default="127.0.0.1",
        description="Tor SOCKS5 proxy host",
    )
    socks_port: int = Field(
        default=9050,
        ge=1,
        le=65535,
        description="Tor SOCKS5 proxy port",
    )

    # Control port settings
    control_enabled: bool = Field(
        default=True,
        description="Enable Tor control port integration for ephemeral hidden services",
    )
    control_host: str = Field(
        default="127.0.0.1",
        description="Tor control port host",
    )
    control_port: int = Field(
        default=9051,
        ge=1,
        le=65535,
        description="Tor control port",
    )
    cookie_path: str | None = Field(
        default=None,
        description="Path to Tor cookie auth file",
    )
    password: SecretStr | None = Field(
        default=None,
        description="Tor control port password (use cookie auth instead if possible)",
    )

    # Hidden service target (for makers)
    target_host: str = Field(
        default="127.0.0.1",
        description="Target host for Tor hidden service (usually container name in Docker)",
    )

    # Stream isolation
    stream_isolation: bool = Field(
        default=True,
        description=(
            "Use SOCKS5 authentication to isolate different connection types "
            "onto separate Tor circuits.  This prevents traffic correlation "
            "between e.g. directory connections, peer connections, and "
            "notification traffic.  Requires IsolateSOCKSAuth on the Tor "
            "SocksPort (enabled by default)."
        ),
    )

    # Connection timeout
    connection_timeout: float = Field(
        default=120.0,
        gt=0.0,
        description=(
            "Timeout in seconds for Tor SOCKS5 connections. Covers TCP handshake, "
            "SOCKS5 negotiation, Tor circuit building, and PoW solving. "
            "Default 120s matches Tor's internal circuit timeout."
        ),
    )


class BitcoinSettings(BaseModel):
    """Bitcoin backend configuration."""

    backend_type: str = Field(
        default="descriptor_wallet",
        description="Backend type: scantxoutset, descriptor_wallet, or neutrino",
    )
    rpc_url: str = Field(
        default="http://127.0.0.1:8332",
        description="Bitcoin Core RPC URL",
    )
    rpc_user: str = Field(
        default="",
        description="Bitcoin Core RPC username",
    )
    rpc_password: SecretStr = Field(
        default=SecretStr(""),
        description="Bitcoin Core RPC password",
    )
    rpc_cookie_file: str | None = Field(
        default=None,
        description=(
            "Path to Bitcoin Core .cookie file for cookie-based RPC authentication. "
            "When set, the cookie file is read at startup and rpc_user/rpc_password "
            "are populated automatically. This is mutually exclusive with setting "
            "rpc_user/rpc_password manually. The cookie file is typically located "
            "at ~/.bitcoin/.cookie (mainnet) or ~/.bitcoin/regtest/.cookie (regtest)."
        ),
    )
    descriptor_wallet_name: str = Field(
        default="jm_descriptor_wallet",
        description="Name of the descriptor wallet to use in Bitcoin Core",
    )
    neutrino_url: str = Field(
        default="http://127.0.0.1:8334",
        description="Neutrino REST API URL (for neutrino backend)",
    )
    neutrino_add_peers: list[str] = Field(
        default_factory=list,
        description=(
            "Preferred peer addresses for neutrino (host:port) while still allowing "
            "DNS/discovery peers. Only takes effect when JoinMarket manages the "
            "neutrino process (e.g., flatpak deployment). When neutrino-api runs as "
            "a standalone service, configure peers directly via its ADD_PEERS env var "
            "or --addpeer flag."
        ),
    )
    neutrino_clearnet_initial_sync: bool = Field(
        default=True,
        description=(
            "Sync block headers over clearnet before switching to Tor. "
            "Headers are public deterministic data identical for all nodes, "
            "so downloading them over clearnet does not reveal watched addresses. "
            "Typically around 2x faster than doing the full initial header sync via Tor."
        ),
    )
    neutrino_prefetch_filters: bool = Field(
        default=True,
        description=(
            "Enable background prefetch of compact block filters. "
            "Enabled by default because jm-wallet info scans these filters anyway, "
            "so prefetching saves time on the initial scan. With the default "
            "lookback of ~2 years, this takes ~3 hours on clearnet and ~3GB disk "
            "on mainnet. Disable to fetch filters on-demand only. "
            "When false, neutrino_prefetch_lookback_blocks is ignored."
        ),
    )
    neutrino_prefetch_lookback_blocks: int = Field(
        default=105120,
        description=(
            "When neutrino_prefetch_filters is true, only prefetch filters "
            "for this many recent blocks (~2 years at 105120 blocks). "
            "Set to 0 to prefetch all filters from genesis. Ignored when "
            "neutrino_prefetch_filters is false."
        ),
    )
    neutrino_scan_lookback_blocks: int = Field(
        default=105120,
        description=(
            "Number of blocks to look back from tip for wallet rescans when "
            "no explicit scan_start_height is set. Default ~2 years (105120 blocks)."
        ),
    )
    neutrino_tls_cert: str | None = Field(
        default=None,
        description=(
            "Path to neutrino-api TLS certificate for HTTPS verification. "
            "When set, the neutrino backend connects over HTTPS and pins "
            "the server certificate to this file (trust-on-first-use). "
            "Generated automatically by neutrino-api on first start."
        ),
    )
    neutrino_auth_token: str | None = Field(
        default=None,
        description=(
            "API bearer token for neutrino-api authentication. "
            "Sent as 'Authorization: Bearer <token>' on every request. "
            "Generated automatically by neutrino-api on first start "
            "and stored in its data directory as 'auth_token'."
        ),
    )
    neutrino_auth_token_file: str | None = Field(
        default=None,
        description=(
            "Path to a file containing the neutrino-api auth token. "
            "If set and neutrino_auth_token is not provided, the token "
            "is read from this file at startup. Useful in Docker "
            "environments where the token is generated by neutrino-api "
            "into a shared volume."
        ),
    )

    @model_validator(mode="after")
    def _load_rpc_cookie_from_file(self) -> Self:
        """Read rpc_user/rpc_password from Bitcoin Core .cookie file.

        Bitcoin Core writes a ``.cookie`` file containing
        ``__cookie__:<random_hex>`` in its data directory.  When
        ``rpc_cookie_file`` is set and ``rpc_user`` has not been
        explicitly provided, the cookie file is parsed and the
        credentials are populated automatically.
        """
        if self.rpc_cookie_file is not None:
            cookie_path = Path(self.rpc_cookie_file).expanduser()
            # Only override if user hasn't explicitly set credentials
            if self.rpc_user == "" and self.rpc_password.get_secret_value() == "":
                if cookie_path.is_file():
                    content = cookie_path.read_text().strip()
                    if ":" in content:
                        user, password = content.split(":", 1)
                        self.rpc_user = user
                        self.rpc_password = SecretStr(password)
                    else:
                        logger.warning(
                            f"Cookie file {cookie_path} has unexpected format "
                            "(expected 'user:password')"
                        )
                else:
                    logger.warning(f"Cookie file not found: {cookie_path}")
            else:
                logger.warning(
                    "Both rpc_cookie_file and rpc_user/rpc_password are set; "
                    "ignoring rpc_cookie_file in favor of explicit credentials"
                )
        return self

    @model_validator(mode="after")
    def _load_auth_token_from_file(self) -> Self:
        """Read neutrino_auth_token from file when neutrino_auth_token_file is set."""
        if self.neutrino_auth_token is None and self.neutrino_auth_token_file is not None:
            token_path = Path(self.neutrino_auth_token_file).expanduser()
            if token_path.is_file():
                self.neutrino_auth_token = token_path.read_text().strip()
        return self


class NetworkSettings(BaseModel):
    """Network configuration."""

    network: NetworkType = Field(
        default=NetworkType.MAINNET,
        description="JoinMarket protocol network (mainnet, testnet, signet, regtest)",
    )
    bitcoin_network: NetworkType | None = Field(
        default=None,
        description="Bitcoin network for address generation (defaults to network)",
    )
    directory_servers: list[str] = Field(
        default_factory=list,
        description="Directory server addresses (host:port). Uses defaults if empty.",
    )

    @field_validator("directory_servers", mode="before")
    @classmethod
    def parse_directory_servers(cls, v: Any) -> Any:
        """Accept JSON arrays, comma-separated strings, or plain single values.

        pydantic-settings passes list[str] env vars through json.loads(), which
        requires JSON format. This validator also accepts plain comma-separated
        strings so that e.g. NETWORK_CONFIG__DIRECTORY_SERVERS=host:port works.
        """
        import json

        if isinstance(v, str):
            v = v.strip()
            # Try JSON first (handles '["a","b"]' or '"a"' forms)
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return [s.strip() for s in parsed if s.strip()]
                if isinstance(parsed, str):
                    return [parsed.strip()] if parsed.strip() else []
            except (json.JSONDecodeError, ValueError):
                pass
            # Fall back to comma-separated plain string
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


class WalletSettings(BaseModel):
    """Wallet configuration."""

    mixdepth_count: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Number of mixdepths (privacy compartments)",
    )
    gap_limit: int = Field(
        default=20,
        ge=6,
        description="BIP44 gap limit for address scanning",
    )
    dust_threshold: int = Field(
        default=27300,
        ge=0,
        description="Dust threshold in satoshis",
    )
    smart_scan: bool = Field(
        default=True,
        description="Use smart scan for fast startup",
    )
    background_full_rescan: bool = Field(
        default=True,
        description="Run full blockchain rescan in background",
    )
    scan_lookback_blocks: int = Field(
        default=52560,
        ge=0,
        description="Blocks to look back for smart scan (~1 year default)",
    )
    scan_start_height: int | None = Field(
        default=None,
        ge=0,
        description="Explicit start height for initial scan (overrides scan_lookback_blocks if set)",
    )
    default_fee_block_target: int = Field(
        default=3,
        ge=1,
        le=1008,
        description="Default block target for fee estimation in wallet transactions",
    )
    mnemonic_file: str | None = Field(
        default=None,
        description="Default path to mnemonic file",
    )
    mnemonic_password: SecretStr | None = Field(
        default=None,
        description="Password for encrypted mnemonic file",
    )
    bip39_passphrase: SecretStr | None = Field(
        default=None,
        description="BIP39 passphrase (13th/25th word). For security, prefer BIP39_PASSPHRASE env var.",
    )


class NotificationSettings(BaseModel):
    """Notification system configuration."""

    enabled: bool = Field(
        default=False,
        description="Enable notifications (requires urls to be set)",
    )
    urls: list[str] = Field(
        default_factory=list,
        description='Apprise notification URLs (e.g., ["tgram://bottoken/ChatID", "gotify://hostname/token"])',
    )
    title_prefix: str = Field(
        default="JoinMarket NG",
        description="Prefix for notification titles",
    )
    component_name: str = Field(
        default="",
        description="Component name in notification titles (e.g., 'Maker', 'Taker'). "
        "Usually set programmatically by each component.",
    )
    include_amounts: bool = Field(
        default=True,
        description="Include amounts in notifications",
    )
    include_txids: bool = Field(
        default=False,
        description="Include transaction IDs in notifications (privacy risk)",
    )
    include_nick: bool = Field(
        default=True,
        description="Include peer nicks in notifications",
    )
    use_tor: bool = Field(
        default=True,
        description="Route notifications through Tor SOCKS proxy",
    )
    # Event type toggles
    notify_fill: bool = Field(default=True, description="Notify on !fill requests")
    notify_rejection: bool = Field(default=True, description="Notify on rejections")
    notify_signing: bool = Field(default=True, description="Notify on transaction signing")
    notify_mempool: bool = Field(default=True, description="Notify on mempool detection")
    notify_confirmed: bool = Field(default=True, description="Notify on confirmation")
    notify_nick_change: bool = Field(default=True, description="Notify on nick change")
    notify_disconnect: bool = Field(
        default=False,
        description="Notify on individual directory server disconnect/reconnect (noisy)",
    )
    notify_all_disconnect: bool = Field(
        default=True,
        description="Notify when ALL directory servers are disconnected (critical)",
    )
    notify_coinjoin_start: bool = Field(default=True, description="Notify on CoinJoin start")
    notify_coinjoin_complete: bool = Field(default=True, description="Notify on CoinJoin complete")
    notify_coinjoin_failed: bool = Field(default=True, description="Notify on CoinJoin failure")
    notify_peer_events: bool = Field(default=False, description="Notify on peer connect/disconnect")
    notify_rate_limit: bool = Field(default=True, description="Notify on rate limit bans")
    notify_startup: bool = Field(default=True, description="Notify on component startup")
    notify_summary: bool = Field(
        default=True,
        description="Send periodic summary notifications with CoinJoin stats",
    )
    notify_summary_balance: bool = Field(
        default=False,
        description=(
            "Include total wallet balance and UTXO count in periodic summary notifications. "
            "Disabled by default for privacy."
        ),
    )
    summary_interval_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description=(
            "Interval in hours between summary notifications (1-168). "
            "Common values: 24 (daily), 168 (weekly)"
        ),
    )
    check_for_updates: bool = Field(
        default=False,
        description=(
            "Check GitHub for new releases and include version info in summary notifications. "
            "PRIVACY WARNING: This polls the GitHub API (api.github.com) each summary interval. "
            "The request is routed through Tor when use_tor is enabled, but GitHub will still "
            "see the Tor exit node IP. Opt-in only."
        ),
    )
    # Retry settings
    retry_enabled: bool = Field(
        default=True,
        description=(
            "Retry failed notifications in the background with exponential backoff. "
            "Recommended when routing through Tor where transient failures are common."
        ),
    )
    retry_max_attempts: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum number of retry attempts for a failed notification (1-10)",
    )
    retry_base_delay: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description=(
            "Base delay in seconds before the first retry (1-60). "
            "Subsequent retries double this delay (exponential backoff)."
        ),
    )


class MakerSettings(BaseModel):
    """Maker-specific settings."""

    min_size: int = Field(
        default=DUST_THRESHOLD,
        ge=0,
        description="Minimum CoinJoin amount in satoshis (default: dust threshold)",
    )
    offer_type: str = Field(
        default="sw0reloffer",
        description="Offer type: sw0reloffer (relative) or sw0absoffer (absolute)",
    )
    cj_fee_relative: str = Field(
        default="0.001",
        description="Relative CoinJoin fee (0.001 = 0.1%)",
    )
    cj_fee_absolute: int = Field(
        default=500,
        ge=0,
        description="Absolute CoinJoin fee in satoshis",
    )
    tx_fee_contribution: int = Field(
        default=0,
        ge=0,
        description="Transaction fee contribution in satoshis",
    )
    min_confirmations: int = Field(
        default=1,
        ge=0,
        description="Minimum confirmations for UTXOs",
    )
    allow_mixdepth_zero_merge: bool = Field(
        default=False,
        description=(
            "Disable the mixdepth 0 single-UTXO restriction "
            "(experienced makers only, reduces privacy)"
        ),
    )
    merge_algorithm: str = Field(
        default="default",
        description="UTXO selection: default, gradual, greedy, random",
    )
    session_timeout_sec: int = Field(
        default=300,
        ge=60,
        description="Maximum time for a CoinJoin session",
    )
    pending_tx_timeout_min: int = Field(
        default=60,
        ge=10,
        le=1440,
        description="Minutes before marking unbroadcast CoinJoins as failed",
    )
    rescan_interval_sec: int = Field(
        default=600,
        ge=60,
        description="Interval for periodic wallet rescans",
    )
    # Hidden service settings
    onion_serving_host: str = Field(
        default="127.0.0.1",
        description="Bind address for incoming connections",
    )
    onion_serving_port: int = Field(
        default=5222,
        ge=0,
        le=65535,
        description="Port for incoming onion connections",
    )
    # Rate limiting
    message_rate_limit: int = Field(
        default=10,
        ge=1,
        description="Messages per second per peer (sustained)",
    )
    message_burst_limit: int = Field(
        default=100,
        ge=1,
        description="Maximum burst messages per peer",
    )

    @field_validator("cj_fee_relative", mode="before")
    @classmethod
    def normalize_cj_fee_relative(cls, v: str | float | int) -> str:
        """
        Normalize cj_fee_relative to avoid scientific notation.

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


class TakerSettings(BaseModel):
    """Taker-specific settings."""

    counterparty_count: int = Field(
        default=10,
        ge=1,
        le=20,
        description="Number of makers to select for CoinJoin",
    )
    max_cj_fee_abs: int = Field(
        default=500,
        ge=0,
        description="Maximum absolute CoinJoin fee in satoshis",
    )
    max_cj_fee_rel: str = Field(
        default="0.001",
        description="Maximum relative CoinJoin fee (0.001 = 0.1%)",
    )

    @field_validator("max_cj_fee_rel", mode="before")
    @classmethod
    def normalize_max_cj_fee_rel(cls, v: str | float | int) -> str:
        """Normalize to avoid scientific notation (same as MakerSettings.cj_fee_relative)."""
        if isinstance(v, (int, float)):
            return format(Decimal(str(v)), "f")
        if isinstance(v, str) and "e" in v.lower():
            try:
                return format(Decimal(v), "f")
            except InvalidOperation:
                pass
        return v

    tx_fee_factor: float = Field(
        default=3.0,
        ge=1.0,
        description="Multiply estimated fee by this factor",
    )
    fee_block_target: int | None = Field(
        default=None,
        ge=1,
        le=1008,
        description="Target blocks for fee estimation",
    )
    bondless_makers_allowance: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of time to choose makers randomly",
    )
    bond_value_exponent: float = Field(
        default=1.3,
        gt=0.0,
        description="Exponent for fidelity bond value calculation",
    )
    bondless_require_zero_fee: bool = Field(
        default=True,
        description="Require zero absolute fee for bondless maker spots",
    )
    maker_timeout_sec: int = Field(
        default=60,
        ge=10,
        description="Timeout for maker responses",
    )
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
    tx_broadcast: str = Field(
        default="random-peer",
        description="Broadcast policy: self, random-peer, multiple-peers, not-self",
    )
    broadcast_peer_count: int = Field(
        default=3,
        ge=1,
        description="Number of peers for multiple-peers broadcast",
    )
    minimum_makers: int = Field(
        default=1,
        ge=1,
        description="Minimum number of makers required",
    )
    rescan_interval_sec: int = Field(
        default=600,
        ge=60,
        description="Interval for periodic wallet rescans",
    )


class DirectoryServerSettings(BaseModel):
    """Directory server specific settings."""

    host: str = Field(
        default="127.0.0.1",
        description="Host address to bind to",
    )
    port: int = Field(
        default=5222,
        ge=0,
        le=65535,
        description="Port to listen on (0 = let OS assign)",
    )
    max_peers: int = Field(
        default=10000,
        ge=1,
        description="Maximum number of connected peers",
    )
    max_message_size: int = Field(
        default=2097152,
        ge=1024,
        description="Maximum message size in bytes (2MB default)",
    )
    max_line_length: int = Field(
        default=65536,
        ge=1024,
        description="Maximum JSON-line message length (64KB default)",
    )
    max_json_nesting_depth: int = Field(
        default=10,
        ge=1,
        description="Maximum nesting depth for JSON parsing",
    )
    message_rate_limit: int = Field(
        default=500,
        ge=1,
        description="Messages per second (sustained)",
    )
    message_burst_limit: int = Field(
        default=1000,
        ge=1,
        description="Maximum burst size",
    )
    rate_limit_disconnect_threshold: int = Field(
        default=0,
        ge=0,
        description="Disconnect after N rate limit violations (0 = never disconnect)",
    )
    broadcast_batch_size: int = Field(
        default=50,
        ge=1,
        description="Batch size for concurrent broadcasts",
    )
    health_check_host: str = Field(
        default="127.0.0.1",
        description="Host for health check endpoint",
    )
    health_check_port: int = Field(
        default=8080,
        ge=0,
        le=65535,
        description="Port for health check endpoint (0 = let OS assign)",
    )
    motd: str = Field(
        default="JoinMarket NG Directory Server https://github.com/joinmarket-ng/joinmarket-ng/",
        description="Message of the day sent to clients",
    )
    heartbeat_sweep_interval: float = Field(
        default=60.0,
        gt=0.0,
        description="Seconds between heartbeat sweep cycles (default 60)",
    )
    heartbeat_idle_threshold: float = Field(
        default=600.0,
        gt=0.0,
        description="Seconds of inactivity before probing a peer (default 600 = 10 min)",
    )
    heartbeat_hard_evict: float = Field(
        default=1500.0,
        gt=0.0,
        description="Seconds of inactivity before unconditional eviction (default 1500 = 25 min)",
    )
    heartbeat_pong_wait: float = Field(
        default=30.0,
        gt=0.0,
        description="Seconds to wait for PONG after sending PING (default 30)",
    )


class OrderbookWatcherSettings(BaseModel):
    """Orderbook watcher specific settings."""

    http_host: str = Field(
        default="0.0.0.0",
        description="HTTP server bind address",
    )
    http_port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="HTTP server port",
    )
    update_interval: int = Field(
        default=60,
        ge=10,
        description="Update interval in seconds",
    )
    mempool_api_url: str = Field(
        default="",
        description="Mempool API URL for transaction lookups",
    )
    mempool_web_url: str | None = Field(
        default=None,
        description="Mempool web URL for human-readable links",
    )
    uptime_grace_period: int = Field(
        default=60,
        ge=0,
        description="Grace period before tracking uptime",
    )
    max_message_size: int = Field(
        default=2097152,
        ge=1024,
        description="Maximum message size in bytes (2MB default)",
    )
    connection_timeout: float = Field(
        default=120.0,
        gt=0.0,
        description=(
            "Timeout in seconds for Tor SOCKS5 connections. Covers TCP handshake, "
            "SOCKS5 negotiation, Tor circuit building, and PoW solving."
        ),
    )


class LoggingSettings(BaseModel):
    """Logging configuration."""

    level: str = Field(
        default="INFO",
        description="Log level: TRACE, DEBUG, INFO, WARNING, ERROR",
    )
    sensitive: bool = Field(
        default=False,
        description="Enable sensitive logging (mnemonics, keys)",
    )


class _CommaListEnvSettingsSource(EnvSettingsSource):
    """Custom env source that decodes list[str] fields from comma-separated strings.

    pydantic-settings' default EnvSettingsSource calls json.loads() on complex
    fields (including list[str]), which requires JSON-formatted values like
    '["a","b"]'. This subclass also accepts plain comma-separated values like
    "a,b" or a bare single value "a" for list[str] fields, making container
    environment variable configuration more ergonomic.
    """

    def decode_complex_value(self, field_name: str, field_info: Any, value: Any) -> Any:
        if isinstance(value, str) and self._is_list_of_str(field_info):
            value = value.strip()
            try:
                return json.loads(value)
            except (json.JSONDecodeError, ValueError):
                return [s.strip() for s in value.split(",") if s.strip()]
        return super().decode_complex_value(field_name, field_info, value)

    @staticmethod
    def _is_list_of_str(field_info: Any) -> bool:
        """Return True if the field is annotated as list[str] or list[str] | None."""
        import typing

        ann = getattr(field_info, "annotation", None)
        if ann is None:
            return False
        origin = getattr(ann, "__origin__", None)
        if origin is list:
            args: tuple[Any, ...] = getattr(ann, "__args__", ())
            return len(args) > 0 and args[0] is str
        # Handle Optional[list[str]] / list[str] | None
        if origin is typing.Union or str(origin) == "typing.Union":
            for arg in getattr(ann, "__args__", ()):
                if getattr(arg, "__origin__", None) is list:
                    inner = getattr(arg, "__args__", ())
                    if inner and inner[0] is str:
                        return True
        return False


class JoinMarketSettings(BaseSettings):
    """
    Main JoinMarket settings class.

    Loads configuration from multiple sources with the following priority:
    1. CLI arguments (not handled here, passed to component __init__)
    2. Environment variables
    3. TOML config file (~/.joinmarket-ng/config.toml)
    4. Default values
    """

    model_config = SettingsConfigDict(
        env_prefix="",  # No prefix by default, use env_nested_delimiter for nested
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",  # Ignore unknown fields (for forward compatibility)
    )

    # Marker for config file path discovery
    _config_file_path: ClassVar[Path | None] = None

    # Core settings
    data_dir: Path | None = Field(
        default=None,
        description="Data directory (defaults to ~/.joinmarket-ng)",
    )

    # Nested settings groups
    tor: TorSettings = Field(default_factory=TorSettings)
    bitcoin: BitcoinSettings = Field(default_factory=BitcoinSettings)
    network_config: NetworkSettings = Field(default_factory=NetworkSettings)
    wallet: WalletSettings = Field(default_factory=WalletSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # Component-specific settings
    maker: MakerSettings = Field(default_factory=MakerSettings)
    taker: TakerSettings = Field(default_factory=TakerSettings)
    directory_server: DirectoryServerSettings = Field(default_factory=DirectoryServerSettings)
    orderbook_watcher: OrderbookWatcherSettings = Field(default_factory=OrderbookWatcherSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        Customize settings sources and their priority.

        Priority (highest to lowest):
        1. init_settings (CLI arguments passed to constructor)
        2. env_settings (environment variables with __ delimiter)
        3. toml_settings (config.toml file)
        4. defaults (in field definitions)
        """
        toml_source = TomlConfigSettingsSource(settings_cls)
        comma_env = _CommaListEnvSettingsSource(settings_cls)
        return (
            init_settings,
            comma_env,
            toml_source,
        )

    def get_data_dir(self) -> Path:
        """Get the data directory, using default if not set."""
        if self.data_dir is not None:
            return self.data_dir
        return get_default_data_dir()

    def get_directory_servers(self) -> list[str]:
        """Get directory servers, using network defaults if not set."""
        if self.network_config.directory_servers:
            return self.network_config.directory_servers
        network_name = self.network_config.network.value
        return DEFAULT_DIRECTORY_SERVERS.get(network_name, [])

    def get_neutrino_add_peers(self) -> list[str]:
        """Get the configured neutrino add peers."""
        return self.bitcoin.neutrino_add_peers


class TomlConfigSettingsSource(PydanticBaseSettingsSource):
    """
    Custom settings source that reads from a TOML config file.

    The config file is expected at ~/.joinmarket-ng/config.toml or
    $JOINMARKET_DATA_DIR/config.toml if the environment variable is set.
    """

    def __init__(self, settings_cls: type[BaseSettings]) -> None:
        super().__init__(settings_cls)
        self._config: dict[str, Any] = {}
        self._load_config()

    def _get_config_path(self) -> Path:
        """Determine the config file path."""
        # Check for explicit config path in environment
        env_path = os.environ.get("JOINMARKET_CONFIG_FILE")
        if env_path:
            return Path(env_path)

        # Use data directory
        data_dir_env = os.environ.get("JOINMARKET_DATA_DIR")
        data_dir = Path(data_dir_env) if data_dir_env else Path.home() / ".joinmarket-ng"

        return data_dir / "config.toml"

    def _load_config(self) -> None:
        """Load configuration from TOML file."""
        config_path = self._get_config_path()

        if not config_path.exists():
            logger.debug(f"Config file not found at {config_path}, using defaults")
            return

        try:
            import tomllib

            with open(config_path, "rb") as f:
                self._config = tomllib.load(f)

            logger.info(f"Loaded config from {config_path}")
        except tomllib.TOMLDecodeError as e:
            logger.error(f"Invalid TOML syntax in config file {config_path}")
            logger.error(f"Error: {e}")
            logger.error("Please fix the syntax errors in your config file and try again.")
            logger.error(
                "Tip: Make sure section headers like [bitcoin], [tor], etc. are uncommented"
            )
            import sys

            sys.exit(1)
        except Exception as e:
            logger.error(f"Failed to load config from {config_path}: {e}")
            logger.error("Please check your config file and try again.")
            import sys

            sys.exit(1)

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        """Get field value from TOML config."""
        # Handle nested fields by looking up in nested dicts
        value = self._config.get(field_name)
        return value, field_name, value is not None

    def __call__(self) -> dict[str, Any]:
        """Return all config values as a flat dict for pydantic-settings."""
        return self._config


def get_config_path() -> Path:
    """Get the path to the config file."""
    data_dir_env = os.environ.get("JOINMARKET_DATA_DIR")
    data_dir = Path(data_dir_env) if data_dir_env else Path.home() / ".joinmarket-ng"
    return data_dir / "config.toml"


def generate_config_template() -> str:
    """
    Generate a config file template with all settings commented out.

    This allows users to see all available settings with their defaults
    and descriptions, while only uncommenting what they want to change.
    """
    lines: list[str] = []

    lines.append("# JoinMarket NG Configuration")
    lines.append("#")
    lines.append("# This file contains all available settings with their default values.")
    lines.append("# Settings are commented out by default - uncomment to override.")
    lines.append("#")
    lines.append("# Priority (highest to lowest):")
    lines.append("#   1. CLI arguments")
    lines.append("#   2. Environment variables")
    lines.append("#   3. This config file")
    lines.append("#   4. Built-in defaults")
    lines.append("#")
    lines.append("# Environment variables use uppercase with double underscore for nesting:")
    lines.append("#   TOR__SOCKS_HOST=127.0.0.1")
    lines.append("#   BITCOIN__RPC_URL=http://localhost:8332")
    lines.append("#")
    lines.append("")

    # Generate sections for each nested model
    def add_section(title: str, model_cls: type[BaseModel], prefix: str = "") -> None:
        lines.append(f"# {'=' * 60}")
        lines.append(f"# {title}")
        lines.append(f"# {'=' * 60}")
        lines.append(f"[{prefix}]" if prefix else "")
        lines.append("")

        for field_name, field_info in model_cls.model_fields.items():
            # Get description
            desc = field_info.description or ""
            if desc:
                lines.append(f"# {desc}")

            # Get default value
            default = field_info.default
            factory = field_info.default_factory
            if factory is not None:
                # default_factory can be Callable[[], Any] or Callable[[dict], Any]
                # We call with no args for the common case
                try:
                    default = factory()  # type: ignore[call-arg]
                except TypeError:
                    default = factory({})  # type: ignore[call-arg]

            # Format the value for TOML
            if isinstance(default, bool):
                value_str = str(default).lower()
            elif isinstance(default, str):
                value_str = f'"{default}"'
            elif isinstance(default, list):
                # For directory_servers, show example from defaults
                if field_name == "directory_servers" and prefix == "network_config":
                    lines.append("# Mainnet defaults (leave empty to use automatically):")
                    lines.append("# directory_servers = [")
                    for server in DEFAULT_DIRECTORY_SERVERS["mainnet"]:
                        lines.append(f'#   "{server}",')
                    lines.append("# ]")
                    lines.append("")
                    lines.append("# Signet defaults:")
                    lines.append("# directory_servers = [")
                    for server in DEFAULT_DIRECTORY_SERVERS["signet"]:
                        lines.append(f'#   "{server}",')
                    lines.append("# ]")
                    lines.append("")
                    continue
                if field_name == "neutrino_add_peers" and prefix == "bitcoin":
                    lines.append(
                        "# Preferred peers for neutrino (host:port), while discovery stays enabled."
                    )
                    lines.append("# neutrino_add_peers = [")
                    lines.append('#   "your-filter-peer:38333",')
                    lines.append("# ]")
                    lines.append("")
                    continue
                value_str = "[]" if not default else str(default).replace("'", '"')
            elif isinstance(default, SecretStr):
                value_str = '""'
            elif default is None:
                # Skip None values with a comment
                lines.append(f"# {field_name} = ")
                lines.append("")
                continue
            elif hasattr(default, "value"):  # Enum - use string value
                value_str = f'"{default.value}"'
            else:
                value_str = str(default)

            lines.append(f"# {field_name} = {value_str}")
            lines.append("")

    # Data directory (top-level)
    lines.append("# Data directory for JoinMarket files")
    lines.append("# Defaults to ~/.joinmarket-ng or $JOINMARKET_DATA_DIR")
    lines.append("# data_dir = ")
    lines.append("")

    # Add all sections
    add_section("Tor Settings", TorSettings, "tor")
    add_section("Bitcoin Backend Settings", BitcoinSettings, "bitcoin")
    add_section("Network Settings", NetworkSettings, "network_config")
    add_section("Wallet Settings", WalletSettings, "wallet")
    add_section("Notification Settings", NotificationSettings, "notifications")
    add_section("Logging Settings", LoggingSettings, "logging")
    add_section("Maker Settings", MakerSettings, "maker")
    add_section("Taker Settings", TakerSettings, "taker")
    add_section("Directory Server Settings", DirectoryServerSettings, "directory_server")
    add_section("Orderbook Watcher Settings", OrderbookWatcherSettings, "orderbook_watcher")

    return "\n".join(lines)


def _get_bundled_template() -> str | None:
    """Load the bundled config.toml.template from package data.

    Returns:
        Template text, or None if not available.
    """
    try:
        ref = importlib.resources.files("jmcore") / "data" / "config.toml.template"
        return ref.read_text(encoding="utf-8")
    except Exception:
        return None


def _get_user_sections(user_text: str) -> set[str]:
    """Return section names present in user config text.

    Includes both uncommented ``[section]`` headers and legacy commented
    placeholders like ``# [section]``.

    Args:
        user_text: Contents of the user's config.toml.

    Returns:
        Set of section names found in the user config.
    """
    commented_sections = {
        m.group(1) for m in re.finditer(r"^\s*#\s*\[(\w+)]\s*$", user_text, re.MULTILINE)
    }

    import tomlkit

    try:
        doc = tomlkit.parse(user_text)
        return set(doc.keys()) | commented_sections
    except Exception:
        # If parsing fails, fall back to regex for uncommented headers.
        active_sections = {
            m.group(1) for m in re.finditer(r"^\s*\[(\w+)]\s*$", user_text, re.MULTILINE)
        }
        return active_sections | commented_sections


# Regex matching a TOML key assignment: ``key = value`` or ``# key = value``.
_KEY_RE = re.compile(r"^#?\s*(\w+)\s*=", re.MULTILINE)


def _get_template_section_keys(template_text: str) -> dict[str, set[str]]:
    """Extract key names per section from the template.

    Args:
        template_text: Full text of the config template.

    Returns:
        Mapping of section name to set of key names defined in that section.
    """
    section_re = re.compile(r"^\[(\w+)]", re.MULTILINE)
    matches = list(section_re.finditer(template_text))
    result: dict[str, set[str]] = {}
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(template_text)
        section_text = template_text[start:end]
        result[match.group(1)] = {m.group(1) for m in _KEY_RE.finditer(section_text)}
    return result


def _get_user_section_keys(user_text: str) -> dict[str, set[str]]:
    """Extract key names per section from the user's config.

    Uses both uncommented and commented section headers as boundaries
    to correctly scope keys.

    Args:
        user_text: Full text of the user's config.toml.

    Returns:
        Mapping of uncommented section name to set of key names found.
    """
    boundary_re = re.compile(r"^(?:#\s*)?\[(\w+)]", re.MULTILINE)
    uncommented_re = re.compile(r"^\[(\w+)]", re.MULTILINE)
    all_boundaries = list(boundary_re.finditer(user_text))
    uncommented = list(uncommented_re.finditer(user_text))

    result: dict[str, set[str]] = {}
    for match in uncommented:
        start = match.end()
        end = len(user_text)
        for boundary in all_boundaries:
            if boundary.start() > match.start():
                end = boundary.start()
                break
        section_text = user_text[start:end]
        result[match.group(1)] = {m.group(1) for m in _KEY_RE.finditer(section_text)}
    return result


def config_diff(
    config_path: Path,
    template_text: str | None = None,
) -> list[str]:
    """Compare the user's config against the template and report differences.

    This is a **read-only** operation -- the user's config file is never
    modified.  Returns a list of new sections and keys that exist in the
    template but are missing from the user's config, so the caller can
    display them.

    Args:
        config_path: Path to the user's ``config.toml``.
        template_text: Template text to compare against.  When *None*,
            the bundled ``config.toml.template`` shipped with the package
            is used.

    Returns:
        List of human-readable descriptions of missing items (empty if the
        config is up to date).  Each entry is either ``"section:<name>"``
        for a missing section or ``"key:<section>.<key>"`` for a missing
        key within an existing section.
    """
    if template_text is None:
        template_text = _get_bundled_template()
    if template_text is None:
        template_text = generate_config_template()
    if not template_text:
        return []

    if not config_path.exists():
        return []

    user_text = config_path.read_text()
    user_sections = _get_user_sections(user_text)

    # Extract template section names.
    template_section_re = re.compile(r"^\[(\w+)]", re.MULTILINE)
    template_section_names = [m.group(1) for m in template_section_re.finditer(template_text)]

    diffs: list[str] = []

    # Report missing sections.
    for name in template_section_names:
        if name not in user_sections:
            diffs.append(f"section:{name}")

    # Report missing keys within existing sections.
    template_keys = _get_template_section_keys(template_text)
    user_keys = _get_user_section_keys(user_text)
    for section_name in template_section_names:
        if section_name not in user_keys:
            continue
        missing = template_keys.get(section_name, set()) - user_keys[section_name]
        for key in sorted(missing):
            diffs.append(f"key:{section_name}.{key}")

    return diffs


def migrate_config(
    config_path: Path,
    template_text: str | None = None,
) -> list[str]:
    """Create the config file from the template if it does not exist.

    When the config file already exists, no modifications are made.
    Use :func:`config_diff` to discover new settings that the user
    may want to add manually.

    Args:
        config_path: Path to the user's ``config.toml``.
        template_text: Template text for fresh creation.  When *None*,
            the bundled ``config.toml.template`` shipped with the package
            is used.

    Returns:
        Empty list (kept for backward compatibility).
    """
    if template_text is None:
        template_text = _get_bundled_template()
    if template_text is None:
        template_text = generate_config_template()
    if not template_text:
        logger.warning("No config template available; skipping config creation")
        return []

    if not config_path.exists():
        logger.info(f"Config file missing; creating from template at {config_path}")
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(template_text)

    return []


def ensure_config_file(data_dir: Path | None = None) -> Path:
    """Ensure the config file exists.

    On first run the config file is created from the bundled template.
    Existing config files are never modified.

    Args:
        data_dir: Optional data directory path. Uses default if not provided.

    Returns:
        Path to the config file.
    """
    if data_dir is None:
        data_dir = get_default_data_dir()

    config_path = data_dir / "config.toml"
    migrate_config(config_path)
    return config_path


# Global settings instance (lazy-loaded)
_settings: JoinMarketSettings | None = None


def get_settings(**overrides: Any) -> JoinMarketSettings:
    """
    Get the JoinMarket settings instance.

    On first call, loads settings from all sources. Subsequent calls
    return the cached instance unless reset_settings() is called.

    Args:
        **overrides: Optional settings overrides (highest priority)

    Returns:
        JoinMarketSettings instance
    """
    global _settings
    if _settings is None or overrides:
        _settings = JoinMarketSettings(**overrides)
    return _settings


def reset_settings() -> None:
    """Reset the global settings instance (useful for testing)."""
    global _settings
    _settings = None


__all__ = [
    "JoinMarketSettings",
    "TorSettings",
    "BitcoinSettings",
    "NetworkSettings",
    "WalletSettings",
    "NotificationSettings",
    "MakerSettings",
    "TakerSettings",
    "DirectoryServerSettings",
    "OrderbookWatcherSettings",
    "LoggingSettings",
    "get_settings",
    "reset_settings",
    "get_config_path",
    "generate_config_template",
    "ensure_config_file",
    "config_diff",
    "migrate_config",
    "DEFAULT_DIRECTORY_SERVERS",
]
