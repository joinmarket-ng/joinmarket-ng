"""Real-TCP regression coverage for maker directory startup liveness."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from directory_server.server import DirectoryServer
from jmcore.config import TorControlConfig
from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.directory_client import DirectoryClient
from jmcore.models import NetworkType, Offer, OfferType
from jmcore.network import ONION_HOSTID, TCPConnection
from jmcore.nick_auth import NickAuthMode
from jmcore.protocol import COMMAND_PREFIX, MessageType
from jmcore.settings import DirectoryServerSettings

from maker.bot import MakerBot
from maker.config import MakerConfig

_HOST = "127.0.0.1"
_WATCHDOG_TIMEOUT = 5.0
_CONNECTION_TIMEOUT = 30.0


async def _wait_until(predicate: Callable[[], bool], description: str) -> None:
    """Wait for a transport state transition without a timing-based delay."""
    deadline = asyncio.get_running_loop().time() + _WATCHDOG_TIMEOUT
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError(f"Timed out waiting for {description}")
        await asyncio.sleep(0)


async def _wait_for_server_start(server: DirectoryServer, server_task: asyncio.Task[None]) -> int:
    """Return a dynamically bound directory port once its serving task is ready."""

    def server_is_ready() -> bool:
        if server_task.done():
            server_task.result()
        return server.server is not None and bool(server.server.sockets)

    await _wait_until(server_is_ready, "directory server startup")
    assert server.server is not None
    assert server.server.sockets is not None
    return int(server.server.sockets[0].getsockname()[1])


def _unexpected_failures(results: list[object]) -> list[BaseException]:
    """Retain teardown errors while allowing intentionally canceled server tasks."""
    return [
        result
        for result in results
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
    ]


async def _receive_signed_offer(requester: DirectoryClient, maker_nick: str) -> tuple[str, str]:
    """Return the authenticated private offer sent to the requester."""
    assert requester.connection is not None
    deadline = asyncio.get_running_loop().time() + _WATCHDOG_TIMEOUT
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for the maker's private offer")
        response = await asyncio.wait_for(requester.connection.receive(), timeout=remaining)
        message = json.loads(response.decode("utf-8"))
        line = message.get("line", "")
        route = line.split(COMMAND_PREFIX, 2)
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


@pytest.mark.parametrize("stalled_first", [True, False], ids=("stalled-first", "healthy-first"))
async def test_maker_serves_orderbook_when_parallel_startup_cancels_stalled_handshake(
    stalled_first: bool, tmp_path: Path
) -> None:
    """One healthy directory must make maker startup live without waiting for a stalled peer."""
    stalled_handshake_received = asyncio.Event()
    stalled_peer_eof = asyncio.Event()
    raw_handler_tasks: list[asyncio.Task[Any]] = []
    raw_server: asyncio.AbstractServer | None = None
    directory: DirectoryServer | None = None
    directory_task: asyncio.Task[None] | None = None
    requester: DirectoryClient | None = None
    bot: MakerBot | None = None
    failures: list[BaseException] = []

    async def await_cleanup(awaitable: Awaitable[object], operation: str) -> object | None:
        """Bound one teardown operation while allowing later cleanup to continue."""
        try:
            return await asyncio.wait_for(awaitable, timeout=_WATCHDOG_TIMEOUT)
        except Exception as error:
            failures.append(RuntimeError(f"{operation} failed: {error}"))
            return None

    async def stalled_handshake_server(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        raw_handler_tasks.append(task)
        try:
            await asyncio.wait_for(reader.readuntil(b"\n"), timeout=_WATCHDOG_TIMEOUT)
            stalled_handshake_received.set()
            assert await asyncio.wait_for(reader.read(), timeout=_WATCHDOG_TIMEOUT) == b""
            stalled_peer_eof.set()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    try:
        raw_server = await asyncio.start_server(stalled_handshake_server, _HOST, 0)
        assert raw_server.sockets is not None
        stalled_port = int(raw_server.sockets[0].getsockname()[1])

        directory = DirectoryServer(
            DirectoryServerSettings(
                host=_HOST,
                port=0,
                max_peers=4,
                health_check_port=0,
                nick_auth_mode=NickAuthMode.DISABLED,
            ),
            NetworkType.REGTEST,
            "J5StartupTransportDirectoryOO",
        )
        original_perform_handshake = directory._perform_handshake

        async def wait_for_stalled_handshake(
            connection: TCPConnection, connection_id: str
        ) -> str | None:
            await asyncio.wait_for(stalled_handshake_received.wait(), timeout=_WATCHDOG_TIMEOUT)
            return await original_perform_handshake(connection, connection_id)

        directory._perform_handshake = wait_for_stalled_handshake  # type: ignore[method-assign]
        directory_task = asyncio.create_task(directory.start())
        healthy_port = await _wait_for_server_start(directory, directory_task)
        healthy_endpoint = f"{_HOST}:{healthy_port}"
        stalled_endpoint = f"{_HOST}:{stalled_port}"
        endpoints = (
            [stalled_endpoint, healthy_endpoint]
            if stalled_first
            else [healthy_endpoint, stalled_endpoint]
        )

        wallet = MagicMock(mixdepth_count=5, utxo_cache={})
        backend = MagicMock()
        backend.can_provide_neutrino_metadata.return_value = False
        config = MakerConfig(
            mnemonic="test " * 12,
            data_dir=tmp_path,
            directory_servers=endpoints,
            network=NetworkType.REGTEST,
            nick_auth_mode=NickAuthMode.DISABLED,
            tor_control=TorControlConfig(enabled=False),
            connection_timeout=_CONNECTION_TIMEOUT,
            directory_startup_timeout=10,
        )
        bot = MakerBot(wallet=wallet, backend=backend, config=config)
        bot.current_offers = [
            Offer(
                counterparty=bot.nick,
                oid=0,
                ordertype=OfferType.SW0_RELATIVE,
                minsize=10_000,
                maxsize=100_000,
                txfee=0,
                cjfee="0.0001",
            )
        ]

        await asyncio.wait_for(bot._connect_to_directories_with_retry(), timeout=_WATCHDOG_TIMEOUT)
        await asyncio.wait_for(stalled_peer_eof.wait(), timeout=_WATCHDOG_TIMEOUT)
        assert set(bot.directory_clients) == {healthy_endpoint}
        await _wait_until(lambda: directory.peer_registry.count() == 1, "maker handshake")

        bot.running = True
        bot._start_generation_listeners(bot.generations[bot.current_generation_id])
        healthy_client = bot.directory_clients[healthy_endpoint]
        await _wait_until(
            lambda: (
                healthy_client.connection is not None
                and healthy_client.connection._receive_lock.locked()
            ),
            "maker directory listener",
        )

        requester = DirectoryClient(
            host=_HOST,
            port=healthy_port,
            network=NetworkType.REGTEST.value,
            nick_identity=NickIdentity(private_key_bytes=b"\x02" * 32),
            nick_auth_mode=NickAuthMode.DISABLED,
            timeout=_WATCHDOG_TIMEOUT,
        )
        await requester.connect()
        await _wait_until(lambda: directory.peer_registry.count() == 2, "requester handshake")
        await requester.send_public_message("orderbook")
        command, data = await _receive_signed_offer(requester, bot.nick)

        assert command == OfferType.SW0_RELATIVE.value
        assert data == "0 10000 100000 0 0.0001"
        assert bot._orderbook_response_counts == {
            "directory_admitted": 1,
            "directory_suppressed": 0,
            "direct_admitted": 0,
            "direct_suppressed": 0,
        }
    finally:
        if requester is not None:
            await await_cleanup(requester.close(), "requester close")

        if bot is not None:
            bot.running = False
            await await_cleanup(bot._directory_pool.close_all(), "maker directory close")
            for task in bot.listen_tasks:
                if not task.done():
                    task.cancel()
            listener_results = await await_cleanup(
                asyncio.gather(*bot.listen_tasks, return_exceptions=True),
                "maker listener join",
            )
            if isinstance(listener_results, list):
                failures.extend(_unexpected_failures(listener_results))

        if directory is not None:
            await await_cleanup(
                _wait_until(
                    lambda: (
                        directory.peer_registry.count() == 0
                        and len(directory.connections) == 0
                        and not directory._client_tasks
                    ),
                    "directory client cleanup",
                ),
                "directory client cleanup",
            )

        if raw_server is not None:
            raw_server.close()
            await await_cleanup(raw_server.wait_closed(), "stalled server close")
        raw_handler_results = await await_cleanup(
            asyncio.gather(*raw_handler_tasks, return_exceptions=True),
            "stalled server handler join",
        )
        if isinstance(raw_handler_results, list):
            failures.extend(_unexpected_failures(raw_handler_results))

        if directory is not None:
            await await_cleanup(directory.stop(), "directory stop")
        if directory_task is not None:
            if not directory_task.done():
                directory_task.cancel()
            directory_results = await await_cleanup(
                asyncio.gather(directory_task, return_exceptions=True),
                "directory serving task join",
            )
            if isinstance(directory_results, list):
                failures.extend(_unexpected_failures(directory_results))
        if directory is not None and directory.health_server.thread is not None:
            failures.append(
                RuntimeError("directory health-check thread remained active after teardown")
            )
        if failures:
            raise RuntimeError("directory startup transport teardown failed") from failures[0]
