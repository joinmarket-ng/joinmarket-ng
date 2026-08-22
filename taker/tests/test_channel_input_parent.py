from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    analyze_coinjoin_outputs,
    estimate_vsize,
    get_txid,
    parse_transaction,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
)
from jmcore.channel_input import (
    ChannelDescriptor,
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputKeyPair,
    ChannelInputPreparePayload,
    ChannelInputTranscript,
    EndpointTerms,
    InputPrevout,
    Outpoint,
    PolicyCeilings,
    approval_set_hash,
    endpoint_terms_hash,
    sign_payload,
    transcript_hash,
)
from jmcore.channel_input import (
    SettlementOutput as AuthorizedSettlementOutput,
)
from jmswap.lnd import (
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
    WitnessUtxo,
)
from jmwallet.wallet.signing import create_p2wpkh_script_code, sign_p2tr_input, sign_p2wpkh_input

from taker.channel_input_parent import (
    ChannelContribution,
    ChannelInputAuthorization,
    ChannelInputParentCoordinator,
    ChannelInputParentError,
    ChannelInputParentPlan,
    ChannelInputParentRecord,
    ChannelInputParentState,
    ChannelInputRestartAction,
    EndpointContribution,
    FrozenSnapshot,
    OrdinaryWitness,
    ParentPrevout,
    SettlementOutput,
)


@dataclass
class FakeTransaction:
    txid: str
    raw: str
    confirmations: int


class FakeChain:
    def __init__(self, calls: list[str] | None = None) -> None:
        self.transaction: FakeTransaction | None = None
        self.broadcasts: list[str] = []
        self.calls = calls

    async def get_transaction(self, txid: str) -> FakeTransaction | None:
        if self.calls is not None:
            self.calls.append("chain")
        if self.transaction is not None and self.transaction.txid == txid:
            return self.transaction
        return None

    async def broadcast_transaction(self, tx_hex: str) -> str:
        self.broadcasts.append(tx_hex)
        return get_txid(tx_hex)


class FakeEndpoint:
    def __init__(
        self,
        *,
        local_key: PrivateKey,
        remote_key: PrivateKey,
        funding_key: PrivateKey,
        first: bool,
        shared_signature: list[bytes | None],
        crash_after_sign: bool = False,
        fail_sign_once: bool = False,
        fail_freeze_once: bool = False,
        fail_prepare_once: bool = False,
        mutate_final: bool = False,
        snapshot_peer_key: PrivateKey | None = None,
        snapshot_local_claim_sat: int = 50_000,
        calls: list[str] | None = None,
    ) -> None:
        self.local_key = local_key
        self.remote_key = remote_key
        self.funding_key = funding_key
        self.first = first
        self.shared_signature = shared_signature
        self.crash_after_sign = crash_after_sign
        self.fail_sign_once = fail_sign_once
        self.fail_freeze_once = fail_freeze_once
        self.fail_prepare_once = fail_prepare_once
        self.mutate_final = mutate_final
        self.snapshot_peer_key = snapshot_peer_key
        self.snapshot_local_claim_sat = snapshot_local_claim_sat
        self.parent_hex = ""
        self.index = 0
        self.attempt: LndEscrowSigningAttempt | None = None
        self.partial: bytes | None = None
        self.status_remote_nonce: bytes | None = None
        self.status_partial: bytes | None = None
        self.begin_calls = 0
        self.sign_calls = 0
        self.cancel_calls = 0
        self.freeze_calls = 0
        self.calls = calls

    def _snapshot(self, session_id: str, channel_id: str) -> FrozenChannelSnapshot:
        del session_id
        local = self.local_key.public_key.format(compressed=True)
        remote_key = self.snapshot_peer_key or self.remote_key
        remote = remote_key.public_key.format(compressed=True)
        return FrozenChannelSnapshot(
            session_id="fake",
            channel_id=channel_id,
            funding_outpoint=EscrowOutpoint(txid="11" * 32, vout=0),
            funding_scriptpubkey=b"\x51\x20" + self.funding_key.public_key_xonly.format(),
            peer_node_pubkey=remote,
            local_funding_pubkey=local,
            remote_funding_pubkey=remote,
            capacity_sat=100_000,
            local_claim_sat=self.snapshot_local_claim_sat,
            remote_claim_sat=100_000 - self.snapshot_local_claim_sat,
        )

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> FrozenChannelSnapshot:
        self.freeze_calls += 1
        if self.fail_freeze_once:
            self.fail_freeze_once = False
            raise RuntimeError("simulated freeze interruption")
        return self._snapshot(session_id, channel_id)

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo],
        channel_input_index: int,
    ) -> str:
        del session_id, funding_outpoint, input_utxos
        self.parent_hex = parent_tx_hex
        self.index = channel_input_index
        if self.fail_prepare_once:
            self.fail_prepare_once = False
            raise RuntimeError("simulated PrepareParent interruption")
        return get_txid(parent_tx_hex)

    async def begin_channel_input_signing(
        self, session_id: str, funding_outpoint: EscrowOutpoint, *, restart_attempt: bool = False
    ) -> LndEscrowSigningAttempt:
        del session_id, funding_outpoint
        assert not restart_attempt
        self.begin_calls += 1
        if self.attempt is None:
            marker = b"\x01" if self.first else b"\x02"
            self.attempt = LndEscrowSigningAttempt(
                attempt_id=marker * 32, public_nonce=(marker * 66)
            )
        return self.attempt

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes:
        del session_id, funding_outpoint, remote_public_nonce
        assert self.attempt == attempt
        self.sign_calls += 1
        if self.fail_sign_once:
            self.fail_sign_once = False
            raise RuntimeError("simulated signing interruption before durable partial")
        if self.partial is None:
            self.partial = (b"\x03" if self.first else b"\x04") * 32
        if self.crash_after_sign:
            self.crash_after_sign = False
            raise RuntimeError("simulated process death after durable endpoint signing")
        return self.partial

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        del session_id, funding_outpoint, remote_partial_signature
        assert self.attempt == attempt
        tx = parse_transaction(self.parent_hex)
        prevouts = [
            100_000,
            130_000,
        ]
        scripts = [
            b"\x51\x20" + self.funding_key.public_key_xonly.format(),
            pubkey_to_p2wpkh_script(ORDINARY_KEY.public_key.format(compressed=True)),
        ]
        signature = self.shared_signature[0]
        if signature is None:
            signature = sign_p2tr_input(tx, self.index, prevouts, scripts, self.funding_key)
            self.shared_signature[0] = signature
        outputs = list(tx.outputs)
        if self.mutate_final:
            outputs[0] = TxOutput(value=outputs[0].value + 1, script=outputs[0].script)
        witnesses: list[list[bytes]] = [[] for _ in tx.inputs]
        witnesses[self.index] = [signature]
        raw = serialize_transaction(tx.version, tx.inputs, outputs, tx.locktime, witnesses).hex()
        return LndFinalizedEscrowParent(
            tx_hex=raw,
            txid=get_txid(raw),
            channel_input_index=self.index,
            channel_input_witness=(signature,),
        )

    async def recover_channel_input_parent(
        self, session_id: str, funding_outpoint: EscrowOutpoint, remote_partial_signature: bytes
    ) -> LndFinalizedEscrowParent:
        assert self.attempt is not None
        return await self.finalize_channel_input_parent(
            session_id, funding_outpoint, self.attempt, remote_partial_signature
        )

    async def channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus:
        del session_id, funding_outpoint
        if self.calls is not None:
            self.calls.append("status")
        assert self.attempt is not None
        partial = self.status_partial if self.status_partial is not None else self.partial
        signed = partial is not None
        return LndEscrowStatus(
            stage=LndEscrowStage.PARENT_SIGNED if signed else LndEscrowStage.PARENT_PREPARED,
            parent_txid=get_txid(self.parent_hex),
            finalized=False,
            durable_local_public_nonce=self.attempt.public_nonce if signed else None,
            durable_remote_public_nonce=(
                self.status_remote_nonce
                if self.status_remote_nonce is not None
                else (b"\x02" if self.first else b"\x01") * 66
            )
            if signed
            else None,
            durable_local_partial_signature=partial,
            active_attempt=self.attempt,
        )

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool:
        del session_id, funding_outpoint
        self.cancel_calls += 1
        return True


