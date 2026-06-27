"""Authentication of relayed maker responses in wait_for_responses.

A malicious peer must not be able to have its response attributed to another
maker by embedding that maker's nick in its payload, nor to impersonate a maker
without its signing key.
"""

from __future__ import annotations

import pytest
from _taker_test_helpers import make_directory_client
from jmcore.crypto import NickIdentity
from jmcore.network import ONION_HOSTID


def signed_line(identity: NickIdentity, recipient: str, command: str, data: str) -> str:
    """Build a directory-relayed, signed JM privmsg line."""
    return f"{identity.nick}!{recipient}!{command} {identity.sign_message(data, ONION_HOSTID)}"


async def run(client, expected_nicks, command, lines, counts=None):
    for line in lines:
        await client._direct_message_queue.put({"line": line, "source": "dir1"})
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
async def test_genuine_error_from_maker_recorded():
    client = make_directory_client()
    maker = NickIdentity(5)
    line = signed_line(maker, client.nick_identity.nick, "error", "commitment-blacklisted")
    responses = await run(client, [maker.nick], "!pubkey", [line])
    assert responses[maker.nick]["error"] is True
    assert "commitment-blacklisted" in responses[maker.nick]["data"]
