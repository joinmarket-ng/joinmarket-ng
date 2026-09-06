from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    estimate_vsize,
    get_txid,
    parse_transaction_bytes,
    serialize_transaction,
)
from jmcore.channel_input import (
    MAX_CHANNELS,
    ChannelDescriptor,
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputCancelPayload,
    ChannelInputFinalizePayload,
    ChannelInputInvitePayload,
    ChannelInputKeyPair,
    ChannelInputPartialPayload,
    ChannelInputPreparedPayload,
    ChannelInputPreparePayload,
    ChannelInputProposalPayload,
    ChannelInputSignPayload,
    ChannelInputStatusRequestPayload,
    ChannelInputTranscript,
    ChannelInputValidationError,
    EndpointTerms,
    InputPrevout,
    Outpoint,
    PolicyCeilings,
    SettlementOutput,
    approval_set_hash,
    cancellation_binding_hash,
    endpoint_terms_hash,
    sign_payload,
    transcript_hash,
)
from jmcore.channel_input_config import ChannelInputConfig
from jmswap.lnd import (
    FINAL_TAPROOT_COMMITMENT,
    ChannelInputCandidate,
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
)

from maker.channel_input import (
    MakerChannelInputChannelState,
    MakerChannelInputFreezeIntent,
    MakerChannelInputNegotiation,
    MakerChannelInputSnapshot,
    MakerChannelInputStage,
    MakerFrozenChannelInput,
)

SESSION_ID = "11" * 32
COORDINATOR_SECRET = (1).to_bytes(32, "big")
PARTICIPANT_SECRET = (2).to_bytes(32, "big")


def _node(index: int) -> str:
    return bytes(CKey.from_secret_bytes(index.to_bytes(32, "big")).pub).hex()


def _config(*, points: tuple[str, ...], peers: tuple[str, ...]) -> ChannelInputConfig:
    return ChannelInputConfig(
        enabled=True,
        lnd_grpc_url="localhost:10009",
        lnd_tls_cert_path=Path("/tmp/lnd.cert"),
        lnd_macaroon_path=Path("/tmp/admin.macaroon"),
        lnd_escrow_macaroon_path=Path("/tmp/escrow.macaroon"),
        allowed_channel_points=points,
        allowed_peer_node_pubkeys=peers,
        min_channel_capacity_sat=100_000,
        max_channel_capacity_sat=100_000,
        max_claim_adjustment_sat=1_000,
        max_miner_fee_share_sat=1_000,
        recovery_delay_blocks=20,
    )


def _invite() -> ChannelInputInvitePayload:
    coordinator = ChannelInputKeyPair.from_secret(COORDINATOR_SECRET)
    return cast(
        ChannelInputInvitePayload,
        sign_payload(
            ChannelInputInvitePayload(
                session_id=SESSION_ID,
                attempt=1,
                signer_key=coordinator.public_key,
                network="regtest",
                expiry=2_000,
                max_parent_fee_sat=2_000,
                max_fee_rate_sat_vb=100,
                min_recovery_delta=10,
                max_recovery_delta=50,
            ),
            COORDINATOR_SECRET,
            aux_randomness=b"\x01" * 32,
        ),
    )


def _frozen(index: int) -> MakerFrozenChannelInput:
    funding_outpoint = EscrowOutpoint(txid=f"{index + 10:064x}", vout=index)
    return MakerFrozenChannelInput(
        snapshot=FrozenChannelSnapshot(
            session_id=SESSION_ID,
            channel_id=f"{funding_outpoint.txid}:{funding_outpoint.vout}",
            funding_outpoint=funding_outpoint,
            funding_scriptpubkey=bytes.fromhex("5120" + f"{index + 20:064x}"),
            peer_node_pubkey=bytes.fromhex(_node(index + 10)),
            local_funding_pubkey=bytes.fromhex(_node(index + 20)),
            remote_funding_pubkey=bytes.fromhex(_node(index + 30)),
            capacity_sat=100_000,
            local_claim_sat=50_000,
            remote_claim_sat=50_000,
        ),
        settlement_script_pubkey="5120" + f"{index + 40:064x}",
    )


def _candidate(frozen: MakerFrozenChannelInput, *, channel_id: int = 1) -> ChannelInputCandidate:
    snapshot = frozen.snapshot
    return ChannelInputCandidate(
        channel_id=channel_id,
        funding_outpoint=snapshot.funding_outpoint,
        peer_node_pubkey=snapshot.peer_node_pubkey.hex(),
        capacity_sat=snapshot.capacity_sat,
        local_balance_sat=snapshot.local_claim_sat,
        remote_balance_sat=snapshot.remote_claim_sat,
        private=True,
        active=True,
        default_channel_state=True,
        pending_htlc_count=0,
        commitment_type=FINAL_TAPROOT_COMMITMENT,
    )


def _plan_freeze(
    negotiation: MakerChannelInputNegotiation,
    frozen: tuple[MakerFrozenChannelInput, ...],
) -> tuple[MakerChannelInputFreezeIntent, ...]:
    return negotiation.plan_freeze(
        tuple(_candidate(item, channel_id=index) for index, item in enumerate(frozen)),
        settlement_scripts=_settlement_scripts(frozen),
    )


def _settlement_scripts(
    frozen: tuple[MakerFrozenChannelInput, ...],
) -> dict[str, str]:
    return {
        f"{item.snapshot.funding_outpoint.txid}:{item.snapshot.funding_outpoint.vout}": (
            item.settlement_script_pubkey
        )
        for item in frozen
    }


def _negotiation(
    channels: int = 2,
) -> tuple[MakerChannelInputNegotiation, tuple[MakerFrozenChannelInput, ...]]:
    frozen = tuple(_frozen(index) for index in range(channels))
    negotiation = MakerChannelInputNegotiation.from_invite(
        local_actor="maker",
        coordinator_actor="coordinator",
        invite=_invite(),
        config=_config(
            points=tuple(
                f"{item.snapshot.funding_outpoint.txid}:{item.snapshot.funding_outpoint.vout}"
                for item in frozen
            ),
            peers=tuple(item.snapshot.peer_node_pubkey.hex() for item in frozen),
        ),
        network="regtest",
        local_node_key=_node(3),
        key_pair=ChannelInputKeyPair.from_secret(PARTICIPANT_SECRET),
    )
    return negotiation, frozen


def _proposal(
    negotiation: MakerChannelInputNegotiation,
) -> tuple[ChannelInputProposalPayload, tuple[EndpointTerms, ...]]:
    participant = negotiation.participant_key
    coordinator = ChannelInputKeyPair.from_secret(COORDINATOR_SECRET).public_key
    inputs: list[TxInput] = []
    prevouts: list[InputPrevout] = []
    outputs: list[TxOutput] = []
    maker_terms: list[EndpointTerms] = []
    descriptors: list[ChannelDescriptor] = []
    for index, hello in enumerate(negotiation.hellos):
        inputs.append(TxInput.from_hex(hello.funding_outpoint.txid, hello.funding_outpoint.vout))
        prevouts.append(
            InputPrevout(
                outpoint=hello.funding_outpoint,
                value_sat=hello.capacity_sat,
                script_pubkey=hello.funding_script_pubkey,
            )
        )
        maker_output = SettlementOutput(
            output_index=index * 2,
            amount_sat=49_000,
            script_pubkey=hello.settlement_script_pubkey,
        )
        coordinator_output = SettlementOutput(
            output_index=index * 2 + 1,
            amount_sat=50_000,
            script_pubkey="5120" + f"{index + 60:064x}",
        )
        maker = EndpointTerms(
            session_id=SESSION_ID,
            attempt=1,
            funding_outpoint=hello.funding_outpoint,
            input_index=index,
            participant_key=participant,
            role="first",
            local_node_key=hello.node_key,
            peer_node_key=hello.peer_node_key,
            capacity_sat=100_000,
            frozen_local_claim_sat=50_000,
            frozen_remote_claim_sat=50_000,
            settlement_outputs=(maker_output,),
            miner_fee_share_sat=1_000,
            claim_adjustment_sat=0,
            policy_ceilings=hello.policy_ceilings,
        )
        coordinator_terms = EndpointTerms(
            session_id=SESSION_ID,
            attempt=1,
            funding_outpoint=hello.funding_outpoint,
            input_index=index,
            participant_key=coordinator,
            role="second",
            local_node_key=hello.peer_node_key,
            peer_node_key=hello.node_key,
            capacity_sat=100_000,
            frozen_local_claim_sat=50_000,
            frozen_remote_claim_sat=50_000,
            settlement_outputs=(coordinator_output,),
            miner_fee_share_sat=0,
            claim_adjustment_sat=0,
            policy_ceilings=PolicyCeilings(
                max_miner_fee_share_sat=1_000,
                max_claim_adjustment_sat=1_000,
                max_settlement_outputs=1,
            ),
        )
        maker_terms.append(maker)
        descriptors.append(
            ChannelDescriptor(
                funding_outpoint=hello.funding_outpoint,
                input_index=index,
                capacity_sat=100_000,
                first_participant_key=participant,
                second_participant_key=coordinator,
                first_terms_hash=endpoint_terms_hash(maker).hex(),
                second_terms_hash=endpoint_terms_hash(coordinator_terms).hex(),
            )
        )
        outputs.extend(
            (
                TxOutput(
                    value=maker_output.amount_sat,
                    script=bytes.fromhex(maker_output.script_pubkey),
                ),
                TxOutput(
                    value=coordinator_output.amount_sat,
                    script=bytes.fromhex(coordinator_output.script_pubkey),
                ),
            )
        )
    total_fee = 1_000 * len(negotiation.hellos)
    estimated_vsize = estimate_vsize(["p2tr"] * len(inputs), ["p2tr"] * len(outputs))
    transcript = ChannelInputTranscript(
        network="regtest",
        session_id=SESSION_ID,
        attempt=1,
        unsigned_parent_tx=serialize_transaction(2, inputs, outputs, 0).hex(),
        input_prevouts=tuple(prevouts),
        total_fee_sat=total_fee,
        fee_rate_sat_vb=(total_fee + estimated_vsize - 1) // estimated_vsize,
        expiry=1_900,
        recovery_height=120,
        participant_keys=(coordinator, participant),
        coordinator_key=coordinator,
        channels=tuple(descriptors),
    )
    proposal = cast(
        ChannelInputProposalPayload,
        sign_payload(
            ChannelInputProposalPayload(
                session_id=SESSION_ID,
                attempt=1,
                signer_key=coordinator,
                recipient_key=participant,
                transcript=transcript,
                transcript_hash=transcript_hash(transcript).hex(),
                recipient_terms=tuple(maker_terms),
            ),
            COORDINATOR_SECRET,
            aux_randomness=b"\x02" * 32,
        ),
    )
    return proposal, tuple(maker_terms)


