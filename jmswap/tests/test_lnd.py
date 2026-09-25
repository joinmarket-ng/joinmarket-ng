from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock

import pytest
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    get_txid,
    parse_transaction,
    scriptpubkey_to_address,
    serialize_transaction,
)

from jmswap.lnd import (
    COFUNDED_CHANNEL_RING_V1_CAPABLE,
    FINAL_TAPROOT_COMMITMENT,
    AcceptorBounds,
    EndpointRole,
    ExternalChannelRequest,
    FundingNegotiation,
    FundingTransactionChainStatus,
    InboundChannelExpectation,
    LndAmbiguousShimError,
    LndBackend,
    LndCapabilityError,
    LndRetirementPostconditionError,
    LndRetirementRpcError,
    LndRpcError,
    LndTimeoutError,
    LndValidationError,
    PendingChannelExpectation,
    ReadinessStatement,
    TransactionPresence,
    VerifiedChannelRetirementAuthorization,
    VerifiedChannelRetirementStatus,
    VerifiedFunding,
    WitnessUtxo,
    build_unsigned_psbt,
    can_advertise_cofunded_channel_ring_v1,
    evaluate_channel_accept_request,
    validate_contribution_accounting,
    validate_endpoint_readiness,
)
from jmswap.lndrpc import lightning_pb2 as ln

PENDING_ID = b"\x11" * 32
OPENER_ID = "02" + "22" * 32
FUNDEE_ID = "03" + "33" * 32
CHAIN_HASH = b"\x44" * 32
FUNDING_SCRIPT = bytes.fromhex("5120" + "55" * 32)
OTHER_SCRIPT = bytes.fromhex("5120" + "66" * 32)
FUNDING_ADDRESS = scriptpubkey_to_address(FUNDING_SCRIPT, "regtest")
CAPACITY = 1_500_000
PUSH = 600_000
OPENER_RESERVE = 10_000
FUNDEE_RESERVE = 15_000


class FakeStream:
    def __init__(self, update: Any = None, *, delay: float = 0) -> None:
        self.update = update
        self.delay = delay

    async def read(self) -> Any:
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.update


class FakeAcceptorCall:
    def __init__(self, requests: list[Any], connection_gate: asyncio.Event | None = None) -> None:
        self.requests = iter(requests)
        self.responses: list[Any] = []
        self.connection_gate = connection_gate

    async def wait_for_connection(self) -> None:
        if self.connection_gate is not None:
            await self.connection_gate.wait()

    def __aiter__(self) -> FakeAcceptorCall:
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self.requests)
        except StopIteration:
            raise StopAsyncIteration from None

    async def write(self, response: Any) -> None:
        self.responses.append(response)

    def cancel(self) -> None:
        return None


class FakeStub:
    def __init__(self) -> None:
        self.open_requests: list[Any] = []
        self.funding_steps: list[Any] = []
        self.connect_requests: list[Any] = []
        self.pending_responses: list[Any] = []
        self.open_errors: list[Exception] = []
        self.open_delay = 0.0
        self.acceptor_requests: list[Any] = []
        self.acceptor_call: FakeAcceptorCall | None = None
        self.acceptor_calls: list[FakeAcceptorCall] = []
        self.acceptor_connection_gate: asyncio.Event | None = None
        self.abandon_requests: list[Any] = []
        self.abandon_error: Exception | None = None
        self.keep_pending_after_abandon = False

    async def GetInfo(self, _request: Any) -> Any:  # noqa: N802
        return ln.GetInfoResponse(
            identity_pubkey=OPENER_ID,
            version="0.21.1-beta commit=v0.21.1-beta",
            synced_to_chain=True,
            wallet_synced=True,
            chains=[ln.Chain(chain="bitcoin", network="regtest")],
            uris=[f"{OPENER_ID}@{'a' * 56}.onion:9735"],
            features={
                81: ln.Feature(name="simple-taproot-chans", is_required=False, is_known=True)
            },
        )

    async def ConnectPeer(self, request: Any) -> Any:  # noqa: N802
        self.connect_requests.append(request)
        return ln.ConnectPeerResponse()

    def ChannelAcceptor(self) -> FakeAcceptorCall:  # noqa: N802
        self.acceptor_call = FakeAcceptorCall(self.acceptor_requests, self.acceptor_connection_gate)
        self.acceptor_calls.append(self.acceptor_call)
        return self.acceptor_call

    def OpenChannel(self, request: Any) -> FakeStream:  # noqa: N802
        self.open_requests.append(request)
        if self.open_errors:
            raise self.open_errors.pop(0)
        return FakeStream(
            ln.OpenStatusUpdate(
                pending_chan_id=request.funding_shim.psbt_shim.pending_chan_id,
                psbt_fund=ln.ReadyForPsbtFunding(
                    funding_address=FUNDING_ADDRESS,
                    funding_amount=CAPACITY,
                ),
            ),
            delay=self.open_delay,
        )

    async def FundingStateStep(self, request: Any) -> Any:  # noqa: N802
        self.funding_steps.append(request)
        return ln.FundingStateStepResp()

    async def PendingChannels(self, _request: Any) -> Any:  # noqa: N802
        if len(self.pending_responses) > 1:
            return self.pending_responses.pop(0)
        if self.pending_responses:
            return self.pending_responses[0]
        return ln.PendingChannelsResponse()

    async def AbandonChannel(self, request: Any) -> Any:  # noqa: N802
        self.abandon_requests.append(request)
        if self.abandon_error is not None:
            raise self.abandon_error
        if not self.keep_pending_after_abandon:
            self.pending_responses = [ln.PendingChannelsResponse()]
        return ln.AbandonChannelResponse(status="abandoned")

    async def SignMessage(self, request: Any) -> Any:  # noqa: N802
        assert request.msg.startswith(b"jmswap-lnd-readiness-v1\x00")
        return ln.SignMessageResponse(signature="signed")

    async def VerifyMessage(self, request: Any) -> Any:  # noqa: N802
        return ln.VerifyMessageResponse(valid=request.signature == "signed", pubkey=OPENER_ID)


