from __future__ import annotations

import json
from pathlib import Path

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import TxInput, TxOutput, estimate_vsize, serialize_transaction
from jmcore.channel_input import (
    ChannelDescriptor,
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputCancelAckPayload,
    ChannelInputHelloPayload,
    ChannelInputKeyPair,
    ChannelInputProposalPayload,
    ChannelInputTranscript,
    ChannelInputValidationError,
    EndpointTerms,
    InputPrevout,
    Outpoint,
    PolicyCeilings,
    SettlementOutput,
    approval_set_hash,
    cancel_payload_hash,
    endpoint_terms_hash,
    sign_payload,
    transcript_hash,
)
from jmswap.lnd import ChannelInputCandidate, EscrowOutpoint, FrozenChannelSnapshot

from taker.channel_input_negotiation import (
    TakerChannelInputNegotiation,
    TakerChannelInputNegotiationError,
    TakerChannelInputSnapshot,
    TakerChannelInputStage,
)
from taker.channel_input_store import TakerChannelInputStore

_SESSION = "11" * 32
_COORDINATOR_SECRET = (1).to_bytes(32, "big")
_MAKER_SECRETS = tuple(index.to_bytes(32, "big") for index in range(2, 5))


def _node(index: int) -> str:
    return PrivateKey(index.to_bytes(32, "big")).public_key.format(compressed=True).hex()


def _signed_hello(
    secret: bytes,
    participant: str,
    node: str,
    peer: str,
    outpoint: Outpoint,
    script: str,
    settlement: str,
) -> ChannelInputHelloPayload:
    return sign_payload(
        ChannelInputHelloPayload(
            session_id=_SESSION,
            attempt=1,
            signer_key=participant,
            participant_key=participant,
            node_key=node,
            peer_node_key=peer,
            funding_outpoint=outpoint,
            capacity_sat=100_000,
            funding_script_pubkey=script,
            frozen_local_claim_sat=50_000,
            frozen_remote_claim_sat=50_000,
            settlement_script_pubkey=settlement,
            quoted_miner_fee_share_sat=1_000,
            policy_ceilings=PolicyCeilings(
                max_miner_fee_share_sat=1_000,
                max_claim_adjustment_sat=1_000,
                max_settlement_outputs=1,
            ),
        ),
        secret,
        aux_randomness=b"\x01" * 32,
    )


def _new() -> TakerChannelInputNegotiation:
    return TakerChannelInputNegotiation.create(
        network="regtest",
        local_actor="taker",
        selected_nicks=("maker-a", "maker-b", "maker-c"),
        secret_key=_COORDINATOR_SECRET,
        session_id=_SESSION,
        attempt=1,
        expiry=2_000,
        max_parent_fee_sat=3_000,
        max_fee_rate_sat_vb=100,
        min_recovery_delta=10,
        max_recovery_delta=50,
    )


def _accept_hello(negotiation: TakerChannelInputNegotiation, index: int) -> None:
    nick = negotiation.selected_nicks[index]
    secret = _MAKER_SECRETS[index]
    maker = ChannelInputKeyPair.from_secret(secret)
    outpoint = Outpoint(txid=f"{index + 20:064x}", vout=index)
    funding = "5120" + f"{index + 30:064x}"
    remote = _signed_hello(
        secret,
        maker.public_key,
        _node(index + 20),
        _node(10),
        outpoint,
        funding,
        "5120" + f"{index + 40:064x}",
    )
    local = _signed_hello(
        _COORDINATOR_SECRET,
        negotiation.coordinator_key,
        _node(10),
        _node(index + 20),
        outpoint,
        funding,
        "5120" + f"{index + 50:064x}",
    )
    negotiation.accept_hello(nick, remote, local)


def _candidate(index: int) -> ChannelInputCandidate:
    return ChannelInputCandidate(
        channel_id=index,
        funding_outpoint=EscrowOutpoint(txid=f"{index + 20:064x}", vout=index),
        peer_node_pubkey=_node(index + 20),
        capacity_sat=100_000,
        local_balance_sat=50_000,
        remote_balance_sat=50_000,
        private=True,
        active=True,
        default_channel_state=True,
        pending_htlc_count=0,
        commitment_type=7,
    )


