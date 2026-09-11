"""Crash-safe, node-local zero-fee circular Lightning maintenance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from jmcore.channel_maintenance import ChannelMaintenanceConfig
from jmcore.constants import MAX_MONEY
from jmcore.secure_files import atomic_write_private, ensure_private_directory, read_private_file
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jmswap.lnd import (
    ChannelInputCandidate,
    MaintenanceChannel,
    MaintenanceInvoice,
    MaintenancePayment,
    PeerRouteHop,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - the service requires POSIX flock in production.
    fcntl = None  # type: ignore[assignment]


MAINTENANCE_MESSAGE_TYPE = 65_501
MAX_PEER_MESSAGE_BYTES = 8_192
MAX_PEER_REQUESTS_PER_MINUTE = 6
MAX_WIRE_EXPIRY = 4_102_444_800
MAX_SPEND_PAUSE_SECONDS = 30
SPEND_PAUSE_QUARANTINE_SECONDS = 60
MAX_PENDING_PEER_QUERIES = 16
MAX_SPEND_PAUSE_REPLAY_RECORDS = 256
SPEND_PAUSE_FEATURE: Literal["spend_pause_v1"] = "spend_pause_v1"
_NETWORK_CHAIN_HASHES = {
    "mainnet": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
    "testnet": "000000000933ea01ad0ee984209779baae8c49fdbd7a3b0c0eeb0b1e0b6f0d5c",
    "signet": "00000008819873e925422c1ff0f99f7cc9bbb232af63a077a480a3633bee1ef6",
    "regtest": "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206",
}
_STATE_FILENAME = "maintenance-v1.json"
_STATE_LOCK_FILENAME = "maintenance-v1.state.lock"
_SERVICE_LOCK_FILENAME = "maintenance-v1.service.lock"
_PEER_RECONNECT_BACKOFF_SECONDS = 1
_NODE_ID_RE = r"^(02|03)[0-9a-f]{64}$"
_CHANNEL_POINT_RE = r"^[0-9a-f]{64}:(?:0|[1-9][0-9]{0,9})$"
_HEX32_RE = r"^[0-9a-f]{64}$"


class MaintenanceError(Exception):
    """Base error for maintenance service state and coordination."""


class MaintenanceStateError(MaintenanceError):
    """Persisted maintenance state is unsafe to read or replace."""


class MaintenanceLockError(MaintenanceError):
    """Another process already owns maintenance for this LND identity."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EnrollmentRecord(_StrictModel):
    """Local affirmative enrollment, never sent over the peer protocol."""

    node_id: str
    peer_node_id: str
    channel_point: str
    origin_id: str
    status: Literal["pending_validation", "fee_init_intent", "fee_init_unknown", "enrolled"]

    @field_validator("node_id", "peer_node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if re.fullmatch(_NODE_ID_RE, value) is None:
            raise ValueError("node ID must be a lowercase compressed public key")
        return value

    @field_validator("channel_point")
    @classmethod
    def validate_channel_point(cls, value: str) -> str:
        if re.fullmatch(_CHANNEL_POINT_RE, value) is None:
            raise ValueError("channel point must be lowercase txid:vout")
        return value

    @field_validator("origin_id")
    @classmethod
    def validate_origin_id(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("origin ID must be the opaque lowercase original funding txid")
        return value

    @model_validator(mode="after")
    def validate_endpoints(self) -> EnrollmentRecord:
        if self.node_id == self.peer_node_id:
            raise ValueError("maintenance enrollment endpoints must differ")
        return self


class PaymentAttempt(_StrictModel):
    """A payment journal record that blocks any replacement attempt."""

    phase: Literal["created_invoice", "dispatch_intent", "unresolved"]
    payment_hash: str
    payment_request: str = Field(min_length=1, max_length=10_000)
    amount_sat: int = Field(gt=0, le=MAX_MONEY)
    outgoing_channel_id: int = Field(gt=0)
    outgoing_channel_point: str
    return_channel_point: str
    middle_peer_node_id: str
    return_peer_node_id: str
    expires_at: int = Field(ge=1, le=MAX_WIRE_EXPIRY)
    fee_budget_msat: Literal[0] = 0
    invoice_preimage: str | None = Field(default=None, repr=False)
    return_channel_id: int | None = Field(default=None, gt=0, le=(1 << 64) - 1)

    @model_validator(mode="after")
    def validate_hold_invoice(self) -> PaymentAttempt:
        if (self.invoice_preimage is None) != (self.return_channel_id is None):
            raise ValueError("hold invoice requires both preimage and receiving channel")
        if self.invoice_preimage is not None:
            if re.fullmatch(_HEX32_RE, self.invoice_preimage) is None:
                raise ValueError("hold invoice preimage must be lowercase 32-byte hexadecimal")
            if (
                hashlib.sha256(bytes.fromhex(self.invoice_preimage)).hexdigest()
                != self.payment_hash
            ):
                raise ValueError("hold invoice preimage does not match payment hash")
        return self

    @field_validator("payment_hash")
    @classmethod
    def validate_payment_hash(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("payment hash must be lowercase 32-byte hexadecimal")
        return value

    @field_validator("outgoing_channel_point", "return_channel_point")
    @classmethod
    def validate_attempt_channel_point(cls, value: str) -> str:
        if re.fullmatch(_CHANNEL_POINT_RE, value) is None:
            raise ValueError("payment channel point must be lowercase txid:vout")
        return value

    @field_validator("middle_peer_node_id", "return_peer_node_id")
    @classmethod
    def validate_attempt_node_id(cls, value: str) -> str:
        if re.fullmatch(_NODE_ID_RE, value) is None:
            raise ValueError("payment peer ID must be a lowercase compressed public key")
        return value


class RemoteSpendPauseRecord(_StrictModel):
    """Durable replay and rolling-budget evidence for an authenticated remote pause."""

    request_id: str
    peer_node_id: str
    chain_hash: str
    channel_point: str
    expiry: int = Field(ge=1, le=MAX_WIRE_EXPIRY)
    accepted_at: int = Field(ge=0, le=MAX_WIRE_EXPIRY)
    status: Literal["paused", "released", "expired"]

    @field_validator("request_id", "chain_hash")
    @classmethod
    def validate_hex32(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("spend pause binding must be lowercase 32-byte hexadecimal")
        return value

    @field_validator("peer_node_id")
    @classmethod
    def validate_peer_node_id(cls, value: str) -> str:
        return EnrollmentRecord.validate_node_id(value)

    @field_validator("channel_point")
    @classmethod
    def validate_channel_point(cls, value: str) -> str:
        return EnrollmentRecord.validate_channel_point(value)

    @model_validator(mode="after")
    def validate_deadline(self) -> RemoteSpendPauseRecord:
        if self.accepted_at > self.expiry:
            raise ValueError("spend pause acceptance cannot follow its expiry")
        return self


class OperatorPaymentResolution(_StrictModel):
    payment_hash: str
    terminal_status: Literal["succeeded", "failed"]
    recorded_at: int = Field(ge=1, le=MAX_WIRE_EXPIRY)

    @field_validator("payment_hash")
    @classmethod
    def validate_payment_hash(cls, value: str) -> str:
        return PaymentAttempt.validate_payment_hash(value)


class _MaintenanceState(_StrictModel):
    version: Literal[1] = 1
    node_id: str | None = None
    enrollments: dict[str, EnrollmentRecord] = Field(default_factory=dict)
    admission_channels: list[str] = Field(default_factory=list)
    spend_guards: dict[str, str] = Field(default_factory=dict)
    remote_spend_pauses: dict[str, RemoteSpendPauseRecord] = Field(
        default_factory=dict, max_length=MAX_SPEND_PAUSE_REPLAY_RECORDS
    )
    spend_pause_session_id: str | None = None
    spend_pause_quarantine_until: int = Field(default=0, ge=0)
    spend_pause_wall_high_water: int = Field(default=0, ge=0, le=MAX_WIRE_EXPIRY)
    next_due: int | None = Field(default=None, ge=1, le=MAX_WIRE_EXPIRY)
    payment: PaymentAttempt | None = None
    operator_payment_resolution: OperatorPaymentResolution | None = None

    @model_validator(mode="after")
    def validate_enrollment_keys(self) -> _MaintenanceState:
        if self.node_id is not None:
            EnrollmentRecord.validate_node_id(self.node_id)
            if any(record.node_id != self.node_id for record in self.enrollments.values()):
                raise ValueError("maintenance state contains another node's enrollment")
        if self.spend_guards and self.node_id is None:
            raise ValueError("spend guards require a bound LND identity")
        if self.remote_spend_pauses and self.node_id is None:
            raise ValueError("remote spend pauses require a bound LND identity")
        if (
            self.spend_pause_session_id is not None
            and re.fullmatch(_HEX32_RE, self.spend_pause_session_id) is None
        ):
            raise ValueError("spend pause session ID must be lowercase 32-byte hexadecimal")
        if len(set(self.admission_channels)) != len(self.admission_channels) or any(
            point not in self.enrollments for point in self.admission_channels
        ):
            raise ValueError("admission markers must identify unique enrolled channels")
        for point, owner in self.spend_guards.items():
            EnrollmentRecord.validate_channel_point(point)
            _validate_spend_owner(owner)
        if any(
            channel_point != record.channel_point
            for channel_point, record in self.enrollments.items()
        ):
            raise ValueError("maintenance enrollment key differs from its channel point")
        for request_id, pause in self.remote_spend_pauses.items():
            if request_id != pause.request_id:
                raise ValueError("remote spend pause key differs from its request ID")
            enrollment = self.enrollments.get(pause.channel_point)
            if (
                enrollment is None
                or enrollment.node_id != self.node_id
                or enrollment.peer_node_id != pause.peer_node_id
                or enrollment.status != "enrolled"
                or pause.channel_point not in self.admission_channels
            ):
                raise ValueError("remote spend pause lacks a confirmed enrolled channel")
        return self


def _validate_spend_owner(owner: str) -> None:
    if not isinstance(owner, str) or not 1 <= len(owner) <= 256 or not owner.isprintable():
        raise ValueError("spend owner must be a bounded nonempty printable identifier")


def _require_state_node(state: _MaintenanceState, node_id: str) -> None:
    EnrollmentRecord.validate_node_id(node_id)
    if (state.node_id is not None and state.node_id != node_id) or any(
        item.node_id != node_id for item in state.enrollments.values()
    ):
        raise MaintenanceStateError("maintenance directory belongs to another LND node")


class _RouteHop(_StrictModel):
    node_id: str
    channel_id: int = Field(gt=0)
    base_fee_msat: int = Field(ge=0)
    fee_ppm: int = Field(ge=0)
    cltv_delta: int = Field(gt=0, le=144)

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if re.fullmatch(_NODE_ID_RE, value) is None:
            raise ValueError("hop node ID must be a lowercase compressed public key")
        return value

    def as_lnd_hop(self) -> PeerRouteHop:
        return PeerRouteHop(
            node_id=self.node_id,
            channel_id=self.channel_id,
            base_fee_msat=self.base_fee_msat,
            fee_ppm=self.fee_ppm,
            cltv_delta=self.cltv_delta,
        )


class RouteRequest(_StrictModel):
    version: Literal[1] = 1
    kind: Literal["route_request"] = "route_request"
    request_id: str
    destination_node_id: str
    amount_sat: int = Field(gt=0, le=MAX_MONEY)
    expiry: int = Field(ge=1, le=MAX_WIRE_EXPIRY)

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("request ID must be a lowercase random 32-byte hexadecimal value")
        return value

    @field_validator("destination_node_id")
    @classmethod
    def validate_destination_node_id(cls, value: str) -> str:
        if re.fullmatch(_NODE_ID_RE, value) is None:
            raise ValueError("destination node ID must be a lowercase compressed public key")
        return value


class RouteResponse(_StrictModel):
    version: Literal[1] = 1
    kind: Literal["route_response"] = "route_response"
    request_id: str
    hop: _RouteHop | None = None
    expiry: int = Field(ge=1, le=MAX_WIRE_EXPIRY)

    @field_validator("request_id")
    @classmethod
    def validate_request_id(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("request ID must be a lowercase random 32-byte hexadecimal value")
        return value


class SpendPauseCapabilityRequest(_StrictModel):
    """One explicit, short-lived request for the private spend-pause feature."""

    version: Literal[1] = 1
    kind: Literal["capability_request"] = "capability_request"
    feature: Literal["spend_pause_v1"] = SPEND_PAUSE_FEATURE
    request_id: str
    chain_hash: str
    expiry: int = Field(ge=1, le=MAX_WIRE_EXPIRY)

    @field_validator("request_id", "chain_hash")
    @classmethod
    def validate_hex32(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("capability binding must be lowercase 32-byte hexadecimal")
        return value


class SpendPauseCapabilityReply(_StrictModel):
    """Authenticated capability reply, bound exactly to one peer request."""

    version: Literal[1] = 1
    kind: Literal["capability_reply"] = "capability_reply"
    feature: Literal["spend_pause_v1"] = SPEND_PAUSE_FEATURE
    request_id: str
    chain_hash: str
    expiry: int = Field(ge=1, le=MAX_WIRE_EXPIRY)

    @field_validator("request_id", "chain_hash")
    @classmethod
    def validate_hex32(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("capability binding must be lowercase 32-byte hexadecimal")
        return value


class SpendPauseRequest(_StrictModel):
    """Authenticated advisory request to avoid one currently confirmed channel."""

    version: Literal[1] = 1
    kind: Literal["spend_pause"] = "spend_pause"
    chain_hash: str
    channel_point: str
    request_id: str
    expiry: int = Field(ge=1, le=MAX_WIRE_EXPIRY)

    @field_validator("chain_hash", "request_id")
    @classmethod
    def validate_hex32(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("spend pause binding must be lowercase 32-byte hexadecimal")
        return value

    @field_validator("channel_point")
    @classmethod
    def validate_channel_point(cls, value: str) -> str:
        return EnrollmentRecord.validate_channel_point(value)


class SpendPauseReply(_StrictModel):
    """Exact result for a spend-pause request, never an authorization decision."""

    version: Literal[1] = 1
    kind: Literal["spend_reply"] = "spend_reply"
    status: Literal["paused", "busy"]
    chain_hash: str
    channel_point: str
    request_id: str
    expiry: int = Field(ge=1, le=MAX_WIRE_EXPIRY)

    @field_validator("chain_hash", "request_id")
    @classmethod
    def validate_hex32(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("spend pause binding must be lowercase 32-byte hexadecimal")
        return value

    @field_validator("channel_point")
    @classmethod
    def validate_channel_point(cls, value: str) -> str:
        return EnrollmentRecord.validate_channel_point(value)


class SpendPauseRelease(_StrictModel):
    """Authenticated advisory release, bound to the exact original pause request."""

    version: Literal[1] = 1
    kind: Literal["spend_release"] = "spend_release"
    chain_hash: str
    channel_point: str
    request_id: str
    expiry: int = Field(ge=1, le=MAX_WIRE_EXPIRY)

    @field_validator("chain_hash", "request_id")
    @classmethod
    def validate_hex32(cls, value: str) -> str:
        if re.fullmatch(_HEX32_RE, value) is None:
            raise ValueError("spend pause binding must be lowercase 32-byte hexadecimal")
        return value

    @field_validator("channel_point")
    @classmethod
    def validate_channel_point(cls, value: str) -> str:
        return EnrollmentRecord.validate_channel_point(value)


class MaintenanceBackend(Protocol):
    async def list_maintenance_channels(self) -> tuple[MaintenanceChannel, ...]: ...

    async def list_channel_input_candidates(self) -> tuple[ChannelInputCandidate, ...]: ...

    async def send_peer_message(
        self, peer_node_id: str, message_type: int, payload: bytes
    ) -> None: ...

    def subscribe_peer_messages(self, message_type: int) -> AsyncIterator[tuple[str, bytes]]: ...

    async def create_maintenance_invoice(
        self, amount_sat: int, hops: Sequence[PeerRouteHop], expiry_seconds: int
    ) -> MaintenanceInvoice: ...

    async def send_maintenance_payment(
        self, payment_request: str, outgoing_channel_id: int, timeout_seconds: int
    ) -> MaintenancePayment: ...

    async def track_maintenance_payment(
        self, payment_hash: str, timeout_seconds: int = 5
    ) -> MaintenancePayment: ...

    async def reconcile_maintenance_invoice(
        self, payment_hash: str, preimage: str, return_channel_id: int, amount_sat: int
    ) -> None: ...

    async def set_private_channel_zero_fee(self, channel_point: str) -> None: ...


class MaintenanceRandom(Protocol):
    def randint(self, minimum: int, maximum: int) -> int: ...

    def token_hex(self, nbytes: int) -> str: ...


class _SystemMaintenanceRandom:
    def randint(self, minimum: int, maximum: int) -> int:
        return minimum + secrets.randbelow(maximum - minimum + 1)

    def token_hex(self, nbytes: int) -> str:
        return secrets.token_hex(nbytes)


class _Flock:
    """One owner-only POSIX flock file, stable across atomic state replacement."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self, *, blocking: bool) -> bool:
        if fcntl is None:  # pragma: no cover - unsupported production platform.
            raise MaintenanceLockError("POSIX flock is unavailable")
        if self._fd is not None:
            raise MaintenanceLockError("maintenance lock is already held")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            lock_flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, lock_flags)
            except BlockingIOError:
                os.close(fd)
                return False
            self._fd = fd
            return True
        except Exception:
            os.close(fd)
            raise

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class _StateStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / _STATE_FILENAME
        self._lock = _Flock(directory / _STATE_LOCK_FILENAME)

    def ensure(self) -> None:
        try:
            ensure_private_directory(self.directory)
        except Exception as exc:
            raise MaintenanceStateError("maintenance state directory is unsafe") from exc

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.ensure()
        if not self._lock.acquire(
            blocking=True
        ):  # pragma: no cover - blocking acquisition always wins.
            raise MaintenanceStateError("could not acquire maintenance state lock")
        try:
            yield
        finally:
            self._lock.release()

    def load_unlocked(self) -> _MaintenanceState:
        if not self.path.exists():
            if self.path.is_symlink():
                raise MaintenanceStateError("maintenance state path is a symlink")
            return _MaintenanceState()
        try:
            document = _strict_json_load(read_private_file(self.path))
            return _MaintenanceState.model_validate(document)
        except MaintenanceStateError:
            raise
        except Exception as exc:
            raise MaintenanceStateError(
                "maintenance state is absent, legacy, partial, or corrupt"
            ) from exc

    def save_unlocked(self, state: _MaintenanceState) -> None:
        try:
            payload = (
                json.dumps(
                    state.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("ascii")
                + b"\n"
            )
            atomic_write_private(self.path, payload)
            _fsync_directory(self.directory)
        except Exception as exc:
            raise MaintenanceStateError("could not durably save maintenance state") from exc


def _strict_json_load(payload: bytes) -> object:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object member")
            result[key] = value
        return result

    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=object_without_duplicates,
        parse_constant=reject_constant,
    )


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(directory, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _valid_cltv_delta(value: object) -> bool:
    return type(value) is int and 1 <= value <= 144


def enroll_channel(
    directory: Path | str,
    node_id: str,
    peer_node_id: str,
    channel_point: str,
    origin_id: str,
) -> bool:
    """Persist one affirmative enrollment without discovering or overwriting channels.

    ``origin_id`` is local-only original-funding metadata. It is intentionally
    absent from all wire models and peer messages.
    """

    record = EnrollmentRecord(
        node_id=node_id,
        peer_node_id=peer_node_id,
        channel_point=channel_point,
        origin_id=origin_id,
        status="pending_validation",
    )
    store = _StateStore(Path(directory))
    with store.locked():
        state = store.load_unlocked()
        _require_state_node(state, node_id)
        if record.channel_point in state.enrollments:
            return False
        records = dict(state.enrollments)
        records[record.channel_point] = record
        store.save_unlocked(
            state.model_copy(
                update={
                    "node_id": node_id,
                    "enrollments": records,
                    "admission_channels": sorted([*state.admission_channels, record.channel_point]),
                }
            )
        )
    return True


def claim_spend_channels(
    directory: Path | str, node_id: str, owner: str, channel_points: Sequence[str]
) -> bool:
    """Claim all local inputs before freezing, serialized against payment dispatch.

    The caller must persist its freeze intent first. Claims never expire and may
    only be released after durable cancellation proves the channels resumed.
    All processes controlling one LND identity must share this directory.
    """
    _validate_spend_owner(owner)
    if isinstance(channel_points, (str, bytes)) or not channel_points:
        raise ValueError("spend admission requires a sequence of channel points")
    points = set(channel_points)
    for point in points:
        EnrollmentRecord.validate_channel_point(point)
    store = _StateStore(Path(directory))
    with store.locked():
        state = store.load_unlocked()
        _require_state_node(state, node_id)
        if any(state.spend_guards.get(point, owner) != owner for point in points):
            return False
        payment = state.payment
        if (
            payment is not None
            and payment.phase != "created_invoice"
            and points.intersection((payment.outgoing_channel_point, payment.return_channel_point))
        ):
            return False
        guards = dict(state.spend_guards)
        guards.update(dict.fromkeys(points, owner))
        updated = state.model_copy(update={"node_id": node_id, "spend_guards": guards})
        if updated != state:
            store.save_unlocked(updated)
    return True


def release_spend_channels(
    directory: Path | str, node_id: str, owner: str, channel_points: Sequence[str]
) -> None:
    """Release only this owner's claims after proven, durably recorded cancellation."""
    _validate_spend_owner(owner)
    if isinstance(channel_points, (str, bytes)):
        raise ValueError("spend release requires a sequence of channel points")
    for point in channel_points:
        EnrollmentRecord.validate_channel_point(point)
    store = _StateStore(Path(directory))
    with store.locked():
        state = store.load_unlocked()
        _require_state_node(state, node_id)
        guards = dict(state.spend_guards)
        for point in channel_points:
            if guards.get(point) == owner:
                del guards[point]
        if guards != state.spend_guards:
            store.save_unlocked(state.model_copy(update={"spend_guards": guards}))


def require_channel_enrollment(
    directory: Path | str,
    node_id: str,
    peer_node_id: str,
    channel_point: str,
    origin_id: str,
) -> None:
    """Check a recorded handoff without recreating missing enrollment or fee state."""
    store = _StateStore(Path(directory))
    with store.locked():
        record = store.load_unlocked().enrollments.get(channel_point)
    if record is None or (
        record.node_id != node_id
        or record.peer_node_id != peer_node_id
        or record.origin_id != origin_id
    ):
        raise MaintenanceStateError(
            "recorded channel maintenance enrollment is missing or mismatched"
        )


def maintenance_status(directory: Path | str, node_id: str) -> dict[str, object]:
    """Expose local operational state without invoices, contacts, or round metadata."""
    store = _StateStore(Path(directory))
    with store.locked():
        state = store.load_unlocked()
    _require_state_node(state, node_id)
    counts: dict[str, int] = {}
    for item in state.enrollments.values():
        counts[item.status] = counts.get(item.status, 0) + 1
    return {
        "enrollments": counts,
        "next_due": state.next_due,
        "payment_phase": state.payment.phase if state.payment else None,
        "payment_hash": state.payment.payment_hash if state.payment else None,
        "payment_expires_at": state.payment.expires_at if state.payment else None,
        "automatic_initiation_blocked": state.payment is not None,
        "spend_guard_count": len(state.spend_guards),
        "unreconciled_admission_count": len(state.enrollments) - len(state.admission_channels),
        "operator_payment_resolution": (
            state.operator_payment_resolution.model_dump(mode="json")
            if state.operator_payment_resolution is not None
            else None
        ),
        "uncertain_fee_initializations": counts.get("fee_init_unknown", 0)
        + counts.get("fee_init_intent", 0),
    }


@contextmanager
def _operator_state_store(directory: Path | str) -> Iterator[_StateStore]:
    store = _StateStore(Path(directory))
    store.ensure()
    service_lock = _Flock(store.directory / _SERVICE_LOCK_FILENAME)
    if not service_lock.acquire(blocking=False):
        raise MaintenanceLockError("stop the maintenance service before operator reconciliation")
    try:
        with store.locked():
            yield store
    finally:
        service_lock.release()


def reconcile_maintenance_admission(
    directory: Path | str,
    node_id: str,
    channel_points: Sequence[str],
    *,
    confirmed_idle: bool = False,
) -> None:
    """Record an operator's verified idle state for existing, fully enrolled channels.

    This never discovers channels, releases spend guards, or changes fee policies.
    The operator must reconcile both local spend registries before confirming.
    """
    if confirmed_idle is not True:
        raise ValueError("explicit confirmation of reconciled idle channels is required")
    if isinstance(channel_points, (str, bytes)) or not channel_points:
        raise ValueError("admission reconciliation requires explicit channel points")
    for point in channel_points:
        EnrollmentRecord.validate_channel_point(point)
    with _operator_state_store(directory) as store:
        state = store.load_unlocked()
        _require_state_node(state, node_id)
        for point in channel_points:
            record = state.enrollments.get(point)
            if record is None or record.status != "enrolled":
                raise MaintenanceStateError("only fully enrolled channels can be reconciled")
            if point in state.spend_guards or (
                state.payment is not None
                and point
                in (state.payment.outgoing_channel_point, state.payment.return_channel_point)
            ):
                raise MaintenanceStateError(
                    "channel still has a local spend or maintenance obligation"
                )
        store.save_unlocked(
            state.model_copy(
                update={
                    "node_id": node_id,
                    "admission_channels": sorted(
                        set(state.admission_channels).union(channel_points)
                    ),
                }
            )
        )


def resolve_maintenance_payment(
    directory: Path | str,
    node_id: str,
    payment_hash: str,
    terminal_status: Literal["succeeded", "failed"],
    *,
    confirmed_no_inflight: bool = False,
    recorded_at: int,
    next_due: int,
) -> None:
    """Record an explicit operator resolution when backend history cannot prove one.

    The operator must independently verify the exact terminal payment outcome and
    absence of in-flight HTLCs. Expiry, cancellation, and missing history alone are
    insufficient. Keep an audit marker while clearing only the matching journal.
    """
    if confirmed_no_inflight is not True:
        raise ValueError(
            "explicit confirmation of terminal outcome and no in-flight HTLCs is required"
        )
    resolution = OperatorPaymentResolution(
        payment_hash=payment_hash, terminal_status=terminal_status, recorded_at=recorded_at
    )
    if type(next_due) is not int or not recorded_at < next_due <= MAX_WIRE_EXPIRY:
        raise ValueError("next maintenance time must be in the future")
    with _operator_state_store(directory) as store:
        state = store.load_unlocked()
        _require_state_node(state, node_id)
        if state.payment is None or state.payment.payment_hash != payment_hash:
            raise MaintenanceStateError("payment hash differs from the current maintenance journal")
        store.save_unlocked(
            state.model_copy(
                update={
                    "node_id": node_id,
                    "payment": None,
                    "next_due": next_due,
                    "operator_payment_resolution": resolution,
                }
            )
        )


@dataclass
class _PendingRouteRequest:
    peer_node_id: str
    destination_node_id: str
    expiry: int
    future: asyncio.Future[RouteResponse]


@dataclass
class _PendingSpendRequest:
    peer_node_id: str
    request: SpendPauseCapabilityRequest | SpendPauseRequest
    monotonic_deadline: float
    future: asyncio.Future[SpendPauseCapabilityReply | SpendPauseReply]


class MaintenanceService:
    """One LND-node owner for bounded, explicitly enrolled channel maintenance."""

    def __init__(
        self,
        backend: MaintenanceBackend,
        config: ChannelMaintenanceConfig,
        directory: Path | str,
        node_id: str,
        *,
        clock: Callable[[], float] = time.time,
        monotonic_clock: Callable[[], float] = time.monotonic,
        chain_hash: str | None = None,
        random_source: MaintenanceRandom | None = None,
    ) -> None:
        if re.fullmatch(_NODE_ID_RE, node_id) is None:
            raise ValueError("node_id must be a lowercase compressed public key")
        if chain_hash is not None and re.fullmatch(_HEX32_RE, chain_hash) is None:
            raise ValueError("chain_hash must be a lowercase 32-byte hexadecimal value")
        self.backend = backend
        self.config = config
        self.directory = Path(directory)
        self.node_id = node_id
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        backend_network = getattr(backend, "network", None)
        self.chain_hash = chain_hash or (
            _NETWORK_CHAIN_HASHES.get(backend_network) if isinstance(backend_network, str) else None
        )
        self._random = random_source or _SystemMaintenanceRandom()
        self._store = _StateStore(self.directory)
        self._service_lock = _Flock(self.directory / _SERVICE_LOCK_FILENAME)
        self._stop_event = asyncio.Event()
        self._listener_task: asyncio.Task[None] | None = None
        self._pending_requests: dict[str, _PendingRouteRequest] = {}
        self._pending_spend_requests: dict[str, _PendingSpendRequest] = {}
        self._incoming_request_times: dict[str, deque[int]] = {}
        self._outgoing_request_times: dict[str, deque[int]] = {}
        self._responder_busy: set[str] = set()
        self._spend_pause_session_id = secrets.token_hex(32)
        self._spend_pause_session_ready = False
        self._spend_pause_wall_high_water = 0
        self._active_remote_pause_deadlines: dict[str, float] = {}
        self._remote_pause_budget_deadlines: dict[str, tuple[float, int]] = {}
        self._announced_spend_guards: dict[str, str] = {}
        self._outbound_guard_pauses: dict[str, tuple[str, str, SpendPauseRequest]] = {}

    async def run(self) -> None:
        """Own the service lock and process scheduled work until stopped or cancelled."""

        self._store.ensure()
        if not self._service_lock.acquire(blocking=False):
            raise MaintenanceLockError("maintenance service lock is already held")
        try:
            maintenance_status(self.directory, self.node_id)
            self._stop_event.clear()
            self._listener_task = asyncio.create_task(self._listen_peer_messages())
            while not self._stop_event.is_set():
                await self.tick()
                await self._wait_until_next_tick()
        finally:
            self.stop()
            await self._cancel_listener()
            self._service_lock.release()

    def stop(self) -> None:
        """Request a clean stop; any in-flight payment remains journaled."""

        self._stop_event.set()
        if self._listener_task is not None:
            self._listener_task.cancel()

    async def close(self) -> None:
        """Stop the service and close a concrete backend when it supports closing."""

        self.stop()
        await self._cancel_listener()
        close = getattr(self.backend, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result

    async def tick(self) -> None:
        """Perform one bounded scheduling, reconciliation, or initiation pass."""

        state = self._read_state()
        if state is None:
            return
        if state.payment is not None:
            if state.payment.phase == "created_invoice":
                # This phase precedes durable dispatch authorization, so it cannot have sent.
                self._clear_payment(
                    state.payment.payment_hash, self._now() + self.config.interval_min_seconds
                )
                return
            await self._reconcile_payment(state.payment)
            return
        if not self.config.enabled:
            return
        await self._announce_local_spend_guard(state)
        await self._initialize_enrollments()
        state = self._read_state()
        if state is None:
            return
        if state.next_due is None:
            self._set_next_due(self._now() + self._random_interval())
            return
        if self._now() >= state.next_due:
            await self._attempt_circular_payment()

    async def handle_peer_message(self, peer_node_id: str, payload: bytes) -> None:
        """Handle one LND-authenticated peer message, silently rejecting invalid input."""

        if (
            re.fullmatch(_NODE_ID_RE, peer_node_id) is None
            or not isinstance(payload, bytes)
            or len(payload) > MAX_PEER_MESSAGE_BYTES
        ):
            return
        try:
            document = _strict_json_load(payload)
            if not isinstance(document, dict):
                return
            kind = document.get("kind")
            if kind in {"spend_pause", "spend_release"} and not self._allow_request(
                self._incoming_request_times, peer_node_id
            ):
                return
            if not self._is_enrolled_peer(peer_node_id):
                return
            if kind == "route_request":
                if not self.config.enabled:
                    return
                await self._handle_route_request(
                    peer_node_id, RouteRequest.model_validate(document)
                )
            elif kind == "route_response":
                self._handle_route_response(peer_node_id, RouteResponse.model_validate(document))
            elif kind == "capability_request":
                await self._handle_spend_pause_capability_request(
                    peer_node_id, SpendPauseCapabilityRequest.model_validate(document)
                )
            elif kind == "capability_reply":
                self._handle_spend_pause_capability_reply(
                    peer_node_id, SpendPauseCapabilityReply.model_validate(document)
                )
            elif kind == "spend_pause":
                await self._handle_spend_pause_request(
                    peer_node_id, SpendPauseRequest.model_validate(document)
                )
            elif kind == "spend_reply":
                self._handle_spend_pause_reply(
                    peer_node_id, SpendPauseReply.model_validate(document)
                )
            elif kind == "spend_release":
                self._handle_spend_pause_release(
                    peer_node_id, SpendPauseRelease.model_validate(document)
                )
        except (TypeError, UnicodeDecodeError, ValueError, RecursionError):
            return

    def _now(self) -> int:
        return int(self._clock())

    def _monotonic_now(self) -> float:
        return self._monotonic_clock()

    def _pause_wall_now(self) -> int:
        return max(self._now(), self._spend_pause_wall_high_water)

    def _ensure_spend_pause_session(self) -> bool:
        """Expire inherited remote leases before this process can honor a new one."""

        if self._spend_pause_session_ready:
            return True
        try:
            with self._store.locked():
                state = self._store.load_unlocked()
                _require_state_node(state, self.node_id)
                wall_now = max(self._now(), state.spend_pause_wall_high_water)
                session_changed = state.spend_pause_session_id != self._spend_pause_session_id
                pauses = dict(state.remote_spend_pauses)
                if session_changed:
                    pauses = {
                        request_id: pause.model_copy(update={"status": "expired"})
                        if pause.status == "paused"
                        else pause
                        for request_id, pause in pauses.items()
                    }
                updated = state.model_copy(
                    update={
                        "remote_spend_pauses": pauses,
                        "spend_pause_session_id": self._spend_pause_session_id,
                        "spend_pause_quarantine_until": math.ceil(
                            self._monotonic_now() + SPEND_PAUSE_QUARANTINE_SECONDS
                        )
                        if session_changed
                        else state.spend_pause_quarantine_until,
                        "spend_pause_wall_high_water": wall_now,
                    }
                )
                if updated != state:
                    self._store.save_unlocked(updated)
                self._spend_pause_wall_high_water = updated.spend_pause_wall_high_water
                self._spend_pause_session_ready = True
                return True
        except MaintenanceStateError:
            return False

    def _prune_remote_spend_pauses(
        self, state: _MaintenanceState, wall_now: int
    ) -> dict[str, RemoteSpendPauseRecord]:
        return {
            request_id: pause
            for request_id, pause in state.remote_spend_pauses.items()
            # Retain every replay ID for at least one full budget window, which is also
            # longer than the maximum pause lease and original wire expiry.
            if (
                pause.accepted_at + SPEND_PAUSE_QUARANTINE_SECONDS > wall_now
                or (
                    (budget := self._remote_pause_budget_deadlines.get(request_id)) is not None
                    and budget[0] > self._monotonic_now()
                )
            )
        }

    def _remote_pause_is_active(
        self, state: _MaintenanceState, pause: RemoteSpendPauseRecord
    ) -> bool:
        deadline = self._active_remote_pause_deadlines.get(pause.request_id)
        return (
            state.spend_pause_session_id == self._spend_pause_session_id
            and pause.status == "paused"
            and deadline is not None
            and self._monotonic_now() < deadline
            and self._pause_wall_now() < pause.expiry
        )

    def _active_remote_pause_points(self, state: _MaintenanceState) -> set[str]:
        return {
            pause.channel_point
            for pause in state.remote_spend_pauses.values()
            if self._remote_pause_is_active(state, pause)
        }

    def _read_state(self) -> _MaintenanceState | None:
        if not self._ensure_spend_pause_session():
            return None
        try:
            with self._store.locked():
                state = self._store.load_unlocked()
                _require_state_node(state, self.node_id)
                return state
        except MaintenanceStateError:
            return None

    def _mutate_state(
        self, mutate: Callable[[_MaintenanceState], _MaintenanceState]
    ) -> _MaintenanceState | None:
        if not self._ensure_spend_pause_session():
            return None
        try:
            with self._store.locked():
                current = self._store.load_unlocked()
                _require_state_node(current, self.node_id)
                updated = mutate(current)
                if updated != current:
                    self._store.save_unlocked(updated)
                return updated
        except MaintenanceStateError:
            return None

    def _set_next_due(self, due: int) -> None:
        self._mutate_state(lambda state: state.model_copy(update={"next_due": due}))

    def _random_interval(self) -> int:
        return self._random.randint(
            self.config.interval_min_seconds, self.config.interval_max_seconds
        )

    def _set_backoff(self) -> None:
        self._set_next_due(self._now() + self.config.interval_min_seconds)

    def _is_enrolled_peer(self, peer_node_id: str) -> bool:
        state = self._read_state()
        return state is not None and any(
            record.node_id == self.node_id
            and record.peer_node_id == peer_node_id
            and record.status == "enrolled"
            for record in state.enrollments.values()
        )

    def _transition_enrollment(
        self,
        expected_record: EnrollmentRecord,
        expected_status: str,
        replacement_status: str,
    ) -> bool:
        changed = False

        def mutate(state: _MaintenanceState) -> _MaintenanceState:
            nonlocal changed
            current = state.enrollments.get(expected_record.channel_point)
            if current != expected_record or current.status != expected_status:
                return state
            records = dict(state.enrollments)
            records[current.channel_point] = current.model_copy(
                update={"status": replacement_status}
            )
            changed = True
            return state.model_copy(update={"enrollments": records})

        updated = self._mutate_state(mutate)
        record = (
            updated.enrollments.get(expected_record.channel_point) if updated is not None else None
        )
        return (
            changed
            and record is not None
            and record == expected_record.model_copy(update={"status": replacement_status})
        )

    async def _initialize_enrollments(self) -> None:
        state = self._read_state()
        if state is None:
            return
        for _channel_point, record in state.enrollments.items():
            if record.node_id == self.node_id and record.status == "fee_init_intent":
                # A crash after intent durability cannot prove whether LND accepted the write.
                self._transition_enrollment(record, "fee_init_intent", "fee_init_unknown")
        pending = tuple(
            (channel_point, record)
            for channel_point, record in state.enrollments.items()
            if record.node_id == self.node_id
            and record.status == "pending_validation"
            and channel_point in state.admission_channels
            and channel_point not in state.spend_guards
        )
        if not pending:
            return
        try:
            channels = await self.backend.list_maintenance_channels()
            candidates = await self.backend.list_channel_input_candidates()
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        by_point = {channel.channel_point: channel for channel in channels}
        safe_candidates = {
            f"{candidate.funding_outpoint.txid}:{candidate.funding_outpoint.vout}": candidate
            for candidate in candidates
        }
        for channel_point, record in pending:
            channel = by_point.get(channel_point)
            candidate = safe_candidates.get(channel_point)
            if not self._matches_new_enrollment(record, channel, candidate):
                continue
            if not self._transition_enrollment(record, "pending_validation", "fee_init_intent"):
                continue
            intent_record = record.model_copy(update={"status": "fee_init_intent"})
            try:
                await self.backend.set_private_channel_zero_fee(channel_point)
            except asyncio.CancelledError:
                self._transition_enrollment(intent_record, "fee_init_intent", "fee_init_unknown")
                raise
            except Exception:
                self._transition_enrollment(intent_record, "fee_init_intent", "fee_init_unknown")
            else:
                self._transition_enrollment(intent_record, "fee_init_intent", "enrolled")

    @staticmethod
    def _matches_new_enrollment(
        record: EnrollmentRecord,
        channel: MaintenanceChannel | None,
        candidate: ChannelInputCandidate | None,
    ) -> bool:
        return (
            channel is not None
            and candidate is not None
            and channel.channel_point == record.channel_point
            and channel.peer_node_id == record.peer_node_id
            and channel.private
            and channel.active
            and candidate.peer_node_pubkey == record.peer_node_id
            and candidate.private
            and candidate.active
            and candidate.channel_id == channel.channel_id
        )

    async def _eligible_channels(self) -> tuple[tuple[MaintenanceChannel, EnrollmentRecord], ...]:
        state = self._read_state()
        if state is None:
            return ()
        remote_paused = self._active_remote_pause_points(state)
        enrolled = {
            channel_point: record
            for channel_point, record in state.enrollments.items()
            if record.node_id == self.node_id
            and record.status == "enrolled"
            and channel_point in state.admission_channels
            and channel_point not in state.spend_guards
            and channel_point not in remote_paused
        }
        if not enrolled:
            return ()
        try:
            channels = await self.backend.list_maintenance_channels()
            candidates = await self.backend.list_channel_input_candidates()
        except asyncio.CancelledError:
            raise
        except Exception:
            return ()
        safe_candidates = {
            f"{candidate.funding_outpoint.txid}:{candidate.funding_outpoint.vout}": candidate
            for candidate in candidates
        }
        eligible: list[tuple[MaintenanceChannel, EnrollmentRecord]] = []
        for channel in channels:
            record = enrolled.get(channel.channel_point)
            candidate = safe_candidates.get(channel.channel_point)
            if record is None or candidate is None:
                continue
            if (
                channel.peer_node_id != record.peer_node_id
                or not channel.private
                or not channel.active
                or not _valid_cltv_delta(channel.local_cltv_delta)
                or not _valid_cltv_delta(channel.remote_cltv_delta)
                or candidate.channel_id != channel.channel_id
                or candidate.peer_node_pubkey != record.peer_node_id
                or not candidate.private
                or not candidate.active
            ):
                continue
            eligible.append((channel, record))
        return tuple(sorted(eligible, key=lambda item: item[0].channel_point))

    def _spendable(self, balance_sat: int, reserve_sat: int) -> int:
        return max(0, balance_sat - reserve_sat - self.config.liquidity_margin_sat)

    def _within_liquidity_limit(self, balance_sat: int, reserve_sat: int, amount_sat: int) -> bool:
        spendable = self._spendable(balance_sat, reserve_sat)
        return amount_sat <= (spendable * self.config.max_spendable_percent) // 100

    def _can_send_locally(self, channel: MaintenanceChannel, amount_sat: int) -> bool:
        return (
            channel.local_base_fee_msat == 0
            and channel.local_fee_ppm == 0
            and self._within_liquidity_limit(
                channel.local_balance_sat, channel.local_reserve_sat, amount_sat
            )
        )

    def _can_receive_from_peer(self, channel: MaintenanceChannel, amount_sat: int) -> bool:
        return (
            channel.remote_base_fee_msat == 0
            and channel.remote_fee_ppm == 0
            and channel.peer_alias is not None
            and self._within_liquidity_limit(
                channel.remote_balance_sat, channel.remote_reserve_sat, amount_sat
            )
        )

    async def _attempt_circular_payment(self) -> None:
        amount_sat = self._random.randint(self.config.amount_min_sat, self.config.amount_max_sat)
        channels = await self._eligible_channels()
        pair = self._choose_triangle_endpoints(channels, amount_sat)
        if pair is None:
            self._set_backoff()
            return
        outgoing, outgoing_record, returning, _returning_record = pair
        response = await self._request_route(
            outgoing.peer_node_id, returning.peer_node_id, amount_sat
        )
        if (
            response is None
            or response.hop is None
            or response.hop.node_id != outgoing.peer_node_id
            or response.hop.base_fee_msat != 0
            or response.hop.fee_ppm != 0
        ):
            self._set_backoff()
            return
        assert returning.peer_alias is not None
        return_hop = PeerRouteHop(
            node_id=returning.peer_node_id,
            channel_id=returning.peer_alias,
            base_fee_msat=returning.remote_base_fee_msat,
            fee_ppm=returning.remote_fee_ppm,
            cltv_delta=returning.remote_cltv_delta,
        )
        try:
            invoice = await self.backend.create_maintenance_invoice(
                amount_sat,
                [response.hop.as_lnd_hop(), return_hop],
                self.config.payment_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._set_backoff()
            return
        now = self._now()
        if not now < invoice.expires_at <= now + self.config.payment_timeout_seconds:
            self._set_backoff()
            return
        attempt = PaymentAttempt(
            phase="created_invoice",
            payment_hash=invoice.payment_hash,
            payment_request=invoice.payment_request,
            amount_sat=amount_sat,
            outgoing_channel_id=outgoing.channel_id,
            outgoing_channel_point=outgoing.channel_point,
            return_channel_point=returning.channel_point,
            middle_peer_node_id=outgoing.peer_node_id,
            return_peer_node_id=returning.peer_node_id,
            expires_at=invoice.expires_at,
            invoice_preimage=invoice.preimage,
            return_channel_id=returning.channel_id if invoice.preimage is not None else None,
        )
        if not self._record_payment(attempt):
            return
        dispatch = attempt.model_copy(update={"phase": "dispatch_intent"})
        if not self._replace_payment(attempt, dispatch):
            return
        try:
            outcome = await self._dispatch_payment(dispatch)
        except asyncio.CancelledError:
            self._mark_payment_unresolved(dispatch.payment_hash)
            raise
        except Exception:
            self._mark_payment_unresolved(dispatch.payment_hash)
            return
        await self._apply_payment_outcome(dispatch.payment_hash, outcome)

    async def _reconcile_invoice(self, attempt: PaymentAttempt) -> None:
        if attempt.invoice_preimage is not None:
            assert attempt.return_channel_id is not None
            await self.backend.reconcile_maintenance_invoice(
                attempt.payment_hash,
                attempt.invoice_preimage,
                attempt.return_channel_id,
                attempt.amount_sat,
            )

    async def _monitor_invoice(self, attempt: PaymentAttempt) -> None:
        while True:
            await self._reconcile_invoice(attempt)
            await asyncio.sleep(0.25)

    async def _dispatch_payment(self, attempt: PaymentAttempt) -> MaintenancePayment:
        monitor = (
            asyncio.create_task(self._monitor_invoice(attempt))
            if attempt.invoice_preimage is not None
            else None
        )
        try:
            return await self.backend.send_maintenance_payment(
                attempt.payment_request,
                attempt.outgoing_channel_id,
                self.config.payment_timeout_seconds,
            )
        finally:
            if monitor is not None:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor

    def _choose_triangle_endpoints(
        self,
        channels: tuple[tuple[MaintenanceChannel, EnrollmentRecord], ...],
        amount_sat: int,
    ) -> tuple[MaintenanceChannel, EnrollmentRecord, MaintenanceChannel, EnrollmentRecord] | None:
        pairs = [
            (outgoing, outgoing_record, returning, returning_record)
            for outgoing, outgoing_record in channels
            for returning, returning_record in channels
            if outgoing.peer_node_id != returning.peer_node_id
            and outgoing.channel_point != returning.channel_point
            and self._can_send_locally(outgoing, amount_sat)
            and self._can_receive_from_peer(returning, amount_sat)
        ]
        if not pairs:
            return None
        distinct_origins = [pair for pair in pairs if pair[1].origin_id != pair[3].origin_id]
        choices = distinct_origins or pairs
        return choices[self._random.randint(0, len(choices) - 1)]

    def _allow_request(self, history: dict[str, deque[int]], peer_node_id: str) -> bool:
        now = self._now()
        peer_history = history.setdefault(peer_node_id, deque())
        while peer_history and peer_history[0] <= now - 60:
            peer_history.popleft()
        if len(peer_history) >= MAX_PEER_REQUESTS_PER_MINUTE:
            return False
        peer_history.append(now)
        return True

    def _peer_query_slot_available(self, peer_node_id: str) -> bool:
        return (
            len(self._pending_requests) + len(self._pending_spend_requests)
            < MAX_PENDING_PEER_QUERIES
            and not any(
                pending.peer_node_id == peer_node_id for pending in self._pending_requests.values()
            )
            and not any(
                pending.peer_node_id == peer_node_id
                for pending in self._pending_spend_requests.values()
            )
        )

    async def _request_route(
        self, peer_node_id: str, destination_node_id: str, amount_sat: int
    ) -> RouteResponse | None:
        if not self._peer_query_slot_available(peer_node_id) or not self._allow_request(
            self._outgoing_request_times, peer_node_id
        ):
            return None
        expiry = self._now() + self.config.peer_query_timeout_seconds
        request = RouteRequest(
            request_id=self._random.token_hex(32),
            destination_node_id=destination_node_id,
            amount_sat=amount_sat,
            expiry=expiry,
        )
        future: asyncio.Future[RouteResponse] = asyncio.get_running_loop().create_future()
        self._pending_requests[request.request_id] = _PendingRouteRequest(
            peer_node_id=peer_node_id,
            destination_node_id=destination_node_id,
            expiry=expiry,
            future=future,
        )
        try:
            await self.backend.send_peer_message(
                peer_node_id, MAINTENANCE_MESSAGE_TYPE, _wire_json(request)
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), self.config.peer_query_timeout_seconds
                )
            except TimeoutError:
                return None
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        finally:
            pending = self._pending_requests.pop(request.request_id, None)
            if pending is not None and not pending.future.done():
                pending.future.cancel()

    async def request_spend_pause(
        self, peer_node_id: str, channel_point: str, duration_seconds: int = MAX_SPEND_PAUSE_SECONDS
    ) -> SpendPauseRequest | None:
        """Ask one authenticated peer to avoid a channel for a short advisory lease."""

        EnrollmentRecord.validate_node_id(peer_node_id)
        EnrollmentRecord.validate_channel_point(channel_point)
        if (
            type(duration_seconds) is not int
            or not 1 <= duration_seconds <= MAX_SPEND_PAUSE_SECONDS
        ):
            raise ValueError("spend pause duration must be between 1 and 30 seconds")
        if self.chain_hash is None or not await self._has_current_spend_pause_channel(
            peer_node_id, channel_point
        ):
            return None

        capability = SpendPauseCapabilityRequest(
            request_id=self._random.token_hex(32),
            chain_hash=self.chain_hash,
            expiry=self._pause_wall_now() + self.config.peer_query_timeout_seconds,
        )
        capability_reply = await self._request_spend_reply(peer_node_id, capability)
        if not isinstance(capability_reply, SpendPauseCapabilityReply):
            return None

        # The shared channel can change while a peer is answering the capability query.
        if not await self._has_current_spend_pause_channel(peer_node_id, channel_point):
            return None
        request = SpendPauseRequest(
            chain_hash=self.chain_hash,
            channel_point=channel_point,
            request_id=self._random.token_hex(32),
            expiry=self._pause_wall_now() + duration_seconds,
        )
        reply = await self._request_spend_reply(peer_node_id, request)
        if isinstance(reply, SpendPauseReply) and reply.status == "paused":
            return request
        return None

    async def _announce_local_spend_guard(self, state: _MaintenanceState) -> None:
        """Best-effort advisory notice, at most one new guard per scheduling pass.

        Freezing never waits for this notice or treats a reply as authorization.
        Failed queries are not retried for the same guard; remote leases expire
        independently if either process exits before a release is delivered.
        """
        if self.chain_hash is None:
            return
        self._announced_spend_guards = {
            point: owner
            for point, owner in self._announced_spend_guards.items()
            if state.spend_guards.get(point) == owner
        }
        for point, (owner, peer, request) in tuple(self._outbound_guard_pauses.items()):
            if state.spend_guards.get(point) != owner:
                del self._outbound_guard_pauses[point]
                await self.release_spend_pause(peer, request)
        for point, owner in sorted(state.spend_guards.items()):
            record = state.enrollments.get(point)
            if record is None or self._announced_spend_guards.get(point) == owner:
                continue
            self._announced_spend_guards[point] = owner
            accepted_pause = await self.request_spend_pause(record.peer_node_id, point)
            if accepted_pause is not None:
                self._outbound_guard_pauses[point] = (owner, record.peer_node_id, accepted_pause)
            return

    async def release_spend_pause(self, peer_node_id: str, request: SpendPauseRequest) -> bool:
        """Release one previously accepted remote advisory pause without touching local guards."""

        EnrollmentRecord.validate_node_id(peer_node_id)
        if self.chain_hash is None or request.chain_hash != self.chain_hash:
            return False
        try:
            await self.backend.send_peer_message(
                peer_node_id,
                MAINTENANCE_MESSAGE_TYPE,
                _wire_json(
                    SpendPauseRelease(
                        chain_hash=request.chain_hash,
                        channel_point=request.channel_point,
                        request_id=request.request_id,
                        expiry=request.expiry,
                    )
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        return True

    async def _request_spend_reply(
        self, peer_node_id: str, request: SpendPauseCapabilityRequest | SpendPauseRequest
    ) -> SpendPauseCapabilityReply | SpendPauseReply | None:
        if not self._peer_query_slot_available(peer_node_id) or not self._allow_request(
            self._outgoing_request_times, peer_node_id
        ):
            return None
        future: asyncio.Future[SpendPauseCapabilityReply | SpendPauseReply] = (
            asyncio.get_running_loop().create_future()
        )
        pending = _PendingSpendRequest(
            peer_node_id=peer_node_id,
            request=request,
            monotonic_deadline=self._monotonic_now() + self.config.peer_query_timeout_seconds,
            future=future,
        )
        self._pending_spend_requests[request.request_id] = pending
        try:
            await self.backend.send_peer_message(
                peer_node_id, MAINTENANCE_MESSAGE_TYPE, _wire_json(request)
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future), self.config.peer_query_timeout_seconds
                )
            except TimeoutError:
                return None
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        finally:
            orphan = self._pending_spend_requests.pop(request.request_id, None)
            if orphan is not None and not orphan.future.done():
                orphan.future.cancel()

    async def _has_current_spend_pause_channel(self, peer_node_id: str, channel_point: str) -> bool:
        state = self._read_state()
        if state is None:
            return False
        record = state.enrollments.get(channel_point)
        if (
            record is None
            or record.node_id != self.node_id
            or record.peer_node_id != peer_node_id
            or record.status != "enrolled"
            or channel_point not in state.admission_channels
        ):
            return False
        try:
            channels = await self.backend.list_maintenance_channels()
            candidates = await self.backend.list_channel_input_candidates()
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        channel = next((item for item in channels if item.channel_point == channel_point), None)
        candidate = next(
            (
                item
                for item in candidates
                if f"{item.funding_outpoint.txid}:{item.funding_outpoint.vout}" == channel_point
            ),
            None,
        )
        return self._matches_new_enrollment(record, channel, candidate)

    async def _handle_spend_pause_capability_request(
        self, peer_node_id: str, request: SpendPauseCapabilityRequest
    ) -> None:
        if self.chain_hash is None:
            return
        wall_now = self._pause_wall_now()
        if (
            request.chain_hash != self.chain_hash
            or request.expiry <= wall_now
            or request.expiry > wall_now + MAX_SPEND_PAUSE_SECONDS
            or not self._allow_request(self._incoming_request_times, peer_node_id)
        ):
            return
        try:
            await self.backend.send_peer_message(
                peer_node_id,
                MAINTENANCE_MESSAGE_TYPE,
                _wire_json(
                    SpendPauseCapabilityReply(
                        request_id=request.request_id,
                        chain_hash=request.chain_hash,
                        expiry=request.expiry,
                    )
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _handle_spend_pause_capability_reply(
        self, peer_node_id: str, reply: SpendPauseCapabilityReply
    ) -> None:
        pending = self._pending_spend_requests.get(reply.request_id)
        if (
            pending is None
            or pending.future.done()
            or not isinstance(pending.request, SpendPauseCapabilityRequest)
            or pending.peer_node_id != peer_node_id
            or pending.monotonic_deadline <= self._monotonic_now()
            or reply.request_id != pending.request.request_id
            or reply.chain_hash != pending.request.chain_hash
            or reply.expiry != pending.request.expiry
            or reply.expiry <= self._pause_wall_now()
        ):
            return
        pending.future.set_result(reply)

    async def _handle_spend_pause_request(
        self, peer_node_id: str, request: SpendPauseRequest
    ) -> None:
        if self.chain_hash is None:
            return
        wall_now = self._pause_wall_now()
        if (
            request.chain_hash != self.chain_hash
            or request.expiry <= wall_now
            or request.expiry > wall_now + MAX_SPEND_PAUSE_SECONDS
        ):
            return

        result = self._record_remote_spend_pause(peer_node_id, request, channel_confirmed=None)
        if result is None:
            result = self._record_remote_spend_pause(
                peer_node_id,
                request,
                channel_confirmed=await self._has_current_spend_pause_channel(
                    peer_node_id, request.channel_point
                ),
            )
        if result is None:
            return
        try:
            await self.backend.send_peer_message(
                peer_node_id,
                MAINTENANCE_MESSAGE_TYPE,
                _wire_json(
                    SpendPauseReply(
                        status=result,
                        chain_hash=request.chain_hash,
                        channel_point=request.channel_point,
                        request_id=request.request_id,
                        expiry=request.expiry,
                    )
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    def _record_remote_spend_pause(
        self,
        peer_node_id: str,
        request: SpendPauseRequest,
        *,
        channel_confirmed: bool | None,
    ) -> Literal["paused", "busy"] | None:
        result: Literal["paused", "busy"] | None = None
        deadline: float | None = None

        def mutate(state: _MaintenanceState) -> _MaintenanceState:
            nonlocal deadline, result
            wall_now = max(self._pause_wall_now(), state.spend_pause_wall_high_water)
            pauses = self._prune_remote_spend_pauses(state, wall_now)
            existing = pauses.get(request.request_id)
            if existing is not None:
                exact = (
                    existing.peer_node_id == peer_node_id
                    and existing.chain_hash == request.chain_hash
                    and existing.channel_point == request.channel_point
                    and existing.expiry == request.expiry
                )
                if exact:
                    if self._remote_pause_is_active(state, existing):
                        result = "paused"
                    else:
                        if existing.status == "paused":
                            pauses[request.request_id] = existing.model_copy(
                                update={"status": "expired"}
                            )
                        result = "busy"
                else:
                    result = "busy"
                return state.model_copy(
                    update={
                        "remote_spend_pauses": pauses,
                        "spend_pause_wall_high_water": wall_now,
                    }
                )
            if self._monotonic_now() < state.spend_pause_quarantine_until:
                result = "busy"
            elif channel_confirmed is None:
                return state.model_copy(
                    update={
                        "remote_spend_pauses": pauses,
                        "spend_pause_wall_high_water": wall_now,
                    }
                )
            else:
                enrollment = state.enrollments.get(request.channel_point)
                payment = state.payment
                grant_seconds = request.expiry - wall_now
                recent_grants = sum(
                    grant
                    for request_id, (
                        budget_deadline,
                        grant,
                    ) in self._remote_pause_budget_deadlines.items()
                    if budget_deadline > self._monotonic_now()
                    and (pause := pauses.get(request_id)) is not None
                    and pause.channel_point == request.channel_point
                )
                if (
                    not channel_confirmed
                    or enrollment is None
                    or enrollment.node_id != self.node_id
                    or enrollment.peer_node_id != peer_node_id
                    or enrollment.status != "enrolled"
                    or request.channel_point not in state.admission_channels
                    or grant_seconds < 1
                    or grant_seconds > MAX_SPEND_PAUSE_SECONDS
                    or len(pauses) >= MAX_SPEND_PAUSE_REPLAY_RECORDS
                    or recent_grants + grant_seconds > MAX_SPEND_PAUSE_SECONDS
                    or (
                        payment is not None
                        and payment.phase != "created_invoice"
                        and request.channel_point
                        in (payment.outgoing_channel_point, payment.return_channel_point)
                    )
                ):
                    result = "busy"
                else:
                    pauses[request.request_id] = RemoteSpendPauseRecord(
                        request_id=request.request_id,
                        peer_node_id=peer_node_id,
                        chain_hash=request.chain_hash,
                        channel_point=request.channel_point,
                        expiry=request.expiry,
                        accepted_at=wall_now,
                        status="paused",
                    )
                    deadline = self._monotonic_now() + grant_seconds
                    result = "paused"
            return state.model_copy(
                update={
                    "remote_spend_pauses": pauses,
                    "spend_pause_wall_high_water": wall_now,
                }
            )

        updated = self._mutate_state(mutate)
        if updated is None or result is None:
            return None
        self._spend_pause_wall_high_water = updated.spend_pause_wall_high_water
        self._active_remote_pause_deadlines = {
            request_id: active_deadline
            for request_id, active_deadline in self._active_remote_pause_deadlines.items()
            if request_id in updated.remote_spend_pauses
        }
        self._remote_pause_budget_deadlines = {
            request_id: budget
            for request_id, budget in self._remote_pause_budget_deadlines.items()
            if request_id in updated.remote_spend_pauses
        }
        if deadline is not None and result == "paused":
            self._active_remote_pause_deadlines[request.request_id] = deadline
            self._remote_pause_budget_deadlines[request.request_id] = (
                self._monotonic_now() + SPEND_PAUSE_QUARANTINE_SECONDS,
                request.expiry - updated.spend_pause_wall_high_water,
            )
        return result

    def _handle_spend_pause_reply(self, peer_node_id: str, reply: SpendPauseReply) -> None:
        pending = self._pending_spend_requests.get(reply.request_id)
        if (
            pending is None
            or pending.future.done()
            or not isinstance(pending.request, SpendPauseRequest)
            or pending.peer_node_id != peer_node_id
            or pending.monotonic_deadline <= self._monotonic_now()
            or reply.chain_hash != pending.request.chain_hash
            or reply.channel_point != pending.request.channel_point
            or reply.request_id != pending.request.request_id
            or reply.expiry != pending.request.expiry
            or reply.expiry <= self._pause_wall_now()
        ):
            return
        pending.future.set_result(reply)

    def _handle_spend_pause_release(self, peer_node_id: str, release: SpendPauseRelease) -> None:
        if self.chain_hash is None or release.chain_hash != self.chain_hash:
            return

        released = False

        def mutate(state: _MaintenanceState) -> _MaintenanceState:
            nonlocal released
            wall_now = max(self._pause_wall_now(), state.spend_pause_wall_high_water)
            pauses = self._prune_remote_spend_pauses(state, wall_now)
            existing = pauses.get(release.request_id)
            if (
                existing is not None
                and existing.peer_node_id == peer_node_id
                and existing.chain_hash == release.chain_hash
                and existing.channel_point == release.channel_point
                and existing.expiry == release.expiry
                and existing.status == "paused"
            ):
                pauses[release.request_id] = existing.model_copy(update={"status": "released"})
                released = True
            return state.model_copy(
                update={
                    "remote_spend_pauses": pauses,
                    "spend_pause_wall_high_water": wall_now,
                }
            )

        updated = self._mutate_state(mutate)
        if updated is not None:
            self._spend_pause_wall_high_water = updated.spend_pause_wall_high_water
        if released:
            self._active_remote_pause_deadlines.pop(release.request_id, None)

    async def _handle_route_request(self, peer_node_id: str, request: RouteRequest) -> None:
        now = self._now()
        if (
            request.expiry <= now
            or request.expiry > now + self.config.payment_timeout_seconds
            or peer_node_id in self._responder_busy
            or not self._allow_request(self._incoming_request_times, peer_node_id)
        ):
            return
        self._responder_busy.add(peer_node_id)
        try:
            channels = await self._eligible_channels()
            if not any(channel.peer_node_id == peer_node_id for channel, _record in channels):
                return
            candidates = [
                channel
                for channel, _record in channels
                if channel.peer_node_id == request.destination_node_id
                and channel.local_alias is not None
                and self._can_send_locally(channel, request.amount_sat)
            ]
            hop: _RouteHop | None = None
            if candidates:
                channel = candidates[self._random.randint(0, len(candidates) - 1)]
                assert channel.local_alias is not None
                hop = _RouteHop(
                    node_id=self.node_id,
                    channel_id=channel.local_alias,
                    base_fee_msat=channel.local_base_fee_msat,
                    fee_ppm=channel.local_fee_ppm,
                    cltv_delta=channel.local_cltv_delta,
                )
            response = RouteResponse(request_id=request.request_id, hop=hop, expiry=request.expiry)
            await self.backend.send_peer_message(
                peer_node_id, MAINTENANCE_MESSAGE_TYPE, _wire_json(response)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        finally:
            self._responder_busy.discard(peer_node_id)

    def _handle_route_response(self, peer_node_id: str, response: RouteResponse) -> None:
        pending = self._pending_requests.get(response.request_id)
        if (
            pending is None
            or pending.future.done()
            or pending.peer_node_id != peer_node_id
            or response.expiry <= self._now()
            or response.expiry > pending.expiry
        ):
            return
        pending.future.set_result(response)

    def _record_payment(self, attempt: PaymentAttempt) -> bool:
        stored = False

        def mutate(state: _MaintenanceState) -> _MaintenanceState:
            nonlocal stored
            if state.payment is not None or not self._payment_admitted(state, attempt):
                return state
            stored = True
            return state.model_copy(update={"payment": attempt})

        updated = self._mutate_state(mutate)
        return stored and updated is not None and updated.payment == attempt

    def _replace_payment(self, expected: PaymentAttempt, replacement: PaymentAttempt) -> bool:
        replaced = False

        def mutate(state: _MaintenanceState) -> _MaintenanceState:
            nonlocal replaced
            if state.payment != expected:
                return state
            if expected.phase == "created_invoice" and not self._payment_admitted(
                state, replacement
            ):
                return state
            replaced = True
            return state.model_copy(update={"payment": replacement})

        updated = self._mutate_state(mutate)
        return replaced and updated is not None and updated.payment == replacement

    def _payment_admitted(self, state: _MaintenanceState, attempt: PaymentAttempt) -> bool:
        remote_paused = self._active_remote_pause_points(state)
        for point in (attempt.outgoing_channel_point, attempt.return_channel_point):
            record = state.enrollments.get(point)
            if (
                record is None
                or record.node_id != self.node_id
                or record.status != "enrolled"
                or point not in state.admission_channels
                or point in state.spend_guards
                or point in remote_paused
            ):
                return False
        return True

    def _mark_payment_unresolved(self, payment_hash: str) -> None:
        state = self._read_state()
        if state is None or state.payment is None or state.payment.payment_hash != payment_hash:
            return
        self._replace_payment(
            state.payment, state.payment.model_copy(update={"phase": "unresolved"})
        )

    async def _reconcile_payment(self, attempt: PaymentAttempt) -> None:
        try:
            await self._reconcile_invoice(attempt)
            outcome = await self.backend.track_maintenance_payment(
                attempt.payment_hash, timeout_seconds=self.config.peer_query_timeout_seconds
            )
        except asyncio.CancelledError:
            self._mark_payment_unresolved(attempt.payment_hash)
            raise
        except Exception:
            self._mark_payment_unresolved(attempt.payment_hash)
            return
        await self._apply_payment_outcome(attempt.payment_hash, outcome)

    async def _apply_payment_outcome(self, payment_hash: str, outcome: MaintenancePayment) -> None:
        if outcome.status in {"unknown", "in_flight"}:
            self._mark_payment_unresolved(payment_hash)
            return
        if outcome.status == "succeeded" and outcome.fee_msat == 0:
            self._clear_payment(payment_hash, self._now() + self._random_interval())
            return
        # A terminal failure, including a contradictory nonzero terminal fee, is not retried now.
        self._clear_payment(payment_hash, self._now() + self.config.interval_min_seconds)

    def _clear_payment(self, payment_hash: str, next_due: int) -> None:
        def mutate(state: _MaintenanceState) -> _MaintenanceState:
            if state.payment is None or state.payment.payment_hash != payment_hash:
                return state
            return state.model_copy(update={"payment": None, "next_due": next_due})

        self._mutate_state(mutate)

    async def _listen_peer_messages(self) -> None:
        while not self._stop_event.is_set():
            try:
                async for peer_node_id, payload in self.backend.subscribe_peer_messages(
                    MAINTENANCE_MESSAGE_TYPE
                ):
                    await self.handle_peer_message(peer_node_id, payload)
                    if self._stop_event.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if self._stop_event.is_set():
                return
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=_PEER_RECONNECT_BACKOFF_SECONDS
                )
            except TimeoutError:
                continue

    async def _cancel_listener(self) -> None:
        task = self._listener_task
        self._listener_task = None
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _wait_until_next_tick(self) -> None:
        state = self._read_state()
        if state is None or state.payment is not None or state.next_due is None:
            timeout = self.config.peer_query_timeout_seconds
        else:
            timeout = max(
                1, min(self.config.peer_query_timeout_seconds, state.next_due - self._now())
            )
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=timeout)
        except TimeoutError:
            return


def _wire_json(
    message: (
        RouteRequest
        | RouteResponse
        | SpendPauseCapabilityRequest
        | SpendPauseCapabilityReply
        | SpendPauseRequest
        | SpendPauseReply
        | SpendPauseRelease
    ),
) -> bytes:
    payload = json.dumps(
        message.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    if len(payload) > MAX_PEER_MESSAGE_BYTES:  # Defensive check before the backend's own bound.
        raise ValueError("maintenance peer message exceeds the protocol size bound")
    return payload
