"""Regression coverage for UTXO selector rendering and resize behavior."""

from __future__ import annotations

import curses
import os
import sys
import time

import pytest
from _jmwallet_test_helpers import Frame, KeyEvent, StrictScreen

import jmwallet.utxo_selector as utxo_selector
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.utxo_metadata import AUTO_FREEZE_REUSE_LABEL


@pytest.fixture
def patched_selector_curses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep rendering tests independent from a terminal's curses initialization."""

    def no_op(*_args: object, **_kwargs: object) -> None:
        pass

    def color_pair(_pair_number: int) -> int:
        return 0

    monkeypatch.setattr(utxo_selector.curses, "curs_set", no_op)
    monkeypatch.setattr(utxo_selector.curses, "use_default_colors", no_op)
    monkeypatch.setattr(utxo_selector.curses, "init_pair", no_op)
    monkeypatch.setattr(utxo_selector.curses, "color_pair", color_pair)
    # Deterministic maker status: the real check scans /proc and would flip
    # with whatever maker processes happen to run on the test machine.
    monkeypatch.setattr(utxo_selector, "maker_bot_status", lambda: "STOPPED")


def _utxo(
    txid_char: str,
    *,
    mixdepth: int = 0,
    value: int = 100_000,
    confirmations: int = 10,
    address: str | None = None,
    label: str | None = None,
    frozen: bool = False,
    locktime: int | None = None,
    branch: int = 0,
) -> UTXOInfo:
    return UTXOInfo(
        txid=txid_char * 64,
        vout=0,
        value=value,
        address=address or f"bcrt1q{txid_char * 12}",
        confirmations=confirmations,
        scriptpubkey="0014" + txid_char * 40,
        path=f"m/84'/0'/{mixdepth}'/{branch}/{ord(txid_char) - ord('a'):02d}",
        mixdepth=mixdepth,
        label=label,
        frozen=frozen,
        locktime=locktime,
    )


def _event(height: int, width: int, key: int) -> KeyEvent:
    return KeyEvent(height, width, key)


def _run(
    screen: StrictScreen,
    utxos: list[UTXOInfo],
    *,
    target_amount: int = 0,
    allowed_mixdepth: int | None = None,
    min_confirmations: int = 0,
    excluded_outpoints: set[tuple[str, int]] | None = None,
    allow_fidelity_bonds: bool = True,
) -> list[UTXOInfo]:
    # Same composition as select_utxos_interactive: extended-view group order
    # plus labelled branch-group separators.
    display_items = utxo_selector._prepare_display_items(utxos)
    result = utxo_selector._run_selector(
        screen,
        display_items,
        target_amount,
        allowed_mixdepth,
        min_confirmations,
        excluded_outpoints or set(),
        allow_fidelity_bonds,
    )
    assert screen.pending_events == 0
    return result


def _line_with(frame: Frame, text: str) -> str:
    return next(line for line in frame.lines if text in line)


def _frame_with(frames: list[Frame], text: str) -> Frame:
    return next(frame for frame in frames if text in frame.text)


@pytest.mark.parametrize(
    ("width", "is_full_table"),
    [(80, True), (120, True), (160, True), (200, True)],
)
def test_widths_keep_monetary_summary_and_table_essentials(
    patched_selector_curses: None, width: int, is_full_table: bool
) -> None:
    """Adaptive widths keep spend-critical columns and full sats values at any size."""
    height = 32
    coin = _utxo("a")
    screen = StrictScreen(
        height,
        width,
        [_event(height, width, ord(" ")), _event(height, width, ord("q"))],
    )

    assert _run(screen, [coin], target_amount=50_000) == []

    selected_frame = screen.frames[-1]
    header = _line_with(selected_frame, "sats")
    assert "Target: 50,000 sats" in selected_frame.text
    assert "Total: 100,000 sats" in selected_frame.text
    assert "100,000" in selected_frame.text
    if is_full_table:
        assert "address" in header
        assert "confs" in header
    else:
        assert "address" not in header
        assert "confs" not in header


