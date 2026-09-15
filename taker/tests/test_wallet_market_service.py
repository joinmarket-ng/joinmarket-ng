"""Synthetic wallet-bound seller service and transport coverage."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import pubkey_to_p2wpkh_address
from jmcore.credential_market import (
    BondCredential,
    BondReference,
    MarketAuthorization,
    MarketError,
    MarketQuote,
    PaymentTerms,
    SignedDocument,
    bond_resource,
    sign_document,
)
from jmcore.crypto import NickIdentity, bitcoin_message_hash_bytes, get_cert_msg
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_keys import BoundMarketKeys, MarketKeyError, MarketKeyScope, WalletMarketKeys
from jmwallet.backends.base import BondVerificationRequest
from nacl.public import PrivateKey
from test_market_transport import LoopbackDirectory

from taker.market_service import (
    MarketService,
    QuoteRequest,
    accept_listing,
    accept_quote,
    open_delivery,
)
from taker.market_store import MarketStore
from taker.market_transport import MarketRequestTimeoutError, MarketTransport

SEED = bytes.fromhex(
    "c76c4ac4f4e4a00d6b274d5c39c700bb4a7ddc04fbc6f78e85ca75007b5b495f74a9"
    "043eeb77bdd53aa6fc3a0e31462270316fa04b8c19114c8798706cd02ac8"
)


def _key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


class _Backend:
    async def get_block_height(self) -> int:
        return 1

    async def verify_bonds(self, bonds: list[BondVerificationRequest]) -> list[SimpleNamespace]:
        return [
            SimpleNamespace(
                valid=True,
                txid=bond.txid,
                vout=bond.vout,
                confirmations=6,
                value=1_000_000,
            )
            for bond in bonds
        ]


class _BlockingBackend(_Backend):
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def get_block_height(self) -> int:
        self.entered.set()
        await self.release.wait()
        return 1


def _bond() -> BondReference:
    return BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=1),
        pubkey=bytes(_key(0x11).pub).hex(),
        locktime=int(time.time()) + 86_400,
    )


def _scope(bond: BondReference, **changes: object) -> MarketKeyScope:
    return MarketKeyScope.model_validate(
        {
            "network": "regtest",
            "chain_hash": "11" * 32,
            "role": "seller",
            "period": 0,
            "bond": bond.outpoint.model_dump(mode="json"),
        }
        | changes
    )


def _authorization(bound: BoundMarketKeys, bond: BondReference) -> SignedDocument:
    return sign_document(
        MarketAuthorization(bond=bond, period=0, seller_pubkey=bound.signing_public_key().hex()),
        _key(0x11),
    )


def _payment() -> PaymentTerms:
    return PaymentTerms(
        rail="onchain",
        request=pubkey_to_p2wpkh_address(b"\x02" + b"\x42" * 32, "regtest"),
        amount_sats=1_000,
        min_confirmations=1,
    )


def _credential(bond: BondReference) -> dict[str, object]:
    certificate = _key(0x44)
    signature = _key(0x11).sign(
        bitcoin_message_hash_bytes(get_cert_msg(bytes(certificate.pub), 1)),
        _ecdsa_sig_grind_low_r=False,
    )
    return BondCredential(
        bond=bond,
        cert_pubkey=bytes(certificate.pub).hex(),
        cert_expiry=1,
        cert_signature=signature.hex(),
    ).model_dump(mode="json")


def _activate_store(path: Path, wallet_id: str) -> MarketStore:
    commitments = path.parent / "commitments.json"
    commitments.write_text(json.dumps({"external_v1": {}, "used": []}), encoding="ascii")
    store = MarketStore(path, wallet_id=wallet_id)
    store.activate_wallet(commitments, history_confirmed=True)
    return store


def _request(buyer_key: PrivateKey) -> QuoteRequest:
    return QuoteRequest(
        request_id="01" * 16,
        buyer_pubkey=bytes(buyer_key.public_key).hex(),
        product="bond",
        certificate_pubkey=bytes(_key(0x44).pub).hex(),
        rail="onchain",
        max_price_sats=1_000,
    )


@pytest.mark.asyncio
async def test_wallet_bound_seller_lifecycle_uses_private_transport(tmp_path: Path) -> None:
    bond = _bond()
    keys = WalletMarketKeys(SEED, "regtest")
    bound = keys.bind(_scope(bond))
    store = _activate_store(tmp_path / "seller.sqlite", bound.wallet_id)
    store.add_inventory("bond", bond_resource(bond, 0), None)
    store.add_payment(_payment(), "payment-1")
    authorization = _authorization(bound, bond)
    backend = _Backend()
    service = MarketService(
        store,
        authorization,
        bound,
        bound,
        backend,
        products=["bond"],
        price_sats=1_000,
    )
    seller = MarketTransport(
        directory_servers=[],
        network="regtest",
        nick_identity=NickIdentity(private_key_bytes=b"\x01" * 32),
        encryption_private_key=bound,
        listing_callback=service.listing,
        responder=service.respond,
        direct_location="127.0.0.1:5222",
        connection_timeout=0.2,
    )
    buyer = MarketTransport(
        directory_servers=[],
        network="regtest",
        nick_identity=NickIdentity(private_key_bytes=b"\x02" * 32),
        direct_location="127.0.0.1:5222",
        connection_timeout=0.2,
    )
    relay = LoopbackDirectory()
    buyer_key = PrivateKey.generate()
    try:
        await seller.start()
        await buyer.start()
        relay.attach(seller)
        relay.attach(buyer)

        discovered = await buyer.discover(timeout=0.5)
        assert len(discovered) == 1
        seller_nick, listing_raw = discovered[0]
        _, listing = accept_listing(listing_raw, "regtest", int(time.time()))
        assert listing.seller_pubkey == bound.signing_public_key().hex()

        request = _request(buyer_key)
        response = await buyer.request(
            seller_nick,
            bytes.fromhex(listing.encryption_pubkey),
            request.model_dump(mode="json"),
            timeout=0.5,
        )
        quote_document = SignedDocument.model_validate(response["quote"])
        quote = await accept_quote(quote_document, listing, request, backend, int(time.time()))
        assert quote == quote_document.verified(MarketQuote, listing.seller_pubkey)

        store.attach_credential(quote.quote_id, _credential(bond))
        store.finalize(quote.quote_id, bound, "synthetic-settlement", int(time.time()), 1)
        delivery = await buyer.request(
            seller_nick,
            bytes.fromhex(listing.encryption_pubkey),
            {"action": "delivery", "quote_id": quote.quote_id},
            timeout=0.5,
        )
        package = open_delivery(str(delivery["delivery"]), buyer_key, quote_document)
        assert package == store.get_package(quote.quote_id)
    finally:
        await buyer.close()
        await seller.close()
        keys.close()
        store.close()


def test_wallet_bound_service_rejects_mixed_scope_and_wallet_constructors(tmp_path: Path) -> None:
    bond = _bond()
    keys = WalletMarketKeys(SEED, "regtest")
    bound = keys.bind(_scope(bond))
    authorization = _authorization(bound, bond)
    backend = _Backend()
    matching = _activate_store(tmp_path / "matching.sqlite", bound.wallet_id)
    other_keys = WalletMarketKeys(b"\x01" * 64, "regtest")
    other_store = _activate_store(tmp_path / "other.sqlite", other_keys.ledger_identity())
    different_scope = keys.bind(_scope(bond, chain_hash="22" * 32))
    try:
        with pytest.raises(MarketError, match="both be wallet-bound"):
            MarketService(
                matching,
                authorization,
                bound,
                PrivateKey.generate(),
                backend,
                products=["bond"],
                price_sats=1_000,
            )
        with pytest.raises(MarketError, match="different scopes"):
            MarketService(
                matching,
                authorization,
                bound,
                different_scope,
                backend,
                products=["bond"],
                price_sats=1_000,
            )
        with pytest.raises(MarketError, match="ready matching"):
            MarketService(
                other_store,
                authorization,
                bound,
                bound,
                backend,
                products=["bond"],
                price_sats=1_000,
            )
    finally:
        keys.close()
        other_keys.close()
        matching.close()
        other_store.close()


def test_transport_rejects_invalid_or_closed_provider_capability() -> None:
    buyer_keys = WalletMarketKeys(SEED, "regtest")
    buyer = buyer_keys.bind(
        MarketKeyScope(network="regtest", chain_hash="11" * 32, role="buyer", period=0)
    )
    signet_keys = WalletMarketKeys(SEED, "signet")
    seller = signet_keys.bind(
        MarketKeyScope(network="signet", chain_hash="11" * 32, role="seller", period=0)
    )
    closed_keys = WalletMarketKeys(b"\x02" * 64, "regtest")
    closed = closed_keys.bind(
        MarketKeyScope(network="regtest", chain_hash="11" * 32, role="seller", period=0)
    )
    closed_keys.close()
    kwargs = {
        "directory_servers": [],
        "network": "regtest",
        "nick_identity": NickIdentity(private_key_bytes=b"\x06" * 32),
        "direct_location": "127.0.0.1:5222",
    }
    try:
        with pytest.raises(MarketKeyError, match="seller scope"):
            MarketTransport(encryption_private_key=buyer, **kwargs)
        with pytest.raises(MarketKeyError, match="network"):
            MarketTransport(encryption_private_key=seller, **kwargs)
        with pytest.raises(MarketKeyError, match="closed"):
            MarketTransport(encryption_private_key=closed, **kwargs)
    finally:
        buyer_keys.close()
        signet_keys.close()


@pytest.mark.asyncio
async def test_revoked_or_recovery_required_wallet_stops_seller_issuance(tmp_path: Path) -> None:
    bond = _bond()
    keys = WalletMarketKeys(SEED, "regtest")
    bound = keys.bind(_scope(bond))
    store = _activate_store(tmp_path / "seller.sqlite", bound.wallet_id)
    store.add_inventory("bond", bond_resource(bond, 0), None)
    store.add_payment(_payment(), "payment-1")
    authorization = _authorization(bound, bond)
    backend = _BlockingBackend()
    service = MarketService(
        store,
        authorization,
        bound,
        bound,
        backend,
        products=["bond"],
        price_sats=1_000,
    )
    buyer_key = PrivateKey.generate()
    listing_task = asyncio.create_task(service.listing())
    quote_task = asyncio.create_task(
        service.respond("J5ABCDEFGHJKLMNP", _request(buyer_key).model_dump())
    )
    try:
        await backend.entered.wait()
        keys.close()
        backend.release.set()
        with pytest.raises(MarketKeyError):
            await listing_task
        assert await quote_task == {"error": "unavailable"}
        assert store.pending(int(time.time())) == []
    finally:
        if not listing_task.done():
            listing_task.cancel()
        if not quote_task.done():
            quote_task.cancel()
        await asyncio.gather(listing_task, quote_task, return_exceptions=True)
        store.close()

    restored = WalletMarketKeys(SEED, "regtest")
    restored_bound = restored.bind(_scope(bond))
    recovered_store = _activate_store(tmp_path / "recovery.sqlite", restored_bound.wallet_id)
    try:
        recovered_service = MarketService(
            recovered_store,
            _authorization(restored_bound, bond),
            restored_bound,
            restored_bound,
            _Backend(),
            products=["bond"],
            price_sats=1_000,
        )
        recovered_store.mark_recovery_required()
        with pytest.raises(MarketError, match="ready matching"):
            await recovered_service.listing()
        assert await recovered_service.respond(
            "J5ABCDEFGHJKLMNP", _request(buyer_key).model_dump()
        ) == {"error": "unavailable"}
    finally:
        restored.close()
        recovered_store.close()


@pytest.mark.asyncio
async def test_closed_provider_capability_drops_request_before_responder(tmp_path: Path) -> None:
    bond = _bond()
    keys = WalletMarketKeys(SEED, "regtest")
    bound = keys.bind(_scope(bond))
    store = _activate_store(tmp_path / "seller.sqlite", bound.wallet_id)
    authorization = _authorization(bound, bond)
    service = MarketService(
        store,
        authorization,
        bound,
        bound,
        _Backend(),
        products=["bond"],
        price_sats=1_000,
    )
    calls: list[dict[str, object]] = []

    async def responder(sender: str, body: dict[str, object]) -> dict[str, object]:
        calls.append(body)
        return await service.respond(sender, body)

    seller = MarketTransport(
        directory_servers=[],
        network="regtest",
        nick_identity=NickIdentity(private_key_bytes=b"\x03" * 32),
        encryption_private_key=bound,
        responder=responder,
        direct_location="127.0.0.1:5222",
        connection_timeout=0.2,
    )
    buyer = MarketTransport(
        directory_servers=[],
        network="regtest",
        nick_identity=NickIdentity(private_key_bytes=b"\x04" * 32),
        direct_location="127.0.0.1:5222",
        connection_timeout=0.2,
    )
    relay = LoopbackDirectory()
    buyer_key = PrivateKey.generate()
    provider_public_key = bound.encryption_public_key()
    try:
        await seller.start()
        await buyer.start()
        relay.attach(seller)
        relay.attach(buyer)
        keys.close()

        with pytest.raises(MarketRequestTimeoutError):
            await buyer.request(
                seller.nick,
                provider_public_key,
                _request(buyer_key).model_dump(mode="json"),
                timeout=0.4,
            )
        assert calls == []
    finally:
        await buyer.close()
        await seller.close()
        store.close()
