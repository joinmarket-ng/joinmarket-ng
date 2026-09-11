from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from bitcointx.core.key import CKey
from jmcore.channel_ring_store import (
    FundingEdgeRecord,
    Outpoint,
    RingChainStatus,
    RingLifecycleState,
    RingParticipantRecord,
    RingParticipantRole,
    RingParticipantStore,
    TransactionPresence,
)
from jmcore.cofunded_ring import (
    BackendLimits,
    ChannelPolicy,
    CoordinatorParticipant,
    LocalContribution,
    ManifestOutput,
    PrivateEdgePlan,
    PrivateParticipant,
    RingEdge,
    RingKeyPair,
    RingManifest,
    RingPlanPayload,
    RingPrivateHelloPayload,
    RingPrivatePlanPayload,
    contact_public_key,
    sign_payload,
)

import jmswap.maintenance as maintenance
from jmswap.channel_ring import InitializedChannelRingBackend
from jmswap.lnd import LndBackend, LndNodeInfo
from jmswap.maintenance import MaintenanceStateError

ROUND_NONCE = "10" * 32
MANIFEST_TXID = "20" * 32
MARKER_ERROR = "Private channel maintenance enrollment requires operator review"


def _secret(index: int) -> bytes:
    return index.to_bytes(32, "big")


def _ring_key(index: int) -> str:
    return RingKeyPair.from_secret(_secret(index)).public_key


def _node_id(index: int) -> str:
    return bytes(CKey.from_secret_bytes(_secret(index)).pub).hex()


def _limits() -> BackendLimits:
    return BackendLimits(
        network="regtest",
        offer_type="tr0absoffer",
        min_channel_capacity=20_000,
        max_channel_capacity=1_000_000,
        max_push_amount=500_000,
        dust_limit=354,
        max_reserve=100_000,
        max_commitment_fee=100_000,
        max_pending_channels=4,
    )


def _policy() -> ChannelPolicy:
    return ChannelPolicy(
        fundee_csv_delay=144,
        fundee_reserve=10_000,
        min_depth=3,
        opener_csv_delay=144,
        opener_reserve=10_000,
    )


def _contact_secret(index: int) -> str:
    return f"{index + 100:064x}"


@dataclass(frozen=True)
class _PlanFixture:
    limits: BackendLimits
    manifest: RingManifest
    private_plan: RingPrivatePlanPayload
    legacy_plan: RingPlanPayload
    incoming_peer: PrivateParticipant
    outgoing_peer: PrivateParticipant
    local_contacts: dict[str, PrivateParticipant]


