"""
Interactive UTXO selector TUI.

Shows every UTXO in the wallet grouped by mixdepth and branch (fidelity
bonds first, then external, then internal addresses — the extended view's
section order) so the user can compare coins across the whole wallet before
picking which one(s) to spend. Groups are introduced by labelled separator
lines.

A spend can only draw from a single mixdepth, so the selector pins the
source mixdepth to the first selected UTXO: while anything is selected,
UTXOs in other mixdepths render as unselectable ``[-]`` rows. Deselecting
everything unpins the mixdepth again. Callers that already know the source
mixdepth can pass ``allowed_mixdepth`` to pin it up front (other mixdepths
are then shown for context only).
"""

from __future__ import annotations

import curses
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

from jmwallet.utxo_tui import (
    _draw_footer,
    _draw_frame_filler,
    _draw_help_overlay,
    _draw_hrule,
    _header_line,
    _help_overlay_rows,
    _is_address_reused,
    _minimum_table_width,
    _table_columns,
    _table_sep_width,
    _utxo_state_col,
    _wrap_footer_fields,
    adjust_scroll,
    align_control_columns,
    build_display_items,
    draw_resize_prompt,
    draw_status_block,
    format_utxo_line,
    group_separator_parts,
    maker_bot_status,
    seek_selectable,
    status_header_lines,
    utxo_cursor_description,
    utxo_display_sort_key,
    utxo_status_text,
    wait_for_help_close,
    wrap_status_text,
)
from jmwallet.wallet.rows import SELECTOR_STATE_TOKENS

if TYPE_CHECKING:
    from jmwallet.wallet.models import UTXOInfo


_CONTROLS_HINT = (
    "Space/Tab: toggle | j/k: navigate | s: select mixdepth | "
    "d: deselect all | Enter: confirm | ?: help | q/Esc: cancel"
)

# States that may ever be selected in a session. ``unconf`` is selectable
# only because direct sends run with min_confirmations == 0 (CoinJoins
# route 0-conf coins to ``immat`` instead); ``pend`` is catalog-only
# until the pending wiring lands and must never appear here.
_SELECTABLE_STATES: frozenset[str] = frozenset({"spend", "unconf"})


def _is_base_selectable(
    utxo: UTXOInfo,
    allowed_mixdepth: int | None,
    min_confirmations: int,
    excluded_outpoints: set[tuple[str, int]] | None = None,
    *,
    allow_fidelity_bonds: bool = True,
) -> bool:
    """Whether a UTXO may ever be selected in this session.

    Frozen UTXOs and still-locked fidelity bonds are never selectable, nor
    are UTXOs below ``min_confirmations`` or outside ``allowed_mixdepth``
    (when pinned by the caller), or in ``excluded_outpoints``. With
    ``allow_fidelity_bonds=False`` (CoinJoin context) expired bonds are
    excluded too. They are still displayed for context.
    """
    return (
        _utxo_state_col(
            utxo,
            excluded_outpoints,
            allowed_mixdepth,
            min_confirmations,
            allow_fidelity_bonds=allow_fidelity_bonds,
        )
        in _SELECTABLE_STATES
    )


_HELP_TITLE = " — UTXO Selector Help Screen —"


def _draw_header(
    stdscr: curses.window,
    width: int,
    columns: list[tuple[str, int]],
    status_lines: list[str],
) -> str:
    """Draw the title bar, status block and table header.

    The status block sits between the title rule and the header top edge
    (whiptail ``menu_header`` style: wallet name, fingerprint, maker bot
    status). ``status_lines`` is the prebuilt 1- or 2-line cascade from
    :func:`status_header_lines`; the returned header line is used to size
    the row separators and the footer separators.
    """
    header = " — UTXO Selector —"
    stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
    stdscr.addstr(1, 0, header.center(width)[:width])
    stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)

    header_line = _header_line(columns)
    # Geometry: rule(2), status rows(3..2+R), block bottom rule(3+R),
    # header(4+R), header bottom rule(5+R), list from 6+R.
    header_row = 4 + len(status_lines)
    stdscr.addstr(header_row, 0, header_line[:width])
    if "RU!" in header_line:
        # Overdraw the flush RU! header cell in the warning color.
        ru_col = header_line.index("RU!")
        stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
        stdscr.addstr(header_row, ru_col, "RU!")
        stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)
    header_sep_width = min(len(header_line) - 1, width - 1)
    # The box's top edge: a rule between the title and the status block.
    _draw_hrule(stdscr, 2, header_sep_width)
    status_block_end = draw_status_block(stdscr, 3, status_lines, header_sep_width)
    # The header's own top edge below the status block.
    _draw_hrule(stdscr, 3 + status_block_end, header_sep_width)
    _draw_hrule(stdscr, header_row + 1, header_sep_width)
    return header_line


