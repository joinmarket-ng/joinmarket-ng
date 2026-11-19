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
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import curses
    from collections.abc import Callable

    from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.rows import (
    _STATE_TOKENS,
    STATE_HELP,
    TAG_HELP,
    StateToken,
    short_label,
    state_token,
)
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


def draw_resize_prompt(stdscr: curses.window, width: int, reason: str = "") -> None:
    """Draw the minimum-size prompt shared by both TUIs.

    ``reason`` names the dimension the terminal falls short of
    (``"width"`` / ``"height"``) so the user resizes the right one; the
    generic message stays the fallback for callers without a diagnosis
    (the interrupted-repaint path).
    """
    import curses

    if reason == "width":
        text = "Terminal too narrow. Please resize. q/Esc: cancel"
    elif reason == "height":
        text = "Terminal too short. Please resize. q/Esc: cancel"
    else:
        text = "Terminal too small. Please resize. q/Esc: cancel"
    try:
        stdscr.addstr(0, 0, text[: max(1, width - 1)])
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


def _display_width(text: str) -> int:
    """Terminal columns of ``text`` (never ``len()`` for width decisions).

    CJK-wide characters (unicodedata East Asian W/F) render 2 columns,
    combining marks (Mn) render 0; the emoji/VS16 special case is ignored
    (no emoji in TUI chrome). Mirrors the legacy TUI's column discipline.
    """
    import unicodedata

    width = 0
    for char in text:
        if unicodedata.category(char) == "Mn":
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def maker_bot_status() -> str:
    """``RUNNING``/``STOPPED`` for the maker bot, independent of how it started.

    Scans ``/proc`` argv for ``jm-maker`` (the maker console script and the
    systemd unit both run this entry point), so the TUI sees the maker
    whether it was started manually, via the legacy TUI menu, or under the
    Raspiblitz systemd service. ``pgrep -f`` parity without a subprocess:
    the exact argv match cannot hit the scanner's own process (whose argv
    is a Python TUI command line) or unrelated grep invocations.
    """
    from pathlib import Path

    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            argv = (proc_dir / "cmdline").read_bytes().split(b"\x00")
        except (OSError, PermissionError):
            continue
        for arg in argv:
            if arg == b"":
                continue
            try:
                arg_text = arg.decode("utf-8", errors="ignore")
            except UnicodeDecodeError:
                continue
            # pgrep -f parity: match the entry point whether it was invoked
            # via PATH (``jm-maker serve``) or by absolute path
            # (``/home/.../jm-maker serve``). The basename comparison never
            # matches unrelated commands (``not-jm-maker-x`` stays clean).
            if arg_text == "jm-maker" or arg_text.rsplit("/", 1)[-1] == "jm-maker":
                return "RUNNING"
    return "STOPPED"


def enrich_utxo_labels(label_lookup: Callable[[str], str], utxos: list[UTXOInfo]) -> None:
    """Populate missing wallet labels for display in both UTXO TUIs.

    ``utxo_cache`` items carry no labels, so without this the ``tag`` column
    stays empty for every regular UTXO. Existing labels (BIP-329
    annotations) and fidelity bond rows are preserved -- a bond's tag column
    already describes the bond state (``fb-a``/``fb-x`` via
    :func:`jmwallet.wallet.rows.short_label`).
    ``label_lookup`` maps an address to its wallet label (``""`` when none).
    """
    for utxo in utxos:
        if utxo.label is None and not utxo.is_fidelity_bond:
            utxo.label = label_lookup(utxo.address)


def _label_col_value(utxo: UTXOInfo) -> str:
    """Value of the tag column (FB status marker or label).

    Shared between the row formatter and the column sizing so the width
    always matches the actual content. Delegates to the canonical row
    vocabulary (:func:`jmwallet.wallet.rows.short_label`): fidelity bonds
    map to ``fb-a``/``fb-x``, wallet statuses to their short forms
    (``non-cj-change`` -> ``chg``); BIP-329 user labels pass through.
    """
    return short_label(utxo)


