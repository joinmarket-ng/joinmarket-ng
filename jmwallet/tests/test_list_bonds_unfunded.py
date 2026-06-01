"""Online ``list-bonds`` must surface registered-but-unfunded fidelity bonds.

The online path (``--mnemonic-file``) only discovers funded UTXOs on-chain.
Bonds created with ``generate-bond-address`` / ``import-bond`` but not yet
funded live only in the per-wallet registry, so they must be listed
separately and shown by default.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmwallet.cli.bonds import _list_fidelity_bonds
from jmwallet.wallet.bond_registry import BondRegistry, FidelityBondInfo

# BIP-39 test vector -- never use on mainnet.
MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about"
)

UNFUNDED_ADDRESS = "bcrt1qunfundedbond00000000000000000000000000xyz"


def _backend_settings() -> MagicMock:
    settings = MagicMock()
    settings.network = "regtest"
    settings.data_dir = MagicMock()
    settings.rpc_url = "http://localhost:18443"
    settings.rpc_user = "user"
    settings.rpc_password = "pass"
    return settings


def _unfunded_registry() -> BondRegistry:
    registry = BondRegistry()
    registry.add_bond(
        FidelityBondInfo(
            address=UNFUNDED_ADDRESS,
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
async def test_online_list_bonds_shows_unfunded_registered_bond(capsys) -> None:
    wallet = MagicMock()
    wallet.wallet_fingerprint = "deadbeef"
    wallet.metadata_store = MagicMock()
    wallet.sync_all = AsyncMock()
    wallet.sync_fidelity_bonds = AsyncMock()
    wallet.close = AsyncMock()
    wallet.utxo_cache = {}

    backend = MagicMock()

    with (
        patch("jmwallet.backends.descriptor_wallet.DescriptorWalletBackend", return_value=backend),
        patch("jmwallet.wallet.service.WalletService", return_value=wallet),
        patch("jmwallet.wallet.bond_registry.load_registry", return_value=_unfunded_registry()),
        # No funded bonds discovered on-chain.
        patch("maker.fidelity.find_fidelity_bonds", AsyncMock(return_value=[])),
    ):
        await _list_fidelity_bonds(MNEMONIC, _backend_settings(), [])

    out = capsys.readouterr().out
    assert "Registered but unfunded fidelity bond(s): 1" in out
    assert UNFUNDED_ADDRESS in out
    assert "UNFUNDED" in out
