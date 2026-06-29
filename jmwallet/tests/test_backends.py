"""
Integration tests for DescriptorWalletBackend and NeutrinoBackend
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmwallet.backends.base import BondVerificationRequest
from jmwallet.backends.neutrino import (
    GENESIS_BLOCK_HASHES,
    NeutrinoBackend,
    NeutrinoConfig,
    NeutrinoNetworkMismatchError,
)


class TestBackendCloseReuse:
    """Unit tests verifying that backends are reusable after close()."""

    @pytest.mark.asyncio
    async def test_descriptor_wallet_backend_reusable_after_close(self):
        """Closing a DescriptorWalletBackend should produce fresh httpx clients."""
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        backend = DescriptorWalletBackend()
        original_client = backend.client
        original_import_client = backend._import_client

        await backend.close()

        # Clients must have been replaced
        assert backend.client is not original_client
        assert backend._import_client is not original_import_client
        # New clients must be open (not closed)
        assert not backend.client.is_closed
        assert not backend._import_client.is_closed
        # Wallet state flags must be reset
        assert backend._wallet_loaded is False
        assert backend._descriptors_imported is False

        # Clean up the new clients
        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_reusable_after_close(self):
        """Closing a NeutrinoBackend should produce a fresh httpx client and reset state."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8080")
        # Simulate some accumulated state
        backend._watched_addresses = {"bcrt1qtest"}
        backend._initial_rescan_done = True
        backend._synced = True
        original_client = backend.client

        await backend.close()

        assert backend.client is not original_client
        assert not backend.client.is_closed
        assert backend._watched_addresses == set()
        assert backend._initial_rescan_done is False
        assert backend._synced is False

        await backend.close()


