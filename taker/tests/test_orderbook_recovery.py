"""Preselection transport recovery does not retry financial exchanges."""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, Mock

import pytest
from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient, DirectoryClientError
from jmcore.models import Offer, OfferType
from jmcore.network import ConnectionError as NetworkConnectionError
from jmcore.nick_auth import NickAuthMode
from jmcore.protocol import MessageType

from taker.multi_directory import MultiDirectoryClient

SERVER = "directory.test:5222"


def pool(servers: list[str] | None = None) -> MultiDirectoryClient:
    return MultiDirectoryClient(
        directory_servers=servers if servers is not None else [SERVER],
        network="regtest",
        nick_identity=NickIdentity(5),
        nick_auth_mode=NickAuthMode.REQUIRE_VERIFIED,
        nick_auth_directory_ids={SERVER: "test:original-directory"},
        stream_isolation=True,
    )


def offer() -> Offer:
    return Offer(
        counterparty="maker",
        oid=0,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=10_000,
        maxsize=1_000_000,
        txfee=0,
        cjfee="0.001",
    )


def client(offers: list[Offer] | None = None) -> Mock:
    result = Mock(spec=DirectoryClient)
    result.fetch_orderbooks = AsyncMock(return_value=(offers or [], []))
    result.close = AsyncMock()
    return result


def transport_error() -> DirectoryClientError:
    error = DirectoryClientError("Peerlist transport failed")
    error.__cause__ = NetworkConnectionError("Connection closed by peer")
    return error


async def test_failed_existing_transport_is_replaced_once_before_selection() -> None:
    directory = pool()
    old, fresh = client(), client([offer()])
    old.fetch_orderbooks.side_effect = transport_error()
    directory.clients[SERVER] = old
    directory.connect_to_directory = AsyncMock(return_value=(SERVER, fresh))

    assert await directory.fetch_orderbook(max_wait=1, min_wait=0, reconnect=True) == [offer()]
    old.close.assert_awaited_once()
    old.fetch_orderbooks.assert_awaited_once()
    fresh.fetch_orderbooks.assert_awaited_once()
    directory.connect_to_directory.assert_awaited_once_with(SERVER)
    assert directory.clients[SERVER] is fresh
    assert 0 < fresh.fetch_orderbooks.call_args.kwargs["max_wait"] <= 1


@pytest.mark.parametrize("policy_error", [False, True])
async def test_empty_book_or_policy_rejection_does_not_trigger_reconnect(
    policy_error: bool,
) -> None:
    directory = pool()
    existing = client()
    if policy_error:
        # Policy rejection may close the socket too. That does not classify it
        # as a transport failure or permit automatic reauthentication.
        existing.fetch_orderbooks.side_effect = DirectoryClientError("Authentication rejected")
    directory.clients[SERVER] = existing
    directory.connect_to_directory = AsyncMock()

    assert await directory.fetch_orderbook(max_wait=1, reconnect=True) == []
    directory.connect_to_directory.assert_not_awaited()
    existing.close.assert_not_awaited()


@pytest.mark.parametrize("connect_succeeds", [False, True])
async def test_absent_directory_aliases_get_only_one_connection_opportunity(
    connect_succeeds: bool,
) -> None:
    directory = pool(["directory.test", SERVER])
    fresh = client()
    fresh.fetch_orderbooks.side_effect = transport_error()
    directory.connect_to_directory = AsyncMock(
        return_value=(SERVER, fresh) if connect_succeeds else None
    )

    assert await directory.fetch_orderbook(max_wait=1, reconnect=True) == []
    directory.connect_to_directory.assert_awaited_once_with(SERVER)
    assert directory.clients == {}
    if connect_succeeds:
        fresh.fetch_orderbooks.assert_awaited_once()
        fresh.close.assert_awaited_once()


async def test_failed_refetch_does_not_connect_a_third_transport() -> None:
    directory = pool()
    old, fresh = client(), client()
    old.fetch_orderbooks.side_effect = transport_error()
    fresh.fetch_orderbooks.side_effect = transport_error()
    directory.clients[SERVER] = old
    directory.connect_to_directory = AsyncMock(return_value=(SERVER, fresh))

    assert await directory.fetch_orderbook(max_wait=1, reconnect=True) == []
    directory.connect_to_directory.assert_awaited_once()
    fresh.close.assert_awaited_once()
    assert directory.clients == {}


