"""Opening an existing ledger must never create a replacement database."""

from __future__ import annotations

from pathlib import Path

import pytest

from jmcore.market_store import MarketStore, MarketStoreCorruptError, MarketStoreUnavailableError


def test_existing_only_open_preserves_missing_state(tmp_path: Path) -> None:
    path = tmp_path / "missing-parent" / "seller.sqlite"
    with pytest.raises(MarketStoreUnavailableError, match="does not exist"):
        MarketStore(path, create=False)
    assert not path.parent.exists()


def test_existing_only_sqlite_open_cannot_create_after_preflight_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "seller.sqlite"
    monkeypatch.setattr(Path, "is_file", lambda _path: True)
    with pytest.raises(MarketStoreCorruptError, match="could not open"):
        MarketStore(path, create=False)
    assert not path.exists()


def test_existing_only_open_preserves_legacy_schema(tmp_path: Path) -> None:
    path = tmp_path / "seller.sqlite"
    MarketStore(path).close()
    before = path.read_bytes()
    with MarketStore(path, create=False) as store:
        assert not store.is_wallet_ledger
    assert path.read_bytes() == before
