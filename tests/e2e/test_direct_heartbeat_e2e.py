"""Live direct heartbeat coverage through the ring maker's onion service."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Coroutine
from typing import Any, cast

import pytest
import jmcore.network as network
from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient, DirectoryClientError
from jmcore.network import OnionPeer, TCPConnection
from jmcore.protocol import (
    FEATURE_DIRECT_PING_V1,
    FEATURE_PING,
    FEATURE_PRIVATE_CHANNEL_RING,
    JM_VERSION,
    MessageType,
)

pytestmark = [pytest.mark.docker, pytest.mark.ring_e2e]

DIRECTORY_PORT = int(os.getenv("RING_E2E_DIRECTORY_PORT", "25222"))
TOR_SOCKS_PORT = int(os.getenv("RING_E2E_TOR_SOCKS_PORT", "29050"))
# A fresh onion service on the live Tor network can take minutes to become
# reachable; 50 seconds failed in a suite whose maker had just started.
ONION_READY_TIMEOUT = 180.0
DISCOVERY_TIMEOUT = 120.0
CONNECTION_TIMEOUT = 30.0
HEARTBEAT_TIMEOUT = 20.0
OFFER_TIMEOUT = 30.0
RETRY_INTERVAL = 2.0


def _message(data: bytes) -> dict[str, object] | None:
    try:
        decoded = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


class _ObservedConnection:
    """Delegate a real transport while recording heartbeat envelopes only."""

    def __init__(self, connection: TCPConnection) -> None:
        self._connection = connection
        self.ping_nonces: list[str] = []
        self.pong_nonces: list[str] = []

    async def send(self, data: bytes) -> None:
        envelope = _message(data)
        nonce = envelope.get("line") if envelope is not None else None
        if envelope is not None and envelope.get("type") == MessageType.PING.value:
            if isinstance(nonce, str):
                self.ping_nonces.append(nonce)
        await self._connection.send(data)

    async def receive(self) -> bytes:
        data = await self._connection.receive()
        envelope = _message(data)
        nonce = envelope.get("line") if envelope is not None else None
        if envelope is not None and envelope.get("type") == MessageType.PONG.value:
            if isinstance(nonce, str):
                self.pong_nonces.append(nonce)
        return data

    async def close(self) -> None:
        await self._connection.close()

    def abort(self) -> None:
        self._connection.abort()

    def is_connected(self) -> bool:
        return self._connection.is_connected()


async def _maker4_endpoint() -> tuple[str, str]:
    """Poll the directory until its non-ring maker advertises a direct onion endpoint."""
    deadline = asyncio.get_running_loop().time() + DISCOVERY_TIMEOUT
    last_peer_count = 0
    last_non_ring_ping_count = 0
    while True:
        directory = DirectoryClient(
            host="127.0.0.1",
            port=DIRECTORY_PORT,
            network="regtest",
            timeout=CONNECTION_TIMEOUT,
            peerlist_timeout=15.0,
            allow_clearnet_connections=True,
        )
        try:
            await directory.connect()
            peerlist = await directory.get_peerlist_with_features()
            last_peer_count = len(peerlist)
            candidates = [
                (nick, location)
                for nick, location, features in peerlist
                if _is_onion_endpoint(location)
                and features.supports(FEATURE_PING)
                and not features.supports(FEATURE_PRIVATE_CHANNEL_RING)
            ]
            last_non_ring_ping_count = sum(
                features.supports(FEATURE_PING)
                and not features.supports(FEATURE_PRIVATE_CHANNEL_RING)
                for _, _, features in peerlist
            )
        except DirectoryClientError:
            candidates = []
        finally:
            await directory.close()

        if len(candidates) == 1:
            return candidates[0]
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError(
                "maker4 direct onion endpoint did not become ready "
                f"(peers={last_peer_count}, non_ring_ping_peers={last_non_ring_ping_count}, "
                f"onion_candidates={len(candidates)})"
            )
        await asyncio.sleep(2)


def _is_onion_endpoint(location: str) -> bool:
    host, separator, port = location.rpartition(":")
    return separator == ":" and host.endswith(".onion") and port == "5222"


async def _connect_to_maker(
    maker_nick: str,
    location: str,
    identity: NickIdentity,
    observed_connections: list[_ObservedConnection],
    dial_exception_types: list[str],
    on_message: Callable[[str, bytes], Coroutine[Any, Any, None]],
) -> tuple[OnionPeer, _ObservedConnection]:
    """Retry real Tor dials until the advertised hidden service accepts one."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ONION_READY_TIMEOUT
    attempts = 0
    while loop.time() < deadline:
        before = len(observed_connections)
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        attempts += 1
        peer = OnionPeer(
            nick=maker_nick,
            location=location,
            socks_port=TOR_SOCKS_PORT,
            timeout=min(CONNECTION_TIMEOUT, remaining),
            nick_identity=identity,
            on_message=on_message,
        )
        if await peer.connect(identity.nick, "NOT-SERVING-ONION", "regtest"):
            if len(observed_connections) != before + 1:
                await peer.disconnect()
                raise RuntimeError("direct heartbeat transport was not observed")
            return peer, observed_connections[-1]
        await peer.disconnect()
        remaining = deadline - loop.time()
        if remaining > 0:
            await asyncio.sleep(min(RETRY_INTERVAL, remaining))

    last_exception_type = dial_exception_types[-1] if dial_exception_types else "none"
    raise RuntimeError(
        "maker4 direct onion service did not accept a connection "
        f"(attempts={attempts}, last_exception={last_exception_type})"
    )


