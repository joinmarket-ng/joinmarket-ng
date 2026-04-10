"""
PoDLE commitment blacklist for preventing commitment reuse.

When a PoDLE commitment is used in a CoinJoin (whether successful or failed),
it should be blacklisted to prevent reuse. This module provides persistence
and checking of the commitment blacklist.

The blacklist is shared across the JoinMarket network via !hp2 messages.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path

from loguru import logger

from jmcore.paths import get_commitment_blacklist_path

# PoDLE commitments are SHA256 hashes of an EC point, encoded as hex.
# That means exactly 64 hex characters (32 bytes).
COMMITMENT_HEX_LENGTH = 64

_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


def validate_commitment_hex(commitment: str) -> tuple[bool, str]:
    """Validate that a commitment string is well-formed hex of the correct length.

    A valid commitment (after prefix stripping) must be exactly 64
    hex characters representing 32 bytes (SHA256 output).

    Args:
        commitment: The raw commitment string (prefix already stripped).

    Returns:
        A ``(valid, error_message)`` tuple.  When valid is True the
        error_message is the empty string.
    """
    if not commitment:
        return False, "empty commitment"

    if len(commitment) != COMMITMENT_HEX_LENGTH:
        return False, (
            f"invalid commitment length {len(commitment)}, expected {COMMITMENT_HEX_LENGTH}"
        )

    if not _HEX_RE.match(commitment):
        return False, "commitment contains non-hex characters"

    return True, ""


class CommitmentBlacklist:
    """
    Thread-safe commitment blacklist with file persistence.

    The blacklist is stored as a simple text file with one commitment per line.
    This matches the reference implementation's format for compatibility.
    """

    def __init__(self, blacklist_path: Path | None = None, data_dir: Path | None = None):
        """
        Initialize the commitment blacklist.

        Args:
            blacklist_path: Path to the blacklist file. If None, uses data_dir.
            data_dir: Data directory for JoinMarket (defaults to get_default_data_dir()).
                     Only used if blacklist_path is None.
        """
        if blacklist_path is None:
            blacklist_path = get_commitment_blacklist_path(data_dir)
        self.blacklist_path = blacklist_path

        # In-memory cache of blacklisted commitments
        self._commitments: set[str] = set()
        self._lock = threading.Lock()

        # Load existing blacklist from disk
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        """Load blacklist from disk into memory.

        Commitments are normalized to lowercase on load to ensure consistent
        case-insensitive matching. This is important because hex-encoded
        commitments are case-insensitive by nature ('ABCD' == 'abcd'), and
        files written by the reference implementation may contain mixed-case
        entries.
        """
        if not self.blacklist_path.exists():
            logger.debug(f"No existing blacklist at {self.blacklist_path}")
            return

        try:
            with open(self.blacklist_path, encoding="ascii") as f:
                for line in f:
                    commitment = line.strip().lower()
                    if commitment:
                        self._commitments.add(commitment)
            logger.info(f"Loaded {len(self._commitments)} commitments from blacklist")
        except Exception as e:
            logger.error(f"Failed to load blacklist from {self.blacklist_path}: {e}")

    def _save_to_disk(self) -> None:
        """Save in-memory blacklist to disk."""
        try:
            # Ensure parent directory exists
            self.blacklist_path.parent.mkdir(parents=True, exist_ok=True)

            with open(self.blacklist_path, "w", encoding="ascii") as f:
                for commitment in sorted(self._commitments):
                    f.write(commitment + "\n")
                f.flush()
            logger.debug(f"Saved {len(self._commitments)} commitments to blacklist")
        except Exception as e:
            logger.error(f"Failed to save blacklist to {self.blacklist_path}: {e}")

    def is_blacklisted(self, commitment: str) -> bool:
        """
        Check if a commitment is blacklisted.

        Args:
            commitment: The commitment hash (hex string, typically 64 chars)

        Returns:
            True if the commitment is blacklisted, False otherwise
        """
        # Normalize commitment (strip whitespace, lowercase)
        commitment = commitment.strip().lower()

        with self._lock:
            return commitment in self._commitments

    def add(self, commitment: str, persist: bool = True) -> bool:
        """
        Add a commitment to the blacklist.

        Args:
            commitment: The commitment hash (hex string)
            persist: If True, save to disk immediately

        Returns:
            True if the commitment was newly added, False if already present
        """
        # Normalize commitment
        commitment = commitment.strip().lower()

        if not commitment:
            logger.warning("Attempted to add empty commitment to blacklist")
            return False

        with self._lock:
            if commitment in self._commitments:
                return False

            self._commitments.add(commitment)
            logger.debug(f"Added commitment to blacklist: {commitment[:16]}...")

            if persist:
                self._save_to_disk()

            return True

    def check_and_add(self, commitment: str, persist: bool = True) -> bool:
        """
        Check if a commitment is blacklisted, and if not, add it.

        This is the primary method for handling commitments during CoinJoin.
        It atomically checks and adds in a single operation.

        Args:
            commitment: The commitment hash (hex string)
            persist: If True, save to disk immediately after adding

        Returns:
            True if the commitment is NEW (allowed), False if already blacklisted
        """
        # Normalize commitment
        commitment = commitment.strip().lower()

        if not commitment:
            logger.warning("Attempted to check empty commitment")
            return False

        with self._lock:
            if commitment in self._commitments:
                logger.info(f"Commitment already blacklisted: {commitment[:16]}...")
                return False

            self._commitments.add(commitment)
            logger.debug(f"Added commitment to blacklist: {commitment[:16]}...")

            if persist:
                self._save_to_disk()

            return True

    def __len__(self) -> int:
        """Return the number of blacklisted commitments."""
        with self._lock:
            return len(self._commitments)

    def __contains__(self, commitment: str) -> bool:
        """Check if a commitment is blacklisted using 'in' operator."""
        return self.is_blacklisted(commitment)


# Global singleton instance (initialized lazily)
_global_blacklist: CommitmentBlacklist | None = None
_global_blacklist_lock = threading.Lock()


def get_blacklist(
    blacklist_path: Path | None = None, data_dir: Path | None = None
) -> CommitmentBlacklist:
    """
    Get the global commitment blacklist instance.

    Args:
        blacklist_path: Path to the blacklist file. Only used on first call
                       to initialize the singleton.
        data_dir: Data directory for JoinMarket. Only used on first call
                 to initialize the singleton.

    Returns:
        The global CommitmentBlacklist instance
    """
    global _global_blacklist

    with _global_blacklist_lock:
        if _global_blacklist is None:
            _global_blacklist = CommitmentBlacklist(blacklist_path, data_dir)
        return _global_blacklist


def set_blacklist_path(blacklist_path: Path | None = None, data_dir: Path | None = None) -> None:
    """
    Set the path for the global blacklist.

    Must be called before any blacklist operations. If the blacklist
    has already been initialized, this will reinitialize it with the new path.

    Args:
        blacklist_path: Explicit path to blacklist file
        data_dir: Data directory (used if blacklist_path is None)
    """
    global _global_blacklist

    with _global_blacklist_lock:
        _global_blacklist = CommitmentBlacklist(blacklist_path, data_dir)
        logger.info(f"Set blacklist path to {_global_blacklist.blacklist_path}")


def check_commitment(commitment: str) -> bool:
    """
    Check if a commitment is allowed (not blacklisted).

    Convenience function that uses the global blacklist.

    Args:
        commitment: The commitment hash (hex string)

    Returns:
        True if the commitment is allowed, False if blacklisted
    """
    return not get_blacklist().is_blacklisted(commitment)


def add_commitment(commitment: str, persist: bool = True) -> bool:
    """
    Add a commitment to the global blacklist.

    Convenience function that uses the global blacklist.

    Args:
        commitment: The commitment hash (hex string)
        persist: If True, save to disk immediately

    Returns:
        True if the commitment was newly added, False if already present
    """
    return get_blacklist().add(commitment, persist=persist)


def check_and_add_commitment(commitment: str, persist: bool = True) -> bool:
    """
    Check if a commitment is allowed and add it to the blacklist.

    Convenience function that uses the global blacklist.
    This is the primary function to use during CoinJoin processing.

    Args:
        commitment: The commitment hash (hex string)
        persist: If True, save to disk immediately after adding

    Returns:
        True if the commitment is NEW (allowed), False if already blacklisted
    """
    return get_blacklist().check_and_add(commitment, persist=persist)