def _approval_set(
    proposal: ChannelInputProposalPayload,
    local_approval: ChannelInputApprovalPayload,
) -> ChannelInputApprovalSetPayload:
    transcript = proposal.transcript
    coordinator = transcript.coordinator_key
    coordinator_approval = cast(
        ChannelInputApprovalPayload,
        sign_payload(
            ChannelInputApprovalPayload(
                session_id=SESSION_ID,
                attempt=1,
                signer_key=coordinator,
                transcript_hash=proposal.transcript_hash,
                terms_hashes=tuple(sorted(transcript.terms_hashes_for(coordinator))),
            ),
            COORDINATOR_SECRET,
            aux_randomness=b"\x03" * 32,
        ),
    )
    return cast(
        ChannelInputApprovalSetPayload,
        sign_payload(
            ChannelInputApprovalSetPayload(
                session_id=SESSION_ID,
                attempt=1,
                signer_key=coordinator,
                transcript=transcript,
                approvals=(coordinator_approval, local_approval),
            ),
            COORDINATOR_SECRET,
            aux_randomness=b"\x04" * 32,
        ),
    )


def _prepare(
    approval_set: ChannelInputApprovalSetPayload,
    local_ack: ChannelInputApprovalSetAckPayload,
) -> ChannelInputPreparePayload:
    transcript = approval_set.transcript
    coordinator = transcript.coordinator_key
    coordinator_ack = cast(
        ChannelInputApprovalSetAckPayload,
        sign_payload(
            ChannelInputApprovalSetAckPayload(
                session_id=SESSION_ID,
                attempt=1,
                signer_key=coordinator,
                transcript_hash=transcript_hash(transcript).hex(),
                approval_set_hash=approval_set_hash(approval_set).hex(),
            ),
            COORDINATOR_SECRET,
            aux_randomness=b"\x05" * 32,
        ),
    )
    return cast(
        ChannelInputPreparePayload,
        sign_payload(
            ChannelInputPreparePayload(
                session_id=SESSION_ID,
                attempt=1,
                signer_key=coordinator,
                transcript_hash=transcript_hash(transcript).hex(),
                approval_set_hash=approval_set_hash(approval_set).hex(),
                approval_set=approval_set,
                approval_set_acks=(coordinator_ack, local_ack),
            ),
            COORDINATOR_SECRET,
            aux_randomness=b"\x06" * 32,
        ),
    )


def _cancel(
    proposal: ChannelInputProposalPayload, *, reason_code: str = "coordinator_abort"
) -> ChannelInputCancelPayload:
    coordinator = ChannelInputKeyPair.from_secret(COORDINATOR_SECRET)
    return cast(
        ChannelInputCancelPayload,
        sign_payload(
            ChannelInputCancelPayload(
                session_id=proposal.session_id,
                attempt=proposal.attempt,
                signer_key=coordinator.public_key,
                transcript_hash=proposal.transcript_hash,
                reason_code=reason_code,
            ),
            COORDINATOR_SECRET,
        ),
    )


def _pretranscript_cancel(
    negotiation: MakerChannelInputNegotiation, *, binding: str | None = None
) -> ChannelInputCancelPayload:
    coordinator = ChannelInputKeyPair.from_secret(COORDINATOR_SECRET)
    return cast(
        ChannelInputCancelPayload,
        sign_payload(
            ChannelInputCancelPayload(
                session_id=negotiation.invite.session_id,
                attempt=negotiation.invite.attempt,
                signer_key=coordinator.public_key,
                transcript_hash=binding or cancellation_binding_hash(negotiation.invite).hex(),
                reason_code="coordinator_abort",
            ),
            COORDINATOR_SECRET,
        ),
    )


def _proposal_approved() -> tuple[
    MakerChannelInputNegotiation,
    ChannelInputProposalPayload,
    ChannelInputApprovalPayload,
]:
    negotiation, frozen = _negotiation()
    _plan_freeze(negotiation, frozen)
    negotiation.install_frozen_inputs(frozen)
    proposal, _ = _proposal(negotiation)
    approval = negotiation.accept_proposal(
        "coordinator", proposal, current_time=1_000, current_height=100
    )
    return negotiation, proposal, approval


def _prepared_negotiation(
    channels: int = 2,
) -> tuple[MakerChannelInputNegotiation, ChannelInputProposalPayload]:
    negotiation, frozen = _negotiation(channels)
    _plan_freeze(negotiation, frozen)
    negotiation.install_frozen_inputs(frozen)
    proposal, _ = _proposal(negotiation)
    approval = negotiation.accept_proposal(
        "coordinator", proposal, current_time=1_000, current_height=100
    )
    approval_set = _approval_set(proposal, approval)
    ack = negotiation.accept_approval_set("coordinator", approval_set)
    negotiation.accept_prepare("coordinator", _prepare(approval_set, ack))
    return negotiation, proposal


def _peer_prepared(
    proposal: ChannelInputProposalPayload, local: ChannelInputPreparedPayload, nonce: bytes
) -> ChannelInputPreparedPayload:
    coordinator = ChannelInputKeyPair.from_secret(COORDINATOR_SECRET)
    return cast(
        ChannelInputPreparedPayload,
        sign_payload(
            ChannelInputPreparedPayload(
                session_id=proposal.session_id,
                attempt=proposal.attempt,
                signer_key=coordinator.public_key,
                transcript_hash=proposal.transcript_hash,
                funding_outpoint=local.funding_outpoint,
                public_nonce=nonce.hex(),
            ),
            COORDINATOR_SECRET,
        ),
    )


def _sign_intent(
    proposal: ChannelInputProposalPayload,
    local: ChannelInputPreparedPayload,
    peer: ChannelInputPreparedPayload,
) -> ChannelInputSignPayload:
    coordinator = ChannelInputKeyPair.from_secret(COORDINATOR_SECRET)
    return cast(
        ChannelInputSignPayload,
        sign_payload(
            ChannelInputSignPayload(
                session_id=proposal.session_id,
                attempt=proposal.attempt,
                signer_key=coordinator.public_key,
                transcript_hash=proposal.transcript_hash,
                funding_outpoint=local.funding_outpoint,
                local_public_nonce=local.public_nonce,
                peer_prepared=peer,
            ),
            COORDINATOR_SECRET,
        ),
    )


