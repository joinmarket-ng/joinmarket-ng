"""Tests for taker-owned channel-input durable recovery."""

from __future__ import annotations

from pathlib import Path

import pytest
from jmcore.channel_input import (
    ChannelInputCancelAckPayload,
    ChannelInputCancelPayload,
    ChannelInputHelloPayload,
    ChannelInputKeyPair,
    Outpoint,
    PolicyCeilings,
    cancel_payload_hash,
    sign_payload,
)
from jmcore.channel_input_config import ChannelInputConfig
from jmswap.lnd import (
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
)
from test_channel_input_negotiation import (
    _COORDINATOR_SECRET,
    _MAKER_SECRETS,
    _accept_hello,
    _candidate,
    _freeze_intent,
    _freeze_remote,
    _frozen_snapshot,
    _new,
    _node,
)

from taker.channel_input_negotiation import TakerChannelInputNegotiation, TakerChannelInputStage
from taker.channel_input_runtime import TakerChannelInputRegistry, TakerChannelInputRuntimeError
from taker.channel_input_store import TakerChannelInputStore


class FakeBackend:
    def __init__(self) -> None:
        self.statuses: dict[tuple[str, EscrowOutpoint], LndEscrowStatus | None] = {}
        self.frozen: dict[str, FrozenChannelSnapshot] = {}
        self.events: list[str] = []

    async def find_channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus | None:
        self.events.append(f"find:{funding_outpoint.txid}:{funding_outpoint.vout}")
        return self.statuses.get((session_id, funding_outpoint))

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> FrozenChannelSnapshot:
        self.events.append(f"freeze:{channel_id}")
        return self.frozen[channel_id]

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool:
        self.events.append(f"cancel:{funding_outpoint.txid}:{funding_outpoint.vout}")
        self.statuses[(session_id, funding_outpoint)] = None
        return True


def _config(*outpoints: EscrowOutpoint) -> ChannelInputConfig:
    return ChannelInputConfig(
        allowed_channel_points=tuple(f"{item.txid}:{item.vout}" for item in outpoints),
        recovery_delay_blocks=10,
    )


def _store(tmp_path: Path, *negotiations: TakerChannelInputNegotiation) -> TakerChannelInputStore:
    store = TakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=4)
    for negotiation in negotiations:
        store.save(negotiation.snapshot())
    return store


def _registry(
    tmp_path: Path, negotiation: TakerChannelInputNegotiation
) -> TakerChannelInputRegistry:
    points = tuple(intent.funding_outpoint for _, intent in negotiation.freeze_intents)
    return TakerChannelInputRegistry.load(
        store=_store(tmp_path, negotiation),
        config=_config(*points),
        network="regtest",
        local_actor="taker",
    )


def _locked() -> LndEscrowStatus:
    return LndEscrowStatus(stage=LndEscrowStage.LOCKED, parent_txid=None, finalized=False)


def _remote_for(negotiation: TakerChannelInputNegotiation, index: int) -> ChannelInputHelloPayload:
    remote = _freeze_remote(index).model_copy(
        update={
            "session_id": negotiation.invite.session_id,
            "attempt": negotiation.invite.attempt,
            "sig": "00" * 64,
        }
    )
    return sign_payload(remote, _MAKER_SECRETS[index], aux_randomness=bytes([index + 1]) * 32)


def _add_freeze(
    negotiation: TakerChannelInputNegotiation, nick: str, index: int
) -> ChannelInputHelloPayload:
    remote = _remote_for(negotiation, index)
    negotiation.record_freeze_intent(
        nick,
        remote,
        _candidate(index),
        local_node_key=_node(10),
        settlement_script_pubkey="5120" + f"{index + 50:064x}",
        quoted_miner_fee_share_sat=1_000,
        policy_ceilings=PolicyCeilings(
            max_miner_fee_share_sat=1_000,
            max_claim_adjustment_sat=1_000,
            max_settlement_outputs=1,
        ),
    )
    return remote