@pytest.mark.parametrize(("width", "full_footer_label"), [(80, False), (200, True)])
def test_frozen_labels_are_clipped_in_rows_but_preserved_in_footer(
    patched_selector_curses: None, width: int, full_footer_label: bool
) -> None:
    """Frozen labels and RU! survive compact rendering without writing past the edge."""
    height = 32
    custom_label = "custom-label-" + "x" * 48
    shared_address = "bcrt1qsharedaddress"
    auto_frozen = _utxo(
        "a",
        address=shared_address,
        label=AUTO_FREEZE_REUSE_LABEL,
        frozen=True,
    )
    custom_frozen = _utxo(
        "b",
        address=shared_address,
        label=custom_label,
        frozen=True,
    )
    screen = StrictScreen(
        height,
        width,
        [_event(height, width, ord("j")), _event(height, width, ord("q"))],
    )

    assert _run(screen, [auto_frozen, custom_frozen]) == []

    auto_frame = screen.frames[0]
    custom_frame = screen.frames[-1]
    # Rows are anchored by the tag column (the outpoint moved to the footer).
    auto_row = _line_with(auto_frame, "jm:autof")
    custom_row = _line_with(custom_frame, "custom-l")
    assert "frozn" in auto_frame.text
    assert "RU!" in auto_frame.text
    assert "jm:autof" in auto_row
    assert "frozn" in custom_row
    assert "RU!" in custom_row
    assert custom_label not in custom_row
    assert "..." in custom_row
    assert "Outpoint:" in custom_frame.text
    if full_footer_label:
        assert f"Label: {AUTO_FREEZE_REUSE_LABEL}" in auto_frame.text
        assert f"Label: {custom_label}" in custom_frame.text
    else:
        # At 80 columns the outpoint wraps onto its own line instead of
        # being clipped; the full Label still fits on a later wrapped line.
        assert custom_label in custom_frame.text


@pytest.mark.parametrize("width", [80, 107, 120, 160, 200])
def test_autofrozen_label_alone_keeps_row_state_and_reuse_warning(
    patched_selector_curses: None, width: int
) -> None:
    coin = _utxo("a", label=AUTO_FREEZE_REUSE_LABEL, frozen=True)
    screen = StrictScreen(24, width, [_event(24, width, ord("q"))])

    assert _run(screen, [coin]) == []

    row = _line_with(screen.frames[0], "jm:autof")
    assert "frozn" in row and "RU!" in row
    assert "Warning: Address reuse" in screen.frames[0].text


