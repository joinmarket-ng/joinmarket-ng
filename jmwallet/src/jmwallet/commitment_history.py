"""Initialize taker commitment history for a data directory's first generated wallet."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from jmcore.crypto import mnemonic_to_seed
from jmcore.paths import GENERATED_WALLETS_ONLY, create_commitment_history
from jmcore.secure_files import create_private_file_exclusive, read_private_file
from loguru import logger

from jmwallet.history import HISTORY_FILENAME, LEGACY_HISTORY_FILENAME

_GENERATED_WALLETS_DIR = "generated-wallets"
_GENERATION_PROOF = b"JoinMarket NG freshly generated mnemonic v1\n"


def history_requires_generation_authorization(data_dir: Path, history: dict[str, object]) -> bool:
    """Retain the restriction even if another client rewrites unknown JSON fields."""
    return history.get(GENERATED_WALLETS_ONLY) is True or os.path.lexists(
        data_dir / "cmtdata" / _GENERATED_WALLETS_DIR
    )


def _authorization_path(data_dir: Path, mnemonic: str) -> Path:
    # Bind the entire freshly generated mnemonic family, including identities
    # selected later with a BIP39 passphrase. A four-byte wallet fingerprint is
    # not collision-resistant. Never store the mnemonic or an extended public key.
    identifier = hashlib.sha256(
        b"JoinMarket NG generated commitment history v1\0" + mnemonic_to_seed(mnemonic)
    ).hexdigest()
    return data_dir / "cmtdata" / _GENERATED_WALLETS_DIR / identifier


def generated_wallet_is_authorized(data_dir: Path, mnemonic: str) -> bool:
    """Require durable affirmative generation evidence, never infer it from absence."""
    try:
        path = _authorization_path(data_dir, mnemonic)
        if path.parent.is_symlink():
            return False
        return read_private_file(path) == _GENERATION_PROOF
    except OSError:
        return False


def _has_entries(path: Path, *, ignore: frozenset[str] = frozenset()) -> bool:
    if not os.path.lexists(path):
        return False
    if not path.is_dir():
        return True
    return any(entry.name not in ignore for entry in path.iterdir())


def is_first_wallet_in_data_dir(
    data_dir: Path,
    wallet_file: Path,
    *,
    saved_wallet: bool = False,
    authorization: Path | None = None,
) -> bool:
    """Return True if a new wallet file would start an unused data directory.

    Call before saving and recheck before initialization, allowing only the exact
    saved wallet's own artifacts. Unknown files, including hidden wallets and
    backups, never prove that a directory is unused.
    """
    try:
        wallets_dir = data_dir / "wallets"
        if wallet_file.parent.resolve(strict=False) != wallets_dir.resolve(strict=False):
            return False
        if any(
            os.path.lexists(data_dir / name) for name in (HISTORY_FILENAME, LEGACY_HISTORY_FILENAME)
        ):
            return False
        ignored = {".wallet-operation.lock"}
        if saved_wallet:
            # Ignore only artifacts of this exact generated wallet, not every
            # hidden file or similarly named backup belonging to an older wallet.
            ignored.update(
                {
                    wallet_file.name,
                    wallet_file.name + ".meta",
                    wallet_file.name + ".meta.lock",
                    wallet_file.name + ".meta.recovery.lock",
                }
            )
        if _has_entries(wallets_dir, ignore=frozenset(ignored)):
            return False
        if authorization is None:
            return not _has_entries(data_dir / "cmtdata")
        return not (
            _has_entries(data_dir / "cmtdata", ignore=frozenset({_GENERATED_WALLETS_DIR}))
            or _has_entries(authorization.parent, ignore=frozenset({authorization.name}))
        )
    except OSError:
        return False


def initialize_commitment_history_for_new_wallet(
    data_dir: Path, *, mnemonic: str, wallet_file: Path, first_wallet: bool
) -> None:
    """Authorize a generated mnemonic, initializing only a still-unused directory.

    Existing automatic histories can admit another freshly generated mnemonic
    without rewriting commitments. Imports and recovery must never call this.
    Failure is nonfatal to saving the wallet; it grants no taker authorization.
    """
    try:
        if wallet_file.parent.resolve() != (data_dir / "wallets").resolve():
            return
        if not wallet_file.is_file():
            return
        history_path = data_dir / "cmtdata" / "commitments.json"
        try:
            with history_path.open(encoding="utf-8") as history:
                data = json.load(history)
        except FileNotFoundError:
            if not first_wallet or not is_first_wallet_in_data_dir(
                data_dir, wallet_file, saved_wallet=True
            ):
                return
            create_history = True
        else:
            # Never replace or rewrite an existing history, even to enroll a new
            # seed. Independent proof files avoid racing a taker's history save.
            if not isinstance(data, dict) or not history_requires_generation_authorization(
                data_dir, data
            ):
                return
            create_history = False

        authorization = _authorization_path(data_dir, mnemonic)
        try:
            create_private_file_exclusive(authorization, _GENERATION_PROOF)
        except FileExistsError:
            if not generated_wallet_is_authorized(data_dir, mnemonic):
                raise OSError("Existing generation authorization is invalid") from None
        if not create_history:
            return
        # Recheck after every awaited preparation and after recording our proof.
        # A newly appeared backup or another wallet is not permission to reset.
        if not is_first_wallet_in_data_dir(
            data_dir, wallet_file, saved_wallet=True, authorization=authorization
        ):
            return
        try:
            create_commitment_history(data_dir, generated_wallets_only=True)
        except FileExistsError:
            return
    except (OSError, ValueError, TypeError) as exc:
        logger.warning(
            "Commitment-history setup needs operator action before CoinJoin. "
            "Preserve existing state and follow the taker recovery guidance."
        )
        logger.bind(sensitive=True).warning(f"Commitment history initialization failed: {exc}")
        return
    logger.info("Initialized empty commitment history for the new wallet")
