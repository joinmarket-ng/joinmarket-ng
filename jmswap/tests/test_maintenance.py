from __future__ import annotations

import asyncio
import hashlib
import json
import multiprocessing
import stat
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from jmcore.channel_maintenance import ChannelMaintenanceConfig
from jmcore.constants import MAX_MONEY

import jmswap.maintenance as maintenance
from jmswap.lnd import (
    ChannelInputCandidate,
    EscrowOutpoint,
    MaintenanceChannel,
    MaintenanceInvoice,
    MaintenancePayment,
    PeerRouteHop,
)
from jmswap.maintenance import (
    MAINTENANCE_MESSAGE_TYPE,
    MAX_PEER_REQUESTS_PER_MINUTE,
    MAX_WIRE_EXPIRY,
    MaintenanceLockError,
    MaintenanceService,
    RouteRequest,
    RouteResponse,
    _PendingRouteRequest,
    enroll_channel,
)

LOCAL = "02" + "11" * 32
PEER_B = "03" + "22" * 32
PEER_C = "02" + "33" * 32
PEER_D = "03" + "44" * 32
POINT_B = "aa" * 32 + ":1"
POINT_C = "bb" * 32 + ":2"
ORIGIN_B = "cc" * 32
ORIGIN_C = "dd" * 32
REQUEST_ONE = f"{1:064x}"


class FixedRandom:
    def __init__(self) -> None:
        self.counter = 0

    def randint(self, minimum: int, _maximum: int) -> int:
        return minimum

    def token_hex(self, nbytes: int) -> str:
        self.counter += 1
        return f"{self.counter:064x}"[-(nbytes * 2) :]


class FakeBackend:
    def __init__(self) -> None:
        self.channels: tuple[MaintenanceChannel, ...] = ()
        self.candidates: tuple[ChannelInputCandidate, ...] = ()
        self.fee_calls: list[str] = []
        self.messages: list[tuple[str, int, dict[str, Any]]] = []
        self.invoices: list[tuple[int, tuple[PeerRouteHop, ...], int]] = []
        self.send_calls: list[tuple[str, int, int]] = []
        self.track_calls: list[str] = []
        self.payment = MaintenancePayment(status="succeeded", fee_msat=0)
        self.track_payment = MaintenancePayment(status="unknown", fee_msat=None)
        self.fail_fee_write = False
        self.cancel_payment = False
        self.response: dict[str, Any] | None = None
        self.service: MaintenanceService | None = None
        self._message_event = asyncio.Event()
        self.subscription_calls = 0
        self.subscription_eofs = 0

    async def list_maintenance_channels(self) -> tuple[MaintenanceChannel, ...]:
        return self.channels

    async def reconcile_maintenance_invoice(
        self, payment_hash: str, preimage: str, return_channel_id: int, amount_sat: int
    ) -> None:
        raise AssertionError("legacy invoice fixture must not require hold reconciliation")

    async def list_channel_input_candidates(self) -> tuple[ChannelInputCandidate, ...]:
        return self.candidates

    async def set_private_channel_zero_fee(self, channel_point: str) -> None:
        self.fee_calls.append(channel_point)
        if self.fail_fee_write:
            raise TimeoutError("unknown policy write")

    async def send_peer_message(self, peer: str, message_type: int, payload: bytes) -> None:
        self.messages.append((peer, message_type, json.loads(payload)))
        if self.response is not None and self.service is not None:
            response = self.response
            self.response = None
            await self.service.handle_peer_message(peer, json.dumps(response).encode("ascii"))

    async def create_maintenance_invoice(
        self, amount_sat: int, hops: Sequence[PeerRouteHop], expiry_seconds: int
    ) -> MaintenanceInvoice:
        self.invoices.append((amount_sat, tuple(hops), expiry_seconds))
        return MaintenanceInvoice("ee" * 32, "lnbcrt-maintenance", 130)

    async def send_maintenance_payment(
        self, payment_request: str, outgoing_channel_id: int, timeout_seconds: int
    ) -> MaintenancePayment:
        self.send_calls.append((payment_request, outgoing_channel_id, timeout_seconds))
        if self.cancel_payment:
            raise asyncio.CancelledError
        return self.payment

    async def track_maintenance_payment(
        self, payment_hash: str, timeout_seconds: int = 5
    ) -> MaintenancePayment:
        self.track_calls.append(payment_hash)
        return self.track_payment

    async def subscribe_peer_messages(self, _message_type: int) -> AsyncIterator[tuple[str, bytes]]:
        self.subscription_calls += 1
        if self.subscription_eofs:
            self.subscription_eofs -= 1
            return
        while True:
            await self._message_event.wait()
            self._message_event.clear()
            yield PEER_B, b""