def _table_columns(
    width: int,
    utxos: list[UTXOInfo],
    mark_title: str = "Sel",
    state_tokens: Sequence[str] = _STATE_TOKENS,
) -> list[tuple[str, int]]:
    """Adaptive column widths following the ``info`` extended view.

    Every non-address column is exactly as wide as its (lowercase) title or
    the longest visible value, whichever is bigger; titles carry the units
    (``sats``, ``confs``) so the cells stay unit-free. Only the address
    column grabs the remaining terminal width (full layout); below the
    minimum width the compact layout drops address and confs. The outpoint
    no longer occupies a column -- the full value is shown in the footer's
    UTXO Status line. The tag column is capped at 11 characters so long
    BIP-329 labels cannot push the table past the terminal (system labels
    are 4 chars or shorter and do not need the cap); the full label always
    appears in the UTXO Status footer line. The state column width derives
    from ``state_tokens`` -- per view, so a view that never emits the wider
    tokens does not reserve space for them (the freeze manager passes its
    ``frozn``/``lock``/``spend`` subset; the selector keeps the full
    catalog, maximum 6, ``unconf``/``no-cj``/``oth-md``).

    ``mark_title`` names the leading checkbox-style column (``Sel`` in the
    selector, ``F`` in the freeze manager); it is drawn by the row renderer,
    not by :func:`format_utxo_line`.
    """
    tag_width = max(4, min(11, max((len(_label_col_value(u)) for u in utxos), default=0)))
    sats_width = max(4, max((len(f"{u.value:,}") for u in utxos), default=0))
    state_width = max(5, max((len(t) for t in state_tokens), default=0))
    columns = [
        (mark_title, 3),
        ("MD", max(2, max((len(f"m{u.mixdepth}") for u in utxos), default=0))),
        ("sats", sats_width),
        ("tag", tag_width),
        ("state", state_width),
    ]
    # The RU! column exists only when the wallet actually has address reuse
    # ("if and only if" rule: no reuse, no column). The bookkeeping width is
    # 1 because the cell renders flush as ``|RU!|`` / ``|   |`` (5 columns
    # incl. both border pipes) instead of the usual ``| cell |`` grid cell.
    if _has_address_reuse(utxos):
        columns.append(("RU!", 1))
    full_columns = columns[:2] + [
        ("address", 12),
        columns[2],
        ("confs", max(5, max((len(f"{u.confirmations:,}") for u in utxos), default=0))),
        *columns[3:],
    ]
    extra = width - _minimum_table_width(full_columns)
    if extra >= 0:
        full_columns[2] = ("address", 12 + extra)
        return full_columns
    return columns


def _minimum_table_width(columns: list[tuple[str, int]]) -> int:
    # Header-line length (sep_width + 1): the table's right border sits in
    # the terminal's last usable column, no spare cell remains to its right.
    # The RU! cell lives inside the table.
    return sum(size for _, size in columns) + 3 * (len(columns) - 1) + 4


def _table_sep_width(columns: list[tuple[str, int]]) -> int:
    """Separator span for ``columns``: row separators and footer separators.

    Matches ``len(header_line) - 1`` for the header line built from the same
    columns (left border column 0, content column 1); the right border sits
    at ``sep_width``. The footer builders use this to frame their
    separators exactly between the table's border columns.
    """
    return sum(size for _, size in columns) + 3 * (len(columns) - 1) + 3


def _has_address_reuse(utxos: list[UTXOInfo]) -> bool:
    """Whether any coin in the wallet shares its address (RU! column rule)."""
    address_utxo_counts: dict[str, int] = {}
    for utxo in utxos:
        address_utxo_counts[utxo.address] = address_utxo_counts.get(utxo.address, 0) + 1
    return any(_is_address_reused(utxo, address_utxo_counts) for utxo in utxos)


