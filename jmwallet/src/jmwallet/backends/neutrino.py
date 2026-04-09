"""
Neutrino (BIP157/BIP158) light client blockchain backend.

Lightweight alternative to running a full Bitcoin node.
Uses compact block filters for privacy-preserving SPV operation.

The Neutrino client runs as a separate Go process and communicates via gRPC.
This backend wraps the neutrino gRPC API for the JoinMarket wallet.

Reference: https://github.com/lightninglabs/neutrino

Neutrino-compatible Protocol Support:
This backend implements verify_utxo_with_metadata() for Neutrino-compatible
UTXO verification. When peers provide scriptPubKey and blockheight hints
(via neutrino_compat feature flag), this backend can verify UTXOs without
arbitrary queries by:
1. Adding the scriptPubKey to the watch list
2. Rescanning from the hinted blockheight
3. Downloading matching blocks via compact block filters
4. Extracting and verifying the UTXO
"""

from __future__ import annotations

import asyncio
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from jmwallet.backends.base import (
    UTXO,
    BlockchainBackend,
    BondVerificationRequest,
    BondVerificationResult,
    Transaction,
    UTXOVerificationResult,
)


@dataclass
class ServerCapabilities:
    """Detected capabilities of the neutrino-api server.

    Populated once on first successful connection via
    ``NeutrinoBackend._detect_server_capabilities()``.  Provides
    feature-flag information that allows the backend to degrade
    gracefully when running against older server versions.
    """

    #: True once detection has run (even if probes failed).
    detected: bool = False

    #: ``GET /v1/rescan/status`` is available (v0.7.0+).
    has_rescan_status: bool = False

    #: Rescan status includes ``last_start_height``/``last_scanned_tip``
    #: (v0.9.0+ with persistent state).
    has_persistent_rescan_state: bool = False

    #: Extra fields returned by ``/v1/status`` (informational).
    status_fields: dict[str, Any] = field(default_factory=dict)


