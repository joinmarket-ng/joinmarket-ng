"""New private publication must never fall back to replacing a wallet."""

from __future__ import annotations

import errno
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import pytest

from jmcore import secure_files
from jmcore.secure_files import atomic_write_private


def test_new_publication_is_complete_private_and_leaves_no_temporary_name(tmp_path: Path) -> None:
    path = tmp_path / "new" / "wallet"
    atomic_write_private(path, b"complete encrypted contents", replace=False)
    assert path.read_bytes() == b"complete encrypted contents"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]


def test_existing_wallet_is_never_replaced_or_tightened(tmp_path: Path) -> None:
    path = tmp_path / "wallet"
    path.write_bytes(b"existing wallet")
    path.chmod(0o644)
    before = path.stat()
    with pytest.raises(FileExistsError):
        atomic_write_private(path, b"other wallet", replace=False)
    assert path.read_bytes() == b"existing wallet"
    assert path.stat().st_ino == before.st_ino
    assert stat.S_IMODE(path.stat().st_mode) == stat.S_IMODE(before.st_mode)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs POSIX test privileges")
@pytest.mark.parametrize("dangling", [False, True])
def test_even_a_dangling_alias_is_an_existing_destination(tmp_path: Path, dangling: bool) -> None:
    target, alias = tmp_path / "target", tmp_path / "wallet"
    if not dangling:
        target.write_bytes(b"existing target")
    alias.symlink_to(target)
    with pytest.raises(FileExistsError):
        atomic_write_private(alias, b"new wallet", replace=False)
    assert alias.is_symlink()
    if dangling:
        assert not target.exists()
    else:
        assert target.read_bytes() == b"existing target"


def test_concurrent_publications_have_only_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "wallet"
    ready = Barrier(2)

    def publish(value: bytes) -> bool:
        ready.wait(timeout=3)
        try:
            atomic_write_private(path, value, replace=False)
        except FileExistsError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, (b"first complete wallet", b"second complete wallet")))
    assert sorted(results) == [False, True]
    assert path.read_bytes() in (b"first complete wallet", b"second complete wallet")
    assert list(tmp_path.iterdir()) == [path]


def test_unsupported_links_do_not_use_a_replacing_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "wallet"
    monkeypatch.setattr(
        secure_files.os, "link", Mock(side_effect=OSError(errno.ENOTSUP, "unsupported"))
    )
    replace = Mock()
    monkeypatch.setattr(secure_files.os, "replace", replace)
    with pytest.raises(OSError):
        atomic_write_private(path, b"wallet", replace=False)
    replace.assert_not_called()
    assert not path.exists() and list(tmp_path.iterdir()) == []


def test_successful_link_then_flush_failure_is_not_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "wallet"
    monkeypatch.setattr(
        secure_files,
        "sync_parent_directories",
        Mock(side_effect=OSError(errno.EIO, "flush failed")),
    )
    with pytest.raises(OSError):
        atomic_write_private(path, b"published but unacknowledged", replace=False)
    assert path.read_bytes() == b"published but unacknowledged"
    with pytest.raises(FileExistsError):
        atomic_write_private(path, b"must not replace", replace=False)
    assert path.read_bytes() == b"published but unacknowledged"
