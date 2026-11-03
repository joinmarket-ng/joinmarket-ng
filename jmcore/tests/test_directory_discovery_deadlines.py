"""Absolute discovery deadlines preserve partial data without authorizing pruning."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from unittest.mock import AsyncMock, Mock

import pytest

from jmcore.directory_client import DirectoryClient, DirectoryClientError
from jmcore.protocol import MessageType

MAKER = "J57wPBk1VfjSP5Te"


def _frame(message_type: MessageType, line: str = "") -> bytes:
    return json.dumps({"type": message_type.value, "line": line}).encode()


def _client() -> tuple[DirectoryClient, AsyncMock]:
    connection = AsyncMock()
    connection.abort = Mock()
    connection.is_connected = Mock(return_value=True)
    client = DirectoryClient("host", 1234, "regtest")
    client.connection = connection
    client.directory_peerlist_features = True
    client._peerlist_timeout = 0.06
    client._peerlist_chunk_timeout = 0.04
    client._active_peers = {"retained": "retained.onion:5222"}
    return client, connection


@pytest.mark.asyncio
async def test_peerlist_synchronous_send_expiry_preserves_no_response_bookkeeping() -> None:
    client, connection = _client()

    async def send(_data: bytes) -> None:
        time.sleep(0.12)

    connection.send.side_effect = send
    result = await client._fetch_peerlist_result()

    assert result.snapshot is None
    assert result.peers == ()
    assert client._peerlist_timeout_count == 1
    assert client._peerlist_supported is None
    connection.receive.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sink", [False, True])
@pytest.mark.parametrize("announces_features", [False, True])
async def test_peerlist_deadline_covers_blocked_request_send(
    sink: bool,
    announces_features: bool,
) -> None:
    client, connection = _client()
    client._listen_loop_active = sink
    client.directory_peerlist_features = announces_features
    client.timeout = 0.06
    blocked = asyncio.Event()

    async def send(_data: bytes) -> None:
        await blocked.wait()

    connection.send.side_effect = send
    async with asyncio.timeout(0.8):
        result = await client._fetch_peerlist_result()

    assert result.snapshot is None
    assert result.peers == ()
    assert client._peerlist_timeout_count == 1
    assert client._peerlist_supported is (None if announces_features else False)
    assert client._peerlist_inflight is None
    assert client._peerlist_inflight_bytes == 0
    assert client.get_active_nicks() == {"retained"}
    connection.receive.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("sink", [False, True])
@pytest.mark.parametrize("drip", ["", "peerlist_features"])
async def test_peerlist_drip_cannot_extend_total_deadline(sink: bool, drip: str) -> None:
    client, connection = _client()
    client._listen_loop_active = sink
    first = f"{MAKER};maker.onion:5222"
    sent_first = False
    request_sent = asyncio.Event()

    async def receive() -> bytes:
        nonlocal sent_first
        await asyncio.sleep(0.005)
        line = drip if sent_first else first
        sent_first = True
        return _frame(MessageType.PEERLIST, line)

    async def send(_data: bytes) -> None:
        request_sent.set()

    async def feed_sink() -> None:
        await request_sent.wait()
        client._enqueue_peerlist_chunk(first)
        while True:
            await asyncio.sleep(0.005)
            client._enqueue_peerlist_chunk(drip)

    connection.send.side_effect = send
    connection.receive.side_effect = receive
    feeder = asyncio.create_task(feed_sink()) if sink else None
    try:
        async with asyncio.timeout(0.8):
            result = await client._fetch_peerlist_result()
    finally:
        if feeder is not None:
            feeder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await feeder

    assert result.snapshot is None
    assert [nick for nick, _, _ in result.peers] == [MAKER]
    assert client.get_active_nicks() == {"retained", MAKER}
    assert client._peerlist_supported is True
    assert client._peerlist_timeout_count == 0
    assert client._peerlist_inflight is None
    assert client._peerlist_inflight_bytes == 0
    if sink:
        connection.receive.assert_not_awaited()


@pytest.mark.asyncio
async def test_peerlist_deadline_covers_blocked_pong_after_partial_response() -> None:
    client, connection = _client()
    connection.receive.side_effect = [
        _frame(MessageType.PEERLIST, f"{MAKER};maker.onion:5222"),
        _frame(MessageType.PING),
    ]

    async def send(data: bytes) -> None:
        if json.loads(data)["type"] == MessageType.PONG.value:
            await asyncio.Event().wait()

    connection.send.side_effect = send
    async with asyncio.timeout(0.8):
        result = await client._fetch_peerlist_result()

    assert result.snapshot is None
    assert [nick for nick, _, _ in result.peers] == [MAKER]
    assert client.get_active_nicks() == {"retained", MAKER}


@pytest.mark.asyncio
async def test_peerlist_processing_time_counts_toward_total_deadline() -> None:
    client, connection = _client()
    connection.receive.side_effect = [
        _frame(MessageType.PEERLIST, f"{MAKER};maker.onion:5222"),
        TimeoutError(),
    ]
    process = client._process_peerlist_response

    def slow_process(line: str) -> tuple[list, bool]:
        result = process(line)
        # Deliberately block the event loop to model synchronous chunk work.
        time.sleep(0.12)
        return result

    client._process_peerlist_response = slow_process  # type: ignore[method-assign]
    async with asyncio.timeout(0.8):
        result = await client._fetch_peerlist_result()
    assert result.snapshot is None
    assert [nick for nick, _, _ in result.peers] == [MAKER]
    assert client.get_active_nicks() == {"retained", MAKER}


@pytest.mark.asyncio
@pytest.mark.parametrize("line", ["", f"{MAKER};maker.onion:5222"])
async def test_normal_first_response_and_quiet_period_still_complete(line: str) -> None:
    client, connection = _client()
    sent = False

    async def receive() -> bytes:
        nonlocal sent
        if not sent:
            sent = True
            await asyncio.sleep(0.02)
            return _frame(MessageType.PEERLIST, line)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    connection.receive.side_effect = receive
    async with asyncio.timeout(0.8):
        result = await client._fetch_peerlist_result()

    assert result.snapshot is not None
    assert result.snapshot.nicks == ({MAKER} if line else set())
    assert client.get_active_nicks() == result.snapshot.nicks


@pytest.mark.asyncio
async def test_orderbook_deadline_covers_blocked_broadcast_send() -> None:
    client, connection = _client()
    client.get_peerlist_with_features = AsyncMock(return_value=[])  # type: ignore[method-assign]

    async def send(_data: bytes) -> None:
        await asyncio.Event().wait()

    connection.send.side_effect = send
    async with asyncio.timeout(0.8):
        with pytest.raises(DirectoryClientError, match="Timed out sending orderbook request"):
            await client.fetch_orderbooks(max_wait=0.06)
    connection.receive.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_pong", [False, True])
async def test_orderbook_deadline_retains_final_chunk(blocked_pong: bool) -> None:
    client, connection = _client()
    client.get_peerlist_with_features = AsyncMock(return_value=[])  # type: ignore[method-assign]
    frames = [_frame(MessageType.PUBMSG, f"{MAKER}!PUBLIC!sw0reloffer 0 100000 1000000 500 0.001")]
    if blocked_pong:
        frames.append(_frame(MessageType.PING))

    async def receive() -> bytes:
        if frames:
            return frames.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(data: bytes) -> None:
        if json.loads(data)["type"] == MessageType.PONG.value:
            await asyncio.Event().wait()

    connection.receive.side_effect = receive
    connection.send.side_effect = send
    async with asyncio.timeout(0.8):
        offers, bonds = await client.fetch_orderbooks(max_wait=0.06, min_wait=0, quiet_period=0)

    assert [(offer.counterparty, offer.oid) for offer in offers] == [(MAKER, 0)]
    assert bonds == []
    assert client.connection is connection
    connection.abort.assert_not_called()


@pytest.mark.asyncio
async def test_listen_caller_cancellation_is_not_partial_success() -> None:
    client, connection = _client()
    entered_receive = asyncio.Event()

    async def receive() -> bytes:
        entered_receive.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    connection.receive.side_effect = receive
    task = asyncio.create_task(client.listen_for_messages(duration=60))
    await entered_receive.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
