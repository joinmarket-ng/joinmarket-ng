"""Registry reloads authorize new workers or explicit retirement, never absence-based shutdown."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from jmcore.lightning_keys import WalletLightningKeys

from jmswap import managed_supervisor
from jmswap.lnd_config import LndEnvironment
from jmswap.managed_nodes import (
    ManagedNode,
    ManagedNodeError,
    ManagedNodePaths,
    NodeRegistry,
    NodeState,
    RegistryStore,
    allocate_node,
    new_registry,
)
from jmswap.managed_supervisor import Supervisor

WALLET = "ab" * 32


class FakeLnd:
    def __init__(self, node: ManagedNode, paths: ManagedNodePaths, env: LndEnvironment) -> None:
        self.node = node
        self.paths = paths
        self.running = False
        self.returncode: int | None = None
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.backup_reads = 0

    async def start(self) -> None:
        self.running = True
        self.started.set()

    async def stop(self) -> None:
        self.running = False
        self.returncode = 0
        self.stopped.set()

    async def ensure_unlocked(self, keys: WalletLightningKeys, *, allow_create: bool) -> None:
        assert not allow_create

    async def call(self, method: str, request: Any) -> Any:
        if method == "ListChannels":
            return SimpleNamespace(channels=[])
        assert method == "PendingChannels"
        self.backup_reads += 1
        return SimpleNamespace(pending_open_channels=[])


async def wait_until(condition: Callable[[], bool]) -> None:
    async def wait() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(wait(), timeout=3)


@pytest.fixture
def harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Supervisor, RegistryStore, dict[str, FakeLnd], AsyncMock]:
    store = RegistryStore(ManagedNodePaths.for_wallet(tmp_path, "regtest", WALLET))
    registry = new_registry("regtest", WALLET)
    node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    node.state = NodeState.ACTIVE
    store.update(lambda current: registry)
    wallet = store.paths.wallet_db(node)
    wallet.parent.mkdir(parents=True)
    wallet.write_bytes(b"existing test wallet")
    processes: dict[str, FakeLnd] = {}

    def process(node: ManagedNode, paths: ManagedNodePaths, env: LndEnvironment) -> FakeLnd:
        assert node.name not in processes, "a completed worker must not be implicitly restarted"
        lnd = FakeLnd(node, paths, env)
        processes[node.name] = lnd
        return lnd

    async def provision(
        store: RegistryStore,
        name: str,
        keys: WalletLightningKeys,
        env: LndEnvironment,
    ) -> FakeLnd:
        current = store.load()
        assert current is not None and current.find(name).state is NodeState.PROVISIONING
        lnd = process(current.find(name), store.paths, env)
        await lnd.start()
        return lnd

    provisioning = AsyncMock(side_effect=provision)
    monkeypatch.setattr(managed_supervisor, "ManagedLnd", process)
    monkeypatch.setattr(managed_supervisor, "provision_node", provisioning)
    environment = LndEnvironment(
        network="regtest",
        lnd_binary=tmp_path / "unused-lnd",
        bitcoind_host="127.0.0.1:18443",
        bitcoind_user="test",
        bitcoind_password="test",
    )
    supervisor = Supervisor(
        store,
        WalletLightningKeys(b"unit" * 16, "regtest"),
        environment,
        poll_seconds=0.005,
        backup_seconds=0.005,
    )
    return supervisor, store, processes, provisioning


async def test_replacement_is_discovered_while_the_retiring_node_keeps_running(
    harness: Any,
) -> None:
    supervisor, store, processes, provision = harness
    stop = asyncio.Event()
    service = asyncio.create_task(supervisor.run(stop))
    try:
        await wait_until(
            lambda: bool(processes) and next(iter(processes.values())).backup_reads > 0
        )
        original = next(iter(processes.values()))

        def replace(registry: NodeRegistry | None) -> NodeRegistry:
            assert registry is not None
            registry.find(original.node.name).state = NodeState.RETIRING
            allocate_node(registry, 0, birthday=2, ports=(20003, 20004))
            return registry

        store.update(replace)
        await wait_until(
            lambda: len(processes) == 2 and all(p.backup_reads for p in processes.values())
        )
        assert all(process.running for process in processes.values())
        assert provision.await_count == 1
        replacement = next(process for process in processes.values() if process is not original)
        assert replacement.node.generation == original.node.generation + 1

        def retire(registry: NodeRegistry | None) -> NodeRegistry:
            assert registry is not None
            registry.find(original.node.name).state = NodeState.RETIRED
            return registry

        store.update(retire)
        await asyncio.wait_for(original.stopped.wait(), timeout=3)
        assert replacement.running and not service.done()
        # Discovery continues but does not restart the explicitly retired identity.
        reads = replacement.backup_reads
        await wait_until(lambda: replacement.backup_reads > reads + 1)
        assert len(processes) == 2 and provision.await_count == 1
    finally:
        stop.set()
        await asyncio.wait_for(service, timeout=3)
    assert not supervisor.processes
    assert all(process.stopped.is_set() for process in processes.values())


@pytest.mark.parametrize("unknown", ["absent", "malformed", "unsupported", "removed", "foreign"])
async def test_unknown_registry_state_keeps_owned_nodes_and_backups_running(
    harness: Any,
    unknown: str,
) -> None:
    supervisor, store, processes, provision = harness
    stop = asyncio.Event()
    service = asyncio.create_task(supervisor.run(stop))
    try:
        await wait_until(
            lambda: bool(processes) and next(iter(processes.values())).backup_reads > 0
        )
        original = next(iter(processes.values()))
        document = json.loads(store.paths.registry.read_text())
        if unknown == "absent":
            store.paths.registry.unlink()
        elif unknown == "malformed":
            store.paths.registry.write_text("not json")
        else:
            if unknown == "unsupported":
                document["version"] = 2
            elif unknown == "removed":
                document["nodes"] = []
            else:
                document["wallet_identity"] = "ef" * 32
                document["nodes"][0]["state"] = "retired"
                foreign = NodeRegistry.model_validate(document)
                allocate_node(foreign, 1, birthday=3, ports=(20005, 20006))
                document = foreign.model_dump(mode="json")
            store.paths.registry.write_text(json.dumps(document))
        reads = original.backup_reads
        await wait_until(lambda: original.backup_reads > reads + 2)
        assert original.running and not original.stopped.is_set()
        assert len(processes) == 1 and provision.await_count == 0
        assert not service.done()
        if unknown == "foreign":
            with pytest.raises(ManagedNodeError, match="different wallet"):
                await supervisor._start(original.node.name)
    finally:
        stop.set()
        await asyncio.wait_for(service, timeout=3)


async def test_cancellation_stops_workers_and_releases_the_process_lock(harness: Any) -> None:
    supervisor, store, processes, _ = harness
    stop = asyncio.Event()
    service = asyncio.create_task(supervisor.run(stop))
    await wait_until(lambda: bool(processes) and next(iter(processes.values())).backup_reads > 0)
    service.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(service, timeout=3)
    assert stop.is_set() and not supervisor.processes
    assert all(process.stopped.is_set() for process in processes.values())
    with store.exclusive_processes():
        pass


async def test_a_missing_established_wallet_is_not_repeatedly_started(
    harness: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    supervisor, store, processes, provision = harness
    registry = store.load()
    assert registry is not None
    store.paths.wallet_db(registry.nodes[0]).unlink()
    load = store.load
    reads = 0

    def observed_load() -> NodeRegistry | None:
        nonlocal reads
        reads += 1
        return load()

    monkeypatch.setattr(store, "load", observed_load)
    start = AsyncMock(wraps=supervisor._start)
    monkeypatch.setattr(supervisor, "_start", start)
    stop = asyncio.Event()
    service = asyncio.create_task(supervisor.run(stop))
    try:
        await wait_until(lambda: reads > 5)
        assert processes == {} and provision.await_count == 0
        assert start.await_count == 1
    finally:
        stop.set()
        await asyncio.wait_for(service, timeout=3)


@pytest.mark.parametrize("state", ["absent", "malformed", "retired"])
async def test_startup_still_refuses_unknown_or_nonrunnable_registry(
    harness: Any, state: str
) -> None:
    supervisor, store, processes, provision = harness
    if state == "absent":
        store.paths.registry.unlink()
    elif state == "malformed":
        store.paths.registry.write_text("{incomplete")
    else:
        registry = store.load()
        assert registry is not None
        registry.nodes[0].state = NodeState.RETIRED
        store.update(lambda current: registry)
    with pytest.raises(ManagedNodeError):
        await supervisor.run(asyncio.Event())
    assert processes == {} and provision.await_count == 0
