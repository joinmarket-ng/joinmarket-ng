"""Owned node shutdown must finish before caller cancellation releases ownership."""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

import pytest

from jmswap.lnd_config import LndEnvironment
from jmswap.managed_lnd import ManagedLnd
from jmswap.managed_nodes import ManagedNodePaths, allocate_node, new_registry


class Child:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.waiting = asyncio.Event()
        self.exited = asyncio.Event()
        self.reaped = False
        self.terminate = Mock()
        self.kill = Mock()

    async def wait(self) -> int:
        self.waiting.set()
        await self.exited.wait()
        self.reaped = True
        assert self.returncode is not None
        return self.returncode

    def exit(self) -> None:
        self.returncode = 0
        self.exited.set()


def owned_child(tmp_path: Path) -> tuple[ManagedLnd, Child]:
    registry = new_registry("regtest", "ab" * 32)
    node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    paths = ManagedNodePaths.for_wallet(tmp_path, "regtest", registry.wallet_identity)
    env = LndEnvironment(
        network="regtest", lnd_binary=tmp_path / "unused", bitcoind_host="127.0.0.1:18443"
    )
    lnd = ManagedLnd(node, paths, env)
    child = Child()
    lnd._process = cast(Any, child)
    return lnd, child


async def test_repeated_cancellation_during_exit_wait_is_deferred_until_child_reaped(
    tmp_path: Path,
) -> None:
    lnd, child = owned_child(tmp_path)
    stopping = asyncio.create_task(lnd.stop())
    try:
        await asyncio.wait_for(child.waiting.wait(), timeout=3)
        for _ in range(2):
            stopping.cancel()
            await asyncio.sleep(0)
            assert not stopping.done() and not child.reaped
        child.exit()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, timeout=3)
        assert child.reaped and not lnd.running
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
    finally:
        child.exit()
        await asyncio.gather(stopping, return_exceptions=True)


async def test_cancellation_during_stop_rpc_cannot_abandon_the_owned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lnd, child = owned_child(tmp_path)
    macaroon = lnd.paths.macaroon(lnd.node)
    macaroon.parent.mkdir(parents=True)
    macaroon.write_bytes(b"unit credential")
    requested, release_rpc = asyncio.Event(), asyncio.Event()

    async def stop_rpc(*args: Any, **kwargs: Any) -> None:
        requested.set()
        await release_rpc.wait()

    rpc = AsyncMock(side_effect=stop_rpc)
    monkeypatch.setattr(lnd, "call", rpc)
    stopping = asyncio.create_task(lnd.stop())
    try:
        await asyncio.wait_for(requested.wait(), timeout=3)
        stopping.cancel()
        await asyncio.sleep(0)
        assert not stopping.done() and not child.waiting.is_set()
        release_rpc.set()
        await asyncio.wait_for(child.waiting.wait(), timeout=3)
        assert not stopping.done()
        child.exit()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, timeout=3)
        assert child.reaped and not lnd.running
        assert rpc.await_count == 1
    finally:
        release_rpc.set()
        child.exit()
        await asyncio.gather(stopping, return_exceptions=True)


async def test_normal_shutdown_waits_for_the_child_without_canceling_it(tmp_path: Path) -> None:
    lnd, child = owned_child(tmp_path)
    stopping = asyncio.create_task(lnd.stop())
    await asyncio.wait_for(child.waiting.wait(), timeout=3)
    assert not stopping.done()
    child.exit()
    await asyncio.wait_for(stopping, timeout=3)
    assert child.reaped and not lnd.running


async def test_existing_timeout_escalation_still_reaps_after_an_exit_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lnd, child = owned_child(tmp_path)

    attempts = 0

    async def wait_for_exit() -> int:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TimeoutError
        child.reaped = True
        child.returncode = 0
        return 0

    wait = AsyncMock(side_effect=wait_for_exit)
    monkeypatch.setattr(child, "wait", wait)
    child.terminate.side_effect = ProcessLookupError()
    child.kill.side_effect = ProcessLookupError()
    await lnd.stop()
    assert wait.await_count == 3
    child.terminate.assert_called_once()
    child.kill.assert_called_once()
    assert child.reaped and not lnd.running


async def test_real_child_is_reaped_before_shutdown_cancellation_returns(tmp_path: Path) -> None:
    lnd, _ = owned_child(tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    lnd._process = process
    stopping = asyncio.create_task(lnd.stop(timeout=0.05))
    try:
        await asyncio.sleep(0)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(stopping, timeout=3)
        assert process.returncode is not None and not lnd.running
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await process.wait()
        await asyncio.gather(stopping, return_exceptions=True)