def backend(stub: Any | None = None) -> LndBackend:
    result = LndBackend(host="localhost:10009", tls_cert=b"", macaroon_hex="")
    result._stub = stub or FakeStub()
    return result


def external_request(**changes: Any) -> ExternalChannelRequest:
    values: dict[str, Any] = {
        "pending_channel_id": PENDING_ID,
        "peer_node_id": FUNDEE_ID,
        "peer_host": "fundee:9735",
        "capacity_sat": CAPACITY,
        "push_sat": PUSH,
        "opener_reserve_sat": OPENER_RESERVE,
        "fundee_reserve_sat": FUNDEE_RESERVE,
        "opener_csv_delay": 144,
        "fundee_csv_delay": 288,
        "min_depth": 3,
        "timeout_seconds": 2.0,
    }
    values.update(changes)
    return ExternalChannelRequest(**values)


def inbound_expectation() -> InboundChannelExpectation:
    return InboundChannelExpectation(
        pending_channel_id=PENDING_ID,
        opener_node_id=OPENER_ID,
        chain_hash=CHAIN_HASH,
        capacity_sat=CAPACITY,
        push_msat=PUSH * 1000,
        opener_reserve_sat=OPENER_RESERVE,
        fundee_reserve_sat=FUNDEE_RESERVE,
        opener_csv_delay=144,
        fundee_csv_delay=288,
        min_depth=3,
    )


def test_retained_channel_contracts_resume_idempotently_and_reject_conflicts() -> None:
    backend = LndBackend("unused", b"cert", "00")
    negotiation = FundingNegotiation(
        pending_channel_id=PENDING_ID,
        peer_node_id=FUNDEE_ID,
        funding_address="bcrt1ptest",
        funding_script_pubkey=FUNDING_SCRIPT,
        capacity_sat=CAPACITY,
        push_sat=PUSH,
        opener_reserve_sat=OPENER_RESERVE,
        fundee_reserve_sat=FUNDEE_RESERVE,
        opener_csv_delay=144,
        fundee_csv_delay=288,
        min_depth=3,
    )
    backend.resume_external_channel(negotiation)
    backend.resume_external_channel(negotiation)
    backend.resume_inbound_channel(inbound_expectation())
    backend.resume_inbound_channel(inbound_expectation())
    assert backend._pending[PENDING_ID].negotiation == negotiation
    assert backend._accepted[PENDING_ID] == inbound_expectation()

    changed = negotiation.model_copy(update={"capacity_sat": CAPACITY + 1})
    with pytest.raises(LndValidationError, match="conflicts"):
        backend.resume_external_channel(changed)


def valid_accept_request() -> Any:
    return ln.ChannelAcceptRequest(
        pending_chan_id=PENDING_ID,
        node_pubkey=bytes.fromhex(OPENER_ID),
        chain_hash=CHAIN_HASH,
        funding_amt=CAPACITY,
        push_amt=PUSH * 1000,
        dust_limit=354,
        max_value_in_flight=CAPACITY * 990,
        channel_reserve=FUNDEE_RESERVE,
        min_htlc=1,
        fee_per_kw=2_500,
        csv_delay=288,
        max_accepted_htlcs=30,
        channel_flags=0,
        commitment_type=ln.TAPROOT,
        wants_zero_conf=False,
        wants_scid_alias=False,
    )


def funding_tx(
    script: bytes = FUNDING_SCRIPT, value: int = CAPACITY
) -> tuple[str, list[WitnessUtxo]]:
    inputs = [
        TxInput.from_hex("aa" * 32, 0, sequence=0xFFFFFFFD),
        TxInput.from_hex("bb" * 32, 1, sequence=0xFFFFFFFD),
    ]
    outputs = [
        TxOutput(value=value, script=script),
        TxOutput(value=490_000, script=bytes.fromhex("0014" + "77" * 20)),
    ]
    raw = serialize_transaction(2, inputs, outputs, 0).hex()
    utxos = [
        WitnessUtxo(value_sat=700_000, script_pubkey=bytes.fromhex("0014" + "88" * 20)),
        WitnessUtxo(value_sat=1_300_000, script_pubkey=bytes.fromhex("5120" + "99" * 32)),
    ]
    return raw, utxos


async def negotiate(target: LndBackend) -> Any:
    return await target.start_external_channel(external_request())


async def test_node_info_checks_identity_version_network_sync_and_capability() -> None:
    target = backend()
    info = await target.node_info()
    assert info.identity_pubkey == OPENER_ID
    assert info.version.startswith("0.21.1")
    assert info.feature_bits == frozenset({81})
    assert info.advertised_uris == (f"{OPENER_ID}@{'a' * 56}.onion:9735",)


async def test_production_capability_binds_onion_endpoint_to_getinfo_identity() -> None:
    target = backend()
    endpoint = "a" * 56 + ".onion:9735"
    info = await target.check_production_cofunded_channel_ring_v1(endpoint)
    assert info.identity_pubkey == OPENER_ID
    with pytest.raises(LndCapabilityError, match="not advertised"):
        await target.check_production_cofunded_channel_ring_v1("b" * 56 + ".onion:9735")


