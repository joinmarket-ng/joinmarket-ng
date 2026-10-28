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

The test checks wallet history enumeration without depending on a particular
RPC implementation. The co-spend workload preserves the expensive case for
the legacy grouping-based implementation.

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

import math
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from loguru import logger

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


@pytest_asyncio.fixture
async def setup_rpc_client(
    bitcoin_rpc_config: dict[str, str],
) -> AsyncIterator[httpx.AsyncClient]:
    """Reuse setup connections within this test's event loop, including cleanup."""
    async with httpx.AsyncClient(
        base_url=bitcoin_rpc_config["rpc_url"].rstrip("/") + "/",
        auth=(bitcoin_rpc_config["rpc_user"], bitcoin_rpc_config["rpc_password"]),
        timeout=_RPC_TIMEOUT,
    ) as client:
        yield client


async def _rpc(
    client: httpx.AsyncClient,
    method: str,
    params: list[Any] | None = None,
    wallet: str | None = None,
) -> Any:
    url = f"wallet/{wallet}" if wallet else ""
    payload = {
        "jsonrpc": "1.0",
        "id": "jmng-perf",
        "method": method,
        "params": params or [],
    }
    response = await client.post(url, json=payload)
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"{method} RPC error: {data['error']}")
    return data.get("result")


async def _ensure_wallet(client: httpx.AsyncClient, name: str) -> None:
    """Create or load a regtest wallet, idempotently."""
    wallets = await _rpc(client, "listwallets")
    if name in wallets:
        return
    listed = await _rpc(client, "listwalletdir")
    if any(w.get("name") == name for w in listed.get("wallets", [])):
        await _rpc(client, "loadwallet", [name])
        return
    # Core 30 only supports descriptor wallets; ``descriptors`` defaults
    # to True. Bare ``createwallet name`` is the only form that works.
    await _rpc(client, "createwallet", [name])


async def _mine(
    client: httpx.AsyncClient, addr: str, n: int, miner_wallet: str
) -> None:
    await _rpc(client, "generatetoaddress", [n, addr], wallet=miner_wallet)


async def _setup_funded_miner(
    client: httpx.AsyncClient, miner_wallet: str, target_btc: float = 8.0
) -> str:
    """Return a freshly generated miner address with mature funds.

    Tries to send from the pre-funded ``test-funder`` Core wallet first
    (mines only 1 confirmation block).  Falls back to coinbase mining in
    batches of 100 blocks when test-funder is not available.
    """
    miner_addr = await _rpc(
        client, "getnewaddress", ["", "bech32"], wallet=miner_wallet
    )

    # Fast path: fund from test-funder (1 confirmation block).
    try:
        await _rpc(
            client,
            "sendtoaddress",
            [miner_addr, target_btc + 1.0],
            wallet="test-funder",
        )
        await _mine(client, miner_addr, 1, miner_wallet)
    except Exception as exc:
        logger.warning("Performance setup could not use test-funder: {}", exc)

    # Modern Core reports spendable balance through getbalance, not getwalletinfo.
    # Check again after the final allowed mining round before reporting failure.
    max_mining_rounds = 50
    balance = 0.0
    for mining_round in range(max_mining_rounds + 1):
        result = await _rpc(client, "getbalance", wallet=miner_wallet)
        if (
            not isinstance(result, (int, float))
            or isinstance(result, bool)
            or not math.isfinite(result)
            or result < 0
        ):
            raise RuntimeError("Bitcoin Core returned an invalid spendable balance")
        balance = float(result)
        if balance >= target_btc:
            return miner_addr
        if mining_round == max_mining_rounds:
            break

        chain = await _rpc(client, "getblockchaininfo")
        stats = await _rpc(client, "getblockstats", [chain["blocks"], ["subsidy"]])
        subsidy = stats.get("subsidy")
        if not isinstance(subsidy, int) or isinstance(subsidy, bool) or subsidy < 0:
            raise RuntimeError("Bitcoin Core returned an invalid block subsidy")
        if subsidy == 0:
            raise RuntimeError(
                "Cannot fund performance-test wallet: regtest subsidy is exhausted. "
                "Use a fresh isolated test chain or a funded test-funder wallet."
            )
        await _mine(client, miner_addr, 100, miner_wallet)

    raise RuntimeError(
        f"Performance-test miner has {balance:.8f} BTC, needs {target_btc:.8f} BTC "
        f"after {max_mining_rounds} mining rounds"
    )


