"""Crash-durable private storage for taker channel-input negotiations."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from bitcointx.core.key import CKey
from jmcore.channel_input import MAX_ATTEMPT
from jmcore.secure_files import atomic_write_private, ensure_private_directory, read_private_file

from taker.channel_input_negotiation import (
    TakerChannelInputNegotiation,
    TakerChannelInputSnapshot,
    TakerChannelInputStage,
)

MAX_ACTIVE_SESSIONS = 16
MAX_TOTAL_RECORDS = 10_000
MAX_RECORD_BYTES = 4 * 1024 * 1024
_RECORD_NAME = re.compile(r"^([0-9a-f]{64})-([0-9]+)-([0-9a-f]{64})\.json$")
_IDENTITY_NAME = "coordinator-nick.key"


def _is_parent_store_directory(path: Path) -> bool:
    """Parent records have their own validator and share only this private root."""
    if path.name != "parents":
        return False
    try:
        info = path.lstat()
        return (
            stat.S_ISDIR(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o700
            and info.st_uid == os.getuid()
        )
    except OSError:
        return False


class TakerChannelInputStoreError(Exception):
    """A coordinator snapshot could not be safely persisted or restored."""


class TakerChannelInputStoreCollisionError(TakerChannelInputStoreError):
    """A session attempt is already bound to another coordinator identity."""


class TakerChannelInputStoreLimitError(TakerChannelInputStoreError):
    """A new snapshot would exceed configured store limits."""


@dataclass(frozen=True)
class TakerChannelInputLoadReport:
    records: tuple[TakerChannelInputSnapshot, ...]
    corruptions: tuple[str, ...]

    @property
    def safe(self) -> bool:
        return not self.corruptions


class TakerChannelInputStore:
    """Private, fail-closed storage for signed taker negotiation snapshots."""

    def __init__(self, directory: Path, *, max_active_sessions: int) -> None:
        if (
            not isinstance(max_active_sessions, int)
            or isinstance(max_active_sessions, bool)
            or not 1 <= max_active_sessions <= MAX_ACTIVE_SESSIONS
        ):
            raise ValueError(f"max_active_sessions must be between 1 and {MAX_ACTIVE_SESSIONS}")
        self.directory = directory
        self.max_active_sessions = max_active_sessions
        self._lock = RLock()
        self._ensure_directory()

    def save(self, snapshot: TakerChannelInputSnapshot) -> None:
        try:
            validated, coordinator_key = _validate_snapshot(snapshot)
            filename = _record_filename(
                validated.invite.session_id, validated.invite.attempt, coordinator_key
            )
            payload = _serialize_snapshot(validated)
        except Exception as exc:
            if isinstance(exc, TakerChannelInputStoreError):
                raise
            raise TakerChannelInputStoreError("channel-input snapshot is invalid") from exc
        with self._lock:
            try:
                self._ensure_directory()
                report = self._load_all_unlocked()
                if report.corruptions:
                    raise TakerChannelInputStoreError(
                        "cannot update store while corrupt records exist"
                    )
                self._enforce_identity_and_limit(
                    report.records,
                    session_id=validated.invite.session_id,
                    attempt=validated.invite.attempt,
                    coordinator_key=coordinator_key,
                    stage=validated.stage,
                )
                atomic_write_private(self.directory / filename, payload)
                _fsync_private_directory(self.directory)
                self._ensure_directory()
            except TakerChannelInputStoreError:
                raise
            except Exception as exc:
                raise TakerChannelInputStoreError(
                    "could not durably save channel-input snapshot"
                ) from exc

    def load_or_create_nick_secret(self) -> bytes:
        """Return the durable outer nick key required for restart recovery."""
        with self._lock:
            try:
                self._ensure_directory()
                path = self.directory / _IDENTITY_NAME
                if path.exists() or path.is_symlink():
                    return _read_nick_secret(path)
                secret = secrets.token_bytes(32)
                # Validate the scalar before exposing or persisting it.
                CKey.from_secret_bytes(secret)
                atomic_write_private(path, secret.hex().encode("ascii"))
                _fsync_private_directory(self.directory)
                return secret
            except TakerChannelInputStoreError:
                raise
            except Exception as exc:
                raise TakerChannelInputStoreError(
                    "could not load or create channel-input coordinator identity"
                ) from exc

    def load(self, session_id: str, attempt: int) -> TakerChannelInputSnapshot | None:
        _validate_lookup(session_id, attempt)
        with self._lock:
            self._ensure_directory()
            report = self._load_all_unlocked()
        if report.corruptions:
            raise TakerChannelInputStoreError("channel-input store contains corrupt records")
        matches = tuple(
            record
            for record in report.records
            if (record.invite.session_id, record.invite.attempt) == (session_id, attempt)
        )
        if len(matches) > 1:
            raise TakerChannelInputStoreCollisionError(
                "multiple snapshots exist for one session attempt"
            )
        return matches[0] if matches else None

    def load_all(self) -> TakerChannelInputLoadReport:
        with self._lock:
            self._ensure_directory()
            return self._load_all_unlocked()

    def delete(self, snapshot: TakerChannelInputSnapshot) -> None:
        try:
            validated, coordinator_key = _validate_snapshot(snapshot)
            filename = _record_filename(
                validated.invite.session_id, validated.invite.attempt, coordinator_key
            )
        except Exception as exc:
            if isinstance(exc, TakerChannelInputStoreError):
                raise
            raise TakerChannelInputStoreError("channel-input snapshot is invalid") from exc
        with self._lock:
            self._ensure_directory()
            report = self._load_all_unlocked()
            if report.corruptions:
                raise TakerChannelInputStoreError("cannot delete while corrupt records exist")
            matches = [
                record
                for record in report.records
                if _session_attempt(record) == _session_attempt(validated)
            ]
            if len(matches) != 1 or matches[0] != validated:
                raise TakerChannelInputStoreError("snapshot differs from durable record")
            path = self.directory / filename
            if self._load_path(path) != validated:
                raise TakerChannelInputStoreError("snapshot filename differs from durable record")
            try:
                path.unlink()
                _fsync_private_directory(self.directory)
                self._ensure_directory()
            except Exception as exc:
                raise TakerChannelInputStoreError(
                    "could not durably delete channel-input snapshot"
                ) from exc

    def _ensure_directory(self) -> None:
        try:
            ensure_private_directory(self.directory)
            if not stat.S_ISDIR(self.directory.stat().st_mode):
                raise OSError("private store path is not a directory")
        except Exception as exc:
            raise TakerChannelInputStoreError("channel-input store directory is unsafe") from exc

    def _load_all_unlocked(self) -> TakerChannelInputLoadReport:
        records: list[TakerChannelInputSnapshot] = []
        corruptions: list[str] = []
        try:
            paths = sorted(self.directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise TakerChannelInputStoreError(
                "could not enumerate channel-input snapshots"
            ) from exc
        identity_path = self.directory / _IDENTITY_NAME
        if identity_path.exists() or identity_path.is_symlink():
            try:
                _read_nick_secret(identity_path)
            except TakerChannelInputStoreError:
                corruptions.append(f"{_IDENTITY_NAME}: invalid coordinator identity")
        record_paths = [
            path
            for path in paths
            if path.name != _IDENTITY_NAME
            and not _is_atomic_temp_path(path)
            and not _is_parent_store_directory(path)
        ]
        if len(record_paths) > MAX_TOTAL_RECORDS:
            return TakerChannelInputLoadReport(
                records=(),
                corruptions=("channel-input store exceeds maximum record count",),
            )
        for path in record_paths:
            try:
                records.append(self._load_path(path))
            except Exception:
                corruptions.append(f"{path.name}: invalid channel-input snapshot")
        records.sort(key=_snapshot_sort_key)
        for previous, current in zip(records, records[1:], strict=False):
            if _session_attempt(previous) == _session_attempt(current):
                corruptions.append(
                    f"{_record_filename_for_snapshot(previous)}: duplicate session attempt"
                )
        return TakerChannelInputLoadReport(tuple(records), tuple(corruptions))

    def _load_path(self, path: Path) -> TakerChannelInputSnapshot:
        try:
            info = path.lstat()
        except OSError as exc:
            raise TakerChannelInputStoreError("could not inspect channel-input snapshot") from exc
        if not stat.S_ISREG(info.st_mode):
            raise TakerChannelInputStoreError("channel-input snapshot is not a regular file")
        if info.st_size > MAX_RECORD_BYTES:
            raise TakerChannelInputStoreError("channel-input snapshot exceeds size limit")
        try:
            payload = read_private_file(path)
            if len(payload) > MAX_RECORD_BYTES:
                raise TakerChannelInputStoreError("channel-input snapshot exceeds size limit")
            snapshot = TakerChannelInputSnapshot.model_validate_json(payload)
            validated, coordinator_key = _validate_snapshot(snapshot)
        except TakerChannelInputStoreError:
            raise
        except Exception as exc:
            raise TakerChannelInputStoreError("channel-input snapshot failed validation") from exc
        if path.name != _record_filename(
            validated.invite.session_id, validated.invite.attempt, coordinator_key
        ):
            raise TakerChannelInputStoreError(
                "channel-input snapshot filename differs from identity"
            )
        return validated

    def _enforce_identity_and_limit(
        self,
        records: tuple[TakerChannelInputSnapshot, ...],
        *,
        session_id: str,
        attempt: int,
        coordinator_key: str,
        stage: TakerChannelInputStage,
    ) -> None:
        exact_exists = False
        for record in records:
            if _session_attempt(record) != (session_id, attempt):
                continue
            if _coordinator_key_for_snapshot(record) != coordinator_key:
                raise TakerChannelInputStoreCollisionError(
                    "session attempt belongs to another coordinator"
                )
            exact_exists = True
        if not exact_exists:
            if len(records) >= MAX_TOTAL_RECORDS:
                raise TakerChannelInputStoreLimitError("maximum channel-input records reached")
            active = sum(record.stage is not TakerChannelInputStage.CANCELED for record in records)
            if stage is not TakerChannelInputStage.CANCELED and active >= self.max_active_sessions:
                raise TakerChannelInputStoreLimitError(
                    "maximum active channel-input sessions reached"
                )


def _validate_snapshot(
    snapshot: TakerChannelInputSnapshot,
) -> tuple[TakerChannelInputSnapshot, str]:
    validated = TakerChannelInputSnapshot.model_validate(snapshot.model_dump(mode="python"))
    negotiation = TakerChannelInputNegotiation.from_snapshot(validated)
    return validated, negotiation.coordinator_key


def _serialize_snapshot(snapshot: TakerChannelInputSnapshot) -> bytes:
    payload = json.dumps(
        snapshot.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    if len(payload) > MAX_RECORD_BYTES:
        raise TakerChannelInputStoreError("channel-input snapshot exceeds size limit")
    return payload


def _record_filename(session_id: str, attempt: int, coordinator_key: str) -> str:
    _validate_lookup(session_id, attempt)
    _validate_coordinator_key(coordinator_key)
    return f"{session_id}-{attempt}-{coordinator_key}.json"


def _record_filename_for_snapshot(snapshot: TakerChannelInputSnapshot) -> str:
    return _record_filename(*_session_attempt(snapshot), _coordinator_key_for_snapshot(snapshot))


def _coordinator_key_for_snapshot(snapshot: TakerChannelInputSnapshot) -> str:
    return _validate_snapshot(snapshot)[1]


def _session_attempt(snapshot: TakerChannelInputSnapshot) -> tuple[str, int]:
    return snapshot.invite.session_id, snapshot.invite.attempt


def _snapshot_sort_key(snapshot: TakerChannelInputSnapshot) -> tuple[str, int, str]:
    return (*_session_attempt(snapshot), _coordinator_key_for_snapshot(snapshot))


def _validate_lookup(session_id: str, attempt: int) -> None:
    if not isinstance(session_id, str) or len(session_id) != 64 or session_id.lower() != session_id:
        raise TakerChannelInputStoreError("session ID must be 32-byte lowercase hexadecimal")
    try:
        bytes.fromhex(session_id)
    except ValueError as exc:
        raise TakerChannelInputStoreError(
            "session ID must be 32-byte lowercase hexadecimal"
        ) from exc
    if not isinstance(attempt, int) or isinstance(attempt, bool) or not 0 <= attempt <= MAX_ATTEMPT:
        raise TakerChannelInputStoreError("attempt is outside channel-input protocol bounds")


def _validate_coordinator_key(value: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or value.lower() != value:
        raise TakerChannelInputStoreError("coordinator key must be 32-byte lowercase hexadecimal")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise TakerChannelInputStoreError(
            "coordinator key must be 32-byte lowercase hexadecimal"
        ) from exc
    return value


def _read_nick_secret(path: Path) -> bytes:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != 64:
            raise TakerChannelInputStoreError("coordinator identity file is unsafe")
        encoded = read_private_file(path)
        if len(encoded) != 64 or encoded.lower() != encoded:
            raise TakerChannelInputStoreError("coordinator identity is invalid")
        secret = bytes.fromhex(encoded.decode("ascii"))
        CKey.from_secret_bytes(secret)
        return secret
    except TakerChannelInputStoreError:
        raise
    except Exception as exc:
        raise TakerChannelInputStoreError("coordinator identity is invalid") from exc


def _is_atomic_temp_artifact(name: str) -> bool:
    if not name.startswith(".") or not name.endswith(".tmp"):
        return False
    record_name, separator, suffix = name[1:-4].partition(".json.")
    return bool(separator and suffix and _RECORD_NAME.fullmatch(f"{record_name}.json"))


def _is_atomic_temp_path(path: Path) -> bool:
    if not _is_atomic_temp_artifact(path.name):
        return False
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _fsync_private_directory(directory: Path) -> None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