async def test_deadline_also_covers_peerlist_before_the_offer_timer() -> None:
    directory = pool()
    existing = DirectoryClient("127.0.0.1", 1, "regtest")
    existing.close = AsyncMock()

    async def stalled(**_: object) -> None:
        await asyncio.Event().wait()

    existing.get_peerlist_with_features = AsyncMock(side_effect=stalled)
    directory.clients[SERVER] = existing
    directory.connect_to_directory = AsyncMock()
    start = asyncio.get_running_loop().time()

    assert await directory.fetch_orderbook(max_wait=0.03, reconnect=True) == []
    assert asyncio.get_running_loop().time() - start < 0.5
    existing.close.assert_awaited_once()
    directory.connect_to_directory.assert_not_awaited()


async def test_handshake_deadline_cleans_up_the_original_authenticated_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = pool()
    fresh = client()
    connecting = asyncio.Event()

    async def stalled_connect() -> None:
        connecting.set()
        await asyncio.Event().wait()

    fresh.connect = AsyncMock(side_effect=stalled_connect)
    constructor = Mock(return_value=fresh)
    monkeypatch.setattr("jmcore.directory_pool.DirectoryClient", constructor)

    assert await directory.fetch_orderbook(max_wait=0.03, reconnect=True) == []
    assert connecting.is_set() and directory.clients == {}
    fresh.close.assert_awaited_once()
    kwargs = constructor.call_args.kwargs
    assert kwargs["nick_identity"] is directory.nick_identity
    assert kwargs["nick_auth_mode"] is NickAuthMode.REQUIRE_VERIFIED
    assert kwargs["nick_auth_directory_id"] == "test:original-directory"
    assert (kwargs["socks_username"], kwargs["socks_password"]) == directory._dir_creds


async def test_failed_instance_cannot_remove_a_concurrent_replacement() -> None:
    directory = pool()
    old, replacement = client(), client([offer()])

    async def replaced(**_: object) -> None:
        directory.clients[SERVER] = replacement
        raise transport_error()

    old.fetch_orderbooks.side_effect = replaced
    directory.clients[SERVER] = old
    directory.connect_to_directory = AsyncMock()

    assert await directory.fetch_orderbook(max_wait=1, reconnect=True) == [offer()]
    directory.connect_to_directory.assert_not_awaited()
    old.close.assert_awaited_once()
    replacement.close.assert_not_awaited()
    assert directory.clients[SERVER] is replacement


@pytest.mark.parametrize("close_during_hook", [False, True])
async def test_closing_pool_cannot_adopt_a_pending_candidate(close_during_hook: bool) -> None:
    directory = pool()
    fresh = client([offer()])
    entered, release = asyncio.Event(), asyncio.Event()

    async def connect(_: str) -> tuple[str, Mock]:
        if not close_during_hook:
            entered.set()
            await release.wait()
        return SERVER, fresh

    async def hook(_: str, __: DirectoryClient) -> None:
        if close_during_hook:
            entered.set()
            await release.wait()

    directory.connect_to_directory = AsyncMock(side_effect=connect)
    directory._on_directory_connected = hook
    task = asyncio.create_task(directory.fetch_orderbook(max_wait=1, reconnect=True))
    await asyncio.wait_for(entered.wait(), 1)
    await directory.close_all()
    release.set()

    assert await task == []
    assert directory.clients == {}
    fresh.close.assert_awaited()
    fresh.fetch_orderbooks.assert_not_awaited()


async def test_cancellation_during_adoption_hook_removes_and_closes_candidate() -> None:
    directory = pool()
    fresh = client()
    entered = asyncio.Event()

    async def hook(_: str, __: DirectoryClient) -> None:
        entered.set()
        await asyncio.Event().wait()

    directory.connect_to_directory = AsyncMock(return_value=(SERVER, fresh))
    directory._on_directory_connected = hook
    task = asyncio.create_task(directory.fetch_orderbook(max_wait=1, reconnect=True))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert directory.clients == {}
    fresh.close.assert_awaited_once()


