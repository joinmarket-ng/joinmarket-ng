"""Typed async adapter over the LND ``Lightning`` and ``routerrpc.Router`` APIs.

The private buyout protocol needs two things from a node that the escrow
service does not provide: a peer-to-peer transport for its payloads (BOLT 1
custom messages of type :data:`~jmswap.buyout_messages.BUYOUT_CUSTOM_MESSAGE_TYPE`)
and the invoice it settles the buyout with. This module is the transport
boundary for both, plus the handful of read-only calls a runtime needs to judge
its own node (chain sync, channel metadata) and the two on-chain-adjacent calls
the flow ends with (a P2TR address, a channel close).

It is a mapping layer and nothing else: it holds no session state, remembers no
invoice, caches nothing between calls and never retries. In particular a payment
is attempted exactly once per :meth:`LndPeerClient.pay` call; if the call fails
or its deadline expires the payment may still be in flight, and recovering that
is :meth:`LndPeerClient.track_payment` driven by the runtime, never a retry made
here. Deciding whether a channel is eligible, whether an invoice's expiry and
network are acceptable and whether a decoded invoice is the one that was agreed
are all questions for the protocol layer; this module only guarantees that what
it returns is structurally what the call promised.

Credentials never leave this module: the TLS certificate and macaroon are held
privately, the macaroon is attached per call and only to calls addressed to the
two services named in :data:`SERVICE_NAMES`, the channel is always
TLS-authenticated with no insecure fallback, and neither the repr of the client
nor any raised error carries credentials or backend error detail. gRPC failures
are reported as a status code only; cancellation of the surrounding task is
propagated untouched.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum, IntEnum
from types import TracebackType
from typing import Annotated, Any, Final

import grpc
from bitcointx.core.key import CPubKey
from pydantic import BaseModel, ConfigDict, Field

from jmswap.buyout_messages import (
    BUYOUT_CUSTOM_MESSAGE_TYPE,
    MAX_BOLT11_CHARS,
    MAX_MONEY,
    MAX_PAYLOAD_BYTES,
    MAX_UINT32,
    CompressedPubKey,
    Outpoint,
    decode_buyout_payload,
)
from jmswap.lndrpc import lightning_pb2 as pb
from jmswap.lndrpc import lightning_pb2_grpc as pb_grpc
from jmswap.lndrpc import router_pb2 as router_pb
from jmswap.lndrpc import router_pb2_grpc as router_grpc

LIGHTNING_SERVICE_NAME: Final = "lnrpc.Lightning"
ROUTER_SERVICE_NAME: Final = "routerrpc.Router"
SERVICE_NAMES: Final = (LIGHTNING_SERVICE_NAME, ROUTER_SERVICE_NAME)
"""The only fully qualified gRPC services the macaroon is attached to."""

ALLOWED_CHAIN: Final = "bitcoin"
"""The only chain a buyout node may be on; LND reports at most one."""

PREIMAGE_LENGTH: Final = 32
PAYMENT_HASH_LENGTH: Final = 32

MAX_ROUTE_HINT_PATHS: Final = 20
"""Most route hint paths one client may be configured with."""

MAX_ROUTE_HINT_HOPS: Final = 20
"""Most hops one route hint path may describe."""

_MAX_UINT64: Final = 2**64 - 1
_MAX_UINT16: Final = 65_535

_COMPRESSED_PUBKEY_LENGTH: Final = 33
_MSAT_PER_SAT: Final = 1_000
_BOLT11_SHAPE: Final = re.compile(r"ln[0-9a-z]+")
_CHANNEL_POINT_SHAPE: Final = re.compile(r"(?P<txid>[0-9a-f]{64}):(?P<vout>0|[1-9][0-9]*)")
_HEX32_SHAPE: Final = re.compile(r"[0-9a-f]{64}")


class LndPeerError(Exception):
    """Base error of the peer and invoice adapter."""


class LndPeerRpcError(LndPeerError):
    """A Lightning or Router RPC failed.

    Only the gRPC status code is reported: backend error strings can quote
    channel state, routes and invoice detail and are never surfaced or chained.
    """

    def __init__(self, method: str, code: grpc.StatusCode | None) -> None:
        self.method = method
        self.code = code
        name = code.name if code is not None else "UNKNOWN"
        super().__init__(f"LND {method} failed with status {name}")


class LndPeerResponseError(LndPeerError):
    """The node answered with data that is malformed or self-contradictory."""


class InvoiceState(IntEnum):
    """Invoice state as LND records it (``lnrpc.Invoice.InvoiceState``)."""

    OPEN = 0
    SETTLED = 1
    CANCELED = 2
    ACCEPTED = 3


class PaymentStatus(Enum):
    """Outcome of a payment attempt.

    LND's ``INITIATED`` and ``IN_FLIGHT`` both mean "not resolved yet" to a
    caller that cannot act on the difference, so they collapse into
    :attr:`IN_FLIGHT`.
    """

    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


def _response_error(message: str) -> LndPeerResponseError:
    return LndPeerResponseError(f"LND {message}")


def _require_argument_bytes(value: bytes, length: int, name: str) -> bytes:
    if type(value) is not bytes or len(value) != length:
        raise ValueError(f"{name} must be exactly {length} bytes")
    return value


def _require_argument_pubkey(value: bytes, name: str) -> bytes:
    raw = _require_argument_bytes(value, _COMPRESSED_PUBKEY_LENGTH, name)
    if raw[0] not in (0x02, 0x03) or not CPubKey(raw).is_fullyvalid():
        raise ValueError(f"{name} is not a valid compressed public key")
    return raw


def _require_argument_count(value: int, name: str, minimum: int) -> int:
    if isinstance(value, bool) or type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    if value > MAX_MONEY:
        raise ValueError(f"{name} is out of range")
    return value


def _require_bolt11(bolt11: str) -> str:
    if (
        not isinstance(bolt11, str)
        or len(bolt11) > MAX_BOLT11_CHARS
        or not _BOLT11_SHAPE.fullmatch(bolt11)
    ):
        raise ValueError("bolt11 must be a lowercase BOLT 11 payment request")
    return bolt11


def _is_valid_pubkey(raw: bytes) -> bool:
    return (
        len(raw) == _COMPRESSED_PUBKEY_LENGTH
        and raw[0] in (0x02, 0x03)
        and bool(CPubKey(raw).is_fullyvalid())
    )


def _require_response_amount(value: int, name: str) -> int:
    amount = int(value)
    if not 0 <= amount <= MAX_MONEY:
        raise _response_error(f"{name} is not a satoshi amount")
    return amount


def _require_response_hex32(value: str, name: str) -> bytes:
    text = str(value)
    if not _HEX32_SHAPE.fullmatch(text):
        raise _response_error(f"{name} is not a 32-byte lowercase hex value")
    return bytes.fromhex(text)


def _require_response_pubkey_hex(value: str, name: str) -> bytes:
    text = str(value)
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        raise _response_error(f"{name} is not hex") from None
    if text != text.lower() or not _is_valid_pubkey(raw):
        raise _response_error(f"{name} is not a valid compressed public key")
    return raw


def _channel_point(point: Outpoint) -> pb.ChannelPoint:
    """Convert a display-order outpoint into LND's internal-order channel point."""
    if not isinstance(point, Outpoint):
        raise ValueError("channel point must be an Outpoint")
    return pb.ChannelPoint(
        funding_txid_bytes=bytes.fromhex(point.txid)[::-1], output_index=point.vout
    )


