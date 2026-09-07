"""Private directory and atomic secret-file utilities."""

from __future__ import annotations

import errno
import logging
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

logger = logging.getLogger(__name__)


def _open_regular_file(
    path: Path,
    *,
    follow_final_symlink: bool,
    allow_unchanged_mode: bool,
) -> int:
    """Open and tighten a regular file, optionally following a configured alias."""
    if not follow_final_symlink and path.is_symlink():
        raise OSError(f"refusing to use symlink as private file: {path}")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    if not follow_final_symlink:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            kind = "sensitive" if follow_final_symlink else "private"
            raise OSError(f"{kind} file does not exist or is not regular: {path}")
        try:
            os.fchmod(fd, 0o600)
        except OSError as exc:
            if not allow_unchanged_mode or exc.errno not in {
                errno.EACCES,
                errno.EPERM,
                errno.EROFS,
            }:
                raise
            logger.warning("Could not tighten sensitive file permissions")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_private_regular_file(path: Path) -> int:
    """Open a regular file without following its final symlink component."""
    return _open_regular_file(
        path,
        follow_final_symlink=False,
        allow_unchanged_mode=False,
    )


def _tighten_private_directory(path: Path) -> None:
    if os.name == "nt":  # Directory descriptors are not portable on Windows.
        path.chmod(0o700)
        return

    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _reject_parent_traversal(path: Path) -> None:
    if ".." in path.parts:
        raise ValueError(f"private path must not contain parent traversal ('..'): {path}")


def ensure_private_directory(path: Path) -> None:
    """Create or tighten a secret-bearing directory to owner-only access."""
    _reject_parent_traversal(path)
    if path.is_symlink():
        raise OSError(f"refusing to use symlink as private directory: {path}")

    missing_parents: list[Path] = []
    parent = path.parent
    while not parent.exists():
        missing_parents.append(parent)
        if parent == parent.parent:
            break
        parent = parent.parent

    for parent in reversed(missing_parents):
        parent.mkdir(mode=0o700, exist_ok=True)
        _tighten_private_directory(parent)

    path.mkdir(mode=0o700, exist_ok=True)
    _tighten_private_directory(path)


def ensure_private_file(path: Path) -> None:
    """Tighten an existing regular secret file to owner-only access."""
    fd = _open_private_regular_file(path)
    os.close(fd)


def read_private_file(path: Path) -> bytes:
    """Read and tighten a regular secret file through one no-follow descriptor."""
    fd = _open_private_regular_file(path)
    with os.fdopen(fd, "rb") as private_file:
        return private_file.read()


def ensure_sensitive_directory(path: Path) -> None:
    """Create a missing application directory privately without changing an existing one."""
    resolved_path = path.resolve(strict=False)
    if not resolved_path.exists():
        ensure_private_directory(resolved_path)


def ensure_sensitive_file(path: Path) -> None:
    """Best-effort tighten a regular config or metadata file, following aliases."""
    fd = _open_regular_file(
        path,
        follow_final_symlink=True,
        allow_unchanged_mode=True,
    )
    os.close(fd)


def read_sensitive_file(path: Path) -> bytes:
    """Read a regular config or metadata file, following aliases when configured."""
    fd = _open_regular_file(
        path,
        follow_final_symlink=True,
        allow_unchanged_mode=True,
    )
    with os.fdopen(fd, "rb") as sensitive_file:
        return sensitive_file.read()


def atomic_write_private(path: Path, data: bytes) -> None:
    """Atomically write bytes without exposing a permissively-mode temporary file."""
    _reject_parent_traversal(path)
    parent = path.parent
    if parent.is_symlink():
        raise OSError(f"refusing to use symlink as private directory: {parent}")
    if not parent.exists():
        ensure_private_directory(parent)

    fd, temp_name = tempfile.mkstemp(
        dir=parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as temp_file:
            fd = -1
            temp_file.write(data)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    finally:
        if fd >= 0:
            os.close(fd)
        with suppress(FileNotFoundError):
            temp_path.unlink()


def atomic_write_sensitive_file(path: Path, data: bytes) -> None:
    """Atomically update a sensitive file while preserving configured aliases."""
    atomic_write_private(path.resolve(strict=False), data)
