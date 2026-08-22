from __future__ import annotations

import base64
import json
from typing import Literal, cast

import pytest
from coincurve import PrivateKey
from pydantic import ValidationError

from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    estimate_vsize,
    hash256,
    parse_transaction_bytes,
    serialize_transaction,
)
from jmcore.channel_input import (
    MAX_CHANNEL_INPUT_PAYLOAD_BYTES,
    ChannelDescriptor,
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputCancelAckPayload,
    ChannelInputCancelPayload,
    ChannelInputFinalizedPayload,
    ChannelInputFinalizePayload,
    ChannelInputHelloPayload,
    ChannelInputInvitePayload,
    ChannelInputKeyPair,
    ChannelInputPartialPayload,
    ChannelInputPreparedPayload,
    ChannelInputPreparePayload,
    ChannelInputProposalPayload,
    ChannelInputSignPayload,
    ChannelInputTranscript,
    ChannelInputValidationError,
    EndpointTerms,
    InputPrevout,
    Outpoint,
    PolicyCeilings,
    SettlementOutput,
    approval_set_hash,
    cancel_payload_hash,
    cancellation_binding_hash,
    canonical_json,
    decode_channel_input_message,
    encode_channel_input_message,
    endpoint_terms_hash,
    payload_hash,
    sign_payload,
    transcript_hash,
    validate_cancel_ack,
    validate_channel_payload_for_transcript,
    validate_complete_approval_set,
    validate_finalize_for_transcript,
    validate_finalized_for_transcript,
    validate_hello_for_invite,
    validate_hello_pair,
    validate_partial_for_transcript,
    validate_prepare_payload,
    validate_prepared_for_transcript,
    validate_proposal,
    validate_proposal_for_hellos,
    validate_recipient_approval,
    validate_revealed_endpoint_pair,
    validate_sign_for_transcript,
    validate_transcript_against_transaction,
    validate_transcript_for_invite,
    verify_payload,
)


def _secret(index: int) -> bytes:
    return index.to_bytes(32, "big")


def _key(index: int) -> str:
    return ChannelInputKeyPair.from_secret(_secret(index)).public_key


def _node(index: int) -> str:
    return PrivateKey(_secret(index)).public_key.format(compressed=True).hex()


def _ceilings() -> PolicyCeilings:
    return PolicyCeilings(
        max_miner_fee_share_sat=1_000,
        max_claim_adjustment_sat=2_000,
        max_settlement_outputs=2,
    )


def _terms(
    participant: int,
    peer: int,
    *,
    role: Literal["first", "second"],
    funding_index: int,
    output_index: int,
    adjustment: int = 0,
) -> EndpointTerms:
    return EndpointTerms(
        session_id="11" * 32,
        attempt=3,
        funding_outpoint=Outpoint(txid=f"{funding_index + 1:064x}", vout=funding_index),
        input_index=funding_index,
        participant_key=_key(participant),
        role=role,
        local_node_key=_node(participant + 20),
        peer_node_key=_node(peer + 20),
        capacity_sat=100_000,
        frozen_local_claim_sat=50_000,
        frozen_remote_claim_sat=50_000,
        settlement_outputs=(
            SettlementOutput(
                output_index=output_index,
                amount_sat=49_000 + adjustment,
                script_pubkey="5120" + f"{output_index + 101:064x}",
            ),
        ),
        miner_fee_share_sat=1_000,
        claim_adjustment_sat=adjustment,
        policy_ceilings=_ceilings(),
    )


def _fee_rate(fee_sat: int, inputs: int = 2, outputs: int = 4) -> int:
    vsize = estimate_vsize(["p2tr"] * inputs, ["p2tr"] * outputs)
    return (fee_sat + vsize - 1) // vsize


def _transcript() -> tuple[ChannelInputTranscript, tuple[EndpointTerms, ...]]:
    terms = (
        _terms(1, 2, role="first", funding_index=0, output_index=0),
        _terms(2, 1, role="second", funding_index=0, output_index=1),
        _terms(3, 4, role="first", funding_index=1, output_index=2),
        _terms(4, 3, role="second", funding_index=1, output_index=3),
    )
    inputs = [TxInput.from_hex(f"{index + 1:064x}", index) for index in range(2)]
    outputs = [
        TxOutput(value=49_000, script=bytes.fromhex("5120" + f"{index + 101:064x}"))
        for index in range(4)
    ]
    fee_sat = 4_000
    transcript = ChannelInputTranscript(
        network="regtest",
        session_id="11" * 32,
        attempt=3,
        unsigned_parent_tx=serialize_transaction(2, inputs, outputs, 0).hex(),
        input_prevouts=tuple(
            InputPrevout(
                outpoint=Outpoint(txid=f"{index + 1:064x}", vout=index),
                value_sat=100_000,
                script_pubkey="5120" + f"{index + 201:064x}",
            )
            for index in range(2)
        ),
        total_fee_sat=fee_sat,
        fee_rate_sat_vb=_fee_rate(fee_sat),
        expiry=1_900_000_000,
        recovery_height=999_999,
        participant_keys=tuple(_key(index) for index in range(1, 5)),
        coordinator_key=_key(1),
        channels=(
            ChannelDescriptor(
                funding_outpoint=terms[0].funding_outpoint,
                input_index=0,
                capacity_sat=100_000,
                first_participant_key=_key(1),
                second_participant_key=_key(2),
                first_terms_hash=endpoint_terms_hash(terms[0]).hex(),
                second_terms_hash=endpoint_terms_hash(terms[1]).hex(),
            ),
            ChannelDescriptor(
                funding_outpoint=terms[2].funding_outpoint,
                input_index=1,
                capacity_sat=100_000,
                first_participant_key=_key(3),
                second_participant_key=_key(4),
                first_terms_hash=endpoint_terms_hash(terms[2]).hex(),
                second_terms_hash=endpoint_terms_hash(terms[3]).hex(),
            ),
        ),
    )
    return transcript, terms


def _signed_invite() -> ChannelInputInvitePayload:
    return sign_payload(
        ChannelInputInvitePayload(
            session_id="11" * 32,
            attempt=3,
            signer_key=_key(1),
            network="regtest",
            expiry=1_900_000_000,
            max_parent_fee_sat=10_000,
            max_fee_rate_sat_vb=100,
            min_recovery_delta=6,
            max_recovery_delta=144,
        ),
        _secret(1),
        aux_randomness=b"\x00" * 32,
    )


def _signed_hello() -> ChannelInputHelloPayload:
    return sign_payload(
        ChannelInputHelloPayload(
            session_id="11" * 32,
            attempt=3,
            signer_key=_key(2),
            participant_key=_key(2),
            node_key=_node(22),
            peer_node_key=_node(23),
            funding_outpoint=Outpoint(txid="01" * 32, vout=0),
            capacity_sat=100_000,
            funding_script_pubkey="5120" + "22" * 32,
            frozen_local_claim_sat=50_000,
            frozen_remote_claim_sat=50_000,
            settlement_script_pubkey="5120" + "33" * 32,
            quoted_miner_fee_share_sat=1_000,
            policy_ceilings=_ceilings(),
        ),
        _secret(2),
        aux_randomness=b"\x01" * 32,
    )


def _signed_cancel(transcript: ChannelInputTranscript) -> ChannelInputCancelPayload:
    return sign_payload(
        ChannelInputCancelPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(1),
            transcript_hash=transcript_hash(transcript).hex(),
            reason_code="maker_unavailable",
        ),
        _secret(1),
        aux_randomness=b"\x50" * 32,
    )


