"""Version options at every installed console entry point."""

from __future__ import annotations

import importlib
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from click import unstyle
from typer import rich_utils
from typer.testing import CliRunner

from jmcore.version import get_version


@pytest.mark.parametrize(
    ("module_name", "entrypoint", "startup_symbols"),
    [
        ("jmcore.tui", "main", ["shutil.which", "_find_menu_script", "os.execvpe"]),
        (
            "directory_server.main",
            "main",
            ["run_server", "asyncio.run", "get_settings"],
        ),
        ("directory_server.cli", "main", ["get_settings", "setup_logging", "urlopen"]),
        (
            "orderbook_watcher.main",
            "main",
            ["run_watcher", "asyncio.run", "get_settings"],
        ),
        (
            "orderbook_watcher.main",
            "main_deprecated",
            ["run_watcher", "asyncio.run", "get_settings"],
        ),
    ],
)
def test_version_bypasses_startup(
    module_name: str,
    entrypoint: str,
    startup_symbols: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = importlib.import_module(module_name)
    monkeypatch.setattr("sys.argv", ["jm-command", "--version"])
    mocks = []
    for symbol in startup_symbols:
        mock = Mock(side_effect=AssertionError("Version must not initialize services"))
        monkeypatch.setattr(f"{module_name}.{symbol}", mock)
        mocks.append(mock)
    with pytest.raises(SystemExit) as exc:
        getattr(module, entrypoint)()
    assert exc.value.code == 0
    output = capsys.readouterr()
    assert output.out == f"JoinMarket NG {get_version()}\n"
    assert ("deprecated" in output.err) is (entrypoint == "main_deprecated")
    for mock in mocks:
        mock.assert_not_called()


@pytest.mark.parametrize(
    "module_name",
    ["jmwallet.cli", "maker.cli", "taker.cli", "tumbler.cli", "jmwalletd.cli"],
)
@pytest.mark.parametrize("force_terminal", [False, True], ids=["plain", "colored"])
def test_typer_version_option(
    module_name: str, force_terminal: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rich_utils, "FORCE_TERMINAL", force_terminal)
    app = importlib.import_module(module_name).app
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"JoinMarket NG {get_version()}\n"

    help_result = CliRunner().invoke(app, ["--help"])
    assert help_result.exit_code == 0, help_result.output
    assert "--version" in unstyle(help_result.stdout)


@pytest.mark.parametrize(
    "command",
    [
        "jm-ng",
        "jm-wallet",
        "jm-maker",
        "jm-taker",
        "jm-tumbler",
        "jmwalletd",
        "jm-directory-server",
        "jm-directory-ctl",
        "jm-orderbook-watcher",
        "orderbook-watcher",
    ],
)
def test_console_version_without_configuration(command: str, tmp_path: Path) -> None:
    """The real console scripts must exit without initializing local state."""
    data_dir = tmp_path / "data"
    config_file = tmp_path / "config.toml"
    config_file.write_text("not valid TOML [")
    env = {
        **os.environ,
        "JOINMARKET_DATA_DIR": str(data_dir),
        "JOINMARKET_CONFIG_FILE": str(config_file),
    }
    result = subprocess.run(
        [command, "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"JoinMarket NG {get_version()}\n"
    assert not data_dir.exists()
    assert config_file.read_text() == "not valid TOML ["