def _parse_channel_point(value: str) -> Outpoint:
    """Parse LND's ``txid:index`` channel point, which is already display order."""
    match = _CHANNEL_POINT_SHAPE.fullmatch(str(value))
    if match is None:
        raise _response_error("channel point is not a txid:index outpoint")
    try:
        return Outpoint(txid=match["txid"], vout=int(match["vout"]))
    except ValueError:
        raise _response_error("channel point is not a valid outpoint") from None


class PaymentRouteHop(BaseModel):
    """One hop of an operator-configured route hint, as ``lnrpc.HopHint``.

    The fields are LND's own, with LND's ranges: a channel and a CLTV delta must
    be present (both are positive), and the two fee terms are uint32 and may be
    zero. A hint says how to reach the destination through a channel the payer's
    graph does not advertise; it is the payer's own routing input and never
    leaves this node, so nothing here is negotiated or sent to a counterparty.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, hide_input_in_errors=True)

    node_id: CompressedPubKey
    chan_id: Annotated[int, Field(ge=1, le=_MAX_UINT64)]
    fee_base_msat: Annotated[int, Field(ge=0, le=MAX_UINT32)]
    fee_proportional_millionths: Annotated[int, Field(ge=0, le=MAX_UINT32)]
    cltv_expiry_delta: Annotated[int, Field(ge=1, le=_MAX_UINT16)]


def _require_route_hints(value: Any) -> tuple[tuple[PaymentRouteHop, ...], ...]:
    """Validate and copy configured route hints; errors never quote a hint."""
    if type(value) is not tuple:
        raise ValueError("payment_route_hints must be a tuple of route hint paths")
    if len(value) > MAX_ROUTE_HINT_PATHS:
        raise ValueError(f"payment_route_hints must hold at most {MAX_ROUTE_HINT_PATHS} paths")
    paths: list[tuple[PaymentRouteHop, ...]] = []
    for path in value:
        if type(path) is not tuple or not path:
            raise ValueError("each payment route hint must be a non-empty tuple of hops")
        if len(path) > MAX_ROUTE_HINT_HOPS:
            raise ValueError(f"a payment route hint must hold at most {MAX_ROUTE_HINT_HOPS} hops")
        if any(type(hop) is not PaymentRouteHop for hop in path):
            raise ValueError("each payment route hint hop must be a PaymentRouteHop")
        paths.append(tuple(path))
    return tuple(paths)


@dataclass(frozen=True)
class PeerMessage:
    """One buyout payload received from a peer over a BOLT 1 custom message."""

    peer_pubkey: bytes
    payload: bytes


@dataclass(frozen=True)
class NodeInfo:
    """What the local node reports about itself and its chain view."""

    identity_pubkey: str
    block_height: int
    synced_to_chain: bool
    network: str


@dataclass(frozen=True)
class ChannelInfo:
    """One open channel, with the metadata a runtime needs to judge eligibility.

    ``commitment_type`` is LND's enum name (a buyout needs
    ``SIMPLE_TAPROOT``-class channels), and the balances are the off-chain
    balances of the current commitment: they sum to at most the capacity, since
    the commitment fee and any pending HTLC value are not in either balance.
    """

    point: Outpoint
    peer_pubkey: str
    active: bool
    private: bool
    capacity_sat: int
    local_balance_sat: int
    remote_balance_sat: int
    pending_htlcs: int
    commitment_type: str

    def __post_init__(self) -> None:
        if not isinstance(self.point, Outpoint):
            raise _response_error("channel point is not an outpoint")
        _require_response_pubkey_hex(self.peer_pubkey, "channel peer pubkey")
        capacity = _require_response_amount(self.capacity_sat, "channel capacity")
        if capacity <= 0:
            raise _response_error("channel capacity must be positive")
        local = _require_response_amount(self.local_balance_sat, "channel local balance")
        remote = _require_response_amount(self.remote_balance_sat, "channel remote balance")
        if local + remote > capacity:
            raise _response_error("channel balances exceed the channel capacity")
        if int(self.pending_htlcs) < 0:
            raise _response_error("channel pending HTLC count is negative")
        if not self.commitment_type:
            raise _response_error("channel omits its commitment type")


@dataclass(frozen=True)
class InvoiceInfo:
    """A BOLT 11 invoice as the local node decoded it.

    The node parses and checksums the request; this adapter only requires the
    fields a buyout settlement depends on to be present, positive and whole
    satoshis. Whether the expiry, the destination and the network are acceptable
    is the runtime's decision, made against :class:`NodeInfo`.
    """

    payment_hash: bytes
    destination: bytes
    amount_sat: int
    created_at: int
    expiry_seconds: int
    min_final_cltv: int


@dataclass(frozen=True)
class InvoiceStatus:
    """The live state of an invoice this node issued."""

    state: InvoiceState
    amount_paid_sat: int
    preimage: bytes | None


@dataclass(frozen=True)
class PaymentResult:
    """The current result of one payment attempt.

    ``preimage`` is present only once the payment succeeded, and is always the
    preimage of ``payment_hash``: a result that claims success without a
    matching preimage is rejected instead of returned.
    """

    status: PaymentStatus
    payment_hash: bytes
    preimage: bytes | None
    fee_sat: int


class _NodeMacaroon(grpc.AuthMetadataPlugin):
    """Attaches the node macaroon to the two buyout services, and nothing else."""

    def __init__(self, macaroon: bytes) -> None:
        self._metadata: tuple[tuple[str, str], ...] = (("macaroon", macaroon.hex()),)

    def __call__(
        self, context: grpc.AuthMetadataContext, callback: grpc.AuthMetadataPluginCallback
    ) -> None:
        service_url = str(context.service_url)
        if not any(service_url.endswith(f"/{name}") for name in SERVICE_NAMES):
            callback((), ValueError("node macaroon is scoped to the Lightning and Router services"))
            return
        callback(self._metadata, None)

    def __repr__(self) -> str:
        return "<node macaroon credentials>"


class LndPeerClient:
    """Async client for one LND node's peer messaging and invoice surface.

    Use it as an async context manager; the gRPC channel exists only inside the
    context. Every unary call carries the configured deadline. The two streaming
    calls are deliberately different: :meth:`receive` is a long-lived
    subscription and carries no deadline at all, while :meth:`pay` waits for the
    node's own payment timeout plus the configured deadline as transport slack.

    ``payment_route_hints`` is an operator's answer to "how do I reach that
    node": it is used by :meth:`pay` only, is never discovered, and never
    reaches an invoice or a peer. Leaving it empty is the default and pays
    exactly as before.
    """

    def __init__(
        self,
        endpoint: str,
        tls_certificate: bytes,
        macaroon: bytes,
        timeout: float = 30.0,
        payment_route_hints: tuple[tuple[PaymentRouteHop, ...], ...] = (),
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("endpoint must be a non-empty host:port string")
        if type(tls_certificate) is not bytes or not tls_certificate:
            raise ValueError("tls_certificate must be non-empty PEM bytes")
        if type(macaroon) is not bytes or not macaroon:
            raise ValueError("macaroon must be non-empty bytes")
        if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        self._endpoint = endpoint
        self._channel_credentials = grpc.ssl_channel_credentials(tls_certificate)
        self._call_credentials = grpc.metadata_call_credentials(_NodeMacaroon(macaroon))
        self._timeout = float(timeout)
        self._payment_route_hints = _require_route_hints(payment_route_hints)
        self._channel: grpc.aio.Channel | None = None
        self._lightning: Any = None
        self._router: Any = None

    def __repr__(self) -> str:
        return f"LndPeerClient(endpoint={self._endpoint!r}, timeout={self._timeout!r})"

    async def __aenter__(self) -> LndPeerClient:
        if self._channel is None:
            self._channel = grpc.aio.secure_channel(self._endpoint, self._channel_credentials)
            self._lightning = pb_grpc.LightningStub(self._channel)  # type: ignore[no-untyped-call]
            self._router = router_grpc.RouterStub(self._channel)  # type: ignore[no-untyped-call]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        channel, self._channel = self._channel, None
        self._lightning = self._router = None
        if channel is not None:
            await channel.close()

    def _stub(self, router: bool = False) -> Any:
        stub = self._router if router else self._lightning
        if stub is None:
            raise LndPeerError("client is not connected; use it as an async context manager")
        return stub

    async def _call(self, method: str, request: Any, router: bool = False) -> Any:
        """Issue one unary RPC under the client deadline with the node macaroon."""
        try:
            return await getattr(self._stub(router), method)(
                request, timeout=self._timeout, credentials=self._call_credentials
            )
        except grpc.aio.AioRpcError as exc:
            # Chaining would re-expose the backend's error detail.
            raise LndPeerRpcError(method, exc.code()) from None

    async def send(self, peer_pubkey: bytes, payload: bytes) -> None:
        """Send one buyout payload to ``peer_pubkey`` as a custom message.

        The payload is decoded first, so this client can only put canonical
        JMP-0011 messages on the wire; a payload the codec rejects raises
        :class:`~jmswap.buyout_messages.BuyoutMessageError` and no RPC is made.
        """
        peer = _require_argument_pubkey(peer_pubkey, "peer_pubkey")
        decode_buyout_payload(payload)
        await self._call(
            "SendCustomMessage",
            pb.SendCustomMessageRequest(
                peer=peer, type=BUYOUT_CUSTOM_MESSAGE_TYPE, data=bytes(payload)
            ),
        )

    async def receive(self) -> AsyncIterator[PeerMessage]:
        """Yield buyout payloads from peers until the iterator is closed.

        Only messages of the buyout type, from a peer key that parses, and
        within the payload bound reach the caller; everything else the node
        relays is dropped without a trace, because a peer must not be able to
        break the subscription or steer what its counterparty records. Closing
        the iterator, or cancelling the task awaiting it, cancels the underlying
        stream.
        """
        # A subscription has no deadline: it is expected to outlive any call.
        call = self._stub().SubscribeCustomMessages(
            pb.SubscribeCustomMessagesRequest(), credentials=self._call_credentials
        )
        try:
            async for update in call:
                message = _peer_message(update)
                if message is not None:
                    yield message
        except grpc.aio.AioRpcError as exc:
            raise LndPeerRpcError("SubscribeCustomMessages", exc.code()) from None
        finally:
            call.cancel()

    async def node_info(self) -> NodeInfo:
        """Report the node's identity and chain view."""
        response = await self._call("GetInfo", pb.GetInfoRequest())
        if len(response.chains) != 1:
            raise _response_error("info does not report exactly one chain")
        chain = response.chains[0]
        if chain.chain and chain.chain != ALLOWED_CHAIN:
            raise _response_error("info reports a node that is not on bitcoin")
        if not chain.network:
            raise _response_error("info omits the network")
        return NodeInfo(
            identity_pubkey=_require_response_pubkey_hex(
                response.identity_pubkey, "identity pubkey"
            ).hex(),
            block_height=int(response.block_height),
            synced_to_chain=bool(response.synced_to_chain),
            network=str(chain.network),
        )

    async def channels(self) -> list[ChannelInfo]:
        """List the open channels with the metadata eligibility is judged on."""
        response = await self._call("ListChannels", pb.ListChannelsRequest())
        return [_channel_info(channel) for channel in response.channels]

    async def new_address(self) -> str:
        """Return a fresh P2TR address from the node's default account."""
        response = await self._call(
            "NewAddress", pb.NewAddressRequest(type=pb.AddressType.TAPROOT_PUBKEY)
        )
        address = str(response.address)
        if not address:
            raise _response_error("new address response is empty")
        return address

    async def create_invoice(
        self,
        preimage: bytes,
        amount_sat: int,
        expiry_seconds: int,
        *,
        include_private_routes: bool = False,
    ) -> str:
        """Add a hold-free invoice for ``preimage`` and return its BOLT 11 string.

        The preimage is chosen by the caller, so the buyout can bind the payment
        to a secret it already committed to. The node's answer is only accepted
        if the invoice it created really is the one for that preimage.
        """
        raw = _require_argument_bytes(preimage, PREIMAGE_LENGTH, "preimage")
        amount = _require_argument_count(amount_sat, "amount_sat", 1)
        expiry = _require_argument_count(expiry_seconds, "expiry_seconds", 1)
        if type(include_private_routes) is not bool:
            raise ValueError("include_private_routes must be a bool")
        response = await self._call(
            "AddInvoice",
            pb.Invoice(r_preimage=raw, value=amount, expiry=expiry, private=include_private_routes),
        )
        if bytes(response.r_hash) != hashlib.sha256(raw).digest():
            raise _response_error("invoice is not for the requested preimage")
        payment_request = str(response.payment_request)
        if not payment_request:
            raise _response_error("invoice response omits the payment request")
        return payment_request

    async def invoice_request(self, payment_hash: bytes) -> str | None:
        """Recover an already-created invoice after an interrupted AddInvoice call."""
        raw = _require_argument_bytes(payment_hash, PAYMENT_HASH_LENGTH, "payment_hash")
        try:
            response = await self._call("LookupInvoice", pb.PaymentHash(r_hash=raw))
        except LndPeerRpcError as exc:
            if exc.code is grpc.StatusCode.NOT_FOUND:
                return None
            raise
        if bytes(response.r_hash) != raw:
            raise _response_error("invoice lookup answered for a different payment hash")
        request = str(response.payment_request)
        if not request:
            raise _response_error("invoice lookup omits the payment request")
        return request

    async def inspect_invoice(self, bolt11: str) -> InvoiceInfo:
        """Decode a BOLT 11 invoice with the local node.

        Decoding is delegated because the node parses and checks the request's
        signature and checksum; this adapter only insists that the invoice
        carries a positive, whole-satoshi amount, which a buyout settlement
        needs and which a millisatoshi remainder would silently break.
        """
        response = await self._call(
            "DecodePayReq", pb.PayReqString(pay_req=_require_bolt11(bolt11))
        )
        amount = _require_response_amount(response.num_satoshis, "invoice amount")
        if amount <= 0:
            raise _response_error("invoice amount is not positive")
        if int(response.num_msat) != amount * _MSAT_PER_SAT:
            raise _response_error("invoice amount is not a whole number of satoshis")
        created_at = int(response.timestamp)
        expiry = int(response.expiry)
        min_final_cltv = int(response.cltv_expiry)
        if created_at <= 0:
            raise _response_error("invoice omits its creation time")
        if expiry <= 0:
            raise _response_error("invoice omits its expiry")
        if min_final_cltv <= 0:
            raise _response_error("invoice omits its final CLTV delta")
        return InvoiceInfo(
            payment_hash=_require_response_hex32(response.payment_hash, "invoice payment hash"),
            destination=_require_response_pubkey_hex(response.destination, "invoice destination"),
            amount_sat=amount,
            created_at=created_at,
            expiry_seconds=expiry,
            min_final_cltv=min_final_cltv,
        )

    async def invoice_status(self, payment_hash: bytes) -> InvoiceStatus:
        """Look up an invoice this node issued by its payment hash."""
        raw = _require_argument_bytes(payment_hash, PAYMENT_HASH_LENGTH, "payment_hash")
        response = await self._call("LookupInvoice", pb.PaymentHash(r_hash=raw))
        if bytes(response.r_hash) != raw:
            raise _response_error("invoice lookup answered for a different payment hash")
        try:
            state = InvoiceState(int(response.state))
        except ValueError:
            raise _response_error("invoice reports an unknown state") from None
        preimage = bytes(response.r_preimage) or None
        if preimage is not None and hashlib.sha256(preimage).digest() != raw:
            raise _response_error("invoice preimage does not match its payment hash")
        if state is InvoiceState.SETTLED and preimage is None:
            raise _response_error("settled invoice omits its preimage")
        return InvoiceStatus(
            state=state,
            amount_paid_sat=_require_response_amount(response.amt_paid_sat, "invoice paid amount"),
            preimage=preimage,
        )

    async def pay(
        self,
        bolt11: str,
        *,
        fee_limit_sat: int,
        cltv_limit: int,
        timeout_seconds: int,
    ) -> PaymentResult:
        """Attempt to pay ``bolt11`` once, as a single-part payment.

        The caller states every bound explicitly: there is no default fee, CLTV
        or timeout policy here. The attempt is never repeated, not even after a
        transport failure: if this call does not return a result the payment may
        still be in flight and :meth:`track_payment` is the only way to learn
        its outcome.

        The configured route hints, if any, are added to this request and to
        nothing else. LND merges them with the hints the invoice itself carries
        and uses the result to build the route; the payee only ever sees the
        final onion payload, so a hint configured here is not observable by the
        counterparty and changes no bound above.
        """
        request = router_pb.SendPaymentRequest(
            payment_request=_require_bolt11(bolt11),
            route_hints=[
                pb.RouteHint(hop_hints=[pb.HopHint(**hop.model_dump()) for hop in path])
                for path in self._payment_route_hints
            ],
            fee_limit_sat=_require_argument_count(fee_limit_sat, "fee_limit_sat", 0),
            cltv_limit=_require_argument_count(cltv_limit, "cltv_limit", 1),
            timeout_seconds=_require_argument_count(timeout_seconds, "timeout_seconds", 1),
            # One attempt, one route: a split payment would settle the invoice
            # from several channels and is not what a buyout pays from.
            max_parts=1,
            no_inflight_updates=True,
        )
        call = self._stub(router=True).SendPaymentV2(
            request,
            timeout=float(timeout_seconds) + self._timeout,
            credentials=self._call_credentials,
        )
        update = await _first_update(call, "SendPaymentV2")
        if update is None:
            raise _response_error("payment stream ended without a result")
        return _payment_result(update)

    async def track_payment(self, payment_hash: bytes) -> PaymentResult | None:
        """Report the current result of a payment, or ``None`` if it is unknown.

        The first update the node sends is the payment as it stands now, which
        is what a runtime polls for; this call does not wait for the payment to
        resolve, so deciding when to look again is the runtime's business.
        """
        raw = _require_argument_bytes(payment_hash, PAYMENT_HASH_LENGTH, "payment_hash")
        call = self._stub(router=True).TrackPaymentV2(
            router_pb.TrackPaymentRequest(payment_hash=raw, no_inflight_updates=False),
            timeout=self._timeout,
            credentials=self._call_credentials,
        )
        try:
            update = await _first_update(call, "TrackPaymentV2")
        except LndPeerRpcError as exc:
            if exc.code is grpc.StatusCode.NOT_FOUND:
                return None
            raise
        if update is None:
            return None
        result = _payment_result(update)
        if result.payment_hash != raw:
            raise _response_error("payment update is for a different payment hash")
        return result

    async def close_channel(self, point: Outpoint, force: bool) -> None:
        """Start closing a channel and return once the node accepted the close.

        This waits for the node's first update only, under the client deadline;
        it never waits for the closing transaction to confirm. A backend that
        refuses to close a channel it has locked for an escrow fails the call,
        and that failure is the caller's to handle.
        """
        call = self._stub().CloseChannel(
            pb.CloseChannelRequest(channel_point=_channel_point(point), force=bool(force)),
            timeout=self._timeout,
            credentials=self._call_credentials,
        )
        update = await _first_update(call, "CloseChannel")
        if update is None:
            raise _response_error("close stream ended without an update")
        if update.WhichOneof("update") is None:
            raise _response_error("close update is empty")


