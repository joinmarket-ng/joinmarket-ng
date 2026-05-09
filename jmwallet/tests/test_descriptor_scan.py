"""
Tests for descriptor-based wallet scanning.
"""

from unittest.mock import AsyncMock

import pytest

from jmwallet.backends.base import BlockchainBackend
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.service import WalletService


class MockBackend(BlockchainBackend):
    """Mock backend for testing descriptor parsing."""

    async def get_utxos(self, addresses):
        return []

    async def get_address_balance(self, address):
        return 0

    async def broadcast_transaction(self, tx_hex):
        return "mock_txid"

    async def get_transaction(self, txid):
        return None

    async def estimate_fee(self, target_blocks):
        return 10

    async def get_block_height(self):
        return 100

    async def get_block_time(self, block_height):
        return 1000000

    async def get_block_hash(self, block_height):
        return "mock_hash"

    async def get_utxo(self, txid, vout):
        return None


@pytest.mark.asyncio
async def test_parse_descriptor_path(test_mnemonic):
    """Test parsing descriptor paths returned by Bitcoin Core."""
    mock_backend = MockBackend()
    wallet = WalletService(test_mnemonic, mock_backend, network="regtest")

    # Build the descriptor mapping (what we send to Bitcoin Core)
    descriptors = []
    desc_to_path = {}

    for mixdepth in range(wallet.mixdepth_count):
        xpub = wallet.get_account_xpub(mixdepth)
        desc_ext = f"wpkh({xpub}/0/*)"
        desc_int = f"wpkh({xpub}/1/*)"

        descriptors.append({"desc": desc_ext, "range": [0, 999]})
        descriptors.append({"desc": desc_int, "range": [0, 999]})

        desc_to_path[desc_ext] = (mixdepth, 0)
        desc_to_path[desc_int] = (mixdepth, 1)

    # Get actual address for mixdepth 0, change 0, index 0
    wallet.get_address(0, 0, 0)  # Cache the address
    key = wallet.master_key.derive(f"{wallet.root_path}/0'/0/0")
    pubkey_hex = key.get_public_key_bytes(compressed=True).hex()

    # Simulate what Bitcoin Core returns
    fingerprint = wallet.master_key.derive(f"{wallet.root_path}/0'").fingerprint.hex()
    simulated_desc = f"wpkh([{fingerprint}/0/0]{pubkey_hex})#checksum"

    # Parse it back
    result = wallet._parse_descriptor_path(simulated_desc, desc_to_path)

    assert result is not None, f"Failed to parse descriptor: {simulated_desc}"
    mixdepth, change, index = result
    assert mixdepth == 0, f"Expected mixdepth 0, got {mixdepth}"
    assert change == 0, f"Expected change 0, got {change}"
    assert index == 0, f"Expected index 0, got {index}"


@pytest.mark.asyncio
async def test_parse_descriptor_path_multiple_mixdepths(test_mnemonic):
    """Test parsing descriptors from different mixdepths."""
    mock_backend = MockBackend()
    wallet = WalletService(test_mnemonic, mock_backend, network="regtest")

    # Build descriptor mapping
    desc_to_path = {}
    for mixdepth in range(wallet.mixdepth_count):
        xpub = wallet.get_account_xpub(mixdepth)
        desc_ext = f"wpkh({xpub}/0/*)"
        desc_int = f"wpkh({xpub}/1/*)"
        desc_to_path[desc_ext] = (mixdepth, 0)
        desc_to_path[desc_int] = (mixdepth, 1)

    # Test mixdepth 2, change 1, index 5
    test_mixdepth = 2
    test_change = 1
    test_index = 5

    key = wallet.master_key.derive(
        f"{wallet.root_path}/{test_mixdepth}'/{test_change}/{test_index}"
    )
    pubkey_hex = key.get_public_key_bytes(compressed=True).hex()

    fingerprint = wallet.master_key.derive(f"{wallet.root_path}/{test_mixdepth}'").fingerprint.hex()
    simulated_desc = f"wpkh([{fingerprint}/{test_change}/{test_index}]{pubkey_hex})#test"

    result = wallet._parse_descriptor_path(simulated_desc, desc_to_path)

    assert result is not None
    mixdepth, change, index = result
    assert mixdepth == test_mixdepth
    assert change == test_change
    assert index == test_index


@pytest.mark.asyncio
async def test_discover_fidelity_bonds_auto_initialises_descriptor_wallet(test_mnemonic):
    """Bond discovery should set up descriptor wallets when called on a fresh service."""
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(test_mnemonic, backend, network="regtest")

    setup_mock = AsyncMock()
    wallet.setup_descriptor_wallet = setup_mock  # type: ignore[method-assign]
    backend.is_wallet_setup = AsyncMock(return_value=False)  # type: ignore[method-assign]
    wallet.import_fidelity_bond_addresses = AsyncMock(return_value=True)  # type: ignore[method-assign]
    backend.start_background_rescan = AsyncMock(return_value=None)  # type: ignore[method-assign]
    backend.wait_for_rescan_complete = AsyncMock(return_value=True)  # type: ignore[method-assign]
    backend.get_utxos = AsyncMock(return_value=[])  # type: ignore[method-assign]

    discovered = await wallet.discover_fidelity_bonds()

    assert discovered == []
    setup_mock.assert_awaited_once_with(rescan=False)


@pytest.mark.asyncio
async def test_sync_all_reinitialises_if_wallet_descriptors_do_not_match_seed(test_mnemonic):
    """sync_all should re-import descriptors when loaded wallet tracks another seed."""
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(test_mnemonic, backend, network="regtest")

    backend.is_wallet_setup = AsyncMock(return_value=True)  # type: ignore[method-assign]
    backend.list_descriptors = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"desc": "wpkh(tpubD6NzFakeDescriptor/0/*)#abcd1234"}]
    )
    setup_mock = AsyncMock(return_value=True)
    wallet.setup_descriptor_wallet = setup_mock  # type: ignore[method-assign]
    wallet._sync_all_with_descriptors = AsyncMock(  # type: ignore[attr-defined,method-assign]
        return_value={md: [] for md in range(wallet.mixdepth_count)}
    )

    await wallet.sync_all()

    setup_mock.assert_awaited_once()
