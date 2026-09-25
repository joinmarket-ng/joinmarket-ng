"""Runner lifecycle checks without touching Docker or funded test fixtures."""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run-ring-e2e.sh"


@pytest.fixture
def runner_env() -> Iterator[tuple[dict[str, str], Path, Path, Path]]:
    with tempfile.TemporaryDirectory(prefix="runner-safety-", dir=ROOT / "tmp") as name:
        base = Path(name)
        fake_bin = base / "bin"
        fake_bin.mkdir()
        docker_log = base / "docker.log"
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
case "$*" in
  *'ps -aq --filter'*) [ "${FAKE_CONTAINERS:-0}" != 1 ] || echo retained ;;
  *'volume ls -q --filter'*) [ "${FAKE_VOLUMES:-0}" != 1 ] || echo retained ;;
  *'network ls -q --filter'*) [ "${FAKE_NETWORKS:-0}" != 1 ] || echo retained ;;
  *'ps --services --status running'*) printf 'lnd-bitcoin\\nlnd-opener\\nlnd-fundee\\n' ;;
  *'ps --format'*) echo Up ;;
  *'down -v'*) [ "${FAKE_DOWN_FAIL:-0}" != 1 ] ;;
esac
""",
            encoding="utf-8",
        )
        fake_docker.chmod(0o755)
        (fake_bin / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (fake_bin / "sleep").chmod(0o755)
        (fake_bin / "pytest").write_text(
            '#!/bin/sh\nexit "${FAKE_PYTEST_STATUS:-0}"\n', encoding="utf-8"
        )
        (fake_bin / "pytest").chmod(0o755)
        ring_data = base / "ring"
        external_data = base / "external"
        env = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_DOCKER_LOG": str(docker_log),
            "RING_E2E_PROJECT": "runner-safety-ring",
            "LND_EXTERNAL_PROJECT": "runner-safety-external",
            "RING_E2E_DATA_DIR": str(ring_data),
            "LND_EXTERNAL_DATA_DIR": str(external_data),
            "RING_E2E_NO_BUILD": "1",
        }
        for key in ("E2E_RESET", "RING_E2E_NO_RESET", "RING_E2E_KEEP"):
            env.pop(key, None)
        yield env, ring_data, external_data, docker_log


def run(env: dict[str, str], suite: str = "ring") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(RUNNER), suite],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("retained", ["host", "volume", "container", "network"])
def test_retained_state_is_not_automatically_deleted(
    runner_env: tuple[dict[str, str], Path, Path, Path], retained: str
) -> None:
    env, ring_data, _, log = runner_env
    if retained == "host":
        ring_data.mkdir()
        (ring_data / ".legacy-journal").write_text("unknown", encoding="utf-8")
    else:
        env[f"FAKE_{retained.upper()}S"] = "1"

    result = run(env)

    assert result.returncode != 0
    assert "retained E2E state" in result.stderr
    assert "down -v" not in log.read_text(encoding="utf-8")
    assert " run " not in log.read_text(encoding="utf-8")
    if retained == "host":
        assert (ring_data / ".legacy-journal").read_text(encoding="utf-8") == "unknown"


def test_inspection_error_refuses_unknown_state(
    runner_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, ring_data, _, log = runner_env
    env["FAKE_INSPECTION_FAIL"] = "1"
    fake_docker = Path(env["PATH"].split(":", maxsplit=1)[0]) / "docker"
    script = fake_docker.read_text(encoding="utf-8").replace(
        "  *'ps -aq --filter'*) [",
        "  *'ps -aq --filter'*) [ \"${FAKE_INSPECTION_FAIL:-0}\" != 1 ] || exit 4; [",
    )
    fake_docker.write_text(script, encoding="utf-8")

    result = run(env)

    assert result.returncode != 0
    assert "cannot inspect existing E2E state" in result.stderr
    assert not ring_data.exists()
    assert "down -v" not in log.read_text(encoding="utf-8")


def test_failed_test_preserves_new_fixture_and_original_exit_code(
    runner_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, _, external_data, log = runner_env
    env["FAKE_PYTEST_STATUS"] = "23"

    result = run(env, "lnd-external")

    assert result.returncode == 23
    assert (external_data / ".e2e-run.json").exists()
    assert "down -v" not in log.read_text(encoding="utf-8")


def test_success_only_cleans_its_own_new_fixture(
    runner_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, _, external_data, log = runner_env

    result = run(env, "lnd-external")

    assert result.returncode == 0
    assert (
        external_data / ".e2e-run.json"
    ).exists()  # Fake Docker cannot remove host data.
    commands = log.read_text(encoding="utf-8")
    assert (
        commands.index("up -d") < commands.index("down -v") < commands.index("run --rm")
    )


def test_cleanup_failure_keeps_host_data(
    runner_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, _, external_data, log = runner_env
    env["FAKE_DOWN_FAIL"] = "1"

    result = run(env, "lnd-external")

    assert result.returncode != 0
    assert (external_data / ".e2e-run.json").exists()
    assert "E2E cleanup incomplete" in result.stderr
    assert "down -v" in log.read_text(encoding="utf-8")
    assert "run --rm" not in log.read_text(encoding="utf-8")


def test_failed_explicit_reset_never_wipes_host_state(
    runner_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, ring_data, _, log = runner_env
    ring_data.mkdir()
    retained = ring_data / "journal"
    retained.write_text("preserve", encoding="utf-8")
    env.update(E2E_RESET="1", FAKE_DOWN_FAIL="1")

    result = run(env)

    assert result.returncode != 0
    assert retained.read_text(encoding="utf-8") == "preserve"
    assert "down -v" in log.read_text(encoding="utf-8")
    assert " run " not in log.read_text(encoding="utf-8")


def test_all_preflights_both_targets_before_mutation(
    runner_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, ring_data, external_data, log = runner_env
    external_data.mkdir()

    result = run(env, "all")

    assert result.returncode != 0
    assert "retained E2E state" in result.stderr
    assert not ring_data.exists()
    assert "up -d" not in log.read_text(encoding="utf-8")


def test_retained_replay_flag_is_rejected_without_inspection(
    runner_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, _, _, log = runner_env
    env["RING_E2E_NO_RESET"] = "1"

    result = run(env)

    assert result.returncode == 2
    assert "cannot establish" in result.stderr
    assert not log.exists()


def test_no_taker_ring_refuses_retained_state_without_mutation(
    runner_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, ring_data, _, log = runner_env
    ring_data.mkdir()
    (ring_data / "signed-journal").write_text("preserve", encoding="utf-8")

    result = run(env, "ring-no-taker")

    assert result.returncode != 0
    assert "retained E2E state" in result.stderr
    assert (ring_data / "signed-journal").read_text(encoding="utf-8") == "preserve"
    assert "down -v" not in log.read_text(encoding="utf-8")


def test_no_taker_ring_rejects_replay_without_docker_access(
    runner_env: tuple[dict[str, str], Path, Path, Path],
) -> None:
    env, _, _, log = runner_env
    env["RING_E2E_NO_RESET"] = "1"

    result = run(env, "ring-no-taker")

    assert result.returncode == 2
    assert "cannot establish" in result.stderr
    assert not log.exists()


def test_no_taker_config_never_queries_taker_lnd(tmp_path: Path) -> None:
    from tests.e2e.test_cofunded_ring_e2e import channel_ring_config

    with patch(
        "tests.e2e.test_cofunded_ring_e2e.onion_endpoint",
        side_effect=AssertionError("unexpected taker LND lookup"),
    ):
        config = channel_ring_config(tmp_path, with_taker_node=False)

    assert config.enabled and not config.taker_joins
    assert not config.nodes and not config.mixdepth_nodes