def test_lnd_credentials_are_redacted_from_repr() -> None:
    target = LndBackend(
        host="localhost:10009",
        tls_cert=b"private-tls-material",
        macaroon_hex="private-macaroon-material",
    )
    rendered = repr(target)
    assert "private-tls-material" not in rendered
    assert "private-macaroon-material" not in rendered


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("version", "0.20.1-beta", "lacks production"),
        ("synced_to_chain", False, "not fully synced"),
        ("wallet_synced", False, "not fully synced"),
        ("features", {}, "feature bit"),
    ],
)
async def test_node_info_rejects_missing_capability(
    field: str, value: Any, error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = FakeStub()

    async def get_info(_request: Any) -> Any:
        values: dict[str, Any] = {
            "identity_pubkey": OPENER_ID,
            "version": "0.21.1-beta",
            "synced_to_chain": True,
            "wallet_synced": True,
            "chains": [ln.Chain(chain="bitcoin", network="regtest")],
            "features": {
                80: ln.Feature(name="simple-taproot-chans", is_required=True, is_known=True)
            },
        }
        values[field] = value
        return ln.GetInfoResponse(**values)

    monkeypatch.setattr(stub, "GetInfo", get_info)
    with pytest.raises(LndCapabilityError, match=error):
        await backend(stub).node_info()


@pytest.mark.parametrize(
    "features",
    [
        {81: ln.Feature(name="taproot", is_required=False, is_known=True)},
        {81: ln.Feature(name="simple-taproot-chans", is_required=False, is_known=False)},
        {
            80: ln.Feature(name="simple-taproot-chans", is_required=True, is_known=True),
            81: ln.Feature(name="simple-taproot-chans", is_required=False, is_known=True),
        },
    ],
)
async def test_node_info_rejects_malformed_final_taproot_features(
    features: dict[int, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = FakeStub()

    async def get_info(_request: Any) -> Any:
        return ln.GetInfoResponse(
            identity_pubkey=OPENER_ID,
            version="0.21.1-beta",
            synced_to_chain=True,
            wallet_synced=True,
            chains=[ln.Chain(chain="bitcoin", network="regtest")],
            features=features,
        )

    monkeypatch.setattr(stub, "GetInfo", get_info)
    with pytest.raises(LndCapabilityError, match="feature"):
        await backend(stub).node_info()


async def test_open_uses_caller_id_exact_push_private_final_taproot_and_no_publish() -> None:
    target = backend()
    negotiated = await negotiate(target)
    request = target._stub.open_requests[0]
    assert negotiated.pending_channel_id == PENDING_ID
    assert negotiated.funding_script_pubkey == FUNDING_SCRIPT
    assert request.local_funding_amount == CAPACITY
    assert request.push_sat == PUSH
    assert request.private is True
    assert request.commitment_type == FINAL_TAPROOT_COMMITMENT
    assert request.zero_conf is False and request.scid_alias is False
    assert request.remote_csv_delay == 288
    assert request.max_local_csv == 144
    assert request.remote_chan_reserve_sat == FUNDEE_RESERVE
    assert request.funding_shim.psbt_shim.pending_chan_id == PENDING_ID
    assert request.funding_shim.psbt_shim.no_publish is True

    with pytest.raises(LndValidationError, match="already used"):
        await target.start_external_channel(external_request())


async def test_open_allows_tor_peer_connection_to_use_full_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = backend()
    connect_peer = AsyncMock()
    monkeypatch.setattr(target, "connect_peer", connect_peer)

    await target.start_external_channel(external_request(timeout_seconds=90.0))

    assert connect_peer.await_args is not None
    assert connect_peer.await_args.kwargs["timeout_seconds"] > 89.0


async def test_open_retries_transient_onion_peer_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = backend()
    connect_peer = AsyncMock(side_effect=[RuntimeError("TTL expired"), None])
    monkeypatch.setattr(target, "connect_peer", connect_peer)

    await target.start_external_channel(external_request(peer_host=f"{'a' * 56}.onion:9735"))

    assert connect_peer.await_count == 2


async def test_open_does_not_retry_non_onion_peer_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = backend()
    connect_peer = AsyncMock(side_effect=RuntimeError("connection refused"))
    monkeypatch.setattr(target, "connect_peer", connect_peer)

    # Reported as a backend error that still carries LND's own reason.
    with pytest.raises(LndRpcError, match="connection refused"):
        await target.start_external_channel(external_request())

    connect_peer.assert_awaited_once()


async def test_open_reports_a_rejected_channel_as_a_backend_error() -> None:
    stub = FakeStub()
    stub.open_errors = [RuntimeError("co-funded channel rejected: capacity")]
    target = backend(stub)

    with pytest.raises(LndRpcError, match="co-funded channel rejected: capacity"):
        await target.start_external_channel(external_request())


async def test_open_retries_peer_not_online_without_changing_pending_id() -> None:
    stub = FakeStub()
    stub.open_errors = [RuntimeError("peer is not online"), RuntimeError("peer not online")]
    target = backend(stub)
    negotiated = await negotiate(target)
    assert negotiated.pending_channel_id == PENDING_ID
    assert len(stub.open_requests) == 3
    assert {bytes(r.funding_shim.psbt_shim.pending_chan_id) for r in stub.open_requests} == {
        PENDING_ID
    }


async def test_open_timeout_attempts_safe_cancel() -> None:
    stub = FakeStub()
    stub.open_delay = 0.2
    target = backend(stub)
    with pytest.raises(LndTimeoutError, match="OpenChannel"):
        await target.start_external_channel(external_request(timeout_seconds=0.01))
    assert stub.funding_steps[-1].WhichOneof("trigger") == "shim_cancel"


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [
        ("pending_chan_id", b"\x99" * 32, "pending channel ID"),
        ("node_pubkey", bytes.fromhex(FUNDEE_ID), "opener node"),
        ("chain_hash", b"\x99" * 32, "chain hash"),
        ("funding_amt", CAPACITY - 1, "capacity"),
        ("push_amt", PUSH * 1000 - 1, "push amount"),
        ("commitment_type", ln.SIMPLE_TAPROOT, "commitment type"),
        ("channel_flags", 1, "private channel flags"),
        ("wants_zero_conf", True, "zero-conf"),
        ("wants_scid_alias", True, "SCID alias"),
        ("channel_reserve", FUNDEE_RESERVE - 1, "fundee reserve"),
        ("dust_limit", 20_000, "dust limit"),
        # Below the final-Taproot dust threshold that stock LND itself proposes.
        ("dust_limit", 330, "dust limit"),
        ("fee_per_kw", 2_000_000, "fee policy"),
        # Above the anchor commitment rate cap, so the opener would burn its own
        # ring contribution into the commitment fee.
        ("fee_per_kw", 25_000, "fee policy"),
        ("csv_delay", 144, "fundee CSV policy"),
    ],
)
def test_acceptor_rejects_every_contract_substitution(field: str, value: Any, label: str) -> None:
    request = valid_accept_request()
    setattr(request, field, value)
    decision = evaluate_channel_accept_request(request, inbound_expectation(), AcceptorBounds())
    assert decision.accept is False
    assert label in decision.error


def test_acceptor_accepts_only_exact_contract_and_returns_no_fallback() -> None:
    decision = evaluate_channel_accept_request(
        valid_accept_request(), inbound_expectation(), AcceptorBounds()
    )
    assert decision.accept is True
    assert decision.error == ""


async def test_acceptor_returns_exact_opener_csv_and_confirmation_policy() -> None:
    stub = FakeStub()
    stub.acceptor_requests = [valid_accept_request()]
    target = backend(stub)
    observation = await target.run_channel_acceptor(
        inbound_expectation(), AcceptorBounds(), timeout_seconds=1
    )
    assert observation.pending_channel_id == PENDING_ID
    assert stub.acceptor_call is not None
    response = stub.acceptor_call.responses[0]
    assert response.csv_delay == 144
    assert response.reserve_sat == OPENER_RESERVE
    assert response.min_accept_depth == 3


async def test_acceptor_shares_one_stream_for_duplicate_expectation() -> None:
    stub = FakeStub()
    stub.acceptor_requests = [valid_accept_request()]
    target = backend(stub)

    first, second = await asyncio.gather(
        target.run_channel_acceptor(inbound_expectation(), AcceptorBounds(), timeout_seconds=1),
        target.run_channel_acceptor(inbound_expectation(), AcceptorBounds(), timeout_seconds=1),
    )

    assert first == second
    assert len(stub.acceptor_calls) == 1


async def test_acceptor_routes_multiple_expected_pending_ids_on_one_stream() -> None:
    other_id = b"\x22" * 32
    other_request = valid_accept_request()
    other_request.pending_chan_id = other_id
    stub = FakeStub()
    stub.acceptor_requests = [valid_accept_request(), other_request]
    stub.acceptor_connection_gate = asyncio.Event()
    target = backend(stub)
    other_expected = inbound_expectation().model_copy(update={"pending_channel_id": other_id})
    first_ready = asyncio.Event()
    second_ready = asyncio.Event()

    observations = asyncio.gather(
        target.run_channel_acceptor(
            inbound_expectation(), AcceptorBounds(), timeout_seconds=1, ready=first_ready
        ),
        target.run_channel_acceptor(
            other_expected, AcceptorBounds(), timeout_seconds=1, ready=second_ready
        ),
    )
    while not stub.acceptor_calls:
        await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not first_ready.is_set()
    assert not second_ready.is_set()
    stub.acceptor_connection_gate.set()
    first, second = await observations

    assert {first.pending_channel_id, second.pending_channel_id} == {PENDING_ID, other_id}
    assert len(stub.acceptor_calls) == 1
    assert stub.acceptor_call is not None
    assert all(response.accept for response in stub.acceptor_call.responses)


async def test_acceptor_opens_the_stream_before_signalling_ready() -> None:
    stub = FakeStub()
    stub.acceptor_requests = [valid_accept_request()]
    stub.acceptor_connection_gate = asyncio.Event()
    target = backend(stub)
    ready = asyncio.Event()
    ready_when_opened: list[bool] = []
    open_stream = stub.ChannelAcceptor

    def recording_acceptor() -> Any:
        ready_when_opened.append(ready.is_set())
        return open_stream()

    stub.ChannelAcceptor = recording_acceptor  # type: ignore[method-assign]

    task = asyncio.create_task(
        target.run_channel_acceptor(
            inbound_expectation(), AcceptorBounds(), timeout_seconds=1, ready=ready
        )
    )
    while not stub.acceptor_calls:
        await asyncio.sleep(0)
    assert not ready.is_set()
    stub.acceptor_connection_gate.set()
    await asyncio.wait_for(ready.wait(), 1)

    # The ring tells its predecessor to open only after this event, so LND must
    # already hold the acceptor stream; otherwise an inbound open is judged by
    # LND's default policy instead of the negotiated contract.
    assert ready_when_opened == [False]
    assert len(stub.acceptor_calls) == 1
    await asyncio.wait_for(task, 1)


async def test_acceptor_stream_failure_is_raised_and_never_reports_ready() -> None:
    stub = FakeStub()

    def failing_acceptor() -> Any:
        raise RuntimeError("acceptor stream unavailable")

    stub.ChannelAcceptor = failing_acceptor  # type: ignore[method-assign]
    target = backend(stub)
    ready = asyncio.Event()

    with pytest.raises(RuntimeError, match="acceptor stream unavailable"):
        await target.run_channel_acceptor(
            inbound_expectation(), AcceptorBounds(), timeout_seconds=1, ready=ready
        )

    assert not ready.is_set()
    assert not target._acceptor_registrations


def test_psbt_preserves_txid_and_contains_every_v0_v1_witness_utxo() -> None:
    raw, utxos = funding_tx()
    psbt, txid = build_unsigned_psbt(raw, utxos)
    assert txid == get_txid(raw)
    assert psbt.startswith(b"psbt\xff")
    assert psbt[5:7] == b"\x01\x00"
    assert psbt[7] == len(bytes.fromhex(raw))
    for utxo in utxos:
        assert utxo.value_sat.to_bytes(8, "little") in psbt
        assert utxo.script_pubkey in psbt


def test_psbt_rejects_missing_or_non_segwit_witness_utxos() -> None:
    raw, utxos = funding_tx()
    with pytest.raises(LndValidationError, match="input_utxos"):
        build_unsigned_psbt(raw, utxos[:1])
    with pytest.raises(ValueError, match="SegWit"):
        WitnessUtxo(value_sat=1, script_pubkey=bytes.fromhex("76a914" + "11" * 20 + "88ac"))
    parsed = parse_transaction(raw)
    witnessed = serialize_transaction(
        parsed.version,
        parsed.inputs,
        parsed.outputs,
        parsed.locktime,
        [[b"signature"], []],
    ).hex()
    with pytest.raises(LndValidationError, match="witness data"):
        build_unsigned_psbt(witnessed, utxos)


async def test_verify_validates_output_and_txid_before_psbt_verify() -> None:
    stub = FakeStub()
    target = backend(stub)
    negotiated = await negotiate(target)
    raw, utxos = funding_tx()
    verified = await target.verify_external_funding(negotiated, raw, get_txid(raw), 0, utxos)
    assert verified.channel_point == f"{get_txid(raw)}:0"
    assert len(stub.funding_steps) == 1
    transition = stub.funding_steps[0]
    assert transition.WhichOneof("trigger") == "psbt_verify"
    assert transition.psbt_verify.skip_finalize is True
    assert transition.psbt_verify.pending_chan_id == PENDING_ID


def test_verified_funding_binds_its_txid_to_the_unsigned_psbt() -> None:
    raw, utxos = funding_tx()
    psbt, _ = build_unsigned_psbt(raw, utxos)
    with pytest.raises(ValueError, match="TXID differs"):
        VerifiedFunding(
            pending_channel_id=PENDING_ID,
            funding_txid="00" * 32,
            funding_vout=0,
            unsigned_psbt=psbt,
        )


@pytest.mark.parametrize(
    ("script", "value", "txid", "vout", "message"),
    [
        (OTHER_SCRIPT, CAPACITY, None, 0, "negotiated output"),
        (FUNDING_SCRIPT, CAPACITY - 1, None, 0, "negotiated output"),
        (FUNDING_SCRIPT, CAPACITY, "00" * 32, 0, "unsigned transaction TXID"),
        (FUNDING_SCRIPT, CAPACITY, None, 1, "negotiated output"),
        (FUNDING_SCRIPT, CAPACITY, None, 9, "outside"),
    ],
)
async def test_verify_rejects_script_txid_and_outpoint_substitution_before_rpc(
    script: bytes, value: int, txid: str | None, vout: int, message: str
) -> None:
    stub = FakeStub()
    target = backend(stub)
    negotiated = await negotiate(target)
    raw, utxos = funding_tx(script, value)
    with pytest.raises(LndValidationError, match=message):
        await target.verify_external_funding(negotiated, raw, txid or get_txid(raw), vout, utxos)
    assert stub.funding_steps == []


async def test_verified_channel_rejects_a_changed_outpoint() -> None:
    target = backend()
    negotiated = await negotiate(target)
    raw, utxos = funding_tx()
    await target.verify_external_funding(negotiated, raw, get_txid(raw), 0, utxos)
    with pytest.raises(LndValidationError, match="negotiated output"):
        await target.verify_external_funding(negotiated, raw, get_txid(raw), 1, utxos)


def pending_expectation(raw: str) -> PendingChannelExpectation:
    return PendingChannelExpectation(
        pending_channel_id=PENDING_ID,
        channel_point=f"{get_txid(raw)}:0",
        opener_node_id=OPENER_ID,
        fundee_node_id=FUNDEE_ID,
        capacity_sat=CAPACITY,
        opener_contribution_sat=CAPACITY - PUSH,
        fundee_contribution_sat=PUSH,
        opener_reserve_sat=OPENER_RESERVE,
        fundee_reserve_sat=FUNDEE_RESERVE,
    )


def pending_response(expected: PendingChannelExpectation, role: EndpointRole) -> Any:
    opener = role is EndpointRole.OPENER
    channel = ln.PendingChannelsResponse.PendingChannel(
        remote_node_pub=FUNDEE_ID if opener else OPENER_ID,
        channel_point=expected.channel_point,
        capacity=CAPACITY,
        local_balance=CAPACITY - PUSH - 1_660 if opener else PUSH,
        remote_balance=PUSH if opener else CAPACITY - PUSH - 1_660,
        local_chan_reserve_sat=OPENER_RESERVE if opener else FUNDEE_RESERVE,
        remote_chan_reserve_sat=FUNDEE_RESERVE if opener else OPENER_RESERVE,
        initiator=ln.INITIATOR_LOCAL if opener else ln.INITIATOR_REMOTE,
        commitment_type=ln.TAPROOT,
        private=True,
    )
    return ln.PendingChannelsResponse(
        pending_open_channels=[
            ln.PendingChannelsResponse.PendingOpenChannel(channel=channel, commit_fee=1_000)
        ]
    )


async def test_pending_state_retries_then_models_both_exact_endpoints() -> None:
    raw, _ = funding_tx()
    expected = pending_expectation(raw)
    opener_stub = FakeStub()
    fundee_stub = FakeStub()
    opener_stub.pending_responses = [
        ln.PendingChannelsResponse(),
        pending_response(expected, EndpointRole.OPENER),
    ]
    fundee_stub.pending_responses = [pending_response(expected, EndpointRole.FUNDEE)]
    opener = await backend(opener_stub).pending_channel_observation(
        expected, EndpointRole.OPENER, timeout_seconds=1, poll_interval=0
    )
    fundee = await backend(fundee_stub).pending_channel_observation(
        expected, EndpointRole.FUNDEE, timeout_seconds=1, poll_interval=0
    )
    readiness = validate_endpoint_readiness(opener, fundee, expected)
    assert readiness.opener.initiator == ln.INITIATOR_LOCAL
    assert readiness.fundee.initiator == ln.INITIATOR_REMOTE
    assert (
        readiness.opener.local_balance_sat
        + readiness.opener.commit_fee_sat
        + readiness.opener.commitment_overhead_sat
        == CAPACITY - PUSH
    )
    assert readiness.fundee.local_balance_sat == PUSH


def retirement_authorization(
    verified: VerifiedFunding, **changes: Any
) -> VerifiedChannelRetirementAuthorization:
    values: dict[str, Any] = {
        "verified_funding": verified,
        "channel_point": verified.channel_point,
        "unsigned_txid": verified.funding_txid,
        "chain_status": FundingTransactionChainStatus(
            unsigned_txid=verified.funding_txid,
            mempool=TransactionPresence.ABSENT,
            chain=TransactionPresence.ABSENT,
        ),
        "local_coinjoin_input_signature_created": False,
        "local_coinjoin_input_signature_sent": False,
        "funding_transaction_broadcast": False,
        "final_transaction_observed": False,
    }
    values.update(changes)
    return VerifiedChannelRetirementAuthorization(**values)


async def verified_retirement_target() -> tuple[LndBackend, FakeStub, VerifiedFunding]:
    stub = FakeStub()
    target = backend(stub)
    negotiated = await negotiate(target)
    raw, utxos = funding_tx()
    verified = await target.verify_external_funding(negotiated, raw, get_txid(raw), 0, utxos)
    expected = pending_expectation(raw)
    stub.pending_responses = [pending_response(expected, EndpointRole.OPENER)]
    await target.pending_channel_observation(
        expected, EndpointRole.OPENER, timeout_seconds=1, poll_interval=0
    )
    return target, stub, verified


@pytest.mark.parametrize(
    ("change", "value", "message"),
    [
        ("local_coinjoin_input_signature_created", True, "signature was already created"),
        ("local_coinjoin_input_signature_sent", True, "signature was already sent"),
        ("funding_transaction_broadcast", True, "already broadcast"),
        ("final_transaction_observed", True, "final funding transaction"),
    ],
)
async def test_retirement_refuses_each_local_safety_precondition(
    change: str, value: bool, message: str
) -> None:
    target, stub, verified = await verified_retirement_target()
    with pytest.raises(LndValidationError, match=message):
        await target.retire_verified_external_channel(
            retirement_authorization(verified, **{change: value})
        )
    assert stub.abandon_requests == []


@pytest.mark.parametrize("location", ["mempool", "chain"])
@pytest.mark.parametrize("presence", [TransactionPresence.PRESENT, TransactionPresence.UNKNOWN])
async def test_retirement_refuses_present_or_uncertain_chain_status(
    location: str, presence: TransactionPresence
) -> None:
    target, stub, verified = await verified_retirement_target()
    status = FundingTransactionChainStatus(
        unsigned_txid=verified.funding_txid,
        mempool=presence if location == "mempool" else TransactionPresence.ABSENT,
        chain=presence if location == "chain" else TransactionPresence.ABSENT,
    )
    with pytest.raises(LndValidationError, match="present or uncertain"):
        await target.retire_verified_external_channel(
            retirement_authorization(verified, chain_status=status)
        )
    assert stub.abandon_requests == []


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"channel_point": "99" * 32 + ":0"}, "channel point"),
        ({"unsigned_txid": "99" * 32}, "unsigned TXID"),
        (
            {
                "chain_status": FundingTransactionChainStatus(
                    unsigned_txid="99" * 32,
                    mempool=TransactionPresence.ABSENT,
                    chain=TransactionPresence.ABSENT,
                )
            },
            "chain status TXID",
        ),
    ],
)
async def test_retirement_refuses_substituted_point_or_txid(
    changes: dict[str, Any], message: str
) -> None:
    target, stub, verified = await verified_retirement_target()
    with pytest.raises(LndValidationError, match=message):
        await target.retire_verified_external_channel(retirement_authorization(verified, **changes))
    assert stub.abandon_requests == []