ORDINARY_KEY = PrivateKey(b"\x07" * 32)
SESSION_ID = "11" * 32
FIRST_PARTICIPANT_SECRET = b"\x01" * 32
SECOND_PARTICIPANT_SECRET = b"\x02" * 32


def _participant_key(secret: bytes) -> str:
    return ChannelInputKeyPair.from_secret(secret).public_key


def _authorization(
    *,
    unsigned_parent_hex: str,
    ordinary_script: bytes,
    outputs: list[TxOutput],
    first_value_sat: int,
    second_value_sat: int,
    first_claim_adjustment_sat: int,
    second_claim_adjustment_sat: int,
) -> ChannelInputAuthorization:
    funding_key = PrivateKey(b"\x05" * 32)
    first_node = PrivateKey(b"\x01" * 32)
    second_node = PrivateKey(b"\x02" * 32)
    first_participant_key = _participant_key(FIRST_PARTICIPANT_SECRET)
    second_participant_key = _participant_key(SECOND_PARTICIPANT_SECRET)
    ceilings = PolicyCeilings(
        max_miner_fee_share_sat=1_000,
        max_claim_adjustment_sat=1_000,
        max_settlement_outputs=1,
    )
    first_terms = EndpointTerms(
        session_id=SESSION_ID,
        attempt=0,
        funding_outpoint=Outpoint(txid="11" * 32, vout=0),
        input_index=0,
        participant_key=first_participant_key,
        role="first",
        local_node_key=first_node.public_key.format(compressed=True).hex(),
        peer_node_key=second_node.public_key.format(compressed=True).hex(),
        capacity_sat=100_000,
        frozen_local_claim_sat=50_000,
        frozen_remote_claim_sat=50_000,
        settlement_outputs=(
            AuthorizedSettlementOutput(
                output_index=0, amount_sat=first_value_sat, script_pubkey=outputs[0].script.hex()
            ),
        ),
        miner_fee_share_sat=1_000,
        claim_adjustment_sat=first_claim_adjustment_sat,
        policy_ceilings=ceilings,
    )
    second_terms = EndpointTerms(
        session_id=SESSION_ID,
        attempt=0,
        funding_outpoint=Outpoint(txid="11" * 32, vout=0),
        input_index=0,
        participant_key=second_participant_key,
        role="second",
        local_node_key=second_node.public_key.format(compressed=True).hex(),
        peer_node_key=first_node.public_key.format(compressed=True).hex(),
        capacity_sat=100_000,
        frozen_local_claim_sat=50_000,
        frozen_remote_claim_sat=50_000,
        settlement_outputs=(
            AuthorizedSettlementOutput(
                output_index=1, amount_sat=second_value_sat, script_pubkey=outputs[1].script.hex()
            ),
        ),
        miner_fee_share_sat=1_000,
        claim_adjustment_sat=second_claim_adjustment_sat,
        policy_ceilings=ceilings,
    )
    fee_sat = 230_000 - sum(output.value for output in outputs)
    transcript = ChannelInputTranscript(
        network="regtest",
        session_id=SESSION_ID,
        attempt=0,
        unsigned_parent_tx=unsigned_parent_hex,
        input_prevouts=(
            InputPrevout(
                outpoint=Outpoint(txid="11" * 32, vout=0),
                value_sat=100_000,
                script_pubkey=(b"\x51\x20" + funding_key.public_key_xonly.format()).hex(),
            ),
            InputPrevout(
                outpoint=Outpoint(txid="22" * 32, vout=1),
                value_sat=130_000,
                script_pubkey=ordinary_script.hex(),
            ),
        ),
        total_fee_sat=fee_sat,
        fee_rate_sat_vb=(
            fee_sat + estimate_vsize(["p2tr", "p2wpkh"], ["p2tr", "p2tr", "p2wpkh", "p2tr"]) - 1
        )
        // estimate_vsize(["p2tr", "p2wpkh"], ["p2tr", "p2tr", "p2wpkh", "p2tr"]),
        expiry=1_900_000_000,
        recovery_height=999_999,
        participant_keys=(first_participant_key, second_participant_key),
        coordinator_key=first_participant_key,
        channels=(
            ChannelDescriptor(
                funding_outpoint=Outpoint(txid="11" * 32, vout=0),
                input_index=0,
                capacity_sat=100_000,
                first_participant_key=first_participant_key,
                second_participant_key=second_participant_key,
                first_terms_hash=endpoint_terms_hash(first_terms).hex(),
                second_terms_hash=endpoint_terms_hash(second_terms).hex(),
            ),
        ),
    )
    approvals = tuple(
        sign_payload(
            ChannelInputApprovalPayload(
                session_id=SESSION_ID,
                attempt=0,
                signer_key=participant_key,
                transcript_hash=transcript_hash(transcript).hex(),
                terms_hashes=tuple(sorted(transcript.terms_hashes_for(participant_key))),
            ),
            secret,
            aux_randomness=secret,
        )
        for participant_key, secret in (
            (first_participant_key, FIRST_PARTICIPANT_SECRET),
            (second_participant_key, SECOND_PARTICIPANT_SECRET),
        )
    )
    approval_set = sign_payload(
        ChannelInputApprovalSetPayload(
            session_id=SESSION_ID,
            attempt=0,
            signer_key=first_participant_key,
            transcript=transcript,
            approvals=approvals,
        ),
        FIRST_PARTICIPANT_SECRET,
        aux_randomness=b"\x03" * 32,
    )
    acks = tuple(
        sign_payload(
            ChannelInputApprovalSetAckPayload(
                session_id=SESSION_ID,
                attempt=0,
                signer_key=participant_key,
                transcript_hash=transcript_hash(transcript).hex(),
                approval_set_hash=approval_set_hash(approval_set).hex(),
            ),
            secret,
            aux_randomness=bytes([index]) * 32,
        )
        for index, (participant_key, secret) in enumerate(
            (
                (first_participant_key, FIRST_PARTICIPANT_SECRET),
                (second_participant_key, SECOND_PARTICIPANT_SECRET),
            ),
            start=4,
        )
    )
    prepare = sign_payload(
        ChannelInputPreparePayload(
            session_id=SESSION_ID,
            attempt=0,
            signer_key=first_participant_key,
            transcript_hash=transcript_hash(transcript).hex(),
            approval_set_hash=approval_set_hash(approval_set).hex(),
            approval_set=approval_set,
            approval_set_acks=acks,
        ),
        FIRST_PARTICIPANT_SECRET,
        aux_randomness=b"\x06" * 32,
    )
    return ChannelInputAuthorization(prepare=prepare, endpoint_terms=(first_terms, second_terms))