async def _derive_address_pool(
    client: httpx.AsyncClient, wallet: str, count: int
) -> deque[str]:
    """Prederive owned addresses without one SQLite commit per address.

    Only use this for these isolated test wallets. Derivation does not reserve
    indexes in Core: consume each returned pool once, and do not mix it with
    getnewaddress calls. Funding obtains the miner address before pools exist.
    """
    # Leave Core's normal lookahead available as receiving advances next_index.
    await _rpc(client, "keypoolrefill", [count + 1000], wallet=wallet)
    info = await _rpc(client, "listdescriptors", wallet=wallet)
    descriptors = [
        descriptor
        for descriptor in info["descriptors"]
        if descriptor.get("active")
        and not descriptor.get("internal")
        and descriptor["desc"].startswith("wpkh(")
    ]
    if len(descriptors) != 1:
        raise RuntimeError(
            "Performance setup needs one active external wpkh descriptor"
        )
    descriptor = descriptors[0]
    start = descriptor.get("next_index", descriptor.get("next"))
    if not isinstance(start, int) or isinstance(start, bool) or start < 0:
        raise RuntimeError("Performance setup descriptor has an invalid next index")

    addresses: list[str] = []
    for first in range(start, start + count, 500):
        last = min(first + 499, start + count - 1)
        batch = await _rpc(
            client, "deriveaddresses", [descriptor["desc"], [first, last]]
        )
        if (
            not isinstance(batch, list)
            or len(batch) != last - first + 1
            or any(not isinstance(address, str) or not address for address in batch)
        ):
            raise RuntimeError("Performance setup derived an invalid address batch")
        addresses.extend(batch)
    if len(set(addresses)) != count:
        raise RuntimeError("Performance setup derived duplicate addresses")
    return deque(addresses)


async def _send_many_receives(
    client: httpx.AsyncClient,
    n: int,
    batch_size: int,
    miner_addr: str,
    miner_wallet: str,
    addresses: deque[str],
) -> None:
    """
    Generate ``n`` distinct history-bearing addresses on the test wallet,
    funded via batched ``sendmany`` calls from the miner wallet.

    Batching reduces tx count by ``batch_size`` versus a per-address
    ``sendtoaddress`` loop, which is the difference between a few seconds
    and many minutes when ``n`` is in the thousands. Each output still
    lands at a distinct wallet-owned address so the wallet ends up with ``n``
    addresses that have history; the legacy ``listaddressgroupings`` path
    would still have to walk every such output.
    """
    sent = 0
    while sent < n:
        targets = {
            addresses.popleft(): 0.0001 for _ in range(min(batch_size, n - sent))
        }
        await _rpc(client, "sendmany", ["", targets], wallet=miner_wallet)
        sent += len(targets)
        await _mine(client, miner_addr, 1, miner_wallet)
    await _mine(client, miner_addr, 6, miner_wallet)


async def _ensure_miner_utxos(
    client: httpx.AsyncClient,
    miner_addr: str,
    miner_wallet: str,
    count: int,
    addresses: deque[str],
    value: float = 0.01,
) -> None:
    """Pre-split miner coins so CoinJoin sims have enough small inputs."""
    have = await _rpc(
        client,
        "listunspent",
        [1, 9999999, [], True, {"minimumAmount": value, "maximumAmount": value * 2}],
        wallet=miner_wallet,
    )
    if len(have) >= count:
        return
    batch = 200
    sent = 0
    while sent < count:
        targets = {addresses.popleft(): value for _ in range(min(batch, count - sent))}
        await _rpc(client, "sendmany", ["", targets], wallet=miner_wallet)
        sent += len(targets)
        await _mine(client, miner_addr, 1, miner_wallet)
    await _mine(client, miner_addr, 6, miner_wallet)


