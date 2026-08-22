"""Durable maker-side negotiation for channel-input v1 before backend preparation."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from enum import StrEnum
from typing import Literal, cast

from coincurve import PublicKey
from jmcore.channel_input import (
    MAX_CHANNELS,
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputHelloPayload,
    ChannelInputInvitePayload,
    ChannelInputKeyPair,
    ChannelInputModel,
    ChannelInputPayload,
    ChannelInputPreparePayload,
    ChannelInputProposalPayload,
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
    validate_hello_for_invite,
    validate_prepare_payload,
    validate_proposal_for_hellos,
    validate_recipient_approval,
)
from jmcore.channel_input_config import ChannelInputConfig
from jmcore.channel_input_session import (
    ChannelInputActorRegistry,
    ChannelInputReplayLedger,
    ChannelInputReplayResult,
)
from jmswap.lnd import FrozenChannelSnapshot
from pydantic import Field, field_serializer, field_validator, model_validator


class MakerChannelInputStage(StrEnum):
    """Durable stages that do not make any LND or network calls."""

    INVITED = "invited"
    HELLOS_READY = "hellos_ready"
    PROPOSAL_APPROVED = "proposal_approved"
    APPROVAL_SET_ACKED = "approval_set_acked"
    PREPARED = "prepared"


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
    frozen_inputs: tuple[MakerFrozenChannelInput, ...] = ()
    hellos: tuple[ChannelInputHelloPayload, ...] = ()
    proposal: ChannelInputProposalPayload | None = None
    accepted_height: int | None = Field(default=None, ge=0, le=0xFFFFFFFF)
    local_approval: ChannelInputApprovalPayload | None = None
    approval_set: ChannelInputApprovalSetPayload | None = None
    local_ack: ChannelInputApprovalSetAckPayload | None = None
    prepare: ChannelInputPreparePayload | None = None
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
        if len(self.frozen_inputs) > MAX_CHANNELS or len(self.hellos) > MAX_CHANNELS:
            raise ValueError("channel input snapshot exceeds channel limit")
        stage_rank = {
            MakerChannelInputStage.INVITED: 0,
            MakerChannelInputStage.HELLOS_READY: 1,
            MakerChannelInputStage.PROPOSAL_APPROVED: 2,
            MakerChannelInputStage.APPROVAL_SET_ACKED: 3,
            MakerChannelInputStage.PREPARED: 4,
        }
        rank = stage_rank[self.stage]
        if (rank >= 1) != bool(self.frozen_inputs) or len(self.frozen_inputs) != len(self.hellos):
            raise ValueError("snapshot frozen inputs and hellos do not match stage")
        if rank >= 2 and (self.proposal is None or self.local_approval is None):
            raise ValueError("snapshot proposal and approval do not match stage")
        if rank < 2 and (self.proposal is not None or self.local_approval is not None):
            raise ValueError("snapshot proposal and approval do not match stage")
        if (rank >= 2) != (self.accepted_height is not None):
            raise ValueError("snapshot accepted height does not match stage")
        if rank >= 3 and (self.approval_set is None or self.local_ack is None):
            raise ValueError("snapshot approval set and ACK do not match stage")
        if rank < 3 and (self.approval_set is not None or self.local_ack is not None):
            raise ValueError("snapshot approval set and ACK do not match stage")
        if rank >= 4 and self.prepare is None:
            raise ValueError("snapshot prepare does not match stage")
        if rank < 4 and self.prepare is not None:
            raise ValueError("snapshot prepare does not match stage")
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
        frozen_inputs: tuple[MakerFrozenChannelInput, ...] = (),
        hellos: tuple[ChannelInputHelloPayload, ...] = (),
        proposal: ChannelInputProposalPayload | None = None,
        accepted_height: int | None = None,
        local_approval: ChannelInputApprovalPayload | None = None,
        approval_set: ChannelInputApprovalSetPayload | None = None,
        local_ack: ChannelInputApprovalSetAckPayload | None = None,
        prepare: ChannelInputPreparePayload | None = None,
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
        self.frozen_inputs = frozen_inputs
        self.hellos = hellos
        self.proposal = proposal
        self.accepted_height = accepted_height
        self.local_approval = local_approval
        self.approval_set = approval_set
        self.local_ack = local_ack
        self.prepare = prepare

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
            frozen_inputs=self.frozen_inputs,
            hellos=self.hellos,
            proposal=self.proposal,
            accepted_height=self.accepted_height,
            local_approval=self.local_approval,
            approval_set=self.approval_set,
            local_ack=self.local_ack,
            prepare=self.prepare,
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
            frozen_inputs=snapshot.frozen_inputs,
            hellos=snapshot.hellos,
            proposal=snapshot.proposal,
            accepted_height=snapshot.accepted_height,
            local_approval=snapshot.local_approval,
            approval_set=snapshot.approval_set,
            local_ack=snapshot.local_ack,
            prepare=snapshot.prepare,
        )
        negotiation._validate_restored_state()
        return negotiation

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
        if self.stage is not MakerChannelInputStage.INVITED:
            raise ChannelInputValidationError(
                "frozen inputs are not valid at this negotiation stage"
            )
        _validate_frozen_inputs(self.config, self.invite, self.local_node_key, normalized)
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
            signed = cast(ChannelInputHelloPayload, sign_payload(hello, self._key_pair.secret_key))
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
        signed = cast(
            ChannelInputApprovalPayload, sign_payload(approval, self._key_pair.secret_key)
        )
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
        signed = cast(
            ChannelInputApprovalSetAckPayload, sign_payload(ack, self._key_pair.secret_key)
        )
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
            _validate_frozen_inputs(
                self.config, self.invite, self.local_node_key, self.frozen_inputs
            )
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
        expected_ledger = tuple(
            sorted((slot, _payload_digest(payload)) for slot, payload in expected_entries)
        )
        if self._ledger.snapshot() != expected_ledger:
            raise ChannelInputValidationError("replay ledger does not match negotiation payloads")


def _slot(name: str) -> str:
    return f"maker-channel-input-v1:{name}"


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