def _cancel_ack(
    negotiation: TakerChannelInputNegotiation,
    cancel: ChannelInputCancelPayload,
    nick: str,
    maker_index: int,
) -> ChannelInputCancelAckPayload:
    remote = dict(negotiation.remote_hellos)[nick]
    return sign_payload(
        ChannelInputCancelAckPayload(
            session_id=negotiation.invite.session_id,
            attempt=negotiation.invite.attempt,
            signer_key=remote.participant_key,
            transcript_hash=cancel.transcript_hash,
            cancel_hash=cancel_payload_hash(cancel).hex(),
            resumed_outpoints=(remote.funding_outpoint,),
        ),
        _MAKER_SECRETS[maker_index],
        aux_randomness=bytes([maker_index + 10]) * 32,
    )


def _unknown_hello_cancel_ack(
    negotiation: TakerChannelInputNegotiation,
    cancel: ChannelInputCancelPayload,
    index: int,
) -> ChannelInputCancelAckPayload:
    maker = ChannelInputKeyPair.from_secret(_MAKER_SECRETS[index])
    return sign_payload(
        ChannelInputCancelAckPayload(
            session_id=negotiation.invite.session_id,
            attempt=negotiation.invite.attempt,
            signer_key=maker.public_key,
            transcript_hash=cancel.transcript_hash,
            cancel_hash=cancel_payload_hash(cancel).hex(),
            resumed_outpoints=(Outpoint(txid=f"{index + 20:064x}", vout=index),),
        ),
        _MAKER_SECRETS[index],
        aux_randomness=bytes([index + 20]) * 32,
    )


def test_load_is_fail_closed_and_allows_full_recovery_capacity(tmp_path: Path) -> None:
    empty = TakerChannelInputStore(tmp_path / "empty", max_active_sessions=1)
    registry = TakerChannelInputRegistry.load(
        store=empty, config=_config(), network="regtest", local_actor="taker"
    )
    assert registry.session_count == 0

    negotiation = _new()
    store = TakerChannelInputStore(tmp_path / "full", max_active_sessions=1)
    store.save(negotiation.snapshot())
    loaded = TakerChannelInputRegistry.load(
        store=store, config=_config(), network="regtest", local_actor="taker"
    )
    assert loaded.session_count == 1

    with pytest.raises(TakerChannelInputRuntimeError, match="network"):
        TakerChannelInputRegistry.load(
            store=store, config=_config(), network="signet", local_actor="taker"
        )
    with pytest.raises(TakerChannelInputRuntimeError, match="identity"):
        TakerChannelInputRegistry.load(
            store=store, config=_config(), network="regtest", local_actor="other"
        )


def test_load_rejects_unbacked_hello_evidence_and_duplicate_active_outpoint(tmp_path: Path) -> None:
    legacy = _new()
    _accept_hello(legacy, 0)
    legacy_store = _store(tmp_path / "legacy", legacy)
    with pytest.raises(TakerChannelInputRuntimeError, match="freeze evidence"):
        TakerChannelInputRegistry.load(
            store=legacy_store, config=_config(), network="regtest", local_actor="taker"
        )

    first = _new()
    _add_freeze(first, "maker-a", 0)
    second = TakerChannelInputNegotiation.create(
        network="regtest",
        local_actor="taker",
        selected_nicks=("maker-a",),
        secret_key=(9).to_bytes(32, "big"),
        session_id="22" * 32,
        attempt=1,
        expiry=2_000,
        max_parent_fee_sat=3_000,
        max_fee_rate_sat_vb=100,
        min_recovery_delta=10,
        max_recovery_delta=50,
    )
    _add_freeze(second, "maker-a", 0)
    store = _store(tmp_path / "duplicate", first, second)
    with pytest.raises(TakerChannelInputRuntimeError, match="multiple session attempts"):
        TakerChannelInputRegistry.load(
            store=store,
            config=_config(_candidate(0).funding_outpoint),
            network="regtest",
            local_actor="taker",
        )


def test_expired_canceled_tombstone_is_pruned(tmp_path: Path) -> None:
    negotiation = _new()
    remote = _add_freeze(negotiation, "maker-a", 0)
    negotiation.install_frozen_channel("maker-a", _frozen_snapshot(0))
    cancel = negotiation.plan_cancel("local_freeze_failed")
    negotiation.accept_cancel_ack("maker-a", _cancel_ack(negotiation, cancel, "maker-a", 0))
    negotiation.accept_cancel_ack("maker-b", _unknown_hello_cancel_ack(negotiation, cancel, 1))
    negotiation.accept_cancel_ack("maker-c", _unknown_hello_cancel_ack(negotiation, cancel, 2))
    negotiation.record_local_resumed((remote.funding_outpoint,))
    store = _store(tmp_path, negotiation)

    registry = TakerChannelInputRegistry.load(
        store=store,
        config=_config(_candidate(0).funding_outpoint),
        network="regtest",
        local_actor="taker",
    )

    assert registry.session_count == 0
    assert store.load_all().records == ()


