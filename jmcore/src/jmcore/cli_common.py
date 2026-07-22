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
        settings = setup_cli(log_level, data_dir=data_dir)
        backend = resolve_backend_settings(settings, network=network, rpc_url=rpc_url, ...)
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from loguru import logger
from pydantic import SecretStr, ValidationError

from jmcore.models import NetworkType
from jmcore.settings import JoinMarketSettings, get_config_path, get_settings, reset_settings

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
    neutrino_add_peers: list[str]
    data_dir: Path
    scan_start_height: int | None = None
    neutrino_tls_cert: str | None = None
    neutrino_auth_token: str | None = None
    fee_estimate_url: str | None = None
    fee_estimate_proxy: str | None = None


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
    creation_height: int | None = None  # Block height at wallet creation time


# =============================================================================
# Help Formatting
# =============================================================================


class SortedHelpFormatter(argparse.HelpFormatter):
    """Argparse help formatter that lists options and subcommands alphabetically.

    - Optional arguments are sorted by their first long name (fallback: first
      short name), case-insensitively, both in the options list and in the
      usage synopsis.
    - Positional arguments keep their declared order (it defines call syntax)
      and stay ahead of the sorted options within mixed groups.
    - Subcommand listings (from ``add_subparsers``) are sorted by name.

    Typer-based CLIs get the same behavior from :mod:`jmcore.cli_help`.
    """

    @staticmethod
    def _sort_key(action: argparse.Action) -> tuple[int, str]:
        if not action.option_strings:
            # Stable sort keeps positionals in declaration order, first.
            return (0, "")
        long_opts = [opt for opt in action.option_strings if opt.startswith("--")]
        name = long_opts[0] if long_opts else action.option_strings[0]
        return (1, name.lstrip("-").casefold())

    def add_usage(
        self,
        usage: str | None,
        actions: Iterable[argparse.Action],
        groups: Iterable[Any],
        prefix: str | None = None,
    ) -> None:
        super().add_usage(usage, sorted(actions, key=self._sort_key), groups, prefix)

    def add_arguments(self, actions: Iterable[argparse.Action]) -> None:
        super().add_arguments(sorted(actions, key=self._sort_key))

    def _metavar_formatter(
        self, action: argparse.Action, default_metavar: str
    ) -> Callable[[int], tuple[str, ...]]:
        if (
            isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
            and action.metavar is None
            and action.choices is not None
        ):
            # Sort the "{cmd1,cmd2,...}" usage metavar. Regular choices (e.g.
            # log levels) keep their declared, often meaningful, order.
            choices = ",".join(sorted(str(choice) for choice in action.choices))
            result = f"{{{choices}}}"
            return lambda tuple_size: (result,) * tuple_size
        return super()._metavar_formatter(action, default_metavar)

    def _iter_indented_subactions(
        self, action: argparse.Action
    ) -> Generator[argparse.Action, None, None]:
        try:
            get_subactions = action._get_subactions  # type: ignore[attr-defined]
        except AttributeError:
            return
        self._indent()
        yield from sorted(get_subactions(), key=lambda subaction: subaction.dest)
        self._dedent()


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


