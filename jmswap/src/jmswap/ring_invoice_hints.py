"""Incoming invoice paths explicitly authorized by this wallet's ring owner.

LND's automatic private hints omit unadvertised peers. Ring orchestration can
authorize these specific edges instead, without adding hints for unrelated
private channels or giving hold-invoice credentials access to the Lightning service.
"""

from __future__ import annotations

from collections.abc import Mapping

from jmcore.channel_ring import RingNodeBinding

from jmswap.buyout_messages import MAX_MONEY, Outpoint
from jmswap.lnd_peer import (
    MAX_ROUTE_HINT_PATHS,
    LndPeerClient,
    LndPeerResponseError,
    LndPeerRpcError,
    PaymentRouteHop,
)


async def incoming_ring_route_hints(
    client: LndPeerClient,
    binding: RingNodeBinding,
    incoming_channels: Mapping[Outpoint, str],
    amount_sat: int,
) -> tuple[tuple[PaymentRouteHop, ...], ...]:
    """Validate caller-authorized incoming edges against their original native node.

    The caller verifies wallet/mixdepth ownership and complete accepted journal
    provenance before supplying any edge. Native balances are only a current
    estimate, never a routing or settlement guarantee. Unusable or unavailable
    policy data supplies no additional hint; node identity mismatch is fatal.
    """
    if type(amount_sat) is not int or not 1 <= amount_sat <= MAX_MONEY:
        raise ValueError("invoice amount must be a positive satoshi amount")
    if not incoming_channels:
        return ()
    info = await client.node_info()
    if info.identity_pubkey != binding.local_node_id or info.network != binding.network:
        raise LndPeerResponseError("invoice hint node does not match its original ring binding")
    if not info.synced_to_chain:
        return ()
    amount_msat = amount_sat * 1_000
    hints: list[tuple[PaymentRouteHop, ...]] = []
    identifiers: set[int] = set()
    for channel in await client.channels():
        expected_peer = incoming_channels.get(channel.point)
        if (
            expected_peer is None
            or expected_peer != channel.peer_pubkey
            or not channel.active
            or not channel.private
            or "TAPROOT" not in channel.commitment_type
            or channel.pending_htlcs
            or channel.remote_reserve_sat is None
            or channel.remote_balance_sat - channel.remote_reserve_sat < amount_sat
        ):
            continue
        identifier = channel.peer_scid_alias
        if not identifier:
            # An arbitrary local alias cannot identify the peer's forwarding
            # direction. Unknown alias provenance must not become a real SCID.
            if channel.zero_conf or channel.alias_scids:
                continue
            identifier = channel.channel_id
        if not identifier or identifier in identifiers:
            continue
        try:
            edge = await client.channel_routing_edge(channel.point)
        except (LndPeerResponseError, LndPeerRpcError):
            continue
        if (
            {edge.node1_pub, edge.node2_pub} != {binding.local_node_id, expected_peer}
            or edge.capacity != channel.capacity_sat
            or edge.channel_id != channel.channel_id
        ):
            continue
        policy_name = "node1_policy" if edge.node1_pub == expected_peer else "node2_policy"
        if not edge.HasField(policy_name):
            continue
        policy = getattr(edge, policy_name)
        if (
            policy.disabled
            or policy.min_htlc < 0
            or not policy.min_htlc <= amount_msat <= policy.max_htlc_msat
        ):
            continue
        try:
            hop = PaymentRouteHop(
                node_id=expected_peer,
                chan_id=identifier,
                fee_base_msat=policy.fee_base_msat,
                fee_proportional_millionths=policy.fee_rate_milli_msat,
                cltv_expiry_delta=policy.time_lock_delta,
            )
        except ValueError:
            continue
        identifiers.add(identifier)
        hints.append((hop,))
        if len(hints) == MAX_ROUTE_HINT_PATHS:
            break
    return tuple(hints)
