"""Focused durable endpoint runtime tests without LND or Bitcoin Core."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from jmswap.channel_escrow import (
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowOutpoint,
    EscrowTimeoutEvidence,
    FreezeChannelRequest,
)
from jmswap.lnd import (
    LndEscrowInvoice,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
)
from jmswap.peer_transport import (
    EscrowInvoiceProposalPayload,
    EscrowProtocolRole,
    EscrowPublicKeysPayload,
    ParentMuSig2NoncePayload,
    ParentMuSig2PartialSignaturePayload,
    ParentPreparationPayload,
    ParentPrevoutPayload,
    TimeoutMuSig2NoncePayload,
    TimeoutMuSig2PartialSignaturePayload,
)
from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.service import WalletService
from pydantic import ValidationError

from maker.escrow_endpoint import EndpointParentSigner
from maker.escrow_keys import EscrowEndpointRole, WalletEscrowKeyDeriver
from maker.escrow_runtime import (
    ChannelInputEscrowRuntime,
    EndpointEscrowRuntimePolicy,
    EndpointEscrowSessionRecord,
    EndpointEscrowSessionStore,
)

EDGE_ID = "a1" * 32
FUNDING_TXID = "b2" * 32
MAKER_KEY = "02" + "11" * 32
PEER_KEY = "03" + "22" * 32


def _snapshot(*, local_claim: int = 70_000, remote_claim: int = 30_000) -> ChannelSnapshot:
    return ChannelSnapshot(
        session_id=EDGE_ID,
        channel_id="channel",
        funding_outpoint=EscrowOutpoint(txid=FUNDING_TXID, vout=1),
        funding_scriptpubkey="5120" + "44" * 32,
        local_funding_pubkey="02" + "55" * 32,
        remote_funding_pubkey="03" + "66" * 32,
        capacity_sats=100_000,
        maker_claim_sats=local_claim,
        peer_claim_sats=remote_claim,
        quiescent=True,
        active_htlcs=0,
    )


def _keys(role: EscrowProtocolRole) -> EscrowPublicKeysPayload:
    return EscrowPublicKeysPayload(
        role=role,
        escrow_pubkey=MAKER_KEY if role is EscrowProtocolRole.MAKER else PEER_KEY,
        timeout_destination_scriptpubkey="5120"
        + ("77" if role is EscrowProtocolRole.MAKER else "88") * 32,
        maker_claim_pubkey="02" + "99" * 32 if role is EscrowProtocolRole.MAKER else None,
    )


def _policy() -> EndpointEscrowRuntimePolicy:
    return EndpointEscrowRuntimePolicy(
        premium_sats=1_000,
        timeout_compensation_sats=500,
        escrow_value_sats=80_000,
        timeout_fee_sats=500,
        invoice_expiry_seconds=600,
        invoice_min_final_cltv_delta=18,
        peer_settle_delta_blocks=5,
        success_broadcast_delta_blocks=10,
        timeout_delta_blocks=30,
        success_confirmation_budget_blocks=5,
        peer_message_timeout_seconds=1,
    )


def _proposal() -> EscrowInvoiceProposalPayload:
    return EscrowInvoiceProposalPayload(
        role=EscrowProtocolRole.PEER,
        invoice="lnsb1runtime",
        payment_hash="33" * 32,
        payee_pubkey=PEER_KEY,
        payment_secret="44" * 32,
        amount_msat=31_000_000,
        expiry_unix=2_000_000_000,
        min_final_cltv_delta=18,
    )


def _terms(snapshot: ChannelSnapshot, authorizer: str) -> ChannelInputEscrowTerms:
    return ChannelInputEscrowTerms(
        snapshot=snapshot,
        authorizer_pubkey=authorizer,
        payment_hash="33" * 32,
        invoice="lnsb1runtime",
        invoice_payee_pubkey=PEER_KEY,
        invoice_payment_secret="44" * 32,
        invoice_amount_msat=31_000_000,
        invoice_expiry_unix=2_000_000_000,
        invoice_min_final_cltv_delta=18,
        invoice_verified=True,
        premium_sats=1_000,
        timeout_compensation_sats=500,
        escrow_value_sats=80_000,
        timeout_fee_sats=500,
        timeout_height=130,
        peer_settle_deadline=105,
        success_broadcast_deadline=110,
        parent_confirmation_depth=1,
        success_confirmation_depth=1,
        success_confirmation_budget_blocks=5,
        reorg_margin_blocks=0,
        safety_delta_blocks=1,
        preimage_budget_blocks=0,
    )


@dataclass(slots=True)
class _Wallet:
    master_key: HDKey
    wallet_fingerprint: str
    root_path: str = "m/84'/1'"


class _Lnd:
    def __init__(self) -> None:
        self.invoice_calls = 0
        self.prepare_calls = 0
        self.escrow_status = LndEscrowStatus(
            stage=LndEscrowStage.LOCKED, parent_txid=None, finalized=False
        )

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> ChannelSnapshot:
        return _snapshot()

    async def create_escrow_invoice(self, *_args: Any, **_kwargs: Any) -> LndEscrowInvoice:
        self.invoice_calls += 1
        return LndEscrowInvoice(
            invoice="lnsb1runtime",
            payment_hash="33" * 32,
            payee_pubkey=PEER_KEY,
            payment_secret="44" * 32,
            amount_msat=31_000_000,
            expiry_unix=2_000_000_000,
            min_final_cltv_delta=18,
        )

    async def decode_escrow_invoice(self, _invoice: str) -> LndEscrowInvoice:
        return await self.create_escrow_invoice()

    async def prepare_channel_input_parent(self, *_args: Any, **_kwargs: Any) -> str:
        self.prepare_calls += 1
        parent_tx_hex = cast(str, _args[2])
        from jmcore.bitcoin import get_txid

        return get_txid(parent_tx_hex)

    async def begin_channel_input_signing(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("parent signing is not needed by this test")

    async def sign_channel_input_parent(self, *_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("parent signing is not needed by this test")

    async def finalize_channel_input_parent(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("parent signing is not needed by this test")

    async def recover_channel_input_parent(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("parent recovery is not needed by this test")

    async def channel_input_escrow_status(self, *_args: Any, **_kwargs: Any) -> LndEscrowStatus:
        return self.escrow_status


class _Broker:
    def __init__(self) -> None:
        self.sent: list[object] = []
        self.completed: list[object] = []
        self.receipts: list[Any] = []

    def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    def binding(self, _edge_id: str) -> Any:
        return SimpleNamespace(channel_point=SimpleNamespace(txid=FUNDING_TXID, vout=1))

    def pending(self, _edge_id: str) -> tuple[Any, ...]:
        return tuple(self.receipts)

    async def send(self, _edge_id: str, payload: Any) -> Any:
        self.sent.append(payload)
        return SimpleNamespace(payload=payload)

    async def receive(self, _edge_id: str, _expected: Any, **_kwargs: Any) -> Any:
        if not self.receipts:
            raise AssertionError("unexpected broker receive")
        return self.receipts.pop(0)

    def complete(self, envelope: Any) -> None:
        self.completed.append(envelope)


class _Chain:
    def __init__(self) -> None:
        self.frozen: list[tuple[str, EscrowOutpoint]] = []
        self.calls: list[str] = []

    async def register_frozen_channel(self, session_id: str, outpoint: EscrowOutpoint) -> None:
        self.frozen.append((session_id, outpoint))

    async def broadcast_parent(self, request: Any) -> Any:
        self.calls.append("broadcast_parent")
        return request

    async def observe_escrow_chain(self, request: Any) -> Any:
        self.calls.append("observe")
        return request

    async def build_broadcast_success_spend(self, request: Any) -> Any:
        self.calls.append("success")
        return request

    async def broadcast_timeout_recovery(self, request: Any) -> Any:
        self.calls.append("timeout")
        return request

    async def fee_bump_success_spend(self, request: Any) -> Any:
        self.calls.append("bump")
        return request

    async def rebroadcast_recovery_artifacts(self, request: Any) -> Any:
        self.calls.append("recovery")
        return request


class _Payment:
    async def send_single_part_payment(self, request: Any) -> Any:
        return request

    async def observe_escrow_payment(self, request: Any) -> Any:
        return request


class _Height:
    async def block_height(self) -> int:
        return 100


def _runtime(tmp_path: Path) -> tuple[ChannelInputEscrowRuntime, _Broker, _Lnd, _Chain]:
    master = HDKey.from_seed(b"k" * 64)
    deriver = WalletEscrowKeyDeriver(
        cast(WalletService, _Wallet(master, master.derive("m/0").fingerprint.hex()))
    )
    broker = _Broker()
    lnd = _Lnd()
    chain = _Chain()
    runtime = ChannelInputEscrowRuntime(
        lnd=lnd,
        key_deriver=deriver,
        broker=broker,
        chain=chain,
        payment=_Payment(),
        height_source=_Height(),
        policy=_policy(),
        endpoint_store=EndpointEscrowSessionStore(tmp_path / "endpoint"),
    )
    return runtime, broker, lnd, chain


def test_endpoint_store_enforces_permissions_cas_and_model_consistency(tmp_path: Path) -> None:
    store = EndpointEscrowSessionStore(tmp_path / "endpoint")
    record = EndpointEscrowSessionRecord(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.MAKER,
        snapshot=_snapshot(),
        frozen_channel_registered=True,
        local_keys=_keys(EscrowProtocolRole.MAKER),
    )
    store.save(record)
    path = tmp_path / "endpoint" / f"{EDGE_ID}.json"
    assert (path.stat().st_mode & 0o777) == 0o600
    next_record = record.advance(remote_keys=_keys(EscrowProtocolRole.PEER))
    store.save(next_record, expected_revision=0)
    with pytest.raises(Exception, match="compare-and-swap"):
        store.save(next_record.advance(frozen_channel_registered=False), expected_revision=0)
    with pytest.raises(ValidationError, match="local public keys"):
        EndpointEscrowSessionRecord(
            edge_id=EDGE_ID,
            role=EscrowEndpointRole.MAKER,
            snapshot=_snapshot(),
            local_keys=_keys(EscrowProtocolRole.PEER),
        )


def test_canonical_peer_snapshot_swaps_claims_and_funding_pubkeys() -> None:
    local = _snapshot(local_claim=40_000, remote_claim=60_000)
    canonical = ChannelInputEscrowRuntime.canonical_snapshot(local, EscrowEndpointRole.PEER)

    assert canonical.maker_claim_sats == 60_000
    assert canonical.peer_claim_sats == 40_000
    assert canonical.local_funding_pubkey == local.remote_funding_pubkey
    assert canonical.remote_funding_pubkey == local.local_funding_pubkey


async def test_structural_adapter_delegation_and_capability_contract(tmp_path: Path) -> None:
    runtime, _broker, _lnd, chain = _runtime(tmp_path)
    assert runtime.channel_input_escrow_capabilities == frozenset()
    request = SimpleNamespace(value="request")

    assert await runtime.broadcast_parent(request) is request
    assert await runtime.observe_escrow_chain(request) is request
    assert await runtime.build_broadcast_success_spend(request) is request
    assert await runtime.broadcast_timeout_recovery(request) is request
    assert await runtime.fee_bump_success_spend(request) is request
    assert await runtime.rebroadcast_recovery_artifacts(request) is request
    assert await runtime.send_single_part_payment(request) is request
    assert await runtime.observe_escrow_payment(request) is request
    assert chain.calls == ["broadcast_parent", "observe", "success", "timeout", "bump", "recovery"]


async def test_maker_role_conflict_is_rejected_without_replacing_endpoint(tmp_path: Path) -> None:
    runtime, broker, _lnd, _chain = _runtime(tmp_path)
    await runtime.freeze_quiesce_channel(
        FreezeChannelRequest(session_id=EDGE_ID, channel_id="channel")
    )
    envelope = SimpleNamespace(edge_id=EDGE_ID, payload=_keys(EscrowProtocolRole.MAKER))

    with pytest.raises(Exception, match="role"):
        await runtime.handle_inbound(envelope)

    assert runtime._endpoint_store.load(EDGE_ID).role is EscrowEndpointRole.MAKER  # noqa: SLF001
    assert broker.completed == []


async def test_peer_record_replays_offer_and_terms_bind_maker_escrow_key(tmp_path: Path) -> None:
    runtime, broker, lnd, _chain = _runtime(tmp_path)
    peer_record = EndpointEscrowSessionRecord(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.PEER,
        snapshot=_snapshot(),
        frozen_channel_registered=True,
        local_keys=_keys(EscrowProtocolRole.PEER),
        remote_keys=_keys(EscrowProtocolRole.MAKER),
    )
    runtime._endpoint_store.save(peer_record)  # noqa: SLF001

    recovered = await runtime._ensure_peer_offer(peer_record)  # noqa: SLF001
    replayed = await runtime._ensure_peer_offer(recovered)  # noqa: SLF001
    terms = _terms(replayed.snapshot, MAKER_KEY)
    runtime._validate_peer_terms(replayed, terms)  # noqa: SLF001

    assert lnd.invoice_calls == 1
    assert broker.sent.count(replayed.local_keys) == 2
    assert broker.sent.count(replayed.invoice_proposal) == 2
    with pytest.raises(Exception, match="canonical"):
        runtime._validate_peer_terms(  # noqa: SLF001
            replayed,
            terms.model_copy(update={"authorizer_pubkey": replayed.snapshot.local_funding_pubkey}),
        )


async def test_maker_completes_inbound_only_after_persisting_remote_evidence(
    tmp_path: Path,
) -> None:
    runtime, broker, _lnd, _chain = _runtime(tmp_path)
    await runtime.freeze_quiesce_channel(
        FreezeChannelRequest(session_id=EDGE_ID, channel_id="channel")
    )
    keys_envelope = SimpleNamespace(payload=_keys(EscrowProtocolRole.PEER))
    invoice_envelope = SimpleNamespace(payload=_proposal())
    broker.receipts = [keys_envelope, invoice_envelope]

    def completed_after_persistence(envelope: Any) -> None:
        record = runtime._endpoint_store.load(EDGE_ID)  # noqa: SLF001
        if envelope is keys_envelope:
            assert record is not None and record.remote_keys == envelope.payload
        if envelope is invoice_envelope:
            assert record is not None and record.invoice_proposal == envelope.payload
        broker.completed.append(envelope)

    cast(Any, broker).complete = completed_after_persistence
    terms = await runtime.complete_maker_handshake(EDGE_ID)

    assert terms.authorizer_pubkey != terms.snapshot.local_funding_pubkey
    assert terms.authorizer_pubkey == runtime._endpoint_store.load(EDGE_ID).local_keys.escrow_pubkey  # noqa: SLF001


def _parent() -> ParentPreparationPayload:
    from jmcore.bitcoin import TxInput, TxOutput, get_txid, serialize_transaction

    parent_tx_hex = serialize_transaction(
        2,
        [TxInput.from_hex(FUNDING_TXID, vout=1, sequence=0xFFFFFFFD)],
        [TxOutput(value=80_000, script=bytes.fromhex("5120" + "aa" * 32))],
        0,
    ).hex()
    return ParentPreparationPayload(
        round_id="10" * 32,
        parent_tx_hex=parent_tx_hex,
        parent_txid=get_txid(parent_tx_hex),
        channel_input_index=0,
        ordered_prevouts=(
            ParentPrevoutPayload(
                value_sats=100_000,
                scriptpubkey=_snapshot().funding_scriptpubkey,
            ),
        ),
        escrow_output_index=0,
        escrow_value_sats=80_000,
        escrow_scriptpubkey="5120" + "aa" * 32,
    )


def _signing_record(*, include_remote_partial: bool = False) -> EndpointEscrowSessionRecord:
    parent = _parent()
    timeout_local_nonce = TimeoutMuSig2NoncePayload(
        round_id=parent.round_id,
        timeout_txid="20" * 32,
        timeout_sighash="30" * 32,
        public_nonce="41" * 66,
    )
    timeout_remote_nonce = timeout_local_nonce.model_copy(update={"public_nonce": "42" * 66})
    timeout_local_partial = TimeoutMuSig2PartialSignaturePayload(
        round_id=parent.round_id,
        timeout_txid="20" * 32,
        timeout_sighash="30" * 32,
        partial_signature="51" * 32,
    )
    timeout_remote_partial = timeout_local_partial.model_copy(
        update={"partial_signature": "52" * 32}
    )
    local_parent_nonce = b"l" * 66
    parent_round_id = EndpointParentSigner.maker_round_id(
        EDGE_ID, parent.parent_txid, local_parent_nonce
    )
    remote_parent_nonce = ParentMuSig2NoncePayload(
        round_id=parent_round_id,
        parent_txid=parent.parent_txid,
        public_nonce=(b"r" * 66).hex(),
    )
    return EndpointEscrowSessionRecord(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.MAKER,
        snapshot=_snapshot(),
        frozen_channel_registered=True,
        local_keys=_keys(EscrowProtocolRole.MAKER),
        remote_keys=_keys(EscrowProtocolRole.PEER),
        invoice_proposal=_proposal(),
        terms=_terms(_snapshot(), MAKER_KEY),
        parent=parent,
        timeout_local_nonce=timeout_local_nonce,
        timeout_remote_nonce=timeout_remote_nonce,
        timeout_local_partial=timeout_local_partial,
        timeout_remote_partial=timeout_remote_partial,
        timeout_evidence=EscrowTimeoutEvidence(
            timeout_tx_hex="00",
            timeout_txid="90" * 32,
            spent_escrow_outpoint=EscrowOutpoint(
                txid=parent.parent_txid, vout=parent.escrow_output_index
            ),
            locktime=130,
            maker_output_sats=48_000,
            peer_output_sats=31_500,
            fee_sats=500,
            fully_signed=True,
            signature_verified=True,
        ),
        parent_local_nonce=(
            ParentMuSig2NoncePayload(
                round_id=parent_round_id,
                parent_txid=parent.parent_txid,
                public_nonce=local_parent_nonce.hex(),
            )
            if include_remote_partial
            else None
        ),
        parent_remote_nonce=remote_parent_nonce,
        parent_local_partial=(
            ParentMuSig2PartialSignaturePayload(
                round_id=parent_round_id,
                parent_txid=parent.parent_txid,
                partial_signature=(b"s" * 32).hex(),
            )
            if include_remote_partial
            else None
        ),
        parent_remote_partial=(
            ParentMuSig2PartialSignaturePayload(
                round_id=parent_round_id,
                parent_txid=parent.parent_txid,
                partial_signature="80" * 32,
            )
            if include_remote_partial
            else None
        ),
    )


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        (
            "timeout_remote_nonce",
            TimeoutMuSig2NoncePayload(
                round_id="10" * 32,
                timeout_txid="20" * 32,
                timeout_sighash="30" * 32,
                public_nonce="40" * 66,
            ),
        ),
        (
            "timeout_remote_partial",
            TimeoutMuSig2PartialSignaturePayload(
                round_id="10" * 32,
                timeout_txid="20" * 32,
                timeout_sighash="30" * 32,
                partial_signature="50" * 32,
            ),
        ),
        (
            "parent_remote_nonce",
            ParentMuSig2NoncePayload(
                round_id="60" * 32,
                parent_txid=_parent().parent_txid,
                public_nonce="70" * 66,
            ),
        ),
        (
            "parent_remote_partial",
            ParentMuSig2PartialSignaturePayload(
                round_id="60" * 32,
                parent_txid=_parent().parent_txid,
                partial_signature="80" * 32,
            ),
        ),
    ],
)
async def test_restart_completes_receipt_already_persisted_before_crash(
    tmp_path: Path, field: str, payload: object
) -> None:
    runtime, broker, _lnd, _chain = _runtime(tmp_path)
    envelope = SimpleNamespace(payload=payload)
    broker.receipts = [envelope]
    parent = _parent()
    local_timeout_nonce = TimeoutMuSig2NoncePayload(
        round_id=parent.round_id,
        timeout_txid="20" * 32,
        timeout_sighash="30" * 32,
        public_nonce="41" * 66,
    )
    remote_timeout_nonce = (
        payload if field == "timeout_remote_nonce" else local_timeout_nonce.model_copy()
    )
    local_parent_nonce = ParentMuSig2NoncePayload(
        round_id="60" * 32,
        parent_txid=parent.parent_txid,
        public_nonce="71" * 66,
    )
    remote_parent_nonce = (
        payload if field == "parent_remote_nonce" else local_parent_nonce.model_copy()
    )
    record = EndpointEscrowSessionRecord(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.MAKER,
        snapshot=_snapshot(),
        frozen_channel_registered=True,
        local_keys=_keys(EscrowProtocolRole.MAKER),
        remote_keys=_keys(EscrowProtocolRole.PEER),
        invoice_proposal=_proposal(),
        terms=_terms(_snapshot(), MAKER_KEY),
        parent=parent,
        timeout_local_nonce=local_timeout_nonce,
        timeout_remote_nonce=remote_timeout_nonce,
        timeout_local_partial=(
            TimeoutMuSig2PartialSignaturePayload(
                round_id=parent.round_id,
                timeout_txid="20" * 32,
                timeout_sighash="30" * 32,
                partial_signature="51" * 32,
            )
            if field in {"timeout_remote_partial", "parent_remote_nonce", "parent_remote_partial"}
            else None
        ),
        timeout_remote_partial=(
            payload
            if field == "timeout_remote_partial"
            else (
                TimeoutMuSig2PartialSignaturePayload(
                    round_id=parent.round_id,
                    timeout_txid="20" * 32,
                    timeout_sighash="30" * 32,
                    partial_signature="52" * 32,
                )
                if field in {"parent_remote_nonce", "parent_remote_partial"}
                else None
            )
        ),
        timeout_evidence=(
            EscrowTimeoutEvidence(
                timeout_tx_hex="00",
                timeout_txid="90" * 32,
                spent_escrow_outpoint=EscrowOutpoint(
                    txid=parent.parent_txid, vout=parent.escrow_output_index
                ),
                locktime=130,
                maker_output_sats=48_000,
                peer_output_sats=31_500,
                fee_sats=500,
                fully_signed=True,
                signature_verified=True,
            )
            if field.startswith("parent_")
            else None
        ),
        parent_local_nonce=local_parent_nonce if field.startswith("parent_") else None,
        parent_remote_nonce=remote_parent_nonce if field.startswith("parent_") else None,
        parent_remote_partial=payload if field == "parent_remote_partial" else None,
    )
    runtime._endpoint_store.save(record)  # noqa: SLF001

    if field == "timeout_remote_nonce":
        recovered, recovered_payload = await runtime._ensure_timeout_remote_nonce(record)  # noqa: SLF001
    elif field == "timeout_remote_partial":
        recovered, recovered_payload = await runtime._ensure_timeout_remote_partial(record)  # noqa: SLF001
    elif field == "parent_remote_nonce":
        recovered, recovered_payload = await runtime._ensure_parent_remote_nonce(record)  # noqa: SLF001
    else:
        recovered, recovered_payload = await runtime._ensure_parent_remote_partial(record)  # noqa: SLF001

    assert recovered is record
    assert recovered_payload == payload
    assert broker.completed == [envelope]


def _signed_lnd_status(
    record: EndpointEscrowSessionRecord,
    *,
    stage: LndEscrowStage = LndEscrowStage.PARENT_SIGNED,
    active: bool = True,
) -> LndEscrowStatus:
    assert record.parent is not None
    local_nonce = b"l" * 66
    finalized_parent = None
    if stage is LndEscrowStage.FINALIZED:
        finalized_parent = LndFinalizedEscrowParent(
            tx_hex=record.parent.parent_tx_hex,
            txid=record.parent.parent_txid,
            channel_input_index=record.parent.channel_input_index,
            channel_input_witness=(b"f" * 64,),
        )
    return LndEscrowStatus(
        stage=stage,
        parent_txid=record.parent.parent_txid,
        finalized=stage is LndEscrowStage.FINALIZED,
        durable_local_public_nonce=local_nonce,
        durable_remote_public_nonce=b"r" * 66,
        durable_local_partial_signature=b"s" * 32,
        finalized_parent=finalized_parent,
        active_attempt=(
            LndEscrowSigningAttempt(attempt_id=b"a" * 32, public_nonce=local_nonce)
            if active
            else None
        ),
    )


async def test_reconcile_recovers_lnd_partial_written_before_endpoint_journal(
    tmp_path: Path,
) -> None:
    runtime, _broker, lnd, _chain = _runtime(tmp_path)
    record = _signing_record()
    runtime._endpoint_store.save(record)  # noqa: SLF001
    lnd.escrow_status = _signed_lnd_status(record)

    recovered = await runtime._reconcile_lnd(record)  # noqa: SLF001

    assert recovered.parent_local_nonce is not None
    assert recovered.parent_local_nonce.public_nonce == (b"l" * 66).hex()
    assert recovered.parent_local_partial is not None
    assert recovered.parent_local_partial.partial_signature == (b"s" * 32).hex()
    assert runtime._endpoint_store.load(EDGE_ID) == recovered  # noqa: SLF001


async def test_reconcile_recovers_finalized_witness_before_authorization_journal(
    tmp_path: Path,
) -> None:
    runtime, _broker, lnd, _chain = _runtime(tmp_path)
    record = _signing_record(include_remote_partial=True)
    runtime._endpoint_store.save(record)  # noqa: SLF001
    lnd.escrow_status = _signed_lnd_status(record, stage=LndEscrowStage.FINALIZED, active=False)

    recovered = await runtime._reconcile_lnd(record)  # noqa: SLF001

    assert recovered.authorization is not None
    assert recovered.authorization.parent_txid == record.parent.parent_txid
    assert recovered.authorization.channel_input_witness == (b"f" * 64,)


async def test_reconcile_reprepares_exact_parent_when_endpoint_is_ahead_of_lnd(
    tmp_path: Path,
) -> None:
    runtime, _broker, lnd, _chain = _runtime(tmp_path)
    record = _signing_record()

    recovered = await runtime._reconcile_lnd(record)  # noqa: SLF001

    assert recovered is record
    assert lnd.prepare_calls == 1


async def test_reconcile_rejects_conflicting_lnd_parent(tmp_path: Path) -> None:
    runtime, _broker, lnd, _chain = _runtime(tmp_path)
    record = _signing_record()
    lnd.escrow_status = LndEscrowStatus(
        stage=LndEscrowStage.PARENT_PREPARED,
        parent_txid="ff" * 32,
        finalized=False,
    )

    with pytest.raises(Exception, match="prepared parent conflicts"):
        await runtime._reconcile_lnd(record)  # noqa: SLF001


async def test_reconcile_retains_durable_partial_when_lnd_lost_signed_attempt(
    tmp_path: Path,
) -> None:
    runtime, _broker, lnd, _chain = _runtime(tmp_path)
    record = _signing_record()
    runtime._endpoint_store.save(record)  # noqa: SLF001
    lnd.escrow_status = _signed_lnd_status(record, active=False)

    recovered = await runtime._reconcile_lnd(record)  # noqa: SLF001

    persisted = runtime._endpoint_store.load(EDGE_ID)  # noqa: SLF001
    assert persisted is not None and persisted.parent_local_partial is not None
    assert recovered == persisted
