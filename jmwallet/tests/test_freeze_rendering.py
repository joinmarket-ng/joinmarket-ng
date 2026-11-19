"""Regression coverage for freeze manager rendering and resize behavior."""

from __future__ import annotations

import curses
from datetime import UTC, datetime
from typing import cast
from unittest.mock import MagicMock

import pytest
from _jmwallet_test_helpers import Frame, KeyEvent, StrictScreen

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
    # Deterministic maker status: the real check scans /proc and would flip
    # with whatever maker processes happen to run on the test machine.
    monkeypatch.setattr(freeze_mod, "maker_bot_status", lambda: "STOPPED")


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
    freeze_mod._run_freeze_tui(cast(curses.window, screen), display_items, wallet, "w.mnemonic")
    assert screen.pending_events == 0


def _run_named(
    screen: StrictScreen, display_items: list[UTXOInfo | None], wallet: MagicMock
) -> None:
    """Runner variant with a long wallet name (forces the 2-line status block)."""
    wallet.wallet_fingerprint = "abcd1234"  # realistic length: a MagicMock's str is huge
    freeze_mod._run_freeze_tui(
        cast(curses.window, screen),
        display_items,
        wallet,
        "a-very-long-wallet-name-for-height-budget-testing",
    )
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

    # 120 columns fit the table; only the height budget fails.
    assert "Terminal too short" in screen.frames[-1].text
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
    assert "Terminal too short" in screen.frames[1].text


def test_one_by_one_terminal_prompt_is_clipped_and_quittable(
    patched_freeze_curses: None,
) -> None:
    """Even a 1x1 terminal shows a clipped prompt and q exits cleanly."""
    wallet = MagicMock()
    screen = StrictScreen(1, 1, [KeyEvent(1, 1, ord("q"))])

    _run(screen, [_utxo("a")], wallet)

    assert "Terminal too small" not in screen.frames[-1].text  # no room for the text
    assert screen.frames[-1].lines[0].strip() != ""  # a clipped prompt character was drawn


def test_narrow_terminal_prompt_names_width(patched_freeze_curses: None) -> None:
    """Below the table minimum the prompt names width as the fix."""
    wallet = MagicMock()
    screen = StrictScreen(24, 40, [KeyEvent(24, 40, ord("q"))])

    _run(screen, [_utxo("a")], wallet)

    assert "Terminal too narrow" in screen.frames[-1].text
    assert "Terminal too short" not in screen.frames[-1].text


def test_short_terminal_prompt_names_height(patched_freeze_curses: None) -> None:
    """Enough columns but no vertical budget: the prompt names height."""
    wallet = MagicMock()
    screen = StrictScreen(3, 120, [KeyEvent(3, 120, ord("q"))])

    _run(screen, [_utxo("a")], wallet)

    assert "Terminal too short" in screen.frames[-1].text
    assert "Terminal too narrow" not in screen.frames[-1].text


def test_eighty_columns_long_wallet_name_renders_with_one_line_status(
    patched_freeze_curses: None,
) -> None:
    """80x22 with a long wallet name renders via the 1-line status fallback.

    Regression: the 2-line status block (forced by narrow widths) pushed the
    vertical budget below zero and the generic 'too small' prompt hid a
    height problem that the one-line block solves. At 80 columns with a
    13-row footer the 2-line block needs height 23, the 1-line form 22.
    """
    wallet = MagicMock()
    screen = StrictScreen(22, 80, [KeyEvent(22, 80, ord("q"))])

    _run_named(screen, [_utxo("a")], wallet)

    frame = screen.frames[-1]
    assert "UTXO Freeze Manager" in frame.text
    assert "Terminal too" not in frame.text
    status_line = _line_with(frame, "Active Wallet:")
    # One-line status form: the row is one line, so narrow widths clip even
    # labels -- anchor on the fingerprint value, not the label.
    assert "abcd1234" in status_line


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

    assert "[F]" in frozen_row and "frozn" in frozen_row
    assert "[!]" in locked_row and "lock" in locked_row and "fb-a" in locked_row
    assert "[!]" in expired_row and "spend" in expired_row and "fb-x" in expired_row
    assert "[ ]" in regular_row and "spend" in regular_row
    # Per-view state width: the freeze subset (frozn/lock/spend, max 5) leaves
    # no trailing padding behind the token, unlike the full catalog (8).
    assert "| frozn |" in frozen_row
    assert "| lock  |" in locked_row  # 4-char token padded to the 5-wide column
    assert "| spend |" in expired_row
    assert "| spend |" in regular_row


