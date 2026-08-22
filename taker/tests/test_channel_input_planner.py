from __future__ import annotations

from dataclasses import dataclass
from typing import cast

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import TxInput, TxOutput, pubkey_to_p2wpkh_script, serialize_transaction
from jmcore.channel_input import (
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputHelloPayload,
    ChannelInputKeyPair,
    ChannelInputPreparePayload,
    Outpoint,
    PolicyCeilings,
    approval_set_hash,
    sign_payload,
    transcript_hash,
)

from taker.channel_input_negotiation import TakerChannelInputNegotiation
from taker.channel_input_parent import (
    ChannelInputParentCoordinator,
    ChannelInputParentError,
    OrdinaryWitness,
)
from taker.channel_input_planner import (
    ChannelInputParentDraft,
    ChannelInputPlanningError,
    authorize_channel_input_parent,
    derive_channel_input_parent_draft,
    plan_channel_input_contributions,
)
from taker.tx_builder import CoinJoinTxBuilder

_SESSION_ID = "11" * 32
_COORDINATOR_SECRET = b"\x01" * 32
_MAKER_SECRET = b"\x02" * 32


def _node(secret: bytes) -> str:
    return PrivateKey(secret).public_key.format(compressed=True).hex()


def _p2tr(marker: int) -> str:
    return "5120" + f"{marker:02x}" * 32


def _hello(
    secret: bytes,
    participant_key: str,
    node_key: str,
    peer_node_key: str,
    local_claim: int,
    remote_claim: int,
    settlement_marker: int,
) -> ChannelInputHelloPayload:
    return sign_payload(
        ChannelInputHelloPayload(
            session_id=_SESSION_ID,
            attempt=3,
            signer_key=participant_key,
            participant_key=participant_key,
            node_key=node_key,
            peer_node_key=peer_node_key,
            funding_outpoint=Outpoint(txid="22" * 32, vout=4),
            capacity_sat=100_000,
            funding_script_pubkey=_p2tr(0xA1),
            frozen_local_claim_sat=local_claim,
            frozen_remote_claim_sat=remote_claim,
            settlement_script_pubkey=_p2tr(settlement_marker),
            quoted_miner_fee_share_sat=0,
            policy_ceilings=PolicyCeilings(
                max_miner_fee_share_sat=0,
                max_claim_adjustment_sat=2_000,
                max_settlement_outputs=1,
            ),
        ),
        secret,
        aux_randomness=secret,
    )


@dataclass(frozen=True)
class Fixture:
    negotiation: TakerChannelInputNegotiation
    contributions: tuple
    parent: bytes
    metadata: dict[str, object]


def _fixture() -> Fixture:
    negotiation = TakerChannelInputNegotiation.create(
        network="regtest",
        local_actor="taker",
        selected_nicks=("maker",),
        secret_key=_COORDINATOR_SECRET,
        session_id=_SESSION_ID,
        attempt=3,
        expiry=2_000,
        max_parent_fee_sat=2_000,
        max_fee_rate_sat_vb=100,
        min_recovery_delta=10,
        max_recovery_delta=50,
    )
    maker_key = ChannelInputKeyPair.from_secret(_MAKER_SECRET)
    remote = _hello(
        _MAKER_SECRET,
        maker_key.public_key,
        _node(b"\x03" * 32),
        _node(b"\x04" * 32),
        50_000,
        50_000,
        0xB1,
    )
    local = _hello(
        _COORDINATOR_SECRET,
        negotiation.coordinator_key,
        _node(b"\x04" * 32),
        _node(b"\x03" * 32),
        50_000,
        50_000,
        0xB2,
    )
    negotiation.accept_hello("maker", remote, local)
    contributions = plan_channel_input_contributions(negotiation, {"maker": 1_000})
    channel = contributions[0]
    ordinary_script = pubkey_to_p2wpkh_script(
        PrivateKey(b"\x05" * 32).public_key.format(compressed=True)
    )
    # The channel input and settlements deliberately occupy shuffled positions.
    parent = serialize_transaction(
        2,
        [
            TxInput.from_hex("33" * 32, 0),
            TxInput.from_hex(channel.funding_input.txid, channel.funding_input.vout),
        ],
        [
            TxOutput(value=99_500, script=ordinary_script),
            channel.second_endpoint.output,
            channel.first_endpoint.output,
        ],
        0,
        None,
    )
    metadata: dict[str, object] = {
        "input_values": [100_000, channel.funding_input.value],
        "input_scriptpubkeys": [ordinary_script.hex(), channel.funding_input.scriptpubkey.hex()],
        "channel_input_ids": [None, channel.channel_id],
        "channel_endpoint_outputs": [
            None,
            {
                "channel_id": channel.channel_id,
                "endpoint_id": channel.second_endpoint.endpoint_id,
                "participant_key": channel.second_endpoint.participant_key,
                "script_pubkey": channel.second_endpoint.output.scriptpubkey,
                "value": channel.second_endpoint.output.value,
            },
            {
                "channel_id": channel.channel_id,
                "endpoint_id": channel.first_endpoint.endpoint_id,
                "participant_key": channel.first_endpoint.participant_key,
                "script_pubkey": channel.first_endpoint.output.scriptpubkey,
                "value": channel.first_endpoint.output.value,
            },
        ],
        "actual_fee": 500,
    }
    return Fixture(negotiation, contributions, parent, metadata)