def _header_line(columns: list[tuple[str, int]]) -> str:
    """Build the table header line for ``columns`` (shared by both TUIs).

    The optional RU! column (present only with address reuse) renders flush
    as ``|RU!|`` -- no padding around the marker, nothing right of the
    table's right border; every other title is centered in its cell.
    """
    cells = " | ".join(f"{name:^{size}}" for name, size in columns if name != "RU!")
    if any(name == "RU!" for name, _ in columns):
        return "| " + cells + " |RU!|"
    return "| " + cells + " |"


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
    state_override: StateToken | None = None,
    reused: bool | None = None,
) -> str:
    """Format a single UTXO row with separate Label and State columns.

    ``mark_title`` names the checkbox-style column owned by the row
    renderer; it is excluded from the formatted line. ``state_override``
    replaces the selector's State computation (the freeze manager renders
    its own states: frozn/lock/spend, a subset of the shared catalog).
    ``reused`` marks the coin's address as shared (the callers know the
    wallet-wide address counts); without it only the auto-freeze reuse
    label can mark the row.
    """
    columns = columns or _table_columns(term_width, [utxo], mark_title)
    # RU! cell: flush marker when this coin shares its address, blanks keep
    # every row's right border aligned (no content right of the border).
    if reused is None:
        reused = utxo.label == AUTO_FREEZE_REUSE_LABEL
    ru_cell = "RU!" if reused else "   "
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
    cells = [(name, size) for name, size in columns if name not in {mark_title, "RU!"}]
    body = _format_cells(values, cells) + " |"
    if any(name == "RU!" for name, _ in columns):
        body += ru_cell + "|"
    return _clip(body, max_width)


def _utxo_state_col(
    utxo: UTXOInfo,
    excluded_outpoints: set[tuple[str, int]] | None = None,
    allowed_mixdepth: int | None = None,
    min_confirmations: int = 0,
    *,
    allow_fidelity_bonds: bool = True,
) -> StateToken:
    """Selection state for a UTXO row (also used in the footer).

    Delegates to the canonical catalog resolver
    :func:`jmwallet.wallet.rows.state_token` (priority order and the
    0-conf context split live there); this wrapper keeps the TUI import
    surface stable.
    """
    return state_token(
        utxo,
        excluded_outpoints,
        allowed_mixdepth,
        min_confirmations,
        allow_fidelity_bonds=allow_fidelity_bonds,
    )


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
    if utxo.label == "non-cj-change":
        return "Regular change (not from CoinJoin)"
    if utxo.label == "deposit":
        return "Address with funds from internal or external sources"
    if utxo.label:
        return f"Label: {utxo.label}"
    return ""


def _wrap_footer_fields(text: str, width: int) -> list[str]:
    """Wrap between fields so amounts and their units always stay together.

    A single field longer than ``width`` (a full txid at a narrow terminal)
    is hard-chunked so it still renders instead of overflowing the frame.
    """
    lines: list[str] = []
    for segment in text.split("\n"):
        current = ""
        for field in segment.split(" | "):
            if len(field) > width:
                if current:
                    lines.append(current)
                    current = ""
                chunks = [field[i : i + width] for i in range(0, len(field), width)]
                lines.extend(chunks[:-1])
                field = chunks[-1]
            candidate = f"{current} | {field}" if current else field
            if len(candidate) > width and current:
                lines.append(current)
                current = field
            else:
                current = candidate
        lines.append(current)
    return lines


def _footer_content_width(sep_width: int) -> int:
    """Text width inside a framed footer row (borders plus one blank column)."""
    return max(1, sep_width - 4)


def align_control_columns(text: str, width: int) -> list[str]:
    """Lay out a controls hint as aligned key/columns in a footer box.

    The fields (split on ``" | "``) are arranged in a column grid: each
    column is as wide as its longest field, so the ``|`` separators line
    up vertically across rows -- the controls read as a key table. Fields
    flow to fill a row only while the grid fits ``width``; the last row
    may carry fewer columns. When everything fits on one line no grid is
    needed and the hint renders unwrapped (padded by the footer frame
    later). A field wider than ``width`` alone falls back to the plain
    field wrap (:func:`_wrap_footer_fields`), which hard-chunks it.
    """
    fields = text.split(" | ")
    if len(fields) == 1 or max(len(field) for field in fields) > width:
        return _wrap_footer_fields(text, width)
    if sum(len(field) for field in fields) + 3 * (len(fields) - 1) <= width:
        return [text]

    def grid(rows_of_fields: list[list[str]]) -> list[str]:
        """Render rows on the shared column grid (longest field per column)."""
        column_count = max(len(row) for row in rows_of_fields)
        column_widths = [
            max(len(row[i]) for row in rows_of_fields if i < len(row)) for i in range(column_count)
        ]
        lines = []
        for row in rows_of_fields:
            cells = [field.ljust(column_widths[i]) for i, field in enumerate(row[:-1])]
            cells.append(row[-1])
            lines.append(" | ".join(cells))
        return lines

    # Greedy packing on the shared grid: try each column count, prefer the
    # layout with the fewest lines; the widest grid wins ties (fewer rows
    # per screen height budget). One field per row is always possible here:
    # a field wider than ``width`` alone was handled above.
    best: list[str] | None = None
    best_cols = 0
    for columns in range(len(fields), 0, -1):
        rows_of_fields = [fields[i : i + columns] for i in range(0, len(fields), columns)]
        rendered = grid(rows_of_fields)
        if max(len(line) for line in rendered) <= width:
            best, best_cols = rendered, columns
            break
    if best is None:  # unreachable: single-column always fits (checked above)
        return _wrap_footer_fields(text, width)
    if best_cols == len(fields):
        return [text]
    return best


