"""Cross-process advisory locks backed by stable sidecar files."""

from __future__ import annotations

import errno
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol, cast

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]


_WINDOWS_LOCK_RETRY_SECONDS = 0.05
_WINDOWS_LOCK_CONTENTION_ERRNOS = frozenset(
    error_number
    for error_number in (
        errno.EACCES,
        errno.EAGAIN,
        getattr(errno, "EDEADLK", None),
    )
    if error_number is not None
)


class _LockHandle(Protocol):
    def fileno(self) -> int: ...

    def seek(self, offset: int, whence: int = 0) -> int: ...


class _Msvcrt(Protocol):
    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, length: int) -> None: ...


def _sidecar_path(target: Path) -> Path:
    """Return the stable lock path associated with ``target``."""
    return target.with_suffix(".lock")


def _acquire_windows_lock(handle: _LockHandle) -> None:
    """Acquire the first sidecar byte, retrying while another process holds it."""
    assert msvcrt is not None
    windows_msvcrt = cast(_Msvcrt, msvcrt)

    while True:
        handle.seek(0)
        try:
            windows_msvcrt.locking(handle.fileno(), windows_msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno not in _WINDOWS_LOCK_CONTENTION_ERRNOS:
                raise
            time.sleep(_WINDOWS_LOCK_RETRY_SECONDS)
        else:
            return


@contextmanager
def exclusive_sidecar_lock(target: Path | str) -> Iterator[None]:
    """Acquire a blocking, cross-process advisory lock for ``target``.

    The target may be atomically replaced while this lock is held, so locking
    the target inode directly is unsafe. A persistent ``.lock`` sidecar keeps
    the lock stable across replacements.

    Raises:
        OSError: If the sidecar cannot be opened or locked.
        RuntimeError: If the platform has no supported file-locking primitive.
    """
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _sidecar_path(target_path)

    with lock_path.open("a+b") as handle:
        if os.name == "posix":
            if fcntl is None:  # pragma: no cover - broken POSIX Python build
                raise RuntimeError("POSIX file locking is unavailable")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif os.name == "nt":
            if msvcrt is None:  # pragma: no cover - broken Windows Python build
                raise RuntimeError("Windows file locking is unavailable")
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            _acquire_windows_lock(handle)
        else:  # pragma: no cover - unsupported platforms
            raise RuntimeError(f"Unsupported platform for file locking: {os.name}")

        try:
            yield
        finally:
            if os.name == "posix":
                assert fcntl is not None
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            else:  # pragma: no cover - Windows
                assert msvcrt is not None
                windows_msvcrt = cast(_Msvcrt, msvcrt)
                handle.seek(0)
                windows_msvcrt.locking(handle.fileno(), windows_msvcrt.LK_UNLCK, 1)
