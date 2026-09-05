"""Private market request handling and buyer-side acceptance checks."""

from __future__ import annotations

import base64
import hashlib
import time
from typing import Any, Literal

from bitcointx.core.key import CKey
from jmcore.btc_script import derive_bond_address
from jmcore.credential_market import (
    BondCredential,
    CredentialPackage,
    Hex32,
    MarketAuthorization,
    MarketError,
    MarketListing,
    MarketModel,
    MarketQuote,
    Product,
    Pubkey,
    SignedDocument,
    canonical,
    decode_document,
    document_hash,
    period_at_height,
    sign_document,
    verify_allocation,
    verify_authorization,
)
from jmcore.market_faults import MarketFaultCache
from jmwallet.backends.base import BlockchainBackend, BondVerificationRequest
from nacl.public import PrivateKey, SealedBox
from pydantic import Field

from taker.market_payments import validate_payment_terms
from taker.market_store import MarketStore, MarketStoreError


class QuoteRequest(MarketModel):
    action: Literal["quote"] = "quote"
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    buyer_pubkey: Hex32
    product: Product
    certificate_pubkey: Pubkey | None = None
    rail: Literal["onchain", "lightning"]
    max_price_sats: int = Field(ge=1, le=2100000000000000)


class DeliveryRequest(MarketModel):
    action: Literal["delivery"] = "delivery"
    quote_id: Hex32


async def verify_market_bond(
    authority: MarketAuthorization,
    backend: BlockchainBackend,
    network: str,
    now: int,
    fault_cache: MarketFaultCache | None = None,
) -> int:
    """Verify stake against the configured chain, never against a seller API."""
    height = await backend.get_block_height()
    if authority.bond.network != network or authority.period != period_at_height(height):
        raise MarketError("Market authorization network or period mismatch")
    bond = authority.bond
    if bond.locktime <= now:
        raise MarketError("Market collateral is no longer timelocked")
    address = derive_bond_address(bytes.fromhex(bond.pubkey), bond.locktime, network)
    results = await backend.verify_bonds(
        [
            BondVerificationRequest(
                txid=bond.outpoint.txid,
                vout=bond.outpoint.vout,
                utxo_pub=bytes.fromhex(bond.pubkey),
                locktime=bond.locktime,
                address=address.address,
                scriptpubkey=address.scriptpubkey.hex(),
            )
        ]
    )
    if len(results) != 1:
        raise MarketError("Market collateral verification unavailable")
    result = results[0]
    if (
        not result.valid
        or result.txid != bond.outpoint.txid
        or result.vout != bond.outpoint.vout
        or result.confirmations < 1
        or result.value <= 0
    ):
        raise MarketError("Invalid market collateral")
    if fault_cache is not None and fault_cache.excludes_verified_bond(bond, height=height):
        raise MarketError("Market collateral has verified fault evidence")
    return height


