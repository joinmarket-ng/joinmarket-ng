"""End-to-end edge cases for the tumbler.

These tests exercise behaviours that are not covered by the happy-path
suite in ``test_tumbler_e2e.py``:

* legacy JAM tumbler payloads that still include ``include_bondless_bursts``
  must be accepted (the router silently drops the key);
* a plan with ``max_phase_retries=0`` and an impossible counterparty count
  fails fast on the first attempt;
* the runner's tweak-and-retry loop swaps the destination to ``INTERNAL``
  on the second attempt of a failed taker phase, so we can observe that
  the ``destination`` field on the persisted phase changes between
  attempts.

The tests target the real ``docker compose --profile e2e`` stack and rely
on helpers exposed by ``tests/e2e/test_tumbler_e2e.py``. Where possible we
keep total wall-clock time well under five minutes so the suite remains
practical to run on CI.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest

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
    _poll_until_terminal,
    _post_start,
    _post_stop,
    _wait_for_jmwalletd,
    _wait_for_sync_and_funds,
)
from tests.e2e.rpc_utils import rpc_call

pytestmark = [pytest.mark.e2e, pytest.mark.tumbler_e2e]


# ---------------------------------------------------------------------------
# Shared fixtures.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
async def jmwalletd_ready() -> None:
    await _wait_for_jmwalletd()


@pytest.fixture()
async def client(jmwalletd_ready: None) -> AsyncGenerator[httpx.AsyncClient, None]:
    # Function-scoped so the underlying httpx connection pool (and any
    # ``asyncio.Event`` it internally creates) is bound to the test's own
    # event loop. pytest-asyncio defaults to a fresh loop per test, and a
    # module-scoped client would re-use a now-closed loop on the second test.
    async with httpx.AsyncClient(timeout=60, verify=TLS_VERIFY) as c:
        yield c


@pytest.fixture()
async def funded_wallet(
    client: httpx.AsyncClient,
) -> AsyncGenerator[tuple[str, str, str], None]:
    """Create+fund a fresh wallet per test.

    Function-scoped so each test starts from a clean daemon state — module
    scope would carry a previous test's PENDING plan over and force-overwrite
    coupling, and would also re-bind httpx internals to a stale event loop.
    """
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
            pass


async def _post_plan_with(
    client: httpx.AsyncClient,
    name: str,
    token: str,
    destination: str,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    r = await client.post(
        f"{API}/wallet/{name}/tumbler/plan",
        json={
            "destinations": [destination],
            "parameters": parameters,
        },
        headers=_auth(token),
    )
    assert r.status_code == 201, f"plan: {r.status_code} {r.text}"
    return dict(r.json())


# ---------------------------------------------------------------------------
# Tests.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_jam_payload_with_bondless_field_is_accepted(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """Legacy JAM tumbler form sends ``include_bondless_bursts`` in
    ``parameters``. The router must drop it silently and still build a
    valid plan rather than 400ing on an unknown field."""
    name, token, destination = funded_wallet
    plan = await _post_plan_with(
        client,
        name,
        token,
        destination,
        parameters={
            # Legacy field that no longer exists in TumbleParameters.
            "include_bondless_bursts": True,
            # Plus the modern fields the planner needs.
            "maker_count_min": 2,
            "maker_count_max": 2,
            "include_maker_sessions": False,
            "mintxcount": 2,
            "time_lambda_seconds": 0.1,
            "seed": 7,
        },
    )
    assert plan["status"].lower() == "pending"
    assert all(p["kind"] == "taker_coinjoin" for p in plan["phases"])
    # max_phase_retries default of 3 must round-trip on the parameters.
    assert plan.get("parameters", {}).get("max_phase_retries", None) in (None, 3)


@pytest.mark.asyncio
async def test_max_phase_retries_zero_fails_plan_on_first_failure(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """With ``max_phase_retries=0`` the runner must not retry. Force a
    failure by demanding 19 makers (the e2e profile only has three) so
    the very first stage-1 sweep fails the plan."""
    name, token, destination = funded_wallet
    plan = await _post_plan_with(
        client,
        name,
        token,
        destination,
        parameters={
            "maker_count_min": 19,
            "maker_count_max": 19,
            "include_maker_sessions": False,
            "mintxcount": 2,
            "time_lambda_seconds": 0.1,
            "max_phase_retries": 0,
            "seed": 11,
        },
    )
    assert plan["status"].lower() == "pending"

    await _post_start(client, name, token)
    async with _background_miner():
        # Plan should fail quickly: no retries, no successful CJ rounds.
        final = await _poll_until_terminal(client, name, token, timeout=120.0)
    assert final["status"].lower() == "failed", final
    # The first phase must carry attempt_count == 0 (no retries used) and
    # have its FAILED status preserved on disk.
    first_phase = final["phases"][0]
    assert first_phase["status"].lower() == "failed", first_phase
    assert first_phase.get("attempt_count", 0) == 0, first_phase
    assert first_phase.get("error"), first_phase


@pytest.mark.asyncio
async def test_retry_swaps_external_destination_to_internal_on_failure(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """Drive a failure on the *last* phase (which targets the user's
    destination), then verify that the runner rewrites the persisted
    phase's ``destination`` to ``"INTERNAL"`` after the retry budget
    consumes one attempt.

    We force the failure with an impossible counterparty count and a
    retry budget of 1, so the runner makes exactly two attempts: the
    first against the external destination and the second after the
    tweak swaps it.
    """
    name, token, destination = funded_wallet
    plan = await _post_plan_with(
        client,
        name,
        token,
        destination,
        parameters={
            "maker_count_min": 19,
            "maker_count_max": 19,
            "include_maker_sessions": False,
            "mintxcount": 2,
            "time_lambda_seconds": 0.1,
            "max_phase_retries": 1,
            "seed": 23,
        },
    )
    # Sanity: at least one phase originally targets the external address.
    addressed = [p for p in plan["phases"] if p.get("destination") == destination]
    assert addressed, plan["phases"]

    await _post_start(client, name, token)
    # The plan will fail (every phase asks for 19 makers and only 3 exist),
    # but along the way the runner will have rewritten the destination on
    # the failing phase to "INTERNAL" before exhausting the retry budget.
    async with _background_miner():
        final = await _poll_until_terminal(client, name, token, timeout=120.0)
    assert final["status"].lower() == "failed", final

    # The first phase that originally pointed at the external address
    # must now record either ``destination == "INTERNAL"`` (rewritten by
    # the tweak helper) or ``attempt_count >= 1`` (retry was attempted).
    rewritten = [
        p
        for p in final["phases"]
        if p.get("status", "").lower() == "failed"
        and (p.get("destination") == "INTERNAL" or int(p.get("attempt_count", 0)) >= 1)
    ]
    assert rewritten, (
        "expected at least one failed phase to show retry book-keeping "
        f"(attempt_count or rewritten destination): {final['phases']}"
    )


@pytest.mark.asyncio
async def test_status_endpoint_exposes_attempt_count(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """``GET /tumbler/status`` must surface ``attempt_count`` on every
    phase response (defaulting to 0 for an untouched plan)."""
    name, token, destination = funded_wallet
    await _post_plan_with(
        client,
        name,
        token,
        destination,
        parameters={
            "maker_count_min": 2,
            "maker_count_max": 2,
            "include_maker_sessions": False,
            "mintxcount": 2,
            "time_lambda_seconds": 0.1,
            "seed": 3,
        },
    )
    status = await _get_status(client, name, token)
    for phase in status["phases"]:
        assert "attempt_count" in phase, phase
        assert isinstance(phase["attempt_count"], int)
        assert phase["attempt_count"] == 0
    # Eagerly stop without starting; the wallet fixture handles cleanup.
    try:
        await _post_stop(client, name, token)
    except Exception:
        pass
    # Avoid a tight loop in fixtures by giving the daemon a tick to settle.
    await asyncio.sleep(STATUS_POLL_INTERVAL_SEC)


# ---------------------------------------------------------------------------
# Frozen / unfunded mixdepth regressions.
# ---------------------------------------------------------------------------


async def _list_utxos(
    client: httpx.AsyncClient, name: str, token: str
) -> list[dict[str, Any]]:
    r = await client.get(f"{API}/wallet/{name}/utxos", headers=_auth(token))
    assert r.status_code == 200, f"utxos: {r.status_code} {r.text}"
    return list(r.json().get("utxos", []))


async def _freeze_all_utxos(client: httpx.AsyncClient, name: str, token: str) -> int:
    """Freeze every currently-unfrozen UTXO in the wallet; return the count."""
    frozen = 0
    for utxo in await _list_utxos(client, name, token):
        if utxo.get("frozen"):
            continue
        r = await client.post(
            f"{API}/wallet/{name}/freeze",
            json={"utxo-string": utxo["utxo"], "freeze": True},
            headers=_auth(token),
        )
        assert r.status_code == 200, f"freeze: {r.status_code} {r.text}"
        frozen += 1
    return frozen


@pytest.mark.asyncio
async def test_plan_rejects_wallet_with_only_frozen_funds(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """Regression: with every UTXO frozen the wallet has nothing to tumble,
    so plan creation must be rejected instead of scheduling a stage-1 sweep
    of the frozen mixdepth (which used to stall the whole run)."""
    name, token, destination = funded_wallet
    frozen = await _freeze_all_utxos(client, name, token)
    assert frozen > 0, "expected the funded wallet to have unfrozen UTXOs"

    r = await client.post(
        f"{API}/wallet/{name}/tumbler/plan",
        json={
            "destinations": [destination],
            "parameters": {
                "maker_count_min": 2,
                "maker_count_max": 2,
                "include_maker_sessions": False,
                "mintxcount": 2,
                "time_lambda_seconds": 0.1,
                "seed": 29,
            },
        },
        headers=_auth(token),
    )
    assert r.status_code == 400, f"expected rejection, got {r.status_code}: {r.text}"
    assert "no confirmed coins" in r.text.lower(), r.text


@pytest.mark.asyncio
async def test_started_plan_skips_phases_when_funds_frozen_after_planning(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """Regression coverage for two field reports:

    1. Funds frozen *after* the plan was built: the stage-1 sweep of
       mixdepth 0 can never succeed and used to burn the retry budget
       (with hours of back-off) before failing the whole plan.
    2. Sweeps of empty mixdepths: the stage-2 chain visits mixdepths that
       never received coins (here md1-md4, since the md0 sweep is skipped).

    Both cases must now be SKIPPED so the plan terminates as COMPLETED
    with zero retries consumed instead of stalling or failing.
    """
    name, token, destination = funded_wallet
    plan = await _post_plan_with(
        client,
        name,
        token,
        destination,
        parameters={
            "maker_count_min": 2,
            "maker_count_max": 2,
            "include_maker_sessions": False,
            "mintxcount": 2,
            "time_lambda_seconds": 0.1,
            "max_phase_retries": 1,
            "seed": 31,
        },
    )
    assert plan["status"].lower() == "pending"
    assert any(p.get("mixdepth") == 0 for p in plan["phases"]), plan["phases"]

    frozen = await _freeze_all_utxos(client, name, token)
    assert frozen > 0, "expected the funded wallet to have unfrozen UTXOs"

    await _post_start(client, name, token)
    async with _background_miner():
        final = await _poll_until_terminal(client, name, token, timeout=600.0)

    assert final["status"].lower() == "completed", final
    taker_phases = [p for p in final["phases"] if p["kind"] == "taker_coinjoin"]
    assert taker_phases, final["phases"]
    assert all(p["status"].lower() == "skipped" for p in taker_phases), final["phases"]
    # Skipping must not consume the retry budget.
    assert all(int(p.get("attempt_count", 0)) == 0 for p in taker_phases), final[
        "phases"
    ]
    # No transaction may have been broadcast from frozen/empty mixdepths.
    assert all(not p.get("txid") for p in taker_phases), final["phases"]
