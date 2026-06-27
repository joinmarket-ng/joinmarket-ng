"""
Nostr transport for swap discovery and RPC.

A swap provider advertises its :class:`~jmswap.models.ProviderTerms` as a public
(addressable) Nostr event and answers swap RPCs sent as NIP-04 encrypted direct
messages; a client discovers providers from the adverts and calls RPCs over the
same DMs. The relay layer is abstracted behind :class:`NostrTransport` so the
protocol can run over real relays (:class:`RelayTransport`) or an in-process
:class:`LoopbackRelay` for tests and embedded use.

Event kinds:
    * ``SWAP_ADVERT_KIND`` (31000) - addressable advert carrying the terms JSON.
    * ``SWAP_RPC_KIND`` (25582) - ephemeral NIP-04 DM carrying a swap RPC.
"""

from __future__ import annotations

import abc
import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from jmswap.errors import SwapProtocolError
from jmswap.models import ProviderTerms
from jmswap.nip04 import (
    create_event,
    create_nip04_dm_event,
    nip04_decrypt,
    nip04_encrypt,
    privkey_to_xonly_pubkey,
    sign_event,
    verify_event,
)

SWAP_ADVERT_KIND = 31000
SWAP_RPC_KIND = 25582
ADVERT_D_TAG = "jmswap-provider-v1"

DEFAULT_RELAYS = ("ws://localhost:7000",)

# An RPC handler maps (method, params, sender_pubkey) -> a result dict.
RpcHandler = Callable[[str, dict[str, Any], str], Coroutine[Any, Any, dict[str, Any]]]