@pytest.mark.parametrize("value", [100_000_000, 10_000_000_000])
def test_table_preserves_large_coin_values(patched_selector_curses: None, value: int) -> None:
    screen = StrictScreen(24, 80, [_event(24, 80, ord(" ")), _event(24, 80, ord("q"))])

    assert _run(screen, [_utxo("a", value=value)], target_amount=value // 2) == []

    frame = screen.frames[-1]
    assert "sats" in _line_with(frame, "confs")
    assert f"{value:,}" in _line_with(frame, "bcrt1q")
    assert f"Total: {value:,} sats" in frame.text


def test_full_table_reserves_wide_values_and_shows_outpoint_in_footer(
    patched_selector_curses: None,
) -> None:
    coin = _utxo("a", value=2_100_000_000_000_000, label=AUTO_FREEZE_REUSE_LABEL)
    coin.vout = 12_345
    screen = StrictScreen(28, 140, [_event(28, 140, ord("q"))])

    assert _run(screen, [coin]) == []

    row = _line_with(screen.frames[0], "bcrt1q")
    assert "2,100,000,000,000,000" in row
    status = _line_with(screen.frames[0], "Outpoint:")
    # The outpoint sits on the "Outpoint:" line whenever it fits (forced
    # break only on overflow); the explanation follows on its own line.
    assert f"{coin.txid}:12345" in status
    assert "spend" in row and "RU!" in row
    assert "spend" in row and "RU!" in row


@pytest.mark.parametrize("key", [ord("q"), 27])
def test_quit_keys_work_on_a_one_cell_terminal(patched_selector_curses: None, key: int) -> None:
    """The resize prompt is safely clipped and q/Escape remain usable at 1x1."""
    screen = StrictScreen(1, 1, [_event(1, 1, key)])

    assert _run(screen, [_utxo("a"), _utxo("b")]) == []

    assert screen.frames[0].text.strip()


@pytest.mark.parametrize("height", [15, 16, 17])
def test_short_terminals_show_a_coin_or_resize_prompt_never_hidden_confirmation(
    patched_selector_curses: None, height: int
) -> None:
    """A terminal without room for a row must display a resize prompt instead of a blank TUI."""
    width = 120
    screen = StrictScreen(height, width, [_event(height, width, ord("q"))])

    assert _run(screen, [_utxo("a"), _utxo("b")]) == []

    frame = screen.frames[0]
    assert "aaaaaaaa" in frame.text or "resize" in frame.text.lower()


def test_small_screen_keys_do_not_change_or_confirm_selection_before_restore(
    patched_selector_curses: None,
) -> None:
    """Input other than quit is ignored while the selector only has room for its prompt."""
    normal_height = 28
    normal_width = 120
    coin = _utxo("a")
    screen = StrictScreen(
        normal_height,
        normal_width,
        [
            _event(normal_height, normal_width, ord(" ")),
            _event(1, 1, curses.KEY_RESIZE),
            _event(1, 1, ord(" ")),
            _event(1, 1, ord("\n")),
            _event(normal_height, normal_width, curses.KEY_RESIZE),
            _event(normal_height, normal_width, ord("q")),
        ],
    )

    assert _run(screen, [coin]) == []

    restored_frame = screen.frames[-1]
    assert "Selected: 1/1" in restored_frame.text
    assert "Total: 100,000 sats" in restored_frame.text


def test_resize_restore_keeps_selection_cursor_and_scroll_position(
    patched_selector_curses: None,
) -> None:
    """A resize does not lose existing selection or the off-screen navigation position."""
    height = 24
    width = 120
    coins = [_utxo(chr(ord("a") + index), value=10_000 + index) for index in range(14)]
    events = [_event(height, width, ord(" "))]
    events.extend(_event(height, width, ord("j")) for _ in range(9))
    events.extend(
        [
            _event(1, 1, curses.KEY_RESIZE),
            _event(height, width, curses.KEY_RESIZE),
            _event(height, width, ord("\n")),
        ]
    )
    screen = StrictScreen(height, width, events)

    assert _run(screen, coins) == [coins[0]]

    restored_frame = screen.frames[-1]
    assert "Total: 10,000 sats" in restored_frame.text
    assert "kkkkkkkk" in restored_frame.text


def test_enlarging_a_shrunken_window_refills_the_list(patched_selector_curses: None) -> None:
    """A bottom-reached scroll offset from a shrunken window is given up on enlarge."""
    coins = [_utxo(chr(ord("a") + index)) for index in range(14)]
    events = [
        _event(32, 120, ord("G")),  # cursor to the last coin
        _event(20, 120, curses.KEY_RESIZE),  # shrink: only the final rows stay visible
        # Enlarge past the footer's guaranteed blank line (14 footer rows):
        # 34 rows give the full 14-coin list room again (5 + 14 + 14 + 1).
        _event(34, 120, curses.KEY_RESIZE),
        _event(34, 120, ord("q")),
    ]
    screen = StrictScreen(34, 120, events)

    assert _run(screen, coins) == []

    shrunk_frame = screen.frames[-2]
    assert "Terminal too small" not in shrunk_frame.text
    assert "nnnnnnnn" in shrunk_frame.text  # cursor row stayed visible
    assert "aaaaaaaa" not in shrunk_frame.text
    restored_frame = screen.frames[-1]
    assert "aaaaaaaa" in restored_frame.text
    assert "nnnnnnnn" in restored_frame.text


def test_interrupted_repaint_does_not_confirm_selection(patched_selector_curses: None) -> None:
    class InterruptedScreen(StrictScreen):
        def addstr(self, y: int, x: int, text: str, _attr: int = 0) -> None:
            if len(self.frames) == 1 and "aaaaaaaa" in text:
                raise curses.error("terminal resized during repaint")
            super().addstr(y, x, text, _attr)

    screen = InterruptedScreen(
        24,
        120,
        [_event(24, 120, ord(" ")), _event(24, 120, ord("\n")), _event(24, 120, ord("q"))],
    )

    assert _run(screen, [_utxo("a")]) == []
    assert "resize" in screen.frames[1].text
    assert "Selected: 1/1" in screen.frames[-1].text


def test_immature_and_dynamically_pinned_other_mixdepth_states_match_footer(
    patched_selector_curses: None,
) -> None:
    """Blocked states explain min-confirmation and dynamic source restrictions consistently."""
    height = 32
    width = 140
    selectable = _utxo("a", mixdepth=0)
    immature = _utxo("b", mixdepth=0, confirmations=2)
    other_mixdepth = _utxo("c", mixdepth=1)
    screen = StrictScreen(
        height,
        width,
        [
            _event(height, width, ord(" ")),
            _event(height, width, ord(" ")),
            _event(height, width, ord(" ")),
            _event(height, width, ord("\n")),
        ],
    )

    assert _run(screen, [selectable, immature, other_mixdepth], min_confirmations=5) == [selectable]

    immature_frame = _frame_with(screen.frames, "requires 5 confirmations")
    other_mixdepth_frame = _frame_with(screen.frames, "source is m0")
    assert "immat" in _line_with(immature_frame, "bbbbbbbb")
    assert "5 confirmation" in immature_frame.text
    assert "oth-md" in _line_with(other_mixdepth_frame, "cccccccc")
    assert "Source mixdepth: m0" in other_mixdepth_frame.text


def test_fixed_mixdepth_other_state_matches_footer_and_cannot_be_selected(
    patched_selector_curses: None,
) -> None:
    """A caller-pinned mixdepth labels context coins as other-md and leaves them blocked."""
    height = 32
    width = 140
    allowed = _utxo("a", mixdepth=0)
    blocked = _utxo("b", mixdepth=1)
    screen = StrictScreen(
        height,
        width,
        [
            _event(height, width, ord("j")),
            _event(height, width, ord(" ")),
            _event(height, width, ord("q")),
        ],
    )

    assert _run(screen, [allowed, blocked], allowed_mixdepth=0) == []

    blocked_frame = _frame_with(screen.frames, "source is m0")
    assert "oth-md" in _line_with(blocked_frame, "bbbbbbbb")
    assert "Source mixdepth: m0" in blocked_frame.text
    assert "[x]" not in _line_with(blocked_frame, "bbbbbbbb")


def test_select_all_and_deselect_all_respect_gates_then_unpin_mixdepth(
    patched_selector_curses: None,
) -> None:
    """s/d select only eligible coins, and d restores selection in another mixdepth."""
    height = 34
    width = 160
    first = _utxo("a", mixdepth=0, value=20_000)
    second = _utxo("b", mixdepth=0, value=30_000)
    frozen = _utxo("c", mixdepth=0, frozen=True)
    locked = _utxo("d", mixdepth=0, locktime=4_000_000_000)
    in_use = _utxo("e", mixdepth=0)
    immature = _utxo("f", mixdepth=0, confirmations=1)
    other_mixdepth = _utxo("g", mixdepth=1, value=70_000)
    screen = StrictScreen(
        height,
        width,
        [
            _event(height, width, ord("s")),
            _event(height, width, ord("d")),
            _event(height, width, ord("G")),
            _event(height, width, ord("s")),
            _event(height, width, ord("\n")),
        ],
    )

    assert _run(
        screen,
        [first, second, frozen, locked, in_use, immature, other_mixdepth],
        min_confirmations=5,
        excluded_outpoints={(in_use.txid, in_use.vout)},
    ) == [other_mixdepth]

    assert "Selected: 2/2" in screen.frames[1].text
    assert "Selected: 0/3" in screen.frames[2].text
    selected_frame = screen.frames[1]
    for state in ("frozn", "lock", "in-use", "immat"):
        assert state in selected_frame.text


def test_coinjoin_context_blocks_expired_bond_from_toggle_and_select_all(
    patched_selector_curses: None,
) -> None:
    """With allow_fidelity_bonds=False an expired bond is no-cj: Space and s skip it."""
    height = 32
    width = 160
    coin = _utxo("a", mixdepth=0, value=20_000)
    expired_bond = _utxo("b", mixdepth=0, value=50_000, locktime=1)
    expired_bond.scriptpubkey = "0020" + "b" * 64
    screen = StrictScreen(
        height,
        width,
        [
            _event(height, width, ord("j")),  # cursor onto the bond
            _event(height, width, ord(" ")),  # toggle attempt
            _event(height, width, ord("s")),  # select all in mixdepth
            _event(height, width, ord("\n")),
        ],
    )

    assert _run(screen, [coin, expired_bond], allow_fidelity_bonds=False) == [coin]

    bond_frame = _frame_with(screen.frames, "fidelity bonds cannot be used in CoinJoins")
    bond_row = _line_with(bond_frame, "bbbbbbbb")
    assert "[-]" in bond_row
    assert "no-cj" in bond_row
    assert "fb-x" in bond_row
    assert "fidelity bonds cannot be used in CoinJoins" in bond_frame.text
    assert "Selected: 1/1" in screen.frames[-1].text


def test_send_context_keeps_expired_bond_selectable(patched_selector_curses: None) -> None:
    """Default allow_fidelity_bonds=True: the redeem path can still pick an expired bond."""
    height = 32
    width = 160
    expired_bond = _utxo("b", mixdepth=0, value=50_000, locktime=1)
    expired_bond.scriptpubkey = "0020" + "b" * 64
    screen = StrictScreen(
        height,
        width,
        [_event(height, width, ord(" ")), _event(height, width, ord("\n"))],
    )

    assert _run(screen, [expired_bond]) == [expired_bond]
    # The spend state lives in the table's state column only.
    bond_row = _line_with(screen.frames[0], "bbbbbbbb")
    assert "spend" in bond_row
    assert "no-cj" not in screen.frames[0].text


@pytest.mark.parametrize(
    ("target_amount", "expected", "forbidden"),
    [
        (0, "Sweep mode", "Target:"),
        (50_000, "Need: 10,000 sats", "Excess:"),
        (40_000, "Target met", "Need:"),
        (30_000, "Excess: 10,000 sats", "Need:"),
    ],
)
def test_sweep_and_target_summaries_cover_under_equal_and_over(
    patched_selector_curses: None,
    target_amount: int,
    expected: str,
    forbidden: str,
) -> None:
    """Selection details distinguish sweep, insufficient, exact, and excess totals."""
    height = 30
    width = 140
    screen = StrictScreen(
        height,
        width,
        [_event(height, width, ord(" ")), _event(height, width, ord("q"))],
    )

    assert _run(screen, [_utxo("a", value=40_000)], target_amount=target_amount) == []

    summary = screen.frames[-1].text
    assert expected in summary
    assert forbidden not in summary
    assert "Total: 40,000 sats" in summary


@pytest.mark.parametrize("width", [80, 120])
def test_real_curses_pty_renders_selection_and_reuse_warning(width: int) -> None:
    """Exercise the public selector and inspect actual curses cells in a pseudo-terminal."""
    if not sys.platform.startswith("linux"):
        pytest.skip("real curses PTY regression test requires Linux")

    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import termios

    master_fd, slave_fd = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    try:
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 25, width, 0, 0))
        environment = os.environ.copy()
        environment["TERM"] = "xterm"
        python_paths = [path for path in sys.path if path]
        if existing_pythonpath := environment.get("PYTHONPATH"):
            python_paths.append(existing_pythonpath)
        environment["PYTHONPATH"] = os.pathsep.join(python_paths)
        script = """
import curses

import jmwallet.utxo_selector as selector
from jmwallet.wallet.models import UTXOInfo

coin_a = UTXOInfo(
    txid="a" * 64,
    vout=0,
    value=100_000,
    address="bcrt1qptyone",
    confirmations=10,
    scriptpubkey="0014" + "aa" * 20,
    path="m/84'/0'/0'/0/0",
    mixdepth=0,
)
coin_b = UTXOInfo(
    txid="b" * 64,
    vout=0,
    value=50_000,
    address="bcrt1qptyone",
    confirmations=10,
    scriptpubkey="0014" + "bb" * 20,
    path="m/84'/0'/0'/0/1",
    mixdepth=0,
    frozen=True,
    label="jm:autofrozen:reuse",
)
real_wrapper = curses.wrapper

def queue_cancel(callback, *args):
    def run_with_cancel(stdscr):
        curses.ungetch(ord("q"))
        curses.ungetch(ord(" "))
        result = callback(stdscr, *args)
        height, width = stdscr.getmaxyx()
        lines = [stdscr.instr(y, 0, width - 1).decode() for y in range(height)]
        assert any("Total: 100,000 sats" in line for line in lines), lines
        assert any("Target: 50,000 sats" in line for line in lines), lines
        frozen_row = next(line for line in lines if "jm:autof" in line)
        assert "frozn" in frozen_row and "RU!" in frozen_row, frozen_row
        # Wiring guard: the status block must go through wrap_status_text.
        # The displayed outpoint must sit WHOLE on a single line — a lost
        # wiring hard-chunks it mid-value across two lines (this is the
        # parity property, independent of which coin the cursor shows).
        txids = (coin_a.txid, coin_b.txid)
        whole = [line for line in lines if any(f"{t}:0" in line for t in txids)]
        partial = [
            line
            for line in lines
            if any(t[:24] in line and f"{t}:0" not in line for t in txids)
        ]
        assert whole and not partial, lines
        return result
    return real_wrapper(run_with_cancel)

selector.curses.wrapper = queue_cancel
result = selector.select_utxos_interactive([coin_a, coin_b], target_amount=50_000)
assert result == []
print("PTY_SELECTOR_RESULT=[]", flush=True)
"""
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=environment,
        )
        os.close(slave_fd)
        slave_fd = -1

        output = bytearray()
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            readable, _, _ = select.select([master_fd], [], [], 0.1)
            if readable:
                try:
                    output.extend(os.read(master_fd, 4096))
                except OSError:
                    break
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
            pytest.fail("selector PTY child timed out")

        while True:
            readable, _, _ = select.select([master_fd], [], [], 0)
            if not readable:
                break
            try:
                output.extend(os.read(master_fd, 4096))
            except OSError:
                break

        assert process.returncode == 0, output.decode(errors="replace")
        assert b"PTY_SELECTOR_RESULT=[]" in output
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        if slave_fd >= 0:
            os.close(slave_fd)
        os.close(master_fd)