def utxo_status_text(utxo: UTXOInfo, extra: list[str] | None = None) -> str:
    """The footer's UTXO Status payload for ``utxo`` (shared by both TUIs).

    Line 1 is ``Outpoint:`` with the full txid:vout; wrap it with
    :func:`wrap_status_text`, which moves the complete value below the
    label when the line is too wide instead of breaking mid-value. The
    explanation -- the cursor description plus any caller-supplied
    restriction fields -- always goes on its own line below, without a
    separator at the line break.
    """
    text = f"Outpoint: {utxo.txid}:{utxo.vout}"
    fields = [utxo_cursor_description(utxo), *(extra or [])]
    fields = [field for field in fields if field]
    if fields:
        text += "\n" + " | ".join(fields)
    return text


def wrap_status_text(text: str, width: int) -> list[str]:
    """Wrap a UTXO Status payload, keeping the outpoint value whole.

    The ``Outpoint:`` label keeps the value on its line only while it fits;
    otherwise the complete ``txid:vout`` moves to its own line below the
    label (no mid-value break). Only a value too wide for the box even on
    its own line is hard-chunked. The explanation below (after ``\\n``)
    wraps like any other footer field via :func:`_wrap_footer_fields`.
    """
    head, _, tail = text.partition("\n")
    lines: list[str] = []
    prefix = "Outpoint: "
    if head.startswith(prefix):
        value = head[len(prefix) :]
        if len(head) > width:
            lines.append("Outpoint:")
            if len(value) > width:
                lines.extend(value[i : i + width] for i in range(0, len(value), width))
            else:
                lines.append(value)
        else:
            lines.append(head)
    else:
        lines.extend(_wrap_footer_fields(head, width))
    if tail:
        lines.extend(_wrap_footer_fields(tail, width))
    return lines


def _draw_footer(
    stdscr: curses.window, start: int, rows: list[tuple[str, int]], sep_width: int
) -> None:
    """Draw precomputed footer rows framed by the table's border columns.

    Every text row becomes ``| <text>....|`` drawn in three segments: plain
    left border, the text with its own attribute, then plain padding and
    right border pushed to the table's right edge (footer box, same width
    as the table). Pre-framed separator rows pass through unchanged.
    """
    import curses

    for offset, (line, attr) in enumerate(rows):
        row = start + offset
        if line.startswith("|") and line.endswith("|") and len(line) > 1:
            stdscr.addstr(row, 0, line, attr)
            continue
        text = f" {line}"
        stdscr.addstr(row, 0, "|", curses.A_NORMAL)
        stdscr.addstr(row, 1, text, attr)
        if 1 + len(text) <= sep_width:
            # Padding and right border stay plain: only the text carries the
            # row's attribute, the box frame never takes the text color.
            stdscr.addstr(
                row, 1 + len(text), " " * (sep_width - 1 - len(text)) + "|", curses.A_NORMAL
            )


_STATUS_LABEL_WIDTH = 15  # "Active Wallet: " and "Fingerprint:   " share one value column


