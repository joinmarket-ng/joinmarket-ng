from __future__ import annotations

import subprocess
from pathlib import Path


def test_raspiblitz_tui_script_exists() -> None:
    script_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "menu.joinmarket-ng.sh"
    )
    assert script_path.is_file()


def test_raspiblitz_tui_script_is_valid_bash() -> None:
    script_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "menu.joinmarket-ng.sh"
    )
    result = subprocess.run(
        ["bash", "-n", str(script_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
