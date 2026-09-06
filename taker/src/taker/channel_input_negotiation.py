"""Durable pure coordinator negotiation for channel-input v1."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, TypeVar, cast

from bitcointx.core.key import CPubKey
from jmcore.channel_input import (
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
    ChannelInputModel,
    ChannelInputPartialPayload,
    ChannelInputPayload,
    ChannelInputPreparedPayload,
    ChannelInputPreparePayload,
    ChannelInputProposalPayload,
    ChannelInputRecoveryAcceptPayload,
    ChannelInputRecoveryConfirmPayload,
    ChannelInputRecoveryInitPayload,
    ChannelInputSignPayload,
    ChannelInputStatusPayload,
    ChannelInputStatusRequestPayload,
    ChannelInputTranscript,
    ChannelInputValidationError,
    EndpointTerms,
    Network,
    Outpoint,
    PolicyCeilings,
    approval_set_hash,
    cancellation_binding_hash,
    endpoint_terms_hash,
    sign_payload,
    transcript_hash,
    validate_cancel_ack,
    validate_complete_approval_set,
    validate_finalize_for_transcript,
    validate_finalized_for_transcript,
    validate_hello_for_invite,
    validate_hello_pair,
    validate_partial_for_transcript,
    validate_prepare_payload,
    validate_prepared_for_transcript,
    validate_proposal_for_hellos,
    validate_revealed_endpoint_pair,
    validate_sign_for_transcript,
    validate_status_for_transcript,
    validate_transcript_for_invite,
    verify_payload,
)
from jmcore.channel_input_session import ChannelInputActorRegistry, ChannelInputReplayLedger
from jmswap.lnd import (
    ChannelInputCandidate,
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
)
from pydantic import Field, field_serializer, field_validator, model_validator

_FROZEN_SNAPSHOT_BYTE_FIELDS = (
    "funding_scriptpubkey",
    "peer_node_pubkey",
    "local_funding_pubkey",
    "remote_funding_pubkey",
)


class TakerChannelInputNegotiationError(Exception):
    """The coordinator negotiation cannot safely advance."""


class TakerChannelInputStage(StrEnum):
    INVITED = "invited"
    FREEZE_INTENDED = "freeze_intended"
    HELLOS_READY = "hellos_ready"
    PROPOSALS_READY = "proposals_ready"
    APPROVAL_SET_READY = "approval_set_ready"
    PREPARE_READY = "prepare_ready"
    CANCEL_INTENDED = "cancel_intended"
    CANCELED = "canceled"


class TakerChannelInputFreezeIntent(ChannelInputModel):
    """Exact durable request facts for one selected maker's local LND freeze."""

    nick: str = Field(min_length=1)
    channel_id: str
    funding_outpoint: EscrowOutpoint
    peer_node_pubkey: str
    capacity_sat: int = Field(gt=0)
    settlement_script_pubkey: str
    quoted_miner_fee_share_sat: int = Field(ge=0)
    policy_ceilings: PolicyCeilings

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
    def validate_settlement_script(cls, value: str) -> str:
        _validate_p2tr_script(value, "settlement_script_pubkey")
        return value

    @model_validator(mode="after")
    def validate_intent(self) -> TakerChannelInputFreezeIntent:
        if self.channel_id != _outpoint_identifier(self.funding_outpoint):
            raise ValueError("channel_id must equal the funding outpoint")
        if self.quoted_miner_fee_share_sat > self.policy_ceilings.max_miner_fee_share_sat:
            raise ValueError("quoted fee share exceeds policy ceiling")
        return self


