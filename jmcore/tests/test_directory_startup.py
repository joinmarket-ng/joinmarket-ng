"""Startup liveness and cleanup tests for ``DirectoryClientPool``."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmcore.crypto import NickIdentity
from jmcore.directory_pool import DirectoryClientPool


def _identity() -> NickIdentity:
    return NickIdentity(private_key_bytes=b"\x01" * 32)


def _make_pool(servers: list[str]) -> DirectoryClientPool:
    return DirectoryClientPool(
        directory_servers=servers,
        network="mainnet",
        nick_identity=_identity(),
    )


async def _wait_for(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1.0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "servers",
    (
        ["hanging.onion:5222", "fast.onion:5222"],
        ["fast.onion:5222", "hanging.onion:5222"],
    ),
    ids=("hanging-first", "fast-first"),
)
async def test_connect_all_with_retry_does_not_wait_for_hanging_directory(
    servers: list[str],
) -> None:
    pool = _make_pool(servers)
    hanging_started = asyncio.Event()
    never_complete = asyncio.Event()

    hanging_client = MagicMock()

    async def hang() -> None:
        hanging_started.set()
        await never_complete.wait()

    hanging_client.connect = AsyncMock(side_effect=hang)
    hanging_client.close = AsyncMock()

    fast_client = MagicMock()

    async def connect_fast() -> None:
        await hanging_started.wait()

    fast_client.connect = AsyncMock(side_effect=connect_fast)
    fast_client.close = AsyncMock()

    clients = {"hanging.onion": hanging_client, "fast.onion": fast_client}
    with patch(
        "jmcore.directory_pool.DirectoryClient",
        side_effect=lambda **kwargs: clients[kwargs["host"]],
    ):
        connected = await asyncio.wait_for(
            pool.connect_all_with_retry(timeout=0.1, initial_delay=0.01),
            timeout=1.0,
        )

    assert connected == 1
    assert pool.clients == {"fast.onion:5222": fast_client}
    hanging_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_all_with_retry_applies_deadline_to_all_hanging_connects() -> None:
    pool = _make_pool(["one.onion:5222", "two.onion:5222"])
    all_started = asyncio.Event()
    never_complete = asyncio.Event()
    started: set[str] = set()
    clients: dict[str, MagicMock] = {}

    for host in ("one.onion", "two.onion"):
        client = MagicMock()

        async def hang(host: str = host) -> None:
            started.add(host)
            if len(started) == 2:
                all_started.set()
            await never_complete.wait()

        client.connect = AsyncMock(side_effect=hang)
        client.close = AsyncMock()
        clients[host] = client

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    with patch(
        "jmcore.directory_pool.DirectoryClient",
        side_effect=lambda **kwargs: clients[kwargs["host"]],
    ):
        startup = asyncio.create_task(pool.connect_all_with_retry(timeout=0.05, initial_delay=0.01))
        await _wait_for(all_started)
        connected = await asyncio.wait_for(startup, timeout=1.0)

    assert connected == 0
    assert pool.clients == {}
    assert loop.time() - started_at < 0.5
    for client in clients.values():
        client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_connect_to_directory_cancellation_closes_unregistered_client() -> None:
    pool = _make_pool(["blocked.onion:5222"])
    connect_started = asyncio.Event()
    never_complete = asyncio.Event()
    client = MagicMock()

    async def hang() -> None:
        connect_started.set()
        await never_complete.wait()

    client.connect = AsyncMock(side_effect=hang)
    client.close = AsyncMock()

    with patch("jmcore.directory_pool.DirectoryClient", return_value=client):
        connection = asyncio.create_task(pool.connect_to_directory("blocked.onion:5222"))
        await _wait_for(connect_started)
        connection.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connection

    client.close.assert_awaited_once()
    assert pool.clients == {}


@pytest.mark.asyncio
async def test_connect_all_with_retry_retains_all_immediate_successes() -> None:
    pool = _make_pool(["one.onion:5222", "two.onion:5222"])
    one_client = MagicMock(connect=AsyncMock())
    two_client = MagicMock(connect=AsyncMock())
    one_client.close = AsyncMock()
    two_client.close = AsyncMock()
    clients = {"one.onion": one_client, "two.onion": two_client}

    with patch(
        "jmcore.directory_pool.DirectoryClient",
        side_effect=lambda **kwargs: clients[kwargs["host"]],
    ):
        connected = await asyncio.wait_for(
            pool.connect_all_with_retry(timeout=0.1, initial_delay=0.01),
            timeout=1.0,
        )

    assert connected == 2
    assert pool.clients == {
        "one.onion:5222": one_client,
        "two.onion:5222": two_client,
    }


@pytest.mark.asyncio
async def test_connect_all_with_retry_deduplicates_canonical_endpoint_aliases() -> None:
    pool = _make_pool(["same.onion", "same.onion:5222"])
    client = MagicMock(connect=AsyncMock(), close=AsyncMock())

    with patch("jmcore.directory_pool.DirectoryClient", return_value=client) as constructor:
        connected = await asyncio.wait_for(
            pool.connect_all_with_retry(timeout=0.1, initial_delay=0.01),
            timeout=1.0,
        )

    assert connected == 1
    assert pool.clients == {"same.onion:5222": client}
    constructor.assert_called_once()
    client.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_all_with_retry_cancellation_during_hook_closes_client() -> None:
    pool = _make_pool(["fast.onion:5222"])
    client = MagicMock(connect=AsyncMock(), close=AsyncMock())
    hook_started = asyncio.Event()
    never_complete = asyncio.Event()

    async def block_hook(_node_id: str, _client: Any) -> None:
        hook_started.set()
        await never_complete.wait()

    pool._on_directory_connected = block_hook  # type: ignore[method-assign]

    with patch("jmcore.directory_pool.DirectoryClient", return_value=client):
        startup = asyncio.create_task(pool.connect_all_with_retry(timeout=0.1, initial_delay=0.01))
        await _wait_for(hook_started)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup

    client.close.assert_awaited_once()
    assert pool.clients == {}


@pytest.mark.asyncio
async def test_cancellation_closes_success_that_arrives_while_another_hook_yields() -> None:
    pool = _make_pool(["first.onion:5222", "second.onion:5222"])
    hook_started = asyncio.Event()
    second_connected = asyncio.Event()
    hook_blocked = asyncio.Event()
    never_complete = asyncio.Event()
    first = MagicMock(connect=AsyncMock(), close=AsyncMock())

    async def connect_second() -> None:
        await hook_started.wait()
        second_connected.set()

    second = MagicMock(connect=AsyncMock(side_effect=connect_second), close=AsyncMock())
    clients = {"first.onion": first, "second.onion": second}

    async def block_hook(_node_id: str, _client: Any) -> None:
        hook_started.set()
        await second_connected.wait()
        hook_blocked.set()
        await never_complete.wait()

    pool._on_directory_connected = block_hook  # type: ignore[method-assign]
    with patch(
        "jmcore.directory_pool.DirectoryClient",
        side_effect=lambda **kwargs: clients[kwargs["host"]],
    ):
        startup = asyncio.create_task(pool.connect_all_with_retry(timeout=5.0))
        await _wait_for(hook_blocked)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await startup

    first.close.assert_awaited_once()
    second.close.assert_awaited_once()
    assert pool.clients == {}
