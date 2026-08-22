"""Durable pure coordinator negotiation for channel-input v1."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, TypeVar, cast

from jmcore.channel_input import (
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputCancelAckPayload,
    ChannelInputCancelPayload,
    ChannelInputHelloPayload,
    ChannelInputInvitePayload,
    ChannelInputKeyPair,
    ChannelInputModel,
    ChannelInputPayload,
    ChannelInputPreparePayload,
    ChannelInputProposalPayload,
    ChannelInputTranscript,
    ChannelInputValidationError,
    EndpointTerms,
    Network,
    Outpoint,
    approval_set_hash,
    endpoint_terms_hash,
    sign_payload,
    transcript_hash,
    validate_cancel_ack,
    validate_complete_approval_set,
    validate_hello_for_invite,
    validate_hello_pair,
    validate_prepare_payload,
    validate_proposal_for_hellos,
    validate_revealed_endpoint_pair,
    validate_transcript_for_invite,
    verify_payload,
)
from jmcore.channel_input_session import ChannelInputActorRegistry, ChannelInputReplayLedger
from pydantic import Field, field_validator, model_validator


class TakerChannelInputNegotiationError(Exception):
    """The coordinator negotiation cannot safely advance."""


class TakerChannelInputStage(StrEnum):
    INVITED = "invited"
    HELLOS_READY = "hellos_ready"
    PROPOSALS_READY = "proposals_ready"
    APPROVAL_SET_READY = "approval_set_ready"
    PREPARE_READY = "prepare_ready"
    CANCEL_INTENDED = "cancel_intended"
    CANCELED = "canceled"


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
    transcript: ChannelInputTranscript | None = None
    endpoint_terms: tuple[EndpointTerms, ...] = ()
    proposals: tuple[tuple[str, ChannelInputProposalPayload], ...] = ()
    local_approval: ChannelInputApprovalPayload | None = None
    maker_approvals: tuple[tuple[str, ChannelInputApprovalPayload], ...] = ()
    approval_set: ChannelInputApprovalSetPayload | None = None
    local_ack: ChannelInputApprovalSetAckPayload | None = None
    maker_acks: tuple[tuple[str, ChannelInputApprovalSetAckPayload], ...] = ()
    prepare: ChannelInputPreparePayload | None = None
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
        transcript: ChannelInputTranscript | None = None,
        endpoint_terms: tuple[EndpointTerms, ...] = (),
        proposals: tuple[tuple[str, ChannelInputProposalPayload], ...] = (),
        local_approval: ChannelInputApprovalPayload | None = None,
        maker_approvals: tuple[tuple[str, ChannelInputApprovalPayload], ...] = (),
        approval_set: ChannelInputApprovalSetPayload | None = None,
        local_ack: ChannelInputApprovalSetAckPayload | None = None,
        maker_acks: tuple[tuple[str, ChannelInputApprovalSetAckPayload], ...] = (),
        prepare: ChannelInputPreparePayload | None = None,
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
        self.transcript = transcript
        self.endpoint_terms = endpoint_terms
        self.proposals = proposals
        self.local_approval = local_approval
        self.maker_approvals = maker_approvals
        self.approval_set = approval_set
        self.local_ack = local_ack
        self.maker_acks = maker_acks
        self.prepare = prepare
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
        invite = cast(
            ChannelInputInvitePayload,
            sign_payload(
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
            ),
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
            transcript=self.transcript,
            endpoint_terms=self.endpoint_terms,
            proposals=self.proposals,
            local_approval=self.local_approval,
            maker_approvals=self.maker_approvals,
            approval_set=self.approval_set,
            local_ack=self.local_ack,
            maker_acks=self.maker_acks,
            prepare=self.prepare,
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
            transcript=snapshot.transcript,
            endpoint_terms=snapshot.endpoint_terms,
            proposals=snapshot.proposals,
            local_approval=snapshot.local_approval,
            maker_approvals=snapshot.maker_approvals,
            approval_set=snapshot.approval_set,
            local_ack=snapshot.local_ack,
            maker_acks=snapshot.maker_acks,
            prepare=snapshot.prepare,
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
            signed = cast(
                ChannelInputProposalPayload, sign_payload(proposal, self._key_pair.secret_key)
            )
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
        signed_local = cast(
            ChannelInputApprovalPayload, sign_payload(local, self._key_pair.secret_key)
        )
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
        signed = cast(
            ChannelInputApprovalSetPayload, sign_payload(approval_set, self._key_pair.secret_key)
        )
        validate_complete_approval_set(signed)
        ack = ChannelInputApprovalSetAckPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=transcript_hash(self.transcript).hex(),
            approval_set_hash=approval_set_hash(signed).hex(),
        )
        signed_ack = cast(
            ChannelInputApprovalSetAckPayload, sign_payload(ack, self._key_pair.secret_key)
        )
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
        signed = cast(ChannelInputPreparePayload, sign_payload(prepare, self._key_pair.secret_key))
        validate_prepare_payload(signed)
        self._registry.require_verified(self.local_actor, signed)
        _record(self._ledger, _slot("prepare"), signed)
        self.prepare = signed
        self.stage = TakerChannelInputStage.PREPARE_READY
        return signed

    def plan_cancel(self, reason_code: str) -> ChannelInputCancelPayload:
        if self.cancel is not None:
            if self.cancel.reason_code != reason_code:
                raise ChannelInputValidationError(
                    "cancel reason conflicts with durable cancellation"
                )
            return self.cancel
        if self.stage not in {
            TakerChannelInputStage.PROPOSALS_READY,
            TakerChannelInputStage.APPROVAL_SET_READY,
            TakerChannelInputStage.PREPARE_READY,
        }:
            raise ChannelInputValidationError("cancel is not valid at this negotiation stage")
        assert self.transcript is not None
        cancel = ChannelInputCancelPayload(
            session_id=self.invite.session_id,
            attempt=self.invite.attempt,
            signer_key=self.coordinator_key,
            transcript_hash=transcript_hash(self.transcript).hex(),
            reason_code=reason_code,
            partial_released=False,
        )
        signed = cast(ChannelInputCancelPayload, sign_payload(cancel, self._key_pair.secret_key))
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
        self._registry.require_verified(nick, ack)
        validate_cancel_ack(
            self.cancel,
            ack,
            dict(self.remote_hellos)[nick].participant_key,
            (dict(self.remote_hellos)[nick].funding_outpoint,),
        )
        _record(self._ledger, _slot("cancel_ack", nick), ack)
        self.cancel_acks = _nick_items((*self.cancel_acks, (nick, ack)))

    def record_local_resumed(self, outpoints: Sequence[Outpoint]) -> None:
        expected = tuple(
            sorted(
                (hello.funding_outpoint for _, hello in self.local_hellos), key=lambda x: x.key()
            )
        )
        resumed = tuple(outpoints)
        if resumed != expected:
            raise ChannelInputValidationError(
                "local resumed outpoints differ from local hello outpoints"
            )
        if len(self.cancel_acks) != len(self.selected_nicks):
            raise ChannelInputValidationError("local resume requires every maker cancel ACK")
        if self.local_resumed_outpoints:
            if self.local_resumed_outpoints != resumed:
                raise ChannelInputValidationError(
                    "local resumed outpoints conflict with durable evidence"
                )
            return
        if self.stage is not TakerChannelInputStage.CANCEL_INTENDED:
            raise ChannelInputValidationError("local resume is not valid at this negotiation stage")
        self.local_resumed_outpoints = resumed
        self.stage = TakerChannelInputStage.CANCELED

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
            if (
                _nick_items(self.remote_hellos) != self.remote_hellos
                or _nick_items(self.local_hellos) != self.local_hellos
            ):
                raise ChannelInputValidationError("hello mappings are not sorted")
            if set(remote) != set(local) or not set(remote).issubset(self.selected_nicks):
                raise ChannelInputValidationError("hello mappings do not match selected makers")
            for nick in remote:
                self._validate_hello_evidence(nick, remote[nick], local[nick])
            if len({hello.funding_outpoint for hello in remote.values()}) != len(remote):
                raise ChannelInputValidationError("hello outpoints are not unique")
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

    def _validate_stage_fields(self) -> None:
        hello_count = len(self.remote_hellos)
        complete_hellos = hello_count == len(self.selected_nicks)
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
        if self.stage is TakerChannelInputStage.INVITED:
            if hello_count >= len(self.selected_nicks) or any(later_evidence):
                raise ChannelInputValidationError("invited state evidence is inconsistent")
            return
        if self.stage is TakerChannelInputStage.HELLOS_READY:
            if not complete_hellos or any(later_evidence):
                raise ChannelInputValidationError("hello-ready state evidence is inconsistent")
            return
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
        if self.stage is TakerChannelInputStage.CANCEL_INTENDED:
            self._validate_cancel_shape(
                complete_approvals=complete_approvals,
                complete_acks=complete_acks,
                complete_cancel_acks=complete_cancel_acks,
                canceled=False,
            )
            return
        if self.stage is TakerChannelInputStage.CANCELED:
            self._validate_cancel_shape(
                complete_approvals=complete_approvals,
                complete_acks=complete_acks,
                complete_cancel_acks=complete_cancel_acks,
                canceled=True,
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
    ) -> None:
        if self.cancel is None:
            raise ChannelInputValidationError("cancel state lacks cancellation intent")
        if self.approval_set is None:
            if self.local_ack is not None or self.maker_acks or self.prepare:
                raise ChannelInputValidationError("cancel state has orphaned approval evidence")
        else:
            if not complete_approvals or self.local_ack is None:
                raise ChannelInputValidationError("cancel state approval evidence is incomplete")
            if self.prepare is not None and not complete_acks:
                raise ChannelInputValidationError("cancel state prepare ACK evidence is incomplete")
        if canceled:
            if not complete_cancel_acks or not self.local_resumed_outpoints:
                raise ChannelInputValidationError("canceled state lacks complete resume evidence")
        elif self.local_resumed_outpoints:
            raise ChannelInputValidationError("cancel-intended state has local resume evidence")

    def _validate_signed_evidence(self) -> None:
        if self.transcript is None:
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
        if self.cancel is not None:
            self._registry.require_verified(self.local_actor, self.cancel)
            if (
                self.cancel.session_id != self.invite.session_id
                or self.cancel.attempt != self.invite.attempt
                or self.cancel.transcript_hash != transcript_hash(self.transcript).hex()
            ):
                raise ChannelInputValidationError("cancel differs from transcript")
        _validate_nick_mapping(
            self.cancel_acks,
            self.selected_nicks,
            require_complete=self.stage is TakerChannelInputStage.CANCELED,
            label="cancel ACKs",
        )
        for nick, cancel_ack in self.cancel_acks:
            if self.cancel is None:
                raise ChannelInputValidationError("cancel ACK lacks cancellation")
            self._registry.require_verified(nick, cancel_ack)
            validate_cancel_ack(
                self.cancel,
                cancel_ack,
                dict(self.remote_hellos)[nick].participant_key,
                (dict(self.remote_hellos)[nick].funding_outpoint,),
            )
        if self.local_resumed_outpoints:
            expected = tuple(
                sorted(
                    (hello.funding_outpoint for _, hello in self.local_hellos),
                    key=lambda x: x.key(),
                )
            )
            if self.local_resumed_outpoints != expected:
                raise ChannelInputValidationError("local resumed outpoints are invalid")

    def _expected_registry(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            sorted(
                (
                    (self.local_actor, self.coordinator_key),
                    *((nick, hello.signer_key) for nick, hello in self.remote_hellos),
                )
            )
        )

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
