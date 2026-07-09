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
import contextlib
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

import httpx
from jmcore.bitcoin import btc_to_sats
from loguru import logger

from jmwallet.backends.base import UTXO, BlockchainBackend, Transaction

# Timeout policy for regular RPC calls.
#
# A short connect timeout still detects a dead/unreachable node quickly,
# but the read timeout is generous: if Bitcoin Core has accepted the request
# and is processing it, it is by definition working. Many wallet RPCs
# (``listdescriptors``, ``listreceivedbyaddress``, ``listunspent``,
# ``rescanblockchain``) block on internal locks or scale with wallet/chain size
# and routinely take longer than 30s under load (busy node, slow disk,
# Raspberry-Pi-class hosts). The previous 30s default produced spurious
# ``ReadTimeout`` errors that triggered fall-back paths with stale data
# (e.g. an empty descriptor-range map that caused subsequent
# ``importdescriptors`` to be rejected with ``new range must include
# current range``).
#
# A 10-minute read budget is generous enough to absorb realistic worst
# cases without making a truly stuck process appear hung indefinitely;
# users can still Ctrl-C.
DEFAULT_RPC_CONNECT_TIMEOUT = 10.0
DEFAULT_RPC_READ_TIMEOUT = 600.0
DEFAULT_RPC_TIMEOUT = httpx.Timeout(
    connect=DEFAULT_RPC_CONNECT_TIMEOUT,
    read=DEFAULT_RPC_READ_TIMEOUT,
    write=DEFAULT_RPC_READ_TIMEOUT,
    pool=DEFAULT_RPC_CONNECT_TIMEOUT,
)

# Timeout for descriptor import - first-time imports trigger a partial rescan
# that runs synchronously inside the importdescriptors RPC. On slow hosts
# (e.g. a Raspberry Pi) the smart-scan window (~1 year) can easily exceed
# ten minutes, so we use a much larger budget here. If the HTTP read still
# times out we fall back to polling getwalletinfo for ``scanning`` instead
# of bubbling up a confusing ReadTimeout (issue #472).
IMPORT_RPC_TIMEOUT = 1800.0

# How often the concurrent progress monitor polls ``getwalletinfo`` while a
# blocking ``importdescriptors`` rescan runs. The first import used to appear
# frozen for 15+ minutes with no feedback at all (issue #472).
IMPORT_SCAN_PROGRESS_INTERVAL = 10.0

# Import-time rescans whose descriptor timestamp is older than this are
# announced to the user up front, since Bitcoin Core will scan that whole
# window synchronously inside the importdescriptors RPC.
LONG_IMPORT_SCAN_WARNING_AGE = 86_400.0  # 1 day

# Maximum time to wait for a transient "wallet already loading" state to clear.
# Bitcoin Core keeps a previously issued ``loadwallet``/``createwallet`` running
# server-side even after the HTTP request that triggered it timed out; while it
# runs, any new ``loadwallet`` is rejected with ``RPC error -4: Wallet already
# loading.`` (issue #465). Polling ``listwallets`` until the wallet appears
# resolves the condition. ~60s matches the cumulative create_wallet backoff
# schedule and is generous enough for typical load-on-restart waits without
# making a genuinely stuck node appear hung forever (users can Ctrl-C).
WALLET_LOADING_MAX_WAIT = 60.0

# Default gap limit for descriptor ranges
DEFAULT_GAP_LIMIT = 1000

# Default scan lookback period (approximately 1 year of blocks)
# Bitcoin averages ~144 blocks/day * 365 days ≈ 52,560 blocks
DEFAULT_SCAN_LOOKBACK_BLOCKS = 52_560

# Bitcoin Core's ``importdescriptors``/``ParseDescriptorRange`` rejects any
# descriptor range whose span exceeds 1,000,000 indices with the error
# "Range is too large" (src/rpc/util.cpp: ``if (high >= low + 1000000)``).
# A range expressed as ``[0, N]`` therefore allows at most 1,000,000 indices
# (0..999_999), so the largest usable ``scan_range`` per branch is 1,000,000.
# Requests beyond this are clamped down to the limit before they reach Core,
# otherwise the whole import fails and the wallet is left without coverage.
# This is the canonical definition; ``jmwallet.wallet.constants`` re-exports it
# (defined here so the backend module has no dependency on the wallet package).
MAX_DESCRIPTOR_RANGE = 1_000_000

# Environment variable to enable sensitive logging (descriptors, addresses, etc.)
SENSITIVE_LOGGING = os.environ.get("SENSITIVE_LOGGING", "").lower() in ("1", "true", "yes")


