"""Authentication of relayed maker responses in wait_for_responses.

A malicious peer must not be able to have its response attributed to another
maker by embedding that maker's nick in its payload, nor to impersonate a maker
without its signing key.
"""

from __future__ import annotations

import asyncio

import pytest
from _taker_test_helpers import make_directory_client
from jmcore.crypto import NickIdentity
from jmcore.network import ONION_HOSTID
from jmcore.protocol import MessageType


def signed_line(identity: NickIdentity, recipient: str, command: str, data: str) -> str:
    """Build a directory-relayed, signed JM privmsg line."""
    return f"{identity.nick}!{recipient}!{command} {identity.sign_message(data, ONION_HOSTID)}"


async def run(client, expected_nicks, command, lines, counts=None):
    for line in lines:
        await client._direct_message_queue.put(
            {"type": MessageType.PRIVMSG.value, "line": line, "source": "dir1"}
        )
    client.clients = {}
    return await client.wait_for_responses(
        expected_nicks=expected_nicks,
        expected_command=command,
        timeout=1.0,
        expected_counts=counts,
    )


@pytest.mark.asyncio
async def test_legit_signed_response_accepted():
    client = make_directory_client()
    maker = NickIdentity(5)
    line = signed_line(maker, client.nick_identity.nick, "pubkey", "MAKER_NACL features=ping")
    responses = await run(client, [maker.nick], "!pubkey", [line])
    assert maker.nick in responses
    assert responses[maker.nick]["data"].split()[0] == "MAKER_NACL"


@pytest.mark.asyncio
async def test_embedded_victim_nick_not_attributed_to_victim():
    client = make_directory_client()
    victim = NickIdentity(5)
    attacker = NickIdentity(5)
    # Attacker sends its own validly-signed !pubkey but embeds the victim nick.
    data = f"ATTACKER_NACL {victim.nick}"
    line = signed_line(attacker, client.nick_identity.nick, "pubkey", data)
    responses = await run(client, [victim.nick, attacker.nick], "!pubkey", [line])
    assert responses[attacker.nick]["data"].split()[0] == "ATTACKER_NACL"
    # The honest maker's session must remain unset, never keyed to the attacker.
    assert victim.nick not in responses


@pytest.mark.asyncio
async def test_forged_from_nick_rejected():
    client = make_directory_client()
    victim = NickIdentity(5)
    attacker = NickIdentity(5)
    # Attacker claims the victim's nick in the envelope but signs with its own key.
    signed = attacker.sign_message("ATTACKER_NACL", ONION_HOSTID)
    line = f"{victim.nick}!{client.nick_identity.nick}!pubkey {signed}"
    responses = await run(client, [victim.nick], "!pubkey", [line])
    assert victim.nick not in responses


@pytest.mark.asyncio
async def test_spoofed_error_does_not_abort_victim():
    client = make_directory_client()
    victim = NickIdentity(5)
    attacker = NickIdentity(5)
    # Valid message from attacker whose payload contains "!error" and victim nick.
    data = f"x !error blacklist {victim.nick}"
    line = signed_line(attacker, client.nick_identity.nick, "pubkey", data)
    responses = await run(client, [victim.nick, attacker.nick], "!pubkey", [line])
    assert not responses.get(victim.nick, {}).get("error")
    assert victim.nick not in responses


@pytest.mark.asyncio
async def test_tampered_payload_dropped():
    client = make_directory_client()
    maker = NickIdentity(5)
    signed = maker.sign_message("MAKER_NACL", ONION_HOSTID)
    tampered = "pubkey TAMPERED " + signed.split(" ", 1)[1]
    line = f"{maker.nick}!{client.nick_identity.nick}!{tampered}"
    responses = await run(client, [maker.nick], "!pubkey", [line])
    assert maker.nick not in responses


@pytest.mark.asyncio
async def test_public_message_from_expected_maker_ignored():
    client = make_directory_client()
    maker = NickIdentity(5)
    line = f"{maker.nick}!PUBLIC!sw0reloffer 0 100000 1000000 0 0.001"
    await client._direct_message_queue.put(
        {"type": MessageType.PUBMSG.value, "line": line, "source": "dir1"}
    )
    client.clients = {}

    responses = await client.wait_for_responses(
        expected_nicks=[maker.nick], expected_command="!pubkey", timeout=0.01
    )

    assert maker.nick not in responses