def _plan(
    *,
    invalid_witness: bool = False,
    first_claim_adjustment_sat: int = 0,
    second_claim_adjustment_sat: int = 0,
    first_settlement_value_sat: int | None = None,
) -> ChannelInputParentPlan:
    funding_key = PrivateKey(b"\x05" * 32)
    ordinary_pubkey = ORDINARY_KEY.public_key.format(compressed=True)
    ordinary_script = pubkey_to_p2wpkh_script(ordinary_pubkey)
    inputs = [TxInput.from_hex("11" * 32, 0), TxInput.from_hex("22" * 32, 1)]
    first_value = (
        49_000 + first_claim_adjustment_sat
        if first_settlement_value_sat is None
        else first_settlement_value_sat
    )
    second_value = 49_000 + second_claim_adjustment_sat
    outputs = [
        TxOutput(value=first_value, script=b"\x51\x20" + b"\x21" * 32),
        TxOutput(value=second_value, script=b"\x51\x20" + b"\x22" * 32),
        TxOutput(value=40_000, script=ordinary_script),
        TxOutput(value=40_000, script=b"\x51\x20" + b"\x23" * 32),
    ]
    unsigned = serialize_transaction(2, inputs, outputs, 0).hex()
    tx = parse_transaction(unsigned)
    signature = sign_p2wpkh_input(
        tx, 1, create_p2wpkh_script_code(ordinary_pubkey), 130_000, ORDINARY_KEY
    )
    if invalid_witness:
        signature = b"\x00" * len(signature)
    return ChannelInputParentPlan(
        session_id=SESSION_ID,
        unsigned_parent_hex=unsigned,
        prevouts=(
            ParentPrevout(
                value_sat=100_000,
                script_pubkey=(b"\x51\x20" + funding_key.public_key_xonly.format()).hex(),
            ),
            ParentPrevout(value_sat=130_000, script_pubkey=ordinary_script.hex()),
        ),
        channels=(
            ChannelContribution(
                channel_id="channel-a",
                channel_input_index=0,
                first=EndpointContribution(
                    endpoint_id="first",
                    participant_key=_participant_key(FIRST_PARTICIPANT_SECRET),
                    settlement_outputs=(
                        SettlementOutput(
                            output_index=0,
                            value_sat=first_value,
                            script_pubkey=outputs[0].script.hex(),
                        ),
                    ),
                    miner_fee_share_sat=1_000,
                    claim_adjustment_sat=first_claim_adjustment_sat,
                ),
                second=EndpointContribution(
                    endpoint_id="second",
                    participant_key=_participant_key(SECOND_PARTICIPANT_SECRET),
                    settlement_outputs=(
                        SettlementOutput(
                            output_index=1,
                            value_sat=second_value,
                            script_pubkey=outputs[1].script.hex(),
                        ),
                    ),
                    miner_fee_share_sat=1_000,
                    claim_adjustment_sat=second_claim_adjustment_sat,
                ),
            ),
        ),
        ordinary_witnesses=(
            OrdinaryWitness(input_index=1, witness=(signature.hex(), ordinary_pubkey.hex())),
        ),
        authorization=_authorization(
            unsigned_parent_hex=unsigned,
            ordinary_script=ordinary_script,
            outputs=outputs,
            first_value_sat=first_value,
            second_value_sat=second_value,
            first_claim_adjustment_sat=first_claim_adjustment_sat,
            second_claim_adjustment_sat=second_claim_adjustment_sat,
        ),
    )


