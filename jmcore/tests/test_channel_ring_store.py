from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from jmcore.channel_ring import RingNodeBinding
from jmcore.channel_ring_store import (
    Outpoint,
    RingAntiGriefError,
    RingChainStatus,
    RingLifecycleState,
    RingParticipantRecord,
    RingParticipantRole,
    RingParticipantStore,
    RingRetirementAction,
    RingRetryData,
    RingStoreError,
    RingTransitionError,
    TransactionPresence,
)
from jmcore.cofunded_ring import RingKeyPair

SECRET = bytes.fromhex("11" * 32)
PUBLIC = RingKeyPair.from_secret(SECRET).public_key
OUTPOINT = Outpoint(txid="22" * 32, vout=1)


def record(
    *,
    revision: int = 0,
    state: RingLifecycleState = RingLifecycleState.INVITED,
    taker: str = "taker-session",
    outpoint: Outpoint = OUTPOINT,
) -> RingParticipantRecord:
    values: dict[str, object] = {
        "journal_kind": "private_channel_ring",
        "node_binding": RingNodeBinding(
            network="regtest",
            wallet_identity="a" * 64,
            source_mixdepth=0,
            node_name="md0",
            local_node_id="02" + PUBLIC,
        ),
        "round_nonce": "33" * 32,
        "revision": revision,
        "ring_secret": SECRET.hex(),
        "ring_public_key": PUBLIC,
        "taker_session_identity": taker,
        "local_role": RingParticipantRole.MAKER,
        "local_position": 1,
        "local_input_outpoints": (outpoint,),
        "created_at": 1.0,
        "updated_at": 1.0,
        "state": state,
    }
    if state in {
        RingLifecycleState.SIGNED,
        RingLifecycleState.BROADCAST,
        RingLifecycleState.CONFIRMED_OPEN,
    }:
        values.update(
            local_input_signature_created=True,
            local_input_signature_sent=True,
            local_signatures=("signature",),
            final_tx="00",
        )
    if state is RingLifecycleState.CONFIRMED_OPEN:
        values["chain_status"] = RingChainStatus(
            exact_txid="44" * 32,
            chain=TransactionPresence.PRESENT,
            confirmations=1,
        )
    if state is RingLifecycleState.CONFLICTED:
        values["chain_status"] = RingChainStatus(
            confirmed_conflict_txid="44" * 32,
            conflict_confirmations=1,
        )
    return RingParticipantRecord.model_validate(values)


