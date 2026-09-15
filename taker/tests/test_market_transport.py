"""Focused transport tests for credential-market discovery and private exchange."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore.credential_market import MarketError, canonical
from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient
from jmcore.network import ONION_HOSTID, TCPConnection
from jmcore.protocol import MessageType
from nacl.public import PrivateKey, PublicKey, SealedBox

from taker.market_transport import (
    MAX_IN_FLIGHT,
    MAX_LISTING_BYTES,
    MarketRequestTimeoutError,
    MarketTransport,
    _Inbound,
    _PendingRequest,
)


class LoopbackDirectory:
    """Minimal asynchronous relay that preserves DirectoryClient message shape."""

    def __init__(self) -> None:
        self.clients: dict[str, LoopbackDirectoryClient] = {}

    def attach(self, transport: MarketTransport) -> LoopbackDirectoryClient:
        client = LoopbackDirectoryClient(self, transport)
        self.clients[transport.nick] = client
        transport.clients["loopback"] = client  # type: ignore[assignment]
        return client


class LoopbackDirectoryClient:
    """DirectoryClient subset used by MarketTransport's public API."""

    def __init__(self, relay: LoopbackDirectory, transport: MarketTransport) -> None:
        self.relay = relay
        self.transport = transport
        self._active_peers: dict[str, str] = {}
        self._messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.public_messages: list[str] = []
        self.private_messages: list[tuple[str, str, str]] = []
        self.closed = False

    async def send_public_message(self, message: str) -> None:
        self.public_messages.append(message)
        for client in self.relay.clients.values():
            client._messages.put_nowait(
                {
                    "type": MessageType.PUBMSG.value,
                    "line": f"{self.transport.nick}!PUBLIC!{message}",
                }
            )

    async def send_private_message(self, recipient: str, command: str, data: str) -> None:
        self.private_messages.append((recipient, command, data))
        target = self.relay.clients[recipient]
        signed = self.transport.nick_identity.sign_message(data, ONION_HOSTID)
        target._messages.put_nowait(
            {
                "type": MessageType.PRIVMSG.value,
                "line": f"{self.transport.nick}!{recipient}!{command} {signed}",
            }
        )

    async def listen_for_messages(self, duration: float) -> list[dict[str, Any]]:
        try:
            first = await asyncio.wait_for(self._messages.get(), timeout=duration)
        except TimeoutError:
            return []
        messages = [first]
        while True:
            try:
                messages.append(self._messages.get_nowait())
            except asyncio.QueueEmpty:
                return messages

    async def close(self) -> None:
        self.closed = True


def _transport(
    *,
    key: bytes,
    encryption_key: PrivateKey | None = None,
    listing_callback: Callable[[], bytes] | None = None,
    responder: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    on_fault: Callable[[bytes], None] | None = None,
    listen_host: str | None = None,
    listen_port: int = 0,
) -> MarketTransport:
    return MarketTransport(
        directory_servers=[],
        network="regtest",
        nick_identity=NickIdentity(private_key_bytes=key),
        encryption_private_key=encryption_key,
        listing_callback=listing_callback,
        responder=responder,
        on_fault=on_fault,
        direct_location="127.0.0.1:5222",
        listen_host=listen_host,
        listen_port=listen_port,
        connection_timeout=0.5,
    )


def _sealed_request(provider_key: PrivateKey, reply_key: PrivateKey, request_id: str) -> str:
    request = canonical(
        {
            "request_id": request_id,
            "reply_pubkey": base64.b64encode(bytes(reply_key.public_key)).decode("ascii"),
            "body": {"request": "value"},
        }
    )
    return base64.b64encode(bytes(SealedBox(provider_key.public_key).encrypt(request))).decode(
        "ascii"
    )


def test_production_direct_location_rejects_clearnet() -> None:
    with pytest.raises(ValueError, match="onion service"):
        MarketTransport(
            directory_servers=[],
            network="mainnet",
            nick_identity=NickIdentity(private_key_bytes=b"\x0e" * 32),
            direct_location="127.0.0.1:5222",
        )


@pytest.mark.asyncio
async def test_discover_returns_signed_canonical_listing() -> None:
    buyer = _transport(key=b"\x01" * 32)
    seller = _transport(
        key=b"\x02" * 32,
        listing_callback=lambda: b'{"kind":"listing","price":1}',
    )
    relay = LoopbackDirectory()
    try:
        await buyer.start()
        await seller.start()
        buyer_client = relay.attach(buyer)
        relay.attach(seller)

        listings = await buyer.discover(timeout=0.5)

        assert listings == [(seller.nick, b'{"kind":"listing","price":1}')]
        assert buyer_client.public_messages == ["mbook"]
    finally:
        await buyer.close()
        await seller.close()