def test_state_column_width_follows_the_view_token_set() -> None:
    """Freeze reserves space for its frozn/spend/lock subset; selector for the catalog."""
    from jmwallet.utxo_tui import _STATE_TOKENS, _table_columns
    from jmwallet.wallet.rows import FREEZE_STATE_TOKENS

    utxos = [_utxo("a")]
    freeze_cols = dict(_table_columns(120, utxos, "F", state_tokens=FREEZE_STATE_TOKENS))
    assert freeze_cols["state"] == 5
    catalog_cols = dict(_table_columns(120, utxos, "Sel"))
    assert catalog_cols["state"] == max(len(t) for t in _STATE_TOKENS) == 6
    assert "pend" in _STATE_TOKENS and "pend" not in FREEZE_STATE_TOKENS


def test_frame_starts_at_column_zero_and_footer_separators_match_table_width(
    patched_freeze_curses: None,
) -> None:
    """The frame starts at column 0 and the footer box spans the table width."""
    wallet = MagicMock()
    screen = StrictScreen(24, 120, [KeyEvent(24, 120, ord("q"))])

    _run(screen, [_utxo("a")], wallet)

    frame = screen.frames[0]
    table_row = _line_with(frame, "[ ]")
    assert table_row.startswith("| [ ]")  # border at column 0, content at column 1
    separators = [line for line in frame.lines if line.startswith("|-")]
    # Seven framed separators: the header separator, the two status block
    # rules (top edge above the block, top edge of the header below it) plus
    # the five footer separators (Freeze Details, UTXO Status, Controls, and
    # the box's closing bottom edge; the closing rule doubles as the footer
    # box's top edge) -- each framed: no overhang past the table's right
    # border, no left gap.
    assert len(separators) == 7
    for separator in separators:
        assert separator.rstrip().endswith("|")
        assert separator.rstrip()[0] == "|"  # left border in the frame's column
        assert len(separator.rstrip()) == len(table_row.rstrip())


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


@pytest.mark.parametrize("width", [80, 160])
def test_footer_status_shows_out_point_label_and_bond_texts(
    patched_freeze_curses: None, width: int
) -> None:
    """The UTXO Status carries the full outpoint and the complete why-texts.

    The bond why-text (69 chars) must render COMPLETE at the narrow end:
    historically it was hard-clipped there, so the full text including the
    date is the regression property.
    """
    wallet = MagicMock()
    labeled = _utxo("a")
    labeled.label = "savings"
    locked_bond = _utxo("b", locktime=4_000_000_000)
    expired_bond = _utxo("c", locktime=1)
    screen = StrictScreen(
        30,
        width,
        [
            KeyEvent(30, width, ord("j")),
            KeyEvent(30, width, ord("j")),
            KeyEvent(30, width, ord("q")),
        ],
    )

    _run(screen, [labeled, locked_bond, expired_bond], wallet)

    first_frame = screen.frames[0]
    # "Outpoint:" gets its own wrapped line; the full outpoint follows on
    # the next wrapped line (forced break, no separator before the value).
    assert f"{labeled.txid}:0" in first_frame.text
    assert "Label: savings" in first_frame.text
    locked_frame = screen.frames[1]
    assert f"{locked_bond.txid}:0" in locked_frame.text
    # Full why-text including the date, nothing clipped off.
    assert (
        f"Active Fidelity Bond: Locked and not spendable until "
        f"{datetime.fromtimestamp(4_000_000_000, UTC).strftime('%Y-%m-%d')} UTC!"
        in locked_frame.text
    )
    expired_frame = screen.frames[2]
    assert "Expired Fidelity Bond" in expired_frame.text