async def test_retirement_refuses_verified_funding_substituted_for_local_state() -> None:
    target, stub, verified = await verified_retirement_target()
    substituted = verified.model_copy(update={"funding_vout": 1})
    with pytest.raises(LndValidationError, match="local verified funding differs"):
        await target.retire_verified_external_channel(retirement_authorization(substituted))
    assert stub.abandon_requests == []


async def test_retirement_refuses_present_point_not_observed_by_this_backend() -> None:
    stub = FakeStub()
    target = backend(stub)
    negotiated = await negotiate(target)
    raw, utxos = funding_tx()
    verified = await target.verify_external_funding(negotiated, raw, get_txid(raw), 0, utxos)
    stub.pending_responses = [pending_response(pending_expectation(raw), EndpointRole.OPENER)]
    with pytest.raises(LndValidationError, match="not locally observed"):
        await target.retire_verified_external_channel(retirement_authorization(verified))
    assert stub.abandon_requests == []


async def test_retirement_abandons_exact_present_point_with_dangerous_flags() -> None:
    target, stub, verified = await verified_retirement_target()
    outcome = await target.retire_verified_external_channel(
        retirement_authorization(verified), timeout_seconds=1, poll_interval=0
    )
    assert outcome.status is VerifiedChannelRetirementStatus.ABANDONED
    assert outcome.channel_point == verified.channel_point
    assert outcome.lnd_status == "abandoned"
    request = stub.abandon_requests[0]
    assert request.channel_point.funding_txid_str == verified.funding_txid
    assert request.channel_point.output_index == verified.funding_vout
    assert request.pending_funding_shim_only is False
    assert request.i_know_what_i_am_doing is True


