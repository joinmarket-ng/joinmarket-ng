"""
UTXO freeze/unfreeze commands.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from jmcore.cli_common import (
    ResolvedBackendSettings,
    resolve_backend_settings,
    resolve_mnemonic,
    setup_cli,
)
from loguru import logger

from jmwallet.cli import app

if TYPE_CHECKING:
    import curses

    from jmwallet.wallet.models import UTXOInfo
    from jmwallet.wallet.service import WalletService


@app.command()
def freeze(
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
    mixdepth: Annotated[
        int | None,
        typer.Option("--mixdepth", "-m", help="Filter to a specific mixdepth (0-4)"),
    ] = None,
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
    """Interactively freeze/unfreeze UTXOs to exclude them from coin selection.

    Opens a TUI where you can toggle the frozen state of individual UTXOs.
    Frozen UTXOs are persisted in BIP-329 format and excluded from all
    automatic coin selection (taker, maker, and sweep operations).
    Changes take effect immediately on each toggle.
    """
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

    backend = resolve_backend_settings(
        settings,
        network=network,
        backend_type=backend_type,
        rpc_url=rpc_url,
        neutrino_url=neutrino_url,
        data_dir=data_dir,
    )

    asyncio.run(
        _freeze_utxos(
            resolved_mnemonic,
            backend,
            resolved_bip39_passphrase,
            mixdepth_filter=mixdepth,
        )
    )


async def _freeze_utxos(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str = "",
    mixdepth_filter: int | None = None,
) -> None:
    """Interactive UTXO freeze/unfreeze implementation."""
    from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.service import WalletService

    network = backend_settings.network
    backend_type = backend_settings.backend_type
    data_dir = backend_settings.data_dir

    # Load fidelity bond addresses from registry
    from jmwallet.wallet.bond_registry import load_registry

    bond_registry = load_registry(data_dir)
    fidelity_bond_addresses: list[tuple[str, int, int]] = [
        (bond.address, bond.locktime, bond.index) for bond in bond_registry.bonds
    ]

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

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=5,
        passphrase=bip39_passphrase,
        data_dir=data_dir,
    )

    try:
        # Sync wallet (same pattern as _show_wallet_info)
        if backend_type == "descriptor_wallet":
            if isinstance(backend, DescriptorWalletBackend):
                bond_count = len(fidelity_bond_addresses)
                base_ready = await wallet.is_descriptor_wallet_ready(fidelity_bond_count=0)
                full_ready = await wallet.is_descriptor_wallet_ready(fidelity_bond_count=bond_count)
                if not base_ready:
                    logger.info("Descriptor wallet not set up. Setting up...")
                    await wallet.setup_descriptor_wallet(
                        rescan=True,
                        fidelity_bond_addresses=fidelity_bond_addresses if bond_count else None,
                    )
                elif not full_ready and bond_count > 0:
                    logger.info("Importing fidelity bond addresses...")
                    await wallet.import_fidelity_bond_addresses(
                        fidelity_bond_addresses, rescan=True
                    )
                await wallet.sync_with_descriptor_wallet(
                    fidelity_bond_addresses=fidelity_bond_addresses if bond_count else None
                )
        else:
            await wallet.sync_all(fidelity_bond_addresses or None)

        # Collect all UTXOs (including frozen ones) across requested mixdepths
        all_utxos: list[UTXOInfo] = []
        if mixdepth_filter is not None:
            if mixdepth_filter < 0 or mixdepth_filter >= wallet.mixdepth_count:
                print(f"Error: mixdepth must be 0-{wallet.mixdepth_count - 1}")
                raise typer.Exit(1)
            all_utxos = wallet.utxo_cache.get(mixdepth_filter, [])
        else:
            for md in range(wallet.mixdepth_count):
                all_utxos.extend(wallet.utxo_cache.get(md, []))

        if not all_utxos:
            md_msg = f" in mixdepth {mixdepth_filter}" if mixdepth_filter is not None else ""
            print(f"No UTXOs found{md_msg}.")
            return

        # Sort by mixdepth, then value descending
        all_utxos.sort(key=lambda u: (u.mixdepth, -u.value))

        # Check terminal
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            # Non-interactive: just show frozen status
            _show_freeze_status(all_utxos)
            return

        # Launch interactive TUI
        import curses

        curses.wrapper(_run_freeze_tui, all_utxos, wallet)

        # Show summary after TUI exit
        frozen_count = sum(1 for u in all_utxos if u.frozen)
        total = len(all_utxos)
        print(f"\n{frozen_count}/{total} UTXO(s) frozen.")

    finally:
        await wallet.close()


def _show_freeze_status(utxos: list[UTXOInfo]) -> None:
    """Show freeze status for non-interactive mode (no terminal)."""
    from jmcore.bitcoin import format_amount

    current_md = -1
    for utxo in utxos:
        if utxo.mixdepth != current_md:
            current_md = utxo.mixdepth
            print(f"\nMixdepth {current_md}:")

        frozen_tag = " [FROZEN]" if utxo.frozen else ""
        fb_tag = ""
        if utxo.is_fidelity_bond:
            fb_tag = " [FB-LOCKED]" if utxo.is_locked else " [FB]"

        print(
            f"  {utxo.txid[:12]}...:{utxo.vout:<3} "
            f"{format_amount(utxo.value):>18} "
            f"{utxo.confirmations:>6} conf"
            f"{fb_tag}{frozen_tag}"
        )


def _run_freeze_tui(
    stdscr: curses.window,
    utxos: list[UTXOInfo],
    wallet: WalletService,
) -> None:
    """Run the curses-based UTXO freeze/unfreeze TUI.

    Changes are persisted immediately on each toggle via wallet.toggle_freeze_utxo().

    Args:
        stdscr: The curses window.
        utxos: All UTXOs to display (including already-frozen ones).
        wallet: WalletService instance for persisting freeze state.
    """
    import curses

    from jmcore.bitcoin import format_amount

    curses.curs_set(0)
    curses.use_default_colors()

    # Color pairs
    curses.init_pair(1, curses.COLOR_CYAN, -1)  # Header
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # Cursor line
    curses.init_pair(3, curses.COLOR_RED, -1)  # Frozen UTXOs
    curses.init_pair(4, curses.COLOR_GREEN, -1)  # Spendable UTXOs
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # Fidelity bond UTXOs

    cursor_pos = 0
    scroll_offset = 0
    error_message: str | None = None
    error_display_until: float = 0.0

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()

        # Header
        header = " UTXO Freeze Manager - Space: toggle freeze, q: exit "
        stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
        stdscr.addstr(0, 0, header.center(width)[:width])
        stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

        # Column headers
        col_header = " St  MD |             Amount |   Confs   | Outpoint"
        stdscr.addstr(1, 0, col_header[: width - 1])
        stdscr.addstr(2, 0, "-" * min(len(col_header) + 20, width - 1))

        # Calculate visible area
        list_start = 3
        list_height = height - 6

        # Adjust scroll
        if cursor_pos < scroll_offset:
            scroll_offset = cursor_pos
        elif cursor_pos >= scroll_offset + list_height:
            scroll_offset = cursor_pos - list_height + 1

        # Display UTXOs
        for i, utxo in enumerate(utxos):
            if i < scroll_offset or i >= scroll_offset + list_height:
                continue

            display_row = list_start + (i - scroll_offset)
            if display_row >= height - 3:
                break

            is_cursor = i == cursor_pos

            # Status indicator
            if utxo.frozen:
                status = "[F]"
            else:
                status = "[ ]"

            amount_str = format_amount(utxo.value)
            conf_str = f"{utxo.confirmations:>6} conf"
            md_str = f"m{utxo.mixdepth}"

            # Fidelity bond indicator
            fb_indicator = ""
            if utxo.is_fidelity_bond:
                fb_indicator = " [FB-LOCKED]" if utxo.is_locked else " [FB]"

            # Label
            label_str = f" ({utxo.label})" if utxo.label else ""

            outpoint = f"{utxo.txid[:8]}...:{utxo.vout}"
            line = (
                f" {status} {md_str:>3} | {amount_str:>18} | "
                f"{conf_str} | {outpoint}{fb_indicator}{label_str}"
            )

            if len(line) > width - 1:
                line = line[: width - 4] + "..."

            # Colors
            if is_cursor:
                attr = curses.color_pair(2) | curses.A_REVERSE
            elif utxo.frozen:
                attr = curses.color_pair(3) | curses.A_DIM
            elif utxo.is_fidelity_bond:
                attr = curses.color_pair(5)
            else:
                attr = curses.color_pair(4)

            try:
                stdscr.addstr(display_row, 0, line[: width - 1], attr)
            except curses.error:
                pass

        # Footer
        frozen_count = sum(1 for u in utxos if u.frozen)
        total_frozen_value = sum(u.value for u in utxos if u.frozen)
        total_spendable_value = sum(u.value for u in utxos if not u.frozen)

        stdscr.addstr(height - 3, 0, "-" * min(width - 1, 80))

        # Show error message if any (displayed for 3 seconds)
        import time

        if error_message and time.monotonic() < error_display_until:
            try:
                stdscr.addstr(
                    height - 4, 0, f" ERROR: {error_message}"[: width - 1], curses.color_pair(3)
                )
            except curses.error:
                pass
        else:
            error_message = None

        footer1 = (
            f" Frozen: {frozen_count}/{len(utxos)} UTXOs | "
            f"Frozen value: {format_amount(total_frozen_value)} | "
            f"Spendable: {format_amount(total_spendable_value)}"
        )
        footer2 = " Space/Tab: toggle | j/k: navigate | a: freeze all | n: unfreeze all | q: exit"

        stdscr.attron(curses.A_BOLD)
        try:
            stdscr.addstr(height - 2, 0, footer1[: width - 1])
            stdscr.addstr(height - 1, 0, footer2[: width - 1])
        except curses.error:
            pass
        stdscr.attroff(curses.A_BOLD)

        stdscr.refresh()

        # Handle input
        key = stdscr.getch()

        if key == ord("q") or key == 27:  # q or Escape
            return

        elif key == ord(" ") or key == ord("\t"):  # Space or Tab: toggle
            utxo = utxos[cursor_pos]
            try:
                wallet.toggle_freeze_utxo(utxo.outpoint)
            except OSError as e:
                error_message = f"Failed to persist freeze state: {e}"
                error_display_until = time.monotonic() + 5.0
            # Move cursor down after toggle
            if cursor_pos < len(utxos) - 1:
                cursor_pos += 1

        elif key == curses.KEY_UP or key == ord("k"):
            cursor_pos = max(0, cursor_pos - 1)

        elif key == curses.KEY_DOWN or key == ord("j"):
            cursor_pos = min(len(utxos) - 1, cursor_pos + 1)

        elif key == curses.KEY_PPAGE:  # Page Up
            cursor_pos = max(0, cursor_pos - list_height)

        elif key == curses.KEY_NPAGE:  # Page Down
            cursor_pos = min(len(utxos) - 1, cursor_pos + list_height)

        elif key == ord("g"):  # Go to top
            cursor_pos = 0

        elif key == ord("G"):  # Go to bottom
            cursor_pos = len(utxos) - 1

        elif key == ord("a"):  # Freeze all
            try:
                for utxo in utxos:
                    if not utxo.frozen:
                        wallet.toggle_freeze_utxo(utxo.outpoint)
            except OSError as e:
                error_message = f"Failed to persist freeze state: {e}"
                error_display_until = time.monotonic() + 5.0

        elif key == ord("n"):  # Unfreeze all
            try:
                for utxo in utxos:
                    if utxo.frozen:
                        wallet.toggle_freeze_utxo(utxo.outpoint)
            except OSError as e:
                error_message = f"Failed to persist freeze state: {e}"
                error_display_until = time.monotonic() + 5.0
