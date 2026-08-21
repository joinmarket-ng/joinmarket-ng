"""Tests for cross-process sidecar file locks."""

from __future__ import annotations

import errno
import multiprocessing
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from jmcore import file_lock
from jmcore.file_lock import exclusive_sidecar_lock


class _WindowsLockHandle:
    def __init__(self) -> None:
        self.seeks: list[int] = []

    def fileno(self) -> int:
        return 42

    def seek(self, offset: int, whence: int = 0) -> int:
        self.seeks.append(offset)
        return offset


def _acquire_and_signal(target: str, acquired: multiprocessing.Queue[bool]) -> None:
    with exclusive_sidecar_lock(target):
        acquired.put(True)


def test_exclusive_sidecar_lock_uses_stable_sidecar(tmp_path: Path) -> None:
    target = tmp_path / "metadata.jsonl"

    with exclusive_sidecar_lock(target):
        assert (tmp_path / "metadata.lock").exists()


def test_exclusive_sidecar_lock_blocks_other_processes(tmp_path: Path) -> None:
    target = tmp_path / "metadata.jsonl"
    context = multiprocessing.get_context("spawn")
    acquired: multiprocessing.Queue[bool] = context.Queue()

    with exclusive_sidecar_lock(target):
        process = context.Process(target=_acquire_and_signal, args=(str(target), acquired))
        process.start()
        with pytest.raises(queue.Empty):
            acquired.get(timeout=0.2)

    assert acquired.get(timeout=5) is True
    process.join(timeout=5)
    assert process.exitcode == 0


def test_windows_lock_retries_contention_until_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = _WindowsLockHandle()
    errors = [OSError(errno.EACCES, "locked"), OSError(errno.EAGAIN, "locked")]
    attempts: list[tuple[int, int, int]] = []
    sleeps: list[float] = []

    def locking(file_descriptor: int, mode: int, length: int) -> None:
        attempts.append((file_descriptor, mode, length))
        if errors:
            raise errors.pop(0)

    monkeypatch.setattr(
        file_lock,
        "msvcrt",
        SimpleNamespace(LK_NBLCK=7, locking=locking),
    )
    monkeypatch.setattr(file_lock.time, "sleep", sleeps.append)

    file_lock._acquire_windows_lock(handle)

    assert attempts == [(42, 7, 1), (42, 7, 1), (42, 7, 1)]
    assert handle.seeks == [0, 0, 0]
    assert sleeps == [file_lock._WINDOWS_LOCK_RETRY_SECONDS] * 2


def test_windows_lock_propagates_unexpected_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = _WindowsLockHandle()
    unexpected_error = OSError(errno.EIO, "disk error")

    def locking(file_descriptor: int, mode: int, length: int) -> None:
        raise unexpected_error

    monkeypatch.setattr(
        file_lock,
        "msvcrt",
        SimpleNamespace(LK_NBLCK=7, locking=locking),
    )

    with pytest.raises(OSError) as raised:
        file_lock._acquire_windows_lock(handle)

    assert raised.value is unexpected_error
    assert handle.seeks == [0]
