"""Tests for the typed channel-input escrow backend boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from jmswap.channel_escrow import (
    ChainObservationRequest,
    ChannelInputEscrowBackend,
    ChannelInputEscrowRecord,
    ChannelInputEscrowStore,
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowChainObservation,
    EscrowLifecycleState,
    EscrowOutpoint,
    EscrowParentBroadcast,
    EscrowParentEvidence,
    EscrowRouteEvidence,
    EscrowSpendResult,
    EscrowTimeoutBroadcast,
    EscrowTimeoutEvidence,
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
    require_channel_input_escrow_backend,
)
from jmswap.channel_funding import ChannelInputEscrowCapability
from jmswap.errors import ChannelInputEscrowError
from jmswap.lightning import PaymentStatus

TXID = "11" * 32
TIMEOUT_TXID = "22" * 32
PREIMAGE = "33" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()


def _terms(**overrides: object) -> ChannelInputEscrowTerms:
    values: dict[str, object] = {
        "snapshot": ChannelSnapshot(
            session_id="session-1",
            channel_id="channel-1",
            funding_outpoint=EscrowOutpoint(txid="44" * 32, vout=2),
            capacity_sats=100_000,
            maker_claim_sats=70_000,
            peer_claim_sats=30_000,
            quiescent=True,
            active_htlcs=0,
        ),
        "authorizer_pubkey": "02" + "66" * 32,
        "payment_hash": PAYMENT_HASH,
        "invoice": "lnbcrt1privateinvoice",
        "invoice_payee_pubkey": "02" + "34" * 32,
        "invoice_payment_secret": "35" * 32,
        "invoice_amount_msat": 32_000_000,
        "invoice_expiry_unix": 2_000_000_000,
        "invoice_min_final_cltv_delta": 18,
        "invoice_verified": True,
        "premium_sats": 2_000,
        "timeout_compensation_sats": 500,
        "escrow_value_sats": 80_000,
        "timeout_fee_sats": 500,
        "timeout_height": 1_000,
        "peer_settle_deadline": 970,
        "success_broadcast_deadline": 980,
        "parent_confirmation_depth": 1,
        "success_confirmation_depth": 1,
        "success_confirmation_budget_blocks": 10,
        "reorg_margin_blocks": 5,
        "safety_delta_blocks": 3,
        "preimage_budget_blocks": 2,
    }
    values.update(overrides)
    return ChannelInputEscrowTerms(**values)


def _prepared(terms: ChannelInputEscrowTerms | None = None) -> PreparedChannelInputEscrow:
    selected_terms = terms or _terms()
    outpoint = EscrowOutpoint(txid=TXID, vout=0)
    return PreparedChannelInputEscrow(
        terms=selected_terms,
        parent_evidence=EscrowParentEvidence(
            parent_tx_hex="0100",
            parent_txid=TXID,
            channel_funding_outpoint=selected_terms.snapshot.funding_outpoint,
            channel_input_index=0,
            escrow_outpoint=outpoint,
            escrow_value_sats=80_000,
            escrow_scriptpubkey="5120" + "55" * 32,
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


def _authorization(**overrides: object) -> ParentAuthorization:
    values: dict[str, object] = {
        "session_id": "session-1",
        "parent_txid": TXID,
        "prepared_parent_tx_hex": "0100",
        "signed_parent_tx_hex": "010000",
        "timeout_txid": TIMEOUT_TXID,
        "channel_funding_outpoint": EscrowOutpoint(txid="44" * 32, vout=2),
        "channel_input_index": 0,
        "signer_pubkey": "02" + "66" * 32,
        "signature": b"signature",
        "sighash": "default",
        "parent_fully_signed": True,
        "signed_parent_txid_verified": True,
        "signature_verified": True,
        "commits_all_inputs": True,
        "commits_all_outputs": True,
    }
    values.update(overrides)
    return ParentAuthorization(**values)


def _parent_broadcast() -> EscrowParentBroadcast:
    return EscrowParentBroadcast(session_id="session-1", parent_txid=TXID, broadcast_height=940)


def _chain_observation(**overrides: object) -> EscrowChainObservation:
    values: dict[str, object] = {
        "session_id": "session-1",
        "parent_txid": TXID,
        "parent_confirmations": 1,
        "in_best_chain": True,
        "observed_height": 950,
    }
    values.update(overrides)
    return EscrowChainObservation(**values)


def _success_spend(**overrides: object) -> EscrowSpendResult:
    values: dict[str, object] = {
        "session_id": "session-1",
        "parent_txid": TXID,
        "spent_escrow_outpoint": EscrowOutpoint(txid=TXID, vout=0),
        "txid": "77" * 32,
        "tx_hex": "0200",
        "escrow_input_value_sats": 80_000,
        "additional_input_value_sats": 0,
        "maker_controlled_output_sats": 79_500,
        "fee_sats": 500,
        "fee_rate_milli_sat_vb": 1_000,
        "fulfillment_height": 978,
        "broadcast_height": 978,
        "rbf_enabled": True,
        "preimage_verified": True,
        "success_path_verified": True,
        "transaction_verified": True,
        "all_outputs_maker_controlled": True,
    }
    values.update(overrides)
    return EscrowSpendResult(**values)


def _recovery_broadcast(*txids: str, height: int) -> RecoveryBroadcastResult:
    return RecoveryBroadcastResult(
        session_id="session-1",
        parent_txid=TXID,
        broadcast_txids=txids,
        broadcast_height=height,
    )


def test_valid_economic_model() -> None:
    terms = _terms()
    assert terms.invoice_amount_msat == 1000 * (terms.snapshot.peer_claim_sats + terms.premium_sats)
    assert terms.snapshot.capacity_sats >= terms.escrow_value_sats


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"invoice_amount_msat": 1}, "invoice amount"),
        ({"escrow_value_sats": 100_001}, "capacity"),
        ({"escrow_value_sats": 31_000}, "escrow value"),
        ({"success_broadcast_deadline": 985}, "broadcast deadline"),
    ],
)
def test_terms_reject_invalid_economics_and_deadlines(
    override: dict[str, object], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        _terms(**override)


def test_terms_require_independently_verified_invoice() -> None:
    with pytest.raises(ValidationError, match="independently verified"):
        _terms(invoice_verified=False)


@pytest.mark.parametrize(
    ("quiescent", "active_htlcs", "maker_claim_sats"),
    [(False, 0, 70_000), (True, 1, 70_000), (True, 0, 69_999)],
)
def test_snapshot_rejects_nonsettled_or_unconserved_state(
    quiescent: bool, active_htlcs: int, maker_claim_sats: int
) -> None:
    with pytest.raises(ValidationError):
        ChannelSnapshot(
            session_id="session-1",
            channel_id="channel-1",
            funding_outpoint=EscrowOutpoint(txid="44" * 32, vout=0),
            capacity_sats=100_000,
            maker_claim_sats=maker_claim_sats,
            peer_claim_sats=30_000,
            quiescent=quiescent,
            active_htlcs=active_htlcs,
        )


@pytest.mark.parametrize(
    ("timeout_override", "match"),
    [
        ({"spent_escrow_outpoint": EscrowOutpoint(txid=TXID, vout=1)}, "exact parent"),
        ({"locktime": 999}, "locktime"),
        ({"peer_output_sats": 30_499}, "peer output"),
        ({"maker_output_sats": 49_001}, "maker output"),
        ({"fee_sats": 499}, "fee"),
    ],
)
def test_prepared_escrow_requires_exact_timeout_equations_and_outpoint(
    timeout_override: dict[str, object], match: str
) -> None:
    terms = _terms()
    prepared = _prepared(terms)
    timeout_values = prepared.timeout_evidence.model_dump()
    timeout_values.update(timeout_override)
    with pytest.raises(ValidationError, match=match):
        PreparedChannelInputEscrow(
            terms=terms,
            parent_evidence=prepared.parent_evidence,
            timeout_evidence=EscrowTimeoutEvidence(**timeout_values),
        )


def test_parent_evidence_requires_independent_checks() -> None:
    with pytest.raises(ValidationError, match="nonmalleable"):
        EscrowParentEvidence(
            parent_tx_hex="0100",
            parent_txid=TXID,
            channel_funding_outpoint=EscrowOutpoint(txid="44" * 32, vout=2),
            channel_input_index=0,
            escrow_outpoint=EscrowOutpoint(txid=TXID, vout=0),
            escrow_value_sats=80_000,
            escrow_scriptpubkey="51",
            all_inputs_nonmalleable=False,
            exact_transaction_verified=True,
        )


def test_prepared_escrow_rejects_parent_value_and_timeout_signature_mismatch() -> None:
    terms = _terms()
    prepared = _prepared(terms)
    with pytest.raises(ValidationError, match="parent escrow value"):
        PreparedChannelInputEscrow(
            terms=terms,
            parent_evidence=prepared.parent_evidence.model_copy(
                update={"escrow_value_sats": 79_999}
            ),
            timeout_evidence=prepared.timeout_evidence,
        )
    with pytest.raises(ValidationError, match="fully signed"):
        PreparedChannelInputEscrow(
            terms=terms,
            parent_evidence=prepared.parent_evidence,
            timeout_evidence=prepared.timeout_evidence.model_copy(update={"fully_signed": False}),
        )
    with pytest.raises(ValidationError, match="frozen channel funding outpoint"):
        PreparedChannelInputEscrow(
            terms=terms,
            parent_evidence=prepared.parent_evidence.model_copy(
                update={"channel_funding_outpoint": EscrowOutpoint(txid="99" * 32, vout=0)}
            ),
            timeout_evidence=prepared.timeout_evidence,
        )


def test_route_requires_channel_exclusion_and_deadline_inequalities() -> None:
    terms = _terms()
    valid = EscrowRouteEvidence(
        part_count=1,
        route_channel_ids=("channel-elsewhere",),
        final_expiry_height=975,
        outgoing_expiry_height=978,
    )
    valid.validate_against(terms)

    for route in (
        valid.model_copy(update={"route_channel_ids": ("channel-1",)}),
        valid.model_copy(update={"final_expiry_height": 970}),
        valid.model_copy(update={"outgoing_expiry_height": 998}),
        valid.model_copy(update={"outgoing_expiry_height": 979}),
    ):
        with pytest.raises(ChannelInputEscrowError):
            route.validate_against(terms)


def test_payment_request_requires_fresh_confirmed_exact_parent() -> None:
    prepared = _prepared()
    observation = EscrowChainObservation(
        session_id="session-1",
        parent_txid=TXID,
        parent_confirmations=1,
        in_best_chain=True,
        observed_height=950,
    )
    request = SinglePartPaymentRequest(
        prepared=prepared,
        chain_observation=observation,
        excluded_channel_ids=("channel-1",),
        requested_at_height=950,
        requested_at_unix=1_900_000_000,
    )
    assert request.chain_observation.parent_txid == TXID

    invalid_observations = (
        observation.model_copy(update={"parent_confirmations": 0}),
        observation.model_copy(update={"parent_txid": "99" * 32}),
        observation.model_copy(update={"in_best_chain": False, "parent_confirmations": 0}),
    )
    for invalid in invalid_observations:
        with pytest.raises(ValidationError):
            SinglePartPaymentRequest(
                prepared=prepared,
                chain_observation=invalid,
                excluded_channel_ids=("channel-1",),
                requested_at_height=950,
                requested_at_unix=1_900_000_000,
            )
    with pytest.raises(ValidationError, match="fresh"):
        SinglePartPaymentRequest(
            prepared=prepared,
            chain_observation=observation,
            excluded_channel_ids=("channel-1",),
            requested_at_height=951,
            requested_at_unix=1_900_000_000,
        )
    with pytest.raises(ValidationError, match="expired"):
        SinglePartPaymentRequest(
            prepared=prepared,
            chain_observation=observation,
            excluded_channel_ids=("channel-1",),
            requested_at_height=950,
            requested_at_unix=2_000_000_000,
        )


def test_payment_result_requires_matching_success_preimage() -> None:
    route = EscrowRouteEvidence(
        part_count=1,
        route_channel_ids=("channel-elsewhere",),
        final_expiry_height=975,
        outgoing_expiry_height=978,
    )
    result = SinglePartPaymentResult(
        payment_id="payment-1",
        status=PaymentStatus.SUCCEEDED,
        preimage=PREIMAGE,
        attempt_height=950,
        observed_height=978,
        settled_height=978,
        route=route,
    )
    result.validate_against(_terms())

    with pytest.raises(ChannelInputEscrowError, match="preimage"):
        result.model_copy(update={"preimage": "99" * 32}).validate_against(_terms())
    with pytest.raises(ValidationError, match="preimage and settlement height"):
        SinglePartPaymentResult(
            payment_id="payment-1",
            status=PaymentStatus.SUCCEEDED,
            attempt_height=950,
            observed_height=978,
            route=route,
        )

    with pytest.raises(ChannelInputEscrowError, match="minimum final CLTV"):
        result.model_copy(update={"attempt_height": 958}).validate_against(_terms())

    request = SuccessSpendRequest(
        prepared=_prepared(),
        payment_result=result,
    )
    assert request.payment_result.preimage == PREIMAGE
    with pytest.raises(ValidationError, match="outgoing HTLC expiry"):
        SuccessSpendRequest(
            prepared=_prepared(),
            payment_result=result.model_copy(
                update={"observed_height": 979, "settled_height": 979}
            ),
        )


def test_chain_observation_rejects_incoherent_conflict_data() -> None:
    with pytest.raises(ValidationError, match="best-chain"):
        EscrowChainObservation(
            session_id="session-1",
            parent_txid=TXID,
            parent_confirmations=1,
            in_best_chain=True,
            conflict_txid="66" * 32,
            observed_height=1_000,
        )
    with pytest.raises(ValidationError, match="cannot both confirm"):
        EscrowChainObservation(
            session_id="session-1",
            parent_txid=TXID,
            parent_confirmations=1,
            in_best_chain=True,
            success_confirmations=1,
            timeout_confirmations=1,
            observed_height=1_000,
        )


def test_record_rejects_illegal_transition_is_idempotent_and_tracks_reorg() -> None:
    terms = _terms()
    record = ChannelInputEscrowRecord(terms=terms)
    with pytest.raises(ChannelInputEscrowError, match="cannot transition"):
        record.advance(EscrowLifecycleState.PARENT_SIGNED)

    record.attach_prepared(_prepared(terms))
    record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
    timestamp = record.updated_at
    record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
    assert record.updated_at == timestamp
    record.attach_authorization(_authorization())
    record.advance(EscrowLifecycleState.PARENT_SIGNED)
    with pytest.raises(ChannelInputEscrowError, match="broadcast evidence"):
        record.advance(EscrowLifecycleState.PARENT_PENDING)
    record.attach_parent_broadcast(_parent_broadcast())
    record.advance(EscrowLifecycleState.PARENT_PENDING)
    with pytest.raises(ChannelInputEscrowError, match="confirmation evidence"):
        record.advance(EscrowLifecycleState.PARENT_CONFIRMED)
    record.attach_chain_observation(_chain_observation())
    record.advance(EscrowLifecycleState.PARENT_CONFIRMED)
    record.attach_chain_observation(
        _chain_observation(parent_confirmations=0, in_best_chain=False, observed_height=951)
    )
    record.advance(EscrowLifecycleState.PARENT_REORGED)
    assert record.state_before_reorg is EscrowLifecycleState.PARENT_CONFIRMED
    conflict_record = record.model_copy(deep=True)
    conflict_record.attach_chain_observation(
        _chain_observation(
            parent_confirmations=0,
            in_best_chain=False,
            conflict_txid="88" * 32,
            conflict_confirmations=1,
            conflicting_channel_outpoint=terms.snapshot.funding_outpoint,
            conflict_spends_channel=True,
            observed_height=951,
        )
    )
    conflict_record.advance(EscrowLifecycleState.COMMITMENT_CONFLICT)
    with pytest.raises(ChannelInputEscrowError, match="rebroadcast attempt"):
        record.advance(EscrowLifecycleState.PARENT_CONFIRMED)
    record.attach_recovery_broadcast(_recovery_broadcast(TXID, height=951))
    record.attach_chain_observation(_chain_observation(observed_height=952))
    record.advance(EscrowLifecycleState.PARENT_CONFIRMED)
    assert record.state_before_reorg is None


def test_record_cannot_abort_after_parent_authorization() -> None:
    terms = _terms()
    record = ChannelInputEscrowRecord(terms=terms)
    record.attach_prepared(_prepared(terms))
    record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
    record.attach_authorization(_authorization(sighash="all"))
    record.advance(EscrowLifecycleState.PARENT_SIGNED)

    with pytest.raises(ChannelInputEscrowError, match="cannot transition"):
        record.advance(EscrowLifecycleState.ABORTED)


def test_reorg_tracks_durable_descendant_broadcast_obligation() -> None:
    terms = _terms()
    prepared = _prepared(terms)
    success_record = ChannelInputEscrowRecord(terms=terms)
    success_record.attach_prepared(prepared)
    success_record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
    success_record.attach_authorization(_authorization())
    success_record.advance(EscrowLifecycleState.PARENT_SIGNED)
    success_record.attach_parent_broadcast(_parent_broadcast())
    success_record.advance(EscrowLifecycleState.PARENT_PENDING)
    success_record.attach_chain_observation(_chain_observation())
    success_record.advance(EscrowLifecycleState.PARENT_CONFIRMED)
    success_record.attach_payment_request(
        SinglePartPaymentRequest(
            prepared=prepared,
            chain_observation=_chain_observation(),
            excluded_channel_ids=("channel-1",),
            requested_at_height=950,
            requested_at_unix=1_900_000_000,
        )
    )
    pending = SinglePartPaymentResult(
        payment_id="payment-1",
        status=PaymentStatus.PENDING,
        attempt_height=950,
        observed_height=950,
        route=EscrowRouteEvidence(
            part_count=1,
            route_channel_ids=("channel-elsewhere",),
            final_expiry_height=975,
            outgoing_expiry_height=978,
        ),
    )
    success_record.attach_payment_result(pending)
    success_record.advance(EscrowLifecycleState.PAYMENT_IN_FLIGHT)
    success_record.attach_payment_result(
        pending.model_copy(
            update={
                "status": PaymentStatus.SUCCEEDED,
                "preimage": PREIMAGE,
                "observed_height": 978,
                "settled_height": 978,
            }
        )
    )
    success_record.attach_success_spend(_success_spend())
    success_record.attach_chain_observation(
        _chain_observation(parent_confirmations=0, in_best_chain=False, observed_height=979)
    )
    success_record.advance(EscrowLifecycleState.PARENT_REORGED)
    assert success_record.state_before_reorg is EscrowLifecycleState.SUCCESS_SPEND_PENDING

    timeout_record = success_record.model_copy(
        deep=True,
        update={
            "state": EscrowLifecycleState.PARENT_CONFIRMED,
            "state_before_reorg": None,
            "recovery_required": False,
            "payment_request": None,
            "payment_result": None,
            "success_spends": (),
            "chain_observation": _chain_observation(),
        },
    )
    timeout_record.attach_timeout_broadcast(
        EscrowTimeoutBroadcast(
            session_id="session-1",
            parent_txid=TXID,
            timeout_txid=TIMEOUT_TXID,
            broadcast_height=1_001,
        )
    )
    timeout_record.attach_chain_observation(
        _chain_observation(parent_confirmations=0, in_best_chain=False, observed_height=1_001)
    )
    timeout_record.advance(EscrowLifecycleState.PARENT_REORGED)
    assert timeout_record.state_before_reorg is EscrowLifecycleState.TIMEOUT_PENDING


def test_parent_authorization_requires_timeout_signed_and_exact_artifacts() -> None:
    terms = _terms()
    record = ChannelInputEscrowRecord(terms=terms)
    record.attach_prepared(_prepared(terms))
    authorization = _authorization()
    with pytest.raises(ChannelInputEscrowError, match="timeout_signed"):
        record.attach_authorization(authorization)
    record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
    record.attach_authorization(authorization)
    assert record.authorization == authorization
    with pytest.raises(ValidationError, match="independently verified"):
        _authorization(signature_verified=False)


def test_record_requires_route_and_success_spend_evidence(tmp_path: Path) -> None:
    terms = _terms()
    prepared = _prepared(terms)
    record = ChannelInputEscrowRecord(terms=terms)
    record.attach_prepared(prepared)
    record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
    record.attach_authorization(_authorization())
    record.advance(EscrowLifecycleState.PARENT_SIGNED)
    record.attach_parent_broadcast(_parent_broadcast())
    record.advance(EscrowLifecycleState.PARENT_PENDING)
    record.attach_chain_observation(_chain_observation())
    record.advance(EscrowLifecycleState.PARENT_CONFIRMED)
    with pytest.raises(ChannelInputEscrowError, match="durable request"):
        record.advance(EscrowLifecycleState.PAYMENT_IN_FLIGHT)

    route = EscrowRouteEvidence(
        part_count=1,
        route_channel_ids=("channel-elsewhere",),
        final_expiry_height=975,
        outgoing_expiry_height=978,
    )
    payment = SinglePartPaymentResult(
        payment_id="payment-1",
        status=PaymentStatus.PENDING,
        attempt_height=950,
        observed_height=950,
        route=route,
    )
    record.attach_payment_request(
        SinglePartPaymentRequest(
            prepared=prepared,
            chain_observation=_chain_observation(),
            excluded_channel_ids=("channel-1",),
            requested_at_height=950,
            requested_at_unix=1_900_000_000,
        )
    )
    record.attach_payment_result(payment)
    record.advance(EscrowLifecycleState.PAYMENT_IN_FLIGHT)
    with pytest.raises(ChannelInputEscrowError, match="broadcast spend"):
        record.advance(EscrowLifecycleState.SUCCESS_SPEND_PENDING)
    record.attach_chain_observation(
        _chain_observation(parent_confirmations=0, in_best_chain=False, observed_height=951)
    )
    record.advance(EscrowLifecycleState.PARENT_REORGED)

    settled_payment = payment.model_copy(
        update={
            "status": PaymentStatus.SUCCEEDED,
            "preimage": PREIMAGE,
            "observed_height": 978,
            "settled_height": 978,
        }
    )
    record.attach_payment_result(settled_payment)
    with pytest.raises(ChannelInputEscrowError, match="terminal payment evidence"):
        record.attach_payment_result(settled_payment.model_copy(update={"observed_height": 979}))

    with pytest.raises(ChannelInputEscrowError, match="escrow outpoint"):
        record.attach_success_spend(
            _success_spend(spent_escrow_outpoint=EscrowOutpoint(txid=TXID, vout=1))
        )
    spend = _success_spend()
    record.attach_success_spend(spend)
    assert record.state is EscrowLifecycleState.PARENT_REORGED
    assert record.state_before_reorg is EscrowLifecycleState.SUCCESS_SPEND_PENDING
    loss_record = record.model_copy(deep=True)
    loss_record.attach_chain_observation(
        _chain_observation(
            parent_confirmations=0,
            in_best_chain=False,
            conflict_txid="88" * 32,
            conflict_confirmations=1,
            conflicting_channel_outpoint=terms.snapshot.funding_outpoint,
            conflict_spends_channel=True,
            observed_height=978,
        )
    )
    loss_record.advance(EscrowLifecycleState.CROSS_LAYER_LOSS)
    with pytest.raises(ChannelInputEscrowError, match="omitted a required"):
        record.attach_recovery_broadcast(_recovery_broadcast(TXID, height=978))
    record.attach_recovery_broadcast(_recovery_broadcast(TXID, "77" * 32, height=978))
    record.attach_chain_observation(_chain_observation(observed_height=979))
    record.advance(EscrowLifecycleState.SUCCESS_SPEND_PENDING)
    assert record.peer_settle_deadline_violated
    replacement = _success_spend(
        txid="78" * 32,
        tx_hex="0300",
        maker_controlled_output_sats=79_400,
        fee_sats=600,
        fee_rate_milli_sat_vb=1_200,
        replacement_policy_verified=True,
        replaces_txid="77" * 32,
    )
    record.attach_success_spend(replacement)
    store = ChannelInputEscrowStore(tmp_path)
    store.save(record)
    recovered = store.load("session-1")
    assert recovered is not None
    assert recovered.payment_result is not None
    assert recovered.payment_result.preimage == PREIMAGE
    assert recovered.success_spend is not None
    assert recovered.success_spend.tx_hex == "0300"
    assert len(recovered.success_spends) == 2

    with pytest.raises(ChannelInputEscrowError, match="success evidence"):
        record.advance(EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED)
    record.attach_chain_observation(
        _chain_observation(
            parent_confirmations=2,
            success_txid="78" * 32,
            success_confirmations=1,
            observed_height=980,
        )
    )
    record.advance(EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED)
    record.attach_chain_observation(
        _chain_observation(parent_confirmations=0, in_best_chain=False, observed_height=981)
    )
    record.advance(EscrowLifecycleState.PARENT_REORGED)
    assert record.state_before_reorg is EscrowLifecycleState.SUCCESS_SPEND_PENDING
    record.attach_recovery_broadcast(_recovery_broadcast(TXID, "78" * 32, height=981))
    record.attach_chain_observation(_chain_observation(observed_height=982))
    record.advance(EscrowLifecycleState.SUCCESS_SPEND_PENDING)


def test_record_requires_exact_mature_timeout_broadcast() -> None:
    terms = _terms()
    prepared = _prepared(terms)
    record = ChannelInputEscrowRecord(terms=terms)
    record.attach_prepared(prepared)
    record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
    record.attach_authorization(_authorization())
    record.advance(EscrowLifecycleState.PARENT_SIGNED)
    record.attach_parent_broadcast(_parent_broadcast())
    record.advance(EscrowLifecycleState.PARENT_PENDING)
    record.attach_chain_observation(_chain_observation())
    record.advance(EscrowLifecycleState.PARENT_CONFIRMED)
    with pytest.raises(ChannelInputEscrowError, match="broadcast timeout"):
        record.advance(EscrowLifecycleState.TIMEOUT_PENDING)
    with pytest.raises(ChannelInputEscrowError, match="eligible height"):
        record.attach_timeout_broadcast(
            EscrowTimeoutBroadcast(
                session_id="session-1",
                parent_txid=TXID,
                timeout_txid=TIMEOUT_TXID,
                broadcast_height=1_000,
            )
        )

    record.attach_timeout_broadcast(
        EscrowTimeoutBroadcast(
            session_id="session-1",
            parent_txid=TXID,
            timeout_txid=TIMEOUT_TXID,
            broadcast_height=1_001,
        )
    )
    record.attach_chain_observation(_chain_observation(observed_height=1_001))
    record.advance(EscrowLifecycleState.TIMEOUT_PENDING)
    with pytest.raises(ChannelInputEscrowError, match="exact-timeout evidence"):
        record.advance(EscrowLifecycleState.TIMEOUT_CONFIRMED)
    record.attach_chain_observation(
        _chain_observation(
            parent_confirmations=2,
            timeout_txid=TIMEOUT_TXID,
            timeout_confirmations=1,
            observed_height=1_002,
        )
    )
    record.advance(EscrowLifecycleState.TIMEOUT_CONFIRMED)


def test_store_atomic_round_trip_and_corrupt_list_behavior(tmp_path: Path) -> None:
    store = ChannelInputEscrowStore(tmp_path)
    record = ChannelInputEscrowRecord(terms=_terms())
    store.save(record)
    loaded = store.load("session-1")
    assert loaded is not None
    assert loaded.terms == record.terms
    loaded.attach_prepared(_prepared(loaded.terms))
    store.save(loaded, expected_revision=record.revision)
    with pytest.raises(ChannelInputEscrowError, match="newer escrow recovery state"):
        store.save(record)
    record.attach_prepared(_prepared(record.terms))
    record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
    with pytest.raises(ChannelInputEscrowError, match="expected revision has changed"):
        store.save(record, expected_revision=0)
    assert len(list(tmp_path.glob("*.json"))) == 1

    corrupt_path = tmp_path / "corrupt.json"
    corrupt_path.write_text("not json")
    assert [saved.session_id for saved in store.list()] == ["session-1"]
    assert corrupt_path.exists()


def test_negotiated_parent_intent_is_exact_and_immutable() -> None:
    terms = _terms()
    record = ChannelInputEscrowRecord(terms=terms)
    parent = _prepared(terms).parent_evidence
    record.bind_negotiated_parent(parent)
    revision = record.revision
    record.bind_negotiated_parent(parent)
    assert record.revision == revision
    with pytest.raises(ChannelInputEscrowError, match="immutable"):
        record.bind_negotiated_parent(parent.model_copy(update={"parent_tx_hex": "0300"}))


def test_store_hashes_session_paths_without_collisions(tmp_path: Path) -> None:
    store = ChannelInputEscrowStore(tmp_path)
    for session_id in ("../session", ".._session"):
        record = ChannelInputEscrowRecord(
            terms=_terms(
                snapshot=ChannelSnapshot(
                    session_id=session_id,
                    channel_id="channel-1",
                    funding_outpoint=EscrowOutpoint(txid="44" * 32, vout=2),
                    capacity_sats=100_000,
                    maker_claim_sats=70_000,
                    peer_claim_sats=30_000,
                    quiescent=True,
                    active_htlcs=0,
                )
            )
        )
        store.save(record)
        assert store.load(session_id) is not None
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert not (tmp_path.parent / "session.json").exists()


def test_store_rejects_incomplete_state_and_mismatched_session_file(tmp_path: Path) -> None:
    store = ChannelInputEscrowStore(tmp_path)
    incomplete = ChannelInputEscrowRecord(terms=_terms()).model_copy(
        update={"state": EscrowLifecycleState.PARENT_SIGNED}
    )
    with pytest.raises(ValidationError, match="prepared timeout evidence"):
        store.save(incomplete)

    store.save(ChannelInputEscrowRecord(terms=_terms()))

    second = ChannelInputEscrowRecord(
        terms=_terms(
            snapshot=ChannelSnapshot(
                session_id="session-2",
                channel_id="channel-1",
                funding_outpoint=EscrowOutpoint(txid="44" * 32, vout=2),
                capacity_sats=100_000,
                maker_claim_sats=70_000,
                peer_claim_sats=30_000,
                quiescent=True,
                active_htlcs=0,
            )
        )
    )
    store.save(second)
    first_path = tmp_path / f"{hashlib.sha256(b'session-1').hexdigest()}.json"
    second_path = tmp_path / f"{hashlib.sha256(b'session-2').hexdigest()}.json"
    first_path.write_text(second_path.read_text())
    with pytest.raises(ChannelInputEscrowError, match="does not match"):
        store.load("session-1")
    assert [record.session_id for record in store.list()] == ["session-2"]


def test_runtime_protocol_structural_acceptance_and_rejection() -> None:
    class CompleteBackend:
        channel_input_escrow_capabilities = frozenset(ChannelInputEscrowCapability)

        async def freeze_quiesce_channel(self, request: FreezeChannelRequest) -> ChannelSnapshot:
            raise NotImplementedError

        async def prepare_signed_timeout(
            self, request: PrepareTimeoutRequest
        ) -> PreparedChannelInputEscrow:
            raise NotImplementedError

        async def authorize_parent(
            self, request: ParentAuthorizationRequest
        ) -> ParentAuthorization:
            raise NotImplementedError

        async def broadcast_parent(self, request: ParentBroadcastRequest) -> EscrowParentBroadcast:
            raise NotImplementedError

        async def observe_escrow_chain(
            self, request: ChainObservationRequest
        ) -> EscrowChainObservation:
            raise NotImplementedError

        async def send_single_part_payment(
            self, request: SinglePartPaymentRequest
        ) -> SinglePartPaymentResult:
            raise NotImplementedError

        async def observe_escrow_payment(
            self, request: PaymentObservationRequest
        ) -> SinglePartPaymentResult:
            raise NotImplementedError

        async def build_broadcast_success_spend(
            self, request: SuccessSpendRequest
        ) -> EscrowSpendResult:
            raise NotImplementedError

        async def broadcast_timeout_recovery(
            self, request: RecoveryRequest
        ) -> EscrowTimeoutBroadcast:
            raise NotImplementedError

        async def fee_bump_success_spend(self, request: RecoveryRequest) -> EscrowSpendResult:
            raise NotImplementedError

        async def rebroadcast_recovery_artifacts(
            self, request: RecoveryRequest
        ) -> RecoveryBroadcastResult:
            raise NotImplementedError

        async def resume_durable_sessions(self) -> list[ChannelInputEscrowRecord]:
            raise NotImplementedError

    class IncompleteBackend:
        channel_input_escrow_capabilities = frozenset(ChannelInputEscrowCapability)

    class MissingCapabilities(CompleteBackend):
        channel_input_escrow_capabilities = frozenset()

    assert isinstance(CompleteBackend(), ChannelInputEscrowBackend)
    assert not isinstance(IncompleteBackend(), ChannelInputEscrowBackend)
    assert require_channel_input_escrow_backend(CompleteBackend())
    with pytest.raises(ChannelInputEscrowError, match="adapter contract"):
        require_channel_input_escrow_backend(IncompleteBackend())
    with pytest.raises(ChannelInputEscrowError, match="capabilities"):
        require_channel_input_escrow_backend(MissingCapabilities())
