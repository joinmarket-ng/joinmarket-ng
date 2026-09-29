"""Tests for bounded public credential-market listing capture."""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from bitcointx.core.key import CKey

from jmcore.credential_market import (
    MAX_MARKET_LISTING_BYTES,
    MarketError,
    MarketListing,
    accept_listing,
    canonical,
    sign_document,
)
from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient
from jmcore.network import ONION_HOSTID
from jmcore.protocol import MessageType


def _key(byte: int) -> CKey:
    return CKey(bytes([byte]) * 32)


def _listing_raw(*, network: str = "regtest", expires_at: int = 101) -> bytes:
    seller = _key(0x11)
    listing = MarketListing(
        network=network,
        period=0,
        seller_pubkey=bytes(seller.pub).hex(),
        encryption_pubkey="22" * 32,
        products=["podle"],
        price_sats=1,
        expires_at=expires_at,
    )
    return canonical(sign_document(listing, seller))


def _moffer(identity: NickIdentity, raw: bytes, *, recipient: str = "PUBLIC") -> dict[str, str]:
    encoded = base64.b64encode(raw).decode("ascii")
    signed = identity.sign_message(encoded, ONION_HOSTID)
    return {
        "type": MessageType.PUBMSG.value,
        "line": f"{identity.nick}!{recipient}!moffer {signed}",
    }


@pytest.mark.asyncio
async def test_signed_public_moffer_is_captured_and_drained() -> None:
    client = DirectoryClient("directory-a", 5222, "regtest")
    sender = NickIdentity(private_key_bytes=b"\x01" * 32)
    raw = _listing_raw()
    message = _moffer(sender, raw)
    client.connection = MagicMock()
    client.connection.is_connected.return_value = True
    client.connection.receive = AsyncMock(
        side_effect=[json.dumps(message).encode(), TimeoutError()]
    )

    assert await client.listen_for_messages(duration=1.0) == [message]

    assert client.drain_market_listings() == [(sender.nick, raw)]
    assert client.drain_market_listings() == []
    assert client.offers == {}
    assert client.bonds == {}


def test_market_listing_drain_rejects_bad_signature_and_noncanonical_document() -> None:
    client = DirectoryClient("directory-a", 5222, "regtest")
    sender = NickIdentity(private_key_bytes=b"\x02" * 32)
    attacker = NickIdentity(private_key_bytes=b"\x03" * 32)
    raw = _listing_raw()
    encoded = base64.b64encode(raw).decode("ascii")
    bad_signature = {
        "type": MessageType.PUBMSG.value,
        "line": f"{sender.nick}!PUBLIC!moffer {attacker.sign_message(encoded, ONION_HOSTID)}",
    }

    client._capture_market_listing(bad_signature)
    client._capture_market_listing(_moffer(sender, b'{"not": "canonical"}'))

    assert client.drain_market_listings() == []


@pytest.mark.parametrize(
    "raw",
    [b"not-base64", b"x" * (MAX_MARKET_LISTING_BYTES + 1)],
)
def test_market_listing_capture_rejects_invalid_or_bloated_base64(raw: bytes) -> None:
    client = DirectoryClient("directory-a", 5222, "regtest")
    sender = NickIdentity(private_key_bytes=b"\x04" * 32)
    if raw == b"not-base64":
        message = {
            "type": MessageType.PUBMSG.value,
            "line": f"{sender.nick}!PUBLIC!moffer {sender.sign_message('%%%%', ONION_HOSTID)}",
        }
    else:
        message = _moffer(sender, raw)

    client._capture_market_listing(message)

    assert client.drain_market_listings() == []


@pytest.mark.parametrize(
    "message_type,recipient,sender",
    [("privmsg", "PUBLIC", "valid"), ("pubmsg", "other", "valid"), ("pubmsg", "PUBLIC", "invalid")],
)
def test_market_listing_capture_rejects_private_unicast_and_invalid_nicks(
    message_type: str, recipient: str, sender: str
) -> None:
    client = DirectoryClient("directory-a", 5222, "regtest")
    identity = NickIdentity(private_key_bytes=b"\x05" * 32)
    message = _moffer(identity, _listing_raw(), recipient=recipient)
    message["type"] = message_type
    if sender == "invalid":
        message["line"] = message["line"].replace(identity.nick, "invalid", 1)

    client._capture_market_listing(message)

    assert client.drain_market_listings() == []


def test_market_listing_capture_is_bounded_and_drain_clears_queue() -> None:
    client = DirectoryClient("directory-a", 5222, "regtest")
    sender = NickIdentity(private_key_bytes=b"\x06" * 32)
    message = _moffer(sender, _listing_raw())

    for _ in range(257):
        client._capture_market_listing(message)

    assert len(client.drain_market_listings()) == 256
    assert client.drain_market_listings() == []


def test_unverified_overflow_cannot_displace_valid_listing_batch() -> None:
    client = DirectoryClient("directory-a", 5222, "regtest")
    sender = NickIdentity(private_key_bytes=b"\x06" * 32)
    raw = _listing_raw()
    message = _moffer(sender, raw)
    for _ in range(256):
        client._capture_market_listing(message)

    forged = json.loads(raw)
    forged["body"]["price_sats"] = 2
    client._capture_market_listing(_moffer(sender, canonical(forged)))

    assert client.drain_market_listings() == [(sender.nick, raw)] * 256
    client._capture_market_listing(message)
    assert client.drain_market_listings() == [(sender.nick, raw)]


@pytest.mark.parametrize(("network", "expires_at"), [("testnet", 101), ("regtest", 100)])
def test_shared_listing_acceptance_rejects_network_and_expiry(
    network: str, expires_at: int
) -> None:
    with pytest.raises(MarketError, match="network or expiry"):
        accept_listing(_listing_raw(network=network, expires_at=expires_at), "regtest", 100)
