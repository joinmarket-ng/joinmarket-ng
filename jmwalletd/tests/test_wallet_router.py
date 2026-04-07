"""Tests for jmwalletd.routers.wallet — wallet lifecycle endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from jmwalletd.app import create_app
from jmwalletd.deps import get_daemon_state, set_daemon_state
from jmwalletd.state import DaemonState


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
    def test_returns_version(self, client: TestClient) -> None:
        resp = client.get("/api/v1/getinfo")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data


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
        mock_create.return_value = (mock_ws, "abandon " * 11 + "about")

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


class TestWalletRecover:
    @patch("jmwalletd.routers.wallet.recover_wallet", new_callable=AsyncMock)
    def test_success(
        self, mock_recover: AsyncMock, client: TestClient, daemon_state: DaemonState
    ) -> None:
        mock_ws = MagicMock()
        seedphrase = "abandon " * 11 + "about"
        mock_recover.return_value = mock_ws

        resp = client.post(
            "/api/v1/wallet/recover",
            json={
                "walletname": "recovered.jmdat",
                "password": "pass",
                "wallettype": "sw",
                "seedphrase": seedphrase,
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["walletname"] == "recovered.jmdat"
        assert data["seedphrase"] == seedphrase
        assert daemon_state.wallet_mnemonic == seedphrase


class TestWalletUnlock:
    @patch("jmwalletd.routers.wallet.open_wallet_with_mnemonic", new_callable=AsyncMock)
    def test_success(
        self, mock_open_with_mnemonic: AsyncMock, client: TestClient, daemon_state: DaemonState
    ) -> None:
        (daemon_state.wallets_dir / "w.jmdat").touch()
        mock_open_with_mnemonic.return_value = (MagicMock(), "abandon " * 11 + "about")

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
