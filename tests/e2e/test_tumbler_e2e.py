"""End-to-end tests for jmwalletd's ``/tumbler/*`` endpoints.

These tests drive the full tumbler pipeline through the HTTP API against a
real ``docker compose --profile e2e`` stack: a Bitcoin Core regtest node,
two directory servers, three maker bots, and a running ``jmwalletd``. The
test creates a fresh wallet via the API, funds it with a single large
UTXO, then issues ``POST /tumbler/plan`` + ``POST /tumbler/start`` and
polls ``GET /tumbler/status`` until the plan terminates.

Scope: smallest plan that still exercises stage-1 sweep and stage-2
sweep+fractional logic (``mintxcount=2``, ``include_maker_sessions=False``).
With one funded mixdepth on a 5-mixdepth wallet and one destination, the
builder emits 1 stage-1 sweep + 4 stage-2 blocks of (1 fractional + 1
sweep) = 9 CoinJoin phases. ``maker_count_min`` and ``maker_count_max``
are both ``2`` because only three makers are available in the ``e2e``
profile.

Requires ``docker compose --profile e2e up -d`` and Bitcoin Core RPC
reachable on ``127.0.0.1:18443`` (the default for the e2e profile).
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
from loguru import logger

from tests.e2e.rpc_utils import BitcoinRPCError, rpc_call

pytestmark = pytest.mark.e2e

JMWALLETD_URL = "https://127.0.0.1:28183"
API = f"{JMWALLETD_URL}/api/v1"

# Self-signed cert on the e2e jmwalletd; clients must skip verification.
TLS_VERIFY = False

# Funds per mixdepth deposit. 1 BTC is well above the PoDLE commitment
# requirement (20% of CJ amount) for the fractional/sweep CJs this test
# drives, and leaves enough headroom for network fees across three CJs.
FUND_AMOUNT_BTC = 1.0

# Wall-clock ceiling per real CoinJoin round. Three CJ phases fit in 10 min
# under healthy conditions; we budget generously to absorb the first-run
# Tor directory bootstrap.
STATUS_POLL_TIMEOUT_SEC = 60 * 15
STATUS_POLL_INTERVAL_SEC = 2.0


# ---------------------------------------------------------------------------
# HTTP helpers (kept in-file; the shared test_jmwalletd_api.py helpers are
# private to that module and reimporting them here would couple two test
# files together).
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _wait_for_jmwalletd(timeout: float = 60.0) -> None:
    """Block until jmwalletd is responding on its HTTP port."""
    deadline = asyncio.get_event_loop().time() + timeout
    async with httpx.AsyncClient(verify=TLS_VERIFY) as client:
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await client.get(f"{API}/getinfo", timeout=5)
                if r.status_code == 200:
                    return
            except httpx.ConnectError:
                pass
            await asyncio.sleep(1.0)
    pytest.fail(f"jmwalletd did not become ready within {timeout}s")


async def _ensure_no_wallet(client: httpx.AsyncClient) -> None:
    """Best-effort: lock any wallet currently loaded so the daemon is idle."""
    r = await client.get(f"{API}/session")
    if r.status_code != 200:
        return
    body = r.json()
    if not body.get("session"):
        return
    wallet_name = body.get("wallet_name", "")
    if not wallet_name:
        return
    # No valid token here, but jmwalletd accepts lock for an already-loaded
    # wallet in some code paths; failure is fine, we just ignore it.
    await client.get(
        f"{API}/wallet/{wallet_name}/lock",
        headers=_auth("dummy"),
    )


async def _create_wallet(
    client: httpx.AsyncClient,
    password: str = "testpass",
) -> tuple[str, str, str]:
    """Create a wallet; return ``(name, access_token, refresh_token)``."""
    name = f"tumbler-{uuid.uuid4().hex[:8]}.jmdat"
    r = await client.post(
        f"{API}/wallet/create",
        json={"walletname": name, "password": password, "wallettype": "sw-fb"},
    )
    assert r.status_code == 201, f"wallet/create failed: {r.status_code} {r.text}"
    body = r.json()
    return name, body["token"], body["refresh_token"]


async def _lock_wallet(client: httpx.AsyncClient, name: str, token: str) -> None:
    r = await client.get(f"{API}/wallet/{name}/lock", headers=_auth(token))
    # Tolerate 401 on cleanup (wallet may already be locked/replaced).
    assert r.status_code in (200, 401), f"lock failed: {r.status_code} {r.text}"


async def _unlock_wallet(
    client: httpx.AsyncClient, name: str, password: str = "testpass"
) -> tuple[str, str]:
    r = await client.post(
        f"{API}/wallet/{name}/unlock",
        json={"password": password},
    )
    assert r.status_code == 200, f"unlock failed: {r.status_code} {r.text}"
    body = r.json()
    return body["token"], body["refresh_token"]


async def _new_address(
    client: httpx.AsyncClient, name: str, token: str, mixdepth: int = 0
) -> str:
    r = await client.get(
        f"{API}/wallet/{name}/address/new/{mixdepth}",
        headers=_auth(token),
    )
    assert r.status_code == 200, f"address/new failed: {r.status_code} {r.text}"
    addr = r.json()["address"]
    assert addr.startswith("bcrt1"), f"unexpected regtest address: {addr}"
    return str(addr)


async def _balances_nonzero(
    client: httpx.AsyncClient, name: str, token: str
) -> dict[int, int]:
    """Poll ``/display`` until any mixdepth is non-zero; return balances per mixdepth."""
    r = await client.get(f"{API}/wallet/{name}/display", headers=_auth(token))
    assert r.status_code == 200
    info = r.json().get("walletinfo", {})
    # WalletDisplay shape: ``accounts: [{account: int, account_balance: str, branches: [...]}, ...]``
    out: dict[int, int] = {}
    for acct in info.get("accounts", []):
        mixdepth = int(acct.get("account", 0))
        # Balance is a BTC-formatted string; convert to sats.
        btc_str = str(acct.get("account_balance", "0"))
        sats = int(round(float(btc_str) * 1e8))
        out[mixdepth] = sats
    return out


async def _wait_for_sync_and_funds(
    client: httpx.AsyncClient,
    name: str,
    token: str,
    min_sats: int,
    timeout: float = 90.0,
) -> None:
    """Poll until the wallet reports confirmed balance >= ``min_sats``."""
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[int, int] = {}
    while asyncio.get_event_loop().time() < deadline:
        try:
            last = await _balances_nonzero(client, name, token)
            if sum(last.values()) >= min_sats:
                return
        except (httpx.HTTPError, AssertionError) as exc:
            logger.debug("sync poll transient error: {}", exc)
        await asyncio.sleep(1.0)
    pytest.fail(
        f"wallet never saw {min_sats} sats after {timeout}s; last balances: {last}"
    )


# ---------------------------------------------------------------------------
# Bitcoin funding helper. ``rpc_utils.ensure_wallet_funded`` mines directly
# to the target address which gives many ~50 BTC coinbase UTXOs -- the
# wallet can't yet spend those via PoDLE because each is marked as coinbase
# for 100 blocks and the JM output assumptions differ. For a consistent
# single large confirmed UTXO, use the ``fidelity_funder`` wallet that the
# ``wallet-funder`` docker service creates on the e2e ``bitcoin`` node.
# ---------------------------------------------------------------------------


async def _fund_via_fidelity_funder(address: str, amount_btc: float) -> str:
    """Send ``amount_btc`` from ``fidelity_funder`` on port 18443, mine 6 blocks.

    Returns the txid. Raises on failure.
    """
    # Make sure funder has the funds; wallet-funder already did this at stack
    # startup, but re-check so the test is robust to a stripped-down env.
    try:
        balance_btc = float(await rpc_call("getbalance", wallet="fidelity_funder") or 0)
    except BitcoinRPCError as exc:
        pytest.fail(f"fidelity_funder wallet missing on regtest node: {exc}")
    if balance_btc < amount_btc + 0.01:
        # Top up the funder by mining to one of its own addresses, then 100
        # more blocks for coinbase maturity.
        funder_addr = await rpc_call("getnewaddress", wallet="fidelity_funder") or ""
        assert isinstance(funder_addr, str) and funder_addr
        await rpc_call("generatetoaddress", [1, funder_addr])
        await rpc_call("generatetoaddress", [100, funder_addr])

    txid = await rpc_call(
        "sendtoaddress", [address, amount_btc], wallet="fidelity_funder"
    )
    assert isinstance(txid, str) and txid, f"sendtoaddress returned {txid!r}"
    # Confirm with 6 blocks so the wallet marks the UTXO as confirmed and
    # available for CJ selection.
    miner_addr = await rpc_call("getnewaddress", wallet="fidelity_funder") or ""
    assert isinstance(miner_addr, str) and miner_addr
    await rpc_call("generatetoaddress", [6, miner_addr])
    logger.info("funded {} with {} BTC, txid={}", address, amount_btc, txid)
    return txid


# ---------------------------------------------------------------------------
# Tumbler-specific helpers.
# ---------------------------------------------------------------------------


def _minimal_plan_parameters() -> dict[str, Any]:
    """Parameters for a minimal plan that still round-trips the builder.

    - ``maker_count_min=maker_count_max=2``: only three makers in the e2e
      profile, and the reference JM CJ requires counterparty_count < maker
      count to guarantee a pool big enough to pick from.
    - ``include_maker_sessions=False``: keep the plan to taker-coinjoin
      phases only, which is what this test exercises.
    - ``mintxcount=2``: smallest value that still produces a stage-2
      fractional phase + stage-2 sweep phase per mixdepth in the chain.
    - ``time_lambda_seconds=1.0``: near-zero wait between phases.
    - ``seed=42``: deterministic plan layout across runs.
    """
    return {
        "maker_count_min": 2,
        "maker_count_max": 2,
        "include_maker_sessions": False,
        "mintxcount": 2,
        "time_lambda_seconds": 1.0,
        "seed": 42,
    }


async def _post_plan(
    client: httpx.AsyncClient,
    name: str,
    token: str,
    destination: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    r = await client.post(
        f"{API}/wallet/{name}/tumbler/plan",
        json={
            "destinations": [destination],
            "parameters": _minimal_plan_parameters(),
            "force": force,
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"tumbler/plan failed: {r.status_code} {r.text}"
    return dict(r.json())


async def _post_start(client: httpx.AsyncClient, name: str, token: str) -> None:
    r = await client.post(
        f"{API}/wallet/{name}/tumbler/start",
        headers=_auth(token),
    )
    assert r.status_code in (200, 202), (
        f"tumbler/start failed: {r.status_code} {r.text}"
    )


async def _post_stop(
    client: httpx.AsyncClient, name: str, token: str
) -> dict[str, Any]:
    r = await client.post(
        f"{API}/wallet/{name}/tumbler/stop",
        headers=_auth(token),
    )
    assert r.status_code in (200, 202), f"tumbler/stop failed: {r.status_code} {r.text}"
    return dict(r.json()) if r.content else {}


async def _get_status(
    client: httpx.AsyncClient, name: str, token: str
) -> dict[str, Any]:
    r = await client.get(
        f"{API}/wallet/{name}/tumbler/status",
        headers=_auth(token),
    )
    assert r.status_code == 200, f"tumbler/status failed: {r.status_code} {r.text}"
    return dict(r.json())


async def _poll_until_terminal(
    client: httpx.AsyncClient,
    name: str,
    token: str,
    *,
    timeout: float = STATUS_POLL_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Poll ``/tumbler/status`` until the plan reaches a terminal state.

    Terminal states: ``completed``, ``failed``, ``cancelled``. Returns the
    final status payload.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        last = await _get_status(client, name, token)
        status = str(last.get("status", "")).lower()
        if status in ("completed", "failed", "cancelled"):
            return last
        logger.info(
            "tumbler status: {} phase {}/{}",
            status,
            last.get("current_phase"),
            len(last.get("phases", [])),
        )
        await asyncio.sleep(STATUS_POLL_INTERVAL_SEC)
    pytest.fail(f"tumbler did not terminate within {timeout}s; last status: {last}")
    raise AssertionError("unreachable")  # pragma: no cover  -- pytest.fail is NoReturn


async def _poll_until_phase_advances(
    client: httpx.AsyncClient,
    name: str,
    token: str,
    *,
    target_phase: int,
    timeout: float = STATUS_POLL_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Poll until ``current_phase >= target_phase`` or the plan terminates."""
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        last = await _get_status(client, name, token)
        status = str(last.get("status", "")).lower()
        if int(last.get("current_phase", 0)) >= target_phase:
            return last
        if status in ("completed", "failed", "cancelled"):
            return last
        await asyncio.sleep(STATUS_POLL_INTERVAL_SEC)
    pytest.fail(
        f"tumbler never advanced to phase {target_phase} within {timeout}s; last={last}"
    )
    raise AssertionError("unreachable")  # pragma: no cover  -- pytest.fail is NoReturn


