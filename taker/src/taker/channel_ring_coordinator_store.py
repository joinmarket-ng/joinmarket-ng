"""Private, durable taker coordination without a local Lightning endpoint.

The participant journal has a distinct strict schema and binds every record to
an LND node. Keep coordinator-only records in a separate subdirectory under
the same wallet-wide runtime lease, never fabricate participant bindings.
"""

from __future__ import annotations

import json
import os
import stat
import time
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from jmcore.bitcoin import get_txid
from jmcore.channel_ring_store import (
    Outpoint,
    RingChainStatus,
    RingParticipantRecord,
    RingParticipantStore,
    StrictStoreModel,
    TransactionPresence,
)
from jmcore.cofunded_ring import (
    PrivateParticipant,
    RingKeyPair,
    RingManifest,
    RingPlanPayload,
    RingPreparedPayload,
    SignedReadinessAttestation,
)
from pydantic import Field, field_validator, model_validator


class CoordinatorStoreError(Exception):
    """A coordinator journal is unsafe, corrupt, or cannot be written."""


class CoordinatorPhase(StrEnum):
    INVITING = "inviting"
    PLANNING = "planning"
    OPENING = "opening"
    UNSIGNED = "unsigned"
    READY = "ready"
    SIGN_AUTHORIZED = "sign_authorized"
    SIGNING = "signing"
    SIGNED = "signed"
    BROADCAST = "broadcast"
    CONFIRMED = "confirmed"
    RECOVERY_REQUIRED = "recovery_required"
    RETIRED = "retired"


_PHASE_NEXT: dict[CoordinatorPhase, frozenset[CoordinatorPhase]] = {
    CoordinatorPhase.INVITING: frozenset({CoordinatorPhase.PLANNING}),
    CoordinatorPhase.PLANNING: frozenset({CoordinatorPhase.OPENING}),
    CoordinatorPhase.OPENING: frozenset({CoordinatorPhase.UNSIGNED}),
    CoordinatorPhase.UNSIGNED: frozenset({CoordinatorPhase.READY}),
    CoordinatorPhase.READY: frozenset({CoordinatorPhase.SIGN_AUTHORIZED}),
    CoordinatorPhase.SIGN_AUTHORIZED: frozenset({CoordinatorPhase.SIGNING}),
    CoordinatorPhase.SIGNING: frozenset({CoordinatorPhase.SIGNED}),
    CoordinatorPhase.SIGNED: frozenset({CoordinatorPhase.BROADCAST}),
    CoordinatorPhase.BROADCAST: frozenset({CoordinatorPhase.CONFIRMED}),
    CoordinatorPhase.CONFIRMED: frozenset({CoordinatorPhase.RETIRED}),
    CoordinatorPhase.RECOVERY_REQUIRED: frozenset(
        {CoordinatorPhase.CONFIRMED, CoordinatorPhase.RETIRED}
    ),
    CoordinatorPhase.RETIRED: frozenset(),
}


