"""
Bitcoin Core RPC blockchain backend.
Uses RPC calls but NOT wallet functionality (no BDB dependency).
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import random
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

import httpx
from jmcore.bitcoin import btc_to_sats
from loguru import logger

from jmwallet.backends.base import (
    UTXO,
    BlockchainBackend,
    BondVerificationRequest,
    BondVerificationResult,
    Transaction,
)

# Timeout for regular RPC calls (seconds)
DEFAULT_RPC_TIMEOUT = 30.0

# Timeout for scantxoutset calls - mainnet scans can take 90+ seconds
SCAN_RPC_TIMEOUT = 300.0  # 5 minutes

# Maximum retries for scantxoutset when another scan is in progress
# Mainnet UTXO scans can take 90+ seconds, so we need many retries
SCAN_MAX_RETRIES = 30
SCAN_BASE_DELAY = 0.5  # Base delay in seconds for exponential backoff

# Polling interval for scan status checks (mainnet scans take ~90 seconds)
SCAN_STATUS_POLL_INTERVAL = 10.0  # seconds

# Environment variable to enable sensitive logging (descriptors, addresses, etc.)
# WARNING: Enabling this will log wallet descriptors and addresses to the log
SENSITIVE_LOGGING = os.environ.get("SENSITIVE_LOGGING", "").lower() in ("1", "true", "yes")


class BitcoinCoreBackend(BlockchainBackend):
    """
    Blockchain backend using Bitcoin Core RPC.
    Does NOT use Bitcoin Core wallet (avoids BDB issues).
    Uses scantxoutset and other non-wallet RPC methods.
    """

    supports_descriptor_scan: bool = True

    def __init__(
        self,
        rpc_url: str = "http://127.0.0.1:18443",
        rpc_user: str = "rpcuser",
        rpc_password: str = "rpcpassword",
        scan_timeout: float = SCAN_RPC_TIMEOUT,
    ):
        self.rpc_url = rpc_url.rstrip("/")
        self.rpc_user = rpc_user
        self.rpc_password = rpc_password
        self.scan_timeout = scan_timeout

        parsed = urlparse(self.rpc_url)
        hostname = parsed.hostname or ""
        is_local = hostname in ("127.0.0.1", "localhost", "::1") or hostname.endswith(".onion")
        if not is_local:
            try:
                is_local = ipaddress.ip_address(hostname).is_loopback
            except (ValueError, TypeError):
                pass
        if parsed.scheme != "https" and not is_local:
            logger.warning(
                "Bitcoin Core RPC URL is remote and non-HTTPS; "
                "RPC credentials may be exposed in transit"
            )

        # Client for regular RPC calls
        self.client = httpx.AsyncClient(timeout=DEFAULT_RPC_TIMEOUT, auth=(rpc_user, rpc_password))
        # Separate client for long-running scans
        self._scan_client = httpx.AsyncClient(timeout=scan_timeout, auth=(rpc_user, rpc_password))
        self._request_id = 0

    async def _rpc_call(
        self,
        method: str,
        params: list | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> Any:
        """
        Make an RPC call to Bitcoin Core.

        Args:
            method: RPC method name
            params: Method parameters
            client: Optional httpx client (uses default client if not provided)

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

        try:
            response = await use_client.post(self.rpc_url, json=payload)
            response.raise_for_status()
            data = response.json()

            if "error" in data and data["error"]:
                error_info = data["error"]
                error_code = error_info.get("code", "unknown")
                error_msg = error_info.get("message", str(error_info))
                raise ValueError(f"RPC error {error_code}: {error_msg}")

            return data.get("result")

        except httpx.TimeoutException as e:
            logger.error(f"RPC call timed out: {method} - {e}")
            raise
        except httpx.HTTPError as e:
            logger.error(f"RPC call failed: {method} - {e}")
            raise

    async def _rpc_batch(
        self,
        requests: list[tuple[str, list]],
        client: httpx.AsyncClient | None = None,
    ) -> list[Any]:
        """Make a JSON-RPC batch call to Bitcoin Core.

        Sends multiple RPC calls in a single HTTP request. This is dramatically
        more efficient than individual calls when verifying many UTXOs -- e.g.
        100 gettxout calls become 1 HTTP round-trip instead of 100.

        Args:
            requests: List of (method, params) tuples
            client: Optional httpx client (uses default client if not provided)

        Returns:
            List of results in the same order as requests. Failed calls return None.

        Raises:
            httpx.HTTPError: On connection/timeout errors
        """
        if not requests:
            return []

        batch_payload = []
        for i, (method, params) in enumerate(requests):
            batch_payload.append(
                {
                    "jsonrpc": "2.0",
                    "id": i,
                    "method": method,
                    "params": params,
                }
            )

        use_client = client or self.client

        try:
            response = await use_client.post(self.rpc_url, json=batch_payload)
            response.raise_for_status()
            data = response.json()
        except httpx.TimeoutException as e:
            logger.error(f"RPC batch call timed out ({len(requests)} requests) - {e}")
            raise
        except httpx.HTTPError as e:
            logger.error(f"RPC batch call failed ({len(requests)} requests) - {e}")
            raise

        # Bitcoin Core returns results as a list, but order is NOT guaranteed
        # to match request order. Index by id.
        results: list[Any] = [None] * len(requests)
        for item in data:
            idx = item.get("id")
            if idx is not None and 0 <= idx < len(requests):
                if item.get("error"):
                    error_info = item["error"]
                    error_code = error_info.get("code", "unknown")
                    error_msg = error_info.get("message", str(error_info))
                    logger.debug(
                        "RPC batch item %d (%s) error %s: %s",
                        idx,
                        requests[idx][0],
                        error_code,
                        error_msg,
                    )
                    results[idx] = None
                else:
                    results[idx] = item.get("result")

        return results

    async def _scantxoutset_with_retry(
        self, descriptors: Sequence[str | dict[str, Any]]
    ) -> dict[str, Any] | None:
        """
        Execute scantxoutset with retry logic for handling concurrent scan conflicts.

        Bitcoin Core only allows one scantxoutset at a time. This method:
        1. Checks if a scan is already in progress
        2. If so, waits for it to complete (via status polling) before starting ours
        3. Starts our scan with extended timeout for mainnet

        Args:
            descriptors: List of output descriptors to scan for. Can be:
                - Simple strings: "addr(bc1q...)"
                - Dicts with range: {"desc": "wpkh([fp/84'/0'/0'/0/*)", "range": [0, 999]}

        Returns:
            Scan result dict or None if all retries failed
        """
        for attempt in range(SCAN_MAX_RETRIES):
            try:
                # First check if a scan is already running
                status = await self._rpc_call("scantxoutset", ["status"])
                if status is not None:
                    # A scan is in progress - wait for it
                    # Bitcoin Core returns progress as 0-100, not 0-1
                    progress = status.get("progress", 0) / 100.0
                    logger.debug(
                        f"Another scan in progress ({progress:.1%}), waiting... "
                        f"(attempt {attempt + 1}/{SCAN_MAX_RETRIES})"
                    )
                    if attempt < SCAN_MAX_RETRIES - 1:
                        await asyncio.sleep(SCAN_STATUS_POLL_INTERVAL)
                        continue

                # Start our scan with extended timeout
                logger.debug(f"Starting UTXO scan for {len(descriptors)} descriptor(s)...")
                if SENSITIVE_LOGGING:
                    logger.debug(f"Descriptors for scan: {descriptors}")
                result = await self._rpc_call(
                    "scantxoutset", ["start", descriptors], client=self._scan_client
                )
                if result:
                    unspent_count = len(result.get("unspents", []))
                    total_amount = result.get("total_amount", 0)
                    logger.debug(
                        f"Scan completed: found {unspent_count} UTXOs, total {total_amount:.8f} BTC"
                    )
                    if SENSITIVE_LOGGING and unspent_count > 0:
                        logger.debug(f"Scan result: {result}")
                return result

            except ValueError as e:
                error_str = str(e)
                # Check for "scan already in progress" error (code -8)
                if "code': -8" in error_str or "Scan already in progress" in error_str:
                    if attempt < SCAN_MAX_RETRIES - 1:
                        delay = SCAN_BASE_DELAY * (2**attempt) + random.uniform(0, 0.5)
                        logger.debug(
                            f"Scan in progress (RPC error), retrying in {delay:.2f}s "
                            f"(attempt {attempt + 1}/{SCAN_MAX_RETRIES})"
                        )
                        await asyncio.sleep(delay)
                        continue
                    else:
                        logger.warning(
                            f"Max retries ({SCAN_MAX_RETRIES}) exceeded waiting for scan slot"
                        )
                        return None
                else:
                    # Other RPC errors - log and re-raise
                    logger.error(f"scantxoutset RPC error: {error_str}")
                    raise

            except httpx.TimeoutException:
                # Timeout during scan - this is a real failure on mainnet
                logger.error(
                    f"scantxoutset timed out after {self.scan_timeout}s. "
                    "Try increasing scan_timeout for mainnet."
                )
                return None

            except Exception as e:
                logger.error(f"Unexpected error during scantxoutset: {type(e).__name__}: {e}")
                raise

        logger.warning(f"scantxoutset failed after {SCAN_MAX_RETRIES} attempts")
        return None

    async def get_utxos(self, addresses: list[str]) -> list[UTXO]:
        utxos: list[UTXO] = []
        if not addresses:
            return utxos

        # Get tip height once for confirmation calculation
        try:
            tip_height = await self.get_block_height()
        except Exception as e:
            logger.error(f"Failed to get block height for UTXO scan: {e}")
            return utxos

        # Process in batches to avoid huge RPC requests
        batch_size: int = 1000
        for i in range(0, len(addresses), batch_size):
            chunk = addresses[i : i + batch_size]
            descriptors = [f"addr({addr})" for addr in chunk]
            queried_addresses = set(chunk)
            if SENSITIVE_LOGGING:
                logger.debug(f"Scanning addresses batch {i // batch_size + 1}: {chunk}")

            try:
                # Scan for all addresses in this chunk at once (with retry for conflicts)
                result = await self._scantxoutset_with_retry(descriptors)

                if not result or "unspents" not in result:
                    continue

                for utxo_data in result["unspents"]:
                    confirmations = 0
                    if utxo_data.get("height", 0) > 0:
                        confirmations = tip_height - utxo_data["height"] + 1

                    # Extract address from descriptor "addr(ADDRESS)#checksum" or "addr(ADDRESS)"
                    desc = utxo_data.get("desc", "")
                    # Remove checksum if present
                    if "#" in desc:
                        desc = desc.split("#")[0]

                    address = ""
                    if desc.startswith("addr(") and desc.endswith(")"):
                        address = desc[5:-1]
                        if address not in queried_addresses:
                            logger.warning(
                                "Descriptor scan returned address not in query set: %s",
                                address,
                            )
                    else:
                        # Only log warning if we really can't parse it (and it's not empty)
                        if desc:
                            logger.warning(f"Failed to parse address from descriptor: '{desc}'")

                    utxo = UTXO(
                        txid=utxo_data["txid"],
                        vout=utxo_data["vout"],
                        value=btc_to_sats(utxo_data["amount"]),
                        address=address,
                        confirmations=confirmations,
                        scriptpubkey=utxo_data.get("scriptPubKey", ""),
                        height=utxo_data.get("height"),
                    )
                    utxos.append(utxo)

                logger.debug(
                    f"Scanned {len(chunk)} addresses, found {len(result['unspents'])} UTXOs"
                )

            except Exception as e:
                logger.warning(f"Failed to scan UTXOs for batch starting {chunk[0]}: {e}")
                continue

        return utxos

    async def scan_descriptors(
        self, descriptors: Sequence[str | dict[str, Any]]
    ) -> dict[str, Any] | None:
        """
        Scan the UTXO set using output descriptors.

        This is much more efficient than scanning individual addresses,
        especially for HD wallets where you can use xpub descriptors with
        ranges to scan thousands of addresses in a single UTXO set pass.

        Example descriptors:
            - "addr(bc1q...)" - single address
            - "wpkh(xpub.../0/*)" - HD wallet external addresses (default range 0-1000)
            - {"desc": "wpkh(xpub.../0/*)", "range": [0, 999]} - explicit range

        Args:
            descriptors: List of output descriptors (strings or dicts with range)

        Returns:
            Raw scan result dict from Bitcoin Core, or None on failure.
            Result includes:
                - success: bool
                - txouts: number of UTXOs scanned
                - height: current block height
                - unspents: list of found UTXOs with txid, vout, scriptPubKey,
                            desc (matched descriptor), amount, height
                - total_amount: sum of all found UTXOs
        """
        if not descriptors:
            return {"success": True, "unspents": [], "total_amount": 0}

        logger.info(f"Starting descriptor scan with {len(descriptors)} descriptor(s)...")
        result = await self._scantxoutset_with_retry(descriptors)

        if result:
            unspent_count = len(result.get("unspents", []))
            total = result.get("total_amount", 0)
            logger.info(
                f"Descriptor scan complete: found {unspent_count} UTXOs, total {total:.8f} BTC"
            )
        else:
            logger.warning("Descriptor scan failed or returned no results")

        return result

    async def get_address_balance(self, address: str) -> int:
        utxos = await self.get_utxos([address])
        balance = sum(utxo.value for utxo in utxos)
        logger.debug(f"Balance for {address}: {balance} sats")
        return balance

    async def broadcast_transaction(self, tx_hex: str) -> str:
        try:
            txid = await self._rpc_call("sendrawtransaction", [tx_hex])
            logger.info(f"Broadcast transaction: {txid}")
            return txid

        except Exception as e:
            logger.error(f"Failed to broadcast transaction: {e}")
            raise ValueError(f"Broadcast failed: {e}") from e

    async def get_transaction(self, txid: str) -> Transaction | None:
        try:
            tx_data = await self._rpc_call("getrawtransaction", [txid, True])

            if not tx_data:
                return None

            confirmations = tx_data.get("confirmations", 0)
            block_height = None
            block_time = None

            if "blockhash" in tx_data:
                block_info = await self._rpc_call("getblockheader", [tx_data["blockhash"]])
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
            logger.debug(f"Failed to fetch transaction {txid}: {e}")
            return None

    async def estimate_fee(self, target_blocks: int) -> float:
        try:
            result = await self._rpc_call("estimatesmartfee", [target_blocks])

            if "feerate" in result:
                btc_per_kb = result["feerate"]
                # Convert BTC/kB to sat/vB (keep precision for sub-sat rates)
                sat_per_vbyte = btc_to_sats(btc_per_kb) / 1000
                logger.debug(f"Estimated fee for {target_blocks} blocks: {sat_per_vbyte} sat/vB")
                return sat_per_vbyte
            else:
                logger.warning("Fee estimation unavailable, using fallback")
                return 1.0

        except Exception as e:
            logger.warning(f"Failed to estimate fee: {e}, using fallback")
            return 1.0

    async def get_mempool_min_fee(self) -> float | None:
        """Get the minimum fee rate (in sat/vB) for transaction to be accepted into mempool.

        Returns:
            Minimum fee rate in sat/vB, or None if unavailable.
        """
        try:
            result = await self._rpc_call("getmempoolinfo", [])
            if "mempoolminfee" in result:
                btc_per_kb = result["mempoolminfee"]
                # Convert BTC/kB to sat/vB
                sat_per_vbyte = btc_to_sats(btc_per_kb) / 1000
                logger.debug(f"Mempool min fee: {sat_per_vbyte} sat/vB")
                return sat_per_vbyte
            return None
        except Exception as e:
            logger.debug(f"Failed to get mempool min fee: {e}")
            return None

    async def get_block_height(self) -> int:
        try:
            info = await self._rpc_call("getblockchaininfo", [])
            height = info.get("blocks", 0)
            logger.debug(f"Current block height: {height}")
            return height

        except Exception as e:
            logger.error(f"Failed to fetch block height: {e}")
            raise

    async def get_block_time(self, block_height: int) -> int:
        try:
            block_hash = await self.get_block_hash(block_height)
            block_header = await self._rpc_call("getblockheader", [block_hash])
            timestamp = block_header.get("time", 0)
            logger.debug(f"Block {block_height} timestamp: {timestamp}")
            return timestamp

        except Exception as e:
            logger.error(f"Failed to fetch block time for height {block_height}: {e}")
            raise

    async def get_block_hash(self, block_height: int) -> str:
        try:
            block_hash = await self._rpc_call("getblockhash", [block_height])
            logger.debug(f"Block hash for height {block_height}: {block_hash}")
            return block_hash

        except Exception as e:
            logger.error(f"Failed to fetch block hash for height {block_height}: {e}")
            raise

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        """Get a specific UTXO from the blockchain UTXO set using gettxout.
        Returns None if the UTXO does not exist or has been spent.

        If not found in confirmed UTXO set, checks mempool for unconfirmed transactions.
        """
        try:
            # gettxout returns None if UTXO doesn't exist or is spent
            # include_mempool=True checks both confirmed and unconfirmed outputs
            result = await self._rpc_call("gettxout", [txid, vout, True])

            if result is None:
                # Not found in UTXO set - check if it's in mempool (unconfirmed)
                logger.debug(
                    f"UTXO {txid}:{vout} not found in confirmed UTXO set, checking mempool..."
                )
                try:
                    # Get raw transaction from mempool
                    tx_data = await self._rpc_call("getrawtransaction", [txid, True])

                    if tx_data and "vout" in tx_data:
                        # Check if the vout exists and hasn't been spent
                        if vout < len(tx_data["vout"]):
                            vout_data = tx_data["vout"][vout]
                            value = btc_to_sats(vout_data.get("value", 0))

                            # Extract address from scriptPubKey
                            script_pub_key = vout_data.get("scriptPubKey", {})
                            address = script_pub_key.get("address", "")
                            # For multiple addresses (e.g., multisig), join them
                            if not address and "addresses" in script_pub_key:
                                addresses = script_pub_key.get("addresses", [])
                                address = addresses[0] if addresses else ""
                            scriptpubkey = script_pub_key.get("hex", "")

                            # Unconfirmed transaction has 0 confirmations
                            logger.info(f"Found UTXO {txid}:{vout} in mempool (unconfirmed)")
                            return UTXO(
                                txid=txid,
                                vout=vout,
                                value=value,
                                address=address,
                                confirmations=0,
                                scriptpubkey=scriptpubkey,
                                height=None,
                            )
                except Exception as mempool_err:
                    logger.debug(f"UTXO {txid}:{vout} not in mempool either: {mempool_err}")

                logger.debug(f"UTXO {txid}:{vout} not found (spent or doesn't exist)")
                return None

            # Get tip height for confirmation calculation
            tip_height = await self.get_block_height()

            confirmations = result.get("confirmations", 0)
            value = btc_to_sats(result.get("value", 0))  # BTC to sats

            # Extract address from scriptPubKey
            script_pub_key = result.get("scriptPubKey", {})
            address = script_pub_key.get("address", "")
            scriptpubkey = script_pub_key.get("hex", "")

            # Calculate height from confirmations
            height = None
            if confirmations > 0:
                height = tip_height - confirmations + 1

            return UTXO(
                txid=txid,
                vout=vout,
                value=value,
                address=address,
                confirmations=confirmations,
                scriptpubkey=scriptpubkey,
                height=height,
            )

        except Exception as e:
            logger.error(f"Failed to get UTXO {txid}:{vout}: {e}")
            return None

    async def verify_bonds(
        self,
        bonds: list[BondVerificationRequest],
    ) -> list[BondVerificationResult]:
        """Verify fidelity bond UTXOs using batched JSON-RPC calls.

        Uses ``_rpc_batch()`` to verify all bonds in just 2-3 HTTP requests:
        1. Batch ``gettxout`` for all bonds (1 request)
        2. ``getblockchaininfo`` for current height (1 request, concurrent with #1)
        3. Batch ``getblockheader`` for unique block hashes from results (1 request)

        For 100 bonds this is ~3 HTTP round-trips instead of ~200 sequential ones.
        """
        if not bonds:
            return []

        # Step 1: Batch gettxout + getblockchaininfo concurrently
        gettxout_requests: list[tuple[str, list]] = [
            ("gettxout", [b.txid, b.vout, True]) for b in bonds
        ]

        gettxout_task = self._rpc_batch(gettxout_requests)
        height_task = self.get_block_height()
        gettxout_results, current_height = await asyncio.gather(gettxout_task, height_task)

        # Step 2: Collect unique block hashes that need timestamp lookups
        for result in gettxout_results:
            if result is not None and result.get("confirmations", 0) > 0:
                # gettxout returns bestblock but we need the block at confirmation height
                # confirmations = tip - conf_height + 1, so conf_height = tip - confs + 1
                confs = result["confirmations"]
                conf_height = current_height - confs + 1
                # We'll need the block hash for this height; collect heights first
                result["_conf_height"] = conf_height

        # Get block hashes for all unique confirmation heights
        unique_conf_heights: set[int] = set()
        for result in gettxout_results:
            if result is not None and "_conf_height" in result:
                unique_conf_heights.add(result["_conf_height"])

        # Batch getblockhash for unique heights
        height_to_time: dict[int, int] = {}
        if unique_conf_heights:
            sorted_heights = sorted(unique_conf_heights)
            hash_requests: list[tuple[str, list]] = [("getblockhash", [h]) for h in sorted_heights]
            hash_results = await self._rpc_batch(hash_requests)

            # Now batch getblockheader for the hashes
            header_requests: list[tuple[str, list]] = []
            height_order: list[int] = []
            for i, h in enumerate(sorted_heights):
                block_hash = hash_results[i]
                if block_hash is not None:
                    header_requests.append(("getblockheader", [block_hash]))
                    height_order.append(h)

            if header_requests:
                header_results = await self._rpc_batch(header_requests)
                for i, h in enumerate(height_order):
                    header = header_results[i]
                    if header is not None:
                        height_to_time[h] = header.get("time", 0)

        # Step 3: Build results
        results: list[BondVerificationResult] = []
        for i, bond in enumerate(bonds):
            gettxout_result = gettxout_results[i]

            if gettxout_result is None:
                results.append(
                    BondVerificationResult(
                        txid=bond.txid,
                        vout=bond.vout,
                        value=0,
                        confirmations=0,
                        block_time=0,
                        valid=False,
                        error="UTXO not found or spent",
                    )
                )
                continue

            confs = gettxout_result.get("confirmations", 0)
            if confs <= 0:
                value = btc_to_sats(gettxout_result.get("value", 0))
                results.append(
                    BondVerificationResult(
                        txid=bond.txid,
                        vout=bond.vout,
                        value=value,
                        confirmations=0,
                        block_time=0,
                        valid=False,
                        error="UTXO unconfirmed",
                    )
                )
                continue

            value = btc_to_sats(gettxout_result.get("value", 0))
            conf_height = gettxout_result.get("_conf_height", 0)
            block_time = height_to_time.get(conf_height, 0)

            results.append(
                BondVerificationResult(
                    txid=bond.txid,
                    vout=bond.vout,
                    value=value,
                    confirmations=confs,
                    block_time=block_time,
                    valid=True,
                )
            )

        logger.debug(
            "Verified %d bonds: %d valid, %d invalid",
            len(bonds),
            sum(1 for r in results if r.valid),
            sum(1 for r in results if not r.valid),
        )
        return results

    def can_provide_neutrino_metadata(self) -> bool:
        """
        Bitcoin Core can provide Neutrino-compatible metadata.

        Full node can access scriptpubkey and blockheight for all UTXOs,
        allowing Neutrino takers to use our makers.

        Returns:
            True - Bitcoin Core always provides extended UTXO metadata
        """
        return True

    async def close(self) -> None:
        """Close backend connections and reset clients so the backend can be reused."""
        await self.client.aclose()
        await self._scan_client.aclose()
        # Re-create fresh clients so this instance is usable again if the
        # wallet service is restarted (e.g. maker stop → start in jmwalletd).
        self.client = httpx.AsyncClient(
            timeout=DEFAULT_RPC_TIMEOUT, auth=(self.rpc_user, self.rpc_password)
        )
        self._scan_client = httpx.AsyncClient(
            timeout=self.scan_timeout, auth=(self.rpc_user, self.rpc_password)
        )
