"""Private mnemonic sidecars and explicit, public wallet identity selection.

Legacy fingerprints are only cache hints. Registration never implies recovery
is needed, and deriving an identity never changes the selected wallet.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, ValidationError, field_validator

from jmcore.secure_files import atomic_write_private

IDENTITY_VERSION = 1
UPGRADE_GUIDANCE = (
    "Wallet identity is unconfirmed. Existing wallet and recovery data are unchanged. "
    "Run `jm-wallet identity register --mnemonic-file <file>` with the intended "
    "BIP39 selection, then `jm-wallet identity select <fingerprint> --mnemonic-file <file>` "
    "to remember it. Use --prompt-bip39-passphrase when needed."
)


class WalletIdentity(BaseModel):
    """A remembered derivation, not a stored credential or authentication proof."""

    fingerprint: str
    bip39: Literal["none", "required", "unknown"]

    @field_validator("fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        value = value.strip().lower()
        if len(value) != 8 or any(c not in "0123456789abcdef" for c in value):
            raise ValueError("Wallet fingerprint must be exactly 8 hex characters")
        return value


def meta_path(mnemonic_file: Path) -> Path:
    return mnemonic_file.with_name(mnemonic_file.name + ".meta")


@contextmanager
def metadata_lock(mnemonic_file: Path, *, nonblocking: bool = False) -> Iterator[None]:
    path = meta_path(mnemonic_file)
    lock_path = path.with_name(path.name + (".recovery.lock" if nonblocking else ".lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("Mnemonic metadata lock must be a regular file")
        os.fchmod(fd, 0o600)
        if sys.platform == "win32":  # pragma: no cover - Windows
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK if nonblocking else msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield
    finally:
        os.close(fd)


def load_mnemonic_meta(mnemonic_file: Path, *, strict: bool = False) -> dict[str, Any]:
    """Keep legacy best-effort reads, but never repair corruption during a write."""
    try:
        data = json.loads(meta_path(mnemonic_file).read_text())
        if not isinstance(data, dict):
            raise ValueError("Mnemonic metadata must be a JSON object")
        if strict:
            _check_version(data)
        return data
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        if strict:
            raise ValueError(
                "Wallet metadata is unreadable or unsupported; restore or repair it explicitly. "
                "It has not been replaced. Use --wallet-fingerprint for offline reads."
            ) from exc
        logger.warning("Failed to read mnemonic metadata")
        return {}


def _check_version(meta: dict[str, Any]) -> None:
    if "identity_version" not in meta:
        if "identities" in meta or "selected_identity" in meta:
            raise ValueError("Partial wallet identity metadata has no supported version")
        return
    version = meta["identity_version"]
    if type(version) is not int or version != IDENTITY_VERSION:
        raise ValueError("Unsupported wallet identity metadata version")


def write_mnemonic_meta(mnemonic_file: Path, meta: dict[str, Any]) -> None:
    _check_version(meta)
    atomic_write_private(meta_path(mnemonic_file), (json.dumps(meta, indent=2) + "\n").encode())


def registered_identities(mnemonic_file: Path) -> dict[str, WalletIdentity]:
    meta = load_mnemonic_meta(mnemonic_file, strict=True)
    return _registered_identities(meta)


def _registered_identities(meta: dict[str, Any]) -> dict[str, WalletIdentity]:
    if meta.get("identity_version") != IDENTITY_VERSION:
        return {}
    entries = meta.get("identities", {})
    if not isinstance(entries, dict):
        raise ValueError("Invalid wallet identity collection; repair metadata explicitly")
    identities: dict[str, WalletIdentity] = {}
    for fingerprint, entry in entries.items():
        try:
            if not isinstance(entry, dict):
                raise ValueError("Invalid identity")
            identity = WalletIdentity.model_validate(
                {"fingerprint": fingerprint, "bip39": entry.get("bip39")}
            )
        except (ValueError, ValidationError):
            logger.warning("Ignoring an invalid wallet identity entry")
            continue
        identities[identity.fingerprint] = identity
    return identities


def selected_identity(mnemonic_file: Path) -> WalletIdentity | None:
    meta = load_mnemonic_meta(mnemonic_file, strict=True)
    selection = meta.get("selected_identity")
    if selection is None:
        if meta.get("identity_version") == IDENTITY_VERSION:
            raise ValueError(
                "No wallet identity selected. Run `jm-wallet identity list`, then "
                "`jm-wallet identity select <fingerprint> --mnemonic-file <file>`."
            )
        return None
    if not isinstance(selection, str):
        raise ValueError("Invalid selected wallet identity; repair metadata explicitly")
    identity = _registered_identities(meta).get(selection)
    if identity is None:
        raise ValueError(
            "Selected wallet identity is unavailable; select a valid identity explicitly"
        )
    return identity


def register_identity(mnemonic_file: Path, identity: WalletIdentity) -> None:
    """Register only, preserving selection, unknown fields, and recovery state."""
    with metadata_lock(mnemonic_file):
        meta = load_mnemonic_meta(mnemonic_file, strict=True)
        entries = meta.get("identities", {})
        if not isinstance(entries, dict):
            raise ValueError("Invalid wallet identity collection; repair metadata explicitly")
        old = entries.get(identity.fingerprint)
        if isinstance(old, dict) and old.get("bip39") not in (identity.bip39, "unknown"):
            raise ValueError("Identity already registered with a different passphrase requirement")
        entries[identity.fingerprint] = {"bip39": identity.bip39}
        meta.update(identity_version=IDENTITY_VERSION, identities=entries)
        write_mnemonic_meta(mnemonic_file, meta)


def select_identity(mnemonic_file: Path, fingerprint: str) -> None:
    with metadata_lock(mnemonic_file):
        meta = load_mnemonic_meta(mnemonic_file, strict=True)
        identity = _registered_identities(meta).get(fingerprint.strip().lower())
        if identity is None:
            raise ValueError("Wallet identity is not registered")
        meta["selected_identity"] = identity.fingerprint
        # Compatibility hint for older releases. New readers never let this
        # mutable legacy field override an explicitly selected identity.
        meta["fingerprint"] = identity.fingerprint
        write_mnemonic_meta(mnemonic_file, meta)
