"""
Command-line interface for JoinMarket Taker.

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
from jmcore.models import NetworkType
from jmcore.notifications import get_notifier
from jmcore.paths import remove_nick_state, write_nick_state
from jmcore.settings import (
    DEFAULT_DIRECTORY_SERVERS,
    JoinMarketSettings,
    ensure_config_file,
)
from jmwallet.wallet.service import WalletService
from loguru import logger

from taker.config import BroadcastPolicy, MaxCjFee, Schedule, ScheduleEntry, TakerConfig

app = typer.Typer(
    name="jm-taker",
    help="JoinMarket Taker - Execute CoinJoin transactions",
    add_completion=False,
)


def build_taker_config(
    settings: JoinMarketSettings,
    mnemonic: str,
    passphrase: str,
    # CoinJoin specific settings
    amount: int = 0,
    destination: str = "",
    mixdepth: int = 0,
    counterparties: int | None = None,
    select_utxos: bool = False,
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
    max_abs_fee: int | None = None,
    max_rel_fee: str | None = None,
    fee_rate: float | None = None,
    block_target: int | None = None,
    address_type: str | None = None,
    bondless_makers_allowance: float | None = None,
    bond_value_exponent: float | None = None,
    bondless_require_zero_fee: bool | None = None,
) -> TakerConfig:
    """
    Build TakerConfig from unified settings with CLI overrides.

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
            "add_peers": settings.get_neutrino_add_peers(),
        }

    # Resolve directory servers
    if directory_servers:
        dir_servers = [s.strip() for s in directory_servers.split(",")]
    elif settings.network_config.directory_servers:
        dir_servers = settings.network_config.directory_servers
    elif network is not None:
        # Network was overridden via CLI, get defaults for that network
        dir_servers = DEFAULT_DIRECTORY_SERVERS.get(effective_network.value, [])
    else:
        dir_servers = settings.get_directory_servers()

    # Resolve Tor settings
    effective_socks_host = tor_socks_host if tor_socks_host is not None else settings.tor.socks_host
    effective_socks_port = tor_socks_port if tor_socks_port is not None else settings.tor.socks_port

    # Resolve taker-specific settings
    effective_counterparties = (
        counterparties if counterparties is not None else settings.taker.counterparty_count
    )
    effective_max_abs_fee = (
        max_abs_fee if max_abs_fee is not None else settings.taker.max_cj_fee_abs
    )
    effective_max_rel_fee = (
        max_rel_fee if max_rel_fee is not None else settings.taker.max_cj_fee_rel
    )
    # Only set fee_block_target when fee_rate is not provided (they are mutually exclusive)
    effective_block_target: int | None = None
    if fee_rate is None:
        effective_block_target = (
            block_target
            if block_target is not None
            else (
                settings.taker.fee_block_target
                if settings.taker.fee_block_target is not None
                else settings.wallet.default_fee_block_target
            )
        )
    effective_bondless = (
        bondless_makers_allowance
        if bondless_makers_allowance is not None
        else settings.taker.bondless_makers_allowance
    )
    effective_bond_exp = (
        bond_value_exponent
        if bond_value_exponent is not None
        else settings.taker.bond_value_exponent
    )
    effective_bondless_zero_fee = (
        bondless_require_zero_fee
        if bondless_require_zero_fee is not None
        else settings.taker.bondless_require_zero_fee
    )

    # Parse broadcast policy
    try:
        broadcast_policy = BroadcastPolicy(settings.taker.tx_broadcast)
    except ValueError:
        broadcast_policy = BroadcastPolicy.MULTIPLE_PEERS

    # Import SecretStr for wrapping sensitive values
    from pydantic import SecretStr

    return TakerConfig(
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
        destination_address=SecretStr(destination),
        amount=amount,
        mixdepth=mixdepth,
        counterparty_count=effective_counterparties,
        max_cj_fee=MaxCjFee(abs_fee=effective_max_abs_fee, rel_fee=effective_max_rel_fee),
        tx_fee_factor=settings.taker.tx_fee_factor,
        fee_rate=fee_rate,  # CLI only, no settings equivalent
        fee_block_target=effective_block_target,
        bondless_makers_allowance=effective_bondless,
        bond_value_exponent=effective_bond_exp,
        bondless_makers_allowance_require_zero_fee=effective_bondless_zero_fee,
        maker_timeout_sec=settings.taker.maker_timeout_sec,
        order_wait_time=settings.taker.order_wait_time,
        tx_broadcast=broadcast_policy,
        broadcast_peer_count=settings.taker.broadcast_peer_count,
        minimum_makers=settings.taker.minimum_makers,
        rescan_interval_sec=settings.taker.rescan_interval_sec,
        select_utxos=select_utxos,
        address_type=address_type if address_type is not None else settings.wallet.address_type,
    )


