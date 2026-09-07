"""Tests for owner-only atomic secret persistence."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from jmcore.secure_files import (
    atomic_write_private,
    atomic_write_sensitive_file,
    ensure_private_directory,
    ensure_private_file,
    ensure_sensitive_directory,
    ensure_sensitive_file,
    read_private_file,
    read_sensitive_file,
)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_ensure_private_directory_ignores_permissive_umask(tmp_path: Path) -> None:
    private_dir = tmp_path / "wallets"
    previous_umask = os.umask(0o022)
    try:
        ensure_private_directory(private_dir)
    finally:
        os.umask(previous_umask)

    assert _mode(private_dir) == 0o700


def test_ensure_private_directory_tightens_existing_mode(tmp_path: Path) -> None:
    private_dir = tmp_path / "wallets"
    private_dir.mkdir(mode=0o755)

    ensure_private_directory(private_dir)

    assert _mode(private_dir) == 0o700


def test_ensure_private_directory_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "wallets"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        ensure_private_directory(link)


def test_atomic_write_private_creates_private_parent_with_permissive_umask(
    tmp_path: Path,
) -> None:
    private_dir = tmp_path / "wallets"
    secret = private_dir / "wallet.jmdat"
    previous_umask = os.umask(0o000)
    try:
        atomic_write_private(secret, b"secret")
    finally:
        os.umask(previous_umask)

    assert secret.read_bytes() == b"secret"
    assert _mode(private_dir) == 0o700
    assert _mode(secret) == 0o600


def test_atomic_write_private_creates_nested_missing_parents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    intermediate_dir = Path("wallets")
    private_dir = intermediate_dir / "profiles"
    secret = private_dir / "wallet.jmdat"
    previous_umask = os.umask(0o000)
    try:
        atomic_write_private(secret, b"secret")
    finally:
        os.umask(previous_umask)

    assert _mode(tmp_path) == 0o755
    assert _mode(intermediate_dir) == 0o700
    assert _mode(private_dir) == 0o700
    assert secret.read_bytes() == b"secret"


def test_atomic_write_private_bare_relative_path_preserves_cwd_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tmp_path.chmod(0o755)
    monkeypatch.chdir(tmp_path)

    atomic_write_private(Path("wallet.jmdat"), b"secret")

    assert _mode(tmp_path) == 0o755
    assert Path("wallet.jmdat").read_bytes() == b"secret"


def test_atomic_write_private_preserves_existing_parent_mode(tmp_path: Path) -> None:
    existing_dir = tmp_path / "exports"
    existing_dir.mkdir(mode=0o755)

    atomic_write_private(existing_dir / "wallet.jmdat", b"secret")

    assert _mode(existing_dir) == 0o755


def test_atomic_write_private_rejects_symlink_parent(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    private_dir = tmp_path / "wallets"
    private_dir.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        atomic_write_private(private_dir / "wallet.jmdat", b"secret")

    assert not (target / "wallet.jmdat").exists()


def test_private_directory_helpers_reject_parent_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="parent traversal"):
        ensure_private_directory(Path("wallets") / ".." / "private")
    with pytest.raises(ValueError, match="parent traversal"):
        atomic_write_private(Path("wallets") / ".." / "wallet.jmdat", b"secret")

    assert not (tmp_path / "wallets").exists()
    assert not (tmp_path / "private").exists()


def test_ensure_private_file_tightens_existing_mode(tmp_path: Path) -> None:
    secret = tmp_path / "wallet.jmdat"
    secret.write_bytes(b"secret")
    secret.chmod(0o644)

    ensure_private_file(secret)

    assert _mode(secret) == 0o600


def test_ensure_private_file_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    link = tmp_path / "wallet.jmdat"
    link.symlink_to(target)

    with pytest.raises(OSError, match="symlink"):
        ensure_private_file(link)


def test_read_private_file_tightens_and_reads_same_file(tmp_path: Path) -> None:
    secret = tmp_path / "wallet.jmdat"
    secret.write_bytes(b"secret")
    secret.chmod(0o644)

    assert read_private_file(secret) == b"secret"
    assert _mode(secret) == 0o600


def test_read_private_file_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    link = tmp_path / "wallet.jmdat"
    link.symlink_to(target)

    with pytest.raises(OSError, match="symlink"):
        read_private_file(link)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are not available")
def test_read_private_file_rejects_non_regular_file_without_blocking(tmp_path: Path) -> None:
    fifo = tmp_path / "wallet.jmdat"
    os.mkfifo(fifo)

    with pytest.raises(OSError, match="not regular"):
        read_private_file(fifo)


def test_atomic_write_private_replaces_symlink_without_following_it(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"do not overwrite")
    link = tmp_path / "wallet.jmdat"
    link.symlink_to(target)

    atomic_write_private(link, b"new secret")

    assert not link.is_symlink()
    assert link.read_bytes() == b"new secret"
    assert target.read_bytes() == b"do not overwrite"
    assert _mode(link) == 0o600


def test_read_sensitive_file_follows_alias_and_tightens_target(tmp_path: Path) -> None:
    target = tmp_path / "configured.toml"
    target.write_bytes(b"secret")
    target.chmod(0o644)
    link = tmp_path / "config.toml"
    link.symlink_to(target)

    assert read_sensitive_file(link) == b"secret"
    ensure_sensitive_file(link)

    assert link.is_symlink()
    assert _mode(target) == 0o600


def test_read_sensitive_file_allows_read_when_tightening_is_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive = tmp_path / "configured.toml"
    sensitive.write_bytes(b"admin-managed")

    def deny_tightening(_fd: int, _mode: int) -> None:
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(os, "fchmod", deny_tightening)

    assert read_sensitive_file(sensitive) == b"admin-managed"


def test_atomic_write_sensitive_file_preserves_alias(tmp_path: Path) -> None:
    target = tmp_path / "configured.toml"
    target.write_bytes(b"old")
    link = tmp_path / "config.toml"
    link.symlink_to(target)

    atomic_write_sensitive_file(link, b"new")

    assert link.is_symlink()
    assert target.read_bytes() == b"new"
    assert _mode(target) == 0o600


def test_ensure_sensitive_directory_preserves_existing_alias_mode(tmp_path: Path) -> None:
    target = tmp_path / "shared-data"
    target.mkdir()
    target.chmod(0o755)
    link = tmp_path / "data"
    link.symlink_to(target, target_is_directory=True)

    ensure_sensitive_directory(link)

    assert link.is_symlink()
    assert _mode(target) == 0o755
