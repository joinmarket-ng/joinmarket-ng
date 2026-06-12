"""
Helpers for interacting with local Bitcoin Core regtest node.
"""

from __future__ import annotations

import math
import os
from typing import Any

import httpx
from loguru import logger

BITCOIN_RPC_URL = os.getenv("BITCOIN_RPC_URL", "http://127.0.0.1:18443")
BITCOIN_RPC_USER = os.getenv("BITCOIN_RPC_USER", "test")
BITCOIN_RPC_PASSWORD = os.getenv("BITCOIN_RPC_PASSWORD", "test")

# Name of the pre-funded Bitcoin Core wallet created by fund-test-wallets.sh.
# Test fixtures use it to send BTC via sendtoaddress instead of mining
# coinbases, avoiding the cascading halving problem on long-running chains.
TEST_FUNDER_WALLET = "test-funder"


class BitcoinRPCError(Exception):
    pass


async def rpc_call(
    method: str, params: list[Any] | None = None, wallet: str | None = None
) -> Any:
    url = BITCOIN_RPC_URL.rstrip("/")
    if wallet:
        url = f"{url}/wallet/{wallet}"

    payload = {
        "jsonrpc": "1.0",
        "id": "jm-tests",
        "method": method,
        "params": params or [],
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            url, auth=(BITCOIN_RPC_USER, BITCOIN_RPC_PASSWORD), json=payload
        )

    data = response.json()
    if data.get("error"):
        raise BitcoinRPCError(data["error"])
    return data.get("result")


async def mine_blocks(blocks: int, address: str) -> None:
    """
    Mine blocks to a specific address.

    We avoid using wallet RPC completely - the wallet is external to Bitcoin Core.
    """
    await rpc_call("generatetoaddress", [blocks, address])
    logger.info(f"Mined {blocks} blocks to {address}")


async def send_from_test_funder(
    target_address: str,
    amount_btc: float,
    confirmations: int = 1,
) -> bool:
    """Send ``amount_btc`` BTC from the pre-funded ``test-funder`` Core wallet.

    The ``test-funder`` wallet is created by ``scripts/fund-test-wallets.sh``
    during Docker setup with a large coinbase balance (≈6 000 BTC).  Using it
    for individual test wallet funding avoids mining coinbases per test, which
    would advance the chain height and eventually push the regtest coinbase
    subsidy to zero.

    After sending, mines ``confirmations`` blocks so the recipient's UTXO is
    confirmed and immediately spendable.

    Returns True on success, False when the wallet is unavailable or has
    insufficient funds (callers should fall back to coinbase mining).
    """
    try:
        info = await rpc_call("getwalletinfo", wallet=TEST_FUNDER_WALLET)
        balance = float(info.get("balance", 0))
        if balance < amount_btc:
            logger.warning(
                f"test-funder balance ({balance:.4f} BTC) < {amount_btc:.4f} BTC required"
            )
            return False

        await rpc_call(
            "sendtoaddress", [target_address, amount_btc], wallet=TEST_FUNDER_WALLET
        )
        # Mine confirmation blocks to any valid address (coinbase reward is
        # zero at deep heights but the block confirms the send transaction).
        miner_addr = await rpc_call(
            "getnewaddress", ["", "bech32"], wallet=TEST_FUNDER_WALLET
        )
        await rpc_call("generatetoaddress", [confirmations, miner_addr])
        logger.info(
            f"Funded {target_address} with {amount_btc} BTC from test-funder "
            f"({confirmations} confirmation block(s) mined)"
        )
        return True
    except BitcoinRPCError as exc:
        logger.debug(f"test-funder unavailable: {exc}")
        return False
    except Exception as exc:
        logger.debug(f"Unexpected error using test-funder: {exc}")
        return False


