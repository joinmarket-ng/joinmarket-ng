"""
E2E test for the ``jm-wallet sync-bonds`` command.

Validates the fast registry-refresh path: a bond address is recorded in the
per-wallet registry (as ``generate-bond-address`` would), the address is funded
on-chain, and ``sync-bonds`` updates the registry entry with the on-chain UTXO
info (txid, vout, value, confirmations) without running a full 960-timelock
discovery scan.

Prerequisites:
- Docker and Docker Compose installed
- Run: docker compose --profile e2e up -d

Usage:
    pytest tests/e2e/test_sync_bonds_e2e.py -v -s --timeout=120 -m e2e
"""

from __future__ import annotations

from pathlib import Path

import pytest
from jmcore.cli_common import ResolvedBackendSettings
from jmcore.timenumber import timenumber_to_timestamp
from loguru import logger

from jmwallet.cli.bonds import _sync_bonds_async
from jmwallet.wallet.address import script_to_p2wsh_address
from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.bond_registry import (
    create_bond_info,
    load_registry,
    save_registry,
)
from jmwallet.wallet.service import FIDELITY_BOND_BRANCH

# Mark all tests in this module as requiring Docker e2e profile
pytestmark = pytest.mark.e2e

# Standard test mnemonic (12 words). Never a real-funds wallet.
TEST_MNEMONIC = (
    "abandon abandon abandon abandon abandon abandon "
    "abandon abandon abandon abandon abandon about"
)


def _derive_bond(
    mnemonic: str, network: str, timenumber: int
) -> tuple[str, str, bytes, str]:
    """Derive the bond address/registry fields the way generate-bond-address does."""
    from jmcore.btc_script import mk_freeze_script

    seed = mnemonic_to_seed(mnemonic, "")
    master_key = HDKey.from_seed(seed)
    wallet_fingerprint = master_key.derive("m/0").fingerprint.hex()

    coin_type = 0 if network == "mainnet" else 1
    root_path = f"m/84'/{coin_type}'"
    deriv_path = f"{root_path}/0'/{FIDELITY_BOND_BRANCH}/{timenumber}"

    key = master_key.derive(deriv_path)
    pubkey_hex = key.get_public_key_bytes(compressed=True).hex()
    witness_script = mk_freeze_script(pubkey_hex, timenumber_to_timestamp(timenumber))
    address = script_to_p2wsh_address(witness_script, network)
    return wallet_fingerprint, address, witness_script, pubkey_hex


@pytest.mark.asyncio
async def test_sync_bonds_updates_registry_after_funding(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready,
    tmp_path: Path,
) -> None:
    """sync-bonds refreshes a registered bond's UTXO info once it is funded."""
    from tests.e2e.rpc_utils import mine_blocks, rpc_call

    network = "regtest"
    # Timenumber 0 = January 2020 (past locktime, valid and spendable).
    timenumber = 0
    locktime = timenumber_to_timestamp(timenumber)

    fingerprint, bond_address, witness_script, pubkey_hex = _derive_bond(
        TEST_MNEMONIC, network, timenumber
    )
    deriv_path = f"m/84'/1'/0'/{FIDELITY_BOND_BRANCH}/{timenumber}"
    logger.info(f"Bond address: {bond_address} (fingerprint={fingerprint})")

    # Record the bond in the per-wallet registry, like generate-bond-address.
    registry = load_registry(tmp_path, fingerprint, allow_legacy_fallback=False)
    registry.add_bond(
        create_bond_info(
            address=bond_address,
            locktime=locktime,
            index=timenumber,
            path=deriv_path,
            pubkey_hex=pubkey_hex,
            witness_script=witness_script,
            network=network,
        )
    )
    save_registry(registry, tmp_path, fingerprint)

    # Before funding, the registered bond must be unfunded.
    before = load_registry(tmp_path, fingerprint, allow_legacy_fallback=False)
    bond_before = before.get_bond_by_address(bond_address)
    assert bond_before is not None
    assert not bond_before.is_funded, "Bond should be unfunded before sync"

    # Fund the bond address on-chain (coinbase to the bond, then maturity blocks).
    logger.info("Funding bond address...")
    await mine_blocks(1, bond_address)
    dummy_addr = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
    await mine_blocks(110, dummy_addr)

    scan = await rpc_call("scantxoutset", ["start", [f"addr({bond_address})"]])
    utxos = scan.get("unspents", [])
    assert len(utxos) >= 1, f"Bond address should have UTXOs, got: {utxos}"
    expected_value = int(utxos[0]["amount"] * 100_000_000)

    backend_settings = ResolvedBackendSettings(
        network=network,
        bitcoin_network=network,
        backend_type="descriptor_wallet",
        rpc_url=bitcoin_rpc_config["rpc_url"],
        rpc_user=bitcoin_rpc_config["rpc_user"],
        rpc_password=bitcoin_rpc_config["rpc_password"],
        neutrino_url="",
        neutrino_add_peers=[],
        data_dir=tmp_path,
    )

    # Run the actual sync-bonds code path.
    await _sync_bonds_async(TEST_MNEMONIC, backend_settings, "")

    # The registry entry must now carry the on-chain UTXO info.
    after = load_registry(tmp_path, fingerprint, allow_legacy_fallback=False)
    bond_after = after.get_bond_by_address(bond_address)
    assert bond_after is not None
    assert bond_after.is_funded, "Bond should be funded after sync"
    assert bond_after.txid, "Bond should have a UTXO txid after sync"
    assert bond_after.value == expected_value, (
        f"Expected {expected_value}, got {bond_after.value}"
    )
    assert (bond_after.confirmations or 0) >= 1

    logger.info(
        f"sync-bonds recorded UTXO {bond_after.txid}:{bond_after.vout} "
        f"value={bond_after.value}"
    )
