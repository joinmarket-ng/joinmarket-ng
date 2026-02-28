"""Tests for coinjoin endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from jmwalletd.deps import get_daemon_state
from jmwalletd.state import CoinjoinState


@pytest.fixture
def authed_client(app_with_wallet: TestClient, auth_token: str) -> tuple[TestClient, str]:
    """Return an authenticated client and the token used."""
    return app_with_wallet, auth_token


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestDirectSend:
    @patch("jmwalletd.send.do_direct_send")
    def test_direct_send_success(
        self,
        mock_send: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client

        # Mock the result object
        mock_result = Mock()
        mock_result.txid = "txid123"
        mock_result.tx_hex = "rawhex"
        mock_result.hex = "rawhex"
        mock_result.model_dump.return_value = {}
        # Make attributes accessible
        mock_result.inputs = []
        mock_result.outputs = []
        mock_result.locktime = 0
        mock_result.version = 2

        mock_send.return_value = mock_result

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/taker/direct-send",
            json={
                "mixdepth": 0,
                "amount_sats": 1000,
                "destination": "bcrt1qdest",
                "txfee": 500,
            },
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json()["txinfo"]["txid"] == "txid123"
        mock_send.assert_awaited_once()

    def test_direct_send_while_taker_running(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.taker_running = True

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/taker/direct-send",
            json={"mixdepth": 0, "amount_sats": 1000, "destination": "addr"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 400


class TestDoCoinjoin:
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("taker.taker.Taker")
    @patch("taker.config.TakerConfig")
    def test_start_coinjoin(
        self,
        mock_config: Mock,
        mock_taker_cls: Mock,
        mock_backend: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client
        mock_taker = AsyncMock()
        mock_taker_cls.return_value = mock_taker

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/taker/coinjoin",
            json={
                "mixdepth": 0,
                "amount_sats": 100000,
                "destination": "bcrt1qdest",
                "counterparties": 3,
                "txfee": 500,
            },
            headers=_auth_headers(token),
        )
        assert resp.status_code == 202


class TestStartMaker:
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("maker.bot.MakerBot")
    @patch("maker.config.MakerConfig")
    def test_start_maker(
        self,
        mock_config: Mock,
        mock_maker_cls: Mock,
        mock_backend: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client
        mock_maker = AsyncMock()
        mock_maker.nick = "JmMaker"
        mock_maker.current_offers = []
        mock_maker_cls.return_value = mock_maker

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/maker/start",
            json={
                "txfee": "1000",
                "cjfee_a": "500",
                "cjfee_r": "0.002",
                "ordertype": "sw0reloffer",
                "minsize": "100000",
            },
            headers=_auth_headers(token),
        )
        if resp.status_code != 202:
            print(f"Error response: {resp.text}")
        assert resp.status_code == 202

    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("maker.bot.MakerBot")
    @patch("maker.config.MakerConfig")
    @patch("jmcore.settings.get_settings")
    def test_start_maker_uses_directory_servers_from_settings(
        self,
        mock_get_settings: Mock,
        mock_config: Mock,
        mock_maker_cls: Mock,
        mock_backend: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """MakerConfig must receive directory servers and Tor config from JoinMarketSettings."""
        client, token = authed_client
        mock_maker = AsyncMock()
        mock_maker.nick = "JmMaker"
        mock_maker.current_offers = []
        mock_maker_cls.return_value = mock_maker

        from jmcore.models import NetworkType

        expected_dirs = ["testdirectoryfakeaddress.onion:5222"]
        mock_settings = Mock()
        mock_settings.get_directory_servers.return_value = expected_dirs
        mock_settings.network_config.network = NetworkType.SIGNET
        mock_settings.tor.socks_host = "127.0.0.1"
        mock_settings.tor.socks_port = 9050
        mock_settings.tor.stream_isolation = False
        mock_get_settings.return_value = mock_settings

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/maker/start",
            json={
                "txfee": "1000",
                "cjfee_a": "500",
                "cjfee_r": "0.002",
                "ordertype": "sw0reloffer",
                "minsize": "100000",
            },
            headers=_auth_headers(token),
        )
        assert resp.status_code == 202

        _, kwargs = mock_config.call_args
        assert kwargs["network"] == NetworkType.SIGNET
        assert kwargs["directory_servers"] == expected_dirs
        assert kwargs["socks_host"] == "127.0.0.1"
        assert kwargs["socks_port"] == 9050
        assert kwargs["stream_isolation"] is False


class TestStopMaker:
    def test_stop_maker(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.activate_coinjoin_state(CoinjoinState.MAKER_RUNNING)
        state.maker_running = True

        mock_maker = AsyncMock()
        state._maker_ref = mock_maker

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/maker/stop",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 202
        assert state.maker_running is False
        assert state.coinjoin_state == CoinjoinState.NOT_RUNNING
        assert state._maker_ref is None

    def test_stop_maker_not_running(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.maker_running = False
        # Ensure wallet is loaded so we don't get 401
        if state.wallet_service is None:
            state.wallet_service = Mock()

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/maker/stop",
            headers=_auth_headers(token),
        )
        # ServiceNotStarted is a 401 in jmwalletd/errors.py
        assert resp.status_code == 401
