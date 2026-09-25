"""Focused lifecycle limits for maker direct peer sockets."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore import network as jmcore_network
from jmcore.crypto import NickIdentity
from jmcore.models import NetworkType
from jmcore.network import ONION_HOSTID, HiddenServiceListener, OnionPeer, TCPConnection
from jmcore.protocol import (
    FEATURE_DIRECT_PING_V1,
    JM_VERSION,
    MessageType,
    create_handshake_request,
)

import maker.direct_connection as direct_connection
from maker.bot import MakerBot
from maker.config import MakerConfig
from maker.direct_connection import DirectConnectionState
from maker.generation import MakerGeneration
from maker.rate_limiting import DirectConnectionRateLimiter


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


_VALID_NONCE = "0123456789abcdef" * 2


def _ping(nonce: object, **extra: object) -> bytes:
    message: dict[str, object] = {"type": MessageType.PING.value, "line": nonce}
    message.update(extra)
    return json.dumps(message).encode()


def _sent_messages(connection: MagicMock) -> list[dict]:
    """Decode everything the maker wrote to one socket, in order."""
    return [json.loads(call.args[0].decode()) for call in connection.send.await_args_list]


async def _verified_exchange(
    bot: MakerBot,
    connection: MagicMock,
    peer: NickIdentity,
    messages: list[bytes],
    peer_str: str = "ping:1",
) -> list[bytes]:
    """Handshake, verify the socket with a signed message, then deliver `messages`.

    Returns the messages the maker never read because it closed the socket.
    """
    pending = [_handshake(peer.nick), _signed_message(peer, bot.nick, "fill", "payload"), *messages]
    connection.is_connected.side_effect = [True] * len(pending) + [False]

    async def receive() -> bytes:
        return pending.pop(0)

    connection.receive = receive
    connection.send = AsyncMock()
    await asyncio.wait_for(bot._on_direct_connection(connection, peer_str), timeout=5.0)
    return pending


@pytest.mark.asyncio
async def test_verified_ping_is_echoed_with_identical_nonce(bot: MakerBot) -> None:
    """A verified sender's PING returns exactly one PONG carrying its own nonce."""
    bot.running = True
    peer = NickIdentity(JM_VERSION)
    connection = _connection()
    bot._handle_fill = AsyncMock()
    bot._handle_auth = AsyncMock()

    unread = await _verified_exchange(
        bot,
        connection,
        peer,
        [_ping(_VALID_NONCE), _signed_message(peer, bot.nick, "auth", "payload")],
    )

    assert unread == []
    assert _sent_messages(connection)[1:] == [
        {"type": MessageType.PONG.value, "line": _VALID_NONCE}
    ]
    # The socket survives the heartbeat and keeps serving protocol commands.
    bot._handle_auth.assert_awaited_once_with(
        peer.nick, "auth payload", source="direct", generation_id=0
    )
    assert connection not in bot.generations[0].direct_connection_states
    connection.close.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(_ping("abc"), id="too-short"),
        pytest.param(_ping(_VALID_NONCE[:-1]), id="one-char-short"),
        pytest.param(_ping(_VALID_NONCE + "0"), id="one-char-long"),
        pytest.param(_ping(_VALID_NONCE.upper()), id="uppercase-hex"),
        pytest.param(_ping("g" * 32), id="non-hex"),
        pytest.param(_ping(" " + _VALID_NONCE[1:]), id="whitespace"),
        pytest.param(_ping("a" * 4096), id="oversized"),
        pytest.param(_ping(None), id="null-nonce"),
        pytest.param(_ping(12345678901234567890123456789012), id="integer-nonce"),
        pytest.param(_ping([_VALID_NONCE]), id="list-nonce"),
        pytest.param(_ping({"line": _VALID_NONCE}), id="object-nonce"),
        pytest.param(_ping(_VALID_NONCE, extra="x"), id="extra-field"),
        pytest.param(json.dumps({"type": MessageType.PING.value}).encode(), id="missing-line"),
    ],
)
async def test_malformed_verified_ping_closes_without_amplification(
    bot: MakerBot, payload: bytes
) -> None:
    """Malformed heartbeats never produce a reply and end the socket immediately."""
    bot.running = True
    peer = NickIdentity(JM_VERSION)
    connection = _connection()
    bot._handle_fill = AsyncMock()
    bot._handle_auth = AsyncMock()

    unread = await _verified_exchange(
        bot,
        connection,
        peer,
        [payload, _signed_message(peer, bot.nick, "auth", "payload")],
    )

    assert len(unread) == 1
    assert _sent_messages(connection)[1:] == []
    bot._handle_auth.assert_not_awaited()
    assert connection not in bot.generations[0].direct_connection_states
    connection.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_unverified_pings_get_no_reply_and_cannot_extend_the_deadline(
    bot: MakerBot, monkeypatch
) -> None:
    """Heartbeat traffic is not authentication and cannot hold an unverified socket open."""
    clock = 0.0
    bot.running = True
    peer = NickIdentity(JM_VERSION)
    connection = _connection()
    connection.is_connected.return_value = True
    messages = [
        _handshake(peer.nick),
        _ping(_VALID_NONCE),
        _ping(_VALID_NONCE),
        _ping(_VALID_NONCE),
        _ping(_VALID_NONCE),
    ]
    elapsed = [10.0, 20.0, 20.0, 20.0, 20.0]

    def monotonic() -> float:
        return clock

    async def receive() -> bytes:
        nonlocal clock
        clock += elapsed.pop(0)
        return messages.pop(0)

    connection.receive = receive
    connection.send = AsyncMock()
    monkeypatch.setattr(direct_connection.time, "monotonic", monotonic)

    await bot._on_direct_connection(connection, "unverified-ping:1")

    # The absolute 60s deadline stopped the socket with a message still queued.
    assert len(messages) == 1
    assert connection.send.await_count == 1  # the handshake reply only, never a PONG
    assert connection not in bot.generations[0].direct_connection_states
    connection.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_rate_limited_ping_is_not_answered(bot: MakerBot) -> None:
    """The per-peer message limiter runs before the heartbeat reply, without closing."""
    bot.running = True
    bot._direct_connection_rate_limiter = DirectConnectionRateLimiter(
        message_rate_per_sec=0.0, message_burst=2
    )
    peer = NickIdentity(JM_VERSION)
    connection = _connection()
    bot._handle_fill = AsyncMock()

    unread = await _verified_exchange(bot, connection, peer, [_ping(_VALID_NONCE)])

    assert unread == []  # a limited heartbeat is dropped, not treated as malformed
    assert _sent_messages(connection)[1:] == []
    connection.close.assert_awaited_once_with()


