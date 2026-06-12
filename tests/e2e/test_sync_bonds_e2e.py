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
from jmwallet.wallet.service import FIDELITY_BOND_BRANCH, WalletService

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

    # Fund the bond address on-chain.
    logger.info("Funding bond address...")
    from tests.e2e.rpc_utils import send_from_test_funder

    dummy_addr = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
    funded = await send_from_test_funder(bond_address, 0.01, confirmations=1)
    if not funded:
        await mine_blocks(1, bond_address)
        await mine_blocks(110, dummy_addr)

    scan = await rpc_call("scantxoutset", ["start", [f"addr({bond_address})"]])
    utxos = scan.get("unspents", [])
    assert len(utxos) >= 1, f"Bond address should have UTXOs, got: {utxos}"
    # The bond address is deterministic (mnemonic + timenumber); on a reused
    # regtest node it may carry coinbase UTXOs from earlier runs. The registry
    # records the single highest-value UTXO, so compare against the max.
    expected_value = max(int(u["amount"] * 100_000_000) for u in utxos)

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


@pytest.mark.asyncio
async def test_sync_bonds_finds_bond_when_base_wallet_already_set_up(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready,
    tmp_path: Path,
) -> None:
    """Regression: a bond funded after the base wallet was set up is still found.

    Reproduces the reported bug where a freshly created and funded fidelity bond
    shows as ``locked`` with 0 sats and ``sync-bonds`` does not help (only the
    slow ``recover-bonds`` full scan finds it). The root cause was that the CLI
    ``sync-bonds`` path used ``sync_all``, whose lazy descriptor-wallet setup
    imports the bond's watch-only ``addr()`` descriptor with ``rescan=False``
    (Bitcoin Core tracks it only from "now"), so an already-confirmed bond UTXO
    never entered ``listunspent``. Once imported that way the descriptor count
    also satisfied the old count-based readiness check in ``info``/``send``/
    ``freeze``, so they skipped the rescanning import too and the bond stayed
    invisible everywhere until a full rescan. The fix routes every CLI command
    through the bond-aware sync, which imports missing bond descriptors with a
    rescan and detects them by the actual ``addr()`` descriptor set.

    The scenario here sets up the base descriptor wallet *first* (the common
    case: a wallet already in use), and only then creates and funds the bond.
    """
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
    )

    from tests.e2e.rpc_utils import mine_blocks, rpc_call, send_from_test_funder

    network = "regtest"
    # Use a distinct (past) timenumber so this test's bond does not collide with
    # the other test's coinbase UTXO on the shared regtest node.
    timenumber = 3  # April 2020, valid past locktime
    locktime = timenumber_to_timestamp(timenumber)

    fingerprint, bond_address, witness_script, pubkey_hex = _derive_bond(
        TEST_MNEMONIC, network, timenumber
    )
    deriv_path = f"m/84'/1'/0'/{FIDELITY_BOND_BRANCH}/{timenumber}"
    wallet_name = generate_wallet_name(fingerprint, network)
    logger.info(f"Bond address: {bond_address} (fingerprint={fingerprint})")

    rpc_url = bitcoin_rpc_config["rpc_url"]
    rpc_user = bitcoin_rpc_config["rpc_user"]
    rpc_password = bitcoin_rpc_config["rpc_password"]

    # Start from a clean Core wallet so this run actually performs base setup.
    try:
        await rpc_call("unloadwallet", [wallet_name])
    except Exception:
        pass

    # 1) Set up the base descriptor wallet FIRST, with no bonds. This imports
    #    the standard mixdepth descriptors (and makes Core over-count them),
    #    which is exactly the state that used to make the bond import be skipped.
    setup_backend = DescriptorWalletBackend(
        rpc_url=rpc_url,
        rpc_user=rpc_user,
        rpc_password=rpc_password,
        wallet_name=wallet_name,
    )
    setup_wallet = WalletService(
        mnemonic=TEST_MNEMONIC,
        backend=setup_backend,
        network=network,
        mixdepth_count=5,
        data_dir=tmp_path,
    )
    await setup_backend.create_wallet()
    await setup_wallet.setup_descriptor_wallet(
        rescan=False, fidelity_bond_addresses=None
    )
    base_descriptors = await setup_backend.list_descriptors()
    # The bond's addr() descriptor must NOT be present yet.
    assert not any(
        f"addr({bond_address})" in str(d.get("desc", "")) for d in base_descriptors
    ), "Bond descriptor should not be imported during base setup"
    await setup_wallet.close()

    # 2) Record the bond in the per-wallet registry (as generate-bond-address does).
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

    # 3) Fund the bond on-chain AFTER base setup.
    logger.info("Funding bond address...")
    from tests.e2e.rpc_utils import rpc_call

    funded = await send_from_test_funder(bond_address, 0.01, confirmations=1)
    if not funded:
        await mine_blocks(1, bond_address)
        await mine_blocks(110, "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080")

    scan = await rpc_call("scantxoutset", ["start", [f"addr({bond_address})"]])
    unspents = scan.get("unspents", [])
    assert len(unspents) >= 1, f"Bond address should have UTXOs, got: {unspents}"
    unspent_values = [int(u["amount"] * 100_000_000) for u in unspents]
    # The bond address is deterministic (mnemonic + timenumber), so on a reused
    # regtest node it may carry coinbase UTXOs from earlier runs. The registry
    # tracks the single highest-value UTXO per address; the display sums them.
    expected_best_value = max(unspent_values)
    expected_total_value = sum(unspent_values)

    backend_settings = ResolvedBackendSettings(
        network=network,
        bitcoin_network=network,
        backend_type="descriptor_wallet",
        rpc_url=rpc_url,
        rpc_user=rpc_user,
        rpc_password=rpc_password,
        neutrino_url="",
        neutrino_add_peers=[],
        data_dir=tmp_path,
    )

    # 4) Run the real sync-bonds command body. Before the fix this left the bond
    #    unfunded (the addr() descriptor was never imported).
    await _sync_bonds_async(TEST_MNEMONIC, backend_settings, "")

    after = load_registry(tmp_path, fingerprint, allow_legacy_fallback=False)
    bond_after = after.get_bond_by_address(bond_address)
    assert bond_after is not None
    assert bond_after.is_funded, (
        "Bond funded after the base wallet was set up must be found by sync-bonds "
        "(regression: it showed as locked with 0 sats)"
    )
    assert bond_after.value == expected_best_value, (
        f"Expected best UTXO value {expected_best_value}, got {bond_after.value}"
    )

    # 5) The bond must also be visible through the display path that powers
    #    ``jm-wallet info --extended`` (address shown locked WITH its balance,
    #    not 0 sats). A fresh wallet using the bond-aware sync surfaces it.
    display_backend = DescriptorWalletBackend(
        rpc_url=rpc_url,
        rpc_user=rpc_user,
        rpc_password=rpc_password,
        wallet_name=wallet_name,
    )
    display_wallet = WalletService(
        mnemonic=TEST_MNEMONIC,
        backend=display_backend,
        network=network,
        mixdepth_count=5,
        data_dir=tmp_path,
    )
    try:
        await display_backend.create_wallet()
        await display_wallet.sync_with_registered_bonds()
        bond_infos = display_wallet.get_fidelity_bond_addresses_info(6)
        shown = next((b for b in bond_infos if b.address == bond_address), None)
        assert shown is not None, "Bond address missing from info --extended display"
        assert shown.balance == expected_total_value, (
            f"info --extended must show the bond balance, got {shown.balance} sats"
        )
        assert shown.is_bond and shown.locktime == locktime
    finally:
        await display_wallet.close()

    logger.info(
        f"Bond visible after base-first setup: registry value={bond_after.value}, "
        f"display balance={shown.balance}"
    )