def _freeze_remote(index: int) -> ChannelInputHelloPayload:
    maker = ChannelInputKeyPair.from_secret(_MAKER_SECRETS[index])
    return _signed_hello(
        _MAKER_SECRETS[index],
        maker.public_key,
        _node(index + 20),
        _node(10),
        Outpoint(txid=f"{index + 20:064x}", vout=index),
        "5120" + f"{index + 30:064x}",
        "5120" + f"{index + 40:064x}",
    )


def _freeze_intent(
    negotiation: TakerChannelInputNegotiation, index: int
) -> tuple[ChannelInputHelloPayload, ChannelInputCandidate]:
    remote = _freeze_remote(index)
    candidate = _candidate(index)
    negotiation.record_freeze_intent(
        negotiation.selected_nicks[index],
        remote,
        candidate,
        local_node_key=_node(10),
        settlement_script_pubkey="5120" + f"{index + 50:064x}",
        quoted_miner_fee_share_sat=1_000,
        policy_ceilings=PolicyCeilings(
            max_miner_fee_share_sat=1_000,
            max_claim_adjustment_sat=1_000,
            max_settlement_outputs=1,
        ),
    )
    return remote, candidate


def _frozen_snapshot(index: int) -> FrozenChannelSnapshot:
    candidate = _candidate(index)
    return FrozenChannelSnapshot(
        session_id=_SESSION,
        channel_id=f"{candidate.funding_outpoint.txid}:{candidate.funding_outpoint.vout}",
        funding_outpoint=candidate.funding_outpoint,
        funding_scriptpubkey=bytes.fromhex("5120" + f"{index + 30:064x}"),
        peer_node_pubkey=bytes.fromhex(candidate.peer_node_pubkey),
        local_funding_pubkey=bytes.fromhex(_node(index + 60)),
        remote_funding_pubkey=bytes.fromhex(_node(index + 70)),
        capacity_sat=candidate.capacity_sat,
        local_claim_sat=50_000,
        remote_claim_sat=50_000,
    )


def _ready() -> tuple[TakerChannelInputNegotiation, tuple[bytes, ...]]:
    negotiation = _new()
    for index in range(len(negotiation.selected_nicks)):
        _accept_hello(negotiation, index)
    return negotiation, _MAKER_SECRETS


