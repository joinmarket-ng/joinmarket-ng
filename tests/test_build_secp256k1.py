"""The native builder must authenticate source before invoking build tools."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import build_secp256k1 as builder


@pytest.mark.parametrize("cached", [False, True])
def test_bad_digest_never_extracts_or_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cached: bool
) -> None:
    if cached:
        (tmp_path / f"secp256k1-{builder.COMMIT}.tar.gz").write_bytes(b"corrupt")
    fetch = Mock(return_value=io.BytesIO(b"corrupt"))
    extract = Mock()
    run = Mock()
    monkeypatch.setattr(builder.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(builder.tarfile, "open", extract)
    monkeypatch.setattr(builder.subprocess, "run", run)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        builder.build_library(tmp_path / "prefix", tmp_path)

    assert fetch.call_count == (0 if cached else 1)
    extract.assert_not_called()
    run.assert_not_called()
    assert not (tmp_path / "prefix").exists()


def test_verified_source_builds_required_modules_in_explicit_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as archive:
        content = b"# fixture"
        member = tarfile.TarInfo(f"secp256k1-{builder.COMMIT}/CMakeLists.txt")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    payload = data.getvalue()
    monkeypatch.setattr(builder, "ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest())
    fetch = Mock(return_value=io.BytesIO(payload))
    run = Mock()
    monkeypatch.setattr(builder.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(builder.subprocess, "run", run)

    prefix = tmp_path / "prefix"
    builder.build_library(prefix, tmp_path)
    fetch.assert_called_once_with(builder.ARCHIVE_URL, timeout=60)
    assert (
        tmp_path / f"secp256k1-{builder.COMMIT}/CMakeLists.txt"
    ).read_bytes() == content
    configure = run.call_args_list[0].args[0]
    assert f"-DCMAKE_INSTALL_PREFIX={prefix}" in configure
    assert "-DCMAKE_INSTALL_LIBDIR=lib" in configure
    assert "-DBUILD_SHARED_LIBS=ON" in configure
    for module in ("RECOVERY", "ECDH", "EXTRAKEYS", "SCHNORRSIG", "MUSIG"):
        assert f"-DSECP256K1_ENABLE_MODULE_{module}=ON" in configure
    assert len(run.call_args_list) == 3
    assert run.call_args_list[1].args[0][:2] == ["cmake", "--build"]
    assert run.call_args_list[2].args[0][:2] == ["cmake", "--install"]
    assert all(call.kwargs == {"check": True} for call in run.call_args_list)

    # A cached archive remains authenticated and needs no second network fetch.
    builder.build_library(prefix, tmp_path)
    fetch.assert_called_once()
