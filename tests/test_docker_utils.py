"""Tests for instance-scoped Docker Compose test isolation."""

from __future__ import annotations

from pathlib import Path

from tests.e2e import docker_utils


def test_instance_scoped_compose_override_resolution(
    tmp_path: Path, monkeypatch
) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.touch()
    override = (
        tmp_path
        / "tmp"
        / "parallel-tests"
        / "i3"
        / "docker-compose.reference-maker.override.yml"
    )
    override.parent.mkdir(parents=True)
    override.touch()
    monkeypatch.setattr(docker_utils, "get_compose_file", lambda: compose_file)
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "jmpt-i3-reference-maker")

    assert docker_utils.get_compose_override_file() == override
