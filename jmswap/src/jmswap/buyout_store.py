"""Durable private journal of channel-buyout attempts.

A buyout attempt spans two processes, a CoinJoin round and an on-chain escrow,
so the only thing that can answer "may I sign the parent?" or "is this channel
already committed to an attempt?" after a restart is a record that survived the
restart. This module is that record and nothing else: it stores what a run
already decided, it never decides anything itself. There is no policy here, no
state machine, no Lightning or Bitcoin call, no timer, no expiry, no background
rescan and no recovery action. The runtime owns the set of states and their
meaning; the journal only keeps one bounded string per session and refuses the
few transitions that would make a stored fact unsafe to trust later.

What the journal guarantees
---------------------------

* **Compare-and-swap.** :meth:`BuyoutStore.update` writes only if the stored
  revision still equals the revision of the record the caller read, so a second
  process that read the same session loses instead of silently overwriting.
* **Immutable binding.** Session id, peer key, role and the channel point set
  are fixed at :meth:`BuyoutStore.create`. An update that disagrees with them is
  a caller bug and is rejected rather than applied.
* **Monotonic signing marker.** ``parent_signing_started`` never goes back to
  false, and a session cannot be recorded as ``CANCELED`` while it is true. The
  marker exists so that a crashed run can tell "my signature may be on a parent
  somewhere" from "nothing was signed"; a marker that could be cleared, or a
  cancellation that could contradict it, would prove nothing.
* **Channel reservations.** Every channel point is reserved by the session that
  first used it and stays reserved forever, including after the attempt
  completes. A channel whose attempt reached the chain can still be reorged out,
  so "that session is over" is not evidence that the channel is free. The single
  exception is an affirmative ``CANCELED`` record with the signing marker false:
  that is a written statement that nothing was ever signed for those channels, so
  a later attempt may take them over. A missing or unrecognized record is never
  such evidence.
* **One funding transaction per buyer.** A buyer session may not reserve a
  channel whose funding transaction already funds a different channel reserved
  by another buyer session. A node's two ring edges share one funding
  transaction, so this keeps the node from buying out both edges, which would
  tie them to one owner on chain. Released (canceled, never-signed) reservations
  do not count.
* **Bounded occupancy.** At most :data:`MAX_UNRESOLVED_SESSIONS` sessions may be
  unresolved at once (``CANCELED``, ``COMPLETED`` and ``CONFLICTED`` are the
  resolved states), which keeps a stuck peer from filling the journal.
  Resolved sessions are never pruned: they back session-id replay rejection and
  the ``CANCELED`` channel takeover above. A configured peer that repeatedly
  proposes and cancels therefore grows the journal slowly; the peer allowlist,
  not the store, is the control for that.

Upgrade and file handling
-------------------------

The journal file is created by this module with mode ``0600`` inside a ``0700``
directory it either created or found already private; an existing directory or
file with wider permissions, a symlink, or a foreign owner is refused rather
than quietly repaired. A file this module did not create is used only if it
carries ``user_version = 1`` and exactly the schema of this version. An empty or
unversioned file, an unknown version and an unreadable database are all refused
without a single byte being written: none of them proves the file is free for
reuse, and overwriting one could destroy the record of an attempt whose parent
was already signed. There are no migrations, so a future version that changes
the schema must introduce a new version number and decide explicitly what to do
with the old file.

Writes use ``BEGIN IMMEDIATE`` with ``synchronous = FULL`` and the rollback
journal (no WAL, so no readable sidecar survives a crash outside the private
directory). Creation, reservation and revision bumps therefore serialize between
processes and an interrupted write leaves the previous revision intact.

Errors
------

Every rejection raises a subclass of :class:`BuyoutStoreError` with a fixed
message naming the rule that failed. Session data, peer keys, channel points and
filesystem paths are never quoted, and the underlying exception chain is
suppressed, so any failure here can be logged without logging the private terms
of an attempt. :class:`StoredSession` hides the same values from its ``repr``.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import TracebackType
from typing import Literal

SCHEMA_VERSION = 1
"""``user_version`` of the journal file written and accepted by this module."""

MAX_UNRESOLVED_SESSIONS = 4
"""Sessions in a non-resolved state that may exist at the same time."""

MAX_DATA_BYTES = 262144
"""Maximum size of one session's serialized ``data`` object."""