class NeutrinoBackend(BlockchainBackend):
    """
    Blockchain backend using Neutrino light client.

    Neutrino is a privacy-preserving Bitcoin light client that uses
    BIP157/BIP158 compact block filters instead of traditional SPV.

    Communication with the neutrino daemon is via REST API.
    The neutrino daemon should be running alongside this client.
    """

    supports_watch_address: bool = True
    _INITIAL_RESCAN_TIMEOUT_SECONDS: float = 1800.0
    _ONGOING_INITIAL_RESCAN_CHECK_TIMEOUT_SECONDS: float = 30.0
    _TRIVIAL_RESCAN_BLOCKS: int = 1000

    def __init__(
        self,
        neutrino_url: str = "http://127.0.0.1:8334",
        network: str = "mainnet",
        add_peers: list[str] | None = None,
        data_dir: str = "/data/neutrino",
        scan_start_height: int | None = None,
        scan_lookback_blocks: int = 105120,
        tls_cert_path: str | None = None,
        auth_token: str | None = None,
    ):
        """
        Initialize Neutrino backend.

        Args:
            neutrino_url: URL of the neutrino REST API (default port 8334)
            network: Bitcoin network (mainnet, testnet, regtest, signet)
            add_peers: Preferred peer addresses to add (optional)
            data_dir: Directory for neutrino data (headers, filters)
            scan_start_height: Block height to start initial rescan from (optional).
                If set, skips scanning blocks before this height during initial wallet sync.
                Critical for performance on mainnet/signet where scanning from genesis is slow.
                If None, a smart default is computed at first sync using scan_lookback_blocks.
            scan_lookback_blocks: Number of blocks to look back from the chain tip when
                scan_start_height is not set. Defaults to 105120 (~2 years of blocks).
                Only used on networks where _min_valid_blockheight is 0 (signet, regtest).
            tls_cert_path: Path to neutrino-api TLS certificate for HTTPS verification.
                When set, the client connects over HTTPS and pins the server certificate.
            auth_token: API bearer token for neutrino-api authentication.
                Sent as ``Authorization: Bearer <token>`` on every request.
        """
        self.neutrino_url = neutrino_url.rstrip("/")
        self.network = network
        self.add_peers = add_peers or []
        self.data_dir = data_dir

        # Store auth settings for client (re-)creation in close().
        self._tls_cert_path = tls_cert_path
        self._auth_token = auth_token
        self.client = self._build_http_client()

        # Cache for watched addresses (neutrino needs to know what to scan for)
        self._watched_addresses: set[str] = set()
        self._watched_outpoints: set[tuple[str, int]] = set()

        # Security limits to prevent DoS via excessive watch list / rescan abuse
        self._max_watched_addresses: int = 10000  # Maximum addresses to track
        self._max_rescan_depth: int = 100000  # Maximum blocks to rescan (roughly 2 years)
        self._min_valid_blockheight: int = 481824  # SegWit activation (mainnet)
        # For testnet/regtest, this will be adjusted based on network

        # Block filter cache
        self._filter_header_tip: int = 0
        self._synced: bool = False

        # Track if we've done the initial rescan
        self._initial_rescan_done: bool = False
        self._initial_rescan_started: bool = False

        # Track the last block height we rescanned to (for incremental rescans)
        self._last_rescan_height: int = 0

        # Track if we just triggered a rescan (to avoid waiting multiple times)
        self._rescan_in_progress: bool = False

        # Track if we just completed a rescan (to enable retry logic for async UTXO lookups)
        self._just_rescanned: bool = False

        # Adjust minimum blockheight based on network
        if network == "regtest":
            self._min_valid_blockheight = 0  # Regtest can have any height
        elif network == "testnet":
            self._min_valid_blockheight = 834624  # Approximate SegWit on testnet
        elif network == "signet":
            self._min_valid_blockheight = 0  # Signet started with SegWit

        # Store the explicit user override (may be None).
        self._explicit_scan_start_height: int | None = scan_start_height
        self._scan_lookback_blocks: int = scan_lookback_blocks

        # Wallet creation height hint (set later via set_wallet_creation_height).
        self._wallet_creation_height: int | None = None

        # _scan_start_height is resolved lazily in _resolve_scan_start_height()
        # once we know the chain tip.  For now, use the explicit value or a
        # placeholder that will be overwritten before the first rescan.
        self._scan_start_height: int = (
            scan_start_height if scan_start_height is not None else self._min_valid_blockheight
        )

        # Server capability detection (populated once on first connection).
        self._server_capabilities = ServerCapabilities()

    def _build_http_client(self) -> httpx.AsyncClient:
        """Create an ``httpx.AsyncClient`` with optional TLS pinning and auth."""
        kwargs: dict[str, Any] = {"timeout": 300.0}

        if self._tls_cert_path:
            cert_path = Path(self._tls_cert_path).expanduser()
            if not cert_path.is_file():
                logger.warning(
                    f"TLS certificate not found at {cert_path}; "
                    "falling back to default CA verification"
                )
            else:
                ctx = ssl.create_default_context(cafile=str(cert_path))
                kwargs["verify"] = ctx
                logger.debug(f"Neutrino HTTPS pinned to certificate {cert_path}")

        if self._auth_token:
            kwargs["headers"] = {"Authorization": f"Bearer {self._auth_token}"}
            logger.debug("Neutrino API authentication enabled (Bearer token)")

        return httpx.AsyncClient(**kwargs)

    def set_wallet_creation_height(self, height: int | None) -> None:
        """Use wallet creation height as scan start if no explicit override.

        When the wallet was created at a known block height, there is no
        need to scan blocks before that point.  This takes priority over
        the lookback-based default but NOT over an explicit
        ``scan_start_height`` set by the user in config.

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

        if self._explicit_scan_start_height is not None:
            logger.debug(
                f"Ignoring creation_height={height}, "
                f"explicit scan_start_height={self._explicit_scan_start_height} takes priority"
            )
            return
        self._wallet_creation_height = height
        logger.info(f"Wallet creation height set to {height} (will use as scan start hint)")

    @property
    def server_capabilities(self) -> ServerCapabilities:
        """Return the detected server capabilities (read-only)."""
        return self._server_capabilities

    async def _detect_server_capabilities(self) -> None:
        """Probe neutrino-api endpoints once to determine server capabilities.

        Called automatically during the first ``wait_for_sync()`` call.
        Results are cached in ``_server_capabilities`` for the lifetime
        of the backend instance (reset on ``close()``).

        The detection is best-effort: network errors are logged as
        warnings and treated as "capability not available".
        """
        if self._server_capabilities.detected:
            return

        caps = self._server_capabilities

        # --- Probe /v1/status (always available) ---
        try:
            status = await self._api_call("GET", "v1/status")
            caps.status_fields = dict(status) if isinstance(status, dict) else {}
            logger.info(
                "Neutrino server: block_height={}, filter_height={}, synced={}",
                status.get("block_height", "?"),
                status.get("filter_height", "?"),
                status.get("synced", "?"),
            )
        except Exception as exc:
            logger.warning(f"Could not probe neutrino-api /v1/status: {exc}")
            caps.detected = True
            return

        # --- Probe /v1/rescan/status (v0.7.0+) ---
        try:
            rescan_status = await self._api_call("GET", "v1/rescan/status")
            caps.has_rescan_status = True

            # Check for persistent state fields (v0.9.0+)
            if "last_start_height" in rescan_status and "last_scanned_tip" in rescan_status:
                caps.has_persistent_rescan_state = True
                logger.info(
                    "Neutrino rescan state: last_start={}, last_tip={}, in_progress={}",
                    rescan_status.get("last_start_height", 0),
                    rescan_status.get("last_scanned_tip", 0),
                    rescan_status.get("in_progress", False),
                )
            else:
                logger.info(
                    "Neutrino rescan status available (no persistent state -- "
                    "server older than v0.9.0)"
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                logger.warning(
                    "GET /v1/rescan/status returned 404 -- neutrino-api may be "
                    "older than v0.7.0. Rescan completion polling will not work; "
                    "consider upgrading to v0.9.0+."
                )
            else:
                logger.warning(f"Rescan status probe failed: {exc}")
        except Exception as exc:
            logger.warning(f"Rescan status probe failed: {exc}")

        caps.detected = True

        # Summary log line
        features = []
        if caps.has_rescan_status:
            features.append("rescan-status")
        if caps.has_persistent_rescan_state:
            features.append("persistent-state")
        if features:
            logger.info(f"Neutrino server capabilities: {', '.join(features)}")
        else:
            logger.warning(
                "Neutrino server has no detected advanced capabilities. "
                "Upgrade to neutrino-api v0.9.0+ for best performance."
            )

    async def _api_call(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Make an API call to the neutrino daemon."""
        url = f"{self.neutrino_url}/{endpoint}"

        try:
            if method == "GET":
                response = await self.client.get(url, params=params)
            elif method == "POST":
                response = await self.client.post(url, json=data)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status()
            return response.json()

        except httpx.HTTPStatusError as e:
            # 404 responses are expected during normal operation (unconfirmed txs, spent UTXOs)
            # Don't log them as errors to avoid confusing users
            if e.response.status_code == 404:
                logger.debug(f"Neutrino API returned 404: {endpoint}")
            else:
                logger.error(f"Neutrino API call failed: {endpoint} - {e}")
            raise
        except httpx.HTTPError as e:
            logger.error(f"Neutrino API call failed: {endpoint} - {e}")
            raise

    async def _wait_for_rescan(
        self,
        timeout: float = 300.0,
        poll_interval: float = 2.0,
        require_started: bool = False,
        start_timeout: float = 10.0,
    ) -> bool:
        """
        Wait until the neutrino daemon reports no rescan is in progress.

        Polls ``GET /v1/rescan/status`` every *poll_interval* seconds until
        ``in_progress`` is False or *timeout* is exceeded.

        Args:
            timeout: Maximum seconds to wait (default 300 s / 5 min).
            poll_interval: Seconds between status polls (default 2 s).
            require_started: If True, require observing ``in_progress=True`` at
                least once before accepting completion.
            start_timeout: Seconds to wait for ``in_progress=True`` to appear
                when ``require_started`` is enabled.

        Returns:
            True if rescan completion was confirmed via status polling,
            False if status could not be confirmed (timeout or endpoint error).
        """
        # When the server does not expose /v1/rescan/status, polling is
        # pointless.  Fall back immediately so the caller uses a fixed delay.
        if self._server_capabilities.detected and not self._server_capabilities.has_rescan_status:
            logger.debug("Server lacks /v1/rescan/status; cannot poll for completion")
            return False

        start = asyncio.get_event_loop().time()
        saw_in_progress = False
        while True:
            try:
                status = await self._api_call("GET", "v1/rescan/status")
                in_progress = bool(status.get("in_progress", False))
                if in_progress:
                    saw_in_progress = True
                elif require_started and not saw_in_progress:
                    elapsed = asyncio.get_event_loop().time() - start
                    if elapsed < start_timeout:
                        await asyncio.sleep(poll_interval)
                        continue
                    logger.warning(
                        "Rescan status never entered in_progress=true; "
                        "treating completion as unconfirmed"
                    )
                    return False

                if not in_progress:
                    return True
            except Exception as e:
                # Endpoint not available (old server version or any error) –
                # do not assume completion.
                if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
                    logger.warning("GET /v1/rescan/status not available")
                else:
                    logger.warning(f"GET /v1/rescan/status failed ({e})")
                return False

            elapsed = asyncio.get_event_loop().time() - start
            if elapsed >= timeout:
                logger.warning(f"Rescan did not complete within {timeout:.0f}s; proceeding anyway")
                return False

            await asyncio.sleep(poll_interval)

    async def wait_for_sync(self, timeout: float = 300.0) -> bool:
        """
        Wait for neutrino to sync block headers and filters.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if synced, False if timeout
        """
        start_time = asyncio.get_event_loop().time()
        last_progress_log = start_time

        # Detect server capabilities once on the first sync attempt.
        if not self._server_capabilities.detected:
            await self._detect_server_capabilities()

        while True:
            try:
                status = await self._api_call("GET", "v1/status")
                synced = status.get("synced", False)
                block_height = status.get("block_height", 0)
                filter_height = status.get("filter_height", 0)

                if synced and block_height == filter_height:
                    self._synced = True
                    self._filter_header_tip = block_height
                    logger.info(f"Neutrino synced at height {block_height}")
                    return True

                now = asyncio.get_event_loop().time()
                # Log progress every 30 seconds at INFO level for user visibility
                if now - last_progress_log >= 30.0:
                    elapsed = now - start_time
                    logger.info(
                        f"Neutrino syncing... headers: {block_height}, "
                        f"filters: {filter_height} ({elapsed:.0f}s elapsed)"
                    )
                    last_progress_log = now
                else:
                    logger.debug(f"Syncing... blocks: {block_height}, filters: {filter_height}")

            except Exception as e:
                logger.warning(f"Waiting for neutrino daemon: {e}")

            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed > timeout:
                logger.error("Neutrino sync timeout")
                return False

            await asyncio.sleep(2.0)

    async def add_watch_address(self, address: str) -> None:
        """
        Add an address to the local watch list.

        In neutrino-api v0.4, address watching is implicit - you just query
        UTXOs or do rescans with the addresses you care about. This method
        tracks addresses locally for convenience.

        Security: Limits the number of watched addresses to prevent memory
        exhaustion attacks.

        Args:
            address: Bitcoin address to watch

        Raises:
            ValueError: If watch list limit exceeded
        """
        if address in self._watched_addresses:
            return

        if len(self._watched_addresses) >= self._max_watched_addresses:
            logger.warning(
                f"Watch list limit reached ({self._max_watched_addresses}). "
                f"Cannot add address: {address[:20]}..."
            )
            raise ValueError(f"Watch list limit ({self._max_watched_addresses}) exceeded")

        self._watched_addresses.add(address)
        logger.trace(f"Watching address: {address}")

    async def add_watch_outpoint(self, txid: str, vout: int) -> None:
        """
        Add an outpoint to the local watch list.

        In neutrino-api v0.4, outpoint watching is done via UTXO queries
        with the address parameter. This method tracks outpoints locally.

        Args:
            txid: Transaction ID
            vout: Output index
        """
        outpoint = (txid, vout)
        if outpoint in self._watched_outpoints:
            return

        self._watched_outpoints.add(outpoint)
        logger.debug(f"Watching outpoint: {txid}:{vout}")

    async def _get_rescan_coverage(self) -> tuple[int, int]:
        """Query neutrino-api for persisted rescan coverage.

        The neutrino-api ``GET /v1/rescan/status`` endpoint returns metadata
        about the most recent rescan: ``last_start_height`` and
        ``last_scanned_tip``.  These are persisted to disk and survive
        neutrino-api restarts.

        On servers older than v0.9.0 (no persistent state fields), this
        always returns ``(0, 0)`` which forces a fresh rescan -- the safe
        fallback when we cannot know what has been scanned previously.

        Returns:
            ``(last_start_height, last_scanned_tip)``.  Both are 0 when no
            prior rescan has been performed or the endpoint is unavailable.
        """
        # Short-circuit when we already know the server cannot provide this.
        if (
            self._server_capabilities.detected
            and not self._server_capabilities.has_persistent_rescan_state
        ):
            return (0, 0)

        try:
            status = await self._api_call("GET", "v1/rescan/status")
            return (
                int(status.get("last_start_height", 0)),
                int(status.get("last_scanned_tip", 0)),
            )
        except Exception:
            return (0, 0)

    async def _resolve_scan_start_height(self, tip_height: int) -> int:
        """Compute the effective scan start height for the initial rescan.

        Priority order:
        1. Explicit ``scan_start_height`` from config (always wins).
        2. ``creation_height`` from wallet file (if wallet was created at a
           known block height, no need to scan before that).
        3. Lookback window from the current chain tip (signet/regtest where
           ``_min_valid_blockheight`` is 0).
        4. ``_min_valid_blockheight`` (SegWit activation on mainnet/testnet).

        Returns:
            The block height to start the initial rescan from.
        """
        if self._explicit_scan_start_height is not None:
            return self._explicit_scan_start_height

        if self._wallet_creation_height is not None:
            start = max(self._wallet_creation_height, self._min_valid_blockheight)
            logger.info(
                f"Using wallet creation height as scan start: {start} "
                f"(creation={self._wallet_creation_height}, "
                f"min_valid={self._min_valid_blockheight})"
            )
            return start

        if self._scan_lookback_blocks > 0 and tip_height > self._scan_lookback_blocks:
            lookback_height = tip_height - self._scan_lookback_blocks
            start = max(lookback_height, self._min_valid_blockheight)
        else:
            start = self._min_valid_blockheight

        logger.info(
            f"Computed scan start height: {start} "
            f"(tip={tip_height}, lookback={self._scan_lookback_blocks}, "
            f"min_valid={self._min_valid_blockheight})"
        )
        return start

    async def get_utxos(self, addresses: list[str]) -> list[UTXO]:
        """
        Get UTXOs for given addresses using neutrino's rescan capability.

        Neutrino will scan the blockchain using compact block filters
        to find transactions relevant to the watched addresses.

        On first call, ensures the neutrino node is fully synced (headers +
        compact block filters up to the chain tip) before triggering a
        blockchain rescan.  This is critical because scanBlocks() can only
        check filters it has already downloaded -- if the node is still
        syncing, blocks containing funded transactions will be silently missed.

        After initial rescan, automatically rescans if new blocks have arrived
        to detect transactions that occurred after the last scan.
        """
        utxos: list[UTXO] = []

        # Add addresses to watch list
        for address in addresses:
            await self.add_watch_address(address)

        # ---- Ensure neutrino is synced before initial rescan ----
        # Without this, the rescan may run against an incomplete filter set
        # and silently miss blocks that contain our funded transactions.
        if not self._initial_rescan_done and not self._synced:
            logger.info("Waiting for neutrino to sync headers and filters before initial rescan...")
            synced = await self.wait_for_sync(timeout=self._INITIAL_RESCAN_TIMEOUT_SECONDS)
            if not synced:
                logger.warning(
                    "Neutrino did not fully sync within timeout; "
                    "proceeding with rescan on partial filter set "
                    "(balance may be incomplete until next sync)"
                )

        # Get current tip height to check if new blocks have arrived
        current_height = await self.get_block_height()

        # On first UTXO query, trigger a full blockchain rescan to find existing UTXOs
        # This is critical for wallets that were funded before neutrino was watching them
        logger.debug(
            f"get_utxos: _initial_rescan_done={self._initial_rescan_done}, "
            f"watched_addresses={len(self._watched_addresses)}, "
            f"last_rescan={self._last_rescan_height}, current={current_height}"
        )
        if not self._initial_rescan_done and self._watched_addresses:
            # Resolve the scan start height now that we know the chain tip.
            self._scan_start_height = await self._resolve_scan_start_height(current_height)

            # Check if neutrino-api already has rescan coverage for our range.
            # This avoids redundant initial rescans on every CLI invocation --
            # the neutrino-api persists scan metadata to disk, so blocks scanned
            # by a prior process are not re-scanned.
            prior_start, prior_tip = await self._get_rescan_coverage()

            if (
                prior_tip >= current_height
                and prior_start > 0
                and prior_start <= self._scan_start_height
            ):
                # neutrino-api already scanned from our start height to the
                # current tip.  No rescan needed -- just query UTXOs directly.
                logger.info(
                    f"Neutrino already scanned to tip {prior_tip} "
                    f"(from height {prior_start}); skipping initial rescan"
                )
                self._initial_rescan_done = True
                self._last_rescan_height = prior_tip
                # Don't set _just_rescanned -- no async UTXO indexing to wait for.
            else:
                completed = False
                if not self._initial_rescan_started:
                    # Estimate how many new blocks actually need scanning.
                    effective_prior_tip = max(prior_tip, self._scan_start_height)
                    blocks_to_scan = max(0, current_height - effective_prior_tip)

                    logger.info(
                        f"Performing initial blockchain rescan for "
                        f"{len(self._watched_addresses)} watched addresses "
                        f"from height {self._scan_start_height} to {current_height} "
                        f"(~{blocks_to_scan} blocks to scan)..."
                    )
                    try:
                        await self._api_call(
                            "POST",
                            "v1/rescan",
                            data={
                                "addresses": list(self._watched_addresses),
                                "start_height": self._scan_start_height,
                            },
                        )
                        self._initial_rescan_started = True
                        completed = await self._wait_for_rescan(
                            require_started=True,
                            timeout=self._INITIAL_RESCAN_TIMEOUT_SECONDS,
                        )
                    except Exception as e:
                        self._initial_rescan_started = False
                        logger.warning(f"Initial rescan failed (will retry on next sync): {e}")
                else:
                    completed = await self._wait_for_rescan(
                        require_started=False,
                        timeout=self._ONGOING_INITIAL_RESCAN_CHECK_TIMEOUT_SECONDS,
                    )

                if completed:
                    self._initial_rescan_done = True
                    self._initial_rescan_started = False
                    self._rescan_in_progress = False

                    # Use the actual scanned tip from metadata for accuracy.
                    # This may be higher than *current_height* if new blocks
                    # arrived during the rescan.
                    _, post_tip = await self._get_rescan_coverage()
                    self._last_rescan_height = max(post_tip, current_height)

                    # Only enable UTXO retries when a significant number of
                    # blocks were actually scanned.  For trivial catch-ups
                    # (e.g. a few blocks), async indexing completes instantly
                    # and retries just waste 8-13 seconds on empty wallets.
                    blocks_actually_scanned = max(
                        0, post_tip - max(prior_tip, self._scan_start_height - 1)
                    )
                    if blocks_actually_scanned > self._TRIVIAL_RESCAN_BLOCKS:
                        self._just_rescanned = True
                        logger.info(
                            f"Initial blockchain rescan completed "
                            f"({blocks_actually_scanned} blocks scanned)"
                        )
                    else:
                        logger.info(
                            f"Initial blockchain rescan completed (trivial: "
                            f"{blocks_actually_scanned} blocks, skipping UTXO retries)"
                        )
                else:
                    logger.warning(
                        "Initial rescan completion could not be confirmed; rescan still pending"
                    )
                    self._rescan_in_progress = False
        elif current_height > self._last_rescan_height and not self._rescan_in_progress:
            # New blocks have arrived since last rescan - need to scan them.
            # neutrino-api does NOT automatically watch addresses for new
            # blocks; each rescan must be explicitly triggered.
            # We rescan ALL watched addresses, not just the ones in the
            # current query, because wallet sync happens mixdepth by mixdepth
            # and we need to find outputs to any of our addresses.
            self._rescan_in_progress = True
            logger.info(
                f"New blocks detected ({self._last_rescan_height} -> {current_height}), "
                f"rescanning for {len(self._watched_addresses)} watched addresses..."
            )
            try:
                # Rescan from just before the last known height to catch edge cases
                start_height = max(0, self._last_rescan_height - 1)

                await self._api_call(
                    "POST",
                    "v1/rescan",
                    data={
                        "addresses": list(self._watched_addresses),
                        "start_height": start_height,
                    },
                )
                completed = await self._wait_for_rescan(require_started=True)

                if completed:
                    _, post_tip = await self._get_rescan_coverage()
                    self._last_rescan_height = max(post_tip, current_height)
                    self._rescan_in_progress = False

                    blocks_scanned = max(0, current_height - start_height)
                    if blocks_scanned > self._TRIVIAL_RESCAN_BLOCKS:
                        self._just_rescanned = True
                    logger.info(
                        f"Incremental rescan completed from block "
                        f"{start_height} to {self._last_rescan_height}"
                    )
                else:
                    logger.warning(
                        "Incremental rescan completion could not be confirmed; "
                        "will retry from previous height"
                    )
                    self._rescan_in_progress = False
            except Exception as e:
                logger.warning(f"Incremental rescan failed: {e}")
                self._rescan_in_progress = False
        elif self._rescan_in_progress:
            # A rescan was just triggered by a previous get_utxos call in this batch.
            # Wait briefly for it to complete.
            logger.debug("Rescan in progress from previous query, waiting briefly...")
            await asyncio.sleep(1.0)

        try:
            # Request UTXO scan for addresses with retry logic
            # The neutrino API performs UTXO lookups asynchronously, so we may need
            # to retry if the initial query happens before async indexing completes.
            # We only retry if we just completed a rescan (indicated by _just_rescanned flag)
            # to avoid unnecessary delays when scanning addresses that have no UTXOs.
            max_retries = 5 if self._just_rescanned else 1
            result: dict[str, Any] = {"utxos": []}

            for retry in range(max_retries):
                result = await self._api_call(
                    "POST",
                    "v1/utxos",
                    data={"addresses": addresses},
                )

                utxo_count = len(result.get("utxos", []))

                # If we found UTXOs or this is the last retry, proceed
                if utxo_count > 0 or retry == max_retries - 1:
                    if retry > 0 and self._just_rescanned:
                        logger.debug(f"Found {utxo_count} UTXOs after {retry + 1} attempts")
                    break

                # No UTXOs yet - wait with exponential backoff before retrying
                # This allows time for async UTXO indexing to complete
                wait_time = 1.5**retry  # 1.0s, 1.5s, 2.25s, 3.37s, 5.06s
                logger.debug(
                    f"No UTXOs found on attempt {retry + 1}/{max_retries}, "
                    f"waiting {wait_time:.2f}s for async indexing..."
                )
                await asyncio.sleep(wait_time)

            # Reset the flag after we've completed the UTXO query
            # (subsequent queries in this batch won't need full retry)
            if self._just_rescanned:
                self._just_rescanned = False

            tip_height = await self.get_block_height()

            for utxo_data in result.get("utxos", []):
                height = utxo_data.get("height", 0)
                confirmations = 0
                if height > 0:
                    confirmations = tip_height - height + 1

                utxo = UTXO(
                    txid=utxo_data["txid"],
                    vout=utxo_data["vout"],
                    value=utxo_data["value"],
                    address=utxo_data.get("address", ""),
                    confirmations=confirmations,
                    scriptpubkey=utxo_data.get("scriptpubkey", ""),
                    height=height if height > 0 else None,
                )
                utxos.append(utxo)

            logger.debug(f"Found {len(utxos)} UTXOs for {len(addresses)} addresses")

        except Exception as e:
            logger.error(f"Failed to fetch UTXOs: {e}")

        return utxos

    async def get_address_balance(self, address: str) -> int:
        """Get balance for an address in satoshis."""
        utxos = await self.get_utxos([address])
        balance = sum(utxo.value for utxo in utxos)
        logger.debug(f"Balance for {address}: {balance} sats")
        return balance

    async def broadcast_transaction(self, tx_hex: str) -> str:
        """
        Broadcast transaction via neutrino to the P2P network.

        Neutrino maintains P2P connections and can broadcast transactions
        directly to connected peers.
        """
        try:
            result = await self._api_call(
                "POST",
                "v1/tx/broadcast",
                data={"tx_hex": tx_hex},
            )
            txid = result.get("txid", "")
            logger.info(f"Broadcast transaction: {txid}")
            return txid

        except Exception as e:
            logger.error(f"Failed to broadcast transaction: {e}")
            raise ValueError(f"Broadcast failed: {e}") from e

    async def get_transaction(self, txid: str) -> Transaction | None:
        """
        Get transaction by txid.

        Note: Neutrino uses compact block filters (BIP158) and can only fetch
        transactions for addresses it has rescanned. It cannot fetch arbitrary
        transactions by txid alone. This method always returns None.

        For verification after broadcast, rely on UTXO checks with known addresses
        and block heights instead.
        """
        # Neutrino doesn't support fetching arbitrary transactions by txid
        # It can only work with UTXOs for known addresses via compact filters
        return None

    async def verify_tx_output(
        self,
        txid: str,
        vout: int,
        address: str,
        start_height: int | None = None,
    ) -> bool:
        """
        Verify that a specific transaction output exists using neutrino's UTXO endpoint.

        Uses GET /v1/utxo/{txid}/{vout}?address=...&start_height=... to check if
        the output exists. This works because neutrino uses compact block filters
        that can match on addresses.

        Args:
            txid: Transaction ID to verify
            vout: Output index to check
            address: The address that should own this output
            start_height: Block height hint for efficient scanning (recommended)

        Returns:
            True if the output exists, False otherwise
        """
        try:
            params: dict[str, str | int] = {"address": address}
            if start_height is not None:
                params["start_height"] = start_height

            result = await self._api_call(
                "GET",
                f"v1/utxo/{txid}/{vout}",
                params=params,
            )

            # If we got a response with unspent status, the output exists
            # Note: Even spent outputs confirm the transaction was broadcast
            if result is not None:
                logger.debug(
                    f"Verified tx output {txid}:{vout} exists "
                    f"(unspent={result.get('unspent', 'unknown')})"
                )
                return True

            return False

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                # Output not found
                logger.debug(f"Tx output {txid}:{vout} not found")
                return False
            logger.warning(f"Error verifying tx output {txid}:{vout}: {e}")
            return False
        except Exception as e:
            logger.warning(f"Error verifying tx output {txid}:{vout}: {e}")
            return False

    async def estimate_fee(self, target_blocks: int) -> float:
        """
        Estimate fee in sat/vbyte for target confirmation blocks.

        Neutrino does not support fee estimation - returns conservative defaults.
        Use can_estimate_fee() to check if reliable estimation is available.
        """
        # Neutrino cannot estimate fees - return conservative defaults
        if target_blocks <= 1:
            return 5.0
        elif target_blocks <= 3:
            return 2.0
        elif target_blocks <= 6:
            return 1.0
        else:
            return 1.0

    def can_estimate_fee(self) -> bool:
        """Neutrino cannot reliably estimate fees - requires full node."""
        return False

    def has_mempool_access(self) -> bool:
        """Neutrino cannot access mempool - only sees confirmed transactions.

        BIP157/158 compact block filters only match confirmed blocks.
        Unconfirmed transactions in the mempool are not visible to Neutrino.

        This means verify_tx_output() will return False for valid transactions
        that are in the mempool but not yet confirmed. Takers using Neutrino
        must use alternative verification strategies (e.g., trust maker ACKs,
        multi-maker broadcast, wait for confirmation).
        """
        return False

    async def get_block_height(self) -> int:
        """Get current blockchain height from neutrino."""
        try:
            result = await self._api_call("GET", "v1/status")
            height = result.get("block_height", 0)
            logger.debug(f"Current block height: {height}")
            return height

        except Exception as e:
            logger.error(f"Failed to fetch block height: {e}")
            raise

    async def get_block_time(self, block_height: int) -> int:
        """Get block time (unix timestamp) for given height."""
        try:
            result = await self._api_call(
                "GET",
                f"v1/block/{block_height}/header",
            )
            timestamp = result.get("timestamp", 0)
            logger.debug(f"Block {block_height} timestamp: {timestamp}")
            return timestamp

        except Exception as e:
            logger.error(f"Failed to fetch block time for height {block_height}: {e}")
            raise

    async def get_block_hash(self, block_height: int) -> str:
        """Get block hash for given height."""
        try:
            result = await self._api_call(
                "GET",
                f"v1/block/{block_height}/header",
            )
            block_hash = result.get("hash", "")
            logger.debug(f"Block hash for height {block_height}: {block_hash}")
            return block_hash

        except Exception as e:
            logger.error(f"Failed to fetch block hash for height {block_height}: {e}")
            raise

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        """Get a specific UTXO from the blockchain.
        Returns None if the UTXO does not exist or has been spent."""
        # Neutrino uses compact block filters and cannot perform arbitrary
        # UTXO lookups without the address. The API endpoint v1/utxo/{txid}/{vout}
        # requires the 'address' parameter to scan filter matches.
        #
        # If we don't have the address, we can't look it up.
        # Callers should use verify_utxo_with_metadata() or verify_bonds() instead.
        return None

    async def verify_bonds(
        self,
        bonds: list[BondVerificationRequest],
    ) -> list[BondVerificationResult]:
        """Verify fidelity bond UTXOs using compact block filter address scanning.

        Since the neutrino backend cannot do arbitrary UTXO lookups (get_utxo returns
        None), this method uses the pre-computed bond address from each request to scan
        the UTXO set via the neutrino-api's address-based endpoint.

        For each bond:
        1. Use the pre-computed P2WSH address (derived from utxo_pub + locktime)
        2. Query ``v1/utxo/{txid}/{vout}?address={addr}&start_height={scan_start_height}``
        3. Parse the response to determine value, confirmations, and block time

        Uses scan_start_height (defaulting to the network's minimum valid blockheight)
        instead of scanning from genesis. This is safe because fidelity bonds can only
        exist after SegWit activation, and dramatically faster on long chains.
        """
        if not bonds:
            return []

        current_height = await self.get_block_height()

        # ``verify_bonds()`` can be called before the first wallet sync has run
        # on this backend instance (e.g. jmwalletd taker flow where wallet sync
        # may use a different backend object). In that case ``_scan_start_height``
        # is still the constructor default (often 0 on signet/regtest), which can
        # trigger very deep scans and slow responses. Resolve it lazily from tip.
        resolved_scan_start = await self._resolve_scan_start_height(current_height)
        self._scan_start_height = resolved_scan_start

        semaphore = asyncio.Semaphore(10)

        async def _verify_one(bond: BondVerificationRequest) -> BondVerificationResult:
            async with semaphore:
                try:
                    # Use the neutrino-api single-UTXO endpoint with address hint
                    # Start from _scan_start_height instead of genesis for performance.
                    # Bonds require SegWit (P2WSH) so they cannot exist before
                    # the network's minimum valid blockheight.
                    response = await self._api_call(
                        "GET",
                        f"v1/utxo/{bond.txid}/{bond.vout}",
                        params={
                            "address": bond.address,
                            "start_height": resolved_scan_start,
                        },
                    )

                    if response is None:
                        return BondVerificationResult(
                            txid=bond.txid,
                            vout=bond.vout,
                            value=0,
                            confirmations=0,
                            block_time=0,
                            valid=False,
                            error="UTXO not found",
                        )

                    if not response.get("unspent", False):
                        return BondVerificationResult(
                            txid=bond.txid,
                            vout=bond.vout,
                            value=0,
                            confirmations=0,
                            block_time=0,
                            valid=False,
                            error="UTXO spent",
                        )

                    value = response.get("value", 0)
                    block_height = response.get("block_height", 0)
                    confirmations = (
                        max(0, current_height - block_height + 1) if block_height > 0 else 0
                    )

                    if confirmations <= 0:
                        return BondVerificationResult(
                            txid=bond.txid,
                            vout=bond.vout,
                            value=value,
                            confirmations=0,
                            block_time=0,
                            valid=False,
                            error="UTXO unconfirmed",
                        )

                    # Get block time for confirmation timestamp
                    block_time = await self.get_block_time(block_height)

                    return BondVerificationResult(
                        txid=bond.txid,
                        vout=bond.vout,
                        value=value,
                        confirmations=confirmations,
                        block_time=block_time,
                        valid=True,
                    )
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        return BondVerificationResult(
                            txid=bond.txid,
                            vout=bond.vout,
                            value=0,
                            confirmations=0,
                            block_time=0,
                            valid=False,
                            error="UTXO not found",
                        )
                    logger.warning(
                        "Bond verification failed for {}:{}: {}",
                        bond.txid,
                        bond.vout,
                        e,
                    )
                    return BondVerificationResult(
                        txid=bond.txid,
                        vout=bond.vout,
                        value=0,
                        confirmations=0,
                        block_time=0,
                        valid=False,
                        error=str(e),
                    )
                except Exception as e:
                    logger.warning(
                        "Bond verification failed for {}:{}: {}",
                        bond.txid,
                        bond.vout,
                        e,
                    )
                    return BondVerificationResult(
                        txid=bond.txid,
                        vout=bond.vout,
                        value=0,
                        confirmations=0,
                        block_time=0,
                        valid=False,
                        error=str(e),
                    )

        results = await asyncio.gather(*[_verify_one(b) for b in bonds])
        logger.debug(
            "Verified {} bonds via neutrino: {} valid, {} invalid",
            len(bonds),
            sum(1 for r in results if r.valid),
            sum(1 for r in results if not r.valid),
        )
        return list(results)

    def requires_neutrino_metadata(self) -> bool:
        """
        Neutrino backend requires metadata for arbitrary UTXO verification.

        Without scriptPubKey and blockheight hints, Neutrino cannot verify
        UTXOs that it hasn't been watching from the start.

        Returns:
            True - Neutrino always requires metadata for counterparty UTXOs
        """
        return True

    def can_provide_neutrino_metadata(self) -> bool:
        """
        Neutrino backend CAN provide metadata for its own wallet UTXOs.

        A neutrino maker knows its own scriptpubkeys (derived from the wallet)
        and block heights (from its own transaction history). This metadata is
        included in !ioauth responses so that neutrino takers can verify the
        maker's UTXOs via compact block filters.

        Note: This is distinct from requires_neutrino_metadata(), which asks
        whether this backend needs metadata FROM counterparties to verify THEIR
        UTXOs. A neutrino backend both requires metadata from others AND can
        provide metadata about its own UTXOs.

        Returns:
            True - Neutrino can provide scriptpubkey + blockheight for own UTXOs
        """
        return True

    async def verify_utxo_with_metadata(
        self,
        txid: str,
        vout: int,
        scriptpubkey: str,
        blockheight: int,
    ) -> UTXOVerificationResult:
        """
        Verify a UTXO using provided metadata (neutrino_compat feature).

        This is the key method that enables Neutrino light clients to verify
        counterparty UTXOs in CoinJoin without arbitrary blockchain queries.

        Uses the neutrino-api v0.4 UTXO check endpoint which requires:
        - address: The Bitcoin address that owns the UTXO (derived from scriptPubKey)
        - start_height: Block height to start scanning from (for efficiency)

        The API scans from start_height to chain tip using compact block filters
        to determine if the UTXO exists and whether it has been spent.

        Security: Validates blockheight to prevent rescan abuse attacks where
        malicious peers provide very low blockheights to trigger expensive rescans.

        Args:
            txid: Transaction ID
            vout: Output index
            scriptpubkey: Expected scriptPubKey (hex) - used to derive address
            blockheight: Block height where UTXO was confirmed - scan start hint

        Returns:
            UTXOVerificationResult with verification status and UTXO data
        """
        # Security: Validate blockheight to prevent rescan abuse
        tip_height = await self.get_block_height()

        if blockheight < self._min_valid_blockheight:
            return UTXOVerificationResult(
                valid=False,
                error=f"Blockheight {blockheight} is below minimum valid height "
                f"{self._min_valid_blockheight} for {self.network}",
            )

        if blockheight > tip_height:
            return UTXOVerificationResult(
                valid=False,
                error=f"Blockheight {blockheight} is in the future (tip: {tip_height})",
            )

        # Limit rescan depth to prevent DoS
        rescan_depth = tip_height - blockheight
        if rescan_depth > self._max_rescan_depth:
            return UTXOVerificationResult(
                valid=False,
                error=f"Rescan depth {rescan_depth} exceeds max {self._max_rescan_depth}. "
                f"UTXO too old for efficient verification.",
            )

        logger.debug(
            f"Verifying UTXO {txid}:{vout} with metadata "
            f"(scriptpubkey={scriptpubkey[:20]}..., blockheight={blockheight})"
        )

        # Step 1: Derive address from scriptPubKey
        # The neutrino-api v0.4 requires the address for UTXO lookup
        address = self._scriptpubkey_to_address(scriptpubkey)
        if not address:
            return UTXOVerificationResult(
                valid=False,
                error=f"Could not derive address from scriptPubKey: {scriptpubkey[:40]}...",
            )

        logger.debug(f"Derived address {address} from scriptPubKey")

        try:
            # Step 2: Query the specific UTXO using the v0.4 API
            # GET /v1/utxo/{txid}/{vout}?address=...&start_height=...
            #
            # The start_height parameter is critical for performance:
            # - Scanning 1 block takes ~0.01s
            # - Scanning 100 blocks takes ~0.5s
            # - Scanning 10,000+ blocks can take minutes
            #
            # We use blockheight - 1 as a safety margin in case of reorgs
            start_height = max(0, blockheight - 1)

            result = await self._api_call(
                "GET",
                f"v1/utxo/{txid}/{vout}",
                params={"address": address, "start_height": start_height},
            )

            # Check if UTXO is unspent
            if not result.get("unspent", False):
                spending_txid = result.get("spending_txid", "unknown")
                spending_height = result.get("spending_height", "unknown")
                return UTXOVerificationResult(
                    valid=False,
                    error=f"UTXO has been spent in tx {spending_txid} at height {spending_height}",
                )

            # Step 3: Verify scriptPubKey matches
            actual_scriptpubkey = result.get("scriptpubkey", "")
            scriptpubkey_matches = actual_scriptpubkey.lower() == scriptpubkey.lower()

            if not scriptpubkey_matches:
                return UTXOVerificationResult(
                    valid=False,
                    value=result.get("value", 0),
                    error=f"ScriptPubKey mismatch: expected {scriptpubkey[:20]}..., "
                    f"got {actual_scriptpubkey[:20]}...",
                    scriptpubkey_matches=False,
                )

            # Step 4: Calculate confirmations
            tip_height = await self.get_block_height()
            # The blockheight parameter is the confirmation height hint from the peer
            confirmations = tip_height - blockheight + 1 if blockheight > 0 else 0

            logger.info(
                f"UTXO {txid}:{vout} verified: value={result.get('value', 0)}, "
                f"confirmations={confirmations}"
            )

            return UTXOVerificationResult(
                valid=True,
                value=result.get("value", 0),
                confirmations=confirmations,
                scriptpubkey_matches=True,
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return UTXOVerificationResult(
                    valid=False,
                    error="UTXO not found - may not exist or address derivation failed",
                )
            return UTXOVerificationResult(
                valid=False,
                error=f"UTXO query failed: {e}",
            )
        except Exception as e:
            return UTXOVerificationResult(
                valid=False,
                error=f"Verification failed: {e}",
            )

    def _scriptpubkey_to_address(self, scriptpubkey: str) -> str | None:
        """Convert a scriptPubKey hex string to a Bitcoin address."""
        from bitcointx import ChainParams
        from bitcointx.core.script import CScript
        from bitcointx.wallet import CCoinAddress as _CCoinAddress
        from bitcointx.wallet import CCoinAddressError

        network_to_chain = {
            "mainnet": "bitcoin",
            "testnet": "bitcoin/testnet",
            "signet": "bitcoin/signet",
            "regtest": "bitcoin/regtest",
        }
        chain = network_to_chain.get(self.network, "bitcoin")
        try:
            with ChainParams(chain):
                return str(_CCoinAddress.from_scriptPubKey(CScript(bytes.fromhex(scriptpubkey))))
        except (CCoinAddressError, ValueError) as e:
            logger.warning(f"Failed to convert scriptPubKey to address: {e}")
            return None

    async def get_filter_header(self, block_height: int) -> str:
        """
        Get compact block filter header for given height.

        BIP157 filter headers form a chain for validation.
        """
        try:
            result = await self._api_call(
                "GET",
                f"v1/block/{block_height}/filter_header",
            )
            return result.get("filter_header", "")

        except Exception as e:
            logger.error(f"Failed to fetch filter header for height {block_height}: {e}")
            raise

    async def get_connected_peers(self) -> list[dict[str, Any]]:
        """Get list of connected P2P peers."""
        try:
            result = await self._api_call("GET", "v1/peers")
            return result.get("peers", [])

        except Exception as e:
            logger.warning(f"Failed to fetch peers: {e}")
            return []

    async def rescan_from_height(
        self,
        start_height: int,
        addresses: list[str] | None = None,
        outpoints: list[tuple[str, int]] | None = None,
    ) -> None:
        """
        Rescan blockchain from a specific height for addresses.

        This triggers neutrino to re-check compact block filters from
        the specified height for relevant transactions.

        Uses the neutrino-api v0.4 rescan endpoint:
        POST /v1/rescan with {"start_height": N, "addresses": [...]}

        Note: The v0.4 API only supports address-based rescans.
        Outpoints are tracked via address watches instead.

        Args:
            start_height: Block height to start rescan from
            addresses: List of addresses to scan for (required for v0.4)
            outpoints: List of (txid, vout) outpoints - not directly supported,
                      will be ignored (use add_watch_outpoint instead)

        Raises:
            ValueError: If start_height is invalid or rescan depth exceeds limits
        """
        if not addresses:
            logger.warning("Rescan called without addresses - nothing to scan")
            return

        # Security: Validate start_height to prevent rescan abuse
        if start_height < self._min_valid_blockheight:
            raise ValueError(
                f"start_height {start_height} is below minimum valid height "
                f"{self._min_valid_blockheight} for {self.network}"
            )

        tip_height = await self.get_block_height()
        if start_height > tip_height:
            raise ValueError(f"start_height {start_height} is in the future (tip: {tip_height})")

        rescan_depth = tip_height - start_height
        if rescan_depth > self._max_rescan_depth:
            raise ValueError(
                f"Rescan depth {rescan_depth} exceeds maximum {self._max_rescan_depth} blocks"
            )

        # Track addresses locally (with limit check)
        for addr in addresses:
            await self.add_watch_address(addr)

        # Note: v0.4 API doesn't support outpoints in rescan
        if outpoints:
            logger.debug(
                "Outpoints parameter ignored in v0.4 rescan API. "
                "Use address-based watching instead."
            )
            for txid, vout in outpoints:
                self._watched_outpoints.add((txid, vout))

        try:
            await self._api_call(
                "POST",
                "v1/rescan",
                data={
                    "start_height": start_height,
                    "addresses": addresses,
                },
            )
            logger.info(f"Started rescan from height {start_height} for {len(addresses)} addresses")

        except Exception as e:
            logger.error(f"Failed to start rescan: {e}")
            raise

    async def close(self) -> None:
        """Close the HTTP client connection and reset so the backend can be reused."""
        await self.client.aclose()
        # Re-create a fresh client so this instance is usable again if the
        # wallet service is restarted (e.g. maker stop -> start in jmwalletd).
        self.client = self._build_http_client()
        self._watched_addresses = set()
        self._watched_outpoints = set()
        self._filter_header_tip = 0
        self._synced = False
        self._initial_rescan_done = False
        self._initial_rescan_started = False
        self._last_rescan_height = 0
        self._rescan_in_progress = False
        self._just_rescanned = False
        self._server_capabilities = ServerCapabilities()


class NeutrinoConfig:
    """
    Configuration for running a neutrino daemon.

    This configuration can be used to start a neutrino process
    programmatically or generate a config file.
    """

    def __init__(
        self,
        network: str = "mainnet",
        data_dir: str = "/data/neutrino",
        listen_port: int = 8334,
        peers: list[str] | None = None,
        tor_socks: str | None = None,
        clearnet_initial_sync: bool = True,
        prefetch_filters: bool = True,
        prefetch_lookback_blocks: int = 105120,
    ):
        """
        Initialize neutrino configuration.

        Args:
            network: Bitcoin network (mainnet, testnet, regtest, signet)
            data_dir: Directory for neutrino data
            listen_port: Port for REST API
            peers: List of peer addresses to connect to
            tor_socks: Tor SOCKS5 proxy address (e.g., "127.0.0.1:9050")
            clearnet_initial_sync: Sync headers over clearnet before switching
                to Tor. Safe because headers are public deterministic data.
                Typically ~2x faster than Tor for initial header sync.
                Default: True.
            prefetch_filters: Enable background prefetch of compact block
                filters. Enabled by default because jm-wallet info scans
                these filters anyway, so prefetching saves time. With the
                default lookback of ~2 years, takes ~3 hours on clearnet
                and ~3GB disk on mainnet. Default: True.
            prefetch_lookback_blocks: When prefetch is enabled, only prefetch
                filters for this many recent blocks. 0 = prefetch all from
                genesis. Default: 105120 (~2 years).
        """
        self.network = network
        self.data_dir = data_dir
        self.listen_port = listen_port
        self.peers = peers or []
        self.tor_socks = tor_socks
        self.clearnet_initial_sync = clearnet_initial_sync
        self.prefetch_filters = prefetch_filters
        self.prefetch_lookback_blocks = prefetch_lookback_blocks

    def get_chain_params(self) -> dict[str, Any]:
        """Get chain-specific parameters."""
        params = {
            "mainnet": {
                "default_port": 8333,
                "dns_seeds": [
                    "seed.bitcoin.sipa.be",
                    "dnsseed.bluematt.me",
                    "dnsseed.bitcoin.dashjr.org",
                    "seed.bitcoinstats.com",
                    "seed.bitcoin.jonasschnelli.ch",
                    "seed.btc.petertodd.net",
                ],
            },
            "testnet": {
                "default_port": 18333,
                "dns_seeds": [
                    "testnet-seed.bitcoin.jonasschnelli.ch",
                    "seed.tbtc.petertodd.net",
                    "testnet-seed.bluematt.me",
                ],
            },
            "signet": {
                "default_port": 38333,
                "dns_seeds": [
                    "seed.signet.bitcoin.sprovoost.nl",
                ],
            },
            "regtest": {
                "default_port": 18444,
                "dns_seeds": [],
            },
        }
        return params.get(self.network, params["mainnet"])

    def to_args(self) -> list[str]:
        """Generate command-line arguments for neutrino daemon."""
        args = [
            f"--datadir={self.data_dir}",
            f"--{self.network}",
            f"--restlisten=0.0.0.0:{self.listen_port}",
        ]

        if self.tor_socks:
            args.append(f"--proxy={self.tor_socks}")

        for peer in self.peers:
            args.append(f"--addpeer={peer}")

        # Clearnet initial sync: safe because headers are public data
        if self.clearnet_initial_sync:
            args.append("--clearnet-initial-sync=true")
        else:
            args.append("--clearnet-initial-sync=false")

        # Filter prefetch (disabled by default to save ~15GB on mainnet)
        if self.prefetch_filters:
            args.append("--prefetchfilters=true")
            if self.prefetch_lookback_blocks > 0:
                args.append(f"--prefetchlookback={self.prefetch_lookback_blocks}")
        else:
            args.append("--prefetchfilters=false")

        return args

    def to_env(self) -> dict[str, str]:
        """Generate environment variables for neutrino daemon (Docker)."""
        env: dict[str, str] = {
            "NETWORK": self.network,
            "DATA_DIR": self.data_dir,
            "LISTEN_ADDR": f"0.0.0.0:{self.listen_port}",
            "CLEARNET_INITIAL_SYNC": str(self.clearnet_initial_sync).lower(),
            "PREFETCH_FILTERS": str(self.prefetch_filters).lower(),
        }

        if self.tor_socks:
            env["TOR_PROXY"] = self.tor_socks

        if self.peers:
            env["ADD_PEERS"] = ",".join(self.peers)

        if self.prefetch_filters and self.prefetch_lookback_blocks > 0:
            env["PREFETCH_LOOKBACK"] = str(self.prefetch_lookback_blocks)

        return env
