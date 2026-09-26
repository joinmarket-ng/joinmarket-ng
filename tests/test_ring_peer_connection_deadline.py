"""The ring harness must bound blocking lncli calls by its advertised deadline."""

from __future__ import annotations

import subprocess
import time
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from tests.e2e import test_cofunded_ring_e2e as ring


async def test_connected_peer_does_not_dial(monkeypatch: pytest.MonkeyPatch) -> None:
    lncli = Mock(return_value={"peers": [{"pub_key": "peer"}]})
    monkeypatch.setattr(ring, "lncli", lncli)
    await ring.ensure_peer_connected("node", "peer", "unused.onion:9735", timeout=1)
    assert lncli.call_count == 1
    assert lncli.call_args.args == ("node", "listpeers")
    assert 0 < lncli.call_args.kwargs["timeout"] <= 1


async def test_no_command_starts_after_peer_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lncli = Mock()
    monkeypatch.setattr(ring, "lncli", lncli)
    with pytest.raises(RuntimeError, match="attempts=0"):
        await ring.ensure_peer_connected("node", "peer", "unused.onion:9735", timeout=0)
    lncli.assert_not_called()


async def test_successful_dial_still_requires_the_exact_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lncli = Mock(
        side_effect=[
            {"peers": [{"pub_key": "other"}]},
            {},
            {"peers": [{"pub_key": "peer"}]},
        ]
    )
    monkeypatch.setattr(ring, "lncli", lncli)
    monkeypatch.setattr(ring.asyncio, "sleep", AsyncMock())
    await ring.ensure_peer_connected("node", "peer", "unused.onion:9735", timeout=10)
    assert [call.args[1] for call in lncli.call_args_list] == [
        "listpeers",
        "connect",
        "listpeers",
    ]


async def test_stalled_peer_lookup_cannot_extend_deadline_or_start_dial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def lncli(service: str, command: str, *, timeout: float = 180) -> dict[str, Any]:
        assert command == "listpeers"
        assert 0 < timeout <= 0.05
        time.sleep(timeout)
        raise subprocess.TimeoutExpired("lncli listpeers", timeout)

    monkeypatch.setattr(ring, "lncli", lncli)
    with pytest.raises(RuntimeError, match="attempts=0, last_exception=TimeoutExpired"):
        await ring.ensure_peer_connected(
            "node", "peer", "unused.onion:9735", timeout=0.05
        )


async def test_stalled_connect_has_remaining_budget_not_default_compose_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[float] = []

    def lncli(
        service: str, command: str, *args: str, timeout: float = 180
    ) -> dict[str, Any]:
        assert 0 < timeout <= 0.2
        if command == "listpeers":
            return {"peers": []}
        assert command == "connect"
        # LND's server-side dial must also be bounded; killing docker exec alone
        # need not stop the outstanding dial inside the node.
        assert args[0:1] == ("--timeout",)
        assert args[1].endswith("s")
        assert 0 < float(args[1][:-1]) <= 1
        seen.append(timeout)
        time.sleep(timeout)
        raise subprocess.TimeoutExpired("lncli connect", timeout)

    monkeypatch.setattr(ring, "lncli", lncli)
    start = time.monotonic()
    with pytest.raises(RuntimeError, match="attempts=1, last_exception=TimeoutExpired"):
        await ring.ensure_peer_connected(
            "node", "peer", "unused.onion:9735", timeout=0.2
        )
    assert time.monotonic() - start < 2
    assert len(seen) == 1


def test_lncli_propagates_timeout_to_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="{}"))
    monkeypatch.setattr(ring.subprocess, "run", run)
    assert ring.lncli("node", "listpeers", timeout=0.25) == {}
    assert run.call_args.kwargs["timeout"] == 0.25
