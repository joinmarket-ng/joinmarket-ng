"""End-to-end behavioural test for the maker-session idle-timeout fallback.

Runs a real tumble through the first stage-1 sweep and into the maker
phase, with a very short idle timeout. Because nothing else in the e2e
compose stack initiates CoinJoins against our maker, ``cj_served`` never
advances and the phase must exit via the idle-timeout path (not the
``maker_session_seconds`` deadline, which we keep generous).

This is the authoritative behavioural coverage for the fallback. A
lightweight HTTP-contract test lives in ``test_tumbler_maker_idle.py``.

Requires ``docker compose --profile e2e up -d``. Reuses helpers from
``tests/e2e/test_tumbler_e2e``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import httpx
import pytest
from loguru import logger

from tests.e2e.rpc_utils import rpc_call
from tests.e2e.test_tumbler_e2e import (
    API,
    FUND_AMOUNT_BTC,
    STATUS_POLL_INTERVAL_SEC,
    TLS_VERIFY,
    _auth,
    _background_miner,
    _create_wallet,
    _ensure_no_wallet,
    _fund_via_fidelity_funder,
    _get_status,
    _lock_wallet,
    _new_address,
    _post_start,
    _post_stop,
    _wait_for_jmwalletd,
    _wait_for_sync_and_funds,
)

pytestmark = [pytest.mark.e2e, pytest.mark.tumbler_e2e]

# Keep the deadline well above the idle window so a successful timeout is
# unambiguous: if the phase exits via ``maker_session_seconds`` instead the
# test will fail because the runner did not observe the idle path.
IDLE_TIMEOUT_SECONDS = 5.0
MAKER_SESSION_SECONDS = 60.0

# Wait budget for the maker phase to be reached and complete via idle.
# stage-1 sweeps one mixdepth (one CJ) before the maker phase, so the
# upper bound is dominated by the first CJ round-trip plus the idle window.
MAKER_PHASE_TIMEOUT_SEC = 180.0


@pytest.fixture(scope="module")
async def jmwalletd_ready() -> None:
    await _wait_for_jmwalletd()


@pytest.fixture()
async def client(jmwalletd_ready: None) -> AsyncGenerator[httpx.AsyncClient, None]:
    async with httpx.AsyncClient(timeout=60, verify=TLS_VERIFY) as c:
        yield c


@pytest.fixture()
async def funded_wallet(
    client: httpx.AsyncClient,
) -> AsyncGenerator[tuple[str, str, str], None]:
    await _ensure_no_wallet(client)
    name, token, _ = await _create_wallet(client)
    try:
        deposit = await _new_address(client, name, token, mixdepth=0)
        await _fund_via_fidelity_funder(deposit, FUND_AMOUNT_BTC)
        await _wait_for_sync_and_funds(
            client, name, token, min_sats=int(FUND_AMOUNT_BTC * 0.99 * 1e8)
        )
        dest = await rpc_call("getnewaddress", wallet="fidelity_funder") or ""
        assert isinstance(dest, str) and dest.startswith("bcrt1"), dest
        yield name, token, dest
    finally:
        try:
            await _lock_wallet(client, name, token)
        except Exception:
            logger.exception("cleanup lock failed for {}", name)


async def _post_plan_with_idle_fallback(
    client: httpx.AsyncClient,
    name: str,
    token: str,
    destination: str,
) -> dict[str, Any]:
    r = await client.post(
        f"{API}/wallet/{name}/tumbler/plan",
        json={
            "destinations": [destination],
            "parameters": {
                "maker_count_min": 2,
                "maker_count_max": 2,
                "include_maker_sessions": True,
                "mintxcount": 2,
                "time_lambda_seconds": 0.1,
                # Generous upper bound; if the phase exits because this
                # deadline fires instead of the idle timeout, the test
                # fails because the assertion below requires cj_served==0
                # and a runtime below this value.
                "maker_session_seconds": MAKER_SESSION_SECONDS,
                "maker_session_idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
                "seed": 42,
            },
            "force": False,
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"tumbler/plan failed: {r.status_code} {r.text}"
    return dict(r.json())


async def _poll_until_maker_phase_completed(
    client: httpx.AsyncClient,
    name: str,
    token: str,
    *,
    maker_phase_index: int,
    timeout: float,
) -> dict[str, Any]:
    """Poll status until the maker phase reaches a terminal state.

    Returns the maker phase dict. Fails the test if the plan terminates
    with the maker phase not completed, or the timeout expires.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    last: dict[str, Any] = {}
    while asyncio.get_event_loop().time() < deadline:
        last = await _get_status(client, name, token)
        phases = last.get("phases", [])
        if maker_phase_index < len(phases):
            mp = phases[maker_phase_index]
            mp_status = str(mp.get("status", "")).lower()
            logger.info(
                "maker phase status={} cj_served={} plan_status={}",
                mp_status,
                mp.get("cj_served"),
                last.get("status"),
            )
            if mp_status in ("completed", "failed", "cancelled"):
                return dict(mp)
        plan_status = str(last.get("status", "")).lower()
        if plan_status in ("failed", "cancelled"):
            pytest.fail(
                f"plan terminated as {plan_status} before maker phase completed: {last}"
            )
        await asyncio.sleep(STATUS_POLL_INTERVAL_SEC)
    pytest.fail(
        f"maker phase {maker_phase_index} never reached terminal state within "
        f"{timeout}s; last={last}"
    )
    raise AssertionError("unreachable")  # pragma: no cover  -- pytest.fail is NoReturn


