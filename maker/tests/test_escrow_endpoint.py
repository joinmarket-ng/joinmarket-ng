"""Tests for endpoint-local timeout signing using wallet-derived escrow keys."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from typing import cast

import pytest
from jmcore.bitcoin import get_txid, parse_transaction
from jmcore.taproot import TaprootTx, TxIn, TxOut
from jmswap.bitcoin_escrow import ChannelInputEscrowTree
from jmswap.channel_escrow import (
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowOutpoint,
    EscrowTimeoutEvidence,
)
from jmswap.errors import ChannelInputEscrowError
from jmswap.lnd import LndEscrowSigningAttempt, LndFinalizedEscrowParent
from jmswap.peer_transport import (
    ParentMuSig2PartialSignaturePayload,
    ParentPreparationPayload,
    ParentPrevoutPayload,
)
from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.service import WalletService

from maker.escrow_endpoint import (
    EndpointEscrowKeys,
    EndpointEscrowSigner,
    EndpointParentSigner,
    EndpointTimeoutSigningContext,
)
from maker.escrow_keys import EscrowEndpointRole, WalletEscrowKeyDeriver

EDGE_ID = "a1" * 32
FUNDING_TXID = "b2" * 32
ROUND_ID = "c3" * 32


@dataclass(slots=True)
class FakeWallet:
    """Minimal deterministic wallet value for endpoint signing tests."""

    master_key: HDKey
    wallet_fingerprint: str
    root_path: str = "m/84'/1'"


@dataclass(slots=True)
class FakeParentSigningBackend:
    """Minimal async LND boundary that records exact parent-signing requests."""

    nonces: tuple[bytes, ...]
    prepare_calls: list[tuple[str, EscrowOutpoint, str, list[tuple[int, bytes]], int]] = field(
        default_factory=list
    )
    begin_calls: list[tuple[str, EscrowOutpoint, bool]] = field(default_factory=list)
    sign_calls: list[tuple[bytes, bytes]] = field(default_factory=list)
    finalize_calls: list[tuple[bytes, bytes]] = field(default_factory=list)
    recover_calls: list[tuple[str, EscrowOutpoint, bytes]] = field(default_factory=list)
    allowed_remote_partials: set[bytes] = field(default_factory=set)
    finalized_override: LndFinalizedEscrowParent | None = None
    _parent_tx_hex: str | None = field(default=None, init=False)
    _channel_input_index: int | None = field(default=None, init=False)
    _attempt_count: int = field(default=0, init=False)

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: list[tuple[int, bytes]],
        channel_input_index: int,
    ) -> str:
        self.prepare_calls.append(
            (session_id, funding_outpoint, parent_tx_hex, input_utxos, channel_input_index)
        )
        self._parent_tx_hex = parent_tx_hex
        self._channel_input_index = channel_input_index
        return get_txid(parent_tx_hex)

    async def begin_channel_input_signing(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        *,
        restart_attempt: bool = False,
    ) -> LndEscrowSigningAttempt:
        self.begin_calls.append((session_id, funding_outpoint, restart_attempt))
        nonce_index = min(self._attempt_count, len(self.nonces) - 1)
        self._attempt_count += 1
        return LndEscrowSigningAttempt(
            attempt_id=self._attempt_count.to_bytes(32, "big"),
            public_nonce=self.nonces[nonce_index],
        )

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes:
        self.sign_calls.append((attempt.attempt_id, remote_public_nonce))
        return hashlib.sha256(attempt.attempt_id + remote_public_nonce).digest()

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        self.finalize_calls.append((attempt.attempt_id, remote_partial_signature))
        if remote_partial_signature not in self.allowed_remote_partials:
            raise ValueError("unexpected remote partial signature")
        if self.finalized_override is not None:
            return self.finalized_override
        if self._parent_tx_hex is None or self._channel_input_index is None:
            raise ValueError("parent was not prepared")
        parsed = parse_transaction(self._parent_tx_hex)
        finalized = TaprootTx(
            inputs=[
                TxIn(txid=value.txid, vout=value.vout, sequence=value.sequence)
                for value in parsed.inputs
            ],
            outputs=[
                TxOut(value=value.value, scriptpubkey=value.script) for value in parsed.outputs
            ],
            version=parsed.version,
            locktime=parsed.locktime,
            witnesses=[
                [b"w" * 64] if index == self._channel_input_index else []
                for index in range(len(parsed.inputs))
            ],
        )
        return LndFinalizedEscrowParent(
            tx_hex=finalized.serialize().hex(),
            txid=finalized.txid(),
            channel_input_index=self._channel_input_index,
            channel_input_witness=(b"w" * 64,),
        )

    async def recover_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        self.recover_calls.append((session_id, funding_outpoint, remote_partial_signature))
        return await self.finalize_channel_input_parent(
            session_id,
            funding_outpoint,
            LndEscrowSigningAttempt(attempt_id=b"\x00" * 32, public_nonce=self.nonces[0]),
            remote_partial_signature,
        )


def _wallet(seed_byte: int) -> FakeWallet:
    master_key = HDKey.from_seed(bytes([seed_byte]) * 64)
    return FakeWallet(
        master_key=master_key,
        wallet_fingerprint=master_key.derive("m/0").fingerprint.hex(),
    )


def _signer(seed_byte: int) -> EndpointEscrowSigner:
    wallet = cast(WalletService, _wallet(seed_byte))
    return EndpointEscrowSigner(WalletEscrowKeyDeriver(wallet))


def _terms() -> ChannelInputEscrowTerms:
    return ChannelInputEscrowTerms(
        snapshot=ChannelSnapshot(
            session_id=EDGE_ID,
            channel_id="test-channel",
            funding_outpoint=EscrowOutpoint(txid=FUNDING_TXID, vout=2),
            funding_scriptpubkey="5120" + "10" * 32,
            local_funding_pubkey="02" + "12" * 32,
            remote_funding_pubkey="03" + "13" * 32,
            capacity_sats=150_000,
            maker_claim_sats=80_000,
            peer_claim_sats=70_000,
            quiescent=True,
            active_htlcs=0,
        ),
        authorizer_pubkey="02" + "11" * 32,
        payment_hash=hashlib.sha256(b"endpoint timeout test").hexdigest(),
        invoice="lnbcrt1endpointtest",
        invoice_payee_pubkey="03" + "22" * 32,
        invoice_payment_secret="33" * 32,
        invoice_amount_msat=70_000_000,
        invoice_expiry_unix=2_000_000_000,
        invoice_min_final_cltv_delta=18,
        invoice_verified=True,
        premium_sats=0,
        timeout_compensation_sats=250,
        escrow_value_sats=100_000,
        timeout_fee_sats=200,
        timeout_height=850_000,
        peer_settle_deadline=849_000,
        success_broadcast_deadline=849_100,
        parent_confirmation_depth=1,
        success_confirmation_depth=1,
        success_confirmation_budget_blocks=100,
        reorg_margin_blocks=10,
        safety_delta_blocks=10,
        preimage_budget_blocks=0,
    )


def _parent(terms: ChannelInputEscrowTerms, escrow_script: bytes) -> ParentPreparationPayload:
    tx = TaprootTx(
        inputs=[TxIn(txid=FUNDING_TXID, vout=2, sequence=0xFFFFFFFD)],
        outputs=[TxOut(value=terms.escrow_value_sats, scriptpubkey=escrow_script)],
        version=2,
        witnesses=[[]],
    )
    tx_hex = tx.serialize().hex()
    return ParentPreparationPayload(
        round_id=ROUND_ID,
        parent_tx_hex=tx_hex,
        parent_txid=tx.txid(),
        channel_input_index=0,
        ordered_prevouts=(
            ParentPrevoutPayload(
                value_sats=terms.snapshot.capacity_sats,
                scriptpubkey="5120" + "44" * 32,
            ),
        ),
        escrow_output_index=0,
        escrow_value_sats=terms.escrow_value_sats,
        escrow_scriptpubkey=escrow_script.hex(),
    )


def _contexts() -> tuple[
    EndpointEscrowSigner,
    EndpointEscrowSigner,
    EndpointEscrowKeys,
    EndpointEscrowKeys,
    EndpointTimeoutSigningContext,
    EndpointTimeoutSigningContext,
]:
    maker = _signer(1)
    peer = _signer(2)
    maker_local = maker.derive_keys(EDGE_ID.upper(), EscrowEndpointRole.MAKER)
    peer_local = peer.derive_keys(EDGE_ID, EscrowEndpointRole.PEER)
    maker_payload = maker_local.public_payload()
    peer_payload = peer_local.public_payload()
    terms = _terms()
    tree = ChannelInputEscrowTree(
        maker_escrow_pubkey=bytes.fromhex(maker_payload.escrow_pubkey),
        peer_escrow_pubkey=bytes.fromhex(peer_payload.escrow_pubkey),
        maker_claim_pubkey=bytes.fromhex(maker_payload.maker_claim_pubkey or ""),
        payment_hash=bytes.fromhex(terms.payment_hash),
    )
    parent = _parent(terms, tree.funding_output_script())
    maker_context = maker.build_timeout_context(
        local_keys=maker_local,
        maker_keys=maker_payload,
        peer_keys=peer_payload,
        terms=terms,
        parent=parent,
    )
    peer_context = peer.build_timeout_context(
        local_keys=peer_local,
        maker_keys=maker_payload,
        peer_keys=peer_payload,
        terms=terms,
        parent=parent,
    )
    return maker, peer, maker_local, peer_local, maker_context, peer_context


def _timeout_evidence(parent: ParentPreparationPayload) -> EscrowTimeoutEvidence:
    return EscrowTimeoutEvidence(
        timeout_tx_hex=parent.parent_tx_hex,
        timeout_txid="d4" * 32,
        spent_escrow_outpoint=EscrowOutpoint(
            txid=parent.parent_txid,
            vout=parent.escrow_output_index,
        ),
        locktime=850_000,
        maker_output_sats=29_550,
        peer_output_sats=70_250,
        fee_sats=200,
        fully_signed=True,
        signature_verified=True,
    )


def _parent_signing_setup() -> tuple[
    EndpointParentSigner,
    EndpointParentSigner,
    FakeParentSigningBackend,
    FakeParentSigningBackend,
    EscrowOutpoint,
    ParentPreparationPayload,
    EscrowTimeoutEvidence,
]:
    _, _, _, _, maker_context, _ = _contexts()
    parent = _parent(_terms(), maker_context.tree.funding_output_script())
    funding_outpoint = _terms().snapshot.funding_outpoint
    maker_backend = FakeParentSigningBackend((b"m" * 66, b"M" * 66))
    peer_backend = FakeParentSigningBackend((b"p" * 66, b"P" * 66))
    return (
        EndpointParentSigner(maker_backend),
        EndpointParentSigner(peer_backend),
        maker_backend,
        peer_backend,
        funding_outpoint,
        parent,
        _timeout_evidence(parent),
    )


def test_endpoint_keys_are_role_bound_and_public_only() -> None:
    maker, peer, maker_local, peer_local, _, _ = _contexts()

    assert maker_local.edge_id == EDGE_ID
    assert maker_local.claim is not None
    assert peer_local.claim is None
    assert maker_local.public_payload().role.value == "maker"
    assert peer_local.public_payload().role.value == "peer"
    assert maker_local.public_payload().timeout_destination_scriptpubkey.startswith("5120")
    metadata = [
        maker_local.escrow.metadata.model_dump_json(),
        maker_local.timeout_destination.metadata.model_dump_json(),
        maker_local.nonce_seed.metadata.model_dump_json(),
        maker_local.claim.metadata.model_dump_json(),
        peer_local.escrow.metadata.model_dump_json(),
    ]
    assert all("private" not in value and "randomness" not in value for value in metadata)
    with pytest.raises(ChannelInputEscrowError):
        maker.derive_keys(EDGE_ID, "maker")  # type: ignore[arg-type]
    with pytest.raises(ChannelInputEscrowError):
        peer.derive_keys("00" * 31, EscrowEndpointRole.PEER)


def test_endpoints_build_sign_and_finalize_identical_verified_timeout() -> None:
    maker, peer, maker_local, _, maker_context, peer_context = _contexts()
    maker_nonce = maker.nonce_payload(maker_context)
    peer_nonce = peer.nonce_payload(peer_context)

    assert maker_context.tree == peer_context.tree
    assert maker_context.unsigned_spend.tx.txid() == peer_context.unsigned_spend.tx.txid()
    assert maker_context.unsigned_spend.sighash == peer_context.unsigned_spend.sighash
    maker_partial = maker.sign_timeout(maker_context, peer_nonce)
    peer_partial = peer.sign_timeout(peer_context, maker_nonce)
    maker_evidence = maker.finalize_timeout(maker_context, peer_nonce, maker_partial, peer_partial)
    peer_evidence = peer.finalize_timeout(peer_context, maker_nonce, peer_partial, maker_partial)

    assert maker_evidence == peer_evidence
    assert maker_evidence.fully_signed and maker_evidence.signature_verified
    assert maker_evidence.maker_output_sats == 29_550
    assert maker_evidence.peer_output_sats == 70_250
    assert maker_evidence.fee_sats == 200
    repeated = maker.finalize_timeout(maker_context, peer_nonce, maker_partial, peer_partial)
    assert repeated == maker_evidence
    private_key_hex = maker_local.escrow.private_key_bytes().hex()
    assert private_key_hex not in repr(maker_local)
    assert private_key_hex not in repr(maker_context)
    assert str(maker_context.local_secnonce.k1) not in repr(maker_context)
    assert str(maker_context.local_secnonce.k2) not in repr(maker_context)


def test_restored_endpoint_repeats_exact_nonce_and_partial() -> None:
    maker, peer, _, _, maker_context, peer_context = _contexts()
    maker_nonce = maker.nonce_payload(maker_context)
    peer_nonce = peer.nonce_payload(peer_context)
    original_partial = maker.sign_timeout(maker_context, peer_nonce)
    restored = _signer(1)
    restored_local = restored.derive_keys(EDGE_ID, EscrowEndpointRole.MAKER)
    restored_context = restored.build_timeout_context(
        local_keys=restored_local,
        maker_keys=maker_context.maker_keys,
        peer_keys=maker_context.peer_keys,
        terms=_terms(),
        parent=_parent(_terms(), maker_context.tree.funding_output_script()),
    )

    assert restored.nonce_payload(restored_context) == maker_nonce
    assert restored.sign_timeout(restored_context, peer_nonce) == original_partial


def test_endpoint_rejects_role_parent_and_transcript_confusion() -> None:
    maker, peer, maker_local, peer_local, maker_context, peer_context = _contexts()
    maker_nonce = maker.nonce_payload(maker_context)
    peer_nonce = peer.nonce_payload(peer_context)
    maker_partial = maker.sign_timeout(maker_context, peer_nonce)
    peer_partial = peer.sign_timeout(peer_context, maker_nonce)

    with pytest.raises(ChannelInputEscrowError):
        maker.build_timeout_context(
            local_keys=maker_local,
            maker_keys=peer_local.public_payload(),
            peer_keys=peer_local.public_payload(),
            terms=_terms(),
            parent=_parent(_terms(), maker_context.tree.funding_output_script()),
        )
    with pytest.raises(ChannelInputEscrowError):
        maker.build_timeout_context(
            local_keys=maker_local,
            maker_keys=maker_context.maker_keys,
            peer_keys=maker_context.peer_keys,
            terms=_terms().model_copy(update={"escrow_value_sats": 100_001}),
            parent=_parent(_terms(), maker_context.tree.funding_output_script()),
        )
    with pytest.raises(ChannelInputEscrowError):
        maker.build_timeout_context(
            local_keys=maker_local,
            maker_keys=maker_context.maker_keys,
            peer_keys=maker_context.peer_keys,
            terms=_terms(),
            parent=_parent(_terms(), maker_context.tree.funding_output_script()).model_copy(
                update={"escrow_value_sats": 100_001}
            ),
        )
    with pytest.raises(ChannelInputEscrowError):
        maker.sign_timeout(maker_context, peer_nonce.model_copy(update={"round_id": "00" * 32}))
    with pytest.raises(ChannelInputEscrowError):
        maker.sign_timeout(
            maker_context, peer_nonce.model_copy(update={"timeout_sighash": "00" * 32})
        )
    with pytest.raises(ChannelInputEscrowError):
        maker.finalize_timeout(
            maker_context,
            peer_nonce,
            maker_partial.model_copy(update={"partial_signature": "00" * 32}),
            peer_partial,
        )


async def test_parent_signing_coordinates_both_endpoints_without_exposing_attempt() -> None:
    (
        maker,
        peer,
        maker_backend,
        peer_backend,
        funding_outpoint,
        parent,
        timeout_evidence,
    ) = _parent_signing_setup()

    assert (
        await maker.prepare_parent(EDGE_ID.upper(), funding_outpoint, parent) == parent.parent_txid
    )
    assert await peer.prepare_parent(EDGE_ID, funding_outpoint, parent) == parent.parent_txid
    expected_prevouts = [(150_000, bytes.fromhex("5120" + "44" * 32))]
    assert maker_backend.prepare_calls[0] == (
        EDGE_ID,
        funding_outpoint,
        parent.parent_tx_hex,
        expected_prevouts,
        0,
    )
    assert peer_backend.prepare_calls[0][3] == expected_prevouts

    maker_context = await maker.begin_signing(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.MAKER,
        funding_outpoint=funding_outpoint,
        parent=parent,
        timeout_evidence=timeout_evidence,
    )
    maker_nonce = maker.nonce_payload(maker_context)
    peer_context = await peer.begin_signing(
        edge_id=EDGE_ID.upper(),
        role=EscrowEndpointRole.PEER,
        funding_outpoint=funding_outpoint,
        parent=parent,
        timeout_evidence=timeout_evidence,
        peer_round_id=maker_nonce.round_id,
    )
    peer_nonce = peer.nonce_payload(peer_context)

    assert maker_context.round_id == peer_context.round_id
    assert maker_nonce.parent_txid == parent.parent_txid
    assert peer_nonce.parent_txid == parent.parent_txid
    assert "attempt_id" not in repr(maker_context)
    assert "attempt_id" not in repr(peer_context)
    assert "attempt_id" not in maker_nonce.model_dump_json()
    assert "attempt_id" not in peer_nonce.model_dump_json()

    maker_partial = await maker.sign_parent(maker_context, peer_nonce)
    peer_partial = await peer.sign_parent(peer_context, maker_nonce)
    assert "attempt_id" not in maker_partial.model_dump_json()
    assert "attempt_id" not in peer_partial.model_dump_json()
    maker_backend.allowed_remote_partials.add(bytes.fromhex(peer_partial.partial_signature))
    peer_backend.allowed_remote_partials.add(bytes.fromhex(maker_partial.partial_signature))

    maker_artifact = await maker.finalize_parent(maker_context, peer_partial)
    peer_artifact = await peer.finalize_parent(peer_context, maker_partial)

    assert (
        maker_artifact.channel_input_witness == peer_artifact.channel_input_witness == (b"w" * 64,)
    )
    assert maker_artifact.txid == peer_artifact.txid == parent.parent_txid
    assert maker_backend.sign_calls[0][0] not in maker_nonce.model_dump_json().encode()
    assert peer_backend.finalize_calls[0][0] not in peer_partial.model_dump_json().encode()


async def test_parent_signing_rejects_timeout_and_transcript_confusion() -> None:
    (
        maker,
        peer,
        maker_backend,
        peer_backend,
        funding_outpoint,
        parent,
        timeout_evidence,
    ) = _parent_signing_setup()

    unverified_values = timeout_evidence.model_dump()
    unverified_values["signature_verified"] = False
    unverified_timeout = EscrowTimeoutEvidence.model_construct(**unverified_values)
    mismatched_timeout = timeout_evidence.model_copy(
        update={"spent_escrow_outpoint": EscrowOutpoint(txid="ee" * 32, vout=0)}
    )
    with pytest.raises(ChannelInputEscrowError):
        await maker.begin_signing(
            edge_id=EDGE_ID,
            role=EscrowEndpointRole.MAKER,
            funding_outpoint=funding_outpoint,
            parent=parent,
            timeout_evidence=unverified_timeout,
        )
    with pytest.raises(ChannelInputEscrowError):
        await maker.begin_signing(
            edge_id=EDGE_ID,
            role=EscrowEndpointRole.MAKER,
            funding_outpoint=funding_outpoint,
            parent=parent,
            timeout_evidence=mismatched_timeout,
        )
    assert not maker_backend.begin_calls

    with pytest.raises(ChannelInputEscrowError):
        await maker.begin_signing(
            edge_id=EDGE_ID,
            role=EscrowEndpointRole.MAKER,
            funding_outpoint=funding_outpoint,
            parent=parent,
            timeout_evidence=timeout_evidence,
            peer_round_id=ROUND_ID,
        )
    with pytest.raises(ChannelInputEscrowError):
        await peer.begin_signing(
            edge_id=EDGE_ID,
            role=EscrowEndpointRole.PEER,
            funding_outpoint=funding_outpoint,
            parent=parent,
            timeout_evidence=timeout_evidence,
        )
    begin_call_count = len(peer_backend.begin_calls)
    with pytest.raises(ChannelInputEscrowError):
        await peer.begin_signing(
            edge_id=EDGE_ID,
            role=EscrowEndpointRole.PEER,
            funding_outpoint=funding_outpoint,
            parent=parent,
            timeout_evidence=timeout_evidence,
            peer_round_id="00" * 31,
        )
    assert len(peer_backend.begin_calls) == begin_call_count

    wrong_funding = EscrowOutpoint(txid="ff" * 32, vout=funding_outpoint.vout)
    with pytest.raises(ChannelInputEscrowError, match="funding outpoint"):
        await maker.prepare_parent(EDGE_ID, wrong_funding, parent)
    assert not maker_backend.prepare_calls

    await maker.prepare_parent(EDGE_ID, funding_outpoint, parent)
    await peer.prepare_parent(EDGE_ID, funding_outpoint, parent)
    maker_context = await maker.begin_signing(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.MAKER,
        funding_outpoint=funding_outpoint,
        parent=parent,
        timeout_evidence=timeout_evidence,
    )
    peer_context = await peer.begin_signing(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.PEER,
        funding_outpoint=funding_outpoint,
        parent=parent,
        timeout_evidence=timeout_evidence,
        peer_round_id=maker_context.round_id,
    )
    maker_nonce = maker.nonce_payload(maker_context)
    peer_nonce = peer.nonce_payload(peer_context)
    with pytest.raises(ChannelInputEscrowError):
        await maker.sign_parent(
            maker_context, peer_nonce.model_copy(update={"round_id": "00" * 32})
        )
    with pytest.raises(ChannelInputEscrowError):
        await maker.sign_parent(
            maker_context, peer_nonce.model_copy(update={"parent_txid": "00" * 32})
        )
    with pytest.raises(ChannelInputEscrowError):
        await maker.sign_parent(maker_context, maker_nonce)

    maker_partial = await maker.sign_parent(maker_context, peer_nonce)
    peer_partial = await peer.sign_parent(peer_context, maker_nonce)
    maker_backend.allowed_remote_partials.add(bytes.fromhex(peer_partial.partial_signature))
    peer_backend.allowed_remote_partials.add(bytes.fromhex(maker_partial.partial_signature))
    with pytest.raises(ChannelInputEscrowError):
        await maker.finalize_parent(
            maker_context, peer_partial.model_copy(update={"round_id": "00" * 32})
        )
    with pytest.raises(ChannelInputEscrowError):
        await maker.finalize_parent(
            maker_context, peer_partial.model_copy(update={"parent_txid": "00" * 32})
        )
    with pytest.raises(ChannelInputEscrowError):
        await maker.finalize_parent(
            maker_context, peer_partial.model_copy(update={"partial_signature": "00" * 32})
        )

    artifact = await maker.finalize_parent(maker_context, peer_partial)
    for invalid_artifact in (
        replace(artifact, txid="00" * 32),
        replace(artifact, channel_input_index=1),
        replace(artifact, channel_input_witness=(b"w" * 63,)),
        replace(artifact, channel_input_witness=(b"x" * 64,)),
        replace(artifact, tx_hex="00"),
    ):
        maker_backend.finalized_override = invalid_artifact
        with pytest.raises(ChannelInputEscrowError):
            await maker.finalize_parent(maker_context, peer_partial)


async def test_parent_signing_restart_changes_maker_round_and_peer_adopts_it() -> None:
    (
        maker,
        peer,
        _,
        peer_backend,
        funding_outpoint,
        parent,
        timeout_evidence,
    ) = _parent_signing_setup()

    initial_maker_context = await maker.begin_signing(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.MAKER,
        funding_outpoint=funding_outpoint,
        parent=parent,
        timeout_evidence=timeout_evidence,
    )
    restarted_maker_context = await maker.begin_signing(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.MAKER,
        funding_outpoint=funding_outpoint,
        parent=parent,
        timeout_evidence=timeout_evidence,
        restart_attempt=True,
    )
    restarted_peer_context = await peer.begin_signing(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.PEER,
        funding_outpoint=funding_outpoint,
        parent=parent,
        timeout_evidence=timeout_evidence,
        peer_round_id=restarted_maker_context.round_id,
        restart_attempt=True,
    )

    assert initial_maker_context.round_id != restarted_maker_context.round_id
    assert restarted_peer_context.round_id == restarted_maker_context.round_id
    assert peer_backend.begin_calls == [(EDGE_ID, funding_outpoint, True)]


async def test_parent_signing_recovers_from_durable_lnd_transcript() -> None:
    (
        maker,
        _,
        maker_backend,
        _,
        funding_outpoint,
        parent,
        timeout_evidence,
    ) = _parent_signing_setup()
    await maker.prepare_parent(EDGE_ID, funding_outpoint, parent)
    remote_partial = ParentMuSig2PartialSignaturePayload(
        round_id=ROUND_ID,
        parent_txid=parent.parent_txid,
        partial_signature=(b"r" * 32).hex(),
    )
    maker_backend.allowed_remote_partials.add(b"r" * 32)

    artifact = await maker.recover_parent(
        edge_id=EDGE_ID,
        funding_outpoint=funding_outpoint,
        parent=parent,
        timeout_evidence=timeout_evidence,
        remote_partial_payload=remote_partial,
        round_id=ROUND_ID,
    )

    assert artifact.txid == parent.parent_txid
    assert maker_backend.recover_calls == [(EDGE_ID, funding_outpoint, b"r" * 32)]
    with pytest.raises(ChannelInputEscrowError, match="recovery transcript"):
        await maker.recover_parent(
            edge_id=EDGE_ID,
            funding_outpoint=funding_outpoint,
            parent=parent,
            timeout_evidence=timeout_evidence,
            remote_partial_payload=remote_partial.model_copy(update={"round_id": "00" * 32}),
            round_id=ROUND_ID,
        )