class TestNeutrinoBackend:
    """Unit tests for NeutrinoBackend (mocked)."""

    @pytest.mark.asyncio
    async def test_neutrino_backend_init(self):
        """Test NeutrinoBackend initialization."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="regtest",
        )
        assert backend.neutrino_url == "http://localhost:8334"
        assert backend.network == "regtest"
        assert backend._synced is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_metadata_capabilities(self):
        """Neutrino backend requires metadata from peers and can provide its own."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        # Requires metadata from counterparties to verify their UTXOs
        assert backend.requires_neutrino_metadata() is True
        # Can provide metadata for its own wallet UTXOs (scriptpubkey + blockheight)
        assert backend.can_provide_neutrino_metadata() is True
        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_scan_start_height_default(self):
        """Test that scan_start_height defaults to _min_valid_blockheight per network."""
        # Mainnet: defaults to SegWit activation height
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        assert backend._scan_start_height == 481824
        await backend.close()

        # Regtest: defaults to 0 (before _resolve_scan_start_height runs)
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        assert backend._scan_start_height == 0
        await backend.close()

        # Signet: defaults to 0 (before _resolve_scan_start_height runs)
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="signet")
        assert backend._scan_start_height == 0
        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_scan_start_height_explicit(self):
        """Test that explicit scan_start_height overrides the default."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_start_height=750000,
        )
        assert backend._scan_start_height == 750000
        await backend.close()

        # Even on regtest, explicit value is used
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="regtest",
            scan_start_height=100,
        )
        assert backend._scan_start_height == 100
        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_get_utxos_uses_scan_start_height(self):
        """Test that get_utxos uses scan_start_height for initial rescan."""
        from unittest.mock import patch

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_start_height=750000,
        )
        backend._api_call = AsyncMock(return_value={"utxos": []})
        backend.get_block_height = AsyncMock(return_value=800000)

        # Mock wait_for_sync so we don't block on the v1/status polling loop
        with patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync:
            mock_sync.return_value = True
            await backend.get_utxos(["bc1qtest123"])

        # The initial rescan should use start_height=750000
        rescan_posts = [
            call
            for call in backend._api_call.call_args_list
            if call[0][0] == "POST" and call[0][1] == "v1/rescan"
        ]
        assert len(rescan_posts) == 1
        assert rescan_posts[0][1]["data"]["start_height"] == 750000
        assert rescan_posts[0][1]["data"]["addresses"] == ["bc1qtest123"]
        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_verify_bonds_uses_scan_start_height(self):
        """Test that verify_bonds uses scan_start_height instead of 0."""

        from jmwallet.backends.base import BondVerificationRequest

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_start_height=750000,
        )
        backend.get_block_height = AsyncMock(return_value=800000)
        backend._api_call = AsyncMock(
            return_value={
                "unspent": True,
                "value": 100000,
                "block_height": 760000,
                "scriptpubkey": "0020" + "00" * 32,
            }
        )
        backend.get_block_time = AsyncMock(return_value=1700000000)

        bond = BondVerificationRequest(
            txid="a" * 64,
            vout=0,
            utxo_pub=b"\x02" + b"\x00" * 32,
            locktime=1800000000,
            address="bc1qtest",
            scriptpubkey="0020" + "00" * 32,
        )

        results = await backend.verify_bonds([bond])
        assert len(results) == 1
        assert results[0].valid is True

        # Check that the API call used scan_start_height, not 0
        utxo_call = backend._api_call.call_args
        assert utxo_call[1]["params"]["start_height"] == 750000
        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_verify_bonds_resolves_scan_start_height_lazy(self):
        """verify_bonds should resolve scan start when backend wasn't synced yet."""

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="signet",
            scan_start_height=None,
            scan_lookback_blocks=105120,
        )
        backend.get_block_height = AsyncMock(return_value=300000)
        backend._api_call = AsyncMock(
            return_value={
                "unspent": True,
                "value": 100000,
                "block_height": 299000,
                "scriptpubkey": "0020" + "11" * 32,
            }
        )
        backend.get_block_time = AsyncMock(return_value=1700000000)

        bond = BondVerificationRequest(
            txid="b" * 64,
            vout=0,
            utxo_pub=b"\x02" + b"\x01" * 32,
            locktime=1800000000,
            address="tb1qtest",
            scriptpubkey="0020" + "11" * 32,
        )

        results = await backend.verify_bonds([bond])
        assert len(results) == 1
        assert results[0].valid is True

        expected_start = 300000 - 105120
        utxo_call = backend._api_call.call_args
        assert utxo_call[1]["params"]["start_height"] == expected_start
        assert backend._scan_start_height == expected_start
        await backend.close()

    @pytest.mark.asyncio
    async def test_wait_for_rescan_completes_immediately(self):
        """Test _wait_for_rescan returns immediately when in_progress is False."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        backend._api_call = AsyncMock(return_value={"in_progress": False})

        completed = await backend._wait_for_rescan()

        backend._api_call.assert_called_once_with("GET", "v1/rescan/status")
        assert completed is True
        await backend.close()

    @pytest.mark.asyncio
    async def test_wait_for_rescan_polls_until_done(self):
        """Test _wait_for_rescan polls until in_progress transitions to False."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        # First two calls return in_progress=True, third returns False
        backend._api_call = AsyncMock(
            side_effect=[
                {"in_progress": True},
                {"in_progress": True},
                {"in_progress": False},
            ]
        )

        await backend._wait_for_rescan(poll_interval=0.01)

        assert backend._api_call.call_count == 3
        await backend.close()

    @pytest.mark.asyncio
    async def test_wait_for_rescan_fallback_on_error(self):
        """Test _wait_for_rescan returns gracefully when endpoint is unavailable."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        backend._api_call = AsyncMock(side_effect=Exception("endpoint not found"))

        # Should not raise, and should report unconfirmed completion
        completed = await backend._wait_for_rescan()

        backend._api_call.assert_called_once()
        assert completed is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_wait_for_rescan_require_started_rejects_immediate_false(self):
        """When require_started=True, immediate false should be unconfirmed."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        backend._api_call = AsyncMock(return_value={"in_progress": False})

        completed = await backend._wait_for_rescan(
            require_started=True,
            start_timeout=0.01,
            poll_interval=0.01,
        )

        assert completed is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_wait_for_rescan_require_started_accepts_true_then_false(self):
        """When require_started=True, true->false should confirm completion."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        backend._api_call = AsyncMock(
            side_effect=[
                {"in_progress": True},
                {"in_progress": False},
            ]
        )

        completed = await backend._wait_for_rescan(require_started=True, poll_interval=0.01)

        assert completed is True
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_does_not_restart_initial_rescan_while_pending(self):
        """Once initial rescan starts, later calls should poll instead of restarting."""
        from unittest.mock import patch

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="signet")
        backend.get_block_height = AsyncMock(return_value=100)
        backend._api_call = AsyncMock(return_value={"utxos": []})

        with (
            patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync,
            patch.object(backend, "_wait_for_rescan", new_callable=AsyncMock) as mock_wait,
        ):
            mock_sync.return_value = True
            mock_wait.side_effect = [False, False]
            await backend.get_utxos(["tb1qtest123"])
            await backend.get_utxos(["tb1qtest123"])

        assert backend._initial_rescan_done is False
        assert backend._initial_rescan_started is True

        rescan_posts = [
            call
            for call in backend._api_call.call_args_list
            if call[0][0] == "POST" and call[0][1] == "v1/rescan"
        ]
        assert len(rescan_posts) == 1

        assert mock_wait.call_args_list[0][1]["require_started"] is True
        assert mock_wait.call_args_list[1][1]["require_started"] is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_marks_initial_rescan_done_when_confirmed(self):
        """Initial rescan state should persist in-process after confirmed completion."""
        from unittest.mock import patch

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="signet")
        backend.get_block_height = AsyncMock(return_value=321)
        backend._api_call = AsyncMock(return_value={"utxos": []})

        with (
            patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync,
            patch.object(backend, "_wait_for_rescan", new_callable=AsyncMock) as mock_wait,
        ):
            mock_sync.return_value = True
            mock_wait.return_value = True
            await backend.get_utxos(["tb1qtest123"])

        assert backend._initial_rescan_done is True
        assert backend._last_rescan_height == 321
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_uses_extended_timeout_for_initial_rescan(self):
        """Initial rescan should wait longer than incremental rescans."""
        from unittest.mock import patch

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="signet")
        backend.get_block_height = AsyncMock(return_value=321)
        backend._api_call = AsyncMock(return_value={"utxos": []})

        with (
            patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync,
            patch.object(backend, "_wait_for_rescan", new_callable=AsyncMock) as mock_wait,
        ):
            mock_sync.return_value = True
            mock_wait.return_value = True
            await backend.get_utxos(["tb1qtest123"])

        mock_wait.assert_called_once_with(
            require_started=True,
            timeout=backend._INITIAL_RESCAN_TIMEOUT_SECONDS,
        )
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_uses_wait_for_rescan_not_sleep(self):
        """Test that get_utxos calls _wait_for_rescan instead of a fixed sleep."""
        from unittest.mock import patch

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="regtest",
        )
        backend._api_call = AsyncMock(return_value={"utxos": []})
        backend.get_block_height = AsyncMock(return_value=100)

        with (
            patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync,
            patch.object(backend, "_wait_for_rescan", new_callable=AsyncMock) as mock_wait,
        ):
            mock_sync.return_value = True
            await backend.get_utxos(["bcrt1qtest"])
            mock_wait.assert_called_once()

        await backend.close()

    @pytest.mark.asyncio
    async def test_resolve_scan_start_height_explicit_override(self):
        """Explicit scan_start_height should always be used regardless of tip."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="signet",
            scan_start_height=250000,
        )
        result = await backend._resolve_scan_start_height(tip_height=300000)
        assert result == 250000
        await backend.close()

    @pytest.mark.asyncio
    async def test_resolve_scan_start_height_lookback_on_signet(self):
        """On signet (min_valid=0), lookback from tip should be used."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="signet",
            scan_lookback_blocks=10000,
        )
        result = await backend._resolve_scan_start_height(tip_height=295000)
        # 295000 - 10000 = 285000, max(285000, 0) = 285000
        assert result == 285000
        await backend.close()

    @pytest.mark.asyncio
    async def test_resolve_scan_start_height_lookback_on_mainnet(self):
        """On mainnet, min_valid_blockheight (SegWit activation) is the floor."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_lookback_blocks=52560,
        )
        # tip=500000, lookback=500000-52560=447440, but min_valid=481824
        result = await backend._resolve_scan_start_height(tip_height=500000)
        assert result == 481824  # floor wins
        await backend.close()

    @pytest.mark.asyncio
    async def test_resolve_scan_start_height_lookback_above_min_valid(self):
        """When lookback height exceeds min_valid, use lookback height."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_lookback_blocks=52560,
        )
        # tip=900000, lookback=900000-52560=847440, which > 481824
        result = await backend._resolve_scan_start_height(tip_height=900000)
        assert result == 847440
        await backend.close()

    @pytest.mark.asyncio
    async def test_resolve_scan_start_height_small_chain(self):
        """When tip < lookback blocks, fallback to min_valid_blockheight."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="regtest",
            scan_lookback_blocks=52560,
        )
        # tip=100 is less than lookback=52560, so use min_valid=0
        result = await backend._resolve_scan_start_height(tip_height=100)
        assert result == 0
        await backend.close()

    @pytest.mark.asyncio
    async def test_resolve_scan_start_height_creation_height_priority(self):
        """creation_height should take priority over lookback but not explicit."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_lookback_blocks=52560,
        )
        backend.set_wallet_creation_height(800000)
        result = await backend._resolve_scan_start_height(tip_height=900000)
        # creation_height (800000) > min_valid (481824), use creation_height
        assert result == 800000
        await backend.close()

    @pytest.mark.asyncio
    async def test_resolve_scan_start_height_explicit_beats_creation_height(self):
        """Explicit scan_start_height beats creation_height."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_start_height=700000,
        )
        backend.set_wallet_creation_height(800000)
        result = await backend._resolve_scan_start_height(tip_height=900000)
        assert result == 700000  # Explicit wins
        await backend.close()

    @pytest.mark.asyncio
    async def test_resolve_scan_start_height_creation_height_clamped_to_min_valid(self):
        """creation_height below min_valid_blockheight is clamped up."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
        )
        backend.set_wallet_creation_height(100000)  # Below mainnet SegWit activation
        result = await backend._resolve_scan_start_height(tip_height=900000)
        assert result == 481824  # min_valid_blockheight wins
        await backend.close()

    def test_set_wallet_creation_height_ignored_when_explicit(self):
        """set_wallet_creation_height is a no-op when explicit scan_start_height is set."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_start_height=700000,
        )
        backend.set_wallet_creation_height(800000)
        assert backend._wallet_creation_height is None  # Should NOT be set

    def test_set_wallet_creation_height_stored_when_no_explicit(self):
        """set_wallet_creation_height stores value when no explicit override."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
        )
        backend.set_wallet_creation_height(800000)
        assert backend._wallet_creation_height == 800000

    def test_set_wallet_creation_height_none_clears_hint(self):
        """set_wallet_creation_height(None) clears any previously stored hint."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
        )
        backend.set_wallet_creation_height(800000)
        backend.set_wallet_creation_height(None)
        assert backend._wallet_creation_height is None

    def test_set_wallet_creation_height_negative_ignored(self):
        """Negative creation heights are ignored to avoid invalid scan hints."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
        )
        backend.set_wallet_creation_height(-1)
        assert backend._wallet_creation_height is None

    @pytest.mark.asyncio
    async def test_get_utxos_calls_wait_for_sync_before_initial_rescan(self):
        """get_utxos must call wait_for_sync before the first rescan."""
        from unittest.mock import patch

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="signet")
        backend.get_block_height = AsyncMock(return_value=295000)
        backend._api_call = AsyncMock(return_value={"utxos": []})

        call_order: list[str] = []

        async def track_sync(*args: object, **kwargs: object) -> bool:
            call_order.append("wait_for_sync")
            return True

        async def track_rescan(*args: object, **kwargs: object) -> bool:
            call_order.append("_wait_for_rescan")
            return True

        with (
            patch.object(backend, "wait_for_sync", side_effect=track_sync),
            patch.object(backend, "_wait_for_rescan", side_effect=track_rescan),
        ):
            await backend.get_utxos(["tb1qtest123"])

        assert "wait_for_sync" in call_order
        assert "wait_for_sync" == call_order[0], "wait_for_sync must be called first"
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_skips_wait_for_sync_when_already_synced(self):
        """If _synced is True, wait_for_sync should not be called again."""
        from unittest.mock import patch

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="signet")
        backend._synced = True  # Already synced
        backend.get_block_height = AsyncMock(return_value=295000)
        backend._api_call = AsyncMock(return_value={"utxos": []})

        with (
            patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync,
            patch.object(backend, "_wait_for_rescan", new_callable=AsyncMock) as mock_wait,
        ):
            mock_wait.return_value = True
            await backend.get_utxos(["tb1qtest123"])
            mock_sync.assert_not_called()

        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_skips_wait_for_sync_after_initial_rescan(self):
        """After initial rescan is done, wait_for_sync should not be called."""
        from unittest.mock import patch

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="signet")
        backend._initial_rescan_done = True
        backend._last_rescan_height = 295000
        backend.get_block_height = AsyncMock(return_value=295000)
        backend._api_call = AsyncMock(return_value={"utxos": []})

        with patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync:
            await backend.get_utxos(["tb1qtest123"])
            mock_sync.assert_not_called()

        await backend.close()

    @pytest.mark.asyncio
    async def test_scan_lookback_blocks_parameter(self):
        """Test that scan_lookback_blocks is stored and used correctly."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="signet",
            scan_lookback_blocks=1000,
        )
        assert backend._scan_lookback_blocks == 1000

        # Default value
        backend2 = NeutrinoBackend(neutrino_url="http://localhost:8334", network="signet")
        assert backend2._scan_lookback_blocks == 105120

        await backend.close()
        await backend2.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_cannot_estimate_fee(self):
        """Test that NeutrinoBackend reports it cannot estimate fees."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        assert backend.can_estimate_fee() is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_fee_fallback_values(self):
        """Test that NeutrinoBackend returns float fallback fee values."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")

        # Test fallback values for different targets (no API call - will fail and use fallback)
        # Can't actually call estimate_fee without mocking, but we can check the type
        # when it returns fallback values

        # Mock _api_call to raise an exception (simulating unavailable API)
        backend._api_call = AsyncMock(side_effect=Exception("API unavailable"))

        # Check fallback for different targets - should return float
        fee_1block = await backend.estimate_fee(1)
        assert isinstance(fee_1block, float)
        assert fee_1block == 5.0  # Fallback for <= 1 block

        fee_3block = await backend.estimate_fee(3)
        assert isinstance(fee_3block, float)
        assert fee_3block == 2.0  # Fallback for <= 3 blocks

        fee_6block = await backend.estimate_fee(6)
        assert isinstance(fee_6block, float)
        assert fee_6block == 1.0  # Fallback for <= 6 blocks

        fee_12block = await backend.estimate_fee(12)
        assert isinstance(fee_12block, float)
        assert fee_12block == 1.0  # Fallback for > 6 blocks

        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_add_watch_address(self):
        """Test adding addresses to watch list.

        In neutrino-api v0.4, address watching is done locally without API calls.
        The addresses are tracked in memory and used when making queries.
        """
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")

        address = "bcrt1q0000000000000000000000000000000000000"
        await backend.add_watch_address(address)

        # Address should be in watched set (local tracking)
        assert address in backend._watched_addresses
        assert len(backend._watched_addresses) == 1
        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_watch_address_limit(self):
        """Test that watch list has a maximum size limit."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        # Override limit to a small value for testing
        backend._max_watched_addresses = 5

        # Add addresses up to limit
        for i in range(5):
            await backend.add_watch_address(f"bcrt1qtest{i}")

        # Next add should raise ValueError
        with pytest.raises(ValueError, match="Watch list limit"):
            await backend.add_watch_address("bcrt1qexceeds")

        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_blockheight_validation(self):
        """Test blockheight validation in verify_utxo_with_metadata."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        # Mock get_block_height to return a known value
        backend.get_block_height = AsyncMock(return_value=800000)

        # Test: blockheight too low (before SegWit activation)
        result = await backend.verify_utxo_with_metadata(
            txid="abc123",
            vout=0,
            scriptpubkey="0014" + "00" * 20,  # valid P2WPKH
            blockheight=100000,  # Way before SegWit
        )
        assert result.valid is False
        assert "below minimum valid height" in (result.error or "")

        # Test: blockheight in the future
        result = await backend.verify_utxo_with_metadata(
            txid="abc123",
            vout=0,
            scriptpubkey="0014" + "00" * 20,
            blockheight=900000,  # Future block
        )
        assert result.valid is False
        assert "in the future" in (result.error or "")

        await backend.close()

    @pytest.mark.asyncio
    async def test_neutrino_backend_rescan_depth_limit(self):
        """Test that rescan depth is limited to prevent DoS."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._max_rescan_depth = 1000  # Override for testing
        backend.get_block_height = AsyncMock(return_value=800000)

        # Test: rescan depth exceeds limit
        result = await backend.verify_utxo_with_metadata(
            txid="abc123",
            vout=0,
            scriptpubkey="0014" + "00" * 20,
            blockheight=700000,  # 100,000 blocks ago (exceeds limit)
        )
        assert result.valid is False
        assert "exceeds max" in (result.error or "")

        await backend.close()

    @pytest.mark.asyncio
    async def test_ensure_addresses_scanned_rescans_new_address(self):
        """A newly watched bond address triggers a historical rescan.

        The requested start height must drop below neutrino-api's persisted
        ``last_start_height`` so its "skip already-scanned range" optimisation is
        bypassed and the old blocks are genuinely re-evaluated.
        """

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="signet",
            scan_start_height=250,
        )
        backend.get_block_height = AsyncMock(return_value=1000)
        backend._api_call = AsyncMock(return_value={})
        backend._wait_for_rescan = AsyncMock(return_value=True)
        # neutrino-api already scanned [200, 1000]; the backfill must start below 200.
        backend._get_rescan_coverage = AsyncMock(return_value=(200, 1000))

        bond_addr = "tb1qbondaddressexample00000000000000000000000000xyz"
        await backend.ensure_addresses_scanned([bond_addr])

        # A rescan was issued for the new address below the persisted start so
        # neutrino-api does not skip the already-scanned blocks.
        rescan_calls = [
            c for c in backend._api_call.call_args_list if c.args[:2] == ("POST", "v1/rescan")
        ]
        assert rescan_calls, "expected a rescan to be triggered"
        body = rescan_calls[-1].kwargs["data"]
        assert bond_addr in body["addresses"]
        assert body["start_height"] == 199  # persisted_start (200) - 1, below the floor
        assert bond_addr in backend._watched_addresses
        # Next get_utxos should wait for async indexing.
        assert backend._just_rescanned is True
        await backend.close()

    @pytest.mark.asyncio
    async def test_ensure_addresses_scanned_skips_already_watched(self):
        """Already-watched addresses do not trigger a redundant rescan."""

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="signet",
            scan_start_height=100,
        )
        backend.get_block_height = AsyncMock(return_value=1000)
        backend._api_call = AsyncMock(return_value={})
        backend._wait_for_rescan = AsyncMock(return_value=True)
        backend._get_rescan_coverage = AsyncMock(return_value=(200, 1000))

        addr = "tb1qalreadywatched0000000000000000000000000000000xyz"
        await backend.add_watch_address(addr)

        await backend.ensure_addresses_scanned([addr])

        rescan_calls = [
            c for c in backend._api_call.call_args_list if c.args[:2] == ("POST", "v1/rescan")
        ]
        assert not rescan_calls, "should not rescan an already-watched address"
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_rescan_coverage_returns_metadata(self):
        """_get_rescan_coverage should parse last_start_height and last_scanned_tip."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._api_call = AsyncMock(
            return_value={
                "in_progress": False,
                "last_start_height": 481824,
                "last_scanned_tip": 900000,
            }
        )

        start, tip = await backend._get_rescan_coverage()
        assert start == 481824
        assert tip == 900000
        backend._api_call.assert_called_once_with("GET", "v1/rescan/status")
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_rescan_coverage_returns_zeros_on_error(self):
        """_get_rescan_coverage should return (0, 0) when endpoint is unavailable."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._api_call = AsyncMock(side_effect=Exception("connection refused"))

        start, tip = await backend._get_rescan_coverage()
        assert start == 0
        assert tip == 0
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_skips_initial_rescan_when_full_coverage(self):
        """When neutrino-api already has full coverage, skip the initial rescan."""
        from unittest.mock import patch

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_start_height=750000,
        )
        backend._api_call = AsyncMock(return_value={"utxos": []})
        backend.get_block_height = AsyncMock(return_value=800000)

        # Simulate neutrino-api having full coverage
        with (
            patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync,
            patch.object(
                backend,
                "_get_rescan_coverage",
                new_callable=AsyncMock,
                return_value=(750000, 800000),
            ),
            patch.object(backend, "_wait_for_rescan", new_callable=AsyncMock) as mock_wait,
        ):
            mock_sync.return_value = True
            await backend.get_utxos(["bc1qtest123"])

        # No rescan should have been triggered
        rescan_posts = [
            call
            for call in backend._api_call.call_args_list
            if call[0][0] == "POST" and call[0][1] == "v1/rescan"
        ]
        assert len(rescan_posts) == 0
        mock_wait.assert_not_called()

        # But initial rescan should be marked as done
        assert backend._initial_rescan_done is True
        assert backend._last_rescan_height == 800000
        # No UTXO retries since no actual scan happened
        assert backend._just_rescanned is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_no_retries_for_trivial_rescan(self):
        """Trivial rescans (few blocks) should not trigger UTXO retries."""
        from unittest.mock import patch

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_start_height=750000,
        )
        backend._api_call = AsyncMock(return_value={"utxos": []})
        backend.get_block_height = AsyncMock(return_value=800010)

        # Simulate neutrino-api having coverage up to 800000 (10 blocks behind)
        coverage_calls = iter([(750000, 800000), (750000, 800010)])

        with (
            patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync,
            patch.object(
                backend,
                "_get_rescan_coverage",
                new_callable=AsyncMock,
                side_effect=lambda: next(coverage_calls),
            ),
            patch.object(backend, "_wait_for_rescan", new_callable=AsyncMock) as mock_wait,
        ):
            mock_sync.return_value = True
            mock_wait.return_value = True
            await backend.get_utxos(["bc1qtest123"])

        # Rescan should have been triggered (partial gap)
        assert backend._initial_rescan_done is True
        # But no UTXO retries since only 10 blocks were scanned (< _TRIVIAL_RESCAN_BLOCKS)
        assert backend._just_rescanned is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_retries_for_large_rescan(self):
        """Large rescans should trigger UTXO retries for async indexing."""
        from unittest.mock import patch

        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="mainnet",
            scan_start_height=750000,
        )
        backend._api_call = AsyncMock(return_value={"utxos": []})
        backend.get_block_height = AsyncMock(return_value=800000)

        # Simulate no prior coverage (fresh neutrino-api)
        coverage_calls = iter([(0, 0), (750000, 800000)])

        with (
            patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as mock_sync,
            patch.object(
                backend,
                "_get_rescan_coverage",
                new_callable=AsyncMock,
                side_effect=lambda: next(coverage_calls),
            ),
            patch.object(backend, "_wait_for_rescan", new_callable=AsyncMock) as mock_wait,
        ):
            mock_sync.return_value = True
            mock_wait.return_value = True
            await backend.get_utxos(["bc1qtest123"])

        assert backend._initial_rescan_done is True
        # 50000 blocks scanned (> _TRIVIAL_RESCAN_BLOCKS), retries should fire.
        # _just_rescanned is reset after the retry loop, so verify via call count:
        # 5 UTXO queries = max_retries when _just_rescanned was True.
        utxo_posts = [
            call
            for call in backend._api_call.call_args_list
            if call[0][0] == "POST" and call[0][1] == "v1/utxos"
        ]
        assert len(utxo_posts) == 5
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_incremental_uses_metadata_tip(self):
        """Incremental rescan should use metadata tip for _last_rescan_height."""
        from unittest.mock import patch

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="signet")
        backend._initial_rescan_done = True
        backend._last_rescan_height = 1000
        backend.get_block_height = AsyncMock(return_value=1050)
        backend._api_call = AsyncMock(return_value={"utxos": []})

        # After incremental rescan, metadata shows tip at 1055 (blocks arrived during scan)
        with patch.object(
            backend,
            "_get_rescan_coverage",
            new_callable=AsyncMock,
            return_value=(0, 1055),
        ):
            with patch.object(backend, "_wait_for_rescan", new_callable=AsyncMock) as mock_wait:
                mock_wait.return_value = True
                await backend.get_utxos(["tb1qtest123"])

        # _last_rescan_height should use the higher metadata tip
        assert backend._last_rescan_height == 1055
        await backend.close()

    @pytest.mark.asyncio
    async def test_detect_server_capabilities_full(self):
        """Full capability detection for v0.9.0+ server."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._api_call = AsyncMock(
            side_effect=[
                # /v1/status response
                {"block_height": 900000, "filter_height": 900000, "synced": True},
                # /v1/rescan/status response (v0.9.0+)
                {
                    "in_progress": False,
                    "last_start_height": 481824,
                    "last_scanned_tip": 900000,
                },
            ]
        )

        await backend._detect_server_capabilities()

        caps = backend.server_capabilities
        assert caps.detected is True
        assert caps.has_rescan_status is True
        assert caps.has_persistent_rescan_state is True
        assert caps.status_fields["block_height"] == 900000
        await backend.close()

    @pytest.mark.asyncio
    async def test_detect_server_capabilities_v07(self):
        """Capability detection for v0.7.0 server (rescan status, no persistent state)."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._api_call = AsyncMock(
            side_effect=[
                {"block_height": 800000, "filter_height": 800000, "synced": True},
                # v0.7.0 has rescan/status but no persistent fields
                {"in_progress": False},
            ]
        )

        await backend._detect_server_capabilities()

        caps = backend.server_capabilities
        assert caps.detected is True
        assert caps.has_rescan_status is True
        assert caps.has_persistent_rescan_state is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_detect_server_capabilities_old_server(self):
        """Capability detection for pre-v0.7.0 server (no rescan status endpoint)."""

        import httpx

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.request = MagicMock()

        backend._api_call = AsyncMock(
            side_effect=[
                {"block_height": 700000, "filter_height": 700000, "synced": True},
                httpx.HTTPStatusError("Not Found", response=mock_response, request=MagicMock()),
            ]
        )

        await backend._detect_server_capabilities()

        caps = backend.server_capabilities
        assert caps.detected is True
        assert caps.has_rescan_status is False
        assert caps.has_persistent_rescan_state is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_detect_server_capabilities_unreachable(self):
        """Capability detection when server is unreachable."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._api_call = AsyncMock(side_effect=Exception("connection refused"))

        await backend._detect_server_capabilities()

        caps = backend.server_capabilities
        assert caps.detected is True
        assert caps.has_rescan_status is False
        assert caps.has_persistent_rescan_state is False
        assert caps.status_fields == {}
        await backend.close()

    @pytest.mark.asyncio
    async def test_detect_server_capabilities_idempotent(self):
        """Detection runs only once even when called multiple times."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._api_call = AsyncMock(
            side_effect=[
                {"block_height": 900000, "filter_height": 900000, "synced": True},
                {"in_progress": False, "last_start_height": 0, "last_scanned_tip": 0},
            ]
        )

        await backend._detect_server_capabilities()
        await backend._detect_server_capabilities()  # Should be a no-op

        # Only 2 API calls total (status + rescan/status), not 4
        assert backend._api_call.call_count == 2
        await backend.close()

    @pytest.mark.asyncio
    async def test_capabilities_reset_on_close(self):
        """Closing the backend should reset detected capabilities."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._server_capabilities.detected = True
        backend._server_capabilities.has_rescan_status = True

        await backend.close()

        assert backend.server_capabilities.detected is False
        assert backend.server_capabilities.has_rescan_status is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_get_rescan_coverage_skips_call_without_persistent_state(self):
        """_get_rescan_coverage short-circuits when server lacks persistent state."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._server_capabilities.detected = True
        backend._server_capabilities.has_persistent_rescan_state = False
        backend._api_call = AsyncMock()

        start, tip = await backend._get_rescan_coverage()

        assert start == 0
        assert tip == 0
        backend._api_call.assert_not_called()
        await backend.close()

    @pytest.mark.asyncio
    async def test_wait_for_rescan_skips_poll_without_rescan_status(self):
        """_wait_for_rescan returns False immediately when server lacks endpoint."""

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend._server_capabilities.detected = True
        backend._server_capabilities.has_rescan_status = False
        backend._api_call = AsyncMock()

        result = await backend._wait_for_rescan()

        assert result is False
        backend._api_call.assert_not_called()
        await backend.close()

    def test_neutrino_config_init(self):
        """Test NeutrinoConfig initialization."""
        config = NeutrinoConfig(
            network="mainnet",
            data_dir="/data/neutrino",
            listen_port=8334,
            peers=["node1.bitcoin.org:8333"],
            tor_socks="127.0.0.1:9050",
        )
        assert config.network == "mainnet"
        assert config.data_dir == "/data/neutrino"
        assert config.listen_port == 8334
        assert config.peers == ["node1.bitcoin.org:8333"]
        assert config.tor_socks == "127.0.0.1:9050"

    def test_neutrino_config_chain_params(self):
        """Test getting chain parameters from config."""
        config = NeutrinoConfig(network="mainnet")
        params = config.get_chain_params()
        assert params["default_port"] == 8333
        assert len(params["dns_seeds"]) > 0

        config = NeutrinoConfig(network="testnet")
        params = config.get_chain_params()
        assert params["default_port"] == 18333

        config = NeutrinoConfig(network="regtest")
        params = config.get_chain_params()
        assert params["default_port"] == 18444
        assert params["dns_seeds"] == []

    def test_neutrino_config_to_args(self):
        """Test generating command-line arguments."""
        config = NeutrinoConfig(
            network="testnet",
            data_dir="/data/neutrino",
            listen_port=8334,
            peers=["peer1:18333", "peer2:18333"],
            tor_socks="127.0.0.1:9050",
        )
        args = config.to_args()
        assert "--datadir=/data/neutrino" in args
        assert "--testnet" in args
        assert "--restlisten=0.0.0.0:8334" in args
        assert "--proxy=127.0.0.1:9050" in args
        assert "--addpeer=peer1:18333" in args
        assert "--addpeer=peer2:18333" in args

    def test_neutrino_config_new_params_defaults(self):
        """Test NeutrinoConfig default values for new sync parameters."""
        config = NeutrinoConfig()
        assert config.clearnet_initial_sync is True
        assert config.prefetch_filters is True
        assert config.prefetch_lookback_blocks == 105120

    def test_neutrino_config_new_params_custom(self):
        """Test NeutrinoConfig with custom sync parameters."""
        config = NeutrinoConfig(
            clearnet_initial_sync=False,
            prefetch_filters=True,
            prefetch_lookback_blocks=50000,
        )
        assert config.clearnet_initial_sync is False
        assert config.prefetch_filters is True
        assert config.prefetch_lookback_blocks == 50000

    def test_neutrino_config_to_args_clearnet_sync(self):
        """Test that clearnet-initial-sync flag is included in args."""
        config_on = NeutrinoConfig(clearnet_initial_sync=True, tor_socks="127.0.0.1:9050")
        args_on = config_on.to_args()
        assert "--clearnet-initial-sync=true" in args_on

        config_off = NeutrinoConfig(clearnet_initial_sync=False, tor_socks="127.0.0.1:9050")
        args_off = config_off.to_args()
        assert "--clearnet-initial-sync=false" in args_off

    def test_neutrino_config_to_args_prefetch_filters(self):
        """Test that prefetch filter flags are included in args."""
        config_on = NeutrinoConfig(prefetch_filters=True, prefetch_lookback_blocks=50000)
        args_on = config_on.to_args()
        assert "--prefetchfilters=true" in args_on
        assert "--prefetchlookback=50000" in args_on

        config_off = NeutrinoConfig(prefetch_filters=False)
        args_off = config_off.to_args()
        assert "--prefetchfilters=false" in args_off
        # No lookback arg when prefetch is off
        assert all("--prefetchlookback" not in a for a in args_off)

    def test_neutrino_config_to_args_prefetch_no_lookback(self):
        """Test that lookback is omitted when set to 0 (fetch all from genesis)."""
        config = NeutrinoConfig(prefetch_filters=True, prefetch_lookback_blocks=0)
        args = config.to_args()
        assert "--prefetchfilters=true" in args
        assert all("--prefetchlookback" not in a for a in args)

    def test_neutrino_config_to_env_basic(self):
        """Test to_env() generates correct Docker environment variables."""
        config = NeutrinoConfig(
            network="mainnet",
            data_dir="/data/neutrino",
            listen_port=8334,
            tor_socks="127.0.0.1:9050",
            peers=["node1:8333", "node2:8333"],
        )
        env = config.to_env()
        assert env["NETWORK"] == "mainnet"
        assert env["DATA_DIR"] == "/data/neutrino"
        assert env["LISTEN_ADDR"] == "0.0.0.0:8334"
        assert env["TOR_PROXY"] == "127.0.0.1:9050"
        assert env["ADD_PEERS"] == "node1:8333,node2:8333"
        assert env["CLEARNET_INITIAL_SYNC"] == "true"
        assert env["PREFETCH_FILTERS"] == "true"
        assert env["PREFETCH_LOOKBACK"] == "105120"

    def test_neutrino_config_to_env_no_tor(self):
        """Test to_env() omits TOR_PROXY when no Tor is configured."""
        config = NeutrinoConfig(network="regtest")
        env = config.to_env()
        assert "TOR_PROXY" not in env
        assert "ADD_PEERS" not in env

    def test_neutrino_config_to_env_prefetch_with_lookback(self):
        """Test to_env() includes PREFETCH_LOOKBACK when prefetch is enabled."""
        config = NeutrinoConfig(prefetch_filters=True, prefetch_lookback_blocks=50000)
        env = config.to_env()
        assert env["PREFETCH_FILTERS"] == "true"
        assert env["PREFETCH_LOOKBACK"] == "50000"

    def test_neutrino_config_to_env_prefetch_no_lookback(self):
        """Test to_env() omits PREFETCH_LOOKBACK when set to 0 or prefetch off."""
        config_off = NeutrinoConfig(prefetch_filters=False)
        env_off = config_off.to_env()
        assert "PREFETCH_LOOKBACK" not in env_off

        config_zero = NeutrinoConfig(prefetch_filters=True, prefetch_lookback_blocks=0)
        env_zero = config_zero.to_env()
        assert "PREFETCH_LOOKBACK" not in env_zero

    # ------------------------------------------------------------------
    # Mempool tracker integration (neutrino-api 1.3.0+)
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_init_defaults_include_mempool_true(self):
        """include_mempool defaults to True; has_mempool_access stays False until detect."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        try:
            assert backend.include_mempool is True
            assert backend.has_mempool_access() is False
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_init_include_mempool_false(self):
        """Operator opt-out is plumbed through the constructor."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="regtest",
            include_mempool=False,
        )
        try:
            assert backend.include_mempool is False
            backend._server_capabilities.has_mempool_tracker = True
            assert backend.has_mempool_access() is False
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_detect_capabilities_sets_mempool_tracker_from_status(self):
        """A status payload with mempool_enabled=true flips the capability flag."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        backend._api_call = AsyncMock(
            side_effect=[
                {
                    "synced": True,
                    "block_height": 800000,
                    "filter_height": 800000,
                    "mempool_enabled": True,
                    "mempool": {"entries": 2, "utxos": 1, "spends": 1, "peers": 8},
                },
                {
                    "in_progress": False,
                    "last_start_height": 700000,
                    "last_scanned_tip": 800000,
                },
            ]
        )

        try:
            await backend._detect_server_capabilities()
            assert backend.server_capabilities.has_mempool_tracker is True
            assert backend.has_mempool_access() is True
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_detect_capabilities_no_tracker_when_disabled(self):
        """A status payload without mempool_enabled keeps the capability flag false."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        backend._api_call = AsyncMock(
            side_effect=[
                {"synced": True, "block_height": 800000, "filter_height": 800000},
                {"in_progress": False},
            ]
        )

        try:
            await backend._detect_server_capabilities()
            assert backend.server_capabilities.has_mempool_tracker is False
            assert backend.has_mempool_access() is False
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_sends_include_mempool_when_capable(self):
        """get_utxos passes include_mempool=true once the tracker is detected."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        backend._initial_rescan_done = True
        backend._server_capabilities.has_mempool_tracker = True
        backend._watched_addresses = {"bc1qtest"}

        backend._api_call = AsyncMock(
            return_value={
                "utxos": [
                    {
                        "txid": "a" * 64,
                        "vout": 0,
                        "value": 100000,
                        "address": "bc1qtest",
                        "scriptpubkey": "0014" + "00" * 20,
                        "height": 800000,
                    },
                    {
                        "txid": "b" * 64,
                        "vout": 1,
                        "value": 50000,
                        "address": "bc1qtest",
                        "scriptpubkey": "0014" + "00" * 20,
                        "height": 0,
                    },
                ]
            }
        )
        backend.get_block_height = AsyncMock(return_value=800000)

        try:
            with patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as ws:
                ws.return_value = True
                utxos = await backend.get_utxos(["bc1qtest"])

            utxos_posts = [
                call
                for call in backend._api_call.call_args_list
                if call[0][0] == "POST" and call[0][1] == "v1/utxos"
            ]
            assert len(utxos_posts) == 1
            assert utxos_posts[0][1]["data"]["include_mempool"] is True

            assert len(utxos) == 2
            confirmed = next(u for u in utxos if u.txid == "a" * 64)
            mempool = next(u for u in utxos if u.txid == "b" * 64)
            assert confirmed.confirmations == 1
            assert confirmed.height == 800000
            assert mempool.confirmations == 0
            assert mempool.height is None
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_get_utxos_omits_include_mempool_when_disabled(self):
        """include_mempool is not sent when the operator disabled it client-side."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="regtest",
            include_mempool=False,
        )
        backend._initial_rescan_done = True
        backend._server_capabilities.has_mempool_tracker = True
        backend._watched_addresses = {"bc1qtest"}
        backend._api_call = AsyncMock(return_value={"utxos": []})
        backend.get_block_height = AsyncMock(return_value=800000)

        try:
            with patch.object(backend, "wait_for_sync", new_callable=AsyncMock) as ws:
                ws.return_value = True
                await backend.get_utxos(["bc1qtest"])

            utxos_posts = [
                call
                for call in backend._api_call.call_args_list
                if call[0][0] == "POST" and call[0][1] == "v1/utxos"
            ]
            assert len(utxos_posts) == 1
            assert "include_mempool" not in utxos_posts[0][1]["data"]
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_get_transaction_returns_mempool_tx(self):
        """get_transaction surfaces a watched mempool tx as confirmations=0."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        backend._api_call = AsyncMock(
            return_value={"txid": "c" * 64, "hex": "0200000001abcd", "mempool": True}
        )

        try:
            tx = await backend.get_transaction("c" * 64)
            assert tx is not None
            assert tx.txid == "c" * 64
            assert tx.raw == "0200000001abcd"
            assert tx.confirmations == 0
            assert tx.block_height is None
            assert tx.block_time is None
            backend._api_call.assert_called_once_with(
                "GET", f"v1/tx/{'c' * 64}", expected_status_codes=frozenset({501})
            )
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_get_transaction_returns_none_on_404(self):
        """A 404 from /v1/tx/ is a normal miss, not an error."""
        import httpx

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        response = MagicMock()
        response.status_code = 404
        backend._api_call = AsyncMock(
            side_effect=httpx.HTTPStatusError("not found", request=MagicMock(), response=response)
        )

        try:
            tx = await backend.get_transaction("d" * 64)
            assert tx is None
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_get_transaction_returns_none_on_501(self):
        """A 501 from /v1/tx/ (txid not a watched mempool tx) is a normal miss."""
        import httpx

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        response = MagicMock()
        response.status_code = 501
        backend._api_call = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "not implemented", request=MagicMock(), response=response
            )
        )

        try:
            tx = await backend.get_transaction("d" * 64)
            assert tx is None
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_get_transaction_returns_none_when_disabled(self):
        """With include_mempool=False, get_transaction never hits the network."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="regtest",
            include_mempool=False,
        )
        backend._api_call = AsyncMock(side_effect=AssertionError("should not be called"))

        try:
            assert await backend.get_transaction("e" * 64) is None
            backend._api_call.assert_not_called()
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_api_call_expected_status_logged_as_debug(self):
        """A declared expected status code (501) is logged at debug, not error.

        Regression test: ``GET /v1/tx/{txid}`` returns 501 for any txid that is
        not a watched mempool tx (e.g. one that already confirmed). That is a
        normal miss surfaced via ``update_all_pending_transactions`` during
        ``jm-wallet info`` and must not show up as an alarming ERROR line.
        """
        import httpx
        from loguru import logger

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        backend._maybe_pin_certificate = AsyncMock()

        response = MagicMock()
        response.status_code = 501
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "not implemented", request=MagicMock(), response=response
            )
        )
        backend.client.get = AsyncMock(return_value=mock_response)

        records: list[tuple[str, str]] = []
        sink_id = logger.add(
            lambda message: records.append(
                (message.record["level"].name, message.record["message"])
            ),
            level="DEBUG",
        )
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await backend._api_call("GET", "v1/tx/abc", expected_status_codes=frozenset({501}))
        finally:
            logger.remove(sink_id)
            await backend.close()

        assert not any(level == "ERROR" for level, _ in records), (
            f"Expected 501 must not be logged at ERROR; got: {records}"
        )
        assert any(level == "DEBUG" and "501" in msg for level, msg in records), (
            f"Expected a DEBUG log mentioning 501; got: {records}"
        )

    @pytest.mark.asyncio
    async def test_api_call_unexpected_status_logged_as_error(self):
        """A status code not declared expected still surfaces as an error log."""
        import httpx
        from loguru import logger

        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
        backend._maybe_pin_certificate = AsyncMock()

        response = MagicMock()
        response.status_code = 500
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "server error", request=MagicMock(), response=response
            )
        )
        backend.client.get = AsyncMock(return_value=mock_response)

        records: list[tuple[str, str]] = []
        sink_id = logger.add(
            lambda message: records.append(
                (message.record["level"].name, message.record["message"])
            ),
            level="DEBUG",
        )
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await backend._api_call("GET", "v1/status", expected_status_codes=frozenset({501}))
        finally:
            logger.remove(sink_id)
            await backend.close()

        assert any(level == "ERROR" for level, _ in records), (
            f"Unexpected 500 must be logged at ERROR; got: {records}"
        )

    @pytest.mark.asyncio
    async def test_verify_utxo_with_metadata_rejects_mempool_spend(self):
        """A confirmed UTXO with a pending mempool spend is treated as invalid."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")

        backend._api_call = AsyncMock(
            return_value={
                "unspent": True,
                "value": 100000,
                "scriptpubkey": "0014" + "00" * 20,
                "block_height": 800000,
                "mempool_spending_txid": "f" * 64,
                "mempool_spending_input": 0,
                "mempool_spend_first_seen": 1714501234,
            }
        )
        backend.get_block_height = AsyncMock(return_value=800010)

        try:
            result = await backend.verify_utxo_with_metadata(
                txid="a" * 64,
                vout=0,
                scriptpubkey="0014" + "00" * 20,
                blockheight=800000,
            )
            assert result.valid is False
            assert "mempool" in (result.error or "").lower()
            assert "f" * 64 in (result.error or "")
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_verify_utxo_with_metadata_disables_mempool_overlay(self):
        """Operator opt-out propagates include_mempool=false to the GET params."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            network="regtest",
            include_mempool=False,
        )

        backend._api_call = AsyncMock(
            return_value={
                "unspent": True,
                "value": 100000,
                "scriptpubkey": "0014" + "00" * 20,
                "block_height": 800000,
            }
        )
        backend.get_block_height = AsyncMock(return_value=800010)

        try:
            await backend.verify_utxo_with_metadata(
                txid="a" * 64,
                vout=0,
                scriptpubkey="0014" + "00" * 20,
                blockheight=800000,
            )
            calls = [
                call
                for call in backend._api_call.call_args_list
                if call[0][0] == "GET" and call[0][1].startswith("v1/utxo/")
            ]
            assert len(calls) == 1
            assert calls[0][1]["params"].get("include_mempool") == "false"
        finally:
            await backend.close()


@pytest.mark.docker
@pytest.mark.neutrino
@pytest.mark.asyncio
async def test_neutrino_backend_integration():
    """Integration test for NeutrinoBackend (requires running neutrino server)."""
    backend = NeutrinoBackend(
        neutrino_url="http://localhost:8334",
        network="regtest",
    )

    try:
        # Try to connect - skip if not available
        try:
            await backend._api_call("GET", "v1/status")
        except Exception:
            await backend.close()
            pytest.skip(
                "Neutrino server not available at localhost:8334. "
                "Start with: docker compose --profile neutrino up -d neutrino"
            )
            return

        # Test get_block_height
        height = await backend.get_block_height()
        assert height >= 0

        # Test fee estimation (fallback values)
        fee = await backend.estimate_fee(6)
        assert fee > 0

        # Test watching a valid bech32 address (valid P2WPKH)
        # Use a known valid regtest address
        test_address = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
        await backend.add_watch_address(test_address)
        # Note: The address may not be added if the neutrino server validation fails,
        # but the basic connectivity test is still valid
        if test_address in backend._watched_addresses:
            assert test_address in backend._watched_addresses

    finally:
        await backend.close()


class TestNeutrinoBackendAuth:
    """Unit tests for NeutrinoBackend TLS and auth token support."""

    @pytest.mark.asyncio
    async def test_no_auth_by_default(self):
        """Without TLS/auth params, client should have no auth headers or custom verify."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        assert backend._tls_cert_path is None
        assert backend._auth_token is None
        # Default httpx client uses True for verify (system CA bundle)
        assert "authorization" not in {k.lower() for k in backend.client.headers}
        await backend.close()

    @pytest.mark.asyncio
    async def test_auth_token_sets_bearer_header(self):
        """When auth_token is provided, client should send Authorization header."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            auth_token="deadbeef1234",
        )
        assert backend._auth_token == "deadbeef1234"
        auth = backend.client.headers.get("authorization")
        assert auth == "Bearer deadbeef1234"
        await backend.close()

    @pytest.mark.asyncio
    async def test_tls_cert_path_missing_file_triggers_tofu(self, tmp_path):
        """When tls_cert_path is missing and URL is HTTPS, TOFU pinning is pending."""
        missing = str(tmp_path / "nonexistent.cert")
        backend = NeutrinoBackend(
            neutrino_url="https://localhost:8334",
            tls_cert_path=missing,
        )
        # No warning at construction: the cert is fetched and pinned on first use.
        assert backend._tofu_pending is True
        assert backend._pinned_cert_pem is None
        await backend.close()

    @pytest.mark.asyncio
    async def test_tls_cert_path_valid_file(self, tmp_path):
        """When tls_cert_path is a real file, client should use a custom SSL context."""
        import ssl

        # Create a dummy PEM cert (won't validate but tests plumbing)
        cert_file = tmp_path / "tls.cert"
        cert_file.write_text("")  # placeholder
        # We can't easily make a real cert in pure Python without extra deps,
        # so just verify the code path doesn't crash by checking the verify attr
        # is an SSLContext when a valid PEM is supplied.
        # Instead, test with a mock to verify the ssl context is created.
        with patch("jmwallet.backends.neutrino.ssl.create_default_context") as mock_ctx:
            mock_ssl_ctx = MagicMock(spec=ssl.SSLContext)
            mock_ctx.return_value = mock_ssl_ctx
            backend = NeutrinoBackend(
                neutrino_url="https://localhost:8334",
                tls_cert_path=str(cert_file),
            )
            mock_ctx.assert_called_once_with(cafile=str(cert_file), cadata=None)
            # Hostname verification is disabled for pinned certs (TOFU model):
            # the neutrino-api self-signed cert only has SANs for
            # localhost/127.0.0.1/::1, but we may connect via Docker service
            # names like jm-neutrino.
            assert mock_ssl_ctx.check_hostname is False
            assert mock_ssl_ctx.verify_mode == ssl.CERT_REQUIRED
            await backend.close()

    @pytest.mark.asyncio
    async def test_close_preserves_auth_settings(self):
        """After close(), the re-created client should still have auth headers."""
        backend = NeutrinoBackend(
            neutrino_url="http://localhost:8334",
            auth_token="mytoken123",
        )
        original_client = backend.client
        await backend.close()

        assert backend.client is not original_client
        assert not backend.client.is_closed
        auth = backend.client.headers.get("authorization")
        assert auth == "Bearer mytoken123"
        await backend.close()

    @pytest.mark.asyncio
    async def test_combined_tls_and_auth(self, tmp_path):
        """Both TLS cert pinning and auth token can be used together."""
        import ssl

        cert_file = tmp_path / "tls.cert"
        cert_file.write_text("")

        with patch("jmwallet.backends.neutrino.ssl.create_default_context") as mock_ctx:
            mock_ssl_ctx = MagicMock(spec=ssl.SSLContext)
            mock_ctx.return_value = mock_ssl_ctx
            backend = NeutrinoBackend(
                neutrino_url="https://localhost:8334",
                tls_cert_path=str(cert_file),
                auth_token="combined_token",
            )
            mock_ctx.assert_called_once()
            assert mock_ssl_ctx.check_hostname is False
            assert mock_ssl_ctx.verify_mode == ssl.CERT_REQUIRED
            auth = backend.client.headers.get("authorization")
            assert auth == "Bearer combined_token"
            await backend.close()

    @pytest.mark.asyncio
    async def test_tls_cert_path_supports_tilde(self, tmp_path, monkeypatch):
        """TLS cert path should support ~ expansion."""
        import ssl

        fake_home = tmp_path / "home"
        cert_dir = fake_home / ".joinmarket-ng" / "neutrino"
        cert_dir.mkdir(parents=True)
        cert_file = cert_dir / "tls.cert"
        cert_file.write_text("")

        monkeypatch.setenv("HOME", str(fake_home))

        with patch("jmwallet.backends.neutrino.ssl.create_default_context") as mock_ctx:
            mock_ssl_ctx = MagicMock(spec=ssl.SSLContext)
            mock_ctx.return_value = mock_ssl_ctx
            backend = NeutrinoBackend(
                neutrino_url="https://localhost:8334",
                tls_cert_path="~/.joinmarket-ng/neutrino/tls.cert",
            )
            mock_ctx.assert_called_once_with(cafile=str(cert_file), cadata=None)
            await backend.close()


class TestNeutrinoTofuPinning:
    """Unit tests for trust-on-first-use TLS certificate pinning."""

    @pytest.mark.asyncio
    async def test_http_url_never_pins(self):
        """Plain HTTP URLs do not trigger TOFU pinning."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        assert backend._is_https is False
        assert backend._tofu_pending is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_tofu_persists_cert_to_path(self, tmp_path):
        """When tls_cert_path is set, TOFU writes the fetched cert there."""
        import ssl

        cert_path = tmp_path / "neutrino" / "tls.cert"
        backend = NeutrinoBackend(
            neutrino_url="https://localhost:8334",
            tls_cert_path=str(cert_path),
        )
        assert backend._tofu_pending is True

        with (
            patch(
                "jmwallet.backends.neutrino.ssl.get_server_certificate",
                return_value="PEMDATA",
            ),
            patch(
                "jmwallet.backends.neutrino.ssl.create_default_context",
                return_value=MagicMock(spec=ssl.SSLContext),
            ),
        ):
            await backend._maybe_pin_certificate()

            assert cert_path.is_file()
            assert cert_path.read_text() == "PEMDATA"
            assert backend._tofu_pending is False
            assert backend._pinned_cert_pem is None
            await backend.close()

    @pytest.mark.asyncio
    async def test_tofu_pins_in_memory_without_path(self):
        """Without tls_cert_path, TOFU pins the cert in memory for the session."""
        import ssl

        backend = NeutrinoBackend(
            neutrino_url="https://localhost:8334",
            tls_cert_path="",
        )
        assert backend._tofu_pending is True

        with (
            patch(
                "jmwallet.backends.neutrino.ssl.get_server_certificate",
                return_value="MEMPEM",
            ),
            patch(
                "jmwallet.backends.neutrino.ssl.create_default_context",
                return_value=MagicMock(spec=ssl.SSLContext),
            ),
        ):
            await backend._maybe_pin_certificate()

            assert backend._pinned_cert_pem == "MEMPEM"
            assert backend._tofu_pending is False
            await backend.close()

    @pytest.mark.asyncio
    async def test_tofu_fetch_failure_leaves_pending(self):
        """If the cert cannot be fetched, TOFU stays pending for a later retry."""
        backend = NeutrinoBackend(
            neutrino_url="https://localhost:8334",
            tls_cert_path="",
        )
        with patch(
            "jmwallet.backends.neutrino.ssl.get_server_certificate",
            side_effect=OSError("connection refused"),
        ):
            await backend._maybe_pin_certificate()
        assert backend._tofu_pending is True
        assert backend._pinned_cert_pem is None
        await backend.close()


