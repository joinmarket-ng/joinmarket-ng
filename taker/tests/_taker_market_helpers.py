"""Shared credential-market test helpers for taker tests.

Separated from conftest.py to avoid import collisions when running tests from
the monorepo root.
"""

from __future__ import annotations

import time
from typing import Any

from bitcointx.core.key import CKey
from jmcore.credential_market import CredentialPackage, PaymentTerms, SignedDocument
from jmcore.market_keys import BoundMarketKeys
from jmcore.market_store import MarketStore
from jmswap.lnd_invoices import HoldInvoiceStatus, payment_hash_for
from jmswap.lnd_peer import InvoiceState

from taker._vendor.bolt11 import Bolt11, MilliSatoshi, Tag, TagChar, Tags, encode


def hold_quote(
    store: MarketStore,
    authorization: SignedDocument,
    seller_key: CKey | BoundMarketKeys,
    buyer_pubkey: str,
    product: Any,
    certificate_pubkey: str | None,
    rail: Any,
    max_price_sats: int,
    now: int,
    height: int,
    *,
    ttl: int = 300,
    request_id: str,
    price_sats: int = 1_000,
) -> SignedDocument:
    """Reserve and issue one quote with placeholder payment terms (store tests only)."""
    result = store.reserve_hold_quote(
        authorization,
        seller_key,
        buyer_pubkey,
        product,
        certificate_pubkey,
        rail,
        max_price_sats,
        price_sats,
        now,
        height,
        ttl=ttl,
        request_id=request_id,
    )
    if isinstance(result, SignedDocument):
        return result
    payment_hash = result.payment_hash.hex()
    terms = PaymentTerms(
        rail="lightning", request=f"regtest-hold-{payment_hash}", amount_sats=result.amount_sats
    )
    return store.issue_hold_quote(result.quote_id, seller_key, terms, f"ln:{payment_hash}", now)


def settle_quote(
    store: MarketStore,
    quote_id: str,
    seller_key: CKey | BoundMarketKeys,
    now: int,
    height: int,
) -> CredentialPackage:
    """Finalize a quote as if its hold invoice was accepted, then record settlement."""
    package = store.finalize(quote_id, seller_key, now, height)
    store.complete_hold_settle(quote_id)
    return package


def regtest_invoice(payment_hash: bytes, amount_sats: int, expiry_seconds: int) -> str:
    now = int(time.time())
    return encode(
        Bolt11(
            currency="bcrt",
            date=now,
            amount_msat=MilliSatoshi(amount_sats * 1000),
            tags=Tags(
                [
                    Tag(TagChar.payment_hash, payment_hash.hex()),
                    Tag(TagChar.payment_secret, "22" * 32),
                    Tag(TagChar.description, ""),
                    Tag(TagChar.expire_time, expiry_seconds),
                    Tag(TagChar.min_final_cltv_expiry, 18),
                ]
            ),
        ),
        "11" * 32,
    )


class FakeHoldInvoices:
    """In-memory stand-in for :class:`jmswap.lnd_invoices.LndInvoiceClient`."""

    def __init__(self) -> None:
        self.invoices: dict[bytes, HoldInvoiceStatus] = {}
        self.calls: list[tuple[str, bytes]] = []
        self.fail: set[str] = set()
        self.height = 1_000

    def _check(self, method: str, key: bytes) -> None:
        self.calls.append((method, key))
        if method in self.fail:
            raise RuntimeError(f"injected {method} failure")

    async def add_hold_invoice(
        self, payment_hash: bytes, amount_sats: int, expiry_seconds: int, *, private: bool = False
    ) -> str:
        self._check("add", payment_hash)
        existing = self.invoices.get(payment_hash)
        if existing is not None:
            return existing.payment_request
        request = regtest_invoice(payment_hash, amount_sats, expiry_seconds)
        self.invoices[payment_hash] = HoldInvoiceStatus(InvoiceState.OPEN, request, 0, None)
        return request

    async def lookup(self, payment_hash: bytes) -> HoldInvoiceStatus | None:
        self._check("lookup", payment_hash)
        return self.invoices.get(payment_hash)

    async def settle(self, preimage: bytes) -> None:
        payment_hash = payment_hash_for(preimage)
        self._check("settle", payment_hash)
        status = self.invoices[payment_hash]
        if status.state is not InvoiceState.ACCEPTED:
            raise RuntimeError("invoice is not accepted")
        self._set(payment_hash, InvoiceState.SETTLED)

    async def cancel(self, payment_hash: bytes) -> None:
        self._check("cancel", payment_hash)
        status = self.invoices.get(payment_hash)
        if status is None:
            raise RuntimeError("invoice not found")
        if status.state is InvoiceState.SETTLED:
            raise RuntimeError("invoice already settled")
        self._set(payment_hash, InvoiceState.CANCELED)

    def pay(self, payment_hash: bytes, amount_sats: int, expiry_height: int = 1_100) -> None:
        """Simulate the buyer's HTLC being accepted."""
        status = self.invoices[payment_hash]
        assert status.state is InvoiceState.OPEN
        self.invoices[payment_hash] = HoldInvoiceStatus(
            InvoiceState.ACCEPTED, status.payment_request, amount_sats, expiry_height
        )

    def _set(self, payment_hash: bytes, state: InvoiceState) -> None:
        status = self.invoices[payment_hash]
        self.invoices[payment_hash] = HoldInvoiceStatus(
            state,
            status.payment_request,
            status.amount_paid_sats,
            status.min_htlc_expiry_height if state is InvoiceState.ACCEPTED else None,
        )