def _draw_utxo_rows(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    selected: set[int],
    cursor_pos: int,
    scroll_offset: int,
    list_height: int,
    list_start: int,
    width: int,
    sep_width: int,
    excluded_outpoints: set[tuple[str, int]],
    address_utxo_counts: dict[str, int],
    is_selectable: Callable[[int], bool],
    columns: list[tuple[str, int]],
    pinned: int | None,
    min_confirmations: int,
    allow_fidelity_bonds: bool = True,
) -> None:
    """Draw the visible UTXO rows with separators, marks and colors."""
    for i, item in enumerate(display_items):
        if i < scroll_offset or i >= scroll_offset + list_height:
            continue

        display_row = list_start + (i - scroll_offset)

        if item is None:
            stdscr.addstr(display_row, 0, "|", curses.A_NORMAL)
            inner_width = sep_width - 1
            if i + 1 < len(display_items) and display_items[i + 1] is None:
                # Blank border-only row above a mixdepth title.
                pass
            else:
                parts = group_separator_parts(display_items, i)
                if parts is not None:
                    md_part, dash_sep, branch_part, full_title = parts
                    col = 2
                    if not full_title:
                        # Second/third branch of this mixdepth: dashes strike
                        # out only the "Mixdepth N" part; the em dash and the
                        # branch name keep their place and color, flush with
                        # the full title above.
                        stdscr.addstr(display_row, col, "-" * len(md_part), curses.A_NORMAL)
                        col += len(md_part)
                    else:
                        # Same colors as the extended view: "Mixdepth N" bold
                        # cyan, em dash and branch name plain cyan; the dashes
                        # stay white.
                        stdscr.addstr(
                            display_row, col, md_part, curses.color_pair(3) | curses.A_BOLD
                        )
                        col += len(md_part)
                    stdscr.addstr(display_row, col, dash_sep, curses.color_pair(3))
                    col += len(dash_sep)
                    if col + len(branch_part) + 1 <= sep_width - 1:
                        stdscr.addstr(display_row, col, f"{branch_part} ", curses.color_pair(3))
                        col += len(branch_part) + 1
                    stdscr.addstr(display_row, col, "-" * (sep_width - 1 - col), curses.A_NORMAL)
                else:
                    stdscr.addstr(display_row, 1, "-" * inner_width, curses.A_NORMAL)
            stdscr.addstr(display_row, sep_width, "|", curses.A_NORMAL)
            continue

        is_selected = i in selected
        is_cursor = i == cursor_pos

        if not is_selectable(i):
            mark = "[-]"
        elif is_selected:
            mark = "[x]"
        else:
            mark = "[ ]"
        line = f"| {mark} | " + format_utxo_line(
            item,
            width,
            excluded_outpoints,
            width,
            columns=columns,
            allowed_mixdepth=pinned,
            min_confirmations=min_confirmations,
            allow_fidelity_bonds=allow_fidelity_bonds,
            reused=_is_address_reused(item, address_utxo_counts),
        )

        # Apply colors
        if is_cursor:
            attr = curses.color_pair(2) | curses.A_REVERSE
        elif is_selected:
            attr = curses.color_pair(1) | curses.A_BOLD
        elif (
            item.frozen
            or (item.is_fidelity_bond and item.is_locked)
            or (item.txid, item.vout) in excluded_outpoints
        ):
            # Frozen, in-use, or still-locked bonds are red and unselectable.
            attr = curses.color_pair(4) | curses.A_DIM
        elif not is_selectable(i):
            # Immature, outside the (pinned) source mixdepth, or a bond in a
            # CoinJoin context (no-cj)
            attr = curses.A_DIM
        elif item.is_fidelity_bond:
            # Unlocked FB - magenta (can be spent but should be careful)
            attr = curses.color_pair(5)
        else:
            attr = curses.A_NORMAL

        stdscr.addstr(display_row, 0, "|", curses.A_NORMAL)
        stdscr.addstr(display_row, 1, line[1:-1], attr)
        stdscr.addstr(display_row, len(line) - 1, "|", curses.A_NORMAL)
        if _is_address_reused(item, address_utxo_counts):
            # Overdraw the flush RU! cell (inside the border) in red bold.
            ru_col = len(line) - 4
            stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
            stdscr.addstr(display_row, ru_col, "RU!")
            stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)


