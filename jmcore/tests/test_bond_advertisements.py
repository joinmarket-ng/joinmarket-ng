"""Advertisement history preserves legacy policy and owns its cross-process RMW."""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
from pathlib import Path

import pytest

from jmcore.bond_advertisements import (
    AdvertisementRecordError,
    locked_advertisements,
    read_advertisements,
    record_advertisement,
)

FIRST = ("11" * 32, 0)
SECOND = ("22" * 32, 1)


def test_snapshot_absence_is_nonmutating_and_initialization_stays_conservative(
    tmp_path: Path,
) -> None:
    path = tmp_path / "missing-parent" / "advertised-bonds.json"
    assert read_advertisements(path) is None
    assert not path.parent.exists()
    record = record_advertisement(path, 10)
    assert json.loads(path.read_bytes()) == {
        "version": 1,
        "since": 11,
        "advertised_through": {},
    }
    assert not record.rentable(FIRST, 11) and record.rentable(FIRST, 12)
    first_lock_inode = path.with_name(path.name + ".lock").stat().st_ino
    record_advertisement(path, 12, FIRST)
    assert path.with_name(path.name + ".lock").stat().st_ino == first_lock_inode
    restored = read_advertisements(path)
    assert restored is not None and restored.through(FIRST) == 13


def test_scoped_writer_cannot_escape_or_mutate_its_private_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    with locked_advertisements(path) as history:
        returned = history.record(10, FIRST)
        returned.advertised_through[f"{SECOND[0]}:{SECOND[1]}"] = 99
        snapshot = history.snapshot
        assert snapshot is not None
        snapshot.advertised_through[f"{SECOND[0]}:{SECOND[1]}"] = 98
        history.record(12, FIRST)
    with pytest.raises(AdvertisementRecordError, match="closed"):
        history.record(13, SECOND)
    restored = read_advertisements(path)
    assert restored is not None
    assert restored.through(FIRST) == 13 and restored.through(SECOND) == 11


def test_corrupt_record_is_never_replaced_by_a_writer(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    original = b'{"version":1,"since":'
    path.write_bytes(original)
    with pytest.raises(AdvertisementRecordError, match="corrupt"):
        record_advertisement(path, 10, FIRST)
    assert path.read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="readiness handshake uses POSIX pipe selection")
def test_independent_process_cannot_lose_an_advertisement_update(tmp_path: Path) -> None:
    path = tmp_path / "history.json"
    record_advertisement(path, 10)
    script = """
from pathlib import Path
import sys
from jmcore.bond_advertisements import locked_advertisements
print('attempt', flush=True)
with locked_advertisements(Path(sys.argv[1])) as history:
    print('entered', flush=True)
    history.record(15, ('22' * 32, 1))
"""
    child = None
    try:
        with locked_advertisements(path) as history:
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(path)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert child.stdout is not None
            assert select.select([child.stdout], [], [], 10)[0], "writer did not reach lock attempt"
            assert child.stdout.readline() == "attempt\n"
            assert not select.select([child.stdout], [], [], 0.2)[0], (
                "writer entered held exclusion"
            )
            history.record(12, FIRST)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert stdout == "entered\n"
        restored = read_advertisements(path)
        assert restored is not None
        assert restored.through(FIRST) == 13 and restored.through(SECOND) == 16
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
