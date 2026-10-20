"""Wallet-ledger integration coverage for local and external PoDLE use."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_utxo
from jmcore.bitcoin import hash160
from jmcore.external_podle import ExternalPoDLE
from jmcore.market_store import MarketStore, MarketStoreConflictError
from jmcore.paths import get_market_store_path
from jmcore.podle import PoDLECommitment, generate_podle
from jmwallet.backends.base import UTXO

from taker.podle_manager import ExternalPoDLEPoolError, PoDLEManager

WALLET_ID = "ab" * 32


class _EmptyBlacklist:
    def is_blacklisted(self, _commitment: str) -> bool:
        return False


class _OneBlacklist:
    def __init__(self, commitment: str) -> None:
        self.commitment = commitment

    def is_blacklisted(self, commitment: str) -> bool:
        return commitment == self.commitment


def _record(proof: PoDLECommitment, *, txid: str, vout: int, index: int = 0) -> ExternalPoDLE:
    return ExternalPoDLE(
        version=1,
        network="regtest",
        outpoint={"txid": txid, "vout": vout},
        P=proof.p.hex(),
        P2=proof.p2.hex(),
        sig=proof.sig.hex(),
        e=proof.e.hex(),
        commitment=proof.commitment.hex(),
        index=index,
        scriptpubkey=(b"\x00\x14" + hash160(proof.p)).hex(),
        blockheight=100,
    )


def _external_record(*, key_byte: int = 0x12, txid_char: str = "c") -> ExternalPoDLE:
    txid = txid_char * 64
    proof = generate_podle(bytes([key_byte]) * 32, f"{txid}:2", 0)
    return _record(proof, txid=txid, vout=2)


def _activate(manager: PoDLEManager, commitments_path: Path | None = None) -> MarketStore:
    store = MarketStore(manager._market_store_path, wallet_id=WALLET_ID)
    store.activate_wallet(commitments_path or manager.filepath, history_confirmed=True)
    return store


def test_selection_reloads_projection_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    utxos = [make_utxo(txid_char=char, address=f"bcrt1qsynthetic{char}") for char in "abc"]
    keys = {utxo.address: bytes([index + 1]) * 32 for index, utxo in enumerate(utxos)}
    reload = Mock(wraps=manager._reload)
    monkeypatch.setattr(manager, "_reload", reload)

    assert len(_fresh_utxos(manager, utxos, keys)) == 3
    assert reload.call_count == 1
    assert manager.used_commitments == set()
    manager.close()
    store.close()


def _backend(record: ExternalPoDLE) -> AsyncMock:
    backend = AsyncMock()
    backend.requires_neutrino_metadata = Mock(return_value=False)
    backend.get_utxo = AsyncMock(
        return_value=UTXO(
            txid=record.outpoint.txid,
            vout=record.outpoint.vout,
            value=1_000_000,
            address="bcrt1qexternal",
            confirmations=6,
            scriptpubkey=record.scriptpubkey,
            height=record.blockheight,
        )
    )
    return backend


def _generate(manager: PoDLEManager, utxos: list, keys: dict[str, bytes]):
    return manager.generate_fresh_commitment(
        utxos,
        cj_amount=1_000_000,
        private_key_getter=keys.get,
        min_confirmations=5,
        min_percent=20,
        max_retries=1,
    )


def _fresh_utxos(manager: PoDLEManager, utxos: list, keys: dict[str, bytes]) -> list:
    return manager.get_fresh_commitment_utxos(
        utxos,
        cj_amount=1_000_000,
        private_key_getter=keys.get,
        min_confirmations=5,
        min_percent=20,
        max_retries=1,
    )


def test_inactive_manager_never_creates_or_mutates_market_store(tmp_path: Path) -> None:
    first = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    first_utxo = make_utxo(txid_char="a", address="bcrt1qinactive1")

    assert _generate(first, [first_utxo], {first_utxo.address: b"\x01" * 32}) is not None
    market_path = get_market_store_path(tmp_path)
    assert not market_path.exists()
    assert not market_path.parent.exists()

    legacy = MarketStore(market_path)
    legacy.close()
    before = market_path.read_bytes()
    second = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    second_utxo = make_utxo(txid_char="b", address="bcrt1qinactive2")

    assert _generate(second, [second_utxo], {second_utxo.address: b"\x02" * 32}) is not None
    assert market_path.read_bytes() == before
    assert not market_path.with_name(f"{market_path.name}.wallet-ledger").exists()


@pytest.mark.parametrize("failure", ("malformed_json", "save", "lock"))
def test_legacy_local_generation_preserves_suppressed_persistence_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    utxo = make_utxo(txid_char="a", address="bcrt1qlegacy-failure")
    proof = generate_podle(b"\x09" * 32, f"{utxo.txid}:{utxo.vout}", 0)
    if failure == "malformed_json":
        manager.filepath.write_text("{not-json", encoding="utf-8")
    elif failure == "save":
        monkeypatch.setattr(
            manager,
            "_save_locked",
            Mock(side_effect=ExternalPoDLEPoolError("simulated JSON write failure")),
        )
    else:
        monkeypatch.setattr(
            "taker.podle_manager.exclusive_file_lock",
            Mock(side_effect=OSError("simulated lock failure")),
        )

    generated = _generate(manager, [utxo], {utxo.address: b"\x09" * 32})

    assert generated is not None
    assert proof.commitment.hex() in manager.used_commitments
    assert not get_market_store_path(tmp_path).exists()
    if failure == "malformed_json":
        assert manager.filepath.read_text(encoding="utf-8") == "{not-json"


def test_native_local_generation_refuses_malformed_json_before_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    utxo = make_utxo(txid_char="a", address="bcrt1qnative-malformed")
    proof = generate_podle(b"\x0a" * 32, f"{utxo.txid}:{utxo.vout}", 0)
    manager.filepath.write_text("{not-json", encoding="utf-8")

    assert _generate(manager, [utxo], {utxo.address: b"\x0a" * 32}) is None
    assert store.is_available_for_local(proof.commitment.hex())
    store.close()


def test_native_local_generation_refuses_failed_sidecar_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    utxo = make_utxo(txid_char="a", address="bcrt1qnative-lock")
    proof = generate_podle(b"\x0b" * 32, f"{utxo.txid}:{utxo.vout}", 0)
    monkeypatch.setattr(
        "taker.podle_manager.exclusive_file_lock",
        Mock(side_effect=OSError("simulated lock failure")),
    )

    assert _generate(manager, [utxo], {utxo.address: b"\x0b" * 32}) is None
    assert store.is_available_for_local(proof.commitment.hex())
    store.close()


def test_active_generation_excludes_market_inventory_and_survives_json_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    seller_utxo = make_utxo(txid_char="a", address="bcrt1qseller")
    fresh_utxo = make_utxo(txid_char="b", address="bcrt1qfresh")
    seller_key = b"\x03" * 32
    fresh_key = b"\x04" * 32
    seller_proof = generate_podle(seller_key, f"{seller_utxo.txid}:{seller_utxo.vout}", 0)
    seller_record = _record(seller_proof, txid=seller_utxo.txid, vout=seller_utxo.vout)
    store.add_inventory("podle", seller_record.commitment, seller_record.model_dump(mode="json"))

    keys = {seller_utxo.address: seller_key, fresh_utxo.address: fresh_key}
    assert _fresh_utxos(manager, [seller_utxo], keys) == []
    generated = _generate(manager, [seller_utxo, fresh_utxo], keys)

    assert generated is not None
    commitment = generated.commitment.commitment.hex()
    assert commitment in manager.used_commitments
    assert not store.is_available_for_local(commitment)
    assert generated.utxo == f"{fresh_utxo.txid}:{fresh_utxo.vout}"

    manager.filepath.unlink()
    restarted = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    assert _fresh_utxos(restarted, [fresh_utxo], keys) == []
    store.close()


@pytest.mark.asyncio
async def test_active_external_import_and_consume_use_wallet_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    record = _external_record()

    assert manager.import_external(record)
    assert store.is_available_for_local(record.commitment)
    consumed = await manager.consume_external(_backend(record), "regtest", 1_000_000, 5, 20, 1)

    assert consumed is not None
    assert consumed.commitment.commitment.hex() == record.commitment
    assert record.commitment in manager.used_commitments
    assert manager.external_count() == 0
    assert not store.is_available_for_local(record.commitment)
    store.close()


def test_native_local_projection_failure_burns_commitment_without_returning_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    utxo = make_utxo(txid_char="a", address="bcrt1qprojection")
    proof = generate_podle(b"\x05" * 32, f"{utxo.txid}:{utxo.vout}", 0)
    monkeypatch.setattr(
        manager,
        "_save_locked",
        Mock(side_effect=ExternalPoDLEPoolError("simulated JSON write failure")),
    )

    assert _generate(manager, [utxo], {utxo.address: b"\x05" * 32}) is None
    assert not store.is_available_for_local(proof.commitment.hex())
    assert not manager.filepath.exists()

    restarted = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    assert _fresh_utxos(restarted, [utxo], {utxo.address: b"\x05" * 32}) == []
    store.close()


@pytest.mark.asyncio
async def test_native_external_projection_failure_burns_record_without_returning_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    record = _external_record()
    assert manager.import_external(record)
    original_save = manager._save_locked
    saves = 0

    def fail_projection() -> None:
        nonlocal saves
        saves += 1
        if saves == 2:
            raise ExternalPoDLEPoolError("simulated JSON write failure")
        original_save()

    monkeypatch.setattr(manager, "_save_locked", fail_projection)
    consumed = await manager.consume_external(_backend(record), "regtest", 1_000_000, 5, 20, 1)

    assert consumed is None
    assert saves == 2
    assert not store.is_available_for_local(record.commitment)
    assert manager.external_count() == 0
    store.close()


@pytest.mark.parametrize("failure", ("recovery", "corrupt", "missing_database", "missing_intent"))
def test_native_ledger_failures_refuse_external_import(tmp_path: Path, failure: str) -> None:
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    market_path = manager._market_store_path
    intent_path = market_path.with_name(f"{market_path.name}.wallet-ledger")
    if failure == "recovery":
        store.mark_recovery_required()
    else:
        store.close()
        if failure == "corrupt":
            market_path.write_bytes(b"not a sqlite database")
        elif failure == "missing_database":
            market_path.unlink()
        else:
            intent_path.unlink()

    with pytest.raises(ExternalPoDLEPoolError):
        manager.import_external(_external_record())


def test_observed_native_ledger_artifact_loss_never_falls_back_to_json(tmp_path: Path) -> None:
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    assert manager.external_count() == 0
    intent_path = manager._market_store_path.with_name(
        f"{manager._market_store_path.name}.wallet-ledger"
    )
    intent_path.unlink()

    with pytest.raises(ExternalPoDLEPoolError, match="artifacts are missing"):
        manager.import_external(_external_record())
    store.close()


def test_observed_native_database_replacement_stays_permanently_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    market_path = manager._market_store_path
    snapshot = tmp_path / "seller-before-claim.sqlite"
    snapshot.write_bytes(market_path.read_bytes())
    first_utxo = make_utxo(txid_char="a", address="bcrt1qreplacement-first")
    second_utxo = make_utxo(txid_char="b", address="bcrt1qreplacement-second")
    first_key = b"\x0c" * 32
    second_key = b"\x0d" * 32
    second_proof = generate_podle(second_key, f"{second_utxo.txid}:{second_utxo.vout}", 0)

    assert _generate(manager, [first_utxo], {first_utxo.address: first_key}) is not None
    snapshot.replace(market_path)
    verifier = MarketStore(market_path, wallet_id=WALLET_ID)
    assert verifier.is_available_for_local(second_proof.commitment.hex())

    assert _generate(manager, [second_utxo], {second_utxo.address: second_key}) is None
    with pytest.raises(ExternalPoDLEPoolError, match="previously replaced"):
        manager.import_external(_external_record())
    assert verifier.is_available_for_local(second_proof.commitment.hex())
    verifier.close()
    store.close()


def test_path_mismatch_refuses_native_ledger_fallback(tmp_path: Path) -> None:
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    other_commitments = tmp_path / "other-commitments.json"
    other_commitments.write_text('{"used": [], "external_v1": {}}', encoding="utf-8")
    store = _activate(manager, other_commitments)

    with pytest.raises(ExternalPoDLEPoolError, match="does not match"):
        manager.import_external(_external_record())
    assert not manager.filepath.exists()
    store.close()


def test_candidate_selected_before_activation_is_rechecked_before_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    utxo = make_utxo(txid_char="a", address="bcrt1qactivation-race")
    key = b"\x06" * 32
    proof = generate_podle(key, f"{utxo.txid}:{utxo.vout}", 0)
    record = _record(proof, txid=utxo.txid, vout=utxo.vout)
    original_snapshot = manager._local_selection_exclusions
    stores: list[MarketStore] = []

    def activate_after_legacy_check() -> set[str]:
        exclusions = original_snapshot()
        assert record.commitment not in exclusions
        store = _activate(manager)
        store.add_inventory("podle", record.commitment, record.model_dump(mode="json"))
        stores.append(store)
        return exclusions

    monkeypatch.setattr(manager, "_local_selection_exclusions", activate_after_legacy_check)

    assert _generate(manager, [utxo], {utxo.address: key}) is None
    assert len(stores) == 1
    assert not stores[0].is_available_for_local(record.commitment)
    stores[0].close()


def test_blacklist_marking_claims_native_ledger_before_json_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    utxo = make_utxo(txid_char="a", address="bcrt1qblacklisted")
    proof = generate_podle(b"\x07" * 32, f"{utxo.txid}:{utxo.vout}", 0)
    commitment = proof.commitment.hex()
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _OneBlacklist(commitment))

    assert _generate(manager, [utxo], {utxo.address: b"\x07" * 32}) is None
    assert commitment in manager.used_commitments
    assert not store.is_available_for_local(commitment)
    store.close()


def test_competing_market_and_local_claims_have_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("taker.podle_manager.get_blacklist", lambda: _EmptyBlacklist())
    manager = PoDLEManager(tmp_path, wallet_id=WALLET_ID)
    store = _activate(manager)
    utxo = make_utxo(txid_char="a", address="bcrt1qcompeting")
    key = b"\x08" * 32
    proof = generate_podle(key, f"{utxo.txid}:{utxo.vout}", 0)
    record = _record(proof, txid=utxo.txid, vout=utxo.vout)
    barrier = threading.Barrier(2)
    outcomes: dict[str, bool] = {}

    def claim_local() -> None:
        barrier.wait()
        outcomes["local"] = _generate(manager, [utxo], {utxo.address: key}) is not None

    def add_inventory() -> None:
        barrier.wait()
        try:
            store.add_inventory("podle", record.commitment, record.model_dump(mode="json"))
        except MarketStoreConflictError:
            outcomes["market"] = False
        else:
            outcomes["market"] = True

    threads = [threading.Thread(target=claim_local), threading.Thread(target=add_inventory)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert sum(outcomes.values()) == 1
    store.close()