async def test_retirement_absent_point_is_idempotent_only_with_fresh_authorization() -> None:
    target, stub, verified = await verified_retirement_target()
    stub.pending_responses = [ln.PendingChannelsResponse()]
    first = await target.retire_verified_external_channel(retirement_authorization(verified))
    second = await target.retire_verified_external_channel(retirement_authorization(verified))
    assert first.status is VerifiedChannelRetirementStatus.ALREADY_ABSENT
    assert second.status is VerifiedChannelRetirementStatus.ALREADY_ABSENT
    assert stub.abandon_requests == []

    uncertain = retirement_authorization(
        verified,
        chain_status=FundingTransactionChainStatus(
            unsigned_txid=verified.funding_txid,
            mempool=TransactionPresence.UNKNOWN,
            chain=TransactionPresence.ABSENT,
        ),
    )
    with pytest.raises(LndValidationError, match="uncertain"):
        await target.retire_verified_external_channel(uncertain)


async def test_retirement_propagates_abandon_rpc_failure_without_claiming_success() -> None:
    target, stub, verified = await verified_retirement_target()
    stub.abandon_error = RuntimeError("stock LND rejected abandon")
    with pytest.raises(LndRetirementRpcError, match="AbandonChannel failed"):
        await target.retire_verified_external_channel(retirement_authorization(verified))


