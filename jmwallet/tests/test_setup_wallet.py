"""Setup publication records history without inferring current wallet freshness."""

from __future__ import annotations

import errno
import json
import stat
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import Mock

import pytest
from jmcore import secure_files
from jmcore.secure_files import exclusive_file_lock
from jmcore.wallet_metadata import meta_path
from pydantic import SecretStr

from jmwallet import setup_wallet
from jmwallet.cli.mnemonic import load_mnemonic_file
from jmwallet.setup_wallet import (
    SetupWalletError,
    SetupWalletIncompleteError,
    publish_setup_wallet,
)

PHRASE = " ".join(["all"] * 12)
PASSWORD = SecretStr("private-test-password")


@pytest.fixture(autouse=True)
def predictable_generation(monkeypatch: pytest.MonkeyPatch) -> Mock:
    generate = Mock(return_value=PHRASE)
    monkeypatch.setattr(setup_wallet, "generate_mnemonic_secure", generate)
    return generate


def journal(path: Path) -> dict[str, Any]:
    return json.loads(path.with_name(path.name + ".setup.json").read_text())


@pytest.mark.parametrize("operation", ["generate", "import"])
def test_complete_encrypted_publication_and_historical_resume(
    tmp_path: Path,
    predictable_generation: Mock,
    operation: Literal["generate", "import"],
) -> None:
    path = tmp_path / "wallet"
    kwargs: dict[str, Any] = {"mnemonic": SecretStr(PHRASE)} if operation == "import" else {}
    result = publish_setup_wallet(path, PASSWORD, operation=operation, **kwargs)
    assert result.complete and result.newly_created is (operation == "generate")
    assert result.mnemonic.get_secret_value() == PHRASE
    assert load_mnemonic_file(path, PASSWORD.get_secret_value()) == PHRASE
    assert PHRASE.encode() not in path.read_bytes()
    assert journal(path)["stage"] == "completed"
    assert json.loads(meta_path(path).read_text())["fidelity_bond_recovery"] == (
        "not_required" if operation == "generate" else "pending"
    )
    for text in (repr(result), repr(path.read_bytes()), json.dumps(journal(path))):
        assert PHRASE not in text and PASSWORD.get_secret_value() not in text
    before = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    resumed = publish_setup_wallet(path, PASSWORD, operation=operation, resume=True)
    assert resumed.complete and not resumed.newly_created
    assert before == {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    assert predictable_generation.call_count == (1 if operation == "generate" else 0)


@pytest.mark.parametrize(
    "artifact",
    ["wallet", "wallet.meta", "wallet.setup.json", "wallet.meta.lock", "wallet.meta.recovery.lock"],
)
def test_existing_artifacts_prevent_creation_without_replacement(
    tmp_path: Path,
    predictable_generation: Mock,
    artifact: str,
) -> None:
    existing = tmp_path / artifact
    existing.write_bytes(b"preserved existing artifact")
    before = existing.stat().st_ino
    with pytest.raises(SetupWalletError, match="already exists"):
        publish_setup_wallet(tmp_path / "wallet", PASSWORD, operation="generate")
    assert existing.read_bytes() == b"preserved existing artifact"
    assert existing.stat().st_ino == before
    predictable_generation.assert_not_called()


def test_refused_existing_wallet_does_not_chmod_its_parent(tmp_path: Path) -> None:
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    path = directory / "wallet"
    path.write_bytes(b"existing wallet")
    before = stat.S_IMODE(directory.stat().st_mode)
    with pytest.raises(SetupWalletError, match="already exists"):
        publish_setup_wallet(path, PASSWORD, operation="generate")
    assert stat.S_IMODE(directory.stat().st_mode) == before
    assert path.read_bytes() == b"existing wallet"


def test_platform_without_directory_durability_is_refused_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(setup_wallet, "os", SimpleNamespace(name="nt"))
    with pytest.raises(SetupWalletError, match="requires POSIX"):
        publish_setup_wallet(tmp_path / "wallet", PASSWORD, operation="generate")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("artifact", ["wallet", "wallet.meta", "wallet.setup.json"])
def test_existing_aliases_including_dangling_are_never_replaced(
    tmp_path: Path, artifact: str
) -> None:
    existing = tmp_path / artifact
    destination = tmp_path / "missing"
    existing.symlink_to(destination)
    with pytest.raises(SetupWalletError):
        publish_setup_wallet(tmp_path / "wallet", PASSWORD, operation="generate")
    assert existing.is_symlink() and not destination.exists()


@pytest.mark.parametrize("boundary", ["intent", "prepared", "wallet", "metadata", "completed"])
@pytest.mark.parametrize("after", [False, True])
def test_interrupted_writes_never_regenerate_or_claim_freshness_on_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    predictable_generation: Mock,
    boundary: str,
    after: bool,
) -> None:
    path = tmp_path / "wallet"
    real = setup_wallet.atomic_write_private

    def write(target: Path, contents: bytes, **kwargs: Any) -> None:
        stage = (
            json.loads(contents)["stage"]
            if target.name.endswith(".setup.json")
            else "metadata"
            if target == meta_path(path)
            else "wallet"
        )
        if stage == boundary and not after:
            raise OSError(errno.EIO, "private-test-password")
        real(target, contents, **kwargs)
        if stage == boundary and after:
            raise OSError(errno.EIO, "private-test-password")

    monkeypatch.setattr(setup_wallet, "atomic_write_private", write)
    with pytest.raises(SetupWalletIncompleteError) as error:
        publish_setup_wallet(path, PASSWORD, operation="generate")
    assert PASSWORD.get_secret_value() not in "".join(traceback.format_exception(error.value))
    calls = predictable_generation.call_count
    monkeypatch.setattr(setup_wallet, "atomic_write_private", real)
    if not path.exists():
        with pytest.raises(SetupWalletError):
            publish_setup_wallet(path, PASSWORD, operation="generate", resume=True)
    else:
        resumed = publish_setup_wallet(path, PASSWORD, operation="generate", resume=True)
        assert not resumed.newly_created
        assert resumed.complete is meta_path(path).exists()
        if not resumed.complete:
            assert not meta_path(path).exists()  # No fresh-generation backfill.
    assert predictable_generation.call_count == calls