def _plan(
    negotiation: TakerChannelInputNegotiation,
) -> tuple[ChannelInputTranscript, tuple[EndpointTerms, ...]]:
    remote = dict(negotiation.remote_hellos)
    local = dict(negotiation.local_hellos)
    inputs: list[TxInput] = []
    prevouts: list[InputPrevout] = []
    outputs: list[TxOutput] = []
    descriptors: list[ChannelDescriptor] = []
    terms: list[EndpointTerms] = []
    for index, nick in enumerate(negotiation.selected_nicks):
        maker, coordinator = remote[nick], local[nick]
        inputs.append(TxInput.from_hex(maker.funding_outpoint.txid, maker.funding_outpoint.vout))
        prevouts.append(
            InputPrevout(
                outpoint=maker.funding_outpoint,
                value_sat=maker.capacity_sat,
                script_pubkey=maker.funding_script_pubkey,
            )
        )
        maker_output = SettlementOutput(
            output_index=index * 2,
            amount_sat=49_000,
            script_pubkey=maker.settlement_script_pubkey,
        )
        coordinator_output = SettlementOutput(
            output_index=index * 2 + 1,
            amount_sat=50_000,
            script_pubkey=coordinator.settlement_script_pubkey,
        )
        outputs.extend(
            [
                TxOutput(
                    value=maker_output.amount_sat, script=bytes.fromhex(maker_output.script_pubkey)
                ),
                TxOutput(
                    value=coordinator_output.amount_sat,
                    script=bytes.fromhex(coordinator_output.script_pubkey),
                ),
            ]
        )
        maker_terms = EndpointTerms(
            session_id=_SESSION,
            attempt=1,
            funding_outpoint=maker.funding_outpoint,
            input_index=index,
            participant_key=maker.participant_key,
            role="first",
            local_node_key=maker.node_key,
            peer_node_key=maker.peer_node_key,
            capacity_sat=maker.capacity_sat,
            frozen_local_claim_sat=maker.frozen_local_claim_sat,
            frozen_remote_claim_sat=maker.frozen_remote_claim_sat,
            settlement_outputs=(maker_output,),
            miner_fee_share_sat=1_000,
            claim_adjustment_sat=0,
            policy_ceilings=maker.policy_ceilings,
        )
        coordinator_terms = EndpointTerms(
            session_id=_SESSION,
            attempt=1,
            funding_outpoint=coordinator.funding_outpoint,
            input_index=index,
            participant_key=coordinator.participant_key,
            role="second",
            local_node_key=coordinator.node_key,
            peer_node_key=coordinator.peer_node_key,
            capacity_sat=coordinator.capacity_sat,
            frozen_local_claim_sat=coordinator.frozen_local_claim_sat,
            frozen_remote_claim_sat=coordinator.frozen_remote_claim_sat,
            settlement_outputs=(coordinator_output,),
            miner_fee_share_sat=0,
            claim_adjustment_sat=0,
            policy_ceilings=coordinator.policy_ceilings,
        )
        terms.extend((maker_terms, coordinator_terms))
        descriptors.append(
            ChannelDescriptor(
                funding_outpoint=maker.funding_outpoint,
                input_index=index,
                capacity_sat=maker.capacity_sat,
                first_participant_key=maker.participant_key,
                second_participant_key=coordinator.participant_key,
                first_terms_hash=endpoint_terms_hash(maker_terms).hex(),
                second_terms_hash=endpoint_terms_hash(coordinator_terms).hex(),
            )
        )
    fee = 3_000
    rate = (fee + estimate_vsize(["p2tr"] * 3, ["p2tr"] * 6) - 1) // estimate_vsize(
        ["p2tr"] * 3, ["p2tr"] * 6
    )
    transcript = ChannelInputTranscript(
        network="regtest",
        session_id=_SESSION,
        attempt=1,
        unsigned_parent_tx=serialize_transaction(2, inputs, outputs, 0, None).hex(),
        input_prevouts=tuple(prevouts),
        total_fee_sat=fee,
        fee_rate_sat_vb=rate,
        expiry=1_500,
        recovery_height=120,
        participant_keys=tuple(
            sorted((negotiation.coordinator_key, *(h.participant_key for h in remote.values())))
        ),
        coordinator_key=negotiation.coordinator_key,
        channels=tuple(descriptors),
    )
    return transcript, tuple(
        sorted(terms, key=lambda item: (item.funding_outpoint.key(), item.participant_key))
    )


def _approval(proposal: ChannelInputProposalPayload, secret: bytes) -> ChannelInputApprovalPayload:
    return sign_payload(
        ChannelInputApprovalPayload(
            session_id=_SESSION,
            attempt=1,
            signer_key=proposal.recipient_key,
            transcript_hash=proposal.transcript_hash,
            terms_hashes=tuple(
                sorted(proposal.transcript.terms_hashes_for(proposal.recipient_key))
            ),
        ),
        secret,
        aux_randomness=b"\x02" * 32,
    )


def _approve_all(
    negotiation: TakerChannelInputNegotiation,
    proposals: tuple[tuple[str, ChannelInputProposalPayload], ...],
    secrets: tuple[bytes, ...],
) -> None:
    for (nick, proposal), secret in zip(proposals, secrets, strict=True):
        negotiation.accept_approval(nick, _approval(proposal, secret))


def test_three_maker_prepare_round_trip_and_replays() -> None:
    negotiation, secrets = _ready()
    transcript, terms = _plan(negotiation)
    proposals = negotiation.plan_proposals(transcript, terms, current_time=100, current_height=100)
    assert (
        negotiation.plan_proposals(transcript, terms, current_time=100, current_height=100)
        == proposals
    )
    for (nick, proposal), secret in zip(proposals, secrets, strict=True):
        approval = sign_payload(
            ChannelInputApprovalPayload(
                session_id=_SESSION,
                attempt=1,
                signer_key=proposal.recipient_key,
                transcript_hash=proposal.transcript_hash,
                terms_hashes=tuple(sorted(transcript.terms_hashes_for(proposal.recipient_key))),
            ),
            secret,
            aux_randomness=b"\x02" * 32,
        )
        negotiation.accept_approval(nick, approval)
        negotiation.accept_approval(nick, approval)
    approval_set = negotiation.build_approval_set()
    for (nick, proposal), secret in zip(proposals, secrets, strict=True):
        ack = sign_payload(
            ChannelInputApprovalSetAckPayload(
                session_id=_SESSION,
                attempt=1,
                signer_key=proposal.recipient_key,
                transcript_hash=transcript_hash(transcript).hex(),
                approval_set_hash=approval_set_hash(approval_set).hex(),
            ),
            secret,
            aux_randomness=b"\x03" * 32,
        )
        negotiation.accept_approval_ack(nick, ack)
    prepare = negotiation.build_prepare()
    assert negotiation.build_prepare() == prepare
    assert negotiation.stage is TakerChannelInputStage.PREPARE_READY
    restored = TakerChannelInputNegotiation.from_snapshot(
        TakerChannelInputSnapshot.model_validate_json(negotiation.snapshot().model_dump_json())
    )
    assert restored.snapshot() == negotiation.snapshot()