def _signed_cancel_ack(
    cancel: ChannelInputCancelPayload,
    resumed_outpoints: tuple[Outpoint, ...],
    *,
    signer: int = 2,
    aux: int = 51,
) -> ChannelInputCancelAckPayload:
    return sign_payload(
        ChannelInputCancelAckPayload(
            session_id=cancel.session_id,
            attempt=cancel.attempt,
            signer_key=_key(signer),
            transcript_hash=cancel.transcript_hash,
            cancel_hash=cancel_payload_hash(cancel).hex(),
            resumed_outpoints=resumed_outpoints,
        ),
        _secret(signer),
        aux_randomness=bytes([aux]) * 32,
    )


def _signed_hello_for_terms(
    terms: EndpointTerms, *, signer_index: int = 2
) -> ChannelInputHelloPayload:
    return sign_payload(
        ChannelInputHelloPayload(
            session_id=terms.session_id,
            attempt=terms.attempt,
            signer_key=_key(signer_index),
            participant_key=_key(signer_index),
            node_key=terms.local_node_key,
            peer_node_key=terms.peer_node_key,
            funding_outpoint=terms.funding_outpoint,
            capacity_sat=terms.capacity_sat,
            funding_script_pubkey="5120" + f"{terms.input_index + 201:064x}",
            frozen_local_claim_sat=terms.frozen_local_claim_sat,
            frozen_remote_claim_sat=terms.frozen_remote_claim_sat,
            settlement_script_pubkey=terms.settlement_outputs[0].script_pubkey,
            quoted_miner_fee_share_sat=terms.miner_fee_share_sat,
            policy_ceilings=terms.policy_ceilings,
        ),
        _secret(signer_index),
        aux_randomness=b"\x02" * 32,
    )


def _signed_proposal(
    transcript: ChannelInputTranscript, recipient_terms: tuple[EndpointTerms, ...]
) -> ChannelInputProposalPayload:
    return sign_payload(
        ChannelInputProposalPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(1),
            recipient_key=_key(2),
            transcript=transcript,
            transcript_hash=transcript_hash(transcript).hex(),
            recipient_terms=recipient_terms,
        ),
        _secret(1),
        aux_randomness=b"\x03" * 32,
    )


def _approval_set(transcript: ChannelInputTranscript) -> ChannelInputApprovalSetPayload:
    approvals = tuple(
        sign_payload(
            ChannelInputApprovalPayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=_key(index),
                transcript_hash=transcript_hash(transcript).hex(),
                terms_hashes=tuple(sorted(transcript.terms_hashes_for(_key(index)))),
            ),
            _secret(index),
            aux_randomness=bytes([index]) * 32,
        )
        for index in range(1, 5)
    )
    return sign_payload(
        ChannelInputApprovalSetPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(1),
            transcript=transcript,
            approvals=approvals,
        ),
        _secret(1),
        aux_randomness=b"\x05" * 32,
    )


def _acks(
    approval_set: ChannelInputApprovalSetPayload,
) -> tuple[ChannelInputApprovalSetAckPayload, ...]:
    transcript = approval_set.transcript
    return tuple(
        sign_payload(
            ChannelInputApprovalSetAckPayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=_key(index),
                transcript_hash=transcript_hash(transcript).hex(),
                approval_set_hash=approval_set_hash(approval_set).hex(),
            ),
            _secret(index),
            aux_randomness=bytes([index + 10]) * 32,
        )
        for index in range(1, 5)
    )


def _prepare(approval_set: ChannelInputApprovalSetPayload) -> ChannelInputPreparePayload:
    transcript = approval_set.transcript
    return sign_payload(
        ChannelInputPreparePayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(1),
            transcript_hash=transcript_hash(transcript).hex(),
            approval_set_hash=approval_set_hash(approval_set).hex(),
            approval_set=approval_set,
            approval_set_acks=_acks(approval_set),
        ),
        _secret(1),
        aux_randomness=b"\x14" * 32,
    )


def _resign_prepare(
    prepare: ChannelInputPreparePayload, *, signer: int = 1, aux: int = 21
) -> ChannelInputPreparePayload:
    return sign_payload(prepare, _secret(signer), aux_randomness=bytes([aux]) * 32)


def _post_approval_payloads() -> tuple[
    ChannelInputTranscript,
    ChannelInputPreparedPayload,
    ChannelInputPreparedPayload,
    ChannelInputSignPayload,
    ChannelInputPartialPayload,
    ChannelInputFinalizePayload,
]:
    transcript, _ = _transcript()
    digest = transcript_hash(transcript).hex()
    outpoint = transcript.channels[0].funding_outpoint
    first_nonce = "aa" * 66
    second_nonce = "bb" * 66
    first_prepared = cast(
        ChannelInputPreparedPayload,
        sign_payload(
            ChannelInputPreparedPayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=_key(1),
                transcript_hash=digest,
                funding_outpoint=outpoint,
                public_nonce=first_nonce,
            ),
            _secret(1),
            aux_randomness=b"\x31" * 32,
        ),
    )
    second_prepared = cast(
        ChannelInputPreparedPayload,
        sign_payload(
            ChannelInputPreparedPayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=_key(2),
                transcript_hash=digest,
                funding_outpoint=outpoint,
                public_nonce=second_nonce,
            ),
            _secret(2),
            aux_randomness=b"\x32" * 32,
        ),
    )
    sign = cast(
        ChannelInputSignPayload,
        sign_payload(
            ChannelInputSignPayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=transcript.coordinator_key,
                transcript_hash=digest,
                funding_outpoint=outpoint,
                local_public_nonce=first_nonce,
                peer_prepared=second_prepared,
            ),
            _secret(1),
            aux_randomness=b"\x33" * 32,
        ),
    )
    partial = cast(
        ChannelInputPartialPayload,
        sign_payload(
            ChannelInputPartialPayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=_key(2),
                transcript_hash=digest,
                funding_outpoint=outpoint,
                local_public_nonce=second_nonce,
                peer_public_nonce=first_nonce,
                partial_signature="cc" * 32,
            ),
            _secret(2),
            aux_randomness=b"\x34" * 32,
        ),
    )
    finalize = cast(
        ChannelInputFinalizePayload,
        sign_payload(
            ChannelInputFinalizePayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=transcript.coordinator_key,
                transcript_hash=digest,
                funding_outpoint=outpoint,
                local_public_nonce=first_nonce,
                peer_partial=partial,
            ),
            _secret(1),
            aux_randomness=b"\x35" * 32,
        ),
    )
    return transcript, first_prepared, second_prepared, sign, partial, finalize


def test_full_multi_maker_round_and_observation_barrier() -> None:
    transcript, terms = _transcript()
    validate_transcript_against_transaction(
        transcript, bytes.fromhex(transcript.unsigned_parent_tx), transcript.input_prevouts
    )
    validate_revealed_endpoint_pair(transcript, transcript.channels[0], terms[0], terms[1])
    validate_revealed_endpoint_pair(transcript, transcript.channels[1], terms[2], terms[3])

    proposal = sign_payload(
        ChannelInputProposalPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(1),
            recipient_key=_key(2),
            transcript=transcript,
            transcript_hash=transcript_hash(transcript).hex(),
            recipient_terms=(terms[1],),
        ),
        _secret(1),
        aux_randomness=b"\x06" * 32,
    )
    validate_proposal(proposal)
    approval_set = _approval_set(transcript)
    validate_complete_approval_set(approval_set)
    validate_recipient_approval(approval_set, _key(2), (terms[1],))
    prepare = _prepare(approval_set)
    validate_prepare_payload(prepare)
    assert decode_channel_input_message(encode_channel_input_message(prepare)) == prepare


