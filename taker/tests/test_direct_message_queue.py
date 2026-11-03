"""Bounds on the taker's direct-peer message queue.

Direct peers feed ``_direct_message_queue`` from their receive loop, but the
queue is only drained inside ``wait_for_responses``. A connected maker must not
be able to grow taker memory without limit while the taker is outside a wait.
"""

from __future__ import annotations

import json

import pytest
from _taker_test_helpers import make_directory_client, register_direct_peer
from jmcore.crypto import NickIdentity
from jmcore.directory_client import MAX_BUFFERED_MESSAGE_BYTES, MAX_BUFFERED_MESSAGES
from jmcore.network import ONION_HOSTID
from jmcore.protocol import MessageType

from taker.multi_directory import DIRECT_PEER_MAX_BYTES, DIRECT_PEER_MAX_MESSAGES


def peer_message(line: str) -> bytes:
    return json.dumps({"type": MessageType.PRIVMSG.value, "line": line}).encode("utf-8")


@pytest.mark.asyncio
async def test_direct_message_count_is_bounded() -> None:
    client = make_directory_client()
    register_direct_peer(client, "J5attacker")
    data = peer_message("J5attacker!J5taker!pubkey x")

    for _ in range(MAX_BUFFERED_MESSAGES + 500):
        await client._on_peer_message("J5attacker", data)

    assert client._direct_message_queue.qsize() == MAX_BUFFERED_MESSAGES


@pytest.mark.asyncio
async def test_direct_message_bytes_are_bounded() -> None:
    client = make_directory_client()
    register_direct_peer(client, "J5attacker")
    data = peer_message("J5attacker!J5taker!pubkey " + "A" * (1024 * 1024))

    for _ in range(20):
        await client._on_peer_message("J5attacker", data)

    queued = client._direct_message_queue.qsize()
    assert 0 < queued < 20
    assert queued * len(data) <= MAX_BUFFERED_MESSAGE_BYTES


@pytest.mark.asyncio
async def test_direct_messages_accepted_again_after_drain() -> None:
    client = make_directory_client()
    register_direct_peer(client, "J5attacker")
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
    register_direct_peer(client, maker.nick)
    await client._on_peer_message(maker.nick, peer_message(line))
    responses = await client.wait_for_responses(
        expected_nicks=[maker.nick], expected_command="!pubkey", timeout=1.0
    )
    assert responses[maker.nick]["data"].split()[0] == "MAKER_NACL"


@pytest.mark.asyncio
async def test_flooded_connection_cannot_charge_claimed_sender_or_exclude_healthy_reply() -> None:
    client = make_directory_client()
    maker = NickIdentity(5)
    register_direct_peer(client, "assigned-attacker")
    register_direct_peer(client, maker.nick)
    spoofed = json.dumps(
        {
            "type": MessageType.PRIVMSG.value,
            "line": f"{maker.nick}!{client.nick}!pubkey invalid-signature",
            "from_nick": maker.nick,
        }
    ).encode()
    for _ in range(DIRECT_PEER_MAX_MESSAGES + 10):
        await client._on_peer_message("assigned-attacker", spoofed)
    assert set(client._direct_message_usage) == {"assigned-attacker"}
    assert client._direct_message_usage["assigned-attacker"][0] == DIRECT_PEER_MAX_MESSAGES

    signed = maker.sign_message("MAKER_NACL", ONION_HOSTID)
    await client._on_peer_message(
        maker.nick, peer_message(f"{maker.nick}!{client.nick}!pubkey {signed}")
    )
    assert client._direct_message_queue.qsize() == DIRECT_PEER_MAX_MESSAGES + 1
    responses = await client.wait_for_responses([maker.nick], "!pubkey", timeout=1)
    assert responses[maker.nick]["data"].split()[0] == "MAKER_NACL"
    assert client._direct_message_usage == {}
    assert client._direct_message_queue_bytes == 0