def _build_footer_lines(
    display_items: list[UTXOInfo | None],
    selected: set[int],
    base_selectable: set[int],
    is_selectable: Callable[[int], bool],
    target_amount: int,
    allowed_mixdepth: int | None,
    pinned: int | None,
    cursor_pos: int,
    excluded_outpoints: set[tuple[str, int]],
    address_utxo_counts: dict[str, int],
    min_confirmations: int = 0,
    allow_fidelity_bonds: bool = True,
    notice: str = "",
) -> tuple[str, str, str, str]:
    """Build the footer text lines, returning (line1, line2, line4, ru_note).

    ``line1`` is the selection summary, ``line2`` the (optional) pinned source
    mixdepth, ``line4`` the cursor status with the State column first and
    ``ru_note`` an optional address-reuse warning. A transient ``notice``
    (feedback for a blocked action) takes precedence over the reuse warning.
    """
    selected_utxos = [display_items[i] for i in selected]
    total_selected = sum(u.value for u in selected_utxos if u is not None)
    total_str = f"{total_selected:,} sats"
    selectable_count = sum(1 for i in base_selectable if is_selectable(i))

    if target_amount > 0:
        remaining = target_amount - total_selected
        target_str = f"{target_amount:,} sats"
        if remaining > 0:
            footer_line1 = f"Target: {target_str} | Need: {remaining:,} sats more |"
        elif remaining == 0:
            footer_line1 = f"Target: {target_str} | Target met |"
        else:
            footer_line1 = f"Target: {target_str} | Excess: {-remaining:,} sats |"
    else:
        footer_line1 = "Sweep mode |"

    footer_line1 += f" Selected: {len(selected)}/{selectable_count} UTXOs | Total: {total_str}"

    footer_line2 = ""
    if pinned is not None:
        if allowed_mixdepth is not None:
            footer_line2 = f"Source mixdepth: m{pinned}"
        else:
            footer_line2 = f"Source mixdepth: m{pinned} (deselect all UTXOs to change)"

    # Context-sensitive status line for the item under cursor
    cursor_item = display_items[cursor_pos] if cursor_pos < len(display_items) else None
    footer_line4 = ""
    ru_note = ""
    if cursor_item is not None:
        footer_line4 = utxo_cursor_description(cursor_item)

        # Keep the state and restriction visible even when a long label is clipped.
        state_col = _utxo_state_col(
            cursor_item,
            excluded_outpoints,
            pinned,
            min_confirmations,
            allow_fidelity_bonds=allow_fidelity_bonds,
        )
        # Shared payload: line 1 is the outpoint (wrapped only when it is
        # too wide for the box); the explanation line below carries the
        # cursor description plus the WHY behind a non-selectable state.
        extra: list[str] = []
        if state_col == "immat":
            extra.append(f"requires {min_confirmations} confirmations")
        elif state_col == "oth-md":
            extra.append(f"source is m{pinned}")
        elif state_col == "no-cj":
            extra.append(
                "fidelity bonds cannot be used in CoinJoins; "
                "redeem with jm-wallet send --select-utxos"
            )
        footer_line4 = utxo_status_text(cursor_item, extra)
        # RU note for address reuse (same logic as the table's RU! indicator).
        is_reused = _is_address_reused(cursor_item, address_utxo_counts)
        if is_reused:
            ru_note = "RU! Warning: Address reuse reduces privacy"

    if notice:
        ru_note = notice

    return footer_line1, footer_line2, footer_line4, ru_note


