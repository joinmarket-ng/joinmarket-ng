from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncGenerator
from typing import Any

import grpc
import pytest

from jmswap.lnd import (
    LndBackend,
    LndRpcError,
    LndValidationError,
    MaintenanceChannel,
    MaintenancePayment,
    PeerRouteHop,
)
from jmswap.lndrpc import invoices_pb2 as invoices
from jmswap.lndrpc import lightning_pb2 as ln

LOCAL_ID = "02" + "11" * 32
PEER_ID = "03" + "22" * 32
OTHER_PEER_ID = "02" + "33" * 32
CHANNEL_POINT = "aa" * 32 + ":4"
MISSING_POLICY_POINT = "bb" * 32 + ":5"
CUSTOM_TYPE = 32769
PAYMENT_PREIMAGE = b"p" * 32
PAYMENT_HASH = hashlib.sha256(PAYMENT_PREIMAGE).hexdigest()
PAYMENT_ADDR = b"s" * 32
MAINTENANCE_BLOCK_HEIGHT = 800_000
MAINTENANCE_AMOUNT_SAT = 25_000
RETURN_CHANNEL_ID = 1_000


class FakeGrpcError(Exception):
    def __init__(self, status_code: grpc.StatusCode, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code

    def code(self) -> grpc.StatusCode:
        return self.status_code


class FakeStream:
    def __init__(
        self, updates: list[Any] | None = None, *, error: Exception | None = None, delay: float = 0
    ) -> None:
        self.updates = updates or []
        self.error = error
        self.delay = delay
        self.read_count = 0
        self.cancel_count = 0

    async def read(self) -> Any:
        self.read_count += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.updates:
            return self.updates.pop(0)
        return None

    def __aiter__(self) -> FakeStream:
        return self

    async def __anext__(self) -> Any:
        if self.error is not None:
            raise self.error
        if not self.updates:
            raise StopAsyncIteration
        return self.updates.pop(0)

    def cancel(self) -> None:
        self.cancel_count += 1


class FakeMaintenanceStub:
    def __init__(self) -> None:
        self.list_response = ln.ListChannelsResponse()
        self.graph_response = ln.ChannelGraph()
        self.info_response = ln.GetInfoResponse(
            identity_pubkey=LOCAL_ID, block_height=MAINTENANCE_BLOCK_HEIGHT
        )
        self.decode_response = maintenance_pay_req(timestamp=int(time.time()))
        self.list_error: Exception | None = None
        self.graph_error: Exception | None = None
        self.custom_stream = FakeStream()
        self.add_invoice_response = ln.AddInvoiceResponse(
            r_hash=b"h" * 32, payment_request="lnbcrt-maintenance"
        )
        self.lookup_response = ln.Invoice()
        self.lookup_error: Exception | None = None
        self.list_requests: list[Any] = []
        self.graph_requests: list[Any] = []
        self.info_requests: list[Any] = []
        self.decode_requests: list[Any] = []
        self.custom_requests: list[Any] = []
        self.add_invoice_requests: list[Any] = []
        self.lookup_requests: list[Any] = []
        self.update_requests: list[Any] = []

    async def ListChannels(self, request: Any) -> Any:  # noqa: N802
        self.list_requests.append(request)
        if self.list_error is not None:
            raise self.list_error
        return self.list_response

    async def DescribeGraph(self, request: Any) -> Any:  # noqa: N802
        self.graph_requests.append(request)
        if self.graph_error is not None:
            raise self.graph_error
        return self.graph_response

    async def GetInfo(self, request: Any) -> Any:  # noqa: N802
        self.info_requests.append(request)
        return self.info_response

    async def DecodePayReq(self, request: Any) -> Any:  # noqa: N802
        self.decode_requests.append(request)
        return self.decode_response

    async def SendCustomMessage(self, request: Any) -> Any:  # noqa: N802
        self.custom_requests.append(request)
        return ln.SendCustomMessageResponse()

    def SubscribeCustomMessages(self, _request: Any) -> FakeStream:  # noqa: N802
        return self.custom_stream

    async def AddInvoice(self, request: Any) -> Any:  # noqa: N802
        self.add_invoice_requests.append(request)
        return self.add_invoice_response

    async def LookupInvoice(self, request: Any) -> Any:  # noqa: N802
        self.lookup_requests.append(request)
        if self.lookup_error is not None:
            raise self.lookup_error
        return self.lookup_response

    async def UpdateChannelPolicy(self, request: Any) -> Any:  # noqa: N802
        self.update_requests.append(request)
        return ln.PolicyUpdateResponse()


class FakeInvoicesStub:
    def __init__(self) -> None:
        self.add_hold_response = invoices.AddHoldInvoiceResp(payment_request="lnbcrt-maintenance")
        self.lookup_response = ln.Invoice()
        self.lookup_error: Exception | None = None
        self.add_hold_requests: list[Any] = []
        self.lookup_requests: list[Any] = []
        self.settle_requests: list[Any] = []
        self.cancel_requests: list[Any] = []

    async def AddHoldInvoice(self, request: Any) -> Any:  # noqa: N802
        self.add_hold_requests.append(request)
        return self.add_hold_response

    async def SettleInvoice(self, request: Any) -> Any:  # noqa: N802
        self.settle_requests.append(request)
        return invoices.SettleInvoiceResp()

    async def CancelInvoice(self, request: Any) -> Any:  # noqa: N802
        self.cancel_requests.append(request)
        return invoices.CancelInvoiceResp()


class FakeRouterStub:
    def __init__(self) -> None:
        self.send_to_route_response: Any = successful_attempt()
        self.send_to_route_delay = 0.0
        self.send_to_route_gate: asyncio.Event | None = None
        self.track_stream = FakeStream([ln.Payment(status=ln.Payment.IN_FLIGHT)])
        self.send_to_route_requests: list[Any] = []
        self.send_payment_requests: list[Any] = []
        self.track_requests: list[Any] = []

    def SendPaymentV2(self, request: Any) -> FakeStream:  # noqa: N802
        self.send_payment_requests.append(request)
        raise AssertionError("maintenance payments must not use SendPaymentV2")

    async def SendToRouteV2(self, request: Any) -> Any:  # noqa: N802
        self.send_to_route_requests.append(request)
        if self.send_to_route_gate is not None:
            await self.send_to_route_gate.wait()
        if self.send_to_route_delay:
            await asyncio.sleep(self.send_to_route_delay)
        return self.send_to_route_response

    def TrackPaymentV2(self, request: Any) -> FakeStream:  # noqa: N802
        self.track_requests.append(request)
        return self.track_stream


def backend(
    stub: FakeMaintenanceStub | None = None,
    router: FakeRouterStub | None = None,
    invoices_stub: FakeInvoicesStub | None = None,
) -> LndBackend:
    result = LndBackend(host="localhost:10009", tls_cert=b"", macaroon_hex="")
    maintenance_stub = stub or FakeMaintenanceStub()
    invoice_stub = invoices_stub or FakeInvoicesStub()
    if invoices_stub is not None:
        maintenance_stub.lookup_response = invoices_stub.lookup_response
        maintenance_stub.lookup_error = invoices_stub.lookup_error
        maintenance_stub.lookup_requests = invoices_stub.lookup_requests
    result._stub = maintenance_stub
    result._router_stub = router
    result._invoices_stub = invoice_stub
    return result


def listed_channel(
    channel_point: str = CHANNEL_POINT, *, private: bool = False, **changes: Any
) -> Any:
    values: dict[str, Any] = {
        "channel_point": channel_point,
        "chan_id": 123,
        "remote_pubkey": PEER_ID,
        "active": False,
        "private": private,
        "local_balance": 700_000,
        "remote_balance": 300_000,
        "pending_htlcs": [ln.HTLC()],
        "local_constraints": ln.ChannelConstraints(chan_reserve_sat=11_000),
        "remote_constraints": ln.ChannelConstraints(chan_reserve_sat=12_000),
        "alias_scids": [1_001],
        "peer_scid_alias": 2_002,
    }
    values.update(changes)
    return ln.Channel(**values)


def maintenance_pay_req(**changes: Any) -> Any:
    values: dict[str, Any] = {
        "destination": LOCAL_ID,
        "payment_hash": PAYMENT_HASH,
        "num_satoshis": 25_000,
        "num_msat": 25_000_000,
        "timestamp": 1_700_000_000,
        "expiry": 120,
        "cltv_expiry": 18,
        "payment_addr": PAYMENT_ADDR,
        "route_hints": [
            ln.RouteHint(
                hop_hints=[
                    ln.HopHint(
                        node_id=PEER_ID,
                        chan_id=1_001,
                        fee_base_msat=0,
                        fee_proportional_millionths=0,
                        cltv_expiry_delta=40,
                    ),
                    ln.HopHint(
                        node_id=OTHER_PEER_ID,
                        chan_id=2_002,
                        fee_base_msat=0,
                        fee_proportional_millionths=0,
                        cltv_expiry_delta=50,
                    ),
                ]
            )
        ],
    }
    values.update(changes)
    return ln.PayReq(**values)


def successful_attempt(*, total_fees_msat: int = 0, preimage: bytes = PAYMENT_PREIMAGE) -> Any:
    return ln.HTLCAttempt(
        status=ln.HTLCAttempt.SUCCEEDED,
        route=ln.Route(total_fees_msat=total_fees_msat),
        preimage=preimage,
    )


def maintenance_invoice(
    state: int,
    *,
    htlc_state: int | None = None,
    htlcs: list[Any] | None = None,
    **changes: Any,
) -> Any:
    if htlcs is None:
        htlcs = [
            ln.InvoiceHTLC(
                chan_id=RETURN_CHANNEL_ID,
                amt_msat=MAINTENANCE_AMOUNT_SAT * 1000,
                state=ln.ACCEPTED if htlc_state is None else htlc_state,
            )
        ]
    values: dict[str, Any] = {
        "r_hash": bytes.fromhex(PAYMENT_HASH),
        "value": MAINTENANCE_AMOUNT_SAT,
        "value_msat": MAINTENANCE_AMOUNT_SAT * 1000,
        "amt_paid_msat": MAINTENANCE_AMOUNT_SAT * 1000,
        "state": state,
        "htlcs": htlcs,
    }
    values.update(changes)
    return ln.Invoice(**values)


def configure_maintenance_payment(stub: FakeMaintenanceStub) -> None:
    stub.decode_response = maintenance_pay_req(timestamp=int(time.time()))
    stub.info_response = ln.GetInfoResponse(
        identity_pubkey=LOCAL_ID, block_height=MAINTENANCE_BLOCK_HEIGHT
    )
    stub.list_response = ln.ListChannelsResponse(
        channels=[
            listed_channel(
                chan_id=999, remote_pubkey=PEER_ID, peer_scid_alias=0, active=True, private=True
            ),
            listed_channel(
                channel_point="cc" * 32 + ":6",
                chan_id=1_000,
                remote_pubkey=OTHER_PEER_ID,
                active=True,
                private=True,
                peer_scid_alias=2_002,
            ),
        ]
    )


def graph_edge(
    channel_point: str = CHANNEL_POINT, *, local_cltv_delta: int = 40, **changes: Any
) -> Any:
    values: dict[str, Any] = {
        "chan_point": channel_point,
        "node1_pub": LOCAL_ID,
        "node2_pub": PEER_ID,
        "node1_policy": ln.RoutingPolicy(
            fee_base_msat=3, fee_rate_milli_msat=4, time_lock_delta=local_cltv_delta
        ),
        "node2_policy": ln.RoutingPolicy(
            fee_base_msat=5, fee_rate_milli_msat=6, time_lock_delta=50
        ),
    }
    values.update(changes)
    return ln.ChannelEdge(**values)


async def test_list_maintenance_channels_maps_aliases_and_omits_missing_policy() -> None:
    stub = FakeMaintenanceStub()
    stub.list_response = ln.ListChannelsResponse(
        channels=[listed_channel(), listed_channel(MISSING_POLICY_POINT, chan_id=456)]
    )
    stub.graph_response = ln.ChannelGraph(
        edges=[graph_edge(), ln.ChannelEdge(chan_point=MISSING_POLICY_POINT, node1_pub=LOCAL_ID)]
    )

    channels = await backend(stub).list_maintenance_channels()

    assert channels == (
        MaintenanceChannel(
            channel_point=CHANNEL_POINT,
            channel_id=123,
            peer_node_id=PEER_ID,
            active=False,
            private=False,
            local_balance_sat=700_000,
            remote_balance_sat=300_000,
            local_reserve_sat=11_000,
            remote_reserve_sat=12_000,
            pending_htlc_count=1,
            local_alias=1_001,
            peer_alias=2_002,
            local_base_fee_msat=3,
            local_fee_ppm=4,
            remote_base_fee_msat=5,
            remote_fee_ppm=6,
            local_cltv_delta=40,
            remote_cltv_delta=50,
        ),
    )
    assert stub.list_requests[0] == ln.ListChannelsRequest()
    assert stub.graph_requests[0].include_unannounced is True


async def test_list_maintenance_channels_reports_graph_rpc_failures() -> None:
    stub = FakeMaintenanceStub()
    stub.graph_error = RuntimeError("graph unavailable")

    with pytest.raises(LndRpcError, match="graph unavailable"):
        await backend(stub).list_maintenance_channels()


async def test_peer_messages_bind_unknown_odd_type_and_cancel_subscription() -> None:
    stub = FakeMaintenanceStub()
    target = backend(stub)

    await target.send_peer_message(PEER_ID, CUSTOM_TYPE, b"maintenance")
    request = stub.custom_requests[0]
    assert bytes(request.peer) == bytes.fromhex(PEER_ID)
    assert request.type == CUSTOM_TYPE
    assert bytes(request.data) == b"maintenance"

    with pytest.raises(ValueError, match="odd"):
        await target.send_peer_message(PEER_ID, 32768, b"")
    with pytest.raises(ValueError, match="8192"):
        await target.send_peer_message(PEER_ID, CUSTOM_TYPE, b"x" * 8193)
    assert len(stub.custom_requests) == 1

    stub.custom_stream = FakeStream(
        [
            ln.CustomMessage(peer=bytes.fromhex(OTHER_PEER_ID), type=CUSTOM_TYPE + 2, data=b"skip"),
            ln.CustomMessage(peer=bytes.fromhex(OTHER_PEER_ID), type=CUSTOM_TYPE, data=b"x" * 8193),
            ln.CustomMessage(peer=bytes.fromhex(PEER_ID), type=CUSTOM_TYPE, data=b"accepted"),
        ]
    )
    subscription = target.subscribe_peer_messages(CUSTOM_TYPE)
    assert await anext(subscription) == (PEER_ID, b"accepted")
    assert isinstance(subscription, AsyncGenerator)
    await subscription.aclose()
    assert stub.custom_stream.cancel_count == 1


async def test_create_invoice_and_send_payment_use_one_exact_zero_fee_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = FakeMaintenanceStub()
    router = FakeRouterStub()
    invoices_stub = FakeInvoicesStub()
    configure_maintenance_payment(stub)
    target = backend(stub, router, invoices_stub)
    monkeypatch.setattr("jmswap.lnd.time.time", lambda: 1_700_000_000)
    monkeypatch.setattr("jmswap.lnd.secrets.token_bytes", lambda _size: PAYMENT_PREIMAGE)

    invoice = await target.create_maintenance_invoice(
        25_000,
        [PeerRouteHop(PEER_ID, 1_001, 0, 0, 40), PeerRouteHop(LOCAL_ID, 2_002, 0, 0, 50)],
        120,
    )

    assert invoice.payment_hash == PAYMENT_HASH
    assert invoice.payment_request == "lnbcrt-maintenance"
    assert invoice.expires_at == 1_700_000_120
    assert invoice.preimage == PAYMENT_PREIMAGE.hex()
    assert stub.add_invoice_requests == []
    add_request = invoices_stub.add_hold_requests[0]
    assert bytes(add_request.hash) == bytes.fromhex(PAYMENT_HASH)
    assert add_request.value == 25_000
    assert add_request.expiry == 120
    assert add_request.private is False
    assert add_request.memo == ""
    assert len(add_request.route_hints) == 1
    assert [hop.node_id for hop in add_request.route_hints[0].hop_hints] == [PEER_ID, LOCAL_ID]
    assert [hop.chan_id for hop in add_request.route_hints[0].hop_hints] == [1_001, 2_002]

    payment = await target.send_maintenance_payment(invoice.payment_request, 999, 8)

    assert payment == MaintenancePayment(status="succeeded", fee_msat=0)
    assert stub.decode_requests == [ln.PayReqString(pay_req=invoice.payment_request)]
    assert stub.info_requests == [ln.GetInfoRequest()]
    send_request = router.send_to_route_requests[0]
    assert bytes(send_request.payment_hash) == bytes.fromhex(PAYMENT_HASH)
    assert send_request.skip_temp_err is False
    route = send_request.route
    assert route.total_time_lock == 800_111
    assert route.total_fees_msat == 0
    assert route.total_amt_msat == 25_000_000
    assert route.first_hop_amount_msat == 25_000_000
    assert [hop.pub_key for hop in route.hops] == [PEER_ID, OTHER_PEER_ID, LOCAL_ID]
    assert [hop.chan_id for hop in route.hops] == [999, 1_001, 2_002]
    assert [hop.amt_to_forward_msat for hop in route.hops] == [25_000_000] * 3
    assert [hop.fee_msat for hop in route.hops] == [0, 0, 0]
    assert [hop.expiry for hop in route.hops] == [800_071, 800_021, 800_021]
    assert bytes(route.hops[-1].mpp_record.payment_addr) == PAYMENT_ADDR
    assert route.hops[-1].mpp_record.total_amt_msat == 25_000_000
    assert router.send_payment_requests == []


async def test_reconcile_maintenance_invoice_settles_only_the_expected_incoming_channel() -> None:
    invoices_stub = FakeInvoicesStub()
    invoices_stub.lookup_response = maintenance_invoice(ln.Invoice.ACCEPTED)

    await backend(invoices_stub=invoices_stub).reconcile_maintenance_invoice(
        PAYMENT_HASH,
        PAYMENT_PREIMAGE.hex(),
        RETURN_CHANNEL_ID,
        MAINTENANCE_AMOUNT_SAT,
    )

    assert bytes(invoices_stub.lookup_requests[0].r_hash) == bytes.fromhex(PAYMENT_HASH)
    assert [bytes(request.preimage) for request in invoices_stub.settle_requests] == [
        PAYMENT_PREIMAGE
    ]
    assert invoices_stub.cancel_requests == []


@pytest.mark.parametrize("case", ["wrong channel", "split", "wrong amount"])
async def test_reconcile_maintenance_invoice_cancels_ambiguous_or_wrong_acceptance(
    case: str,
) -> None:
    invoices_stub = FakeInvoicesStub()
    if case == "wrong channel":
        invoices_stub.lookup_response = maintenance_invoice(
            ln.Invoice.ACCEPTED,
            htlcs=[
                ln.InvoiceHTLC(
                    chan_id=RETURN_CHANNEL_ID + 1,
                    amt_msat=MAINTENANCE_AMOUNT_SAT * 1000,
                    state=ln.ACCEPTED,
                )
            ],
        )
    elif case == "split":
        invoices_stub.lookup_response = maintenance_invoice(
            ln.Invoice.ACCEPTED,
            htlcs=[
                ln.InvoiceHTLC(
                    chan_id=RETURN_CHANNEL_ID,
                    amt_msat=MAINTENANCE_AMOUNT_SAT * 500,
                    state=ln.ACCEPTED,
                ),
                ln.InvoiceHTLC(
                    chan_id=RETURN_CHANNEL_ID,
                    amt_msat=MAINTENANCE_AMOUNT_SAT * 500,
                    state=ln.ACCEPTED,
                ),
            ],
        )
    elif case == "wrong amount":
        invoices_stub.lookup_response = maintenance_invoice(
            ln.Invoice.ACCEPTED, amt_paid_msat=(MAINTENANCE_AMOUNT_SAT + 1) * 1000
        )
    else:
        raise AssertionError(f"unhandled test case {case}")

    await backend(invoices_stub=invoices_stub).reconcile_maintenance_invoice(
        PAYMENT_HASH,
        PAYMENT_PREIMAGE.hex(),
        RETURN_CHANNEL_ID,
        MAINTENANCE_AMOUNT_SAT,
    )

    assert invoices_stub.settle_requests == []
    assert [bytes(request.payment_hash) for request in invoices_stub.cancel_requests] == [
        bytes.fromhex(PAYMENT_HASH)
    ]


@pytest.mark.parametrize("state", [ln.Invoice.OPEN, ln.Invoice.CANCELED])
async def test_reconcile_maintenance_invoice_leaves_open_or_canceled_invoices_unchanged(
    state: int,
) -> None:
    invoices_stub = FakeInvoicesStub()
    invoices_stub.lookup_response = ln.Invoice(state=state)

    await backend(invoices_stub=invoices_stub).reconcile_maintenance_invoice(
        PAYMENT_HASH,
        PAYMENT_PREIMAGE.hex(),
        RETURN_CHANNEL_ID,
        MAINTENANCE_AMOUNT_SAT,
    )

    assert invoices_stub.settle_requests == []
    assert invoices_stub.cancel_requests == []


async def test_reconcile_maintenance_invoice_accepts_already_settled_exact_binding() -> None:
    invoices_stub = FakeInvoicesStub()
    invoices_stub.lookup_response = maintenance_invoice(ln.Invoice.SETTLED, htlc_state=ln.SETTLED)

    await backend(invoices_stub=invoices_stub).reconcile_maintenance_invoice(
        PAYMENT_HASH,
        PAYMENT_PREIMAGE.hex(),
        RETURN_CHANNEL_ID,
        MAINTENANCE_AMOUNT_SAT,
    )

    assert invoices_stub.settle_requests == []
    assert invoices_stub.cancel_requests == []

    invoices_stub.lookup_response = maintenance_invoice(
        ln.Invoice.SETTLED,
        htlc_state=ln.SETTLED,
        htlcs=[
            ln.InvoiceHTLC(
                chan_id=RETURN_CHANNEL_ID + 1,
                amt_msat=MAINTENANCE_AMOUNT_SAT * 1000,
                state=ln.SETTLED,
            )
        ],
    )
    with pytest.raises(LndValidationError, match="wrong channel"):
        await backend(invoices_stub=invoices_stub).reconcile_maintenance_invoice(
            PAYMENT_HASH,
            PAYMENT_PREIMAGE.hex(),
            RETURN_CHANNEL_ID,
            MAINTENANCE_AMOUNT_SAT,
        )
    assert invoices_stub.settle_requests == []
    assert invoices_stub.cancel_requests == []


@pytest.mark.parametrize(
    ("payment_hash", "preimage"),
    [
        (PAYMENT_HASH.upper(), PAYMENT_PREIMAGE.hex()),
        (PAYMENT_HASH, "zz" * 32),
        ("ab" * 32, PAYMENT_PREIMAGE.hex()),
    ],
)
async def test_reconcile_maintenance_invoice_rejects_malformed_or_unbound_secrets(
    payment_hash: str, preimage: str
) -> None:
    invoices_stub = FakeInvoicesStub()

    with pytest.raises(ValueError):
        await backend(invoices_stub=invoices_stub).reconcile_maintenance_invoice(
            payment_hash,
            preimage,
            RETURN_CHANNEL_ID,
            MAINTENANCE_AMOUNT_SAT,
        )
    assert invoices_stub.lookup_requests == []
    assert invoices_stub.settle_requests == []
    assert invoices_stub.cancel_requests == []


async def test_reconcile_maintenance_invoice_keeps_unknown_invoice_conservative() -> None:
    invoices_stub = FakeInvoicesStub()
    invoices_stub.lookup_error = FakeGrpcError(grpc.StatusCode.NOT_FOUND, "invoice not found")

    with pytest.raises(LndRpcError, match="LookupInvoice failed"):
        await backend(invoices_stub=invoices_stub).reconcile_maintenance_invoice(
            PAYMENT_HASH,
            PAYMENT_PREIMAGE.hex(),
            RETURN_CHANNEL_ID,
            MAINTENANCE_AMOUNT_SAT,
        )
    assert invoices_stub.settle_requests == []
    assert invoices_stub.cancel_requests == []


@pytest.mark.parametrize(
    "case",
    [
        "different destination",
        "inconsistent whole-sat amount",
        "missing route hint",
        "malformed route hint",
        "multiple route hints",
        "route hint fees",
        "expired invoice",
        "excessive CLTV exposure",
        "uint32 height",
    ],
)
async def test_maintenance_payment_rejects_invalid_invoice_before_dispatch(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = FakeMaintenanceStub()
    configure_maintenance_payment(stub)
    monkeypatch.setattr("jmswap.lnd.time.time", lambda: 1_700_000_000)
    if case == "different destination":
        stub.decode_response = maintenance_pay_req(destination=OTHER_PEER_ID)
    elif case == "inconsistent whole-sat amount":
        stub.decode_response = maintenance_pay_req(num_msat=25_000_001)
    elif case == "missing route hint":
        stub.decode_response = maintenance_pay_req(route_hints=[])
    elif case == "malformed route hint":
        stub.decode_response = maintenance_pay_req(
            route_hints=[
                ln.RouteHint(hop_hints=[maintenance_pay_req().route_hints[0].hop_hints[0]])
            ]
        )
    elif case == "multiple route hints":
        valid_hint = maintenance_pay_req().route_hints[0]
        stub.decode_response = maintenance_pay_req(route_hints=[valid_hint, valid_hint])
    elif case == "route hint fees":
        stub.decode_response = maintenance_pay_req(
            route_hints=[
                ln.RouteHint(
                    hop_hints=[
                        ln.HopHint(
                            node_id=PEER_ID,
                            chan_id=1_001,
                            fee_base_msat=1,
                            fee_proportional_millionths=0,
                            cltv_expiry_delta=40,
                        ),
                        ln.HopHint(
                            node_id=OTHER_PEER_ID,
                            chan_id=2_002,
                            fee_base_msat=0,
                            fee_proportional_millionths=0,
                            cltv_expiry_delta=50,
                        ),
                    ]
                )
            ]
        )
    elif case == "expired invoice":
        stub.decode_response = maintenance_pay_req(timestamp=1, expiry=1)
    elif case == "excessive CLTV exposure":
        stub.decode_response = maintenance_pay_req(cltv_expiry=286)
    elif case == "uint32 height":
        stub.info_response = ln.GetInfoResponse(
            identity_pubkey=LOCAL_ID, block_height=(1 << 32) - 1
        )
    else:
        raise AssertionError(f"unhandled test case {case}")

    router = FakeRouterStub()
    with pytest.raises(LndValidationError):
        await backend(stub, router).send_maintenance_payment("lnbcrt-maintenance", 999, 8)
    assert router.send_to_route_requests == []
    assert router.send_payment_requests == []


@pytest.mark.parametrize(
    "case", ["wrong outgoing peer", "missing return", "inactive return", "ambiguous return"]
)
async def test_maintenance_payment_binds_exact_local_channels_before_dispatch(case: str) -> None:
    stub = FakeMaintenanceStub()
    configure_maintenance_payment(stub)
    if case == "wrong outgoing peer":
        stub.list_response.channels[0].remote_pubkey = OTHER_PEER_ID
    elif case == "missing return":
        del stub.list_response.channels[1]
    elif case == "inactive return":
        stub.list_response.channels[1].active = False
    elif case == "ambiguous return":
        stub.list_response.channels.append(
            listed_channel(
                channel_point="dd" * 32 + ":7",
                chan_id=1_001,
                remote_pubkey=OTHER_PEER_ID,
                active=True,
                private=True,
                peer_scid_alias=2_002,
            )
        )
    else:
        raise AssertionError(f"unhandled test case {case}")

    router = FakeRouterStub()
    with pytest.raises(LndValidationError):
        await backend(stub, router).send_maintenance_payment("lnbcrt-maintenance", 999, 8)
    assert router.send_to_route_requests == []
    assert router.send_payment_requests == []


async def test_maintenance_payment_times_out_or_cancels_without_retry() -> None:
    stub = FakeMaintenanceStub()
    configure_maintenance_payment(stub)
    router = FakeRouterStub()
    router.send_to_route_delay = 1.1
    target = backend(stub, router)

    payment = await target.send_maintenance_payment("lnbcrt-maintenance", 999, 1)

    assert payment == MaintenancePayment(status="in_flight", fee_msat=None)
    assert len(router.send_to_route_requests) == 1
    assert router.send_payment_requests == []

    router.send_to_route_delay = 0
    router.send_to_route_gate = asyncio.Event()
    task = asyncio.create_task(target.send_maintenance_payment("lnbcrt-maintenance", 999, 8))
    while len(router.send_to_route_requests) == 1:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(router.send_to_route_requests) == 2
    assert router.send_payment_requests == []


async def test_maintenance_payment_rejects_nonzero_terminal_fee_and_tracks_once() -> None:
    stub = FakeMaintenanceStub()
    configure_maintenance_payment(stub)
    router = FakeRouterStub()
    target = backend(stub, router)
    router.send_to_route_response = successful_attempt(total_fees_msat=1)

    with pytest.raises(LndValidationError, match="nonzero route fees"):
        await target.send_maintenance_payment("lnbcrt-maintenance", 999, 8)
    assert len(router.send_to_route_requests) == 1

    router.track_stream = FakeStream(
        [
            ln.Payment(status=ln.Payment.IN_FLIGHT),
            ln.Payment(status=ln.Payment.SUCCEEDED, fee_msat=0),
        ]
    )
    payment = await target.track_maintenance_payment("ab" * 32)

    assert payment == MaintenancePayment(status="in_flight", fee_msat=None)
    assert router.track_stream.read_count == 1
    assert router.track_stream.cancel_count == 1
    request = router.track_requests[0]
    assert bytes(request.payment_hash) == bytes.fromhex("ab" * 32)
    assert request.no_inflight_updates is False


async def test_maintenance_payment_rejects_malformed_attempt_results() -> None:
    stub = FakeMaintenanceStub()
    configure_maintenance_payment(stub)
    router = FakeRouterStub()
    target = backend(stub, router)

    router.send_to_route_response = object()
    with pytest.raises(LndValidationError, match="invalid maintenance attempt"):
        await target.send_maintenance_payment("lnbcrt-maintenance", 999, 8)

    router.send_to_route_response = successful_attempt(preimage=b"x" * 32)
    with pytest.raises(LndValidationError, match="invalid preimage"):
        await target.send_maintenance_payment("lnbcrt-maintenance", 999, 8)
    assert len(router.send_to_route_requests) == 2


@pytest.mark.parametrize("field", ["active", "private"])
async def test_exact_route_rejects_ineligible_outgoing_channel(field: str) -> None:
    stub = FakeMaintenanceStub()
    configure_maintenance_payment(stub)
    setattr(stub.list_response.channels[0], field, False)
    router = FakeRouterStub()
    with pytest.raises(LndValidationError, match="outgoing maintenance channel must"):
        await backend(stub, router).send_maintenance_payment("lnbcrt-maintenance", 999, 8)
    assert not router.send_to_route_requests


async def test_tracking_unknown_payment_is_not_reported_as_failure() -> None:
    router = FakeRouterStub()
    router.track_stream = FakeStream(
        error=FakeGrpcError(grpc.StatusCode.NOT_FOUND, "payment not found")
    )

    payment = await backend(router=router).track_maintenance_payment("cd" * 32)

    assert payment == MaintenancePayment(status="unknown", fee_msat=None)


async def test_set_private_channel_zero_fee_writes_only_that_channel_and_preserves_cltv() -> None:
    stub = FakeMaintenanceStub()
    stub.list_response = ln.ListChannelsResponse(channels=[listed_channel(private=True)])
    stub.graph_response = ln.ChannelGraph(edges=[graph_edge(local_cltv_delta=91)])
    target = backend(stub)

    await target.set_private_channel_zero_fee(CHANNEL_POINT)

    request = stub.update_requests[0]
    assert request.WhichOneof("scope") == "chan_point"
    assert getattr(request, "global") is False
    assert request.chan_point.funding_txid_str == "aa" * 32
    assert request.chan_point.output_index == 4
    assert request.base_fee_msat == 0
    assert request.fee_rate_ppm == 0
    assert request.time_lock_delta == 91

    stub.list_response = ln.ListChannelsResponse(channels=[listed_channel(private=False)])
    with pytest.raises(LndValidationError, match="public channel"):
        await target.set_private_channel_zero_fee(CHANNEL_POINT)
    assert len(stub.update_requests) == 1

    stub.list_response = ln.ListChannelsResponse(channels=[])
    with pytest.raises(LndValidationError, match="not found"):
        await target.set_private_channel_zero_fee(CHANNEL_POINT)
    assert len(stub.update_requests) == 1


def test_router_uses_the_existing_tls_and_macaroon_authenticated_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jmswap.lndrpc.invoices_pb2_grpc as invoices_grpc
    import jmswap.lndrpc.lightning_pb2_grpc as lightning_grpc
    import jmswap.lndrpc.router_pb2_grpc as router_grpc

    calls: dict[str, Any] = {}
    channel = object()

    class LightningStub:
        def __init__(self, received_channel: Any) -> None:
            calls["lightning_channel"] = received_channel

    class RouterStub:
        def __init__(self, received_channel: Any) -> None:
            calls["router_channel"] = received_channel

    class InvoicesStub:
        def __init__(self, received_channel: Any) -> None:
            calls["invoices_channel"] = received_channel

    def ssl_channel_credentials(cert: bytes) -> str:
        calls["tls_cert"] = cert
        return "tls"

    def metadata_call_credentials(callback: Any) -> str:
        calls["macaroon_callback"] = callback
        return "macaroon"

    def composite_channel_credentials(tls: str, macaroon: str) -> str:
        calls["credentials"] = (tls, macaroon)
        return "credentials"

    def secure_channel(host: str, credentials: str) -> object:
        calls["channel_args"] = (host, credentials)
        calls["channel_count"] = calls.get("channel_count", 0) + 1
        return channel

    monkeypatch.setattr(grpc, "ssl_channel_credentials", ssl_channel_credentials)
    monkeypatch.setattr(grpc, "metadata_call_credentials", metadata_call_credentials)
    monkeypatch.setattr(grpc, "composite_channel_credentials", composite_channel_credentials)
    monkeypatch.setattr(grpc.aio, "secure_channel", secure_channel)
    monkeypatch.setattr(lightning_grpc, "LightningStub", LightningStub)
    monkeypatch.setattr(router_grpc, "RouterStub", RouterStub)
    monkeypatch.setattr(invoices_grpc, "InvoicesStub", InvoicesStub)

    target = LndBackend(host="lnd:10009", tls_cert=b"tls-cert", macaroon_hex="deadbeef")
    target._ensure_router()
    target._ensure_invoices()
    calls["macaroon_callback"](
        None, lambda metadata, error: calls.update(metadata=metadata, metadata_error=error)
    )

    assert calls["tls_cert"] == b"tls-cert"
    assert calls["credentials"] == ("tls", "macaroon")
    assert calls["channel_args"] == ("lnd:10009", "credentials")
    assert calls["metadata"] == (("macaroon", "deadbeef"),)
    assert calls["metadata_error"] is None
    assert calls["lightning_channel"] is channel
    assert calls["router_channel"] is channel
    assert calls["invoices_channel"] is channel
    assert calls["channel_count"] == 1


async def test_close_resets_the_invoices_stub_with_the_shared_channel() -> None:
    class ClosableChannel:
        def __init__(self) -> None:
            self.close_count = 0

        async def close(self) -> None:
            self.close_count += 1

    channel = ClosableChannel()
    target = LndBackend(host="localhost:10009", tls_cert=b"", macaroon_hex="")
    target._channel = channel
    target._stub = object()
    target._router_stub = object()
    target._invoices_stub = object()

    await target.close()

    assert channel.close_count == 1
    assert target._stub is None
    assert target._router_stub is None
    assert target._invoices_stub is None
