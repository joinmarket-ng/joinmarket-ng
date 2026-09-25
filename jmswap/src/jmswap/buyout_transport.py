"""Bounded request/reply routing over authenticated Lightning custom messages."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Self

import grpc

from jmswap.buyout_messages import (
    BuyoutMessage,
    BuyoutMessageError,
    decode_buyout_payload,
    encode_buyout_payload,
)
from jmswap.lnd_peer import LndPeerClient, LndPeerRpcError

Handler = Callable[[str, BuyoutMessage], Awaitable[BuyoutMessage | None]]

# LND reports a momentarily disconnected peer as NOT_FOUND ("peer is not
# connected") and reconnects channel peers by itself, so these codes cost one
# attempt instead of ending the request. Other RPC errors stay fatal.
_TRANSIENT_SEND_CODES = frozenset({grpc.StatusCode.NOT_FOUND, grpc.StatusCode.UNAVAILABLE})

_RESPONSES: dict[str, frozenset[str]] = {
    "buyout_propose": frozenset({"buyout_accept", "buyout_reject"}),
    "buyout_parent": frozenset({"buyout_nonces", "buyout_reject"}),
    "buyout_split_partial": frozenset({"buyout_parent_partials", "buyout_reject"}),
    "buyout_cancel": frozenset({"buyout_status", "buyout_reject"}),
    "buyout_status": frozenset({"buyout_status", "buyout_invoice", "buyout_reject"}),
    "buyout_sweep": frozenset({"buyout_sweep_partial", "buyout_reject"}),
}


class PrivateTransportError(Exception):
    """Private peer delivery did not complete; the caller must reconcile its journal."""


class BuyoutTransport:
    """One subscription, bounded concurrent handlers, and one request per session.

    A timeout retransmits only the exact same bytes. The receiver must persist
    an idempotent response before sending it. A timeout is never cancellation.
    Peers outside the operator's explicit allowlist cannot allocate handler tasks.
    """

    def __init__(
        self,
        peer: LndPeerClient,
        allowed_peers: frozenset[str],
        handler: Handler | None = None,
        timeout: float = 30,
    ) -> None:
        if not allowed_peers or timeout <= 0:
            raise ValueError("an explicit peer allowlist and positive timeout are required")
        self.peer, self.allowed_peers, self.handler = peer, allowed_peers, handler
        self.timeout = timeout
        self._reader: asyncio.Task[None] | None = None
        self._handlers: set[asyncio.Task[None]] = set()
        self._pending: dict[
            tuple[str, str], tuple[frozenset[str], asyncio.Future[BuyoutMessage]]
        ] = {}
        self._closing = False

    @property
    def alive(self) -> bool:
        """Whether the private message subscription is still running."""
        return not self._closing and self._reader is not None and not self._reader.done()

    async def __aenter__(self) -> Self:
        if self._reader is not None:
            raise PrivateTransportError("transport cannot be entered more than once")
        self._reader = asyncio.create_task(self._receive())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *args: object) -> None:
        self._closing = True
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        tasks = list(self._handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for _, future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def request(self, peer: str, message: BuyoutMessage) -> BuyoutMessage:
        if self._closing:
            raise PrivateTransportError("private transport is closed")
        if peer not in self.allowed_peers or message.type not in _RESPONSES:
            raise PrivateTransportError("peer or request is not authorized")
        key = (peer, message.epoch_id)
        if key in self._pending or len(self._pending) >= 4:
            raise PrivateTransportError("private request capacity exceeded")
        encoded = encode_buyout_payload(message)
        future: asyncio.Future[BuyoutMessage] = asyncio.get_running_loop().create_future()
        responses = _RESPONSES[message.type]
        if message.type == "buyout_cancel" and message.proposal_hash is not None:
            responses = responses | {"buyout_cancel"}
        self._pending[key] = (responses, future)
        try:
            for _ in range(3):
                if self._reader is None or self._reader.done():
                    raise PrivateTransportError("private subscription is unavailable")
                try:
                    await self.peer.send(bytes.fromhex(peer), encoded)
                except LndPeerRpcError as exc:
                    if exc.code not in _TRANSIENT_SEND_CODES:
                        raise
                    # An earlier copy may still be answered while LND reconnects.
                try:
                    response = await asyncio.wait_for(asyncio.shield(future), self.timeout)
                except TimeoutError:
                    continue
                if response.type == "buyout_reject":
                    raise PrivateTransportError("counterparty rejected the private request")
                return response
            raise PrivateTransportError("private request timed out; reconciliation required")
        finally:
            self._pending.pop(key, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()

    async def _receive(self) -> None:
        try:
            async for item in self.peer.receive():
                identity = item.peer_pubkey.hex()
                if identity not in self.allowed_peers:
                    continue
                try:
                    message = decode_buyout_payload(item.payload)
                except BuyoutMessageError:
                    continue
                pending = self._pending.get((identity, message.epoch_id))
                if pending is not None and message.type in pending[0]:
                    if not pending[1].done():
                        pending[1].set_result(message)
                    continue
                if self.handler is not None and len(self._handlers) < 8:
                    task = asyncio.create_task(self._handle(identity, message))
                    self._handlers.add(task)
                    task.add_done_callback(self._handlers.discard)
        except Exception:
            # Requests receive a sanitized failure below; never expose RPC details.
            pass
        finally:
            for _, future in self._pending.values():
                if not future.done():
                    future.set_exception(PrivateTransportError("private subscription ended"))

    async def _handle(self, identity: str, message: BuyoutMessage) -> None:
        assert self.handler is not None
        # A failed handler retains its durable attempt; sending no reply avoids
        # turning an uncertain signing side effect into a false rejection ACK.
        with suppress(Exception):
            reply = await self.handler(identity, message)
            if reply is not None:
                await self.peer.send(bytes.fromhex(identity), encode_buyout_payload(reply))
