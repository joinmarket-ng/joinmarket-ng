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
    suspend_stderr_logging,
)
from loguru import logger

from jmwallet.cli import app
from jmwallet.utxo_tui import (
    _clip,
    _draw_footer,
    _is_address_reused,
    _minimum_table_width,
    _table_columns,
    _wrap_footer_fields,
    adjust_scroll,
    build_display_items,
    draw_resize_prompt,
    enrich_utxo_labels,
    format_utxo_line,
    group_separator_parts,
    seek_selectable,
    utxo_cursor_description,
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
    control once its timelock expires. Bonds are tagged FB-active/
    FB-expired in the Label column; "freeze all"/"unfreeze all" include them.
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

        # Same label enrichment the selector's caller (send.py) runs:
        # utxo_cache items carry no wallet labels, so without this the tag
        # column stays empty for every regular UTXO.
        enrich_utxo_labels(wallet.get_utxo_label_from_wallet, all_utxos)

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

        # Launch interactive TUI. Curses owns the terminal now: all log
        # output is suspended (see suspend_stderr_logging); freeze errors
        # are shown in the TUI's own footer note row.
        import curses

        with suspend_stderr_logging():
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


def _seek_selectable(display_items: list[UTXOInfo | None], start: int, direction: int) -> int:
    """Return the nearest selectable index from ``start``.

    Skips only ``None`` separators: every UTXO (fidelity bonds included) is
    toggleable in the freeze manager.
    """
    return seek_selectable(display_items, start, direction, lambda _: True)


_FREEZE_CONTROLS_HINT = (
    "Space/Tab: toggle | j/k: navigate | a: freeze all | n: unfreeze all | q/Esc: exit"
)


def _freeze_state_col(utxo: UTXOInfo) -> str:
    """State-column value of a UTXO in the freeze manager.

    Unlike the selector there are no session restrictions here (every UTXO is
    toggleable), so the state is simply frozen, locked (a still-timelocked
    fidelity bond) or spendable.
    """
    if utxo.frozen:
        return "frozen"
    if utxo.is_fidelity_bond and utxo.is_locked:
        return "locked"
    return "spendable"


def _draw_header(stdscr: curses.window, width: int, columns: list[tuple[str, int]]) -> str:
    """Draw the title bar and the table header, returning the header line.

    The returned header line sizes the row separators and right borders.
    """
    import curses

    title = " — UTXO Freeze Manager —"
    stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
    stdscr.addstr(1, 0, title.center(width)[:width])
    stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)

    header_line = "  | " + " | ".join(f"{name:^{size}}" for name, size in columns) + " |"
    stdscr.addstr(3, 0, header_line[: width - 1])
    header_sep_width = min(len(header_line) - 2, width - 3)
    stdscr.addstr(4, 2, "|", curses.A_NORMAL)
    stdscr.addstr(4, 3, "-" * (header_sep_width - 2), curses.A_NORMAL)
    right_pos = 3 + header_sep_width - 2
    if right_pos < width - 1:
        stdscr.addstr(4, right_pos, "|", curses.A_NORMAL)
    return header_line


