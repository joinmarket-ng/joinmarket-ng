"""Tests for the Lightning backend abstraction and fake network."""

from __future__ import annotations

import asyncio

import pytest
from jmcore.bitcoin import sha256

from jmswap.errors import LightningError
from jmswap.lightning import (
    FakeLightningBackend,
    FakeLightningNetwork,
    InboundStatus,
    PaymentStatus,
)


async def test_normal_invoice_pays_immediately() -> None:
    net = FakeLightningNetwork()
    alice = FakeLightningBackend(net, "aa" * 33)
    bob = FakeLightningBackend(net, "bb" * 33)
    invoice, payment_hash = await alice.create_invoice(21_000, "coffee")
    result = await bob.pay_invoice(invoice)
    assert result.status == PaymentStatus.SUCCEEDED
    assert result.preimage is not None
    assert sha256(bytes.fromhex(result.preimage)).hex() == payment_hash
    assert await alice.lookup_inbound(payment_hash) == InboundStatus.SETTLED


async def test_hold_invoice_settle_releases_payment() -> None:
    """A hold-invoice payment blocks until the payee settles with the preimage."""
    net = FakeLightningNetwork()
    provider = FakeLightningBackend(net, "cc" * 33)
    client = FakeLightningBackend(net, "dd" * 33)

    preimage = b"\x11" * 32
    payment_hash = sha256(preimage).hex()
    invoice = await provider.create_hold_invoice(50_000, payment_hash)

    pay_task = asyncio.create_task(client.pay_invoice(invoice))
    await asyncio.sleep(0)  # let the payment register as held
    assert await provider.lookup_inbound(payment_hash) == InboundStatus.HELD
    assert not pay_task.done()

    await provider.settle_hold_invoice(payment_hash, preimage.hex())
    result = await pay_task
    assert result.status == PaymentStatus.SUCCEEDED
    assert result.preimage == preimage.hex()


async def test_hold_invoice_cancel_fails_payment() -> None:
    net = FakeLightningNetwork()
    provider = FakeLightningBackend(net)
    client = FakeLightningBackend(net)
    preimage = b"\x22" * 32
    payment_hash = sha256(preimage).hex()
    invoice = await provider.create_hold_invoice(50_000, payment_hash)
    pay_task = asyncio.create_task(client.pay_invoice(invoice))
    await asyncio.sleep(0)
    await provider.cancel_hold_invoice(payment_hash)
    result = await pay_task
    assert result.status == PaymentStatus.FAILED
    assert result.preimage is None


async def test_settle_rejects_wrong_preimage() -> None:
    net = FakeLightningNetwork()
    provider = FakeLightningBackend(net)
    payment_hash = sha256(b"\x33" * 32).hex()
    await provider.create_hold_invoice(10_000, payment_hash)
    with pytest.raises(LightningError):
        await provider.settle_hold_invoice(payment_hash, ("99" * 32))


async def test_decode_invoice_roundtrip() -> None:
    net = FakeLightningNetwork()
    node = FakeLightningBackend(net)
    invoice, payment_hash = await node.create_invoice(123_456)
    decoded = await node.decode_invoice(invoice)
    assert decoded.payment_hash == payment_hash
    assert decoded.amount_msat == 123_456


async def test_get_payment_lookup() -> None:
    net = FakeLightningNetwork()
    a = FakeLightningBackend(net)
    b = FakeLightningBackend(net)
    invoice, _ = await a.create_invoice(1000)
    res = await b.pay_invoice(invoice)
    again = await b.get_payment(res.payment_id)
    assert again.preimage == res.preimage
    assert again.status == PaymentStatus.SUCCEEDED