def _finalize_intent(
    proposal: ChannelInputProposalPayload,
    local: ChannelInputPreparedPayload,
    peer: ChannelInputPreparedPayload,
) -> ChannelInputFinalizePayload:
    coordinator = ChannelInputKeyPair.from_secret(COORDINATOR_SECRET)
    peer_partial = cast(
        ChannelInputPartialPayload,
        sign_payload(
            ChannelInputPartialPayload(
                session_id=proposal.session_id,
                attempt=proposal.attempt,
                signer_key=coordinator.public_key,
                transcript_hash=proposal.transcript_hash,
                funding_outpoint=local.funding_outpoint,
                local_public_nonce=peer.public_nonce,
                peer_public_nonce=local.public_nonce,
                partial_signature="cc" * 32,
            ),
            COORDINATOR_SECRET,
        ),
    )
    return cast(
        ChannelInputFinalizePayload,
        sign_payload(
            ChannelInputFinalizePayload(
                session_id=proposal.session_id,
                attempt=proposal.attempt,
                signer_key=coordinator.public_key,
                transcript_hash=proposal.transcript_hash,
                funding_outpoint=local.funding_outpoint,
                local_public_nonce=local.public_nonce,
                peer_partial=peer_partial,
            ),
            COORDINATOR_SECRET,
        ),
    )


def _finalized_result(
    proposal: ChannelInputProposalPayload, index: int, signature: bytes
) -> LndFinalizedEscrowParent:
    unsigned = parse_transaction_bytes(bytes.fromhex(proposal.transcript.unsigned_parent_tx))
    witnesses: list[tuple[bytes, ...]] = [() for _ in unsigned.inputs]
    witnesses[index] = (signature,)
    return LndFinalizedEscrowParent(
        tx_hex=serialize_transaction(
            unsigned.version, unsigned.inputs, unsigned.outputs, unsigned.locktime, witnesses
        ).hex(),
        txid=get_txid(proposal.transcript.unsigned_parent_tx),
        channel_input_index=index,
        channel_input_witness=(signature,),
    )


def _status_request(
    proposal: ChannelInputProposalPayload, outpoint: Outpoint, *, query: bytes = b"\x01" * 32
) -> ChannelInputStatusRequestPayload:
    coordinator = ChannelInputKeyPair.from_secret(COORDINATOR_SECRET)
    return cast(
        ChannelInputStatusRequestPayload,
        sign_payload(
            ChannelInputStatusRequestPayload(
                session_id=proposal.session_id,
                attempt=proposal.attempt,
                signer_key=coordinator.public_key,
                transcript_hash=proposal.transcript_hash,
                funding_outpoint=outpoint,
                query_id=query.hex(),
            ),
            COORDINATOR_SECRET,
        ),
    )


def _escrow_status(
    stage: LndEscrowStage,
    *,
    parent_txid: str | None,
    attempt: LndEscrowSigningAttempt | None = None,
    local_nonce: bytes | None = None,
    remote_nonce: bytes | None = None,
    partial: bytes | None = None,
    finalized: LndFinalizedEscrowParent | None = None,
) -> LndEscrowStatus:
    return LndEscrowStatus(
        stage=stage,
        parent_txid=parent_txid,
        finalized=stage is LndEscrowStage.FINALIZED,
        active_attempt=attempt,
        durable_local_public_nonce=local_nonce,
        durable_remote_public_nonce=remote_nonce,
        durable_local_partial_signature=partial,
        finalized_parent=finalized,
    )


def _signing_negotiation() -> tuple[
    MakerChannelInputNegotiation,
    ChannelInputProposalPayload,
    ChannelInputPreparedPayload,
    ChannelInputPreparedPayload,
    ChannelInputSignPayload,
]:
    negotiation, proposal = _prepared_negotiation(channels=1)
    terms = proposal.recipient_terms[0]
    local = negotiation.record_prepared(
        terms.funding_outpoint,
        get_txid(proposal.transcript.unsigned_parent_tx),
        LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66),
    )
    peer = _peer_prepared(proposal, local, b"\x03" * 66)
    sign = _sign_intent(proposal, local, peer)
    assert negotiation.accept_sign("coordinator", sign) is None
    return negotiation, proposal, local, peer, sign


def test_channel_state_chain_is_durable_and_replays_exact_payloads() -> None:
    negotiation, proposal = _prepared_negotiation()
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    local_prepared: list[ChannelInputPreparedPayload] = []
    for index, terms in enumerate(proposal.recipient_terms):
        attempt = LndEscrowSigningAttempt(
            attempt_id=(index + 1).to_bytes(32, "big"),
            public_nonce=bytes([index + 10]) * 66,
        )
        prepared = negotiation.record_prepared(terms.funding_outpoint, parent_txid, attempt)
        assert negotiation.signing_attempt_for(terms.funding_outpoint) == attempt
        assert negotiation.record_prepared(terms.funding_outpoint, parent_txid, attempt) == prepared
        local_prepared.append(prepared)
        snapshot = MakerChannelInputSnapshot.model_validate_json(
            negotiation.snapshot().model_dump_json()
        )
        assert MakerChannelInputNegotiation.from_snapshot(snapshot).snapshot() == snapshot
    assert negotiation.stage is MakerChannelInputStage.NONCES_READY

    peer_prepared = [
        _peer_prepared(proposal, local, bytes([index + 30]) * 66)
        for index, local in enumerate(local_prepared)
    ]
    signs = [
        _sign_intent(proposal, local, peer)
        for local, peer in zip(local_prepared, peer_prepared, strict=True)
    ]
    assert negotiation.accept_sign("coordinator", signs[0]) is None
    first_partial = negotiation.record_partial(local_prepared[0].funding_outpoint, b"\xaa" * 32)
    assert negotiation.accept_sign("coordinator", signs[0]) == first_partial
    assert negotiation.stage is MakerChannelInputStage.SIGNING

    assert negotiation.accept_sign("coordinator", signs[1]) is None
    second_partial = negotiation.record_partial(local_prepared[1].funding_outpoint, b"\xbb" * 32)
    assert (
        negotiation.record_partial(local_prepared[1].funding_outpoint, b"\xbb" * 32)
        == second_partial
    )
    first_finalize = _finalize_intent(proposal, local_prepared[0], peer_prepared[0])
    assert negotiation.accept_finalize("coordinator", first_finalize) is None
    assert negotiation.stage is MakerChannelInputStage.FINALIZING
    first_result = _finalized_result(proposal, 0, b"\x11" * 64)
    first_finalized = negotiation.record_finalized(local_prepared[0].funding_outpoint, first_result)
    assert (
        negotiation.record_finalized(local_prepared[0].funding_outpoint, first_result)
        == first_finalized
    )

    second_finalize = _finalize_intent(proposal, local_prepared[1], peer_prepared[1])
    assert negotiation.accept_finalize("coordinator", second_finalize) is None
    negotiation.record_finalized(
        local_prepared[1].funding_outpoint, _finalized_result(proposal, 1, b"\x22" * 64)
    )
    assert negotiation.stage is MakerChannelInputStage.FINALIZED
    snapshot = MakerChannelInputSnapshot.model_validate_json(
        negotiation.snapshot().model_dump_json()
    )
    restored = MakerChannelInputNegotiation.from_snapshot(snapshot)
    assert restored.snapshot() == snapshot
    assert restored.channel_states[0].finalized is not None


