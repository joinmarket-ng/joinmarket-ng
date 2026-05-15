"""Tests for MultiDirectoryClient.bind_session() channel selection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

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
    return mdc


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


def test_channel_binding_is_frozen():
    cb = ChannelBinding(nick="n", channel_id="direct")
    with pytest.raises((AttributeError, TypeError)):
        cb.nick = "other"  # type: ignore[misc]
