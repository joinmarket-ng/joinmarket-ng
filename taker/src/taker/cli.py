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
import json
from pathlib import Path
from typing import Annotated, Any

import typer
from jmcore.cli_common import resolve_mnemonic, setup_cli
from jmcore.cli_help import SortedTyper
from jmcore.external_podle import ExternalPoDLE
from jmcore.models import NetworkType
from jmcore.notifications import get_notifier
from jmcore.paths import remove_nick_state, write_nick_state
from jmcore.settings import JoinMarketSettings, ensure_config_file
from jmwallet.wallet.service import WalletService
from loguru import logger

from taker.config import TakerConfig
from taker.config_builder import build_taker_config
from taker.podle_manager import ExternalPoDLEPoolError, PoDLEManager

__all__ = ["app", "build_taker_config", "create_backend"]

app = SortedTyper(
    name="jm-taker",
    help="JoinMarket Taker - Execute CoinJoin transactions",
    no_args_is_help=True,
)

_MAX_EXTERNAL_PODLE_IMPORT_BYTES = 64 * 1024


def create_backend(config: TakerConfig) -> Any:
    """Create appropriate backend based on config."""
    bitcoin_network = config.bitcoin_network or config.network

    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend

    backend: DescriptorWalletBackend | NeutrinoBackend
    if config.backend_type == "neutrino":
        backend = NeutrinoBackend(
            neutrino_url=config.backend_config.get("neutrino_url", "http://127.0.0.1:8334"),
            network=bitcoin_network.value,
            scan_start_height=config.backend_config.get("scan_start_height"),
            add_peers=config.backend_config.get("add_peers", []),
            tls_cert_path=config.backend_config.get("tls_cert_path"),
            auth_token=config.backend_config.get("auth_token"),
            include_mempool=config.backend_config.get("include_mempool", True),
            fee_estimate_url=config.backend_config.get("fee_estimate_url"),
            fee_estimate_proxy=config.backend_config.get("fee_estimate_proxy"),
        )
    elif config.backend_type == "descriptor_wallet":
        fingerprint = get_mnemonic_fingerprint(
            config.mnemonic.get_secret_value(), config.passphrase.get_secret_value() or ""
        )
        wallet_name = generate_wallet_name(fingerprint, bitcoin_network.value)
        backend = DescriptorWalletBackend(
            rpc_url=config.backend_config["rpc_url"],
            rpc_user=config.backend_config["rpc_user"],
            rpc_password=config.backend_config["rpc_password"],
            wallet_name=wallet_name,
            scan_start_height=config.backend_config.get("scan_start_height"),
            scan_lookback_blocks=config.backend_config.get("scan_lookback_blocks", 52_560),
        )
    else:
        raise ValueError(f"Unknown backend type: {config.backend_type}")

    if config.creation_height is not None:
        backend.set_wallet_creation_height(config.creation_height)

    return backend