@pytest.mark.parametrize("width", [80, 81, 82])
def test_no_status_padding_line_when_wallet_has_longer_outpoints(
    patched_selector_curses: None, width: int
) -> None:
    """No wallet-maximum padding line in the status block (freeze parity)."""

    # Freeze-parity: a 2-digit-vout coin tips the wrap threshold one column
    # earlier; the dropped max-reserve padding must not leave a visible
    # empty line at width 81 (user-reported resize flip 80/81/82).
    cursor_coin = _utxo("a")  # vout 0
    wide_coin = _utxo("b")
    wide_coin.vout = 15
    screen = StrictScreen(30, width, [_event(30, width, ord("q"))])

    assert _run(screen, [cursor_coin, wide_coin], target_amount=50_000) == []

    lines = screen.frames[0].lines
    start = next(i for i, line in enumerate(lines) if "UTXO Status" in line)
    status_block = []
    for line in lines[start + 1 :]:
        if line.startswith("|") and set(line) <= set("|-"):
            break
        status_block.append(line)
    assert any(f"{cursor_coin.txid}:0" in line for line in status_block)
    for line in status_block:
        inner = line.strip("|").strip()
        assert inner, status_block


def test_branch_group_separators_are_labelled_in_extended_view_order(
    patched_selector_curses: None,
) -> None:
    utxos = [
        _utxo("a", mixdepth=0),
        _utxo("b", mixdepth=0, branch=1),
        _utxo("c", mixdepth=0, branch=2, locktime=1_893_456_000),
        _utxo("d", mixdepth=1),
    ]
    # Tall enough that every group row (blank rows included) stays visible.
    screen = StrictScreen(30, 120, [_event(30, 120, ord("q"))])
    _run(screen, utxos)
    text = screen.frames[-1].text
    # A mixdepth's first group gets the full title; further branch groups
    # strike out only the "Mixdepth N" part and keep the branch name.
    fragments = [
        "Mixdepth 0 — fidelity bond addresses",
        "---------- — external addresses",
        "---------- — internal addresses",
        "Mixdepth 1 — external addresses",
    ]
    positions = [text.index(fragment) for fragment in fragments]
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)


