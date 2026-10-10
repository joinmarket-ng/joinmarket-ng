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
        "DROP TABLE hold_invoices",
        "ALTER TABLE hold_invoices DROP COLUMN preimage",
        "ALTER TABLE payments DROP COLUMN terms",
        "ALTER TABLE quotes DROP COLUMN quote_document",
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


def _store_with_unbacked_quote(path: Path) -> None:
    """A quote without a hold record is not a valid interrupted current store."""
    MarketStore(path).close()
    with sqlite3.connect(path) as connection:
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
            ) VALUES ('legacy-quote', 'r1', x'00', x'00', ?, 'bond', ?, 1, 'payment-1', 1, 1,
                       'live')
            """,
            ("bb" * 32, "aa" * 32),
        )


def test_quote_without_hold_is_rejected_without_releasing_inventory(tmp_path: Path) -> None:
    path = tmp_path / "seller.sqlite"
    _store_with_unbacked_quote(path)
    before = path.read_bytes()
    with pytest.raises(MarketStoreCorruptError, match="unsupported market store format"):
        MarketStore(path, create=False)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.v1-backup*"))


@pytest.mark.parametrize("unrelated", [False, True])
def test_existing_empty_or_unrelated_sqlite_is_never_initialized(
    tmp_path: Path, unrelated: bool
) -> None:
    path = tmp_path / "seller.sqlite"
    with sqlite3.connect(path) as connection:
        if unrelated:
            connection.execute("CREATE TABLE unrelated (value TEXT)")
    before = path.read_bytes()
    with pytest.raises(MarketStoreCorruptError, match="unsupported market store format"):
        MarketStore(path)
    assert path.read_bytes() == before


def test_current_store_uses_structure_not_a_version_marker(tmp_path: Path) -> None:
    path = tmp_path / "seller.sqlite"
    MarketStore(path).close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT key FROM metadata").fetchall() == [("ledger_mode",)]
        connection.execute("INSERT INTO metadata VALUES ('schema_version', 'obsolete')")
    before = path.read_bytes()
    MarketStore(path, create=False).close()
    assert path.read_bytes() == before
