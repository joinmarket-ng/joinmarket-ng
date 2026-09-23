"""CLI tests for jmwalletd."""

from __future__ import annotations

import click
from typer.testing import CliRunner

from jmwalletd.cli import _generate_self_signed_cert, app

runner = CliRunner()


def test_root_help_shows_completion_options() -> None:
    """jmwalletd CLI should expose Typer shell completion options."""
    result = runner.invoke(app, ["--help"], prog_name="jmwalletd")
    output = click.unstyle(result.stdout)

    assert result.exit_code == 0
    assert "--install-completion" in output
    assert "--show-completion" in output
    assert "Usage: jmwalletd [OPTIONS]" in output


def test_help_output_is_alphabetically_sorted() -> None:
    """Subcommands and options must be listed alphabetically in --help."""
    from jmcore.cli_help import find_unsorted_help

    assert find_unsorted_help(app) == []


def test_generate_self_signed_cert_protects_private_key(tmp_path) -> None:
    ssl_dir = tmp_path / "ssl"

    _generate_self_signed_cert(ssl_dir)

    assert ssl_dir.stat().st_mode & 0o777 == 0o700
    assert (ssl_dir / "key.pem").stat().st_mode & 0o777 == 0o600
    assert (ssl_dir / "cert.pem").exists()