class TakerFrozenChannelInput(ChannelInputModel):
    """One exact LND freeze result, with JSON-safe byte serialization."""

    snapshot: FrozenChannelSnapshot

    @field_validator("snapshot", mode="before")
    @classmethod
    def parse_snapshot_json(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        converted = dict(value)
        for name in _FROZEN_SNAPSHOT_BYTE_FIELDS:
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
        for name in _FROZEN_SNAPSHOT_BYTE_FIELDS:
            data[name] = cast(bytes, data[name]).hex()
        return data


class TakerChannelInputParentChannelState(ChannelInputModel):
    """Durable post-prepare signing evidence for exactly one selected maker."""

    nick: str = Field(min_length=1)
    funding_outpoint: Outpoint
    local_attempt_id: str | None = Field(default=None, repr=False)
    local_prepared: ChannelInputPreparedPayload | None = None
    remote_prepared: ChannelInputPreparedPayload | None = None
    local_restart_intended: bool = False
    sign_request: ChannelInputSignPayload | None = None
    local_partial: ChannelInputPartialPayload | None = None
    remote_partial: ChannelInputPartialPayload | None = None
    finalize_request: ChannelInputFinalizePayload | None = None
    remote_finalized: ChannelInputFinalizedPayload | None = None
    status_request: ChannelInputStatusRequestPayload | None = None
    status_response: ChannelInputStatusPayload | None = None

    @field_validator("local_attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) != 64 or value.lower() != value:
            raise ValueError("local_attempt_id must be 32-byte lowercase hexadecimal")
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError("local_attempt_id must be 32-byte lowercase hexadecimal") from exc
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> TakerChannelInputParentChannelState:
        payloads = (
            self.local_prepared,
            self.remote_prepared,
            self.sign_request,
            self.local_partial,
            self.remote_partial,
            self.finalize_request,
            self.remote_finalized,
            self.status_request,
            self.status_response,
        )
        if any(
            payload is not None and payload.funding_outpoint != self.funding_outpoint
            for payload in payloads
        ):
            raise ValueError("parent channel state payload outpoint differs from state")
        if (self.local_attempt_id is None) != (self.local_prepared is None):
            raise ValueError("local signing attempt and prepared evidence must be paired")
        if self.sign_request is not None and (
            self.local_prepared is None or self.remote_prepared is None
        ):
            raise ValueError("sign request requires both prepared records")
        if self.local_partial is not None and self.sign_request is None:
            raise ValueError("local partial requires a sign request")
        if self.remote_partial is not None and self.sign_request is None:
            raise ValueError("remote partial requires a sign request")
        if self.finalize_request is not None and (
            self.local_partial is None or self.remote_partial is None
        ):
            raise ValueError("finalize request requires both partial records")
        if self.remote_finalized is not None and self.finalize_request is None:
            raise ValueError("remote finalized evidence requires a finalize request")
        if self.status_response is not None and self.status_request is None:
            raise ValueError("status response requires a status request")
        if self.local_restart_intended and any(
            value is not None
            for value in (
                self.sign_request,
                self.local_partial,
                self.remote_partial,
                self.finalize_request,
                self.remote_finalized,
            )
        ):
            raise ValueError("local restart intent conflicts with signing evidence")
        return self


class TakerChannelInputSnapshot(ChannelInputModel):
    """The complete signed coordinator state for a single protocol attempt."""

    version: Literal[1] = 1
    network: Network
    local_actor: str = Field(min_length=1)
    selected_nicks: tuple[str, ...] = Field(min_length=1, max_length=31)
    coordinator_secret_key_hex: str = Field(repr=False)
    stage: TakerChannelInputStage
    invite: ChannelInputInvitePayload
    remote_hellos: tuple[tuple[str, ChannelInputHelloPayload], ...] = ()
    local_hellos: tuple[tuple[str, ChannelInputHelloPayload], ...] = ()
    freeze_intents: tuple[tuple[str, TakerChannelInputFreezeIntent], ...] = ()
    frozen_inputs: tuple[tuple[str, TakerFrozenChannelInput], ...] = ()
    transcript: ChannelInputTranscript | None = None
    endpoint_terms: tuple[EndpointTerms, ...] = ()
    proposals: tuple[tuple[str, ChannelInputProposalPayload], ...] = ()
    local_approval: ChannelInputApprovalPayload | None = None
    maker_approvals: tuple[tuple[str, ChannelInputApprovalPayload], ...] = ()
    approval_set: ChannelInputApprovalSetPayload | None = None
    local_ack: ChannelInputApprovalSetAckPayload | None = None
    maker_acks: tuple[tuple[str, ChannelInputApprovalSetAckPayload], ...] = ()
    prepare: ChannelInputPreparePayload | None = None
    parent_channel_states: tuple[TakerChannelInputParentChannelState, ...] = ()
    cancel: ChannelInputCancelPayload | None = None
    cancel_acks: tuple[tuple[str, ChannelInputCancelAckPayload], ...] = ()
    local_resumed_outpoints: tuple[Outpoint, ...] = ()
    actor_registry_snapshot: tuple[tuple[str, str], ...]
    replay_ledger_snapshot: tuple[tuple[str, str], ...]

    @field_validator("coordinator_secret_key_hex")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        if not isinstance(value, str) or len(value) != 64 or value.lower() != value:
            raise ValueError("coordinator_secret_key_hex must be 32-byte lowercase hexadecimal")
        try:
            bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(
                "coordinator_secret_key_hex must be 32-byte lowercase hexadecimal"
            ) from exc
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> TakerChannelInputSnapshot:
        if tuple(sorted(self.selected_nicks)) != self.selected_nicks or len(
            set(self.selected_nicks)
        ) != len(self.selected_nicks):
            raise ValueError("selected_nicks must be unique and sorted")
        if self.local_actor in self.selected_nicks:
            raise ValueError("local_actor must not be selected")
        if self.invite.network != self.network:
            raise ValueError("snapshot network differs from invitation")
        if (
            tuple(sorted(self.parent_channel_states, key=lambda state: state.nick))
            != self.parent_channel_states
        ):
            raise ValueError("parent channel states must be nick-sorted")
        if len({state.nick for state in self.parent_channel_states}) != len(
            self.parent_channel_states
        ):
            raise ValueError("parent channel states must have unique nicks")
        if self.parent_channel_states and self.stage not in {
            TakerChannelInputStage.PREPARE_READY,
            TakerChannelInputStage.CANCEL_INTENDED,
            TakerChannelInputStage.CANCELED,
        }:
            raise ValueError("parent channel states require prepare-ready or cancellation stage")
        if self.stage in {
            TakerChannelInputStage.CANCEL_INTENDED,
            TakerChannelInputStage.CANCELED,
        } and any(
            state.local_partial is not None
            or state.remote_partial is not None
            or state.finalize_request is not None
            or state.remote_finalized is not None
            for state in self.parent_channel_states
        ):
            raise ValueError("canceled snapshot contains partial or final parent evidence")
        return self


class TakerChannelInputNegotiation:
    """Coordinator-owned negotiation through prepare authorization or cancellation."""

    def __init__(
        self,
        *,
        network: Network,
        local_actor: str,
        selected_nicks: tuple[str, ...],
        key_pair: ChannelInputKeyPair,
        invite: ChannelInputInvitePayload,
        registry: ChannelInputActorRegistry,
        ledger: ChannelInputReplayLedger,
        stage: TakerChannelInputStage = TakerChannelInputStage.INVITED,
        remote_hellos: tuple[tuple[str, ChannelInputHelloPayload], ...] = (),
        local_hellos: tuple[tuple[str, ChannelInputHelloPayload], ...] = (),
        freeze_intents: tuple[tuple[str, TakerChannelInputFreezeIntent], ...] = (),
        frozen_inputs: tuple[tuple[str, TakerFrozenChannelInput], ...] = (),
        transcript: ChannelInputTranscript | None = None,
        endpoint_terms: tuple[EndpointTerms, ...] = (),
        proposals: tuple[tuple[str, ChannelInputProposalPayload], ...] = (),
        local_approval: ChannelInputApprovalPayload | None = None,
        maker_approvals: tuple[tuple[str, ChannelInputApprovalPayload], ...] = (),
        approval_set: ChannelInputApprovalSetPayload | None = None,
        local_ack: ChannelInputApprovalSetAckPayload | None = None,
        maker_acks: tuple[tuple[str, ChannelInputApprovalSetAckPayload], ...] = (),
        prepare: ChannelInputPreparePayload | None = None,
        parent_channel_states: tuple[TakerChannelInputParentChannelState, ...] = (),
        cancel: ChannelInputCancelPayload | None = None,
        cancel_acks: tuple[tuple[str, ChannelInputCancelAckPayload], ...] = (),
        local_resumed_outpoints: tuple[Outpoint, ...] = (),
    ) -> None:
        self.network = network
        self.local_actor = local_actor
        self.selected_nicks = selected_nicks
        self._key_pair = key_pair
        self.invite = invite
        self._registry = registry
        self._ledger = ledger
        self.stage = stage
        self.remote_hellos = remote_hellos
        self.local_hellos = local_hellos
        self.freeze_intents = freeze_intents
        self.frozen_inputs = frozen_inputs
        self.transcript = transcript
        self.endpoint_terms = endpoint_terms
        self.proposals = proposals
        self.local_approval = local_approval
        self.maker_approvals = maker_approvals
        self.approval_set = approval_set
        self.local_ack = local_ack
        self.maker_acks = maker_acks
        self.prepare = prepare
        self.parent_channel_states = parent_channel_states
        self.cancel = cancel
        self.cancel_acks = cancel_acks
        self.local_resumed_outpoints = local_resumed_outpoints

    @classmethod
    def create(
        cls,
        *,
        network: Network,
        local_actor: str,
        selected_nicks: Sequence[str],
        secret_key: bytes,
        session_id: str,
        attempt: int,
        expiry: int,
        max_parent_fee_sat: int,
        max_fee_rate_sat_vb: int,
        min_recovery_delta: int,
        max_recovery_delta: int,
    ) -> TakerChannelInputNegotiation:
        nicks = _selected_nicks(selected_nicks, local_actor)
        key_pair = ChannelInputKeyPair.from_secret(secret_key)
        invite = sign_payload(
            ChannelInputInvitePayload(
                session_id=session_id,
                attempt=attempt,
                signer_key=key_pair.public_key,
                network=network,
                expiry=expiry,
                max_parent_fee_sat=max_parent_fee_sat,
                max_fee_rate_sat_vb=max_fee_rate_sat_vb,
                min_recovery_delta=min_recovery_delta,
                max_recovery_delta=max_recovery_delta,
            ),
            secret_key,
        )
        registry = ChannelInputActorRegistry()
        registry.bind(local_actor, key_pair.public_key)
        ledger = ChannelInputReplayLedger()
        _record(ledger, _slot("invite"), invite)
        return cls(
            network=network,
            local_actor=local_actor,
            selected_nicks=nicks,
            key_pair=key_pair,
            invite=invite,
            registry=registry,
            ledger=ledger,
        )

    @property
    def coordinator_key(self) -> str:
        return self._key_pair.public_key

    def sign_recovery_init(
        self, payload: ChannelInputRecoveryInitPayload
    ) -> ChannelInputRecoveryInitPayload:
        """Sign a process-local recovery request with the durable coordinator key."""

        if payload.signer_key != self.coordinator_key:
            raise ChannelInputValidationError("recovery request signer differs from coordinator")
        return sign_payload(payload, self._key_pair.secret_key)

    def sign_recovery_confirm(
        self, payload: ChannelInputRecoveryConfirmPayload
    ) -> ChannelInputRecoveryConfirmPayload:
        """Sign the encrypted recovery proof with the durable coordinator key."""

        if payload.signer_key != self.coordinator_key:
            raise ChannelInputValidationError("recovery confirm signer differs from coordinator")
        return sign_payload(payload, self._key_pair.secret_key)

    def require_recovery_accept(
        self, nick: str, payload: ChannelInputRecoveryAcceptPayload
    ) -> None:
        """Require a recovery response from the selected maker's durable signer."""

        self._require_nick(nick)
        self._registry.require_verified(nick, payload)

    def snapshot(self) -> TakerChannelInputSnapshot:
        return TakerChannelInputSnapshot(
            network=self.network,
            local_actor=self.local_actor,
            selected_nicks=self.selected_nicks,
            coordinator_secret_key_hex=self._key_pair.secret_key.hex(),
            stage=self.stage,
            invite=self.invite,
            remote_hellos=self.remote_hellos,
            local_hellos=self.local_hellos,
            freeze_intents=self.freeze_intents,
            frozen_inputs=self.frozen_inputs,
            transcript=self.transcript,
            endpoint_terms=self.endpoint_terms,
            proposals=self.proposals,
            local_approval=self.local_approval,
            maker_approvals=self.maker_approvals,
            approval_set=self.approval_set,
            local_ack=self.local_ack,
            maker_acks=self.maker_acks,
            prepare=self.prepare,
            parent_channel_states=self.parent_channel_states,
            cancel=self.cancel,
            cancel_acks=self.cancel_acks,
            local_resumed_outpoints=self.local_resumed_outpoints,
            actor_registry_snapshot=self._registry.snapshot(),
            replay_ledger_snapshot=self._ledger.snapshot(),
        )

    @classmethod
    def from_snapshot(cls, snapshot: TakerChannelInputSnapshot) -> TakerChannelInputNegotiation:
        snapshot = TakerChannelInputSnapshot.model_validate(snapshot.model_dump(mode="python"))
        try:
            key_pair = ChannelInputKeyPair.from_secret(
                bytes.fromhex(snapshot.coordinator_secret_key_hex)
            )
            registry = ChannelInputActorRegistry.from_snapshot(snapshot.actor_registry_snapshot)
            ledger = ChannelInputReplayLedger.from_snapshot(snapshot.replay_ledger_snapshot)
        except (TypeError, ValueError) as exc:
            raise TakerChannelInputNegotiationError("snapshot credentials are invalid") from exc
        result = cls(
            network=snapshot.network,
            local_actor=snapshot.local_actor,
            selected_nicks=snapshot.selected_nicks,
            key_pair=key_pair,
            invite=snapshot.invite,
            registry=registry,
            ledger=ledger,
            stage=snapshot.stage,
            remote_hellos=snapshot.remote_hellos,
            local_hellos=snapshot.local_hellos,
            freeze_intents=snapshot.freeze_intents,
            frozen_inputs=snapshot.frozen_inputs,
            transcript=snapshot.transcript,
            endpoint_terms=snapshot.endpoint_terms,
            proposals=snapshot.proposals,
            local_approval=snapshot.local_approval,
            maker_approvals=snapshot.maker_approvals,
            approval_set=snapshot.approval_set,
            local_ack=snapshot.local_ack,
            maker_acks=snapshot.maker_acks,
            prepare=snapshot.prepare,
            parent_channel_states=snapshot.parent_channel_states,
            cancel=snapshot.cancel,
            cancel_acks=snapshot.cancel_acks,
            local_resumed_outpoints=snapshot.local_resumed_outpoints,
        )
        result._validate_restored_state()
        return result

    def accept_hello(
        self,
        nick: str,
        remote_hello: ChannelInputHelloPayload,
        local_hello: ChannelInputHelloPayload,
    ) -> None:
        self._require_nick(nick)
        existing_remote = dict(self.remote_hellos).get(nick)
        existing_local = dict(self.local_hellos).get(nick)
        if existing_remote is not None or existing_local is not None:
            if (existing_remote, existing_local) != (remote_hello, local_hello):
                raise ChannelInputValidationError("hello conflicts with durable hello pair")
            return
        if self.stage is not TakerChannelInputStage.INVITED:
            raise ChannelInputValidationError("hello is not valid at this negotiation stage")
        validate_hello_for_invite(self.invite, remote_hello)
        validate_hello_for_invite(self.invite, local_hello)
        if (
            remote_hello.participant_key == self.coordinator_key
            or local_hello.participant_key != self.coordinator_key
        ):
            raise ChannelInputValidationError(
                "hello participant keys do not identify the coordinator pair"
            )
        if (
            remote_hello.signer_key != remote_hello.participant_key
            or local_hello.signer_key != self.coordinator_key
        ):
            raise ChannelInputValidationError("hello signing keys do not match participants")
        if remote_hello.participant_key in {item.participant_key for _, item in self.remote_hellos}:
            raise ChannelInputValidationError("maker participant key is already assigned")
        if remote_hello.funding_outpoint in {
            item.funding_outpoint for _, item in self.remote_hellos
        }:
            raise ChannelInputValidationError("channel funding outpoint is already assigned")
        validate_hello_pair(remote_hello, local_hello)
        self._registry.bind_verified(nick, remote_hello)
        self._registry.require_verified(self.local_actor, local_hello)
        _record(self._ledger, _slot("hello_remote", nick), remote_hello)
        _record(self._ledger, _slot("hello_local", nick), local_hello)
        self.remote_hellos = _nick_items((*self.remote_hellos, (nick, remote_hello)))
        self.local_hellos = _nick_items((*self.local_hellos, (nick, local_hello)))
        if len(self.remote_hellos) == len(self.selected_nicks):
            self.stage = TakerChannelInputStage.HELLOS_READY

    def record_freeze_intent(
        self,
        nick: str,
        remote_hello: ChannelInputHelloPayload,
        candidate: ChannelInputCandidate,
        *,
        local_node_key: str,
        settlement_script_pubkey: str,
        quoted_miner_fee_share_sat: int,
        policy_ceilings: PolicyCeilings,
    ) -> TakerChannelInputFreezeIntent:
        """Authenticate a maker hello and durably plan its local LND freeze request."""

        self._require_nick(nick)
        intent = _freeze_intent_from_candidate(
            nick,
            candidate,
            settlement_script_pubkey=settlement_script_pubkey,
            quoted_miner_fee_share_sat=quoted_miner_fee_share_sat,
            policy_ceilings=policy_ceilings,
        )
        if intent.quoted_miner_fee_share_sat > self.invite.max_parent_fee_sat:
            raise ChannelInputValidationError(
                "freeze intent quoted fee exceeds invitation parent fee"
            )
        existing_intent = dict(self.freeze_intents).get(nick)
        existing_remote = dict(self.remote_hellos).get(nick)
        if existing_intent is not None:
            if existing_intent != intent or existing_remote != remote_hello:
                raise ChannelInputValidationError("freeze intent conflicts with durable evidence")
            return existing_intent
        if self.stage not in {
            TakerChannelInputStage.INVITED,
            TakerChannelInputStage.FREEZE_INTENDED,
        }:
            raise ChannelInputValidationError(
                "freeze intent is not valid at this negotiation stage"
            )
        if existing_remote is not None:
            if existing_remote != remote_hello:
                raise ChannelInputValidationError("remote hello conflicts with durable evidence")
        if nick in dict(self.local_hellos):
            raise ChannelInputValidationError(
                "local hello cannot exist before remote freeze evidence"
            )
        self._validate_remote_freeze_hello(nick, remote_hello, candidate, local_node_key)
        if any(
            other.funding_outpoint == intent.funding_outpoint
            for other_nick, other in self.freeze_intents
            if other_nick != nick
        ):
            raise ChannelInputValidationError("channel funding outpoint is already assigned")
        self._registry.bind_verified(nick, remote_hello)
        if existing_remote is None:
            _record(self._ledger, _slot("hello_remote", nick), remote_hello)
            self.remote_hellos = _nick_items((*self.remote_hellos, (nick, remote_hello)))
        self.freeze_intents = _nick_items((*self.freeze_intents, (nick, intent)))
        self.stage = TakerChannelInputStage.FREEZE_INTENDED
        return intent

    def install_frozen_channel(
        self, nick: str, snapshot: FrozenChannelSnapshot
    ) -> ChannelInputHelloPayload:
        """Install one exact LND freeze result and create the signed local hello."""

        self._require_nick(nick)
        frozen = _normalize_frozen_input(snapshot)
        existing = dict(self.frozen_inputs).get(nick)
        if existing is not None:
            if existing != frozen:
                raise ChannelInputValidationError("frozen channel conflicts with durable evidence")
            local = dict(self.local_hellos).get(nick)
            if local is None:
                raise ChannelInputValidationError("frozen channel lacks durable local hello")
            return local
        if self.stage is not TakerChannelInputStage.FREEZE_INTENDED:
            raise ChannelInputValidationError(
                "frozen channel is not valid at this negotiation stage"
            )
        intent = dict(self.freeze_intents).get(nick)
        remote = dict(self.remote_hellos).get(nick)
        if intent is None or remote is None:
            raise ChannelInputValidationError("frozen channel requires intent and remote hello")
        if nick in dict(self.local_hellos):
            raise ChannelInputValidationError("frozen channel conflicts with durable local hello")
        self._validate_frozen_channel(nick, frozen, intent, remote)
        signed = self._local_hello_from_frozen(remote, intent, frozen)
        validate_hello_for_invite(self.invite, signed)
        validate_hello_pair(remote, signed)
        self._registry.require_verified(self.local_actor, signed)
        _record(self._ledger, _slot("hello_local", nick), signed)
        self.local_hellos = _nick_items((*self.local_hellos, (nick, signed)))
        self.frozen_inputs = _nick_items((*self.frozen_inputs, (nick, frozen)))
        if (
            len(self.freeze_intents) == len(self.selected_nicks)
            and len(self.frozen_inputs) == len(self.selected_nicks)
            and len(self.remote_hellos) == len(self.selected_nicks)
            and len(self.local_hellos) == len(self.selected_nicks)
        ):
            self.stage = TakerChannelInputStage.HELLOS_READY
        return signed

    def plan_proposals(
        self,
        transcript: ChannelInputTranscript,
        endpoint_terms: Sequence[EndpointTerms],
        *,
        current_time: int,
        current_height: int,
    ) -> tuple[tuple[str, ChannelInputProposalPayload], ...]:
        terms = _ordered_terms(endpoint_terms)
        if self.proposals:
            if self.transcript != transcript or self.endpoint_terms != terms:
                raise ChannelInputValidationError(
                    "proposal plan conflicts with durable transcript or terms"
                )
            return self.proposals
        if self.stage is not TakerChannelInputStage.HELLOS_READY:
            raise ChannelInputValidationError("proposals are not valid at this negotiation stage")
        self._validate_plan(
            transcript, terms, current_time=current_time, current_height=current_height
        )
        proposals: list[tuple[str, ChannelInputProposalPayload]] = []
        for nick, hello in self.remote_hellos:
            recipient_terms = tuple(
                item for item in terms if item.participant_key == hello.participant_key
            )
            proposal = ChannelInputProposalPayload(
                session_id=self.invite.session_id,
                attempt=self.invite.attempt,
                signer_key=self.coordinator_key,
                transcript=transcript,
                transcript_hash=transcript_hash(transcript).hex(),
                recipient_key=hello.participant_key,
                recipient_terms=recipient_terms,
            )
            signed = sign_payload(proposal, self._key_pair.secret_key)
            validate_proposal_for_hellos(
                self.invite,
                (hello,),
                signed,
                current_time=current_time,
                current_height=current_height,
            )
            self._registry.require_verified(self.local_actor, signed)
            _record(self._ledger, _slot("proposal", nick), signed)
            proposals.append((nick, signed))
        local = ChannelInputApprovalPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=transcript_hash(transcript).hex(),
            terms_hashes=tuple(sorted(transcript.terms_hashes_for(self.coordinator_key))),
        )
        signed_local = sign_payload(local, self._key_pair.secret_key)
        self._registry.require_verified(self.local_actor, signed_local)
        _record(self._ledger, _slot("approval_local"), signed_local)
        self.transcript = transcript
        self.endpoint_terms = terms
        self.proposals = tuple(proposals)
        self.local_approval = signed_local
        self.stage = TakerChannelInputStage.PROPOSALS_READY
        return self.proposals

    def accept_approval(self, nick: str, approval: ChannelInputApprovalPayload) -> None:
        self._require_nick(nick)
        existing = dict(self.maker_approvals).get(nick)
        if existing is not None:
            if existing != approval:
                raise ChannelInputValidationError("approval conflicts with durable approval")
            return
        if self.stage is not TakerChannelInputStage.PROPOSALS_READY:
            raise ChannelInputValidationError("approval is not valid at this negotiation stage")
        assert self.transcript is not None
        self._registry.require_verified(nick, approval)
        proposal = dict(self.proposals)[nick]
        if (
            approval.session_id != self.invite.session_id
            or approval.attempt != self.invite.attempt
            or approval.transcript_hash != transcript_hash(self.transcript).hex()
            or set(approval.terms_hashes)
            != self.transcript.terms_hashes_for(proposal.recipient_key)
        ):
            raise ChannelInputValidationError("approval does not bind the assigned proposal terms")
        _record(self._ledger, _slot("approval", nick), approval)
        self.maker_approvals = _nick_items((*self.maker_approvals, (nick, approval)))

    def build_approval_set(self) -> ChannelInputApprovalSetPayload:
        if self.approval_set is not None:
            return self.approval_set
        if self.stage is not TakerChannelInputStage.PROPOSALS_READY or len(
            self.maker_approvals
        ) != len(self.selected_nicks):
            raise ChannelInputValidationError("approval set requires every maker approval")
        assert self.transcript is not None and self.local_approval is not None
        approvals = tuple(
            sorted(
                (self.local_approval, *(item for _, item in self.maker_approvals)),
                key=lambda x: x.signer_key,
            )
        )
        approval_set = ChannelInputApprovalSetPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript=self.transcript,
            approvals=approvals,
        )
        signed = sign_payload(approval_set, self._key_pair.secret_key)
        validate_complete_approval_set(signed)
        ack = ChannelInputApprovalSetAckPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=transcript_hash(self.transcript).hex(),
            approval_set_hash=approval_set_hash(signed).hex(),
        )
        signed_ack = sign_payload(ack, self._key_pair.secret_key)
        self._registry.require_verified(self.local_actor, signed)
        self._registry.require_verified(self.local_actor, signed_ack)
        _record(self._ledger, _slot("approval_set"), signed)
        _record(self._ledger, _slot("approval_ack_local"), signed_ack)
        self.approval_set, self.local_ack = signed, signed_ack
        self.stage = TakerChannelInputStage.APPROVAL_SET_READY
        return signed

    def accept_approval_ack(self, nick: str, ack: ChannelInputApprovalSetAckPayload) -> None:
        self._require_nick(nick)
        existing = dict(self.maker_acks).get(nick)
        if existing is not None:
            if existing != ack:
                raise ChannelInputValidationError("approval ACK conflicts with durable ACK")
            return
        if self.stage is not TakerChannelInputStage.APPROVAL_SET_READY:
            raise ChannelInputValidationError("approval ACK is not valid at this negotiation stage")
        assert self.transcript is not None and self.approval_set is not None
        self._registry.require_verified(nick, ack)
        if (
            ack.session_id != self.invite.session_id
            or ack.attempt != self.invite.attempt
            or ack.transcript_hash != transcript_hash(self.transcript).hex()
            or ack.approval_set_hash != approval_set_hash(self.approval_set).hex()
            or ack.signer_key != dict(self.remote_hellos)[nick].participant_key
        ):
            raise ChannelInputValidationError("approval ACK does not bind the exact approval set")
        _record(self._ledger, _slot("approval_ack", nick), ack)
        self.maker_acks = _nick_items((*self.maker_acks, (nick, ack)))

    def build_prepare(self) -> ChannelInputPreparePayload:
        if self.prepare is not None:
            return self.prepare
        if self.stage is not TakerChannelInputStage.APPROVAL_SET_READY or len(
            self.maker_acks
        ) != len(self.selected_nicks):
            raise ChannelInputValidationError("prepare requires every maker approval ACK")
        assert (
            self.transcript is not None
            and self.approval_set is not None
            and self.local_ack is not None
        )
        prepare = ChannelInputPreparePayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=transcript_hash(self.transcript).hex(),
            approval_set_hash=approval_set_hash(self.approval_set).hex(),
            approval_set=self.approval_set,
            approval_set_acks=tuple(
                sorted(
                    (self.local_ack, *(item for _, item in self.maker_acks)),
                    key=lambda x: x.signer_key,
                )
            ),
        )
        signed = sign_payload(prepare, self._key_pair.secret_key)
        validate_prepare_payload(signed)
        self._registry.require_verified(self.local_actor, signed)
        _record(self._ledger, _slot("prepare"), signed)
        self.prepare = signed
        self.stage = TakerChannelInputStage.PREPARE_READY
        return signed

    def accept_remote_prepared(self, nick: str, payload: ChannelInputPreparedPayload) -> None:
        """Accept one maker's signed nonce after the complete prepare authorization."""

        self._require_post_prepare(nick)
        state = self._state_for_nick(nick)
        if state.remote_prepared is not None:
            if state.remote_prepared != payload:
                raise ChannelInputValidationError(
                    "remote prepared conflicts with durable channel state"
                )
            return
        self._validate_remote_prepared(nick, payload)
        self._replace_parent_channel_state(state.model_copy(update={"remote_prepared": payload}))

    def record_local_prepared(
        self, nick: str, attempt: LndEscrowSigningAttempt
    ) -> ChannelInputPreparedPayload:
        """Sign and retain the local LND nonce before exposing it to a maker."""

        self._require_post_prepare(nick)
        attempt = _validated_signing_attempt(attempt)
        state = self._state_for_nick(nick)
        if state.local_prepared is not None:
            if (
                state.local_attempt_id == attempt.attempt_id.hex()
                and state.local_prepared.public_nonce == attempt.public_nonce.hex()
            ):
                return state.local_prepared
            if not state.local_restart_intended:
                raise ChannelInputValidationError(
                    "local prepared conflicts with durable channel state"
                )
            self._require_unconsumed_local_prepared(state)
            if state.local_attempt_id == attempt.attempt_id.hex():
                raise ChannelInputValidationError(
                    "local prepared rotation requires a new LND attempt"
                )
        prepared = self._sign_local_prepared(nick, attempt)
        self._replace_parent_channel_state(
            state.model_copy(
                update={
                    "local_attempt_id": attempt.attempt_id.hex(),
                    "local_prepared": prepared,
                    "local_restart_intended": False,
                }
            )
        )
        return prepared

    def plan_local_prepared_rotation(self, nick: str) -> None:
        """Persist intent to replace one unconsumed local LND signing attempt."""

        self._require_post_prepare(nick)
        state = self._state_for_nick(nick)
        if state.local_prepared is None:
            raise ChannelInputValidationError(
                "local prepared rotation requires local prepared evidence"
            )
        self._require_unconsumed_local_prepared(state)
        if not state.local_restart_intended:
            self._replace_parent_channel_state(
                state.model_copy(update={"local_restart_intended": True})
            )

    def build_sign_request(self, nick: str) -> ChannelInputSignPayload:
        """Build the exact coordinator signing authorization for one maker channel."""

        self._require_post_prepare(nick)
        state = self._state_for_nick(nick)
        if state.sign_request is not None:
            return state.sign_request
        if (
            state.local_prepared is None
            or state.remote_prepared is None
            or state.local_restart_intended
        ):
            raise ChannelInputValidationError(
                "sign request requires stable local and remote prepared evidence"
            )
        request = ChannelInputSignPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=self._transcript_hash(),
            funding_outpoint=state.funding_outpoint,
            local_public_nonce=state.remote_prepared.public_nonce,
            peer_prepared=state.local_prepared,
        )
        signed = sign_payload(request, self._key_pair.secret_key)
        self._validate_sign_request(nick, signed, state)
        self._replace_parent_channel_state(state.model_copy(update={"sign_request": signed}))
        return signed

    def accept_remote_partial(self, nick: str, payload: ChannelInputPartialPayload) -> None:
        """Accept exactly one maker partial signature with its ordered nonce pair."""

        self._require_post_prepare(nick)
        state = self._state_for_nick(nick)
        if state.remote_partial is not None:
            if state.remote_partial != payload:
                raise ChannelInputValidationError(
                    "remote partial conflicts with durable channel state"
                )
            return
        self._validate_remote_partial(nick, payload, state)
        self._replace_parent_channel_state(state.model_copy(update={"remote_partial": payload}))

    def record_local_partial(
        self, nick: str, partial_signature: bytes
    ) -> ChannelInputPartialPayload:
        """Sign and retain the coordinator partial after its sign request is durable."""

        self._require_post_prepare(nick)
        if not isinstance(partial_signature, bytes) or len(partial_signature) != 32:
            raise ChannelInputValidationError("partial signature must be exactly 32 bytes")
        state = self._state_for_nick(nick)
        if state.local_partial is not None:
            if state.local_partial.partial_signature == partial_signature.hex():
                return state.local_partial
            raise ChannelInputValidationError("local partial conflicts with durable channel state")
        if (
            state.sign_request is None
            or state.local_prepared is None
            or state.remote_prepared is None
        ):
            raise ChannelInputValidationError(
                "local partial requires durable sign and prepared evidence"
            )
        partial = ChannelInputPartialPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=self._transcript_hash(),
            funding_outpoint=state.funding_outpoint,
            local_public_nonce=state.local_prepared.public_nonce,
            peer_public_nonce=state.remote_prepared.public_nonce,
            partial_signature=partial_signature.hex(),
        )
        signed = sign_payload(partial, self._key_pair.secret_key)
        self._validate_local_partial(signed, state)
        self._replace_parent_channel_state(state.model_copy(update={"local_partial": signed}))
        return signed

    def build_finalize_request(self, nick: str) -> ChannelInputFinalizePayload:
        """Build the exact coordinator finalization authorization for one maker channel."""

        self._require_post_prepare(nick)
        state = self._state_for_nick(nick)
        if state.finalize_request is not None:
            return state.finalize_request
        if (
            state.local_prepared is None
            or state.remote_prepared is None
            or state.local_partial is None
            or state.remote_partial is None
        ):
            raise ChannelInputValidationError(
                "finalize request requires both prepared and partial records"
            )
        request = ChannelInputFinalizePayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=self._transcript_hash(),
            funding_outpoint=state.funding_outpoint,
            local_public_nonce=state.remote_prepared.public_nonce,
            peer_partial=state.local_partial,
        )
        signed = sign_payload(request, self._key_pair.secret_key)
        self._validate_finalize_request(nick, signed, state)
        self._replace_parent_channel_state(state.model_copy(update={"finalize_request": signed}))
        return signed

    def accept_remote_finalized(self, nick: str, payload: ChannelInputFinalizedPayload) -> None:
        """Accept exact maker final-parent evidence after the finalize authorization."""

        self._require_post_prepare(nick)
        state = self._state_for_nick(nick)
        if state.remote_finalized is not None:
            if state.remote_finalized != payload:
                raise ChannelInputValidationError(
                    "remote finalized conflicts with durable channel state"
                )
            return
        if state.finalize_request is None:
            raise ChannelInputValidationError(
                "remote finalized requires a durable finalize request"
            )
        self._registry.require_verified(nick, payload)
        assert self.transcript is not None
        validate_finalized_for_transcript(payload, self.transcript)
        if (
            payload.signer_key != self._maker_key(nick)
            or payload.funding_outpoint != state.funding_outpoint
        ):
            raise ChannelInputValidationError(
                "remote finalized differs from maker channel identity"
            )
        self._replace_parent_channel_state(state.model_copy(update={"remote_finalized": payload}))

    def build_status_request(self, nick: str) -> ChannelInputStatusRequestPayload:
        """Persist a fresh query before returning it, or replay an unanswered query exactly."""

        self._require_post_prepare(nick)
        state = self._state_for_nick(nick)
        if state.status_request is not None and state.status_response is None:
            return state.status_request
        request = ChannelInputStatusRequestPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=self._transcript_hash(),
            funding_outpoint=state.funding_outpoint,
            query_id=secrets.token_bytes(32).hex(),
        )
        signed = sign_payload(request, self._key_pair.secret_key)
        self._registry.require_verified(self.local_actor, signed)
        self._replace_parent_channel_state(
            state.model_copy(update={"status_request": signed, "status_response": None})
        )
        return signed

    def accept_remote_status(self, nick: str, response: ChannelInputStatusPayload) -> None:
        """Merge one exact signed maker status without regressing stronger evidence."""

        self._require_post_prepare(nick)
        state = self._state_for_nick(nick)
        if state.status_response is not None:
            if state.status_response == response:
                return
            raise ChannelInputValidationError(
                "status response conflicts with durable channel state"
            )
        if state.status_request is None:
            raise ChannelInputValidationError("status response requires an exact pending request")
        assert self.transcript is not None
        self._registry.require_verified(nick, response)
        validate_status_for_transcript(
            response, state.status_request, self.transcript, self._maker_key(nick)
        )
        if response.prepared.funding_outpoint != state.funding_outpoint:
            raise ChannelInputValidationError("status prepared differs from maker channel")
        replacement = state
        if state.remote_prepared is None:
            self._validate_remote_prepared(nick, response.prepared)
            replacement = replacement.model_copy(update={"remote_prepared": response.prepared})
        elif state.remote_prepared != response.prepared:
            if response.escrow_stage != "parent_prepared" or self._has_sign_or_stronger_evidence(
                state
            ):
                raise ChannelInputValidationError(
                    "status prepared conflicts with durable signing evidence"
                )
            self._validate_remote_prepared(nick, response.prepared)
            replacement = replacement.model_copy(update={"remote_prepared": response.prepared})
        if replacement.local_prepared is not None and response.partial is not None:
            if response.partial.peer_public_nonce != replacement.local_prepared.public_nonce:
                raise ChannelInputValidationError(
                    "status partial peer nonce differs from local prepared"
                )
        replacement = self._merge_status_evidence(nick, response, replacement)
        self._replace_parent_channel_state(
            replacement.model_copy(update={"status_response": response})
        )

    def plan_cancel(self, reason_code: str) -> ChannelInputCancelPayload:
        if self.cancel is not None:
            if self.cancel.reason_code != reason_code:
                raise ChannelInputValidationError(
                    "cancel reason conflicts with durable cancellation"
                )
            return self.cancel
        if self.stage not in {
            TakerChannelInputStage.INVITED,
            TakerChannelInputStage.FREEZE_INTENDED,
            TakerChannelInputStage.HELLOS_READY,
            TakerChannelInputStage.PROPOSALS_READY,
            TakerChannelInputStage.APPROVAL_SET_READY,
            TakerChannelInputStage.PREPARE_READY,
        }:
            raise ChannelInputValidationError("cancel is not valid at this negotiation stage")
        if self._has_partial_or_final_evidence():
            raise ChannelInputValidationError(
                "cancel conflicts with durable partial or final evidence"
            )
        cancel = ChannelInputCancelPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=cancellation_binding_hash(self.invite, self.transcript).hex(),
            reason_code=reason_code,
            partial_released=False,
        )
        signed = sign_payload(cancel, self._key_pair.secret_key)
        self._registry.require_verified(self.local_actor, signed)
        _record(self._ledger, _slot("cancel"), signed)
        self.cancel = signed
        self.stage = TakerChannelInputStage.CANCEL_INTENDED
        return signed

    def accept_cancel_ack(self, nick: str, ack: ChannelInputCancelAckPayload) -> None:
        self._require_nick(nick)
        existing = dict(self.cancel_acks).get(nick)
        if existing is not None:
            if existing != ack:
                raise ChannelInputValidationError("cancel ACK conflicts with durable ACK")
            return
        if self.cancel is None or self.stage is not TakerChannelInputStage.CANCEL_INTENDED:
            raise ChannelInputValidationError("cancel ACK requires cancellation intent")
        remote_hello = dict(self.remote_hellos).get(nick)
        updated_registry: ChannelInputActorRegistry | None = None
        if remote_hello is None:
            if len(ack.resumed_outpoints) != 1:
                raise ChannelInputValidationError(
                    "cancel ACK without a durable hello requires exactly one resumed outpoint"
                )
            validate_cancel_ack(self.cancel, ack, ack.signer_key, ack.resumed_outpoints)
            self._require_unique_cancel_ack_outpoints(ack)
            updated_registry = ChannelInputActorRegistry.from_snapshot(self._registry.snapshot())
            updated_registry.bind_verified(nick, ack)
        else:
            self._registry.require_verified(nick, ack)
            validate_cancel_ack(
                self.cancel,
                ack,
                remote_hello.participant_key,
                (remote_hello.funding_outpoint,),
            )
            self._require_unique_cancel_ack_outpoints(ack)
        _record(self._ledger, _slot("cancel_ack", nick), ack)
        if updated_registry is not None:
            self._registry = updated_registry
        self.cancel_acks = _nick_items((*self.cancel_acks, (nick, ack)))

    def record_local_resumed(self, outpoints: Sequence[Outpoint]) -> None:
        expected = self._expected_local_resumed_outpoints()
        resumed = tuple(outpoints)
        if resumed != expected:
            raise ChannelInputValidationError(
                "local resumed outpoints differ from local hello outpoints"
            )
        if len(self.cancel_acks) != len(self.selected_nicks):
            raise ChannelInputValidationError("local resume requires every maker cancel ACK")
        if self.stage is TakerChannelInputStage.CANCELED:
            if self.local_resumed_outpoints != resumed:
                raise ChannelInputValidationError(
                    "local resumed outpoints conflict with durable evidence"
                )
            return
        if self.stage is not TakerChannelInputStage.CANCEL_INTENDED:
            raise ChannelInputValidationError("local resume is not valid at this negotiation stage")
        self.local_resumed_outpoints = resumed
        self.stage = TakerChannelInputStage.CANCELED

    def _require_post_prepare(self, nick: str) -> None:
        self._require_nick(nick)
        if self.stage is not TakerChannelInputStage.PREPARE_READY or self.prepare is None:
            raise ChannelInputValidationError(
                "post-prepare channel state requires accepted prepare"
            )
        if self.prepare != self._exact_prepare():
            raise ChannelInputValidationError(
                "post-prepare channel state requires exact accepted prepare"
            )

    def _exact_prepare(self) -> ChannelInputPreparePayload:
        assert self.transcript is not None and self.approval_set is not None
        assert self.local_ack is not None
        expected_acks = tuple(
            sorted(
                (self.local_ack, *(ack for _, ack in self.maker_acks)),
                key=lambda item: item.signer_key,
            )
        )
        return ChannelInputPreparePayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=transcript_hash(self.transcript).hex(),
            approval_set_hash=approval_set_hash(self.approval_set).hex(),
            approval_set=self.approval_set,
            approval_set_acks=expected_acks,
            sig=self.prepare.sig if self.prepare is not None else "00" * 64,
        )

    def _state_for_nick(self, nick: str) -> TakerChannelInputParentChannelState:
        self._require_nick(nick)
        existing = next((state for state in self.parent_channel_states if state.nick == nick), None)
        if existing is not None:
            return existing
        remote = dict(self.remote_hellos).get(nick)
        if remote is None:
            raise ChannelInputValidationError("parent channel state requires accepted maker hello")
        return TakerChannelInputParentChannelState(
            nick=nick, funding_outpoint=remote.funding_outpoint
        )

    def _replace_parent_channel_state(
        self, replacement: TakerChannelInputParentChannelState
    ) -> None:
        states = [
            replacement if state.nick == replacement.nick else state
            for state in self.parent_channel_states
        ]
        if not any(state.nick == replacement.nick for state in self.parent_channel_states):
            states.append(replacement)
        self.parent_channel_states = tuple(sorted(states, key=lambda state: state.nick))

    def _maker_key(self, nick: str) -> str:
        try:
            return dict(self.remote_hellos)[nick].participant_key
        except KeyError as exc:
            raise ChannelInputValidationError("maker hello is absent from negotiation") from exc

    def _transcript_hash(self) -> str:
        assert self.transcript is not None
        return transcript_hash(self.transcript).hex()

    def _sign_local_prepared(
        self, nick: str, attempt: LndEscrowSigningAttempt
    ) -> ChannelInputPreparedPayload:
        state = self._state_for_nick(nick)
        prepared = ChannelInputPreparedPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=self._transcript_hash(),
            funding_outpoint=state.funding_outpoint,
            public_nonce=attempt.public_nonce.hex(),
        )
        signed = sign_payload(prepared, self._key_pair.secret_key)
        assert self.transcript is not None
        self._registry.require_verified(self.local_actor, signed)
        validate_prepared_for_transcript(signed, self.transcript, self.coordinator_key)
        return signed

    def _validate_remote_prepared(self, nick: str, payload: ChannelInputPreparedPayload) -> None:
        state = self._state_for_nick(nick)
        assert self.transcript is not None
        self._registry.require_verified(nick, payload)
        validate_prepared_for_transcript(payload, self.transcript, self._maker_key(nick))
        if payload.funding_outpoint != state.funding_outpoint:
            raise ChannelInputValidationError("remote prepared differs from maker channel identity")

    def _validate_sign_request(
        self,
        nick: str,
        payload: ChannelInputSignPayload,
        state: TakerChannelInputParentChannelState,
    ) -> None:
        assert (
            self.transcript is not None
            and state.local_prepared is not None
            and state.remote_prepared is not None
        )
        self._registry.require_verified(self.local_actor, payload)
        if (
            payload.signer_key != self.coordinator_key
            or payload.local_public_nonce
            != (state.remote_prepared.public_nonce if state.remote_prepared is not None else None)
            or payload.peer_prepared != state.local_prepared
        ):
            raise ChannelInputValidationError("sign request differs from prepared channel evidence")
        validate_sign_for_transcript(
            payload,
            self.transcript,
            self._maker_key(nick),
            expected_local_public_nonce=state.remote_prepared.public_nonce,
        )

    def _validate_remote_partial(
        self,
        nick: str,
        payload: ChannelInputPartialPayload,
        state: TakerChannelInputParentChannelState,
    ) -> None:
        if (
            state.sign_request is None
            or state.local_prepared is None
            or state.remote_prepared is None
        ):
            raise ChannelInputValidationError(
                "remote partial requires durable sign and prepared evidence"
            )
        assert self.transcript is not None
        self._registry.require_verified(nick, payload)
        validate_partial_for_transcript(
            payload,
            self.transcript,
            self._maker_key(nick),
            expected_local_public_nonce=state.remote_prepared.public_nonce,
            expected_peer_public_nonce=state.local_prepared.public_nonce,
        )

    def _validate_local_partial(
        self, payload: ChannelInputPartialPayload, state: TakerChannelInputParentChannelState
    ) -> None:
        if state.local_prepared is None or state.remote_prepared is None:
            raise ChannelInputValidationError("local partial requires both prepared records")
        assert self.transcript is not None
        self._registry.require_verified(self.local_actor, payload)
        validate_partial_for_transcript(
            payload,
            self.transcript,
            self.coordinator_key,
            expected_local_public_nonce=state.local_prepared.public_nonce,
            expected_peer_public_nonce=state.remote_prepared.public_nonce,
        )

    def _validate_finalize_request(
        self,
        nick: str,
        payload: ChannelInputFinalizePayload,
        state: TakerChannelInputParentChannelState,
    ) -> None:
        if state.local_prepared is None or state.remote_prepared is None:
            raise ChannelInputValidationError("finalize request requires both prepared records")
        assert self.transcript is not None
        self._registry.require_verified(self.local_actor, payload)
        validate_finalize_for_transcript(
            payload,
            self.transcript,
            self._maker_key(nick),
            expected_local_public_nonce=state.remote_prepared.public_nonce,
            expected_peer_public_nonce=state.local_prepared.public_nonce,
        )
        if payload.peer_partial != state.local_partial:
            raise ChannelInputValidationError(
                "finalize request differs from local partial evidence"
            )

    @staticmethod
    def _has_sign_or_stronger_evidence(state: TakerChannelInputParentChannelState) -> bool:
        return any(
            value is not None
            for value in (
                state.sign_request,
                state.local_partial,
                state.remote_partial,
                state.finalize_request,
                state.remote_finalized,
            )
        )

    def _require_unconsumed_local_prepared(
        self, state: TakerChannelInputParentChannelState
    ) -> None:
        if self._has_sign_or_stronger_evidence(state):
            raise ChannelInputValidationError("local prepared cannot rotate after signing evidence")

    def _has_partial_or_final_evidence(self) -> bool:
        return any(
            state.local_partial is not None
            or state.remote_partial is not None
            or state.finalize_request is not None
            or state.remote_finalized is not None
            for state in self.parent_channel_states
        )

    def _merge_status_evidence(
        self,
        nick: str,
        response: ChannelInputStatusPayload,
        state: TakerChannelInputParentChannelState,
    ) -> TakerChannelInputParentChannelState:
        if response.escrow_stage == "parent_prepared":
            if any(
                value is not None
                for value in (
                    state.local_partial,
                    state.remote_partial,
                    state.finalize_request,
                    state.remote_finalized,
                )
            ):
                raise ChannelInputValidationError("status regresses durable signing evidence")
            return state
        if response.partial is None:
            raise ChannelInputValidationError("signed status lacks remote partial evidence")
        if state.remote_partial is None:
            self._validate_remote_partial(nick, response.partial, state)
            state = state.model_copy(update={"remote_partial": response.partial})
        elif state.remote_partial != response.partial:
            raise ChannelInputValidationError(
                "status remote partial conflicts with durable evidence"
            )
        if response.escrow_stage == "parent_signed":
            if state.remote_finalized is not None:
                raise ChannelInputValidationError("status regresses durable final evidence")
            return state
        if response.finalized is None:
            raise ChannelInputValidationError("finalized status lacks final parent evidence")
        if state.remote_finalized is None:
            if state.finalize_request is None:
                raise ChannelInputValidationError("status final evidence lacks finalize request")
            self._registry.require_verified(nick, response.finalized)
            assert self.transcript is not None
            validate_finalized_for_transcript(response.finalized, self.transcript)
            if response.finalized.signer_key != self._maker_key(nick):
                raise ChannelInputValidationError("status finalized signer differs from maker")
            state = state.model_copy(update={"remote_finalized": response.finalized})
        elif state.remote_finalized != response.finalized:
            raise ChannelInputValidationError(
                "status final evidence conflicts with durable evidence"
            )
        return state

    def _validate_plan(
        self,
        transcript: ChannelInputTranscript,
        terms: tuple[EndpointTerms, ...],
        *,
        current_time: int,
        current_height: int,
    ) -> None:
        validate_transcript_for_invite(
            self.invite, transcript, current_time=current_time, current_height=current_height
        )
        expected_roster = {
            self.coordinator_key,
            *(item.participant_key for _, item in self.remote_hellos),
        }
        if (
            set(transcript.participant_keys) != expected_roster
            or transcript.coordinator_key != self.coordinator_key
        ):
            raise ChannelInputValidationError("transcript roster does not match accepted hellos")
        if len(transcript.channels) != len(self.selected_nicks) or len(terms) != 2 * len(
            transcript.channels
        ):
            raise ChannelInputValidationError(
                "transcript must contain exactly one channel and two terms per maker"
            )
        by_hash = {endpoint_terms_hash(item).hex(): item for item in terms}
        if len(by_hash) != len(terms) or set(by_hash) != transcript.all_terms_hashes():
            raise ChannelInputValidationError(
                "revealed endpoint terms do not exactly cover transcript hashes"
            )
        remotes = dict(self.remote_hellos)
        locals_ = dict(self.local_hellos)
        expected_outpoints = {item.funding_outpoint for item in remotes.values()}
        if {item.funding_outpoint for item in transcript.channels} != expected_outpoints:
            raise ChannelInputValidationError(
                "transcript channel outpoints do not match accepted hellos"
            )
        for descriptor in transcript.channels:
            remote = next(
                item
                for item in remotes.values()
                if item.funding_outpoint == descriptor.funding_outpoint
            )
            local = next(
                item
                for item in locals_.values()
                if item.funding_outpoint == descriptor.funding_outpoint
            )
            if {descriptor.first_participant_key, descriptor.second_participant_key} != {
                self.coordinator_key,
                remote.participant_key,
            }:
                raise ChannelInputValidationError(
                    "descriptor does not pair coordinator with its maker"
                )
            first, second = (
                by_hash[descriptor.first_terms_hash],
                by_hash[descriptor.second_terms_hash],
            )
            validate_revealed_endpoint_pair(transcript, descriptor, first, second)
            self._validate_terms_hello(
                first, local if first.participant_key == self.coordinator_key else remote
            )
            self._validate_terms_hello(
                second, local if second.participant_key == self.coordinator_key else remote
            )

    @staticmethod
    def _validate_terms_hello(terms: EndpointTerms, hello: ChannelInputHelloPayload) -> None:
        if (
            terms.funding_outpoint != hello.funding_outpoint
            or terms.capacity_sat != hello.capacity_sat
            or terms.local_node_key != hello.node_key
            or terms.peer_node_key != hello.peer_node_key
            or terms.frozen_local_claim_sat != hello.frozen_local_claim_sat
            or terms.frozen_remote_claim_sat != hello.frozen_remote_claim_sat
            or terms.policy_ceilings != hello.policy_ceilings
            or terms.miner_fee_share_sat > hello.quoted_miner_fee_share_sat
            or any(
                output.script_pubkey != hello.settlement_script_pubkey
                for output in terms.settlement_outputs
            )
        ):
            raise ChannelInputValidationError("endpoint terms differ from accepted hello")

    def _validate_restored_state(self) -> None:
        try:
            _selected_nicks(self.selected_nicks, self.local_actor)
            if (
                self.coordinator_key != self.invite.signer_key
                or self.network != self.invite.network
            ):
                raise ChannelInputValidationError("coordinator identity differs from invite")
            if not verify_payload(self.invite):
                raise ChannelInputValidationError("invite signature is invalid")
            remote, local = dict(self.remote_hellos), dict(self.local_hellos)
            intents, frozen = dict(self.freeze_intents), dict(self.frozen_inputs)
            if (
                _nick_items(self.remote_hellos) != self.remote_hellos
                or _nick_items(self.local_hellos) != self.local_hellos
                or _nick_items(self.freeze_intents) != self.freeze_intents
                or _nick_items(self.frozen_inputs) != self.frozen_inputs
            ):
                raise ChannelInputValidationError("durable nick mappings are not sorted")
            if (
                not set(remote).issubset(self.selected_nicks)
                or not set(local).issubset(remote)
                or not set(intents).issubset(remote)
                or not set(frozen).issubset(set(intents) & set(local))
            ):
                raise ChannelInputValidationError("durable evidence does not match selected makers")
            for nick in remote:
                if nick in local:
                    self._validate_hello_evidence(nick, remote[nick], local[nick])
                else:
                    self._validate_remote_hello_evidence(nick, remote[nick])
            if len({hello.funding_outpoint for hello in remote.values()}) != len(remote):
                raise ChannelInputValidationError("hello outpoints are not unique")
            for nick, intent in intents.items():
                self._validate_freeze_intent_evidence(nick, intent, remote[nick])
            if len({intent.funding_outpoint for intent in intents.values()}) != len(intents):
                raise ChannelInputValidationError("freeze intent outpoints are not unique")
            for nick, frozen_input in frozen.items():
                self._validate_frozen_channel(nick, frozen_input, intents[nick], remote[nick])
                self._validate_local_hello_from_frozen(
                    local[nick], remote[nick], intents[nick], frozen_input
                )
            self._validate_stage_fields()
            self._validate_signed_evidence()
            expected = self._expected_registry()
            if self._registry.snapshot() != expected:
                raise ChannelInputValidationError(
                    "actor registry differs from signed negotiation evidence"
                )
            if self._ledger.snapshot() != self._expected_ledger():
                raise ChannelInputValidationError(
                    "replay ledger differs from signed negotiation evidence"
                )
        except (TypeError, ValueError, KeyError, StopIteration) as exc:
            raise TakerChannelInputNegotiationError("snapshot failed deep validation") from exc

    def _validate_hello_evidence(
        self, nick: str, remote: ChannelInputHelloPayload, local: ChannelInputHelloPayload
    ) -> None:
        self._require_nick(nick)
        validate_hello_for_invite(self.invite, remote)
        validate_hello_for_invite(self.invite, local)
        if (
            remote.participant_key == self.coordinator_key
            or local.participant_key != self.coordinator_key
        ):
            raise ChannelInputValidationError("restored hello coordinator keys differ")
        validate_hello_pair(remote, local)

    def _validate_remote_hello_evidence(self, nick: str, remote: ChannelInputHelloPayload) -> None:
        self._require_nick(nick)
        validate_hello_for_invite(self.invite, remote)
        if (
            remote.participant_key == self.coordinator_key
            or remote.signer_key != remote.participant_key
        ):
            raise ChannelInputValidationError("restored remote hello signer differs from maker")

    def _validate_remote_freeze_hello(
        self,
        nick: str,
        remote: ChannelInputHelloPayload,
        candidate: ChannelInputCandidate,
        local_node_key: str,
    ) -> None:
        self._validate_remote_hello_evidence(nick, remote)
        _validate_node_key(local_node_key, "local_node_key")
        normalized_candidate = _normalize_candidate(candidate)
        if (
            remote.node_key != normalized_candidate.peer_node_pubkey
            or remote.peer_node_key != local_node_key
            or remote.funding_outpoint.key()
            != (
                normalized_candidate.funding_outpoint.txid,
                normalized_candidate.funding_outpoint.vout,
            )
            or remote.capacity_sat != normalized_candidate.capacity_sat
        ):
            raise ChannelInputValidationError("remote hello differs from local freeze candidate")
        if remote.participant_key in {
            item.participant_key for other_nick, item in self.remote_hellos if other_nick != nick
        }:
            raise ChannelInputValidationError("maker participant key is already assigned")
        if remote.funding_outpoint in {
            item.funding_outpoint for other_nick, item in self.remote_hellos if other_nick != nick
        }:
            raise ChannelInputValidationError("channel funding outpoint is already assigned")

    def _validate_freeze_intent_evidence(
        self, nick: str, intent: TakerChannelInputFreezeIntent, remote: ChannelInputHelloPayload
    ) -> None:
        if (
            intent.nick != nick
            or intent.peer_node_pubkey != remote.node_key
            or intent.funding_outpoint.txid != remote.funding_outpoint.txid
            or intent.funding_outpoint.vout != remote.funding_outpoint.vout
            or intent.capacity_sat != remote.capacity_sat
            or intent.quoted_miner_fee_share_sat > self.invite.max_parent_fee_sat
        ):
            raise ChannelInputValidationError("freeze intent differs from remote hello")

    def _validate_frozen_channel(
        self,
        nick: str,
        frozen: TakerFrozenChannelInput,
        intent: TakerChannelInputFreezeIntent,
        remote: ChannelInputHelloPayload,
    ) -> None:
        snapshot = frozen.snapshot
        self._validate_freeze_intent_evidence(nick, intent, remote)
        if (
            snapshot.session_id != self.invite.session_id
            or snapshot.channel_id != intent.channel_id
            or snapshot.funding_outpoint != intent.funding_outpoint
            or snapshot.peer_node_pubkey.hex() != intent.peer_node_pubkey
            or snapshot.capacity_sat != intent.capacity_sat
            or snapshot.funding_scriptpubkey.hex() != remote.funding_script_pubkey
            or snapshot.local_claim_sat != remote.frozen_remote_claim_sat
            or snapshot.remote_claim_sat != remote.frozen_local_claim_sat
            or not snapshot.quiescent
            or snapshot.active_htlcs != 0
        ):
            raise ChannelInputValidationError("frozen channel differs from intent or remote hello")

    def _local_hello_from_frozen(
        self,
        remote: ChannelInputHelloPayload,
        intent: TakerChannelInputFreezeIntent,
        frozen: TakerFrozenChannelInput,
    ) -> ChannelInputHelloPayload:
        snapshot = frozen.snapshot
        return sign_payload(
            ChannelInputHelloPayload(
                session_id=self.invite.session_id,
                attempt=self.invite.attempt,
                signer_key=self.coordinator_key,
                participant_key=self.coordinator_key,
                node_key=remote.peer_node_key,
                peer_node_key=remote.node_key,
                funding_outpoint=remote.funding_outpoint,
                capacity_sat=snapshot.capacity_sat,
                funding_script_pubkey=snapshot.funding_scriptpubkey.hex(),
                frozen_local_claim_sat=snapshot.local_claim_sat,
                frozen_remote_claim_sat=snapshot.remote_claim_sat,
                settlement_script_pubkey=intent.settlement_script_pubkey,
                quoted_miner_fee_share_sat=intent.quoted_miner_fee_share_sat,
                policy_ceilings=intent.policy_ceilings,
            ),
            self._key_pair.secret_key,
        )

    def _validate_local_hello_from_frozen(
        self,
        local: ChannelInputHelloPayload,
        remote: ChannelInputHelloPayload,
        intent: TakerChannelInputFreezeIntent,
        frozen: TakerFrozenChannelInput,
    ) -> None:
        snapshot = frozen.snapshot
        validate_hello_for_invite(self.invite, local)
        validate_hello_pair(remote, local)
        if (
            local.signer_key != self.coordinator_key
            or local.participant_key != self.coordinator_key
            or local.node_key != remote.peer_node_key
            or local.peer_node_key != remote.node_key
            or local.funding_outpoint != remote.funding_outpoint
            or local.capacity_sat != snapshot.capacity_sat
            or local.funding_script_pubkey != snapshot.funding_scriptpubkey.hex()
            or local.frozen_local_claim_sat != snapshot.local_claim_sat
            or local.frozen_remote_claim_sat != snapshot.remote_claim_sat
            or local.settlement_script_pubkey != intent.settlement_script_pubkey
            or local.quoted_miner_fee_share_sat != intent.quoted_miner_fee_share_sat
            or local.policy_ceilings != intent.policy_ceilings
        ):
            raise ChannelInputValidationError("local hello differs from frozen channel evidence")

    def _validate_stage_fields(self) -> None:
        remote_hello_count = len(self.remote_hellos)
        complete_hellos = remote_hello_count == len(self.selected_nicks) and len(
            self.local_hellos
        ) == len(self.selected_nicks)
        complete_freeze = len(self.freeze_intents) == len(self.selected_nicks) and len(
            self.frozen_inputs
        ) == len(self.selected_nicks)
        complete_proposals = (
            self.transcript is not None
            and bool(self.endpoint_terms)
            and len(self.proposals) == len(self.selected_nicks)
            and self.local_approval is not None
        )
        complete_approvals = len(self.maker_approvals) == len(self.selected_nicks)
        complete_acks = len(self.maker_acks) == len(self.selected_nicks)
        complete_cancel_acks = len(self.cancel_acks) == len(self.selected_nicks)
        later_evidence = (
            self.transcript,
            self.endpoint_terms,
            self.proposals,
            self.local_approval,
            self.maker_approvals,
            self.approval_set,
            self.local_ack,
            self.maker_acks,
            self.prepare,
            self.cancel,
            self.cancel_acks,
            self.local_resumed_outpoints,
        )
        if self.parent_channel_states and self.stage not in {
            TakerChannelInputStage.PREPARE_READY,
            TakerChannelInputStage.CANCEL_INTENDED,
            TakerChannelInputStage.CANCELED,
        }:
            raise ChannelInputValidationError("parent channel states precede prepare")
        if self.stage is TakerChannelInputStage.INVITED:
            if (
                remote_hello_count >= len(self.selected_nicks)
                or self.freeze_intents
                or self.frozen_inputs
                or any(later_evidence)
            ):
                raise ChannelInputValidationError("invited state evidence is inconsistent")
            return
        if self.stage is TakerChannelInputStage.FREEZE_INTENDED:
            if not self.freeze_intents or any(later_evidence):
                raise ChannelInputValidationError("freeze-intended state evidence is inconsistent")
            if complete_hellos and complete_freeze:
                raise ChannelInputValidationError(
                    "freeze-intended state has complete hello evidence"
                )
            return
        if self.stage is TakerChannelInputStage.HELLOS_READY:
            if (
                not complete_hellos
                or any(later_evidence)
                or bool(self.freeze_intents) != bool(self.frozen_inputs)
                or (self.freeze_intents and not complete_freeze)
            ):
                raise ChannelInputValidationError("hello-ready state evidence is inconsistent")
            return
        if self.stage is TakerChannelInputStage.CANCEL_INTENDED:
            self._validate_cancel_shape(
                complete_approvals=complete_approvals,
                complete_acks=complete_acks,
                complete_cancel_acks=complete_cancel_acks,
                canceled=False,
                pre_transcript=self.transcript is None,
            )
            return
        if self.stage is TakerChannelInputStage.CANCELED:
            self._validate_cancel_shape(
                complete_approvals=complete_approvals,
                complete_acks=complete_acks,
                complete_cancel_acks=complete_cancel_acks,
                canceled=True,
                pre_transcript=self.transcript is None,
            )
            return
        if bool(self.freeze_intents) != bool(self.frozen_inputs) or (
            self.freeze_intents and not complete_freeze
        ):
            raise ChannelInputValidationError("later state contains incomplete freeze evidence")
        if not complete_hellos or not complete_proposals:
            raise ChannelInputValidationError("state requires complete proposal evidence")
        if self.stage is TakerChannelInputStage.PROPOSALS_READY:
            if (
                self.approval_set
                or self.local_ack
                or self.maker_acks
                or self.prepare
                or self.cancel
            ):
                raise ChannelInputValidationError("proposal-ready state contains later evidence")
            if self.cancel_acks or self.local_resumed_outpoints:
                raise ChannelInputValidationError("proposal-ready state contains cancel evidence")
            return
        if self.stage is TakerChannelInputStage.APPROVAL_SET_READY:
            if not complete_approvals or self.approval_set is None or self.local_ack is None:
                raise ChannelInputValidationError("approval-set state lacks approval evidence")
            if self.prepare or self.cancel or self.cancel_acks or self.local_resumed_outpoints:
                raise ChannelInputValidationError("approval-set state contains later evidence")
            return
        if self.stage is TakerChannelInputStage.PREPARE_READY:
            if (
                not complete_approvals
                or self.approval_set is None
                or self.local_ack is None
                or not complete_acks
                or self.prepare is None
            ):
                raise ChannelInputValidationError("prepare-ready state lacks complete ACK evidence")
            if self.cancel or self.cancel_acks or self.local_resumed_outpoints:
                raise ChannelInputValidationError(
                    "prepare-ready state contains cancellation evidence"
                )
            return
        raise ChannelInputValidationError("snapshot stage is not supported")

    def _validate_cancel_shape(
        self,
        *,
        complete_approvals: bool,
        complete_acks: bool,
        complete_cancel_acks: bool,
        canceled: bool,
        pre_transcript: bool,
    ) -> None:
        if self.cancel is None:
            raise ChannelInputValidationError("cancel state lacks cancellation intent")
        if self._has_partial_or_final_evidence():
            raise ChannelInputValidationError(
                "cancel state contains partial or final parent evidence"
            )
        if pre_transcript:
            if self.parent_channel_states:
                raise ChannelInputValidationError(
                    "pre-transcript cancel contains parent channel states"
                )
            if (
                self.endpoint_terms
                or self.proposals
                or self.local_approval is not None
                or self.maker_approvals
                or self.approval_set is not None
                or self.local_ack is not None
                or self.maker_acks
                or self.prepare is not None
            ):
                raise ChannelInputValidationError(
                    "pre-transcript cancel contains proposal evidence"
                )
            if canceled:
                if not complete_cancel_acks:
                    raise ChannelInputValidationError(
                        "canceled state lacks complete resume evidence"
                    )
            elif self.local_resumed_outpoints:
                raise ChannelInputValidationError("cancel-intended state has local resume evidence")
            return
        if self.approval_set is None:
            if self.local_ack is not None or self.maker_acks or self.prepare:
                raise ChannelInputValidationError("cancel state has orphaned approval evidence")
        else:
            if not complete_approvals or self.local_ack is None:
                raise ChannelInputValidationError("cancel state approval evidence is incomplete")
            if self.prepare is not None and not complete_acks:
                raise ChannelInputValidationError("cancel state prepare ACK evidence is incomplete")
        if canceled:
            if not complete_cancel_acks:
                raise ChannelInputValidationError("canceled state lacks complete resume evidence")
        elif self.local_resumed_outpoints:
            raise ChannelInputValidationError("cancel-intended state has local resume evidence")

    def _validate_signed_evidence(self) -> None:
        if self.transcript is None:
            self._validate_cancel_evidence()
            return
        restore_height = self.transcript.recovery_height - self.invite.min_recovery_delta
        self._validate_plan(
            self.transcript,
            self.endpoint_terms,
            current_time=0,
            current_height=max(0, restore_height),
        )
        if (
            dict(self.proposals).keys() != dict(self.remote_hellos).keys()
            or _nick_items(self.proposals) != self.proposals
        ):
            raise ChannelInputValidationError("proposal mapping is incomplete or unordered")
        for nick, proposal in self.proposals:
            self._registry.require_verified(self.local_actor, proposal)
            validate_proposal_for_hellos(
                self.invite,
                (dict(self.remote_hellos)[nick],),
                proposal,
                current_time=0,
                current_height=max(
                    0, self.transcript.recovery_height - self.invite.min_recovery_delta
                ),
            )
        assert self.local_approval is not None
        self._registry.require_verified(self.local_actor, self.local_approval)
        if (
            self.local_approval.session_id != self.invite.session_id
            or self.local_approval.attempt != self.invite.attempt
            or self.local_approval.transcript_hash != transcript_hash(self.transcript).hex()
            or set(self.local_approval.terms_hashes)
            != self.transcript.terms_hashes_for(self.coordinator_key)
        ):
            raise ChannelInputValidationError("local approval does not cover coordinator terms")
        _validate_nick_mapping(
            self.maker_approvals,
            self.selected_nicks,
            require_complete=self.approval_set is not None,
            label="maker approvals",
        )
        for nick, approval in self.maker_approvals:
            self._registry.require_verified(nick, approval)
            proposal = dict(self.proposals)[nick]
            if (
                approval.session_id != self.invite.session_id
                or approval.attempt != self.invite.attempt
                or approval.transcript_hash != transcript_hash(self.transcript).hex()
                or set(approval.terms_hashes)
                != self.transcript.terms_hashes_for(proposal.recipient_key)
            ):
                raise ChannelInputValidationError("maker approval differs from assigned terms")
        if self.approval_set is not None:
            validate_complete_approval_set(self.approval_set)
            if self.approval_set.transcript != self.transcript or self.local_ack is None:
                raise ChannelInputValidationError("approval set differs from transcript")
            self._registry.require_verified(self.local_actor, self.approval_set)
            self._registry.require_verified(self.local_actor, self.local_ack)
            if (
                self.local_ack.session_id != self.invite.session_id
                or self.local_ack.attempt != self.invite.attempt
                or self.local_ack.transcript_hash != transcript_hash(self.transcript).hex()
                or self.local_ack.approval_set_hash != approval_set_hash(self.approval_set).hex()
            ):
                raise ChannelInputValidationError("local ACK differs from approval set")
        elif self.maker_acks or self.prepare:
            raise ChannelInputValidationError("ACK or prepare lacks approval set")
        _validate_nick_mapping(
            self.maker_acks,
            self.selected_nicks,
            require_complete=self.prepare is not None,
            label="maker ACKs",
        )
        for nick, ack in self.maker_acks:
            self._registry.require_verified(nick, ack)
            if (
                self.approval_set is None
                or ack.session_id != self.invite.session_id
                or ack.attempt != self.invite.attempt
                or ack.transcript_hash != transcript_hash(self.transcript).hex()
                or ack.approval_set_hash != approval_set_hash(self.approval_set).hex()
            ):
                raise ChannelInputValidationError("maker ACK differs from approval set")
        if self.prepare is not None:
            validate_prepare_payload(self.prepare)
            if self.approval_set != self.prepare.approval_set:
                raise ChannelInputValidationError("prepare differs from approval set")
            self._validate_parent_channel_states()
        elif self.parent_channel_states:
            raise ChannelInputValidationError("parent channel states require accepted prepare")
        self._validate_cancel_evidence()

    def _validate_parent_channel_states(self) -> None:
        """Recheck dynamic post-prepare evidence outside the immutable replay ledger."""

        assert self.transcript is not None
        states = self.parent_channel_states
        if tuple(sorted(states, key=lambda state: state.nick)) != states:
            raise ChannelInputValidationError("parent channel states are not nick-sorted")
        if len({state.nick for state in states}) != len(states):
            raise ChannelInputValidationError("parent channel states have duplicate nicks")
        remote_hellos = dict(self.remote_hellos)
        for state in states:
            self._require_nick(state.nick)
            remote = remote_hellos.get(state.nick)
            if remote is None or state.funding_outpoint != remote.funding_outpoint:
                raise ChannelInputValidationError(
                    "parent channel state differs from accepted maker hello"
                )
            if state.local_prepared is not None:
                assert state.local_attempt_id is not None
                try:
                    attempt = LndEscrowSigningAttempt(
                        attempt_id=bytes.fromhex(state.local_attempt_id),
                        public_nonce=bytes.fromhex(state.local_prepared.public_nonce),
                    )
                except ValueError as exc:
                    raise ChannelInputValidationError("local signing attempt is invalid") from exc
                if attempt.public_nonce.hex() != state.local_prepared.public_nonce:
                    raise ChannelInputValidationError("local prepared differs from signing attempt")
                self._registry.require_verified(self.local_actor, state.local_prepared)
                validate_prepared_for_transcript(
                    state.local_prepared, self.transcript, self.coordinator_key
                )
            if state.remote_prepared is not None:
                self._validate_remote_prepared(state.nick, state.remote_prepared)
            if state.sign_request is not None:
                self._validate_sign_request(state.nick, state.sign_request, state)
            if state.local_partial is not None:
                self._validate_local_partial(state.local_partial, state)
            if state.remote_partial is not None:
                self._validate_remote_partial(state.nick, state.remote_partial, state)
            if state.finalize_request is not None:
                self._validate_finalize_request(state.nick, state.finalize_request, state)
            if state.remote_finalized is not None:
                if state.finalize_request is None:
                    raise ChannelInputValidationError("remote finalized lacks finalize request")
                self._registry.require_verified(state.nick, state.remote_finalized)
                validate_finalized_for_transcript(state.remote_finalized, self.transcript)
                if state.remote_finalized.signer_key != self._maker_key(state.nick):
                    raise ChannelInputValidationError("remote finalized signer differs from maker")
            if state.status_request is not None:
                self._registry.require_verified(self.local_actor, state.status_request)
                if (
                    state.status_request.session_id != self.invite.session_id
                    or state.status_request.attempt != self.invite.attempt
                    or state.status_request.transcript_hash != self._transcript_hash()
                    or state.status_request.funding_outpoint != state.funding_outpoint
                ):
                    raise ChannelInputValidationError(
                        "status request differs from parent channel state"
                    )
            if state.status_response is not None:
                assert state.status_request is not None
                self._registry.require_verified(state.nick, state.status_response)
                validate_status_for_transcript(
                    state.status_response,
                    state.status_request,
                    self.transcript,
                    self._maker_key(state.nick),
                )
                if state.remote_prepared != state.status_response.prepared:
                    raise ChannelInputValidationError(
                        "status prepared was not merged into parent channel state"
                    )
                if state.status_response.partial is not None and (
                    state.remote_partial != state.status_response.partial
                    or state.local_prepared is None
                    or state.status_response.partial.peer_public_nonce
                    != state.local_prepared.public_nonce
                ):
                    raise ChannelInputValidationError(
                        "status partial was not merged into parent channel state"
                    )
                if (
                    state.status_response.finalized is not None
                    and state.remote_finalized != state.status_response.finalized
                ):
                    raise ChannelInputValidationError(
                        "status final evidence was not merged into parent channel state"
                    )

    def _validate_cancel_evidence(self) -> None:
        if self.cancel is not None:
            self._registry.require_verified(self.local_actor, self.cancel)
            if (
                self.cancel.session_id != self.invite.session_id
                or self.cancel.attempt != self.invite.attempt
                or self.cancel.transcript_hash
                != cancellation_binding_hash(self.invite, self.transcript).hex()
            ):
                raise ChannelInputValidationError("cancel differs from cancellation binding")
        _validate_nick_mapping(
            self.cancel_acks,
            self.selected_nicks,
            require_complete=self.stage is TakerChannelInputStage.CANCELED,
            label="cancel ACKs",
        )
        resumed_outpoints: set[Outpoint] = set()
        for nick, cancel_ack in self.cancel_acks:
            if self.cancel is None:
                raise ChannelInputValidationError("cancel ACK lacks cancellation")
            remote_hello = dict(self.remote_hellos).get(nick)
            if remote_hello is None:
                if len(cancel_ack.resumed_outpoints) != 1:
                    raise ChannelInputValidationError(
                        "cancel ACK without a durable hello requires exactly one resumed outpoint"
                    )
                validate_cancel_ack(
                    self.cancel, cancel_ack, cancel_ack.signer_key, cancel_ack.resumed_outpoints
                )
            else:
                validate_cancel_ack(
                    self.cancel,
                    cancel_ack,
                    remote_hello.participant_key,
                    (remote_hello.funding_outpoint,),
                )
            self._registry.require_verified(nick, cancel_ack)
            for outpoint in cancel_ack.resumed_outpoints:
                if outpoint in resumed_outpoints:
                    raise ChannelInputValidationError(
                        "cancel ACK resumed outpoint is already assigned"
                    )
                resumed_outpoints.add(outpoint)
        if self.stage is TakerChannelInputStage.CANCELED:
            expected = self._expected_local_resumed_outpoints()
            if self.local_resumed_outpoints != expected:
                raise ChannelInputValidationError("local resumed outpoints are invalid")

    def _expected_local_resumed_outpoints(self) -> tuple[Outpoint, ...]:
        if self.transcript is not None:
            outpoints = (hello.funding_outpoint for _, hello in self.local_hellos)
        else:
            outpoints = (
                Outpoint(
                    txid=frozen.snapshot.funding_outpoint.txid,
                    vout=frozen.snapshot.funding_outpoint.vout,
                )
                for _, frozen in self.frozen_inputs
            )
        return tuple(sorted(outpoints, key=lambda x: x.key()))

    def _expected_registry(self) -> tuple[tuple[str, str], ...]:
        bindings = {
            self.local_actor: self.coordinator_key,
            **{nick: hello.signer_key for nick, hello in self.remote_hellos},
            **{
                nick: ack.signer_key
                for nick, ack in self.cancel_acks
                if nick not in dict(self.remote_hellos)
            },
        }
        return tuple(sorted(bindings.items()))

    def _require_unique_cancel_ack_outpoints(self, ack: ChannelInputCancelAckPayload) -> None:
        assigned = {
            outpoint for _, existing in self.cancel_acks for outpoint in existing.resumed_outpoints
        }
        if any(outpoint in assigned for outpoint in ack.resumed_outpoints):
            raise ChannelInputValidationError("cancel ACK resumed outpoint is already assigned")

    def _expected_ledger(self) -> tuple[tuple[str, str], ...]:
        ledger = ChannelInputReplayLedger()
        _record(ledger, _slot("invite"), self.invite)
        for nick, remote_hello in self.remote_hellos:
            _record(ledger, _slot("hello_remote", nick), remote_hello)
        for nick, local_hello in self.local_hellos:
            _record(ledger, _slot("hello_local", nick), local_hello)
        for nick, proposal in self.proposals:
            _record(ledger, _slot("proposal", nick), proposal)
        if self.local_approval is not None:
            _record(ledger, _slot("approval_local"), self.local_approval)
        for nick, approval in self.maker_approvals:
            _record(ledger, _slot("approval", nick), approval)
        if self.approval_set is not None:
            _record(ledger, _slot("approval_set"), self.approval_set)
        if self.local_ack is not None:
            _record(ledger, _slot("approval_ack_local"), self.local_ack)
        for nick, maker_ack in self.maker_acks:
            _record(ledger, _slot("approval_ack", nick), maker_ack)
        if self.prepare is not None:
            _record(ledger, _slot("prepare"), self.prepare)
        if self.cancel is not None:
            _record(ledger, _slot("cancel"), self.cancel)
        for nick, cancel_ack in self.cancel_acks:
            _record(ledger, _slot("cancel_ack", nick), cancel_ack)
        return ledger.snapshot()

    def _require_nick(self, nick: str) -> None:
        if nick not in self.selected_nicks:
            raise ChannelInputValidationError("actor is not a selected maker")


