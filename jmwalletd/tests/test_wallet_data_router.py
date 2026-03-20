"""Tests for jmwalletd.routers.wallet_data — wallet data query endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi.testclient import TestClient

from jmwalletd.app import create_app
from jmwalletd.deps import get_daemon_state, set_daemon_state
from jmwalletd.state import DaemonState


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


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TestWalletDisplay:
    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/wallet/test_wallet.jmdat/display")
        assert resp.status_code == 401

    def test_returns_display(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        # Mock the display-related methods
        ws.mixdepth_count = 5
        ws.get_balance = AsyncMock(return_value=100_000_000)
        ws.get_available_balance = AsyncMock(return_value=90_000_000)
        ws.get_address_info_for_mixdepth = Mock(return_value=[])

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/display",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["walletname"] == "test_wallet.jmdat"
        assert "walletinfo" in data
        ws.sync.assert_awaited_once()

    def test_skips_sync_while_rescanning(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        state.rescanning = True
        ws.sync.reset_mock()
        ws.mixdepth_count = 5
        ws.get_balance = AsyncMock(return_value=100_000_000)
        ws.get_available_balance = AsyncMock(return_value=90_000_000)
        ws.get_address_info_for_mixdepth = Mock(return_value=[])

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/display",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        ws.sync.assert_not_awaited()


class TestWalletDisplayWithHistory:
    """Verify that the display endpoint passes history data for address classification."""

    @patch("jmwalletd.routers.wallet_data.get_address_history_types")
    @patch("jmwalletd.routers.wallet_data.get_used_addresses")
    def test_passes_history_data_to_address_info(
        self,
        mock_get_used: MagicMock,
        mock_get_history: MagicMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """The display endpoint should pass used_addresses and history_addresses."""
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        ws.mixdepth_count = 5
        ws.get_balance = AsyncMock(return_value=100_000_000)
        ws.get_available_balance = AsyncMock(return_value=90_000_000)
        ws.get_address_info_for_mixdepth = Mock(return_value=[])

        used = {"addr1", "addr2"}
        history = {"addr1": "cj_out", "addr2": "change"}
        mock_get_used.return_value = used
        mock_get_history.return_value = history

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/display",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200

        # Verify that history helpers were called with the data dir.
        mock_get_used.assert_called_once_with(state.data_dir)
        mock_get_history.assert_called_once_with(state.data_dir)

        # Verify get_address_info_for_mixdepth was called with history data.
        # It's called once for each (mixdepth, change) pair: 5 * 2 = 10 calls.
        assert ws.get_address_info_for_mixdepth.call_count == 10
        for call in ws.get_address_info_for_mixdepth.call_args_list:
            _, kwargs = call
            assert kwargs.get("used_addresses") == used
            assert kwargs.get("history_addresses") == history

    @patch("jmwalletd.routers.wallet_data.get_address_history_types")
    @patch("jmwalletd.routers.wallet_data.get_used_addresses")
    def test_empty_history_still_works(
        self,
        mock_get_used: MagicMock,
        mock_get_history: MagicMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """With no history, the display endpoint should still work."""
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        ws.mixdepth_count = 5
        ws.get_balance = AsyncMock(return_value=0)
        ws.get_available_balance = AsyncMock(return_value=0)
        ws.get_address_info_for_mixdepth = Mock(return_value=[])

        mock_get_used.return_value = set()
        mock_get_history.return_value = {}

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/display",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["walletname"] == "test_wallet.jmdat"

    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/wallet/test_wallet.jmdat/utxos")
        assert resp.status_code == 401

    def test_empty_utxos(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service
        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/utxos",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["utxos"] == []
        ws.sync.assert_awaited_once()

    def test_utxos_skip_sync_while_rescanning(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        state.rescanning = True
        ws.sync.reset_mock()

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/utxos",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        ws.sync.assert_not_awaited()


class TestNewAddress:
    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/wallet/test_wallet.jmdat/address/new/0")
        assert resp.status_code == 401

    def test_get_new_address(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service
        ws.mixdepth_count = 5
        ws.get_new_address = Mock(return_value="bcrt1qnewaddr123")

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/address/new/0",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["address"] == "bcrt1qnewaddr123"

    def test_get_new_address_calls_wallet_service_each_time(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service
        ws.mixdepth_count = 5
        ws.get_new_address = Mock(side_effect=["bcrt1qaddr1", "bcrt1qaddr2"])

        resp1 = client.get(
            "/api/v1/wallet/test_wallet.jmdat/address/new/0",
            headers=_auth_headers(token),
        )
        resp2 = client.get(
            "/api/v1/wallet/test_wallet.jmdat/address/new/0",
            headers=_auth_headers(token),
        )

        assert resp1.status_code == 200
        assert resp2.status_code == 200
        assert resp1.json()["address"] == "bcrt1qaddr1"
        assert resp2.json()["address"] == "bcrt1qaddr2"
        assert ws.get_new_address.call_count == 2

    def test_invalid_mixdepth(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.wallet_service.mixdepth_count = 5

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/address/new/99",
            headers=_auth_headers(token),
        )
        # Should return 400 for invalid mixdepth
        assert resp.status_code in (400, 422)

    def test_walletname_must_match_unlocked_wallet(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.wallet_service.mixdepth_count = 5

        resp = client.get(
            "/api/v1/wallet/other_wallet.jmdat/address/new/0",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 400


class TestGetSeed:
    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/wallet/test_wallet.jmdat/getseed")
        assert resp.status_code == 401

    def test_returns_seed(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.wallet_service.mnemonic = "abandon " * 11 + "about"

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/getseed",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["seedphrase"] == "abandon " * 11 + "about"


class TestFreeze:
    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/freeze",
            json={"utxo-string": "abc:0", "freeze": True},
        )
        assert resp.status_code == 401

    def test_freeze_utxo(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service
        ws.freeze_utxo = Mock()

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/freeze",
            json={"utxo-string": "abc123:0", "freeze": True},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        ws.freeze_utxo.assert_called_once_with("abc123:0")

    def test_unfreeze_utxo(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service
        ws.unfreeze_utxo = Mock()

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/freeze",
            json={"utxo-string": "abc123:0", "freeze": False},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        ws.unfreeze_utxo.assert_called_once_with("abc123:0")


class TestConfigGet:
    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/configget",
            json={"section": "POLICY", "field": "tx_fees"},
        )
        assert resp.status_code == 401

    @patch("jmcore.settings.get_settings")
    def test_get_policy_tx_fees(
        self,
        mock_get_settings: MagicMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """tx_fees maps to wallet.default_fee_block_target via _POLICY_FIELD_MAP."""
        client, token = authed_client
        mock_wallet = MagicMock()
        mock_wallet.default_fee_block_target = 3
        mock_settings = MagicMock()
        mock_settings.wallet = mock_wallet
        mock_get_settings.return_value = mock_settings

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/configget",
            json={"section": "POLICY", "field": "tx_fees"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["configvalue"] == "3"

    @patch("jmcore.settings.get_settings")
    def test_get_policy_max_cj_fee_abs(
        self,
        mock_get_settings: MagicMock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """max_cj_fee_abs maps to taker.max_cj_fee_abs via _POLICY_FIELD_MAP."""
        client, token = authed_client
        mock_taker = MagicMock()
        mock_taker.max_cj_fee_abs = 500
        mock_settings = MagicMock()
        mock_settings.taker = mock_taker
        mock_get_settings.return_value = mock_settings

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/configget",
            json={"section": "POLICY", "field": "max_cj_fee_abs"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["configvalue"] == "500"

    def test_get_policy_max_sweep_fee_change(
        self,
        authed_client: tuple[TestClient, str],
    ) -> None:
        """max_sweep_fee_change returns hardcoded default from _POLICY_DEFAULTS."""
        client, token = authed_client
        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/configget",
            json={"section": "POLICY", "field": "max_sweep_fee_change"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["configvalue"] == "0.8"

    def test_get_from_overrides(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.config_overrides["POLICY"] = {"tx_fees": "5000"}

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/configget",
            json={"section": "POLICY", "field": "tx_fees"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["configvalue"] == "5000"


class TestConfigSet:
    def test_set_config(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/configset",
            json={"section": "POLICY", "field": "tx_fees", "value": "7000"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        state = get_daemon_state()
        assert state.config_overrides["POLICY"]["tx_fees"] == "7000"


class TestTimelockAddress:
    @patch("jmwalletd.routers.wallet_data.save_registry")
    @patch("jmwalletd.routers.wallet_data.load_registry")
    def test_get_timelock_address(
        self,
        mock_load_registry: Mock,
        mock_save_registry: Mock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service
        ws.get_fidelity_bond_address = Mock(return_value="bcrt1qfidelity123")
        mock_key = MagicMock()
        mock_key.get_public_key_bytes.return_value = bytes(33)
        ws.get_fidelity_bond_key = Mock(return_value=mock_key)
        ws.get_fidelity_bond_script = Mock(return_value=b"\x00" * 32)
        ws.network = "signet"
        ws.root_path = "m/84'/1'/0'"
        mock_registry = MagicMock()
        mock_registry.bonds = []
        mock_registry.get_bond_by_address.return_value = None
        mock_load_registry.return_value = mock_registry

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/address/timelock/new/2026-06",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        assert resp.json()["address"] == "bcrt1qfidelity123"
        # Check that it was called with index 0 (no existing bonds for this locktime)
        ws.get_fidelity_bond_address.assert_called_once()
        args = ws.get_fidelity_bond_address.call_args
        assert args[0][0] == 0  # index is first arg
        # Check that the bond was saved to the registry
        mock_save_registry.assert_called_once()
        mock_registry.add_bond.assert_called_once()

    def test_invalid_date_format(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/address/timelock/new/invalid-date",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 400


class TestSignMessage:
    @patch("jmcore.crypto.bitcoin_message_hash")
    @patch("coincurve.PrivateKey")
    def test_sign_message_success(
        self,
        mock_privkey_cls: Mock,
        mock_hash: Mock,
        authed_client: tuple[TestClient, str],
    ) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        # Setup mocks
        mock_hash.return_value = b"msg_hash"

        mock_pk_instance = Mock()
        # "raw_sig" base64 encoded is "cmF3X3NpZw=="
        mock_pk_instance.sign_recoverable.return_value = b"raw_sig"
        mock_privkey_cls.return_value = mock_pk_instance

        # Wallet service mocks
        mock_key = Mock()
        mock_key.private_key = b"privkeybytes"
        mock_key.address = "bcrt1qaddr123"
        ws.get_key_for_address.return_value = mock_key
        # mock get_address to return the address needed for lookup
        ws.get_address.return_value = "bcrt1qaddr123"

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/signmessage",
            json={"hd_path": "m/84'/0'/0'/0/5", "message": "hello"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["signature"] == "cmF3X3NpZw=="
        assert data["address"] == "bcrt1qaddr123"
        assert data["message"] == "hello"

    def test_sign_message_invalid_path(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/signmessage",
            json={"hd_path": "short/path", "message": "hello"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 400

    def test_sign_message_key_not_found(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service
        ws.get_address.return_value = "addr1"
        ws.get_key_for_address.return_value = None

        resp = client.post(
            "/api/v1/wallet/test_wallet.jmdat/signmessage",
            json={"hd_path": "m/84'/0'/0'/0/5", "message": "hello"},
            headers=_auth_headers(token),
        )
        assert resp.status_code == 400


class TestRescan:
    def test_rescan_success(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        # Ensure rescan_blockchain exists and is async
        ws.backend.rescan_blockchain = AsyncMock()

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/rescanblockchain/0",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200

    def test_rescan_not_supported(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        # Remove rescan_blockchain from backend mock
        ws.backend = Mock(spec=object)

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/rescanblockchain/0",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 400

    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/wallet/test_wallet.jmdat/rescanblockchain/0")
        assert resp.status_code == 401

    def test_rescan_info(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.rescanning = False
        state.rescan_progress = 0.0

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/getrescaninfo",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rescanning"] is False


class TestYieldGenReport:
    def test_no_report_file(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/wallet/yieldgen/report")
        # No report file -> 404 YieldGeneratorDataUnreadable
        assert resp.status_code == 404

    def test_with_report_file(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        state = get_daemon_state()
        report_file = state.data_dir / "yigen-statement.csv"
        report_file.write_text("timestamp,cjamount,fee\n2024-01-01,100000,250\n")

        resp = client.get("/api/v1/wallet/yieldgen/report")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["yigen_data"]) == 2  # header + data line
