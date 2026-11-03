"""Tests for MultiDirectoryClient.bind_session() channel selection."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer, OfferType
from jmcore.protocol import MessageType

from taker.multi_directory import ChannelBinding, MultiDirectoryClient


def _make_client(
    *,
    clients: dict[str, MagicMock] | None = None,
    active_nicks: dict[str, dict[str, bool]] | None = None,
    peer_connections: dict[str, MagicMock] | None = None,
    prefer_direct: bool = True,
) -> MultiDirectoryClient:
    """Build a MultiDirectoryClient skeleton without running __init__."""
    mdc = MultiDirectoryClient.__new__(MultiDirectoryClient)
    mdc.clients = clients or {}
    mdc._active_nicks = active_nicks or {}
    mdc._peer_connections = peer_connections or {}
    mdc._pending_connect_tasks = {}
    mdc.prefer_direct_connections = prefer_direct
    mdc.network = "mainnet"
    mdc.allow_clearnet_connections = False
    return mdc


def _offer(
    cert_expiry: int,
    *,
    features: dict[str, bool] | None = None,
    neutrino_compat: bool = False,
) -> Offer:
    return Offer(
        counterparty="J5maker",
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=100_000,
        maxsize=1_000_000,
        txfee=1_000,
        cjfee="0.001",
        fidelity_bond_data={
            "utxo_txid": "ab" * 32,
            "utxo_vout": 0,
            "locktime": 2_000_000_000,
            "utxo_pub": "02" + "11" * 32,
            "cert_expiry": cert_expiry,
        },
        features=features or {},
        neutrino_compat=neutrino_compat,
    )


@pytest.mark.asyncio
async def test_stalled_directory_does_not_block_healthy_offers() -> None:
    stalled = DirectoryClient("stalled", 1234, "regtest")
    stalled.connection = AsyncMock()
    stalled.directory_peerlist_features = True
    stalled._peerlist_timeout = 0.03
    stalled._peerlist_chunk_timeout = 0.02

    async def stalled_send(_data: bytes) -> None:
        await asyncio.Event().wait()

    stalled.connection.send.side_effect = stalled_send
    healthy = DirectoryClient("healthy", 1234, "regtest")
    healthy.connection = AsyncMock()
    healthy.connection.is_connected = MagicMock(return_value=True)
    healthy.get_peerlist_with_features = AsyncMock(return_value=[])  # type: ignore[method-assign]
    frame = json.dumps(
        {
            "type": MessageType.PUBMSG.value,
            "line": "J57wPBk1VfjSP5Te!PUBLIC!sw0reloffer 0 100000 1000000 500 0.001",
        }
    ).encode()
    sent = False

    async def healthy_receive() -> bytes:
        nonlocal sent
        if not sent:
            sent = True
            return frame
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    healthy.connection.receive.side_effect = healthy_receive
    mdc = _make_client()
    mdc.clients = {"stalled": stalled, "healthy": healthy}
    async with asyncio.timeout(0.8):
        offers = await mdc.fetch_orderbook(max_wait=0.03, min_wait=0, quiet_period=0)

    assert [(offer.counterparty, offer.oid) for offer in offers] == [("J57wPBk1VfjSP5Te", 0)]
    assert stalled._peerlist_inflight is None
    assert stalled._peerlist_timeout_count == 1


@pytest.mark.asyncio
async def test_fetch_orderbook_prefers_renewed_certificate_across_directories():
    stale_client = MagicMock()
    stale_client.fetch_orderbooks = AsyncMock(return_value=([_offer(901_152)], []))
    renewed_client = MagicMock()
    renewed_client.fetch_orderbooks = AsyncMock(return_value=([_offer(903_168)], []))
    mdc = _make_client(clients={"stale": stale_client, "renewed": renewed_client})

    offers = await mdc.fetch_orderbook(max_wait=0, min_wait=0, quiet_period=0)

    assert len(offers) == 1
    assert offers[0].fidelity_bond_data is not None
    assert offers[0].fidelity_bond_data["cert_expiry"] == 903_168


@pytest.mark.asyncio
@pytest.mark.parametrize("directory_order", [("positive", "renewed"), ("renewed", "positive")])
async def test_fetch_orderbook_merges_positive_feature_evidence(
    directory_order: tuple[str, str],
) -> None:
    positive = _offer(
        901_152,
        features={"neutrino_compat": True, "peerlist_features": True},
        neutrino_compat=True,
    )
    renewed = _offer(
        903_168,
        features={"neutrino_compat": False, "peerlist_features": False},
    )
    offers_by_name = {"positive": positive, "renewed": renewed}
    clients: dict[str, MagicMock] = {}
    for name in directory_order:
        client = MagicMock()
        client.fetch_orderbooks = AsyncMock(return_value=([offers_by_name[name]], []))
        clients[name] = client
    mdc = _make_client(clients=clients)

    offers = await mdc.fetch_orderbook(max_wait=0, min_wait=0, quiet_period=0)

    assert len(offers) == 1
    assert offers[0].fidelity_bond_data is not None
    assert offers[0].fidelity_bond_data["cert_expiry"] == 903_168
    assert offers[0].features == {"neutrino_compat": True, "peerlist_features": True}
    assert offers[0].neutrino_compat is True
    assert renewed.features == {"neutrino_compat": False, "peerlist_features": False}


def test_bind_session_returns_none_when_no_directories():
    mdc = _make_client()
    assert mdc.bind_session("J5x") is None


def test_bind_session_prefers_direct_when_connected_and_preferred():
    peer = MagicMock()
    peer.is_connected.return_value = True
    peer.peer_location = "abc.onion:1234"

    dir_client = MagicMock()
    dir_client._active_peers = {"J5x": "abc.onion:1234"}

    mdc = _make_client(
        clients={"dir1": dir_client},
        peer_connections={"J5x": peer},
        prefer_direct=True,
    )
    binding = mdc.bind_session("J5x")
    assert binding == ChannelBinding(
        nick="J5x", channel_id="direct", peer_location="abc.onion:1234"
    )
    assert binding.is_direct is True


def test_bind_session_does_not_use_direct_when_disabled():
    peer = MagicMock()
    peer.is_connected.return_value = True
    peer.peer_location = "abc.onion:1234"

    dir_client = MagicMock()
    dir_client._active_peers = {"J5x": "abc.onion:1234"}

    mdc = _make_client(
        clients={"dir1": dir_client},
        peer_connections={"J5x": peer},
        active_nicks={"J5x": {"dir1": True}},
        prefer_direct=False,
    )
    binding = mdc.bind_session("J5x")
    assert binding is not None
    assert binding.channel_id == "directory:dir1"
    assert binding.is_direct is False


def test_bind_session_prefers_active_nick_directory():
    d1 = MagicMock()
    d1._active_peers = {}
    d2 = MagicMock()
    d2._active_peers = {}
    mdc = _make_client(
        clients={"dirA": d1, "dirB": d2},
        active_nicks={"J5y": {"dirA": False, "dirB": True}},
        prefer_direct=True,
    )
    binding = mdc.bind_session("J5y")
    assert binding is not None
    assert binding.channel_id == "directory:dirB"


def test_bind_session_falls_back_to_peerlist_match():
    d1 = MagicMock()
    d1._active_peers = {}
    d2 = MagicMock()
    d2._active_peers = {"J5z": "host.onion:1"}
    mdc = _make_client(
        clients={"dirA": d1, "dirB": d2},
        prefer_direct=True,
    )
    binding = mdc.bind_session("J5z")
    assert binding is not None
    assert binding.channel_id == "directory:dirB"
    assert binding.peer_location == "host.onion:1"


def test_bind_session_last_resort_any_connected_directory():
    d1 = MagicMock()
    d1._active_peers = {}
    mdc = _make_client(clients={"dirA": d1}, prefer_direct=True)
    binding = mdc.bind_session("J5unknown")
    assert binding is not None
    assert binding.channel_id == "directory:dirA"
    assert binding.peer_location is None


@pytest.mark.parametrize(
    "location",
    ["127.0.0.1:5222", "maker.example:5222", "192.0.2.1:5222"],
)
def test_non_onion_peerlist_location_is_ignored_in_production(location: str):
    directory = MagicMock()
    directory._active_peers = {"J5x": location}
    mdc = _make_client(clients={"dir": directory})

    assert mdc.get_peer_location("J5x") is None


def test_regtest_allows_local_peerlist_location():
    directory = MagicMock()
    directory._active_peers = {"J5x": "127.0.0.1:5222"}
    mdc = _make_client(clients={"dir": directory})
    mdc.network = "regtest"

    assert mdc.get_peer_location("J5x") == "127.0.0.1:5222"


def test_clearnet_development_override_reaches_directory_clients():
    mdc = MultiDirectoryClient(
        directory_servers=["directory.example:5222"],
        network="mainnet",
        nick_identity=NickIdentity(private_key_bytes=b"\x01" * 32),
        allow_clearnet_connections=True,
    )

    assert mdc._build_client_kwargs("directory.example", 5222)["allow_clearnet_connections"] is True


def test_channel_binding_is_frozen():
    cb = ChannelBinding(nick="n", channel_id="direct")
    with pytest.raises((AttributeError, TypeError)):
        cb.nick = "other"  # type: ignore[misc]


def test_upgrade_channel_prefers_direct_once_connected():
    """A directory session upgrades to direct once a peer handshakes."""
    peer = MagicMock()
    peer.is_connected.return_value = True
    mdc = _make_client(peer_connections={"J5x": peer}, prefer_direct=True)
    assert mdc.upgrade_channel_prefer_direct("J5x", "directory:dir1") == "direct"


def test_upgrade_channel_keeps_directory_when_no_peer():
    """Without a handshaked peer, the directory channel is kept."""
    mdc = _make_client(prefer_direct=True)
    assert mdc.upgrade_channel_prefer_direct("J5x", "directory:dir1") == "directory:dir1"


def test_upgrade_channel_never_downgrades_direct():
    """An already-direct session is never downgraded to a directory."""
    mdc = _make_client(prefer_direct=True)
    assert mdc.upgrade_channel_prefer_direct("J5x", "direct") == "direct"


def test_upgrade_channel_noop_when_direct_disabled():
    """With direct connections disabled, the channel is left unchanged."""
    peer = MagicMock()
    peer.is_connected.return_value = True
    mdc = _make_client(peer_connections={"J5x": peer}, prefer_direct=False)
    assert mdc.upgrade_channel_prefer_direct("J5x", "directory:dir1") == "directory:dir1"
