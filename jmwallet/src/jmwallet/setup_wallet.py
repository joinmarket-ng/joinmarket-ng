"""Encrypted setup-wallet publication with explicit origin and conservative resume.

The journal records publication history, not authentication, current emptiness,
recovery completeness, or permission to activate a market. Only an uninterrupted
fresh generation can return the in-memory ``newly_created`` witness.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from jmcore.secure_files import (
    atomic_write_private,
    ensure_sensitive_directory,
    exclusive_file_lock,
    read_private_file,
    sync_parent_directories,
)
from jmcore.wallet_metadata import encode_mnemonic_meta, meta_path
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from jmwallet.cli.mnemonic import (
    FIDELITY_BOND_RECOVERY_NOT_REQUIRED,
    FIDELITY_BOND_RECOVERY_PENDING,
    decrypt_mnemonic,
    encrypt_mnemonic,
    generate_mnemonic_secure,
    validate_mnemonic,
)


class SetupWalletError(Exception):
    """Setup cannot safely create or resume the requested wallet."""


class SetupWalletIncompleteError(SetupWalletError):
    """Publication may have happened; retain the wallet and journal for inspection."""


class _WalletJournal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(strict=True, ge=1, le=1)
    target: str
    operation: Literal["generate", "import"]
    stage: Literal["intent", "prepared", "completed"]
    ciphertext_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    metadata_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _stage_evidence(self) -> Self:
        hashes = (self.ciphertext_sha256, self.metadata_sha256)
        if self.stage == "intent":
            if hashes != (None, None):
                raise ValueError("intent contains partial prepared evidence")
        elif any(value is None for value in hashes):
            raise ValueError("prepared publication requires complete digests")
        return self


@dataclass(frozen=True)
class SetupWalletPublication:
    path: Path
    mnemonic: SecretStr
    origin: Literal["generated", "imported"]
    newly_created: bool
    complete: bool


def _present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _target(path: Path) -> Path:
    # Resolve parent aliases, never the final wallet name. An existing final
    # alias (even dangling) is not an unused output name.
    return path.expanduser().parent.resolve(strict=False) / path.name


def _journal_path(target: Path) -> Path:
    return target.with_name(target.name + ".setup.json")


def _metadata(operation: Literal["generate", "import"]) -> bytes:
    policy = (
        FIDELITY_BOND_RECOVERY_NOT_REQUIRED
        if operation == "generate"
        else FIDELITY_BOND_RECOVERY_PENDING
    )
    return encode_mnemonic_meta({"fidelity_bond_recovery": policy})


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _save_journal(target: Path, record: _WalletJournal, *, create: bool = False) -> None:
    path = _journal_path(target)
    atomic_write_private(path, (record.model_dump_json() + "\n").encode(), replace=not create)
    sync_parent_directories(path)


def _load_journal(target: Path, operation: str) -> _WalletJournal:
    try:
        record = _WalletJournal.model_validate_json(read_private_file(_journal_path(target)))
    except (OSError, ValueError):
        raise SetupWalletError(
            "setup journal is missing, unreadable, or unsupported; preserved"
        ) from None
    if record.target != str(target) or record.operation != operation:
        raise SetupWalletError("setup journal does not match this target and operation")
    return record


def _resume(
    target: Path,
    password: SecretStr,
    operation: Literal["generate", "import"],
    *,
    confirm_import_recovery: bool,
) -> SetupWalletPublication:
    record = _load_journal(target, operation)
    if not _present(target) or record.stage == "intent":
        raise SetupWalletIncompleteError(
            "wallet publication is unknown; refusing implicit regeneration"
        )
    try:
        encrypted = read_private_file(target)
        if _digest(encrypted) != record.ciphertext_sha256:
            raise ValueError("published wallet differs")
        mnemonic = decrypt_mnemonic(encrypted, password.get_secret_value())
        if not validate_mnemonic(mnemonic):
            raise ValueError("invalid mnemonic")
        sync_parent_directories(target)
        expected_metadata = _metadata(operation)
        if _digest(expected_metadata) != record.metadata_sha256:
            raise ValueError("prepared metadata format differs")
        metadata = meta_path(target)
        if not _present(metadata) and operation == "import" and confirm_import_recovery:
            atomic_write_private(metadata, expected_metadata, replace=False)
        matches = (
            _present(metadata) and _digest(read_private_file(metadata)) == record.metadata_sha256
        )
        if matches:
            sync_parent_directories(metadata)
            _save_journal(target, record.model_copy(update={"stage": "completed"}))
    except (OSError, ValueError):
        raise SetupWalletIncompleteError(
            "wallet publication cannot be verified; retain files and journal"
        ) from None
    return SetupWalletPublication(
        target,
        SecretStr(mnemonic),
        "generated" if operation == "generate" else "imported",
        newly_created=False,
        complete=matches,
    )


def publish_setup_wallet(
    path: Path,
    password: SecretStr,
    *,
    operation: Literal["generate", "import"],
    mnemonic: SecretStr | None = None,
    word_count: int = 24,
    resume: bool = False,
    confirm_import_recovery: bool = False,
) -> SetupWalletPublication:
    """Publish an explicitly selected new encrypted wallet or resume its journal.

    Other wallet/metadata writers must remain stopped for this transaction;
    the stable nonblocking setup lock excludes cooperating setup invocations.
    Existing-wallet use belongs to the ordinary unlock path, not this API.
    No absence or stored origin proves that a wallet has never been used.
    Durable publication currently requires a supported POSIX filesystem. New
    directories are private; existing directory modes remain unchanged. Resume
    reads may tighten file permissions through the existing private-reader API.

    Resume never generates keys or fills in fresh-generation metadata. It may
    recover a matching publication, returning ``newly_created=False``. Missing
    import metadata requires explicit recovery confirmation before publication;
    changed metadata is preserved and returns ``complete=False``.
    """
    if os.name != "posix":
        raise SetupWalletError("durable setup wallet publication currently requires POSIX")
    if operation not in {"generate", "import"}:
        raise SetupWalletError("select explicit wallet generation or import")
    if not isinstance(password, SecretStr) or not password.get_secret_value():
        raise SetupWalletError("setup wallet publication requires a nonempty encryption password")
    if resume and mnemonic is not None:
        raise SetupWalletError("resume uses the recorded wallet, not a new mnemonic")
    if not resume:
        if operation == "generate" and mnemonic is not None:
            raise SetupWalletError("generation cannot accept an imported mnemonic")
        if operation == "import" and (
            mnemonic is None or not validate_mnemonic(mnemonic.get_secret_value())
        ):
            raise SetupWalletError("explicit import requires a valid BIP39 mnemonic")
        if operation == "generate" and word_count not in {12, 15, 18, 21, 24}:
            raise SetupWalletError("unsupported BIP39 word count")
    try:
        target = _target(path)
        ensure_sensitive_directory(target.parent)
        lock = target.with_name(target.name + ".setup.lock")
        with exclusive_file_lock(lock, blocking=False):
            if resume:
                return _resume(
                    target,
                    password,
                    operation,
                    confirm_import_recovery=confirm_import_recovery,
                )
            metadata = meta_path(target)
            artifacts = (
                target,
                metadata,
                _journal_path(target),
                metadata.with_name(metadata.name + ".lock"),
                metadata.with_name(metadata.name + ".recovery.lock"),
            )
            if any(_present(artifact) for artifact in artifacts):
                raise SetupWalletError(
                    "wallet or sidecar already exists; use existing wallet or explicit resume"
                )
            return _publish_new(target, password, operation, mnemonic, word_count)
    except SetupWalletError:
        raise
    except Exception:
        # File operations and crypto errors may embed secret inputs or paths.
        # Even a reported fsync error can follow successful publication.
        raise SetupWalletIncompleteError(
            "publication outcome incomplete; retain wallet and journal, inspect or resume"
        ) from None


def _publish_new(
    target: Path,
    password: SecretStr,
    operation: Literal["generate", "import"],
    imported: SecretStr | None,
    word_count: int,
) -> SetupWalletPublication:
    record = _WalletJournal(version=1, target=str(target), operation=operation, stage="intent")
    _save_journal(target, record, create=True)
    # No new entropy or encryption occurs before the durable intent. A failed
    # intent/prepared attempt is never regenerated implicitly on the next call.
    phrase = (
        generate_mnemonic_secure(word_count)
        if operation == "generate"
        else imported.get_secret_value()
        if imported is not None
        else ""
    )
    if not validate_mnemonic(phrase):
        raise SetupWalletIncompleteError(
            "wallet phrase could not be verified; retain setup journal"
        )
    encrypted = encrypt_mnemonic(phrase, password.get_secret_value())
    metadata = _metadata(operation)
    record = record.model_copy(
        update={
            "stage": "prepared",
            "ciphertext_sha256": _digest(encrypted),
            "metadata_sha256": _digest(metadata),
        }
    )
    _save_journal(target, record)
    atomic_write_private(target, encrypted, replace=False)
    atomic_write_private(meta_path(target), metadata, replace=False)
    _save_journal(target, record.model_copy(update={"stage": "completed"}))
    return SetupWalletPublication(
        target,
        SecretStr(phrase),
        "generated" if operation == "generate" else "imported",
        newly_created=operation == "generate",
        complete=True,
    )
