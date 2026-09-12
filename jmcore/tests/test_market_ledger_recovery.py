"""Explicit wallet-ledger recovery and side-effect-free diagnosis tests."""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest

from jmcore.bitcoin import hash160
from jmcore.market_store import (
    MarketStore,
    MarketStoreCorruptError,
    MarketStoreError,
    MarketStoreUnavailableError,
)
from jmcore.podle import generate_podle

WALLET_A = "a1" * 32
WALLET_B = "b2" * 32


def _write_history(path: Path, *, used: list[str] | None = None) -> None:
    path.write_text(
        json.dumps({"external_v1": {}, "used": used or []}, sort_keys=True), encoding="utf-8"
    )


def _activate(path: Path, commitments_path: Path, wallet_id: str = WALLET_A) -> MarketStore:
    store = MarketStore(path, wallet_id=wallet_id)
    store.activate_wallet(commitments_path, history_confirmed=True)
    return store


def _make_recovery_required(path: Path, commitments_path: Path) -> MarketStore:
    _write_history(commitments_path, used=["not-a-commitment"])
    store = MarketStore(path, wallet_id=WALLET_A)
    with pytest.raises(MarketStoreCorruptError, match="invalid used"):
        store.activate_wallet(commitments_path, history_confirmed=True)
    assert store.wallet_state() == "recovery_required"
    return store


def _ownership_rows(path: Path) -> list[tuple[str, str | None, str, int | None]]:
    with sqlite3.connect(path) as connection:
        return [
            (str(row[0]), row[1], str(row[2]), row[3])
            for row in connection.execute(
                """
                SELECT commitment, wallet_id, state, inventory_id
                FROM podle_ownership
                ORDER BY commitment
                """
            )
        ]


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


def test_diagnose_absent_directory_creates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "market.sqlite"

    diagnosis = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_A, commitments_path=tmp_path / "commitments.json"
    )

    assert diagnosis == {
        "state": "absent",
        "schema_version": None,
        "reason": "no ledger artifacts",
    }
    assert not path.parent.exists()


def test_diagnose_missing_database_with_intent_requires_recovery(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    intent_path = path.with_name(f"{path.name}.wallet-ledger")
    intent_path.write_bytes(b"{}")

    diagnosis = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_A, commitments_path=tmp_path / "commitments.json"
    )

    assert diagnosis["state"] == "recovery_required"
    assert diagnosis["schema_version"] is None
    assert not path.exists()
    assert intent_path.read_bytes() == b"{}"


def test_diagnose_legacy_store_preserves_artifact(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    legacy = MarketStore(path)
    legacy.close()
    before = path.read_bytes()

    diagnosis = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_A, commitments_path=commitments_path
    )

    assert diagnosis == {
        "state": "legacy",
        "schema_version": "1",
        "reason": "legacy market store",
    }
    assert path.read_bytes() == before
    assert not path.with_name(f"{path.name}.wallet-ledger").exists()


def test_diagnose_reports_ready_disabled_and_recovery_required(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    _write_history(commitments_path)
    store = _activate(path, commitments_path)
    store.close()

    ready = MarketStore.diagnose_wallet(path, wallet_id=WALLET_A, commitments_path=commitments_path)
    disabled = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_B, commitments_path=commitments_path
    )
    mismatched = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_A, commitments_path=tmp_path / "other-commitments.json"
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE wallet_ledger SET state = 'activating' WHERE wallet_id = ?", (WALLET_A,)
        )
    activating = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_A, commitments_path=commitments_path
    )
    with MarketStore(path, wallet_id=WALLET_A) as recovering:
        recovering.mark_recovery_required()
    recovery_required = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_A, commitments_path=commitments_path
    )

    assert ready["state"] == "ready"
    assert ready["schema_version"] == "2"
    assert disabled["state"] == "disabled"
    assert disabled["schema_version"] == "2"
    assert mismatched["state"] == "recovery_required"
    assert mismatched["schema_version"] == "2"
    assert activating["state"] == "activating"
    assert activating["schema_version"] == "2"
    assert recovery_required["state"] == "recovery_required"
    assert recovery_required["schema_version"] == "2"


