"""Hold-invoice settlement reconciler tests against a real store and a fake LND."""

from __future__ import annotations

import pytest
from _taker_market_helpers import FakeHoldInvoices
from jmcore.credential_market import PaymentTerms
from jmcore.market_keys import WalletMarketKeys
from jmcore.market_store import HoldReservation, MarketStore
from jmswap.lnd_peer import InvoiceState
from test_market_store import _authorization, _buyer_key, _key, _podle_credential, _request_id
from test_wallet_market_service import SEED, _activate_store, _bond, _scope
from test_wallet_market_service import _authorization as _bound_authorization

from taker.market_settlement import (
    CREDENTIAL_GRACE_SECONDS,
    HTLC_SAFETY_BLOCKS,
    RESERVATION_GRACE_SECONDS,
    MarketSettlement,
)

NOW = 1_000
SELLER = _key(0x22)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class Seller:
    """One store, one fake LND node, and a reconciler on a controllable clock."""

    def __init__(self, store: MarketStore) -> None:
        self.store = store
        self.invoices = FakeHoldInvoices()
        self.clock = Clock()
        self.settlement = MarketSettlement(
            store, self.invoices, SELLER, self._height, clock=self.clock
        )

    async def _height(self) -> int:
        return self.invoices.height

    def reserve(self, number: int = 1) -> HoldReservation:
        credential = _podle_credential(0x40 + number)
        self.store.add_inventory("podle", str(credential["commitment"]), credential)
        reservation = self.store.reserve_hold_quote(
            _authorization(),
            SELLER,
            bytes(_buyer_key(0x33).public_key).hex(),
            "podle",
            None,
            "lightning",
            10_000,
            1_000,
            self.clock.now,
            1,
            ttl=300,
            request_id=_request_id(number),
        )
        assert isinstance(reservation, HoldReservation)
        return reservation

    async def quote(self, number: int = 1) -> HoldReservation:
        """Reserve, create the hold invoice, and publish the quote, as the service does."""
        reservation = self.reserve(number)
        request = await self.invoices.add_hold_invoice(
            reservation.payment_hash, reservation.amount_sats, 420
        )
        terms = PaymentTerms(rail="lightning", request=request, amount_sats=1_000)
        self.store.issue_hold_quote(
            reservation.quote_id,
            SELLER,
            terms,
            f"ln:{reservation.payment_hash.hex()}",
            self.clock.now,
        )
        return reservation

    def state(self, reservation: HoldReservation) -> InvoiceState:
        return self.invoices.invoices[reservation.payment_hash].state

    def holds(self) -> list[str]:
        return [hold.state for hold in self.store.hold_invoices(self.clock.now)]


@pytest.fixture
def seller(tmp_path):
    store = MarketStore(tmp_path / "market.sqlite")
    yield Seller(store)
    store.close()


async def test_accepted_payment_settles_after_delivery_is_prepared(seller: Seller) -> None:
    reservation = await seller.quote()
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.OPEN
    assert seller.store.get_delivery(reservation.quote_id) is None

    seller.invoices.pay(reservation.payment_hash, 1_000)
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.SETTLED
    assert seller.store.get_delivery(reservation.quote_id) is not None
    assert seller.holds() == []


async def test_failed_settle_call_is_retried_on_the_next_pass(seller: Seller) -> None:
    reservation = await seller.quote()
    seller.invoices.pay(reservation.payment_hash, 1_000)
    seller.invoices.fail.add("settle")
    await seller.settlement.reconcile_once()
    # The delivery is prepared and withheld; cancellation is no longer possible.
    assert seller.holds() == ["settling"]
    assert seller.store.get_delivery(reservation.quote_id) is None

    seller.invoices.fail.clear()
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.SETTLED
    assert seller.store.get_delivery(reservation.quote_id) is not None


@pytest.mark.parametrize(
    ("amount", "htlc_expiry"),
    [(999, 1_100), (1_000, 1_000 + HTLC_SAFETY_BLOCKS)],
    ids=["underpaid", "near-htlc-expiry"],
)
async def test_unsafe_payment_is_refunded_and_inventory_returns(
    seller: Seller, amount: int, htlc_expiry: int
) -> None:
    reservation = await seller.quote()
    seller.invoices.pay(reservation.payment_hash, amount, htlc_expiry)
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.CANCELED
    assert seller.store.get_package(reservation.quote_id) is None
    assert seller.holds() == []
    # The same opening is for sale again.
    seller.reserve(2)