def _freeze_intent_from_candidate(
    nick: str,
    candidate: ChannelInputCandidate,
    *,
    settlement_script_pubkey: str,
    quoted_miner_fee_share_sat: int,
    policy_ceilings: PolicyCeilings,
) -> TakerChannelInputFreezeIntent:
    normalized = _normalize_candidate(candidate)
    try:
        return TakerChannelInputFreezeIntent(
            nick=nick,
            channel_id=_outpoint_identifier(normalized.funding_outpoint),
            funding_outpoint=normalized.funding_outpoint,
            peer_node_pubkey=normalized.peer_node_pubkey,
            capacity_sat=normalized.capacity_sat,
            settlement_script_pubkey=settlement_script_pubkey,
            quoted_miner_fee_share_sat=quoted_miner_fee_share_sat,
            policy_ceilings=policy_ceilings,
        )
    except ValueError as exc:
        raise ChannelInputValidationError("freeze intent evidence is invalid") from exc


def _normalize_candidate(candidate: ChannelInputCandidate) -> ChannelInputCandidate:
    if not isinstance(candidate, ChannelInputCandidate):
        raise ChannelInputValidationError("freeze candidate must use an exact LND candidate")
    try:
        return ChannelInputCandidate.model_validate(candidate.model_dump(mode="python"))
    except ValueError as exc:
        raise ChannelInputValidationError("freeze candidate evidence is invalid") from exc