async def _first_update(call: Any, method: str) -> Any | None:
    """Read the first message of a server stream, then drop the stream."""
    try:
        update = await call.read()
    except grpc.aio.AioRpcError as exc:
        raise LndPeerRpcError(method, exc.code()) from None
    finally:
        call.cancel()
    return None if update == grpc.aio.EOF else update


def _peer_message(update: Any) -> PeerMessage | None:
    """Map one custom message onto a buyout payload, or drop it."""
    if int(update.type) != BUYOUT_CUSTOM_MESSAGE_TYPE:
        return None
    peer = bytes(update.peer)
    payload = bytes(update.data)
    if not _is_valid_pubkey(peer) or not payload or len(payload) > MAX_PAYLOAD_BYTES:
        return None
    return PeerMessage(peer_pubkey=peer, payload=payload)


def _channel_info(channel: Any) -> ChannelInfo:
    try:
        commitment_type = pb.CommitmentType.Name(int(channel.commitment_type))
    except ValueError:
        raise _response_error("channel reports an unknown commitment type") from None
    return ChannelInfo(
        point=_parse_channel_point(channel.channel_point),
        peer_pubkey=str(channel.remote_pubkey),
        active=bool(channel.active),
        private=bool(channel.private),
        capacity_sat=int(channel.capacity),
        local_balance_sat=int(channel.local_balance),
        remote_balance_sat=int(channel.remote_balance),
        pending_htlcs=len(channel.pending_htlcs),
        commitment_type=commitment_type,
    )