@app.command()
def coinjoin(
    amount: Annotated[
        int | None,
        typer.Option(
            "--amount",
            "-a",
            help="Amount in sats (0 for sweep; with --select-utxos, defaults to sweep)",
        ),
    ] = None,
    destination: Annotated[
        str,
        typer.Option(
            "--destination",
            "-d",
            help="Destination address (or 'INTERNAL' for next mixdepth)",
        ),
    ] = "INTERNAL",
    mixdepth: Annotated[
        int | None,
        typer.Option(
            "--mixdepth",
            "-m",
            help="Source mixdepth (default 0; with --select-utxos, derived from "
            "the selection unless set explicitly; --input-utxo entries must belong "
            "to this mixdepth)",
        ),
    ] = None,
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
        typer.Option("--backend", "-b", help="Backend type: descriptor_wallet | neutrino"),
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
            help="Fraction of allowance slots chosen uniformly from zero-fee offers (0.0-1.0)",
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
            help="Restrict allowance spots to zero-fee offers",
        ),
    ] = None,
    quantized_offers_only: Annotated[
        bool | None,
        typer.Option(
            "--quantized-offers-only/--allow-non-quantized-offers",
            help="Only select offers whose advertised CoinJoin fee is on the public grid",
        ),
    ] = None,
    round_up_cj_fees: Annotated[
        bool | None,
        typer.Option(
            "--round-up-cj-fees/--no-round-up-cj-fees",
            help="Round selected maker fees up to public fee quanta",
        ),
    ] = None,
    equalize_cj_fees: Annotated[
        bool | None,
        typer.Option(
            "--equalize-cj-fees/--no-equalize-cj-fees",
            help="Pay all selected makers the highest realized fee in the selected set",
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
    input_utxo: Annotated[
        list[str] | None,
        typer.Option(
            "--input-utxo",
            help="Explicit input UTXO as txid:vout (repeatable). CoinJoin spends exactly "
            "the given UTXOs, including for sweeps, and never adds other inputs. Every "
            "UTXO must be eligible and belong to --mixdepth. Mutually exclusive with "
            "--select-utxos.",
        ),
    ] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
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
    if select_utxos and input_utxo:
        logger.error("Cannot specify both --select-utxos and --input-utxo")
        raise typer.Exit(1)

    if amount is None:
        if not select_utxos:
            logger.error("--amount is required unless --select-utxos is used")
            raise typer.Exit(1)
        amount = 0

    # Load settings (log_level=None means use settings.logging.level)
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

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
        resolved_creation_height = resolved.creation_height if resolved else None
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
            mixdepth=mixdepth if mixdepth is not None else 0,
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
            bondless_makers_allowance=bondless_makers_allowance,
            bond_value_exponent=bond_value_exponent,
            bondless_require_zero_fee=bondless_require_zero_fee,
            require_quantized_cj_fees=quantized_offers_only,
            round_up_cj_fees=round_up_cj_fees,
            equalize_cj_fees=equalize_cj_fees,
        )
    except ValueError as e:
        logger.error(str(e))
        raise typer.Exit(1)

    if resolved_creation_height is not None:
        config.creation_height = resolved_creation_height
    if resolved is not None:
        config.mnemonic_file = resolved.mnemonic_file

    # Log configuration source
    logger.info(f"Using network: {config.network.value}")
    logger.info(f"Using backend: {config.backend_type}")
    logger.info("Tor SOCKS proxy configured")
    logger.bind(sensitive=True).info(f"Tor SOCKS: {config.socks_host}:{config.socks_port}")

    try:
        asyncio.run(
            _run_coinjoin(
                settings=settings,
                config=config,
                amount=amount,
                destination=destination,
                mixdepth=mixdepth,
                counterparties=config.counterparty_count,
                skip_confirmation=yes,
                input_utxos=input_utxo,
            )
        )
    except RuntimeError as e:
        # Clean error for expected failures (e.g., connection failures)
        logger.error("CoinJoin failed")
        logger.bind(sensitive=True).error("CoinJoin failure detail: {}", e)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        raise typer.Exit(130)
    except Exception:
        logger.error("Unexpected CoinJoin error")
        logger.bind(sensitive=True).exception("Unexpected CoinJoin error")
        raise typer.Exit(1)


async def _run_coinjoin(
    settings: JoinMarketSettings,
    config: TakerConfig,
    amount: int,
    destination: str,
    mixdepth: int | None,
    counterparties: int | None,
    skip_confirmation: bool,
    input_utxos: list[str] | None = None,
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
            logger.error("Failed to connect to Neutrino backend")
            logger.bind(sensitive=True).error("Neutrino backend error detail: {}", e)
            raise typer.Exit(1)
    else:
        logger.info("Verifying Bitcoin Core RPC connection...")
        try:
            await backend.get_block_height()
            logger.info("Bitcoin Core RPC connection verified")
        except Exception as e:
            logger.error("Failed to connect to Bitcoin Core RPC")
            logger.bind(sensitive=True).error("Bitcoin Core RPC error detail: {}", e)
            raise typer.Exit(1)

    # Create wallet
    wallet = WalletService(
        mnemonic=config.mnemonic.get_secret_value(),
        passphrase=config.passphrase.get_secret_value(),
        backend=backend,
        network=bitcoin_network.value,
        mixdepth_count=config.mixdepth_count,
        gap_limit=config.gap_limit,
        scan_range=config.scan_range,
        data_dir=config.data_dir,
        max_sats_freeze_reuse=config.max_sats_freeze_reuse,
        reconstruct_history=config.reconstruct_history,
        mnemonic_file=config.mnemonic_file,
    )

    # Create confirmation callback
    async def confirmation_callback(
        maker_details: list[dict[str, Any]],
        cj_amount: int,
        total_fee: int,
        destination: str,
        mining_fee: int | None = None,
        fee_rate: float | None = None,
        stage: str = "",
    ) -> bool:
        """Callback for user confirmation after maker selection."""
        from jmcore.confirmation import confirm_transaction_async, format_maker_summary

        additional_info = format_maker_summary(
            maker_details,
            fee_rate=fee_rate,
            amount=cj_amount,
            minimum_fee_rate=taker.minimum_fee_rate_sat_vb,
        )
        # ``taker`` is assigned below in this scope, before the callback can
        # fire. With --select-utxos the source mixdepth is derived from the
        # user's selection, so read it back from the taker.
        source_mixdepth = taker.last_source_mixdepth
        if source_mixdepth is None:
            source_mixdepth = mixdepth if mixdepth is not None else 0
        additional_info["Source Mixdepth"] = source_mixdepth

        return await confirm_transaction_async(
            operation="coinjoin",
            amount=cj_amount,
            destination=destination,
            fee=total_fee,
            mining_fee=mining_fee,
            additional_info=additional_info,
            skip_confirmation=skip_confirmation,
            stage=stage,
        )

    # Create taker
    taker = Taker(wallet, backend, config, confirmation_callback=confirmation_callback)

    try:
        # Write nick state file for external tracking and cross-component protection
        nick = taker.nick
        data_dir = config.data_dir
        write_nick_state(data_dir, "taker", nick)
        logger.info("Taker nick state written")
        logger.bind(sensitive=True).info(f"Nick state written to {data_dir}/state/taker.nick")

        # Send startup notification (including nick)
        notifier = get_notifier(settings, component_name="Taker")
        await notifier.notify_startup(
            component="Taker (CoinJoin)",
            network=config.network.value,
            nick=nick,
        )

        # Sync wallet first (before connecting to directory servers)
        await taker.sync_wallet()

        # Early eligibility validation: confirm the mixdepth has spendable,
        # confirmed, unfrozen, non-bond UTXOs that can fund (and commit to) the
        # CoinJoin BEFORE connecting to directory servers and fetching the
        # orderbook. This avoids minutes of network work on a doomed round
        # (issue #528).
        eligibility_reason = await taker.check_utxo_eligibility(
            amount,
            mixdepth,
            input_utxos=input_utxos,
        )
        if eligibility_reason is not None:
            logger.error(eligibility_reason)
            raise typer.Exit(1)

        # Now connect to directory servers (UTXOs are eligible)
        await taker.connect()

        amount_display = "ALL (sweep)" if amount == 0 else f"{amount:,} sats"
        logger.bind(sensitive=True).info("Starting CoinJoin: {} -> {}", amount_display, destination)
        txid = await taker.do_coinjoin(
            amount=amount,
            destination=destination,
            mixdepth=mixdepth,
            counterparty_count=counterparties,
            input_utxos=input_utxos,
        )

        if txid:
            typer.echo(f"CoinJoin successful: {txid}")
            logger.info("CoinJoin successful")
            logger.bind(sensitive=True).info("CoinJoin successful: txid={}", txid)
            logger.info(f"Broadcast method: {taker.last_broadcast_method}")
            if taker.last_broadcast_fallback_reason:
                logger.warning(
                    "Privacy warning: configured policy {} fell back to self-broadcast ({})",
                    taker.last_broadcast_policy,
                    taker.last_broadcast_fallback_reason,
                )
        else:
            logger.error("CoinJoin failed")
            # Free our reserved inputs immediately so a retry can reuse them
            # (otherwise they stay locked until the TTL expires).
            taker.release_input_locks()
            raise typer.Exit(1)

    finally:
        # Clean up nick state file on shutdown
        remove_nick_state(config.data_dir, "taker")
        await taker.stop()


@app.command("import-podle")
def import_podle(
    record_file: Annotated[
        Path,
        typer.Argument(help="JSON file containing one external PoDLE record or a list of records"),
    ],
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            "-d",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory for JoinMarket files",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
) -> None:
    """Import external PoDLE records without printing their contents."""
    try:
        with record_file.open("rb") as f:
            raw = f.read(_MAX_EXTERNAL_PODLE_IMPORT_BYTES + 1)
    except OSError:
        typer.echo("Could not read external PoDLE import file.", err=True)
        raise typer.Exit(1)
    if len(raw) > _MAX_EXTERNAL_PODLE_IMPORT_BYTES:
        typer.echo("External PoDLE import file is too large.", err=True)
        raise typer.Exit(1)
    try:
        payload = json.loads(raw)
        records_data = payload if isinstance(payload, list) else [payload]
        if not records_data or not all(isinstance(item, dict) for item in records_data):
            raise ValueError
        records = [ExternalPoDLE.model_validate(item) for item in records_data]
    except Exception:
        typer.echo("Invalid external PoDLE import file.", err=True)
        raise typer.Exit(1)

    if data_dir is None:
        data_dir = setup_cli(None, config_file=config_file).get_data_dir()
    manager = PoDLEManager(data_dir)
    try:
        imported = sum(manager.import_external(record) for record in records)
        available = manager.external_count()
    except ExternalPoDLEPoolError:
        typer.echo("Could not safely update external PoDLE pool.", err=True)
        raise typer.Exit(1)
    typer.echo(f"Imported {imported} external PoDLE record(s). {available} available.")


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
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
) -> None:
    """Clear the list of ignored makers."""
    from jmcore.paths import get_ignored_makers_path

    # Load settings so a config file (possibly decoupled via --config-file)
    # that sets data_dir is honored when no explicit --data-dir is given.
    if data_dir is None:
        settings = setup_cli(None, config_file=config_file)
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
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
) -> None:
    """Initialize the config file with default settings."""
    from jmcore.paths import get_default_data_dir

    if data_dir is None:
        data_dir = get_default_data_dir()

    config_path = ensure_config_file(data_dir, config_file=config_file)
    typer.echo(f"Config file created at: {config_path}")
    typer.echo("\nAll settings are commented out by default.")
    typer.echo("Edit the file to customize your configuration.")


def main() -> None:
    """Entry point."""
    from jmcore.process_hardening import harden_current_process

    # Disable core dumps and ptrace before any wallet command loads secrets.
    harden_current_process()
    app()


if __name__ == "__main__":
    main()