async def _two_matched_heartbeats(connection: _ObservedConnection) -> tuple[str, str]:
    deadline = asyncio.get_running_loop().time() + HEARTBEAT_TIMEOUT
    while True:
        matched = [
            nonce for nonce in connection.ping_nonces if nonce in connection.pong_nonces
        ]
        unique = tuple(dict.fromkeys(matched))
        if len(unique) >= 2:
            return unique[0], unique[1]
        if asyncio.get_running_loop().time() >= deadline:
            raise RuntimeError("did not observe two matching direct heartbeat PONGs")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.timeout(420)
async def test_direct_ping_v1_keeps_maker4_onion_connection_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.getenv("RING_E2E") != "1":
        pytest.skip("start the ring-e2e Compose profile and set RING_E2E=1")

    monkeypatch.setattr(network, "_DIRECT_PING_INTERVAL_SEC", 0.4)
    original_connect_via_tor = network.connect_via_tor
    observed_connections: list[_ObservedConnection] = []
    dial_exception_types: list[str] = []

    async def observing_connect_via_tor(
        onion_address: str,
        port: int,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9050,
        max_message_size: int = 2097152,
        timeout: float = 120.0,
        socks_username: str | None = None,
        socks_password: str | None = None,
    ) -> TCPConnection:
        try:
            connection = await original_connect_via_tor(
                onion_address,
                port,
                socks_host,
                socks_port,
                max_message_size,
                timeout,
                socks_username,
                socks_password,
            )
        except Exception as error:
            dial_exception_types.append(type(error).__name__)
            raise
        observed = _ObservedConnection(connection)
        observed_connections.append(observed)
        return cast(TCPConnection, observed)

    monkeypatch.setattr(network, "connect_via_tor", observing_connect_via_tor)

    identity = NickIdentity(JM_VERSION)
    offer_received = asyncio.Event()
    maker_nick, location = await _maker4_endpoint()

    async def on_message(_nick: str, data: bytes) -> None:
        envelope = _message(data)
        if envelope is None or envelope.get("type") != MessageType.PRIVMSG.value:
            return
        line = envelope.get("line")
        if not isinstance(line, str):
            return
        parts = line.split("!", 2)
        if len(parts) != 3:
            return
        sender, recipient, command = parts
        if (
            sender == maker_nick
            and recipient == identity.nick
            and command.split(" ", 1)[0].endswith("offer")
        ):
            offer_received.set()

    peer, connection = await _connect_to_maker(
        maker_nick,
        location,
        identity,
        observed_connections,
        dial_exception_types,
        on_message,
    )
    try:
        assert peer.supports_feature(FEATURE_DIRECT_PING_V1) is True
        assert await peer.send_privmsg(identity.nick, "direct-heartbeat-e2e", "ready")

        first_nonce, second_nonce = await _two_matched_heartbeats(connection)
        assert first_nonce != second_nonce
        assert first_nonce in connection.pong_nonces
        assert second_nonce in connection.pong_nonces
        assert peer.is_connected()

        orderbook_request = {
            "type": MessageType.PUBMSG.value,
            "line": f"{identity.nick}!PUBLIC!orderbook",
        }
        assert await peer.send(json.dumps(orderbook_request).encode("utf-8"))
        await asyncio.wait_for(offer_received.wait(), timeout=OFFER_TIMEOUT)
        assert peer.is_connected()
    finally:
        await peer.disconnect()
