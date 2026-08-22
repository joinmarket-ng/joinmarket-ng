from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import TxInput, TxOutput, estimate_vsize, serialize_transaction
from jmcore.channel_input import (
    MAX_CHANNELS,
    ChannelDescriptor,
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputInvitePayload,
    ChannelInputKeyPair,
    ChannelInputPreparePayload,
    ChannelInputProposalPayload,
    ChannelInputTranscript,
    ChannelInputValidationError,
    EndpointTerms,
    InputPrevout,
    PolicyCeilings,
    SettlementOutput,
    approval_set_hash,
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
)

from maker.channel_input import (
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
    return PrivateKey(index.to_bytes(32, "big")).public_key.format(compressed=True).hex()


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
        tuple(_candidate(item, channel_id=index) for index, item in enumerate(frozen))
    )


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

    intents = negotiation.plan_freeze(tuple(reversed(candidates)))
    assert intents == negotiation.plan_freeze(candidates)
    assert negotiation.stage is MakerChannelInputStage.FREEZE_INTENDED

    conflict = candidates[0].model_copy(update={"capacity_sat": 99_999})
    with pytest.raises(ChannelInputValidationError):
        negotiation.plan_freeze((conflict, candidates[1]))

    negotiation.install_frozen_inputs(frozen)
    assert negotiation.plan_freeze(candidates) == intents

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
        negotiation.plan_freeze(supplied)


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
        negotiation.plan_freeze((candidate,))


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
    "failure", ["channel_id", "outpoint", "peer", "capacity", "missing", "extra"]
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