def _endpoints(
    *,
    crash_after_sign: bool = False,
    fail_sign_once: bool = False,
    fail_freeze_once: bool = False,
    fail_prepare_once: bool = False,
    mutate_final: bool = False,
    first_snapshot_peer_key: PrivateKey | None = None,
    first_snapshot_local_claim_sat: int = 50_000,
    calls: list[str] | None = None,
) -> dict[str, FakeEndpoint]:
    first_key = PrivateKey(b"\x01" * 32)
    second_key = PrivateKey(b"\x02" * 32)
    funding_key = PrivateKey(b"\x05" * 32)
    shared_signature: list[bytes | None] = [None]
    return {
        "first": FakeEndpoint(
            local_key=first_key,
            remote_key=second_key,
            funding_key=funding_key,
            first=True,
            shared_signature=shared_signature,
            crash_after_sign=crash_after_sign,
            fail_sign_once=fail_sign_once,
            fail_freeze_once=fail_freeze_once,
            fail_prepare_once=fail_prepare_once,
            mutate_final=mutate_final,
            snapshot_peer_key=first_snapshot_peer_key,
            snapshot_local_claim_sat=first_snapshot_local_claim_sat,
            calls=calls,
        ),
        "second": FakeEndpoint(
            local_key=second_key,
            remote_key=first_key,
            funding_key=funding_key,
            first=False,
            shared_signature=shared_signature,
            calls=calls,
        ),
    }


def _final_parent(plan: ChannelInputParentPlan) -> str:
    funding_key = PrivateKey(b"\x05" * 32)
    transaction = parse_transaction(plan.unsigned_parent_hex)
    signature = sign_p2tr_input(
        transaction,
        0,
        [prevout.value_sat for prevout in plan.prevouts],
        [bytes.fromhex(prevout.script_pubkey) for prevout in plan.prevouts],
        funding_key,
    )
    ordinary = plan.ordinary_witnesses[0]
    return serialize_transaction(
        transaction.version,
        transaction.inputs,
        transaction.outputs,
        transaction.locktime,
        [[signature], [bytes.fromhex(item) for item in ordinary.witness]],
    ).hex()


def _save_record(
    coordinator: ChannelInputParentCoordinator,
    plan: ChannelInputParentPlan,
    state: ChannelInputParentState,
) -> None:
    coordinator._store.save(
        ChannelInputParentRecord(
            record_id=coordinator.record_id(plan.session_id),
            plan=plan,
            state=state,
            updated_at_ns=0,
        )
    )