@pytest.mark.parametrize("artifact", ("missing", "malformed", "symlink"))
def test_diagnose_rejects_invalid_intent_artifacts(tmp_path: Path, artifact: str) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    _write_history(commitments_path)
    store = _activate(path, commitments_path)
    store.close()
    intent_path = path.with_name(f"{path.name}.wallet-ledger")
    if artifact == "missing":
        intent_path.unlink()
    elif artifact == "malformed":
        intent_path.write_bytes(b"not-json")
    else:
        intent_path.unlink()
        intent_path.symlink_to(commitments_path)

    diagnosis = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_A, commitments_path=commitments_path
    )

    assert diagnosis["state"] == "recovery_required"


def test_diagnose_rejects_symlink_database_without_opening_it(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite"
    alias = tmp_path / "market.sqlite"
    target.write_bytes(b"not a database")
    alias.symlink_to(target)

    diagnosis = MarketStore.diagnose_wallet(
        alias, wallet_id=WALLET_A, commitments_path=tmp_path / "commitments.json"
    )

    assert diagnosis["state"] == "recovery_required"
    assert target.read_bytes() == b"not a database"


def test_diagnose_does_not_change_permissive_database_or_intent(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    _write_history(commitments_path)
    store = _activate(path, commitments_path)
    store.close()
    intent_path = path.with_name(f"{path.name}.wallet-ledger")
    path.chmod(0o644)
    intent_path.chmod(0o644)
    before = {
        artifact: (artifact.read_bytes(), stat.S_IMODE(artifact.stat().st_mode))
        for artifact in (path, intent_path)
    }

    diagnosis = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_A, commitments_path=commitments_path
    )

    assert diagnosis["state"] == "ready"
    assert {
        artifact: (artifact.read_bytes(), stat.S_IMODE(artifact.stat().st_mode))
        for artifact in (path, intent_path)
    } == before


def test_diagnose_journal_artifact_is_unavailable_and_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    _write_history(commitments_path)
    store = _activate(path, commitments_path)
    store.close()
    intent_path = path.with_name(f"{path.name}.wallet-ledger")
    journal_path = path.with_name(f"{path.name}-journal")
    journal_path.write_bytes(b"unfinished")
    journal_path.chmod(0o644)
    before = {
        artifact: (artifact.read_bytes(), stat.S_IMODE(artifact.stat().st_mode))
        for artifact in (path, intent_path, journal_path)
    }

    diagnosis = MarketStore.diagnose_wallet(
        path, wallet_id=WALLET_A, commitments_path=commitments_path
    )

    assert diagnosis == {
        "state": "unavailable",
        "schema_version": None,
        "reason": "inspection unavailable",
    }
    assert {
        artifact: (artifact.read_bytes(), stat.S_IMODE(artifact.stat().st_mode))
        for artifact in (path, intent_path, journal_path)
    } == before


def test_recovery_requires_confirmation_and_existing_history(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    store = _make_recovery_required(path, commitments_path)

    with pytest.raises(MarketStoreError, match="history_confirmed"):
        store.recover_wallet(commitments_path)
    other_commitments_path = tmp_path / "other-commitments.json"
    _write_history(other_commitments_path)
    with pytest.raises(MarketStoreError, match="does not match"):
        store.recover_wallet(other_commitments_path, history_confirmed=True)
    commitments_path.unlink()
    with pytest.raises(MarketStoreError, match="must exist"):
        store.recover_wallet(commitments_path, history_confirmed=True)

    assert store.wallet_state() == "recovery_required"
    store.close()


def test_recovery_rejects_invalid_or_colliding_history_without_clearing_recovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    credential = _podle_credential(0x41)
    commitment = str(credential["commitment"])
    legacy = MarketStore(path)
    legacy.add_inventory("podle", commitment, credential)
    legacy.close()
    store = _make_recovery_required(path, commitments_path)
    before = _ownership_rows(path)

    commitments_path.write_bytes(b"not-json")
    with pytest.raises(MarketStoreCorruptError, match="could not read"):
        store.recover_wallet(commitments_path, history_confirmed=True)
    assert store.wallet_state() == "recovery_required"

    _write_history(commitments_path, used=[commitment])
    with pytest.raises(MarketStoreCorruptError, match="collides with market inventory"):
        store.recover_wallet(commitments_path, history_confirmed=True)
    assert store.wallet_state() == "recovery_required"
    assert _ownership_rows(path) == before
    store.close()


def test_recovery_monotonically_merges_history_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    _write_history(commitments_path)
    store = _activate(path, commitments_path)
    existing_used = "10" * 32
    other_wallet_hold = "11" * 32
    recovered_used = "12" * 32
    assert store.claim_local(existing_used)
    with MarketStore(path, wallet_id=WALLET_B) as other_wallet:
        assert other_wallet.hold_local(other_wallet_hold)
    store.mark_recovery_required()
    _write_history(commitments_path, used=[recovered_used])

    store.recover_wallet(commitments_path, history_confirmed=True)
    assert store.wallet_state() == "ready"
    expected_rows = [
        (existing_used, WALLET_A, "used", None),
        (other_wallet_hold, WALLET_B, "held_local", None),
        (recovered_used, None, "used", None),
    ]
    assert _ownership_rows(path) == expected_rows
    store.close()

    with MarketStore(path, wallet_id=WALLET_A, create=False) as reopened:
        assert reopened.wallet_state() == "ready"
        reopened.recover_wallet(commitments_path, history_confirmed=True)
        assert reopened.wallet_state() == "ready"
    assert _ownership_rows(path) == expected_rows


def test_recovery_does_not_activate_disabled_or_legacy_wallets(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    _write_history(commitments_path)
    first = _activate(path, commitments_path)
    first.close()

    with MarketStore(path, wallet_id=WALLET_B, create=False) as disabled:
        with pytest.raises(MarketStoreUnavailableError, match="activated"):
            disabled.recover_wallet(commitments_path, history_confirmed=True)
        assert disabled.wallet_state() == "disabled"

    legacy_path = tmp_path / "legacy.sqlite"
    legacy = MarketStore(legacy_path, wallet_id=WALLET_A)
    with pytest.raises(MarketStoreError, match="has not been activated"):
        legacy.recover_wallet(commitments_path, history_confirmed=True)
    legacy.close()
    with sqlite3.connect(legacy_path) as connection:
        assert connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone() == ("1",)
    assert not legacy_path.with_name(f"{legacy_path.name}.wallet-ledger").exists()


def test_activation_cannot_clear_recovery_required_state(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    commitments_path = tmp_path / "commitments.json"
    store = _make_recovery_required(path, commitments_path)
    _write_history(commitments_path)

    with pytest.raises(MarketStoreUnavailableError, match="explicit recovery"):
        store.activate_wallet(commitments_path, history_confirmed=True)

    assert store.wallet_state() == "recovery_required"
    store.close()


def test_recovery_consumes_confirmed_holds_without_reassigning_owners(tmp_path: Path) -> None:
    path = tmp_path / "market.sqlite"
    history = tmp_path / "commitments.json"
    _write_history(history)
    with _activate(path, history) as store:
        assert store.hold_local("11" * 32)
        with MarketStore(path, wallet_id=WALLET_B) as other:
            assert other.hold_local("22" * 32)
        store.mark_recovery_required()
        _write_history(history, used=["11" * 32, "22" * 32])
        store.recover_wallet(history, history_confirmed=True)
        assert store.wallet_state() == "ready"
        assert not store.claim_local("11" * 32)
    assert _ownership_rows(path) == [
        ("11" * 32, WALLET_A, "used", None),
        ("22" * 32, WALLET_B, "used", None),
    ]


def test_history_disappearing_after_precheck_cannot_complete_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "market.sqlite"
    history = tmp_path / "commitments.json"
    with _make_recovery_required(path, history) as store:
        _write_history(history)
        read_history = store._read_commitments_history

        def remove_then_read(*args: Any, **kwargs: Any) -> Any:
            history.unlink()
            return read_history(*args, **kwargs)

        monkeypatch.setattr(store, "_read_commitments_history", remove_then_read)
        with pytest.raises(MarketStoreCorruptError, match="could not read"):
            store.recover_wallet(history, history_confirmed=True)
        assert store.wallet_state() == "recovery_required"
