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
- the address column formatting (full address on every row, middle-ellipsis
  when too long),
- scroll-offset adjustment to keep the cursor visible,
- the shared minimum-size prompt used while either terminal has no room
  for the full layout,
- the table layout shared by both TUIs: dynamic columns sized to terminal
  width and content, cell clipping/formatting, and single-row rendering,
- footer field wrapping/rendering, address-reuse detection (the RU! badge),
  and the cursor-item description used by both status lines,
- the wallet-label enrichment both feeders run so the ``tag`` column is
  never empty for regular UTXOs.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import curses
    from collections.abc import Callable

    from jmwallet.wallet.models import UTXOInfo

from jmwallet.wallet.utxo_metadata import AUTO_FREEZE_REUSE_LABEL

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


def format_address_column(address: str) -> str:
    """Format an address for the TUI address column.

    Every row shows its own address so duplicate addresses stay visible.
    Long addresses (e.g. fidelity bond P2WSH) are truncated in the middle.
    """
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
    import curses

    try:
        stdscr.addstr(0, 0, "Terminal too small. Please resize. q/Esc: cancel"[: max(1, width - 1)])
    except curses.error:
        # Curses can report ERR after successfully writing the bottom-right cell (1x1).
        pass
    stdscr.refresh()


# -- Table layout and row formatting ----------------------------------------
#
# Shared by both TUIs (the interactive UTXO selector and the freeze manager):
# column sets that expand the address column to the available terminal width
# (falling back to a compact set below the minimum width), cell
# clipping/formatting, and single-row rendering.


def _clip(text: str, width: int) -> str:
    """Clip a display field explicitly, rather than silently losing its suffix."""
    return text if len(text) <= width else text[: max(0, width - 3)] + "." * min(3, width)


def enrich_utxo_labels(label_lookup: Callable[[str], str], utxos: list[UTXOInfo]) -> None:
    """Populate missing wallet labels for display in both UTXO TUIs.

    ``utxo_cache`` items carry no labels, so without this the ``tag`` column
    stays empty for every regular UTXO. Existing labels (BIP-329
    annotations) and fidelity bond rows are preserved -- a bond's tag column
    already describes the bond state (``FB-active``/``FB-expired``).
    ``label_lookup`` maps an address to its wallet label (``""`` when none).
    """
    for utxo in utxos:
        if utxo.label is None and not utxo.is_fidelity_bond:
            utxo.label = label_lookup(utxo.address)


def _label_col_value(utxo: UTXOInfo) -> str:
    """Value of the tag column (FB status marker or label).

    Shared between the row formatter and the column sizing so the width
    always matches the actual content. Maps ``non-cj-change`` to the
    shorter ``reg-change`` like the info table does.
    """
    if utxo.is_fidelity_bond:
        return "FB-active" if utxo.is_locked else "FB-expired"
    return "reg-change" if utxo.label == "non-cj-change" else (utxo.label or "")


def _table_columns(
    width: int, utxos: list[UTXOInfo], mark_title: str = "Sel"
) -> list[tuple[str, int]]:
    """Adaptive column widths following the ``info`` extended view.

    Every non-address column is exactly as wide as its (lowercase) title or
    the longest visible value, whichever is bigger; titles carry the units
    (``sats``, ``confs``) so the cells stay unit-free. Only the address
    column grabs the remaining terminal width (full layout); below the
    minimum width the compact layout drops address and confs. The outpoint
    no longer occupies a column -- the full value is shown in the footer's
    UTXO Status line. The tag column is capped at 11 characters so long
    BIP-329 labels cannot push the table past the terminal; the full label
    always appears in the UTXO Status footer line.

    ``mark_title`` names the leading checkbox-style column (``Sel`` in the
    selector, ``F`` in the freeze manager); it is drawn by the row renderer,
    not by :func:`format_utxo_line`.
    """
    tag_width = max(3, min(11, max((len(_label_col_value(u)) for u in utxos), default=0)))
    sats_width = max(4, max((len(f"{u.value:,}") for u in utxos), default=0))
    columns = [
        (mark_title, 3),
        ("MD", max(2, max((len(f"m{u.mixdepth}") for u in utxos), default=0))),
        ("sats", sats_width),
        ("tag", tag_width),
        ("state", 9),
    ]
    full_columns = columns[:2] + [
        ("address", 12),
        columns[2],
        ("confs", max(5, max((len(f"{u.confirmations:,}") for u in utxos), default=0))),
        columns[3],
        columns[4],
    ]
    extra = width - _minimum_table_width(full_columns)
    if extra >= 0:
        full_columns[2] = ("address", 12 + extra)
        return full_columns
    return columns


def _minimum_table_width(columns: list[tuple[str, int]]) -> int:
    # Indent, outer borders, RU! suffix and an unused rightmost terminal cell.
    return sum(size for _, size in columns) + 3 * (len(columns) - 1) + 11


def _format_cells(values: dict[str, str], columns: list[tuple[str, int]]) -> str:
    return " | ".join(
        f"{_clip(values[name], size):>{size}}"
        if name in {"MD", "sats", "confs"}
        else f"{_clip(values[name], size):<{size}}"
        for name, size in columns
    )


