"""Automatic hold-invoice settlement for a credential-market seller.

Every quote is paid through its own hold invoice. The buyer's payment stays
locked in an accepted HTLC until the seller has durably prepared the delivery;
only then is the invoice settled. When a sale cannot complete, the invoice is
canceled and the network refunds the buyer.

The store records each decision before the LND call that carries it out
(``finalize`` records the intent to settle, ``begin_hold_cancel`` the intent
to cancel), and the reconciler only records an outcome after LND reports it. A
failed or ambiguous call is therefore retried on the next pass, including after
a restart. Inventory returns to sale only after LND confirms the cancellation.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Protocol, TypeAlias

from bitcointx.core.key import CKey
from jmcore.credential_market import MarketError
from jmcore.market_keys import BoundMarketKeys
from jmcore.market_store import (
    HoldInvoice,
    MarketStore,
    MarketStoreExpiredError,
    MarketStoreUnavailableError,
)
from jmcore.settings import JoinMarketSettings
from jmswap.lnd_invoices import HoldInvoiceStatus, LndInvoiceClient, payment_hash_for
from jmswap.lnd_peer import InvoiceState
from loguru import logger

# A reservation whose invoice was never published is canceled after this delay.
# It exceeds the invoice client deadline, so a slow in-flight quote is not raced.
RESERVATION_GRACE_SECONDS = 60
# How long an accepted payment may wait for a missing credential after the
# invoice expired (an offline bond owner signing with `seller attach`).
CREDENTIAL_GRACE_SECONDS = 600
# Cancel an accepted payment this many blocks before its HTLC would expire.
HTLC_SAFETY_BLOCKS = 12
RECONCILE_INTERVAL_SECONDS = 2.0


class HoldInvoiceBackend(Protocol):
    """The subset of :class:`jmswap.lnd_invoices.LndInvoiceClient` the seller uses."""

    async def add_hold_invoice(
        self, payment_hash: bytes, amount_sats: int, expiry_seconds: int
    ) -> str: ...

    async def lookup(self, payment_hash: bytes) -> HoldInvoiceStatus | None: ...

    async def settle(self, preimage: bytes) -> None: ...

    async def cancel(self, payment_hash: bytes) -> None: ...


def seller_invoice_client(settings: JoinMarketSettings) -> LndInvoiceClient:
    """Return the hold-invoice client for the configured seller LND node."""
    market = settings.market
    if not (market.lnd_grpc_url and market.lnd_tls_cert_path and market.lnd_macaroon_path):
        raise MarketError(
            "Selling needs a Lightning node for hold invoices: set [market] lnd_grpc_url,"
            " lnd_tls_cert_path and lnd_macaroon_path"
        )
    try:
        certificate = Path(market.lnd_tls_cert_path).expanduser().read_bytes()
        macaroon = Path(market.lnd_macaroon_path).expanduser().read_bytes()
    except OSError as exc:
        raise MarketError("Could not read the seller LND certificate or macaroon") from exc
    return LndInvoiceClient(market.lnd_grpc_url, certificate, macaroon)


InvoiceRoute = Callable[[bytes], str | None]
"""Name the node that must invoice a payment hash, or ``None`` when none may."""


class InvoiceRefusedError(MarketError):
    """No configured Lightning node may create this hold invoice."""


class MultiNodeHoldInvoices:
    """Hold invoices on several LND nodes, each new one created where ``route`` says.

    A maker invoices each sale from the node of the mixdepth whose coin it
    sells. The route is derived from the reserved credential, never from the
    current offer mixdepth, so a retry or restart cannot pair one mixdepth's
    coin with another's node. Invoices created earlier settle or cancel on
    the node that holds them. ``lookup`` returns ``None`` only when every node
    reports the hash unknown: an unreachable node makes the answer unknown, so
    it raises, and an invoice is never created twice on different nodes.
    Invoices are private, so they carry route hints for private channels.
    """

    def __init__(self, clients: Mapping[str, LndInvoiceClient], route: InvoiceRoute) -> None:
        if not clients:
            raise ValueError("At least one Lightning node is required")
        self._clients = dict(clients)
        self._route = route
        self._owners: dict[bytes, str] = {}
        self._stack = AsyncExitStack()

    async def __aenter__(self) -> MultiNodeHoldInvoices:
        try:
            for client in self._clients.values():
                await self._stack.enter_async_context(client)
        except BaseException:
            await self._stack.aclose()
            raise
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self._stack.aclose()

    async def add_hold_invoice(
        self, payment_hash: bytes, amount_sats: int, expiry_seconds: int
    ) -> str:
        owner = self._owners.get(payment_hash) or await self._find(payment_hash)
        if owner is None:
            owner = self._route(payment_hash)
            if owner is None or owner not in self._clients:
                raise InvoiceRefusedError("No Lightning node may invoice this sale")
        # Recorded before the call: a retry, or the cancel of an invoice whose
        # creation may still land, must reach the same node.
        self._owners[payment_hash] = owner
        return await self._clients[owner].add_hold_invoice(
            payment_hash, amount_sats, expiry_seconds, private=True
        )

    async def lookup(self, payment_hash: bytes) -> HoldInvoiceStatus | None:
        owner = self._owners.get(payment_hash)
        if owner is not None:
            return await self._clients[owner].lookup(payment_hash)
        found = await self._probe(payment_hash)
        return None if found is None else found[1]

    async def settle(self, preimage: bytes) -> None:
        client = await self._owner(payment_hash_for(preimage))
        await client.settle(preimage)

    async def cancel(self, payment_hash: bytes) -> None:
        client = await self._owner(payment_hash)
        await client.cancel(payment_hash)

    async def _owner(self, payment_hash: bytes) -> LndInvoiceClient:
        name = self._owners.get(payment_hash) or await self._find(payment_hash)
        if name is None:
            raise MarketError("No Lightning node knows this hold invoice")
        return self._clients[name]

    async def _find(self, payment_hash: bytes) -> str | None:
        found = await self._probe(payment_hash)
        return None if found is None else found[0]

    async def _probe(self, payment_hash: bytes) -> tuple[str, HoldInvoiceStatus] | None:
        failure: Exception | None = None
        for name, client in self._clients.items():
            try:
                status = await client.lookup(payment_hash)
            except Exception as exc:
                failure = exc
                continue
            if status is not None:
                self._owners[payment_hash] = name
                return name, status
        if failure is not None:
            raise failure
        return None


CredentialProvider = Callable[[str], Awaitable[None]]
"""Attach the credential for one accepted quote, if this seller can create it."""

SaleCheck = Callable[[HoldInvoice], Awaitable[bool]]
"""Whether a held sale is still deliverable; ``False`` refunds it, raising retries later."""


SellerKey: TypeAlias = CKey | BoundMarketKeys
SellerKeyResolver: TypeAlias = Callable[[HoldInvoice], SellerKey]
"""The key of the seller authority that quoted a hold, for a runtime serving several."""


class MarketSettlement:
    """Reconcile every unfinished hold invoice of one store with its LND node.

    Run exactly one instance per store, inside :meth:`MarketStore.seller_runtime`.
    """

    def __init__(
        self,
        store: MarketStore,
        invoices: HoldInvoiceBackend,
        seller_key: SellerKey | SellerKeyResolver,
        block_height: Callable[[], Awaitable[int]],
        *,
        credential_provider: CredentialProvider | None = None,
        sale_check: SaleCheck | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.invoices = invoices
        self.seller_key = seller_key
        if isinstance(seller_key, CKey | BoundMarketKeys):
            self._seller_key_for: SellerKeyResolver = lambda _hold: seller_key
        else:
            self._seller_key_for = seller_key
        self.block_height = block_height
        self.credential_provider = credential_provider
        self.sale_check = sale_check
        self.clock = clock

    async def run(self, interval: float = RECONCILE_INTERVAL_SECONDS) -> None:
        """Reconcile until cancelled."""
        while True:
            try:
                await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A store that is briefly unavailable must not end settlement.
                logger.warning("Hold invoice reconciliation pass failed: {}", type(exc).__name__)
            await asyncio.sleep(interval)

    async def reconcile_once(self) -> None:
        """Make one pass over every unfinished hold invoice."""
        for hold in self.store.hold_invoices(self._now()):
            try:
                await self._reconcile(hold)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Each invoice is retried on the next pass; one failure must not
                # stall the others. Errors carry no invoice data.
                logger.warning("Hold invoice reconciliation failed: {}", type(exc).__name__)

    def _now(self) -> int:
        return int(self.clock())

    async def _reconcile(self, hold: HoldInvoice) -> None:
        if hold.state == "settling":
            await self._settle(hold)
            return
        if hold.state == "canceling":
            await self._cancel(hold)
            return
        if hold.state == "reserved":
            if self._now() >= hold.created_at + RESERVATION_GRACE_SECONDS:
                self.store.begin_hold_cancel(hold.quote_id)
                await self._cancel(hold)
            return

        status = await self.invoices.lookup(hold.payment_hash)
        if status is None:
            # A published quote's invoice must exist. Its absence means a
            # different node or lost LND data; keep the reservation.
            logger.error("Hold invoice of a published quote is missing from LND")
            return
        if status.state is InvoiceState.CANCELED:
            self.store.begin_hold_cancel(hold.quote_id)
            self.store.complete_hold_cancel(hold.quote_id)
        elif status.state is InvoiceState.OPEN:
            if self._now() >= hold.invoice_expires_at:
                self.store.begin_hold_cancel(hold.quote_id)
                await self._cancel(hold)
        elif status.state is InvoiceState.ACCEPTED:
            await self._complete_sale(hold, status)
        else:
            logger.error("Hold invoice settled without a prepared delivery")

    async def _complete_sale(self, hold: HoldInvoice, status: HoldInvoiceStatus) -> None:
        if status.amount_paid_sats < hold.amount_sats:
            await self._refund(hold, "underpaid")
            return
        height = await self.block_height()
        if (
            status.min_htlc_expiry_height is not None
            and height >= status.min_htlc_expiry_height - HTLC_SAFETY_BLOCKS
        ):
            await self._refund(hold, "payment is close to its HTLC expiry")
            return
        deliverable = await self._deliverable(hold)
        if deliverable is None:
            # Unknown, so neither settle nor refund yet; the HTLC deadline
            # above still refunds a check that never succeeds.
            return
        if not deliverable:
            await self._refund(hold, "the credential no longer verifies")
            return
        if not hold.credential_attached and self.credential_provider is not None:
            try:
                await self.credential_provider(hold.quote_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A provider that keeps failing is a missing credential: the
                # refund deadline below still applies.
                logger.warning("Market credential provider failed: {}", type(exc).__name__)
        try:
            self.store.finalize(hold.quote_id, self._seller_key_for(hold), self._now(), height)
        except MarketStoreExpiredError:
            await self._refund(hold, "quote can no longer be delivered")
            return
        except MarketStoreUnavailableError:
            if self._now() >= hold.invoice_expires_at + CREDENTIAL_GRACE_SECONDS:
                await self._refund(hold, "no credential was attached in time")
            return
        await self._settle(hold, checked=True)

    async def _deliverable(self, hold: HoldInvoice) -> bool | None:
        """The sale check's answer, ``None`` when it could not decide."""
        if self.sale_check is None:
            return True
        try:
            return await self.sale_check(hold)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Market sale check failed: {}", type(exc).__name__)
            return None

    async def _refund(self, hold: HoldInvoice, reason: str) -> None:
        logger.warning("Refunding a held market payment: {}", reason)
        self.store.begin_hold_cancel(hold.quote_id)
        await self._cancel(hold)

    async def _settle(self, hold: HoldInvoice, *, checked: bool = False) -> None:
        """Settle a prepared delivery; ``checked`` when the sale check just passed."""
        status = await self.invoices.lookup(hold.payment_hash)
        if status is not None and status.state is InvoiceState.ACCEPTED:
            # A retried settlement may run in a later retarget period, where the
            # prepared package is useless to the buyer: refund instead.
            height = await self.block_height()
            if self.store.abandon_undeliverable_settlement(hold.quote_id, height):
                logger.warning("Refunding a held market payment: package period ended")
                await self._cancel(hold)
                return
            if not checked:
                # A retry may come long after the check: charge only for a
                # credential that still verifies.
                deliverable = await self._deliverable(hold)
                if deliverable is None:
                    if (
                        status.min_htlc_expiry_height is None
                        or height < status.min_htlc_expiry_height - HTLC_SAFETY_BLOCKS
                    ):
                        return
                    reason = "payment is close to its HTLC expiry"
                elif deliverable:
                    reason = None
                else:
                    reason = "the credential no longer verifies"
                if reason is not None:
                    logger.warning("Refunding a held market payment: {}", reason)
                    self.store.abandon_settlement(hold.quote_id)
                    await self._cancel(hold)
                    return
            await self.invoices.settle(hold.preimage)
            status = await self.invoices.lookup(hold.payment_hash)
        if status is None:
            logger.error("Hold invoice being settled is missing from LND")
        elif status.state is InvoiceState.SETTLED:
            self.store.complete_hold_settle(hold.quote_id)
        elif status.state is InvoiceState.CANCELED:
            # The HTLC was canceled before settlement (for example near its
            # expiry). The prepared allocation stays consumed and undelivered.
            logger.error("Held market payment was canceled after its delivery was prepared")
            self.store.complete_hold_cancel(hold.quote_id)

    async def _cancel(self, hold: HoldInvoice) -> None:
        status = await self.invoices.lookup(hold.payment_hash)
        if status is None:
            # The invoice may never have been created, or its creation may still
            # land. Create it now so that canceling it is definitive.
            remaining = hold.invoice_expires_at - self._now()
            try:
                await self.invoices.add_hold_invoice(
                    hold.payment_hash, hold.amount_sats, min(max(remaining, 1), 3600)
                )
            except InvoiceRefusedError:
                # No node knows the invoice and none may create it (its coin
                # left the wallet). Only a never-issued reservation can finish
                # here: nobody received its payment request.
                if self.store.cancel_unpublished_reservation(hold.quote_id):
                    return
                raise
            status = await self.invoices.lookup(hold.payment_hash)
        if status is not None and status.state is InvoiceState.SETTLED:
            logger.error("Hold invoice settled while it was being canceled")
            return
        if status is None or status.state is not InvoiceState.CANCELED:
            await self.invoices.cancel(hold.payment_hash)
            status = await self.invoices.lookup(hold.payment_hash)
        if status is not None and status.state is InvoiceState.CANCELED:
            self.store.complete_hold_cancel(hold.quote_id)