def test_proposal_binds_distinct_recipient_and_coordinator() -> None:
    transcript, terms = _transcript()
    proposal = ChannelInputProposalPayload(
        session_id=transcript.session_id,
        attempt=transcript.attempt,
        signer_key=_key(1),
        recipient_key=_key(2),
        transcript=transcript,
        transcript_hash=transcript_hash(transcript).hex(),
        recipient_terms=(terms[1],),
    )
    signed = sign_payload(proposal, _secret(1), aux_randomness=b"\x06" * 32)
    assert not verify_payload(signed.model_copy(update={"signer_key": _key(3)}))
    assert not verify_payload(signed.model_copy(update={"recipient_key": _key(3)}))
    with pytest.raises(ValidationError, match="proposal recipient"):
        ChannelInputProposalPayload(**{**proposal.model_dump(), "recipient_key": _key(9)})
    with pytest.raises(ValidationError, match="must differ"):
        ChannelInputProposalPayload(**{**proposal.model_dump(), "recipient_key": _key(1)})
    with pytest.raises(ValidationError, match="recipient terms do not belong"):
        ChannelInputProposalPayload(**{**proposal.model_dump(), "recipient_key": _key(3)})
    with pytest.raises(ValidationError, match="proposal signer"):
        ChannelInputProposalPayload(**{**proposal.model_dump(), "signer_key": _key(9)})
    with pytest.raises(ValidationError, match="transcript coordinator"):
        ChannelInputProposalPayload(**{**proposal.model_dump(), "signer_key": _key(3)})
    with pytest.raises(ValidationError, match="transcript_hash"):
        ChannelInputProposalPayload(**{**proposal.model_dump(), "transcript_hash": "00" * 32})


def test_proposal_rejects_descriptor_owner_substitution() -> None:
    transcript, terms = _transcript()
    substituted_descriptor = transcript.channels[0].model_copy(
        update={"second_participant_key": _key(3)}
    )
    substituted_transcript = _transcript_with_descriptor(transcript, substituted_descriptor)
    with pytest.raises(ValidationError, match="not assigned by transcript descriptor"):
        ChannelInputProposalPayload(
            session_id=substituted_transcript.session_id,
            attempt=substituted_transcript.attempt,
            signer_key=_key(1),
            recipient_key=_key(2),
            transcript=substituted_transcript,
            transcript_hash=transcript_hash(substituted_transcript).hex(),
            recipient_terms=(terms[1],),
        )


@pytest.mark.parametrize("declared_delta", [-1, 1])
def test_transcript_rejects_lie_about_actual_fee_rate(declared_delta: int) -> None:
    transcript, _ = _transcript()
    with pytest.raises(ValidationError, match="declared fee rate"):
        ChannelInputTranscript(
            **{
                **transcript.model_dump(),
                "fee_rate_sat_vb": transcript.fee_rate_sat_vb + declared_delta,
            }
        )


def test_transcript_rejects_unsupported_prevout_and_output_scripts() -> None:
    transcript, _ = _transcript()
    with pytest.raises(ValidationError, match="native P2WPKH"):
        InputPrevout(
            outpoint=transcript.input_prevouts[0].outpoint,
            value_sat=100_000,
            script_pubkey="76a914" + "11" * 20 + "88ac",
        )
    parsed = parse_transaction_bytes(bytes.fromhex(transcript.unsigned_parent_tx))
    outputs = list(parsed.outputs)
    outputs[0] = TxOutput(value=49_000, script=b"\x51")
    bad_unsigned = serialize_transaction(
        parsed.version, parsed.inputs, outputs, parsed.locktime
    ).hex()
    with pytest.raises(ValidationError, match="parent output script"):
        ChannelInputTranscript(**{**transcript.model_dump(), "unsigned_parent_tx": bad_unsigned})


def test_transcript_validates_coordinator_and_descriptor_roster_ownership() -> None:
    transcript, _ = _transcript()
    with pytest.raises(ValidationError, match="coordinator_key"):
        ChannelInputTranscript(**{**transcript.model_dump(), "coordinator_key": _key(9)})
    duplicate_endpoints = transcript.channels[0].model_copy(
        update={"second_participant_key": _key(1)}
    )
    with pytest.raises(ValidationError, match="endpoint participant keys must differ"):
        ChannelDescriptor(**duplicate_endpoints.model_dump())
    unrostered_owner = transcript.channels[0].model_copy(update={"first_participant_key": _key(9)})
    with pytest.raises(ValidationError, match="descriptor participant"):
        ChannelInputTranscript(
            **{**transcript.model_dump(), "channels": (unrostered_owner,) + transcript.channels[1:]}
        )


def test_invite_hello_signatures_and_expiry_are_verified_without_clock() -> None:
    transcript, _ = _transcript()
    invite = _signed_invite()
    hello = _signed_hello()
    validate_hello_for_invite(invite, hello)
    validate_transcript_for_invite(
        invite, transcript, current_time=1_800_000_000, current_height=999_900
    )
    with pytest.raises(ChannelInputValidationError, match="invite BIP340"):
        validate_hello_for_invite(invite.model_copy(update={"sig": "00" * 64}), hello)
    with pytest.raises(ChannelInputValidationError, match="hello BIP340"):
        validate_hello_for_invite(invite, hello.model_copy(update={"sig": "00" * 64}))
    with pytest.raises(ChannelInputValidationError, match="invite has expired"):
        validate_transcript_for_invite(
            invite, transcript, current_time=invite.expiry, current_height=999_900
        )
    with pytest.raises(ChannelInputValidationError, match="transcript has expired"):
        validate_transcript_for_invite(
            invite,
            transcript.model_copy(update={"expiry": 1_700_000_000}),
            current_time=1_800_000_000,
            current_height=999_900,
        )
    with pytest.raises(ChannelInputValidationError, match="transcript expiry exceeds"):
        validate_transcript_for_invite(
            invite,
            transcript.model_copy(update={"expiry": invite.expiry + 1}),
            current_time=1_800_000_000,
            current_height=999_900,
        )
    with pytest.raises(ChannelInputValidationError, match="invite signer.*coordinator"):
        validate_transcript_for_invite(
            invite,
            transcript.model_copy(update={"coordinator_key": _key(2)}),
            current_time=1_800_000_000,
            current_height=999_900,
        )


def test_hello_requires_frozen_channel_evidence() -> None:
    hello = _signed_hello()
    for update, match in (
        ({"funding_script_pubkey": "0014" + "44" * 20}, "funding_script_pubkey.*P2TR"),
        ({"peer_node_key": hello.node_key}, "local and peer node keys must differ"),
        (
            {"frozen_local_claim_sat": 50_001, "frozen_remote_claim_sat": 50_000},
            "frozen claims must conserve",
        ),
    ):
        with pytest.raises(ValidationError, match=match):
            ChannelInputHelloPayload(**{**hello.model_dump(), **update})


def test_hello_pair_binds_reciprocal_frozen_channel_evidence() -> None:
    _, terms = _transcript()
    first = _signed_hello_for_terms(terms[0], signer_index=1)
    second = _signed_hello_for_terms(terms[1])

    validate_hello_pair(second, first)

    substitutions = (
        (first.model_copy(update={"sig": "00" * 64}), "second hello BIP340"),
        (
            sign_payload(
                second.model_copy(update={"attempt": second.attempt + 1}),
                _secret(2),
                aux_randomness=b"\x21" * 32,
            ),
            "session or attempt",
        ),
        (
            sign_payload(
                second.model_copy(update={"funding_outpoint": Outpoint(txid="99" * 32, vout=1)}),
                _secret(2),
                aux_randomness=b"\x22" * 32,
            ),
            "funding outpoint",
        ),
        (
            sign_payload(
                second.model_copy(
                    update={
                        "capacity_sat": 99_999,
                        "frozen_local_claim_sat": 49_999,
                        "frozen_remote_claim_sat": 50_000,
                    }
                ),
                _secret(2),
                aux_randomness=b"\x23" * 32,
            ),
            "capacity",
        ),
        (
            sign_payload(
                second.model_copy(update={"funding_script_pubkey": "5120" + "99" * 32}),
                _secret(2),
                aux_randomness=b"\x24" * 32,
            ),
            "funding script",
        ),
        (
            sign_payload(
                second.model_copy(update={"signer_key": _key(1), "participant_key": _key(1)}),
                _secret(1),
                aux_randomness=b"\x25" * 32,
            ),
            "participants must differ",
        ),
        (
            sign_payload(
                second.model_copy(update={"peer_node_key": _node(30)}),
                _secret(2),
                aux_randomness=b"\x26" * 32,
            ),
            "node identities",
        ),
        (
            sign_payload(
                second.model_copy(
                    update={"frozen_local_claim_sat": 49_999, "frozen_remote_claim_sat": 50_001}
                ),
                _secret(2),
                aux_randomness=b"\x27" * 32,
            ),
            "frozen claims",
        ),
    )
    for substituted, match in substitutions:
        with pytest.raises(ChannelInputValidationError, match=match):
            validate_hello_pair(first, substituted)