def test_every_file_and_directory_flush_boundary_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    predictable_generation: Mock,
) -> None:
    real = secure_files.os.fsync
    calls = 0

    def count(fd: int) -> None:
        nonlocal calls
        calls += 1
        real(fd)

    monkeypatch.setattr(secure_files.os, "fsync", count)
    publish_setup_wallet(tmp_path / "baseline" / "wallet", PASSWORD, operation="generate")
    boundaries = calls
    assert boundaries > 10
    for failure_index in range(1, boundaries + 1):
        path = tmp_path / f"failure-{failure_index}" / "wallet"
        current = 0

        def fail_once(fd: int) -> None:
            nonlocal current
            current += 1
            if current == failure_index:
                raise OSError(errno.EIO, "flush failed")
            real(fd)

        monkeypatch.setattr(secure_files.os, "fsync", fail_once)
        with pytest.raises(SetupWalletIncompleteError):
            publish_setup_wallet(path, PASSWORD, operation="generate")
        calls_before = predictable_generation.call_count
        monkeypatch.setattr(secure_files.os, "fsync", real)
        if not path.exists():
            with pytest.raises(SetupWalletError):
                publish_setup_wallet(path, PASSWORD, operation="generate", resume=True)
        else:
            assert not publish_setup_wallet(
                path, PASSWORD, operation="generate", resume=True
            ).newly_created
        assert predictable_generation.call_count == calls_before


def test_import_metadata_recovery_requires_explicit_confirmation_and_never_overwrites_progress(
    tmp_path: Path,
) -> None:
    path = tmp_path / "wallet"
    publish_setup_wallet(path, PASSWORD, operation="import", mnemonic=SecretStr(PHRASE))
    meta_path(path).unlink()
    assert not publish_setup_wallet(path, PASSWORD, operation="import", resume=True).complete
    assert not meta_path(path).exists()
    recovered = publish_setup_wallet(
        path,
        PASSWORD,
        operation="import",
        resume=True,
        confirm_import_recovery=True,
    )
    assert recovered.complete and not recovered.newly_created
    changed = b'{"fidelity_bond_recovery":"started"}'
    meta_path(path).write_bytes(changed)
    incomplete = publish_setup_wallet(
        path,
        PASSWORD,
        operation="import",
        resume=True,
        confirm_import_recovery=True,
    )
    assert not incomplete.complete and meta_path(path).read_bytes() == changed


@pytest.mark.parametrize(
    "problem",
    ["missing-version", "version", "partial", "target", "operation", "wallet", "password"],
)
def test_resume_rejects_unknown_or_mismatched_evidence_without_repair(
    tmp_path: Path, problem: str
) -> None:
    path = tmp_path / "wallet"
    publish_setup_wallet(path, PASSWORD, operation="generate")
    record = journal(path)
    if problem == "missing-version":
        del record["version"]
    elif problem == "version":
        record["version"] = 2
    elif problem == "partial":
        del record["ciphertext_sha256"]
    elif problem == "target":
        record["target"] = str(tmp_path / "other")
    elif problem == "operation":
        record["operation"] = "import"
    elif problem == "wallet":
        path.write_bytes(b"different encrypted wallet")
    journal_path = path.with_name(path.name + ".setup.json")
    journal_path.write_text(json.dumps(record))
    before = {file.name: file.read_bytes() for file in tmp_path.iterdir()}
    password = SecretStr("wrong password") if problem == "password" else PASSWORD
    with pytest.raises(SetupWalletError):
        publish_setup_wallet(path, password, operation="generate", resume=True)
    assert before == {file.name: file.read_bytes() for file in tmp_path.iterdir()}


def test_stable_lock_refuses_other_setup_owners_before_key_generation(
    tmp_path: Path,
    predictable_generation: Mock,
) -> None:
    path = tmp_path / "wallet"
    with exclusive_file_lock(tmp_path / "wallet.setup.lock"):
        with pytest.raises(SetupWalletError):
            publish_setup_wallet(path, PASSWORD, operation="generate")
    predictable_generation.assert_not_called()
    assert not path.exists()


def test_parallel_setup_has_only_one_publication(tmp_path: Path) -> None:
    path = tmp_path / "wallet"
    ready = Barrier(2)

    def create(_: int) -> bool:
        ready.wait(timeout=3)
        try:
            return publish_setup_wallet(path, PASSWORD, operation="generate").complete
        except SetupWalletError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, (0, 1))) == [False, True]
    assert load_mnemonic_file(path, PASSWORD.get_secret_value()) == PHRASE


def test_blank_password_and_implicit_import_are_rejected_before_writes(tmp_path: Path) -> None:
    path = tmp_path / "wallet"
    with pytest.raises(SetupWalletError):
        publish_setup_wallet(path, SecretStr(""), operation="generate")
    with pytest.raises(SetupWalletError):
        publish_setup_wallet(path, PASSWORD, operation="import")
    assert list(tmp_path.iterdir()) == []
