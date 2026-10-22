"""Published images must deliver `docker stop`'s SIGTERM to the command they run."""

from __future__ import annotations

import os
import subprocess

import pytest

pytestmark = pytest.mark.docker

IMAGE_ENV_VARS = (
    "DIRECTORY_SERVER_IMAGE",
    "ORDERBOOK_WATCHER_IMAGE",
    "MAKER_IMAGE",
    "TAKER_IMAGE",
    "JMWALLETD_IMAGE",
)


@pytest.mark.parametrize("image_env", IMAGE_ENV_VARS)
def test_docker_stop_terminates_command_with_sigterm(image_env: str) -> None:
    image = os.environ.get(image_env, "")
    if not image:
        pytest.fail(f"Set {image_env} to the image to test")

    # `sleep` installs no SIGTERM handler. As a bare PID 1 it would ignore SIGTERM
    # and be SIGKILLed (exit 137) after the stop timeout; under an init it dies
    # from the forwarded SIGTERM (exit 128 + 15).
    started = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--pull=never",
            "--network=none",
            image,
            "sleep",
            "300",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    container = started.stdout.strip()
    try:
        subprocess.run(
            ["docker", "stop", "--time", "20", container],
            capture_output=True,
            timeout=60,
            check=True,
        )
        exit_code = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.ExitCode}}", container],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout.strip()
        assert exit_code == "143"
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container],
            capture_output=True,
            timeout=60,
            check=False,
        )
