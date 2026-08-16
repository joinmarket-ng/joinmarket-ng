"""
LDK Server backend: a :class:`~jmswap.lightning.LightningBackend` over LDK
Server's gRPC-over-HTTP/2 API.

The swap operations map onto LDK Server RPCs:

================================  ================================
LightningBackend method           LDK Server RPC
================================  ================================
``create_invoice``                ``Bolt11Receive``
``create_hold_invoice``           ``Bolt11ReceiveForHash``
``pay_invoice`` / ``get_payment`` ``Bolt11Send`` + ``GetPaymentDetails``
``settle_hold_invoice``           ``Bolt11ClaimForHash``
``cancel_hold_invoice``           ``Bolt11FailForHash``
``decode_invoice``                ``DecodeInvoice``
``node_id``                       ``GetNodeInfo``
================================  ================================

A self-contained minimal protobuf codec encodes/decodes exactly the messages
used here (no codegen/runtime protobuf dependency), the gRPC length-prefix frame
is added/stripped, and requests are authenticated with LDK Server's
``x-auth: HMAC <ts>:<hmac>`` scheme. The HTTP/2 transport is injected so the
codec and auth are unit-testable without a live server; :class:`HttpxLdkTransport`
provides a production transport when ``httpx[http2]`` is installed.
"""

from __future__ import annotations

import abc
import hashlib
import hmac
import time

from jmswap.channel_funding import ChannelInputEscrowCapability
from jmswap.errors import LightningError
from jmswap.lightning import (
    DecodedInvoice,
    InboundStatus,
    LightningBackend,
    PaymentResult,
    PaymentStatus,
)

# --- Minimal protobuf codec (wire types 0=varint, 2=length-delimited) -------


def _varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("varint must be non-negative")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def f_uint(field: int, value: int) -> bytes:
    return _tag(field, 0) + _varint(value)


def f_str(field: int, value: str) -> bytes:
    raw = value.encode("utf-8")
    return _tag(field, 2) + _varint(len(raw)) + raw


def f_msg(field: int, value: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(value)) + value


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = 0
    result = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7


def parse_message(buf: bytes) -> dict[int, list[int | bytes]]:
    """Parse a protobuf message into ``{field_number: [values...]}``.

    Varint fields yield ``int`` values; length-delimited fields yield ``bytes``.
    """
    out: dict[int, list[int | bytes]] = {}
    i = 0
    n = len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        field = key >> 3
        wire = key & 0x07
        value: int | bytes
        if wire == 0:
            value, i = _read_varint(buf, i)
        elif wire == 2:
            length, i = _read_varint(buf, i)
            value = buf[i : i + length]
            i += length
        elif wire == 5:
            value = int.from_bytes(buf[i : i + 4], "little")
            i += 4
        elif wire == 1:
            value = int.from_bytes(buf[i : i + 8], "little")
            i += 8
        else:
            raise LightningError(f"unsupported protobuf wire type {wire}")
        out.setdefault(field, []).append(value)
    return out


def _first_bytes(parsed: dict[int, list[int | bytes]], field: int) -> bytes | None:
    vals = parsed.get(field)
    if not vals:
        return None
    v = vals[0]
    return v if isinstance(v, bytes) else None


def _first_str(parsed: dict[int, list[int | bytes]], field: int) -> str | None:
    raw = _first_bytes(parsed, field)
    return raw.decode("utf-8") if raw is not None else None


def _first_int(parsed: dict[int, list[int | bytes]], field: int) -> int | None:
    vals = parsed.get(field)
    if not vals:
        return None
    v = vals[0]
    return v if isinstance(v, int) else None


# --- gRPC framing + auth -----------------------------------------------------


def grpc_frame(message: bytes) -> bytes:
    """Wrap a protobuf message in a gRPC length-prefixed frame."""
    return b"\x00" + len(message).to_bytes(4, "big") + message


def grpc_unframe(body: bytes) -> bytes:
    """Strip the 5-byte gRPC frame header, returning the message bytes."""
    if len(body) < 5:
        raise LightningError("gRPC response too short")
    if body[0] != 0x00:
        raise LightningError("compressed gRPC frames are not supported")
    length = int.from_bytes(body[1:5], "big")
    return body[5 : 5 + length]


def auth_header(api_key: str, grpc_body: bytes, timestamp: int | None = None) -> str:
    """Build the ``x-auth: HMAC <ts>:<hmac>`` header value for LDK Server."""
    ts = timestamp if timestamp is not None else int(time.time())
    mac = hmac.new(
        api_key.encode("utf-8"),
        ts.to_bytes(8, "big") + grpc_body,
        hashlib.sha256,
    ).hexdigest()
    return f"HMAC {ts}:{mac}"


