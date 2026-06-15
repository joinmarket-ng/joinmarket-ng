"""
UTXO freeze/unfreeze commands.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
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
        typer.Option("--backend", "-b", help="Backend: descriptor_wallet | neutrino"),
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
            envvar="JOINMARKET_DATA_DIR",
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

    Still-locked fidelity bonds are shown as [FB-LOCKED] and cannot be toggled
    (they are already unspendable until their timelock expires). Expired
    fidelity bonds behave like regular UTXOs: they can be frozen/unfrozen, and
    "unfreeze all" will unfreeze them.
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
        resolved_mnemonic = resolved.mnemonic
        resolved_bip39_passphrase = resolved.bip39_passphrase
        resolved_creation_height = resolved.creation_height
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
            creation_height=resolved_creation_height,
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
        )
    )


async def _freeze_utxos(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str = "",
    mixdepth_filter: int | None = None,
    *,
    creation_height: int | None = None,
    max_sats_freeze_reuse: int = -1,
) -> None:
    """Interactive UTXO freeze/unfreeze implementation."""
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.service import WalletService

    network = backend_settings.network
    backend_type = backend_settings.backend_type
    data_dir = backend_settings.data_dir

    # The wallet name is derived from the master fingerprint. Registered
    # fidelity bonds are loaded and imported by ``sync_with_registered_bonds``
    # below, so they do not need to be collected here.
    from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint

    wallet_fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase or "")

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

    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=5,
        passphrase=bip39_passphrase,
        data_dir=data_dir,
        max_sats_freeze_reuse=max_sats_freeze_reuse,
    )

    try:
        # Bond-aware sync (same path as the daemon and ``jm-wallet info``):
        # imports any registered fidelity bond's watch-only ``addr()``
        # descriptor into Bitcoin Core (and rescans) when missing, so a bond
        # funded after the base wallet was set up is visible (and freezable).
        # Detection is by the actual ``addr()`` descriptor set, not a
        # descriptor count. Non-descriptor backends (neutrino) scan the bond
        # addresses directly inside this call.
        await wallet.sync_with_registered_bonds()

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

        # Treat locked FBs as frozen regardless of explicit flag
        for utxo in all_utxos:
            if utxo.is_fidelity_bond and utxo.is_locked and not utxo.frozen:
                utxo.frozen = True

        if not all_utxos:
            md_msg = f" in mixdepth {mixdepth_filter}" if mixdepth_filter is not None else ""
            print(f"No UTXOs found{md_msg}.")
            return

        # Sort by derivation path (same order as wallet info extended)
        all_utxos.sort(key=lambda u: u.path)

        # Build the display list with blank-line separators between mixdepths.
        display_items = _build_display_items(all_utxos)

        # Check terminal
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            # Non-interactive: just show frozen status
            _show_freeze_status(all_utxos)
            return

        # Launch interactive TUI
        import curses

        curses.wrapper(_run_freeze_tui, display_items, wallet)

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


def _is_freeze_toggleable(utxo: UTXOInfo) -> bool:
    """Whether the user may toggle a UTXO's frozen flag in the freeze manager.

    Still-locked fidelity bonds are not toggleable: they cannot be spent until
    their timelock expires, so flipping their frozen flag is a confusing no-op.
    Everything else (regular UTXOs and *expired* fidelity bonds) is toggleable.
    """
    return not (utxo.is_fidelity_bond and utxo.is_locked)


def _unfreeze_non_locked_utxos(wallet: WalletService, utxos: list[UTXOInfo]) -> tuple[int, int]:
    """Unfreeze every frozen UTXO except still-locked fidelity bonds.

    A still-locked fidelity bond (``is_locked``) cannot be spent until its
    timelock expires, so unfreezing it has no effect and is skipped. An
    *expired* fidelity bond (timelock passed) is treated like a regular UTXO and
    unfrozen, so "unfreeze all" actually makes it spendable again.

    Returns:
        Tuple of (unfrozen_count, skipped_locked_bond_count).
    """
    unfrozen_count = 0
    skipped_locked_bonds = 0

    for utxo in utxos:
        if not utxo.frozen:
            continue
        if not _is_freeze_toggleable(utxo):
            skipped_locked_bonds += 1
            continue
        wallet.toggle_freeze_utxo(utxo.outpoint)
        unfrozen_count += 1

    return unfrozen_count, skipped_locked_bonds


def _build_display_items(utxos: list[UTXOInfo]) -> list[UTXOInfo | None]:
    """Insert ``None`` separators between mixdepths for the freeze TUI.

    ``utxos`` must already be sorted so that all UTXOs of a mixdepth are
    contiguous (the caller sorts by derivation path). A ``None`` entry is
    inserted between consecutive mixdepth groups; it renders as a separator
    line and is skipped during navigation.
    """
    display_items: list[UTXOInfo | None] = []
    current_md = -1
    for utxo in utxos:
        if utxo.mixdepth != current_md:
            current_md = utxo.mixdepth
            if display_items:
                display_items.append(None)
        display_items.append(utxo)
    return display_items


# Column header for the freeze TUI; shared between the renderer and the
# separator-line widths so everything stays aligned.
_FREEZE_COL_HEADER = (
    "  F  | MD | Address                                    "
    "|      Amount     | Confirmations | Outpoint"
)

# Address column width used to pad/collapse addresses for alignment.
_FREEZE_ADDR_WIDTH = 42


def _seek_selectable(display_items: list[UTXOInfo | None], start: int, direction: int) -> int:
    """Return the nearest selectable (non-``None``) index from ``start``.

    ``display_items`` contains ``None`` separators between mixdepths that must
    never be selectable. Starting at ``start`` (already clamped to a valid
    range by the caller) this walks in ``direction`` (``+1``/``-1``) skipping
    separators. If no selectable item is found in that direction, ``start`` is
    returned unchanged so the cursor stays put rather than landing on a
    separator.
    """
    pos = start
    while 0 <= pos < len(display_items) and display_items[pos] is None:
        pos += direction
    if 0 <= pos < len(display_items):
        return pos
    return start


def _format_freeze_address(address: str, prev_address: str) -> str:
    """Format an address for the freeze TUI address column.

    Consecutive UTXOs sharing the same address render the address only once;
    subsequent rows show blanks so the column stays visually grouped. Long
    fidelity-bond addresses are truncated in the middle to fit the column.
    """
    if address == prev_address:
        return " " * _FREEZE_ADDR_WIDTH
    if len(address) > _FREEZE_ADDR_WIDTH:
        return address[:20] + "..." + address[-19:]
    return address


def _build_utxo_line(utxo: UTXOInfo, prev_address: str) -> str:
    """Build the single-row text for a UTXO in the freeze TUI."""
    status = "[F]" if utxo.frozen else "[ ]"
    amount_str = f"{utxo.value:,} sats"
    conf_str = f"{utxo.confirmations:>8,} conf"
    md_str = f"m{utxo.mixdepth}"

    fb_indicator = ""
    if utxo.is_fidelity_bond:
        fb_indicator = " [FB-LOCKED]" if utxo.is_locked else " [FB]"

    label_str = f" ({utxo.label})" if utxo.label else ""
    addr_str = _format_freeze_address(utxo.address, prev_address)
    outpoint = f"{utxo.txid[:8]}...:{utxo.vout}"

    return (
        f" {status} | {md_str:>2} | {addr_str:<{_FREEZE_ADDR_WIDTH}} | {amount_str:>15} | "
        f"{conf_str} | {outpoint}{fb_indicator}{label_str}"
    )


def _draw_utxo_rows(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    cursor_pos: int,
    scroll_offset: int,
    list_start: int,
    list_height: int,
    height: int,
    width: int,
) -> None:
    """Draw the visible UTXO rows (and mixdepth separators)."""
    import curses

    separator = "-" * min(len(_FREEZE_COL_HEADER) + 5, width - 1)
    prev_address = ""
    for i, item in enumerate(display_items):
        if i < scroll_offset or i >= scroll_offset + list_height:
            continue

        display_row = list_start + (i - scroll_offset)
        if display_row >= height - 3:
            break

        # Separator line between mixdepths.
        if item is None:
            try:
                stdscr.addstr(display_row, 0, separator, curses.color_pair(1) | curses.A_DIM)
            except curses.error:
                pass
            prev_address = ""  # Reset on mixdepth change
            continue

        line = _build_utxo_line(item, prev_address)
        prev_address = item.address

        if len(line) > width - 1:
            line = line[: width - 4] + "..."

        if i == cursor_pos:
            attr = curses.color_pair(2) | curses.A_REVERSE
        elif item.frozen:
            attr = curses.color_pair(3)
        elif item.is_fidelity_bond:
            attr = curses.color_pair(5)
        else:
            attr = curses.color_pair(4)

        try:
            stdscr.addstr(display_row, 0, line[: width - 1], attr)
        except curses.error:
            pass


def _draw_freeze_footer(
    stdscr: curses.window,
    utxo_items: list[UTXOInfo],
    error_message: str | None,
    error_active: bool,
    height: int,
    width: int,
) -> None:
    """Draw the separator, optional error line, and footer help lines."""
    import curses

    frozen_count = sum(1 for u in utxo_items if u.frozen)
    total_frozen_value = sum(u.value for u in utxo_items if u.frozen)
    total_spendable_value = sum(u.value for u in utxo_items if not u.frozen)

    stdscr.addstr(height - 4, 0, "-" * min(len(_FREEZE_COL_HEADER) + 5, width - 1))

    if error_active and error_message:
        try:
            stdscr.addstr(
                height - 1, 0, f" ERROR: {error_message}"[: width - 1], curses.color_pair(3)
            )
        except curses.error:
            pass

    footer1 = (
        f" Frozen: {frozen_count}/{len(utxo_items)} UTXOs | "
        f"Frozen: {total_frozen_value:,} sats | "
        f"Spendable: {total_spendable_value:,} sats"
    )
    footer2 = " Space/Tab: toggle | j/k: navigate | a: freeze all | n: unfreeze all | q: exit"

    stdscr.attron(curses.A_BOLD)
    try:
        stdscr.addstr(height - 3, 0, footer1[: width - 1])
        stdscr.addstr(height - 2, 0, footer2[: width - 1])
    except curses.error:
        pass
    stdscr.attroff(curses.A_BOLD)


def _render_freeze_screen(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    cursor_pos: int,
    scroll_offset: int,
    error_message: str | None,
    error_active: bool,
) -> tuple[int, int]:
    """Render the whole freeze screen; return ``(scroll_offset, list_height)``."""
    import curses

    stdscr.clear()
    height, width = stdscr.getmaxyx()

    header = " — UTXO Freeze Manager —"
    stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
    stdscr.addstr(1, 0, header.center(width)[:width])
    stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

    stdscr.addstr(3, 0, _FREEZE_COL_HEADER[: width - 1])
    stdscr.addstr(4, 0, "-" * min(len(_FREEZE_COL_HEADER) + 5, width - 1))

    list_start = 5  # Data starts here (+1 blank line under the header rule)
    list_height = height - 6

    # Keep the cursor on screen.
    if cursor_pos < scroll_offset:
        scroll_offset = cursor_pos
    elif cursor_pos >= scroll_offset + list_height:
        scroll_offset = cursor_pos - list_height + 1

    _draw_utxo_rows(
        stdscr, display_items, cursor_pos, scroll_offset, list_start, list_height, height, width
    )

    utxo_items = [u for u in display_items if u is not None]
    _draw_freeze_footer(stdscr, utxo_items, error_message, error_active, height, width)

    stdscr.refresh()
    return scroll_offset, list_height


@dataclass
class _FreezeTUIState:
    """Mutable cursor/error state for the freeze TUI event loop."""

    cursor_pos: int = 0
    scroll_offset: int = 0
    error_message: str | None = None
    error_display_until: float = 0.0

    def set_error(self, message: str, duration: float = 5.0) -> None:
        import time

        self.error_message = message
        self.error_display_until = time.monotonic() + duration


def _toggle_selected_utxo(
    state: _FreezeTUIState, display_items: list[UTXOInfo | None], wallet: WalletService
) -> None:
    """Toggle freeze on the UTXO under the cursor and advance the cursor."""
    utxo = display_items[state.cursor_pos]
    if utxo is None:
        return

    # A still-locked fidelity bond cannot be spent until its timelock expires,
    # so toggling its frozen flag is a confusing no-op; skip it. Expired
    # fidelity bonds and regular UTXOs toggle normally.
    if not _is_freeze_toggleable(utxo):
        state.set_error("Locked fidelity bond cannot be (un)frozen; it is timelocked")
    else:
        try:
            wallet.toggle_freeze_utxo(utxo.outpoint)
        except OSError as e:
            state.set_error(f"Failed to persist freeze state: {e}")

    # Move cursor down to the next selectable item after the action.
    if state.cursor_pos < len(display_items) - 1:
        state.cursor_pos = _seek_selectable(display_items, state.cursor_pos + 1, 1)


def _freeze_all_utxos(
    state: _FreezeTUIState, display_items: list[UTXOInfo | None], wallet: WalletService
) -> None:
    """Freeze every toggleable UTXO (skipping still-locked fidelity bonds)."""
    try:
        for item in display_items:
            if item is None or not _is_freeze_toggleable(item):
                continue
            if not item.frozen:
                wallet.toggle_freeze_utxo(item.outpoint)
    except OSError as e:
        state.set_error(f"Failed to persist freeze state: {e}")


def _unfreeze_all_utxos(
    state: _FreezeTUIState, display_items: list[UTXOInfo | None], wallet: WalletService
) -> None:
    """Unfreeze every non-locked UTXO, reporting skipped locked bonds."""
    try:
        utxo_items = [u for u in display_items if u is not None]
        _, skipped_locked_bonds = _unfreeze_non_locked_utxos(wallet, utxo_items)
        if skipped_locked_bonds > 0:
            state.set_error(
                f"Skipped {skipped_locked_bonds} locked fidelity bond UTXO(s); "
                "kept frozen until timelock expires"
            )
    except OSError as e:
        state.set_error(f"Failed to persist freeze state: {e}")


def _handle_freeze_key(
    key: int,
    state: _FreezeTUIState,
    display_items: list[UTXOInfo | None],
    wallet: WalletService,
    list_height: int,
) -> bool:
    """Apply a key press to ``state``. Return ``True`` to keep running.

    Returning ``False`` signals the caller to exit the event loop.
    """
    import curses

    last = len(display_items) - 1

    if key == ord("q") or key == 27:  # q or Escape
        return False
    elif key == ord(" ") or key == ord("\t"):  # toggle
        _toggle_selected_utxo(state, display_items, wallet)
    elif key == curses.KEY_UP or key == ord("k"):
        state.cursor_pos = _seek_selectable(display_items, state.cursor_pos - 1, -1)
    elif key == curses.KEY_DOWN or key == ord("j"):
        state.cursor_pos = _seek_selectable(display_items, state.cursor_pos + 1, 1)
    elif key == curses.KEY_PPAGE:  # Page Up
        state.cursor_pos = _seek_selectable(
            display_items, max(0, state.cursor_pos - list_height), -1
        )
    elif key == curses.KEY_NPAGE:  # Page Down
        state.cursor_pos = _seek_selectable(
            display_items, min(last, state.cursor_pos + list_height), 1
        )
    elif key == ord("g"):  # Go to top
        state.cursor_pos = _seek_selectable(display_items, 0, 1)
    elif key == ord("G"):  # Go to bottom
        state.cursor_pos = _seek_selectable(display_items, last, -1)
    elif key == ord("a"):  # Freeze all
        _freeze_all_utxos(state, display_items, wallet)
    elif key == ord("n"):  # Unfreeze all
        _unfreeze_all_utxos(state, display_items, wallet)

    return True


def _run_freeze_tui(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    wallet: WalletService,
) -> None:
    """Run the curses-based UTXO freeze/unfreeze TUI.

    Changes are persisted immediately on each toggle via wallet.toggle_freeze_utxo().

    Args:
        stdscr: The curses window.
        display_items: All UTXOs to display with blank lines between mixdepths.
        wallet: WalletService instance for persisting freeze state.
    """
    import curses
    import time

    curses.curs_set(0)
    curses.use_default_colors()

    # Color pairs
    curses.init_pair(1, curses.COLOR_CYAN, -1)  # Header
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # Cursor line
    curses.init_pair(3, curses.COLOR_RED, -1)  # Frozen UTXOs
    curses.init_pair(4, curses.COLOR_GREEN, -1)  # Spendable UTXOs
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # Fidelity bond UTXOs

    state = _FreezeTUIState()

    while True:
        error_active = bool(state.error_message) and time.monotonic() < state.error_display_until
        if not error_active:
            state.error_message = None

        state.scroll_offset, list_height = _render_freeze_screen(
            stdscr,
            display_items,
            state.cursor_pos,
            state.scroll_offset,
            state.error_message,
            error_active,
        )

        key = stdscr.getch()
        if not _handle_freeze_key(key, state, display_items, wallet, list_height):
            return