def channel(
    point: str,
    peer: str,
    channel_id: int,
    *,
    local_fee: int = 0,
    remote_fee: int = 0,
    local_alias: int | None = 1_001,
    peer_alias: int | None = 2_001,
    local_cltv_delta: int = 40,
    remote_cltv_delta: int = 50,
) -> MaintenanceChannel:
    return MaintenanceChannel(
        channel_point=point,
        channel_id=channel_id,
        peer_node_id=peer,
        active=True,
        private=True,
        local_balance_sat=200_000,
        remote_balance_sat=200_000,
        local_reserve_sat=10_000,
        remote_reserve_sat=10_000,
        pending_htlc_count=0,
        local_alias=local_alias,
        peer_alias=peer_alias,
        local_base_fee_msat=local_fee,
        local_fee_ppm=0,
        remote_base_fee_msat=remote_fee,
        remote_fee_ppm=0,
        local_cltv_delta=local_cltv_delta,
        remote_cltv_delta=remote_cltv_delta,
    )


def candidate(point: str, peer: str, channel_id: int) -> ChannelInputCandidate:
    txid, vout = point.split(":")
    return ChannelInputCandidate(
        channel_id=channel_id,
        funding_outpoint=EscrowOutpoint(txid=txid, vout=int(vout)),
        peer_node_pubkey=peer,
        capacity_sat=400_000,
        local_balance_sat=200_000,
        remote_balance_sat=200_000,
        private=True,
        active=True,
        default_channel_state=True,
        pending_htlc_count=0,
        commitment_type=7,
    )


def config(**changes: Any) -> ChannelMaintenanceConfig:
    values: dict[str, Any] = {"interval_min_seconds": 10, "interval_max_seconds": 20}
    values.update(changes)
    return ChannelMaintenanceConfig.model_validate(values)


def service(
    tmp_path: Path,
    backend: FakeBackend,
    *,
    now: int = 100,
    policy: ChannelMaintenanceConfig | None = None,
) -> tuple[MaintenanceService, list[int]]:
    current = [now]
    target = MaintenanceService(
        backend,
        policy or config(),
        tmp_path,
        LOCAL,
        clock=lambda: float(current[0]),
        random_source=FixedRandom(),
    )
    backend.service = target
    return target, current


async def enroll_and_initialize(tmp_path: Path, backend: FakeBackend) -> MaintenanceService:
    assert enroll_channel(tmp_path, LOCAL, PEER_B, POINT_B, ORIGIN_B)
    assert enroll_channel(tmp_path, LOCAL, PEER_C, POINT_C, ORIGIN_C)
    backend.channels = (channel(POINT_B, PEER_B, 101), channel(POINT_C, PEER_C, 102))
    backend.candidates = (candidate(POINT_B, PEER_B, 101), candidate(POINT_C, PEER_C, 102))
    target, _current = service(tmp_path, backend)
    await target.tick()
    return target


def read_state(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "maintenance-v1.json").read_text())