def test_status_evidence_tracks_reconciled_prepared_signed_and_finalized_state() -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    outpoint = proposal.recipient_terms[0].funding_outpoint
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    attempt = LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66)
    local = negotiation.record_prepared(outpoint, parent_txid, attempt)
    prepared_request = _status_request(proposal, outpoint)
    prepared_status = negotiation.record_status(
        "coordinator",
        prepared_request,
        _escrow_status(LndEscrowStage.PARENT_PREPARED, parent_txid=parent_txid, attempt=attempt),
    )
    assert prepared_status.escrow_stage == "parent_prepared"
    assert prepared_status.attempt_active is True
    assert prepared_status.partial is None and prepared_status.finalized is None
    assert (
        negotiation.record_status(
            "coordinator",
            prepared_request,
            _escrow_status(
                LndEscrowStage.PARENT_PREPARED, parent_txid=parent_txid, attempt=attempt
            ),
        )
        == prepared_status
    )
    replacement_request = _status_request(proposal, outpoint, query=b"\x06" * 32)
    replacement_status = negotiation.record_status(
        "coordinator",
        replacement_request,
        _escrow_status(LndEscrowStage.PARENT_PREPARED, parent_txid=parent_txid, attempt=attempt),
    )
    assert replacement_status.query_id == replacement_request.query_id
    assert replacement_status != prepared_status
    assert negotiation.channel_states[0].status_request == replacement_request

    peer = _peer_prepared(proposal, local, b"\x03" * 66)
    negotiation.accept_sign("coordinator", _sign_intent(proposal, local, peer))
    negotiation.record_partial(outpoint, b"\xaa" * 32)
    signed_request = _status_request(proposal, outpoint, query=b"\x04" * 32)
    signed_status = negotiation.record_status(
        "coordinator",
        signed_request,
        _escrow_status(
            LndEscrowStage.PARENT_SIGNED,
            parent_txid=parent_txid,
            local_nonce=bytes.fromhex(local.public_nonce),
            remote_nonce=bytes.fromhex(peer.public_nonce),
            partial=b"\xaa" * 32,
        ),
    )
    assert signed_status.escrow_stage == "parent_signed"
    assert signed_status.attempt_active is False
    assert signed_status.partial == negotiation.channel_states[0].partial
    assert signed_status != prepared_status

    negotiation.accept_finalize("coordinator", _finalize_intent(proposal, local, peer))
    result = _finalized_result(proposal, 0, b"\x11" * 64)
    negotiation.record_finalized(outpoint, result)
    finalized_request = _status_request(proposal, outpoint, query=b"\x05" * 32)
    finalized_status = negotiation.record_status(
        "coordinator",
        finalized_request,
        _escrow_status(
            LndEscrowStage.FINALIZED,
            parent_txid=parent_txid,
            local_nonce=bytes.fromhex(local.public_nonce),
            remote_nonce=bytes.fromhex(peer.public_nonce),
            partial=b"\xaa" * 32,
            finalized=result,
        ),
    )
    assert finalized_status.escrow_stage == "finalized"
    assert finalized_status.attempt_active is False
    assert finalized_status.finalized == negotiation.channel_states[0].finalized
    assert negotiation.channel_states[0].status_request == finalized_request
    assert negotiation.channel_states[0].status_response == finalized_status
    snapshot = MakerChannelInputSnapshot.model_validate_json(
        negotiation.snapshot().model_dump_json()
    )
    assert MakerChannelInputNegotiation.from_snapshot(snapshot).snapshot() == snapshot


@pytest.mark.parametrize(
    "failure",
    ["actor", "signer", "signature", "hash", "outpoint", "locked", "parent", "attempt"],
)
def test_status_rejects_bad_request_and_prepared_backend_evidence(failure: str) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    outpoint = proposal.recipient_terms[0].funding_outpoint
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    attempt = LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66)
    negotiation.record_prepared(outpoint, parent_txid, attempt)
    request = _status_request(proposal, outpoint)
    actor = "coordinator"
    status = _escrow_status(
        LndEscrowStage.PARENT_PREPARED, parent_txid=parent_txid, attempt=attempt
    )
    if failure == "actor":
        actor = "other"
    elif failure == "signer":
        request = sign_payload(
            request.model_copy(update={"signer_key": negotiation.participant_key}),
            PARTICIPANT_SECRET,
        )
    elif failure == "signature":
        request = request.model_copy(update={"sig": "00" * 64})
    elif failure == "hash":
        request = sign_payload(
            request.model_copy(update={"transcript_hash": "00" * 32}), COORDINATOR_SECRET
        )
    elif failure == "outpoint":
        request = _status_request(proposal, Outpoint(txid="ff" * 32, vout=0))
    elif failure == "locked":
        status = _escrow_status(LndEscrowStage.LOCKED, parent_txid=None)
    elif failure == "parent":
        status = _escrow_status(
            LndEscrowStage.PARENT_PREPARED, parent_txid="00" * 32, attempt=attempt
        )
    else:
        status = _escrow_status(
            LndEscrowStage.PARENT_PREPARED,
            parent_txid=parent_txid,
            attempt=LndEscrowSigningAttempt(attempt_id=b"\x03" * 32, public_nonce=b"\x04" * 66),
        )
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_status(actor, request, status)


@pytest.mark.parametrize("failure", ["local_nonce", "remote_nonce", "partial", "finalized"])
def test_status_rejects_signed_and_finalized_backend_mismatches(failure: str) -> None:
    negotiation, proposal, local, peer, _ = _signing_negotiation()
    outpoint = local.funding_outpoint
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    negotiation.record_partial(outpoint, b"\xaa" * 32)
    request = _status_request(proposal, outpoint)
    status = _escrow_status(
        LndEscrowStage.PARENT_SIGNED,
        parent_txid=parent_txid,
        local_nonce=bytes.fromhex(local.public_nonce),
        remote_nonce=bytes.fromhex(peer.public_nonce),
        partial=b"\xaa" * 32,
    )
    if failure == "local_nonce":
        status = status.model_copy(update={"durable_local_public_nonce": b"\x04" * 66})
    elif failure == "remote_nonce":
        status = status.model_copy(update={"durable_remote_public_nonce": b"\x04" * 66})
    elif failure == "partial":
        status = status.model_copy(update={"durable_local_partial_signature": b"\xbb" * 32})
    else:
        negotiation.accept_finalize("coordinator", _finalize_intent(proposal, local, peer))
        result = _finalized_result(proposal, 0, b"\x11" * 64)
        negotiation.record_finalized(outpoint, result)
        status = _escrow_status(
            LndEscrowStage.FINALIZED,
            parent_txid=parent_txid,
            local_nonce=bytes.fromhex(local.public_nonce),
            remote_nonce=bytes.fromhex(peer.public_nonce),
            partial=b"\xaa" * 32,
            finalized=_finalized_result(proposal, 0, b"\x22" * 64),
        )
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_status("coordinator", request, status)


def test_status_snapshot_rejects_mismatched_query_binding() -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    outpoint = proposal.recipient_terms[0].funding_outpoint
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    attempt = LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66)
    negotiation.record_prepared(outpoint, parent_txid, attempt)
    negotiation.record_status(
        "coordinator",
        _status_request(proposal, outpoint),
        _escrow_status(LndEscrowStage.PARENT_PREPARED, parent_txid=parent_txid, attempt=attempt),
    )
    state = negotiation.channel_states[0]
    assert state.status_response is not None
    data = state.model_dump(mode="python")
    data["status_response"]["query_id"] = "ff" * 32
    with pytest.raises(ValueError, match="status evidence"):
        MakerChannelInputChannelState.model_validate(data)


def test_status_rejects_before_prepare_is_accepted() -> None:
    negotiation, proposal, _ = _proposal_approved()
    outpoint = proposal.recipient_terms[0].funding_outpoint
    with pytest.raises(ChannelInputValidationError, match="preparation"):
        negotiation.record_status(
            "coordinator",
            _status_request(proposal, outpoint),
            _escrow_status(
                LndEscrowStage.PARENT_PREPARED,
                parent_txid=get_txid(proposal.transcript.unsigned_parent_tx),
                attempt=LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66),
            ),
        )


def test_cancel_is_signed_replayable_and_rejects_partial_evidence() -> None:
    negotiation, proposal, _ = _proposal_approved()
    cancel = _cancel(proposal)

    assert negotiation.accept_cancel("coordinator", cancel) is None
    assert negotiation.stage is MakerChannelInputStage.CANCEL_INTENDED
    ack = negotiation.record_canceled(
        tuple(
            sorted(
                (term.funding_outpoint for term in proposal.recipient_terms),
                key=lambda item: item.key(),
            )
        )
    )
    assert negotiation.stage is MakerChannelInputStage.CANCELED
    assert negotiation.accept_cancel("coordinator", cancel) == ack
    restored = MakerChannelInputNegotiation.from_snapshot(negotiation.snapshot())
    assert restored.accept_cancel("coordinator", cancel) == ack
    with pytest.raises(ChannelInputValidationError, match="conflicts"):
        restored.accept_cancel("coordinator", _cancel(proposal, reason_code="replacement"))

    signing, signing_proposal, local, _, _ = _signing_negotiation()
    signing.record_partial(local.funding_outpoint, b"\xaa" * 32)
    with pytest.raises(ChannelInputValidationError, match="partial"):
        signing.accept_cancel("coordinator", _cancel(signing_proposal))


@pytest.mark.parametrize("install_hellos", (False, True))
def test_pretranscript_cancel_is_signed_replayable_and_restores(
    install_hellos: bool,
) -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    if install_hellos:
        negotiation.install_frozen_inputs(frozen)

    cancel = _pretranscript_cancel(negotiation)
    assert negotiation.accept_cancel("coordinator", cancel) is None
    outpoints = tuple(
        sorted(
            (
                Outpoint(txid=intent.funding_outpoint.txid, vout=intent.funding_outpoint.vout)
                for intent in negotiation.freeze_intents
            ),
            key=lambda item: item.key(),
        )
    )
    ack = negotiation.record_canceled(outpoints)
    assert ack.transcript_hash == cancellation_binding_hash(negotiation.invite).hex()
    assert negotiation.accept_cancel("coordinator", cancel) == ack
    restored = MakerChannelInputNegotiation.from_snapshot(negotiation.snapshot())
    assert restored.accept_cancel("coordinator", cancel) == ack


