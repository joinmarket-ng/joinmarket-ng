"""Tests for ``WalletService.sync_with_registered_bonds``.

The jmwalletd daemon's ``/utxos`` and ``/display`` endpoints rely on this
bond-aware sync so that funded fidelity bonds (stored under the registry's
``.../2/...`` branch, which is not part of the standard descriptor import)
are scanned into ``utxo_cache`` and therefore visible to JAM. Without it,
the bond UTXO is absent from the API response and the coins "disappear".
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from jmwallet.backends.base import UTXO, BlockchainBackend
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.bond_registry import (
    BondRegistry,
    FidelityBondInfo,
    save_registry,
)
from jmwallet.wallet.service import WalletService

# BIP-39 test vector -- never use on mainnet.
MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)

BOND_ADDRESS = "bcrt1qbondaddress0000000000000000000000000000xyz"
BOND_LOCKTIME = 1893456000  # 2030-01-01 00:00:00 UTC
BOND_INDEX = 120


def _make_wallet(data_dir: Path) -> WalletService:
    backend = DescriptorWalletBackend(wallet_name="test_wallet")
    backend._wallet_loaded = True
    return WalletService(
        mnemonic=MNEMONIC,
        backend=backend,
        network="regtest",
        data_dir=data_dir,
    )


def _write_bond_registry(ws: WalletService, *, network: str = "regtest") -> None:
    registry = BondRegistry()
    registry.add_bond(
        FidelityBondInfo(
            address=BOND_ADDRESS,
            locktime=BOND_LOCKTIME,
            locktime_human="2030-01-01 00:00:00",
            index=BOND_INDEX,
            path=f"m/84'/1'/0'/2/{BOND_INDEX}",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network=network,
            created_at="2025-01-01T00:00:00",
        )
    )
    save_registry(registry, ws.data_dir, ws.wallet_fingerprint)


def test_load_registered_bond_addresses_filters_by_network(tmp_path: Path) -> None:
    ws = _make_wallet(tmp_path)
    _write_bond_registry(ws, network="regtest")

    bonds = ws.load_registered_bond_addresses()
    assert bonds == [(BOND_ADDRESS, BOND_LOCKTIME, BOND_INDEX)]


def test_load_registered_bond_addresses_skips_other_networks(tmp_path: Path) -> None:
    ws = _make_wallet(tmp_path)
    _write_bond_registry(ws, network="mainnet")

    # The wallet is on regtest; a mainnet bond in the registry must be ignored.
    assert ws.load_registered_bond_addresses() == []


def test_load_registered_bond_addresses_empty_without_data_dir() -> None:
    backend = DescriptorWalletBackend(wallet_name="test_wallet")
    backend._wallet_loaded = True
    ws = WalletService(mnemonic=MNEMONIC, backend=backend, network="regtest")
    assert ws.load_registered_bond_addresses() == []


@pytest.mark.asyncio
async def test_sync_with_registered_bonds_imports_missing_bond(tmp_path: Path) -> None:
    """A bond present in the registry but not yet imported is imported + rescanned."""
    ws = _make_wallet(tmp_path)
    _write_bond_registry(ws)

    # Base wallet (mixdepths) is set up, but the bond descriptor is not present
    # in the descriptor list.
    ws._imported_bond_addresses = AsyncMock(return_value=set())
    ws.is_descriptor_wallet_ready = AsyncMock(return_value=True)
    ws.setup_descriptor_wallet = AsyncMock()
    ws.import_fidelity_bond_addresses = AsyncMock(return_value=True)
    ws.sync_with_descriptor_wallet = AsyncMock(return_value={0: []})

    result = await ws.sync_with_registered_bonds()

    expected_bonds = [(BOND_ADDRESS, BOND_LOCKTIME, BOND_INDEX)]
    # Bond descriptor is imported (with rescan) since the base wallet already
    # exists -- we must not re-import the whole wallet.
    ws.setup_descriptor_wallet.assert_not_awaited()
    ws.import_fidelity_bond_addresses.assert_awaited_once_with(expected_bonds, rescan=True)
    ws.sync_with_descriptor_wallet.assert_awaited_once_with(expected_bonds)
    assert result == {0: []}


@pytest.mark.asyncio
async def test_sync_with_registered_bonds_first_time_setup(tmp_path: Path) -> None:
    """When the base wallet is not set up, bonds are imported during setup."""
    ws = _make_wallet(tmp_path)
    _write_bond_registry(ws)

    ws._imported_bond_addresses = AsyncMock(return_value=set())
    ws.is_descriptor_wallet_ready = AsyncMock(return_value=False)
    ws.setup_descriptor_wallet = AsyncMock()
    ws.import_fidelity_bond_addresses = AsyncMock()
    ws.sync_with_descriptor_wallet = AsyncMock(return_value={0: []})

    await ws.sync_with_registered_bonds()

    expected_bonds = [(BOND_ADDRESS, BOND_LOCKTIME, BOND_INDEX)]
    ws.setup_descriptor_wallet.assert_awaited_once_with(
        rescan=True, fidelity_bond_addresses=expected_bonds
    )
    ws.import_fidelity_bond_addresses.assert_not_awaited()
    ws.sync_with_descriptor_wallet.assert_awaited_once_with(expected_bonds)


@pytest.mark.asyncio
async def test_sync_with_registered_bonds_already_imported(tmp_path: Path) -> None:
    """When the bond is already imported, sync runs without re-importing."""
    ws = _make_wallet(tmp_path)
    _write_bond_registry(ws)

    # The bond address is already present among the imported descriptors.
    ws._imported_bond_addresses = AsyncMock(return_value={BOND_ADDRESS.lower()})
    ws.is_descriptor_wallet_ready = AsyncMock(return_value=True)
    ws.setup_descriptor_wallet = AsyncMock()
    ws.import_fidelity_bond_addresses = AsyncMock()
    ws.sync_with_descriptor_wallet = AsyncMock(return_value={0: []})

    await ws.sync_with_registered_bonds()

    ws.setup_descriptor_wallet.assert_not_awaited()
    ws.import_fidelity_bond_addresses.assert_not_awaited()
    ws.sync_with_descriptor_wallet.assert_awaited_once_with(
        [(BOND_ADDRESS, BOND_LOCKTIME, BOND_INDEX)]
    )


@pytest.mark.asyncio
async def test_imported_bond_addresses_parses_addr_descriptors(tmp_path: Path) -> None:
    """``_imported_bond_addresses`` extracts addresses from ``addr(...)`` descriptors."""
    ws = _make_wallet(tmp_path)
    ws.backend.list_descriptors = AsyncMock(
        return_value=[
            {"desc": "wpkh([abcd/0/0]02aa...)#chk"},
            {"desc": f"addr({BOND_ADDRESS})#abcd1234"},
            {"desc": "addr(bcrt1qother)"},
        ]
    )

    imported = await ws._imported_bond_addresses()
    assert imported == {BOND_ADDRESS.lower(), "bcrt1qother"}


@pytest.mark.asyncio
async def test_sync_imports_bond_when_base_has_extra_descriptors(tmp_path: Path) -> None:
    """Regression: a bond must be imported even when the descriptor *count* is high.

    The base wallet imports more descriptors than ``mixdepth_count * 2`` (Bitcoin
    Core records internal/external variants), so a count-based readiness check
    would falsely conclude the bond is present and skip importing it -- leaving
    the funded bond invisible. Detection must be by actual ``addr()`` descriptor.
    """
    ws = _make_wallet(tmp_path)
    _write_bond_registry(ws)

    # Plenty of base descriptors imported, but the bond's addr() is absent.
    ws.backend.list_descriptors = AsyncMock(
        return_value=[{"desc": f"wpkh([fp/{i}]02ab...)#c"} for i in range(20)]
    )
    ws.is_descriptor_wallet_ready = AsyncMock(return_value=True)
    ws.setup_descriptor_wallet = AsyncMock()
    ws.import_fidelity_bond_addresses = AsyncMock(return_value=True)
    ws.sync_with_descriptor_wallet = AsyncMock(return_value={0: []})

    await ws.sync_with_registered_bonds()

    ws.import_fidelity_bond_addresses.assert_awaited_once_with(
        [(BOND_ADDRESS, BOND_LOCKTIME, BOND_INDEX)], rescan=True
    )


@pytest.mark.asyncio
async def test_sync_with_registered_bonds_no_bonds_base_ready(tmp_path: Path) -> None:
    """With no registered bonds and a ready base wallet, a plain sync runs."""
    ws = _make_wallet(tmp_path)  # No registry written.

    ws.is_descriptor_wallet_ready = AsyncMock(return_value=True)
    ws.setup_descriptor_wallet = AsyncMock()
    ws.import_fidelity_bond_addresses = AsyncMock()
    ws.sync_with_descriptor_wallet = AsyncMock(return_value={0: []})

    await ws.sync_with_registered_bonds()

    # The base wallet is checked but already set up, so no import work happens.
    ws.is_descriptor_wallet_ready.assert_awaited_once_with(fidelity_bond_count=0)
    ws.setup_descriptor_wallet.assert_not_awaited()
    ws.import_fidelity_bond_addresses.assert_not_awaited()
    ws.sync_with_descriptor_wallet.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_sync_with_registered_bonds_no_bonds_first_time_setup(tmp_path: Path) -> None:
    """With no bonds but an un-set-up base wallet, base setup still runs.

    Regression: the CLI paths rely on this method for first-time descriptor
    setup. If it only set up the base wallet when bonds were present, a
    brand-new wallet with no bonds would never import its mixdepth descriptors.
    """
    ws = _make_wallet(tmp_path)  # No registry written.

    ws.is_descriptor_wallet_ready = AsyncMock(return_value=False)
    ws.setup_descriptor_wallet = AsyncMock()
    ws.import_fidelity_bond_addresses = AsyncMock()
    ws.sync_with_descriptor_wallet = AsyncMock(return_value={0: []})

    await ws.sync_with_registered_bonds()

    # No bonds, so setup imports just the base descriptors (with a rescan).
    ws.setup_descriptor_wallet.assert_awaited_once_with(rescan=True, fidelity_bond_addresses=None)
    ws.import_fidelity_bond_addresses.assert_not_awaited()
    ws.sync_with_descriptor_wallet.assert_awaited_once_with(None)


@pytest.mark.asyncio
async def test_sync_with_registered_bonds_non_descriptor_backend(tmp_path: Path) -> None:
    """Non-descriptor backends scan bond addresses directly via sync_all."""
    ws = _make_wallet(tmp_path)
    _write_bond_registry(ws)

    # Swap in a non-descriptor backend; sync_all must receive the bonds.
    ws.backend = object()  # type: ignore[assignment]
    ws.sync_all = AsyncMock(return_value={0: []})

    await ws.sync_with_registered_bonds()

    ws.sync_all.assert_awaited_once_with([(BOND_ADDRESS, BOND_LOCKTIME, BOND_INDEX)])


class _FakeLightClientBackend(BlockchainBackend):
    """Minimal light-client backend (Neutrino-like) for the legacy sync path.

    Returns a UTXO only for the fidelity bond address and only after it has
    been registered via ``ensure_addresses_scanned`` -- mirroring how Neutrino
    must historically rescan a freshly watched address before its funding is
    visible.
    """

    supports_descriptor_scan = False
    supports_watch_address = True

    def __init__(self, bond_address: str, bond_utxo: UTXO) -> None:
        self._bond_address = bond_address
        self._bond_utxo = bond_utxo
        self._scanned: set[str] = set()
        self.ensure_calls: list[list[str]] = []

    async def add_watch_address(self, address: str) -> None:
        self._scanned.add(address)

    async def ensure_addresses_scanned(self, addresses: list[str]) -> None:
        self.ensure_calls.append(list(addresses))
        self._scanned.update(addresses)

    async def get_utxos(self, addresses: list[str]) -> list[UTXO]:
        if self._bond_address in addresses and self._bond_address in self._scanned:
            return [self._bond_utxo]
        return []

    async def get_block_height(self) -> int:
        return 1_000

    # Unused abstract members for this test.
    async def get_address_balance(self, address: str) -> int:
        return 0

    async def broadcast_transaction(self, tx_hex: str) -> str:
        return "txid"

    async def get_transaction(self, txid: str):  # type: ignore[no-untyped-def]
        return None

    async def estimate_fee(self, target_blocks: int) -> float:
        return 1.0

    async def get_block_hash(self, block_height: int) -> str:
        return "00"

    async def get_block_time(self, block_height: int) -> int:
        return 0

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        return None


@pytest.mark.asyncio
async def test_sync_all_scans_bonds_on_light_client_backend(tmp_path: Path) -> None:
    """The legacy address-scan path must scan supplied fidelity bond addresses.

    Regression: ``sync_all(fidelity_bond_addresses)`` previously ignored the
    bonds entirely on non-descriptor backends (Neutrino), so a funded bond
    never landed in ``utxo_cache`` and the coins "disappeared" from the wallet.
    """
    from jmcore.timenumber import timenumber_to_timestamp

    # Use a real derivable timenumber so the address matches what
    # sync_fidelity_bonds derives internally.
    timenumber = BOND_INDEX
    locktime = timenumber_to_timestamp(timenumber)

    backend = _FakeLightClientBackend(bond_address="", bond_utxo=None)  # type: ignore[arg-type]
    ws = WalletService(
        mnemonic=MNEMONIC,
        backend=backend,  # type: ignore[arg-type]
        network="regtest",
        data_dir=tmp_path,
    )
    bond_address = ws.get_fidelity_bond_address(timenumber, locktime)
    backend._bond_address = bond_address
    backend._bond_utxo = UTXO(
        txid="cc" * 32,
        vout=0,
        value=123_456,
        address=bond_address,
        confirmations=42,
        scriptpubkey="0020" + "33" * 32,
        height=950,
    )

    result = await ws.sync_all([(bond_address, locktime, timenumber)])

    # The bond was historically rescanned before querying.
    assert backend.ensure_calls, "ensure_addresses_scanned was not called for the bond"
    assert bond_address in backend.ensure_calls[0]

    # The bond UTXO is present in mixdepth 0 (both the returned mapping and the
    # cache), tagged as a fidelity bond with the :locktime path suffix.
    bond_in_result = [u for u in result.get(0, []) if u.address == bond_address]
    assert len(bond_in_result) == 1
    bond = bond_in_result[0]
    assert bond.locktime == locktime
    assert bond.is_fidelity_bond
    assert bond.path.endswith(f":{locktime}")
    assert any(u.address == bond_address for u in ws.utxo_cache.get(0, []))
