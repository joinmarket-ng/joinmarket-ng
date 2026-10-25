"""Bounds on the taker's direct-peer message queue.

Direct peers feed ``_direct_message_queue`` from their receive loop, but the
queue is only drained inside ``wait_for_responses``. A connected maker must not
be able to grow taker memory without limit while the taker is outside a wait.
"""

from __future__ import annotations

import json

import pytest
from _taker_test_helpers import make_directory_client
from jmcore.crypto import NickIdentity
from jmcore.directory_client import MAX_BUFFERED_MESSAGE_BYTES, MAX_BUFFERED_MESSAGES
from jmcore.network import ONION_HOSTID
from jmcore.protocol import MessageType


def peer_message(line: str) -> bytes:
    return json.dumps({"type": MessageType.PRIVMSG.value, "line": line}).encode("utf-8")


@pytest.mark.asyncio
async def test_direct_message_count_is_bounded() -> None:
    client = make_directory_client()
    data = peer_message("J5attacker!J5taker!pubkey x")

    for _ in range(MAX_BUFFERED_MESSAGES + 500):
        await client._on_peer_message("J5attacker", data)

    assert client._direct_message_queue.qsize() == MAX_BUFFERED_MESSAGES


@pytest.mark.asyncio
async def test_direct_message_bytes_are_bounded() -> None:
    client = make_directory_client()
    data = peer_message("J5attacker!J5taker!pubkey " + "A" * (1024 * 1024))

    for _ in range(20):
        await client._on_peer_message("J5attacker", data)

    queued = client._direct_message_queue.qsize()
    assert 0 < queued < 20
    assert queued * len(data) <= MAX_BUFFERED_MESSAGE_BYTES


@pytest.mark.asyncio
async def test_direct_messages_accepted_again_after_drain() -> None:
    client = make_directory_client()
    maker = NickIdentity(5)
    flood = peer_message("J5attacker!J5taker!pubkey x")
    for _ in range(MAX_BUFFERED_MESSAGES + 10):
        await client._on_peer_message("J5attacker", flood)

    client.clients = {}
    await client.wait_for_responses(
        expected_nicks=[maker.nick], expected_command="!pubkey", timeout=0.1
    )
    assert client._direct_message_queue.qsize() == 0

    signed = maker.sign_message("MAKER_NACL", ONION_HOSTID)
    line = f"{maker.nick}!{client.nick_identity.nick}!pubkey {signed}"
    await client._on_peer_message(maker.nick, peer_message(line))
    responses = await client.wait_for_responses(
        expected_nicks=[maker.nick], expected_command="!pubkey", timeout=1.0
    )
    assert responses[maker.nick]["data"].split()[0] == "MAKER_NACL"