@pytest.mark.asyncio
async def test_maker_phase_exits_via_idle_timeout_when_no_cj_served(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """With no external takers hitting our maker, the maker phase must
    exit as COMPLETED via the idle-timeout fallback with ``cj_served==0``
    and well before ``maker_session_seconds`` elapses."""
    name, token, destination = funded_wallet

    plan = await _post_plan_with_idle_fallback(client, name, token, destination)
    phase_kinds = [p["kind"] for p in plan["phases"]]
    # Expected layout with mintxcount=2, funds in mixdepth 0 only on a
    # 5-mixdepth wallet, and ``include_maker_sessions=True``:
    # 1 stage-1 sweep (taker_coinjoin) + 4 stage-2 blocks of
    # (maker_session, fractional taker_coinjoin, sweep taker_coinjoin).
    assert phase_kinds.count("maker_session") >= 1, phase_kinds
    maker_index = phase_kinds.index("maker_session")
    assert maker_index >= 1, (
        f"maker phase should sit after at least one stage-1 sweep: {phase_kinds}"
    )
    # Sanity-check: the idle timeout survived the plan round-trip.
    assert (
        plan["phases"][maker_index].get("idle_timeout_seconds") == IDLE_TIMEOUT_SECONDS
    )

    await _post_start(client, name, token)
    async with _background_miner():
        maker_phase = await _poll_until_maker_phase_completed(
            client,
            name,
            token,
            maker_phase_index=maker_index,
            timeout=MAKER_PHASE_TIMEOUT_SEC,
        )

    # Stop the plan: we're not interested in the remaining stage-2 phases.
    try:
        await _post_stop(client, name, token)
    except Exception:
        logger.exception("stop after maker phase failed (non-fatal)")

    assert str(maker_phase.get("status", "")).lower() == "completed", (
        f"maker phase should complete via idle timeout: {maker_phase}"
    )
    # The whole point of the idle fallback: no external taker hit us.
    assert maker_phase.get("cj_served") == 0, (
        f"expected zero CJs served for idle-timeout path: {maker_phase}"
    )
    # The phase must have exited via ``idle_timeout_seconds``, not the
    # ``maker_session_seconds`` deadline. Use the persisted phase
    # timestamps so the bound is independent of stage-1 sweep latency
    # (which can dominate wall-clock under load).
    started_at = maker_phase.get("started_at")
    finished_at = maker_phase.get("finished_at")
    assert started_at and finished_at, maker_phase
    started_dt = datetime.fromisoformat(started_at)
    finished_dt = datetime.fromisoformat(finished_at)
    phase_elapsed = (finished_dt - started_dt).total_seconds()
    # Allow generous headroom over the idle window for shutdown/teardown
    # but stay well below the maker_session_seconds deadline.
    assert phase_elapsed < MAKER_SESSION_SECONDS * 0.5, (
        f"maker phase ran for {phase_elapsed:.0f}s; idle path should exit "
        f"within roughly {IDLE_TIMEOUT_SECONDS}s, not the "
        f"{MAKER_SESSION_SECONDS}s deadline"
    )
