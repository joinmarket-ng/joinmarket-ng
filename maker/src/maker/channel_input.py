"""Durable maker-side negotiation for channel-input v1 before backend preparation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Literal, cast

from coincurve import PublicKey
from jmcore.bitcoin import get_txid, parse_transaction_bytes
from jmcore.channel_input import (
    MAX_CHANNELS,
    ChannelDescriptor,
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputFinalizedPayload,
    ChannelInputFinalizePayload,
    ChannelInputHelloPayload,
    ChannelInputInvitePayload,
    ChannelInputKeyPair,
    ChannelInputModel,
    ChannelInputPartialPayload,
    ChannelInputPayload,
    ChannelInputPreparedPayload,
    ChannelInputPreparePayload,
    ChannelInputProposalPayload,
    ChannelInputSignPayload,
    ChannelInputValidationError,
    Network,
    Outpoint,
    PolicyCeilings,
    approval_set_hash,
    canonical_json,
    endpoint_terms_hash,
    sign_payload,
    transcript_hash,
    validate_complete_approval_set,
    validate_finalize_for_transcript,
    validate_finalized_for_transcript,
    validate_hello_for_invite,
    validate_partial_for_transcript,
    validate_prepare_payload,
    validate_prepared_for_transcript,
    validate_proposal_for_hellos,
    validate_recipient_approval,
    validate_sign_for_transcript,
)
from jmcore.channel_input_config import ChannelInputConfig
from jmcore.channel_input_session import (
    ChannelInputActorRegistry,
    ChannelInputReplayLedger,
    ChannelInputReplayResult,
)
from jmswap.lnd import (
    ChannelInputCandidate,
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
    LndFinalizedEscrowParent,
)
from pydantic import Field, field_serializer, field_validator, model_validator


class MakerChannelInputStage(StrEnum):
    """Durable stages that do not make any LND or network calls."""

    INVITED = "invited"
    FREEZE_INTENDED = "freeze_intended"
    HELLOS_READY = "hellos_ready"
    PROPOSAL_APPROVED = "proposal_approved"
    APPROVAL_SET_ACKED = "approval_set_acked"
    PREPARED = "prepared"
    NONCES_READY = "nonces_ready"
    SIGNING = "signing"
    FINALIZING = "finalizing"
    FINALIZED = "finalized"


class MakerFrozenChannelInput(ChannelInputModel):
    """One exact LND freeze result and the P2TR script owned by this maker."""

    snapshot: FrozenChannelSnapshot
    settlement_script_pubkey: str

    @field_validator("snapshot", mode="before")
    @classmethod
    def parse_snapshot_json(cls, value: object) -> object:
        # FrozenChannelSnapshot has byte fields. Persist them as hexadecimal in this model.
        if not isinstance(value, dict):
            return value
        converted = dict(value)
        for name in (
            "funding_scriptpubkey",
            "peer_node_pubkey",
            "local_funding_pubkey",
            "remote_funding_pubkey",
        ):
            item = converted.get(name)
            if isinstance(item, str):
                try:
                    converted[name] = bytes.fromhex(item)
                except ValueError as exc:
                    raise ValueError(f"frozen {name} must be hexadecimal") from exc
        return converted

    @field_serializer("snapshot", when_used="json")
    def serialize_snapshot_json(self, value: FrozenChannelSnapshot) -> dict[str, object]:
        data = value.model_dump(mode="python")
        for name in (
            "funding_scriptpubkey",
            "peer_node_pubkey",
            "local_funding_pubkey",
            "remote_funding_pubkey",
        ):
            data[name] = cast(bytes, data[name]).hex()
        return data

    @model_validator(mode="after")
    def validate_settlement_script(self) -> MakerFrozenChannelInput:
        _validate_p2tr_script(self.settlement_script_pubkey, "settlement_script_pubkey")
        return self


class MakerChannelInputFreezeIntent(ChannelInputModel):
    """Durable exact request facts for a future idempotent LND freeze RPC."""

    channel_id: str
    funding_outpoint: EscrowOutpoint
    peer_node_pubkey: str
    capacity_sat: int = Field(gt=0)
    settlement_script_pubkey: str

    @field_validator("channel_id")
    @classmethod
    def validate_channel_id(cls, value: str) -> str:
        _validate_outpoint_identifier(value, "channel_id")
        return value

    @field_validator("peer_node_pubkey")
    @classmethod
    def validate_peer_node_pubkey(cls, value: str) -> str:
        _validate_node_key(value, "peer_node_pubkey")
        return value

    @field_validator("settlement_script_pubkey")
    @classmethod
    def validate_settlement_script_pubkey(cls, value: str) -> str:
        _validate_p2tr_script(value, "settlement_script_pubkey")
        return value

    @model_validator(mode="after")
    def validate_channel_id_matches_outpoint(self) -> MakerChannelInputFreezeIntent:
        if self.channel_id != _outpoint_identifier(self.funding_outpoint):
            raise ValueError("channel_id must equal the funding outpoint")
        return self


class MakerChannelInputChannelState(ChannelInputModel):
    """Durable per-channel LND signing boundary and authenticated wire evidence."""

    funding_outpoint: Outpoint
    parent_txid: str
    attempt_id: str = Field(repr=False)
    prepared: ChannelInputPreparedPayload
    restart_intended: bool = False
    sign_request: ChannelInputSignPayload | None = None
    partial: ChannelInputPartialPayload | None = None
    finalize_request: ChannelInputFinalizePayload | None = None
    finalized: ChannelInputFinalizedPayload | None = None

    @field_validator("parent_txid", "attempt_id")
    @classmethod
    def validate_hex32(cls, value: str) -> str:
        if not isinstance(value, str) or len(value) != 64 or value.lower() != value:
            raise ValueError("channel state value must be 32-byte lowercase hexadecimal")
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("channel state value must be 32-byte lowercase hexadecimal") from exc
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> MakerChannelInputChannelState:
        payloads = (
            self.prepared,
            self.sign_request,
            self.partial,
            self.finalize_request,
            self.finalized,
        )
        for payload in payloads:
            if payload is not None and payload.funding_outpoint != self.funding_outpoint:
                raise ValueError("channel state payload funding outpoint differs from state")
        if self.partial is not None and self.sign_request is None:
            raise ValueError("channel state partial requires a sign request")
        if self.finalize_request is not None and self.partial is None:
            raise ValueError("channel state finalize request requires a partial")
        if self.finalized is not None and self.finalize_request is None:
            raise ValueError("channel state finalized evidence requires a finalize request")
        if self.restart_intended and any(
            value is not None
            for value in (
                self.sign_request,
                self.partial,
                self.finalize_request,
                self.finalized,
            )
        ):
            raise ValueError("nonce restart intent conflicts with signing evidence")
        return self


class MakerChannelInputSnapshot(ChannelInputModel):
    """Complete immutable persisted negotiation state, including signed wire data."""

    version: Literal[1] = 1
    config: ChannelInputConfig
    network: Network
    local_actor: str = Field(min_length=1)
    coordinator_actor: str = Field(min_length=1)
    local_node_key: str
    participant_secret_key_hex: str = Field(repr=False)
    stage: MakerChannelInputStage
    invite: ChannelInputInvitePayload
    freeze_intents: tuple[MakerChannelInputFreezeIntent, ...] = ()
    frozen_inputs: tuple[MakerFrozenChannelInput, ...] = ()
    hellos: tuple[ChannelInputHelloPayload, ...] = ()
    proposal: ChannelInputProposalPayload | None = None
    accepted_height: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    local_approval: ChannelInputApprovalPayload | None = None
    approval_set: ChannelInputApprovalSetPayload | None = None
    local_ack: ChannelInputApprovalSetAckPayload | None = None
    prepare: ChannelInputPreparePayload | None = None
    channel_states: tuple[MakerChannelInputChannelState, ...] = ()
    actor_registry_snapshot: tuple[tuple[str, str], ...]
    replay_ledger_snapshot: tuple[tuple[str, str], ...]

    @field_validator("participant_secret_key_hex")
    @classmethod
    def validate_secret_key(cls, value: str) -> str:
        if len(value) != 64 or value.lower() != value:
            raise ValueError("participant_secret_key_hex must be 32-byte lowercase hexadecimal")
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(
                "participant_secret_key_hex must be 32-byte lowercase hexadecimal"
            ) from exc
        return value

    @model_validator(mode="after")
    def validate_stage_shape(self) -> MakerChannelInputSnapshot:
        _validate_node_key(self.local_node_key, "local_node_key")
        if self.network != self.invite.network:
            raise ValueError("snapshot maker network differs from invitation")
        if self.local_actor == self.coordinator_actor:
            raise ValueError("local and coordinator actors must differ")
        if (
            len(self.freeze_intents) > MAX_CHANNELS
            or len(self.frozen_inputs) > MAX_CHANNELS
            or len(self.hellos) > MAX_CHANNELS
        ):
            raise ValueError("channel input snapshot exceeds channel limit")
        stage_rank = {
            MakerChannelInputStage.INVITED: 0,
            MakerChannelInputStage.FREEZE_INTENDED: 1,
            MakerChannelInputStage.HELLOS_READY: 2,
            MakerChannelInputStage.PROPOSAL_APPROVED: 3,
            MakerChannelInputStage.APPROVAL_SET_ACKED: 4,
            MakerChannelInputStage.PREPARED: 5,
            MakerChannelInputStage.NONCES_READY: 6,
            MakerChannelInputStage.SIGNING: 7,
            MakerChannelInputStage.FINALIZING: 8,
            MakerChannelInputStage.FINALIZED: 9,
        }
        rank = stage_rank[self.stage]
        if self.stage is MakerChannelInputStage.INVITED:
            if self.freeze_intents or self.frozen_inputs or self.hellos:
                raise ValueError("invited snapshot contains freeze or hello evidence")
        elif self.stage is MakerChannelInputStage.FREEZE_INTENDED:
            if not self.freeze_intents or self.frozen_inputs or self.hellos:
                raise ValueError("freeze-intended snapshot evidence does not match stage")
        elif (
            not self.freeze_intents
            or not self.frozen_inputs
            or len(self.freeze_intents) != len(self.frozen_inputs)
            or len(self.frozen_inputs) != len(self.hellos)
        ):
            raise ValueError("snapshot intents, frozen inputs, and hellos do not match stage")
        if rank >= 3 and (self.proposal is None or self.local_approval is None):
            raise ValueError("snapshot proposal and approval do not match stage")
        if rank < 3 and (self.proposal is not None or self.local_approval is not None):
            raise ValueError("snapshot proposal and approval do not match stage")
        if (rank >= 3) != (self.accepted_height is not None):
            raise ValueError("snapshot accepted height does not match stage")
        if rank >= 4 and (self.approval_set is None or self.local_ack is None):
            raise ValueError("snapshot approval set and ACK do not match stage")
        if rank < 4 and (self.approval_set is not None or self.local_ack is not None):
            raise ValueError("snapshot approval set and ACK do not match stage")
        if rank >= 5 and self.prepare is None:
            raise ValueError("snapshot prepare does not match stage")
        if rank < 5 and self.prepare is not None:
            raise ValueError("snapshot prepare does not match stage")
        if rank < 5 and self.channel_states:
            raise ValueError("snapshot channel states precede prepare")
        ordered_states = tuple(
            sorted(
                self.channel_states, key=lambda state: _outpoint_sort_key(state.funding_outpoint)
            )
        )
        if ordered_states != self.channel_states:
            raise ValueError("snapshot channel states are not deterministically ordered")
        if len(
            {_outpoint_sort_key(state.funding_outpoint) for state in self.channel_states}
        ) != len(self.channel_states):
            raise ValueError("snapshot channel states must have unique funding outpoints")
        if rank == 5:
            if self.proposal is not None and len(self.channel_states) >= len(
                self.proposal.recipient_terms
            ):
                raise ValueError("prepared snapshot has complete channel nonce coverage")
            if any(
                state.sign_request is not None
                or state.partial is not None
                or state.finalize_request is not None
                or state.finalized is not None
                for state in self.channel_states
            ):
                raise ValueError("prepared snapshot contains signing evidence")
        elif rank > 5:
            if self.proposal is None:
                raise ValueError("snapshot channel states require a proposal")
            expected_count = len(self.proposal.recipient_terms)
            if len(self.channel_states) != expected_count:
                raise ValueError("snapshot channel state count does not match local terms")
            if self.stage is MakerChannelInputStage.NONCES_READY and any(
                state.sign_request is not None for state in self.channel_states
            ):
                raise ValueError("nonces-ready snapshot contains sign requests")
            if self.stage is MakerChannelInputStage.SIGNING and (
                not any(
                    state.sign_request is not None or state.partial is not None
                    for state in self.channel_states
                )
                or any(
                    state.finalize_request is not None or state.finalized is not None
                    for state in self.channel_states
                )
            ):
                raise ValueError("signing snapshot channel states do not match stage")
            if self.stage is MakerChannelInputStage.FINALIZING and not any(
                state.finalize_request is not None or state.finalized is not None
                for state in self.channel_states
            ):
                raise ValueError("finalizing snapshot channel states do not match stage")
            if self.stage is MakerChannelInputStage.FINALIZED and not all(
                state.finalized is not None for state in self.channel_states
            ):
                raise ValueError("finalized snapshot is missing channel evidence")
        return self


class MakerChannelInputNegotiation:
    """Maker-owned authenticated channel-input negotiation through accepted prepare."""

    def __init__(
        self,
        *,
        config: ChannelInputConfig,
        network: Network,
        local_actor: str,
        coordinator_actor: str,
        local_node_key: str,
        key_pair: ChannelInputKeyPair,
        invite: ChannelInputInvitePayload,
        registry: ChannelInputActorRegistry,
        ledger: ChannelInputReplayLedger,
        stage: MakerChannelInputStage = MakerChannelInputStage.INVITED,
        freeze_intents: tuple[MakerChannelInputFreezeIntent, ...] = (),
        frozen_inputs: tuple[MakerFrozenChannelInput, ...] = (),
        hellos: tuple[ChannelInputHelloPayload, ...] = (),
        proposal: ChannelInputProposalPayload | None = None,
        accepted_height: int | None = None,
        local_approval: ChannelInputApprovalPayload | None = None,
        approval_set: ChannelInputApprovalSetPayload | None = None,
        local_ack: ChannelInputApprovalSetAckPayload | None = None,
        prepare: ChannelInputPreparePayload | None = None,
        channel_states: tuple[MakerChannelInputChannelState, ...] = (),
    ) -> None:
        self.config = config
        self.network = network
        self.local_actor = local_actor
        self.coordinator_actor = coordinator_actor
        self.local_node_key = local_node_key
        self._key_pair = key_pair
        self.invite = invite
        self._registry = registry
        self._ledger = ledger
        self.stage = stage
        self.freeze_intents = freeze_intents
        self.frozen_inputs = frozen_inputs
        self.hellos = hellos
        self.proposal = proposal
        self.accepted_height = accepted_height
        self.local_approval = local_approval
        self.approval_set = approval_set
        self.local_ack = local_ack
        self.prepare = prepare
        self.channel_states = channel_states

    @classmethod
    def from_invite(
        cls,
        *,
        local_actor: str,
        coordinator_actor: str,
        invite: ChannelInputInvitePayload,
        config: ChannelInputConfig,
        network: Network,
        local_node_key: str,
        key_pair: ChannelInputKeyPair | None = None,
    ) -> MakerChannelInputNegotiation:
        """Bind a signed invitation before the caller freezes any local channel."""

        if not config.enabled:
            raise ChannelInputValidationError("channel input is disabled")
        _revalidate_payload(invite)
        if invite.network != network:
            raise ChannelInputValidationError("invite network differs from maker network")
        if not (
            invite.min_recovery_delta <= config.recovery_delay_blocks <= invite.max_recovery_delta
        ):
            raise ChannelInputValidationError(
                "maker recovery delay is outside invitation recovery bounds"
            )
        _validate_node_key(local_node_key, "local_node_key")
        participant = key_pair or ChannelInputKeyPair.generate()
        if (
            ChannelInputKeyPair.from_secret(participant.secret_key).public_key
            != participant.public_key
        ):
            raise ChannelInputValidationError("injected participant key pair is inconsistent")
        registry = ChannelInputActorRegistry()
        registry.bind_verified(coordinator_actor, invite)
        registry.bind(local_actor, participant.public_key)
        ledger = ChannelInputReplayLedger()
        _record_new(ledger, _slot("invite"), invite)
        return cls(
            config=config,
            network=network,
            local_actor=local_actor,
            coordinator_actor=coordinator_actor,
            local_node_key=local_node_key,
            key_pair=participant,
            invite=invite,
            registry=registry,
            ledger=ledger,
        )

    @property
    def participant_key(self) -> str:
        return self._key_pair.public_key

    def snapshot(self) -> MakerChannelInputSnapshot:
        """Return state suitable for durable persistence at every protocol boundary."""

        return MakerChannelInputSnapshot(
            config=self.config,
            network=self.network,
            local_actor=self.local_actor,
            coordinator_actor=self.coordinator_actor,
            local_node_key=self.local_node_key,
            participant_secret_key_hex=self._key_pair.secret_key.hex(),
            stage=self.stage,
            invite=self.invite,
            freeze_intents=self.freeze_intents,
            frozen_inputs=self.frozen_inputs,
            hellos=self.hellos,
            proposal=self.proposal,
            accepted_height=self.accepted_height,
            local_approval=self.local_approval,
            approval_set=self.approval_set,
            local_ack=self.local_ack,
            prepare=self.prepare,
            channel_states=self.channel_states,
            actor_registry_snapshot=self._registry.snapshot(),
            replay_ledger_snapshot=self._ledger.snapshot(),
        )

    @classmethod
    def from_snapshot(cls, snapshot: MakerChannelInputSnapshot) -> MakerChannelInputNegotiation:
        """Restore only a state whose payloads, actor bindings, and ledger all agree."""

        snapshot = MakerChannelInputSnapshot.model_validate(snapshot.model_dump(mode="python"))
        try:
            key_pair = ChannelInputKeyPair.from_secret(
                bytes.fromhex(snapshot.participant_secret_key_hex)
            )
            registry = ChannelInputActorRegistry.from_snapshot(snapshot.actor_registry_snapshot)
            ledger = ChannelInputReplayLedger.from_snapshot(snapshot.replay_ledger_snapshot)
        except (TypeError, ValueError) as exc:
            raise ChannelInputValidationError(
                "channel input snapshot credentials are invalid"
            ) from exc
        negotiation = cls(
            config=snapshot.config,
            network=snapshot.network,
            local_actor=snapshot.local_actor,
            coordinator_actor=snapshot.coordinator_actor,
            local_node_key=snapshot.local_node_key,
            key_pair=key_pair,
            invite=snapshot.invite,
            registry=registry,
            ledger=ledger,
            stage=snapshot.stage,
            freeze_intents=snapshot.freeze_intents,
            frozen_inputs=snapshot.frozen_inputs,
            hellos=snapshot.hellos,
            proposal=snapshot.proposal,
            accepted_height=snapshot.accepted_height,
            local_approval=snapshot.local_approval,
            approval_set=snapshot.approval_set,
            local_ack=snapshot.local_ack,
            prepare=snapshot.prepare,
            channel_states=snapshot.channel_states,
        )
        negotiation._validate_restored_state()
        return negotiation

    def plan_freeze(
        self,
        candidates: Sequence[ChannelInputCandidate],
        *,
        settlement_scripts: Mapping[str, str],
    ) -> tuple[MakerChannelInputFreezeIntent, ...]:
        """Persist exact outpoint-based freeze requests before calling an LND backend."""

        normalized = _normalize_freeze_candidates(candidates)
        intents = _freeze_intents_from_candidates(
            self.config,
            self.local_node_key,
            normalized,
            settlement_scripts=settlement_scripts,
        )
        if self.freeze_intents:
            if intents != self.freeze_intents:
                raise ChannelInputValidationError(
                    "freeze candidates conflict with prior freeze intents"
                )
            return self.freeze_intents
        if self.stage is not MakerChannelInputStage.INVITED:
            raise ChannelInputValidationError(
                "freeze intents are not valid at this negotiation stage"
            )
        self.freeze_intents = intents
        self.stage = MakerChannelInputStage.FREEZE_INTENDED
        return intents

    def install_frozen_inputs(
        self, frozen_inputs: Sequence[MakerFrozenChannelInput]
    ) -> tuple[ChannelInputHelloPayload, ...]:
        """Install exact LND freeze evidence and create one signed hello per outpoint."""

        normalized = _normalize_frozen_inputs(frozen_inputs)
        if self.frozen_inputs:
            if normalized != self.frozen_inputs:
                raise ChannelInputValidationError(
                    "frozen channel inputs conflict with prior hello evidence"
                )
            return self.hellos
        if self.stage is not MakerChannelInputStage.FREEZE_INTENDED:
            raise ChannelInputValidationError(
                "frozen inputs are not valid at this negotiation stage"
            )
        _validate_frozen_inputs(self.config, self.invite, self.local_node_key, normalized)
        _validate_frozen_inputs_against_intents(self.freeze_intents, normalized)
        ceilings = PolicyCeilings(
            max_miner_fee_share_sat=self.config.max_miner_fee_share_sat,
            max_claim_adjustment_sat=self.config.max_claim_adjustment_sat,
            max_settlement_outputs=1,
        )
        hellos: list[ChannelInputHelloPayload] = []
        for frozen in normalized:
            snapshot = frozen.snapshot
            hello = ChannelInputHelloPayload(
                session_id=self.invite.session_id,
                attempt=self.invite.attempt,
                signer_key=self.participant_key,
                participant_key=self.participant_key,
                node_key=self.local_node_key,
                peer_node_key=snapshot.peer_node_pubkey.hex(),
                funding_outpoint=Outpoint(
                    txid=snapshot.funding_outpoint.txid,
                    vout=snapshot.funding_outpoint.vout,
                ),
                capacity_sat=snapshot.capacity_sat,
                funding_script_pubkey=snapshot.funding_scriptpubkey.hex(),
                frozen_local_claim_sat=snapshot.local_claim_sat,
                frozen_remote_claim_sat=snapshot.remote_claim_sat,
                settlement_script_pubkey=frozen.settlement_script_pubkey,
                quoted_miner_fee_share_sat=self.config.max_miner_fee_share_sat,
                policy_ceilings=ceilings,
            )
            signed = sign_payload(hello, self._key_pair.secret_key)
            self._registry.require_verified(self.local_actor, signed)
            _record_new(self._ledger, _hello_slot(signed), signed)
            hellos.append(signed)
        self.frozen_inputs = normalized
        self.hellos = tuple(hellos)
        self.stage = MakerChannelInputStage.HELLOS_READY
        return self.hellos

    def accept_proposal(
        self,
        coordinator_actor: str,
        proposal: ChannelInputProposalPayload,
        *,
        current_time: int,
        current_height: int,
    ) -> ChannelInputApprovalPayload:
        """Validate and approve exact local endpoint terms from the coordinator."""

        self._require_coordinator(coordinator_actor, proposal)
        if self.proposal is not None:
            if proposal != self.proposal:
                raise ChannelInputValidationError("proposal conflicts with accepted proposal")
            assert self.local_approval is not None
            return self.local_approval
        if self.stage is not MakerChannelInputStage.HELLOS_READY:
            raise ChannelInputValidationError("proposal is not valid at this negotiation stage")
        _validate_maker_proposal_policy(self.config, proposal, current_height)
        validate_proposal_for_hellos(
            self.invite,
            self.hellos,
            proposal,
            current_time=current_time,
            current_height=current_height,
        )
        if proposal.recipient_key != self.participant_key:
            raise ChannelInputValidationError("proposal is assigned to a different participant")
        _record_new(self._ledger, _slot("proposal"), proposal)
        terms_hashes = tuple(
            sorted(endpoint_terms_hash(terms).hex() for terms in proposal.recipient_terms)
        )
        approval = ChannelInputApprovalPayload(
            session_id=proposal.session_id,
            attempt=proposal.attempt,
            signer_key=self.participant_key,
            transcript_hash=proposal.transcript_hash,
            terms_hashes=terms_hashes,
        )
        signed = sign_payload(approval, self._key_pair.secret_key)
        self._registry.require_verified(self.local_actor, signed)
        _record_new(self._ledger, _slot("approval"), signed)
        self.proposal = proposal
        self.accepted_height = current_height
        self.local_approval = signed
        self.stage = MakerChannelInputStage.PROPOSAL_APPROVED
        return signed

    def accept_approval_set(
        self,
        coordinator_actor: str,
        approval_set: ChannelInputApprovalSetPayload,
    ) -> ChannelInputApprovalSetAckPayload:
        """Require the exact accepted transcript's complete approval set and ACK it."""

        self._require_coordinator(coordinator_actor, approval_set)
        if self.approval_set is not None:
            if approval_set != self.approval_set:
                raise ChannelInputValidationError(
                    "approval set conflicts with accepted approval set"
                )
            assert self.local_ack is not None
            return self.local_ack
        if self.stage is not MakerChannelInputStage.PROPOSAL_APPROVED:
            raise ChannelInputValidationError("approval set is not valid at this negotiation stage")
        assert self.proposal is not None
        if approval_set.transcript != self.proposal.transcript:
            raise ChannelInputValidationError(
                "approval set transcript differs from accepted proposal"
            )
        validate_complete_approval_set(approval_set)
        validate_recipient_approval(
            approval_set, self.participant_key, self.proposal.recipient_terms
        )
        assert self.local_approval is not None
        _require_exact_approval(approval_set, self.local_approval)
        _record_new(self._ledger, _slot("approval_set"), approval_set)
        ack = ChannelInputApprovalSetAckPayload(
            session_id=approval_set.session_id,
            attempt=approval_set.attempt,
            signer_key=self.participant_key,
            transcript_hash=transcript_hash(approval_set.transcript).hex(),
            approval_set_hash=approval_set_hash(approval_set).hex(),
        )
        signed = sign_payload(ack, self._key_pair.secret_key)
        self._registry.require_verified(self.local_actor, signed)
        _record_new(self._ledger, _slot("approval_ack"), signed)
        self.approval_set = approval_set
        self.local_ack = signed
        self.stage = MakerChannelInputStage.APPROVAL_SET_ACKED
        return signed

    def accept_prepare(
        self, coordinator_actor: str, prepare: ChannelInputPreparePayload
    ) -> ChannelInputPreparePayload:
        """Accept complete prepare authorization without touching the backend."""

        self._require_coordinator(coordinator_actor, prepare)
        if self.prepare is not None:
            if prepare != self.prepare:
                raise ChannelInputValidationError("prepare conflicts with accepted prepare")
            return self.prepare
        if self.stage is not MakerChannelInputStage.APPROVAL_SET_ACKED:
            raise ChannelInputValidationError("prepare is not valid at this negotiation stage")
        assert self.approval_set is not None and self.proposal is not None
        if prepare.approval_set != self.approval_set:
            raise ChannelInputValidationError(
                "prepare approval set differs from accepted approval set"
            )
        validate_prepare_payload(prepare)
        validate_recipient_approval(
            prepare.approval_set, self.participant_key, self.proposal.recipient_terms
        )
        assert self.local_ack is not None
        _require_exact_ack(prepare, self.local_ack)
        _record_new(self._ledger, _slot("prepare"), prepare)
        self.prepare = prepare
        self.stage = MakerChannelInputStage.PREPARED
        return prepare

    def record_prepared(
        self,
        funding_outpoint: Outpoint,
        parent_txid: str,
        attempt: LndEscrowSigningAttempt,
    ) -> ChannelInputPreparedPayload:
        """Persist a locally generated LND nonce before it is sent to the coordinator."""

        self._require_prepared_negotiation()
        _require_parent_txid(parent_txid, self._parent_txid())
        attempt = _validated_signing_attempt(attempt)
        self._require_local_outpoint(funding_outpoint)
        existing = self._channel_state(funding_outpoint)
        if existing is not None:
            if (
                existing.parent_txid == parent_txid
                and existing.attempt_id == attempt.attempt_id.hex()
                and existing.prepared.public_nonce == attempt.public_nonce.hex()
            ):
                return existing.prepared
            raise ChannelInputValidationError("prepared nonce conflicts with durable channel state")
        prepared = self._sign_prepared(funding_outpoint, attempt)
        self._replace_channel_state(
            MakerChannelInputChannelState(
                funding_outpoint=funding_outpoint,
                parent_txid=parent_txid,
                attempt_id=attempt.attempt_id.hex(),
                prepared=prepared,
            )
        )
        self._recompute_channel_stage()
        return prepared

    def rotate_unsigned_prepared(
        self,
        funding_outpoint: Outpoint,
        parent_txid: str,
        attempt: LndEscrowSigningAttempt,
    ) -> ChannelInputPreparedPayload:
        """Replace an unconsumed LND nonce after an explicit backend restart decision."""

        self._require_prepared_negotiation()
        _require_parent_txid(parent_txid, self._parent_txid())
        attempt = _validated_signing_attempt(attempt)
        state = self._require_channel_state(funding_outpoint)
        if not state.restart_intended:
            raise ChannelInputValidationError("prepared nonce rotation lacks durable intent")
        if any(
            value is not None
            for value in (
                state.sign_request,
                state.partial,
                state.finalize_request,
                state.finalized,
            )
        ):
            raise ChannelInputValidationError(
                "prepared nonce cannot rotate after signing authorization"
            )
        if state.attempt_id == attempt.attempt_id.hex():
            raise ChannelInputValidationError("prepared nonce rotation requires a new LND attempt")
        prepared = self._sign_prepared(funding_outpoint, attempt)
        self._replace_channel_state(
            state.model_copy(
                update={
                    "parent_txid": parent_txid,
                    "attempt_id": attempt.attempt_id.hex(),
                    "prepared": prepared,
                    "restart_intended": False,
                }
            )
        )
        self._recompute_channel_stage()
        return prepared

    def plan_unsigned_prepared_rotation(self, funding_outpoint: Outpoint) -> None:
        """Persist authorization to replace one unconsumed LND nonce attempt."""

        self._require_prepared_negotiation()
        state = self._require_channel_state(funding_outpoint)
        if any(
            value is not None
            for value in (
                state.sign_request,
                state.partial,
                state.finalize_request,
                state.finalized,
            )
        ):
            raise ChannelInputValidationError(
                "prepared nonce cannot restart after signing authorization"
            )
        if state.restart_intended:
            return
        self._replace_channel_state(state.model_copy(update={"restart_intended": True}))
        self._recompute_channel_stage()

    def signing_attempt_for(self, funding_outpoint: Outpoint) -> LndEscrowSigningAttempt:
        """Reconstruct the exact persisted LND attempt for a later pure backend call."""

        state = self._require_channel_state(funding_outpoint)
        return LndEscrowSigningAttempt(
            attempt_id=bytes.fromhex(state.attempt_id),
            public_nonce=bytes.fromhex(state.prepared.public_nonce),
        )

    def accept_sign(
        self, coordinator_actor: str, sign: ChannelInputSignPayload
    ) -> ChannelInputPartialPayload | None:
        """Persist a verified coordinator signing authorization without calling LND."""

        self._require_coordinator(coordinator_actor, sign)
        if self.stage is MakerChannelInputStage.PREPARED:
            raise ChannelInputValidationError("sign requires complete local nonce coverage")
        state = self._require_channel_state(sign.funding_outpoint)
        if state.restart_intended:
            raise ChannelInputValidationError("sign conflicts with a durable nonce restart intent")
        assert self.proposal is not None
        validate_sign_for_transcript(
            sign,
            self.proposal.transcript,
            self.participant_key,
            expected_local_public_nonce=state.prepared.public_nonce,
        )
        if state.sign_request is not None:
            if sign != state.sign_request:
                raise ChannelInputValidationError(
                    "sign request conflicts with durable channel state"
                )
            return state.partial
        self._replace_channel_state(state.model_copy(update={"sign_request": sign}))
        self._recompute_channel_stage()
        return None

    def record_partial(
        self, funding_outpoint: Outpoint, partial_signature: bytes
    ) -> ChannelInputPartialPayload:
        """Sign and persist a local partial after its coordinator intent is durable."""

        state = self._require_channel_state(funding_outpoint)
        if state.sign_request is None:
            raise ChannelInputValidationError("partial requires a durable sign request")
        if not isinstance(partial_signature, bytes) or len(partial_signature) != 32:
            raise ChannelInputValidationError("partial signature must be exactly 32 bytes")
        if state.partial is not None:
            if state.partial.partial_signature == partial_signature.hex():
                return state.partial
            raise ChannelInputValidationError(
                "partial signature conflicts with durable channel state"
            )
        partial = ChannelInputPartialPayload(
            session_id=state.sign_request.session_id,
            attempt=state.sign_request.attempt,
            signer_key=self.participant_key,
            transcript_hash=state.sign_request.transcript_hash,
            funding_outpoint=funding_outpoint,
            local_public_nonce=state.prepared.public_nonce,
            peer_public_nonce=state.sign_request.peer_prepared.public_nonce,
            partial_signature=partial_signature.hex(),
        )
        signed = sign_payload(partial, self._key_pair.secret_key)
        self._validate_local_partial(signed, state)
        self._replace_channel_state(state.model_copy(update={"partial": signed}))
        self._recompute_channel_stage()
        return signed

    def accept_finalize(
        self, coordinator_actor: str, finalize: ChannelInputFinalizePayload
    ) -> ChannelInputFinalizedPayload | None:
        """Persist a verified coordinator finalization authorization without calling LND."""

        self._require_coordinator(coordinator_actor, finalize)
        if self.stage is MakerChannelInputStage.PREPARED:
            raise ChannelInputValidationError("finalize requires complete local nonce coverage")
        state = self._require_channel_state(finalize.funding_outpoint)
        if state.partial is None or state.sign_request is None:
            raise ChannelInputValidationError("finalize requires a durable local partial")
        assert self.proposal is not None
        validate_finalize_for_transcript(
            finalize,
            self.proposal.transcript,
            self.participant_key,
            expected_local_public_nonce=state.prepared.public_nonce,
            expected_peer_public_nonce=state.sign_request.peer_prepared.public_nonce,
        )
        if state.finalize_request is not None:
            if finalize != state.finalize_request:
                raise ChannelInputValidationError(
                    "finalize request conflicts with durable channel state"
                )
            return state.finalized
        self._replace_channel_state(state.model_copy(update={"finalize_request": finalize}))
        self._recompute_channel_stage()
        return None

    def record_finalized(
        self, funding_outpoint: Outpoint, result: LndFinalizedEscrowParent
    ) -> ChannelInputFinalizedPayload:
        """Persist the exact one-witness LND parent transaction before network release."""

        state = self._require_channel_state(funding_outpoint)
        if state.finalize_request is None:
            raise ChannelInputValidationError(
                "finalized parent requires a durable finalize request"
            )
        result = _validated_finalized_parent(result)
        _require_parent_txid(result.txid, self._parent_txid())
        assert self.proposal is not None
        descriptor = _descriptor_for_local_outpoint(self.proposal, funding_outpoint)
        if result.channel_input_index != descriptor.input_index:
            raise ChannelInputValidationError(
                "finalized parent channel input index differs from transcript"
            )
        try:
            transaction = parse_transaction_bytes(bytes.fromhex(result.tx_hex))
        except (ValueError, IndexError) as exc:
            raise ChannelInputValidationError("finalized parent transaction is invalid") from exc
        if (
            len(transaction.witnesses) <= descriptor.input_index
            or tuple(transaction.witnesses[descriptor.input_index]) != result.channel_input_witness
        ):
            raise ChannelInputValidationError("finalized parent witness differs from LND result")
        if state.finalized is not None:
            if (
                state.finalized.channel_signed_parent_tx == result.tx_hex
                and state.finalized.txid == result.txid
            ):
                return state.finalized
            raise ChannelInputValidationError(
                "finalized parent conflicts with durable channel state"
            )
        try:
            finalized = ChannelInputFinalizedPayload(
                session_id=state.finalize_request.session_id,
                attempt=state.finalize_request.attempt,
                signer_key=self.participant_key,
                transcript_hash=state.finalize_request.transcript_hash,
                funding_outpoint=funding_outpoint,
                channel_signed_parent_tx=result.tx_hex,
                txid=result.txid,
            )
        except ValueError as exc:
            raise ChannelInputValidationError("finalized parent differs from transcript") from exc
        signed = sign_payload(finalized, self._key_pair.secret_key)
        self._validate_local_finalized(signed)
        self._replace_channel_state(state.model_copy(update={"finalized": signed}))
        self._recompute_channel_stage()
        return signed

    def _require_prepared_negotiation(self) -> None:
        if self.prepare is None or self.stage not in (
            MakerChannelInputStage.PREPARED,
            MakerChannelInputStage.NONCES_READY,
            MakerChannelInputStage.SIGNING,
            MakerChannelInputStage.FINALIZING,
            MakerChannelInputStage.FINALIZED,
        ):
            raise ChannelInputValidationError("channel preparation has not been accepted")

    def _parent_txid(self) -> str:
        assert self.proposal is not None
        return get_txid(self.proposal.transcript.unsigned_parent_tx)

    def _expected_local_outpoints(self) -> tuple[Outpoint, ...]:
        assert self.proposal is not None
        outpoints = tuple(terms.funding_outpoint for terms in self.proposal.recipient_terms)
        if len({_outpoint_sort_key(outpoint) for outpoint in outpoints}) != len(outpoints):
            raise ChannelInputValidationError(
                "proposal local terms have duplicate funding outpoints"
            )
        return tuple(sorted(outpoints, key=_outpoint_sort_key))

    def _require_local_outpoint(self, funding_outpoint: Outpoint) -> None:
        if funding_outpoint not in self._expected_local_outpoints():
            raise ChannelInputValidationError("funding outpoint is not a local recipient term")

    def _channel_state(self, funding_outpoint: Outpoint) -> MakerChannelInputChannelState | None:
        return next(
            (state for state in self.channel_states if state.funding_outpoint == funding_outpoint),
            None,
        )

    def _require_channel_state(self, funding_outpoint: Outpoint) -> MakerChannelInputChannelState:
        self._require_local_outpoint(funding_outpoint)
        state = self._channel_state(funding_outpoint)
        if state is None:
            raise ChannelInputValidationError("funding outpoint has no durable LND signing attempt")
        return state

    def _replace_channel_state(self, replacement: MakerChannelInputChannelState) -> None:
        states = [
            replacement if state.funding_outpoint == replacement.funding_outpoint else state
            for state in self.channel_states
        ]
        if not any(
            state.funding_outpoint == replacement.funding_outpoint for state in self.channel_states
        ):
            states.append(replacement)
        self.channel_states = tuple(
            sorted(states, key=lambda state: _outpoint_sort_key(state.funding_outpoint))
        )

    def _sign_prepared(
        self, funding_outpoint: Outpoint, attempt: LndEscrowSigningAttempt
    ) -> ChannelInputPreparedPayload:
        assert self.proposal is not None
        prepared = ChannelInputPreparedPayload(
            session_id=self.proposal.session_id,
            attempt=self.proposal.attempt,
            signer_key=self.participant_key,
            transcript_hash=self.proposal.transcript_hash,
            funding_outpoint=funding_outpoint,
            public_nonce=attempt.public_nonce.hex(),
        )
        signed = sign_payload(prepared, self._key_pair.secret_key)
        self._registry.require_verified(self.local_actor, signed)
        validate_prepared_for_transcript(signed, self.proposal.transcript, self.participant_key)
        return signed

    def _validate_local_partial(
        self, partial: ChannelInputPartialPayload, state: MakerChannelInputChannelState
    ) -> None:
        assert self.proposal is not None and state.sign_request is not None
        self._registry.require_verified(self.local_actor, partial)
        validate_partial_for_transcript(
            partial,
            self.proposal.transcript,
            self.participant_key,
            expected_local_public_nonce=state.prepared.public_nonce,
            expected_peer_public_nonce=state.sign_request.peer_prepared.public_nonce,
        )

    def _validate_local_finalized(self, finalized: ChannelInputFinalizedPayload) -> None:
        assert self.proposal is not None
        self._registry.require_verified(self.local_actor, finalized)
        validate_finalized_for_transcript(finalized, self.proposal.transcript)

    def _recompute_channel_stage(self) -> None:
        expected_count = len(self._expected_local_outpoints())
        if len(self.channel_states) < expected_count:
            self.stage = MakerChannelInputStage.PREPARED
        elif all(state.finalized is not None for state in self.channel_states):
            self.stage = MakerChannelInputStage.FINALIZED
        elif any(
            state.finalize_request is not None or state.finalized is not None
            for state in self.channel_states
        ):
            self.stage = MakerChannelInputStage.FINALIZING
        elif any(
            state.sign_request is not None or state.partial is not None
            for state in self.channel_states
        ):
            self.stage = MakerChannelInputStage.SIGNING
        else:
            self.stage = MakerChannelInputStage.NONCES_READY

    def _require_coordinator(self, actor: str, payload: ChannelInputPayload) -> None:
        if actor != self.coordinator_actor:
            raise ChannelInputValidationError("channel input message came from a different actor")
        _revalidate_payload(payload)
        self._registry.require_verified(actor, payload)

    def _validate_restored_state(self) -> None:
        if not self.config.enabled:
            raise ChannelInputValidationError("restored channel input configuration is disabled")
        if self.network != self.invite.network:
            raise ChannelInputValidationError("restored maker network differs from invitation")
        if not (
            self.invite.min_recovery_delta
            <= self.config.recovery_delay_blocks
            <= self.invite.max_recovery_delta
        ):
            raise ChannelInputValidationError(
                "restored maker recovery delay is outside invitation bounds"
            )
        if self._key_pair.public_key == self.invite.signer_key:
            raise ChannelInputValidationError("participant key must differ from coordinator key")
        expected_registry = tuple(
            sorted(
                (
                    (self.coordinator_actor, self.invite.signer_key),
                    (self.local_actor, self.participant_key),
                )
            )
        )
        if self._registry.snapshot() != expected_registry:
            raise ChannelInputValidationError("actor registry does not match negotiation actors")
        self._registry.require_verified(self.coordinator_actor, self.invite)
        expected_entries: list[tuple[str, ChannelInputPayload]] = [(_slot("invite"), self.invite)]
        if self.stage is not MakerChannelInputStage.INVITED:
            _validate_freeze_intents(self.config, self.local_node_key, self.freeze_intents)
        if self.stage not in (
            MakerChannelInputStage.INVITED,
            MakerChannelInputStage.FREEZE_INTENDED,
        ):
            _validate_frozen_inputs(
                self.config, self.invite, self.local_node_key, self.frozen_inputs
            )
            _validate_frozen_inputs_against_intents(self.freeze_intents, self.frozen_inputs)
            for frozen, hello in zip(self.frozen_inputs, self.hellos, strict=True):
                self._registry.require_verified(self.local_actor, hello)
                _validate_hello_against_frozen(self.invite, self.local_node_key, frozen, hello)
                expected_entries.append((_hello_slot(hello), hello))
        if self.proposal is not None:
            self._require_coordinator(self.coordinator_actor, self.proposal)
            validate_proposal_for_hellos(
                self.invite,
                self.hellos,
                self.proposal,
                current_time=0,
                current_height=_require_accepted_height(self.accepted_height),
            )
            if self.proposal.recipient_key != self.participant_key:
                raise ChannelInputValidationError(
                    "restored proposal belongs to another participant"
                )
            assert self.local_approval is not None
            _validate_maker_proposal_policy(
                self.config, self.proposal, _require_accepted_height(self.accepted_height)
            )
            self._registry.require_verified(self.local_actor, self.local_approval)
            expected_hashes = tuple(
                sorted(endpoint_terms_hash(terms).hex() for terms in self.proposal.recipient_terms)
            )
            if (
                self.local_approval.session_id != self.proposal.session_id
                or self.local_approval.attempt != self.proposal.attempt
                or self.local_approval.transcript_hash != self.proposal.transcript_hash
                or self.local_approval.terms_hashes != expected_hashes
            ):
                raise ChannelInputValidationError("restored local approval differs from proposal")
            expected_entries.extend(
                ((_slot("proposal"), self.proposal), (_slot("approval"), self.local_approval))
            )
        if self.approval_set is not None:
            assert self.proposal is not None and self.local_ack is not None
            self._require_coordinator(self.coordinator_actor, self.approval_set)
            if self.approval_set.transcript != self.proposal.transcript:
                raise ChannelInputValidationError(
                    "restored approval set transcript differs from proposal"
                )
            validate_complete_approval_set(self.approval_set)
            validate_recipient_approval(
                self.approval_set, self.participant_key, self.proposal.recipient_terms
            )
            assert self.local_approval is not None
            _require_exact_approval(self.approval_set, self.local_approval)
            self._registry.require_verified(self.local_actor, self.local_ack)
            if (
                self.local_ack.session_id != self.approval_set.session_id
                or self.local_ack.attempt != self.approval_set.attempt
                or self.local_ack.transcript_hash
                != transcript_hash(self.approval_set.transcript).hex()
                or self.local_ack.approval_set_hash != approval_set_hash(self.approval_set).hex()
            ):
                raise ChannelInputValidationError("restored local ACK differs from approval set")
            expected_entries.extend(
                (
                    (_slot("approval_set"), self.approval_set),
                    (_slot("approval_ack"), self.local_ack),
                )
            )
        if self.prepare is not None:
            assert self.approval_set is not None and self.proposal is not None
            self._require_coordinator(self.coordinator_actor, self.prepare)
            if self.prepare.approval_set != self.approval_set:
                raise ChannelInputValidationError("restored prepare differs from approval set")
            validate_prepare_payload(self.prepare)
            validate_recipient_approval(
                self.prepare.approval_set, self.participant_key, self.proposal.recipient_terms
            )
            assert self.local_ack is not None
            _require_exact_ack(self.prepare, self.local_ack)
            expected_entries.append((_slot("prepare"), self.prepare))
        if self.prepare is not None:
            self._validate_restored_channel_states()
        expected_ledger = tuple(
            sorted((slot, _payload_digest(payload)) for slot, payload in expected_entries)
        )
        if self._ledger.snapshot() != expected_ledger:
            raise ChannelInputValidationError("replay ledger does not match negotiation payloads")

    def _validate_restored_channel_states(self) -> None:
        """Recheck all state not covered by the immutable pre-prepare replay ledger."""

        assert self.proposal is not None
        expected_outpoints = self._expected_local_outpoints()
        frozen_outpoints = {hello.funding_outpoint.key() for hello in self.hellos}
        if any(outpoint.key() not in frozen_outpoints for outpoint in expected_outpoints):
            raise ChannelInputValidationError(
                "proposal local term is not backed by frozen hello evidence"
            )
        state_outpoints = tuple(state.funding_outpoint for state in self.channel_states)
        if any(outpoint not in expected_outpoints for outpoint in state_outpoints):
            raise ChannelInputValidationError("channel state is not a local recipient term")
        if len(set(_outpoint_sort_key(outpoint) for outpoint in state_outpoints)) != len(
            state_outpoints
        ):
            raise ChannelInputValidationError("channel states have duplicate funding outpoints")
        if tuple(sorted(state_outpoints, key=_outpoint_sort_key)) != state_outpoints:
            raise ChannelInputValidationError("channel states are not deterministically ordered")
        parent_txid = self._parent_txid()
        for state in self.channel_states:
            _require_parent_txid(state.parent_txid, parent_txid)
            try:
                attempt = LndEscrowSigningAttempt(
                    attempt_id=bytes.fromhex(state.attempt_id),
                    public_nonce=bytes.fromhex(state.prepared.public_nonce),
                )
            except ValueError as exc:
                raise ChannelInputValidationError("channel signing attempt is invalid") from exc
            if attempt.public_nonce.hex() != state.prepared.public_nonce:
                raise ChannelInputValidationError(
                    "channel prepared nonce differs from signing attempt"
                )
            self._registry.require_verified(self.local_actor, state.prepared)
            validate_prepared_for_transcript(
                state.prepared, self.proposal.transcript, self.participant_key
            )
            if state.sign_request is not None:
                self._registry.require_verified(self.coordinator_actor, state.sign_request)
                validate_sign_for_transcript(
                    state.sign_request,
                    self.proposal.transcript,
                    self.participant_key,
                    expected_local_public_nonce=state.prepared.public_nonce,
                )
            if state.partial is not None:
                if state.sign_request is None:
                    raise ChannelInputValidationError("restored partial lacks sign request")
                self._validate_local_partial(state.partial, state)
            if state.finalize_request is not None:
                if state.sign_request is None:
                    raise ChannelInputValidationError("restored finalize lacks sign request")
                self._registry.require_verified(self.coordinator_actor, state.finalize_request)
                validate_finalize_for_transcript(
                    state.finalize_request,
                    self.proposal.transcript,
                    self.participant_key,
                    expected_local_public_nonce=state.prepared.public_nonce,
                    expected_peer_public_nonce=state.sign_request.peer_prepared.public_nonce,
                )
            if state.finalized is not None:
                self._validate_local_finalized(state.finalized)
        original_stage = self.stage
        self._recompute_channel_stage()
        if self.stage is not original_stage:
            raise ChannelInputValidationError("restored channel state shape differs from stage")


