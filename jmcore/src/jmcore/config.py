"""
Base configuration classes for JoinMarket components.

This module provides Pydantic BaseModel classes that can be inherited
by specific components (maker, taker, etc.) to reduce duplication and
ensure consistency.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, SecretStr, model_validator

from jmcore.constants import DUST_THRESHOLD
from jmcore.models import NetworkType


class TorConfig(BaseModel):
    """
    Configuration for Tor SOCKS proxy connection.

    Used for outgoing connections to directory servers and peers.
    """

    socks_host: str = Field(default="127.0.0.1", description="Tor SOCKS5 proxy host address")
    socks_port: int = Field(default=9050, ge=1, le=65535, description="Tor SOCKS5 proxy port")
    stream_isolation: bool = Field(
        default=True,
        description="Isolate connection types onto separate Tor circuits via SOCKS5 auth",
    )

    model_config = {"frozen": False}


class TorControlConfig(BaseModel):
    """
    Configuration for Tor control port connection.

    When enabled, allows dynamic creation of ephemeral hidden services
    at startup using Tor's control port. This allows generating a new
    .onion address each time without needing to pre-configure the hidden
    service in torrc.

    Requires Tor to be configured with:
        ControlPort 127.0.0.1:9051
        CookieAuthentication 1
        CookieAuthFile /var/lib/tor/control_auth_cookie

    Environment variables (via pydantic-settings):
        TOR__CONTROL_HOST - Tor control host (default: 127.0.0.1)
        TOR__CONTROL_PORT - Tor control port (default: 9051)
        TOR__COOKIE_PATH - Cookie auth file path
        TOR__PASSWORD - Tor control password (not recommended)
    """

    enabled: bool = Field(default=True, description="Enable Tor control port integration")
    host: str = Field(default="127.0.0.1", description="Tor control port host")
    port: int = Field(default=9051, ge=1, le=65535, description="Tor control port")
    cookie_path: Path | None = Field(
        default=None,
        description="Path to Tor cookie auth file (e.g., /var/lib/tor/control_auth_cookie)",
    )
    password: SecretStr | None = Field(
        default=None,
        description="Password for HASHEDPASSWORD auth (not recommended, use cookie auth)",
    )

    model_config = {"frozen": False}


def create_tor_control_config_from_env() -> TorControlConfig:
    """
    Create TorControlConfig from environment variables with smart defaults.

    This is a legacy function for direct env var access. Prefer using
    JoinMarketSettings which handles TOR__* env vars through pydantic-settings.

    Environment variables (legacy format):
        TOR__CONTROL_HOST - Tor control host (default: 127.0.0.1)
        TOR__CONTROL_PORT - Tor control port (default: 9051)
        TOR__COOKIE_PATH - Cookie auth file path
        TOR__PASSWORD - Tor control password

    Auto-detection:
        - If TOR__COOKIE_PATH is set, use it
        - Otherwise try common paths: /run/tor/control.authcookie, /var/run/tor/control.authcookie,
          /var/lib/tor/control_auth_cookie
    """
    from jmcore.settings import get_settings

    settings = get_settings()
    tor = settings.tor

    # Try to find cookie path
    cookie_path: Path | None = None
    if tor.cookie_path:
        cookie_path = Path(tor.cookie_path)
    else:
        # Try common paths (ordered by likelihood on modern Linux systems)
        # - /run/tor/control.authcookie: Debian/Ubuntu with systemd (most common)
        # - /var/run/tor/control.authcookie: Older systems (often symlink to /run)
        # - /var/lib/tor/control_auth_cookie: Tor default example in torrc
        common_paths = [
            Path("/run/tor/control.authcookie"),
            Path("/var/run/tor/control.authcookie"),
            Path("/var/lib/tor/control_auth_cookie"),
        ]
        for path in common_paths:
            # Check that file exists AND has content (non-zero size)
            # An empty cookie file indicates Tor isn't configured to write there
            if path.exists() and path.stat().st_size > 0:
                cookie_path = path
                break

    return TorControlConfig(
        enabled=tor.control_enabled,
        host=tor.control_host,
        port=tor.control_port,
        cookie_path=cookie_path,
        password=tor.password,
    )


class BackendConfig(BaseModel):
    """
    Configuration for Bitcoin backend connection.

    Supports different backend types:
    - scantxoutset: Bitcoin Core RPC with scantxoutset
    - neutrino: Light client using BIP 157/158
    """

    backend_type: str = Field(
        default="scantxoutset",
        description="Backend type: 'scantxoutset' or 'neutrino'",
    )
    backend_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Backend-specific configuration (RPC credentials, neutrino peers, etc.)",
    )

    model_config = {"frozen": False}


class WalletConfig(BaseModel):
    """
    Base wallet configuration shared by all JoinMarket wallet users.

    Includes wallet seed, network settings, HD wallet structure, and
    backend connection details.
    """

    # Wallet seed
    mnemonic: SecretStr = Field(..., description="BIP39 mnemonic phrase for wallet seed")
    passphrase: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        description="BIP39 passphrase (13th/25th word)",
    )

    # Network settings
    network: NetworkType = Field(
        default=NetworkType.MAINNET,
        description="Protocol network for directory server handshakes",
    )
    bitcoin_network: NetworkType | None = Field(
        default=None,
        description="Bitcoin network for address generation (defaults to same as network)",
    )

    # Data directory
    data_dir: Path | None = Field(
        default=None,
        description=(
            "Data directory for JoinMarket files (commitment blacklist, history, etc.). "
            "Defaults to ~/.joinmarket-ng or $JOINMARKET_DATA_DIR if set"
        ),
    )

    # Backend configuration
    backend_type: str = Field(
        default="scantxoutset",
        description="Backend type: 'scantxoutset' or 'neutrino'",
    )
    backend_config: dict[str, Any] = Field(
        default_factory=dict,
        description="Backend-specific configuration",
    )

    # Directory servers
    directory_servers: list[str] = Field(
        default_factory=list,
        description="List of directory server URLs (e.g., ['onion_host:port', ...])",
    )

    # Tor/SOCKS configuration
    socks_host: str = Field(default="127.0.0.1", description="Tor SOCKS5 proxy host")
    socks_port: int = Field(default=9050, ge=1, le=65535, description="Tor SOCKS5 proxy port")
    stream_isolation: bool = Field(
        default=True,
        description="Isolate connection types onto separate Tor circuits via SOCKS5 auth",
    )
    connection_timeout: float = Field(
        default=120.0,
        gt=0.0,
        description=(
            "Timeout in seconds for Tor SOCKS5 connections. Covers TCP handshake, "
            "SOCKS5 negotiation, Tor circuit building, and PoW solving. "
            "Default 120s matches Tor's internal circuit timeout. "
            "Under PoW defense (DoS attack), connections may take significantly "
            "longer than normal (~5-15s)."
        ),
    )

    # HD wallet structure
    address_type: str = Field(
        default="p2wpkh",
        description="Address type for the wallet: 'p2wpkh' or 'p2tr'",
    )
    mixdepth_count: int = Field(
        default=5,
        ge=1,
        le=10,
        description="Number of mixdepths in the wallet (privacy compartments)",
    )
    gap_limit: int = Field(default=20, ge=6, description="BIP44 gap limit for address scanning")

    # Dust threshold
    dust_threshold: int = Field(
        default=DUST_THRESHOLD,
        ge=0,
        description="Dust threshold in satoshis for change outputs (default: 27300)",
    )

    # Descriptor wallet scan configuration
    smart_scan: bool = Field(
        default=True,
        description=(
            "Use smart scan for fast startup (scan from ~1 year ago instead of genesis). "
            "A full rescan runs in background to catch any older transactions."
        ),
    )
    background_full_rescan: bool = Field(
        default=True,
        description=(
            "Run full blockchain rescan in background after smart scan. "
            "This ensures no transactions are missed while allowing fast startup."
        ),
    )
    scan_lookback_blocks: int = Field(
        default=52_560,
        ge=0,
        description=(
            "Number of blocks to look back for smart scan (default: ~1 year = 52560 blocks). "
            "Set to 0 to always scan from genesis (slow but complete)."
        ),
    )

    model_config = {"frozen": False}

    @model_validator(mode="after")
    def set_bitcoin_network_default(self) -> WalletConfig:
        """If bitcoin_network is not set, default to the protocol network."""
        if self.bitcoin_network is None:
            object.__setattr__(self, "bitcoin_network", self.network)
        return self


class DirectoryServerConfig(BaseModel):
    """
    Configuration for directory server instances.

    Used by standalone directory servers, not by clients.
    """

    network: NetworkType = Field(
        default=NetworkType.MAINNET, description="Network type for the directory server"
    )
    host: str = Field(default="127.0.0.1", description="Host address to bind to")
    port: int = Field(default=5222, ge=1, le=65535, description="Port to listen on")

    # Limits
    max_peers: int = Field(default=10000, ge=1, description="Maximum number of connected peers")
    max_message_size: int = Field(
        default=2097152, ge=1024, description="Maximum message size in bytes (default: 2MB)"
    )
    max_line_length: int = Field(
        default=65536, ge=1024, description="Maximum JSON-line message length (default: 64KB)"
    )
    max_json_nesting_depth: int = Field(
        default=10, ge=1, le=100, description="Maximum nesting depth for JSON parsing"
    )

    # Rate limiting
    # Higher limits to accommodate makers responding to orderbook requests
    # A single maker might send multiple offer messages + bond proofs rapidly
    message_rate_limit: int = Field(
        default=500, ge=1, description="Messages per second (sustained)"
    )
    message_burst_limit: int = Field(default=1000, ge=1, description="Maximum burst size")
    rate_limit_disconnect_threshold: int = Field(
        default=200, ge=1, description="Disconnect after N violations"
    )

    # Broadcasting
    broadcast_batch_size: int = Field(
        default=50,
        ge=1,
        description="Batch size for concurrent broadcasts (lower = less memory)",
    )

    # Logging
    log_level: str = Field(default="INFO", description="Logging level")

    # Server info
    motd: str = Field(
        default="JoinMarket Directory Server https://github.com/joinmarket-ng/joinmarket-ng",
        description="Message of the day sent to clients",
    )

    # Health check
    health_check_host: str = Field(
        default="127.0.0.1", description="Host for health check endpoint"
    )
    health_check_port: int = Field(
        default=8080, ge=1, le=65535, description="Port for health check endpoint"
    )

    model_config = {"frozen": False}


__all__ = [
    "TorConfig",
    "TorControlConfig",
    "create_tor_control_config_from_env",
    "BackendConfig",
    "WalletConfig",
    "DirectoryServerConfig",
]
