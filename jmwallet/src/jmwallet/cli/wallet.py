"""
Wallet management commands: import, generate, info, validate.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from jmcore.cli_common import (
    ResolvedBackendSettings,
    resolve_backend_settings,
    resolve_mnemonic,
    setup_cli,
    setup_logging,
)
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
) -> None:
    """Import an existing BIP39 mnemonic phrase to create/recover a wallet.

    Enter your existing mnemonic interactively with autocomplete support,
    or set the MNEMONIC environment variable.

    By default, saves to ~/.joinmarket-ng/wallets/default.mnemonic with password protection.

    Examples:
        jm-wallet import                          # Interactive input, 24 words
        jm-wallet import --words 12               # Interactive input, 12 words
        MNEMONIC="word1 word2 ..." jm-wallet import  # Via env var
        jm-wallet import -o my-wallet.mnemonic    # Custom output file
    """
    setup_logging()

    if word_count not in (12, 15, 18, 21, 24):
        logger.error(f"Invalid word count: {word_count}. Must be 12, 15, 18, 21, or 24.")
        raise typer.Exit(1)

    # Get mnemonic from env var or interactive input
    import os

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
        output_file = Path.home() / ".joinmarket-ng" / "wallets" / "default.mnemonic"

    # Check if file exists
    if output_file.exists() and not force:
        logger.warning(f"Wallet file already exists: {output_file}")
        if not typer.confirm("Overwrite existing wallet file?", default=False):
            typer.echo("Import cancelled")
            raise typer.Exit(0)

    # Get password for encryption
    password: str | None = None
    if prompt_password:
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
) -> None:
    """Generate a new BIP39 mnemonic phrase with secure entropy.

    By default, saves to ~/.joinmarket-ng/wallets/default.mnemonic with password protection.
    Use --no-save to only display the mnemonic without saving.
    """
    setup_logging()

    try:
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

        # Auto-enable save if output_file is specified (even if --no-save was used)
        should_save = save or output_file is not None

        if should_save:
            if output_file is None:
                output_file = Path.home() / ".joinmarket-ng" / "wallets" / "default.mnemonic"

            # Check if file already exists and prompt for confirmation
            if output_file.exists():
                logger.warning(f"Wallet file already exists: {output_file}")
                overwrite = typer.confirm("Overwrite existing wallet file?", default=False)
                if not overwrite:
                    typer.echo("Wallet generation cancelled")
                    raise typer.Exit(0)

            # Prompt for password if requested
            password: str | None = None
            if prompt_password:
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
        typer.Option(
            "--backend", "-b", help="Backend: scantxoutset | descriptor_wallet | neutrino"
        ),
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
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """Display wallet information and balances by mixdepth."""
    settings = setup_cli(log_level)

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
            gap_limit=gap,
        )
    )


async def _show_wallet_info(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str = "",
    extended: bool = False,
    gap_limit: int = 6,
) -> None:
    """Show wallet info implementation."""
    from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
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

    # Load fidelity bond addresses from registry
    from jmwallet.wallet.bond_registry import load_registry

    bond_registry = load_registry(data_dir)
    fidelity_bond_addresses: list[tuple[str, int, int]] = [
        (bond.address, bond.locktime, bond.index)
        for bond in bond_registry.bonds
        if bond.network == network
    ]
    if fidelity_bond_addresses:
        logger.info(f"Found {len(fidelity_bond_addresses)} fidelity bond(s) in registry")

    # Create backend
    backend: BitcoinCoreBackend | DescriptorWalletBackend | NeutrinoBackend
    if backend_type == "neutrino":
        backend = NeutrinoBackend(neutrino_url=backend_settings.neutrino_url, network=network)
        logger.info("Waiting for neutrino to sync...")
        synced = await backend.wait_for_sync(timeout=300.0)
        if not synced:
            logger.error("Neutrino sync timeout")
            raise typer.Exit(1)
    elif backend_type == "descriptor_wallet":
        from jmwallet.backends.descriptor_wallet import (
            generate_wallet_name,
            get_mnemonic_fingerprint,
        )

        fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase or "")
        wallet_name = generate_wallet_name(fingerprint, network)
        backend = DescriptorWalletBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
            wallet_name=wallet_name,
        )
    elif backend_type == "scantxoutset":
        backend = BitcoinCoreBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
        )
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")

    # Create wallet with data_dir for history lookups
    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=5,
        passphrase=bip39_passphrase,
        data_dir=data_dir,
    )

    try:
        # Use descriptor wallet sync if available
        if backend_type == "descriptor_wallet":
            from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

            if isinstance(backend, DescriptorWalletBackend):
                # Check if base wallet is set up (without counting bonds)
                bond_count = len(fidelity_bond_addresses)
                base_wallet_ready = await wallet.is_descriptor_wallet_ready(fidelity_bond_count=0)
                full_wallet_ready = await wallet.is_descriptor_wallet_ready(
                    fidelity_bond_count=bond_count
                )

                if not base_wallet_ready:
                    # First time setup - import everything including bonds
                    logger.info("Descriptor wallet not set up. Setting up...")
                    await wallet.setup_descriptor_wallet(
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
            # Use standard sync (scantxoutset for scantxoutset, BIP157/158 for neutrino)
            await wallet.sync_all(fidelity_bond_addresses or None)

        # Update any pending transaction statuses
        # This safeguards against one-shot coinjoins that exited before confirmation
        await update_all_pending_transactions(backend, data_dir)

        from jmcore.bitcoin import format_amount

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

        # Clean up any stale pending transactions (older than 60 minutes)
        cleaned = cleanup_stale_pending_transactions(max_age_minutes=60, data_dir=data_dir)
        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} stale pending transaction(s)")

        pending = get_pending_transactions(data_dir)
        if pending:
            print(f"\nPending Transactions: {len(pending)}")
            for entry in pending:
                if entry.txid:
                    print(f"  {entry.txid[:16]}... - {entry.role} - {entry.confirmations} confs")
                else:
                    print(f"  [Broadcasting...] - {entry.role}")

        # Get history info for address status
        used_addresses = get_used_addresses(data_dir)
        history_addresses = get_address_history_types(data_dir)

        if extended:
            # Extended view with detailed address information
            print("\nJM wallet")
            _show_extended_wallet_info(wallet, used_addresses, history_addresses, gap_limit)
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
                # Get next address after the last used (highest used index + 1)
                addr, _ = wallet.get_next_after_last_used_address(md, used_addresses)
                print(f"  Mixdepth {md}: {addr}")

    finally:
        await wallet.close()


def _show_extended_wallet_info(
    wallet: WalletService,
    used_addresses: set[str],
    history_addresses: dict[str, str],
    gap_limit: int,
) -> None:
    """
    Display extended wallet information with detailed address listings.

    Mirrors the reference implementation's output format:
    - Shows zpub for each mixdepth (BIP84 native segwit format)
    - Lists external and internal addresses with derivation paths
    - Shows address status (deposit, cj-out, non-cj-change, new, etc.)
    - Shows balance per address and per branch
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
    print("  non-cj-change - Regular change (not from CoinJoin)")
    print("  used-empty  - Previously used, now empty (do not reuse)")
    print("  flagged     - Shared with peers but tx failed (do not reuse)")
    print()

    # Get pending transactions to mark addresses
    pending_txs = get_pending_transactions(wallet.data_dir)
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
        for addr_info in ext_addresses:
            btc_balance = sats_to_btc(addr_info.balance)
            ext_balance += addr_info.balance
            # Format: path  address  balance  status
            # Pad path to ensure consistent alignment regardless of index digits
            status_display: str = addr_info.status
            if addr_info.address in pending_addresses:
                status_display += " (pending)"
            elif addr_info.has_unconfirmed:
                status_display += " (unconfirmed)"
            if addr_info.address in frozen_addresses:
                status_display += " [FROZEN]"
            print(f"{addr_info.path:<24}{addr_info.address}\t{btc_balance:.8f}\t{status_display}")

        print(f"Balance:\t{sats_to_btc(ext_balance):.8f}")

        # Internal addresses (change / CJ output)
        int_addresses = wallet.get_address_info_for_mixdepth(
            md, 1, gap_limit, used_addresses, history_addresses
        )
        int_path = f"m/84'/{0 if wallet.network == 'mainnet' else 1}'/{md}'/1"
        print(f"internal addresses\t{int_path}")

        int_balance = 0
        for addr_info in int_addresses:
            btc_balance = sats_to_btc(addr_info.balance)
            int_balance += addr_info.balance
            # Pad path to ensure consistent alignment regardless of index digits
            status_str: str = addr_info.status
            if addr_info.address in pending_addresses:
                status_str += " (pending)"
            elif addr_info.has_unconfirmed:
                status_str += " (unconfirmed)"
            if addr_info.address in frozen_addresses:
                status_str += " [FROZEN]"
            print(f"{addr_info.path:<24}{addr_info.address}\t{btc_balance:.8f}\t{status_str}")

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