@pytest.mark.asyncio
async def test_request_prefers_actual_loopback_direct_peer() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def responder(sender: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((sender, body))
        return {"accepted": True}

    seller_key = PrivateKey.generate()
    seller = _transport(
        key=b"\x03" * 32,
        encryption_key=seller_key,
        responder=responder,
        listen_host="127.0.0.1",
    )
    buyer = _transport(key=b"\x04" * 32)
    relay = LoopbackDirectory()
    try:
        await seller.start()
        await buyer.start()
        buyer_client = relay.attach(buyer)
        relay.attach(seller)
        assert seller.hidden_service_listener is not None
        buyer_client._active_peers[seller.nick] = (
            f"127.0.0.1:{seller.hidden_service_listener.bound_port}"
        )

        result = await buyer.request(
            seller.nick,
            bytes(seller_key.public_key),
            {"secret": "never-relayed"},
            timeout=0.8,
        )

        assert result == {"accepted": True}
        assert calls == [(buyer.nick, {"secret": "never-relayed"})]
        assert buyer_client.private_messages == []
    finally:
        await buyer.close()
        await seller.close()


@pytest.mark.asyncio
async def test_request_falls_back_to_one_encrypted_directory_relay() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def responder(sender: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((sender, body))
        return {"accepted": True}

    seller_key = PrivateKey.generate()
    buyer = _transport(key=b"\x05" * 32)
    seller = _transport(key=b"\x06" * 32, encryption_key=seller_key, responder=responder)
    relay = LoopbackDirectory()
    try:
        await buyer.start()
        await seller.start()
        buyer_client = relay.attach(buyer)
        relay.attach(seller)

        result = await buyer.request(
            seller.nick,
            bytes(seller_key.public_key),
            {"secret": "not public"},
            timeout=0.5,
        )

        assert result == {"accepted": True}
        assert calls == [(buyer.nick, {"secret": "not public"})]
        assert len(buyer_client.private_messages) == 1
        recipient, command, encrypted = buyer_client.private_messages[0]
        assert (recipient, command) == (seller.nick, "mrequest")
        assert "not public" not in encrypted
    finally:
        await buyer.close()
        await seller.close()


@pytest.mark.asyncio
async def test_duplicate_request_uses_cached_response_across_direct_and_relay() -> None:
    provider_key = PrivateKey.generate()
    calls: list[dict[str, Any]] = []

    def responder(_sender: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append(body)
        return {"response": "once"}

    provider = _transport(key=b"\x07" * 32, encryption_key=provider_key, responder=responder)
    requester = NickIdentity(private_key_bytes=b"\x08" * 32)
    reply_key = PrivateKey.generate()
    request_id = "a" * 32
    data = _sealed_request(provider_key, reply_key, request_id)
    rest = f"mrequest {requester.sign_message(data, ONION_HOSTID)}"
    connection = MagicMock(spec=TCPConnection)
    connection.is_connected.return_value = True
    connection.send = AsyncMock()
    directory = MagicMock(spec=DirectoryClient)
    directory.send_private_message = AsyncMock()
    try:
        await provider._handle_private(requester.nick, rest, _Inbound(0, "", connection=connection))
        await provider._handle_private(requester.nick, rest, _Inbound(0, "", directory=directory))

        assert calls == [{"request": "value"}]
        connection.send.assert_awaited_once()
        directory.send_private_message.assert_awaited_once()
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_tampered_wrong_peer_and_replayed_responses_are_ignored() -> None:
    buyer = _transport(key=b"\x09" * 32)
    seller = NickIdentity(private_key_bytes=b"\x0a" * 32)
    attacker = NickIdentity(private_key_bytes=b"\x0b" * 32)
    unrelated_reply_key = PrivateKey.generate()
    unrelated_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    unrelated_id = "c" * 32
    reply_key = PrivateKey.generate()
    request_id = "b" * 32
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    buyer._pending[unrelated_id] = _PendingRequest(
        seller.nick, unrelated_reply_key, unrelated_future
    )
    buyer._pending[request_id] = _PendingRequest(seller.nick, reply_key, future)
    plaintext = canonical({"request_id": request_id, "body": {"ok": True}})
    encrypted = base64.b64encode(
        bytes(SealedBox(PublicKey(bytes(reply_key.public_key))).encrypt(plaintext))
    ).decode("ascii")
    valid_rest = f"mresponse {seller.sign_message(encrypted, ONION_HOSTID)}"
    try:
        wrong_peer = f"mresponse {attacker.sign_message(encrypted, ONION_HOSTID)}"
        await buyer._handle_private(attacker.nick, wrong_peer, _Inbound(0, ""))
        await buyer._handle_private(
            seller.nick,
            f"mresponse {seller.sign_message('not-base64', ONION_HOSTID)}",
            _Inbound(0, ""),
        )
        assert not future.done()

        await buyer._handle_private(seller.nick, valid_rest, _Inbound(0, ""))
        assert future.result() == {"ok": True}
        assert not unrelated_future.done()
        await buyer._handle_private(seller.nick, valid_rest, _Inbound(0, ""))
        assert future.result() == {"ok": True}
    finally:
        buyer._pending.pop(unrelated_id, None)
        unrelated_future.cancel()
        await buyer.close()


@pytest.mark.asyncio
async def test_timeout_bounds_fault_delivery_and_cleanup() -> None:
    faults: list[bytes] = []
    buyer = _transport(key=b"\x0c" * 32, on_fault=faults.append)
    seller = _transport(key=b"\x0d" * 32, encryption_key=PrivateKey.generate())
    relay = LoopbackDirectory()
    try:
        await buyer.start()
        buyer_client = relay.attach(buyer)
        relay.attach(seller)

        with pytest.raises(MarketRequestTimeoutError):
            await buyer.request(
                seller.nick,
                bytes(seller.encryption_private_key.public_key),
                {"request": "timeout"},
                timeout=0.1,
            )
        assert len(buyer_client.private_messages) == 1

        proof = b'{"kind":"fault","version":1}'
        await buyer._handle_public(
            seller.nick,
            f"mproof {base64.b64encode(proof).decode('ascii')}",
            None,
        )
        assert faults == [proof]

        with pytest.raises(MarketError):
            buyer._canonical_document(
                b'{"listing":"' + b"x" * MAX_LISTING_BYTES + b'"}',
                limit=MAX_LISTING_BYTES,
            )
        for _ in range(MAX_IN_FLIGHT + 3):
            buyer._enqueue(_Inbound(MessageType.PUBMSG.value, f"{buyer.nick}!PUBLIC!mbook"))
        assert buyer._inbound.qsize() <= MAX_IN_FLIGHT
    finally:
        await buyer.close()
        await seller.close()

    assert buyer.clients == {}
    assert buyer_client.closed is True