def clamp_descriptor_range(low: int, high: int) -> tuple[int, int]:
    """Clamp a descriptor ``[low, high]`` range to Bitcoin Core's limit.

    Bitcoin Core's ``importdescriptors`` rejects any range whose span exceeds
    ``MAX_DESCRIPTOR_RANGE`` indices with the error "Range is too large"
    (``ParseDescriptorRange``: ``high >= low + 1000000``). When the whole
    import is rejected the wallet ends up without any descriptor coverage, so
    we clamp the high bound down to the largest value Core accepts instead of
    letting the request fail.

    Returns the (possibly clamped) ``(low, high)`` tuple. Callers should warn
    the user when the result differs from the request.
    """
    max_high = low + MAX_DESCRIPTOR_RANGE - 1
    if high > max_high:
        return low, max_high
    return low, high


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

        # Wallet creation height hint (set via set_wallet_creation_height).
        self._wallet_creation_height: int | None = None

        # Cache for the oldest-wallet-tx blocktime ("wallet birthtime"). We
        # compute this from listsinceblock on demand and cache it because a
        # new transaction can only make the result older (or stay equal),
        # and computing it on every status call would re-paginate the whole
        # wallet history. ``None`` means "not computed yet"; ``0`` means
        # "computed and the wallet has no transactions".
        self._oldest_tx_blocktime: int | None = None

    def set_wallet_creation_height(self, height: int | None) -> None:
        """Use wallet creation height to narrow smart scan range.

        When the wallet was created at a known block height, the smart
        scan timestamp can start from that block instead of the generic
        lookback window, avoiding unnecessary scanning of older blocks.

        Passing ``None`` clears any previously set creation height hint.
        """
        if height is None:
            self._wallet_creation_height = None
            logger.debug("Cleared wallet creation height hint")
            return

        if not isinstance(height, int) or isinstance(height, bool):
            logger.warning(f"Ignoring non-integer creation_height={height!r}")
            return

        if height < 0:
            logger.warning(f"Ignoring invalid negative creation_height={height}")
            return

        self._wallet_creation_height = height
        logger.info(f"Wallet creation height set to {height} (will use for smart scan)")

    def _get_wallet_url(self) -> str:
        """Get the RPC URL for wallet-specific calls."""
        return f"{self.rpc_url}/wallet/{self.wallet_name}"

    @staticmethod
    def _is_wallet_not_loaded_error(error: ValueError) -> bool:
        """Check if an RPC error indicates the wallet is not loaded (error -18)."""
        error_str = str(error)
        return "RPC error -18" in error_str

    @staticmethod
    def _is_wallet_loading_error(error: ValueError | Exception) -> bool:
        """Check if an RPC error indicates a transient "wallet already loading" state.

        Bitcoin Core returns ``RPC error -4: Wallet already loading.`` while a
        prior ``loadwallet``/``createwallet`` call is still in-flight (for
        example after a previous wallet-info scan timed out mid-load). The
        condition is transient: polling ``listwallets`` and retrying after a
        short delay resolves it. See issue #465.
        """
        error_str = str(error).lower()
        return "already loading" in error_str or "wallet is already being loaded" in error_str

    @staticmethod
    def _is_wallet_disabled_error(error: ValueError | Exception) -> bool:
        """Detect ``RPC error -32601: Method not found`` on a wallet RPC.

        Bitcoin Core only registers the wallet RPC namespace (``listwallets``,
        ``loadwallet``, ``createwallet``, ``getaddressinfo``, ...) when wallet
        support is enabled. If the node is started with ``-disablewallet=1`` or
        was built without wallet support, every wallet RPC responds with
        ``-32601 Method not found``. ``bitcoin-cli listwallets`` exhibits the
        same symptom from outside JoinMarket.

        JoinMarket-NG's descriptor wallet backend cannot operate against such
        a node, so we detect this case to surface a clear, actionable error
        instead of a cryptic generic JSON-RPC failure.
        """
        error_str = str(error).lower()
        return "-32601" in error_str or "method not found" in error_str

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
            try:
                await self._rpc_call("loadwallet", [self.wallet_name], use_wallet=False)
            except ValueError as e:
                # A prior load is still running server-side (issue #465);
                # wait for it to finish rather than reporting failure.
                if self._is_wallet_loading_error(e) and await self._poll_until_wallet_loaded(
                    WALLET_LOADING_MAX_WAIT
                ):
                    return True
                raise
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
            # Logged at debug because some callers (notably the rescan
            # kick) deliberately use a short timeout and treat
            # TimeoutException as the success path: Bitcoin Core keeps
            # running the RPC server-side even if the client disconnects.
            # Unexpected timeouts still surface via the re-raised
            # exception, so callers can log with their own context.
            logger.debug(
                f"RPC call '{method}' timed out (treated as expected by caller "
                f"if a short deadline was set): {e!r}"
            )
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

    async def _rpc_batch_call(
        self,
        calls: Sequence[tuple[str, list[Any]]],
        client: httpx.AsyncClient | None = None,
        use_wallet: bool = True,
        chunk_size: int = 500,
    ) -> list[Any]:
        """
        Send a JSON-RPC batch to Bitcoin Core and return one result per call.

        A JSON-RPC batch lets the client send N method calls in a single HTTP
        POST body and receive N responses (possibly reordered) in a single
        response body. For methods that are individually cheap inside Bitcoin
        Core but dominated by HTTP round-trip cost (notably ``getaddressinfo``
        when scanning thousands of addresses), this is dramatically faster
        than a sequential loop, especially against a remote node.

        Per-call errors are surfaced as ``Exception`` objects in the result
        list (same index as the input call) rather than raising, so that one
        bad address does not poison results for the rest of the batch. The
        caller decides how to handle each failure. Transport-level errors
        (connection refused, timeout, malformed JSON, wallet-not-loaded) still
        raise, since they affect the entire batch.

        Args:
            calls: Sequence of ``(method, params)`` tuples to send as one batch.
            client: Optional httpx client (uses default client if not provided).
            use_wallet: If True, target the wallet-scoped URL (required for
                most wallet RPCs like ``getaddressinfo``).
            chunk_size: Maximum number of calls per HTTP POST. Larger values
                cut HTTP overhead further but can blow past httpx's response
                size limits and Bitcoin Core's request body limits on huge
                wallets. 500 has been benchmarked as a safe sweet spot.

        Returns:
            List of length ``len(calls)``; each entry is either the RPC
            ``result`` value or an ``Exception`` describing the per-call error.

        Raises:
            httpx.HTTPError: On transport-level errors.
            ValueError: On malformed batch responses or wallet-not-loaded
                errors that survive a single reload attempt.
        """
        if not calls:
            return []

        use_client = client or self.client
        url = self._get_wallet_url() if use_wallet and self._wallet_loaded else self.rpc_url

        missing = object()
        results: list[Any] = [missing] * len(calls)

        async def send_chunk(start: int, end: int) -> None:
            sub = calls[start:end]
            # Use the chunk offset as the JSON-RPC id so we can map responses
            # back to the original call index even if Core reorders them.
            payload = [
                {
                    "jsonrpc": "2.0",
                    "id": start + i,
                    "method": method,
                    "params": params or [],
                }
                for i, (method, params) in enumerate(sub)
            ]
            response = await use_client.post(url, json=payload)
            try:
                data = response.json()
            except Exception:
                response.raise_for_status()
                raise
            response.raise_for_status()
            if not isinstance(data, list):
                # Core only returns a non-list body if the whole batch failed
                # at the transport layer (e.g. wallet-not-loaded on the URL).
                err = data.get("error") if isinstance(data, dict) else None
                raise ValueError(f"batch RPC returned non-list response: {err or data}")
            for entry in data:
                idx = entry.get("id")
                if not isinstance(idx, int) or idx < 0 or idx >= len(calls):
                    # Out-of-range id: ignore rather than crash; we'll surface
                    # any unfilled slots as errors at the end.
                    continue
                if entry.get("error"):
                    err_info = entry["error"]
                    code = err_info.get("code", "unknown") if isinstance(err_info, dict) else "?"
                    msg = (
                        err_info.get("message", str(err_info))
                        if isinstance(err_info, dict)
                        else str(err_info)
                    )
                    results[idx] = ValueError(f"RPC error {code}: {msg}")
                else:
                    results[idx] = entry.get("result")

        for start in range(0, len(calls), chunk_size):
            await send_chunk(start, min(start + chunk_size, len(calls)))

        # Surface any slots the server omitted as explicit errors so callers
        # don't silently treat them as ``None``-valued successes.
        for i in range(len(results)):
            if results[i] is missing:
                method = calls[i][0]
                results[i] = ValueError(f"RPC batch dropped response for call {i} ({method})")

        return results

    async def _poll_until_wallet_loaded(self, max_total_wait: float) -> bool:
        """Poll ``listwallets`` until our wallet appears, up to ``max_total_wait`` seconds.

        Bitcoin Core returns ``RPC error -4: Wallet already loading`` while a
        prior ``loadwallet``/``createwallet`` call is still running server-side
        (commonly after a previous call timed out at the HTTP layer). The state
        is transient: once the load finishes the wallet shows up in
        ``listwallets``. Returns True as soon as the wallet is observed loaded
        (setting ``_wallet_loaded``), or False if the budget is exhausted first.
        See issue #465.
        """
        waited = 0.0
        delay = 1.0
        while waited < max_total_wait:
            await asyncio.sleep(delay)
            waited += delay
            try:
                wallets = await self._rpc_call("listwallets", use_wallet=False)
                if self.wallet_name in wallets:
                    logger.info(
                        f"Wallet '{self.wallet_name}' finished loading after "
                        f"~{waited:.0f}s of waiting"
                    )
                    self._wallet_loaded = True
                    return True
            except (ValueError, httpx.HTTPError) as poll_err:
                # Keep polling; listwallets may also transiently error.
                logger.debug(f"listwallets poll failed (will retry): {poll_err}")
            delay = min(delay * 2, 8.0)
        return False

    async def create_wallet(self, disable_private_keys: bool = True) -> bool:
        """
        Create a descriptor wallet in Bitcoin Core.

        The wallet is encrypted with the passphrase (if provided) to protect
        the xpubs from unauthorized access. This is important because xpubs
        reveal transaction history, which would undo the privacy benefits
        of CoinJoin if exposed.

        Handles the transient ``RPC error -4: Wallet already loading`` state
        (issue #465) by polling ``listwallets`` with exponential backoff;
        this typically happens when a previous ``loadwallet`` call timed out
        at the HTTP layer but is still running inside Bitcoin Core.

        Args:
            disable_private_keys: If True, creates a watch-only wallet (recommended)

        Returns:
            True if wallet was created or already exists
        """
        # Retry schedule for transient "already loading" errors. Bitcoin Core
        # load times scale with rescan depth; back off up to ~60s total.
        loading_backoff_s: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0, 30.0)

        try:
            # First check if wallet already exists
            try:
                wallets = await self._rpc_call("listwallets", use_wallet=False)
            except ValueError as e:
                if self._is_wallet_disabled_error(e):
                    raise ValueError(
                        "Bitcoin Core rejected 'listwallets' with "
                        "'-32601 Method not found'. The node has wallet support "
                        "disabled (started with '-disablewallet=1' or built "
                        "without wallet support). JoinMarket-NG needs a Bitcoin "
                        "Core build with wallet support enabled and the wallet "
                        "subsystem active. Remove '-disablewallet' (or "
                        "'disablewallet=1' from bitcoin.conf), restart "
                        "bitcoind, and verify with 'bitcoin-cli listwallets'."
                    ) from e
                raise
            if self.wallet_name in wallets:
                logger.info(f"Wallet '{self.wallet_name}' already loaded")
                self._wallet_loaded = True
                return True

            # Try to load existing wallet, retrying on transient "already loading"
            for attempt, delay in enumerate(loading_backoff_s, start=1):
                try:
                    await self._rpc_call("loadwallet", [self.wallet_name], use_wallet=False)
                    logger.info(f"Loaded existing wallet '{self.wallet_name}'")
                    self._wallet_loaded = True
                    return True
                except ValueError as e:
                    if self._is_wallet_loading_error(e):
                        logger.warning(
                            f"Bitcoin Core reports wallet already loading "
                            f"(attempt {attempt}/{len(loading_backoff_s)}); "
                            f"waiting {delay:.0f}s and polling listwallets..."
                        )
                        if await self._poll_until_wallet_loaded(delay):
                            return True
                        continue
                    error_str = str(e).lower()
                    # RPC error -18 is "Wallet not found" or "Path does not exist"
                    not_found_errs = ("not found", "does not exist", "-18")
                    if not any(err in error_str for err in not_found_errs):
                        raise
                    break  # wallet not found -> fall through to createwallet
            else:
                # Exhausted retries and the wallet still reports "already loading".
                raise ValueError(
                    f"Wallet '{self.wallet_name}' is still loading in Bitcoin Core "
                    "after extended retries; please try again in a moment."
                )

            # Create new descriptor wallet (watch-only, no private keys)
            # Params: wallet_name, disable_private_keys, blank, passphrase, avoid_reuse, descriptors
            for attempt, delay in enumerate(loading_backoff_s, start=1):
                try:
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
                except ValueError as e:
                    if self._is_wallet_loading_error(e):
                        logger.warning(
                            f"createwallet hit 'already loading' "
                            f"(attempt {attempt}/{len(loading_backoff_s)}); "
                            f"waiting up to {delay:.0f}s for prior load to finish..."
                        )
                        if await self._poll_until_wallet_loaded(delay):
                            return True
                        continue
                    raise
            raise ValueError(
                f"Wallet '{self.wallet_name}' is still loading in Bitcoin Core "
                "after extended retries; please try again in a moment."
            )

        except Exception as e:
            logger.error(f"Failed to create/load wallet: {e}")
            raise

    async def _get_smart_scan_timestamp(
        self, lookback_blocks: int = DEFAULT_SCAN_LOOKBACK_BLOCKS
    ) -> int:
        """
        Calculate a smart scan timestamp based on current block height.

        If a wallet creation height is set (via ``set_wallet_creation_height``),
        uses that block's timestamp instead of the generic lookback window,
        since the wallet cannot have received funds before it was created.

        Otherwise returns a Unix timestamp corresponding to approximately
        ``lookback_blocks`` ago. This allows scanning recent history quickly
        without waiting for a full genesis-to-tip rescan.

        Args:
            lookback_blocks: Number of blocks to look back (default: ~1 year)

        Returns:
            Unix timestamp for the target block
        """
        try:
            current_height = await self.get_block_height()

            if self._wallet_creation_height is not None:
                target_height = max(0, self._wallet_creation_height)
                logger.info(
                    f"Smart scan using wallet creation height: {target_height} "
                    f"(current={current_height})"
                )
            else:
                target_height = max(0, current_height - lookback_blocks)

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
        used_creation_height = False
        if timestamp is None:
            if not rescan:
                timestamp = "now"
            elif smart_scan:
                # Smart scan: start from the wallet's creation height when
                # known, otherwise from ~1 year ago for fast startup
                used_creation_height = self._wallet_creation_height is not None
                timestamp = await self._get_smart_scan_timestamp()
                background_rescan_needed = background_full_rescan
            else:
                # Full rescan from genesis
                timestamp = 0

        # Look up existing per-descriptor ranges so that re-imports never
        # shrink a descriptor's tracked range. Bitcoin Core's
        # ``importdescriptors`` rejects requests whose ``range`` does not
        # include the descriptor's current range with an error like
        # ``new range must include current range = [0,2802]`` (issue: deep
        # wallets retried with a smaller default scan range after a previous
        # partial-failure left some descriptors with divergent ranges).
        existing_ranges: dict[str, tuple[int, int]] = {}
        if any(
            isinstance(d, dict) and "range" in d or (isinstance(d, str) and "*" in d)
            for d in descriptors
        ):
            # Use the long-timeout import client: on deep wallets
            # ``listdescriptors`` can exceed the 30s default timeout, and
            # silently falling back to ``{}`` here would lead Bitcoin Core to
            # reject the import with "new range must include current range".
            # If even the long-timeout call fails we surface a clear error
            # rather than emitting a request we know Core will reject.
            try:
                existing_ranges = await self.get_descriptor_ranges(raise_on_error=True)
            except Exception as e:
                raise RuntimeError(
                    "Failed to fetch existing descriptor ranges before import; "
                    "cannot safely build a non-shrinking import range. Original "
                    f"error: {e}"
                ) from e

        def _expanded_range(
            desc_with_checksum: str, requested: list[int] | tuple[int, int]
        ) -> list[int]:
            """Return a range that includes both the requested and any existing range."""
            req_start, req_end = int(requested[0]), int(requested[1])
            desc_base = desc_with_checksum.split("#", 1)[0]
            current = existing_ranges.get(desc_base)
            if current is None:
                # Fallback: try matching with checksum included
                current = existing_ranges.get(desc_with_checksum)
            if current is None:
                return [req_start, req_end]
            cur_start, cur_end = current
            new_start = min(req_start, cur_start)
            new_end = max(req_end, cur_end)
            if new_end != req_end or new_start != req_start:
                logger.info(
                    f"Expanding import range for '{desc_base}' from "
                    f"[{req_start}, {req_end}] to [{new_start}, {new_end}] to "
                    f"include current range [{cur_start}, {cur_end}]"
                )
            return [new_start, new_end]

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
                request: dict[str, Any] = {
                    "desc": desc_with_checksum,
                    "timestamp": timestamp,
                    "active": is_ranged,  # Only ranged descriptors can be active
                }
                if "range" in desc:
                    expanded = _expanded_range(desc_with_checksum, desc["range"])
                    clamped_low, clamped_high = clamp_descriptor_range(expanded[0], expanded[1])
                    if clamped_high != expanded[1]:
                        logger.warning(
                            "Descriptor range [%d, %d] exceeds Bitcoin Core's "
                            "limit of %d indices per descriptor; clamping to "
                            "[%d, %d]. Bitcoin Core would otherwise reject the "
                            "import with 'Range is too large'. Indices beyond "
                            "%d cannot be tracked in a single descriptor. See "
                            "docs/technical/wallet-scanning.md.",
                            expanded[0],
                            expanded[1],
                            MAX_DESCRIPTOR_RANGE,
                            clamped_low,
                            clamped_high,
                            clamped_high,
                        )
                    request["range"] = [clamped_low, clamped_high]
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
                scan_origin = "wallet creation height" if used_creation_height else "~1 year ago"
                rescan_info = (
                    f"smart scan from {scan_origin} (timestamp={timestamp}), "
                    "full rescan in background"
                )
            else:
                rescan_info = f"timestamp={timestamp}"
            logger.info(
                f"Importing {len(import_requests)} descriptor(s) into wallet ({rescan_info})..."
            )

        # Bitcoin Core runs the rescan implied by ``timestamp`` synchronously
        # inside the importdescriptors RPC: the HTTP call blocks until the
        # scan is done. Announce long scans up front and report progress from
        # a concurrent task so first-time setup does not look frozen
        # (issue #472).
        progress_task: asyncio.Task[None] | None = None
        if timestamp != "now":
            if timestamp == 0 or (
                isinstance(timestamp, int)
                and time.time() - timestamp > LONG_IMPORT_SCAN_WARNING_AGE
            ):
                logger.info(
                    "Bitcoin Core is now scanning the blockchain for this "
                    "wallet's history as part of the descriptor import. This "
                    "happens once per wallet and can take a long time (15+ "
                    "minutes on slow hardware); progress is reported below. "
                    "It is safe to interrupt (Ctrl+C): the scan keeps running "
                    "inside Bitcoin Core and the next command picks up the "
                    "result."
                )
            progress_task = asyncio.create_task(self._log_import_scan_progress())

        try:
            try:
                result = await self._rpc_call(
                    "importdescriptors", [import_requests], client=self._import_client
                )
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as timeout_err:
                # The HTTP read timed out, but Bitcoin Core's importdescriptors
                # call is still running server-side -- the rescan that follows
                # the import is what actually blocks. Wait for the scan to
                # finish, then verify the import went through (issue #472).
                logger.warning(
                    "importdescriptors HTTP read timed out after "
                    f"{self.import_timeout:.0f}s; the import is still running "
                    "in Bitcoin Core. Waiting for the rescan to complete..."
                )
                rescan_done = await self.wait_for_rescan_complete(
                    poll_interval=10.0,
                    timeout=None,  # No additional cap -- let the user Ctrl-C
                )
                if not rescan_done:
                    raise RuntimeError(
                        "importdescriptors HTTP call timed out and the rescan "
                        "is still in progress in Bitcoin Core. Please retry "
                        "the command in a moment."
                    ) from timeout_err
                # Best-effort verification: listdescriptors confirms the import
                # actually applied. We synthesize a result envelope so the rest
                # of this function can keep running.
                logger.info(
                    "Rescan finished after HTTP timeout; verifying that "
                    "descriptors were imported..."
                )
                try:
                    verify = await self._rpc_call("listdescriptors")
                    actual_count = len(verify.get("descriptors", []))
                except Exception as verify_err:
                    raise RuntimeError(
                        "importdescriptors timed out and the post-timeout "
                        "verification call also failed; please retry."
                    ) from verify_err
                if actual_count == 0:
                    raise RuntimeError(
                        "importdescriptors timed out and the wallet still has "
                        "no descriptors. Please retry the command."
                    ) from timeout_err
                # Synthesize an all-success result so the existing code path
                # below treats this as a normal completion.
                result = [{"success": True} for _ in import_requests]

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

            self._descriptors_imported = error_count == 0 and success_count > 0
            if not self._descriptors_imported:
                logger.warning(
                    "Descriptor import had failures; backend remains in not-fully-imported state"
                )

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
        finally:
            if progress_task is not None:
                progress_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await progress_task

    async def _log_import_scan_progress(
        self, poll_interval: float = IMPORT_SCAN_PROGRESS_INTERVAL
    ) -> None:
        """Log wallet scan progress at INFO level while a blocking import runs.

        ``importdescriptors`` holds the HTTP call open while Bitcoin Core
        rescans the blockchain for the imported descriptors. This helper is
        run as a concurrent task during that call so users see progress
        instead of a silent multi-minute hang (issue #472). It never finishes
        on its own; the caller cancels it once the import returns.
        """
        while True:
            await asyncio.sleep(poll_interval)
            status = await self.get_rescan_status()
            if status and status.get("in_progress"):
                progress = float(status.get("progress") or 0.0)
                duration = int(status.get("duration") or 0)
                logger.info(
                    f"Wallet history scan in progress: {progress * 100:.1f}% (elapsed {duration}s)"
                )

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

    async def _effective_rescan_height(self, start_height: int) -> int:
        """
        Normalize a requested rescan start height into one Bitcoin Core accepts.

        Applied rules, in order:

        1. Negative heights are clamped to 0.
        2. When a wallet creation height hint is set (via
           ``set_wallet_creation_height``), the height is floored up to it,
           since the wallet cannot hold coins from before it was created.
        3. Heights beyond the current chain tip are clamped down to the tip.
           Callers using mainnet-derived constants (e.g. SegWit activation
           height 481824, which JAM sends by default) would otherwise be
           rejected by Core with ``RPC error -8: Invalid start_height`` on
           signet/testnet/regtest where the tip is much lower.
        """
        effective_height = max(0, start_height)
        if effective_height != start_height:
            logger.warning(f"Requested rescan height {start_height} is negative; clamping to 0")

        if (
            self._wallet_creation_height is not None
            and effective_height < self._wallet_creation_height
        ):
            logger.info(
                f"Flooring rescan start height {effective_height} to wallet creation "
                f"height {self._wallet_creation_height}; coins cannot predate it. "
                "Adjust the wallet creation height to scan earlier blocks."
            )
            effective_height = self._wallet_creation_height

        chain_tip = await self.get_block_height()
        if effective_height > chain_tip:
            logger.warning(
                f"Requested rescan height {effective_height} is beyond the chain "
                f"tip {chain_tip}; clamping to the tip"
            )
            effective_height = chain_tip

        return effective_height

    async def start_background_rescan(self, start_height: int = 0) -> bool:
        """
        Trigger a server-side blockchain rescan and return once Bitcoin
        Core has actually started it.

        ``rescanblockchain`` is a blocking RPC, but the rescan itself runs
        inside Bitcoin Core (not the client) and is not bound to the HTTP
        connection: once Core accepts the call, the scan keeps running
        even if the client disconnects (this is what ``abortrescan``
        exists for). We exploit that by posting the RPC with a short
        HTTP timeout, swallowing the expected ``TimeoutException``, and
        then polling ``getwalletinfo.scanning`` to confirm the scan is
        actually in progress before returning.

        Previously this method used ``asyncio.create_task`` to run the
        RPC in the background. That task was tied to the current event
        loop and could be torn down before the RPC was ever sent if the
        caller exited shortly after, so the rescan kick could be a
        silent no-op.

        Args:
            start_height: Block height to start rescan from (default: 0 = genesis).
                The value is normalized via ``_effective_rescan_height``:
                floored up to the wallet creation height when one is set (the
                wallet cannot hold coins from before it was created, so
                scanning earlier blocks only wastes time) and clamped into
                ``[0, chain tip]`` so out-of-range requests do not make
                Bitcoin Core reject the rescan outright.

        Returns:
            True if the rescan already completed synchronously (fast
            regtest / already-synced wallets), False if a background scan
            is now running server-side and the caller should poll
            ``get_rescan_status`` / ``wait_for_rescan_complete``.

        Raises:
            RuntimeError: If Bitcoin Core does not start scanning within
                a reasonable window (10s).
        """
        if not self._wallet_loaded:
            raise RuntimeError("Wallet not loaded. Call create_wallet() first.")

        start_height = await self._effective_rescan_height(start_height)

        logger.info(
            f"Triggering blockchain rescan from height {start_height}. "
            "Bitcoin Core will keep running it server-side even if the CLI exits."
        )

        # Short-timeout client. We expect the request to time out because
        # rescanblockchain only returns once the scan completes, which can
        # take hours on mainnet.
        kick_client = httpx.AsyncClient(timeout=2.0, auth=(self.rpc_user, self.rpc_password))
        try:
            try:
                await self._rpc_call(
                    "rescanblockchain",
                    [start_height],
                    client=kick_client,
                )
                # If we got a clean return, the rescan was so fast (regtest /
                # already-synced wallet) that it completed inside 2s. That is
                # fine, nothing more to do.
                logger.info("rescanblockchain returned synchronously (fast wallet/regtest)")
                return True
            except httpx.TimeoutException:
                # Expected. Bitcoin Core is now scanning server-side.
                pass
            except ValueError as exc:
                # RPC error -4: "Wallet is currently rescanning. Abort existing
                # rescan or wait." A scan is already running server-side, so
                # instead of surfacing a spurious failure, fall through to the
                # confirmation loop and let the caller track the existing scan.
                if "-4" not in str(exc) or "rescan" not in str(exc).lower():
                    raise
                logger.info(
                    "Bitcoin Core is already rescanning; tracking the existing "
                    "scan instead of starting a new one"
                )
        finally:
            await kick_client.aclose()

        # Confirm bitcoind actually started scanning. If we never observe
        # ``scanning`` go truthy within the grace window, something is
        # wrong (request was rejected, wallet not loaded server-side, ...)
        # and we should surface that rather than pretend the rescan kicked
        # off.
        deadline = asyncio.get_event_loop().time() + 10.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                info = await self._rpc_call("getwalletinfo")
            except Exception as exc:
                logger.debug(f"getwalletinfo while confirming rescan start: {exc}")
                await asyncio.sleep(0.5)
                continue
            scanning = info.get("scanning")
            if scanning:
                duration = scanning.get("duration") if isinstance(scanning, dict) else None
                progress = scanning.get("progress") if isinstance(scanning, dict) else None
                duration_str = (
                    f"{int(duration)}s elapsed" if duration is not None else "elapsed unknown"
                )
                progress_str = (
                    f"{float(progress) * 100:.2f}%" if progress is not None else "progress unknown"
                )
                logger.info(
                    f"Bitcoin Core confirmed rescan in progress ({progress_str}, {duration_str})"
                )
                return False
            # Some Bitcoin Core versions return scanning=false very briefly
            # right after acceptance; back off a bit and re-check.
            await asyncio.sleep(0.5)

        raise RuntimeError(
            "Triggered rescanblockchain but Bitcoin Core never reported "
            "scanning=true within 10s. The wallet may not be loaded or "
            "the RPC may have been rejected."
        )

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

    async def get_wallet_scan_status(self) -> dict[str, Any]:
        """Return a diagnostic snapshot of the wallet's scan/coverage state.

        Combines several Bitcoin Core RPCs into a single dict useful for
        debugging the "wallet does not know an address was used" class of
        issues (smart-scan window too narrow, interrupted background full
        rescan, etc.). Used by ``jm-wallet info --scan-status`` and the
        ``jm-wallet rescan`` command.

        Returned keys (any may be ``None`` on RPC failure):

        - ``scanning_in_progress`` (bool): whether Bitcoin Core is
          currently rescanning the wallet (mirrors
          ``getwalletinfo.scanning != false``).
        - ``scan_progress`` (float | None): 0..1, when a scan is active.
        - ``scan_duration_s`` (int | None): elapsed time of the active
          scan in seconds, when active.
        - ``oldest_descriptor_timestamp`` (int | None): minimum
          ``timestamp`` across active descriptors. ``importdescriptors``
          sets this to the smart-scan boundary (~1 year ago) at first
          setup; if no rescan from genesis was ever run, this is the
          effective lower bound of the wallet's history coverage.
        - ``birthtime`` (int | None): block time of the oldest
          transaction that involves any wallet address, computed from
          ``listsinceblock``. For empty wallets this falls back to the
          oldest active descriptor timestamp (and ``None`` if neither
          is available). Cached for the lifetime of the backend.
        - ``txcount`` (int): number of wallet transactions Core knows
          about.
        """
        result: dict[str, Any] = {
            "scanning_in_progress": False,
            "scan_progress": None,
            "scan_duration_s": None,
            "oldest_descriptor_timestamp": None,
            "birthtime": None,
            "txcount": 0,
        }
        if not self._wallet_loaded:
            return result

        try:
            wallet_info = await self._rpc_call("getwalletinfo")
        except Exception as e:
            logger.debug(f"getwalletinfo failed: {e}")
            wallet_info = {}

        scanning = wallet_info.get("scanning")
        if isinstance(scanning, dict):
            result["scanning_in_progress"] = True
            result["scan_progress"] = scanning.get("progress")
            result["scan_duration_s"] = scanning.get("duration")
        result["txcount"] = wallet_info.get("txcount", 0)

        try:
            desc_list = await self._rpc_call("listdescriptors")
            descs = desc_list.get("descriptors", []) if isinstance(desc_list, dict) else []
        except Exception as e:
            logger.debug(f"listdescriptors for scan status failed: {e}")
            descs = []

        # The smallest timestamp across active descriptors marks the
        # oldest block our wallet considers "covered". importdescriptors
        # sets this when the import was issued; a value much newer than
        # the genesis block timestamp tells us the full rescan never ran
        # (smart-scan only).
        timestamps = [
            d["timestamp"]
            for d in descs
            if isinstance(d, dict)
            and d.get("active")
            and isinstance(d.get("timestamp"), (int, float))
        ]
        if timestamps:
            result["oldest_descriptor_timestamp"] = int(min(timestamps))

        # Birthtime: block time of the oldest wallet transaction. Cached
        # because listsinceblock returns the entire wallet history and is
        # expensive for old/deep wallets.
        result["birthtime"] = await self._compute_wallet_birthtime(
            fallback=result["oldest_descriptor_timestamp"],
        )

        return result

    async def _compute_wallet_birthtime(self, fallback: int | None) -> int | None:
        """Return the block time of the oldest wallet transaction.

        The result is cached on the backend instance because new
        transactions can only make the answer older (so a cached non-zero
        value remains correct) or leave it unchanged. We call
        ``listsinceblock`` with the genesis blockhash equivalent (empty
        string) which returns every wallet transaction with its
        ``blocktime`` so we can do a single-pass min in Python.

        For an empty wallet we fall back to the oldest active descriptor
        timestamp, which is the closest proxy Bitcoin Core has for "when
        we expect our coins to start appearing on-chain".
        """
        if self._oldest_tx_blocktime is None:
            try:
                # confirmation depth 1, include removed, include change so
                # nothing is filtered out. Single call: bitcoind streams
                # the full transaction list.
                payload = await self._rpc_call(
                    "listsinceblock",
                    ["", 1, True, True],
                )
                txs = payload.get("transactions", []) if isinstance(payload, dict) else []
                blocktimes = [
                    int(tx["blocktime"])
                    for tx in txs
                    if isinstance(tx, dict) and isinstance(tx.get("blocktime"), (int, float))
                ]
                self._oldest_tx_blocktime = min(blocktimes) if blocktimes else 0
            except Exception as exc:
                logger.debug(f"listsinceblock for birthtime failed: {exc}")
                # Don't cache transient failures; try again next call.
                return fallback

        if self._oldest_tx_blocktime > 0:
            return self._oldest_tx_blocktime
        return fallback

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

        We require at least one positive ``in_progress`` observation before
        accepting ``in_progress == False`` as meaning the rescan finished,
        because ``getwalletinfo.scanning`` can momentarily report False right
        after Bitcoin Core accepts the RPC but before it starts working.

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
                except ValueError as e:
                    # Transient "already loading" (issue #465): a prior load
                    # (e.g. one whose HTTP read timed out during a load-time
                    # rescan) is still running server-side. Wait for it to
                    # finish instead of reporting the wallet as not-set-up,
                    # which would trigger a needless full re-import/rescan.
                    if not (
                        self._is_wallet_loading_error(e)
                        and await self._poll_until_wallet_loaded(WALLET_LOADING_MAX_WAIT)
                    ):
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
                                    "address": txout["scriptPubKey"].get("address", ""),
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
                        "address": u.get("address", ""),
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
            start_height: Block height to start rescan from.  Normalized via
                ``_effective_rescan_height``: floored up to the wallet creation
                height when one is set and clamped into ``[0, chain tip]`` so
                that callers using mainnet-derived constants (e.g. SegWit
                activation height 481824) work correctly on signet/testnet
                where the tip is much lower.

        Returns:
            Rescan result
        """
        try:
            effective_height = await self._effective_rescan_height(start_height)
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
        """Return every wallet-owned address that has ever received funds.

        Uses ``listsinceblock`` with an empty blockhash to fetch every
        wallet transaction (including change outputs) in a single RPC
        roundtrip. ``include_change=true`` is critical: without it Core
        silently drops change-branch addresses, which is the JoinMarket
        deposit-reuse bug that motivated the rewrite.

        Why not ``listtransactions skip=N``?
        -----------------------------------
        ``listtransactions`` is paginated with ``count``/``skip``, but
        Core walks the wallet's transaction list from the beginning on
        every call (O(N) per page → O(N^2) total). On real-world heavy
        JoinMarket wallets (220K+ tx-entries after a genesis rescan)
        page 200+ takes minutes server-side and routinely trips socket
        timeouts or causes bitcoind to drop the connection mid-stream.
        The walker then silently returned the partial result, the
        wallet thought it had enumerated history, and the next deposit
        address landed on a previously-funded address. See
        ``tmp/joinmarket_ng_wallet_rescan_3.txt`` for a real-world
        failure trace.

        ``listsinceblock`` enumerates in a single server-side pass: O(N)
        total, one HTTP roundtrip, one large JSON response. aiohttp /
        httpx stream multi-megabyte JSON without issue.

        Failure semantics
        -----------------
        Raises on any RPC error. The previous implementation logged a
        warning and returned whatever was collected so far; that was a
        privacy bug because the partial set was then treated as
        authoritative by the sync layer and persisted to BIP-329. The
        wallet's persisted ``used_addresses`` store remains the canonical
        do-not-reissue set; the sync layer is responsible for unioning
        this RPC result with persisted state (never replacing it).
        """
        addresses: set[str] = set()

        # Wallet may not be loaded yet during initial setup.
        if not self._wallet_loaded:
            return addresses

        # Empty blockhash → enumerate from genesis. ``include_watchonly``
        # is deprecated in Core 30 (it always includes watch-only on
        # descriptor wallets) but we pass ``true`` for backwards
        # compatibility with older nodes. ``include_change=true`` is the
        # critical flag: without it change-branch outputs are dropped and
        # we miss every internal address that has ever received funds.
        try:
            result = await self._rpc_call(
                "listsinceblock",
                # blockhash, target_confirmations, include_watchonly,
                # include_removed, include_change
                ["", 1, True, True, True],
            )
        except Exception:
            # Surface the failure: callers (sync layer, scan_status_only
            # diagnostic) must distinguish "no addresses" from "RPC
            # failed" and refuse to downgrade persisted state.
            logger.exception("listsinceblock failed; cannot enumerate address history")
            raise

        transactions = result.get("transactions", []) if isinstance(result, dict) else []

        for entry in transactions:
            cat = entry.get("category")
            # ``receive`` covers external funding AND change returned to
            # the wallet on internal descriptors (when include_change=true).
            # ``generate``/``immature`` cover mining rewards if the user is
            # also a miner; harmless to include. ``send`` is excluded
            # because the address there is the destination, not ours.
            if cat in ("receive", "generate", "immature"):
                addr = entry.get("address")
                if addr:
                    addresses.add(addr)

        logger.debug(
            f"Found {len(addresses)} addresses with history "
            f"(scanned {len(transactions)} listsinceblock entries)"
        )
        return addresses

    async def address_has_history(self, address: str) -> bool | None:
        """Return True if ``address`` has ever received funds on-chain.

        Uses ``getreceivedbyaddress addr 0`` (zero-confirmation threshold)
        which is a cheap O(1) lookup against the wallet's per-address
        receive index. This is the defense-in-depth check used before
        proposing a fresh deposit address: even if the bulk enumeration
        in :meth:`get_addresses_with_history` was incomplete (RPC
        truncation, node crash mid-walk, stale persisted state),
        ``getreceivedbyaddress`` will catch a previously-funded address
        because Bitcoin Core keeps that index up to date as part of
        normal wallet operation.

        Returns
        -------
        ``True`` if the address has any received amount > 0,
        ``False`` if it has zero,
        ``None`` if the RPC failed (callers should treat this as
        "unknown" and either retry or fail closed depending on context).

        Notes
        -----
        - The address must be watched by the wallet for
          ``getreceivedbyaddress`` to work; Core errors with -4 / -5
          otherwise. JoinMarket deposit addresses derive from imported
          ranged descriptors so they are always watched within range.
        - ``getreceivedbyaddress`` only counts confirmed funding by
          default; passing ``0`` includes the mempool so we also catch
          addresses that received funds but the tx isn't mined yet.
        """
        if not self._wallet_loaded:
            return None
        try:
            received = await self._rpc_call("getreceivedbyaddress", [address, 0])
        except Exception as exc:
            logger.warning(
                f"getreceivedbyaddress({address[:12]}...) failed: {exc}; "
                f"cannot verify whether address has on-chain history"
            )
            return None
        # Core returns a BTC float; any non-zero value means the address
        # has been funded at least once. Even tiny dust receives count
        # for privacy purposes.
        try:
            return float(received) > 0
        except (TypeError, ValueError):
            return None

    async def get_address_info(self, address: str) -> dict[str, Any] | None:
        """
        Return Bitcoin Core's ``getaddressinfo`` result for an address.

        The result includes ``ismine`` and (for descriptor wallets) ``desc``
        — the descriptor of the address with its derivation path baked in,
        which lets callers determine ``(change, index)`` without scanning.

        Returns ``None`` on RPC error so callers can fall back conservatively
        (e.g., treat as not-ours rather than triggering an expensive scan).
        """
        try:
            return await self._rpc_call("getaddressinfo", [address])
        except Exception as e:
            logger.debug(f"getaddressinfo failed for {address[:20]}...: {e}")
            return None

    async def batch_get_address_info(self, addresses: Sequence[str]) -> list[dict[str, Any] | None]:
        """
        Look up ``getaddressinfo`` for many addresses in a single JSON-RPC batch.

        This is the batched counterpart to :meth:`get_address_info`. It is
        intended for hot paths that need to resolve ismine / desc for
        hundreds or thousands of addresses at once (notably the wallet sync
        loop's ``addresses_beyond_range`` handler), where a sequential loop
        would otherwise pay N HTTP round-trips. Empirically ~20x faster than
        a serial loop against a localhost regtest node and dramatically more
        on remote / Tor-fronted Core endpoints.

        Per-address RPC errors are converted to ``None`` (matching the
        single-address :meth:`get_address_info` contract) so callers can
        fall back conservatively.

        Args:
            addresses: Sequence of Bitcoin addresses to look up. Order is
                preserved in the returned list.

        Returns:
            List of ``getaddressinfo`` result dicts, parallel to
            ``addresses``. Entries for addresses that errored at the RPC
            layer are ``None``.
        """
        if not addresses:
            return []
        raw = await self._rpc_batch_call([("getaddressinfo", [a]) for a in addresses])
        out: list[dict[str, Any] | None] = []
        for i, value in enumerate(raw):
            if isinstance(value, Exception):
                logger.debug(f"batch getaddressinfo failed for {addresses[i][:20]}...: {value}")
                out.append(None)
            else:
                out.append(value)
        return out

    async def is_address_mine(self, address: str) -> bool:
        """
        Check whether an address belongs to this wallet.

        Uses Bitcoin Core's ``getaddressinfo`` RPC, which is authoritative for
        descriptor wallets: it inspects the loaded descriptors and returns
        ``ismine=True`` only for addresses derived from this wallet's own
        descriptors. Counterparty addresses that merely appear in transaction
        history (e.g., in ``listaddressgroupings`` due to CoinJoin co-spends)
        return ``ismine=False``.

        Args:
            address: Bitcoin address to check.

        Returns:
            ``True`` if the address belongs to this wallet, ``False`` otherwise
            (including on RPC errors, where we conservatively assume it is not
            ours rather than triggering an expensive extended-range scan).
        """
        info = await self.get_address_info(address)
        return bool(info.get("ismine", False)) if info else False

    async def filter_mine_addresses(self, addresses: Sequence[str]) -> set[str]:
        """
        Return the subset of ``addresses`` that belong to this wallet.

        Uses a single JSON-RPC batch under the hood, so this scales to
        thousands of addresses with one HTTP round-trip per ``chunk_size``
        block rather than one per address.

        Args:
            addresses: Iterable of Bitcoin addresses to check.

        Returns:
            Set of addresses for which ``ismine`` is true.
        """
        if not addresses:
            return set()
        addr_list = list(addresses)
        infos = await self.batch_get_address_info(addr_list)
        return {
            addr
            for addr, info in zip(addr_list, infos)
            if info is not None and info.get("ismine", False)
        }

    async def get_descriptor_ranges(
        self, raise_on_error: bool = False
    ) -> dict[str, tuple[int, int]]:
        """
        Get the current range for each imported descriptor.

        Args:
            raise_on_error: When True, propagate the underlying RPC error
                instead of returning ``{}``. Use this in code paths where an
                empty result would silently corrupt subsequent decisions
                (e.g. the pre-import range check, where missing data leads
                Bitcoin Core to reject the request with "new range must
                include current range").

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
            if raise_on_error:
                raise
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


def generate_wallet_name(mnemonic_fingerprint: str, network: str = "mainnet") -> str:
    """
    Generate a deterministic wallet name from mnemonic fingerprint.

    This ensures the same mnemonic always uses the same wallet, avoiding
    duplicate wallet creation.

    Args:
        mnemonic_fingerprint: First 8 chars of SHA256(mnemonic)
        network: Network name (mainnet, testnet, regtest)

    Returns:
        Wallet name like "jm_abc12345_mainnet"
    """
    return f"jm_{mnemonic_fingerprint}_{network}"


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