def test_single_group_gets_a_leading_labelled_separator(
    patched_selector_curses: None,
) -> None:
    screen = StrictScreen(24, 120, [_event(24, 120, ord("q"))])
    _run(screen, [_utxo("a")])
    assert "Mixdepth 0 — external addresses" in screen.frames[-1].text


class TestSeparatorLayout:
    def test_new_mixdepth_gets_blank_row_and_full_title(
        self, patched_selector_curses: None
    ) -> None:
        screen = StrictScreen(24, 120, [_event(24, 120, ord("q"))])
        _run(screen, [_utxo("a", mixdepth=0), _utxo("b", mixdepth=1)])
        frame = screen.frames[-1]
        title_row = next(
            i for i, line in enumerate(frame.lines) if "Mixdepth 1 — external addresses" in line
        )
        blank_row = frame.lines[title_row - 1]
        # The blank border-only row that introduces a new mixdepth.
        assert "|" in blank_row
        assert "-" not in blank_row

    def test_branch_continuation_strikes_out_only_the_mixdepth_part(
        self, patched_selector_curses: None
    ) -> None:
        utxos = [
            _utxo("a", mixdepth=0, branch=2, locktime=1_893_456_000),
            _utxo("b", mixdepth=0, branch=0),
        ]
        screen = StrictScreen(24, 120, [_event(24, 120, ord("q"))])
        _run(screen, utxos)
        frame = screen.frames[-1]
        full = _line_with(frame, "Mixdepth 0 — fidelity bond addresses")
        short = _line_with(frame, "— external addresses")
        # Only the "Mixdepth N" part is struck out; the em dash and the
        # branch name keep the columns of the full title above.
        title_col = full.index("Mixdepth 0")
        md_width = len("Mixdepth 0")
        assert short[title_col : title_col + md_width] == "-" * md_width
        assert short.index(" — ") == full.index(" — ")
        assert short.index("external addresses") == full.index("fidelity bond addresses")
        # The rest of the row is dashes and borders only — no repeated label.
        tail = short[short.index("external addresses") + len("external addresses") :]
        assert "-" in tail
        assert set(tail) <= {"-", "|", " "}

    def test_branch_continuation_has_no_blank_row_above(
        self, patched_selector_curses: None
    ) -> None:
        utxos = [
            _utxo("a", mixdepth=0, branch=2, locktime=1_893_456_000),
            _utxo("b", mixdepth=0, branch=0),
        ]
        screen = StrictScreen(24, 120, [_event(24, 120, ord("q"))])
        _run(screen, utxos)
        frame = screen.frames[-1]
        short_row = next(i for i, line in enumerate(frame.lines) if "— external addresses" in line)
        # A branch change within one mixdepth follows the previous coin row
        # directly; only a new mixdepth is introduced by a blank row.
        assert "[" in frame.lines[short_row - 1]