def _plan_fixture() -> _PlanFixture:
    limits = _limits()
    keys = [_ring_key(index) for index in range(1, 5)]

    def coordinator(index: int) -> CoordinatorParticipant:
        return CoordinatorParticipant(
            participant_key=keys[index - 1],
            encryption_key=contact_public_key(_contact_secret(index)),
            backend_limits=limits,
        )

    def participant(index: int) -> PrivateParticipant:
        return PrivateParticipant(
            participant_key=keys[index - 1],
            node_id=_node_id(index + 20),
            onion_endpoint="a" * 56 + ".onion:9735",
            backend_limits=limits,
        )

    incoming_edge = PrivateEdgePlan(
        edge_id="31" * 32,
        pending_channel_id="41" * 32,
        opener_key=keys[0],
        acceptor_key=keys[1],
        capacity=250_000,
        push_amount=120_000,
        policy=_policy(),
    )
    outgoing_edge = PrivateEdgePlan(
        edge_id="32" * 32,
        pending_channel_id="42" * 32,
        opener_key=keys[1],
        acceptor_key=keys[2],
        capacity=280_000,
        push_amount=180_000,
        policy=_policy(),
    )
    contribution = LocalContribution(
        participant_key=keys[1], residual=300_000, outgoing=180_000, incoming=120_000
    )

    def private_hello(index: int) -> RingPrivateHelloPayload:
        hello = RingPrivateHelloPayload(
            round_nonce=ROUND_NONCE,
            revision=1,
            signer_key=keys[index - 1],
            participant=coordinator(index),
        )
        return cast(RingPrivateHelloPayload, sign_payload(hello, _secret(index)))

    private_plan = RingPrivatePlanPayload(
        round_nonce=ROUND_NONCE,
        revision=1,
        signer_key=keys[0],
        network="regtest",
        offer_type="tr0absoffer",
        cycle_keys=keys,
        position=1,
        contribution=contribution,
        predecessor=coordinator(1),
        successor=coordinator(3),
        incoming_edge=incoming_edge,
        outgoing_edge=outgoing_edge,
        neighbor_hellos=[private_hello(2), private_hello(1), private_hello(3)],
    )
    private_plan = cast(RingPrivatePlanPayload, sign_payload(private_plan, _secret(1)))
    incoming_peer = participant(1)
    outgoing_peer = participant(3)
    legacy_plan = RingPlanPayload(
        round_nonce=ROUND_NONCE,
        revision=1,
        signer_key=keys[0],
        network="regtest",
        offer_type="tr0absoffer",
        cycle_keys=keys,
        position=1,
        contribution=contribution,
        predecessor=incoming_peer,
        successor=outgoing_peer,
        incoming_edge=incoming_edge,
        outgoing_edge=outgoing_edge,
    )
    legacy_plan = cast(RingPlanPayload, sign_payload(legacy_plan, _secret(1)))
    edges = [
        RingEdge(
            opener_key=keys[index],
            acceptor_key=keys[(index + 1) % len(keys)],
            pending_channel_id=f"{index + 40:064x}",
            capacity=250_000 + index,
            output_index=index + 4,
            script_pubkey="5120" + f"{index + 50:064x}",
            policy=_policy(),
        )
        for index in range(len(keys))
    ]
    outputs = [
        ManifestOutput(
            index=index,
            amount=100_000,
            script_pubkey="5120" + f"{index + 60:064x}",
        )
        for index in range(len(keys))
    ] + [
        ManifestOutput(
            index=edge.output_index, amount=edge.capacity, script_pubkey=edge.script_pubkey
        )
        for edge in edges
    ]
    return _PlanFixture(
        limits=limits,
        manifest=RingManifest(
            network="regtest",
            round_nonce=ROUND_NONCE,
            revision=1,
            unsigned_tx_hash="21" * 32,
            unsigned_txid=MANIFEST_TXID,
            participant_keys=keys,
            edges=edges,
            equal_output_indices=[0, 1, 2, 3],
            outputs=outputs,
        ),
        private_plan=private_plan,
        legacy_plan=legacy_plan,
        incoming_peer=incoming_peer,
        outgoing_peer=outgoing_peer,
        local_contacts={
            incoming_peer.participant_key: incoming_peer,
            outgoing_peer.participant_key: outgoing_peer,
        },
    )


class _NoFeeBackend:
    def __init__(self) -> None:
        self.fee_calls: list[str] = []

    async def set_private_channel_zero_fee(self, channel_point: str) -> None:
        self.fee_calls.append(channel_point)
        raise AssertionError("maintenance enrollment must not initialize channel fees")


@dataclass(frozen=True)
class _EnrollmentContext:
    store: RingParticipantStore
    record: RingParticipantRecord
    initialized: InitializedChannelRingBackend
    maintenance_directory: Path
    backend: _NoFeeBackend
    fixture: _PlanFixture


