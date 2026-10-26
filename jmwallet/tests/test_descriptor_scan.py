"""
Tests for descriptor-based wallet scanning.
"""

from unittest.mock import AsyncMock, Mock, patch

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
async def test_descriptor_sync_clamps_future_utxo_height_confirmations(test_mnemonic):
    """A descriptor scan cannot create negative confirmations from a future height."""
    backend = MockBackend()
    wallet = WalletService(test_mnemonic, backend, network="regtest", mixdepth_count=1)
    address = wallet.get_address(0, 0, 0)
    backend.get_block_height = AsyncMock(return_value=100)  # type: ignore[method-assign]
    backend.scan_descriptors = AsyncMock(  # type: ignore[method-assign]
        return_value={
            "success": True,
            "unspents": [
                {
                    "txid": "a" * 64,
                    "vout": 0,
                    "amount": 0.001,
                    "address": address,
                    "scriptPubKey": "0014" + "00" * 20,
                    "height": 101,
                    "desc": "",
                }
            ],
        }
    )

    synced = await wallet._sync_all_with_descriptors()

    assert synced is not None
    assert synced[0][0].confirmations == 0


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
    backend.rescan_for_recovery = AsyncMock()  # type: ignore[method-assign]
    backend.get_utxos = AsyncMock(return_value=[])  # type: ignore[method-assign]
    register_mock = Mock()
    wallet._self_register_bond_utxos = register_mock  # type: ignore[method-assign]

    discovered = await wallet.discover_fidelity_bonds()

    assert discovered == []
    setup_mock.assert_awaited_once_with(rescan=False)
    register_mock.assert_called_once_with([], require_persistence=False)
    assert wallet.fidelity_bond_locktime_cache == {}


@pytest.mark.asyncio
async def test_setup_descriptor_wallet_includes_all_bonds_without_caching_candidates(
    test_mnemonic,
):
    """Recovery imports every canonical bond descriptor but displays none as funded."""
    from jmcore.timenumber import TIMENUMBER_COUNT

    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(test_mnemonic, backend, network="regtest")

    backend.is_wallet_setup = AsyncMock(return_value=False)  # type: ignore[method-assign]
    setup_wallet_mock = AsyncMock()
    backend.setup_wallet = setup_wallet_mock  # type: ignore[method-assign]

    await wallet.setup_descriptor_wallet(include_all_fidelity_bonds=True, rescan=False)

    setup_wallet_mock.assert_awaited_once()
    assert setup_wallet_mock.await_args is not None
    descriptors = setup_wallet_mock.await_args.args[0]
    bond_descriptors = [item for item in descriptors if item["desc"].startswith("addr(")]
    assert len(bond_descriptors) == TIMENUMBER_COUNT
    assert len({item["desc"] for item in bond_descriptors}) == TIMENUMBER_COUNT
    assert wallet.fidelity_bond_locktime_cache == {}
    assert all(path[1] != 2 for path in wallet.address_cache.values())


@pytest.mark.asyncio
async def test_setup_descriptor_wallet_checks_actual_bond_descriptor_coverage(test_mnemonic):
    """Unrelated descriptors cannot hide a missing canonical recovery address."""
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(test_mnemonic, backend, network="regtest")
    canonical = [
        ("bcrt1qcanonical1", 1_893_456_000, 120),
        ("bcrt1qcanonical2", 1_896_134_400, 121),
    ]
    wallet._canonical_fidelity_bond_address_entries = lambda: canonical  # type: ignore[method-assign]

    backend.is_wallet_setup = AsyncMock(return_value=True)  # type: ignore[method-assign]
    backend.list_descriptors = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"desc": "addr(bcrt1qcanonical1)#checksum"},
            {"desc": "addr(bcrt1qunrelated)#checksum"},
        ]
    )
    setup_wallet_mock = AsyncMock()
    backend.setup_wallet = setup_wallet_mock  # type: ignore[method-assign]

    await wallet.setup_descriptor_wallet(include_all_fidelity_bonds=True, rescan=False)

    setup_wallet_mock.assert_awaited_once()
    assert setup_wallet_mock.await_args is not None
    descriptors = setup_wallet_mock.await_args.args[0]
    imported_addresses = {item["desc"] for item in descriptors if item["desc"].startswith("addr(")}
    assert imported_addresses == {"addr(bcrt1qcanonical1)", "addr(bcrt1qcanonical2)"}


