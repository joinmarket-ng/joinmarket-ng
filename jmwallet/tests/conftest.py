"""
Pytest configuration and fixtures for jmwallet tests.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest
from _jmwallet_test_helpers import TEST_MNEMONIC

from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.service import WalletService

# ---------------------------------------------------------------------------
# Auto-isolation fixtures
# ---------------------------------------------------------------------------


# Environment variables that several CLI commands mutate via os.environ[...]= ...
# (e.g. ``setup_cli`` writing JOINMARKET_DATA_DIR so subsequent settings loads
# pick up the user-selected data directory). When such commands are exercised
# through ``CliRunner``, click's isolation only restores keys passed via its
# ``env`` argument, so any *new* keys the command writes leak into later tests.
#
# Snapshot and restore the relevant env vars between tests so that a CLI test
# pointing at a (now-deleted) tmpdir cannot poison ``get_default_data_dir()``
# in a later test that expects the default ``Path.home()``-derived location.
_ISOLATED_ENV_VARS = (
    "JOINMARKET_DATA_DIR",
    "MNEMONIC",
    "MNEMONIC_PASSWORD",
    "BIP39_PASSPHRASE",
)


@pytest.fixture(autouse=True)
def _isolate_joinmarket_env() -> Generator[None, None, None]:
    """Snapshot/restore env vars that CLI commands may mutate globally."""
    snapshot = {k: os.environ.get(k) for k in _ISOLATED_ENV_VARS}
    try:
        yield
    finally:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_mnemonic() -> str:
    """Test mnemonic (BIP39 test vector)."""
    return TEST_MNEMONIC


@pytest.fixture
def test_network() -> str:
    """Test network."""
    return "regtest"


@pytest.fixture
def temp_data_dir() -> Generator[Path, None, None]:
    """Create a temporary data directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_backend() -> DescriptorWalletBackend:
    """Create a DescriptorWalletBackend with _wallet_loaded=True."""
    backend = DescriptorWalletBackend(wallet_name="test_wallet")
    backend._wallet_loaded = True
    return backend


@pytest.fixture
def mock_backend_imported(mock_backend: DescriptorWalletBackend) -> DescriptorWalletBackend:
    """Create a DescriptorWalletBackend with both _wallet_loaded and _descriptors_imported."""
    mock_backend._descriptors_imported = True
    return mock_backend


@pytest.fixture
def wallet_service(
    test_mnemonic: str, mock_backend_imported: DescriptorWalletBackend
) -> WalletService:
    """Create a WalletService with default test config (mainnet, 5 mixdepths)."""
    return WalletService(
        mnemonic=test_mnemonic,
        backend=mock_backend_imported,
        network="mainnet",
        mixdepth_count=5,
    )