@pytest.mark.parametrize("width", [80, 160])
def test_outpoint_value_moves_whole_below_the_label_at_narrow_width(
    patched_freeze_curses: None, width: int
) -> None:
    """The status outpoint value never breaks mid-value, also at 80 columns."""

    # Selector-parity: the shared wrap_status_text moves the complete value
    # below the label when the line is too wide; a lost wiring must fail
    # here (the freeze render tests otherwise run at 120+ columns only).
    wallet = MagicMock()
    coin = _utxo("a")
    coin.label = "savings"
    screen = StrictScreen(
        30,
        width,
        [KeyEvent(30, width, ord("q"))],
    )

    _run(screen, [coin], wallet)

    frame = screen.frames[0]
    _line_with(frame, "Outpoint:")
    assert "Label: savings" in frame.text
    # The full 64-hex outpoint sits UNBROKEN on a single line (label-only on
    # the Outpoint line at 80 cols, value+label one line at 160): a lost
    # wrap_status_text wiring hard-chunks mid-value instead.
    assert f"{coin.txid}:0" in frame.text
    value_line = _line_with(frame, f"{coin.txid}:0")
    # Whole on one line (frame padding may surround it), never hard-chunked.
    assert f"{coin.txid}:0" in value_line


@pytest.mark.parametrize("width", [80, 81, 82])
def test_no_status_padding_line_when_wallet_has_longer_outpoints(
    patched_freeze_curses: None, width: int
) -> None:
    """No wallet-maximum padding line between status and note/separator.

    A 2-digit-vout coin (outpoint one char wider) tips the wrap threshold
    one column earlier than the 1-digit cursor coin; the dropped
    max-reserve padding must not leave a visible empty line at width 81
    (user-reported resize flip 80/81/82, 2026-11-15).
    """
    wallet = MagicMock()
    cursor_coin = _utxo("a", value=100_000)  # txid 64 hex, vout 0
    wide_coin = _utxo("b", value=200_000)
    wide_coin.vout = 15  # outpoint 77 chars incl. label prefix
    screen = StrictScreen(30, width, [KeyEvent(30, width, ord("q"))])

    _run(screen, [cursor_coin, wide_coin], wallet)

    lines = screen.frames[0].lines
    start = next(i for i, line in enumerate(lines) if "UTXO Status" in line)
    status_block = []
    for line in lines[start + 1 :]:
        if line.startswith("|") and set(line) <= set("|-"):
            break
        status_block.append(line)
    # Exactly the cursor coin's payload -- every block line carries content
    # (a max-reserve pad row renders as a framed all-blank line).
    assert any(f"{cursor_coin.txid}:0" in line for line in status_block)
    for line in status_block:
        inner = line.strip("|").strip()
        assert inner, status_block


def test_address_reuse_marks_rows_and_note(
    patched_freeze_curses: None,
) -> None:
    """A reused address gets the flush RU! cell and a note row."""
    wallet = MagicMock()
    same = "bcrt1qreusedreused"
    screen = StrictScreen(24, 120, [KeyEvent(24, 120, ord("q"))])

    _run(screen, [_utxo("a", address=same), _utxo("b", address=same)], wallet)

    frame = screen.frames[0]
    assert frame.text.count("RU!") >= 3  # two row cells plus the note row
    reuse_rows = [line for line in frame.lines if same in line]
    assert len(reuse_rows) == 2
    for row in reuse_rows:
        # The marker sits flush against the table's right border; the frame
        # pads to the terminal width, so strip trailing blanks first.
        assert row.rstrip().endswith("|RU!|")
    assert "RU! Warning: Address reuse reduces privacy" in frame.text


