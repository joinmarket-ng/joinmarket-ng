"""Explicit hints disclose only trusted incoming ring edges on their original node."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey
from jmcore.channel_ring import RingNodeBinding

from jmswap.buyout_messages import Outpoint
from jmswap.lnd_peer import ChannelInfo, LndPeerResponseError, NodeInfo
from jmswap.lndrpc import lightning_pb2 as pb
from jmswap.ring_invoice_hints import incoming_ring_route_hints

LOCAL = bytes(CKey(b"\x61" * 32).pub).hex()
REMOTE = bytes(CKey(b"\x62" * 32).pub).hex()
OTHER = bytes(CKey(b"\x63" * 32).pub).hex()
POINT = Outpoint(txid="11" * 32, vout=0)
BINDING = RingNodeBinding(
    network="regtest",
    wallet_identity="aa" * 32,
    source_mixdepth=0,
    node_name="node0",
    local_node_id=LOCAL,
)


def channel(**changes: Any) -> ChannelInfo:
    return replace(
        ChannelInfo(
            point=POINT,
            peer_pubkey=REMOTE,
            active=True,
            private=True,
            capacity_sat=1_000_000,
            local_balance_sat=600_000,
            remote_balance_sat=399_000,
            pending_htlcs=0,
            commitment_type="SIMPLE_TAPROOT",
            channel_id=42,
            remote_reserve_sat=10_000,
        ),
        **changes,
    )


def edge(**changes: Any) -> pb.ChannelEdge:
    values = {
        "chan_point": f"{POINT.txid}:{POINT.vout}",
        "channel_id": 42,
        "capacity": 1_000_000,
        "node1_pub": REMOTE,
        "node2_pub": LOCAL,
        "node1_policy": pb.RoutingPolicy(
            time_lock_delta=40,
            fee_base_msat=1_000,
            fee_rate_milli_msat=1,
            min_htlc=1_000,
            max_htlc_msat=100_000_000,
        ),
        "node2_policy": pb.RoutingPolicy(
            time_lock_delta=99,
            fee_base_msat=9_000,
            fee_rate_milli_msat=9,
            min_htlc=1_000,
            max_htlc_msat=100_000_000,
        ),
    }
    values.update(changes)
    return pb.ChannelEdge(**values)


def client(channels: list[ChannelInfo] | None = None, routing_edge: Any = None) -> Any:
    return SimpleNamespace(
        node_info=AsyncMock(return_value=NodeInfo(LOCAL, 100, True, "regtest")),
        channels=AsyncMock(return_value=[channel()] if channels is None else channels),
        channel_routing_edge=AsyncMock(
            return_value=edge() if routing_edge is None else routing_edge
        ),
    )


@pytest.mark.parametrize("reverse", [False, True])
async def test_only_remote_to_local_policy_is_disclosed(reverse: bool) -> None:
    graph = edge()
    if reverse:
        graph = edge(
            node1_pub=LOCAL,
            node2_pub=REMOTE,
            node1_policy=graph.node2_policy,
            node2_policy=graph.node1_policy,
        )
    peer = client(routing_edge=graph)
    hints = await incoming_ring_route_hints(peer, BINDING, {POINT: REMOTE}, 100)
    assert len(hints) == 1 and len(hints[0]) == 1
    assert hints[0][0].model_dump() == {
        "node_id": REMOTE,
        "chan_id": 42,
        "fee_base_msat": 1_000,
        "fee_proportional_millionths": 1,
        "cltv_expiry_delta": 40,
    }
    peer.channel_routing_edge.assert_awaited_once_with(POINT)


async def test_missing_authority_permits_no_native_discovery_or_extra_hint() -> None:
    peer = client()
    assert await incoming_ring_route_hints(peer, BINDING, {}, 100) == ()
    peer.node_info.assert_not_awaited()
    peer.channels.assert_not_awaited()


async def test_unrelated_private_channel_is_not_disclosed() -> None:
    peer = client([channel(point=Outpoint(txid="22" * 32, vout=1), peer_pubkey=OTHER)])
    assert await incoming_ring_route_hints(peer, BINDING, {POINT: REMOTE}, 100) == ()
    peer.channel_routing_edge.assert_not_awaited()


@pytest.mark.parametrize(
    "info", [NodeInfo(OTHER, 100, True, "regtest"), NodeInfo(LOCAL, 100, True, "signet")]
)
async def test_replacement_identity_or_network_cannot_supply_hints(info: NodeInfo) -> None:
    peer = client()
    peer.node_info.return_value = info
    with pytest.raises(LndPeerResponseError):
        await incoming_ring_route_hints(peer, BINDING, {POINT: REMOTE}, 100)
    peer.channels.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"active": False},
        {"private": False},
        {"peer_pubkey": OTHER},
        {"commitment_type": "ANCHORS"},
        {"remote_reserve_sat": None},
        {"remote_balance_sat": 10_099},
        {"pending_htlcs": 1},
        {"channel_id": 0},
        {"alias_scids": (99,)},
        {"zero_conf": True},
    ],
)
async def test_unusable_channel_does_not_authorize_a_hint(changes: dict[str, Any]) -> None:
    peer = client([channel(**changes)])
    assert await incoming_ring_route_hints(peer, BINDING, {POINT: REMOTE}, 100) == ()
    peer.channel_routing_edge.assert_not_awaited()


async def test_peer_alias_is_used_instead_of_local_alias_or_confirmed_scid() -> None:
    peer = client([channel(peer_scid_alias=123, alias_scids=(999,), zero_conf=True)])
    hints = await incoming_ring_route_hints(peer, BINDING, {POINT: REMOTE}, 100)
    assert hints[0][0].chan_id == 123


@pytest.mark.parametrize(
    "changes",
    [
        {"node1_pub": OTHER},
        {"capacity": 999_999},
        {"channel_id": 43},
        {"node1_policy": None},
        {"node1_policy": pb.RoutingPolicy(disabled=True)},
        {"node1_policy": pb.RoutingPolicy(min_htlc=100_001, max_htlc_msat=200_000)},
        {"node1_policy": pb.RoutingPolicy(min_htlc=1, max_htlc_msat=99_999)},
        {"node1_policy": pb.RoutingPolicy(min_htlc=-1, max_htlc_msat=200_000)},
        {"node1_policy": pb.RoutingPolicy(min_htlc=1, max_htlc_msat=200_000, fee_base_msat=-1)},
    ],
)
async def test_mismatched_or_unusable_remote_policy_is_not_disclosed(
    changes: dict[str, Any],
) -> None:
    peer = client(routing_edge=edge(**changes))
    assert await incoming_ring_route_hints(peer, BINDING, {POINT: REMOTE}, 100) == ()


async def test_unknown_policy_response_adds_no_hint() -> None:
    peer = client()
    peer.channel_routing_edge.side_effect = LndPeerResponseError("unavailable policy")
    assert await incoming_ring_route_hints(peer, BINDING, {POINT: REMOTE}, 100) == ()