def _footer_rows(
    sep_width: int,
    footer_line1: str,
    footer_line2: str,
    footer_line4: str,
    ru_note: str,
) -> list[tuple[str, int]]:
    """Lay out the footer before reserving space for the scrollable coin rows.

    ``sep_width`` is the table's separator span
    (:func:`jmwallet.utxo_tui._table_sep_width`): the footer box spans the
    table's full width -- framed separators and framed text rows, borders
    at columns 0 and ``sep_width``.
    """
    content_width = max(1, sep_width - 4)
    separator = ("|" + "-" * (sep_width - 1) + "|", curses.A_NORMAL)
    title_attr = curses.color_pair(3) | curses.A_BOLD
    # The first separator closes the table's bottom edge (the frame filler
    # keeps the border columns connected above it).
    rows = [separator, ("Selection Details", title_attr)]
    rows.extend((line, curses.A_BOLD) for line in _wrap_footer_fields(footer_line1, content_width))
    rows.append((footer_line2, curses.A_BOLD))
    rows.extend([separator, ("UTXO Status", title_attr)])
    # The status block shows exactly the cursor coin's payload -- no
    # max-reserve padding over all coins: the padding rendered as a visible
    # empty line whenever the cursor coin wrapped shorter than the wallet
    # maximum (e.g. the one-column window where a 2-digit-vout outpoint
    # tips the wrap threshold), and it flipped around on resize
    # (user decision 2026-11-15). The footer height may breathe by one line
    # when coins of different status heights alternate under the cursor;
    # that cost is accepted (RU!/notice rows stay conditional, see below).
    status_lines = wrap_status_text(footer_line4, content_width)
    rows.extend((line, curses.A_BOLD) for line in status_lines)
    # The note row exists only when there is something to show (RU! or a
    # transient notice); an always-reserved empty row wasted a list line in
    # the overwhelmingly common no-reuse case (user decision 2026-11-15).
    if ru_note:
        rows.append((ru_note, curses.color_pair(4) | curses.A_BOLD))
    rows.extend([separator, ("Controls", title_attr)])
    rows.extend(
        (line, curses.A_NORMAL) for line in align_control_columns(_CONTROLS_HINT, content_width)
    )
    # The closing separator is the box's bottom edge; the render loop keeps
    # the terminal's last row empty below it (a one-cell black frame).
    rows.append(separator)
    return rows


