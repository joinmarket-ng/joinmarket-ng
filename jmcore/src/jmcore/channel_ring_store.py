"""Durable local participant state for co-funded channel rings."""

from __future__ import annotations

import json
import os
import stat
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jmcore.cofunded_ring import (
    LocalContribution,
    PreparedIncomingState,
    PreparedOutgoingState,
    PrivateEdgePlan,
    PrivateParticipant,
    ReadinessState,
    RingInvitePayload,
    RingKeyPair,
    RingManifest,
    RingPlanPayload,
    RingPreparedPayload,
    SignedReadinessAttestation,
)


class RingStoreError(Exception):
    """Base error for durable participant state."""


class RingTransitionError(RingStoreError):
    """A lifecycle change violates ordering or point-of-no-return rules."""


class RingAntiGriefError(RingStoreError):
    """A new local record exceeds configured anti-grief limits."""


class RingLifecycleState(StrEnum):
    INVITED = "invited"
    PLANNED = "planned"
    ACCEPTOR_ARMED = "acceptor_armed"
    PREPARED = "prepared"
    PSBT_VERIFIED = "psbt_verified"
    READY = "ready"
    SIGNING = "signing"
    SIGNED = "signed"
    BROADCAST = "broadcast"
    CONFIRMED_OPEN = "confirmed_open"
    RETIRING = "retiring"
    RETIRED = "retired"
    CONFLICTED = "conflicted"
    RECOVERY_REQUIRED = "recovery_required"


class RingParticipantRole(StrEnum):
    TAKER = "taker"
    MAKER = "maker"


class TransactionPresence(StrEnum):
    UNKNOWN = "unknown"
    ABSENT = "absent"
    PRESENT = "present"


class RingRetirementAction(StrEnum):
    SHIM_CANCEL = "shim_cancel"
    ABANDON_VERIFIED = "abandon_verified"
    NORMAL_CHANNEL_RETIREMENT = "normal_channel_retirement"
    BLOCKED = "blocked"


class StrictStoreModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Outpoint(StrictStoreModel):
    txid: str
    vout: int = Field(ge=0, le=0xFFFFFFFF)

    @field_validator("txid")
    @classmethod
    def validate_txid(cls, value: str) -> str:
        _validate_hex(value, 32, "txid")
        return value

    def __str__(self) -> str:
        return f"{self.txid}:{self.vout}"


