"""Opening an existing ledger must never create a replacement database."""

from __future__ import annotations

import sqlite3
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


def test_existing_only_open_preserves_standalone_schema(tmp_path: Path) -> None:
    path = tmp_path / "seller.sqlite"
    MarketStore(path).close()
    before = path.read_bytes()
    with MarketStore(path, create=False) as store:
        assert not store.is_wallet_ledger
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "tamper",
    (
        "UPDATE metadata SET value = '2' WHERE key = 'schema_version'",
        "DELETE FROM metadata WHERE key = 'ledger_mode'",
        "UPDATE metadata SET value = 'wallet-ish' WHERE key = 'ledger_mode'",
        "DROP TABLE podle_ownership",
    ),
)
def test_unsupported_store_format_fails_closed_without_deleting_it(
    tmp_path: Path, tamper: str
) -> None:
    """An unrecognized on-disk format is never migrated, reset, or removed."""
    path = tmp_path / "seller.sqlite"
    MarketStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(tamper)
    before = path.read_bytes()

    with pytest.raises(MarketStoreCorruptError, match="unsupported market store format"):
        MarketStore(path)

    assert path.read_bytes() == before
