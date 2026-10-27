"""Maker producers cannot reopen or tear down a seller after admission closes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from jmcore.market_store import HoldInvoice
from taker.wallet_market import WalletMarketSeller
from test_market_sales import _Backend, _coin, _owned_bond, _sales, _Seller

from maker import market_sales
from maker.market_sales import MarketSales


class _RetainedSeller(_Seller):
    def __init__(self) -> None:
        super().__init__()
        self.running = True
        self.accepting = True
        self.close_entered = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()
        self.stop_calls = 0

    async def close_admission(self) -> None:
        self.accepting = False
        self.close_entered.set()
        await self.idle.wait()

    async def stop(self) -> None:
        self.running = False
        self.stop_calls += 1
        self.idle.set()


@pytest.mark.parametrize("retry", [False, True])
async def test_restart_with_lost_required_history_starts_original_hold_monitor_without_a_bond(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retry: bool,
) -> None:
    import time
    from unittest.mock import AsyncMock

    from jmcore.bond_advertisements import (
        advertisement_record_path,
        initialize_fresh_advertisements,
    )
    from jmcore.constants import GENESIS_BLOCK_HASHES
    from jmcore.credential_market import MarketAuthorization, PaymentTerms, sign_document
    from jmcore.external_podle import ExternalPoDLEOutpoint
    from jmcore.market_keys import MarketKeyScope
    from jmcore.market_store import HoldReservation, MarketStore
    from jmcore.models import NetworkType
    from jmcore.paths import get_market_store_path, get_used_commitments_path
    from jmcore.settings import NetworkSettings
    from jmswap.lnd_invoices import HoldInvoiceStatus, InvoiceState
    from taker.market_settlement import MultiNodeHoldInvoices

    sales, bot, _, backend = _sales(tmp_path, price=700)
    sales._settings = sales._settings.model_copy(
        update={
            "network_config": NetworkSettings(network=NetworkType.REGTEST, directory_servers=[]),
        }
    )
    sales._seller = None
    wallet = bot.wallet
    bond = _owned_bond(0xAA)
    assert bond.private_key is not None and bond.pubkey is not None
    outpoint = ExternalPoDLEOutpoint(txid=bond.txid, vout=bond.vout)
    keys = wallet.market_keys.bind(
        MarketKeyScope(
            network="regtest",
            chain_hash=GENESIS_BLOCK_HASHES["regtest"],
            role="seller",
            period=0,
            bond=outpoint,
        )
    )
    from jmcore.credential_market import BondReference

    authority = MarketAuthorization(
        bond=BondReference(
            network="regtest", outpoint=outpoint, pubkey=bond.pubkey.hex(), locktime=bond.locktime
        ),
        period=0,
        seller_pubkey=keys.signing_public_key().hex(),
    )
    authorization = sign_document(authority, bond.private_key)
    history = advertisement_record_path(tmp_path)
    initialize_fresh_advertisements(
        history, wallet.market_wallet_id, "regtest", history_confirmed=True
    )
    now = int(time.time())
    with MarketStore(get_market_store_path(tmp_path), wallet_id=wallet.market_wallet_id) as store:
        store.activate_wallet(
            get_used_commitments_path(tmp_path, create=False),
            history_confirmed=True,
            bound_advertisements_network="regtest",
        )
        store.add_bond_inventory(authority.bond, 0)
        reserved = store.reserve_hold_quote(
            authorization,
            keys,
            "44" * 32,
            "bond",
            keys.signing_public_key().hex(),
            "lightning",
            1000,
            1000,
            now,
            1000,
            request_id="01" * 16,
        )
        assert isinstance(reserved, HoldReservation)
        terms = PaymentTerms(rail="lightning", request="original-hold-request", amount_sats=1000)
        store.issue_hold_quote(
            reserved.quote_id, keys, terms, f"ln:{reserved.payment_hash.hex()}", now
        )
        (hold,) = store.hold_invoices(now)
    history.unlink()
    looked_up = asyncio.Event()
    original_node = AsyncMock()
    original_node.__aenter__.return_value = original_node

    async def lookup(payment_hash: bytes) -> HoldInvoiceStatus | None:
        assert payment_hash == hold.payment_hash
        looked_up.set()
        return HoldInvoiceStatus(InvoiceState.OPEN, terms.request, 0, None)

    original_node.lookup.side_effect = lookup
    invoices = MultiNodeHoldInvoices({"original": original_node}, sales._invoice_node)
    monkeypatch.setattr(sales, "_invoices", lambda: invoices)
    genesis = AsyncMock(
        side_effect=[OSError("temporarily unavailable"), GENESIS_BLOCK_HASHES["regtest"]]
        if retry
        else None,
        return_value=GENESIS_BLOCK_HASHES["regtest"],
    )
    monkeypatch.setattr(backend, "get_block_hash", genesis, raising=False)
    authorize = AsyncMock(side_effect=AssertionError("do not require or create current authority"))
    monkeypatch.setattr(wallet, "create_market_authorization", authorize)
    try:
        if retry:
            monkeypatch.setattr(market_sales, "RETRY_SECONDS", 0.001)
            sales.start()
        else:
            await sales._ensure_seller()
        await asyncio.wait_for(looked_up.wait(), timeout=3)
        if sales._task is not None:
            await asyncio.wait_for(sales._task, timeout=3)
        monitor = sales._seller
        assert monitor is not None and monitor.running and not monitor.accepting
        assert monitor._settlement_task is not None and not monitor._settlement_task.done()
        assert monitor._transport is None and monitor._service is None and sales._admission_closed
        assert monitor._seller_key_for(hold).signing_public_key() == keys.signing_public_key()
        assert invoices._owners[hold.payment_hash] == "original"
        authorize.assert_not_awaited()
        original_node.add_hold_invoice.assert_not_awaited()
        original_node.cancel.assert_not_awaited()
        assert not history.exists()
    finally:
        await sales.stop()
        await wallet.close()


async def test_failed_reconciliation_start_keeps_owner_until_failed_cleanup_can_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from jmcore.bond_advertisements import (
        advertisement_record_path,
        initialize_fresh_advertisements,
    )
    from jmcore.constants import GENESIS_BLOCK_HASHES
    from jmcore.market_store import MarketStore
    from jmcore.models import NetworkType
    from jmcore.paths import get_market_store_path, get_used_commitments_path
    from jmcore.settings import NetworkSettings
    from taker import wallet_market

    sales, bot, _, backend = _sales(tmp_path, price=700)
    sales._seller = None
    sales._settings = sales._settings.model_copy(
        update={
            "network_config": NetworkSettings(network=NetworkType.REGTEST, directory_servers=[]),
        }
    )
    history = advertisement_record_path(tmp_path)
    initialize_fresh_advertisements(
        history, bot.wallet.market_wallet_id, "regtest", history_confirmed=True
    )
    path = get_market_store_path(tmp_path)
    with MarketStore(path, wallet_id=bot.wallet.market_wallet_id) as store:
        store.activate_wallet(
            get_used_commitments_path(tmp_path, create=False),
            history_confirmed=True,
            bound_advertisements_network="regtest",
        )
    history.unlink()
    invoices = AsyncMock()
    invoices.__aenter__.return_value = invoices
    monkeypatch.setattr(sales, "_invoices", lambda: invoices)
    monkeypatch.setattr(
        backend,
        "get_block_hash",
        AsyncMock(
            side_effect=[
                OSError("startup unavailable"),
                GENESIS_BLOCK_HASHES["regtest"],
            ]
        ),
        raising=False,
    )
    close = MarketStore.close
    close_failures = 0

    def failing_close(store: MarketStore) -> None:
        nonlocal close_failures
        if store.path == path and close_failures < 2:
            close_failures += 1
            raise OSError("cleanup not yet confirmed")
        close(store)

    monkeypatch.setattr(MarketStore, "close", failing_close)
    created: list[WalletMarketSeller] = []
    initialize = wallet_market.WalletMarketSeller.__init__

    def track(seller: WalletMarketSeller, *args: Any, **kwargs: Any) -> None:
        initialize(seller, *args, **kwargs)
        created.append(seller)

    monkeypatch.setattr(wallet_market.WalletMarketSeller, "__init__", track)
    try:
        with pytest.raises(OSError, match="cleanup"):
            await sales._ensure_seller()
        owner = sales._seller
        assert owner is not None and owner._store is not None and not owner.running
        assert sales._needs_reconciliation_start() and created == [owner]
        with pytest.raises(OSError, match="cleanup"):
            await sales._ensure_seller()
        assert sales._seller is owner and owner._store is not None and created == [owner]
        await sales._ensure_seller()
        assert owner._store is None and len(created) == 2
        assert sales._seller is created[1] and sales._seller.running and not sales._seller.accepting
    finally:
        await sales.stop()
        await bot.wallet.close()


async def test_terminal_stop_retains_failed_close_for_explicit_retry(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    sales, _, seller, _ = state
    stop = AsyncMock(side_effect=[OSError("close failed"), None])
    monkeypatch.setattr(seller, "stop", stop)
    with pytest.raises(OSError, match="close failed"):
        await sales.stop()
    assert sales._seller is seller
    await sales.stop()
    assert sales._seller is None and stop.await_count == 2


@pytest.mark.parametrize("failure", ["missing", "legacy", "corrupt", "during-height"])
async def test_required_history_failure_closes_admission_but_keeps_original_settlement(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from jmcore.bond_advertisements import (
        advertisement_record_path,
        initialize_fresh_advertisements,
        record_advertisement,
    )
    from jmcore.market_store import MarketStore
    from jmcore.paths import get_market_store_path, get_used_commitments_path

    sales, bot, seller, backend = state
    _coin(bot.wallet, backend, 1, 0)
    await sales.refresh()
    payment_hash = b"\x44" * 32
    seller.sold[payment_hash] = seller.inventory[0]
    directory = bot.wallet.data_dir
    history = advertisement_record_path(directory)
    initialize_fresh_advertisements(
        history, bot.wallet.market_wallet_id, "regtest", history_confirmed=True
    )
    with MarketStore(
        get_market_store_path(directory), wallet_id=bot.wallet.market_wallet_id
    ) as store:
        store.activate_wallet(
            get_used_commitments_path(directory, create=False),
            history_confirmed=True,
            bound_advertisements_network="regtest",
        )
    sales._verify_required_advertisements()
    entered, release = asyncio.Event(), asyncio.Event()
    task: asyncio.Task[None] | None = None
    if failure == "during-height":

        async def height() -> int:
            entered.set()
            await release.wait()
            return 100_000

        monkeypatch.setattr(backend, "get_block_height", height)
        task = asyncio.create_task(sales._ensure_seller())
        await entered.wait()
    try:
        history.unlink()
        if failure == "legacy":
            record_advertisement(history, 0)
        elif failure == "corrupt":
            history.write_bytes(b'{"version":2,')
        release.set()
        if task is not None:
            await task
        else:
            await sales.refresh()
        assert sales._admission_closed and not seller.accepting
        assert sales._seller is seller and seller.running and seller.stop_calls == 0
        assert sales._invoice_node(payment_hash) == "n1"
        assert await sales._sale_check(
            cast(HoldInvoice, SimpleNamespace(payment_hash=payment_hash))
        )
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
async def state(
    tmp_path: Path,
) -> AsyncIterator[tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend]]:
    sales, bot, _, backend = _sales(tmp_path, price=700)
    seller = _RetainedSeller()
    sales._seller = cast(WalletMarketSeller, seller)
    try:
        yield sales, bot, seller, backend
    finally:
        await sales.stop()
        await bot.wallet.close()


async def test_closed_producers_keep_original_routing_and_never_restock_or_replace(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
) -> None:
    sales, bot, seller, backend = state
    _coin(bot.wallet, backend, 1, 0)
    await sales.refresh()
    payment_hash = b"\x44" * 32
    seller.sold[payment_hash] = seller.inventory[0]
    before = list(seller.inventory), list(seller.prices), sales.mixdepth
    await sales.close_admission()
    bot.offer_manager.mixdepth = 0
    bot.fidelity_bond = SimpleNamespace(txid="55" * 32, vout=1, private_key=object())
    bot.config.rent_bonds = True
    await sales._ensure_seller()
    await sales.refresh()
    await sales.run()
    sales.start()
    assert sales._task is None
    assert sales._seller is seller and seller.running and not seller.accepting
    assert seller.stop_calls == 0
    assert (seller.inventory, seller.prices, sales.mixdepth) == before
    assert sales._invoice_node(payment_hash) == "n1"
    hold = cast(HoldInvoice, SimpleNamespace(payment_hash=payment_hash))
    assert await sales._sale_check(hold) is True
    await sales.close_admission()


@pytest.mark.parametrize("blocked", ["producer", "callbacks"])
async def test_canceling_closure_leaves_both_gates_closed_without_stopping_seller(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
    blocked: str,
) -> None:
    sales, _, seller, _ = state
    if blocked == "producer":
        await sales._producer_lock.acquire()
    else:
        seller.idle.clear()
    closing = asyncio.create_task(sales.close_admission())
    try:
        await seller.close_entered.wait()
        await asyncio.sleep(0)
        assert not closing.done() and not seller.accepting
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert sales._admission_closed and not seller.accepting and seller.running
        assert sales._seller is seller and seller.stop_calls == 0
    finally:
        if blocked == "producer":
            sales._producer_lock.release()
        seller.idle.set()
        await asyncio.gather(closing, return_exceptions=True)
    await sales.close_admission()


@pytest.mark.parametrize("error", [False, True])
async def test_closure_racing_height_and_retry_never_stops_original_seller(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
    monkeypatch: pytest.MonkeyPatch,
    error: bool,
) -> None:
    sales, _, seller, backend = state
    entered, release = asyncio.Event(), asyncio.Event()

    async def height() -> int:
        entered.set()
        await release.wait()
        if error:
            raise RuntimeError("synthetic producer failure")
        return 100_000

    monkeypatch.setattr(backend, "get_block_height", height)
    sales.start()
    await entered.wait()
    closing = asyncio.create_task(sales.close_admission())
    try:
        await seller.close_entered.wait()
        release.set()
        await asyncio.wait_for(closing, 2)
        assert sales._task is not None
        await asyncio.wait_for(sales._task, 2)
        assert sales._seller is seller and seller.running and seller.stop_calls == 0
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


async def test_closure_during_backing_check_stops_stocking_without_canceling_check(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from taker import market_service

    sales, bot, seller, backend = state
    _coin(bot.wallet, backend, 1, 0)
    entered, release = asyncio.Event(), asyncio.Event()
    original = market_service.verify_podle_backing

    async def backing(*args: Any) -> None:
        entered.set()
        await release.wait()
        await original(*args)

    monkeypatch.setattr(market_service, "verify_podle_backing", backing)
    refresh = asyncio.create_task(sales.refresh())
    await entered.wait()
    closing = asyncio.create_task(sales.close_admission())
    try:
        await seller.close_entered.wait()
        assert not seller.accepting and not closing.done()
        release.set()
        await asyncio.gather(refresh, closing)
        assert seller.inventory == [] and seller.stop_calls == 0 and seller.running
    finally:
        release.set()
        await asyncio.gather(refresh, closing, return_exceptions=True)


async def test_explicit_stop_can_finish_while_admission_closure_waits_for_callbacks(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
) -> None:
    sales, _, seller, _ = state
    seller.idle.clear()
    closing = asyncio.create_task(sales.close_admission())
    await seller.close_entered.wait()
    await asyncio.wait_for(sales.stop(), 2)
    await asyncio.wait_for(closing, 2)
    assert seller.stop_calls == 1 and not seller.running and sales._seller is None


@pytest.mark.parametrize("phase", ["offer", "price", "lease", "stock-height"])
async def test_refresh_does_not_mutate_or_stock_after_awaiting_closed_producer(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    import taker.podle_purchase as purchase

    sales, bot, seller, backend = state
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(*_args: object) -> int:
        entered.set()
        await release.wait()
        return 1

    if phase == "offer":
        monkeypatch.setattr(bot.offer_manager, "current_offer_mixdepth", pending)
    elif phase == "price":
        bot.config.podle_price_sats = None
        monkeypatch.setattr(purchase, "market_fee_rate", pending)
    elif phase == "lease":
        sales._podle_served = False
        sales._rented = [_owned_bond(0x55)]
        monkeypatch.setattr(backend, "get_block_height", pending)
    else:
        coin = _coin(bot.wallet, backend, 1, 0)
        coin.height = None
        monkeypatch.setattr(backend, "get_block_height", pending)
    refresh = asyncio.create_task(sales.refresh())
    await entered.wait()
    closing = asyncio.create_task(sales.close_admission())
    try:
        await seller.close_entered.wait()
        before = list(seller.prices), sales.mixdepth
        release.set()
        await asyncio.gather(refresh, closing)
        assert seller.inventory == [] and seller.bond_inventory == [] and seller.bond_prices == []
        assert (seller.prices, sales.mixdepth) == before
        assert seller.stop_calls == 0 and seller.running
    finally:
        release.set()
        await asyncio.gather(refresh, closing, return_exceptions=True)


async def test_explicit_stop_cancels_producer_outside_lock_while_closure_is_waiting(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sales, _, seller, backend = state
    entered = asyncio.Event()

    async def height() -> int:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(backend, "get_block_height", height)
    sales.start()
    await entered.wait()
    closing = asyncio.create_task(sales.close_admission())
    await seller.close_entered.wait()
    await asyncio.wait_for(sales.stop(), 2)
    await asyncio.wait_for(closing, 2)
    assert sales._task is None and sales._seller is None and seller.stop_calls == 1


@pytest.mark.parametrize("during", ["fee", "startup", "teardown"])
async def test_closure_during_seller_replacement_never_creates_open_replacement(
    state: tuple[MarketSales, SimpleNamespace, _RetainedSeller, _Backend],
    monkeypatch: pytest.MonkeyPatch,
    during: str,
) -> None:
    import taker.podle_purchase as purchase
    import taker.wallet_market as runtime

    sales, bot, old, _ = state
    sales._seller = None
    bot.config.podle_price_sats = None
    bot.config.channel_ring.nodes = {"n1": object()}
    bot.fidelity_bond = SimpleNamespace(txid="11" * 32, vout=0, private_key=object())
    monkeypatch.setattr(sales, "_require_ledger", lambda _wallet: None)
    monkeypatch.setattr(market_sales, "_invoice_client", lambda _node: object())
    monkeypatch.setattr(market_sales, "_seller_options", lambda *_args: object())
    entered, release = asyncio.Event(), asyncio.Event()
    created: list[_RetainedSeller] = []

    async def fee(_backend: object) -> float:
        if during == "fee":
            entered.set()
            await release.wait()
        return 1.0

    class Starting(_RetainedSeller):
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            super().__init__()
            self.running = False
            created.append(self)

        async def start(self) -> None:
            entered.set()
            await release.wait()
            self.running = True

    if during == "teardown":
        sales._seller = cast(WalletMarketSeller, old)

        async def stopping() -> None:
            entered.set()
            await release.wait()
            old.stop_calls += 1
            old.running = False

        monkeypatch.setattr(old, "stop", stopping)
    monkeypatch.setattr(purchase, "market_fee_rate", fee)
    monkeypatch.setattr(runtime, "WalletMarketSeller", Starting)
    ensuring = asyncio.create_task(sales._ensure_seller())
    await entered.wait()
    closing = asyncio.create_task(sales.close_admission())
    try:
        await asyncio.sleep(0)
        if during == "startup":
            assert len(created) == 1 and not created[0].accepting
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
        release.set()
        await ensuring
        await sales.close_admission()
        if during == "startup":
            assert sales._seller is created[0] and created[0].running
            assert not created[0].accepting and created[0].stop_calls == 0
        else:
            assert created == [] and sales._seller is None
    finally:
        release.set()
        await asyncio.gather(ensuring, closing, return_exceptions=True)
