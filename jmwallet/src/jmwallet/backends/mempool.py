"""
Mempool.space API blockchain backend.
Beginner-friendly backend that requires no setup.
"""

from __future__ import annotations

import asyncio

import httpx
from loguru import logger

from jmwallet.backends.base import (
    UTXO,
    BlockchainBackend,
    BondVerificationRequest,
    BondVerificationResult,
    Transaction,
)


class MempoolBackend(BlockchainBackend):
    """
    Blockchain backend using Mempool.space API.
    Works with public instance or self-hosted.
    """

    def __init__(self, base_url: str = "https://mempool.space/api", network: str = "mainnet"):
        if network == "testnet":
            base_url = "https://mempool.space/testnet/api"
        elif network == "signet":
            base_url = "https://mempool.space/signet/api"

        self.base_url = base_url.rstrip("/")
        self.network = network
        self.client = httpx.AsyncClient(timeout=30.0)

    async def get_utxos(self, addresses: list[str]) -> list[UTXO]:
        utxos: list[UTXO] = []
        for address in addresses:
            try:
                response = await self.client.get(f"{self.base_url}/address/{address}/utxo")
                response.raise_for_status()
                data = response.json()

                for utxo_data in data:
                    utxo = UTXO(
                        txid=utxo_data["txid"],
                        vout=utxo_data["vout"],
                        value=utxo_data["value"],
                        address=address,
                        confirmations=utxo_data["status"].get("block_height", 0),
                        scriptpubkey="",
                        height=utxo_data["status"].get("block_height"),
                    )
                    utxos.append(utxo)

                logger.debug(f"Found {len(data)} UTXOs for address {address}")

            except httpx.HTTPError as e:
                logger.warning(f"Failed to fetch UTXOs for {address}: {e}")
                continue

        return utxos

    async def get_address_balance(self, address: str) -> int:
        try:
            response = await self.client.get(f"{self.base_url}/address/{address}")
            response.raise_for_status()
            data = response.json()

            chain_stats = data.get("chain_stats", {})
            funded = chain_stats.get("funded_txo_sum", 0)
            spent = chain_stats.get("spent_txo_sum", 0)

            balance = funded - spent
            logger.debug(f"Balance for {address}: {balance} sats")
            return balance

        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch balance for {address}: {e}")
            return 0

    async def broadcast_transaction(self, tx_hex: str) -> str:
        try:
            response = await self.client.post(f"{self.base_url}/tx", content=tx_hex)
            response.raise_for_status()
            txid = response.text.strip()
            logger.info(f"Broadcast transaction: {txid}")
            return txid

        except httpx.HTTPError as e:
            logger.error(f"Failed to broadcast transaction: {e}")
            raise ValueError(f"Broadcast failed: {e}") from e

    async def get_transaction(self, txid: str) -> Transaction | None:
        try:
            response = await self.client.get(f"{self.base_url}/tx/{txid}")
            response.raise_for_status()
            data = response.json()

            raw_response = await self.client.get(f"{self.base_url}/tx/{txid}/hex")
            raw_response.raise_for_status()
            raw_hex = raw_response.text.strip()

            status = data.get("status", {})
            confirmed = status.get("confirmed", False)
            block_height = status.get("block_height") if confirmed else None
            block_time = status.get("block_time") if confirmed else None

            tip_height = await self.get_block_height()
            confirmations = 0
            if block_height:
                confirmations = tip_height - block_height + 1

            return Transaction(
                txid=txid,
                raw=raw_hex,
                confirmations=confirmations,
                block_height=block_height,
                block_time=block_time,
            )

        except httpx.HTTPError as e:
            logger.warning(f"Failed to fetch transaction {txid}: {e}")
            return None

    async def estimate_fee(self, target_blocks: int) -> float:
        try:
            response = await self.client.get(f"{self.base_url}/v1/fees/recommended")
            response.raise_for_status()
            data = response.json()

            if target_blocks <= 1:
                fee_rate = data.get("fastestFee", 1)
            elif target_blocks <= 3:
                fee_rate = data.get("halfHourFee", 1)
            elif target_blocks <= 6:
                fee_rate = data.get("hourFee", 1)
            else:
                fee_rate = data.get("minimumFee", 1)

            logger.debug(f"Estimated fee for {target_blocks} blocks: {fee_rate} sat/vB")
            return float(fee_rate)

        except httpx.HTTPError as e:
            logger.warning(f"Failed to estimate fee: {e}, using fallback")
            return 1.0

    async def get_block_height(self) -> int:
        try:
            response = await self.client.get(f"{self.base_url}/blocks/tip/height")
            response.raise_for_status()
            height = int(response.text.strip())
            logger.debug(f"Current block height: {height}")
            return height

        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch block height: {e}")
            raise

    async def get_block_time(self, block_height: int) -> int:
        try:
            block_hash = await self.get_block_hash(block_height)
            response = await self.client.get(f"{self.base_url}/block/{block_hash}")
            response.raise_for_status()
            data = response.json()
            timestamp = data.get("timestamp", 0)
            logger.debug(f"Block {block_height} timestamp: {timestamp}")
            return timestamp

        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch block time for height {block_height}: {e}")
            raise

    async def get_block_hash(self, block_height: int) -> str:
        try:
            response = await self.client.get(f"{self.base_url}/block-height/{block_height}")
            response.raise_for_status()
            block_hash = response.text.strip()
            logger.debug(f"Block hash for height {block_height}: {block_hash}")
            return block_hash

        except httpx.HTTPError as e:
            logger.error(f"Failed to fetch block hash for height {block_height}: {e}")
            raise

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        """Get a specific UTXO from the blockchain.
        Returns None if the UTXO does not exist or has been spent."""
        try:
            # Get transaction output info
            response = await self.client.get(f"{self.base_url}/tx/{txid}/outspend/{vout}")
            response.raise_for_status()
            outspend_data = response.json()

            # If it's been spent, return None
            if outspend_data.get("spent", False):
                logger.debug(f"UTXO {txid}:{vout} has been spent")
                return None

            # Get the transaction to get output details
            tx_response = await self.client.get(f"{self.base_url}/tx/{txid}")
            tx_response.raise_for_status()
            tx_data = tx_response.json()

            if vout >= len(tx_data.get("vout", [])):
                logger.debug(f"UTXO {txid}:{vout} vout index out of range")
                return None

            output = tx_data["vout"][vout]
            status = tx_data.get("status", {})
            confirmed = status.get("confirmed", False)
            block_height = status.get("block_height") if confirmed else None

            tip_height = await self.get_block_height()
            confirmations = 0
            if block_height:
                confirmations = tip_height - block_height + 1

            return UTXO(
                txid=txid,
                vout=vout,
                value=output.get("value", 0),
                address=output.get("scriptpubkey_address", ""),
                confirmations=confirmations,
                scriptpubkey=output.get("scriptpubkey", ""),
                height=block_height,
            )

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug(f"UTXO {txid}:{vout} not found")
                return None
            logger.error(f"Failed to get UTXO {txid}:{vout}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to get UTXO {txid}:{vout}: {e}")
            return None

    async def verify_bonds(
        self,
        bonds: list[BondVerificationRequest],
    ) -> list[BondVerificationResult]:
        """Verify fidelity bond UTXOs via parallel mempool.space API calls.

        Fetches the tip height once, then issues parallel HTTP requests for each
        bond UTXO with a semaphore to respect rate limits.

        For each bond: outspend check + tx details = 2 HTTP requests.
        """
        if not bonds:
            return []

        tip_height = await self.get_block_height()
        semaphore = asyncio.Semaphore(10)

        async def _verify_one(bond: BondVerificationRequest) -> BondVerificationResult:
            async with semaphore:
                try:
                    # Check if spent
                    resp = await self.client.get(
                        f"{self.base_url}/tx/{bond.txid}/outspend/{bond.vout}"
                    )
                    resp.raise_for_status()
                    outspend = resp.json()

                    if outspend.get("spent", False):
                        return BondVerificationResult(
                            txid=bond.txid,
                            vout=bond.vout,
                            value=0,
                            confirmations=0,
                            block_time=0,
                            valid=False,
                            error="UTXO spent",
                        )

                    # Get transaction details
                    tx_resp = await self.client.get(f"{self.base_url}/tx/{bond.txid}")
                    tx_resp.raise_for_status()
                    tx_data = tx_resp.json()

                    if bond.vout >= len(tx_data.get("vout", [])):
                        return BondVerificationResult(
                            txid=bond.txid,
                            vout=bond.vout,
                            value=0,
                            confirmations=0,
                            block_time=0,
                            valid=False,
                            error="vout index out of range",
                        )

                    output = tx_data["vout"][bond.vout]
                    status = tx_data.get("status", {})
                    confirmed = status.get("confirmed", False)
                    block_height = status.get("block_height") if confirmed else None
                    block_time = status.get("block_time", 0) if confirmed else 0

                    confirmations = 0
                    if block_height:
                        confirmations = tip_height - block_height + 1

                    if confirmations <= 0:
                        return BondVerificationResult(
                            txid=bond.txid,
                            vout=bond.vout,
                            value=output.get("value", 0),
                            confirmations=0,
                            block_time=0,
                            valid=False,
                            error="UTXO unconfirmed",
                        )

                    return BondVerificationResult(
                        txid=bond.txid,
                        vout=bond.vout,
                        value=output.get("value", 0),
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
            f"Verified {len(bonds)} bonds: "
            f"{sum(1 for r in results if r.valid)} valid, "
            f"{sum(1 for r in results if not r.valid)} invalid"
        )
        return list(results)

    async def close(self) -> None:
        await self.client.aclose()