async def test_unpaid_invoice_is_canceled_only_after_it_expires(seller: Seller) -> None:
    reservation = await seller.quote()
    seller.clock.now = reservation.quote_expires_at
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.OPEN
    assert seller.holds() == ["open"]

    seller.clock.now = reservation.invoice_expires_at
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.CANCELED
    assert seller.holds() == []


async def test_payment_accepted_just_before_quote_expiry_still_completes(seller: Seller) -> None:
    reservation = await seller.quote()
    seller.invoices.pay(reservation.payment_hash, 1_000)
    seller.clock.now = reservation.quote_expires_at + 30
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.SETTLED
    assert seller.store.get_delivery(reservation.quote_id) is not None


async def test_abandoned_reservation_gets_a_definitive_cancel(seller: Seller) -> None:
    reservation = seller.reserve()
    await seller.settlement.reconcile_once()
    assert seller.holds() == ["reserved"]
    assert reservation.payment_hash not in seller.invoices.invoices

    seller.clock.now = NOW + RESERVATION_GRACE_SECONDS
    await seller.settlement.reconcile_once()
    # The invoice is created (idempotently) and canceled so a late creation cannot be paid.
    assert seller.state(reservation) is InvoiceState.CANCELED
    assert seller.holds() == []
    seller.reserve(2)


async def test_failed_cancel_call_keeps_inventory_until_lnd_confirms(seller: Seller) -> None:
    reservation = await seller.quote()
    seller.clock.now = reservation.invoice_expires_at
    seller.invoices.fail.add("cancel")
    await seller.settlement.reconcile_once()
    assert seller.holds() == ["canceling"]
    with pytest.raises(Exception, match="inventory"):
        seller.reserve(1)

    seller.invoices.fail.clear()
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.CANCELED
    assert seller.holds() == []


async def test_externally_canceled_invoice_releases_its_reservation(seller: Seller) -> None:
    reservation = await seller.quote()
    await seller.invoices.cancel(reservation.payment_hash)
    await seller.settlement.reconcile_once()
    assert seller.holds() == []


async def test_missing_invoice_of_a_published_quote_keeps_the_reservation(
    seller: Seller,
) -> None:
    reservation = await seller.quote()
    del seller.invoices.invoices[reservation.payment_hash]
    seller.clock.now = reservation.invoice_expires_at + 1
    await seller.settlement.reconcile_once()
    assert seller.holds() == ["open"]
    assert ("cancel", reservation.payment_hash) not in seller.invoices.calls


async def test_accepted_payment_waits_for_a_missing_credential_then_refunds(
    seller: Seller,
) -> None:
    reservation = await seller.quote()
    seller.store._connection.execute(  # noqa: SLF001
        "UPDATE quotes SET pending_credential = NULL WHERE quote_id = ?",
        (reservation.quote_id,),
    )
    seller.invoices.pay(reservation.payment_hash, 1_000, 5_000)
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.ACCEPTED
    assert seller.holds() == ["open"]

    seller.clock.now = reservation.invoice_expires_at + CREDENTIAL_GRACE_SECONDS
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.CANCELED
    assert seller.holds() == []


async def test_credential_provider_runs_for_an_accepted_quote_without_credential(
    seller: Seller,
) -> None:
    reservation = await seller.quote()
    credential = seller.store._connection.execute(  # noqa: SLF001
        "SELECT pending_credential FROM quotes WHERE quote_id = ?", (reservation.quote_id,)
    ).fetchone()[0]
    seller.store._connection.execute(  # noqa: SLF001
        "UPDATE quotes SET pending_credential = NULL WHERE quote_id = ?",
        (reservation.quote_id,),
    )
    provided: list[str] = []

    async def provide(quote_id: str) -> None:
        provided.append(quote_id)
        seller.store._connection.execute(  # noqa: SLF001
            "UPDATE quotes SET pending_credential = ? WHERE quote_id = ?", (credential, quote_id)
        )

    seller.settlement.credential_provider = provide
    await seller.settlement.reconcile_once()
    assert provided == []
    seller.invoices.pay(reservation.payment_hash, 1_000)
    await seller.settlement.reconcile_once()
    assert provided == [reservation.quote_id]
    assert seller.state(reservation) is InvoiceState.SETTLED


