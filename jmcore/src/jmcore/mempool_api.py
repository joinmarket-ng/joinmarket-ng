"""
Mempool API client for Bitcoin blockchain queries.
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger
from pydantic import BaseModel


class AddressStats(BaseModel):
    funded_txo_count: int
    funded_txo_sum: int
    spent_txo_count: int
    spent_txo_sum: int
    tx_count: int


class AddressInfo(BaseModel):
    address: str
    chain_stats: AddressStats
    mempool_stats: AddressStats

    def total_received(self) -> int:
        return self.chain_stats.funded_txo_sum + self.mempool_stats.funded_txo_sum

    def total_sent(self) -> int:
        return self.chain_stats.spent_txo_sum + self.mempool_stats.spent_txo_sum

    def balance(self) -> int:
        return self.total_received() - self.total_sent()


class TxOut(BaseModel):
    scriptpubkey: str
    scriptpubkey_asm: str
    scriptpubkey_type: str
    scriptpubkey_address: str | None = None
    value: int


class TxStatus(BaseModel):
    confirmed: bool
    block_height: int | None = None
    block_hash: str | None = None
    block_time: int | None = None


class Transaction(BaseModel):
    txid: str
    version: int
    locktime: int
    size: int
    weight: int
    fee: int
    vin: list[dict[str, Any]]
    vout: list[TxOut]
    status: TxStatus


class MempoolAPIError(Exception):
    pass


class MempoolAPI:
    def __init__(
        self,
        base_url: str,
        timeout: float = 30.0,
        socks_proxy: str | None = None,
        trust_env: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.timeout = timeout
        self.socks_proxy = socks_proxy
        self.trust_env = trust_env

        client_kwargs: dict[str, Any] = {"trust_env": trust_env}
        if socks_proxy:
            try:
                from httpx_socks import AsyncProxyTransport

                from jmcore.tor_isolation import normalize_proxy_url

                # python-socks does not support the socks5h:// scheme directly.
                # normalize_proxy_url converts socks5h:// -> socks5:// + rdns=True
                # so that .onion addresses are resolved by Tor.
                normalized = normalize_proxy_url(socks_proxy)

                transport = AsyncProxyTransport.from_url(normalized.url, rdns=normalized.rdns)
                client_kwargs["transport"] = transport
            except ImportError as e:
                raise MempoolAPIError(
                    "Tor-routed mempool access requires httpx-socks; install httpx-socks and retry"
                ) from e
            except Exception as e:
                raise MempoolAPIError("Failed to configure Tor transport for mempool access") from e

        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, **client_kwargs)

    async def __aenter__(self) -> MempoolAPI:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def test_connection(self) -> bool:
        """Test if the API connection works by making a simple request."""
        if not self.base_url:
            logger.debug("Mempool API connection test skipped (no base_url configured)")
            return False

        try:
            # Test with a lightweight endpoint - get current block tip height
            url = f"{self.base_url}/blocks/tip/height"
            logger.debug(f"Testing connection to: {url}")
            response = await self.client.get(url)
            response.raise_for_status()
            height = int(response.text)
            logger.info(f"Connection test successful - current block height: {height}")
            return True
        except Exception as e:
            logger.error(f"MempoolAPI connection test failed: {e}")
            return False

    async def _get(self, endpoint: str) -> dict[str, Any]:
        if not self.base_url:
            raise MempoolAPIError("Mempool API URL is not configured")

        url = f"{self.base_url}/{endpoint}"
        try:
            logger.debug(f"MempoolAPI request: GET {url}")
            transport = "Tor" if self.socks_proxy else "direct"
            logger.debug(f"MempoolAPI request transport configured: {transport}")
            response = await self.client.get(url)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"MempoolAPI error: {e}")
            logger.debug(
                f"MempoolAPI client transport: {getattr(self.client, '_transport', 'None')}"
            )
            raise MempoolAPIError(f"API request failed: {e}") from e

    async def get_address_info(self, address: str) -> AddressInfo:
        data = await self._get(f"address/{address}")
        return AddressInfo(**data)

    async def get_transaction(self, txid: str) -> Transaction:
        data = await self._get(f"tx/{txid}")
        return Transaction(**data)

    async def get_block_height(self) -> int:
        response = await self.client.get(f"{self.base_url}/blocks/tip/height")
        response.raise_for_status()
        return int(response.text)

    async def get_block_hash(self, height: int) -> str:
        response = await self.client.get(f"{self.base_url}/block-height/{height}")
        response.raise_for_status()
        return response.text

    async def get_utxo_confirmations(self, txid: str, vout: int) -> int | None:
        try:
            tx = await self.get_transaction(txid)
            if not tx.status.confirmed or tx.status.block_height is None:
                return None

            current_height = await self.get_block_height()
            confirmations = current_height - tx.status.block_height + 1
            return max(0, confirmations)
        except MempoolAPIError:
            return None

    async def get_utxo_value(self, txid: str, vout: int) -> int | None:
        try:
            tx = await self.get_transaction(txid)
            if vout >= len(tx.vout):
                return None
            return tx.vout[vout].value
        except MempoolAPIError:
            return None
