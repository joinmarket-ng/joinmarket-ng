"""Crash-safe taker coordination records without a fabricated LND participant."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from jmcore.channel_ring import RingNodeBinding
from jmcore.channel_ring_store import (
    Outpoint,
    RingParticipantRecord,
    RingParticipantRole,
    RingParticipantStore,
)
from jmcore.cofunded_ring import RingKeyPair
from pydantic import ValidationError

from taker.channel_ring_coordinator_store import (
    CoordinatorPhase,
    CoordinatorRecord,
    CoordinatorStore,
    CoordinatorStoreError,
)


def coordinator_store(root: Path) -> CoordinatorStore:
    return CoordinatorStore(root, network="regtest", wallet_identity="44" * 32)


def fresh() -> CoordinatorRecord:
    return CoordinatorRecord.fresh(
        signer=RingKeyPair.from_secret(bytes.fromhex("11" * 32)),
        round_nonce="aa" * 32,
        network="regtest",
        wallet_identity="44" * 32,
        session_identity="isolated-round",
        source_mixdepth=0,
        input_outpoints=(Outpoint(txid="22" * 32, vout=1),),
        input_lock_owner="wallet-reservation",
        reached_nicks=("maker-1", "maker-2", "maker-3"),
    )


def test_fresh_coordinator_journal_is_private_separate_and_round_trips(tmp_path: Path) -> None:
    store = coordinator_store(tmp_path)
    record = fresh()
    store.save(record)
    assert store.load_all() == (record,)
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.directory / record.filename).stat().st_mode) == 0o600
    assert not (tmp_path / record.filename).exists()
    # Existing participant journals scan only their own directory and schema.
    participants = RingParticipantStore(tmp_path, max_active_sessions=4, max_verified_sessions=2)
    assert participants.load_all().records == ()
    assert participants.load_all().corruptions == ()


def test_interrupted_or_unknown_coordinator_record_blocks_all_writes(tmp_path: Path) -> None:
    store = coordinator_store(tmp_path)
    record = fresh()
    store.save(record)
    path = store.directory / record.filename
    original = path.read_bytes()
    payload = json.loads(original)
    payload["unknown_future_field"] = True
    path.write_text(json.dumps(payload))
    with pytest.raises(CoordinatorStoreError, match="strict validation"):
        store.load_all()
    with pytest.raises(CoordinatorStoreError, match="strict validation"):
        store.save(record)
    assert path.read_text() == json.dumps(payload)

    path.write_bytes(original)
    # A truncated .json is not silently discarded or re-created.
    path.write_bytes(b'{"journal_kind":')
    with pytest.raises(CoordinatorStoreError, match="strict validation"):
        store.load_all()
    assert path.read_bytes() == b'{"journal_kind":'


def test_duplicate_json_keys_and_unsafe_permissions_are_rejected(tmp_path: Path) -> None:
    store = coordinator_store(tmp_path)
    record = fresh()
    store.save(record)
    path = store.directory / record.filename
    original = path.read_bytes()
    path.write_bytes(original[:-1] + b',"phase":"inviting"}')
    with pytest.raises(CoordinatorStoreError, match="strict validation"):
        store.load_all()
    path.write_bytes(original)
    path.chmod(0o644)
    with pytest.raises(CoordinatorStoreError, match="unsafe file permissions"):
        store.load_all()


def test_unknown_or_partial_identity_cannot_authorize_signing(tmp_path: Path) -> None:
    store = coordinator_store(tmp_path)
    record = fresh()
    with pytest.raises(ValidationError, match="does not match"):
        CoordinatorRecord.model_validate({**record.model_dump(), "signer_key": "33" * 32})
    store.save(record)
    with pytest.raises(ValidationError, match="three authenticated channel makers"):
        store.transition(record.filename, CoordinatorPhase.PLANNING)
    assert store.load_all() == (record,)
    with pytest.raises(CoordinatorStoreError, match="identity cannot change"):
        record.transition(CoordinatorPhase.PLANNING, input_lock_owner="different")


def test_signing_intent_cannot_be_rolled_back(tmp_path: Path) -> None:
    store = coordinator_store(tmp_path)
    record = fresh().model_copy(update={"sign_authorization_sent": True})
    store.save(record)
    with pytest.raises(CoordinatorStoreError, match="cannot erase signing intent"):
        store.save(record.model_copy(update={"sign_authorization_sent": False}))
    assert store.load_all() == (record,)


def test_failed_atomic_replace_preserves_previous_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = coordinator_store(tmp_path)
    record = fresh()
    store.save(record)
    original = (store.directory / record.filename).read_bytes()

    def fail_replace(*args: object) -> None:
        del args
        raise OSError("simulated interrupted replace")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="interrupted replace"):
        store.save(record.transition(CoordinatorPhase.INVITING))
    assert (store.directory / record.filename).read_bytes() == original
    assert list(store.directory.glob("*.tmp")) == []


def test_symlink_coordinator_directory_is_not_accepted(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "coordinators").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CoordinatorStoreError, match="symlink"):
        coordinator_store(tmp_path)


def test_coordinator_capacity_counts_retained_participant_journals(tmp_path: Path) -> None:
    participant_store = RingParticipantStore(
        tmp_path, max_active_sessions=2, max_verified_sessions=1
    )
    participant_store.save(
        RingParticipantRecord.fresh(
            round_nonce="cc" * 32,
            revision=0,
            node_binding=RingNodeBinding(
                network="regtest",
                wallet_identity="44" * 32,
                source_mixdepth=0,
                node_name="md0",
                local_node_id="02" + RingKeyPair.from_secret(bytes.fromhex("12" * 32)).public_key,
            ),
            taker_session_identity="retained-participant",
            local_role=RingParticipantRole.TAKER,
            local_position=0,
        )
    )
    store = CoordinatorStore(
        tmp_path,
        network="regtest",
        wallet_identity="44" * 32,
        participant_store=participant_store,
        max_active_sessions=2,
        max_verified_sessions=1,
    )
    record = fresh()
    store.save(record)
    with pytest.raises(CoordinatorStoreError, match="active-session capacity"):
        store.save(
            record.model_copy(
                update={
                    "round_nonce": "bb" * 32,
                    "input_outpoints": (Outpoint(txid="55" * 32, vout=0),),
                    "input_lock_owner": "another-reservation",
                }
            )
        )


def test_wrong_wallet_or_network_blocks_loading_and_new_writes(tmp_path: Path) -> None:
    store = coordinator_store(tmp_path)
    record = fresh()
    store.save(record)
    alien = CoordinatorStore(tmp_path, network="signet", wallet_identity="44" * 32)
    with pytest.raises(CoordinatorStoreError, match="different wallet or network"):
        alien.load_all()
    with pytest.raises(CoordinatorStoreError, match="different wallet or network"):
        alien.save(record)


def test_two_active_coordinators_cannot_claim_the_same_input(tmp_path: Path) -> None:
    store = coordinator_store(tmp_path)
    store.save(fresh())
    other = RingKeyPair.from_secret(bytes.fromhex("13" * 32))
    overlapping = fresh().model_copy(
        update={
            "round_nonce": "bb" * 32,
            "signer_secret": other.secret_key.hex(),
            "signer_key": other.public_key,
        }
    )
    with pytest.raises(CoordinatorStoreError, match="claimed by another active ring"):
        store.save(overlapping)


def test_coordinator_cannot_claim_active_participant_input(tmp_path: Path) -> None:
    participant_store = RingParticipantStore(
        tmp_path, max_active_sessions=4, max_verified_sessions=2
    )
    participant_store.save(
        RingParticipantRecord.fresh(
            round_nonce="cc" * 32,
            revision=0,
            node_binding=RingNodeBinding(
                network="regtest",
                wallet_identity="44" * 32,
                source_mixdepth=0,
                node_name="md0",
                local_node_id="02" + RingKeyPair.from_secret(bytes.fromhex("12" * 32)).public_key,
            ),
            taker_session_identity="retained-participant",
            local_role=RingParticipantRole.TAKER,
            local_position=0,
            local_input_outpoints=fresh().input_outpoints,
            input_lock_owner="old-owner",
        )
    )
    store = CoordinatorStore(
        tmp_path,
        network="regtest",
        wallet_identity="44" * 32,
        participant_store=participant_store,
    )
    with pytest.raises(CoordinatorStoreError, match="claimed by another active ring"):
        store.save(fresh())
