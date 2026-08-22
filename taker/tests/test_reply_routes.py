"""Exact authenticated reply routing for taker private messages."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_directory_client
from jmcore.crypto import NickIdentity
from jmcore.network import ONION_HOSTID, PeerConnectionBinding

from taker.multi_directory import ReplyRoute


def _signed_line(sender: NickIdentity, recipient: str, data: str) -> str:
    signed = sender.sign_message(data, ONION_HOSTID)
    return f"{sender.nick}!{recipient}!channel_input {signed}"


@pytest.mark.asyncio
async def test_direct_response_retains_exact_ingress_binding() -> None:
    client = make_directory_client()
    maker = NickIdentity(5)
    binding = Mock(spec=PeerConnectionBinding)
    binding.send_privmsg = AsyncMock(return_value=True)
    ingress_peer = Mock()
    ingress_peer.bind_current_connection.return_value = binding
    line = _signed_line(maker, client.nick, "encrypted-payload")

    await client._on_peer_message(
        ingress_peer,
        maker.nick,
        json.dumps({"line": line}).encode(),
    )
    client.clients = {}
    responses = await client.wait_for_responses(
        [maker.nick],
        "!channel_input",
        timeout=1.0,
        include_routes=True,
    )

    route = responses[maker.nick]["route"]
    assert isinstance(route, ReplyRoute)
    assert route.is_direct
    await client.send_privmsg_via_route(maker.nick, "channel_input", "response", route)
    binding.send_privmsg.assert_awaited_once_with(client.nick, "channel_input", "response")


@pytest.mark.asyncio
async def test_directory_response_uses_exact_ingress_directory() -> None:
    client = make_directory_client()
    maker = NickIdentity(5)
    source = AsyncMock()
    other = AsyncMock()
    source._active_peers = {}
    other._active_peers = {}
    client.clients = {"source:5222": source, "other:5222": other}

    route = ReplyRoute(nick=maker.nick, channel_id="directory:source:5222")
    used = await client.send_privmsg_via_route(
        maker.nick,
        "channel_input",
        "response",
        route,
    )

    assert used == "directory:source:5222"
    source.send_private_message.assert_awaited_once_with(
        maker.nick,
        "channel_input",
        "response",
    )
    other.send_private_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_route_rejects_different_recipient() -> None:
    client = make_directory_client()
    route = ReplyRoute(nick="J5expected", channel_id="directory:source:5222")

    with pytest.raises(ValueError, match="bound to J5expected"):
        await client.send_privmsg_via_route(
            "J5substituted",
            "channel_input",
            "response",
            route,
        )
