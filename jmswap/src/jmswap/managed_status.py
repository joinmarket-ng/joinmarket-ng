"""Read-only status of managed nodes, for `jm-lightning status` and `jm-wallet info`."""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from jmswap.lnd_config import LndEnvironment
from jmswap.managed_lnd import ManagedLnd
from jmswap.managed_nodes import (
    ManagedNode,
    ManagedNodeError,
    ManagedNodePaths,
    NodeRegistry,
    NodeState,
)


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


def mixdepth_lines(rows: list[NodeStatus], reserve_min_sats: int) -> list[str]:
    """One line per mixdepth node, for `jm-wallet info`."""
    lines: list[str] = []
    for row in sorted(rows, key=lambda r: (r.mixdepth, r.generation)):
        label = f"  Mixdepth {row.mixdepth}: {(row.identity_pubkey or '-')[:12]}"
        if row.state is not NodeState.ACTIVE:
            label += f" ({row.state.value})"
        if not row.running:
            lines.append(f"{label}  {row.error or 'not running'}")
            continue
        detail = (
            f"reserve {row.confirmed_sats:,}, {row.channels} channels, "
            f"out {row.outbound_sats:,}, in {row.inbound_sats:,} sats"
        )
        if row.pending_channels:
            detail += f", {row.pending_channels} pending"
        if row.state is NodeState.ACTIVE and not row.reserve_ok:
            detail += f" (reserve below {reserve_min_sats:,})"
        if not row.synced:
            detail += " (syncing)"
        lines.append(f"{label}  {detail}")
    if any(not row.running for row in rows):
        lines.append("  Start the nodes with `jm-lightning run`")
    return lines


async def wallet_lightning_lines(settings: Any, seed: bytes) -> list[str]:
    """The Lightning section of `jm-wallet info` for this wallet's managed nodes."""
    from jmswap.lightning_context import lightning_context

    try:
        ctx = lightning_context(settings, seed, with_keys=False)
        registry = ctx.store.load()
    except ManagedNodeError as exc:
        return [f"  Unavailable: {exc}"]
    if registry is None:
        return ["  No managed nodes yet; create them with `jm-lightning init`"]
    reserve_min = int(settings.lightning.reserve_min_sats)
    rows = await registry_status(registry, ctx.paths, ctx.env, reserve_min_sats=reserve_min)
    return mixdepth_lines(rows, reserve_min)
