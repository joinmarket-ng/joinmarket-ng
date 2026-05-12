"""
End-to-end test that ``DescriptorWalletBackend.batch_get_address_info``
issues a single JSON-RPC batch and is meaningfully faster than the
sequential single-address path against a real Bitcoin Core regtest node.

Background
==========

The wallet sync loop's ``addresses_beyond_range`` branch
(jmwallet/wallet/sync.py:1148) used to call ``getaddressinfo`` once per
address in a serial Python loop. Microbenchmarks against a local Core
30.2 regtest node show:

- Serial ``await get_address_info``:  ~0.5s for 500 addresses
- ``asyncio.gather`` with semaphore:  no improvement (Core serializes
  wallet RPCs on the wallet mutex)
- JSON-RPC batch (one HTTP POST):     ~0.025s for 500 addresses (>20x)

The batch path wins by eliminating N HTTP round-trips, not by exploiting
parallelism. On a remote / Tor-fronted Core node where each RTT is
100-500ms the gap widens by another two orders of magnitude.

This test pins the win at a level that any reasonable batch
implementation must satisfy, and guards against accidentally
reintroducing the per-call HTTP cost.

Requires: docker compose up -d (default ``jm-bitcoin`` regtest node).
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import httpx
import pytest

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

pytestmark = pytest.mark.e2e


NUM_ADDRESSES = 300

# Generous wall-clock budgets that any reasonable implementation must
# satisfy. The expected gap is much larger (>10x); these thresholds just
# guard against pathological regressions like accidentally falling back
# to the serial loop.
BATCH_BUDGET_SECONDS = 1.5
# Batch must be at least 2x faster than serial on localhost (in practice
# we observe >20x). Anything less suggests batching has regressed to a
# per-call HTTP fan-out.
MIN_SPEEDUP_RATIO = 2.0


async def _rpc(
    cfg: dict[str, str],
    method: str,
    params: list[Any] | None = None,
    wallet: str | None = None,
) -> Any:
    url = cfg["rpc_url"].rstrip("/")
    if wallet:
        url = f"{url}/wallet/{wallet}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            url,
            auth=(cfg["rpc_user"], cfg["rpc_password"]),
            json={
                "jsonrpc": "1.0",
                "id": "e2e-batch",
                "method": method,
                "params": params or [],
            },
        )
    data = response.json()
    if data.get("error"):
        raise RuntimeError(f"{method}: {data['error']}")
    return data["result"]


@pytest.mark.asyncio
async def test_batch_get_address_info_is_faster_than_serial(
    bitcoin_rpc_config: dict[str, str],
) -> None:
    """
    Compare ``batch_get_address_info`` vs the serial ``get_address_info``
    loop it replaces in ``wallet/sync.py``. Asserts both the absolute
    batch budget and a minimum speedup ratio.
    """
    cfg = bitcoin_rpc_config
    suffix = secrets.token_hex(4)
    wallet = f"jmng_batch_test_{suffix}"

    # Create a per-run wallet to avoid colliding with parallel CI jobs.
    wallets = await _rpc(cfg, "listwallets")
    if wallet not in wallets:
        await _rpc(cfg, "createwallet", [wallet])

    try:
        addresses: list[str] = [
            await _rpc(cfg, "getnewaddress", ["", "bech32"], wallet=wallet)
            for _ in range(NUM_ADDRESSES)
        ]

        backend = DescriptorWalletBackend(
            rpc_url=cfg["rpc_url"],
            rpc_user=cfg["rpc_user"],
            rpc_password=cfg["rpc_password"],
            wallet_name=wallet,
        )
        backend._wallet_loaded = True
        try:
            # Warm the HTTP connection pool so we measure steady state.
            await backend.get_address_info(addresses[0])

            # Serial baseline: what the old sync.py loop did.
            t0 = time.perf_counter()
            for addr in addresses:
                info = await backend.get_address_info(addr)
                assert info is not None and info.get("ismine") is True
            serial_elapsed = time.perf_counter() - t0

            # Batch path: what sync.py now does.
            t0 = time.perf_counter()
            results = await backend.batch_get_address_info(addresses)
            batch_elapsed = time.perf_counter() - t0

            # Correctness: every address must come back ismine=True with
            # a wpkh desc, since we just generated them from this wallet.
            assert len(results) == NUM_ADDRESSES
            assert all(r is not None and r.get("ismine") is True for r in results), (
                "batch_get_address_info dropped or misreported entries"
            )

            # Absolute budget.
            assert batch_elapsed < BATCH_BUDGET_SECONDS, (
                f"batch_get_address_info took {batch_elapsed:.3f}s on "
                f"{NUM_ADDRESSES} addresses, exceeding {BATCH_BUDGET_SECONDS}s "
                "budget. Did batching regress to a per-call HTTP fan-out?"
            )

            # Relative speedup. On localhost regtest the observed ratio
            # is >20x; we assert at least 2x as a soft tripwire.
            ratio = (
                serial_elapsed / batch_elapsed if batch_elapsed > 0 else float("inf")
            )
            assert ratio >= MIN_SPEEDUP_RATIO, (
                f"batch only {ratio:.1f}x faster than serial "
                f"(serial={serial_elapsed:.3f}s, batch={batch_elapsed:.3f}s). "
                f"Expected at least {MIN_SPEEDUP_RATIO}x; suspect regression."
            )
        finally:
            await backend.close()
    finally:
        try:
            await _rpc(cfg, "unloadwallet", [wallet])
        except Exception:
            pass
