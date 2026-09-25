"""Tests for the durable private journal of buyout sessions.

The journal exists to be believed after a crash, so most tests here write with
one store object and read with another (or with another process) instead of
trusting the in-memory return value. The reservation and marker tests spell out
what each stored state is supposed to prove: a resolved session does not free
its channels, only an affirmative ``CANCELED`` record with the signing marker
false does, and no missing or unreadable state ever does.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from jmswap.buyout_store import (
    MAX_DATA_BYTES,
    MAX_UNRESOLVED_SESSIONS,
    SCHEMA_VERSION,
    BuyoutStore,
    ConflictError,
    InvalidRecordError,
    JournalError,
    StoredSession,
    UnknownSessionError,
)

PEER = "02" + "ab" * 32
OTHER_PEER = "03" + "cd" * 32


def session_id(tag: int) -> str:
    return f"{tag:064x}"


def channel_point(tag: int, vout: int = 0) -> str:
    return f"{tag:064x}:{vout}"


@pytest.fixture
def journal_path(tmp_path: Path) -> Path:
    """A path inside a directory the store has to create itself."""
    return tmp_path / "private" / "buyout.sqlite3"


@pytest.fixture
def store(journal_path: Path) -> Iterator[BuyoutStore]:
    with BuyoutStore(journal_path) as opened:
        yield opened


def create_default(store: BuyoutStore, tag: int = 1, **kwargs: object) -> StoredSession:
    arguments: dict[str, object] = {
        "session_id": session_id(tag),
        "peer_pubkey": PEER,
        "role": "buyer",
        "channel_points": (channel_point(tag),),
    }
    arguments.update(kwargs)
    return store.create(**arguments)  # type: ignore[arg-type]


def test_runtime_operation_excludes_other_instances_and_processes(
    store: BuyoutStore,
    journal_path: Path,
) -> None:
    code = textwrap.dedent("""
        import sys
        from pathlib import Path
        from jmswap.buyout_store import BuyoutStore, ConflictError
        with BuyoutStore(Path(sys.argv[1])) as store:
            try:
                with store.exclusive_operation():
                    pass
            except ConflictError:
                sys.exit(23)
    """)
    with BuyoutStore(journal_path) as other:
        with store.exclusive_operation():
            for contender in (store, other):
                with pytest.raises(ConflictError), contender.exclusive_operation():
                    pytest.fail("overlapping operation admitted")
            result = subprocess.run(
                [sys.executable, "-c", code, str(journal_path)], timeout=10, check=False
            )
            assert result.returncode == 23
        result = subprocess.run(
            [sys.executable, "-c", code, str(journal_path)], timeout=10, check=False
        )
        assert result.returncode == 0
        with other.exclusive_operation():
            create_default(other)


def test_runtime_operation_releases_after_exception(store: BuyoutStore) -> None:
    with pytest.raises(RuntimeError), store.exclusive_operation():
        raise RuntimeError("interrupted")
    with store.exclusive_operation():
        create_default(store)


def test_runtime_operation_rejects_closed_store(store: BuyoutStore) -> None:
    store.close()
    with pytest.raises(JournalError, match="closed"), store.exclusive_operation():
        pytest.fail("closed journal admitted")


@pytest.mark.parametrize("replacement", [False, True])
def test_runtime_operation_rejects_missing_or_replaced_file(
    store: BuyoutStore,
    journal_path: Path,
    replacement: bool,
) -> None:
    journal_path.rename(journal_path.with_suffix(".original"))
    if replacement:
        with BuyoutStore(journal_path):
            pass
    with pytest.raises(JournalError), store.exclusive_operation():
        pytest.fail("different journal admitted")


def test_create_returns_initial_record(store: BuyoutStore) -> None:
    record = create_default(store, data={"offer": "x"})
    assert record.revision == 0
    assert record.state == "CREATED"
    assert record.parent_signing_started is False
    assert record.channel_points == (channel_point(1),)
    assert record.data == {"offer": "x"}


def test_reopen_replays_records_exactly(journal_path: Path) -> None:
    payload: dict[str, object] = {
        "nested": {"list": [1, "two", True, None], "empty": {}},
        "count": -42,
        "big": 2**62,
    }
    with BuyoutStore(journal_path) as store:
        first = store.create(session_id(1), PEER, "buyer", (channel_point(1), channel_point(1, 3)))
        second = store.create(session_id(2), OTHER_PEER, "counterparty", (channel_point(2),))
        second = store.update(second, state="ESCROW_FUNDED", data=payload)

    with BuyoutStore(journal_path) as store:
        assert store.get(session_id(1)) == first
        assert store.get(session_id(2)) == second
        assert store.list() == [first, second]
        assert store.get(session_id(2)).data == payload


def test_get_unknown_session_raises(store: BuyoutStore) -> None:
    with pytest.raises(UnknownSessionError):
        store.get(session_id(9))


def test_duplicate_session_id_rejected(store: BuyoutStore) -> None:
    create_default(store)
    with pytest.raises(ConflictError):
        store.create(session_id(1), PEER, "buyer", (channel_point(5),))


def test_closed_store_rejects_use(journal_path: Path) -> None:
    store = BuyoutStore(journal_path)
    create_default(store)
    store.close()
    store.close()
    with pytest.raises(JournalError):
        store.get(session_id(1))
    with pytest.raises(JournalError):
        create_default(store, tag=2)


# --- compare-and-swap -------------------------------------------------------


def test_update_increments_revision_and_persists(journal_path: Path) -> None:
    with BuyoutStore(journal_path) as store:
        record = create_default(store)
        updated = store.update(record, state="PROPOSED", data={"a": 1})
        assert updated.revision == 1
        assert updated.state == "PROPOSED"
        assert updated.parent_signing_started is False
        again = store.update(updated, state="ACCEPTED", data={"a": 2})
        assert again.revision == 2
    with BuyoutStore(journal_path) as store:
        assert store.get(session_id(1)) == again


def test_stale_record_loses_across_two_connections(journal_path: Path) -> None:
    with BuyoutStore(journal_path) as writer, BuyoutStore(journal_path) as reader:
        record = create_default(writer)
        stale = reader.get(record.session_id)
        assert stale == record
        winner = writer.update(record, state="PROPOSED", data={})
        with pytest.raises(ConflictError):
            reader.update(stale, state="CANCELED", data={})
        assert reader.get(record.session_id) == winner


def test_update_rejects_changed_binding(store: BuyoutStore) -> None:
    record = create_default(store)
    for forged in (
        StoredSession(
            record.session_id,
            OTHER_PEER,
            record.role,
            record.channel_points,
            record.revision,
            record.state,
            record.data,
            record.parent_signing_started,
        ),
        StoredSession(
            record.session_id,
            record.peer_pubkey,
            "counterparty",
            record.channel_points,
            record.revision,
            record.state,
            record.data,
            record.parent_signing_started,
        ),
        StoredSession(
            record.session_id,
            record.peer_pubkey,
            record.role,
            (channel_point(7),),
            record.revision,
            record.state,
            record.data,
            record.parent_signing_started,
        ),
    ):
        with pytest.raises(InvalidRecordError):
            store.update(forged, state="PROPOSED", data={})
    assert store.get(record.session_id) == record


def test_update_of_unknown_session_raises(store: BuyoutStore) -> None:
    record = create_default(store)
    missing = StoredSession(
        session_id(8),
        record.peer_pubkey,
        record.role,
        record.channel_points,
        0,
        "CREATED",
        {},
        False,
    )
    with pytest.raises(UnknownSessionError):
        store.update(missing, state="PROPOSED", data={})


# --- signing marker ---------------------------------------------------------


def test_marker_is_monotonic(journal_path: Path) -> None:
    with BuyoutStore(journal_path) as store:
        record = create_default(store)
        signing = store.update(record, state="SIGNING", data={}, parent_signing_started=True)
        assert signing.parent_signing_started is True
        with pytest.raises(InvalidRecordError):
            store.update(signing, state="ABORTED", data={}, parent_signing_started=False)
        kept = store.update(signing, state="BROADCAST", data={})
        assert kept.parent_signing_started is True
    with BuyoutStore(journal_path) as store:
        assert store.get(session_id(1)).parent_signing_started is True


def test_cancel_after_signing_started_is_rejected(store: BuyoutStore) -> None:
    record = create_default(store)
    signing = store.update(record, state="SIGNING", data={}, parent_signing_started=True)
    with pytest.raises(InvalidRecordError):
        store.update(signing, state="CANCELED", data={})
    with pytest.raises(InvalidRecordError):
        store.update(
            store.get(record.session_id),
            state="CANCELED",
            data={},
            parent_signing_started=True,
        )
    assert store.get(record.session_id).state == "SIGNING"


def test_signing_cannot_start_while_canceling(store: BuyoutStore) -> None:
    record = create_default(store)
    with pytest.raises(InvalidRecordError):
        store.update(record, state="CANCELED", data={}, parent_signing_started=True)
    assert store.get(record.session_id).state == "CREATED"


# --- channel point reservations ---------------------------------------------


def test_channel_point_reserved_across_stores(journal_path: Path) -> None:
    with BuyoutStore(journal_path) as first, BuyoutStore(journal_path) as second:
        create_default(first, tag=1)
        with pytest.raises(ConflictError):
            second.create(session_id(2), OTHER_PEER, "counterparty", (channel_point(1),))
        with pytest.raises(ConflictError):
            second.create(
                session_id(2), OTHER_PEER, "counterparty", (channel_point(2), channel_point(1))
            )
        # The failed attempt reserved nothing of its own.
        assert second.create(session_id(3), OTHER_PEER, "buyer", (channel_point(2),))


def test_completed_session_keeps_its_channels_reserved(store: BuyoutStore) -> None:
    record = create_default(store)
    store.update(record, state="COMPLETED", data={})
    with pytest.raises(ConflictError):
        store.create(session_id(2), PEER, "buyer", (channel_point(1),))


def test_conflicted_session_keeps_its_channels_reserved(store: BuyoutStore) -> None:
    record = create_default(store)
    store.update(record, state="CONFLICTED", data={})
    with pytest.raises(ConflictError):
        store.create(session_id(2), PEER, "buyer", (channel_point(1),))


def test_canceled_never_signed_session_releases_its_channels(journal_path: Path) -> None:
    with BuyoutStore(journal_path) as store:
        first = create_default(store, tag=1)
        canceled = store.update(first, state="CANCELED", data={"why": "peer left"})
        reused = store.create(session_id(2), OTHER_PEER, "counterparty", (channel_point(1),))
        assert reused.channel_points == (channel_point(1),)
        # The canceled record is kept verbatim, including its channel points.
        assert store.get(session_id(1)) == canceled
        # Only one session may hold the channel at a time.
        with pytest.raises(ConflictError):
            store.create(session_id(3), PEER, "buyer", (channel_point(1),))
    with BuyoutStore(journal_path) as store:
        assert store.get(session_id(1)) == canceled
        assert store.get(session_id(2)).channel_points == (channel_point(1),)


def test_taken_over_session_can_never_start_signing(store: BuyoutStore) -> None:
    first = create_default(store, tag=1)
    canceled = store.update(first, state="CANCELED", data={})
    store.create(session_id(2), OTHER_PEER, "counterparty", (channel_point(1),))
    with pytest.raises(InvalidRecordError):
        store.update(canceled, state="SIGNING", data={}, parent_signing_started=True)
    assert store.get(session_id(1)) == canceled


def test_unresolved_session_limit(store: BuyoutStore) -> None:
    records = [create_default(store, tag=tag) for tag in range(1, MAX_UNRESOLVED_SESSIONS + 1)]
    with pytest.raises(ConflictError):
        create_default(store, tag=MAX_UNRESOLVED_SESSIONS + 1)
    store.update(records[0], state="COMPLETED", data={})
    freed = create_default(store, tag=MAX_UNRESOLVED_SESSIONS + 1)
    assert freed.revision == 0
    with pytest.raises(ConflictError):
        create_default(store, tag=MAX_UNRESOLVED_SESSIONS + 2)


# --- input validation -------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "ab", "AB" * 32, "zz" * 32, "ab" * 33],
)
def test_invalid_session_id_rejected(store: BuyoutStore, bad: str) -> None:
    with pytest.raises(InvalidRecordError):
        store.create(bad, PEER, "buyer", (channel_point(1),))


@pytest.mark.parametrize("bad", ["04" + "ab" * 32, "02" + "AB" * 32, "ab" * 32, ""])
def test_invalid_peer_key_rejected(store: BuyoutStore, bad: str) -> None:
    with pytest.raises(InvalidRecordError):
        store.create(session_id(1), bad, "buyer", (channel_point(1),))


def test_invalid_role_rejected(store: BuyoutStore) -> None:
    with pytest.raises(InvalidRecordError):
        store.create(session_id(1), PEER, "maker", (channel_point(1),))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "points",
    [
        (),
        ("ab" * 32,),
        ("ab" * 32 + ":",),
        ("ab" * 32 + ":01",),
        ("ab" * 32 + ":-1",),
        ("AB" * 32 + ":0",),
        ("ab" * 32 + ":4294967296",),
        (channel_point(1), channel_point(1)),
        tuple(channel_point(1, vout) for vout in range(17)),
    ],
)
def test_invalid_channel_points_rejected(store: BuyoutStore, points: tuple[str, ...]) -> None:
    with pytest.raises(InvalidRecordError):
        store.create(session_id(1), PEER, "buyer", points)


def test_channel_points_must_be_a_sequence(store: BuyoutStore) -> None:
    with pytest.raises(InvalidRecordError):
        store.create(session_id(1), PEER, "buyer", channel_point(1))


@pytest.mark.parametrize("bad", ["", "with space", "lower9", "A" * 65, "STATE-1"])
def test_invalid_state_rejected(store: BuyoutStore, bad: str) -> None:
    record = create_default(store)
    with pytest.raises(InvalidRecordError):
        store.update(record, state=bad, data={})


@pytest.mark.parametrize(
    "bad",
    [
        {"f": 1.5},
        {"b": b"bytes"},
        {"t": (1, 2)},
        {"s": {1, 2}},
        {"nested": [{"deep": 0.0}]},
        {1: "int key"},
        {"o": object()},
        {"big": 2**63},
    ],
)
def test_invalid_data_rejected(store: BuyoutStore, bad: dict[object, object]) -> None:
    with pytest.raises(InvalidRecordError):
        create_default(store, tag=2, data=bad)
    record = create_default(store)
    with pytest.raises(InvalidRecordError):
        store.update(record, state="PROPOSED", data=bad)  # type: ignore[arg-type]
    assert store.get(record.session_id) == record


def test_data_must_be_an_object(store: BuyoutStore) -> None:
    with pytest.raises(InvalidRecordError):
        create_default(store, data=[1, 2])


def test_oversized_data_rejected(store: BuyoutStore) -> None:
    with pytest.raises(InvalidRecordError):
        create_default(store, data={"blob": "x" * MAX_DATA_BYTES})
    fitting = {"blob": "x" * (MAX_DATA_BYTES - 64)}
    assert create_default(store, data=fitting).data == fitting


def test_deeply_nested_data_rejected(store: BuyoutStore) -> None:
    payload: dict[str, object] = {}
    cursor = payload
    for _ in range(40):
        nested: dict[str, object] = {}
        cursor["next"] = nested
        cursor = nested
    with pytest.raises(InvalidRecordError):
        create_default(store, data=payload)


def test_errors_and_repr_do_not_leak_private_values(store: BuyoutStore) -> None:
    record = create_default(store, data={"secret": "preimage-value"})
    rendered = repr(record)
    assert "preimage-value" not in rendered
    assert PEER not in rendered
    assert channel_point(1) not in rendered
    assert record.session_id in rendered

    with pytest.raises(ConflictError) as reserved:
        store.create(session_id(2), OTHER_PEER, "buyer", (channel_point(1),))
    with pytest.raises(InvalidRecordError) as bad_data:
        store.update(record, state="PROPOSED", data={"secret": 1.5})
    for error in (str(reserved.value), str(bad_data.value)):
        assert channel_point(1) not in error
        assert PEER not in error
        assert "secret" not in error
        assert "1.5" not in error


# --- file and schema handling -----------------------------------------------


def test_creates_private_directory_and_file(journal_path: Path) -> None:
    with BuyoutStore(journal_path):
        pass
    assert stat.S_IMODE(journal_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
    assert not list(journal_path.parent.glob("*-wal"))


def test_rejects_world_readable_directory(tmp_path: Path) -> None:
    directory = tmp_path / "loose"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    with pytest.raises(JournalError):
        BuyoutStore(directory / "buyout.sqlite3")
    assert not (directory / "buyout.sqlite3").exists()


def test_rejects_symlinked_directory(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(JournalError):
        BuyoutStore(link / "buyout.sqlite3")


def test_rejects_symlinked_file(journal_path: Path, tmp_path: Path) -> None:
    with BuyoutStore(journal_path) as store:
        create_default(store)
    target = journal_path.parent / "elsewhere.sqlite3"
    journal_path.rename(target)
    journal_path.symlink_to(target)
    with pytest.raises(JournalError):
        BuyoutStore(journal_path)


def test_rejects_group_readable_file(journal_path: Path) -> None:
    with BuyoutStore(journal_path) as store:
        create_default(store)
    os.chmod(journal_path, 0o640)
    with pytest.raises(JournalError):
        BuyoutStore(journal_path)
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o640


def test_rejects_non_regular_file(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    fifo = directory / "buyout.sqlite3"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(JournalError):
        BuyoutStore(fifo)


def test_refuses_empty_file_without_touching_it(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "buyout.sqlite3"
    path.touch(mode=0o600)
    with pytest.raises(JournalError):
        BuyoutStore(path)
    assert path.stat().st_size == 0


def test_refuses_unknown_schema_version_without_overwriting(journal_path: Path) -> None:
    with BuyoutStore(journal_path) as store:
        create_default(store, data={"keep": "me"})
    connection = sqlite3.connect(journal_path, isolation_level=None)
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    connection.close()
    digest = hashlib.sha256(journal_path.read_bytes()).hexdigest()
    with pytest.raises(JournalError):
        BuyoutStore(journal_path)
    assert hashlib.sha256(journal_path.read_bytes()).hexdigest() == digest


def test_refuses_foreign_schema_at_same_version(journal_path: Path) -> None:
    journal_path.parent.mkdir(mode=0o700)
    connection = sqlite3.connect(journal_path, isolation_level=None)
    connection.execute("CREATE TABLE sessions (session_id TEXT)")
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    connection.close()
    os.chmod(journal_path, 0o600)
    with pytest.raises(JournalError):
        BuyoutStore(journal_path)


def test_refuses_corrupt_database_without_overwriting(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "buyout.sqlite3"
    path.write_bytes(b"not a database at all" * 16)
    os.chmod(path, 0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(JournalError):
        BuyoutStore(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_tampered_row_is_refused(journal_path: Path) -> None:
    with BuyoutStore(journal_path) as store:
        create_default(store)
    connection = sqlite3.connect(journal_path, isolation_level=None)
    connection.execute("UPDATE sessions SET data = ?", (json.dumps({"rate": 1.5}),))
    connection.close()
    with BuyoutStore(journal_path) as store:
        with pytest.raises(JournalError):
            store.get(session_id(1))
        with pytest.raises(JournalError):
            store.list()


# --- crash atomicity --------------------------------------------------------


CRASH_CHILD = """
import os, signal, sys
from pathlib import Path
from jmswap.buyout_store import BuyoutStore

store = BuyoutStore(Path(sys.argv[1]))
record = store.get(sys.argv[2])
with store._write() as cursor:
    cursor.execute(
        "UPDATE sessions SET state = 'COMPLETED', revision = revision + 1 WHERE session_id = ?",
        (record.session_id,),
    )
    os.kill(os.getpid(), signal.SIGKILL)
"""


def test_interrupted_write_leaves_previous_revision(journal_path: Path) -> None:
    with BuyoutStore(journal_path) as store:
        record = create_default(store, data={"phase": "proposed"})

    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(CRASH_CHILD), str(journal_path), record.session_id],
        capture_output=True,
    )
    assert result.returncode == -9, result.stderr.decode()

    with BuyoutStore(journal_path) as store:
        assert store.get(record.session_id) == record
        # The journal is still writable after recovering the interrupted write.
        assert store.update(record, state="PROPOSED", data={}).revision == 1
