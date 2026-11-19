"""Regression coverage for freeze manager rendering and resize behavior."""

from __future__ import annotations

import curses
from typing import cast
from unittest.mock import MagicMock

import pytest
from _jmwallet_test_helpers import KeyEvent, StrictScreen

import jmwallet.cli.freeze as freeze_mod
from jmwallet.wallet.models import UTXOInfo


@pytest.fixture
def patched_freeze_curses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep rendering tests independent from a terminal's curses initialization."""

    def no_op(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr(curses, "curs_set", no_op)
    monkeypatch.setattr(curses, "use_default_colors", no_op)
    monkeypatch.setattr(curses, "init_pair", no_op)
    monkeypatch.setattr(curses, "color_pair", lambda _pair_number: 0)


def _utxo(
    txid_char: str,
    *,
    mixdepth: int = 0,
    value: int = 100_000,
    frozen: bool = False,
    locktime: int | None = None,
    address: str | None = None,
) -> UTXOInfo:
    return UTXOInfo(
        txid=txid_char * 64,
        vout=0,
        value=value,
        address=address if address is not None else f"bcrt1q{txid_char * 12}",
        confirmations=10,
        scriptpubkey="0014" + txid_char * 40,
        path=f"m/84'/0'/{mixdepth}'/0/{ord(txid_char) - ord('a'):02d}",
        mixdepth=mixdepth,
        frozen=frozen,
        locktime=locktime,
    )


def _run(screen: StrictScreen, display_items: list[UTXOInfo | None], wallet: MagicMock) -> None:
    freeze_mod._run_freeze_tui(cast(curses.window, screen), display_items, wallet)
    assert screen.pending_events == 0


def test_tiny_terminal_shows_resize_prompt_and_ignores_toggle_keys(
    patched_freeze_curses: None,
) -> None:
    """A terminal without room for a row must prompt instead of crashing or acting."""
    wallet = MagicMock()
    screen = StrictScreen(
        3,
        120,
        [
            KeyEvent(3, 120, ord(" ")),  # toggle attempt while too small: ignored
            KeyEvent(3, 120, ord("q")),
        ],
    )

    _run(screen, [_utxo("a")], wallet)

    assert "Terminal too small" in screen.frames[-1].text
    wallet.toggle_freeze_utxo.assert_not_called()


def test_shrink_below_minimum_then_restore_recovers_normal_rendering(
    patched_freeze_curses: None,
) -> None:
    """The freeze screen renders again once the terminal is large enough."""
    wallet = MagicMock()
    screen = StrictScreen(
        24,
        120,
        [
            KeyEvent(3, 120, curses.KEY_RESIZE),
            KeyEvent(24, 120, curses.KEY_RESIZE),
            KeyEvent(24, 120, ord("q")),
        ],
    )

    _run(screen, [_utxo("a")], wallet)

    normal_frames = [screen.frames[0], screen.frames[-1]]
    for frame in normal_frames:
        assert "UTXO Freeze Manager" in frame.text
        assert "aaaaaaaa" in frame.text
    assert "Terminal too small" in screen.frames[1].text


def test_one_by_one_terminal_prompt_is_clipped_and_quittable(
    patched_freeze_curses: None,
) -> None:
    """Even a 1x1 terminal shows a clipped prompt and q exits cleanly."""
    wallet = MagicMock()
    screen = StrictScreen(1, 1, [KeyEvent(1, 1, ord("q"))])

    _run(screen, [_utxo("a")], wallet)

    assert "Terminal too small" not in screen.frames[-1].text  # no room for the text
    assert screen.frames[-1].lines[0].strip() != ""  # a clipped prompt character was drawn


def _line_with(frame: object, text: str) -> str:
    """Return the rendered frame line containing ``text`` (anchor by content)."""
    for line in frame.lines:  # type: ignore[attr-defined]
        if text in line:
            return line
    raise AssertionError(f"no line containing {text!r}\n{frame.text}")  # type: ignore[attr-defined]


def test_state_column_and_marks_cover_all_utxo_kinds(
    patched_freeze_curses: None,
) -> None:
    """Frozen/locked/spendable states, [F]/[!]/[ ] marks and the FB tag column."""
    wallet = MagicMock()
    frozen_regular = _utxo("a", frozen=True)
    locked_bond = _utxo("b", locktime=4_000_000_000)  # far future
    expired_bond = _utxo("c", locktime=1)
    regular = _utxo("d")
    screen = StrictScreen(24, 120, [KeyEvent(24, 120, ord("q"))])

    _run(screen, [frozen_regular, locked_bond, expired_bond, regular], wallet)

    frame = screen.frames[0]
    frozen_row = _line_with(frame, "bcrt1qaaaa")
    locked_row = _line_with(frame, "bcrt1qbbbb")
    expired_row = _line_with(frame, "bcrt1qcccc")
    regular_row = _line_with(frame, "bcrt1qdddd")

    assert "[F]" in frozen_row and "frozen" in frozen_row
    assert "[!]" in locked_row and "locked" in locked_row and "FB-active" in locked_row
    assert "[!]" in expired_row and "spendable" in expired_row and "FB-expired" in expired_row
    assert "[ ]" in regular_row and "spendable" in regular_row


def test_toggle_failure_shows_error_line_and_keeps_running(
    patched_freeze_curses: None,
) -> None:
    """A persisting failure lands in the footer's note row, without a crash."""
    wallet = MagicMock()
    wallet.toggle_freeze_utxo.side_effect = OSError("disk full")
    screen = StrictScreen(
        24,
        120,
        [KeyEvent(24, 120, ord(" ")), KeyEvent(24, 120, ord("q"))],
    )

    _run(screen, [_utxo("a")], wallet)

    assert "ERROR: Failed to persist freeze state" in screen.frames[1].text


def test_toggle_works_on_a_still_locked_bond(
    patched_freeze_curses: None,
) -> None:
    """Locked bonds toggle like any UTXO: the freeze flag survives the timelock."""
    wallet = MagicMock()
    bond = _utxo("b", locktime=4_000_000_000)
    screen = StrictScreen(
        24,
        120,
        [KeyEvent(24, 120, ord(" ")), KeyEvent(24, 120, ord("q"))],
    )

    _run(screen, [bond], wallet)

    wallet.toggle_freeze_utxo.assert_called_once_with(bond.outpoint)


def test_freeze_all_and_unfreeze_all_include_all_utxo_kinds(
    patched_freeze_curses: None,
) -> None:
    """Bulk actions cover regular UTXOs and bonds alike; state decides the skip."""
    wallet = MagicMock()
    frozen_regular = _utxo("a", frozen=True)
    locked_bond = _utxo("b", locktime=4_000_000_000)
    expired_bond = _utxo("c", locktime=1)
    regular = _utxo("d")

    # Freeze all: every unfrozen item, regardless of bond state.
    screen = StrictScreen(
        24,
        120,
        [KeyEvent(24, 120, ord("a")), KeyEvent(24, 120, ord("q"))],
    )
    _run(screen, [frozen_regular, locked_bond, expired_bond, regular], wallet)
    assert {call.args[0] for call in wallet.toggle_freeze_utxo.call_args_list} == {
        frozen_regular.outpoint,
        locked_bond.outpoint,
        expired_bond.outpoint,
        regular.outpoint,
    } - {frozen_regular.outpoint}

    # Unfreeze all: only the frozen ones.
    wallet.reset_mock()
    screen = StrictScreen(
        24,
        120,
        [KeyEvent(24, 120, ord("n")), KeyEvent(24, 120, ord("q"))],
    )
    _run(screen, [frozen_regular, locked_bond, expired_bond, regular], wallet)
    assert [call.args[0] for call in wallet.toggle_freeze_utxo.call_args_list] == [
        frozen_regular.outpoint
    ]


def test_footer_status_shows_out_point_label_and_bond_texts(
    patched_freeze_curses: None,
) -> None:
    """The UTXO Status line carries the full outpoint and the why-texts."""
    wallet = MagicMock()
    labeled = _utxo("a")
    labeled.label = "savings"
    locked_bond = _utxo("b", locktime=4_000_000_000)
    expired_bond = _utxo("c", locktime=1)
    # 160 columns so the bond's why-text is not clipped off the status line.
    screen = StrictScreen(
        24,
        160,
        [
            KeyEvent(24, 160, ord("j")),
            KeyEvent(24, 160, ord("j")),
            KeyEvent(24, 160, ord("q")),
        ],
    )

    _run(screen, [labeled, locked_bond, expired_bond], wallet)

    first_frame = screen.frames[0]
    assert f"Out: {labeled.txid}:0" in first_frame.text
    assert "Label: savings" in first_frame.text
    locked_frame = screen.frames[1]
    assert f"Out: {locked_bond.txid}:0" in locked_frame.text
    assert "Active Fidelity Bond: Locked and not spendable until" in locked_frame.text
    expired_frame = screen.frames[2]
    assert "Expired Fidelity Bond" in expired_frame.text


def test_address_reuse_marks_rows_and_note(
    patched_freeze_curses: None,
) -> None:
    """A reused address gets the RU! badge behind the frame and a note."""
    wallet = MagicMock()
    same = "bcrt1qreusedreused"
    screen = StrictScreen(24, 120, [KeyEvent(24, 120, ord("q"))])

    _run(screen, [_utxo("a", address=same), _utxo("b", address=same)], wallet)

    frame = screen.frames[0]
    assert frame.text.count("RU!") >= 3  # two row badges plus the note row
    reuse_rows = [line for line in frame.lines if same in line]
    assert len(reuse_rows) == 2
    for row in reuse_rows:
        assert row.rstrip().endswith("RU!")
    assert "RU! Warning: Address reuse reduces privacy" in frame.text
