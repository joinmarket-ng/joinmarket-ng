"""Tests for maker-owned channel-input startup recovery."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import cast

import pytest
from jmcore.bitcoin import get_txid
from jmcore.channel_input import (
    ChannelInputCancelPayload,
    ChannelInputKeyPair,
    ChannelInputStatusPayload,
    ChannelInputStatusRequestPayload,
    ChannelInputValidationError,
    Outpoint,
    encode_channel_input_message,
    sign_payload,
)
from jmswap.lnd import (
    ChannelInputCandidate,
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
    WitnessUtxo,
)
from test_maker_channel_input_negotiation import (
    _approval_set,
    _cancel,
    _candidate,
    _finalize_intent,
    _finalized_result,
    _negotiation,
    _node,
    _peer_prepared,
    _plan_freeze,
    _prepare,
    _prepared_negotiation,
    _pretranscript_cancel,
    _proposal,
    _proposal_approved,
    _sign_intent,
    _signing_negotiation,
    _status_request,
)

from maker.channel_input import (
    MakerChannelInputNegotiation,
    MakerChannelInputStage,
    MakerFrozenChannelInput,
)
from maker.channel_input_runtime import MakerChannelInputRegistry, MakerChannelInputRuntimeError
from maker.channel_input_store import MakerChannelInputStore


class FakeBackend:
    def __init__(self) -> None:
        self.statuses: dict[tuple[str, EscrowOutpoint], LndEscrowStatus | None] = {}
        self.frozen: dict[str, FrozenChannelSnapshot] = {}
        self.attempts: dict[EscrowOutpoint, LndEscrowSigningAttempt] = {}
        self.partials: dict[EscrowOutpoint, bytes] = {}
        self.finalized: dict[EscrowOutpoint, LndFinalizedEscrowParent] = {}
        self.events: list[str] = []

    async def find_channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus | None:
        self.events.append(f"find:{funding_outpoint.txid}:{funding_outpoint.vout}")
        return self.statuses.get((session_id, funding_outpoint))

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool:
        self.events.append(f"cancel:{funding_outpoint.txid}:{funding_outpoint.vout}")
        self.statuses[(session_id, funding_outpoint)] = None
        return True

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> FrozenChannelSnapshot:
        self.events.append(f"freeze:{channel_id}")
        return self.frozen[channel_id]

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo],
        channel_input_index: int,
    ) -> str:
        self.events.append(f"prepare:{funding_outpoint.txid}:{channel_input_index}")
        assert all(isinstance(item, WitnessUtxo) for item in input_utxos)
        return get_txid(parent_tx_hex)

    async def begin_channel_input_signing(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        *,
        restart_attempt: bool = False,
    ) -> LndEscrowSigningAttempt:
        self.events.append(f"begin:{funding_outpoint.txid}:{restart_attempt}")
        return self.attempts[funding_outpoint]

    async def recover_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        self.events.append(f"recover:{funding_outpoint.txid}")
        assert len(remote_partial_signature) == 32
        return self.finalized[funding_outpoint]

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes:
        self.events.append(f"sign:{funding_outpoint.txid}:{attempt.attempt_id.hex()}")
        assert len(remote_public_nonce) == 66
        assert self.attempts[funding_outpoint] == attempt
        return self.partials[funding_outpoint]

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        self.events.append(f"finalize:{funding_outpoint.txid}:{attempt.attempt_id.hex()}")
        assert len(remote_partial_signature) == 32
        assert self.attempts[funding_outpoint] == attempt
        return self.finalized[funding_outpoint]


def _store(tmp_path: Path, negotiation: MakerChannelInputNegotiation) -> MakerChannelInputStore:
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=10)
    store.save(negotiation.snapshot())
    return store


def _registry(
    tmp_path: Path, negotiation: MakerChannelInputNegotiation
) -> MakerChannelInputRegistry:
    return MakerChannelInputRegistry.load(
        store=_store(tmp_path, negotiation), config=negotiation.config, network=negotiation.network
    )


def _escrow(outpoint: Outpoint) -> EscrowOutpoint:
    return EscrowOutpoint(txid=outpoint.txid, vout=outpoint.vout)


def _status(
    stage: LndEscrowStage,
    *,
    parent_txid: str | None = None,
    attempt: LndEscrowSigningAttempt | None = None,
    local_nonce: bytes | None = None,
    remote_nonce: bytes | None = None,
    partial: bytes | None = None,
    finalized: LndFinalizedEscrowParent | None = None,
) -> LndEscrowStatus:
    return LndEscrowStatus(
        stage=stage,
        parent_txid=parent_txid,
        finalized=stage is LndEscrowStage.FINALIZED,
        active_attempt=attempt,
        durable_local_public_nonce=local_nonce,
        durable_remote_public_nonce=remote_nonce,
        durable_local_partial_signature=partial,
        finalized_parent=finalized,
    )


def test_empty_load_and_fail_closed_runtime_admission(tmp_path: Path) -> None:
    negotiation, _ = _negotiation(channels=1)
    empty = MakerChannelInputStore(tmp_path / "empty", max_active_sessions=10)
    registry = MakerChannelInputRegistry.load(
        store=empty, config=negotiation.config, network=negotiation.network
    )
    assert registry.session_count == 0
    assert registry.recovery_outpoints == ()

    store = _store(tmp_path, negotiation)
    with pytest.raises(MakerChannelInputRuntimeError, match="configuration"):
        MakerChannelInputRegistry.load(
            store=store,
            config=negotiation.config.model_copy(update={"max_parent_fee_sat": 9}),
            network=negotiation.network,
        )
    with pytest.raises(MakerChannelInputRuntimeError, match="network"):
        MakerChannelInputRegistry.load(store=store, config=negotiation.config, network="signet")
    (store.directory / "corrupt.json").write_text("bad", encoding="ascii")
    with pytest.raises(MakerChannelInputRuntimeError, match="corrupt"):
        MakerChannelInputRegistry.load(
            store=store, config=negotiation.config, network=negotiation.network
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_lock", [False, True])
async def test_freeze_recovery_is_idempotent_and_saves_before_each_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing_lock: bool
) -> None:
    negotiation, frozen = _negotiation(channels=2)
    _plan_freeze(negotiation, frozen)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    for item in frozen:
        intent = next(
            value
            for value in negotiation.freeze_intents
            if value.channel_id == item.snapshot.channel_id
        )
        backend.statuses[(negotiation.invite.session_id, intent.funding_outpoint)] = (
            _status(LndEscrowStage.LOCKED) if existing_lock else None
        )
        backend.frozen[intent.channel_id] = item.snapshot
    store = registry._store
    original_save = store.save

    def saving(snapshot: object) -> None:
        backend.events.append("save")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "save", saving)
    await registry.reconcile_all(backend, negotiation.local_node_key)

    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and restored.stage is MakerChannelInputStage.HELLOS_READY
    assert backend.events == [
        f"find:{item.snapshot.funding_outpoint.txid}:{item.snapshot.funding_outpoint.vout}"
        for item in frozen
    ] + [
        "save",
        f"freeze:{frozen[0].snapshot.channel_id}",
        "save",
        f"freeze:{frozen[1].snapshot.channel_id}",
        "save",
    ]
    assert registry.recovery_outpoints == tuple(
        sorted(
            (item.snapshot.funding_outpoint for item in frozen),
            key=lambda item: (item.txid, item.vout),
        )
    )


@pytest.mark.asyncio
async def test_freeze_recovery_rejects_later_escrow_stage_before_mutation(tmp_path: Path) -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    intent = negotiation.freeze_intents[0]
    backend.statuses[(negotiation.invite.session_id, intent.funding_outpoint)] = _status(
        LndEscrowStage.PARENT_PREPARED, parent_txid="00" * 32
    )

    with pytest.raises(MakerChannelInputRuntimeError, match="later escrow"):
        await registry.reconcile_all(backend, negotiation.local_node_key)
    assert not any(event.startswith("freeze:") for event in backend.events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [None, _status(LndEscrowStage.PARENT_PREPARED, parent_txid="00" * 32)]
)
async def test_pre_prepare_requires_every_frozen_outpoint_locked(
    tmp_path: Path, status: LndEscrowStatus | None
) -> None:
    negotiation, frozen = _negotiation(channels=1)
    _plan_freeze(negotiation, frozen)
    negotiation.install_frozen_inputs(frozen)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    backend.statuses[(negotiation.invite.session_id, frozen[0].snapshot.funding_outpoint)] = status

    with pytest.raises(MakerChannelInputRuntimeError, match="frozen channel"):
        await registry.reconcile_all(backend, negotiation.local_node_key)


@pytest.mark.asyncio
async def test_locked_parent_prepares_then_records_exact_attempt(tmp_path: Path) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    outpoint = proposal.recipient_terms[0].funding_outpoint
    escrow = _escrow(outpoint)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    backend.statuses[(negotiation.invite.session_id, escrow)] = _status(LndEscrowStage.LOCKED)
    backend.attempts[escrow] = LndEscrowSigningAttempt(
        attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66
    )

    await registry.reconcile_all(backend, negotiation.local_node_key)

    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None
    assert restored.channel_states[0].prepared.public_nonce == "02" * 66
    assert backend.events[1:] == [f"prepare:{outpoint.txid}:0", f"begin:{outpoint.txid}:False"]


@pytest.mark.asyncio
async def test_prepared_recovery_preflights_all_channels_before_mutation(tmp_path: Path) -> None:
    negotiation, proposal = _prepared_negotiation(channels=2)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    first, second = proposal.recipient_terms
    backend.statuses[(negotiation.invite.session_id, _escrow(first.funding_outpoint))] = _status(
        LndEscrowStage.LOCKED
    )
    backend.statuses[(negotiation.invite.session_id, _escrow(second.funding_outpoint))] = _status(
        LndEscrowStage.PARENT_PREPARED,
        parent_txid="00" * 32,
    )

    with pytest.raises(MakerChannelInputRuntimeError, match="differs from transcript"):
        await registry.reconcile_all(backend, negotiation.local_node_key)

    assert not any(event.startswith(("prepare:", "begin:", "recover:")) for event in backend.events)


@pytest.mark.asyncio
async def test_parent_prepared_attempt_recovery_rotation_and_conflict(tmp_path: Path) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    outpoint = proposal.recipient_terms[0].funding_outpoint
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    escrow = _escrow(outpoint)
    first = LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66)
    negotiation.record_prepared(outpoint, parent_txid, first)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    backend.statuses[(negotiation.invite.session_id, escrow)] = _status(
        LndEscrowStage.PARENT_PREPARED, parent_txid=parent_txid
    )
    replacement = LndEscrowSigningAttempt(attempt_id=b"\x03" * 32, public_nonce=b"\x04" * 66)
    backend.attempts[escrow] = replacement

    await registry.reconcile_all(backend, negotiation.local_node_key)
    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and restored.channel_states[0].attempt_id == "03" * 32
    assert backend.events[-1] == f"begin:{outpoint.txid}:True"
    assert restored.channel_states[0].restart_intended is False

    backend.statuses[(negotiation.invite.session_id, escrow)] = _status(
        LndEscrowStage.PARENT_PREPARED,
        parent_txid=parent_txid,
        attempt=LndEscrowSigningAttempt(attempt_id=b"\x05" * 32, public_nonce=b"\x06" * 66),
    )
    with pytest.raises(MakerChannelInputRuntimeError, match="attempt"):
        await registry.reconcile_all(backend, negotiation.local_node_key)


@pytest.mark.asyncio
async def test_parent_prepared_adopts_replacement_only_after_persisted_restart_intent(
    tmp_path: Path,
) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    outpoint = proposal.recipient_terms[0].funding_outpoint
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    escrow = _escrow(outpoint)
    negotiation.record_prepared(
        outpoint,
        parent_txid,
        LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66),
    )
    negotiation.plan_unsigned_prepared_rotation(outpoint)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    replacement = LndEscrowSigningAttempt(attempt_id=b"\x03" * 32, public_nonce=b"\x04" * 66)
    backend.statuses[(negotiation.invite.session_id, escrow)] = _status(
        LndEscrowStage.PARENT_PREPARED,
        parent_txid=parent_txid,
        attempt=replacement,
    )

    await registry.reconcile_all(backend, negotiation.local_node_key)

    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None
    assert restored.channel_states[0].attempt_id == replacement.attempt_id.hex()
    assert restored.channel_states[0].restart_intended is False
    assert not any(event.startswith("begin:") for event in backend.events)


@pytest.mark.asyncio
async def test_parent_prepared_rejects_persisted_partial_even_with_matching_attempt(
    tmp_path: Path,
) -> None:
    negotiation, proposal, local, _, _ = _signing_negotiation()
    negotiation.record_partial(local.funding_outpoint, b"\xaa" * 32)
    state = negotiation.channel_states[0]
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    escrow = _escrow(local.funding_outpoint)
    backend.statuses[(negotiation.invite.session_id, escrow)] = _status(
        LndEscrowStage.PARENT_PREPARED,
        parent_txid=get_txid(proposal.transcript.unsigned_parent_tx),
        attempt=LndEscrowSigningAttempt(
            attempt_id=bytes.fromhex(state.attempt_id),
            public_nonce=bytes.fromhex(state.prepared.public_nonce),
        ),
    )

    with pytest.raises(MakerChannelInputRuntimeError, match="precedes durable signing"):
        await registry.reconcile_all(backend, negotiation.local_node_key)


@pytest.mark.asyncio
async def test_signed_partial_and_finalized_evidence_recovery_fails_on_conflict(
    tmp_path: Path,
) -> None:
    negotiation, proposal, local, peer, _ = _signing_negotiation()
    outpoint = local.funding_outpoint
    escrow = _escrow(outpoint)
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    partial = b"\xaa" * 32
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    backend.statuses[(negotiation.invite.session_id, escrow)] = _status(
        LndEscrowStage.PARENT_SIGNED,
        parent_txid=parent_txid,
        local_nonce=bytes.fromhex(local.public_nonce),
        remote_nonce=bytes.fromhex(peer.public_nonce),
        partial=partial,
    )

    await registry.reconcile_all(backend, negotiation.local_node_key)
    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and restored.channel_states[0].partial is not None

    finalize = _finalize_intent(proposal, local, peer)
    restored.accept_finalize("coordinator", finalize)
    result = _finalized_result(proposal, 0, b"\x11" * 64)
    backend.finalized[escrow] = result
    await registry.reconcile_all(backend, negotiation.local_node_key)
    assert restored.channel_states[0].finalized is not None

    backend.statuses[(negotiation.invite.session_id, escrow)] = _status(
        LndEscrowStage.PARENT_SIGNED,
        parent_txid=parent_txid,
        local_nonce=bytes.fromhex(local.public_nonce),
        remote_nonce=b"\x09" * 66,
        partial=partial,
    )
    with pytest.raises(MakerChannelInputRuntimeError):
        await registry.reconcile_all(backend, negotiation.local_node_key)


@pytest.mark.asyncio
async def test_finalized_status_rebuilds_final_evidence_and_rejects_replacement(
    tmp_path: Path,
) -> None:
    negotiation, proposal, local, peer, _ = _signing_negotiation()
    outpoint = local.funding_outpoint
    escrow = _escrow(outpoint)
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    partial = b"\xaa" * 32
    negotiation.record_partial(outpoint, partial)
    negotiation.accept_finalize("coordinator", _finalize_intent(proposal, local, peer))
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    first = _finalized_result(proposal, 0, b"\x11" * 64)
    backend.statuses[(negotiation.invite.session_id, escrow)] = _status(
        LndEscrowStage.FINALIZED,
        parent_txid=parent_txid,
        local_nonce=bytes.fromhex(local.public_nonce),
        remote_nonce=bytes.fromhex(peer.public_nonce),
        partial=partial,
        finalized=first,
    )

    await registry.reconcile_all(backend, negotiation.local_node_key)
    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and restored.channel_states[0].finalized is not None

    replacement = _finalized_result(proposal, 0, b"\x22" * 64)
    backend.statuses[(negotiation.invite.session_id, escrow)] = _status(
        LndEscrowStage.FINALIZED,
        parent_txid=parent_txid,
        local_nonce=bytes.fromhex(local.public_nonce),
        remote_nonce=bytes.fromhex(peer.public_nonce),
        partial=partial,
        finalized=replacement,
    )
    with pytest.raises(MakerChannelInputRuntimeError, match="finalization"):
        await registry.reconcile_all(backend, negotiation.local_node_key)


@pytest.mark.asyncio
async def test_node_mismatch_stops_all_backend_calls(tmp_path: Path) -> None:
    negotiation, _ = _negotiation(channels=1)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()

    with pytest.raises(MakerChannelInputRuntimeError, match="another local node"):
        await registry.reconcile_all(backend, _node(99))
    assert backend.events == []


def _empty_registry(
    tmp_path: Path, negotiation: MakerChannelInputNegotiation
) -> MakerChannelInputRegistry:
    return MakerChannelInputRegistry.load(
        store=MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=10),
        config=negotiation.config,
        network=negotiation.network,
    )


async def _invite(
    registry: MakerChannelInputRegistry,
    backend: FakeBackend,
    negotiation: MakerChannelInputNegotiation,
    frozen: tuple[MakerFrozenChannelInput, ...],
    allocations: list[str],
) -> tuple[object, ...]:
    for item in frozen:
        backend.frozen[item.snapshot.channel_id] = item.snapshot

    def allocate(candidate: ChannelInputCandidate) -> str:
        allocations.append(candidate.funding_outpoint.txid)
        return "5120" + candidate.funding_outpoint.txid

    return await registry.handle_payload(
        negotiation.invite,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,
        candidates=tuple(_candidate(item, channel_id=index) for index, item in enumerate(frozen)),
        settlement_script_for=allocate,
        current_time=1_000,
        current_height=100,
    )


@pytest.mark.asyncio
async def test_live_invite_selects_one_candidate_and_replays_without_allocating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    negotiation, frozen = _negotiation(channels=2)
    registry = _empty_registry(tmp_path, negotiation)
    backend = FakeBackend()
    allocations: list[str] = []
    original_save = registry._store.save

    def saving(snapshot: object) -> None:
        backend.events.append("save")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(registry._store, "save", saving)
    hellos = await _invite(registry, backend, negotiation, frozen, allocations)

    assert len(hellos) == 1
    assert allocations == [frozen[0].snapshot.funding_outpoint.txid]
    assert backend.events == [
        "save",
        f"find:{frozen[0].snapshot.funding_outpoint.txid}:{frozen[0].snapshot.funding_outpoint.vout}",
        "save",
        f"freeze:{frozen[0].snapshot.channel_id}",
        "save",
    ]

    replay = await _invite(registry, backend, negotiation, frozen, allocations)
    assert replay == hellos
    assert len(allocations) == 1
    with pytest.raises(ChannelInputValidationError, match="different actor"):
        await registry.handle_payload(
            negotiation.invite,
            coordinator_actor="other-coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=backend,
            candidates=(),
            settlement_script_for=lambda candidate: pytest.fail(
                "settlement allocation must not run"
            ),
            current_time=1_000,
            current_height=100,
        )


@pytest.mark.asyncio
async def test_live_dispatch_persists_backend_boundaries_and_replays_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, frozen = _negotiation(channels=1)
    registry = _empty_registry(tmp_path, original)
    backend = FakeBackend()
    allocations: list[str] = []
    await _invite(registry, backend, original, frozen, allocations)
    negotiation = registry.get(original.invite.session_id, original.invite.attempt)
    assert negotiation is not None
    proposal, _ = _proposal(negotiation)
    approval = await registry.handle_payload(
        proposal,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,
        candidates=(),
        settlement_script_for=lambda candidate: "",
        current_time=1_000,
        current_height=100,
    )
    approval_set = _approval_set(proposal, approval[0])
    ack = await registry.handle_payload(
        approval_set,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,
        candidates=(),
        settlement_script_for=lambda candidate: "",
        current_time=1_000,
        current_height=100,
    )
    prepare = _prepare(approval_set, ack[0])
    outpoint = proposal.recipient_terms[0].funding_outpoint
    escrow = _escrow(outpoint)
    attempt = LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66)
    backend.statuses[(proposal.session_id, escrow)] = _status(LndEscrowStage.LOCKED)
    backend.attempts[escrow] = attempt
    prepared = await registry.handle_payload(
        prepare,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,
        candidates=(),
        settlement_script_for=lambda candidate: "",
        current_time=1_000,
        current_height=100,
    )
    local = prepared[0]
    backend.statuses[(proposal.session_id, escrow)] = _status(
        LndEscrowStage.PARENT_PREPARED,
        parent_txid=get_txid(proposal.transcript.unsigned_parent_tx),
        attempt=attempt,
    )
    assert (
        await registry.handle_payload(
            prepare,
            coordinator_actor="coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=backend,
            candidates=(),
            settlement_script_for=lambda candidate: "",
            current_time=1_000,
            current_height=100,
        )
        == prepared
    )
    peer = _peer_prepared(proposal, local, b"\x03" * 66)
    sign = _sign_intent(proposal, local, peer)
    backend.partials[escrow] = b"\xaa" * 32
    original_save = registry._store.save

    def saving(snapshot: object) -> None:
        backend.events.append("save")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(registry._store, "save", saving)
    partial = await registry.handle_payload(
        sign,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,
        candidates=(),
        settlement_script_for=lambda candidate: "",
        current_time=1_000,
        current_height=100,
    )
    assert backend.events[-2:] == [
        f"sign:{outpoint.txid}:{attempt.attempt_id.hex()}",
        "save",
    ]
    assert (
        await registry.handle_payload(
            sign,
            coordinator_actor="coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=backend,
            candidates=(),
            settlement_script_for=lambda candidate: "",
            current_time=1_000,
            current_height=100,
        )
        == partial
    )

    finalize = _finalize_intent(proposal, local, peer)
    backend.finalized[escrow] = _finalized_result(proposal, 0, b"\x11" * 64)
    finalized = await registry.handle_payload(
        finalize,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,
        candidates=(),
        settlement_script_for=lambda candidate: "",
        current_time=1_000,
        current_height=100,
    )
    assert backend.events[-2:] == [
        f"finalize:{outpoint.txid}:{attempt.attempt_id.hex()}",
        "save",
    ]
    assert (
        await registry.handle_payload(
            finalize,
            coordinator_actor="coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=backend,
            candidates=(),
            settlement_script_for=lambda candidate: "",
            current_time=1_000,
            current_height=100,
        )
        == finalized
    )


@pytest.mark.asyncio
async def test_live_dispatch_rejects_unknown_and_unsupported_payloads(tmp_path: Path) -> None:
    negotiation, _ = _negotiation(channels=1)
    registry = _empty_registry(tmp_path, negotiation)
    backend = FakeBackend()
    cancel = ChannelInputCancelPayload(
        session_id=negotiation.invite.session_id,
        attempt=negotiation.invite.attempt,
        signer_key=ChannelInputKeyPair.from_secret((1).to_bytes(32, "big")).public_key,
        transcript_hash="00" * 32,
        reason_code="unsupported",
    )
    with pytest.raises(MakerChannelInputRuntimeError, match="unknown"):
        await registry.handle_payload(
            cancel,
            coordinator_actor="coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=backend,
            candidates=(),
            settlement_script_for=lambda candidate: "",
            current_time=1_000,
            current_height=100,
        )
    prepared_negotiation, _ = _prepared_negotiation(channels=1)
    assert prepared_negotiation.proposal is not None
    unknown = prepared_negotiation.proposal.model_copy(update={"attempt": 2})
    with pytest.raises(MakerChannelInputRuntimeError, match="unknown"):
        await registry.handle_payload(
            unknown,
            coordinator_actor="coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=backend,
            candidates=(),
            settlement_script_for=lambda candidate: "",
            current_time=1_000,
            current_height=100,
        )


@pytest.mark.asyncio
async def test_live_invite_excludes_frozen_outpoints_and_enforces_capacity(tmp_path: Path) -> None:
    negotiation, frozen = _negotiation(channels=1)
    registry = _empty_registry(tmp_path, negotiation)
    backend = FakeBackend()
    allocations: list[str] = []
    await _invite(registry, backend, negotiation, frozen, allocations)
    next_invite = sign_payload(
        negotiation.invite.model_copy(update={"attempt": negotiation.invite.attempt + 1}),
        (1).to_bytes(32, "big"),
    )

    with pytest.raises(MakerChannelInputRuntimeError, match="no channel-input candidates"):
        await registry.handle_payload(
            next_invite,
            coordinator_actor="coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=backend,
            candidates=(_candidate(frozen[0]),),
            settlement_script_for=lambda candidate: pytest.fail(
                "settlement allocation must not run"
            ),
            current_time=1_000,
            current_height=100,
        )

    limited = MakerChannelInputRegistry.load(
        store=MakerChannelInputStore(tmp_path / "limited", max_active_sessions=1),
        config=negotiation.config,
        network=negotiation.network,
    )
    limited_backend = FakeBackend()
    await _invite(limited, limited_backend, negotiation, frozen, [])
    with pytest.raises(MakerChannelInputRuntimeError, match="maximum active"):
        await limited.handle_payload(
            next_invite,
            coordinator_actor="coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=limited_backend,
            candidates=(),
            settlement_script_for=lambda candidate: pytest.fail(
                "settlement allocation must not run"
            ),
            current_time=1_000,
            current_height=100,
        )


async def _dispatch_cancel(
    registry: MakerChannelInputRegistry,
    negotiation: MakerChannelInputNegotiation,
    backend: FakeBackend,
    cancel: ChannelInputCancelPayload | None = None,
) -> tuple[object, ...]:
    if cancel is None:
        assert negotiation.proposal is not None
        cancel = _cancel(negotiation.proposal)
    return await registry.handle_payload(
        cancel,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,
        candidates=(),
        settlement_script_for=lambda candidate: "",
        current_time=1_000,
        current_height=100,
    )


async def _dispatch_status(
    registry: MakerChannelInputRegistry,
    negotiation: MakerChannelInputNegotiation,
    backend: FakeBackend,
    request: ChannelInputStatusRequestPayload,
) -> tuple[ChannelInputStatusPayload, ...]:
    responses = await registry.handle_payload(
        request,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,
        candidates=(),
        settlement_script_for=lambda candidate: "",
        current_time=1_000,
        current_height=100,
    )
    return tuple(cast(ChannelInputStatusPayload, response) for response in responses)


@pytest.mark.asyncio
async def test_status_dispatch_recovers_then_persists_exact_prepared_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    outpoint = proposal.recipient_terms[0].funding_outpoint
    escrow = _escrow(outpoint)
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    attempt = LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    backend.statuses[(proposal.session_id, escrow)] = _status(
        LndEscrowStage.PARENT_PREPARED, parent_txid=parent_txid, attempt=attempt
    )
    backend.attempts[escrow] = attempt
    original_save = registry._store.save

    def saving(snapshot: object) -> None:
        backend.events.append("save")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(registry._store, "save", saving)
    request = _status_request(proposal, outpoint)
    response = await _dispatch_status(registry, negotiation, backend, request)

    assert len(response) == 1
    status = response[0]
    assert status.escrow_stage == "parent_prepared"
    assert backend.events == [
        f"find:{outpoint.txid}:{outpoint.vout}",
        "save",
        f"begin:{outpoint.txid}:False",
        "save",
        f"find:{outpoint.txid}:{outpoint.vout}",
        "save",
    ]
    restored = registry.get(proposal.session_id, proposal.attempt)
    assert restored is not None
    assert restored.channel_states[0].status_response == status
    encoded = encode_channel_input_message(status)
    assert attempt.attempt_id.hex() not in encoded


@pytest.mark.asyncio
async def test_status_dispatch_reflects_recovered_nonce_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    outpoint = proposal.recipient_terms[0].funding_outpoint
    escrow = _escrow(outpoint)
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    first = LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66)
    replacement = LndEscrowSigningAttempt(attempt_id=b"\x03" * 32, public_nonce=b"\x04" * 66)
    negotiation.record_prepared(outpoint, parent_txid, first)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    backend.statuses[(proposal.session_id, escrow)] = _status(
        LndEscrowStage.PARENT_PREPARED, parent_txid=parent_txid
    )

    async def begin(
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        *,
        restart_attempt: bool = False,
    ) -> LndEscrowSigningAttempt:
        assert session_id == proposal.session_id
        assert funding_outpoint == escrow and restart_attempt
        backend.statuses[(proposal.session_id, escrow)] = _status(
            LndEscrowStage.PARENT_PREPARED, parent_txid=parent_txid, attempt=replacement
        )
        return replacement

    monkeypatch.setattr(backend, "begin_channel_input_signing", begin)
    response = await _dispatch_status(
        registry, negotiation, backend, _status_request(proposal, outpoint)
    )

    status = response[0]
    assert status.prepared.public_nonce == replacement.public_nonce.hex()
    restored = registry.get(proposal.session_id, proposal.attempt)
    assert restored is not None
    assert restored.channel_states[0].attempt_id == replacement.attempt_id.hex()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [LndEscrowStage.PARENT_SIGNED, LndEscrowStage.FINALIZED])
async def test_status_dispatch_returns_recovered_partial_and_finalized_evidence(
    tmp_path: Path, stage: LndEscrowStage
) -> None:
    negotiation, proposal, local, peer, _ = _signing_negotiation()
    outpoint = local.funding_outpoint
    escrow = _escrow(outpoint)
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    result = _finalized_result(proposal, 0, b"\x11" * 64)
    if stage is LndEscrowStage.FINALIZED:
        negotiation.record_partial(outpoint, b"\xaa" * 32)
        negotiation.accept_finalize("coordinator", _finalize_intent(proposal, local, peer))
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    backend.statuses[(proposal.session_id, escrow)] = _status(
        stage,
        parent_txid=parent_txid,
        local_nonce=bytes.fromhex(local.public_nonce),
        remote_nonce=bytes.fromhex(peer.public_nonce),
        partial=b"\xaa" * 32,
        finalized=result if stage is LndEscrowStage.FINALIZED else None,
    )

    response = await _dispatch_status(
        registry, negotiation, backend, _status_request(proposal, outpoint)
    )

    status = response[0]
    assert status.partial is not None
    assert status.escrow_stage == (
        "finalized" if stage is LndEscrowStage.FINALIZED else "parent_signed"
    )
    assert (status.finalized is not None) is (stage is LndEscrowStage.FINALIZED)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, _status(LndEscrowStage.LOCKED)])
async def test_status_dispatch_rejects_missing_or_locked_evidence(
    tmp_path: Path, status: LndEscrowStatus | None
) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    outpoint = proposal.recipient_terms[0].funding_outpoint
    parent_txid = get_txid(proposal.transcript.unsigned_parent_tx)
    attempt = LndEscrowSigningAttempt(attempt_id=b"\x01" * 32, public_nonce=b"\x02" * 66)
    negotiation.record_prepared(outpoint, parent_txid, attempt)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    backend.statuses[(proposal.session_id, _escrow(outpoint))] = status

    with pytest.raises(MakerChannelInputRuntimeError):
        await _dispatch_status(registry, negotiation, backend, _status_request(proposal, outpoint))


@pytest.mark.asyncio
async def test_cancel_preflights_saves_and_replays_signed_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    negotiation, proposal = _prepared_negotiation(channels=2)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    for terms in proposal.recipient_terms:
        backend.statuses[(proposal.session_id, _escrow(terms.funding_outpoint))] = _status(
            LndEscrowStage.LOCKED
        )
    original_save = registry._store.save

    def saving(snapshot: object) -> None:
        backend.events.append("save")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(registry._store, "save", saving)
    cancel = _cancel(proposal)
    response = await _dispatch_cancel(registry, negotiation, backend, cancel)

    assert len(response) == 1
    assert (
        registry.get(proposal.session_id, proposal.attempt).stage is MakerChannelInputStage.CANCELED
    )  # type: ignore[union-attr]
    first_cancel = next(
        index for index, event in enumerate(backend.events) if event.startswith("cancel:")
    )
    assert backend.events[:first_cancel].count("save") == 2
    assert all(
        event.startswith("find:") or event == "save" for event in backend.events[:first_cancel]
    )
    cancel_count = sum(event.startswith("cancel:") for event in backend.events)
    assert await _dispatch_cancel(registry, negotiation, backend, cancel) == response
    assert sum(event.startswith("cancel:") for event in backend.events) == cancel_count


@pytest.mark.asyncio
async def test_pretranscript_cancel_queries_before_resume_and_replays_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    negotiation, frozen = _negotiation(channels=2)
    _plan_freeze(negotiation, frozen)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    for intent in negotiation.freeze_intents:
        backend.statuses[(negotiation.invite.session_id, intent.funding_outpoint)] = _status(
            LndEscrowStage.LOCKED
        )
    original_save = registry._store.save

    def saving(snapshot: object) -> None:
        backend.events.append("save")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(registry._store, "save", saving)
    cancel = _pretranscript_cancel(negotiation)
    response = await _dispatch_cancel(registry, negotiation, backend, cancel)

    first_cancel = next(
        index for index, event in enumerate(backend.events) if event.startswith("cancel:")
    )
    assert all(
        event.startswith("find:") or event == "save" for event in backend.events[:first_cancel]
    )
    assert backend.events[:first_cancel].count("save") == 2
    cancel_count = sum(event.startswith("cancel:") for event in backend.events)
    assert await _dispatch_cancel(registry, negotiation, backend, cancel) == response
    assert sum(event.startswith("cancel:") for event in backend.events) == cancel_count


@pytest.mark.asyncio
async def test_pretranscript_cancel_rejects_later_escrow_before_mutation(tmp_path: Path) -> None:
    negotiation, frozen = _negotiation(channels=2)
    _plan_freeze(negotiation, frozen)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    first, second = negotiation.freeze_intents
    backend.statuses[(negotiation.invite.session_id, first.funding_outpoint)] = _status(
        LndEscrowStage.LOCKED
    )
    backend.statuses[(negotiation.invite.session_id, second.funding_outpoint)] = _status(
        LndEscrowStage.PARENT_PREPARED, parent_txid="00" * 32
    )

    with pytest.raises(MakerChannelInputRuntimeError, match="signed"):
        await _dispatch_cancel(registry, negotiation, backend, _pretranscript_cancel(negotiation))
    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and restored.cancel is None
    assert len([event for event in backend.events if event.startswith("find:")]) == 2
    assert not any(event.startswith("cancel:") for event in backend.events)


@pytest.mark.asyncio
async def test_cancel_rejects_signed_status_before_intent_or_backend_cancel(tmp_path: Path) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    outpoint = proposal.recipient_terms[0].funding_outpoint
    backend.statuses[(proposal.session_id, _escrow(outpoint))] = _status(
        LndEscrowStage.PARENT_SIGNED,
        parent_txid=get_txid(proposal.transcript.unsigned_parent_tx),
        local_nonce=b"\x01" * 66,
        remote_nonce=b"\x02" * 66,
        partial=b"\x03" * 32,
    )

    with pytest.raises(MakerChannelInputRuntimeError, match="signed"):
        await _dispatch_cancel(registry, negotiation, backend)
    restored = registry.get(proposal.session_id, proposal.attempt)
    assert restored is not None and restored.cancel is None
    assert not any(event.startswith("cancel:") for event in backend.events)


@pytest.mark.asyncio
async def test_canceled_attempt_rejects_durable_sign_replay_before_backend_call(
    tmp_path: Path,
) -> None:
    negotiation, proposal, _, _, sign = _signing_negotiation()
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    outpoint = proposal.recipient_terms[0].funding_outpoint
    backend.statuses[(proposal.session_id, _escrow(outpoint))] = _status(
        LndEscrowStage.PARENT_PREPARED,
        parent_txid=get_txid(proposal.transcript.unsigned_parent_tx),
    )
    await _dispatch_cancel(registry, negotiation, backend)
    backend.events.clear()

    with pytest.raises(MakerChannelInputRuntimeError, match="terminal"):
        await registry.handle_payload(
            sign,
            coordinator_actor="coordinator",
            local_actor="maker",
            local_node_key=negotiation.local_node_key,
            backend=backend,
            candidates=(),
            settlement_script_for=lambda candidate: "",
            current_time=1_000,
            current_height=100,
        )

    assert backend.events == []


@pytest.mark.asyncio
async def test_startup_resumes_cancel_intent_after_one_escrow_already_resumed(
    tmp_path: Path,
) -> None:
    negotiation, proposal = _prepared_negotiation(channels=2)
    assert negotiation.accept_cancel("coordinator", _cancel(proposal)) is None
    store = _store(tmp_path, negotiation)
    registry = MakerChannelInputRegistry.load(
        store=store, config=negotiation.config, network=negotiation.network
    )
    backend = FakeBackend()
    first, second = proposal.recipient_terms
    backend.statuses[(proposal.session_id, _escrow(first.funding_outpoint))] = None
    backend.statuses[(proposal.session_id, _escrow(second.funding_outpoint))] = _status(
        LndEscrowStage.PARENT_PREPARED,
        parent_txid=get_txid(proposal.transcript.unsigned_parent_tx),
    )

    await registry.reconcile_all(backend, negotiation.local_node_key)

    restored = registry.get(proposal.session_id, proposal.attempt)
    assert restored is not None and restored.stage is MakerChannelInputStage.CANCELED
    assert (
        backend.events.count(
            f"cancel:{second.funding_outpoint.txid}:{second.funding_outpoint.vout}"
        )
        == 1
    )
    assert not any(
        event.startswith(f"cancel:{first.funding_outpoint.txid}") for event in backend.events
    )


@pytest.mark.asyncio
async def test_startup_resumes_pretranscript_cancel_intent(tmp_path: Path) -> None:
    negotiation, frozen = _negotiation(channels=2)
    _plan_freeze(negotiation, frozen)
    cancel = _pretranscript_cancel(negotiation)
    assert negotiation.accept_cancel("coordinator", cancel) is None
    store = _store(tmp_path, negotiation)
    registry = MakerChannelInputRegistry.load(
        store=store, config=negotiation.config, network=negotiation.network
    )
    backend = FakeBackend()
    first, second = negotiation.freeze_intents
    backend.statuses[(negotiation.invite.session_id, first.funding_outpoint)] = None
    backend.statuses[(negotiation.invite.session_id, second.funding_outpoint)] = _status(
        LndEscrowStage.LOCKED
    )

    await registry.reconcile_all(backend, negotiation.local_node_key)

    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and restored.stage is MakerChannelInputStage.CANCELED
    assert (
        backend.events.count(
            f"cancel:{second.funding_outpoint.txid}:{second.funding_outpoint.vout}"
        )
        == 1
    )


def test_expired_canceled_tombstone_is_pruned_and_releases_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    negotiation, proposal, _ = _proposal_approved()
    assert negotiation.accept_cancel("coordinator", _cancel(proposal)) is None
    negotiation.record_canceled(
        tuple(
            sorted(
                (term.funding_outpoint for term in proposal.recipient_terms),
                key=lambda item: item.key(),
            )
        )
    )
    store = _store(tmp_path, negotiation)
    monkeypatch.setattr("maker.channel_input_runtime.time.time", lambda: 2_000)

    registry = MakerChannelInputRegistry.load(
        store=store, config=negotiation.config, network=negotiation.network
    )

    assert registry.session_count == 0
    assert store.load_all().records == ()


@pytest.mark.asyncio
async def test_unexpired_tombstone_releases_candidate_and_active_capacity(tmp_path: Path) -> None:
    negotiation, proposal = _prepared_negotiation(channels=1)
    store = MakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=1)
    store.save(negotiation.snapshot())
    registry = MakerChannelInputRegistry.load(
        store=store, config=negotiation.config, network=negotiation.network
    )
    backend = FakeBackend()
    outpoint = proposal.recipient_terms[0].funding_outpoint
    backend.statuses[(proposal.session_id, _escrow(outpoint))] = _status(LndEscrowStage.LOCKED)
    await _dispatch_cancel(registry, negotiation, backend)
    assert registry.session_count == 0

    next_invite = sign_payload(
        negotiation.invite.model_copy(update={"attempt": negotiation.invite.attempt + 1}),
        (1).to_bytes(32, "big"),
    )
    frozen = negotiation.frozen_inputs[0]
    backend.frozen[frozen.snapshot.channel_id] = frozen.snapshot
    response = await registry.handle_payload(
        next_invite,
        coordinator_actor="coordinator",
        local_actor="maker",
        local_node_key=negotiation.local_node_key,
        backend=backend,
        candidates=(_candidate(frozen),),
        settlement_script_for=lambda candidate: frozen.settlement_script_pubkey,
        current_time=1_000,
        current_height=100,
    )

    assert len(response) == 1
    assert registry.session_count == 1
