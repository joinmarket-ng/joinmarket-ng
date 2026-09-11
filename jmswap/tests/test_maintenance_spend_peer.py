from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from jmcore.channel_maintenance import ChannelMaintenanceConfig

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
    MAX_PENDING_PEER_QUERIES,
    MAX_SPEND_PAUSE_SECONDS,
    MaintenanceService,
    PaymentAttempt,
    SpendPauseCapabilityReply,
    SpendPauseCapabilityRequest,
    SpendPauseRelease,
    SpendPauseReply,
    SpendPauseRequest,
    claim_spend_channels,
    enroll_channel,
)

LOCAL = "02" + "11" * 32
PEER_B = "03" + "22" * 32
PEER_C = "02" + "33" * 32
POINT_B = "aa" * 32 + ":1"
POINT_C = "bb" * 32 + ":2"
ORIGIN_B = "cc" * 32
ORIGIN_C = "dd" * 32
REGTEST = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"


class Clock:
    def __init__(self) -> None:
        self.wall = 100
        self.monotonic = 0.0


class FixedRandom:
    def __init__(self) -> None:
        self.value = 0

    def randint(self, minimum: int, _maximum: int) -> int:
        return minimum

    def token_hex(self, nbytes: int) -> str:
        self.value += 1
        return f"{self.value:0{nbytes * 2}x}"


class FakeBackend:
    network = "regtest"

    def __init__(self) -> None:
        self.channels: tuple[MaintenanceChannel, ...] = ()
        self.candidates: tuple[ChannelInputCandidate, ...] = ()
        self.messages: list[tuple[str, int, dict[str, Any]]] = []
        self.service: MaintenanceService | None = None
        self.reply_capability = False
        self.reply_pause = False

    async def list_maintenance_channels(self) -> tuple[MaintenanceChannel, ...]:
        return self.channels

    async def list_channel_input_candidates(self) -> tuple[ChannelInputCandidate, ...]:
        return self.candidates

    async def send_peer_message(self, peer_node_id: str, message_type: int, payload: bytes) -> None:
        document = json.loads(payload)
        self.messages.append((peer_node_id, message_type, document))
        if self.service is None:
            return
        if document["kind"] == "capability_request" and self.reply_capability:
            response = {
                "version": 1,
                "kind": "capability_reply",
                "feature": "spend_pause_v1",
                "request_id": document["request_id"],
                "chain_hash": document["chain_hash"],
                "expiry": document["expiry"],
            }
            await self.service.handle_peer_message(
                peer_node_id, json.dumps(response).encode("ascii")
            )
        elif document["kind"] == "spend_pause" and self.reply_pause:
            response = {
                "version": 1,
                "kind": "spend_reply",
                "status": "paused",
                "chain_hash": document["chain_hash"],
                "channel_point": document["channel_point"],
                "request_id": document["request_id"],
                "expiry": document["expiry"],
            }
            await self.service.handle_peer_message(
                peer_node_id, json.dumps(response).encode("ascii")
            )

    async def create_maintenance_invoice(
        self, amount_sat: int, hops: Sequence[PeerRouteHop], expiry_seconds: int
    ) -> MaintenanceInvoice:
        raise AssertionError("spend-pause tests do not create invoices")

    async def send_maintenance_payment(
        self, payment_request: str, outgoing_channel_id: int, timeout_seconds: int
    ) -> MaintenancePayment:
        raise AssertionError("spend-pause tests do not dispatch payments")

    async def track_maintenance_payment(
        self, payment_hash: str, timeout_seconds: int = 5
    ) -> MaintenancePayment:
        raise AssertionError("spend-pause tests do not track payments")

    async def reconcile_maintenance_invoice(
        self, payment_hash: str, preimage: str, return_channel_id: int, amount_sat: int
    ) -> None:
        raise AssertionError("spend-pause tests do not reconcile invoices")

    async def set_private_channel_zero_fee(self, channel_point: str) -> None:
        return None

    async def subscribe_peer_messages(self, message_type: int) -> AsyncIterator[tuple[str, bytes]]:
        if False:
            yield "", b""


def policy() -> ChannelMaintenanceConfig:
    return ChannelMaintenanceConfig(interval_min_seconds=10, interval_max_seconds=20)