def _validated_signing_attempt(attempt: LndEscrowSigningAttempt) -> LndEscrowSigningAttempt:
    if not isinstance(attempt, LndEscrowSigningAttempt):
        raise ChannelInputValidationError("LND signing attempt is invalid")
    try:
        return LndEscrowSigningAttempt.model_validate(attempt.model_dump(mode="python"))
    except ValueError as exc:
        raise ChannelInputValidationError("LND signing attempt is invalid") from exc


def _normalize_frozen_input(snapshot: FrozenChannelSnapshot) -> TakerFrozenChannelInput:
    if not isinstance(snapshot, FrozenChannelSnapshot):
        raise ChannelInputValidationError("frozen channel must use an exact LND snapshot")
    try:
        return TakerFrozenChannelInput.model_validate(
            {"snapshot": snapshot.model_dump(mode="python")}
        )
    except ValueError as exc:
        raise ChannelInputValidationError("frozen channel evidence is invalid") from exc


def _selected_nicks(selected_nicks: Sequence[str], local_actor: str) -> tuple[str, ...]:
    if not isinstance(local_actor, str) or not local_actor:
        raise ChannelInputValidationError("local_actor must be a non-empty string")
    if isinstance(selected_nicks, str | bytes):
        raise ChannelInputValidationError("selected_nicks must be a finite sequence")
    nicks = tuple(selected_nicks)
    if not nicks or any(not isinstance(nick, str) or not nick for nick in nicks):
        raise ChannelInputValidationError("selected_nicks must contain non-empty strings")
    if tuple(sorted(nicks)) != nicks or len(set(nicks)) != len(nicks) or local_actor in nicks:
        raise ChannelInputValidationError(
            "selected_nicks must be unique, sorted, and exclude local_actor"
        )
    return nicks