@pytest.mark.asyncio
async def test_response_addressed_to_another_taker_ignored():
    client = make_directory_client()
    maker = NickIdentity(5)
    other_taker = NickIdentity(5)
    line = signed_line(maker, other_taker.nick, "pubkey", "MAKER_NACL")

    responses = await run(client, [maker.nick], "!pubkey", [line])

    assert maker.nick not in responses


@pytest.mark.asyncio
async def test_genuine_error_from_maker_recorded():
    client = make_directory_client()
    maker = NickIdentity(5)
    line = signed_line(maker, client.nick_identity.nick, "error", "commitment-blacklisted")
    responses = await run(client, [maker.nick], "!pubkey", [line])
    assert responses[maker.nick]["error"] is True
    assert "commitment-blacklisted" in responses[maker.nick]["data"]


@pytest.mark.asyncio
async def test_signature_then_error_keeps_signature_response():
    client = make_directory_client()
    maker = NickIdentity(5)
    sig = signed_line(maker, client.nick_identity.nick, "sig", "invalid-signature")
    error = signed_line(maker, client.nick_identity.nick, "error", "declined")

    responses = await run(client, [maker.nick], "!sig", [sig, error], {maker.nick: 2})

    assert responses[maker.nick] == {"data": [sig.split("!sig ", 1)[1]]}


@pytest.mark.asyncio
async def test_error_then_signature_keeps_terminal_error():
    client = make_directory_client()
    maker = NickIdentity(5)
    error = signed_line(maker, client.nick_identity.nick, "error", "declined")
    sig = signed_line(maker, client.nick_identity.nick, "sig", "late-signature")

    responses = await run(client, [maker.nick], "!sig", [error, sig], {maker.nick: 2})

    assert responses[maker.nick]["error"] is True
    assert "declined" in responses[maker.nick]["data"]


@pytest.mark.asyncio
async def test_signature_error_completes_independent_of_message_length():
    client = make_directory_client()
    maker = NickIdentity(5)
    error = signed_line(maker, client.nick_identity.nick, "error", "x")

    responses = await asyncio.wait_for(
        run(client, [maker.nick], "!sig", [error], {maker.nick: 100}), timeout=0.2
    )

    assert responses[maker.nick]["error"] is True


@pytest.mark.asyncio
async def test_stall_callback_fires_once_for_silent_makers_and_keeps_partial_sigs():
    client = make_directory_client()
    me = client.nick_identity.nick
    partial, silent = NickIdentity(5), NickIdentity(5)
    await client._direct_message_queue.put(
        {"type": MessageType.PRIVMSG.value, "line": signed_line(partial, me, "sig", "s1")}
    )
    client.clients = {}
    stalled: list[set[str]] = []

    async def on_stalled(nicks: set[str]) -> None:
        stalled.append(nicks)
        for identity, data in ((partial, "s2"), (silent, "s3")):
            await client._direct_message_queue.put(
                {"type": MessageType.PRIVMSG.value, "line": signed_line(identity, me, "sig", data)}
            )

    responses = await client.wait_for_responses(
        expected_nicks=[partial.nick, silent.nick],
        expected_command="!sig",
        timeout=5.0,
        expected_counts={partial.nick: 2, silent.nick: 1},
        on_stalled=(0.5, on_stalled),
    )
    assert stalled == [{silent.nick}]
    assert [d.split()[0] for d in responses[partial.nick]["data"]] == ["s1", "s2"]
    assert [d.split()[0] for d in responses[silent.nick]["data"]] == ["s3"]


@pytest.mark.asyncio
async def test_directory_channel_skips_connected_direct_peer():
    from unittest.mock import AsyncMock, MagicMock

    client = make_directory_client()
    peer = MagicMock(send_privmsg=AsyncMock(return_value=True))
    client._get_connected_peer = MagicMock(return_value=peer)  # type: ignore[method-assign]
    directory = MagicMock(send_private_message=AsyncMock(), host="dir", port=1)
    client.clients = {"dir:1": directory}

    channel = await client.send_privmsg("maker", "tx", "blob", force_channel="directory")
    assert channel == "directory:dir:1"
    directory.send_private_message.assert_awaited_once_with("maker", "tx", "blob")
    peer.send_privmsg.assert_not_awaited()