def channel(point: str, peer: str, channel_id: int) -> MaintenanceChannel:
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
        local_alias=1_001,
        peer_alias=2_001,
        local_base_fee_msat=0,
        local_fee_ppm=0,
        remote_base_fee_msat=0,
        remote_fee_ppm=0,
        local_cltv_delta=40,
        remote_cltv_delta=50,
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


async def ready_service(tmp_path: Path) -> tuple[MaintenanceService, FakeBackend, Clock]:
    clock = Clock()
    backend = FakeBackend()
    assert enroll_channel(tmp_path, LOCAL, PEER_B, POINT_B, ORIGIN_B)
    assert enroll_channel(tmp_path, LOCAL, PEER_C, POINT_C, ORIGIN_C)
    backend.channels = (channel(POINT_B, PEER_B, 101), channel(POINT_C, PEER_C, 102))
    backend.candidates = (candidate(POINT_B, PEER_B, 101), candidate(POINT_C, PEER_C, 102))
    service = MaintenanceService(
        backend,
        policy(),
        tmp_path,
        LOCAL,
        clock=lambda: float(clock.wall),
        monotonic_clock=lambda: clock.monotonic,
        random_source=FixedRandom(),
    )
    backend.service = service
    await service._initialize_enrollments()
    clock.monotonic += maintenance.SPEND_PAUSE_QUARANTINE_SECONDS
    return service, backend, clock


async def send_pause(
    service: MaintenanceService,
    peer_node_id: str,
    channel_point: str,
    request_id: int,
    expiry: int,
    chain_hash: str = REGTEST,
) -> None:
    await service.handle_peer_message(
        peer_node_id,
        json.dumps(
            SpendPauseRequest(
                chain_hash=chain_hash,
                channel_point=channel_point,
                request_id=f"{request_id:064x}",
                expiry=expiry,
            ).model_dump()
        ).encode("ascii"),
    )


def last_reply(backend: FakeBackend) -> dict[str, Any]:
    return backend.messages[-1][2]


def stored_pause(
    service: MaintenanceService, request_id: int
) -> maintenance.RemoteSpendPauseRecord:
    state = service._read_state()
    assert state is not None
    return state.remote_spend_pauses[f"{request_id:064x}"]


async def test_sender_requires_exact_capability_reply_before_pause(tmp_path: Path) -> None:
    service, backend, _clock = await ready_service(tmp_path)

    assert await service.request_spend_pause(PEER_B, POINT_B) is None
    assert [message[2]["kind"] for message in backend.messages] == ["capability_request"]

    backend.reply_capability = True
    backend.reply_pause = True
    request = await service.request_spend_pause(PEER_B, POINT_B, duration_seconds=7)

    assert request is not None
    assert request.chain_hash == REGTEST
    assert request.channel_point == POINT_B
    assert request.expiry == 107
    assert [message[2]["kind"] for message in backend.messages[-2:]] == [
        "capability_request",
        "spend_pause",
    ]
    assert await service.release_spend_pause(PEER_B, request)
    assert (
        backend.messages[-1][2]
        == SpendPauseRelease(
            chain_hash=REGTEST,
            channel_point=POINT_B,
            request_id=request.request_id,
            expiry=request.expiry,
        ).model_dump()
    )


@pytest.mark.parametrize("peer", [PEER_B, "03" + "55" * 32])
@pytest.mark.parametrize(
    "payload",
    [b"[" * 4096 + b"]" * 4096, b'{"x":' * 1000 + b"0" + b"}" * 1000],
    ids=["deep-array", "deep-object"],
)
async def test_deep_json_does_not_escape_peer_handler(
    tmp_path: Path, peer: str, payload: bytes
) -> None:
    service, backend, _ = await ready_service(tmp_path)
    before = (tmp_path / "maintenance-v1.json").read_bytes()
    await service.handle_peer_message(peer, payload)
    assert not backend.messages
    assert (tmp_path / "maintenance-v1.json").read_bytes() == before


