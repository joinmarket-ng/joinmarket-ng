"""Pacing of wait_for_responses when directory connections are dead.

A closed directory connection makes ``listen_for_messages`` raise immediately.
Without pacing, the wait loop degenerates into a busy loop that spins thousands
of iterations per second (and logs one error line per client per iteration)
until the timeout expires.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from _taker_test_helpers import make_directory_client, register_direct_peer
from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClientError
from jmcore.network import ONION_HOSTID
from jmcore.protocol import MessageType
from loguru import logger

from taker.multi_directory import MultiDirectoryClient


class DeadClient:
    """Simulates a client whose connection is closed: fails instantly."""

    def __init__(self) -> None:
        self.calls = 0
        self.close_calls = 0

    async def listen_for_messages(self, duration: float) -> list[dict[str, Any]]:
        self.calls += 1
        raise DirectoryClientError("Connection closed")

    async def close(self) -> None:
        self.close_calls += 1


class HealthyClient:
    """Simulates a directory client that returns a predetermined response."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.calls = 0
        self.messages = messages

    async def listen_for_messages(self, duration: float) -> list[dict[str, Any]]:
        self.calls += 1
        return self.messages


class ReplacingDeadClient(DeadClient):
    """Simulates a stale listener replaced before it reports its failure."""

    def __init__(self, clients: dict[str, Any], server: str, replacement: Any) -> None:
        super().__init__()
        self.clients = clients
        self.server = server
        self.replacement = replacement

    async def listen_for_messages(self, duration: float) -> list[dict[str, Any]]:
        self.calls += 1
        self.clients[self.server] = self.replacement
        raise DirectoryClientError("Connection closed")


class DirectMessageDeadClient(DeadClient):
    """Queues a direct response before its terminal listener failure."""

    def __init__(self, client: MultiDirectoryClient, nick: str, message: dict[str, Any]) -> None:
        super().__init__()
        self.client = client
        self.nick = nick
        self.message = message

    async def listen_for_messages(self, duration: float) -> list[dict[str, Any]]:
        self.calls += 1
        await self.client._on_peer_message(self.nick, json.dumps(self.message).encode())
        raise DirectoryClientError("Connection closed")


def signed_line(identity: NickIdentity, recipient: str, command: str, data: str) -> str:
    """Build a signed private message returned by a directory listener."""
    return f"{identity.nick}!{recipient}!{command} {identity.sign_message(data, ONION_HOSTID)}"


@pytest.mark.asyncio
async def test_dead_directory_does_not_busy_loop() -> None:
    client = make_directory_client()
    dead = DeadClient()
    client.clients = {"dead.onion:5222": dead}

    warnings: list[dict[str, Any]] = []
    handler_id = logger.add(lambda message: warnings.append(message.record), level="WARNING")
    loop = asyncio.get_event_loop()
    start = loop.time()
    try:
        responses = await client.wait_for_responses(
            expected_nicks=["J5NeverResponds"],
            expected_command="!pubkey",
            timeout=0.6,
        )
        elapsed = loop.time() - start
    finally:
        logger.remove(handler_id)

    assert responses == {}
    assert dead.calls == 1
    assert dead.close_calls == 1
    assert client.clients == {}
    assert [
        record["message"] for record in warnings if record["message"].startswith("Error listening")
    ] == ["Error listening to dead.onion:5222: Connection closed"]
    # It must still wait out (roughly) the full timeout for direct messages.
    assert 0.5 <= elapsed < 5.0


@pytest.mark.asyncio
async def test_no_directory_clients_does_not_busy_loop() -> None:
    client = make_directory_client()
    client.clients = {}

    loop = asyncio.get_event_loop()
    start = loop.time()
    responses = await client.wait_for_responses(
        expected_nicks=["J5NeverResponds"],
        expected_command="!pubkey",
        timeout=0.5,
    )
    elapsed = loop.time() - start

    assert responses == {}
    assert 0.4 <= elapsed < 5.0


@pytest.mark.asyncio
async def test_terminal_listener_keeps_newer_replacement_client() -> None:
    client = make_directory_client()
    server = "directory.onion:5222"
    replacement = HealthyClient([])
    stale = ReplacingDeadClient({}, server, replacement)
    client.clients = {server: stale}
    stale.clients = client.clients

    await client.wait_for_responses(
        expected_nicks=["J5NeverResponds"],
        expected_command="!pubkey",
        timeout=0.1,
    )

    assert stale.calls == 1
    assert stale.close_calls == 1
    assert client.clients[server] is replacement


@pytest.mark.asyncio
async def test_terminal_listener_does_not_block_healthy_directory_response() -> None:
    client = make_directory_client()
    maker = NickIdentity(5)
    dead = DeadClient()
    healthy = HealthyClient(
        [
            {
                "type": MessageType.PRIVMSG.value,
                "line": signed_line(
                    maker,
                    client.nick_identity.nick,
                    "pubkey",
                    "MAKER_NACL",
                ),
            }
        ]
    )
    client.clients = {"dead.onion:5222": dead, "healthy.onion:5222": healthy}

    responses = await client.wait_for_responses(
        expected_nicks=[maker.nick],
        expected_command="!pubkey",
        timeout=0.5,
    )

    assert responses[maker.nick]["data"].split()[0] == "MAKER_NACL"
    assert dead.calls == 1
    assert dead.close_calls == 1
    assert healthy.calls == 1
    assert list(client.clients.values()) == [healthy]


@pytest.mark.asyncio
async def test_direct_response_arriving_after_eviction_is_processed() -> None:
    client = make_directory_client()
    maker = NickIdentity(5)
    direct_message = {
        "type": MessageType.PRIVMSG.value,
        "line": signed_line(maker, client.nick_identity.nick, "pubkey", "MAKER_NACL"),
    }
    register_direct_peer(client, maker.nick)
    dead = DirectMessageDeadClient(client, maker.nick, direct_message)
    client.clients = {"dead.onion:5222": dead}

    responses = await client.wait_for_responses(
        expected_nicks=[maker.nick],
        expected_command="!pubkey",
        timeout=1.2,
    )

    assert responses[maker.nick]["data"].split()[0] == "MAKER_NACL"
    assert dead.close_calls == 1
    assert client.clients == {}
