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
) -> UTXOInfo:
    return UTXOInfo(
        txid=txid_char * 64,
        vout=0,
        value=value,
        address=f"bcrt1q{txid_char * 12}",
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