_PAYMENT_STATUS: Final[dict[int, PaymentStatus]] = {
    pb.Payment.PaymentStatus.IN_FLIGHT: PaymentStatus.IN_FLIGHT,
    pb.Payment.PaymentStatus.INITIATED: PaymentStatus.IN_FLIGHT,
    pb.Payment.PaymentStatus.SUCCEEDED: PaymentStatus.SUCCEEDED,
    pb.Payment.PaymentStatus.FAILED: PaymentStatus.FAILED,
}


def _payment_result(update: Any) -> PaymentResult:
    status = _PAYMENT_STATUS.get(int(update.status))
    if status is None:
        raise _response_error("payment reports an unknown status")
    payment_hash = _require_response_hex32(update.payment_hash, "payment hash")
    preimage = _payment_preimage(update.payment_preimage)
    if status is PaymentStatus.SUCCEEDED:
        if preimage is None:
            raise _response_error("succeeded payment omits its preimage")
        if hashlib.sha256(preimage).digest() != payment_hash:
            raise _response_error("payment preimage does not match its payment hash")
    elif preimage is not None:
        raise _response_error("unsettled payment reports a preimage")
    return PaymentResult(
        status=status,
        payment_hash=payment_hash,
        preimage=preimage,
        fee_sat=_require_response_amount(update.fee_sat, "payment fee"),
    )


def _payment_preimage(value: str) -> bytes | None:
    """Read a payment preimage; LND reports an unknown one as empty or zeros."""
    text = str(value)
    if not text:
        return None
    raw = _require_response_hex32(text, "payment preimage")
    return None if raw == bytes(PREIMAGE_LENGTH) else raw