def test_blocked_toggle_shows_a_transient_mixdepth_pin_notice(
    patched_selector_curses: None,
) -> None:
    utxos = [_utxo("a", mixdepth=0), _utxo("b", mixdepth=1)]
    screen = StrictScreen(
        24,
        120,
        [
            _event(24, 120, ord(" ")),  # select the only m0 coin; cursor moves to m1
            _event(24, 120, ord(" ")),  # blocked by the mixdepth pin: notice
            _event(24, 120, ord("j")),  # any further key clears it again
            _event(24, 120, ord("q")),
        ],
    )
    _run(screen, utxos)
    notice = "Locked to source mixdepth m0 — press d to deselect all"
    assert notice in screen.frames[2].text
    assert notice not in screen.frames[3].text


def test_pin_notice_takes_precedence_over_the_reuse_warning(
    patched_selector_curses: None,
) -> None:
    shared_address = "bcrt1qsharedaddress"
    utxos = [
        _utxo("a", mixdepth=0, address=shared_address),
        _utxo("b", mixdepth=1, address=shared_address),
    ]
    screen = StrictScreen(
        24,
        120,
        [
            _event(24, 120, ord(" ")),
            _event(24, 120, ord(" ")),
            _event(24, 120, ord("j")),
            _event(24, 120, ord("q")),
        ],
    )
    _run(screen, utxos)
    ru_note = "RU! Warning: Address reuse reduces privacy"
    notice = "Locked to source mixdepth m0 — press d to deselect all"
    # Cursor sits on the reused m1 coin: the warning shows before and after,
    # but the blocked-toggle notice replaces it for one render cycle.
    assert ru_note in screen.frames[1].text
    assert notice in screen.frames[2].text
    assert ru_note not in screen.frames[2].text
    assert ru_note in screen.frames[3].text
    assert notice not in screen.frames[3].text


