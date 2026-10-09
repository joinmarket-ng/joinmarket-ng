"""The hold-invoice client against the pinned LND, with a real payer.

Bob pays Alice's hold invoice over a direct channel. The payment stays in
flight until Alice settles it (Bob learns the preimage) or cancels it (Bob's
payment fails and his funds return). This is the exact contract the
credential-market seller relies on.
"""

from __future__ import annotations

import asyncio
import secrets

import pytest
from test_lnd_buyout_regtest import (
    BuyoutRegtest,
    _open_private_taproot_channel,
    _wait_for_sync,
    grpc_endpoint,
    peer_client,
    stack,  # noqa: F401 (pytest fixture)
)

from jmswap.lnd_invoices import HoldInvoiceStatus, LndInvoiceClient, payment_hash_for
from jmswap.lnd_peer import InvoiceState, LndPeerClient, PaymentResult, PaymentStatus

pytestmark = [pytest.mark.docker, pytest.mark.timeout(600)]

INVOICE_MACAROON = "/lnd/data/chain/bitcoin/regtest/invoice.macaroon"
AMOUNT_SATS = 1_000


async def _pay_until_accepted(
    bob: LndPeerClient, alice: LndInvoiceClient, request: str, payment_hash: bytes
) -> tuple[asyncio.Task[PaymentResult], HoldInvoiceStatus]:
    """Start Bob's payment and wait until Alice holds it.

    Right after a channel turns active LND can still report insufficient
    balance until its link is ready, so a payment that fails before being held
    is simply retried.
    """
    async with asyncio.timeout(90):
        while True:
            payment = asyncio.create_task(
                bob.pay(request, fee_limit_sat=10, cltv_limit=144, timeout_seconds=120)
            )
            while not payment.done():
                status = await alice.lookup(payment_hash)
                if status is not None and status.state is InvoiceState.ACCEPTED:
                    return payment, status
                await asyncio.sleep(0.2)
            assert payment.result().status is PaymentStatus.FAILED
            await asyncio.sleep(1.0)


@pytest.fixture(scope="module")
def channel_stack(stack: BuyoutRegtest) -> BuyoutRegtest:  # noqa: F811
    """The shared stack with one Alice to Bob channel, Bob holding the pushed balance."""
    _open_private_taproot_channel(stack)
    # Both nodes must agree on the tip, or the invoice's final CLTV check fails.
    _wait_for_sync(stack, "alice")
    _wait_for_sync(stack, "bob")
    return stack


@pytest.mark.parametrize("settle", [True, False], ids=["settle", "cancel"])
async def test_held_payment_resolves_only_as_the_seller_decides(
    channel_stack: BuyoutRegtest, settle: bool
) -> None:
    regtest = channel_stack
    certificate, _ = regtest.credentials("alice")
    macaroon = regtest.secrets_dir / "alice" / "invoice.macaroon"
    if not macaroon.exists():
        # The scoped default macaroon the seller is told to use.
        regtest.compose("cp", f"alice:{INVOICE_MACAROON}", str(macaroon))
        macaroon.chmod(0o600)

    async with (
        grpc_endpoint(regtest, "alice") as endpoint,
        LndInvoiceClient(endpoint, certificate, macaroon.read_bytes()) as alice,
        peer_client(regtest, "bob") as bob,
    ):
        preimage = secrets.token_bytes(32)
        payment_hash = payment_hash_for(preimage)
        # On a node without any invoice this is LND's UNKNOWN "no existing invoices".
        assert await alice.lookup(payment_hash) is None
        request = await alice.add_hold_invoice(payment_hash, AMOUNT_SATS, 300, private=True)
        # Creating it again is idempotent, as the settlement reconciler needs.
        assert await alice.add_hold_invoice(payment_hash, AMOUNT_SATS, 300, private=True) == (
            request
        )
        payment, status = await _pay_until_accepted(bob, alice, request, payment_hash)
        try:
            assert not payment.done()
            assert status.amount_paid_sats == AMOUNT_SATS
            assert status.min_htlc_expiry_height is not None
            if settle:
                await alice.settle(preimage)
            else:
                await alice.cancel(payment_hash)
            result = await asyncio.wait_for(asyncio.shield(payment), 60)
        finally:
            if not payment.done():
                try:
                    await alice.cancel(payment_hash)
                finally:
                    payment.cancel()
            await asyncio.gather(payment, return_exceptions=True)

        assert result.payment_hash == payment_hash
        if settle:
            assert result.status is PaymentStatus.SUCCEEDED
            assert result.preimage == preimage
        else:
            assert result.status is PaymentStatus.FAILED
            assert result.preimage is None
        final = await alice.lookup(payment_hash)
        assert final is not None
        assert final.state is (InvoiceState.SETTLED if settle else InvoiceState.CANCELED)
