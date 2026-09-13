"""Regression coverage for UTXO selector rendering and resize behavior."""

from __future__ import annotations

import curses
import os
import sys
import time
from collections import deque
from dataclasses import dataclass

import pytest

import jmwallet.utxo_selector as utxo_selector
from jmwallet.utxo_tui import build_display_items, utxo_sort_key
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.utxo_metadata import AUTO_FREEZE_REUSE_LABEL


@dataclass(frozen=True)
class KeyEvent:
    """A key read and the terminal dimensions in effect for the next frame."""

    height: int
    width: int
    key: int


@dataclass(frozen=True)
class Frame:
    """A completed curses framebuffer captured by :class:`StrictScreen`."""

    height: int
    width: int
    lines: tuple[str, ...]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class StrictScreen:
    """Small curses-window double that rejects off-screen writes and extra input reads."""

    def __init__(self, height: int, width: int, events: list[KeyEvent]) -> None:
        self._height = height
        self._width = width
        self._events = deque(events)
        self._rows: list[list[str]] = []
        self.frames: list[Frame] = []
        self.clear()

    @property
    def pending_events(self) -> int:
        return len(self._events)

    def getmaxyx(self) -> tuple[int, int]:
        return self._height, self._width

    def clear(self) -> None:
        self._rows = [[" "] * self._width for _ in range(self._height)]

    def erase(self) -> None:
        self.clear()

    def addstr(self, y: int, x: int, text: str, _attr: int = 0) -> None:
        if y < 0 or x < 0 or y >= self._height or x + len(text) > self._width:
            raise AssertionError(
                f"off-screen write at ({y}, {x}) for {len(text)} chars on "
                f"{self._height}x{self._width} screen"
            )
        self._rows[y][x : x + len(text)] = text

    def addnstr(self, y: int, x: int, text: str, count: int, attr: int = 0) -> None:
        self.addstr(y, x, text[:count], attr)

    def attron(self, _attr: int) -> None:
        pass

    def attroff(self, _attr: int) -> None:
        pass

    def keypad(self, _enabled: bool) -> None:
        pass

    def nodelay(self, _enabled: bool) -> None:
        pass

    def refresh(self) -> None:
        self.frames.append(
            Frame(self._height, self._width, tuple("".join(row) for row in self._rows))
        )

    def getch(self) -> int:
        if not self._events:
            raise AssertionError("selector requested input after scripted events were exhausted")
        event = self._events.popleft()
        self._height = event.height
        self._width = event.width
        return event.key


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
) -> UTXOInfo:
    return UTXOInfo(
        txid=txid_char * 64,
        vout=0,
        value=value,
        address=address or f"bcrt1q{txid_char * 12}",
        confirmations=confirmations,
        scriptpubkey="0014" + txid_char * 40,
        path=f"m/84'/0'/{mixdepth}'/0/{ord(txid_char) - ord('a'):02d}",
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
) -> list[UTXOInfo]:
    display_items = build_display_items(sorted(utxos, key=utxo_sort_key))
    result = utxo_selector._run_selector(
        screen,
        display_items,
        target_amount,
        allowed_mixdepth,
        min_confirmations,
        excluded_outpoints or set(),
    )
    assert screen.pending_events == 0
    return result


def _line_with(frame: Frame, text: str) -> str:
    return next(line for line in frame.lines if text in line)


def _frame_with(frames: list[Frame], text: str) -> Frame:
    return next(frame for frame in frames if text in frame.text)


