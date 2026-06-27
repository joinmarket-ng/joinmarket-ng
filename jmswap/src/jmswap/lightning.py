"""
Lightning backend abstraction for the swap protocol.

The provider and client only need a handful of Lightning operations:

* receive a normal invoice (a party that knows the preimage),
* receive a **hold** invoice for a client-chosen payment hash (the provider on a
  swap-out, who does not yet know the preimage),
* pay an invoice and learn the preimage on success (the provider on a swap-in),
* settle / cancel a held inbound payment once the preimage is learned on-chain.

These map one-to-one onto LDK Server's ``Bolt11Receive``,
``Bolt11ReceiveForHash``, ``Bolt11Send``, ``Bolt11ClaimForHash`` and
``Bolt11FailForHash`` RPCs (see :mod:`jmswap.ldk`).

A fully functional :class:`FakeLightningBackend` (backed by an in-process
:class:`FakeLightningNetwork`) lets the whole swap protocol be exercised without
real Lightning nodes, including the hold-invoice settle/cancel semantics.
"""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from enum import StrEnum

from jmcore.bitcoin import sha256

from jmswap.errors import LightningError


class PaymentStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InboundStatus(StrEnum):
    UNKNOWN = "unknown"  # invoice not known
    PENDING = "pending"  # invoice issued, not yet paid
    HELD = "held"  # hold invoice paid by the sender, awaiting settle/cancel
    SETTLED = "settled"  # claimed with the preimage
    FAILED = "failed"  # cancelled / failed


@dataclass(frozen=True)
class DecodedInvoice:
    payment_hash: str
    amount_msat: int | None
    description: str | None = None
    expiry: int = 3600


@dataclass(frozen=True)
class PaymentResult:
    payment_id: str
    status: PaymentStatus
    preimage: str | None = None


class LightningBackend(abc.ABC):
    """The Lightning operations required by a swap provider or client."""

    @abc.abstractmethod
    async def node_id(self) -> str:
        """Return this node's public key (hex)."""

    @abc.abstractmethod
    async def create_invoice(
        self, amount_msat: int, description: str = "", expiry: int = 3600
    ) -> tuple[str, str]:
        """Create a normal invoice. Returns ``(invoice, payment_hash)``."""

    @abc.abstractmethod
    async def create_hold_invoice(
        self, amount_msat: int, payment_hash: str, description: str = "", expiry: int = 3600
    ) -> str:
        """Create a hold invoice for ``payment_hash``. Returns the invoice string."""

    @abc.abstractmethod
    async def pay_invoice(self, invoice: str, amount_msat: int | None = None) -> PaymentResult:
        """Pay ``invoice``. On success the result carries the preimage."""

    @abc.abstractmethod
    async def get_payment(self, payment_id: str) -> PaymentResult:
        """Look up an outbound payment by id (to poll for its preimage/status)."""

    @abc.abstractmethod
    async def lookup_inbound(self, payment_hash: str) -> InboundStatus:
        """Status of an inbound (possibly held) payment by hash."""

    @abc.abstractmethod
    async def settle_hold_invoice(self, payment_hash: str, preimage: str) -> None:
        """Settle a held inbound payment by revealing ``preimage``."""

    @abc.abstractmethod
    async def cancel_hold_invoice(self, payment_hash: str) -> None:
        """Cancel/fail a held or pending inbound payment."""

    @abc.abstractmethod
    async def decode_invoice(self, invoice: str) -> DecodedInvoice:
        """Decode an invoice's payment hash, amount and description."""


# --- In-process fake network ------------------------------------------------


@dataclass
class _Invoice:
    payee: str  # node id of the receiver
    amount_msat: int
    payment_hash: str
    is_hold: bool
    preimage: str | None  # known to payee for normal invoices, None for hold
    status: InboundStatus = InboundStatus.PENDING
    settle_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Payment:
    payment_id: str
    payment_hash: str
    status: PaymentStatus
    preimage: str | None = None


def _fake_invoice_str(payment_hash: str, amount_msat: int) -> str:
    return f"lnfake1{payment_hash}{amount_msat:016x}"


def _parse_fake_invoice(invoice: str) -> tuple[str, int]:
    if not invoice.startswith("lnfake1"):
        raise LightningError("not a fake invoice")
    body = invoice[len("lnfake1") :]
    payment_hash = body[:64]
    amount_msat = int(body[64:80], 16)
    return payment_hash, amount_msat


