"""Tests for the shared UTXO TUI helpers (jmwallet.utxo_tui).

These helpers back both the freeze manager and the interactive UTXO
selector: group separators (mixdepths, or mixdepth/branch groups),
predicate-based cursor navigation, the address column formatting, and
scroll adjustment.
"""

from __future__ import annotations

import curses
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from jmwallet import utxo_tui as utxo_tui_mod
from jmwallet.utxo_tui import (
    ADDRESS_COL_WIDTH,
    _display_width,
    _help_overlay_rows,
    _wrap_footer_fields,
    adjust_scroll,
    align_control_columns,
    branch_display_rank,
    build_display_items,
    chunk_to_display_width,
    draw_resize_prompt,
    draw_status_block,
    enrich_utxo_labels,
    format_address_column,
    group_label,
    group_separator_parts,
    seek_selectable,
    status_header_lines,
    utxo_branch,
    utxo_display_sort_key,
    utxo_sort_key,
    wrap_status_text,
)
from jmwallet.wallet import rows as rows_mod
from jmwallet.wallet.models import UTXOInfo


def _utxo(
    mixdepth: int,
    value: int = 100_000,
    txid_char: str = "a",
    vout: int = 0,
    index: int = 0,
    branch: int = 0,
    path: str | None = None,
    label: str | None = None,
    locktime: int | None = None,
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
        label=label,
        locktime=locktime,
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
    def test_address_always_shown(self) -> None:
        # No dedup: consecutive rows with the same address must each show it.
        assert format_address_column("bcrt1qsame") == "bcrt1qsame"

    def test_long_address_middle_truncated(self) -> None:
        long_addr = "bcrt1q" + "x" * 60
        out = format_address_column(long_addr)
        assert len(out) == ADDRESS_COL_WIDTH
        assert "..." in out
        assert out.startswith(long_addr[:20])
        assert out.endswith(long_addr[-19:])


class TestAdjustScroll:
    def test_cursor_above_view(self) -> None:
        assert adjust_scroll(2, 5, 10, 30) == 2

    def test_cursor_below_view(self) -> None:
        assert adjust_scroll(20, 0, 10, 30) == 11

    def test_cursor_in_view_unchanged(self) -> None:
        assert adjust_scroll(5, 3, 10, 30) == 3

    def test_offset_drops_when_whole_list_fits(self) -> None:
        # Window enlarged beyond the item count: everything is visible.
        assert adjust_scroll(5, 3, 10, 10) == 0

    def test_offset_drops_after_window_enlarged(self) -> None:
        # A bottom-of-list offset from a shrunken window must be given up
        # once the window is tall enough to show more rows again.
        assert adjust_scroll(15, 14, 15, 16) == 1

    def test_offset_clamped_to_last_full_page(self) -> None:
        assert adjust_scroll(8, 9, 5, 12) == 7


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


class TestGroupSeparatorParts:
    """Label parts a group separator is rendered from.

    ``full_title`` distinguishes a separator opening a new mixdepth (render
    the whole title) from one that only switches the branch within a mixdepth
    (renderers strike the title with dashes and keep only the branch name).
    """

    def test_leading_separator_introduces_first_group_with_full_title(self) -> None:
        items = build_display_items([_utxo(0)], branch_groups=True)
        # Layout: [None (blank row), None (labelled separator), utxo]
        assert group_separator_parts(items, 1) == (
            "Mixdepth 0",
            " — ",
            "external addresses",
            True,
        )

    def test_branch_change_within_mixdepth_uses_short_form(self) -> None:
        utxos = [_utxo(0, branch=0), _utxo(0, branch=1, txid_char="b")]
        items = build_display_items(utxos, branch_groups=True)
        # Layout: [None, None, external, None (separator), internal]
        assert group_separator_parts(items, 3) == (
            "Mixdepth 0",
            " — ",
            "internal addresses",
            False,
        )

    def test_new_mixdepth_uses_full_title(self) -> None:
        items = build_display_items([_utxo(0), _utxo(1, txid_char="b")], branch_groups=True)
        # Layout: [None, None, u0, None (blank), None (separator), u1]
        assert group_separator_parts(items, 4) == (
            "Mixdepth 1",
            " — ",
            "external addresses",
            True,
        )

    def test_trailing_separator_has_no_parts(self) -> None:
        # Not constructed by build_display_items today; renders as plain dashes.
        items = build_display_items([_utxo(0)], branch_groups=True) + [None]
        assert group_separator_parts(items, len(items) - 1) is None


class TestEnrichUtxoLabels:
    """The tag column only shows something if a feeder runs this enrichment.

    Both feeders (send.py for the selector, freeze.py for the freeze
    manager) call this on their raw utxo_cache items before rendering.
    """

    def test_unlabeled_regular_utxo_gets_wallet_label(self) -> None:
        unlabeled = _utxo(0)
        enrich_utxo_labels(lambda _addr: "cj-out", [unlabeled])
        assert unlabeled.label == "cj-out"

    def test_existing_label_is_preserved(self) -> None:
        labeled = _utxo(0, label="personal note")
        enrich_utxo_labels(lambda _addr: "cj-out", [labeled])
        assert labeled.label == "personal note"

    def test_fidelity_bond_is_never_looked_up(self) -> None:
        # A bond's tag column already renders fb-a/fb-x; its
        # label slot must stay untouched so nothing overwrites a user note.
        bond = _utxo(0, locktime=1_900_000_000)

        def fail_lookup(_addr: str) -> str:
            raise AssertionError("label lookup must not run for bonds")

        enrich_utxo_labels(fail_lookup, [bond])
        assert bond.label is None

    def test_unlabeled_and_no_wallet_label_stays_empty(self) -> None:
        unlabeled = _utxo(0)
        enrich_utxo_labels(lambda _addr: "", [unlabeled])
        assert unlabeled.label == ""


class TestWrapStatusText:
    """The outpoint value moves whole below the label, never breaks mid-value."""

    def test_value_moves_whole_below_the_label(self) -> None:
        # 64-hex txid + vout: too wide for a 75-column box together with the
        # label, but fits on its own line.
        txid = "a" * 64
        lines = wrap_status_text(f"Outpoint: {txid}:0", 75)
        assert lines == ["Outpoint:", f"{txid}:0"]

    def test_value_stays_on_the_label_line_when_it_fits(self) -> None:
        txid = "a" * 64
        lines = wrap_status_text(f"Outpoint: {txid}:0", 76)
        assert lines == [f"Outpoint: {txid}:0"]

    def test_overlong_value_is_hard_chunked_only_as_last_resort(self) -> None:
        txid = "a" * 140
        lines = wrap_status_text(f"Outpoint: {txid}:0", 75)
        assert lines[0] == "Outpoint:"
        assert all(len(line) <= 75 for line in lines[1:])
        assert "".join(lines[1:]) == f"{txid}:0"

    def test_explanation_wraps_below_without_separator_at_breaks(self) -> None:
        txid = "a" * 64
        text = f"Outpoint: {txid}:0\nfrozn | tag: keep"
        lines = wrap_status_text(text, 75)
        assert lines[0] == "Outpoint:"
        assert lines[1] == f"{txid}:0"
        assert lines[2] == "frozn | tag: keep"

    def test_payload_without_outpoint_prefix_wraps_normally(self) -> None:
        lines = wrap_status_text("a | b", 75)
        assert lines == ["a | b"]


class TestFrameTopEdge:
    """The box's top edge: a rule between the title and the header row."""

    def test_top_edge_rule_sits_between_title_and_header(self) -> None:
        from unittest.mock import MagicMock, patch

        from jmwallet.cli.freeze import _render_freeze_screen

        utxos = [
            UTXOInfo(
                txid=f"{i + 1:064x}",
                vout=0,
                value=100_000,
                address=f"bcrt1qedge{i}",
                confirmations=10,
                scriptpubkey="0014" + "11" * 20,
                path=f"m/84'/1'/0'/0/{i}",
                mixdepth=0,
            )
            for i in range(3)
        ]
        screen = MagicMock()
        screen.getmaxyx.return_value = (24, 160)
        with (
            patch("curses.color_pair", return_value=0),
            patch("jmwallet.cli.freeze.maker_bot_status", return_value="STOPPED"),
        ):
            _render_freeze_screen(screen, utxos, 0, 0, None, False, MagicMock(), "w.mnemonic")
        written = {(c.args[0], c.args[1]): c.args[2] for c in screen.addstr.call_args_list}
        # Top edge spans columns 1..sep_width-1 on row 2, borders on both
        # ends; bottom edge sits one row above the screen's bottom, and the
        # screen's last row stays empty (black frame).
        assert written[(2, 0)] == "|"
        assert written[(2, 1)] == "-" * 158
        assert written[(2, 159)] == "|"
        # The closing edge is drawn as one pre-framed line.
        assert written[(22, 0)] == "|" + "-" * 158 + "|"
        # The screen's last row stays empty: the black frame below the box.
        assert all(key[0] != 23 for key in written)


class TestHelpOverlayCatalogGuards:
    """The help overlay explains every emittable value and nothing else.

    Invariant: for each TUI, the overlay's state section covers exactly
    the states that view can emit -- a missing explanation leaves the user
    without help for a visible token; an extra one explains a state the
    view can never show. Same contract for tags (both TUIs share the
    ``short_label`` emission set).
    """

    SEP = 40

    @pytest.fixture(autouse=True)
    def _patch_curses_color_pair(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # _help_overlay_rows resolves color_pair at row-build time; the
        # overlay tests below run without a curses terminal.
        monkeypatch.setattr(curses, "color_pair", lambda _pair_number: 0)

    def _state_entries(self, state_tokens: tuple[str, ...]) -> set[str]:
        """Collect state tokens from overlay rows via their entry structure.

        Every entry is formatted ``f"{token:<7} - detail...``: the token
        is padded to 7 and the separator " - " follows, so "- " sits
        exactly at columns 9-10 of the raw text row. Wrapped continuation
        lines of long explanations do not carry that structure and are
        ignored.
        """
        rows = _help_overlay_rows(self.SEP, state_tokens)
        start = next(i for i, (line, _) in enumerate(rows) if "states (spendability):" in line)
        entries = set()
        for line, _attr in rows[start + 1 :]:
            if line.startswith("|-"):
                break
            if line[8:10] == "- ":
                token = line[:8].strip()
                assert token in state_tokens, token
                entries.add(token)
        return entries

    def test_state_help_covers_exactly_the_catalog(self) -> None:
        """Every catalog token has a STATE_HELP entry; no orphan entries."""
        assert set(rows_mod.STATE_HELP) == set(rows_mod._STATE_TOKENS)

    def test_tag_help_covers_every_short_label_value(self) -> None:
        """TAG_HELP explains every value ``short_label`` can ever produce.

        Emission set: every ``_STATUS_TO_LABEL`` value plus the fidelity
        bond pair plus the BIP-329 pass-through form.
        """
        emittable = set(rows_mod._STATUS_TO_LABEL.values()) | {"fb-a", "fb-x"}
        for token in emittable:
            assert token in rows_mod.TAG_HELP, token
        # The BIP-329 pass-through renders as "<label>".
        assert "<label>" in rows_mod.TAG_HELP

    def test_freeze_overlay_states_exactly_the_freeze_subset(self) -> None:
        """Freeze help lists exactly frozn/spend/lock, in catalog order.

        ``FREEZE_STATE_TOKENS`` is derived from the catalog by filtering,
        so the overlay's order is catalog order; the tuple pin makes an
        accidental catalog reorder loud instead of a silent visual change.
        """
        assert rows_mod.FREEZE_STATE_TOKENS == ("frozn", "spend", "lock")
        assert self._state_entries(rows_mod.FREEZE_STATE_TOKENS) == {"frozn", "lock", "spend"}

    def test_selector_overlay_states_exactly_catalog_minus_pend(self) -> None:
        """Selector help lists every catalog state except ``pend``."""
        assert set(rows_mod.SELECTOR_STATE_TOKENS) == set(rows_mod._STATE_TOKENS) - {"pend"}
        assert self._state_entries(rows_mod.SELECTOR_STATE_TOKENS) == set(
            rows_mod.SELECTOR_STATE_TOKENS
        )

    def test_missing_state_help_entry_crashes_loudly(self) -> None:
        """A state token without a STATE_HELP entry raises, never skips."""
        with pytest.raises(KeyError):
            _help_overlay_rows(self.SEP, ("no-such-state",))


class TestStatusHeaderLines:
    """The status block cascade: stages, alignment, clipping, borders."""

    NAME = "w.mnemonic"
    FP = "abcd1234"

    def _lines(self, sep_width: int) -> list[str]:
        return status_header_lines(sep_width, self.NAME, self.FP, "STOPPED")

    def test_wide_terminal_one_line(self) -> None:
        """Everything fits on one line at a wide terminal."""
        lines = self._lines(120)
        assert lines == ["Active Wallet: w.mnemonic | Fingerprint:   abcd1234 | Maker Bot: STOPPED"]

    def test_narrow_terminal_two_lines_maker_with_name(self) -> None:
        """Stage 2: name + maker status above, fingerprint aligned below."""
        lines = self._lines(66)
        assert lines == [
            "Active Wallet: w.mnemonic | Maker Bot: STOPPED",
            "Fingerprint:   abcd1234",
        ]

    def test_very_narrow_terminal_name_alone_first(self) -> None:
        """Stage 3: name alone, fingerprint and maker status share the second."""
        # Geometry: the stage-3 second line (44 cols with a 7-char fingerprint)
        # fits exactly at sep 46 while stage 2 (45 cols) does not -- the only
        # window where stage 3 renders unclipped.
        lines = status_header_lines(46, self.NAME, "abcd123", "STOPPED")
        assert lines == [
            "Active Wallet: w.mnemonic",
            "Fingerprint:   abcd123 | Maker Bot: STOPPED",
        ]

    def test_extreme_width_clips_values_never_overflows(self) -> None:
        """Below stage 3 thresholds every line is clipped to the body width."""
        lines = self._lines(30)
        assert all(_display_width(line) <= 28 for line in lines)
        # The name line still fits whole; the second line clips its tail.
        assert lines[0] == "Active Wallet: w.mnemonic"
        assert lines[1].endswith("...")

    def test_values_align_at_column_21_in_every_stage(self) -> None:
        """Both labels are 15 columns, so the value starts at index 15."""
        for sep_width in (30, 40, 50, 66, 80, 127, 200):
            for line in self._lines(sep_width):
                if line.startswith("Active Wallet: "):
                    assert line[len("Active Wallet: ") :].startswith(self.NAME)
                elif line.startswith("Fingerprint:   ") and "Maker" not in line:
                    assert line[len("Fingerprint:   ") :].startswith(self.FP)

    def test_long_wallet_name_pushes_cascade(self) -> None:
        """A long name falls through to the later cascade stages, not overflow."""
        lines = status_header_lines(80, "a-very-long-wallet-name.mnemonic", self.FP, "STOPPED")
        assert len(lines) == 2
        assert all(_display_width(line) <= 78 for line in lines)

    def test_max_lines_one_forces_single_line_form(self) -> None:
        """max_lines=1 returns the one-line form with all three fields, clipped."""
        lines = status_header_lines(
            110, "a-very-long-wallet-name.mnemonic", self.FP, "STOPPED", max_lines=1
        )
        assert len(lines) == 1
        line = lines[0]
        # Everything fits at this width: all three fields share the row.
        assert line.startswith("Active Wallet: a-very-long-wallet-name.mnemonic")
        assert "Fingerprint:   " in line
        assert "Maker Bot: STOPPED" in line
        assert _display_width(line) <= 108

    def test_max_lines_one_clips_tail_when_narrow(self) -> None:
        """Narrow one-line form keeps all fields and clips the long name value."""
        lines = status_header_lines(
            80, "a-very-long-wallet-name.mnemonic", self.FP, "STOPPED", max_lines=1
        )
        assert len(lines) == 1
        line = lines[0]
        # Name value clipped to fit, but fingerprint and maker status survive.
        name_part = line.split(" | Fingerprint:")[0]
        assert name_part.startswith("Active Wallet: ")
        assert name_part.endswith("...")
        assert "Fingerprint:   abcd1234" in line
        assert "Maker Bot: STOPPED" in line
        assert _display_width(line) <= 78

    def test_max_lines_default_two_keeps_cascade(self) -> None:
        """Omitting max_lines keeps the pre-existing cascade behavior."""
        lines = status_header_lines(80, "a-very-long-wallet-name.mnemonic", self.FP, "STOPPED")
        assert len(lines) == 2

    def test_draw_resize_prompt_reason_texts(self) -> None:
        """The prompt names the dimension it wants resized; generic stays fallback."""

        class _PromptScreen:
            """Minimal double recording the prompt text at (0, 0)."""

            def __init__(self, width: int) -> None:
                self._width = width
                self.text = ""

            def getmaxyx(self) -> tuple[int, int]:
                return 24, self._width

            def addstr(self, _y: int, _x: int, text: str, _attr: int = 0) -> None:
                self.text = text

            def refresh(self) -> None:
                pass

        screen = _PromptScreen(60)
        draw_resize_prompt(cast(curses.window, screen), 60, "width")
        assert screen.text.startswith("Terminal too narrow. Please resize.")

        screen = _PromptScreen(60)
        draw_resize_prompt(cast(curses.window, screen), 60, "height")
        assert screen.text.startswith("Terminal too short. Please resize.")

        screen = _PromptScreen(60)
        draw_resize_prompt(cast(curses.window, screen), 60)
        assert screen.text.startswith("Terminal too small. Please resize.")

    def test_display_width_counts_columns_not_characters(self) -> None:
        """CJK-wide text measures 2 columns per char; combining marks 0."""
        assert _display_width("ab") == 2
        assert _display_width("中文") == 4
        assert _display_width("é") == 1  # combining acute: 1 + 0

    @staticmethod
    def _fake_proc(monkeypatch: pytest.MonkeyPatch, argv: bytes) -> None:
        """Pin the /proc scan to a single fake process with the given argv."""
        import pathlib

        class FakeCmdline:
            @staticmethod
            def read_bytes() -> bytes:
                return argv

        class FakeEntry:
            name = "4711"

            def isdigit(self) -> bool:
                return True

            def __truediv__(self, other: str) -> FakeCmdline:
                assert other == "cmdline"
                return FakeCmdline()

            @property
            def cmdline(self) -> FakeCmdline:
                return FakeCmdline()

        class FakeProcDir:
            def iterdir(self):  # type: ignore[no-untyped-def]
                yield FakeEntry()

        monkeypatch.setattr(pathlib, "Path", lambda _p: FakeProcDir())

    def test_maker_status_running_on_jm_maker_argv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A process whose argv contains ``jm-maker`` reads as RUNNING."""
        self._fake_proc(
            monkeypatch, b"/usr/bin/python3\x00/home/u/.local/bin/jm-maker\x00serve\x00"
        )
        assert utxo_tui_mod.maker_bot_status() == "RUNNING"

    def test_maker_status_stopped_without_jm_maker(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No matching argv anywhere reads as STOPPED (never matches itself)."""
        self._fake_proc(monkeypatch, b"/usr/bin/python3\x00/usr/local/bin/jm-taker\x00")
        assert utxo_tui_mod.maker_bot_status() == "STOPPED"


class TestDrawStatusBlock:
    """The framed status block: borders, padding, value column."""

    def test_block_lines_are_framed_left_and_right(self) -> None:
        screen = MagicMock()
        screen.getmaxyx.return_value = (10, 80)
        with patch("curses.color_pair", return_value=0):
            draw_status_block(screen, 3, ["Active Wallet: w | Maker Bot: STOPPED"], 78)
        written = {(c.args[0], c.args[1]): c.args[2] for c in screen.addstr.call_args_list}
        assert written[(3, 0)] == "|"
        assert written[(3, 1)] == " Active Wallet: w | Maker Bot: STOPPED"
        # Plain padding + right border flush at sep_width (37 text columns);
        # the padding write begins after the text and ends with the border.
        padding_write = written[(3, 39)]
        assert padding_write.endswith("|")
        assert padding_write == " " * 39 + "|"

    def test_block_returns_row_count(self) -> None:
        screen = MagicMock()
        screen.getmaxyx.return_value = (10, 80)
        with patch("curses.color_pair", return_value=0):
            drawn = draw_status_block(screen, 3, ["a", "b"], 78)
        assert drawn == 2
        assert {c.args[0] for c in screen.addstr.call_args_list} == {3, 4}


class TestFooterWidthMeasurement:
    """Footer wrapping measures terminal columns, never characters (rule 15b).

    A user BIP-329 label may contain CJK-wide characters that render 2
    columns each; character-count wrapping would emit lines wider than the
    footer box (overflowing the frame or triggering a bogus resize prompt).
    Every line below is asserted with _display_width, mirroring the caller's
    column budget.
    """

    def test_wrap_lines_never_exceed_the_column_budget_with_wide_chars(self) -> None:
        # 6 CJK chars = 12 columns + ASCII text: character count (9) says it
        # fits in a 10-column box, the real width (17) does not.
        wide_label = "漢字漢字漢字"
        lines = _wrap_footer_fields(f"State: spend | tag: {wide_label}", 20)
        assert all(_display_width(line) <= 20 for line in lines)

    def test_wide_field_hard_chunks_without_splitting_a_wide_char(self) -> None:
        wide = "漢" * 12  # 24 columns
        chunks = chunk_to_display_width(wide, 10)
        assert all(_display_width(chunk) <= 10 for chunk in chunks)
        assert "".join(chunks) == wide

    def test_chunk_and_reassembly_survive_combining_marks(self) -> None:
        text = "á́b́ć" * 4  # combining marks render 0 columns
        chunks = chunk_to_display_width(text, 7)
        assert all(_display_width(chunk) <= 7 for chunk in chunks)
        assert "".join(chunks) == text

    def test_wrap_status_explanation_with_wide_label_stays_in_the_box(self) -> None:
        txid = "a" * 64
        wide_label = "漢字漢字漢字"
        text = f"Outpoint: {txid}:0\nState: spend | tag: {wide_label}"
        lines = wrap_status_text(text, 75)
        assert lines[0] == "Outpoint:"
        assert lines[1] == f"{txid}:0"
        assert all(_display_width(line) <= 80 for line in lines[2:])

    def test_align_control_columns_with_wide_field_pads_in_columns(self) -> None:
        # Same-length-in-chars fields, one wide: a len()-grid would align the
        # separators wrongly and overflow the budget.
        text = "漢漢: toggle | abcd: exit"
        lines = align_control_columns(text, 14)
        assert all(_display_width(line) <= 14 for line in lines)


class TestAlignControlColumns:
    """The controls hint renders as an aligned key table when it must wrap."""

    def test_fits_on_one_line_stays_unwrapped(self) -> None:
        """No grid needed when the whole hint fits: unchanged single line."""
        text = "Space/Tab: toggle | j/k: navigate | q/Esc: exit"
        assert align_control_columns(text, 200) == [text]

    def test_selector_hint_at_footer_width_aligns_separators(self) -> None:
        """At the 80-column footer width the selectors hint wraps aligned."""
        hint = (
            "Space/Tab: toggle | j/k: navigate | s: select mixdepth | "
            "d: deselect all | Enter: confirm | ?: help | q/Esc: cancel"
        )
        lines = align_control_columns(hint, 75)
        assert len(lines) == 2
        # Separators line up vertically: row 2 pads each cell to the grid.
        assert lines[0].startswith("Space/Tab: toggle | j/k: navigate | ")
        assert lines[1].startswith("Enter: confirm    | ?: help       | ")
        # Every row keeps the same separator column positions.
        row1 = [i for i in range(len(lines[0])) if lines[0][i : i + 3] == " | "]
        row2 = [i for i in range(len(lines[1])) if lines[1][i : i + 3] == " | "]
        assert row1[:2] == row2[:2]
        assert all(len(line) <= 75 for line in lines)

    def test_freeze_hint_at_footer_width_aligns_separators(self) -> None:
        """The freeze hint wraps into an aligned 4+2 grid."""
        hint = (
            "Space/Tab: toggle | j/k: navigate | a: freeze all | "
            "n: unfreeze all | ?: help | q/Esc: exit"
        )
        lines = align_control_columns(hint, 75)
        assert lines == [
            "Space/Tab: toggle | j/k: navigate | a: freeze all | n: unfreeze all",
            "?: help           | q/Esc: exit",
        ]

    def test_very_narrow_width_falls_back_to_one_column_per_row(self) -> None:
        """Below the widest single field the grid degrades to one per row."""
        hint = "a: freeze all | n: unfreeze all | q/Esc: exit"
        lines = align_control_columns(hint, 15)
        assert lines == ["a: freeze all", "n: unfreeze all", "q/Esc: exit"]

    def test_oversized_field_hard_chunks_like_plain_wrap(self) -> None:
        """A field wider than the box falls back to the plain hard-chunk wrap."""
        huge = "x" * 40
        text = f"{huge} | q/Esc: exit"
        lines = align_control_columns(text, 20)
        assert all(len(line) <= 20 for line in lines)
        assert any("q/Esc: exit" in line for line in lines)