def _draw_utxo_rows(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    cursor_pos: int,
    scroll_offset: int,
    list_height: int,
    list_start: int,
    width: int,
    sep_width: int,
    address_utxo_counts: dict[str, int],
    columns: list[tuple[str, int]],
) -> None:
    """Draw the visible UTXO rows with bordered separators, freeze marks and colors.

    Marks: [F] frozen, [!] an unfrozen fidelity bond, [ ] anything else. A
    frozen bond shows [F]; its Label/State columns still describe the bond.
    Rows share the selector's layout and palette (yellow cursor, bright red
    frozen and still-timelocked bonds, magenta expired bonds, RU! badges
    behind the right border).
    """
    import curses

    for i, item in enumerate(display_items):
        if i < scroll_offset or i >= scroll_offset + list_height:
            continue
        display_row = list_start + (i - scroll_offset)

        # Two consecutive None entries encode a blank border row followed by
        # the labelled separator of a new mixdepth; a single None is a
        # branch-only separator.
        if item is None:
            stdscr.addstr(display_row, 2, "|", curses.A_NORMAL)
            inner_width = sep_width - 2
            if i + 1 < len(display_items) and display_items[i + 1] is None:
                # Blank border-only row above a mixdepth title.
                pass
            else:
                parts = group_separator_parts(display_items, i)
                if parts is not None:
                    md_part, dash_sep, branch_part, full_title = parts
                    col = 3
                    if not full_title:
                        # Branch change: dashes strike out the "Mixdepth N"
                        # part; the branch name keeps its column and color.
                        stdscr.addstr(display_row, col, "-" * len(md_part), curses.A_NORMAL)
                        col += len(md_part)
                    else:
                        stdscr.addstr(
                            display_row, col, md_part, curses.color_pair(3) | curses.A_BOLD
                        )
                        col += len(md_part)
                    stdscr.addstr(display_row, col, dash_sep, curses.color_pair(3))
                    col += len(dash_sep)
                    if col + len(branch_part) + 1 <= 3 + inner_width:
                        stdscr.addstr(display_row, col, f"{branch_part} ", curses.color_pair(3))
                        col += len(branch_part) + 1
                    stdscr.addstr(display_row, col, "-" * (3 + inner_width - col), curses.A_NORMAL)
                else:
                    stdscr.addstr(display_row, 3, "-" * inner_width, curses.A_NORMAL)
            stdscr.addstr(display_row, sep_width + 1, "|", curses.A_NORMAL)
            continue

        if item.frozen:
            mark = "[F]"
        elif item.is_fidelity_bond:
            mark = "[!]"
        else:
            mark = "[ ]"
        line = f"| {mark} | " + format_utxo_line(
            item,
            width,
            None,
            width,
            columns=columns,
            mark_title="F",
            state_override=_freeze_state_col(item),
        )

        if i == cursor_pos:
            attr = curses.color_pair(2) | curses.A_REVERSE
        elif item.frozen or item.is_locked:
            # Frozen or still-timelocked bond: both are immutable from this
            # TUI, so both signal stop in bright red like the selector.
            attr = curses.color_pair(4) | curses.A_BOLD
        elif item.is_fidelity_bond:
            # Expired bond: spendable, but its address is published on the
            # network, so only magenta like the selector's unlocked bonds.
            attr = curses.color_pair(5)
        else:
            attr = curses.color_pair(1)  # Spendable UTXOs stay green

        stdscr.addstr(display_row, 2, "|", curses.A_NORMAL)
        stdscr.addstr(display_row, 3, line[1:-1], attr)
        right_border_pos = len(line) + 1
        stdscr.addstr(display_row, right_border_pos, "|", curses.A_NORMAL)
        if _is_address_reused(item, address_utxo_counts):
            stdscr.addstr(
                display_row, right_border_pos + 1, " RU!", curses.color_pair(4) | curses.A_BOLD
            )


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


def _freeze_footer_rows(
    width: int,
    utxo_items: list[UTXOInfo],
    cursor_item: UTXOInfo | None,
    address_utxo_counts: dict[str, int],
    error_message: str | None,
    error_active: bool,
) -> list[tuple[str, int]]:
    """Lay out the footer sections before reserving space for the coin rows.

    Freeze Details holds the per-category statistics (wrapped between
    fields), UTXO Status describes the coin under the cursor, and the note
    row carries an active error or an RU! warning (reserved even when empty
    so cursor moves do not resize the list).
    """
    import curses

    content_width = max(1, width - 3)
    separator = ("-" * content_width, curses.A_NORMAL)
    rows = [separator, ("Freeze Details", curses.A_BOLD)]
    for stats_line in _format_freeze_footer_stats(utxo_items).split("\n"):
        rows.extend(
            (line, curses.A_BOLD) for line in _wrap_footer_fields(stats_line.strip(), content_width)
        )
    rows.extend([separator, ("UTXO Status", curses.A_BOLD)])
    status_text = ""
    if cursor_item is not None:
        # "Out:" (short for Outpoint) keeps the full txid:vout visible; the
        # state column already lives in the table.
        status_text = f"Out: {cursor_item.txid}:{cursor_item.vout}"
        description = utxo_cursor_description(cursor_item)
        if description:
            status_text += f" | {description}"
    rows.append((_clip(status_text, content_width), curses.A_DIM))
    note = ""
    if error_active and error_message:
        note = f"ERROR: {error_message}"
    elif cursor_item is not None and _is_address_reused(cursor_item, address_utxo_counts):
        note = "RU! Warning: Address reuse reduces privacy"
    rows.append((_clip(note, content_width), curses.color_pair(4) | curses.A_BOLD))
    rows.extend([separator, ("Controls", curses.A_BOLD)])
    rows.extend(
        (line, curses.A_NORMAL)
        for line in _wrap_footer_fields(_FREEZE_CONTROLS_HINT, content_width)
    )
    rows.append(("", curses.A_NORMAL))
    return rows


