"""Requests authorized before a wallet lock began must not start wallet work.

FastAPI resolves ``require_auth`` before the handler body, with awaits in
between, and ``_lock_wallet`` resets the token keys before its first await but
only stops services that already exist. These tests run that reset between the
two, in a ``require_wallet_match`` override (resolved after ``require_auth``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.persistence import load_plan, save_plan
from tumbler.plan import PlanStatus

from jmwalletd.deps import get_daemon_state, require_wallet_match
from jmwalletd.state import CoinjoinState

WALLET = "test_wallet.jmdat"


@pytest.fixture
def lock_begins_after_auth(app_with_wallet: TestClient) -> Iterator[TestClient]:
    app = cast(FastAPI, app_with_wallet.app)
    app.dependency_overrides[require_wallet_match] = get_daemon_state().token_authority.reset
    yield app_with_wallet
    app.dependency_overrides.clear()


def _post(client: TestClient, token: str, path: str, body: dict[str, Any] | None = None) -> Any:
    return client.post(
        f"/api/v1/wallet/{WALLET}/{path}",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize(
    ("path", "body"),
    [
        (
            "maker/start",
            {
                "txfee": "1000",
                "cjfee_a": "500",
                "cjfee_r": "0.002",
                "ordertype": "sw0reloffer",
                "minsize": "100000",
            },
        ),
        (
            "taker/coinjoin",
            {
                "mixdepth": 0,
                "amount_sats": 100000,
                "destination": "bcrt1qdest",
                "counterparties": 3,
                "txfee": 500,
            },
        ),
    ],
)
def test_service_is_not_started_once_lock_began(
    lock_begins_after_auth: TestClient, auth_token: str, path: str, body: dict[str, Any]
) -> None:
    state = get_daemon_state()
    with (
        patch("jmwalletd._backend.get_backend", new_callable=AsyncMock),
        patch("maker.bot.MakerBot", return_value=AsyncMock()),
        patch("taker.taker.Taker", return_value=AsyncMock()),
    ):
        response = _post(lock_begins_after_auth, auth_token, path, body)

    assert response.status_code == 401
    assert state.coinjoin_state == CoinjoinState.NOT_RUNNING
    assert state._maker_task is None
    assert state._taker_task is None


@pytest.mark.parametrize("status", [PlanStatus.PENDING, PlanStatus.RUNNING])
def test_tumbler_is_not_started_once_lock_began(
    lock_begins_after_auth: TestClient, auth_token: str, status: PlanStatus
) -> None:
    """A RUNNING plan without a live runner would be reconciled and saved."""
    state = get_daemon_state()
    params = TumbleParameters(
        destinations=[
            "bcrt1qdest0000000000000000000000000000000dest",
            "bcrt1qdest1111111111111111111111111111111dest",
        ],
        mixdepth_balances={0: 100_000_000, 1: 50_000_000, 2: 25_000_000},
        seed=42,
    )
    plan = PlanBuilder(wallet_name=WALLET, params=params).build()
    plan.status = status
    save_plan(plan, state.data_dir)

    response = _post(lock_begins_after_auth, auth_token, "tumbler/start")

    assert response.status_code == 401
    assert state.coinjoin_state == CoinjoinState.NOT_RUNNING
    assert state.tumble_task is None
    assert load_plan(WALLET, state.data_dir).status == status


def test_rescan_is_not_started_once_lock_began(
    lock_begins_after_auth: TestClient, auth_token: str
) -> None:
    state = get_daemon_state()
    state.wallet_service.backend = MagicMock()

    response = lock_begins_after_auth.get(
        f"/api/v1/wallet/{WALLET}/rescanblockchain/0",
        headers={"Authorization": f"Bearer {auth_token}"},
    )

    assert response.status_code == 401
    assert state._rescan_task is None
    assert state.rescanning is False
