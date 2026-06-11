"""Tests for jmwalletd.routers.wallet_data — wallet data query endpoints."""

from __future__ import annotations

from pathlib import Path
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
        ws.sync_with_registered_bonds.assert_awaited_once()

    def test_skips_sync_while_rescanning(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        state.rescanning = True
        ws.sync_with_registered_bonds.reset_mock()
        ws.mixdepth_count = 5
        ws.get_balance = AsyncMock(return_value=100_000_000)
        ws.get_available_balance = AsyncMock(return_value=90_000_000)
        ws.get_address_info_for_mixdepth = Mock(return_value=[])

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/display",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        ws.sync_with_registered_bonds.assert_not_awaited()


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

        # Verify that history helpers were called with the data dir scoped to
        # the active wallet (issue #473: wallet_fingerprint isolation).
        mock_get_used.assert_called_once()
        used_args, used_kwargs = mock_get_used.call_args
        assert used_args == (state.data_dir,)
        assert "wallet_fingerprint" in used_kwargs

        mock_get_history.assert_called_once()
        hist_args, hist_kwargs = mock_get_history.call_args
        assert hist_args == (state.data_dir,)
        assert "wallet_fingerprint" in hist_kwargs

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
        ws.sync_with_registered_bonds.assert_awaited_once()

    def test_utxos_skip_sync_while_rescanning(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service

        state.rescanning = True
        ws.sync_with_registered_bonds.reset_mock()

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/utxos",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        ws.sync_with_registered_bonds.assert_not_awaited()


class TestListUtxosFidelityBonds:
    """A funded fidelity bond must be returned by /utxos with a ``locktime``.

    JAM keys fidelity-bond detection off a truthy ``locktime`` field and parses
    the actual timestamp out of the ``path`` (``.../2/<index>:<locktime>``). If
    the bond is missing from the response (or the field is absent), the coins
    "disappear" from JAM. This guards both the presence of the bond UTXO and
    the legacy-compatible datetime formatting of the field.
    """

    def test_bond_utxo_surfaces_locktime(self, authed_client: tuple[TestClient, str]) -> None:
        from jmwallet.wallet.models import UTXOInfo

        client, token = authed_client
        state = get_daemon_state()
        ws = state.wallet_service
        ws.mixdepth_count = 5

        # locktime 1748736000 == 2025-06-01 00:00:00 UTC (a 1st-of-month bond).
        bond_locktime = 1748736000
        bond = UTXOInfo(
            txid="aa" * 32,
            vout=0,
            value=200_000_000,
            address="bcrt1qbond",
            confirmations=10,
            scriptpubkey="0020" + "11" * 32,
            path=f"m/84'/1'/0'/2/0:{bond_locktime}",
            mixdepth=0,
            locktime=bond_locktime,
        )
        regular = UTXOInfo(
            txid="bb" * 32,
            vout=1,
            value=50_000_000,
            address="bcrt1qregular",
            confirmations=5,
            scriptpubkey="0014" + "22" * 20,
            path="m/84'/1'/1'/0/3",
            mixdepth=1,
        )
        ws.utxo_cache = {0: [bond], 1: [regular], 2: [], 3: [], 4: []}

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/utxos",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        utxos = {u["utxo"]: u for u in resp.json()["utxos"]}

        bond_entry = utxos[f"{'aa' * 32}:0"]
        # Legacy joinmarket-clientserver UTC datetime string format.
        assert bond_entry["locktime"] == "2025-06-01 00:00:00"
        # The path carries the parseable unix timestamp JAM extracts.
        assert bond_entry["path"].endswith(f":{bond_locktime}")
        assert bond_entry["mixdepth"] == 0

        # Regular UTXOs must not carry a locktime (field omitted / null).
        regular_entry = utxos[f"{'bb' * 32}:1"]
        assert regular_entry["locktime"] is None


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
        ws.get_new_address_verified = AsyncMock(return_value="bcrt1qnewaddr123")

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
        ws.get_new_address_verified = AsyncMock(side_effect=["bcrt1qaddr1", "bcrt1qaddr2"])

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
        assert ws.get_new_address_verified.call_count == 2

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
        # require_wallet_match intercepts before business logic: 404, not 400
        assert resp.status_code == 404


class TestGetSeed:
    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/wallet/test_wallet.jmdat/getseed")
        assert resp.status_code == 401

    def test_returns_seed(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.wallet_mnemonic = "abandon " * 11 + "about"

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/getseed",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["seedphrase"] == "abandon " * 11 + "about"

    def test_errors_when_no_mnemonic_set(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        state = get_daemon_state()
        state.wallet_mnemonic = ""

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/getseed",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 400
        assert "Seed phrase is not available" in resp.json()["message"]


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
        # Check that it was called with the correct timenumber for 2026-06
        ws.get_fidelity_bond_address.assert_called_once()
        args = ws.get_fidelity_bond_address.call_args
        # 2026-06 -> timenumber 77 (months since Jan 2020)
        assert args[0][0] == 77  # timenumber is first arg
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
    def _append_maker_entry(
        self,
        data_dir: Path,
        *,
        cj_amount: int,
        fee_received: int,
        txfee_contribution: int,
        success: bool = True,
        txid: str = "ab" * 32,
    ) -> None:
        """Write a maker row into the daemon data dir's history.csv."""
        from jmwallet.history import (
            TransactionHistoryEntry,
            append_history_entry,
        )

        entry = TransactionHistoryEntry(
            timestamp="2024-01-01T10:00:00",
            completed_at="2024-01-01T10:05:00",
            confirmed_at="2024-01-01T10:05:00",
            role="maker",
            success=success,
            confirmations=1 if success else 0,
            txid=txid,
            cj_amount=cj_amount,
            counterparty_nicks="J5xtaker",
            fee_received=fee_received,
            txfee_contribution=txfee_contribution,
            net_fee=fee_received - txfee_contribution,
            utxos_used=f"{txid}:0,{txid}:1",
            network="regtest",
            wallet_fingerprint="deadbeef",
        )
        append_history_entry(entry, data_dir=data_dir)

    def test_empty_report_returns_header_and_marker(
        self, authed_client: tuple[TestClient, str]
    ) -> None:
        """With no maker history the report is still returned (header + marker)."""
        client, token = authed_client
        resp = client.get(
            "/api/v1/wallet/yieldgen/report",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        rows = resp.json()["yigen_data"]
        # Header row + a single "Connected" startup marker, no earnings rows.
        assert rows[0].startswith("timestamp,cj amount/satoshi,")
        assert any("Connected" in r for r in rows)
        assert len(rows) == 2

    def test_report_synthesized_from_maker_history(
        self, authed_client: tuple[TestClient, str], data_dir: Path
    ) -> None:
        """A successful maker CoinJoin appears as a reference-format earnings row."""
        client, token = authed_client
        self._append_maker_entry(
            data_dir, cj_amount=100_000, fee_received=2_680, txfee_contribution=200
        )

        resp = client.get(
            "/api/v1/wallet/yieldgen/report",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        rows = resp.json()["yigen_data"]
        # header + Connected marker + one earnings row
        assert len(rows) == 3
        earning = rows[-1].split(",")
        # cj amount, input count, input value, cjfee, earned
        assert earning[1] == "100000"
        assert earning[2] == "2"  # two utxos_used
        assert earning[4] == "2680"  # cjfee = fee_received
        assert earning[5] == str(2_680 - 200)  # earned = net_fee

    def test_pending_maker_entry_excluded(
        self, authed_client: tuple[TestClient, str], data_dir: Path
    ) -> None:
        """Unconfirmed/failed maker rows are not reported as earnings."""
        client, token = authed_client
        self._append_maker_entry(
            data_dir,
            cj_amount=50_000,
            fee_received=0,
            txfee_contribution=0,
            success=False,
            txid="cd" * 32,
        )

        resp = client.get(
            "/api/v1/wallet/yieldgen/report",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        rows = resp.json()["yigen_data"]
        # Only header + Connected marker (the pending row is excluded).
        assert len(rows) == 2

    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        # Without a bearer token the endpoint must reject the request to avoid
        # leaking yield-gen earnings to unauthenticated callers.
        client, _ = authed_client
        resp = client.get("/api/v1/wallet/yieldgen/report")
        assert resp.status_code == 401

    def test_rejects_invalid_token(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get(
            "/api/v1/wallet/yieldgen/report",
            headers=_auth_headers("not-a-real-token"),
        )
        assert resp.status_code == 401


class TestWalletHistory:
    """GET /wallet/{walletname}/history returns the wallet's history.csv rows."""

    def _append(
        self,
        data_dir: Path,
        *,
        role: str = "maker",
        cj_amount: int = 100_000,
        fee_received: int = 0,
        fingerprint: str = "deadbeef",
        txid: str = "ab" * 32,
        timestamp: str = "2024-01-01T10:00:00",
    ) -> None:
        from jmwallet.history import TransactionHistoryEntry, append_history_entry

        append_history_entry(
            TransactionHistoryEntry(
                timestamp=timestamp,
                role=role,  # type: ignore[arg-type]
                success=True,
                confirmations=1,
                txid=txid,
                cj_amount=cj_amount,
                counterparty_nicks="J5peer",
                fee_received=fee_received,
                network="regtest",
                wallet_fingerprint=fingerprint,
            ),
            data_dir=data_dir,
        )

    def test_empty_history(self, authed_client: tuple[TestClient, str]) -> None:
        client, token = authed_client
        # Active wallet fingerprint must be a concrete value for read_history.
        get_daemon_state().wallet_service.wallet_fingerprint = "deadbeef"
        resp = client.get("/api/v1/wallet/test_wallet.jmdat/history", headers=_auth_headers(token))
        assert resp.status_code == 200
        assert resp.json()["history"] == []

    def test_returns_wallet_history_entries(
        self, authed_client: tuple[TestClient, str], data_dir: Path
    ) -> None:
        client, token = authed_client
        get_daemon_state().wallet_service.wallet_fingerprint = "deadbeef"
        self._append(data_dir, role="maker", cj_amount=100_000, fee_received=2_500)

        resp = client.get("/api/v1/wallet/test_wallet.jmdat/history", headers=_auth_headers(token))
        assert resp.status_code == 200
        history = resp.json()["history"]
        assert len(history) == 1
        assert history[0]["role"] == "maker"
        assert history[0]["cj_amount"] == 100_000
        assert history[0]["fee_received"] == 2_500
        assert history[0]["txid"] == "ab" * 32

    def test_scoped_to_active_wallet_fingerprint(
        self, authed_client: tuple[TestClient, str], data_dir: Path
    ) -> None:
        client, token = authed_client
        get_daemon_state().wallet_service.wallet_fingerprint = "deadbeef"
        # One entry for the active wallet, one for a different wallet.
        self._append(data_dir, fingerprint="deadbeef", txid="aa" * 32)
        self._append(data_dir, fingerprint="cafebabe", txid="bb" * 32)

        resp = client.get("/api/v1/wallet/test_wallet.jmdat/history", headers=_auth_headers(token))
        assert resp.status_code == 200
        history = resp.json()["history"]
        assert len(history) == 1
        assert history[0]["txid"] == "aa" * 32

    def test_limit_param(self, authed_client: tuple[TestClient, str], data_dir: Path) -> None:
        client, token = authed_client
        get_daemon_state().wallet_service.wallet_fingerprint = "deadbeef"
        self._append(data_dir, txid="11" * 32, timestamp="2024-01-01T10:00:00")
        self._append(data_dir, txid="22" * 32, timestamp="2024-02-01T10:00:00")

        resp = client.get(
            "/api/v1/wallet/test_wallet.jmdat/history?limit=1",
            headers=_auth_headers(token),
        )
        assert resp.status_code == 200
        history = resp.json()["history"]
        assert len(history) == 1
        # Most-recent first -> the February row.
        assert history[0]["txid"] == "22" * 32

    def test_requires_auth(self, authed_client: tuple[TestClient, str]) -> None:
        client, _ = authed_client
        resp = client.get("/api/v1/wallet/test_wallet.jmdat/history")
        assert resp.status_code == 401

    def test_rejects_wrong_wallet_name(self, authed_client: tuple[TestClient, str]) -> None:
        # IDOR guard: the path walletname must match the loaded wallet.
        client, token = authed_client
        resp = client.get("/api/v1/wallet/other_wallet.jmdat/history", headers=_auth_headers(token))
        assert resp.status_code in (401, 404)