def format_utxo_line(
    utxo: UTXOInfo,
    max_width: int = 120,
    excluded_outpoints: set[tuple[str, int]] | None = None,
    term_width: int = 120,
    *,
    allowed_mixdepth: int | None = None,
    min_confirmations: int = 0,
    columns: list[tuple[str, int]] | None = None,
    allow_fidelity_bonds: bool = True,
    mark_title: str = "Sel",
    state_override: str | None = None,
) -> str:
    """Format a single UTXO row with separate Label and State columns.

    ``mark_title`` names the checkbox-style column owned by the row
    renderer; it is excluded from the formatted line. ``state_override``
    replaces the selector's State computation (the freeze manager renders
    its own states: frozen/locked/spendable).
    """
    columns = columns or _table_columns(term_width, [utxo], mark_title)
    addr_col_width = dict(columns).get("address", 12)

    # Every row shows its own address (duplicate addresses stay visible);
    # long addresses are middle-truncated to the column width.
    if len(utxo.address) > addr_col_width:
        # Middle-ellipsis: show start...end
        prefix_len = (addr_col_width - 3) // 2
        suffix_len = addr_col_width - 3 - prefix_len
        addr_str = utxo.address[:prefix_len] + "..." + utxo.address[-suffix_len:]
    else:
        addr_str = utxo.address

    values = {
        "MD": f"m{utxo.mixdepth}",
        "address": addr_str,
        "sats": f"{utxo.value:,}",
        "confs": f"{utxo.confirmations:,}",
        "tag": _label_col_value(utxo),
        "state": (
            state_override
            if state_override is not None
            else _utxo_state_col(
                utxo,
                excluded_outpoints,
                allowed_mixdepth,
                min_confirmations,
                allow_fidelity_bonds=allow_fidelity_bonds,
            )
        ),
    }
    return _clip(
        _format_cells(values, [(name, size) for name, size in columns if name != mark_title])
        + " |",
        max_width,
    )


def _utxo_state_col(
    utxo: UTXOInfo,
    excluded_outpoints: set[tuple[str, int]] | None = None,
    allowed_mixdepth: int | None = None,
    min_confirmations: int = 0,
    *,
    allow_fidelity_bonds: bool = True,
) -> str:
    """Return the State-column value for a UTXO (also used in the footer).

    Priority order: in-use (CoinJoin active) overrides everything, then frozen
    (user locked), then locked (FB timelock active), then ``no-cj`` for any
    remaining fidelity bond when the caller disallows bonds (CoinJoin
    context), then session restrictions.
    """
    if excluded_outpoints and (utxo.txid, utxo.vout) in excluded_outpoints:
        return "in-use"
    if utxo.frozen:
        return "frozen"
    if utxo.is_fidelity_bond and utxo.is_locked:
        return "locked"
    if utxo.is_fidelity_bond and not allow_fidelity_bonds:
        return "no-cj"
    if utxo.confirmations < min_confirmations:
        return "immature"
    if allowed_mixdepth is not None and utxo.mixdepth != allowed_mixdepth:
        return "other-md"
    return "spendable"


def _is_address_reused(
    utxo: UTXOInfo,
    address_utxo_counts: dict[str, int],
) -> bool:
    """Whether a UTXO shares its address with other UTXOs (reduces privacy).

    Reuse is detected the same way for the table's RU! indicator and the
    footer's RU explanation.
    """
    return address_utxo_counts.get(utxo.address, 0) > 1 or utxo.label == AUTO_FREEZE_REUSE_LABEL


def utxo_cursor_description(utxo: UTXOInfo) -> str:
    """One-sentence explanation of a coin for the UTXO Status footer line.

    Coin meanings are identical in the selector and the freeze manager.
    """
    if utxo.is_fidelity_bond:
        # is_locked implies a locktime; the None check lets mypy see it.
        if utxo.is_locked and utxo.locktime is not None:
            return (
                f"Active Fidelity Bond: Locked and not spendable until "
                f"{datetime.fromtimestamp(utxo.locktime, UTC).strftime('%Y-%m-%d')} UTC!"
            )
        return "Expired Fidelity Bond"
    if utxo.label == "cj-change":
        return "Change output from a CoinJoin (deanonymising, keep separate)"
    if utxo.label == "cj-out":
        return "CoinJoin output (mixed funds)"
    if utxo.label in ("reg-change", "non-cj-change"):
        return "Regular change (not from CoinJoin)"
    if utxo.label == "deposit":
        return "Address with funds from internal or external sources"
    if utxo.label:
        return f"Label: {utxo.label}"
    return ""


def _wrap_footer_fields(text: str, width: int) -> list[str]:
    """Wrap between fields so amounts and their units always stay together."""
    lines: list[str] = []
    current = ""
    for field in text.split(" | "):
        candidate = f"{current} | {field}" if current else field
        if len(candidate) > width and current:
            lines.append(current)
            current = field
        else:
            current = candidate
    lines.append(current)
    return lines


def _draw_footer(stdscr: curses.window, start: int, rows: list[tuple[str, int]]) -> None:
    """Draw precomputed footer rows without truncating selection amounts."""
    for offset, (line, attr) in enumerate(rows):
        stdscr.addstr(start + offset, 2, line, attr)
