"""
Bitcoin Core Descriptor Wallet backend.

Uses descriptor wallets with importdescriptors RPC for efficient UTXO tracking.
This is much faster than scantxoutset for ongoing wallet operations as Bitcoin Core
maintains the UTXO state automatically.

Key advantages over scantxoutset:
1. Persistent tracking: Once descriptors are imported, UTXOs are tracked automatically
2. Real-time updates: Balance updates as blocks arrive, no need for full UTXO set scan
3. Efficient queries: listunspent is O(wallet UTXOs) vs O(entire UTXO set) for scantxoutset
4. Mempool awareness: Can see unconfirmed transactions immediately

Trade-offs:
1. Requires wallet creation/management on Bitcoin Core side
2. Wallet files persist on disk (privacy consideration)
3. Initial import can take time for large descriptor ranges
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from jmcore.bitcoin import btc_to_sats
from loguru import logger

from jmwallet.backends.base import UTXO, BlockchainBackend, Transaction

# Timeout for regular RPC calls (seconds)
DEFAULT_RPC_TIMEOUT = 30.0

# Timeout for descriptor import - can take a while for large ranges
IMPORT_RPC_TIMEOUT = 120.0

# Default gap limit for descriptor ranges
DEFAULT_GAP_LIMIT = 1000

# Default scan lookback period (approximately 1 year of blocks)
# Bitcoin averages ~144 blocks/day * 365 days ≈ 52,560 blocks
DEFAULT_SCAN_LOOKBACK_BLOCKS = 52_560

# Environment variable to enable sensitive logging (descriptors, addresses, etc.)
SENSITIVE_LOGGING = os.environ.get("SENSITIVE_LOGGING", "").lower() in ("1", "true", "yes")


class DescriptorWalletBackend(BlockchainBackend):
    supports_descriptor_scan: bool = True
    """
    Blockchain backend using Bitcoin Core descriptor wallets.

    This backend creates and manages a descriptor wallet in Bitcoin Core,
    importing xpub descriptors for efficient UTXO tracking. Once imported,
    Bitcoin Core automatically tracks UTXOs and provides fast queries via listunspent.

    Usage:
        backend = DescriptorWalletBackend(
            rpc_url="http://127.0.0.1:8332",
            rpc_user="user",
            rpc_password="pass",
            wallet_name="jm_wallet",
        )

        # Setup wallet and import descriptors (one-time or on startup)
        await backend.setup_wallet(descriptors)

        # Fast UTXO queries - no more full UTXO set scans
        utxos = await backend.get_utxos(addresses)
    """

    def __init__(
        self,
        rpc_url: str = "http://127.0.0.1:18443",
        rpc_user: str = "rpcuser",
        rpc_password: str = "rpcpassword",
        wallet_name: str = "jm_descriptor_wallet",
        import_timeout: float = IMPORT_RPC_TIMEOUT,
    ):
        """
        Initialize descriptor wallet backend.

        Args:
            rpc_url: Bitcoin Core RPC URL
            rpc_user: RPC username
            rpc_password: RPC password
            wallet_name: Name for the descriptor wallet in Bitcoin Core
            import_timeout: Timeout for descriptor import operations
        """
        self.rpc_url = rpc_url.rstrip("/")
        self.rpc_user = rpc_user
        self.rpc_password = rpc_password
        self.wallet_name = wallet_name
        self.import_timeout = import_timeout

        logger.info(f"Initialized DescriptorWalletBackend with wallet: {wallet_name}")

        # Client for regular RPC calls
        self.client = httpx.AsyncClient(timeout=DEFAULT_RPC_TIMEOUT, auth=(rpc_user, rpc_password))
        # Client for long-running import operations
        self._import_client = httpx.AsyncClient(
            timeout=import_timeout, auth=(rpc_user, rpc_password)
        )
        self._request_id = 0

        # Track if wallet is setup
        self._wallet_loaded = False
        self._descriptors_imported = False

        # Track background rescan status
        self._background_rescan_height: int | None = None

    def _get_wallet_url(self) -> str:
        """Get the RPC URL for wallet-specific calls."""
        return f"{self.rpc_url}/wallet/{self.wallet_name}"

    @staticmethod
    def _is_wallet_not_loaded_error(error: ValueError) -> bool:
        """Check if an RPC error indicates the wallet is not loaded (error -18)."""
        error_str = str(error)
        return "RPC error -18" in error_str

    async def _ensure_wallet_loaded(self) -> bool:
        """
        Ensure the wallet is loaded in Bitcoin Core.

        This handles the case where Bitcoin Core has been restarted and the
        wallet is no longer loaded. It checks listwallets first and attempts
        loadwallet if needed.

        Note: this intentionally does NOT set ``_wallet_loaded = False`` on
        failure. The flag means "the wallet was set up in this session" and
        should remain True so that future calls still attempt wallet-scoped
        RPC (which will trigger another reload attempt). Setting it to False
        would cause early returns in get_utxos/get_descriptor_ranges that
        silently skip all RPC, preventing recovery on the next rescan cycle.

        Returns:
            True if the wallet is loaded (or was successfully reloaded)
        """
        try:
            wallets = await self._rpc_call("listwallets", use_wallet=False)
            if self.wallet_name in wallets:
                return True

            # Wallet not in list -- attempt to load it
            await self._rpc_call("loadwallet", [self.wallet_name], use_wallet=False)
            logger.info(f"Reloaded wallet '{self.wallet_name}' after Bitcoin Core restart")
            return True
        except Exception as e:
            logger.error(f"Failed to reload wallet '{self.wallet_name}': {e}")
            return False

    async def _rpc_call(
        self,
        method: str,
        params: list | None = None,
        client: httpx.AsyncClient | None = None,
        use_wallet: bool = True,
    ) -> Any:
        """
        Make an RPC call to Bitcoin Core.

        If a wallet-scoped call fails with RPC error -18 (wallet not loaded),
        automatically attempts to reload the wallet and retries the call once.
        This handles Bitcoin Core restarts transparently.

        Args:
            method: RPC method name
            params: Method parameters
            client: Optional httpx client (uses default client if not provided)
            use_wallet: If True, use wallet-specific URL

        Returns:
            RPC result

        Raises:
            ValueError: On RPC errors
            httpx.HTTPError: On connection/timeout errors
        """
        result = await self._rpc_call_inner(method, params, client, use_wallet)
        return result

    async def _rpc_call_inner(
        self,
        method: str,
        params: list | None = None,
        client: httpx.AsyncClient | None = None,
        use_wallet: bool = True,
        _retried: bool = False,
    ) -> Any:
        """
        Internal RPC call implementation with automatic wallet reload on error -18.

        Args:
            method: RPC method name
            params: Method parameters
            client: Optional httpx client (uses default client if not provided)
            use_wallet: If True, use wallet-specific URL
            _retried: Internal flag to prevent infinite retry loops

        Returns:
            RPC result

        Raises:
            ValueError: On RPC errors
            httpx.HTTPError: On connection/timeout errors
        """
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or [],
        }

        use_client = client or self.client
        url = self._get_wallet_url() if use_wallet and self._wallet_loaded else self.rpc_url

        try:
            response = await use_client.post(url, json=payload)

            # Try to parse JSON response even if status code indicates error
            # Bitcoin Core may return 500 with valid JSON-RPC error details
            try:
                data = response.json()
            except Exception:
                # If JSON parsing fails, raise HTTP error
                response.raise_for_status()
                raise

            if "error" in data and data["error"]:
                error_info = data["error"]
                error_code = error_info.get("code", "unknown")
                error_msg = error_info.get("message", str(error_info))
                raise ValueError(f"RPC error {error_code}: {error_msg}")

            # Check HTTP status only after verifying no RPC error in response
            response.raise_for_status()

            return data.get("result")

        except httpx.TimeoutException as e:
            logger.error(f"RPC call timed out: {method} - {e}")
            raise
        except ValueError as e:
            # If this is a wallet-not-loaded error on a wallet-scoped call,
            # try to reload the wallet and retry once
            if (
                use_wallet
                and self._wallet_loaded
                and not _retried
                and self._is_wallet_not_loaded_error(e)
            ):
                logger.warning(
                    f"Wallet '{self.wallet_name}' not loaded in Bitcoin Core "
                    f"(detected during '{method}' call), attempting to reload..."
                )
                if await self._ensure_wallet_loaded():
                    return await self._rpc_call_inner(
                        method, params, client, use_wallet, _retried=True
                    )
            # Re-raise ValueError (RPC errors) as-is
            raise
        except httpx.HTTPError as e:
            logger.error(f"RPC call failed: {method} - {e}")
            raise

    async def create_wallet(self, disable_private_keys: bool = True) -> bool:
        """
        Create a descriptor wallet in Bitcoin Core.

        The wallet is encrypted with the passphrase (if provided) to protect
        the xpubs from unauthorized access. This is important because xpubs
        reveal transaction history, which would undo the privacy benefits
        of CoinJoin if exposed.

        Args:
            disable_private_keys: If True, creates a watch-only wallet (recommended)

        Returns:
            True if wallet was created or already exists
        """
        try:
            # First check if wallet already exists
            wallets = await self._rpc_call("listwallets", use_wallet=False)
            if self.wallet_name in wallets:
                logger.info(f"Wallet '{self.wallet_name}' already loaded")
                self._wallet_loaded = True
                return True

            # Try to load existing wallet
            try:
                await self._rpc_call("loadwallet", [self.wallet_name], use_wallet=False)
                logger.info(f"Loaded existing wallet '{self.wallet_name}'")
                self._wallet_loaded = True
                return True
            except ValueError as e:
                error_str = str(e).lower()
                # RPC error -18 is "Wallet not found" or "Path does not exist"
                not_found_errs = ("not found", "does not exist", "-18")
                if not any(err in error_str for err in not_found_errs):
                    raise

            # Create new descriptor wallet (watch-only, no private keys)
            # Params: wallet_name, disable_private_keys, blank, passphrase, avoid_reuse, descriptors
            result = await self._rpc_call(
                "createwallet",
                [
                    self.wallet_name,  # wallet_name
                    disable_private_keys,  # disable_private_keys
                    True,  # blank (no default keys)
                    "",  # passphrase (empty - not supported for watch-only wallets)
                    False,  # avoid_reuse
                    True,  # descriptors (MUST be True for descriptor wallet)
                ],
                use_wallet=False,
            )

            logger.info(f"Created descriptor wallet '{self.wallet_name}': {result}")
            self._wallet_loaded = True
            return True

        except Exception as e:
            logger.error(f"Failed to create/load wallet: {e}")
            raise

    async def _get_smart_scan_timestamp(
        self, lookback_blocks: int = DEFAULT_SCAN_LOOKBACK_BLOCKS
    ) -> int:
        """
        Calculate a smart scan timestamp based on current block height.

        Returns a Unix timestamp corresponding to approximately `lookback_blocks` ago.
        This allows scanning recent history quickly without waiting for a full
        genesis-to-tip rescan.

        If the node is pruned, ensures the timestamp is not earlier than the
        oldest available block (prune height).

        Args:
            lookback_blocks: Number of blocks to look back (default: ~1 year)

        Returns:
            Unix timestamp for the target block
        """
        try:
            # Get current block height and pruning info
            blockchain_info = await self._rpc_call("getblockchaininfo", use_wallet=False)
            current_height = blockchain_info.get("blocks", 0)

            # Calculate target height (don't go below 0)
            target_height = max(0, current_height - lookback_blocks)

            # If node is pruned, ensure target_height is not below pruneheight
            if blockchain_info.get("pruned", False):
                prune_height = blockchain_info.get("pruneheight", 0)
                if target_height < prune_height:
                    logger.info(
                        f"Pruned node detected: adjusting smart scan target height from "
                        f"{target_height} to prune height {prune_height}"
                    )
                    target_height = prune_height

            # Get block time at target height
            block_hash = await self.get_block_hash(target_height)
            block_header = await self._rpc_call("getblockheader", [block_hash], use_wallet=False)
            timestamp = block_header.get("time", 0)

            logger.debug(
                f"Smart scan: current height {current_height}, "
                f"target height {target_height}, timestamp {timestamp}"
            )
            return timestamp

        except Exception as e:
            logger.warning(f"Failed to calculate smart scan timestamp: {e}, falling back to 0")
            return 0

    async def import_descriptors(
        self,
        descriptors: Sequence[str | dict[str, Any]],
        rescan: bool = True,
        timestamp: str | int | None = None,
        smart_scan: bool = True,
        background_full_rescan: bool = True,
    ) -> dict[str, Any]:
        """
        Import descriptors into the wallet.

        This is the key operation that enables efficient UTXO tracking. Once imported,
        Bitcoin Core will automatically track all addresses derived from these descriptors.

        Smart Scan Behavior (smart_scan=True):
            Instead of scanning from genesis (which can take 20+ minutes on mainnet),
            the smart scan imports descriptors with a timestamp ~1 year in the past.
            This allows quick startup while still catching most wallet activity.

            If background_full_rescan=True, a full rescan from genesis is triggered
            in the background after the initial import completes. This runs asynchronously
            and ensures no transactions are missed.

        Args:
            descriptors: List of output descriptors. Can be:
                - Simple strings: "wpkh(xpub.../0/*)"
                - Dicts with range:
                  {"desc": "wpkh(xpub.../0/*)", "range": [0, DEFAULT_GAP_LIMIT - 1]}
            rescan: If True, rescan blockchain (behavior depends on smart_scan).
                   If False, only track new transactions (timestamp="now").
            timestamp: Override timestamp. If None, uses smart calculation or 0/"now".
                      Can be Unix timestamp for partial rescan from specific time.
            smart_scan: If True and rescan=True, scan from ~1 year ago instead of genesis.
                       This allows quick startup. (default: True)
            background_full_rescan: If True and smart_scan=True, trigger full rescan
                                   from genesis in background after import. (default: True)

        Returns:
            Import result from Bitcoin Core with additional 'background_rescan_started' key

        Example:
            # Smart scan (fast startup, background full rescan)
            await backend.import_descriptors([
                {
                    "desc": "wpkh(xpub.../0/*)",
                    "range": [0, DEFAULT_GAP_LIMIT - 1],
                    "internal": False,
                },
            ], rescan=True, smart_scan=True)

            # Full rescan from genesis (slow but complete)
            await backend.import_descriptors([...], rescan=True, smart_scan=False)

            # No rescan (for brand new wallets with no history)
            await backend.import_descriptors([...], rescan=False)
        """
        if not self._wallet_loaded:
            raise RuntimeError("Wallet not loaded. Call create_wallet() first.")

        # Calculate appropriate timestamp
        background_rescan_needed = False
        if timestamp is None:
            if not rescan:
                timestamp = "now"
            elif smart_scan:
                # Smart scan: start from ~1 year ago for fast startup
                timestamp = await self._get_smart_scan_timestamp()
                background_rescan_needed = background_full_rescan
            else:
                # Full rescan from genesis
                timestamp = 0

        # Format descriptors for importdescriptors RPC
        import_requests = []
        for desc in descriptors:
            if isinstance(desc, str):
                # Add checksum if not present
                desc_with_checksum = await self._add_descriptor_checksum(desc)
                # Single address descriptors (addr(...)) cannot be active - they're not ranged
                is_ranged = "*" in desc or "range" in desc if isinstance(desc, str) else False
                import_requests.append(
                    {
                        "desc": desc_with_checksum,
                        "timestamp": timestamp,
                        "active": is_ranged,  # Only ranged descriptors can be active
                        "internal": False,
                    }
                )
            elif isinstance(desc, dict):
                desc_str = desc.get("desc", "")
                desc_with_checksum = await self._add_descriptor_checksum(desc_str)
                # Determine if descriptor is ranged (has * wildcard or explicit range)
                is_ranged = "*" in desc_str or "range" in desc
                request = {
                    "desc": desc_with_checksum,
                    "timestamp": timestamp,
                    "active": is_ranged,  # Only ranged descriptors can be active
                }
                if "range" in desc:
                    request["range"] = desc["range"]
                if "internal" in desc:
                    request["internal"] = desc["internal"]
                import_requests.append(request)

        if SENSITIVE_LOGGING:
            logger.debug(f"Importing {len(import_requests)} descriptor(s): {import_requests}")
        else:
            if timestamp == 0:
                rescan_info = "from genesis (timestamp=0)"
            elif timestamp == "now":
                rescan_info = "no rescan (timestamp='now')"
            elif smart_scan and background_rescan_needed:
                rescan_info = (
                    f"smart scan from ~1 year ago (timestamp={timestamp}), "
                    "full rescan in background"
                )
            else:
                rescan_info = f"timestamp={timestamp}"
            logger.info(
                f"Importing {len(import_requests)} descriptor(s) into wallet ({rescan_info})..."
            )

        try:
            result = await self._rpc_call(
                "importdescriptors", [import_requests], client=self._import_client
            )

            # Check for errors in results
            success_count = sum(1 for r in result if r.get("success", False))
            error_count = len(result) - success_count

            if error_count > 0:
                errors = [
                    r.get("error", {}).get("message", "unknown")
                    for r in result
                    if not r.get("success", False)
                ]
                logger.warning(f"Import completed with {error_count} error(s): {errors}")
                # Log full results for debugging
                for i, r in enumerate(result):
                    if not r.get("success", False):
                        logger.debug(f"  Descriptor {i} failed: {r}")
            else:
                logger.info(f"Successfully imported {success_count} descriptor(s)")

            # Verify import by listing descriptors
            try:
                verify_result = await self._rpc_call("listdescriptors")
                actual_count = len(verify_result.get("descriptors", []))
                logger.debug(f"Verification: wallet now has {actual_count} descriptor(s)")
                if actual_count == 0 and success_count > 0:
                    logger.error(
                        f"CRITICAL: Import reported {success_count} successes but wallet has "
                        f"0 descriptors! This may indicate a Bitcoin Core bug or wallet issue."
                    )
            except Exception as e:
                logger.warning(f"Could not verify descriptor import: {e}")

            self._descriptors_imported = True

            # Trigger background full rescan if needed
            background_rescan_started = False
            if background_rescan_needed and success_count > 0:
                try:
                    await self.start_background_rescan()
                    background_rescan_started = True
                except Exception as e:
                    logger.warning(f"Failed to start background rescan: {e}")

            return {
                "success_count": success_count,
                "error_count": error_count,
                "results": result,
                "background_rescan_started": background_rescan_started,
            }

        except Exception as e:
            logger.error(f"Failed to import descriptors: {e}")
            raise

    async def _add_descriptor_checksum(self, descriptor: str) -> str:
        """Add checksum to descriptor if not present."""
        if "#" in descriptor:
            return descriptor  # Already has checksum

        try:
            result = await self._rpc_call("getdescriptorinfo", [descriptor], use_wallet=False)
            return result.get("descriptor", descriptor)
        except Exception as e:
            logger.warning(f"Failed to get descriptor checksum: {e}")
            return descriptor

    async def start_background_rescan(self, start_height: int = 0) -> None:
        """
        Start a background blockchain rescan from the given height.

        This triggers a rescan that runs asynchronously in Bitcoin Core.
        The rescan will find any transactions that were missed by the
        initial smart scan (which only scans recent blocks).

        Unlike the synchronous rescan in import_descriptors, this method
        returns immediately and the rescan continues in the background.

        Args:
            start_height: Block height to start rescan from (default: 0 = genesis)
        """
        if not self._wallet_loaded:
            raise RuntimeError("Wallet not loaded. Call create_wallet() first.")

        try:
            logger.info(
                f"Starting background blockchain rescan from height {start_height}. "
                "This will run in the background and may take several minutes on mainnet."
            )

            # rescanblockchain runs in the background when called via RPC
            # We use a fire-and-forget approach with a short timeout client
            # to avoid blocking on the full rescan
            import asyncio

            # Create a task that won't block the caller
            # We don't await it - let it run in background
            asyncio.create_task(self._run_background_rescan(start_height))

            self._background_rescan_height = start_height

        except Exception as e:
            logger.error(f"Failed to start background rescan: {e}")
            raise

    async def _run_background_rescan(self, start_height: int) -> None:
        """
        Internal method to run the background rescan.

        This is executed as a fire-and-forget task.
        """
        try:
            # Use a client with very long timeout for the background rescan
            # 2 hours should be enough for a full mainnet rescan
            background_client = httpx.AsyncClient(
                timeout=7200.0,  # 2 hours
                auth=(self.rpc_user, self.rpc_password),
            )
            try:
                result = await self._rpc_call(
                    "rescanblockchain",
                    [start_height],
                    client=background_client,
                )
                start_h = result.get("start_height", start_height)
                stop_h = result.get("stop_height", "?")
                logger.info(f"Background rescan completed: scanned blocks {start_h} to {stop_h}")
            finally:
                await background_client.aclose()

            self._background_rescan_height = None

        except asyncio.CancelledError:
            logger.info("Background rescan was cancelled")
            self._background_rescan_height = None
        except Exception as e:
            logger.error(f"Background rescan failed: {e}")
            self._background_rescan_height = None

    async def get_rescan_status(self) -> dict[str, Any] | None:
        """
        Check the status of any ongoing wallet rescan.

        Returns:
            Dict with rescan progress info, or None if no rescan in progress.
            Example: {"progress": 0.5, "current_height": 500000}
        """
        if not self._wallet_loaded:
            return None

        try:
            # getwalletinfo includes rescan progress if a rescan is in progress
            wallet_info = await self._rpc_call("getwalletinfo")

            if "scanning" in wallet_info and wallet_info["scanning"]:
                scanning_info = wallet_info["scanning"]
                return {
                    "in_progress": True,
                    "progress": scanning_info.get("progress", 0),
                    "duration": scanning_info.get("duration", 0),
                }

            return {"in_progress": False}

        except Exception as e:
            logger.debug(f"Could not get rescan status: {e}")
            return None

    def is_background_rescan_pending(self) -> bool:
        """Check if a background rescan was started and may still be running."""
        return self._background_rescan_height is not None

    async def wait_for_rescan_complete(
        self,
        poll_interval: float = 5.0,
        timeout: float | None = None,
        progress_callback: Callable[[float], None] | None = None,
        startup_grace_period: float = 30.0,
    ) -> bool:
        """
        Wait for any ongoing wallet rescan to complete.

        This is useful after importing descriptors with rescan=True to ensure
        the wallet is fully synced before querying UTXOs.

        There is a race condition between ``start_background_rescan()`` firing
        the ``rescanblockchain`` RPC and Bitcoin Core updating
        ``getwalletinfo.scanning``.  To avoid returning prematurely (before
        the rescan actually starts), we require at least one positive
        ``in_progress`` observation before we accept ``in_progress == False``
        as meaning the rescan finished.

        Args:
            poll_interval: How often to check rescan status (seconds)
            timeout: Maximum time to wait (seconds). None = wait indefinitely.
            progress_callback: Optional callback(progress) called with progress 0.0-1.0
            startup_grace_period: How long to wait for the rescan to start before
                assuming it completed very quickly or was never needed (seconds).

        Returns:
            True if rescan completed, False if timed out
        """
        import time

        start_time = time.time()
        saw_in_progress = False

        # Small initial delay to let Bitcoin Core start the rescan
        await asyncio.sleep(min(poll_interval, 2.0))

        while True:
            status = await self.get_rescan_status()

            in_progress = status is not None and status.get("in_progress", False)

            if in_progress:
                saw_in_progress = True
                progress = status.get("progress", 0)  # type: ignore[union-attr]
                if progress_callback:
                    progress_callback(progress)
                logger.debug(f"Rescan in progress: {progress:.1%}")
            elif saw_in_progress:
                # Rescan was running and has now finished
                return True
            else:
                # Haven't seen the rescan start yet.  Keep polling for a
                # reasonable grace period so we don't miss a slow start.
                elapsed = time.time() - start_time
                if elapsed > startup_grace_period:
                    # After the grace period without ever seeing a rescan we
                    # assume it either completed very quickly or was never
                    # started.
                    logger.debug(
                        "Rescan never observed as in-progress after "
                        f"{elapsed:.0f}s, assuming complete"
                    )
                    return True

            if timeout is not None and (time.time() - start_time) > timeout:
                logger.warning(f"Rescan wait timed out after {timeout}s")
                return False

            await asyncio.sleep(poll_interval)

    async def setup_wallet(
        self,
        descriptors: Sequence[str | dict[str, Any]],
        rescan: bool = True,
        smart_scan: bool = True,
        background_full_rescan: bool = True,
    ) -> bool:
        """
        Complete wallet setup: create wallet and import descriptors.

        This is a convenience method for initial setup. By default, uses smart scan
        for fast startup with a background full rescan.

        Args:
            descriptors: Descriptors to import
            rescan: Whether to rescan blockchain
            smart_scan: If True and rescan=True, scan from ~1 year ago (fast startup)
            background_full_rescan: If True and smart_scan=True, run full rescan in background

        Returns:
            True if setup completed successfully
        """
        await self.create_wallet(disable_private_keys=True)
        await self.import_descriptors(
            descriptors,
            rescan=rescan,
            smart_scan=smart_scan,
            background_full_rescan=background_full_rescan,
        )
        return True

    async def list_descriptors(self) -> list[dict[str, Any]]:
        """
        List all descriptors currently imported in the wallet.

        Returns:
            List of descriptor info dicts with fields like 'desc', 'timestamp', 'active', etc.

        Example:
            descriptors = await backend.list_descriptors()
            for d in descriptors:
                print(f"Descriptor: {d['desc']}, Active: {d.get('active', False)}")
        """
        if not self._wallet_loaded:
            raise RuntimeError("Wallet not loaded. Call create_wallet() first.")

        try:
            result = await self._rpc_call("listdescriptors")
            return result.get("descriptors", [])
        except Exception as e:
            logger.error(f"Failed to list descriptors: {e}")
            raise

    async def is_wallet_setup(self, expected_descriptor_count: int | None = None) -> bool:
        """
        Check if wallet is already set up with imported descriptors.

        Args:
            expected_descriptor_count: If provided, verifies this many descriptors are imported.
                                      For JoinMarket: 2 per mixdepth (external + internal)
                                      Example: 5 mixdepths = 10 descriptors minimum

        Returns:
            True if wallet exists and has descriptors imported

        Example:
            # Check if wallet is set up for 5 mixdepths
            if await backend.is_wallet_setup(expected_descriptor_count=10):
                # Already set up, just sync
                utxos = await wallet.sync_with_descriptor_wallet()
            else:
                # First time - import descriptors
                await wallet.setup_descriptor_wallet(rescan=True)
        """
        try:
            # Check if wallet exists and is loaded
            wallets = await self._rpc_call("listwallets", use_wallet=False)
            if self.wallet_name in wallets:
                self._wallet_loaded = True
            else:
                # Try to load it
                try:
                    await self._rpc_call("loadwallet", [self.wallet_name], use_wallet=False)
                    self._wallet_loaded = True
                except ValueError:
                    return False

            # Check if descriptors are imported
            descriptors = await self.list_descriptors()
            if not descriptors:
                return False

            # If expected count provided, verify
            if expected_descriptor_count is not None:
                return len(descriptors) >= expected_descriptor_count

            return True

        except Exception as e:
            logger.debug(f"Wallet setup check failed: {e}")
            return False

    async def get_utxos(self, addresses: list[str]) -> list[UTXO]:
        """
        Get UTXOs for given addresses using listunspent.

        This is MUCH faster than scantxoutset because:
        1. Only queries wallet's tracked UTXOs (not entire UTXO set)
        2. Includes unconfirmed transactions from mempool
        3. O(wallet size) instead of O(UTXO set size)

        Args:
            addresses: List of addresses to filter by (empty = all wallet UTXOs)

        Returns:
            List of UTXOs
        """
        if not self._wallet_loaded:
            logger.warning("Wallet not loaded, returning empty UTXO list")
            return []

        try:
            # Get current block height for calculating UTXO height
            tip_height = await self.get_block_height()

            # listunspent params: minconf, maxconf, addresses, include_unsafe, query_options
            # minconf=0 includes unconfirmed, maxconf=9999999 includes all confirmed
            # NOTE: When addresses is empty, we must omit it entirely (not pass [])
            # because Bitcoin Core interprets [] as "filter to 0 addresses" = return nothing
            if addresses:
                # Filter to specific addresses
                result = await self._rpc_call(
                    "listunspent",
                    [
                        0,  # minconf - include unconfirmed
                        9999999,  # maxconf
                        addresses,  # filter addresses
                        True,  # include_unsafe (include unconfirmed from mempool)
                    ],
                )
            else:
                # Get all wallet UTXOs - omit addresses parameter
                result = await self._rpc_call(
                    "listunspent",
                    [
                        0,  # minconf - include unconfirmed
                        9999999,  # maxconf
                    ],
                )

            utxos = []
            for utxo_data in result:
                confirmations = utxo_data.get("confirmations", 0)
                height = None
                if confirmations > 0:
                    height = tip_height - confirmations + 1

                utxo = UTXO(
                    txid=utxo_data["txid"],
                    vout=utxo_data["vout"],
                    value=btc_to_sats(utxo_data["amount"]),
                    address=utxo_data.get("address", ""),
                    confirmations=confirmations,
                    scriptpubkey=utxo_data.get("scriptPubKey", ""),
                    height=height,
                )
                utxos.append(utxo)

            logger.debug(f"Found {len(utxos)} UTXOs via listunspent")
            return utxos

        except Exception as e:
            logger.error(f"Failed to get UTXOs via listunspent: {e}")
            return []

    async def get_all_utxos(self) -> list[UTXO]:
        """
        Get all UTXOs tracked by the wallet.

        Returns:
            List of all wallet UTXOs
        """
        return await self.get_utxos([])

    async def scan_descriptors(self, _descriptors: list[Any]) -> dict[str, Any] | None:
        """
        Return all wallet UTXOs in the format expected by ``_sync_all_with_descriptors``.

        Rather than performing a slow ``scantxoutset`` (as the
        ``ScantxoutsetBackend`` does), we use Bitcoin Core's descriptor wallet
        ``listunspent`` RPC which:

        * Returns every UTXO tracked by *this* wallet instantly.
        * Already includes a ``desc`` field with the derivation path in the
          form ``wpkh([fingerprint/change/index]pubkey)#checksum``, which is
          exactly what ``_parse_descriptor_path`` in ``sync.py`` expects.
        * Has no per-mixdepth address-window limit — all historical addresses
          (regardless of index) are automatically tracked.

        The ``_descriptors`` argument (the xpub-based descriptor list built by
        ``sync.py``) is intentionally ignored; the wallet already knows which
        addresses to watch.
        """
        if not self._wallet_loaded:
            logger.warning("scan_descriptors: wallet not loaded")
            return None

        try:
            tip_height = await self.get_block_height()

            # listunspent without an address filter returns ALL wallet UTXOs.
            # By default, listunspent excludes locked UTXOs. We must query both
            # unlocked and locked UTXOs to get the complete state.

            # 1. Get unlocked UTXOs (default behavior)
            raw_utxos: list[dict[str, Any]] = await self._rpc_call(
                "listunspent",
                [0, 9_999_999],
            )

            # 2. Get locked UTXOs via listlockunspent
            # (since listunspent locked=True is not supported in all versions)
            try:
                locked_outpoints = await self._rpc_call("listlockunspent")
                if locked_outpoints:
                    logger.debug(f"Found {len(locked_outpoints)} locked UTXOs, fetching details...")
                    # Fetch details for each locked UTXO
                    for outpoint in locked_outpoints:
                        txid = outpoint["txid"]
                        vout = outpoint["vout"]

                        # Try to get transaction details from wallet or blockchain
                        # We use gettransaction to get the 'details' part including address/category
                        # or gettxout for raw info

                        # Try gettxout first as it's lighter
                        txout = await self._rpc_call(
                            "gettxout", [txid, vout, True], use_wallet=False
                        )
                        if txout:
                            # Reconstruct UTXO dict to match listunspent format
                            raw_utxos.append(
                                {
                                    "txid": txid,
                                    "vout": vout,
                                    "amount": txout["value"],
                                    "scriptPubKey": txout["scriptPubKey"]["hex"],
                                    "confirmations": txout["confirmations"],
                                    # We might miss 'desc' here if gettxout doesn't
                                    # provide it (it doesn't).
                                    # However, listunspent provides 'desc'.
                                    # If we need 'desc', we might need to use
                                    # getaddressinfo or gettransaction?
                                    # DescriptorWalletBackend relies on 'desc'
                                    # for _parse_descriptor_path?
                                    # Yes, sync.py needs 'desc'.
                                    # If gettxout doesn't give desc, we have a problem.
                                    # But wait, if it's in the wallet, gettransaction might help?
                                    "desc": "",  # Placeholder, might break sync if empty
                                }
                            )

                            # Correction: gettxout does NOT return descriptor.
                            # We need the descriptor for sync.py to identify the mixdepth/index.
                            # Only listunspent returns 'desc' reliably for descriptor wallets.
                            # If we can't get 'desc' for locked UTXOs, we can't
                            # track them correctly.

                            # Fallback: Can we unlock them temporarily? No, race condition.
                            # Can we deduce 'desc'? No.

                            # Actually, if we use getaddressinfo on the address?
                            # txout["scriptPubKey"]["address"] gives address.
                            # getaddressinfo(address) -> "desc"
                            if "address" in txout["scriptPubKey"]:
                                addr = txout["scriptPubKey"]["address"]
                                addr_info = await self._rpc_call("getaddressinfo", [addr])
                                if "desc" in addr_info:
                                    raw_utxos[-1]["desc"] = addr_info["desc"]
            except Exception as e:
                logger.warning(f"Failed to fetch locked UTXOs: {e}")

            unspents: list[dict[str, Any]] = []
            for u in raw_utxos:
                confirmations = u.get("confirmations", 0)
                height = (tip_height - confirmations + 1) if confirmations > 0 else 0
                unspents.append(
                    {
                        "txid": u["txid"],
                        "vout": u["vout"],
                        "amount": u["amount"],
                        "scriptPubKey": u.get("scriptPubKey", ""),
                        "height": height,
                        "desc": u.get("desc", ""),
                    }
                )

            logger.debug(f"scan_descriptors: returning {len(unspents)} UTXOs via listunspent")
            return {"success": True, "unspents": unspents}

        except Exception as e:
            logger.error(f"scan_descriptors failed: {e}")
            return None

    async def get_address_balance(self, address: str) -> int:
        """Get balance for an address in satoshis."""
        utxos = await self.get_utxos([address])
        return sum(utxo.value for utxo in utxos)

    async def get_wallet_balance(self) -> dict[str, int]:
        """
        Get total wallet balance including unconfirmed.

        Returns:
            Dict with 'confirmed', 'unconfirmed', 'total' balances in satoshis
        """
        try:
            result = await self._rpc_call("getbalances")
            mine = result.get("mine", {})
            confirmed = btc_to_sats(mine.get("trusted", 0))
            unconfirmed = btc_to_sats(mine.get("untrusted_pending", 0))
            return {
                "confirmed": confirmed,
                "unconfirmed": unconfirmed,
                "total": confirmed + unconfirmed,
            }
        except Exception as e:
            logger.error(f"Failed to get wallet balance: {e}")
            return {"confirmed": 0, "unconfirmed": 0, "total": 0}

    async def broadcast_transaction(self, tx_hex: str) -> str:
        """Broadcast transaction, returns txid."""
        try:
            txid = await self._rpc_call("sendrawtransaction", [tx_hex], use_wallet=False)
            logger.info(f"Broadcast transaction: {txid}")
            return txid
        except Exception as e:
            logger.error(f"Failed to broadcast transaction: {e}")
            raise ValueError(f"Broadcast failed: {e}") from e

    async def get_transaction(self, txid: str) -> Transaction | None:
        """Get transaction by txid."""
        try:
            # First try wallet transaction for extra info
            try:
                tx_data = await self._rpc_call("gettransaction", [txid, True])
                confirmations = tx_data.get("confirmations", 0)
                block_height = tx_data.get("blockheight")
                block_time = tx_data.get("blocktime")
                raw_hex = tx_data.get("hex", "")
            except ValueError:
                # Fall back to getrawtransaction if not in wallet
                tx_data = await self._rpc_call("getrawtransaction", [txid, True], use_wallet=False)
                if not tx_data:
                    return None
                confirmations = tx_data.get("confirmations", 0)
                block_height = None
                block_time = None
                if "blockhash" in tx_data:
                    block_info = await self._rpc_call(
                        "getblockheader", [tx_data["blockhash"]], use_wallet=False
                    )
                    block_height = block_info.get("height")
                    block_time = block_info.get("time")
                raw_hex = tx_data.get("hex", "")

            return Transaction(
                txid=txid,
                raw=raw_hex,
                confirmations=confirmations,
                block_height=block_height,
                block_time=block_time,
            )
        except Exception as e:
            logger.debug(f"Failed to get transaction {txid}: {e}")
            return None

    async def estimate_fee(self, target_blocks: int) -> float:
        """Estimate fee in sat/vbyte for target confirmation blocks."""
        try:
            result = await self._rpc_call("estimatesmartfee", [target_blocks], use_wallet=False)
            if "feerate" in result:
                btc_per_kb = result["feerate"]
                sat_per_vbyte = btc_to_sats(btc_per_kb) / 1000
                return sat_per_vbyte
            else:
                logger.warning("Fee estimation unavailable, using fallback")
                return 1.0
        except Exception as e:
            logger.warning(f"Failed to estimate fee: {e}, using fallback")
            return 1.0

    async def get_mempool_min_fee(self) -> float | None:
        """Get the minimum fee rate (in sat/vB) for transaction to be accepted into mempool."""
        try:
            result = await self._rpc_call("getmempoolinfo", use_wallet=False)
            if "mempoolminfee" in result:
                btc_per_kb = result["mempoolminfee"]
                sat_per_vbyte = btc_to_sats(btc_per_kb) / 1000
                logger.debug(f"Mempool min fee: {sat_per_vbyte} sat/vB")
                return sat_per_vbyte
            return None
        except Exception as e:
            logger.debug(f"Failed to get mempool min fee: {e}")
            return None

    async def get_block_height(self) -> int:
        """Get current blockchain height."""
        info = await self._rpc_call("getblockchaininfo", use_wallet=False)
        return info.get("blocks", 0)

    async def get_block_time(self, block_height: int) -> int:
        """Get block time (unix timestamp) for given height."""
        block_hash = await self.get_block_hash(block_height)
        block_header = await self._rpc_call("getblockheader", [block_hash], use_wallet=False)
        return block_header.get("time", 0)

    async def get_block_hash(self, block_height: int) -> str:
        """Get block hash for given height."""
        return await self._rpc_call("getblockhash", [block_height], use_wallet=False)

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        """
        Get a specific UTXO.

        First checks wallet's UTXOs, then falls back to gettxout for non-wallet UTXOs.
        """
        # First check wallet UTXOs (fast)
        try:
            utxos = await self._rpc_call(
                "listunspent",
                [0, 9999999, [], True, {"minimumAmount": 0}],
            )
            for utxo_data in utxos:
                if utxo_data["txid"] == txid and utxo_data["vout"] == vout:
                    return UTXO(
                        txid=utxo_data["txid"],
                        vout=utxo_data["vout"],
                        value=btc_to_sats(utxo_data["amount"]),
                        address=utxo_data.get("address", ""),
                        confirmations=utxo_data.get("confirmations", 0),
                        scriptpubkey=utxo_data.get("scriptPubKey", ""),
                        height=None,
                    )
        except Exception as e:
            logger.debug(f"Wallet UTXO lookup failed: {e}")

        # Fall back to gettxout for non-wallet UTXOs
        try:
            result = await self._rpc_call("gettxout", [txid, vout, True], use_wallet=False)
            if result is None:
                return None

            tip_height = await self.get_block_height()
            confirmations = result.get("confirmations", 0)
            height = tip_height - confirmations + 1 if confirmations > 0 else None

            script_pub_key = result.get("scriptPubKey", {})
            return UTXO(
                txid=txid,
                vout=vout,
                value=btc_to_sats(result.get("value", 0)),
                address=script_pub_key.get("address", ""),
                confirmations=confirmations,
                scriptpubkey=script_pub_key.get("hex", ""),
                height=height,
            )
        except Exception as e:
            logger.error(f"Failed to get UTXO {txid}:{vout}: {e}")
            return None

    async def rescan_blockchain(self, start_height: int = 0) -> dict[str, Any]:
        """
        Rescan blockchain from given height.

        Useful after importing new descriptors or recovering wallet.

        Args:
            start_height: Block height to start rescan from.  Values beyond the
                current chain tip are clamped to the tip so that callers using
                mainnet-derived constants (e.g. SegWit activation height 481824)
                work correctly on signet/testnet where the tip is much lower.

        Returns:
            Rescan result
        """
        try:
            chain_tip = await self.get_block_height()
            effective_height = min(max(0, start_height), chain_tip)
            if effective_height != start_height:
                logger.warning(
                    f"Requested rescan height {start_height} is out of range "
                    f"[0, {chain_tip}]; clamping to {effective_height}"
                )
            logger.info(f"Starting blockchain rescan from height {effective_height}...")
            result = await self._rpc_call(
                "rescanblockchain",
                [effective_height],
                client=self._import_client,  # Use longer timeout
            )
            logger.info(f"Rescan complete: {result}")
            return result
        except Exception as e:
            logger.error(f"Rescan failed: {e}")
            raise

    async def get_new_address(self, address_type: str = "bech32") -> str:
        """
        Get a new address from the wallet.

        Note: This only works if private keys are enabled in the wallet.
        For watch-only wallets, derive addresses from the descriptors instead.
        """
        try:
            return await self._rpc_call("getnewaddress", ["", address_type])
        except ValueError as e:
            if "private keys disabled" in str(e).lower():
                raise RuntimeError(
                    "Cannot generate new addresses in watch-only wallet. "
                    "Derive addresses from your descriptors instead."
                ) from e
            raise

    async def get_addresses_with_history(self) -> set[str]:
        """
        Get all addresses that have ever been involved in transactions.

        Uses listaddressgroupings as the primary source, which returns addresses
        that have been used as inputs or outputs in any transaction. This is more
        reliable than listsinceblock for descriptor wallets because it captures
        address usage even when transaction details aren't fully recorded.

        Falls back to listsinceblock as a secondary source to catch any addresses
        that might only appear in transaction history.

        This is critical for tracking address usage to prevent reuse - a key
        privacy concern for CoinJoin wallets.

        Returns:
            Set of addresses that have ever been used in transactions
        """
        addresses: set[str] = set()

        # Primary source: listaddressgroupings
        # This returns addresses grouped by common ownership (used together in txs)
        # It reliably shows addresses that have been used, even if the transaction
        # details aren't available in listsinceblock (e.g., after wallet import)
        try:
            groupings = await self._rpc_call("listaddressgroupings", [])
            for group in groupings:
                for entry in group:
                    # Each entry is [address, balance, label?]
                    if entry and len(entry) >= 1:
                        addresses.add(entry[0])
            logger.debug(f"Found {len(addresses)} addresses from listaddressgroupings")
        except Exception as e:
            logger.warning(f"Failed to get addresses from listaddressgroupings: {e}")

        # Secondary source: listsinceblock
        # This catches addresses that might only appear in transaction history
        # but weren't grouped (e.g., single-use receive addresses)
        try:
            # listsinceblock params: blockhash (empty = all), target_confirmations,
            #                        include_watchonly, include_removed
            result = await self._rpc_call("listsinceblock", ["", 1, True, False])

            for tx in result.get("transactions", []):
                # Only include "receive" and "generate" categories - these are addresses
                # where this wallet received funds (our own addresses).
                # "send" category includes counterparty addresses we sent TO.
                if "address" in tx and tx.get("category") in ("receive", "generate"):
                    addresses.add(tx["address"])
        except Exception as e:
            logger.warning(f"Failed to get addresses from listsinceblock: {e}")

        logger.debug(f"Total addresses with history: {len(addresses)}")
        return addresses

    async def get_descriptor_ranges(self) -> dict[str, tuple[int, int]]:
        """
        Get the current range for each imported descriptor.

        Returns:
            Dictionary mapping descriptor base (without checksum) to (start, end) range.
            For non-ranged descriptors (addr(...)), returns empty range.

        Example:
            ranges = await backend.get_descriptor_ranges()
            # {"wpkh(xpub.../0/*)": (0, 999), "wpkh(xpub.../1/*)": (0, 999)}
        """
        if not self._wallet_loaded:
            return {}

        try:
            result = await self._rpc_call("listdescriptors")
            ranges: dict[str, tuple[int, int]] = {}

            for desc_info in result.get("descriptors", []):
                desc = desc_info.get("desc", "")
                # Remove checksum for cleaner key
                desc_base = desc.split("#")[0] if "#" in desc else desc

                # Get range - may be [start, end] or just end for simple ranges
                range_info = desc_info.get("range")
                if range_info is not None:
                    if isinstance(range_info, list) and len(range_info) >= 2:
                        ranges[desc_base] = (range_info[0], range_info[1])
                    elif isinstance(range_info, int):
                        ranges[desc_base] = (0, range_info)

            return ranges
        except Exception as e:
            logger.warning(f"Failed to get descriptor ranges: {e}")
            return {}

    async def get_max_descriptor_range(self) -> int:
        """
        Get the maximum range end across all imported descriptors.

        Returns:
            Maximum end index, or DEFAULT_GAP_LIMIT if no descriptors found.
        """
        ranges = await self.get_descriptor_ranges()
        if not ranges:
            return DEFAULT_GAP_LIMIT

        max_end = 0
        for start, end in ranges.values():
            if end > max_end:
                max_end = end

        return max_end if max_end > 0 else DEFAULT_GAP_LIMIT

    async def upgrade_descriptor_ranges(
        self,
        descriptors: Sequence[str | dict[str, Any]],
        new_range_end: int,
        rescan: bool = False,
    ) -> dict[str, Any]:
        """
        Upgrade descriptor ranges to track more addresses.

        This re-imports existing descriptors with a larger range. Bitcoin Core
        will automatically track the new addresses without re-scanning the entire
        blockchain (unless rescan=True is specified).

        This is useful when a wallet has grown beyond the initially imported range.
        For example, if originally imported with range [0, 999] and now need to
        track addresses up to index 5000.

        Args:
            descriptors: List of descriptors to upgrade (same format as import_descriptors)
            new_range_end: New end index for the range (e.g., 5000 for [0, 5000])
            rescan: Whether to rescan blockchain for the new addresses.
                   Usually not needed if wallet was already tracking some range.

        Returns:
            Import result from Bitcoin Core

        Note:
            Re-importing with a larger range is safe - Bitcoin Core will extend
            the tracking without duplicating or losing existing data.
        """
        if not self._wallet_loaded:
            raise RuntimeError("Wallet not loaded. Call create_wallet() first.")

        # Update ranges in descriptor dicts
        updated_descriptors = []
        for desc in descriptors:
            if isinstance(desc, str):
                # String descriptor - add range
                updated_descriptors.append(
                    {
                        "desc": desc,
                        "range": [0, new_range_end],
                    }
                )
            elif isinstance(desc, dict):
                # Dict descriptor - update range
                updated = dict(desc)
                if "*" in updated.get("desc", ""):  # Only ranged descriptors
                    updated["range"] = [0, new_range_end]
                updated_descriptors.append(updated)

        logger.info(
            f"Upgrading {len(updated_descriptors)} descriptor(s) to range [0, {new_range_end}]"
        )

        # Re-import with new range
        # timestamp="now" means don't rescan unless explicitly requested
        return await self.import_descriptors(
            updated_descriptors,
            rescan=rescan,
            timestamp=0 if rescan else "now",
            smart_scan=False,  # Don't use smart scan for upgrades
            background_full_rescan=False,
        )

    async def unload_wallet(self) -> None:
        """Unload the wallet from Bitcoin Core."""
        if self._wallet_loaded:
            try:
                await self._rpc_call("unloadwallet", [self.wallet_name], use_wallet=False)
                logger.info(f"Unloaded wallet '{self.wallet_name}'")
                self._wallet_loaded = False
            except Exception as e:
                logger.warning(f"Failed to unload wallet: {e}")

    def can_provide_neutrino_metadata(self) -> bool:
        """Bitcoin Core can provide Neutrino-compatible metadata."""
        return True

    async def close(self) -> None:
        """Close backend connections and reset clients so the backend can be reused."""
        await self.client.aclose()
        await self._import_client.aclose()
        # Re-create fresh clients so this instance is usable again if the
        # wallet service is restarted (e.g. maker stop → start in jmwalletd).
        self.client = httpx.AsyncClient(
            timeout=DEFAULT_RPC_TIMEOUT, auth=(self.rpc_user, self.rpc_password)
        )
        self._import_client = httpx.AsyncClient(
            timeout=self.import_timeout, auth=(self.rpc_user, self.rpc_password)
        )
        self._wallet_loaded = False
        self._descriptors_imported = False


def generate_wallet_name(
    mnemonic_fingerprint: str, network: str = "mainnet", address_type: str = "p2wpkh"
) -> str:
    """
    Generate a deterministic wallet name from mnemonic fingerprint.

    This ensures the same mnemonic always uses the same wallet, avoiding
    duplicate wallet creation.

    Args:
        mnemonic_fingerprint: First 8 chars of SHA256(mnemonic)
        network: Network name (mainnet, testnet, regtest)
        address_type: Address type (p2wpkh or p2tr)

    Returns:
        Wallet name like "jm_abc12345_mainnet" (or "jm_abc12345_mainnet_tr")
    """
    name = f"jm_{mnemonic_fingerprint}_{network}"
    if address_type == "p2tr":
        name += "_tr"
    return name


def get_mnemonic_fingerprint(mnemonic: str, passphrase: str = "") -> str:
    """
    Get BIP32 master key fingerprint from mnemonic (like SeedSigner).

    This creates the master HD key from the seed and derives m/0 to get
    the fingerprint, following the same approach as SeedSigner and other
    Bitcoin wallet software.

    Args:
        mnemonic: BIP39 mnemonic phrase
        passphrase: Optional BIP39 passphrase (13th/25th word)

    Returns:
        8-character hex string (4 bytes) of the m/0 fingerprint
    """
    from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed

    # Convert mnemonic to seed bytes
    seed = mnemonic_to_seed(mnemonic, passphrase)

    # Create master HD key from seed
    root = HDKey.from_seed(seed)

    # Derive m/0 child key (following SeedSigner approach)
    child = root.derive("m/0")

    # Get fingerprint (4 bytes)
    fingerprint_bytes = child.fingerprint

    # Convert to 8-character hex string
    return fingerprint_bytes.hex()
