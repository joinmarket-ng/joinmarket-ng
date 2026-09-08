"""Liveness regressions for bounded maker directory orderbook responses."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from directory_server.server import DirectoryServer
from jmcore.config import TorControlConfig
from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.directory_client import DirectoryClient, DirectoryClientError
from jmcore.models import NetworkType, Offer, OfferType, PeerStatus
from jmcore.network import ONION_HOSTID
from jmcore.nick_auth import NickAuthMode
from jmcore.protocol import COMMAND_PREFIX, MessageType
from jmcore.settings import DirectoryServerSettings

from maker.bot import MakerBot
from maker.config import MakerConfig
from maker.directory_pool import MakerDirectoryPool
from maker.generation import MakerGeneration
from maker.offers import OfferManager
from maker.rate_limiting import ProcessWideTokenBucket

_HOST = "127.0.0.1"
_WATCHDOG_TIMEOUT = 5.0
_SEND_TIMEOUT = 0.1


def _make_bot(tmp_path: Path, directory_servers: list[str]) -> MakerBot:
    wallet = MagicMock(mixdepth_count=5, utxo_cache={})
    backend = MagicMock()
    backend.can_provide_neutrino_metadata.return_value = False
    config = MakerConfig(
        mnemonic="test " * 12,
        data_dir=tmp_path,
        directory_servers=directory_servers,
        network=NetworkType.REGTEST,
        nick_auth_mode=NickAuthMode.DISABLED,
        tor_control=TorControlConfig(enabled=False),
    )
    return MakerBot(wallet=wallet, backend=backend, config=config)


def _offers(bot: MakerBot, count: int) -> list[Offer]:
    return [
        Offer(
            counterparty=bot.nick,
            oid=index,
            ordertype=OfferType.SW0_RELATIVE,
            minsize=10_000 + index,
            maxsize=100_000 + index,
            txfee=index,
            cjfee=f"0.000{index + 1}",
        )
        for index in range(count)
    ]


async def _wait_until(predicate: Callable[[], bool], description: str) -> None:
    """Wait for an event-loop state transition without a fixed delay."""
    deadline = asyncio.get_running_loop().time() + _WATCHDOG_TIMEOUT
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Timed out waiting for {description}")
        await asyncio.sleep(0)


async def _wait_for_directory_start(
    server: DirectoryServer, server_task: asyncio.Task[None]
) -> int:
    """Return the dynamically assigned directory port after startup."""

    def server_is_ready() -> bool:
        if server_task.done():
            server_task.result()
        return server.server is not None and bool(server.server.sockets)

    await _wait_until(server_is_ready, "directory server startup")
    assert server.server is not None
    assert server.server.sockets is not None
    return int(server.server.sockets[0].getsockname()[1])


async def _receive_signed_offer(requester: DirectoryClient, maker_nick: str) -> tuple[str, str]:
    """Return the next authenticated private offer delivered to ``requester``."""
    assert requester.connection is not None
    deadline = asyncio.get_running_loop().time() + _WATCHDOG_TIMEOUT
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for the maker's private offer")
        response = await asyncio.wait_for(requester.connection.receive(), timeout=remaining)
        message = json.loads(response.decode("utf-8"))
        route = message.get("line", "").split(COMMAND_PREFIX, 2)
        if (
            message.get("type") != MessageType.PRIVMSG.value
            or len(route) != 3
            or route[0] != maker_nick
            or route[1] != requester.nick
        ):
            continue
        authenticated, command, data = verify_signed_privmsg(maker_nick, route[2], ONION_HOSTID)
        assert authenticated
        return command, data


async def _close_real_tcp_resources(
    requester: DirectoryClient | None,
    bot: MakerBot | None,
    listeners: Sequence[asyncio.Task[None]],
    servers: Sequence[DirectoryServer],
    server_tasks: Sequence[asyncio.Task[None]],
) -> None:
    """Close real sockets and serving tasks without leaving transport work behind."""
    if requester is not None:
        await asyncio.wait_for(requester.close(), timeout=_WATCHDOG_TIMEOUT)

    if bot is not None:
        bot.running = False
    for listener in listeners:
        if not listener.done():
            listener.cancel()
    if listeners:
        await asyncio.wait_for(
            asyncio.gather(*listeners, return_exceptions=True), timeout=_WATCHDOG_TIMEOUT
        )
    if bot is not None:
        await asyncio.wait_for(bot._directory_pool.close_all(), timeout=_WATCHDOG_TIMEOUT)

    for server in servers:
        await asyncio.wait_for(server.stop(), timeout=_WATCHDOG_TIMEOUT)
    for server_task in server_tasks:
        if not server_task.done():
            server_task.cancel()
    if server_tasks:
        results = await asyncio.wait_for(
            asyncio.gather(*server_tasks, return_exceptions=True), timeout=_WATCHDOG_TIMEOUT
        )
        failures = [
            result
            for result in results
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
        ]
        if failures:
            raise RuntimeError("directory server task failed during teardown") from failures[0]
    assert all(server.health_server.thread is None for server in servers)


@pytest.fixture
def bot(tmp_path: Path) -> MakerBot:
    return _make_bot(tmp_path, ["healthy:5222", "stalled:5222"])


async def test_stalled_directory_times_out_once_after_healthy_multi_offer_delivery(
    bot: MakerBot,
) -> None:
    """A blocked directory cannot delay another directory's complete ordered response."""
    bot.current_offers = _offers(bot, 3)
    healthy_complete = asyncio.Event()
    stalled_waiting = asyncio.Event()
    healthy_calls: list[tuple[str, str, str]] = []

    async def send_healthy(recipient: str, command: str, data: str) -> None:
        healthy_calls.append((recipient, command, data))
        if len(healthy_calls) == len(bot.current_offers):
            healthy_complete.set()

    async def stall_send(*_args: object) -> None:
        stalled_waiting.set()
        await asyncio.Event().wait()

    healthy = MagicMock()
    healthy.abort = MagicMock()
    healthy.send_private_message = AsyncMock(side_effect=send_healthy)
    stalled = MagicMock()
    stalled.abort = MagicMock()
    stalled.send_private_message = AsyncMock(side_effect=stall_send)
    bot.directory_clients = {"healthy": healthy, "stalled": stalled}

    with patch("maker.protocol_handlers._ORDERBOOK_SEND_TIMEOUT_SEC", _SEND_TIMEOUT):
        response_task = asyncio.create_task(bot._send_offers_to_taker("J5Taker"))
        await asyncio.wait_for(
            asyncio.gather(healthy_complete.wait(), stalled_waiting.wait()),
            timeout=_WATCHDOG_TIMEOUT,
        )
        assert not response_task.done()
        await asyncio.wait_for(response_task, timeout=_WATCHDOG_TIMEOUT)

    assert healthy_calls == [
        (
            "J5Taker",
            offer.ordertype.value,
            f"{offer.oid} {offer.minsize} {offer.maxsize} {offer.txfee} {offer.cjfee}",
        )
        for offer in bot.current_offers
    ]
    assert stalled.send_private_message.await_count == 1
    stalled.abort.assert_called_once_with()
    healthy.abort.assert_not_called()