_NickItem = TypeVar("_NickItem")


def _nick_items(
    items: Sequence[tuple[str, _NickItem]],
) -> tuple[tuple[str, _NickItem], ...]:
    values = tuple(items)
    if len({nick for nick, _ in values}) != len(values):
        raise ChannelInputValidationError("nick mapping contains duplicates")
    return tuple(sorted(values, key=lambda item: item[0]))


def _validate_nick_mapping(
    items: Sequence[tuple[str, object]],
    selected_nicks: tuple[str, ...],
    *,
    require_complete: bool,
    label: str,
) -> None:
    ordered = _nick_items(items)
    nicks = {nick for nick, _ in ordered}
    if not nicks.issubset(selected_nicks) or (require_complete and nicks != set(selected_nicks)):
        raise ChannelInputValidationError(f"{label} do not match selected makers")
    if tuple(items) != ordered:
        raise ChannelInputValidationError(f"{label} are not deterministically ordered")


def _ordered_terms(endpoint_terms: Sequence[EndpointTerms]) -> tuple[EndpointTerms, ...]:
    if isinstance(endpoint_terms, str | bytes):
        raise ChannelInputValidationError("endpoint_terms must be a finite sequence")
    terms = tuple(endpoint_terms)
    if not all(isinstance(item, EndpointTerms) for item in terms):
        raise ChannelInputValidationError("endpoint_terms must contain endpoint terms")
    ordered = tuple(
        sorted(terms, key=lambda item: (item.funding_outpoint.key(), item.participant_key))
    )
    if terms != ordered:
        raise ChannelInputValidationError("endpoint_terms must use stable outpoint/key order")
    return terms