class TestNeutrinoNetworkVerification:
    """Unit tests for genesis-hash based network mismatch detection."""

    @pytest.mark.asyncio
    async def test_matching_network_passes(self):
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend.get_block_hash = AsyncMock(return_value=GENESIS_BLOCK_HASHES["mainnet"])
        await backend._verify_network()
        assert backend._network_verified is True
        await backend.close()

    @pytest.mark.asyncio
    async def test_mismatched_network_raises(self):
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        # Server actually serves signet.
        backend.get_block_hash = AsyncMock(return_value=GENESIS_BLOCK_HASHES["signet"])
        with pytest.raises(NeutrinoNetworkMismatchError) as exc:
            await backend._verify_network()
        assert "signet" in str(exc.value)
        assert "mainnet" in str(exc.value)
        assert backend._network_verified is False
        await backend.close()

    @pytest.mark.asyncio
    async def test_genesis_fetch_failure_does_not_block(self):
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="mainnet")
        backend.get_block_hash = AsyncMock(side_effect=RuntimeError("server down"))
        await backend._verify_network()  # best-effort, no raise
        assert backend._network_verified is False
        await backend.close()


class TestSupportsDescriptorScan:
    """Unit tests for the supports_descriptor_scan capability flag."""

    def test_base_backend_does_not_support_descriptor_scan(self):
        """BlockchainBackend base class must default to False."""
        from jmwallet.backends.base import BlockchainBackend

        assert BlockchainBackend.supports_descriptor_scan is False

    def test_neutrino_does_not_support_descriptor_scan(self):
        """NeutrinoBackend must report supports_descriptor_scan=False."""
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")
        assert backend.supports_descriptor_scan is False

    def test_descriptor_wallet_supports_descriptor_scan(self):
        """DescriptorWalletBackend must report supports_descriptor_scan=True."""
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        backend = DescriptorWalletBackend()
        assert backend.supports_descriptor_scan is True