async def test_retirement_fails_if_pending_point_remains_after_abandon() -> None:
    target, stub, verified = await verified_retirement_target()
    stub.keep_pending_after_abandon = True
    with pytest.raises(LndRetirementPostconditionError, match="still contains"):
        await target.retire_verified_external_channel(
            retirement_authorization(verified), timeout_seconds=0.01, poll_interval=0
        )


def test_ring_capability_requires_a_complete_lifecycle_strategy() -> None:
    assert not can_advertise_cofunded_channel_ring_v1(
        safe_verified_retirement_live_validated=False,
        retained_state_reconciliation=True,
        strict_anti_grief_limits=False,
    )
    assert can_advertise_cofunded_channel_ring_v1(
        safe_verified_retirement_live_validated=False,
        retained_state_reconciliation=True,
        strict_anti_grief_limits=True,
    )
    assert COFUNDED_CHANNEL_RING_V1_CAPABLE is True


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("remote_node_pub", OPENER_ID, "remote node"),
        ("capacity", CAPACITY - 1, "capacity"),
        ("commitment_type", ln.SIMPLE_TAPROOT, "commitment type"),
        ("initiator", ln.INITIATOR_REMOTE, "initiator"),
        ("private", False, "private"),
        ("local_chan_reserve_sat", OPENER_RESERVE - 1, "local reserve"),
        ("remote_chan_reserve_sat", FUNDEE_RESERVE - 1, "remote reserve"),
    ],
)
async def test_pending_state_rejects_wrong_exact_fields(
    field: str, value: Any, message: str
) -> None:
    raw, _ = funding_tx()
    expected = pending_expectation(raw)
    response = pending_response(expected, EndpointRole.OPENER)
    setattr(response.pending_open_channels[0].channel, field, value)
    stub = FakeStub()
    stub.pending_responses = [response]
    with pytest.raises(LndValidationError, match=message):
        await backend(stub).pending_channel_observation(expected, EndpointRole.OPENER)


