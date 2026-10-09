"""Opening an existing ledger must never create a replacement database."""

from __future__ import annotations

import sqlite3
import stat
import time
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
        "UPDATE metadata SET value = '3' WHERE key = 'schema_version'",
        "UPDATE metadata SET value = '1' WHERE key = 'schema_version'",
        "DROP TABLE hold_invoices",
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


def _legacy_v1_store(path: Path, *, quote_expires_at: int) -> None:
    """Write the shape a previous release left: queued payments and a manual quote."""
    MarketStore(path).close()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE hold_invoices")
        connection.execute("UPDATE metadata SET value = '1' WHERE key = 'schema_version'")
        connection.execute(
            """
            INSERT INTO inventory (id, product, resource, state, reservation_quote_id)
            VALUES (1, 'bond', ?, 'reserved', 'legacy-quote')
            """,
            ("aa" * 32,),
        )
        connection.executemany(
            "INSERT INTO payments VALUES (?, 'lightning', ?, x'00', ?, ?)",
            [
                ("payment-1", "lnbcrt1reserved", "reserved", "legacy-quote"),
                ("payment-2", "lnbcrt1queued", "available", None),
            ],
        )
        connection.execute(
            """
            INSERT INTO quotes (
                quote_id, request_id, request_fingerprint, quote_document, buyer_pubkey,
                product, resource, inventory_id, payment_id, created_at, expires_at, state
            ) VALUES ('legacy-quote', 'r1', x'00', x'00', ?, 'bond', ?, 1, 'payment-1', 1, ?,
                      'live')
            """,
            ("bb" * 32, "aa" * 32, quote_expires_at),
        )


def _rows(path: Path, query: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(query).fetchall()


def test_v1_store_with_a_payable_quote_is_not_migrated(tmp_path: Path) -> None:
    path = tmp_path / "seller.sqlite"
    _legacy_v1_store(path, quote_expires_at=int(time.time()) + 600)
    before = path.read_bytes()
    with pytest.raises(MarketStoreUnavailableError, match="upgrade waits for 1 live quote"):
        MarketStore(path, create=False)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.v1-backup*"))


def test_v1_store_migrates_after_snapshot_with_v1_expiry(tmp_path: Path) -> None:
    path = tmp_path / "seller.sqlite"
    _legacy_v1_store(path, quote_expires_at=1)
    before = path.read_bytes()

    MarketStore(path, create=False).close()

    backup = tmp_path / "seller.sqlite.v1-backup"
    restored = tmp_path / "restored.sqlite"
    restored.write_bytes(before)
    for query in (
        "SELECT * FROM metadata ORDER BY key",
        "SELECT * FROM quotes",
        "SELECT * FROM inventory",
        "SELECT * FROM payments ORDER BY payment_id",
    ):
        assert _rows(backup, query) == _rows(restored, query)
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert _rows(path, "SELECT value FROM metadata WHERE key = 'schema_version'") == [("2",)]
    assert _rows(path, "SELECT state FROM quotes") == [("expired",)]
    assert _rows(path, "SELECT state, reservation_quote_id FROM inventory") == [("available", None)]
    # Queued manual payment requests are never selected again.
    assert _rows(path, "SELECT payment_id, state FROM payments ORDER BY payment_id") == [
        ("payment-1", "reserved"),
        ("payment-2", "available"),
    ]
    assert _rows(path, "SELECT COUNT(*) FROM hold_invoices") == [(0,)]

    MarketStore(path, create=False).close()
    assert sorted(item.name for item in tmp_path.glob("*.v1-backup*")) == [backup.name]


def test_v1_migration_never_overwrites_an_earlier_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "seller.sqlite"
    _legacy_v1_store(path, quote_expires_at=1)
    earlier = tmp_path / "seller.sqlite.v1-backup"
    earlier.write_bytes(b"earlier snapshot")
    MarketStore(path, create=False).close()
    assert earlier.read_bytes() == b"earlier snapshot"
    assert (tmp_path / "seller.sqlite.v1-backup.2").is_file()
