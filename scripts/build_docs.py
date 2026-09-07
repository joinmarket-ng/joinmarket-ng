#!/usr/bin/env python3
"""Reproduce the ProperDocs Pages build job locally.

This script mirrors `.github/workflows/properdocs-pages.yml`:

1. Install docs dependencies from `requirements-docs.txt`
2. Install editable project packages needed for API docs generation
3. Run `properdocs build --strict -f properdocs.yml`

It runs all commands via the current Python interpreter so the behavior is
consistent inside a virtualenv and in CI-like local environments.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EDITABLE_PACKAGES = [
    "jmcore",
    "jmwallet",
    "taker",
    "maker",
    "directory_server",
    "orderbook_watcher",
    "jmwalletd",
    "tumbler",
]


def _run(command: list[str]) -> None:
    """Run a command and fail fast on non-zero exit status."""
    subprocess.run(command, cwd=ROOT, check=True)


def main() -> None:
    """Execute the same docs build steps as GitHub workflow."""
    python = sys.executable

    print("=" * 60)
    print("Reproducing GitHub ProperDocs build workflow")
    print("=" * 60)

    print("Installing docs dependencies...")
    _run([python, "-m", "pip", "install", "-r", "requirements-docs.txt"])

    print("Installing editable project packages...")
    editable_args = [str(ROOT / package) for package in EDITABLE_PACKAGES]
    editable_install_cmd = [python, "-m", "pip", "install"]
    for editable_path in editable_args:
        editable_install_cmd.extend(["-e", editable_path])
    _run(editable_install_cmd)

    print("Building documentation with ProperDocs...")
    _run([python, "-m", "properdocs", "build", "--strict", "-f", "properdocs.yml"])

    print("=" * 60)
    print("Build complete: site/")
    print("=" * 60)


if __name__ == "__main__":
    main()
