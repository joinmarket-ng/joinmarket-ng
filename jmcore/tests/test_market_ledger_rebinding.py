"""Explicit, durable wallet-ledger commitments-path rebinding tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Literal

import pytest
from bitcointx.core.key import CKey
from nacl.public import PrivateKey

from jmcore.bitcoin import hash160
from jmcore.credential_market import (
    BondReference,
    MarketAuthorization,
    PaymentTerms,
    canonical,
    period_at_height,
    sign_document,
)
from jmcore.external_podle import ExternalPoDLEOutpoint
from jmcore.market_store import (
    MarketStore,
    MarketStoreCorruptError,
    MarketStoreError,
    MarketStoreUnavailableError,
)
from jmcore.podle import generate_podle

WALLET_A = "a1" * 32
WALLET_B = "b2" * 32
EXISTING_USED = "10" * 32
OWN_HOLD = "11" * 32
OTHER_WALLET_HOLD = "12" * 32
MOVED_HISTORY_USED = "13" * 32


def _write_history(path: Path, *, used: list[str] | None = None) -> None:
    path.write_text(
        json.dumps({"external_v1": {}, "used": used or []}, sort_keys=True), encoding="ascii"
    )


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


def _seller_key(value: int) -> CKey:
    return CKey(bytes([value]) * 32)


def _authorization() -> object:
    owner = _seller_key(0x21)
    seller = _seller_key(0x22)
    bond = BondReference(
        network="regtest",
        outpoint=ExternalPoDLEOutpoint(txid="aa" * 32, vout=1),
        pubkey=bytes(owner.pub).hex(),
        locktime=600_000_000,
    )
    return sign_document(
        MarketAuthorization(
            bond=bond, period=period_at_height(1), seller_pubkey=bytes(seller.pub).hex()
        ),
        owner,
    )


def _payment(value: int) -> PaymentTerms:
    return PaymentTerms(
        rail="onchain",
        request=f"regtest-payment-{value}",
        amount_sats=1_000 + value,
        min_confirmations=1,
    )


def _metadata(path: Path) -> dict[str, str]:
    with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True) as connection:
        return {
            str(key): str(value)
            for key, value in connection.execute("SELECT key, value FROM metadata")
        }


def _ownership_rows(path: Path) -> list[tuple[str, str | None, str, int | None]]:
    with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True) as connection:
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


def _seller_rows(path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True) as connection:
        return {
            "inventory": [
                tuple(row)
                for row in connection.execute(
                    """
                    SELECT id, product, resource, credential, certificate_pubkey, state,
                           reservation_quote_id, wallet_id
                    FROM inventory ORDER BY id
                    """
                )
            ],
            "payments": [
                tuple(row)
                for row in connection.execute(
                    "SELECT payment_id, rail, request, terms, state, quote_id FROM payments"
                    " ORDER BY payment_id"
                )
            ],
            "quotes": [
                tuple(row)
                for row in connection.execute(
                    """
                    SELECT quote_id, request_id, request_fingerprint, quote_document, buyer_pubkey,
                           product, resource, certificate_pubkey, inventory_id, payment_id,
                           created_at, expires_at, state, pending_credential, settlement_ref,
                           package, sealed_delivery
                    FROM quotes ORDER BY quote_id
                    """
                )
            ],
        }


def _marker(source: Path, target: Path) -> str:
    return canonical({"from": str(source), "to": str(target)}).decode("ascii")


def _intent_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.wallet-ledger")


def _moved_ledger(
    tmp_path: Path, *, state: Literal["ready", "recovery_required"] = "ready"
) -> tuple[Path, Path, Path]:
    old_directory = tmp_path / "old-data"
    old_directory.mkdir()
    source = old_directory / "commitments.json"
    _write_history(source)
    old_database = old_directory / "market.sqlite"
    store = MarketStore(old_database, wallet_id=WALLET_A)
    store.activate_wallet(source, history_confirmed=True)
    assert store.claim_local(EXISTING_USED)
    assert store.hold_local(OWN_HOLD)
    with MarketStore(old_database, wallet_id=WALLET_B) as other_wallet:
        assert other_wallet.hold_local(OTHER_WALLET_HOLD)
    credential = _podle_credential(0x41)
    store.add_inventory("podle", str(credential["commitment"]), credential)
    store.add_payment(_payment(1), "payment-1")
    store.create_quote(
        _authorization(),
        _seller_key(0x22),
        bytes(PrivateKey(bytes([0x23]) * 32).public_key).hex(),
        "podle",
        None,
        "onchain",
        10_000,
        100,
        1,
        request_id="01" * 16,
    )
    if state == "recovery_required":
        store.mark_recovery_required()
    store.close()

    new_directory = tmp_path / "new-data"
    old_directory.rename(new_directory)
    database = new_directory / "market.sqlite"
    target = new_directory / "commitments.json"
    _write_history(target, used=[MOVED_HISTORY_USED])
    assert not source.exists()
    return database, source, target


@pytest.mark.parametrize("state", ("ready", "recovery_required"))
def test_rebind_moves_renamed_ledger_without_resetting_market_or_local_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: Literal["ready", "recovery_required"]
) -> None:
    database, source, target = _moved_ledger(tmp_path, state=state)
    before_ownership = _ownership_rows(database)
    before_seller = _seller_rows(database)
    target_before = target.read_bytes()
    read_history = MarketStore._read_commitments_history
    history_paths: list[Path] = []

    def record_history(self: MarketStore, path: Path, **kwargs: Any) -> Any:
        history_paths.append(path)
        return read_history(self, path, **kwargs)

    monkeypatch.setattr(MarketStore, "_read_commitments_history", record_history)

    MarketStore.rebind_wallet(
        database,
        wallet_id=WALLET_A,
        previous_commitments_path=source,
        commitments_path=target,
        history_confirmed=True,
        writers_stopped=True,
    )

    assert history_paths == [target]
    assert not source.exists()
    assert target.read_bytes() == target_before
    assert _metadata(database) == {
        "commitments_path": str(target),
        "commitments_rebind_completed": _marker(source, target),
        "schema_version": "2",
    }
    assert _intent_path(database).read_bytes() == MarketStore._wallet_intent_bytes(target)
    assert _seller_rows(database) == before_seller
    after_ownership = _ownership_rows(database)
    assert all(row in after_ownership for row in before_ownership)
    assert (MOVED_HISTORY_USED, None, "used", None) in after_ownership
    with MarketStore(database, wallet_id=WALLET_A, create=False) as reopened:
        assert reopened.wallet_state() == state

    completed_ownership = _ownership_rows(database)
    completed_seller = _seller_rows(database)
    MarketStore.rebind_wallet(
        database,
        wallet_id=WALLET_A,
        previous_commitments_path=source,
        commitments_path=target,
        history_confirmed=True,
        writers_stopped=True,
    )
    assert _ownership_rows(database) == completed_ownership
    assert _seller_rows(database) == completed_seller


def test_rebind_commits_marker_before_merge_and_clears_it_after_fsynced_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database, source, target = _moved_ledger(tmp_path)
    marker = _marker(source, target)
    old_intent = _intent_path(database).read_bytes()
    events: list[str] = []
    write_intent = MarketStore._write_wallet_intent
    fsync_parent = MarketStore._fsync_parent

    def observe_fsync(path: Path) -> None:
        assert _metadata(database) == {
            "commitments_path": str(target),
            "commitments_rebind": marker,
            "schema_version": "2",
        }
        assert _intent_path(database).read_bytes() == MarketStore._wallet_intent_bytes(target)
        events.append("fsync")
        fsync_parent(path)

    def observe_intent(self: MarketStore, commitments_path: Path) -> None:
        assert _metadata(database) == {
            "commitments_path": str(target),
            "commitments_rebind": marker,
            "schema_version": "2",
        }
        assert _intent_path(database).read_bytes() == old_intent
        events.append("intent-start")
        write_intent(self, commitments_path)
        assert events == ["intent-start", "fsync"]
        assert _intent_path(database).read_bytes() == MarketStore._wallet_intent_bytes(target)
        assert _metadata(database)["commitments_rebind"] == marker
        events.append("intent-fsynced")

    monkeypatch.setattr(MarketStore, "_fsync_parent", staticmethod(observe_fsync))
    monkeypatch.setattr(MarketStore, "_write_wallet_intent", observe_intent)

    MarketStore.rebind_wallet(
        database,
        wallet_id=WALLET_A,
        previous_commitments_path=source,
        commitments_path=target,
        history_confirmed=True,
        writers_stopped=True,
    )

    assert events == ["intent-start", "fsync", "intent-fsynced"]
    assert _metadata(database) == {
        "commitments_path": str(target),
        "commitments_rebind_completed": marker,
        "schema_version": "2",
    }


@pytest.mark.parametrize(
    "kwargs",
    (
        {},
        {"history_confirmed": True},
        {"writers_stopped": True},
    ),
)
def test_rebind_requires_confirmation_and_stopped_writers(
    tmp_path: Path, kwargs: dict[str, bool]
) -> None:
    database = tmp_path / "missing.sqlite"

    with pytest.raises(MarketStoreError, match="confirmed history and stopped writers"):
        MarketStore.rebind_wallet(
            database,
            wallet_id=WALLET_A,
            previous_commitments_path=tmp_path / "old.json",
            commitments_path=tmp_path / "new.json",
            **kwargs,
        )

    assert not database.exists()
    assert not _intent_path(database).exists()


def test_rebind_refuses_absent_legacy_disabled_and_corrupt_ledgers(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    _write_history(target)

    absent = tmp_path / "absent.sqlite"
    with pytest.raises(MarketStoreError, match="existing regular market store"):
        MarketStore.rebind_wallet(
            absent,
            wallet_id=WALLET_A,
            previous_commitments_path=tmp_path / "old.json",
            commitments_path=target,
            history_confirmed=True,
            writers_stopped=True,
        )
    assert not absent.exists()

    legacy = tmp_path / "legacy.sqlite"
    MarketStore(legacy).close()
    legacy_before = legacy.read_bytes()
    with pytest.raises(MarketStoreError, match="has not been activated"):
        MarketStore.rebind_wallet(
            legacy,
            wallet_id=WALLET_A,
            previous_commitments_path=tmp_path / "legacy-old.json",
            commitments_path=target,
            history_confirmed=True,
            writers_stopped=True,
        )
    assert legacy.read_bytes() == legacy_before
    assert not _intent_path(legacy).exists()

    disabled_source = tmp_path / "disabled-source.json"
    _write_history(disabled_source)
    disabled = tmp_path / "disabled.sqlite"
    with MarketStore(disabled, wallet_id=WALLET_A) as store:
        store.activate_wallet(disabled_source, history_confirmed=True)
    disabled_before = _metadata(disabled)
    with pytest.raises(MarketStoreUnavailableError, match="activated wallet"):
        MarketStore.rebind_wallet(
            disabled,
            wallet_id=WALLET_B,
            previous_commitments_path=disabled_source,
            commitments_path=target,
            history_confirmed=True,
            writers_stopped=True,
        )
    assert _metadata(disabled) == disabled_before

    corrupt_source = tmp_path / "corrupt-source.json"
    _write_history(corrupt_source)
    corrupt = tmp_path / "corrupt.sqlite"
    with MarketStore(corrupt, wallet_id=WALLET_A) as store:
        store.activate_wallet(corrupt_source, history_confirmed=True)
    _intent_path(corrupt).unlink()
    corrupt_before = corrupt.read_bytes()
    with pytest.raises(MarketStoreCorruptError, match="intent"):
        MarketStore.rebind_wallet(
            corrupt,
            wallet_id=WALLET_A,
            previous_commitments_path=corrupt_source,
            commitments_path=target,
            history_confirmed=True,
            writers_stopped=True,
        )
    assert corrupt.read_bytes() == corrupt_before
    assert not _intent_path(corrupt).exists()


def test_rebind_refuses_missing_history_and_wrong_source_path_without_artifacts(
    tmp_path: Path,
) -> None:
    database, source, target = _moved_ledger(tmp_path)
    target.unlink()
    database_before = database.read_bytes()
    intent_before = _intent_path(database).read_bytes()

    with pytest.raises(MarketStoreError, match="must exist"):
        MarketStore.rebind_wallet(
            database,
            wallet_id=WALLET_A,
            previous_commitments_path=source,
            commitments_path=target,
            history_confirmed=True,
            writers_stopped=True,
        )

    assert not source.exists()
    assert not target.exists()
    assert database.read_bytes() == database_before
    assert _intent_path(database).read_bytes() == intent_before

    _write_history(target)
    wrong_source = tmp_path / "wrong-source.json"
    before_wrong_source = database.read_bytes()
    before_wrong_intent = _intent_path(database).read_bytes()
    with pytest.raises(MarketStoreError, match="previous commitments path"):
        MarketStore.rebind_wallet(
            database,
            wallet_id=WALLET_A,
            previous_commitments_path=wrong_source,
            commitments_path=target,
            history_confirmed=True,
            writers_stopped=True,
        )
    assert not wrong_source.exists()
    assert database.read_bytes() == before_wrong_source
    assert _intent_path(database).read_bytes() == before_wrong_intent


@pytest.mark.parametrize("failure", ("history", "intent", "clear"))
def test_rebind_crashes_leave_a_pending_exact_retry_and_block_normal_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Literal["history", "intent", "clear"]
) -> None:
    database, source, target = _moved_ledger(tmp_path)
    retained = MarketStore(database, wallet_id=WALLET_A, create=False)
    marker = _marker(source, target)
    old_intent = _intent_path(database).read_bytes()
    target_intent = MarketStore._wallet_intent_bytes(target)

    with monkeypatch.context() as patch:
        if failure == "history":

            def fail_history(self: MarketStore, path: Path, **kwargs: Any) -> Any:
                raise MarketStoreCorruptError("injected history read failure")

            patch.setattr(MarketStore, "_read_commitments_history", fail_history)
            expected_path = source
            expected_intent = old_intent
            error = "history read failure"
        elif failure == "intent":

            def fail_intent(self: MarketStore, path: Path) -> None:
                raise MarketStoreError("injected intent write failure")

            patch.setattr(MarketStore, "_write_wallet_intent", fail_intent)
            expected_path = target
            expected_intent = old_intent
            error = "intent write failure"
        else:
            verify_schema = MarketStore._verify_schema

            def fail_clear(self: MarketStore, *args: Any, **kwargs: Any) -> None:
                if _intent_path(database).read_bytes() == target_intent:
                    raise MarketStoreCorruptError("injected marker clearing failure")
                verify_schema(self, *args, **kwargs)

            patch.setattr(MarketStore, "_verify_schema", fail_clear)
            expected_path = target
            expected_intent = target_intent
            error = "marker clearing failure"

        with pytest.raises(MarketStoreError, match=error):
            MarketStore.rebind_wallet(
                database,
                wallet_id=WALLET_A,
                previous_commitments_path=source,
                commitments_path=target,
                history_confirmed=True,
                writers_stopped=True,
            )

    assert _metadata(database) == {
        "commitments_path": str(expected_path),
        "commitments_rebind": marker,
        "schema_version": "2",
    }
    assert _intent_path(database).read_bytes() == expected_intent
    assert not source.exists()
    with pytest.raises(MarketStoreCorruptError, match="rebinding requires explicit completion"):
        MarketStore(database, wallet_id=WALLET_A, create=False)
    with pytest.raises(MarketStoreCorruptError, match="rebinding requires explicit completion"):
        retained.hold_local("17" * 32)
    with pytest.raises(MarketStoreCorruptError, match="rebinding requires explicit completion"):
        retained.add_payment(_payment(2), "payment-2")
    with pytest.raises(MarketStoreCorruptError, match="rebinding requires explicit completion"):
        retained.pending(400)
    retained.close()

    MarketStore.rebind_wallet(
        database,
        wallet_id=WALLET_A,
        previous_commitments_path=source,
        commitments_path=target,
        history_confirmed=True,
        writers_stopped=True,
    )
    assert _metadata(database) == {
        "commitments_path": str(target),
        "commitments_rebind_completed": marker,
        "schema_version": "2",
    }
    assert _intent_path(database).read_bytes() == target_intent
    assert (MOVED_HISTORY_USED, None, "used", None) in _ownership_rows(database)


@pytest.mark.parametrize("wrong_part", ("from", "to"))
def test_rebind_rejects_wrong_pending_retry_without_modifying_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_part: Literal["from", "to"]
) -> None:
    database, source, target = _moved_ledger(tmp_path)

    def fail_history(self: MarketStore, path: Path, **kwargs: Any) -> Any:
        raise MarketStoreCorruptError("injected history read failure")

    monkeypatch.setattr(MarketStore, "_read_commitments_history", fail_history)
    with pytest.raises(MarketStoreCorruptError, match="history read failure"):
        MarketStore.rebind_wallet(
            database,
            wallet_id=WALLET_A,
            previous_commitments_path=source,
            commitments_path=target,
            history_confirmed=True,
            writers_stopped=True,
        )
    monkeypatch.undo()

    wrong_source = tmp_path / "wrong-source.json"
    wrong_target = tmp_path / "wrong-target.json"
    _write_history(wrong_target)
    retry_source = wrong_source if wrong_part == "from" else source
    retry_target = target if wrong_part == "from" else wrong_target
    database_before = database.read_bytes()
    intent_before = _intent_path(database).read_bytes()
    metadata_before = _metadata(database)

    with pytest.raises(MarketStoreCorruptError, match="rebinding artifacts do not match"):
        MarketStore.rebind_wallet(
            database,
            wallet_id=WALLET_A,
            previous_commitments_path=retry_source,
            commitments_path=retry_target,
            history_confirmed=True,
            writers_stopped=True,
        )

    assert not wrong_source.exists()
    assert database.read_bytes() == database_before
    assert _intent_path(database).read_bytes() == intent_before
    assert _metadata(database) == metadata_before


def test_rebind_rejects_wrong_source_even_when_target_is_already_bound(tmp_path: Path) -> None:
    database, source, target = _moved_ledger(tmp_path)
    MarketStore.rebind_wallet(
        database,
        wallet_id=WALLET_A,
        previous_commitments_path=source,
        commitments_path=target,
        history_confirmed=True,
        writers_stopped=True,
    )
    wrong_source = tmp_path / "wrong-source.json"
    database_before = database.read_bytes()
    intent_before = _intent_path(database).read_bytes()

    with pytest.raises(MarketStoreError, match="previous commitments path"):
        MarketStore.rebind_wallet(
            database,
            wallet_id=WALLET_A,
            previous_commitments_path=wrong_source,
            commitments_path=target,
            history_confirmed=True,
            writers_stopped=True,
        )

    assert not wrong_source.exists()
    assert database.read_bytes() == database_before
    assert _intent_path(database).read_bytes() == intent_before
