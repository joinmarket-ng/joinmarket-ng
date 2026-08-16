"""Pre-escrow authorization and durable binding state for one maker ring edge."""

from __future__ import annotations

import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jmswap.errors import ChannelInputEscrowError

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_TRANSPORT_CHARS = re.compile(r"^[A-Za-z0-9_-]+$")
_FROZEN_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)
_MUTABLE_MODEL_CONFIG = ConfigDict(extra="forbid", strict=True)


def _validate_hex(value: str, *, byte_length: int | None = None, name: str) -> str:
    if not value or len(value) % 2 or any(character not in _HEX_DIGITS for character in value):
        raise ValueError(f"{name} must be non-empty hexadecimal")
    if byte_length is not None and len(value) != byte_length * 2:
        raise ValueError(f"{name} must be exactly {byte_length} bytes")
    return value.lower()


def _validate_compressed_pubkey(value: str, *, name: str) -> str:
    normalized = _validate_hex(value, byte_length=33, name=name)
    if not normalized.startswith(("02", "03")):
        raise ValueError(f"{name} must be a compressed public key")
    return normalized


def _validate_ln_node(value: str, *, name: str) -> str:
    pubkey, separator, endpoint = value.partition("@")
    if not separator or not endpoint or "@" in endpoint:
        raise ValueError(f"{name} must be compressed hex pubkey@non-empty endpoint")
    normalized_pubkey = _validate_compressed_pubkey(pubkey, name=f"{name} public key")
    return f"{normalized_pubkey}@{endpoint}"


class RingEdgeRole(StrEnum):
    """The two maker roles for a pre-escrow ring edge."""

    FUNDER = "funder"
    RECEIVER = "receiver"


class RingChannelPoint(BaseModel):
    """The finalized on-chain channel funding outpoint."""

    model_config = _FROZEN_MODEL_CONFIG

    txid: str
    vout: int = Field(ge=0, le=0xFFFFFFFF)

    @field_validator("txid")
    @classmethod
    def validate_txid(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="txid")