def test_rejects_collisions_and_tampered_restore() -> None:
    negotiation, _ = _ready()
    remote = dict(negotiation.remote_hellos)["maker-a"]
    local = dict(negotiation.local_hellos)["maker-a"]
    with pytest.raises(ChannelInputValidationError):
        negotiation.accept_hello("maker-a", remote.model_copy(update={"sig": "00" * 64}), local)
    snapshot = negotiation.snapshot().model_copy(
        update={"replay_ledger_snapshot": (("invite", "00" * 32),)}
    )
    with pytest.raises(TakerChannelInputNegotiationError):
        TakerChannelInputNegotiation.from_snapshot(snapshot)
    payload = json.loads(negotiation.snapshot().model_dump_json())
    payload["coordinator_secret_key_hex"] = "00" * 32
    with pytest.raises((TakerChannelInputNegotiationError, ValueError)):
        TakerChannelInputNegotiation.from_snapshot(
            TakerChannelInputSnapshot.model_validate(payload)
        )


def test_partial_hello_snapshot_restore_is_canonical(tmp_path: Path) -> None:
    negotiation = _new()
    store = TakerChannelInputStore(tmp_path / "private", max_active_sessions=1)
    _accept_hello(negotiation, 0)
    assert negotiation.stage is TakerChannelInputStage.INVITED
    assert (
        TakerChannelInputNegotiation.from_snapshot(negotiation.snapshot()).snapshot()
        == negotiation.snapshot()
    )
    store.save(negotiation.snapshot())
    assert store.load(_SESSION, 1) == negotiation.snapshot()
    _accept_hello(negotiation, 1)
    assert (
        TakerChannelInputNegotiation.from_snapshot(
            TakerChannelInputSnapshot.model_validate_json(negotiation.snapshot().model_dump_json())
        ).snapshot()
        == negotiation.snapshot()
    )
    store.save(negotiation.snapshot())
    assert store.load(_SESSION, 1) == negotiation.snapshot()


def test_freeze_intent_snapshot_restores_before_rpc() -> None:
    negotiation = _new()
    remote, candidate = _freeze_intent(negotiation, 0)
    snapshot = TakerChannelInputSnapshot.model_validate_json(
        negotiation.snapshot().model_dump_json()
    )
    restored = TakerChannelInputNegotiation.from_snapshot(snapshot)

    assert restored.stage is TakerChannelInputStage.FREEZE_INTENDED
    assert restored.remote_hellos == (("maker-a", remote),)
    assert restored.local_hellos == ()
    assert dict(restored.freeze_intents)["maker-a"].funding_outpoint == candidate.funding_outpoint
    assert restored.snapshot() == negotiation.snapshot()


def test_frozen_result_creates_local_hello_and_advances_when_complete() -> None:
    negotiation = _new()
    for index in range(len(negotiation.selected_nicks)):
        _freeze_intent(negotiation, index)
    local_hellos = tuple(
        negotiation.install_frozen_channel(nick, _frozen_snapshot(index))
        for index, nick in enumerate(negotiation.selected_nicks)
    )

    assert negotiation.stage is TakerChannelInputStage.HELLOS_READY
    assert tuple(item for _, item in negotiation.local_hellos) == local_hellos
    assert (
        TakerChannelInputNegotiation.from_snapshot(
            TakerChannelInputSnapshot.model_validate_json(negotiation.snapshot().model_dump_json())
        ).snapshot()
        == negotiation.snapshot()
    )