def _slot(name: str) -> str:
    return f"maker-channel-input-v1:{name}"


def _outpoint_sort_key(outpoint: Outpoint) -> tuple[str, int]:
    return (outpoint.txid, outpoint.vout)


def _require_parent_txid(value: str, expected: str) -> None:
    if value != expected:
        raise ChannelInputValidationError("parent txid differs from accepted transcript")


def _validated_signing_attempt(attempt: LndEscrowSigningAttempt) -> LndEscrowSigningAttempt:
    if not isinstance(attempt, LndEscrowSigningAttempt):
        raise ChannelInputValidationError("LND signing attempt is invalid")
    try:
        return LndEscrowSigningAttempt.model_validate(attempt.model_dump(mode="python"))
    except ValueError as exc:
        raise ChannelInputValidationError("LND signing attempt is invalid") from exc


def _validated_finalized_parent(result: LndFinalizedEscrowParent) -> LndFinalizedEscrowParent:
    if not isinstance(result, LndFinalizedEscrowParent):
        raise ChannelInputValidationError("LND finalized parent is invalid")
    try:
        return LndFinalizedEscrowParent.model_validate(result.model_dump(mode="python"))
    except ValueError as exc:
        raise ChannelInputValidationError("LND finalized parent is invalid") from exc


def _descriptor_for_local_outpoint(
    proposal: ChannelInputProposalPayload, funding_outpoint: Outpoint
) -> ChannelDescriptor:
    descriptors = [
        descriptor
        for descriptor in proposal.transcript.channels
        if descriptor.funding_outpoint == funding_outpoint
    ]
    if len(descriptors) != 1:
        raise ChannelInputValidationError("funding outpoint has no unique transcript descriptor")
    return descriptors[0]


