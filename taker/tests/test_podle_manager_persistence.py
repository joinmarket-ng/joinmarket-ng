"""Durability of the taker's used PoDLE commitment record."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from _taker_test_helpers import make_utxo
from jmcore.paths import get_used_commitments_path
from jmcore.podle import generate_podle

from taker.podle import ExtendedPoDLECommitment
from taker.podle_manager import CommitmentHistoryError, PoDLEManager, initialize_commitment_history

_SAVE_WITH_SIZE_LIMIT = textwrap.dedent(
    """
    import resource
    import signal
    import sys
    from pathlib import Path

    from taker.podle_manager import PoDLEManager

    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    manager = PoDLEManager(data_dir=Path(sys.argv[1]))
    limit = manager.filepath.stat().st_size // 2
    resource.setrlimit(resource.RLIMIT_FSIZE, (limit, resource.RLIM_INFINITY))
    manager.used_commitments.add("ff" * 32)
    manager._save()
    """
)


@pytest.mark.skipif(sys.platform == "win32", reason="needs RLIMIT_FSIZE")
def test_failed_save_keeps_previous_used_commitments(tmp_path: Path) -> None:
    """A write that fails part way (disk full) must not lose earlier commitments."""
    initialize_commitment_history(tmp_path)
    manager = PoDLEManager(data_dir=tmp_path)
    manager.used_commitments = {f"{i:064x}" for i in range(200)}
    manager._save()

    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path))
    result = subprocess.run(
        [sys.executable, "-c", _SAVE_WITH_SIZE_LIMIT, str(tmp_path)],
        env=env,
        check=False,
        capture_output=True,
        timeout=60,
    )

    assert result.returncode != 0
    assert b"CommitmentHistoryError" in result.stderr
    assert PoDLEManager(data_dir=tmp_path).used_commitments >= manager.used_commitments


def test_corrupt_commitments_file_is_not_discarded(tmp_path: Path) -> None:
    filepath = get_used_commitments_path(tmp_path)
    corrupt = '{"used": ["' + "ab" * 32 + '", '
    filepath.write_text(corrupt)

    with pytest.raises(CommitmentHistoryError):
        PoDLEManager(data_dir=tmp_path)
    assert filepath.read_text() == corrupt
    assert list(filepath.parent.iterdir()) == [filepath]


def test_repeated_corruption_keeps_original_and_existing_backup(tmp_path: Path) -> None:
    filepath = get_used_commitments_path(tmp_path)
    backup = filepath.with_suffix(".json.corrupt.1700000000")
    backup.write_bytes(b"earlier evidence")
    corrupt_contents = ['{"used": [', '{"used": ["' + "ab" * 32 + '", ']

    for contents in corrupt_contents:
        filepath.write_text(contents)
        with pytest.raises(CommitmentHistoryError):
            PoDLEManager(data_dir=tmp_path)
        assert filepath.read_text() == contents

    backups = sorted(filepath.parent.glob(f"{filepath.name}.corrupt.*"))
    assert backups == [backup]
    assert backup.read_bytes() == b"earlier evidence"


def test_unreadable_history_is_left_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filepath = get_used_commitments_path(tmp_path)
    filepath.write_text('{"used": [], "external": {}}')

    def deny_open(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr("taker.podle_manager.open", deny_open, raising=False)
    with pytest.raises(CommitmentHistoryError) as exc:
        PoDLEManager(data_dir=tmp_path)

    assert isinstance(exc.value.__cause__, PermissionError)
    assert filepath.read_text() == '{"used": [], "external": {}}'
    assert not list(filepath.parent.glob(f"{filepath.name}.corrupt.*"))


def test_missing_history_with_orphaned_backup_requires_operator_action(tmp_path: Path) -> None:
    path = get_used_commitments_path(tmp_path)
    backup = path.with_suffix(".json.corrupt.1")
    backup.write_bytes(b"preserve")
    with pytest.raises(CommitmentHistoryError, match="missing"):
        PoDLEManager(tmp_path)
    assert not path.exists()
    assert backup.read_bytes() == b"preserve"


def test_initializer_refuses_dangling_history_alias(tmp_path: Path) -> None:
    path = get_used_commitments_path(tmp_path)
    target = tmp_path / "missing-target"
    path.symlink_to(target)
    with pytest.raises(CommitmentHistoryError, match="already exists"):
        initialize_commitment_history(tmp_path)
    assert path.is_symlink()
    assert not target.exists()


def test_concurrent_initialization_has_exactly_one_winner(tmp_path: Path) -> None:
    # Precreate only the parent, leaving the exclusive file creation to race.
    path = get_used_commitments_path(tmp_path)

    def initialize(_index: int) -> bool:
        try:
            initialize_commitment_history(tmp_path)
        except CommitmentHistoryError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialize, range(2)))
    assert sorted(results) == [False, True]
    assert json.loads(path.read_text()) == {"used": [], "external": {}}


@pytest.mark.parametrize(
    "contents",
    [
        b"[]",
        b"{}",
        b'{"used": []}',
        b'{"used": {}, "external": {}}',
        b'{"used": [1], "external": {}}',
        b'{"used": ["ab cd"], "external": {}}',
        b'{"used": ["0"], "external": {}}',
        b'{"used": [], "external": []}',
        b'{"used": ["ab"], "used": [], "external": {}}',
        b"\xff",
    ],
)
def test_invalid_history_schema_preserves_bytes(tmp_path: Path, contents: bytes) -> None:
    path = get_used_commitments_path(tmp_path)
    path.write_bytes(contents)
    with pytest.raises(CommitmentHistoryError):
        PoDLEManager(tmp_path)
    assert path.read_bytes() == contents
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize(
    "entry",
    [
        {},
        {"P": "aa"},
        {"P": "zz", "reveal": {}},
        {"P": "aa", "reveal": {"x": {}}},
        {"P": "aa", "reveal": {"0": {"P2": "aa", "s": "aa"}}},
    ],
)
def test_partial_external_history_is_not_accepted(tmp_path: Path, entry: dict) -> None:
    path = get_used_commitments_path(tmp_path)
    contents = json.dumps({"used": [], "external": {"00" * 32 + ":08": entry}})
    path.write_text(contents)
    with pytest.raises(CommitmentHistoryError):
        PoDLEManager(tmp_path)
    assert path.read_text() == contents


def test_reference_history_preserves_external_extensions_and_case_equivalence(
    tmp_path: Path,
) -> None:
    path = get_used_commitments_path(tmp_path)
    external = {
        "00" * 32 + ":08": {
            "P": "aB",
            "extension": ["preserve"],
            "reveal": {"00": {"P2": "", "s": "Aa", "e": "bb", "extra": 7}},
        }
    }
    original = {"used": ["AB" * 32, "", "aa"], "external": external, "extra": {"value": 1}}
    contents = json.dumps(original).encode()
    path.write_bytes(contents)
    manager = PoDLEManager(tmp_path)
    assert manager.used_commitments == {"ab" * 32, "", "aa"}
    assert path.read_bytes() == contents
    manager.used_commitments.add("cd" * 32)
    manager._save()
    saved = json.loads(path.read_text())
    assert saved["external"] == external
    assert saved["extra"] == original["extra"]


@pytest.mark.parametrize("previous_release", [False, True])
def test_save_failure_never_releases_initial_or_replacement_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    previous_release: bool,
) -> None:
    initialize_commitment_history(tmp_path)
    manager = PoDLEManager(tmp_path)
    utxo = make_utxo()

    def generate() -> ExtendedPoDLECommitment | None:
        return manager.generate_fresh_commitment([utxo], 10_000_000, lambda _address: b"\x01" * 32)

    if previous_release:
        assert generate() is not None
    recorded = manager.filepath.read_bytes()

    def fail_save(_path: Path, _data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("taker.podle_manager.atomic_write_sensitive_file", fail_save)
    with pytest.raises(CommitmentHistoryError, match="No fresh commitment was released"):
        generate()
    assert manager.filepath.read_bytes() == recorded
    assert len(manager.used_commitments) == (2 if previous_release else 1)


def test_blacklist_save_failure_does_not_skip_to_another_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    initialize_commitment_history(tmp_path)
    manager = PoDLEManager(tmp_path)
    utxo = make_utxo()
    blacklist = Mock()
    blacklist.is_blacklisted.return_value = True
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: blacklist)

    def fail_save(_path: Path, _data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("taker.podle_manager.atomic_write_sensitive_file", fail_save)
    with pytest.raises(CommitmentHistoryError):
        manager.get_fresh_commitment_utxos([utxo], 10_000_000, lambda _address: b"\x01" * 32)
    blacklist.is_blacklisted.assert_called_once()


def test_directory_sync_failure_stops_release_even_if_history_was_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialize_commitment_history(tmp_path)
    manager = PoDLEManager(tmp_path)

    def fail_directory_sync(_path: Path) -> None:
        raise OSError("directory sync failed")

    monkeypatch.setattr("jmcore.secure_files._sync_directory", fail_directory_sync)
    with pytest.raises(CommitmentHistoryError, match="No fresh commitment was released"):
        manager.generate_fresh_commitment([make_utxo()], 10_000_000, lambda _address: b"\x01" * 32)
    assert set(json.loads(manager.filepath.read_text())["used"]) == manager.used_commitments
    assert len(manager.used_commitments) == 1


def test_imported_uppercase_commitment_is_not_reused(tmp_path: Path) -> None:
    utxo = make_utxo()
    previous = generate_podle(b"\x01" * 32, f"{utxo.txid}:{utxo.vout}", 0)
    path = get_used_commitments_path(tmp_path)
    path.write_text(json.dumps({"used": [previous.commitment.hex().upper()], "external": {}}))
    manager = PoDLEManager(tmp_path)
    fresh = manager.generate_fresh_commitment([utxo], 10_000_000, lambda _address: b"\x01" * 32)
    assert fresh is not None
    assert fresh.index == 1
