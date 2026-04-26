"""
Tests for DescriptorWalletBackend.

Unit tests mock Bitcoin Core RPC responses.
Integration tests (marked with @pytest.mark.docker) require a running Bitcoin Core instance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from _jmwallet_test_helpers import (
    TEST_BOND_ADDRESS,
    TEST_BOND_LOCKTIME,
    TEST_FAKE_TXID,
    TEST_MNEMONIC,
    TEST_RPC_PASSWORD,
    TEST_RPC_URL,
    TEST_RPC_USER,
    make_mock_rpc,
)

from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    generate_wallet_name,
    get_mnemonic_fingerprint,
)


class TestDescriptorWalletBackendUnit:
    """Unit tests for DescriptorWalletBackend (no Docker required)."""

    def test_init(self):
        """Test backend initialization."""
        backend = DescriptorWalletBackend(
            rpc_url=TEST_RPC_URL,
            rpc_user=TEST_RPC_USER,
            rpc_password=TEST_RPC_PASSWORD,
            wallet_name="test_wallet",
        )
        assert backend.rpc_url == TEST_RPC_URL
        assert backend.wallet_name == "test_wallet"
        assert backend._wallet_loaded is False
        assert backend._descriptors_imported is False

    def test_get_wallet_url(self):
        """Test wallet-specific URL generation."""
        backend = DescriptorWalletBackend(
            rpc_url=TEST_RPC_URL,
            wallet_name="my_wallet",
        )
        assert backend._get_wallet_url() == f"{TEST_RPC_URL}/wallet/my_wallet"

    @pytest.mark.asyncio
    async def test_create_wallet_already_loaded(self):
        """Test create_wallet when wallet is already loaded."""
        backend = DescriptorWalletBackend(wallet_name="existing_wallet")
        backend._rpc_call = AsyncMock(return_value=["existing_wallet", "other_wallet"])

        result = await backend.create_wallet()

        assert result is True
        assert backend._wallet_loaded is True
        backend._rpc_call.assert_called_once_with("listwallets", use_wallet=False)

    @pytest.mark.asyncio
    async def test_create_wallet_load_existing(self):
        """Test create_wallet loading an existing wallet file."""
        backend = DescriptorWalletBackend(wallet_name="stored_wallet")

        call_count = 0

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            nonlocal call_count
            call_count += 1
            if method == "listwallets":
                return []  # Not loaded
            elif method == "loadwallet":
                return {"name": "stored_wallet"}
            raise ValueError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        result = await backend.create_wallet()

        assert result is True
        assert backend._wallet_loaded is True

    @pytest.mark.asyncio
    async def test_create_wallet_new(self):
        """Test create_wallet creating a new wallet."""
        backend = DescriptorWalletBackend(wallet_name="new_wallet")

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "listwallets":
                return []
            elif method == "loadwallet":
                raise ValueError("Wallet not found")
            elif method == "createwallet":
                # Verify descriptor wallet params
                assert params[0] == "new_wallet"  # wallet_name
                assert params[1] is True  # disable_private_keys
                assert params[2] is True  # blank
                assert params[5] is True  # descriptors (MUST be True)
                return {"name": "new_wallet", "warning": ""}
            raise ValueError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        result = await backend.create_wallet()

        assert result is True
        assert backend._wallet_loaded is True

    @pytest.mark.asyncio
    async def test_create_wallet_http_500_with_rpc_error(self):
        """Test create_wallet handles HTTP 500 with JSON-RPC error correctly."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")

        call_count = 0

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            nonlocal call_count
            call_count += 1
            if method == "listwallets":
                return []
            elif method == "loadwallet":
                # Simulate Bitcoin Core returning 500 status but with valid JSON-RPC error
                # This should be converted to ValueError by the fixed _rpc_call
                raise ValueError("RPC error -18: Wallet file verification failed")
            elif method == "createwallet":
                return {"name": "test_wallet", "warning": ""}
            raise ValueError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        result = await backend.create_wallet()

        assert result is True
        assert backend._wallet_loaded is True
        # Should have called: listwallets, loadwallet (failed), createwallet
        assert call_count == 3

    # ------------------------------------------------------------------
    # Regression tests for issue #465: RPC error -4 "Wallet already loading"
    # ------------------------------------------------------------------

    def test_is_wallet_loading_error_detects_rpc_minus_4(self):
        """Static helper must match the error string Bitcoin Core emits."""
        # Actual format from Bitcoin Core ("Wallet already loading.")
        assert DescriptorWalletBackend._is_wallet_loading_error(
            ValueError("RPC error -4: Wallet already loading.")
        )
        # Variant phrasing seen across Core versions
        assert DescriptorWalletBackend._is_wallet_loading_error(
            ValueError("Wallet is already being loaded")
        )
        # Unrelated errors must not trigger the retry path
        assert not DescriptorWalletBackend._is_wallet_loading_error(
            ValueError("RPC error -18: Wallet not found")
        )
        assert not DescriptorWalletBackend._is_wallet_loading_error(
            ValueError("some generic failure")
        )

    @pytest.mark.asyncio
    async def test_create_wallet_retries_on_already_loading(self, monkeypatch):
        """When Bitcoin Core reports ``RPC error -4: Wallet already loading``,
        create_wallet should poll listwallets with backoff and succeed once
        the wallet finishes loading, instead of propagating the error and
        crashing the caller with a traceback (issue #465)."""
        backend = DescriptorWalletBackend(wallet_name="slow_wallet")

        # Skip real sleeps to keep the test fast.
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        call_log: list[str] = []
        listwallets_calls = {"count": 0}

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            call_log.append(method)
            if method == "listwallets":
                listwallets_calls["count"] += 1
                # First call: wallet not loaded yet.
                # Second call (inside retry poll loop): wallet has finished loading.
                if listwallets_calls["count"] == 1:
                    return []
                return ["slow_wallet"]
            if method == "loadwallet":
                # Core is still loading it from the previous (timed-out) request.
                raise ValueError("RPC error -4: Wallet already loading.")
            raise AssertionError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        result = await backend.create_wallet()

        assert result is True
        assert backend._wallet_loaded is True
        # We must have polled listwallets at least twice: initial check +
        # one poll that finds the wallet now loaded.
        assert listwallets_calls["count"] >= 2
        # loadwallet must have been attempted.
        assert "loadwallet" in call_log
        # createwallet must NOT have been called -- the wallet was already there.
        assert "createwallet" not in call_log

    @pytest.mark.asyncio
    async def test_create_wallet_gives_up_after_sustained_already_loading(self, monkeypatch):
        """If the "already loading" state never clears within the retry
        schedule, create_wallet must raise a clear error (not silently
        crash with a traceback deep in Bitcoin Core response parsing).
        """
        backend = DescriptorWalletBackend(wallet_name="stuck_wallet")
        monkeypatch.setattr("asyncio.sleep", AsyncMock(return_value=None))

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "listwallets":
                return []  # never appears
            if method == "loadwallet":
                raise ValueError("RPC error -4: Wallet already loading.")
            raise AssertionError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        with pytest.raises(ValueError, match="still loading"):
            await backend.create_wallet()

    @pytest.mark.asyncio
    async def test_import_descriptors(self, mock_backend: DescriptorWalletBackend):
        """Test importing descriptors into wallet."""
        backend = mock_backend

        descriptors = [
            {"desc": "wpkh(xpub.../0/*)", "range": [0, 999]},
            {"desc": "wpkh(xpub.../1/*)", "range": [0, 999]},
        ]

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "getdescriptorinfo":
                # Return descriptor with checksum
                desc = params[0]
                return {"descriptor": f"{desc}#abcd1234"}
            elif method == "importdescriptors":
                import_reqs = params[0]
                # Return success for all
                return [{"success": True} for _ in import_reqs]
            raise ValueError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        result = await backend.import_descriptors(descriptors)

        assert result["success_count"] == 2
        assert result["error_count"] == 0
        assert backend._descriptors_imported is True

    @pytest.mark.asyncio
    async def test_import_descriptors_partial_failure(self, mock_backend: DescriptorWalletBackend):
        """Test importing descriptors with some failures."""
        backend = mock_backend

        descriptors = ["desc1", "desc2", "desc3"]

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "getdescriptorinfo":
                return {"descriptor": f"{params[0]}#check"}
            elif method == "importdescriptors":
                return [
                    {"success": True},
                    {"success": False, "error": {"message": "Invalid descriptor"}},
                    {"success": True},
                ]
            raise ValueError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        result = await backend.import_descriptors(descriptors)

        assert result["success_count"] == 2
        assert result["error_count"] == 1
        assert backend._descriptors_imported is False

    @pytest.mark.asyncio
    async def test_import_descriptors_wallet_not_loaded(self):
        """Test import_descriptors raises error if wallet not loaded."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = False

        with pytest.raises(RuntimeError, match="Wallet not loaded"):
            await backend.import_descriptors(["desc1"])

    @pytest.mark.asyncio
    async def test_import_descriptors_rescan_timestamps(
        self, mock_backend: DescriptorWalletBackend
    ):
        """Test that rescan parameter correctly sets timestamp."""
        backend = mock_backend

        captured_requests = []

        async def mock_rpc(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "getdescriptorinfo":
                return {"descriptor": f"{params[0]}#check"}  # type: ignore
            elif method == "importdescriptors":
                # Capture the import requests to verify timestamp
                captured_requests.extend(params[0])  # type: ignore
                return [{"success": True} for _ in params[0]]  # type: ignore
            elif method == "listdescriptors":
                return {"descriptors": [{"desc": "test"}]}
            raise ValueError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc  # type: ignore

        # Test rescan=True (should use timestamp=0)
        captured_requests.clear()
        await backend.import_descriptors(["desc1"], rescan=True)
        assert len(captured_requests) == 1
        assert captured_requests[0]["timestamp"] == 0, "rescan=True should use timestamp=0"

        # Test rescan=False (should use timestamp="now")
        captured_requests.clear()
        await backend.import_descriptors(["desc2"], rescan=False)
        assert len(captured_requests) == 1
        assert captured_requests[0]["timestamp"] == "now", "rescan=False should use timestamp='now'"

        # Test explicit timestamp override
        captured_requests.clear()
        await backend.import_descriptors(["desc3"], rescan=True, timestamp=1234567890)
        assert len(captured_requests) == 1
        assert captured_requests[0]["timestamp"] == 1234567890, (
            "explicit timestamp should override rescan"
        )

    @pytest.mark.asyncio
    async def test_import_descriptors_recovers_from_http_read_timeout(
        self, mock_backend: DescriptorWalletBackend
    ) -> None:
        """Regression test for issue #472.

        The first ``importdescriptors`` call with smart-scan can keep the HTTP
        connection open longer than the configured read timeout because the
        rescan it triggers runs synchronously inside the RPC. Instead of
        bubbling up an opaque ``httpx.ReadTimeout``, the backend should wait
        for the rescan to finish and confirm the import via ``listdescriptors``.
        """
        import httpx

        backend = mock_backend

        rescan_states = iter(
            [
                # First poll: rescan is in progress
                {"in_progress": True, "progress": 0.4},
                # Second poll: rescan finished
                {"in_progress": False, "progress": 1.0},
            ]
        )

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "getdescriptorinfo":
                return {"descriptor": f"{params[0]}#check"}
            if method == "importdescriptors":
                # Simulate Bitcoin Core taking longer than IMPORT_RPC_TIMEOUT.
                raise httpx.ReadTimeout("simulated read timeout", request=None)
            if method == "getwalletinfo":
                state = next(rescan_states, {"in_progress": False})
                if state["in_progress"]:
                    return {
                        "scanning": {
                            "duration": 30,
                            "progress": state["progress"],
                        }
                    }
                return {"scanning": False}
            if method == "listdescriptors":
                return {"descriptors": [{"desc": "wpkh(xpub.../0/*)#check"}]}
            raise ValueError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        # Speed the wait_for_rescan_complete polling loop up so the test is
        # quick. The default 5s poll + 30s grace would otherwise dominate.
        original_wait = backend.wait_for_rescan_complete

        async def fast_wait(**kwargs):
            kwargs["poll_interval"] = 0.01
            kwargs.setdefault("startup_grace_period", 0.05)
            return await original_wait(**kwargs)

        backend.wait_for_rescan_complete = fast_wait  # type: ignore[method-assign]

        result = await backend.import_descriptors(
            [{"desc": "wpkh(xpub.../0/*)", "range": [0, 999]}],
            rescan=True,
            smart_scan=False,  # avoid the extra _get_smart_scan_timestamp call
            background_full_rescan=False,
        )

        assert result["success_count"] == 1
        assert result["error_count"] == 0
        assert backend._descriptors_imported is True

    @pytest.mark.asyncio
    async def test_get_utxos(self, mock_backend: DescriptorWalletBackend):
        """Test getting UTXOs via listunspent."""
        backend = mock_backend

        mock_utxos = [
            {
                "txid": "abc123",
                "vout": 0,
                "amount": 0.01,
                "address": "bc1qtest1",
                "confirmations": 6,
                "scriptPubKey": "0014...",
            },
            {
                "txid": "def456",
                "vout": 1,
                "amount": 0.02,
                "address": "bc1qtest2",
                "confirmations": 0,  # Unconfirmed
                "scriptPubKey": "0014...",
            },
        ]

        backend._rpc_call = make_mock_rpc(
            {
                "getblockchaininfo": {"blocks": 1000},
                "listunspent": mock_utxos,
            }
        )

        utxos = await backend.get_utxos(["bc1qtest1", "bc1qtest2"])

        assert len(utxos) == 2
        assert utxos[0].txid == "abc123"
        assert utxos[0].value == 1_000_000  # 0.01 BTC in sats
        assert utxos[0].confirmations == 6
        # height = tip (1000) - confirmations (6) + 1 = 995
        assert utxos[0].height == 995

        assert utxos[1].txid == "def456"
        assert utxos[1].value == 2_000_000
        assert utxos[1].confirmations == 0  # Unconfirmed visible
        assert utxos[1].height is None

    @pytest.mark.asyncio
    async def test_get_utxos_filter_addresses(self, mock_backend: DescriptorWalletBackend):
        """Test that get_utxos passes addresses to Bitcoin Core for filtering.

        When addresses are provided, we pass them to listunspent RPC and Bitcoin Core
        does the filtering. The mock simulates Bitcoin Core's behavior of returning
        only matching UTXOs.
        """
        backend = mock_backend

        # Simulate Bitcoin Core returning only the filtered UTXO
        # (Bitcoin Core does the filtering when addresses are provided)
        filtered_utxos = [
            {"txid": "abc", "vout": 0, "amount": 0.01, "address": "bc1qtest1", "confirmations": 1},
        ]

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "getblockchaininfo":
                return {"blocks": 1000}
            elif method == "listunspent":
                # Verify that addresses are passed to Bitcoin Core
                assert params is not None
                assert len(params) >= 3, "Should have minconf, maxconf, addresses"
                addresses = params[2]
                assert addresses == ["bc1qtest1"], "Should pass addresses to Bitcoin Core"
                return filtered_utxos
            raise ValueError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        # Filter to only bc1qtest1
        utxos = await backend.get_utxos(["bc1qtest1"])

        assert len(utxos) == 1
        assert utxos[0].address == "bc1qtest1"
        assert utxos[0].height == 1000  # 1000 - 1 + 1

    @pytest.mark.asyncio
    async def test_get_utxos_no_filter(self, mock_backend: DescriptorWalletBackend):
        """Test that get_utxos returns all UTXOs when no addresses provided.

        When addresses list is empty, we omit the addresses parameter entirely
        so Bitcoin Core returns all wallet UTXOs.
        """
        backend = mock_backend

        all_utxos = [
            {"txid": "abc", "vout": 0, "amount": 0.01, "address": "bc1qtest1", "confirmations": 1},
            {"txid": "def", "vout": 0, "amount": 0.02, "address": "bc1qtest2", "confirmations": 1},
            {"txid": "ghi", "vout": 0, "amount": 0.03, "address": "bc1qtest3", "confirmations": 1},
        ]

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "getblockchaininfo":
                return {"blocks": 1000}
            elif method == "listunspent":
                # When no addresses, params should only have minconf and maxconf
                assert params is not None
                assert len(params) == 2, f"Should only have minconf, maxconf but got {params}"
                return all_utxos
            raise ValueError(f"Unexpected method: {method}")

        backend._rpc_call = mock_rpc

        # Get all UTXOs (empty address list)
        utxos = await backend.get_utxos([])

        assert len(utxos) == 3

    @pytest.mark.asyncio
    async def test_get_wallet_balance(self, mock_backend: DescriptorWalletBackend):
        """Test getting wallet balance."""
        backend = mock_backend

        mock_balances = {
            "mine": {
                "trusted": 1.5,
                "untrusted_pending": 0.1,
            }
        }

        backend._rpc_call = AsyncMock(return_value=mock_balances)

        balance = await backend.get_wallet_balance()

        assert balance["confirmed"] == 150_000_000  # 1.5 BTC in sats
        assert balance["unconfirmed"] == 10_000_000  # 0.1 BTC in sats
        assert balance["total"] == 160_000_000

    @pytest.mark.asyncio
    async def test_get_transaction_from_wallet(self, mock_backend: DescriptorWalletBackend):
        """Test getting transaction that exists in wallet."""
        backend = mock_backend

        mock_tx = {
            "confirmations": 10,
            "blockheight": 800000,
            "blocktime": 1700000000,
            "hex": "0100000001...",
        }

        backend._rpc_call = AsyncMock(return_value=mock_tx)

        tx = await backend.get_transaction("txid123")

        assert tx is not None
        assert tx.txid == "txid123"
        assert tx.confirmations == 10
        assert tx.block_height == 800000

    @pytest.mark.asyncio
    async def test_rescan_blockchain(self, mock_backend: DescriptorWalletBackend):
        """Test blockchain rescan."""
        backend = mock_backend
        chain_tip = 800000

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "getblockchaininfo":
                return {"blocks": chain_tip}
            return {"start_height": 0, "stop_height": chain_tip}

        backend._rpc_call = AsyncMock(side_effect=mock_rpc)

        result = await backend.rescan_blockchain(start_height=700000)

        assert result["start_height"] == 0
        backend._rpc_call.assert_called()

    @pytest.mark.asyncio
    async def test_rescan_blockchain_clamps_above_tip(self, mock_backend: DescriptorWalletBackend):
        """Rescan height above the chain tip is clamped to the current tip."""
        backend = mock_backend
        chain_tip = 50000  # signet — much lower than mainnet heights

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "getblockchaininfo":
                return {"blocks": chain_tip}
            # Verify the height passed to rescanblockchain was clamped
            assert params == [chain_tip], f"Expected [{chain_tip}], got {params}"
            return {"start_height": chain_tip, "stop_height": chain_tip}

        backend._rpc_call = AsyncMock(side_effect=mock_rpc)

        # 481824 is the mainnet SegWit activation height; JAM sends this by default
        result = await backend.rescan_blockchain(start_height=481824)

        assert result["start_height"] == chain_tip

    @pytest.mark.asyncio
    async def test_rescan_blockchain_clamps_negative(self, mock_backend: DescriptorWalletBackend):
        """Negative rescan height is clamped to 0."""
        backend = mock_backend
        chain_tip = 100000

        async def mock_rpc(method, params=None, client=None, use_wallet=True):
            if method == "getblockchaininfo":
                return {"blocks": chain_tip}
            assert params == [0], f"Expected [0], got {params}"
            return {"start_height": 0, "stop_height": chain_tip}

        backend._rpc_call = AsyncMock(side_effect=mock_rpc)

        result = await backend.rescan_blockchain(start_height=-1)

        assert result["start_height"] == 0

    def test_can_provide_neutrino_metadata(self):
        """Test that backend can provide Neutrino metadata."""
        backend = DescriptorWalletBackend()
        assert backend.can_provide_neutrino_metadata() is True

    @pytest.mark.asyncio
    async def test_list_descriptors(self):
        """Test listing descriptors from wallet."""
        backend = DescriptorWalletBackend()
        backend._wallet_loaded = True
        backend._rpc_call = AsyncMock(
            return_value={
                "descriptors": [
                    {"desc": "wpkh(xpub.../0/*)#checksum", "active": True},
                    {"desc": "wpkh(xpub.../1/*)#checksum", "active": True},
                ]
            }
        )

        descriptors = await backend.list_descriptors()
        assert len(descriptors) == 2
        assert descriptors[0]["desc"].startswith("wpkh")
        assert descriptors[0]["active"] is True

    @pytest.mark.asyncio
    async def test_is_wallet_setup_true(self):
        """Test checking if wallet is set up (positive case)."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._rpc_call = AsyncMock(
            side_effect=[
                ["test_wallet"],  # listwallets
                {
                    "descriptors": [
                        {"desc": "wpkh(xpub.../0/*)#checksum", "active": True},
                        {"desc": "wpkh(xpub.../1/*)#checksum", "active": True},
                    ]
                },  # listdescriptors
            ]
        )

        is_ready = await backend.is_wallet_setup(expected_descriptor_count=2)
        assert is_ready is True

    @pytest.mark.asyncio
    async def test_is_wallet_setup_false_no_wallet(self):
        """Test checking if wallet is set up (wallet doesn't exist)."""
        backend = DescriptorWalletBackend(wallet_name="nonexistent")
        backend._rpc_call = AsyncMock(
            side_effect=[
                [],  # listwallets - wallet not loaded
                ValueError("Wallet file verification failed"),  # loadwallet fails
            ]
        )

        is_ready = await backend.is_wallet_setup()
        assert is_ready is False

    @pytest.mark.asyncio
    async def test_is_wallet_setup_false_no_descriptors(self):
        """Test checking if wallet is set up (wallet exists but no descriptors)."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._rpc_call = AsyncMock(
            side_effect=[
                ["test_wallet"],  # listwallets
                {"descriptors": []},  # listdescriptors - empty
            ]
        )

        is_ready = await backend.is_wallet_setup()
        assert is_ready is False


class TestWalletNameGeneration:
    """Tests for wallet name generation utilities."""

    def test_get_mnemonic_fingerprint(self):
        """Test mnemonic fingerprint generation."""
        mnemonic = TEST_MNEMONIC
        fp = get_mnemonic_fingerprint(mnemonic)

        assert len(fp) == 8
        assert fp.isalnum()

        # Same mnemonic should give same fingerprint
        assert get_mnemonic_fingerprint(mnemonic) == fp

        # Different mnemonic should give different fingerprint
        other = "zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo zoo wrong"
        assert get_mnemonic_fingerprint(other) != fp

    def test_generate_wallet_name(self):
        """Test wallet name generation."""
        name = generate_wallet_name("abc12345", "mainnet")
        assert name == "jm_abc12345_mainnet"

        name = generate_wallet_name("def67890", "testnet")
        assert name == "jm_def67890_testnet"

        name = generate_wallet_name("xyz00000", "regtest")
        assert name == "jm_xyz00000_regtest"


@pytest.mark.docker
@pytest.mark.asyncio
async def test_descriptor_wallet_backend_integration():
    """Integration test requiring Docker Bitcoin Core service."""
    import uuid

    # Use unique wallet name to avoid conflicts
    wallet_name = f"jm_test_{uuid.uuid4().hex[:8]}"

    backend = DescriptorWalletBackend(
        rpc_url=TEST_RPC_URL,
        rpc_user=TEST_RPC_USER,
        rpc_password=TEST_RPC_PASSWORD,
        wallet_name=wallet_name,
    )

    try:
        # Check connection first
        try:
            height = await backend.get_block_height()
            assert height >= 0
        except Exception:
            pytest.skip(
                "Bitcoin Core not available at localhost:18443. "
                "Start with: docker compose up -d bitcoin"
            )
            return

        # Create wallet
        result = await backend.create_wallet(disable_private_keys=True)
        assert result is True
        assert backend._wallet_loaded is True

        # Import a simple descriptor
        # Using a known valid testnet xpub from BIP32 test vectors
        # This won't have funds, just testing the import mechanism
        test_xpub = (
            "tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1xkh56hjod1Rh"
            "GjxbaTLV3X4FyWuejifB9jusQ46QzG87VKp"
        )
        # Bitcoin Core will add checksum automatically via getdescriptorinfo
        descriptors = [
            {"desc": f"wpkh({test_xpub}/0/*)", "range": [0, 10], "timestamp": "now"},
        ]

        import_result = await backend.import_descriptors(descriptors, rescan=False)
        assert import_result["success_count"] >= 1

        # Test listunspent (should return empty for this test wallet)
        utxos = await backend.get_utxos([])
        assert isinstance(utxos, list)

        # Test balance
        balance = await backend.get_wallet_balance()
        assert "total" in balance

        # Test fee estimation
        fee = await backend.estimate_fee(6)
        assert isinstance(fee, float)

    finally:
        # Cleanup: unload wallet
        try:
            await backend.unload_wallet()
        except Exception:
            pass
        await backend.close()


@pytest.mark.docker
@pytest.mark.asyncio
async def test_descriptor_wallet_with_funds():
    """Test descriptor wallet with actual funded addresses.

    This test:
    1. Creates a descriptor wallet
    2. Generates an address
    3. Mines to that address
    4. Verifies UTXO is visible via listunspent
    """
    import uuid

    wallet_name = f"jm_funded_{uuid.uuid4().hex[:8]}"

    # Create backend WITHOUT disable_private_keys so we can generate addresses
    backend = DescriptorWalletBackend(
        rpc_url=TEST_RPC_URL,
        rpc_user=TEST_RPC_USER,
        rpc_password=TEST_RPC_PASSWORD,
        wallet_name=wallet_name,
    )

    try:
        # Check connection
        try:
            await backend.get_block_height()
        except Exception:
            pytest.skip("Bitcoin Core not available")
            return

        # Create wallet with private keys enabled for address generation
        await backend._rpc_call(
            "createwallet",
            [wallet_name, False, False, "", False, True],  # descriptors=True
            use_wallet=False,
        )
        backend._wallet_loaded = True

        # Generate a new address from the wallet
        address = await backend.get_new_address("bech32")
        assert address.startswith("bcrt1")

        # Mine a block to this address
        await backend._rpc_call("generatetoaddress", [1, address], use_wallet=False)

        # Mine more blocks to make UTXO spendable (100 confirmations for coinbase)
        # For this test, we just need 1 block, the UTXO should be visible
        await backend._rpc_call(
            "generatetoaddress",
            [100, "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"],
            use_wallet=False,
        )

        # Check UTXOs
        utxos = await backend.get_utxos([address])
        assert len(utxos) >= 1
        assert utxos[0].address == address
        assert utxos[0].value > 0  # Should have coinbase reward

        # Check balance
        balance = await backend.get_wallet_balance()
        assert balance["total"] > 0

    finally:
        try:
            await backend.unload_wallet()
        except Exception:
            pass
        await backend.close()


@pytest.mark.docker
@pytest.mark.asyncio
async def test_descriptor_wallet_service_integration():
    """Test WalletService with DescriptorWalletBackend - full workflow.

    This test validates the complete flow:
    1. Create a WalletService with DescriptorWalletBackend
    2. Check if descriptor wallet is ready
    3. Setup descriptor wallet (import descriptors)
    4. Verify descriptors were imported
    5. Sync wallet and find UTXOs
    """
    import uuid

    from jmwallet.backends.descriptor_wallet import (
        get_mnemonic_fingerprint,
    )
    from jmwallet.wallet.service import WalletService

    # Use the standard test mnemonic which has funds in regtest
    mnemonic = TEST_MNEMONIC
    network = "regtest"

    # Generate deterministic wallet name
    fingerprint = get_mnemonic_fingerprint(mnemonic, "")
    # Use unique suffix to avoid conflicts with other tests
    wallet_name = f"jm_{fingerprint}_{uuid.uuid4().hex[:8]}_test"

    backend = DescriptorWalletBackend(
        rpc_url=TEST_RPC_URL,
        rpc_user=TEST_RPC_USER,
        rpc_password=TEST_RPC_PASSWORD,
        wallet_name=wallet_name,
    )

    try:
        # Check connection
        try:
            await backend.get_block_height()
        except Exception:
            pytest.skip("Bitcoin Core not available")
            return

        # Create wallet service
        wallet = WalletService(
            mnemonic=mnemonic,
            backend=backend,
            network=network,
            mixdepth_count=5,
            passphrase="",
        )

        # Check initial state - wallet should not be ready
        is_ready = await wallet.is_descriptor_wallet_ready()
        assert is_ready is False, "Fresh wallet should not be ready"

        # Setup descriptor wallet
        setup_result = await wallet.setup_descriptor_wallet(rescan=False)
        assert setup_result is True

        # Verify wallet is now ready
        is_ready = await wallet.is_descriptor_wallet_ready()
        assert is_ready is True, "Wallet should be ready after setup"

        # Verify descriptors were actually imported
        descriptors = await backend.list_descriptors()
        assert len(descriptors) >= 10, (
            f"Expected 10 descriptors (5 mixdepths x 2), got {len(descriptors)}"
        )

        # Sync wallet using descriptor wallet method
        await wallet.sync_with_descriptor_wallet()

        # The test mnemonic should have UTXOs in mixdepth 0 (from regtest funder)
        total_balance = await wallet.get_total_balance()
        # Note: May or may not have funds depending on test order
        # Just verify the sync completed without error
        assert total_balance >= 0

        # Verify first address matches expected derivation
        first_addr = wallet.get_receive_address(0, 0)
        assert first_addr.startswith("bcrt1"), f"Expected regtest address, got {first_addr}"

    finally:
        try:
            await backend.unload_wallet()
        except Exception:
            pass
        await backend.close()


# =============================================================================
# Smart Scan Tests
# =============================================================================


class TestSmartScan:
    """Tests for smart scan timestamp calculation."""

    @pytest.mark.asyncio
    async def test_smart_scan_timestamp_calculation(self) -> None:
        """Test that smart scan calculates timestamp correctly."""
        backend = DescriptorWalletBackend(wallet_name="test_smart_scan")

        rpc_calls: list[tuple[str, list[Any] | None]] = []

        async def mock_rpc(
            method: str,
            params: list[Any] | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            rpc_calls.append((method, params))
            if method == "getblockchaininfo":
                return {"blocks": 100_000, "headers": 100_000}
            elif method == "getblockhash":
                return "000000000000abcd1234"
            elif method == "getblockheader":
                return {"time": 1700000000}
            return {}

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        # Default lookback is 52,560 blocks (~1 year)
        # Current height: 100,000, lookback: 52,560 -> target: 47,440
        timestamp = await backend._get_smart_scan_timestamp()

        # Verify the RPC calls
        method_names = [call[0] for call in rpc_calls]
        assert "getblockchaininfo" in method_names
        assert "getblockhash" in method_names
        assert "getblockheader" in method_names
        # Check getblockhash was called with correct height
        getblockhash_calls = [call for call in rpc_calls if call[0] == "getblockhash"]
        assert getblockhash_calls[0][1] == [47_440]
        assert timestamp == 1700000000

    @pytest.mark.asyncio
    async def test_smart_scan_clamps_to_zero(self) -> None:
        """Test that smart scan clamps to block 0 for short chains."""
        backend = DescriptorWalletBackend(wallet_name="test_clamp")

        rpc_calls: list[tuple[str, list[Any] | None]] = []

        async def mock_rpc(
            method: str,
            params: list[Any] | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            rpc_calls.append((method, params))
            if method == "getblockchaininfo":
                return {"blocks": 1000, "headers": 1000}
            elif method == "getblockhash":
                return "0000000000genesis"
            elif method == "getblockheader":
                return {"time": 1231006505}  # Genesis time
            return {}

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        timestamp = await backend._get_smart_scan_timestamp()

        # 1000 - 52560 = negative, should clamp to 0
        getblockhash_calls = [call for call in rpc_calls if call[0] == "getblockhash"]
        assert getblockhash_calls[0][1] == [0]
        assert timestamp == 1231006505

    @pytest.mark.asyncio
    async def test_smart_scan_uses_creation_height(self) -> None:
        """Smart scan uses creation_height instead of lookback when set."""
        backend = DescriptorWalletBackend(wallet_name="test_creation_height")
        backend.set_wallet_creation_height(800000)

        rpc_calls: list[tuple[str, list[Any] | None]] = []

        async def mock_rpc(
            method: str,
            params: list[Any] | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            rpc_calls.append((method, params))
            if method == "getblockchaininfo":
                return {"blocks": 900000, "headers": 900000}
            elif method == "getblockhash":
                return "0000000000creation"
            elif method == "getblockheader":
                return {"time": 1710000000}  # Timestamp at creation height
            return {}

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        timestamp = await backend._get_smart_scan_timestamp()

        # Should use creation_height=800000, not lookback=900000-52560=847440
        getblockhash_calls = [call for call in rpc_calls if call[0] == "getblockhash"]
        assert getblockhash_calls[0][1] == [800000]
        assert timestamp == 1710000000

    def test_set_wallet_creation_height(self) -> None:
        """set_wallet_creation_height stores the height."""
        backend = DescriptorWalletBackend(wallet_name="test_set_height")
        assert backend._wallet_creation_height is None
        backend.set_wallet_creation_height(800000)
        assert backend._wallet_creation_height == 800000

    def test_set_wallet_creation_height_none_clears_hint(self) -> None:
        """set_wallet_creation_height(None) clears any previously stored hint."""
        backend = DescriptorWalletBackend(wallet_name="test_clear_height")
        backend.set_wallet_creation_height(800000)
        backend.set_wallet_creation_height(None)
        assert backend._wallet_creation_height is None

    def test_set_wallet_creation_height_negative_ignored(self) -> None:
        """Negative creation heights are ignored to avoid invalid scan hints."""
        backend = DescriptorWalletBackend(wallet_name="test_negative_height")
        backend.set_wallet_creation_height(-1)
        assert backend._wallet_creation_height is None


# =============================================================================
# Background Rescan Tests
# =============================================================================


class TestBackgroundRescan:
    """Tests for background rescan functionality."""

    @pytest.mark.asyncio
    async def test_run_background_rescan_clears_task_reference_on_success(self) -> None:
        backend = DescriptorWalletBackend(wallet_name="test_rescan_cleanup")
        backend._wallet_loaded = True
        backend._background_rescan_height = 100
        backend._background_rescan_task = AsyncMock()

        async def mock_rpc(
            method: str,
            params: list[Any] | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "rescanblockchain":
                return {"start_height": 100, "stop_height": 200}
            return {}

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        await backend._run_background_rescan(100)

        assert backend._background_rescan_height is None
        assert backend._background_rescan_task is None

    @pytest.mark.asyncio
    async def test_start_background_rescan(self) -> None:
        """Test that background rescan is started correctly."""
        backend = DescriptorWalletBackend(wallet_name="test_rescan")
        backend._wallet_loaded = True

        backend._rpc_call = make_mock_rpc({}, strict=False, default={})

        await backend.start_background_rescan()

        assert backend.is_background_rescan_pending() is True

    @pytest.mark.asyncio
    async def test_rescan_status_not_scanning(self) -> None:
        """Test rescan status when not scanning."""
        backend = DescriptorWalletBackend(wallet_name="test_status")
        backend._wallet_loaded = True

        backend._rpc_call = make_mock_rpc(
            {"getwalletinfo": {"scanning": False, "walletname": "test_wallet"}},
            strict=False,
            default={},
        )

        status = await backend.get_rescan_status()

        assert status is not None
        assert status["in_progress"] is False

    @pytest.mark.asyncio
    async def test_rescan_status_while_scanning(self) -> None:
        """Test rescan status during active scan."""
        backend = DescriptorWalletBackend(wallet_name="test_scanning")
        backend._wallet_loaded = True

        backend._rpc_call = make_mock_rpc(
            {
                "getwalletinfo": {
                    "scanning": {"duration": 120, "progress": 0.45},
                    "walletname": "test_wallet",
                }
            },
            strict=False,
            default={},
        )

        status = await backend.get_rescan_status()

        assert status is not None
        assert status["in_progress"] is True
        assert status["progress"] == 0.45
        assert status["duration"] == 120

    @pytest.mark.asyncio
    async def test_import_with_smart_scan_and_background_rescan(self) -> None:
        """Test import_descriptors with smart scan and background rescan enabled."""
        backend = DescriptorWalletBackend(wallet_name="test_smart_background")
        backend._wallet_loaded = True

        rpc_calls: list[tuple[str, list[Any] | None]] = []

        async def mock_rpc(
            method: str,
            params: list[Any] | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            rpc_calls.append((method, params))
            if method == "getblockchaininfo":
                return {"blocks": 100_000, "headers": 100_000}
            elif method == "getblockhash":
                return "00000000hash"
            elif method == "getblock":
                return {"time": 1700000000}
            elif method == "importdescriptors":
                return [{"success": True}]
            return {}

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        await backend.import_descriptors(
            descriptors=[
                {
                    "desc": "wpkh([fingerprint/84'/1'/0'/0/0]xpub...)#checksum",
                    "active": True,
                    "range": [0, 999],
                    "timestamp": "now",
                }
            ],
            smart_scan=True,
            background_full_rescan=True,
        )

        # Should have called getblockchaininfo for smart scan
        method_names = [call[0] for call in rpc_calls]
        assert "getblockchaininfo" in method_names

        # Background rescan should be pending
        assert backend.is_background_rescan_pending() is True

    @pytest.mark.asyncio
    async def test_wait_for_rescan_complete_race_condition(self) -> None:
        """Test that wait_for_rescan_complete handles the race condition where
        getwalletinfo.scanning reports False before the rescan has actually started.

        Without the fix, the method would return True immediately on the first
        poll, before the rescan had even begun.  With the fix, it waits until
        it observes at least one in_progress=True poll, or a grace period.
        """
        backend = DescriptorWalletBackend(wallet_name="test_race")
        backend._wallet_loaded = True

        # Simulate: first 2 polls return not-scanning (rescan hasn't started),
        # then 2 polls return in-progress, then back to not-scanning (done).
        call_count = 0

        async def mock_rpc(
            method: str,
            params: list[Any] | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            nonlocal call_count
            if method == "getwalletinfo":
                call_count += 1
                if call_count <= 2:
                    # Not yet started
                    return {"scanning": False, "walletname": "test_race"}
                elif call_count <= 4:
                    # Scanning in progress
                    progress = 0.5 if call_count == 3 else 0.9
                    return {
                        "scanning": {"duration": 10, "progress": progress},
                        "walletname": "test_race",
                    }
                else:
                    # Done
                    return {"scanning": False, "walletname": "test_race"}
            return {}

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        result = await backend.wait_for_rescan_complete(
            poll_interval=0.01,  # Very fast polling for test
            timeout=5.0,
        )

        assert result is True
        # Should have polled multiple times -- at least 5 (2 not started +
        # 2 in progress + 1 done).
        assert call_count >= 5

    @pytest.mark.asyncio
    async def test_wait_for_rescan_complete_never_starts(self) -> None:
        """If the rescan never starts within the grace period, wait should
        return True (assuming it completed very quickly or was never needed)."""
        backend = DescriptorWalletBackend(wallet_name="test_never_starts")
        backend._wallet_loaded = True

        async def mock_rpc(
            method: str,
            params: list[Any] | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "getwalletinfo":
                return {"scanning": False, "walletname": "test_never"}
            return {}

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        result = await backend.wait_for_rescan_complete(
            poll_interval=0.01,
            timeout=60.0,
            startup_grace_period=0.1,  # Short grace period for testing
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_wait_for_rescan_complete_timeout(self) -> None:
        """Test that wait_for_rescan_complete respects timeout."""
        backend = DescriptorWalletBackend(wallet_name="test_timeout")
        backend._wallet_loaded = True

        async def mock_rpc(
            method: str,
            params: list[Any] | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "getwalletinfo":
                # Always scanning (never finishes)
                return {
                    "scanning": {"duration": 999, "progress": 0.01},
                    "walletname": "test_timeout",
                }
            return {}

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        result = await backend.wait_for_rescan_complete(
            poll_interval=0.01,
            timeout=0.15,
        )
        assert result is False


# =============================================================================
# Fidelity Bond Sync Tests
# =============================================================================


class TestFidelityBondSync:
    """Tests for syncing fidelity bonds with descriptor wallet backend."""

    @pytest.mark.asyncio
    async def test_sync_with_fidelity_bonds(self) -> None:
        """Test that sync_with_descriptor_wallet correctly handles fidelity bond addresses."""
        from jmwallet.backends.base import UTXO
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
        from jmwallet.wallet.service import WalletService

        # Create backend with mock
        backend = DescriptorWalletBackend(wallet_name="test_fb_sync")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        # Mock the bond address and UTXO
        bond_address = TEST_BOND_ADDRESS
        bond_locktime = TEST_BOND_LOCKTIME  # 2025-01-15 00:00:00 UTC
        bond_index = 0
        bond_value = 29890

        # Mock get_all_utxos to return both regular and bond UTXOs
        async def mock_get_all_utxos() -> list[UTXO]:
            return [
                # Regular wallet UTXO
                UTXO(
                    txid=TEST_FAKE_TXID,
                    vout=0,
                    value=100000,
                    address="bc1qregularaddress123",
                    confirmations=100,
                    scriptpubkey="0014regular",
                ),
                # Fidelity bond UTXO (P2WSH)
                UTXO(
                    txid="def456" * 10 + "de",
                    vout=1,
                    value=bond_value,
                    address=bond_address,
                    confirmations=50,
                    scriptpubkey="0020" + "a" * 64,  # P2WSH scriptPubKey
                ),
            ]

        backend.get_all_utxos = mock_get_all_utxos  # type: ignore[method-assign]

        # Create wallet service with test mnemonic
        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Sync with fidelity bond addresses
        fidelity_bond_addresses = [(bond_address, bond_locktime, bond_index)]
        result = await wallet.sync_with_descriptor_wallet(fidelity_bond_addresses)

        # Verify that the bond UTXO was found in mixdepth 0
        mixdepth_0_utxos = result.get(0, [])

        # Find the bond UTXO in the results
        bond_utxos = [u for u in mixdepth_0_utxos if u.address == bond_address]
        assert len(bond_utxos) == 1, f"Expected 1 bond UTXO, found {len(bond_utxos)}"

        bond_utxo = bond_utxos[0]
        assert bond_utxo.value == bond_value
        assert bond_utxo.locktime == bond_locktime
        assert bond_utxo.is_timelocked is True

    @pytest.mark.asyncio
    async def test_sync_without_fidelity_bonds(self) -> None:
        """Test that sync_with_descriptor_wallet works without fidelity bonds."""
        from jmwallet.backends.base import UTXO
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_no_fb")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        # Mock get_all_utxos to return regular UTXOs only
        async def mock_get_all_utxos() -> list[UTXO]:
            return [
                UTXO(
                    txid=TEST_FAKE_TXID,
                    vout=0,
                    value=100000,
                    address="bc1qregularaddress123",
                    confirmations=100,
                    scriptpubkey="0014regular",
                ),
            ]

        backend.get_all_utxos = mock_get_all_utxos  # type: ignore[method-assign]

        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Sync without fidelity bonds
        result = await wallet.sync_with_descriptor_wallet()

        # Should complete without error
        assert result is not None

    @pytest.mark.asyncio
    async def test_setup_descriptor_wallet_with_fidelity_bonds(self) -> None:
        """Test that setup_descriptor_wallet imports fidelity bond addresses."""
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_setup_fb")

        imported_descriptors: list[dict[str, Any]] = []

        async def mock_rpc(
            method: str,
            params: list[Any] | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "listwallets":
                return ["test_setup_fb"]
            elif method == "listdescriptors":
                return {"descriptors": []}  # No descriptors yet
            elif method == "importdescriptors":
                if params:
                    imported_descriptors.extend(params[0])
                return [{"success": True} for _ in params[0]] if params else []
            elif method == "getblockchaininfo":
                return {"blocks": 100000}
            return {}

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]
        backend._wallet_loaded = True

        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Setup with fidelity bond addresses
        bond_address = TEST_BOND_ADDRESS
        bond_locktime = TEST_BOND_LOCKTIME
        bond_index = 0
        fidelity_bond_addresses = [(bond_address, bond_locktime, bond_index)]

        await wallet.setup_descriptor_wallet(
            rescan=False,
            fidelity_bond_addresses=fidelity_bond_addresses,
            check_existing=False,
        )

        # Verify that bond address was imported
        imported_descs_strs = [str(d.get("desc", "")) for d in imported_descriptors]
        bond_desc_imported = any(bond_address in desc for desc in imported_descs_strs)
        assert bond_desc_imported, (
            f"Bond address {bond_address} not found in imported descriptors: {imported_descs_strs}"
        )

    @pytest.mark.asyncio
    async def test_sync_with_external_fidelity_bond(self) -> None:
        """Test that sync correctly handles external fidelity bonds (index=-1).

        External fidelity bonds are bonds created from cold storage wallets.
        They have index=-1 and are stored in the bond registry. They must be
        properly recognized during sync to avoid being counted as spendable funds.
        """
        from jmwallet.backends.base import UTXO
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
        from jmwallet.wallet.service import WalletService

        # Create backend with mock
        backend = DescriptorWalletBackend(wallet_name="test_external_fb")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        # External fidelity bond address (from cold storage, not derived from this wallet)
        external_bond_address = "bc1qxhxgfy77xl7fzdc3ayx27n03fh6dctkgd6tlgufpvzjzzcpmklgs4sdxyl"
        external_bond_locktime = 1738368000  # 2026-02-01 00:00:00 UTC
        external_bond_index = -1  # -1 indicates external/cold storage bond
        external_bond_value = 29791

        # Mock get_all_utxos to return the external bond UTXO
        async def mock_get_all_utxos() -> list[UTXO]:
            return [
                # External fidelity bond UTXO (P2WSH)
                UTXO(
                    txid="external" * 7 + "ab",
                    vout=0,
                    value=external_bond_value,
                    address=external_bond_address,
                    confirmations=100,
                    scriptpubkey="0020" + "b" * 64,  # P2WSH scriptPubKey
                ),
            ]

        backend.get_all_utxos = mock_get_all_utxos  # type: ignore[method-assign]

        # Create wallet service with test mnemonic
        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Sync with external fidelity bond address (index=-1)
        fidelity_bond_addresses = [
            (external_bond_address, external_bond_locktime, external_bond_index)
        ]
        result = await wallet.sync_with_descriptor_wallet(fidelity_bond_addresses)

        # Verify that the external bond UTXO was found in mixdepth 0
        mixdepth_0_utxos = result.get(0, [])

        # Find the bond UTXO in the results
        bond_utxos = [u for u in mixdepth_0_utxos if u.address == external_bond_address]
        assert len(bond_utxos) == 1, f"Expected 1 bond UTXO, found {len(bond_utxos)}"

        bond_utxo = bond_utxos[0]
        assert bond_utxo.value == external_bond_value
        assert bond_utxo.locktime == external_bond_locktime
        assert bond_utxo.is_fidelity_bond is True
        assert bond_utxo.is_timelocked is True  # Alias should also work

        # Verify that the bond is NOT counted in get_balance_for_offers
        balance_for_offers = await wallet.get_balance_for_offers(0)
        assert balance_for_offers == 0, (
            f"External bond should NOT be included in offers balance, got {balance_for_offers}"
        )

        # Verify that get_fidelity_bond_balance correctly returns the bond value
        fb_balance = await wallet.get_fidelity_bond_balance(0)
        assert fb_balance == external_bond_value, (
            f"Expected FB balance {external_bond_value}, got {fb_balance}"
        )

    def test_find_address_path_checks_fidelity_bond_registry(self, tmp_path: Path) -> None:
        """Test that _find_address_path checks the fidelity bond registry.

        Regression test for bug: During wallet sync, fidelity bond addresses
        were flagged as "out of range" because _find_address_path only searched
        branches [0, 1] (external/internal), but fidelity bonds use branch 2.

        The fix checks the fidelity bond registry before doing expensive derivation.
        This avoids triggering unnecessary extended range searches (~40 seconds).
        """
        from unittest.mock import MagicMock, patch

        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
        from jmwallet.wallet.bond_registry import BondRegistry, FidelityBondInfo
        from jmwallet.wallet.service import FIDELITY_BOND_BRANCH, WalletService

        backend = DescriptorWalletBackend(wallet_name="test_find_fb")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        test_mnemonic = TEST_MNEMONIC

        # Create wallet with data_dir so it can access bond registry
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
            data_dir=tmp_path,
        )

        # Create a mock bond in the registry
        bond_address = TEST_BOND_ADDRESS
        bond_locktime = TEST_BOND_LOCKTIME
        bond_index = 0

        mock_bond = FidelityBondInfo(
            address=bond_address,
            locktime=bond_locktime,
            locktime_human="2025-01-15 00:00:00 UTC",
            index=bond_index,
            path="m/84'/0'/0'/2/0",
            pubkey="02" + "ab" * 32,
            witness_script_hex="0014" + "ab" * 20,
            network="mainnet",
            created_at="2025-01-01T00:00:00Z",
            txid=TEST_FAKE_TXID,
            vout=0,
            value=29890,
            confirmations=100,
        )
        mock_registry = MagicMock(spec=BondRegistry)
        mock_registry.get_bond_by_address.return_value = mock_bond

        # Patch load_registry to return our mock
        with patch("jmwallet.wallet.bond_registry.load_registry", return_value=mock_registry):
            # Clear address cache to ensure we're testing the registry lookup
            wallet.address_cache.clear()

            # Find the address path - should find it in registry without deriving
            result = wallet._find_address_path(bond_address)

            # Verify the result
            assert result is not None, "Should have found address in bond registry"
            assert result == (0, FIDELITY_BOND_BRANCH, bond_index), (
                f"Expected (0, {FIDELITY_BOND_BRANCH}, {bond_index}), got {result}"
            )

            # Verify the address was cached
            assert bond_address in wallet.address_cache
            assert wallet.address_cache[bond_address] == (0, FIDELITY_BOND_BRANCH, bond_index)

            # Verify the locktime was cached
            assert hasattr(wallet, "fidelity_bond_locktime_cache")
            assert wallet.fidelity_bond_locktime_cache[bond_address] == bond_locktime

            # Verify that registry lookup was called
            mock_registry.get_bond_by_address.assert_called_once_with(bond_address)

    def test_find_address_path_without_data_dir_skips_registry(self) -> None:
        """Test that _find_address_path skips registry lookup when data_dir is None."""
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_no_data_dir")
        backend._wallet_loaded = True

        test_mnemonic = TEST_MNEMONIC

        # Create wallet WITHOUT data_dir
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
            data_dir=None,  # No data_dir
        )

        # Clear cache
        wallet.address_cache.clear()

        # Try to find a non-existent address with max_scan=0 to avoid long derivation
        # This should return None without errors (not crash due to missing data_dir)
        result = wallet._find_address_path("bc1qnonexistent123", max_scan=0)

        # Should return None (not found, and no crash)
        assert result is None


# =============================================================================
# Address History Tests
# =============================================================================


class TestAddressHistory:
    """Tests for tracking address history including spent addresses."""

    @pytest.mark.asyncio
    async def test_get_addresses_with_history(self) -> None:
        """Test that get_addresses_with_history returns all addresses with transaction history."""
        backend = DescriptorWalletBackend(wallet_name="test_addr_history")
        backend._wallet_loaded = True

        # listaddressgroupings returns addresses grouped by common ownership
        mock_groupings = [
            [["bc1qtest1", 0.0], ["bc1qtest2", 0.01]],  # Group 1
            [["bc1qtest3", 0.0]],  # Group 2
        ]

        mock_transactions = [
            {
                "address": "bc1qtest1",
                "category": "receive",
                "amount": 0.01,
                "txid": "abc123",
            },
            {
                "address": "bc1qtest2",
                "category": "receive",
                "amount": 0.02,
                "txid": "def456",
            },
            {
                "address": "bc1qtest1",  # Same address, second tx
                "category": "send",
                "amount": -0.01,
                "txid": "ghi789",
            },
            {
                "address": "bc1qtest3",
                "category": "receive",
                "amount": 0.03,
                "txid": "jkl012",
            },
        ]

        backend._rpc_call = make_mock_rpc(
            {
                "listaddressgroupings": mock_groupings,
                "listsinceblock": {"transactions": mock_transactions, "lastblock": "0" * 64},
            },
            strict=False,
            default={},
        )

        addresses = await backend.get_addresses_with_history()

        # Should have 3 unique addresses from both sources
        assert len(addresses) == 3
        assert "bc1qtest1" in addresses
        assert "bc1qtest2" in addresses
        assert "bc1qtest3" in addresses

    @pytest.mark.asyncio
    async def test_get_addresses_with_history_empty(self) -> None:
        """Test get_addresses_with_history with no transactions."""
        backend = DescriptorWalletBackend(wallet_name="test_empty_history")
        backend._wallet_loaded = True

        backend._rpc_call = make_mock_rpc(
            {
                "listaddressgroupings": [],
                "listsinceblock": {"transactions": [], "lastblock": "0" * 64},
            },
            strict=False,
            default={},
        )

        addresses = await backend.get_addresses_with_history()

        assert len(addresses) == 0

    @pytest.mark.asyncio
    async def test_get_addresses_with_history_filters_categories(self) -> None:
        """Test that get_addresses_with_history only includes receive/generate from listsinceblock.

        "send" addresses are counterparty addresses (where we sent to) and should
        not be included from listsinceblock, since they don't belong to this wallet.
        However, listaddressgroupings returns all addresses involved in transactions.
        """
        backend = DescriptorWalletBackend(wallet_name="test_filter_history")
        backend._wallet_loaded = True

        # listaddressgroupings only returns our own addresses
        mock_groupings = [
            [["bc1qreceive", 0.0]],
            [["bc1qgenerate", 50.0]],
        ]

        mock_transactions = [
            {
                "address": "bc1qreceive",
                "category": "receive",
                "amount": 0.01,
                "txid": "abc",
            },
            {
                "address": "bc1qsend",
                "category": "send",  # Should be excluded (counterparty address)
                "amount": -0.01,
                "txid": "def",
            },
            {
                "address": "bc1qgenerate",
                "category": "generate",
                "amount": 50.0,
                "txid": "ghi",
            },
            {
                "address": "bc1qimmature",
                "category": "immature",  # Should be excluded
                "amount": 50.0,
                "txid": "jkl",
            },
            {
                "category": "orphan",  # No address, should be skipped
                "amount": 0,
                "txid": "mno",
            },
        ]

        backend._rpc_call = make_mock_rpc(
            {
                "listaddressgroupings": mock_groupings,
                "listsinceblock": {"transactions": mock_transactions, "lastblock": "0" * 64},
            },
            strict=False,
            default={},
        )

        addresses = await backend.get_addresses_with_history()

        # Should have 2 addresses (receive, generate) but not send/immature/orphan
        # "send" addresses are counterparty addresses in CoinJoin transactions
        assert len(addresses) == 2
        assert "bc1qreceive" in addresses
        assert "bc1qsend" not in addresses  # Counterparty addresses excluded
        assert "bc1qgenerate" in addresses
        assert "bc1qimmature" not in addresses

    @pytest.mark.asyncio
    async def test_get_addresses_with_history_groupings_only(self) -> None:
        """Test that addresses found only in listaddressgroupings are included.

        This is the critical fix for the bug where addresses that were used
        but don't appear in listsinceblock (e.g., after wallet import without
        proper rescan) are still detected via listaddressgroupings.
        """
        backend = DescriptorWalletBackend(wallet_name="test_groupings_only")
        backend._wallet_loaded = True

        # This address appears in listaddressgroupings but NOT in listsinceblock
        # This happens when the wallet was imported and the transaction details
        # weren't properly recorded, but Bitcoin Core still knows about the grouping
        mock_groupings = [
            [["bc1qusedbutnotintxlist", 0.0]],  # Used address with 0 balance
            [["bc1qalsoused", 0.0]],
        ]

        # Empty transaction list - simulating missing tx history
        mock_transactions: list[dict[str, Any]] = []

        backend._rpc_call = make_mock_rpc(
            {
                "listaddressgroupings": mock_groupings,
                "listsinceblock": {"transactions": mock_transactions, "lastblock": "0" * 64},
            },
            strict=False,
            default={},
        )

        addresses = await backend.get_addresses_with_history()

        # Should find addresses from listaddressgroupings even when listsinceblock is empty
        assert len(addresses) == 2
        assert "bc1qusedbutnotintxlist" in addresses
        assert "bc1qalsoused" in addresses

    @pytest.mark.asyncio
    async def test_sync_populates_addresses_with_history(self) -> None:
        """Test that sync_with_descriptor_wallet populates addresses_with_history."""
        from jmwallet.backends.base import UTXO
        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_sync_history")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        # Address that HAS a UTXO
        addr_with_utxo = "bc1qcurrentutxo"
        # Address that WAS used but now has 0 balance (fully spent)
        addr_spent = "bc1qspentaddr"

        async def mock_get_all_utxos() -> list[UTXO]:
            return [
                UTXO(
                    txid=TEST_FAKE_TXID,
                    vout=0,
                    value=100000,
                    address=addr_with_utxo,
                    confirmations=100,
                    scriptpubkey="0014current",
                ),
            ]

        async def mock_get_addresses_with_history() -> set[str]:
            return {addr_with_utxo, addr_spent}

        backend.get_all_utxos = mock_get_all_utxos  # type: ignore[method-assign]
        backend.get_addresses_with_history = mock_get_addresses_with_history  # type: ignore

        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Pre-populate address cache with our test addresses
        # This simulates what _populate_address_cache would do for real addresses
        wallet.address_cache[addr_with_utxo] = (0, 0, 0)
        wallet.address_cache[addr_spent] = (0, 0, 1)

        await wallet.sync_with_descriptor_wallet()

        # Both addresses should be in addresses_with_history
        assert addr_with_utxo in wallet.addresses_with_history
        assert addr_spent in wallet.addresses_with_history


class TestDescriptorRangeUpgrade:
    """Tests for descriptor range detection and upgrade functionality."""

    @pytest.mark.asyncio
    async def test_get_descriptor_ranges(self) -> None:
        """Test getting descriptor ranges from wallet."""
        backend = DescriptorWalletBackend(wallet_name="test_ranges")
        backend._wallet_loaded = True

        async def mock_rpc_call(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "listdescriptors":
                return {
                    "descriptors": [
                        {
                            "desc": "wpkh(xpub.../0/*)#checksum",
                            "range": [0, 999],
                        },
                        {
                            "desc": "wpkh(xpub.../1/*)#checksum",
                            "range": [0, 999],
                        },
                        {
                            "desc": "addr(bc1q...)#checksum",
                            # No range for addr() descriptors
                        },
                    ]
                }
            raise ValueError(f"Unexpected RPC: {method}")

        backend._rpc_call = mock_rpc_call  # type: ignore[method-assign]

        ranges = await backend.get_descriptor_ranges()

        assert len(ranges) == 2  # Only ranged descriptors
        assert ranges["wpkh(xpub.../0/*)"] == (0, 999)
        assert ranges["wpkh(xpub.../1/*)"] == (0, 999)

    @pytest.mark.asyncio
    async def test_get_max_descriptor_range(self) -> None:
        """Test getting maximum descriptor range."""
        backend = DescriptorWalletBackend(wallet_name="test_max_range")
        backend._wallet_loaded = True

        async def mock_rpc_call(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "listdescriptors":
                return {
                    "descriptors": [
                        {"desc": "wpkh(xpub.../0/*)#abc", "range": [0, 4999]},
                        {"desc": "wpkh(xpub.../1/*)#def", "range": [0, 2999]},
                    ]
                }
            raise ValueError(f"Unexpected RPC: {method}")

        backend._rpc_call = mock_rpc_call  # type: ignore[method-assign]

        max_range = await backend.get_max_descriptor_range()

        assert max_range == 4999

    @pytest.mark.asyncio
    async def test_get_max_descriptor_range_empty(self) -> None:
        """Test max range returns default when no descriptors."""
        backend = DescriptorWalletBackend(wallet_name="test_empty_range")
        backend._wallet_loaded = True

        async def mock_rpc_call(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "listdescriptors":
                return {"descriptors": []}
            raise ValueError(f"Unexpected RPC: {method}")

        backend._rpc_call = mock_rpc_call  # type: ignore[method-assign]

        max_range = await backend.get_max_descriptor_range()

        # Should return DEFAULT_GAP_LIMIT
        from jmwallet.backends.descriptor_wallet import DEFAULT_GAP_LIMIT

        assert max_range == DEFAULT_GAP_LIMIT

    @pytest.mark.asyncio
    async def test_upgrade_descriptor_ranges(self) -> None:
        """Test upgrading descriptor ranges."""
        backend = DescriptorWalletBackend(wallet_name="test_upgrade")
        backend._wallet_loaded = True

        import_calls: list[dict] = []

        async def mock_rpc_call(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "getdescriptorinfo":
                desc = params[0] if params else ""
                return {"descriptor": f"{desc}#mockchecksum"}
            if method == "importdescriptors":
                import_calls.append({"method": method, "params": params})
                return [{"success": True}]
            if method == "listdescriptors":
                return {"descriptors": [{"desc": "test", "range": [0, 999]}]}
            raise ValueError(f"Unexpected RPC: {method}")

        backend._rpc_call = mock_rpc_call  # type: ignore[method-assign]

        descriptors = [
            {"desc": "wpkh(xpub.../0/*)", "range": [0, 999]},
            {"desc": "wpkh(xpub.../1/*)", "range": [0, 999]},
        ]

        result = await backend.upgrade_descriptor_ranges(descriptors, 4999, rescan=False)

        assert result["success_count"] == 1
        assert len(import_calls) == 1

        # Check that ranges were updated
        imported = import_calls[0]["params"][0]
        assert imported[0]["range"] == [0, 4999]
        assert imported[1]["range"] == [0, 4999]

    @pytest.mark.asyncio
    async def test_check_and_upgrade_descriptor_range_no_upgrade_needed(self) -> None:
        """Test that no upgrade happens when range is sufficient."""
        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_no_upgrade")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        async def mock_rpc_call(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "listdescriptors":
                return {
                    "descriptors": [
                        {"desc": "wpkh(xpub.../0/*)#abc", "range": [0, 999]},
                    ]
                }
            raise ValueError(f"Unexpected RPC: {method}")

        backend._rpc_call = mock_rpc_call  # type: ignore[method-assign]

        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Set up addresses_with_history with low indices
        wallet.address_cache["bc1q_test1"] = (0, 0, 50)
        wallet.address_cache["bc1q_test2"] = (0, 1, 100)
        wallet.addresses_with_history = {"bc1q_test1", "bc1q_test2"}

        # Current range (999) > highest used (100) + gap_limit (100)
        upgraded = await wallet.check_and_upgrade_descriptor_range(gap_limit=100)

        assert upgraded is False

    @pytest.mark.asyncio
    async def test_check_and_upgrade_descriptor_range_upgrade_needed(self) -> None:
        """Test that upgrade happens when range is insufficient."""
        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_upgrade_needed")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        upgrade_called = False
        new_range_used = 0

        async def mock_rpc_call(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            nonlocal upgrade_called, new_range_used
            if method == "listdescriptors":
                return {
                    "descriptors": [
                        {"desc": "wpkh(xpub.../0/*)#abc", "range": [0, 999]},
                    ]
                }
            if method == "getdescriptorinfo":
                desc = params[0] if params else ""
                return {"descriptor": f"{desc}#mockchecksum"}
            if method == "importdescriptors":
                upgrade_called = True
                # Extract the new range
                if params and params[0]:
                    new_range_used = params[0][0].get("range", [0, 0])[1]
                return [{"success": True} for _ in (params[0] if params else [])]
            raise ValueError(f"Unexpected RPC: {method}")

        backend._rpc_call = mock_rpc_call  # type: ignore[method-assign]

        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Set up addresses_with_history with HIGH indices (beyond current range)
        wallet.address_cache["bc1q_high_idx"] = (0, 0, 950)
        wallet.addresses_with_history = {"bc1q_high_idx"}

        # With gap_limit=100, we need range >= 950 + 100 + 1 = 1051
        # Current range is 999, so upgrade should be triggered
        upgraded = await wallet.check_and_upgrade_descriptor_range(gap_limit=100)

        assert upgraded is True
        assert upgrade_called is True
        assert new_range_used >= 1051

    @pytest.mark.asyncio
    async def test_populate_address_cache(self) -> None:
        """Test pre-populating address cache."""
        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_cache")
        backend._wallet_loaded = True

        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Should start empty
        assert len(wallet.address_cache) == 0

        # Populate for small range
        await wallet._populate_address_cache(10)

        # Should have 5 mixdepths * 2 branches * 10 indices = 100 addresses
        assert len(wallet.address_cache) == 100

        # Verify addresses are properly cached
        addr = wallet.get_address(0, 0, 5)
        assert addr in wallet.address_cache
        assert wallet.address_cache[addr] == (0, 0, 5)

    @pytest.mark.asyncio
    async def test_sync_detects_spent_addresses_from_history(self) -> None:
        """Test that sync detects addresses with history even if empty and not cached.

        Regression test for bug: After importing a wallet, addresses that had
        received funds (now spent/empty) were shown as 'new' instead of 'used-empty'.

        The bug was that `get_addresses_with_history()` only added addresses to
        `addresses_with_history` if they were already in `address_cache`. If the
        cache wasn't populated far enough, spent addresses would be missed.

        The fix uses `_find_address_path()` which will derive and find addresses
        even if they're not in the initial cache.
        """
        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_spent_history")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Derive the address at index 1 (m/84'/0'/0'/0/1) - the address that was used
        spent_address = wallet.get_address(0, 0, 1)

        # Simulate scenario:
        # 1. Address cache is only populated to index 0 (e.g., small range)
        # 2. get_addresses_with_history() returns address at index 1 from node
        # 3. The address should still be tracked as having history

        # Clear the cache to simulate limited initial population
        wallet.address_cache.clear()
        # Only cache index 0
        wallet.get_address(0, 0, 0)

        # Verify the spent_address is NOT in cache (simulating the bug condition)
        assert spent_address not in wallet.address_cache

        # Mock RPC calls for sync
        async def mock_rpc_call(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "listunspent":
                return []  # No UTXOs (everything was spent)
            if method == "listdescriptors":
                return {
                    "descriptors": [
                        {"desc": "wpkh(xpub.../0/*)#abc", "range": [0, 999]},
                    ]
                }
            if method == "listaddressgroupings":
                # Return the spent address in groupings
                return [[[spent_address, 0.0]]]
            if method == "listsinceblock":
                # Return the spent address as having history (listsinceblock format)
                return {
                    "transactions": [
                        {
                            "address": spent_address,
                            "category": "receive",
                            "amount": 0.001,
                            "confirmations": 100,
                        }
                    ],
                    "lastblock": "0" * 64,
                }
            raise ValueError(f"Unexpected RPC: {method}")

        backend._rpc_call = mock_rpc_call  # type: ignore[method-assign]

        # Run sync
        await wallet.sync_with_descriptor_wallet()

        # The spent address should now be in addresses_with_history
        # This is the bug we're testing - before the fix, it would NOT be added
        # because address_cache.get() would return None
        assert spent_address in wallet.addresses_with_history

        # Also verify it was added to the address_cache via _find_address_path
        assert spent_address in wallet.address_cache
        assert wallet.address_cache[spent_address] == (0, 0, 1)

        # Now verify the address shows as 'used-empty' not 'new'
        addresses = wallet.get_address_info_for_mixdepth(
            mixdepth=0,
            change=0,  # External addresses
            gap_limit=3,
            used_addresses=set(),
            history_addresses={},
        )

        # Find the address at index 1
        addr_1_info = next(a for a in addresses if a.index == 1)
        assert addr_1_info.status == "used-empty"
        assert addr_1_info.balance == 0

    @pytest.mark.asyncio
    async def test_sync_with_deep_history_wallet(self) -> None:
        """Test that sync handles wallets with deep history efficiently.

        This test simulates a wallet with a large transaction history where:
        1. Many addresses have been used across different indices
        2. The descriptor range is large (e.g., 5000)
        3. There are many addresses with history to process

        The test verifies that:
        - Sync completes without hanging
        - All addresses with history are tracked
        - Performance is reasonable (no O(n*m) explosion)
        """
        import time

        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_deep_history")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Pre-derive some addresses at high indices (simulating deep history)
        # In a real wallet, these might be at indices 2000, 3000, etc.
        high_index_addresses: list[str] = []
        for idx in [100, 500, 900]:
            addr = wallet.get_address(0, 0, idx)
            high_index_addresses.append(addr)

        # Clear cache to simulate fresh start
        wallet.address_cache.clear()

        # Track how many times expensive derivation is called
        derivation_count = 0
        original_get_address = wallet.get_address

        def counting_get_address(mixdepth: int, change: int, index: int) -> str:
            nonlocal derivation_count
            derivation_count += 1
            return original_get_address(mixdepth, change, index)

        wallet.get_address = counting_get_address  # type: ignore[method-assign]

        # Current descriptor range is [0, 999]
        current_range = 1000

        async def mock_rpc_call(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            if method == "listunspent":
                # Return a UTXO at index 900
                return [
                    {
                        "txid": "a" * 64,
                        "vout": 0,
                        "address": high_index_addresses[2],  # Index 900
                        "amount": 0.01,
                        "confirmations": 10,
                        "scriptPubKey": "0014" + "ab" * 20,
                    }
                ]
            if method == "listdescriptors":
                return {
                    "descriptors": [
                        {"desc": "wpkh(xpub.../0/*)#abc", "range": [0, current_range - 1]},
                        {"desc": "wpkh(xpub.../1/*)#def", "range": [0, current_range - 1]},
                    ]
                }
            if method == "listaddressgroupings":
                # Return addresses at various indices in groupings
                return [[[addr, 0.0] for addr in high_index_addresses]]
            if method == "listsinceblock":
                # Return transactions for multiple addresses at various indices
                return {
                    "transactions": [
                        {"address": addr, "category": "receive", "amount": 0.001}
                        for addr in high_index_addresses
                    ],
                    "lastblock": "0" * 64,
                }
            if method == "getdescriptorinfo":
                desc = params[0] if params else ""
                return {"descriptor": f"{desc}#mockchecksum"}
            if method == "importdescriptors":
                return [{"success": True}]
            if method == "getblockchaininfo":
                return {"blocks": 800000}
            raise ValueError(f"Unexpected RPC: {method}")

        backend._rpc_call = mock_rpc_call  # type: ignore[method-assign]

        # Run sync and measure time
        start_time = time.time()
        await wallet.sync_with_descriptor_wallet()
        elapsed = time.time() - start_time

        # Verify all addresses with history were tracked
        for addr in high_index_addresses:
            assert addr in wallet.addresses_with_history, (
                f"Address at high index should be tracked: {addr}"
            )

        # The UTXO at index 900 should be found
        assert len(wallet.utxo_cache[0]) == 1
        assert wallet.utxo_cache[0][0].address == high_index_addresses[2]

        # Performance check: sync should complete in reasonable time
        # Address cache population for 1000 addresses takes ~4-5s on typical hardware
        # In the bug scenario, this could take minutes due to O(n*m) derivation
        assert elapsed < 15.0, f"Sync took too long: {elapsed:.2f}s"

        # Log derivation count for debugging
        # With proper caching, this should be ~= mixdepths * 2 * current_range
        # NOT mixdepths * 2 * current_range * num_addresses_to_check
        expected_derivations = 5 * 2 * current_range  # 10,000
        # Allow some overhead for additional lookups
        assert derivation_count < expected_derivations * 1.5, (
            f"Too many derivations: {derivation_count} (expected ~{expected_derivations})"
        )

    @pytest.mark.asyncio
    async def test_sync_with_very_large_range_upgrade(self) -> None:
        """Test that upgrading to a very large descriptor range doesn't hang.

        This test simulates a scenario where:
        1. Wallet initially has range [0, 999]
        2. History shows addresses used at very high indices
        3. Range needs to be upgraded significantly

        This is a regression test for the sync hanging issue reported by users
        with large wallets.
        """
        import time

        from jmwallet.wallet.service import WalletService

        backend = DescriptorWalletBackend(wallet_name="test_large_range")
        backend._wallet_loaded = True
        backend._descriptors_imported = True

        test_mnemonic = TEST_MNEMONIC
        wallet = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,
            network="mainnet",
            mixdepth_count=5,
        )

        # Simulate a wallet that was used with the reference implementation
        # which might have addresses at very high indices
        # (reference impl uses different gap limit logic)

        # For this test, we'll have addresses at indices up to 2000
        # which is beyond the default range of 1000
        very_high_index = 2000
        high_index_address = wallet.get_address(0, 0, very_high_index)
        wallet.address_cache.clear()

        current_range = 1000
        upgrade_called = False
        new_range_after_upgrade = current_range

        async def mock_rpc_call(
            method: str,
            params: list | None = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            nonlocal upgrade_called, new_range_after_upgrade

            if method == "listunspent":
                return []  # No UTXOs
            if method == "listdescriptors":
                # Return current range (which gets updated after upgrade)
                range_end = new_range_after_upgrade - 1
                return {
                    "descriptors": [
                        {"desc": "wpkh(xpub.../0/*)#abc", "range": [0, range_end]},
                        {"desc": "wpkh(xpub.../1/*)#def", "range": [0, range_end]},
                    ]
                }
            if method == "listaddressgroupings":
                # Return address at very high index in groupings
                return [[[high_index_address, 0.0]]]
            if method == "listsinceblock":
                # Return transaction for address at very high index
                return {
                    "transactions": [
                        {"address": high_index_address, "category": "receive", "amount": 0.001}
                    ],
                    "lastblock": "0" * 64,
                }
            if method == "getdescriptorinfo":
                desc = params[0] if params else ""
                return {"descriptor": f"{desc}#mockchecksum"}
            if method == "importdescriptors":
                upgrade_called = True
                # Extract the new range from the import request
                if params and params[0]:
                    for req in params[0]:
                        if "range" in req:
                            new_range_after_upgrade = req["range"][1] + 1
                            break
                return [{"success": True} for _ in (params[0] if params else [])]
            if method == "getblockchaininfo":
                return {"blocks": 800000}
            raise ValueError(f"Unexpected RPC: {method}")

        backend._rpc_call = mock_rpc_call  # type: ignore[method-assign]

        # Run sync and measure time
        start_time = time.time()
        await wallet.sync_with_descriptor_wallet()
        elapsed = time.time() - start_time

        # The high index address should be found and tracked
        # (after range upgrade)
        assert high_index_address in wallet.addresses_with_history

        # Range should have been upgraded to accommodate the high index
        assert upgrade_called, "Descriptor range should have been upgraded"
        # new range should be at least very_high_index + gap_limit + 1
        assert new_range_after_upgrade >= very_high_index + 100 + 1

        # Performance: should complete in reasonable time
        # This test does two cache populations (initial 1000 + upgrade to 2101)
        # plus extended search, so allow up to 60 seconds
        # In the bug scenario (before fix), this would hang indefinitely
        assert elapsed < 60.0, f"Sync took too long: {elapsed:.2f}s (potential hang)"


# =============================================================================
# Wallet Auto-Reload Tests (Bitcoin Core restart resilience)
# =============================================================================


class TestWalletAutoReload:
    """Tests for automatic wallet reload when Bitcoin Core restarts.

    When Bitcoin Core is restarted, wallets are unloaded. The backend should
    detect RPC error -18 and transparently reload the wallet, then retry.
    """

    @pytest.mark.asyncio
    async def test_is_wallet_not_loaded_error_positive(self) -> None:
        """Test that _is_wallet_not_loaded_error detects error -18."""
        err = ValueError("RPC error -18: Requested wallet does not exist or is not loaded")
        assert DescriptorWalletBackend._is_wallet_not_loaded_error(err) is True

    @pytest.mark.asyncio
    async def test_is_wallet_not_loaded_error_negative(self) -> None:
        """Test that _is_wallet_not_loaded_error ignores other errors."""
        err = ValueError("RPC error -1: Something else went wrong")
        assert DescriptorWalletBackend._is_wallet_not_loaded_error(err) is False

    @pytest.mark.asyncio
    async def test_ensure_wallet_loaded_already_loaded(self) -> None:
        """Test _ensure_wallet_loaded when wallet is already in listwallets."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = True

        calls: list[str] = []

        async def mock_rpc(
            method: str,
            params: Any = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            calls.append(method)
            if method == "listwallets":
                return ["test_wallet", "other_wallet"]
            raise ValueError(f"Unexpected: {method}")

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        result = await backend._ensure_wallet_loaded()

        assert result is True
        assert calls == ["listwallets"]

    @pytest.mark.asyncio
    async def test_ensure_wallet_loaded_reloads_after_restart(self) -> None:
        """Test _ensure_wallet_loaded reloads wallet after Bitcoin Core restart."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = True

        calls: list[str] = []

        async def mock_rpc(
            method: str,
            params: Any = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            calls.append(method)
            if method == "listwallets":
                return []  # Wallet not loaded after restart
            if method == "loadwallet":
                return {"name": "test_wallet"}
            raise ValueError(f"Unexpected: {method}")

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        result = await backend._ensure_wallet_loaded()

        assert result is True
        assert calls == ["listwallets", "loadwallet"]

    @pytest.mark.asyncio
    async def test_ensure_wallet_loaded_fails_gracefully(self) -> None:
        """Test _ensure_wallet_loaded when Bitcoin Core is unreachable."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = True

        async def mock_rpc(
            method: str,
            params: Any = None,
            client: Any = None,
            use_wallet: bool = True,
        ) -> Any:
            raise ConnectionError("Connection refused")

        backend._rpc_call = mock_rpc  # type: ignore[method-assign]

        result = await backend._ensure_wallet_loaded()

        assert result is False
        # _wallet_loaded should remain True so future calls still attempt RPC
        assert backend._wallet_loaded is True

    @pytest.mark.asyncio
    async def test_rpc_call_retries_on_wallet_not_loaded(self) -> None:
        """Test that _rpc_call automatically reloads wallet on error -18 and retries."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = True

        call_log: list[tuple[str, str]] = []  # (method, url_type)

        async def mock_post(url: str, json: dict[str, Any]) -> Any:
            method = json["method"]
            url_type = "wallet" if "/wallet/" in url else "base"
            call_log.append((method, url_type))

            # First listunspent call: error -18 (wallet not loaded)
            if method == "listunspent" and len([c for c in call_log if c[0] == "listunspent"]) == 1:
                return _make_rpc_response(
                    json["id"],
                    error={
                        "code": -18,
                        "message": "Requested wallet does not exist or is not loaded",
                    },
                )

            # listwallets for reload check
            if method == "listwallets":
                return _make_rpc_response(json["id"], result=[])

            # loadwallet succeeds
            if method == "loadwallet":
                return _make_rpc_response(json["id"], result={"name": "test_wallet"})

            # Retry listunspent succeeds
            if method == "listunspent":
                return _make_rpc_response(json["id"], result=[])

            raise ValueError(f"Unexpected call: {method}")

        backend.client = _make_mock_http_client(mock_post)

        result = await backend._rpc_call("listunspent", [0, 9999999])

        assert result == []
        # Should have: listunspent (fail), listwallets, loadwallet, listunspent (success)
        methods = [c[0] for c in call_log]
        assert methods == ["listunspent", "listwallets", "loadwallet", "listunspent"]

    @pytest.mark.asyncio
    async def test_rpc_call_does_not_retry_non_wallet_errors(self) -> None:
        """Test that non-wallet errors are raised immediately without retry."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = True

        call_count = 0

        async def mock_post(url: str, json: dict[str, Any]) -> Any:
            nonlocal call_count
            call_count += 1
            return _make_rpc_response(
                json["id"],
                error={"code": -1, "message": "Some other error"},
            )

        backend.client = _make_mock_http_client(mock_post)

        with pytest.raises(ValueError, match="RPC error -1"):
            await backend._rpc_call("listunspent", [0, 9999999])

        # Should only call once (no retry)
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_rpc_call_does_not_retry_non_wallet_scoped_calls(self) -> None:
        """Test that use_wallet=False calls are not retried on error -18."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = True

        call_count = 0

        async def mock_post(url: str, json: dict[str, Any]) -> Any:
            nonlocal call_count
            call_count += 1
            return _make_rpc_response(
                json["id"],
                error={"code": -18, "message": "Wallet not loaded"},
            )

        backend.client = _make_mock_http_client(mock_post)

        with pytest.raises(ValueError, match="RPC error -18"):
            await backend._rpc_call("getblockchaininfo", use_wallet=False)

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_rpc_call_retries_only_once(self) -> None:
        """Test that the retry only happens once (no infinite loops)."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = True

        call_log: list[str] = []

        async def mock_post(url: str, json: dict[str, Any]) -> Any:
            method = json["method"]
            call_log.append(method)

            # Both listunspent calls return -18
            if method == "listunspent":
                return _make_rpc_response(
                    json["id"],
                    error={"code": -18, "message": "Wallet not loaded"},
                )

            # Reload appears to succeed
            if method == "listwallets":
                return _make_rpc_response(json["id"], result=[])
            if method == "loadwallet":
                return _make_rpc_response(json["id"], result={"name": "test_wallet"})

            raise ValueError(f"Unexpected: {method}")

        backend.client = _make_mock_http_client(mock_post)

        with pytest.raises(ValueError, match="RPC error -18"):
            await backend._rpc_call("listunspent", [0, 9999999])

        # Should have: listunspent (fail), listwallets, loadwallet, listunspent (fail again)
        assert call_log == ["listunspent", "listwallets", "loadwallet", "listunspent"]

    @pytest.mark.asyncio
    async def test_rpc_call_raises_original_error_when_reload_fails(self) -> None:
        """Test that the original -18 error is raised when wallet reload fails."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = True

        async def mock_post(url: str, json: dict[str, Any]) -> Any:
            method = json["method"]
            if method == "listunspent":
                return _make_rpc_response(
                    json["id"],
                    error={"code": -18, "message": "Wallet not loaded"},
                )
            if method == "listwallets":
                return _make_rpc_response(json["id"], result=[])
            if method == "loadwallet":
                return _make_rpc_response(
                    json["id"],
                    error={"code": -18, "message": "Wallet file not found"},
                )
            raise ValueError(f"Unexpected: {method}")

        backend.client = _make_mock_http_client(mock_post)

        with pytest.raises(ValueError, match="RPC error -18: Wallet not loaded"):
            await backend._rpc_call("listunspent", [0, 9999999])

        # _wallet_loaded remains True for future retry attempts
        assert backend._wallet_loaded is True

    @pytest.mark.asyncio
    async def test_get_utxos_recovers_after_bitcoind_restart(self) -> None:
        """Integration-style test: get_utxos recovers after Bitcoin Core restart."""
        backend = DescriptorWalletBackend(wallet_name="test_wallet")
        backend._wallet_loaded = True

        mock_utxos = [
            {
                "txid": "abc123",
                "vout": 0,
                "amount": 0.01,
                "address": "bc1qtest",
                "confirmations": 6,
                "scriptPubKey": "0014...",
            }
        ]

        first_attempt = True

        async def mock_post(url: str, json: dict[str, Any]) -> Any:
            nonlocal first_attempt
            method = json["method"]

            if method == "getblockchaininfo":
                return _make_rpc_response(json["id"], result={"blocks": 1000})

            if method == "listunspent":
                if first_attempt:
                    first_attempt = False
                    return _make_rpc_response(
                        json["id"],
                        error={
                            "code": -18,
                            "message": "Requested wallet does not exist or is not loaded",
                        },
                    )
                return _make_rpc_response(json["id"], result=mock_utxos)

            if method == "listwallets":
                return _make_rpc_response(json["id"], result=[])
            if method == "loadwallet":
                return _make_rpc_response(json["id"], result={"name": "test_wallet"})

            raise ValueError(f"Unexpected: {method}")

        backend.client = _make_mock_http_client(mock_post)

        utxos = await backend.get_utxos([])

        assert len(utxos) == 1
        assert utxos[0].txid == "abc123"
        assert utxos[0].value == 1_000_000


# ---------------------------------------------------------------------------
# Helpers for TestWalletAutoReload
# ---------------------------------------------------------------------------


class _MockResponse:
    """Minimal mock for httpx.Response."""

    def __init__(self, data: dict[str, Any], status_code: int = 200) -> None:
        self._data = data
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self._data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "http://localhost"),
                response=self,  # type: ignore[arg-type]
            )


def _make_rpc_response(
    request_id: int,
    result: Any = None,
    error: dict[str, Any] | None = None,
) -> _MockResponse:
    """Create a mock RPC JSON response."""
    data: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error:
        data["error"] = error
        return _MockResponse(data, status_code=500)
    data["result"] = result
    return _MockResponse(data)


def _make_mock_http_client(post_fn: Any) -> Any:
    """Create a mock httpx.AsyncClient with the given post function."""
    client = AsyncMock(spec=httpx.AsyncClient)
    client.post = post_fn
    return client
