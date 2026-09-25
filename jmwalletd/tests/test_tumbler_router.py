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

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from tumbler.builder import PlanBuilder, TumbleParameters
from tumbler.persistence import load_plan, plan_path, save_plan
from tumbler.plan import PhaseStatus, Plan, PlanStatus, TakerCoinjoinPhase

from jmcore.paths import read_nick_state
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


def _mark_stale_confirmation_wait(plan: Plan, *, legacy: bool = False) -> TakerCoinjoinPhase:
    """Mark a non-final taker phase as a safely resumable stale run."""
    phase = next(
        phase
        for phase in plan.phases
        if isinstance(phase, TakerCoinjoinPhase) and phase.index + 1 < len(plan.phases)
    )
    phase.status = PhaseStatus.COMPLETED if legacy else PhaseStatus.AWAITING_CONFIRMATION
    phase.txid = "ab" * 32
    plan.current_phase = phase.index
    plan.status = PlanStatus.RUNNING
    return phase


# ----------------------------------------------------------------------------
# POST /tumbler/plan
# ----------------------------------------------------------------------------


class TestCreatePlan:
    _REGTEST_DESTINATIONS = [
        "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
        "bcrt1q4h7w2n6jnvs4fc7rvxa78a75rkcxx4ch44jl8m",
    ]

    def test_create_plan_validates_active_wallet_network(
        self, app_with_wallet: TestClient, auth_token: str
    ) -> None:
        state = get_daemon_state()
        state.wallet_service.network = "regtest"
        response = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": self._REGTEST_DESTINATIONS},
            headers=_auth(auth_token),
        )
        assert response.status_code == 201, response.text
        assert load_plan(WALLET, state.data_dir).network == "regtest"

    @pytest.mark.parametrize(
        ("destinations", "error"),
        [
            (
                [
                    "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080",
                    "BCRT1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KYGT080",
                ],
                "duplicate",
            ),
            (["bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"], "invalid"),
            (["INTERNAL"], "invalid"),
        ],
    )
    def test_create_plan_rejects_invalid_or_duplicate_exits(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        destinations: list[str],
        error: str,
    ) -> None:
        get_daemon_state().wallet_service.network = "regtest"
        response = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": destinations},
            headers=_auth(auth_token),
        )
        assert response.status_code == 400
        assert error in response.json()["message"]

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
        ws.get_coinjoin_balance.return_value = 0
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": ["bcrt1qdestAaaaaa", "bcrt1qdestBbbbbb"]},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 400
        assert "no confirmed coins" in resp.json()["message"]

    def test_create_plan_uses_coinjoin_selectable_balances(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        ws = get_daemon_state().wallet_service
        ws.get_coinjoin_balance.reset_mock()
        ws.get_coinjoin_balance.return_value = 50_000_000
        ws.get_locked_input_outpoints.return_value = {("aa" * 32, 1)}
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={"destinations": ["bcrt1qdestAaaaaa", "bcrt1qdestBbbbbb"], "force": True},
            headers=_auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        assert ws.get_coinjoin_balance.await_args_list
        for call in ws.get_coinjoin_balance.await_args_list:
            assert call.kwargs["min_confirmations"] == 5
            assert call.kwargs["exclude"] == {("aa" * 32, 1)}

    def test_create_plan_accepts_legacy_jam_parameters(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
    ) -> None:
        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/plan",
            json={
                "destinations": ["bcrt1qdestAaaaaa", "bcrt1qdestBbbbbb"],
                "force": True,
                "parameters": {
                    "addrcount": 1,
                    "minmakercount": 1,
                    "makercountrange": [1, 0],
                    "mixdepthcount": 1,
                    "mintxcount": 1,
                    "txcountparams": [1, 0],
                    "timelambda": 0.025,
                    "stage1_timelambda_increase": 1,
                    "liquiditywait": 13,
                    "waittime": 0,
                },
            },
            headers=_auth(auth_token),
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == PlanStatus.PENDING
        assert len(body["phases"]) > 0

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

    def test_status_preserves_stale_confirmation_wait_for_resume(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
    ) -> None:
        state = get_daemon_state()
        phase = _mark_stale_confirmation_wait(plan_on_disk)
        save_plan(plan_on_disk, state.data_dir)

        resp = app_with_wallet.get(
            f"/api/v1/wallet/{WALLET}/tumbler/status",
            headers=_auth(auth_token),
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["stale"] is True
        assert body["status"] == PlanStatus.PENDING
        assert body["phases"][phase.index]["status"] == PhaseStatus.AWAITING_CONFIRMATION
        assert body["phases"][phase.index]["txid"] == phase.txid
        disk = load_plan(WALLET, state.data_dir)
        assert disk.status == PlanStatus.PENDING
        disk_phase = disk.phases[phase.index]
        assert isinstance(disk_phase, TakerCoinjoinPhase)
        assert disk_phase.status == PhaseStatus.AWAITING_CONFIRMATION
        assert disk_phase.txid == phase.txid

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

    def test_start_resumes_stale_legacy_confirmation_wait(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A legacy post-broadcast state must reach the runner as PENDING.

        The runner itself skips the taker factory for the preserved
        ``AWAITING_CONFIRMATION`` phase, so accepting this start cannot replay
        the CoinJoin.
        """
        state = get_daemon_state()
        phase = _mark_stale_confirmation_wait(plan_on_disk, legacy=True)
        save_plan(plan_on_disk, state.data_dir)

        class CapturingRunner:
            captured_plan: Plan | None = None

            def __init__(self, plan: Plan, _ctx: object) -> None:
                self.plan = plan
                type(self).captured_plan = plan

            async def run(self) -> Plan:
                return self.plan

        monkeypatch.setattr("jmwalletd.routers.tumbler.TumbleRunner", CapturingRunner)

        resp = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/start",
            headers=_auth(auth_token),
        )

        assert resp.status_code == 202, resp.text
        captured = CapturingRunner.captured_plan
        assert captured is not None
        assert captured.status == PlanStatus.PENDING
        captured_phase = captured.phases[phase.index]
        assert isinstance(captured_phase, TakerCoinjoinPhase)
        assert captured_phase.status == PhaseStatus.AWAITING_CONFIRMATION
        assert captured_phase.txid == phase.txid

    @pytest.mark.parametrize(
        ("allow_clearnet_connections", "directory_server"),
        [
            (False, "directoryexample.onion:5222"),
            (True, "directory.internal:5222"),
        ],
    )
    def test_start_maker_factory_forwards_runtime_settings(
        self,
        app_with_wallet: TestClient,
        auth_token: str,
        plan_on_disk: Plan,
        monkeypatch: pytest.MonkeyPatch,
        allow_clearnet_connections: bool,
        directory_server: str,
    ) -> None:
        """Tumbler maker phases preserve network and maker policy settings."""
        from jmcore.models import NetworkType, OfferType
        from jmcore.settings import JoinMarketSettings, NetworkSettings
        from maker.config import MakerConfig

        settings = JoinMarketSettings(
            network_config=NetworkSettings(
                network=NetworkType.SIGNET,
                bitcoin_network=NetworkType.REGTEST,
                directory_servers=[directory_server],
                allow_clearnet_connections=allow_clearnet_connections,
            ),
            tor={
                "socks_host": "127.0.0.2",
                "socks_port": 9052,
                "stream_isolation": False,
                "connection_timeout": 45.0,
                "control_host": "127.0.0.3",
                "control_port": 9053,
                "cookie_path": "/tmp/tumbler-control.cookie",
                "target_host": "maker-service",
            },
            maker={
                "min_size": 200_000,
                "cj_fee_relative": "0.003",
                "cj_fee_absolute": 700,
                "tx_fee_contribution": 800,
                "cjfee_factor": 0.15,
                "txfee_contribution_factor": 0.45,
                "size_factor": 0.25,
                "min_confirmations": 7,
                "allow_mixdepth_zero_merge": True,
                "merge_algorithm": "gradual",
                "mixdepth_selection_policy": "concentrated",
                "min_fee_rate_sat_vb": 2.5,
                "min_fee_block_target": 12,
                "session_timeout_sec": 600,
                "pre_sign_timeout_sec": 120,
                "identity_renewal_min_sec": 61,
                "identity_renewal_max_sec": 121,
                "identity_grace_sec": 90,
                "identity_rotation_quiet_min_sec": 17,
                "identity_rotation_quiet_max_sec": 29,
                "pending_tx_timeout_min": 15,
                "pending_tx_abandon_hours": 96,
                "rescan_interval_sec": 780,
                "onion_host": "tumbler-maker.example.onion",
                "onion_serving_host": "0.0.0.0",
                "onion_serving_port": 5223,
                "message_rate_limit": 11,
                "message_burst_limit": 111,
                "offer_reannounce_delay_max": 432,
                "directory_reconnect_interval": 75,
                "directory_reconnect_max_retries": 8,
                "directory_startup_timeout": 180,
                "orderbook_rate_limit": 2,
                "orderbook_rate_interval": 15.0,
                "orderbook_violation_ban_threshold": 101,
                "orderbook_violation_warning_threshold": 11,
                "orderbook_violation_severe_threshold": 51,
                "orderbook_ban_duration": 7200.0,
                "dual_offers": True,
            },
            wallet={"max_fee_rate_sat_vb": 777.0},
        )

        class CapturingRunner:
            context: Any

            def __init__(self, plan: Plan, context: Any) -> None:
                self.plan = plan
                type(self).context = context

            async def run(self) -> Plan:
                return self.plan

        monkeypatch.setattr("jmwalletd.routers.tumbler.get_settings", lambda: settings)
        monkeypatch.setattr("jmwalletd.routers.tumbler.TumbleRunner", CapturingRunner)
        monkeypatch.setattr("jmwalletd._backend.get_backend", AsyncMock())
        maker_bot_cls = MagicMock()
        monkeypatch.setattr("maker.bot.MakerBot", maker_bot_cls)

        response = app_with_wallet.post(
            f"/api/v1/wallet/{WALLET}/tumbler/start",
            headers=_auth(auth_token),
        )

        assert response.status_code == 202, response.text
        asyncio.run(CapturingRunner.context.maker_factory(MagicMock()))
        maker_config = maker_bot_cls.call_args.kwargs["config"]
        assert isinstance(maker_config, MakerConfig)
        assert maker_config.network == NetworkType.SIGNET
        assert maker_config.bitcoin_network == NetworkType.REGTEST
        assert maker_config.data_dir == get_daemon_state().data_dir
        assert maker_config.directory_servers == [directory_server]
        assert maker_config.allow_clearnet_connections is allow_clearnet_connections
        assert maker_config.socks_host == "127.0.0.2"
        assert maker_config.socks_port == 9052
        assert maker_config.stream_isolation is False
        assert maker_config.connection_timeout == 45.0
        assert maker_config.tor_control.host == "127.0.0.3"
        assert maker_config.tor_control.port == 9053
        assert maker_config.tor_control.cookie_path == Path("/tmp/tumbler-control.cookie")
        assert maker_config.tor_target_host == "maker-service"
        assert maker_config.onion_host == "tumbler-maker.example.onion"
        assert maker_config.onion_serving_host == "0.0.0.0"
        assert maker_config.onion_serving_port == 5223
        assert maker_config.min_size == 200_000
        assert maker_config.cjfee_factor == 0.15
        assert maker_config.txfee_contribution_factor == 0.45
        assert maker_config.size_factor == 0.25
        assert maker_config.min_confirmations == 7
        assert maker_config.allow_mixdepth_zero_merge is True
        assert maker_config.merge_algorithm.value == "gradual"
        assert str(maker_config.mixdepth_selection_policy) == "concentrated"
        assert maker_config.min_fee_rate_sat_vb == 2.5
        assert maker_config.min_fee_block_target == 12
        assert maker_config.max_fee_rate_sat_vb == 777.0
        assert maker_config.session_timeout_sec == 600
        assert maker_config.pre_sign_timeout_sec == 120
        assert maker_config.identity_renewal_min_sec == 61
        assert maker_config.identity_renewal_max_sec == 121
        assert maker_config.identity_grace_sec == 90
        assert maker_config.identity_rotation_quiet_min_sec == 17
        assert maker_config.identity_rotation_quiet_max_sec == 29
        assert maker_config.pending_tx_timeout_min == 15
        assert maker_config.pending_tx_abandon_hours == 96
        assert maker_config.rescan_interval_sec == 780
        assert maker_config.message_rate_limit == 11
        assert maker_config.message_burst_limit == 111
        assert maker_config.offer_reannounce_delay_max == 432
        assert maker_config.directory_reconnect_interval == 75
        assert maker_config.directory_reconnect_max_retries == 8
        assert maker_config.directory_startup_timeout == 180
        assert maker_config.orderbook_rate_limit == 2
        assert maker_config.orderbook_rate_interval == 15.0
        assert maker_config.orderbook_violation_ban_threshold == 101
        assert maker_config.orderbook_violation_warning_threshold == 11
        assert maker_config.orderbook_violation_severe_threshold == 51
        assert maker_config.orderbook_ban_duration == 7200.0
        assert maker_config.offer_type is OfferType.SW0_ABSOLUTE
        assert maker_config.cj_fee_absolute == 0
        assert maker_config.no_fidelity_bond is True
        assert maker_config.offer_configs == []

        nick_change_callback = maker_bot_cls.call_args.kwargs["nick_change_callback"]
        nick_change_callback("J5OldTumblerMaker", "J5RotatedTumblerMaker")
        assert read_nick_state(get_daemon_state().data_dir, "maker") == "J5RotatedTumblerMaker"


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
        # 204 must carry an empty body; a non-empty body (e.g. ``b"null"``)
        # makes uvicorn raise "Response content longer than Content-Length".
        assert resp.content == b""
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

    def test_reconcile_preserves_running_confirmation_wait(self, tmp_path: Path) -> None:
        state = DaemonState(data_dir=tmp_path)
        plan = _build_plan()
        phase = _mark_stale_confirmation_wait(plan)
        save_plan(plan, tmp_path)

        reconciled = state.reconcile_stale_tumbler_plans()

        assert reconciled == [WALLET]
        disk = load_plan(WALLET, tmp_path)
        assert disk.status == PlanStatus.PENDING
        disk_phase = disk.phases[phase.index]
        assert isinstance(disk_phase, TakerCoinjoinPhase)
        assert disk_phase.status == PhaseStatus.AWAITING_CONFIRMATION
        assert disk_phase.txid == phase.txid

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


# ---------------------------------------------------------------------------
# build_tumbler_taker_config (regression: sweep "Not enough makers" failure)
# ---------------------------------------------------------------------------


class TestBuildTumblerTakerConfig:
    """``build_tumbler_taker_config`` must cap ``minimum_makers`` at the
    phase's ``counterparty_count`` so a sweep that legitimately selects N
    makers is not rejected against a stale higher policy threshold.

    Regression: tumbler phases planned with ``counterparty_count=2`` against
    a 3-maker test stack produced ``Not enough makers for sweep: 2`` because
    the walletd factory left ``minimum_makers`` at the policy default (4).
    """

    def _settings(self, *, policy_minimum_makers: int = 4) -> object:
        from jmcore.models import NetworkType
        from jmcore.settings import JoinMarketSettings

        # A real settings object so the shared config builder exercises the
        # same attribute surface (backend, wallet, tor, taker) as production.
        settings = JoinMarketSettings()
        settings.data_dir = Path("/tmp/jm-test")
        settings.network_config.network = NetworkType.REGTEST
        settings.network_config.directory_servers = []
        settings.network_config.nick_auth_directory_ids = {
            "directory.internal:5222": "test:tumbler-directory"
        }
        settings.bitcoin.backend_type = "descriptor_wallet"
        settings.tor.socks_host = "127.0.0.1"
        settings.tor.socks_port = 9050
        settings.tor.stream_isolation = True
        settings.taker.minimum_makers = policy_minimum_makers
        return settings

    def _phase(
        self,
        *,
        counterparty_count: int = 2,
        mixdepth: int = 0,
        amount: int = 0,
    ) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            counterparty_count=counterparty_count,
            mixdepth=mixdepth,
            amount=amount,
        )

    def test_caps_minimum_makers_at_phase_counterparties(self) -> None:
        from jmwalletd.routers.tumbler import build_tumbler_taker_config

        captured: dict[str, object] = {}

        def fake_taker_config_cls(**kwargs: object) -> object:
            captured.update(kwargs)
            return MagicMock()

        build_tumbler_taker_config(
            phase=self._phase(counterparty_count=2),
            mnemonic="dummy",
            jm_settings=self._settings(policy_minimum_makers=4),
            taker_config_cls=fake_taker_config_cls,
        )

        assert captured["counterparty_count"] == 2
        assert captured["minimum_makers"] == 2

    def test_keeps_policy_minimum_when_phase_count_is_higher(self) -> None:
        from jmwalletd.routers.tumbler import build_tumbler_taker_config

        captured: dict[str, object] = {}

        def fake_taker_config_cls(**kwargs: object) -> object:
            captured.update(kwargs)
            return MagicMock()

        build_tumbler_taker_config(
            phase=self._phase(counterparty_count=6),
            mnemonic="dummy",
            jm_settings=self._settings(policy_minimum_makers=4),
            taker_config_cls=fake_taker_config_cls,
        )

        assert captured["counterparty_count"] == 6
        assert captured["minimum_makers"] == 4

    def test_handles_missing_or_falsy_counterparty_count(self) -> None:
        from jmwalletd.routers.tumbler import build_tumbler_taker_config

        captured: dict[str, object] = {}

        def fake_taker_config_cls(**kwargs: object) -> object:
            captured.update(kwargs)
            return MagicMock()

        build_tumbler_taker_config(
            phase=self._phase(counterparty_count=0),
            mnemonic="dummy",
            jm_settings=self._settings(policy_minimum_makers=4),
            taker_config_cls=fake_taker_config_cls,
        )

        # Falsy / missing counterparty_count falls back to 1, which then caps
        # minimum_makers at 1 (matches taker.cli behaviour).
        assert captured["counterparty_count"] == 1
        assert captured["minimum_makers"] == 1

    def test_forwards_taker_policy_settings(self) -> None:
        """Regression: daemon tumbler phases must honor ``[taker]`` policy.

        The factory used to set only network/Tor/directory fields, so fee
        limits, timeouts, and the orderbook-wait knobs silently fell back to
        ``TakerConfig`` defaults for tumbles started through the API.
        """
        from jmwalletd.routers.tumbler import build_tumbler_taker_config

        settings: Any = self._settings()
        settings.taker.max_cj_fee_abs = 777
        settings.taker.market_fault_exclusion = True
        settings.taker.maker_timeout_sec = 90
        settings.taker.order_wait_time = 60.0
        settings.taker.orderbook_min_wait = 45.0
        settings.taker.orderbook_quiet_period = 20.0

        captured: dict[str, Any] = {}

        def fake_taker_config_cls(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return MagicMock()

        build_tumbler_taker_config(
            phase=self._phase(counterparty_count=2),
            mnemonic="dummy",
            jm_settings=settings,
            taker_config_cls=fake_taker_config_cls,
        )

        assert captured["max_cj_fee"].abs_fee == 777
        assert captured["market_fault_exclusion"] is True
        assert captured["maker_timeout_sec"] == 90
        assert captured["order_wait_time"] == 60.0
        assert captured["orderbook_min_wait"] == 45.0
        assert captured["orderbook_quiet_period"] == 20.0
        assert captured["nick_auth_directory_ids"] == {
            "directory.internal:5222": "test:tumbler-directory"
        }
        # The runner resolves destinations itself; the placeholder stays empty.
        assert captured["destination_address"].get_secret_value() == ""

    def test_applies_configset_fee_policy_overrides(self) -> None:
        """Regression (issue #566): fee policy set via configset (JAM's fee
        modal) must reach tumbler phase configs so a sat/vB rate works on
        backends without fee estimation (neutrino)."""
        from jmwalletd.routers.tumbler import build_tumbler_taker_config

        captured: dict[str, Any] = {}

        def fake_taker_config_cls(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return MagicMock()

        build_tumbler_taker_config(
            phase=self._phase(counterparty_count=2),
            mnemonic="dummy",
            jm_settings=self._settings(),
            taker_config_cls=fake_taker_config_cls,
            config_overrides={
                "POLICY": {
                    "tx_fees": "5000",
                    "max_cj_fee_abs": "30000",
                    "max_sweep_fee_change": "1.25",
                }
            },
        )

        assert captured["fee_rate"] == 5.0
        assert captured["fee_block_target"] is None
        assert captured["max_sweep_fee_change"] == 1.25
        assert captured["max_cj_fee"].abs_fee == 30000