def create_backend(config: TakerConfig) -> Any:
    """Create appropriate backend based on config."""
    bitcoin_network = config.bitcoin_network or config.network

    from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend

    if config.backend_type == "neutrino":
        return NeutrinoBackend(
            neutrino_url=config.backend_config.get("neutrino_url", "http://127.0.0.1:8334"),
            network=bitcoin_network.value,
            scan_start_height=config.backend_config.get("scan_start_height"),
            add_peers=config.backend_config.get("add_peers", []),
        )
    elif config.backend_type == "descriptor_wallet":
        fingerprint = get_mnemonic_fingerprint(
            config.mnemonic.get_secret_value(), config.passphrase.get_secret_value() or ""
        )
        wallet_name = generate_wallet_name(fingerprint, bitcoin_network.value, config.address_type)
        return DescriptorWalletBackend(
            rpc_url=config.backend_config["rpc_url"],
            rpc_user=config.backend_config["rpc_user"],
            rpc_password=config.backend_config["rpc_password"],
            wallet_name=wallet_name,
        )
    else:  # scantxoutset
        return BitcoinCoreBackend(
            rpc_url=config.backend_config["rpc_url"],
            rpc_user=config.backend_config["rpc_user"],
            rpc_password=config.backend_config["rpc_password"],
        )


