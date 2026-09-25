from __future__ import annotations

from pathlib import Path

import yaml


def test_ring_maker_deadlines_allow_phase_timeout_and_cancellation() -> None:
    compose_path = (
        Path(__file__).resolve().parents[1] / "jmswap/docker-compose.ring-e2e.yml"
    )
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    for name in ("ring-maker1", "ring-maker2", "ring-maker3"):
        environment = compose["services"][name]["environment"]
        phase_timeout = int(environment["MAKER__CHANNEL_RING__PHASE_TIMEOUT_SECONDS"])
        assert int(environment["MAKER__PRE_SIGN_TIMEOUT_SEC"]) > 2 * phase_timeout
        assert int(environment["MAKER__SESSION_TIMEOUT_SEC"]) > 2 * phase_timeout