@pytest.mark.asyncio
async def test_freeze_recovery_queries_all_before_each_durable_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    negotiation = _new()
    _freeze_intent(negotiation, 0)
    _freeze_intent(negotiation, 1)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    for index in range(2):
        snapshot = _frozen_snapshot(index)
        backend.frozen[snapshot.channel_id] = snapshot
    original_save = registry._store.save

    def save(snapshot: object) -> None:
        backend.events.append("save")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(registry._store, "save", save)
    await registry.reconcile_all(backend, _node(10))

    expected_finds = [
        f"find:{_candidate(index).funding_outpoint.txid}:{index}" for index in range(2)
    ]
    assert backend.events[:2] == expected_finds
    assert backend.events[2:] == [
        "save",
        f"freeze:{_frozen_snapshot(0).channel_id}",
        "save",
        "save",
        f"freeze:{_frozen_snapshot(1).channel_id}",
        "save",
    ]


@pytest.mark.asyncio
async def test_freeze_recovery_rejects_later_stage_before_mutation(tmp_path: Path) -> None:
    negotiation = _new()
    _freeze_intent(negotiation, 0)
    _freeze_intent(negotiation, 1)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    intent = negotiation.freeze_intents[1][1]
    backend.statuses[(negotiation.invite.session_id, intent.funding_outpoint)] = LndEscrowStatus(
        stage=LndEscrowStage.PARENT_PREPARED,
        parent_txid="00" * 32,
        finalized=False,
    )

    with pytest.raises(TakerChannelInputRuntimeError, match="later escrow"):
        await registry.reconcile_all(backend, _node(10))
    assert not any(event.startswith("freeze:") for event in backend.events)


@pytest.mark.asyncio
async def test_partial_freeze_recovery_replays_and_rejects_conflicting_result(
    tmp_path: Path,
) -> None:
    negotiation = _new()
    _add_freeze(negotiation, "maker-a", 0)
    _add_freeze(negotiation, "maker-b", 1)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()
    first, second = _frozen_snapshot(0), _frozen_snapshot(1)
    backend.frozen[first.channel_id] = first

    with pytest.raises(KeyError):
        await registry.reconcile_all(backend, _node(10))

    recovered = TakerChannelInputRegistry.load(
        store=registry._store,
        config=_config(_candidate(0).funding_outpoint, _candidate(1).funding_outpoint),
        network="regtest",
        local_actor="taker",
    )
    backend.frozen[second.channel_id] = second
    await recovered.reconcile_all(backend, _node(10))
    restored = recovered.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and len(restored.frozen_inputs) == 2

    backend.frozen[first.channel_id] = first.model_copy(update={"channel_id": "bad-result"})
    with pytest.raises(TakerChannelInputRuntimeError, match="conflicts with intent"):
        await recovered.reconcile_all(backend, _node(10))


@pytest.mark.asyncio
async def test_accept_remote_hello_persists_and_replays_exact_freeze(tmp_path: Path) -> None:
    candidate = _candidate(0)
    registry = TakerChannelInputRegistry.load(
        store=TakerChannelInputStore(tmp_path / "new", max_active_sessions=4),
        config=_config(candidate.funding_outpoint),
        network="regtest",
        local_actor="taker",
    )
    negotiation = await registry.create(
        selected_nicks=("maker-a",),
        secret_key=_COORDINATOR_SECRET,
        session_id="11" * 32,
        attempt=1,
        expiry=2_000,
        max_parent_fee_sat=3_000,
        max_fee_rate_sat_vb=100,
        min_recovery_delta=10,
        max_recovery_delta=50,
    )
    backend = FakeBackend()
    frozen = _frozen_snapshot(0)
    backend.frozen[frozen.channel_id] = frozen
    remote = _freeze_remote(0)
    first = await registry.accept_remote_hello(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-a",
        remote,
        backend=backend,
        local_node_key=_node(10),
        candidates=(candidate,),
        settlement_script_pubkey="5120" + "50" * 32,
        quoted_miner_fee_share_sat=1_000,
        policy_ceilings=remote.policy_ceilings,
    )
    second = await registry.accept_remote_hello(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-a",
        remote,
        backend=backend,
        local_node_key=_node(10),
        candidates=(candidate,),
        settlement_script_pubkey="5120" + "50" * 32,
        quoted_miner_fee_share_sat=1_000,
        policy_ceilings=remote.policy_ceilings,
    )
    assert first == second
    assert backend.events.count(f"freeze:{frozen.channel_id}") == 2