async def test_tick_announces_each_local_guard_once_and_releases_after_cancellation(
    tmp_path: Path,
) -> None:
    service, backend, _ = await ready_service(tmp_path)
    backend.reply_capability = True
    backend.reply_pause = True
    assert claim_spend_channels(tmp_path, LOCAL, "local-owner", [POINT_B])
    await service.tick()
    assert [item[2]["kind"] for item in backend.messages] == ["capability_request", "spend_pause"]
    await service.tick()
    assert len(backend.messages) == 2
    maintenance.release_spend_channels(tmp_path, LOCAL, "local-owner", [POINT_B])
    await service.tick()
    assert [item[2]["kind"] for item in backend.messages] == [
        "capability_request",
        "spend_pause",
        "spend_release",
    ]
    assert backend.messages[-1][2]["request_id"] == backend.messages[1][2]["request_id"]
    assert "local-owner" not in json.dumps(backend.messages)


async def test_receiver_rejects_wrong_bindings_and_expiry(tmp_path: Path) -> None:
    service, backend, clock = await ready_service(tmp_path)

    await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 10, "11" * 32)
    assert backend.messages == []
    await send_pause(service, PEER_C, POINT_B, 2, clock.wall + 10)
    assert last_reply(backend)["status"] == "busy"
    await send_pause(service, PEER_B, "ff" * 32 + ":3", 3, clock.wall + 10)
    assert last_reply(backend)["status"] == "busy"
    await send_pause(service, PEER_B, POINT_B, 4, clock.wall)
    assert last_reply(backend)["request_id"] == f"{3:064x}"
    await send_pause(service, PEER_B, POINT_B, 5, clock.wall + MAX_SPEND_PAUSE_SECONDS + 1)
    assert last_reply(backend)["request_id"] == f"{3:064x}"


async def test_duplicate_release_and_budget_replays_are_conservative(tmp_path: Path) -> None:
    service, backend, clock = await ready_service(tmp_path)

    await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 20)
    assert last_reply(backend)["status"] == "paused"
    await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 20)
    assert last_reply(backend)["status"] == "paused"
    await service.handle_peer_message(
        PEER_B,
        json.dumps(
            SpendPauseRelease(
                chain_hash=REGTEST,
                channel_point=POINT_B,
                request_id=f"{1:064x}",
                expiry=clock.wall + 20,
            ).model_dump()
        ).encode("ascii"),
    )
    await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 20)
    assert last_reply(backend)["status"] == "busy"

    await send_pause(service, PEER_B, POINT_B, 2, clock.wall + 10)
    assert last_reply(backend)["status"] == "paused"
    await send_pause(service, PEER_B, POINT_B, 3, clock.wall + 1)
    assert last_reply(backend)["status"] == "busy"
    with pytest.raises(ValueError, match="between 1 and 30"):
        await service.request_spend_pause(PEER_B, POINT_B, MAX_SPEND_PAUSE_SECONDS + 1)


async def test_inexact_replay_cannot_expire_an_accepted_pause(tmp_path: Path) -> None:
    service, backend, clock = await ready_service(tmp_path)

    await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 20)
    await send_pause(service, PEER_C, POINT_C, 1, clock.wall + 20)
    assert last_reply(backend)["status"] == "busy"
    await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 19)
    assert last_reply(backend)["status"] == "busy"

    assert stored_pause(service, 1).status == "paused"
    assert stored_pause(service, 1).channel_point == POINT_B
    await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 20)
    assert last_reply(backend)["status"] == "paused"
    assert all(
        channel.channel_point != POINT_B for channel, _record in await service._eligible_channels()
    )


async def test_forward_wall_jump_preserves_live_monotonic_pause_budget(tmp_path: Path) -> None:
    service, backend, clock = await ready_service(tmp_path)

    await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 20)
    assert last_reply(backend)["status"] == "paused"
    clock.wall += 100
    await send_pause(service, PEER_B, POINT_B, 2, clock.wall + 11)

    assert last_reply(backend)["status"] == "busy"
    assert stored_pause(service, 1).status == "paused"