def _render_freeze_screen(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    cursor_pos: int,
    scroll_offset: int,
    error_message: str | None,
    error_active: bool,
) -> tuple[int, int]:
    """Render the whole freeze screen; return ``(scroll_offset, list_height)``.

    With too little room for the framed layout nothing is drawn and the
    returned list height is 0; the event loop then shows the resize prompt.
    """
    stdscr.clear()
    height, width = stdscr.getmaxyx()
    utxo_items = [u for u in display_items if u is not None]
    columns = _table_columns(width, utxo_items, "F")

    # Address reuse detection: count UTXOs per address (shared RU! logic).
    address_utxo_counts: dict[str, int] = {}
    for item in utxo_items:
        address_utxo_counts[item.address] = address_utxo_counts.get(item.address, 0) + 1

    cursor_item = display_items[cursor_pos] if cursor_pos < len(display_items) else None
    footer_rows = _freeze_footer_rows(
        width, utxo_items, cursor_item, address_utxo_counts, error_message, error_active
    )

    list_start = 5
    list_height = height - list_start - len(footer_rows)
    if list_height < 1 or width < max(80, _minimum_table_width(columns)):
        return scroll_offset, 0

    # Keep the cursor on screen.
    scroll_offset = adjust_scroll(cursor_pos, scroll_offset, list_height, len(display_items))

    header_line = _draw_header(stdscr, width, columns)
    _draw_utxo_rows(
        stdscr,
        display_items,
        cursor_pos,
        scroll_offset,
        list_height,
        list_start,
        width,
        len(header_line) - 2,
        address_utxo_counts,
        columns,
    )
    _draw_footer(stdscr, height - len(footer_rows), footer_rows)
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

    # Palette aligned with the interactive UTXO selector; green stays the
    # "healthy" spendable color and red marks frozen coins, errors, RU!.
    curses.init_pair(1, curses.COLOR_GREEN, -1)  # Spendable UTXOs
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # Current cursor
    curses.init_pair(3, curses.COLOR_CYAN, -1)  # Title bar and group titles
    curses.init_pair(4, curses.COLOR_RED, -1)  # Frozen rows (bold), errors and RU! warnings
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # Unfrozen fidelity bonds

    state = _FreezeTUIState()
    # Land the cursor on the first selectable (toggleable, non-separator) item
    # so it never starts on a mixdepth separator or a non-toggleable fidelity
    # bond (e.g. when mixdepth 0 holds only a bond).
    state.cursor_pos = _seek_selectable(display_items, 0, 1)

    while True:
        error_active = bool(state.error_message) and time.monotonic() < state.error_display_until
        if not error_active:
            state.error_message = None

        try:
            state.scroll_offset, list_height = _render_freeze_screen(
                stdscr,
                display_items,
                state.cursor_pos,
                state.scroll_offset,
                state.error_message,
                error_active,
            )
        except curses.error:
            # Do not dispatch freeze actions after an interrupted repaint.
            draw_resize_prompt(stdscr, stdscr.getmaxyx()[1])
            if stdscr.getch() in (ord("q"), 27):
                return
            continue
        if list_height < 1:
            # Too little room for the framed layout: show the shared
            # resize prompt and ignore all input except q/Escape.
            draw_resize_prompt(stdscr, stdscr.getmaxyx()[1])
            if stdscr.getch() in (ord("q"), 27):
                return
            continue

        key = stdscr.getch()
        if not _handle_freeze_key(key, state, display_items, wallet, list_height):
            return
