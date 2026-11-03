"""Initialize taker commitment history for a data directory's first generated wallet."""

from __future__ import annotations

import os
from pathlib import Path

from jmcore.paths import create_commitment_history
from loguru import logger

from jmwallet.history import HISTORY_FILENAME, LEGACY_HISTORY_FILENAME


def _has_entries(path: Path, *, ignore_hidden: bool) -> bool:
    if not os.path.lexists(path):
        return False
    if not path.is_dir():
        return True
    return any(not (ignore_hidden and entry.name.startswith(".")) for entry in path.iterdir())


def is_first_wallet_in_data_dir(data_dir: Path, wallet_file: Path) -> bool:
    """Return True if a new wallet file would start an unused data directory.

    Call before saving the wallet. Any other wallet, transaction history or
    commitment data means earlier commitments may exist, so this returns False.
    """
    try:
        wallets_dir = data_dir / "wallets"
        if wallet_file.parent.resolve(strict=False) != wallets_dir.resolve(strict=False):
            return False
        if any(
            os.path.lexists(data_dir / name) for name in (HISTORY_FILENAME, LEGACY_HISTORY_FILENAME)
        ):
            return False
        return not (
            _has_entries(wallets_dir, ignore_hidden=True)
            or _has_entries(data_dir / "cmtdata", ignore_hidden=False)
        )
    except OSError:
        return False


def initialize_commitment_history_for_new_wallet(data_dir: Path) -> None:
    """Create empty commitment history after saving a newly generated first wallet.

    A fresh seed has no commitments to reuse. Failure is logged, not raised:
    the taker still refuses to start until history is initialized manually.
    """
    try:
        create_commitment_history(data_dir)
    except FileExistsError:
        return
    except OSError as exc:
        logger.warning(
            "Could not initialize commitment history; run jm-taker init-commitment-history "
            "before the first CoinJoin"
        )
        logger.bind(sensitive=True).warning(f"Commitment history initialization failed: {exc}")
        return
    logger.info("Initialized empty commitment history for the new wallet")
