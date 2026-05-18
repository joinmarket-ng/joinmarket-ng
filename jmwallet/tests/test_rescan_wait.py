"""
Tests for ``jm-wallet rescan --wait`` progress polling.

These guard against the regression where a mainnet rescan that takes longer
than the import-RPC timeout (30 minutes) would crash the CLI even though
Bitcoin Core happily kept scanning in the background.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

from jmwallet.cli.wallet import _await_rescan_completion


class _StubBackend:
    """Minimal stand-in for DescriptorWalletBackend.get_rescan_status."""

    def __init__(self, statuses: Iterator[dict | None]) -> None:
        self._statuses = statuses
        self.get_rescan_status = AsyncMock(side_effect=self._next_status)

    async def _next_status(self) -> dict | None:
        try:
            return next(self._statuses)
        except StopIteration:
            return {"in_progress": False}


@pytest.mark.asyncio
async def test_await_rescan_completion_polls_until_done() -> None:
    statuses: Iterator[dict | None] = iter(
        [
            {"in_progress": True, "progress": 0.0, "duration": 0},
            {"in_progress": True, "progress": 0.5, "duration": 12},
            {"in_progress": True, "progress": 0.9, "duration": 30},
            {"in_progress": False},
        ]
    )
    backend = _StubBackend(statuses)
    seen: list[tuple[float, float]] = []

    await _await_rescan_completion(
        backend,
        poll_interval_seconds=0.0,
        progress_callback=lambda p, d: seen.append((p, d)),
    )

    # Final callback marks 1.0; preceding ones reflect server progress.
    assert seen[-1] == (1.0, 0.0)
    assert any(p == pytest.approx(0.5) for p, _ in seen)
    assert backend.get_rescan_status.await_count >= 3


@pytest.mark.asyncio
async def test_await_rescan_completion_retries_on_transient_error() -> None:
    statuses: Iterator[dict | None] = iter(
        [
            {"in_progress": True, "progress": 0.1, "duration": 1},
            {"in_progress": False},
        ]
    )
    backend = _StubBackend(statuses)
    failures = {"left": 2}
    real_get = backend.get_rescan_status.side_effect

    async def flaky() -> dict | None:
        if failures["left"] > 0:
            failures["left"] -= 1
            raise ConnectionError("temporary blip")
        return await real_get()

    backend.get_rescan_status.side_effect = flaky

    # Should not raise; retries swallow transient errors.
    await asyncio.wait_for(
        _await_rescan_completion(backend, poll_interval_seconds=0.0),
        timeout=2.0,
    )
    assert failures["left"] == 0
