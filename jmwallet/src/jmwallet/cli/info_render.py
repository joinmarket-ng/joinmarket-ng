"""
'full' renderer for the extended wallet info view.

Consumes the canonical row model (:mod:`jmwallet.wallet.rows`) and produces
pipe-aligned table lines: one line per UTXO (hard rule), no elision, exact
confirmation counts. The renderer only formats the rows it is given — it
never re-derives labels, visibility or ordering (that is the model's job).

Column layout (regular branch tables share one global width vector, see
:func:`compute_widths`; fidelity-bond tables keep their own per-table
widths)::

    idx | address | sats | confs | tag | state | outpoint

``idx`` is the derivation index within the branch, rendered as ``/5`` — the
branch path printed once in the branch header above the table plus this cell
gives the full derivation path. The units live in the headers
(``sats``/``confs``), so the cells carry bare numbers. The ``RU!`` reuse
flag is not a column: it is appended at the end of the row, after the
outpoint (like the legacy ``(reused)`` suffix), so the rare edge case does
not tax the ``tag`` column width.

The fidelity bond branch adds a ``fb-date`` column — the locktime date
(``YY-MM-DD``) qualifying the fb-a/fb-x label — after ``state``, which
there shows the bond spendability (``lock`` while time-locked, ``spend``
once expired; a pending outgoing spend overrides it). Frozen UTXOs append
``frozn`` to that state. A 0-conf bond shows ``0`` under ``confs``
instead of an ``unconf`` state.
"""

from __future__ import annotations

from collections.abc import Sequence

from jmwallet.cli.ansi import ANSI_BOLD_RED, ANSI_BOLD_WHITE, ANSI_CYAN, colorize
from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH
from jmwallet.wallet.rows import (
    AddressRow,
    BranchSummaryRow,
    HiddenRow,
    MixdepthSummaryRow,
    Row,
    UtxoRow,
    fb_date_str,
)

# Units live in the headers; the cells carry bare numbers.
_BASE_HEADERS = ("idx", "address", "sats", "confs", "tag", "state", "outpoint")
# The fidelity bond branch adds the locktime date (fb-date) after the state
# column; there state shows the bond spendability (lock/spend).
_FB_HEADERS = ("idx", "address", "sats", "confs", "tag", "state", "fb-date", "outpoint")

_SEP = " | "

# All branch headers pad their name to this width so the derivation paths
# (and the account zpub in the external header) start in the same column.
# Driven by the longest name, "fidelity bond addr.".
BRANCH_NAME_WIDTH = len("fidelity bond addr.")


def separator_width(widths: Sequence[int]) -> int:
    """Length of a table's dashed separator line (``-+-``-joined column runs).

    Used to size the page-level separators and to right-extend shorter
    table separators, so every dashed line on the page has the same length.
    """
    return sum(widths) + 3 * (len(widths) - 1)


def _state_text(row: UtxoRow | AddressRow) -> str:
    """Spendability shown per row; ``spend`` is explicit (positive signal).

    Precedence: frozn > pend > unconf > spend. Empty addresses have no
    spendability unless an outgoing spend is pending. Only used for the
    regular branches — fidelity bond rows show their lock status instead
    (see :func:`_fb_state_text`).
    """
    if isinstance(row, AddressRow):
        return "pend" if row.pending else ""
    if row.frozen:
        return "frozn"
    if row.pending:
        return "pend"
    if row.unconfirmed:
        return "unconf"
    return "spend"


def _fb_state_text(row: UtxoRow | AddressRow) -> str:
    """Spendability for fidelity bond rows, retaining frozen indications.

    Time-locked bonds show ``lock``; expired ones show ``spend`` — the
    same token as regular branches, since expired bonds are spendable
    (via explicit selection or a bond-only sweep, never as a CoinJoin
    input). Empty bond addresses show no state (nothing to lock or spend).
    A 0-conf bond shows ``0`` under ``confs`` instead of an ``unconf`` state.
    """
    if isinstance(row, UtxoRow):
        state = "pend" if row.pending else "lock" if row.label == "fb-a" else "spend"
        return f"{state} frozn" if row.frozen else state
    return "pend" if row.pending else ""


