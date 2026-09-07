"""Resource limit regressions for untrusted directory client state."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from jmcore.directory_client import DirectoryClient, DirectoryClientError
from jmcore.models import Offer, OfferType
from jmcore.protocol import FEATURE_NEUTRINO_COMPAT, MessageType

VALID_MAKER_NICKS = ("J57wPBk1VfjSP5Te", "J57wPBk1VfjSP5Tf")


def _offer(nick: str, oid: int, txfee: int = 500) -> Offer:
    return Offer(
        counterparty=nick,
        oid=oid,
        ordertype=OfferType.SW0_RELATIVE,
        minsize=750_000,
        maxsize=790_107_726_787,
        txfee=txfee,
        cjfee="0.001",
    )


def _message() -> dict[str, Any]:
    return {"type": MessageType.PUBMSG.value, "line": "maker!PUBLIC!not-an-offer"}


@pytest.mark.asyncio
async def test_peerlist_message_buffer_limit_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected messages cannot make a peerlist fetch retain an unbounded queue."""
    monkeypatch.setattr("jmcore.directory_client.MAX_BUFFERED_MESSAGES", 1)
    connection = AsyncMock()
    connection.receive.side_effect = [
        json.dumps(_message()).encode("utf-8"),
        json.dumps(_message()).encode("utf-8"),
    ]
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = connection

    with pytest.raises(DirectoryClientError, match="message buffer limit"):
        await client.get_peerlist_with_features()

    connection.close.assert_awaited_once()
    assert client.connection is None


@pytest.mark.asyncio
async def test_peerlist_message_buffer_byte_limit_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buffered JSON accounting includes the serialized UTF-8 message size."""
    message = _message()
    monkeypatch.setattr(
        "jmcore.directory_client.MAX_BUFFERED_MESSAGE_BYTES",
        len(json.dumps(message).encode("utf-8")) - 1,
    )
    connection = AsyncMock()
    connection.receive.return_value = json.dumps(message).encode("utf-8")
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = connection

    with pytest.raises(DirectoryClientError, match="message buffer byte limit"):
        await client.get_peerlist_with_features()

    connection.close.assert_awaited_once()
    assert client.connection is None


@pytest.mark.asyncio
async def test_listen_message_byte_limit_closes_without_returning_partial_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """listen_for_messages fails closed instead of returning a truncated message list."""
    message = _message()
    monkeypatch.setattr(
        "jmcore.directory_client.MAX_COLLECTED_MESSAGE_BYTES",
        len(json.dumps(message).encode("utf-8")) - 1,
    )
    connection = AsyncMock()
    connection.is_connected = Mock(return_value=True)
    connection.receive.return_value = json.dumps(message).encode("utf-8")
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = connection

    with pytest.raises(DirectoryClientError, match="listen byte limit"):
        await client.listen_for_messages(duration=1.0)

    connection.close.assert_awaited_once()
    assert client.connection is None


@pytest.mark.asyncio
async def test_orderbook_fetch_limit_applies_across_listen_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An orderbook fetch counts messages returned by every listen chunk together."""
    monkeypatch.setattr("jmcore.directory_client.MAX_COLLECTED_MESSAGES", 2)
    connection = AsyncMock()
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = connection
    client.get_peerlist_with_features = AsyncMock(return_value=[])  # type: ignore[method-assign]
    listen_calls = 0

    async def listen_once(duration: float = 5.0) -> list[dict[str, Any]]:
        nonlocal listen_calls
        listen_calls += 1
        return [_message()]

    client.listen_for_messages = listen_once  # type: ignore[method-assign]

    with pytest.raises(DirectoryClientError, match="orderbook fetch message limit"):
        await client.fetch_orderbooks(max_wait=30.0, min_wait=0.0, quiet_period=0.0)

    assert listen_calls == 3
    connection.close.assert_awaited_once()
    assert client.connection is None