class CoordinatorRecord(StrictStoreModel):
    """Exact per-round coordination evidence, independent of any LND endpoint."""

    journal_kind: Literal["taker_ring_coordinator"] = "taker_ring_coordinator"
    round_nonce: str
    revision: int = Field(default=0, ge=0, le=2_147_483_647)
    signer_secret: str = Field(repr=False)
    signer_key: str
    network: Literal["mainnet", "testnet", "signet", "regtest"]
    wallet_identity: str
    session_identity: str = Field(min_length=1, max_length=256)
    source_mixdepth: int = Field(ge=0, le=31)
    input_outpoints: tuple[Outpoint, ...]
    input_lock_owner: str = Field(min_length=1, max_length=256)
    # Store recipients before sending an invitation, because an interrupted
    # send may have reached the maker even if no hello came back.
    reached_nicks: tuple[str, ...] = ()
    participant_nicks: dict[str, str] = Field(default_factory=dict)
    participants: tuple[PrivateParticipant, ...] = ()
    plans: dict[str, RingPlanPayload] = Field(default_factory=dict)
    prepared: dict[str, RingPreparedPayload] = Field(default_factory=dict)
    unsigned_tx: str | None = None
    unsigned_psbt: str | None = None
    manifest: RingManifest | None = None
    readiness_set: tuple[SignedReadinessAttestation, ...] = ()
    sign_authorization_sent: bool = False
    ready_ack_keys: tuple[str, ...] = ()
    sign_ack_keys: tuple[str, ...] = ()
    canceled_nicks: tuple[str, ...] = ()
    input_signature_creation_intent: bool = False
    input_signature_sent: bool = False
    local_signatures: tuple[str, ...] = ()
    final_tx: str | None = None
    chain_status: RingChainStatus = Field(default_factory=RingChainStatus)
    phase: CoordinatorPhase = CoordinatorPhase.INVITING
    created_at: float = Field(ge=0)
    updated_at: float = Field(ge=0)

    @classmethod
    def fresh(
        cls,
        *,
        signer: RingKeyPair,
        round_nonce: str,
        network: Literal["mainnet", "testnet", "signet", "regtest"],
        wallet_identity: str,
        session_identity: str,
        source_mixdepth: int,
        input_outpoints: tuple[Outpoint, ...],
        input_lock_owner: str,
        reached_nicks: tuple[str, ...],
    ) -> CoordinatorRecord:
        now = time.time()
        return cls(
            round_nonce=round_nonce,
            signer_secret=signer.secret_key.hex(),
            signer_key=signer.public_key,
            network=network,
            wallet_identity=wallet_identity,
            session_identity=session_identity,
            source_mixdepth=source_mixdepth,
            input_outpoints=input_outpoints,
            input_lock_owner=input_lock_owner,
            reached_nicks=reached_nicks,
            created_at=now,
            updated_at=now,
        )

    @field_validator("round_nonce", "signer_secret", "signer_key", "wallet_identity")
    @classmethod
    def validate_hex_key(cls, value: str) -> str:
        if (
            len(value) != 64
            or value != value.lower()
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("coordinator key and round nonce must be lowercase 32-byte hex")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> CoordinatorRecord:
        if RingKeyPair.from_secret(bytes.fromhex(self.signer_secret)).public_key != self.signer_key:
            raise ValueError("coordinator signing key does not match the persisted secret")
        if not self.input_outpoints or len(set(self.input_outpoints)) != len(self.input_outpoints):
            raise ValueError("coordinator inputs must be nonempty and unique")
        if len(set(self.reached_nicks)) != len(self.reached_nicks) or not self.reached_nicks:
            raise ValueError("reached maker list must be nonempty and unique")
        if self.updated_at < self.created_at:
            raise ValueError("coordinator update time precedes creation")
        keys = {participant.participant_key for participant in self.participants}
        if len(keys) != len(self.participants):
            raise ValueError("coordinator participants contain duplicate channel keys")
        if self.signer_key in keys:
            raise ValueError("coordinator signing key cannot identify a channel endpoint")
        if set(self.participant_nicks) != keys or (
            len(set(self.participant_nicks.values())) != len(self.participant_nicks)
            or not set(self.participant_nicks.values()).issubset(self.reached_nicks)
        ):
            raise ValueError("coordinator maker keys do not match reached maker identities")
        if not set(self.plans).issubset(keys) or not set(self.prepared).issubset(keys):
            raise ValueError("coordinator plans or prepared reports have unknown participants")
        if not set(self.ready_ack_keys).issubset(keys) or not set(self.sign_ack_keys).issubset(
            keys
        ):
            raise ValueError("coordinator acknowledgments have unknown participants")
        if self.sign_ack_keys and not self.sign_authorization_sent:
            raise ValueError("sign acknowledgments require durable authorization intent")
        if not set(self.canceled_nicks).issubset(self.reached_nicks):
            raise ValueError("cancellation acknowledgments contain unknown maker identities")
        if self.input_signature_sent and not self.input_signature_creation_intent:
            raise ValueError("sent taker signatures require a durable signing intent")
        if self.manifest is not None and (
            self.manifest.network != self.network
            or self.manifest.round_nonce != self.round_nonce
            or self.manifest.revision != self.revision
            or set(self.manifest.participant_keys) != keys
        ):
            raise ValueError("coordinator manifest does not match the frozen maker cycle")
        if self.unsigned_tx is not None and self.manifest is None:
            raise ValueError("unsigned transaction requires an exact manifest")
        if self.unsigned_tx is not None and self.manifest is not None:
            if get_txid(self.unsigned_tx) != self.manifest.unsigned_txid:
                raise ValueError("unsigned coordinator transaction differs from manifest")
        if self.final_tx is not None and (
            self.manifest is None or get_txid(self.final_tx) != self.manifest.unsigned_txid
        ):
            raise ValueError("final coordinator transaction differs from manifest")
        if self.phase not in {CoordinatorPhase.INVITING, CoordinatorPhase.RECOVERY_REQUIRED} and (
            len(keys) < 3
        ):
            raise ValueError("coordinator needs at least three authenticated channel makers")
        if self.phase is CoordinatorPhase.OPENING and set(self.plans) != keys:
            raise ValueError("opening intent requires every participant plan")
        if self.phase in {
            CoordinatorPhase.UNSIGNED,
            CoordinatorPhase.READY,
            CoordinatorPhase.SIGN_AUTHORIZED,
            CoordinatorPhase.SIGNING,
            CoordinatorPhase.SIGNED,
            CoordinatorPhase.BROADCAST,
            CoordinatorPhase.CONFIRMED,
        } and (set(self.plans) != keys or set(self.prepared) != keys):
            raise ValueError("unsigned distribution requires complete plans and preparation")
        if self.phase in {
            CoordinatorPhase.UNSIGNED,
            CoordinatorPhase.READY,
            CoordinatorPhase.SIGN_AUTHORIZED,
            CoordinatorPhase.SIGNING,
            CoordinatorPhase.SIGNED,
            CoordinatorPhase.BROADCAST,
            CoordinatorPhase.CONFIRMED,
        } and (self.manifest is None or self.unsigned_tx is None or self.unsigned_psbt is None):
            raise ValueError("unsigned distribution requires exact durable funding evidence")
        if (
            self.phase
            in {
                CoordinatorPhase.READY,
                CoordinatorPhase.SIGN_AUTHORIZED,
                CoordinatorPhase.SIGNING,
                CoordinatorPhase.SIGNED,
                CoordinatorPhase.BROADCAST,
            }
            and not self.readiness_set
        ):
            raise ValueError("ready coordinator round requires complete attestations")
        if (
            self.phase
            in {
                CoordinatorPhase.SIGN_AUTHORIZED,
                CoordinatorPhase.SIGNING,
                CoordinatorPhase.SIGNED,
                CoordinatorPhase.BROADCAST,
            }
            and not self.sign_authorization_sent
        ):
            raise ValueError("signing requires durable readiness and authorization intent")
        if (
            self.phase
            in {
                CoordinatorPhase.SIGN_AUTHORIZED,
                CoordinatorPhase.SIGNING,
                CoordinatorPhase.SIGNED,
                CoordinatorPhase.BROADCAST,
            }
            and set(self.ready_ack_keys) != keys
        ):
            raise ValueError("sign authorization requires all maker readiness acknowledgments")
        if (
            self.phase
            in {
                CoordinatorPhase.SIGNING,
                CoordinatorPhase.SIGNED,
                CoordinatorPhase.BROADCAST,
            }
            and set(self.sign_ack_keys) != keys
        ):
            raise ValueError("signing requires all maker signing acknowledgments")
        if self.phase in {CoordinatorPhase.SIGNED, CoordinatorPhase.BROADCAST} and not (
            self.input_signature_creation_intent and self.local_signatures
        ):
            raise ValueError("signing requires a durable taker input signature intent")
        if self.phase in {CoordinatorPhase.SIGNED, CoordinatorPhase.BROADCAST} and not (
            self.input_signature_creation_intent and self.input_signature_sent
        ):
            raise ValueError("signed coordinator round requires a durable taker signature")
        if self.phase is CoordinatorPhase.BROADCAST and self.final_tx is None:
            raise ValueError("broadcast coordinator round requires its exact signed transaction")
        if self.phase is CoordinatorPhase.CONFIRMED and (
            self.manifest is None
            or self.chain_status.exact_txid != self.manifest.unsigned_txid
            or self.chain_status.confirmations < 1
        ):
            raise ValueError("confirmed coordinator round requires an exact confirmed transaction")
        if self.phase is CoordinatorPhase.RETIRED and self.chain_status.confirmations < 1:
            if (
                self.sign_authorization_sent
                or self.input_signature_creation_intent
                or set(self.canceled_nicks) != set(self.reached_nicks)
                or (
                    self.manifest is not None
                    and (
                        self.chain_status.exact_txid != self.manifest.unsigned_txid
                        or self.chain_status.mempool is not TransactionPresence.ABSENT
                        or self.chain_status.chain is not TransactionPresence.ABSENT
                    )
                )
            ):
                raise ValueError(
                    "coordinator retirement requires all cancellations and absence proof"
                )
        return self

    @property
    def filename(self) -> str:
        return f"{self.round_nonce}-{self.revision}-{self.signer_key}.json"

    @property
    def active(self) -> bool:
        return self.phase not in {CoordinatorPhase.CONFIRMED, CoordinatorPhase.RETIRED}

    @property
    def verified_unresolved(self) -> bool:
        """Unknown recovery phase consumes verified capacity conservatively."""
        return self.active and self.phase in {
            CoordinatorPhase.UNSIGNED,
            CoordinatorPhase.READY,
            CoordinatorPhase.SIGN_AUTHORIZED,
            CoordinatorPhase.SIGNING,
            CoordinatorPhase.SIGNED,
            CoordinatorPhase.BROADCAST,
            CoordinatorPhase.RECOVERY_REQUIRED,
        }

    def transition(self, phase: CoordinatorPhase, **updates: Any) -> CoordinatorRecord:
        if phase != self.phase and phase not in _PHASE_NEXT[self.phase]:
            if phase is not CoordinatorPhase.RECOVERY_REQUIRED or not self.active:
                raise CoordinatorStoreError("coordinator phase transition is not allowed")
        for identity in (
            "round_nonce",
            "revision",
            "signer_secret",
            "signer_key",
            "network",
            "wallet_identity",
            "session_identity",
            "source_mixdepth",
            "input_outpoints",
            "input_lock_owner",
            "reached_nicks",
        ):
            if identity in updates:
                raise CoordinatorStoreError("coordinator identity cannot change")
        values = self.model_dump()
        values.update(updates)
        values["phase"] = phase
        values["updated_at"] = time.time()
        return CoordinatorRecord.model_validate(values)


def _no_duplicate_keys(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate coordinator JSON key")
        result[key] = value
    return result


class CoordinatorStore:
    """Strict, atomic coordinator files under the participant directory lease."""

    def __init__(
        self,
        participant_directory: Path,
        *,
        network: Literal["mainnet", "testnet", "signet", "regtest"],
        wallet_identity: str,
        participant_store: RingParticipantStore | None = None,
        max_active_sessions: int = 4,
        max_verified_sessions: int = 2,
    ) -> None:
        if max_active_sessions < 1 or not 1 <= max_verified_sessions <= max_active_sessions:
            raise ValueError("coordinator limits must be positive and verified <= active")
        self.network = network
        self.wallet_identity = wallet_identity
        self.participant_store = participant_store
        self.max_active_sessions = max_active_sessions
        self.max_verified_sessions = max_verified_sessions
        self.directory = participant_directory / "coordinators"
        if self.directory.is_symlink():
            raise CoordinatorStoreError("coordinator directory cannot be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if not stat.S_ISDIR(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
            raise CoordinatorStoreError("coordinator directory is not privately owned")
        os.chmod(self.directory, 0o700)

    def load_all(self) -> tuple[CoordinatorRecord, ...]:
        records = []
        for path in sorted(self.directory.glob("*.json")):
            records.append(self._load(path))
        return tuple(records)

    def _load(self, path: Path) -> CoordinatorRecord:
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise CoordinatorStoreError("coordinator journal has unsafe file permissions")
        try:
            raw = path.read_bytes()
            payload = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
            if not isinstance(payload, dict) or set(payload) != set(CoordinatorRecord.model_fields):
                raise ValueError("coordinator journal fields differ from the strict schema")
            record = CoordinatorRecord.model_validate_json(raw)
        except ValueError as exc:
            raise CoordinatorStoreError("coordinator journal failed strict validation") from exc
        if path.name != record.filename:
            raise CoordinatorStoreError("coordinator journal filename differs from its identity")
        if record.network != self.network or record.wallet_identity != self.wallet_identity:
            raise CoordinatorStoreError(
                "coordinator journal belongs to a different wallet or network"
            )
        return record

    def save(self, record: CoordinatorRecord) -> None:
        # Pydantic model_copy(update=...) does not validate. Do not persist an
        # internally constructed object whose fields violate the strict schema.
        record = CoordinatorRecord.model_validate(record.model_dump())
        if record.network != self.network or record.wallet_identity != self.wallet_identity:
            raise CoordinatorStoreError(
                "coordinator journal belongs to a different wallet or network"
            )
        records = self.load_all()  # Never overwrite a record if any journal is corrupt.
        participant_active = participant_verified = 0
        participant_records: tuple[RingParticipantRecord, ...] = ()
        if self.participant_store is not None:
            report = self.participant_store.load_all()
            if report.corruptions:
                raise CoordinatorStoreError("participant journals are corrupt")
            participant_records = report.records
            participant_active = sum(item.active for item in report.records)
            participant_verified = sum(item.verified_unresolved for item in report.records)
        other_active = sum(item.active for item in records if item.filename != record.filename)
        if record.active:
            claimed = set(record.input_outpoints)
            if any(
                claimed.intersection(item.input_outpoints)
                for item in records
                if item.active and item.filename != record.filename
            ) or any(
                claimed.intersection(item.local_input_outpoints)
                for item in participant_records
                if item.active
            ):
                raise CoordinatorStoreError("coordinator input is claimed by another active ring")
        if record.active and participant_active + other_active >= self.max_active_sessions:
            raise CoordinatorStoreError("coordinator active-session capacity is exhausted")
        verified_phases = {
            CoordinatorPhase.UNSIGNED,
            CoordinatorPhase.READY,
            CoordinatorPhase.SIGN_AUTHORIZED,
            CoordinatorPhase.SIGNING,
            CoordinatorPhase.SIGNED,
            CoordinatorPhase.BROADCAST,
            CoordinatorPhase.RECOVERY_REQUIRED,
        }
        other_verified = sum(
            item.phase in verified_phases
            for item in records
            if item.filename != record.filename and item.active
        )
        if (
            record.active
            and record.phase in verified_phases
            and participant_verified + other_verified >= self.max_verified_sessions
        ):
            raise CoordinatorStoreError("coordinator verified-session capacity is exhausted")
        if any(
            item.round_nonce == record.round_nonce
            and item.revision == record.revision
            and item.signer_key != record.signer_key
            for item in records
        ):
            raise CoordinatorStoreError("coordinator identity differs from durable state")
        existing = next((item for item in records if item.filename == record.filename), None)
        if existing is not None and any(
            getattr(existing, field) != getattr(record, field)
            for field in (
                "session_identity",
                "signer_secret",
                "source_mixdepth",
                "input_outpoints",
                "input_lock_owner",
                "reached_nicks",
            )
        ):
            raise CoordinatorStoreError("coordinator ownership differs from durable state")
        if existing is not None:
            if (
                record.phase != existing.phase
                and record.phase not in _PHASE_NEXT[existing.phase]
                and not (record.phase is CoordinatorPhase.RECOVERY_REQUIRED and existing.active)
            ):
                raise CoordinatorStoreError("coordinator journal cannot regress its phase")
            for evidence in (
                "participants",
                "participant_nicks",
                "plans",
                "prepared",
                "unsigned_tx",
                "unsigned_psbt",
                "manifest",
                "readiness_set",
                "ready_ack_keys",
                "sign_ack_keys",
                "canceled_nicks",
                "local_signatures",
                "final_tx",
            ):
                previous = getattr(existing, evidence)
                if previous and getattr(record, evidence) != previous:
                    raise CoordinatorStoreError(
                        "coordinator journal cannot discard frozen evidence"
                    )
            for marker in (
                "sign_authorization_sent",
                "input_signature_creation_intent",
                "input_signature_sent",
            ):
                if getattr(existing, marker) and not getattr(record, marker):
                    raise CoordinatorStoreError("coordinator journal cannot erase signing intent")
        payload = json.dumps(
            record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        target = self.directory / record.filename
        temporary = self.directory / f".{record.filename}.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(descriptor, view)
                    if written == 0:
                        raise CoordinatorStoreError("coordinator journal write made no progress")
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
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def transition(
        self, filename: str, phase: CoordinatorPhase, **updates: Any
    ) -> CoordinatorRecord:
        if "/" in filename or filename.startswith(".") or not filename.endswith(".json"):
            raise CoordinatorStoreError("invalid coordinator journal filename")
        path = self.directory / filename
        if not path.exists():
            raise CoordinatorStoreError("coordinator journal is missing")
        record = self._load(path).transition(phase, **updates)
        self.save(record)
        return record
