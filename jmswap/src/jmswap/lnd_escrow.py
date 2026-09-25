"""Typed async adapter over the pinned LND ``ChannelEscrow`` gRPC service.

The backend freezes one SIMPLE_TAPROOT channel per session and then admits
exactly one parent transaction for it: once the channel input has been signed
the prepared parent can no longer be replaced. This module is the transport
boundary for that service and nothing else. It maps the seven RPCs onto typed
requests and responses, converts between the wire models used on the private
buyout protocol (:class:`jmswap.buyout_messages.Outpoint`,
:class:`jmswap.buyout_messages.Prevout`, display-order txids) and the internal
byte order LND speaks, and rejects a response that is not structurally what the
call promised.

It holds no lifecycle state, caches nothing between calls and makes no
decisions: which channel to freeze, which parent to prepare and whether a
finalized transaction really is the parent that was agreed are all questions for
the protocol layer, which knows the agreement. In particular ``finalize``
returns the backend's serialized transaction as-is; the caller must compare it
against the parent it built, because this adapter deliberately keeps no memory
of the parent it submitted.

Credentials never leave this module: the TLS certificate and the dedicated
``channelescrow`` macaroon are held privately, the macaroon is attached per call
and only to calls addressed to the escrow service, the channel is always
TLS-authenticated with no insecure fallback, and neither the repr of the client
nor any raised error carries credentials, channel records or backend error
detail. gRPC failures are reported as a status code only; cancellation of the
surrounding task is propagated untouched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import IntEnum
from types import TracebackType
from typing import Any, Final

import grpc
from bitcointx.core.key import CPubKey

from jmswap.buyout_messages import MAX_MONEY, Outpoint, Prevout
from jmswap.lndrpc import channelescrow_pb2 as pb
from jmswap.lndrpc import channelescrow_pb2_grpc as pb_grpc

SERVICE_NAME: Final = "channelescrowrpc.ChannelEscrow"
"""Fully qualified gRPC service the macaroon is scoped to."""

SESSION_ID_LENGTH: Final = 32
ATTEMPT_ID_LENGTH: Final = 32
PUBLIC_NONCE_LENGTH: Final = 66
PARTIAL_SIGNATURE_LENGTH: Final = 32
TXID_LENGTH: Final = 32

_COMPRESSED_PUBKEY_LENGTH: Final = 33
_P2TR_SCRIPT_LENGTH: Final = 34


class LndEscrowError(Exception):
    """Base error of the ChannelEscrow adapter."""


class LndEscrowRpcError(LndEscrowError):
    """A ChannelEscrow RPC failed.

    Only the gRPC status code is reported: backend error strings can quote
    channel state and are never surfaced or chained.
    """

    def __init__(self, method: str, code: grpc.StatusCode | None) -> None:
        self.method = method
        self.code = code
        name = code.name if code is not None else "UNKNOWN"
        super().__init__(f"ChannelEscrow {method} failed with status {name}")


class LndEscrowResponseError(LndEscrowError):
    """The backend answered with data that is malformed or self-contradictory."""


class EscrowStage(IntEnum):
    """Durable escrow stage reported by the backend (``channeldb.EscrowStage``)."""

    LOCKED = 0
    PARENT_PREPARED = 1
    PARENT_SIGNED = 2
    FINALIZED = 3


def _response_error(message: str) -> LndEscrowResponseError:
    return LndEscrowResponseError(f"ChannelEscrow {message}")


def _require_response_bytes(value: bytes, length: int, name: str) -> bytes:
    raw = bytes(value)
    if len(raw) != length:
        raise _response_error(f"{name} must be exactly {length} bytes")
    return raw


def _require_response_pubkey(value: bytes, name: str) -> bytes:
    raw = _require_response_bytes(value, _COMPRESSED_PUBKEY_LENGTH, name)
    if raw[0] not in (0x02, 0x03) or not CPubKey(raw).is_fullyvalid():
        raise _response_error(f"{name} is not a valid compressed public key")
    return raw


def _require_public_nonce(value: bytes, name: str, error: type[Exception]) -> bytes:
    raw = bytes(value)
    if len(raw) != PUBLIC_NONCE_LENGTH:
        raise error(f"{name} must be exactly {PUBLIC_NONCE_LENGTH} bytes")
    for offset in (0, _COMPRESSED_PUBKEY_LENGTH):
        point = raw[offset : offset + _COMPRESSED_PUBKEY_LENGTH]
        if point[0] not in (0x02, 0x03) or not CPubKey(point).is_fullyvalid():
            raise error(f"{name} is not two valid compressed public keys")
    return raw


def _require_response_amount(value: int, name: str) -> int:
    amount = int(value)
    if not 0 <= amount <= MAX_MONEY:
        raise _response_error(f"{name} is not a satoshi amount")
    return amount


def _require_argument_bytes(value: bytes, length: int, name: str) -> bytes:
    if type(value) is not bytes or len(value) != length:
        raise ValueError(f"{name} must be exactly {length} bytes")
    return value


def _channel_point(point: Outpoint) -> pb.ChannelPoint:
    """Convert a display-order outpoint into LND's internal-order channel point.

    ``ChannelPoint.funding_txid`` carries the raw ``chainhash.Hash``, which is
    the byte reversal of the txid as it is displayed and as the buyout wire
    models carry it.
    """
    if not isinstance(point, Outpoint):
        raise ValueError("channel point must be an Outpoint")
    return pb.ChannelPoint(funding_txid=bytes.fromhex(point.txid)[::-1], output_index=point.vout)


def _display_txid(raw: bytes, name: str) -> str:
    """Convert an internal-order hash from the backend into a display txid."""
    return _require_response_bytes(raw, TXID_LENGTH, name)[::-1].hex()


def _session_id(session_id: bytes) -> bytes:
    return _require_argument_bytes(session_id, SESSION_ID_LENGTH, "session id")


@dataclass(frozen=True)
class FrozenChannel:
    """The quiesced channel a freeze locked to one session.

    The claims are the cooperative-close balances the backend computed for the
    snapshot and must account for the capacity exactly. A snapshot that leaves
    satoshis unassigned (a millisatoshi residual rounded away, or a capacity the
    backend did not fully allocate) is rejected rather than silently credited to
    one side: this adapter never decides who owns an unassigned satoshi. The
    funding script is the P2TR output the parent has to spend, and the two
    funding keys must be distinct even after dropping their parity, since the
    MuSig2 funding key and the Taproot output key only see the x-only form.
    """

    point: Outpoint
    capacity_sat: int
    local_claim_sat: int
    remote_claim_sat: int
    peer_pubkey: bytes
    funding_script: bytes
    local_funding_pubkey: bytes
    remote_funding_pubkey: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.point, Outpoint):
            raise _response_error("frozen channel point is not an outpoint")
        capacity = _require_response_amount(self.capacity_sat, "capacity")
        if capacity <= 0:
            raise _response_error("capacity must be positive")
        local = _require_response_amount(self.local_claim_sat, "local claim")
        remote = _require_response_amount(self.remote_claim_sat, "remote claim")
        if local + remote != capacity:
            raise _response_error("claims do not account for the channel capacity exactly")
        script = _require_response_bytes(self.funding_script, _P2TR_SCRIPT_LENGTH, "funding script")
        if script[0] != 0x51 or script[1] != 0x20:
            raise _response_error("funding script is not a native P2TR script")
        _require_response_pubkey(self.peer_pubkey, "peer pubkey")
        local_funding = _require_response_pubkey(self.local_funding_pubkey, "local funding pubkey")
        remote_funding = _require_response_pubkey(
            self.remote_funding_pubkey, "remote funding pubkey"
        )
        if local_funding[1:] == remote_funding[1:]:
            raise _response_error("funding keys are equal in their x-only form")


@dataclass(frozen=True)
class SigningAttempt:
    """One MuSig2 signing attempt of the frozen channel input."""

    attempt_id: bytes
    public_nonce: bytes

    def __post_init__(self) -> None:
        _require_response_bytes(self.attempt_id, ATTEMPT_ID_LENGTH, "attempt id")
        _require_public_nonce(self.public_nonce, "public nonce", LndEscrowResponseError)


@dataclass(frozen=True)
class EscrowStatus:
    """The durable escrow record, plus the in-memory attempt if one is live.

    ``parent_txid`` is absent exactly while no parent has been prepared: the
    backend omits it below :attr:`EscrowStage.PARENT_PREPARED` and always
    reports it from that stage on. The durable nonces and partial signature are
    what survives a backend restart, and are what allows a signed attempt to be
    finalized without repeating it.
    """

    stage: EscrowStage
    parent_txid: str | None
    finalized: bool
    durable_local_nonce: bytes | None
    durable_remote_nonce: bytes | None
    durable_local_partial: bytes | None
    finalized_parent: bytes | None
    active_attempt: SigningAttempt | None

    def __post_init__(self) -> None:
        self._check_parent()
        self._check_durable()

    def _check_parent(self) -> None:
        prepared = self.stage >= EscrowStage.PARENT_PREPARED
        if prepared and self.parent_txid is None:
            raise _response_error("status omits the parent txid of a prepared escrow")
        if not prepared and self.parent_txid is not None:
            raise _response_error("status reports a parent txid before the parent was prepared")
        if self.finalized != (self.stage is EscrowStage.FINALIZED):
            raise _response_error("status finalized flag contradicts the reported stage")
        if self.finalized and not self.finalized_parent:
            raise _response_error("status reports a finalized escrow without its parent")

    def _check_durable(self) -> None:
        for nonce, name in (
            (self.durable_local_nonce, "durable local nonce"),
            (self.durable_remote_nonce, "durable remote nonce"),
        ):
            if nonce is not None:
                _require_public_nonce(nonce, name, LndEscrowResponseError)
        if self.durable_local_partial is not None:
            _require_response_bytes(
                self.durable_local_partial, PARTIAL_SIGNATURE_LENGTH, "durable partial signature"
            )
        signed_fields = (
            self.durable_local_nonce,
            self.durable_remote_nonce,
            self.durable_local_partial,
        )
        if self.stage is EscrowStage.PARENT_SIGNED and any(part is None for part in signed_fields):
            raise _response_error("status reports a signed escrow without its durable signature")


class _EscrowMacaroon(grpc.AuthMetadataPlugin):
    """Attaches the escrow macaroon to escrow calls, and to nothing else."""

    def __init__(self, macaroon: bytes) -> None:
        self._metadata: tuple[tuple[str, str], ...] = (("macaroon", macaroon.hex()),)

    def __call__(
        self, context: grpc.AuthMetadataContext, callback: grpc.AuthMetadataPluginCallback
    ) -> None:
        service_url = str(context.service_url)
        if not service_url.endswith(f"/{SERVICE_NAME}"):
            callback((), ValueError("escrow macaroon is scoped to the ChannelEscrow service"))
            return
        callback(self._metadata, None)

    def __repr__(self) -> str:
        return "<escrow macaroon credentials>"


class LndEscrowClient:
    """Async client for one LND node's ``ChannelEscrow`` service.

    Use it as an async context manager; the gRPC channel exists only inside the
    context. Every call carries the configured deadline, so a backend that
    blocks on a peer (a freeze waits for quiescence, signing waits on the link)
    fails instead of hanging.
    """

    def __init__(
        self,
        endpoint: str,
        tls_certificate: bytes,
        macaroon: bytes,
        timeout: float = 30.0,
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
        self._call_credentials = grpc.metadata_call_credentials(_EscrowMacaroon(macaroon))
        self._timeout = float(timeout)
        self._channel: grpc.aio.Channel | None = None
        self._stub: Any = None

    def __repr__(self) -> str:
        return f"LndEscrowClient(endpoint={self._endpoint!r}, timeout={self._timeout!r})"

    async def __aenter__(self) -> LndEscrowClient:
        if self._channel is None:
            self._channel = grpc.aio.secure_channel(self._endpoint, self._channel_credentials)
            self._stub = pb_grpc.ChannelEscrowStub(self._channel)  # type: ignore[no-untyped-call]
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        channel, self._channel, self._stub = self._channel, None, None
        if channel is not None:
            await channel.close()

    async def _call(self, method: str, request: Any) -> Any:
        """Issue one escrow RPC under the client deadline with its own macaroon."""
        if self._stub is None:
            raise LndEscrowError("client is not connected; use it as an async context manager")
        try:
            return await getattr(self._stub, method)(
                request, timeout=self._timeout, credentials=self._call_credentials
            )
        except grpc.aio.AioRpcError as exc:
            # Chaining would re-expose the backend's error detail.
            raise LndEscrowRpcError(method, exc.code()) from None

    async def freeze(self, point: Outpoint, session_id: bytes) -> FrozenChannel:
        """Freeze the channel for this session and return its locked snapshot."""
        response = await self._call(
            "FreezeChannel",
            pb.FreezeChannelRequest(
                channel_point=_channel_point(point), session_id=_session_id(session_id)
            ),
        )
        if not response.HasField("funding_outpoint"):
            raise _response_error("freeze response omits the funding outpoint")
        frozen = Outpoint(
            txid=_display_txid(response.funding_outpoint.funding_txid, "funding txid"),
            vout=int(response.funding_outpoint.output_index),
        )
        if frozen != point:
            raise _response_error("freeze response is for a different channel")
        return FrozenChannel(
            point=frozen,
            capacity_sat=int(response.capacity_sat),
            local_claim_sat=int(response.local_claim_sat),
            remote_claim_sat=int(response.remote_claim_sat),
            peer_pubkey=bytes(response.peer_pubkey),
            funding_script=bytes(response.funding_script),
            local_funding_pubkey=bytes(response.local_funding_pubkey),
            remote_funding_pubkey=bytes(response.remote_funding_pubkey),
        )

    async def prepare(
        self,
        point: Outpoint,
        session_id: bytes,
        parent: bytes,
        prevouts: Sequence[Prevout],
        input_index: int,
    ) -> str:
        """Submit the sole parent that may spend the frozen funding output.

        ``prevouts`` are the spent outputs of every parent input, in input
        order, and ``input_index`` is the parent input that spends the channel.
        The returned display-order txid is the backend's; the caller is
        responsible for checking it against the parent it built.
        """
        request = pb.PrepareParentRequest(
            channel_point=_channel_point(point),
            session_id=_session_id(session_id),
            raw_parent_tx=_parent_bytes(parent),
            prev_outs=_prevouts(prevouts),
            channel_input_index=_input_index(input_index, len(prevouts)),
        )
        response = await self._call("PrepareParent", request)
        return _display_txid(response.txid, "prepared parent txid")

    async def begin(
        self, point: Outpoint, session_id: bytes, restart_attempt: bool = False
    ) -> SigningAttempt:
        """Start (or resume, unless ``restart_attempt``) the signing attempt."""
        response = await self._call(
            "BeginSigning",
            pb.BeginSigningRequest(
                channel_point=_channel_point(point),
                session_id=_session_id(session_id),
                restart_attempt=bool(restart_attempt),
            ),
        )
        return SigningAttempt(
            attempt_id=bytes(response.attempt_id),
            public_nonce=bytes(response.local_public_nonce),
        )

    async def sign(
        self,
        point: Outpoint,
        session_id: bytes,
        attempt: SigningAttempt,
        peer_nonce: bytes,
    ) -> bytes:
        """Return the backend's partial signature over the prepared parent."""
        response = await self._call(
            "SignParent",
            pb.SignParentRequest(
                channel_point=_channel_point(point),
                session_id=_session_id(session_id),
                attempt_id=_attempt(attempt).attempt_id,
                remote_public_nonce=_require_public_nonce(peer_nonce, "peer nonce", ValueError),
            ),
        )
        return _require_response_bytes(
            response.local_partial_signature, PARTIAL_SIGNATURE_LENGTH, "partial signature"
        )

    async def finalize(
        self,
        point: Outpoint,
        session_id: bytes,
        attempt: SigningAttempt,
        peer_partial: bytes,
    ) -> bytes:
        """Combine the partials and return the serialized signed parent.

        The bytes are returned exactly as the backend produced them. Whether
        they are the agreed parent is the caller's check.
        """
        response = await self._call(
            "FinalizeParent",
            pb.FinalizeParentRequest(
                channel_point=_channel_point(point),
                session_id=_session_id(session_id),
                attempt_id=_attempt(attempt).attempt_id,
                remote_partial_signature=_require_argument_bytes(
                    peer_partial, PARTIAL_SIGNATURE_LENGTH, "peer partial signature"
                ),
            ),
        )
        _display_txid(response.txid, "finalized parent txid")
        raw = bytes(response.raw_tx)
        if not raw:
            raise _response_error("finalize response omits the signed parent")
        return raw

    async def status(self, point: Outpoint, session_id: bytes) -> EscrowStatus:
        """Report the durable escrow record and any live signing attempt."""
        response = await self._call(
            "Status",
            pb.StatusRequest(
                channel_point=_channel_point(point), session_id=_session_id(session_id)
            ),
        )
        return _escrow_status(response)

    async def cancel(self, point: Outpoint, session_id: bytes) -> bool:
        """Release the freeze; ``True`` if the channel link was resumed."""
        response = await self._call(
            "Cancel",
            pb.CancelRequest(
                channel_point=_channel_point(point), session_id=_session_id(session_id)
            ),
        )
        return bool(response.link_resumed)


