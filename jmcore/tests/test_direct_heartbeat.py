"""Direct heartbeat compatibility, liveness, and connection ownership."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import jmcore.network as network
from jmcore.network import OnionPeer, TCPConnection
from jmcore.protocol import FEATURE_DIRECT_PING_V1, MessageType, create_handshake_request

Transport = tuple[MagicMock, asyncio.Queue[bytes | None], asyncio.Event]


def handshake(features: dict[str, object]) -> bytes:
    payload = create_handshake_request("maker", "peer.onion:5222", "regtest")
    payload["features"] = features
    return json.dumps({"type": MessageType.HANDSHAKE.value, "line": json.dumps(payload)}).encode()


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> Transport:
    incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
    closed = asyncio.Event()
    connection = MagicMock(spec=TCPConnection)

    async def receive() -> bytes:
        message = await incoming.get()
        if message is None:
            raise network.ConnectionError("closed")
        return message

    async def close() -> None:
        closed.set()
        incoming.put_nowait(None)

    def abort() -> None:
        closed.set()
        incoming.put_nowait(None)

    connection.receive = AsyncMock(side_effect=receive)
    connection.send = AsyncMock()
    connection.close = AsyncMock(side_effect=close)
    connection.abort.side_effect = abort
    connection.is_connected.side_effect = lambda: not closed.is_set()
    monkeypatch.setattr(network, "connect_direct", AsyncMock(return_value=connection))
    monkeypatch.setattr(network, "_DIRECT_PING_INTERVAL_SEC", 0.01)
    monkeypatch.setattr(network, "_DIRECT_PING_TIMEOUT_SEC", 0.05)
    return connection, incoming, closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "features",
    [{}, {"ping": True}, {FEATURE_DIRECT_PING_V1: False}, {FEATURE_DIRECT_PING_V1: "true"}],
)
async def test_unknown_legacy_or_nonboolean_capability_sends_no_heartbeat(
    transport: Transport, features: dict[str, object]
) -> None:
    connection, incoming, _closed = transport
    incoming.put_nowait(handshake(features))
    peer = OnionPeer("maker", "127.0.0.1:5222")
    try:
        assert await peer.connect("taker", "NOT-SERVING-ONION", "regtest")
        await asyncio.sleep(0.06)
        assert connection.send.await_count == 1  # Handshake only.
    finally:
        await peer.disconnect()
    assert peer._receive_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["missing", "wrong", "null", "extra", "stale"])
async def test_missing_or_uncorrelated_pong_cannot_keep_connection_alive(
    transport: Transport, response: str
) -> None:
    connection, incoming, closed = transport
    incoming.put_nowait(handshake({FEATURE_DIRECT_PING_V1: True}))
    nonces: list[str] = []

    async def send(data: bytes) -> None:
        message = json.loads(data)
        if message["type"] != MessageType.PING.value:
            return
        nonce = message["line"]
        nonces.append(nonce)
        if response == "missing":
            return
        wrong = ("1" if nonce[0] == "0" else "0") + nonce[1:]
        answer: dict[str, object] = {"type": MessageType.PONG.value, "line": wrong}
        if response == "null":
            answer["line"] = None
        elif response == "extra":
            answer.update(line=nonce, extra="not negotiated")
        elif response == "stale":
            answer["line"] = nonces[0]
        incoming.put_nowait(json.dumps(answer).encode())

    connection.send.side_effect = send
    delivered = AsyncMock()
    peer = OnionPeer("maker", "127.0.0.1:5222", on_message=delivered)
    try:
        assert await peer.connect("taker", "NOT-SERVING-ONION", "regtest")
        await asyncio.wait_for(closed.wait(), 1)
        assert len(set(nonces)) == (2 if response == "stale" else 1)
        delivered.assert_not_awaited()
    finally:
        await peer.disconnect()


@pytest.mark.asyncio
async def test_blocked_heartbeat_write_is_bounded(transport: Transport) -> None:
    connection, incoming, closed = transport
    incoming.put_nowait(handshake({FEATURE_DIRECT_PING_V1: True}))

    async def send(data: bytes) -> None:
        if json.loads(data)["type"] == MessageType.PING.value:
            await asyncio.Event().wait()

    connection.send.side_effect = send
    peer = OnionPeer("maker", "127.0.0.1:5222")
    try:
        assert await peer.connect("taker", "NOT-SERVING-ONION", "regtest")
        await asyncio.wait_for(closed.wait(), 1)
    finally:
        await peer.disconnect()


@pytest.mark.asyncio
async def test_close_error_after_heartbeat_failure_still_releases_peer(
    transport: Transport,
) -> None:
    connection, incoming, _closed = transport
    incoming.put_nowait(handshake({FEATURE_DIRECT_PING_V1: True}))
    connection.close.side_effect = OSError("transport close failed")
    disconnected = AsyncMock()
    peer = OnionPeer("maker", "127.0.0.1:5222", on_disconnect=disconnected)
    assert await peer.connect("taker", "NOT-SERVING-ONION", "regtest")
    assert peer._receive_task is not None
    await asyncio.wait_for(peer._receive_task, 1)
    assert not peer.is_connected()
    assert peer._connection is None
    disconnected.assert_awaited_once_with("maker")
    await peer.disconnect()


@pytest.mark.asyncio
async def test_hanging_close_after_abort_is_bounded_and_preserves_replacement(
    transport: Transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, incoming, _closed = transport
    incoming.put_nowait(handshake({FEATURE_DIRECT_PING_V1: True}))
    closing = asyncio.Event()

    async def close() -> None:
        closing.set()
        await asyncio.Event().wait()

    connection.close.side_effect = close
    monkeypatch.setattr(network, "_DIRECT_CLOSE_TIMEOUT_SEC", 0.02)
    disconnected = AsyncMock()
    peer = OnionPeer("maker", "127.0.0.1:5222", on_disconnect=disconnected)
    replacement = MagicMock(spec=TCPConnection)
    replacement.close = AsyncMock()
    try:
        assert await peer.connect("taker", "NOT-SERVING-ONION", "regtest")
        await asyncio.wait_for(closing.wait(), 1)
        assert not peer.is_connected()
        assert peer._connection is None
        peer._connection = replacement
        assert peer._receive_task is not None
        await asyncio.wait_for(peer._receive_task, 1)
        disconnected.assert_awaited_once_with("maker")
        replacement.close.assert_not_awaited()
        assert peer._connection is replacement
    finally:
        peer._connection = None
        await peer.disconnect()


@pytest.mark.asyncio
async def test_late_reciprocal_handshake_can_enable_keepalives(transport: Transport) -> None:
    connection, incoming, _closed = transport
    sent = asyncio.Event()

    async def send(data: bytes) -> None:
        message = json.loads(data)
        if message["type"] == MessageType.PING.value:
            incoming.put_nowait(
                json.dumps({"type": MessageType.PONG.value, "line": message["line"]}).encode()
            )
            sent.set()

    connection.send.side_effect = send
    peer = OnionPeer("maker", "127.0.0.1:5222", timeout=0.01)
    try:
        assert await peer.connect("taker", "NOT-SERVING-ONION", "regtest")
        assert peer.peer_features == {}
        incoming.put_nowait(handshake({FEATURE_DIRECT_PING_V1: True}))
        await asyncio.wait_for(sent.wait(), 1)
        assert peer.is_connected()
    finally:
        await peer.disconnect()


@pytest.mark.asyncio
async def test_old_heartbeat_closes_only_its_original_socket(transport: Transport) -> None:
    connection, incoming, closed = transport
    incoming.put_nowait(handshake({FEATURE_DIRECT_PING_V1: True}))
    sent = asyncio.Event()

    async def send(data: bytes) -> None:
        if json.loads(data)["type"] == MessageType.PING.value:
            sent.set()

    connection.send.side_effect = send
    replacement = MagicMock(spec=TCPConnection)
    replacement.close = AsyncMock()
    peer = OnionPeer("maker", "127.0.0.1:5222")
    try:
        assert await peer.connect("taker", "NOT-SERVING-ONION", "regtest")
        await asyncio.wait_for(sent.wait(), 1)
        # Model a replacement installed before the old receive task unwinds.
        peer._connection = replacement
        assert peer._receive_task is not None
        await asyncio.wait_for(peer._receive_task, 1)
        assert closed.is_set()
        replacement.close.assert_not_awaited()
        assert peer.is_connected()
        assert peer._connection is replacement
    finally:
        await peer.disconnect()


@pytest.mark.asyncio
async def test_old_late_handshake_cannot_change_replacement_capabilities(
    transport: Transport,
) -> None:
    connection, incoming, _closed = transport
    peer = OnionPeer("maker", "127.0.0.1:5222", timeout=0.01)
    replacement = MagicMock(spec=TCPConnection)
    replacement.close = AsyncMock()
    try:
        assert await peer.connect("taker", "NOT-SERVING-ONION", "regtest")
        # Let the old receive task start waiting before installing a replacement.
        await asyncio.sleep(0)
        peer._connection = replacement
        incoming.put_nowait(handshake({FEATURE_DIRECT_PING_V1: True}))
        assert peer._receive_task is not None
        await asyncio.wait_for(peer._receive_task, 1)
        assert peer.peer_features == {}
        assert peer.is_connected()
        replacement.close.assert_not_awaited()
    finally:
        connection.abort()
        await peer.disconnect()