def write_journal(tmp_path: Path, phase: str, *, expires_at: int = 130) -> None:
    state = read_state(tmp_path)
    state["payment"] = {
        "amount_sat": 1_000,
        "expires_at": expires_at,
        "fee_budget_msat": 0,
        "middle_peer_node_id": PEER_B,
        "outgoing_channel_id": 101,
        "outgoing_channel_point": POINT_B,
        "payment_hash": "ee" * 32,
        "payment_request": "lnbcrt-maintenance",
        "phase": phase,
        "return_channel_point": POINT_C,
        "return_peer_node_id": PEER_C,
    }
    (tmp_path / "maintenance-v1.json").write_text(json.dumps(state))


def route_response(request_id: str) -> dict[str, Any]:
    return {
        "version": 1,
        "kind": "route_response",
        "request_id": request_id,
        "hop": {
            "node_id": PEER_B,
            "channel_id": 9_001,
            "base_fee_msat": 0,
            "fee_ppm": 0,
            "cltv_delta": 42,
        },
        "expiry": 105,
    }


def _race_admission(directory: str, point: str, operation: str, barrier: Any, queue: Any) -> None:
    barrier.wait(timeout=10)
    if operation == "spend":
        result = maintenance.claim_spend_channels(directory, LOCAL, "spend", [point])
    else:
        target, _ = service(Path(directory), FakeBackend())
        attempt = maintenance.PaymentAttempt.model_validate(read_state(Path(directory))["payment"])
        result = target._replace_payment(
            attempt, attempt.model_copy(update={"phase": "dispatch_intent"})
        )
    queue.put((operation, result))


@pytest.mark.parametrize("point", [POINT_B, POINT_C])
async def test_spend_and_dispatch_are_atomic_across_processes(tmp_path: Path, point: str) -> None:
    await enroll_and_initialize(tmp_path, FakeBackend())
    write_journal(tmp_path, "created_invoice")
    context = multiprocessing.get_context("fork")
    barrier = context.Barrier(2)
    queue = context.Queue()
    processes = [
        context.Process(
            target=_race_admission, args=(str(tmp_path), point, operation, barrier, queue)
        )
        for operation in ("spend", "payment")
    ]
    try:
        for process in processes:
            process.start()
        results = dict(queue.get(timeout=15) for _ in processes)
        assert sum(results.values()) == 1
        state = read_state(tmp_path)
        assert (state["payment"]["phase"] == "dispatch_intent") == results["payment"]
        assert (point in state["spend_guards"]) == results["spend"]
    finally:
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        queue.close()
    assert all(process.exitcode == 0 for process in processes)


def test_spend_claim_is_durable_idempotent_and_all_or_none(tmp_path: Path) -> None:
    assert maintenance.claim_spend_channels(tmp_path, LOCAL, "first", [POINT_B])
    assert maintenance.claim_spend_channels(tmp_path, LOCAL, "first", [POINT_B])
    assert not maintenance.claim_spend_channels(tmp_path, LOCAL, "second", [POINT_C, POINT_B])
    assert read_state(tmp_path)["spend_guards"] == {POINT_B: "first"}
    maintenance.release_spend_channels(tmp_path, LOCAL, "second", [POINT_B])
    assert not maintenance.claim_spend_channels(tmp_path, LOCAL, "second", [POINT_B])
    maintenance.release_spend_channels(tmp_path, LOCAL, "first", [POINT_B])
    assert maintenance.claim_spend_channels(tmp_path, LOCAL, "second", [POINT_B, POINT_C])
    with pytest.raises(maintenance.MaintenanceStateError, match="another LND node"):
        maintenance.claim_spend_channels(tmp_path, PEER_B, "foreign", [POINT_C])
    with pytest.raises(maintenance.MaintenanceStateError, match="another LND node"):
        enroll_channel(tmp_path, PEER_B, LOCAL, POINT_B, ORIGIN_B)


