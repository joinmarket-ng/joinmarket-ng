"""Exercise optional-handshake handoff with real TCP framing, without Tor."""

from __future__ import annotations

import asyncio
import json

import pytest

from jmcore.network import OnionPeer
from jmcore.protocol import MessageType


@pytest.mark.parametrize("fragmented", [False, True])
async def test_first_private_frame_survives_optional_handshake_handoff(fragmented: bool) -> None:
    frames = [
        json.dumps({"type": MessageType.PRIVMSG.value, "line": line}).encode()
        for line in ("first-frame", "second-frame")
    ]
    release, finish, server_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
    initial_sent, received = asyncio.Event(), asyncio.Event()
    delivered: list[bytes] = []
    server_errors: list[Exception] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readline()  # The initiating handshake.
            first = frames[0] + b"\n"
            split = len(first) // 2 if fragmented else len(first)
            writer.write(first[:split])
            await writer.drain()
            initial_sent.set()
            await release.wait()
            writer.write(first[split:] + frames[1] + b"\n")
            await writer.drain()
            await finish.wait()
        except Exception as exc:
            server_errors.append(exc)
        finally:
            writer.close()
            await writer.wait_closed()
            server_done.set()

    async def on_message(nick: str, data: bytes) -> None:
        assert nick == "maker"
        delivered.append(data)
        if len(delivered) == 2:
            received.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    peer = OnionPeer("maker", f"127.0.0.1:{port}", timeout=0.1, on_message=on_message)
    try:
        assert await peer.connect("taker", "NOT-SERVING-ONION", "regtest")
        await asyncio.wait_for(initial_sent.wait(), 3)
        # In the fragmented case, the optional read has been canceled with
        # half a frame still buffered. The receive loop must recover all bytes.
        release.set()
        await asyncio.wait_for(received.wait(), 3)
        assert delivered == frames
        assert peer.is_connected()
        assert not server_errors
    finally:
        release.set()
        finish.set()
        await peer.disconnect()
        server.close()
        await server.wait_closed()
        await asyncio.wait_for(server_done.wait(), 3)
