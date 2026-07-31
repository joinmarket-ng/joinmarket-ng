from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

import pytest
from coincurve import PrivateKey
from pydantic import ValidationError

from jmswap.interfaces import DirectOnionAdapter
from jmswap.models import CreateNormalSwapRequest, SwapOffer
from jmswap.nip04 import create_dm, nip04_decrypt, nip04_encrypt, privkey_to_xonly_pubkey
from jmswap.nostr import (
    DISCOVERY_D_TAG,
    SWAP_DISCOVERY_KIND,
    SWAP_RPC_KIND,
    ElectrumNostrAdapter,
    LoopbackEventTransport,
)


def offer() -> SwapOffer:
    return SwapOffer(
        percentage_fee=Decimal("0.5"),
        mining_fee=500,
        min_amount=20_000,
        max_forward_amount=1_000_000,
        max_reverse_amount=2_000_000,
        relays="wss://relay.example",
        protocols=("taproot-v1", "taproot-cooperative-v1"),
        pow_nonce="0x0",
    )


def test_strict_camel_case_dto_rejects_unknown_and_snake_case() -> None:
    valid = {
        "invoiceAmount": 20_000,
        "refundPublicKey": PrivateKey.from_int(2).public_key.format().hex(),
        "protocol": "taproot-v1",
    }
    assert CreateNormalSwapRequest.model_validate(valid).invoice_amount == 20_000
    with pytest.raises(ValidationError):
        CreateNormalSwapRequest.model_validate({**valid, "unknown": True})
    with pytest.raises(ValidationError):
        CreateNormalSwapRequest.model_validate(
            {
                "invoice_amount": 20_000,
                **{key: value for key, value in valid.items() if key != "invoiceAmount"},
            }
        )


def test_nip04_roundtrip() -> None:
    first = PrivateKey.from_int(3).secret
    second = PrivateKey.from_int(4).secret
    encrypted = nip04_encrypt("canonical", first, privkey_to_xonly_pubkey(second))
    assert nip04_decrypt(encrypted, second, privkey_to_xonly_pubkey(first)) == "canonical"


async def test_electrum_discovery_and_top_level_rpc_roundtrip() -> None:
    relay = LoopbackEventTransport()
    provider = ElectrumNostrAdapter(PrivateKey.from_int(3).secret, relay)
    client = ElectrumNostrAdapter(PrivateKey.from_int(4).secret, relay)
    event = await provider.publish_offer(
        offer(), network="regtest", expires_at=int(time.time()) + 60
    )
    assert event["kind"] == SWAP_DISCOVERY_KIND
    assert ["d", DISCOVERY_D_TAG] in event["tags"]
    discovered = await client.discover(network="regtest", timeout=0.05)
    assert discovered == [(provider.pubkey, offer())]
    client.select_provider(provider.pubkey)
    seen: list[tuple[str, dict[str, Any]]] = []

    async def handler(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        seen.append((method, payload))
        return {"result": payload["value"]}

    serve = asyncio.create_task(provider.serve(handler))
    await asyncio.sleep(0)
    try:
        result = await client.request("createswap", {"value": 42})
        assert result == {"result": 42}
        assert seen == [("createswap", {"value": 42})]
    finally:
        serve.cancel()
        with pytest.raises(asyncio.CancelledError):
            await serve


def test_nostr_validation_rejects_replay_wrong_recipient_and_stale() -> None:
    relay = LoopbackEventTransport()
    node = ElectrumNostrAdapter(PrivateKey.from_int(3).secret, relay)
    sender = PrivateKey.from_int(4).secret
    event = create_dm(json.dumps({"method": "x"}), sender, node.pubkey, kind=SWAP_RPC_KIND)
    assert node._validate_rpc_event(event, recipient=node.pubkey)
    assert not node._validate_rpc_event(event, recipient=node.pubkey)
    wrong = create_dm(
        "{}", sender, privkey_to_xonly_pubkey(PrivateKey.from_int(5).secret), kind=SWAP_RPC_KIND
    )
    assert not node._validate_rpc_event(wrong, recipient=node.pubkey)
    stale = create_dm("{}", sender, node.pubkey, kind=SWAP_RPC_KIND)
    stale["created_at"] -= 1_000
    from jmswap.nip04 import sign_event

    sign_event(stale, sender)
    assert not node._validate_rpc_event(stale, recipient=node.pubkey)


async def test_direct_onion_adapter_does_not_add_an_envelope() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def encrypted_callback(method: str, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append((method, payload))
        return {"ok": True}

    adapter = DirectOnionAdapter(encrypted_callback)
    assert await adapter.request("createswap", {"id": "00" * 32}) == {"ok": True}
    assert calls == [("createswap", {"id": "00" * 32})]