def _format_row(
    row: UtxoRow | AddressRow, *, fidelity_bond: bool, num_width: int
) -> tuple[list[str], list[str]]:
    """Return ``(plain_cells, display_cells)`` for one row.

    The plain cells drive the column width computation; the display cells are
    what gets printed. They only differ in the red ``RU!`` marker, whose ANSI
    escapes would otherwise corrupt the width arithmetic. The marker is
    appended at the end of the row (after the outpoint), not inside the tag
    cell, so the rare reuse edge case does not stretch the tag column.

    The units live in the table headers (``sats``/``confs``), so the cells
    carry bare numbers; the first column is the derivation index rendered as
    ``/5`` (the branch path printed once above the table plus this cell gives
    the full derivation path). Confirmation counts are exact for all branches.
    """
    if isinstance(row, UtxoRow):
        amount = f"{row.utxo.value:>{num_width},}"
        confs = f"{row.utxo.confirmations:,}"
        outpoint = row.utxo.outpoint
    else:
        amount = f"{'0':>{num_width}}"
        confs = ""
        outpoint = ""

    label: str = row.label
    if row.user_label:
        label += f' "{row.user_label}"'

    if row.reused:
        # Row-level flag, not part of the tag: appended after the outpoint
        # (like the legacy "(reused)" suffix), so address reuse does not
        # stretch the tag column.
        joiner = " " if outpoint else ""
        plain_outpoint = f"{outpoint}{joiner}RU!"
        display_outpoint = f"{outpoint}{joiner}{colorize('RU!', ANSI_BOLD_RED)}"
    else:
        plain_outpoint = display_outpoint = outpoint

    idx = f"/{row.address_index}"
    plain = [idx, row.address, amount, confs, label]
    display = [idx, row.address, amount, confs, label]
    if fidelity_bond:
        state = _fb_state_text(row)
        fb_date = fb_date_str(row.locktime) if row.locktime is not None else ""
        plain.extend([state, fb_date, plain_outpoint])
        display.extend([state, fb_date, display_outpoint])
    else:
        state = _state_text(row)
        plain.extend([state, plain_outpoint])
        display.extend([state, display_outpoint])
    return plain, display


def _emit(cells: Sequence[str]) -> str:
    """Join padded cells into a closed grid: every column separator stays in
    place, even when trailing cells are empty (the line then ends at the last
    pipe, so no trailing whitespace is emitted)."""
    return _SEP.join(cells).rstrip()


def compute_widths(
    rows: Sequence[Row], *, fidelity_bond: bool = False, num_width: int | None = None
) -> list[int]:
    """Column-width vector for a set of rows (header + data cells).

    The CLI computes this once over the rows of all regular branches of a
    page and passes it to :func:`render_branch_table` via ``widths``, so
    every regular branch table shares the same column widths (global
    alignment). Fidelity-bond tables use their own fully independent vector
    (wider addresses, ``fb-date``, own amount width).
    """
    headers = _FB_HEADERS if fidelity_bond else _BASE_HEADERS
    data_rows = [r for r in rows if isinstance(r, (UtxoRow, AddressRow))]
    if num_width is None:
        num_width = max(
            (len(f"{r.utxo.value:,}") for r in data_rows if isinstance(r, UtxoRow)),
            default=1,
        )
    plain_rows = [
        _format_row(r, fidelity_bond=fidelity_bond, num_width=num_width)[0] for r in data_rows
    ]
    return [max([len(headers[i]), *[len(r[i]) for r in plain_rows]]) for i in range(len(headers))]


def render_branch_table(
    rows: Sequence[Row],
    *,
    fidelity_bond: bool = False,
    num_width: int | None = None,
    widths: Sequence[int] | None = None,
    sep_width: int | None = None,
) -> list[str]:
    """Render the rows of one branch as a pipe-aligned table.

    Returns header, separator, one line per row (hard rule: one line per
    UTXO), plus the hidden-count note when a :class:`HiddenRow` is present.
    Every row spans all columns (closed grid), so empty trailing cells keep
    their separators. Column widths are computed over this table's header
    and data cells unless a ``widths`` vector (see :func:`compute_widths`)
    is passed to force global alignment across tables. ``num_width``
    right-aligns the amount cells; pass a wallet-global value to align the
    amounts across tables (default: computed from this table's rows).
    ``sep_width`` right-extends the dashed separator line with dashes so
    every separator on the page shares one length (no-op when the
    separator is already that long). Returns ``[]`` when there is nothing
    to show.
    """
    headers = _FB_HEADERS if fidelity_bond else _BASE_HEADERS
    data_rows = [r for r in rows if isinstance(r, (UtxoRow, AddressRow))]
    hidden = next((r for r in rows if isinstance(r, HiddenRow)), None)
    if not data_rows:
        # No table without data rows: only the hidden-count note, if any.
        return [render_hidden_note(hidden.hidden_count)] if hidden is not None else []

    if num_width is None:
        # Right-align the amount cells ("12,345") so magnitudes line up
        # across rows.
        num_width = max(
            (len(f"{r.utxo.value:,}") for r in data_rows if isinstance(r, UtxoRow)),
            default=1,
        )

    plain_rows: list[list[str]] = []
    display_rows: list[list[str]] = []
    for row in data_rows:
        plain, display = _format_row(row, fidelity_bond=fidelity_bond, num_width=num_width)
        plain_rows.append(plain)
        display_rows.append(display)

    if widths is None:
        widths = compute_widths(data_rows, fidelity_bond=fidelity_bond, num_width=num_width)

    # The idx column is right-aligned (the /-prefixed index reads like the
    # tail of the branch path); all other columns are left-aligned.
    separator = "-+-".join("-" * w for w in widths)
    if sep_width is not None:
        separator = separator.ljust(sep_width, "-")
    lines = [
        _emit(
            [
                headers[i].rjust(widths[i]) if i == 0 else headers[i].ljust(widths[i])
                for i in range(len(headers))
            ]
        ),
        separator,
    ]
    for plain, display in zip(plain_rows, display_rows, strict=True):
        # Pad by the plain length so ANSI escapes in the display cells (the
        # red RU! marker) do not break the column alignment.
        padded = [
            " " * (widths[i] - len(plain[i])) + display[i]
            if i == 0
            else display[i] + " " * (widths[i] - len(plain[i]))
            for i in range(len(widths))
        ]
        lines.append(_emit(padded))
    if hidden is not None:
        lines.append(render_hidden_note(hidden.hidden_count))
    return lines


