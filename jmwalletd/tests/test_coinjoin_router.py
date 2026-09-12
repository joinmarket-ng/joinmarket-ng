"""Tests for coinjoin endpoints."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from fastapi.testclient import TestClient

from jmwalletd.deps import get_daemon_state
from jmwalletd.state import CoinjoinState
from taker.models import TakerState


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
        assert mock_send.call_args.kwargs["rbf"] is True

    @patch("jmwalletd.send.do_direct_send")
    def test_direct_send_forwards_rbf_opt_out(
        self,
        mock_send: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client
        mock_send.return_value = Mock(
            txid="txid123",
            tx_hex="rawhex",
            inputs=[],
            outputs=[],
            locktime=840_000,
            version=2,
        )

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/taker/direct-send",
            json={
                "mixdepth": 0,
                "amount_sats": 1000,
                "destination": "bcrt1qdest",
                "rbf": False,
            },
            headers=_auth_headers(token),
        )

        assert resp.status_code == 200
        assert mock_send.call_args.kwargs["rbf"] is False
        assert resp.json()["txinfo"]["nVersion"] == 2
        assert resp.json()["txinfo"]["nLockTime"] == 840_000

    @patch("jmwalletd.send.do_direct_send")
    def test_direct_send_honors_configset_fee_rate(
        self,
        mock_send: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """Regression (issue #566): a sat/vB rate set via configset (JAM fee
        modal) must be forwarded to the direct-send path instead of being
        silently ignored."""
        client, token = authed_client
        state = get_daemon_state()
        state.config_overrides["POLICY"] = {
            "tx_fees": "5000",
            "tx_fees_factor": "0.4",
        }

        mock_result = Mock()
        mock_result.txid = "txid123"
        mock_result.tx_hex = "rawhex"
        mock_result.hex = "rawhex"
        mock_result.model_dump.return_value = {}
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
                "txfee": 0,
            },
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200

        _, kwargs = mock_send.call_args
        assert kwargs["fee_rate"] == 5.0
        assert kwargs["fee_target_blocks"] is None
        assert kwargs["tx_fee_factor"] == 0.4

    @patch("jmwalletd.send.do_direct_send")
    def test_direct_send_uses_configured_fee_defaults(
        self,
        mock_send: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client
        mock_send.return_value = Mock(
            txid="txid123",
            tx_hex="rawhex",
            hex="rawhex",
            inputs=[],
            outputs=[],
            locktime=0,
            version=2,
        )

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/taker/direct-send",
            json={"mixdepth": 0, "amount_sats": 1000, "destination": "bcrt1qdest"},
            headers=_auth_headers(token),
        )

        assert resp.status_code == 200
        kwargs = mock_send.call_args.kwargs
        assert kwargs["fee_target_blocks"] == 3
        assert kwargs["tx_fee_factor"] == 0.2

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

    @patch("jmwalletd.send.do_direct_send")
    def test_direct_send_forwards_input_utxos(
        self,
        mock_send: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """Explicit input UTXOs (issue #587) reach do_direct_send unchanged."""
        client, token = authed_client
        mock_send.return_value = Mock(
            txid="txid123",
            tx_hex="rawhex",
            hex="rawhex",
            inputs=[],
            outputs=[],
            locktime=0,
            version=2,
        )
        outpoint = f"{'aa' * 32}:0"

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/taker/direct-send",
            json={
                "mixdepth": 0,
                "amount_sats": 1000,
                "destination": "bcrt1qdest",
                "input_utxos": [outpoint],
            },
            headers=_auth_headers(token),
        )

        assert resp.status_code == 200
        assert mock_send.call_args.kwargs["input_utxos"] == [outpoint]

    @patch("jmwalletd.send.do_direct_send")
    def test_direct_send_omitted_input_utxos_is_none(
        self,
        mock_send: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client
        mock_send.return_value = Mock(
            txid="txid123",
            tx_hex="rawhex",
            hex="rawhex",
            inputs=[],
            outputs=[],
            locktime=0,
            version=2,
        )

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/taker/direct-send",
            json={"mixdepth": 0, "amount_sats": 1000, "destination": "bcrt1qdest"},
            headers=_auth_headers(token),
        )

        assert resp.status_code == 200
        assert mock_send.call_args.kwargs["input_utxos"] is None

    @patch("jmwalletd.send.do_direct_send")
    def test_direct_send_rejects_unusable_input_utxo(
        self,
        mock_send: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """A ValueError from the wallet layer (frozen/not-found/etc.) becomes a 400."""
        client, token = authed_client
        mock_send.side_effect = ValueError(f"Input UTXO {'aa' * 32}:0 not found in mixdepth 0")

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/taker/direct-send",
            json={
                "mixdepth": 0,
                "amount_sats": 1000,
                "destination": "bcrt1qdest",
                "input_utxos": [f"{'aa' * 32}:0"],
            },
            headers=_auth_headers(token),
        )

        assert resp.status_code == 400
        assert "not found in mixdepth 0" in resp.json()["message"]


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
    @pytest.mark.parametrize(
        "input_utxos",
        [None, [f"{'aa' * 32}:0"]],
        ids=["omitted", "supplied"],
    )
    def test_start_coinjoin(
        self,
        mock_get_settings: Mock,
        mock_config: Mock,
        mock_taker_cls: Mock,
        mock_backend: AsyncMock,
        authed_client: tuple[TestClient, str],
        input_utxos: list[str] | None,
    ) -> None:
        client, token = authed_client
        state = get_daemon_state()
        mock_taker = AsyncMock()
        mock_taker.last_broadcast_policy = "random-peer"
        mock_taker.last_broadcast_method = "self-fallback"
        mock_taker.last_broadcast_fallback_reason = "peer_delivery_failed"
        mock_taker.state = TakerState.COMPLETE
        mock_taker.txid = "f" * 64
        mock_taker.last_failure_reason = None
        mock_taker_cls.return_value = mock_taker

        from pathlib import Path

        from jmcore.models import NetworkType
        from jmcore.settings import JoinMarketSettings

        expected_dirs = ["testdirectoryfakeaddress.onion:5222"]
        # A real settings object so the shared config builder exercises the
        # same attribute surface (backend, wallet, tor, taker) as production.
        mock_settings = JoinMarketSettings()
        mock_settings.data_dir = Path("/tmp/jm-test")
        mock_settings.network_config.network = NetworkType.SIGNET
        mock_settings.network_config.bitcoin_network = NetworkType.REGTEST
        mock_settings.network_config.directory_servers = expected_dirs
        mock_settings.bitcoin.backend_type = "descriptor_wallet"
        mock_settings.tor.socks_host = "127.0.0.1"
        mock_settings.tor.socks_port = 9050
        mock_settings.tor.stream_isolation = False
        mock_settings.taker.minimum_makers = 4
        mock_get_settings.return_value = mock_settings

        request_body: dict[str, Any] = {
            "mixdepth": 0,
            "amount_sats": 100000,
            "destination": "bcrt1qdest",
            "counterparties": 3,
            "txfee": 500,
        }
        if input_utxos is not None:
            request_body["input_utxos"] = input_utxos

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/taker/coinjoin",
            json=request_body,
            headers=_auth_headers(token),
        )
        assert resp.status_code == 202

        _, kwargs = mock_config.call_args
        assert kwargs["mnemonic"].get_secret_value() == state.wallet_mnemonic
        assert kwargs["network"] == NetworkType.SIGNET
        assert kwargs["bitcoin_network"] == NetworkType.REGTEST
        assert kwargs["directory_servers"] == expected_dirs
        assert kwargs["socks_host"] == "127.0.0.1"
        assert kwargs["socks_port"] == 9050
        assert kwargs["stream_isolation"] is False
        # Policy settings must be forwarded (issue #530). minimum_makers is
        # capped to the 3 requested counterparties (from the policy default 4).
        assert kwargs["counterparty_count"] == 3
        assert kwargs["minimum_makers"] == 3
        assert kwargs["bondless_makers_allowance_require_zero_fee"] is True
        assert mock_backend.await_args is not None
        assert mock_backend.await_args.kwargs["network"] == "regtest"
        assert mock_taker.do_coinjoin.await_args.kwargs["input_utxos"] == input_utxos
        assert state.last_broadcast_policy == "random-peer"
        assert state.last_broadcast_method == "self-fallback"
        assert state.last_broadcast_fallback_reason == "peer_delivery_failed"
        assert state.last_taker_status == "complete"
        assert state.last_taker_txid == "f" * 64
        assert state.last_taker_error is None


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
        from pathlib import Path

        from jmcore.models import NetworkType
        from jmcore.settings import JoinMarketSettings

        # A real settings object (not a hand-maintained stub) so the shared
        # kwargs builder exercises the same attribute surface as production.
        settings = JoinMarketSettings()
        settings.data_dir = Path("/tmp/jm-test")
        settings.network_config.network = NetworkType.REGTEST
        settings.network_config.directory_servers = []
        settings.bitcoin.backend_type = "descriptor_wallet"
        settings.tor.socks_host = "127.0.0.1"
        settings.tor.socks_port = 9050
        settings.tor.stream_isolation = True
        settings.tor.connection_timeout = 120.0
        settings.taker.minimum_makers = minimum_makers
        settings.taker.max_cj_fee_abs = 500
        settings.taker.max_cj_fee_rel = "0.001"
        settings.taker.tx_fee_factor = 0.2
        settings.taker.fee_rate = fee_rate
        settings.taker.fee_block_target = fee_block_target
        settings.taker.bondless_makers_allowance = 0.2
        settings.taker.bond_value_exponent = 1.3
        settings.taker.bondless_require_zero_fee = bondless_require_zero_fee
        settings.taker.maker_timeout_sec = 60
        settings.taker.order_wait_time = 120.0
        settings.taker.orderbook_min_wait = 45.0
        settings.taker.orderbook_quiet_period = 20.0
        settings.taker.tx_broadcast = tx_broadcast
        settings.taker.broadcast_peer_count = 3
        settings.taker.rescan_interval_sec = 600
        settings.taker.pending_tx_abandon_hours = 24
        settings.taker.taker_utxo_age = 5
        settings.taker.taker_utxo_retries = 3
        settings.taker.taker_utxo_amtpercent = 20
        settings.wallet.mixdepth_count = 5
        settings.wallet.gap_limit = 20
        settings.wallet.scan_range = 1000
        settings.wallet.dust_threshold = 27300
        settings.wallet.max_sats_freeze_reuse = -1
        settings.wallet.max_fee_rate_sat_vb = 1000.0
        settings.wallet.default_fee_block_target = 3
        return settings

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

    def _build(
        self,
        *,
        body: object,
        jm_settings: object,
        config_overrides: dict[str, dict[str, str]] | None = None,
    ) -> dict[str, object]:
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
            config_overrides=config_overrides,
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

    # Regression (issue #566): fee policy set via configset (JAM's fee modal)
    # must reach the taker config; it used to be stored but never applied, so
    # neutrino users hit "Cannot use --block-target with neutrino backend"
    # even after choosing a sat/vB rate in the UI.

    def test_configset_tx_fees_rate_overrides_default_block_target(self) -> None:
        captured = self._build(
            body=self._body(),
            jm_settings=self._settings(fee_rate=None, fee_block_target=None),
            config_overrides={"POLICY": {"tx_fees": "5000"}},
        )
        assert captured["fee_rate"] == 5.0
        assert captured["fee_block_target"] is None

    def test_configset_tx_fees_rate_overrides_settings_fee_rate(self) -> None:
        captured = self._build(
            body=self._body(),
            jm_settings=self._settings(fee_rate=12.5),
            config_overrides={"POLICY": {"tx_fees": "2000"}},
        )
        assert captured["fee_rate"] == 2.0
        assert captured["fee_block_target"] is None

    def test_configset_tx_fees_block_target(self) -> None:
        captured = self._build(
            body=self._body(),
            jm_settings=self._settings(fee_rate=None, fee_block_target=None),
            config_overrides={"POLICY": {"tx_fees": "6"}},
        )
        assert captured["fee_rate"] is None
        assert captured["fee_block_target"] == 6

    def test_configset_max_cj_fee_and_factor_overrides(self) -> None:
        captured = self._build(
            body=self._body(),
            jm_settings=self._settings(),
            config_overrides={
                "POLICY": {
                    "max_cj_fee_abs": "30000",
                    "max_cj_fee_rel": "0.0003",
                    "max_sweep_fee_change": "1.25",
                    "tx_fees_factor": "0.5",
                }
            },
        )
        max_cj_fee: Any = captured["max_cj_fee"]
        assert max_cj_fee.abs_fee == 30000
        assert max_cj_fee.rel_fee == "0.0003"
        assert captured["max_sweep_fee_change"] == 1.25
        assert captured["tx_fee_factor"] == 0.5

    def test_invalid_configset_values_fall_back_to_settings(self) -> None:
        captured = self._build(
            body=self._body(),
            jm_settings=self._settings(fee_rate=None, fee_block_target=None),
            config_overrides={"POLICY": {"tx_fees": "garbage"}},
        )
        assert captured["fee_rate"] is None
        assert captured["fee_block_target"] == 3


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
    @patch("jmwalletd.routers.coinjoin.get_settings")
    @pytest.mark.parametrize("allow_clearnet_connections", [False, True])
    def test_start_maker_uses_runtime_settings(
        self,
        mock_get_settings: Mock,
        mock_maker_cls: Mock,
        mock_backend: AsyncMock,
        authed_client: tuple[TestClient, str],
        allow_clearnet_connections: bool,
    ) -> None:
        """REST makers retain request offers and configured runtime policy."""
        client, token = authed_client
        state = get_daemon_state()
        mock_maker = AsyncMock()
        mock_maker.nick = "JmMaker"
        mock_maker.current_offers = []
        mock_maker_cls.return_value = mock_maker

        from pathlib import Path

        from jmcore.models import NetworkType, OfferType
        from jmcore.settings import JoinMarketSettings
        from maker.config import MakerConfig

        expected_dirs = ["testdirectoryfakeaddress.onion:5222"]
        mock_settings = JoinMarketSettings()
        mock_settings.network_config.network = NetworkType.SIGNET
        mock_settings.network_config.bitcoin_network = NetworkType.REGTEST
        mock_settings.network_config.directory_servers = expected_dirs
        mock_settings.network_config.allow_clearnet_connections = allow_clearnet_connections
        mock_settings.network_config.nick_auth_mode = "require_verified"
        expected_ids = {expected_dirs[0]: "test:walletd-directory"}
        mock_settings.network_config.nick_auth_directory_ids = expected_ids
        mock_settings.tor.socks_host = "127.0.0.1"
        mock_settings.tor.socks_port = 9050
        mock_settings.tor.stream_isolation = False
        mock_settings.tor.connection_timeout = 45.0
        mock_settings.tor.control_host = "127.0.0.2"
        mock_settings.tor.control_port = 9052
        mock_settings.tor.cookie_path = "/tmp/walletd-control.cookie"
        mock_settings.tor.target_host = "maker-service"
        # Offer values are deliberately different from the request below: the
        # REST body remains the source of truth for its single offer.
        mock_settings.maker.offer_type = "sw0absoffer"
        mock_settings.maker.min_size = 200_000
        mock_settings.maker.cj_fee_relative = "0.003"
        mock_settings.maker.cj_fee_absolute = 700
        mock_settings.maker.tx_fee_contribution = 800
        mock_settings.maker.dual_offers = True
        mock_settings.maker.cjfee_factor = 0.15
        mock_settings.maker.txfee_contribution_factor = 0.45
        mock_settings.maker.size_factor = 0.25
        mock_settings.maker.min_confirmations = 7
        mock_settings.maker.allow_mixdepth_zero_merge = True
        mock_settings.maker.merge_algorithm = "gradual"
        mock_settings.maker.mixdepth_selection_policy = "concentrated"
        mock_settings.maker.min_fee_rate_sat_vb = 2.5
        mock_settings.maker.min_fee_block_target = 12
        mock_settings.wallet.max_fee_rate_sat_vb = 777.0
        mock_settings.maker.session_timeout_sec = 600
        mock_settings.maker.pre_sign_timeout_sec = 120
        mock_settings.maker.identity_renewal_min_sec = 61
        mock_settings.maker.identity_renewal_max_sec = 121
        mock_settings.maker.identity_grace_sec = 90
        mock_settings.maker.identity_rotation_quiet_min_sec = 17
        mock_settings.maker.identity_rotation_quiet_max_sec = 29
        mock_settings.maker.pending_tx_timeout_min = 15
        mock_settings.maker.pending_tx_abandon_hours = 96
        mock_settings.maker.rescan_interval_sec = 780
        mock_settings.maker.onion_host = "rest-maker.example.onion"
        mock_settings.maker.onion_serving_host = "0.0.0.0"
        mock_settings.maker.onion_serving_port = 5223
        mock_settings.maker.message_rate_limit = 11
        mock_settings.maker.message_burst_limit = 111
        mock_settings.maker.offer_reannounce_delay_max = 432
        mock_settings.maker.directory_reconnect_interval = 75
        mock_settings.maker.directory_reconnect_max_retries = 8
        mock_settings.maker.directory_startup_timeout = 180
        mock_settings.maker.orderbook_rate_limit = 2
        mock_settings.maker.orderbook_rate_interval = 15.0
        mock_settings.maker.orderbook_violation_ban_threshold = 101
        mock_settings.maker.orderbook_violation_warning_threshold = 11
        mock_settings.maker.orderbook_violation_severe_threshold = 51
        mock_settings.maker.orderbook_ban_duration = 7200.0
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

        maker_config = mock_maker_cls.call_args.kwargs["config"]
        assert isinstance(maker_config, MakerConfig)
        assert maker_config.mnemonic.get_secret_value() == state.wallet_mnemonic
        assert maker_config.network == NetworkType.SIGNET
        assert maker_config.bitcoin_network == NetworkType.REGTEST
        assert maker_config.data_dir == state.data_dir
        assert maker_config.directory_servers == expected_dirs
        assert maker_config.allow_clearnet_connections is allow_clearnet_connections
        assert maker_config.nick_auth_mode == "require_verified"
        assert maker_config.nick_auth_directory_ids == expected_ids
        assert maker_config.socks_host == "127.0.0.1"
        assert maker_config.socks_port == 9050
        assert maker_config.stream_isolation is False
        assert maker_config.connection_timeout == 45.0
        assert maker_config.tor_control.host == "127.0.0.2"
        assert maker_config.tor_control.port == 9052
        assert maker_config.tor_control.cookie_path == Path("/tmp/walletd-control.cookie")
        assert maker_config.tor_target_host == "maker-service"
        assert maker_config.onion_host == "rest-maker.example.onion"
        assert maker_config.onion_serving_host == "0.0.0.0"
        assert maker_config.onion_serving_port == 5223
        assert maker_config.offer_type is OfferType.SW0_RELATIVE
        assert maker_config.min_size == 100_000
        assert maker_config.cj_fee_relative == "0.002"
        assert maker_config.cj_fee_absolute == 500
        assert maker_config.tx_fee_contribution == 1000
        assert maker_config.offer_configs == []
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

        async def rotate_then_stop() -> None:
            callback = mock_maker_cls.call_args.kwargs["nick_change_callback"]
            callback("J5TestNickWalletd", "J5RotatedNickWalletd")
            assert state.nickname == "J5RotatedNickWalletd"

        mock_maker.start.side_effect = rotate_then_stop
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

        assert mock_write_nick.call_args_list == [
            call(state.data_dir, "maker", "J5TestNickWalletd"),
            call(state.data_dir, "maker", "J5RotatedNickWalletd"),
        ]
        mock_remove_nick.assert_called_once_with(state.data_dir, "maker")

    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("maker.bot.MakerBot")
    @patch("maker.config.MakerConfig")
    def test_failed_maker_is_stopped_before_state_is_cleared(
        self,
        mock_config: Mock,
        mock_maker_cls: Mock,
        mock_backend: AsyncMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client
        state = get_daemon_state()
        mock_maker = AsyncMock()
        mock_maker.nick = "J5ExpiredMaker"
        mock_maker.start.side_effect = RuntimeError("certificate expired")
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

        time.sleep(0.1)

        mock_maker.stop.assert_awaited_once()
        mock_backend.return_value.close.assert_awaited_once()
        assert state._maker_ref is None
        assert state.coinjoin_state == CoinjoinState.NOT_RUNNING


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


class TestTakerStatus:
    """GET /taker/status (issue #627): status/txid/error, live or snapshotted."""

    def test_no_run_yet(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/taker/status",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"running": False, "status": None, "txid": None, "error": None}

    def test_reads_live_off_a_running_taker(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.activate_coinjoin_state(CoinjoinState.TAKER_RUNNING)

        mock_taker = Mock()
        mock_taker.state = TakerState.BROADCASTING
        mock_taker.txid = ""
        mock_taker.last_failure_reason = None
        state._taker_ref = mock_taker
        # A stale snapshot from a previous run must not leak through while
        # a new one is live.
        state.last_taker_status = "failed"
        state.last_taker_txid = None
        state.last_taker_error = "previous run: no counterparties found"

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/taker/status",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "running": True,
            "status": "broadcasting",
            "txid": None,
            "error": None,
        }

    def test_reports_completed_snapshot_after_teardown(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state._taker_ref = None
        state.taker_running = False
        state.last_taker_status = "complete"
        state.last_taker_txid = "a" * 64
        state.last_taker_error = None

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/taker/status",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "running": False,
            "status": "complete",
            "txid": "a" * 64,
            "error": None,
        }

    def test_reports_failed_snapshot_with_error_after_teardown(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state._taker_ref = None
        state.taker_running = False
        state.last_taker_status = "failed"
        state.last_taker_txid = None
        state.last_taker_error = "No suitable counterparties found."

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/taker/status",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "running": False,
            "status": "failed",
            "txid": None,
            "error": "No suitable counterparties found.",
        }
