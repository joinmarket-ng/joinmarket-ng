"""Shared fixtures for jmwalletd tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastapi.testclient import TestClient

from jmwalletd.app import create_app
from jmwalletd.auth import JMTokenAuthority
from jmwalletd.deps import set_daemon_state
from jmwalletd.state import DaemonState


@pytest.fixture
def token_authority() -> JMTokenAuthority:
    """Fresh JMTokenAuthority for testing."""
    return JMTokenAuthority()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """Temporary data directory with wallets subdirectory."""
    wallets_dir = tmp_path / "wallets"
    wallets_dir.mkdir()
    return tmp_path


@pytest.fixture
def daemon_state(data_dir: Path) -> DaemonState:
    """DaemonState with tmp data_dir."""
    state = DaemonState(data_dir=data_dir)
    set_daemon_state(state)
    return state


@pytest.fixture
def mock_wallet_service() -> MagicMock:
    """Mock WalletService with commonly used methods."""
    ws = MagicMock()
    ws.mnemonic = "abandon " * 11 + "about"
    ws.get_total_balance = Mock(return_value=100_000_000)
    ws.get_balance_for_mixdepth = Mock(return_value=50_000_000)
    ws.get_available_balance_for_mixdepth = Mock(return_value=50_000_000)
    ws.get_receive_address = Mock(return_value="bcrt1qtest1234567890abcdef")
    ws.sync = AsyncMock()
    ws.backend = MagicMock()
    ws.backend.get_block_count = AsyncMock(return_value=800_000)
    ws.num_mixdepths = 5

    # UTXO cache: dict of mixdepth -> dict of utxo_str -> UTXOInfo-like
    ws.utxo_cache = {}
    for md in range(5):
        ws.utxo_cache[md] = {}

    # Address info
    ws.get_address_info_for_mixdepth = Mock(return_value=[])

    return ws


@pytest.fixture
def daemon_state_with_wallet(
    daemon_state: DaemonState, mock_wallet_service: MagicMock
) -> DaemonState:
    """DaemonState with a loaded wallet."""
    daemon_state.wallet_service = mock_wallet_service
    daemon_state.wallet_mnemonic = "abandon " * 11 + "about"
    daemon_state.wallet_name = "test_wallet.jmdat"
    daemon_state.token_authority.issue("test_wallet.jmdat")
    return daemon_state


@pytest.fixture
def auth_token(daemon_state_with_wallet: DaemonState) -> str:
    """Valid JWT access token for the loaded wallet."""
    pair = daemon_state_with_wallet.token_authority.issue("test_wallet.jmdat")
    return pair.token


@pytest.fixture
def app(daemon_state: DaemonState) -> TestClient:
    """FastAPI TestClient with no wallet loaded."""
    application = create_app(data_dir=daemon_state.data_dir)
    # Override the state created by create_app with our test state
    set_daemon_state(daemon_state)
    return TestClient(application)


@pytest.fixture
def app_with_wallet(
    daemon_state_with_wallet: DaemonState,
) -> TestClient:
    """FastAPI TestClient with a loaded wallet."""
    application = create_app(data_dir=daemon_state_with_wallet.data_dir)
    set_daemon_state(daemon_state_with_wallet)
    return TestClient(application)


@pytest.fixture
def app_with_jam_assets(tmp_path: Path, daemon_state: DaemonState) -> TestClient:
    """TestClient with a temporary JAM static directory configured."""
    jam_dir = tmp_path / "jam"
    assets_dir = jam_dir / "assets"
    assets_dir.mkdir(parents=True)
    (jam_dir / "index.html").write_text("<html>jam</html>", encoding="utf-8")
    (assets_dir / "app.js").write_text("console.log('jam')", encoding="utf-8")

    daemon_state.data_dir = tmp_path
    application = create_app(data_dir=tmp_path)
    set_daemon_state(daemon_state)
    return TestClient(application)
