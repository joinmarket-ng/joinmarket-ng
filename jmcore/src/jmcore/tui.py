"""JoinMarket-NG TUI menu launcher.

Thin entry point that locates and execs ``menu.joinmarket-ng.sh``.
It is registered as the ``jm-ng`` console script via *jmcore*'s
``pyproject.toml``, so after ``pip install jmcore`` users can simply
run ``jm-ng`` from the terminal.

The launcher resolves the shell script in this order:

1. ``$JM_NG_MENU`` environment variable override.
2. Package data shipped inside the *jmcore* wheel
   (``jmcore/data/menu.joinmarket-ng.sh``).
3. Relative to the repo root (editable / development installs).
4. ``$HOME/.joinmarket-ng/menu.sh`` (standalone manual installs).

If ``whiptail`` is not found on ``$PATH`` the launcher exits with a
helpful message.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from importlib import resources
from pathlib import Path

from jmcore.version import exit_if_version_requested


def _find_menu_script() -> Path | None:
    """Locate the TUI shell script."""
    # 1. Environment variable override
    env_path = os.environ.get("JM_NG_MENU")
    if env_path:
        p = Path(env_path)
        if p.is_file():
            return p

    # 2. Package data (works for non-editable pip installs / wheels)
    try:
        ref = resources.files("jmcore").joinpath("data/menu.joinmarket-ng.sh")
        # resources.files() returns a Traversable.  For files inside a
        # wheel / installed package this is a real filesystem path; for
        # zip-imported packages it may need extraction via as_file().
        # We try the fast path first (real path).
        p = Path(str(ref))
        if p.is_file():
            return p
    except (TypeError, FileNotFoundError, ModuleNotFoundError):
        pass

    # 3. Relative to repository root (development / editable installs)
    #    This file lives at jmcore/src/jmcore/tui.py  ->  repo root is ../../../../
    repo_root = Path(__file__).resolve().parent.parent.parent.parent
    candidate = repo_root / "scripts" / "menu.joinmarket-ng.sh"
    if candidate.is_file():
        return candidate

    # 4. Standard standalone install location
    from jmcore.paths import get_default_data_dir

    home_script = get_default_data_dir() / "menu.sh"
    if home_script.is_file():
        return home_script

    return None


def main() -> None:
    """Launch the JoinMarket-NG TUI menu."""
    exit_if_version_requested(sys.argv[1:])

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
            "  - jmcore package data (pip install jmcore)\n"
            "  - <repo>/scripts/menu.joinmarket-ng.sh  (development)\n"
            "  - ~/.joinmarket-ng/menu.sh               (standalone install)\n"
            "\n"
            "You can also set the JM_NG_MENU environment variable to the\n"
            "full path of the script.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Replace the current process with bash running the script, pinned to this
    # installation (interpreter and console scripts as a pair) so a stale
    # default venv cannot supply older helpers. Without its console scripts the
    # menu keeps its own venv/PATH discovery.
    env = {k: v for k, v in os.environ.items() if k not in ("JM_NG_PYTHON", "JM_NG_SCRIPTS")}
    scripts = _installation_scripts_dir()
    if scripts is not None:
        env.update(JM_NG_PYTHON=sys.executable, JM_NG_SCRIPTS=str(scripts))
    os.execvpe("bash", ["bash", str(script)], env)


def _installation_scripts_dir() -> Path | None:
    """Return the console-script directory paired with this launcher."""
    candidates = [Path(sys.argv[0]).resolve().parent, Path(sysconfig.get_path("scripts"))]
    return next((path for path in candidates if (path / "jm-wallet").is_file()), None)


if __name__ == "__main__":
    main()