def _draft(fixture: Fixture) -> ChannelInputParentDraft:
    return derive_channel_input_parent_draft(
        fixture.negotiation,
        fixture.parent,
        fixture.metadata,
        fixture.contributions,
        current_height=100,
        maximum_miner_fee_sat=1_000,
    )


def _prepare(draft: ChannelInputParentDraft) -> ChannelInputPreparePayload:
    transcript = draft.transcript
    maker_key = next(
        key for key in transcript.participant_keys if key != transcript.coordinator_key
    )
    keys = (
        (transcript.coordinator_key, _COORDINATOR_SECRET),
        (maker_key, _MAKER_SECRET),
    )
    approvals = tuple(
        sign_payload(
            ChannelInputApprovalPayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=key,
                transcript_hash=transcript_hash(transcript).hex(),
                terms_hashes=tuple(sorted(transcript.terms_hashes_for(key))),
            ),
            secret,
            aux_randomness=secret,
        )
        for key, secret in keys
    )
    approval_set = sign_payload(
        ChannelInputApprovalSetPayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=transcript.coordinator_key,
            transcript=transcript,
            approvals=approvals,
        ),
        _COORDINATOR_SECRET,
        aux_randomness=b"\x06" * 32,
    )
    acks = tuple(
        sign_payload(
            ChannelInputApprovalSetAckPayload(
                session_id=transcript.session_id,
                attempt=transcript.attempt,
                signer_key=key,
                transcript_hash=transcript_hash(transcript).hex(),
                approval_set_hash=approval_set_hash(approval_set).hex(),
            ),
            secret,
            aux_randomness=bytes([index]) * 32,
        )
        for index, (key, secret) in enumerate(keys, start=7)
    )
    return sign_payload(
        ChannelInputPreparePayload(
            session_id=transcript.session_id,
            attempt=transcript.attempt,
            signer_key=transcript.coordinator_key,
            transcript_hash=transcript_hash(transcript).hex(),
            approval_set_hash=approval_set_hash(approval_set).hex(),
            approval_set=approval_set,
            approval_set_acks=acks,
        ),
        _COORDINATOR_SECRET,
        aux_randomness=b"\x09" * 32,
    )


def test_prebuild_fee_is_exact_zero_sum_channel_adjustment() -> None:
    fixture = _fixture()
    contribution = fixture.contributions[0]

    assert contribution.first_endpoint.output.value == 51_000
    assert contribution.second_endpoint.output.value == 49_000
    assert (
        contribution.first_endpoint.output.value + contribution.second_endpoint.output.value
        == contribution.funding_input.value
    )
    assert contribution.funding_input.value == 100_000


@pytest.mark.parametrize("fees", ({}, {"other": 1_000}, {"maker": 2_001}))
def test_prebuild_rejects_roster_and_policy_failures(fees: dict[str, int]) -> None:
    fixture = _fixture()

    with pytest.raises(ChannelInputPlanningError):
        plan_channel_input_contributions(fixture.negotiation, fees)


def test_prebuild_rejects_mirrored_hello_failure() -> None:
    fixture = _fixture()
    nick, local = fixture.negotiation.local_hellos[0]
    fixture.negotiation.local_hellos = ((nick, local.model_copy(update={"capacity_sat": 99_999})),)

    with pytest.raises(ChannelInputPlanningError, match="mirror"):
        plan_channel_input_contributions(fixture.negotiation, {"maker": 1_000})