@asynccontextmanager
async def _local_maker(
    bot: MakerBot,
) -> AsyncIterator[tuple[int, asyncio.Queue[dict], asyncio.Event]]:
    """Serve the maker's direct-connection handler on a local TCP port (no Tor).

    Yields the bound port, the PONGs the maker wrote, and an event set once the
    handler for the accepted socket returns.
    """
    bot.running = True
    pongs: asyncio.Queue[dict] = asyncio.Queue()
    handler_finished = asyncio.Event()

    async def on_connection(connection: TCPConnection, peer_str: str) -> None:
        original_send = connection.send

        async def recording_send(data: bytes) -> None:
            await original_send(data)
            message = json.loads(data.decode())
            if message.get("type") == MessageType.PONG.value:
                pongs.put_nowait(message)

        connection.send = recording_send  # type: ignore[method-assign]
        try:
            await bot._on_direct_connection(connection, peer_str)
        finally:
            handler_finished.set()

    listener = HiddenServiceListener(host="127.0.0.1", port=0, on_connection=on_connection)
    port = await listener.start()
    try:
        yield port, pongs, handler_finished
    finally:
        bot.running = False
        await listener.stop()


@pytest.mark.asyncio
async def test_local_tcp_peer_handshake_signed_message_then_ping_echo(bot: MakerBot) -> None:
    """A real OnionPeer over local TCP gets the feature, an echo, then a close."""
    peer_identity = NickIdentity(JM_VERSION)
    fill_seen = asyncio.Event()
    disconnected = asyncio.Event()

    async def handle_fill(*args: object, **kwargs: object) -> None:
        fill_seen.set()

    async def on_disconnect(nick: str) -> None:
        disconnected.set()

    bot._handle_fill = AsyncMock(side_effect=handle_fill)

    async with _local_maker(bot) as (port, pongs, handler_finished):
        peer = OnionPeer(
            nick=bot.nick,
            location=f"127.0.0.1:{port}",
            timeout=5.0,
            nick_identity=peer_identity,
            on_disconnect=on_disconnect,
        )
        try:
            assert await peer.connect(
                peer_identity.nick, "NOT-SERVING-ONION", NetworkType.REGTEST.value
            )
            assert peer.supports_feature(FEATURE_DIRECT_PING_V1) is True

            assert await peer.send_privmsg(peer_identity.nick, "fill", "0 100000 pubkey")
            await asyncio.wait_for(fill_seen.wait(), timeout=5.0)

            assert await peer.send(_ping(_VALID_NONCE))
            assert await asyncio.wait_for(pongs.get(), timeout=5.0) == {
                "type": MessageType.PONG.value,
                "line": _VALID_NONCE,
            }

            # A malformed heartbeat on the same verified socket ends it silently.
            assert await peer.send(_ping(_VALID_NONCE + "0"))
            await asyncio.wait_for(handler_finished.wait(), timeout=5.0)
            await asyncio.wait_for(disconnected.wait(), timeout=5.0)
            assert pongs.empty()
            assert bot.generations[0].direct_connection_states == {}
            assert bot.generations[0].direct_connections == {}
        finally:
            await peer.disconnect()

    assert peer._receive_task is None
    assert peer._connection is None


