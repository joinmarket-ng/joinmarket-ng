"""
Fidelity bond registry for persistent storage of bond metadata.

This module provides storage and retrieval of fidelity bond information,
including addresses, locktimes, witness scripts, and UTXO tracking.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import BaseModel

if TYPE_CHECKING:
    from jmwallet.wallet.bip32 import HDKey


class FidelityBondInfo(BaseModel):
    """Information about a single fidelity bond."""

    address: str
    locktime: int
    locktime_human: str
    index: int
    path: str
    pubkey: str
    witness_script_hex: str
    network: str
    created_at: str
    # UTXO info (populated when bond is funded)
    txid: str | None = None
    vout: int | None = None
    value: int | None = None  # in satoshis
    confirmations: int | None = None
    # Certificate info (for cold wallet support)
    # Allows keeping bond UTXO private key in cold storage (hardware wallet)
    # while using a hot wallet certificate key for signing nick proofs
    cert_pubkey: str | None = None  # Hot wallet certificate public key (hex)
    cert_privkey: str | None = None  # Hot wallet certificate private key (hex)
    cert_signature: str | None = None  # Certificate signature by UTXO key (hex)
    cert_expiry: int | None = None  # Certificate expiry in 2016-block periods

    @property
    def is_funded(self) -> bool:
        """Check if this bond has been funded."""
        return self.txid is not None and self.value is not None and self.value > 0

    @property
    def is_expired(self) -> bool:
        """Check if the locktime has passed."""
        import time

        return time.time() >= self.locktime

    @property
    def time_until_unlock(self) -> int:
        """Seconds until the bond can be unlocked. Returns 0 if already expired."""
        import time

        remaining = self.locktime - int(time.time())
        return max(0, remaining)

    @property
    def has_certificate(self) -> bool:
        """Check if this bond has a certificate configured (for cold wallet mode)."""
        return (
            self.cert_pubkey is not None
            and self.cert_privkey is not None
            and self.cert_signature is not None
            and self.cert_expiry is not None
        )

    def is_certificate_expired(self, current_block_height: int) -> bool:
        """
        Check if the certificate has expired based on current block height.

        Args:
            current_block_height: Current blockchain height

        Returns:
            True if certificate is expired or not configured
        """
        if not self.has_certificate or self.cert_expiry is None:
            return True

        # cert_expiry is stored in 2016-block periods
        expiry_height = self.cert_expiry * 2016
        return current_block_height >= expiry_height


class BondRegistry(BaseModel):
    """Registry of all fidelity bonds for a wallet."""

    version: int = 1
    bonds: list[FidelityBondInfo] = []

    def add_bond(self, bond: FidelityBondInfo) -> None:
        """Add a new bond to the registry."""
        # Check for duplicate address
        for existing in self.bonds:
            if existing.address == bond.address:
                logger.warning(f"Bond with address {bond.address} already exists, updating")
                self.bonds.remove(existing)
                break
        self.bonds.append(bond)

    def get_bond_by_address(self, address: str) -> FidelityBondInfo | None:
        """Get a bond by its address."""
        for bond in self.bonds:
            if bond.address == address:
                return bond
        return None

    def get_bond_by_index(self, index: int, locktime: int) -> FidelityBondInfo | None:
        """Get a bond by its index and locktime."""
        for bond in self.bonds:
            if bond.index == index and bond.locktime == locktime:
                return bond
        return None

    def get_funded_bonds(self) -> list[FidelityBondInfo]:
        """Get all funded bonds."""
        return [b for b in self.bonds if b.is_funded]

    def get_active_bonds(self) -> list[FidelityBondInfo]:
        """Get all funded bonds that are not yet expired."""
        return [b for b in self.bonds if b.is_funded and not b.is_expired]

    def get_best_bond(self) -> FidelityBondInfo | None:
        """
        Get the best bond for advertising.

        Selection criteria (in order):
        1. Must be funded
        2. Must not be expired
        3. Highest value wins
        4. If tied, longest locktime remaining wins
        """
        active = self.get_active_bonds()
        if not active:
            return None

        # Sort by value (descending), then by time_until_unlock (descending)
        active.sort(key=lambda b: (b.value or 0, b.time_until_unlock), reverse=True)
        return active[0]

    def update_utxo_info(
        self,
        address: str,
        txid: str,
        vout: int,
        value: int,
        confirmations: int,
    ) -> bool:
        """Update UTXO information for a bond."""
        bond = self.get_bond_by_address(address)
        if bond:
            bond.txid = txid
            bond.vout = vout
            bond.value = value
            bond.confirmations = confirmations
            return True
        return False


LEGACY_REGISTRY_FILENAME = "fidelity_bonds.json"


def _safe_fingerprint(fingerprint: str | None) -> str | None:
    """Normalize a wallet fingerprint to a safe filename component.

    Returns the lowercase hex fingerprint when valid, otherwise ``None``
    so callers fall back to the legacy shared path. The fingerprint must
    be non-empty and contain only ``[0-9a-f]``; ``HDKey.fingerprint.hex()``
    always satisfies this, so an invalid value usually indicates a bug.
    """
    if fingerprint is None:
        return None
    safe = fingerprint.strip().lower()
    if not safe or any(c not in "0123456789abcdef" for c in safe):
        logger.warning(
            f"Rejecting unsafe fidelity-bond registry fingerprint {fingerprint!r}; "
            "falling back to legacy shared path"
        )
        return None
    return safe


def get_legacy_registry_path(data_dir: Path) -> Path:
    """Get the path to the legacy (pre per-wallet) shared registry file."""
    return data_dir / LEGACY_REGISTRY_FILENAME


def list_registry_fingerprints(data_dir: Path) -> list[str]:
    """List wallet fingerprints with a per-wallet bond registry on disk.

    Scans ``data_dir`` for files matching ``fidelity_bonds_<fp>.json`` and
    returns the sorted, lowercased fingerprint components. The legacy
    shared ``fidelity_bonds.json`` is intentionally excluded because it
    is not tied to a specific wallet identity.

    This is used by CLI commands that need to operate on the per-wallet
    registry without forcing the user to provide a mnemonic when only one
    wallet exists in the directory (or to print the available choices
    when several do).
    """
    if not data_dir.exists():
        return []
    fingerprints: list[str] = []
    for path in data_dir.glob("fidelity_bonds_*.json"):
        stem = path.stem
        # ``fidelity_bonds_<fp>``; trim the prefix and validate the
        # remainder as a hex fingerprint so we never surface stray files.
        fp = stem[len("fidelity_bonds_") :]
        if _safe_fingerprint(fp) is not None:
            fingerprints.append(fp.lower())
    return sorted(set(fingerprints))


def get_registry_path(data_dir: Path, fingerprint: str | None = None) -> Path:
    """Get the path to the bond registry file.

    When ``fingerprint`` is supplied (the 8-char hex master-key fingerprint
    exposed as :attr:`jmwallet.wallet.service.WalletService.wallet_fingerprint`)
    the path is partitioned per wallet as ``fidelity_bonds_<fingerprint>.json``.
    This prevents one wallet's persisted bonds from leaking into another
    wallet that happens to share the same data directory (issue #492).

    When ``fingerprint`` is ``None`` the legacy shared
    ``fidelity_bonds.json`` path is returned so callers that genuinely
    want the shared file (e.g. the one-shot migration that reads the
    pre-partition file) keep working.
    """
    safe = _safe_fingerprint(fingerprint)
    if safe is None:
        return get_legacy_registry_path(data_dir)
    return data_dir / f"fidelity_bonds_{safe}.json"


def load_registry(
    data_dir: Path,
    fingerprint: str | None = None,
    *,
    allow_legacy_fallback: bool = True,
) -> BondRegistry:
    """
    Load the bond registry from disk.

    Args:
        data_dir: Data directory path
        fingerprint: Optional 8-char hex wallet fingerprint. When given the
            per-wallet ``fidelity_bonds_<fp>.json`` file is read.
        allow_legacy_fallback: When ``True`` (the default) and a per-wallet
            file is requested but missing, the legacy shared
            ``fidelity_bonds.json`` is read as a **read-only display**
            fallback. This MUST be ``False`` on any code path that will
            subsequently :func:`save_registry` the result back to the
            per-wallet file: the legacy file is not filtered by ownership,
            so persisting it would copy *other wallets'* bonds into this
            wallet's registry (issue #492 regression). Wallet-aware
            migration via :func:`migrate_legacy_registry` is the only safe
            way to move legacy entries into a per-wallet file.

    Behavior:
        If a per-wallet file is requested but does not exist and
        ``allow_legacy_fallback`` is ``True``, the legacy shared
        ``fidelity_bonds.json`` is read so upgrading users still see their
        bonds for display until migration partitions them per wallet. The
        legacy file is **not** filtered here: any bond it contains will
        appear under every wallet's view, which is why writers must pass
        ``allow_legacy_fallback=False`` and rely on migration instead.

    Returns:
        BondRegistry instance (empty if no registry file is found)
    """
    registry_path = get_registry_path(data_dir, fingerprint)
    if not registry_path.exists():
        # Fall back to legacy file when looking up a per-wallet path. This
        # keeps read-only display (`registry-show`, offline `list-bonds`)
        # working immediately after an upgrade, before any wallet open has
        # triggered migration. It is intentionally suppressed on write
        # paths (allow_legacy_fallback=False) because the legacy file is
        # unfiltered and persisting it would leak other wallets' bonds.
        if fingerprint is not None and allow_legacy_fallback:
            legacy_path = get_legacy_registry_path(data_dir)
            if legacy_path.exists():
                registry_path = legacy_path
            else:
                return BondRegistry()
        else:
            return BondRegistry()

    try:
        data = json.loads(registry_path.read_text())
        return BondRegistry.model_validate(data)
    except Exception as e:
        logger.error(f"Failed to load bond registry: {e}")
        # Return empty registry on error, but don't overwrite the file
        return BondRegistry()


def save_registry(registry: BondRegistry, data_dir: Path, fingerprint: str | None = None) -> None:
    """
    Save the bond registry to disk.

    Args:
        registry: BondRegistry instance
        data_dir: Data directory path
        fingerprint: Optional 8-char hex wallet fingerprint. When given the
            registry is written to the per-wallet
            ``fidelity_bonds_<fp>.json`` file.
    """
    registry_path = get_registry_path(data_dir, fingerprint)
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None

    try:
        content = registry.model_dump_json(indent=2)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=registry_path.parent,
            prefix=f"{registry_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fchmod(temp_file.fileno(), 0o600)
            os.fsync(temp_file.fileno())

        os.replace(temp_path, registry_path)
        logger.debug(f"Saved bond registry to {registry_path}")
    except Exception as e:
        logger.error(f"Failed to save bond registry: {e}")
        raise
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def get_active_locktimes(data_dir: Path, fingerprint: str | None = None) -> list[int]:
    """
    Get all locktimes from the bond registry that have funded, active bonds.

    This is useful for the maker bot to automatically discover which locktimes
    to scan for when syncing fidelity bonds, without requiring the user to
    manually specify --fidelity-bond-locktime.

    Args:
        data_dir: Data directory path
        fingerprint: Optional 8-char hex wallet fingerprint to scope the
            lookup to a per-wallet registry file.

    Returns:
        List of unique locktimes (Unix timestamps) for active bonds
    """
    registry = load_registry(data_dir, fingerprint)
    active_bonds = registry.get_active_bonds()
    # Get unique locktimes
    locktimes = list({bond.locktime for bond in active_bonds})
    return sorted(locktimes)


def get_all_locktimes(data_dir: Path, fingerprint: str | None = None) -> list[int]:
    """
    Get all locktimes from the bond registry (funded or not).

    This includes all bonds in the registry to allow scanning for UTXOs
    that may have been funded since the last sync.

    Args:
        data_dir: Data directory path
        fingerprint: Optional 8-char hex wallet fingerprint to scope the
            lookup to a per-wallet registry file.

    Returns:
        List of unique locktimes (Unix timestamps) for all bonds
    """
    registry = load_registry(data_dir, fingerprint)
    # Get unique locktimes from ALL bonds (not just funded ones)
    locktimes = list({bond.locktime for bond in registry.bonds})
    return sorted(locktimes)


def create_bond_info(
    address: str,
    locktime: int,
    index: int,
    path: str,
    pubkey_hex: str,
    witness_script: bytes,
    network: str,
) -> FidelityBondInfo:
    """
    Create a FidelityBondInfo instance.

    Args:
        address: The P2WSH address
        locktime: Unix timestamp locktime
        index: Derivation index
        path: Full derivation path
        pubkey_hex: Public key as hex
        witness_script: The witness script bytes
        network: Network name

    Returns:
        FidelityBondInfo instance
    """
    locktime_dt = datetime.fromtimestamp(locktime)
    return FidelityBondInfo(
        address=address,
        locktime=locktime,
        locktime_human=locktime_dt.strftime("%Y-%m-%d %H:%M:%S"),
        index=index,
        path=path,
        pubkey=pubkey_hex,
        witness_script_hex=witness_script.hex(),
        network=network,
        created_at=datetime.now().isoformat(),
    )


def migrate_legacy_registry(
    data_dir: Path,
    fingerprint: str,
    bond_belongs_to_wallet: Callable[[FidelityBondInfo], bool],
) -> int:
    """
    One-shot migration from the legacy shared ``fidelity_bonds.json`` to a
    per-wallet ``fidelity_bonds_<fingerprint>.json`` file (issue #492).

    Behavior:
        - If the per-wallet file already exists, no migration is performed
          (idempotent on repeated wallet opens).
        - If the legacy file does not exist, no migration is performed.
        - Otherwise the legacy file is read and each entry is offered to
          ``bond_belongs_to_wallet``. Matching entries are written to the
          per-wallet file. The legacy file is rewritten with the remaining
          (non-matching) entries, or deleted when it becomes empty.

    The caller (typically :class:`WalletService`) is responsible for
    providing a ``bond_belongs_to_wallet`` predicate that re-derives the
    expected pubkey for the bond's ``path``/``locktime`` from the open
    wallet and compares it to ``bond.pubkey``. Bonds that match are owned
    by the current wallet; the rest are left in the legacy file for other
    wallets to claim on their next open.

    Args:
        data_dir: Data directory holding the registry files.
        fingerprint: 8-char hex wallet fingerprint of the wallet
            performing the migration. Must be a valid lowercase hex
            string; an invalid value aborts the migration.
        bond_belongs_to_wallet: Predicate returning ``True`` when the
            given bond should be claimed by the current wallet.

    Returns:
        Number of bonds claimed by the current wallet (>=0). Returns 0
        when no migration ran for any reason.
    """
    safe_fp = _safe_fingerprint(fingerprint)
    if safe_fp is None:
        return 0

    per_wallet_path = get_registry_path(data_dir, safe_fp)
    legacy_path = get_legacy_registry_path(data_dir)

    if per_wallet_path.exists():
        return 0
    if not legacy_path.exists():
        return 0

    try:
        legacy_data = json.loads(legacy_path.read_text())
        legacy_registry = BondRegistry.model_validate(legacy_data)
    except Exception as e:
        logger.error(f"Failed to read legacy bond registry for migration: {e}")
        return 0

    claimed: list[FidelityBondInfo] = []
    remaining: list[FidelityBondInfo] = []
    for bond in legacy_registry.bonds:
        try:
            if bond_belongs_to_wallet(bond):
                claimed.append(bond)
            else:
                remaining.append(bond)
        except Exception as e:
            # Failing to verify a single bond must not lose data. Keep it
            # in the legacy file so a future open can try again.
            logger.warning(
                f"Bond {bond.address} verification raised during migration: {e}; "
                "leaving entry in legacy file"
            )
            remaining.append(bond)

    if not claimed:
        # Nothing to write; leave the legacy file untouched.
        return 0

    # Persist claimed entries to per-wallet file.
    save_registry(BondRegistry(version=legacy_registry.version, bonds=claimed), data_dir, safe_fp)

    # Rewrite legacy file with the remaining entries, or delete it when
    # empty so future wallets do not see a phantom file.
    if remaining:
        save_registry(
            BondRegistry(version=legacy_registry.version, bonds=remaining), data_dir, None
        )
    else:
        try:
            legacy_path.unlink()
        except OSError as e:
            logger.warning(f"Failed to remove empty legacy bond registry {legacy_path}: {e}")

    logger.info(
        f"Migrated {len(claimed)} bond(s) from legacy registry into "
        f"{per_wallet_path.name}; {len(remaining)} entry(ies) left in legacy file."
    )
    return len(claimed)


def make_wallet_ownership_predicate(
    master_key: HDKey, root_path: str
) -> Callable[[FidelityBondInfo], bool]:
    """Build the ``bond_belongs_to_wallet`` predicate for migration.

    The predicate re-derives the expected compressed pubkey for a bond and
    compares it to the bond's stored ``pubkey``. It first tries the bond's
    explicit BIP32 ``path`` (covering external/cold entries that may not be
    on the canonical fidelity-bond branch); if that path is not derivable it
    falls back to the canonical fidelity-bond branch derived from the bond's
    ``locktime`` (timenumber). Only bonds owned by ``master_key`` match, so
    foreign legacy entries are never claimed.

    This is shared by :class:`jmwallet.wallet.service.WalletService` and the
    offline ``jm-wallet`` bond commands so both paths use identical
    ownership rules.

    Args:
        master_key: The wallet's BIP32 master key.
        root_path: The wallet's account root path (e.g. ``m/84'/0'``); used
            to build the canonical fidelity-bond derivation path.
    """
    from jmwallet.wallet.constants import FIDELITY_BOND_BRANCH

    def _matches(bond: FidelityBondInfo) -> bool:
        try:
            key = master_key.derive(bond.path)
        except Exception:
            try:
                from jmcore.timenumber import timestamp_to_timenumber

                timenumber = timestamp_to_timenumber(bond.locktime)
                key = master_key.derive(f"{root_path}/0'/{FIDELITY_BOND_BRANCH}/{timenumber}")
            except Exception:
                return False
        derived_pubkey = key.get_public_key_bytes(compressed=True).hex()
        return derived_pubkey == bond.pubkey

    return _matches
