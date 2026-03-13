"""
Common CLI components for JoinMarket NG.

This module provides reusable CLI helper functions to reduce duplication
across jmwallet, maker, and taker CLIs.

Architecture:
- Resolver functions: Take CLI args + settings and return resolved values
- Setup functions: Common initialization (logging, settings, etc.)
- Mnemonic loading: Unified mnemonic resolution from multiple sources

The CLI parameter definitions remain in each CLI module for now, but the
resolution logic is centralized here. This approach:
- Avoids typer dependency in jmcore
- Allows each CLI to customize parameter names/help text if needed
- Centralizes the complex resolution logic that was duplicated

Usage:
    from jmcore.cli_common import (
        resolve_backend_settings,
        resolve_mnemonic,
        resolve_tor_settings,
        setup_cli,
    )

    @app.command()
    def my_command(
        network: Annotated[str | None, typer.Option("--network")] = None,
        rpc_url: Annotated[str | None, typer.Option("--rpc-url")] = None,
        ...
    ):
        settings = setup_cli(log_level)
        backend = resolve_backend_settings(settings, network=network, rpc_url=rpc_url, ...)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import SecretStr

from jmcore.models import NetworkType
from jmcore.settings import JoinMarketSettings, get_settings, reset_settings

# =============================================================================
# Resolved Settings Dataclasses
# =============================================================================


@dataclass
class ResolvedBackendSettings:
    """Resolved backend settings ready for use."""

    network: str
    bitcoin_network: str
    backend_type: str
    rpc_url: str
    rpc_user: str
    rpc_password: str
    neutrino_url: str
    neutrino_connect_peers: list[str]
    data_dir: Path
    scan_start_height: int | None = None


@dataclass
class ResolvedTorSettings:
    """Resolved Tor settings ready for use."""

    socks_host: str
    socks_port: int
    control_enabled: bool
    control_host: str
    control_port: int
    cookie_path: Path | None


@dataclass
class ResolvedMnemonic:
    """Resolved mnemonic and BIP39 passphrase.

    Note: bip39_passphrase is the optional BIP39 passphrase (13th/25th word),
    NOT the password used to decrypt an encrypted mnemonic file.
    """

    mnemonic: str
    bip39_passphrase: str
    source: str  # Where the mnemonic came from (for logging)


# =============================================================================
# Setup Functions
# =============================================================================


def setup_logging(level: str = "INFO") -> None:
    """
    Configure loguru logging with consistent format.

    Args:
        level: Log level (TRACE, DEBUG, INFO, WARNING, ERROR)
    """
    logger.remove()
    logger.add(
        sys.stderr,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        level=level.upper(),
        colorize=True,
    )


def setup_cli(log_level: str | None = None) -> JoinMarketSettings:
    """
    Common CLI setup: reset settings cache, configure logging, return settings.

    Log level priority: CLI argument > settings (env/config) > default "INFO"

    Args:
        log_level: Log level override from CLI (None means use settings)

    Returns:
        JoinMarketSettings instance with all sources loaded
    """
    reset_settings()
    settings = get_settings()

    # Resolve log level: CLI > settings > default
    effective_log_level = log_level if log_level is not None else settings.logging.level
    setup_logging(effective_log_level)

    return settings


# =============================================================================
# Resolution Functions
# =============================================================================


def resolve_backend_settings(
    settings: JoinMarketSettings,
    *,
    network: NetworkType | str | None = None,
    bitcoin_network: NetworkType | str | None = None,
    backend_type: str | None = None,
    rpc_url: str | None = None,
    rpc_user: str | None = None,
    rpc_password: str | None = None,
    neutrino_url: str | None = None,
    data_dir: Path | None = None,
) -> ResolvedBackendSettings:
    """
    Resolve backend settings with priority: CLI > Settings (env + config) > Defaults.

    Args:
        settings: JoinMarketSettings instance
        network: CLI override for network
        bitcoin_network: CLI override for bitcoin network
        backend_type: CLI override for backend type
        rpc_url: CLI override for RPC URL
        rpc_user: CLI override for RPC user
        rpc_password: CLI override for RPC password
        neutrino_url: CLI override for Neutrino URL
        data_dir: CLI override for data directory

    Returns:
        ResolvedBackendSettings with all values resolved
    """
    # Resolve network
    if network is not None:
        resolved_network = network.value if isinstance(network, NetworkType) else network
    else:
        resolved_network = settings.network_config.network.value

    # Resolve bitcoin network (defaults to network if not specified)
    if bitcoin_network is not None:
        resolved_bitcoin_network = (
            bitcoin_network.value if isinstance(bitcoin_network, NetworkType) else bitcoin_network
        )
    elif settings.network_config.bitcoin_network is not None:
        resolved_bitcoin_network = settings.network_config.bitcoin_network.value
    else:
        resolved_bitcoin_network = resolved_network

    # Resolve backend type
    resolved_backend_type = (
        backend_type if backend_type is not None else settings.bitcoin.backend_type
    )

    # Resolve RPC settings
    resolved_rpc_url = rpc_url if rpc_url is not None else settings.bitcoin.rpc_url
    resolved_rpc_user = rpc_user if rpc_user is not None else settings.bitcoin.rpc_user

    # Handle SecretStr for password
    if rpc_password is not None:
        resolved_rpc_password = rpc_password
    else:
        pwd = settings.bitcoin.rpc_password
        resolved_rpc_password = pwd.get_secret_value() if isinstance(pwd, SecretStr) else str(pwd)

    # Resolve Neutrino URL
    resolved_neutrino_url = (
        neutrino_url if neutrino_url is not None else settings.bitcoin.neutrino_url
    )

    # Resolve Neutrino connect peers
    resolved_neutrino_connect_peers = settings.get_neutrino_connect_peers()

    # Resolve data directory
    resolved_data_dir = data_dir if data_dir is not None else settings.get_data_dir()

    return ResolvedBackendSettings(
        network=resolved_network,
        bitcoin_network=resolved_bitcoin_network,
        backend_type=resolved_backend_type,
        rpc_url=resolved_rpc_url,
        rpc_user=resolved_rpc_user,
        rpc_password=resolved_rpc_password,
        neutrino_url=resolved_neutrino_url,
        neutrino_connect_peers=resolved_neutrino_connect_peers,
        data_dir=resolved_data_dir,
        scan_start_height=settings.wallet.scan_start_height,
    )


def resolve_tor_settings(
    settings: JoinMarketSettings,
    *,
    socks_host: str | None = None,
    socks_port: int | None = None,
    control_host: str | None = None,
    control_port: int | None = None,
    cookie_path: Path | None = None,
    disable_control: bool = False,
) -> ResolvedTorSettings:
    """
    Resolve Tor settings with priority: CLI > Settings > Defaults.

    Args:
        settings: JoinMarketSettings instance
        socks_host: CLI override for SOCKS host
        socks_port: CLI override for SOCKS port
        control_host: CLI override for control host
        control_port: CLI override for control port
        cookie_path: CLI override for cookie path
        disable_control: Whether to disable Tor control

    Returns:
        ResolvedTorSettings with all values resolved
    """
    resolved_socks_host = socks_host if socks_host is not None else settings.tor.socks_host
    resolved_socks_port = socks_port if socks_port is not None else settings.tor.socks_port

    # Control port settings
    control_enabled = not disable_control and settings.tor.control_enabled

    resolved_control_host = control_host if control_host is not None else settings.tor.control_host
    resolved_control_port = control_port if control_port is not None else settings.tor.control_port

    resolved_cookie_path: Path | None = None
    if cookie_path is not None:
        resolved_cookie_path = cookie_path
    elif settings.tor.cookie_path:
        resolved_cookie_path = Path(settings.tor.cookie_path)

    return ResolvedTorSettings(
        socks_host=resolved_socks_host,
        socks_port=resolved_socks_port,
        control_enabled=control_enabled,
        control_host=resolved_control_host,
        control_port=resolved_control_port,
        cookie_path=resolved_cookie_path,
    )


def resolve_directory_servers(
    settings: JoinMarketSettings,
    *,
    directory_servers: str | None = None,
    network: str | None = None,
) -> list[str]:
    """
    Resolve directory servers with priority: CLI > Settings > Network defaults.

    Args:
        settings: JoinMarketSettings instance
        directory_servers: CLI override (comma-separated)
        network: Network to use for defaults (if not in settings)

    Returns:
        List of directory server addresses
    """
    if directory_servers:
        return [s.strip() for s in directory_servers.split(",") if s.strip()]

    if settings.network_config.directory_servers:
        return settings.network_config.directory_servers

    # Use network-specific defaults
    from jmcore.settings import DEFAULT_DIRECTORY_SERVERS

    effective_network = network or settings.network_config.network.value
    return DEFAULT_DIRECTORY_SERVERS.get(effective_network, [])


# =============================================================================
# Mnemonic Loading
# =============================================================================


def _prompt_for_password() -> str:
    """Prompt for mnemonic file password interactively."""
    try:
        import typer

        return typer.prompt("Enter mnemonic file password", hide_input=True)
    except ImportError:
        import getpass

        return getpass.getpass("Enter mnemonic file password: ")


def load_mnemonic_from_file(
    path: Path,
    password: str | None = None,
    auto_prompt: bool = True,
) -> str:
    """
    Load mnemonic from a file (plain text or Fernet encrypted).

    Args:
        path: Path to mnemonic file
        password: Password for decrypting the file (NOT BIP39 passphrase)
        auto_prompt: If True, prompt for password when encrypted file is detected

    Returns:
        The mnemonic phrase

    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file format is invalid or decryption fails
    """
    if not path.exists():
        raise FileNotFoundError(f"Mnemonic file not found: {path}")

    content = path.read_bytes()

    # Try to decode as plain text first
    try:
        text = content.decode("utf-8")
        # Check if it looks like a valid mnemonic (words separated by spaces)
        words = text.strip().split()
        if len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() for w in words):
            return text.strip()
    except UnicodeDecodeError:
        pass

    # If not plain text, assume it's Fernet encrypted
    if not password:
        password = os.environ.get("MNEMONIC_PASSWORD")
    if not password:
        if auto_prompt:
            password = _prompt_for_password()
        else:
            raise ValueError(
                f"Mnemonic file appears to be encrypted. "
                f"Set MNEMONIC_PASSWORD env, wallet.mnemonic_password in config, "
                f"or use interactive prompt: {path}"
            )

    # Try Fernet decryption
    try:
        import base64

        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        if len(content) < 16:
            raise ValueError("Invalid encrypted data")

        # Extract salt and encrypted token
        salt = content[:16]
        encrypted_token = content[16:]

        # Derive key from password
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))

        # Decrypt
        fernet = Fernet(key)
        try:
            decrypted = fernet.decrypt(encrypted_token)
            mnemonic = decrypted.decode("utf-8")
        except InvalidToken as e:
            raise ValueError("Decryption failed - wrong password or corrupted file") from e
        except UnicodeDecodeError as e:
            raise ValueError(
                f"Decrypted content is not valid UTF-8. File may be corrupted or "
                f"encrypted with a different tool: {path}"
            ) from e
    except ImportError as e:
        raise ValueError(
            "Fernet encryption requires cryptography library. Install with: pip install cryptography"
        ) from e

    # Basic validation
    words = mnemonic.split()
    if len(words) not in (12, 15, 18, 21, 24):
        raise ValueError(
            f"Invalid mnemonic: expected 12-24 words, got {len(words)}. "
            f"File may be corrupted or in wrong format: {path}"
        )

    return mnemonic


def resolve_mnemonic(
    settings: JoinMarketSettings,
    *,
    mnemonic: str | None = None,
    mnemonic_file: Path | None = None,
    password: str | None = None,
    bip39_passphrase: str | None = None,
    prompt_bip39_passphrase: bool = False,
    required: bool = True,
) -> ResolvedMnemonic | None:
    """
    Resolve mnemonic from various sources with priority.

    Mnemonic priority:
    1. --mnemonic argument
    2. --mnemonic-file argument
    3. MNEMONIC_FILE environment variable
    4. MNEMONIC environment variable
    5. Config file wallet.mnemonic_file setting
    6. Default wallet path (~/.joinmarket-ng/wallets/default.mnemonic)

    BIP39 passphrase priority:
    1. --bip39-passphrase argument
    2. BIP39_PASSPHRASE environment variable
    3. Config file wallet.bip39_passphrase setting
    4. Interactive prompt (if --prompt-bip39-passphrase is set)
    5. Empty string (default - no passphrase)

    For encrypted mnemonic files, the password is resolved as:
    1. Config file wallet.mnemonic_password setting (or password param)
    2. MNEMONIC_PASSWORD environment variable
    3. Interactive prompt (if auto_prompt is enabled)

    Args:
        settings: JoinMarketSettings instance
        mnemonic: CLI mnemonic string
        mnemonic_file: CLI mnemonic file path
        password: Password for encrypted mnemonic file (NOT BIP39 passphrase)
        bip39_passphrase: BIP39 passphrase (13th/25th word, NOT file encryption password)
        prompt_bip39_passphrase: Whether to prompt for BIP39 passphrase interactively
        required: Whether mnemonic is required (raises error if not found)

    Returns:
        ResolvedMnemonic or None if not required and not found

    Raises:
        ValueError: If required but not found, or if loading fails
    """
    resolved_mnemonic: str | None = None
    source = ""

    # Priority 1: Direct mnemonic argument
    if mnemonic:
        resolved_mnemonic = mnemonic
        source = "--mnemonic argument"

    # Priority 2: Mnemonic file argument
    elif mnemonic_file:
        resolved_mnemonic = load_mnemonic_from_file(mnemonic_file, password)
        source = f"--mnemonic-file ({mnemonic_file})"

    # Priority 3: MNEMONIC_FILE environment variable
    elif env_file := os.environ.get("MNEMONIC_FILE"):
        env_path = Path(env_file)
        resolved_mnemonic = load_mnemonic_from_file(env_path, password)
        source = f"MNEMONIC_FILE env ({env_path})"

    # Priority 4: MNEMONIC environment variable
    elif env_mnemonic := os.environ.get("MNEMONIC"):
        resolved_mnemonic = env_mnemonic
        source = "MNEMONIC env"

    # Priority 5: Config file wallet.mnemonic_file
    elif settings.wallet.mnemonic_file:
        config_path = Path(settings.wallet.mnemonic_file)
        # Use config password if CLI password not provided
        config_password = password
        if config_password is None and settings.wallet.mnemonic_password:
            config_password = settings.wallet.mnemonic_password.get_secret_value()
        resolved_mnemonic = load_mnemonic_from_file(config_path, config_password)
        source = f"config file ({config_path})"

    # Priority 6: Default wallet path
    else:
        default_wallet = settings.get_data_dir() / "wallets" / "default.mnemonic"
        if default_wallet.exists():
            # Use config password if CLI password not provided
            config_password = password
            if config_password is None and settings.wallet.mnemonic_password:
                config_password = settings.wallet.mnemonic_password.get_secret_value()
            resolved_mnemonic = load_mnemonic_from_file(default_wallet, config_password)
            source = f"default wallet ({default_wallet})"

    if resolved_mnemonic is None:
        if required:
            raise ValueError(
                "No mnemonic provided. Use --mnemonic-file, "
                "MNEMONIC env, or set wallet.mnemonic_file in config."
            )
        return None

    # Resolve BIP39 passphrase
    # Priority: CLI arg > env var > config > prompt > empty
    resolved_passphrase = ""
    if bip39_passphrase:
        resolved_passphrase = bip39_passphrase
    elif env_passphrase := os.environ.get("BIP39_PASSPHRASE"):
        resolved_passphrase = env_passphrase
    elif settings.wallet.bip39_passphrase is not None:
        resolved_passphrase = settings.wallet.bip39_passphrase.get_secret_value()
    elif prompt_bip39_passphrase:
        # Lazy import typer only when needed for prompting
        try:
            import typer

            resolved_passphrase = typer.prompt(
                "Enter BIP39 passphrase (leave empty for none)",
                default="",
                hide_input=True,
            )
        except ImportError:
            # Fall back to getpass if typer not available
            import getpass

            resolved_passphrase = getpass.getpass("Enter BIP39 passphrase (leave empty for none): ")

    return ResolvedMnemonic(
        mnemonic=resolved_mnemonic,
        bip39_passphrase=resolved_passphrase,
        source=source,
    )


def resolve_bip39_passphrase(
    bip39_passphrase: str | None = None,
    prompt: bool = False,
) -> str:
    """
    Resolve BIP39 passphrase from argument or prompt.

    Args:
        bip39_passphrase: Direct passphrase value
        prompt: Whether to prompt interactively

    Returns:
        Resolved passphrase (empty string if none)
    """
    if bip39_passphrase:
        return bip39_passphrase

    if prompt:
        try:
            import typer

            return typer.prompt(
                "Enter BIP39 passphrase (leave empty for none)",
                default="",
                hide_input=True,
            )
        except ImportError:
            import getpass

            return getpass.getpass("Enter BIP39 passphrase (leave empty for none): ")

    return ""


# =============================================================================
# Backend Factory
# =============================================================================


def create_backend(
    backend_settings: ResolvedBackendSettings,
    *,
    wallet_name: str | None = None,
) -> Any:
    """
    Create a backend instance based on resolved settings.

    Args:
        backend_settings: Resolved backend settings
        wallet_name: Wallet name for descriptor_wallet backend

    Returns:
        Backend instance (BitcoinCoreBackend, DescriptorWalletBackend, or NeutrinoBackend)

    Raises:
        ValueError: If backend type is invalid
        ImportError: If backend module not available
    """
    # Import backends lazily to avoid circular imports
    from jmwallet.backends import BitcoinCoreBackend
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
    from jmwallet.backends.neutrino import NeutrinoBackend

    backend_type = backend_settings.backend_type

    if backend_type == "neutrino":
        return NeutrinoBackend(
            neutrino_url=backend_settings.neutrino_url,
            network=backend_settings.bitcoin_network,
            scan_start_height=backend_settings.scan_start_height,
            connect_peers=backend_settings.neutrino_connect_peers,
        )
    elif backend_type == "descriptor_wallet":
        if not wallet_name:
            raise ValueError("wallet_name required for descriptor_wallet backend")
        return DescriptorWalletBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
            wallet_name=wallet_name,
        )
    elif backend_type == "scantxoutset":
        return BitcoinCoreBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
        )
    else:
        raise ValueError(
            f"Invalid backend type: {backend_type}. "
            f"Valid options: scantxoutset, descriptor_wallet, neutrino"
        )


def generate_descriptor_wallet_name(
    mnemonic: str,
    network: str,
    passphrase: str = "",
) -> str:
    """
    Generate a deterministic wallet name from mnemonic fingerprint.

    Args:
        mnemonic: BIP39 mnemonic
        network: Network name (mainnet, testnet, etc.)
        passphrase: BIP39 passphrase

    Returns:
        Wallet name in format "jm-{fingerprint}-{network}"
    """
    from jmwallet.backends.descriptor_wallet import (
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )

    fingerprint = get_mnemonic_fingerprint(mnemonic, passphrase)
    return generate_wallet_name(fingerprint, network)


# =============================================================================
# Logging Helpers
# =============================================================================


def log_resolved_settings(
    backend: ResolvedBackendSettings,
    tor: ResolvedTorSettings | None = None,
    directory_servers: list[str] | None = None,
    mnemonic_source: str | None = None,
) -> None:
    """
    Log resolved settings for debugging/transparency.

    Args:
        backend: Resolved backend settings
        tor: Resolved Tor settings (optional)
        directory_servers: Resolved directory servers (optional)
        mnemonic_source: Source of mnemonic (optional)
    """
    logger.info(f"Network: {backend.network}")
    if backend.bitcoin_network != backend.network:
        logger.info(f"Bitcoin network: {backend.bitcoin_network}")
    logger.info(f"Backend: {backend.backend_type}")

    if backend.backend_type == "neutrino":
        logger.info(f"Neutrino URL: {backend.neutrino_url}")
    else:
        logger.info(f"RPC URL: {backend.rpc_url}")
        if backend.rpc_user:
            logger.info(f"RPC user: {backend.rpc_user}")

    if tor:
        logger.info(f"Tor SOCKS: {tor.socks_host}:{tor.socks_port}")
        if tor.control_enabled:
            logger.info(f"Tor control: {tor.control_host}:{tor.control_port}")

    if directory_servers:
        logger.info(f"Directory servers: {len(directory_servers)} configured")

    if mnemonic_source:
        logger.info(f"Mnemonic loaded from: {mnemonic_source}")