# --- Transport ---------------------------------------------------------------


class LdkTransport(abc.ABC):
    """An async unary gRPC transport (one request -> one response message)."""

    @abc.abstractmethod
    async def unary(self, method: str, grpc_body: bytes, auth: str) -> bytes:
        """POST a framed gRPC request, returning the framed gRPC response body."""


class HttpxLdkTransport(LdkTransport):
    """HTTP/2 transport using ``httpx`` (requires ``httpx[http2]``)."""

    def __init__(self, base_url: str, *, cert: str | None = None, verify: bool | str = True):
        self._base_url = base_url.rstrip("/")
        # A self-signed LDK Server cert is pinned by passing its PEM path as the
        # verification bundle (httpx accepts a path here).
        self._verify: bool | str = cert if cert else verify

    async def unary(self, method: str, grpc_body: bytes, auth: str) -> bytes:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise LightningError("httpx[http2] is required for HttpxLdkTransport") from exc
        url = f"{self._base_url}/api.LightningNode/{method}"
        headers = {"content-type": "application/grpc+proto", "x-auth": auth, "te": "trailers"}
        async with httpx.AsyncClient(http2=True, verify=self._verify) as client:
            resp = await client.post(url, content=grpc_body, headers=headers)
            grpc_status = resp.headers.get("grpc-status")
            if grpc_status not in (None, "0"):
                msg = resp.headers.get("grpc-message", "unknown gRPC error")
                raise LightningError(f"LDK Server gRPC error {grpc_status}: {msg}")
            return resp.content


