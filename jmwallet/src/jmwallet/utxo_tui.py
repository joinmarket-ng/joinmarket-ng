"""
Shared helpers for the curses-based UTXO TUIs.

Both the freeze manager (``jm-wallet freeze``) and the interactive UTXO
selector (``--select-utxos``) render the whole wallet grouped by mixdepth.
This module holds the layout and navigation primitives they share:

- building a display list with ``None`` separators between groups (mixdepths,
  or mixdepth/branch groups with fidelity bonds shown first),
- labelling those separators from the group they introduce (mixdepth title,
  branch name),
- cursor navigation that skips separators (and other non-selectable rows),
- the address column formatting (collapsing consecutive duplicates),
- scroll-offset adjustment to keep the cursor visible,
- the shared minimum-size prompt used while either terminal has no room
  for the full layout.
"""

from __future__ import annotations

import curses
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


# Branch numbers in BIP-84 derivation paths (m/84'/coin'/account'/branch/index).
# These are fixed protocol constants; ``FIDELITY_BOND_BRANCH`` mirrors
# jmwallet.wallet.constants.
EXTERNAL_BRANCH = 0
INTERNAL_BRANCH = 1
FIDELITY_BOND_BRANCH = 2

# Display order of the branch groups, matching the extended view's section
# order: fidelity bonds first, then external, then internal addresses.
_BRANCH_DISPLAY_RANK = {FIDELITY_BOND_BRANCH: 0, EXTERNAL_BRANCH: 1, INTERNAL_BRANCH: 2}

_BRANCH_NAMES = {
    EXTERNAL_BRANCH: "external addresses",
    INTERNAL_BRANCH: "internal addresses",
    FIDELITY_BOND_BRANCH: "fidelity bond addresses",
}


def utxo_branch(utxo: UTXOInfo) -> int:
    """Return the branch component of the UTXO's derivation path.

    Branch is the fourth numeric component in paths like ``m/84'/0'/0'/0/3``
    (external), ``.../1/3`` (internal) or ``.../2/{timenumber}`` (fidelity
    bond); a trailing ``:locktime`` suffix does not affect it. Returns -1 for
    paths without a branch component so malformed paths group together instead
    of crashing the spend flow.
    """
    parts = utxo_sort_key(utxo)
    return parts[3] if len(parts) > 3 else -1


def branch_display_rank(branch: int) -> int:
    """Display rank of a branch group; unknown branches sort last."""
    return _BRANCH_DISPLAY_RANK.get(branch, len(_BRANCH_DISPLAY_RANK))


def utxo_display_sort_key(utxo: UTXOInfo) -> tuple[int, int, tuple[int, ...]]:
    """Sort key for the interactive views: mixdepth, then branch in display
    order (fidelity bonds, external, internal), then derivation index.

    Within a branch group the numeric path order of :func:`utxo_sort_key` is
    preserved, so indices >= 10 still sort correctly.
    """
    return (utxo.mixdepth, branch_display_rank(utxo_branch(utxo)), utxo_sort_key(utxo))


def group_label(utxo: UTXOInfo) -> str:
    """Label of the (mixdepth, branch) group ``utxo`` belongs to.

    Example: ``Mixdepth 0 — fidelity bond addresses``.
    """
    branch = utxo_branch(utxo)
    name = _BRANCH_NAMES.get(branch, f"branch {branch}")
    return f"Mixdepth {utxo.mixdepth} — {name}"


def _neighbor_items(
    display_items: list[UTXOInfo | None], pos: int
) -> tuple[UTXOInfo | None, UTXOInfo | None]:
    """Nearest non-separator items before and after the separator at ``pos``.

    Separators are constructed to precede their group, so the following item
    identifies the group the separator introduces; the preceding item tells
    whether that group continues the same mixdepth (branch-only separator) or
    opens a new one (full title). A trailing separator (not constructed
    today) has no following item and renders as plain dashes.
    """
    prev_item = next((item for item in reversed(display_items[:pos]) if item is not None), None)
    next_item = next((item for item in display_items[pos + 1 :] if item is not None), None)
    return prev_item, next_item


def group_separator_parts(
    display_items: list[UTXOInfo | None], pos: int
) -> tuple[str, str, str, bool] | None:
    """Label parts for the group separator at ``pos``.

    Returns ``(title, em_dash, branch, full_title)`` — for example
    ``("Mixdepth 0", " — ", "external addresses", True)`` — or ``None`` for a
    trailing separator with no following group (renders as plain dashes).
    ``full_title`` is False when the separator only switches the branch within
    the same mixdepth; renderers then strike the title with dashes so a branch
    change leaves only the branch name column-flush with the full title above.
    """
    prev_item, next_item = _neighbor_items(display_items, pos)
    if next_item is None:
        return None
    title, em_dash, branch = group_label(next_item).partition(" — ")
    full_title = prev_item is None or prev_item.mixdepth != next_item.mixdepth
    return title, em_dash, branch, full_title


def build_display_items(
    utxos: list[UTXOInfo], *, branch_groups: bool = False
) -> list[UTXOInfo | None]:
    """Insert ``None`` separators between display groups.

    ``utxos`` must already be sorted so that all UTXOs of a group are
    contiguous. By default a group spans one mixdepth: a separator is inserted
    between consecutive mixdepths. With ``branch_groups=True`` the groups are
    (mixdepth, branch) pairs — external, internal and fidelity bonds get their
    own separators — and the list starts with a leading separator, so every
    separator precedes a group and can be labelled from the group's first row.
    Additionally each new mixdepth is introduced by a blank border-only row,
    encoded as two consecutive ``None`` entries (blank row, then the labelled
    separator). A ``None`` entry renders as a separator line (or, when the
    next entry is also ``None``, as the blank row) and is skipped during
    navigation.
    """
    display_items: list[UTXOInfo | None] = []
    current_group: tuple[int, ...] | None = None
    for utxo in utxos:
        group: tuple[int, ...] = (
            (utxo.mixdepth, utxo_branch(utxo)) if branch_groups else (utxo.mixdepth,)
        )
        if group != current_group:
            if branch_groups:
                if current_group is None or utxo.mixdepth != current_group[0]:
                    display_items.append(None)  # blank border-only row
                display_items.append(None)  # labelled separator
            elif display_items:
                display_items.append(None)
            current_group = group
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


def adjust_scroll(cursor_pos: int, scroll_offset: int, list_height: int, item_count: int) -> int:
    """Return a scroll offset that keeps ``cursor_pos`` visible.

    The result is clamped to ``max(0, item_count - list_height)`` so an
    offset reached while the window was shrunken is given up as soon as
    more rows fit on screen again (never scroll further than necessary).
    """
    if cursor_pos < scroll_offset:
        scroll_offset = cursor_pos
    elif cursor_pos >= scroll_offset + list_height:
        scroll_offset = cursor_pos - list_height + 1
    return min(scroll_offset, max(0, item_count - list_height))


def draw_resize_prompt(stdscr: curses.window, width: int) -> None:
    """Draw the minimum-size prompt shared by both TUIs."""
    try:
        stdscr.addstr(0, 0, "Terminal too small. Please resize. q/Esc: cancel"[: max(1, width - 1)])
    except curses.error:
        # Curses can report ERR after successfully writing the bottom-right cell (1x1).
        pass
    stdscr.refresh()