MAX_STATE_LENGTH = 64
MAX_CHANNEL_POINTS = 16
MAX_DATA_DEPTH = 32
MAX_DATA_INTEGER = 2**63 - 1

STATE_CREATED = "CREATED"
STATE_CANCELED = "CANCELED"
RESOLVED_STATES = frozenset({"CANCELED", "COMPLETED", "CONFLICTED"})
"""States that no longer occupy one of the :data:`MAX_UNRESOLVED_SESSIONS` slots."""

DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
BUSY_TIMEOUT_MS = 5000

#: Session data key naming the session's own payout script (hex scriptPubKey).
PAYOUT_SCRIPT_KEY = "payout_script"

Role = Literal["buyer", "counterparty"]
_ROLES: frozenset[str] = frozenset({"buyer", "counterparty"})

_SESSION_ID_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_PUBKEY_RE = re.compile(r"\A0[23][0-9a-f]{64}\Z")
_STATE_RE = re.compile(rf"\A[A-Za-z_]{{1,{MAX_STATE_LENGTH}}}\Z")
_CHANNEL_POINT_RE = re.compile(r"\A[0-9a-f]{64}:(0|[1-9][0-9]{0,9})\Z")
_MAX_VOUT = 2**32 - 1


class BuyoutStoreError(Exception):
    """Base class for every rejection raised by the journal."""


class JournalError(BuyoutStoreError):
    """The journal file cannot be used, or a stored row cannot be trusted."""


class InvalidRecordError(BuyoutStoreError):
    """The caller supplied a value or a transition the journal cannot record."""


class UnknownSessionError(BuyoutStoreError):
    """No session with the requested id exists in the journal."""


class ConflictError(BuyoutStoreError):
    """Another writer, an existing reservation or the session limit won."""


@dataclass(frozen=True, slots=True)
class StoredSession:
    """One journal row as it was read or written.

    The record is the compare-and-swap token: pass the exact instance returned by
    :meth:`BuyoutStore.get`, :meth:`BuyoutStore.list` or a previous
    :meth:`BuyoutStore.update` back into :meth:`BuyoutStore.update`. ``repr``
    deliberately omits the peer key, the channel points and the data so that a
    record can appear in a log line without linking the attempt to a peer or to
    an on-chain output.
    """

    session_id: str
    peer_pubkey: str = field(repr=False)
    role: Role
    channel_points: tuple[str, ...] = field(repr=False)
    revision: int
    state: str
    data: dict[str, object] = field(repr=False)
    parent_signing_started: bool


_SCHEMA: tuple[tuple[str, str], ...] = (
    (
        "sessions",
        """CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY NOT NULL,
            peer_pubkey TEXT NOT NULL,
            role TEXT NOT NULL,
            revision INTEGER NOT NULL,
            state TEXT NOT NULL,
            data TEXT NOT NULL,
            parent_signing_started INTEGER NOT NULL
        )""",
    ),
    (
        "session_channels",
        """CREATE TABLE session_channels (
            session_id TEXT NOT NULL REFERENCES sessions (session_id),
            position INTEGER NOT NULL,
            channel_point TEXT NOT NULL,
            reserved INTEGER NOT NULL,
            PRIMARY KEY (session_id, position)
        )""",
    ),
    (
        "session_channels_reserved",
        """CREATE UNIQUE INDEX session_channels_reserved
            ON session_channels (channel_point) WHERE reserved = 1""",
    ),
)