def test_pretranscript_cancel_rejects_wrong_invitation_binding() -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)

    with pytest.raises(ChannelInputValidationError, match="invitation"):
        negotiation.accept_cancel(
            "coordinator", _pretranscript_cancel(negotiation, binding="00" * 32)
        )


def test_prepared_rotation_requires_durable_intent_and_tampering_fails_closed() -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    terms = proposal.recipient_terms[0]
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    first = negotiation.record_prepared(
        terms.funding_outpoint,
        parent_txid,
        LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66),
    )
    peer = _peer_prepared(proposal, first, b"\x03" * 66)
    stale_sign = _sign_intent(proposal, first, peer)
    with pytest.raises(ChannelInputValidationError, match="durable intent"):
        negotiation.rotate_unsigned_prepared(
            terms.funding_outpoint,
            parent_txid,
            LndEscrowSigningAttempt(attempt_id=b"\x04" * 32, public_nonce=b"\x05" * 66),
        )
    negotiation.plan_unsigned_prepared_rotation(terms.funding_outpoint)
    restored_intent = MakerChannelInputNegotiation.from_snapshot(negotiation.snapshot())
    assert restored_intent.channel_states[0].restart_intended is True
    with pytest.raises(ChannelInputValidationError, match="restart intent"):
        negotiation.accept_sign("coordinator", stale_sign)
    rotated = negotiation.rotate_unsigned_prepared(
        terms.funding_outpoint,
        parent_txid,
        LndEscrowSigningAttempt(attempt_id=b"\x04" * 32, public_nonce=b"\x05" * 66),
    )
    assert rotated != first
    assert negotiation.channel_states[0].restart_intended is False
    assert negotiation.stage is MakerChannelInputStage.NONCES_READY
    with pytest.raises(ChannelInputValidationError):
        negotiation.accept_sign("coordinator", stale_sign)

    data = negotiation.snapshot().model_dump(mode="python")
    data["channel_states"][0]["prepared"]["sig"] = "00" * 64
    tampered = MakerChannelInputSnapshot.model_validate(data)
    with pytest.raises(ChannelInputValidationError):
        MakerChannelInputNegotiation.from_snapshot(tampered)


def test_prepared_rejects_bad_backend_inputs_and_requires_complete_coverage() -> None:
    negotiation, proposal = _prepared_negotiation(channels=2)
    first, second = proposal.recipient_terms
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    attempt = LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66)
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_prepared(Outpoint(txid="ff" * 32, vout=0), parent_txid, attempt)
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_prepared(first.funding_outpoint, "00" * 32, attempt)
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_prepared(
            first.funding_outpoint, parent_txid, cast(LndEscrowSigningAttempt, object())
        )
    negotiation.record_prepared(first.funding_outpoint, parent_txid, attempt)
    assert negotiation.stage is MakerChannelInputStage.PREPARED
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_prepared(
            first.funding_outpoint,
            parent_txid,
            LndEscrowSigningAttempt(attempt_id=b"\x03" * 32, public_nonce=b"\x04" * 66),
        )
    negotiation.record_prepared(
        second.funding_outpoint,
        parent_txid,
        LndEscrowSigningAttempt(attempt_id=b"\x05" * 32, public_nonce=b"\x06" * 66),
    )
    assert negotiation.stage is MakerChannelInputStage.NONCES_READY


def test_rotation_rejects_same_attempt_and_all_durable_signing_boundaries() -> None:
    negotiation, proposal, local, peer, sign = _signing_negotiation()
    terms = proposal.recipient_terms[0]
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    with pytest.raises(ChannelInputValidationError, match="signing authorization"):
        negotiation.plan_unsigned_prepared_rotation(terms.funding_outpoint)
    with pytest.raises(ChannelInputValidationError, match="durable intent"):
        negotiation.rotate_unsigned_prepared(
            terms.funding_outpoint,
            parent_txid,
            LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x07" * 66),
        )
    negotiation.record_partial(terms.funding_outpoint, b"\xaa" * 32)
    with pytest.raises(ChannelInputValidationError):
        negotiation.rotate_unsigned_prepared(
            terms.funding_outpoint,
            parent_txid,
            LndEscrowSigningAttempt(attempt_id=b"\x08" * 32, public_nonce=b"\x09" * 66),
        )
    finalize = _finalize_intent(proposal, local, peer)
    negotiation.accept_finalize("coordinator", finalize)
    with pytest.raises(ChannelInputValidationError):
        negotiation.rotate_unsigned_prepared(
            terms.funding_outpoint,
            parent_txid,
            LndEscrowSigningAttempt(attempt_id=b"\x0a" * 32, public_nonce=b"\x0b" * 66),
        )
    negotiation.record_finalized(
        terms.funding_outpoint, _finalized_result(proposal, 0, b"\x11" * 64)
    )
    with pytest.raises(ChannelInputValidationError):
        negotiation.rotate_unsigned_prepared(
            terms.funding_outpoint,
            parent_txid,
            LndEscrowSigningAttempt(attempt_id=b"\x0c" * 32, public_nonce=b"\x0d" * 66),
        )
    assert sign == negotiation.channel_states[0].sign_request


@pytest.mark.parametrize(
    "failure",
    ["actor", "signer", "signature", "outpoint", "nonce", "peer", "peer_signature", "peer_nonce"],
)
def test_sign_rejects_authenticated_binding_failures(failure: str) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    terms = proposal.recipient_terms[0]
    local = negotiation.record_prepared(
        terms.funding_outpoint,
        get_txid(proposal.transcript.unsigned_parent_tx),
        LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66),
    )
    peer = _peer_prepared(proposal, local, b"\x03" * 66)
    sign = _sign_intent(proposal, local, peer)
    actor = "coordinator"
    if failure == "actor":
        actor = "other"
    elif failure == "signer":
        sign = sign_payload(
            sign.model_copy(update={"signer_key": negotiation.participant_key}), PARTICIPANT_SECRET
        )
    elif failure == "signature":
        sign = sign.model_copy(update={"sig": "00" * 64})
    elif failure == "outpoint":
        wrong = Outpoint(txid="ff" * 32, vout=0)
        peer = peer.model_copy(update={"funding_outpoint": wrong})
        sign = sign_payload(
            sign.model_copy(update={"funding_outpoint": wrong, "peer_prepared": peer}),
            COORDINATOR_SECRET,
        )
    elif failure == "nonce":
        sign = sign_payload(
            sign.model_copy(update={"local_public_nonce": "04" * 66}), COORDINATOR_SECRET
        )
    elif failure == "peer":
        peer = sign_payload(
            peer.model_copy(update={"signer_key": negotiation.participant_key}), PARTICIPANT_SECRET
        )
        sign = sign_payload(sign.model_copy(update={"peer_prepared": peer}), COORDINATOR_SECRET)
    elif failure == "peer_signature":
        sign = sign_payload(
            sign.model_copy(update={"peer_prepared": peer.model_copy(update={"sig": "00" * 64})}),
            COORDINATOR_SECRET,
        )
    else:
        peer = sign_payload(
            peer.model_copy(update={"public_nonce": local.public_nonce}), COORDINATOR_SECRET
        )
        sign = sign_payload(sign.model_copy(update={"peer_prepared": peer}), COORDINATOR_SECRET)
    with pytest.raises(ChannelInputValidationError):
        negotiation.accept_sign(actor, sign)


