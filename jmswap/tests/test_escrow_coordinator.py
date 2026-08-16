"""Tests for durable channel-input escrow lifecycle coordination."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from jmcore.bitcoin import TxInput, TxOutput, get_txid, serialize_transaction

from jmswap.channel_escrow import (
    ChainObservationRequest,
    ChannelInputEscrowRecord,
    ChannelInputEscrowStore,
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowChainObservation,
    EscrowLifecycleState,
    EscrowOutpoint,
    EscrowParentBroadcast,
    EscrowParentEvidence,
    EscrowParentPrevout,
    EscrowRouteEvidence,
    EscrowSpendResult,
    EscrowTimeoutBroadcast,
    EscrowTimeoutEvidence,
    FinalizedEscrowParent,
    FreezeChannelRequest,
    ParentAuthorization,
    ParentAuthorizationRequest,
    ParentBroadcastRequest,
    PaymentObservationRequest,
    PreparedChannelInputEscrow,
    PrepareTimeoutRequest,
    RecoveryBroadcastResult,
    RecoveryRequest,
    SinglePartPaymentRequest,
    SinglePartPaymentResult,
    SuccessSpendRequest,
)
from jmswap.channel_funding import ChannelInputEscrowCapability
from jmswap.errors import ChannelInputEscrowError
from jmswap.escrow_coordinator import ChannelInputEscrowCoordinator
from jmswap.lightning import PaymentStatus

_FUNDING_TXID = "44" * 32
_FUNDING_SCRIPT = "5120" + "45" * 32
_ESCROW_SCRIPT = "5120" + "55" * 32
_PARENT_TX_HEX = serialize_transaction(
    version=2,
    inputs=[TxInput.from_hex(_FUNDING_TXID, 2, sequence=0xFFFFFFFD)],
    outputs=[TxOutput.from_hex(_ESCROW_SCRIPT, 80_000)],
    locktime=0,
).hex()
PARENT_TXID = get_txid(_PARENT_TX_HEX)
TIMEOUT_TXID = "22" * 32
SUCCESS_TXID = "77" * 32
PREIMAGE = "33" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()


def _terms(session_id: str = "session-1") -> ChannelInputEscrowTerms:
    return ChannelInputEscrowTerms(
        snapshot=ChannelSnapshot(
            session_id=session_id,
            channel_id="channel-1",
            funding_outpoint=EscrowOutpoint(txid=_FUNDING_TXID, vout=2),
            funding_scriptpubkey=_FUNDING_SCRIPT,
            local_funding_pubkey="02" + "46" * 32,
            remote_funding_pubkey="03" + "47" * 32,
            capacity_sats=100_000,
            maker_claim_sats=70_000,
            peer_claim_sats=30_000,
            quiescent=True,
            active_htlcs=0,
        ),
        authorizer_pubkey="02" + "66" * 32,
        payment_hash=PAYMENT_HASH,
        invoice="lnbcrt1privateinvoice",
        invoice_payee_pubkey="02" + "34" * 32,
        invoice_payment_secret="35" * 32,
        invoice_amount_msat=32_000_000,
        invoice_expiry_unix=2_000_000_000,
        invoice_min_final_cltv_delta=18,
        invoice_verified=True,
        premium_sats=2_000,
        timeout_compensation_sats=500,
        escrow_value_sats=80_000,
        timeout_fee_sats=500,
        timeout_height=1_000,
        peer_settle_deadline=970,
        success_broadcast_deadline=980,
        parent_confirmation_depth=1,
        success_confirmation_depth=1,
        success_confirmation_budget_blocks=10,
        reorg_margin_blocks=5,
        safety_delta_blocks=3,
        preimage_budget_blocks=2,
    )


def _prepared(terms: ChannelInputEscrowTerms) -> PreparedChannelInputEscrow:
    outpoint = EscrowOutpoint(txid=PARENT_TXID, vout=0)
    return PreparedChannelInputEscrow(
        terms=terms,
        parent_evidence=EscrowParentEvidence(
            parent_tx_hex=_PARENT_TX_HEX,
            parent_txid=PARENT_TXID,
            channel_funding_outpoint=terms.snapshot.funding_outpoint,
            channel_input_index=0,
            ordered_prevouts=(
                EscrowParentPrevout(
                    value_sats=terms.snapshot.capacity_sats,
                    scriptpubkey=terms.snapshot.funding_scriptpubkey,
                ),
            ),
            escrow_outpoint=outpoint,
            escrow_value_sats=80_000,
            escrow_scriptpubkey=_ESCROW_SCRIPT,
            all_inputs_nonmalleable=True,
            exact_transaction_verified=True,
        ),
        timeout_evidence=EscrowTimeoutEvidence(
            timeout_tx_hex="0200",
            timeout_txid=TIMEOUT_TXID,
            spent_escrow_outpoint=outpoint,
            locktime=1_000,
            maker_output_sats=49_000,
            peer_output_sats=30_500,
            fee_sats=500,
            fully_signed=True,
            signature_verified=True,
        ),
    )


def _chain(
    session_id: str = "session-1", parent_txid: str = PARENT_TXID, **changes: object
) -> EscrowChainObservation:
    values: dict[str, object] = {
        "session_id": session_id,
        "parent_txid": parent_txid,
        "parent_confirmations": 1,
        "in_best_chain": True,
        "observed_height": 950,
    }
    values.update(changes)
    return EscrowChainObservation.model_validate(values)


def _finalized_parent(terms: ChannelInputEscrowTerms) -> FinalizedEscrowParent:
    return FinalizedEscrowParent(
        session_id=terms.snapshot.session_id,
        parent_txid=PARENT_TXID,
        signed_parent_tx_hex="010000",
        channel_funding_outpoint=terms.snapshot.funding_outpoint,
        channel_input_index=0,
        all_inputs_signed=True,
        all_signatures_verified=True,
        signed_parent_txid_verified=True,
    )


class FakeBackend:
    """Complete deterministic backend with failure switches and call evidence."""

    channel_input_escrow_capabilities = frozenset(ChannelInputEscrowCapability)

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.prepare_fails = False
        self.send_fails = False
        self.timeout_fails = False
        self.store: ChannelInputEscrowStore | None = None
        self.payment_result = self._payment(PaymentStatus.PENDING)
        self.payment_observation = self.payment_result
        self.chain_observations: list[EscrowChainObservation] = []
        self.resumed: list[ChannelInputEscrowRecord] = []
        self.sent_requests: list[SinglePartPaymentRequest] = []
        self.freeze_snapshot: ChannelSnapshot | None = None

    @staticmethod
    def _payment(status: PaymentStatus) -> SinglePartPaymentResult:
        values: dict[str, object] = {
            "payment_id": "payment-1",
            "status": status,
            "attempt_height": 950,
            "observed_height": 950,
            "route": EscrowRouteEvidence(
                part_count=1,
                route_channel_ids=("channel-2",),
                final_expiry_height=975,
                outgoing_expiry_height=978,
            ),
        }
        if status is PaymentStatus.SUCCEEDED:
            values.update(preimage=PREIMAGE, settled_height=978, observed_height=978)
        return SinglePartPaymentResult.model_validate(values)

    async def freeze_quiesce_channel(self, request: FreezeChannelRequest) -> ChannelSnapshot:
        self.calls.append("freeze")
        return self.freeze_snapshot or _terms(request.session_id).snapshot

    async def prepare_signed_timeout(
        self, request: PrepareTimeoutRequest
    ) -> PreparedChannelInputEscrow:
        self.calls.append("prepare")
        if self.prepare_fails:
            raise RuntimeError("prepare failed")
        return _prepared(request.terms)

    async def authorize_parent(self, request: ParentAuthorizationRequest) -> ParentAuthorization:
        self.calls.append("authorize")
        if self.store is not None:
            saved = self.store.load(request.prepared.terms.snapshot.session_id)
            assert saved is not None and saved.state is EscrowLifecycleState.TIMEOUT_SIGNED
        prepared = request.prepared
        return ParentAuthorization(
            session_id=prepared.terms.snapshot.session_id,
            parent_txid=prepared.parent_evidence.parent_txid,
            prepared_parent_tx_hex=prepared.parent_evidence.parent_tx_hex,
            timeout_txid=prepared.timeout_evidence.timeout_txid,
            channel_funding_outpoint=prepared.terms.snapshot.funding_outpoint,
            channel_input_index=prepared.parent_evidence.channel_input_index,
            channel_input_witness=(b"w" * 64,),
            sighash="default",
            signature_verified=True,
            channel_witness_txid_verified=True,
            commits_all_inputs=True,
            commits_all_outputs=True,
        )

    async def broadcast_parent(self, request: ParentBroadcastRequest) -> EscrowParentBroadcast:
        self.calls.append("parent")
        return EscrowParentBroadcast(
            session_id=request.prepared.terms.snapshot.session_id,
            parent_txid=request.prepared.parent_evidence.parent_txid,
            broadcast_height=940,
        )

    async def observe_escrow_chain(
        self, request: ChainObservationRequest
    ) -> EscrowChainObservation:
        self.calls.append("chain")
        if self.chain_observations:
            return self.chain_observations.pop(0)
        return _chain(request.session_id, request.parent_txid)

    async def send_single_part_payment(
        self, request: SinglePartPaymentRequest
    ) -> SinglePartPaymentResult:
        self.calls.append("send")
        self.sent_requests.append(request)
        if self.send_fails:
            raise RuntimeError("send failed")
        assert request.requested_at_height == 950
        return self.payment_result

    async def observe_escrow_payment(
        self, request: PaymentObservationRequest
    ) -> SinglePartPaymentResult:
        self.calls.append("payment")
        assert request.payment_id == "payment-1"
        return self.payment_observation

    async def build_broadcast_success_spend(
        self, request: SuccessSpendRequest
    ) -> EscrowSpendResult:
        self.calls.append("success")
        return EscrowSpendResult(
            session_id=request.prepared.terms.snapshot.session_id,
            parent_txid=request.prepared.parent_evidence.parent_txid,
            spent_escrow_outpoint=request.prepared.parent_evidence.escrow_outpoint,
            txid=SUCCESS_TXID,
            tx_hex="0200",
            escrow_input_value_sats=80_000,
            additional_input_value_sats=0,
            maker_controlled_output_sats=79_500,
            fee_sats=500,
            fee_rate_milli_sat_vb=1_000,
            fulfillment_height=978,
            broadcast_height=978,
            rbf_enabled=True,
            preimage_verified=True,
            success_path_verified=True,
            transaction_verified=True,
            all_outputs_maker_controlled=True,
        )

    async def broadcast_timeout_recovery(self, request: RecoveryRequest) -> EscrowTimeoutBroadcast:
        self.calls.append("timeout")
        if self.timeout_fails:
            raise RuntimeError("immature")
        prepared = request.record.prepared
        assert prepared is not None
        return EscrowTimeoutBroadcast(
            session_id=request.record.session_id,
            parent_txid=prepared.parent_evidence.parent_txid,
            timeout_txid=prepared.timeout_evidence.timeout_txid,
            broadcast_height=1_001,
        )

    async def fee_bump_success_spend(self, request: RecoveryRequest) -> EscrowSpendResult:
        self.calls.append("bump")
        previous = request.record.success_spend
        assert previous is not None
        return previous.model_copy(
            update={
                "txid": "88" * 32,
                "fee_sats": 600,
                "maker_controlled_output_sats": 79_400,
                "fee_rate_milli_sat_vb": 1_100,
                "broadcast_height": 979,
                "replaces_txid": previous.txid,
                "replacement_policy_verified": True,
            }
        )

    async def rebroadcast_recovery_artifacts(
        self, request: RecoveryRequest
    ) -> RecoveryBroadcastResult:
        self.calls.append("rebroadcast")
        txids = [request.record.prepared.parent_evidence.parent_txid]  # type: ignore[union-attr]
        if request.record.success_spend is not None:
            txids.append(request.record.success_spend.txid)
        if request.record.timeout_broadcast is not None:
            txids.append(request.record.timeout_broadcast.timeout_txid)
        return RecoveryBroadcastResult(
            session_id=request.record.session_id,
            parent_txid=txids[0],
            broadcast_txids=tuple(txids),
            broadcast_height=951,
        )

    async def resume_durable_sessions(self) -> list[ChannelInputEscrowRecord]:
        self.calls.append("resume")
        return self.resumed


async def _parent_confirmed(
    coordinator: ChannelInputEscrowCoordinator, terms: ChannelInputEscrowTerms
) -> ChannelInputEscrowRecord:
    request = PrepareTimeoutRequest(terms=terms, parent_evidence=_prepared(terms).parent_evidence)
    await coordinator.prepare_timeout(request)
    await coordinator.authorize_parent(terms.snapshot.session_id)
    await coordinator.finalize_parent(terms.snapshot.session_id, _finalized_parent(terms))
    await coordinator.broadcast_parent(terms.snapshot.session_id)
    return await coordinator.observe_chain(terms.snapshot.session_id)


@pytest.fixture
def setup(
    tmp_path: Path,
) -> tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore]:
    backend = FakeBackend()
    store = ChannelInputEscrowStore(tmp_path)
    backend.store = store
    return backend, ChannelInputEscrowCoordinator(backend, store), store


def test_find_parent_requires_a_unique_durable_claim(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    _, coordinator, store = setup
    first_terms = _terms()
    first = ChannelInputEscrowRecord(
        terms=first_terms,
        negotiated_parent_evidence=_prepared(first_terms).parent_evidence,
    )
    store.save(first)

    found = coordinator.find_parent(PARENT_TXID)
    assert found == first
    assert found is not first
    assert coordinator.find_parent("00" * 32) is None
    with pytest.raises(ChannelInputEscrowError, match="invalid escrow parent"):
        coordinator.find_parent("invalid")

    second_terms = _terms("session-2")
    store.save(
        ChannelInputEscrowRecord(
            terms=second_terms,
            negotiated_parent_evidence=_prepared(second_terms).parent_evidence,
        )
    )
    with pytest.raises(ChannelInputEscrowError, match="multiple escrow sessions"):
        coordinator.find_parent(PARENT_TXID)


async def test_prepare_persists_terms_before_side_effect_and_rejects_conflicts(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, store = setup
    terms = _terms()
    request = PrepareTimeoutRequest(terms=terms, parent_evidence=_prepared(terms).parent_evidence)
    backend.prepare_fails = True
    with pytest.raises(RuntimeError, match="prepare failed"):
        await coordinator.prepare_timeout(request)
    saved = store.load("session-1")
    assert saved is not None and saved.state is EscrowLifecycleState.NEGOTIATED
    assert saved.negotiated_parent_evidence == request.parent_evidence

    backend.prepare_fails = False
    prepared = await coordinator.prepare_timeout(request)
    assert prepared.state is EscrowLifecycleState.TIMEOUT_SIGNED
    assert (await coordinator.prepare_timeout(request)).revision == prepared.revision
    assert backend.calls.count("prepare") == 2
    conflicting_hex = serialize_transaction(
        version=2,
        inputs=[TxInput.from_hex(_FUNDING_TXID, 2, sequence=0xFFFFFFFC)],
        outputs=[TxOutput.from_hex(_ESCROW_SCRIPT, 80_000)],
        locktime=0,
    ).hex()
    conflicting_txid = get_txid(conflicting_hex)
    conflicting_values = request.parent_evidence.model_dump()
    conflicting_values.update(
        {
            "parent_tx_hex": conflicting_hex,
            "parent_txid": conflicting_txid,
            "escrow_outpoint": EscrowOutpoint(txid=conflicting_txid, vout=0),
        }
    )
    conflicting = EscrowParentEvidence.model_validate(conflicting_values)
    with pytest.raises(ChannelInputEscrowError, match="conflicts"):
        await coordinator.prepare_timeout(
            PrepareTimeoutRequest(terms=terms, parent_evidence=conflicting)
        )


async def test_freeze_rejects_backend_session_or_channel_substitution(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, _ = setup
    request = FreezeChannelRequest(session_id="session-1", channel_id="channel-1")
    assert await coordinator.freeze_channel(request) == _terms().snapshot
    backend.freeze_snapshot = _terms("different-session").snapshot
    with pytest.raises(ChannelInputEscrowError, match="different escrow session or channel"):
        await coordinator.freeze_channel(request)


async def test_parent_ordering_payment_durability_and_success_recovery(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, store = setup
    terms = _terms()
    request = PrepareTimeoutRequest(terms=terms, parent_evidence=_prepared(terms).parent_evidence)
    await coordinator.prepare_timeout(request)
    with pytest.raises(ChannelInputEscrowError, match="parent broadcast"):
        await coordinator.broadcast_parent("session-1")
    await coordinator.authorize_parent("session-1")
    await coordinator.finalize_parent("session-1", _finalized_parent(terms))
    await coordinator.broadcast_parent("session-1")
    assert (
        await coordinator.observe_chain("session-1")
    ).state is EscrowLifecycleState.PARENT_CONFIRMED

    backend.send_fails = True
    with pytest.raises(RuntimeError, match="send failed"):
        await coordinator.start_payment("session-1", ("channel-1", "channel-2"), 1_900_000_000)
    saved = store.load("session-1")
    assert saved is not None and saved.payment_request is not None and saved.payment_result is None
    backend.send_fails = False
    backend.payment_result = backend._payment(PaymentStatus.SUCCEEDED)
    backend.chain_observations.extend((_chain(), _chain(observed_height=978)))
    recovered = await coordinator.start_payment("session-1", ("changed",), 1)
    assert recovered.state is EscrowLifecycleState.SUCCESS_SPEND_PENDING
    assert backend.calls.count("send") == 2
    assert backend.calls.count("success") == 1
    assert "abort" not in backend.calls


async def test_resume_resends_only_the_exact_durable_payment_request(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, store = setup
    await _parent_confirmed(coordinator, _terms())
    backend.send_fails = True
    with pytest.raises(RuntimeError, match="send failed"):
        await coordinator.start_payment("session-1", ("channel-1",), 1_900_000_000)
    durable = store.load("session-1")
    assert durable is not None and durable.payment_request is not None
    exact_request = durable.payment_request

    backend.send_fails = False
    backend.chain_observations.append(_chain(observed_height=951))
    resumed = await ChannelInputEscrowCoordinator(backend, store).resume_durable_sessions()

    assert resumed[0].state is EscrowLifecycleState.PAYMENT_IN_FLIGHT
    assert backend.sent_requests[-1] == exact_request


async def test_resume_replays_exact_negotiated_parent_after_interrupted_prepare(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, store = setup
    terms = _terms()
    parent_evidence = _prepared(terms).parent_evidence
    store.save(
        ChannelInputEscrowRecord(
            terms=terms,
            negotiated_parent_evidence=parent_evidence,
        )
    )

    [resumed] = await coordinator.resume_durable_sessions()

    assert resumed.state is EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED
    assert resumed.prepared is not None
    assert resumed.prepared.parent_evidence == parent_evidence
    assert backend.calls == ["resume", "prepare", "authorize"]


async def test_finalize_parent_is_idempotent_and_rejects_conflicting_evidence(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, _ = setup
    terms = _terms()
    request = PrepareTimeoutRequest(terms=terms, parent_evidence=_prepared(terms).parent_evidence)
    await coordinator.prepare_timeout(request)
    released = await coordinator.authorize_parent(terms.snapshot.session_id)
    assert released.state is EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED

    finalized = _finalized_parent(terms)
    signed = await coordinator.finalize_parent(terms.snapshot.session_id, finalized)
    assert signed.state is EscrowLifecycleState.PARENT_SIGNED
    assert await coordinator.finalize_parent(terms.snapshot.session_id, finalized) == signed

    conflicting = finalized.model_copy(update={"signed_parent_tx_hex": "020000"})
    with pytest.raises(ChannelInputEscrowError, match="conflicts with durable evidence"):
        await coordinator.finalize_parent(terms.snapshot.session_id, conflicting)


async def test_initial_failed_payment_is_persisted_and_allows_timeout_recovery(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, store = setup
    await _parent_confirmed(coordinator, _terms())
    failed_result = backend._payment(PaymentStatus.FAILED)
    backend.payment_result = failed_result

    payment = await coordinator.start_payment("session-1", ("channel-1",), 1_900_000_000)

    record = store.load("session-1")
    assert record is not None
    assert payment.state is EscrowLifecycleState.PAYMENT_IN_FLIGHT
    assert record.state is EscrowLifecycleState.PAYMENT_IN_FLIGHT
    assert record.payment_request is not None
    assert record.payment_result == failed_result

    backend.chain_observations.append(_chain(parent_confirmations=2, observed_height=1_001))
    timeout = await coordinator.broadcast_timeout("session-1")

    assert timeout.state is EscrowLifecycleState.TIMEOUT_PENDING
    assert timeout.payment_result == failed_result
    assert backend.calls.count("send") == 1
    assert backend.calls.count("timeout") == 1


async def test_timeout_reorg_conflict_and_resume_reconciliation(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, store = setup
    terms = _terms()
    await _parent_confirmed(coordinator, terms)
    backend.timeout_fails = True
    with pytest.raises(RuntimeError, match="immature"):
        await coordinator.broadcast_timeout("session-1")
    assert store.load("session-1").timeout_broadcast is None  # type: ignore[union-attr]
    backend.timeout_fails = False
    backend.chain_observations.append(_chain(parent_confirmations=2, observed_height=1_001))
    timeout = await coordinator.broadcast_timeout("session-1")
    assert timeout.state is EscrowLifecycleState.TIMEOUT_PENDING
    assert backend.calls.count("chain") >= 2

    backend.chain_observations.append(
        _chain(
            parent_confirmations=2,
            observed_height=1_002,
            timeout_txid=TIMEOUT_TXID,
            timeout_confirmations=1,
        )
    )
    assert (
        await coordinator.observe_chain("session-1")
    ).state is EscrowLifecycleState.TIMEOUT_CONFIRMED

    backend.chain_observations.append(_chain(parent_confirmations=0, in_best_chain=False))
    reorged = await coordinator.observe_chain("session-1")
    assert reorged.state is EscrowLifecycleState.PARENT_REORGED
    backend.chain_observations.append(_chain(parent_confirmations=2, observed_height=1_003))
    restored = await coordinator.recover_reorg("session-1")
    assert restored.state is EscrowLifecycleState.TIMEOUT_PENDING
    assert "rebroadcast" in backend.calls

    local = store.load("session-1")
    assert local is not None
    backend.resumed = [local.model_copy(update={"revision": local.revision + 1})]
    backend.chain_observations.append(_chain(parent_confirmations=2, observed_height=1_004))
    calls_before_resume = list(backend.calls)
    with pytest.raises(ChannelInputEscrowError, match="ahead"):
        await coordinator.resume_durable_sessions()
    assert backend.calls == [*calls_before_resume, "resume"]


async def test_concurrent_prepare_is_single_backend_side_effect(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, _ = setup
    terms = _terms()
    request = PrepareTimeoutRequest(terms=terms, parent_evidence=_prepared(terms).parent_evidence)
    first, second = await asyncio.gather(
        coordinator.prepare_timeout(request), coordinator.prepare_timeout(request)
    )
    assert first == second
    assert backend.calls.count("prepare") == 1


async def test_chain_conflicts_and_paid_timeout_loss(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, _ = setup
    unpaid_terms = _terms("unpaid")
    await _parent_confirmed(coordinator, unpaid_terms)
    backend.chain_observations.append(
        _chain(
            session_id="unpaid",
            parent_confirmations=0,
            in_best_chain=False,
            conflict_txid="99" * 32,
            conflict_confirmations=1,
            conflicting_channel_outpoint=unpaid_terms.snapshot.funding_outpoint,
            conflict_spends_channel=True,
        )
    )
    assert (
        await coordinator.observe_chain("unpaid")
    ).state is EscrowLifecycleState.COMMITMENT_CONFLICT

    paid_terms = _terms("paid")
    await _parent_confirmed(coordinator, paid_terms)
    backend.payment_result = backend._payment(PaymentStatus.SUCCEEDED)
    backend.chain_observations.extend(
        (
            _chain(session_id="paid"),
            _chain(session_id="paid", observed_height=978),
        )
    )
    assert (
        await coordinator.start_payment("paid", ("channel-1",), 1_900_000_000)
    ).state is EscrowLifecycleState.SUCCESS_SPEND_PENDING
    backend.chain_observations.append(
        _chain(
            session_id="paid",
            parent_confirmations=2,
            observed_height=1_002,
            timeout_txid=TIMEOUT_TXID,
            timeout_confirmations=1,
        )
    )
    assert (await coordinator.observe_chain("paid")).state is EscrowLifecycleState.PAID_TIMEOUT_LOSS
    assert backend.calls.count("timeout") == 1


async def test_settled_payment_reorg_rebroadcasts_success_before_restore_or_loss(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, _ = setup
    terms = _terms("settled-reorg")
    await _parent_confirmed(coordinator, terms)
    backend.payment_result = backend._payment(PaymentStatus.SUCCEEDED)
    backend.chain_observations.extend(
        (
            _chain(session_id="settled-reorg"),
            _chain(
                session_id="settled-reorg",
                parent_confirmations=0,
                in_best_chain=False,
                observed_height=978,
            ),
        )
    )
    reorged = await coordinator.start_payment("settled-reorg", ("channel-1",), 1_900_000_000)
    assert reorged.state is EscrowLifecycleState.PARENT_REORGED
    assert reorged.state_before_reorg is EscrowLifecycleState.SUCCESS_SPEND_PENDING

    backend.chain_observations.append(
        _chain(session_id="settled-reorg", parent_confirmations=2, observed_height=979)
    )
    restored = await coordinator.recover_reorg("settled-reorg")
    assert restored.state is EscrowLifecycleState.SUCCESS_SPEND_PENDING
    assert restored.recovery_broadcasts[-1].broadcast_txids == (PARENT_TXID, SUCCESS_TXID)

    conflict_terms = _terms("settled-conflict")
    await _parent_confirmed(coordinator, conflict_terms)
    backend.payment_result = backend._payment(PaymentStatus.SUCCEEDED)
    backend.chain_observations.extend(
        (
            _chain(session_id="settled-conflict"),
            _chain(session_id="settled-conflict", observed_height=978),
        )
    )
    await coordinator.start_payment("settled-conflict", ("channel-1",), 1_900_000_000)
    backend.chain_observations.append(
        _chain(
            session_id="settled-conflict",
            parent_confirmations=0,
            in_best_chain=False,
            conflict_txid="99" * 32,
            conflict_confirmations=1,
            conflicting_channel_outpoint=conflict_terms.snapshot.funding_outpoint,
            conflict_spends_channel=True,
            observed_height=979,
        )
    )
    assert (
        await coordinator.observe_chain("settled-conflict")
    ).state is EscrowLifecycleState.CROSS_LAYER_LOSS


@pytest.mark.parametrize(
    ("terminal_payment", "expected_state"),
    [
        (PaymentStatus.FAILED, EscrowLifecycleState.COMMITMENT_CONFLICT),
        (PaymentStatus.SUCCEEDED, EscrowLifecycleState.CROSS_LAYER_LOSS),
    ],
)
async def test_pending_payment_conflict_waits_for_terminal_payment_evidence(
    tmp_path: Path,
    terminal_payment: PaymentStatus,
    expected_state: EscrowLifecycleState,
) -> None:
    backend = FakeBackend()
    store = ChannelInputEscrowStore(tmp_path)
    backend.store = store
    coordinator = ChannelInputEscrowCoordinator(backend, store)
    terms = _terms(f"pending-{terminal_payment}")
    await _parent_confirmed(coordinator, terms)
    backend.chain_observations.append(_chain(session_id=terms.snapshot.session_id))
    payment = await coordinator.start_payment(
        terms.snapshot.session_id, ("channel-1",), 1_900_000_000
    )
    assert payment.state is EscrowLifecycleState.PAYMENT_IN_FLIGHT

    conflict_observation = _chain(
        session_id=terms.snapshot.session_id,
        parent_confirmations=0,
        in_best_chain=False,
        conflict_txid="99" * 32,
        conflict_confirmations=1,
        conflicting_channel_outpoint=terms.snapshot.funding_outpoint,
        conflict_spends_channel=True,
        observed_height=951,
    )
    backend.chain_observations.append(conflict_observation)
    unresolved = await coordinator.observe_chain(terms.snapshot.session_id)
    assert unresolved.state is EscrowLifecycleState.PARENT_REORGED
    assert unresolved.state_before_reorg is EscrowLifecycleState.PAYMENT_IN_FLIGHT

    backend.payment_observation = backend._payment(terminal_payment)
    if terminal_payment is PaymentStatus.SUCCEEDED:
        backend.chain_observations.append(
            conflict_observation.model_copy(update={"observed_height": 978})
        )
    resolved = await coordinator.observe_payment(terms.snapshot.session_id)
    assert resolved.state is expected_state
    if terminal_payment is PaymentStatus.SUCCEEDED:
        assert "success" in backend.calls


async def test_reorg_recovery_classifies_conflict_before_rebroadcast(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, _ = setup
    terms = _terms()
    await _parent_confirmed(coordinator, terms)
    backend.chain_observations.append(_chain(parent_confirmations=0, in_best_chain=False))
    assert (
        await coordinator.observe_chain(terms.snapshot.session_id)
    ).state is EscrowLifecycleState.PARENT_REORGED
    backend.chain_observations.append(
        _chain(
            parent_confirmations=0,
            in_best_chain=False,
            conflict_txid="99" * 32,
            conflict_confirmations=1,
            conflicting_channel_outpoint=terms.snapshot.funding_outpoint,
            conflict_spends_channel=True,
            observed_height=951,
        )
    )

    recovered = await coordinator.recover_reorg(terms.snapshot.session_id)

    assert recovered.state is EscrowLifecycleState.COMMITMENT_CONFLICT
    assert "rebroadcast" not in backend.calls


async def test_shallow_best_chain_reorg_waits_for_parent_depth_without_rebroadcast(
    setup: tuple[FakeBackend, ChannelInputEscrowCoordinator, ChannelInputEscrowStore],
) -> None:
    backend, coordinator, _ = setup
    terms = _terms().model_copy(update={"parent_confirmation_depth": 2})
    backend.chain_observations.append(_chain(parent_confirmations=2))
    confirmed = await _parent_confirmed(coordinator, terms)
    assert confirmed.state is EscrowLifecycleState.PARENT_CONFIRMED

    backend.chain_observations.append(_chain(parent_confirmations=1, observed_height=951))
    reconfirming = await coordinator.observe_chain(terms.snapshot.session_id)

    assert reconfirming.state is EscrowLifecycleState.PARENT_REORGED
    assert reconfirming.state_before_reorg is EscrowLifecycleState.PARENT_CONFIRMED
    assert not reconfirming.recovery_required

    backend.chain_observations.append(_chain(parent_confirmations=2, observed_height=952))
    restored = await coordinator.recover_reorg(terms.snapshot.session_id)

    assert restored.state is EscrowLifecycleState.PARENT_CONFIRMED
    assert "rebroadcast" not in backend.calls
