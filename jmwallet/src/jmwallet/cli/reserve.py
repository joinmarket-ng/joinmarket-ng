"""
Fund the anchor reserve of a mixdepth's managed Lightning node.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from jmcore.cli_common import resolve_backend_settings, resolve_mnemonic, setup_cli
from loguru import logger

from jmwallet.cli import app
from jmwallet.cli.send import _check_fee_options, _send_transaction


@app.command("fund-reserve", no_args_is_help=True)
def fund_reserve(
    mixdepth: Annotated[
        int,
        typer.Argument(help="Mixdepth whose Lightning node to fund; the coins come from it"),
    ],
    amount: Annotated[
        int | None,
        typer.Option(
            "--amount",
            "-a",
            help="Amount in sats (default: reserve_min_sats, capped at reserve_max_sats)",
        ),
    ] = None,
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    fee_rate: Annotated[
        float | None,
        typer.Option("--fee-rate", help="Manual fee rate in sat/vB (excludes --block-target)"),
    ] = None,
    block_target: Annotated[
        int | None,
        typer.Option("--block-target", help="Target blocks for fee estimation (1-1008)"),
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
    """Fund a managed Lightning node's anchor reserve from its own mixdepth.

    Sends from MIXDEPTH to a fresh Taproot address of that mixdepth's node, so
    no other mixdepth is involved. Does nothing when the node's confirmed plus
    unconfirmed balance already reaches reserve_min_sats.
    """
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)
    lightning = settings.lightning
    if lightning.mode != "managed":
        logger.error(
            "fund-reserve needs lightning.mode = 'managed'; in manual mode, fund your "
            "node's on-chain wallet directly from the mixdepth it serves"
        )
        raise typer.Exit(1)
    max_fee_rate = _check_fee_options(fee_rate, block_target, settings.wallet.max_fee_rate_sat_vb)
    if block_target is None and fee_rate is None:
        block_target = settings.wallet.default_fee_block_target
    backend_settings = resolve_backend_settings(settings, data_dir=data_dir)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if not resolved:
            raise ValueError("No mnemonic provided")
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    try:
        from jmswap.lightning_reserve import (
            ReserveError,
            check_reserve_amount,
            default_reserve_amount,
            managed_reserve_target,
        )
    except ImportError:
        logger.error("fund-reserve needs the jmswap package")
        raise typer.Exit(1)
    from jmcore.crypto import mnemonic_to_seed

    seed = mnemonic_to_seed(resolved.mnemonic, resolved.bip39_passphrase)
    try:
        balance, address = asyncio.run(managed_reserve_target(settings, seed, mixdepth))
        if address is None:
            logger.info(f"Mixdepth {mixdepth}'s Lightning reserve is funded ({balance:,} sats)")
            return
        send_amount = (
            amount
            if amount is not None
            else default_reserve_amount(
                balance,
                reserve_min_sats=lightning.reserve_min_sats,
                reserve_max_sats=lightning.reserve_max_sats,
            )
        )
        check_reserve_amount(balance, send_amount, reserve_max_sats=lightning.reserve_max_sats)
    except ReserveError as exc:
        logger.error(f"Cannot fund mixdepth {mixdepth}'s Lightning reserve: {exc}")
        raise typer.Exit(1)

    logger.info(
        f"Funding mixdepth {mixdepth}'s Lightning reserve with {send_amount:,} sats "
        f"(current balance {balance:,} sats)"
    )
    asyncio.run(
        _send_transaction(
            resolved.mnemonic,
            address,
            send_amount,
            mixdepth,
            fee_rate,
            block_target,
            backend_settings,
            True,
            yes,
            False,
            resolved.bip39_passphrase,
            creation_height=resolved.creation_height,
            mnemonic_file=resolved.mnemonic_file,
            mixdepth_count=settings.wallet.mixdepth_count,
            max_fee_rate_sat_vb=max_fee_rate,
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
            reconstruct_history=settings.wallet.reconstruct_history,
            rbf=True,
            address_type=settings.wallet.address_type,
        )
    )
