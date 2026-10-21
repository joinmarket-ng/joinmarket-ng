"""``jm-lightning run``: keep every runnable managed node up and backed up."""

from __future__ import annotations

import asyncio
import contextlib

from jmcore.lightning_keys import WalletLightningKeys
from jmcore.secure_files import atomic_write_private, ensure_private_directory
from loguru import logger

from jmswap.lnd_config import LndEnvironment
from jmswap.managed_lnd import ManagedLnd
from jmswap.managed_nodes import ManagedNodeError, NodeRegistry, NodeState, RegistryStore
from jmswap.managed_provision import provision_node

MIN_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0


async def export_channel_backup(lnd: ManagedLnd) -> bool:
    """Write the node's static channel backup when it changed; True if written."""
    from jmswap.lndrpc import lightning_pb2

    listed = await lnd.call("ListChannels", lightning_pb2.ListChannelsRequest())
    pending = await lnd.call("PendingChannels", lightning_pb2.PendingChannelsRequest())
    if not listed.channels and not pending.pending_open_channels:
        return False
    snapshot = await lnd.call("ExportAllChannelBackups", lightning_pb2.ChanBackupExportRequest())
    data = bytes(snapshot.multi_chan_backup.multi_chan_backup)
    path = lnd.paths.channel_backup(lnd.node)
    with contextlib.suppress(FileNotFoundError):
        if path.read_bytes() == data:
            return False
    ensure_private_directory(path.parent)
    atomic_write_private(path, data)
    return True


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


class Supervisor:
    """Run one LND per runnable registry node, restarting crashed nodes with backoff."""

    def __init__(
        self,
        store: RegistryStore,
        keys: WalletLightningKeys,
        env: LndEnvironment,
        *,
        poll_seconds: float = 5.0,
        backup_seconds: float = 60.0,
    ) -> None:
        self.store = store
        self.keys = keys
        self.env = env
        self.poll_seconds = poll_seconds
        self.backup_seconds = backup_seconds
        self.processes: dict[str, ManagedLnd] = {}
        self._wallet_identity: str | None = None

    async def run(self, stop: asyncio.Event) -> None:
        with self.store.exclusive_processes():
            await self._run(stop)

    async def _run(self, stop: asyncio.Event) -> None:
        registry = self._load_registry()
        if not registry.runnable():
            raise ManagedNodeError("the registry has no runnable managed nodes")
        # Keep completed names: a missing established wallet is not repaired or
        # repeatedly started just because discovery runs again. Explicit recovery
        # requires stopping this supervisor and restarting it afterward.
        tasks: dict[str, asyncio.Task[None]] = {}
        previous_problem: str | None = None
        try:
            while not stop.is_set():
                try:
                    registry = self._load_registry()
                except (ManagedNodeError, OSError) as exc:
                    problem = str(exc)
                    if problem != previous_problem:
                        logger.warning(
                            "Managed node discovery paused: {}; existing nodes keep running",
                            problem,
                        )
                    previous_problem = problem
                else:
                    previous_problem = None
                    for node in registry.runnable():
                        if node.name not in tasks:
                            tasks[node.name] = asyncio.create_task(
                                self._keep_running(node.name, stop)
                            )
                await _sleep_or_stop(stop, self.poll_seconds)
        finally:
            # Also required on cancellation: gathering workers without notifying
            # them would wait forever for a stop event nobody will set.
            stop.set()
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    def _load_registry(self) -> NodeRegistry:
        """Pin wallet ownership at startup and reject a different authority on reload."""
        registry = self.store.load()
        if registry is None:
            raise ManagedNodeError("no managed nodes; run `jm-lightning init` first")
        if self._wallet_identity is None:
            self._wallet_identity = registry.wallet_identity
        elif registry.wallet_identity != self._wallet_identity:
            raise ManagedNodeError("managed node registry belongs to a different wallet")
        return registry

    async def _start(self, name: str) -> ManagedLnd | None:
        registry = self._load_registry()
        node = registry.find(name)
        if node.state not in (
            NodeState.PROVISIONING,
            NodeState.ACTIVE,
            NodeState.RETIRING,
            NodeState.RECOVERING,
        ):
            return None
        if node.state is NodeState.PROVISIONING:
            return await provision_node(self.store, name, self.keys, self.env)
        if not self.store.paths.wallet_db(node).exists():
            # Never reinitialize a node that may have channels: that needs recovery.
            logger.error(
                "{} (mixdepth {}) has no LND data in {}; it is not started and needs "
                "`jm-lightning restore`",
                name,
                node.mixdepth,
                self.store.paths.node_dir(node),
            )
            return None
        lnd = ManagedLnd(node, self.store.paths, self.env)
        await lnd.start()
        try:
            await lnd.ensure_unlocked(self.keys, allow_create=False)
        except BaseException:
            await lnd.stop()
            raise
        logger.info("Managed node {} (mixdepth {}) is running", name, node.mixdepth)
        return lnd

    async def _keep_running(self, name: str, stop: asyncio.Event) -> None:
        backoff = MIN_BACKOFF_SECONDS
        while not stop.is_set():
            lnd: ManagedLnd | None = None
            try:
                lnd = await self._start(name)
                if lnd is None:
                    return
                self.processes[name] = lnd
                backoff = MIN_BACKOFF_SECONDS
                await self._watch(lnd, stop)
                # Normal completion means explicit retirement or service shutdown.
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep supervising the others
                logger.error("Managed node {} failed: {}", name, exc)
            finally:
                if lnd is not None:
                    await lnd.stop()
                    self.processes.pop(name, None)
            if stop.is_set():
                return
            logger.warning("Restarting managed node {} in {:.0f}s", name, backoff)
            await _sleep_or_stop(stop, backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)

    async def _watch(self, lnd: ManagedLnd, stop: asyncio.Event) -> None:
        elapsed = self.backup_seconds
        previous_problem: str | None = None
        while not stop.is_set():
            if not lnd.running:
                raise ManagedNodeError(f"lnd exited with code {lnd.returncode}")
            try:
                node = self._load_registry().find(lnd.node.name)
            except (ManagedNodeError, OSError) as exc:
                problem = str(exc)
                if problem != previous_problem:
                    logger.warning(
                        "Cannot read lifecycle state for {}: {}; keeping LND and backups running",
                        lnd.node.name,
                        problem,
                    )
                previous_problem = problem
            else:
                previous_problem = None
                if node.state is NodeState.RETIRED:
                    logger.info("Stopping explicitly retired managed node {}", node.name)
                    return
            if elapsed >= self.backup_seconds:
                elapsed = 0.0
                try:
                    if await export_channel_backup(lnd):
                        logger.info("Updated the channel backup of {}", lnd.node.name)
                except Exception as exc:  # noqa: BLE001 - retried next interval
                    logger.warning("Channel backup of {} failed: {}", lnd.node.name, exc)
            await _sleep_or_stop(stop, self.poll_seconds)
            elapsed += self.poll_seconds