class BuyoutStore:
    """Private SQLite journal of buyout sessions at ``path``.

    Opening validates the directory, the file and the schema; it creates the
    file only when it does not exist yet. Use it as a context manager, or call
    :meth:`close`, to release the connection.
    """

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        created = _prepare_file(self._path)
        self._connection = _connect(self._path)
        info = self._path.stat()
        self._identity = (info.st_dev, info.st_ino)
        self._closed = False
        try:
            if created:
                _initialize_schema(self._connection)
                _fsync_directory(self._path.parent)
            else:
                _verify_schema(self._connection)
        except BaseException:
            self._connection.close()
            self._closed = True
            raise

    def __enter__(self) -> BuyoutStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection. Idempotent; further calls raise."""
        if not self._closed:
            self._closed = True
            self._connection.close()

    @contextmanager
    def exclusive_operation(self) -> Iterator[None]:
        """Reserve the journal across RPC awaits without holding a SQL transaction.

        Runtime entry points use this nonblocking advisory lock. A separate open
        file description rejects overlapping operations even in the same process;
        closing it, including on process exit, releases ownership. No marker or
        migration is needed for existing journals.
        """
        if self._closed:
            raise JournalError("the journal is closed")
        try:
            descriptor = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            raise JournalError("the journal could not be opened for an operation") from None
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino) != self._identity:
                raise JournalError("the journal was replaced")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ConflictError("another runtime operation owns the journal") from None
            except OSError:
                raise JournalError("the journal operation could not be locked") from None
            yield
        finally:
            os.close(descriptor)

    def create(
        self,
        session_id: str,
        peer_pubkey: str,
        role: Role,
        channel_points: Sequence[str],
        data: dict[str, object] | None = None,
    ) -> StoredSession:
        """Record a new session in state ``CREATED`` at revision 0.

        Fails if the id already exists, if the journal already holds
        :data:`MAX_UNRESOLVED_SESSIONS` unresolved sessions, or if any channel
        point is reserved by a session that is not an affirmatively canceled,
        never-signed one.
        """
        record = StoredSession(
            session_id=_validated_session_id(session_id),
            peer_pubkey=_validated_pubkey(peer_pubkey),
            role=_validated_role(role),
            channel_points=_validated_channel_points(channel_points),
            revision=0,
            state=STATE_CREATED,
            data=_decoded_data(_encoded_data({} if data is None else data)),
            parent_signing_started=False,
        )
        with self._write() as cursor:
            existing = cursor.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (record.session_id,)
            ).fetchone()
            if existing is not None:
                raise ConflictError("a session with this id already exists")
            unresolved = cursor.execute(
                f"SELECT COUNT(*) FROM sessions WHERE state NOT IN ({_RESOLVED_PLACEHOLDERS})",
                _RESOLVED_ORDERED,
            ).fetchone()[0]
            if unresolved >= MAX_UNRESOLVED_SESSIONS:
                raise ConflictError("the journal already holds the maximum of unresolved sessions")
            if record.role == "buyer":
                _reject_sibling_buyout(cursor, record.channel_points)
            payout = record.data.get(PAYOUT_SCRIPT_KEY)
            if payout is not None and payout in _used_payout_scripts(cursor):
                raise ConflictError("the payout address was already used by another session")
            for point in record.channel_points:
                _reserve_channel_point(cursor, point)
            cursor.execute(
                "INSERT INTO sessions"
                " (session_id, peer_pubkey, role, revision, state, data, parent_signing_started)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.session_id,
                    record.peer_pubkey,
                    record.role,
                    record.revision,
                    record.state,
                    _encoded_data(record.data),
                    int(record.parent_signing_started),
                ),
            )
            cursor.executemany(
                "INSERT INTO session_channels (session_id, position, channel_point, reserved)"
                " VALUES (?, ?, ?, 1)",
                [
                    (record.session_id, position, point)
                    for position, point in enumerate(record.channel_points)
                ],
            )
        return record

    def next_payout_script(self, candidates: Sequence[str]) -> str:
        """Return the first candidate no recorded session has used as its payout.

        Every session records its own payout script under
        :data:`PAYOUT_SCRIPT_KEY`, and :meth:`create` refuses a script that is
        already recorded, so a concurrent pick cannot reuse an address. Scripts
        are never released, not even by a canceled session.
        """
        with self._read() as cursor:
            used = _used_payout_scripts(cursor)
        for script in candidates:
            if script not in used:
                return script
        raise ConflictError(
            "every configured payout address was already used; reserve fresh ones with"
            " `jm-wallet address new` and add them to payout_addresses"
        )

    def get(self, session_id: str) -> StoredSession:
        """Return the stored session, or raise :class:`UnknownSessionError`."""
        wanted = _validated_session_id(session_id)
        with self._read() as cursor:
            return _load_session(cursor, wanted)

    def list(self) -> list[StoredSession]:
        """Return every stored session, oldest creation first."""
        with self._read() as cursor:
            channels = _load_channel_points(cursor)
            rows = cursor.execute(
                "SELECT session_id, peer_pubkey, role, revision, state, data,"
                " parent_signing_started FROM sessions ORDER BY rowid"
            ).fetchall()
        return [_record_from_row(row, channels.get(row[0], ())) for row in rows]

    def update(
        self,
        record: StoredSession,
        *,
        state: str,
        data: dict[str, object],
        parent_signing_started: bool | None = None,
    ) -> StoredSession:
        """Compare-and-swap ``record`` to a new state, data and marker.

        ``parent_signing_started=None`` keeps the stored marker. The write
        applies only while the stored revision still equals ``record.revision``,
        and the returned record carries the incremented revision.
        """
        new_state = _validated_state(state)
        new_data = _encoded_data(data)
        wanted = _validated_session_id(record.session_id)
        with self._write() as cursor:
            stored = _load_session(cursor, wanted)
            if (
                stored.peer_pubkey != record.peer_pubkey
                or stored.role != record.role
                or stored.channel_points != record.channel_points
            ):
                raise InvalidRecordError("the record does not match the stored session binding")
            # Fixed at create: it can be neither changed, removed, nor added later
            # (a late addition would bypass the uniqueness check in create).
            if stored.data.get(PAYOUT_SCRIPT_KEY) != data.get(PAYOUT_SCRIPT_KEY):
                raise InvalidRecordError("a session's payout script cannot change")
            if stored.revision != record.revision:
                raise ConflictError("the session was modified by another writer")
            marker = (
                stored.parent_signing_started
                if parent_signing_started is None
                else bool(parent_signing_started)
            )
            _check_marker(cursor, stored, new_state, marker)
            changed = cursor.execute(
                "UPDATE sessions SET state = ?, data = ?, parent_signing_started = ?,"
                " revision = revision + 1 WHERE session_id = ? AND revision = ?",
                (new_state, new_data, int(marker), wanted, record.revision),
            ).rowcount
            if changed != 1:
                raise ConflictError("the session was modified by another writer")
        return replace(
            record,
            revision=record.revision + 1,
            state=new_state,
            data=_decoded_data(new_data),
            parent_signing_started=marker,
        )

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Cursor]:
        if self._closed:
            raise JournalError("the journal is closed")
        cursor = self._connection.cursor()
        try:
            yield cursor
        except sqlite3.Error:
            raise JournalError("the journal could not be read") from None
        finally:
            cursor.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Cursor]:
        if self._closed:
            raise JournalError("the journal is closed")
        cursor = self._connection.cursor()
        try:
            try:
                cursor.execute("BEGIN IMMEDIATE")
            except sqlite3.Error:
                raise JournalError("the journal could not be locked for writing") from None
            try:
                yield cursor
            except BaseException:
                with suppress(sqlite3.Error):
                    cursor.execute("ROLLBACK")
                raise
            cursor.execute("COMMIT")
        except sqlite3.IntegrityError:
            raise ConflictError("a channel point is reserved by another session") from None
        except sqlite3.Error:
            raise JournalError("the journal could not be written") from None
        finally:
            cursor.close()


_RESOLVED_ORDERED = tuple(sorted(RESOLVED_STATES))
_RESOLVED_PLACEHOLDERS = ", ".join("?" for _ in _RESOLVED_ORDERED)


def _check_marker(
    cursor: sqlite3.Cursor, stored: StoredSession, new_state: str, marker: bool
) -> None:
    """Reject the three marker transitions that would invalidate stored evidence."""
    if stored.parent_signing_started and not marker:
        raise InvalidRecordError("the parent signing marker cannot be cleared")
    if marker and new_state == STATE_CANCELED:
        raise InvalidRecordError("a session cannot be canceled once parent signing started")
    if marker and not stored.parent_signing_started:
        released = cursor.execute(
            "SELECT 1 FROM session_channels WHERE session_id = ? AND reserved = 0 LIMIT 1",
            (stored.session_id,),
        ).fetchone()
        if released is not None:
            raise InvalidRecordError(
                "parent signing cannot start after the channel points were taken over"
            )


def _used_payout_scripts(cursor: sqlite3.Cursor) -> set[str]:
    """Collect every payout script a stored session used or may have signed for."""
    used: set[str] = set()
    for (encoded,) in cursor.execute("SELECT data FROM sessions"):
        data = _decoded_data(encoded)
        script = data.get(PAYOUT_SCRIPT_KEY)
        if isinstance(script, str):
            used.add(script)
        # Sessions recorded before PAYOUT_SCRIPT_KEY existed still name their
        # split scripts in the agreed terms.
        for message, key in (("proposal", "split_script_B"), ("acceptance", "split_script_C")):
            body = data.get(message)
            if isinstance(body, dict) and isinstance(body.get(key), str):
                used.add(body[key])
    return used


def _reject_sibling_buyout(cursor: sqlite3.Cursor, points: Sequence[str]) -> None:
    """Refuse a buyer session sharing a funding transaction with an earlier one."""
    wanted = set(points)
    for txid in {point.rpartition(":")[0] for point in points}:
        rows = cursor.execute(
            "SELECT session_channels.channel_point FROM session_channels"
            " JOIN sessions USING (session_id)"
            " WHERE sessions.role = 'buyer' AND session_channels.reserved = 1"
            " AND NOT (sessions.state = ? AND sessions.parent_signing_started = 0)"
            " AND substr(session_channels.channel_point, 1, ?) = ?",
            (STATE_CANCELED, len(txid) + 1, f"{txid}:"),
        ).fetchall()
        # The exact same point is left to _reserve_channel_point, which applies
        # the canceled-and-never-signed takeover rule.
        if any(row[0] not in wanted for row in rows):
            raise ConflictError(
                "a buyout of another channel from the same funding transaction exists"
            )


def _reserve_channel_point(cursor: sqlite3.Cursor, point: str) -> None:
    """Take over ``point``, releasing a canceled never-signed holder if there is one."""
    row = cursor.execute(
        "SELECT sessions.state, sessions.parent_signing_started FROM session_channels"
        " JOIN sessions USING (session_id)"
        " WHERE session_channels.channel_point = ? AND session_channels.reserved = 1",
        (point,),
    ).fetchone()
    if row is None:
        return
    state, marker = row
    if state != STATE_CANCELED or marker != 0:
        raise ConflictError("a channel point is reserved by another session")
    cursor.execute(
        "UPDATE session_channels SET reserved = 0 WHERE channel_point = ? AND reserved = 1",
        (point,),
    )


def _load_session(cursor: sqlite3.Cursor, session_id: str) -> StoredSession:
    row = cursor.execute(
        "SELECT session_id, peer_pubkey, role, revision, state, data, parent_signing_started"
        " FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise UnknownSessionError("no session with this id exists in the journal")
    points = cursor.execute(
        "SELECT channel_point FROM session_channels WHERE session_id = ? ORDER BY position",
        (session_id,),
    ).fetchall()
    return _record_from_row(row, tuple(point for (point,) in points))


def _load_channel_points(cursor: sqlite3.Cursor) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, tuple[str, ...]] = {}
    rows = cursor.execute(
        "SELECT session_id, channel_point FROM session_channels ORDER BY session_id, position"
    ).fetchall()
    for session_id, point in rows:
        grouped[session_id] = (*grouped.get(session_id, ()), point)
    return grouped


def _record_from_row(row: tuple[object, ...], channel_points: tuple[str, ...]) -> StoredSession:
    """Rebuild a record, refusing a row that this module could not have written."""
    session_id, peer_pubkey, role, revision, state, data, marker = row
    if not isinstance(revision, int) or revision < 0 or marker not in (0, 1):
        raise JournalError("a stored session row is not readable")
    if not isinstance(session_id, str) or not isinstance(data, str):
        raise JournalError("a stored session row is not readable")
    try:
        return StoredSession(
            session_id=_validated_session_id(session_id),
            peer_pubkey=_validated_pubkey(peer_pubkey),
            role=_validated_role(role),
            channel_points=_validated_channel_points(channel_points),
            revision=revision,
            state=_validated_state(state),
            data=_decoded_data(data),
            parent_signing_started=bool(marker),
        )
    except (BuyoutStoreError, TypeError):
        raise JournalError("a stored session row is not readable") from None


def _validated_session_id(session_id: object) -> str:
    if not isinstance(session_id, str) or _SESSION_ID_RE.fullmatch(session_id) is None:
        raise InvalidRecordError("the session id must be 64 lowercase hex characters")
    return session_id


def _validated_pubkey(peer_pubkey: object) -> str:
    if not isinstance(peer_pubkey, str) or _PUBKEY_RE.fullmatch(peer_pubkey) is None:
        raise InvalidRecordError("the peer key must be a compressed key in lowercase hex")
    return peer_pubkey


def _validated_role(role: object) -> Role:
    if role not in _ROLES:
        raise InvalidRecordError("the role must be buyer or counterparty")
    return "buyer" if role == "buyer" else "counterparty"


def _validated_state(state: object) -> str:
    if not isinstance(state, str) or _STATE_RE.fullmatch(state) is None:
        raise InvalidRecordError("the state must be a short name of letters and underscores")
    return state


def _validated_channel_points(channel_points: object) -> tuple[str, ...]:
    if isinstance(channel_points, str) or not isinstance(channel_points, Iterable):
        raise InvalidRecordError("the channel points must be a sequence of txid:vout strings")
    points = tuple(channel_points)
    if not points or len(points) > MAX_CHANNEL_POINTS:
        raise InvalidRecordError("a session must reserve between one and 16 channel points")
    for point in points:
        if not isinstance(point, str) or _CHANNEL_POINT_RE.fullmatch(point) is None:
            raise InvalidRecordError("a channel point must be lowercase txid:vout")
        if int(point.split(":")[1]) > _MAX_VOUT:
            raise InvalidRecordError("a channel point must be lowercase txid:vout")
    if len(set(points)) != len(points):
        raise InvalidRecordError("a session cannot list the same channel point twice")
    return points


def _encoded_data(data: object) -> str:
    """Serialize ``data`` canonically, rejecting anything JSON cannot replay exactly."""
    if not isinstance(data, dict):
        raise InvalidRecordError("the session data must be a JSON object")
    _check_data_value(data, 0)
    try:
        encoded = json.dumps(
            data, allow_nan=False, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        raise InvalidRecordError("the session data is not JSON serializable") from None
    if len(encoded.encode("utf-8")) > MAX_DATA_BYTES:
        raise InvalidRecordError("the session data exceeds the maximum serialized size")
    return encoded


def _check_data_value(value: object, depth: int) -> None:
    if depth > MAX_DATA_DEPTH:
        raise InvalidRecordError("the session data is nested too deeply")
    if value is None or isinstance(value, bool | str):
        return
    if isinstance(value, int):
        if not -MAX_DATA_INTEGER <= value <= MAX_DATA_INTEGER:
            raise InvalidRecordError("the session data contains an out-of-range integer")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise InvalidRecordError("the session data object keys must be strings")
            _check_data_value(item, depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _check_data_value(item, depth + 1)
        return
    raise InvalidRecordError("the session data contains an unsupported value type")


def _decoded_data(encoded: str) -> dict[str, object]:
    try:
        decoded = json.loads(encoded)
    except (ValueError, RecursionError):
        raise JournalError("a stored session row is not readable") from None
    if not isinstance(decoded, dict):
        raise JournalError("a stored session row is not readable")
    try:
        _check_data_value(decoded, 0)
    except InvalidRecordError:
        raise JournalError("a stored session row is not readable") from None
    return decoded


def _prepare_file(path: Path) -> bool:
    """Make sure the directory and the file are private. Return True if created."""
    parent = path.parent
    try:
        info = os.lstat(parent)
    except FileNotFoundError:
        try:
            os.mkdir(parent, DIRECTORY_MODE)
            os.chmod(parent, DIRECTORY_MODE)
        except OSError:
            raise JournalError("the journal directory could not be created") from None
        info = os.lstat(parent)
    except OSError:
        raise JournalError("the journal directory could not be inspected") from None
    _require_private(info, directory=True)
    try:
        file_info = os.lstat(path)
    except FileNotFoundError:
        _create_private_file(path)
        return True
    except OSError:
        raise JournalError("the journal file could not be inspected") from None
    _require_private(file_info, directory=False)
    return False


def _require_private(info: os.stat_result, *, directory: bool) -> None:
    kind = "directory" if directory else "file"
    expected_mode = DIRECTORY_MODE if directory else FILE_MODE
    is_expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not is_expected_type:
        raise JournalError(f"the journal {kind} is a symlink or not a regular {kind}")
    if stat.S_IMODE(info.st_mode) != expected_mode:
        raise JournalError(f"the journal {kind} is not private to its owner")
    if info.st_uid != os.getuid():
        raise JournalError(f"the journal {kind} is owned by another user")


def _create_private_file(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, FILE_MODE)
    except OSError:
        raise JournalError("the journal file could not be created") from None
    try:
        os.fchmod(descriptor, FILE_MODE)
    except OSError:
        raise JournalError("the journal file could not be created") from None
    finally:
        os.close(descriptor)


def _fsync_directory(parent: Path) -> None:
    try:
        descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        raise JournalError("the journal directory could not be synced") from None
    try:
        os.fsync(descriptor)
    except OSError:
        raise JournalError("the journal directory could not be synced") from None
    finally:
        os.close(descriptor)


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None, check_same_thread=True
    )
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        journal_mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
    except sqlite3.Error:
        connection.close()
        raise JournalError("the journal file is not a usable database") from None
    if journal_mode is None or str(journal_mode[0]).lower() != "delete":
        connection.close()
        raise JournalError("the journal file could not be put in rollback journal mode")
    if foreign_keys is None or foreign_keys[0] != 1:
        connection.close()
        raise JournalError("the journal file could not enforce foreign keys")
    return connection


def _initialize_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("BEGIN IMMEDIATE")
        for _, statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except sqlite3.Error:
        with suppress(sqlite3.Error):
            connection.execute("ROLLBACK")
        raise JournalError("the journal schema could not be created") from None


def _verify_schema(connection: sqlite3.Connection) -> None:
    """Accept an existing file only at this exact version and schema."""
    try:
        version_row = connection.execute("PRAGMA user_version").fetchone()
        objects = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
    except sqlite3.Error:
        raise JournalError("the journal file is not a usable database") from None
    if version_row is None or version_row[0] != SCHEMA_VERSION:
        raise JournalError("the journal file is empty or written by an unsupported version")
    found = {name: _normalized_sql(sql) for name, sql in objects}
    expected = {name: _normalized_sql(sql) for name, sql in _SCHEMA}
    if found != expected:
        raise JournalError("the journal file does not carry the schema of this version")


def _normalized_sql(sql: object) -> str:
    return " ".join(str(sql).split())