async def test_recovery_and_response_collection_are_mutually_exclusive() -> None:
    directory = pool()
    entered = asyncio.Event()

    async def responses(*_: object) -> dict[str, dict[str, object]]:
        entered.set()
        await asyncio.Event().wait()
        return {}

    directory._wait_for_responses = AsyncMock(side_effect=responses)
    task = asyncio.create_task(directory.wait_for_responses(["maker"], "!ioauth"))
    await asyncio.wait_for(entered.wait(), 1)
    try:
        with pytest.raises(RuntimeError, match="must not overlap"):
            await directory.fetch_orderbook(max_wait=1, reconnect=True)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_zero_recovery_budget_does_not_open_a_transport() -> None:
    directory = pool()
    directory.connect_to_directory = AsyncMock()
    assert await directory.fetch_orderbook(max_wait=0, reconnect=True) == []
    directory.connect_to_directory.assert_not_awaited()


async def test_unexpected_child_failure_joins_other_readers_before_releasing_ownership() -> None:
    directory = pool(["one.test:5222", "two.test:5222"])
    broken, reading = client(), client()
    reading_started, canceled, cleanup = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def read(**_: object) -> None:
        reading_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            canceled.set()
            await cleanup.wait()
            raise

    async def hook(server: str, _: DirectoryClient) -> None:
        if server == "one.test:5222":
            await reading_started.wait()
            raise RuntimeError("unexpected connected-hook failure")

    directory.connect_to_directory = AsyncMock(
        side_effect=lambda server: (server, broken if server == "one.test:5222" else reading)
    )
    directory._on_directory_connected = hook
    reading.fetch_orderbooks.side_effect = read
    task = asyncio.create_task(directory.fetch_orderbook(max_wait=1, reconnect=True))
    await asyncio.wait_for(canceled.wait(), 1)
    try:
        with pytest.raises(RuntimeError, match="must not overlap"):
            await directory.wait_for_responses(["maker"], "!ioauth")
    finally:
        cleanup.set()
    with pytest.raises(ExceptionGroup, match="TaskGroup"):
        await task
    assert not directory._directory_read_phase.locked()
    broken.close.assert_awaited_once()
    reading.close.assert_awaited_once()


async def test_real_tcp_orderbook_fetch_recovers_after_an_idle_remote_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reuse one pool/identity across two fetches, without wallets or native nodes."""
    identities: list[str] = []
    writers: list[asyncio.StreamWriter] = []
    handlers: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        writers.append(writer)

        async def send(kind: MessageType, line: str) -> None:
            writer.write(json.dumps({"type": kind.value, "line": line}).encode() + b"\n")
            await writer.drain()

        try:
            handshake = json.loads(await reader.readline())
            identities.append(json.loads(handshake["line"])["nick"])
            await send(
                MessageType.DN_HANDSHAKE,
                json.dumps(
                    {"accepted": True, "proto-ver-min": 5, "proto-ver-max": 5, "nick": "directory"}
                ),
            )
            while raw := await reader.readline():
                message = json.loads(raw)
                if message["type"] == MessageType.GETPEERLIST.value:
                    await send(MessageType.PEERLIST, "")
                elif message["type"] == MessageType.PUBMSG.value:
                    await send(
                        MessageType.PUBMSG,
                        "J57wPBk1VfjSP5Te!PUBLIC!sw0reloffer 0 10000 1000000 0 0.001",
                    )
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            handlers.remove(task)

    def real_client(**kwargs: object) -> DirectoryClient:
        result = DirectoryClient(**kwargs)  # type: ignore[arg-type]
        result._peerlist_chunk_timeout = 0.01
        result._peerlist_min_interval = 0
        return result

    monkeypatch.setattr("jmcore.directory_pool.DirectoryClient", real_client)
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    directory = MultiDirectoryClient(
        [f"127.0.0.1:{port}"],
        "regtest",
        NickIdentity(5),
        nick_auth_mode=NickAuthMode.DISABLED,
    )
    try:
        assert await directory.connect_all() == 1
        # Disable quiet exit and require the full window: normal deadline
        # completion must retain offers, not cancel and discard the whole book.
        first = await directory.fetch_orderbook(
            max_wait=0.1, min_wait=0.1, quiet_period=0, reconnect=True
        )
        assert len(first) == 1
        assert len(identities) == 1
        original = next(iter(directory.clients.values()))
        writers[0].close()
        await writers[0].wait_closed()
        second = await directory.fetch_orderbook(
            max_wait=0.1, min_wait=0.1, quiet_period=0, reconnect=True
        )
        assert second == first
        assert len(identities) == 2 and identities == [directory.nick, directory.nick]
        assert next(iter(directory.clients.values())) is not original
    finally:
        await directory.close_all()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*handlers)