@pytest.mark.parametrize("point", [POINT_B, POINT_C])
async def test_spend_claim_preempts_created_invoice_and_blocks_dispatch(
    tmp_path: Path, point: str
) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    write_journal(tmp_path, "created_invoice")
    attempt = maintenance.PaymentAttempt.model_validate(read_state(tmp_path)["payment"])
    assert maintenance.claim_spend_channels(tmp_path, LOCAL, "spend", [point])
    assert not target._replace_payment(
        attempt, attempt.model_copy(update={"phase": "dispatch_intent"})
    )
    assert all(channel.channel_point != point for channel, _ in await target._eligible_channels())
    await target.tick()
    assert not backend.send_calls


@pytest.mark.parametrize("phase", ["dispatch_intent", "unresolved"])
@pytest.mark.parametrize("point", [POINT_B, POINT_C])
async def test_dispatched_payment_blocks_spend_claim(
    tmp_path: Path, point: str, phase: str
) -> None:
    await enroll_and_initialize(tmp_path, FakeBackend())
    write_journal(tmp_path, phase)
    assert not maintenance.claim_spend_channels(tmp_path, LOCAL, "spend", [point])
    assert read_state(tmp_path)["spend_guards"] == {}


@pytest.mark.parametrize("status", ["pending_validation", "enrolled"])
async def test_legacy_admission_remains_paused_without_fee_reset(
    tmp_path: Path, status: str
) -> None:
    backend = FakeBackend()
    await enroll_and_initialize(tmp_path, backend)
    state = read_state(tmp_path)
    for key in ("node_id", "admission_channels", "spend_guards"):
        state.pop(key)
    for record in state["enrollments"].values():
        record["status"] = status
    state["next_due"] = 100
    (tmp_path / "maintenance-v1.json").write_text(json.dumps(state))
    backend.fee_calls.clear()
    target, _ = service(tmp_path, backend)
    assert not enroll_channel(tmp_path, LOCAL, PEER_B, POINT_B, ORIGIN_B)
    await target.tick()
    assert not backend.fee_calls
    assert not backend.invoices
    assert not backend.send_calls
    assert maintenance.maintenance_status(tmp_path, LOCAL)["unreconciled_admission_count"] == 2
    write_journal(tmp_path, "unresolved")
    backend.track_payment = MaintenancePayment(status="failed", fee_msat=None)
    await target.tick()
    assert backend.track_calls == ["ee" * 32]
    assert read_state(tmp_path)["payment"] is None


async def test_explicit_enrollment_is_affirmative_and_never_overwrites(tmp_path: Path) -> None:
    assert enroll_channel(tmp_path, LOCAL, PEER_B, POINT_B, ORIGIN_B)
    assert not enroll_channel(tmp_path, LOCAL, PEER_C, POINT_B, ORIGIN_C)

    state = read_state(tmp_path)
    record = state["enrollments"][POINT_B]
    assert record == {
        "channel_point": POINT_B,
        "node_id": LOCAL,
        "origin_id": ORIGIN_B,
        "peer_node_id": PEER_B,
        "status": "pending_validation",
    }


async def test_hold_invoice_settles_concurrently_with_journaled_dispatch(tmp_path: Path) -> None:
    preimage = "ab" * 32
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()

    class HeldBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__()
            self.reconciled = asyncio.Event()

        async def create_maintenance_invoice(
            self, amount_sat: int, hops: Sequence[PeerRouteHop], expiry_seconds: int
        ) -> MaintenanceInvoice:
            return MaintenanceInvoice(payment_hash, "lnbcrt-held", 130, preimage)

        async def reconcile_maintenance_invoice(
            self,
            observed_hash: str,
            observed_preimage: str,
            return_channel_id: int,
            amount_sat: int,
        ) -> None:
            assert (observed_hash, observed_preimage, return_channel_id) == (
                payment_hash,
                preimage,
                102,
            )
            journal = read_state(tmp_path)["payment"]
            assert journal["phase"] == "dispatch_intent"
            assert journal["invoice_preimage"] == preimage
            assert journal["amount_sat"] == amount_sat
            self.reconciled.set()

        async def send_maintenance_payment(
            self, payment_request: str, outgoing_channel_id: int, timeout_seconds: int
        ) -> MaintenancePayment:
            await asyncio.wait_for(self.reconciled.wait(), timeout=1)
            return await super().send_maintenance_payment(
                payment_request, outgoing_channel_id, timeout_seconds
            )

    backend = HeldBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    backend.response = route_response(REQUEST_ONE)
    target._clock = lambda: 110.0
    backend.response["expiry"] = 115
    await target.tick()
    assert backend.reconciled.is_set()
    assert len(backend.send_calls) == 1
    assert read_state(tmp_path)["payment"] is None


