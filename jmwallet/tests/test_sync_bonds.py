"""``jm-wallet sync-bonds`` refreshes funded status of registered bonds.

It must sync only the bond addresses already in the per-wallet registry
(no full timenumber discovery) and write the discovered UTXO info back to
the registry so the offline ``list-bonds`` view reflects the new balance.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmwallet.cli.bonds import _sync_bonds_async
from jmwallet.wallet.bond_registry import BondRegistry, FidelityBondInfo

# BIP-39 test vector -- never use on mainnet.
MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)

BOND_ADDRESS = "bcrt1qbondaddress0000000000000000000000000000xyz"


def _backend_settings() -> MagicMock:
    settings = MagicMock()
    settings.backend_type = "descriptor_wallet"
    settings.network = "regtest"
    settings.data_dir = MagicMock()
    settings.rpc_url = "http://localhost:18443"
    settings.rpc_user = "user"
    settings.rpc_password = "pass"
    return settings


def _registry_with_unfunded_bond() -> BondRegistry:
    registry = BondRegistry()
    registry.add_bond(
        FidelityBondInfo(
            address=BOND_ADDRESS,
            locktime=1893456000,
            locktime_human="2030-01-01 00:00:00",
            index=0,
            path="m/84'/1'/0'/2/0",
            pubkey="02" + "00" * 32,
            witness_script_hex="00" * 50,
            network="regtest",
            created_at="2025-01-01T00:00:00",
        )
    )
    return registry


@pytest.mark.asyncio
async def test_sync_bonds_updates_registry_with_funded_utxo() -> None:
    registry = _registry_with_unfunded_bond()

    wallet = MagicMock()
    wallet.wallet_fingerprint = "deadbeef"
    wallet.sync_with_registered_bonds = AsyncMock()
    wallet.close = AsyncMock()
    # UTXO discovered on-chain at the registered bond address.
    wallet.utxo_cache = {
        0: [
            SimpleNamespace(
                address=BOND_ADDRESS,
                txid="ab" * 32,
                vout=0,
                value=100_000,
                confirmations=3,
            )
        ]
    }

    backend = MagicMock()
    backend.create_wallet = AsyncMock()

    saved: list[BondRegistry] = []

    with (
        patch(
            "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend",
            return_value=backend,
        ),
        patch("jmwallet.backends.descriptor_wallet.generate_wallet_name", return_value="w"),
        patch(
            "jmwallet.backends.descriptor_wallet.get_mnemonic_fingerprint",
            return_value="deadbeef",
        ),
        patch("jmwallet.wallet.service.WalletService", return_value=wallet),
        patch("jmwallet.wallet.bond_registry.load_registry", return_value=registry),
        patch(
            "jmwallet.wallet.bond_registry.save_registry",
            side_effect=lambda reg, *a, **k: saved.append(reg),
        ),
    ):
        await _sync_bonds_async(MNEMONIC, _backend_settings())

    # The bond-aware sync ran (it imports any missing bond ``addr()``
    # descriptor with a rescan, so a bond funded after the base wallet was set
    # up is found; a plain ``sync_all`` would import it without a rescan and
    # miss the already-confirmed UTXO).
    wallet.sync_with_registered_bonds.assert_awaited_once()

    # Registry was updated with the funded UTXO and persisted.
    bond = registry.get_bond_by_address(BOND_ADDRESS)
    assert bond is not None
    assert bond.value == 100_000
    assert bond.txid == "ab" * 32
    assert bond.confirmations == 3
    assert saved and saved[0] is registry


@pytest.mark.asyncio
async def test_sync_bonds_no_bonds_in_registry_does_not_sync() -> None:
    wallet = MagicMock()
    wallet.wallet_fingerprint = "deadbeef"
    wallet.sync_with_registered_bonds = AsyncMock()
    wallet.close = AsyncMock()
    wallet.utxo_cache = {}

    backend = MagicMock()
    backend.create_wallet = AsyncMock()

    with (
        patch(
            "jmwallet.backends.descriptor_wallet.DescriptorWalletBackend",
            return_value=backend,
        ),
        patch("jmwallet.backends.descriptor_wallet.generate_wallet_name", return_value="w"),
        patch(
            "jmwallet.backends.descriptor_wallet.get_mnemonic_fingerprint",
            return_value="deadbeef",
        ),
        patch("jmwallet.wallet.service.WalletService", return_value=wallet),
        patch("jmwallet.wallet.bond_registry.load_registry", return_value=BondRegistry()),
    ):
        await _sync_bonds_async(MNEMONIC, _backend_settings())

    wallet.sync_with_registered_bonds.assert_not_awaited()
    wallet.close.assert_awaited_once()
