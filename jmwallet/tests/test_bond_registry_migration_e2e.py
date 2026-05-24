"""End-to-end migration test: opening two WalletService instances against
the same data directory must isolate their fidelity bonds (issue #492).

A pre-existing shared ``fidelity_bonds.json`` (legacy format) is planted
with one bond belonging to each wallet plus one bond belonging to neither
(it stays in the legacy file). After opening each wallet via
``WalletService`` the per-wallet partitioning must hold.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    get_legacy_registry_path,
    load_registry,
    save_registry,
)
from jmwallet.wallet.service import FIDELITY_BOND_BRANCH, WalletService

# BIP-39 test vectors -- never use these on mainnet.
MNEMONIC_A = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)
MNEMONIC_B = "legal winner thank year wave sausage worth useful legal winner thank yellow"


def _make_service(tmp_path: Path, mnemonic: str) -> WalletService:
    backend = MagicMock()
    backend.connect = AsyncMock()
    backend._descriptors_imported = True
    return WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network="mainnet",
        mixdepth_count=2,
        data_dir=tmp_path,
    )


def _bond_for(mnemonic: str, *, address: str, timenumber: int = 0) -> FidelityBondInfo:
    """Build a registry entry whose pubkey is re-derivable from ``mnemonic``.

    Migration's ``bond_belongs_to_wallet`` predicate derives the bond path
    on the canonical fidelity-bond branch and compares the resulting
    compressed pubkey to ``bond.pubkey``. To make the entry claimable we
    derive that pubkey here from the same mnemonic.
    """
    seed = mnemonic_to_seed(mnemonic)
    master = HDKey.from_seed(seed)
    coin_type = 0  # mainnet
    path = f"m/84'/{coin_type}'/0'/{FIDELITY_BOND_BRANCH}/{timenumber}"
    key = master.derive(path)
    pubkey = key.get_public_key_bytes(compressed=True).hex()
    # Locktime is only used as a label here; the predicate trusts ``path``.
    return FidelityBondInfo(
        address=address,
        locktime=1893456000,  # 2030-01-01 UTC
        locktime_human="2030-01-01 00:00:00",
        index=timenumber,
        path=path,
        pubkey=pubkey,
        witness_script_hex="00" * 50,
        network="mainnet",
        created_at="2025-01-01T00:00:00",
    )


def test_per_wallet_isolation_after_legacy_migration(tmp_path: Path) -> None:
    """Opening wallet A then wallet B against a shared legacy registry
    partitions each wallet's bond into its own per-wallet file."""
    bond_a = _bond_for(MNEMONIC_A, address="bc1qwalleta", timenumber=0)
    bond_b = _bond_for(MNEMONIC_B, address="bc1qwalletb", timenumber=0)
    # Orphan entry: pubkey does not match either wallet so neither claims it.
    bond_orphan = FidelityBondInfo(
        address="bc1qorphan",
        locktime=1893456000,
        locktime_human="2030-01-01 00:00:00",
        index=0,
        path="external",
        pubkey="02" + "ff" * 32,
        witness_script_hex="00" * 50,
        network="mainnet",
        created_at="2025-01-01T00:00:00",
    )

    legacy = BondRegistry()
    legacy.add_bond(bond_a)
    legacy.add_bond(bond_b)
    legacy.add_bond(bond_orphan)
    save_registry(legacy, tmp_path)
    legacy_path = get_legacy_registry_path(tmp_path)
    assert legacy_path.exists()

    # Opening wallet A migrates its own bond out of the legacy file.
    svc_a = _make_service(tmp_path, MNEMONIC_A)
    a_view = load_registry(tmp_path, svc_a.wallet_fingerprint)
    assert [b.address for b in a_view.bonds] == ["bc1qwalleta"], (
        "Wallet A must only see its own bond after migration"
    )

    # Legacy file still has wallet B's bond and the orphan.
    legacy_after_a = load_registry(tmp_path)
    addrs_after_a = {b.address for b in legacy_after_a.bonds}
    assert addrs_after_a == {"bc1qwalletb", "bc1qorphan"}

    # Opening wallet B then claims its own bond, leaving only the orphan.
    svc_b = _make_service(tmp_path, MNEMONIC_B)
    b_view = load_registry(tmp_path, svc_b.wallet_fingerprint)
    assert [b.address for b in b_view.bonds] == ["bc1qwalletb"], (
        "Wallet B must only see its own bond after migration"
    )
    legacy_after_b = load_registry(tmp_path)
    assert [b.address for b in legacy_after_b.bonds] == ["bc1qorphan"], (
        "Orphan bond must remain in the legacy file until a wallet claims it"
    )

    # Cross-check: wallet A's view did not regress when B opened.
    a_view_again = load_registry(tmp_path, svc_a.wallet_fingerprint)
    assert [b.address for b in a_view_again.bonds] == ["bc1qwalleta"]

    # And the two fingerprints are actually different (defensive: if these
    # ever collided the isolation assertions above would still hold but
    # the test would no longer be meaningful).
    assert svc_a.wallet_fingerprint != svc_b.wallet_fingerprint


def test_legacy_file_removed_when_all_bonds_claimed(tmp_path: Path) -> None:
    """When every legacy entry belongs to the opening wallet, the shared
    file is deleted so future opens skip the migration cheaply."""
    bond_a1 = _bond_for(MNEMONIC_A, address="bc1qa1", timenumber=0)
    bond_a2 = _bond_for(MNEMONIC_A, address="bc1qa2", timenumber=1)
    legacy = BondRegistry()
    legacy.add_bond(bond_a1)
    legacy.add_bond(bond_a2)
    save_registry(legacy, tmp_path)
    legacy_path = get_legacy_registry_path(tmp_path)
    assert legacy_path.exists()

    svc = _make_service(tmp_path, MNEMONIC_A)
    per_wallet = load_registry(tmp_path, svc.wallet_fingerprint)
    assert {b.address for b in per_wallet.bonds} == {"bc1qa1", "bc1qa2"}
    assert not legacy_path.exists(), "Legacy file should be removed once all entries are claimed"


def test_reopening_wallet_is_idempotent(tmp_path: Path) -> None:
    """A second WalletService open must not re-trigger migration or
    corrupt the already-partitioned per-wallet file."""
    bond = _bond_for(MNEMONIC_A, address="bc1qonly", timenumber=0)
    legacy = BondRegistry()
    legacy.add_bond(bond)
    save_registry(legacy, tmp_path)

    svc1 = _make_service(tmp_path, MNEMONIC_A)
    fp = svc1.wallet_fingerprint
    first_view = load_registry(tmp_path, fp)
    assert [b.address for b in first_view.bonds] == ["bc1qonly"]

    # Re-open: per-wallet file already exists so migrate_legacy_registry
    # short-circuits without touching it.
    svc2 = _make_service(tmp_path, MNEMONIC_A)
    assert svc2.wallet_fingerprint == fp
    second_view = load_registry(tmp_path, fp)
    assert [b.address for b in second_view.bonds] == ["bc1qonly"]