async def test_fractional_monotonic_start_keeps_a_full_quarantine(tmp_path: Path) -> None:
    service, backend, clock = await ready_service(tmp_path)
    clock.monotonic = 60.5
    restarted = MaintenanceService(
        backend,
        policy(),
        tmp_path,
        LOCAL,
        clock=lambda: float(clock.wall),
        monotonic_clock=lambda: clock.monotonic,
        random_source=FixedRandom(),
    )
    backend.service = restarted

    await send_pause(restarted, PEER_B, POINT_B, 1, clock.wall + 10)
    assert last_reply(backend)["status"] == "busy"
    clock.monotonic = 120.99
    await send_pause(restarted, PEER_B, POINT_B, 2, clock.wall + 10)
    assert last_reply(backend)["status"] == "busy"
    clock.monotonic = 121
    await send_pause(restarted, PEER_B, POINT_B, 3, clock.wall + 10)
    assert last_reply(backend)["status"] == "paused"


async def test_duplicate_pauses_and_releases_share_the_incoming_rate_limit(tmp_path: Path) -> None:
    service, backend, clock = await ready_service(tmp_path)

    for _ in range(6):
        await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 20)
    assert last_reply(backend)["status"] == "paused"
    await service.handle_peer_message(
        PEER_B,
        json.dumps(
            SpendPauseRelease(
                chain_hash=REGTEST,
                channel_point=POINT_B,
                request_id=f"{1:064x}",
                expiry=clock.wall + 20,
            ).model_dump()
        ).encode("ascii"),
    )
    assert stored_pause(service, 1).status == "paused"

    clock.wall += 60
    await service.handle_peer_message(
        PEER_B,
        json.dumps(
            SpendPauseRelease(
                chain_hash=REGTEST,
                channel_point=POINT_B,
                request_id=f"{1:064x}",
                expiry=120,
            ).model_dump()
        ).encode("ascii"),
    )
    assert stored_pause(service, 1).status == "released"


async def test_mismatched_capability_and_spend_replies_leave_futures_pending(
    tmp_path: Path,
) -> None:
    service, _backend, clock = await ready_service(tmp_path)
    capability = SpendPauseCapabilityRequest(
        request_id=f"{1:064x}", chain_hash=REGTEST, expiry=clock.wall + 5
    )
    capability_task = asyncio.create_task(service._request_spend_reply(PEER_B, capability))
    await asyncio.sleep(0)
    await service.handle_peer_message(
        PEER_B,
        json.dumps(
            SpendPauseCapabilityReply(
                request_id=capability.request_id,
                chain_hash=REGTEST,
                expiry=capability.expiry - 1,
            ).model_dump()
        ).encode("ascii"),
    )
    assert not capability_task.done()
    expected_capability_reply = SpendPauseCapabilityReply(
        request_id=capability.request_id,
        chain_hash=capability.chain_hash,
        expiry=capability.expiry,
    )
    await service.handle_peer_message(
        PEER_B, json.dumps(expected_capability_reply.model_dump()).encode("ascii")
    )
    assert await capability_task == expected_capability_reply

    request = SpendPauseRequest(
        request_id=f"{2:064x}", chain_hash=REGTEST, channel_point=POINT_B, expiry=clock.wall + 10
    )
    pause_task = asyncio.create_task(service._request_spend_reply(PEER_B, request))
    await asyncio.sleep(0)
    await service.handle_peer_message(
        PEER_B,
        json.dumps(
            SpendPauseReply(
                status="paused",
                request_id=request.request_id,
                chain_hash=REGTEST,
                channel_point=POINT_C,
                expiry=request.expiry,
            ).model_dump()
        ).encode("ascii"),
    )
    assert not pause_task.done()
    expected_reply = SpendPauseReply(
        status="paused",
        request_id=request.request_id,
        chain_hash=request.chain_hash,
        channel_point=request.channel_point,
        expiry=request.expiry,
    )
    await service.handle_peer_message(
        PEER_B, json.dumps(expected_reply.model_dump()).encode("ascii")
    )
    assert await pause_task == expected_reply