@pytest.mark.asyncio
async def test_setup_descriptor_wallet_expands_undersized_regular_ranges(test_mnemonic):
    """A high descriptor count cannot hide regular ranges below the request."""
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(test_mnemonic, backend, network="regtest")
    imported = [
        {"desc": f"{item['desc']}#checksum", "range": [0, 999]}
        for item in wallet._generate_import_descriptors(1_000)
    ]
    backend.is_wallet_setup = AsyncMock(return_value=True)  # type: ignore[method-assign]
    backend.list_descriptors = AsyncMock(return_value=imported)  # type: ignore[method-assign]
    setup_wallet_mock = AsyncMock()
    backend.setup_wallet = setup_wallet_mock  # type: ignore[method-assign]

    await wallet.setup_descriptor_wallet(scan_range=2_500, rescan=False)

    setup_wallet_mock.assert_awaited_once()
    assert setup_wallet_mock.await_args is not None
    descriptors = setup_wallet_mock.await_args.args[0]
    ranged = [item for item in descriptors if "range" in item]
    assert all(item["range"] == [0, 2_499] for item in ranged)


@pytest.mark.asyncio
async def test_backend_setup_wallet_rejects_partial_descriptor_imports() -> None:
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    backend.create_wallet = AsyncMock(return_value=True)  # type: ignore[method-assign]
    backend.import_descriptors = AsyncMock(  # type: ignore[method-assign]
        return_value={"error_count": 1}
    )

    with pytest.raises(RuntimeError, match="failed to import 1 descriptor"):
        await backend.setup_wallet(["wpkh(tpub/*)"])


@pytest.mark.asyncio
async def test_backend_setup_wallet_requires_requested_full_history_rescan() -> None:
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    backend.create_wallet = AsyncMock(return_value=True)  # type: ignore[method-assign]
    backend.import_descriptors = AsyncMock(  # type: ignore[method-assign]
        return_value={"error_count": 0, "background_rescan_started": False}
    )

    with pytest.raises(RuntimeError, match="failed to start the full-history rescan"):
        await backend.setup_wallet(["wpkh(tpub/*)"])


@pytest.mark.asyncio
async def test_setup_descriptor_wallet_rescans_complete_existing_coverage(
    test_mnemonic,
) -> None:
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(
        test_mnemonic,
        backend,
        network="regtest",
        mixdepth_count=1,
        scan_range=2,
    )
    expected = wallet._generate_import_descriptors(2)
    backend.is_wallet_setup = AsyncMock(return_value=True)  # type: ignore[method-assign]
    backend.list_descriptors = AsyncMock(  # type: ignore[method-assign]
        return_value=[
            {"desc": f"{item['desc']}#checksum", "range": item["range"]} for item in expected
        ]
    )
    backend.start_background_rescan = AsyncMock(return_value=False)  # type: ignore[method-assign]
    backend.setup_wallet = AsyncMock()  # type: ignore[method-assign]

    await wallet.setup_descriptor_wallet(
        scan_range=2,
        rescan=True,
        rescan_existing=True,
    )

    backend.start_background_rescan.assert_awaited_once_with(0)
    backend.setup_wallet.assert_not_awaited()


@pytest.mark.asyncio
async def test_discover_on_light_client_propagates_query_failure(test_mnemonic) -> None:
    backend = MockBackend()
    backend.ensure_addresses_scanned = AsyncMock()  # type: ignore[method-assign]
    backend.get_utxos = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("query unavailable")
    )
    wallet = WalletService(test_mnemonic, backend, network="regtest")

    with pytest.raises(RuntimeError, match="recovery batch 0-100"):
        await wallet.discover_fidelity_bonds()