def setup_cli(
    log_level: str | None = None,
    data_dir: Path | None = None,
    config_file: Path | None = None,
) -> JoinMarketSettings:
    """
    Common CLI setup: reset settings cache, configure logging, return settings.

    Log level priority: CLI argument > settings (env/config) > default "INFO"

    When ``data_dir`` is provided (typically from a CLI ``--data-dir`` flag),
    the ``JOINMARKET_DATA_DIR`` environment variable is set to that path
    BEFORE settings are loaded. This ensures that ``<data_dir>/config.toml``
    is read, that ``wallet.mnemonic_file`` defaults resolve to that data
    directory, and that any other env-driven defaults (paths, etc.) point at
    the user-specified data directory.

    When ``config_file`` is provided (typically from a CLI ``--config-file``
    flag), the ``JOINMARKET_CONFIG_FILE`` environment variable is set so that
    the config is read from (and created at) that explicit path instead of
    ``<data_dir>/config.toml``. This decouples the configuration file location
    from the data directory for FHS-style deployments (e.g. ``/etc/joinmarket``
    config with ``/var/lib/joinmarket`` data); see issue #537.

    Args:
        log_level: Log level override from CLI (None means use settings)
        data_dir: Optional CLI override for data directory. When provided,
            ``JOINMARKET_DATA_DIR`` is set in ``os.environ`` so that all
            subsequent settings-loading code paths agree on the same data
            directory.
        config_file: Optional CLI override for the config file path. When
            provided, ``JOINMARKET_CONFIG_FILE`` is set in ``os.environ`` so
            that settings are read from and created at that explicit path.

    Returns:
        JoinMarketSettings instance with all sources loaded
    """
    if data_dir is not None:
        # Honour the user's CLI choice across every code path that reads
        # JOINMARKET_DATA_DIR (settings, paths, subprocesses, etc.).
        os.environ["JOINMARKET_DATA_DIR"] = str(data_dir)

    if config_file is not None:
        # Decouple the config file from the data dir. Expand ``~`` so a
        # ``--config-file ~/foo.toml`` does not become a literal path.
        os.environ["JOINMARKET_CONFIG_FILE"] = str(Path(config_file).expanduser())

    # Apply an early logging configuration before loading settings so that
    # informational messages emitted while parsing config.toml (e.g.
    # "Loaded config from ...") honor the caller's intended log level.
    # Priority for this early pass: CLI arg > LOGGING__LEVEL env > INFO default.
    early_level = log_level or os.environ.get("LOGGING__LEVEL") or "INFO"
    setup_logging(early_level)

    reset_settings()
    try:
        settings = get_settings()
    except ValidationError as exc:
        _exit_on_invalid_config(exc)

    # Resolve log level: CLI > settings > default
    effective_log_level = log_level if log_level is not None else settings.logging.level
    setup_logging(effective_log_level)

    return settings


def _exit_on_invalid_config(exc: ValidationError) -> NoReturn:
    """Log an actionable message for a config validation error and exit.

    pydantic raises ``ValidationError`` when a value in ``config.toml`` (or an
    environment variable) has the wrong type or is out of range. Without this
    handler the raw traceback propagates and the process exits with a bare
    code 1 (e.g. a systemd unit only shows ``status=1/FAILURE``), leaving the
    operator with no clue which setting is wrong. Translate each error into a
    ``section.key: message`` line pointing at the offending value.
    """
    logger.error(f"Invalid configuration in {get_config_path()}:")
    for err in exc.errors():
        location = ".".join(str(part) for part in err.get("loc", ())) or "<root>"
        message = err.get("msg", "invalid value")
        logger.error(f"  {location}: {message}")
        if location.endswith("urls"):
            logger.error(
                "    Tip: notification urls must be a TOML array, e.g. "
                'urls = ["tgram://bottoken/ChatID"]'
            )
    logger.error("Please fix these values in your config file and try again.")
    sys.exit(1)


# =============================================================================
# Resolution Functions
# =============================================================================