async def test_real_tcp_abort_wakes_listener_and_makes_stalled_directory_reconnectable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocked real writer is aborted, removed by its listener, and eligible to reconnect."""
    servers: list[DirectoryServer] = []
    server_tasks: list[asyncio.Task[None]] = []
    listeners: list[asyncio.Task[None]] = []
    requester: DirectoryClient | None = None
    bot: MakerBot | None = None

    try:
        servers = [
            DirectoryServer(
                DirectoryServerSettings(
                    host=_HOST,
                    port=0,
                    max_peers=3,
                    health_check_port=0,
                    nick_auth_mode=NickAuthMode.DISABLED,
                ),
                NetworkType.REGTEST,
                f"J5SendLivenessDirectory{index}OOOO",
            )
            for index in range(2)
        ]
        server_tasks = [asyncio.create_task(server.start()) for server in servers]
        ports = await asyncio.gather(
            *(
                _wait_for_directory_start(server, task)
                for server, task in zip(servers, server_tasks, strict=True)
            )
        )
        healthy_endpoint = f"{_HOST}:{ports[0]}"
        stalled_endpoint = f"{_HOST}:{ports[1]}"

        bot = _make_bot(tmp_path, [healthy_endpoint, stalled_endpoint])
        bot.current_offers = _offers(bot, 1)
        assert await bot._directory_pool.connect_all_parallel() == 2
        bot.running = True
        listeners = [
            asyncio.create_task(
                bot._listen_client(node_id, client), name=f"maker-directory-listener-{node_id}"
            )
            for node_id, client in bot.directory_clients.items()
        ]
        await _wait_until(
            lambda: all(
                client.connection is not None and client.connection._receive_lock.locked()
                for client in bot.directory_clients.values()
            ),
            "maker directory listeners",
        )

        requester = DirectoryClient(
            host=_HOST,
            port=ports[0],
            network=NetworkType.REGTEST.value,
            nick_identity=NickIdentity(private_key_bytes=b"\x03" * 32),
            nick_auth_mode=NickAuthMode.DISABLED,
            timeout=_WATCHDOG_TIMEOUT,
        )
        await requester.connect()
        await _wait_until(lambda: servers[0].peer_registry.count() == 2, "requester handshake")

        def requester_is_routable() -> bool:
            peer = servers[0].peer_registry.get_by_nick(requester.nick)
            return peer is not None and peer.status is PeerStatus.HANDSHAKED

        await _wait_until(requester_is_routable, "requester routing readiness")

        stalled_client = bot.directory_clients[stalled_endpoint]
        assert stalled_client.connection is not None
        drain_started = asyncio.Event()
        never_release_drain = asyncio.Event()

        async def stall_drain() -> None:
            drain_started.set()
            await never_release_drain.wait()

        monkeypatch.setattr(stalled_client.connection.writer, "drain", stall_drain)

        with patch("maker.protocol_handlers._ORDERBOOK_SEND_TIMEOUT_SEC", _SEND_TIMEOUT):
            await requester.send_public_message("orderbook")
            await asyncio.wait_for(drain_started.wait(), timeout=_WATCHDOG_TIMEOUT)
            command, data = await _receive_signed_offer(requester, bot.nick)
            await _wait_until(lambda: stalled_client.connection is None, "stalled response timeout")

        stalled_listener = next(
            listener
            for listener in listeners
            if listener.get_name() == f"maker-directory-listener-{stalled_endpoint}"
        )
        await _wait_until(
            lambda: stalled_endpoint not in bot.directory_clients,
            "stalled client removal after transport abort",
        )
        assert stalled_client.connection is None
        assert (stalled_endpoint, stalled_endpoint) in bot._directory_pool.list_disconnected()
        assert healthy_endpoint in bot.directory_clients
        assert command == OfferType.SW0_RELATIVE.value
        assert data == "0 10000 100000 0 0.0001"
        assert bot._orderbook_response_counts["directory_admitted"] == 1
        assert bot._orderbook_response_counts["directory_suppressed"] == 0
        await asyncio.wait_for(stalled_listener, timeout=_WATCHDOG_TIMEOUT)
    finally:
        await _close_real_tcp_resources(requester, bot, listeners, servers, server_tasks)


async def test_external_cancellation_joins_all_send_tasks_without_aborting_clients(
    bot: MakerBot,
) -> None:
    """Caller cancellation propagates through the TaskGroup without treating peers as stalled."""
    bot.current_offers = _offers(bot, 1)
    all_started = asyncio.Event()
    cancellations: set[str] = set()
    started: set[str] = set()

    async def block_send(name: str) -> None:
        started.add(name)
        if len(started) == 2:
            all_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations.add(name)
            raise

    async def send_first(*_args: object) -> None:
        await block_send("first")

    async def send_second(*_args: object) -> None:
        await block_send("second")

    first = MagicMock()
    first.abort = MagicMock()
    first.send_private_message = AsyncMock(side_effect=send_first)
    second = MagicMock()
    second.abort = MagicMock()
    second.send_private_message = AsyncMock(side_effect=send_second)
    bot.directory_clients = {"first": first, "second": second}

    response_task = asyncio.create_task(bot._send_offers_to_taker("J5Cancelled"))
    await asyncio.wait_for(all_started.wait(), timeout=_WATCHDOG_TIMEOUT)
    response_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(response_task, timeout=_WATCHDOG_TIMEOUT)

    assert cancellations == {"first", "second"}
    first.abort.assert_not_called()
    second.abort.assert_not_called()


async def test_concurrent_healthy_responses_preserve_budget_and_bond_proofs(bot: MakerBot) -> None:
    """Concurrent normal responses spend one budget token and proof per requester, not per peer."""
    bot.current_offers = _offers(bot, 1)
    bot.fidelity_bond = MagicMock()
    bot._orderbook_proof_work_limiter = ProcessWideTokenBucket(2, 0.0)
    first = MagicMock(send_private_message=AsyncMock(), abort=MagicMock())
    second = MagicMock(send_private_message=AsyncMock(), abort=MagicMock())
    bot.directory_clients = {"first": first, "second": second}

    with patch("maker.protocol_handlers.create_fidelity_bond_proof", return_value="proof") as proof:
        await asyncio.wait_for(
            asyncio.gather(
                bot._send_offers_to_taker("J5First"),
                bot._send_offers_to_taker("J5Second"),
            ),
            timeout=_WATCHDOG_TIMEOUT,
        )

    expected = {
        ("J5First", OfferType.SW0_RELATIVE.value, "0 10000 100000 0 0.0001!tbond proof"),
        ("J5Second", OfferType.SW0_RELATIVE.value, "0 10000 100000 0 0.0001!tbond proof"),
    }
    assert first.send_private_message.await_count == 2
    assert second.send_private_message.await_count == 2
    assert {tuple(sent.args) for sent in first.send_private_message.await_args_list} == expected
    assert {tuple(sent.args) for sent in second.send_private_message.await_args_list} == expected
    assert proof.call_count == 2
    assert bot._orderbook_proof_work_limiter.try_consume() is False
    assert bot._orderbook_response_counts == {
        "directory_admitted": 2,
        "directory_suppressed": 0,
        "direct_admitted": 0,
        "direct_suppressed": 0,
    }
    first.abort.assert_not_called()
    second.abort.assert_not_called()


async def test_old_listener_timeout_cannot_remove_replaced_same_node_client(bot: MakerBot) -> None:
    """An old generation's timed-out client cannot remove a current replacement at its node id."""
    node_id = "directory.onion:5222"
    bot.current_offers = _offers(bot, 1)
    old = bot.generations[0]
    old_send_started = asyncio.Event()
    old_listener_started = asyncio.Event()
    abort_old_listener = asyncio.Event()

    async def block_old_send(*_args: object) -> None:
        old_send_started.set()
        await asyncio.Event().wait()

    async def listen_until_abort(duration: float) -> list[dict[str, Any]]:
        assert duration == 1.0
        old_listener_started.set()
        await abort_old_listener.wait()
        raise DirectoryClientError("aborted")

    old_client = MagicMock()
    old_client.send_private_message = AsyncMock(side_effect=block_old_send)
    old_client.listen_for_messages = AsyncMock(side_effect=listen_until_abort)
    old_client.close = AsyncMock()
    old_client.abort = MagicMock(side_effect=abort_old_listener.set)
    bot.directory_clients = {node_id: old_client}
    old.directory_clients = bot.directory_clients

    replacement_identity = NickIdentity()
    replacement_pool = MakerDirectoryPool(
        config=bot.config,
        nick_identity=replacement_identity,
        neutrino_compat=False,
    )
    replacement = MakerGeneration(
        generation_id=1,
        nick_identity=replacement_identity,
        offer_manager=OfferManager(bot.wallet, bot.config, replacement_identity.nick),
        directory_pool=replacement_pool,
    )
    replacement_pool.clients = replacement.directory_clients
    replacement_client = MagicMock(abort=MagicMock())
    replacement.directory_clients[node_id] = replacement_client
    bot.generations[1] = replacement
    bot.running = True
    listener_task = asyncio.create_task(bot._listen_client(node_id, old_client, generation_id=0))

    try:
        await asyncio.wait_for(old_listener_started.wait(), timeout=_WATCHDOG_TIMEOUT)
        with patch("maker.protocol_handlers._ORDERBOOK_SEND_TIMEOUT_SEC", _SEND_TIMEOUT):
            response_task = asyncio.create_task(
                bot._send_offers_to_taker("J5Taker", generation_id=0)
            )
            await asyncio.wait_for(old_send_started.wait(), timeout=_WATCHDOG_TIMEOUT)
            bot._activate_generation(replacement)
            await asyncio.wait_for(response_task, timeout=_WATCHDOG_TIMEOUT)
        await asyncio.wait_for(listener_task, timeout=_WATCHDOG_TIMEOUT)

        assert bot.directory_clients[node_id] is replacement_client
        old_client.abort.assert_called_once_with()
        old_client.close.assert_awaited_once_with()
        replacement_client.abort.assert_not_called()
    finally:
        bot.running = False
        if not listener_task.done():
            listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await listener_task
