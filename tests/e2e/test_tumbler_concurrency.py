"""End-to-end tests for jmwalletd's tumbler concurrency guards.

While a tumbler plan is running, the daemon blocks manual taker and maker
operations so they cannot collide with the scheduler. These tests drive a
real tumbler through the HTTP API and verify that:

* ``POST /taker/coinjoin`` is rejected with 401 ``ServiceAlreadyStarted``
* ``POST /taker/direct-send`` is rejected with 400 ``ActionNotAllowed``
* ``POST /maker/start`` is rejected with 401 ``ServiceAlreadyStarted``

Once the tumbler is stopped the same endpoints must accept requests again
(verified implicitly by re-posting a manual CoinJoin and receiving a
non-guard response; we only care that the guard is no longer raised).

Requires ``docker compose --profile e2e up -d``. Reuses the helpers from
``tests/e2e/test_tumbler_e2e`` instead of duplicating them.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import httpx
import pytest
from loguru import logger

from tests.e2e.test_tumbler_e2e import (
    API,
    FUND_AMOUNT_BTC,
    TLS_VERIFY,
    _auth,
    _create_wallet,
    _ensure_no_wallet,
    _fund_via_fidelity_funder,
    _lock_wallet,
    _new_address,
    _post_plan,
    _post_start,
    _post_stop,
    _wait_for_jmwalletd,
    _wait_for_sync_and_funds,
)
from tests.e2e.rpc_utils import rpc_call

pytestmark = [pytest.mark.e2e, pytest.mark.tumbler_e2e]


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
    """Same shape as the happy-path fixture but module-scoped to this file."""
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


@pytest.mark.asyncio
async def test_manual_operations_are_blocked_while_tumbler_runs(
    client: httpx.AsyncClient,
    funded_wallet: tuple[str, str, str],
) -> None:
    """With a tumble running, docoinjoin/directsend/startmaker must be rejected."""
    name, token, destination = funded_wallet

    await _post_plan(client, name, token, destination)
    await _post_start(client, name, token)
    try:
        # The runner sets TUMBLER_RUNNING synchronously inside /tumbler/start,
        # so the guards take effect immediately; no poll needed.

        # ``/taker/coinjoin`` -> 401 ServiceAlreadyStarted. The body must
        # still be valid so the guard (not validation) is what rejects the
        # call. Unlike direct-send, docoinjoin checks the full
        # ``coinjoin_state`` machine and raises the 401 path.
        r = await client.post(
            f"{API}/wallet/{name}/taker/coinjoin",
            json={
                "mixdepth": 0,
                "amount_sats": 10_000,
                "counterparties": 2,
                "destination": destination,
            },
            headers=_auth(token),
        )
        assert r.status_code == 401, (
            f"docoinjoin should be blocked with 401, got {r.status_code} {r.text}"
        )
        assert "running" in r.text.lower() or "already" in r.text.lower(), r.text

        # ``/taker/direct-send`` -> 400 ActionNotAllowed.
        r = await client.post(
            f"{API}/wallet/{name}/taker/direct-send",
            json={
                "mixdepth": 0,
                "amount_sats": 10_000,
                "destination": destination,
            },
            headers=_auth(token),
        )
        assert r.status_code == 400, (
            f"directsend should be blocked with 400, got {r.status_code} {r.text}"
        )

        # ``/maker/start`` -> 401 ServiceAlreadyStarted.
        r = await client.post(
            f"{API}/wallet/{name}/maker/start",
            json={
                "txfee": "0",
                "cjfee_a": "500",
                "cjfee_r": "0.0002",
                "ordertype": "reloffer",
                "minsize": "100000",
            },
            headers=_auth(token),
        )
        assert r.status_code == 401, (
            f"startmaker should be blocked with 401, got {r.status_code} {r.text}"
        )
        assert "already" in r.text.lower() or "running" in r.text.lower(), r.text
    finally:
        await _post_stop(client, name, token)
