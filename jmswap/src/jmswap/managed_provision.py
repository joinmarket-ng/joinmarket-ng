"""Create managed nodes and resume interrupted creations."""

from __future__ import annotations

import asyncio
import time

from jmcore.channel_ring import NodeOwner
from jmcore.lightning_keys import WalletLightningKeys
from loguru import logger

from jmswap.channel_ring_nodes import enroll_node_bindings
from jmswap.lnd_config import LndEnvironment
from jmswap.managed_lnd import ManagedLnd, onion_endpoint_of
from jmswap.managed_nodes import (
    ManagedNodeError,
    NodeRegistry,
    NodeState,
    RegistryStore,
    allocate_node,
    new_registry,
)

ONION_TIMEOUT_SECONDS = 180.0


async def provision_node(
    store: RegistryStore,
    name: str,
    keys: WalletLightningKeys,
    env: LndEnvironment,
) -> ManagedLnd:
    """Bring one ``provisioning`` node to ``active`` and return its running process.

    The epoch is local to this installation, so the identity was never operated
    elsewhere: without an LND wallet it is created, with one it is unlocked.
    """
    registry = store.load()
    if registry is None:
        raise ManagedNodeError("no managed node registry")
    node = registry.find(name)
    if node.state is not NodeState.PROVISIONING:
        raise ManagedNodeError(f"{name} is {node.state}, not provisioning")
    paths = store.paths
    lnd = ManagedLnd(node, paths, env)
    await lnd.start()
    try:
        await lnd.ensure_unlocked(keys, allow_create=not paths.wallet_db(node).exists())
        identity, onion = await _identity_and_onion(lnd)
        enroll_node_bindings(
            paths.bindings_dir,
            {
                identity: NodeOwner(
                    network=registry.network,
                    wallet_identity=registry.wallet_identity,
                    source_mixdepth=node.mixdepth,
                )
            },
        )

        def activate(current: NodeRegistry | None) -> NodeRegistry:
            if current is None:
                raise ManagedNodeError("managed node registry disappeared")
            entry = current.find(name)
            if entry.state is not NodeState.PROVISIONING:
                raise ManagedNodeError(f"{name} changed state during provisioning")
            entry.identity_pubkey = identity
            entry.onion_endpoint = onion
            entry.state = NodeState.ACTIVE
            return current

        store.update(activate)
        lnd.node = store.load().find(name)  # type: ignore[union-attr]
        logger.info("Managed node {} for mixdepth {} is active", name, node.mixdepth)
        return lnd
    except BaseException:
        await lnd.stop()
        raise


async def _identity_and_onion(lnd: ManagedLnd) -> tuple[str, str]:
    from jmswap.lndrpc import lightning_pb2

    deadline = time.monotonic() + ONION_TIMEOUT_SECONDS
    while True:
        info = await lnd.call("GetInfo", lightning_pb2.GetInfoRequest())
        identity = str(info.identity_pubkey)
        onion = onion_endpoint_of(identity, [str(uri) for uri in info.uris])
        if onion is not None:
            return identity, onion
        if time.monotonic() > deadline:
            raise ManagedNodeError(
                f"{lnd.node.name} has no onion service; check Tor control access"
            )
        await asyncio.sleep(2)


async def create_nodes(
    store: RegistryStore,
    keys: WalletLightningKeys,
    env: LndEnvironment,
    *,
    wallet_identity: str,
    mixdepths: range,
) -> NodeRegistry:
    """``jm-lightning init``: a fresh epoch with one node per mixdepth.

    Refuses when any registry or node data exists. Every allocation is persisted
    before its wallet is created; an interrupted run is resumed by ``resume``.
    """
    paths = store.paths
    if paths.registry.exists() or paths.has_any_node_data():
        raise ManagedNodeError(
            f"managed node state already exists in {paths.root}; nothing was changed"
        )
    now = int(time.time())

    def allocate(current: NodeRegistry | None) -> NodeRegistry:
        if current is not None:
            raise ManagedNodeError("a managed node registry appeared concurrently")
        registry = new_registry(env.network, wallet_identity)
        for mixdepth in mixdepths:
            allocate_node(registry, mixdepth, birthday=now)
        return registry

    store.update(allocate)
    return await resume(store, keys, env)


def allocate_missing(store: RegistryStore, mixdepths: range) -> list[int]:
    """Allocate nodes for mixdepths the registry has never had a node for.

    This covers a raised ``wallet.mixdepth_count``. A mixdepth with any entry,
    even a retired one, is left to rotation.
    """
    added: list[int] = []
    now = int(time.time())

    def extend(current: NodeRegistry | None) -> NodeRegistry:
        if current is None:
            raise ManagedNodeError("no managed node registry; run `jm-lightning init`")
        known = {node.mixdepth for node in current.nodes}
        for mixdepth in mixdepths:
            if mixdepth not in known:
                allocate_node(current, mixdepth, birthday=now)
                added.append(mixdepth)
        return current

    store.update(extend)
    return added


async def resume(
    store: RegistryStore, keys: WalletLightningKeys, env: LndEnvironment
) -> NodeRegistry:
    """Finish every ``provisioning`` node, one at a time, and stop each again."""
    registry = store.load()
    if registry is None:
        raise ManagedNodeError("no managed node registry; run `jm-lightning init`")
    for node in registry.nodes:
        if node.state is NodeState.PROVISIONING:
            lnd = await provision_node(store, node.name, keys, env)
            await lnd.stop()
    final = store.load()
    assert final is not None
    return final