@pytest.mark.asyncio
async def test_local_tcp_peer_heartbeat_keeps_verified_socket_alive(
    bot: MakerBot, monkeypatch
) -> None:
    """The peer's own heartbeat survives against the maker, then releases its task."""
    monkeypatch.setattr(jmcore_network, "_DIRECT_PING_INTERVAL_SEC", 0.4)
    monkeypatch.setattr(jmcore_network, "_DIRECT_PING_TIMEOUT_SEC", 0.25)
    peer_identity = NickIdentity(JM_VERSION)
    fill_seen = asyncio.Event()

    async def handle_fill(*args: object, **kwargs: object) -> None:
        fill_seen.set()

    bot._handle_fill = AsyncMock(side_effect=handle_fill)

    async with _local_maker(bot) as (port, pongs, handler_finished):
        peer = OnionPeer(
            nick=bot.nick,
            location=f"127.0.0.1:{port}",
            timeout=5.0,
            nick_identity=peer_identity,
        )

        async def on_handshake_complete(nick: str) -> None:
            # Verify the socket before the first patched heartbeat interval elapses.
            await peer.send_privmsg(peer_identity.nick, "fill", "0 100000 pubkey")

        peer.on_handshake_complete = on_handshake_complete
        try:
            assert await peer.connect(
                peer_identity.nick, "NOT-SERVING-ONION", NetworkType.REGTEST.value
            )
            await asyncio.wait_for(fill_seen.wait(), timeout=5.0)

            first = await asyncio.wait_for(pongs.get(), timeout=3.0)
            second = await asyncio.wait_for(pongs.get(), timeout=3.0)
            for pong in (first, second):
                nonce = pong["line"]
                assert len(nonce) == 32
                assert set(nonce) <= set("0123456789abcdef")
            assert first["line"] != second["line"]

            # A missed echo would have closed the socket within the patched timeout.
            assert peer.is_connected()
            assert not handler_finished.is_set()
            states = bot.generations[0].direct_connection_states
            assert len(states) == 1
            assert next(iter(states.values())).verified is True
        finally:
            await peer.disconnect()

        await asyncio.wait_for(handler_finished.wait(), timeout=5.0)
        assert bot.generations[0].direct_connection_states == {}

    assert peer._receive_task is None
    assert peer._connection is None