def _parent_bytes(parent: bytes) -> bytes:
    if type(parent) is not bytes or not parent:
        raise ValueError("parent must be a non-empty serialized transaction")
    return parent


def _prevouts(prevouts: Sequence[Prevout]) -> list[pb.PrevOut]:
    if isinstance(prevouts, str | bytes) or not isinstance(prevouts, Sequence) or not prevouts:
        raise ValueError("prevouts must be a non-empty sequence of Prevout")
    if any(not isinstance(prevout, Prevout) for prevout in prevouts):
        raise ValueError("every prevout must be a Prevout")
    return [
        pb.PrevOut(value_sat=prevout.value, pk_script=bytes.fromhex(prevout.script_pubkey))
        for prevout in prevouts
    ]


def _input_index(input_index: int, prevout_count: int) -> int:
    if (
        isinstance(input_index, bool)
        or type(input_index) is not int
        or not 0 <= input_index < prevout_count
    ):
        raise ValueError("input_index must select one of the parent inputs")
    return input_index


def _attempt(attempt: SigningAttempt) -> SigningAttempt:
    if not isinstance(attempt, SigningAttempt):
        raise ValueError("attempt must be a SigningAttempt")
    return attempt


def _optional_bytes(value: bytes) -> bytes | None:
    raw = bytes(value)
    return raw if raw else None


def _escrow_status(response: Any) -> EscrowStatus:
    try:
        stage = EscrowStage(int(response.stage))
    except ValueError as exc:
        raise _response_error("status reports an unknown stage") from exc
    parent_txid = bytes(response.parent_txid)
    active_id = _optional_bytes(response.active_attempt_id)
    active_nonce = _optional_bytes(response.active_local_public_nonce)
    if (active_id is None) != (active_nonce is None):
        raise _response_error("status reports half of an active signing attempt")
    return EscrowStatus(
        stage=stage,
        parent_txid=_display_txid(parent_txid, "parent txid") if parent_txid else None,
        finalized=bool(response.finalized),
        durable_local_nonce=_optional_bytes(response.durable_local_public_nonce),
        durable_remote_nonce=_optional_bytes(response.durable_remote_public_nonce),
        durable_local_partial=_optional_bytes(response.durable_local_partial_signature),
        finalized_parent=_optional_bytes(response.finalized_parent_tx),
        active_attempt=(
            SigningAttempt(attempt_id=active_id, public_nonce=active_nonce)
            if active_id is not None and active_nonce is not None
            else None
        ),
    )
