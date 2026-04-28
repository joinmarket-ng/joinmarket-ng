"""Build hook that stamps the resolved git commit into the wheel.

This keeps the rest of the build configuration in ``pyproject.toml``.

The hook writes ``src/jmcore/_build_info.py`` containing the short commit
hash and the build reference (branch/tag) used to build the package.
At runtime ``jmcore.version.get_commit_hash`` reads this file first so
that non-editable installs (``pip install git+https://...``, Docker,
release wheels) can still report the commit they were built from.

Resolution order for the commit hash, first match wins:

1. ``JOINMARKET_BUILD_COMMIT`` environment variable -- set by
   ``install.sh`` so we don't need git inside the build sandbox.
2. ``git rev-parse --short HEAD`` run from the source tree being built.
3. Skipped (no file written); runtime falls back to live ``git`` lookup
   for editable installs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from setuptools import setup


def _resolve_commit() -> tuple[str, str]:
    """Return (short_commit, ref) using env or git, both possibly empty."""
    commit = os.environ.get("JOINMARKET_BUILD_COMMIT", "").strip()
    ref = os.environ.get("JOINMARKET_BUILD_REF", "").strip()

    # Always normalize to a 7-char short hash so the menu width stays
    # predictable. Long hashes from CI (`${{ github.sha }}`) and short
    # hashes from `git rev-parse --short` therefore display the same.
    if commit:
        commit_lower = commit.lower()
        if all(c in "0123456789abcdef" for c in commit_lower):
            commit = commit_lower[:7]

    if not commit:
        try:
            result = subprocess.run(  # noqa: S603 - input is constant
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                cwd=Path(__file__).parent,
            )
            if result.returncode == 0:
                commit = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    if not ref and commit:
        # Best-effort: detect tag pointing at HEAD (release wheel) or the
        # current branch name. Detached-HEAD installs (pip git+@<commit>)
        # leave this empty, which is fine -- the runtime treats an empty
        # ref as "unknown".
        try:
            tag = subprocess.run(  # noqa: S603
                ["git", "tag", "--points-at", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
                cwd=Path(__file__).parent,
            )
            if tag.returncode == 0 and tag.stdout.strip():
                ref = tag.stdout.strip().splitlines()[0]
            else:
                branch = subprocess.run(  # noqa: S603
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    cwd=Path(__file__).parent,
                )
                if branch.returncode == 0:
                    candidate = branch.stdout.strip()
                    if candidate and candidate != "HEAD":
                        ref = candidate
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            pass

    return commit, ref


def _write_build_info() -> None:
    commit, ref = _resolve_commit()
    if not commit and not ref:
        # Nothing to record. Leave any pre-existing file in place: a wheel
        # that was previously stamped should not be silently invalidated by
        # a rebuild without git or the env variable.
        return

    target = Path(__file__).parent / "src" / "jmcore" / "_build_info.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '"""Auto-generated at build time. Do not edit manually."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        f"COMMIT = {commit!r}\n"
        f"REF = {ref!r}\n",
        encoding="utf-8",
    )


_write_build_info()
setup()
