"""Tests for the fidelity bond registry module."""

from __future__ import annotations

import json
import time
from pathlib import Path

from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    create_bond_info,
    get_active_locktimes,
    get_all_locktimes,
    get_registry_path,
    load_registry,
    save_registry,
)


class TestFidelityBondInfo:
    """Tests for FidelityBondInfo model."""

    def test_is_funded_true(self) -> None:
        """Bond with txid and positive value should be funded."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid="abc123",
            vout=0,
            value=100000,
            confirmations=10,
        )
        assert bond.is_funded is True

    def test_is_funded_false_no_txid(self) -> None:
        """Bond without txid should not be funded."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )
        assert bond.is_funded is False

    def test_is_funded_false_zero_value(self) -> None:
        """Bond with zero value should not be funded."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid="abc123",
            vout=0,
            value=0,
        )
        assert bond.is_funded is False

    def test_is_expired_past(self) -> None:
        """Bond with past locktime should be expired."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) - 86400,  # Yesterday
            locktime_human="2020-01-01 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )
        assert bond.is_expired is True

    def test_is_expired_future(self) -> None:
        """Bond with future locktime should not be expired."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) + 86400 * 365,  # Next year
            locktime_human="2026-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )
        assert bond.is_expired is False

    def test_time_until_unlock(self) -> None:
        """Test time until unlock calculation."""
        future_locktime = int(time.time()) + 3600  # 1 hour from now
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=future_locktime,
            locktime_human="2025-12-31 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )
        # Should be approximately 3600 seconds (allow 5 second tolerance)
        assert 3595 <= bond.time_until_unlock <= 3605

    def test_time_until_unlock_expired(self) -> None:
        """Test time until unlock for expired bond returns 0."""
        bond = FidelityBondInfo(
            address="bc1qtest",
            locktime=int(time.time()) - 3600,  # 1 hour ago
            locktime_human="2020-01-01 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )
        assert bond.time_until_unlock == 0


class TestBondRegistry:
    """Tests for BondRegistry class."""

    def _create_bond(
        self,
        address: str = "bc1qtest",
        locktime: int | None = None,
        index: int = 0,
        value: int | None = None,
        txid: str | None = None,
    ) -> FidelityBondInfo:
        """Helper to create a test bond."""
        if locktime is None:
            locktime = int(time.time()) + 86400 * 365
        return FidelityBondInfo(
            address=address,
            locktime=locktime,
            locktime_human="2025-12-31 00:00:00",
            index=index,
            path=f"m/84'/0'/0'/2/{index}",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid=txid,
            vout=0 if txid else None,
            value=value,
            confirmations=10 if txid else None,
        )

    def test_add_bond(self) -> None:
        """Test adding a bond to the registry."""
        registry = BondRegistry()
        bond = self._create_bond()
        registry.add_bond(bond)
        assert len(registry.bonds) == 1
        assert registry.bonds[0].address == "bc1qtest"

    def test_add_bond_duplicate_replaces(self) -> None:
        """Adding a bond with same address should replace."""
        registry = BondRegistry()
        bond1 = self._create_bond(address="bc1qsame", value=100)
        bond2 = self._create_bond(address="bc1qsame", value=200)
        registry.add_bond(bond1)
        registry.add_bond(bond2)
        assert len(registry.bonds) == 1
        assert registry.bonds[0].value == 200

    def test_get_bond_by_address(self) -> None:
        """Test finding a bond by address."""
        registry = BondRegistry()
        bond = self._create_bond(address="bc1qfind")
        registry.add_bond(bond)

        found = registry.get_bond_by_address("bc1qfind")
        assert found is not None
        assert found.address == "bc1qfind"

        not_found = registry.get_bond_by_address("bc1qnotfound")
        assert not_found is None

    def test_get_bond_by_index(self) -> None:
        """Test finding a bond by index and locktime."""
        registry = BondRegistry()
        locktime = int(time.time()) + 86400
        bond = self._create_bond(index=5, locktime=locktime)
        registry.add_bond(bond)

        found = registry.get_bond_by_index(5, locktime)
        assert found is not None
        assert found.index == 5

        not_found = registry.get_bond_by_index(5, locktime + 1)
        assert not_found is None

    def test_get_funded_bonds(self) -> None:
        """Test getting funded bonds only."""
        registry = BondRegistry()
        funded1 = self._create_bond(address="bc1qfunded1", txid="tx1", value=100000)
        funded2 = self._create_bond(address="bc1qfunded2", txid="tx2", value=200000)
        unfunded = self._create_bond(address="bc1qunfunded")

        registry.add_bond(funded1)
        registry.add_bond(funded2)
        registry.add_bond(unfunded)

        funded_bonds = registry.get_funded_bonds()
        assert len(funded_bonds) == 2
        assert all(b.is_funded for b in funded_bonds)

    def test_get_active_bonds(self) -> None:
        """Test getting active (funded & not expired) bonds."""
        registry = BondRegistry()
        now = int(time.time())

        active = self._create_bond(
            address="bc1qactive",
            locktime=now + 86400 * 365,  # Future
            txid="tx1",
            value=100000,
        )
        expired = self._create_bond(
            address="bc1qexpired",
            locktime=now - 86400,  # Past
            txid="tx2",
            value=200000,
        )
        unfunded = self._create_bond(
            address="bc1qunfunded",
            locktime=now + 86400 * 365,  # Future but unfunded
        )

        registry.add_bond(active)
        registry.add_bond(expired)
        registry.add_bond(unfunded)

        active_bonds = registry.get_active_bonds()
        assert len(active_bonds) == 1
        assert active_bonds[0].address == "bc1qactive"

    def test_get_best_bond(self) -> None:
        """Test getting the best bond (highest value, longest lock)."""
        registry = BondRegistry()
        now = int(time.time())

        small = self._create_bond(
            address="bc1qsmall",
            locktime=now + 86400 * 365,
            txid="tx1",
            value=100000,
        )
        large = self._create_bond(
            address="bc1qlarge",
            locktime=now + 86400 * 365,
            txid="tx2",
            value=500000,
        )
        medium = self._create_bond(
            address="bc1qmedium",
            locktime=now + 86400 * 730,  # Longer lock
            txid="tx3",
            value=300000,
        )

        registry.add_bond(small)
        registry.add_bond(large)
        registry.add_bond(medium)

        best = registry.get_best_bond()
        assert best is not None
        # Should be the largest value
        assert best.address == "bc1qlarge"

    def test_get_best_bond_empty(self) -> None:
        """Test get_best_bond with no active bonds."""
        registry = BondRegistry()
        assert registry.get_best_bond() is None

    def test_update_utxo_info(self) -> None:
        """Test updating UTXO info for a bond."""
        registry = BondRegistry()
        bond = self._create_bond(address="bc1qupdate")
        registry.add_bond(bond)

        result = registry.update_utxo_info(
            address="bc1qupdate",
            txid="newtxid",
            vout=1,
            value=999999,
            confirmations=100,
        )
        assert result is True

        updated = registry.get_bond_by_address("bc1qupdate")
        assert updated is not None
        assert updated.txid == "newtxid"
        assert updated.vout == 1
        assert updated.value == 999999
        assert updated.confirmations == 100

    def test_update_utxo_info_not_found(self) -> None:
        """Test updating UTXO info for non-existent bond."""
        registry = BondRegistry()
        result = registry.update_utxo_info(
            address="bc1qnotfound",
            txid="tx",
            vout=0,
            value=100,
            confirmations=1,
        )
        assert result is False


class TestRegistryPersistence:
    """Tests for registry save/load functionality."""

    def test_save_and_load(self, tmp_path: Path) -> None:
        """Test saving and loading a registry."""
        registry = BondRegistry()
        bond = FidelityBondInfo(
            address="bc1qpersist",
            locktime=1735689600,
            locktime_human="2025-01-01 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="abcd" * 10,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid="persisttx",
            vout=0,
            value=12345678,
            confirmations=100,
        )
        registry.add_bond(bond)

        save_registry(registry, tmp_path)

        # Verify file was created
        registry_path = get_registry_path(tmp_path)
        assert registry_path.exists()

        # Load and verify
        loaded = load_registry(tmp_path)
        assert len(loaded.bonds) == 1
        assert loaded.bonds[0].address == "bc1qpersist"
        assert loaded.bonds[0].txid == "persisttx"
        assert loaded.bonds[0].value == 12345678

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        """Test loading from non-existent file returns empty registry."""
        loaded = load_registry(tmp_path)
        assert len(loaded.bonds) == 0

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        """Test loading invalid JSON returns empty registry."""
        registry_path = get_registry_path(tmp_path)
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text("not valid json {{{")

        loaded = load_registry(tmp_path)
        assert len(loaded.bonds) == 0

    def test_save_registry_is_atomic(self, tmp_path: Path) -> None:
        """Failed replace should not corrupt existing registry file."""
        initial = BondRegistry(
            bonds=[
                FidelityBondInfo(
                    address="bc1qinitial",
                    locktime=1735689600,
                    locktime_human="2025-01-01 00:00:00",
                    index=0,
                    path="m/84'/0'/0'/2/0",
                    pubkey="02" + "11" * 32,
                    witness_script_hex="aa" * 20,
                    network="mainnet",
                    created_at="2025-01-01T00:00:00",
                )
            ]
        )
        save_registry(initial, tmp_path)

        updated = BondRegistry(
            bonds=[
                FidelityBondInfo(
                    address="bc1qupdated",
                    locktime=1735689601,
                    locktime_human="2025-01-01 00:00:01",
                    index=1,
                    path="m/84'/0'/0'/2/1",
                    pubkey="03" + "22" * 32,
                    witness_script_hex="bb" * 20,
                    network="mainnet",
                    created_at="2025-01-01T00:00:01",
                )
            ]
        )

        from unittest.mock import patch

        with patch("jmwallet.wallet.bond_registry.os.replace", side_effect=OSError("disk full")):
            try:
                save_registry(updated, tmp_path)
            except OSError:
                pass

        # Existing file must remain valid and unchanged.
        loaded = json.loads(get_registry_path(tmp_path).read_text())
        assert loaded["bonds"][0]["address"] == "bc1qinitial"

        # Temporary file should be cleaned up on failure.
        tmp_files = list(get_registry_path(tmp_path).parent.glob("*.tmp"))
        assert tmp_files == []

    def test_save_registry_sets_private_permissions(self, tmp_path: Path) -> None:
        """Registry file should be written with mode 0600."""
        registry = BondRegistry(
            bonds=[
                FidelityBondInfo(
                    address="bc1qperm",
                    locktime=1735689600,
                    locktime_human="2025-01-01 00:00:00",
                    index=0,
                    path="m/84'/0'/0'/2/0",
                    pubkey="02" + "33" * 32,
                    witness_script_hex="cc" * 20,
                    network="mainnet",
                    created_at="2025-01-01T00:00:00",
                )
            ]
        )

        save_registry(registry, tmp_path)
        mode = get_registry_path(tmp_path).stat().st_mode & 0o777
        assert mode == 0o600


class TestCreateBondInfo:
    """Tests for the create_bond_info factory function."""

    def test_create_bond_info(self) -> None:
        """Test creating a FidelityBondInfo with the factory."""
        witness_script = bytes.fromhex("0480857467b17521" + "02" + "00" * 32 + "ac")
        bond = create_bond_info(
            address="bc1qfactory",
            locktime=1735689600,
            index=5,
            path="m/84'/0'/0'/2/5",
            pubkey_hex="02" + "00" * 32,
            witness_script=witness_script,
            network="mainnet",
        )

        assert bond.address == "bc1qfactory"
        assert bond.locktime == 1735689600
        assert bond.index == 5
        assert bond.path == "m/84'/0'/0'/2/5"
        assert bond.witness_script_hex == witness_script.hex()
        assert bond.network == "mainnet"
        assert "2024" in bond.locktime_human or "2025" in bond.locktime_human  # Date format
        assert bond.created_at  # Should have a timestamp
        assert bond.txid is None  # Not funded yet


class TestMultiUtxoHandling:
    """Tests for multiple UTXOs at the same bond address.

    Per the reference implementation, only the single biggest-value UTXO
    at a bond address is used as a fidelity bond.  Sending coins to the
    same address multiple times does NOT increase fidelity bond value.
    """

    def _create_bond(
        self,
        address: str = "bc1qtest",
        locktime: int | None = None,
        index: int = 0,
        value: int | None = None,
        txid: str | None = None,
    ) -> FidelityBondInfo:
        if locktime is None:
            locktime = int(time.time()) + 86400 * 365
        return FidelityBondInfo(
            address=address,
            locktime=locktime,
            locktime_human="2027-01-01 00:00:00",
            index=index,
            path=f"m/84'/0'/0'/2/{index}",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid=txid,
            vout=0 if txid else None,
            value=value,
            confirmations=10 if txid else None,
        )

    def test_update_utxo_info_overwrites_smaller(self) -> None:
        """Updating a bond with a larger UTXO should overwrite."""
        registry = BondRegistry()
        bond = self._create_bond(address="bc1qmulti", txid="tx_small", value=100_000)
        registry.add_bond(bond)

        # Update with larger UTXO
        result = registry.update_utxo_info(
            address="bc1qmulti",
            txid="tx_large",
            vout=0,
            value=500_000,
            confirmations=20,
        )
        assert result is True

        updated = registry.get_bond_by_address("bc1qmulti")
        assert updated is not None
        assert updated.txid == "tx_large"
        assert updated.value == 500_000
        assert updated.confirmations == 20

    def test_get_best_bond_picks_highest_value(self) -> None:
        """get_best_bond should pick the bond with highest value."""
        registry = BondRegistry()
        now = int(time.time())
        locktime = now + 86400 * 365

        # Two funded bonds at different addresses, same locktime
        small = self._create_bond(address="bc1qsmall", locktime=locktime, txid="tx1", value=100_000)
        large = self._create_bond(address="bc1qlarge", locktime=locktime, txid="tx2", value=500_000)
        registry.add_bond(small)
        registry.add_bond(large)

        best = registry.get_best_bond()
        assert best is not None
        assert best.address == "bc1qlarge"
        assert best.value == 500_000

    def test_registry_stores_one_utxo_per_address(self) -> None:
        """Registry should only store one UTXO per bond address (the address acts as key)."""
        registry = BondRegistry()

        bond = self._create_bond(address="bc1qbond", txid="tx_first", value=100_000)
        registry.add_bond(bond)

        # Adding same address replaces the entry
        bond2 = self._create_bond(address="bc1qbond", txid="tx_second", value=300_000)
        registry.add_bond(bond2)

        assert len(registry.bonds) == 1
        assert registry.bonds[0].txid == "tx_second"
        assert registry.bonds[0].value == 300_000


class TestLocktimeFunctions:
    """Tests for locktime discovery functions."""

    def test_get_all_locktimes_empty(self, tmp_path: Path) -> None:
        """Test get_all_locktimes with empty registry."""
        locktimes = get_all_locktimes(tmp_path)
        assert locktimes == []

    def test_get_all_locktimes_returns_all(self, tmp_path: Path) -> None:
        """Test get_all_locktimes returns all unique locktimes."""
        now = int(time.time())
        registry = BondRegistry()

        # Add bonds with different locktimes (some funded, some not)
        bond1 = FidelityBondInfo(
            address="bc1qbond1",
            locktime=now + 86400,
            locktime_human="2025-01-01 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid="tx1",
            vout=0,
            value=100000,
        )
        bond2 = FidelityBondInfo(
            address="bc1qbond2",
            locktime=now + 86400 * 2,
            locktime_human="2025-01-02 00:00:00",
            index=1,
            path="m/84'/0'/0'/2/1",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )  # Unfunded
        bond3 = FidelityBondInfo(
            address="bc1qbond3",
            locktime=now + 86400,  # Same locktime as bond1
            locktime_human="2025-01-01 00:00:00",
            index=2,
            path="m/84'/0'/0'/2/2",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )

        registry.add_bond(bond1)
        registry.add_bond(bond2)
        registry.add_bond(bond3)
        save_registry(registry, tmp_path)

        locktimes = get_all_locktimes(tmp_path)
        # Should return 2 unique locktimes (bond1&3 share one, bond2 has different)
        assert len(locktimes) == 2
        assert now + 86400 in locktimes
        assert now + 86400 * 2 in locktimes
        # Should be sorted
        assert locktimes == sorted(locktimes)

    def test_get_active_locktimes_empty(self, tmp_path: Path) -> None:
        """Test get_active_locktimes with empty registry."""
        locktimes = get_active_locktimes(tmp_path)
        assert locktimes == []

    def test_get_active_locktimes_only_active(self, tmp_path: Path) -> None:
        """Test get_active_locktimes returns only locktimes for active bonds."""
        now = int(time.time())
        registry = BondRegistry()

        # Active bond (funded + not expired)
        active = FidelityBondInfo(
            address="bc1qactive",
            locktime=now + 86400 * 365,
            locktime_human="2026-01-01 00:00:00",
            index=0,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid="tx1",
            vout=0,
            value=100000,
        )
        # Unfunded bond
        unfunded = FidelityBondInfo(
            address="bc1qunfunded",
            locktime=now + 86400 * 200,
            locktime_human="2025-07-01 00:00:00",
            index=1,
            path="m/84'/0'/0'/2/1",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
        )
        # Expired bond (funded but past locktime)
        expired = FidelityBondInfo(
            address="bc1qexpired",
            locktime=now - 86400,  # Past
            locktime_human="2020-01-01 00:00:00",
            index=2,
            path="m/84'/0'/0'/2/2",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="mainnet",
            created_at="2025-01-01T00:00:00",
            txid="tx2",
            vout=0,
            value=200000,
        )

        registry.add_bond(active)
        registry.add_bond(unfunded)
        registry.add_bond(expired)
        save_registry(registry, tmp_path)

        locktimes = get_active_locktimes(tmp_path)
        # Should only return the locktime of the active bond
        assert len(locktimes) == 1
        assert now + 86400 * 365 in locktimes
