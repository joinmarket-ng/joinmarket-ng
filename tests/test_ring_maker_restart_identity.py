"""Fixture restarts must replace, not silently drop, the expected maker identity."""

from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, Mock

import pytest

from tests.e2e import test_cofunded_ring_e2e as ring


@pytest.mark.asyncio
async def test_waits_for_restarted_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    compose = Mock(
        side_effect=[
            subprocess.CompletedProcess([], 0, stdout="old-nick\n"),
            subprocess.CalledProcessError(1, "cat"),
            subprocess.CompletedProcess([], 0, stdout="\n"),
            subprocess.CompletedProcess([], 0, stdout="new-nick\n"),
        ]
    )
    monkeypatch.setattr(ring, "compose", compose)
    sleep = AsyncMock()
    monkeypatch.setattr(ring.asyncio, "sleep", sleep)
    assert (
        await ring.wait_for_maker_nick("ring-maker2", previous="old-nick") == "new-nick"
    )
    assert compose.call_count == 4
    assert sleep.await_count == 3
    compose.assert_called_with(
        "exec",
        "-T",
        "ring-maker2",
        "cat",
        "/home/jm/.joinmarket-ng/state/maker_taproot.nick",
    )


@pytest.mark.asyncio
async def test_stale_identity_cannot_satisfy_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    compose = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="old-nick\n"))
    monkeypatch.setattr(ring, "compose", compose)
    with pytest.raises(RuntimeError, match="current process identity"):
        await ring.wait_for_maker_nick("ring-maker4", previous="old-nick", timeout=0)


@pytest.mark.asyncio
async def test_current_identity_before_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    compose = Mock(
        return_value=subprocess.CompletedProcess([], 0, stdout="current-nick\n")
    )
    monkeypatch.setattr(ring, "compose", compose)
    assert await ring.wait_for_maker_nick("ring-maker2") == "current-nick"
