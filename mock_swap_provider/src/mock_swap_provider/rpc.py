"""Bitcoin Core RPC client for the mock swap provider.

Provides async access to Bitcoin Core's JSON-RPC interface for:
- Creating raw transactions (lockup P2WSH outputs)
- Funding and signing transactions
- Getting blockchain info (block height, mempool)
"""

from __future__ import annotations

from typing import Any

import httpx
from loguru import logger


class BitcoinRPC:
    """Async Bitcoin Core RPC client."""

    def __init__(
        self,
        url: str = "http://localhost:18443",
        user: str = "test",
        password: str = "test",
        wallet: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.user = user
        self.password = password
        self.wallet = wallet
        self.timeout = timeout
        self._request_id = 0

    @property
    def _rpc_url(self) -> str:
        if self.wallet:
            return f"{self.url}/wallet/{self.wallet}"
        return self.url

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Execute an RPC call."""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or [],
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self._rpc_url,
                json=payload,
                auth=(self.user, self.password),
            )
            data = response.json()
            if data.get("error"):
                raise RuntimeError(f"RPC error: {data['error']}")
            return data.get("result")

    async def get_block_count(self) -> int:
        return await self.call("getblockcount")  # type: ignore[return-value]

    async def get_new_address(self, label: str = "", address_type: str = "bech32") -> str:
        return await self.call("getnewaddress", [label, address_type])  # type: ignore[return-value]

    async def create_raw_transaction(
        self, inputs: list[dict[str, Any]], outputs: list[dict[str, Any]]
    ) -> str:
        return await self.call("createrawtransaction", [inputs, outputs])  # type: ignore[return-value]

    async def fund_raw_transaction(
        self, hex_string: str, options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self.call("fundrawtransaction", [hex_string, options or {}])  # type: ignore[return-value]

    async def sign_raw_transaction(self, hex_string: str) -> dict[str, Any]:
        return await self.call("signrawtransactionwithwallet", [hex_string])  # type: ignore[return-value]

    async def send_raw_transaction(self, hex_string: str) -> str:
        return await self.call("sendrawtransaction", [hex_string])  # type: ignore[return-value]

    async def decode_raw_transaction(self, hex_string: str) -> dict[str, Any]:
        return await self.call("decoderawtransaction", [hex_string])  # type: ignore[return-value]

    async def generate_to_address(self, nblocks: int, address: str) -> list[str]:
        return await self.call("generatetoaddress", [nblocks, address])  # type: ignore[return-value]

    async def get_raw_transaction(self, txid: str, verbose: bool = True) -> Any:
        return await self.call("getrawtransaction", [txid, verbose])

    async def create_wallet(self, name: str) -> dict[str, Any]:
        """Create a descriptor wallet. Ignores 'already exists' errors."""
        try:
            return await self.call(  # type: ignore[return-value]
                "createwallet",
                [name, False, False, "", False, True, True],
            )
        except RuntimeError as e:
            if "already exists" in str(e).lower() or "already loaded" in str(e).lower():
                logger.debug(f"Wallet '{name}' already exists, loading...")
                try:
                    return await self.call("loadwallet", [name])  # type: ignore[return-value]
                except RuntimeError:
                    return {"name": name}  # Already loaded
            raise

    async def list_unspent(
        self, min_conf: int = 0, max_conf: int = 9999999
    ) -> list[dict[str, Any]]:
        return await self.call("listunspent", [min_conf, max_conf])  # type: ignore[return-value]

    async def get_balance(self) -> float:
        return await self.call("getbalance")  # type: ignore[return-value]
