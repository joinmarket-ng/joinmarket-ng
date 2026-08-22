"""Fail-closed durable coordinator for final-Taproot channel parent inputs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol

from coincurve import PublicKey, PublicKeyXOnly
from jmcore.bitcoin import (
    get_txid,
    parse_transaction,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)
from jmcore.channel_input import (
    ChannelInputPreparePayload,
    ChannelInputValidationError,
    EndpointTerms,
    InputPrevout,
    Outpoint,
    endpoint_terms_hash,
    validate_prepare_payload,
    validate_revealed_endpoint_pair,
    validate_transcript_against_transaction,
)
from jmswap.lnd import (
    ChannelInputForceCloseOutcome,
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
    WitnessUtxo,
)
from jmwallet.wallet.signing import (
    create_p2wpkh_script_code,
    verify_p2tr_signature,
    verify_p2wpkh_signature,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]+$")]
Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
MAX_PARENT_FEE_SAT = 1_000_000
MAX_CLAIM_ADJUSTMENT_SAT = 1_000_000
MAX_PARENT_RECORDS = 10_000
MAX_PARENT_RECORD_BYTES = 4 * 1024 * 1024
_PARENT_RECORD_NAME = re.compile(r"^[0-9a-f]{64}\.json$")
_PARENT_TEMP_NAME = re.compile(r"^\.[0-9a-f]{64}\.[0-9]+\.[0-9]+\.tmp$")


class ChannelInputParentError(Exception):
    """The immutable parent cannot safely advance."""


class ChannelInputParentState(StrEnum):
    CREATED = "created"
    CHANNELS_FROZEN = "channels_frozen"
    PARENT_PREPARED = "parent_prepared"
    ORDINARY_SIGNED = "ordinary_signed"
    CHANNEL_SIGNING = "channel_signing"
    CHANNEL_SIGNED = "channel_signed"
    FINAL_TX_PERSISTED = "final_tx_persisted"
    BROADCAST_ATTEMPTED = "broadcast_attempted"
    OBSERVED = "observed"
    CONFIRMED = "confirmed"
    CANCELED = "canceled"
    RECOVERY_REQUIRED = "recovery_required"
    FORCE_CLOSED = "force_closed"


class ChannelInputRestartAction(StrEnum):
    NONE = "none"
    CANCEL_PRE_SIGN = "cancel_pre_sign"
    RESUME_EXACT_PARENT = "resume_exact_parent"
    PUBLISH_EXACT_PARENT = "publish_exact_parent"
    FORCE_CLOSE = "force_close"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _hex(value: str, name: str, *, length: int | None = None) -> str:
    if (
        value != value.lower()
        or len(value) % 2
        or (length is not None and len(value) != length * 2)
    ):
        raise ValueError(f"{name} must be lowercase hexadecimal")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be lowercase hexadecimal") from exc
    return value


def _compressed_pubkey(value: str, name: str) -> str:
    _hex(value, name, length=33)
    if not value.startswith(("02", "03")):
        raise ValueError(f"{name} is not compressed")
    try:
        PublicKey(bytes.fromhex(value))
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid secp256k1 public key") from exc
    return value


def _xonly_pubkey(value: str, name: str) -> str:
    _hex(value, name, length=32)
    try:
        PublicKeyXOnly(bytes.fromhex(value))
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid BIP340 public key") from exc
    return value


class ParentPrevout(_StrictModel):
    value_sat: int = Field(gt=0)
    script_pubkey: Hex

    @field_validator("script_pubkey")
    @classmethod
    def validate_script(cls, value: str) -> str:
        _hex(value, "script_pubkey")
        WitnessUtxo(value_sat=1, script_pubkey=bytes.fromhex(value))
        return value

    def witness_utxo(self) -> WitnessUtxo:
        return WitnessUtxo(
            value_sat=self.value_sat, script_pubkey=bytes.fromhex(self.script_pubkey)
        )


class SettlementOutput(_StrictModel):
    output_index: int = Field(ge=0)
    value_sat: int = Field(gt=0)
    script_pubkey: Hex

    @field_validator("script_pubkey")
    @classmethod
    def validate_script(cls, value: str) -> str:
        return _hex(value, "settlement script")


class EndpointContribution(_StrictModel):
    endpoint_id: str = Field(min_length=1, max_length=128)
    participant_key: Hex32
    settlement_outputs: tuple[SettlementOutput, ...] = Field(min_length=1)
    miner_fee_share_sat: int = Field(ge=0)
    claim_adjustment_sat: int = Field(
        default=0, ge=-MAX_CLAIM_ADJUSTMENT_SAT, le=MAX_CLAIM_ADJUSTMENT_SAT
    )

    @field_validator("participant_key")
    @classmethod
    def validate_participant_key(cls, value: str) -> str:
        return _xonly_pubkey(value, "participant_key")


class ChannelContribution(_StrictModel):
    channel_id: str = Field(min_length=1, max_length=256)
    channel_input_index: int = Field(ge=0)
    first: EndpointContribution
    second: EndpointContribution

    @model_validator(mode="after")
    def validate_endpoints(self) -> ChannelContribution:
        if self.first.endpoint_id == self.second.endpoint_id:
            raise ValueError("channel endpoints must have distinct identities")
        if self.first.claim_adjustment_sat + self.second.claim_adjustment_sat != 0:
            raise ValueError("channel claim adjustments must sum to zero")
        return self


class OrdinaryWitness(_StrictModel):
    input_index: int = Field(ge=0)
    witness: tuple[Hex, ...] = Field(min_length=1)

    @field_validator("witness")
    @classmethod
    def validate_witness(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_hex(item, "ordinary witness") for item in value)


class ChannelInputAuthorization(_StrictModel):
    """Durable signed prepare authorization and every committed endpoint term."""

    prepare: ChannelInputPreparePayload
    endpoint_terms: tuple[EndpointTerms, ...] = Field(min_length=2, max_length=64)

    @model_validator(mode="after")
    def validate_authorization(self) -> ChannelInputAuthorization:
        try:
            validate_prepare_payload(self.prepare)
            transcript = self.prepare.approval_set.transcript
            terms_by_hash = {
                endpoint_terms_hash(terms).hex(): terms for terms in self.endpoint_terms
            }
            if len(terms_by_hash) != len(self.endpoint_terms):
                raise ChannelInputValidationError("endpoint terms hashes must be unique")
            if set(terms_by_hash) != transcript.all_terms_hashes():
                raise ChannelInputValidationError(
                    "endpoint terms hashes do not exactly match transcript"
                )
            for descriptor in transcript.channels:
                validate_revealed_endpoint_pair(
                    transcript,
                    descriptor,
                    terms_by_hash[descriptor.first_terms_hash],
                    terms_by_hash[descriptor.second_terms_hash],
                )
        except ChannelInputValidationError as exc:
            raise ValueError(str(exc)) from exc
        return self

    def terms_for(
        self, first_terms_hash: str, second_terms_hash: str
    ) -> tuple[EndpointTerms, EndpointTerms]:
        terms_by_hash = {endpoint_terms_hash(terms).hex(): terms for terms in self.endpoint_terms}
        return terms_by_hash[first_terms_hash], terms_by_hash[second_terms_hash]


class ChannelInputParentPlan(_StrictModel):
    session_id: str = Field(min_length=1, max_length=256)
    unsigned_parent_hex: Hex
    prevouts: tuple[ParentPrevout, ...] = Field(min_length=1)
    channels: tuple[ChannelContribution, ...] = Field(min_length=1)
    ordinary_witnesses: tuple[OrdinaryWitness, ...]
    authorization: ChannelInputAuthorization
    maximum_miner_fee_sat: int = Field(default=MAX_PARENT_FEE_SAT, gt=0, le=MAX_PARENT_FEE_SAT)

    @field_validator("unsigned_parent_hex")
    @classmethod
    def validate_parent_hex(cls, value: str) -> str:
        return _hex(value, "unsigned parent")


class FrozenSnapshot(_StrictModel):
    funding_txid: Hex32
    funding_vout: int = Field(ge=0, le=0xFFFFFFFF)
    funding_script_pubkey: Hex
    capacity_sat: int = Field(gt=0)
    local_claim_sat: int = Field(ge=0)
    remote_claim_sat: int = Field(ge=0)
    peer_node_pubkey: Hex
    local_funding_pubkey: Hex
    remote_funding_pubkey: Hex

    @field_validator("funding_script_pubkey")
    @classmethod
    def validate_funding_script(cls, value: str) -> str:
        _hex(value, "frozen funding script", length=34)
        if not value.startswith("5120"):
            raise ValueError("frozen funding script is not P2TR")
        return value

    @field_validator("peer_node_pubkey", "local_funding_pubkey", "remote_funding_pubkey")
    @classmethod
    def validate_funding_pubkey(cls, value: str) -> str:
        return _compressed_pubkey(value, "frozen public key")

    @model_validator(mode="after")
    def validate_claims(self) -> FrozenSnapshot:
        if self.local_claim_sat + self.remote_claim_sat != self.capacity_sat:
            raise ValueError("frozen claims do not conserve capacity")
        return self

    @classmethod
    def from_lnd(cls, snapshot: FrozenChannelSnapshot) -> FrozenSnapshot:
        return cls(
            funding_txid=snapshot.funding_outpoint.txid,
            funding_vout=snapshot.funding_outpoint.vout,
            funding_script_pubkey=snapshot.funding_scriptpubkey.hex(),
            capacity_sat=snapshot.capacity_sat,
            local_claim_sat=snapshot.local_claim_sat,
            remote_claim_sat=snapshot.remote_claim_sat,
            peer_node_pubkey=snapshot.peer_node_pubkey.hex(),
            local_funding_pubkey=snapshot.local_funding_pubkey.hex(),
            remote_funding_pubkey=snapshot.remote_funding_pubkey.hex(),
        )


class EndpointEvidence(_StrictModel):
    attempt_id: Hex32
    public_nonce: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{132}$")]
    partial_signature: Hex32 | None = None


class ChannelRecord(_StrictModel):
    channel_id: str
    channel_input_index: int
    first_snapshot: FrozenSnapshot
    second_snapshot: FrozenSnapshot
    first_evidence: EndpointEvidence | None = None
    second_evidence: EndpointEvidence | None = None
    first_recover_only: bool = False
    second_recover_only: bool = False
    witness: Hex | None = None


class ChainObservation(_StrictModel):
    confirmations: int = Field(ge=0)


class ChannelInputForceCloseRecord(_StrictModel):
    funding_txid: Hex32
    funding_vout: int = Field(ge=0, le=0xFFFFFFFF)
    commitment_txid: Hex32

    @classmethod
    def from_outcome(cls, outcome: ChannelInputForceCloseOutcome) -> ChannelInputForceCloseRecord:
        return cls(
            funding_txid=outcome.funding_outpoint.txid,
            funding_vout=outcome.funding_outpoint.vout,
            commitment_txid=outcome.commitment_txid,
        )


class ChannelInputParentRecord(_StrictModel):
    record_id: Hex32
    plan: ChannelInputParentPlan
    state: ChannelInputParentState
    channels: tuple[ChannelRecord, ...] = ()
    final_tx_hex: Hex | None = None
    final_txid: Hex32 | None = None
    observation: ChainObservation | None = None
    force_closes: tuple[ChannelInputForceCloseRecord, ...] = ()
    updated_at_ns: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_force_closes(self) -> ChannelInputParentRecord:
        points = [(item.funding_txid, item.funding_vout) for item in self.force_closes]
        if len(points) != len(set(points)):
            raise ValueError("force-close records contain duplicate funding outpoints")
        expected = {
            (channel.second_snapshot.funding_txid, channel.second_snapshot.funding_vout)
            for channel in self.channels
        }
        if not set(points).issubset(expected):
            raise ValueError("force-close records contain an unknown funding outpoint")
        if self.force_closes and self.state not in {
            ChannelInputParentState.RECOVERY_REQUIRED,
            ChannelInputParentState.FORCE_CLOSED,
            ChannelInputParentState.OBSERVED,
            ChannelInputParentState.CONFIRMED,
        }:
            raise ValueError("force-close records conflict with parent state")
        if self.state is ChannelInputParentState.FORCE_CLOSED and (
            not expected or set(points) != expected
        ):
            raise ValueError("force-closed parent lacks exact channel coverage")
        return self


class ChannelInputEndpoint(Protocol):
    """Transport boundary compatible with the ChannelEscrowRPC LND methods."""

    async def freeze_channel_input(
        self, session_id: str, channel_id: str
    ) -> FrozenChannelSnapshot: ...

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo],
        channel_input_index: int,
    ) -> str: ...

    async def begin_channel_input_signing(
        self, session_id: str, funding_outpoint: EscrowOutpoint, *, restart_attempt: bool = False
    ) -> LndEscrowSigningAttempt: ...

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes: ...

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent: ...

    async def recover_channel_input_parent(
        self, session_id: str, funding_outpoint: EscrowOutpoint, remote_partial_signature: bytes
    ) -> LndFinalizedEscrowParent: ...

    async def channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus: ...

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool: ...


class ChainTransaction(Protocol):
    txid: str
    raw: str
    confirmations: int


class ChannelInputParentChain(Protocol):
    async def get_transaction(self, txid: str) -> ChainTransaction | None: ...

    async def broadcast_transaction(self, tx_hex: str) -> str: ...


class _Store:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._ensure_directory()

    def _ensure_directory(self) -> None:
        if self.directory.is_symlink():
            raise ChannelInputParentError("channel-parent store directory cannot be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise ChannelInputParentError("channel-parent store path is not a directory")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ChannelInputParentError("channel-parent store directory has another owner")
        os.chmod(self.directory, 0o700)

    def _path(self, record_id: str) -> Path:
        return self.directory / f"{record_id}.json"

    def load(self, record_id: str) -> ChannelInputParentRecord | None:
        path = self._path(record_id)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ChannelInputParentError("could not inspect channel-parent record") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ChannelInputParentError("channel-parent record is unsafe")
        if info.st_size > MAX_PARENT_RECORD_BYTES:
            raise ChannelInputParentError("channel-parent record exceeds size limit")
        try:
            payload = path.read_bytes()
            if len(payload) > MAX_PARENT_RECORD_BYTES:
                raise ChannelInputParentError("channel-parent record exceeds size limit")
            record = ChannelInputParentRecord.model_validate_json(payload)
        except (OSError, ValueError) as exc:
            raise ChannelInputParentError("channel-parent record is invalid") from exc
        if path.name != f"{record.record_id}.json":
            raise ChannelInputParentError("channel-parent record identity differs from filename")
        return record

    def load_all(self) -> tuple[ChannelInputParentRecord, ...]:
        self._ensure_directory()
        try:
            paths = tuple(sorted(self.directory.iterdir(), key=lambda path: path.name))
        except OSError as exc:
            raise ChannelInputParentError("could not enumerate channel-parent records") from exc
        record_paths: list[Path] = []
        for path in paths:
            if _PARENT_TEMP_NAME.fullmatch(path.name):
                try:
                    info = path.lstat()
                except OSError as exc:
                    raise ChannelInputParentError(
                        "could not inspect channel-parent temporary record"
                    ) from exc
                if (
                    not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size > MAX_PARENT_RECORD_BYTES
                ):
                    raise ChannelInputParentError("channel-parent temporary record is unsafe")
                continue
            if _PARENT_RECORD_NAME.fullmatch(path.name) is None:
                raise ChannelInputParentError("channel-parent store contains an unexpected file")
            record_paths.append(path)
        if len(record_paths) > MAX_PARENT_RECORDS:
            raise ChannelInputParentError("channel-parent store exceeds maximum record count")

        records = tuple(self.load_path(path) for path in record_paths)
        sessions: set[str] = set()
        for record in records:
            if record.record_id != ChannelInputParentCoordinator.record_id(record.plan.session_id):
                raise ChannelInputParentError("channel-parent record ID differs from session ID")
            if record.plan.session_id in sessions:
                raise ChannelInputParentError("channel-parent store contains duplicate sessions")
            sessions.add(record.plan.session_id)
        return tuple(sorted(records, key=lambda record: (record.plan.session_id, record.record_id)))

    def load_path(self, path: Path) -> ChannelInputParentRecord:
        try:
            info = path.lstat()
        except OSError as exc:
            raise ChannelInputParentError("could not inspect channel-parent record") from exc
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise ChannelInputParentError("channel-parent record is unsafe")
        if info.st_size > MAX_PARENT_RECORD_BYTES:
            raise ChannelInputParentError("channel-parent record exceeds size limit")
        try:
            payload = path.read_bytes()
            if len(payload) > MAX_PARENT_RECORD_BYTES:
                raise ChannelInputParentError("channel-parent record exceeds size limit")
            record = ChannelInputParentRecord.model_validate_json(payload)
        except (OSError, ValueError) as exc:
            raise ChannelInputParentError("channel-parent record is invalid") from exc
        if path.name != f"{record.record_id}.json":
            raise ChannelInputParentError("channel-parent record identity differs from filename")
        return record

    def save(self, record: ChannelInputParentRecord) -> None:
        payload = json.dumps(
            record.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if len(payload) > MAX_PARENT_RECORD_BYTES:
            raise ChannelInputParentError("channel-parent record exceeds size limit")
        target = self._path(record.record_id)
        temporary = self.directory / f".{record.record_id}.{os.getpid()}.{time.time_ns()}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written == 0:
                        raise OSError("channel-parent record write made no progress")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
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


class ChannelInputParentCoordinator:
    """Coordinates immutable mixed-input parents, retaining durable signing evidence."""

    def __init__(self, directory: Path) -> None:
        self._store = _Store(directory)

    @staticmethod
    def record_id(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def load(self, session_id: str) -> ChannelInputParentRecord | None:
        """Return one exact durable parent record, or no record for this session."""

        record = self._store.load(self.record_id(session_id))
        if record is not None and record.plan.session_id != session_id:
            raise ChannelInputParentError("channel-parent record session differs from lookup")
        if record is not None:
            self._validate_record(record)
        return record

    def load_all(self) -> tuple[ChannelInputParentRecord, ...]:
        """Return all durable parent records in a stable fail-closed order."""

        records = self._store.load_all()
        for record in records:
            self._validate_record(record)
        return records

    @staticmethod
    def _endpoint(
        endpoints: Mapping[str, ChannelInputEndpoint], contribution: EndpointContribution
    ) -> ChannelInputEndpoint:
        try:
            return endpoints[contribution.endpoint_id]
        except KeyError as exc:
            raise ChannelInputParentError(f"missing endpoint {contribution.endpoint_id!r}") from exc

    @staticmethod
    def _outpoint(snapshot: FrozenSnapshot) -> EscrowOutpoint:
        return EscrowOutpoint(txid=snapshot.funding_txid, vout=snapshot.funding_vout)

    @staticmethod
    def _replace(record: ChannelInputParentRecord, **updates: object) -> ChannelInputParentRecord:
        return record.model_copy(update={**updates, "updated_at_ns": time.time_ns()})

    def _save(
        self, record: ChannelInputParentRecord, **updates: object
    ) -> ChannelInputParentRecord:
        changed = self._replace(record, **updates)
        self._store.save(changed)
        return changed

    @staticmethod
    def _with_partial(evidence: EndpointEvidence, partial: bytes) -> EndpointEvidence:
        return EndpointEvidence(
            attempt_id=evidence.attempt_id,
            public_nonce=evidence.public_nonce,
            partial_signature=partial.hex(),
        )

    @staticmethod
    def _validate_plan(plan: ChannelInputParentPlan) -> None:
        try:
            authorization = ChannelInputAuthorization.model_validate(
                plan.authorization.model_dump(mode="python")
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ChannelInputParentError("channel input authorization is invalid") from exc
        transcript = authorization.prepare.approval_set.transcript
        if plan.session_id != transcript.session_id:
            raise ChannelInputParentError("parent session ID differs from authorization transcript")
        if plan.unsigned_parent_hex != transcript.unsigned_parent_tx:
            raise ChannelInputParentError("unsigned parent differs from authorization transcript")
        try:
            tx = parse_transaction(plan.unsigned_parent_hex)
        except ValueError as exc:
            raise ChannelInputParentError("unsigned parent cannot be parsed") from exc
        if tx.has_witness or any(item.scriptsig for item in tx.inputs):
            raise ChannelInputParentError(
                "parent must be an unsigned transaction without scriptsigs"
            )
        if len(tx.inputs) != len(plan.prevouts):
            raise ChannelInputParentError("parent input and prevout counts differ")
        try:
            plan_input_prevouts = tuple(
                InputPrevout(
                    outpoint=Outpoint(txid=tx_input.txid, vout=tx_input.vout),
                    value_sat=prevout.value_sat,
                    script_pubkey=prevout.script_pubkey,
                )
                for tx_input, prevout in zip(tx.inputs, plan.prevouts, strict=True)
            )
            validate_transcript_against_transaction(
                transcript, bytes.fromhex(plan.unsigned_parent_hex), plan_input_prevouts
            )
        except (ChannelInputValidationError, ValueError) as exc:
            raise ChannelInputParentError(
                "parent prevouts differ from authorization transcript"
            ) from exc
        outpoints = [(item.txid, item.vout) for item in tx.inputs]
        if len(outpoints) != len(set(outpoints)):
            raise ChannelInputParentError("parent has duplicate input outpoints")
        channel_indices = [channel.channel_input_index for channel in plan.channels]
        if len(channel_indices) != len(set(channel_indices)) or any(
            index >= len(tx.inputs) for index in channel_indices
        ):
            raise ChannelInputParentError(
                "channel input indices are duplicate or outside the parent"
            )
        if len(plan.channels) != len(transcript.channels):
            raise ChannelInputParentError(
                "parent channel count differs from authorization transcript"
            )
        descriptors_by_index = {
            descriptor.input_index: descriptor for descriptor in transcript.channels
        }
        if set(channel_indices) != set(descriptors_by_index):
            raise ChannelInputParentError(
                "parent channel inputs differ from authorization transcript"
            )
        assigned = [
            output.output_index
            for channel in plan.channels
            for endpoint in (channel.first, channel.second)
            for output in endpoint.settlement_outputs
        ]
        if len(assigned) != len(set(assigned)) or any(
            index >= len(tx.outputs) for index in assigned
        ):
            raise ChannelInputParentError(
                "settlement output indices are duplicate or outside the parent"
            )
        for channel in plan.channels:
            descriptor = descriptors_by_index[channel.channel_input_index]
            first_terms, second_terms = authorization.terms_for(
                descriptor.first_terms_hash, descriptor.second_terms_hash
            )
            for contribution, participant_key, terms in (
                (channel.first, descriptor.first_participant_key, first_terms),
                (channel.second, descriptor.second_participant_key, second_terms),
            ):
                if contribution.participant_key != participant_key:
                    raise ChannelInputParentError(
                        "channel contribution participant differs from authorization transcript"
                    )
                committed_outputs = tuple(
                    (output.output_index, output.value_sat, output.script_pubkey)
                    for output in contribution.settlement_outputs
                )
                authorized_outputs = tuple(
                    (output.output_index, output.amount_sat, output.script_pubkey)
                    for output in terms.settlement_outputs
                )
                if committed_outputs != authorized_outputs:
                    raise ChannelInputParentError(
                        "channel settlement outputs differ from authorization terms"
                    )
                if contribution.miner_fee_share_sat != terms.miner_fee_share_sat:
                    raise ChannelInputParentError(
                        "channel miner fee share differs from authorization terms"
                    )
                if contribution.claim_adjustment_sat != terms.claim_adjustment_sat:
                    raise ChannelInputParentError(
                        "channel claim adjustment differs from authorization terms"
                    )
            if channel.first.claim_adjustment_sat + channel.second.claim_adjustment_sat != 0:
                raise ChannelInputParentError("channel claim adjustments must sum to zero")
            for endpoint in (channel.first, channel.second):
                for output in endpoint.settlement_outputs:
                    parent_output = tx.outputs[output.output_index]
                    if (
                        parent_output.value != output.value_sat
                        or parent_output.script.hex() != output.script_pubkey
                    ):
                        raise ChannelInputParentError(
                            "settlement output commitment differs from parent"
                        )
        ordinary_indices = set(range(len(tx.inputs))) - set(channel_indices)
        witnesses = {witness.input_index for witness in plan.ordinary_witnesses}
        if witnesses != ordinary_indices or len(witnesses) != len(plan.ordinary_witnesses):
            raise ChannelInputParentError("ordinary witness coverage is incomplete or duplicated")
        fee = sum(item.value_sat for item in plan.prevouts) - sum(item.value for item in tx.outputs)
        if fee <= 0 or fee > plan.maximum_miner_fee_sat:
            raise ChannelInputParentError("parent miner fee is nonpositive or excessive")
        if fee != transcript.total_fee_sat:
            raise ChannelInputParentError("parent miner fee differs from authorization transcript")
        if transcript.total_fee_sat > plan.maximum_miner_fee_sat:
            raise ChannelInputParentError("authorization transcript fee exceeds local maximum")
        channel_fee_shares = sum(
            endpoint.miner_fee_share_sat
            for channel in plan.channels
            for endpoint in (channel.first, channel.second)
        )
        if fee < channel_fee_shares:
            raise ChannelInputParentError("parent miner fee is below channel fee shares")

    @classmethod
    def _validate_record(cls, record: ChannelInputParentRecord) -> None:
        cls._validate_plan(record.plan)
        if record.state is ChannelInputParentState.CREATED:
            if record.channels:
                raise ChannelInputParentError("created parent unexpectedly has frozen channels")
            return
        records_by_index = {channel.channel_input_index: channel for channel in record.channels}
        plan_by_index = {channel.channel_input_index: channel for channel in record.plan.channels}
        if len(records_by_index) != len(record.channels) or set(records_by_index) != set(
            plan_by_index
        ):
            raise ChannelInputParentError("persisted frozen channels differ from parent plan")
        parent = parse_transaction(record.plan.unsigned_parent_hex)
        transcript = record.plan.authorization.prepare.approval_set.transcript
        descriptors = {descriptor.input_index: descriptor for descriptor in transcript.channels}
        for index, contribution in plan_by_index.items():
            channel = records_by_index[index]
            if channel.channel_id != contribution.channel_id:
                raise ChannelInputParentError(
                    "persisted frozen channel ID differs from parent plan"
                )
            descriptor = descriptors[index]
            first_terms, second_terms = record.plan.authorization.terms_for(
                descriptor.first_terms_hash, descriptor.second_terms_hash
            )
            cls._validate_snapshot_terms(channel.first_snapshot, first_terms)
            cls._validate_snapshot_terms(channel.second_snapshot, second_terms)
            cls._validate_snapshot_pair(channel.first_snapshot, channel.second_snapshot)
            cls._validate_conservation(contribution.first, channel.first_snapshot)
            cls._validate_conservation(contribution.second, channel.second_snapshot)
            tx_input = parent.inputs[index]
            prevout = record.plan.prevouts[index]
            if (tx_input.txid, tx_input.vout) != (
                channel.first_snapshot.funding_txid,
                channel.first_snapshot.funding_vout,
            ):
                raise ChannelInputParentError(
                    "channel parent input differs from persisted frozen outpoint"
                )
            if (
                prevout.value_sat != channel.first_snapshot.capacity_sat
                or prevout.script_pubkey != channel.first_snapshot.funding_script_pubkey
            ):
                raise ChannelInputParentError(
                    "channel prevout differs from persisted frozen channel"
                )

    @staticmethod
    def _validate_snapshot_pair(first: FrozenSnapshot, second: FrozenSnapshot) -> None:
        for snapshot in (first, second):
            _compressed_pubkey(snapshot.peer_node_pubkey, "frozen peer node public key")
        fields = ("funding_txid", "funding_vout", "funding_script_pubkey", "capacity_sat")
        if any(getattr(first, field) != getattr(second, field) for field in fields):
            raise ChannelInputParentError("mirrored frozen channel snapshots disagree")
        if (
            first.local_funding_pubkey != second.remote_funding_pubkey
            or first.remote_funding_pubkey != second.local_funding_pubkey
            or first.local_claim_sat != second.remote_claim_sat
            or first.remote_claim_sat != second.local_claim_sat
        ):
            raise ChannelInputParentError("mirrored frozen channel endpoint data disagree")

    @staticmethod
    def _validate_conservation(
        contribution: EndpointContribution, snapshot: FrozenSnapshot
    ) -> None:
        committed = sum(output.value_sat for output in contribution.settlement_outputs)
        adjusted_claim = snapshot.local_claim_sat + contribution.claim_adjustment_sat
        if adjusted_claim <= 0:
            raise ChannelInputParentError("endpoint adjusted frozen claim is nonpositive")
        if committed + contribution.miner_fee_share_sat != adjusted_claim:
            raise ChannelInputParentError(
                "endpoint settlement outputs do not conserve adjusted frozen claim"
            )

    @staticmethod
    def _validate_snapshot_terms(snapshot: FrozenSnapshot, terms: EndpointTerms) -> None:
        if (
            snapshot.funding_txid != terms.funding_outpoint.txid
            or snapshot.funding_vout != terms.funding_outpoint.vout
            or snapshot.capacity_sat != terms.capacity_sat
            or snapshot.local_claim_sat != terms.frozen_local_claim_sat
            or snapshot.remote_claim_sat != terms.frozen_remote_claim_sat
            or snapshot.peer_node_pubkey != terms.peer_node_key
        ):
            raise ChannelInputParentError("frozen channel data differs from authorization terms")

    @staticmethod
    def _validate_ordinary_witnesses(plan: ChannelInputParentPlan) -> None:
        tx = parse_transaction(plan.unsigned_parent_hex)
        values = [item.value_sat for item in plan.prevouts]
        scripts = [bytes.fromhex(item.script_pubkey) for item in plan.prevouts]
        for ordinary in plan.ordinary_witnesses:
            witness = tuple(bytes.fromhex(item) for item in ordinary.witness)
            script = scripts[ordinary.input_index]
            if len(script) == 34 and script[:2] == b"\x51\x20":
                if len(witness) != 1 or not verify_p2tr_signature(
                    tx, ordinary.input_index, values, scripts, witness[0], script[2:]
                ):
                    raise ChannelInputParentError("invalid ordinary P2TR key-path witness")
            elif len(script) == 22 and script[:2] == b"\x00\x14":
                if (
                    len(witness) != 2
                    or pubkey_to_p2wpkh_script(witness[1]) != script
                    or not verify_p2wpkh_signature(
                        tx,
                        ordinary.input_index,
                        create_p2wpkh_script_code(witness[1]),
                        values[ordinary.input_index],
                        witness[0],
                        witness[1],
                    )
                ):
                    raise ChannelInputParentError("invalid ordinary P2WPKH witness")
            else:
                raise ChannelInputParentError("unsupported ordinary input script")

    async def execute(
        self, plan: ChannelInputParentPlan, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        self._validate_plan(plan)
        record_id = self.record_id(plan.session_id)
        existing = self._store.load(record_id)
        if existing is not None:
            if existing.plan != plan:
                raise ChannelInputParentError("session ID is already bound to another parent plan")
            return await self._progress(existing, endpoints)
        record = ChannelInputParentRecord(
            record_id=record_id,
            plan=plan,
            state=ChannelInputParentState.CREATED,
            updated_at_ns=time.time_ns(),
        )
        self._store.save(record)
        return await self._progress(record, endpoints)

    async def _progress(
        self, record: ChannelInputParentRecord, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        try:
            while True:
                if record.state is ChannelInputParentState.CREATED:
                    record = await self._freeze_channels(record, endpoints)
                elif record.state is ChannelInputParentState.CHANNELS_FROZEN:
                    record = await self._prepare_parent(record, endpoints)
                elif record.state is ChannelInputParentState.PARENT_PREPARED:
                    self._validate_ordinary_witnesses(record.plan)
                    record = self._save(record, state=ChannelInputParentState.ORDINARY_SIGNED)
                elif record.state is ChannelInputParentState.ORDINARY_SIGNED:
                    record = await self._start_signing(record, endpoints)
                elif record.state is ChannelInputParentState.CHANNEL_SIGNING:
                    record = await self._reconcile_signing(record, endpoints)
                    record = await self._sign_and_finalize(record, endpoints)
                elif (
                    record.state is ChannelInputParentState.RECOVERY_REQUIRED
                    and record.final_tx_hex is None
                ):
                    record = await self._reconcile_signing(record, endpoints)
                    record = await self._sign_and_finalize(record, endpoints)
                elif record.state is ChannelInputParentState.CHANNEL_SIGNED:
                    final_hex = self._assemble_final(record)
                    return self._save(
                        record,
                        state=ChannelInputParentState.FINAL_TX_PERSISTED,
                        final_tx_hex=final_hex,
                        final_txid=get_txid(final_hex),
                    )
                else:
                    return record
        except Exception as exc:
            self._mark_recovery_required(record.record_id)
            if isinstance(exc, ChannelInputParentError):
                raise
            raise ChannelInputParentError("channel parent progression failed") from exc

    def _mark_recovery_required(self, record_id: str) -> None:
        latest = self._store.load(record_id)
        if latest is None or latest.state in {
            ChannelInputParentState.CREATED,
            ChannelInputParentState.CHANNELS_FROZEN,
            ChannelInputParentState.PARENT_PREPARED,
            ChannelInputParentState.ORDINARY_SIGNED,
            ChannelInputParentState.CANCELED,
        }:
            return
        if latest.state is not ChannelInputParentState.RECOVERY_REQUIRED:
            self._save(latest, state=ChannelInputParentState.RECOVERY_REQUIRED)

    async def _freeze_channels(
        self, record: ChannelInputParentRecord, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        channel_records: list[ChannelRecord] = []
        parent = parse_transaction(record.plan.unsigned_parent_hex)
        transcript = record.plan.authorization.prepare.approval_set.transcript
        descriptors_by_index = {
            descriptor.input_index: descriptor for descriptor in transcript.channels
        }
        for channel in record.plan.channels:
            first_endpoint = self._endpoint(endpoints, channel.first)
            second_endpoint = self._endpoint(endpoints, channel.second)
            first = FrozenSnapshot.from_lnd(
                await first_endpoint.freeze_channel_input(
                    record.plan.session_id, channel.channel_id
                )
            )
            second = FrozenSnapshot.from_lnd(
                await second_endpoint.freeze_channel_input(
                    record.plan.session_id, channel.channel_id
                )
            )
            descriptor = descriptors_by_index[channel.channel_input_index]
            first_terms, second_terms = record.plan.authorization.terms_for(
                descriptor.first_terms_hash, descriptor.second_terms_hash
            )
            self._validate_snapshot_terms(first, first_terms)
            self._validate_snapshot_terms(second, second_terms)
            self._validate_snapshot_pair(first, second)
            self._validate_conservation(channel.first, first)
            self._validate_conservation(channel.second, second)
            tx_input = parent.inputs[channel.channel_input_index]
            prevout = record.plan.prevouts[channel.channel_input_index]
            if (tx_input.txid, tx_input.vout) != (first.funding_txid, first.funding_vout):
                raise ChannelInputParentError(
                    "channel parent input differs from frozen funding outpoint"
                )
            if (
                prevout.value_sat != first.capacity_sat
                or prevout.script_pubkey != first.funding_script_pubkey
            ):
                raise ChannelInputParentError("channel prevout differs from frozen funding data")
            channel_records.append(
                ChannelRecord(
                    channel_id=channel.channel_id,
                    channel_input_index=channel.channel_input_index,
                    first_snapshot=first,
                    second_snapshot=second,
                )
            )
        return self._save(
            record,
            state=ChannelInputParentState.CHANNELS_FROZEN,
            channels=tuple(channel_records),
        )

    async def _prepare_parent(
        self, record: ChannelInputParentRecord, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        prevouts = tuple(item.witness_utxo() for item in record.plan.prevouts)
        expected_txid = get_txid(record.plan.unsigned_parent_hex)
        for channel, frozen in zip(record.plan.channels, record.channels, strict=True):
            for contribution, snapshot in (
                (channel.first, frozen.first_snapshot),
                (channel.second, frozen.second_snapshot),
            ):
                actual = await self._endpoint(endpoints, contribution).prepare_channel_input_parent(
                    record.plan.session_id,
                    self._outpoint(snapshot),
                    record.plan.unsigned_parent_hex,
                    prevouts,
                    channel.channel_input_index,
                )
                if actual != expected_txid:
                    raise ChannelInputParentError(
                        "endpoint prepared an unexpected parent transaction"
                    )
        return self._save(record, state=ChannelInputParentState.PARENT_PREPARED)

    async def _start_signing(
        self, record: ChannelInputParentRecord, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        attempts: list[ChannelRecord] = []
        for contribution, channel in zip(record.plan.channels, record.channels, strict=True):
            first = await self._endpoint(endpoints, contribution.first).begin_channel_input_signing(
                record.plan.session_id,
                self._outpoint(channel.first_snapshot),
                restart_attempt=False,
            )
            second = await self._endpoint(
                endpoints, contribution.second
            ).begin_channel_input_signing(
                record.plan.session_id,
                self._outpoint(channel.second_snapshot),
                restart_attempt=False,
            )
            attempts.append(
                channel.model_copy(
                    update={
                        "first_evidence": EndpointEvidence(
                            attempt_id=first.attempt_id.hex(), public_nonce=first.public_nonce.hex()
                        ),
                        "second_evidence": EndpointEvidence(
                            attempt_id=second.attempt_id.hex(),
                            public_nonce=second.public_nonce.hex(),
                        ),
                    }
                )
            )
        record = self._save(
            record, state=ChannelInputParentState.CHANNEL_SIGNING, channels=tuple(attempts)
        )
        return record

    async def _sign_and_finalize(
        self, record: ChannelInputParentRecord, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        assert record.state is ChannelInputParentState.CHANNEL_SIGNING
        for index, (contribution, channel) in enumerate(
            zip(record.plan.channels, record.channels, strict=True)
        ):
            first_evidence, second_evidence = channel.first_evidence, channel.second_evidence
            if first_evidence is None or second_evidence is None:
                raise ChannelInputParentError("channel signing evidence is missing")
            changed = channel
            if first_evidence.partial_signature is None:
                partial = await self._endpoint(
                    endpoints, contribution.first
                ).sign_channel_input_parent(
                    record.plan.session_id,
                    self._outpoint(channel.first_snapshot),
                    LndEscrowSigningAttempt(
                        attempt_id=bytes.fromhex(first_evidence.attempt_id),
                        public_nonce=bytes.fromhex(first_evidence.public_nonce),
                    ),
                    bytes.fromhex(second_evidence.public_nonce),
                )
                changed = changed.model_copy(
                    update={"first_evidence": self._with_partial(first_evidence, partial)}
                )
                record = self._save(record, channels=_replace_item(record.channels, index, changed))
                channel, first_evidence = changed, changed.first_evidence
                assert first_evidence is not None
            if second_evidence.partial_signature is None:
                partial = await self._endpoint(
                    endpoints, contribution.second
                ).sign_channel_input_parent(
                    record.plan.session_id,
                    self._outpoint(channel.second_snapshot),
                    LndEscrowSigningAttempt(
                        attempt_id=bytes.fromhex(second_evidence.attempt_id),
                        public_nonce=bytes.fromhex(second_evidence.public_nonce),
                    ),
                    bytes.fromhex(first_evidence.public_nonce),
                )
                changed = channel.model_copy(
                    update={"second_evidence": self._with_partial(second_evidence, partial)}
                )
                record = self._save(record, channels=_replace_item(record.channels, index, changed))
        return await self._finalize(record, endpoints)

    async def _finalize(
        self, record: ChannelInputParentRecord, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        expected_txid = get_txid(record.plan.unsigned_parent_hex)
        channel_records = list(record.channels)
        for index, (contribution, channel) in enumerate(
            zip(record.plan.channels, channel_records, strict=True)
        ):
            first, second = channel.first_evidence, channel.second_evidence
            if (
                first is None
                or second is None
                or first.partial_signature is None
                or second.partial_signature is None
            ):
                raise ChannelInputParentError("channel partial signatures are incomplete")
            first_attempt = LndEscrowSigningAttempt(
                attempt_id=bytes.fromhex(first.attempt_id),
                public_nonce=bytes.fromhex(first.public_nonce),
            )
            second_attempt = LndEscrowSigningAttempt(
                attempt_id=bytes.fromhex(second.attempt_id),
                public_nonce=bytes.fromhex(second.public_nonce),
            )
            first_endpoint = self._endpoint(endpoints, contribution.first)
            second_endpoint = self._endpoint(endpoints, contribution.second)
            if channel.first_recover_only:
                first_final = await first_endpoint.recover_channel_input_parent(
                    record.plan.session_id,
                    self._outpoint(channel.first_snapshot),
                    bytes.fromhex(second.partial_signature),
                )
            else:
                first_final = await first_endpoint.finalize_channel_input_parent(
                    record.plan.session_id,
                    self._outpoint(channel.first_snapshot),
                    first_attempt,
                    bytes.fromhex(second.partial_signature),
                )
            if channel.second_recover_only:
                second_final = await second_endpoint.recover_channel_input_parent(
                    record.plan.session_id,
                    self._outpoint(channel.second_snapshot),
                    bytes.fromhex(first.partial_signature),
                )
            else:
                second_final = await second_endpoint.finalize_channel_input_parent(
                    record.plan.session_id,
                    self._outpoint(channel.second_snapshot),
                    second_attempt,
                    bytes.fromhex(first.partial_signature),
                )
            self._validate_finalization(record.plan, channel, first_final)
            self._validate_finalization(record.plan, channel, second_final)
            if (
                first_final.txid != expected_txid
                or second_final.txid != expected_txid
                or first_final.channel_input_index != channel.channel_input_index
                or second_final.channel_input_index != channel.channel_input_index
                or first_final.channel_input_witness != second_final.channel_input_witness
            ):
                raise ChannelInputParentError("endpoint finalization results disagree")
            channel_records[index] = channel.model_copy(
                update={"witness": first_final.channel_input_witness[0].hex()}
            )
        return self._save(
            record, state=ChannelInputParentState.CHANNEL_SIGNED, channels=tuple(channel_records)
        )

    @staticmethod
    def _validate_finalization(
        plan: ChannelInputParentPlan, channel: ChannelRecord, result: LndFinalizedEscrowParent
    ) -> None:
        parsed = parse_transaction(result.tx_hex)
        stripped = serialize_transaction(
            parsed.version, parsed.inputs, parsed.outputs, parsed.locktime
        ).hex()
        if stripped != plan.unsigned_parent_hex:
            raise ChannelInputParentError("endpoint finalized a mutated parent")
        witness = result.channel_input_witness
        if len(witness) != 1 or not verify_p2tr_signature(
            parse_transaction(plan.unsigned_parent_hex),
            channel.channel_input_index,
            [prevout.value_sat for prevout in plan.prevouts],
            [bytes.fromhex(prevout.script_pubkey) for prevout in plan.prevouts],
            witness[0],
            bytes.fromhex(channel.first_snapshot.funding_script_pubkey)[2:],
        ):
            raise ChannelInputParentError("invalid final channel P2TR witness")

    @staticmethod
    def _assemble_final(record: ChannelInputParentRecord) -> str:
        tx = parse_transaction(record.plan.unsigned_parent_hex)
        channel_by_index = {channel.channel_input_index: channel for channel in record.channels}
        ordinary_by_index = {
            ordinary.input_index: ordinary for ordinary in record.plan.ordinary_witnesses
        }
        witnesses: list[list[bytes]] = []
        for index in range(len(tx.inputs)):
            if index in channel_by_index:
                witness = channel_by_index[index].witness
                if witness is None:
                    raise ChannelInputParentError("channel witness is missing")
                witnesses.append([bytes.fromhex(witness)])
            else:
                witnesses.append([bytes.fromhex(item) for item in ordinary_by_index[index].witness])
        final_hex = serialize_transaction(
            tx.version, tx.inputs, tx.outputs, tx.locktime, witnesses
        ).hex()
        # Verifies all ordinary and channel witnesses against the final transaction shape.
        ChannelInputParentCoordinator._validate_ordinary_witnesses(record.plan)
        for channel in record.channels:
            result = LndFinalizedEscrowParent(
                tx_hex=final_hex,
                txid=get_txid(final_hex),
                channel_input_index=channel.channel_input_index,
                channel_input_witness=(bytes.fromhex(channel.witness or ""),),
            )
            ChannelInputParentCoordinator._validate_finalization(record.plan, channel, result)
        return final_hex

    async def resume(
        self, session_id: str, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        record = self._store.load(self.record_id(session_id))
        if record is None:
            raise ChannelInputParentError("channel-parent record does not exist")
        return await self._progress(record, endpoints)

    @staticmethod
    def _validate_current_height(current_height: int) -> None:
        if (
            isinstance(current_height, bool)
            or not isinstance(current_height, int)
            or not 0 <= current_height <= 0xFFFFFFFF
        ):
            raise ChannelInputParentError("current height must be a uint32 integer")

    @staticmethod
    def _has_persisted_partial(record: ChannelInputParentRecord) -> bool:
        return any(
            evidence is not None and evidence.partial_signature is not None
            for channel in record.channels
            for evidence in (channel.first_evidence, channel.second_evidence)
        )

    @staticmethod
    def _is_inherently_post_sign(record: ChannelInputParentRecord) -> bool:
        return (
            record.state is ChannelInputParentState.CHANNEL_SIGNED
            or record.final_tx_hex is not None
        )

    @staticmethod
    def _validate_observed_parent(
        record: ChannelInputParentRecord, observed: ChainTransaction
    ) -> tuple[str, tuple[ChannelRecord, ...]]:
        expected_txid = get_txid(record.plan.unsigned_parent_hex)
        try:
            raw = bytes.fromhex(observed.raw).hex()
            if observed.txid != expected_txid or get_txid(raw) != expected_txid:
                raise ChannelInputParentError("chain returned contradictory transaction data")
            transaction = parse_transaction(raw)
            unsigned = serialize_transaction(
                transaction.version, transaction.inputs, transaction.outputs, transaction.locktime
            ).hex()
        except (TypeError, ValueError) as exc:
            raise ChannelInputParentError("chain returned malformed transaction data") from exc
        if unsigned != record.plan.unsigned_parent_hex:
            raise ChannelInputParentError("chain returned a mutated parent transaction")
        if len(transaction.witnesses) != len(transaction.inputs):
            raise ChannelInputParentError("chain returned incomplete transaction witnesses")

        ordinary_by_index = {
            ordinary.input_index: ordinary for ordinary in record.plan.ordinary_witnesses
        }
        channel_by_index = {
            channel.channel_input_index: channel for channel in record.plan.channels
        }
        values = [prevout.value_sat for prevout in record.plan.prevouts]
        scripts = [bytes.fromhex(prevout.script_pubkey) for prevout in record.plan.prevouts]
        observed_witnesses: dict[int, str] = {}
        for index, witness in enumerate(transaction.witnesses):
            if index in ordinary_by_index:
                if tuple(item.hex() for item in witness) != ordinary_by_index[index].witness:
                    raise ChannelInputParentError("chain returned contradictory ordinary witness")
            elif index in channel_by_index:
                script = scripts[index]
                if (
                    len(script) != 34
                    or script[:2] != b"\x51\x20"
                    or len(witness) != 1
                    or not verify_p2tr_signature(
                        parse_transaction(record.plan.unsigned_parent_hex),
                        index,
                        values,
                        scripts,
                        witness[0],
                        script[2:],
                    )
                ):
                    raise ChannelInputParentError("invalid observed channel P2TR witness")
                observed_witnesses[index] = witness[0].hex()
            else:
                raise ChannelInputParentError("chain returned an unexpected parent input")

        if not record.channels:
            return raw, record.channels
        records_by_index = {channel.channel_input_index: channel for channel in record.channels}
        if set(records_by_index) != set(channel_by_index):
            raise ChannelInputParentError("persisted channel records differ from parent plan")
        return (
            raw,
            tuple(
                channel.model_copy(
                    update={"witness": observed_witnesses[channel.channel_input_index]}
                )
                for channel in record.channels
            ),
        )

    async def _observe_authorized_parent(
        self, record: ChannelInputParentRecord, chain: ChannelInputParentChain
    ) -> ChannelInputParentRecord:
        try:
            expected_txid = get_txid(record.plan.unsigned_parent_hex)
            observed = await chain.get_transaction(expected_txid)
            if observed is None:
                return record
            raw, channels = self._validate_observed_parent(record, observed)
            state = (
                ChannelInputParentState.CONFIRMED
                if observed.confirmations > 0
                else ChannelInputParentState.OBSERVED
            )
            return self._save(
                record,
                state=state,
                channels=channels,
                final_tx_hex=raw,
                final_txid=expected_txid,
                observation=ChainObservation(confirmations=observed.confirmations),
            )
        except Exception as exc:
            self._mark_recovery_required(record.record_id)
            if isinstance(exc, ChannelInputParentError):
                raise
            raise ChannelInputParentError("chain observation failed") from exc

    async def decide_restart(
        self,
        session_id: str,
        endpoints: Mapping[str, ChannelInputEndpoint],
        chain: ChannelInputParentChain,
        *,
        current_height: int,
    ) -> ChannelInputRestartAction:
        self._validate_current_height(current_height)
        record = self._store.load(self.record_id(session_id))
        if record is None:
            raise ChannelInputParentError("channel-parent record does not exist")
        if record.state in {
            ChannelInputParentState.CANCELED,
            ChannelInputParentState.OBSERVED,
            ChannelInputParentState.CONFIRMED,
        }:
            return ChannelInputRestartAction.NONE

        record = await self.observe_restart(session_id, chain)
        if record.state in {
            ChannelInputParentState.OBSERVED,
            ChannelInputParentState.CONFIRMED,
            ChannelInputParentState.FORCE_CLOSED,
        }:
            return ChannelInputRestartAction.NONE

        if record.state in {
            ChannelInputParentState.CREATED,
            ChannelInputParentState.CHANNELS_FROZEN,
            ChannelInputParentState.PARENT_PREPARED,
            ChannelInputParentState.ORDINARY_SIGNED,
        }:
            return ChannelInputRestartAction.CANCEL_PRE_SIGN

        if (
            record.state
            in {
                ChannelInputParentState.CHANNEL_SIGNING,
                ChannelInputParentState.RECOVERY_REQUIRED,
            }
            and record.final_tx_hex is None
        ):
            record = await self._reconcile_signing(record, endpoints)

        if not self._has_persisted_partial(record) and not self._is_inherently_post_sign(record):
            return ChannelInputRestartAction.CANCEL_PRE_SIGN
        recovery_height = record.plan.authorization.prepare.approval_set.transcript.recovery_height
        if current_height >= recovery_height:
            return ChannelInputRestartAction.FORCE_CLOSE
        if record.final_tx_hex is not None:
            return ChannelInputRestartAction.PUBLISH_EXACT_PARENT
        return ChannelInputRestartAction.RESUME_EXACT_PARENT

    async def observe_restart(
        self, session_id: str, chain: ChannelInputParentChain
    ) -> ChannelInputParentRecord:
        """Query for the exact authorized parent without requiring peer endpoints."""

        record = self._store.load(self.record_id(session_id))
        if record is None:
            raise ChannelInputParentError("channel-parent record does not exist")
        if record.state in {
            ChannelInputParentState.CANCELED,
            ChannelInputParentState.OBSERVED,
            ChannelInputParentState.CONFIRMED,
        }:
            return record
        if record.final_tx_hex is not None:
            return await self.observe(session_id, chain)
        return await self._observe_authorized_parent(record, chain)

    def record_force_close(
        self, session_id: str, outcome: ChannelInputForceCloseOutcome
    ) -> ChannelInputParentRecord:
        """Persist one force-close result and finish only after exact channel coverage."""

        record = self._store.load(self.record_id(session_id))
        if record is None:
            raise ChannelInputParentError("channel-parent record does not exist")
        if record.state is ChannelInputParentState.FORCE_CLOSED:
            existing = {
                (item.funding_txid, item.funding_vout): item for item in record.force_closes
            }.get((outcome.funding_outpoint.txid, outcome.funding_outpoint.vout))
            if existing != ChannelInputForceCloseRecord.from_outcome(outcome):
                raise ChannelInputParentError("force-close result conflicts with durable evidence")
            return record
        if record.state not in {
            ChannelInputParentState.CHANNEL_SIGNING,
            ChannelInputParentState.CHANNEL_SIGNED,
            ChannelInputParentState.FINAL_TX_PERSISTED,
            ChannelInputParentState.BROADCAST_ATTEMPTED,
            ChannelInputParentState.RECOVERY_REQUIRED,
        }:
            raise ChannelInputParentError("force close is not valid in the current parent state")
        expected = {
            (channel.second_snapshot.funding_txid, channel.second_snapshot.funding_vout)
            for channel in record.channels
        }
        point = (outcome.funding_outpoint.txid, outcome.funding_outpoint.vout)
        if point not in expected:
            raise ChannelInputParentError("force-close outpoint is not part of the parent")
        durable = {(item.funding_txid, item.funding_vout): item for item in record.force_closes}
        candidate = ChannelInputForceCloseRecord.from_outcome(outcome)
        existing = durable.get(point)
        if existing is not None and existing != candidate:
            raise ChannelInputParentError("force-close result conflicts with durable evidence")
        durable[point] = candidate
        force_closes = tuple(
            sorted(durable.values(), key=lambda item: (item.funding_txid, item.funding_vout))
        )
        state = (
            ChannelInputParentState.FORCE_CLOSED
            if set(durable) == expected
            else ChannelInputParentState.RECOVERY_REQUIRED
        )
        return self._save(record, state=state, force_closes=force_closes)

    async def _reconcile_signing(
        self, record: ChannelInputParentRecord, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        records = list(record.channels)
        expected = get_txid(record.plan.unsigned_parent_hex)
        for index, (contribution, channel) in enumerate(
            zip(record.plan.channels, records, strict=True)
        ):
            statuses = (
                await self._endpoint(endpoints, contribution.first).channel_input_escrow_status(
                    record.plan.session_id, self._outpoint(channel.first_snapshot)
                ),
                await self._endpoint(endpoints, contribution.second).channel_input_escrow_status(
                    record.plan.session_id, self._outpoint(channel.second_snapshot)
                ),
            )
            first, second = channel.first_evidence, channel.second_evidence
            if first is None or second is None:
                raise ChannelInputParentError("persisted endpoint attempt is missing")
            evidence = [first, second]
            recover_only = [channel.first_recover_only, channel.second_recover_only]
            for position, status in enumerate(statuses):
                current = evidence[position]
                paired = evidence[1 - position]
                self._validate_status(status, current, paired, expected)
                if status.durable_local_partial_signature is not None:
                    partial = status.durable_local_partial_signature.hex()
                    if (
                        current.partial_signature is not None
                        and current.partial_signature != partial
                    ):
                        raise ChannelInputParentError(
                            "endpoint status changed a persisted partial signature"
                        )
                    evidence[position] = self._with_partial(
                        current, status.durable_local_partial_signature
                    )
                    recover_only[position] = status.active_attempt is None
            records[index] = channel.model_copy(
                update={
                    "first_evidence": evidence[0],
                    "second_evidence": evidence[1],
                    "first_recover_only": recover_only[0],
                    "second_recover_only": recover_only[1],
                }
            )
        return self._save(
            record, state=ChannelInputParentState.CHANNEL_SIGNING, channels=tuple(records)
        )

    @staticmethod
    def _validate_status(
        status: LndEscrowStatus,
        evidence: EndpointEvidence,
        paired_evidence: EndpointEvidence,
        expected_txid: str,
    ) -> None:
        if status.parent_txid != expected_txid or status.stage < LndEscrowStage.PARENT_PREPARED:
            raise ChannelInputParentError("endpoint status contradicts persisted parent")
        if status.active_attempt is not None and (
            status.active_attempt.attempt_id.hex() != evidence.attempt_id
            or status.active_attempt.public_nonce.hex() != evidence.public_nonce
        ):
            raise ChannelInputParentError("endpoint status replaced the durable signing attempt")
        if status.durable_local_public_nonce is not None and (
            status.durable_local_public_nonce.hex() != evidence.public_nonce
        ):
            raise ChannelInputParentError("endpoint status changed its durable public nonce")
        if status.durable_remote_public_nonce is not None and (
            status.durable_remote_public_nonce.hex() != paired_evidence.public_nonce
        ):
            raise ChannelInputParentError("endpoint status changed its durable remote nonce")
        if evidence.partial_signature is not None and (
            status.durable_local_partial_signature is None
            or status.durable_local_partial_signature.hex() != evidence.partial_signature
        ):
            raise ChannelInputParentError("endpoint status changed a persisted partial signature")
        if status.stage is LndEscrowStage.PARENT_PREPARED and (
            status.active_attempt is None and status.durable_local_partial_signature is None
        ):
            raise ChannelInputParentError("endpoint lost its persisted signing attempt")

    async def cancel(
        self, session_id: str, endpoints: Mapping[str, ChannelInputEndpoint]
    ) -> ChannelInputParentRecord:
        record = self._store.load(self.record_id(session_id))
        if record is None:
            raise ChannelInputParentError("channel-parent record does not exist")
        if record.state is ChannelInputParentState.CREATED:
            record = await self._freeze_channels(record, endpoints)
        if (
            record.state
            in {
                ChannelInputParentState.CHANNEL_SIGNING,
                ChannelInputParentState.RECOVERY_REQUIRED,
            }
            and record.final_tx_hex is None
        ):
            record = await self._reconcile_signing(record, endpoints)
            if self._has_persisted_partial(record):
                raise ChannelInputParentError(
                    "cancellation is forbidden after a channel partial is signed"
                )
        if record.state not in {
            ChannelInputParentState.CHANNELS_FROZEN,
            ChannelInputParentState.PARENT_PREPARED,
            ChannelInputParentState.ORDINARY_SIGNED,
            ChannelInputParentState.CHANNEL_SIGNING,
        }:
            raise ChannelInputParentError("cancellation is forbidden after channel signing begins")
        for contribution, channel in zip(record.plan.channels, record.channels, strict=True):
            first = await self._endpoint(endpoints, contribution.first).cancel_channel_input_escrow(
                session_id, self._outpoint(channel.first_snapshot)
            )
            second = await self._endpoint(
                endpoints, contribution.second
            ).cancel_channel_input_escrow(session_id, self._outpoint(channel.second_snapshot))
            if not first or not second:
                raise ChannelInputParentError("endpoint did not confirm channel resume")
        return self._save(record, state=ChannelInputParentState.CANCELED)

    async def observe(
        self, session_id: str, chain: ChannelInputParentChain
    ) -> ChannelInputParentRecord:
        record = self._store.load(self.record_id(session_id))
        if record is None or record.final_txid is None or record.final_tx_hex is None:
            raise ChannelInputParentError("final transaction has not been persisted")
        try:
            observed = await chain.get_transaction(record.final_txid)
            if observed is None:
                return record
            if (
                observed.txid != record.final_txid
                or get_txid(observed.raw) != record.final_txid
                or bytes.fromhex(observed.raw) != bytes.fromhex(record.final_tx_hex)
            ):
                raise ChannelInputParentError("chain returned contradictory transaction data")
            state = (
                ChannelInputParentState.CONFIRMED
                if observed.confirmations > 0
                else ChannelInputParentState.OBSERVED
            )
            return self._save(
                record,
                state=state,
                observation=ChainObservation(confirmations=observed.confirmations),
            )
        except Exception as exc:
            self._mark_recovery_required(record.record_id)
            if isinstance(exc, ChannelInputParentError):
                raise
            raise ChannelInputParentError("chain observation failed") from exc

    async def publish(
        self, session_id: str, chain: ChannelInputParentChain
    ) -> ChannelInputParentRecord:
        record = await self.observe(session_id, chain)
        if record.state in {ChannelInputParentState.OBSERVED, ChannelInputParentState.CONFIRMED}:
            return record
        if record.state not in {
            ChannelInputParentState.FINAL_TX_PERSISTED,
            ChannelInputParentState.BROADCAST_ATTEMPTED,
            ChannelInputParentState.RECOVERY_REQUIRED,
        }:
            raise ChannelInputParentError("transaction cannot be published in its current state")
        record = self._save(record, state=ChannelInputParentState.BROADCAST_ATTEMPTED)
        try:
            assert record.final_tx_hex is not None and record.final_txid is not None
            returned_txid = await chain.broadcast_transaction(record.final_tx_hex)
            if returned_txid != record.final_txid:
                raise ChannelInputParentError("broadcast returned an unexpected transaction ID")
            return record
        except Exception as exc:
            self._mark_recovery_required(record.record_id)
            if isinstance(exc, ChannelInputParentError):
                raise
            raise ChannelInputParentError("broadcast failed") from exc


def _replace_item(
    items: tuple[ChannelRecord, ...], index: int, item: ChannelRecord
) -> tuple[ChannelRecord, ...]:
    return items[:index] + (item,) + items[index + 1 :]