class FakeLightningNetwork:
    """A shared in-process routing fabric for :class:`FakeLightningBackend`s."""

    def __init__(self) -> None:
        self._invoices: dict[str, _Invoice] = {}
        self._payments: dict[str, _Payment] = {}
        self._counter = 0

    def _register(
        self, payee: str, amount_msat: int, payment_hash: str, is_hold: bool, preimage: str | None
    ) -> str:
        if payment_hash in self._invoices:
            raise LightningError("invoice already exists for payment hash")
        self._invoices[payment_hash] = _Invoice(
            payee=payee,
            amount_msat=amount_msat,
            payment_hash=payment_hash,
            is_hold=is_hold,
            preimage=preimage,
        )
        return _fake_invoice_str(payment_hash, amount_msat)

    def inbound_status(self, payment_hash: str) -> InboundStatus:
        inv = self._invoices.get(payment_hash)
        return inv.status if inv else InboundStatus.UNKNOWN

    def settle(self, payment_hash: str, preimage: str) -> None:
        inv = self._invoices.get(payment_hash)
        if inv is None:
            raise LightningError("unknown invoice")
        if sha256(bytes.fromhex(preimage)).hex() != payment_hash:
            raise LightningError("preimage does not match payment hash")
        inv.preimage = preimage
        inv.status = InboundStatus.SETTLED
        inv.settle_event.set()

    def cancel(self, payment_hash: str) -> None:
        inv = self._invoices.get(payment_hash)
        if inv is None:
            raise LightningError("unknown invoice")
        inv.status = InboundStatus.FAILED
        inv.settle_event.set()

    async def pay(self, invoice: str, amount_msat: int | None) -> _Payment:
        payment_hash, inv_amount = _parse_fake_invoice(invoice)
        inv = self._invoices.get(payment_hash)
        if inv is None:
            raise LightningError("no such invoice on the fake network")
        if amount_msat is not None and inv_amount and amount_msat < inv_amount:
            raise LightningError("payment amount below invoice amount")
        self._counter += 1
        payment_id = f"pay-{self._counter:08d}"
        if not inv.is_hold:
            # Normal invoice: settles immediately with the payee's preimage.
            inv.status = InboundStatus.SETTLED
            inv.settle_event.set()
            payment = _Payment(payment_id, payment_hash, PaymentStatus.SUCCEEDED, inv.preimage)
            self._payments[payment_id] = payment
            return payment
        # Hold invoice: mark held, wait for the payee to settle or cancel.
        inv.status = InboundStatus.HELD
        payment = _Payment(payment_id, payment_hash, PaymentStatus.PENDING, None)
        self._payments[payment_id] = payment
        await inv.settle_event.wait()
        if inv.status == InboundStatus.SETTLED:
            payment.status = PaymentStatus.SUCCEEDED
            payment.preimage = inv.preimage
        else:
            payment.status = PaymentStatus.FAILED
        return payment

    def get_payment(self, payment_id: str) -> _Payment:
        payment = self._payments.get(payment_id)
        if payment is None:
            raise LightningError("unknown payment id")
        return payment


class FakeLightningBackend(LightningBackend):
    """An in-process Lightning node attached to a :class:`FakeLightningNetwork`."""

    def __init__(self, network: FakeLightningNetwork, node_id_hex: str | None = None) -> None:
        self._network = network
        self._node_id = node_id_hex or sha256(id(self).to_bytes(8, "big")).hex()[:66]

    async def node_id(self) -> str:
        return self._node_id

    async def create_invoice(
        self, amount_msat: int, description: str = "", expiry: int = 3600
    ) -> tuple[str, str]:
        import secrets

        preimage = secrets.token_bytes(32)
        payment_hash = sha256(preimage).hex()
        invoice = self._network._register(
            self._node_id, amount_msat, payment_hash, is_hold=False, preimage=preimage.hex()
        )
        return invoice, payment_hash

    async def create_hold_invoice(
        self, amount_msat: int, payment_hash: str, description: str = "", expiry: int = 3600
    ) -> str:
        return self._network._register(
            self._node_id, amount_msat, payment_hash, is_hold=True, preimage=None
        )

    async def pay_invoice(self, invoice: str, amount_msat: int | None = None) -> PaymentResult:
        payment = await self._network.pay(invoice, amount_msat)
        return PaymentResult(payment.payment_id, payment.status, payment.preimage)

    async def get_payment(self, payment_id: str) -> PaymentResult:
        payment = self._network.get_payment(payment_id)
        return PaymentResult(payment.payment_id, payment.status, payment.preimage)

    async def lookup_inbound(self, payment_hash: str) -> InboundStatus:
        return self._network.inbound_status(payment_hash)

    async def settle_hold_invoice(self, payment_hash: str, preimage: str) -> None:
        self._network.settle(payment_hash, preimage)

    async def cancel_hold_invoice(self, payment_hash: str) -> None:
        self._network.cancel(payment_hash)

    async def decode_invoice(self, invoice: str) -> DecodedInvoice:
        payment_hash, amount_msat = _parse_fake_invoice(invoice)
        return DecodedInvoice(payment_hash=payment_hash, amount_msat=amount_msat or None)