@app.command()
def coinjoin(
    amount: Annotated[int, typer.Option("--amount", "-a", help="Amount in sats (0 for sweep)")],
    destination: Annotated[
        str,
        typer.Option(
            "--destination",
            "-d",
            help="Destination address (or 'INTERNAL' for next mixdepth)",
        ),
    ] = "INTERNAL",
    mixdepth: Annotated[int, typer.Option("--mixdepth", "-m", help="Source mixdepth")] = 0,
    counterparties: Annotated[
        int | None, typer.Option("--counterparties", "-n", help="Number of makers")
    ] = None,
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
        typer.Option("--network", case_sensitive=False, help="Protocol network for handshakes"),
    ] = None,
    bitcoin_network: Annotated[
        NetworkType | None,
        typer.Option(
            "--bitcoin-network",
            case_sensitive=False,
            help="Bitcoin network for addresses (defaults to --network)",
        ),
    ] = None,
    backend_type: Annotated[
        str | None,
        typer.Option(
            "--backend", "-b", help="Backend type: scantxoutset | descriptor_wallet | neutrino"
        ),
    ] = None,
    rpc_url: Annotated[
        str | None,
        typer.Option(
            "--rpc-url",
            envvar="BITCOIN_RPC_URL",
            help="Bitcoin full node RPC URL",
        ),
    ] = None,
    neutrino_url: Annotated[
        str | None,
        typer.Option(
            "--neutrino-url",
            envvar="NEUTRINO_URL",
            help="Neutrino REST API URL",
        ),
    ] = None,
    directory_servers: Annotated[
        str | None,
        typer.Option(
            "--directory",
            "-D",
            envvar="DIRECTORY_SERVERS",
            help="Directory servers (comma-separated)",
        ),
    ] = None,
    tor_socks_host: Annotated[
        str | None, typer.Option(help="Tor SOCKS proxy host (overrides TOR__SOCKS_HOST)")
    ] = None,
    tor_socks_port: Annotated[
        int | None, typer.Option(help="Tor SOCKS proxy port (overrides TOR__SOCKS_PORT)")
    ] = None,
    max_abs_fee: Annotated[
        int | None, typer.Option("--max-abs-fee", help="Max absolute fee in sats")
    ] = None,
    max_rel_fee: Annotated[
        str | None, typer.Option("--max-rel-fee", help="Max relative fee (0.001=0.1%)")
    ] = None,
    fee_rate: Annotated[
        float | None,
        typer.Option(
            "--fee-rate",
            help="Manual fee rate in sat/vB. Mutually exclusive with --block-target.",
        ),
    ] = None,
    block_target: Annotated[
        int | None,
        typer.Option(
            "--block-target",
            help="Target blocks for fee estimation (1-1008). Cannot be used with neutrino.",
        ),
    ] = None,
    bondless_makers_allowance: Annotated[
        float | None,
        typer.Option(
            "--bondless-allowance",
            envvar="BONDLESS_MAKERS_ALLOWANCE",
            help="Fraction of time to choose makers randomly (0.0-1.0)",
        ),
    ] = None,
    bond_value_exponent: Annotated[
        float | None,
        typer.Option(
            "--bond-exponent",
            envvar="BOND_VALUE_EXPONENT",
            help="Exponent for fidelity bond value calculation",
        ),
    ] = None,
    bondless_require_zero_fee: Annotated[
        bool | None,
        typer.Option(
            "--bondless-zero-fee/--no-bondless-zero-fee",
            envvar="BONDLESS_REQUIRE_ZERO_FEE",
            help="For bondless spots, require zero absolute fee",
        ),
    ] = None,
    select_utxos: Annotated[
        bool,
        typer.Option(
            "--select-utxos",
            "-s",
            help="Interactively select UTXOs (fzf-like TUI)",
        ),
    ] = False,
    address_type: Annotated[
        str, typer.Option("--address-type", "-A", help="Address type: p2wpkh (default) or p2tr")
    ] = "p2wpkh",
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """
    Execute a single CoinJoin transaction.

    Configuration is loaded from ~/.joinmarket-ng/config.toml (or $JOINMARKET_DATA_DIR/config.toml),
    environment variables, and CLI arguments. CLI arguments have the highest priority.
    """
    # Load settings (log_level=None means use settings.logging.level)
    settings = setup_cli(log_level)

    # Ensure config file exists
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

    # Build config with CLI overrides
    try:
        config = build_taker_config(
            settings=settings,
            mnemonic=resolved_mnemonic,
            passphrase=resolved_passphrase,
            amount=amount,
            destination=destination,
            mixdepth=mixdepth,
            counterparties=counterparties,
            select_utxos=select_utxos,
            network=network,
            bitcoin_network=bitcoin_network,
            backend_type=backend_type,
            rpc_url=rpc_url,
            neutrino_url=neutrino_url,
            directory_servers=directory_servers,
            tor_socks_host=tor_socks_host,
            tor_socks_port=tor_socks_port,
            max_abs_fee=max_abs_fee,
            max_rel_fee=max_rel_fee,
            fee_rate=fee_rate,
            block_target=block_target,
            address_type=address_type,
            bondless_makers_allowance=bondless_makers_allowance,
            bond_value_exponent=bond_value_exponent,
            bondless_require_zero_fee=bondless_require_zero_fee,
        )
    except ValueError as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Log configuration source
    logger.info(f"Using network: {config.network.value}")
    logger.info(f"Using backend: {config.backend_type}")
    logger.info(f"Tor SOCKS: {config.socks_host}:{config.socks_port}")

    try:
        asyncio.run(
            _run_coinjoin(
                settings,
                config,
                amount,
                destination,
                mixdepth,
                config.counterparty_count,
                address_type,
                yes,
            )
        )
    except RuntimeError as e:
        # Clean error for expected failures (e.g., connection failures)
        logger.error(f"CoinJoin failed: {e}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        raise typer.Exit(130)
    except Exception as e:
        # Unexpected errors - show full traceback
        logger.exception(f"Unexpected error: {e}")
        raise typer.Exit(1)


async def _run_coinjoin(
    settings: JoinMarketSettings,
    config: TakerConfig,
    amount: int,
    destination: str,
    mixdepth: int,
    counterparties: int,
    address_type: str,
    skip_confirmation: bool,
) -> None:
    """Run CoinJoin transaction."""
    from taker.taker import Taker

    bitcoin_network = config.bitcoin_network or config.network

    # Create backend
    backend = create_backend(config)

    # Verify backend connection
    if config.backend_type == "neutrino":
        logger.info("Verifying Neutrino connection...")
        try:
            synced = await backend.wait_for_sync(timeout=30.0)
            if not synced:
                logger.error("Neutrino connection failed: not synced")
                raise typer.Exit(1)
            logger.info("Neutrino connection verified")
        except Exception as e:
            logger.error(f"Failed to connect to Neutrino backend: {e}")
            raise typer.Exit(1)
    else:
        logger.info("Verifying Bitcoin Core RPC connection...")
        try:
            await backend.get_block_height()
            logger.info("Bitcoin Core RPC connection verified")
        except Exception as e:
            logger.error(f"Failed to connect to Bitcoin Core RPC: {e}")
            raise typer.Exit(1)

    # Create wallet
    wallet = WalletService(
        mnemonic=config.mnemonic.get_secret_value(),
        passphrase=config.passphrase.get_secret_value(),
        backend=backend,
        network=bitcoin_network.value,
        mixdepth_count=config.mixdepth_count,
        data_dir=config.data_dir,
        address_type=address_type,
    )

    # Create confirmation callback
    def confirmation_callback(
        maker_details: list[dict[str, Any]],
        cj_amount: int,
        total_fee: int,
        destination: str,
        mining_fee: int | None = None,
        fee_rate: float | None = None,
    ) -> bool:
        """Callback for user confirmation after maker selection."""
        from jmcore.confirmation import confirm_transaction, format_maker_summary

        additional_info = format_maker_summary(maker_details, fee_rate=fee_rate)
        additional_info["Source Mixdepth"] = mixdepth

        return confirm_transaction(
            operation="coinjoin",
            amount=cj_amount,
            destination=destination,
            fee=total_fee,
            mining_fee=mining_fee,
            additional_info=additional_info,
            skip_confirmation=skip_confirmation,
        )

    # Create taker
    taker = Taker(wallet, backend, config, confirmation_callback=confirmation_callback)

    try:
        # Write nick state file for external tracking and cross-component protection
        nick = taker.nick
        data_dir = config.data_dir
        write_nick_state(data_dir, "taker", nick)
        logger.info(f"Nick state written to {data_dir}/state/taker.nick")

        # Send startup notification (including nick)
        notifier = get_notifier(settings, component_name="Taker")
        await notifier.notify_startup(
            component="Taker (CoinJoin)",
            network=config.network.value,
            nick=nick,
        )

        # Sync wallet first (before connecting to directory servers)
        await taker.sync_wallet()

        # Early fund validation: check if mixdepth has sufficient funds
        # This avoids connecting to directory servers when funds are insufficient
        mixdepth_balance = await wallet.get_balance(mixdepth)
        if mixdepth_balance == 0:
            logger.error(f"No funds in mixdepth {mixdepth}")
            raise typer.Exit(1)

        if amount > 0 and mixdepth_balance < amount:
            logger.error(
                f"Insufficient funds in mixdepth {mixdepth}: "
                f"have {mixdepth_balance:,} sats, need at least {amount:,} sats"
            )
            raise typer.Exit(1)

        # Now connect to directory servers (funds are sufficient)
        await taker.connect()

        amount_display = "ALL (sweep)" if amount == 0 else f"{amount:,} sats"
        logger.info(f"Starting CoinJoin: {amount_display} -> {destination}")
        txid = await taker.do_coinjoin(
            amount=amount,
            destination=destination,
            mixdepth=mixdepth,
            counterparty_count=counterparties,
        )

        if txid:
            logger.info(f"CoinJoin successful! txid: {txid}")
        else:
            logger.error("CoinJoin failed")
            raise typer.Exit(1)

    finally:
        # Clean up nick state file on shutdown
        remove_nick_state(config.data_dir, "taker")
        await taker.stop()


@app.command()
def tumble(
    schedule_file: Annotated[Path, typer.Argument(help="Path to schedule JSON file")],
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
        typer.Option("--network", case_sensitive=False, help="Bitcoin network"),
    ] = None,
    backend_type: Annotated[
        str | None,
        typer.Option(
            "--backend", "-b", help="Backend type: scantxoutset | descriptor_wallet | neutrino"
        ),
    ] = None,
    rpc_url: Annotated[
        str | None,
        typer.Option(
            "--rpc-url",
            envvar="BITCOIN_RPC_URL",
            help="Bitcoin full node RPC URL",
        ),
    ] = None,
    neutrino_url: Annotated[
        str | None,
        typer.Option(
            "--neutrino-url",
            envvar="NEUTRINO_URL",
            help="Neutrino REST API URL",
        ),
    ] = None,
    directory_servers: Annotated[
        str | None,
        typer.Option(
            "--directory",
            "-D",
            envvar="DIRECTORY_SERVERS",
            help="Directory servers (comma-separated)",
        ),
    ] = None,
    tor_socks_host: Annotated[
        str | None, typer.Option(help="Tor SOCKS proxy host (overrides TOR__SOCKS_HOST)")
    ] = None,
    tor_socks_port: Annotated[
        int | None, typer.Option(help="Tor SOCKS proxy port (overrides TOR__SOCKS_PORT)")
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
    address_type: Annotated[
        str, typer.Option("--address-type", "-A", help="Address type: p2wpkh (default) or p2tr")
    ] = "p2wpkh",
) -> None:
    """
    Run a tumbler schedule of CoinJoins.

    Configuration is loaded from ~/.joinmarket-ng/config.toml, environment variables,
    and CLI arguments. CLI arguments have the highest priority.
    """
    # Load settings (log_level=None means use settings.logging.level)
    settings = setup_cli(log_level)

    # Ensure config file exists
    ensure_config_file(settings.get_data_dir())

    # Load mnemonic using unified resolver
    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        resolved_mnemonic = resolved.mnemonic if resolved else ""
        resolved_bip39_passphrase = resolved.bip39_passphrase if resolved else ""
    except (ValueError, FileNotFoundError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    if not schedule_file.exists():
        logger.error(f"Schedule file not found: {schedule_file}")
        raise typer.Exit(1)

    # Load schedule
    import json

    try:
        with open(schedule_file) as f:
            schedule_data = json.load(f)

        entries = [ScheduleEntry(**entry) for entry in schedule_data["entries"]]
        schedule = Schedule(entries=entries)
    except Exception as e:
        logger.error(f"Failed to load schedule: {e}")
        raise typer.Exit(1)

    # Build config with CLI overrides
    try:
        config = build_taker_config(
            settings=settings,
            mnemonic=resolved_mnemonic,
            passphrase=resolved_bip39_passphrase,
            network=network,
            backend_type=backend_type,
            rpc_url=rpc_url,
            neutrino_url=neutrino_url,
            directory_servers=directory_servers,
            tor_socks_host=tor_socks_host,
            tor_socks_port=tor_socks_port,
            address_type=address_type,
        )
    except ValueError as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Log configuration
    logger.info(f"Using network: {config.network.value}")
    logger.info(f"Using backend: {config.backend_type}")

    try:
        asyncio.run(_run_tumble(settings, config, schedule, address_type))
    except RuntimeError as e:
        # Clean error for expected failures (e.g., connection failures)
        logger.error(f"Tumble failed: {e}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        raise typer.Exit(130)
    except Exception as e:
        # Unexpected errors - show full traceback
        logger.exception(f"Unexpected error: {e}")
        raise typer.Exit(1)


async def _run_tumble(
    settings: JoinMarketSettings, config: TakerConfig, schedule: Schedule, address_type: str
) -> None:
    """Run tumbler schedule."""
    from taker.taker import Taker

    bitcoin_network = config.bitcoin_network or config.network

    # Create backend
    backend = create_backend(config)

    # Verify backend connection
    if config.backend_type == "neutrino":
        logger.info("Verifying Neutrino connection...")
        try:
            synced = await backend.wait_for_sync(timeout=30.0)
            if not synced:
                logger.error("Neutrino connection failed: not synced")
                raise typer.Exit(1)
            logger.info("Neutrino connection verified")
        except Exception as e:
            logger.error(f"Failed to connect to Neutrino backend: {e}")
            raise typer.Exit(1)
    else:
        logger.info("Verifying Bitcoin Core RPC connection...")
        try:
            await backend.get_block_height()
            logger.info("Bitcoin Core RPC connection verified")
        except Exception as e:
            logger.error(f"Failed to connect to Bitcoin Core RPC: {e}")
            raise typer.Exit(1)

    # Create wallet
    wallet = WalletService(
        mnemonic=config.mnemonic.get_secret_value(),
        passphrase=config.passphrase.get_secret_value(),
        backend=backend,
        network=bitcoin_network.value,
        mixdepth_count=config.mixdepth_count,
        data_dir=config.data_dir,
        address_type=address_type,
    )

    # Create taker
    taker = Taker(wallet, backend, config)

    try:
        # Write nick state file for external tracking and cross-component protection
        nick = taker.nick
        data_dir = config.data_dir
        write_nick_state(data_dir, "taker", nick)
        logger.info(f"Nick state written to {data_dir}/state/taker.nick")

        # Send startup notification (including nick)
        notifier = get_notifier(settings, component_name="Taker")
        await notifier.notify_startup(
            component="Taker (Tumble)",
            network=config.network.value,
            nick=nick,
        )
        await taker.start()

        logger.info(f"Starting tumble with {len(schedule.entries)} entries")
        success = await taker.run_schedule(schedule)

        if success:
            logger.info("Tumble complete!")
        else:
            logger.error("Tumble failed")
            raise typer.Exit(1)

    finally:
        # Clean up nick state file on shutdown
        remove_nick_state(config.data_dir, "taker")
        await taker.stop()


@app.command()
def clear_ignored_makers(
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
    """Clear the list of ignored makers."""
    from jmcore.paths import get_ignored_makers_path
    from jmcore.settings import get_settings

    # Load settings to get data_dir from config if not provided
    if data_dir is None:
        settings = get_settings()
        data_dir = settings.get_data_dir()

    ignored_makers_path = get_ignored_makers_path(data_dir)

    if not ignored_makers_path.exists():
        typer.echo("No ignored makers file found.")
        return

    # Count makers before deletion
    try:
        with open(ignored_makers_path, encoding="utf-8") as f:
            count = sum(1 for line in f if line.strip())
    except Exception as e:
        typer.echo(f"Error reading ignored makers file: {e}", err=True)
        raise typer.Exit(1)

    # Ask for confirmation
    if not typer.confirm(f"Clear {count} ignored maker(s)?"):
        typer.echo("Cancelled.")
        return

    # Delete the file
    try:
        ignored_makers_path.unlink()
        typer.echo(f"Cleared {count} ignored maker(s).")
    except Exception as e:
        typer.echo(f"Error deleting ignored makers file: {e}", err=True)
        raise typer.Exit(1)


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
    from jmcore.paths import get_default_data_dir

    if data_dir is None:
        data_dir = get_default_data_dir()

    config_path = ensure_config_file(data_dir)
    typer.echo(f"Config file created at: {config_path}")
    typer.echo("\nAll settings are commented out by default.")
    typer.echo("Edit the file to customize your configuration.")


def main() -> None:
    """Entry point."""
    app()


if __name__ == "__main__":
    main()
