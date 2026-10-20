"""Read-only status of managed nodes, for `jm-lightning status` and `jm-wallet info`."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from jmswap.lnd_config import LndEnvironment
from jmswap.managed_lnd import ManagedLnd
from jmswap.managed_nodes import ManagedNode, ManagedNodePaths, NodeRegistry, NodeState


class NodeStatus(BaseModel):
    name: str
    mixdepth: int
    generation: int
    state: NodeState
    identity_pubkey: str | None
    onion_endpoint: str | None
    running: bool = False
    synced: bool = False
    block_height: int | None = None
    confirmed_sats: int = 0
    unconfirmed_sats: int = 0
    channels: int = 0
    pending_channels: int = 0
    outbound_sats: int = 0
    inbound_sats: int = 0
    reserve_ok: bool = False
    error: str | None = None


async def node_status(
    node: ManagedNode,
    paths: ManagedNodePaths,
    env: LndEnvironment,
    *,
    reserve_min_sats: int,
) -> NodeStatus:
    from jmswap.lndrpc import lightning_pb2

    status = NodeStatus(
        name=node.name,
        mixdepth=node.mixdepth,
        generation=node.generation,
        state=node.state,
        identity_pubkey=node.identity_pubkey,
        onion_endpoint=node.onion_endpoint,
    )
    if node.state is NodeState.RETIRED:
        return status
    if not paths.macaroon(node).exists() or not paths.tls_cert(node).exists():
        status.error = "no LND data" if not paths.wallet_db(node).exists() else "not started"
        return status
    lnd = ManagedLnd(node, paths, env)
    try:
        info = await lnd.call("GetInfo", lightning_pb2.GetInfoRequest(), timeout=5)
        wallet = await lnd.call("WalletBalance", lightning_pb2.WalletBalanceRequest(), timeout=5)
        balance = await lnd.call("ChannelBalance", lightning_pb2.ChannelBalanceRequest(), timeout=5)
        pending = await lnd.call(
            "PendingChannels", lightning_pb2.PendingChannelsRequest(), timeout=5
        )
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        status.error = "not running" if "UNAVAILABLE" in str(exc) else type(exc).__name__
        return status
    status.running = True
    # Private ring nodes do not bootstrap a graph, so only chain sync matters.
    status.synced = bool(info.synced_to_chain)
    status.block_height = int(info.block_height)
    status.confirmed_sats = int(wallet.confirmed_balance)
    status.unconfirmed_sats = int(wallet.unconfirmed_balance)
    status.channels = int(info.num_active_channels) + int(info.num_inactive_channels)
    status.pending_channels = (
        len(pending.pending_open_channels)
        + len(pending.waiting_close_channels)
        + len(pending.pending_force_closing_channels)
    )
    status.outbound_sats = int(balance.local_balance.sat)
    status.inbound_sats = int(balance.remote_balance.sat)
    status.reserve_ok = status.confirmed_sats >= reserve_min_sats
    return status


async def registry_status(
    registry: NodeRegistry,
    paths: ManagedNodePaths,
    env: LndEnvironment,
    *,
    reserve_min_sats: int,
    include_retired: bool = False,
) -> list[NodeStatus]:
    nodes = [n for n in registry.nodes if include_retired or n.state is not NodeState.RETIRED]
    return list(
        await asyncio.gather(
            *(node_status(n, paths, env, reserve_min_sats=reserve_min_sats) for n in nodes)
        )
    )