def test_freeze_intent_and_result_replays_are_idempotent() -> None:
    negotiation = _new()
    remote, candidate = _freeze_intent(negotiation, 0)
    intent = negotiation.record_freeze_intent(
        "maker-a",
        remote,
        candidate,
        local_node_key=_node(10),
        settlement_script_pubkey="5120" + f"{50:064x}",
        quoted_miner_fee_share_sat=1_000,
        policy_ceilings=PolicyCeilings(
            max_miner_fee_share_sat=1_000,
            max_claim_adjustment_sat=1_000,
            max_settlement_outputs=1,
        ),
    )
    local = negotiation.install_frozen_channel("maker-a", _frozen_snapshot(0))

    assert (
        negotiation.record_freeze_intent(
            "maker-a",
            remote,
            candidate,
            local_node_key=_node(10),
            settlement_script_pubkey="5120" + f"{50:064x}",
            quoted_miner_fee_share_sat=1_000,
            policy_ceilings=intent.policy_ceilings,
        )
        == intent
    )
    assert negotiation.install_frozen_channel("maker-a", _frozen_snapshot(0)) == local


def test_freeze_candidate_snapshot_and_intent_mismatches_are_rejected() -> None:
    negotiation = _new()
    remote = _freeze_remote(0)
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_freeze_intent(
            "maker-a",
            remote,
            _candidate(0).model_copy(update={"peer_node_pubkey": _node(99)}),
            local_node_key=_node(10),
            settlement_script_pubkey="5120" + f"{50:064x}",
            quoted_miner_fee_share_sat=1_000,
            policy_ceilings=PolicyCeilings(
                max_miner_fee_share_sat=1_000,
                max_claim_adjustment_sat=1_000,
                max_settlement_outputs=1,
            ),
        )
    _freeze_intent(negotiation, 0)
    with pytest.raises(ChannelInputValidationError):
        negotiation.install_frozen_channel(
            "maker-a",
            _frozen_snapshot(0).model_copy(update={"peer_node_pubkey": bytes.fromhex(_node(99))}),
        )
    intent = dict(negotiation.freeze_intents)["maker-a"]
    tampered = negotiation.snapshot().model_copy(
        update={
            "freeze_intents": (("maker-a", intent.model_copy(update={"capacity_sat": 99_999})),)
        }
    )
    with pytest.raises(TakerChannelInputNegotiationError):
        TakerChannelInputNegotiation.from_snapshot(tampered)
    negotiation.install_frozen_channel("maker-a", _frozen_snapshot(0))
    frozen = dict(negotiation.frozen_inputs)["maker-a"]
    tampered_frozen = negotiation.snapshot().model_copy(
        update={
            "frozen_inputs": (
                (
                    "maker-a",
                    frozen.model_copy(
                        update={
                            "snapshot": frozen.snapshot.model_copy(
                                update={"peer_node_pubkey": bytes.fromhex(_node(99))}
                            )
                        }
                    ),
                ),
            )
        }
    )
    with pytest.raises(TakerChannelInputNegotiationError):
        TakerChannelInputNegotiation.from_snapshot(tampered_frozen)


def test_partial_multi_maker_freeze_progress_restores() -> None:
    negotiation = _new()
    for index in range(len(negotiation.selected_nicks)):
        _freeze_intent(negotiation, index)
    local = negotiation.install_frozen_channel("maker-a", _frozen_snapshot(0))

    restored = TakerChannelInputNegotiation.from_snapshot(negotiation.snapshot())
    assert restored.stage is TakerChannelInputStage.FREEZE_INTENDED
    assert dict(restored.local_hellos)["maker-a"] == local
    assert len(restored.freeze_intents) == 3
    assert len(restored.frozen_inputs) == 1