def render_hidden_note(hidden_count: int) -> str:
    """Note line for the empty addresses hidden by the gap/show-empty logic."""
    return f"({hidden_count} empty addresses hidden; pass --show-empty to display)"


def render_branch_summary(summary: BranchSummaryRow) -> str:
    """Per-branch balance line, fed from the summary row.

    Fidelity bond funds are excluded from normal automatic selection; their breakdown
    distinguishes time-locked from expired funds (chain facts). The final
    figure is the honestly spendable part: expired bonds minus frozen
    ones — a frozen expired bond is excluded from any transaction until
    unfrozen.
    """
    if summary.branch == FIDELITY_BOND_BRANCH:
        bond_parts: list[str] = []
        if summary.locked_balance > 0:
            bond_parts.append(f"{summary.locked_balance:,} time-locked")
        if summary.expired_balance > 0:
            bond_parts.append(f"{summary.expired_balance:,} expired")
        detail = f"{' + '.join(bond_parts)}; " if bond_parts else ""
        return (
            f"Balance: {summary.balance:,} sats "
            f"({detail}spendable (explicit): {summary.expired_spendable:,} sats)"
        )
    return f"Balance: {summary.balance:,} sats (spendable: {summary.spendable:,} sats)"


def render_mixdepth_summary(summary: MixdepthSummaryRow) -> str:
    """Mixdepth total line (legacy wording, fixed padding after the label)."""
    # Fixed padding instead of a tab: terminal tab stops are unpredictable.
    label = colorize(f"Balance for mixdepth {summary.mixdepth}:".ljust(24), ANSI_CYAN)
    return f"{label}{summary.total_balance:,} sats (spendable: {summary.spendable_balance:,} sats)"


def render_legend(*, show_fidelity_bonds: bool = True) -> list[str]:
    """Legend for the three-level vocabulary: tags, states, and the RU! flag.

    ``show_fidelity_bonds=False`` drops the fb-a/fb-x tags and the lock
    state — used when the fidelity bond branch is hidden from the page
    (``wallet.info_show_fidelity_bonds = false``).
    """
    lines = [
        "",
        colorize("Address legend:", ANSI_BOLD_WHITE),
        "  tags (type of address or funds):",
        "    new    - unused, safe for receiving",
        "    dep    - deposit: address with funds from internal or external sources",
        "    cj-o   - coinjoin output: mixed funds",
        "    cj-c   - coinjoin change: change output from a CoinJoin "
        "(deanonymising, keep separate)",
        "    chg    - change: regular change (not from a CoinJoin)",
        "    used   - previously used, now empty (do not reuse)",
        "    flag   - flagged: shared with peers but tx failed (do not reuse)",
        '    rsvd   - reserved: previously issued or set aside (label in "quotes"); do not reuse',
    ]
    if show_fidelity_bonds:
        lines += [
            "    fb-a   - fidelity bond active: time-locked until the fb-date",
            "    fb-x   - fidelity bond expired: past its chain locktime; spendable via "
            "explicit selection or bond-only sweep (when no regular inputs qualify), "
            "never as a CoinJoin input",
        ]
    # The fb-x exception only makes sense when the bond vocabulary is on
    # the page; hidden bonds get the plain spend line (no dangling fb-x).
    spend_line = "    spend  - spendable: usable in any transaction"
    if show_fidelity_bonds:
        spend_line += " (except fidelity bonds: see fb-x)"
    lines += [
        "  states (spendability):",
        spend_line,
        "    frozn  - frozen: excluded from any transaction (manage with jm-wallet freeze)",
        "    pend   - pending: outgoing transaction involving this address not yet confirmed",
        "    unconf - unconfirmed: UTXO has no confirmations yet",
    ]
    if show_fidelity_bonds:
        lines += [
            "    lock   - fidelity bond UTXO still time-locked (until fb-date)",
        ]
    lines += [
        "  flags:",
        f"    {colorize('RU!', ANSI_BOLD_RED)}    - "
        "reused: address was paid to more than once "
        "(privacy warning; avoid reuse), shown at the end of a row",
    ]
    return lines
