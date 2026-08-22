"""Pure derivation of immutable channel-input parent commitments."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    estimate_vsize,
    parse_transaction_bytes,
)
from jmcore.channel_input import (
    ChannelDescriptor,
    ChannelInputHelloPayload,
    ChannelInputPreparePayload,
    ChannelInputTranscript,
    EndpointTerms,
    InputPrevout,
    Outpoint,
    endpoint_terms_hash,
)
from jmcore.channel_input import (
    SettlementOutput as AuthorizedSettlementOutput,
)

from taker.channel_input_negotiation import TakerChannelInputNegotiation, TakerChannelInputStage
from taker.channel_input_parent import (
    MAX_PARENT_FEE_SAT,
    ChannelContribution,
    ChannelInputAuthorization,
    ChannelInputParentCoordinator,
    ChannelInputParentError,
    ChannelInputParentPlan,
    EndpointContribution,
    OrdinaryWitness,
    ParentPrevout,
    SettlementOutput,
)
from taker.tx_builder import ChannelEndpointSettlement, ChannelTransactionContribution


class ChannelInputPlanningError(Exception):
    """The final CoinJoin parent cannot safely become a channel-input plan."""


@dataclass(frozen=True, slots=True)
class ChannelInputParentDraft:
    """Exact final parent commitments awaiting durable prepare authorization."""

    unsigned_parent: bytes
    prevouts: tuple[ParentPrevout, ...]
    channels: tuple[ChannelContribution, ...]
    transcript: ChannelInputTranscript
    endpoint_terms: tuple[EndpointTerms, ...]
    maximum_miner_fee_sat: int

    @property
    def channel_input_indexes(self) -> tuple[int, ...]:
        return tuple(channel.channel_input_index for channel in self.channels)

    @property
    def ordinary_input_indexes(self) -> tuple[int, ...]:
        channel_indexes = frozenset(self.channel_input_indexes)
        return tuple(index for index in range(len(self.prevouts)) if index not in channel_indexes)

    @property
    def unsigned_parent_bytes(self) -> bytes:
        return self.unsigned_parent


def plan_channel_input_contributions(
    negotiation: TakerChannelInputNegotiation, maker_fees_sat: Mapping[str, int]
) -> tuple[ChannelTransactionContribution, ...]:
    """Create stable pre-shuffle channel contributions from accepted hello pairs."""

    try:
        _require_hellos_ready(negotiation)
        remotes = _exact_hellos(negotiation.remote_hellos, negotiation.selected_nicks, "remote")
        locals_ = _exact_hellos(negotiation.local_hellos, negotiation.selected_nicks, "local")
        _require_exact_fee_roster(maker_fees_sat, negotiation.selected_nicks)

        contributions: list[ChannelTransactionContribution] = []
        for nick in negotiation.selected_nicks:
            maker = remotes[nick]
            coordinator = locals_[nick]
            fee_sat = maker_fees_sat[nick]
            _validate_hello_mirror(negotiation, maker, coordinator)
            _validate_claim_adjustment(maker, coordinator, fee_sat)

            channel_id = _channel_id(maker.funding_outpoint)
            contributions.append(
                ChannelTransactionContribution(
                    channel_id=channel_id,
                    funding_input=TxInput.from_hex(
                        maker.funding_outpoint.txid,
                        maker.funding_outpoint.vout,
                        value=maker.capacity_sat,
                        scriptpubkey=maker.funding_script_pubkey,
                    ),
                    first_endpoint=ChannelEndpointSettlement(
                        endpoint_id=_endpoint_id(maker.funding_outpoint, "maker"),
                        participant_key=maker.participant_key,
                        output=TxOutput(
                            value=maker.frozen_local_claim_sat + fee_sat,
                            script=bytes.fromhex(maker.settlement_script_pubkey),
                        ),
                    ),
                    second_endpoint=ChannelEndpointSettlement(
                        endpoint_id=_endpoint_id(maker.funding_outpoint, "coordinator"),
                        participant_key=coordinator.participant_key,
                        output=TxOutput(
                            value=coordinator.frozen_local_claim_sat - fee_sat,
                            script=bytes.fromhex(coordinator.settlement_script_pubkey),
                        ),
                    ),
                )
            )
        return tuple(contributions)
    except (TypeError, ValueError) as exc:
        raise ChannelInputPlanningError(str(exc)) from exc


def derive_channel_input_parent_draft(
    negotiation: TakerChannelInputNegotiation,
    unsigned_parent: bytes,
    metadata: Mapping[str, object],
    contributions: tuple[ChannelTransactionContribution, ...],
    current_height: int,
    maximum_miner_fee_sat: int,
) -> ChannelInputParentDraft:
    """Derive commitments from one already-built parent without rebuilding it."""

    try:
        _require_hellos_ready(negotiation)
        _validate_current_height(current_height)
        _validate_maximum_fee(negotiation, maximum_miner_fee_sat)
        expected = plan_channel_input_contributions(
            negotiation, _fees_from_contributions(negotiation, contributions)
        )
        if contributions != expected:
            raise ValueError("pre-build channel contributions differ from accepted hellos")
        if not isinstance(unsigned_parent, bytes) or not unsigned_parent:
            raise ValueError("unsigned parent must be nonempty bytes")
        transaction = parse_transaction_bytes(unsigned_parent)
        if transaction.has_witness or any(item.scriptsig for item in transaction.inputs):
            raise ValueError("parent must be unsigned without scriptsigs or witnesses")

        input_values = _metadata_sequence(metadata, "input_values", len(transaction.inputs))
        input_scripts = _metadata_sequence(metadata, "input_scriptpubkeys", len(transaction.inputs))
        channel_input_ids = _metadata_sequence(
            metadata, "channel_input_ids", len(transaction.inputs)
        )
        endpoint_outputs = _metadata_sequence(
            metadata, "channel_endpoint_outputs", len(transaction.outputs)
        )
        prevouts = _derive_prevouts(transaction.inputs, input_values, input_scripts)
        channels, endpoint_terms = _derive_channels(
            negotiation,
            transaction,
            prevouts,
            contributions,
            channel_input_ids,
            endpoint_outputs,
        )
        actual_fee = _actual_fee(metadata, prevouts, transaction.outputs, maximum_miner_fee_sat)
        transcript = _build_transcript(
            negotiation,
            unsigned_parent,
            prevouts,
            channels,
            endpoint_terms,
            actual_fee,
            current_height,
        )
        return ChannelInputParentDraft(
            unsigned_parent=unsigned_parent,
            prevouts=tuple(
                ParentPrevout(value_sat=item.value_sat, script_pubkey=item.script_pubkey)
                for item in prevouts
            ),
            channels=channels,
            transcript=transcript,
            endpoint_terms=endpoint_terms,
            maximum_miner_fee_sat=maximum_miner_fee_sat,
        )
    except ChannelInputPlanningError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise ChannelInputPlanningError(str(exc)) from exc


def authorize_channel_input_parent(
    draft: ChannelInputParentDraft,
    prepare: ChannelInputPreparePayload,
    ordinary_witnesses: Sequence[OrdinaryWitness],
) -> ChannelInputParentPlan:
    """Attach one exact durable prepare authorization and ordinary witnesses."""

    try:
        if not isinstance(draft, ChannelInputParentDraft):
            raise ValueError("draft has an invalid type")
        if prepare.approval_set.transcript != draft.transcript:
            raise ValueError("prepare transcript differs from parent draft")
        witnesses = tuple(ordinary_witnesses)
        witness_indexes = tuple(witness.input_index for witness in witnesses)
        if set(witness_indexes) != set(draft.ordinary_input_indexes) or len(
            set(witness_indexes)
        ) != len(witnesses):
            raise ValueError("ordinary witnesses do not exactly cover ordinary parent inputs")
        authorization = ChannelInputAuthorization(
            prepare=prepare,
            endpoint_terms=draft.endpoint_terms,
        )
        plan = ChannelInputParentPlan(
            session_id=draft.transcript.session_id,
            unsigned_parent_hex=draft.unsigned_parent.hex(),
            prevouts=draft.prevouts,
            channels=draft.channels,
            ordinary_witnesses=witnesses,
            authorization=authorization,
            maximum_miner_fee_sat=draft.maximum_miner_fee_sat,
        )
        ChannelInputParentCoordinator._validate_plan(plan)
        return plan
    except (AttributeError, ChannelInputParentError, TypeError, ValueError) as exc:
        raise ChannelInputPlanningError(str(exc)) from exc


def _require_hellos_ready(negotiation: TakerChannelInputNegotiation) -> None:
    if negotiation.stage is not TakerChannelInputStage.HELLOS_READY:
        raise ValueError("channel input negotiation is not HELLOS_READY")


def _exact_hellos(
    values: tuple[tuple[str, ChannelInputHelloPayload], ...],
    selected_nicks: tuple[str, ...],
    label: str,
) -> dict[str, ChannelInputHelloPayload]:
    if len(values) != len(selected_nicks):
        raise ValueError(f"{label} hello roster is incomplete")
    result = dict(values)
    if len(result) != len(values) or set(result) != set(selected_nicks):
        raise ValueError(f"{label} hello roster differs from selected roster")
    return result


def _require_exact_fee_roster(
    maker_fees_sat: Mapping[str, int], selected_nicks: tuple[str, ...]
) -> None:
    if set(maker_fees_sat) != set(selected_nicks) or len(maker_fees_sat) != len(selected_nicks):
        raise ValueError("maker fee mapping must exactly cover the selected roster")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in maker_fees_sat.values()
    ):
        raise ValueError("maker fees must be nonnegative integers")


def _validate_hello_mirror(
    negotiation: TakerChannelInputNegotiation,
    maker: ChannelInputHelloPayload,
    coordinator: ChannelInputHelloPayload,
) -> None:
    if (
        maker.session_id != coordinator.session_id
        or maker.attempt != coordinator.attempt
        or maker.funding_outpoint != coordinator.funding_outpoint
        or maker.capacity_sat != coordinator.capacity_sat
        or maker.funding_script_pubkey != coordinator.funding_script_pubkey
    ):
        raise ValueError("accepted hello mirror fields disagree")
    if (
        maker.participant_key == negotiation.coordinator_key
        or coordinator.participant_key != negotiation.coordinator_key
        or maker.node_key != coordinator.peer_node_key
        or maker.peer_node_key != coordinator.node_key
        or maker.frozen_local_claim_sat != coordinator.frozen_remote_claim_sat
        or maker.frozen_remote_claim_sat != coordinator.frozen_local_claim_sat
    ):
        raise ValueError("accepted hello endpoints are not reciprocal")


def _validate_claim_adjustment(
    maker: ChannelInputHelloPayload, coordinator: ChannelInputHelloPayload, fee_sat: int
) -> None:
    maker_claim = maker.frozen_local_claim_sat + fee_sat
    coordinator_claim = coordinator.frozen_local_claim_sat - fee_sat
    maker_policy = maker.policy_ceilings
    coordinator_policy = coordinator.policy_ceilings
    if (
        fee_sat > maker_policy.max_claim_adjustment_sat
        or fee_sat > coordinator_policy.max_claim_adjustment_sat
        or maker_claim <= 0
        or coordinator_claim <= 0
    ):
        raise ValueError("maker fee claim adjustment exceeds accepted policy or claim balance")


def _channel_id(outpoint: Outpoint) -> str:
    return f"channel-input:{outpoint.txid}:{outpoint.vout}"


def _endpoint_id(outpoint: Outpoint, role: str) -> str:
    return f"channel-input:{outpoint.txid}:{outpoint.vout}:{role}"


def _fees_from_contributions(
    negotiation: TakerChannelInputNegotiation,
    contributions: tuple[ChannelTransactionContribution, ...],
) -> dict[str, int]:
    if len(contributions) != len(negotiation.selected_nicks):
        raise ValueError("pre-build contributions do not cover selected roster")
    remotes = _exact_hellos(negotiation.remote_hellos, negotiation.selected_nicks, "remote")
    fees: dict[str, int] = {}
    for nick, contribution in zip(negotiation.selected_nicks, contributions, strict=True):
        maker = remotes[nick]
        expected_channel_id = _channel_id(maker.funding_outpoint)
        if contribution.channel_id != expected_channel_id:
            raise ValueError("pre-build contribution channel differs from accepted hello")
        fee_sat = contribution.first_endpoint.output.value - maker.frozen_local_claim_sat
        if fee_sat < 0:
            raise ValueError("pre-build maker settlement is below frozen claim")
        fees[nick] = fee_sat
    return fees


def _validate_current_height(current_height: int) -> None:
    if (
        isinstance(current_height, bool)
        or not isinstance(current_height, int)
        or not 0 <= current_height <= 0xFFFFFFFF
    ):
        raise ValueError("current height must be a uint32")


def _validate_maximum_fee(
    negotiation: TakerChannelInputNegotiation, maximum_miner_fee_sat: int
) -> None:
    if (
        isinstance(maximum_miner_fee_sat, bool)
        or not isinstance(maximum_miner_fee_sat, int)
        or not 0 < maximum_miner_fee_sat <= MAX_PARENT_FEE_SAT
        or maximum_miner_fee_sat > negotiation.invite.max_parent_fee_sat
    ):
        raise ValueError("maximum miner fee exceeds accepted parent fee limit")


def _metadata_sequence(
    metadata: Mapping[str, object], name: str, length: int
) -> tuple[object, ...]:
    value = metadata.get(name)
    if not isinstance(value, list | tuple) or len(value) != length:
        raise ValueError(f"metadata {name} is not index-aligned with parent")
    return tuple(value)


def _derive_prevouts(
    inputs: Sequence[TxInput], values: tuple[object, ...], scripts: tuple[object, ...]
) -> tuple[InputPrevout, ...]:
    result: list[InputPrevout] = []
    for input_, value, script in zip(inputs, values, scripts, strict=True):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("metadata input value must be a positive integer")
        if not isinstance(script, str):
            raise ValueError("metadata input script must be hexadecimal")
        result.append(
            InputPrevout(
                outpoint=Outpoint(txid=input_.txid, vout=input_.vout),
                value_sat=value,
                script_pubkey=script,
            )
        )
    return tuple(result)


def _derive_channels(
    negotiation: TakerChannelInputNegotiation,
    transaction: ParsedTransaction,
    prevouts: tuple[InputPrevout, ...],
    contributions: tuple[ChannelTransactionContribution, ...],
    channel_input_ids: tuple[object, ...],
    endpoint_outputs: tuple[object, ...],
) -> tuple[tuple[ChannelContribution, ...], tuple[EndpointTerms, ...]]:
    inputs = transaction.inputs
    outputs = transaction.outputs
    by_channel = {contribution.channel_id: contribution for contribution in contributions}
    if len(by_channel) != len(contributions):
        raise ValueError("pre-build contributions contain duplicate channel IDs")
    positions: dict[str, int] = {}
    for index, channel_id in enumerate(channel_input_ids):
        if channel_id is None:
            continue
        if (
            not isinstance(channel_id, str)
            or channel_id not in by_channel
            or channel_id in positions
        ):
            raise ValueError("metadata channel input IDs do not exactly map contributions")
        contribution = by_channel[channel_id]
        input_ = inputs[index]
        if (
            input_.txid != contribution.funding_input.txid
            or input_.vout != contribution.funding_input.vout
            or prevouts[index].value_sat != contribution.funding_input.value
            or prevouts[index].script_pubkey != contribution.funding_input.scriptpubkey.hex()
        ):
            raise ValueError("metadata channel input does not match final parent input")
        positions[channel_id] = index
    if set(positions) != set(by_channel):
        raise ValueError("metadata omits a channel input contribution")

    output_positions: dict[tuple[str, str], int] = {}
    for index, descriptor in enumerate(endpoint_outputs):
        if descriptor is None:
            continue
        if not isinstance(descriptor, Mapping) or set(descriptor) != {
            "channel_id",
            "endpoint_id",
            "participant_key",
            "script_pubkey",
            "value",
        }:
            raise ValueError("channel endpoint output metadata has an invalid shape")
        channel_id = descriptor["channel_id"]
        endpoint_id = descriptor["endpoint_id"]
        participant_key = descriptor["participant_key"]
        script_pubkey = descriptor["script_pubkey"]
        value = descriptor["value"]
        if (
            not isinstance(channel_id, str)
            or not isinstance(endpoint_id, str)
            or not isinstance(participant_key, str)
            or not isinstance(script_pubkey, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or channel_id not in by_channel
        ):
            raise ValueError("channel endpoint output metadata has invalid values")
        endpoint = next(
            (
                item
                for item in (
                    by_channel[channel_id].first_endpoint,
                    by_channel[channel_id].second_endpoint,
                )
                if item.endpoint_id == endpoint_id
            ),
            None,
        )
        output = outputs[index]
        if (
            endpoint is None
            or endpoint.participant_key != participant_key
            or endpoint.output.scriptpubkey != script_pubkey
            or endpoint.output.value != value
            or output.script.hex() != script_pubkey
            or output.value != value
            or (channel_id, endpoint_id) in output_positions
        ):
            raise ValueError("channel endpoint output metadata differs from final parent")
        output_positions[channel_id, endpoint_id] = index
    expected_endpoints = {
        (contribution.channel_id, endpoint.endpoint_id)
        for contribution in contributions
        for endpoint in (contribution.first_endpoint, contribution.second_endpoint)
    }
    if set(output_positions) != expected_endpoints:
        raise ValueError("metadata does not exactly cover channel settlement outputs")

    remotes = _exact_hellos(negotiation.remote_hellos, negotiation.selected_nicks, "remote")
    locals_ = _exact_hellos(negotiation.local_hellos, negotiation.selected_nicks, "local")
    channels: list[ChannelContribution] = []
    terms: list[EndpointTerms] = []
    for nick, contribution in zip(negotiation.selected_nicks, contributions, strict=True):
        maker = remotes[nick]
        coordinator = locals_[nick]
        first = _endpoint_terms(
            negotiation,
            maker,
            contribution,
            contribution.first_endpoint,
            positions[contribution.channel_id],
            output_positions[contribution.channel_id, contribution.first_endpoint.endpoint_id],
            "first",
            contribution.first_endpoint.output.value - maker.frozen_local_claim_sat,
        )
        second = _endpoint_terms(
            negotiation,
            coordinator,
            contribution,
            contribution.second_endpoint,
            positions[contribution.channel_id],
            output_positions[contribution.channel_id, contribution.second_endpoint.endpoint_id],
            "second",
            contribution.second_endpoint.output.value - coordinator.frozen_local_claim_sat,
        )
        terms.extend((first, second))
        channels.append(
            ChannelContribution(
                channel_id=contribution.channel_id,
                channel_input_index=positions[contribution.channel_id],
                first=_parent_endpoint(contribution.first_endpoint, first),
                second=_parent_endpoint(contribution.second_endpoint, second),
            )
        )
    return tuple(channels), tuple(terms)


def _endpoint_terms(
    negotiation: TakerChannelInputNegotiation,
    hello: ChannelInputHelloPayload,
    contribution: ChannelTransactionContribution,
    endpoint: ChannelEndpointSettlement,
    input_index: int,
    output_index: int,
    role: Literal["first", "second"],
    adjustment: int,
) -> EndpointTerms:
    return EndpointTerms(
        session_id=negotiation.invite.session_id,
        attempt=negotiation.invite.attempt,
        funding_outpoint=hello.funding_outpoint,
        input_index=input_index,
        participant_key=endpoint.participant_key,
        role=role,
        local_node_key=hello.node_key,
        peer_node_key=hello.peer_node_key,
        capacity_sat=contribution.funding_input.value,
        frozen_local_claim_sat=hello.frozen_local_claim_sat,
        frozen_remote_claim_sat=hello.frozen_remote_claim_sat,
        settlement_outputs=(
            AuthorizedSettlementOutput(
                output_index=output_index,
                amount_sat=endpoint.output.value,
                script_pubkey=endpoint.output.scriptpubkey,
            ),
        ),
        miner_fee_share_sat=0,
        claim_adjustment_sat=adjustment,
        policy_ceilings=hello.policy_ceilings,
    )


def _parent_endpoint(
    endpoint: ChannelEndpointSettlement, terms: EndpointTerms
) -> EndpointContribution:
    return EndpointContribution(
        endpoint_id=endpoint.endpoint_id,
        participant_key=endpoint.participant_key,
        settlement_outputs=tuple(
            SettlementOutput(
                output_index=output.output_index,
                value_sat=output.amount_sat,
                script_pubkey=output.script_pubkey,
            )
            for output in terms.settlement_outputs
        ),
        miner_fee_share_sat=0,
        claim_adjustment_sat=terms.claim_adjustment_sat,
    )


def _actual_fee(
    metadata: Mapping[str, object],
    prevouts: tuple[InputPrevout, ...],
    outputs: Sequence[TxOutput],
    maximum_miner_fee_sat: int,
) -> int:
    actual_fee = metadata.get("actual_fee")
    calculated = sum(prevout.value_sat for prevout in prevouts) - sum(
        output.value for output in outputs
    )
    if (
        isinstance(actual_fee, bool)
        or not isinstance(actual_fee, int)
        or actual_fee != calculated
        or not 0 < actual_fee <= maximum_miner_fee_sat
    ):
        raise ValueError("metadata actual fee is nonpositive, excessive, or inconsistent")
    return actual_fee


def _build_transcript(
    negotiation: TakerChannelInputNegotiation,
    unsigned_parent: bytes,
    prevouts: tuple[InputPrevout, ...],
    channels: tuple[ChannelContribution, ...],
    endpoint_terms: tuple[EndpointTerms, ...],
    fee_sat: int,
    current_height: int,
) -> ChannelInputTranscript:
    recovery_delta = (
        negotiation.invite.min_recovery_delta + negotiation.invite.max_recovery_delta
    ) // 2
    recovery_height = current_height + recovery_delta
    if recovery_height > 0xFFFFFFFF:
        raise ValueError("recovery height overflows uint32")
    descriptors = tuple(
        ChannelDescriptor(
            funding_outpoint=first.funding_outpoint,
            input_index=channel.channel_input_index,
            capacity_sat=first.capacity_sat,
            first_participant_key=first.participant_key,
            second_participant_key=second.participant_key,
            first_terms_hash=endpoint_terms_hash(first).hex(),
            second_terms_hash=endpoint_terms_hash(second).hex(),
        )
        for channel, (first, second) in zip(channels, _term_pairs(endpoint_terms), strict=True)
    )
    input_types = [_script_type(prevout.script_pubkey) for prevout in prevouts]
    transaction = parse_transaction_bytes(unsigned_parent)
    output_types = [_script_type(output.script.hex()) for output in transaction.outputs]
    vsize = estimate_vsize(input_types, output_types)
    return ChannelInputTranscript(
        network=negotiation.network,
        session_id=negotiation.invite.session_id,
        attempt=negotiation.invite.attempt,
        unsigned_parent_tx=unsigned_parent.hex(),
        input_prevouts=prevouts,
        total_fee_sat=fee_sat,
        fee_rate_sat_vb=(fee_sat + vsize - 1) // vsize,
        expiry=negotiation.invite.expiry,
        recovery_height=recovery_height,
        participant_keys=tuple(
            sorted(
                (
                    negotiation.coordinator_key,
                    *(descriptor.first_participant_key for descriptor in descriptors),
                )
            )
        ),
        coordinator_key=negotiation.coordinator_key,
        channels=descriptors,
    )


def _term_pairs(
    terms: tuple[EndpointTerms, ...],
) -> tuple[tuple[EndpointTerms, EndpointTerms], ...]:
    if len(terms) % 2:
        raise ValueError("endpoint terms must contain pairs")
    return tuple((terms[index], terms[index + 1]) for index in range(0, len(terms), 2))


def _script_type(script_pubkey: str) -> str:
    if len(script_pubkey) == 44 and script_pubkey.startswith("0014"):
        return "p2wpkh"
    if len(script_pubkey) == 68 and script_pubkey.startswith("0020"):
        return "p2wsh"
    if len(script_pubkey) == 68 and script_pubkey.startswith("5120"):
        return "p2tr"
    raise ValueError("parent scripts must be native segwit")


# Short aliases keep the public planner API obvious at call sites.
build_channel_contributions = plan_channel_input_contributions
derive_parent_draft = derive_channel_input_parent_draft
authorize_parent_plan = authorize_channel_input_parent