def test_partial_finalize_and_finalized_reject_conflicting_evidence() -> None:
    negotiation, proposal, local, peer, _ = _signing_negotiation()
    outpoint = local.funding_outpoint
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_partial(outpoint, b"\x01" * 31)
    negotiation.record_partial(outpoint, b"\xaa" * 32)
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_partial(outpoint, b"\xbb" * 32)
    finalize = _finalize_intent(proposal, local, peer)
    peer_partial = finalize.peer_partial
    for actor, candidate in (
        ("other", finalize),
        ("coordinator", finalize.model_copy(update={"sig": "00" * 64})),
        (
            "coordinator",
            sign_payload(
                finalize.model_copy(update={"signer_key": negotiation.participant_key}),
                PARTICIPANT_SECRET,
            ),
        ),
        (
            "coordinator",
            sign_payload(
                finalize.model_copy(update={"local_public_nonce": "04" * 66}), COORDINATOR_SECRET
            ),
        ),
        (
            "coordinator",
            sign_payload(
                finalize.model_copy(
                    update={
                        "peer_partial": sign_payload(
                            peer_partial.model_copy(
                                update={"signer_key": negotiation.participant_key}
                            ),
                            PARTICIPANT_SECRET,
                        )
                    }
                ),
                COORDINATOR_SECRET,
            ),
        ),
        (
            "coordinator",
            sign_payload(
                finalize.model_copy(
                    update={"peer_partial": peer_partial.model_copy(update={"sig": "00" * 64})}
                ),
                COORDINATOR_SECRET,
            ),
        ),
        (
            "coordinator",
            sign_payload(
                finalize.model_copy(
                    update={
                        "peer_partial": sign_payload(
                            peer_partial.model_copy(update={"peer_public_nonce": "04" * 66}),
                            COORDINATOR_SECRET,
                        )
                    }
                ),
                COORDINATOR_SECRET,
            ),
        ),
    ):
        with pytest.raises(ChannelInputValidationError):
            negotiation.accept_finalize(actor, candidate)
    negotiation.accept_finalize("coordinator", finalize)
    result = _finalized_result(proposal, 0, b"\x11" * 64)
    for candidate in (
        result.model_copy(update={"txid": "00" * 32}),
        result.model_copy(update={"channel_input_index": 1}),
        result.model_copy(update={"channel_input_witness": (b"\x22" * 64,)}),
    ):
        with pytest.raises(ChannelInputValidationError):
            negotiation.record_finalized(outpoint, candidate)
    unsigned = parse_transaction_bytes(bytes.fromhex(proposal.transcript.unsigned_parent_tx))
    outputs = list(unsigned.outputs)
    outputs[0] = TxOutput(value=outputs[0].value - 1, script=outputs[0].script)
    mismatched_parent = serialize_transaction(
        unsigned.version,
        unsigned.inputs,
        outputs,
        unsigned.locktime,
        [(b"\x11" * 64,)],
    ).hex()
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_finalized(
            outpoint, result.model_copy(update={"tx_hex": mismatched_parent})
        )
    negotiation.record_finalized(outpoint, result)
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_finalized(outpoint, _finalized_result(proposal, 0, b"\x33" * 64))


def test_snapshot_channel_state_tampering_fails_closed() -> None:
    negotiation, proposal, local, peer, _ = _signing_negotiation()
    negotiation.record_partial(local.funding_outpoint, b"\xaa" * 32)
    negotiation.accept_finalize("coordinator", _finalize_intent(proposal, local, peer))
    negotiation.record_finalized(
        local.funding_outpoint, _finalized_result(proposal, 0, b"\x11" * 64)
    )
    for field, value in (
        ("stage", "nonces_ready"),
        ("attempt_id", "invalid"),
        (
            "prepared",
            {**negotiation.channel_states[0].prepared.model_dump(), "signer_key": _node(90)},
        ),
        (
            "prepared",
            {
                **negotiation.channel_states[0].prepared.model_dump(),
                "public_nonce": "04" * 66,
            },
        ),
        (
            "partial",
            {**negotiation.channel_states[0].partial.model_dump(), "partial_signature": "00" * 32},
        ),
        (
            "finalized",
            {
                **negotiation.channel_states[0].finalized.model_dump(),
                "channel_signed_parent_tx": proposal.transcript.unsigned_parent_tx,
            },
        ),
    ):
        data = negotiation.snapshot().model_dump(mode="python")
        if field == "stage":
            data["stage"] = value
        else:
            data["channel_states"][0][field] = value
        with pytest.raises((ValueError, ChannelInputValidationError)):
            MakerChannelInputNegotiation.from_snapshot(
                MakerChannelInputSnapshot.model_validate(data)
            )


def test_negotiation_multichannel_replay_and_json_restore() -> None:
    negotiation, frozen = _negotiation()
    assert negotiation.stage is MakerChannelInputStage.INVITED
    assert negotiation.snapshot().participant_secret_key_hex == PARTICIPANT_SECRET.hex()

    intents = _plan_freeze(negotiation, tuple(reversed(frozen)))
    assert negotiation.stage is MakerChannelInputStage.FREEZE_INTENDED
    assert [intent.channel_id for intent in intents] == sorted(
        f"{item.snapshot.funding_outpoint.txid}:{item.snapshot.funding_outpoint.vout}"
        for item in frozen
    )
    freeze_snapshot = MakerChannelInputSnapshot.model_validate_json(
        negotiation.snapshot().model_dump_json()
    )
    assert MakerChannelInputNegotiation.from_snapshot(freeze_snapshot).snapshot() == freeze_snapshot

    hellos = negotiation.install_frozen_inputs(tuple(reversed(frozen)))
    assert [hello.funding_outpoint.txid for hello in hellos] == sorted(
        item.snapshot.funding_outpoint.txid for item in frozen
    )
    assert negotiation.install_frozen_inputs(frozen) == hellos

    proposal, _ = _proposal(negotiation)
    approval = negotiation.accept_proposal(
        "coordinator", proposal, current_time=1_000, current_height=100
    )
    assert (
        negotiation.accept_proposal("coordinator", proposal, current_time=1_500, current_height=100)
        == approval
    )

    approval_set = _approval_set(proposal, approval)
    ack = negotiation.accept_approval_set("coordinator", approval_set)
    assert negotiation.accept_approval_set("coordinator", approval_set) == ack

    prepare = _prepare(approval_set, ack)
    assert negotiation.accept_prepare("coordinator", prepare) == prepare
    assert negotiation.accept_prepare("coordinator", prepare) == prepare
    assert negotiation.stage is MakerChannelInputStage.PREPARED

    restored_snapshot = MakerChannelInputSnapshot.model_validate_json(
        negotiation.snapshot().model_dump_json()
    )
    restored = MakerChannelInputNegotiation.from_snapshot(restored_snapshot)
    assert restored.install_frozen_inputs(frozen) == hellos
    assert (
        restored.accept_proposal("coordinator", proposal, current_time=1_999, current_height=100)
        == approval
    )
    assert restored.accept_approval_set("coordinator", approval_set) == ack
    assert restored.accept_prepare("coordinator", prepare) == prepare


def _shortpaid_proposal(
    negotiation: MakerChannelInputNegotiation,
    *,
    diverted_sat: int = 0,
    steal_script: str | None = None,
    drop_settlement_output: bool = False,
) -> ChannelInputProposalPayload:
    """Re-sign an honest proposal over a parent that underpays the maker.

    The endpoint terms stay exactly as the maker would approve them, mirroring
    the real attack: only the parent bytes diverge from the approved terms.
    """

    proposal, maker_terms = _proposal(negotiation)
    transcript = proposal.transcript
    parsed = parse_transaction_bytes(bytes.fromhex(transcript.unsigned_parent_tx))
    settlement_index = maker_terms[0].settlement_outputs[0].output_index
    outputs = list(parsed.outputs)
    if drop_settlement_output:
        # The claimed index no longer exists in a shorter parent. The dropped
        # value moves to the coordinator's own output so the declared fee, and
        # therefore the maker's fee-rate policy check, is unchanged.
        dropped = outputs[settlement_index]
        keeper = 1 if settlement_index == 0 else 0
        outputs[keeper] = TxOutput(
            value=outputs[keeper].value + dropped.value,
            script=outputs[keeper].script,
        )
        outputs.pop(settlement_index)
    elif steal_script is not None:
        outputs[settlement_index] = TxOutput(
            value=outputs[settlement_index].value,
            script=bytes.fromhex(steal_script),
        )
    else:
        outputs[settlement_index] = TxOutput(
            value=outputs[settlement_index].value - diverted_sat,
            script=outputs[settlement_index].script,
        )
        outputs.append(TxOutput(value=diverted_sat, script=bytes.fromhex("5120" + "aa" * 32)))
    unsigned = serialize_transaction(
        parsed.version, list(parsed.inputs), outputs, parsed.locktime
    ).hex()
    total_fee = sum(prevout.value_sat for prevout in transcript.input_prevouts) - sum(
        output.value for output in outputs
    )
    vsize = estimate_vsize(["p2tr"] * len(parsed.inputs), ["p2tr"] * len(outputs))
    # Keep every declared aggregate consistent with the malicious bytes so only
    # the settlement binding can reject this proposal.
    updated_transcript = transcript.model_copy(
        update={
            "unsigned_parent_tx": unsigned,
            "total_fee_sat": total_fee,
            "fee_rate_sat_vb": (total_fee + vsize - 1) // vsize,
        }
    )
    return cast(
        ChannelInputProposalPayload,
        sign_payload(
            ChannelInputProposalPayload(
                session_id=SESSION_ID,
                attempt=1,
                signer_key=transcript.coordinator_key,
                recipient_key=negotiation.participant_key,
                transcript=updated_transcript,
                transcript_hash=transcript_hash(updated_transcript).hex(),
                recipient_terms=maker_terms,
            ),
            COORDINATOR_SECRET,
            aux_randomness=b"\x02" * 32,
        ),
    )


