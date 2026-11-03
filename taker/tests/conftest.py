"""
Test configuration for taker tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _taker_test_helpers import SAMPLE_MNEMONIC

from taker.podle_manager import initialize_commitment_history


@pytest.fixture(autouse=True)
def isolated_default_commitment_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Successful taker fixtures must not depend on the operator's real history."""
    data_dir = tmp_path / "default-taker-data"
    monkeypatch.setattr("jmcore.paths.get_default_data_dir", lambda: data_dir)
    initialize_commitment_history(data_dir)


@pytest.fixture
def sample_mnemonic() -> str:
    """Test mnemonic (not for production use!)."""
    return SAMPLE_MNEMONIC
