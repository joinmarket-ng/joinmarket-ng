"""Tests for maker-owned channel-input startup recovery."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from jmcore.bitcoin import get_txid
from jmcore.channel_input import Outpoint
from jmswap.lnd import (
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
    WitnessUtxo,
)
from test_channel_input_negotiation import (
    _finalize_intent,
    _finalized_result,
    _negotiation,
    _node,
    _plan_freeze,
    _prepared_negotiation,
    _signing_negotiation,
)

from maker.channel_input import MakerChannelInputNegotiation, MakerChannelInputStage
from maker.channel_input_runtime import MakerChannelInputRegistry, MakerChannelInputRuntimeError
from maker.channel_input_store import MakerChannelInputStore


class FakeBackend:
    def __init__(self) -> None:
        self.statuses: dict[tuple[str, EscrowOutpoint], LndEscrowStatus | None] = {}
        self.frozen: dict[str, FrozenChannelSnapshot] = {}
        self.attempts: dict[EscrowOutpoint, LndEscrowSigningAttempt] = {}
        self.finalized: dict[EscrowOutpoint, LndFinalizedEscrowParent] = {}
        self.events: list[str] = []

    async def find_channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus | None:
        self.events.append(f"find:{funding_outpoint.txid}:{funding_outpoint.vout}")
        return self.statuses.get((session_id, funding_outpoint))

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
