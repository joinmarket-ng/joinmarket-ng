"""Focused lifecycle limits for maker direct peer sockets."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore.crypto import NickIdentity
from jmcore.models import NetworkType
from jmcore.network import ONION_HOSTID, TCPConnection
from jmcore.protocol import JM_VERSION, MessageType, create_handshake_request

import maker.direct_connection as direct_connection
from maker.bot import MakerBot
from maker.config import MakerConfig
from maker.direct_connection import DirectConnectionState
from maker.generation import MakerGeneration


@pytest.fixture
def bot() -> MakerBot:
    backend = MagicMock()
    backend.can_provide_neutrino_metadata.return_value = False
    return MakerBot(
        wallet=MagicMock(),
        backend=backend,
        config=MakerConfig(
            mnemonic="test " * 12,
            network=NetworkType.REGTEST,
            directory_servers=["directory.onion:5222"],
        ),
    )


def _connection() -> MagicMock:
    connection = MagicMock(spec=TCPConnection)
    connection.close = AsyncMock()
    return connection


def _generation(bot: MakerBot, generation_id: int) -> MakerGeneration:
    return MakerGeneration(
        generation_id=generation_id,
        nick_identity=NickIdentity(JM_VERSION),
        offer_manager=bot.offer_manager,
        directory_pool=bot._directory_pool,
    )


def _handshake(nick: str) -> bytes:
    handshake = create_handshake_request(
        nick=nick,
        location="NOT-SERVING-ONION",
        network=NetworkType.REGTEST.value,
        directory=False,
    )
    return json.dumps({"type": MessageType.HANDSHAKE.value, "line": json.dumps(handshake)}).encode()


def _signed_message(identity: NickIdentity, recipient: str, command: str, data: str) -> bytes:
    signed = identity.sign_message(data, ONION_HOSTID)
    return json.dumps(
        {
            "type": MessageType.PRIVMSG.value,
            "line": f"{identity.nick}!{recipient}!{command} {signed}",
        }
    ).encode()


@pytest.mark.asyncio
async def test_socket_limit_counts_generation_states_not_nick_hints(bot: MakerBot) -> None:
    """Duplicate nick hints cannot bypass the process-wide direct socket cap."""
    replacement = _generation(bot, 1)
    bot.generations[1] = replacement
    newer_hint = _connection()
    replacement.direct_connections["J5Duplicate"] = newer_hint

    for generation in bot.generations.values():
        for _ in range(128):
            generation.direct_connection_states[_connection()] = DirectConnectionState(
                nick="J5Duplicate", verified=True
            )

    incoming = _connection()
    await bot._on_direct_connection(incoming, "peer:1", generation_id=1)

    assert (
        sum(len(generation.direct_connection_states) for generation in bot.generations.values())
        == 256
    )
    assert incoming not in replacement.direct_connection_states
    assert replacement.direct_connections["J5Duplicate"] is newer_hint
    incoming.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_connection_slot_is_reclaimed_after_handler_error(bot: MakerBot, monkeypatch) -> None:
    monkeypatch.setattr(direct_connection, "_MAX_DIRECT_CONNECTIONS", 1)
    bot.running = True
    failed = _connection()
    failed.is_connected.return_value = True
    failed.receive = AsyncMock(side_effect=RuntimeError("receive failed"))

    await bot._on_direct_connection(failed, "failed:1")

    assert bot.generations[0].direct_connection_states == {}
    failed.close.assert_awaited_once_with()

    replacement = _connection()
    replacement.is_connected.return_value = True
    replacement.receive = AsyncMock(return_value=b"")
    await bot._on_direct_connection(replacement, "replacement:1")

    replacement.receive.assert_awaited_once_with()
    replacement.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_connection_slot_is_reclaimed_when_handler_is_cancelled(bot: MakerBot) -> None:
    bot.running = True
    connection = _connection()
    connection.is_connected.return_value = True
    receive_started = asyncio.Event()
    never = asyncio.Event()

    async def receive() -> bytes:
        receive_started.set()
        await never.wait()
        return b""

    connection.receive = receive
    task = asyncio.create_task(bot._on_direct_connection(connection, "cancelled:1"))
    await receive_started.wait()
    assert connection in bot.generations[0].direct_connection_states

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection not in bot.generations[0].direct_connection_states
    connection.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_retiring_generation_reclaims_direct_connection_slots(
    bot: MakerBot, monkeypatch
) -> None:
    monkeypatch.setattr(direct_connection, "_MAX_DIRECT_CONNECTIONS", 1)
    old = bot.generations[0]
    stale = _connection()
    old.direct_connection_states[stale] = DirectConnectionState()
    replacement_generation = _generation(bot, 1)
    bot.generations[1] = replacement_generation

    await bot._close_generation(old)

    assert old.direct_connection_states == {}
    stale.close.assert_awaited_once_with()

    bot.running = True
    replacement = _connection()
    replacement.is_connected.return_value = True
    replacement.receive = AsyncMock(return_value=b"")
    await bot._on_direct_connection(replacement, "replacement:1", generation_id=1)

    replacement.receive.assert_awaited_once_with()
    replacement.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_idle_direct_connection_closes_after_timeout(bot: MakerBot, monkeypatch) -> None:
    monkeypatch.setattr(direct_connection, "_DIRECT_CONNECTION_IDLE_TIMEOUT_SEC", 0.01)
    bot.running = True
    connection = _connection()
    connection.is_connected.return_value = True
    never = asyncio.Event()

    async def receive() -> bytes:
        await never.wait()
        return b""

    connection.receive = receive
    await asyncio.wait_for(bot._on_direct_connection(connection, "idle:1"), timeout=1.0)

    assert connection not in bot.generations[0].direct_connection_states
    connection.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_handshakes_do_not_extend_unauthenticated_deadline(
    bot: MakerBot, monkeypatch
) -> None:
    clock = 0.0
    bot.running = True
    peer = NickIdentity(JM_VERSION)
    connection = _connection()
    connection.is_connected.return_value = True
    messages = [_handshake(peer.nick), _handshake(peer.nick), _handshake(peer.nick)]
    elapsed = [20.0, 20.0, 20.0]

    def monotonic() -> float:
        return clock

    async def receive() -> bytes:
        nonlocal clock
        clock += elapsed.pop(0)
        return messages.pop(0)

    connection.receive = receive
    connection.send = AsyncMock()
    monkeypatch.setattr(direct_connection.time, "monotonic", monotonic)

    await bot._on_direct_connection(connection, "unverified:1")

    assert messages == []
    assert connection.send.await_count == 2
    assert connection not in bot.generations[0].direct_connection_states
    connection.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_verified_private_messages_continue_after_unauthenticated_deadline(
    bot: MakerBot, monkeypatch
) -> None:
    clock = 0.0
    bot.running = True
    peer = NickIdentity(JM_VERSION)
    connection = _connection()
    connection.is_connected.side_effect = [True, True, True, False]
    messages = [
        _handshake(peer.nick),
        _signed_message(peer, bot.nick, "fill", "payload"),
        _signed_message(peer, bot.nick, "auth", "payload"),
    ]
    elapsed = [10.0, 20.0, 31.0]

    def monotonic() -> float:
        return clock

    async def receive() -> bytes:
        nonlocal clock
        clock += elapsed.pop(0)
        return messages.pop(0)

    connection.receive = receive
    connection.send = AsyncMock()
    bot._handle_fill = AsyncMock()
    bot._handle_auth = AsyncMock()
    monkeypatch.setattr(direct_connection.time, "monotonic", monotonic)

    await bot._on_direct_connection(connection, "verified:1")

    bot._handle_fill.assert_awaited_once_with(
        peer.nick, "fill payload", source="direct", generation_id=0
    )
    bot._handle_auth.assert_awaited_once_with(
        peer.nick, "auth payload", source="direct", generation_id=0
    )
    connection.close.assert_awaited_once_with()
