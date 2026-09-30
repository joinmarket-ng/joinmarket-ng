"""Eager version handling for CLIs without Typer."""

from __future__ import annotations

import pytest

from jmcore.version import exit_if_version_requested, get_version


@pytest.mark.parametrize("args", [["--version"], ["--unknown", "--version"]])
def test_version_exits_successfully(args: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        exit_if_version_requested(args)
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"JoinMarket NG {get_version()}\n"


@pytest.mark.parametrize("args", [[], ["--unknown"], ["--version=1"], ["--", "--version"]])
def test_non_version_arguments_are_unchanged(
    args: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    original = args.copy()
    exit_if_version_requested(args)
    assert args == original
    assert capsys.readouterr().out == ""