async def test_restart_reconciles_held_invoice_without_resending(tmp_path: Path) -> None:
    preimage = "ab" * 32
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    observed: list[str] = []

    class HeldBackend(FakeBackend):
        async def reconcile_maintenance_invoice(
            self,
            observed_hash: str,
            observed_preimage: str,
            return_channel_id: int,
            amount_sat: int,
        ) -> None:
            assert (observed_hash, observed_preimage, return_channel_id, amount_sat) == (
                payment_hash,
                preimage,
                102,
                1_000,
            )
            observed.append("invoice")

        async def track_maintenance_payment(
            self, observed_hash: str, timeout_seconds: int = 5
        ) -> MaintenancePayment:
            assert observed == ["invoice"]
            return MaintenancePayment(status="succeeded", fee_msat=0)

    backend = HeldBackend()
    await enroll_and_initialize(tmp_path, backend)
    write_journal(tmp_path, "unresolved")
    state = read_state(tmp_path)
    state["payment"].update(
        payment_hash=payment_hash, invoice_preimage=preimage, return_channel_id=102
    )
    (tmp_path / "maintenance-v1.json").write_text(json.dumps(state))
    restarted, _ = service(tmp_path, backend)
    await restarted.tick()
    assert not backend.send_calls
    assert read_state(tmp_path)["payment"] is None


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"version": 0, "enrollments": {}},
        {"version": 1, "enrollments": {POINT_B: {"node_id": LOCAL}}},
    ],
)
async def test_absent_legacy_or_partial_state_is_inert(
    tmp_path: Path, document: dict[str, Any]
) -> None:
    if document:
        (tmp_path / "maintenance-v1.json").write_text(json.dumps(document))
    backend = FakeBackend()
    backend.channels = (channel(POINT_B, PEER_B, 101),)
    backend.candidates = (candidate(POINT_B, PEER_B, 101),)
    target, _current = service(tmp_path, backend)

    await target.tick()

    assert backend.fee_calls == []
    assert backend.send_calls == []


async def test_unknown_fee_initialization_is_not_retried(tmp_path: Path) -> None:
    backend = FakeBackend()
    assert enroll_channel(tmp_path, LOCAL, PEER_B, POINT_B, ORIGIN_B)
    backend.channels = (channel(POINT_B, PEER_B, 101),)
    backend.candidates = (candidate(POINT_B, PEER_B, 101),)
    backend.fail_fee_write = True
    target, _current = service(tmp_path, backend)

    await target.tick()
    await target.tick()

    assert backend.fee_calls == [POINT_B]
    assert read_state(tmp_path)["enrollments"][POINT_B]["status"] == "fee_init_unknown"


async def test_fee_initialization_requires_durable_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend()
    assert enroll_channel(tmp_path, LOCAL, PEER_B, POINT_B, ORIGIN_B)
    backend.channels = (channel(POINT_B, PEER_B, 101),)
    backend.candidates = (candidate(POINT_B, PEER_B, 101),)
    target, _current = service(tmp_path, backend)

    def fail_write(_path: Path, _data: bytes) -> None:
        raise OSError("simulated durable write failure")

    monkeypatch.setattr(maintenance, "atomic_write_private", fail_write)
    await target.tick()

    assert backend.fee_calls == []
    assert read_state(tmp_path)["enrollments"][POINT_B]["status"] == "pending_validation"


