"""Wallet-scoped activation and global PoDLE ownership tests."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from jmcore.bitcoin import hash160
from jmcore.podle import generate_podle

from taker.market_store import (
    MarketStore,
    MarketStoreConflictError,
    MarketStoreCorruptError,
    MarketStoreError,
    MarketStoreUnavailableError,
)

WALLET_A = "a1" * 32
WALLET_B = "b2" * 32


def _podle_credential(value: int) -> dict[str, object]:
    proof = generate_podle(bytes([value]) * 32, f"{value:02x}" * 32 + ":1", index=0)
    return {
        "version": 1,
        "network": "regtest",
        "outpoint": {"txid": f"{value:02x}" * 32, "vout": 1},
        "P": proof.p.hex(),
        "P2": proof.p2.hex(),
        "sig": proof.sig.hex(),
        "e": proof.e.hex(),
        "commitment": proof.commitment.hex(),
        "index": 0,
        "scriptpubkey": (b"\x00\x14" + hash160(proof.p)).hex(),
        "blockheight": 101,
    }


def _write_history(
    path: Path, *, used: list[str] | None = None, external_v1: dict[str, object] | None = None
) -> None:
    path.write_text(
        json.dumps({"used": used or [], "external_v1": external_v1 or {}}, sort_keys=True),
        encoding="utf-8",
    )


def _activate(path: Path, commitments: Path, wallet_id: str = WALLET_A) -> MarketStore:
    store = MarketStore(path, wallet_id=wallet_id)
    store.activate_wallet(commitments, history_confirmed=True)
    return store


def test_v1_store_remains_unchanged_until_explicit_activation(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    store = MarketStore(path)

    assert not store.is_wallet_ledger
    assert store.wallet_state() == "disabled"
    credential = _podle_credential(0x41)
    store.add_inventory("podle", str(credential["commitment"]), credential)
    store.close()

    with sqlite3.connect(path) as connection:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert version == ("1",)
    assert "wallet_ledger" not in tables
    assert "podle_ownership" not in tables


def test_activation_requires_explicit_confirmation_and_exact_canonical_binding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    store = MarketStore(path, wallet_id=WALLET_A)

    with pytest.raises(MarketStoreError, match="history_confirmed"):
        store.activate_wallet(commitments)
    assert not store.is_wallet_ledger

    store.activate_wallet(commitments, history_confirmed=True)
    assert store.is_wallet_ledger
    assert store.wallet_state() == "ready"
    store.activate_wallet(commitments, history_confirmed=True)
    store.check_commitments_path(commitments)
    with pytest.raises(ValueError, match="parent traversal"):
        store.check_commitments_path(tmp_path / "nested" / ".." / "commitments.json")
    with pytest.raises(MarketStoreError, match="does not match"):
        store.check_commitments_path(tmp_path / "other.json")
    assert path.with_name(f"{path.name}.wallet-ledger").exists()
    store.close()


def test_wallet_readiness_is_specific_to_the_bound_wallet(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    first = _activate(path, commitments)
    second = MarketStore(path, wallet_id=WALLET_B)

    assert first.wallet_state() == "ready"
    assert second.wallet_state() == "disabled"
    credential = _podle_credential(0x42)
    with pytest.raises(MarketStoreUnavailableError, match="not ready"):
        second.add_inventory("podle", str(credential["commitment"]), credential)
    second.activate_wallet(commitments, history_confirmed=True)
    assert second.wallet_state() == "ready"
    first.close()
    second.close()


def test_wallet_switch_preserves_other_wallets_unconsumed_hold(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    credential = _podle_credential(0x59)
    commitment = str(credential["commitment"])
    first = _activate(path, commitments)
    assert first.hold_local(commitment)
    first.close()
    _write_history(commitments, external_v1={commitment: credential})

    second = MarketStore(path, wallet_id=WALLET_B)
    second.activate_wallet(commitments, history_confirmed=True)
    assert second.wallet_state() == "ready"
    assert not second.is_available_for_local(commitment)
    assert not second.claim_local(commitment)
    second.close()
    with MarketStore(path, wallet_id=WALLET_A) as first_again:
        assert first_again.claim_local(commitment)


def test_disabled_wallet_records_local_use_without_enabling_sales(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    first = _activate(path, tmp_path / "commitments.json")
    first.close()
    credential = _podle_credential(0x60)
    commitment = str(credential["commitment"])
    with MarketStore(path, wallet_id=WALLET_B) as second:
        assert second.wallet_state() == "disabled"
        assert second.claim_local(commitment)
        assert second.wallet_state() == "disabled"
        with pytest.raises(MarketStoreUnavailableError, match="not ready"):
            second.add_inventory("podle", commitment, credential)


def test_failed_activation_stays_recovery_required_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments, used=["not-a-commitment"])
    store = MarketStore(path, wallet_id=WALLET_A)

    with pytest.raises(MarketStoreCorruptError, match="invalid used"):
        store.activate_wallet(commitments, history_confirmed=True)
    assert store.is_wallet_ledger
    assert store.wallet_state() == "recovery_required"
    store.close()

    reopened = MarketStore(path, wallet_id=WALLET_A)
    assert reopened.wallet_state() == "recovery_required"
    with pytest.raises(MarketStoreUnavailableError, match="explicit recovery"):
        reopened.activate_wallet(commitments, history_confirmed=True)
    reopened.close()


def test_intent_and_v2_corruption_refuse_without_rewriting_database(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    store = _activate(path, commitments)
    store.close()

    intent = path.with_name(f"{path.name}.wallet-ledger")
    intent.unlink()
    before = path.read_bytes()
    with pytest.raises(MarketStoreCorruptError, match="intent"):
        MarketStore(path, wallet_id=WALLET_A)
    assert path.read_bytes() == before

    missing_path = tmp_path / "missing.sqlite"
    missing_intent = missing_path.with_name(f"{missing_path.name}.wallet-ledger")
    missing_intent.write_text("{}", encoding="ascii")
    with pytest.raises(MarketStoreCorruptError, match="intent exists"):
        MarketStore(missing_path, wallet_id=WALLET_A)
    assert not missing_path.exists()


def test_local_holds_become_used_and_conflict_with_global_market_inventory(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    store = _activate(path, commitments)
    local_credential = _podle_credential(0x51)
    local = str(local_credential["commitment"])

    assert store.hold_local(local)
    assert store.is_available_for_local(local)
    assert store.claim_local(local)
    assert not store.is_available_for_local(local)
    assert not store.hold_local(local)
    credential = _podle_credential(0x52)
    store.add_inventory("podle", str(credential["commitment"]), credential)
    with pytest.raises(MarketStoreConflictError, match="owner"):
        store.add_inventory("podle", local, local_credential)
    assert not store.claim_local(str(credential["commitment"]))
    store.close()


def test_independent_connections_observe_activation_and_serialize_local_vs_market_claims(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    legacy_instance = MarketStore(path)
    activated = MarketStore(path, wallet_id=WALLET_A)
    activated.activate_wallet(commitments, history_confirmed=True)

    assert legacy_instance.is_wallet_ledger
    credential = _podle_credential(0x53)
    with pytest.raises(MarketStoreUnavailableError, match="not ready"):
        legacy_instance.add_inventory("podle", str(credential["commitment"]), credential)

    second_connection = MarketStore(path, wallet_id=WALLET_A)
    local_credential = _podle_credential(0x54)
    local = str(local_credential["commitment"])
    assert second_connection.claim_local(local)
    with pytest.raises(MarketStoreConflictError, match="owner"):
        activated.add_inventory("podle", local, local_credential)

    market = _podle_credential(0x55)
    activated.add_inventory("podle", str(market["commitment"]), market)
    assert not second_connection.claim_local(str(market["commitment"]))
    legacy_instance.close()
    activated.close()
    second_connection.close()


def test_activation_rejects_used_or_external_history_that_collides_with_legacy_inventory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.sqlite"
    credential = _podle_credential(0x56)
    legacy = MarketStore(path)
    legacy.add_inventory("podle", str(credential["commitment"]), credential)
    legacy.close()
    commitments = tmp_path / "commitments.json"
    _write_history(commitments, used=[str(credential["commitment"])])

    activating = MarketStore(path, wallet_id=WALLET_A)
    with pytest.raises(MarketStoreCorruptError, match="collides with market inventory"):
        activating.activate_wallet(commitments, history_confirmed=True)
    assert activating.wallet_state() == "recovery_required"
    activating.close()


def test_external_v1_import_is_held_as_a_legacy_pool_and_can_be_claimed(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    credential = _podle_credential(0x57)
    commitment = str(credential["commitment"])
    _write_history(commitments, external_v1={commitment: credential})

    store = _activate(path, commitments)
    assert store.is_available_for_local(commitment)
    assert store.claim_local(commitment)
    assert not store.is_available_for_local(commitment)
    store.close()


@pytest.mark.parametrize(
    "corruption",
    (
        "DELETE FROM metadata WHERE key = 'commitments_path'",
        "DROP TABLE podle_ownership",
        "DELETE FROM podle_ownership",
        "DELETE FROM wallet_ledger WHERE wallet_id = ?",
    ),
)
def test_v2_metadata_table_and_foreign_key_corruption_preserve_database_bytes(
    tmp_path: Path, corruption: str
) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    store = _activate(path, commitments)
    credential = _podle_credential(0x58)
    store.add_inventory("podle", str(credential["commitment"]), credential)
    store.close()

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        if "?" in corruption:
            connection.execute(corruption, (WALLET_A,))
        else:
            connection.execute(corruption)
    before = path.read_bytes()
    with pytest.raises(MarketStoreCorruptError):
        MarketStore(path, wallet_id=WALLET_A)
    assert path.read_bytes() == before


def test_v1_native_wallet_table_is_not_treated_as_a_legacy_store(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    store = MarketStore(path)
    store.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE wallet_ledger (wallet_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
    before = path.read_bytes()
    with pytest.raises(MarketStoreCorruptError, match="interrupted wallet ledger"):
        MarketStore(path)
    assert path.read_bytes() == before


@pytest.mark.parametrize("intent_kind", ("malformed", "mismatched"))
def test_malformed_or_mismatched_v2_intent_preserves_database_bytes(
    tmp_path: Path, intent_kind: str
) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    store = _activate(path, commitments)
    store.close()
    intent = path.with_name(f"{path.name}.wallet-ledger")
    if intent_kind == "malformed":
        intent.write_bytes(b"not-json")
    else:
        other = (tmp_path / "other.json").resolve()
        intent.write_bytes(
            (
                json.dumps(
                    {"commitments_path": str(other), "version": 1},
                    separators=(",", ":"),
                    sort_keys=True,
                )
                + "\n"
            ).encode("ascii")
        )
    before = path.read_bytes()
    with pytest.raises(MarketStoreCorruptError, match="intent"):
        MarketStore(path, wallet_id=WALLET_A)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "mutation", ("DELETE FROM wallet_ledger", "UPDATE wallet_ledger SET state = 'disabled'")
)
def test_v2_without_an_activation_state_is_corrupt(tmp_path: Path, mutation: str) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    store = _activate(path, commitments)
    store.close()

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(mutation)
    before = path.read_bytes()
    with pytest.raises(MarketStoreCorruptError, match="no activation state"):
        MarketStore(path, wallet_id=WALLET_A)
    assert path.read_bytes() == before


def test_live_local_claim_refuses_market_inventory_missing_ownership(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    store = _activate(path, commitments)
    credential = _podle_credential(0x59)
    commitment = str(credential["commitment"])
    store.add_inventory("podle", commitment, credential)

    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM podle_ownership WHERE commitment = ?", (commitment,))
    with pytest.raises(MarketStoreCorruptError, match="matching PoDLE ownership"):
        store.claim_local(commitment)
    store.close()


def test_open_validation_closes_connection_for_custom_corruption_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    store = _activate(path, commitments)
    credential = _podle_credential(0x5A)
    store.add_inventory("podle", str(credential["commitment"]), credential)
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM podle_ownership")

    original_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def capture_connection(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr("jmcore.market_store.sqlite3.connect", capture_connection)
    with pytest.raises(MarketStoreCorruptError, match="matching PoDLE ownership"):
        MarketStore(path, wallet_id=WALLET_A)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")


def test_target_operations_do_not_scan_historical_podle_ownership(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    store = _activate(path, commitments)
    commitment = str(_podle_credential(0x5B)["commitment"])
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)
    try:
        assert store.is_available_for_local(commitment)
    finally:
        store._connection.set_trace_callback(None)

    ownership_queries = [
        statement.lower() for statement in statements if "from podle_ownership" in statement.lower()
    ]
    assert ownership_queries
    assert all("where commitment" in statement for statement in ownership_queries)
    assert not any(statement.lstrip().upper().startswith("PRAGMA") for statement in statements)
    store.close()


def test_concurrent_local_claim_and_market_inventory_insert_have_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments = tmp_path / "commitments.json"
    _write_history(commitments)
    seller = _activate(path, commitments)
    local = MarketStore(path, wallet_id=WALLET_A)
    credential = _podle_credential(0x5C)
    commitment = str(credential["commitment"])
    barrier = threading.Barrier(2)
    outcomes: dict[str, str] = {}

    def claim() -> None:
        barrier.wait()
        outcomes["local"] = "claimed" if local.claim_local(commitment) else "blocked"

    def add_inventory() -> None:
        barrier.wait()
        try:
            seller.add_inventory("podle", commitment, credential)
        except MarketStoreConflictError:
            outcomes["seller"] = "blocked"
        else:
            outcomes["seller"] = "listed"

    threads = [threading.Thread(target=claim), threading.Thread(target=add_inventory)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert (outcomes["local"], outcomes["seller"]) in {
        ("claimed", "blocked"),
        ("blocked", "listed"),
    }
    seller.close()
    local.close()