def _slot(name: str, nick: str | None = None) -> str:
    return name if nick is None else f"{name}/{nick}"


def _record(ledger: ChannelInputReplayLedger, slot: str, payload: ChannelInputPayload) -> None:
    ledger.record(slot, payload)


def _outpoint_identifier(outpoint: EscrowOutpoint) -> str:
    return f"{outpoint.txid}:{outpoint.vout}"


def _validate_outpoint_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a lowercase <64hex>:<uint32> outpoint")
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
        raise ValueError(f"{field_name} must be a lowercase <64hex>:<uint32> outpoint")
    try:
        bytes.fromhex(txid)
        vout = int(vout_text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a lowercase <64hex>:<uint32> outpoint") from exc
    if vout > 0xFFFFFFFF:
        raise ValueError(f"{field_name} must be a lowercase <64hex>:<uint32> outpoint")


def _validate_p2tr_script(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 68
        or value.lower() != value
        or not value.startswith("5120")
    ):
        raise ValueError(f"{field_name} must be a P2TR script_pubkey")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a P2TR script_pubkey") from exc


def _validate_node_key(value: str, field_name: str) -> None:
    if not isinstance(value, str) or len(value) != 66 or value.lower() != value:
        raise ValueError(f"{field_name} must be a compressed secp256k1 public key")
    try:
        raw = bytes.fromhex(value)
        if raw[0] not in (2, 3):
            raise ValueError
        if not CPubKey(raw).is_fullyvalid():
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a compressed secp256k1 public key") from exc
