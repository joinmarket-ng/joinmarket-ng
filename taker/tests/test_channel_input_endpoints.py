"""Post-prepare local LND and encrypted maker endpoint adapter tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from jmcore.bitcoin import get_txid
from jmcore.channel_input import (
    ChannelInputStatusPayload,
    ChannelInputTranscript,
    payload_hash,
    sign_payload,
    transcript_hash,
)
from jmswap.channel_input import InitializedChannelInputBackend
from jmswap.lnd import (
    EscrowOutpoint,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    WitnessUtxo,
)
from test_channel_input_negotiation import (
    _MAKER_SECRETS,
    _maker_finalized,
    _maker_partial,
    _maker_prepared,
)
from test_taker_channel_input_registry import _config, _frozen_prepared
from test_taker_channel_input_transport import _response, _transport

from taker.channel_input_endpoints import (
    LocalChannelInputEndpoint,
    RemoteChannelInputEndpoint,
    TakerChannelInputEndpointError,
)
from taker.channel_input_negotiation import TakerChannelInputNegotiation, TakerChannelInputSnapshot
from taker.channel_input_runtime import TakerChannelInputRegistry
from taker.channel_input_store import TakerChannelInputStore


class EndpointBackend:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.signing_attempt = LndEscrowSigningAttempt(
            attempt_id=b"\x10" * 32, public_nonce=b"\x11" * 66
        )
        self.partial = b"\x12" * 32

    async def prepare_channel_input_parent(
        self,
        _session_id: str,
        _outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        _utxos: tuple[WitnessUtxo, ...],
        _index: int,
    ) -> str:
        self.events.append("prepare")
        return get_txid(parent_tx_hex)

    async def begin_channel_input_signing(
        self, _session_id: str, _outpoint: EscrowOutpoint, *, restart_attempt: bool = False
    ) -> LndEscrowSigningAttempt:
        self.events.append(f"begin:{restart_attempt}")
        return self.signing_attempt

    async def sign_channel_input_parent(
        self,
        _session_id: str,
        _outpoint: EscrowOutpoint,
        _attempt: LndEscrowSigningAttempt,
        _remote_nonce: bytes,
    ) -> bytes:
        self.events.append("sign")
        return self.partial

    async def finalize_channel_input_parent(
        self,
        _session_id: str,
        _outpoint: EscrowOutpoint,
        _attempt: LndEscrowSigningAttempt,
        _remote_partial: bytes,
    ) -> object:
        self.events.append("finalize")
        raise AssertionError("local finalize is not expected in this test backend")

    async def recover_channel_input_parent(
        self, _session_id: str, _outpoint: EscrowOutpoint, _remote_partial: bytes
    ) -> object:
        self.events.append("recover")
        raise AssertionError("local recovery is not expected in this test backend")

    async def find_channel_input_escrow_status(
        self, _session_id: str, _outpoint: EscrowOutpoint
    ) -> LndEscrowStatus:
        self.events.append("status")
        return LndEscrowStatus(
            stage=LndEscrowStage.PARENT_SIGNED,
            parent_txid="01" * 32,
            finalized=False,
            durable_local_public_nonce=self.signing_attempt.public_nonce,
            durable_remote_public_nonce=b"\x13" * 66,
            durable_local_partial_signature=self.partial,
            active_attempt=self.signing_attempt,
        )


def _registry(tmp_path: Path) -> tuple[TakerChannelInputRegistry, TakerChannelInputNegotiation]:
    negotiation = _frozen_prepared()
    outpoints = tuple(intent.funding_outpoint for _, intent in negotiation.freeze_intents)
    store = TakerChannelInputStore(tmp_path / "channel-input", max_active_sessions=4)
    store.save(negotiation.snapshot())
    registry = TakerChannelInputRegistry.load(
        store=store, config=_config(*outpoints), network="regtest", local_actor="taker"
    )
    restored = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert restored is not None
    return registry, restored


def _arguments(
    negotiation: TakerChannelInputNegotiation, nick: str
) -> tuple[EscrowOutpoint, tuple[WitnessUtxo, ...], int]:
    transcript = negotiation.transcript
    assert transcript is not None
    hello = dict(negotiation.remote_hellos)[nick]
    outpoint = EscrowOutpoint(txid=hello.funding_outpoint.txid, vout=hello.funding_outpoint.vout)
    utxos = tuple(
        WitnessUtxo(value_sat=item.value_sat, script_pubkey=bytes.fromhex(item.script_pubkey))
        for item in transcript.input_prevouts
    )
    index = next(
        item.input_index
        for item in transcript.channels
        if item.funding_outpoint == hello.funding_outpoint
    )
    return outpoint, utxos, index


def _transcript(negotiation: TakerChannelInputNegotiation) -> ChannelInputTranscript:
    return cast(ChannelInputTranscript, negotiation.transcript)


@pytest.mark.asyncio
async def test_remote_snapshot_is_reciprocal_and_replays_without_lnd(tmp_path: Path) -> None:
    registry, negotiation = _registry(tmp_path)
    backend = EndpointBackend()
    transport, _directory, _cryptos = _transport(
        negotiation,
        negotiation.selected_nicks,
        registry=registry,
        initialized_backend=SimpleNamespace(backend=backend),
    )
    local = LocalChannelInputEndpoint(
        registry=registry,
        transport=transport,
        initialized_backend=cast(InitializedChannelInputBackend, SimpleNamespace(backend=backend)),
        session_id=negotiation.invite.session_id,
        attempt=negotiation.invite.attempt,
        nick="maker-a",
    )
    remote = RemoteChannelInputEndpoint(
        registry=registry,
        transport=transport,
        initialized_backend=cast(InitializedChannelInputBackend, SimpleNamespace(backend=backend)),
        session_id=negotiation.invite.session_id,
        attempt=negotiation.invite.attempt,
        nick="maker-a",
    )
    plan_channel_id = (
        f"channel-input:{dict(negotiation.frozen_inputs)['maker-a'].snapshot.funding_outpoint.txid}:"
        f"{dict(negotiation.frozen_inputs)['maker-a'].snapshot.funding_outpoint.vout}"
    )
    frozen = await local.freeze_channel_input(negotiation.invite.session_id, plan_channel_id)
    reciprocal = await remote.freeze_channel_input(negotiation.invite.session_id, plan_channel_id)

    assert reciprocal.funding_outpoint == frozen.funding_outpoint
    assert reciprocal.local_funding_pubkey == frozen.remote_funding_pubkey
    assert reciprocal.remote_funding_pubkey == frozen.local_funding_pubkey
    assert reciprocal.local_claim_sat == frozen.remote_claim_sat
    assert reciprocal.remote_claim_sat == frozen.local_claim_sat
    assert reciprocal.peer_node_pubkey == bytes.fromhex(
        dict(negotiation.local_hellos)["maker-a"].node_key
    )
    assert backend.events == []

    outpoint, utxos, index = _arguments(negotiation, "maker-a")
    with pytest.raises(TakerChannelInputEndpointError, match="durable transcript"):
        await local.prepare_channel_input_parent(
            negotiation.invite.session_id,
            outpoint,
            _transcript(negotiation).unsigned_parent_tx,
            utxos,
            index + 1,
        )
    assert backend.events == []


@pytest.mark.asyncio
async def test_remote_exchange_persists_evidence_and_maps_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, negotiation = _registry(tmp_path)
    backend = EndpointBackend()
    initialized = cast(InitializedChannelInputBackend, SimpleNamespace(backend=backend))
    transport, directory, cryptos = _transport(
        negotiation,
        negotiation.selected_nicks,
        registry=registry,
        initialized_backend=initialized,
    )
    remote = RemoteChannelInputEndpoint(
        registry=registry,
        transport=transport,
        initialized_backend=initialized,
        session_id=negotiation.invite.session_id,
        attempt=negotiation.invite.attempt,
        nick="maker-a",
    )
    local = LocalChannelInputEndpoint(
        registry=registry,
        transport=transport,
        initialized_backend=initialized,
        session_id=negotiation.invite.session_id,
        attempt=negotiation.invite.attempt,
        nick="maker-a",
    )
    outpoint, utxos, index = _arguments(negotiation, "maker-a")
    prepared = _maker_prepared(negotiation, "maker-a", b"\x21" * 66)
    directory.responses = {"maker-a": _response("maker-a", prepared, cryptos["maker-a"], "first")}
    assert await remote.prepare_channel_input_parent(
        negotiation.invite.session_id,
        outpoint,
        _transcript(negotiation).unsigned_parent_tx,
        utxos,
        index,
    ) == get_txid(_transcript(negotiation).unsigned_parent_tx)

    local_attempt = await local.begin_channel_input_signing(negotiation.invite.session_id, outpoint)
    remote_attempt = await remote.begin_channel_input_signing(
        negotiation.invite.session_id, outpoint
    )
    assert remote_attempt == await remote.begin_channel_input_signing(
        negotiation.invite.session_id, outpoint
    )
    assert remote_attempt.attempt_id != local_attempt.attempt_id
    save_events: list[str] = []
    original_save = registry._store.save

    def save_before_sign(snapshot: TakerChannelInputSnapshot) -> None:
        save_events.append("save")
        backend.events.append("save")
        directory.events.append("save")
        original_save(snapshot)

    monkeypatch.setattr(registry._store, "save", save_before_sign)
    await local.sign_channel_input_parent(
        negotiation.invite.session_id, outpoint, local_attempt, remote_attempt.public_nonce
    )
    assert backend.events.index("save") < backend.events.index("sign")
    current = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert current is not None
    remote_partial = _maker_partial(current, "maker-a", b"\x22" * 32)
    directory.responses = {
        "maker-a": _response("maker-a", remote_partial, cryptos["maker-a"], "second")
    }
    assert (
        await remote.sign_channel_input_parent(
            negotiation.invite.session_id, outpoint, remote_attempt, local_attempt.public_nonce
        )
        == b"\x22" * 32
    )

    current = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert current is not None
    finalized = _maker_finalized(current, "maker-a")
    directory.responses = {"maker-a": _response("maker-a", finalized, cryptos["maker-a"], "final")}
    finalized_parent = await remote.finalize_channel_input_parent(
        negotiation.invite.session_id, outpoint, remote_attempt, backend.partial
    )
    assert finalized_parent.channel_input_index == index
    assert finalized_parent.channel_input_witness == (b"\x99" * 64,)
    assert (
        await remote.recover_channel_input_parent(
            negotiation.invite.session_id, outpoint, backend.partial
        )
        == finalized_parent
    )
    assert len(directory.routed) == 2
    current = registry.get(negotiation.invite.session_id, negotiation.invite.attempt)
    assert current is not None
    request = await registry.build_status_request(
        negotiation.invite.session_id, negotiation.invite.attempt, "maker-a"
    )
    status = sign_payload(
        ChannelInputStatusPayload(
            session_id=negotiation.invite.session_id,
            attempt=negotiation.invite.attempt,
            signer_key=dict(current.remote_hellos)["maker-a"].participant_key,
            transcript_hash=transcript_hash(_transcript(current)).hex(),
            funding_outpoint=dict(current.remote_hellos)["maker-a"].funding_outpoint,
            query_id=request.query_id,
            status_request_hash=payload_hash(request).hex(),
            escrow_stage="finalized",
            attempt_active=False,
            prepared=prepared,
            partial=remote_partial,
            finalized=finalized,
        ),
        _MAKER_SECRETS[0],
        aux_randomness=b"\x23" * 32,
    )
    directory.responses = {"maker-a": _response("maker-a", status, cryptos["maker-a"], "third")}
    mapped = await remote.channel_input_escrow_status(negotiation.invite.session_id, outpoint)

    assert mapped.stage is LndEscrowStage.FINALIZED
    assert mapped.durable_local_public_nonce == remote_attempt.public_nonce
    assert mapped.durable_remote_public_nonce == local_attempt.public_nonce
    assert mapped.durable_local_partial_signature == b"\x22" * 32
    assert mapped.finalized_parent == finalized_parent
    assert mapped.active_attempt is None
    assert len(directory.routed) == 3
    assert save_events
    assert directory.events.index("save") < directory.events.index(
        "send:channel_input_status_request:maker-a"
    )


@pytest.mark.asyncio
async def test_endpoint_rejects_wrong_outpoint_and_delegates_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry, negotiation = _registry(tmp_path)
    backend = EndpointBackend()
    initialized = cast(InitializedChannelInputBackend, SimpleNamespace(backend=backend))
    transport, _directory, _cryptos = _transport(
        negotiation, negotiation.selected_nicks, registry=registry, initialized_backend=initialized
    )
    endpoint = RemoteChannelInputEndpoint(
        registry=registry,
        transport=transport,
        initialized_backend=initialized,
        session_id=negotiation.invite.session_id,
        attempt=negotiation.invite.attempt,
        nick="maker-a",
    )
    outpoint, _utxos, _index = _arguments(negotiation, "maker-a")
    await registry.accept_remote_prepared(
        negotiation.invite.session_id,
        negotiation.invite.attempt,
        "maker-a",
        _maker_prepared(negotiation, "maker-a", b"\x31" * 66),
    )
    with pytest.raises(TakerChannelInputEndpointError, match="funding outpoint"):
        await endpoint.begin_channel_input_signing(
            negotiation.invite.session_id,
            EscrowOutpoint(txid="ff" * 32, vout=outpoint.vout),
        )

    calls: list[tuple[str, int]] = []

    async def cancel(session_id: str, attempt: int) -> bool:
        calls.append((session_id, attempt))
        return True

    monkeypatch.setattr(transport, "cancel_or_replay", cancel)
    assert await endpoint.cancel_channel_input_escrow(negotiation.invite.session_id, outpoint)
    assert calls == [(negotiation.invite.session_id, negotiation.invite.attempt)]
