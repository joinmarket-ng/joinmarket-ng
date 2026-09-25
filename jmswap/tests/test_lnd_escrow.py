"""Transport-level tests for the LND ChannelEscrow adapter.

Everything here pins the adapter contract only: request mapping including byte
order, structural validation of responses, and how transport failures surface.
The gRPC stub is replaced by a recorder, so no backend is involved and no test
depends on escrow protocol state.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import grpc
import pytest
from bitcointx.core.key import CKey

from jmswap.buyout_messages import Outpoint, Prevout
from jmswap.lnd_escrow import (
    PARTIAL_SIGNATURE_LENGTH,
    PUBLIC_NONCE_LENGTH,
    SERVICE_NAME,
    SESSION_ID_LENGTH,
    EscrowStage,
    EscrowStatus,
    FrozenChannel,
    LndEscrowClient,
    LndEscrowError,
    LndEscrowResponseError,
    LndEscrowRpcError,
    SigningAttempt,
    _EscrowMacaroon,
)
from jmswap.lndrpc import channelescrow_pb2 as pb

TLS_CERT = b"-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"
MACAROON = bytes(range(64))
SESSION_ID = bytes([0x11]) * SESSION_ID_LENGTH
ATTEMPT_ID = bytes([0x22]) * 32
PARTIAL = bytes([0x33]) * PARTIAL_SIGNATURE_LENGTH

# A display-order txid and the internal little-endian hash LND carries. Both
# txids here are deliberately not palindromic, so a missing reversal shows up.
TXID = "0f1e2d3c" * 8
TXID_INTERNAL = bytes.fromhex(TXID)[::-1]
PARENT_TXID = "0123456789abcdef" * 4
PARENT_TXID_INTERNAL = bytes.fromhex(PARENT_TXID)[::-1]

POINT = Outpoint(txid=TXID, vout=3)
FUNDING_SCRIPT = bytes.fromhex("5120" + "cd" * 32)


def _pubkey(secret: int) -> bytes:
    return bytes(CKey.from_secret_bytes(bytes([secret]) * 32).pub)


PEER_PUBKEY = _pubkey(1)
LOCAL_FUNDING_PUBKEY = _pubkey(2)
REMOTE_FUNDING_PUBKEY = _pubkey(3)
# The same point with the opposite parity: a different compressed key, but the
# same x-only key the MuSig2 funding key and the Taproot output key see.
NEGATED_LOCAL_FUNDING_PUBKEY = bytes([LOCAL_FUNDING_PUBKEY[0] ^ 0x01]) + LOCAL_FUNDING_PUBKEY[1:]
LOCAL_NONCE = _pubkey(4) + _pubkey(5)
REMOTE_NONCE = _pubkey(6) + _pubkey(7)


def freeze_response(**overrides: Any) -> pb.FreezeChannelResponse:
    fields: dict[str, Any] = {
        "capacity_sat": 1_000_000,
        "local_claim_sat": 600_000,
        "remote_claim_sat": 400_000,
        "peer_pubkey": PEER_PUBKEY,
        "funding_outpoint": pb.ChannelPoint(funding_txid=TXID_INTERNAL, output_index=3),
        "funding_script": FUNDING_SCRIPT,
        "local_funding_pubkey": LOCAL_FUNDING_PUBKEY,
        "remote_funding_pubkey": REMOTE_FUNDING_PUBKEY,
    }
    fields.update(overrides)
    return pb.FreezeChannelResponse(**fields)


def status_response(**overrides: Any) -> pb.StatusResponse:
    fields: dict[str, Any] = {
        "stage": int(EscrowStage.PARENT_PREPARED),
        "parent_txid": PARENT_TXID_INTERNAL,
        "finalized": False,
    }
    fields.update(overrides)
    return pb.StatusResponse(**fields)


class RecordingStub:
    """Minimal stand-in for ``ChannelEscrowStub`` that records every call."""

    def __init__(self, **results: Any) -> None:
        self.results = results
        self.calls: list[SimpleNamespace] = []

    def __getattr__(self, method: str) -> Any:
        if method.startswith("_"):
            raise AttributeError(method)

        async def call(request: Any, *, timeout: float, credentials: Any) -> Any:
            self.calls.append(
                SimpleNamespace(
                    method=method, request=request, timeout=timeout, credentials=credentials
                )
            )
            result = self.results[method]
            if isinstance(result, BaseException):
                raise result
            return result

        return call

    @property
    def last(self) -> SimpleNamespace:
        return self.calls[-1]


@asynccontextmanager
async def escrow_client(stub: RecordingStub, timeout: float = 30.0) -> AsyncIterator[Any]:
    """Enter a client whose transport is the recording stub."""
    client = LndEscrowClient(
        endpoint="127.0.0.1:10009",
        tls_certificate=TLS_CERT,
        macaroon=MACAROON,
        timeout=timeout,
    )
    async with client:
        client._stub = stub
        yield client


def rpc_error(code: grpc.StatusCode, details: str) -> grpc.aio.AioRpcError:
    return grpc.aio.AioRpcError(
        code, grpc.aio.Metadata(), grpc.aio.Metadata(), details=details, debug_error_string=details
    )


class TestFreeze:
    async def test_maps_the_locked_snapshot(self) -> None:
        stub = RecordingStub(FreezeChannel=freeze_response())
        async with escrow_client(stub) as client:
            frozen = await client.freeze(POINT, SESSION_ID)
        assert frozen == FrozenChannel(
            point=POINT,
            capacity_sat=1_000_000,
            local_claim_sat=600_000,
            remote_claim_sat=400_000,
            peer_pubkey=PEER_PUBKEY,
            funding_script=FUNDING_SCRIPT,
            local_funding_pubkey=LOCAL_FUNDING_PUBKEY,
            remote_funding_pubkey=REMOTE_FUNDING_PUBKEY,
        )

    async def test_channel_point_uses_internal_byte_order(self) -> None:
        stub = RecordingStub(FreezeChannel=freeze_response())
        async with escrow_client(stub) as client:
            await client.freeze(POINT, SESSION_ID)
        request = stub.last.request
        assert request.channel_point.funding_txid == TXID_INTERNAL
        assert request.channel_point.funding_txid != bytes.fromhex(TXID)
        assert request.channel_point.output_index == 3
        assert request.session_id == SESSION_ID

    async def test_rejects_a_snapshot_of_another_channel(self) -> None:
        other = pb.ChannelPoint(funding_txid=TXID_INTERNAL, output_index=4)
        stub = RecordingStub(FreezeChannel=freeze_response(funding_outpoint=other))
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match="different channel"):
                await client.freeze(POINT, SESSION_ID)

    async def test_rejects_a_snapshot_without_a_funding_outpoint(self) -> None:
        response = freeze_response()
        response.ClearField("funding_outpoint")
        stub = RecordingStub(FreezeChannel=response)
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match="omits the funding outpoint"):
                await client.freeze(POINT, SESSION_ID)

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"funding_script": bytes.fromhex("0020" + "cd" * 32)}, "P2TR"),
            ({"funding_script": b""}, "exactly 34 bytes"),
            ({"peer_pubkey": bytes.fromhex("02" + "ff" * 32)}, "valid compressed public key"),
            ({"local_funding_pubkey": b"\x05" + PEER_PUBKEY[1:]}, "valid compressed public key"),
            ({"remote_funding_pubkey": PEER_PUBKEY[:32]}, "exactly 33 bytes"),
            ({"capacity_sat": 0}, "capacity must be positive"),
            ({"capacity_sat": -1}, "not a satoshi amount"),
            ({"remote_claim_sat": -5}, "not a satoshi amount"),
            (
                {"local_funding_pubkey": REMOTE_FUNDING_PUBKEY},
                "funding keys are equal in their x-only form",
            ),
            (
                {"remote_funding_pubkey": NEGATED_LOCAL_FUNDING_PUBKEY},
                "funding keys are equal in their x-only form",
            ),
        ],
    )
    async def test_rejects_malformed_snapshots(self, overrides: Any, message: str) -> None:
        stub = RecordingStub(FreezeChannel=freeze_response(**overrides))
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match=message):
                await client.freeze(POINT, SESSION_ID)

    async def test_accepts_claims_that_account_for_the_capacity_exactly(self) -> None:
        stub = RecordingStub(
            FreezeChannel=freeze_response(
                capacity_sat=1_000_000, local_claim_sat=1_000_000, remote_claim_sat=0
            )
        )
        async with escrow_client(stub) as client:
            frozen = await client.freeze(POINT, SESSION_ID)
        assert frozen.local_claim_sat + frozen.remote_claim_sat == frozen.capacity_sat

    @pytest.mark.parametrize(
        ("local", "remote"),
        [
            # One satoshi short: a millisatoshi residual the backend rounded
            # away, which this adapter refuses to award to either side.
            (600_000, 399_999),
            (0, 0),
            # One satoshi too many, in either direction.
            (600_001, 400_000),
            (600_000, 400_001),
            (1_000_000, 1_000_000),
        ],
    )
    async def test_rejects_claims_that_do_not_account_for_the_capacity(
        self, local: int, remote: int
    ) -> None:
        stub = RecordingStub(
            FreezeChannel=freeze_response(
                capacity_sat=1_000_000, local_claim_sat=local, remote_claim_sat=remote
            )
        )
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match="account for the channel capacity"):
                await client.freeze(POINT, SESSION_ID)


class TestPrepare:
    prevouts = [
        Prevout(value=1_000_000, script_pubkey="5120" + "cd" * 32),
        Prevout(value=250_000, script_pubkey="5120" + "ef" * 32),
    ]

    async def test_maps_the_parent_and_returns_a_display_txid(self) -> None:
        stub = RecordingStub(PrepareParent=pb.PrepareParentResponse(txid=PARENT_TXID_INTERNAL))
        async with escrow_client(stub) as client:
            txid = await client.prepare(POINT, SESSION_ID, b"\x02raw", self.prevouts, 0)
        assert txid == PARENT_TXID
        request = stub.last.request
        assert request.raw_parent_tx == b"\x02raw"
        assert request.channel_input_index == 0
        assert [(out.value_sat, out.pk_script.hex()) for out in request.prev_outs] == [
            (1_000_000, "5120" + "cd" * 32),
            (250_000, "5120" + "ef" * 32),
        ]

    async def test_rejects_a_txid_of_the_wrong_length(self) -> None:
        stub = RecordingStub(PrepareParent=pb.PrepareParentResponse(txid=b"\x00" * 31))
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match="exactly 32 bytes"):
                await client.prepare(POINT, SESSION_ID, b"raw", self.prevouts, 1)

    @pytest.mark.parametrize(
        ("parent", "prevouts", "index"),
        [
            (b"", prevouts, 0),
            ("deadbeef", prevouts, 0),
            (b"raw", [], 0),
            (b"raw", prevouts[0], 0),
            (b"raw", [(1, b"")], 0),
            (b"raw", prevouts, 2),
            (b"raw", prevouts, -1),
            (b"raw", prevouts, True),
        ],
    )
    async def test_rejects_invalid_arguments(self, parent: Any, prevouts: Any, index: Any) -> None:
        stub = RecordingStub(PrepareParent=pb.PrepareParentResponse(txid=PARENT_TXID_INTERNAL))
        async with escrow_client(stub) as client:
            with pytest.raises(ValueError):
                await client.prepare(POINT, SESSION_ID, parent, prevouts, index)
        assert stub.calls == []


class TestSigning:
    async def test_begin_resumes_by_default(self) -> None:
        stub = RecordingStub(
            BeginSigning=pb.BeginSigningResponse(
                attempt_id=ATTEMPT_ID, local_public_nonce=LOCAL_NONCE
            )
        )
        async with escrow_client(stub) as client:
            attempt = await client.begin(POINT, SESSION_ID)
        assert attempt == SigningAttempt(attempt_id=ATTEMPT_ID, public_nonce=LOCAL_NONCE)
        assert stub.last.request.restart_attempt is False

    async def test_begin_forwards_a_restart(self) -> None:
        stub = RecordingStub(
            BeginSigning=pb.BeginSigningResponse(
                attempt_id=ATTEMPT_ID, local_public_nonce=LOCAL_NONCE
            )
        )
        async with escrow_client(stub) as client:
            await client.begin(POINT, SESSION_ID, restart_attempt=True)
        assert stub.last.request.restart_attempt is True

    @pytest.mark.parametrize(
        ("attempt_id", "nonce", "message"),
        [
            (ATTEMPT_ID[:31], LOCAL_NONCE, "attempt id must be exactly 32 bytes"),
            (ATTEMPT_ID, LOCAL_NONCE[:65], "exactly 66 bytes"),
            (ATTEMPT_ID, bytes(PUBLIC_NONCE_LENGTH), "two valid compressed public keys"),
        ],
    )
    async def test_begin_rejects_a_malformed_attempt(
        self, attempt_id: bytes, nonce: bytes, message: str
    ) -> None:
        stub = RecordingStub(
            BeginSigning=pb.BeginSigningResponse(attempt_id=attempt_id, local_public_nonce=nonce)
        )
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match=message):
                await client.begin(POINT, SESSION_ID)

    async def test_sign_forwards_the_attempt_and_returns_the_partial(self) -> None:
        stub = RecordingStub(
            SignParent=pb.SignParentResponse(local_partial_signature=PARTIAL),
        )
        attempt = SigningAttempt(attempt_id=ATTEMPT_ID, public_nonce=LOCAL_NONCE)
        async with escrow_client(stub) as client:
            partial = await client.sign(POINT, SESSION_ID, attempt, REMOTE_NONCE)
        assert partial == PARTIAL
        request = stub.last.request
        assert request.attempt_id == ATTEMPT_ID
        assert request.remote_public_nonce == REMOTE_NONCE

    async def test_sign_rejects_a_malformed_peer_nonce(self) -> None:
        stub = RecordingStub(SignParent=pb.SignParentResponse(local_partial_signature=PARTIAL))
        attempt = SigningAttempt(attempt_id=ATTEMPT_ID, public_nonce=LOCAL_NONCE)
        async with escrow_client(stub) as client:
            with pytest.raises(ValueError, match="peer nonce"):
                await client.sign(POINT, SESSION_ID, attempt, REMOTE_NONCE[:60])
        assert stub.calls == []

    async def test_sign_rejects_a_malformed_partial(self) -> None:
        stub = RecordingStub(SignParent=pb.SignParentResponse(local_partial_signature=PARTIAL[:20]))
        attempt = SigningAttempt(attempt_id=ATTEMPT_ID, public_nonce=LOCAL_NONCE)
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match="partial signature"):
                await client.sign(POINT, SESSION_ID, attempt, REMOTE_NONCE)

    async def test_sign_requires_a_signing_attempt(self) -> None:
        stub = RecordingStub(SignParent=pb.SignParentResponse(local_partial_signature=PARTIAL))
        async with escrow_client(stub) as client:
            with pytest.raises(ValueError, match="SigningAttempt"):
                await client.sign(POINT, SESSION_ID, ATTEMPT_ID, REMOTE_NONCE)


class TestFinalize:
    attempt = SigningAttempt(attempt_id=ATTEMPT_ID, public_nonce=LOCAL_NONCE)

    async def test_returns_the_signed_parent_unchanged(self) -> None:
        stub = RecordingStub(
            FinalizeParent=pb.FinalizeParentResponse(
                raw_tx=b"\x02signed", txid=PARENT_TXID_INTERNAL
            )
        )
        async with escrow_client(stub) as client:
            signed = await client.finalize(POINT, SESSION_ID, self.attempt, PARTIAL)
        assert signed == b"\x02signed"
        request = stub.last.request
        assert request.attempt_id == ATTEMPT_ID
        assert request.remote_partial_signature == PARTIAL

    async def test_rejects_a_response_without_the_parent(self) -> None:
        stub = RecordingStub(
            FinalizeParent=pb.FinalizeParentResponse(raw_tx=b"", txid=PARENT_TXID_INTERNAL)
        )
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match="omits the signed parent"):
                await client.finalize(POINT, SESSION_ID, self.attempt, PARTIAL)

    async def test_rejects_a_response_with_a_malformed_txid(self) -> None:
        stub = RecordingStub(
            FinalizeParent=pb.FinalizeParentResponse(raw_tx=b"\x02signed", txid=b"\x00" * 16)
        )
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match="finalized parent txid"):
                await client.finalize(POINT, SESSION_ID, self.attempt, PARTIAL)

    async def test_rejects_a_malformed_peer_partial(self) -> None:
        stub = RecordingStub(
            FinalizeParent=pb.FinalizeParentResponse(
                raw_tx=b"\x02signed", txid=PARENT_TXID_INTERNAL
            )
        )
        async with escrow_client(stub) as client:
            with pytest.raises(ValueError, match="peer partial signature"):
                await client.finalize(POINT, SESSION_ID, self.attempt, PARTIAL + b"\x00")
        assert stub.calls == []


class TestStatus:
    async def test_maps_a_prepared_escrow(self) -> None:
        stub = RecordingStub(Status=status_response())
        async with escrow_client(stub) as client:
            status = await client.status(POINT, SESSION_ID)
        assert status == EscrowStatus(
            stage=EscrowStage.PARENT_PREPARED,
            parent_txid=PARENT_TXID,
            finalized=False,
            durable_local_nonce=None,
            durable_remote_nonce=None,
            durable_local_partial=None,
            finalized_parent=None,
            active_attempt=None,
        )

    async def test_maps_a_locked_escrow_without_a_parent(self) -> None:
        stub = RecordingStub(Status=status_response(stage=int(EscrowStage.LOCKED), parent_txid=b""))
        async with escrow_client(stub) as client:
            status = await client.status(POINT, SESSION_ID)
        assert status.stage is EscrowStage.LOCKED
        assert status.parent_txid is None

    async def test_maps_durable_signing_state_and_the_active_attempt(self) -> None:
        stub = RecordingStub(
            Status=status_response(
                stage=int(EscrowStage.PARENT_SIGNED),
                durable_local_public_nonce=LOCAL_NONCE,
                durable_remote_public_nonce=REMOTE_NONCE,
                durable_local_partial_signature=PARTIAL,
                active_attempt_id=ATTEMPT_ID,
                active_local_public_nonce=LOCAL_NONCE,
            )
        )
        async with escrow_client(stub) as client:
            status = await client.status(POINT, SESSION_ID)
        assert status.stage is EscrowStage.PARENT_SIGNED
        assert status.durable_local_nonce == LOCAL_NONCE
        assert status.durable_remote_nonce == REMOTE_NONCE
        assert status.durable_local_partial == PARTIAL
        assert status.active_attempt == SigningAttempt(
            attempt_id=ATTEMPT_ID, public_nonce=LOCAL_NONCE
        )

    async def test_maps_a_finalized_escrow(self) -> None:
        stub = RecordingStub(
            Status=status_response(
                stage=int(EscrowStage.FINALIZED),
                finalized=True,
                durable_local_public_nonce=LOCAL_NONCE,
                durable_remote_public_nonce=REMOTE_NONCE,
                durable_local_partial_signature=PARTIAL,
                finalized_parent_tx=b"\x02signed",
            )
        )
        async with escrow_client(stub) as client:
            status = await client.status(POINT, SESSION_ID)
        assert status.finalized is True
        assert status.finalized_parent == b"\x02signed"

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"stage": 4}, "unknown stage"),
            ({"stage": int(EscrowStage.LOCKED)}, "before the parent was prepared"),
            (
                {"stage": int(EscrowStage.PARENT_PREPARED), "parent_txid": b""},
                "omits the parent txid",
            ),
            ({"finalized": True}, "contradicts the reported stage"),
            (
                {
                    "stage": int(EscrowStage.FINALIZED),
                    "finalized": False,
                    "durable_local_public_nonce": LOCAL_NONCE,
                    "durable_remote_public_nonce": REMOTE_NONCE,
                    "durable_local_partial_signature": PARTIAL,
                },
                "contradicts the reported stage",
            ),
            (
                {
                    "stage": int(EscrowStage.FINALIZED),
                    "finalized": True,
                    "durable_local_public_nonce": LOCAL_NONCE,
                    "durable_remote_public_nonce": REMOTE_NONCE,
                    "durable_local_partial_signature": PARTIAL,
                },
                "finalized escrow without its parent",
            ),
            (
                {
                    "stage": int(EscrowStage.PARENT_SIGNED),
                    "durable_local_public_nonce": LOCAL_NONCE,
                },
                "signed escrow without its durable signature",
            ),
            ({"active_attempt_id": ATTEMPT_ID}, "half of an active signing attempt"),
            ({"active_local_public_nonce": LOCAL_NONCE}, "half of an active signing attempt"),
            ({"durable_local_public_nonce": LOCAL_NONCE[:10]}, "exactly 66 bytes"),
            ({"durable_local_partial_signature": PARTIAL[:8]}, "durable partial signature"),
            ({"parent_txid": b"\x01\x02"}, "parent txid must be exactly 32 bytes"),
        ],
    )
    async def test_rejects_contradictory_status(self, overrides: Any, message: str) -> None:
        stub = RecordingStub(Status=status_response(**overrides))
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowResponseError, match=message):
                await client.status(POINT, SESSION_ID)


class TestCancel:
    @pytest.mark.parametrize("resumed", [True, False])
    async def test_reports_whether_the_link_resumed(self, resumed: bool) -> None:
        stub = RecordingStub(Cancel=pb.CancelResponse(link_resumed=resumed))
        async with escrow_client(stub) as client:
            assert await client.cancel(POINT, SESSION_ID) is resumed
        assert stub.last.request.session_id == SESSION_ID


class TestTransport:
    async def test_every_call_carries_the_deadline_and_escrow_credentials(self) -> None:
        stub = RecordingStub(Cancel=pb.CancelResponse(link_resumed=True))
        async with escrow_client(stub, timeout=7.5) as client:
            await client.cancel(POINT, SESSION_ID)
        assert stub.last.timeout == 7.5
        assert isinstance(stub.last.credentials, grpc.CallCredentials)

    async def test_uses_a_tls_channel_and_never_an_insecure_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list[Any] = []
        real_secure_channel = grpc.aio.secure_channel

        def record_secure(target: str, credentials: Any, *args: Any, **kwargs: Any) -> Any:
            created.append((target, credentials))
            return real_secure_channel(target, credentials, *args, **kwargs)

        def forbid_insecure(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the escrow client must not open an insecure channel")

        monkeypatch.setattr(grpc.aio, "secure_channel", record_secure)
        monkeypatch.setattr(grpc.aio, "insecure_channel", forbid_insecure)
        async with escrow_client(RecordingStub()):
            pass
        assert len(created) == 1
        target, credentials = created[0]
        assert target == "127.0.0.1:10009"
        assert isinstance(credentials, grpc.ChannelCredentials)

    async def test_rpc_failures_report_the_status_code_only(self) -> None:
        failure = rpc_error(grpc.StatusCode.FAILED_PRECONDITION, "channel 0123:1 is not clean")
        stub = RecordingStub(FreezeChannel=failure)
        async with escrow_client(stub) as client:
            with pytest.raises(LndEscrowRpcError) as raised:
                await client.freeze(POINT, SESSION_ID)
        assert raised.value.code is grpc.StatusCode.FAILED_PRECONDITION
        assert raised.value.method == "FreezeChannel"
        assert "FAILED_PRECONDITION" in str(raised.value)
        assert "not clean" not in str(raised.value)
        # The gRPC error is neither chained nor reported: its detail string can
        # quote channel state.
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__ is True

    async def test_a_deadline_surfaces_as_its_status_code(self) -> None:
        stub = RecordingStub(Status=rpc_error(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline"))
        async with escrow_client(stub, timeout=0.5) as client:
            with pytest.raises(LndEscrowRpcError) as raised:
                await client.status(POINT, SESSION_ID)
        assert raised.value.code is grpc.StatusCode.DEADLINE_EXCEEDED

    async def test_cancellation_is_not_wrapped(self) -> None:
        stub = RecordingStub(Status=asyncio.CancelledError())
        async with escrow_client(stub) as client:
            with pytest.raises(asyncio.CancelledError):
                await client.status(POINT, SESSION_ID)

    async def test_calls_outside_the_context_are_refused(self) -> None:
        client = LndEscrowClient(
            endpoint="127.0.0.1:10009", tls_certificate=TLS_CERT, macaroon=MACAROON
        )
        with pytest.raises(LndEscrowError, match="not connected"):
            await client.status(POINT, SESSION_ID)
        async with client:
            pass
        with pytest.raises(LndEscrowError, match="not connected"):
            await client.status(POINT, SESSION_ID)

    @pytest.mark.parametrize(
        ("endpoint", "certificate", "macaroon", "timeout"),
        [
            ("", TLS_CERT, MACAROON, 30.0),
            (b"host:1", TLS_CERT, MACAROON, 30.0),
            ("host:1", b"", MACAROON, 30.0),
            ("host:1", "cert", MACAROON, 30.0),
            ("host:1", TLS_CERT, b"", 30.0),
            ("host:1", TLS_CERT, MACAROON.hex(), 30.0),
            ("host:1", TLS_CERT, MACAROON, 0),
            ("host:1", TLS_CERT, MACAROON, -1.0),
            ("host:1", TLS_CERT, MACAROON, True),
        ],
    )
    def test_rejects_invalid_configuration(
        self, endpoint: Any, certificate: Any, macaroon: Any, timeout: Any
    ) -> None:
        with pytest.raises(ValueError):
            LndEscrowClient(
                endpoint=endpoint,
                tls_certificate=certificate,
                macaroon=macaroon,
                timeout=timeout,
            )

    def test_repr_keeps_credentials_out(self) -> None:
        client = LndEscrowClient(
            endpoint="127.0.0.1:10009", tls_certificate=TLS_CERT, macaroon=MACAROON
        )
        text = repr(client)
        assert "127.0.0.1:10009" in text
        assert MACAROON.hex() not in text
        assert "CERTIFICATE" not in text


class RecordingCallback(grpc.AuthMetadataPluginCallback):
    """Captures what the macaroon plugin hands back to gRPC."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Exception | None]] = []

    def __call__(self, metadata: Any, error: Exception | None) -> None:
        self.calls.append((metadata, error))