@pytest.mark.parametrize(
    ("kwargs", "description"),
    [
        # The coordinator short-pays the settlement and keeps the difference.
        ({"diverted_sat": 48_454}, "short-paid settlement value"),
        # The parent pays the claimed amount to a coordinator script instead.
        ({"steal_script": "5120" + "aa" * 32}, "substituted settlement script"),
        # The claimed settlement index is absent from the parent.
        ({"drop_settlement_output": True}, "out-of-range settlement index"),
    ],
)
def test_rejects_settlement_terms_that_differ_from_the_parent(
    kwargs: dict[str, object], description: str
) -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    negotiation.install_frozen_inputs(frozen)
    proposal = _shortpaid_proposal(negotiation, **kwargs)  # type: ignore[arg-type]

    with pytest.raises(ChannelInputValidationError, match="settlement output"):
        negotiation.accept_proposal("coordinator", proposal, current_time=1_000, current_height=100)

    # A rejected proposal must leave nothing approved behind.
    assert negotiation.proposal is None
    assert negotiation.local_approval is None


def test_accepts_settlement_terms_that_match_the_parent_exactly() -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    negotiation.install_frozen_inputs(frozen)
    proposal, maker_terms = _proposal(negotiation)

    approval = negotiation.accept_proposal(
        "coordinator", proposal, current_time=1_000, current_height=100
    )

    assert negotiation.proposal == proposal
    assert set(approval.terms_hashes) == {endpoint_terms_hash(terms).hex() for terms in maker_terms}


def test_rejects_wrong_actor_and_conflicting_proposal() -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    negotiation.install_frozen_inputs(frozen)
    proposal, _ = _proposal(negotiation)
    with pytest.raises(ChannelInputValidationError):
        negotiation.accept_proposal("impostor", proposal, current_time=1_000, current_height=100)
    negotiation.accept_proposal("coordinator", proposal, current_time=1_000, current_height=100)
    with pytest.raises(ChannelInputValidationError):
        negotiation.accept_proposal(
            "coordinator",
            proposal.model_copy(update={"sig": "00" * 64}),
            current_time=1_000,
            current_height=100,
        )


def test_rejects_invalid_frozen_evidence_and_snapshot_tampering() -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    wrong_session = frozen[0].model_copy(
        update={"snapshot": frozen[0].snapshot.model_copy(update={"session_id": "22" * 32})}
    )
    with pytest.raises(ChannelInputValidationError):
        negotiation.install_frozen_inputs((wrong_session,))

    negotiation.install_frozen_inputs(frozen)
    data = negotiation.snapshot().model_dump(mode="python")
    data["participant_secret_key_hex"] = (3).to_bytes(32, "big").hex()
    tampered = MakerChannelInputSnapshot.model_validate(data)
    with pytest.raises(ChannelInputValidationError):
        MakerChannelInputNegotiation.from_snapshot(tampered)


def test_plan_freeze_replay_and_stage_rules() -> None:
    negotiation, frozen = _negotiation(channels=2)
    candidates = tuple(_candidate(item, channel_id=index) for index, item in enumerate(frozen))

    settlement_scripts = _settlement_scripts(frozen)
    intents = negotiation.plan_freeze(
        tuple(reversed(candidates)), settlement_scripts=settlement_scripts
    )
    assert intents == negotiation.plan_freeze(candidates, settlement_scripts=settlement_scripts)
    assert negotiation.stage is MakerChannelInputStage.FREEZE_INTENDED

    conflict = candidates[0].model_copy(update={"capacity_sat": 99_999})
    with pytest.raises(ChannelInputValidationError):
        negotiation.plan_freeze((conflict, candidates[1]), settlement_scripts=settlement_scripts)

    negotiation.install_frozen_inputs(frozen)
    assert negotiation.plan_freeze(candidates, settlement_scripts=settlement_scripts) == intents

    with pytest.raises(ChannelInputValidationError):
        negotiation.plan_freeze(
            candidates,
            settlement_scripts={
                **settlement_scripts,
                intents[0].channel_id: "5120" + "ff" * 32,
            },
        )

    wrong_stage, wrong_stage_frozen = _negotiation(channels=1)
    wrong_stage.stage = MakerChannelInputStage.HELLOS_READY
    with pytest.raises(ChannelInputValidationError):
        _plan_freeze(wrong_stage, wrong_stage_frozen)


@pytest.mark.parametrize("failure", ["zero", "over_limit", "duplicate_outpoint", "duplicate_id"])
def test_plan_freeze_rejects_invalid_candidate_sets(failure: str) -> None:
    channels = MAX_CHANNELS + 1 if failure == "over_limit" else 2
    negotiation, frozen = _negotiation(channels=channels)
    candidates = tuple(_candidate(item, channel_id=index) for index, item in enumerate(frozen))
    supplied: tuple[ChannelInputCandidate, ...]
    if failure == "zero":
        supplied = ()
    elif failure == "over_limit":
        supplied = candidates
    elif failure == "duplicate_outpoint":
        supplied = (
            candidates[0],
            candidates[1].model_copy(update={"funding_outpoint": candidates[0].funding_outpoint}),
        )
    else:
        supplied = (
            candidates[0],
            candidates[1].model_copy(update={"channel_id": candidates[0].channel_id}),
        )
    with pytest.raises(ChannelInputValidationError):
        negotiation.plan_freeze(supplied, settlement_scripts=_settlement_scripts(frozen))


@pytest.mark.parametrize("failure", ["allowlist", "peer", "capacity", "invalid"])
def test_plan_freeze_rejects_candidate_policy_and_model_mismatches(failure: str) -> None:
    negotiation, frozen = _negotiation(channels=1)
    candidate = _candidate(frozen[0])
    if failure == "allowlist":
        candidate = candidate.model_copy(
            update={"funding_outpoint": EscrowOutpoint(txid="99" * 32, vout=0)}
        )
    elif failure == "peer":
        candidate = candidate.model_copy(update={"peer_node_pubkey": _node(90)})
    elif failure == "capacity":
        candidate = candidate.model_copy(update={"capacity_sat": 99_999})
    elif failure == "invalid":
        candidate = candidate.model_copy(update={"pending_htlc_count": 1})
    with pytest.raises(ChannelInputValidationError):
        negotiation.plan_freeze((candidate,), settlement_scripts=_settlement_scripts(frozen))


@pytest.mark.parametrize(
    "failure", ["disabled", "network", "recovery", "signature", "actor", "key"]
)
def test_from_invite_rejects_invalid_admission(failure: str) -> None:
    frozen = _frozen(0)
    config = _config(
        points=(
            f"{frozen.snapshot.funding_outpoint.txid}:{frozen.snapshot.funding_outpoint.vout}",
        ),
        peers=(frozen.snapshot.peer_node_pubkey.hex(),),
    )
    invite = _invite()
    local_actor = "maker"
    network = "regtest"
    key_pair = ChannelInputKeyPair.from_secret(PARTICIPANT_SECRET)
    if failure == "disabled":
        config = config.model_copy(update={"enabled": False})
    elif failure == "network":
        network = "signet"
    elif failure == "recovery":
        config = config.model_copy(update={"recovery_delay_blocks": 51})
    elif failure == "signature":
        invite = invite.model_copy(update={"sig": "00" * 64})
    elif failure == "actor":
        local_actor = "coordinator"
    else:
        key_pair = ChannelInputKeyPair(
            secret_key=PARTICIPANT_SECRET,
            public_key=ChannelInputKeyPair.from_secret((3).to_bytes(32, "big")).public_key,
        )
    with pytest.raises(ChannelInputValidationError):
        MakerChannelInputNegotiation.from_invite(
            local_actor=local_actor,
            coordinator_actor="coordinator",
            invite=invite,
            config=config,
            network=network,  # type: ignore[arg-type]
            local_node_key=_node(3),
            key_pair=key_pair,
        )