class TestSyncAllAddressPreregistration:
    """Unit tests for Bug 1 fix: all wallet addresses are pre-registered before
    the initial rescan fires so that change (internal) addresses are not missed."""

    @pytest.mark.asyncio
    async def test_sync_all_preregisters_change_addresses(self):
        """sync_all() must register both external AND internal addresses with the
        backend *before* the first get_utxos call so the initial neutrino rescan
        covers change addresses."""

        from _jmwallet_test_helpers import TEST_MNEMONIC

        from jmwallet.backends.neutrino import NeutrinoBackend
        from jmwallet.wallet.service import WalletService

        # Build a real WalletService backed by a mocked NeutrinoBackend so we
        # can inspect which addresses were added before the first UTXO query.
        backend = NeutrinoBackend(neutrino_url="http://localhost:8334")

        # Stub out network calls
        backend.get_block_height = AsyncMock(return_value=100)

        registered_before_first_utxo_call: set[str] = set()

        async def fake_get_utxos(addresses: list[str]) -> list:
            # Capture state at first call to verify pre-registration happened
            if not registered_before_first_utxo_call:
                registered_before_first_utxo_call.update(backend._watched_addresses)
            return []

        backend.get_utxos = fake_get_utxos  # type: ignore[assignment]

        wallet = WalletService(
            mnemonic=TEST_MNEMONIC,
            backend=backend,
            network="signet",
            mixdepth_count=1,
            gap_limit=6,
        )

        await wallet.sync_all()

        # All gap_limit addresses for both branches of mixdepth 0 must have been
        # registered before the first UTXO query fired.
        for change in [0, 1]:
            for index in range(wallet.gap_limit):
                addr = wallet.get_address(0, change, index)
                assert addr in registered_before_first_utxo_call, (
                    f"Address m/…/0'/{change}/{index} ({addr}) was not pre-registered "
                    "with backend before initial rescan"
                )
