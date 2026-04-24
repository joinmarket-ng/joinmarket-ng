"""Tests for the ``/tumbler/*`` router.

These tests exercise the plan-lifecycle state matrix documented in
``docs/technical/tumbler-redesign.md`` at the HTTP surface, without actually
spawning the ``TumbleRunner``. Paths that need to observe a live runner
pre-populate ``state.tumble_runner`` / ``state.tumble_task`` with mocks
because FastAPI ``TestClient`` runs the app on an internal anyio event loop
and cannot reliably await ``asyncio.create_task`` side effects from a test.

State matrix covered (wallet = ``test_wallet.jmdat``):

* ``POST /tumbler/plan`` when none / pending / pending+force /
  runner-alive / runner-stale / terminal plan exists on disk.
* ``GET /tumbler/status`` when none / pending / runner-alive /
  runner-stale / terminal.
* ``POST /tumbler/start`` when no plan / pending / terminal / already
  running (conflict).
* ``POST /tumbler/stop`` when no runner / runner alive.
* ``DELETE /tumbler/plan`` when none / pending / terminal / runner-alive.

Startup reconciliation (``DaemonState.reconcile_stale_tumbler_plans``) is
covered separately in ``test_tumbler_reconcile``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from jmtumbler.builder import PlanBuilder, TumbleParameters
from jmtumbler.persistence import load_plan, plan_path, save_plan
from jmtumbler.plan import Plan, PlanStatus

from jmwalletd.deps import get_daemon_state
from jmwalletd.state import CoinjoinState, DaemonState

WALLET = "test_wallet.jmdat"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _build_plan(wallet_name: str = WALLET) -> Plan:
    """Build a deterministic 2-destination plan from balances on 3 mixdepths."""
    params = TumbleParameters(
        destinations=[
            "bcrt1qdest0000000000000000000000000000000dest",
            "bcrt1qdest1111111111111111111111111111111dest",
        ],
        mixdepth_balances={0: 100_000_000, 1: 50_000_000, 2: 25_000_000},
        seed=42,
    )
    return PlanBuilder(wallet_name=wallet_name, params=params).build()


@pytest.fixture
def plan_on_disk(app_with_wallet: TestClient) -> Plan:
    """Persist a fresh PENDING plan for ``WALLET``."""
    state = get_daemon_state()
    plan = _build_plan()
    save_plan(plan, state.data_dir)
    return plan


def _fake_running_runner(plan: Plan) -> MagicMock:
    runner = MagicMock()
    runner.plan = plan
    runner.request_stop = MagicMock()
    runner.stop_and_wait = AsyncMock()
    return runner


def _mark_runner_alive(state: DaemonState, plan: Plan) -> MagicMock:
    """Attach a not-yet-done task + runner mock so ``_runner_alive_for`` is True.

    We use a plain ``MagicMock`` for the task because constructing a real
    ``asyncio.Future`` outside a running loop raises ``DeprecationWarning``
    turned error on newer Python, and the router only inspects ``task.done()``.
    """
    runner = _fake_running_runner(plan)
    fake_task = MagicMock()
    fake_task.done.return_value = False
    fake_task.cancel = MagicMock()
    state.tumble_runner = runner
    state.tumble_task = fake_task
    state.tumble_plan_wallet = plan.wallet_name
    state.coinjoin_state = CoinjoinState.TUMBLER_RUNNING
    return runner


def _clear_runner(state: DaemonState) -> None:
    state.tumble_task = None
    state.tumble_runner = None
    state.tumble_plan_wallet = None
    state.coinjoin_state = CoinjoinState.NOT_RUNNING


# ----------------------------------------------------------------------------
# POST /tumbler/plan
# ----------------------------------------------------------------------------


class TestCreatePlan:
    def test_create_fresh_plan_persists_pending(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": ["bcrt1qdestAaaaaa", "bcrt1qdestBbbbbb"]},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == PlanStatus.PENDING
        assert body["wallet_name"] == WALLET
        assert len(body["phases"]) > 0
        # Persisted to disk?
        state = get_daemon_state()
        disk = load_plan(WALLET, state.data_dir)
        assert disk.status == PlanStatus.PENDING

    def test_create_plan_refuses_existing_pending_without_force(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": ["bcrt1qdestAaaaaa", "bcrt1qdestBbbbbb"]},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 400
        assert "force=true" in resp.json()["message"]

    def test_create_plan_overwrites_pending_with_force(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        old_id = plan_on_disk.plan_id
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={
                "destinations": ["bcrt1qdestAaaaaa", "bcrt1qdestBbbbbb"],
                "force": True,
            },
            headers=_auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["plan_id"] != old_id

    def test_create_plan_overwrites_terminal_without_force(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        plan_on_disk.status = PlanStatus.COMPLETED
        save_plan(plan_on_disk, state.data_dir)

        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": ["bcrt1qdestAaaaaa", "bcrt1qdestBbbbbb"]},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 201
        assert resp.json()["status"] == PlanStatus.PENDING

    def test_create_plan_rejects_while_runner_alive(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        _mark_runner_alive(state, plan_on_disk)
        try:
            resp = app_with_wallet.post(
                f"/api/v1/wallet/{WALLET}/tumbler/plan",
                json={"destinations": ["bcrt1qdestAaaaaa", "bcrt1qdestBbbbbb"]},
                headers=_auth(auth_token),
            )
            assert resp.status_code == 401
        finally:
            _clear_runner(state)

    def test_create_plan_reconciles_stale_running_plan(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        """Plan on disk is RUNNING but no runner is alive => reconcile to FAILED, overwrite."""
        state = get_daemon_state()
        plan_on_disk.status = PlanStatus.RUNNING
        save_plan(plan_on_disk, state.data_dir)

        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": ["bcrt1qdestAaaaaa", "bcrt1qdestBbbbbb"]},
            headers=_auth(auth_token),
        )
        # Reconcile turns RUNNING -> FAILED (terminal), which may be overwritten.
        assert resp.status_code == 201

    def test_create_plan_requires_destinations(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": []},
            headers=_auth(auth_token),
        )
        # pydantic min_length=1 => 422.
        assert resp.status_code == 422

    def test_create_plan_errors_on_empty_wallet(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        ws = get_daemon_state().wallet_service
        ws.get_balance.return_value = 0
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": ["bcrt1qdestAaaaaa"]},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 400
        assert "no confirmed coins" in resp.json()["message"]

    def test_stop_not_running_uses_service_state_auth_header(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/stop",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 401
        assert resp.headers["WWW-Authenticate"].startswith('Bearer, error="service_state"')


# ----------------------------------------------------------------------------
# GET /tumbler/status
# ----------------------------------------------------------------------------


class TestGetStatus:
    def test_status_no_plan(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{WALLET}/tumbler/status",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 404

    def test_status_returns_pending(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{WALLET}/tumbler/status",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == PlanStatus.PENDING
        assert body["stale"] is False

    def test_status_flags_stale_and_reconciles(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        plan_on_disk.status = PlanStatus.RUNNING
        save_plan(plan_on_disk, state.data_dir)

        resp = app_with_wallet.get(
            f"/api/v1/wallet/{WALLET}/tumbler/status",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["stale"] is True
        # Reconcile was persisted: subsequent load shows FAILED with terminal status.
        disk = load_plan(WALLET, state.data_dir)
        assert disk.status == PlanStatus.FAILED

    def test_status_returns_live_runner_plan(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        runner = _mark_runner_alive(state, plan_on_disk)
        # Drift the live plan so we can observe we read the in-memory one.
        runner.plan.status = PlanStatus.RUNNING
        try:
            resp = app_with_wallet.get(
                f"/api/v1/wallet/{WALLET}/tumbler/status",
                headers=_auth(auth_token),
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == PlanStatus.RUNNING
            assert body["stale"] is False
        finally:
            _clear_runner(state)


# ----------------------------------------------------------------------------
# POST /tumbler/start  (focuses on guard rails; the success path is covered e2e)
# ----------------------------------------------------------------------------


class TestStartPlan:
    def test_start_without_plan(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/start",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 404

    def test_start_while_other_service_running(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        state.coinjoin_state = CoinjoinState.MAKER_RUNNING
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/start",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 401
        assert "already running" in resp.json()["message"]

    def test_start_rejects_terminal_plan(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        plan_on_disk.status = PlanStatus.COMPLETED
        save_plan(plan_on_disk, state.data_dir)

        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/start",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 400
        assert "terminal" in resp.json()["message"]

    def test_start_reconciles_and_rejects_stale_running_plan(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        plan_on_disk.status = PlanStatus.RUNNING
        save_plan(plan_on_disk, state.data_dir)

        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/start",
            headers=_auth(auth_token),
        )
        # Reconcile flipped RUNNING->FAILED, then the terminal check rejects.
        assert resp.status_code == 400


# ----------------------------------------------------------------------------
# POST /tumbler/stop
# ----------------------------------------------------------------------------


class TestStopPlan:
    def test_stop_without_runner(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/stop",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 401
        assert "No tumbler" in resp.json()["message"]

    def test_stop_calls_runner_stop(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        runner = _mark_runner_alive(state, plan_on_disk)
        try:
            resp = app_with_wallet.post(
                f"/api/v1/wallet/{WALLET}/tumbler/stop",
                headers=_auth(auth_token),
            )
            assert resp.status_code == 202
            runner.stop_and_wait.assert_awaited_once()
        finally:
            _clear_runner(state)


# ----------------------------------------------------------------------------
# DELETE /tumbler/plan
# ----------------------------------------------------------------------------


class TestDeletePlan:
    def test_delete_without_plan(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.delete(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 404

    def test_delete_pending(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        assert plan_path(WALLET, state.data_dir).exists()
        resp = app_with_wallet.delete(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            headers=_auth(auth_token),
        )
        assert resp.status_code == 204
        assert not plan_path(WALLET, state.data_dir).exists()

    def test_delete_refuses_while_runner_alive(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        _mark_runner_alive(state, plan_on_disk)
        try:
            resp = app_with_wallet.delete(
                f"/api/v1/wallet/{WALLET}/tumbler/plan",
                headers=_auth(auth_token),
            )
            assert resp.status_code == 400
            assert "running" in resp.json()["message"]
        finally:
            _clear_runner(state)


# ----------------------------------------------------------------------------
# Legacy endpoints removed
# ----------------------------------------------------------------------------


class TestLegacyScheduleEndpointsGone:
    def test_post_taker_schedule_is_404(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/taker/schedule",
            json={"destination_addresses": ["a", "b"]},
            headers=_auth(auth_token),
        )
        # Either 404 (no such route) or 405 if another method shadowed it;
        # the point is the old contract is gone.
        assert resp.status_code in (404, 405)

    def test_get_taker_schedule_is_404(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.get(
            f"/api/v1/wallet/{WALLET}/taker/schedule",
            headers=_auth(auth_token),
        )
        assert resp.status_code in (404, 405)


# ----------------------------------------------------------------------------
# Startup reconciliation
# ----------------------------------------------------------------------------


class TestReconcileStaleOnStartup:
    def test_reconcile_marks_running_plan_failed(self, tmp_path: Path) -> None:
        state = DaemonState(data_dir=tmp_path)
        plan = _build_plan()
        plan.status = PlanStatus.RUNNING
        save_plan(plan, tmp_path)

        reconciled = state.reconcile_stale_tumbler_plans()
        assert reconciled == [WALLET]
        disk = load_plan(WALLET, tmp_path)
        assert disk.status == PlanStatus.FAILED
        assert disk.error and "restarted" in disk.error

    def test_reconcile_marks_pending_plan_failed(self, tmp_path: Path) -> None:
        state = DaemonState(data_dir=tmp_path)
        plan = _build_plan()
        assert plan.status == PlanStatus.PENDING
        save_plan(plan, tmp_path)

        reconciled = state.reconcile_stale_tumbler_plans()
        assert reconciled == [WALLET]
        disk = load_plan(WALLET, tmp_path)
        assert disk.status == PlanStatus.FAILED

    def test_reconcile_skips_terminal_plans(self, tmp_path: Path) -> None:
        state = DaemonState(data_dir=tmp_path)
        for status in (PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED):
            plan = _build_plan(wallet_name=f"w_{status.value}.jmdat")
            plan.status = status
            save_plan(plan, tmp_path)

        reconciled = state.reconcile_stale_tumbler_plans()
        assert reconciled == []

    def test_reconcile_returns_empty_when_no_schedules_dir(self, tmp_path: Path) -> None:
        state = DaemonState(data_dir=tmp_path)
        assert state.reconcile_stale_tumbler_plans() == []
