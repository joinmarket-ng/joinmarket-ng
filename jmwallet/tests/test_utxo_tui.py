"""Tests for the shared UTXO TUI helpers (jmwallet.utxo_tui).

These helpers back both the freeze manager and the interactive UTXO
selector: group separators (mixdepths, or mixdepth/branch groups),
predicate-based cursor navigation, the address column formatting, and
scroll adjustment.
"""

from __future__ import annotations

from jmwallet.utxo_tui import (
    ADDRESS_COL_WIDTH,
    adjust_scroll,
    branch_display_rank,
    build_display_items,
    format_address_column,
    group_label,
    seek_selectable,
    utxo_branch,
    utxo_display_sort_key,
    utxo_sort_key,
)
from jmwallet.wallet.models import UTXOInfo


def _utxo(
    mixdepth: int,
    value: int = 100_000,
    txid_char: str = "a",
    vout: int = 0,
    index: int = 0,
    branch: int = 0,
    path: str | None = None,
) -> UTXOInfo:
    return UTXOInfo(
        txid=txid_char * 64,
        vout=vout,
        value=value,
        address=f"bcrt1qmd{mixdepth}",
        confirmations=10,
        scriptpubkey="0014" + "aa" * 20,
        path=path if path is not None else f"m/84'/0'/{mixdepth}'/{branch}/{index}",
        mixdepth=mixdepth,
    )


class TestBuildDisplayItems:
    def test_empty(self) -> None:
        assert build_display_items([]) == []

    def test_single_mixdepth_has_no_separator(self) -> None:
        items = build_display_items([_utxo(0), _utxo(0, txid_char="b")])
        assert None not in items
        assert len(items) == 2

    def test_separator_between_mixdepths(self) -> None:
        items = build_display_items([_utxo(0), _utxo(1), _utxo(2)])
        # u, None, u, None, u
        assert len(items) == 5
        assert items[1] is None
        assert items[3] is None

    def test_no_leading_separator(self) -> None:
        items = build_display_items([_utxo(3)])
        assert items[0] is not None


class TestSeekSelectable:
    def test_skips_separators(self) -> None:
        items = build_display_items([_utxo(0), _utxo(1)])
        # Layout: [u0, None, u1]
        assert seek_selectable(items, 1, 1, lambda _u: True) == 2
        assert seek_selectable(items, 1, -1, lambda _u: True) == 0

    def test_respects_predicate(self) -> None:
        u0, u1 = _utxo(0), _utxo(1, value=42)
        items = build_display_items([u0, u1])
        # Only the second UTXO is "selectable" for this predicate.
        assert seek_selectable(items, 0, 1, lambda u: u.value == 42) == 2

    def test_falls_back_to_opposite_direction(self) -> None:
        items = build_display_items([_utxo(0), _utxo(1)])
        last = len(items) - 1
        # Seeking forward past the end falls back to searching backwards.
        assert seek_selectable(items, last + 1, 1, lambda _u: True) == last

    def test_nothing_matches_returns_start(self) -> None:
        items = build_display_items([_utxo(0)])
        assert seek_selectable(items, 0, 1, lambda _u: False) == 0


class TestFormatAddressColumn:
    def test_repeated_address_blanked(self) -> None:
        out = format_address_column("bcrt1qsame", "bcrt1qsame")
        assert out == " " * ADDRESS_COL_WIDTH

    def test_new_address_shown(self) -> None:
        assert format_address_column("bcrt1qnew", "bcrt1qold") == "bcrt1qnew"

    def test_long_address_middle_truncated(self) -> None:
        long_addr = "bcrt1q" + "x" * 60
        out = format_address_column(long_addr, "")
        assert len(out) == ADDRESS_COL_WIDTH
        assert "..." in out
        assert out.startswith(long_addr[:20])
        assert out.endswith(long_addr[-19:])


class TestAdjustScroll:
    def test_cursor_above_view(self) -> None:
        assert adjust_scroll(2, 5, 10) == 2

    def test_cursor_below_view(self) -> None:
        assert adjust_scroll(20, 0, 10) == 11

    def test_cursor_in_view_unchanged(self) -> None:
        assert adjust_scroll(5, 3, 10) == 3


class TestUtxoSortKey:
    """Regression: derivation paths must compare numerically (indices >= 10).

    ``utxo_sort_key`` is the shared sort key used by both the freeze manager
    and the interactive UTXO selector; a plain string comparison would place
    ``.../0/10`` before ``.../0/2``.
    """

    def test_numeric_order_for_indices_ge_10(self) -> None:
        utxos = [_utxo(0, index=i) for i in (1, 10, 2, 11, 0, 12, 3)]
        ordered = sorted(utxos, key=utxo_sort_key)
        assert [u.path.rsplit("/", 1)[-1] for u in ordered] == [
            "0",
            "1",
            "2",
            "3",
            "10",
            "11",
            "12",
        ]

    def test_fidelity_bond_timenumber_orders_numerically(self) -> None:
        # Fidelity-bond paths use branch 2 and a large timenumber as index.
        utxos = [_utxo(0, branch=2, index=i) for i in (100, 9, 1_234_567)]
        ordered = sorted(utxos, key=utxo_sort_key)
        assert [u.path.rsplit("/", 1)[-1] for u in ordered] == ["9", "100", "1234567"]

    def test_locktime_suffix_does_not_break_sort(self) -> None:
        # Some code paths append ``:locktime`` to the bond path; the key must
        # not crash and must keep bare and suffixed forms adjacent.
        utxos = [
            _utxo(0, branch=2, index=9),
            _utxo(0, branch=2, index=9, path="m/84'/0'/0'/2/9:1893456000"),
            _utxo(0, branch=2, index=12),
            _utxo(0, branch=2, index=100, path="m/84'/0'/0'/2/100:1893456000"),
        ]
        ordered = sorted(utxos, key=utxo_sort_key)
        assert [utxo_sort_key(u) for u in ordered] == [
            (84, 0, 0, 2, 9),
            (84, 0, 0, 2, 9, 1893456000),
            (84, 0, 0, 2, 12),
            (84, 0, 0, 2, 100, 1893456000),
        ]


