"""Tests for coinjoin endpoints."""

from __future__ import annotations

import time
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
    def test_start_coinjoin_requires_mnemonic(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.wallet_mnemonic = ""

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

        assert resp.status_code == 404
        assert "Wallet mnemonic not available" in resp.json()["message"]

    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("taker.taker.Taker")
    @patch("taker.config.TakerConfig")
    @patch("jmwalletd.routers.coinjoin.get_settings")
    def test_start_coinjoin(
        self,
        mock_get_settings: Mock,
        mock_config: Mock,
        mock_taker_cls: Mock,
        mock_backend: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client
        state = get_daemon_state()
        mock_taker = AsyncMock()
        mock_taker_cls.return_value = mock_taker

        from jmcore.models import NetworkType

        expected_dirs = ["testdirectoryfakeaddress.onion:5222"]
        mock_settings = Mock()
        mock_settings.get_directory_servers.return_value = expected_dirs
        mock_settings.network_config.network = NetworkType.SIGNET
        mock_settings.tor.socks_host = "127.0.0.1"
        mock_settings.tor.socks_port = 9050
        mock_settings.tor.stream_isolation = False
        mock_settings.tor.connection_timeout = 120.0
        # Taker/wallet policy must be real values so the config builder's
        # ``min(...)`` cap and field mapping do not operate on bare Mocks.
        mock_settings.taker.minimum_makers = 4
        mock_settings.taker.max_cj_fee_abs = 500
        mock_settings.taker.max_cj_fee_rel = "0.001"
        mock_settings.taker.tx_fee_factor = 0.2
        mock_settings.taker.fee_rate = None
        mock_settings.taker.fee_block_target = None
        mock_settings.taker.bondless_makers_allowance = 0.2
        mock_settings.taker.bond_value_exponent = 1.3
        mock_settings.taker.bondless_require_zero_fee = True
        mock_settings.taker.maker_timeout_sec = 60
        mock_settings.taker.order_wait_time = 120.0
        mock_settings.taker.tx_broadcast = "random-peer"
        mock_settings.taker.broadcast_peer_count = 3
        mock_settings.taker.rescan_interval_sec = 600
        mock_settings.taker.pending_tx_abandon_hours = 24
        mock_settings.taker.taker_utxo_age = 5
        mock_settings.taker.taker_utxo_retries = 3
        mock_settings.taker.taker_utxo_amtpercent = 20
        mock_settings.wallet.mixdepth_count = 5
        mock_settings.wallet.gap_limit = 20
        mock_settings.wallet.scan_range = 1000
        mock_settings.wallet.dust_threshold = 27300
        mock_settings.wallet.max_sats_freeze_reuse = -1
        mock_settings.wallet.max_fee_rate_sat_vb = 1000.0
        mock_settings.wallet.default_fee_block_target = 3
        mock_get_settings.return_value = mock_settings

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

        _, kwargs = mock_config.call_args
        assert kwargs["mnemonic"] == state.wallet_mnemonic
        assert kwargs["network"] == NetworkType.SIGNET
        assert kwargs["directory_servers"] == expected_dirs
        assert kwargs["socks_host"] == "127.0.0.1"
        assert kwargs["socks_port"] == 9050
        assert kwargs["stream_isolation"] is False
        # Policy settings must be forwarded (issue #530). minimum_makers is
        # capped to the 3 requested counterparties (from the policy default 4).
        assert kwargs["counterparty_count"] == 3
        assert kwargs["minimum_makers"] == 3
        assert kwargs["bondless_makers_allowance_require_zero_fee"] is True


class TestBuildCoinjoinTakerConfig:
    """``build_coinjoin_taker_config`` must forward ``[taker]`` policy settings.

    Regression (issue #530): the one-shot coinjoin endpoint built a
    ``TakerConfig`` with only network/Tor/directory fields, so policy settings
    like ``minimum_makers`` and ``bondless_require_zero_fee`` fell back to
    defaults. A request for fewer makers than the policy minimum then failed
    with ``Not enough makers selected``.
    """

    def _settings(
        self,
        *,
        minimum_makers: int = 4,
        bondless_require_zero_fee: bool = True,
        fee_rate: float | None = None,
        fee_block_target: int | None = None,
        tx_broadcast: str = "random-peer",
    ) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            taker=SimpleNamespace(
                minimum_makers=minimum_makers,
                max_cj_fee_abs=500,
                max_cj_fee_rel="0.001",
                tx_fee_factor=0.2,
                fee_rate=fee_rate,
                fee_block_target=fee_block_target,
                bondless_makers_allowance=0.2,
                bond_value_exponent=1.3,
                bondless_require_zero_fee=bondless_require_zero_fee,
                maker_timeout_sec=60,
                order_wait_time=120.0,
                orderbook_min_wait=45.0,
                orderbook_quiet_period=20.0,
                tx_broadcast=tx_broadcast,
                broadcast_peer_count=3,
                rescan_interval_sec=600,
                pending_tx_abandon_hours=24,
                taker_utxo_age=5,
                taker_utxo_retries=3,
                taker_utxo_amtpercent=20,
            ),
            wallet=SimpleNamespace(
                mixdepth_count=5,
                gap_limit=20,
                scan_range=1000,
                dust_threshold=27300,
                max_sats_freeze_reuse=-1,
                max_fee_rate_sat_vb=1000.0,
                default_fee_block_target=3,
            ),
            tor=SimpleNamespace(
                socks_host="127.0.0.1",
                socks_port=9050,
                stream_isolation=True,
                connection_timeout=120.0,
            ),
            network_config=SimpleNamespace(network="regtest"),
            get_directory_servers=lambda: [],
        )

    def _body(
        self,
        *,
        counterparties: int = 3,
        mixdepth: int = 0,
        amount_sats: int = 100_000,
        destination: str = "bcrt1qdest",
    ) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(
            counterparties=counterparties,
            mixdepth=mixdepth,
            amount_sats=amount_sats,
            destination=destination,
        )

    def _build(self, *, body: object, jm_settings: object) -> dict[str, object]:
        from jmwalletd.routers.coinjoin import build_coinjoin_taker_config

        captured: dict[str, object] = {}

        def fake_taker_config_cls(**kwargs: object) -> object:
            captured.update(kwargs)
            return Mock()

        build_coinjoin_taker_config(
            body=body,
            mnemonic="dummy",
            jm_settings=jm_settings,
            taker_config_cls=fake_taker_config_cls,
        )
        return captured

    def test_caps_minimum_makers_at_requested_counterparties(self) -> None:
        captured = self._build(
            body=self._body(counterparties=2),
            jm_settings=self._settings(minimum_makers=4),
        )
        assert captured["counterparty_count"] == 2
        assert captured["minimum_makers"] == 2

    def test_keeps_policy_minimum_when_request_count_is_higher(self) -> None:
        captured = self._build(
            body=self._body(counterparties=6),
            jm_settings=self._settings(minimum_makers=4),
        )
        assert captured["counterparty_count"] == 6
        assert captured["minimum_makers"] == 4

    def test_forwards_bondless_require_zero_fee(self) -> None:
        captured = self._build(
            body=self._body(),
            jm_settings=self._settings(bondless_require_zero_fee=False),
        )
        assert captured["bondless_makers_allowance_require_zero_fee"] is False

    def test_forwards_core_taker_policy_fields(self) -> None:
        from taker.config import MaxCjFee

        captured = self._build(body=self._body(), jm_settings=self._settings())
        max_cj_fee = captured["max_cj_fee"]
        assert isinstance(max_cj_fee, MaxCjFee)
        assert max_cj_fee.abs_fee == 500
        assert captured["tx_fee_factor"] == 0.2
        assert captured["maker_timeout_sec"] == 60
        assert captured["order_wait_time"] == 120.0
        # Regression: the adaptive orderbook-wait knobs must be forwarded too,
        # not fall back to the TakerConfig defaults (30.0 / 15.0).
        assert captured["orderbook_min_wait"] == 45.0
        assert captured["orderbook_quiet_period"] == 20.0
        assert captured["broadcast_peer_count"] == 3
        assert captured["taker_utxo_age"] == 5

    def test_fee_rate_takes_precedence_over_block_target(self) -> None:
        captured = self._build(
            body=self._body(),
            jm_settings=self._settings(fee_rate=12.5, fee_block_target=6),
        )
        assert captured["fee_rate"] == 12.5
        assert captured["fee_block_target"] is None

    def test_falls_back_to_wallet_default_block_target(self) -> None:
        captured = self._build(
            body=self._body(),
            jm_settings=self._settings(fee_rate=None, fee_block_target=None),
        )
        assert captured["fee_rate"] is None
        # Wallet default block target (3) is used when no fee policy is set.
        assert captured["fee_block_target"] == 3

    def test_invalid_tx_broadcast_falls_back_to_multiple_peers(self) -> None:
        from taker.config import BroadcastPolicy

        captured = self._build(
            body=self._body(),
            jm_settings=self._settings(tx_broadcast="not-a-policy"),
        )
        assert captured["tx_broadcast"] == BroadcastPolicy.MULTIPLE_PEERS


class TestStartMaker:
    def test_start_maker_requires_mnemonic(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.wallet_mnemonic = ""

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

        assert resp.status_code == 404
        assert "Wallet mnemonic not available" in resp.json()["message"]

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
    @patch("jmwalletd.routers.coinjoin.get_settings")
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
        state = get_daemon_state()
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
        assert kwargs["mnemonic"] == state.wallet_mnemonic
        assert kwargs["network"] == NetworkType.SIGNET
        assert kwargs["directory_servers"] == expected_dirs
        assert kwargs["socks_host"] == "127.0.0.1"
        assert kwargs["socks_port"] == 9050
        assert kwargs["stream_isolation"] is False

    @patch("jmwalletd.routers.coinjoin.remove_nick_state")
    @patch("jmwalletd.routers.coinjoin.write_nick_state")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("maker.bot.MakerBot")
    @patch("maker.config.MakerConfig")
    def test_start_maker_writes_nick_state(
        self,
        mock_config: Mock,
        mock_maker_cls: Mock,
        mock_backend: AsyncMock,
        mock_write_nick: Mock,
        mock_remove_nick: Mock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """Starting the maker via jmwalletd must write the nick state file."""
        client, token = authed_client
        state = get_daemon_state()
        mock_maker = AsyncMock()
        mock_maker.nick = "J5TestNickWalletd"
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
        assert resp.status_code == 202

        # Allow the background asyncio task to run to completion.
        time.sleep(0.1)

        mock_write_nick.assert_called_once_with(state.data_dir, "maker", "J5TestNickWalletd")
        mock_remove_nick.assert_called_once_with(state.data_dir, "maker")


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