def test_partial_approvals_and_acks_restore() -> None:
    negotiation, secrets = _ready()
    transcript, terms = _plan(negotiation)
    proposals = negotiation.plan_proposals(transcript, terms, current_time=100, current_height=100)
    negotiation.accept_approval(proposals[0][0], _approval(proposals[0][1], secrets[0]))
    assert (
        TakerChannelInputNegotiation.from_snapshot(negotiation.snapshot()).snapshot()
        == negotiation.snapshot()
    )
    _approve_all(negotiation, proposals[1:], secrets[1:])
    approval_set = negotiation.build_approval_set()
    ack = sign_payload(
        ChannelInputApprovalSetAckPayload(
            session_id=_SESSION,
            attempt=1,
            signer_key=proposals[0][1].recipient_key,
            transcript_hash=transcript_hash(transcript).hex(),
            approval_set_hash=approval_set_hash(approval_set).hex(),
        ),
        secrets[0],
        aux_randomness=b"\x03" * 32,
    )
    negotiation.accept_approval_ack(proposals[0][0], ack)
    assert (
        TakerChannelInputNegotiation.from_snapshot(negotiation.snapshot()).snapshot()
        == negotiation.snapshot()
    )


def test_stage_evidence_mismatch_restore_rejected() -> None:
    negotiation, secrets = _ready()
    transcript, terms = _plan(negotiation)
    proposals = negotiation.plan_proposals(transcript, terms, current_time=100, current_height=100)
    for stage in (TakerChannelInputStage.INVITED, TakerChannelInputStage.HELLOS_READY):
        with pytest.raises(TakerChannelInputNegotiationError):
            TakerChannelInputNegotiation.from_snapshot(
                negotiation.snapshot().model_copy(update={"stage": stage})
            )
    _approve_all(negotiation, proposals, secrets)
    approval_set = negotiation.build_approval_set()
    with pytest.raises(TakerChannelInputNegotiationError):
        TakerChannelInputNegotiation.from_snapshot(
            negotiation.snapshot().model_copy(
                update={"stage": TakerChannelInputStage.PROPOSALS_READY}
            )
        )
    with pytest.raises(TakerChannelInputNegotiationError):
        TakerChannelInputNegotiation.from_snapshot(
            negotiation.snapshot().model_copy(
                update={"stage": TakerChannelInputStage.PREPARE_READY}
            )
        )
    for (nick, proposal), secret in zip(proposals, secrets, strict=True):
        negotiation.accept_approval_ack(
            nick,
            sign_payload(
                ChannelInputApprovalSetAckPayload(
                    session_id=_SESSION,
                    attempt=1,
                    signer_key=proposal.recipient_key,
                    transcript_hash=transcript_hash(transcript).hex(),
                    approval_set_hash=approval_set_hash(approval_set).hex(),
                ),
                secret,
                aux_randomness=b"\x06" * 32,
            ),
        )
    negotiation.build_prepare()
    with pytest.raises(TakerChannelInputNegotiationError):
        TakerChannelInputNegotiation.from_snapshot(
            negotiation.snapshot().model_copy(
                update={"stage": TakerChannelInputStage.APPROVAL_SET_READY}
            )
        )


def test_mapping_and_local_approval_tampering_restore_rejected() -> None:
    negotiation, secrets = _ready()
    transcript, terms = _plan(negotiation)
    proposals = negotiation.plan_proposals(transcript, terms, current_time=100, current_height=100)
    malformed = negotiation.snapshot().model_copy(
        update={"proposals": (proposals[0], proposals[0], proposals[2])}
    )
    with pytest.raises(TakerChannelInputNegotiationError):
        TakerChannelInputNegotiation.from_snapshot(malformed)
    assert negotiation.local_approval is not None
    bad_local = sign_payload(
        negotiation.local_approval.model_copy(update={"attempt": 2}),
        _COORDINATOR_SECRET,
        aux_randomness=b"\x05" * 32,
    )
    with pytest.raises(TakerChannelInputNegotiationError):
        TakerChannelInputNegotiation.from_snapshot(
            negotiation.snapshot().model_copy(update={"local_approval": bad_local})
        )
    _approve_all(negotiation, proposals, secrets)
    unordered = negotiation.snapshot().model_copy(
        update={"maker_approvals": tuple(reversed(negotiation.maker_approvals))}
    )
    with pytest.raises(TakerChannelInputNegotiationError):
        TakerChannelInputNegotiation.from_snapshot(unordered)