def _confirmed_context(
    tmp_path: Path,
    *,
    plan: RingPrivatePlanPayload | RingPlanPayload | None = None,
    maintenance_directory: Path | None = None,
    with_maintenance_directory: bool = True,
) -> _EnrollmentContext:
    fixture = _plan_fixture()
    selected_plan = fixture.private_plan if plan is None else plan
    directory = tmp_path / "maintenance"
    configured_directory = (
        directory
        if with_maintenance_directory and maintenance_directory is None
        else maintenance_directory
    )
    private = isinstance(selected_plan, RingPrivatePlanPayload)
    record = RingParticipantRecord(
        round_nonce=ROUND_NONCE,
        revision=1,
        ring_secret=_secret(2).hex(),
        ring_public_key=_ring_key(2),
        taker_session_identity="enrollment-test",
        local_role=RingParticipantRole.MAKER,
        local_position=1,
        plan=selected_plan,
        contact_secret=_contact_secret(2) if private else None,
        maintenance_directory=configured_directory,
        local_contacts=fixture.local_contacts if private else {},
        pending_channel_ids=(
            selected_plan.incoming_edge.pending_channel_id,
            selected_plan.outgoing_edge.pending_channel_id,
        ),
        incoming_edge=FundingEdgeRecord(
            plan=selected_plan.incoming_edge,
            peer=fixture.incoming_peer,
            funding_outpoint=Outpoint(txid=fixture.manifest.unsigned_txid, vout=4),
        ),
        outgoing_edge=FundingEdgeRecord(
            plan=selected_plan.outgoing_edge,
            peer=fixture.outgoing_peer,
            funding_outpoint=Outpoint(txid=fixture.manifest.unsigned_txid, vout=5),
        ),
        manifest=fixture.manifest,
        local_input_signature_created=True,
        local_input_signature_sent=True,
        local_signatures=("signature",),
        final_tx="00",
        chain_status=RingChainStatus(
            exact_txid=fixture.manifest.unsigned_txid,
            chain=TransactionPresence.PRESENT,
            confirmations=1,
        ),
        created_at=1.0,
        updated_at=1.0,
        state=RingLifecycleState.CONFIRMED_OPEN,
    )
    store = RingParticipantStore(tmp_path / "rings", max_active_sessions=4, max_verified_sessions=4)
    store.save(record)
    backend = _NoFeeBackend()
    initialized = InitializedChannelRingBackend(
        backend=cast(LndBackend, backend),
        node_info=LndNodeInfo(
            identity_pubkey=_node_id(40),
            version="0.21.1-beta",
            network="regtest",
            synced_to_chain=True,
            wallet_synced=True,
            feature_bits=frozenset({81}),
            advertised_uris=(),
        ),
        onion_endpoint="a" * 56 + ".onion:9735",
        backend_limits=fixture.limits,
    )
    return _EnrollmentContext(
        store=store,
        record=record,
        initialized=initialized,
        maintenance_directory=directory,
        backend=backend,
        fixture=fixture,
    )


def _maintenance_state(directory: Path) -> dict[str, object]:
    return cast(dict[str, object], json.loads((directory / "maintenance-v1.json").read_text()))


def _expected_enrollments(context: _EnrollmentContext) -> dict[str, dict[str, str]]:
    assert context.record.incoming_edge is not None
    assert context.record.outgoing_edge is not None
    assert context.record.manifest is not None
    return {
        str(context.record.incoming_edge.funding_outpoint): {
            "channel_point": str(context.record.incoming_edge.funding_outpoint),
            "node_id": context.initialized.node_info.identity_pubkey,
            "peer_node_id": context.fixture.incoming_peer.node_id,
            "origin_id": context.record.manifest.unsigned_txid,
            "status": "pending_validation",
        },
        str(context.record.outgoing_edge.funding_outpoint): {
            "channel_point": str(context.record.outgoing_edge.funding_outpoint),
            "node_id": context.initialized.node_info.identity_pubkey,
            "peer_node_id": context.fixture.outgoing_peer.node_id,
            "origin_id": context.record.manifest.unsigned_txid,
            "status": "pending_validation",
        },
    }


