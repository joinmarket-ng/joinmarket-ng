from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
import test_buyout_signing as signing_tests
from test_buyout_signing import BUYER_KEY, COUNTERPARTY_KEY, POINT, Runtime, SettlementRuntime

from jmswap.buyout_chain import BuyoutChain, ChainTransaction
from jmswap.buyout_recovery import BuyoutRecovery
from jmswap.buyout_store import BuyoutStore
from jmswap.lnd_peer import InvoiceState, InvoiceStatus, LndPeerError

runtime = signing_tests.runtime
settlement = signing_tests.settlement


def _recovery(h: SettlementRuntime, *, counterparty: bool = False) -> BuyoutRecovery:
    signer = h.signing.counterparty if counterparty else h.signing.buyer
    identity = COUNTERPARTY_KEY if counterparty else BUYER_KEY
    binding = {"network": "regtest", "lnd_identity": identity.hex(), "mixdepth": 0}
    record = signer.store.get(h.sid)
    signer.store.update(
        record,
        state=record.state,
        data={
            **record.data,
            "runtime_binding": binding,
            "recovery_authorized": True,
            "force_close_authorized": True,
        },
    )
    h.chain.height.return_value = 344
    h.chain.tip.return_value = (344, "aa" * 32)
    h.transactions[POINT.txid] = ChainTransaction(POINT.txid, b"", 145, 200, "bb" * 32)
    return BuyoutRecovery(
        signer.store, signer.escrow, signer.peer, h.chain, runtime_binding=binding
    )


def _change(recovery: BuyoutRecovery, sid: str, **changes: Any) -> None:
    record = recovery.store.get(sid)
    recovery.store.update(record, state=record.state, data={**record.data, **changes})


async def test_unsigned_deadline_releases_only_after_the_agreed_height(runtime: Runtime) -> None:
    binding = {"network": "regtest", "lnd_identity": BUYER_KEY.hex(), "mixdepth": 0}
    runtime.buyer.runtime_binding = binding
    runtime.buyer.recovery_authorized = True
    sid = await runtime.prepare()
    chain = create_autospec(BuyoutChain, instance=True)
    recovery = BuyoutRecovery(
        runtime.buyer.store,
        runtime.buyer.escrow,
        runtime.buyer.peer,
        chain,
        runtime_binding=binding,
    )
    chain.height.return_value = 205
    assert not await recovery.poll(sid)
    runtime.buyer_backend.cancel.assert_not_awaited()
    chain.height.return_value = 206
    assert await recovery.poll(sid)
    runtime.buyer_backend.cancel.assert_awaited_once_with(POINT, bytes.fromhex(sid))
    assert runtime.buyer.store.get(sid).state == "CANCELED"
    assert not runtime.buyer.store.get(sid).parent_signing_started
    assert not await recovery.poll(sid)
    runtime.buyer_backend.cancel.assert_awaited_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"recovery_authorized": None},
        {"recovery_authorized": "true"},
        {"force_close_authorized": None},
        {"force_close_authorized": False},
        {"freeze_height": None},
        {"freeze_height": False},
        {"freeze_height": -1},
    ],
)
async def test_unknown_authorization_or_clock_never_force_closes(
    settlement: SettlementRuntime,
    changes: dict[str, Any],
) -> None:
    h = settlement
    recovery = _recovery(h)
    del h.transactions[h.parent.txid]
    _change(recovery, h.sid, **changes)
    before = recovery.store.get(h.sid)
    assert not await recovery.poll(h.sid)
    assert recovery.store.get(h.sid) == before
    recovery.peer.close_channel.assert_not_awaited()


