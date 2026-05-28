"""
Regression tests for ``install.sh`` invocation modes.

The script is documented to support three invocation styles:

1. Direct execution: ``bash install.sh`` or ``./install.sh``.
2. Piped from curl: ``curl ... | bash`` (and ``... | bash -s -- --flag``).
3. Sourced from a shell or test: ``source install.sh`` to call its helper
   functions without running the installer.

A previous fix (80458c3e) gated ``main`` behind
``[[ "${BASH_SOURCE[0]}" == "$0" ]]`` to support the sourced case but
silently broke the piped case: when bash reads the script from stdin,
``BASH_SOURCE[0]`` is empty while ``$0`` is ``bash``, so the equality
fails and ``main`` is skipped, exiting 0 with no output. This test
locks the dispatch behavior in place so future refactors do not
reintroduce the regression.

We only inspect the script's *dispatch*, not the installer body, by
overriding ``main`` after sourcing or by using ``--help`` (which exits
early). This keeps the tests hermetic: no network, no apt, no venv
creation.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _run(cmd: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_direct_invocation_runs_main() -> None:
    """``bash install.sh --help`` reaches ``main`` and prints usage."""
    result = _run(["bash", str(INSTALL_SH), "--help"])
    assert result.returncode == 0, result.stderr
    # ``main`` dispatches ``--help`` to ``print_help`` which prints the
    # documented Usage banner. Matching the banner (not the script body)
    # confirms control reached ``main`` rather than falling through.
    assert "Usage:" in result.stdout, (
        f"Direct invocation did not reach main:\n{result.stdout}\n{result.stderr}"
    )


def test_piped_invocation_runs_main() -> None:
    """``curl ... | bash -s -- --help`` reaches ``main``.

    We emulate ``curl ... | bash`` by feeding the script to ``bash`` on
    stdin. When this path was broken, ``main`` was skipped and the
    process exited 0 with empty stdout.
    """
    script = INSTALL_SH.read_text()
    result = _run(["bash", "-s", "--", "--help"], stdin=script)
    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout, (
        "Piped invocation did not reach main; install.sh likely "
        "exited 0 without running. Output was:\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )


def test_sourced_invocation_does_not_run_main() -> None:
    """``source install.sh`` exposes helpers without running ``main``.

    Sourcing must not call ``main``; tests and downstream scripts rely
    on being able to use individual helper functions. We verify by
    sourcing the script and then explicitly invoking a known helper
    (``show_help``) -- if ``main`` had also run, the help banner would
    appear twice and the marker we echo before the helper call would
    not be the first occurrence of "Usage:".
    """
    # ``set -e`` is intentionally omitted because some helpers in
    # install.sh check command availability with conditionals that
    # would otherwise trip early-exit on a missing optional binary.
    cmd = f"source {INSTALL_SH} && echo MARKER_AFTER_SOURCE && show_help"
    result = _run(["bash", "-c", cmd])
    assert result.returncode == 0, result.stderr
    # The marker must appear before any "Usage:" line; if main had run
    # during sourcing, "Usage:" would precede the marker.
    marker_idx = result.stdout.find("MARKER_AFTER_SOURCE")
    usage_idx = result.stdout.find("Usage:")
    assert marker_idx != -1, f"marker missing:\n{result.stdout}"
    assert usage_idx != -1, f"show_help did not print Usage:\n{result.stdout}"
    assert marker_idx < usage_idx, (
        "main appears to have run during 'source install.sh' "
        f"(Usage printed before marker):\n{result.stdout}"
    )