def _run_selector(
    stdscr: curses.window,
    display_items: list[UTXOInfo | None],
    target_amount: int,
    allowed_mixdepth: int | None,
    min_confirmations: int,
    excluded_outpoints: set[tuple[str, int]],
    allow_fidelity_bonds: bool = True,
    wallet_name: str = "",
    wallet_fingerprint: str = "",
) -> list[UTXOInfo]:
    """Run the curses-based UTXO selector.

    Args:
        stdscr: The curses window
        display_items: UTXOs (with ``None`` mixdepth separators) to display
        target_amount: Target amount in sats (0 for sweep, shown for info)
        allowed_mixdepth: When set, only this mixdepth is selectable
        min_confirmations: UTXOs below this many confirmations are unselectable
        excluded_outpoints: In-flight CoinJoin inputs shown as State ``in-use``
            with a ``[-]`` mark; unavailable for selection
        allow_fidelity_bonds: When ``False`` (CoinJoin context) expired bonds
            are shown as State ``no-cj`` and cannot be selected
        wallet_name: Wallet file name shown in the status block header
        wallet_fingerprint: Wallet fingerprint shown in the status block header

    Returns:
        List of selected UTXOs
    """
    # Initialize curses
    curses.curs_set(0)  # Hide cursor
    curses.use_default_colors()

    # Initialize color pairs
    curses.init_pair(1, curses.COLOR_GREEN, -1)  # Selected items
    curses.init_pair(2, curses.COLOR_YELLOW, -1)  # Current cursor
    curses.init_pair(3, curses.COLOR_CYAN, -1)  # Header
    curses.init_pair(4, curses.COLOR_RED, -1)  # Locked FBs / frozen or in-use UTXOs / RU! warnings
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # Unlocked fidelity bonds (can be spent)

    selected: set[int] = set()
    scroll_offset = 0
    # Transient footer notice, shown for one render cycle after a blocked action.
    notice = ""

    # Pre-compute which rows can ever be selected in this session.
    base_selectable: set[int] = {
        i
        for i, item in enumerate(display_items)
        if item is not None
        and _is_base_selectable(
            item,
            allowed_mixdepth,
            min_confirmations,
            excluded_outpoints,
            allow_fidelity_bonds=allow_fidelity_bonds,
        )
    }
    last = len(display_items) - 1

    def pinned_mixdepth() -> int | None:
        """The mixdepth the selection is currently pinned to (if any)."""
        if selected:
            item = display_items[next(iter(selected))]
            assert item is not None
            return item.mixdepth
        return allowed_mixdepth

    def is_selectable(index: int) -> bool:
        if index not in base_selectable:
            return False
        pinned = pinned_mixdepth()
        item = display_items[index]
        assert item is not None
        return pinned is None or item.mixdepth == pinned

    # Start on a UTXO row, never on a separator.
    cursor_pos = seek_selectable(display_items, 0, 1, lambda _u: True)

    # Address reuse detection: count UTXOs per address
    address_utxo_counts: dict[str, int] = {}
    for item in display_items:
        if item is not None:
            address_utxo_counts[item.address] = address_utxo_counts.get(item.address, 0) + 1
    utxos = [item for item in display_items if item is not None]

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()

        pinned = pinned_mixdepth()
        columns = _table_columns(width, utxos)
        # Status block above the column titles (whiptail menu_header style).
        # The maker status is process-derived (start-path independent).
        sep_width = _table_sep_width(columns)
        status_lines = status_header_lines(
            sep_width,
            wallet_name or "(no file)",
            wallet_fingerprint,
            maker_bot_status(),
        )
        status_count = len(status_lines)
        footer = _footer_rows(
            _table_sep_width(columns),
            *_build_footer_lines(
                display_items,
                selected,
                base_selectable,
                is_selectable,
                target_amount,
                allowed_mixdepth,
                pinned,
                cursor_pos,
                excluded_outpoints,
                address_utxo_counts,
                min_confirmations,
                allow_fidelity_bonds,
                notice=notice,
            ),
        )
        list_start = 6 + status_count
        # Full layout widens the address column to fill the terminal exactly
        # (right border in the last usable column); the compact layout stays
        # at its own table width, so every border reads from the columns.
        # One blank row stays below the footer box (black frame at the
        # screen's bottom edge); it shrinks the scrollable region.
        list_height = height - list_start - len(footer) - 1
        if width < max(80, _minimum_table_width(columns)):
            # The table itself cannot fit: the fix is horizontal space.
            draw_resize_prompt(stdscr, width, "width")
            if stdscr.getch() in (ord("q"), 27):
                return []
            continue
        if list_height < 1:
            # Width is fine but the vertical budget (title rules, status
            # block, footer, blank frame row) leaves no list row. First try
            # the one-line status block before blaming the terminal height:
            # a screen that rendered before the status block must keep
            # rendering with it.
            if status_count > 1:
                retry = status_header_lines(
                    sep_width,
                    wallet_name or "(no file)",
                    wallet_fingerprint,
                    maker_bot_status(),
                    max_lines=1,
                )
                retry_height = height - (6 + len(retry)) - len(footer) - 1
                if retry_height >= 1:
                    status_lines = retry
                    status_count = len(retry)
                    list_start = 6 + status_count
                    list_height = retry_height
                else:
                    draw_resize_prompt(stdscr, width, "height")
                    if stdscr.getch() in (ord("q"), 27):
                        return []
                    continue
            else:
                draw_resize_prompt(stdscr, width, "height")
                if stdscr.getch() in (ord("q"), 27):
                    return []
                continue

        scroll_offset = adjust_scroll(cursor_pos, scroll_offset, list_height, len(display_items))
        try:
            _draw_header(stdscr, width, columns, status_lines)
            # The frame filler keeps the border columns connected across the
            # spare list rows (rows drawn later win over the filler borders);
            # the footer's own first separator row doubles as its top edge.
            # One blank row stays below the footer box (black frame at the
            # screen's bottom edge); it also keeps the terminal's unwritable
            # bottom-right corner out of the box entirely.
            footer_start = height - len(footer) - 1
            _draw_frame_filler(stdscr, list_start, footer_start, sep_width)
            _draw_utxo_rows(
                stdscr,
                display_items,
                selected,
                cursor_pos,
                scroll_offset,
                list_height,
                list_start,
                width,
                sep_width,
                excluded_outpoints,
                address_utxo_counts,
                is_selectable,
                columns,
                pinned,
                min_confirmations,
                allow_fidelity_bonds,
            )
            _draw_footer(stdscr, footer_start, footer, sep_width)
        except curses.error:
            # Do not dispatch spend actions after an interrupted repaint.
            # The cause of the interruption is not diagnosable here, so the
            # generic prompt text stays.
            draw_resize_prompt(stdscr, width)
            if stdscr.getch() in (ord("q"), 27):
                return []
            continue
        stdscr.refresh()

        # Handle input
        key = stdscr.getch()
        notice = ""

        if key == ord("?"):
            _, width = stdscr.getmaxyx()
            overlay = _help_overlay_rows(sep_width, SELECTOR_STATE_TOKENS)
            _draw_help_overlay(stdscr, overlay, sep_width, _HELP_TITLE)
            wait_for_help_close(stdscr)
            continue

        if key == ord("q") or key == 27:  # q or Escape
            return []

        if key == ord("\n") or key == curses.KEY_ENTER:  # Enter
            if selected:
                result = [display_items[i] for i in sorted(selected)]
                return [u for u in result if u is not None]
            # If nothing selected but there's only one selectable UTXO, select it
            if len(base_selectable) == 1:
                only = display_items[next(iter(base_selectable))]
                assert only is not None
                return [only]
            # Otherwise require explicit selection
            continue

        if key == ord("\t") or key == ord(" "):  # Tab or Space to toggle
            if cursor_pos in selected:
                selected.discard(cursor_pos)
            elif is_selectable(cursor_pos):
                selected.add(cursor_pos)
            elif cursor_pos in base_selectable and pinned is not None:
                # Blocked only by the mixdepth pin: a spend can never mix
                # coins from different mixdepths. Other restrictions are
                # explained by the State column.
                notice = f"Locked to source mixdepth m{pinned} — press d to deselect all"
            # Move cursor down after toggle attempt
            if cursor_pos < last:
                cursor_pos = seek_selectable(display_items, cursor_pos + 1, 1, lambda _u: True)

        elif key == curses.KEY_UP or key == ord("k"):
            cursor_pos = seek_selectable(display_items, max(0, cursor_pos - 1), -1, lambda _u: True)

        elif key == curses.KEY_DOWN or key == ord("j"):
            cursor_pos = seek_selectable(
                display_items, min(last, cursor_pos + 1), 1, lambda _u: True
            )

        elif key == curses.KEY_PPAGE:  # Page Up
            cursor_pos = seek_selectable(
                display_items, max(0, cursor_pos - list_height), -1, lambda _u: True
            )

        elif key == curses.KEY_NPAGE:  # Page Down
            cursor_pos = seek_selectable(
                display_items, min(last, cursor_pos + list_height), 1, lambda _u: True
            )

        elif key == ord("g"):  # Go to top
            cursor_pos = seek_selectable(display_items, 0, 1, lambda _u: True)

        elif key == ord("G"):  # Go to bottom
            cursor_pos = seek_selectable(display_items, last, -1, lambda _u: True)

        elif key == ord("s"):  # Select all selectable UTXOs in one mixdepth
            # Use the pinned mixdepth when set, else the mixdepth under the
            # cursor; a spend can never mix coins from different mixdepths.
            target_md = pinned
            if target_md is None:
                cursor_item = display_items[cursor_pos]
                if cursor_item is not None:
                    target_md = cursor_item.mixdepth
            if target_md is not None:
                for i in base_selectable:
                    item = display_items[i]
                    if item is not None and item.mixdepth == target_md:
                        selected.add(i)

        elif key == ord("d"):  # Deselect all
            selected = set()