async def test_maintenance_success_uses_zero_fee_triangle_and_journal(tmp_path: Path) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    current = [100]
    target._clock = lambda: float(current[0])
    target._set_next_due(100)
    backend.response = {
        "version": 1,
        "kind": "route_response",
        "request_id": REQUEST_ONE,
        "hop": {
            "node_id": PEER_B,
            "channel_id": 9_001,
            "base_fee_msat": 0,
            "fee_ppm": 0,
            "cltv_delta": 42,
        },
        "expiry": 105,
    }

    await target.tick()

    assert backend.fee_calls == [POINT_B, POINT_C]
    assert backend.messages[0] == (
        PEER_B,
        MAINTENANCE_MESSAGE_TYPE,
        {
            "amount_sat": 1_000,
            "destination_node_id": PEER_C,
            "expiry": 105,
            "kind": "route_request",
            "request_id": REQUEST_ONE,
            "version": 1,
        },
    )
    assert backend.invoices == [
        (
            1_000,
            (
                PeerRouteHop(PEER_B, 9_001, 0, 0, 42),
                PeerRouteHop(PEER_C, 2_001, 0, 0, 50),
            ),
            30,
        )
    ]
    assert backend.send_calls == [("lnbcrt-maintenance", 101, 30)]
    assert read_state(tmp_path)["payment"] is None


async def test_invoice_journal_write_failure_prevents_payment_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    target._set_next_due(100)
    backend.response = route_response(REQUEST_ONE)

    def fail_write(_path: Path, _data: bytes) -> None:
        raise OSError("simulated invoice journal failure")

    monkeypatch.setattr(maintenance, "atomic_write_private", fail_write)
    await target.tick()

    assert len(backend.invoices) == 1
    assert backend.send_calls == []
    assert read_state(tmp_path)["payment"] is None


async def test_dispatch_intent_write_failure_prevents_payment_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    target._set_next_due(100)
    backend.response = route_response(REQUEST_ONE)
    original_write = maintenance.atomic_write_private
    writes = 0

    def fail_second_write(path: Path, data: bytes) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("simulated dispatch intent failure")
        original_write(path, data)

    monkeypatch.setattr(maintenance, "atomic_write_private", fail_second_write)
    await target.tick()

    assert writes == 2
    assert backend.send_calls == []
    assert read_state(tmp_path)["payment"]["phase"] == "created_invoice"


@pytest.mark.parametrize("local_fee,remote_fee", [(1, 0), (0, 1)])
async def test_raised_fee_or_no_triangle_skips_payment(
    tmp_path: Path, local_fee: int, remote_fee: int
) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    backend.channels = (
        channel(POINT_B, PEER_B, 101, local_fee=local_fee),
        channel(POINT_C, PEER_C, 102, remote_fee=remote_fee),
    )
    target._set_next_due(100)

    await target.tick()

    assert backend.invoices == []
    assert backend.send_calls == []


@pytest.mark.parametrize(
    ("local_cltv_delta", "remote_cltv_delta"),
    [(0, 50), (145, 50), (40, 0), (40, 145)],
)
async def test_invalid_channel_cltv_delta_is_ineligible(
    tmp_path: Path, local_cltv_delta: int, remote_cltv_delta: int
) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    backend.channels = (
        channel(POINT_B, PEER_B, 101, local_cltv_delta=local_cltv_delta),
        channel(POINT_C, PEER_C, 102, remote_cltv_delta=remote_cltv_delta),
    )
    target._set_next_due(100)

    await target.tick()

    assert backend.messages == []
    assert backend.invoices == []
    assert backend.send_calls == []


