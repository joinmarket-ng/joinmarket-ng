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
