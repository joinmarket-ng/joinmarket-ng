"""Collect authenticated signatures across a real TCP connection replacement."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from _taker_test_helpers import make_directory_client
from jmcore.crypto import NickIdentity
from jmcore.network import ONION_HOSTID, OnionPeer
from jmcore.protocol import MessageType, create_handshake_request


async def test_signature_collection_survives_reconnect_and_late_old_socket_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = make_directory_client()
    client.clients = {}
    identity = NickIdentity(5)
    handshake = {
        "type": MessageType.HANDSHAKE.value,
        "line": json.dumps(create_handshake_request(identity.nick, "NOT-SERVING-ONION", "regtest")),
    }
    frames = [
        {
            "type": MessageType.PRIVMSG.value,
            "line": f"{identity.nick}!{client.nick}!sig "
            + identity.sign_message(payload, ONION_HOSTID),
        }
        for payload in ("signature-one", "signature-two")
    ]
    drop_first, finish = asyncio.Event(), asyncio.Event()
    first_queued, partial_collected = asyncio.Event(), asyncio.Event()
    old_close_started, release_old_close = asyncio.Event(), asyncio.Event()
    server_done = [asyncio.Event(), asyncio.Event()]
    accepted = 0

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal accepted
        index = accepted
        accepted += 1
        try:
            await reader.readline()
            writer.write(json.dumps(handshake).encode() + b"\n")
            # Reconnect replays the first signature before supplying the second.
            for frame in frames[:1] if index == 0 else frames:
                writer.write(json.dumps(frame).encode() + b"\n")
            await writer.drain()
            await (drop_first if index == 0 else finish).wait()
        finally:
            writer.close()
            await writer.wait_closed()
            server_done[index].set()

    async def receive(nick: str, data: bytes) -> None:
        await client._on_peer_message(nick, data)
        first_queued.set()

    async def after_first_signature(silent: set[str]) -> None:
        assert not silent  # First signature was processed, but the set is incomplete.
        partial_collected.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    peer = OnionPeer(
        identity.nick,
        f"127.0.0.1:{port}",
        on_message=receive,
        on_disconnect=client._on_peer_disconnect,
    )
    client._peer_connections[identity.nick] = peer
    collector: asyncio.Task[dict[str, dict[str, Any]]] | None = None
    old_receiver: asyncio.Task[None] | None = None
    try:
        assert await peer.connect(client.nick, "NOT-SERVING-ONION", "regtest")
        await asyncio.wait_for(first_queued.wait(), 3)
        old_connection = peer._connection
        old_receiver = peer._receive_task
        assert old_connection is not None and old_receiver is not None
        real_close = old_connection.close

        async def delayed_close() -> None:
            old_close_started.set()
            await release_old_close.wait()
            await real_close()

        monkeypatch.setattr(old_connection, "close", delayed_close)
        collector = asyncio.create_task(
            client.wait_for_responses(
                [identity.nick],
                "!sig",
                timeout=5,
                expected_counts={identity.nick: 2},
                on_stalled=(0.01, after_first_signature),
            )
        )
        await asyncio.wait_for(partial_collected.wait(), 3)
        drop_first.set()
        await asyncio.wait_for(old_close_started.wait(), 3)
        assert not peer.is_connected()
        assert await peer.connect(client.nick, "NOT-SERVING-ONION", "regtest")
        replacement = peer._connection
        assert replacement is not None and replacement is not old_connection
        release_old_close.set()
        await asyncio.wait_for(old_receiver, 3)
        result = await asyncio.wait_for(collector, 6)
        assert [data.split()[0] for data in result[identity.nick]["data"]] == [
            "signature-one",
            "signature-two",
        ]
        assert client.get_connected_peer(identity.nick) is peer
        assert peer._connection is replacement
        assert replacement.is_connected()
    finally:
        drop_first.set()
        release_old_close.set()
        finish.set()
        if collector is not None:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
        await peer.disconnect()
        if old_receiver is not None:
            await asyncio.gather(old_receiver, return_exceptions=True)
        server.close()
        await server.wait_closed()
        for done in server_done[:accepted]:
            await asyncio.wait_for(done.wait(), 3)