async def _make_coinjoin_like(
    client: httpx.AsyncClient,
    n: int,
    participants: int,
    miner_addr: str,
    miner_wallet: str,
    test_wallet: str,
    test_addresses: deque[str],
    miner_addresses: deque[str],
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
    targets = {test_addresses.popleft(): 0.01 for _ in range(n + 10)}
    await _rpc(client, "sendmany", ["", targets], wallet=miner_wallet)
    await _mine(client, miner_addr, 6, miner_wallet)

    test_utxos = await _rpc(
        client,
        "listunspent",
        [1, 9999999, [], True, {"minimumAmount": 0.009}],
        wallet=test_wallet,
    )
    test_utxos = [u for u in test_utxos if u["amount"] >= 0.009]
    miner_utxos = await _rpc(
        client,
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
            pool = test_addresses if j % 2 == 0 else miner_addresses
            addr = pool.popleft()
            outputs[addr] = cj_amount
        change = in_value - cj_amount * participants - 0.00005
        if change > 0:
            change_addr = miner_addresses.popleft()
            outputs[change_addr] = round(change, 8)

        raw = await _rpc(client, "createrawtransaction", [inputs, outputs])
        s1 = await _rpc(
            client, "signrawtransactionwithwallet", [raw], wallet=miner_wallet
        )
        s2 = await _rpc(
            client, "signrawtransactionwithwallet", [s1["hex"]], wallet=test_wallet
        )
        if not s2.get("complete"):
            continue
        await _rpc(client, "sendrawtransaction", [s2["hex"]])
        if k % 20 == 0:
            await _mine(client, miner_addr, 1, miner_wallet)
    await _mine(client, miner_addr, 6, miner_wallet)


@pytest.mark.asyncio
@pytest.mark.slow
async def test_get_addresses_with_history_scales_on_large_wallet(
    bitcoin_rpc_config: dict[str, str],
    setup_rpc_client: httpx.AsyncClient,
) -> None:
    """
    Regression test for wallet history enumeration without input-side grouping.

    This test fails if the call ever takes longer than
    ``HISTORY_RPC_BUDGET_SECONDS`` on a wallet with several hundred receive
    transactions plus dozens of CoinJoin-like co-spend transactions, which
    is the workload that motivated the fix.
    """
    cfg = bitcoin_rpc_config
    client = setup_rpc_client
    setup_started = time.perf_counter()
    # Per-run wallet names so concurrent runs (e.g. parallel test workers)
    # do not collide. Bitcoin Core regtest will keep these wallets across
    # tests, but the test only cares about its own wallets.
    suffix = secrets.token_hex(4)
    test_wallet = f"jmng_perf_test_{suffix}"
    miner_wallet = f"jmng_perf_miner_{suffix}"

    await _ensure_wallet(client, miner_wallet)
    await _ensure_wallet(client, test_wallet)

    try:
        miner_addr = await _setup_funded_miner(client, miner_wallet)
        logger.info(
            "History perf setup: funded miner after {:.2f}s",
            time.perf_counter() - setup_started,
        )
        miner_input_count = NUM_COINJOINS * (COINJOIN_PARTICIPANTS - 1) + 50
        test_addresses = await _derive_address_pool(
            client,
            test_wallet,
            NUM_RECEIVE_ADDRESSES
            + (NUM_COINJOINS + 10)  # Prefunding inputs.
            + NUM_COINJOINS * ((COINJOIN_PARTICIPANTS + 1) // 2),
        )
        miner_addresses = await _derive_address_pool(
            client,
            miner_wallet,
            miner_input_count + NUM_COINJOINS * (COINJOIN_PARTICIPANTS // 2 + 1),
        )
        logger.info(
            "History perf setup: address pools after {:.2f}s",
            time.perf_counter() - setup_started,
        )
        await _send_many_receives(
            client,
            NUM_RECEIVE_ADDRESSES,
            RECEIVES_PER_BATCH,
            miner_addr,
            miner_wallet,
            test_addresses,
        )
        logger.info(
            "History perf setup: receive workload after {:.2f}s",
            time.perf_counter() - setup_started,
        )
        await _ensure_miner_utxos(
            client,
            miner_addr,
            miner_wallet,
            miner_input_count,
            miner_addresses,
        )
        logger.info(
            "History perf setup: miner inputs after {:.2f}s",
            time.perf_counter() - setup_started,
        )
        await _make_coinjoin_like(
            client,
            NUM_COINJOINS,
            COINJOIN_PARTICIPANTS,
            miner_addr,
            miner_wallet,
            test_wallet,
            test_addresses,
            miner_addresses,
        )
        logger.info(
            "History perf setup: CoinJoin workload after {:.2f}s",
            time.perf_counter() - setup_started,
        )

        info = await _rpc(client, "getwalletinfo", wallet=test_wallet)
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
            logger.info(
                "History perf query: {} addresses in {:.3f}s", len(addresses), elapsed
            )

            # Correctness: the wallet generated many fresh receive
            # addresses; we should see at least the receive count back
            # (CoinJoin outputs add more on top, change addresses too).
            assert len(addresses) >= NUM_RECEIVE_ADDRESSES, (
                f"expected at least {NUM_RECEIVE_ADDRESSES} addresses with "
                f"history, got {len(addresses)}"
            )

            # Performance: hard budget. The current implementation is
            # well under 1s for this dataset; 10s is the regression
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
                await _rpc(client, "unloadwallet", [wallet])
            except Exception:
                pass