def test_balance_validation_includes_persisted_initiator_commit_fee() -> None:
    raw, _ = funding_tx()
    expected = pending_expectation(raw)
    channel = pending_response(expected, EndpointRole.OPENER).pending_open_channels[0]
    observation = LndBackend._parse_pending_observation(
        channel.channel, int(channel.commit_fee), expected, EndpointRole.OPENER
    )
    validate_contribution_accounting(observation, expected)
    changed = observation.model_copy(
        update={"local_balance_sat": observation.local_balance_sat - 1}
    )
    with pytest.raises(LndValidationError, match="balance plus persisted"):
        validate_contribution_accounting(changed, expected)


async def test_pending_state_wrong_outpoint_times_out_without_fallback() -> None:
    raw, _ = funding_tx()
    expected = pending_expectation(raw)
    response = pending_response(expected, EndpointRole.OPENER)
    response.pending_open_channels[0].channel.channel_point = "99" * 32 + ":0"
    stub = FakeStub()
    stub.pending_responses = [response]
    with pytest.raises(LndTimeoutError, match="exact point"):
        await backend(stub).pending_channel_observation(
            expected, EndpointRole.OPENER, timeout_seconds=0.01, poll_interval=0
        )


def test_endpoint_readiness_requires_both_roles() -> None:
    raw, _ = funding_tx()
    expected = pending_expectation(raw)
    item = pending_response(expected, EndpointRole.OPENER).pending_open_channels[0]
    opener = LndBackend._parse_pending_observation(
        item.channel, item.commit_fee, expected, EndpointRole.OPENER
    )
    with pytest.raises(LndValidationError, match="both opener and fundee"):
        validate_endpoint_readiness(opener, deepcopy(opener), expected)


async def test_cancel_sends_shim_cancel_and_forbids_post_signature_cancel() -> None:
    stub = FakeStub()
    target = backend(stub)
    await negotiate(target)
    await target.cancel_external_channel(PENDING_ID, input_signatures_added=False)
    assert stub.funding_steps[-1].shim_cancel.pending_chan_id == PENDING_ID
    with pytest.raises(LndValidationError, match="input signatures"):
        await target.cancel_external_channel(PENDING_ID, input_signatures_added=True)


async def test_cancel_refuses_to_claim_success_after_psbt_verify() -> None:
    target = backend()
    negotiated = await negotiate(target)
    raw, utxos = funding_tx()
    await target.verify_external_funding(negotiated, raw, get_txid(raw), 0, utxos)
    with pytest.raises(LndValidationError, match="cancel before verification"):
        await target.cancel_external_channel(PENDING_ID, input_signatures_added=False)