def _resolve_data_dir_path(value: str | None, data_dir: Path) -> str | None:
    """Resolve a (possibly relative) filesystem path against the data dir.

    The user-facing rule is simple: anything that lives under the data
    directory should be writable as a relative path in ``config.toml``
    (or via env vars), and JoinMarket NG will look for it inside whichever
    data directory is in effect (``--data-dir`` flag, ``JOINMARKET_DATA_DIR``
    env var, or the default ``~/.joinmarket-ng``).

    - ``None`` / empty string: passed through unchanged.
    - Absolute paths (``/foo/bar``): returned as-is.
    - ``~``/``~user`` paths: expanded with ``Path.expanduser`` only.
    - Relative paths (``neutrino/tls.cert``): joined onto ``data_dir``.

    Args:
        value: Configured path string, or None.
        data_dir: Resolved JoinMarket data directory.

    Returns:
        Resolved absolute path string, or ``None`` if input was falsy.
    """
    if not value:
        return None

    # Honour explicit ``~`` expansion before doing anything else: users who
    # write ``~/something`` clearly meant their home directory, not the data
    # directory.
    if value.startswith("~"):
        return str(Path(value).expanduser())

    path = Path(value)
    if path.is_absolute():
        return str(path)

    # Relative path -> resolve against the data directory.
    return str(data_dir / path)


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
    neutrino_tls_cert: str | None = None,
    neutrino_auth_token: str | None = None,
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
        neutrino_tls_cert: CLI override for Neutrino TLS certificate path
        neutrino_auth_token: CLI override for Neutrino API auth token
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

    # Resolve Neutrino add peers
    resolved_neutrino_add_peers = settings.get_neutrino_add_peers()

    # Resolve data directory (used both as the resolved value AND as a base
    # for resolving relative neutrino paths below).
    resolved_data_dir = data_dir if data_dir is not None else settings.get_data_dir()

    # Resolve Neutrino TLS cert path. Relative paths (e.g. "neutrino/tls.cert")
    # are resolved against the data directory so a single config.toml works
    # regardless of where the data dir lives.
    resolved_neutrino_tls_cert = _resolve_data_dir_path(
        neutrino_tls_cert if neutrino_tls_cert is not None else settings.bitcoin.neutrino_tls_cert,
        resolved_data_dir,
    )

    # Resolve Neutrino auth token (string, not a path).
    resolved_neutrino_auth_token = (
        neutrino_auth_token
        if neutrino_auth_token is not None
        else settings.bitcoin.neutrino_auth_token
    )

    # If only an auth token *file* is configured, read it now so the backend
    # never has to know where the data dir lives. Relative paths are resolved
    # against the data directory. A missing file is not an error: the default
    # path may simply not exist on insecure/legacy neutrino-api deployments.
    if not resolved_neutrino_auth_token and settings.bitcoin.neutrino_auth_token_file:
        token_path_str = _resolve_data_dir_path(
            settings.bitcoin.neutrino_auth_token_file, resolved_data_dir
        )
        if token_path_str is not None:
            token_path = Path(token_path_str)
            if token_path.is_file():
                try:
                    resolved_neutrino_auth_token = token_path.read_text().strip() or None
                except OSError as exc:
                    logger.warning(
                        f"Could not read neutrino auth token from {token_path_str}: {exc}"
                    )
            else:
                logger.debug(f"Neutrino auth token file not found at {token_path_str}; skipping")

    # neutrino-api serves HTTPS (with a self-signed certificate) whenever API
    # authentication is enabled. When a token is present but the URL is still
    # plain HTTP, upgrade it so requests don't fail with "client sent an HTTP
    # request to an HTTPS server". The backend pins the certificate on first
    # use (trust-on-first-use), so no manual cert wiring is required.
    if resolved_neutrino_auth_token and resolved_neutrino_url.startswith("http://"):
        upgraded_url = "https://" + resolved_neutrino_url.removeprefix("http://")
        logger.info(
            f"Neutrino auth token detected; upgrading URL from "
            f"{resolved_neutrino_url} to {upgraded_url}"
        )
        resolved_neutrino_url = upgraded_url

    # External fee source for backends without native estimation (neutrino).
    from jmcore.fee_source import build_fee_source_proxy

    resolved_fee_estimate_proxy = build_fee_source_proxy(
        settings.tor.socks_host,
        settings.tor.socks_port,
        settings.tor.stream_isolation,
    )

    return ResolvedBackendSettings(
        network=resolved_network,
        bitcoin_network=resolved_bitcoin_network,
        backend_type=resolved_backend_type,
        rpc_url=resolved_rpc_url,
        rpc_user=resolved_rpc_user,
        rpc_password=resolved_rpc_password,
        neutrino_url=resolved_neutrino_url,
        neutrino_add_peers=resolved_neutrino_add_peers,
        data_dir=resolved_data_dir,
        scan_start_height=settings.wallet.scan_start_height,
        neutrino_tls_cert=resolved_neutrino_tls_cert,
        neutrino_auth_token=resolved_neutrino_auth_token,
        fee_estimate_url=settings.bitcoin.fee_estimate_url,
        fee_estimate_proxy=resolved_fee_estimate_proxy,
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


def _prompt_for_password(path: Path | None = None) -> str:
    """Prompt for mnemonic file password interactively.

    When ``path`` is provided, the wallet file name is shown in the prompt so
    the user knows which wallet they are unlocking. This avoids confusing
    "Decryption failed" errors when multiple wallets exist (see issue #454).
    """
    if path is not None:
        prompt_text = f"Enter password for wallet '{path.name}'"
    else:
        prompt_text = "Enter mnemonic file password"
    try:
        import typer

        return typer.prompt(prompt_text, hide_input=True)
    except ImportError:
        import getpass

        return getpass.getpass(f"{prompt_text}: ")


def load_mnemonic_from_file(
    path: Path,
    password: str | None = None,
    auto_prompt: bool = True,
    max_prompt_attempts: int = 3,
) -> str:
    """
    Load mnemonic from a file (plain text or Fernet encrypted).

    Args:
        path: Path to mnemonic file
        password: Password for decrypting the file (NOT BIP39 passphrase)
        auto_prompt: If True, prompt for password when encrypted file is detected
        max_prompt_attempts: When interactively prompting for a password, how
            many times to retry on wrong-password errors before giving up.
            A value of 1 disables retry. Only applies to the interactive
            prompt path: explicit ``password`` arguments and passwords coming
            from the MNEMONIC_PASSWORD env var still fail fast on mismatch.

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
    prompt_used = False
    if not password:
        password = os.environ.get("MNEMONIC_PASSWORD")
    if not password:
        if auto_prompt:
            password = _prompt_for_password(path)
            prompt_used = True
        else:
            raise ValueError(
                f"Mnemonic file appears to be encrypted. "
                f"Set MNEMONIC_PASSWORD env, wallet.mnemonic_password in config, "
                f"or use interactive prompt: {path}"
            )

    # Retry budget: only honoured for passwords the user is typing interactively
    # so that scripted callers with an explicit password/env var still fail
    # fast (issue #456 applies to the TUI's manual prompt path).
    attempts_remaining = max_prompt_attempts if prompt_used else 1
    mnemonic: str | None = None
    last_error: ValueError | None = None

    while attempts_remaining > 0:
        try:
            mnemonic = _decrypt_fernet_mnemonic(content, password, path)
            break
        except ValueError as e:
            last_error = e
            attempts_remaining -= 1
            # Only retry wrong-password errors from the interactive prompt.
            if not prompt_used or attempts_remaining <= 0:
                raise
            if "decryption failed" not in str(e).lower():
                # Corruption or encoding error -- pointless to retry.
                raise
            try:
                import typer

                typer.echo(f"Decryption failed. {attempts_remaining} attempt(s) remaining.")
            except ImportError:
                print(f"Decryption failed. {attempts_remaining} attempt(s) remaining.")
            password = _prompt_for_password(path)

    if mnemonic is None:
        # Should not happen because the loop either breaks or raises, but keep
        # type checkers happy and surface the last error.
        assert last_error is not None
        raise last_error

    # Basic validation
    words = mnemonic.split()
    if len(words) not in (12, 15, 18, 21, 24):
        raise ValueError(
            f"Invalid mnemonic: expected 12-24 words, got {len(words)}. "
            f"File may be corrupted or in wrong format: {path}"
        )

    return mnemonic


def _decrypt_fernet_mnemonic(content: bytes, password: str, path: Path) -> str:
    """Decrypt a Fernet-encrypted mnemonic blob.

    Raised ValueError differentiates three classes of failure:
    - "Decryption failed - wrong password or corrupted file" (InvalidToken)
    - "Decrypted content is not valid UTF-8..." (corrupt plaintext)
    - "Invalid encrypted data" (too short)
    """
    try:
        import base64

        from cryptography.fernet import Fernet, InvalidToken
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    except ImportError as e:
        raise ValueError(
            "Fernet encryption requires cryptography library. Install with: pip install cryptography"
        ) from e

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
        return decrypted.decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Decryption failed - wrong password or corrupted file") from e
    except UnicodeDecodeError as e:
        raise ValueError(
            f"Decrypted content is not valid UTF-8. File may be corrupted or "
            f"encrypted with a different tool: {path}"
        ) from e


def resolve_configured_mnemonic_file(settings: JoinMarketSettings) -> Path | None:
    """Resolve the active wallet's mnemonic file path without decrypting it.

    Mirrors the file-based portion of :func:`resolve_mnemonic`'s priority
    chain so per-wallet read commands can locate the configured wallet and
    read its companion ``.meta`` fingerprint without prompting for a
    password:

    1. ``MNEMONIC_FILE`` environment variable
    2. Config file ``wallet.mnemonic_file`` setting
    3. Default wallet path (``<data_dir>/wallets/default.mnemonic``)

    Raw-mnemonic sources (``--mnemonic``/``MNEMONIC`` env) have no backing
    file and are intentionally ignored here. Returns ``None`` when no
    file-based wallet is configured or the default does not exist.
    """
    if env_file := os.environ.get("MNEMONIC_FILE"):
        return Path(env_file)
    if settings.wallet.mnemonic_file:
        return Path(settings.wallet.mnemonic_file)
    default_wallet = settings.get_data_dir() / "wallets" / "default.mnemonic"
    if default_wallet.exists():
        return default_wallet
    return None


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
    mnemonic_file_path: Path | None = None  # Track file path for .meta loading

    # Resolve the encryption password once for all file-based paths so that
    # ``wallet.mnemonic_password`` from config.toml works no matter whether
    # the file path came from --mnemonic-file, MNEMONIC_FILE env, the config,
    # or the default wallet location. Without this fallback an explicit
    # --mnemonic-file (as used by the systemd unit template documented in
    # docs/README-maker.md) silently bypasses the config password and falls
    # through to an interactive prompt, which fails under systemd.
    # See issue #498.
    effective_password = password
    if effective_password is None and settings.wallet.mnemonic_password:
        effective_password = settings.wallet.mnemonic_password.get_secret_value()

    # Priority 1: Direct mnemonic argument
    if mnemonic:
        resolved_mnemonic = mnemonic
        source = "--mnemonic argument"

    # Priority 2: Mnemonic file argument
    elif mnemonic_file:
        resolved_mnemonic = load_mnemonic_from_file(mnemonic_file, effective_password)
        mnemonic_file_path = mnemonic_file
        source = f"--mnemonic-file ({mnemonic_file})"

    # Priority 3: MNEMONIC_FILE environment variable
    elif env_file := os.environ.get("MNEMONIC_FILE"):
        env_path = Path(env_file)
        resolved_mnemonic = load_mnemonic_from_file(env_path, effective_password)
        mnemonic_file_path = env_path
        source = f"MNEMONIC_FILE env ({env_path})"

    # Priority 4: MNEMONIC environment variable
    elif env_mnemonic := os.environ.get("MNEMONIC"):
        resolved_mnemonic = env_mnemonic
        source = "MNEMONIC env"

    # Priority 5: Config file wallet.mnemonic_file
    elif settings.wallet.mnemonic_file:
        config_path = Path(settings.wallet.mnemonic_file)
        resolved_mnemonic = load_mnemonic_from_file(config_path, effective_password)
        mnemonic_file_path = config_path
        source = f"config file ({config_path})"

    # Priority 6: Default wallet path
    else:
        default_wallet = settings.get_data_dir() / "wallets" / "default.mnemonic"
        if default_wallet.exists():
            resolved_mnemonic = load_mnemonic_from_file(default_wallet, effective_password)
            mnemonic_file_path = default_wallet
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

    # Load wallet metadata (creation_height) from companion .meta file
    creation_height: int | None = None
    if mnemonic_file_path is not None:
        try:
            from jmwallet.cli.mnemonic import load_mnemonic_meta

            meta = load_mnemonic_meta(mnemonic_file_path)
            raw_creation_height = meta.get("creation_height")
            if isinstance(raw_creation_height, int) and not isinstance(raw_creation_height, bool):
                if raw_creation_height >= 0:
                    creation_height = raw_creation_height
                    logger.debug(f"Loaded wallet creation height: {creation_height}")
                else:
                    logger.warning(
                        f"Ignoring negative creation_height in metadata: {raw_creation_height}"
                    )
            elif raw_creation_height is not None:
                logger.warning(
                    f"Ignoring non-integer creation_height in metadata: {raw_creation_height!r}"
                )
        except Exception as exc:
            logger.debug(f"Could not load mnemonic metadata: {exc}")

    return ResolvedMnemonic(
        mnemonic=resolved_mnemonic,
        bip39_passphrase=resolved_passphrase,
        source=source,
        creation_height=creation_height,
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
    creation_height: int | None = None,
) -> Any:
    """
    Create a backend instance based on resolved settings.

    Args:
        backend_settings: Resolved backend settings
        wallet_name: Wallet name for descriptor_wallet backend
        creation_height: Block height at wallet creation time (used as scan start hint)

    Returns:
        Backend instance (DescriptorWalletBackend or NeutrinoBackend)

    Raises:
        ValueError: If backend type is invalid
        ImportError: If backend module not available
    """
    # Import backends lazily to avoid circular imports
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
    from jmwallet.backends.neutrino import NeutrinoBackend

    backend_type = backend_settings.backend_type

    backend: DescriptorWalletBackend | NeutrinoBackend
    if backend_type == "neutrino":
        backend = NeutrinoBackend(
            neutrino_url=backend_settings.neutrino_url,
            network=backend_settings.bitcoin_network,
            scan_start_height=backend_settings.scan_start_height,
            add_peers=backend_settings.neutrino_add_peers,
            tls_cert_path=backend_settings.neutrino_tls_cert,
            auth_token=backend_settings.neutrino_auth_token,
            fee_estimate_url=backend_settings.fee_estimate_url,
            fee_estimate_proxy=backend_settings.fee_estimate_proxy,
        )
    elif backend_type == "descriptor_wallet":
        if not wallet_name:
            raise ValueError("wallet_name required for descriptor_wallet backend")
        backend = DescriptorWalletBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
            wallet_name=wallet_name,
        )
    else:
        raise ValueError(
            f"Invalid backend type: {backend_type}. Valid options: descriptor_wallet, neutrino"
        )

    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

    return backend


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
