from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable

import pytest
from jmcore.models import MessageEnvelope, NetworkType
from jmcore.protocol import JM_VERSION, MessageType
from jmcore.settings import DirectoryServerSettings

from directory_server.server import DirectoryServer


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1.0):
        while not predicate():
            await asyncio.sleep(0.01)


async def _connect_without_handshake(
    port: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.open_connection("127.0.0.1", port)


@pytest.mark.anyio
async def test_pending_handshake_admission_is_bounded_and_reclaimed() -> None:
    settings = DirectoryServerSettings(
        host="127.0.0.1",
        port=0,
        max_peers=2,
        health_check_port=0,
    )
    server = DirectoryServer(settings, NetworkType.MAINNET, "J5TestNickOOOOOO")
    server.server = await asyncio.start_server(
        server._client_connected, settings.host, settings.port
    )
    port = server.server.sockets[0].getsockname()[1]
    writers: list[asyncio.StreamWriter] = []

    try:
        established_reader, established_writer = await _connect_without_handshake(port)
        writers.append(established_writer)
        handshake = MessageEnvelope(
            message_type=MessageType.HANDSHAKE,
            payload=json.dumps(
                {
                    "app-name": "joinmarket",
                    "directory": False,
                    "location-string": "NOT-SERVING-ONION",
                    "proto-ver": JM_VERSION,
                    "features": {},
                    "nick": "established",
                    "network": "mainnet",
                }
            ),
        )
        established_writer.write(handshake.to_bytes() + b"\n")
        await established_writer.drain()
        response = MessageEnvelope.from_bytes(
            (await asyncio.wait_for(established_reader.readuntil(b"\n"), timeout=1.0)).rstrip(b"\n")
        )
        assert response.message_type == MessageType.DN_HANDSHAKE
        await _wait_until(lambda: server.peer_registry.count() == 1)
        await _wait_until(lambda: server._pending_handshakes == 0)

        pending_reader_one, pending_writer_one = await _connect_without_handshake(port)
        pending_reader_two, pending_writer_two = await _connect_without_handshake(port)
        writers.extend((pending_writer_one, pending_writer_two))
        await _wait_until(lambda: server._pending_handshakes == 2)
        assert server.connections.max_connections == 4
        assert len(server.connections) == 3

        established_connection_id = server.peer_registry.get_connection_id("established")
        assert established_connection_id is not None
        await server._send_to_peer("established", b"still-connected", established_connection_id)
        assert await asyncio.wait_for(established_reader.readuntil(b"\n"), timeout=1.0) == (
            b"still-connected\r\n"
        )

        rejected_reader, rejected_writer = await _connect_without_handshake(port)
        writers.append(rejected_writer)
        assert await asyncio.wait_for(rejected_reader.read(), timeout=1.0) == b""

        pending_writer_one.close()
        await pending_writer_one.wait_closed()
        await _wait_until(lambda: server._pending_handshakes == 1)

        _pending_reader_three, pending_writer_three = await _connect_without_handshake(port)
        writers.append(pending_writer_three)
        await _wait_until(lambda: server._pending_handshakes == 2)
    finally:
        for writer in writers:
            writer.close()
        for writer in writers:
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        await server.stop()
