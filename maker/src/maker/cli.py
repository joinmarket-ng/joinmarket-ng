"""
Maker bot CLI using Typer.

Configuration is loaded with the following priority (highest to lowest):
1. CLI arguments
2. Environment variables
3. Config file (~/.joinmarket-ng/config.toml)
4. Built-in defaults
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer
from jmcore.cli_common import resolve_mnemonic, setup_cli
from jmcore.config import TorControlConfig
from jmcore.models import NetworkType, OfferType
from jmcore.notifications import get_notifier
from jmcore.paths import remove_nick_state, write_nick_state
from jmcore.settings import (
    JoinMarketSettings,
    ensure_config_file,
)
from jmwallet.wallet.service import WalletService
from loguru import logger
from pydantic import SecretStr

from maker.bot import MakerBot
from maker.config import MakerConfig, MergeAlgorithm, OfferConfig

app = typer.Typer(add_completion=False)


def run_async(coro: Any) -> Any:
    return asyncio.run(coro)


def build_maker_config(
    settings: JoinMarketSettings,
    mnemonic: str,
    passphrase: str,
    # CLI overrides (None means use settings value)
    network: NetworkType | None = None,
    bitcoin_network: NetworkType | None = None,
    data_dir: Path | None = None,
    backend_type: str | None = None,
    rpc_url: str | None = None,
    rpc_user: str | None = None,
    rpc_password: str | None = None,
    neutrino_url: str | None = None,
    directory_servers: str | None = None,
    tor_socks_host: str | None = None,
    tor_socks_port: int | None = None,
    tor_control_host: str | None = None,
    tor_control_port: int | None = None,
    tor_cookie_path: Path | None = None,
    disable_tor_control: bool = False,
    onion_serving_host: str | None = None,
    onion_serving_port: int | None = None,
    tor_target_host: str | None = None,
    min_size: int | None = None,
    cj_fee_relative: str | None = None,
    cj_fee_absolute: int | None = None,
    tx_fee_contribution: int | None = None,
    merge_algorithm: str | None = None,
    fidelity_bond_locktimes: list[int] | None = None,
    fidelity_bond_index: int | None = None,
    no_fidelity_bond: bool = False,
    dual_offers: bool = False,
) -> MakerConfig:
    """
    Build MakerConfig from unified settings with CLI overrides.

    CLI arguments (when not None) override settings from config file and env vars.
    """
    # Resolve network settings
    effective_network = network if network is not None else settings.network_config.network
    effective_bitcoin_network = (
        bitcoin_network
        if bitcoin_network is not None
        else settings.network_config.bitcoin_network or effective_network
    )
    effective_data_dir = data_dir if data_dir is not None else settings.get_data_dir()

    # Resolve backend settings
    effective_backend_type = (
        backend_type if backend_type is not None else settings.bitcoin.backend_type
    )
    effective_rpc_url = rpc_url if rpc_url is not None else settings.bitcoin.rpc_url
    effective_rpc_user = rpc_user if rpc_user is not None else settings.bitcoin.rpc_user
    effective_rpc_password = (
        rpc_password
        if rpc_password is not None
        else settings.bitcoin.rpc_password.get_secret_value()
    )
    effective_neutrino_url = (
        neutrino_url if neutrino_url is not None else settings.bitcoin.neutrino_url
    )

    # Build backend config
    backend_config: dict[str, Any] = {}
    if effective_backend_type in ("scantxoutset", "descriptor_wallet"):
        backend_config = {
            "rpc_url": effective_rpc_url,
            "rpc_user": effective_rpc_user,
            "rpc_password": effective_rpc_password,
        }
    elif effective_backend_type == "neutrino":
        backend_config = {
            "neutrino_url": effective_neutrino_url,
            "network": (
                effective_bitcoin_network.value
                if hasattr(effective_bitcoin_network, "value")
                else str(effective_bitcoin_network)
            ),
            "scan_start_height": settings.wallet.scan_start_height,
            "connect_peers": settings.get_neutrino_connect_peers(),
        }

    # Resolve directory servers
    # If CLI provides directory servers, use those
    # Otherwise, if network was overridden via CLI, use defaults for that network
    # Otherwise, use settings (which may have custom servers or default for settings network)
    if directory_servers:
        dir_servers = [s.strip() for s in directory_servers.split(",")]
    elif settings.network_config.directory_servers:
        dir_servers = settings.network_config.directory_servers
    elif network is not None:
        # Network was overridden via CLI, get defaults for that network
        from jmcore.settings import DEFAULT_DIRECTORY_SERVERS

        dir_servers = DEFAULT_DIRECTORY_SERVERS.get(effective_network.value, [])
    else:
        dir_servers = settings.get_directory_servers()

    # Resolve Tor settings
    effective_socks_host = tor_socks_host if tor_socks_host is not None else settings.tor.socks_host
    effective_socks_port = tor_socks_port if tor_socks_port is not None else settings.tor.socks_port

    # Resolve Tor control settings
    if disable_tor_control:
        tor_control_cfg = TorControlConfig(enabled=False)
    else:
        # tor_control host defaults to tor.socks_host
        effective_control_host = (
            tor_control_host if tor_control_host is not None else settings.tor.control_host
        )
        effective_control_port = (
            tor_control_port if tor_control_port is not None else settings.tor.control_port
        )
        effective_cookie_path = None
        if tor_cookie_path is not None:
            effective_cookie_path = tor_cookie_path
        elif settings.tor.cookie_path:
            effective_cookie_path = Path(settings.tor.cookie_path)

        tor_control_cfg = TorControlConfig(
            enabled=settings.tor.control_enabled,
            host=effective_control_host,
            port=effective_control_port,
            cookie_path=effective_cookie_path,
            password=settings.tor.password if settings.tor.password else None,
        )

    # Resolve maker-specific settings
    effective_onion_host = (
        onion_serving_host if onion_serving_host is not None else settings.maker.onion_serving_host
    )
    effective_onion_port = (
        onion_serving_port if onion_serving_port is not None else settings.maker.onion_serving_port
    )
    effective_target_host = (
        tor_target_host if tor_target_host is not None else settings.tor.target_host
    )
    effective_min_size = min_size if min_size is not None else settings.maker.min_size
    effective_tx_fee = (
        tx_fee_contribution
        if tx_fee_contribution is not None
        else settings.maker.tx_fee_contribution
    )

    # Determine offer type and fee values
    # CLI explicit values take precedence
    offer_configs: list[OfferConfig] = []

    if dual_offers:
        # Create both relative and absolute offers
        # Use CLI values if provided, otherwise use settings
        rel_fee = cj_fee_relative if cj_fee_relative is not None else settings.maker.cj_fee_relative
        abs_fee = cj_fee_absolute if cj_fee_absolute is not None else settings.maker.cj_fee_absolute
        tx_fee = (
            tx_fee_contribution
            if tx_fee_contribution is not None
            else settings.maker.tx_fee_contribution
        )
        min_sz = min_size if min_size is not None else settings.maker.min_size

        offer_configs = [
            OfferConfig(
                offer_type=OfferType.SW0_RELATIVE,
                min_size=min_sz,
                cj_fee_relative=rel_fee,
                cj_fee_absolute=abs_fee,
                tx_fee_contribution=tx_fee,
            ),
            OfferConfig(
                offer_type=OfferType.SW0_ABSOLUTE,
                min_size=min_sz,
                cj_fee_relative=rel_fee,
                cj_fee_absolute=abs_fee,
                tx_fee_contribution=tx_fee,
            ),
        ]
        # Set dummy values for legacy fields (they won't be used)
        parsed_offer_type = OfferType.SW0_RELATIVE
        actual_cj_fee_relative = rel_fee
        actual_cj_fee_absolute = abs_fee
    elif cj_fee_relative is not None and cj_fee_absolute is not None:
        raise ValueError(
            "Cannot specify both --cj-fee-relative and --cj-fee-absolute. "
            "Use --dual-offers to create both offer types, or use only one fee option."
        )
    elif cj_fee_absolute is not None:
        # User explicitly set absolute fee via CLI
        parsed_offer_type = OfferType.SW0_ABSOLUTE
        actual_cj_fee_relative = settings.maker.cj_fee_relative
        actual_cj_fee_absolute = cj_fee_absolute
    elif cj_fee_relative is not None:
        # User explicitly set relative fee via CLI
        parsed_offer_type = OfferType.SW0_RELATIVE
        actual_cj_fee_relative = cj_fee_relative
        actual_cj_fee_absolute = settings.maker.cj_fee_absolute
    else:
        # Use settings values (from config file or defaults)
        # Parse offer_type from settings
        try:
            parsed_offer_type = OfferType(settings.maker.offer_type)
        except ValueError:
            raise ValueError(
                f"Invalid offer_type in config: {settings.maker.offer_type}. "
                "Must be one of: sw0reloffer, sw0absoffer, swreloffer, swabsoffer"
            )
        actual_cj_fee_relative = settings.maker.cj_fee_relative
        actual_cj_fee_absolute = settings.maker.cj_fee_absolute

    # Parse merge algorithm
    effective_merge_algorithm_str = (
        merge_algorithm if merge_algorithm is not None else settings.maker.merge_algorithm
    )
    try:
        parsed_merge_algorithm = MergeAlgorithm(effective_merge_algorithm_str.lower())
    except ValueError:
        raise ValueError(
            f"Invalid merge algorithm: {effective_merge_algorithm_str}. "
            "Must be one of: default, gradual, greedy, random"
        )

    # Log offer configuration for clarity
    if offer_configs:
        # Dual offers mode
        logger.info(f"Dual offers mode: creating {len(offer_configs)} offers")
        for i, oc in enumerate(offer_configs):
            fee_str = (
                f"rel={oc.cj_fee_relative}"
                if oc.offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE)
                else f"abs={oc.cj_fee_absolute} sats"
            )
            logger.info(f"  Offer {i}: type={oc.offer_type.value}, {fee_str}")
    else:
        # Single offer mode
        fee_str = (
            f"relative fee={actual_cj_fee_relative} ({float(actual_cj_fee_relative) * 100:.4f}%)"
            if parsed_offer_type in (OfferType.SW0_RELATIVE, OfferType.SWA_RELATIVE)
            else f"absolute fee={actual_cj_fee_absolute} sats"
        )
        logger.info(f"Offer config: type={parsed_offer_type.value}, {fee_str}")

    # Fidelity bond settings
    effective_locktimes = fidelity_bond_locktimes if fidelity_bond_locktimes else []
    effective_bond_index = fidelity_bond_index

    # Validate: no_fidelity_bond is mutually exclusive with other bond options
    if no_fidelity_bond and (effective_locktimes or effective_bond_index is not None):
        raise ValueError(
            "--no-fidelity-bond cannot be combined with "
            "--fidelity-bond-locktime or --fidelity-bond-index"
        )

    # Validate fidelity bond index requires locktimes
    if effective_bond_index is not None and not effective_locktimes:
        raise ValueError(
            "When using --fidelity-bond-index, you must also specify at least one "
            "--fidelity-bond-locktime"
        )

    return MakerConfig(
        mnemonic=SecretStr(mnemonic),
        passphrase=SecretStr(passphrase),
        network=effective_network,
        bitcoin_network=effective_bitcoin_network,
        data_dir=effective_data_dir,
        backend_type=effective_backend_type,
        backend_config=backend_config,
        directory_servers=dir_servers,
        socks_host=effective_socks_host,
        socks_port=effective_socks_port,
        stream_isolation=settings.tor.stream_isolation,
        connection_timeout=settings.tor.connection_timeout,
        mixdepth_count=settings.wallet.mixdepth_count,
        gap_limit=settings.wallet.gap_limit,
        dust_threshold=settings.wallet.dust_threshold,
        smart_scan=settings.wallet.smart_scan,
        background_full_rescan=settings.wallet.background_full_rescan,
        scan_lookback_blocks=settings.wallet.scan_lookback_blocks,
        tor_control=tor_control_cfg,
        onion_serving_host=effective_onion_host,
        onion_serving_port=effective_onion_port,
        tor_target_host=effective_target_host,
        min_size=effective_min_size,
        offer_type=parsed_offer_type,
        cj_fee_relative=actual_cj_fee_relative,
        cj_fee_absolute=actual_cj_fee_absolute,
        tx_fee_contribution=effective_tx_fee,
        min_confirmations=settings.maker.min_confirmations,
        session_timeout_sec=settings.maker.session_timeout_sec,
        pending_tx_timeout_min=settings.maker.pending_tx_timeout_min,
        rescan_interval_sec=settings.maker.rescan_interval_sec,
        message_rate_limit=settings.maker.message_rate_limit,
        message_burst_limit=settings.maker.message_burst_limit,
        fidelity_bond_locktimes=list(effective_locktimes),
        fidelity_bond_index=effective_bond_index,
        no_fidelity_bond=no_fidelity_bond,
        merge_algorithm=parsed_merge_algorithm,
        offer_configs=offer_configs,
    )


def create_wallet_service(config: MakerConfig) -> WalletService:
    backend_type = config.backend_type.lower()
    # Use bitcoin_network for address generation (bcrt1 vs tb1 vs bc1)
    bitcoin_network = config.bitcoin_network or config.network

    from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend

    backend: BitcoinCoreBackend | DescriptorWalletBackend | NeutrinoBackend
    if backend_type == "descriptor_wallet":
        backend_cfg = config.backend_config
        fingerprint = get_mnemonic_fingerprint(
            config.mnemonic.get_secret_value(), config.passphrase.get_secret_value() or ""
        )
        # Convert NetworkType enum to string value
        network_str = (
            bitcoin_network.value if hasattr(bitcoin_network, "value") else str(bitcoin_network)
        )
        wallet_name = generate_wallet_name(fingerprint, network_str)
        backend = DescriptorWalletBackend(
            rpc_url=backend_cfg.get("rpc_url", "http://127.0.0.1:8332"),
            rpc_user=backend_cfg.get("rpc_user", ""),
            rpc_password=backend_cfg.get("rpc_password", ""),
            wallet_name=wallet_name,
        )
    elif backend_type == "scantxoutset":
        backend_cfg = config.backend_config
        backend = BitcoinCoreBackend(
            rpc_url=backend_cfg.get("rpc_url", "http://127.0.0.1:8332"),
            rpc_user=backend_cfg.get("rpc_user", ""),
            rpc_password=backend_cfg.get("rpc_password", ""),
        )
    elif backend_type == "neutrino":
        backend_cfg = config.backend_config
        backend = NeutrinoBackend(
            neutrino_url=backend_cfg.get("neutrino_url", "http://127.0.0.1:8334"),
            network=bitcoin_network.value,
            connect_peers=backend_cfg.get("connect_peers", []),
            data_dir=backend_cfg.get("data_dir", "/data/neutrino"),
            scan_start_height=backend_cfg.get("scan_start_height"),
        )
    else:
        raise typer.BadParameter(f"Unsupported backend: {backend_type}")

    wallet = WalletService(
        mnemonic=config.mnemonic.get_secret_value(),
        backend=backend,
        network=bitcoin_network.value,
        mixdepth_count=config.mixdepth_count,
        gap_limit=config.gap_limit,
        passphrase=config.passphrase.get_secret_value(),
        data_dir=config.data_dir,
    )
    return wallet


# Use a sentinel value for CLI defaults to distinguish "not provided" from explicit values
# This allows us to know when to use settings vs CLI override
_NOT_PROVIDED = object()


@app.command()
def start(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option(
            "--prompt-bip39-passphrase",
            help="Prompt for BIP39 passphrase interactively",
        ),
    ] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            "-d",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory for JoinMarket files. Defaults to ~/.joinmarket-ng",
        ),
    ] = None,
    network: Annotated[
        NetworkType | None,
        typer.Option(
            case_sensitive=False,
            help="Protocol network (mainnet, testnet, signet, regtest)",
        ),
    ] = None,
    bitcoin_network: Annotated[
        NetworkType | None,
        typer.Option(
            case_sensitive=False,
            help="Bitcoin network for address generation (defaults to --network)",
        ),
    ] = None,
    backend_type: Annotated[
        str | None,
        typer.Option(help="Backend type: scantxoutset | descriptor_wallet | neutrino"),
    ] = None,
    rpc_url: Annotated[
        str | None, typer.Option(envvar="BITCOIN_RPC_URL", help="Bitcoin full node RPC URL")
    ] = None,
    neutrino_url: Annotated[
        str | None, typer.Option(envvar="NEUTRINO_URL", help="Neutrino REST API URL")
    ] = None,
    min_size: Annotated[int | None, typer.Option(help="Minimum CoinJoin size in sats")] = None,
    cj_fee_relative: Annotated[
        str | None,
        typer.Option(
            help="Relative coinjoin fee (e.g., 0.001 = 0.1%)",
            envvar="CJ_FEE_RELATIVE",
        ),
    ] = None,
    cj_fee_absolute: Annotated[
        int | None,
        typer.Option(
            help="Absolute coinjoin fee in sats. Mutually exclusive with --cj-fee-relative.",
            envvar="CJ_FEE_ABSOLUTE",
        ),
    ] = None,
    tx_fee_contribution: Annotated[
        int | None, typer.Option(help="Tx fee contribution in sats")
    ] = None,
    directory_servers: Annotated[
        str | None,
        typer.Option(
            "--directory",
            "-D",
            envvar="DIRECTORY_SERVERS",
            help="Directory servers (comma-separated host:port)",
        ),
    ] = None,
    tor_socks_host: Annotated[
        str | None, typer.Option(help="Tor SOCKS proxy host (overrides TOR__SOCKS_HOST)")
    ] = None,
    tor_socks_port: Annotated[
        int | None, typer.Option(help="Tor SOCKS proxy port (overrides TOR__SOCKS_PORT)")
    ] = None,
    tor_control_host: Annotated[
        str | None,
        typer.Option(
            help="Tor control port host (overrides TOR__CONTROL_HOST)",
        ),
    ] = None,
    tor_control_port: Annotated[
        int | None, typer.Option(help="Tor control port (overrides TOR__CONTROL_PORT)")
    ] = None,
    tor_cookie_path: Annotated[
        Path | None,
        typer.Option(
            help="Path to Tor cookie auth file (overrides TOR__COOKIE_PATH)",
        ),
    ] = None,
    disable_tor_control: Annotated[
        bool,
        typer.Option(
            "--disable-tor-control",
            help="Disable Tor control port integration",
        ),
    ] = False,
    onion_serving_host: Annotated[
        str | None,
        typer.Option(
            help="Bind address for incoming connections (overrides MAKER__ONION_SERVING_HOST)",
        ),
    ] = None,
    onion_serving_port: Annotated[
        int | None,
        typer.Option(
            help="Port for incoming .onion connections (overrides MAKER__ONION_SERVING_PORT)",
        ),
    ] = None,
    tor_target_host: Annotated[
        str | None,
        typer.Option(
            help="Target hostname for Tor hidden service (overrides TOR__TARGET_HOST)",
        ),
    ] = None,
    fidelity_bond_locktimes: Annotated[
        list[int],
        typer.Option("--fidelity-bond-locktime", "-L", help="Fidelity bond locktimes to scan for"),
    ] = [],  # noqa: B006
    fidelity_bond_index: Annotated[
        int | None,
        typer.Option(
            "--fidelity-bond-index",
            "-I",
            envvar="FIDELITY_BOND_INDEX",
            help="Fidelity bond derivation index",
        ),
    ] = None,
    fidelity_bond: Annotated[
        str | None,
        typer.Option(
            "--fidelity-bond",
            "-B",
            help="Specific fidelity bond to use (format: txid:vout)",
        ),
    ] = None,
    no_fidelity_bond: Annotated[
        bool,
        typer.Option(
            "--no-fidelity-bond",
            help="Disable fidelity bond usage. Skips registry lookup and bond proof generation "
            "even when bonds exist in the registry.",
        ),
    ] = False,
    merge_algorithm: Annotated[
        str | None,
        typer.Option(
            "--merge-algorithm",
            "-M",
            envvar="MERGE_ALGORITHM",
            help="UTXO selection strategy: default, gradual, greedy, random",
        ),
    ] = None,
    dual_offers: Annotated[
        bool,
        typer.Option(
            "--dual-offers",
            help=(
                "Create both relative and absolute fee offers simultaneously. "
                "Each offer gets a unique ID (0 for relative, 1 for absolute). "
                "Use with --cj-fee-relative and --cj-fee-absolute to set fees for each."
            ),
        ),
    ] = False,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """
    Start the maker bot.

    Configuration is loaded from ~/.joinmarket-ng/config.toml (or $JOINMARKET_DATA_DIR/config.toml),
    environment variables, and CLI arguments. CLI arguments have the highest priority.
    """
    # Load settings (log_level=None means use settings.logging.level)
    settings = setup_cli(log_level)

    # Ensure config file exists (creates template if not)
    ensure_config_file(settings.get_data_dir())

    # Load mnemonic using unified resolver
    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        resolved_mnemonic = resolved.mnemonic if resolved else ""
        resolved_passphrase = resolved.bip39_passphrase if resolved else ""
    except (ValueError, FileNotFoundError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Build MakerConfig with CLI overrides
    try:
        config = build_maker_config(
            settings=settings,
            mnemonic=resolved_mnemonic,
            passphrase=resolved_passphrase,
            network=network,
            bitcoin_network=bitcoin_network,
            data_dir=data_dir,
            backend_type=backend_type,
            rpc_url=rpc_url,
            neutrino_url=neutrino_url,
            directory_servers=directory_servers,
            tor_socks_host=tor_socks_host,
            tor_socks_port=tor_socks_port,
            tor_control_host=tor_control_host,
            tor_control_port=tor_control_port,
            tor_cookie_path=tor_cookie_path,
            disable_tor_control=disable_tor_control,
            onion_serving_host=onion_serving_host,
            onion_serving_port=onion_serving_port,
            tor_target_host=tor_target_host,
            min_size=min_size,
            cj_fee_relative=cj_fee_relative,
            cj_fee_absolute=cj_fee_absolute,
            tx_fee_contribution=tx_fee_contribution,
            merge_algorithm=merge_algorithm,
            fidelity_bond_locktimes=fidelity_bond_locktimes if fidelity_bond_locktimes else None,
            fidelity_bond_index=fidelity_bond_index,
            no_fidelity_bond=no_fidelity_bond,
            dual_offers=dual_offers,
        )
    except ValueError as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Log configuration source
    logger.info(f"Using network: {config.network.value}")
    logger.info(f"Using backend: {config.backend_type}")
    logger.info(f"Tor SOCKS: {config.socks_host}:{config.socks_port}")
    logger.info(f"Directory servers: {len(config.directory_servers)} configured")

    wallet = create_wallet_service(config)
    bot = MakerBot(wallet, wallet.backend, config)

    # Store the specific fidelity bond selection if provided
    if fidelity_bond and no_fidelity_bond:
        logger.error("--fidelity-bond and --no-fidelity-bond are mutually exclusive")
        raise typer.Exit(1)

    if fidelity_bond:
        try:
            parts = fidelity_bond.split(":")
            if len(parts) != 2:
                raise ValueError("Invalid format")
            config.selected_fidelity_bond = (parts[0], int(parts[1]))
            logger.info(f"Using specified fidelity bond: {fidelity_bond}")
        except (ValueError, IndexError):
            logger.error(f"Invalid fidelity bond format: {fidelity_bond}. Use txid:vout")
            raise typer.Exit(1)

    async def run_bot() -> None:
        try:
            # Write nick state file for external tracking and cross-component protection
            nick = bot.nick
            data_dir = config.data_dir
            write_nick_state(data_dir, "maker", nick)
            logger.info(f"Nick state written to {data_dir}/state/maker.nick")

            # Send startup notification immediately (including nick)
            notifier = get_notifier(settings, component_name="Maker")
            await notifier.notify_startup(
                component="Maker",
                network=config.network.value,
                nick=nick,
            )
            await bot.start()
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            # Clean up nick state file on shutdown
            remove_nick_state(config.data_dir, "maker")
            await bot.stop()

    try:
        run_async(run_bot())
    except KeyboardInterrupt:
        logger.info("Shutting down maker bot...")
        run_async(bot.stop())


@app.command()
def generate_address(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option(
            "--prompt-bip39-passphrase",
            help="Prompt for BIP39 passphrase interactively",
        ),
    ] = False,
    network: Annotated[
        NetworkType | None,
        typer.Option(case_sensitive=False, help="Protocol network"),
    ] = None,
    bitcoin_network: Annotated[
        NetworkType | None,
        typer.Option(
            case_sensitive=False,
            help="Bitcoin network for address generation (defaults to --network)",
        ),
    ] = None,
    backend_type: Annotated[str | None, typer.Option(help="Backend type")] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """Generate a new receive address."""
    # Load settings (log_level=None means use settings.logging.level)
    settings = setup_cli(log_level)

    # Load mnemonic using unified resolver
    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        resolved_mnemonic = resolved.mnemonic if resolved else ""
        resolved_passphrase = resolved.bip39_passphrase if resolved else ""
    except (ValueError, FileNotFoundError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Build config with CLI overrides
    try:
        config = build_maker_config(
            settings=settings,
            mnemonic=resolved_mnemonic,
            passphrase=resolved_passphrase,
            network=network,
            bitcoin_network=bitcoin_network,
            backend_type=backend_type,
        )
    except ValueError as e:
        logger.error(str(e))
        raise typer.Exit(1)

    wallet = create_wallet_service(config)
    address = wallet.get_receive_address(0, 0)
    typer.echo(address)


@app.command()
def config_init(
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            "-d",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory for JoinMarket files",
        ),
    ] = None,
) -> None:
    """Initialize the config file with default settings."""
    # Determine data directory
    from jmcore.paths import get_default_data_dir
    from jmcore.settings import reset_settings

    reset_settings()

    if data_dir is None:
        data_dir = get_default_data_dir()

    config_path = ensure_config_file(data_dir)
    typer.echo(f"Config file created at: {config_path}")
    typer.echo("\nAll settings are commented out by default.")
    typer.echo("Edit the file to customize your configuration.")
    typer.echo("\nPriority (highest to lowest):")
    typer.echo("  1. CLI arguments")
    typer.echo("  2. Environment variables")
    typer.echo("  3. Config file")
    typer.echo("  4. Built-in defaults")


def main() -> None:  # pragma: no cover
    app()
