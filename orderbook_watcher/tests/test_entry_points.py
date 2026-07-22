"""
Tests for the console-script entry points.

``jm-orderbook-watcher`` is the primary command (consistent with the other
``jm-*`` CLIs); ``orderbook-watcher`` is a deprecated alias that warns and
forwards.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from orderbook_watcher.main import main_deprecated


def test_pyproject_declares_primary_and_deprecated_scripts() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text())["project"]["scripts"]

    assert scripts["jm-orderbook-watcher"] == "orderbook_watcher.main:main"
    assert scripts["orderbook-watcher"] == "orderbook_watcher.main:main_deprecated"


def test_deprecated_alias_warns_and_forwards(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr("orderbook_watcher.main.main", lambda: calls.append(True))

    main_deprecated()

    assert calls == [True]
    stderr = capsys.readouterr().err
    assert "deprecated" in stderr
    assert "jm-orderbook-watcher" in stderr


def test_deprecated_alias_still_serves_help(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("sys.argv", ["orderbook-watcher", "--help"])

    with pytest.raises(SystemExit) as exc_info:
        main_deprecated()
    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    assert "JoinMarket Orderbook Watcher" in captured.out
    assert "deprecated" in captured.err