def test_proposal_for_hellos_binds_signed_recipient_claims() -> None:
    transcript, terms = _transcript()
    invite = _signed_invite()
    hello = _signed_hello_for_terms(terms[1])
    proposal = _signed_proposal(transcript, (terms[1],))

    validate_proposal_for_hellos(
        invite, (hello,), proposal, current_time=1_800_000_000, current_height=999_900
    )
    with pytest.raises(ChannelInputValidationError, match="finite sequence"):
        validate_proposal_for_hellos(
            invite,
            iter((hello,)),  # type: ignore[arg-type]
            proposal,
            current_time=1_800_000_000,
            current_height=999_900,
        )

    wrong_actor = sign_payload(
        hello.model_copy(update={"signer_key": _key(3), "participant_key": _key(3)}),
        _secret(3),
        aux_randomness=b"\x04" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="participant does not match"):
        validate_proposal_for_hellos(
            invite, (wrong_actor,), proposal, current_time=1_800_000_000, current_height=999_900
        )

    mutations = (
        ({"funding_outpoint": terms[2].funding_outpoint}, "outpoints do not exactly"),
        ({"capacity_sat": 99_999}, "capacity does not match"),
        ({"node_key": _node(30)}, "local node key"),
        ({"peer_node_key": _node(30)}, "peer node key"),
        (
            {"frozen_local_claim_sat": 49_999, "frozen_remote_claim_sat": 50_001},
            "frozen claims",
        ),
        (
            {
                "policy_ceilings": hello.policy_ceilings.model_copy(
                    update={"max_claim_adjustment_sat": 1_999}
                )
            },
            "policy ceilings",
        ),
        ({"quoted_miner_fee_share_sat": 999}, "fee share exceeds hello quote"),
        ({"settlement_script_pubkey": "5120" + "33" * 32}, "settlement script"),
    )
    for update, match in mutations:
        mutated = sign_payload(
            hello.model_copy(update=update), _secret(2), aux_randomness=b"\x05" * 32
        )
        with pytest.raises(ChannelInputValidationError, match=match):
            validate_proposal_for_hellos(
                invite, (mutated,), proposal, current_time=1_800_000_000, current_height=999_900
            )

    substituted_prevouts = list(transcript.input_prevouts)
    substituted_prevouts[terms[1].input_index] = substituted_prevouts[
        terms[1].input_index
    ].model_copy(update={"script_pubkey": "5120" + "99" * 32})
    substituted_transcript = ChannelInputTranscript(
        **{**transcript.model_dump(), "input_prevouts": tuple(substituted_prevouts)}
    )
    substituted_proposal = sign_payload(
        ChannelInputProposalPayload(
            **{
                **proposal.model_dump(),
                "transcript": substituted_transcript,
                "transcript_hash": transcript_hash(substituted_transcript).hex(),
            }
        ),
        _secret(1),
        aux_randomness=b"\x28" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="transcript funding script"):
        validate_proposal_for_hellos(
            invite,
            (hello,),
            substituted_proposal,
            current_time=1_800_000_000,
            current_height=999_900,
        )

    excess = sign_payload(
        hello.model_copy(update={"funding_outpoint": Outpoint(txid="99" * 32, vout=1)}),
        _secret(2),
        aux_randomness=b"\x06" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="duplicate hello"):
        validate_proposal_for_hellos(
            invite, (hello, hello), proposal, current_time=1_800_000_000, current_height=999_900
        )
    with pytest.raises(ChannelInputValidationError, match="must not be empty"):
        validate_proposal_for_hellos(
            invite, (), proposal, current_time=1_800_000_000, current_height=999_900
        )
    with pytest.raises(ChannelInputValidationError, match="outpoints do not exactly"):
        validate_proposal_for_hellos(
            invite, (hello, excess), proposal, current_time=1_800_000_000, current_height=999_900
        )
    with pytest.raises(ChannelInputValidationError, match="hello BIP340"):
        validate_proposal_for_hellos(
            invite,
            (hello.model_copy(update={"sig": "00" * 64}),),
            proposal,
            current_time=1_800_000_000,
            current_height=999_900,
        )
    with pytest.raises(ChannelInputValidationError, match="proposal BIP340"):
        validate_proposal_for_hellos(
            invite,
            (hello,),
            proposal.model_copy(update={"sig": "00" * 64}),
            current_time=1_800_000_000,
            current_height=999_900,
        )
    with pytest.raises(ChannelInputValidationError, match="invite BIP340"):
        validate_proposal_for_hellos(
            invite.model_copy(update={"sig": "00" * 64}),
            (hello,),
            proposal,
            current_time=1_800_000_000,
            current_height=999_900,
        )
    with pytest.raises(ChannelInputValidationError, match="invite has expired"):
        validate_proposal_for_hellos(
            invite, (hello,), proposal, current_time=invite.expiry, current_height=999_900
        )
    with pytest.raises(ChannelInputValidationError, match="recovery height"):
        validate_proposal_for_hellos(
            invite, (hello,), proposal, current_time=1_800_000_000, current_height=999_000
        )


def test_proposal_for_hellos_allows_multiple_endpoints_for_one_participant() -> None:
    transcript, terms = _transcript()
    second_recipient_terms = terms[2].model_copy(update={"participant_key": _key(2)})
    second_descriptor = transcript.channels[1].model_copy(
        update={
            "first_participant_key": _key(2),
            "first_terms_hash": endpoint_terms_hash(second_recipient_terms).hex(),
        }
    )
    reassigned = transcript.model_copy(
        update={"channels": (transcript.channels[0], second_descriptor)}
    )
    proposal = _signed_proposal(reassigned, (terms[1], second_recipient_terms))
    validate_proposal_for_hellos(
        _signed_invite(),
        (_signed_hello_for_terms(terms[1]), _signed_hello_for_terms(second_recipient_terms)),
        proposal,
        current_time=1_800_000_000,
        current_height=999_900,
    )


def _transcript_with_descriptor(
    transcript: ChannelInputTranscript, descriptor: ChannelDescriptor
) -> ChannelInputTranscript:
    return transcript.model_copy(update={"channels": (descriptor,) + transcript.channels[1:]})


