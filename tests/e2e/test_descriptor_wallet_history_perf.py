"""
End-to-end performance regression test for
``DescriptorWalletBackend.get_addresses_with_history``.

Background
==========

Previously, ``get_addresses_with_history`` used Bitcoin Core's
``listaddressgroupings`` (with a ``listsinceblock`` fallback) to discover
the set of wallet-owned addresses that have ever received funds. On
descriptor wallets with many transactions (especially CoinJoin co-spends,
which inflate the input set processed by Core's union-find grouping),
``listaddressgroupings`` walks O(txs * (inputs + 2 * outputs)) with a
per-script ``IsMine`` check; this regularly timed out at 10 minutes for
real-world wallets, and the ``listsinceblock`` fallback added several more
minutes.

The current implementation calls only ``listreceivedbyaddress 0 false true``,
which iterates outputs once and groups by destination, with no input-side
scan. On regtest benchmarks, this is 2.7x to 13x faster than
``listaddressgroupings`` and >5x faster than ``listsinceblock``.

This test guards against regressions by:

  1. Building a descriptor test wallet on a local Bitcoin Core regtest node.
  2. Generating thousands of history-bearing addresses (via batched
     ``sendmany`` receives) plus a meaningful number of CoinJoin-like
     co-spend transactions (the worst case for the legacy RPC).
  3. Calling ``get_addresses_with_history`` and asserting both
     correctness and a generous wall-clock budget that any reasonable
     implementation must satisfy.

The target scale (~3k history-bearing addresses) approximates a heavy
real-world JoinMarket wallet (~13k addresses) closely enough to surface
any O(N^2)-ish regression, while keeping CI wall time well under the
300s per-test budget by using ``sendmany`` to amortize tx count.

Requires: docker compose up -d (the default ``jm-bitcoin`` regtest node).
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import httpx
import pytest

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

pytestmark = pytest.mark.e2e


# Test parameters. We aim to approximate a real-world heavy JoinMarket
# wallet (~13k addresses with history is not unusual after many mixes)
# while keeping CI wall time bounded. Receives are batched via ``sendmany``
# (hundreds of outputs per tx), which lets us build thousands of
# history-bearing addresses in a small number of txs.
NUM_RECEIVE_ADDRESSES = 3000
RECEIVES_PER_BATCH = 500
NUM_COINJOINS = 100
COINJOIN_PARTICIPANTS = 5

# Hard upper bound for ``get_addresses_with_history`` wall time. The
# expected value on a developer machine is well under 1s for this dataset
# (regtest benchmarks show ~0.05s for 3.4k txs and ~0.15s for 23.7k txs);
# 10s is generous enough to absorb CI noise without masking real
# regressions on a wallet with 3k+ history-bearing addresses.
HISTORY_RPC_BUDGET_SECONDS = 10.0


# Long timeout for the heavy setup RPCs (mining 110 blocks, ``sendmany``
# with hundreds of outputs, etc). The shared ``tests.e2e.rpc_utils.rpc_call``
# uses a 10s timeout which is too tight for this kind of bulk setup.
_RPC_TIMEOUT = 120.0


async def _rpc(
    cfg: dict[str, str],
    method: str,
    params: list[Any] | None = None,
    wallet: str | None = None,
) -> Any:
    url = cfg["rpc_url"].rstrip("/")
    if wallet:
        url = f"{url}/wallet/{wallet}"
    payload = {
        "jsonrpc": "1.0",
        "id": "jmng-perf",
        "method": method,
        "params": params or [],
    }
    async with httpx.AsyncClient(timeout=_RPC_TIMEOUT) as client:
        response = await client.post(
            url, auth=(cfg["rpc_user"], cfg["rpc_password"]), json=payload
        )
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"{method} RPC error: {data['error']}")
    return data.get("result")


async def _ensure_wallet(cfg: dict[str, str], name: str) -> None:
    """Create or load a regtest wallet, idempotently."""
    wallets = await _rpc(cfg, "listwallets")
    if name in wallets:
        return
    listed = await _rpc(cfg, "listwalletdir")
    if any(w.get("name") == name for w in listed.get("wallets", [])):
        await _rpc(cfg, "loadwallet", [name])
        return
    # Core 30 only supports descriptor wallets; ``descriptors`` defaults
    # to True. Bare ``createwallet name`` is the only form that works.
    await _rpc(cfg, "createwallet", [name])


async def _mine(cfg: dict[str, str], addr: str, n: int, miner_wallet: str) -> None:
    await _rpc(cfg, "generatetoaddress", [n, addr], wallet=miner_wallet)


async def _setup_funded_miner(
    cfg: dict[str, str], miner_wallet: str, target_btc: float = 8.0
) -> str:
    """Return a freshly generated miner address with mature funds.

    Tries to send from the pre-funded ``test-funder`` Core wallet first
    (mines only 1 confirmation block).  Falls back to coinbase mining in
    batches of 100 blocks when test-funder is not available.
    """
    miner_addr = await _rpc(cfg, "getnewaddress", ["", "bech32"], wallet=miner_wallet)

    # Fast path: fund from test-funder (1 confirmation block).
    try:
        await _rpc(
            cfg, "sendtoaddress", [miner_addr, target_btc + 1.0], wallet="test-funder"
        )
        await _mine(cfg, miner_addr, 1, miner_wallet)
        return miner_addr
    except Exception:
        pass

    # Fallback: coinbase mining in 100-block batches.
    for _ in range(50):
        info = await _rpc(cfg, "getwalletinfo", wallet=miner_wallet)
        if info.get("balance", 0) >= target_btc:
            break
        await _mine(cfg, miner_addr, 100, miner_wallet)
    return miner_addr


async def _send_many_receives(
    cfg: dict[str, str],
    n: int,
    batch_size: int,
    miner_addr: str,
    miner_wallet: str,
    test_wallet: str,
) -> None:
    """
    Generate ``n`` distinct history-bearing addresses on the test wallet,
    funded via batched ``sendmany`` calls from the miner wallet.

    Batching reduces tx count by ``batch_size`` versus a per-address
    ``sendtoaddress`` loop, which is the difference between a few seconds
    and many minutes when ``n`` is in the thousands. Each output still
    lands at a distinct ``getnewaddress`` so the wallet ends up with ``n``
    addresses that have history; the legacy ``listaddressgroupings`` path
    would still have to walk every such output.
    """
    sent = 0
    while sent < n:
        targets: dict[str, float] = {}
        for _ in range(min(batch_size, n - sent)):
            addr = await _rpc(cfg, "getnewaddress", ["", "bech32"], wallet=test_wallet)
            targets[addr] = 0.0001
        await _rpc(cfg, "sendmany", ["", targets], wallet=miner_wallet)
        sent += len(targets)
        await _mine(cfg, miner_addr, 1, miner_wallet)
    await _mine(cfg, miner_addr, 6, miner_wallet)


async def _ensure_miner_utxos(
    cfg: dict[str, str],
    miner_addr: str,
    miner_wallet: str,
    count: int,
    value: float = 0.01,
) -> None:
    """Pre-split miner coins so CoinJoin sims have enough small inputs."""
    have = await _rpc(
        cfg,
        "listunspent",
        [1, 9999999, [], True, {"minimumAmount": value, "maximumAmount": value * 2}],
        wallet=miner_wallet,
    )
    if len(have) >= count:
        return
    batch = 200
    sent = 0
    while sent < count:
        targets: dict[str, float] = {}
        for _ in range(min(batch, count - sent)):
            addr = await _rpc(cfg, "getnewaddress", ["", "bech32"], wallet=miner_wallet)
            targets[addr] = value
        await _rpc(cfg, "sendmany", ["", targets], wallet=miner_wallet)
        sent += len(targets)
        await _mine(cfg, miner_addr, 1, miner_wallet)
    await _mine(cfg, miner_addr, 6, miner_wallet)


async def _make_coinjoin_like(
    cfg: dict[str, str],
    n: int,
    participants: int,
    miner_addr: str,
    miner_wallet: str,
    test_wallet: str,
) -> None:
    """
    Craft ``n`` CoinJoin-like txs with co-spends between miner and test wallet.

    Each tx mixes 1 test-wallet input with (participants - 1) miner inputs
    and produces equal-value outputs split between both wallets plus a
    change output. This is the worst case for ``listaddressgroupings`` (it
    forces large union-find merges); we want the new implementation to be
    immune to this.
    """
    # Pre-fund the test wallet with enough small UTXOs.
    targets: dict[str, float] = {}
    for _ in range(n + 10):
        addr = await _rpc(cfg, "getnewaddress", ["", "bech32"], wallet=test_wallet)
        targets[addr] = 0.01
    await _rpc(cfg, "sendmany", ["", targets], wallet=miner_wallet)
    await _mine(cfg, miner_addr, 6, miner_wallet)

    test_utxos = await _rpc(
        cfg,
        "listunspent",
        [1, 9999999, [], True, {"minimumAmount": 0.009}],
        wallet=test_wallet,
    )
    test_utxos = [u for u in test_utxos if u["amount"] >= 0.009]
    miner_utxos = await _rpc(
        cfg,
        "listunspent",
        [1, 9999999, [], True, {"minimumAmount": 0.005, "maximumAmount": 0.02}],
        wallet=miner_wallet,
    )

    mi = 0
    ti = 0
    for k in range(n):
        if ti >= len(test_utxos) or mi + (participants - 1) > len(miner_utxos):
            break
        inputs: list[dict[str, Any]] = [
            {"txid": test_utxos[ti]["txid"], "vout": test_utxos[ti]["vout"]}
        ]
        in_value = test_utxos[ti]["amount"]
        ti += 1
        for _ in range(participants - 1):
            inputs.append(
                {"txid": miner_utxos[mi]["txid"], "vout": miner_utxos[mi]["vout"]}
            )
            in_value += miner_utxos[mi]["amount"]
            mi += 1

        cj_amount = 0.005
        outputs: dict[str, float] = {}
        for j in range(participants):
            wallet_for_output = test_wallet if j % 2 == 0 else miner_wallet
            addr = await _rpc(
                cfg, "getnewaddress", ["", "bech32"], wallet=wallet_for_output
            )
            outputs[addr] = cj_amount
        change = in_value - cj_amount * participants - 0.00005
        if change > 0:
            change_addr = await _rpc(
                cfg, "getnewaddress", ["", "bech32"], wallet=miner_wallet
            )
            outputs[change_addr] = round(change, 8)

        raw = await _rpc(cfg, "createrawtransaction", [inputs, outputs])
        s1 = await _rpc(cfg, "signrawtransactionwithwallet", [raw], wallet=miner_wallet)
        s2 = await _rpc(
            cfg, "signrawtransactionwithwallet", [s1["hex"]], wallet=test_wallet
        )
        if not s2.get("complete"):
            continue
        await _rpc(cfg, "sendrawtransaction", [s2["hex"]])
        if k % 20 == 0:
            await _mine(cfg, miner_addr, 1, miner_wallet)
    await _mine(cfg, miner_addr, 6, miner_wallet)


@pytest.mark.asyncio
@pytest.mark.slow
async def test_get_addresses_with_history_scales_on_large_wallet(
    bitcoin_rpc_config: dict[str, str],
) -> None:
    """
    Regression test for the listaddressgroupings -> listreceivedbyaddress
    migration in ``DescriptorWalletBackend.get_addresses_with_history``.

    This test fails if the call ever takes longer than
    ``HISTORY_RPC_BUDGET_SECONDS`` on a wallet with several hundred receive
    transactions plus dozens of CoinJoin-like co-spend transactions, which
    is the workload that motivated the fix.
    """
    cfg = bitcoin_rpc_config
    # Per-run wallet names so concurrent runs (e.g. parallel test workers)
    # do not collide. Bitcoin Core regtest will keep these wallets across
    # tests, but the test only cares about its own wallets.
    suffix = secrets.token_hex(4)
    test_wallet = f"jmng_perf_test_{suffix}"
    miner_wallet = f"jmng_perf_miner_{suffix}"

    await _ensure_wallet(cfg, miner_wallet)
    await _ensure_wallet(cfg, test_wallet)

    try:
        miner_addr = await _setup_funded_miner(cfg, miner_wallet)
        await _send_many_receives(
            cfg,
            NUM_RECEIVE_ADDRESSES,
            RECEIVES_PER_BATCH,
            miner_addr,
            miner_wallet,
            test_wallet,
        )
        await _ensure_miner_utxos(
            cfg,
            miner_addr,
            miner_wallet,
            NUM_COINJOINS * (COINJOIN_PARTICIPANTS - 1) + 50,
        )
        await _make_coinjoin_like(
            cfg,
            NUM_COINJOINS,
            COINJOIN_PARTICIPANTS,
            miner_addr,
            miner_wallet,
            test_wallet,
        )

        info = await _rpc(cfg, "getwalletinfo", wallet=test_wallet)
        # Sanity: confirm the wallet was actually populated. ``txcount``
        # only counts txs that touch the test wallet (one per ``sendmany``
        # batch + per-CoinJoin tx), so the threshold is much smaller than
        # ``NUM_RECEIVE_ADDRESSES``.
        expected_min_txs = (NUM_RECEIVE_ADDRESSES // RECEIVES_PER_BATCH) + (
            NUM_COINJOINS // 4
        )
        assert info["txcount"] >= expected_min_txs, (
            f"setup did not produce enough txs (got {info['txcount']}, "
            f"expected >= {expected_min_txs})"
        )

        backend = DescriptorWalletBackend(
            rpc_url=cfg["rpc_url"],
            rpc_user=cfg["rpc_user"],
            rpc_password=cfg["rpc_password"],
            wallet_name=test_wallet,
        )
        backend._wallet_loaded = True
        try:
            # Warm any client-side state; we measure the steady-state cost.
            _ = await backend.get_addresses_with_history()

            t0 = time.perf_counter()
            addresses = await backend.get_addresses_with_history()
            elapsed = time.perf_counter() - t0

            # Correctness: the wallet generated many fresh receive
            # addresses; we should see at least the receive count back
            # (CoinJoin outputs add more on top, change addresses too).
            assert len(addresses) >= NUM_RECEIVE_ADDRESSES, (
                f"expected at least {NUM_RECEIVE_ADDRESSES} addresses with "
                f"history, got {len(addresses)}"
            )

            # Performance: hard budget. The current implementation is
            # well under 1s for this dataset; 5s is the regression
            # tripwire.
            assert elapsed < HISTORY_RPC_BUDGET_SECONDS, (
                f"get_addresses_with_history took {elapsed:.2f}s on "
                f"{info['txcount']} txs / {len(addresses)} addresses, "
                f"exceeding the {HISTORY_RPC_BUDGET_SECONDS}s budget. "
                "Did get_addresses_with_history regress to "
                "listaddressgroupings or listsinceblock?"
            )
        finally:
            await backend.close()
    finally:
        # Best-effort cleanup so the regtest node does not accumulate
        # large wallets across many test runs.
        for wallet in (test_wallet, miner_wallet):
            try:
                await _rpc(cfg, "unloadwallet", [wallet])
            except Exception:
                pass
