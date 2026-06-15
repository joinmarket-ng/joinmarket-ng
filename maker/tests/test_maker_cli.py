"""CLI tests for maker CLI app."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from maker.cli import app

runner = CliRunner()


def test_root_help_shows_completion_options() -> None:
    """Maker CLI should expose Typer shell completion options."""
    result = runner.invoke(app, ["--help"], prog_name="jm-maker")
    output = click.unstyle(result.stdout)

    assert result.exit_code == 0
    assert "--install-completion" in output
    assert "--show-completion" in output


def test_build_maker_config_auto_detects_tor_cookie() -> None:
    """``build_maker_config`` must call ``detect_tor_cookie_path`` when no
    explicit cookie was provided so the maker authenticates to Tor on hosts
    that only configured the default cookie file (issue #471)."""
    import inspect

    from maker import cli as cli_module

    # The helper must be imported into the maker.cli namespace.
    assert hasattr(cli_module, "detect_tor_cookie_path")

    # And it must be called from the cookie-resolution block of
    # ``build_maker_config``. Inspecting the source keeps this independent
    # of JoinMarketSettings construction (which needs a full config.toml).
    source = inspect.getsource(cli_module.build_maker_config)
    assert "detect_tor_cookie_path()" in source
    # Make sure the auto-detect is the fallback after the explicit settings
    # branch, not a replacement for it.
    assert source.index("settings.tor.cookie_path") < source.index("detect_tor_cookie_path()")


def test_config_init_exposes_config_file_option() -> None:
    """``config-init`` must advertise the --config-file flag (#537)."""
    result = runner.invoke(app, ["config-init", "--help"], prog_name="jm-maker")
    output = click.unstyle(result.stdout)

    assert result.exit_code == 0
    assert "--config-file" in output


def test_config_init_creates_config_at_config_file_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--config-file decouples the created config from --data-dir (#537)."""
    monkeypatch.delenv("JOINMARKET_CONFIG_FILE", raising=False)
    data_dir = tmp_path / "var" / "lib" / "joinmarket"
    data_dir.mkdir(parents=True)
    config_file = tmp_path / "etc" / "joinmarket" / "config.toml"

    result = runner.invoke(
        app,
        ["config-init", "--data-dir", str(data_dir), "--config-file", str(config_file)],
        prog_name="jm-maker",
    )

    assert result.exit_code == 0, result.stdout
    assert config_file.exists()
    assert "[tor]" in config_file.read_text()
    # The data directory must not receive its own config.toml.
    assert not (data_dir / "config.toml").exists()
