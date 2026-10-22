"""Transport-level tests for the LND hold-invoice adapter."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import grpc
import pytest

from jmswap.lnd_invoices import (
    INVOICES_SERVICE_NAME,
    LndInvoiceClient,
    LndInvoiceError,
    LndInvoiceResponseError,
    LndInvoiceRpcError,
    _InvoicesMacaroon,
    payment_hash_for,
)
from jmswap.lnd_peer import InvoiceState, PaymentRouteHop
from jmswap.lndrpc import invoices_pb2 as invoices_pb
from jmswap.lndrpc import lightning_pb2 as pb

TLS_CERT = b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
MACAROON = bytes(range(64))
PREIMAGE = bytes([0x42]) * 32
PAYMENT_HASH = hashlib.sha256(PREIMAGE).digest()
BOLT11 = "lnbcrt10u1pjtestinvoice"


class RecordingStub:
    def __init__(self, **results: Any) -> None:
        self.results = results
        self.calls: list[SimpleNamespace] = []

    def __getattr__(self, method: str) -> Any:
        if method.startswith("_"):
            raise AttributeError(method)

        async def unary_call(request: Any, *, timeout: float, credentials: Any) -> Any:
            self.calls.append(SimpleNamespace(method=method, request=request, timeout=timeout))
            result = self.results[method]
            if isinstance(result, list):
                result = result.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result

        return unary_call


def rpc_error(
    code: grpc.StatusCode, details: str = "secret backend detail"
) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(code, grpc.aio.Metadata(), grpc.aio.Metadata(), details=details)


@asynccontextmanager
async def invoice_client(stub: RecordingStub) -> AsyncIterator[LndInvoiceClient]:
    client = LndInvoiceClient("127.0.0.1:10009", TLS_CERT, MACAROON, timeout=5.0)
    async with client:
        client._invoices = stub
        yield client


def lnd_invoice(**overrides: Any) -> pb.Invoice:
    fields: dict[str, Any] = {
        "r_hash": PAYMENT_HASH,
        "payment_request": BOLT11,
        "state": pb.Invoice.ACCEPTED,
        "amt_paid_msat": 1_000_000,
        "htlcs": [
            pb.InvoiceHTLC(state=pb.ACCEPTED, expiry_height=900),
            pb.InvoiceHTLC(state=pb.ACCEPTED, expiry_height=850),
            pb.InvoiceHTLC(state=pb.CANCELED, expiry_height=10),
        ],
    }
    fields.update(overrides)
    return pb.Invoice(**fields)


class RecordingCallback(grpc.AuthMetadataPluginCallback):
    def __init__(self) -> None:
        self.metadata: Any = None
        self.error: Any = None

    def __call__(self, metadata: Any, error: Any) -> None:
        self.metadata, self.error = metadata, error


@pytest.mark.parametrize(
    ("service_url", "attached"),
    [
        (f"https://127.0.0.1:10009/{INVOICES_SERVICE_NAME}", True),
        ("https://127.0.0.1:10009/lnrpc.Lightning", False),
        ("https://127.0.0.1:10009/channelescrowrpc.ChannelEscrow", False),
    ],
)
def test_macaroon_is_scoped_to_the_invoices_service(service_url: str, attached: bool) -> None:
    callback = RecordingCallback()
    _InvoicesMacaroon(MACAROON)(SimpleNamespace(service_url=service_url), callback)  # type: ignore[arg-type]
    assert (callback.metadata == (("macaroon", MACAROON.hex()),)) is attached
    assert (callback.error is None) is attached


def test_client_does_not_expose_credentials() -> None:
    client = LndInvoiceClient("127.0.0.1:10009", TLS_CERT, MACAROON)
    assert MACAROON.hex() not in repr(client)
    assert "macaroon" not in repr(_InvoicesMacaroon(MACAROON)).replace("invoice macaroon", "")


async def test_add_hold_invoice_maps_request() -> None:
    stub = RecordingStub(AddHoldInvoice=invoices_pb.AddHoldInvoiceResp(payment_request=BOLT11))
    async with invoice_client(stub) as client:
        assert await client.add_hold_invoice(PAYMENT_HASH, 1_000, 960) == BOLT11
    request = stub.calls[0].request
    assert (request.hash, request.value, request.expiry, request.private) == (
        PAYMENT_HASH,
        1_000,
        960,
        False,
    )
    assert request.memo == ""


async def test_add_hold_invoice_includes_only_explicit_validated_hints() -> None:
    hop = PaymentRouteHop(
        node_id="0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798",
        chan_id=42,
        fee_base_msat=1_000,
        fee_proportional_millionths=1,
        cltv_expiry_delta=40,
    )
    stub = RecordingStub(AddHoldInvoice=invoices_pb.AddHoldInvoiceResp(payment_request=BOLT11))
    async with invoice_client(stub) as client:
        assert (
            await client.add_hold_invoice(
                PAYMENT_HASH, 1_000, 960, private=True, invoice_route_hints=((hop,),)
            )
            == BOLT11
        )
    request = stub.calls[0].request
    assert request.private is True
    assert list(request.route_hints) == [pb.RouteHint(hop_hints=[pb.HopHint(**hop.model_dump())])]
    assert [call.method for call in stub.calls] == ["AddHoldInvoice"]


async def test_invalid_hold_invoice_hints_do_not_create_or_look_up_an_invoice() -> None:
    stub = RecordingStub()
    async with invoice_client(stub) as client:
        with pytest.raises(ValueError):
            await client.add_hold_invoice(PAYMENT_HASH, 1_000, 960, invoice_route_hints=((),))
    assert stub.calls == []


async def test_add_hold_invoice_resolves_an_existing_hash_by_lookup() -> None:
    stub = RecordingStub(
        AddHoldInvoice=rpc_error(grpc.StatusCode.UNKNOWN),
        LookupInvoiceV2=lnd_invoice(state=pb.Invoice.OPEN, htlcs=[]),
    )
    async with invoice_client(stub) as client:
        assert await client.add_hold_invoice(PAYMENT_HASH, 1_000, 960) == BOLT11
    assert [call.method for call in stub.calls] == ["AddHoldInvoice", "LookupInvoiceV2"]


async def test_add_hold_invoice_failure_without_invoice_raises_status_only() -> None:
    stub = RecordingStub(
        AddHoldInvoice=rpc_error(grpc.StatusCode.UNAVAILABLE),
        LookupInvoiceV2=rpc_error(grpc.StatusCode.NOT_FOUND),
    )
    async with invoice_client(stub) as client:
        with pytest.raises(LndInvoiceRpcError) as raised:
            await client.add_hold_invoice(PAYMENT_HASH, 1_000, 960)
    assert raised.value.code is grpc.StatusCode.UNAVAILABLE
    assert "secret" not in str(raised.value)


@pytest.mark.parametrize(
    ("payment_hash", "amount", "expiry"),
    [(b"\x00" * 31, 1, 1), (PAYMENT_HASH, 0, 1), (PAYMENT_HASH, 1, 0), (PAYMENT_HASH, 1, 3601)],
)
async def test_add_hold_invoice_rejects_bad_arguments(
    payment_hash: bytes, amount: int, expiry: int
) -> None:
    async with invoice_client(RecordingStub()) as client:
        with pytest.raises(ValueError):
            await client.add_hold_invoice(payment_hash, amount, expiry)


async def test_lookup_reports_state_amount_and_lowest_accepted_htlc_expiry() -> None:
    async with invoice_client(RecordingStub(LookupInvoiceV2=lnd_invoice())) as client:
        status = await client.lookup(PAYMENT_HASH)
    assert status is not None
    assert status.state is InvoiceState.ACCEPTED
    assert status.amount_paid_sats == 1_000
    assert status.min_htlc_expiry_height == 850
    assert status.payment_request == BOLT11


async def test_lookup_not_found_is_none_and_other_errors_raise() -> None:
    stub = RecordingStub(
        LookupInvoiceV2=[rpc_error(grpc.StatusCode.NOT_FOUND), rpc_error(grpc.StatusCode.INTERNAL)]
    )
    async with invoice_client(stub) as client:
        assert await client.lookup(PAYMENT_HASH) is None
        with pytest.raises(LndInvoiceRpcError):
            await client.lookup(PAYMENT_HASH)


async def test_lookup_on_a_node_without_invoices_is_none() -> None:
    """LND v0.21 reports an empty invoice database as UNKNOWN, not NOT_FOUND."""
    stub = RecordingStub(
        LookupInvoiceV2=[
            rpc_error(grpc.StatusCode.UNKNOWN, "there are no existing invoices"),
            rpc_error(grpc.StatusCode.UNKNOWN),
        ]
    )
    async with invoice_client(stub) as client:
        assert await client.lookup(PAYMENT_HASH) is None
        with pytest.raises(LndInvoiceRpcError) as failure:
            await client.lookup(PAYMENT_HASH)
    assert "secret" not in str(failure.value)


async def test_lookup_rejects_a_different_invoice() -> None:
    stub = RecordingStub(LookupInvoiceV2=lnd_invoice(r_hash=b"\x01" * 32))
    async with invoice_client(stub) as client:
        with pytest.raises(LndInvoiceResponseError):
            await client.lookup(PAYMENT_HASH)


async def test_settle_and_cancel_map_requests() -> None:
    stub = RecordingStub(
        SettleInvoice=invoices_pb.SettleInvoiceResp(),
        CancelInvoice=invoices_pb.CancelInvoiceResp(),
    )
    async with invoice_client(stub) as client:
        await client.settle(PREIMAGE)
        await client.cancel(PAYMENT_HASH)
    assert stub.calls[0].request.preimage == PREIMAGE
    assert stub.calls[1].request.payment_hash == PAYMENT_HASH


async def test_calls_require_an_entered_client() -> None:
    client = LndInvoiceClient("127.0.0.1:10009", TLS_CERT, MACAROON)
    with pytest.raises(LndInvoiceError):
        await client.cancel(PAYMENT_HASH)


def test_payment_hash_for() -> None:
    assert payment_hash_for(PREIMAGE) == PAYMENT_HASH
