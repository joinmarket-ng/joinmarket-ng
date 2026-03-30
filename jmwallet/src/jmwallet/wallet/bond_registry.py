"""
Fidelity bond registry for persistent storage of bond metadata.

This module provides storage and retrieval of fidelity bond information,
including addresses, locktimes, witness scripts, and UTXO tracking.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel


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


def get_registry_path(data_dir: Path) -> Path:
    """Get the path to the bond registry file."""
    return data_dir / "fidelity_bonds.json"


def load_registry(data_dir: Path) -> BondRegistry:
    """
    Load the bond registry from disk.

    Args:
        data_dir: Data directory path

    Returns:
        BondRegistry instance (empty if file doesn't exist)
    """
    registry_path = get_registry_path(data_dir)
    if not registry_path.exists():
        return BondRegistry()

    try:
        data = json.loads(registry_path.read_text())
        return BondRegistry.model_validate(data)
    except Exception as e:
        logger.error(f"Failed to load bond registry: {e}")
        # Return empty registry on error, but don't overwrite the file
        return BondRegistry()


def save_registry(registry: BondRegistry, data_dir: Path) -> None:
    """
    Save the bond registry to disk.

    Args:
        registry: BondRegistry instance
        data_dir: Data directory path
    """
    registry_path = get_registry_path(data_dir)
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


def get_active_locktimes(data_dir: Path) -> list[int]:
    """
    Get all locktimes from the bond registry that have funded, active bonds.

    This is useful for the maker bot to automatically discover which locktimes
    to scan for when syncing fidelity bonds, without requiring the user to
    manually specify --fidelity-bond-locktime.

    Args:
        data_dir: Data directory path

    Returns:
        List of unique locktimes (Unix timestamps) for active bonds
    """
    registry = load_registry(data_dir)
    active_bonds = registry.get_active_bonds()
    # Get unique locktimes
    locktimes = list({bond.locktime for bond in active_bonds})
    return sorted(locktimes)


def get_all_locktimes(data_dir: Path) -> list[int]:
    """
    Get all locktimes from the bond registry (funded or not).

    This includes all bonds in the registry to allow scanning for UTXOs
    that may have been funded since the last sync.

    Args:
        data_dir: Data directory path

    Returns:
        List of unique locktimes (Unix timestamps) for all bonds
    """
    registry = load_registry(data_dir)
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
