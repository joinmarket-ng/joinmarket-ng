"""
Tests for jmcore.cli_help: alphabetically sorted ``--help`` output for Typer apps.
"""

from __future__ import annotations

import click
import typer
from typer.testing import CliRunner

from jmcore.cli_help import SortedTyper, find_unsorted_help

runner = CliRunner()


def _build_app(app: typer.Typer) -> typer.Typer:
    """Register commands and options deliberately out of alphabetical order."""

    @app.command()
    def zebra(
        zulu: str = typer.Option("z", "--zulu"),
        alpha: str = typer.Option("a", "--alpha"),
        mike: bool = typer.Option(False, "--mike/--no-mike"),
    ) -> None:
        """Zebra command."""
        typer.echo(f"{alpha}:{mike}:{zulu}")

    @app.command()
    def alpha(
        value: str = typer.Argument(...),
        second: str = typer.Argument("2nd"),
        beta: str = typer.Option("b", "--beta"),
    ) -> None:
        """Alpha command."""
        typer.echo(f"{value}:{second}:{beta}")

    return app


class TestSortedTyper:
    def test_sorted_typer_has_no_violations(self) -> None:
        app = _build_app(SortedTyper(name="demo", no_args_is_help=True))
        assert find_unsorted_help(app) == []

    def test_plain_typer_is_detected_as_unsorted(self) -> None:
        """Guard: the checker must flag a vanilla Typer app with this layout."""
        app = _build_app(typer.Typer(name="demo", no_args_is_help=True))
        violations = find_unsorted_help(app)
        assert violations, "expected vanilla Typer app to be reported as unsorted"

    def test_subcommands_listed_alphabetically(self) -> None:
        app = _build_app(SortedTyper(name="demo", no_args_is_help=True))
        result = runner.invoke(app, ["--help"], prog_name="demo")
        output = click.unstyle(result.stdout)

        assert result.exit_code == 0
        assert output.index("alpha") < output.index("zebra")

    def test_options_listed_alphabetically(self) -> None:
        app = _build_app(SortedTyper(name="demo", no_args_is_help=True))
        result = runner.invoke(app, ["zebra", "--help"], prog_name="demo")
        output = click.unstyle(result.stdout)

        assert result.exit_code == 0
        positions = [output.index(opt) for opt in ("--alpha", "--help", "--mike", "--zulu")]
        assert positions == sorted(positions)

    def test_arguments_keep_declared_order(self) -> None:
        """Positional order defines call syntax and must not be re-sorted."""
        app = _build_app(SortedTyper(name="demo", no_args_is_help=True))
        result = runner.invoke(app, ["alpha", "--help"], prog_name="demo")
        output = click.unstyle(result.stdout)

        assert result.exit_code == 0
        # VALUE was declared before SECOND; alphabetical would swap them.
        assert output.index("VALUE") < output.index("SECOND")

    def test_parsing_is_unaffected_by_sorting(self) -> None:
        app = _build_app(SortedTyper(name="demo", no_args_is_help=True))

        result = runner.invoke(app, ["alpha", "hello", "--beta", "x"])
        assert result.exit_code == 0
        assert "hello:2nd:x" in result.stdout

        result = runner.invoke(app, ["zebra", "--mike", "--zulu", "1", "--alpha", "2"])
        assert result.exit_code == 0
        assert "2:True:1" in result.stdout
