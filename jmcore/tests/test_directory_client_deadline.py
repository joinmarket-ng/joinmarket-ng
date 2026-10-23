"""Operation deadlines retain collected offers and bound heartbeat writes too."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from jmcore.directory_client import DirectoryClient
from jmcore.protocol import MessageType


def message(kind: MessageType, line: str = "") -> bytes:
    return json.dumps({"type": kind.value, "line": line}).encode()


@pytest.mark.parametrize("blocked_pong", [False, True])
async def test_collection_deadline_returns_offers_even_without_quiet_exit(
    blocked_pong: bool,
) -> None:
    client = DirectoryClient("127.0.0.1", 1, "regtest")
    client.get_peerlist_with_features = AsyncMock(return_value=[])
    connection = Mock()
    connection.is_connected.return_value = True
    client.connection = connection
    incoming = [
        message(
            MessageType.PUBMSG,
            "J57wPBk1VfjSP5Te!PUBLIC!sw0reloffer 0 10000 1000000 0 0.001",
        )
    ]
    if blocked_pong:
        incoming.append(message(MessageType.PING))
    send_canceled = asyncio.Event()

    async def receive() -> bytes:
        if incoming:
            return incoming.pop(0)
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send(data: bytes) -> None:
        if json.loads(data)["type"] == MessageType.PONG.value:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                send_canceled.set()
                raise

    connection.receive = AsyncMock(side_effect=receive)
    connection.send = AsyncMock(side_effect=send)
    start = asyncio.get_running_loop().time()
    offers, _ = await client.fetch_orderbooks(
        max_wait=30, min_wait=30, quiet_period=0, deadline=start + 0.03
    )
    assert len(offers) == 1
    assert offers[0].counterparty == "J57wPBk1VfjSP5Te"
    assert asyncio.get_running_loop().time() - start < 0.5
    assert send_canceled.is_set() is blocked_pong


async def test_expired_deadline_does_not_send_a_request() -> None:
    client = DirectoryClient("127.0.0.1", 1, "regtest")
    client.get_peerlist_with_features = AsyncMock()
    assert await client.fetch_orderbooks(deadline=asyncio.get_running_loop().time() - 1) == (
        [],
        [],
    )
    client.get_peerlist_with_features.assert_not_awaited()


async def test_peerlist_is_inside_the_operation_deadline() -> None:
    client = DirectoryClient("127.0.0.1", 1, "regtest")

    async def peerlist() -> None:
        await asyncio.Event().wait()

    client.get_peerlist_with_features = AsyncMock(side_effect=peerlist)
    with pytest.raises(TimeoutError):
        await client.fetch_orderbooks(deadline=asyncio.get_running_loop().time() + 0.03)