@pytest.mark.asyncio
async def test_cancellation_waits_for_ack_before_recording_local_resume(tmp_path: Path) -> None:
    negotiation = _new()
    remote, _ = _freeze_intent(negotiation, 0)
    negotiation.install_frozen_channel("maker-a", _frozen_snapshot(0))
    registry = _registry(tmp_path, negotiation)
    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None
    backend = FakeBackend()
    outpoint = EscrowOutpoint(txid=remote.funding_outpoint.txid, vout=remote.funding_outpoint.vout)
    backend.statuses[(negotiation.invite.session_id, outpoint)] = _locked()

    cancel = await registry.plan_cancellation(
        negotiation.invite.session_id, negotiation.invite.attempt, "local_freeze_failed"
    )
    await registry.attempt_local_cancellation_completion(
        negotiation.invite.session_id, negotiation.invite.attempt, backend
    )
    assert restored.stage is TakerChannelInputStage.CANCEL_INTENDED
    registry = TakerChannelInputRegistry.load(
        store=registry._store,
        config=_config(_candidate(0).funding_outpoint),
        network="regtest",
        local_actor="taker",
    )
    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None

    ack = sign_payload(
        ChannelInputCancelAckPayload(
            session_id=negotiation.invite.session_id,
            attempt=negotiation.invite.attempt,
            signer_key=remote.participant_key,
            transcript_hash=cancel.transcript_hash,
            cancel_hash=cancel_payload_hash(cancel).hex(),
            resumed_outpoints=(remote.funding_outpoint,),
        ),
        _MAKER_SECRETS[0],
        aux_randomness=b"\x01" * 32,
    )
    await registry.accept_maker_cancel_ack(
        negotiation.invite.session_id, negotiation.invite.attempt, "maker-a", ack
    )
    await registry.accept_maker_cancel_ack(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-b",
        _unknown_hello_cancel_ack(negotiation, cancel, 1),
    )
    await registry.accept_maker_cancel_ack(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-c",
        _unknown_hello_cancel_ack(negotiation, cancel, 2),
    )
    await registry.attempt_local_cancellation_completion(
        negotiation.invite.session_id, negotiation.invite.attempt, backend
    )
    assert restored.stage is TakerChannelInputStage.CANCELED


@pytest.mark.asyncio
async def test_cancellation_completes_after_unknown_hello_acks_without_local_escrows(
    tmp_path: Path,
) -> None:
    negotiation = _new()
    registry = _registry(tmp_path, negotiation)
    cancel = await registry.plan_cancellation(
        negotiation.invite.session_id, negotiation.invite.attempt, "hello_timeout"
    )
    for index, nick in enumerate(negotiation.selected_nicks):
        await registry.accept_maker_cancel_ack(
            negotiation.invite.session_id,
            negotiation.invite.attempt,
            nick,
            _unknown_hello_cancel_ack(negotiation, cancel, index),
        )

    backend = FakeBackend()
    await registry.attempt_local_cancellation_completion(
        negotiation.invite.session_id, negotiation.invite.attempt, backend
    )

    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None and restored.stage is TakerChannelInputStage.CANCELED
    assert restored.local_resumed_outpoints == ()
    assert backend.events == []