@pytest.mark.parametrize(
    "failure",
    ["session", "allowlist", "peer", "capacity", "duplicate_id", "duplicate_outpoint"],
)
def test_install_frozen_inputs_rejects_each_evidence_invariant(failure: str) -> None:
    negotiation, frozen = _negotiation(channels=2 if failure.startswith("duplicate") else 1)
    _plan_freeze(negotiation, frozen)
    first = frozen[0]
    supplied: tuple[MakerFrozenChannelInput, ...]
    if failure == "session":
        supplied = (
            first.model_copy(
                update={"snapshot": first.snapshot.model_copy(update={"session_id": "22" * 32})}
            ),
        )
    elif failure == "allowlist":
        supplied = (
            first.model_copy(
                update={
                    "snapshot": first.snapshot.model_copy(
                        update={"funding_outpoint": EscrowOutpoint(txid="99" * 32, vout=0)}
                    )
                }
            ),
        )
    elif failure == "peer":
        supplied = (
            first.model_copy(
                update={
                    "snapshot": first.snapshot.model_copy(
                        update={"peer_node_pubkey": bytes.fromhex(_node(90))}
                    )
                }
            ),
        )
    elif failure == "capacity":
        supplied = (
            first.model_copy(
                update={"snapshot": first.snapshot.model_copy(update={"capacity_sat": 99_999})}
            ),
        )
    elif failure == "duplicate_id":
        supplied = (
            first,
            frozen[1].model_copy(
                update={
                    "snapshot": frozen[1].snapshot.model_copy(
                        update={"channel_id": first.snapshot.channel_id}
                    )
                }
            ),
        )
    else:
        supplied = (
            first,
            frozen[1].model_copy(
                update={
                    "snapshot": frozen[1].snapshot.model_copy(
                        update={"funding_outpoint": first.snapshot.funding_outpoint}
                    )
                }
            ),
        )
    with pytest.raises(ChannelInputValidationError):
        negotiation.install_frozen_inputs(supplied)


def test_install_frozen_inputs_rejects_conflicting_repeat() -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    negotiation.install_frozen_inputs(frozen)
    conflict = frozen[0].model_copy(update={"settlement_script_pubkey": "5120" + "ff" * 32})
    with pytest.raises(ChannelInputValidationError):
        negotiation.install_frozen_inputs((conflict,))


@pytest.mark.parametrize(
    "failure",
    ["channel_id", "outpoint", "peer", "capacity", "settlement", "missing", "extra"],
)
def test_install_frozen_inputs_requires_exact_freeze_intent_bijection(failure: str) -> None:
    negotiation, frozen = _negotiation(channels=3)
    _plan_freeze(negotiation, frozen[:2])
    first, second, third = frozen
    supplied: tuple[MakerFrozenChannelInput, ...]
    if failure == "channel_id":
        supplied = (
            first.model_copy(
                update={
                    "snapshot": first.snapshot.model_copy(update={"channel_id": "00" * 32 + ":0"})
                }
            ),
            second,
        )
    elif failure == "outpoint":
        supplied = (
            first.model_copy(
                update={
                    "snapshot": first.snapshot.model_copy(
                        update={"funding_outpoint": third.snapshot.funding_outpoint}
                    )
                }
            ),
            second,
        )
    elif failure == "peer":
        supplied = (
            first.model_copy(
                update={
                    "snapshot": first.snapshot.model_copy(
                        update={"peer_node_pubkey": third.snapshot.peer_node_pubkey}
                    )
                }
            ),
            second,
        )
    elif failure == "capacity":
        negotiation.config = negotiation.config.model_copy(
            update={"min_channel_capacity_sat": 99_999}
        )
        supplied = (
            first.model_copy(
                update={"snapshot": first.snapshot.model_copy(update={"capacity_sat": 99_999})}
            ),
            second,
        )
    elif failure == "settlement":
        supplied = (
            first.model_copy(update={"settlement_script_pubkey": "5120" + "ff" * 32}),
            second,
        )
    elif failure == "missing":
        supplied = (first,)
    else:
        supplied = (first, second, third)
    with pytest.raises(ChannelInputValidationError):
        negotiation.install_frozen_inputs(supplied)


def test_snapshot_restore_rejects_tampered_or_missing_freeze_intents() -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    freeze_data = negotiation.snapshot().model_dump(mode="python")
    tampered_intent = negotiation.freeze_intents[0].model_copy(update={"capacity_sat": 99_999})
    freeze_data["freeze_intents"] = (tampered_intent,)
    tampered = MakerChannelInputSnapshot.model_validate(freeze_data)
    with pytest.raises(ChannelInputValidationError):
        MakerChannelInputNegotiation.from_snapshot(tampered)

    negotiation.install_frozen_inputs(frozen)
    old_hello_data = negotiation.snapshot().model_dump(mode="python")
    old_hello_data["freeze_intents"] = ()
    with pytest.raises(ValueError):
        MakerChannelInputSnapshot.model_validate(old_hello_data)


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_parent_fee_sat", 999), ("max_fee_rate_sat_vb", 1), ("recovery_delay_blocks", 19)],
)
def test_proposal_rejects_maker_local_policy(field: str, value: int) -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    negotiation.install_frozen_inputs(frozen)
    proposal, _ = _proposal(negotiation)
    negotiation.config = negotiation.config.model_copy(update={field: value})
    with pytest.raises(ChannelInputValidationError):
        negotiation.accept_proposal("coordinator", proposal, current_time=1_000, current_height=100)


def test_proposal_rejects_wrong_stage_and_approval_set_requires_exact_approval() -> None:
    accepted, proposal, approval = _proposal_approved()
    before_freeze, _ = _negotiation(channels=1)
    with pytest.raises(ChannelInputValidationError):
        before_freeze.accept_proposal(
            "coordinator", proposal, current_time=1_000, current_height=100
        )

    complete = _approval_set(proposal, approval)
    with pytest.raises(ChannelInputValidationError):
        accepted.accept_approval_set(
            "coordinator", complete.model_copy(update={"approvals": (complete.approvals[0],)})
        )
    replacement = cast(
        ChannelInputApprovalPayload,
        sign_payload(
            approval.model_copy(update={"sig": "00" * 64}),
            PARTICIPANT_SECRET,
            aux_randomness=b"\x07" * 32,
        ),
    )
    with pytest.raises(ChannelInputValidationError):
        accepted.accept_approval_set("coordinator", _approval_set(proposal, replacement))
    with pytest.raises(ChannelInputValidationError):
        before_freeze.accept_approval_set("coordinator", _approval_set(proposal, approval))


def test_prepare_requires_exact_ack_and_correct_stage() -> None:
    negotiation, proposal, approval = _proposal_approved()
    approval_set = _approval_set(proposal, approval)
    ack = negotiation.accept_approval_set("coordinator", approval_set)
    replacement = cast(
        ChannelInputApprovalSetAckPayload,
        sign_payload(
            ack.model_copy(update={"sig": "00" * 64}),
            PARTICIPANT_SECRET,
            aux_randomness=b"\x08" * 32,
        ),
    )
    complete = _prepare(approval_set, ack)
    with pytest.raises(ChannelInputValidationError):
        negotiation.accept_prepare(
            "coordinator",
            complete.model_copy(update={"approval_set_acks": (complete.approval_set_acks[0],)}),
        )
    with pytest.raises(ChannelInputValidationError):
        negotiation.accept_prepare("coordinator", _prepare(approval_set, replacement))

    before_ack, _, _ = _proposal_approved()
    with pytest.raises(ChannelInputValidationError):
        before_ack.accept_prepare("coordinator", _prepare(approval_set, ack))


@pytest.mark.parametrize("tamper", ["actor", "ledger", "height", "payload"])
def test_snapshot_restore_rejects_each_persisted_binding(tamper: str) -> None:
    negotiation, proposal, approval = _proposal_approved()
    approval_set = _approval_set(proposal, approval)
    ack = negotiation.accept_approval_set("coordinator", approval_set)
    negotiation.accept_prepare("coordinator", _prepare(approval_set, ack))
    data = negotiation.snapshot().model_dump(mode="python")
    if tamper == "actor":
        data["actor_registry_snapshot"] = (
            ("coordinator", _node(5)[2:]),
            ("maker", negotiation.participant_key),
        )
    elif tamper == "ledger":
        entries = list(data["replay_ledger_snapshot"])
        entries[0] = (entries[0][0], "00" * 32)
        data["replay_ledger_snapshot"] = tuple(entries)
    elif tamper == "height":
        data["accepted_height"] = 99
    else:
        data["prepare"] = {**data["prepare"], "sig": "00" * 64}
    snapshot = MakerChannelInputSnapshot.model_validate(data)
    with pytest.raises(ChannelInputValidationError):
        MakerChannelInputNegotiation.from_snapshot(snapshot)
