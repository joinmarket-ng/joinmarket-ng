"""Market path discovery does not initialize market persistence."""

from __future__ import annotations

from pathlib import Path

from jmcore.paths import get_market_store_path


def test_market_store_path_is_passive(tmp_path: Path) -> None:
    assert get_market_store_path(tmp_path) == tmp_path / "market" / "seller.sqlite"
    assert not (tmp_path / "market").exists()