def test_endpoint_pair_rejects_identity_claim_and_adjustment_mismatches() -> None:
    transcript, terms = _transcript()
    second = terms[1]
    peer_mutation = second.model_copy(update={"peer_node_key": _node(30)})
    peer_descriptor = transcript.channels[0].model_copy(
        update={"second_terms_hash": endpoint_terms_hash(peer_mutation).hex()}
    )
    with pytest.raises(ChannelInputValidationError, match="peer identities"):
        validate_revealed_endpoint_pair(
            _transcript_with_descriptor(transcript, peer_descriptor),
            peer_descriptor,
            terms[0],
            peer_mutation,
        )
    claims_mutation = second.model_copy(
        update={"frozen_local_claim_sat": 49_999, "frozen_remote_claim_sat": 50_001}
    )
    claims_descriptor = transcript.channels[0].model_copy(
        update={"second_terms_hash": endpoint_terms_hash(claims_mutation).hex()}
    )
    with pytest.raises(ChannelInputValidationError, match="frozen claims"):
        validate_revealed_endpoint_pair(
            _transcript_with_descriptor(transcript, claims_descriptor),
            claims_descriptor,
            terms[0],
            claims_mutation,
        )
    changed_first = _terms(1, 2, role="first", funding_index=0, output_index=0, adjustment=1)
    changed_second = _terms(2, 1, role="second", funding_index=0, output_index=1)
    descriptor = ChannelDescriptor(
        funding_outpoint=changed_first.funding_outpoint,
        input_index=0,
        capacity_sat=100_000,
        first_participant_key=_key(1),
        second_participant_key=_key(2),
        first_terms_hash=endpoint_terms_hash(changed_first).hex(),
        second_terms_hash=endpoint_terms_hash(changed_second).hex(),
    )
    with pytest.raises(ChannelInputValidationError, match="adjustments"):
        validate_revealed_endpoint_pair(
            _transcript_with_descriptor(transcript, descriptor),
            descriptor,
            changed_first,
            changed_second,
        )
    substituted_descriptor = transcript.channels[0].model_copy(
        update={"first_participant_key": _key(3)}
    )
    with pytest.raises(ChannelInputValidationError, match="first endpoint participant"):
        validate_revealed_endpoint_pair(
            _transcript_with_descriptor(transcript, substituted_descriptor),
            substituted_descriptor,
            terms[0],
            terms[1],
        )


@pytest.mark.parametrize(
    "mutation,match",
    [("range", "outside parent"), ("amount", "does not match"), ("script", "does not match")],
)
def test_endpoint_pair_binds_each_settlement_output_to_parent(mutation: str, match: str) -> None:
    transcript, terms = _transcript()
    output = terms[0].settlement_outputs[0]
    if mutation == "range":
        replacement = output.model_copy(update={"output_index": 4})
    elif mutation == "amount":
        replacement = output.model_copy(update={"amount_sat": 48_999})
    else:
        replacement = output.model_copy(update={"script_pubkey": "5120" + "99" * 32})
    first = terms[0].model_copy(update={"settlement_outputs": (replacement,)})
    descriptor = transcript.channels[0].model_copy(
        update={"first_terms_hash": endpoint_terms_hash(first).hex()}
    )
    with pytest.raises(ChannelInputValidationError, match=match):
        validate_revealed_endpoint_pair(
            _transcript_with_descriptor(transcript, descriptor), descriptor, first, terms[1]
        )


def test_complete_approval_set_rejects_duplicate_and_missing_coverage() -> None:
    transcript, _ = _transcript()
    approval_set = _approval_set(transcript)
    validate_complete_approval_set(approval_set)
    duplicate = approval_set.approvals[:-1] + (approval_set.approvals[0],)
    malformed = sign_payload(
        approval_set.model_copy(update={"approvals": duplicate}),
        _secret(1),
        aux_randomness=b"\x08" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="roster participant"):
        validate_complete_approval_set(malformed)
    missing_term = sign_payload(
        approval_set.approvals[0].model_copy(update={"terms_hashes": ("00" * 32,)}),
        _secret(1),
        aux_randomness=b"\x09" * 32,
    )
    malformed = sign_payload(
        approval_set.model_copy(update={"approvals": (missing_term,) + approval_set.approvals[1:]}),
        _secret(1),
        aux_randomness=b"\x0a" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="terms hash"):
        validate_complete_approval_set(malformed)


def test_complete_approval_set_rejects_terms_hash_permutation() -> None:
    transcript, _ = _transcript()
    approval_set = _approval_set(transcript)
    first, second, *remaining = approval_set.approvals
    swapped_first = sign_payload(
        first.model_copy(update={"terms_hashes": second.terms_hashes}),
        _secret(1),
        aux_randomness=b"\x1b" * 32,
    )
    swapped_second = sign_payload(
        second.model_copy(update={"terms_hashes": first.terms_hashes}),
        _secret(2),
        aux_randomness=b"\x1c" * 32,
    )
    swapped_set = sign_payload(
        approval_set.model_copy(update={"approvals": (swapped_first, swapped_second, *remaining)}),
        _secret(1),
        aux_randomness=b"\x1d" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="descriptor ownership"):
        validate_complete_approval_set(swapped_set)


def test_complete_approval_set_allows_multiple_terms_for_one_participant() -> None:
    transcript, _ = _transcript()
    second_channel = transcript.channels[1].model_copy(
        update={"first_participant_key": _key(1), "second_participant_key": _key(4)}
    )
    reassigned = transcript.model_copy(
        update={"channels": (transcript.channels[0], second_channel)}
    )
    approval_set = _approval_set(reassigned)
    assert len(approval_set.approvals[0].terms_hashes) == 2
    validate_complete_approval_set(approval_set)


def test_recipient_approval_requires_exact_local_terms_and_approval() -> None:
    transcript, terms = _transcript()
    approval_set = _approval_set(transcript)
    validate_recipient_approval(approval_set, _key(2), (terms[1],))
    with pytest.raises(ChannelInputValidationError, match="supplied local terms"):
        validate_recipient_approval(approval_set, _key(2), (terms[2],))
    omitted = sign_payload(
        approval_set.model_copy(update={"approvals": approval_set.approvals[:-1]}),
        _secret(1),
        aux_randomness=b"\x1e" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="roster participant"):
        validate_recipient_approval(omitted, _key(2), (terms[1],))


def test_approval_set_requires_transcript_coordinator_signature() -> None:
    transcript, _ = _transcript()
    approval_set = _approval_set(transcript)
    substituted = sign_payload(
        approval_set.model_copy(update={"signer_key": _key(2)}),
        _secret(2),
        aux_randomness=b"\x1f" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="transcript coordinator"):
        validate_complete_approval_set(substituted)


def test_prepare_requires_exact_ack_from_every_roster_member() -> None:
    approval_set = _approval_set(_transcript()[0])
    prepare = _prepare(approval_set)
    validate_prepare_payload(prepare)
    omitted = _resign_prepare(
        prepare.model_copy(update={"approval_set_acks": prepare.approval_set_acks[:-1]})
    )
    with pytest.raises(ChannelInputValidationError, match="one approval-set ACK"):
        validate_prepare_payload(omitted)
    duplicate = _resign_prepare(
        prepare.model_copy(
            update={
                "approval_set_acks": prepare.approval_set_acks[:-1]
                + (prepare.approval_set_acks[0],)
            }
        ),
        aux=22,
    )
    with pytest.raises(ChannelInputValidationError, match="one approval-set ACK"):
        validate_prepare_payload(duplicate)


def test_prepare_rejects_invalid_or_replayed_ack_bindings() -> None:
    approval_set = _approval_set(_transcript()[0])
    prepare = _prepare(approval_set)
    invalid_signature = _resign_prepare(
        prepare.model_copy(
            update={
                "approval_set_acks": (
                    prepare.approval_set_acks[0].model_copy(update={"sig": "00" * 64}),
                )
                + prepare.approval_set_acks[1:]
            }
        )
    )
    with pytest.raises(ChannelInputValidationError, match="ACK BIP340"):
        validate_prepare_payload(invalid_signature)
    wrong_transcript_ack = sign_payload(
        prepare.approval_set_acks[0].model_copy(update={"transcript_hash": "00" * 32}),
        _secret(1),
        aux_randomness=b"\x16" * 32,
    )
    wrong_transcript = _resign_prepare(
        prepare.model_copy(
            update={"approval_set_acks": (wrong_transcript_ack,) + prepare.approval_set_acks[1:]}
        ),
        aux=23,
    )
    with pytest.raises(ChannelInputValidationError, match="does not bind"):
        validate_prepare_payload(wrong_transcript)
    wrong_hash_ack = sign_payload(
        prepare.approval_set_acks[0].model_copy(update={"approval_set_hash": "00" * 32}),
        _secret(1),
        aux_randomness=b"\x17" * 32,
    )
    wrong_hash = _resign_prepare(
        prepare.model_copy(
            update={"approval_set_acks": (wrong_hash_ack,) + prepare.approval_set_acks[1:]}
        ),
        aux=24,
    )
    with pytest.raises(ChannelInputValidationError, match="does not bind"):
        validate_prepare_payload(wrong_hash)
    replayed_ack = sign_payload(
        prepare.approval_set_acks[0].model_copy(update={"attempt": 4}),
        _secret(1),
        aux_randomness=b"\x18" * 32,
    )
    replayed = _resign_prepare(
        prepare.model_copy(
            update={"approval_set_acks": (replayed_ack,) + prepare.approval_set_acks[1:]}
        ),
        aux=25,
    )
    with pytest.raises(ChannelInputValidationError, match="does not bind"):
        validate_prepare_payload(replayed)


