"""Resource-limit tests for watcher bond verification."""

from __future__ import annotations

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock

import pytest
from jmcore.models import FidelityBond, OrderBook

from orderbook_watcher.aggregator import (
    BOND_CACHE_MAX_SIZE,
    BOND_RETRY_CACHE_MAX_SIZE,
    BOND_RETRY_CACHE_TTL_SECONDS,
    MAX_MEMPOOL_VERIFICATION_CLAIMS_PER_UPDATE,
    MEMPOOL_VERIFICATION_CONCURRENCY,
    OrderbookAggregator,
)

TEST_PUBKEY_HEX = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
OTHER_PUBKEY_HEX = "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
LOCKTIME = 2_000_000_000


def _bond(
    index: int,
    *,
    bond_value: int | None = None,
    pubkey: str = TEST_PUBKEY_HEX,
) -> FidelityBond:
    txid = f"{index:064x}"
    return FidelityBond(
        counterparty=f"Maker{index}",
        utxo_txid=txid,
        utxo_vout=0,
        bond_value=bond_value,
        locktime=LOCKTIME,
        amount=1_000_000,
        script=pubkey,
        utxo_confirmations=10,
        utxo_confirmation_timestamp=int(time.time()) - 600,
        cert_expiry=901_152,
        fidelity_bond_data={"utxo_pub": pubkey},
    )


def _aggregator() -> OrderbookAggregator:
    aggregator = OrderbookAggregator(directory_nodes=[], network="mainnet", mempool_api_url="")
    aggregator.mempool_api = AsyncMock()
    return aggregator


def test_positive_cache_is_bounded_without_refreshing_verification_time() -> None:
    aggregator = _aggregator()

    for index in range(BOND_CACHE_MAX_SIZE + 1):
        bond = _bond(index, bond_value=1)
        bond.verification_valid = True
        cache_key = aggregator._bond_claim_key(bond)
        aggregator._bond_claims_reverified.add(cache_key)
        aggregator._update_bond_cache(OrderBook(fidelity_bonds=[bond]))

    first_key = aggregator._bond_claim_key(_bond(0))
    cached_key = aggregator._bond_claim_key(_bond(1))
    cached_bond, verified_at = aggregator._bond_cache[cached_key]
    cached_bond.verification_valid = True
    aggregator._apply_bond_cache(OrderBook(fidelity_bonds=[_bond(1)]))

    assert len(aggregator._bond_cache) == BOND_CACHE_MAX_SIZE
    assert first_key not in aggregator._bond_cache
    assert aggregator._bond_cache[cached_key][1] == verified_at


def test_retry_cache_is_bounded() -> None:
    aggregator = _aggregator()

    for index in range(BOND_RETRY_CACHE_MAX_SIZE + 1):
        aggregator._record_bond_retry(_bond(index), False)

    assert len(aggregator._bond_retry_cache) == BOND_RETRY_CACHE_MAX_SIZE
    assert aggregator._bond_claim_key(_bond(0)) not in aggregator._bond_retry_cache


@pytest.mark.asyncio
async def test_unconfirmed_claim_retry_expires_and_distinct_claim_is_not_suppressed() -> None:
    aggregator = _aggregator()
    assert aggregator.mempool_api is not None
    aggregator.mempool_api.get_transaction.return_value = None
    first = _bond(1)

    await aggregator._calculate_bond_values_via_mempool(OrderBook(fidelity_bonds=[first]))

    cache_key = aggregator._bond_claim_key(first)
    assert aggregator.mempool_api.get_transaction.await_count == 1
    assert aggregator._bond_retry_cache[cache_key][0] is False

    repeated = _bond(1)
    await aggregator._calculate_bond_values_via_mempool(OrderBook(fidelity_bonds=[repeated]))

    assert aggregator.mempool_api.get_transaction.await_count == 1
    assert repeated.verification_valid is False

    corrected_claim = _bond(1, pubkey=OTHER_PUBKEY_HEX)
    await aggregator._calculate_bond_values_via_mempool(OrderBook(fidelity_bonds=[corrected_claim]))

    assert aggregator.mempool_api.get_transaction.await_count == 2

    result, recorded_at = aggregator._bond_retry_cache[cache_key]
    aggregator._bond_retry_cache[cache_key] = (
        result,
        recorded_at - BOND_RETRY_CACHE_TTL_SECONDS,
    )
    await aggregator._calculate_bond_values_via_mempool(OrderBook(fidelity_bonds=[_bond(1)]))

    assert aggregator.mempool_api.get_transaction.await_count == 3


