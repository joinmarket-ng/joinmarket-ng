from __future__ import annotations

import abc
import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any

from jmswap.errors import ProtocolError
from jmswap.models import SwapOffer
from jmswap.nip04 import (
    create_dm,
    create_event,
    nip04_decrypt,
    privkey_to_xonly_pubkey,
    verify_event,
)

SWAP_DISCOVERY_KIND = 30315
SWAP_RPC_KIND = 25582
DISCOVERY_D_TAG = "electrum-swapserver-5"
MAX_EVENT_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 60
RpcHandler = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class EventTransport(abc.ABC):
    @abc.abstractmethod
    async def publish(self, event: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def subscribe(self, filters: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]: ...


def _matches(event: dict[str, Any], event_filter: dict[str, Any]) -> bool:
    if "kinds" in event_filter and event.get("kind") not in event_filter["kinds"]:
        return False
    if "authors" in event_filter and event.get("pubkey") not in event_filter["authors"]:
        return False
    for name, values in event_filter.items():
        if name.startswith("#"):
            actual = {
                tag[1]
                for tag in event.get("tags", [])
                if isinstance(tag, list) and len(tag) >= 2 and tag[0] == name[1:]
            }
            if not actual.intersection(values):
                return False
    return True


class LoopbackEventTransport(EventTransport):
    def __init__(self) -> None:
        self._stored: list[dict[str, Any]] = []
        self._subscribers: list[tuple[list[dict[str, Any]], asyncio.Queue[dict[str, Any]]]] = []

    async def publish(self, event: dict[str, Any]) -> None:
        if not verify_event(event):
            raise ProtocolError("invalid Nostr event signature")
        self._stored.append(event)
        for filters, queue in self._subscribers:
            if any(_matches(event, item) for item in filters):
                await queue.put(event)

    async def subscribe(self, filters: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in self._stored:
            if any(_matches(event, item) for item in filters):
                await queue.put(event)
        self._subscribers.append((filters, queue))
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers = [item for item in self._subscribers if item[1] is not queue]


class RelayEventTransport(EventTransport):
    """Minimal NIP-01 websocket relay adapter for real Electrum Nostr transport."""

    def __init__(self, relay_urls: tuple[str, ...]) -> None:
        if not relay_urls:
            raise ValueError("at least one Nostr relay is required")
        self._relay_urls = relay_urls

    async def publish(self, event: dict[str, Any]) -> None:
        import websockets

        message = json.dumps(["EVENT", event])
        for url in self._relay_urls:
            try:
                async with websockets.connect(url) as websocket:
                    await websocket.send(message)
                    await asyncio.wait_for(websocket.recv(), 5)
            except Exception as exc:  # noqa: BLE001
                raise ProtocolError(f"failed to publish to Nostr relay {url}") from exc

    async def subscribe(self, filters: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
        import websockets

        subscription_id = uuid.uuid4().hex[:16]
        async with websockets.connect(self._relay_urls[0]) as websocket:
            await websocket.send(json.dumps(["REQ", subscription_id, *filters]))
            async for raw_message in websocket:
                message = json.loads(raw_message)
                if (
                    isinstance(message, list)
                    and len(message) >= 3
                    and message[0] == "EVENT"
                    and message[1] == subscription_id
                    and isinstance(message[2], dict)
                ):
                    yield message[2]
                elif (
                    isinstance(message, list)
                    and len(message) >= 2
                    and message[0] == "CLOSED"
                    and message[1] == subscription_id
                ):
                    return


class ElectrumNostrAdapter:
    """Electrum discovery and top-level NIP-04 request/reply transport."""

    def __init__(self, privkey: bytes, transport: EventTransport) -> None:
        self._privkey = privkey
        self._transport = transport
        self.pubkey = privkey_to_xonly_pubkey(privkey)
        self._provider_pubkey: str | None = None
        self._seen: dict[str, None] = {}

    def select_provider(self, pubkey: str) -> None:
        if len(pubkey) != 64:
            raise ValueError("provider Nostr public key must be x-only hex")
        bytes.fromhex(pubkey)
        self._provider_pubkey = pubkey

    async def publish_offer(
        self, offer: SwapOffer, *, network: str, expires_at: int
    ) -> dict[str, Any]:
        event = create_event(
            offer.model_dump_json(),
            self._privkey,
            SWAP_DISCOVERY_KIND,
            [
                ["d", DISCOVERY_D_TAG],
                ["r", f"net:{network}"],
                ["expiration", str(expires_at)],
            ],
        )
        await self._transport.publish(event)
        return event

    async def discover(self, *, network: str, timeout: float = 5.0) -> list[tuple[str, SwapOffer]]:
        found: dict[str, tuple[int, SwapOffer]] = {}
        filters = [
            {
                "kinds": [SWAP_DISCOVERY_KIND],
                "#d": [DISCOVERY_D_TAG],
                "#r": [f"net:{network}"],
            }
        ]

        async def collect() -> None:
            async for event in self._transport.subscribe(filters):
                if not verify_event(event):
                    continue
                created = event.get("created_at")
                now = int(time.time())
                if not isinstance(created, int) or not now - 3600 <= created <= now + 3600:
                    continue
                tags = {
                    tag[0]: tag[1]
                    for tag in event.get("tags", [])
                    if isinstance(tag, list) and len(tag) >= 2
                }
                if tags.get("d") != DISCOVERY_D_TAG or tags.get("r") != f"net:{network}":
                    continue
                try:
                    if int(tags.get("expiration", "0")) <= now:
                        continue
                    offer = SwapOffer.model_validate_json(event["content"])
                except (ValueError, TypeError):
                    continue
                previous = found.get(event["pubkey"])
                if previous is None or created > previous[0]:
                    found[event["pubkey"]] = (created, offer)

        with suppress(TimeoutError):
            await asyncio.wait_for(collect(), timeout)
        return [(pubkey, item[1]) for pubkey, item in found.items()]

    async def request(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        provider = self._provider_pubkey
        if provider is None:
            raise ProtocolError("no Nostr swap provider selected")
        if "method" in payload or "reply_to" in payload:
            raise ProtocolError("reserved Nostr RPC field in canonical payload")
        reply_filter = [{"kinds": [SWAP_RPC_KIND], "#p": [self.pubkey], "authors": [provider]}]
        subscription = self._transport.subscribe(reply_filter)

        async def await_reply(request_id: str) -> dict[str, Any]:
            async for event in subscription:
                if not self._validate_rpc_event(
                    event, recipient=self.pubkey, expected_sender=provider
                ):
                    continue
                try:
                    reply = json.loads(nip04_decrypt(event["content"], self._privkey, provider))
                except (ValueError, TypeError, json.JSONDecodeError):
                    continue
                if not isinstance(reply, dict) or reply.get("reply_to") != request_id:
                    continue
                result = dict(reply)
                result.pop("reply_to")
                if "error" in result:
                    raise ProtocolError("swap provider returned an error")
                return result
            raise ProtocolError("Nostr reply stream ended")

        request_content = dict(payload)
        request_content["method"] = method
        event = create_dm(json.dumps(request_content), self._privkey, provider, kind=SWAP_RPC_KIND)
        task = asyncio.create_task(await_reply(event["id"]))
        await asyncio.sleep(0)
        await self._transport.publish(event)
        try:
            return await asyncio.wait_for(task, 30)
        except TimeoutError as exc:
            task.cancel()
            raise ProtocolError(f"swap RPC {method} timed out") from exc

    async def serve(self, handler: RpcHandler) -> None:
        filters = [{"kinds": [SWAP_RPC_KIND], "#p": [self.pubkey]}]
        async for event in self._transport.subscribe(filters):
            if not self._validate_rpc_event(event, recipient=self.pubkey):
                continue
            sender = event["pubkey"]
            try:
                request = json.loads(nip04_decrypt(event["content"], self._privkey, sender))
                if not isinstance(request, dict):
                    raise ValueError("request must be an object")
                method = request.pop("method")
                if not isinstance(method, str) or "reply_to" in request:
                    raise ValueError("invalid request method")
                response = await handler(method, request)
                if "reply_to" in response:
                    raise ValueError("handler returned reserved reply field")
                reply = dict(response)
            except Exception:  # noqa: BLE001
                reply = {"error": "Internal Server Error"}
            reply["reply_to"] = event["id"]
            await self._transport.publish(
                create_dm(json.dumps(reply), self._privkey, sender, kind=SWAP_RPC_KIND)
            )

    def _validate_rpc_event(
        self,
        event: dict[str, Any],
        *,
        recipient: str,
        expected_sender: str | None = None,
    ) -> bool:
        if event.get("kind") != SWAP_RPC_KIND or not verify_event(event):
            return False
        if expected_sender is not None and event.get("pubkey") != expected_sender:
            return False
        recipients = [
            tag[1]
            for tag in event.get("tags", [])
            if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "p"
        ]
        if recipients != [recipient]:
            return False
        now = int(time.time())
        created = event.get("created_at")
        if not isinstance(created, int):
            return False
        if created < now - MAX_EVENT_AGE_SECONDS or created > now + MAX_FUTURE_SKEW_SECONDS:
            return False
        identifier = event.get("id")
        if not isinstance(identifier, str) or identifier in self._seen:
            return False
        self._seen[identifier] = None
        if len(self._seen) > 10_000:
            self._seen.pop(next(iter(self._seen)))
        return True