def test_confirmed_v2_handoff_records_marker_before_exact_idempotent_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _confirmed_context(tmp_path)
    enroll_calls: list[tuple[object, ...]] = []
    original_enroll = maintenance.enroll_channel

    def inspect_marker_then_enroll(*args: object) -> bool:
        current = context.store.load(context.record.key)
        assert current is not None
        assert current.maintenance_enrollment_recorded
        assert context.backend.fee_calls == []
        enroll_calls.append(args)
        return original_enroll(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(maintenance, "enroll_channel", inspect_marker_then_enroll)

    context.initialized.enroll_confirmed_channels(context.store, context.record)

    assert enroll_calls == [
        (
            context.maintenance_directory,
            context.initialized.node_info.identity_pubkey,
            context.fixture.incoming_peer.node_id,
            f"{MANIFEST_TXID}:4",
            MANIFEST_TXID,
        ),
        (
            context.maintenance_directory,
            context.initialized.node_info.identity_pubkey,
            context.fixture.outgoing_peer.node_id,
            f"{MANIFEST_TXID}:5",
            MANIFEST_TXID,
        ),
    ]
    assert _maintenance_state(context.maintenance_directory)[
        "enrollments"
    ] == _expected_enrollments(context)
    current = context.store.load(context.record.key)
    assert current is not None
    assert current.maintenance_enrollment_recorded
    assert context.backend.fee_calls == []

    context.initialized.enroll_confirmed_channels(context.store, context.record)

    assert len(enroll_calls) == 2
    assert context.backend.fee_calls == []


def test_stale_record_cannot_recreate_deleted_maintenance_state(tmp_path: Path) -> None:
    context = _confirmed_context(tmp_path)
    context.initialized.enroll_confirmed_channels(context.store, context.record)
    (context.maintenance_directory / "maintenance-v1.json").unlink()

    with pytest.raises(MaintenanceStateError, match="missing or mismatched"):
        context.initialized.enroll_confirmed_channels(context.store, context.record)

    current = context.store.load(context.record.key)
    assert current is not None
    assert current.maintenance_enrollment_recorded
    assert current.retry.last_error == MARKER_ERROR
    assert not (context.maintenance_directory / "maintenance-v1.json").exists()
    assert context.backend.fee_calls == []


def test_interrupted_handoff_persists_marker_and_never_writes_missing_edge_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _confirmed_context(tmp_path)
    original_enroll = maintenance.enroll_channel
    enroll_calls: list[tuple[object, ...]] = []

    def interrupt_second_enrollment(*args: object) -> bool:
        enroll_calls.append(args)
        if len(enroll_calls) == 2:
            raise OSError("simulated interruption")
        return original_enroll(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(maintenance, "enroll_channel", interrupt_second_enrollment)

    with pytest.raises(OSError, match="simulated interruption"):
        context.initialized.enroll_confirmed_channels(context.store, context.record)

    current = context.store.load(context.record.key)
    assert current is not None
    assert current.maintenance_enrollment_recorded
    assert current.retry.last_error == MARKER_ERROR
    assert _maintenance_state(context.maintenance_directory)["enrollments"] == {
        f"{MANIFEST_TXID}:4": _expected_enrollments(context)[f"{MANIFEST_TXID}:4"]
    }

    with pytest.raises(MaintenanceStateError, match="missing or mismatched"):
        context.initialized.enroll_confirmed_channels(context.store, context.record)

    assert len(enroll_calls) == 2
    assert _maintenance_state(context.maintenance_directory)["enrollments"] == {
        f"{MANIFEST_TXID}:4": _expected_enrollments(context)[f"{MANIFEST_TXID}:4"]
    }
    assert context.backend.fee_calls == []


def test_marker_write_failure_cannot_call_enrollment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _confirmed_context(tmp_path)
    original_transition = context.store.transition
    enroll_calls: list[tuple[object, ...]] = []

    def fail_marker_write(*args: object, **kwargs: object) -> RingParticipantRecord:
        if kwargs.get("updates") == {"maintenance_enrollment_recorded": True}:
            raise OSError("simulated marker write failure")
        return original_transition(*args, **kwargs)  # type: ignore[arg-type]

    def record_enroll(*args: object) -> bool:
        enroll_calls.append(args)
        return True

    monkeypatch.setattr(context.store, "transition", fail_marker_write)
    monkeypatch.setattr(maintenance, "enroll_channel", record_enroll)

    with pytest.raises(OSError, match="simulated marker write failure"):
        context.initialized.enroll_confirmed_channels(context.store, context.record)

    current = context.store.load(context.record.key)
    assert current is not None
    assert not current.maintenance_enrollment_recorded
    assert current.retry.last_error == MARKER_ERROR
    assert enroll_calls == []
    assert not (context.maintenance_directory / "maintenance-v1.json").exists()
    assert context.backend.fee_calls == []


@pytest.mark.parametrize("case", ["legacy_v1", "v2_without_directory", "serialized_upgrade"])
def test_nonaffirmative_or_legacy_records_do_not_enroll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    fixture = _plan_fixture()
    if case == "legacy_v1":
        context = _confirmed_context(tmp_path, plan=fixture.legacy_plan)
    else:
        context = _confirmed_context(tmp_path, with_maintenance_directory=False)
    if case == "serialized_upgrade":
        record_path = context.store.directory / context.record.key.filename
        document = json.loads(record_path.read_text())
        document.pop("maintenance_directory")
        document.pop("maintenance_enrollment_recorded")
        record_path.write_text(json.dumps(document), encoding="ascii")

    enroll_calls: list[tuple[object, ...]] = []

    def record_enroll(*args: object) -> bool:
        enroll_calls.append(args)
        return True

    monkeypatch.setattr(maintenance, "enroll_channel", record_enroll)
    current = context.store.load(context.record.key)
    assert current is not None

    context.initialized.enroll_confirmed_channels(context.store, current)

    assert enroll_calls == []
    assert not (context.maintenance_directory / "maintenance-v1.json").exists()
    assert context.backend.fee_calls == []
    if case == "serialized_upgrade":
        assert not current.maintenance_enrollment_recorded
        assert current.maintenance_directory is None


@pytest.mark.parametrize("case", ["corrupt", "mismatched"])
def test_recorded_marker_fails_closed_for_corrupt_or_mismatched_maintenance_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    context = _confirmed_context(tmp_path)
    context.initialized.enroll_confirmed_channels(context.store, context.record)
    state_path = context.maintenance_directory / "maintenance-v1.json"
    if case == "corrupt":
        state_path.write_text("{", encoding="ascii")
        error = "absent, legacy, partial, or corrupt"
    else:
        state = _maintenance_state(context.maintenance_directory)
        enrollments = cast(dict[str, dict[str, str]], state["enrollments"])
        enrollments[f"{MANIFEST_TXID}:4"]["peer_node_id"] = _node_id(50)
        state_path.write_text(json.dumps(state), encoding="ascii")
        error = "missing or mismatched"

    enroll_calls: list[tuple[object, ...]] = []

    def record_enroll(*args: object) -> bool:
        enroll_calls.append(args)
        return True

    monkeypatch.setattr(maintenance, "enroll_channel", record_enroll)

    with pytest.raises(MaintenanceStateError, match=error):
        context.initialized.enroll_confirmed_channels(context.store, context.record)

    current = context.store.load(context.record.key)
    assert current is not None
    assert current.maintenance_enrollment_recorded
    assert current.retry.last_error == MARKER_ERROR
    assert enroll_calls == []
    assert context.backend.fee_calls == []
    if case == "corrupt":
        assert state_path.read_text() == "{"
    else:
        assert _maintenance_state(context.maintenance_directory)[
            "enrollments"
        ] != _expected_enrollments(context)
