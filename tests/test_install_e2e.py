"""
End-to-end smoke tests for ``install.sh``.

The installer is run inside a clean container (Debian or Ubuntu) that
matches the conditions in which we saw it fail in the wild: minimal
images without ``curl``, ``gnupg``, or ``sudo`` preinstalled. The
container then runs ``jm-wallet --help`` to prove the resulting venv
is usable.

These tests need Docker. They are marked ``docker`` so the default
``pytest -m 'not docker'`` filter skips them locally. CI runs them in
a dedicated job.

We do NOT replicate every dependency check here. The unit-level
behavior (e.g. "sudo missing -> actionable error") is exercised
implicitly: if the script could not auto-install ``curl``/``gnupg`` or
fell back into an interactive prompt, the container would either fail
or hang past the per-test timeout.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_DIR = REPO_ROOT / "tests" / "install"

# Image tag prefix scoped to this test module so parallel CI runs and
# local re-runs do not collide on a single tag. We include the workflow
# / process id to keep collisions improbable without leaking secrets.
_TAG_NS = f"joinmarket-ng/install-smoke-{os.getpid()}"


def _docker_available() -> bool:
    """Best-effort check that we can talk to a Docker daemon."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="docker not available"),
]


def _build_and_run(
    dockerfile: str, tag_suffix: str
) -> subprocess.CompletedProcess[str]:
    """Build the named Dockerfile and run the container.

    The build context is the repository root so ``COPY install.sh`` and
    ``COPY tests/install/...`` resolve. The container's default ``CMD``
    runs the smoke script, so ``docker run`` returns when the test is
    done.
    """
    tag = f"{_TAG_NS}-{tag_suffix}"
    build = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(INSTALL_DIR / dockerfile),
            "-t",
            tag,
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert build.returncode == 0, (
        f"docker build failed for {dockerfile}:\n{build.stdout}\n{build.stderr}"
    )
    try:
        run = subprocess.run(
            ["docker", "run", "--rm", tag],
            capture_output=True,
            text=True,
            # The full install pulls Python deps from PyPI / git, which
            # can be slow on a cold runner. 10 minutes is generous but
            # finite so a hung interactive prompt still fails the test.
            timeout=600,
        )
    finally:
        # Always free the image so re-runs do not bloat the local cache.
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, check=False)
    return run


def _assert_install_succeeded(result: subprocess.CompletedProcess[str]) -> None:
    """Common assertions for a successful install-smoke run.

    We require both the marker emitted by ``run_install_smoke.sh`` AND
    a zero exit code. The marker check guards against the unlikely
    scenario where a later layer of the script overwrites ``$?`` after
    a real failure.
    """
    combined = result.stdout + result.stderr
    assert "INSTALL_SMOKE_PASS" in combined, (
        f"smoke marker missing (exit={result.returncode}):\n{combined[-4000:]}"
    )
    assert result.returncode == 0, (
        f"install smoke exited {result.returncode}:\n{combined[-4000:]}"
    )


@pytest.mark.timeout(900)
def test_install_on_debian_stable() -> None:
    """``install.sh`` produces a working ``jm-wallet`` on Debian stable."""
    result = _build_and_run("Dockerfile.debian", "debian")
    _assert_install_succeeded(result)


@pytest.mark.timeout(900)
def test_install_on_ubuntu_2404() -> None:
    """``install.sh`` produces a working ``jm-wallet`` on Ubuntu 24.04."""
    result = _build_and_run("Dockerfile.ubuntu", "ubuntu")
    _assert_install_succeeded(result)