ALLOWED = {
    RingLifecycleState.INVITED: {
        RingLifecycleState.PLANNED,
        RingLifecycleState.RETIRING,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.PLANNED: {
        RingLifecycleState.ACCEPTOR_ARMED,
        RingLifecycleState.RETIRING,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.ACCEPTOR_ARMED: {
        RingLifecycleState.PREPARED_NOT_VERIFIED,
        RingLifecycleState.RETIRING,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.PREPARED: {
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.PREPARED_NOT_VERIFIED: {
        RingLifecycleState.VERIFYING,
        RingLifecycleState.RETIRING,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.VERIFYING: {
        RingLifecycleState.PSBT_VERIFIED,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.PSBT_VERIFIED: {
        RingLifecycleState.READY,
        RingLifecycleState.RETIRING,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.READY: {
        RingLifecycleState.SIGNING,
        RingLifecycleState.RETIRING,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.SIGNING: {
        RingLifecycleState.SIGNED,
        RingLifecycleState.RETIRING,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.SIGNED: {
        RingLifecycleState.BROADCAST,
        RingLifecycleState.CONFIRMED_OPEN,
        RingLifecycleState.CONFLICTED,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.BROADCAST: {
        RingLifecycleState.CONFIRMED_OPEN,
        RingLifecycleState.CONFLICTED,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.CONFIRMED_OPEN: {RingLifecycleState.RETIRING, RingLifecycleState.RETIRED},
    RingLifecycleState.RETIRING: {RingLifecycleState.RETIRED, RingLifecycleState.RECOVERY_REQUIRED},
    RingLifecycleState.RETIRED: set(),
    RingLifecycleState.CONFLICTED: {
        RingLifecycleState.RETIRING,
        RingLifecycleState.RECOVERY_REQUIRED,
    },
    RingLifecycleState.RECOVERY_REQUIRED: {
        RingLifecycleState.CONFIRMED_OPEN,
        RingLifecycleState.CONFLICTED,
        RingLifecycleState.RETIRING,
    },
}


def test_node_binding_cannot_change_through_transition_or_save(tmp_path: Path) -> None:
    original = record()
    replacement = original.node_binding.model_copy(update={"source_mixdepth": 1})
    store = RingParticipantStore(tmp_path, max_active_sessions=4, max_verified_sessions=2)
    store.save(original)
    before = (tmp_path / original.key.filename).read_bytes()
    with pytest.raises(RingTransitionError, match="identity fields"):
        store.transition(original.key, original.state, updates={"node_binding": replacement})
    with pytest.raises(RingStoreError, match="ownership"):
        store.save(original.model_copy(update={"node_binding": replacement}))
    assert (tmp_path / original.key.filename).read_bytes() == before


@pytest.mark.parametrize("source", list(RingLifecycleState))
@pytest.mark.parametrize("target", list(RingLifecycleState))
def test_complete_transition_matrix(source: RingLifecycleState, target: RingLifecycleState) -> None:
    # Verification intent has mandatory transaction evidence; this test only
    # exercises the transition graph, not record validation.
    source_record = (
        record(state=RingLifecycleState.PREPARED).model_copy(update={"state": source})
        if source is RingLifecycleState.VERIFYING
        else record(state=source)
    )
    assert source_record.can_transition_to(target) is (
        target == source or target in ALLOWED[source]
    )


def test_interrupted_verification_cannot_cancel_a_consumed_shim() -> None:
    prepared = record(state=RingLifecycleState.PREPARED_NOT_VERIFIED)
    assert prepared.can_transition_to(RingLifecycleState.VERIFYING)
    assert not prepared.can_transition_to(RingLifecycleState.PSBT_VERIFIED)
    intent = prepared.model_copy(update={"state": RingLifecycleState.VERIFYING})
    assert intent.verified_unresolved
    assert intent.retirement_action() is RingRetirementAction.BLOCKED
    assert not intent.can_transition_to(RingLifecycleState.RETIRING)
    with pytest.raises(ValueError, match="exact unsigned funding evidence"):
        RingParticipantRecord.model_validate(intent.model_dump())

    legacy = record(state=RingLifecycleState.PREPARED)
    assert legacy.verified_unresolved
    assert legacy.retirement_action() is RingRetirementAction.BLOCKED
    assert not legacy.can_transition_to(RingLifecycleState.RETIRING)


def test_retained_legacy_prepared_record_loads_but_cannot_shim_cancel(tmp_path: Path) -> None:
    store = RingParticipantStore(tmp_path, max_active_sessions=4, max_verified_sessions=2)
    legacy = record(state=RingLifecycleState.PREPARED)
    store.save(legacy)
    original = (tmp_path / legacy.key.filename).read_bytes()
    assert store.load(legacy.key) == legacy
    with pytest.raises(RingTransitionError, match="not allowed"):
        store.transition(legacy.key, RingLifecycleState.RETIRING)
    assert (tmp_path / legacy.key.filename).read_bytes() == original


def test_preverified_retirement_and_verified_abandon_authorization() -> None:
    prepared = record(state=RingLifecycleState.PREPARED_NOT_VERIFIED)
    assert prepared.retirement_action() is RingRetirementAction.SHIM_CANCEL
    assert (
        prepared.transition(RingLifecycleState.RETIRING, now=2.0).state
        is RingLifecycleState.RETIRING
    )

    verified = record(state=RingLifecycleState.PSBT_VERIFIED)
    absent = RingChainStatus(
        exact_txid="55" * 32,
        mempool=TransactionPresence.ABSENT,
        chain=TransactionPresence.ABSENT,
    )
    verified = RingParticipantRecord(**{**verified.model_dump(), "chain_status": absent})
    assert verified.retirement_action() is RingRetirementAction.ABANDON_VERIFIED
    retired = verified.transition(
        RingLifecycleState.RETIRING,
        now=2.0,
    )
    assert retired.state is RingLifecycleState.RETIRING


@pytest.mark.parametrize(
    "updates",
    [
        {"local_input_signature_created": True},
        {
            "chain_status": RingChainStatus(
                exact_txid="55" * 32,
                mempool=TransactionPresence.UNKNOWN,
                chain=TransactionPresence.ABSENT,
            )
        },
        {
            "chain_status": RingChainStatus(
                exact_txid="55" * 32,
                mempool=TransactionPresence.ABSENT,
                chain=TransactionPresence.PRESENT,
            )
        },
    ],
)
def test_verified_retirement_rejects_unsafe_authorization(updates: dict[str, object]) -> None:
    with pytest.raises(RingTransitionError):
        record(state=RingLifecycleState.PSBT_VERIFIED).transition(
            RingLifecycleState.RETIRING, updates=updates
        )


def test_signed_state_cannot_retire_until_confirmed_conflict() -> None:
    signed = record(state=RingLifecycleState.SIGNED)
    assert signed.retirement_action() is RingRetirementAction.BLOCKED
    with pytest.raises(RingTransitionError, match="not allowed"):
        signed.transition(RingLifecycleState.RETIRING)
    conflicted = signed.transition(
        RingLifecycleState.CONFLICTED,
        updates={
            "chain_status": RingChainStatus(
                confirmed_conflict_txid="66" * 32,
                conflict_confirmations=1,
            ),
        },
    )
    assert conflicted.transition(RingLifecycleState.RETIRING).state is RingLifecycleState.RETIRING


def test_recovery_retirement_requires_point_of_no_return_evidence() -> None:
    unknown = record(state=RingLifecycleState.RECOVERY_REQUIRED)
    # An older PREPARED record may enter recovery with no persisted unsigned
    # transaction even after LND consumed its shim. Missing evidence is not
    # evidence that a cancellation is safe.
    assert unknown.retirement_action() is RingRetirementAction.BLOCKED
    with pytest.raises(RingTransitionError, match="safe retirement evidence"):
        unknown.transition(RingLifecycleState.RETIRING)

    signed_values = record(state=RingLifecycleState.RECOVERY_REQUIRED).model_dump()
    signed_values.update(
        local_input_signature_created=True,
        local_input_signature_sent=True,
        final_tx="00",
    )
    signed_recovery = RingParticipantRecord(**signed_values)
    assert signed_recovery.retirement_action() is RingRetirementAction.BLOCKED
    with pytest.raises(RingTransitionError, match="safe retirement evidence"):
        signed_recovery.transition(RingLifecycleState.RETIRING)


def test_signature_sent_requires_created_and_signed_requires_final_tx() -> None:
    values = record().model_dump()
    values["local_input_signature_sent"] = True
    with pytest.raises(ValueError, match="before it is created"):
        RingParticipantRecord(**values)
    values = record().model_dump()
    values["state"] = RingLifecycleState.SIGNED
    with pytest.raises(ValueError, match="signature to be sent"):
        RingParticipantRecord(**values)


def test_permissions_atomic_persistence_and_restart_state(tmp_path: Path) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=2)
    durable = record(state=RingLifecycleState.BROADCAST)
    store.save(durable)
    path = store.directory / durable.key.filename
    assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load(durable.key) == durable
    restarted = RingParticipantStore(
        store.directory, max_active_sessions=4, max_verified_sessions=2
    )
    assert restarted.load(durable.key) == durable


def test_previous_complete_journal_loads_without_retirement_intent(tmp_path: Path) -> None:
    store = RingParticipantStore(tmp_path, max_active_sessions=4, max_verified_sessions=2)
    previous = record(state=RingLifecycleState.SIGNING)
    payload = previous.model_dump(mode="json")
    del payload["unsigned_retirement_pending"]
    path = tmp_path / previous.key.filename
    path.write_text(json.dumps(payload), encoding="ascii")
    path.chmod(0o600)
    original = path.read_bytes()

    loaded = store.load(previous.key)

    assert loaded == previous
    assert loaded is not None and not loaded.unsigned_retirement_pending
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "missing",
    [
        "state",
        "local_input_signature_created",
        "local_input_signature_sent",
        "local_signatures",
        "final_tx",
    ],
)
def test_previous_partial_journal_is_not_upgraded(tmp_path: Path, missing: str) -> None:
    store = RingParticipantStore(tmp_path, max_active_sessions=4, max_verified_sessions=2)
    previous = record(state=RingLifecycleState.SIGNING)
    payload = previous.model_dump(mode="json")
    del payload["unsigned_retirement_pending"]
    del payload[missing]
    path = tmp_path / previous.key.filename
    path.write_text(json.dumps(payload), encoding="ascii")
    path.chmod(0o600)
    original = path.read_bytes()

    assert store.load_all().corruptions
    with pytest.raises(RingStoreError):
        store.transition(previous.key, RingLifecycleState.RETIRING)
    assert path.read_bytes() == original


def test_atomic_replace_failure_preserves_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=2)
    original = record()
    store.save(original)
    changed = RingParticipantRecord(
        **{
            **original.model_dump(),
            "retry": RingRetryData(attempts=1, last_error="retry"),
            "updated_at": 2.0,
        }
    )

    def fail_replace(_source: object, _target: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated crash"):
        store.save(changed)
    assert store.load(original.key) == original
    assert not list(store.directory.glob("*.tmp"))


def test_corruption_is_reported_not_deleted_and_blocks_updates(tmp_path: Path) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=2)
    bad = store.directory / "corrupt.json"
    bad.write_text("{not-json", encoding="ascii")
    bad.chmod(0o600)
    report = store.load_all()
    assert report.records == ()
    assert len(report.corruptions) == 1
    assert bad.exists()
    with pytest.raises(RingStoreError, match="corrupt records"):
        store.save(record())


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_kind",
        "wrong_kind",
        "unknown_key",
    ],
)
def test_wrong_journal_envelope_is_preserved_and_blocks_writes(
    tmp_path: Path, mutation: str
) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=2)
    durable = record()
    path = store.directory / durable.key.filename
    payload = durable.model_dump(mode="json")
    if mutation == "missing_kind":
        del payload["journal_kind"]
    elif mutation == "wrong_kind":
        payload["journal_kind"] = "old_channel_ring"
    elif mutation == "unknown_key":
        payload["unexpected"] = True
    else:  # pragma: no cover
        raise AssertionError(mutation)
    source = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    path.write_text(source, encoding="ascii")
    path.chmod(0o600)

    report = store.load_all()

    assert report.records == ()
    assert len(report.corruptions) == 1
    assert path.read_text(encoding="ascii") == source
    with pytest.raises(RingStoreError, match="corrupt records"):
        store.save(record(revision=1))
    assert path.read_text(encoding="ascii") == source


def test_unsafe_file_mode_and_symlink_are_rejected(tmp_path: Path) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=2)
    durable = record()
    store.save(durable)
    path = store.directory / durable.key.filename
    path.chmod(0o644)
    with pytest.raises(RingStoreError, match="unsafe mode"):
        store.load(durable.key)

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(RingStoreError, match="symlink"):
        RingParticipantStore(link, max_active_sessions=1, max_verified_sessions=1)


@pytest.mark.parametrize(
    "missing_field", ["state", "local_input_signature_created", "local_input_signature_sent"]
)
def test_missing_durable_signing_state_is_not_defaulted_to_unsigned(
    tmp_path: Path, missing_field: str
) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=2)
    durable = record(state=RingLifecycleState.RECOVERY_REQUIRED)
    store.save(durable)
    path = store.directory / durable.key.filename
    payload = json.loads(path.read_text())
    del payload[missing_field]
    source = json.dumps(payload)
    path.write_text(source)

    reopened = RingParticipantStore(store.directory, max_active_sessions=4, max_verified_sessions=2)
    with pytest.raises(RingStoreError, match="strict validation"):
        reopened.load(durable.key)
    report = reopened.load_all()
    assert report.records == ()
    assert len(report.corruptions) == 1
    assert path.read_text() == source
    with pytest.raises(RingStoreError, match="corrupt records"):
        reopened.save(record(revision=1))
    assert path.read_text() == source


def test_secret_is_persisted_only_in_private_record_and_redacted_from_repr(tmp_path: Path) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=2)
    durable = record()
    store.save(durable)
    assert SECRET.hex() not in repr(durable)
    raw = (store.directory / durable.key.filename).read_text(encoding="ascii")
    assert json.loads(raw)["ring_secret"] == SECRET.hex()
    assert "macaroon" not in raw and "tls_cert" not in raw


def test_queries_limits_inputs_and_second_verified_revision(tmp_path: Path) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=2, max_verified_sessions=1)
    first = record(taker="first")
    store.save(first)
    assert store.active_records(taker_session_identity="first") == (first,)
    assert store.active_records(input_outpoint=OUTPOINT) == (first,)
    with pytest.raises(RingAntiGriefError, match="input outpoint"):
        store.save(record(revision=1, taker="second"))

    other = record(
        revision=1,
        taker="second",
        outpoint=Outpoint(txid="77" * 32, vout=0),
    )
    store.save(other)
    with pytest.raises(RingAntiGriefError, match="maximum active"):
        store.save(
            record(
                revision=2,
                taker="third",
                outpoint=Outpoint(txid="88" * 32, vout=0),
            )
        )

    verified_store = RingParticipantStore(
        tmp_path / "verified", max_active_sessions=4, max_verified_sessions=1
    )
    verified = record(state=RingLifecycleState.PSBT_VERIFIED, taker="same-round")
    verified_store.save(verified)
    with pytest.raises(RingAntiGriefError, match="maximum verified"):
        verified_store.save(
            record(
                revision=1,
                state=RingLifecycleState.PSBT_VERIFIED,
                taker="same-round",
                outpoint=Outpoint(txid="99" * 32, vout=0),
            )
        )


def test_no_second_revision_while_verified_unresolved_even_below_limit(tmp_path: Path) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=4)
    store.save(record(state=RingLifecycleState.READY, taker="same-round"))
    with pytest.raises(RingAntiGriefError, match="another revision"):
        store.save(
            record(
                revision=1,
                taker="same-round",
                outpoint=Outpoint(txid="aa" * 32, vout=0),
            )
        )


def test_reconcile_is_idempotent_and_rejects_divergence(tmp_path: Path) -> None:
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=2)
    recovered = record(state=RingLifecycleState.RECOVERY_REQUIRED)
    assert store.reconcile([recovered]).records == (recovered,)
    assert store.reconcile([recovered]).records == (recovered,)
    divergent = RingParticipantRecord(
        **{**recovered.model_dump(), "retry": RingRetryData(attempts=1)}
    )
    with pytest.raises(RingStoreError, match="conflicts"):
        store.reconcile([divergent])