def test_prepare_rejects_coordinator_substitution_and_wrong_set_hash() -> None:
    approval_set = _approval_set(_transcript()[0])
    prepare = _prepare(approval_set)
    substituted = sign_payload(
        prepare.model_copy(update={"signer_key": _key(2)}),
        _secret(2),
        aux_randomness=b"\x19" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="coordinator"):
        validate_prepare_payload(substituted)
    wrong_hash = _resign_prepare(
        prepare.model_copy(update={"approval_set_hash": "00" * 32}), aux=26
    )
    with pytest.raises(ChannelInputValidationError, match="exact approval set"):
        validate_prepare_payload(wrong_hash)


def test_codec_rejects_malformed_noncanonical_and_bounded_data() -> None:
    invite = _signed_invite()
    hello = _signed_hello()
    assert decode_channel_input_message(encode_channel_input_message(invite)) == invite
    assert decode_channel_input_message(encode_channel_input_message(hello)) == hello
    body = canonical_json(invite).decode()
    duplicate = body[:-1] + ',"v":1}'
    encoded = base64.urlsafe_b64encode(duplicate.encode()).rstrip(b"=").decode()
    with pytest.raises(ChannelInputValidationError, match="duplicate JSON key"):
        decode_channel_input_message(f"!channel_input channel_input_invite {encoded}")
    reordered = json.dumps(dict(reversed(list(json.loads(body).items()))), separators=(",", ":"))
    encoded = base64.urlsafe_b64encode(reordered.encode()).rstrip(b"=").decode()
    with pytest.raises(ChannelInputValidationError, match="not canonical"):
        decode_channel_input_message(f"!channel_input channel_input_invite {encoded}")
    for malformed, match in (
        ("!channel_input unknown e30", "unknown channel input message type"),
        ("!channel_input channel_input_invite e30=", "unpadded base64url"),
        ("!channel_input channel_input_invite !!!", "unpadded base64url"),
    ):
        with pytest.raises(ChannelInputValidationError, match=match):
            decode_channel_input_message(malformed)
    deep = base64.urlsafe_b64encode(b"[" * 13 + b"]" * 13).rstrip(b"=").decode()
    with pytest.raises(ChannelInputValidationError, match="maximum depth"):
        decode_channel_input_message(f"!channel_input channel_input_invite {deep}")
    oversized = "a" * (4 * MAX_CHANNEL_INPUT_PAYLOAD_BYTES // 3 + 129)
    with pytest.raises(ChannelInputValidationError, match="maximum encoded size"):
        decode_channel_input_message(f"!channel_input channel_input_invite {oversized}")


def test_cancel_ack_round_trips_and_binds_exact_signed_cancel() -> None:
    transcript, _ = _transcript()
    cancel = _signed_cancel(transcript)
    outpoints = tuple(channel.funding_outpoint for channel in transcript.channels)
    ack = _signed_cancel_ack(cancel, outpoints)

    validate_cancel_ack(cancel, ack, _key(2), outpoints)
    assert decode_channel_input_message(encode_channel_input_message(ack)) == ack

    differently_signed_cancel = sign_payload(cancel, _secret(1), aux_randomness=b"\x52" * 32)
    assert cancel_payload_hash(differently_signed_cancel) != cancel_payload_hash(cancel)
    with pytest.raises(ChannelInputValidationError, match="exact signed cancel"):
        validate_cancel_ack(differently_signed_cancel, ack, _key(2), outpoints)


def test_cancellation_binding_uses_invite_before_a_transcript_exists() -> None:
    invite = _signed_invite()
    transcript, _ = _transcript()

    assert cancellation_binding_hash(invite) == payload_hash(invite)
    assert cancellation_binding_hash(invite, transcript) == transcript_hash(transcript)


def test_cancel_ack_rejects_invalid_bindings_and_outpoint_coverage() -> None:
    transcript, _ = _transcript()
    cancel = _signed_cancel(transcript)
    outpoints = tuple(channel.funding_outpoint for channel in transcript.channels)
    ack = _signed_cancel_ack(cancel, outpoints)

    with pytest.raises(ChannelInputValidationError, match="cancel BIP340"):
        validate_cancel_ack(cancel.model_copy(update={"sig": "00" * 64}), ack, _key(2), outpoints)
    with pytest.raises(ChannelInputValidationError, match="cancel ACK BIP340"):
        validate_cancel_ack(cancel, ack.model_copy(update={"sig": "00" * 64}), _key(2), outpoints)

    substitutions = (
        (
            _signed_cancel_ack(cancel, outpoints, signer=3, aux=53),
            _key(2),
            outpoints,
            "expected signer",
        ),
        (
            sign_payload(
                ack.model_copy(update={"session_id": "22" * 32}),
                _secret(2),
                aux_randomness=b"\x53" * 32,
            ),
            _key(2),
            outpoints,
            "session, attempt, or transcript",
        ),
        (
            sign_payload(
                ack.model_copy(update={"attempt": cancel.attempt + 1}),
                _secret(2),
                aux_randomness=b"\x54" * 32,
            ),
            _key(2),
            outpoints,
            "session, attempt, or transcript",
        ),
        (
            sign_payload(
                ack.model_copy(update={"transcript_hash": "00" * 32}),
                _secret(2),
                aux_randomness=b"\x55" * 32,
            ),
            _key(2),
            outpoints,
            "session, attempt, or transcript",
        ),
        (
            sign_payload(
                ack.model_copy(update={"cancel_hash": "00" * 32}),
                _secret(2),
                aux_randomness=b"\x56" * 32,
            ),
            _key(2),
            outpoints,
            "exact signed cancel",
        ),
        (
            _signed_cancel_ack(cancel, outpoints[:-1], aux=57),
            _key(2),
            outpoints,
            "do not exactly match",
        ),
    )
    for substituted_ack, expected_signer, expected_outpoints, match in substitutions:
        with pytest.raises(ChannelInputValidationError, match=match):
            validate_cancel_ack(cancel, substituted_ack, expected_signer, expected_outpoints)

    with pytest.raises(ValidationError, match="cancel_hash"):
        ChannelInputCancelAckPayload(
            session_id=cancel.session_id,
            attempt=cancel.attempt,
            signer_key=_key(2),
            transcript_hash=cancel.transcript_hash,
            cancel_hash="00" * 31,
            resumed_outpoints=outpoints,
        )
    with pytest.raises(ValidationError, match="transcript_hash"):
        ChannelInputCancelAckPayload(
            session_id=cancel.session_id,
            attempt=cancel.attempt,
            signer_key=_key(2),
            transcript_hash="00" * 31,
            cancel_hash=cancel_payload_hash(cancel).hex(),
            resumed_outpoints=outpoints,
        )
    with pytest.raises(ValidationError, match="duplicate resumed outpoint"):
        ChannelInputCancelAckPayload(
            session_id=cancel.session_id,
            attempt=cancel.attempt,
            signer_key=_key(2),
            transcript_hash=cancel.transcript_hash,
            cancel_hash=cancel_payload_hash(cancel).hex(),
            resumed_outpoints=(outpoints[0], outpoints[0]),
        )
    with pytest.raises(ValidationError, match="stable lexicographic order"):
        ChannelInputCancelAckPayload(
            session_id=cancel.session_id,
            attempt=cancel.attempt,
            signer_key=_key(2),
            transcript_hash=cancel.transcript_hash,
            cancel_hash=cancel_payload_hash(cancel).hex(),
            resumed_outpoints=tuple(reversed(outpoints)),
        )


def test_channel_messages_cancel_and_finalized_witness_binding() -> None:
    transcript, _ = _transcript()
    transcript_digest = transcript_hash(transcript).hex()
    prepared = sign_payload(
        ChannelInputPreparedPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(1),
            transcript_hash=transcript_digest,
            funding_outpoint=transcript.channels[0].funding_outpoint,
            public_nonce="aa" * 66,
        ),
        _secret(1),
        aux_randomness=b"\x1a" * 32,
    )
    validate_channel_payload_for_transcript(prepared, transcript)
    with pytest.raises(ValidationError, match="66-byte"):
        ChannelInputPreparedPayload(**{**prepared.model_dump(), "public_nonce": "aa" * 65})
    with pytest.raises(ValidationError, match="32-byte"):
        ChannelInputPartialPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(1),
            transcript_hash=transcript_digest,
            funding_outpoint=transcript.channels[0].funding_outpoint,
            local_public_nonce="aa" * 66,
            peer_public_nonce="bb" * 66,
            partial_signature="bb" * 31,
        )
    with pytest.raises(ValidationError, match="66-byte"):
        ChannelInputPartialPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(1),
            transcript_hash=transcript_digest,
            funding_outpoint=transcript.channels[0].funding_outpoint,
            local_public_nonce="aa" * 65,
            peer_public_nonce="bb" * 66,
            partial_signature="bb" * 32,
        )
    with pytest.raises(ValidationError):
        ChannelInputCancelPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(1),
            transcript_hash=transcript_digest,
            reason_code="after_sign",
            partial_released=True,  # type: ignore[arg-type]
        )
    unsigned = parse_transaction_bytes(bytes.fromhex(transcript.unsigned_parent_tx))
    channel_signed_parent = serialize_transaction(
        unsigned.version,
        unsigned.inputs,
        unsigned.outputs,
        unsigned.locktime,
        [[], [b"\x01" * 64]],
    )
    finalized = sign_payload(
        ChannelInputFinalizedPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=_key(3),
            transcript_hash=transcript_digest,
            funding_outpoint=transcript.channels[1].funding_outpoint,
            channel_signed_parent_tx=channel_signed_parent.hex(),
            txid=hash256(bytes.fromhex(transcript.unsigned_parent_tx))[::-1].hex(),
        ),
        _secret(3),
        aux_randomness=b"\x40" * 32,
    )
    assert verify_payload(finalized)
    validate_finalized_for_transcript(finalized, transcript)
    validate_channel_payload_for_transcript(finalized, transcript)
    assert decode_channel_input_message(encode_channel_input_message(finalized)) == finalized

    with pytest.raises(ValidationError, match="witness vector"):
        ChannelInputFinalizedPayload(
            **{
                **finalized.model_dump(),
                "channel_signed_parent_tx": transcript.unsigned_parent_tx,
            }
        )
    with pytest.raises(ValidationError, match="exactly one channel witness"):
        ChannelInputFinalizedPayload(
            **{
                **finalized.model_dump(),
                "channel_signed_parent_tx": serialize_transaction(
                    unsigned.version,
                    unsigned.inputs,
                    unsigned.outputs,
                    unsigned.locktime,
                    [[b"\x02" * 64], [b"\x01" * 64]],
                ).hex(),
            }
        )
    with pytest.raises(ValidationError, match="one 64-byte Taproot signature"):
        ChannelInputFinalizedPayload(
            **{
                **finalized.model_dump(),
                "channel_signed_parent_tx": serialize_transaction(
                    unsigned.version,
                    unsigned.inputs,
                    unsigned.outputs,
                    unsigned.locktime,
                    [[], [b"\x01" * 32, b"\x02" * 32]],
                ).hex(),
            }
        )
    with pytest.raises(ValidationError, match="one 64-byte Taproot signature"):
        ChannelInputFinalizedPayload(
            **{
                **finalized.model_dump(),
                "channel_signed_parent_tx": serialize_transaction(
                    unsigned.version,
                    unsigned.inputs,
                    unsigned.outputs,
                    unsigned.locktime,
                    [[], [b"\x01" * 63]],
                ).hex(),
            }
        )
    script_input = TxInput(
        txid_le=unsigned.inputs[0].txid_le,
        vout=unsigned.inputs[0].vout,
        scriptsig=b"\x01",
        sequence=unsigned.inputs[0].sequence,
    )
    script_raw = serialize_transaction(
        unsigned.version,
        [script_input, unsigned.inputs[1]],
        unsigned.outputs,
        unsigned.locktime,
        [[], [b"\x01" * 64]],
    )
    script_txid = hash256(
        serialize_transaction(
            unsigned.version,
            [script_input, unsigned.inputs[1]],
            unsigned.outputs,
            unsigned.locktime,
        )
    )[::-1].hex()
    with pytest.raises(ValidationError, match="scriptSig"):
        ChannelInputFinalizedPayload(
            **{
                **finalized.model_dump(),
                "channel_signed_parent_tx": script_raw.hex(),
                "txid": script_txid,
            }
        )

    with pytest.raises(ValidationError, match="canonical"):
        ChannelInputFinalizedPayload(
            **{
                **finalized.model_dump(),
                "channel_signed_parent_tx": channel_signed_parent.replace(
                    b"\x01\x40" + b"\x01" * 64,
                    b"\xfd\x01\x00\x40" + b"\x01" * 64,
                ).hex(),
            }
        )
    with pytest.raises(ValidationError, match="txid does not match"):
        ChannelInputFinalizedPayload(**{**finalized.model_dump(), "txid": "00" * 32})

    wrong_position = sign_payload(
        finalized.model_copy(
            update={
                "channel_signed_parent_tx": serialize_transaction(
                    unsigned.version,
                    unsigned.inputs,
                    unsigned.outputs,
                    unsigned.locktime,
                    [[b"\x01" * 64], []],
                ).hex()
            }
        ),
        _secret(3),
        aux_randomness=b"\x41" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="not at the funding outpoint"):
        validate_finalized_for_transcript(wrong_position, transcript)

    mutated_outputs = list(unsigned.outputs)
    mutated_outputs[0] = TxOutput(
        value=mutated_outputs[0].value - 1, script=mutated_outputs[0].script
    )
    mutated_unsigned = serialize_transaction(
        unsigned.version, unsigned.inputs, mutated_outputs, unsigned.locktime
    )
    mutated_parent = serialize_transaction(
        unsigned.version,
        unsigned.inputs,
        mutated_outputs,
        unsigned.locktime,
        [[], [b"\x01" * 64]],
    )
    mutated = sign_payload(
        finalized.model_copy(
            update={
                "channel_signed_parent_tx": mutated_parent.hex(),
                "txid": hash256(mutated_unsigned)[::-1].hex(),
            }
        ),
        _secret(3),
        aux_randomness=b"\x42" * 32,
    )
    with pytest.raises(
        ChannelInputValidationError, match="differs from transcript unsigned parent"
    ):
        validate_finalized_for_transcript(mutated, transcript)