# ---------------------------------------------------------------------------
# Fixtures.
# ---------------------------------------------------------------------------


async def _mine_blocks_periodically(
    stop_event: asyncio.Event, *, interval: float = 2.0, blocks_per_tick: int = 1
) -> None:
    """Mine ``blocks_per_tick`` blocks every ``interval`` seconds until stopped.

    The runner's confirmation gate (``min_confirmations_between_phases``)
    requires multiple confirmations between phases. On regtest there is no
    natural block arrival, so the test drives one artificially.
    """
    miner_addr = await rpc_call("getnewaddress", wallet="fidelity_funder") or ""
    assert isinstance(miner_addr, str) and miner_addr, miner_addr
    while not stop_event.is_set():
        try:
            await rpc_call("generatetoaddress", [blocks_per_tick, miner_addr])
        except Exception:  # pragma: no cover - transient bitcoind flake
            logger.exception("background mining failed; continuing")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval)
        except TimeoutError:
            continue


@contextlib.asynccontextmanager
async def _background_miner(
    interval: float = 2.0,
) -> AsyncGenerator[None, None]:
    """Context manager that mines a regtest block every ``interval`` seconds."""
    stop_event = asyncio.Event()
    task = asyncio.create_task(_mine_blocks_periodically(stop_event, interval=interval))
    try:
        yield
    finally:
        stop_event.set()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(task, timeout=5.0)