@pytest.mark.parametrize(
    ("width", "is_full_table"),
    [(80, False), (120, True), (160, True), (200, True)],
)
def test_widths_keep_monetary_summary_and_table_essentials(
    patched_selector_curses: None, width: int, is_full_table: bool
) -> None:
    """The compact 80-column table keeps spend-critical columns and full sats values."""
    height = 32
    coin = _utxo("a")
    screen = StrictScreen(
        height,
        width,
        [_event(height, width, ord(" ")), _event(height, width, ord("q"))],
    )

    assert _run(screen, [coin], target_amount=50_000) == []

    selected_frame = screen.frames[-1]
    header = _line_with(selected_frame, "Amount")
    assert "Target: 50,000 sats" in selected_frame.text
    assert "Total: 100,000 sats" in selected_frame.text
    assert "100,000 sats" in selected_frame.text
    assert "aaaaaaaa" in selected_frame.text
    if is_full_table:
        assert "Address" in header
        assert "Confs" in header
    else:
        assert "Address" not in header
        assert "Confs" not in header


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
    auto_row = _line_with(auto_frame, "aaaaaaaa")
    custom_row = _line_with(custom_frame, "bbbbbbbb")
    assert "frozen" in auto_frame.text
    assert "RU!" in auto_frame.text
    assert "jm:" in auto_row
    assert "frozen" in custom_row
    assert "RU!" in custom_row
    assert custom_label not in custom_row
    assert "..." in custom_row
    assert "Label: " in custom_frame.text
    if full_footer_label:
        assert f"Label: {AUTO_FREEZE_REUSE_LABEL}" in auto_frame.text
        assert f"Label: {custom_label}" in custom_frame.text
    else:
        assert custom_label not in custom_frame.text
        assert "..." in _line_with(custom_frame, "Label: ")


@pytest.mark.parametrize("width", [80, 107, 120, 160, 200])
def test_autofrozen_label_alone_keeps_row_state_and_reuse_warning(
    patched_selector_curses: None, width: int
) -> None:
    coin = _utxo("a", label=AUTO_FREEZE_REUSE_LABEL, frozen=True)
    screen = StrictScreen(24, width, [_event(24, width, ord("q"))])

    assert _run(screen, [coin]) == []

    row = _line_with(screen.frames[0], "aaaaaaaa")
    assert "frozen" in row and "RU!" in row
    assert "Warning: Address reuse" in screen.frames[0].text


@pytest.mark.parametrize("value", [100_000_000, 10_000_000_000])
def test_compact_table_preserves_large_coin_values(
    patched_selector_curses: None, value: int
) -> None:
    screen = StrictScreen(24, 80, [_event(24, 80, ord(" ")), _event(24, 80, ord("q"))])

    assert _run(screen, [_utxo("a", value=value)], target_amount=value // 2) == []

    frame = screen.frames[-1]
    assert "Amount (sats)" in frame.text
    assert f"{value:,}" in _line_with(frame, "aaaaaaaa")
    assert f"Total: {value:,} sats" in frame.text


def test_full_table_reserves_large_amount_and_outpoint_widths(
    patched_selector_curses: None,
) -> None:
    coin = _utxo("a", value=2_100_000_000_000_000, label=AUTO_FREEZE_REUSE_LABEL)
    coin.vout = 12_345
    screen = StrictScreen(28, 140, [_event(28, 140, ord("q"))])

    assert _run(screen, [coin]) == []

    row = _line_with(screen.frames[0], "aaaaaaaa")
    assert "2,100,000,000,000,000 sats" in row
    assert "aaaaaaaa...:12345" in row
    assert "spendable" in row and "RU!" in row


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

    immature_frame = _frame_with(screen.frames, "State: immature")
    other_mixdepth_frame = _frame_with(screen.frames, "State: other-md")
    assert "immature" in _line_with(immature_frame, "bbbbbbbb")
    assert "5 confirmation" in immature_frame.text
    assert "other-md" in _line_with(other_mixdepth_frame, "cccccccc")
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

    blocked_frame = _frame_with(screen.frames, "State: other-md")
    assert "other-md" in _line_with(blocked_frame, "bbbbbbbb")
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
    for state in ("frozen", "locked", "in-use", "immature"):
        assert state in selected_frame.text


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
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 24, width, 0, 0))
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
        frozen_row = next(line for line in lines if "bbbbbbbb" in line)
        assert "frozen" in frozen_row and "RU!" in frozen_row, frozen_row
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