@pytest.mark.asyncio
async def test_byte_quota_reserves_capacity_for_another_connection() -> None:
    client = make_directory_client()
    for nick in ("attacker", "healthy"):
        register_direct_peer(client, nick)
    large = peer_message("attacker!taker!pubkey " + "A" * (1024 * 1024))
    for _ in range(20):
        await client._on_peer_message("attacker", large)
    count, charged = client._direct_message_usage["attacker"]
    assert count > 0
    assert charged <= DIRECT_PEER_MAX_BYTES
    assert charged + len(large) > DIRECT_PEER_MAX_BYTES
    await client._on_peer_message("healthy", large)
    assert client._direct_message_usage["healthy"][0] == 1
    assert client._direct_message_usage["attacker"] == (count, charged)


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["count", "bytes"])
async def test_aggregate_limits_and_dequeue_keep_exact_owner_accounting(
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    if limit == "count":
        monkeypatch.setattr("taker.multi_directory.DIRECT_PEER_MAX_MESSAGES", 2)
        monkeypatch.setattr("taker.multi_directory.DIRECT_QUEUE_MAX_MESSAGES", 4)
    client = make_directory_client()
    data = peer_message("pubkey " + "x" * 100)
    charged_message = json.loads(data) | {"from_nick": "A", "from_direct": True}
    charge = client._direct_message_size(charged_message)
    if limit == "bytes":
        monkeypatch.setattr("taker.multi_directory.DIRECT_PEER_MAX_BYTES", 2 * charge)
        monkeypatch.setattr("taker.multi_directory.DIRECT_QUEUE_MAX_BYTES", 4 * charge)
    for nick in ("A", "B", "C"):
        register_direct_peer(client, nick)
    for nick in ("A", "A", "B", "B", "C"):
        await client._on_peer_message(nick, data)
    assert client._direct_message_queue.qsize() == 4
    assert client._direct_message_queue_bytes == 4 * charge
    assert set(client._direct_message_usage) == {"A", "B"}
    client._dequeue_direct_message()
    await client._on_peer_message("C", data)
    assert {nick: usage[0] for nick, usage in client._direct_message_usage.items()} == {
        "A": 1,
        "B": 2,
        "C": 1,
    }
    assert client._direct_message_queue_bytes == 4 * charge
    while not client._direct_message_queue.empty():
        client._dequeue_direct_message()
    assert client._direct_message_queue_bytes == 0
    assert client._direct_message_usage == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [40, DIRECT_PEER_MAX_MESSAGES])
async def test_early_high_input_signature_burst_survives_disconnect_and_keeps_quota(
    count: int,
) -> None:
    client = make_directory_client()
    maker = NickIdentity(5)
    register_direct_peer(client, maker.nick)
    for index in range(count):
        signed = maker.sign_message(f"signature-{index}", ONION_HOSTID)
        await client._on_peer_message(
            maker.nick, peer_message(f"{maker.nick}!{client.nick}!sig {signed}")
        )
    before_disconnect = dict(client._direct_message_usage)
    await client._on_peer_disconnect(maker.nick)
    assert client._direct_message_usage == before_disconnect
    assert client._direct_message_queue.qsize() == count
    if count == DIRECT_PEER_MAX_MESSAGES:
        await client._on_peer_message(maker.nick, peer_message("extra reply"))
        assert client._direct_message_queue.qsize() == count
    responses = await client.wait_for_responses(
        [maker.nick],
        "!sig",
        timeout=2,
        expected_counts={maker.nick: count},
    )
    assert len(responses[maker.nick]["data"]) == count
    assert client._direct_message_usage == {}
    assert client._direct_message_queue_bytes == 0


@pytest.mark.asyncio
async def test_cleanup_resets_queue_accounting_and_rejects_late_callbacks() -> None:
    client = make_directory_client()
    for nick in ("one", "two"):
        register_direct_peer(client, nick)
        await client._on_peer_message(nick, peer_message("early message"))
    assert client._direct_message_queue.qsize() == 2
    await client._cleanup_peer_connections()
    assert client._direct_message_queue.empty()
    assert client._direct_message_queue_bytes == 0
    assert client._direct_message_usage == {}
    await client._on_peer_message("one", peer_message("late message"))
    assert client._direct_message_queue.empty()
    assert client._direct_message_usage == {}
