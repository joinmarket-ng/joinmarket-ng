"""Closing callback admission retains original settlement and existing quote delivery."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from bitcointx.core.key import CKey
from jmcore.constants import GENESIS_BLOCK_HASHES
from jmcore.credential_market import (
    MarketAuthorization,
    MarketError,
    SignedDocument,
    canonical,
    sign_document,
)
from jmcore.crypto import NickIdentity
from jmcore.external_podle import ExternalPoDLE, ExternalPoDLEOutpoint
from jmcore.market_keys import BoundMarketKeys, MarketKeyScope, WalletMarketKeys
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

    async def __aenter__(self) -> _Invoices:
        self.closed = False
        return self

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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[WalletMarketSeller, WalletService, _Invoices]]:
    wallet, backend = _wallet(tmp_path)
    mode = getattr(request, "param", None)
    bound_history = mode in {"bound", "bound-bond"}
    if bound_history:
        from jmcore.bond_advertisements import (
            advertisement_record_path,
            initialize_fresh_advertisements,
        )
        from jmcore.paths import get_market_store_path, get_used_commitments_path

        initialize_fresh_advertisements(
            advertisement_record_path(tmp_path),
            wallet.market_wallet_id,
            "regtest",
            history_confirmed=True,
        )
        with MarketStore(
            get_market_store_path(tmp_path), wallet_id=wallet.market_wallet_id
        ) as store:
            store.activate_wallet(
                get_used_commitments_path(tmp_path, create=False),
                history_confirmed=True,
                bound_advertisements_network="regtest",
            )
    else:
        _activate_store(tmp_path, wallet)

    async def authorize(outpoint: ExternalPoDLEOutpoint) -> tuple[BoundMarketKeys, SignedDocument]:
        bond = _bond().model_copy(update={"outpoint": outpoint})
        bound = wallet.market_keys.bind(
            MarketKeyScope(
                network="regtest",
                chain_hash=GENESIS_BLOCK_HASHES["regtest"] if bound_history else "22" * 32,
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
    monkeypatch.setattr(
        backend,
        "get_block_hash",
        AsyncMock(return_value=GENESIS_BLOCK_HASHES["regtest"]),
        raising=False,
    )
    monkeypatch.setattr(wallet_market, "MarketTransport", _Transport)
    monkeypatch.setattr(_Transport, "instances", [])
    monkeypatch.setattr(_Transport, "start_error", None)
    monkeypatch.setattr(_Transport, "close_error", None)
    original_run = MarketSettlement.run

    async def fast_run(settlement: MarketSettlement) -> None:
        await original_run(settlement, interval=0.001)

    monkeypatch.setattr(MarketSettlement, "run", fast_run)
    invoices = _Invoices()
    options = WalletMarketSellerOptions(
        bond=_bond().outpoint,
        products=["bond" if mode == "bound-bond" else "podle"],
        price_sats=1000,
    )
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


@pytest.mark.parametrize("runtime", ["bound"], indirect=True)
async def test_required_history_loss_does_not_block_original_hold_settlement_and_delivery(
    runtime: tuple[WalletMarketSeller, WalletService, _Invoices],
) -> None:
    from jmcore.bond_advertisements import advertisement_record_path

    seller, wallet, invoices = runtime
    seller.add_podle_inventory(ExternalPoDLE.model_validate(_podle_credential(0x41)))
    assert "quote" in await seller._respond_callback("buyer", _request())
    store, settlement, keys = seller._store, seller._settlement, seller._keys
    assert store is not None and settlement is not None and wallet.data_dir is not None
    (hold,) = store.hold_invoices(int(time.time()))
    advertisement_record_path(wallet.data_dir).unlink()
    with MarketStore(store.path, wallet_id=wallet.market_wallet_id, create=False) as restarted:
        assert restarted.hold_invoices(int(time.time()))[0].quote_id == hold.quote_id
    await seller.close_admission()
    invoices.pay(hold.payment_hash, 1000)
    await asyncio.wait_for(invoices.settled.wait(), timeout=2)
    assert store.get_delivery(hold.quote_id) is not None
    assert seller._store is store and seller._settlement is settlement and seller._keys is keys
    assert not invoices.closed and seller.running and not seller.accepting
    assert "delivery" in await seller._respond_callback(
        "buyer",
        {
            "action": "delivery",
            "quote_id": hold.quote_id,
        },
    )


@pytest.mark.parametrize("runtime", ["bound"], indirect=True)
@pytest.mark.parametrize("outcome", ["settled", "earlier-period", "wrong-key"])
async def test_closed_monitor_restart_recovers_original_quote_scope_and_invoice_node(
    runtime: tuple[WalletMarketSeller, WalletService, _Invoices],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    from jmcore.bond_advertisements import advertisement_record_path
    from jmswap.lnd_invoices import InvoiceState

    from taker.market_settlement import CREDENTIAL_GRACE_SECONDS, MultiNodeHoldInvoices

    original, wallet, invoices = runtime
    original.add_podle_inventory(ExternalPoDLE.model_validate(_podle_credential(0x41)))
    assert "quote" in await original._respond_callback("buyer", _request())
    assert original._store is not None and wallet.data_dir is not None
    (hold,) = original._store.hold_invoices(int(time.time()))
    transport_count = len(_Transport.instances)
    await original.stop()
    advertisement_record_path(wallet.data_dir).unlink()
    authorize = AsyncMock(
        side_effect=AssertionError("original bond is unavailable; never reauthorize")
    )
    monkeypatch.setattr(wallet, "create_market_authorization", authorize)
    if outcome == "earlier-period":
        monkeypatch.setattr(wallet.backend, "get_block_height", AsyncMock(return_value=2017))
    elif outcome == "wrong-key":
        bind = WalletMarketKeys.bind

        def wrong_identity(keys: WalletMarketKeys, scope: MarketKeyScope) -> BoundMarketKeys:
            return bind(keys, scope.model_copy(update={"period": scope.period + 1}))

        monkeypatch.setattr(WalletMarketKeys, "bind", wrong_identity)
    other_node = _Invoices()
    owners = MultiNodeHoldInvoices({"n0": other_node, "n1": invoices}, lambda _hash: None)
    restarted = WalletMarketSeller(
        wallet,
        _settings(wallet.data_dir),
        None,
        invoices=owners,
        sale_check=AsyncMock(return_value=True),
    )
    invoices.looked_up.clear()
    try:
        await restarted.start()
        assert restarted.running and not restarted.accepting
        assert restarted._transport is None and restarted._service is None
        assert len(_Transport.instances) == transport_count
        authorize.assert_not_awaited()
        await asyncio.wait_for(invoices.looked_up.wait(), timeout=2)
        assert owners._owners[hold.payment_hash] == "n1"
        assert restarted._settlement_task is not None and not restarted._settlement_task.done()
        assert restarted._settlement is not None and restarted._store is not None
        if outcome == "earlier-period":
            recovered = restarted._seller_key_for(hold)
            assert recovered.scope.period == 0
            assert len(restarted._store.hold_invoices(int(time.time()))) == 1
        else:
            invoices.pay(hold.payment_hash, 1000)
            if outcome == "wrong-key":
                monkeypatch.setattr(
                    restarted._settlement,
                    "clock",
                    lambda: hold.invoice_expires_at + CREDENTIAL_GRACE_SECONDS + 1,
                )
                await restarted._settlement.reconcile_once()
                assert len(restarted._store.hold_invoices(int(time.time()))) == 1
                assert invoices.invoices[hold.payment_hash].state is InvoiceState.ACCEPTED
                assert ("cancel", hold.payment_hash) not in invoices.calls
                assert restarted._store.get_delivery(hold.quote_id) is None
            else:
                await asyncio.wait_for(invoices.settled.wait(), timeout=2)
                assert restarted._store.get_delivery(hold.quote_id) is not None
                assert not other_node.invoices
                assert ("settle", hold.payment_hash) in invoices.calls
        assert not advertisement_record_path(wallet.data_dir).exists()
    finally:
        await restarted.stop()


@pytest.mark.parametrize("runtime", ["bound-bond"], indirect=True)
async def test_reconciliation_signing_requires_the_same_nonterminal_bond_hold_after_await(
    runtime: tuple[WalletMarketSeller, WalletService, _Invoices],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jmcore.bond_advertisements import advertisement_record_path

    original, wallet, invoices = runtime
    original.add_bond_inventory()
    request = {
        **_request(),
        "product": "bond",
        "certificate_pubkey": bytes(CKey(b"\x66" * 32).pub).hex(),
    }
    assert "quote" in await original._respond_callback("buyer", request)
    assert original._store is not None and wallet.data_dir is not None
    (hold,) = original._store.hold_invoices(int(time.time()))
    await original.stop()
    advertisement_record_path(wallet.data_dir).unlink()
    entered, release = asyncio.Event(), asyncio.Event()
    returned = AsyncMock()

    async def sign(*_args: object) -> object:
        entered.set()
        await release.wait()
        return returned

    signer = AsyncMock(side_effect=sign)
    monkeypatch.setattr(wallet, "create_market_bond_credential", signer)
    restarted = WalletMarketSeller(wallet, _settings(wallet.data_dir), None, invoices=invoices)
    task: asyncio.Task[None] | None = None
    try:
        await restarted.start()
        assert restarted._store is not None
        attach = Mock(wraps=restarted._store.attach_credential)
        monkeypatch.setattr(restarted._store, "attach_credential", attach)
        task = asyncio.create_task(restarted._provide_reconciliation_credential(hold.quote_id))
        await asyncio.wait_for(entered.wait(), timeout=2)
        restarted._store.begin_hold_cancel(hold.quote_id)
        release.set()
        with pytest.raises(MarketError, match="nonterminal|changed"):
            await task
        returned.model_dump.assert_not_called()
        attach.assert_not_called()
        assert restarted._store.get_delivery(hold.quote_id) is None
        signer.reset_mock()
        with pytest.raises(MarketError, match="nonterminal"):
            await restarted._provide_reconciliation_credential("ee" * 32)
        signer.assert_not_awaited()
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await restarted.stop()


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
