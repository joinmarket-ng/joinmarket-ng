"""Tests for the LDK Server backend: protobuf codec, HMAC auth, RPC mapping."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from jmswap.errors import LightningError
from jmswap.ldk import (
    LdkServerBackend,
    LdkTransport,
    auth_header,
    f_msg,
    f_str,
    f_uint,
    grpc_frame,
    grpc_unframe,
    parse_message,
)
from jmswap.lightning import PaymentStatus


def test_protobuf_roundtrip_scalars() -> None:
    msg = f_uint(1, 50_000) + f_uint(3, 3600) + f_str(4, "ab" * 32)
    parsed = parse_message(msg)
    assert parsed[1] == [50_000]
    assert parsed[3] == [3600]
    assert parsed[4][0] == ("ab" * 32).encode()


def test_protobuf_nested_message() -> None:
    inner = f_str(1, "coffee")
    msg = f_uint(1, 1000) + f_msg(2, inner)
    parsed = parse_message(msg)
    assert parsed[1] == [1000]
    nested = parse_message(parsed[2][0])  # type: ignore[arg-type]
    assert nested[1][0] == b"coffee"


def test_grpc_frame_roundtrip() -> None:
    msg = f_uint(1, 42)
    framed = grpc_frame(msg)
    assert framed[0] == 0x00
    assert int.from_bytes(framed[1:5], "big") == len(msg)
    assert grpc_unframe(framed) == msg


def test_grpc_unframe_rejects_compressed() -> None:
    with pytest.raises(LightningError):
        grpc_unframe(b"\x01\x00\x00\x00\x01\x00")


def test_auth_header_matches_documented_scheme() -> None:
    api_key = "secret-key"
    body = grpc_frame(f_str(1, "x"))
    ts = 1_700_000_000
    header = auth_header(api_key, body, timestamp=ts)
    assert header.startswith(f"HMAC {ts}:")
    got = header.split(":", 1)[1]
    expected = hmac.new(api_key.encode(), ts.to_bytes(8, "big") + body, hashlib.sha256).hexdigest()
    assert got == expected


class _RecordingTransport(LdkTransport):
    """Captures the last call and returns a queued framed response."""

    def __init__(self, response_message: bytes) -> None:
        self.response_message = response_message
        self.last_method: str | None = None
        self.last_body: bytes | None = None
        self.last_auth: str | None = None

    async def unary(self, method: str, grpc_body: bytes, auth: str) -> bytes:
        self.last_method = method
        self.last_body = grpc_body
        self.last_auth = auth
        return grpc_frame(self.response_message)


async def test_create_hold_invoice_call() -> None:
    response = f_str(1, "lnbcrt500u1invoice")  # Bolt11ReceiveForHashResponse.invoice
    transport = _RecordingTransport(response)
    backend = LdkServerBackend(transport, "api-key")
    payment_hash = "cd" * 32
    invoice = await backend.create_hold_invoice(50_000, payment_hash, expiry=600)
    assert invoice == "lnbcrt500u1invoice"
    assert transport.last_method == "Bolt11ReceiveForHash"
    # decode the request we sent and check the fields
    sent = parse_message(grpc_unframe(transport.last_body))  # type: ignore[arg-type]
    assert sent[1] == [50_000]
    assert sent[3] == [600]
    assert sent[4][0] == payment_hash.encode()
    assert transport.last_auth is not None and transport.last_auth.startswith("HMAC ")


async def test_pay_invoice_polls_payment_details() -> None:
    # Bolt11Send returns a payment_id; the backend then polls GetPaymentDetails.
    # We emulate the second call by returning a Payment with a preimage.
    preimage = "ee" * 32

    class TwoStepTransport(LdkTransport):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def unary(self, method: str, grpc_body: bytes, auth: str) -> bytes:
            self.calls.append(method)
            if method == "Bolt11Send":
                return grpc_frame(f_str(1, "payment-123"))
            # GetPaymentDetails -> Payment{ status=SUCCEEDED, kind.bolt11.preimage }
            bolt11 = f_str(1, "ab" * 32) + f_str(2, preimage)
            kind = f_msg(2, bolt11)
            payment = f_str(1, "payment-123") + f_msg(2, kind) + f_uint(5, 1)
            return grpc_frame(f_msg(1, payment))

    transport = TwoStepTransport()
    backend = LdkServerBackend(transport, "api-key")
    result = await backend.pay_invoice("lnbcrt1invoice")
    assert transport.calls == ["Bolt11Send", "GetPaymentDetails"]
    assert result.status == PaymentStatus.SUCCEEDED
    assert result.preimage == preimage


async def test_settle_hold_invoice_call() -> None:
    transport = _RecordingTransport(b"")  # Bolt11ClaimForHashResponse is empty
    backend = LdkServerBackend(transport, "api-key")
    await backend.settle_hold_invoice("cd" * 32, "ee" * 32)
    assert transport.last_method == "Bolt11ClaimForHash"
    sent = parse_message(grpc_unframe(transport.last_body))  # type: ignore[arg-type]
    assert sent[1][0] == ("cd" * 32).encode()
    assert sent[3][0] == ("ee" * 32).encode()


async def test_decode_invoice_call() -> None:
    payment_hash = "12" * 32
    response = f_str(2, payment_hash) + f_uint(3, 123_000) + f_uint(5, 600)
    transport = _RecordingTransport(response)
    backend = LdkServerBackend(transport, "api-key")
    decoded = await backend.decode_invoice("lnbcrt1invoice")
    assert decoded.payment_hash == payment_hash
    assert decoded.amount_msat == 123_000
    assert decoded.expiry == 600