@pytest.mark.asyncio
async def test_discover_on_light_client_backfills_history_and_registers_bond(
    test_mnemonic, tmp_path
):
    """Non-descriptor discovery must backfill scanned history and persist bonds.

    Neutrino only scans new blocks for already-watched addresses, so discovery
    has to register and backfill all 960 candidates before querying. Found
    bonds must land in the per-wallet registry, otherwise the next
    registry-aware sync drops them again (light clients have no descriptor
    wallet remembering the address).
    """
    from jmcore.timenumber import TIMENUMBER_COUNT, timestamp_to_timenumber

    from jmwallet.backends.base import UTXO
    from jmwallet.wallet.address import script_to_p2wsh_address
    from jmwallet.wallet.bond_registry import load_registry

    backend = MockBackend()
    backend.supports_watch_address = True
    wallet = WalletService(test_mnemonic, backend, network="regtest", data_dir=tmp_path)

    locktime = 1893456000
    timenumber = timestamp_to_timenumber(locktime)
    # Derive the canonical bond address without the caching side effects of
    # get_fidelity_bond_address() so the cache starts empty.
    script = wallet.get_fidelity_bond_script(timenumber, locktime)
    address = script_to_p2wsh_address(script, wallet.network)

    ensure_mock = AsyncMock()
    backend.ensure_addresses_scanned = ensure_mock  # type: ignore[method-assign]

    async def get_utxos(addresses):
        if address in addresses:
            return [
                UTXO(
                    txid="e" * 64,
                    vout=0,
                    value=1_000_000,
                    address=address,
                    confirmations=100,
                    scriptpubkey="0020" + "ff" * 32,
                    height=100,
                )
            ]
        return []

    backend.get_utxos = get_utxos  # type: ignore[method-assign]

    discovered = await wallet.discover_fidelity_bonds()

    ensure_mock.assert_awaited_once()
    await_args = ensure_mock.await_args
    assert await_args is not None
    assert len(await_args.args[0]) == TIMENUMBER_COUNT
    assert [u.address for u in discovered] == [address]

    registry = load_registry(tmp_path, wallet.wallet_fingerprint, allow_legacy_fallback=False)
    bond = registry.get_bond_by_address(address)
    assert bond is not None
    assert bond.txid == "e" * 64
    assert bond.value == 1_000_000


@pytest.mark.asyncio
async def test_bond_import_failure_does_not_cache_untracked_address(test_mnemonic) -> None:
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(test_mnemonic, backend, network="regtest")
    backend.import_descriptors = AsyncMock(  # type: ignore[method-assign]
        return_value={"error_count": 1}
    )
    address = "bcrt1qfailedbond"

    with pytest.raises(RuntimeError, match="Failed to import 1 fidelity bond descriptor"):
        await wallet.import_fidelity_bond_addresses(
            [(address, 1_893_456_000, 120)],
            rescan=False,
        )

    assert address not in wallet.address_cache
    assert address not in wallet.fidelity_bond_locktime_cache


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


@pytest.mark.asyncio
async def test_sync_all_lazy_setup_rescans_when_fidelity_bonds_supplied(test_mnemonic):
    """``sync_all`` lazy setup must rescan when fidelity bonds are supplied.

    A bond's timelock address may already be funded when sync runs; importing
    its ``addr()`` descriptor without a rescan tracks it only from "now", so the
    already-confirmed bond UTXO would be invisible. The lazy setup must
    therefore pass ``rescan=True`` whenever bonds are present.
    """
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(test_mnemonic, backend, network="regtest")

    backend.is_wallet_setup = AsyncMock(return_value=False)  # type: ignore[method-assign]
    setup_mock = AsyncMock(return_value=True)
    wallet.setup_descriptor_wallet = setup_mock  # type: ignore[method-assign]
    wallet._sync_all_with_descriptors = AsyncMock(  # type: ignore[attr-defined,method-assign]
        return_value={md: [] for md in range(wallet.mixdepth_count)}
    )

    bonds = [("bcrt1qbond", 1893456000, 120)]
    await wallet.sync_all(bonds)

    setup_mock.assert_awaited_once_with(
        fidelity_bond_addresses=bonds, rescan=True, check_existing=False
    )


@pytest.mark.asyncio
async def test_sync_all_lazy_setup_skips_rescan_without_bonds(test_mnemonic):
    """``sync_all`` lazy setup must not rescan for a fresh wallet with no bonds.

    A brand-new wallet has no prior history, so the fast ``rescan=False`` setup
    must be preserved when no fidelity bonds are supplied.
    """
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(test_mnemonic, backend, network="regtest")

    backend.is_wallet_setup = AsyncMock(return_value=False)  # type: ignore[method-assign]
    setup_mock = AsyncMock(return_value=True)
    wallet.setup_descriptor_wallet = setup_mock  # type: ignore[method-assign]
    wallet._sync_all_with_descriptors = AsyncMock(  # type: ignore[attr-defined,method-assign]
        return_value={md: [] for md in range(wallet.mixdepth_count)}
    )

    await wallet.sync_all()

    setup_mock.assert_awaited_once_with(
        fidelity_bond_addresses=None, rescan=False, check_existing=False
    )