def status_header_lines(
    sep_width: int, wallet_name: str, fingerprint: str, maker_status: str, max_lines: int = 2
) -> list[str]:
    """Build the framed status lines shown above the column titles.

    Layout follows the legacy whiptail ``menu_header``: values align in
    one column (both labels are 15 columns wide), and the fields degrade
    in three stages as the terminal narrows:

    1. one line: name | fingerprint | maker status;
    2. name + maker status, fingerprint below (aligned under the name);
    3. name alone, fingerprint + maker status below.

    Every stage keeps the name and fingerprint values aligned; stage 4 is
    per-value clipping inside stage 2/3 layouts (a value too long even for
    its line is clipped to the available columns, never overflowing the
    right border). Widths are measured in terminal columns
    (:func:`_display_width`), never character count.

    ``max_lines`` is the height budget the caller can afford: with 1 only
    the single-line form is built (values clipped to fit), so a terminal
    narrow enough to force the two-line cascade but too short to pay for
    the extra row still renders instead of hitting the resize prompt --
    any screen that rendered before the status block keeps rendering
    with it.
    """
    body_width = max(1, sep_width - 2)
    name_prefix = "Active Wallet: "
    fp_prefix = "Fingerprint:   "
    assert len(name_prefix) == _STATUS_LABEL_WIDTH == len(fp_prefix)

    def clip_to(text: str, width: int) -> str:
        """Column-exact clip (``_clip`` counts characters, not columns)."""
        if _display_width(text) <= width:
            return text
        while text and _display_width(text + "...") > width:
            text = text[:-1]
        return text + "..."

    maker_field = f"Maker Bot: {maker_status}"
    name_full = f"{name_prefix}{wallet_name}"
    fp_full = f"{fp_prefix}{fingerprint}"

    if max_lines <= 1:
        line = f"{name_full} | {fp_full} | {maker_field}"
        if _display_width(line) <= body_width:
            return [line]
        # Too tight for everything: keep all three fields and clip the name
        # value (the fingerprint and maker status are short, fixed-shape
        # values; dropping a whole field would hide more information).
        fixed = _display_width(f"{name_prefix} | {fp_full} | {maker_field}")
        budget = body_width - fixed
        if budget >= 4:  # room for at least one name char plus the "..." marker
            fitted = f"{name_prefix}{clip_to(wallet_name, budget)} | {fp_full} | {maker_field}"
            if _display_width(fitted) <= body_width:
                return [fitted]
        # Last resort: head-clip the whole line.
        return [clip_to(line, body_width)]

    lines: list[str] = []
    one_line = f"{name_full} | {fp_full} | {maker_field}"
    if _display_width(one_line) <= body_width:
        lines.append(one_line)
    else:
        two_line = f"{name_full} | {maker_field}"
        if _display_width(two_line) <= body_width:
            lines.append(two_line)
            lines.append(fp_full)
        else:
            lines.append(name_full)
            lines.append(f"{fp_full} | {maker_field}")

    clipped: list[str] = []
    for line in lines:
        clipped.append(clip_to(line, body_width))
    return clipped


def draw_status_block(stdscr: curses.window, row: int, lines: list[str], sep_width: int) -> int:
    """Draw the framed status block; return the number of rows drawn.

    Every line is framed left/right with plain border columns and padded
    to the table's right edge (same geometry as the footer box); the
    horizontal rules above and below the block are drawn by the caller
    (the top rule is the existing title separator, the bottom rule is the
    existing header top edge -- they stay where they are, only their row
    numbers shift).
    """
    import curses

    value_attr = curses.color_pair(3) | curses.A_BOLD
    for line in lines:
        text_cols = _display_width(line)
        stdscr.addstr(row, 0, "|", curses.A_NORMAL)
        stdscr.addstr(row, 1, " " + line, value_attr)
        # Padding and right border stay plain: only the text carries the
        # row's attribute, the box frame never takes the text color.
        pad = sep_width - 2 - text_cols
        if pad > 0:
            stdscr.addstr(row, 2 + text_cols, " " * pad + "|", curses.A_NORMAL)
        elif sep_width >= 2:
            stdscr.addstr(row, sep_width, "|", curses.A_NORMAL)
        row += 1
    return len(lines)


def _draw_hrule(stdscr: curses.window, row: int, sep_width: int) -> None:
    """Draw a horizontal rule: dashes between the border columns.

    Closes the table's top/bottom edges; the right border sits in the
    terminal's last usable column (``sep_width``).
    """
    import curses

    stdscr.addstr(row, 0, "|", curses.A_NORMAL)
    stdscr.addstr(row, 1, "-" * (sep_width - 1), curses.A_NORMAL)
    stdscr.addstr(row, sep_width, "|", curses.A_NORMAL)