def _hello_slot(hello: ChannelInputHelloPayload) -> str:
    return _slot(f"hello:{hello.funding_outpoint.txid}:{hello.funding_outpoint.vout}")


def _payload_digest(payload: ChannelInputPayload) -> str:
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _record_new(ledger: ChannelInputReplayLedger, slot: str, payload: ChannelInputPayload) -> None:
    if ledger.record(slot, payload) is not ChannelInputReplayResult.NEW:
        raise ChannelInputValidationError("channel input replay slot was already recorded")


def _require_exact_approval(
    approval_set: ChannelInputApprovalSetPayload, local_approval: ChannelInputApprovalPayload
) -> None:
    included = next(
        (
            approval
            for approval in approval_set.approvals
            if approval.signer_key == local_approval.signer_key
        ),
        None,
    )
    if included != local_approval:
        raise ChannelInputValidationError("approval set does not contain the exact local approval")


def _require_exact_ack(
    prepare: ChannelInputPreparePayload, local_ack: ChannelInputApprovalSetAckPayload
) -> None:
    included = next(
        (ack for ack in prepare.approval_set_acks if ack.signer_key == local_ack.signer_key), None
    )
    if included != local_ack:
        raise ChannelInputValidationError("prepare does not contain the exact local ACK")


def _revalidate_payload(payload: ChannelInputPayload) -> None:
    try:
        payload_type = type(payload)
        validated = payload_type.model_validate(payload.model_dump(mode="python"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ChannelInputValidationError("channel input payload is structurally invalid") from exc
    if validated != payload:
        raise ChannelInputValidationError("channel input payload is not canonical")


def _require_accepted_height(accepted_height: int | None) -> int:
    if accepted_height is None:
        raise ChannelInputValidationError("restored proposal is missing accepted height")
    return accepted_height


def _validate_maker_proposal_policy(
    config: ChannelInputConfig, proposal: ChannelInputProposalPayload, current_height: int
) -> None:
    if not 0 <= current_height <= 0xFFFFFFFF:
        raise ChannelInputValidationError("maker current height is outside uint32 bounds")
    transcript = proposal.transcript
    if transcript.total_fee_sat > config.max_parent_fee_sat:
        raise ChannelInputValidationError("transcript fee exceeds maker policy maximum")
    if transcript.fee_rate_sat_vb > config.max_fee_rate_sat_vb:
        raise ChannelInputValidationError("transcript fee rate exceeds maker policy maximum")
    if transcript.recovery_height - current_height != config.recovery_delay_blocks:
        raise ChannelInputValidationError("transcript recovery height differs from maker policy")


def _normalize_frozen_inputs(
    frozen_inputs: Sequence[MakerFrozenChannelInput],
) -> tuple[MakerFrozenChannelInput, ...]:
    if isinstance(frozen_inputs, str | bytes):
        raise ChannelInputValidationError("frozen channel inputs must be a sequence")
    normalized = tuple(frozen_inputs)
    if not 1 <= len(normalized) <= MAX_CHANNELS:
        raise ChannelInputValidationError("frozen channel input count is outside protocol bounds")
    if any(not isinstance(item, MakerFrozenChannelInput) for item in normalized):
        raise ChannelInputValidationError("frozen channel inputs must use exact frozen snapshots")
    try:
        normalized = tuple(
            MakerFrozenChannelInput.model_validate(item.model_dump(mode="python"))
            for item in normalized
        )
    except ValueError as exc:
        raise ChannelInputValidationError("frozen channel input evidence is invalid") from exc
    return tuple(
        sorted(
            normalized,
            key=lambda item: (
                item.snapshot.funding_outpoint.txid,
                item.snapshot.funding_outpoint.vout,
            ),
        )
    )


def _normalize_freeze_candidates(
    candidates: Sequence[ChannelInputCandidate],
) -> tuple[ChannelInputCandidate, ...]:
    if isinstance(candidates, str | bytes):
        raise ChannelInputValidationError("freeze candidates must be a sequence")
    normalized = tuple(candidates)
    if not 1 <= len(normalized) <= MAX_CHANNELS:
        raise ChannelInputValidationError("freeze candidate count is outside protocol bounds")
    if any(not isinstance(item, ChannelInputCandidate) for item in normalized):
        raise ChannelInputValidationError(
            "freeze candidates must use exact LND candidate snapshots"
        )
    try:
        normalized = tuple(
            ChannelInputCandidate.model_validate(item.model_dump(mode="python"))
            for item in normalized
        )
    except ValueError as exc:
        raise ChannelInputValidationError("freeze candidate evidence is invalid") from exc
    outpoints = {(item.funding_outpoint.txid, item.funding_outpoint.vout) for item in normalized}
    channel_ids = {item.channel_id for item in normalized}
    if len(outpoints) != len(normalized) or len(channel_ids) != len(normalized):
        raise ChannelInputValidationError("freeze candidates must have unique outpoints and IDs")
    return tuple(
        sorted(
            normalized,
            key=lambda item: (item.funding_outpoint.txid, item.funding_outpoint.vout),
        )
    )


def _freeze_intents_from_candidates(
    config: ChannelInputConfig,
    local_node_key: str,
    candidates: tuple[ChannelInputCandidate, ...],
    *,
    settlement_scripts: Mapping[str, str],
) -> tuple[MakerChannelInputFreezeIntent, ...]:
    _validate_freeze_candidates(config, local_node_key, candidates)
    expected_outpoints = {
        _outpoint_identifier(candidate.funding_outpoint) for candidate in candidates
    }
    if not isinstance(settlement_scripts, Mapping) or set(settlement_scripts) != expected_outpoints:
        raise ChannelInputValidationError(
            "settlement scripts must exactly cover the freeze candidate outpoints"
        )
    for script in settlement_scripts.values():
        _validate_p2tr_script(script, "settlement_script_pubkey")
    try:
        return tuple(
            MakerChannelInputFreezeIntent(
                channel_id=_outpoint_identifier(candidate.funding_outpoint),
                funding_outpoint=candidate.funding_outpoint,
                peer_node_pubkey=candidate.peer_node_pubkey,
                capacity_sat=candidate.capacity_sat,
                settlement_script_pubkey=settlement_scripts[
                    _outpoint_identifier(candidate.funding_outpoint)
                ],
            )
            for candidate in candidates
        )
    except ValueError as exc:
        raise ChannelInputValidationError("freeze candidate intent is invalid") from exc


def _validate_freeze_candidates(
    config: ChannelInputConfig,
    local_node_key: str,
    candidates: tuple[ChannelInputCandidate, ...],
) -> None:
    if not config.enabled:
        raise ChannelInputValidationError("channel input is disabled")
    _validate_node_key(local_node_key, "local_node_key")
    allowed_outpoints = set(config.allowed_channel_points)
    allowed_peers = set(config.allowed_peer_node_pubkeys)
    for candidate in candidates:
        point_text = _outpoint_identifier(candidate.funding_outpoint)
        if point_text not in allowed_outpoints:
            raise ChannelInputValidationError(
                "freeze funding outpoint is not allowed by configuration"
            )
        if allowed_peers and candidate.peer_node_pubkey not in allowed_peers:
            raise ChannelInputValidationError("freeze channel peer is not allowed by configuration")
        if not (
            config.min_channel_capacity_sat
            <= candidate.capacity_sat
            <= config.max_channel_capacity_sat
        ):
            raise ChannelInputValidationError(
                "freeze channel capacity is outside configuration bounds"
            )
        if local_node_key == candidate.peer_node_pubkey:
            raise ChannelInputValidationError("local node key must differ from freeze channel peer")


def _validate_freeze_intents(
    config: ChannelInputConfig,
    local_node_key: str,
    intents: tuple[MakerChannelInputFreezeIntent, ...],
) -> None:
    if not 1 <= len(intents) <= MAX_CHANNELS:
        raise ChannelInputValidationError("freeze intent count is outside protocol bounds")
    try:
        normalized = tuple(
            MakerChannelInputFreezeIntent.model_validate(intent.model_dump(mode="python"))
            for intent in intents
        )
    except ValueError as exc:
        raise ChannelInputValidationError("freeze intent evidence is invalid") from exc
    ordered = tuple(
        sorted(
            normalized,
            key=lambda intent: (intent.funding_outpoint.txid, intent.funding_outpoint.vout),
        )
    )
    if normalized != ordered:
        raise ChannelInputValidationError("freeze intents are not deterministically ordered")
    outpoints = {
        (intent.funding_outpoint.txid, intent.funding_outpoint.vout) for intent in normalized
    }
    channel_ids = {intent.channel_id for intent in normalized}
    if len(outpoints) != len(normalized) or len(channel_ids) != len(normalized):
        raise ChannelInputValidationError("freeze intents must have unique outpoints and IDs")
    _validate_freeze_intent_policy(config, local_node_key, normalized)


def _validate_freeze_intent_policy(
    config: ChannelInputConfig,
    local_node_key: str,
    intents: tuple[MakerChannelInputFreezeIntent, ...],
) -> None:
    if not config.enabled:
        raise ChannelInputValidationError("channel input is disabled")
    _validate_node_key(local_node_key, "local_node_key")
    allowed_outpoints = set(config.allowed_channel_points)
    allowed_peers = set(config.allowed_peer_node_pubkeys)
    for intent in intents:
        if intent.channel_id not in allowed_outpoints:
            raise ChannelInputValidationError(
                "freeze funding outpoint is not allowed by configuration"
            )
        if allowed_peers and intent.peer_node_pubkey not in allowed_peers:
            raise ChannelInputValidationError("freeze channel peer is not allowed by configuration")
        if not (
            config.min_channel_capacity_sat
            <= intent.capacity_sat
            <= config.max_channel_capacity_sat
        ):
            raise ChannelInputValidationError(
                "freeze channel capacity is outside configuration bounds"
            )
        if local_node_key == intent.peer_node_pubkey:
            raise ChannelInputValidationError("local node key must differ from freeze channel peer")


def _validate_frozen_inputs_against_intents(
    intents: tuple[MakerChannelInputFreezeIntent, ...],
    frozen_inputs: tuple[MakerFrozenChannelInput, ...],
) -> None:
    if len(intents) != len(frozen_inputs):
        raise ChannelInputValidationError("frozen inputs do not match freeze intent count")
    intents_by_outpoint = {
        (intent.funding_outpoint.txid, intent.funding_outpoint.vout): intent for intent in intents
    }
    for frozen in frozen_inputs:
        snapshot = frozen.snapshot
        intent = intents_by_outpoint.get(
            (snapshot.funding_outpoint.txid, snapshot.funding_outpoint.vout)
        )
        if intent is None:
            raise ChannelInputValidationError("frozen input is not covered by a freeze intent")
        if (
            snapshot.channel_id != intent.channel_id
            or snapshot.peer_node_pubkey.hex() != intent.peer_node_pubkey
            or snapshot.capacity_sat != intent.capacity_sat
            or frozen.settlement_script_pubkey != intent.settlement_script_pubkey
        ):
            raise ChannelInputValidationError("frozen input differs from its freeze intent")


def _validate_frozen_inputs(
    config: ChannelInputConfig,
    invite: ChannelInputInvitePayload,
    local_node_key: str,
    frozen_inputs: tuple[MakerFrozenChannelInput, ...],
) -> None:
    if not config.enabled:
        raise ChannelInputValidationError("channel input is disabled")
    _validate_node_key(local_node_key, "local_node_key")
    if not 1 <= len(frozen_inputs) <= MAX_CHANNELS:
        raise ChannelInputValidationError("frozen channel input count is outside protocol bounds")
    outpoints: set[tuple[str, int]] = set()
    channel_ids: set[str] = set()
    allowed_outpoints = set(config.allowed_channel_points)
    allowed_peers = set(config.allowed_peer_node_pubkeys)
    for frozen in frozen_inputs:
        snapshot = frozen.snapshot
        outpoint = (snapshot.funding_outpoint.txid, snapshot.funding_outpoint.vout)
        point_text = f"{snapshot.funding_outpoint.txid}:{snapshot.funding_outpoint.vout}"
        peer_key = snapshot.peer_node_pubkey.hex()
        if snapshot.session_id != invite.session_id:
            raise ChannelInputValidationError("frozen channel session does not match invitation")
        if outpoint in outpoints or snapshot.channel_id in channel_ids:
            raise ChannelInputValidationError(
                "frozen channel inputs must have unique outpoints and IDs"
            )
        if point_text not in allowed_outpoints:
            raise ChannelInputValidationError(
                "frozen funding outpoint is not allowed by configuration"
            )
        if allowed_peers and peer_key not in allowed_peers:
            raise ChannelInputValidationError("frozen channel peer is not allowed by configuration")
        if not (
            config.min_channel_capacity_sat
            <= snapshot.capacity_sat
            <= config.max_channel_capacity_sat
        ):
            raise ChannelInputValidationError(
                "frozen channel capacity is outside configuration bounds"
            )
        if local_node_key == peer_key:
            raise ChannelInputValidationError("local node key must differ from frozen channel peer")
        outpoints.add(outpoint)
        channel_ids.add(snapshot.channel_id)


def _validate_hello_against_frozen(
    invite: ChannelInputInvitePayload,
    local_node_key: str,
    frozen: MakerFrozenChannelInput,
    hello: ChannelInputHelloPayload,
) -> None:
    snapshot = frozen.snapshot
    validate_hello_for_invite(invite, hello)
    if (
        hello.participant_key != hello.signer_key
        or hello.node_key != local_node_key
        or hello.peer_node_key != snapshot.peer_node_pubkey.hex()
        or hello.funding_outpoint.key()
        != (snapshot.funding_outpoint.txid, snapshot.funding_outpoint.vout)
        or hello.capacity_sat != snapshot.capacity_sat
        or hello.funding_script_pubkey != snapshot.funding_scriptpubkey.hex()
        or hello.frozen_local_claim_sat != snapshot.local_claim_sat
        or hello.frozen_remote_claim_sat != snapshot.remote_claim_sat
        or hello.settlement_script_pubkey != frozen.settlement_script_pubkey
    ):
        raise ChannelInputValidationError("hello differs from exact frozen channel evidence")


def _outpoint_identifier(outpoint: EscrowOutpoint) -> str:
    return f"{outpoint.txid}:{outpoint.vout}"


def _validate_outpoint_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise ChannelInputValidationError(
            f"{field_name} must be a lowercase <64hex>:<uint32> outpoint"
        )
    txid, separator, vout_text = value.partition(":")
    if (
        not separator
        or ":" in vout_text
        or len(txid) != 64
        or txid.lower() != txid
        or not vout_text.isascii()
        or not vout_text.isdecimal()
        or (len(vout_text) > 1 and vout_text.startswith("0"))
    ):
        raise ChannelInputValidationError(
            f"{field_name} must be a lowercase <64hex>:<uint32> outpoint"
        )
    try:
        bytes.fromhex(txid)
        vout = int(vout_text)
    except ValueError as exc:
        raise ChannelInputValidationError(
            f"{field_name} must be a lowercase <64hex>:<uint32> outpoint"
        ) from exc
    if vout > 0xFFFFFFFF:
        raise ChannelInputValidationError(
            f"{field_name} must be a lowercase <64hex>:<uint32> outpoint"
        )


def _validate_p2tr_script(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 68
        or value.lower() != value
        or not value.startswith("5120")
    ):
        raise ChannelInputValidationError(f"{field_name} must be a P2TR script_pubkey")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ChannelInputValidationError(f"{field_name} must be a P2TR script_pubkey") from exc


def _validate_node_key(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 66 or value.lower() != value:
        raise ChannelInputValidationError(f"{field_name} must be a compressed secp256k1 public key")
    try:
        raw = bytes.fromhex(value)
        if raw[0] not in (2, 3):
            raise ValueError
        PublicKey(raw)
    except ValueError as exc:
        raise ChannelInputValidationError(
            f"{field_name} must be a compressed secp256k1 public key"
        ) from exc