@pytest.mark.asyncio
async def test_mempool_errors_use_retry_cooldown() -> None:
    aggregator = _aggregator()
    assert aggregator.mempool_api is not None
    aggregator.mempool_api.get_transaction.side_effect = RuntimeError("mempool unavailable")
    first = _bond(2)

    await aggregator._calculate_bond_values_via_mempool(OrderBook(fidelity_bonds=[first]))

    cache_key = aggregator._bond_claim_key(first)
    assert aggregator.mempool_api.get_transaction.await_count == 1
    assert aggregator._bond_retry_cache[cache_key][0] is None

    await aggregator._calculate_bond_values_via_mempool(OrderBook(fidelity_bonds=[_bond(2)]))

    assert aggregator.mempool_api.get_transaction.await_count == 1

    result, recorded_at = aggregator._bond_retry_cache[cache_key]
    aggregator._bond_retry_cache[cache_key] = (
        result,
        recorded_at - BOND_RETRY_CACHE_TTL_SECONDS,
    )
    await aggregator._calculate_bond_values_via_mempool(OrderBook(fidelity_bonds=[_bond(2)]))

    assert aggregator.mempool_api.get_transaction.await_count == 2


@pytest.mark.asyncio
async def test_mempool_jobs_are_bounded_deduplicated_and_prioritize_stale_claims() -> None:
    aggregator = _aggregator()
    assert aggregator.mempool_api is not None
    requested_txids: list[str] = []
    active_requests = 0
    peak_active_requests = 0

    async def get_transaction(txid: str) -> None:
        nonlocal active_requests, peak_active_requests
        requested_txids.append(txid)
        active_requests += 1
        peak_active_requests = max(peak_active_requests, active_requests)
        await asyncio.sleep(0)
        active_requests -= 1

    aggregator.mempool_api.get_transaction.side_effect = get_transaction
    fresh_bonds = [_bond(index) for index in range(10, 310)]
    duplicate = _bond(10)
    stale = _bond(999, bond_value=1)
    stale.verification_valid = True
    stale.verification_stale = True

    await aggregator._calculate_bond_values_via_mempool(
        OrderBook(fidelity_bonds=[fresh_bonds[0], duplicate, *fresh_bonds[1:], stale])
    )

    assert len(requested_txids) == MAX_MEMPOOL_VERIFICATION_CLAIMS_PER_UPDATE
    assert requested_txids[0] == stale.utxo_txid
    assert requested_txids.count(fresh_bonds[0].utxo_txid) == 1
    assert peak_active_requests == MEMPOOL_VERIFICATION_CONCURRENCY


@pytest.mark.asyncio
async def test_background_bond_queue_retains_latest_snapshot() -> None:
    aggregator = _aggregator()
    initial = OrderBook()
    replaced = OrderBook()
    latest = OrderBook()
    initial_started = asyncio.Event()
    latest_processed = asyncio.Event()
    release_initial = asyncio.Event()
    processed: list[OrderBook] = []

    async def calculate(orderbook: OrderBook) -> None:
        processed.append(orderbook)
        if orderbook is initial:
            initial_started.set()
            await release_initial.wait()
        elif orderbook is latest:
            latest_processed.set()

    aggregator._calculate_bond_values = calculate
    worker = asyncio.create_task(aggregator._background_bond_calculator())
    try:
        await aggregator._bond_queue.put(initial)
        await initial_started.wait()
        await aggregator._bond_queue.put(replaced)
        await aggregator._bond_queue.put(latest)

        assert aggregator._bond_queue.maxsize == 1
        assert aggregator._bond_queue.qsize() == 1

        release_initial.set()
        await asyncio.wait_for(latest_processed.wait(), timeout=1)
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker

    assert processed == [initial, latest]