@pytest.mark.asyncio
async def test_listener_peerlist_sink_limit_wakes_stalled_fetcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full sink fails closed and leaves a wake marker for its sole reader."""
    monkeypatch.setattr("jmcore.directory_client.MAX_INFLIGHT_PEERLIST_CHUNKS", 1)
    on_disconnect = Mock()
    client = DirectoryClient("host", 1234, "mainnet", on_disconnect=on_disconnect)
    connection = AsyncMock()
    connection.receive.side_effect = [
        json.dumps({"type": MessageType.PEERLIST.value, "line": "peer1;loc1.onion:5222"}).encode(
            "utf-8"
        ),
        json.dumps({"type": MessageType.PEERLIST.value, "line": "peer2;loc2.onion:5222"}).encode(
            "utf-8"
        ),
    ]
    client.connection = connection
    client.get_peerlist_with_features = AsyncMock(return_value=[])  # type: ignore[method-assign]
    client._peerlist_inflight = asyncio.Queue(maxsize=1)

    with pytest.raises(DirectoryClientError, match="peerlist in-flight queue limit"):
        await asyncio.wait_for(client.listen_continuously(request_orderbook=False), timeout=1.0)

    assert client._listen_loop_active is False
    assert client.running is False
    assert client._peerlist_inflight is not None
    assert client._peerlist_inflight.get_nowait() is None
    connection.close.assert_awaited_once()
    assert client.connection is None
    on_disconnect.assert_called_once_with()
    client._notify_disconnect()
    on_disconnect.assert_called_once_with()


def test_retained_state_limits_preserve_existing_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing offers, peers, and features remain updateable at their fixed caps."""
    monkeypatch.setattr("jmcore.directory_client.MAX_RETAINED_OFFERS", 1)
    monkeypatch.setattr("jmcore.directory_client.MAX_RETAINED_PEERS", 1)
    monkeypatch.setattr("jmcore.directory_client.MAX_PEER_FEATURES", 1)
    client = DirectoryClient("host", 1234, "mainnet")
    maker, other_maker = VALID_MAKER_NICKS

    client._merge_peer_features(maker, {FEATURE_NEUTRINO_COMPAT: True})
    assert client._store_offer((maker, 0), _offer(maker, 0))
    assert client._store_offer((maker, 0), _offer(maker, 0, txfee=600))
    client._handle_peerlist_response(f"{maker};updated.onion:5222;F:{FEATURE_NEUTRINO_COMPAT}")

    assert len(client.offers) == 1
    assert client.offers[(maker, 0)].offer.txfee == 600
    assert client._active_peers == {maker: "updated.onion:5222"}
    assert client.peer_features == {maker: {FEATURE_NEUTRINO_COMPAT: True}}

    with pytest.raises(DirectoryClientError, match="offer cache limit"):
        client._store_offer((other_maker, 0), _offer(other_maker, 0))
    with pytest.raises(DirectoryClientError, match="peer feature cache limit"):
        client._merge_peer_features(other_maker, {})
    with pytest.raises(DirectoryClientError, match="peer feature limit"):
        client._merge_peer_features(maker, {"another-feature": True})

    assert set(client.offers) == {(maker, 0)}
    assert set(client.peer_features) == {maker}


def test_storage_field_byte_limits_reject_untrusted_values() -> None:
    """Storage checks backstop protocol parsing for untrusted directory fields."""
    client = DirectoryClient("host", 1234, "mainnet")

    with pytest.raises(DirectoryClientError, match="peer nick exceeds"):
        client._store_offer(("n" * 65, 0), _offer("n" * 65, 0))
    with pytest.raises(DirectoryClientError, match="peer location exceeds"):
        client._validate_peer_storage(VALID_MAKER_NICKS[0], "l" * 301)
    with pytest.raises(DirectoryClientError, match="feature identifier exceeds"):
        client._merge_peer_features(VALID_MAKER_NICKS[0], {"f" * 129: True})


@pytest.mark.asyncio
async def test_authoritative_peerlist_limit_never_returns_partial_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A peerlist that exceeds its retained peer limit is not authoritative."""
    monkeypatch.setattr("jmcore.directory_client.MAX_RETAINED_PEERS", 1)
    connection = AsyncMock()
    connection.receive.side_effect = [
        json.dumps({"type": MessageType.PEERLIST.value, "line": "peer1;loc1.onion:5222"}).encode(
            "utf-8"
        ),
        json.dumps({"type": MessageType.PEERLIST.value, "line": "peer2;loc2.onion:5222"}).encode(
            "utf-8"
        ),
    ]
    client = DirectoryClient("host", 1234, "mainnet")
    client.connection = connection

    with pytest.raises(DirectoryClientError, match="active peer limit"):
        await client.get_authoritative_peerlist_snapshot()

    connection.close.assert_awaited_once()
    assert client.connection is None
    assert client.get_active_nicks() == {"peer1"}