def test_market_config_section_reaches_the_seller_invoice_client(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[market] in config.toml is what the seller connects to; unset refuses to sell."""
    from jmcore.credential_market import MarketError
    from jmcore.settings import get_settings, reset_settings

    from taker.market_settlement import seller_invoice_client

    (tmp_path / "tls.cert").write_bytes(b"certificate")
    (tmp_path / "invoice.macaroon").write_bytes(b"macaroon")
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path))
    reset_settings()
    try:
        with pytest.raises(MarketError, match=r"\[market\] lnd_grpc_url"):
            seller_invoice_client(get_settings())
        (tmp_path / "config.toml").write_text(
            "[market]\n"
            'lnd_grpc_url = "127.0.0.1:10019"\n'
            f'lnd_tls_cert_path = "{tmp_path / "tls.cert"}"\n'
            f'lnd_macaroon_path = "{tmp_path / "invoice.macaroon"}"\n'
        )
        reset_settings()
        client = seller_invoice_client(get_settings())
        assert client._endpoint == "127.0.0.1:10019"  # noqa: SLF001
    finally:
        reset_settings()


async def test_settlement_retried_in_a_later_period_refunds_and_keeps_inventory(
    seller: Seller,
) -> None:
    """A package bound to an ended retarget period is never charged for."""
    reservation = await seller.quote()
    seller.invoices.pay(reservation.payment_hash, 1_000, 10_000)
    seller.invoices.fail.add("settle")
    await seller.settlement.reconcile_once()
    assert seller.holds() == ["settling"]

    seller.invoices.fail.clear()
    seller.invoices.height = 2_017  # first block of the next retarget period
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.CANCELED
    assert seller.store.get_package(reservation.quote_id) is None
    assert seller.holds() == []
    # The prepared allocation stays consumed rather than being sold again.
    with pytest.raises(Exception, match="inventory"):
        seller.reserve(1)


async def test_failing_credential_provider_still_refunds_after_the_grace(
    seller: Seller,
) -> None:
    reservation = await seller.quote()
    seller.store._connection.execute(  # noqa: SLF001
        "UPDATE quotes SET pending_credential = NULL WHERE quote_id = ?",
        (reservation.quote_id,),
    )

    async def provide(_quote_id: str) -> None:
        raise RuntimeError("authority changed")

    seller.settlement.credential_provider = provide
    seller.invoices.pay(reservation.payment_hash, 1_000, 5_000)
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.ACCEPTED

    seller.clock.now = reservation.invoice_expires_at + CREDENTIAL_GRACE_SECONDS
    await seller.settlement.reconcile_once()
    assert seller.state(reservation) is InvoiceState.CANCELED
    assert seller.holds() == []


async def test_restarted_wallet_seller_refunds_an_accepted_old_period_quote(tmp_path) -> None:
    keys = WalletMarketKeys(SEED, "regtest")
    bond = _bond()
    bound = keys.bind(_scope(bond))
    path = tmp_path / "market.sqlite"
    store = _activate_store(path, keys.ledger_identity())
    invoices = FakeHoldInvoices()
    credential = _podle_credential(0x41)
    store.add_inventory("podle", str(credential["commitment"]), credential)
    reservation = store.reserve_hold_quote(
        _bound_authorization(bound, bond),
        bound,
        bytes(_buyer_key(0x33).public_key).hex(),
        "podle",
        None,
        "lightning",
        10_000,
        1_000,
        NOW,
        1,
        request_id=_request_id(1),
    )
    assert isinstance(reservation, HoldReservation)
    invoice = await invoices.add_hold_invoice(reservation.payment_hash, 1_000, 420)
    store.issue_hold_quote(
        reservation.quote_id,
        bound,
        PaymentTerms(rail="lightning", request=invoice, amount_sats=1_000),
        f"ln:{reservation.payment_hash.hex()}",
        NOW,
    )
    invoices.pay(reservation.payment_hash, 1_000, 10_000)
    store.close()

    # Startup derives a new seller key for the new period, but must still
    # reconcile the previous period's accepted invoices without signing them.
    with MarketStore(path, wallet_id=keys.ledger_identity(), create=False) as reopened:
        seller = Seller(reopened)
        seller.invoices = invoices
        invoices.height = 2_017
        seller.settlement = MarketSettlement(
            reopened,
            invoices,
            keys.bind(_scope(bond, period=1)),
            seller._height,
            clock=seller.clock,
        )
        await seller.settlement.reconcile_once()
        assert seller.state(reservation) is InvoiceState.CANCELED
        assert reopened.get_package(reservation.quote_id) is None
        assert seller.holds() == []
