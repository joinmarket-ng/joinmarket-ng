"""The hold-invoice client against the pinned LND, with a real payer.

Bob pays Alice's hold invoice over a direct channel. The payment stays in
flight until Alice settles it (Bob learns the preimage) or cancels it (Bob's
payment fails and his funds return). This is the exact contract the
credential-market seller relies on.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from jmcore.channel_ring import RingNodeBinding
from test_lnd_buyout_regtest import (
    BuyoutRegtest,
    _open_private_taproot_channel,
    _wait_for_sync,
    grpc_endpoint,
    peer_client,
    stack,  # noqa: F401 (pytest fixture)
)

from jmswap.buyout_messages import Outpoint
from jmswap.lnd_invoices import HoldInvoiceStatus, LndInvoiceClient, payment_hash_for
from jmswap.lnd_peer import InvoiceState, LndPeerClient, PaymentResult, PaymentStatus
from jmswap.ring_invoice_hints import incoming_ring_route_hints

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


@pytest.fixture(scope="module")
def private_routing_stack(channel_stack: BuyoutRegtest) -> tuple[BuyoutRegtest, Outpoint]:
    """Alice can reach Carol only through Bob and two unannounced native channels."""
    regtest = channel_stack
    regtest.lncli("bob", "connect", f"{regtest.node_ids['carol']}@carol:9735")
    point = _open_private_taproot_channel(regtest, "bob", "carol", enroll=False)
    node_directory = regtest.secrets_dir / "carol"
    node_directory.mkdir(mode=0o700)
    for source, name in (
        ("/lnd/tls.cert", "tls.cert"),
        ("/lnd/data/chain/bitcoin/regtest/admin.macaroon", "admin.macaroon"),
        (INVOICE_MACAROON, "invoice.macaroon"),
    ):
        path = node_directory / name
        regtest.compose("cp", f"carol:{source}", str(path))
        path.chmod(0o600)
    for node in ("alice", "bob", "carol"):
        _wait_for_sync(regtest, node)
    return regtest, point


@asynccontextmanager
async def _routing_clients(
    regtest: BuyoutRegtest,
) -> AsyncIterator[tuple[LndPeerClient, LndPeerClient, LndInvoiceClient]]:
    directory = regtest.secrets_dir / "carol"
    certificate = (directory / "tls.cert").read_bytes()
    async with (
        peer_client(regtest, "alice") as payer,
        grpc_endpoint(regtest, "carol") as endpoint,
        LndPeerClient(
            endpoint, certificate, (directory / "admin.macaroon").read_bytes()
        ) as receiver,
        LndInvoiceClient(
            endpoint, certificate, (directory / "invoice.macaroon").read_bytes()
        ) as invoices,
    ):
        yield payer, receiver, invoices


@pytest.mark.parametrize("hold", [False, True], ids=["ordinary", "hold"])
async def test_explicit_incoming_hint_routes_through_an_unadvertised_peer(
    private_routing_stack: tuple[BuyoutRegtest, Outpoint],
    hold: bool,
) -> None:
    regtest, point = private_routing_stack
    binding = RingNodeBinding(
        network="regtest",
        wallet_identity="aa" * 32,
        source_mixdepth=0,
        node_name="carol",
        local_node_id=regtest.node_ids["carol"],
    )
    async with _routing_clients(regtest) as (payer, receiver, invoices):
        # Every edge in this disposable stack is private. LND's automatic hint
        # selection omits Bob because he is not publicly advertised.
        assert all(channel.private for channel in await receiver.channels())
        plain = await receiver.create_invoice(
            secrets.token_bytes(32), AMOUNT_SATS, 300, include_private_routes=True
        )
        assert regtest.lncli("carol", "decodepayreq", plain)["route_hints"] == []
        async with asyncio.timeout(90):
            while True:
                hints = await incoming_ring_route_hints(
                    receiver,
                    binding,
                    {point: regtest.node_ids["bob"]},
                    AMOUNT_SATS,
                )
                if hints:
                    break
                await asyncio.sleep(0.2)
        preimage = secrets.token_bytes(32)
        payment_hash = payment_hash_for(preimage)
        if hold:
            request = await invoices.add_hold_invoice(
                payment_hash, AMOUNT_SATS, 300, private=True, invoice_route_hints=hints
            )
            # Recovery keeps the original hint-bearing invoice even when no
            # new explicit paths are supplied on the repeated hash.
            assert await invoices.add_hold_invoice(payment_hash, AMOUNT_SATS, 300) == request
        else:
            request = await receiver.create_invoice(
                preimage, AMOUNT_SATS, 300, include_private_routes=True, invoice_route_hints=hints
            )
        decoded = regtest.lncli("carol", "decodepayreq", request)
        assert len(decoded["route_hints"]) == 1
        hop = decoded["route_hints"][0]["hop_hints"][0]
        assert hop["node_id"] == regtest.node_ids["bob"]
        assert int(hop["chan_id"]) == hints[0][0].chan_id
        payment = asyncio.create_task(
            payer.pay(request, fee_limit_sat=10, cltv_limit=2016, timeout_seconds=90)
        )
        try:
            if hold:
                async with asyncio.timeout(60):
                    while True:
                        status = await invoices.lookup(payment_hash)
                        if status is not None and status.state is InvoiceState.ACCEPTED:
                            break
                        if payment.done():
                            pytest.fail(
                                "private multihop payment ended before its hold was accepted"
                            )
                        await asyncio.sleep(0.2)
                await invoices.settle(preimage)
            result = await asyncio.wait_for(asyncio.shield(payment), 120)
            assert result.status is PaymentStatus.SUCCEEDED and result.preimage == preimage
            original = await payer.track_payment(payment_hash)
            assert original is not None and original.status is PaymentStatus.SUCCEEDED
            assert original.preimage == preimage
            status = await invoices.lookup(payment_hash)
            assert status is not None and status.state is InvoiceState.SETTLED
        finally:
            if not payment.done():
                await invoices.cancel(payment_hash)
                payment.cancel()
            await asyncio.gather(payment, return_exceptions=True)
