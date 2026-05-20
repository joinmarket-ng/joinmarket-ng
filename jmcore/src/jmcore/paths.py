"""
Shared path utilities for JoinMarket data directories.

This module provides consistent path handling across all JoinMarket components
(maker, taker, wallet) for data directories, commitment blacklists, and history.
"""

from __future__ import annotations

import os
from pathlib import Path


def get_default_data_dir() -> Path:
    """
    Get the default JoinMarket data directory.

    Returns ~/.joinmarket-ng or $JOINMARKET_DATA_DIR if set.
    Creates the directory if it doesn't exist.

    For compatibility with reference JoinMarket in Docker, users can
    set JOINMARKET_DATA_DIR=/home/jm/.joinmarket-ng to share the same volume.
    """
    env_path = os.getenv("JOINMARKET_DATA_DIR")
    data_dir = Path(env_path) if env_path else Path.home() / ".joinmarket-ng"

    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_commitment_blacklist_path(data_dir: Path | None = None) -> Path:
    """
    Get the path to the commitment blacklist file.

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())

    Returns:
        Path to cmtdata/commitmentlist (compatible with reference JoinMarket)
    """
    if data_dir is None:
        data_dir = get_default_data_dir()

    # Use cmtdata/ subdirectory for commitment data (matches reference implementation)
    cmtdata_dir = data_dir / "cmtdata"
    cmtdata_dir.mkdir(parents=True, exist_ok=True)

    return cmtdata_dir / "commitmentlist"


def get_used_commitments_path(data_dir: Path | None = None) -> Path:
    """
    Get the path to the used commitments file (for takers).

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())

    Returns:
        Path to cmtdata/commitments.json (compatible with reference JoinMarket)
    """
    if data_dir is None:
        data_dir = get_default_data_dir()

    # Use cmtdata/ subdirectory
    cmtdata_dir = data_dir / "cmtdata"
    cmtdata_dir.mkdir(parents=True, exist_ok=True)

    return cmtdata_dir / "commitments.json"


def get_ignored_makers_path(data_dir: Path | None = None) -> Path:
    """
    Get the path to the ignored makers file (for takers).

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())

    Returns:
        Path to ignored_makers.txt
    """
    if data_dir is None:
        data_dir = get_default_data_dir()

    return data_dir / "ignored_makers.txt"


def get_nick_state_path(data_dir: Path | str | None = None, component: str = "") -> Path:
    """
    Get the path to a component's nick state file.

    The nick state file stores the current nick of a running component,
    allowing operators to easily identify the nick and enabling cross-component
    protection (e.g., taker excluding own maker nick from peer selection).

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())
        component: Component name (e.g., 'maker', 'taker', 'directory', 'orderbook')

    Returns:
        Path to state/<component>.nick (e.g., ~/.joinmarket-ng/state/maker.nick)
    """
    if data_dir is None:
        data_dir = get_default_data_dir()
    elif isinstance(data_dir, str):
        data_dir = Path(data_dir)

    # Use state/ subdirectory to keep state files organized
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    return state_dir / f"{component}.nick"


def write_nick_state(data_dir: Path | str | None, component: str, nick: str) -> Path:
    """
    Write a component's nick to its state file.

    Creates the state directory if it doesn't exist.

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())
        component: Component name (e.g., 'maker', 'taker', 'directory', 'orderbook')
        nick: The nick to write (e.g., 'J5XXXXXXXXX')

    Returns:
        Path to the written state file
    """
    path = get_nick_state_path(data_dir, component)
    path.write_text(nick + "\n")
    return path


def read_nick_state(data_dir: Path | str | None, component: str) -> str | None:
    """
    Read a component's nick from its state file.

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())
        component: Component name (e.g., 'maker', 'taker', 'directory', 'orderbook')

    Returns:
        The nick string if file exists and is readable, None otherwise
    """
    if data_dir is None:
        data_dir = get_default_data_dir()
    elif isinstance(data_dir, str):
        data_dir = Path(data_dir)

    path = get_nick_state_path(data_dir, component)
    if path.exists():
        try:
            return path.read_text().strip()
        except OSError:
            return None
    return None


def remove_nick_state(data_dir: Path | str | None, component: str) -> bool:
    """
    Remove a component's nick state file (e.g., on shutdown).

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())
        component: Component name (e.g., 'maker', 'taker', 'directory', 'orderbook')

    Returns:
        True if file was removed, False if it didn't exist or removal failed
    """
    if data_dir is None:
        data_dir = get_default_data_dir()
    elif isinstance(data_dir, str):
        data_dir = Path(data_dir)

    path = get_nick_state_path(data_dir, component)
    if path.exists():
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


def get_wallet_metadata_path(
    data_dir: Path | None = None,
    fingerprint: str | None = None,
) -> Path:
    """
    Get the path to the wallet metadata file (BIP-329 JSONL format).

    This file stores UTXO-level metadata such as frozen state and labels
    using the BIP-329 wallet labels export format (JSON Lines). Each line
    is a JSON object with a ``type``, ``ref``, and optional fields like
    ``spendable`` (for frozen/unfrozen state) and ``label``.

    The BIP-329 format enables interoperability with external wallets like
    Sparrow for coin control and labeling.

    When ``fingerprint`` is supplied (the 8-char hex master-key fingerprint
    exposed as ``WalletService.wallet_fingerprint``) the path is partitioned
    per wallet as ``wallet_metadata_<fingerprint>.jsonl``. This prevents one
    wallet's persisted "used addresses" set and frozen-UTXO state from
    leaking into another wallet that happens to share the same data
    directory. When ``fingerprint`` is ``None`` the legacy shared
    ``wallet_metadata.jsonl`` path is returned so callers that genuinely
    want the shared file (e.g. the one-shot migration that reads the
    pre-partition file) keep working.

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())
        fingerprint: Optional 8-char hex wallet fingerprint. When given the
            returned path is partitioned per wallet. Only ``[0-9a-f]``
            characters are accepted; any other value is rejected to keep
            the filename safe.

    Returns:
        Path to ``wallet_metadata.jsonl`` (or ``wallet_metadata_<fp>.jsonl``).
    """
    if data_dir is None:
        data_dir = get_default_data_dir()

    if fingerprint is None:
        return data_dir / "wallet_metadata.jsonl"

    safe_fp = fingerprint.strip().lower()
    if not safe_fp or any(c not in "0123456789abcdef" for c in safe_fp):
        # Refuse to compose an unsafe filename. Fall back to the shared
        # path so the caller still gets a usable file rather than
        # crashing; this matches the legacy behavior and is logged at
        # the call site if it ever happens (the fingerprint comes from
        # HDKey.fingerprint.hex() which is always lowercase hex).
        return data_dir / "wallet_metadata.jsonl"

    return data_dir / f"wallet_metadata_{safe_fp}.jsonl"


def get_all_nick_states(data_dir: Path | str | None = None) -> dict[str, str]:
    """
    Read all component nick state files from the data directory.

    Useful for discovering all running components and their nicks.

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())

    Returns:
        Dict mapping component names to their nicks (e.g., {'maker': 'J5XXX', 'taker': 'J5YYY'})
    """
    if data_dir is None:
        data_dir = get_default_data_dir()
    elif isinstance(data_dir, str):
        data_dir = Path(data_dir)

    state_dir = data_dir / "state"
    if not state_dir.exists():
        return {}

    result: dict[str, str] = {}
    for path in state_dir.glob("*.nick"):
        component = path.stem  # e.g., 'maker' from 'maker.nick'
        try:
            nick = path.read_text().strip()
            if nick:
                result[component] = nick
        except OSError:
            continue

    return result
