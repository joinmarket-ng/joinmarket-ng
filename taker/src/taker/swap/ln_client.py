"""
LND REST API client for Lightning Network invoice payment.

Used by the taker to pay swap invoices via its own LND node.
Communicates with LND's REST API (port 8080 by default) using
TLS certificate and macaroon authentication.

This module is optional -- if no LND connection is configured,
the swap client skips automatic invoice payment and expects
the user to pay manually.
"""

from __future__ import annotations

import base64
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger


@dataclass(frozen=True)
class LndConnection:
    """Connection parameters for an LND node's REST API."""

    rest_url: str
    cert_path: str
    macaroon_path: str

    def macaroon_hex(self) -> str:
        """Read the macaroon file and return its hex encoding."""
        mac_bytes = Path(self.macaroon_path).read_bytes()
        return mac_bytes.hex()

    def ssl_context(self) -> ssl.SSLContext:
        """Create an SSL context that trusts the LND TLS certificate."""
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(self.cert_path)
        return ctx


class LndRestClient:
    """Async HTTP client for LND's REST API.

    Supports the subset of LND REST endpoints needed for swap invoice payment:
    - POST /v1/channels/transactions  (SendPaymentSync -- pay a BOLT11 invoice)
    - GET  /v1/getinfo                (node identity and sync status)
    - GET  /v1/balance/channels        (channel balances)
    - GET  /v1/invoice/{r_hash_str}    (lookup invoice by hash)

    All methods are async and use httpx for HTTP transport.
    """

    def __init__(self, connection: LndConnection) -> None:
        self._conn = connection
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the httpx client with LND auth."""
        if self._client is None or self._client.is_closed:
            macaroon_hex = self._conn.macaroon_hex()
            self._client = httpx.AsyncClient(
                base_url=self._conn.rest_url,
                headers={"Grpc-Metadata-macaroon": macaroon_hex},
                verify=self._conn.ssl_context(),
                timeout=60.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def get_info(self) -> dict[str, Any]:
        """Get node info (identity, sync status, etc.)."""
        client = await self._get_client()
        resp = await client.get("/v1/getinfo")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_channel_balance(self) -> dict[str, Any]:
        """Get channel balance summary."""
        client = await self._get_client()
        resp = await client.get("/v1/balance/channels")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def pay_invoice(
        self,
        payment_request: str,
        timeout_seconds: int = 60,
        fee_limit_sat: int = 1000,
    ) -> dict[str, Any]:
        """Pay a BOLT11 invoice synchronously.

        Uses LND's SendPaymentSync RPC which blocks until the payment
        completes or fails.

        Args:
            payment_request: BOLT11 invoice string.
            timeout_seconds: Payment timeout in seconds.
            fee_limit_sat: Maximum routing fee in sats.

        Returns:
            Payment response dict with fields:
            - payment_hash: hex string
            - payment_preimage: hex string (proof of payment)
            - payment_route: route details
            - payment_error: empty string on success

        Raises:
            ValueError: If payment fails (non-empty payment_error).
            httpx.HTTPStatusError: If LND returns an error status.
        """
        client = await self._get_client()

        # SendPaymentSync does not accept timeout_seconds; it is gRPC-gateway
        # specific and LND's REST proxy returns 400 for unknown fields.
        # fee_limit.fixed must be an integer, not a string.
        payload: dict[str, Any] = {
            "payment_request": payment_request,
            "fee_limit": {"fixed": fee_limit_sat},
        }

        logger.debug(f"Sending LN payment: timeout={timeout_seconds}s, fee_limit={fee_limit_sat}")
        resp = await client.post(
            "/v1/channels/transactions",
            json=payload,
            timeout=timeout_seconds + 10,
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()

        payment_error = result.get("payment_error", "")
        if payment_error:
            raise ValueError(f"LN payment failed: {payment_error}")

        # Decode the preimage from base64 if present
        preimage_b64 = result.get("payment_preimage", "")
        if preimage_b64:
            try:
                preimage_bytes = base64.b64decode(preimage_b64)
                result["payment_preimage_hex"] = preimage_bytes.hex()
            except Exception:
                pass

        payment_hash_b64 = result.get("payment_hash", "")
        if payment_hash_b64:
            try:
                hash_bytes = base64.b64decode(payment_hash_b64)
                result["payment_hash_hex"] = hash_bytes.hex()
            except Exception:
                pass

        logger.info(
            f"LN payment successful: hash={result.get('payment_hash_hex', 'unknown')[:16]}..."
        )
        return result

    async def decode_pay_req(self, payment_request: str) -> dict[str, Any]:
        """Decode a BOLT11 payment request without paying it.

        Args:
            payment_request: BOLT11 invoice string.

        Returns:
            Decoded invoice details (destination, num_satoshis, payment_hash, etc.)
        """
        client = await self._get_client()
        resp = await client.get(f"/v1/payreq/{payment_request}")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def lookup_invoice(self, r_hash_hex: str) -> dict[str, Any]:
        """Look up an invoice by its payment hash.

        Args:
            r_hash_hex: Payment hash as hex string.

        Returns:
            Invoice details including settlement status.
        """
        client = await self._get_client()
        # LND REST expects the r_hash_str path parameter as hex, not base64.
        resp = await client.get(f"/v1/invoice/{r_hash_hex}")
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