def _prepare_display_items(utxos: list[UTXOInfo]) -> list[UTXOInfo | None]:
    """Sort UTXOs in extended-view group order and add labelled separators.

    Groups are (mixdepth, branch) pairs; the branch order within a mixdepth
    follows the extended view: fidelity bonds first, then external, then
    internal addresses.
    """
    return build_display_items(sorted(utxos, key=utxo_display_sort_key), branch_groups=True)


def select_utxos_interactive(
    utxos: list[UTXOInfo],
    target_amount: int = 0,
    allowed_mixdepth: int | None = None,
    min_confirmations: int = 0,
    excluded_outpoints: set[tuple[str, int]] | None = None,
    *,
    allow_fidelity_bonds: bool = True,
    wallet_name: str = "",
    wallet_fingerprint: str = "",
) -> list[UTXOInfo]:
    """Display an interactive UTXO selector over the whole wallet.

    UTXOs are grouped by mixdepth and branch in the extended view's order
    (fidelity bonds, external, internal). Selection is
    limited to a single mixdepth: the first toggled UTXO pins the source
    mixdepth until everything is deselected again.

    Keys:
    - Up/Down or j/k: Navigate
    - PgUp/PgDn: Page up/down
    - Tab/Space: Toggle selection
    - Enter: Confirm selection
    - q/Escape: Cancel
    - s: Select all (in the pinned/cursor mixdepth)
    - d: Deselect all
    - g/G: Go to top/bottom

    Args:
        utxos: List of available UTXOs to choose from (any mixdepth)
        target_amount: Target amount in sats (0 for sweep, used for display)
        allowed_mixdepth: When set, restrict selection to this mixdepth
            (other mixdepths are displayed for context only)
        min_confirmations: UTXOs below this many confirmations are shown
            but unselectable
        excluded_outpoints: In-flight CoinJoin inputs shown as State ``in-use``
            with a ``[-]`` mark; unavailable for selection.
        allow_fidelity_bonds: Direct sends (the default) may spend expired
            fidelity bonds. CoinJoin callers pass ``False`` so every bond is
            shown as State ``no-cj`` with a ``[-]`` mark: bond inputs would
            be disclosed to makers and then rejected at signing.
        wallet_name: Wallet file name shown in the status block header
            (keyword-only); empty falls back to ``(no file)``
        wallet_fingerprint: Wallet fingerprint shown in the status block
            header (keyword-only)

    Returns:
        List of selected UTXOs (all from one mixdepth), empty if cancelled

    Raises:
        RuntimeError: If not running in a terminal
    """
    # Handle trivial cases without requiring a terminal
    if not utxos:
        return []
    excluded_outpoints = excluded_outpoints or set()

    # For multiple UTXOs, we need a terminal
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        # If only one UTXO and no terminal, auto-select it (only if selectable)
        if len(utxos) == 1:
            utxo = utxos[0]
            if not _is_base_selectable(
                utxo,
                allowed_mixdepth,
                min_confirmations,
                excluded_outpoints,
                allow_fidelity_bonds=allow_fidelity_bonds,
            ):
                return []
            return utxos
        raise RuntimeError("Interactive UTXO selection requires a terminal")

    # Extended-view group order (FB, external, internal) + labelled separators
    display_items = _prepare_display_items(utxos)

    return curses.wrapper(
        _run_selector,
        display_items,
        target_amount,
        allowed_mixdepth,
        min_confirmations,
        excluded_outpoints,
        allow_fidelity_bonds,
        wallet_name,
        wallet_fingerprint,
    )