def test_help_hint_in_controls_and_overlay_opens_and_closes(
    patched_freeze_curses: None,
) -> None:
    """``?`` opens the help overlay; the next key closes it back to the list."""
    wallet = MagicMock()
    # Height 40: the overlay holds the full tag + state legend plus the
    # view rules; at 24 rows the tail (rules, close hint) would be cut off.
    screen = StrictScreen(
        40,
        120,
        [
            KeyEvent(40, 120, ord("?")),
            KeyEvent(40, 120, ord("q")),  # closes the overlay only
            KeyEvent(40, 120, ord("q")),  # quits the TUI
        ],
    )

    _run(screen, [_utxo("a")], wallet)

    list_frame = screen.frames[0]
    assert "?: help" in list_frame.text  # hint lives in the Controls footer

    help_frame = screen.frames[1]
    # Help-screen title: TUI header style, second row, centered, dashes
    # included, no frame around it.
    assert help_frame.lines[1].strip() == "— UTXO Freeze Manager Help Screen —"
    assert help_frame.lines[1] != help_frame.lines[1].strip()
    # Shared legend content in the extended view's legend format. State
    # entries are the freeze subset only (no selection-context states).
    # Absence checks use the entry signature ({token:<7} - ...) so a
    # substring inside another word ("spending") cannot false-positive.
    assert "tags (type of address or funds):" in help_frame.text
    assert "states (spendability):" in help_frame.text
    assert "frozn" in help_frame.text and "lock" in help_frame.text
    assert "spend" in help_frame.text
    for absent_token in ("pend", "unconf", "in-use", "no-cj", "immat", "oth-md"):
        assert f"{absent_token:<7} - " not in help_frame.text, absent_token
    assert "fb-x" in help_frame.text
    assert "q/Esc: back to the list" in help_frame.text
    # Every section header sits under a full-width rule; the rule before
    # the tag legend is the box's top edge; the box closes after the hint.
    lines = help_frame.lines
    for header in ("tags (type of address or funds):", "states (spendability):"):
        idx = next(i for i, line in enumerate(lines) if header in line)
        assert lines[idx - 1].startswith("|-"), lines[idx - 1]
    close_idx = next(i for i, line in enumerate(lines) if "q/Esc: back to the list" in line)
    assert lines[close_idx + 1].startswith("|-"), lines[close_idx + 1]

    # Back to the list after closing, not exited: one more render happened.
    back_frame = screen.frames[2]
    assert "UTXO Freeze Manager" in back_frame.text
    assert "tags (type of address" not in back_frame.text


def test_reuse_column_appears_only_when_reuse_exists(
    patched_freeze_curses: None,
) -> None:
    """The RU! column exists if and only if the wallet has address reuse."""
    wallet = MagicMock()

    def run(items: list[UTXOInfo]) -> Frame:
        from jmwallet.utxo_tui import build_display_items

        screen = StrictScreen(24, 140, [KeyEvent(24, 140, ord("q"))])
        _run(screen, build_display_items(items, branch_groups=True), wallet)
        return screen.frames[0]

    # No reuse: no column, no warning note row.
    clean = run([_utxo("a"), _utxo("b")])
    assert "RU!" not in clean.text
    assert "Warning: Address reuse" not in clean.text

    # Reuse: flush header cell and row cells inside the border plus the note.
    reused = run(
        [
            _utxo("a", address="bcrt1qshared"),
            _utxo("b", address="bcrt1qshared"),
            _utxo("c"),
        ]
    )
    assert _line_with_frame(reused, "state").rstrip().endswith("|RU!|")
    assert "Warning: Address reuse" in reused.text
    unused = _line_with_frame(reused, "bcrt1qcccccccccccc")
    assert unused.rstrip().endswith("|   |")


def _line_with_frame(frame: Frame, text: str) -> str:
    return next(line for line in frame.lines if text in line)