class FundingEdgeRecord(StrictStoreModel):
    plan: PrivateEdgePlan
    peer: PrivateParticipant
    funding_script_pubkey: str | None = None
    funding_outpoint: Outpoint | None = None

    @field_validator("funding_script_pubkey")
    @classmethod
    def validate_script(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_hex_bytes(value, "funding_script_pubkey")
        return value


class RingChainStatus(StrictStoreModel):
    exact_txid: str | None = None
    mempool: TransactionPresence = TransactionPresence.UNKNOWN
    chain: TransactionPresence = TransactionPresence.UNKNOWN
    confirmations: int = Field(default=0, ge=0)
    confirmed_conflict_txid: str | None = None
    conflict_confirmations: int = Field(default=0, ge=0)
    checked_at: float | None = Field(default=None, ge=0)

    @field_validator("exact_txid", "confirmed_conflict_txid")
    @classmethod
    def validate_optional_txid(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_hex(value, 32, "transaction ID")
        return value

    @model_validator(mode="after")
    def validate_status(self) -> RingChainStatus:
        if self.confirmations and self.chain is not TransactionPresence.PRESENT:
            raise ValueError("confirmations require the exact transaction to be in the chain")
        if self.confirmed_conflict_txid is None and self.conflict_confirmations:
            raise ValueError("conflict confirmations require a conflicting transaction ID")
        if self.confirmed_conflict_txid is not None and self.conflict_confirmations < 1:
            raise ValueError("a conflicting transaction must have at least one confirmation")
        return self


class RingRetryData(StrictStoreModel):
    attempts: int = Field(default=0, ge=0, le=1000)
    next_retry_at: float | None = Field(default=None, ge=0)
    last_error: str | None = Field(default=None, max_length=2000)


class RingRecordKey(StrictStoreModel):
    round_nonce: str
    revision: int = Field(ge=0, le=2_147_483_647)
    participant_public_key: str

    @field_validator("round_nonce", "participant_public_key")
    @classmethod
    def validate_hex_fields(cls, value: str) -> str:
        _validate_hex(value, 32, "record key")
        return value

    @property
    def filename(self) -> str:
        return f"{self.round_nonce}-{self.revision}-{self.participant_public_key}.json"


_PRE_VERIFIED_STATES = frozenset(
    {
        RingLifecycleState.INVITED,
        RingLifecycleState.PLANNED,
        RingLifecycleState.ACCEPTOR_ARMED,
        RingLifecycleState.PREPARED,
    }
)
_VERIFIED_UNRESOLVED_STATES = frozenset(
    {
        RingLifecycleState.PSBT_VERIFIED,
        RingLifecycleState.READY,
        RingLifecycleState.SIGNING,
        RingLifecycleState.SIGNED,
        RingLifecycleState.BROADCAST,
        RingLifecycleState.RETIRING,
        RingLifecycleState.RECOVERY_REQUIRED,
    }
)
_ACTIVE_STATES = frozenset(
    state
    for state in RingLifecycleState
    if state not in {RingLifecycleState.CONFIRMED_OPEN, RingLifecycleState.RETIRED}
)
_ALLOWED_TRANSITIONS: Mapping[RingLifecycleState, frozenset[RingLifecycleState]] = {
    RingLifecycleState.INVITED: frozenset(
        {
            RingLifecycleState.PLANNED,
            RingLifecycleState.RETIRING,
            RingLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    RingLifecycleState.PLANNED: frozenset(
        {
            RingLifecycleState.ACCEPTOR_ARMED,
            RingLifecycleState.RETIRING,
            RingLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    RingLifecycleState.ACCEPTOR_ARMED: frozenset(
        {
            RingLifecycleState.PREPARED,
            RingLifecycleState.RETIRING,
            RingLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    RingLifecycleState.PREPARED: frozenset(
        {
            RingLifecycleState.PSBT_VERIFIED,
            RingLifecycleState.RETIRING,
            RingLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    RingLifecycleState.PSBT_VERIFIED: frozenset(
        {
            RingLifecycleState.READY,
            RingLifecycleState.RETIRING,
            RingLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    RingLifecycleState.READY: frozenset(
        {
            RingLifecycleState.SIGNING,
            RingLifecycleState.RETIRING,
            RingLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    RingLifecycleState.SIGNING: frozenset(
        {
            RingLifecycleState.SIGNED,
            RingLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    RingLifecycleState.SIGNED: frozenset(
        {
            RingLifecycleState.BROADCAST,
            RingLifecycleState.CONFIRMED_OPEN,
            RingLifecycleState.CONFLICTED,
            RingLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    RingLifecycleState.BROADCAST: frozenset(
        {
            RingLifecycleState.CONFIRMED_OPEN,
            RingLifecycleState.CONFLICTED,
            RingLifecycleState.RECOVERY_REQUIRED,
        }
    ),
    RingLifecycleState.CONFIRMED_OPEN: frozenset(
        {RingLifecycleState.RETIRING, RingLifecycleState.RETIRED}
    ),
    RingLifecycleState.RETIRING: frozenset(
        {RingLifecycleState.RETIRED, RingLifecycleState.RECOVERY_REQUIRED}
    ),
    RingLifecycleState.RETIRED: frozenset(),
    RingLifecycleState.CONFLICTED: frozenset(
        {RingLifecycleState.RETIRING, RingLifecycleState.RECOVERY_REQUIRED}
    ),
    RingLifecycleState.RECOVERY_REQUIRED: frozenset(
        {
            RingLifecycleState.CONFIRMED_OPEN,
            RingLifecycleState.CONFLICTED,
            RingLifecycleState.RETIRING,
        }
    ),
}


class RingParticipantRecord(StrictStoreModel):
    """Complete private state for one local participant in one ring revision."""

    round_nonce: str
    revision: int = Field(ge=0, le=2_147_483_647)
    ring_secret: str = Field(repr=False)
    ring_public_key: str
    taker_session_identity: str = Field(min_length=1, max_length=256)
    local_role: RingParticipantRole
    local_position: int = Field(ge=0, le=31)
    invite: RingInvitePayload | None = None
    plan: RingPlanPayload | None = None
    plan_hash: str | None = None
    local_contribution: LocalContribution | None = None
    local_input_outpoints: tuple[Outpoint, ...] = ()
    input_lock_owner: str | None = Field(default=None, min_length=1, max_length=256)
    incoming_edge: FundingEdgeRecord | None = None
    outgoing_edge: FundingEdgeRecord | None = None
    outgoing_open_started: bool = False
    prepared_outgoing: PreparedOutgoingState | None = None
    prepared_incoming: PreparedIncomingState | None = None
    outgoing_funding_address: str | None = Field(default=None, max_length=200)
    outgoing_pending_state: ReadinessState | None = None
    incoming_pending_state: ReadinessState | None = None
    pending_channel_ids: tuple[str, ...] = ()
    unsigned_psbt: str | None = None
    unsigned_tx: str | None = None
    manifest: RingManifest | None = None
    local_readiness: tuple[SignedReadinessAttestation, ...] = ()
    readiness_set: tuple[SignedReadinessAttestation, ...] = ()
    readiness_hash: str | None = None
    coordinator_participants: tuple[PrivateParticipant, ...] = ()
    coordinator_plans: dict[str, RingPlanPayload] = Field(default_factory=dict)
    coordinator_prepared: dict[str, RingPreparedPayload] = Field(default_factory=dict)
    coordinator_reached_keys: tuple[str, ...] = ()
    coordinator_ready_ack_keys: tuple[str, ...] = ()
    coordinator_sign_ack_keys: tuple[str, ...] = ()
    coordinator_sign_authorization_sent: bool = False
    coordinator_tx_fee: int | None = Field(default=None, ge=0)
    handled_requests: dict[str, str] = Field(default_factory=dict)
    handled_responses: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    local_signatures: tuple[str, ...] = ()
    local_input_signature_created: bool = False
    local_input_signature_sent: bool = False
    final_tx: str | None = None
    chain_status: RingChainStatus = Field(default_factory=RingChainStatus)
    created_at: float = Field(ge=0)
    updated_at: float = Field(ge=0)
    retry: RingRetryData = Field(default_factory=RingRetryData)
    state: RingLifecycleState = RingLifecycleState.INVITED

    @classmethod
    def fresh(
        cls,
        *,
        round_nonce: str,
        revision: int,
        taker_session_identity: str,
        local_role: RingParticipantRole,
        local_position: int,
        local_input_outpoints: tuple[Outpoint, ...] = (),
        input_lock_owner: str | None = None,
        now: float | None = None,
    ) -> RingParticipantRecord:
        key_pair = RingKeyPair.generate()
        timestamp = time.time() if now is None else now
        return cls(
            round_nonce=round_nonce,
            revision=revision,
            ring_secret=key_pair.secret_key.hex(),
            ring_public_key=key_pair.public_key,
            taker_session_identity=taker_session_identity,
            local_role=local_role,
            local_position=local_position,
            local_input_outpoints=local_input_outpoints,
            input_lock_owner=input_lock_owner,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @property
    def key(self) -> RingRecordKey:
        return RingRecordKey(
            round_nonce=self.round_nonce,
            revision=self.revision,
            participant_public_key=self.ring_public_key,
        )

    @property
    def active(self) -> bool:
        return self.state in _ACTIVE_STATES

    @property
    def verified_unresolved(self) -> bool:
        return self.state in _VERIFIED_UNRESOLVED_STATES

    def can_transition_to(self, state: RingLifecycleState) -> bool:
        return state == self.state or state in _ALLOWED_TRANSITIONS[self.state]

    def retirement_action(self) -> RingRetirementAction:
        if self.state in _PRE_VERIFIED_STATES:
            return RingRetirementAction.SHIM_CANCEL
        if self.state in {
            RingLifecycleState.PSBT_VERIFIED,
            RingLifecycleState.READY,
        }:
            status = self.chain_status
            if (
                not self.local_input_signature_created
                and not self.local_input_signature_sent
                and status.exact_txid is not None
                and status.mempool is TransactionPresence.ABSENT
                and status.chain is TransactionPresence.ABSENT
            ):
                return RingRetirementAction.ABANDON_VERIFIED
        if (
            self.state is RingLifecycleState.CONFLICTED
            and self.chain_status.confirmed_conflict_txid is not None
        ):
            if self.chain_status.exact_txid is None and not self.local_input_signature_created:
                return RingRetirementAction.SHIM_CANCEL
            return RingRetirementAction.ABANDON_VERIFIED
        if self.state is RingLifecycleState.RECOVERY_REQUIRED:
            if (
                self.local_input_signature_created
                or self.local_input_signature_sent
                or self.final_tx is not None
            ):
                return RingRetirementAction.BLOCKED
            status = self.chain_status
            if (
                status.exact_txid is not None
                and status.mempool is TransactionPresence.ABSENT
                and status.chain is TransactionPresence.ABSENT
            ):
                return RingRetirementAction.ABANDON_VERIFIED
            if status.exact_txid is None and self.unsigned_psbt is None:
                return RingRetirementAction.SHIM_CANCEL
        if self.state is RingLifecycleState.CONFIRMED_OPEN:
            return RingRetirementAction.NORMAL_CHANNEL_RETIREMENT
        return RingRetirementAction.BLOCKED

    @field_validator(
        "round_nonce",
        "ring_secret",
        "ring_public_key",
        "plan_hash",
        "readiness_hash",
    )
    @classmethod
    def validate_hex32(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_hex(value, 32, "record field")
        return value

    @field_validator("pending_channel_ids")
    @classmethod
    def validate_pending_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for pending_id in value:
            _validate_hex(pending_id, 32, "pending_channel_id")
        if len(set(value)) != len(value):
            raise ValueError("pending_channel_ids must be unique")
        return value

    @field_validator("unsigned_psbt")
    @classmethod
    def validate_psbt(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("70736274ff"):
            raise ValueError("unsigned_psbt must be lowercase hex beginning with PSBT magic")
        if value is not None:
            _validate_hex_bytes(value, "unsigned_psbt")
        return value

    @field_validator("unsigned_tx", "final_tx")
    @classmethod
    def validate_transaction_hex(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_hex_bytes(value, "transaction")
        return value

    @model_validator(mode="after")
    def validate_record(self) -> RingParticipantRecord:
        key_pair = RingKeyPair.from_secret(bytes.fromhex(self.ring_secret))
        if key_pair.public_key != self.ring_public_key:
            raise ValueError("ring_public_key does not match ring_secret")
        _validate_hex(self.round_nonce, 32, "round_nonce")
        if (
            self.local_contribution is not None
            and self.local_contribution.participant_key != self.ring_public_key
        ):
            raise ValueError("local contribution belongs to another ring key")
        if len(set(self.local_input_outpoints)) != len(self.local_input_outpoints):
            raise ValueError("local_input_outpoints must be unique")
        edge_pending = {
            edge.plan.pending_channel_id
            for edge in (self.incoming_edge, self.outgoing_edge)
            if edge is not None
        }
        if edge_pending and edge_pending != set(self.pending_channel_ids):
            raise ValueError("pending_channel_ids must exactly match local edge records")
        if self.local_input_signature_sent and not self.local_input_signature_created:
            raise ValueError("a local input signature cannot be sent before it is created")
        if self.invite is not None and (
            self.invite.round_nonce != self.round_nonce
            or self.invite.revision != self.revision
            or self.invite.signer_key == self.ring_public_key
        ):
            raise ValueError("invite does not match participant record identity")
        if self.plan is not None and (
            self.plan.round_nonce != self.round_nonce
            or self.plan.revision != self.revision
            or self.plan.contribution.participant_key != self.ring_public_key
            or self.plan.position != self.local_position
        ):
            raise ValueError("plan does not match participant record identity")
        if self.manifest is not None and (
            self.manifest.round_nonce != self.round_nonce
            or self.manifest.revision != self.revision
            or self.ring_public_key not in self.manifest.participant_keys
        ):
            raise ValueError("manifest does not match participant record identity")
        if self.local_readiness and len(self.local_readiness) != 2:
            raise ValueError("local readiness must contain both endpoint attestations")
        coordinator_keys = [item.participant_key for item in self.coordinator_participants]
        if len(set(coordinator_keys)) != len(coordinator_keys):
            raise ValueError("coordinator participants must be unique")
        if self.coordinator_plans and set(self.coordinator_plans) != set(coordinator_keys):
            raise ValueError("coordinator plans must cover every participant")
        for field_name, keys in (
            ("coordinator_reached_keys", self.coordinator_reached_keys),
            ("coordinator_ready_ack_keys", self.coordinator_ready_ack_keys),
            ("coordinator_sign_ack_keys", self.coordinator_sign_ack_keys),
        ):
            if len(set(keys)) != len(keys) or not set(keys).issubset(coordinator_keys):
                raise ValueError(f"{field_name} contains duplicate or unknown participants")
        if self.coordinator_prepared and not set(self.coordinator_prepared).issubset(
            coordinator_keys
        ):
            raise ValueError("coordinator prepared records contain an unknown participant")
        if self.coordinator_sign_ack_keys and not self.coordinator_sign_authorization_sent:
            raise ValueError("sign acknowledgments require durable signing authorization")
        for message_type, request in self.handled_requests.items():
            if not message_type.startswith("ring_") or not request.startswith("{"):
                raise ValueError("handled request cache is malformed")
        if set(self.handled_responses) - set(self.handled_requests):
            raise ValueError("handled responses require a matching handled request")
        if self.state in {
            RingLifecycleState.SIGNED,
            RingLifecycleState.BROADCAST,
            RingLifecycleState.CONFIRMED_OPEN,
        }:
            if not self.local_input_signature_created or not self.local_input_signature_sent:
                raise ValueError("signed lifecycle states require the local signature to be sent")
            if not self.local_signatures:
                raise ValueError("signed lifecycle states require durable local signatures")
        if self.state is RingLifecycleState.CONFIRMED_OPEN:
            if self.chain_status.exact_txid is None:
                raise ValueError("confirmed/open requires the exact final transaction ID")
            if (
                self.chain_status.chain is not TransactionPresence.PRESENT
                or self.chain_status.confirmations < 1
            ):
                raise ValueError("confirmed/open requires a confirmed exact transaction")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self

    def transition(
        self,
        state: RingLifecycleState,
        *,
        now: float | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> RingParticipantRecord:
        if state == self.state and not updates:
            return self
        if state != self.state and state not in _ALLOWED_TRANSITIONS[self.state]:
            raise RingTransitionError(
                f"transition {self.state.value} -> {state.value} is not allowed"
            )
        values = self.model_dump()
        if updates:
            immutable = {"round_nonce", "revision", "ring_secret", "ring_public_key"}
            changed_immutable = immutable.intersection(updates)
            if changed_immutable:
                raise RingTransitionError(
                    f"record identity fields cannot change: {sorted(changed_immutable)}"
                )
            values.update(updates)
        values["state"] = state
        values["updated_at"] = time.time() if now is None else now
        candidate = RingParticipantRecord(**values)
        if state is RingLifecycleState.RETIRING and state != self.state:
            self._validate_retirement(candidate)
        if (
            state is RingLifecycleState.CONFLICTED
            and candidate.chain_status.confirmed_conflict_txid is None
        ):
            raise RingTransitionError("conflicted requires a confirmed conflicting transaction")
        return candidate

    def _validate_retirement(self, candidate: RingParticipantRecord) -> None:
        if self.state in _PRE_VERIFIED_STATES or self.state is RingLifecycleState.CONFIRMED_OPEN:
            return
        if self.state is RingLifecycleState.CONFLICTED:
            if candidate.chain_status.confirmed_conflict_txid is None:
                raise RingTransitionError("retirement after signing requires a confirmed conflict")
            return
        if self.state is RingLifecycleState.RECOVERY_REQUIRED:
            if self.retirement_action() is RingRetirementAction.BLOCKED:
                raise RingTransitionError("recovery state lacks safe retirement evidence")
            return
        if self.state in {
            RingLifecycleState.PSBT_VERIFIED,
            RingLifecycleState.READY,
        }:
            if candidate.local_input_signature_created or candidate.local_input_signature_sent:
                raise RingTransitionError(
                    "verified retirement is forbidden after creating a signature"
                )
            status = candidate.chain_status
            if status.mempool is not TransactionPresence.ABSENT:
                raise RingTransitionError(
                    "verified retirement requires exact TXID absent from mempool"
                )
            if status.chain is not TransactionPresence.ABSENT:
                raise RingTransitionError(
                    "verified retirement requires exact TXID absent from chain"
                )
            if status.exact_txid is None:
                raise RingTransitionError("verified retirement requires the exact unsigned TXID")
            return
        raise RingTransitionError("signed or unresolved recovery state cannot be retired")


@dataclass(frozen=True)
class CorruptRingRecord:
    path: Path
    error: str


@dataclass(frozen=True)
class RingLoadReport:
    records: tuple[RingParticipantRecord, ...]
    corruptions: tuple[CorruptRingRecord, ...]


class RingParticipantStore:
    """Atomic JSON store with strict permissions and anti-grief indexing."""

    def __init__(
        self,
        directory: Path,
        *,
        max_active_sessions: int,
        max_verified_sessions: int,
    ) -> None:
        if max_active_sessions < 1 or max_verified_sessions < 1:
            raise ValueError("store limits must be positive")
        if max_verified_sessions > max_active_sessions:
            raise ValueError("verified limit cannot exceed active limit")
        self.directory = directory
        self.max_active_sessions = max_active_sessions
        self.max_verified_sessions = max_verified_sessions
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        if self.directory.is_symlink():
            raise RingStoreError("participant store directory cannot be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise RingStoreError("participant store path is not a directory")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise RingStoreError("participant store directory is owned by another user")
        os.chmod(self.directory, 0o700)

    def load(self, key: RingRecordKey) -> RingParticipantRecord | None:
        path = self.directory / key.filename
        if not path.exists():
            return None
        return self._load_path(path)

    def load_all(self) -> RingLoadReport:
        records: list[RingParticipantRecord] = []
        corruptions: list[CorruptRingRecord] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(self._load_path(path))
            except (OSError, ValueError, RingStoreError) as exc:
                corruptions.append(CorruptRingRecord(path=path, error=str(exc)))
        return RingLoadReport(records=tuple(records), corruptions=tuple(corruptions))

    def _load_path(self, path: Path) -> RingParticipantRecord:
        if path.is_symlink():
            raise RingStoreError("participant record cannot be a symlink")
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o600:
            raise RingStoreError(f"participant record has unsafe mode {mode:o}")
        try:
            record = RingParticipantRecord.model_validate_json(path.read_bytes())
        except ValueError as exc:
            raise RingStoreError("participant record failed strict validation") from exc
        if path.name != record.key.filename:
            raise RingStoreError("participant record filename differs from its identity")
        return record

    def save(self, record: RingParticipantRecord) -> None:
        current = self.load_all()
        if current.corruptions:
            raise RingStoreError("cannot update participant store while corrupt records exist")
        existing = [item for item in current.records if item.key != record.key]
        self._enforce_limits(record, existing)
        payload = json.dumps(
            record.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        target = self.directory / record.key.filename
        temporary = self.directory / f".{record.key.filename}.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written == 0:
                        raise OSError("participant record write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
            os.chmod(target, 0o600)
            directory_fd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def reconcile(self, records: Iterable[RingParticipantRecord]) -> RingLoadReport:
        """Idempotently persist recovered records without overwriting divergent state."""
        for record in records:
            existing = self.load(record.key)
            if existing is None:
                self.save(record)
            elif existing != record:
                raise RingStoreError(f"recovered record conflicts with durable state: {record.key}")
        return self.load_all()

    def transition(
        self,
        key: RingRecordKey,
        state: RingLifecycleState,
        *,
        now: float | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> RingParticipantRecord:
        current = self.load(key)
        if current is None:
            raise RingStoreError("participant record does not exist")
        changed = current.transition(state, now=now, updates=updates)
        self.save(changed)
        return changed

    def active_records(
        self,
        *,
        taker_session_identity: str | None = None,
        input_outpoint: Outpoint | None = None,
    ) -> tuple[RingParticipantRecord, ...]:
        records = self.load_all().records
        return tuple(
            record
            for record in records
            if record.active
            and (
                taker_session_identity is None
                or record.taker_session_identity == taker_session_identity
            )
            and (input_outpoint is None or input_outpoint in record.local_input_outpoints)
        )

    def _enforce_limits(
        self,
        candidate: RingParticipantRecord,
        existing: list[RingParticipantRecord],
    ) -> None:
        active = [record for record in existing if record.active]
        verified = [record for record in existing if record.verified_unresolved]
        if candidate.active and len(active) >= self.max_active_sessions:
            raise RingAntiGriefError("maximum active channel-ring sessions reached")
        if candidate.verified_unresolved and len(verified) >= self.max_verified_sessions:
            raise RingAntiGriefError("maximum verified channel-ring sessions reached")
        for record in active:
            shared_inputs = set(record.local_input_outpoints).intersection(
                candidate.local_input_outpoints
            )
            if shared_inputs:
                raise RingAntiGriefError("input outpoint already belongs to an active session")
        for record in existing:
            same_round_participant = (
                record.round_nonce == candidate.round_nonce
                and record.taker_session_identity == candidate.taker_session_identity
                and record.revision != candidate.revision
            )
            if same_round_participant and record.verified_unresolved:
                raise RingAntiGriefError(
                    "cannot create another revision while verified state is unresolved"
                )


def _validate_hex(value: str, byte_length: int, field_name: str) -> None:
    if len(value) != byte_length * 2 or value.lower() != value:
        raise ValueError(f"{field_name} must be {byte_length}-byte lowercase hex")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid lowercase hex") from exc


def _validate_hex_bytes(value: str, field_name: str) -> None:
    if not value or len(value) % 2 or value.lower() != value:
        raise ValueError(f"{field_name} must be non-empty lowercase even-length hex")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid lowercase hex") from exc