def test_wire_amount_expiry_and_hop_bounds_are_strict() -> None:
    with pytest.raises(ValueError):
        ChannelMaintenanceConfig(amount_max_sat=MAX_MONEY + 1)
    with pytest.raises(ValueError):
        RouteRequest(
            request_id="01" * 32,
            destination_node_id=PEER_C,
            amount_sat=MAX_MONEY + 1,
            expiry=100,
        )
    with pytest.raises(ValueError):
        RouteRequest(
            request_id="01" * 32,
            destination_node_id=PEER_C,
            amount_sat=1_000,
            expiry=MAX_WIRE_EXPIRY + 1,
        )
    with pytest.raises(ValueError):
        RouteResponse.model_validate(
            {
                "version": 1,
                "kind": "route_response",
                "request_id": "01" * 32,
                "hop": {
                    "node_id": PEER_B,
                    "channel_id": 9_001,
                    "base_fee_msat": 0,
                    "fee_ppm": 0,
                    "cltv_delta": 145,
                },
                "expiry": 100,
            }
        )


async def test_blocked_escrow_candidate_prevents_initialization_and_payment(tmp_path: Path) -> None:
    backend = FakeBackend()
    assert enroll_channel(tmp_path, LOCAL, PEER_B, POINT_B, ORIGIN_B)
    backend.channels = (channel(POINT_B, PEER_B, 101),)
    target, _current = service(tmp_path, backend)

    await target.tick()

    assert backend.fee_calls == []
    assert backend.send_calls == []


async def test_route_request_rate_identity_and_response_authentication(tmp_path: Path) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    request = {
        "version": 1,
        "kind": "route_request",
        "request_id": "12" * 32,
        "destination_node_id": PEER_C,
        "amount_sat": 1_000,
        "expiry": 105,
    }

    for _ in range(MAX_PEER_REQUESTS_PER_MINUTE + 1):
        await target.handle_peer_message(PEER_B, json.dumps(request).encode("ascii"))

    responses = [message for message in backend.messages if message[2]["kind"] == "route_response"]
    assert len(responses) == MAX_PEER_REQUESTS_PER_MINUTE
    assert all(message[2]["request_id"] == "12" * 32 for message in responses)
    assert all(message[2]["expiry"] == 105 for message in responses)

    future = asyncio.get_running_loop().create_future()
    target._pending_requests["34" * 32] = _PendingRouteRequest(
        peer_node_id=PEER_B,
        destination_node_id=PEER_C,
        expiry=105,
        future=future,
    )
    response = {
        "version": 1,
        "kind": "route_response",
        "request_id": "34" * 32,
        "hop": None,
        "expiry": 105,
    }
    await target.handle_peer_message(PEER_D, json.dumps(response).encode("ascii"))
    assert not future.done()
    await target.handle_peer_message(PEER_B, json.dumps(response).encode("ascii"))
    assert future.done()
    await target.handle_peer_message(PEER_B, json.dumps(response).encode("ascii"))
    assert future.result().request_id == "34" * 32


async def test_duplicate_outbound_peer_query_is_rejected_and_orphan_is_cancelled(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)

    first = asyncio.create_task(target._request_route(PEER_B, PEER_C, 1_000))
    await asyncio.sleep(0)
    pending = next(iter(target._pending_requests.values()))

    assert await target._request_route(PEER_B, PEER_C, 1_000) is None
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert pending.future.cancelled()
    assert target._pending_requests == {}


async def test_route_request_requires_current_eligible_shared_channel(tmp_path: Path) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    backend.channels = (channel(POINT_C, PEER_C, 102),)
    backend.candidates = (candidate(POINT_C, PEER_C, 102),)
    request = {
        "version": 1,
        "kind": "route_request",
        "request_id": "12" * 32,
        "destination_node_id": PEER_C,
        "amount_sat": 1_000,
        "expiry": 105,
    }

    await target.handle_peer_message(PEER_B, json.dumps(request).encode("ascii"))

    assert backend.messages == []


