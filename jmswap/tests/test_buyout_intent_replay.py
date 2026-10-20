"""Crash recovery replays affirmative intent, never missing-state guesses."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock

import grpc
import pytest
import test_buyout_signing as signing_tests
from test_buyout_recovery import _recovery
from test_buyout_signing import BUYER_KEY, SettlementRuntime, settlement_status

from jmswap.buyout_settlement import BuyoutSettlement, SettlementPolicy
from jmswap.buyout_store import BuyoutStore
from jmswap.lnd_peer import (
    InvoiceState,
    InvoiceStatus,
    LndPeerRpcError,
    PaymentResult,
    PaymentStatus,
)

runtime = signing_tests.runtime
settlement = signing_tests.settlement


async def interrupt_payment(h: SettlementRuntime) -> None:
    async def crash(*args: Any, **kwargs: Any) -> None:
        record = h.signing.buyer.store.get(h.sid)
        assert record.data["payment_started"] is True
        assert record.data["payment_intent"]["bolt11"] == args[0]
        raise TimeoutError("crash before dispatch")

    h.signing.buyer.peer.pay.side_effect = crash
    with pytest.raises(TimeoutError):
        await h.buyer.poll(h.sid)
    h.signing.buyer.peer.pay.reset_mock(side_effect=True)
    h.signing.buyer.peer.pay.return_value = PaymentResult(
        PaymentStatus.SUCCEEDED, h.invoice.payment_hash, h.preimage, 0
    )
    h.signing.buyer.peer.track_payment.return_value = None


async def test_reopened_payment_replays_exact_invoice_and_original_policy(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    await interrupt_payment(h)
    original = h.signing.buyer.store.get(h.sid).data["payment_intent"]
    path = h.signing.buyer.store._path
    h.signing.buyer.store.close()
    with BuyoutStore(path) as reopened:
        request = AsyncMock(side_effect=TimeoutError("counterparty offline"))
        buyer = BuyoutSettlement(
            reopened,
            h.signing.buyer.peer,
            h.chain,
            SettlementPolicy(payment_fee_limit_sat=999_999, payment_timeout_seconds=100),
            request=request,
        )
        assert await buyer.poll(h.sid) == "SPEND_BROADCAST"
        assert reopened.get(h.sid).data["payment_intent"] == original
        assert reopened.get(h.sid).data["settlement_preimage"] == h.preimage.hex()
        h.signing.buyer.peer.pay.assert_awaited_once_with(
            original["bolt11"],
            fee_limit_sat=1_000,
            cltv_limit=360,
            timeout_seconds=60,
        )
        # A cooperative sweep request is allowed after payment; a fresh invoice
        # request is not. The buyer can still claim when the peer stays offline.
        request.assert_awaited_once()
        assert request.await_args is not None
        assert request.await_args.args[1].type == "buyout_sweep"


@pytest.mark.parametrize("status", list(PaymentStatus))
async def test_known_payment_is_observed_not_replayed(
    settlement: SettlementRuntime,
    status: PaymentStatus,
) -> None:
    h = settlement
    await interrupt_payment(h)
    h.signing.buyer.peer.track_payment.return_value = PaymentResult(
        status,
        h.invoice.payment_hash,
        h.preimage if status is PaymentStatus.SUCCEEDED else None,
        0,
    )
    await h.buyer.poll(h.sid)
    h.signing.buyer.peer.pay.assert_not_awaited()
    if status is PaymentStatus.FAILED:
        # Deleting terminal history later must not reauthorize payment.
        h.signing.buyer.peer.track_payment.return_value = None
        await h.buyer.poll(h.sid)
        h.signing.buyer.peer.pay.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["absent", "partial", "unknown", "boolean", "unbound"])
async def test_incomplete_payment_intent_remains_lookup_only(
    settlement: SettlementRuntime,
    mutation: str,
) -> None:
    h = settlement
    await interrupt_payment(h)
    store = h.signing.buyer.store
    record = store.get(h.sid)
    data = dict(record.data)
    intent = dict(data["payment_intent"])
    if mutation == "absent":
        data.pop("payment_intent")
    else:
        if mutation == "partial":
            intent.pop("fee_limit_sat")
        elif mutation == "unknown":
            intent["version"] = 2
        elif mutation == "boolean":
            intent["timeout_seconds"] = True
        else:
            intent["bolt11"] += "another-invoice"
        data["payment_intent"] = intent
    store.update(record, state=record.state, data=data)
    await h.buyer.poll(h.sid)
    h.signing.buyer.peer.track_payment.assert_awaited()
    h.signing.buyer.peer.pay.assert_not_awaited()
    assert store.get(h.sid).data == data


@pytest.mark.parametrize("state", ["PAYMENT_STARTED", "PAYMENT_RECOVERY_REQUIRED", "FUTURE_STATE"])
async def test_missing_payment_markers_do_not_authorize_fresh_work(
    settlement: SettlementRuntime,
    state: str,
) -> None:
    h = settlement
    store = h.signing.buyer.store
    record = store.get(h.sid)
    store.update(record, state=state, data=record.data)
    h.signing.buyer.peer.track_payment.return_value = None
    request = AsyncMock(side_effect=h.request)
    h.buyer.request = request
    await h.buyer.poll(h.sid)
    request.assert_not_awaited()
    h.signing.buyer.peer.track_payment.assert_awaited_once_with(h.invoice.payment_hash)
    h.signing.buyer.peer.pay.assert_not_awaited()
    # A parent reorg must not erase uncertainty and make the next poll pristine.
    parent = h.transactions.pop(h.parent.txid)
    await h.buyer.poll(h.sid)
    h.transactions[h.parent.txid] = parent
    await h.buyer.poll(h.sid)
    request.assert_not_awaited()
    h.chain.broadcast.assert_not_awaited()
    # FAILED without the original dispatch markers cannot distinguish an
    # original attempt from a replay with another delayed RPC still outstanding.
    h.signing.buyer.peer.track_payment.return_value = PaymentResult(
        PaymentStatus.FAILED,
        h.invoice.payment_hash,
        None,
        0,
    )
    await h.buyer.poll(h.sid)
    assert store.get(h.sid).state == "PAYMENT_RECOVERY_REQUIRED"
    recovery = _recovery(h)
    h.buyer.runtime_binding = recovery.binding
    h.chain.height.return_value = 700
    h.chain.tip.return_value = (700, "99" * 32)
    await h.buyer.poll(h.sid)
    h.chain.broadcast.assert_not_awaited()
    del h.transactions[h.parent.txid]
    assert not await recovery.poll(h.sid)
    recovery.peer.close_channel.assert_not_awaited()


@pytest.mark.parametrize("state", ["INVOICE_CREATING", "INVOICE_READY", "FUTURE_STATE"])
async def test_missing_invoice_markers_do_not_authorize_creation_or_close(
    settlement: SettlementRuntime,
    state: str,
) -> None:
    h = settlement
    store = h.signing.counterparty.store
    record = store.get(h.sid)
    store.update(record, state=state, data=record.data)
    h.signing.counterparty.peer.invoice_request.return_value = None
    await h.request(BUYER_KEY.hex(), settlement_status(h))
    h.signing.counterparty.peer.invoice_request.assert_awaited_once_with(h.invoice.payment_hash)
    h.signing.counterparty.peer.create_invoice.assert_not_awaited()
    # Missing local invoice markers cannot hide an affirmative settled invoice
    # when the counterparty reaches the timeout-split height.
    h.signing.counterparty.peer.invoice_status.return_value = InvoiceStatus(
        InvoiceState.SETTLED,
        h.invoice.amount_sat,
        h.preimage,
    )
    h.chain.height.return_value = 700
    h.chain.tip.return_value = (700, "99" * 32)
    await h.counterparty.poll(h.sid)
    h.chain.broadcast.assert_not_awaited()
    recovery = _recovery(h, counterparty=True)
    del h.transactions[h.parent.txid]
    assert not await recovery.poll(h.sid)
    recovery.peer.close_channel.assert_not_awaited()


@pytest.mark.parametrize("prior_split", [False, True])
async def test_recorded_close_remains_observable_after_a_conflict_reorg(
    settlement: SettlementRuntime,
    prior_split: bool,
) -> None:
    h = settlement
    recovery = _recovery(h)
    if prior_split:
        h.buyer.runtime_binding = recovery.binding
        h.chain.height.return_value = 700
        h.chain.tip.return_value = (700, "99" * 32)
        assert await h.buyer.poll(h.sid) == "SPEND_BROADCAST"
        h.chain.height.return_value = 344
        h.chain.tip.return_value = (344, "aa" * 32)
    del h.transactions[h.parent.txid]
    assert await recovery.poll(h.sid)
    recovery.peer.close_channel.assert_awaited_once()
    h.chain.unspent.return_value = False
    assert await recovery.poll(h.sid)
    assert recovery.store.get(h.sid).state == "CONFLICTED"
    h.chain.unspent.return_value = True
    assert await recovery.poll(h.sid)
    recovery.peer.close_channel.assert_awaited_once()


async def test_unpaid_split_can_be_rebroadcast_without_starting_payment(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    h.signing.buyer.peer.track_payment.return_value = None
    recovery = _recovery(h)
    h.buyer.runtime_binding = recovery.binding
    h.chain.height.return_value = 700
    h.chain.tip.return_value = (700, "99" * 32)
    assert await h.buyer.poll(h.sid) == "SPEND_BROADCAST"
    first = h.chain.broadcast.await_args.args[0]
    assert await h.buyer.poll(h.sid) == "SPEND_BROADCAST"
    assert h.chain.broadcast.await_count == 2
    assert h.chain.broadcast.await_args.args[0] == first
    h.signing.buyer.peer.pay.assert_not_awaited()


async def test_confirmed_parent_after_close_attempt_keeps_unpaid_recovery(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    h.signing.buyer.peer.track_payment.return_value = None
    recovery = _recovery(h)
    h.buyer.runtime_binding = recovery.binding
    parent = h.transactions.pop(h.parent.txid)
    assert await recovery.poll(h.sid)
    h.transactions[h.parent.txid] = parent
    h.chain.height.return_value = 700
    h.chain.tip.return_value = (700, "99" * 32)
    assert await h.buyer.poll(h.sid) == "SPEND_BROADCAST"
    h.signing.buyer.peer.pay.assert_not_awaited()


@pytest.mark.parametrize("blocked", ["expired", "clock_rollback", "height", "depth", "spent"])
async def test_payment_replay_rechecks_original_time_and_chain_bounds(
    settlement: SettlementRuntime,
    monkeypatch: pytest.MonkeyPatch,
    blocked: str,
) -> None:
    h = settlement
    await interrupt_payment(h)
    intent = h.signing.buyer.store.get(h.sid).data["payment_intent"]
    if blocked == "expired":
        monkeypatch.setattr("jmswap.buyout_settlement.time.time", lambda: intent["expires_at"])
    elif blocked == "clock_rollback":
        monkeypatch.setattr("jmswap.buyout_settlement.time.time", lambda: intent["started_at"] - 1)
    elif blocked == "height":
        h.chain.tip.return_value = (intent["last_dispatch_height"] + 1, "99" * 32)
    elif blocked == "depth":
        h.transactions[h.parent.txid] = replace(h.transactions[h.parent.txid], confirmations=2)
    else:
        h.chain.unspent.return_value = False
    await h.buyer.poll(h.sid)
    h.signing.buyer.peer.pay.assert_not_awaited()
    h.chain.broadcast.assert_not_awaited()


async def test_duplicate_payment_response_recovers_the_winning_result(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    await interrupt_payment(h)
    h.signing.buyer.peer.pay.side_effect = LndPeerRpcError(
        "SendPaymentV2",
        grpc.StatusCode.ALREADY_EXISTS,
    )
    h.signing.buyer.peer.track_payment.side_effect = [
        None,
        PaymentResult(PaymentStatus.SUCCEEDED, h.invoice.payment_hash, h.preimage, 3),
    ]
    await h.buyer.poll(h.sid)
    assert h.signing.buyer.store.get(h.sid).data["settlement_preimage"] == h.preimage.hex()
    h.signing.buyer.peer.pay.assert_awaited_once()


async def test_failed_replay_does_not_prove_a_delayed_original_cannot_pay(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    await interrupt_payment(h)
    failed = PaymentResult(PaymentStatus.FAILED, h.invoice.payment_hash, None, 0)
    h.signing.buyer.peer.pay.return_value = failed
    assert await h.buyer.poll(h.sid) == "PAYMENT_RECOVERY_REQUIRED"
    h.signing.buyer.peer.track_payment.return_value = None
    recovery = _recovery(h)
    assert not await recovery.poll(h.sid)
    h.chain.height.return_value = 700
    h.chain.tip.return_value = (700, "99" * 32)
    await h.buyer.poll(h.sid)
    h.chain.broadcast.assert_not_awaited()
    recovery.peer.close_channel.assert_not_awaited()
    h.signing.buyer.peer.pay.assert_awaited_once()


async def test_reopened_invoice_creation_retains_original_expiry(
    settlement: SettlementRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = settlement
    clock = [h.invoice.created_at]
    monkeypatch.setattr("jmswap.buyout_settlement.time.time", lambda: clock[0])
    h.signing.counterparty.peer.create_invoice.side_effect = TimeoutError("before AddInvoice")
    with pytest.raises(TimeoutError):
        await h.request(BUYER_KEY.hex(), settlement_status(h))
    store = h.signing.counterparty.store
    original = store.get(h.sid).data["invoice_intent"]
    path = store._path
    store.close()
    clock[0] += 20
    h.signing.counterparty.peer.invoice_request.return_value = None
    h.signing.counterparty.peer.create_invoice.reset_mock(side_effect=True)
    with BuyoutStore(path) as reopened:
        counterparty = BuyoutSettlement(
            reopened,
            h.signing.counterparty.peer,
            h.chain,
            SettlementPolicy(invoice_expiry_seconds=99_999),
        )
        await counterparty.handle(BUYER_KEY.hex(), settlement_status(h))
        assert reopened.get(h.sid).data["invoice_intent"] == original
        h.signing.counterparty.peer.create_invoice.assert_awaited_once_with(
            h.preimage,
            h.invoice.amount_sat,
            3_580,
            include_private_routes=True,
        )


async def test_invoice_duplicate_race_recovers_lookup_instead_of_replacing(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    h.signing.counterparty.peer.create_invoice.side_effect = LndPeerRpcError(
        "AddInvoice",
        grpc.StatusCode.UNKNOWN,
    )
    h.signing.counterparty.peer.invoice_request.return_value = "lnbcrt10u1pjtestinvoice"
    assert await h.buyer.poll(h.sid) == "SETTLED"
    h.signing.counterparty.peer.create_invoice.assert_awaited_once()


async def test_delayed_invoice_rpc_cannot_extend_absolute_expiry(
    settlement: SettlementRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = settlement
    monkeypatch.setattr("jmswap.buyout_settlement.time.time", lambda: h.invoice.created_at)
    h.signing.counterparty.peer.inspect_invoice.return_value = replace(
        h.invoice,
        created_at=h.invoice.created_at + 10,
    )
    await h.buyer.poll(h.sid)
    h.signing.buyer.peer.pay.assert_not_awaited()
    # Retain the original invoice for observation, never replace it to fix expiry.
    await h.buyer.poll(h.sid)
    h.signing.counterparty.peer.create_invoice.assert_awaited_once()
    h.signing.buyer.peer.pay.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["expired", "partial", "unknown", "missing_marker"])
async def test_invoice_replay_does_not_infer_permission_from_missing_state(
    settlement: SettlementRuntime,
    mutation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = settlement
    h.signing.counterparty.peer.create_invoice.side_effect = TimeoutError("before AddInvoice")
    with pytest.raises(TimeoutError):
        await h.request(BUYER_KEY.hex(), settlement_status(h))
    store = h.signing.counterparty.store
    record = store.get(h.sid)
    data = dict(record.data)
    intent = dict(data["invoice_intent"])
    if mutation == "expired":
        monkeypatch.setattr("jmswap.buyout_settlement.time.time", lambda: intent["expires_at"])
    elif mutation == "partial":
        intent.pop("amount_sat")
    elif mutation == "unknown":
        intent["version"] = 2
    else:
        data.pop("invoice_started")
    data["invoice_intent"] = intent
    store.update(record, state=record.state, data=data)
    h.signing.counterparty.peer.invoice_request.return_value = None
    h.signing.counterparty.peer.create_invoice.reset_mock()
    await h.request(BUYER_KEY.hex(), settlement_status(h))
    h.signing.counterparty.peer.create_invoice.assert_not_awaited()
    assert store.get(h.sid).data == data
