"""Verify retained discovery candidates against a replacement regtest branch."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from jmwallet.backends.neutrino import NeutrinoBackend
from jmwallet.wallet.service import WalletService
from mnemonic import Mnemonic

from tests.e2e.docker_utils import (
    docker_inspect_running,
    get_container_name,
    run_compose_cmd,
)
from tests.e2e.rpc_utils import TEST_FUNDER_WALLET, rpc_call

pytestmark = pytest.mark.neutrino


async def _wait_for_canonical_tip(backend: NeutrinoBackend) -> None:
    height = await rpc_call("getblockcount")
    expected_hash = await rpc_call("getbestblockhash")
    async with asyncio.timeout(180):
        while True:
            status = await backend._api_call("GET", "v1/status")
            if (
                status.get("block_height") == height
                and status.get("filter_height") == height
                and await backend.get_block_hash(height) == expected_hash
            ):
                return
            await asyncio.sleep(0.5)


async def _signed_payment(
    core_wallet: str, coin: dict[str, Any], address: str, amount: float
) -> str:
    raw = await rpc_call(
        "createrawtransaction",
        [
            [{"txid": coin["txid"], "vout": coin["vout"], "sequence": 4294967293}],
            {address: amount},
        ],
    )
    signed = await rpc_call("signrawtransactionwithwallet", [raw], wallet=core_wallet)
    assert signed["complete"]
    return str(signed["hex"])


@pytest.mark.timeout(420)
async def test_selected_inputs_reject_dropped_creation_but_accept_remined_control(
    neutrino_backend_fixture: NeutrinoBackend, tmp_path: Path
) -> None:
    backend = neutrino_backend_fixture
    wallet = WalletService(
        Mnemonic("english").generate(strength=128),
        backend,
        network="regtest",
        data_dir=tmp_path,
        mixdepth_count=1,
        gap_limit=2,
        scan_range=4,
        reconstruct_history=False,
    )
    core_wallet = f"input-reorg-{uuid.uuid4().hex}"
    created = False
    invalidated_block: str | None = None
    miner_was_running = docker_inspect_running(get_container_name("miner"))
    if miner_was_running:
        run_compose_cmd(["stop", "miner"])
    try:
        await rpc_call("createwallet", [core_wallet])
        created = True
        funding_addresses = [
            await rpc_call("getnewaddress", wallet=core_wallet) for _ in range(2)
        ]
        miner_address = await rpc_call("getnewaddress", wallet=core_wallet)
        await rpc_call(
            "sendmany",
            ["", dict.fromkeys(funding_addresses, 0.02)],
            wallet=TEST_FUNDER_WALLET,
        )
        await rpc_call("generatetoaddress", [1, miner_address])
        await _wait_for_canonical_tip(backend)
        await wallet.sync_mixdepth(0)

        coins = await rpc_call(
            "listunspent", [1, 9999999, funding_addresses], wallet=core_wallet
        )
        assert len(coins) == 2
        targets = [wallet.get_address(0, 0, index) for index in range(2)]
        payments = [
            await _signed_payment(core_wallet, coin, target, 0.0199)
            for coin, target in zip(coins, targets, strict=True)
        ]
        replacement = await _signed_payment(core_wallet, coins[0], miner_address, 0.019)
        txids = [
            await rpc_call("sendrawtransaction", [payment]) for payment in payments
        ]
        old_blocks = await rpc_call("generatetoaddress", [2, miner_address])
        await _wait_for_canonical_tip(backend)

        async with asyncio.timeout(180):
            while True:
                discovered = await wallet.sync_mixdepth(0)
                by_txid = {coin.txid: coin for coin in discovered}
                if all(txid in by_txid for txid in txids):
                    break
                await asyncio.sleep(0.5)
        dropped, control = [by_txid[txid] for txid in txids]
        assert await wallet.verify_coinjoin_inputs([dropped, control], 1) == set()

        # Invalidation alone would put both payments back into the mempool.
        # Replace only one, permanently removing its creation transaction.
        await rpc_call("invalidateblock", [old_blocks[0]])
        invalidated_block = old_blocks[0]
        assert set(txids) <= set(await rpc_call("getrawmempool"))
        await rpc_call("sendrawtransaction", [replacement])
        await rpc_call("generatetoaddress", [3, miner_address])
        assert await rpc_call("gettxout", [dropped.txid, dropped.vout]) is None
        assert await rpc_call("gettxout", [control.txid, control.vout]) is not None
        assert dropped.txid not in await rpc_call("getrawmempool")
        await _wait_for_canonical_tip(backend)

        # Deliberately retain the old wallet candidates, as warm wallets do.
        # Neither a restart nor a cache-repair rescan may be needed for safety.
        assert await wallet.verify_coinjoin_inputs([dropped], 1) == {
            (dropped.txid, dropped.vout)
        }
        assert await wallet.verify_coinjoin_inputs([control], 1) == set()
        assert control.confirmations == 3
    finally:
        try:
            if invalidated_block is not None:
                await rpc_call("reconsiderblock", [invalidated_block])
            if created:
                await rpc_call("unloadwallet", [core_wallet])
        finally:
            if miner_was_running:
                run_compose_cmd(["start", "miner"])
