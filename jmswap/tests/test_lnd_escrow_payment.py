"""Unit tests for the restart-safe LND escrow payment adapter."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from jmcore.bitcoin import TxInput, TxOutput, get_txid, serialize_transaction

from jmswap.channel_escrow import (
    ChannelInputEscrowError,
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowChainObservation,
    EscrowOutpoint,
    EscrowParentEvidence,
    EscrowParentPrevout,
    EscrowTimeoutEvidence,
    PaymentObservationRequest,
    PreparedChannelInputEscrow,
    SinglePartPaymentRequest,
)
from jmswap.errors import LightningError
from jmswap.lightning import PaymentStatus
from jmswap.lnd import LndBackend
from jmswap.lnd_escrow_payment import LndEscrowPaymentAdapter
from jmswap.lndrpc import lightning_pb2 as ln

_FUNDING_TXID = "12" * 32
_FUNDING_SCRIPT = "5120" + "13" * 32
_ESCROW_SCRIPT = "5120" + "66" * 32
_PARENT_TX_HEX = serialize_transaction(
    version=2,
    inputs=[TxInput.from_hex(_FUNDING_TXID, 0, sequence=0xFFFFFFFD)],
    outputs=[TxOutput.from_hex(_ESCROW_SCRIPT, 80_000)],
    locktime=0,
).hex()
_PARENT_TXID = get_txid(_PARENT_TX_HEX)
_PREIMAGE = bytes.fromhex("11" * 32)
_PAYMENT_HASH = hashlib.sha256(_PREIMAGE).hexdigest()


@dataclass
class _HeightSource:
    height: int = 950

    async def block_height(self) -> int:
        return self.height


class _Stream:
    def __init__(self, updates: list[ln.Payment]) -> None:
        self._updates = updates

    def __aiter__(self) -> AsyncIterator[ln.Payment]:
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[ln.Payment]:
        for update in self._updates:
            yield update


class _RpcError(Exception):
    def __init__(self, code_name: str) -> None:
        self._code_name = code_name

    def code(self) -> Any:
        return type("_Code", (), {"name": self._code_name})()


class _LightningStub:
    def __init__(self, channels: list[ln.Channel]) -> None:
        self.channels = channels
        self.requests: list[ln.ListChannelsRequest] = []

    async def ListChannels(self, request: ln.ListChannelsRequest) -> ln.ListChannelsResponse:  # noqa: N802
        self.requests.append(request)
        return ln.ListChannelsResponse(channels=self.channels)


class _RouterStub:
    def __init__(self) -> None:
        self.send_requests: list[Any] = []
        self.track_requests: list[Any] = []
        self.send_response: _Stream | Exception | None = None
        self.track_response: _Stream | Exception | None = None

    def SendPaymentV2(self, request: Any) -> _Stream:  # noqa: N802
        self.send_requests.append(request)
        if isinstance(self.send_response, Exception):
            raise self.send_response
        assert self.send_response is not None
        return self.send_response

    def TrackPaymentV2(self, request: Any) -> _Stream:  # noqa: N802
        self.track_requests.append(request)
        if isinstance(self.track_response, Exception):
            raise self.track_response
        assert self.track_response is not None
        return self.track_response


def _request(*, requested_at_unix: int = 1_900_000_000) -> SinglePartPaymentRequest:
    snapshot = ChannelSnapshot(
        session_id="session-1",
        channel_id="10",
        funding_outpoint=EscrowOutpoint(txid=_FUNDING_TXID, vout=0),
        funding_scriptpubkey=_FUNDING_SCRIPT,
        local_funding_pubkey="02" + "14" * 32,
        remote_funding_pubkey="03" + "15" * 32,
        capacity_sats=80_000,
        maker_claim_sats=70_000,
        peer_claim_sats=10_000,
        quiescent=True,
        active_htlcs=0,
    )
    terms = ChannelInputEscrowTerms(
        snapshot=snapshot,
        authorizer_pubkey="02" + "33" * 32,
        payment_hash=_PAYMENT_HASH,
        invoice="lnbcrt10m1escrowinvoice",
        invoice_payee_pubkey="03" + "44" * 32,
        invoice_payment_secret="55" * 32,
        invoice_amount_msat=10_000_000,
        invoice_expiry_unix=2_000_000_000,
        invoice_min_final_cltv_delta=10,
        invoice_verified=True,
        premium_sats=0,
        timeout_compensation_sats=0,
        escrow_value_sats=80_000,
        timeout_fee_sats=500,
        timeout_height=1_000,
        peer_settle_deadline=970,
        success_broadcast_deadline=980,
        parent_confirmation_depth=1,
        success_confirmation_depth=1,
        success_confirmation_budget_blocks=10,
        reorg_margin_blocks=5,
        safety_delta_blocks=2,
        preimage_budget_blocks=2,
    )
    prepared = PreparedChannelInputEscrow(
        terms=terms,
        parent_evidence=EscrowParentEvidence(
            parent_tx_hex=_PARENT_TX_HEX,
            parent_txid=_PARENT_TXID,
            channel_funding_outpoint=snapshot.funding_outpoint,
            channel_input_index=0,
            ordered_prevouts=(
                EscrowParentPrevout(
                    value_sats=snapshot.capacity_sats,
                    scriptpubkey=snapshot.funding_scriptpubkey,
                ),
            ),
            escrow_outpoint=EscrowOutpoint(txid=_PARENT_TXID, vout=0),
            escrow_value_sats=80_000,
            escrow_scriptpubkey=_ESCROW_SCRIPT,
            all_inputs_nonmalleable=True,
            exact_transaction_verified=True,
        ),
        timeout_evidence=EscrowTimeoutEvidence(
            timeout_tx_hex="020000",
            timeout_txid="77" * 32,
            spent_escrow_outpoint=EscrowOutpoint(txid=_PARENT_TXID, vout=0),
            locktime=1_000,
            maker_output_sats=69_500,
            peer_output_sats=10_000,
            fee_sats=500,
            fully_signed=True,
            signature_verified=True,
        ),
    )
    observation = EscrowChainObservation(
        session_id="session-1",
        parent_txid=_PARENT_TXID,
        parent_confirmations=1,
        in_best_chain=True,
        observed_height=950,
    )
    return SinglePartPaymentRequest(
        prepared=prepared,
        chain_observation=observation,
        excluded_channel_ids=("10",),
        requested_at_height=950,
        requested_at_unix=requested_at_unix,
    )


def _channels() -> list[ln.Channel]:
    return [
        ln.Channel(chan_id=100, channel_point="a" * 64 + ":0", active=True, alias_scids=[10]),
        ln.Channel(chan_id=200, channel_point="b" * 64 + ":1", active=True, alias_scids=[20]),
        ln.Channel(chan_id=300, channel_point="c" * 64 + ":2", active=False, alias_scids=[30]),
        ln.Channel(chan_id=0, channel_point="d" * 64 + ":3", active=True, alias_scids=[40]),
    ]


def _payment(
    status: int,
    *,
    channel_id: int = 200,
    downstream_channel_id: int | None = None,
    preimage: bytes = _PREIMAGE,
    attempts: list[ln.HTLCAttempt] | None = None,
) -> ln.Payment:
    if attempts is None:
        hops = [ln.Hop(chan_id=channel_id, expiry=975)]
        if downstream_channel_id is not None:
            hops.append(ln.Hop(chan_id=downstream_channel_id, expiry=973))
        route = ln.Route(total_time_lock=978, hops=hops)
        attempt_status = {
            ln.Payment.IN_FLIGHT: ln.HTLCAttempt.IN_FLIGHT,
            ln.Payment.SUCCEEDED: ln.HTLCAttempt.SUCCEEDED,
            ln.Payment.FAILED: ln.HTLCAttempt.FAILED,
        }[status]
        attempts = [
            ln.HTLCAttempt(attempt_id=1, status=attempt_status, route=route, preimage=preimage)
        ]
    return ln.Payment(
        payment_hash=_PAYMENT_HASH,
        payment_request="lnbcrt10m1escrowinvoice",
        value_msat=10_000_000,
        status=status,
        htlcs=attempts,
    )


def _adapter(
    tmp_path: Path,
    router: _RouterStub,
    height: _HeightSource | None = None,
    *,
    clock: Any | None = None,
) -> LndEscrowPaymentAdapter:
    backend = LndBackend(host="x", tls_cert=b"", macaroon_hex="")
    backend._stub = _LightningStub(_channels())
    backend._router_stub = router
    return LndEscrowPaymentAdapter(
        backend,
        height or _HeightSource(),
        tmp_path,
        fee_limit_msat=25_000,
        timeout_seconds=30,
        **({"clock": clock} if clock is not None else {}),
    )


async def test_send_freezes_allowed_channels_and_persists_pending_route(tmp_path: Path) -> None:
    router = _RouterStub()
    router.send_response = _Stream(
        [
            ln.Payment(payment_hash=_PAYMENT_HASH, status=ln.Payment.INITIATED),
            _payment(ln.Payment.IN_FLIGHT),
        ]
    )
    adapter = _adapter(tmp_path, router)

    result = await adapter.send_single_part_payment(_request())

    assert result.status is PaymentStatus.PENDING
    assert result.route is not None
    assert result.route.route_channel_ids == ("200",)
    rpc_request = router.send_requests[0]
    assert list(rpc_request.outgoing_chan_ids) == [200]
    assert rpc_request.payment_request == "lnbcrt10m1escrowinvoice"
    assert rpc_request.fee_limit_msat == 25_000
    assert rpc_request.timeout_seconds == 30
    assert rpc_request.max_parts == 1
    assert rpc_request.cltv_limit == 28
    assert not rpc_request.no_inflight_updates
    assert not rpc_request.allow_self_payment
    assert not rpc_request.amp
    assert not rpc_request.cancelable
    journal = next(tmp_path.glob("*.json"))
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


async def test_success_requires_matching_preimage_and_memoizes_terminal_result(
    tmp_path: Path,
) -> None:
    router = _RouterStub()
    router.send_response = _Stream([_payment(ln.Payment.SUCCEEDED)])
    adapter = _adapter(tmp_path, router)
    request = _request()

    result = await adapter.send_single_part_payment(request)
    repeated = await adapter.send_single_part_payment(request)

    assert result.status is PaymentStatus.SUCCEEDED
    assert result.preimage == _PREIMAGE.hex()
    assert result.settled_height == 950
    assert repeated == result
    assert len(router.send_requests) == 1
    assert not router.track_requests

    invalid_router = _RouterStub()
    invalid_router.send_response = _Stream([_payment(ln.Payment.SUCCEEDED, preimage=b"x" * 32)])
    with pytest.raises(ChannelInputEscrowError, match="preimage"):
        await _adapter(tmp_path / "invalid", invalid_router).send_single_part_payment(_request())


async def test_failed_payment_without_route_is_persisted(tmp_path: Path) -> None:
    router = _RouterStub()
    router.send_response = _Stream([_payment(ln.Payment.FAILED, attempts=[])])

    result = await _adapter(tmp_path, router).send_single_part_payment(_request())

    assert result.status is PaymentStatus.FAILED
    assert result.route is None


async def test_expired_pre_submission_request_is_persisted_as_failed(tmp_path: Path) -> None:
    router = _RouterStub()
    router.send_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])
    adapter = _adapter(tmp_path, router, clock=lambda: 2_000_000_000)

    result = await adapter.send_single_part_payment(_request())
    repeated = await adapter.send_single_part_payment(_request())

    assert result.status is PaymentStatus.FAILED
    assert repeated == result
    assert not router.send_requests
    assert not router.track_requests


async def test_retry_tracks_existing_journal_without_resending(tmp_path: Path) -> None:
    router = _RouterStub()
    router.send_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])
    adapter = _adapter(tmp_path, router)
    request = _request()
    await adapter.send_single_part_payment(request)

    height = _HeightSource(951)
    router.track_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])
    restarted = _adapter(tmp_path, router, height)
    result = await restarted.send_single_part_payment(request)

    assert result.status is PaymentStatus.PENDING
    assert result.observed_height == 951
    assert len(router.send_requests) == 1
    assert len(router.track_requests) == 1


async def test_retry_resubmits_not_found_payment_only_at_original_height(tmp_path: Path) -> None:
    router = _RouterStub()
    router.send_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])
    adapter = _adapter(tmp_path, router)
    request = _request()
    await adapter.send_single_part_payment(request)
    router.track_response = _RpcError("NOT_FOUND")
    router.send_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])

    result = await _adapter(tmp_path, router).send_single_part_payment(request)

    assert result.status is PaymentStatus.PENDING
    assert len(router.track_requests) == 1
    assert len(router.send_requests) == 2

    stale_dir = tmp_path / "stale"
    stale_router = _RouterStub()
    stale_router.send_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])
    await _adapter(stale_dir, stale_router).send_single_part_payment(request)
    stale_router.track_response = _RpcError("NOT_FOUND")

    stale_result = await _adapter(
        stale_dir, stale_router, _HeightSource(951)
    ).send_single_part_payment(request)

    assert stale_result.status is PaymentStatus.FAILED
    assert len(stale_router.track_requests) == 1
    assert len(stale_router.send_requests) == 1


async def test_duplicate_submission_tracks_the_existing_lnd_payment(tmp_path: Path) -> None:
    router = _RouterStub()
    router.send_response = _RpcError("ALREADY_EXISTS")
    router.track_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])

    result = await _adapter(tmp_path, router).send_single_part_payment(_request())

    assert result.status is PaymentStatus.PENDING
    assert len(router.send_requests) == 1
    assert len(router.track_requests) == 1

    missing_router = _RouterStub()
    missing_router.send_response = _RpcError("ALREADY_EXISTS")
    missing_router.track_response = _RpcError("NOT_FOUND")
    with pytest.raises(LightningError, match="duplicate submission"):
        await _adapter(tmp_path / "missing", missing_router).send_single_part_payment(_request())


async def test_conflicting_request_and_stale_first_send_fail_closed(tmp_path: Path) -> None:
    router = _RouterStub()
    router.send_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])
    adapter = _adapter(tmp_path, router)
    await adapter.send_single_part_payment(_request())

    with pytest.raises(ChannelInputEscrowError, match="conflicting"):
        await adapter.send_single_part_payment(_request(requested_at_unix=1_900_000_001))

    stale_router = _RouterStub()
    stale_router.send_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])
    stale = _adapter(tmp_path / "stale", stale_router, _HeightSource(951))
    stale_result = await stale.send_single_part_payment(_request())
    repeated = await stale.send_single_part_payment(_request())
    assert stale_result.status is PaymentStatus.FAILED
    assert stale_result.observed_height == 951
    assert repeated == stale_result
    assert not stale_router.send_requests
    assert not stale_router.track_requests


@pytest.mark.parametrize("channel_id", [100, 999])
async def test_rejects_excluded_or_unknown_route_channel(tmp_path: Path, channel_id: int) -> None:
    router = _RouterStub()
    router.send_response = _Stream([_payment(ln.Payment.IN_FLIGHT, channel_id=channel_id)])

    with pytest.raises(ChannelInputEscrowError, match="excluded or unknown"):
        await _adapter(tmp_path, router).send_single_part_payment(_request())


async def test_allows_unknown_downstream_channel_after_allowed_first_hop(tmp_path: Path) -> None:
    router = _RouterStub()
    router.send_response = _Stream(
        [_payment(ln.Payment.IN_FLIGHT, channel_id=200, downstream_channel_id=999)]
    )

    result = await _adapter(tmp_path, router).send_single_part_payment(_request())

    assert result.route is not None
    assert result.route.route_channel_ids == ("200", "999")


async def test_observation_validates_payment_id_and_tracks(tmp_path: Path) -> None:
    router = _RouterStub()
    router.send_response = _Stream([_payment(ln.Payment.IN_FLIGHT)])
    adapter = _adapter(tmp_path, router)
    await adapter.send_single_part_payment(_request())
    router.track_response = _Stream([_payment(ln.Payment.FAILED, attempts=[])])

    result = await adapter.observe_escrow_payment(
        PaymentObservationRequest(session_id="session-1", payment_id=_PAYMENT_HASH)
    )

    assert result.status is PaymentStatus.FAILED
    with pytest.raises(ChannelInputEscrowError, match="payment ID"):
        await adapter.observe_escrow_payment(
            PaymentObservationRequest(session_id="session-1", payment_id="wrong")
        )