class TestMacaroonScope:
    def context(self, service: str) -> Any:
        return SimpleNamespace(
            service_url=f"https://127.0.0.1:10009/{service}", method_name="Status"
        )

    def test_attaches_the_macaroon_to_escrow_calls(self) -> None:
        callback = RecordingCallback()
        _EscrowMacaroon(MACAROON)(self.context(SERVICE_NAME), callback)
        assert callback.calls == [((("macaroon", MACAROON.hex()),), None)]

    def test_refuses_to_sign_calls_of_another_service(self) -> None:
        callback = RecordingCallback()
        _EscrowMacaroon(MACAROON)(self.context("lnrpc.Lightning"), callback)
        (metadata, error) = callback.calls[0]
        assert metadata == ()
        assert isinstance(error, ValueError)

    def test_repr_keeps_the_macaroon_out(self) -> None:
        assert MACAROON.hex() not in repr(_EscrowMacaroon(MACAROON))


class TestSessionId:
    @pytest.mark.parametrize(
        "session_id", [b"", bytes(31), bytes(33), SESSION_ID.hex(), bytearray(SESSION_ID)]
    )
    async def test_every_call_requires_exactly_32_bytes(self, session_id: Any) -> None:
        stub = RecordingStub(Cancel=pb.CancelResponse(link_resumed=True))
        async with escrow_client(stub) as client:
            with pytest.raises(ValueError, match="session id"):
                await client.cancel(POINT, session_id)
        assert stub.calls == []

    async def test_requires_an_outpoint(self) -> None:
        stub = RecordingStub(Cancel=pb.CancelResponse(link_resumed=True))
        async with escrow_client(stub) as client:
            with pytest.raises(ValueError, match="Outpoint"):
                await client.cancel(f"{TXID}:3", SESSION_ID)