def test_hello_collisions_and_plan_mismatches_rejected() -> None:
    negotiation = _new()
    _accept_hello(negotiation, 0)
    remote = dict(negotiation.remote_hellos)["maker-a"]
    local = dict(negotiation.local_hellos)["maker-a"]
    with pytest.raises(ChannelInputValidationError):
        negotiation.accept_hello("maker-b", remote, local)
    _accept_hello(negotiation, 1)
    _accept_hello(negotiation, 2)
    transcript, terms = _plan(negotiation)
    with pytest.raises(ChannelInputValidationError):
        negotiation.plan_proposals(
            transcript.model_copy(update={"participant_keys": (negotiation.coordinator_key,)}),
            terms,
            current_time=100,
            current_height=100,
        )
    mismatched = list(terms)
    mismatched[0] = mismatched[0].model_copy(
        update={
            "settlement_outputs": (
                SettlementOutput(
                    output_index=0, amount_sat=49_000, script_pubkey="5120" + "99" * 32
                ),
            )
        }
    )
    with pytest.raises(ChannelInputValidationError):
        negotiation.plan_proposals(
            transcript,
            tuple(
                sorted(
                    mismatched, key=lambda item: (item.funding_outpoint.key(), item.participant_key)
                )
            ),
            current_time=100,
            current_height=100,
        )


def test_cancel_requires_all_remote_and_local_resume() -> None:
    negotiation, secrets = _ready()
    transcript, terms = _plan(negotiation)
    proposals = negotiation.plan_proposals(transcript, terms, current_time=100, current_height=100)
    cancel = negotiation.plan_cancel("user_cancel")
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_local_resumed(())
    for (nick, proposal), secret in zip(proposals, secrets, strict=True):
        outpoint = dict(negotiation.remote_hellos)[nick].funding_outpoint
        ack = sign_payload(
            ChannelInputCancelAckPayload(
                session_id=_SESSION,
                attempt=1,
                signer_key=proposal.recipient_key,
                transcript_hash=transcript_hash(transcript).hex(),
                cancel_hash=cancel_payload_hash(cancel).hex(),
                resumed_outpoints=(outpoint,),
            ),
            secret,
            aux_randomness=b"\x04" * 32,
        )
        negotiation.accept_cancel_ack(nick, ack)
    local_outpoints = tuple(
        sorted(
            (hello.funding_outpoint for _, hello in negotiation.local_hellos), key=lambda x: x.key()
        )
    )
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_local_resumed(local_outpoints[:-1])
    with pytest.raises(ChannelInputValidationError):
        negotiation.record_local_resumed(tuple(reversed(local_outpoints)))
    negotiation.record_local_resumed(local_outpoints)
    assert negotiation.stage is TakerChannelInputStage.CANCELED


@pytest.mark.parametrize(
    "cancel_stage",
    (
        TakerChannelInputStage.PROPOSALS_READY,
        TakerChannelInputStage.APPROVAL_SET_READY,
        TakerChannelInputStage.PREPARE_READY,
    ),
)
def test_cancel_is_durable_from_every_pre_partial_stage(
    cancel_stage: TakerChannelInputStage,
) -> None:
    negotiation, secrets = _ready()
    transcript, terms = _plan(negotiation)
    proposals = negotiation.plan_proposals(transcript, terms, current_time=100, current_height=100)
    if cancel_stage is not TakerChannelInputStage.PROPOSALS_READY:
        _approve_all(negotiation, proposals, secrets)
        approval_set = negotiation.build_approval_set()
        if cancel_stage is TakerChannelInputStage.PREPARE_READY:
            for (nick, proposal), secret in zip(proposals, secrets, strict=True):
                negotiation.accept_approval_ack(
                    nick,
                    sign_payload(
                        ChannelInputApprovalSetAckPayload(
                            session_id=_SESSION,
                            attempt=1,
                            signer_key=proposal.recipient_key,
                            transcript_hash=transcript_hash(transcript).hex(),
                            approval_set_hash=approval_set_hash(approval_set).hex(),
                        ),
                        secret,
                        aux_randomness=b"\x07" * 32,
                    ),
                )
            negotiation.build_prepare()

    assert negotiation.stage is cancel_stage
    cancel = negotiation.plan_cancel("phase_timeout")
    assert negotiation.plan_cancel("phase_timeout") == cancel
    assert negotiation.stage is TakerChannelInputStage.CANCEL_INTENDED
    assert (
        TakerChannelInputNegotiation.from_snapshot(negotiation.snapshot()).snapshot()
        == negotiation.snapshot()
    )
    with pytest.raises(ChannelInputValidationError, match="reason conflicts"):
        negotiation.plan_cancel("user_cancel")
