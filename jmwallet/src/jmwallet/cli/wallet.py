"""
Wallet management commands: import, generate, info, validate.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from jmcore.cli_common import (
    ResolvedBackendSettings,
    resolve_backend_settings,
    resolve_mnemonic,
    setup_cli,
    setup_logging,
)
from jmcore.paths import get_default_data_dir
from loguru import logger

from jmwallet.cli import app
from jmwallet.cli.mnemonic import (
    generate_mnemonic_secure,
    interactive_mnemonic_input,
    load_mnemonic_file,
    prompt_password_with_confirmation,
    save_mnemonic_file,
    validate_mnemonic,
)

if TYPE_CHECKING:
    from jmwallet.wallet.service import WalletService


@app.command("import")
def import_mnemonic(
    word_count: Annotated[
        int, typer.Option("--words", "-w", help="Number of words (12, 15, 18, 21, or 24)")
    ] = 24,
    output_file: Annotated[
        Path | None, typer.Option("--output", "-o", help="Output file path")
    ] = None,
    prompt_password: Annotated[
        bool,
        typer.Option(
            "--prompt-password/--no-prompt-password",
            help="Prompt for password interactively (default: prompt)",
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing file without confirmation"),
    ] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help=(
                "Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR). "
                "When --output is not given, the wallet is saved under "
                "<data-dir>/wallets/default.mnemonic."
            ),
        ),
    ] = None,
) -> None:
    """Import an existing BIP39 mnemonic phrase to create/recover a wallet.

    Enter your existing mnemonic interactively with autocomplete support,
    or set the MNEMONIC environment variable.

    By default, saves to <data-dir>/wallets/default.mnemonic with password
    protection. The data directory is taken from --data-dir, the
    JOINMARKET_DATA_DIR environment variable, or ~/.joinmarket-ng (in that
    order of precedence).

    Examples:
        jm-wallet import                          # Interactive input, 24 words
        jm-wallet import --words 12               # Interactive input, 12 words
        MNEMONIC="word1 word2 ..." jm-wallet import  # Via env var
        jm-wallet import -o my-wallet.mnemonic    # Custom output file
    """
    if data_dir is not None:
        os.environ["JOINMARKET_DATA_DIR"] = str(data_dir)
    setup_logging()

    if word_count not in (12, 15, 18, 21, 24):
        logger.error(f"Invalid word count: {word_count}. Must be 12, 15, 18, 21, or 24.")
        raise typer.Exit(1)

    # Get mnemonic from env var or interactive input
    env_mnemonic = os.environ.get("MNEMONIC")
    if env_mnemonic:
        mnemonic = env_mnemonic.strip()
        # Validate provided mnemonic
        words = mnemonic.split()
        if len(words) != word_count:
            logger.warning(
                f"Mnemonic has {len(words)} words but --words={word_count} was specified. "
                f"Using actual word count: {len(words)}"
            )
        if not validate_mnemonic(mnemonic):
            logger.error("Provided mnemonic is INVALID (bad checksum)")
            if not typer.confirm("Continue anyway?", default=False):
                raise typer.Exit(1)
        resolved_mnemonic = mnemonic
    else:
        # Interactive input with autocomplete
        if not sys.stdin.isatty():
            logger.error("Interactive input requires a terminal. Set MNEMONIC env var instead.")
            raise typer.Exit(1)
        resolved_mnemonic = interactive_mnemonic_input(word_count)

    # Display summary
    typer.echo("\n" + "=" * 80)
    typer.echo("IMPORTED MNEMONIC")
    typer.echo("=" * 80)
    word_list = resolved_mnemonic.split()
    typer.echo(f"Word count: {len(word_list)}")
    typer.echo(f"First word: {word_list[0]}")
    typer.echo(f"Last word: {word_list[-1]}")
    typer.echo("=" * 80 + "\n")

    # Determine output file
    if output_file is None:
        output_file = get_default_data_dir() / "wallets" / "default.mnemonic"

    # Check if file exists
    if output_file.exists() and not force:
        logger.warning(f"Wallet file already exists: {output_file}")
        if not typer.confirm("Overwrite existing wallet file?", default=False):
            typer.echo("Import cancelled")
            raise typer.Exit(1)

    # Get password for encryption
    password: str | None = None
    # Allow callers (typically the TUI) to pre-provide the password via
    # MNEMONIC_PASSWORD so the user isn't prompted again after already
    # entering it in a whiptail dialog (issue #462).
    env_password = os.environ.get("MNEMONIC_PASSWORD")
    if env_password:
        password = env_password
    elif prompt_password:
        password = prompt_password_with_confirmation()

    # Save the mnemonic
    save_mnemonic_file(resolved_mnemonic, output_file, password)

    typer.echo(f"\nMnemonic saved to: {output_file}")
    if password:
        typer.echo("File is encrypted - you will need the password to use it.")
    else:
        typer.echo("WARNING: File is NOT encrypted")
        typer.echo("For production use, consider using a password!")
    typer.echo("\nWallet import complete. You can now use other jm-wallet commands.")


@app.command()
def generate(
    word_count: Annotated[
        int, typer.Option("--words", "-w", help="Number of words (12, 15, 18, 21, or 24)")
    ] = 24,
    save: Annotated[
        bool, typer.Option("--save/--no-save", help="Save to file (default: save)")
    ] = True,
    output_file: Annotated[
        Path | None, typer.Option("--output", "-o", help="Output file path")
    ] = None,
    prompt_password: Annotated[
        bool,
        typer.Option(
            "--prompt-password/--no-prompt-password",
            help="Prompt for password interactively (default: prompt)",
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option("--force", "-f", help="Overwrite existing file without confirmation"),
    ] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help=(
                "Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR). "
                "When --output is not given, the wallet is saved under "
                "<data-dir>/wallets/default.mnemonic."
            ),
        ),
    ] = None,
) -> None:
    """Generate a new BIP39 mnemonic phrase with secure entropy.

    By default, saves to <data-dir>/wallets/default.mnemonic with password
    protection. The data directory is taken from --data-dir, the
    JOINMARKET_DATA_DIR environment variable, or ~/.joinmarket-ng (in that
    order of precedence). Use --no-save to only display the mnemonic without
    saving.
    """
    if data_dir is not None:
        os.environ["JOINMARKET_DATA_DIR"] = str(data_dir)
    setup_logging()

    try:
        # Auto-enable save if output_file is specified (even if --no-save was used)
        should_save = save or output_file is not None

        if should_save:
            if output_file is None:
                output_file = get_default_data_dir() / "wallets" / "default.mnemonic"

            # Check if file already exists BEFORE generating the seed
            if output_file.exists() and not force:
                logger.warning(f"Wallet file already exists: {output_file}")
                overwrite = typer.confirm("Overwrite existing wallet file?", default=False)
                if not overwrite:
                    typer.echo("Wallet generation cancelled")
                    raise typer.Exit(1)

        mnemonic = generate_mnemonic_secure(word_count)

        # Validate the generated mnemonic
        if not validate_mnemonic(mnemonic):
            logger.error("Generated mnemonic failed validation - this should not happen")
            raise typer.Exit(1)

        # Always display the mnemonic first
        typer.echo("\n" + "=" * 80)
        typer.echo("GENERATED MNEMONIC - WRITE THIS DOWN AND KEEP IT SAFE!")
        typer.echo("=" * 80)
        typer.echo(f"\n{mnemonic}\n")
        typer.echo("=" * 80)
        typer.echo("\nThis mnemonic controls your Bitcoin funds.")
        typer.echo("Anyone with this phrase can spend your coins.")
        typer.echo("Store it securely offline - NEVER share it with anyone!")
        typer.echo("=" * 80 + "\n")

        if should_save:
            # Prompt for password if requested
            password: str | None = None
            # Allow callers (typically the TUI) to pre-provide the password
            # via MNEMONIC_PASSWORD so the user isn't asked for it again
            # after having already entered it in a whiptail dialog
            # (issue #462). An empty env value is treated as "no password".
            env_password = os.environ.get("MNEMONIC_PASSWORD")
            if env_password:
                password = env_password
            elif prompt_password:
                password = prompt_password_with_confirmation()

            save_mnemonic_file(mnemonic, output_file, password)

            typer.echo(f"\nMnemonic saved to: {output_file}")
            if password:
                typer.echo("File is encrypted - you will need the password to use it.")
            else:
                typer.echo("WARNING: File is NOT encrypted")
                typer.echo("For production use, generate again with a password!")
            typer.echo("KEEP THIS FILE SECURE - IT CONTROLS YOUR FUNDS!")
        else:
            typer.echo("\nMnemonic NOT saved (--no-save was used)")
            typer.echo("To save it, run: jm-wallet generate")

    except ValueError as e:
        logger.error(f"Failed to generate mnemonic: {e}")
        raise typer.Exit(1)
    except typer.Exit:
        # Re-raise Exit exceptions without modification
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise typer.Exit(1)


@app.command()
def info(
    mnemonic_file: Annotated[
        Path | None,
        typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file", envvar="MNEMONIC_FILE"),
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option(
            "--prompt-bip39-passphrase",
            help="Prompt for BIP39 passphrase interactively",
        ),
    ] = False,
    network: Annotated[str | None, typer.Option("--network", "-n", help="Bitcoin network")] = None,
    backend_type: Annotated[
        str | None,
        typer.Option("--backend", "-b", help="Backend: descriptor_wallet | neutrino"),
    ] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    neutrino_url: Annotated[
        str | None, typer.Option("--neutrino-url", envvar="NEUTRINO_URL")
    ] = None,
    extended: Annotated[
        bool, typer.Option("--extended", "-e", help="Show detailed address view with derivations")
    ] = False,
    gap: Annotated[
        int, typer.Option("--gap", "-g", help="Max address gap to show in extended view")
    ] = 6,
    scan_depth: Annotated[
        int | None,
        typer.Option(
            "--scan-depth",
            help=(
                "One-shot override of the descriptor scan range (max address "
                "index per branch). When set, JoinMarket re-imports descriptors "
                "at the given range and triggers a full rescan from genesis -- "
                "use this once for a wallet migrated from legacy "
                "joinmarket-clientserver whose addresses sit beyond the "
                "default 1000 (issue #475). Without this flag, the configured "
                "``[wallet].scan_range`` is used and an existing import is "
                "left alone. Slow: a full rescan can take 20+ minutes on "
                "mainnet."
            ),
        ),
    ] = None,
    show_empty: Annotated[
        bool,
        typer.Option(
            "--show-empty/--no-show-empty",
            help=(
                "In --extended view, show addresses with zero balance. "
                "When disabled (default), empty addresses are hidden except "
                "for the first unused one per branch so you still have a "
                "fresh receive address."
            ),
        ),
    ] = False,
    scan_status: Annotated[
        bool,
        typer.Option(
            "--scan-status",
            help=(
                "Print Bitcoin Core's wallet scan/coverage diagnostics and "
                "exit (descriptor wallet only). Useful when the wallet is "
                "proposing already-used addresses: shows whether a rescan is "
                "currently running, the oldest active-descriptor timestamp "
                "(i.e., the lower bound of what Core has actually scanned), "
                "and the wallet transaction count. If the oldest timestamp "
                "is far newer than your wallet's first use, run "
                "``jm-wallet rescan`` to repair coverage."
            ),
        ),
    ] = False,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """Display wallet information and balances by mixdepth."""
    settings = setup_cli(log_level, data_dir=data_dir)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if not resolved:
            raise ValueError("No mnemonic provided")
        resolved_mnemonic = resolved.mnemonic
        resolved_bip39_passphrase = resolved.bip39_passphrase
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Resolve backend settings with CLI overrides taking priority
    backend = resolve_backend_settings(
        settings,
        network=network,
        backend_type=backend_type,
        rpc_url=rpc_url,
        neutrino_url=neutrino_url,
        data_dir=data_dir,
    )

    asyncio.run(
        _show_wallet_info(
            resolved_mnemonic,
            backend,
            resolved_bip39_passphrase,
            extended=extended,
            display_gap=gap,
            gap_limit=settings.wallet.gap_limit,
            scan_range=settings.wallet.scan_range,
            scan_depth=scan_depth,
            show_empty=show_empty,
            creation_height=resolved.creation_height if resolved else None,
            scan_status_only=scan_status,
        )
    )


async def _show_wallet_info(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str = "",
    extended: bool = False,
    display_gap: int = 6,
    gap_limit: int = 20,
    scan_range: int = 1000,
    scan_depth: int | None = None,
    show_empty: bool = False,
    creation_height: int | None = None,
    scan_status_only: bool = False,
) -> None:
    """Show wallet info implementation.

    Args:
        display_gap: Max empty addresses shown beyond last used in extended view.
        gap_limit: BIP44 gap limit (trailing-empty threshold). Forwarded to
            ``WalletService`` for sync-time logic.
        scan_range: Initial descriptor scan range. Forwarded to
            ``WalletService`` and used by ``setup_descriptor_wallet`` as the
            initial lookahead window.
        scan_depth: Optional one-shot override of the descriptor scan range
            for this invocation. When provided, forces re-import of
            descriptors at the given range and a full rescan from genesis --
            the recovery path for wallets migrated from legacy
            joinmarket-clientserver whose addresses sit beyond the configured
            ``scan_range`` (issue #475).
    """
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.history import (
        get_address_history_types,
        get_used_addresses,
        update_all_pending_transactions,
    )
    from jmwallet.wallet.service import WalletService

    network = backend_settings.network
    backend_type = backend_settings.backend_type
    data_dir = backend_settings.data_dir

    # Fail fast for backend-incompatible options. --scan-status surfaces
    # Bitcoin Core's descriptor scan coverage, which has no Neutrino
    # analogue. Refuse before instantiating any backend or waiting on
    # network sync, so the user is not made to wait for an error they
    # could have known up front.
    if scan_status_only and backend_type != "descriptor_wallet":
        logger.error(
            "--scan-status is only supported with the descriptor_wallet backend "
            f"(configured backend: {backend_type}). Neutrino exposes its own "
            "sync state through `jm-wallet info` directly."
        )
        raise typer.Exit(2)

    # Load fidelity bond addresses from registry
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
    from jmwallet.wallet.bond_registry import load_registry

    wallet_fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase or "")
    bond_registry = load_registry(data_dir, wallet_fingerprint)
    fidelity_bond_addresses: list[tuple[str, int, int]] = [
        (bond.address, bond.locktime, bond.index)
        for bond in bond_registry.bonds
        if bond.network == network
    ]
    if fidelity_bond_addresses:
        logger.info(f"Found {len(fidelity_bond_addresses)} fidelity bond(s) in registry")

    # Create backend
    backend: DescriptorWalletBackend | NeutrinoBackend
    if backend_type == "neutrino":
        backend = NeutrinoBackend(
            neutrino_url=backend_settings.neutrino_url,
            network=network,
            scan_start_height=backend_settings.scan_start_height,
            add_peers=backend_settings.neutrino_add_peers,
            tls_cert_path=backend_settings.neutrino_tls_cert,
            auth_token=backend_settings.neutrino_auth_token,
        )
        logger.info("Waiting for neutrino to sync...")
        synced = await backend.wait_for_sync(timeout=300.0)
        if not synced:
            logger.error("Neutrino sync timeout")
            raise typer.Exit(1)
    elif backend_type == "descriptor_wallet":
        from jmwallet.backends.descriptor_wallet import generate_wallet_name

        wallet_name = generate_wallet_name(wallet_fingerprint, network)
        backend = DescriptorWalletBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
            wallet_name=wallet_name,
        )
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")

    # If the wallet file records a creation height, tell the backend so it
    # can skip scanning blocks that predate the wallet.
    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

    # Create wallet with data_dir for history lookups. The ``scan_range``
    # field on ``WalletService`` is the initial descriptor lookahead window
    # (default 1000) and ``gap_limit`` is the true BIP44 trailing-empty
    # threshold. Migrated wallets with deep addresses (issue #475) are
    # handled by passing ``--scan-depth`` once: that forces a descriptor
    # re-import at the given range and a full rescan from genesis.
    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=5,
        gap_limit=gap_limit,
        scan_range=scan_range,
        passphrase=bip39_passphrase,
        data_dir=data_dir,
    )

    try:
        # Early-exit diagnostic path: report Bitcoin Core's wallet scan
        # status and exit without running the full sync. Cheap and useful
        # when the wallet is proposing already-used addresses (see
        # ``get_wallet_scan_status`` docstring for the failure modes this
        # surfaces).
        if scan_status_only:
            # Backend-type guard runs early (see top of this function); by
            # the time we get here, backend must be a DescriptorWalletBackend.
            from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

            assert isinstance(backend, DescriptorWalletBackend)
            # Make sure the wallet is loaded so getwalletinfo /
            # listdescriptors actually return something. ``is_wallet_setup``
            # also loads the wallet as a side effect.
            await backend.is_wallet_setup(expected_descriptor_count=None)
            status = await backend.get_wallet_scan_status()
            _print_scan_status(status)
            return

        # Use descriptor wallet sync if available
        if backend_type == "descriptor_wallet":
            from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

            if isinstance(backend, DescriptorWalletBackend):
                # Resolve the effective descriptor scan range for this run:
                # an explicit ``--scan-depth`` wins, else fall back to the
                # configured ``[wallet].scan_range`` (already on ``wallet``).
                effective_scan_range = scan_depth if scan_depth is not None else wallet.scan_range

                # Check if base wallet is set up (without counting bonds)
                bond_count = len(fidelity_bond_addresses)
                base_wallet_ready = await wallet.is_descriptor_wallet_ready(fidelity_bond_count=0)
                full_wallet_ready = await wallet.is_descriptor_wallet_ready(
                    fidelity_bond_count=bond_count
                )

                if scan_depth is not None:
                    # Recovery path for migrated wallets (issue #475): force
                    # re-import of descriptors with the requested range and a
                    # full rescan from genesis. This bypasses the
                    # ``is_wallet_setup`` short-circuit so an already-set-up
                    # wallet picks up the deeper range. ``smart_scan=False``
                    # + ``background_full_rescan=False`` means we scan from
                    # block 0 synchronously (slow but complete).
                    logger.info(
                        f"--scan-depth: re-importing descriptors with range "
                        f"[0, {effective_scan_range - 1}] and rescanning from genesis. "
                        "This may take 20+ minutes on mainnet."
                    )
                    await wallet.setup_descriptor_wallet(
                        scan_range=effective_scan_range,
                        fidelity_bond_addresses=(fidelity_bond_addresses if bond_count else None),
                        rescan=True,
                        check_existing=False,
                        smart_scan=False,
                        background_full_rescan=False,
                    )
                    logger.info("Deep rescan complete")
                elif not base_wallet_ready:
                    # First time setup - import everything including bonds
                    logger.info("Descriptor wallet not set up. Setting up...")
                    await wallet.setup_descriptor_wallet(
                        scan_range=effective_scan_range,
                        rescan=True,
                        fidelity_bond_addresses=fidelity_bond_addresses if bond_count else None,
                    )
                    logger.info("Descriptor wallet setup complete")
                elif not full_wallet_ready and bond_count > 0:
                    # Base wallet exists but bonds are missing - import just the bonds
                    logger.info(
                        "Descriptor wallet exists but fidelity bond addresses not imported. "
                        "Importing bond addresses..."
                    )
                    await wallet.import_fidelity_bond_addresses(
                        fidelity_bond_addresses, rescan=True
                    )

                # Use fast descriptor wallet sync (including fidelity bonds)
                await wallet.sync_with_descriptor_wallet(
                    fidelity_bond_addresses=fidelity_bond_addresses if bond_count else None
                )
        else:
            # Use standard sync (BIP157/158 for neutrino)
            await wallet.sync_all(fidelity_bond_addresses or None)

        # Update any pending transaction statuses
        # This safeguards against one-shot coinjoins that exited before confirmation
        await update_all_pending_transactions(
            backend, data_dir, wallet_fingerprint=wallet.wallet_fingerprint
        )

        from jmcore.bitcoin import format_amount

        # Show the wallet master fingerprint so users can pass it via
        # --wallet-fingerprint to cold-wallet bond commands.
        print(f"Wallet fingerprint: {wallet.wallet_fingerprint}")

        # Get total balance, separating FB balance
        total_balance = await wallet.get_total_balance(include_fidelity_bonds=False)
        fb_balance = await wallet.get_fidelity_bond_balance(0)  # FB only in mixdepth 0
        # Calculate total frozen balance across all mixdepths (excluding FB)
        total_frozen = sum(
            u.value
            for utxos_list in wallet.utxo_cache.values()
            for u in utxos_list
            if u.frozen and not u.is_fidelity_bond
        )
        # Build Total Balance display with optional FB and frozen suffixes
        suffix_parts: list[str] = []
        if fb_balance > 0:
            suffix_parts.append(f"{format_amount(fb_balance)} FB")
        if total_frozen > 0:
            suffix_parts.append(f"{format_amount(total_frozen)} frozen")
        display_balance = total_balance + fb_balance
        if suffix_parts:
            print(f"\nTotal Balance: {format_amount(display_balance)} ({', '.join(suffix_parts)})")
        else:
            print(f"\nTotal Balance: {format_amount(total_balance)}")

        # Show pending transactions if any
        from jmwallet.history import cleanup_stale_pending_transactions, get_pending_transactions

        # Clean up any stale pending transactions (older than 60 minutes).
        # Scope to the active wallet (issue #473) so we don't mark another
        # wallet's pending entries as failed from this wallet's CLI run.
        cleaned = cleanup_stale_pending_transactions(
            max_age_minutes=60,
            data_dir=data_dir,
            wallet_fingerprint=wallet.wallet_fingerprint,
        )
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} stale pending transaction(s)")

        pending = get_pending_transactions(data_dir, wallet_fingerprint=wallet.wallet_fingerprint)
        if pending:
            print(f"\nPending Transactions: {len(pending)}")
            for entry in pending:
                if entry.txid:
                    print(f"  {entry.txid[:16]}... - {entry.role} - {entry.confirmations} confs")
                else:
                    print(f"  [Broadcasting...] - {entry.role}")

        # Get history info for address status (scoped to active wallet, #473)
        used_addresses = get_used_addresses(data_dir, wallet_fingerprint=wallet.wallet_fingerprint)
        history_addresses = get_address_history_types(
            data_dir, wallet_fingerprint=wallet.wallet_fingerprint
        )

        if extended:
            # Extended view with detailed address information
            print("\nJM wallet")
            _show_extended_wallet_info(
                wallet, used_addresses, history_addresses, display_gap, show_empty=show_empty
            )
        else:
            # Simple view - show balance and suggested address per mixdepth
            print("\nBalance by mixdepth:")
            for md in range(5):
                balance = await wallet.get_balance(md, include_fidelity_bonds=False)
                # Calculate frozen balance for this mixdepth
                frozen_balance = sum(
                    u.value
                    for u in wallet.utxo_cache.get(md, [])
                    if u.frozen and not u.is_fidelity_bond
                )
                # Build suffix parts
                md_suffix_parts: list[str] = []
                if md == 0:
                    fb_balance = await wallet.get_fidelity_bond_balance(md)
                    if fb_balance > 0:
                        md_suffix_parts.append(f"+{fb_balance:,} FB")
                if frozen_balance > 0:
                    md_suffix_parts.append(f"{frozen_balance:,} frozen")
                suffix = f" ({', '.join(md_suffix_parts)})" if md_suffix_parts else ""
                print(f"  Mixdepth {md}: {balance:>15,} sats{suffix}")

            print("\nDeposit addresses (next unused):")
            for md in range(5):
                # Get next deposit address with per-candidate on-chain
                # verification (Layer 4b). Even if the bulk address-history
                # sync was incomplete due to a transient RPC failure,
                # ``get_next_safe_deposit_address`` will catch a
                # previously-funded candidate via ``getreceivedbyaddress``
                # and advance past it. This is the privacy belt-and-
                # suspenders that prevents proposing already-used
                # deposit addresses; see ``tmp/joinmarket_ng_wallet_rescan_3.txt``
                # for the real-world failure trace that motivated it.
                addr, _ = await wallet.get_next_safe_deposit_address(md, used_addresses)
                print(f"  Mixdepth {md}: {addr}")

    finally:
        await wallet.close()


def _print_scan_status(status: dict) -> None:
    """Pretty-print the diagnostic dict from
    ``DescriptorWalletBackend.get_wallet_scan_status``.

    Formats timestamps, flags suspiciously narrow coverage (oldest active
    descriptor timestamp much newer than genesis), and notes whether a
    rescan is currently in progress. Intended for ``jm-wallet info
    --scan-status`` and ``jm-wallet rescan``.
    """
    from datetime import datetime

    def _fmt_ts(ts: int | None) -> str:
        if not ts:
            return "(unknown)"
        return (
            datetime.fromtimestamp(int(ts), tz=UTC).isoformat(timespec="seconds")
            + f"  (unix {int(ts)})"
        )

    def _fmt_age(seconds: int) -> str:
        """Render an age in human-friendly units (years/months/days)."""
        if seconds < 60:
            return f"{seconds}s"
        if seconds < 3600:
            return f"{seconds // 60}m"
        if seconds < 86400:
            return f"{seconds // 3600}h"
        days = seconds // 86400
        if days < 60:
            return f"{days} day{'s' if days != 1 else ''}"
        months = days // 30
        if months < 24:
            return f"{months} month{'s' if months != 1 else ''}"
        years = days // 365
        remaining_months = (days % 365) // 30
        if remaining_months:
            return (
                f"{years} year{'s' if years != 1 else ''}, "
                f"{remaining_months} month{'s' if remaining_months != 1 else ''}"
            )
        return f"{years} year{'s' if years != 1 else ''}"

    print("\nBitcoin Core wallet scan status:")
    print(f"  Transactions known to Core:    {status.get('txcount', 0):,}")
    print(f"  Wallet birthtime:              {_fmt_ts(status.get('birthtime'))}")
    print(f"  Smart-scan boundary:           {_fmt_ts(status.get('oldest_descriptor_timestamp'))}")

    if status.get("scanning_in_progress"):
        progress = status.get("scan_progress")
        progress_str = f"{progress * 100:.1f}%" if progress is not None else "?"
        duration = status.get("scan_duration_s")
        duration_str = f", {duration}s elapsed" if duration else ""
        print(f"  Rescan currently running:      yes ({progress_str}{duration_str})")
    else:
        print("  Rescan currently running:      no")

    # Heuristic warning: importdescriptors sets the smart-scan boundary to
    # ~1 year ago at first setup, which is fine for "recent receives" but
    # misses older history. Bitcoin's genesis is 2009-01-03 (unix
    # 1230768000). If the user's coins are older than the smart-scan
    # boundary, Core does not know they were ever used.
    oldest = status.get("oldest_descriptor_timestamp")
    if oldest is not None and oldest > 1230768000:
        from time import time as _now

        age_seconds = max(0, int(_now()) - int(oldest))
        print(
            f"\n  Note: Bitcoin Core has only scanned the last {_fmt_age(age_seconds)} "
            "for this wallet. If your wallet has spends or receives older "
            "than that, Core has not indexed them and may propose "
            "already-used addresses as fresh deposits. Run "
            "`jm-wallet rescan` to scan from genesis."
        )


def _print_branch_addresses(
    addresses: list,  # list[AddressInfo] - avoid import cycle at module top
    pending_addresses: set[str],
    frozen_addresses: set[str],
    show_empty: bool = False,
    new_address_limit: int = 6,
) -> tuple[int, int]:
    """Print addresses for one wallet branch and return (total_balance, hidden_count).

    When ``show_empty`` is False, addresses with zero balance are skipped
    with two exceptions:

    * Up to ``new_address_limit`` unused ``new`` addresses are displayed so
      users (especially in the TUI) can pick multiple fresh receive
      addresses without dropping to ``--show-empty`` (see issue #463).
    * ``used-empty`` and ``flagged`` entries are always hidden in this mode
      because they are not safe to reuse and only add noise.

    Total balance is computed over all addresses (even skipped ones) so
    balance display remains accurate. ``hidden_count`` counts addresses
    that were omitted from the output because they were empty.
    """
    from jmcore.bitcoin import sats_to_btc

    total_balance = 0
    hidden = 0
    empty_new_shown = 0
    # Statuses that are unsafe to reuse; hide them entirely when not in
    # show-empty mode to keep the output actionable.
    _always_hide_when_empty = {"used-empty", "flagged"}

    for addr_info in addresses:
        total_balance += addr_info.balance

        # Filter empty addresses unless explicitly requested.
        if not show_empty and addr_info.balance == 0:
            if addr_info.status == "new" and empty_new_shown < new_address_limit:
                empty_new_shown += 1
            elif addr_info.status in _always_hide_when_empty:
                # Unsafe-to-reuse empty addresses are never surfaced in the
                # default view; still count them so the "hidden" summary
                # stays accurate.
                hidden += 1
                continue
            else:
                hidden += 1
                continue

        btc_balance = sats_to_btc(addr_info.balance)
        status_display: str = addr_info.status
        if addr_info.address in pending_addresses:
            status_display += " (pending)"
        elif addr_info.has_unconfirmed:
            status_display += " (unconfirmed)"
        if addr_info.address in frozen_addresses:
            status_display += " [FROZEN]"

        # Append confirmation count for funded addresses (capped at 5+).
        if addr_info.utxos:
            min_confs = min(u.confirmations for u in addr_info.utxos)
            if min_confs >= 5:
                confs_display = "5+ conf"
            else:
                confs_display = f"{min_confs} conf"
            status_display += f" ({confs_display})"

        print(f"{addr_info.path:<24}{addr_info.address}\t{btc_balance:.8f}\t{status_display}")

    return total_balance, hidden


def _show_extended_wallet_info(
    wallet: WalletService,
    used_addresses: set[str],
    history_addresses: dict[str, str],
    gap_limit: int,
    show_empty: bool = False,
) -> None:
    """
    Display extended wallet information with detailed address listings.

    Mirrors the reference implementation's output format:
    - Shows zpub for each mixdepth (BIP84 native segwit format)
    - Lists external and internal addresses with derivation paths
    - Shows address status (deposit, cj-out, cj-change, non-cj-change, new, etc.)
    - Shows balance per address and per branch

    When ``show_empty`` is False (the default), addresses with zero balance
    are hidden to keep the output readable on long-running wallets. The
    first unused "new" address of each branch is still displayed so the
    user has a fresh receive address at a glance, and the number of
    hidden entries is printed as a summary line.
    """
    from jmcore.bitcoin import sats_to_btc

    from jmwallet.history import get_pending_transactions
    from jmwallet.wallet.service import FIDELITY_BOND_BRANCH

    # Build set of addresses with frozen UTXOs
    frozen_addresses: set[str] = set()
    for utxos in wallet.utxo_cache.values():
        for utxo in utxos:
            if utxo.frozen:
                frozen_addresses.add(utxo.address)

    # Print legend for address statuses
    print("Address status legend:")
    print("  new         - Unused, safe for receiving")
    print("  deposit     - External address with funds")
    print("  cj-out      - CoinJoin output (mixed funds)")
    print("  cj-change   - Change output from a CoinJoin (deanonymising, keep separate)")
    print("  non-cj-change - Regular change (not from CoinJoin)")
    print("  used-empty  - Previously used, now empty (do not reuse)")
    print("  flagged     - Shared with peers but tx failed (do not reuse)")
    print()

    # Get pending transactions to mark addresses (scoped to active wallet, #473)
    pending_txs = get_pending_transactions(
        wallet.data_dir, wallet_fingerprint=wallet.wallet_fingerprint
    )
    pending_addresses = set()
    for entry in pending_txs:
        if entry.destination_address:
            pending_addresses.add(entry.destination_address)
        if entry.change_address:
            pending_addresses.add(entry.change_address)

    for md in range(wallet.mixdepth_count):
        # Get account zpub (BIP84 format for native segwit)
        zpub = wallet.get_account_zpub(md)

        print(f"mixdepth\t{md}\t{zpub}")

        # External addresses (receive / deposit)
        ext_addresses = wallet.get_address_info_for_mixdepth(
            md, 0, gap_limit, used_addresses, history_addresses
        )
        # Get the external branch zpub path
        ext_path = f"m/84'/{0 if wallet.network == 'mainnet' else 1}'/{md}'/0"
        print(f"external addresses\t{ext_path}\t{zpub}")

        ext_balance = 0
        ext_balance, ext_hidden = _print_branch_addresses(
            ext_addresses,
            pending_addresses,
            frozen_addresses,
            show_empty=show_empty,
        )

        if ext_hidden:
            print(f"\t\t\t({ext_hidden} empty addresses hidden; pass --show-empty to display)")
        print(f"Balance:\t{sats_to_btc(ext_balance):.8f}")

        # Internal addresses (change / CJ output)
        int_addresses = wallet.get_address_info_for_mixdepth(
            md, 1, gap_limit, used_addresses, history_addresses
        )
        int_path = f"m/84'/{0 if wallet.network == 'mainnet' else 1}'/{md}'/1"
        print(f"internal addresses\t{int_path}")

        int_balance, int_hidden = _print_branch_addresses(
            int_addresses,
            pending_addresses,
            frozen_addresses,
            show_empty=show_empty,
        )

        if int_hidden:
            print(f"\t\t\t({int_hidden} empty addresses hidden; pass --show-empty to display)")
        print(f"Balance:\t{sats_to_btc(int_balance):.8f}")

        # Fidelity bond branch (only for mixdepth 0)
        bond_addresses: list = []  # Initialize for type checker
        if md == 0:
            bond_addresses = wallet.get_fidelity_bond_addresses_info(gap_limit)
            if bond_addresses:
                bond_path = (
                    f"m/84'/{0 if wallet.network == 'mainnet' else 1}'/0'/{FIDELITY_BOND_BRANCH}"
                )
                print(f"fidelity bond addresses\t{bond_path}\t{zpub}")

                bond_balance = 0
                bond_locked = 0  # Locked balance (not yet expired)
                import time

                current_time = int(time.time())

                for addr_info in bond_addresses:
                    btc_balance = sats_to_btc(addr_info.balance)
                    bond_balance += addr_info.balance
                    is_locked = addr_info.locktime and addr_info.locktime > current_time
                    if is_locked:
                        bond_locked += addr_info.balance

                    # Show locktime as date for bonds
                    locktime_str = ""
                    if addr_info.locktime:
                        dt = datetime.fromtimestamp(addr_info.locktime)
                        locktime_str = dt.strftime("%Y-%m-%d")
                        if is_locked:
                            locktime_str += " [LOCKED]"

                    # Show unconfirmed status if applicable
                    if addr_info.has_unconfirmed:
                        locktime_str += " (unconfirmed)"

                    # Pad path to ensure consistent alignment regardless of index digits
                    print(
                        f"{addr_info.path:<24}{addr_info.address}\t{btc_balance:.8f}\t{locktime_str}"
                    )

                # Show bond balance with locked amount in parentheses
                if bond_locked > 0:
                    print(
                        f"Balance:\t{sats_to_btc(bond_balance - bond_locked):.8f} "
                        f"({sats_to_btc(bond_locked):.8f})"
                    )
                else:
                    print(f"Balance:\t{sats_to_btc(bond_balance):.8f}")

        # Total balance for mixdepth
        total_md_balance = ext_balance + int_balance
        # For mixdepth 0, show FB balance separately if there are bonds
        if md == 0 and bond_addresses:
            bond_balance = sum(addr_info.balance for addr_info in bond_addresses)
            if bond_balance > 0:
                print(
                    f"Balance for mixdepth {md}:\t{sats_to_btc(total_md_balance):.8f} "
                    f"(+{sats_to_btc(bond_balance):.8f} FB)"
                )
            else:
                print(f"Balance for mixdepth {md}:\t{sats_to_btc(total_md_balance):.8f}")
        else:
            print(f"Balance for mixdepth {md}:\t{sats_to_btc(total_md_balance):.8f}")


@app.command("verify-password")
def verify_password(
    mnemonic_file: Annotated[
        Path,
        typer.Option(
            "--mnemonic-file",
            "-f",
            help="Path to encrypted mnemonic file",
            envvar="MNEMONIC_FILE",
        ),
    ],
    password: Annotated[
        str | None,
        typer.Option(
            "--password",
            "-p",
            help="Password to verify. If not provided, read from MNEMONIC_PASSWORD env or prompt.",
            envvar="MNEMONIC_PASSWORD",
        ),
    ] = None,
    prompt: Annotated[
        bool,
        typer.Option(
            "--prompt/--no-prompt",
            help="Prompt for password if not provided via flag/env.",
        ),
    ] = True,
) -> None:
    """Verify that a password can decrypt an encrypted mnemonic file.

    Exits with status 0 if the password is correct, 1 otherwise.
    Intended for scripting (e.g. the TUI) to validate a password before
    storing it in config.toml. No mnemonic content is printed.
    """
    if not mnemonic_file.exists():
        print(f"Error: Mnemonic file not found: {mnemonic_file}")
        raise typer.Exit(1)

    # Detect plaintext wallets up front: there is nothing to verify.
    try:
        data = mnemonic_file.read_bytes()
        text = data.decode("utf-8")
        words = text.strip().split()
        if len(words) in (12, 15, 18, 21, 24) and all(w.isalpha() for w in words):
            print("Mnemonic file is not encrypted; no password to verify.")
            raise typer.Exit(2)
    except UnicodeDecodeError:
        pass

    if not password and prompt:
        password = typer.prompt("Enter password to verify", hide_input=True)

    if not password:
        print("Error: No password provided.")
        raise typer.Exit(1)

    try:
        load_mnemonic_file(mnemonic_file, password)
    except ValueError as e:
        # Wrong password or corrupt file -- do not leak details.
        msg = str(e).lower()
        if "decryption failed" in msg or "wrong password" in msg:
            print("Password is INCORRECT")
        else:
            print(f"Error: {e}")
        raise typer.Exit(1)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise typer.Exit(1)

    print("Password is CORRECT")


@app.command()
def validate(
    mnemonic_file: Annotated[
        Path | None,
        typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file", envvar="MNEMONIC_FILE"),
    ] = None,
) -> None:
    """Validate a mnemonic phrase.

    Provide a mnemonic via --mnemonic-file, the MNEMONIC environment variable,
    or enter it interactively when prompted.
    """
    import os

    mnemonic: str = ""

    if mnemonic_file:
        try:
            mnemonic = load_mnemonic_file(mnemonic_file)
        except ValueError as e:
            if "encrypted" in str(e).lower():
                # File is encrypted, prompt for password
                password = typer.prompt("Enter password to decrypt mnemonic file", hide_input=True)
                try:
                    mnemonic = load_mnemonic_file(mnemonic_file, password)
                except (FileNotFoundError, ValueError) as e2:
                    print(f"Error: {e2}")
                    raise typer.Exit(1)
            else:
                print(f"Error: {e}")
                raise typer.Exit(1)
        except FileNotFoundError as e:
            print(f"Error: {e}")
            raise typer.Exit(1)
    else:
        env_mnemonic = os.environ.get("MNEMONIC")
        if env_mnemonic:
            mnemonic = env_mnemonic.strip()
        else:
            mnemonic = typer.prompt("Enter mnemonic to validate")

    if validate_mnemonic(mnemonic):
        print("Mnemonic is VALID")
        word_count = len(mnemonic.strip().split())
        print(f"Word count: {word_count}")
    else:
        print("Mnemonic is INVALID")
        raise typer.Exit(1)


@app.command()
def showseed(
    mnemonic_file: Annotated[
        Path,
        typer.Option(
            "--mnemonic-file",
            "-f",
            help="Path to the mnemonic file",
            envvar="MNEMONIC_FILE",
        ),
    ],
    password: Annotated[
        str | None,
        typer.Option(
            "--password",
            "-p",
            help=(
                "Password for an encrypted mnemonic file. If not given, the "
                "MNEMONIC_PASSWORD env var is used, otherwise an interactive "
                "prompt is shown."
            ),
            envvar="MNEMONIC_PASSWORD",
        ),
    ] = None,
    numbered: Annotated[
        bool,
        typer.Option(
            "--numbered/--no-numbered",
            help="Print each seed word on its own line, prefixed with its index.",
        ),
    ] = True,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help="Skip the interactive 'Are you sure?' confirmation. Use with care.",
        ),
    ] = False,
) -> None:
    """Display the BIP39 seed words (mnemonic) of an existing wallet.

    Reads the encrypted ``.mnemonic`` file produced by ``jm-wallet generate``
    (or any compatible wallet) and prints the seed words to stdout.

    SECURITY:
    - The seed words give full control over all funds. Never share them, never
      type them into a website, never store them in cloud sync.
    - Only run this command in a private setting. Output goes to stdout in
      plaintext; redirect carefully.
    - The password is required when the mnemonic file is encrypted.
    """
    if not mnemonic_file.exists():
        print(f"Error: Mnemonic file not found: {mnemonic_file}")
        raise typer.Exit(1)

    # Try plaintext load first; if encrypted, prompt for / use password.
    try:
        mnemonic = load_mnemonic_file(mnemonic_file)
    except ValueError as e:
        if "encrypted" in str(e).lower():
            if not password:
                password = typer.prompt("Enter password to decrypt mnemonic file", hide_input=True)
            try:
                mnemonic = load_mnemonic_file(mnemonic_file, password)
            except ValueError as e2:
                msg = str(e2).lower()
                if "decryption failed" in msg or "wrong password" in msg:
                    print("Error: Incorrect password.")
                else:
                    print(f"Error: {e2}")
                raise typer.Exit(1)
            except FileNotFoundError as e2:
                print(f"Error: {e2}")
                raise typer.Exit(1)
        else:
            print(f"Error: {e}")
            raise typer.Exit(1)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        raise typer.Exit(1)

    if not yes:
        # Interactive guard so seed words are never accidentally splashed on
        # a shared terminal (e.g. when the user mistypes another command).
        confirm = typer.confirm(
            "About to print the BIP39 seed words to stdout. "
            "Are you in a private setting and sure you want to continue?",
            default=False,
        )
        if not confirm:
            print("Aborted.")
            raise typer.Exit(1)

    words = mnemonic.strip().split()

    typer.secho(
        "WARNING: Anyone with these words can spend all your funds. "
        "Do not share them, photograph them, or paste them into any website.",
        fg=typer.colors.RED,
        bold=True,
        err=True,
    )

    if numbered:
        for i, word in enumerate(words, start=1):
            print(f"{i:2d}. {word}")
    else:
        print(mnemonic.strip())


@app.command()
def rescan(
    mnemonic_file: Annotated[
        Path | None,
        typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file", envvar="MNEMONIC_FILE"),
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option(
            "--prompt-bip39-passphrase",
            help="Prompt for BIP39 passphrase interactively",
        ),
    ] = False,
    network: Annotated[str | None, typer.Option("--network", "-n", help="Bitcoin network")] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    start_height: Annotated[
        int,
        typer.Option(
            "--start-height",
            help=(
                "Block height to rescan from (default: 0 = genesis). The "
                "wallet's recorded creation height is used as a floor when "
                "available, so values below it are clamped up automatically."
            ),
        ),
    ] = 0,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """Trigger a Bitcoin Core wallet rescan to repair history coverage.

    Use this when ``jm-wallet info --scan-status`` shows that the oldest
    active descriptor timestamp is newer than your wallet's first use, or
    when the wallet is proposing addresses you remember spending from.
    Rescans are slow (20+ minutes on mainnet from genesis) but read-only;
    no funds are at risk.

    The rescan runs server-side in Bitcoin Core: this command blocks while
    polling progress, but interrupting it with Ctrl-C only ends the
    polling, not the rescan itself. Bitcoin Core will keep scanning, and
    you can re-attach later via ``jm-wallet info --scan-status``.
    """
    settings = setup_cli(log_level, data_dir=data_dir)

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

    backend_settings = resolve_backend_settings(
        settings,
        network=network,
        rpc_url=rpc_url,
        data_dir=data_dir,
    )

    # Rescan is a Bitcoin Core wallet operation; the Neutrino backend has
    # no analogue and trying to force it down a descriptor_wallet code
    # path would just fail later with a confusing connection error.
    if backend_settings.backend_type != "descriptor_wallet":
        logger.error(
            "jm-wallet rescan is only supported with the descriptor_wallet backend "
            f"(configured backend: {backend_settings.backend_type}). The Neutrino "
            "backend reuses its own filter cache and does not expose a rescan."
        )
        raise typer.Exit(2)

    asyncio.run(
        _run_rescan(
            mnemonic=resolved.mnemonic,
            backend_settings=backend_settings,
            bip39_passphrase=resolved.bip39_passphrase,
            start_height=start_height,
            creation_height=resolved.creation_height,
        )
    )


async def _run_rescan(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str,
    start_height: int,
    creation_height: int | None,
) -> None:
    """Implementation of ``jm-wallet rescan``."""
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )

    fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase or "")
    wallet_name = generate_wallet_name(fingerprint, backend_settings.network)
    backend = DescriptorWalletBackend(
        rpc_url=backend_settings.rpc_url,
        rpc_user=backend_settings.rpc_user,
        rpc_password=backend_settings.rpc_password,
        wallet_name=wallet_name,
    )
    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

    try:
        loaded = await backend.is_wallet_setup(expected_descriptor_count=None)
        if not loaded:
            logger.error(
                f"Wallet {wallet_name!r} is not loaded in Bitcoin Core. "
                "Run `jm-wallet info` once to set it up before rescanning."
            )
            raise typer.Exit(1)

        # Show pre-rescan status so the user can confirm coverage actually
        # changed afterward.
        pre_status = await backend.get_wallet_scan_status()
        print("Before rescan:")
        _print_scan_status(pre_status)

        # Clamp to wallet creation height when it is more recent than the
        # requested start, mirroring what setup_descriptor_wallet does.
        effective_start = max(start_height, creation_height or 0)
        if effective_start != start_height:
            print(
                f"\nUsing wallet creation height {effective_start} "
                f"(requested {start_height}) as the rescan floor."
            )

        print(f"\nRescanning from height {effective_start}. This can take a while...")
        # Trigger the server-side rescan and poll for progress. The rescan
        # is owned by Bitcoin Core (not this process), so Ctrl-C is safe:
        # it ends polling without stopping the scan itself.
        await backend.start_background_rescan(start_height=effective_start)
        try:
            await _await_rescan_completion(backend)
        except KeyboardInterrupt:
            print(
                "\nPolling interrupted. The rescan continues in Bitcoin Core; "
                "check status with `jm-wallet info --scan-status`."
            )
            return
        post_status = await backend.get_wallet_scan_status()
        print("\nAfter rescan:")
        _print_scan_status(post_status)
    finally:
        await backend.close()


async def _await_rescan_completion(
    backend: Any,
    poll_interval_seconds: float = 5.0,
    progress_callback: Callable[[float, float], None] | None = None,
) -> None:
    """Poll ``getwalletinfo`` until the in-progress rescan finishes.

    Bitcoin Core's ``rescanblockchain`` runs server-side and is not bound to
    the lifetime of the originating RPC call: even if our HTTP client times
    out, the rescan keeps going. By polling ``getwalletinfo.scanning`` we
    sidestep client-side timeouts entirely while still surfacing live
    progress to the user.

    Args:
        backend: ``DescriptorWalletBackend`` with an in-flight rescan.
        poll_interval_seconds: Sleep between successive status checks.
        progress_callback: Optional sink for ``(progress_fraction, duration_s)``;
            primarily a test seam.
    """
    last_pct = -1
    # Brief grace period so Bitcoin Core actually flips ``scanning`` on.
    await asyncio.sleep(min(poll_interval_seconds, 2.0))
    while True:
        try:
            status = await backend.get_rescan_status()
        except Exception as exc:
            logger.warning(f"Failed to poll rescan status: {exc}; retrying...")
            await asyncio.sleep(poll_interval_seconds)
            continue

        if not status or not status.get("in_progress"):
            if progress_callback is not None:
                progress_callback(1.0, 0.0)
            print("\nRescan complete.")
            return

        progress = float(status.get("progress", 0.0) or 0.0)
        duration = float(status.get("duration", 0) or 0)
        if progress_callback is not None:
            progress_callback(progress, duration)

        pct = int(progress * 100)
        if pct != last_pct:
            print(
                f"  Rescan progress: {pct:>3}%  (elapsed {int(duration)}s)",
                end="\r",
                flush=True,
            )
            last_pct = pct

        await asyncio.sleep(poll_interval_seconds)
