"""Tests for private Tor hidden-service key file creation."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from scripts import generate_tor_keys as tool


@pytest.fixture
def permissive_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def _run(monkeypatch: pytest.MonkeyPatch, output_dir: Path) -> int:
    monkeypatch.setattr(tool.sys, "argv", ["generate_tor_keys.py", str(output_dir)])
    return tool.main()


@pytest.mark.usefixtures("permissive_umask")
def test_new_directory_and_files_are_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "keys"

    assert _run(monkeypatch, output_dir) == 0

    assert _mode(output_dir) == 0o700
    for name in ("hs_ed25519_secret_key", "hs_ed25519_public_key", "hostname"):
        assert _mode(output_dir / name) == 0o600
    assert (
        (output_dir / "hs_ed25519_secret_key")
        .read_bytes()
        .startswith(b"== ed25519v1-secret: type0 ==")
    )
    assert (output_dir / "hostname").read_text().strip().endswith(".onion")


def test_existing_key_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "hs_ed25519_secret_key"
    secret.write_bytes(b"existing identity")

    assert _run(monkeypatch, tmp_path) == 1

    assert secret.read_bytes() == b"existing identity"
    assert not (tmp_path / "hs_ed25519_public_key").exists()
    assert not (tmp_path / "hostname").exists()


def test_planted_symlink_is_refused_without_writing_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_dir = tmp_path / "keys"
    output_dir.mkdir()
    target = tmp_path / "attacker-readable"
    (output_dir / "hs_ed25519_secret_key").symlink_to(target)

    assert _run(monkeypatch, output_dir) == 1

    assert not target.exists()


def test_exclusive_writer_refuses_symlink_created_after_the_check(
    tmp_path: Path,
) -> None:
    target = tmp_path / "attacker-readable"
    link = tmp_path / "hs_ed25519_secret_key"
    link.symlink_to(target)

    with pytest.raises(FileExistsError):
        tool.write_new_private_file(link, b"secret")

    assert not target.exists()
