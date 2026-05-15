"""Tests for jmcore.directory_pool.DirectoryClientPool."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmcore.crypto import NickIdentity
from jmcore.directory_pool import DirectoryClientPool


def _identity() -> NickIdentity:
    # Deterministic seed so tests don't rely on randomness.
    return NickIdentity(private_key_bytes=b"\x01" * 32)


def _make_pool(servers: list[str], **kwargs: Any) -> DirectoryClientPool:
    return DirectoryClientPool(
        directory_servers=servers,
        network="mainnet",
        nick_identity=_identity(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_connect_to_directory_parses_address_and_connects():
    pool = _make_pool(["onion.example:5222"])
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(return_value=None)

    with patch("jmcore.directory_pool.DirectoryClient", return_value=fake_client) as ctor:
        result = await pool.connect_to_directory("onion.example:5222")

    assert result is not None
    node_id, client = result
    assert node_id == "onion.example:5222"
    assert client is fake_client
    fake_client.connect.assert_awaited_once()
    # node_id is NOT registered by connect_to_directory itself.
    assert pool.clients == {}
    # SOCKS isolation creds should be None when not enabled.
    kwargs = ctor.call_args.kwargs
    assert kwargs["socks_username"] is None
    assert kwargs["socks_password"] is None


@pytest.mark.asyncio
async def test_connect_to_directory_returns_none_on_failure():
    pool = _make_pool(["bad.example:5222"])
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(side_effect=ConnectionRefusedError("boom"))

    with patch("jmcore.directory_pool.DirectoryClient", return_value=fake_client):
        result = await pool.connect_to_directory("bad.example:5222")

    assert result is None


@pytest.mark.asyncio
async def test_connect_to_directory_returns_none_on_unparseable_address():
    pool = _make_pool([])
    # Force parse to fail.
    with patch("jmcore.directory_pool.parse_directory_address", side_effect=ValueError("x")):
        result = await pool.connect_to_directory("not-an-address!!")
    assert result is None


@pytest.mark.asyncio
async def test_stream_isolation_supplies_socks_credentials():
    pool = _make_pool(["onion.example:5222"], stream_isolation=True)
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(return_value=None)

    with patch("jmcore.directory_pool.DirectoryClient", return_value=fake_client) as ctor:
        await pool.connect_to_directory("onion.example:5222")

    kwargs = ctor.call_args.kwargs
    assert kwargs["socks_username"] is not None
    assert kwargs["socks_password"] is not None


@pytest.mark.asyncio
async def test_connect_all_parallel_fires_hook_for_each_success():
    pool = _make_pool(["a.onion:5222", "b.onion:5222", "c.onion:5222"])

    hook_calls: list[str] = []

    async def hook(node_id: str, _client: Any) -> None:
        hook_calls.append(node_id)

    pool._on_directory_connected = hook  # type: ignore[method-assign]

    def make_client() -> MagicMock:
        c = MagicMock()
        c.connect = AsyncMock(return_value=None)
        return c

    clients = [make_client(), make_client(), make_client()]
    # Second one will fail to simulate partial outage.
    clients[1].connect.side_effect = OSError("connection refused")

    call_iter = iter(clients)
    with patch("jmcore.directory_pool.DirectoryClient", side_effect=lambda **_: next(call_iter)):
        connected = await pool.connect_all_parallel()

    assert connected == 2
    assert set(pool.clients.keys()) == {"a.onion:5222", "c.onion:5222"}
    assert set(hook_calls) == {"a.onion:5222", "c.onion:5222"}


@pytest.mark.asyncio
async def test_connect_all_with_retry_returns_zero_on_timeout():
    pool = _make_pool(["bad.onion:5222"])
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(side_effect=OSError("nope"))

    with patch("jmcore.directory_pool.DirectoryClient", return_value=fake_client):
        # initial_delay > timeout -> single pass, no sleep needed.
        n = await pool.connect_all_with_retry(timeout=0.01, initial_delay=0.001)

    assert n == 0
    assert pool.clients == {}


@pytest.mark.asyncio
async def test_connect_all_with_retry_succeeds_on_second_pass():
    pool = _make_pool(["maybe.onion:5222"])

    call_count = {"n": 0}

    def make_client(**_: Any) -> MagicMock:
        c = MagicMock()
        call_count["n"] += 1

        async def connect_impl() -> None:
            # First attempt fails, second succeeds.
            if call_count["n"] == 1:
                raise OSError("Tor still bootstrapping")

        c.connect = AsyncMock(side_effect=connect_impl)
        return c

    with patch("jmcore.directory_pool.DirectoryClient", side_effect=make_client):
        n = await pool.connect_all_with_retry(timeout=5.0, initial_delay=0.01)

    assert n == 1
    assert "maybe.onion:5222" in pool.clients


@pytest.mark.asyncio
async def test_list_disconnected_reports_only_configured_and_parseable():
    pool = _make_pool(["a.onion:5222", "b.onion:5222"])
    pool.clients["a.onion:5222"] = MagicMock()

    pairs = pool.list_disconnected()
    assert pairs == [("b.onion:5222", "b.onion:5222")]


@pytest.mark.asyncio
async def test_reconnect_disconnected_only_targets_missing():
    pool = _make_pool(["a.onion:5222", "b.onion:5222"])
    existing = MagicMock()
    pool.clients["a.onion:5222"] = existing

    new_client = MagicMock()
    new_client.connect = AsyncMock(return_value=None)

    with patch("jmcore.directory_pool.DirectoryClient", return_value=new_client):
        result = await pool.reconnect_disconnected()

    assert len(result) == 1
    assert result[0][0] == "b.onion:5222"
    assert pool.clients["a.onion:5222"] is existing  # untouched
    assert pool.clients["b.onion:5222"] is new_client


@pytest.mark.asyncio
async def test_close_all_clears_clients_and_invokes_hook():
    pool = _make_pool(["a.onion:5222"])
    c1 = MagicMock()
    c1.close = AsyncMock(return_value=None)
    pool.clients["a.onion:5222"] = c1

    disconnected: list[str] = []

    async def hook(node_id: str) -> None:
        disconnected.append(node_id)

    pool._on_directory_disconnected = hook  # type: ignore[method-assign]

    await pool.close_all()

    c1.close.assert_awaited_once()
    assert pool.clients == {}
    assert disconnected == ["a.onion:5222"]


@pytest.mark.asyncio
async def test_close_all_swallows_errors():
    pool = _make_pool(["a.onion:5222"])
    c1 = MagicMock()
    c1.close = AsyncMock(side_effect=RuntimeError("late socket error"))
    pool.clients["a.onion:5222"] = c1

    # Must not raise even though close blew up.
    await pool.close_all()
    assert pool.clients == {}


@pytest.mark.asyncio
async def test_subclass_can_inject_extra_kwargs_via_build_client_kwargs():
    class Specialized(DirectoryClientPool):
        def _build_client_kwargs(self, host: str, port: int) -> dict[str, Any]:
            base = super()._build_client_kwargs(host, port)
            base["location"] = "abc.onion:1"
            base["neutrino_compat"] = True
            return base

    pool = Specialized(
        directory_servers=["x.onion:5222"],
        network="mainnet",
        nick_identity=_identity(),
    )
    fake_client = MagicMock()
    fake_client.connect = AsyncMock(return_value=None)

    with patch("jmcore.directory_pool.DirectoryClient", return_value=fake_client) as ctor:
        await pool.connect_to_directory("x.onion:5222")

    kwargs = ctor.call_args.kwargs
    assert kwargs["location"] == "abc.onion:1"
    assert kwargs["neutrino_compat"] is True


def test_pool_does_not_perform_io_in_constructor():
    # Constructor should be cheap and not touch the network or event loop
    # state, so that simply building a pool inside __init__ paths of
    # higher-level components remains safe.
    loop_was_running = False
    try:
        asyncio.get_running_loop()
        loop_was_running = True
    except RuntimeError:
        pass
    pool = _make_pool(["a.onion:5222"])
    assert pool.clients == {}
    assert loop_was_running is False  # sanity: this is a sync test
