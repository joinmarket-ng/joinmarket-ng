"""Guided setup routes through this interpreter without changing the shell menu."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from jmcore import tui


def test_setup_arguments_are_forwarded_without_whiptail_or_shell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = ModuleType("jmwallet.onboarding")
    main = Mock()
    setattr(module, "main", main)
    monkeypatch.setitem(sys.modules, "jmwallet.onboarding", module)
    monkeypatch.setattr(
        sys, "argv", ["jm-ng", "setup", "--data-dir", "/path with spaces", "--yes"]
    )
    monkeypatch.setattr(
        tui.shutil,
        "which",
        lambda executable: pytest.fail("setup must not require whiptail"),
    )
    monkeypatch.setattr(
        tui.os,
        "execvpe",
        lambda *args: pytest.fail("setup must not launch the shell menu"),
    )
    tui.main()
    main.assert_called_once_with(["--data-dir", "/path with spaces", "--yes"])
