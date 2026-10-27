"""Typed async adapter over LND's ``invoicesrpc.Invoices`` hold-invoice API.

The credential market sells each item against one hold invoice: the buyer's
payment is locked in an accepted HTLC until the seller has durably prepared the
delivery, and only then is it settled. If the sale cannot complete, the invoice
is canceled and the buyer is refunded by the network.

This module is a mapping layer only. It holds no market state, never retries and
never decides whether an invoice should be settled or canceled. Like
:mod:`jmswap.lnd_peer`, it keeps credentials private: the macaroon is attached
only to calls addressed to ``invoicesrpc.Invoices`` (so LND's default
``invoice.macaroon`` suffices), the channel is always TLS-authenticated, and
errors carry a gRPC status code only.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Final

import grpc

from jmswap.lnd_peer import InvoiceState, PaymentRouteHop, serialize_route_hints
from jmswap.lndrpc import invoices_pb2 as invoices_pb
from jmswap.lndrpc import invoices_pb2_grpc as invoices_grpc

INVOICES_SERVICE_NAME: Final = "invoicesrpc.Invoices"

_HASH_LENGTH: Final = 32
_MAX_EXPIRY_SECONDS: Final = 3600
_MAX_AMOUNT_SATS: Final = 21_000_000 * 100_000_000
_MSAT_PER_SAT: Final = 1_000


class LndInvoiceError(Exception):
    """Base error of the hold-invoice adapter."""


# LND v0.21 answers a lookup on a node that never created an invoice with this
# error and status UNKNOWN (LookupInvoiceV2 only maps ErrInvoiceNotFound to
# NOT_FOUND), so a new seller node would otherwise fail its first lookups.
_NO_INVOICES_DETAIL = "there are no existing invoices"


class LndInvoiceRpcError(LndInvoiceError):
    """An Invoices RPC failed; only the status code is reported."""

    def __init__(
        self, method: str, code: grpc.StatusCode | None, *, not_found: bool = False
    ) -> None:
        self.method = method
        self.code = code
        # The node reported that the invoice does not exist. Only this boolean is
        # derived from LND's error detail; the detail itself is never kept.
        self.not_found = not_found or code == grpc.StatusCode.NOT_FOUND
        name = code.name if code is not None else "UNKNOWN"
        super().__init__(f"LND {method} failed with status {name}")


class LndInvoiceResponseError(LndInvoiceError):
    """The node answered with data that does not match the request."""


@dataclass(frozen=True)
class HoldInvoiceStatus:
    """What the seller needs to decide about one hold invoice."""

    state: InvoiceState
    payment_request: str
    amount_paid_sats: int
    min_htlc_expiry_height: int | None
    """Lowest expiry height of the accepted HTLCs, if any are held."""


class _InvoicesMacaroon(grpc.AuthMetadataPlugin):
    def __init__(self, macaroon: bytes) -> None:
        self._metadata: tuple[tuple[str, str], ...] = (("macaroon", macaroon.hex()),)

    def __call__(
        self, context: grpc.AuthMetadataContext, callback: grpc.AuthMetadataPluginCallback
    ) -> None:
        if not str(context.service_url).endswith(f"/{INVOICES_SERVICE_NAME}"):
            callback((), ValueError("macaroon is scoped to the Invoices service"))
            return
        callback(self._metadata, None)

    def __repr__(self) -> str:
        return "<invoice macaroon credentials>"


def _require_hash(value: bytes, name: str) -> bytes:
    if type(value) is not bytes or len(value) != _HASH_LENGTH:
        raise ValueError(f"{name} must be exactly {_HASH_LENGTH} bytes")
    return value


def validate_hold_invoice_request(
    payment_hash: bytes, amount_sats: int, expiry_seconds: int
) -> None:
    """Apply the same request bounds when creating or recovering an invoice."""
    _require_hash(payment_hash, "payment_hash")
    if type(amount_sats) is not int or not 1 <= amount_sats <= _MAX_AMOUNT_SATS:
        raise ValueError("amount_sats is out of range")
    if type(expiry_seconds) is not int or not 1 <= expiry_seconds <= _MAX_EXPIRY_SECONDS:
        raise ValueError("expiry_seconds is out of range")


class LndInvoiceClient:
    """Async hold-invoice client for one LND node; use as an async context manager."""

    def __init__(
        self,
        endpoint: str,
        tls_certificate: bytes,
        macaroon: bytes,
        timeout: float = 30.0,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("endpoint must be a non-empty host:port string")
        if type(tls_certificate) is not bytes or not tls_certificate:
            raise ValueError("tls_certificate must be non-empty PEM bytes")
        if type(macaroon) is not bytes or not macaroon:
            raise ValueError("macaroon must be non-empty bytes")
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        self._endpoint = endpoint
        self._channel_credentials = grpc.ssl_channel_credentials(tls_certificate)
        self._call_credentials = grpc.metadata_call_credentials(_InvoicesMacaroon(macaroon))
        self._timeout = float(timeout)
        self._channel: grpc.aio.Channel | None = None
        self._invoices: Any = None

    def __repr__(self) -> str:
        return f"LndInvoiceClient(endpoint={self._endpoint!r}, timeout={self._timeout!r})"

    async def __aenter__(self) -> LndInvoiceClient:
        if self._channel is None:
            self._channel = grpc.aio.secure_channel(self._endpoint, self._channel_credentials)
            self._invoices = invoices_grpc.InvoicesStub(self._channel)  # type: ignore[no-untyped-call]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        channel, self._channel = self._channel, None
        self._invoices = None
        if channel is not None:
            await channel.close()

    async def _call(self, method: str, request: Any) -> Any:
        if self._invoices is None:
            raise LndInvoiceError("client is not connected; use it as an async context manager")
        try:
            return await getattr(self._invoices, method)(
                request, timeout=self._timeout, credentials=self._call_credentials
            )
        except grpc.aio.AioRpcError as exc:
            no_invoices = exc.code() == grpc.StatusCode.UNKNOWN and (
                exc.details() == _NO_INVOICES_DETAIL
            )
            raise LndInvoiceRpcError(method, exc.code(), not_found=no_invoices) from None

    async def add_hold_invoice(
        self,
        payment_hash: bytes,
        amount_sats: int,
        expiry_seconds: int,
        *,
        private: bool = False,
        invoice_route_hints: tuple[tuple[PaymentRouteHop, ...], ...] = (),
    ) -> str:
        """Create a memo-less hold invoice and return its BOLT11 request.

        Re-adding an existing hash is not an error: the existing invoice's
        request is returned, so a retry after an ambiguous failure always ends
        with one invoice that this seller controls.
        """
        validate_hold_invoice_request(payment_hash, amount_sats, expiry_seconds)
        hints = serialize_route_hints(invoice_route_hints, name="invoice_route_hints")
        try:
            response = await self._call(
                "AddHoldInvoice",
                invoices_pb.AddHoldInvoiceRequest(
                    hash=payment_hash,
                    value=amount_sats,
                    expiry=expiry_seconds,
                    private=private,
                    route_hints=hints,
                ),
            )
        except LndInvoiceRpcError:
            status = await self.lookup(payment_hash)
            if status is None:
                raise
            return status.payment_request
        request = str(getattr(response, "payment_request", ""))
        if not request.startswith("ln"):
            raise LndInvoiceResponseError("LND returned no payment request")
        return request

    async def lookup(self, payment_hash: bytes) -> HoldInvoiceStatus | None:
        """Return the invoice state, or ``None`` when LND reports it unknown.

        ``None`` only means this node does not know the hash right now. It is
        never proof that a payment cannot arrive.
        """
        _require_hash(payment_hash, "payment_hash")
        try:
            invoice = await self._call(
                "LookupInvoiceV2", invoices_pb.LookupInvoiceMsg(payment_hash=payment_hash)
            )
        except LndInvoiceRpcError as exc:
            if exc.not_found:
                return None
            raise
        if bytes(invoice.r_hash) != payment_hash:
            raise LndInvoiceResponseError("LND returned a different invoice")
        try:
            state = InvoiceState(int(invoice.state))
        except ValueError:
            raise LndInvoiceResponseError("LND returned an unknown invoice state") from None
        accepted = [htlc for htlc in invoice.htlcs if int(htlc.state) == 0]
        return HoldInvoiceStatus(
            state=state,
            payment_request=str(invoice.payment_request),
            amount_paid_sats=int(invoice.amt_paid_msat) // _MSAT_PER_SAT,
            min_htlc_expiry_height=(
                min(int(htlc.expiry_height) for htlc in accepted) if accepted else None
            ),
        )

    async def settle(self, preimage: bytes) -> None:
        """Release the preimage for an accepted hold invoice."""
        _require_hash(preimage, "preimage")
        await self._call("SettleInvoice", invoices_pb.SettleInvoiceMsg(preimage=preimage))

    async def cancel(self, payment_hash: bytes) -> None:
        """Cancel an open or accepted hold invoice, refunding held HTLCs."""
        _require_hash(payment_hash, "payment_hash")
        await self._call("CancelInvoice", invoices_pb.CancelInvoiceMsg(payment_hash=payment_hash))


def payment_hash_for(preimage: bytes) -> bytes:
    """Return the payment hash committed to by ``preimage``."""
    return hashlib.sha256(_require_hash(preimage, "preimage")).digest()