class NostrTransport(abc.ABC):
    """A minimal publish/subscribe interface over one or more Nostr relays."""

    @abc.abstractmethod
    async def publish(self, event: dict[str, Any]) -> None:
        """Publish a signed event."""

    @abc.abstractmethod
    def subscribe(self, filters: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        """Subscribe to events matching ``filters`` (NIP-01), yielding events."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None


def _event_matches(event: dict[str, Any], filt: dict[str, Any]) -> bool:
    if "kinds" in filt and event.get("kind") not in filt["kinds"]:
        return False
    if "authors" in filt and event.get("pubkey") not in filt["authors"]:
        return False
    for key, values in filt.items():
        if key.startswith("#"):
            tag_name = key[1:]
            tag_values = {t[1] for t in event.get("tags", []) if len(t) >= 2 and t[0] == tag_name}
            if not tag_values.intersection(values):
                return False
    return True


class LoopbackRelay(NostrTransport):
    """An in-process relay: events published here are delivered to subscribers.

    Lets a provider and a client run the full advert/RPC protocol in one process
    (used by tests and embedded single-process deployments).
    """

    def __init__(self) -> None:
        self._subscribers: list[tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any]]]] = []
        self._stored: list[dict[str, Any]] = []

    async def publish(self, event: dict[str, Any]) -> None:
        if not verify_event(event):
            raise SwapProtocolError("refusing to publish an event with an invalid signature")
        self._stored.append(event)
        for filters, queue in self._subscribers:
            if any(_event_matches(event, f) for f in filters):
                await queue.put(event)

    async def subscribe(  # type: ignore[override]
        self, filters: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        # Replay stored events that match (so late subscribers still see adverts).
        for event in self._stored:
            if any(_event_matches(event, f) for f in filters):
                await queue.put(event)
        self._subscribers.append((filters, queue))
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers = [(f, q) for (f, q) in self._subscribers if q is not queue]


class RelayTransport(NostrTransport):
    """A transport backed by one or more real relays over websockets."""

    def __init__(self, relay_urls: tuple[str, ...] = DEFAULT_RELAYS) -> None:
        self._relay_urls = relay_urls

    async def publish(self, event: dict[str, Any]) -> None:
        import websockets

        message = json.dumps(["EVENT", event])
        for url in self._relay_urls:
            try:
                async with websockets.connect(url) as ws:
                    await ws.send(message)
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), timeout=5)
            except Exception as exc:  # noqa: BLE001 - best-effort fan-out
                raise SwapProtocolError(f"failed to publish to relay {url}: {exc}") from exc

    async def subscribe(  # type: ignore[override]
        self, filters: list[dict[str, Any]]
    ) -> AsyncIterator[dict[str, Any]]:
        import websockets

        sub_id = uuid.uuid4().hex[:16]
        # A single relay is used for the streaming subscription for simplicity.
        url = self._relay_urls[0]
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps(["REQ", sub_id, *filters]))
            async for raw in ws:
                msg = json.loads(raw)
                if msg[0] == "EVENT" and msg[1] == sub_id:
                    yield msg[2]
                elif msg[0] == "CLOSED" and msg[1] == sub_id:  # pragma: no cover
                    break


@dataclass
class SwapAdvert:
    """A discovered provider advert: the terms plus the advertising Nostr key."""

    nostr_pubkey: str
    terms: ProviderTerms


class SwapNostrNode:
    """A Nostr identity that can advertise terms, serve RPCs, discover and call.

    The same object backs both roles; a provider uses :meth:`advertise` and
    :meth:`serve`, a client uses :meth:`discover` and :meth:`call`.
    """

    def __init__(self, privkey: bytes, transport: NostrTransport) -> None:
        self._privkey = privkey
        self._transport = transport
        self.pubkey = privkey_to_xonly_pubkey(privkey)

    # -- provider side --------------------------------------------------------

    async def advertise(self, terms: ProviderTerms) -> dict[str, Any]:
        """Publish an addressable advert carrying ``terms``."""
        content = terms.model_dump_json()
        event = create_event(content, self._privkey, SWAP_ADVERT_KIND, tags=[["d", ADVERT_D_TAG]])
        await self._transport.publish(event)
        return event

    async def serve(self, handler: RpcHandler) -> None:
        """Answer incoming swap RPC DMs forever, dispatching to ``handler``.

        Each request is a NIP-04 DM whose plaintext is
        ``{"id", "method", "params"}``; the reply is a DM back to the sender with
        ``{"id", "result"}`` or ``{"id", "error"}``.
        """
        filters = [{"kinds": [SWAP_RPC_KIND], "#p": [self.pubkey]}]
        async for event in self._transport.subscribe(filters):
            sender = event["pubkey"]
            try:
                plaintext = nip04_decrypt(event["content"], self._privkey, sender)
                request = json.loads(plaintext)
                method = request["method"]
                params = request.get("params", {})
            except Exception:  # noqa: BLE001 - ignore undecryptable / malformed DMs
                continue
            reply: dict[str, Any] = {"id": request.get("id")}
            try:
                reply["result"] = await handler(method, params, sender)
            except Exception as exc:  # noqa: BLE001 - surface handler errors to the client
                reply["error"] = {"message": str(exc)}
            await self._send_dm(sender, json.dumps(reply))

    # -- client side ----------------------------------------------------------

    async def discover(self, timeout: float = 5.0) -> list[SwapAdvert]:
        """Collect provider adverts seen within ``timeout`` seconds."""
        filters = [{"kinds": [SWAP_ADVERT_KIND], "#d": [ADVERT_D_TAG]}]
        adverts: dict[str, SwapAdvert] = {}

        async def _collect() -> None:
            async for event in self._transport.subscribe(filters):
                if not verify_event(event):
                    continue
                with suppress(Exception):
                    terms = ProviderTerms.model_validate_json(event["content"])
                    adverts[event["pubkey"]] = SwapAdvert(event["pubkey"], terms)

        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_collect(), timeout=timeout)
        return list(adverts.values())

    async def call(
        self, provider_pubkey: str, method: str, params: dict[str, Any], timeout: float = 30.0
    ) -> dict[str, Any]:
        """Send a swap RPC to ``provider_pubkey`` and await the reply."""
        request_id = uuid.uuid4().hex
        # Subscribe for the reply *before* publishing to avoid a race.
        reply_filter = [
            {"kinds": [SWAP_RPC_KIND], "#p": [self.pubkey], "authors": [provider_pubkey]}
        ]
        sub = self._transport.subscribe(reply_filter)

        async def _await_reply() -> dict[str, Any]:
            async for event in sub:
                try:
                    plaintext = nip04_decrypt(event["content"], self._privkey, provider_pubkey)
                    reply = json.loads(plaintext)
                except Exception:  # noqa: BLE001 - skip DMs we cannot read
                    continue
                if reply.get("id") != request_id:
                    continue
                if "error" in reply:
                    raise SwapProtocolError(reply["error"].get("message", "swap RPC error"))
                result = reply.get("result", {})
                return result if isinstance(result, dict) else {"result": result}
            raise SwapProtocolError("reply stream ended")

        reply_task = asyncio.create_task(_await_reply())
        await asyncio.sleep(0)  # let the subscription register
        await self._send_dm(
            provider_pubkey, json.dumps({"id": request_id, "method": method, "params": params})
        )
        try:
            return await asyncio.wait_for(reply_task, timeout=timeout)
        except TimeoutError as exc:
            reply_task.cancel()
            raise SwapProtocolError(f"swap RPC {method} timed out") from exc

    # -- helpers --------------------------------------------------------------

    async def _send_dm(self, recipient_pubkey: str, content: str) -> None:
        event = create_nip04_dm_event(content, self._privkey, recipient_pubkey, kind=SWAP_RPC_KIND)
        await self._transport.publish(event)

    def encrypt_to(self, recipient_pubkey: str, content: str) -> str:
        return nip04_encrypt(content, self._privkey, recipient_pubkey)

    def signed_dm(self, recipient_pubkey: str, content: str) -> dict[str, Any]:
        return sign_event(
            create_nip04_dm_event(content, self._privkey, recipient_pubkey, SWAP_RPC_KIND),
            self._privkey,
        )
