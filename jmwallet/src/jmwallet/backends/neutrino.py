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
from typing import Any

import httpx
from loguru import logger

from jmwallet.backends.base import (
    UTXO,
    BlockchainBackend,
    Transaction,
    UTXOVerificationResult,
)


class NeutrinoBackend(BlockchainBackend):
    """
    Blockchain backend using Neutrino light client.

    Neutrino is a privacy-preserving Bitcoin light client that uses
    BIP157/BIP158 compact block filters instead of traditional SPV.

    Communication with the neutrino daemon is via REST API.
    The neutrino daemon should be running alongside this client.
    """

    def __init__(
        self,
        neutrino_url: str = "http://127.0.0.1:8334",
        network: str = "mainnet",
        connect_peers: list[str] | None = None,
        data_dir: str = "/data/neutrino",
    ):
        """
        Initialize Neutrino backend.

        Args:
            neutrino_url: URL of the neutrino REST API (default port 8334)
            network: Bitcoin network (mainnet, testnet, regtest, signet)
            connect_peers: List of peer addresses to connect to (optional)
            data_dir: Directory for neutrino data (headers, filters)
        """
        self.neutrino_url = neutrino_url.rstrip("/")
        self.network = network
        self.connect_peers = connect_peers or []
        self.data_dir = data_dir
        self.client = httpx.AsyncClient(timeout=60.0)

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

    async def wait_for_sync(self, timeout: float = 300.0) -> bool:
        """
        Wait for neutrino to sync block headers and filters.

        Args:
            timeout: Maximum time to wait in seconds

        Returns:
            True if synced, False if timeout
        """
        start_time = asyncio.get_event_loop().time()

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

    async def get_utxos(self, addresses: list[str]) -> list[UTXO]:
        """
        Get UTXOs for given addresses using neutrino's rescan capability.

        Neutrino will scan the blockchain using compact block filters
        to find transactions relevant to the watched addresses.

        On first call, triggers a full blockchain rescan from genesis to ensure
        all historical UTXOs are found (critical for wallets funded before neutrino started).

        After initial rescan, automatically rescans if new blocks have arrived
        to detect transactions that occurred after the last scan.
        """
        utxos: list[UTXO] = []

        # Add addresses to watch list
        for address in addresses:
            await self.add_watch_address(address)

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
            logger.info(
                f"Performing initial blockchain rescan for {len(self._watched_addresses)} "
                "watched addresses (this may take a moment)..."
            )
            try:
                # Trigger rescan from block 0 for all watched addresses
                await self._api_call(
                    "POST",
                    "v1/rescan",
                    data={
                        "addresses": list(self._watched_addresses),
                        "start_height": 0,
                    },
                )
                # Wait for rescan to complete (neutrino processes this asynchronously)
                # On regtest with ~3000 blocks, this typically takes 5-10 seconds
                await asyncio.sleep(10.0)
                self._initial_rescan_done = True
                self._last_rescan_height = current_height
                self._rescan_in_progress = False
                self._just_rescanned = True
                logger.info("Initial blockchain rescan completed")
            except Exception as e:
                logger.warning(f"Initial rescan failed (will retry on next sync): {e}")
                self._rescan_in_progress = False
        elif current_height > self._last_rescan_height and not self._rescan_in_progress:
            # New blocks have arrived since last rescan - need to scan them
            # This is critical for finding CoinJoin outputs that were just confirmed
            # We rescan ALL watched addresses, not just the ones in the current query,
            # because wallet sync happens mixdepth by mixdepth and we need to find
            # outputs to any of our addresses
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
                # Wait for rescan to complete
                # NOTE: The rescan is asynchronous - neutrino needs time to:
                # 1. Match block filters
                # 2. Download full blocks that match
                # 3. Extract and index UTXOs
                # We wait 7 seconds to allow time for indexing to complete
                await asyncio.sleep(7.0)

                self._last_rescan_height = current_height
                self._rescan_in_progress = False
                self._just_rescanned = True
                logger.info(
                    f"Incremental rescan completed from block {start_height} to {current_height}"
                )
            except Exception as e:
                logger.warning(f"Incremental rescan failed: {e}")
                self._rescan_in_progress = False
        elif self._rescan_in_progress:
            # A rescan was just triggered by a previous get_utxos call in this batch
            # Wait a bit for it to complete, but don't wait the full 7 seconds
            logger.debug("Rescan in progress from previous query, waiting briefly...")
            await asyncio.sleep(1.0)
        else:
            # No new blocks, just wait for filter matching / async UTXO lookups
            await asyncio.sleep(0.5)

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
        # Callers should use verify_utxo_with_metadata() instead.
        return None

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
        Neutrino backend cannot reliably provide metadata for all UTXOs.

        Light clients can only provide metadata for UTXOs they've been watching.
        They cannot provide metadata for arbitrary addresses like full nodes can.

        Returns:
            False - Neutrino cannot provide metadata for arbitrary UTXOs
        """
        return False

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
        """
        Convert scriptPubKey to address for watch list.

        Supports common script types:
        - P2WPKH: 0014<20-byte-hash> -> bc1q...
        - P2WSH: 0020<32-byte-hash> -> bc1q...
        - P2PKH: 76a914<20-byte-hash>88ac -> 1...
        - P2SH: a914<20-byte-hash>87 -> 3...

        Args:
            scriptpubkey: Hex-encoded scriptPubKey

        Returns:
            Bitcoin address or None if conversion fails
        """
        try:
            script_bytes = bytes.fromhex(scriptpubkey)

            # P2WPKH: OP_0 <20 bytes>
            if len(script_bytes) == 22 and script_bytes[0] == 0x00 and script_bytes[1] == 0x14:
                # Use bech32 encoding
                return self._encode_bech32_address(script_bytes[2:], 0)

            # P2WSH: OP_0 <32 bytes>
            if len(script_bytes) == 34 and script_bytes[0] == 0x00 and script_bytes[1] == 0x20:
                return self._encode_bech32_address(script_bytes[2:], 0)

            # P2PKH: OP_DUP OP_HASH160 <20 bytes> OP_EQUALVERIFY OP_CHECKSIG
            if (
                len(script_bytes) == 25
                and script_bytes[0] == 0x76
                and script_bytes[1] == 0xA9
                and script_bytes[2] == 0x14
                and script_bytes[23] == 0x88
                and script_bytes[24] == 0xAC
            ):
                return self._encode_base58check_address(script_bytes[3:23], 0x00)

            # P2SH: OP_HASH160 <20 bytes> OP_EQUAL
            if (
                len(script_bytes) == 23
                and script_bytes[0] == 0xA9
                and script_bytes[1] == 0x14
                and script_bytes[22] == 0x87
            ):
                return self._encode_base58check_address(script_bytes[2:22], 0x05)

            logger.warning(f"Unknown scriptPubKey format: {scriptpubkey[:20]}...")
            return None

        except Exception as e:
            logger.warning(f"Failed to convert scriptPubKey to address: {e}")
            return None

    def _encode_bech32_address(self, witness_program: bytes, witness_version: int) -> str:
        """Encode witness program as bech32 address."""
        from bech32 import encode as bech32_encode

        hrp = "bc" if self.network == "mainnet" else "bcrt" if self.network == "regtest" else "tb"
        result = bech32_encode(hrp, witness_version, witness_program)
        if result is None:
            raise ValueError(
                f"Failed to encode bech32 address: "
                f"version={witness_version}, program={witness_program.hex()}"
            )
        return result

    def _encode_base58check_address(self, payload: bytes, version: int) -> str:
        """Encode payload as base58check address."""
        import hashlib

        versioned = bytes([version]) + payload
        checksum = hashlib.sha256(hashlib.sha256(versioned).digest()).digest()[:4]
        data = versioned + checksum

        ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"  # noqa: N806
        n = int.from_bytes(data, "big")
        result = ""
        while n > 0:
            n, r = divmod(n, 58)
            result = ALPHABET[r] + result

        # Add leading zeros
        for byte in data:
            if byte == 0:
                result = "1" + result
            else:
                break

        return result

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
        """Close the HTTP client connection."""
        await self.client.aclose()


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
    ):
        """
        Initialize neutrino configuration.

        Args:
            network: Bitcoin network (mainnet, testnet, regtest, signet)
            data_dir: Directory for neutrino data
            listen_port: Port for REST API
            peers: List of peer addresses to connect to
            tor_socks: Tor SOCKS5 proxy address (e.g., "127.0.0.1:9050")
        """
        self.network = network
        self.data_dir = data_dir
        self.listen_port = listen_port
        self.peers = peers or []
        self.tor_socks = tor_socks

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
            args.append(f"--connect={peer}")

        return args
