"""
Utilities for Docker container name resolution and compose commands.

Supports parallel test execution by reading ``JM_CONTAINER_PREFIX`` and
``COMPOSE_PROJECT_NAME`` from the environment.  When these are not set,
the default ``jm-`` prefix and no project name are used, preserving
backwards compatibility with single-suite runs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from loguru import logger


def get_container_prefix() -> str:
    """Return the container-name prefix for the current test suite.

    Reads ``JM_CONTAINER_PREFIX`` from the environment; defaults to ``"jm"``
    so that the resulting container names match the docker-compose defaults
    (e.g. ``jm-maker1``).
    """
    return os.environ.get("JM_CONTAINER_PREFIX", "jm")


def get_container_name(service: str) -> str:
    """Resolve a Docker container name for *service*.

    Examples (default prefix ``jm``)::

        get_container_name("maker1")   -> "jm-maker1"
        get_container_name("neutrino") -> "jm-neutrino"

    When ``JM_CONTAINER_PREFIX=jm-e2e``::

        get_container_name("maker1")   -> "jm-e2e-maker1"
    """
    prefix = get_container_prefix()
    return f"{prefix}-{service}"


def get_compose_file() -> Path:
    """Return the path to the project ``docker-compose.yml``."""
    return Path(__file__).parent.parent.parent / "docker-compose.yml"


def get_compose_override_file() -> Path | None:
    """Return an override compose file path when running in parallel mode.

    Resolution order:
    1. ``JM_COMPOSE_OVERRIDE_FILE`` environment variable
    2. Derived from ``COMPOSE_PROJECT_NAME=jmpt-<suite>`` as
       ``tmp/parallel-tests/docker-compose.<suite>.override.yml``

    Returns ``None`` when no valid override file is found.
    """
    env_override = os.environ.get("JM_COMPOSE_OVERRIDE_FILE")
    if env_override:
        override_path = Path(env_override)
        if override_path.exists():
            return override_path

    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if project.startswith("jmpt-"):
        suite = project.removeprefix("jmpt-")
        derived_override = (
            get_compose_file().parent
            / "tmp"
            / "parallel-tests"
            / f"docker-compose.{suite}.override.yml"
        )
        if derived_override.exists():
            return derived_override

    return None


def get_compose_cmd_prefix() -> list[str]:
    """Build the ``docker compose -f ... [-p ...]`` prefix list.

    Includes ``-p <project>`` when ``COMPOSE_PROJECT_NAME`` is set so that
    compose commands target the correct isolated project.
    """
    cmd = ["docker", "compose", "-f", str(get_compose_file())]
    override_file = get_compose_override_file()
    if override_file is not None:
        cmd += ["-f", str(override_file)]

    project = os.environ.get("COMPOSE_PROJECT_NAME")
    if project:
        cmd += ["-p", project]
    return cmd


def run_compose_cmd(
    args: list[str], check: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a ``docker compose`` command with project isolation support."""
    cmd = get_compose_cmd_prefix() + args
    logger.debug(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def run_container_cmd(
    service: str, args: list[str], timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    """Run a command inside a compose *service* via ``docker compose exec``."""
    cmd = get_compose_cmd_prefix() + ["exec", "-T", service] + args
    logger.debug(f"Running in {service}: {' '.join(args)}")
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def docker_inspect_running(container: str) -> bool:
    """Return True if *container* is in a running state."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.stdout.strip() == "true"
    except (
        subprocess.TimeoutExpired,
        FileNotFoundError,
        subprocess.CalledProcessError,
    ):
        return False


def docker_exec(container: str, args: list[str], timeout: int = 10) -> str | None:
    """Run a command in *container* and return stdout, or ``None`` on failure."""
    try:
        result = subprocess.run(
            ["docker", "exec", container] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def docker_cp(src: str, dst: str, timeout: int = 10) -> bool:
    """Copy a file from a container using ``docker cp``."""
    try:
        result = subprocess.run(
            ["docker", "cp", src, dst],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def get_neutrino_port() -> int:
    """Return the neutrino API port from ``NEUTRINO_URL`` or default 8334."""
    url = os.environ.get("NEUTRINO_URL", "")
    if url:
        # Parse port from URL like "https://127.0.0.1:8334"
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if parsed.port:
                return parsed.port
        except Exception:
            pass
    return 8334


def get_directory_port() -> int:
    """Return the directory server port, default 5222."""
    # Could be derived from env vars if needed in the future
    return int(os.environ.get("DIRECTORY_PORT", "5222"))


def get_bitcoin_rpc_port() -> int:
    """Return the Bitcoin RPC port from ``BITCOIN_RPC_URL`` or default 18443."""
    url = os.environ.get("BITCOIN_RPC_URL", "")
    if url:
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            if parsed.port:
                return parsed.port
        except Exception:
            pass
    return 18443
