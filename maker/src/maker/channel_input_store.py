"""Crash-durable private storage for maker channel-input negotiations."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from jmcore.channel_input import MAX_ATTEMPT
from jmcore.secure_files import atomic_write_private, ensure_private_directory, read_private_file

from maker.channel_input import (
    MakerChannelInputNegotiation,
    MakerChannelInputSnapshot,
    MakerChannelInputStage,
)

MAX_ACTIVE_SESSIONS = 10_000
MAX_RECORD_BYTES = 4 * 1024 * 1024

_RECORD_NAME = re.compile(r"^([0-9a-f]{64})-([0-9]+)-([0-9a-f]{64})\.json$")


class MakerChannelInputStoreError(Exception):
    """A channel-input snapshot could not be safely persisted or restored."""


class MakerChannelInputStoreCollisionError(MakerChannelInputStoreError):
    """A session attempt is already bound to another participant identity."""


class MakerChannelInputStoreLimitError(MakerChannelInputStoreError):
    """A new snapshot would exceed the configured active-session limit."""


@dataclass(frozen=True)
class MakerChannelInputLoadReport:
    """The valid records and unsafe entries discovered in the private store."""

    records: tuple[MakerChannelInputSnapshot, ...]
    corruptions: tuple[str, ...]

    @property
    def safe(self) -> bool:
        """Whether every non-temporary directory entry was valid."""

        return not self.corruptions


class MakerChannelInputStore:
    """Private, fail-closed storage for every signed maker negotiation state."""

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

    def save(self, snapshot: MakerChannelInputSnapshot) -> None:
        """Validate and durably replace one complete snapshot."""

        try:
            validated, participant_key = _validate_snapshot(snapshot)
            filename = _record_filename(
                validated.invite.session_id,
                validated.invite.attempt,
                participant_key,
            )
            payload = _serialize_snapshot(validated)
        except Exception as exc:
            if isinstance(exc, MakerChannelInputStoreError):
                raise
            raise MakerChannelInputStoreError("channel-input snapshot is invalid") from None

        with self._lock:
            try:
                self._ensure_directory()
                report = self._load_all_unlocked()
                if report.corruptions:
                    raise MakerChannelInputStoreError(
                        "cannot update channel-input store while corrupt records exist"
                    )
                self._enforce_identity_and_limit(
                    report.records,
                    session_id=validated.invite.session_id,
                    attempt=validated.invite.attempt,
                    participant_key=participant_key,
                    stage=validated.stage,
                )
                atomic_write_private(self.directory / filename, payload)
                _fsync_private_directory(self.directory)
                self._ensure_directory()
            except MakerChannelInputStoreError:
                raise
            except Exception as exc:
                raise MakerChannelInputStoreError(
                    "could not durably save channel-input snapshot"
                ) from exc

    def load(self, session_id: str, attempt: int) -> MakerChannelInputSnapshot | None:
        """Load the sole snapshot for a validated session attempt, if it exists."""

        _validate_lookup(session_id, attempt)
        with self._lock:
            try:
                self._ensure_directory()
                report = self._load_all_unlocked()
            except MakerChannelInputStoreError:
                raise
            except Exception as exc:
                raise MakerChannelInputStoreError("could not load channel-input snapshots") from exc
        if report.corruptions:
            raise MakerChannelInputStoreError("channel-input store contains corrupt records")
        matches = tuple(
            record
            for record in report.records
            if record.invite.session_id == session_id and record.invite.attempt == attempt
        )
        if len(matches) > 1:
            raise MakerChannelInputStoreCollisionError(
                "multiple channel-input snapshots exist for one session attempt"
            )
        return matches[0] if matches else None

    def load_all(self) -> MakerChannelInputLoadReport:
        """Return all valid snapshots and every corruption found during enumeration."""

        with self._lock:
            try:
                self._ensure_directory()
                return self._load_all_unlocked()
            except MakerChannelInputStoreError:
                raise
            except Exception as exc:
                raise MakerChannelInputStoreError(
                    "could not enumerate channel-input snapshots"
                ) from exc

    def delete(self, snapshot: MakerChannelInputSnapshot) -> None:
        """Durably remove exactly one validated private snapshot record."""

        try:
            validated, participant_key = _validate_snapshot(snapshot)
            filename = _record_filename(
                validated.invite.session_id,
                validated.invite.attempt,
                participant_key,
            )
        except Exception as exc:
            if isinstance(exc, MakerChannelInputStoreError):
                raise
            raise MakerChannelInputStoreError("channel-input snapshot is invalid") from None
        with self._lock:
            try:
                self._ensure_directory()
                report = self._load_all_unlocked()
                if report.corruptions:
                    raise MakerChannelInputStoreError(
                        "cannot delete channel-input snapshot while corrupt records exist"
                    )
                matches = [
                    record
                    for record in report.records
                    if _session_attempt(record) == _session_attempt(validated)
                ]
                if len(matches) != 1 or matches[0] != validated:
                    raise MakerChannelInputStoreError(
                        "channel-input snapshot differs from durable record"
                    )
                path = self.directory / filename
                if self._load_path(path) != validated:
                    raise MakerChannelInputStoreError(
                        "channel-input snapshot filename differs from durable record"
                    )
                path.unlink()
                _fsync_private_directory(self.directory)
                self._ensure_directory()
            except MakerChannelInputStoreError:
                raise
            except Exception as exc:
                raise MakerChannelInputStoreError(
                    "could not durably delete channel-input snapshot"
                ) from exc

    def _ensure_directory(self) -> None:
        try:
            ensure_private_directory(self.directory)
            info = self.directory.stat()
            if not stat.S_ISDIR(info.st_mode):
                raise OSError("private store path is not a directory")
        except Exception as exc:
            raise MakerChannelInputStoreError("channel-input store directory is unsafe") from exc

    def _load_all_unlocked(self) -> MakerChannelInputLoadReport:
        records: list[MakerChannelInputSnapshot] = []
        corruptions: list[str] = []
        try:
            paths = sorted(self.directory.iterdir(), key=lambda path: path.name)
        except OSError as exc:
            raise MakerChannelInputStoreError(
                "could not enumerate channel-input snapshots"
            ) from exc

        for path in paths:
            if _is_atomic_temp_artifact(path.name):
                try:
                    if stat.S_ISREG(path.lstat().st_mode):
                        continue
                except OSError:
                    pass
            try:
                records.append(self._load_path(path))
            except Exception:
                corruptions.append(f"{path.name}: invalid channel-input snapshot")

        records.sort(key=_snapshot_sort_key)
        for previous, current in zip(records, records[1:]):
            if _session_attempt(previous) == _session_attempt(current):
                corruptions.append(
                    f"{_record_filename_for_snapshot(previous)}: "
                    "duplicate channel-input session attempt"
                )
        return MakerChannelInputLoadReport(records=tuple(records), corruptions=tuple(corruptions))

    def _load_path(self, path: Path) -> MakerChannelInputSnapshot:
        try:
            info = path.lstat()
        except OSError as exc:
            raise MakerChannelInputStoreError("could not inspect channel-input snapshot") from exc
        if not stat.S_ISREG(info.st_mode):
            raise MakerChannelInputStoreError("channel-input snapshot is not a regular file")
        if info.st_size > MAX_RECORD_BYTES:
            raise MakerChannelInputStoreError("channel-input snapshot exceeds size limit")
        try:
            payload = read_private_file(path)
        except OSError as exc:
            raise MakerChannelInputStoreError("could not read channel-input snapshot") from exc
        if len(payload) > MAX_RECORD_BYTES:
            raise MakerChannelInputStoreError("channel-input snapshot exceeds size limit")
        try:
            snapshot = MakerChannelInputSnapshot.model_validate_json(payload)
            validated, participant_key = _validate_snapshot(snapshot)
            expected = _record_filename(
                validated.invite.session_id,
                validated.invite.attempt,
                participant_key,
            )
        except Exception:
            raise MakerChannelInputStoreError("channel-input snapshot failed validation") from None
        if path.name != expected:
            raise MakerChannelInputStoreError(
                "channel-input snapshot filename differs from identity"
            )
        return validated

    def _enforce_identity_and_limit(
        self,
        records: tuple[MakerChannelInputSnapshot, ...],
        *,
        session_id: str,
        attempt: int,
        participant_key: str,
        stage: MakerChannelInputStage,
    ) -> None:
        exact_exists = False
        for record in records:
            record_participant_key = _participant_key_for_snapshot(record)
            if _session_attempt(record) != (session_id, attempt):
                continue
            if record_participant_key != participant_key:
                raise MakerChannelInputStoreCollisionError(
                    "channel-input session attempt belongs to another participant"
                )
            exact_exists = True
        if not exact_exists:
            if len(records) >= MAX_ACTIVE_SESSIONS:
                raise MakerChannelInputStoreLimitError("maximum channel-input records reached")
            active_records = sum(
                record.stage is not MakerChannelInputStage.CANCELED for record in records
            )
            if (
                stage is not MakerChannelInputStage.CANCELED
                and active_records >= self.max_active_sessions
            ):
                raise MakerChannelInputStoreLimitError(
                    "maximum active channel-input sessions reached"
                )


def _validate_snapshot(
    snapshot: MakerChannelInputSnapshot,
) -> tuple[MakerChannelInputSnapshot, str]:
    validated = MakerChannelInputSnapshot.model_validate(snapshot.model_dump(mode="python"))
    negotiation = MakerChannelInputNegotiation.from_snapshot(validated)
    return validated, _validate_participant_key(negotiation.participant_key)


def _serialize_snapshot(snapshot: MakerChannelInputSnapshot) -> bytes:
    payload = json.dumps(
        snapshot.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    if len(payload) > MAX_RECORD_BYTES:
        raise MakerChannelInputStoreError("channel-input snapshot exceeds size limit")
    return payload


def _record_filename(session_id: str, attempt: int, participant_key: str) -> str:
    _validate_lookup(session_id, attempt)
    return f"{session_id}-{attempt}-{_validate_participant_key(participant_key)}.json"


def _record_filename_for_snapshot(snapshot: MakerChannelInputSnapshot) -> str:
    return _record_filename(
        snapshot.invite.session_id,
        snapshot.invite.attempt,
        _participant_key_for_snapshot(snapshot),
    )


def _participant_key_for_snapshot(snapshot: MakerChannelInputSnapshot) -> str:
    _, participant_key = _validate_snapshot(snapshot)
    return participant_key


def _session_attempt(snapshot: MakerChannelInputSnapshot) -> tuple[str, int]:
    return snapshot.invite.session_id, snapshot.invite.attempt


def _snapshot_sort_key(snapshot: MakerChannelInputSnapshot) -> tuple[str, int, str]:
    return (*_session_attempt(snapshot), _participant_key_for_snapshot(snapshot))


def _validate_lookup(session_id: str, attempt: int) -> None:
    if not isinstance(session_id, str) or len(session_id) != 64 or session_id.lower() != session_id:
        raise MakerChannelInputStoreError("session ID must be 32-byte lowercase hexadecimal")
    try:
        bytes.fromhex(session_id)
    except ValueError as exc:
        raise MakerChannelInputStoreError(
            "session ID must be 32-byte lowercase hexadecimal"
        ) from exc
    if not isinstance(attempt, int) or isinstance(attempt, bool) or not 0 <= attempt <= MAX_ATTEMPT:
        raise MakerChannelInputStoreError("attempt is outside channel-input protocol bounds")


def _validate_participant_key(participant_key: str) -> str:
    if (
        not isinstance(participant_key, str)
        or len(participant_key) != 64
        or participant_key.lower() != participant_key
    ):
        raise MakerChannelInputStoreError("participant key must be 32-byte lowercase hexadecimal")
    try:
        bytes.fromhex(participant_key)
    except ValueError as exc:
        raise MakerChannelInputStoreError(
            "participant key must be 32-byte lowercase hexadecimal"
        ) from exc
    return participant_key


def _is_atomic_temp_artifact(name: str) -> bool:
    if not name.startswith(".") or not name.endswith(".tmp"):
        return False
    record_name, separator, suffix = name[1:-4].partition(".json.")
    return bool(separator and suffix and _RECORD_NAME.fullmatch(f"{record_name}.json"))


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