class MarketService:
    """Seller callbacks. Remote messages cannot acknowledge or settle a payment."""

    def __init__(
        self,
        store: MarketStore,
        authorization: SignedDocument,
        signing_key: CKey,
        encryption_key: PrivateKey,
        backend: BlockchainBackend,
        *,
        products: list[Product],
        price_sats: int,
        quote_ttl: int = 300,
        fault_cache: MarketFaultCache | None = None,
    ) -> None:
        self.authority = verify_authorization(authorization)
        if signing_key.pub.hex() != self.authority.seller_pubkey:
            raise MarketError("Wrong seller signing key")
        if not 1 <= quote_ttl <= 86400:
            raise MarketError("Invalid quote reservation lifetime")
        self.store = store
        self.authorization = authorization
        self.signing_key = signing_key
        self.encryption_key = encryption_key
        self.backend = backend
        self.products = products
        self.price_sats = price_sats
        self.quote_ttl = quote_ttl
        self.fault_cache = fault_cache
        self._verified_until = 0.0
        self._height = 0
        self._next_quote = 0.0

    async def _check_stake(self) -> int:
        if time.monotonic() >= self._verified_until:
            self._height = await verify_market_bond(
                self.authority,
                self.backend,
                self.authority.bond.network,
                int(time.time()),
                self.fault_cache,
            )
            self._verified_until = time.monotonic() + 10
        return self._height

    async def listing(self) -> bytes:
        await self._check_stake()
        return canonical(
            sign_document(
                MarketListing(
                    network=self.authority.bond.network,
                    period=self.authority.period,
                    seller_pubkey=self.authority.seller_pubkey,
                    encryption_pubkey=bytes(self.encryption_key.public_key).hex(),
                    products=self.products,
                    price_sats=self.price_sats,
                    expires_at=int(time.time()) + 60,
                ),
                self.signing_key,
            )
        )

    async def respond(self, _sender: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            if body.get("action") == "delivery":
                request = DeliveryRequest.model_validate(body)
                # A quote id is not buyer authentication. Return only the package
                # sealed to the original buyer, even to a caller knowing a txid.
                return {"delivery": self.store.get_delivery(request.quote_id)}
            quote_request = QuoteRequest.model_validate(body)
            if quote_request.product not in self.products:
                raise MarketError("Product unavailable")
            if time.monotonic() < self._next_quote:
                return {"error": "busy"}
            self._next_quote = time.monotonic() + 1
            height = await self._check_stake()
            quote_document = self.store.create_quote(
                self.authorization,
                self.signing_key,
                quote_request.buyer_pubkey,
                quote_request.product,
                quote_request.certificate_pubkey,
                quote_request.rail,
                quote_request.max_price_sats,
                int(time.time()),
                height,
                ttl=min(self.quote_ttl, 900)
                if quote_request.rail == "lightning"
                else self.quote_ttl,
                request_id=quote_request.request_id,
            )
            quote = quote_document.verified(MarketQuote, self.authority.seller_pubkey)
            validate_payment_terms(
                quote.payment, self.authority.bond.network, int(time.time()), quote.expires_at
            )
            return {"quote": quote_document.model_dump(mode="json")}
        except (ValueError, MarketStoreError):
            return {"error": "unavailable"}


def accept_listing(raw: bytes, network: str, now: int) -> tuple[SignedDocument, MarketListing]:
    signed = SignedDocument.model_validate(decode_document(raw))
    candidate = MarketListing.model_validate(signed.body)
    listing = signed.verified(MarketListing, candidate.seller_pubkey)
    if listing.network != network or not now < listing.expires_at <= now + 120:
        raise MarketError("Listing network or expiry mismatch")
    return signed, listing


async def accept_quote(
    signed: SignedDocument,
    listing: MarketListing,
    request: QuoteRequest,
    backend: BlockchainBackend,
    now: int,
    fault_cache: MarketFaultCache | None = None,
) -> MarketQuote:
    quote = signed.verified(MarketQuote, listing.seller_pubkey)
    if (
        quote.buyer_pubkey != request.buyer_pubkey
        or quote.product != request.product
        or quote.certificate_pubkey != request.certificate_pubkey
        or quote.payment.rail != request.rail
    ):
        raise MarketError("Quote does not match buyer request")
    authority = verify_authorization(quote.authorization)
    if authority.seller_pubkey != listing.seller_pubkey:
        raise MarketError("Quote seller is not authorized by collateral owner")
    height = await verify_market_bond(authority, backend, listing.network, now, fault_cache)
    quote.check(
        network=listing.network, height=height, now=now, max_price_sats=request.max_price_sats
    )
    validate_payment_terms(quote.payment, listing.network, now, quote.expires_at)
    return quote


def open_delivery(
    encrypted: str, buyer_key: PrivateKey, quote_document: SignedDocument
) -> CredentialPackage:
    if len(encrypted) > 22000:
        raise MarketError("Delivery size limit exceeded")
    candidate = MarketQuote.model_validate(quote_document.body)
    authority = verify_authorization(candidate.authorization)
    quote = quote_document.verified(MarketQuote, authority.seller_pubkey)
    if bytes(buyer_key.public_key).hex() != quote.buyer_pubkey:
        raise MarketError("Wrong buyer key")
    plaintext = SealedBox(buyer_key).decrypt(base64.b64decode(encrypted, validate=True))
    package = CredentialPackage.model_validate(decode_document(plaintext))
    package.verify()
    _, allocation = verify_allocation(package.authorization, package.allocation)
    if (
        document_hash(package.authorization.body) != document_hash(quote.authorization.body)
        or allocation.allocation_id != quote.quote_id
        or allocation.buyer_tag != hashlib.sha256(bytes(buyer_key.public_key)).hexdigest()
        or allocation.resource != quote.resource
        or allocation.product != quote.product
        or allocation.certificate_pubkey != quote.certificate_pubkey
    ):
        raise MarketError("Delivery does not match purchased quote")
    return package


def make_bond_credential(
    authorization: SignedDocument, certificate_pubkey: str, owner_key: CKey
) -> BondCredential:
    """Offline owner helper. It never receives a renter's private key."""
    from jmcore.crypto import bitcoin_message_hash_bytes, get_cert_msg

    authority = verify_authorization(authorization)
    if owner_key.pub.hex() != authority.bond.pubkey:
        raise MarketError("Wrong bond spending key")
    expiry = authority.period + 1
    signature = owner_key.sign(
        bitcoin_message_hash_bytes(get_cert_msg(bytes.fromhex(certificate_pubkey), expiry))
    )
    credential = BondCredential(
        bond=authority.bond,
        cert_pubkey=certificate_pubkey,
        cert_expiry=expiry,
        cert_signature=signature.hex(),
    )
    credential.verify()
    return credential
