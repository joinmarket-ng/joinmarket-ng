"""Unit tests for the bounded Bitcoin Core escrow chain adapter."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import create_p2tr_scriptpubkey, get_txid
from jmcore.taproot import TaprootTx, TxIn, TxOut

from jmswap.bitcoin_core_escrow import (
    BitcoinCoreEscrowChainAdapter,
    BitcoinCoreRpcError,
    SuccessSpendMaterial,
)
from jmswap.bitcoin_escrow import ChannelInputEscrowTree, build_success_spend
from jmswap.channel_escrow import (
    ChainObservationRequest,
    ChannelInputEscrowRecord,
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowChainObservation,
    EscrowLifecycleState,
    EscrowOutpoint,
    EscrowParentBroadcast,
    EscrowParentEvidence,
    EscrowParentPrevout,
    EscrowRouteEvidence,
    EscrowTimeoutEvidence,
    FinalizedEscrowParent,
    ParentAuthorization,
    ParentBroadcastRequest,
    PreparedChannelInputEscrow,
    RecoveryRequest,
    SinglePartPaymentRequest,
    SinglePartPaymentResult,
    SuccessSpendRequest,
)
from jmswap.errors import ChainError, ChannelInputEscrowError
from jmswap.lightning import PaymentStatus
from jmswap.spend import SwapOutpoint

_PREIMAGE = bytes.fromhex("11" * 32)
_PAYMENT_HASH = hashlib.sha256(_PREIMAGE).hexdigest()


@dataclass
class _MaterialResolver:
    material: SuccessSpendMaterial

    async def resolve_success_material(self, session_id: str) -> SuccessSpendMaterial:
        assert session_id == "session-1"
        return self.material


class _Rpc:
    def __init__(self, height: int = 950) -> None:
        self.height = height
        self.calls: list[tuple[str, list[Any]]] = []
        self.transactions: dict[str, dict[str, Any]] = {}
        self.blocks: dict[int, dict[str, Any]] = {}
        self.header_confirmations: dict[str, int] = {}
        self.send_error: BitcoinCoreRpcError | None = None
        self.policy_allowed = True

    async def call(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, params))
        if method == "getblockcount":
            return self.height
        if method == "estimatesmartfee":
            return {"feerate": 0.00001}
        if method == "getmempoolinfo":
            return {"mempoolminfee": 0.00001, "incrementalrelayfee": 0.00001}
        if method == "testmempoolaccept":
            return [{"allowed": self.policy_allowed}]
        if method == "sendrawtransaction":
            if self.send_error is not None:
                raise self.send_error
            raw = params[0]
            txid = get_txid(raw)
            self.transactions[txid] = {"hex": raw}
            return txid
        if method == "getrawtransaction":
            txid = params[0]
            if txid not in self.transactions:
                raise BitcoinCoreRpcError(-5)
            return self.transactions[txid]
        if method == "getblockheader":
            return {"confirmations": self.header_confirmations.get(params[0], 2)}
        if method == "getblockhash":
            return f"block-{params[0]}"
        if method == "getblock":
            return self.blocks[int(params[0].split("-")[1])]
        raise AssertionError(f"unexpected RPC {method}")


def _artifacts() -> tuple[PreparedChannelInputEscrow, ParentBroadcastRequest, SuccessSpendMaterial]:
    maker_escrow = PrivateKey((1).to_bytes(32, "big"))
    peer_escrow = PrivateKey((2).to_bytes(32, "big"))
    maker_claim = PrivateKey((3).to_bytes(32, "big"))
    tree = ChannelInputEscrowTree(
        maker_escrow.public_key.format(compressed=True),
        peer_escrow.public_key.format(compressed=True),
        maker_claim.public_key.format(compressed=True),
        bytes.fromhex(_PAYMENT_HASH),
    )
    parent = TaprootTx(
        inputs=[TxIn("aa" * 32, 0, 0xFFFFFFFD)],
        outputs=[TxOut(80_000, tree.funding_output_script())],
        witnesses=[[b"\x01"]],
    )
    parent_hex = parent.serialize().hex()
    parent_txid = get_txid(parent_hex)
    funding = EscrowOutpoint(txid="aa" * 32, vout=0)
    escrow = EscrowOutpoint(txid=parent_txid, vout=0)
    terms = ChannelInputEscrowTerms(
        snapshot=ChannelSnapshot(
            session_id="session-1",
            channel_id="channel-1",
            funding_outpoint=funding,
            funding_scriptpubkey="5120" + "99" * 32,
            local_funding_pubkey=maker_escrow.public_key.format(compressed=True).hex(),
            remote_funding_pubkey=peer_escrow.public_key.format(compressed=True).hex(),
            capacity_sats=80_000,
            maker_claim_sats=70_000,
            peer_claim_sats=10_000,
            quiescent=True,
            active_htlcs=0,
        ),
        authorizer_pubkey=maker_escrow.public_key.format(compressed=True).hex(),
        payment_hash=_PAYMENT_HASH,
        invoice="lnbcrt10m1escrowinvoice",
        invoice_payee_pubkey=peer_escrow.public_key.format(compressed=True).hex(),
        invoice_payment_secret="22" * 32,
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
    outpoint = SwapOutpoint(parent_txid, 0, 80_000, tree.funding_output_script())
    timeout_hex = (
        build_success_spend(
            tree,
            outpoint,
            _PREIMAGE,
            maker_claim.secret,
            create_p2tr_scriptpubkey(bytes.fromhex("44" * 32)),
            500,
        )
        .serialize()
        .hex()
    )
    prepared = PreparedChannelInputEscrow(
        terms=terms,
        parent_evidence=EscrowParentEvidence(
            parent_tx_hex=parent_hex,
            parent_txid=parent_txid,
            channel_funding_outpoint=funding,
            channel_input_index=0,
            ordered_prevouts=(
                EscrowParentPrevout(
                    value_sats=terms.snapshot.capacity_sats,
                    scriptpubkey=terms.snapshot.funding_scriptpubkey,
                ),
            ),
            escrow_outpoint=escrow,
            escrow_value_sats=80_000,
            escrow_scriptpubkey=tree.funding_output_script().hex(),
            all_inputs_nonmalleable=True,
            exact_transaction_verified=True,
        ),
        timeout_evidence=EscrowTimeoutEvidence(
            timeout_tx_hex=timeout_hex,
            timeout_txid=get_txid(timeout_hex),
            spent_escrow_outpoint=escrow,
            locktime=1_000,
            maker_output_sats=69_500,
            peer_output_sats=10_000,
            fee_sats=500,
            fully_signed=True,
            signature_verified=True,
        ),
    )
    authorization = ParentAuthorization(
        session_id="session-1",
        parent_txid=parent_txid,
        prepared_parent_tx_hex=parent_hex,
        timeout_txid=prepared.timeout_evidence.timeout_txid,
        channel_funding_outpoint=funding,
        channel_input_index=0,
        channel_input_witness=(b"x" * 64,),
        sighash="default",
        signature_verified=True,
        channel_witness_txid_verified=True,
        commits_all_inputs=True,
        commits_all_outputs=True,
    )
    finalized_parent = FinalizedEscrowParent(
        session_id="session-1",
        parent_txid=parent_txid,
        signed_parent_tx_hex=parent_hex,
        channel_funding_outpoint=funding,
        channel_input_index=0,
        all_inputs_signed=True,
        all_signatures_verified=True,
        signed_parent_txid_verified=True,
    )
    material = SuccessSpendMaterial(
        tree=tree,
        maker_claim_private_key=maker_claim.secret,
        maker_output_script=create_p2tr_scriptpubkey(bytes.fromhex("44" * 32)),
        maker_output_is_controlled=True,
    )
    return (
        prepared,
        ParentBroadcastRequest(
            prepared=prepared,
            authorization=authorization,
            finalized_parent=finalized_parent,
        ),
        material,
    )


def _adapter(
    tmp_path: Path, rpc: _Rpc, material: SuccessSpendMaterial
) -> BitcoinCoreEscrowChainAdapter:
    return BitcoinCoreEscrowChainAdapter(
        rpc, _MaterialResolver(material), tmp_path, fee_target_blocks=2, minimum_feerate_sat_vb=1
    )


async def _broadcast_parent(
    tmp_path: Path, rpc: _Rpc
) -> tuple[BitcoinCoreEscrowChainAdapter, PreparedChannelInputEscrow, ParentBroadcastRequest]:
    prepared, request, material = _artifacts()
    adapter = _adapter(tmp_path, rpc, material)
    await adapter.register_frozen_channel("session-1", prepared.terms.snapshot.funding_outpoint)
    await adapter.broadcast_parent(request)
    return adapter, prepared, request


def _success_request(prepared: PreparedChannelInputEscrow) -> SuccessSpendRequest:
    return SuccessSpendRequest(
        prepared=prepared,
        payment_result=SinglePartPaymentResult(
            payment_id=_PAYMENT_HASH,
            status=PaymentStatus.SUCCEEDED,
            preimage=_PREIMAGE.hex(),
            attempt_height=950,
            observed_height=950,
            settled_height=950,
            route=EscrowRouteEvidence(
                part_count=1,
                route_channel_ids=("other-channel",),
                final_expiry_height=975,
                outgoing_expiry_height=978,
            ),
        ),
    )


async def test_registration_parent_retry_and_private_permissions(tmp_path: Path) -> None:
    rpc = _Rpc()
    prepared, request, material = _artifacts()
    adapter = _adapter(tmp_path, rpc, material)

    await adapter.register_frozen_channel("session-1", prepared.terms.snapshot.funding_outpoint)
    await adapter.register_frozen_channel("session-1", prepared.terms.snapshot.funding_outpoint)
    result = await adapter.broadcast_parent(request)
    restarted = _adapter(tmp_path, rpc, material)
    assert await restarted.broadcast_parent(request) == result
    assert [method for method, _ in rpc.calls].count("sendrawtransaction") == 1
    journal = next(tmp_path.glob("*.json"))
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600

    journal.chmod(0o644)
    with pytest.raises(ChannelInputEscrowError, match="unsafe permissions"):
        await restarted.observe_escrow_chain(
            ChainObservationRequest(session_id="session-1", parent_txid=result.parent_txid)
        )


async def test_observation_reports_active_parent_and_confirmed_frozen_conflict(
    tmp_path: Path,
) -> None:
    rpc = _Rpc()
    adapter, prepared, parent_request = await _broadcast_parent(tmp_path, rpc)
    parent_txid = prepared.parent_evidence.parent_txid
    rpc.transactions[parent_txid]["blockhash"] = "parent-block"
    active = await adapter.observe_escrow_chain(
        ChainObservationRequest(session_id="session-1", parent_txid=parent_txid)
    )
    assert active.in_best_chain
    assert active.parent_confirmations == 2

    del rpc.transactions[parent_txid]
    conflict_txid = "bb" * 32
    rpc.blocks[950] = {
        "tx": [
            {
                "txid": conflict_txid,
                "vin": [{"txid": prepared.terms.snapshot.funding_outpoint.txid, "vout": 0}],
            }
        ]
    }
    conflict = await adapter.observe_escrow_chain(
        ChainObservationRequest(
            session_id="session-1", parent_txid=parent_request.prepared.parent_evidence.parent_txid
        )
    )
    assert not conflict.in_best_chain
    assert conflict.conflict_txid == conflict_txid
    assert conflict.conflict_confirmations == 1


async def test_success_spend_requires_policy_and_uses_exact_retry(tmp_path: Path) -> None:
    rpc = _Rpc()
    adapter, prepared, _ = await _broadcast_parent(tmp_path, rpc)
    request = _success_request(prepared)

    result = await adapter.build_broadcast_success_spend(request)
    assert result.additional_input_value_sats == 0
    assert result.rbf_enabled
    assert result.all_outputs_maker_controlled
    assert await adapter.build_broadcast_success_spend(request) == result
    assert [method for method, _ in rpc.calls].count("testmempoolaccept") == 1

    rejected_rpc = _Rpc()
    rejected_rpc.policy_allowed = False
    rejected_adapter, rejected_prepared, _ = await _broadcast_parent(
        tmp_path / "rejected", rejected_rpc
    )
    with pytest.raises(ChainError, match="mempool policy"):
        await rejected_adapter.build_broadcast_success_spend(_success_request(rejected_prepared))


async def test_generic_send_failure_is_not_treated_as_success(tmp_path: Path) -> None:
    rpc = _Rpc()
    rpc.send_error = BitcoinCoreRpcError(-25)
    prepared, request, material = _artifacts()
    adapter = _adapter(tmp_path, rpc, material)
    await adapter.register_frozen_channel("session-1", prepared.terms.snapshot.funding_outpoint)

    with pytest.raises(ChainError, match="rejected escrow transaction broadcast"):
        await adapter.broadcast_parent(request)


async def test_stale_chain_transaction_is_rebroadcast_not_treated_as_visible(
    tmp_path: Path,
) -> None:
    rpc = _Rpc()
    prepared, request, material = _artifacts()
    txid = prepared.parent_evidence.parent_txid
    rpc.transactions[txid] = {
        "hex": request.finalized_parent.signed_parent_tx_hex,
        "blockhash": "stale-block",
    }
    rpc.header_confirmations["stale-block"] = 0
    adapter = _adapter(tmp_path, rpc, material)
    await adapter.register_frozen_channel("session-1", prepared.terms.snapshot.funding_outpoint)

    await adapter.broadcast_parent(request)

    assert [method for method, _ in rpc.calls].count("sendrawtransaction") == 1


async def test_conflicting_registration_and_invalid_material_fail_closed(tmp_path: Path) -> None:
    rpc = _Rpc()
    prepared, _, material = _artifacts()
    adapter = _adapter(tmp_path, rpc, material)
    await adapter.register_frozen_channel("session-1", prepared.terms.snapshot.funding_outpoint)
    with pytest.raises(ChannelInputEscrowError, match="conflicting frozen"):
        await adapter.register_frozen_channel("session-1", EscrowOutpoint(txid="cc" * 32, vout=0))

    wrong_tree = ChannelInputEscrowTree(
        material.tree.maker_escrow_pubkey,
        material.tree.peer_escrow_pubkey,
        material.tree.maker_claim_pubkey,
        b"\x00" * 32,
    )
    bad = _adapter(
        tmp_path / "bad",
        rpc,
        SuccessSpendMaterial(
            wrong_tree,
            material.maker_claim_private_key,
            material.maker_output_script,
            True,
        ),
    )
    await bad.register_frozen_channel("session-1", prepared.terms.snapshot.funding_outpoint)
    await bad.broadcast_parent(_artifacts()[1])
    with pytest.raises(ChannelInputEscrowError, match="payment hash"):
        await bad.build_broadcast_success_spend(_success_request(prepared))


def _recovery_record(
    prepared: PreparedChannelInputEscrow, request: ParentBroadcastRequest
) -> ChannelInputEscrowRecord:
    record = ChannelInputEscrowRecord(terms=prepared.terms)
    record.attach_prepared(prepared)
    record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
    record.attach_authorization(request.authorization)
    record.advance(EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED)
    record.attach_finalized_parent(request.finalized_parent)
    record.advance(EscrowLifecycleState.PARENT_SIGNED)
    record.attach_parent_broadcast(
        EscrowParentBroadcast(
            session_id=record.session_id,
            parent_txid=prepared.parent_evidence.parent_txid,
            broadcast_height=950,
        )
    )
    record.advance(EscrowLifecycleState.PARENT_PENDING)
    return record


async def test_timeout_requires_strict_maturity_and_exact_transaction(tmp_path: Path) -> None:
    rpc = _Rpc(1_000)
    adapter, prepared, parent_request = await _broadcast_parent(tmp_path, rpc)
    record = _recovery_record(prepared, parent_request)
    with pytest.raises(ChannelInputEscrowError, match="not yet mature"):
        await adapter.broadcast_timeout_recovery(RecoveryRequest(record=record))

    rpc.height = 1_001
    result = await adapter.broadcast_timeout_recovery(RecoveryRequest(record=record))
    assert result.timeout_txid == prepared.timeout_evidence.timeout_txid


async def test_fee_bump_and_recovery_rebroadcast_parent_before_success(tmp_path: Path) -> None:
    rpc = _Rpc()
    adapter, prepared, parent_request = await _broadcast_parent(tmp_path, rpc)
    success_request = _success_request(prepared)
    initial = await adapter.build_broadcast_success_spend(success_request)
    record = _recovery_record(prepared, parent_request)
    confirmed = EscrowChainObservation(
        session_id=record.session_id,
        parent_txid=prepared.parent_evidence.parent_txid,
        parent_confirmations=2,
        in_best_chain=True,
        observed_height=950,
    )
    record.attach_chain_observation(confirmed)
    record.advance(EscrowLifecycleState.PARENT_CONFIRMED)
    payment_request = SinglePartPaymentRequest(
        prepared=prepared,
        chain_observation=confirmed,
        excluded_channel_ids=("channel-1",),
        requested_at_height=950,
        requested_at_unix=1_900_000_000,
    )
    record.attach_payment_request(payment_request)
    pending = success_request.payment_result.model_copy(
        update={"status": PaymentStatus.PENDING, "preimage": None, "settled_height": None}
    )
    record.attach_payment_result(pending)
    record.advance(EscrowLifecycleState.PAYMENT_IN_FLIGHT)
    record.attach_payment_result(success_request.payment_result)
    record.attach_success_spend(initial)
    record.advance(EscrowLifecycleState.SUCCESS_SPEND_PENDING)

    bump_request = RecoveryRequest(record=record)
    replacement = await adapter.fee_bump_success_spend(bump_request)
    assert replacement.replaces_txid == initial.txid
    assert replacement.fee_sats > initial.fee_sats
    assert replacement.fee_rate_milli_sat_vb > initial.fee_rate_milli_sat_vb
    assert replacement.replacement_policy_verified
    assert await adapter.fee_bump_success_spend(bump_request) == replacement

    record.attach_success_spend(replacement)
    second = await adapter.fee_bump_success_spend(RecoveryRequest(record=record))
    assert second.replaces_txid == replacement.txid
    assert second.fee_sats > replacement.fee_sats

    record.attach_success_spend(second)
    record.attach_chain_observation(
        EscrowChainObservation(
            session_id=record.session_id,
            parent_txid=prepared.parent_evidence.parent_txid,
            parent_confirmations=0,
            in_best_chain=False,
            observed_height=951,
        )
    )
    record.advance(EscrowLifecycleState.PARENT_REORGED)
    rpc.transactions.clear()
    before = len(rpc.calls)
    recovery = await adapter.rebroadcast_recovery_artifacts(RecoveryRequest(record=record))
    sent = [params[0] for method, params in rpc.calls[before:] if method == "sendrawtransaction"]
    assert recovery.broadcast_txids == (prepared.parent_evidence.parent_txid, second.txid)
    assert sent == [parent_request.finalized_parent.signed_parent_tx_hex, second.tx_hex]


async def test_conflicting_parent_request_and_invalid_preimage_fail_closed(tmp_path: Path) -> None:
    rpc = _Rpc()
    adapter, prepared, parent_request = await _broadcast_parent(tmp_path, rpc)
    changed_authorization = parent_request.authorization.model_copy(
        update={"channel_input_witness": (b"d" * 64,)}
    )
    with pytest.raises(ChannelInputEscrowError, match="conflicting parent"):
        await adapter.broadcast_parent(
            ParentBroadcastRequest(
                prepared=prepared,
                authorization=changed_authorization,
                finalized_parent=parent_request.finalized_parent,
            )
        )

    invalid = _success_request(prepared).model_copy(
        update={
            "payment_result": _success_request(prepared).payment_result.model_copy(
                update={"preimage": "22" * 32}
            )
        }
    )
    with pytest.raises(ChannelInputEscrowError, match="preimage"):
        await adapter.build_broadcast_success_spend(invalid)