def absent_funding_intent(stub: FakeStub) -> None:
    """Make LND answer shim_cancel the way it does for an absent funding intent."""

    async def missing_intent(request: Any) -> Any:
        stub.funding_steps.append(request)
        raise RuntimeError("no funding intent found for pending_chan_id")

    stub.FundingStateStep = missing_intent  # type: ignore[method-assign]


async def test_cancel_tolerates_absent_intent_for_a_locally_created_shim() -> None:
    stub = FakeStub()
    target = backend(stub)
    await negotiate(target)
    absent_funding_intent(stub)

    # This process created the shim and knows PsbtVerify never ran, so LND
    # reporting no intent means the shim is genuinely gone.
    await target.cancel_external_channel(PENDING_ID, input_signatures_added=False)
    assert PENDING_ID not in target._pending


async def test_cancel_of_a_resumed_shim_never_reports_success_when_absent() -> None:
    negotiated = await negotiate(backend())
    stub = FakeStub()
    absent_funding_intent(stub)
    restarted = backend(stub)
    restarted.resume_external_channel(negotiated)

    # After a restart the shim may have been consumed by a PsbtVerify whose result
    # was never persisted, and LND cannot distinguish that from a canceled shim, so
    # the caller must reconcile rather than record a clean retirement.
    with pytest.raises(LndAmbiguousShimError, match="reconcile instead"):
        await restarted.cancel_external_channel(PENDING_ID, input_signatures_added=False)


async def test_cancel_of_an_unknown_absent_shim_is_ambiguous() -> None:
    stub = FakeStub()
    absent_funding_intent(stub)
    target = backend(stub)

    with pytest.raises(LndAmbiguousShimError, match="unknown or resumed"):
        await target.cancel_external_channel(PENDING_ID, input_signatures_added=False)


async def test_resumed_verified_shim_refuses_cancel_and_allows_guarded_retirement() -> None:
    negotiated = await negotiate(backend())
    raw, utxos = funding_tx()
    verified = VerifiedFunding(
        pending_channel_id=PENDING_ID,
        funding_txid=get_txid(raw),
        funding_vout=0,
        unsigned_psbt=build_unsigned_psbt(raw, utxos)[0],
    )
    stub = FakeStub()
    stub.pending_responses = [pending_response(pending_expectation(raw), EndpointRole.OPENER)]
    restarted = backend(stub)
    restarted.resume_external_channel(
        negotiated, verified=verified, observed_channel_point=verified.channel_point
    )

    with pytest.raises(LndValidationError, match="cancel before verification"):
        await restarted.cancel_external_channel(PENDING_ID, input_signatures_added=False)

    outcome = await restarted.retire_verified_external_channel(retirement_authorization(verified))
    assert outcome.status is VerifiedChannelRetirementStatus.ABANDONED
    assert stub.abandon_requests[0].i_know_what_i_am_doing is True


async def test_resumed_evidence_must_match_its_shim_and_verified_point() -> None:
    negotiated = await negotiate(backend())
    raw, utxos = funding_tx()
    verified = VerifiedFunding(
        pending_channel_id=b"\x33" * 32,
        funding_txid=get_txid(raw),
        funding_vout=0,
        unsigned_psbt=build_unsigned_psbt(raw, utxos)[0],
    )
    restarted = backend()
    with pytest.raises(LndValidationError, match="another shim"):
        restarted.resume_external_channel(negotiated, verified=verified)
    with pytest.raises(LndValidationError, match="does not match the verified point"):
        restarted.resume_external_channel(negotiated, observed_channel_point=f"{get_txid(raw)}:0")


def test_resumed_evidence_must_not_conflict_with_memory() -> None:
    negotiated = FundingNegotiation(
        pending_channel_id=PENDING_ID,
        peer_node_id=FUNDEE_ID,
        funding_address=FUNDING_ADDRESS,
        funding_script_pubkey=FUNDING_SCRIPT,
        capacity_sat=CAPACITY,
        push_sat=PUSH,
        opener_reserve_sat=OPENER_RESERVE,
        fundee_reserve_sat=FUNDEE_RESERVE,
        opener_csv_delay=144,
        fundee_csv_delay=288,
        min_depth=3,
    )
    raw, utxos = funding_tx()
    verified = VerifiedFunding(
        pending_channel_id=PENDING_ID,
        funding_txid=get_txid(raw),
        funding_vout=0,
        unsigned_psbt=build_unsigned_psbt(raw, utxos)[0],
    )
    restarted = backend()
    restarted.resume_external_channel(
        negotiated, verified=verified, observed_channel_point=verified.channel_point
    )

    with pytest.raises(LndValidationError, match="verified funding conflicts"):
        restarted.resume_external_channel(
            negotiated, verified=verified.model_copy(update={"funding_vout": 1})
        )
    with pytest.raises(LndValidationError, match="channel point conflicts"):
        restarted.resume_inbound_channel(
            inbound_expectation(), observed_channel_point=f"{'99' * 32}:1"
        )


def readiness_statement(raw: str) -> ReadinessStatement:
    return ReadinessStatement(
        pending_channel_id=PENDING_ID.hex(),
        channel_point=f"{get_txid(raw)}:0",
        unsigned_txid=get_txid(raw),
        funding_script_pubkey=FUNDING_SCRIPT.hex(),
        capacity_sat=CAPACITY,
        push_sat=PUSH,
        opener_node_id=OPENER_ID,
        fundee_node_id=FUNDEE_ID,
        chain_hash=CHAIN_HASH.hex(),
    )


async def test_canonical_readiness_is_signed_and_verified_by_exact_node_key() -> None:
    raw, _ = funding_tx()
    statement = readiness_statement(raw)
    assert statement.canonical_bytes() == readiness_statement(raw).canonical_bytes()
    target = backend()
    signature = await target.sign_readiness(statement)
    assert await target.verify_readiness_signature(statement, signature, OPENER_ID) is True
    assert await target.verify_readiness_signature(statement, signature, FUNDEE_ID) is False