@pytest.mark.asyncio
async def test_setup_descriptor_wallet_defaults_scan_range_to_wallet_scan_range(
    test_mnemonic,
):
    """``setup_descriptor_wallet`` without an explicit ``scan_range`` must use
    the configured ``[wallet].scan_range``.

    The bond-aware sync (``sync_with_registered_bonds``) and the CLI rely on
    this default: they call ``setup_descriptor_wallet(rescan=True)`` without a
    ``scan_range``, so the descriptor import range must come from the wallet's
    configured ``scan_range`` (default 1000).
    """
    backend = DescriptorWalletBackend(
        rpc_url="http://127.0.0.1:18443",
        rpc_user="user",
        rpc_password="pass",
        wallet_name="jm_descriptor_wallet_test",
    )
    wallet = WalletService(
        test_mnemonic, backend, network="regtest", mixdepth_count=5, scan_range=1000
    )

    backend.is_wallet_setup = AsyncMock(return_value=False)  # type: ignore[method-assign]
    setup_wallet_mock = AsyncMock()
    backend.setup_wallet = setup_wallet_mock  # type: ignore[method-assign]

    # No explicit scan_range -> defaults to wallet.scan_range (1000).
    await wallet.setup_descriptor_wallet(rescan=True, check_existing=False)

    setup_wallet_mock.assert_awaited_once()
    assert setup_wallet_mock.await_args is not None
    imported_descriptors = setup_wallet_mock.await_args.args[0]
    # Ranged base descriptors must span [0, scan_range - 1] == [0, 999].
    ranged = [d for d in imported_descriptors if "range" in d]
    assert ranged, "expected ranged base descriptors"
    assert all(d["range"] == [0, 999] for d in ranged)


@pytest.mark.asyncio
async def test_neutrino_bond_discovery_scans_back_to_timelock_epoch(test_mnemonic, tmp_path):
    """Imported-wallet bond recovery on neutrino covers every possible bond.

    Bond timelocks start in January 2020 (mainnet block ~610691), well before
    the default ``tip - 105120`` lookback used for ordinary wallet syncs.
    Coverage recorded by an earlier, narrower scan must not skip the backfill.
    """
    from jmwallet.backends.neutrino import NeutrinoBackend

    tip = 921_000
    backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
    backend._server_capabilities.detected = True
    backend._server_capabilities.has_rescan_status = True
    backend._server_capabilities.has_persistent_rescan_state = True
    backend._server_capabilities.has_force_rescan = True
    backend._initial_rescan_done = True
    backend._last_rescan_height = tip
    backend.get_block_height = AsyncMock(return_value=tip)  # type: ignore[method-assign]
    backend._get_rescan_coverage = AsyncMock(return_value=(tip - 105_120, tip))  # type: ignore[method-assign]
    backend._wait_for_rescan_status = AsyncMock(  # type: ignore[method-assign]
        return_value={"in_progress": False, "last_error": ""}
    )
    backend._api_call = AsyncMock(return_value={"utxos": []})  # type: ignore[method-assign]
    wallet = WalletService(test_mnemonic, backend, network="mainnet", data_dir=tmp_path)

    canonical = list(wallet._canonical_bond_address_map())
    wallet._record_explicit_address_coverage(canonical)

    with patch("jmwallet.backends.neutrino.asyncio.sleep", new=AsyncMock()):
        await wallet.discover_fidelity_bonds()

    forced = [
        call.kwargs["data"]
        for call in backend._api_call.call_args_list
        if call.args[:2] == ("POST", "v1/rescan") and call.kwargs["data"].get("force")
    ]
    assert forced
    assert set(forced[-1]["addresses"]) == set(canonical)
    assert forced[-1]["start_height"] <= 610_691
    await backend.close()


@pytest.mark.asyncio
async def test_neutrino_bond_recovery_start_on_test_networks() -> None:
    from jmwallet.backends.neutrino import NeutrinoBackend

    for network in ("signet", "regtest"):
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network=network)
        assert await backend._resolve_scan_start_height(200_000) == 200_000 - 105_120
        assert await backend._resolve_scan_start_height(200_000, bond_recovery=True) == 0
        await backend.close()
