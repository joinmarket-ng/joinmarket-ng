"""End-to-end test for ``jmwallet info --scan-depth`` recovery (issue #475).

Background
==========

Wallets migrated from legacy joinmarket-clientserver often have used
address indices well past JM-NG's default descriptor lookahead window of
1000. Before the issue #475 fix, those funds were silently invisible
because:

  * The default ``[wallet].scan_range = 1000`` means Bitcoin Core only
    indexes addresses ``[0, 1000)`` per branch.
  * The ``--scan-depth`` flag was silently ignored on already-set-up
    wallets because ``is_descriptor_wallet_ready`` short-circuited the
    setup path.
  * The separate ``--rescan-deep`` flag was the only working recovery
    path, and users had to discover and combine it with ``--scan-depth``.

The fix collapses recovery into a single flag: passing
``--scan-depth N`` now forces a descriptor re-import at range N with
``check_existing=False`` and a synchronous rescan from genesis.

These tests use a real regtest bitcoind to exercise the recovery flow
end to end:

  * ``test_fresh_wallet_finds_shallow_funds``: a brand-new wallet with
    funds at index 5 is discovered with the default ``scan_range``.
  * ``test_deep_wallet_misses_funds_without_scan_depth``: a wallet with
    funds at index 2500 is set up with default ``scan_range=1000``, then
    re-opened without ``--scan-depth``. The funds are invisible. This
    documents the failure mode that motivated issue #475.
  * ``test_deep_wallet_recovers_with_scan_depth``: same deep wallet,
    but now re-opened with ``--scan-depth 3000``. The funds are found.

Requires: ``docker compose up -d`` (the default regtest bitcoind).
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from jmwallet.backends.descriptor_wallet import (
    DescriptorWalletBackend,
    generate_wallet_name,
    get_mnemonic_fingerprint,
)
from jmwallet.cli.mnemonic import generate_mnemonic_secure
from jmwallet.wallet.service import WalletService

pytestmark = pytest.mark.e2e


# Long timeout for bulk RPCs (mining to maturity, rescans). The shared
# ``tests.e2e.rpc_utils.rpc_call`` uses a 10s timeout which is too tight
# for descriptor re-imports + full rescans.
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
        "id": "jmng-475",
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
    """Create or load a regtest wallet on Core, idempotently."""
    wallets = await _rpc(cfg, "listwallets")
    if name in wallets:
        return
    listed = await _rpc(cfg, "listwalletdir")
    if any(w.get("name") == name for w in listed.get("wallets", [])):
        await _rpc(cfg, "loadwallet", [name])
        return
    await _rpc(cfg, "createwallet", [name])


async def _setup_funded_miner(cfg: dict[str, str], miner_wallet: str) -> str:
    """Return a freshly generated miner address with mature funds.

    Tries to send from the pre-funded ``test-funder`` Core wallet first
    (mines only 1 confirmation block).  Falls back to subsidy-aware coinbase
    mining when test-funder is not available.
    """
    await _ensure_wallet(cfg, miner_wallet)
    miner_addr = await _rpc(cfg, "getnewaddress", ["", "bech32"], wallet=miner_wallet)

    # Fast path: fund from test-funder (1 confirmation block).
    try:
        await _rpc(cfg, "sendtoaddress", [miner_addr, 2.0], wallet="test-funder")
        await _rpc(cfg, "generatetoaddress", [1, miner_addr], wallet=miner_wallet)
        return miner_addr
    except Exception:
        pass

    # Fallback: coinbase mining.
    import math

    target_btc = 1.0
    for _ in range(20):
        info = await _rpc(cfg, "getwalletinfo", wallet=miner_wallet)
        if float(info.get("balance", 0)) >= target_btc:
            break
        chain = await _rpc(cfg, "getblockchaininfo")
        height = int(chain["blocks"])
        stats = await _rpc(cfg, "getblockstats", [height, ["subsidy"]])
        subsidy_btc = float(stats["subsidy"]) / 1e8
        if subsidy_btc <= 0:
            raise RuntimeError(
                f"Block subsidy is zero at height {height}; recreate the chain "
                "with `docker compose down -v`."
            )
        deficit = target_btc - float(info.get("balance", 0))
        needed_mature = max(1, math.ceil(deficit / subsidy_btc))
        await _rpc(
            cfg,
            "generatetoaddress",
            [needed_mature + 100, miner_addr],
            wallet=miner_wallet,
        )
    return miner_addr


async def _fund_address(
    cfg: dict[str, str],
    miner_wallet: str,
    miner_addr: str,
    target_addr: str,
    amount: float = 0.01,
) -> None:
    """Send ``amount`` BTC to ``target_addr`` and mine 6 confirmations."""
    await _rpc(cfg, "sendtoaddress", [target_addr, amount], wallet=miner_wallet)
    await _rpc(cfg, "generatetoaddress", [6, miner_addr], wallet=miner_wallet)


@pytest_asyncio.fixture
async def isolated_miner(
    bitcoin_rpc_config: dict[str, str],
) -> AsyncGenerator[tuple[str, str], None]:
    """Per-test miner wallet on Core, returns ``(wallet_name, miner_addr)``.

    Using a unique wallet name per test avoids cross-test contamination of
    coinbase UTXOs and keeps the funding side hermetic.
    """
    miner_wallet = f"miner_{secrets.token_hex(4)}"
    miner_addr = await _setup_funded_miner(bitcoin_rpc_config, miner_wallet)
    yield miner_wallet, miner_addr
    # Don't unload; leaving the wallet around is harmless and avoids
    # interfering with concurrent tests on the same node.


def _make_unique_mnemonic() -> str:
    """Generate a fresh mnemonic per test so the Core wallet name is unique.

    The wallet name is derived from a SHA256 fingerprint of the mnemonic
    via ``generate_wallet_name``; without a unique mnemonic, a second test
    would reuse the first test's already-imported descriptors and the
    ``check_existing`` short-circuit would mask the behavior under test.
    """
    return generate_mnemonic_secure(word_count=12)


async def _new_jm_wallet(
    cfg: dict[str, str], mnemonic: str, scan_range: int = 1000
) -> tuple[WalletService, DescriptorWalletBackend]:
    """Build a fresh ``WalletService`` + ``DescriptorWalletBackend`` for ``mnemonic``."""
    fingerprint = get_mnemonic_fingerprint(mnemonic, "")
    wallet_name = generate_wallet_name(fingerprint, "regtest")
    backend = DescriptorWalletBackend(
        rpc_url=cfg["rpc_url"],
        rpc_user=cfg["rpc_user"],
        rpc_password=cfg["rpc_password"],
        wallet_name=wallet_name,
    )
    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network="regtest",
        mixdepth_count=5,
        scan_range=scan_range,
    )
    return wallet, backend


@pytest.mark.asyncio
async def test_fresh_wallet_finds_shallow_funds(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
    isolated_miner: tuple[str, str],
) -> None:
    """A fresh wallet with funds at a low index is discovered with the default scan_range.

    Sanity check: the default lookahead is sufficient for the common case.
    """
    miner_wallet, miner_addr = isolated_miner
    mnemonic = _make_unique_mnemonic()
    wallet, backend = await _new_jm_wallet(bitcoin_rpc_config, mnemonic)
    try:
        # Derive a low-index address and fund it before any descriptor setup.
        # ``get_receive_address`` does not need the backend to be set up;
        # it derives locally from the mnemonic.
        addr = wallet.get_receive_address(mixdepth=0, index=5)
        await _fund_address(
            bitcoin_rpc_config, miner_wallet, miner_addr, addr, amount=0.01
        )

        # Set up the descriptor wallet with the default scan_range (1000).
        # This is the path ``jm-wallet info`` takes on first run for a new
        # mnemonic.
        await wallet.setup_descriptor_wallet(scan_range=1000, rescan=True)
        await wallet.sync_with_descriptor_wallet()

        balance = await wallet.get_balance(mixdepth=0)
        assert balance == 1_000_000, f"expected 0.01 BTC at index 5, got {balance} sats"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_deep_wallet_misses_funds_without_scan_depth(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
    isolated_miner: tuple[str, str],
) -> None:
    """A wallet with funds at index 2500 is invisible at default ``scan_range=1000``.

    This documents the failure mode that motivated issue #475: legacy
    joinmarket-clientserver wallets routinely used indices past 1000, and
    JM-NG's default lookahead silently drops them.
    """
    miner_wallet, miner_addr = isolated_miner
    mnemonic = _make_unique_mnemonic()
    wallet, backend = await _new_jm_wallet(bitcoin_rpc_config, mnemonic)
    try:
        # Fund a deep index (2500) that sits beyond the default scan_range.
        deep_addr = wallet.get_receive_address(mixdepth=0, index=2500)
        await _fund_address(
            bitcoin_rpc_config, miner_wallet, miner_addr, deep_addr, amount=0.01
        )

        # Set up with default scan_range. Core can only see [0, 1000).
        await wallet.setup_descriptor_wallet(scan_range=1000, rescan=True)
        await wallet.sync_with_descriptor_wallet()

        balance = await wallet.get_balance(mixdepth=0)
        assert balance == 0, (
            "deep wallet should appear empty at default scan_range; "
            f"got {balance} sats which contradicts the issue #475 failure mode"
        )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_deep_wallet_recovers_with_scan_depth(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
    isolated_miner: tuple[str, str],
) -> None:
    """``--scan-depth N`` re-imports descriptors and finds funds beyond the default range.

    This is the issue #475 recovery path. The test simulates a migrated
    wallet: set up at default scan_range first (so the wallet is already
    "ready" on Core), then re-open with scan_depth=3000 which must
    bypass the short-circuit, re-import descriptors at the deeper range,
    and rescan from genesis.
    """
    miner_wallet, miner_addr = isolated_miner
    mnemonic = _make_unique_mnemonic()

    # Phase 1: fund a deep address, set up wallet at default scan_range
    # (mimics the migrated-wallet starting state where the JM-NG default
    # imported only [0, 1000) but the user actually has funds at 2500).
    wallet, backend = await _new_jm_wallet(bitcoin_rpc_config, mnemonic)
    try:
        deep_addr = wallet.get_receive_address(mixdepth=0, index=2500)
        await _fund_address(
            bitcoin_rpc_config, miner_wallet, miner_addr, deep_addr, amount=0.01
        )

        await wallet.setup_descriptor_wallet(scan_range=1000, rescan=True)
        await wallet.sync_with_descriptor_wallet()
        # Pre-condition: the deep funds are invisible at this point.
        assert await wallet.get_balance(mixdepth=0) == 0
    finally:
        await backend.close()

    # Phase 2: re-open the wallet with the deeper scan range. This is what
    # ``jm-wallet info --scan-depth 3000`` does: bypasses ``check_existing``,
    # re-imports descriptors with the new range, and rescans from genesis.
    wallet, backend = await _new_jm_wallet(bitcoin_rpc_config, mnemonic)
    try:
        # Same args the CLI uses when ``--scan-depth`` is set: force
        # re-import + full synchronous rescan.
        await wallet.setup_descriptor_wallet(
            scan_range=3000,
            rescan=True,
            check_existing=False,
            smart_scan=False,
            background_full_rescan=False,
        )
        await wallet.sync_with_descriptor_wallet()

        balance = await wallet.get_balance(mixdepth=0)
        assert balance == 1_000_000, (
            f"--scan-depth 3000 must recover the deep 0.01 BTC at index 2500; got {balance} sats"
        )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_core_range_limit_matches_constant(
    bitcoin_rpc_config: dict[str, str],
    ensure_blockchain_ready: None,
) -> None:
    """Pin ``MAX_DESCRIPTOR_RANGE`` to Bitcoin Core's actual range limit.

    Regression test for the "Range is too large" bug. Core's
    ``ParseDescriptorRange`` rejects a descriptor whose range span exceeds
    1,000,000 indices with ``code -8 "Range is too large"`` (the rejection is
    fast: it happens before any keypool expansion). JoinMarket NG clamps
    requested ranges down to ``MAX_DESCRIPTOR_RANGE - 1`` so the import is
    never rejected wholesale (which previously left the wallet without any
    coverage).

    Here we verify directly against a real node that ``high ==
    MAX_DESCRIPTOR_RANGE`` is exactly the first value Core rejects, locking
    our constant to Core's behavior. We deliberately do not assert the
    accept path at ``high == MAX_DESCRIPTOR_RANGE - 1``: Core would accept it,
    but expanding the 1,000,000-entry keypool takes well over a minute per
    descriptor, which is covered cheaply by the unit tests instead.
    """
    from jmwallet.backends.descriptor_wallet import MAX_DESCRIPTOR_RANGE

    mnemonic = _make_unique_mnemonic()
    wallet, backend = await _new_jm_wallet(bitcoin_rpc_config, mnemonic)
    try:
        await backend.create_wallet(disable_private_keys=True)
        xpub = wallet.get_account_xpub(0)
        info = await backend._rpc_call(
            "getdescriptorinfo", [f"wpkh({xpub}/0/*)"], use_wallet=False
        )
        desc = info["descriptor"]

        # high == MAX_DESCRIPTOR_RANGE is one past the largest accepted span
        # and must be rejected with the exact "Range is too large" error.
        result = await backend._rpc_call(
            "importdescriptors",
            [
                [
                    {
                        "desc": desc,
                        "range": [0, MAX_DESCRIPTOR_RANGE],
                        "timestamp": "now",
                        "active": False,
                    }
                ]
            ],
        )
        assert result[0]["success"] is False
        assert "Range is too large" in result[0]["error"]["message"], (
            f"unexpected error for over-limit range: {result[0]}"
        )
    finally:
        await backend.close()
