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
from jmwallet.utxo_tui import (
    ADDRESS_COL_WIDTH as _FREEZE_ADDR_WIDTH,
)
from jmwallet.utxo_tui import (
    adjust_scroll,
    build_display_items,
    format_address_column,
    group_separator_parts,
    seek_selectable,
    utxo_display_sort_key,
)

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
        typer.Option("--mixdepth", "-m", help="Filter to a specific configured mixdepth"),
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
    """Interactively freeze/unfreeze UTXOs to exclude them from coin selection.

    Opens a TUI where you can toggle the frozen state of individual UTXOs.
    Frozen UTXOs are persisted in BIP-329 format and excluded from all
    automatic coin selection (taker, maker, and sweep operations).
    Changes take effect immediately on each toggle.

    Fidelity bonds (locked or expired) can be frozen like any other UTXO:
    freezing a still-locked bond keeps it out of the sweep fallback and coin
    control once its timelock expires. Bonds are tagged [FB-LOCKED] or
    [FB-EXPIRED] in the list; "freeze all"/"unfreeze all" include them.
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
            mnemonic_file=resolved.mnemonic_file,
            mixdepth_count=settings.wallet.mixdepth_count,
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
            reconstruct_history=settings.wallet.reconstruct_history,
        )
    )


async def _freeze_utxos(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str = "",
    mixdepth_filter: int | None = None,
    *,
    creation_height: int | None = None,
    mnemonic_file: Path | None = None,
    mixdepth_count: int = 5,
    max_sats_freeze_reuse: int = -1,
    reconstruct_history: bool = True,
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
            scan_start_height=backend_settings.scan_start_height,
            scan_lookback_blocks=backend_settings.scan_lookback_blocks,
        )
    else:
        raise ValueError(f"Unknown backend type: {backend_type}")

    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=mixdepth_count,
        passphrase=bip39_passphrase,
        data_dir=data_dir,
        max_sats_freeze_reuse=max_sats_freeze_reuse,
        reconstruct_history=reconstruct_history,
        mnemonic_file=mnemonic_file,
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

        if not all_utxos:
            md_msg = f" in mixdepth {mixdepth_filter}" if mixdepth_filter is not None else ""
            print(f"No UTXOs found{md_msg}.")
            return

        # Sort by display order (mixdepth, branch, then derivation path with
        # numeric components so indices >= 10 order correctly), matching the
        # interactive UTXO selector; branch groups stay contiguous this way.
        all_utxos.sort(key=utxo_display_sort_key)

        # Build the display list grouped by mixdepth/branch with labelled
        # separators (external/internal/bond groups).
        display_items = build_display_items(all_utxos, branch_groups=True)

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


def _unfreeze_utxos(wallet: WalletService, utxos: list[UTXOInfo]) -> int:
    """Unfreeze every frozen UTXO, fidelity bonds included.

    A frozen still-locked bond is frozen deliberately: the flag keeps it out
    of the sweep fallback and coin control once its timelock expires, so
    "unfreeze all" clears it like any other frozen flag.

    Returns:
        Number of UTXOs unfrozen.
    """
    unfrozen_count = 0

    for utxo in utxos:
        if not utxo.frozen:
            continue
        wallet.toggle_freeze_utxo(utxo.outpoint)
        unfrozen_count += 1

    return unfrozen_count


# Building the display list is shared with the interactive UTXO selector;
# the alias is kept under the old name for tests (mixdepth-only grouping).
_build_display_items = build_display_items


# Column header for the freeze TUI; shared between the renderer and the
# separator-line widths so everything stays aligned.
_FREEZE_COL_HEADER = (
    "  F  | MD | Address                                    "
    "|      Amount     | Confirmations | Outpoint"
)


def _seek_selectable(display_items: list[UTXOInfo | None], start: int, direction: int) -> int:
    """Return the nearest selectable index from ``start``.

    Skips only ``None`` separators: every UTXO (fidelity bonds included) is
    toggleable in the freeze manager.
    """
    return seek_selectable(display_items, start, direction, lambda _: True)


def _format_freeze_address(address: str, prev_address: str) -> str:
    """Format an address for the freeze TUI address column.

    Consecutive UTXOs sharing the same address render the address only once;
    subsequent rows show blanks so the column stays visually grouped. Long
    fidelity-bond addresses are truncated in the middle to fit the column.
    """
    return format_address_column(address, prev_address)


def _build_utxo_line(utxo: UTXOInfo, prev_address: str) -> str:
    """Build the single-row text for a UTXO in the freeze TUI."""
    # [!] highlights an unfrozen bond's special role: still-locked bonds are
    # unspendable on-chain, and expired bonds are auto-selected by the sweep
    # fallback and coin control. A frozen bond shows [F] like any frozen UTXO;
    # regular UTXOs show [F] or [ ].
    if utxo.is_fidelity_bond and not utxo.frozen:
        status = "[!]"
    elif utxo.frozen:
        status = "[F]"
    else:
        status = "[ ]"

    amount_str = f"{utxo.value:,} sats"
    conf_str = f"{utxo.confirmations:>8,} conf"
    md_str = f"m{utxo.mixdepth}"

    fb_indicator = ""
    if utxo.is_fidelity_bond:
        fb_indicator = " [FB-LOCKED]" if utxo.is_locked else " [FB-EXPIRED]"

    label_str = f" ({utxo.label})" if utxo.label else ""
    addr_str = _format_freeze_address(utxo.address, prev_address)
    outpoint = f"{utxo.txid[:8]}...:{utxo.vout}"

    return (
        f" {status} | {md_str:>2} | {addr_str:<{_FREEZE_ADDR_WIDTH}} | {amount_str:>15} | "
        f"{conf_str} | {outpoint}{fb_indicator}{label_str}"
    )


def _draw_separator(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    pos: int,
    row: int,
    width: int,
) -> None:
    """Draw the labelled separator introducing the group at ``pos``.

    A new mixdepth gets its full title in bold cyan; a branch change within
    the same mixdepth replaces the title with placeholder dashes and keeps
    the branch name column-flush with the title above. The rest of the line
    is padded with plain dashes; a trailing separator without a following
    group renders as a plain dash line.
    """
    import curses

    end_col = width - 1
    try:
        parts = group_separator_parts(display_items, pos)
        if parts is None:
            stdscr.addstr(row, 1, "-" * (end_col - 1), curses.A_NORMAL)
            return
        title, em_dash, branch, full_title = parts
        col = 1
        if full_title:
            stdscr.addstr(row, col, title, curses.color_pair(1) | curses.A_BOLD)
        else:
            stdscr.addstr(row, col, "-" * len(title), curses.A_NORMAL)
        col += len(title)
        stdscr.addstr(row, col, em_dash, curses.color_pair(1))
        col += len(em_dash)
        if col + len(branch) + 1 <= end_col:
            stdscr.addstr(row, col, f"{branch} ", curses.color_pair(1))
            col += len(branch) + 1
        if col < end_col:
            stdscr.addstr(row, col, "-" * (end_col - col), curses.A_NORMAL)
    except curses.error:
        pass


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
    """Draw the visible UTXO rows (blank rows and labelled group separators)."""
    import curses

    prev_address = ""
    for i, item in enumerate(display_items):
        if i < scroll_offset or i >= scroll_offset + list_height:
            continue

        display_row = list_start + (i - scroll_offset)
        if display_row >= height - 3:
            break

        # Two consecutive None entries encode a blank border row followed by
        # the labelled separator of a new mixdepth; a single None is a
        # branch-only separator.
        if item is None:
            next_is_none = i + 1 < len(display_items) and display_items[i + 1] is None
            if not next_is_none:
                _draw_separator(stdscr, display_items, i, display_row, width)
            prev_address = ""  # Reset the address dedup at every group break
            continue

        line = _build_utxo_line(item, prev_address)
        prev_address = item.address

        if len(line) > width - 1:
            line = line[: width - 4] + "..."

        if i == cursor_pos:
            attr = curses.color_pair(2) | curses.A_REVERSE
        elif item.frozen:
            attr = curses.color_pair(3) | curses.A_BOLD
        elif item.is_fidelity_bond:
            attr = curses.color_pair(5)
        else:
            attr = curses.color_pair(4)

        try:
            stdscr.addstr(display_row, 0, line[: width - 1], attr)
        except curses.error:
            pass


def _format_freeze_footer_stats(utxo_items: list[UTXOInfo]) -> str:
    """Build the footer statistics lines for the freeze TUI.

    Two lines, newline-separated; regular UTXOs and fidelity bonds are
    never mixed. Line 1 covers non-bond UTXOs (Total | Frozen | Spendable)
    and is labeled ``Total(w/o FBs)`` when bonds exist, plain ``Total``
    when they don't. Line 2 covers fidelity bonds and is omitted entirely
    when the wallet has none: Spendable counts expired, unfrozen bonds
    (the sweep fallback and coin control auto-select those), while
    Frozen/Locked collects every bond that is frozen or still timelocked.
    Each line's segments sum to that line's total.
    """

    def _utxo_word(count: int) -> str:
        return "UTXOs" if count != 1 else "UTXO"

    def _format_category(value: int, count: int) -> str:
        """Format category with value and optional UTXO count in parentheses."""
        if value == 0:
            return f"{value:,} sats"
        return f"{value:,} sats ({count} {_utxo_word(count)})"

    def _stats(us: list[UTXOInfo]) -> tuple[int, int]:
        return (sum(u.value for u in us), len(us))

    regular = [u for u in utxo_items if not u.is_fidelity_bond]
    bonds = [u for u in utxo_items if u.is_fidelity_bond]

    total_value, total_count = _stats(regular)
    frozen_value, frozen_count = _stats([u for u in regular if u.frozen])
    spendable_value, spendable_count = _stats([u for u in regular if not u.frozen])

    total_label = "Total(w/o FBs)" if bonds else "Total"
    lines = [
        f" {total_label}: {_format_category(total_value, total_count)} | "
        f"Frozen: {_format_category(frozen_value, frozen_count)} | "
        f"Spendable: {_format_category(spendable_value, spendable_count)}"
    ]

    if bonds:
        fb_total_value, fb_total_count = _stats(bonds)
        fb_unavailable_value, fb_unavailable_count = _stats(
            [u for u in bonds if u.frozen or u.is_locked]
        )
        fb_spendable_value, fb_spendable_count = _stats(
            [u for u in bonds if not u.frozen and not u.is_locked]
        )
        lines.append(
            f" Fidelity Bonds: {_format_category(fb_total_value, fb_total_count)} | "
            f"Frozen/Locked: {_format_category(fb_unavailable_value, fb_unavailable_count)} | "
            f"Spendable: {_format_category(fb_spendable_value, fb_spendable_count)}"
        )

    return "\n".join(lines)


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

    stats_lines = _format_freeze_footer_stats(utxo_items).split("\n")
    legend = " Space/Tab: toggle | j/k: navigate | a: freeze all | n: unfreeze all | q: exit"

    # Layout grows upward from the legend: stats line(s), then the separator.
    separator_row = height - 3 - len(stats_lines)
    stdscr.addstr(separator_row, 0, "-" * min(len(_FREEZE_COL_HEADER) + 5, width - 1))

    if error_active and error_message:
        try:
            stdscr.addstr(
                height - 1, 0, f" ERROR: {error_message}"[: width - 1], curses.color_pair(3)
            )
        except curses.error:
            pass

    stdscr.attron(curses.A_BOLD)
    try:
        for i, line in enumerate(stats_lines):
            stdscr.addstr(separator_row + 1 + i, 0, line[: width - 1])
        stdscr.addstr(height - 2, 0, legend[: width - 1])
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
    # Reserve the footer separator, stats, legend, and error line before
    # sizing the scrollable list. Bonds need a second stats line.
    has_bonds = any(u is not None and u.is_fidelity_bond for u in display_items)
    separator_row = height - (5 if has_bonds else 4)
    list_height = separator_row - list_start

    # Keep the cursor on screen.
    scroll_offset = adjust_scroll(cursor_pos, scroll_offset, list_height)

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

    # Fidelity bonds toggle like any other UTXO, including still-locked ones:
    # a frozen locked bond stays out of the sweep fallback and coin control
    # once its timelock expires.
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
    """Freeze every unfrozen UTXO, fidelity bonds included."""
    try:
        for item in display_items:
            if item is None or item.frozen:
                continue
            wallet.toggle_freeze_utxo(item.outpoint)
    except OSError as e:
        state.set_error(f"Failed to persist freeze state: {e}")


def _unfreeze_all_utxos(
    state: _FreezeTUIState, display_items: list[UTXOInfo | None], wallet: WalletService
) -> None:
    """Unfreeze every frozen UTXO, fidelity bonds included."""
    try:
        utxo_items = [u for u in display_items if u is not None]
        _unfreeze_utxos(wallet, utxo_items)
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
        display_items: All UTXOs to display with labelled separators between
        mixdepth/branch groups.
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
    # Land the cursor on the first selectable (toggleable, non-separator) item
    # so it never starts on a mixdepth separator or a non-toggleable fidelity
    # bond (e.g. when mixdepth 0 holds only a bond).
    state.cursor_pos = _seek_selectable(display_items, 0, 1)

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
