"""
Canonical row model for wallet info displays.

All wallet views (the static CLI ``info --extended`` today, the interactive
explorer later) are fed from a single, view-independent row model. Renderers
consume rows; they never re-derive visibility, labels or ordering from wallet
internals (a second derivation path drifting from the real display logic was
a bug source in earlier rework attempts).

Vocabulary (three levels, kept as separate row fields):

- ``label``: what the funds are (``new``, ``dep``, ``cj-o``, ``cj-c``,
  ``chg``, ``used``, ``flag``, ``rsvd``, ``fb-a``, ``fb-x``). Short display
  forms (max 4 chars) keep the label column narrow; the underlying
  ``AddressStatus`` keeps the historic long names (persisted in wallet
  metadata), e.g. ``non-cj-change`` is surfaced as ``chg`` (see
  ``_STATUS_TO_LABEL``).
- state: spendability, derived per row from the ``frozen``/``pending``
  flags and the confirmation count (``spend``/``frozn``/``pend``/
  ``unconf``; renderers show ``spend`` explicitly as a positive
  signal). Fidelity bonds carry their lock state in the label
  (``fb-a``/``fb-x``) and the locktime date as a separate data
  field (``locktime``, formatted via ``fb_date_str``).
- ``reused``: independent flag. A reused ``cj-o`` is still a ``cj-o``
  (label) that was reused (flag) — see issue #564.

Pipeline stages (deliberately separate, so later CLI/explorer filter and
sort flags become thin layers over tested stages)::

    build -> filter -> sort -> group -> render

The static CLI currently uses the identity filter and renders in build
order; the sort stage with :func:`base_order_key` exists for the explorer
and future flags.

Ordering note: the build stage preserves the input order of the address
lists, which for external/internal branches is derivation-index order and
for the fidelity bond branch is the service's locktime-then-index order
(the meaningful chronological axis for bonds). ``base_order_key`` defines
the canonical *sorted* order as (mixdepth, branch rank, derivation index,
intra-address position); applied to the fidelity bond branch this can
differ from the legacy locktime listing, which is acceptable because the
static renderer does not sort.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypeAlias

from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH
from jmwallet.wallet.models import AddressInfo, UTXOInfo

# Row-level labels: AddressStatus minus the pseudo-statuses "reused" (a
# flag, not a label) and "bond" (split into fb-a/fb-x by locktime), with
# the historic "non-cj-change" surfaced under its display name "chg"
# (see _STATUS_TO_LABEL). Short forms (max 4 chars) keep the column narrow.
RowLabel = Literal[
    "new",
    "dep",
    "cj-o",
    "cj-c",
    "chg",
    "used",
    "flag",
    "rsvd",
    "fb-a",
    "fb-x",
]

# Branch identifiers: 0 = external (receive), 1 = internal (change),
# FIDELITY_BOND_BRANCH (2) = fidelity bonds.
EXTERNAL_BRANCH = 0
INTERNAL_BRANCH = 1

# How many unused "new" addresses stay visible per branch when empty
# addresses are hidden (unchanged from the legacy display, see issue #463).
DEFAULT_NEW_ADDRESS_LIMIT = 6


@dataclass
class UtxoRow:
    """One row per funded UTXO (hard rule: one line per UTXO)."""

    mixdepth: int
    branch: int
    address_index: int
    path: str
    address: str
    label: RowLabel
    reused: bool
    pending: bool
    user_label: str
    utxo: UTXOInfo
    locktime: int | None = None  # fidelity bond locktime; source of the fb-date
    original_seq: int = 0  # UTXO position within its address (cache order)

    @property
    def frozen(self) -> bool:
        """Spendability deviation: frozen UTXOs are excluded from coin selection."""
        return self.utxo.frozen

    @property
    def unconfirmed(self) -> bool:
        return self.utxo.confirmations == 0


@dataclass
class AddressRow:
    """One row for a visible address without UTXOs.

    Covers ``new``/``rsvd`` (always shown) and, with ``show_empty``,
    ``used``/``flag`` addresses. Also used for unfunded fidelity
    bond addresses (redeemed bonds stay visible, with their locktime).
    ``reused`` carries the RU flag for empty-but-reused addresses so
    renderers can still warn about them.
    """

    mixdepth: int
    branch: int
    address_index: int
    path: str
    address: str
    label: RowLabel
    reused: bool
    pending: bool
    user_label: str
    locktime: int | None = None
    original_seq: int = 0


@dataclass
class HiddenRow:
    """Count of empty addresses hidden by the gap/show-empty logic."""

    mixdepth: int
    branch: int
    hidden_count: int


@dataclass
class BranchSummaryRow:
    """Aggregated balance for one branch of one mixdepth."""

    mixdepth: int
    branch: int
    balance: int
    frozen_balance: int = 0
    locked_balance: int = 0  # fb-a funds (fidelity bond branch only)
    expired_balance: int = 0  # fb-x funds (fidelity bond branch only)
    expired_frozen_balance: int = 0  # frozen fb-x funds (fidelity bond branch only)

    @property
    def spendable(self) -> int:
        """Balance usable for automatic coin selection.

        Fidelity bond funds are excluded from normal automatic selection
        (locked ones cannot move; expired ones need explicit selection or
        a bond-only sweep, see :attr:`expired_spendable`), so the branch
        contributes 0, matching the legacy "(spendable: 0 sats)" display.
        """
        if self.branch == FIDELITY_BOND_BRANCH:
            return 0
        return self.balance - self.frozen_balance

    @property
    def expired_spendable(self) -> int:
        """Expired bond funds that are spendable right now.

        Frozen expired bonds are excluded: frozen means excluded from
        any transaction, so counting them would contradict the ``frozn``
        state shown on the row.
        """
        return self.expired_balance - self.expired_frozen_balance


@dataclass
class MixdepthSummaryRow:
    """Aggregated balance for one mixdepth (all branches incl. bonds)."""

    mixdepth: int
    total_balance: int
    spendable_balance: int


Row: TypeAlias = UtxoRow | AddressRow | HiddenRow | BranchSummaryRow | MixdepthSummaryRow
AddressRowLike: TypeAlias = UtxoRow | AddressRow
GroupableRow: TypeAlias = UtxoRow | AddressRow | HiddenRow | BranchSummaryRow

# Branch sort rank: external < internal < fidelity bonds.
_BRANCH_SORT_RANK = {EXTERNAL_BRANCH: 0, INTERNAL_BRANCH: 1, FIDELITY_BOND_BRANCH: 2}


# Display-level rename + shortening: the underlying ``AddressStatus``
# vocabulary keeps the historic long names (persisted in wallet metadata and
# used by the history/origin classification); the row model surfaces short
# display forms, e.g. ``non-cj-change`` as ``chg`` (the interactive UTXO
# selector uses its own ``reg-change`` display name).
_STATUS_TO_LABEL: dict[str, RowLabel] = {
    "new": "new",
    "deposit": "dep",
    "cj-out": "cj-o",
    "cj-change": "cj-c",
    "non-cj-change": "chg",
    "used-empty": "used",
    "flagged": "flag",
    "reserved": "rsvd",
}


def derive_label(addr: AddressInfo, *, now: int | None = None) -> tuple[RowLabel, bool]:
    """Translate an ``AddressInfo`` into the three-level row vocabulary.

    Returns ``(label, reused)``:

    - ``status == "reused"`` → ``(base_status, True)``. ``base_status`` is
      always set in that case (issue #564); defensively falls back to
      ``dep``/``chg`` by branch if it is missing or unusable.
    - bond addresses → ``fb-a`` until ``now`` strictly passes the locktime,
      ``fb-x`` once strictly past it (the lock state lives in the label; the
      locktime date is a separate row field).
    - every other status maps onto its label via ``_STATUS_TO_LABEL``.

    The CLI supplies chain median-time-past as ``now``; standalone display
    callers default to host time.
    """
    if addr.is_bond or addr.status == "bond":
        if now is None:
            now = int(time.time())
        locked = addr.locktime is not None and addr.locktime >= now
        return ("fb-a" if locked else "fb-x"), False
    if addr.status == "reused":
        base = addr.base_status
        if base is None or base not in _STATUS_TO_LABEL:
            base = "deposit" if addr.is_external else "non-cj-change"
        return _STATUS_TO_LABEL[base], True
    return _STATUS_TO_LABEL.get(addr.status, "flag"), False


def fb_date_str(locktime: int) -> str:
    """Format a fidelity bond locktime as YY-MM-DD (local time)."""
    return datetime.fromtimestamp(locktime).strftime("%y-%m-%d")


def build_branch_rows(
    addresses: list[AddressInfo],
    *,
    mixdepth: int,
    branch: int,
    pending_addresses: set[str] | None = None,
    show_empty: bool = False,
    new_address_limit: int = DEFAULT_NEW_ADDRESS_LIMIT,
    now: int | None = None,
) -> list[Row]:
    """Build the canonical rows for one branch of one mixdepth.

    Produces one :class:`UtxoRow` per UTXO (hard rule: one line per UTXO),
    one :class:`AddressRow` per visible address without funds, and a single
    :class:`HiddenRow` when the gap/show-empty logic hid empty addresses.
    Rows are emitted in input order, which equals the legacy display order
    (derivation index for external/internal, locktime for fidelity bonds).

    Visibility mirrors the legacy display exactly: ``reserved`` addresses
    are always shown; up to ``new_address_limit`` unused ``new`` addresses
    are shown; empty ``used-empty``/``flagged`` addresses are hidden.
    ``show_empty`` disables all hiding. The fidelity bond branch never
    hides anything (redeemed/unfunded bonds must stay visible).
    """
    pending = pending_addresses or set()
    rows: list[Row] = []
    hidden = 0
    empty_new_shown = 0
    hide_empties = not show_empty and branch != FIDELITY_BOND_BRANCH

    for addr in addresses:
        if hide_empties and addr.balance == 0:
            if addr.status == "reserved":
                # Set-aside/handed-out addresses are always shown (with
                # their label) so the user can see what they gave out.
                pass
            elif addr.status == "new" and empty_new_shown < new_address_limit:
                empty_new_shown += 1
            else:
                # used-empty/flagged and surplus new addresses are noise.
                hidden += 1
                continue

        label, reused = derive_label(addr, now=now)

        if addr.utxos:
            for seq, utxo in enumerate(addr.utxos):
                rows.append(
                    UtxoRow(
                        mixdepth=mixdepth,
                        branch=branch,
                        address_index=addr.index,
                        path=addr.path,
                        address=addr.address,
                        label=label,
                        reused=reused,
                        pending=addr.address in pending,
                        user_label=addr.label,
                        utxo=utxo,
                        locktime=addr.locktime,
                        original_seq=seq,
                    )
                )
        else:
            rows.append(
                AddressRow(
                    mixdepth=mixdepth,
                    branch=branch,
                    address_index=addr.index,
                    path=addr.path,
                    address=addr.address,
                    label=label,
                    reused=reused,
                    pending=addr.address in pending,
                    user_label=addr.label,
                    locktime=addr.locktime,
                )
            )

    if hidden:
        rows.append(HiddenRow(mixdepth=mixdepth, branch=branch, hidden_count=hidden))
    return rows


def filter_rows(rows: list[Row], predicate: Callable[[Row], bool] | None = None) -> list[Row]:
    """Pipeline filter stage.

    Identity when no predicate is given — the static CLI has no filters yet;
    the interactive explorer (and possible future CLI flags) will pass one.
    """
    if predicate is None:
        return list(rows)
    return [row for row in rows if predicate(row)]


def path_order_key(path: tuple[int, int, int] | None) -> tuple[int, int, int, int]:
    """Canonical ordering key for path-addressable items.

    ``path`` is ``(mixdepth, branch, derivation index)`` as returned by
    ``WalletService._find_address_path``; the key is ``(0, mixdepth, branch
    rank, index)`` with the branch rank taken from ``_BRANCH_SORT_RANK``
    (external < internal < fidelity bonds). ``None`` (path not resolvable)
    maps to ``(1, 0, 0, 0)`` so such items sort after every resolved path.

    This is the canonical *sorted* order; the fidelity bond build path
    deliberately orders by (locktime, index) instead — see the module
    docstring.
    """
    if path is None:
        return (1, 0, 0, 0)
    mixdepth, branch, index = path
    return (0, mixdepth, _BRANCH_SORT_RANK.get(branch, branch), index)


def base_order_key(row: AddressRowLike) -> tuple[int, int, int, int]:
    """Canonical ordering: mixdepth, branch (external < internal < fidelity
    bonds), derivation index, then the UTXO's original position within its
    address (cache order).

    Formalizes the order the legacy display produced for the
    external/internal branches. There is deliberately no txid/vout
    tiebreak: the intra-address cache order is the stable is-order.
    """
    _, mixdepth, branch_rank, index = path_order_key((row.mixdepth, row.branch, row.address_index))
    return (mixdepth, branch_rank, index, row.original_seq)


def sort_rows(
    rows: list[AddressRowLike],
    key: Callable[[AddressRowLike], Any] | None = None,
) -> list[AddressRowLike]:
    """Pipeline sort stage.

    Without a key, sorts into :func:`base_order_key` order. With a user key
    (explorer/future CLI flags), rows are first brought into base order so
    ties under the user key fall back to the stable base order; the key is
    expected to keep address groups together (renderer concern).
    """
    ordered = sorted(rows, key=base_order_key)
    if key is None:
        return ordered
    return sorted(ordered, key=key)


def group_rows(rows: list[GroupableRow]) -> dict[tuple[int, int], list[GroupableRow]]:
    """Pipeline grouping stage: bucket rows by ``(mixdepth, branch)``,
    preserving their order within each bucket."""
    groups: dict[tuple[int, int], list[GroupableRow]] = {}
    for row in rows:
        groups.setdefault((row.mixdepth, row.branch), []).append(row)
    return groups


def summarize_branch(
    rows: list[Row], *, mixdepth: int, branch: int, now: int | None = None
) -> BranchSummaryRow:
    """Aggregate the rows of a single branch into a :class:`BranchSummaryRow`.

    Expects the rows produced by one :func:`build_branch_rows` call.
    Hidden rows contribute nothing (hidden addresses are empty by
    construction). For the fidelity bond branch, funds are additionally
    split into locked (locktime in the future) and expired, and the
    frozen share of the expired funds is tracked separately so the
    spendable (explicit) figure can exclude it.
    """
    if now is None:
        now = int(time.time())
    balance = 0
    frozen = 0
    locked = 0
    expired = 0
    expired_frozen = 0
    for row in rows:
        if not isinstance(row, UtxoRow):
            continue
        balance += row.utxo.value
        if row.utxo.frozen:
            frozen += row.utxo.value
        if branch == FIDELITY_BOND_BRANCH and row.locktime is not None:
            if row.locktime >= now:
                locked += row.utxo.value
            else:
                expired += row.utxo.value
                if row.utxo.frozen:
                    expired_frozen += row.utxo.value
    return BranchSummaryRow(
        mixdepth=mixdepth,
        branch=branch,
        balance=balance,
        frozen_balance=frozen,
        locked_balance=locked,
        expired_balance=expired,
        expired_frozen_balance=expired_frozen,
    )


def summarize_mixdepth(branches: list[BranchSummaryRow], *, mixdepth: int) -> MixdepthSummaryRow:
    """Aggregate branch summaries into the mixdepth total.

    The total includes fidelity bond funds; the spendable figure excludes
    them (their branch summary reports 0 spendable).
    """
    return MixdepthSummaryRow(
        mixdepth=mixdepth,
        total_balance=sum(b.balance for b in branches),
        spendable_balance=sum(b.spendable for b in branches),
    )


__all__ = [
    "DEFAULT_NEW_ADDRESS_LIMIT",
    "EXTERNAL_BRANCH",
    "INTERNAL_BRANCH",
    "AddressRow",
    "AddressRowLike",
    "BranchSummaryRow",
    "GroupableRow",
    "HiddenRow",
    "MixdepthSummaryRow",
    "Row",
    "RowLabel",
    "UtxoRow",
    "base_order_key",
    "build_branch_rows",
    "derive_label",
    "fb_date_str",
    "filter_rows",
    "group_rows",
    "path_order_key",
    "sort_rows",
    "summarize_branch",
    "summarize_mixdepth",
]
