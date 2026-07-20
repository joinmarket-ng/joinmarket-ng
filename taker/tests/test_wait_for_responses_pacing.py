"""Pacing of wait_for_responses when directory connections are dead.

A closed directory connection makes ``listen_for_messages`` raise immediately.
Without pacing, the wait loop degenerates into a busy loop that spins thousands
of iterations per second (and logs one error line per client per iteration)
until the timeout expires.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from _taker_test_helpers import make_directory_client
from jmcore.directory_client import DirectoryClientError


class DeadClient:
    """Simulates a client whose connection is closed: fails instantly."""

    def __init__(self) -> None:
        self.calls = 0

    async def listen_for_messages(self, duration: float) -> list[dict[str, Any]]:
        self.calls += 1
        raise DirectoryClientError("Connection closed")


@pytest.mark.asyncio
async def test_dead_directory_does_not_busy_loop() -> None:
    client = make_directory_client()
    dead = DeadClient()
    client.clients = {"dead.onion:5222": dead}

    loop = asyncio.get_event_loop()
    start = loop.time()
    responses = await client.wait_for_responses(
        expected_nicks=["J5NeverResponds"],
        expected_command="!pubkey",
        timeout=0.6,
    )
    elapsed = loop.time() - start

    assert responses == {}
    # The loop must be paced at roughly one listen window per iteration, not
    # spin freely: a busy loop would produce thousands of calls in 0.6s.
    assert dead.calls <= 3
    # It must still wait out (roughly) the full timeout for direct messages.
    assert 0.5 <= elapsed < 5.0


@pytest.mark.asyncio
async def test_no_directory_clients_does_not_busy_loop() -> None:
    client = make_directory_client()
    client.clients = {}

    loop = asyncio.get_event_loop()
    start = loop.time()
    responses = await client.wait_for_responses(
        expected_nicks=["J5NeverResponds"],
        expected_command="!pubkey",
        timeout=0.5,
    )
    elapsed = loop.time() - start

    assert responses == {}
    assert 0.4 <= elapsed < 5.0
