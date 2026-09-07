"""Tests for jmwalletd.routers.wallet — wallet lifecycle endpoints."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from jmwalletd.app import create_app
from jmwalletd.deps import get_daemon_state, set_daemon_state
from jmwalletd.errors import (
    InvalidCredentials,
    UnlockBackoff,
    WalletAlreadyUnlocked,
    WalletLifecycleQueueFull,
)
from jmwalletd.models import CreateWalletRequest, UnlockWalletRequest
from jmwalletd.routers import wallet as wallet_router
from jmwalletd.state import CoinjoinState, DaemonState


@pytest.fixture
def client(daemon_state: DaemonState) -> TestClient:
    """TestClient with our daemon_state injected."""
    application = create_app(data_dir=daemon_state.data_dir)
    set_daemon_state(daemon_state)
    return TestClient(application)


@pytest.fixture
def authed_client(
    daemon_state_with_wallet: DaemonState,
) -> tuple[TestClient, str]:
    """TestClient with loaded wallet + valid auth token."""
    application = create_app(data_dir=daemon_state_with_wallet.data_dir)
    set_daemon_state(daemon_state_with_wallet)
    pair = daemon_state_with_wallet.token_authority.issue("test_wallet.jmdat")
    client = TestClient(application)
    return client, pair.token


class TestGetInfo:
    def test_returns_version_and_backend(self, client: TestClient) -> None:
        resp = client.get("/api/v1/getinfo")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert data["backend"] == "joinmarket-ng"


class TestGetSession:
    def test_unauthenticated_no_wallet(self, client: TestClient) -> None:
        resp = client.get("/api/v1/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] is False
        assert data["wallet_name"] == ""

    def test_with_wallet_no_token(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] is True

    def test_with_invalid_token_returns_401(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/session", headers={"Authorization": "Bearer invalidtoken"})
        assert resp.status_code == 401

    def test_with_valid_token(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp = client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["session"] is True
        assert data["wallet_name"] == "test_wallet.jmdat"

    def test_broadcast_outcome_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.last_broadcast_policy = "random-peer"
        state.last_broadcast_method = "self-fallback"
        state.last_broadcast_fallback_reason = "peer_delivery_failed"

        public_data = client.get("/api/v1/session").json()
        assert public_data["broadcast_policy"] is None
        assert public_data["broadcast_method"] is None
        assert public_data["broadcast_fallback_reason"] is None

        resp = client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        private_data = resp.json()
        assert private_data["broadcast_policy"] == "random-peer"
        assert private_data["broadcast_method"] == "self-fallback"
        assert private_data["broadcast_fallback_reason"] == "peer_delivery_failed"

    def test_descriptor_wallet_name_exposed_when_authed(
        self,
        daemon_state_with_wallet: DaemonState,
    ) -> None:
        """When the active backend is a descriptor wallet, /session must
        expose its bitcoind wallet name to authenticated clients so they
        can address Bitcoin Core RPC endpoints (used by Playwright setup
        to issue listunspent / sendall against the right wallet)."""
        daemon_state_with_wallet.wallet_service.backend.wallet_name = "jm_deadbeef_regtest"
        application = create_app(data_dir=daemon_state_with_wallet.data_dir)
        set_daemon_state(daemon_state_with_wallet)
        pair = daemon_state_with_wallet.token_authority.issue("test_wallet.jmdat")
        client = TestClient(application)
        resp = client.get(
            "/api/v1/session",
            headers={"Authorization": f"Bearer {pair.token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["descriptor_wallet_name"] == "jm_deadbeef_regtest"

    def test_descriptor_wallet_name_absent_when_unauth(
        self,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """Unauthenticated /session must not leak the bitcoind wallet name."""
        client, _ = authed_client
        resp = client.get("/api/v1/session")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("descriptor_wallet_name") is None

    def test_rescanning_true_while_core_scans(self, authed_client: tuple[TestClient, str]) -> None:
        """/session must report rescanning=true while Bitcoin Core is still
        scanning, even if the daemon-side flag went stale (issue #551)."""
        client, _ = authed_client
        state = get_daemon_state()
        state.rescanning = False
        state.wallet_service.backend.get_rescan_status = AsyncMock(
            return_value={"in_progress": True, "progress": 0.5, "duration": 60}
        )

        resp = client.get("/api/v1/session")
        assert resp.status_code == 200
        assert resp.json()["rescanning"] is True

    def test_rescanning_false_when_idle(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        state = get_daemon_state()
        state.rescanning = False
        state.wallet_service.backend.get_rescan_status = AsyncMock(
            return_value={"in_progress": False}
        )

        resp = client.get("/api/v1/session")
        assert resp.status_code == 200
        assert resp.json()["rescanning"] is False


class TestSessionSchedule:
    """/session must expose the running tumble as a legacy schedule (#553)."""

    DEST = "bcrt1qpnv3nze7u6ecw63mn06ksxh497a3lryagh233q"

    def _install_fake_tumbler(self, state: DaemonState) -> None:
        from types import SimpleNamespace

        from tumbler.plan import PhaseStatus, Plan, TakerCoinjoinPhase

        plan = Plan(
            wallet_name="test_wallet.jmdat",
            destinations=[self.DEST],
            phases=[
                TakerCoinjoinPhase(
                    index=0,
                    mixdepth=0,
                    amount_fraction=0.25,
                    counterparty_count=4,
                    destination="INTERNAL",
                    wait_seconds=120.0,
                    status=PhaseStatus.COMPLETED,
                    txid="a" * 64,
                ),
                TakerCoinjoinPhase(
                    index=1,
                    mixdepth=1,
                    amount_fraction=0.0,
                    counterparty_count=6,
                    destination=self.DEST,
                    status=PhaseStatus.RUNNING,
                ),
            ],
            current_phase=1,
        )
        state.tumble_runner = SimpleNamespace(plan=plan)
        state.tumble_plan_wallet = state.wallet_name
        state.activate_coinjoin_state(CoinjoinState.TUMBLER_RUNNING)

    def test_schedule_populated_while_tumbler_runs(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        client, token = authed_client
        self._install_fake_tumbler(get_daemon_state())

        resp = client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["coinjoin_in_process"] is True
        assert data["schedule"] == [
            [0, 0.25, 4, "INTERNAL", 2.0, 16, 1],
            [1, 0.0, 6, self.DEST, 0.0, 16, 0],
        ]

    def test_schedule_hidden_without_token(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        self._install_fake_tumbler(get_daemon_state())

        resp = client.get("/api/v1/session")
        assert resp.status_code == 200
        assert resp.json()["schedule"] is None

    def test_schedule_null_when_idle(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp = client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["schedule"] is None

    def test_schedule_null_for_single_shot_taker(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        """Direct /taker/coinjoin runs carry no schedule, like the reference."""
        client, token = authed_client
        get_daemon_state().activate_coinjoin_state(CoinjoinState.TAKER_RUNNING)

        resp = client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["coinjoin_in_process"] is True
        assert data["schedule"] is None


class TestListWallets:
    def test_empty(self, client: TestClient, daemon_state: DaemonState) -> None:
        resp = client.get("/api/v1/wallet/all")
        assert resp.status_code == 200
        assert resp.json()["wallets"] == []

    def test_with_wallets(self, client: TestClient, daemon_state: DaemonState) -> None:
        (daemon_state.wallets_dir / "a.jmdat").touch()
        (daemon_state.wallets_dir / "b.jmdat").touch()
        resp = client.get("/api/v1/wallet/all")
        assert resp.status_code == 200
        wallets = resp.json()["wallets"]
        assert "a.jmdat" in wallets
        assert "b.jmdat" in wallets


class TestWalletCreate:
    @patch("jmwalletd.routers.wallet.create_wallet", new_callable=AsyncMock)
    def test_success(
        self, mock_create: AsyncMock, client: TestClient, daemon_state: DaemonState
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.backend.supports_tx_enumeration = False
        mock_create.return_value = (mock_ws, "abandon " * 11 + "about")
        daemon_state.start_tx_monitor = MagicMock(wraps=daemon_state.start_tx_monitor)

        resp = client.post(
            "/api/v1/wallet/create",
            json={"walletname": "new.jmdat", "password": "secret"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["walletname"] == "new.jmdat"
        assert "token" in data
        assert "seedphrase" in data
        assert daemon_state.wallet_mnemonic == data["seedphrase"]
        daemon_state.start_tx_monitor.assert_called_once_with(baseline_existing=False)

    @patch("jmwalletd.routers.wallet.create_wallet", new_callable=AsyncMock)
    def test_already_loaded_returns_401(
        self, mock_create: AsyncMock, authed_client: tuple[TestClient, str]
    ) -> None:
        client, _ = authed_client
        resp = client.post(
            "/api/v1/wallet/create",
            json={"walletname": "x.jmdat", "password": "p"},
        )
        assert resp.status_code == 401  # WalletAlreadyUnlocked

    @patch("jmwalletd.routers.wallet.create_wallet", new_callable=AsyncMock)
    def test_already_exists_returns_409(
        self, mock_create: AsyncMock, client: TestClient, daemon_state: DaemonState
    ) -> None:
        (daemon_state.wallets_dir / "existing.jmdat").touch()
        resp = client.post(
            "/api/v1/wallet/create",
            json={"walletname": "existing.jmdat", "password": "p"},
        )
        assert resp.status_code == 409  # WalletAlreadyExists

    async def test_concurrent_create_requests_serialize_lifecycle(
        self, daemon_state: DaemonState
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        wallet_service = MagicMock()
        wallet_service.backend.supports_tx_enumeration = False

        async def create(**_kwargs: Any) -> tuple[MagicMock, str]:
            started.set()
            await release.wait()
            return wallet_service, "abandon " * 11 + "about"

        with patch.object(
            wallet_router, "create_wallet", new=AsyncMock(side_effect=create)
        ) as mock_create:
            first = asyncio.create_task(
                wallet_router.wallet_create(
                    CreateWalletRequest(walletname="first.jmdat", password="secret"),
                    daemon_state,
                )
            )
            await started.wait()
            second = asyncio.create_task(
                wallet_router.wallet_create(
                    CreateWalletRequest(walletname="second.jmdat", password="secret"),
                    daemon_state,
                )
            )
            await asyncio.sleep(0)
            assert mock_create.await_count == 1

            release.set()
            await first
            with pytest.raises(WalletAlreadyUnlocked):
                await second


class TestWalletRecover:
    @patch("jmwalletd.routers.wallet.recover_wallet", new_callable=AsyncMock)
    def test_success(
        self, mock_recover: AsyncMock, client: TestClient, daemon_state: DaemonState
    ) -> None:
        mock_ws = MagicMock()
        mock_ws.backend.supports_tx_enumeration = False
        seedphrase = "abandon " * 11 + "about"
        mock_recover.return_value = mock_ws

        resp = client.post(
            "/api/v1/wallet/recover",
            json={
                "walletname": "recovered.jmdat",
                "password": "pass",
                "wallettype": "sw",
                "seedphrase": seedphrase,
                "scan_range": 2_500,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["walletname"] == "recovered.jmdat"
        assert data["seedphrase"] == seedphrase
        assert daemon_state.wallet_mnemonic == seedphrase
        assert mock_recover.await_args is not None
        assert mock_recover.await_args.kwargs["scan_range"] == 2_500

    @patch("jmwalletd.routers.wallet.recover_wallet", new_callable=AsyncMock)
    def test_monitor_failure_rolls_back_wallet_file(
        self, mock_recover: AsyncMock, client: TestClient, daemon_state: DaemonState
    ) -> None:
        wallet_path = daemon_state.wallets_dir / "retry.jmdat"
        mock_ws = MagicMock()

        async def recover(**_kwargs: Any) -> MagicMock:
            wallet_path.touch()
            return mock_ws

        mock_recover.side_effect = recover
        daemon_state.start_tx_monitor = MagicMock()
        daemon_state.wait_tx_monitor_ready = AsyncMock(return_value=False)
        seedphrase = "abandon " * 11 + "about"

        resp = client.post(
            "/api/v1/wallet/recover",
            json={
                "walletname": wallet_path.name,
                "password": "pass",
                "wallettype": "sw",
                "seedphrase": seedphrase,
            },
        )

        assert resp.status_code == 503
        assert not wallet_path.exists()
        assert daemon_state.wallet_loaded is False


class TestWalletUnlock:
    @patch("jmwalletd.routers.wallet.open_wallet_with_mnemonic", new_callable=AsyncMock)
    def test_success(
        self, mock_open_with_mnemonic: AsyncMock, client: TestClient, daemon_state: DaemonState
    ) -> None:
        (daemon_state.wallets_dir / "w.jmdat").touch()
        mock_ws = MagicMock()
        mock_ws.backend.supports_tx_enumeration = False
        mock_open_with_mnemonic.return_value = (mock_ws, "abandon " * 11 + "about")

        resp = client.post(
            "/api/v1/wallet/w.jmdat/unlock",
            json={"password": "secret"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["walletname"] == "w.jmdat"
        assert "token" in data
        assert daemon_state.wallet_mnemonic == "abandon " * 11 + "about"
        assert mock_open_with_mnemonic.await_args is not None
        assert mock_open_with_mnemonic.await_args.kwargs["sync_on_open"] is False

    def test_wallet_not_found(self, client: TestClient) -> None:
        resp = client.post(
            "/api/v1/wallet/nonexistent.jmdat/unlock",
            json={"password": "x"},
        )
        assert resp.status_code == 404

    def test_same_wallet_reissues_tokens(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.wallet_password = "secret"
        # Create the wallet file so the router finds it
        (state.wallets_dir / state.wallet_name).touch()
        # Unlock the same wallet with correct password
        resp = client.post(
            f"/api/v1/wallet/{state.wallet_name}/unlock",
            json={"password": "secret"},
        )
        assert resp.status_code == 200
        assert resp.json()["walletname"] == state.wallet_name

    def test_same_wallet_wrong_password(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        state = get_daemon_state()
        state.wallet_password = "correct"
        # Create the wallet file so the router finds it
        (state.wallets_dir / state.wallet_name).touch()
        resp = client.post(
            f"/api/v1/wallet/{state.wallet_name}/unlock",
            json={"password": "wrong"},
        )
        assert resp.status_code == 401  # InvalidCredentials

    def test_same_wallet_waits_for_monitor_baseline(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        client, _ = authed_client
        state = get_daemon_state()
        state.wallet_password = "secret"
        (state.wallets_dir / state.wallet_name).touch()

        with patch.object(
            state, "wait_tx_monitor_ready", AsyncMock(return_value=False)
        ) as wait_ready:
            resp = client.post(
                f"/api/v1/wallet/{state.wallet_name}/unlock",
                json={"password": "secret"},
            )

        assert resp.status_code == 503
        assert resp.json()["message"] == "Transaction monitor baseline is not ready."
        wait_ready.assert_awaited_once()
        assert state.wallet_loaded is False

    def test_wrong_cross_wallet_password_preserves_active_session(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        client, _ = authed_client
        state = get_daemon_state()
        target = "target.jmdat"
        (state.wallets_dir / target).touch()
        state.wallet_password = "active-password"
        active_service = state.wallet_service
        active_mnemonic = state.wallet_mnemonic
        active_name = state.wallet_name
        active_generation = state._wallet_generation
        active_sync_task = MagicMock()
        state._wallet_sync_task = active_sync_task
        tokens = state.token_authority.issue(active_name)

        with (
            patch(
                "jmwalletd.routers.wallet.verify_wallet_password",
                new_callable=AsyncMock,
                side_effect=ValueError("Wrong password"),
            ) as verify_password,
            patch(
                "jmwalletd.routers.wallet.open_wallet_with_mnemonic",
                new_callable=AsyncMock,
            ) as open_wallet,
        ):
            response = client.post(
                f"/api/v1/wallet/{target}/unlock",
                json={"password": "wrong"},
            )

        assert response.status_code == 401
        verify_password.assert_awaited_once()
        open_wallet.assert_not_awaited()
        assert state.wallet_service is active_service
        assert state.wallet_mnemonic == active_mnemonic
        assert state.wallet_name == active_name
        assert state._wallet_generation == active_generation
        assert state._wallet_sync_task is active_sync_task
        state.token_authority.verify_access(tokens.token)

    async def test_unlock_backoff_grows_and_success_resets(self, daemon_state: DaemonState) -> None:
        wallet_name = "backoff.jmdat"
        (daemon_state.wallets_dir / wallet_name).touch()
        request = UnlockWalletRequest(password="wrong")
        open_wallet = AsyncMock(side_effect=ValueError("Wrong password"))

        with patch.object(wallet_router, "open_wallet_with_mnemonic", new=open_wallet):
            with pytest.raises(InvalidCredentials):
                await wallet_router.wallet_unlock(wallet_name, request, daemon_state)
            first_delay = daemon_state.unlock_retry_delay(wallet_name)

            with pytest.raises(UnlockBackoff):
                await wallet_router.wallet_unlock(wallet_name, request, daemon_state)
            assert open_wallet.await_count == 1

            daemon_state._unlock_failures[wallet_name].retry_after = 0.0
            with pytest.raises(InvalidCredentials):
                await wallet_router.wallet_unlock(wallet_name, request, daemon_state)
            assert daemon_state.unlock_retry_delay(wallet_name) > first_delay

            daemon_state._unlock_failures[wallet_name].retry_after = 0.0
            wallet_service = MagicMock()
            wallet_service.backend.supports_tx_enumeration = False
            wallet_service.sync = AsyncMock()
            open_wallet.side_effect = None
            open_wallet.return_value = (wallet_service, "abandon " * 11 + "about")

            response = await wallet_router.wallet_unlock(
                wallet_name,
                UnlockWalletRequest(password="correct"),
                daemon_state,
            )

        assert response.walletname == wallet_name
        assert wallet_name not in daemon_state._unlock_failures
        assert daemon_state._wallet_sync_task is not None
        await daemon_state._wallet_sync_task

    async def test_wallet_lifecycle_admission_rejects_flood_before_kdf(
        self, daemon_state: DaemonState
    ) -> None:
        wallet_name = "bounded.jmdat"
        (daemon_state.wallets_dir / wallet_name).touch()
        started = asyncio.Event()
        release = asyncio.Event()
        wallet_service = MagicMock()
        wallet_service.backend.supports_tx_enumeration = False
        wallet_service.sync = AsyncMock()

        async def open_wallet(**_kwargs: Any) -> tuple[MagicMock, str]:
            started.set()
            await release.wait()
            return wallet_service, "abandon " * 11 + "about"

        request = UnlockWalletRequest(password="secret")
        with patch.object(
            wallet_router, "open_wallet_with_mnemonic", new=AsyncMock(side_effect=open_wallet)
        ) as mock_open:
            first = asyncio.create_task(
                wallet_router.wallet_unlock(wallet_name, request, daemon_state)
            )
            await started.wait()
            waiting = [
                asyncio.create_task(wallet_router.wallet_unlock(wallet_name, request, daemon_state))
                for _ in range(7)
            ]
            await asyncio.sleep(0)

            with pytest.raises(WalletLifecycleQueueFull):
                await wallet_router.wallet_unlock(wallet_name, request, daemon_state)
            assert mock_open.await_count == 1

            release.set()
            await first
            await asyncio.gather(*waiting)

        assert daemon_state._wallet_lifecycle_operations == 0
        assert daemon_state._wallet_sync_task is not None
        await daemon_state._wallet_sync_task

    async def test_same_wallet_compares_utf8_encoded_passwords(
        self, daemon_state: DaemonState
    ) -> None:
        wallet_name = "unicode.jmdat"
        daemon_state.wallet_service = MagicMock()
        daemon_state.wallet_name = wallet_name
        daemon_state.wallet_password = "secret-cafe\u00e9"
        (daemon_state.wallets_dir / wallet_name).touch()

        response = await wallet_router.wallet_unlock(
            wallet_name,
            UnlockWalletRequest(password="secret-cafe\u00e9"),
            daemon_state,
        )

        assert response.walletname == wallet_name

    async def test_same_wallet_none_password_never_matches_empty(
        self, daemon_state: DaemonState
    ) -> None:
        wallet_name = "empty.jmdat"
        daemon_state.wallet_service = MagicMock()
        daemon_state.wallet_name = wallet_name
        daemon_state.wallet_password = ""
        (daemon_state.wallets_dir / wallet_name).touch()
        request = UnlockWalletRequest.model_construct(password=None)

        with pytest.raises(InvalidCredentials):
            await wallet_router.wallet_unlock(wallet_name, request, daemon_state)


class TestWalletLock:
    def test_requires_auth(self, client: TestClient) -> None:
        resp = client.get("/api/v1/wallet/w.jmdat/lock")
        assert resp.status_code in (401, 404)

    def test_lock_loaded_wallet(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/lock",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["walletname"] == "test_wallet.jmdat"
        assert data["already_locked"] is False


class TestTokenRefresh:
    def test_refresh_success(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        pair = state.token_authority.issue(state.wallet_name)

        resp = client.post(
            "/api/v1/token",
            json={"grant_type": "refresh_token", "refresh_token": pair.refresh_token},
            headers={"Authorization": f"Bearer {pair.token}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "token" in data
        assert "refresh_token" in data
        assert data["walletname"] == state.wallet_name

    def test_reunlock_does_not_invalidate_existing_refresh_token(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        """Regression: a second unlock of the already-unlocked wallet (another
        tab/device) used to rotate the refresh key, so the first client's next
        refresh failed with "Signature verification failed" while its
        websocket (access token) kept working."""
        client, _ = authed_client
        state = get_daemon_state()
        state.wallet_password = "secret"
        (state.wallets_dir / state.wallet_name).touch()
        pair = state.token_authority.issue(state.wallet_name)

        # Second client unlocks the same, already-unlocked wallet.
        resp = client.post(
            f"/api/v1/wallet/{state.wallet_name}/unlock",
            json={"password": "secret"},
        )
        assert resp.status_code == 200

        # Make sure we are past any rotation grace: the first client's
        # refresh token must be valid via the live key, not the grace window.
        state.token_authority._previous_refresh_key_expiry = 0.0

        resp = client.post(
            "/api/v1/token",
            json={"grant_type": "refresh_token", "refresh_token": pair.refresh_token},
            headers={"Authorization": f"Bearer {pair.token}"},
        )
        assert resp.status_code == 200, resp.text

    def test_concurrent_refresh_within_grace_succeeds(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        """Regression: two near-simultaneous refreshes with the same token
        (browser retry / second tab) must both succeed within the rotation
        grace window instead of logging the client out."""
        client, _ = authed_client
        state = get_daemon_state()
        pair = state.token_authority.issue(state.wallet_name)

        first = client.post(
            "/api/v1/token",
            json={"grant_type": "refresh_token", "refresh_token": pair.refresh_token},
            headers={"Authorization": f"Bearer {pair.token}"},
        )
        assert first.status_code == 200
        # The same (now-rotated-away) refresh token is retried immediately.
        second = client.post(
            "/api/v1/token",
            json={"grant_type": "refresh_token", "refresh_token": pair.refresh_token},
            headers={"Authorization": f"Bearer {pair.token}"},
        )
        assert second.status_code == 200, second.text

    def test_wrong_grant_type(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        pair = state.token_authority.issue(state.wallet_name)

        resp = client.post(
            "/api/v1/token",
            json={"grant_type": "password", "refresh_token": pair.refresh_token},
            headers={"Authorization": f"Bearer {pair.token}"},
        )
        assert resp.status_code == 400

    def test_invalid_refresh_token(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        pair = state.token_authority.issue(state.wallet_name)

        resp = client.post(
            "/api/v1/token",
            json={"grant_type": "refresh_token", "refresh_token": "invalid"},
            headers={"Authorization": f"Bearer {pair.token}"},
        )
        assert resp.status_code == 401


class TestResponseHeaders:
    """Check that CORS and cache-control headers are set."""

    def test_cache_control(self, client: TestClient) -> None:
        resp = client.get("/api/v1/getinfo")
        assert "no-cache" in resp.headers.get("cache-control", "")
        assert "no-store" in resp.headers.get("cache-control", "")

    def test_cors_headers(self, client: TestClient) -> None:
        # Untrusted origin -- should NOT be allowed
        resp = client.options(
            "/api/v1/getinfo",
            headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"},
        )
        assert resp.headers.get("access-control-allow-origin") is None

        # Trusted local origin -- should be allowed
        origin = "http://localhost:3000"
        resp = client.options(
            "/api/v1/getinfo",
            headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
        )
        assert resp.headers.get("access-control-allow-origin") == origin


class TestSessionOfferList:
    """Verify that the session endpoint reads offer_list from the JoinMarket-NG maker.

    Note: In the reference implementation (original JoinMarket), this was
    a more permissive endpoint. JoinMarket-NG enforces stricter privacy here.
    """

    def test_offer_list_from_maker_ref(self, authed_client: tuple[TestClient, str]) -> None:
        """When a maker is running, the session should return its offers."""
        client, token = authed_client
        state = get_daemon_state()

        # Simulate a running maker with current_offers.
        maker = MagicMock()
        offer = MagicMock()
        offer.oid = 0
        offer.ordertype = "sw0absoffer"
        offer.minsize = 100_000
        offer.maxsize = 50_000_000
        offer.txfee = 0
        offer.cjfee = "250"
        maker.current_offers = [offer]

        state._maker_ref = maker
        state.maker_running = True

        resp = client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["maker_running"] is True
        assert data["offer_list"] is not None
        assert len(data["offer_list"]) == 1
        assert data["offer_list"][0]["ordertype"] == "sw0absoffer"
        assert data["offer_list"][0]["cjfee"] == "250"
        assert data["offer_list"][0]["minsize"] == 100_000

    def test_nickname_from_current_maker_generation(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        """The live maker nickname must take precedence after identity rotation."""
        client, token = authed_client
        state = get_daemon_state()
        maker = MagicMock()
        maker.nick = "J5RotatedMaker"
        maker.current_offers = []
        state._maker_ref = maker
        state.maker_running = True
        state.nickname = "J5StartupMaker"

        resp = client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})

        assert resp.status_code == 200
        assert resp.json()["nickname"] == "J5RotatedMaker"

    def test_offer_list_none_without_maker(self, authed_client: tuple[TestClient, str]) -> None:
        """Without a maker reference, offer_list should be None."""
        client, token = authed_client
        state = get_daemon_state()
        state._maker_ref = None
        state.offer_list = None

        resp = client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["offer_list"] is None

    def test_offer_list_fallback_to_state(self, authed_client: tuple[TestClient, str]) -> None:
        """If maker ref has no offers, fall back to state.offer_list."""
        client, token = authed_client
        state = get_daemon_state()

        maker = MagicMock()
        maker.current_offers = []
        state._maker_ref = maker
        state.maker_running = True
        state.offer_list = [{"oid": 0, "ordertype": "sw0absoffer", "cjfee": "100"}]

        resp = client.get("/api/v1/session", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        data = resp.json()
        # Falls back to state.offer_list when maker has no offers.
        assert data["offer_list"] is not None
        assert data["offer_list"][0]["cjfee"] == "100"