async def test_pending_spend_queries_share_global_and_peer_limits(tmp_path: Path) -> None:
    service, _backend, clock = await ready_service(tmp_path)
    pending = [
        asyncio.create_task(
            service._request_spend_reply(
                f"02{index:064x}",
                SpendPauseCapabilityRequest(
                    request_id=f"{index:064x}", chain_hash=REGTEST, expiry=clock.wall + 5
                ),
            )
        )
        for index in range(1, MAX_PENDING_PEER_QUERIES + 1)
    ]
    await asyncio.sleep(0)
    assert len(service._pending_spend_requests) == MAX_PENDING_PEER_QUERIES
    assert (
        await service._request_spend_reply(
            PEER_B,
            SpendPauseCapabilityRequest(
                request_id=f"{99:064x}", chain_hash=REGTEST, expiry=clock.wall + 5
            ),
        )
        is None
    )
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

    first = asyncio.create_task(
        service._request_spend_reply(
            PEER_B,
            SpendPauseCapabilityRequest(
                request_id=f"{100:064x}", chain_hash=REGTEST, expiry=clock.wall + 5
            ),
        )
    )
    await asyncio.sleep(0)
    assert (
        await service._request_spend_reply(
            PEER_B,
            SpendPauseCapabilityRequest(
                request_id=f"{101:064x}", chain_hash=REGTEST, expiry=clock.wall + 5
            ),
        )
        is None
    )
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_restart_quarantine_expires_remote_leases_without_wall_rollback_bypass(
    tmp_path: Path,
) -> None:
    service, backend, clock = await ready_service(tmp_path)
    await send_pause(service, PEER_B, POINT_B, 1, clock.wall + 30)
    assert last_reply(backend)["status"] == "paused"
    assert all(
        channel.channel_point != POINT_B for channel, _record in await service._eligible_channels()
    )

    clock.monotonic += 1
    restarted = MaintenanceService(
        backend,
        policy(),
        tmp_path,
        LOCAL,
        clock=lambda: float(clock.wall),
        monotonic_clock=lambda: clock.monotonic,
        random_source=FixedRandom(),
    )
    backend.service = restarted
    assert any(
        channel.channel_point == POINT_B
        for channel, _record in await restarted._eligible_channels()
    )

    clock.wall = 1
    await send_pause(restarted, PEER_B, POINT_B, 2, 130)
    assert last_reply(backend)["status"] == "busy"
    clock.monotonic += maintenance.SPEND_PAUSE_QUARANTINE_SECONDS
    await send_pause(restarted, PEER_B, POINT_B, 3, 130)
    assert last_reply(backend)["status"] == "paused"


@pytest.mark.parametrize("point", [POINT_B, POINT_C])
async def test_remote_pause_preempts_created_invoice_but_not_local_spend_guard(
    tmp_path: Path, point: str
) -> None:
    service, backend, clock = await ready_service(tmp_path)
    attempt = PaymentAttempt(
        phase="created_invoice",
        payment_hash="ee" * 32,
        payment_request="lnbcrt-maintenance",
        amount_sat=1_000,
        outgoing_channel_id=101,
        outgoing_channel_point=POINT_B,
        return_channel_point=POINT_C,
        middle_peer_node_id=PEER_B,
        return_peer_node_id=PEER_C,
        expires_at=130,
    )
    assert service._record_payment(attempt)

    peer = PEER_B if point == POINT_B else PEER_C
    await send_pause(service, peer, point, 1, clock.wall + 10)
    assert last_reply(backend)["status"] == "paused"
    assert not service._replace_payment(
        attempt, attempt.model_copy(update={"phase": "dispatch_intent"})
    )
    assert all(
        channel.channel_point != point for channel, _record in await service._eligible_channels()
    )
    assert claim_spend_channels(tmp_path, LOCAL, "local-spend", [point])


@pytest.mark.parametrize(("point", "peer_node_id"), [(POINT_B, PEER_B), (POINT_C, PEER_C)])
async def test_remote_pause_returns_busy_for_dispatched_payment(
    tmp_path: Path, point: str, peer_node_id: str
) -> None:
    service, backend, clock = await ready_service(tmp_path)
    attempt = PaymentAttempt(
        phase="dispatch_intent",
        payment_hash="ee" * 32,
        payment_request="lnbcrt-maintenance",
        amount_sat=1_000,
        outgoing_channel_id=101,
        outgoing_channel_point=POINT_B,
        return_channel_point=POINT_C,
        middle_peer_node_id=PEER_B,
        return_peer_node_id=PEER_C,
        expires_at=130,
    )
    assert service._record_payment(attempt)

    await send_pause(service, peer_node_id, point, 1, clock.wall + 10)

    assert last_reply(backend)["status"] == "busy"
