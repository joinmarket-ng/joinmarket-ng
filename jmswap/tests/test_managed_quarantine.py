"""Quarantine intent orders launches but never attests an existing process's policy."""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey

from jmswap import managed_quarantine
from jmswap.lnd_config import LndEnvironment, render_lnd_conf
from jmswap.managed_lnd import ManagedLnd
from jmswap.managed_nodes import (
    ManagedNode,
    ManagedNodeError,
    ManagedNodePaths,
    NodeState,
    RegistryStore,
    allocate_node,
    new_registry,
)
from jmswap.managed_quarantine import (
    QuarantineState,
    managed_launch_policy,
    quarantine_state,
    request_quarantine,
)

WALLET = "ab" * 32
PUBKEY = bytes(CKey(b"\x31" * 32).pub).hex()


@pytest.fixture
def owned(tmp_path: Path) -> tuple[RegistryStore, ManagedNode, LndEnvironment]:
    paths = ManagedNodePaths.for_wallet(tmp_path, "regtest", WALLET)
    store = RegistryStore(paths)
    registry = new_registry("regtest", WALLET)
    node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    node.state, node.identity_pubkey = NodeState.ACTIVE, PUBKEY
    store.update(lambda current: registry)
    binary = tmp_path / "fake-lnd"
    binary.touch()
    env = LndEnvironment(
        network="regtest",
        lnd_binary=binary,
        bitcoind_host="127.0.0.1:18443",
        bitcoind_user="test",
        bitcoind_password="test",
    )
    return store, node, env


def set_state(store: RegistryStore, state: NodeState) -> None:
    registry = store.load()
    assert registry is not None
    registry.nodes[0].state = state
    store.update(lambda current: registry)


@pytest.mark.parametrize(
    "state", [NodeState.ACTIVE, NodeState.PROVISIONING, NodeState.RETIRING, NodeState.RECOVERING]
)
async def test_current_lifecycle_controls_launch_and_persists_quarantine(
    owned: Any, state: NodeState
) -> None:
    store, snapshot, env = owned
    set_state(store, state)
    before = store.paths.registry.read_bytes()
    async with managed_launch_policy(store.paths, snapshot, network=env.network) as (
        current,
        restricted,
    ):
        assert current.state is state
        assert restricted is (state in {NodeState.RETIRING, NodeState.RECOVERING})
        config = render_lnd_conf(current, env, quarantined=restricted)
        assert f"maxpendingchannels={0 if restricted else 4}" in config
    assert store.paths.registry.read_bytes() == before
    expected = QuarantineState.REQUESTED if restricted else QuarantineState.ABSENT
    assert quarantine_state(store.paths, snapshot) is expected


async def test_quarantine_is_sticky_across_active_metadata_rollback(owned: Any) -> None:
    store, node, env = owned
    await request_quarantine(store.paths, node)
    set_state(store, NodeState.RETIRING)
    set_state(store, NodeState.ACTIVE)
    async with managed_launch_policy(store.paths, node, network=env.network) as (
        current,
        restricted,
    ):
        assert current.state is NodeState.ACTIVE and restricted
        assert "maxpendingchannels=0" in render_lnd_conf(current, env, quarantined=restricted)


@pytest.mark.parametrize(
    "invalid",
    ["malformed", "version", "missing-version", "boolean", "wallet", "network", "scope", "pubkey"],
)
async def test_unknown_quarantine_is_restrictive_but_never_replaced(
    owned: Any, invalid: str
) -> None:
    store, node, env = owned
    await request_quarantine(store.paths, node)
    path = store.paths.quarantine(node)
    document = json.loads(path.read_text())
    if invalid == "malformed":
        path.write_text("{partial")
    elif invalid == "missing-version":
        del document["version"]
        path.write_text(json.dumps(document))
    else:
        key, value = {
            "version": ("version", 2),
            "boolean": ("version", True),
            "wallet": ("wallet_identity", "ef" * 32),
            "network": ("network", "signet"),
            "scope": ("generation", node.generation + 1),
            "pubkey": ("identity_pubkey", bytes(CKey(b"\x32" * 32).pub).hex()),
        }[invalid]
        document[key] = value
        path.write_text(json.dumps(document))
    before = path.read_bytes()
    assert quarantine_state(store.paths, node) is QuarantineState.UNKNOWN
    async with managed_launch_policy(store.paths, node, network=env.network) as (
        current,
        restricted,
    ):
        assert restricted
        assert "maxpendingchannels=0" in render_lnd_conf(current, env, quarantined=restricted)
    with pytest.raises(ManagedNodeError, match="preserved for review"):
        await request_quarantine(store.paths, node)
    assert path.read_bytes() == before


@pytest.mark.parametrize("authority", ["absent", "invalid", "removed", "retired", "environment"])
async def test_unknown_or_retired_authority_never_uses_a_constructor_snapshot(
    owned: Any, authority: str
) -> None:
    store, snapshot, env = owned
    network = env.network
    if authority == "absent":
        store.paths.registry.unlink()
    elif authority == "invalid":
        store.paths.registry.write_text("{partial")
    elif authority == "environment":
        network = "signet"
    else:
        registry = store.load()
        assert registry is not None
        if authority == "removed":
            registry.nodes.clear()
        else:
            registry.nodes[0].state = NodeState.RETIRED
        store.update(lambda current: registry)
    with pytest.raises(ManagedNodeError):
        async with managed_launch_policy(store.paths, snapshot, network=network):
            pytest.fail("unknown authority must not yield launch permission")
    assert not store.paths.node_dir(snapshot).exists()


