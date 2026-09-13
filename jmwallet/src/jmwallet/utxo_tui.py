"""
Shared helpers for the curses-based UTXO TUIs.

Both the freeze manager (``jm-wallet freeze``) and the interactive UTXO
selector (``--select-utxos``) render the whole wallet grouped by mixdepth.
This module holds the layout and navigation primitives they share:

- building a display list with ``None`` separators between mixdepth groups,
- cursor navigation that skips separators (and other non-selectable rows),
- the address column formatting (collapsing consecutive duplicates),
- scroll-offset adjustment to keep the cursor visible.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from jmwallet.wallet.models import UTXOInfo

# Width of the address column used by both TUIs so rows stay aligned.
ADDRESS_COL_WIDTH = 42


def utxo_sort_key(utxo: UTXOInfo) -> tuple[int, ...]:
    """Numeric derivation-path sort key shared by the freeze manager and the
    interactive UTXO selector.

    Paths look like ``m/84'/0'/0'/0/3`` (receive), ``.../1/3`` (change) or
    ``.../2/{timenumber}`` (fidelity bond). Comparing these as plain strings
    misorders indices >= 10 (``.../0/10`` sorts before ``.../0/2``), so the
    integer path components are compared instead. A fidelity-bond locktime
    appended as ``:locktime`` adds a trailing tie-breaker and sorts
    consistently with the bare ``.../2/{timenumber}`` form.
    """
    return tuple(int(part) for part in re.findall(r"\d+", utxo.path))


def build_display_items(utxos: list[UTXOInfo]) -> list[UTXOInfo | None]:
    """Insert ``None`` separators between mixdepth groups.

    ``utxos`` must already be sorted so that all UTXOs of a mixdepth are
    contiguous. A ``None`` entry is inserted between consecutive mixdepth
    groups; it renders as a separator line and is skipped during navigation.
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


def seek_selectable(
    display_items: list[UTXOInfo | None],
    start: int,
    direction: int,
    is_selectable: Callable[[UTXOInfo], bool],
) -> int:
    """Return the nearest index from ``start`` whose item satisfies ``is_selectable``.

    ``None`` separators are always skipped. Searches in ``direction`` first
    and falls back to the opposite direction; returns ``start`` when nothing
    matches at all.
    """
    pos = start

    # Search in the requested direction
    while 0 <= pos < len(display_items):
        item = display_items[pos]
        if item is not None and is_selectable(item):
            return pos
        pos += direction

    # If not found, search in the opposite direction from start
    opposite = -direction
    pos = start + opposite
    while 0 <= pos < len(display_items):
        item = display_items[pos]
        if item is not None and is_selectable(item):
            return pos
        pos += opposite

    return start


def format_address_column(address: str, prev_address: str) -> str:
    """Format an address for the TUI address column.

    Consecutive UTXOs sharing the same address render the address only once;
    subsequent rows show blanks so the column stays visually grouped. Long
    addresses (e.g. fidelity bond P2WSH) are truncated in the middle.
    """
    if address == prev_address:
        return " " * ADDRESS_COL_WIDTH
    if len(address) > ADDRESS_COL_WIDTH:
        return address[:20] + "..." + address[-19:]
    return address


def adjust_scroll(cursor_pos: int, scroll_offset: int, list_height: int) -> int:
    """Return a scroll offset that keeps ``cursor_pos`` visible."""
    if cursor_pos < scroll_offset:
        return cursor_pos
    if cursor_pos >= scroll_offset + list_height:
        return cursor_pos - list_height + 1
    return scroll_offset