def test_postbuild_derives_shuffled_indexes_exact_bytes_and_fee_rate() -> None:
    fixture = _fixture()
    draft = _draft(fixture)

    assert draft.unsigned_parent == fixture.parent
    assert draft.channel_input_indexes == (1,)
    assert draft.ordinary_input_indexes == (0,)
    assert [item.value_sat for item in draft.prevouts] == [100_000, 100_000]
    assert draft.channels[0].first.settlement_outputs[0].output_index == 2
    assert draft.channels[0].second.settlement_outputs[0].output_index == 1
    assert draft.transcript.input_prevouts[1].outpoint.txid == "22" * 32
    assert draft.transcript.total_fee_sat == 500
    assert draft.transcript.fee_rate_sat_vb > 0
    assert draft.transcript.expiry == fixture.negotiation.invite.expiry
    assert draft.transcript.recovery_height == 130


def test_postbuild_never_rebuilds_the_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _fixture()

    def fail_rebuild(*_args: object, **_kwargs: object) -> tuple[bytes, dict[str, object]]:
        raise AssertionError("planner must not rebuild the parent")

    monkeypatch.setattr(CoinJoinTxBuilder, "build_unsigned_tx", fail_rebuild)

    assert _draft(fixture).unsigned_parent == fixture.parent


@pytest.mark.parametrize("field", ("input_values", "channel_input_ids", "channel_endpoint_outputs"))
def test_postbuild_rejects_truncated_metadata(field: str) -> None:
    fixture = _fixture()
    metadata = dict(fixture.metadata)
    metadata[field] = list(cast(list[object], metadata[field]))[:-1]

    with pytest.raises(ChannelInputPlanningError):
        derive_channel_input_parent_draft(
            fixture.negotiation, fixture.parent, metadata, fixture.contributions, 100, 1_000
        )


def test_postbuild_rejects_metadata_substitution_and_recovery_overflow() -> None:
    fixture = _fixture()
    metadata = dict(fixture.metadata)
    endpoints = list(cast(list[dict[str, object] | None], metadata["channel_endpoint_outputs"]))
    endpoints[1] = {**cast(dict[str, object], endpoints[1]), "value": 49_001}
    metadata["channel_endpoint_outputs"] = endpoints

    with pytest.raises(ChannelInputPlanningError):
        derive_channel_input_parent_draft(
            fixture.negotiation, fixture.parent, metadata, fixture.contributions, 100, 1_000
        )
    with pytest.raises(ChannelInputPlanningError, match="overflows"):
        derive_channel_input_parent_draft(
            fixture.negotiation,
            fixture.parent,
            fixture.metadata,
            fixture.contributions,
            0xFFFFFFFF,
            1_000,
        )


def test_authorization_requires_exact_prepare_and_ordinary_coverage() -> None:
    fixture = _fixture()
    draft = _draft(fixture)
    prepare = _prepare(draft)
    witness = OrdinaryWitness(input_index=0, witness=("00",))

    with pytest.raises(ChannelInputPlanningError):
        authorize_channel_input_parent(draft, prepare, ())
    with pytest.raises(ChannelInputPlanningError):
        authorize_channel_input_parent(draft, prepare, (witness, witness))
    with pytest.raises(ChannelInputPlanningError):
        authorize_channel_input_parent(
            draft, prepare, (OrdinaryWitness(input_index=1, witness=("00",)),)
        )

    other = _fixture()
    other_draft = derive_channel_input_parent_draft(
        other.negotiation,
        other.parent,
        other.metadata,
        other.contributions,
        current_height=101,
        maximum_miner_fee_sat=1_000,
    )
    with pytest.raises(ChannelInputPlanningError, match="prepare transcript"):
        authorize_channel_input_parent(draft, _prepare(other_draft), (witness,))


def test_authorization_returns_plan_accepted_by_parent_coordinator() -> None:
    draft = _draft(_fixture())
    plan = authorize_channel_input_parent(
        draft,
        _prepare(draft),
        (OrdinaryWitness(input_index=0, witness=("00",)),),
    )

    ChannelInputParentCoordinator._validate_plan(plan)


def test_authorization_wraps_parent_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _draft(_fixture())

    def reject_parent(_plan: object) -> None:
        raise ChannelInputParentError("invalid parent")

    monkeypatch.setattr(
        ChannelInputParentCoordinator,
        "_validate_plan",
        reject_parent,
    )

    with pytest.raises(ChannelInputPlanningError, match="invalid parent"):
        authorize_channel_input_parent(
            draft,
            _prepare(draft),
            (OrdinaryWitness(input_index=0, witness=("00",)),),
        )