async def ensure_wallet_funded(
    target_address: str, amount_btc: float = 1.0, confirmations: int = 1
) -> bool:
    """
    Fund a wallet address by sending BTC to it.

    First tries to send from the pre-funded ``test-funder`` Core wallet
    (subsidy-independent, mines only 1 confirmation block per call).
    Falls back to coinbase mining for backwards compatibility with
    environments that do not have the test-funder wallet.

    Args:
        target_address: Address to fund
        amount_btc: Minimum spendable amount the address should end up with
        confirmations: Extra confirmations to mine on top of maturity

    Returns:
        True if successful, False otherwise
    """
    # Fast path: use the dedicated test-funder wallet (1 confirmation block).
    if await send_from_test_funder(target_address, amount_btc + 0.1, confirmations):
        return True

    # Fallback: mine coinbases directly to the address (legacy path for
    # environments without the test-funder wallet).
    try:
        info = await rpc_call("getblockchaininfo")
        height = int(info["blocks"])
        # Authoritative current subsidy (sats) from the chain, so we never
        # hardcode the halving schedule.
        stats = await rpc_call("getblockstats", [height, ["subsidy"]])
        subsidy_btc = float(stats["subsidy"]) / 1e8

        # Fee buffer covers the spends that follow funding (sendmany batches,
        # CoinJoin inputs). Generous on regtest where it costs nothing.
        target_btc = amount_btc + 0.5

        if subsidy_btc <= 0:
            logger.error(
                f"Block subsidy is zero at height {height}; cannot fund via "
                "coinbase on this exhausted regtest chain. Recreate the chain "
                "with `docker compose down -v`."
            )
            return False

        # Coinbase needs 100 confirmations to mature, so to end up with
        # ``needed_mature`` mature coinbases we mine that many plus 100.
        needed_mature = max(1, math.ceil(target_btc / subsidy_btc))
        blocks_to_mine = needed_mature + 100 + confirmations

        logger.info(
            f"Funding {target_address}: subsidy={subsidy_btc:.8f} BTC at "
            f"height {height}, mining {blocks_to_mine} blocks "
            f"(~{needed_mature * subsidy_btc:.4f} BTC mature) for >= {target_btc} BTC"
        )
        await rpc_call("generatetoaddress", [blocks_to_mine, target_address])
        return True
    except BitcoinRPCError as exc:
        logger.error(f"Failed to auto-fund wallet: {exc}")
        return False
    except Exception as exc:
        logger.error(f"Unexpected error during auto-funding: {exc}")
        return False


async def fund_core_wallet(
    wallet: str,
    miner_address: str,
    target_btc: float = 1.0,
    *,
    max_rounds: int = 20,
) -> bool:
    """Mine enough mature coinbases into a Core ``wallet`` to reach a balance.

    First tries to send from the pre-funded ``test-funder`` wallet
    (subsidy-independent, mines only 1 confirmation block). Falls back to
    coinbase mining if test-funder is unavailable.

    Args:
        wallet: Loaded Core wallet name to fund.
        miner_address: Address in that wallet to receive the funds.
        target_btc: Minimum spendable balance to reach.
        max_rounds: Safety cap on coinbase mining rounds.

    Returns:
        True once the wallet's spendable balance is >= ``target_btc``.
    """
    info = await rpc_call("getwalletinfo", wallet=wallet)
    balance = float(info.get("balance", 0))
    if balance >= target_btc:
        return True

    # Try test-funder first.
    deficit = target_btc - balance
    if await send_from_test_funder(miner_address, deficit + 0.1, 1):
        return True

    # Fallback: coinbase mining.
    for _ in range(max_rounds):
        info = await rpc_call("getwalletinfo", wallet=wallet)
        balance = float(info.get("balance", 0))
        if balance >= target_btc:
            return True

        chain = await rpc_call("getblockchaininfo")
        height = int(chain["blocks"])
        stats = await rpc_call("getblockstats", [height, ["subsidy"]])
        subsidy_btc = float(stats["subsidy"]) / 1e8
        if subsidy_btc <= 0:
            logger.error(
                f"Block subsidy is zero at height {height}; cannot fund wallet "
                f"{wallet}. Recreate the chain with `docker compose down -v`."
            )
            return False

        deficit = target_btc - balance
        # Mine the coinbases needed to cover the deficit, plus 100 for maturity.
        needed_mature = max(1, math.ceil(deficit / subsidy_btc))
        await rpc_call(
            "generatetoaddress", [needed_mature + 100, miner_address], wallet=wallet
        )

    info = await rpc_call("getwalletinfo", wallet=wallet)
    funded = float(info.get("balance", 0)) >= target_btc
    if not funded:
        logger.error(
            f"Could not fund Core wallet {wallet} to {target_btc} BTC after "
            f"{max_rounds} rounds (balance={info.get('balance', 0)})"
        )
    return funded
