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
    socks_host: str | None = None
    socks_port: int | None = None

    def macaroon_hex(self) -> str:
        """Read the macaroon file and return its hex encoding."""
        mac_bytes = Path(self.macaroon_path).read_bytes()
        return mac_bytes.hex()

    def ssl_context(self) -> ssl.SSLContext:
        """Create an SSL context that trusts the LND TLS certificate."""
        ctx = ssl.create_default_context()
        ctx.load_verify_locations(self.cert_path)
        return ctx

    def is_onion(self) -> bool:
        """True if rest_url points to a .onion hidden service."""
        return ".onion" in self.rest_url.lower()


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
        """Get or create the httpx client with LND auth.

        When the LND REST URL is a .onion hidden service or a SOCKS proxy is
        configured, route the connection through the proxy so the LND request
        does not bypass Tor. This matters for privacy because Lightning
        payments leak partial route information (Kappos et al. 2021); even a
        clearnet metadata leak about which LND instance paid a swap invoice
        would be enough to deanonymize the taker.
        """
        if self._client is None or self._client.is_closed:
            macaroon_hex = self._conn.macaroon_hex()
            kwargs: dict[str, Any] = {
                "base_url": self._conn.rest_url,
                "headers": {"Grpc-Metadata-macaroon": macaroon_hex},
                "verify": self._conn.ssl_context(),
                "timeout": 60.0,
            }
            use_socks = self._conn.is_onion() and self._conn.socks_host and self._conn.socks_port
            if use_socks:
                from httpx_socks import AsyncProxyTransport

                proxy_url = f"socks5://{self._conn.socks_host}:{self._conn.socks_port}"
                kwargs["transport"] = AsyncProxyTransport.from_url(
                    proxy_url, verify=self._conn.ssl_context()
                )
                logger.debug(f"LND REST routed via SOCKS: {proxy_url}")
            self._client = httpx.AsyncClient(**kwargs)
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
        *,
        enable_mpp: bool = False,
        max_parts: int = 16,
    ) -> dict[str, Any]:
        """Pay a BOLT11 invoice synchronously.

        Uses LND's SendPaymentSync RPC which blocks until the payment
        completes or fails. When ``enable_mpp`` is True, falls back to
        ``/v2/router/send`` (SendPaymentV2) and asks LND to split the payment
        across up to ``max_parts`` HTLCs. MPP improves Lightning privacy by
        spreading routing observation across multiple paths (mitigates the
        Kappos et al. 2021 deanonymization vector at the cost of higher
        routing fees and a small reliability hit).

        Args:
            payment_request: BOLT11 invoice string.
            timeout_seconds: Payment timeout in seconds.
            fee_limit_sat: Maximum routing fee in sats.
            enable_mpp: If True, use SendPaymentV2 with multi-path payments.
            max_parts: Maximum HTLC parts when MPP is enabled.

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

        if enable_mpp:
            return await self._pay_invoice_v2(
                client,
                payment_request,
                timeout_seconds=timeout_seconds,
                fee_limit_sat=fee_limit_sat,
                max_parts=max_parts,
            )

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

    async def _pay_invoice_v2(
        self,
        client: httpx.AsyncClient,
        payment_request: str,
        *,
        timeout_seconds: int,
        fee_limit_sat: int,
        max_parts: int,
    ) -> dict[str, Any]:
        """Pay via /v2/router/send (SendPaymentV2) with MPP enabled.

        Streams JSON-encoded payment updates; the final update with
        ``status == "SUCCEEDED"`` carries the preimage. Failures surface as
        ``status == "FAILED"`` with a ``failure_reason`` string.
        """
        import json

        payload: dict[str, Any] = {
            "payment_request": payment_request,
            "timeout_seconds": timeout_seconds,
            "fee_limit_sat": str(fee_limit_sat),
            "max_parts": max_parts,
            # no_inflight_updates trims the stream to terminal states only.
            "no_inflight_updates": True,
            # cancelable lets the local timeout actually abort in-flight HTLCs
            # instead of forcing us to wait for them to settle or expire.
            "cancelable": True,
        }
        logger.debug(
            f"Sending LN MPP payment: max_parts={max_parts}, "
            f"timeout={timeout_seconds}s, fee_limit={fee_limit_sat}"
        )
        async with client.stream(
            "POST",
            "/v2/router/send",
            json=payload,
            timeout=timeout_seconds + 30,
        ) as resp:
            resp.raise_for_status()
            terminal: dict[str, Any] | None = None
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                try:
                    update = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # /v2 wraps payloads in {"result": {...}}.
                payment = update.get("result", update)
                if payment.get("status") in {"SUCCEEDED", "FAILED"}:
                    terminal = payment
                    break
        if terminal is None:
            raise ValueError("LN MPP payment ended without a terminal status")
        if terminal.get("status") == "FAILED":
            raise ValueError(f"LN MPP payment failed: {terminal.get('failure_reason', 'unknown')}")
        # Normalize fields to match SendPaymentSync's response shape.
        preimage_hex = terminal.get("payment_preimage", "")
        payment_hash_hex = terminal.get("payment_hash", "")
        result = {
            "payment_preimage_hex": preimage_hex,
            "payment_hash_hex": payment_hash_hex,
            "payment_error": "",
            "raw": terminal,
        }
        logger.info(f"LN MPP payment successful: hash={payment_hash_hex[:16]}...")
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