async def test_force_close_intent_precedes_rpc_and_is_not_repeated(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    recovery = _recovery(h)
    del h.transactions[h.parent.txid]

    async def close(*args: Any, **kwargs: Any) -> None:
        record = recovery.store.get(h.sid)
        assert record.state == "FORCE_CLOSE_REQUESTED"
        assert record.data["force_close_attempts"][-1]["status"] == "started"

    recovery.peer.close_channel.side_effect = close
    assert await recovery.poll(h.sid)
    assert recovery.store.get(h.sid).state == "FORCE_CLOSE_PENDING"
    assert await recovery.poll(h.sid)
    recovery.peer.close_channel.assert_awaited_once_with(POINT, force=True)
    h.chain.unspent.return_value = False
    assert await recovery.poll(h.sid)
    assert recovery.store.get(h.sid).state == "CONFLICTED"
    recovery.escrow.cancel.assert_not_awaited()


@pytest.mark.parametrize("authorization", [None, False, "true", 1])
async def test_prior_close_request_does_not_supply_missing_authorization(
    settlement: SettlementRuntime, authorization: Any
) -> None:
    h = settlement
    recovery = _recovery(h)
    del h.transactions[h.parent.txid]
    record = recovery.store.get(h.sid)
    data = {**record.data, "force_close_requested": True}
    if authorization is None:
        del data["force_close_authorized"]
    else:
        data["force_close_authorized"] = authorization
    recovery.store.update(record, state=record.state, data=data)
    before = recovery.store.get(h.sid)

    assert not await recovery.poll(h.sid)
    assert recovery.store.get(h.sid) == before
    recovery.peer.close_channel.assert_not_awaited()


async def test_uncertain_force_close_requires_explicit_retry_after_restart(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    recovery = _recovery(h)
    del h.transactions[h.parent.txid]
    recovery.peer.close_channel.side_effect = LndPeerError("lost close reply")
    with pytest.raises(LndPeerError):
        await recovery.poll(h.sid)
    restarted = BuyoutRecovery(
        recovery.store,
        recovery.escrow,
        recovery.peer,
        recovery.chain,
        runtime_binding=recovery.binding,
    )
    assert await restarted.poll(h.sid)
    assert recovery.store.get(h.sid).state == "FORCE_CLOSE_RECOVERY_REQUIRED"
    recovery.peer.close_channel.assert_awaited_once()
    recovery.peer.close_channel.side_effect = None
    await restarted.force_close(h.sid)
    assert recovery.peer.close_channel.await_count == 2
    assert len(recovery.store.get(h.sid).data["force_close_attempts"]) == 2
    assert await restarted.poll(h.sid)
    assert recovery.peer.close_channel.await_count == 2


async def test_crash_after_force_close_intent_survives_database_reopen(
    settlement: SettlementRuntime, tmp_path: Path
) -> None:
    class SimulatedCrash(BaseException):
        pass

    h = settlement
    original = _recovery(h)
    del h.transactions[h.parent.txid]

    async def crash_during_close(*args: Any, **kwargs: Any) -> None:
        saved = original.store.get(h.sid)
        assert saved.state == "FORCE_CLOSE_REQUESTED"
        assert saved.data["force_close_attempts"][-1]["status"] == "started"
        raise SimulatedCrash

    original.peer.close_channel.side_effect = crash_during_close
    with pytest.raises(SimulatedCrash):
        await original.poll(h.sid)
    saved = original.store.get(h.sid)
    original.store.close()
    # A new DB connection and a new recovery object must not treat a lost reply
    # as proof the force-close never happened, even across repeated restarts.
    for _ in range(2):
        with BuyoutStore(tmp_path / "buyer" / "sessions.sqlite") as reopened:
            restarted = BuyoutRecovery(
                reopened,
                original.escrow,
                original.peer,
                original.chain,
                runtime_binding=original.binding,
            )
            assert await restarted.poll(h.sid)
            current = reopened.get(h.sid)
            assert current.state == "FORCE_CLOSE_RECOVERY_REQUIRED"
            assert current.parent_signing_started
            assert current.data["force_close_attempts"] == saved.data["force_close_attempts"]
    original.peer.close_channel.assert_awaited_once()
    original.escrow.cancel.assert_not_awaited()


@pytest.mark.parametrize("authorization", [None, False, "true", "absent"])
async def test_reopened_legacy_session_cannot_infer_force_close_authorization(
    settlement: SettlementRuntime, tmp_path: Path, authorization: Any
) -> None:
    h = settlement
    original = _recovery(h)
    del h.transactions[h.parent.txid]
    record = original.store.get(h.sid)
    data = {**record.data, "force_close_requested": True}
    if authorization == "absent":
        del data["force_close_authorized"]
    else:
        data["force_close_authorized"] = authorization
    saved = original.store.update(record, state=record.state, data=data)
    original.store.close()
    with BuyoutStore(tmp_path / "buyer" / "sessions.sqlite") as reopened:
        restarted = BuyoutRecovery(
            reopened,
            original.escrow,
            original.peer,
            original.chain,
            runtime_binding=original.binding,
        )
        assert not await restarted.poll(h.sid)
        assert reopened.get(h.sid) == saved
    original.peer.close_channel.assert_not_awaited()
    original.escrow.cancel.assert_not_awaited()


@pytest.mark.parametrize(
    "changes",
    [
        {"payment_started": True},
        {"payment_started": True, "payment_failed": "true"},
        {"settlement_preimage": "11" * 32, "payment_failed": True},
    ],
)
async def test_paid_or_uncertain_payment_never_force_closes(
    settlement: SettlementRuntime,
    changes: dict[str, Any],
) -> None:
    h = settlement
    recovery = _recovery(h)
    del h.transactions[h.parent.txid]
    _change(recovery, h.sid, **changes)
    assert not await recovery.poll(h.sid)
    recovery.peer.close_channel.assert_not_awaited()


@pytest.mark.parametrize("state", [InvoiceState.OPEN, InvoiceState.ACCEPTED, InvoiceState.SETTLED])
async def test_counterparty_does_not_close_an_invoice_that_could_be_paid(
    settlement: SettlementRuntime,
    state: InvoiceState,
) -> None:
    h = settlement
    recovery = _recovery(h, counterparty=True)
    del h.transactions[h.parent.txid]
    _change(recovery, h.sid, invoice_started=True)
    recovery.peer.invoice_status.return_value = InvoiceStatus(state, 0, None)
    assert not await recovery.poll(h.sid)
    recovery.peer.close_channel.assert_not_awaited()


async def test_confirmed_parent_is_never_force_closed(settlement: SettlementRuntime) -> None:
    recovery = _recovery(settlement)
    assert not await recovery.poll(settlement.sid)
    recovery.peer.close_channel.assert_not_awaited()


@pytest.mark.parametrize("prior_authorization", [None, False])
async def test_explicit_action_can_authorize_recovery_of_a_bound_legacy_session(
    settlement: SettlementRuntime,
    prior_authorization: bool | None,
) -> None:
    h = settlement
    recovery = _recovery(h)
    del h.transactions[h.parent.txid]
    _change(
        recovery,
        h.sid,
        recovery_authorized=prior_authorization,
        force_close_authorized=prior_authorization,
        freeze_height=None,
    )
    assert not await recovery.poll(h.sid)
    recovery.peer.close_channel.assert_not_awaited()
    await recovery.force_close(h.sid)
    recovery.peer.close_channel.assert_awaited_once_with(POINT, force=True)
    record = recovery.store.get(h.sid)
    assert record.data["recovery_authorized"] is True
    assert record.data["force_close_authorized"] is True
    assert record.data["force_close_requested"] is True


async def test_unconfirmed_funding_is_not_proof_of_conflict(settlement: SettlementRuntime) -> None:
    h = settlement
    recovery = _recovery(h)
    del h.transactions[h.parent.txid]
    del h.transactions[POINT.txid]
    h.chain.unspent.return_value = False
    before = recovery.store.get(h.sid)
    assert not await recovery.poll(h.sid)
    assert recovery.store.get(h.sid) == before
    recovery.peer.close_channel.assert_not_awaited()


async def test_changing_tip_prevents_force_close(settlement: SettlementRuntime) -> None:
    h = settlement
    recovery = _recovery(h)
    del h.transactions[h.parent.txid]
    h.chain.tip.side_effect = [(344, "aa" * 32), (345, "bb" * 32)]
    assert not await recovery.poll(h.sid)
    recovery.peer.close_channel.assert_not_awaited()


async def test_buyer_can_split_without_counterparty_after_csv(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    _change(_recovery(h), h.sid, recovery_authorized=True)
    h.buyer.request = None
    h.chain.height.return_value = 630
    h.chain.tip.return_value = (630, "aa" * 32)
    await h.buyer.poll(h.sid)
    h.chain.broadcast.assert_not_awaited()
    h.chain.height.return_value = 631
    h.chain.tip.return_value = (631, "bb" * 32)
    assert await h.buyer.poll(h.sid) == "SPEND_BROADCAST"
    assert h.signing.buyer.store.get(h.sid).data["spends"][-1]["kind"] == "split"
    h.signing.buyer.peer.pay.assert_not_awaited()


async def test_buyer_does_not_split_when_payment_outcome_is_unknown(
    settlement: SettlementRuntime,
) -> None:
    h = settlement
    _change(_recovery(h), h.sid, recovery_authorized=True, payment_started=True)
    h.signing.buyer.peer.track_payment.return_value = None
    h.chain.tip.return_value = (700, "cc" * 32)
    await h.buyer.poll(h.sid)
    h.chain.broadcast.assert_not_awaited()
    h.signing.buyer.peer.pay.assert_not_awaited()
