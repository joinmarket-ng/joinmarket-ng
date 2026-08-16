"""Focused lifecycle and deterministic-selection tests for escrow deployment."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import create_p2tr_scriptpubkey
from jmswap.channel_escrow import (
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowLifecycleState,
    EscrowOutpoint,
)
from jmswap.lightning import PaymentStatus
from jmswap.peer_transport import EscrowProtocolRole, EscrowPublicKeysPayload
from jmswap.ring_authorization import RingEdgeAuthorizationStore, RingEdgeRole

from maker.config import ChannelInputEscrowConfig, MakerConfig, SwapProviderConfig
from maker.escrow_deployment import (
    ChannelInputEscrowDeployment,
    build_channel_input_escrow_deployment,
)
from maker.escrow_keys import EscrowEndpointRole
from maker.escrow_runtime import EndpointEscrowRuntimePolicy


class FakeRuntime:
    def __init__(self, endpoint_store: Any, terms: ChannelInputEscrowTerms) -> None:
        self.endpoint_store = endpoint_store
        self.terms = terms
        self.recovered: list[tuple[str, ...]] = []
        self.initiated: list[tuple[str, str]] = []
        self.closed = 0

    def recover_known_finalized_ring_edges(self, edge_ids: tuple[str, ...]) -> None:
        self.recovered.append(edge_ids)

    async def initiate(self, edge_id: str, channel_id: str) -> None:
        self.initiated.append((edge_id, channel_id))
        self.endpoint_store.records[edge_id] = endpoint_record(self.terms)

    async def complete_maker_handshake(self, _edge_id: str) -> ChannelInputEscrowTerms:
        return self.terms

    async def close(self) -> None:
        self.closed += 1


class FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resume_durable_sessions(self) -> list[object]:
        self.calls.append("resume")
        return []

    async def prepare_timeout(self, _request: object) -> object:
        raise AssertionError("not used by deployment tests")

    async def authorize_parent(self, _session_id: str) -> object:
        raise AssertionError("not used by deployment tests")


class FakeStore:
    def __init__(self) -> None:
        self.records: dict[str, object] = {}

    def load(self, edge_id: str) -> object | None:
        return self.records.get(edge_id)

    def list(self) -> list[object]:
        return list(self.records.values())


class FakeClosable:
    def __init__(self) -> None:
        self.closed = 0
        self.cancel_external_channel_funding: AsyncMock = AsyncMock()

    async def close(self) -> None:
        self.closed += 1


def policy() -> EndpointEscrowRuntimePolicy:
    return EndpointEscrowRuntimePolicy(
        premium_sats=1_000,
        timeout_compensation_sats=500,
        escrow_value_sats=150_000,
        timeout_fee_sats=500,
        invoice_expiry_seconds=3_600,
        invoice_min_final_cltv_delta=18,
        peer_settle_delta_blocks=24,
        success_broadcast_delta_blocks=36,
        timeout_delta_blocks=72,
        success_confirmation_budget_blocks=6,
        reorg_margin_blocks=6,
        safety_delta_blocks=3,
        preimage_budget_blocks=2,
    )


def terms(edge_id: str = "a" * 64) -> ChannelInputEscrowTerms:
    return ChannelInputEscrowTerms(
        snapshot=ChannelSnapshot(
            session_id=edge_id,
            channel_id=f"{'1' * 64}:1",
            funding_outpoint=EscrowOutpoint(txid="1" * 64, vout=1),
            funding_scriptpubkey=create_p2tr_scriptpubkey(b"\x55" * 32).hex(),
            local_funding_pubkey="02" + "11" * 32,
            remote_funding_pubkey="03" + "22" * 32,
            capacity_sats=200_000,
            maker_claim_sats=160_000,
            peer_claim_sats=40_000,
            quiescent=True,
            active_htlcs=0,
        ),
        authorizer_pubkey="02" + "33" * 32,
        payment_hash="44" * 32,
        invoice="lnbcrt1privateinvoice",
        invoice_payee_pubkey="02" + "66" * 32,
        invoice_payment_secret="77" * 32,
        invoice_amount_msat=41_000_000,
        invoice_expiry_unix=2_000_000_000,
        invoice_min_final_cltv_delta=18,
        invoice_verified=True,
        premium_sats=1_000,
        timeout_compensation_sats=500,
        escrow_value_sats=150_000,
        timeout_fee_sats=500,
        timeout_height=1_000,
        peer_settle_deadline=970,
        success_broadcast_deadline=980,
        parent_confirmation_depth=1,
        success_confirmation_depth=1,
        success_confirmation_budget_blocks=6,
        reorg_margin_blocks=6,
        safety_delta_blocks=3,
        preimage_budget_blocks=2,
    )


def endpoint_record(value: ChannelInputEscrowTerms) -> SimpleNamespace:
    def public_key(secret: int) -> str:
        return PrivateKey(secret.to_bytes(32)).public_key.format(compressed=True).hex()

    local = EscrowPublicKeysPayload(
        role=EscrowProtocolRole.MAKER,
        escrow_pubkey=public_key(1),
        maker_claim_pubkey=public_key(2),
        timeout_destination_scriptpubkey=create_p2tr_scriptpubkey(b"\xaa" * 32).hex(),
    )
    remote = EscrowPublicKeysPayload(
        role=EscrowProtocolRole.PEER,
        escrow_pubkey=public_key(3),
        timeout_destination_scriptpubkey=create_p2tr_scriptpubkey(b"\xbb" * 32).hex(),
    )
    return SimpleNamespace(
        role=EscrowEndpointRole.MAKER,
        local_keys=local,
        remote_keys=remote,
        terms=value,
        snapshot=value.snapshot,
    )


def ring_record(
    edge_id: str,
    role: RingEdgeRole,
    *,
    finalized: bool = True,
    capacity: int = 200_000,
    receiver_contribution: int = 40_000,
) -> SimpleNamespace:
    authorization = SimpleNamespace(
        edge_id=edge_id,
        capacity_sats=capacity,
        receiver_contribution_sats=receiver_contribution,
        role_for=lambda _nick: role,
    )
    return SimpleNamespace(
        authorization=authorization,
        local_nick="local",
        channel_point=SimpleNamespace(txid="1" * 64, vout=1) if finalized else None,
    )


def deployment(
    records: list[SimpleNamespace],
) -> tuple[ChannelInputEscrowDeployment, FakeRuntime, FakeClosable, FakeClosable]:
    endpoint_store = FakeStore()
    coordinator_store = FakeStore()
    value = terms()
    runtime = FakeRuntime(endpoint_store, value)
    coordinator = FakeCoordinator()
    auth_store = SimpleNamespace(list=lambda: records)
    lnd = FakeClosable()
    rpc = FakeClosable()
    instance = ChannelInputEscrowDeployment(
        runtime=runtime,  # type: ignore[arg-type]
        coordinator=coordinator,  # type: ignore[arg-type]
        authorization_store=auth_store,  # type: ignore[arg-type]
        endpoint_store=endpoint_store,  # type: ignore[arg-type]
        coordinator_store=coordinator_store,  # type: ignore[arg-type]
        lnd=lnd,  # type: ignore[arg-type]
        rpc=rpc,  # type: ignore[arg-type]
        policy=policy(),
    )
    return instance, runtime, lnd, rpc


class MonitoringCoordinator:
    def __init__(self) -> None:
        self.observed: dict[str, object] = {}
        self.calls: list[tuple[object, ...]] = []
        self.failures: set[str] = set()

    async def observe_chain(self, session_id: str) -> object:
        self.calls.append(("observe_chain", session_id))
        if session_id in self.failures:
            raise RuntimeError("isolated monitor failure")
        return self.observed[session_id]

    async def start_payment(
        self, session_id: str, excluded_channel_ids: tuple[str, ...], requested_at_unix: int
    ) -> object:
        self.calls.append(("start_payment", session_id, excluded_channel_ids, requested_at_unix))
        return self.observed[session_id]

    async def observe_payment(self, session_id: str) -> object:
        self.calls.append(("observe_payment", session_id))
        return self.observed[session_id]

    async def ensure_success_spend(self, session_id: str) -> object:
        self.calls.append(("ensure_success_spend", session_id))
        return self.observed[session_id]

    async def broadcast_timeout(self, session_id: str) -> object:
        self.calls.append(("broadcast_timeout", session_id))
        return self.observed[session_id]

    async def fee_bump_success(self, session_id: str) -> object:
        self.calls.append(("fee_bump_success", session_id))
        return self.observed[session_id]

    async def recover_reorg(self, session_id: str) -> object:
        self.calls.append(("recover_reorg", session_id))
        return self.observed[session_id]


def monitor_deployment(
    coordinator: MonitoringCoordinator,
    records: list[SimpleNamespace] | None = None,
) -> ChannelInputEscrowDeployment:
    endpoint_store = FakeStore()
    coordinator_store = FakeStore()
    coordinator_store.records = {str(record.session_id): record for record in records or []}
    runtime = FakeRuntime(endpoint_store, terms())
    return ChannelInputEscrowDeployment(
        runtime=runtime,  # type: ignore[arg-type]
        coordinator=coordinator,  # type: ignore[arg-type]
        authorization_store=SimpleNamespace(list=lambda: []),  # type: ignore[arg-type]
        endpoint_store=endpoint_store,  # type: ignore[arg-type]
        coordinator_store=coordinator_store,  # type: ignore[arg-type]
        lnd=FakeClosable(),  # type: ignore[arg-type]
        rpc=FakeClosable(),  # type: ignore[arg-type]
        policy=policy(),
        monitor_poll_interval_seconds=0.01,
        clock=lambda: 1_900_000_000,
    )


def monitor_record(
    state: EscrowLifecycleState,
    *,
    edge_id: str = "a" * 64,
    height: int = 950,
    payment_status: PaymentStatus | None = None,
    payment_request: object | None = None,
    success_broadcast_height: int = 949,
) -> SimpleNamespace:
    value = terms(edge_id)
    return SimpleNamespace(
        session_id=edge_id,
        state=state,
        state_before_reorg=None,
        terms=value,
        negotiated_parent_evidence=None,
        peer_settle_deadline_violated=False,
        payment_request=payment_request,
        payment_result=(
            SimpleNamespace(status=payment_status) if payment_status is not None else None
        ),
        chain_observation=SimpleNamespace(
            observed_height=height,
            success_confirmations=0,
            timeout_confirmations=0,
        ),
        success_spend=SimpleNamespace(broadcast_height=success_broadcast_height),
    )


@pytest.mark.asyncio
async def test_start_recovers_finalized_edges_before_plan_use_and_is_idempotent() -> None:
    instance, runtime, _lnd, _rpc = deployment(
        [
            ring_record("b" * 64, RingEdgeRole.RECEIVER),
            ring_record("a" * 64, RingEdgeRole.RECEIVER, finalized=False),
        ]
    )

    assert await instance.plan_for_authenticated_fill() is None
    await instance.start()
    await instance.start()

    assert runtime.recovered == [("b" * 64,)]
    assert await instance.plan_for_authenticated_fill() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("funding_address", "funding_canceled"),
    [(None, False), ("bcrt1pending", True)],
)
async def test_start_cancels_an_interrupted_ring_open_before_admission(
    funding_address: str | None, funding_canceled: bool
) -> None:
    instance, runtime, lnd, _rpc = deployment([])
    authorization = SimpleNamespace(edge_id="a" * 64, authorization_id="b" * 64)
    pending_record = SimpleNamespace(
        authorization=authorization,
        pending_channel_id="c" * 64,
        funding_address=funding_address,
        funding_canceled=funding_canceled,
        funding_cancel_completed=False,
        funding_completed=False,
        channel_point=None,
    )
    authorization_store = MagicMock()
    authorization_store.list.return_value = [pending_record]
    instance.authorization_store = authorization_store
    lnd.cancel_external_channel_funding = AsyncMock()

    await instance.start()

    if funding_canceled:
        authorization_store.cancel_funding.assert_not_called()
    else:
        authorization_store.cancel_funding.assert_called_once_with("a" * 64, "b" * 64)
    lnd.cancel_external_channel_funding.assert_awaited_once_with("c" * 64)
    authorization_store.complete_funding_cancellation.assert_called_once_with("a" * 64, "b" * 64)
    assert runtime.recovered == [()]
    await instance.close()


@pytest.mark.asyncio
async def test_start_fails_closed_when_pending_ring_cancellation_fails() -> None:
    instance, runtime, lnd, _rpc = deployment([])
    authorization = SimpleNamespace(edge_id="a" * 64, authorization_id="b" * 64)
    pending_record = SimpleNamespace(
        authorization=authorization,
        pending_channel_id="c" * 64,
        funding_address=None,
        funding_canceled=True,
        funding_cancel_completed=False,
        funding_completed=False,
        channel_point=None,
    )
    authorization_store = MagicMock()
    authorization_store.list.return_value = [pending_record]
    instance.authorization_store = authorization_store
    lnd.cancel_external_channel_funding = AsyncMock(side_effect=RuntimeError("LND unavailable"))

    with pytest.raises(RuntimeError, match="LND unavailable"):
        await instance.start()

    authorization_store.complete_funding_cancellation.assert_not_called()
    assert runtime.recovered == []
    assert instance._failed is True


@pytest.mark.asyncio
async def test_plan_selects_smallest_finalized_funder_once_and_never_arbitrary_peer() -> None:
    instance, runtime, _lnd, _rpc = deployment(
        [
            ring_record("0" * 64, RingEdgeRole.FUNDER, capacity=100_000),
            ring_record("1" * 64, RingEdgeRole.FUNDER, receiver_contribution=149_000),
            ring_record("c" * 64, RingEdgeRole.FUNDER, finalized=False),
            ring_record("b" * 64, RingEdgeRole.RECEIVER),
            ring_record("a" * 64, RingEdgeRole.FUNDER),
        ]
    )
    await instance.start()

    first, second = await asyncio.gather(
        instance.plan_for_authenticated_fill(), instance.plan_for_authenticated_fill()
    )

    assert first is not None
    assert second is None
    assert runtime.initiated == [("a" * 64, f"{'1' * 64}:1")]


@pytest.mark.asyncio
async def test_close_is_idempotent_and_prevents_further_selection() -> None:
    instance, runtime, lnd, rpc = deployment([ring_record("a" * 64, RingEdgeRole.FUNDER)])
    await instance.start()
    await instance.close()
    await instance.close()

    assert runtime.closed == 1
    assert lnd.closed == 1
    assert rpc.closed == 1
    assert await instance.plan_for_authenticated_fill() is None


@pytest.mark.asyncio
async def test_monitor_starts_payment_with_consumed_channel_excluded() -> None:
    coordinator = MonitoringCoordinator()
    record = monitor_record(EscrowLifecycleState.PARENT_CONFIRMED)
    coordinator.observed[record.session_id] = record
    instance = monitor_deployment(coordinator)

    await instance._monitor_session_once(record)

    assert coordinator.calls == [
        ("observe_chain", record.session_id),
        (
            "start_payment",
            record.session_id,
            (record.terms.snapshot.channel_id,),
            1_900_000_000,
        ),
    ]


@pytest.mark.asyncio
async def test_monitor_does_not_start_payment_outside_cltv_budget() -> None:
    coordinator = MonitoringCoordinator()
    record = monitor_record(EscrowLifecycleState.PARENT_CONFIRMED, height=963)
    coordinator.observed[record.session_id] = record
    instance = monitor_deployment(coordinator)

    await instance._monitor_session_once(record)

    assert coordinator.calls == [("observe_chain", record.session_id)]


@pytest.mark.asyncio
async def test_monitor_times_out_failed_payment_only_after_maturity() -> None:
    coordinator = MonitoringCoordinator()
    immature = monitor_record(
        EscrowLifecycleState.PAYMENT_IN_FLIGHT,
        height=1_000,
        payment_status=PaymentStatus.FAILED,
    )
    coordinator.observed[immature.session_id] = immature
    instance = monitor_deployment(coordinator)

    await instance._monitor_session_once(immature)
    assert coordinator.calls == [("observe_chain", immature.session_id)]

    mature = monitor_record(
        EscrowLifecycleState.PAYMENT_IN_FLIGHT,
        height=1_001,
        payment_status=PaymentStatus.FAILED,
    )
    coordinator.observed[mature.session_id] = mature
    await instance._monitor_session_once(mature)
    assert coordinator.calls[-2:] == [
        ("observe_chain", mature.session_id),
        ("broadcast_timeout", mature.session_id),
    ]


@pytest.mark.asyncio
async def test_monitor_fee_bumps_once_per_new_block_before_deadline() -> None:
    coordinator = MonitoringCoordinator()
    record = monitor_record(
        EscrowLifecycleState.SUCCESS_SPEND_PENDING,
        height=979,
        success_broadcast_height=978,
    )
    coordinator.observed[record.session_id] = record
    instance = monitor_deployment(coordinator)

    await instance._monitor_session_once(record)

    assert coordinator.calls == [
        ("observe_chain", record.session_id),
        ("fee_bump_success", record.session_id),
    ]
    coordinator.calls.clear()
    at_deadline = monitor_record(
        EscrowLifecycleState.SUCCESS_SPEND_PENDING,
        height=981,
        success_broadcast_height=980,
    )
    coordinator.observed[at_deadline.session_id] = at_deadline
    await instance._monitor_session_once(at_deadline)
    assert coordinator.calls == [("observe_chain", at_deadline.session_id)]


@pytest.mark.asyncio
async def test_monitor_session_failures_do_not_block_other_sessions() -> None:
    coordinator = MonitoringCoordinator()
    failed = monitor_record(EscrowLifecycleState.PARENT_PENDING, edge_id="a" * 64)
    healthy = monitor_record(EscrowLifecycleState.PARENT_PENDING, edge_id="b" * 64)
    coordinator.observed[healthy.session_id] = healthy
    coordinator.failures.add(failed.session_id)
    instance = monitor_deployment(coordinator, [failed, healthy])

    instance._schedule_monitor_pass()
    await asyncio.sleep(0)
    instance._reap_monitor_tasks()

    assert ("observe_chain", failed.session_id) in coordinator.calls
    assert ("observe_chain", healthy.session_id) in coordinator.calls


@pytest.mark.asyncio
async def test_builder_rejects_disabled_escrow_and_missing_rpc_dependencies(tmp_path) -> None:
    ring_store = RingEdgeAuthorizationStore(tmp_path / "ring")
    disabled = MakerConfig(mnemonic="test " * 12)
    with pytest.raises(ValueError, match="not enabled"):
        await build_channel_input_escrow_deployment(  # type: ignore[arg-type]
            object(), object(), disabled, ring_store
        )

    enabled = MakerConfig(
        mnemonic="test " * 12,
        channel_input_escrow=ChannelInputEscrowConfig(enabled=True),
        swap_provider=SwapProviderConfig(
            lnd_grpc_url="127.0.0.1:10009",
            lnd_tls_cert_path="/missing/tls.cert",
            lnd_macaroon_path="/missing/admin.macaroon",
            lnd_escrow_macaroon_path="/missing/escrow.macaroon",
        ),
    )
    with pytest.raises(ValueError, match="backend_config"):
        await build_channel_input_escrow_deployment(  # type: ignore[arg-type]
            object(), object(), enabled, ring_store
        )
