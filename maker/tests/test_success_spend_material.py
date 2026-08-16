"""Focused tests for maker-local success-spend material restoration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from jmswap.channel_escrow import ChannelInputEscrowTerms, ChannelSnapshot, EscrowOutpoint
from jmswap.errors import ChannelInputEscrowError
from jmswap.peer_transport import (
    EscrowInvoiceProposalPayload,
    EscrowProtocolRole,
    EscrowPublicKeysPayload,
)
from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.service import WalletService

from maker.escrow_endpoint import EndpointEscrowSigner
from maker.escrow_keys import EscrowEndpointRole, WalletEscrowKeyDeriver
from maker.escrow_runtime import EndpointEscrowSessionRecord, EndpointEscrowSessionStore
from maker.success_spend_material import MakerSuccessSpendMaterialResolver

EDGE_ID = "a1" * 32
FUNDING_TXID = "b2" * 32
PAYMENT_HASH = "c3" * 32


@dataclass(slots=True)
class _Wallet:
    master_key: HDKey
    wallet_fingerprint: str
    root_path: str = "m/84'/1'"


def _deriver(seed: bytes) -> WalletEscrowKeyDeriver:
    master = HDKey.from_seed(seed)
    return WalletEscrowKeyDeriver(
        cast(WalletService, _Wallet(master, master.derive("m/0").fingerprint.hex()))
    )


def _snapshot() -> ChannelSnapshot:
    return ChannelSnapshot(
        session_id=EDGE_ID,
        channel_id="channel",
        funding_outpoint=EscrowOutpoint(txid=FUNDING_TXID, vout=1),
        funding_scriptpubkey="5120" + "44" * 32,
        local_funding_pubkey="02" + "55" * 32,
        remote_funding_pubkey="03" + "66" * 32,
        capacity_sats=100_000,
        maker_claim_sats=70_000,
        peer_claim_sats=30_000,
        quiescent=True,
        active_htlcs=0,
    )


def _proposal(peer_keys: EscrowPublicKeysPayload) -> EscrowInvoiceProposalPayload:
    return EscrowInvoiceProposalPayload(
        role=EscrowProtocolRole.PEER,
        invoice="lnsb1successmaterial",
        payment_hash=PAYMENT_HASH,
        payee_pubkey=peer_keys.escrow_pubkey,
        payment_secret="d4" * 32,
        amount_msat=31_000_000,
        expiry_unix=2_000_000_000,
        min_final_cltv_delta=18,
    )


def _terms(
    maker_keys: EscrowPublicKeysPayload, peer_keys: EscrowPublicKeysPayload
) -> ChannelInputEscrowTerms:
    return ChannelInputEscrowTerms(
        snapshot=_snapshot(),
        authorizer_pubkey=maker_keys.escrow_pubkey,
        payment_hash=PAYMENT_HASH,
        invoice="lnsb1successmaterial",
        invoice_payee_pubkey=peer_keys.escrow_pubkey,
        invoice_payment_secret="d4" * 32,
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


def _maker_record(
    maker_deriver: WalletEscrowKeyDeriver,
    peer_deriver: WalletEscrowKeyDeriver,
) -> EndpointEscrowSessionRecord:
    maker_keys = (
        EndpointEscrowSigner(maker_deriver)
        .derive_keys(EDGE_ID, EscrowEndpointRole.MAKER)
        .public_payload()
    )
    peer_keys = (
        EndpointEscrowSigner(peer_deriver)
        .derive_keys(EDGE_ID, EscrowEndpointRole.PEER)
        .public_payload()
    )
    return EndpointEscrowSessionRecord(
        edge_id=EDGE_ID,
        role=EscrowEndpointRole.MAKER,
        snapshot=_snapshot(),
        local_keys=maker_keys,
        remote_keys=peer_keys,
        invoice_proposal=_proposal(peer_keys),
        terms=_terms(maker_keys, peer_keys),
    )


async def test_resolves_restart_stable_maker_success_spend_material(tmp_path: Path) -> None:
    maker_deriver = _deriver(b"m" * 64)
    peer_deriver = _deriver(b"p" * 64)
    store = EndpointEscrowSessionStore(tmp_path / "endpoint")
    record = _maker_record(maker_deriver, peer_deriver)
    store.save(record)
    assert record.remote_keys is not None
    assert record.terms is not None

    first = await MakerSuccessSpendMaterialResolver(store, maker_deriver).resolve_success_material(
        EDGE_ID
    )
    restarted = await MakerSuccessSpendMaterialResolver(
        store, _deriver(b"m" * 64)
    ).resolve_success_material(EDGE_ID)

    assert restarted == first
    assert first.tree.maker_escrow_pubkey == bytes.fromhex(record.local_keys.escrow_pubkey)
    assert first.tree.peer_escrow_pubkey == bytes.fromhex(record.remote_keys.escrow_pubkey)
    assert first.tree.maker_claim_pubkey == bytes.fromhex(
        record.local_keys.maker_claim_pubkey or ""
    )
    assert first.tree.payment_hash == bytes.fromhex(record.terms.payment_hash)
    assert first.maker_output_script == bytes.fromhex(
        record.local_keys.timeout_destination_scriptpubkey
    )
    assert first.maker_output_is_controlled is True


async def test_rejects_unknown_session(tmp_path: Path) -> None:
    resolver = MakerSuccessSpendMaterialResolver(
        EndpointEscrowSessionStore(tmp_path / "endpoint"), _deriver(b"m" * 64)
    )

    with pytest.raises(ChannelInputEscrowError, match="unable to resolve"):
        await resolver.resolve_success_material(EDGE_ID)


async def test_rejects_peer_owned_endpoint_record(tmp_path: Path) -> None:
    maker_deriver = _deriver(b"m" * 64)
    peer_deriver = _deriver(b"p" * 64)
    maker_keys = (
        EndpointEscrowSigner(maker_deriver)
        .derive_keys(EDGE_ID, EscrowEndpointRole.MAKER)
        .public_payload()
    )
    peer_keys = (
        EndpointEscrowSigner(peer_deriver)
        .derive_keys(EDGE_ID, EscrowEndpointRole.PEER)
        .public_payload()
    )
    store = EndpointEscrowSessionStore(tmp_path / "endpoint")
    store.save(
        EndpointEscrowSessionRecord(
            edge_id=EDGE_ID,
            role=EscrowEndpointRole.PEER,
            snapshot=_snapshot(),
            local_keys=peer_keys,
            remote_keys=maker_keys,
        )
    )
    resolver = MakerSuccessSpendMaterialResolver(store, maker_deriver)

    with pytest.raises(ChannelInputEscrowError, match="unable to resolve"):
        await resolver.resolve_success_material(EDGE_ID)


@pytest.mark.parametrize("missing", ["terms", "remote_keys"])
async def test_rejects_maker_record_missing_durable_terms_or_remote_keys(
    tmp_path: Path, missing: str
) -> None:
    maker_deriver = _deriver(b"m" * 64)
    peer_deriver = _deriver(b"p" * 64)
    record = _maker_record(maker_deriver, peer_deriver)
    updates: dict[str, object] = {"terms": None}
    if missing == "remote_keys":
        updates.update({"remote_keys": None, "invoice_proposal": None})
    record = record.model_copy(update=updates)
    store = EndpointEscrowSessionStore(tmp_path / "endpoint")
    store.save(record)
    resolver = MakerSuccessSpendMaterialResolver(store, maker_deriver)

    with pytest.raises(ChannelInputEscrowError, match="unable to resolve"):
        await resolver.resolve_success_material(EDGE_ID)


async def test_rejects_durable_local_key_payload_mismatch(tmp_path: Path) -> None:
    maker_deriver = _deriver(b"m" * 64)
    peer_deriver = _deriver(b"p" * 64)
    mismatched_maker_keys = (
        EndpointEscrowSigner(_deriver(b"x" * 64))
        .derive_keys(EDGE_ID, EscrowEndpointRole.MAKER)
        .public_payload()
    )
    base_record = _maker_record(maker_deriver, peer_deriver)
    assert base_record.remote_keys is not None
    record = base_record.model_copy(
        update={
            "local_keys": mismatched_maker_keys,
            "terms": _terms(mismatched_maker_keys, base_record.remote_keys),
        }
    )
    store = EndpointEscrowSessionStore(tmp_path / "endpoint")
    store.save(record)
    resolver = MakerSuccessSpendMaterialResolver(store, maker_deriver)

    with pytest.raises(ChannelInputEscrowError, match="unable to resolve"):
        await resolver.resolve_success_material(EDGE_ID)