def test_post_approval_validator_chain_and_nested_wire_evidence() -> None:
    transcript, first_prepared, second_prepared, sign, partial, finalize = _post_approval_payloads()
    first_nonce = first_prepared.public_nonce
    second_nonce = second_prepared.public_nonce

    validate_prepared_for_transcript(first_prepared, transcript, _key(1))
    validate_prepared_for_transcript(second_prepared, transcript, _key(2))
    validate_sign_for_transcript(
        sign,
        transcript,
        _key(1),
        expected_local_public_nonce=first_nonce,
    )
    validate_partial_for_transcript(
        partial,
        transcript,
        _key(2),
        expected_local_public_nonce=second_nonce,
        expected_peer_public_nonce=first_nonce,
    )
    validate_finalize_for_transcript(
        finalize,
        transcript,
        _key(1),
        expected_local_public_nonce=first_nonce,
        expected_peer_public_nonce=second_nonce,
    )
    assert decode_channel_input_message(encode_channel_input_message(finalize)) == finalize

    tampered_partial = partial.model_copy(update={"sig": "00" * 64})
    tampered_finalize = sign_payload(
        finalize.model_copy(update={"peer_partial": tampered_partial}),
        _secret(1),
        aux_randomness=b"\x36" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="channel payload BIP340"):
        validate_finalize_for_transcript(
            cast(ChannelInputFinalizePayload, tampered_finalize),
            transcript,
            _key(1),
            expected_local_public_nonce=first_nonce,
            expected_peer_public_nonce=second_nonce,
        )


