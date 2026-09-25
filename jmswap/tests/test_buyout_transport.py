from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import grpc
import pytest

from jmswap.buyout_messages import BuyoutCancel, BuyoutMessage, BuyoutStatus, encode_buyout_payload
from jmswap.buyout_transport import BuyoutTransport, PrivateTransportError
from jmswap.lnd_peer import LndPeerClient, LndPeerRpcError, PeerMessage

PEER = "02" + "11" * 32
OTHER = "03" + "22" * 32
EPOCH = "33" * 32
ACCEPT = "44" * 32


def request_message() -> BuyoutCancel:
    return BuyoutCancel(
        v=1,
        type="buyout_cancel",
        epoch_id=EPOCH,
        attempt=0,
        accept_hash=ACCEPT,
        reason_code="operator_cancel",
    )


def response_message(epoch: str = EPOCH) -> BuyoutStatus:
    return BuyoutStatus(
        v=1, type="buyout_status", epoch_id=epoch, attempt=0, accept_hash=ACCEPT, stage="canceled"
    )


class FakePeer:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[PeerMessage | Exception] = asyncio.Queue()
        self.sent: asyncio.Queue[tuple[bytes, bytes]] = asyncio.Queue()
        self.closed = False
        self.send_failures: list[Exception] = []

    async def send(self, peer: bytes, payload: bytes) -> None:
        await self.sent.put((peer, payload))
        if self.send_failures:
            raise self.send_failures.pop(0)

    async def receive(self) -> AsyncIterator[PeerMessage]:
        try:
            while True:
                item = await self.incoming.get()
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.closed = True

    def reply(self, message: BuyoutMessage, peer: str = PEER) -> None:
        self.incoming.put_nowait(PeerMessage(bytes.fromhex(peer), encode_buyout_payload(message)))

    def transport(self, **kwargs: object) -> BuyoutTransport:
        return BuyoutTransport(cast(LndPeerClient, self), frozenset({PEER}), **kwargs)


async def test_response_requires_matching_peer_epoch_and_type() -> None:
    peer = FakePeer()
    async with peer.transport() as transport:
        task = asyncio.create_task(transport.request(PEER, request_message()))
        await peer.sent.get()
        peer.reply(response_message(), OTHER)
        peer.reply(response_message("55" * 32))
        peer.reply(request_message())
        peer.incoming.put_nowait(PeerMessage(bytes.fromhex(PEER), b"invalid"))
        await asyncio.sleep(0)
        assert not task.done()
        peer.reply(response_message())
        assert await task == response_message()
    assert peer.closed


async def test_liveness_tracks_subscription_and_shutdown() -> None:
    peer = FakePeer()
    transport = peer.transport()
    assert not transport.alive
    async with transport:
        assert transport.alive
        peer.incoming.put_nowait(RuntimeError("connection lost"))
        await asyncio.sleep(0)
        assert not transport.alive
    assert not transport.alive


async def test_retries_identical_bytes_and_requires_reconciliation() -> None:
    peer = FakePeer()
    async with peer.transport(timeout=0.001) as transport:
        with pytest.raises(PrivateTransportError, match="reconciliation required"):
            await transport.request(PEER, request_message())
    sent = [peer.sent.get_nowait() for _ in range(3)]
    assert sent == [(bytes.fromhex(PEER), encode_buyout_payload(request_message()))] * 3
    assert peer.sent.empty()


@pytest.mark.parametrize("code", [grpc.StatusCode.NOT_FOUND, grpc.StatusCode.UNAVAILABLE])
async def test_offline_peer_costs_one_attempt_and_resends_identical_bytes(
    code: grpc.StatusCode,
) -> None:
    # LND answers NOT_FOUND while a flapping channel peer reconnects.
    peer = FakePeer()
    peer.send_failures.append(LndPeerRpcError("SendCustomMessage", code))
    async with peer.transport(timeout=0.01) as transport:
        task = asyncio.create_task(transport.request(PEER, request_message()))
        first = await asyncio.wait_for(peer.sent.get(), 5)
        second = await asyncio.wait_for(peer.sent.get(), 5)
        assert first == second == (bytes.fromhex(PEER), encode_buyout_payload(request_message()))
        peer.reply(response_message())
        assert await task == response_message()


async def test_reply_to_copy_sent_before_disconnect_is_accepted() -> None:
    peer = FakePeer()
    peer.send_failures.append(LndPeerRpcError("SendCustomMessage", grpc.StatusCode.NOT_FOUND))
    async with peer.transport(timeout=5) as transport:
        task = asyncio.create_task(transport.request(PEER, request_message()))
        await peer.sent.get()
        peer.reply(response_message())
        assert await task == response_message()
    assert peer.sent.empty()