@pytest.mark.asyncio
async def test_heartbeat_survives_a_command_handler_that_outlasts_the_idle_limit(
    bot: MakerBot, monkeypatch
) -> None:
    """A busy sequential handler blocks the reader; the retried nonce keeps the socket."""
    # The maker reads one message at a time, so a long command occupies the
    # reader. Probes must retry a single challenge instead of failing.
    monkeypatch.setattr(direct_connection, "_DIRECT_CONNECTION_IDLE_TIMEOUT_SEC", 0.5)
    monkeypatch.setattr(jmcore_network, "_DIRECT_PING_INTERVAL_SEC", 0.15)
    monkeypatch.setattr(jmcore_network, "_DIRECT_PING_TIMEOUT_SEC", 20.0)
    monkeypatch.setattr(jmcore_network, "_DIRECT_PING_WRITE_TIMEOUT_SEC", 5.0)
    peer_identity = NickIdentity(JM_VERSION)
    handler_entered = asyncio.Event()
    release_handler = asyncio.Event()

    async def handle_fill(*args: object, **kwargs: object) -> None:
        handler_entered.set()
        await release_handler.wait()

    bot._handle_fill = AsyncMock(side_effect=handle_fill)
    bot._handle_auth = AsyncMock()
    bot._handle_tx = AsyncMock()

    async with _local_maker(bot) as (port, pongs, handler_finished):
        peer = OnionPeer(
            nick=bot.nick,
            location=f"127.0.0.1:{port}",
            timeout=5.0,
            nick_identity=peer_identity,
        )

        async def on_handshake_complete(nick: str) -> None:
            # Verify the socket before the first patched heartbeat interval elapses.
            await peer.send_privmsg(peer_identity.nick, "fill", "0 100000 pubkey")

        peer.on_handshake_complete = on_handshake_complete
        try:
            assert await peer.connect(
                peer_identity.nick, "NOT-SERVING-ONION", NetworkType.REGTEST.value
            )
            await asyncio.wait_for(handler_entered.wait(), timeout=5.0)

            # Stay busy well past the maker's patched idle limit while probes retry.
            await asyncio.sleep(0.8)
            assert pongs.empty()  # a blocked handler cannot answer a heartbeat
            assert bot._handle_fill.await_count == 1  # no concurrent or PING-driven command
            assert peer.is_connected()
            assert not handler_finished.is_set()

            release_handler.set()
            replies = [await asyncio.wait_for(pongs.get(), timeout=5.0)]
            while True:  # drain the probes that queued up behind the busy handler
                try:
                    replies.append(await asyncio.wait_for(pongs.get(), timeout=0.08))
                except TimeoutError:
                    break

            # Retries carry one challenge, so every queued reply echoes its nonce.
            assert len(replies) >= 3
            assert all(reply == replies[0] for reply in replies)
            assert len(replies[0]["line"]) == 32
            assert set(replies[0]["line"]) <= set("0123456789abcdef")

            assert peer.is_connected()
            assert bot._handle_fill.await_count == 1
            bot._handle_auth.assert_not_awaited()
            bot._handle_tx.assert_not_awaited()
            states = bot.generations[0].direct_connection_states
            assert len(states) == 1
            assert next(iter(states.values())).verified is True
        finally:
            release_handler.set()
            await peer.disconnect()

        await asyncio.wait_for(handler_finished.wait(), timeout=5.0)
        assert bot.generations[0].direct_connection_states == {}
        assert bot.generations[0].direct_connections == {}

    assert peer._receive_task is None
    assert peer._connection is None