def test_help_hint_in_controls_and_overlay_opens_and_closes(
    patched_selector_curses: None,
) -> None:
    """``?`` opens the help overlay; the next key closes it back to the list."""
    coins = [_utxo("a"), _utxo("b", frozen=True)]
    # Height 48: the overlay holds the title header, the full tag + state
    # legend plus the view rules; at smaller heights the tail (rules,
    # close hint, closing rule) would be cut off.
    screen = StrictScreen(
        48,
        120,
        [
            _event(48, 120, ord("?")),
            _event(48, 120, ord("q")),  # closes the overlay only
            _event(48, 120, ord("q")),  # cancels the selector
        ],
    )

    assert _run(screen, coins) == []

    list_frame = screen.frames[0]
    assert "?: help" in list_frame.text  # hint lives in the Controls footer

    help_frame = screen.frames[1]
    # Help-screen title: TUI header style, second row, centered, dashes
    # included, no frame around it.
    assert help_frame.lines[1].strip() == "— UTXO Selector Help Screen —"
    assert help_frame.lines[1] != help_frame.lines[1].strip()
    # Shared legend content in the extended view's legend format.
    assert "tags (type of address or funds):" in help_frame.text
    assert "states (spendability):" in help_frame.text
    assert "unconf" in help_frame.text and "in-use" in help_frame.text
    assert "jm-wallet send --select-utxos" in help_frame.text
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
    assert "UTXO Selector" in back_frame.text
    assert "tags (type of address" not in back_frame.text


def test_help_scrolls_at_24_lines_and_reflows_after_resize(
    patched_selector_curses: None,
) -> None:
    screen = StrictScreen(
        24,
        100,
        [
            _event(24, 80, ord("?")),  # Resize between drawing the list and opening help.
            *[_event(24, 80, curses.KEY_DOWN) for _ in range(20)],
            _event(24, 60, curses.KEY_RESIZE),
            _event(24, 100, ord("q")),
            _event(24, 100, ord("q")),
        ],
    )

    assert _run(screen, [_utxo("a")]) == []

    first_help = screen.frames[1]
    assert first_help.width == 80
    assert "q/Esc: back to the list" in first_help.text
    assert "oth-md" not in first_help.text
    before_resize, after_resize, back_to_list = screen.frames[-3:]
    assert "oth-md" in before_resize.text
    assert after_resize.width == 60
    assert "UTXO Selector Help Screen" in after_resize.text
    assert "q/Esc: back to the list" in after_resize.text
    assert "UTXO Selector Help Screen" not in back_to_list.text


def test_enter_cannot_auto_select_unconfirmed_coin_outside_pinned_mixdepth(
    patched_selector_curses: None,
) -> None:
    screen = StrictScreen(24, 100, [_event(24, 100, ord("\n")), _event(24, 100, ord("q"))])

    assert _run(screen, [_utxo("a", mixdepth=1, confirmations=0)], allowed_mixdepth=0) == []
    assert "oth-md" in screen.frames[0].text


