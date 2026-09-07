"""Regression tests for wallet CLI password hardening."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from jmwallet.cli import app
from jmwallet.cli.mnemonic import encrypt_mnemonic

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
