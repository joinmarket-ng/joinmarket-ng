"""Small read/broadcast Bitcoin Core boundary for escrow monitoring.

No wallet creation, descriptor imports, rescans, or index construction occurs.
The operator must supply an already-synchronized node with txindex enabled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Self

import httpx

from jmswap.bitcoin_escrow import EscrowOutpoint


class ChainError(Exception):
    """A chain observation or broadcast did not complete reliably."""


class CoreRpcError(ChainError):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"Bitcoin Core RPC failed ({code})")


@dataclass(frozen=True)
class ChainTransaction:
    txid: str
    raw: bytes
    confirmations: int
    height: int | None
    block_hash: str | None


class BuyoutChain:
    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=endpoint,
            auth=(username, password),
            timeout=30,
            transport=transport,
            trust_env=False,
        )

    async def __aenter__(self) -> Self:
        try:
            await self.check_ready()
        except BaseException:
            await self._client.aclose()
            raise
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()

    async def _rpc(self, method: str, *params: object) -> Any:
        try:
            response = await self._client.post(
                "", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": list(params)}
            )
            payload = response.json()
            if payload.get("error"):
                raise CoreRpcError(int(payload["error"]["code"]))
            response.raise_for_status()
            return payload["result"]
        except CoreRpcError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ChainError("Bitcoin Core response was unavailable or invalid") from None

    async def check_ready(self) -> str:
        info = await self._rpc("getblockchaininfo")
        indexes = await self._rpc("getindexinfo", "txindex")
        if info.get("initialblockdownload") or not indexes.get("txindex", {}).get("synced"):
            raise ChainError("buyout monitoring requires a synchronized node and txindex")
        return str(info["chain"])

    async def tip(self) -> tuple[int, str]:
        info = await self._rpc("getblockchaininfo")
        if info.get("initialblockdownload"):
            raise ChainError("Bitcoin Core is not synchronized")
        return int(info["blocks"]), str(info["bestblockhash"])

    async def height(self) -> int:
        return (await self.tip())[0]

    async def block_hash(self, height: int) -> str:
        return str(await self._rpc("getblockhash", height))

    async def transaction(self, txid: str) -> ChainTransaction | None:
        try:
            data = await self._rpc("getrawtransaction", txid, True)
        except CoreRpcError as exc:
            if exc.code == -5:
                return None
            raise
        if data["txid"] != txid:
            raise ChainError("Bitcoin Core returned a different transaction")
        block_hash = data.get("blockhash")
        confirmations = int(data.get("confirmations", 0))
        height = None
        if block_hash is not None:
            header = await self._rpc("getblockheader", block_hash)
            confirmations = int(header["confirmations"])
            if confirmations < 0:
                return None
            height = int(header["height"])
        return ChainTransaction(txid, bytes.fromhex(data["hex"]), confirmations, height, block_hash)

    async def unspent(self, outpoint: EscrowOutpoint, *, include_mempool: bool = True) -> bool:
        data = await self._rpc("gettxout", outpoint.txid, outpoint.vout, include_mempool)
        if data is None:
            return False
        value = int(Decimal(str(data["value"])) * 100_000_000)
        if value != outpoint.value or data["scriptPubKey"]["hex"] != outpoint.scriptpubkey.hex():
            raise ChainError("escrow prevout disagrees with the recorded parent")
        return True

    async def fee_rates(self) -> tuple[int, int]:
        estimate = await self._rpc("estimatesmartfee", 2, "conservative")
        mempool = await self._rpc("getmempoolinfo")

        def rate(value: object) -> int:
            return math.ceil(Decimal(str(value)) * 100_000)

        floor = rate(mempool["mempoolminfee"])
        suggested = rate(estimate["feerate"]) if "feerate" in estimate else floor
        return max(1, floor, suggested), max(1, rate(mempool["incrementalrelayfee"]))

    async def broadcast(self, raw: bytes) -> str:
        from jmcore.bitcoin import get_txid

        txid = get_txid(raw.hex())
        try:
            returned = await self._rpc("sendrawtransaction", raw.hex())
        except CoreRpcError as exc:
            # Already confirmed is affirmative evidence, not an inferred retry.
            if exc.code == -27 and await self.transaction(txid) is not None:
                return txid
            raise
        if returned != txid:
            raise ChainError("Bitcoin Core returned a different broadcast txid")
        return txid
