"""JoinMarket-NG TUI menu launcher.

Thin entry point that locates and execs ``scripts/menu.joinmarket-ng.sh``.
It is registered as the ``jm-ng`` console script via *jmcore*'s
``pyproject.toml``, so after ``pip install -e jmcore`` (or a full install)
users can simply run ``jm-ng`` from the terminal.

The launcher resolves the shell script in this order:

1. Relative to the repo root (editable / development installs).
2. ``$HOME/.joinmarket-ng/menu.sh`` (standalone manual installs).
3. ``$JM_NG_MENU`` environment variable override.

If ``whiptail`` is not found on ``$PATH`` the launcher exits with a
helpful message.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _find_menu_script() -> Path | None:
    """Locate the TUI shell script."""
    # 1. Environment variable override
    env_path = os.environ.get("JM_NG_MENU")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p

    # 2. Relative to repository root (development / editable installs)
    #    This file lives at jmcore/src/jmcore/tui.py  ->  repo root is ../../../../
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    candidate = repo_root / "scripts" / "menu.joinmarket-ng.sh"
    if candidate.is_file():
        return candidate

    # 3. Standard standalone install location
    home_script = Path.home() / ".joinmarket-ng" / "menu.sh"
    if home_script.is_file():
        return home_script

    return None


def main() -> None:
    """Launch the JoinMarket-NG TUI menu."""
    # Pre-flight: whiptail is required
    if not shutil.which("whiptail"):
        print(
            "Error: 'whiptail' is required but not found.\n"
            "Install it with your package manager, e.g.:\n"
            "  sudo apt install whiptail          # Debian/Ubuntu\n"
            "  sudo pacman -S libnewt              # Arch Linux",
            file=sys.stderr,
        )
        raise SystemExit(1)

    script = _find_menu_script()
    if script is None:
        print(
            "Error: Could not locate the TUI menu script.\n"
            "Expected locations:\n"
            "  - <repo>/scripts/menu.joinmarket-ng.sh  (development)\n"
            "  - ~/.joinmarket-ng/menu.sh               (standalone install)\n"
            "\n"
            "You can also set the JM_NG_MENU environment variable to the\n"
            "full path of the script.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Replace the current process with bash running the script
    os.execvp("bash", ["bash", str(script)])


if __name__ == "__main__":
    main()