@pytest.mark.asyncio
async def test_cancellation_preflights_before_mutation_and_orders_resumed_outpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    negotiation = _new()
    first_remote = _add_freeze(negotiation, "maker-a", 1)
    second_remote = _add_freeze(negotiation, "maker-b", 0)
    negotiation.install_frozen_channel("maker-a", _frozen_snapshot(1))
    negotiation.install_frozen_channel("maker-b", _frozen_snapshot(0))
    registry = _registry(tmp_path, negotiation)
    cancel = await registry.plan_cancellation(
        negotiation.invite.session_id, negotiation.invite.attempt, "local_freeze_failed"
    )
    await registry.accept_maker_cancel_ack(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-a",
        _cancel_ack(negotiation, cancel, "maker-a", 1),
    )
    await registry.accept_maker_cancel_ack(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-b",
        _cancel_ack(negotiation, cancel, "maker-b", 0),
    )
    await registry.accept_maker_cancel_ack(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-c",
        _unknown_hello_cancel_ack(negotiation, cancel, 2),
    )
    backend = FakeBackend()
    first_outpoint = EscrowOutpoint(
        txid=first_remote.funding_outpoint.txid, vout=first_remote.funding_outpoint.vout
    )
    second_outpoint = EscrowOutpoint(
        txid=second_remote.funding_outpoint.txid, vout=second_remote.funding_outpoint.vout
    )
    backend.statuses[(negotiation.invite.session_id, first_outpoint)] = _locked()
    backend.statuses[(negotiation.invite.session_id, second_outpoint)] = None
    original_save = registry._store.save

    def save(snapshot: object) -> None:
        backend.events.append("save")
        original_save(snapshot)  # type: ignore[arg-type]

    monkeypatch.setattr(registry._store, "save", save)
    await registry.attempt_local_cancellation_completion(
        negotiation.invite.session_id, negotiation.invite.attempt, backend
    )

    assert backend.events == [
        f"find:{first_outpoint.txid}:{first_outpoint.vout}",
        f"find:{second_outpoint.txid}:{second_outpoint.vout}",
        "save",
        f"cancel:{first_outpoint.txid}:{first_outpoint.vout}",
        "save",
    ]
    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None
    assert restored.local_resumed_outpoints == tuple(
        sorted(
            (first_remote.funding_outpoint, second_remote.funding_outpoint),
            key=lambda x: x.key(),
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [LndEscrowStage.PARENT_SIGNED, LndEscrowStage.FINALIZED])
async def test_cancellation_rejects_signed_or_finalized_escrow_before_mutation(
    tmp_path: Path, stage: LndEscrowStage
) -> None:
    negotiation = _new()
    remote = _add_freeze(negotiation, "maker-a", 0)
    negotiation.install_frozen_channel("maker-a", _frozen_snapshot(0))
    registry = _registry(tmp_path, negotiation)
    await registry.plan_cancellation(
        negotiation.invite.session_id, negotiation.invite.attempt, "local_freeze_failed"
    )
    outpoint = EscrowOutpoint(txid=remote.funding_outpoint.txid, vout=remote.funding_outpoint.vout)
    finalized = (
        LndFinalizedEscrowParent(
            tx_hex="00",
            txid="00" * 32,
            channel_input_index=0,
            channel_input_witness=(b"\x01" * 64,),
        )
        if stage is LndEscrowStage.FINALIZED
        else None
    )
    backend = FakeBackend()
    backend.statuses[(negotiation.invite.session_id, outpoint)] = LndEscrowStatus(
        stage=stage,
        parent_txid="00" * 32,
        finalized=stage is LndEscrowStage.FINALIZED,
        durable_local_public_nonce=b"\x02" * 66,
        durable_remote_public_nonce=b"\x03" * 66,
        durable_local_partial_signature=b"\x04" * 32,
        finalized_parent=finalized,
    )

    with pytest.raises(TakerChannelInputRuntimeError, match="signed or finalized"):
        await registry.attempt_local_cancellation_completion(
            negotiation.invite.session_id, negotiation.invite.attempt, backend
        )
    assert backend.events == [f"find:{outpoint.txid}:{outpoint.vout}"]


@pytest.mark.asyncio
async def test_local_node_mismatch_prevents_all_lnd_calls(tmp_path: Path) -> None:
    negotiation = _new()
    _freeze_intent(negotiation, 0)
    registry = _registry(tmp_path, negotiation)
    backend = FakeBackend()

    with pytest.raises(TakerChannelInputRuntimeError, match="another local node"):
        await registry.reconcile_all(backend, _node(99))
    assert backend.events == []
