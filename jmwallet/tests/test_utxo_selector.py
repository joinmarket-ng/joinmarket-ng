"""
Tests for the interactive UTXO selector TUI.

Tests cover:
- format_utxo_line() output formatting
- select_utxos_interactive() behavior in non-TTY environments
- Single UTXO auto-selection
- Frozen/locked UTXO unselectability
- Mixdepth pinning (allowed_mixdepth) and min-confirmation gating
"""

from __future__ import annotations

import sys

import pytest

import jmwallet.utxo_selector as utxo_selector
import jmwallet.utxo_tui as utxo_tui
from jmwallet.utxo_selector import (
    _is_base_selectable,
    format_utxo_line,
    select_utxos_interactive,
)
from jmwallet.wallet.models import UTXOInfo


@pytest.fixture
def sample_utxos() -> list[UTXOInfo]:
    """Create sample UTXOs for testing."""
    return [
        UTXOInfo(
            txid="a" * 64,
            vout=0,
            value=100_000,
            address="bcrt1test1",
            confirmations=10,
            scriptpubkey="0014" + "aa" * 20,
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        ),
        UTXOInfo(
            txid="b" * 64,
            vout=1,
            value=50_000,
            address="bcrt1test2",
            confirmations=5,
            scriptpubkey="0014" + "bb" * 20,
            path="m/84'/0'/0'/0/1",
            mixdepth=0,
        ),
        UTXOInfo(
            txid="c" * 64,
            vout=0,
            value=1_000_000,
            address="bcrt1test3",
            confirmations=100,
            scriptpubkey="0014" + "cc" * 20,
            path="m/84'/0'/0'/0/2",
            mixdepth=1,
        ),
        # Timelocked fidelity bond UTXO
        UTXOInfo(
            txid="d" * 64,
            vout=0,
            value=500_000,
            address="bcrt1bond",
            confirmations=1000,
            scriptpubkey="0020" + "dd" * 32,  # P2WSH
            path="m/84'/0'/0'/2/0",
            mixdepth=0,
            locktime=1893456000,
        ),
    ]


@pytest.fixture
def frozen_utxo() -> UTXOInfo:
    """Create a frozen UTXO for testing."""
    utxo = UTXOInfo(
        txid="e" * 64,
        vout=0,
        value=75_000,
        address="bcrt1qfrzen",
        confirmations=50,
        scriptpubkey="0014" + "ee" * 20,
        path="m/84'/0'/0'/0/3",
        mixdepth=0,
        frozen=True,
    )
    return utxo


class TestFormatUtxoLine:
    """Tests for format_utxo_line()."""

    def test_format_basic_utxo(self, sample_utxos: list[UTXOInfo]) -> None:
        """Test formatting a basic UTXO."""
        utxo = sample_utxos[0]
        line = format_utxo_line(utxo)

        assert "m0" in line  # Mixdepth 0
        assert "100,000" in line or "0.00100000" in line  # Amount
        assert f"{utxo.confirmations:>8,}" in line  # Confirmations column
        assert "aaaaaaaa" in line  # Truncated txid
        assert ":0" in line  # Vout

    def test_format_timelocked_utxo(self, sample_utxos: list[UTXOInfo]) -> None:
        """Test formatting a timelocked (fidelity bond) UTXO."""
        utxo = sample_utxos[3]  # The timelocked one
        line = format_utxo_line(utxo)

        assert "FB-active" in line  # Label column for a still-locked bond
        assert "locked" in line  # State column

    def test_format_with_max_width(self, sample_utxos: list[UTXOInfo]) -> None:
        """Test that lines are truncated to max width."""
        utxo = sample_utxos[0]
        line = format_utxo_line(utxo, max_width=40)

        assert len(line) <= 40
        assert line.endswith("...")

    def test_format_large_value(self, sample_utxos: list[UTXOInfo]) -> None:
        """Test formatting UTXO with larger value."""
        utxo = sample_utxos[2]  # 1,000,000 sats
        line = format_utxo_line(utxo)

        assert "m1" in line  # Mixdepth 1
        # Should show the BTC amount
        assert "0.01" in line or "1,000,000" in line

    def test_format_with_label(self, sample_utxos: list[UTXOInfo]) -> None:
        """Test formatting UTXO with a label."""
        utxo = sample_utxos[0]
        utxo.label = "cj-out"
        line = format_utxo_line(utxo)

        assert "cj-out" in line

    def test_format_in_use_indicator(self, sample_utxos: list[UTXOInfo]) -> None:
        """Inputs committed to another CoinJoin are visibly marked."""
        utxo = sample_utxos[0]

        line = format_utxo_line(utxo, excluded_outpoints={(utxo.txid, utxo.vout)})

        assert "in-use" in line  # State column

    def test_format_without_label(self, sample_utxos: list[UTXOInfo]) -> None:
        """Test formatting UTXO without a label."""
        utxo = sample_utxos[0]
        utxo.label = None
        line = format_utxo_line(utxo)

        # Should not contain label parentheses at the end
        # The line should not end with a label like "(cj-out)"
        assert not line.endswith(")")


