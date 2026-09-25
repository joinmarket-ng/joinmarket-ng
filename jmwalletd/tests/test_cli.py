"""CLI tests for jmwalletd."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from loguru import logger
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


@pytest.mark.parametrize(
    ("host", "warns"),
    [("127.0.0.1", False), ("0.0.0.0", True), ("::", True)],
)
def test_plain_http_network_listener_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, warns: bool
) -> None:
    """Keep proxy deployments possible, but warn when plaintext is network-facing."""
    runs: list[dict[str, object]] = []
    warnings: list[str] = []
    monkeypatch.setattr("jmcore.process_hardening.harden_current_process", lambda: None)
    monkeypatch.setattr("jmwalletd.app.create_app", lambda data_dir: object())
    monkeypatch.setattr("uvicorn.run", lambda *_args, **kwargs: runs.append(kwargs))
    sink = logger.add(warnings.append, level="WARNING")
    try:
        result = runner.invoke(
            app, ["--host", host, "--no-tls", "--data-dir", str(tmp_path)], prog_name="jmwalletd"
        )
    finally:
        logger.remove(sink)

    assert result.exit_code == 0, result.output
    assert ("Plain HTTP on" in "\n".join(warnings)) is warns
    assert len(runs) == 1
    assert runs[0]["host"] == host
    assert runs[0]["ssl_certfile"] is None


def test_generate_self_signed_cert_protects_private_key(tmp_path) -> None:
    ssl_dir = tmp_path / "ssl"

    _generate_self_signed_cert(ssl_dir)

    assert ssl_dir.stat().st_mode & 0o777 == 0o700
    assert (ssl_dir / "key.pem").stat().st_mode & 0o777 == 0o600
    assert (ssl_dir / "cert.pem").exists()