class RingEdgeAuthorization(BaseModel):
    """Immutable authorization for one directed maker-to-maker channel edge."""

    model_config = _FROZEN_MODEL_CONFIG

    version: Literal[1] = 1
    edge_id: str
    coinjoin_context: str
    taker_nick: str = Field(min_length=1)
    taker_session_pubkey: str
    funder_nick: str = Field(min_length=1)
    receiver_nick: str = Field(min_length=1)
    funder_session_pubkey: str
    receiver_session_pubkey: str
    funder_ln_node: str
    receiver_ln_node: str
    coinjoin_amount_sats: int = Field(gt=0)
    capacity_sats: int = Field(gt=0)
    funder_contribution_sats: int = Field(gt=0)
    receiver_contribution_sats: int = Field(gt=0)
    expires_at_unix: int = Field(gt=0)

    @field_validator(
        "edge_id",
        "coinjoin_context",
        "taker_session_pubkey",
        "funder_session_pubkey",
        "receiver_session_pubkey",
    )
    @classmethod
    def validate_32_byte_hex(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "value")
        return _validate_hex(value, byte_length=32, name=field_name)

    @field_validator("funder_ln_node", "receiver_ln_node")
    @classmethod
    def validate_ln_node(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "LN node")
        return _validate_ln_node(value, name=field_name)

    @model_validator(mode="after")
    def validate_ring_edge(self) -> Self:
        if len({self.taker_nick, self.funder_nick, self.receiver_nick}) != 3:
            raise ValueError("taker, funder, and receiver nicks must be distinct")
        if (
            len(
                {
                    self.taker_session_pubkey,
                    self.funder_session_pubkey,
                    self.receiver_session_pubkey,
                }
            )
            != 3
        ):
            raise ValueError("taker, funder, and receiver session keys must be distinct")
        funder_node_pubkey = self.funder_ln_node.partition("@")[0]
        receiver_node_pubkey = self.receiver_ln_node.partition("@")[0]
        if funder_node_pubkey == receiver_node_pubkey:
            raise ValueError("funder and receiver LN node public keys must be distinct")
        if self.capacity_sats != self.funder_contribution_sats + self.receiver_contribution_sats:
            raise ValueError("funder and receiver contributions must exactly conserve capacity")
        return self

    @property
    def authorization_id(self) -> str:
        """Return the SHA256 commitment to the canonical authorization payload."""
        return hashlib.sha256(self._canonical_json()).hexdigest()

    def _canonical_json(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")

    def role_for(self, local_nick: str) -> RingEdgeRole:
        """Return ``local_nick``'s maker role for this edge."""
        if local_nick == self.funder_nick:
            return RingEdgeRole.FUNDER
        if local_nick == self.receiver_nick:
            return RingEdgeRole.RECEIVER
        raise ChannelInputEscrowError("local nick is not an authorized ring edge endpoint")

    def local_contribution_sats(self, local_nick: str) -> int:
        """Return the capacity contribution assigned to ``local_nick``."""
        if self.role_for(local_nick) is RingEdgeRole.FUNDER:
            return self.funder_contribution_sats
        return self.receiver_contribution_sats

    def peer_nick(self, local_nick: str) -> str:
        """Return the opposite maker endpoint for ``local_nick``."""
        if self.role_for(local_nick) is RingEdgeRole.FUNDER:
            return self.receiver_nick
        return self.funder_nick

    def peer_session_pubkey(self, local_nick: str) -> str:
        """Return the opposite maker's session public key."""
        if self.role_for(local_nick) is RingEdgeRole.FUNDER:
            return self.receiver_session_pubkey
        return self.funder_session_pubkey

    def peer_ln_node(self, local_nick: str) -> str:
        """Return the opposite maker's advertised Lightning node endpoint."""
        if self.role_for(local_nick) is RingEdgeRole.FUNDER:
            return self.receiver_ln_node
        return self.funder_ln_node

    def to_transport(self) -> str:
        """Encode canonical authorization JSON as unpadded URL-safe base64."""
        return base64.urlsafe_b64encode(self._canonical_json()).rstrip(b"=").decode("ascii")

    @classmethod
    def from_transport(cls, value: str) -> Self:
        """Decode and strictly validate a URL-safe base64 authorization payload."""
        if not isinstance(value, str) or not value:
            raise ChannelInputEscrowError("ring authorization transport must be a non-empty string")
        if not _TRANSPORT_CHARS.fullmatch(value):
            raise ChannelInputEscrowError(
                "ring authorization transport must be unpadded URL-safe base64"
            )
        try:
            decoded = base64.b64decode(
                value + "=" * (-len(value) % 4), altchars=b"-_", validate=True
            )
            authorization = cls.model_validate_json(decoded)
        except (UnicodeDecodeError, ValueError, ValidationError, binascii.Error) as exc:
            raise ChannelInputEscrowError("malformed ring authorization transport") from exc
        return cast(Self, authorization)


class RingEdgeFunding(BaseModel):
    """Funding output relayed to one authorized maker endpoint with the CoinJoin."""

    model_config = _FROZEN_MODEL_CONFIG

    edge_id: str
    authorization_id: str
    funding_address: str = Field(min_length=1)
    contribution_sats: int = Field(gt=0)

    @field_validator("edge_id", "authorization_id")
    @classmethod
    def validate_identifier(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "identifier")
        return _validate_hex(value, byte_length=32, name=field_name)

    def to_transport(self) -> str:
        """Encode the relay as a compact, whitespace-free token."""
        return ":".join(
            (
                self.edge_id,
                self.authorization_id,
                self.funding_address,
                str(self.contribution_sats),
            )
        )

    @classmethod
    def from_transport(cls, value: str) -> Self:
        """Decode one compact funding relay token."""
        try:
            edge_id, authorization_id, address, contribution = value.split(":", 3)
            return cast(
                Self,
                cls(
                    edge_id=edge_id,
                    authorization_id=authorization_id,
                    funding_address=address,
                    contribution_sats=int(contribution),
                ),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ChannelInputEscrowError("malformed ring edge funding relay") from exc


class RingEdgeAuthorizationRecord(BaseModel):
    """Mutable local state for a persisted ring edge authorization."""

    model_config = _MUTABLE_MODEL_CONFIG

    authorization: RingEdgeAuthorization
    local_nick: str = Field(min_length=1)
    funding_address: str | None = Field(default=None, min_length=1)
    funding_scriptpubkey: str | None = None
    pending_channel_id: str | None = Field(default=None, min_length=1)
    funding_canceled: bool = False
    funding_cancel_completed: bool = False
    channel_point: RingChannelPoint | None = None
    funding_completed: bool = False
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("funding_scriptpubkey")
    @classmethod
    def validate_funding_scriptpubkey(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_hex(value, name="funding_scriptpubkey")

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        try:
            self.authorization.role_for(self.local_nick)
        except ChannelInputEscrowError as exc:
            raise ValueError(str(exc)) from exc
        if (self.funding_address is None) != (self.funding_scriptpubkey is None):
            raise ValueError("funding address and scriptpubkey must be bound together")
        if self.funding_cancel_completed and (
            not self.funding_canceled or self.pending_channel_id is None
        ):
            raise ValueError("completed funding cancellation requires a canceled pending channel")
        if self.channel_point is not None and self.funding_address is None:
            raise ValueError("channel point requires a funding binding")
        if self.funding_completed and self.channel_point is None:
            raise ValueError("completed funding requires a channel point")
        if self.funding_completed and self.funding_canceled:
            raise ValueError("completed funding cannot be canceled")
        if self.funding_completed and self.pending_channel_id is None:
            raise ValueError("completed funding requires a pending channel ID")
        if self.created_at.tzinfo != UTC or self.updated_at.tzinfo != UTC:
            raise ValueError("record timestamps must use UTC")
        return self


class RingEdgeAuthorizationStore:
    """A crash-safe, one-file-per-edge authorization record store."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, edge_id: str) -> Path:
        try:
            _validate_hex(edge_id, byte_length=32, name="edge ID")
        except (TypeError, ValueError) as exc:
            raise ChannelInputEscrowError("edge ID must be 32-byte hexadecimal") from exc
        digest = hashlib.sha256(edge_id.encode("ascii")).hexdigest()
        return self._dir / f"{digest}.json"

    @contextlib.contextmanager
    def _edge_lock(self, edge_id: str) -> Iterator[None]:
        lock_path = self._path(edge_id).with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _load_path(self, path: Path, edge_id: str) -> RingEdgeAuthorizationRecord | None:
        if not path.exists():
            return None
        try:
            record = RingEdgeAuthorizationRecord.model_validate_json(path.read_text())
        except (OSError, ValidationError, ValueError) as exc:
            raise ChannelInputEscrowError(
                "persisted ring authorization record is malformed"
            ) from exc
        if record.authorization.edge_id != edge_id:
            raise ChannelInputEscrowError(
                "persisted ring authorization does not match requested edge ID"
            )
        return record

    def _write_record(self, record: RingEdgeAuthorizationRecord) -> None:
        path = self._path(record.authorization.edge_id)
        data = record.model_dump_json()
        fd, temporary_path = tempfile.mkstemp(dir=self._dir, prefix=".ring-edge-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as file_handle:
                file_handle.write(data)
                file_handle.flush()
                os.fsync(file_handle.fileno())
            os.replace(temporary_path, path)
            self._sync_directory()
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary_path)
            raise

    def _sync_directory(self) -> None:
        directory_fd = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    @staticmethod
    def _updated_record(
        record: RingEdgeAuthorizationRecord, **updates: object
    ) -> RingEdgeAuthorizationRecord:
        values = record.model_dump()
        values.update(updates)
        try:
            return RingEdgeAuthorizationRecord.model_validate(values)
        except ValidationError as exc:
            raise ChannelInputEscrowError(str(exc)) from exc

    def authorize(
        self,
        authorization: RingEdgeAuthorization,
        *,
        local_nick: str,
        now_unix: int | None = None,
    ) -> RingEdgeAuthorizationRecord:
        """Persist an authorization, rejecting expired or conflicting edge replays."""
        if not isinstance(authorization, RingEdgeAuthorization):
            raise ChannelInputEscrowError("ring edge authorization is invalid")
        now = int(datetime.now(UTC).timestamp()) if now_unix is None else now_unix
        if authorization.expires_at_unix <= now:
            raise ChannelInputEscrowError("ring edge authorization has expired")
        edge_id = authorization.edge_id
        with self._edge_lock(edge_id):
            path = self._path(edge_id)
            existing = self._load_path(path, edge_id)
            if existing is not None:
                if existing.authorization == authorization and existing.local_nick == local_nick:
                    return existing
                raise ChannelInputEscrowError(
                    "ring edge ID replay conflicts with persisted authorization"
                )
            try:
                record = RingEdgeAuthorizationRecord(
                    authorization=authorization,
                    local_nick=local_nick,
                )
            except ValidationError as exc:
                raise ChannelInputEscrowError(str(exc)) from exc
            self._write_record(record)
            return record

    def load(self, edge_id: str) -> RingEdgeAuthorizationRecord | None:
        """Load an edge record or return ``None`` when it has not been persisted."""
        return self._load_path(self._path(edge_id), edge_id)

    def list(self) -> list[RingEdgeAuthorizationRecord]:
        """Return valid records while leaving corrupt artifacts intact for recovery review."""
        records: list[RingEdgeAuthorizationRecord] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                record = RingEdgeAuthorizationRecord.model_validate_json(path.read_text())
                if path != self._path(record.authorization.edge_id):
                    continue
                records.append(record)
            except (OSError, ValidationError, ValueError, ChannelInputEscrowError):
                continue
        return records

    def bind_funding(
        self,
        edge_id: str,
        authorization_id: str,
        address: str,
        scriptpubkey: str,
        *,
        pending_channel_id: str | None = None,
    ) -> RingEdgeAuthorizationRecord:
        """Irreversibly bind an authorized edge to its pending funding output."""
        with self._edge_lock(edge_id):
            record = self._load_path(self._path(edge_id), edge_id)
            if record is None:
                raise ChannelInputEscrowError("ring edge authorization record does not exist")
            if authorization_id != record.authorization.authorization_id:
                raise ChannelInputEscrowError("ring edge authorization hash does not match")
            if record.funding_address is not None:
                if (
                    record.funding_address == address
                    and record.funding_scriptpubkey == scriptpubkey
                    and record.pending_channel_id == pending_channel_id
                ):
                    return record
                raise ChannelInputEscrowError("ring edge funding binding is immutable")
            if record.funding_canceled:
                raise ChannelInputEscrowError("ring edge funding binding was canceled")
            if record.funding_completed:
                raise ChannelInputEscrowError("ring edge funding is already completed")
            if (
                record.pending_channel_id is not None
                and pending_channel_id != record.pending_channel_id
            ):
                raise ChannelInputEscrowError("ring edge pending channel ID is immutable")
            updated = self._updated_record(
                record,
                funding_address=address,
                funding_scriptpubkey=scriptpubkey,
                pending_channel_id=record.pending_channel_id or pending_channel_id,
                revision=record.revision + 1,
                updated_at=datetime.now(UTC),
            )
            self._write_record(updated)
            return updated

    def reserve_funding(
        self, edge_id: str, authorization_id: str, pending_channel_id: str
    ) -> RingEdgeAuthorizationRecord:
        """Durably reserve a caller-selected external funding ID before opening."""
        if not isinstance(pending_channel_id, str) or not pending_channel_id:
            raise ChannelInputEscrowError("pending channel ID must be non-empty")
        with self._edge_lock(edge_id):
            record = self._load_path(self._path(edge_id), edge_id)
            if record is None:
                raise ChannelInputEscrowError("ring edge authorization record does not exist")
            if authorization_id != record.authorization.authorization_id:
                raise ChannelInputEscrowError("ring edge authorization hash does not match")
            if (
                record.funding_canceled
                or record.funding_completed
                or record.funding_address is not None
            ):
                raise ChannelInputEscrowError("ring edge funding cannot be reserved")
            if record.pending_channel_id is not None:
                if record.pending_channel_id == pending_channel_id:
                    return record
                raise ChannelInputEscrowError("ring edge pending channel ID is immutable")
            updated = self._updated_record(
                record,
                pending_channel_id=pending_channel_id,
                revision=record.revision + 1,
                updated_at=datetime.now(UTC),
            )
            self._write_record(updated)
            return updated

    def cancel_funding(self, edge_id: str, authorization_id: str) -> RingEdgeAuthorizationRecord:
        """Durably abandon a pending funding binding so it cannot be replayed."""
        with self._edge_lock(edge_id):
            record = self._load_path(self._path(edge_id), edge_id)
            if record is None:
                raise ChannelInputEscrowError("ring edge authorization record does not exist")
            if authorization_id != record.authorization.authorization_id:
                raise ChannelInputEscrowError("ring edge authorization hash does not match")
            if record.funding_completed:
                raise ChannelInputEscrowError("ring edge funding is already completed")
            if record.funding_address is None and record.pending_channel_id is None:
                raise ChannelInputEscrowError(
                    "ring edge funding cancellation requires a funding binding"
                )
            if record.funding_canceled:
                return record
            updated = self._updated_record(
                record,
                funding_canceled=True,
                revision=record.revision + 1,
                updated_at=datetime.now(UTC),
            )
            self._write_record(updated)
            return updated

    def complete_funding_cancellation(
        self, edge_id: str, authorization_id: str
    ) -> RingEdgeAuthorizationRecord:
        """Record successful cancellation of a durably identified pending open."""
        with self._edge_lock(edge_id):
            record = self._load_path(self._path(edge_id), edge_id)
            if record is None:
                raise ChannelInputEscrowError("ring edge authorization record does not exist")
            if authorization_id != record.authorization.authorization_id:
                raise ChannelInputEscrowError("ring edge authorization hash does not match")
            if not record.funding_canceled or record.pending_channel_id is None:
                raise ChannelInputEscrowError("ring edge funding cancellation was not requested")
            if record.funding_cancel_completed:
                return record
            updated = self._updated_record(
                record,
                funding_cancel_completed=True,
                revision=record.revision + 1,
                updated_at=datetime.now(UTC),
            )
            self._write_record(updated)
            return updated

    def complete_funding(
        self,
        edge_id: str,
        authorization_id: str,
        channel_point: RingChannelPoint,
    ) -> RingEdgeAuthorizationRecord:
        """Durably record successful backend completion of an intended channel point."""
        if not isinstance(channel_point, RingChannelPoint):
            raise ChannelInputEscrowError("ring edge channel point is invalid")
        with self._edge_lock(edge_id):
            record = self._load_path(self._path(edge_id), edge_id)
            if record is None:
                raise ChannelInputEscrowError("ring edge authorization record does not exist")
            if authorization_id != record.authorization.authorization_id:
                raise ChannelInputEscrowError("ring edge authorization hash does not match")
            if record.funding_canceled:
                raise ChannelInputEscrowError("ring edge funding binding was canceled")
            if record.pending_channel_id is None:
                raise ChannelInputEscrowError(
                    "ring edge funding completion requires a pending channel ID"
                )
            if record.channel_point is None:
                raise ChannelInputEscrowError(
                    "ring edge funding completion requires a channel point"
                )
            if record.channel_point != channel_point:
                raise ChannelInputEscrowError("ring edge channel point binding is immutable")
            if record.funding_completed:
                return record
            updated = self._updated_record(
                record,
                funding_completed=True,
                revision=record.revision + 1,
                updated_at=datetime.now(UTC),
            )
            self._write_record(updated)
            return updated

    def bind_channel_point(
        self,
        edge_id: str,
        authorization_id: str,
        channel_point: RingChannelPoint,
    ) -> RingEdgeAuthorizationRecord:
        """Irreversibly bind the finalized channel outpoint after funding is bound."""
        if not isinstance(channel_point, RingChannelPoint):
            raise ChannelInputEscrowError("ring edge channel point is invalid")
        with self._edge_lock(edge_id):
            record = self._load_path(self._path(edge_id), edge_id)
            if record is None:
                raise ChannelInputEscrowError("ring edge authorization record does not exist")
            if authorization_id != record.authorization.authorization_id:
                raise ChannelInputEscrowError("ring edge authorization hash does not match")
            if record.funding_address is None:
                raise ChannelInputEscrowError("ring edge channel point requires a funding binding")
            if record.funding_canceled:
                raise ChannelInputEscrowError("ring edge funding binding was canceled")
            if record.channel_point is not None:
                if record.channel_point == channel_point:
                    return record
                raise ChannelInputEscrowError("ring edge channel point binding is immutable")
            updated = self._updated_record(
                record,
                channel_point=channel_point,
                revision=record.revision + 1,
                updated_at=datetime.now(UTC),
            )
            self._write_record(updated)
            return updated