async def test_peer_offline_on_every_attempt_requires_reconciliation() -> None:
    peer = FakePeer()
    peer.send_failures.extend(
        LndPeerRpcError("SendCustomMessage", grpc.StatusCode.NOT_FOUND) for _ in range(3)
    )
    async with peer.transport(timeout=0.001) as transport:
        with pytest.raises(PrivateTransportError, match="reconciliation required"):
            await transport.request(PEER, request_message())
    assert peer.sent.qsize() == 3


async def test_non_transient_send_failure_is_not_retried() -> None:
    peer = FakePeer()
    peer.send_failures.append(
        LndPeerRpcError("SendCustomMessage", grpc.StatusCode.PERMISSION_DENIED)
    )
    async with peer.transport(timeout=0.001) as transport:
        with pytest.raises(LndPeerRpcError):
            await transport.request(PEER, request_message())
    assert peer.sent.qsize() == 1


async def test_subscription_failure_wakes_pending_request() -> None:
    peer = FakePeer()
    async with peer.transport() as transport:
        task = asyncio.create_task(transport.request(PEER, request_message()))
        await peer.sent.get()
        peer.incoming.put_nowait(RuntimeError("sensitive RPC details"))
        with pytest.raises(PrivateTransportError, match="subscription ended"):
            await task
        with pytest.raises(PrivateTransportError, match="unavailable"):
            await transport.request(PEER, request_message())


async def test_concurrent_session_and_unknown_peer_are_rejected() -> None:
    peer = FakePeer()
    async with peer.transport() as transport:
        task = asyncio.create_task(transport.request(PEER, request_message()))
        await peer.sent.get()
        with pytest.raises(PrivateTransportError, match="capacity"):
            await transport.request(PEER, request_message())
        with pytest.raises(PrivateTransportError, match="authorized"):
            await transport.request(OTHER, request_message())
        peer.reply(response_message())
        await task


async def test_close_cancels_handlers_without_sending_false_ack() -> None:
    peer = FakePeer()
    entered, exited = asyncio.Event(), asyncio.Event()

    async def handle(identity: str, message: BuyoutMessage) -> BuyoutMessage:
        assert identity == PEER
        entered.set()
        try:
            await asyncio.Future()
        finally:
            exited.set()
        return response_message()

    transport = peer.transport(handler=handle)
    async with transport:
        peer.reply(request_message(), OTHER)
        await asyncio.sleep(0)
        assert not entered.is_set()
        peer.reply(request_message())
        await entered.wait()
    assert exited.is_set()
    assert peer.sent.empty()
    with pytest.raises(PrivateTransportError, match="closed"):
        await transport.request(PEER, request_message())


async def test_handler_failure_does_not_send_rejection() -> None:
    peer = FakePeer()
    handled = asyncio.Event()

    async def handle(identity: str, message: BuyoutMessage) -> BuyoutMessage:
        handled.set()
        raise RuntimeError("uncertain signing side effect")

    async with peer.transport(handler=handle):
        peer.reply(request_message())
        await handled.wait()
    assert peer.sent.empty()


async def test_handler_response_is_sent_to_authenticated_sender() -> None:
    peer = FakePeer()

    async def handle(identity: str, message: BuyoutMessage) -> BuyoutMessage:
        assert identity == PEER
        assert message == request_message()
        return response_message()

    async with peer.transport(handler=handle):
        peer.reply(request_message())
        assert await peer.sent.get() == (
            bytes.fromhex(PEER),
            encode_buyout_payload(response_message()),
        )


async def test_handler_work_is_bounded() -> None:
    peer = FakePeer()
    running = 0
    started = asyncio.Event()

    async def handle(identity: str, message: BuyoutMessage) -> BuyoutMessage:
        nonlocal running
        running += 1
        if running == 8:
            started.set()
        await asyncio.Future()
        return response_message()

    async with peer.transport(handler=handle):
        for _ in range(20):
            peer.reply(request_message())
        await started.wait()
        await asyncio.sleep(0)
        assert running == 8


async def test_close_wakes_pending_requests() -> None:
    peer = FakePeer()
    async with peer.transport() as transport:
        task = asyncio.create_task(transport.request(PEER, request_message()))
        await peer.sent.get()
    with pytest.raises(PrivateTransportError, match="subscription ended"):
        await task
