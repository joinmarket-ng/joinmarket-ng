"""Tests for the Nostr transport, NIP-04 crypto, and advert/RPC roundtrips."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from coincurve import PrivateKey

from jmswap.errors import SwapProtocolError
from jmswap.models import ProviderTerms, SwapDirection
from jmswap.nip04 import (
    create_nip04_dm_event,
    nip04_decrypt,
    nip04_encrypt,
    privkey_to_xonly_pubkey,
    verify_event,
)
from jmswap.nostr import LoopbackRelay, SwapNostrNode

PROVIDER_SWAP_PUBKEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


def _terms(pubkey_hex: str) -> ProviderTerms:
    return ProviderTerms(
        provider_id=pubkey_hex,
        provider_pubkey=PROVIDER_SWAP_PUBKEY,
        network="regtest",
        directions=[SwapDirection.SWAP_IN, SwapDirection.SWAP_OUT],
        name="test-provider",
    )


def test_nip04_encrypt_decrypt_roundtrip() -> None:
    a = PrivateKey().secret
    b = PrivateKey().secret
    a_pub = privkey_to_xonly_pubkey(a)
    b_pub = privkey_to_xonly_pubkey(b)
    ciphertext = nip04_encrypt("hello swap", a, b_pub)
    assert nip04_decrypt(ciphertext, b, a_pub) == "hello swap"


def test_event_signature_verifies() -> None:
    sk = PrivateKey().secret
    recipient = privkey_to_xonly_pubkey(PrivateKey().secret)
    event = create_nip04_dm_event("ping", sk, recipient)
    assert verify_event(event)
    event["content"] = event["content"] + "x"  # tamper
    assert not verify_event(event)


async def test_advertise_and_discover() -> None:
    relay = LoopbackRelay()
    provider = SwapNostrNode(PrivateKey().secret, relay)
    client = SwapNostrNode(PrivateKey().secret, relay)
    await provider.advertise(_terms(provider.pubkey))
    adverts = await client.discover(timeout=0.2)
    assert len(adverts) == 1
    assert adverts[0].nostr_pubkey == provider.pubkey
    assert adverts[0].terms.name == "test-provider"


async def test_rpc_call_roundtrip() -> None:
    relay = LoopbackRelay()
    provider = SwapNostrNode(PrivateKey().secret, relay)
    client = SwapNostrNode(PrivateKey().secret, relay)

    async def handler(method: str, params: dict[str, Any], sender: str) -> dict[str, Any]:
        assert method == "createswap"
        assert sender == client.pubkey
        return {"echo": params["amount"], "swap_id": "abc"}

    serve_task = asyncio.create_task(provider.serve(handler))
    try:
        result = await client.call(provider.pubkey, "createswap", {"amount": 50_000}, timeout=2.0)
        assert result == {"echo": 50_000, "swap_id": "abc"}
    finally:
        serve_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve_task


async def test_rpc_call_propagates_handler_error() -> None:
    relay = LoopbackRelay()
    provider = SwapNostrNode(PrivateKey().secret, relay)
    client = SwapNostrNode(PrivateKey().secret, relay)

    async def handler(method: str, params: dict[str, Any], sender: str) -> dict[str, Any]:
        raise ValueError("insufficient liquidity")

    serve_task = asyncio.create_task(provider.serve(handler))
    try:
        with pytest.raises(SwapProtocolError, match="insufficient liquidity"):
            await client.call(provider.pubkey, "createswap", {}, timeout=2.0)
    finally:
        serve_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve_task


async def test_loopback_rejects_unsigned_event() -> None:
    relay = LoopbackRelay()
    with pytest.raises(SwapProtocolError):
        await relay.publish(
            {"pubkey": "00", "kind": 1, "tags": [], "content": "x", "created_at": 0}
        )
