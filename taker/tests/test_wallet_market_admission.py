"""Closing callback admission retains original settlement and existing quote delivery."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey
from jmcore.credential_market import (
    MarketAuthorization,
    MarketError,
    SignedDocument,
    canonical,
    sign_document,
)
from jmcore.crypto import NickIdentity
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.market_keys import BoundMarketKeys, MarketKeyScope
from jmcore.market_store import MarketStore, MarketStoreError
from jmswap.lnd_invoices import HoldInvoiceStatus
from jmwallet.wallet.service import WalletService
from nacl.public import PrivateKey, SealedBox
from test_market_store import _podle_credential
from test_wallet_market_runtime import (
    _activate_store,
    _BlockingCallbacks,
    _bond,
    _bound_authorization,
    _NullInvoices,
    _settings,
    _Transport,
    _wallet,
)

from taker import wallet_market
from taker.market_settlement import MarketSettlement
from taker.market_transport import MarketTransport, _Inbound
from taker.wallet_market import WalletMarketSeller, WalletMarketSellerOptions


class _Invoices(_NullInvoices):
    def __init__(self) -> None:
        super().__init__()
        self.looked_up = asyncio.Event()
        self.settled = asyncio.Event()
        self.closed = False

    async def lookup(self, payment_hash: bytes) -> HoldInvoiceStatus | None:
        self.looked_up.set()
        return await super().lookup(payment_hash)

    async def settle(self, preimage: bytes) -> None:
        await super().settle(preimage)
        self.settled.set()

    async def __aexit__(self, *_exc: object) -> None:
        self.closed = True


@pytest.fixture
async def runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[WalletMarketSeller, WalletService, _Invoices]]:
    wallet, backend = _wallet(tmp_path)
    _activate_store(tmp_path, wallet)

    async def authorize(outpoint: ExternalPoDLEOutpoint) -> tuple[BoundMarketKeys, SignedDocument]:
        bond = _bond().model_copy(update={"outpoint": outpoint})
        bound = wallet.market_keys.bind(
            MarketKeyScope(
                network="regtest",
                chain_hash="22" * 32,
                role="seller",
                period=0,
                bond=outpoint,
            )
        )
        return bound, sign_document(
            MarketAuthorization(
                bond=bond,
                period=0,
                seller_pubkey=bound.signing_public_key().hex(),
            ),
            CKey(b"\x02" * 32),
        )

    monkeypatch.setattr(wallet, "create_market_authorization", authorize)
    monkeypatch.setattr(backend, "get_block_height", AsyncMock(return_value=1), raising=False)
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    monkeypatch.setattr(_Transport, "instances", [])
    monkeypatch.setattr(_Transport, "start_error", None)
    monkeypatch.setattr(_Transport, "close_error", None)
    original_run = MarketSettlement.run

    async def fast_run(settlement: MarketSettlement) -> None:
        await original_run(settlement, interval=0.001)

    monkeypatch.setattr(MarketSettlement, "run", fast_run)
    invoices = _Invoices()
    options = WalletMarketSellerOptions(bond=_bond().outpoint, products=["podle"], price_sats=1000)
    extra = options.model_copy(update={"bond": ExternalPoDLEOutpoint(txid="33" * 32, vout=1)})
    seller = WalletMarketSeller(
        wallet, _settings(tmp_path), options, invoices=invoices, additional=[extra]
    )
    try:
        await seller.start()
        assert seller._service is not None
        monkeypatch.setattr(seller._service, "_check_stake", AsyncMock(return_value=1))
        yield seller, wallet, invoices
    finally:
        await seller.stop()
        await wallet.close()


@pytest.mark.parametrize("additional", [False, True])
async def test_close_waits_for_admitted_callbacks_without_canceling_them(
    runtime: tuple[WalletMarketSeller, WalletService, _Invoices],
    additional: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seller, _, _ = runtime
    blocker = _BlockingCallbacks()
    service = seller._additional[0].service if additional else seller._service
    assert service is not None
    monkeypatch.setattr(service, "listing", blocker.listing)
    monkeypatch.setattr(service, "respond", blocker.respond)
    listing = asyncio.create_task(seller._listing_for(service))
    response = asyncio.create_task(seller._respond_for(service, "buyer", {"action": "quote"}))
    await blocker.listing_entered.wait()
    await blocker.respond_entered.wait()
    closed = asyncio.create_task(seller.close_admission())
    await asyncio.sleep(0)
    try:
        assert not seller.accepting and seller.running and not closed.done()
        with pytest.raises(MarketError):
            await seller._listing_for(service)
        assert await seller._respond_for(service, "buyer", {"action": "quote"}) == {
            "error": "unavailable"
        }
        closed.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closed
        assert not listing.cancelled() and not response.cancelled()
        assert not seller.accepting
        blocker.release.set()
        assert await listing == b"listing"
        assert await response == {"quote": "published-before-stop"}
        await seller.close_admission()
        assert seller._settlement_task is not None and not seller._settlement_task.done()
    finally:
        blocker.release.set()
        await asyncio.gather(listing, response, return_exceptions=True)


def _request(number: int = 1) -> dict[str, object]:
    return {
        "action": "quote",
        "request_id": f"{number:032x}",
        "buyer_pubkey": bytes(PrivateKey(b"\x44" * 32).public_key).hex(),
        "product": "podle",
        "rail": "lightning",
        "max_price_sats": 1000,
    }


@pytest.mark.parametrize("unknown", ["unreachable", "missing"])
async def test_closed_admission_keeps_settlement_unknowns_delivery_and_ownership(
    runtime: tuple[WalletMarketSeller, WalletService, _Invoices],
    unknown: str,
) -> None:
    seller, wallet, invoices = runtime
    seller.add_podle_inventory(ExternalPoDLE.model_validate(_podle_credential(0x41)))
    response = await seller._respond_callback("buyer", _request())
    assert "quote" in response
    store, settlement = seller._store, seller._settlement
    assert store is not None and settlement is not None
    (hold,) = store.hold_invoices(int(time.time()))
    original = invoices.invoices[hold.payment_hash]
    if unknown == "unreachable":
        invoices.fail.add("lookup")
    else:
        del invoices.invoices[hold.payment_hash]
    invoices.looked_up.clear()
    await seller.close_admission()
    await asyncio.wait_for(invoices.looked_up.wait(), timeout=2)
    assert store.get_delivery(hold.quote_id) is None
    assert len(store.hold_invoices(int(time.time()))) == 1
    assert seller._store is store and seller._settlement is settlement and not invoices.closed
    assert seller._keys is not None and seller._keys.signing_public_key()
    with MarketStore(store.path, wallet_id=wallet.market_wallet_id, create=False) as other:
        with pytest.raises(MarketStoreError, match="another seller runtime"):
            with other.seller_runtime():
                pass
    invoices.fail.clear()
    invoices.invoices[hold.payment_hash] = original
    invoices.pay(hold.payment_hash, 1000)
    await asyncio.wait_for(invoices.settled.wait(), timeout=2)
    assert store.get_delivery(hold.quote_id) is not None
    assert "delivery" in await seller._respond_callback(
        "buyer",
        {
            "action": "delivery",
            "quote_id": hold.quote_id,
        },
    )
    assert await seller._respond_callback("buyer", _request(2)) == {"error": "unavailable"}
    await seller.stop()
    assert invoices.closed and not seller.running


async def test_transport_can_replay_existing_quote_but_cannot_allocate_after_closure(
    runtime: tuple[WalletMarketSeller, WalletService, _Invoices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seller, _, invoices = runtime
    seller.add_podle_inventory(ExternalPoDLE.model_validate(_podle_credential(0x41)))
    provider, reply = PrivateKey(b"\x55" * 32), PrivateKey(b"\x66" * 32)
    transport = MarketTransport(
        directory_servers=[],
        network="regtest",
        nick_identity=NickIdentity(),
        encryption_private_key=provider,
        responder=seller._respond_callback,
    )
    sent = AsyncMock()
    monkeypatch.setattr(transport, "_reply", sent)

    def encrypted(number: int) -> str:
        plain = canonical(
            {
                "request_id": f"{number:032x}",
                "reply_pubkey": base64.b64encode(bytes(reply.public_key)).decode(),
                "body": _request(number),
            }
        )
        return base64.b64encode(SealedBox(provider.public_key).encrypt(plain)).decode()

    inbound = _Inbound(message_type=0, line="")
    try:
        await transport._handle_request("buyer", encrypted(1), inbound)
        assert sent.await_count == 1
        assert sent.await_args is not None
        first = sent.await_args.args[1]
        assert "quote" in json.loads(SealedBox(reply).decrypt(base64.b64decode(first)))["body"]
        await seller.close_admission()
        await transport._handle_request("buyer", encrypted(1), inbound)
        assert sent.await_args.args[1] == first
        await transport._handle_request("buyer", encrypted(2), inbound)
        last = sent.await_args.args[1]
        assert json.loads(SealedBox(reply).decrypt(base64.b64decode(last)))["body"] == {
            "error": "unavailable"
        }
        assert sum(method == "add" for method, _ in invoices.calls) == 1
        assert seller._store is not None and len(seller._store.hold_invoices(int(time.time()))) == 1
    finally:
        await transport.close()


async def test_close_waits_for_accepted_invoice_rpc_and_does_not_issue_again(
    runtime: tuple[WalletMarketSeller, WalletService, _Invoices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seller, _, invoices = runtime
    seller.add_podle_inventory(ExternalPoDLE.model_validate(_podle_credential(0x41)))
    entered, release = asyncio.Event(), asyncio.Event()
    original = invoices.add_hold_invoice

    async def pending_invoice(payment_hash: bytes, amount_sats: int, expiry_seconds: int) -> str:
        result = await original(payment_hash, amount_sats, expiry_seconds)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(invoices, "add_hold_invoice", pending_invoice)
    response = asyncio.create_task(seller._respond_callback("buyer", _request()))
    await entered.wait()
    closing = asyncio.create_task(seller.close_admission())
    await asyncio.sleep(0)
    try:
        assert not closing.done() and not seller.accepting
        assert await seller._respond_callback("buyer", _request(2)) == {"error": "unavailable"}
        release.set()
        assert "quote" in await response
        await closing
        assert sum(method == "add" for method, _ in invoices.calls) == 1
    finally:
        release.set()
        await asyncio.gather(response, closing, return_exceptions=True)


async def test_canceled_invoice_callback_is_not_evidence_that_its_obligation_disappeared(
    runtime: tuple[WalletMarketSeller, WalletService, _Invoices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seller, _, invoices = runtime
    seller.add_podle_inventory(ExternalPoDLE.model_validate(_podle_credential(0x41)))
    entered = asyncio.Event()
    original = invoices.add_hold_invoice

    async def uncertain_invoice(payment_hash: bytes, amount_sats: int, expiry_seconds: int) -> str:
        await original(payment_hash, amount_sats, expiry_seconds)
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(invoices, "add_hold_invoice", uncertain_invoice)
    response = asyncio.create_task(seller._respond_callback("buyer", _request()))
    try:
        await entered.wait()
        response.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response
        await seller.close_admission()
        assert seller._store is not None
        (hold,) = seller._store.hold_invoices(int(time.time()))
        assert hold.state == "reserved" and hold.payment_hash in invoices.invoices
        assert seller._settlement_task is not None and not seller._settlement_task.done()
    finally:
        response.cancel()
        await asyncio.gather(response, return_exceptions=True)


@pytest.mark.parametrize("when", ["before", "during"])
async def test_admission_closure_is_sticky_across_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    when: str,
) -> None:
    wallet, _ = _wallet(tmp_path)
    _activate_store(tmp_path, wallet)
    bound, authorization = _bound_authorization(wallet)
    entered, release = asyncio.Event(), asyncio.Event()

    async def authorize(_outpoint: ExternalPoDLEOutpoint) -> tuple[BoundMarketKeys, SignedDocument]:
        entered.set()
        await release.wait()
        return bound, authorization

    monkeypatch.setattr(wallet, "create_market_authorization", authorize)
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    seller = WalletMarketSeller(
        wallet,
        _settings(tmp_path),
        WalletMarketSellerOptions(
            bond=_bond().outpoint,
            products=["podle"],
            price_sats=1000,
        ),
        invoices=_Invoices(),
    )
    if when == "before":
        await seller.close_admission()
    starting = asyncio.create_task(seller.start())
    try:
        await entered.wait()
        if when == "during":
            await seller.close_admission()
        release.set()
        await starting
        assert seller.running and not seller.accepting
        assert await seller._respond_callback("buyer", _request()) == {"error": "unavailable"}
        with pytest.raises(MarketError):
            await seller._listing_callback()
    finally:
        release.set()
        await asyncio.gather(starting, return_exceptions=True)
        await seller.stop()
        await wallet.close()
