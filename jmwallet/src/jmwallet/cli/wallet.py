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


# ANSI SGR codes used by the extended wallet-info view. Kept as named
# constants so the call sites stay readable and the escape sequences are not
# scattered as raw literals.
_ANSI_RESET = "\033[0m"
_ANSI_BOLD_YELLOW = "\033[1;33m"
_ANSI_BOLD_WHITE = "\033[1;37m"
_ANSI_BOLD_CYAN = "\033[1;36m"
_ANSI_CYAN = "\033[0;36m"


def _color_enabled() -> bool:
    """Whether ANSI colors should be emitted to stdout.

    Colors are only safe on an interactive terminal. When stdout is piped or
    redirected (``jm-wallet info --extended > file`` / ``| less`` / CI logs),
    raw escape codes corrupt the output and break downstream parsing, so they
    are suppressed. The ``NO_COLOR`` convention (https://no-color.org) is also
    honored. This mirrors the TTY guards already used elsewhere in the CLI
    (e.g. the freeze manager and the interactive UTXO selector).
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    return sys.stdout.isatty()


def _colorize(text: str, code: str) -> str:
    """Wrap ``text`` in an ANSI ``code`` when colors are enabled, else return it unchanged."""
    if not _color_enabled():
        return text
    return f"{code}{text}{_ANSI_RESET}"


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
                "Print Bitcoin Core's wallet scan/coverage diagnostics and exit "
                "(descriptor wallet only). Use it when the wallet proposes "
                "already-used addresses; if coverage is incomplete, repair it "
                "with `jm-wallet rescan`. See the wallet scanning docs."
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
    """Display wallet information and balances by mixdepth."""
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

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
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
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
    max_sats_freeze_reuse: int = -1,
    show_empty: bool = False,
    creation_height: int | None = None,
    scan_status_only: bool = False,
) -> None:
    """Show wallet info implementation.

    Args:
        display_gap: Max empty addresses shown beyond last used in extended view.
        gap_limit: BIP44 gap limit (trailing-empty threshold). Forwarded to
            ``WalletService`` for sync-time logic.
        scan_range: Initial descriptor scan range (the address-index lookahead
            window imported into Bitcoin Core). Forwarded to ``WalletService``
            and used by ``setup_descriptor_wallet`` on first-time setup. To
            widen the range for an already-imported wallet (e.g. one migrated
            from legacy joinmarket-clientserver), use ``jm-wallet rescan
            --scan-depth N``. See docs/technical/wallet-scanning.md.
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

    # Report the fidelity bond registry state. Loaded with the legacy
    # fallback *disabled* so this count matches what
    # ``wallet.sync_with_registered_bonds()`` (below) actually uses -- that
    # method always reads the strict per-wallet file. Using the (default)
    # legacy-fallback-enabled load here would let this log claim bonds that
    # sync then silently ignores, which is exactly how funded bonds went
    # missing after the per-wallet registry partition (#492): the legacy
    # shared file still had entries that were never migrated (pubkey/path
    # mismatch), so this line kept reporting them as "found" while sync
    # registered none of them.
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
    from jmwallet.wallet.bond_registry import load_registry

    wallet_fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase or "")
    bond_registry = load_registry(data_dir, wallet_fingerprint, allow_legacy_fallback=False)
    fidelity_bond_addresses: list[tuple[str, int, int]] = [
        (bond.address, bond.locktime, bond.index)
        for bond in bond_registry.bonds
        if bond.network == network
    ]
    if fidelity_bond_addresses:
        logger.info(f"Found {len(fidelity_bond_addresses)} fidelity bond(s) in registry")

    # Surface bonds stuck in the legacy shared file: they display here as
    # "found" would have, but sync will not use them until they are
    # migrated (automatic, on WalletService init, if their pubkey matches
    # this wallet) or manually recovered. This is display-only; it does not
    # change what gets synced.
    legacy_registry = load_registry(data_dir, wallet_fingerprint, allow_legacy_fallback=True)
    unmigrated = len(legacy_registry.bonds) - len(bond_registry.bonds)
    if unmigrated > 0:
        logger.warning(
            f"{unmigrated} bond(s) found only in the legacy shared registry "
            "(not yet claimed by this wallet); they will not be synced until "
            "migrated. If they belong to this wallet and are funded, a sync "
            "still recovers them automatically via canonical derivation; "
            "otherwise run 'jm-wallet recover-bonds' or 'jm-wallet import-bond'."
        )

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
    # (default 1000) and ``gap_limit`` is the BIP44 trailing-empty threshold.
    # Widening the range for an already-imported wallet (e.g. one migrated
    # from legacy joinmarket-clientserver) is done with ``jm-wallet rescan
    # --scan-depth N``. See docs/technical/wallet-scanning.md.
    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=5,
        gap_limit=gap_limit,
        scan_range=scan_range,
        passphrase=bip39_passphrase,
        data_dir=data_dir,
        max_sats_freeze_reuse=max_sats_freeze_reuse,
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

        # Bond-aware sync: loads the per-wallet bond registry and ensures each
        # registered bond's watch-only ``addr()`` descriptor is imported into
        # Bitcoin Core (and rescanned) before scanning. The previous
        # descriptor-*count* readiness check over-counted the base wallet
        # (Bitcoin Core records extra internal/external variants), so a bond
        # funded after the base wallet was set up was never imported and showed
        # as locked with 0 sats. Non-descriptor backends (neutrino) scan the
        # bond addresses directly inside this call.
        await wallet.sync_with_registered_bonds()

        # Update any pending transaction statuses
        # This safeguards against one-shot coinjoins that exited before confirmation
        await update_all_pending_transactions(
            backend, data_dir, wallet_fingerprint=wallet.wallet_fingerprint
        )

        # Show the wallet master fingerprint so users can pass it via
        # --wallet-fingerprint to cold-wallet bond commands.
        print(f"\nWallet fingerprint: {wallet.wallet_fingerprint}")

        # Spendable balance: get_total_balance() excludes frozen UTXOs (and,
        # with include_fidelity_bonds=False, fidelity bonds). It is therefore
        # the *spendable* total, not the grand total; frozen and FB amounts are
        # added back explicitly below for the wallet-wide summary and for the
        # column width.
        spendable_balance = await wallet.get_total_balance(include_fidelity_bonds=False)
        fb_balance = await wallet.get_fidelity_bond_balance(0)  # FB only in mixdepth 0
        # Calculate total frozen balance across all mixdepths (excluding FB)
        total_frozen = sum(
            u.value
            for utxos_list in wallet.utxo_cache.values()
            for u in utxos_list
            if u.frozen and not u.is_fidelity_bond
        )

        # Calculate formatting width for branch balances
        balance_width = len(f"{spendable_balance + total_frozen:,}")

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
            _show_extended_wallet_info(
                wallet,
                used_addresses,
                history_addresses,
                display_gap,
                show_empty=show_empty,
                balance_width=balance_width,
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

        # Show Total Balance with aligned columns and visual calculation
        unit_suffix = " sats"
        unit_suffix_width = len(unit_suffix)

        # Calculate total: spendable + frozen + all FBs (locked + expired)
        total_balance = spendable_balance + total_frozen + fb_balance
        total_str = f"{total_balance:,}{unit_suffix}"
        header = _colorize(f"{'Total Wallet Balance:':<35}", _ANSI_BOLD_YELLOW)
        print(f"\n{header}{total_str:>{balance_width + unit_suffix_width}}")

        # Subtract frozen UTXOs (not spendable)
        if total_frozen > 0:
            frozen_str = f"{total_frozen:,}{unit_suffix}"
            print(f"{'- Frozen UTXOs:':<35}{frozen_str:>{balance_width + unit_suffix_width}}")

        # Subtract Fidelity Bonds (locked and expired, not spendable)
        if fb_balance > 0:
            # Calculate time-locked FBs (not yet expired)
            import time

            current_time = int(time.time())
            bond_locked_total = sum(
                u.value
                for utxos in wallet.utxo_cache.values()
                for u in utxos
                if u.is_fidelity_bond and u.locktime and u.locktime > current_time
            )

            # Expired = Total FBs - Locked
            expired_balance = fb_balance - bond_locked_total

            if bond_locked_total > 0:
                locked_str = f"{bond_locked_total:,}{unit_suffix}"
                print(
                    f"{'- Fidelity Bonds Time-Locked:':<35}"
                    f"{locked_str:>{balance_width + unit_suffix_width}}"
                )

                expired_str = f"{expired_balance:,}{unit_suffix}"
                print(
                    f"{'- Fidelity Bonds Expired:':<35}"
                    f"{expired_str:>{balance_width + unit_suffix_width}}"
                )
            else:
                # Only expired bonds present
                expired_str = f"{expired_balance:,}{unit_suffix}"
                print(
                    f"{'- Fidelity Bonds Expired:':<35}"
                    f"{expired_str:>{balance_width + unit_suffix_width}}"
                )

        # Final spendable amount after all deductions
        spendable_str = f"{spendable_balance:,}{unit_suffix}"
        print(
            f"{'= Total Spendable Balance:':<35}"
            f"{spendable_str:>{balance_width + unit_suffix_width}}"
        )

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
    show_empty: bool = False,
    new_address_limit: int = 6,
    balance_width: int = 10,
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

    When an address has multiple UTXOs, each UTXO is listed separately
    (matching the behavior of freeze.py).
    """

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

        status_display: str = addr_info.status
        if addr_info.address in pending_addresses:
            status_display += " (pending)"
        elif addr_info.has_unconfirmed:
            status_display += " (unconfirmed)"

        unit_suffix = " sats"
        unit_suffix_width = len(unit_suffix)

        # Show individual UTXOs when multiple exist on same address
        if len(addr_info.utxos) > 1:
            # Show each UTXO with address only on first
            for i, utxo in enumerate(addr_info.utxos):
                # Append confirmation count for each UTXO
                if utxo.confirmations >= 5:
                    confs_display = "5+ conf"
                else:
                    confs_display = f"{utxo.confirmations} conf"
                utxo_status = f"{status_display} ({confs_display})"
                if utxo.frozen:
                    utxo_status += " [FROZEN]"

                balance_unit = f"{utxo.value:,}{unit_suffix}"

                if i == 0:
                    # First UTXO: show path and address
                    print(
                        f"{addr_info.path:<24}{addr_info.address:<44}"
                        f"{balance_unit:>{balance_width + unit_suffix_width}}  {utxo_status}"
                    )
                else:
                    # Subsequent UTXOs: indent only
                    print(
                        f"{'':<24}{'':<44}"
                        f"{balance_unit:>{balance_width + unit_suffix_width}}  {utxo_status}"
                    )
        elif len(addr_info.utxos) == 1:
            # Single UTXO - show as before with confirmations
            utxo = addr_info.utxos[0]
            if utxo.confirmations >= 5:
                confs_display = "5+ conf"
            else:
                confs_display = f"{utxo.confirmations} conf"
            status_display += f" ({confs_display})"
            if utxo.frozen:
                status_display += " [FROZEN]"

            balance_unit = f"{addr_info.balance:,}{unit_suffix}"
            print(
                f"{addr_info.path:<24}{addr_info.address:<44}"
                f"{balance_unit:>{balance_width + unit_suffix_width}}  {status_display}"
            )
        else:
            # No UTXOs (new/empty address) - show as before
            balance_unit = f"{addr_info.balance:,}{unit_suffix}"
            print(
                f"{addr_info.path:<24}{addr_info.address:<44}"
                f"{balance_unit:>{balance_width + unit_suffix_width}}  {status_display}"
            )

    return total_balance, hidden


def _show_extended_wallet_info(
    wallet: WalletService,
    used_addresses: set[str],
    history_addresses: dict[str, str],
    gap_limit: int,
    show_empty: bool = False,
    balance_width: int = 10,
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

    from jmwallet.history import get_pending_transactions
    from jmwallet.wallet.service import FIDELITY_BOND_BRANCH

    # Print legend for address statuses
    print(f"\n{_colorize('Address status legend:', _ANSI_BOLD_WHITE)}")
    print("  new           - Unused, safe for receiving")
    print("  deposit       - Address with funds from internal or external sources")
    print("  cj-out        - CoinJoin output (mixed funds)")
    print("  cj-change     - Change output from a CoinJoin (deanonymising, keep separate)")
    print("  non-cj-change - Regular change (not from CoinJoin)")
    print("  used-empty    - Previously used, now empty (do not reuse)")
    print("  flagged       - Shared with peers but tx failed (do not reuse)")
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

    print("-" * 89)
    for md in range(wallet.mixdepth_count):
        # Get account zpub (BIP84 format for native segwit)
        zpub = wallet.get_account_zpub(md)

        print()  # Visual separator before mixdepths 0-4

        print(f"{_colorize(f'Mixdepth {md}', _ANSI_BOLD_CYAN)}\t{zpub}")

        # External addresses (receive / deposit)
        ext_addresses = wallet.get_address_info_for_mixdepth(
            md, 0, gap_limit, used_addresses, history_addresses
        )
        # Get the external branch zpub path
        ext_path = f"m/84'/{0 if wallet.network == 'mainnet' else 1}'/{md}'/0"
        print(f"{_colorize('external addresses', _ANSI_CYAN)}\t{ext_path}\t{zpub}")

        ext_balance = 0
        ext_balance, ext_hidden = _print_branch_addresses(
            ext_addresses,
            pending_addresses,
            show_empty=show_empty,
            balance_width=balance_width,
        )

        # Calculate frozen balance for external branch (change=0)
        ext_frozen = sum(
            u.value
            for u in wallet.utxo_cache.get(md, [])
            if u.frozen
            and not u.is_fidelity_bond
            and wallet.address_cache.get(u.address, (None, None, None))[1] == 0
        )

        if ext_hidden:
            print(
                f"\t\t\t({ext_hidden} empty addresses hidden; "
                f"to display use CLI and pass --show-empty)"
            )
        print(f"Balance: {ext_balance:,} sats (spendable: {ext_balance - ext_frozen:,} sats)")

        # Internal addresses (change / CJ output)
        int_addresses = wallet.get_address_info_for_mixdepth(
            md, 1, gap_limit, used_addresses, history_addresses
        )
        int_path = f"m/84'/{0 if wallet.network == 'mainnet' else 1}'/{md}'/1"
        print(f"{_colorize('internal addresses', _ANSI_CYAN)}\t{int_path}")

        int_balance, int_hidden = _print_branch_addresses(
            int_addresses,
            pending_addresses,
            show_empty=show_empty,
            balance_width=balance_width,
        )

        # Calculate frozen balance for internal branch (change=1)
        int_frozen = sum(
            u.value
            for u in wallet.utxo_cache.get(md, [])
            if u.frozen
            and not u.is_fidelity_bond
            and wallet.address_cache.get(u.address, (None, None, None))[1] == 1
        )

        if int_hidden:
            print(
                f"\t\t\t({int_hidden} empty addresses hidden; "
                f"to display use CLI and pass --show-empty)"
            )
        print(f"Balance: {int_balance:,} sats (spendable: {int_balance - int_frozen:,} sats)")

        # Fidelity bond branch (only for mixdepth 0)
        bond_addresses: list = []  # Initialize for type checker
        if md == 0:
            bond_addresses = wallet.get_fidelity_bond_addresses_info(gap_limit)
            if bond_addresses:
                bond_path = (
                    f"m/84'/{0 if wallet.network == 'mainnet' else 1}'/0'/{FIDELITY_BOND_BRANCH}"
                )
                print(f"{_colorize('fidelity bond addresses', _ANSI_CYAN)}\t{bond_path}\t{zpub}")

                bond_balance = 0
                bond_locked = 0  # Locked balance (not yet expired)
                import time

                current_time = int(time.time())

                for addr_info in bond_addresses:
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
                            locktime_str += " [FB TIME-LOCKED]"
                        else:
                            locktime_str += " [FB EXPIRED]"

                    # Show individual UTXOs when multiple exist on same FB address
                    if len(addr_info.utxos) > 1:
                        addr_len = len(addr_info.address)
                        for i, utxo in enumerate(addr_info.utxos):
                            utxo_status = locktime_str
                            # Confirmation count per UTXO
                            if utxo.confirmations == 0:
                                utxo_status += " (unconfirmed)"
                            elif utxo.confirmations >= 5:
                                utxo_status += " (5+ conf)"
                            else:
                                utxo_status += f" ({utxo.confirmations} conf)"

                            if i == 0:
                                print(
                                    f"{addr_info.path:<24}\t{addr_info.address}\t"
                                    f"{utxo.value:,} sats\t{utxo_status}"
                                )
                            else:
                                print(
                                    f"{'':<24}\t{'':<{addr_len}}\t"
                                    f"{utxo.value:,} sats\t{utxo_status}"
                                )
                    elif len(addr_info.utxos) == 1:
                        utxo = addr_info.utxos[0]
                        # Confirmation count for single UTXO
                        # Note: For single UTXO we modify locktime_str directly
                        # since no copy is needed (not reused for other UTXOs)
                        if utxo.confirmations == 0:
                            locktime_str += " (unconfirmed)"
                        elif utxo.confirmations >= 5:
                            locktime_str += " (5+ conf)"
                        else:
                            locktime_str += f" ({utxo.confirmations} conf)"
                        print(
                            f"{addr_info.path:<24}\t{addr_info.address}\t"
                            f"{addr_info.balance:,} sats\t{locktime_str}"
                        )
                    else:
                        print(
                            f"{addr_info.path:<24}\t{addr_info.address}\t"
                            f"{addr_info.balance:,} sats\t{locktime_str}"
                        )

                # Show bond balance with detailed breakdown
                expired_balance = bond_balance - bond_locked

                if bond_locked > 0 and expired_balance > 0:
                    print(
                        f"Balance: {bond_balance:,} sats "
                        f"({bond_locked:,} time-locked + {expired_balance:,} expired) "
                        f"(spendable: 0 sats)"
                    )
                elif bond_locked > 0:
                    print(
                        f"Balance: {bond_balance:,} sats "
                        f"({bond_locked:,} time-locked) "
                        f"(spendable: 0 sats)"
                    )
                else:
                    print(
                        f"Balance: {bond_balance:,} sats "
                        f"({expired_balance:,} expired) "
                        f"(spendable: 0 sats)"
                    )

        # Total balance for mixdepth with spendable calculation
        total_md_balance = ext_balance + int_balance
        spendable_md = (ext_balance - ext_frozen) + (int_balance - int_frozen)

        # For mixdepth 0, add FB balance to total (FBs are not spendable)
        if md == 0 and bond_addresses:
            total_md_balance += sum(addr_info.balance for addr_info in bond_addresses)

        md_label = _colorize(f"Balance for mixdepth {md}:", _ANSI_CYAN)
        print(f"{md_label}\t{total_md_balance:,} sats (spendable: {spendable_md:,} sats)")
        print("-" * 89)


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
                "available, so values below it are clamped up automatically. "
                "Honored both on its own and together with --scan-depth."
            ),
        ),
    ] = 0,
    scan_depth: Annotated[
        int | None,
        typer.Option(
            "--scan-depth",
            help=(
                "Widen the descriptor address-index range to N per branch "
                "before rescanning (re-imports descriptors). Use this once for "
                "a wallet whose used addresses sit beyond the configured "
                "[wallet].scan_range. See the wallet scanning docs."
            ),
        ),
    ] = None,
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
    """Rescan the blockchain to repair a descriptor wallet's coverage.

    Two kinds of gap can leave the wallet unaware of its own coins:

    - Time coverage: Bitcoin Core has not scanned far enough back. Plain
      `jm-wallet rescan` (optionally `--start-height H`) re-scans blocks
      against the current descriptor range.
    - Index coverage: a used address sits beyond the imported address range
      (common for wallets migrated from legacy joinmarket-clientserver). Pass
      `--scan-depth N` to widen the range to N per branch, then rescan.
      `--scan-depth` can be combined with `--start-height H` to widen the
      range and only rescan from height H (defaults to genesis).

    Rescans are slow (20+ minutes on mainnet from genesis) but read-only. The
    scan runs server-side in Bitcoin Core, so Ctrl-C only stops the progress
    polling, not the scan; re-attach later with `jm-wallet info --scan-status`.
    See docs/technical/wallet-scanning.md.
    """
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

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
            scan_depth=scan_depth,
            gap_limit=settings.wallet.gap_limit,
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
        )
    )


async def _run_rescan(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str,
    start_height: int,
    creation_height: int | None,
    scan_depth: int | None = None,
    gap_limit: int = 20,
    max_sats_freeze_reuse: int = -1,
) -> None:
    """Implementation of ``jm-wallet rescan``.

    When ``scan_depth`` is set, descriptors are first re-imported at the
    wider range without scanning (index-coverage repair), then a block
    rescan from ``start_height`` is run so a user-supplied ``--start-height``
    is honored (it defaults to 0 = genesis). Otherwise a plain block rescan
    from ``start_height`` is run against the current descriptor range
    (time-coverage repair).
    """
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

        if scan_depth is not None:
            # Index-coverage repair: re-import descriptors at the wider range,
            # then rescan. This is the path for wallets whose used addresses
            # sit beyond the imported range (e.g. migrated from legacy
            # joinmarket-clientserver).
            #
            # The widening import itself uses rescan=False (timestamp="now"),
            # so it only registers the new addresses without scanning. The
            # actual rescan is then driven from ``effective_start`` so that a
            # user-supplied ``--start-height`` is honored (it was previously
            # ignored, forcing a full genesis scan). Without ``--start-height``
            # this still scans from genesis (start_height defaults to 0).
            from jmwallet.wallet.constants import MAX_DESCRIPTOR_RANGE
            from jmwallet.wallet.service import WalletService

            # Bitcoin Core rejects descriptor ranges spanning more than
            # MAX_DESCRIPTOR_RANGE indices with "Range is too large". Cap the
            # requested depth so the import succeeds instead of failing wholesale
            # and leaving the wallet without coverage.
            if scan_depth > MAX_DESCRIPTOR_RANGE:
                logger.warning(
                    f"--scan-depth {scan_depth} exceeds Bitcoin Core's "
                    f"per-descriptor range limit of {MAX_DESCRIPTOR_RANGE}; "
                    f"capping to {MAX_DESCRIPTOR_RANGE}. Addresses beyond index "
                    f"{MAX_DESCRIPTOR_RANGE - 1} cannot be tracked in a single "
                    "descriptor. See docs/technical/wallet-scanning.md."
                )
                scan_depth = MAX_DESCRIPTOR_RANGE

            if effective_start != start_height:
                print(
                    f"\nUsing wallet creation height {effective_start} "
                    f"(requested {start_height}) as the rescan floor."
                )
            start_desc = "genesis" if effective_start == 0 else f"height {effective_start}"
            print(
                f"\nWidening descriptor range to [0, {scan_depth - 1}] per branch "
                f"and rescanning from {start_desc}. This may take 20+ minutes "
                "on mainnet when scanning from genesis."
            )
            wallet = WalletService(
                mnemonic=mnemonic,
                backend=backend,
                network=backend_settings.network,
                mixdepth_count=5,
                gap_limit=gap_limit,
                scan_range=scan_depth,
                passphrase=bip39_passphrase,
                data_dir=backend_settings.data_dir,
                max_sats_freeze_reuse=max_sats_freeze_reuse,
            )
            # Register the wider range without scanning (rescan=False), then
            # run an explicit block rescan from the requested height below.
            await wallet.setup_descriptor_wallet(
                scan_range=scan_depth,
                rescan=False,
                check_existing=False,
                smart_scan=False,
                background_full_rescan=False,
            )
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
            return

        # Time-coverage repair: plain block rescan against the current range.
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
