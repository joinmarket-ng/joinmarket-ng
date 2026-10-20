"""``compact`` renderer for the extended wallet info view (``--view compact``).

An 80-column variant of the ``full`` view (:mod:`jmwallet.cli.info_render`)
that trades vertical space for width:

- every UTXO occupies two lines: the pipe-aligned row carries
  ``idx | address | sats | confs | tag``; the detail line
  underneath, indented to the address column, holds the spendability
  state and the full, copyable outpoint;
- every multi-line entry is closed by a dash line -- funded UTXOs and
  unfunded bond rows (fb-date detail) alike -- so each entry reads as
  its own block; only single-line empty addresses get none;
- fidelity bond rows share the same columns but get a third line: line 2
  carries the lock/spend state (the full view's vocabulary, frozen
  suffix included) plus the locktime date, line 3 the full, copyable
  outpoint -- state + date + outpoint on one line would break the
  budget; the 62-char bech32 bond address is middle-elided into the
  address column; unfunded bond rows keep the date-only detail;
- empty addresses stay on the grid with blank sats/confs cells (no ``0``
  amount -- zero carries no information);
- all tables of the page share one column geometry: idx, address, sats,
  confs and tag are aligned across branches and mixdepths. When a very
  large amount would push the widest ``RU!`` row past the budget, the
  address column is the shock absorber and elides harder (floor
  ``_ADDR_MIN``);
- a trailing ``"user label"`` is truncated to the remaining width budget;
  the red ``RU!`` reused flag is never dropped;
- every dashed line on the page (table separators, per-entry closing
  lines and mixdepth frames) shares one length: the widest visible
  content line;
- the legend prints the full glossary (the ``full`` view's entries and
  texts, wrapped to the budget with hanging indents): narrow-terminal
  users cannot fall back to ``--view full``, so nothing is delegated.

No rendered line exceeds 80 visible characters.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from textwrap import wrap

from jmwallet.cli.ansi import (
    ANSI_BOLD_CYAN,
    ANSI_BOLD_RED,
    ANSI_BOLD_WHITE,
    ANSI_CYAN,
    colorize,
)
from jmwallet.cli.info_render import (
    _SEP,
    BRANCH_NAME_WIDTH,
    _fb_state_text,
    _state_text,
    render_branch_summary,
    render_hidden_note,
    render_mixdepth_summary,
)
from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH
from jmwallet.wallet.rows import (
    AddressRow,
    BranchSummaryRow,
    MixdepthSummaryRow,
    Row,
    UtxoRow,
    fb_date_str,
    summarize_branch,
    summarize_mixdepth,
)

MAX_LINE = 80
"""Per-line width budget in visible characters."""

_ADDR_WIDTH = 42
"""Default address column width. A regular 42-char bech32 address fits
as-is; the 62-char bech32 fidelity bond addresses (P2WSH timelocks) are
middle-elided down to this."""

_ADDR_MIN = 24
"""Hard floor for the address column. Very large amounts widen the global
sats column; the address column absorbs that by eliding harder, but never
below this (a bech32 prefix plus a few recognizable chars on both ends)."""

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# One branch of one mixdepth: (branch, name, derivation path, rows).
BranchPlan = tuple[int, str, str, list[Row]]
# One mixdepth: (mixdepth, branches) -- the full view's plan shape minus
# the account zpub (compact drops it from the branch header).
MixdepthPlan = tuple[int, list[BranchPlan]]

# Page line under construction: a content string, or ("dash", joints) for a
# dashed line; joints keeps a table's column crosses, "" marks a plain
# mixdepth frame line. Dash length is decided once the widest content line
# is known.
_PageLine = str | tuple[str, str]


def _visible_len(line: str) -> int:
    """Length of ``line`` with ANSI color codes stripped."""
    return len(_ANSI_RE.sub("", line))


def _elide_address(address: str, width: int = _ADDR_WIDTH) -> str:
    """Middle-elide to ``width`` chars, balanced head/tail around a one-char
    ellipsis (keeps the bech32 prefix and checksum ends recognizable)."""
    if len(address) <= width:
        return address
    head = (width - 1) // 2
    return f"{address[:head]}\u2026{address[-(width - 1 - head) :]}"


def _appendages(row: UtxoRow | AddressRow, line_len: int) -> str:
    """Trailing ``"user label"`` (truncated) and red RU! -- RU! never drops."""
    suffix = ""
    budget = MAX_LINE - line_len - (4 if row.reused else 0)
    if row.user_label and budget >= 5:
        label = f' "{row.user_label}"'
        if len(label) > budget:
            label = f' "{row.user_label[: budget - 4]}\u2026"'
        suffix += label
    if row.reused:
        suffix += " " + colorize("RU!", ANSI_BOLD_RED)
    return suffix


# Both table types share one column layout: idx | address | sats | confs | tag.
_REGULAR_HEADERS = ["idx", "address", "sats", "confs", "tag"]
_FB_HEADERS = ["idx", "address", "sats", "confs", "tag"]


def _regular_cells(row: UtxoRow | AddressRow, *, num_width: int, addr_width: int) -> list[str]:
    """Regular-branch line-1 cells; empty addresses keep blank sats/confs cells."""
    address = _elide_address(row.address, addr_width)
    if isinstance(row, UtxoRow):
        amount = f"{row.utxo.value:,}".rjust(num_width)
        confs = f"{row.utxo.confirmations:,}"
        return [f"/{row.address_index}", address, amount, confs, row.label]
    return [f"/{row.address_index}", address, "".rjust(num_width), "", row.label]


def _fb_cells(row: UtxoRow | AddressRow, *, num_width: int, addr_width: int) -> list[str]:
    """Fidelity-bond cells. Empty bonds stay on the grid: a blank amount cell
    keeps the tag under its header (collapse buys nothing without labels)."""
    cells = [f"/{row.address_index}", _elide_address(row.address, addr_width)]
    if isinstance(row, UtxoRow):
        cells.append(f"{row.utxo.value:,}".rjust(num_width))
        # Exact confs, mirroring the full view.
        cells.append(f"{row.utxo.confirmations:,}")
    else:
        cells.append("".rjust(num_width))
        cells.append("")
    cells.append(row.label)
    return cells


def _detail_lines(row: UtxoRow | AddressRow, *, fb: bool) -> list[str]:
    """Content of a row's detail line(s) (no indent -- the table adds it):

    regular UTXOs get one line (state + full, copyable outpoint); funded
    bond rows get two -- the lock/spend state plus locktime date, then
    the outpoint -- because state + date + outpoint on one line would
    break the 80-column budget. Confs live on the row's ``confs`` column
    for both table types.
    """
    if isinstance(row, AddressRow):
        # Mirrors the full view, which shows state and fb-date in separate
        # columns: a pending unfunded bond row keeps both.
        date = fb_date_str(row.locktime) if fb and row.locktime is not None else ""
        if row.pending:
            return [f"pend {date}".strip()]
        return [date] if date else []
    if fb:
        state = _fb_state_text(row)
        date = fb_date_str(row.locktime) if row.locktime is not None else ""
        return [f"{state} {date}".strip(), row.utxo.outpoint]
    return [f"{_state_text(row)} {row.utxo.outpoint}"]


def _render_table(
    headers: list[str],
    rows: list[tuple[list[str], UtxoRow | AddressRow]],
    *,
    fb: bool,
    min_widths: Sequence[int],
) -> list[_PageLine]:
    """Pipe table: header, ("dash", joints) separator, one line per row
    (+ detail lines). Every two-line entry is closed by a plain dash
    line, so consecutive entries read as separate blocks.

    The index column is right-aligned. ``min_widths`` carries the
    page-global column widths, so every table on the page shares one
    geometry (sats/confs/tag aligned across branches and mixdepths). Detail
    lines are indented to the address column (``widths[0] + len(_SEP)``);
    when the longest detail would overflow, one clamped indent is shared
    by all detail lines of the table (keeps the bond details aligned).
    """
    if not rows:
        return []
    widths = [
        max(len(header), min_widths[i], *[len(cells[i]) for cells, _ in rows if len(cells) > i])
        for i, header in enumerate(headers)
    ]

    def padded(cells: list[str]) -> str:
        return _SEP.join(
            cells[i].rjust(widths[i]) if i == 0 else cells[i].ljust(widths[i])
            for i in range(len(cells))
        ).rstrip()

    separator = "-+-".join("-" * width for width in widths)
    lines: list[_PageLine] = [padded(headers), ("dash", separator)]
    details = [_detail_lines(row, fb=fb) for _, row in rows]
    detail_indent = max(
        min(
            widths[0] + len(_SEP),
            MAX_LINE - max((_visible_len(d) for ds in details for d in ds), default=0),
        ),
        0,
    )
    for (cells, row), row_details in zip(rows, details, strict=True):
        line = padded(cells)
        line += _appendages(row, len(line))
        lines.append(line)
        for detail in row_details:
            lines.append(f"{' ' * detail_indent}{detail}")
        if row_details:
            lines.append(("dash", ""))
    return lines


def render_compact_legend(
    *, has_locked_bond: bool = False, has_expired_bond: bool = False
) -> list[str]:
    """The full legend glossary, wrapped to the 80-column budget.

    Same entries and texts as :func:`jmwallet.cli.info_render.render_legend`
    (a consistency test pins them token by token): narrow-terminal users
    cannot fall back to ``--view full``, so the explanations are not
    delegated. Long entries wrap with a hanging indent under the text
    column. Bond vocabulary appears iff the corresponding bond state
    exists in the (unfiltered) wallet data -- the page references bond
    funds even when the bond branch is hidden (hidden-bond notice,
    balance footer), so, like the footer, the legend follows existence,
    not the display config. The ``spend`` gloss carries its fb-x
    exception only when an expired bond exists: active bonds show the
    ``lock`` state, never ``spend``.
    """

    def entry(token: str, text: str) -> list[str]:
        indent = f"    {token:<6} - "
        return wrap(
            text,
            width=MAX_LINE,
            initial_indent=indent,
            subsequent_indent=" " * len(indent),
        )

    lines = [
        "",
        colorize("Address legend:", ANSI_BOLD_WHITE),
        "  tags (type of address or funds):",
        *entry("new", "unused, safe for receiving"),
        *entry("dep", "deposit: address with funds from internal or external sources"),
        *entry("cj-o", "coinjoin output: mixed funds"),
        *entry(
            "cj-c",
            "coinjoin change: change output from a CoinJoin (deanonymising, keep separate)",
        ),
        *entry("chg", "change: regular change (not from a CoinJoin)"),
        *entry("used", "previously used, now empty (do not reuse)"),
        *entry("flag", "flagged: shared with peers but tx failed (do not reuse)"),
        *entry(
            "rsvd",
            'reserved: previously issued or set aside (label in "quotes"); do not reuse',
        ),
    ]
    if has_locked_bond:
        lines += entry("fb-a", "fidelity bond active: time-locked until the fb-date")
    if has_expired_bond:
        lines += entry(
            "fb-x",
            "fidelity bond expired: past its chain locktime; spendable via explicit "
            "selection or bond-only sweep (when no regular inputs qualify), never as "
            "a CoinJoin input",
        )
    spend = "spendable: usable in any transaction"
    if has_expired_bond:
        spend += " (except fidelity bonds: see fb-x)"
    lines += [
        "  states (spendability):",
        *entry("spend", spend),
        *entry(
            "frozn",
            "frozen: excluded from any transaction (manage with jm-wallet freeze)",
        ),
        *entry(
            "pend",
            "pending: outgoing transaction involving this address not yet confirmed",
        ),
        *entry("unconf", "unconfirmed: UTXO has no confirmations yet"),
    ]
    if has_locked_bond:
        lines += entry("lock", "fidelity bond UTXO still time-locked (until fb-date)")
    lines.append("  flags:")
    # RU! is colorized, so it is wrapped with a plain prefix first and the
    # colorized token substituted afterwards -- ANSI codes would break
    # textwrap's width accounting.
    ru_text = (
        "reused: address was paid to more than once "
        "(privacy warning; avoid reuse), shown at the end of a row"
    )
    ru_lines = wrap(ru_text, width=MAX_LINE, initial_indent=" " * 13, subsequent_indent=" " * 13)
    ru_lines[0] = f"    {colorize('RU!', ANSI_BOLD_RED)}    - {ru_lines[0].strip()}"
    lines += ru_lines
    line2 = "  line 2: state, full outpoint (under the address column)"
    if has_locked_bond or has_expired_bond:
        line2 = "  line 2: state, full outpoint (bonds: state + date, outpoint on 3rd line)"
    lines.append(line2)
    return lines


def render_compact_fb_summary(summary: BranchSummaryRow) -> list[str]:
    """Shortened fidelity-bond summary (``locked`` instead of ``time-locked``).

    Mirrors the full view's spendability split: expired bonds report as
    ``spendable (explicit)`` excluding the frozen ones. Unlike the full
    view, the frozen amount is not annotated here: it already appears in
    the page footer (``- fidelity bonds expired (frozen)``) and the suffix
    would break the 80-column budget for large balances; ``--view full``
    carries the full annotation. Uses the one-line wording when it fits the
    width budget; larger bond balances split into a balance line plus an
    indented breakdown line, mirroring :func:`render_compact_mixdepth_summary`,
    so the locked/expired detail is never dropped. The final figure is the
    honestly spendable part -- expired bonds minus frozen ones, spendable
    only via explicit selection or a bond-only sweep (same rule as the full
    view).
    """
    parts: list[str] = []
    if summary.locked_balance > 0:
        parts.append(f"{summary.locked_balance:,} locked")
    if summary.expired_balance > 0:
        parts.append(f"{summary.expired_balance:,} expired")
    detail = f"{' + '.join(parts)}; " if parts else ""
    spendable = f"spendable (explicit): {summary.expired_spendable:,} sats"
    one_liner = f"Balance: {summary.balance:,} sats ({detail}{spendable})"
    if len(one_liner) <= MAX_LINE:
        return [one_liner]
    return [
        f"Balance: {summary.balance:,} sats",
        f"  {detail}{spendable}",
    ]


def render_compact_mixdepth_summary(summary: MixdepthSummaryRow) -> list[str]:
    """Mixdepth total line(s). Uses the full view's one-line wording when it
    fits; very large balances split into a balance line plus an indented
    ``spendable:`` line to stay within the width budget."""
    one_liner = render_mixdepth_summary(summary)
    if _visible_len(one_liner) <= MAX_LINE:
        return [one_liner]
    label = colorize(f"Balance for mixdepth {summary.mixdepth}:".ljust(24), ANSI_CYAN)
    return [
        f"{label}{summary.total_balance:,} sats",
        f"  spendable: {summary.spendable_balance:,} sats",
    ]


def render_compact_page(
    plan: Sequence[MixdepthPlan],
    *,
    hidden_fb_notices: Mapping[int, tuple[str, str]] | None = None,
    hidden_total: int = 0,
    now: int | None = None,
    has_locked_bond: bool = False,
    has_expired_bond: bool = False,
) -> list[str]:
    """Render the whole compact page and return its lines.

    ``plan`` carries one ``(mixdepth, branches)`` entry per mixdepth (see
    :data:`MixdepthPlan`). ``hidden_fb_notices`` maps a mixdepth to the
    ``(base, hint)`` notice parts printed in place of the hidden fidelity
    bond table (base line, hint indented on the next line); ``hidden_total``
    feeds the aggregated hidden-addresses note at the end of the page.
    ``now`` is the chain-time snapshot for bond locktime evaluation;
    ``has_locked_bond``/``has_expired_bond`` describe bond-state existence
    in the unfiltered wallet data and drive the legend's bond vocabulary.
    """
    # One global column geometry for every table on the page: sats is as
    # wide as the largest amount of the whole wallet, idx/tag cover their
    # widest cell, and the address column absorbs whatever is left. The
    # address width is chosen so even the widest row plus a trailing RU!
    # fits the budget: 4 separators + " RU!" are the fixed overhead.
    all_rows = [
        row
        for _, branches in plan
        for _, _, _, rows in branches
        for row in rows
        if isinstance(row, (UtxoRow, AddressRow))
    ]
    num_width = max(
        (len(f"{row.utxo.value:,}") for row in all_rows if isinstance(row, UtxoRow)),
        default=1,
    )
    idx_width = max([len("idx")] + [len(f"/{row.address_index}") for row in all_rows])
    sats_width = max(len("sats"), num_width)
    confs_width = max(
        [len("confs")]
        + [len(f"{row.utxo.confirmations:,}") for row in all_rows if isinstance(row, UtxoRow)]
    )
    tag_width = max([len("tag")] + [len(row.label) for row in all_rows])
    fixed = idx_width + sats_width + confs_width + tag_width + 4 * len(_SEP) + len(" RU!")
    addr_width = max(_ADDR_MIN, min(_ADDR_WIDTH, MAX_LINE - fixed))
    grid_widths = (idx_width, addr_width, sats_width, confs_width, tag_width)

    # One pass: tables + summaries stream into the flat page-line list; the
    # dash length is decided afterwards, once the widest content line is
    # known.
    lines: list[_PageLine] = []
    lines.extend(
        render_compact_legend(has_locked_bond=has_locked_bond, has_expired_bond=has_expired_bond)
    )
    lines.append(("dash", ""))
    for md, branches in plan:
        lines.append("")
        lines.append(colorize(f"Mixdepth {md}", ANSI_BOLD_CYAN))
        # The hidden fidelity bond table is replaced by a two-line notice
        # exactly where the table would be (before the regular branches).
        if hidden_fb_notices and md in hidden_fb_notices:
            base, hint = hidden_fb_notices[md]
            lines.append("")
            # The notice can exceed the budget (multi-digit bond counts,
            # large totals); wrap it like the legend's RU! entry -- plain
            # first, the colorized prefix substituted back afterwards, or
            # ANSI codes would break textwrap's width accounting.
            plain_base = _ANSI_RE.sub("", base)
            if len(plain_base) > MAX_LINE:
                wrapped = wrap(plain_base, width=MAX_LINE, subsequent_indent="  ")
                wrapped[0] = wrapped[0].replace(
                    "fidelity bond addr.", colorize("fidelity bond addr.", ANSI_CYAN), 1
                )
                lines.extend(wrapped)
            else:
                lines.append(base)
            lines.append(f"  {hint}")
        summaries: list[BranchSummaryRow] = []
        for branch, name, path, rows in branches:
            fb = branch == FIDELITY_BOND_BRANCH
            cells_rows: list[tuple[list[str], UtxoRow | AddressRow]] = [
                (
                    _fb_cells(row, num_width=num_width, addr_width=addr_width)
                    if fb
                    else _regular_cells(row, num_width=num_width, addr_width=addr_width),
                    row,
                )
                for row in rows
                if isinstance(row, (UtxoRow, AddressRow))
            ]
            summary = summarize_branch(rows, mixdepth=md, branch=branch, now=now)
            summaries.append(summary)
            lines.append("")
            lines.append(f"{colorize(name.ljust(BRANCH_NAME_WIDTH), ANSI_CYAN)}  {path}")
            lines.extend(
                _render_table(
                    _FB_HEADERS if fb else _REGULAR_HEADERS,
                    cells_rows,
                    fb=fb,
                    min_widths=grid_widths,
                )
            )
            if fb:
                lines.extend(render_compact_fb_summary(summary))
            else:
                lines.append(render_branch_summary(summary))
        lines.extend(render_compact_mixdepth_summary(summarize_mixdepth(summaries, mixdepth=md)))
        lines.append(("dash", ""))
    if hidden_total > 0:
        lines.append(render_hidden_note(hidden_total))

    # Every dashed line shares one length: the widest visible content line.
    dash_width = max(_visible_len(line) for line in lines if isinstance(line, str))
    return [line[1].ljust(dash_width, "-") if isinstance(line, tuple) else line for line in lines]