def _draw_frame_filler(stdscr: curses.window, start: int, end: int, sep_width: int) -> None:
    """Draw empty framed rows between the last coin row and the footer.

    Keeps the left/right border columns visually connected across the gap
    the terminal height leaves above the footer (the box never ends early);
    also guarantees at least one blank framed line between the wallet rows
    and the footer separator.
    """
    import curses

    for row in range(start, end):
        stdscr.addstr(row, 0, "|", curses.A_NORMAL)
        stdscr.addstr(row, sep_width, "|", curses.A_NORMAL)


def _help_overlay_rows(sep_width: int, state_tokens: Sequence[str]) -> list[tuple[str, int]]:
    """Build the framed help overlay rows: legend, close hint.

    The tag legend comes from the shared catalog (:data:`TAG_HELP`) --
    both TUIs emit the same tag set via :func:`jmwallet.wallet.rows.short_label`,
    so nothing is filtered there. The state legend lists exactly
    ``state_tokens``, the states the calling TUI can emit (the freeze
    manager's ``frozn``/``lock``/``spend`` subset, the selector's
    :data:`SELECTOR_STATE_TOKENS` catalog-minus-``pend``) -- the overlay
    must never explain a state this view cannot show. Entries render in
    the extended view's address-legend format (``token - full term:
    detail``). Every section header sits under a full-width rule (the
    leading rule doubles as the box's top edge), and the trailing rule
    after the close hint closes the box. Returns ``[(text, attr)]``
    without frame decoration -- :func:`_draw_footer` frames every row;
    separators must be pre-framed.
    """
    import curses

    content_width = max(1, sep_width - 2)
    separator = ("|" + "-" * (sep_width - 1) + "|", curses.A_NORMAL)
    rows: list[tuple[str, int]] = [separator]
    title_attr = curses.color_pair(3) | curses.A_BOLD
    body_attr = curses.A_NORMAL
    token_attr = curses.color_pair(3) | curses.A_BOLD

    rows.append(("tags (type of address or funds):", title_attr))
    for token, text in TAG_HELP.items():
        rows.extend(
            (line, body_attr) for line in _wrap_footer_fields(f"{token:<7} - {text}", content_width)
        )
    rows.append(separator)
    rows.append(("states (spendability):", title_attr))
    for token in state_tokens:
        # Lookup, not a skip: a state token without a STATE_HELP entry is
        # a catalog drift and should crash loudly, not vanish from the help.
        text = STATE_HELP[token]
        rows.extend(
            (line, body_attr) for line in _wrap_footer_fields(f"{token:<7} - {text}", content_width)
        )
    rows.extend(
        [
            separator,
            ("q/Esc: back to the list", token_attr),
            separator,
        ]
    )
    return rows


def _draw_help_overlay(
    stdscr: curses.window, rows: list[tuple[str, int]], sep_width: int, title: str
) -> None:
    """Draw the help overlay top-down over the whole screen (same frame).

    Mirrors the main screen's header geometry: the TUI's title sits
    centered on the second terminal row (em-dashes included, same color
    as the TUI title), and the framed help box starts below it -- the
    first row of ``rows`` is the box's top edge.
    """
    import curses

    stdscr.clear()
    height, width = stdscr.getmaxyx()
    stdscr.attron(curses.color_pair(3) | curses.A_BOLD)
    stdscr.addstr(1, 0, title.center(width)[:width])
    stdscr.attroff(curses.color_pair(3) | curses.A_BOLD)
    # Keep the last terminal row blank (same guard as the old full-screen
    # layout): rows[:height-3] with start 2 ends the box at height-2.
    _draw_footer(stdscr, 2, rows[: max(0, height - 3)], sep_width)
    stdscr.refresh()


def wait_for_help_close(stdscr: curses.window) -> bool:
    """Block until the overlay closes. Always returns to the list.

    Any key (``q``/``Esc`` per the controls hint) closes the help page; the
    TUI itself only quits from the list view, so a second ``q`` is needed
    there.
    """
    stdscr.getch()
    return True