def test_reuse_column_appears_only_when_reuse_exists(
    patched_selector_curses: None,
) -> None:
    """The RU! column exists if and only if the wallet has address reuse."""

    def run(utxos: list[UTXOInfo]) -> Frame:
        # Height 26: the status block costs one list row, so this run keeps
        # the same visible list capacity the 24-row layout had before it.
        screen = StrictScreen(26, 140, [_event(26, 140, ord("q"))])
        assert _run(screen, utxos) == []
        return screen.frames[0]

    # No reuse: no column, nothing right of the table's right border, no
    # footer warning.
    clean = run([_utxo("a"), _utxo("b")])
    assert _line_with(clean, "state").rstrip().endswith("|")
    assert "RU!" not in clean.text
    assert "Warning: Address reuse" not in clean.text

    # Reuse: flush header cell and row cells inside the border; the footer
    # carries the explanation.
    reused = run(
        [
            _utxo("a", address="bcrt1qshared"),
            _utxo("b", address="bcrt1qshared"),
            _utxo("c"),
        ]
    )
    assert _line_with(reused, "state").rstrip().endswith("|RU!|")
    reused_rows = [line for line in reused.lines if "bcrt1qshared" in line and "|" in line]
    assert reused_rows
    for row in reused_rows:
        assert row.rstrip().endswith("|RU!|")
    # The unused coin's row keeps the border flush with blank cell padding.
    other_row = _line_with(reused, "bcrt1qcccccccccccc")
    assert other_row.rstrip().endswith("|   |")
    assert "Warning: Address reuse" in reused.text


def test_footer_opens_with_the_table_closing_separator(
    patched_selector_curses: None,
) -> None:
    """The footer's first row closes the table; no reserved blank row remains."""
    screen = StrictScreen(24, 140, [_event(24, 140, ord("q"))])
    assert _run(screen, [_utxo("a")]) == []

    frame = screen.frames[0]
    sep_rows = [line for line in frame.lines if line.startswith("|-")]
    # Bottom edge of the table plus the footer's internal separators.
    assert len(sep_rows) >= 2
    # Between the last coin row and the table's closing separator only empty
    # framed rows may appear (border columns stay connected; no stray text).
    last_coin = frame.lines.index(_line_with(frame, "bcrt1q"))
    closing = next(
        i
        for i, line in enumerate(frame.lines[last_coin + 1 :], last_coin + 1)
        if line.startswith("|-")
    )
    for filler in frame.lines[last_coin + 1 : closing]:
        assert filler.startswith("|") and filler.rstrip().endswith("|")
        assert filler.replace("|", "").strip() == ""


def test_eighty_columns_long_wallet_name_renders_with_one_line_status(
    patched_selector_curses: None,
) -> None:
    """80x22 with a long wallet name renders via the 1-line status fallback.

    Regression: the 2-line status block (forced by narrow widths) pushed the
    vertical budget below zero and the generic 'too small' prompt hid a
    height problem that the one-line block solves. At 80 columns with the
    selector's footer the 2-line block needs one row more than the terminal
    offers; the one-line form fits.
    """
    coin = _utxo("a")
    screen = StrictScreen(22, 80, [_event(22, 80, ord("q"))])

    result = utxo_selector._run_selector(
        screen,
        utxo_selector._prepare_display_items([coin]),
        0,
        None,
        0,
        set(),
        True,
        wallet_name="a-very-long-wallet-name-for-height-budget-testing",
        wallet_fingerprint="abcd1234",
    )

    assert result == []
    frame = screen.frames[-1]
    assert "Terminal too" not in frame.text
    status_line = _line_with(frame, "Active Wallet:")
    # One-line status form: the row is one line, so narrow widths clip even
    # labels -- anchor on the fingerprint value, not the label.
    assert "abcd1234" in status_line


def test_width_below_minimum_prompt_names_width(patched_selector_curses: None) -> None:
    """Below the table minimum the prompt names width as the fix."""
    coin = _utxo("a")
    screen = StrictScreen(24, 40, [_event(24, 40, ord("q"))])

    result = utxo_selector._run_selector(
        screen, utxo_selector._prepare_display_items([coin]), 0, None, 0, set(), True
    )

    assert result == []
    assert "Terminal too narrow" in screen.frames[-1].text
    assert "Terminal too short" not in screen.frames[-1].text


def test_short_terminal_prompt_names_height(patched_selector_curses: None) -> None:
    """Enough columns but no vertical budget: the prompt names height."""
    coin = _utxo("a")
    screen = StrictScreen(3, 120, [_event(3, 120, ord("q"))])

    result = utxo_selector._run_selector(
        screen, utxo_selector._prepare_display_items([coin]), 0, None, 0, set(), True
    )

    assert result == []
    assert "Terminal too short" in screen.frames[-1].text
    assert "Terminal too narrow" not in screen.frames[-1].text
