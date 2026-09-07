"""Regression tests for wallet CLI password hardening."""

from __future__ import annotations

from enum import Enum, auto
from pathlib import Path
from unittest.mock import Mock

import pytest
import typer
from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.cli.mnemonic import encrypt_mnemonic
from jmwallet.cli.wallet import showseed, verify_password

_COMMAND_LINE_PASSWORD_WARNING = (
    "WARNING: Passing a password on the command line can expose it to other users."
)
_MNEMONIC = "abandon " * 11 + "about"


def _make_encrypted_wallet(tmp_path: Path, password: str) -> Path:
    wallet = tmp_path / "wallet.mnemonic"
    wallet.write_bytes(encrypt_mnemonic(_MNEMONIC, password))
    return wallet


@pytest.mark.parametrize(
    ("command", "extra_args", "expected_stdout"),
    [
        ("verify-password", ["--no-prompt"], "Password is CORRECT\n"),
        (
            "showseed",
            ["--yes"],
            "\n".join(f"{index:2d}. {word}" for index, word in enumerate(_MNEMONIC.split(), 1))
            + "\n",
        ),
    ],
)
def test_command_line_password_warning_preserves_stdout(
    tmp_path: Path,
    command: str,
    extra_args: list[str],
    expected_stdout: str,
) -> None:
    password = "secret-password"
    wallet = _make_encrypted_wallet(tmp_path, password)
    runner = CliRunner()

    result = runner.invoke(app, [command, "-f", str(wallet), "-p", password, *extra_args])

    assert result.exit_code == 0, result.output
    assert result.stdout == expected_stdout
    assert _COMMAND_LINE_PASSWORD_WARNING in result.stderr
    assert password not in result.stderr


@pytest.mark.parametrize(
    ("command", "extra_args", "expected_stdout"),
    [
        ("verify-password", ["--no-prompt"], "Password is CORRECT\n"),
        (
            "showseed",
            ["--yes"],
            "\n".join(f"{index:2d}. {word}" for index, word in enumerate(_MNEMONIC.split(), 1))
            + "\n",
        ),
    ],
)
def test_environment_password_does_not_warn(
    tmp_path: Path,
    command: str,
    extra_args: list[str],
    expected_stdout: str,
) -> None:
    password = "environment-secret"
    wallet = _make_encrypted_wallet(tmp_path, password)
    runner = CliRunner()

    result = runner.invoke(
        app,
        [command, "-f", str(wallet), *extra_args],
        env={"MNEMONIC_PASSWORD": password},
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == expected_stdout
    assert _COMMAND_LINE_PASSWORD_WARNING not in result.stderr


@pytest.mark.parametrize("command", ["verify-password", "showseed"])
def test_password_warning_accepts_an_independent_source_enum(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    # Catch cross-library enum identity comparisons even with pre-vendoring Typer.
    class IndependentParameterSource(Enum):
        COMMANDLINE = auto()

    password = "independent-enum-secret"
    wallet = _make_encrypted_wallet(tmp_path, password)
    ctx = Mock(spec=typer.Context)
    ctx.get_parameter_source.return_value = IndependentParameterSource.COMMANDLINE

    if command == "verify-password":
        verify_password(ctx=ctx, mnemonic_file=wallet, password=password, prompt=False)
    else:
        showseed(ctx=ctx, mnemonic_file=wallet, password=password, yes=True)

    captured = capsys.readouterr()
    assert captured.err.count(_COMMAND_LINE_PASSWORD_WARNING) == 1
    assert _COMMAND_LINE_PASSWORD_WARNING not in captured.out
    assert password not in captured.err
    ctx.get_parameter_source.assert_called_once_with("password")
