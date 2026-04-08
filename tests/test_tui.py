from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "menu.joinmarket-ng.sh"


# ---------------------------------------------------------------------------
# Shell script tests
# ---------------------------------------------------------------------------


def test_tui_script_exists() -> None:
    assert SCRIPT_PATH.is_file()


def test_tui_script_is_valid_bash() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT_PATH)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_tui_script_has_environment_detection() -> None:
    """The unified script must detect Raspiblitz vs standalone."""
    content = SCRIPT_PATH.read_text()
    assert "RASPIBLITZ=" in content
    assert "bonus.joinmarket-ng.sh" in content


def test_tui_script_has_stop_maker_helper() -> None:
    """The script must include the stop_maker helper for standalone mode."""
    content = SCRIPT_PATH.read_text()
    assert "stop_maker()" in content


def test_tui_script_has_display_send_status() -> None:
    """The script must include the display_send_status UX helper."""
    content = SCRIPT_PATH.read_text()
    assert "display_send_status()" in content


def test_tui_script_has_wallet_name_validation() -> None:
    """Wallet name inputs must be validated against directory traversal."""
    content = SCRIPT_PATH.read_text()
    assert "^[A-Za-z0-9._-]+$" in content


def test_tui_script_has_fee_rate_validation() -> None:
    """Fee rate must be validated as numeric when provided."""
    content = SCRIPT_PATH.read_text()
    assert "Fee rate must be a numeric value" in content


def test_tui_script_has_address_validation() -> None:
    """Destination address must be validated against basic bitcoin address format."""
    content = SCRIPT_PATH.read_text()
    assert "does not look like a valid Bitcoin address" in content


def test_tui_script_has_history_role_validation() -> None:
    """History role filter must be validated (maker/taker or empty)."""
    content = SCRIPT_PATH.read_text()
    assert "maker|taker)" in content


def test_tui_script_has_sed_escaping() -> None:
    """set_config_value must escape sed metacharacters."""
    content = SCRIPT_PATH.read_text()
    assert "sed -e 's/[&\\\\/|]/\\\\&/g'" in content or "escape" in content.lower()


# ---------------------------------------------------------------------------
# Python entry point tests
# ---------------------------------------------------------------------------


def test_tui_module_importable() -> None:
    from jmcore import tui  # noqa: F401


def test_tui_find_menu_script_finds_repo_script() -> None:
    from jmcore.tui import _find_menu_script

    found = _find_menu_script()
    assert found is not None
    assert found.name == "menu.joinmarket-ng.sh"


def test_tui_main_exits_without_whiptail() -> None:
    """When whiptail is missing, main() should exit with code 1."""
    from jmcore.tui import main

    with patch("shutil.which", return_value=None):
        with pytest.raises(SystemExit, match="1"):
            main()
