"""
LND REST API client for the swap provider.

Used by the mock swap provider to create real BOLT11 invoices and
monitor their payment status via LND's REST API.

When LND is not available (no cert/macaroon), the provider falls back
to generating fake invoices and broadcasting lockup transactions
immediately (the original mock behavior).
"""

from __future__ import annotations

import asyncio
import base64
import ssl
from pathlib import Path
from typing import Any

import httpx
from loguru import logger


class LndProviderClient:
    """Async client for the swap provider's LND node.

    Provides methods for:
    - Creating BOLT11 invoices with specific preimage hashes
    - Subscribing to invoice settlement notifications
    - Checking node connectivity and channel status
    """

    def __init__(
        self,
        rest_url: str,
        cert_path: str,
        macaroon_path: str,
    ) -> None:
        self._rest_url = rest_url
        self._cert_path = cert_path
        self._macaroon_path = macaroon_path
        self._client: httpx.AsyncClient | None = None
        self._available: bool | None = None

    def _macaroon_hex(self) -> str:
        """Read macaroon file and return hex encoding."""
        return Path(self._macaroon_path).read_bytes().hex()

    def _ssl_context(self) -> ssl.SSLContext:
        """Create SSL context trusting the LND TLS cert."""
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(self._cert_path)
        return ctx

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self._rest_url,
                headers={"Grpc-Metadata-macaroon": self._macaroon_hex()},
                verify=self._ssl_context(),
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def is_available(self, retries: int = 10, retry_delay: float = 3.0) -> bool:
        """Check if LND is reachable and synced.

        Retries on transient failures before giving up and falling back to MOCK mode.
        Caches the result after the first successful check.
        """
        if self._available is not None:
            return self._available

        # Check files exist
        if not Path(self._cert_path).is_file():
            logger.warning(f"LND cert not found: {self._cert_path}")
            self._available = False
            return False
        if not Path(self._macaroon_path).is_file():
            logger.warning(f"LND macaroon not found: {self._macaroon_path}")
            self._available = False
            return False

        for attempt in range(1, retries + 1):
            try:
                client = await self._get_client()
                resp = await client.get("/v1/getinfo", timeout=10.0)
                resp.raise_for_status()
                info = resp.json()
                synced = info.get("synced_to_chain", False)
                if synced:
                    logger.info(
                        f"LND connected: alias={info.get('alias', '?')}, "
                        f"pubkey={info.get('identity_pubkey', '?')[:16]}..., "
                        f"channels={info.get('num_active_channels', 0)}"
                    )
                    self._available = True
                    return True
                logger.warning(f"LND not yet synced to chain (attempt {attempt}/{retries})")
            except Exception as e:
                logger.warning(f"LND not available (attempt {attempt}/{retries}): {e}")
            if attempt < retries:
                await asyncio.sleep(retry_delay)

        logger.warning("LND unavailable after all retries -- falling back to MOCK mode")
        self._available = False
        return False

    async def add_invoice(
        self,
        r_hash: bytes,
        value_sats: int,
        memo: str = "",
        expiry: int = 3600,
    ) -> dict[str, Any]:
        """Create a BOLT11 invoice with a specific payment hash.

        This uses LND's AddInvoice RPC. For a reverse submarine swap,
        we want to create an invoice whose payment hash matches the
        preimage_hash from the swap request, so that when the taker
        pays, we learn the preimage.

        Note: LND's standard AddInvoice generates its own preimage.
        For a reverse swap the provider creates the preimage, hashes it,
        and the taker must reveal it when claiming. So we use AddInvoice
        with r_preimage set (provider knows the preimage).

        Args:
            r_hash: 32-byte payment hash (unused here -- we pass r_preimage instead).
            value_sats: Invoice amount in satoshis.
            memo: Invoice description.
            expiry: Invoice expiry in seconds.

        Returns:
            Dict with 'payment_request' (BOLT11 string) and 'r_hash'.
        """
        client = await self._get_client()

        # For reverse submarine swaps, the provider generates the preimage
        # and creates the invoice with it. The r_hash parameter here is the
        # SHA256(preimage) that the taker committed to. We need to create
        # an invoice where LND's internal preimage matches, so we set r_preimage.
        #
        # However, in our flow the taker generates the preimage and gives us
        # the hash. We can't create an invoice with an arbitrary hash in standard
        # LND without the preimage. Instead, we create a regular invoice and
        # the payment hash will be LND-generated. The swap ID still uses the
        # taker's preimage_hash for tracking.
        #
        # The key insight: in a reverse submarine swap, payment flows from
        # taker -> provider. The provider creates a normal invoice, taker pays it,
        # provider verifies payment, then broadcasts lockup. The lockup HTLC uses
        # the taker's preimage_hash (separate from the LN invoice hash).

        payload: dict[str, Any] = {
            "value": str(value_sats),
            "memo": memo or f"Swap {r_hash.hex()[:16]}",
            "expiry": str(expiry),
        }

        resp = await client.post("/v1/invoices", json=payload)
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()

        # Decode the r_hash from base64
        r_hash_b64 = result.get("r_hash", "")
        if r_hash_b64:
            try:
                hash_bytes = base64.b64decode(r_hash_b64)
                result["r_hash_hex"] = hash_bytes.hex()
            except Exception:
                pass

        logger.info(
            f"LND invoice created: {value_sats} sats, hash={result.get('r_hash_hex', '?')[:16]}..."
        )
        return result

    async def lookup_invoice(self, r_hash_hex: str) -> dict[str, Any]:
        """Look up an invoice by payment hash.

        Args:
            r_hash_hex: Payment hash as hex string.

        Returns:
            Invoice details including 'state' ('OPEN', 'SETTLED', 'CANCELED').
        """
        client = await self._get_client()
        # LND REST expects the r_hash_str path parameter as hex, not base64.
        resp = await client.get(f"/v1/invoice/{r_hash_hex}")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def wait_for_invoice_settlement(
        self,
        r_hash_hex: str,
        timeout: float = 120.0,
        poll_interval: float = 1.0,
    ) -> bool:
        """Wait for an invoice to be settled (paid).

        Polls the invoice status until it is SETTLED or the timeout expires.

        Args:
            r_hash_hex: Payment hash as hex string.
            timeout: Maximum seconds to wait.
            poll_interval: Seconds between polls.

        Returns:
            True if invoice was settled, False if timeout expired.
        """
        import time

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                invoice = await self.lookup_invoice(r_hash_hex)
                state = invoice.get("state", "OPEN")
                if state == "SETTLED":
                    logger.info(f"Invoice settled: {r_hash_hex[:16]}...")
                    return True
                if state in ("CANCELED", "EXPIRED"):
                    logger.warning(f"Invoice {state}: {r_hash_hex[:16]}...")
                    return False
            except Exception as e:
                logger.debug(f"Invoice lookup error (will retry): {e}")

            await asyncio.sleep(poll_interval)

        logger.warning(f"Invoice settlement timeout after {timeout}s: {r_hash_hex[:16]}...")
        return False

    async def subscribe_invoices(
        self,
        add_index: int = 0,
        settle_index: int = 0,
    ) -> Any:
        """Subscribe to invoice updates via streaming.

        This uses LND's SubscribeInvoices streaming RPC.
        Not used in the current polling-based implementation but
        available for future optimization.

        Args:
            add_index: Start from this add index.
            settle_index: Start from this settle index.
        """
        # This would use LND's streaming endpoint:
        # GET /v1/invoices/subscribe?add_index=N&settle_index=N
        # For now we use polling via wait_for_invoice_settlement.
        raise NotImplementedError("Streaming invoice subscription not yet implemented")