def test_sign_binds_current_nonce_and_signed_peer_prepared_evidence() -> None:
    transcript, _, second_prepared, sign, _, _ = _post_approval_payloads()
    first_nonce = sign.local_public_nonce

    with pytest.raises(ChannelInputValidationError, match="recipient current nonce"):
        validate_sign_for_transcript(
            sign,
            transcript,
            _key(1),
            expected_local_public_nonce="cc" * 66,
        )
    malformed_prepared = second_prepared.model_copy(update={"attempt": sign.attempt + 1})
    with pytest.raises(ValidationError, match="peer prepared does not match"):
        ChannelInputSignPayload(**{**sign.model_dump(), "peer_prepared": malformed_prepared})
    wrong_peer = sign_payload(
        second_prepared.model_copy(update={"signer_key": _key(3)}),
        _secret(3),
        aux_randomness=b"\x37" * 32,
    )
    wrong_peer_sign = sign_payload(
        sign.model_copy(update={"peer_prepared": wrong_peer}),
        _secret(1),
        aux_randomness=b"\x38" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="prepared signer"):
        validate_sign_for_transcript(
            cast(ChannelInputSignPayload, wrong_peer_sign),
            transcript,
            _key(1),
            expected_local_public_nonce=first_nonce,
        )
    invalid_peer_sign = sign_payload(
        sign.model_copy(
            update={"peer_prepared": second_prepared.model_copy(update={"sig": "00" * 64})}
        ),
        _secret(1),
        aux_randomness=b"\x39" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="channel payload BIP340"):
        validate_sign_for_transcript(
            cast(ChannelInputSignPayload, invalid_peer_sign),
            transcript,
            _key(1),
            expected_local_public_nonce=first_nonce,
        )
    with pytest.raises(ValidationError, match="nonces must differ"):
        ChannelInputSignPayload(
            **{
                **sign.model_dump(),
                "peer_prepared": second_prepared.model_copy(update={"public_nonce": first_nonce}),
            }
        )
    non_coordinator = sign_payload(
        sign.model_copy(update={"signer_key": _key(3)}),
        _secret(3),
        aux_randomness=b"\x3a" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="sign signer"):
        validate_sign_for_transcript(
            cast(ChannelInputSignPayload, non_coordinator),
            transcript,
            _key(1),
            expected_local_public_nonce=first_nonce,
        )


def test_partial_and_finalize_validate_ordered_authenticated_nonce_evidence() -> None:
    transcript, _, second_prepared, _, partial, finalize = _post_approval_payloads()
    first_nonce = finalize.local_public_nonce
    second_nonce = second_prepared.public_nonce

    wrong_signer = sign_payload(
        partial.model_copy(update={"signer_key": _key(3)}),
        _secret(3),
        aux_randomness=b"\x3b" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="partial signer"):
        validate_partial_for_transcript(
            cast(ChannelInputPartialPayload, wrong_signer),
            transcript,
            _key(2),
            expected_local_public_nonce=second_nonce,
            expected_peer_public_nonce=first_nonce,
        )
    wrong_local_nonce = sign_payload(
        partial.model_copy(update={"local_public_nonce": "cc" * 66}),
        _secret(2),
        aux_randomness=b"\x3c" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="partial local nonce"):
        validate_partial_for_transcript(
            cast(ChannelInputPartialPayload, wrong_local_nonce),
            transcript,
            _key(2),
            expected_local_public_nonce=second_nonce,
            expected_peer_public_nonce=first_nonce,
        )
    wrong_peer_nonce = sign_payload(
        partial.model_copy(update={"peer_public_nonce": "cc" * 66}),
        _secret(2),
        aux_randomness=b"\x3d" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="partial peer nonce"):
        validate_partial_for_transcript(
            cast(ChannelInputPartialPayload, wrong_peer_nonce),
            transcript,
            _key(2),
            expected_local_public_nonce=second_nonce,
            expected_peer_public_nonce=first_nonce,
        )
    with pytest.raises(ChannelInputValidationError, match="recipient current nonce"):
        validate_finalize_for_transcript(
            finalize,
            transcript,
            _key(1),
            expected_local_public_nonce="cc" * 66,
            expected_peer_public_nonce=second_nonce,
        )
    wrong_peer_partial = sign_payload(
        partial.model_copy(update={"signer_key": _key(3)}),
        _secret(3),
        aux_randomness=b"\x3e" * 32,
    )
    wrong_peer_finalize = sign_payload(
        finalize.model_copy(update={"peer_partial": wrong_peer_partial}),
        _secret(1),
        aux_randomness=b"\x3f" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="partial signer"):
        validate_finalize_for_transcript(
            cast(ChannelInputFinalizePayload, wrong_peer_finalize),
            transcript,
            _key(1),
            expected_local_public_nonce=first_nonce,
            expected_peer_public_nonce=second_nonce,
        )
    invalid_peer_finalize = sign_payload(
        finalize.model_copy(update={"peer_partial": partial.model_copy(update={"sig": "00" * 64})}),
        _secret(1),
        aux_randomness=b"\x40" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="channel payload BIP340"):
        validate_finalize_for_transcript(
            cast(ChannelInputFinalizePayload, invalid_peer_finalize),
            transcript,
            _key(1),
            expected_local_public_nonce=first_nonce,
            expected_peer_public_nonce=second_nonce,
        )
    reversed_partial = partial.model_copy(
        update={"local_public_nonce": first_nonce, "peer_public_nonce": second_nonce}
    )
    with pytest.raises(ValidationError, match="orientation"):
        ChannelInputFinalizePayload(**{**finalize.model_dump(), "peer_partial": reversed_partial})
    mismatched_partial = partial.model_copy(update={"peer_public_nonce": "cc" * 66})
    with pytest.raises(ValidationError, match="orientation"):
        ChannelInputFinalizePayload(**{**finalize.model_dump(), "peer_partial": mismatched_partial})
    non_coordinator = sign_payload(
        finalize.model_copy(update={"signer_key": _key(3)}),
        _secret(3),
        aux_randomness=b"\x41" * 32,
    )
    with pytest.raises(ChannelInputValidationError, match="finalize signer"):
        validate_finalize_for_transcript(
            cast(ChannelInputFinalizePayload, non_coordinator),
            transcript,
            _key(1),
            expected_local_public_nonce=first_nonce,
            expected_peer_public_nonce=second_nonce,
        )