async def test_restart_tracks_one_unresolved_attempt_and_cancel_stays_unresolved(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    target._set_next_due(100)
    backend.response = {
        "version": 1,
        "kind": "route_response",
        "request_id": REQUEST_ONE,
        "hop": {
            "node_id": PEER_B,
            "channel_id": 9_001,
            "base_fee_msat": 0,
            "fee_ppm": 0,
            "cltv_delta": 42,
        },
        "expiry": 105,
    }
    backend.cancel_payment = True

    with pytest.raises(asyncio.CancelledError):
        await target.tick()
    assert read_state(tmp_path)["payment"]["phase"] == "unresolved"
    assert len(backend.send_calls) == 1

    restarted, _current = service(tmp_path, backend)
    backend.track_payment = MaintenancePayment(status="succeeded", fee_msat=0)
    await restarted.tick()

    assert backend.track_calls == ["ee" * 32]
    assert len(backend.send_calls) == 1
    assert read_state(tmp_path)["payment"] is None


async def test_created_invoice_is_cleared_without_tracking_or_sending(tmp_path: Path) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    write_journal(tmp_path, "created_invoice", expires_at=1)

    await target.tick()

    state = read_state(tmp_path)
    assert backend.track_calls == []
    assert backend.send_calls == []
    assert state["payment"] is None
    assert state["next_due"] == 110


@pytest.mark.parametrize("phase", ["dispatch_intent", "unresolved"])
async def test_past_expiry_dispatched_or_unresolved_payment_stays_blocked(
    tmp_path: Path, phase: str
) -> None:
    backend = FakeBackend()
    target = await enroll_and_initialize(tmp_path, backend)
    write_journal(tmp_path, phase, expires_at=1)
    backend.track_payment = MaintenancePayment(status="unknown", fee_msat=None)

    await target.tick()

    state = read_state(tmp_path)
    assert backend.track_calls == ["ee" * 32]
    assert backend.send_calls == []
    assert state["payment"]["phase"] == "unresolved"


async def test_disabled_service_skips_new_work_but_reconciles_journaled_payment(
    tmp_path: Path,
) -> None:
    backend = FakeBackend()
    await enroll_and_initialize(tmp_path, backend)
    disabled, _current = service(tmp_path, backend, policy=config(enabled=False))
    backend.service = disabled
    disabled._set_next_due(100)
    backend.fee_calls.clear()
    backend.messages.clear()
    request = {
        "version": 1,
        "kind": "route_request",
        "request_id": "12" * 32,
        "destination_node_id": PEER_C,
        "amount_sat": 1_000,
        "expiry": 105,
    }

    await disabled.tick()
    await disabled.handle_peer_message(PEER_B, json.dumps(request).encode("ascii"))

    assert backend.fee_calls == []
    assert backend.messages == []
    assert backend.invoices == []
    assert backend.send_calls == []

    write_journal(tmp_path, "dispatch_intent")
    backend.track_payment = MaintenancePayment(status="succeeded", fee_msat=0)
    await disabled.tick()

    assert backend.track_calls == ["ee" * 32]
    assert read_state(tmp_path)["payment"] is None


async def test_peer_subscription_eof_reconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeBackend()
    backend.subscription_eofs = 1
    target, _current = service(tmp_path, backend)
    monkeypatch.setattr(maintenance, "_PEER_RECONNECT_BACKOFF_SECONDS", 0.01)

    runner = asyncio.create_task(target.run())
    await asyncio.sleep(0.03)
    target.stop()
    await runner

    assert backend.subscription_calls >= 2


async def test_missing_schedule_is_future_and_service_lock_is_exclusive(tmp_path: Path) -> None:
    backend = FakeBackend()
    target, current = service(tmp_path, backend)
    await target.tick()
    assert read_state(tmp_path)["next_due"] == current[0] + 10
    assert backend.send_calls == []

    runner = asyncio.create_task(target.run())
    await asyncio.sleep(0)
    second, _second_current = service(tmp_path, backend)
    with pytest.raises(MaintenanceLockError):
        await second.run()
    target.stop()
    await runner


async def test_durable_state_and_directory_are_owner_only(tmp_path: Path) -> None:
    backend = FakeBackend()
    target, _current = service(tmp_path, backend)

    await target.tick()

    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "maintenance-v1.json").stat().st_mode) == 0o600