class LdkServerBackend(LightningBackend):
    """A swap Lightning backend backed by an LDK Server node."""

    requires_pre_signing_funding_completion = False
    channel_input_escrow_capabilities: frozenset[ChannelInputEscrowCapability] = frozenset()

    def __init__(self, transport: LdkTransport, api_key: str) -> None:
        self._transport = transport
        self._api_key = api_key

    async def _call(self, method: str, request: bytes) -> dict[int, list[int | bytes]]:
        body = grpc_frame(request)
        auth = auth_header(self._api_key, body)
        response = await self._transport.unary(method, body, auth)
        return parse_message(grpc_unframe(response))

    async def node_id(self) -> str:
        parsed = await self._call("GetNodeInfo", b"")
        node = _first_str(parsed, 1)
        if node is None:
            raise LightningError("GetNodeInfo returned no node_id")
        return node

    async def create_invoice(
        self, amount_msat: int, description: str = "", expiry: int = 3600
    ) -> tuple[str, str]:
        req = f_uint(1, amount_msat) + f_uint(3, expiry)
        if description:
            req += f_msg(2, f_str(1, description))  # Bolt11InvoiceDescription.direct
        parsed = await self._call("Bolt11Receive", req)
        invoice = _first_str(parsed, 1)
        payment_hash = _first_str(parsed, 2)
        if invoice is None or payment_hash is None:
            raise LightningError("Bolt11Receive returned an incomplete response")
        return invoice, payment_hash

    async def create_hold_invoice(
        self, amount_msat: int, payment_hash: str, description: str = "", expiry: int = 3600
    ) -> str:
        req = f_uint(1, amount_msat) + f_uint(3, expiry) + f_str(4, payment_hash)
        if description:
            req += f_msg(2, f_str(1, description))
        parsed = await self._call("Bolt11ReceiveForHash", req)
        invoice = _first_str(parsed, 1)
        if invoice is None:
            raise LightningError("Bolt11ReceiveForHash returned no invoice")
        return invoice

    async def pay_invoice(
        self,
        invoice: str,
        amount_msat: int | None = None,
        *,
        timeout: float = 120.0,
        poll_interval: float = 1.0,
    ) -> PaymentResult:
        import asyncio

        req = f_str(1, invoice)
        if amount_msat is not None:
            req += f_uint(2, amount_msat)
        parsed = await self._call("Bolt11Send", req)
        payment_id = _first_str(parsed, 1)
        if payment_id is None:
            raise LightningError("Bolt11Send returned no payment_id")
        # Bolt11Send returns immediately; poll GetPaymentDetails until the payment
        # reaches a terminal state (and the preimage becomes available).
        deadline = asyncio.get_event_loop().time() + timeout
        result = await self.get_payment(payment_id)
        while result.status == PaymentStatus.PENDING and asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(poll_interval)
            result = await self.get_payment(payment_id)
        return result

    async def get_payment(self, payment_id: str) -> PaymentResult:
        parsed = await self._call("GetPaymentDetails", f_str(1, payment_id))
        payment_raw = _first_bytes(parsed, 1)
        if payment_raw is None:
            return PaymentResult(payment_id, PaymentStatus.FAILED, None)
        payment = parse_message(payment_raw)
        status_int = _first_int(payment, 5) or 0
        status = {
            0: PaymentStatus.PENDING,
            1: PaymentStatus.SUCCEEDED,
            2: PaymentStatus.FAILED,
        }.get(status_int, PaymentStatus.PENDING)
        preimage = None
        kind_raw = _first_bytes(payment, 2)
        if kind_raw is not None:
            kind = parse_message(kind_raw)
            bolt11_raw = _first_bytes(kind, 2)  # PaymentKind.bolt11
            if bolt11_raw is not None:
                bolt11 = parse_message(bolt11_raw)
                preimage = _first_str(bolt11, 2)  # Bolt11.preimage
        return PaymentResult(payment_id, status, preimage)

    async def lookup_inbound(self, payment_hash: str) -> InboundStatus:
        # LDK Node keys inbound payments by their payment hash.
        parsed = await self._call("GetPaymentDetails", f_str(1, payment_hash))
        payment_raw = _first_bytes(parsed, 1)
        if payment_raw is None:
            return InboundStatus.UNKNOWN
        payment = parse_message(payment_raw)
        status_int = _first_int(payment, 5) or 0
        if status_int == 1:
            return InboundStatus.SETTLED
        if status_int == 2:
            return InboundStatus.FAILED
        # PENDING with a recorded amount means the inbound HTLC is held.
        return InboundStatus.HELD if _first_int(payment, 3) else InboundStatus.PENDING

    async def settle_hold_invoice(self, payment_hash: str, preimage: str) -> None:
        req = f_str(1, payment_hash) + f_str(3, preimage)
        await self._call("Bolt11ClaimForHash", req)

    async def cancel_hold_invoice(self, payment_hash: str) -> None:
        await self._call("Bolt11FailForHash", f_str(1, payment_hash))

    # -- External (CoinJoin) channel funding (forked LDK Server) --------------

    async def open_channel_external_funding(
        self,
        node_pubkey: str,
        address: str,
        channel_amount_sats: int,
        push_to_counterparty_msat: int | None = None,
        user_channel_id: str | None = None,
    ) -> str:
        """Open a channel whose funding output is provided externally.

        Returns the ``user_channel_id`` to track the open. Requires the LDK
        Server fork that exposes the external-funding RPCs.
        """
        if user_channel_id is not None:
            raise LightningError("LDK does not support caller-selected external funding IDs")
        req = f_str(1, node_pubkey) + f_str(2, address) + f_uint(3, channel_amount_sats)
        if push_to_counterparty_msat is not None:
            req += f_uint(4, push_to_counterparty_msat)
        parsed = await self._call("OpenChannelExternalFunding", req)
        ucid = _first_str(parsed, 1)
        if ucid is None:
            raise LightningError("OpenChannelExternalFunding returned no user_channel_id")
        return ucid

    async def get_funding_output(self, user_channel_id: str) -> tuple[bool, str, int]:
        """Poll for the funding output. Returns ``(ready, script_pubkey_hex, value_sats)``."""
        parsed = await self._call("GetFundingOutput", f_str(1, user_channel_id))
        ready = bool(_first_int(parsed, 1))
        return ready, _first_str(parsed, 2) or "", _first_int(parsed, 3) or 0

    async def provide_funding_outpoint(
        self,
        user_channel_id: str,
        funding_txid: str,
        funding_vout: int,
        raw_tx_hex: str | None = None,
        input_utxos: list[tuple[int, bytes]] | None = None,
    ) -> None:
        """Provide the outpoint of the externally-broadcast funding transaction.

        ``raw_tx_hex`` / ``input_utxos`` are accepted for interface parity with the LND
        backend (which needs the PSBT) and ignored here: LDK takes the outpoint after
        the funding tx is broadcast.
        """
        req = f_str(1, user_channel_id) + f_str(2, funding_txid) + f_uint(3, funding_vout)
        await self._call("ProvideFundingOutpoint", req)

    async def cancel_external_channel_funding(self, user_channel_id: str) -> None:
        """LDK's external-funding API has no pending-open cancellation RPC."""

    async def decode_invoice(self, invoice: str) -> DecodedInvoice:
        parsed = await self._call("DecodeInvoice", f_str(1, invoice))
        payment_hash = _first_str(parsed, 2)
        if payment_hash is None:
            raise LightningError("DecodeInvoice returned no payment_hash")
        return DecodedInvoice(
            payment_hash=payment_hash,
            amount_msat=_first_int(parsed, 3),
            expiry=_first_int(parsed, 5) or 3600,
        )