def test_happy_path_persists_final_bytes_and_publishes_query_first(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    endpoints = _endpoints()
    plan = _plan()
    assert plan.channels[0].first.claim_adjustment_sat == 0
    assert plan.channels[0].second.claim_adjustment_sat == 0
    record = asyncio.run(coordinator.execute(plan, endpoints))

    assert record.state is ChannelInputParentState.FINAL_TX_PERSISTED
    assert record.final_tx_hex is not None
    assert analyze_coinjoin_outputs(parse_transaction(record.final_tx_hex).outputs).is_coinjoin
    chain = FakeChain()
    asyncio.run(coordinator.publish(record.plan.session_id, chain))
    assert chain.broadcasts == [record.final_tx_hex]
    asyncio.run(coordinator.publish(record.plan.session_id, chain))
    assert chain.broadcasts == [record.final_tx_hex, record.final_tx_hex]
    chain.transaction = FakeTransaction(
        record.final_txid or "", record.final_tx_hex, confirmations=1
    )
    observed = asyncio.run(coordinator.publish(record.plan.session_id, chain))
    assert observed.state is ChannelInputParentState.CONFIRMED
    assert len(chain.broadcasts) == 2


def test_zero_sum_claim_adjustments_progress_and_conserve_capacity(tmp_path: Path) -> None:
    adjustment_sat = 500
    plan = _plan(
        first_claim_adjustment_sat=adjustment_sat,
        second_claim_adjustment_sat=-adjustment_sat,
    )
    record = asyncio.run(ChannelInputParentCoordinator(tmp_path).execute(plan, _endpoints()))

    assert record.state is ChannelInputParentState.FINAL_TX_PERSISTED
    channel = record.channels[0]
    first, second = plan.channels[0].first, plan.channels[0].second
    assert (
        sum(output.value_sat for output in first.settlement_outputs) + first.miner_fee_share_sat
        == channel.first_snapshot.local_claim_sat + first.claim_adjustment_sat
    )
    assert (
        sum(output.value_sat for output in second.settlement_outputs) + second.miner_fee_share_sat
        == channel.second_snapshot.local_claim_sat + second.claim_adjustment_sat
    )
    assert (
        sum(output.value_sat for output in first.settlement_outputs)
        + first.miner_fee_share_sat
        + sum(output.value_sat for output in second.settlement_outputs)
        + second.miner_fee_share_sat
        == channel.first_snapshot.capacity_sat
    )


def test_rejects_nonzero_sum_claim_adjustments_before_rpc_progression(tmp_path: Path) -> None:
    plan = _plan()
    contribution = plan.channels[0]
    malformed = plan.model_copy(
        update={
            "channels": (
                contribution.model_copy(
                    update={
                        "first": contribution.first.model_copy(update={"claim_adjustment_sat": 1})
                    }
                ),
            )
        }
    )
    endpoints = _endpoints()

    with pytest.raises(ChannelInputParentError, match="claim adjustment differs"):
        asyncio.run(ChannelInputParentCoordinator(tmp_path).execute(malformed, endpoints))

    assert endpoints["first"].freeze_calls == endpoints["second"].freeze_calls == 0


@pytest.mark.parametrize("settlement_value_sat", [49_499, 49_501])
def test_rejects_underpay_and_overpay_against_adjusted_claim(
    tmp_path: Path, settlement_value_sat: int
) -> None:
    authorized = _plan(
        first_claim_adjustment_sat=500,
        second_claim_adjustment_sat=-500,
    )
    plan = authorized.model_copy(
        update={
            "channels": (
                authorized.channels[0].model_copy(
                    update={
                        "first": authorized.channels[0].first.model_copy(
                            update={
                                "settlement_outputs": (
                                    SettlementOutput(
                                        output_index=0,
                                        value_sat=settlement_value_sat,
                                        script_pubkey=authorized.channels[0]
                                        .first.settlement_outputs[0]
                                        .script_pubkey,
                                    ),
                                )
                            }
                        )
                    }
                ),
            )
        }
    )

    with pytest.raises(ChannelInputParentError, match="settlement outputs differ"):
        asyncio.run(ChannelInputParentCoordinator(tmp_path).execute(plan, _endpoints()))


def test_peer_node_pubkey_is_durable_and_requires_a_valid_compressed_key(tmp_path: Path) -> None:
    endpoints = _endpoints()
    plan = _plan()
    record = asyncio.run(ChannelInputParentCoordinator(tmp_path).execute(plan, endpoints))
    durable = asyncio.run(
        ChannelInputParentCoordinator(tmp_path).resume(record.plan.session_id, endpoints)
    )
    expected_first_peer = endpoints["first"].remote_key.public_key.format(compressed=True).hex()
    expected_second_peer = endpoints["second"].remote_key.public_key.format(compressed=True).hex()

    assert durable.channels[0].first_snapshot.peer_node_pubkey == expected_first_peer
    assert durable.channels[0].second_snapshot.peer_node_pubkey == expected_second_peer
    assert durable.plan.authorization == plan.authorization
    invalid = durable.channels[0].first_snapshot.model_dump()
    invalid["peer_node_pubkey"] = "02" + "00" * 32
    with pytest.raises(ValueError, match="valid secp256k1"):
        FrozenSnapshot.model_validate(invalid)


def test_rejects_absent_or_invalid_signed_authorization_before_freeze(tmp_path: Path) -> None:
    plan = _plan()
    with pytest.raises(ValueError, match="authorization"):
        ChannelInputParentPlan.model_validate(plan.model_dump(exclude={"authorization"}))

    tampered = plan.model_copy(
        update={
            "authorization": plan.authorization.model_copy(
                update={"prepare": plan.authorization.prepare.model_copy(update={"sig": "00" * 64})}
            )
        }
    )
    endpoints = _endpoints()
    with pytest.raises(ChannelInputParentError, match="authorization is invalid"):
        asyncio.run(ChannelInputParentCoordinator(tmp_path).execute(tampered, endpoints))

    assert endpoints["first"].freeze_calls == endpoints["second"].freeze_calls == 0


@pytest.mark.parametrize("kind", ["missing", "extra", "substituted"])
def test_authorization_requires_exact_revealed_endpoint_terms(kind: str) -> None:
    authorization = _plan().authorization
    first, second = authorization.endpoint_terms
    terms: tuple[EndpointTerms, ...]
    if kind == "missing":
        terms = (first,)
    elif kind == "extra":
        terms = (first, second, first)
    else:
        terms = (
            first,
            second.model_copy(
                update={
                    "peer_node_key": PrivateKey(b"\x03" * 32)
                    .public_key.format(compressed=True)
                    .hex()
                }
            ),
        )

    with pytest.raises(ValueError):
        ChannelInputAuthorization(prepare=authorization.prepare, endpoint_terms=terms)


@pytest.mark.parametrize(
    "kind",
    ["transaction", "prevout", "fee", "participant", "settlement", "fee_share", "adjustment"],
)
def test_rejects_plan_authorization_mismatches_before_freeze(tmp_path: Path, kind: str) -> None:
    plan = _plan()
    channel = plan.channels[0]
    if kind == "transaction":
        mismatched = plan.model_copy(
            update={"unsigned_parent_hex": plan.unsigned_parent_hex[:-2] + "01"}
        )
    elif kind in {"prevout", "fee"}:
        mismatched = plan.model_copy(
            update={
                "prevouts": (
                    plan.prevouts[0],
                    plan.prevouts[1].model_copy(
                        update={"value_sat": plan.prevouts[1].value_sat + 1}
                    ),
                )
            }
        )
    elif kind == "participant":
        mismatched = plan.model_copy(
            update={
                "channels": (
                    channel.model_copy(
                        update={
                            "first": channel.first.model_copy(
                                update={"participant_key": channel.second.participant_key}
                            )
                        }
                    ),
                )
            }
        )
    elif kind == "settlement":
        mismatched = plan.model_copy(
            update={
                "channels": (
                    channel.model_copy(
                        update={
                            "first": channel.first.model_copy(
                                update={
                                    "settlement_outputs": (
                                        channel.first.settlement_outputs[0].model_copy(
                                            update={
                                                "value_sat": channel.first.settlement_outputs[
                                                    0
                                                ].value_sat
                                                + 1
                                            }
                                        ),
                                    )
                                }
                            )
                        }
                    ),
                )
            }
        )
    elif kind == "fee_share":
        mismatched = plan.model_copy(
            update={
                "channels": (
                    channel.model_copy(
                        update={
                            "first": channel.first.model_copy(update={"miner_fee_share_sat": 999})
                        }
                    ),
                )
            }
        )
    else:
        mismatched = plan.model_copy(
            update={
                "channels": (
                    channel.model_copy(
                        update={
                            "first": channel.first.model_copy(update={"claim_adjustment_sat": 1})
                        }
                    ),
                )
            }
        )
    endpoints = _endpoints()

    with pytest.raises(ChannelInputParentError):
        asyncio.run(ChannelInputParentCoordinator(tmp_path).execute(mismatched, endpoints))

    assert endpoints["first"].freeze_calls == endpoints["second"].freeze_calls == 0


@pytest.mark.parametrize("kind", ["peer", "claims"])
def test_rejects_frozen_data_that_differs_from_authorization_terms(
    tmp_path: Path, kind: str
) -> None:
    endpoints = _endpoints(
        first_snapshot_peer_key=PrivateKey(b"\x03" * 32) if kind == "peer" else None,
        first_snapshot_local_claim_sat=49_999 if kind == "claims" else 50_000,
    )

    with pytest.raises(ChannelInputParentError, match="frozen channel data differs"):
        asyncio.run(ChannelInputParentCoordinator(tmp_path).execute(_plan(), endpoints))

    assert endpoints["first"].freeze_calls == endpoints["second"].freeze_calls == 1


def test_rejects_invalid_ordinary_witness_before_channel_signing(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    endpoints = _endpoints()

    with pytest.raises(ChannelInputParentError, match="invalid ordinary"):
        asyncio.run(coordinator.execute(_plan(invalid_witness=True), endpoints))

    assert endpoints["first"].sign_calls == 0
    assert endpoints["second"].sign_calls == 0


def test_rejects_settlement_mismatch_and_mutated_endpoint_parent(tmp_path: Path) -> None:
    plan = _plan()
    broken = plan.model_copy(
        update={
            "channels": (
                plan.channels[0].model_copy(
                    update={
                        "first": plan.channels[0].first.model_copy(
                            update={
                                "settlement_outputs": (
                                    SettlementOutput(
                                        output_index=0,
                                        value_sat=49_001,
                                        script_pubkey=plan.channels[0]
                                        .first.settlement_outputs[0]
                                        .script_pubkey,
                                    ),
                                )
                            }
                        )
                    }
                ),
            )
        }
    )
    with pytest.raises(ChannelInputParentError, match="settlement output"):
        asyncio.run(
            ChannelInputParentCoordinator(tmp_path / "mismatch").execute(broken, _endpoints())
        )
    with pytest.raises(ChannelInputParentError, match="mutated parent"):
        asyncio.run(
            ChannelInputParentCoordinator(tmp_path / "mutated").execute(
                _plan(), _endpoints(mutate_final=True)
            )
        )


def test_rejects_channel_prevout_and_fee_share_mismatches(tmp_path: Path) -> None:
    plan = _plan()
    mismatched_prevout = plan.model_copy(
        update={
            "prevouts": (
                plan.prevouts[0].model_copy(update={"value_sat": 100_001}),
                plan.prevouts[1],
            )
        }
    )
    with pytest.raises(ChannelInputParentError, match="parent prevouts differ"):
        asyncio.run(
            ChannelInputParentCoordinator(tmp_path / "prevout").execute(
                mismatched_prevout, _endpoints()
            )
        )
    mismatched_script = plan.model_copy(
        update={
            "prevouts": (
                plan.prevouts[0].model_copy(update={"script_pubkey": "5120" + "99" * 32}),
                plan.prevouts[1],
            )
        }
    )
    with pytest.raises(ChannelInputParentError, match="parent prevouts differ"):
        asyncio.run(
            ChannelInputParentCoordinator(tmp_path / "script").execute(
                mismatched_script, _endpoints()
            )
        )

    excessive_shares = plan.model_copy(
        update={
            "channels": (
                plan.channels[0].model_copy(
                    update={
                        "first": plan.channels[0].first.model_copy(
                            update={"miner_fee_share_sat": 60_000}
                        ),
                        "second": plan.channels[0].second.model_copy(
                            update={"miner_fee_share_sat": 60_000}
                        ),
                    }
                ),
            )
        }
    )
    with pytest.raises(ChannelInputParentError, match="miner fee share differs"):
        asyncio.run(
            ChannelInputParentCoordinator(tmp_path / "fees").execute(excessive_shares, _endpoints())
        )


def test_restarts_from_prepare_failure_without_entering_signing(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    endpoints = _endpoints(fail_prepare_once=True)
    plan = _plan()
    with pytest.raises(ChannelInputParentError, match="progression failed"):
        asyncio.run(coordinator.execute(plan, endpoints))

    resumed = asyncio.run(coordinator.execute(plan, endpoints))
    assert resumed.state is ChannelInputParentState.FINAL_TX_PERSISTED
    assert endpoints["first"].begin_calls == endpoints["second"].begin_calls == 1


def test_recovers_after_durable_partial_without_restarting_attempt(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    endpoints = _endpoints(crash_after_sign=True)
    with pytest.raises(ChannelInputParentError, match="progression failed"):
        asyncio.run(coordinator.execute(_plan(), endpoints))

    resumed = asyncio.run(ChannelInputParentCoordinator(tmp_path).resume(SESSION_ID, endpoints))
    assert resumed.state is ChannelInputParentState.FINAL_TX_PERSISTED
    assert endpoints["first"].begin_calls == 1
    assert endpoints["first"].sign_calls == 1
    assert endpoints["second"].begin_calls == 1


@pytest.mark.parametrize("contradiction", ["nonce", "partial"])
def test_rejects_durable_signing_evidence_contradictions(
    tmp_path: Path, contradiction: str
) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    endpoints = _endpoints(
        crash_after_sign=contradiction == "nonce", mutate_final=contradiction == "partial"
    )
    with pytest.raises(ChannelInputParentError):
        asyncio.run(coordinator.execute(_plan(), endpoints))
    if contradiction == "nonce":
        endpoints["first"].status_remote_nonce = b"\x09" * 66
    else:
        endpoints["first"].status_partial = b"\x09" * 32

    with pytest.raises(ChannelInputParentError, match="status changed"):
        asyncio.run(coordinator.resume(SESSION_ID, endpoints))


def test_rejects_same_txid_with_different_chain_witness(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    record = asyncio.run(coordinator.execute(_plan(), _endpoints()))
    assert record.final_tx_hex is not None and record.final_txid is not None
    parsed = parse_transaction(record.final_tx_hex)
    witnesses = parsed.witnesses
    witnesses[1] = [b"\x00" * len(witnesses[1][0]), witnesses[1][1]]
    different_witness = serialize_transaction(
        parsed.version, parsed.inputs, parsed.outputs, parsed.locktime, witnesses
    ).hex()
    assert get_txid(different_witness) == record.final_txid
    chain = FakeChain()
    chain.transaction = FakeTransaction(record.final_txid, different_witness, confirmations=0)

    with pytest.raises(ChannelInputParentError, match="contradictory"):
        asyncio.run(coordinator.observe(record.plan.session_id, chain))


def test_cancellation_only_before_channel_signing(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    endpoints = _endpoints(crash_after_sign=True)
    with pytest.raises(ChannelInputParentError):
        asyncio.run(coordinator.execute(_plan(), endpoints))
    with pytest.raises(ChannelInputParentError, match="forbidden"):
        asyncio.run(coordinator.cancel(SESSION_ID, endpoints))

    coordinator = ChannelInputParentCoordinator(tmp_path / "pre-sign")
    endpoints = _endpoints()
    plan = _plan(invalid_witness=True)
    with pytest.raises(ChannelInputParentError):
        asyncio.run(coordinator.execute(plan, endpoints))
    canceled = asyncio.run(coordinator.cancel(plan.session_id, endpoints))
    assert canceled.state is ChannelInputParentState.CANCELED
    assert endpoints["first"].cancel_calls == endpoints["second"].cancel_calls == 1

    coordinator = ChannelInputParentCoordinator(tmp_path / "created")
    endpoints = _endpoints(fail_freeze_once=True)
    with pytest.raises(ChannelInputParentError, match="progression failed"):
        asyncio.run(coordinator.execute(_plan(), endpoints))
    canceled = asyncio.run(coordinator.cancel(SESSION_ID, endpoints))
    assert canceled.state is ChannelInputParentState.CANCELED
    assert endpoints["first"].cancel_calls == endpoints["second"].cancel_calls == 1


@pytest.mark.parametrize(
    "state",
    [
        ChannelInputParentState.CREATED,
        ChannelInputParentState.CHANNELS_FROZEN,
        ChannelInputParentState.PARENT_PREPARED,
        ChannelInputParentState.ORDINARY_SIGNED,
    ],
)
def test_restart_decision_cancels_pre_sign_states(
    tmp_path: Path, state: ChannelInputParentState
) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    plan = _plan()
    _save_record(coordinator, plan, state)

    action = asyncio.run(
        coordinator.decide_restart(SESSION_ID, _endpoints(), FakeChain(), current_height=0)
    )

    assert action is ChannelInputRestartAction.CANCEL_PRE_SIGN


def test_cancel_and_restart_decision_allow_reconciled_nonce_only_signing(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    endpoints = _endpoints(fail_sign_once=True)
    with pytest.raises(ChannelInputParentError, match="progression failed"):
        asyncio.run(coordinator.execute(_plan(), endpoints))

    action = asyncio.run(
        coordinator.decide_restart(SESSION_ID, endpoints, FakeChain(), current_height=0)
    )
    canceled = asyncio.run(coordinator.cancel(SESSION_ID, endpoints))

    assert action is ChannelInputRestartAction.CANCEL_PRE_SIGN
    assert canceled.state is ChannelInputParentState.CANCELED
    assert endpoints["first"].cancel_calls == endpoints["second"].cancel_calls == 1


def test_restart_decision_recovers_durable_partial_at_deadline(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    endpoints = _endpoints(crash_after_sign=True)
    with pytest.raises(ChannelInputParentError, match="progression failed"):
        asyncio.run(coordinator.execute(_plan(), endpoints))

    below_deadline = asyncio.run(
        coordinator.decide_restart(SESSION_ID, endpoints, FakeChain(), current_height=999_998)
    )
    at_deadline = asyncio.run(
        coordinator.decide_restart(SESSION_ID, endpoints, FakeChain(), current_height=999_999)
    )

    assert below_deadline is ChannelInputRestartAction.RESUME_EXACT_PARENT
    assert at_deadline is ChannelInputRestartAction.FORCE_CLOSE


def test_restart_decision_publishes_persisted_parent_before_deadline(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    record = asyncio.run(coordinator.execute(_plan(), _endpoints()))

    below_deadline = asyncio.run(
        coordinator.decide_restart(
            record.plan.session_id, _endpoints(), FakeChain(), current_height=999_998
        )
    )
    at_deadline = asyncio.run(
        coordinator.decide_restart(
            record.plan.session_id, _endpoints(), FakeChain(), current_height=999_999
        )
    )

    assert below_deadline is ChannelInputRestartAction.PUBLISH_EXACT_PARENT
    assert at_deadline is ChannelInputRestartAction.FORCE_CLOSE


def test_restart_decision_adopts_exact_observed_parent_before_final_persistence(
    tmp_path: Path,
) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    plan = _plan()
    endpoints = _endpoints(crash_after_sign=True)
    with pytest.raises(ChannelInputParentError, match="progression failed"):
        asyncio.run(coordinator.execute(plan, endpoints))
    final_hex = _final_parent(plan)
    chain = FakeChain()
    chain.transaction = FakeTransaction(get_txid(final_hex), final_hex, confirmations=1)

    action = asyncio.run(coordinator.decide_restart(SESSION_ID, endpoints, chain, current_height=0))
    record = coordinator._store.load(coordinator.record_id(SESSION_ID))

    assert action is ChannelInputRestartAction.NONE
    assert record is not None
    assert record.state is ChannelInputParentState.CONFIRMED
    assert record.final_tx_hex == final_hex
    assert record.channels[0].witness is not None


@pytest.mark.parametrize("kind", ["malformed", "mutated", "invalid_witness"])
def test_restart_decision_rejects_invalid_observed_parent_before_persistence(
    tmp_path: Path, kind: str
) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    plan = _plan()
    endpoints = _endpoints(crash_after_sign=True)
    with pytest.raises(ChannelInputParentError):
        asyncio.run(coordinator.execute(plan, endpoints))
    final_hex = _final_parent(plan)
    if kind == "malformed":
        raw = "00"
    else:
        transaction = parse_transaction(final_hex)
        if kind == "mutated":
            transaction.outputs[0] = TxOutput(
                value=transaction.outputs[0].value + 1, script=transaction.outputs[0].script
            )
        else:
            transaction.witnesses[0] = [b"\x00" * len(transaction.witnesses[0][0])]
        raw = serialize_transaction(
            transaction.version,
            transaction.inputs,
            transaction.outputs,
            transaction.locktime,
            transaction.witnesses,
        ).hex()
    chain = FakeChain()
    chain.transaction = FakeTransaction(get_txid(plan.unsigned_parent_hex), raw, confirmations=0)

    with pytest.raises(ChannelInputParentError):
        asyncio.run(coordinator.decide_restart(SESSION_ID, endpoints, chain, current_height=0))


@pytest.mark.parametrize(
    "terminal_state",
    [
        ChannelInputParentState.CANCELED,
        ChannelInputParentState.OBSERVED,
        ChannelInputParentState.CONFIRMED,
    ],
)
def test_restart_decision_queries_chain_before_lnd_and_skips_terminal_rpcs(
    tmp_path: Path, terminal_state: ChannelInputParentState
) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    calls: list[str] = []
    endpoints = _endpoints(crash_after_sign=True, calls=calls)
    with pytest.raises(ChannelInputParentError):
        asyncio.run(coordinator.execute(_plan(), endpoints))
    calls.clear()

    action = asyncio.run(
        coordinator.decide_restart(SESSION_ID, endpoints, FakeChain(calls), current_height=0)
    )

    assert action is ChannelInputRestartAction.RESUME_EXACT_PARENT
    assert calls == ["chain", "status", "status"]
    terminal = ChannelInputParentCoordinator(tmp_path / "terminal")
    plan = _plan()
    _save_record(terminal, plan, terminal_state)
    terminal_calls: list[str] = []
    assert (
        asyncio.run(
            terminal.decide_restart(
                SESSION_ID,
                _endpoints(calls=terminal_calls),
                FakeChain(terminal_calls),
                current_height=0,
            )
        )
        is ChannelInputRestartAction.NONE
    )
    assert terminal_calls == []


@pytest.mark.parametrize("current_height", [-1, 0x1_0000_0000, True, 1.5])
def test_restart_decision_rejects_invalid_current_height(
    tmp_path: Path, current_height: object
) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    _save_record(coordinator, _plan(), ChannelInputParentState.CREATED)

    with pytest.raises(ChannelInputParentError, match="uint32"):
        asyncio.run(
            coordinator.decide_restart(
                SESSION_ID,
                _endpoints(),
                FakeChain(),
                current_height=current_height,  # type: ignore[arg-type]
            )
        )