async def test_directory_io_failure_never_acknowledges_durable_intent(
    owned: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, node, env = owned
    real_sync = managed_quarantine._fsync_directory

    def fail_sync(directory: Path) -> None:
        raise OSError(errno.EIO, "directory I/O failure")

    monkeypatch.setattr(managed_quarantine, "_fsync_directory", fail_sync)
    for _ in range(2):
        with pytest.raises(OSError):
            await request_quarantine(store.paths, node)
    # A valid file from the first failed attempt must not bypass the flush on retry.
    assert quarantine_state(store.paths, node) is QuarantineState.REQUESTED
    with pytest.raises(OSError):
        async with managed_launch_policy(store.paths, node, network=env.network):
            pytest.fail("launch must not claim durable intent after an I/O failure")
    monkeypatch.setattr(managed_quarantine, "_fsync_directory", real_sync)
    await request_quarantine(store.paths, node)


async def test_request_winning_the_lock_forces_the_next_launch_to_zero(
    owned: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, node, env = owned
    await request_quarantine(store.paths, node)
    lnd = ManagedLnd(node, store.paths, env)
    monkeypatch.setattr(lnd, "_stop_stale_instance", AsyncMock())
    spawned: list[str] = []

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        spawned.append(store.paths.lnd_conf(node).read_text())
        return SimpleNamespace(returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    await lnd.start()
    assert len(spawned) == 1 and "maxpendingchannels=0" in spawned[0]


async def test_launch_winning_first_is_not_retroactively_claimed_quarantined(
    owned: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, node, env = owned
    entered, release = asyncio.Event(), asyncio.Event()
    lnd = ManagedLnd(node, store.paths, env)
    monkeypatch.setattr(lnd, "_stop_stale_instance", AsyncMock())
    spawned: list[str] = []

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        spawned.append(store.paths.lnd_conf(node).read_text())
        entered.set()
        await release.wait()
        return SimpleNamespace(returncode=None)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    launching = asyncio.create_task(lnd.start())
    requesting: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        requesting = asyncio.create_task(request_quarantine(store.paths, node))
        await asyncio.sleep(0.06)
        assert not requesting.done()
        release.set()
        await asyncio.wait_for(launching, timeout=3)
        await asyncio.wait_for(requesting, timeout=3)
        assert "maxpendingchannels=4" in spawned[0]
        assert quarantine_state(store.paths, node) is QuarantineState.REQUESTED
        # The recorded request makes the NEXT launch restrictive, not this one.
        assert "maxpendingchannels=4" in store.paths.lnd_conf(node).read_text()
    finally:
        release.set()
        for task in (launching, requesting):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (launching, requesting) if task), return_exceptions=True
        )


async def test_canceled_spawn_is_recovered_and_stopped_before_unlocking(
    owned: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, node, env = owned
    entered, release, stopping, stopped = (asyncio.Event() for _ in range(4))
    process = SimpleNamespace(returncode=None)
    lnd = ManagedLnd(node, store.paths, env)
    monkeypatch.setattr(lnd, "_stop_stale_instance", AsyncMock())

    async def spawn(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        return process

    async def stop() -> None:
        assert lnd._process is process
        stopping.set()
        await stopped.wait()
        process.returncode = 0

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(lnd, "stop", stop)
    launching = asyncio.create_task(lnd.start())
    requesting: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        launching.cancel()
        requesting = asyncio.create_task(request_quarantine(store.paths, node))
        release.set()
        await asyncio.wait_for(stopping.wait(), timeout=3)
        assert not requesting.done()
        launching.cancel()
        await asyncio.sleep(0.06)
        assert not launching.done() and not requesting.done()
        stopped.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(launching, timeout=3)
        await asyncio.wait_for(requesting, timeout=3)
        assert not lnd.running
    finally:
        release.set()
        stopped.set()
        for task in (launching, requesting):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (launching, requesting) if task), return_exceptions=True
        )


async def test_canceled_lock_waiter_cannot_leave_a_future_request_or_lock(owned: Any) -> None:
    store, node, env = owned
    async with managed_launch_policy(store.paths, node, network=env.network):
        waiting = asyncio.create_task(request_quarantine(store.paths, node))
        await asyncio.sleep(0.06)
        assert not waiting.done()
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiting, timeout=3)
        assert quarantine_state(store.paths, node) is QuarantineState.ABSENT
    await asyncio.wait_for(request_quarantine(store.paths, node), timeout=3)
    assert quarantine_state(store.paths, node) is QuarantineState.REQUESTED


async def test_launch_reloads_authority_after_waiting_for_the_policy_lock(owned: Any) -> None:
    store, node, env = owned

    async def waiting_launch() -> None:
        async with managed_launch_policy(store.paths, node, network=env.network):
            pytest.fail("retired authority must be checked again after acquiring the lock")

    async with managed_launch_policy(store.paths, node, network=env.network):
        waiting = asyncio.create_task(waiting_launch())
        await asyncio.sleep(0.06)
        assert not waiting.done()
        set_state(store, NodeState.RETIRED)
    with pytest.raises(ManagedNodeError, match="retired"):
        await asyncio.wait_for(waiting, timeout=3)