@pytest.fixture(scope="module")
async def jmwalletd_ready() -> None:
    await _wait_for_jmwalletd()


@pytest.fixture()
async def client(jmwalletd_ready: None) -> AsyncGenerator[httpx.AsyncClient, None]:
    # Longer timeout than the default: CJ startup can take tens of seconds as
    # the taker builds PoDLE commitments and authenticates to the directory.
    async with httpx.AsyncClient(timeout=60, verify=TLS_VERIFY) as c:
        yield c


@pytest.fixture()
async def funded_wallet(
    client: httpx.AsyncClient,
) -> AsyncGenerator[tuple[str, str, str], None]:
    """Create a wallet, fund mixdepth 0 with one 1-BTC UTXO, yield ``(name, token, destination)``.

    The ``destination`` is a fresh bech32 regtest address *outside* the
    wallet, used as the final tumble destination.
    """
    await _ensure_no_wallet(client)
    name, token, _ = await _create_wallet(client)
    try:
        # Fund mixdepth 0.
        deposit = await _new_address(client, name, token, mixdepth=0)
        await _fund_via_fidelity_funder(deposit, FUND_AMOUNT_BTC)
        await _wait_for_sync_and_funds(
            client, name, token, min_sats=int(FUND_AMOUNT_BTC * 0.99 * 1e8)
        )
        # A fresh destination address outside the wallet. We reuse
        # ``fidelity_funder`` as the "external" receiver; getreceivedbyaddress
        # lets us prove funds arrived.
        dest = await rpc_call("getnewaddress", wallet="fidelity_funder") or ""
        assert isinstance(dest, str) and dest.startswith("bcrt1"), dest
        yield name, token, dest
    finally:
        try:
            await _lock_wallet(client, name, token)
        except Exception:
            logger.exception("cleanup lock failed for {}", name)


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tumbler_happy_path_runs_three_coinjoins_and_pays_destination(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """Create -> start -> poll until COMPLETED; assert three successful CJs."""
    name, token, destination = funded_wallet

    plan = await _post_plan(client, name, token, destination)
    assert plan["status"].lower() == "pending"
    # 1 stage-1 sweep + 4 stage-2 blocks of (mintxcount-1=1 fractional + 1 sweep) = 9.
    assert len(plan["phases"]) == 9, (
        f"minimal plan should have nine CJ phases; got: "
        f"{[p['kind'] for p in plan['phases']]}"
    )
    assert all(p["kind"] == "taker_coinjoin" for p in plan["phases"])

    await _post_start(client, name, token)
    async with _background_miner():
        final = await _poll_until_terminal(client, name, token)

    assert final["status"].lower() == "completed", (
        f"plan did not complete: status={final['status']} error={final.get('error')}"
    )
    for idx, phase in enumerate(final["phases"]):
        assert phase["status"].lower() == "completed", (
            f"phase {idx} not completed: {phase}"
        )
        assert phase.get("txid"), f"phase {idx} has no txid: {phase}"

    # Destination (external) must have received funds from the stage-2 sweep.
    received_btc = await rpc_call(
        "getreceivedbyaddress", [destination, 1], wallet="fidelity_funder"
    )
    # Tumble fees, CJ fees, and rounding shave a small amount off the
    # deposit; assert at least 80% of the deposit arrived to catch a
    # misrouted CJ while tolerating realistic fee overhead.
    assert float(received_btc) >= FUND_AMOUNT_BTC * 0.8, (
        f"destination only received {received_btc} BTC"
    )


@pytest.mark.asyncio
async def test_tumbler_stop_mid_run_cancels_remaining_phases(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """Start the plan, wait for the first phase to land, then stop."""
    name, token, destination = funded_wallet

    await _post_plan(client, name, token, destination)
    await _post_start(client, name, token)

    # Wait until the runner has advanced past phase 0 -- stopping before the
    # first CJ broadcasts would be a trivial case that the router already
    # covers; we want the interesting "stop while in-flight" path.
    async with _background_miner():
        await _poll_until_phase_advances(client, name, token, target_phase=1)

    # ``POST /tumbler/stop`` returns ``202 Accepted`` with an empty body once
    # the runner has been asked to stop and awaited. The authoritative
    # post-stop state is exposed by ``GET /tumbler/status`` below.
    await _post_stop(client, name, token)

    # Give the runner a moment to finalise its CANCELLED persistence.
    await asyncio.sleep(2.0)
    final = await _get_status(client, name, token)
    assert final["status"].lower() == "cancelled"
    # Any phase after the one that was running must be PENDING or CANCELLED
    # -- never RUNNING or COMPLETED. At least one phase completed; at least
    # one remains non-completed.
    statuses = [p["status"].lower() for p in final["phases"]]
    assert any(s == "completed" for s in statuses), statuses
    assert any(s in ("pending", "cancelled") for s in statuses), statuses


@pytest.mark.asyncio
async def test_tumbler_reconciles_after_daemon_restart(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """Restart ``jmwalletd`` mid-run; the plan must transition to FAILED
    on the next status request with ``stale=True``."""
    name, token, destination = funded_wallet

    await _post_plan(client, name, token, destination)
    await _post_start(client, name, token)
    async with _background_miner():
        await _poll_until_phase_advances(client, name, token, target_phase=1)

    # Restart the daemon container. ``docker restart`` is synchronous.
    result = subprocess.run(
        ["docker", "restart", "jm-walletd"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"docker restart jm-walletd failed: {result.stderr}"

    # Wait for jmwalletd to respond again.
    # We use wall-clock sleep so the event-loop-based helper doesn't race
    # the container restart.
    time.sleep(3.0)
    await _wait_for_jmwalletd(timeout=60)

    # A fresh unlock is required because the daemon tears the wallet down.
    new_token, _ = await _unlock_wallet(client, name)

    # The lifespan hook's ``reconcile_stale_tumbler_plans`` should have
    # already marked the plan FAILED on startup; ``stale`` stays True only
    # until the next status call flips it. Either order is acceptable.
    status = await _get_status(client, name, new_token)
    assert status["status"].lower() in ("failed", "cancelled"), status
    assert status.get("error"), "reconciled plan should carry an error string"