class TestUtxoBranch:
    def test_external_internal_fidelity_branches(self) -> None:
        assert utxo_branch(_utxo(0, branch=0)) == 0
        assert utxo_branch(_utxo(0, branch=1)) == 1
        assert utxo_branch(_utxo(0, branch=2)) == 2

    def test_locktime_suffix_ignored(self) -> None:
        u = _utxo(0, branch=2, index=9, path="m/84'/0'/0'/2/9:1893456000")
        assert utxo_branch(u) == 2

    def test_path_without_branch_groups_as_minus_one(self) -> None:
        # Malformed paths must not crash the spend flow; they group together.
        u = _utxo(0, path="m/84'/0'")
        assert utxo_branch(u) == -1


class TestBranchDisplayRank:
    def test_fidelity_bonds_first_then_external_then_internal(self) -> None:
        assert branch_display_rank(2) < branch_display_rank(0) < branch_display_rank(1)

    def test_unknown_branch_sorts_last(self) -> None:
        assert branch_display_rank(99) > branch_display_rank(1)


class TestUtxoDisplaySortKey:
    def test_branch_groups_follow_extended_view_order(self) -> None:
        utxos = [
            _utxo(0, branch=0, index=0),
            _utxo(0, branch=1, index=0, txid_char="b"),
            _utxo(0, branch=2, index=9, txid_char="c"),
            _utxo(0, branch=0, index=1, txid_char="d"),
        ]
        ordered = sorted(utxos, key=utxo_display_sort_key)
        assert [utxo_branch(u) for u in ordered] == [2, 0, 0, 1]

    def test_mixdepths_stay_contiguous(self) -> None:
        utxos = [_utxo(1, branch=2), _utxo(0, branch=0, txid_char="b")]
        ordered = sorted(utxos, key=utxo_display_sort_key)
        assert [u.mixdepth for u in ordered] == [0, 1]

    def test_within_group_numeric_index_order_preserved(self) -> None:
        utxos = [_utxo(0, branch=0, index=i) for i in (10, 2, 1)]
        ordered = sorted(utxos, key=utxo_display_sort_key)
        assert [u.path.rsplit("/", 1)[-1] for u in ordered] == ["1", "2", "10"]


class TestGroupLabel:
    def test_labels(self) -> None:
        assert group_label(_utxo(0, branch=0)) == "Mixdepth 0 — external addresses"
        assert group_label(_utxo(1, branch=1)) == "Mixdepth 1 — internal addresses"
        assert group_label(_utxo(0, branch=2)) == "Mixdepth 0 — fidelity bond addresses"

    def test_unknown_branch_labelled_by_number(self) -> None:
        assert group_label(_utxo(0, branch=7)) == "Mixdepth 0 — branch 7"


class TestBuildDisplayItemsBranchGroups:
    def test_empty_list(self) -> None:
        assert build_display_items([], branch_groups=True) == []

    def test_leading_blank_row_and_labelled_separator(self) -> None:
        # Even a single group is introduced — blank border-only row, then the
        # labelled separator — so every separator can carry its group's title.
        items = build_display_items([_utxo(0)], branch_groups=True)
        assert [item is None for item in items] == [True, True, False]

    def test_boundaries_at_mixdepth_and_branch(self) -> None:
        utxos = [
            _utxo(0, branch=0, index=0),
            _utxo(0, branch=0, index=1, txid_char="b"),
            _utxo(0, branch=1, index=0, txid_char="c"),
            _utxo(1, branch=0, index=0, txid_char="d"),
        ]
        items = build_display_items(utxos, branch_groups=True)
        # A new mixdepth is introduced by a blank row plus the labelled
        # separator (two consecutive None); a branch change within the same
        # mixdepth only adds the labelled separator.
        assert [item is None for item in items] == [
            True,  # blank row introducing mixdepth 0
            True,  # separator: m0 external
            False,
            False,
            True,  # separator: m0 internal
            False,
            True,  # blank row introducing mixdepth 1
            True,  # separator: m1 external
            False,
        ]

    def test_default_mode_unchanged_by_branch_changes(self) -> None:
        # The freeze manager's default path must stay byte-identical: branch
        # changes within a mixdepth do not open a new group.
        utxos = [
            _utxo(0, branch=0, index=0),
            _utxo(0, branch=1, index=0, txid_char="b"),
        ]
        items = build_display_items(utxos)
        assert None not in items