class TestSelectUtxosInteractive:
    """Tests for select_utxos_interactive()."""

    def test_empty_utxos_returns_empty(self) -> None:
        """Test that empty input returns empty list."""
        result = select_utxos_interactive([])
        assert result == []

    def test_single_utxo_returns_directly(self, sample_utxos: list[UTXOInfo]) -> None:
        """Test that single UTXO is returned without TUI."""
        single_utxo = [sample_utxos[0]]
        result = select_utxos_interactive(single_utxo)
        assert result == single_utxo

    def test_non_tty_raises_error(
        self, sample_utxos: list[UTXOInfo], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test that non-TTY environment raises RuntimeError."""
        import sys
        from io import StringIO

        # Simulate non-TTY stdin
        fake_stdin = StringIO()
        monkeypatch.setattr(sys, "stdin", fake_stdin)

        with pytest.raises(RuntimeError, match="terminal"):
            select_utxos_interactive(sample_utxos)


class TestUtxoSorting:
    """Tests for display ordering through the selector's public entrypoint."""

    def test_entrypoint_orders_branch_groups_and_inserts_labelled_separators(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The public entrypoint passes group-ordered, separated items to curses.

        Within a mixdepth the extended view's order applies: fidelity bonds
        (branch 2), then external (0), then internal (1). Each group — the
        first included — is introduced by a labelled separator; a new
        mixdepth additionally gets a leading blank row (two ``None``).
        """

        class TtyStream:
            def isatty(self) -> bool:
                return True

        first = _make_utxo(0, value=10_000)
        first.txid = "a" * 64
        first.path = "m/84'/0'/0'/0/1"
        second = _make_utxo(1, value=20_000)
        second.txid = "b" * 64
        second.path = "m/84'/0'/1'/0/0"
        third = _make_utxo(1, value=90_000)
        third.txid = "c" * 64
        third.path = "m/84'/0'/1'/1/0"
        fourth = _make_utxo(2, value=50_000)
        fourth.txid = "d" * 64
        fourth.path = "m/84'/0'/2'/0/0"

        captured: dict[str, list[UTXOInfo | None]] = {}

        def fake_wrapper(
            _runner: object,
            display_items: list[UTXOInfo | None],
            *_args: object,
            **_kwargs: object,
        ) -> list[UTXOInfo]:
            captured["display_items"] = display_items
            return []

        monkeypatch.setattr(sys, "stdin", TtyStream())
        monkeypatch.setattr(sys, "stdout", TtyStream())
        monkeypatch.setattr(utxo_selector.curses, "wrapper", fake_wrapper)

        assert select_utxos_interactive([fourth, third, first, second]) == []

        display_items = captured["display_items"]
        assert [item.path for item in display_items if item is not None] == [
            "m/84'/0'/0'/0/1",
            "m/84'/0'/1'/0/0",
            "m/84'/0'/1'/1/0",
            "m/84'/0'/2'/0/0",
        ]
        assert [index for index, item in enumerate(display_items) if item is None] == [
            0,  # blank row introducing mixdepth 0
            1,  # separator: m0 external
            3,  # blank row introducing mixdepth 1
            4,  # separator: m1 external
            6,  # separator: m1 internal (same mixdepth, no blank row)
            8,  # blank row introducing mixdepth 2
            9,  # separator: m2 external
        ]


class TestNeighborItems:
    """The separator renderer labels a separator from its neighbouring UTXOs."""

    def test_neighbours_skip_blank_rows_and_separators(self) -> None:
        first = _make_utxo(0)
        second = _make_utxo(1)
        second.txid = "b" * 64
        items = utxo_selector._prepare_display_items([second, first])
        # Layout: [None, None, first, None, None, second]
        prev_item, next_item = utxo_tui._neighbor_items(items, 0)
        assert prev_item is None
        assert next_item is first
        prev_item, next_item = utxo_tui._neighbor_items(items, 4)
        assert prev_item is first
        assert next_item is second

    def test_trailing_separator_has_no_following_item(self) -> None:
        # Not constructed by build_display_items today; renders as plain dashes.
        items = utxo_selector._prepare_display_items([_make_utxo(0)]) + [None]
        prev_item, next_item = utxo_tui._neighbor_items(items, len(items) - 1)
        assert prev_item is not None
        assert next_item is None


class TestFrozenUtxoFormatting:
    """Tests for frozen UTXO display formatting."""

    def test_frozen_indicator_shown(self, frozen_utxo: UTXOInfo) -> None:
        """Frozen UTXOs show ``frozen`` in their State column."""
        line = format_utxo_line(frozen_utxo)
        assert "frozen" in line

    def test_frozen_indicator_after_label(self, frozen_utxo: UTXOInfo) -> None:
        """The frozen State column comes after the Label column."""
        frozen_utxo.label = "deposit"
        line = format_utxo_line(frozen_utxo, max_width=120)

        frozen_pos = line.index("frozen")
        label_pos = line.index("deposit")
        assert frozen_pos > label_pos, (
            f"frozen state at {frozen_pos} should come after deposit at {label_pos}: {line}"
        )

    def test_frozen_indicator_after_fb_indicator(self) -> None:
        """A frozen, still-locked bond shows FB-active and a frozen State."""
        utxo = UTXOInfo(
            txid="f" * 64,
            vout=0,
            value=200_000,
            address="bcrt1qfrzenfb",
            confirmations=500,
            scriptpubkey="0020" + "ff" * 32,
            path="m/84'/0'/0'/2/0",
            mixdepth=0,
            locktime=1893456000,
            frozen=True,
        )
        line = format_utxo_line(utxo)
        # Label column shows FB-active; the State column shows frozen, and as
        # the last column it always appears after the label.
        assert "FB-active" in line
        assert "frozen" in line
        assert line.index("frozen") > line.index("FB-active")

    def test_unfrozen_no_indicator(self, sample_utxos: list[UTXOInfo]) -> None:
        """Non-frozen UTXOs show ``spendable`` in their State column."""
        line = format_utxo_line(sample_utxos[0])
        assert "spendable" in line
        assert "frozen" not in line


class TestFrozenUtxoUnselectability:
    """Tests for frozen/locked UTXO unselectability in interactive mode."""

    def test_single_frozen_utxo_not_auto_selected(self) -> None:
        """A single frozen UTXO is not auto-selected in non-TTY mode."""
        frozen = UTXOInfo(
            txid="f" * 64,
            vout=0,
            value=100_000,
            address="bcrt1frozen",
            confirmations=10,
            scriptpubkey="0014" + "ff" * 20,
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
            frozen=True,
        )
        result = select_utxos_interactive([frozen])
        assert result == []

    def test_single_locked_fb_utxo_not_auto_selected(self) -> None:
        """A single locked fidelity bond UTXO is not auto-selected in non-TTY mode."""
        locked_fb = UTXOInfo(
            txid="f" * 64,
            vout=0,
            value=100_000,
            address="bcrt1locked",
            confirmations=10,
            scriptpubkey="0020" + "ff" * 32,
            path="m/84'/0'/0'/2/0",
            mixdepth=0,
            locktime=1893456000,  # Far future
        )
        result = select_utxos_interactive([locked_fb])
        assert result == []

    def test_single_selectable_utxo_auto_selected(self) -> None:
        """A single non-frozen, non-locked UTXO is auto-selected in non-TTY mode."""
        normal = UTXOInfo(
            txid="f" * 64,
            vout=0,
            value=100_000,
            address="bcrt1normal",
            confirmations=10,
            scriptpubkey="0014" + "ff" * 20,
            path="m/84'/0'/0'/0/0",
            mixdepth=0,
        )
        result = select_utxos_interactive([normal])
        assert result == [normal]

    def test_single_in_use_utxo_not_auto_selected(self) -> None:
        """In-flight inputs remain visible but cannot be selected in non-TTY mode."""
        in_use = _make_utxo(0)

        result = select_utxos_interactive([in_use], excluded_outpoints={(in_use.txid, in_use.vout)})

        assert result == []


def _make_utxo(
    mixdepth: int,
    *,
    value: int = 100_000,
    confirmations: int = 10,
    frozen: bool = False,
    locktime: int | None = None,
) -> UTXOInfo:
    return UTXOInfo(
        txid="f" * 64,
        vout=0,
        value=value,
        address=f"bcrt1qmd{mixdepth}",
        confirmations=confirmations,
        scriptpubkey="0014" + "ff" * 20,
        path=f"m/84'/0'/{mixdepth}'/0/0",
        mixdepth=mixdepth,
        frozen=frozen,
        locktime=locktime,
    )


class TestBaseSelectability:
    """Tests for the session-wide selectability rules."""

    def test_normal_utxo_selectable(self) -> None:
        assert _is_base_selectable(_make_utxo(0), None, 0) is True

    def test_frozen_never_selectable(self) -> None:
        assert _is_base_selectable(_make_utxo(0, frozen=True), None, 0) is False

    def test_locked_fb_never_selectable(self) -> None:
        locked = _make_utxo(0, locktime=4_000_000_000)  # far future
        locked.scriptpubkey = "0020" + "ff" * 32
        assert _is_base_selectable(locked, None, 0) is False

    def test_expired_fb_selectable(self) -> None:
        expired = _make_utxo(0, locktime=1)  # 1970, expired
        expired.scriptpubkey = "0020" + "ff" * 32
        assert _is_base_selectable(expired, None, 0) is True

    def test_immature_not_selectable(self) -> None:
        assert _is_base_selectable(_make_utxo(0, confirmations=2), None, 5) is False

    def test_in_use_not_selectable(self) -> None:
        utxo = _make_utxo(0)
        assert _is_base_selectable(utxo, None, 0, {(utxo.txid, utxo.vout)}) is False

    def test_other_mixdepth_not_selectable_when_pinned(self) -> None:
        assert _is_base_selectable(_make_utxo(1), 0, 0) is False
        assert _is_base_selectable(_make_utxo(1), 1, 0) is True


def _expired_bond(mixdepth: int = 0) -> UTXOInfo:
    bond = _make_utxo(mixdepth, locktime=1)  # 1970, expired
    bond.scriptpubkey = "0020" + "ff" * 32
    return bond


class TestCoinJoinContextBonds:
    """``allow_fidelity_bonds=False`` (CoinJoin callers) excludes every bond.

    Direct sends keep the default (``True``) so expired bonds stay redeemable.
    """

    def test_expired_bond_not_selectable(self) -> None:
        assert _is_base_selectable(_expired_bond(), None, 0, allow_fidelity_bonds=False) is False

    def test_expired_bond_state_is_no_cj(self) -> None:
        assert utxo_selector._utxo_state_col(_expired_bond(), allow_fidelity_bonds=False) == "no-cj"

    def test_locked_bond_state_stays_locked(self) -> None:
        locked = _make_utxo(0, locktime=4_000_000_000)
        assert utxo_selector._utxo_state_col(locked, allow_fidelity_bonds=False) == "locked"

    def test_frozen_bond_state_stays_frozen(self) -> None:
        frozen = _expired_bond()
        frozen.frozen = True
        assert utxo_selector._utxo_state_col(frozen, allow_fidelity_bonds=False) == "frozen"

    def test_regular_utxo_unaffected(self) -> None:
        assert _is_base_selectable(_make_utxo(0), None, 0, allow_fidelity_bonds=False) is True
        assert utxo_selector._utxo_state_col(_make_utxo(0), allow_fidelity_bonds=False) == (
            "spendable"
        )

    def test_default_keeps_expired_bond_selectable(self) -> None:
        # Send/redeem path contract: unchanged.
        assert _is_base_selectable(_expired_bond(), None, 0) is True
        assert utxo_selector._utxo_state_col(_expired_bond()) == "spendable"

    def test_row_state_column_shows_no_cj(self) -> None:
        line = format_utxo_line(_expired_bond(), allow_fidelity_bonds=False)
        assert "no-cj" in line
        assert "FB-expired" in line

    def test_footer_explains_no_cj(self) -> None:
        bond = _expired_bond()
        _, _, status, _ = utxo_selector._build_footer_lines(
            [bond],
            set(),
            set(),
            lambda _i: False,
            0,
            None,
            None,
            0,
            set(),
            {},
            0,
            allow_fidelity_bonds=False,
        )
        assert status.startswith("State: no-cj (fidelity bonds cannot be used in CoinJoins")
        assert "jm-wallet send --select-utxos" in status
        assert "Expired Fidelity Bond" in status

    def test_single_expired_bond_not_auto_selected_non_tty(self) -> None:
        """Non-TTY single-UTXO shortcut must not hand a bond to a CoinJoin."""
        assert select_utxos_interactive([_expired_bond()], allow_fidelity_bonds=False) == []
        # Send path: still auto-selected.
        bond = _expired_bond()
        assert select_utxos_interactive([bond]) == [bond]


class TestMixdepthPinningNonTTY:
    """Non-TTY fallback honors allowed_mixdepth and min_confirmations."""

    def test_single_utxo_outside_allowed_mixdepth_not_selected(self) -> None:
        utxo = _make_utxo(2)
        assert select_utxos_interactive([utxo], allowed_mixdepth=0) == []

    def test_single_utxo_in_allowed_mixdepth_selected(self) -> None:
        utxo = _make_utxo(2)
        assert select_utxos_interactive([utxo], allowed_mixdepth=2) == [utxo]

    def test_single_immature_utxo_not_selected(self) -> None:
        utxo = _make_utxo(0, confirmations=2)
        assert select_utxos_interactive([utxo], min_confirmations=5) == []
