from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    analyze_coinjoin_outputs,
    get_txid,
    parse_transaction,
    pubkey_to_p2wpkh_script,
    serialize_transaction,
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
    ChannelInputParentCoordinator,
    ChannelInputParentError,
    ChannelInputParentPlan,
    ChannelInputParentState,
    EndpointContribution,
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
    def __init__(self) -> None:
        self.transaction: FakeTransaction | None = None
        self.broadcasts: list[str] = []

    async def get_transaction(self, txid: str) -> FakeTransaction | None:
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
        fail_freeze_once: bool = False,
        fail_prepare_once: bool = False,
        mutate_final: bool = False,
    ) -> None:
        self.local_key = local_key
        self.remote_key = remote_key
        self.funding_key = funding_key
        self.first = first
        self.shared_signature = shared_signature
        self.crash_after_sign = crash_after_sign
        self.fail_freeze_once = fail_freeze_once
        self.fail_prepare_once = fail_prepare_once
        self.mutate_final = mutate_final
        self.parent_hex = ""
        self.index = 0
        self.attempt: LndEscrowSigningAttempt | None = None
        self.partial: bytes | None = None
        self.status_remote_nonce: bytes | None = None
        self.status_partial: bytes | None = None
        self.begin_calls = 0
        self.sign_calls = 0
        self.cancel_calls = 0

    def _snapshot(self, session_id: str, channel_id: str) -> FrozenChannelSnapshot:
        del session_id
        local = self.local_key.public_key.format(compressed=True)
        remote = self.remote_key.public_key.format(compressed=True)
        return FrozenChannelSnapshot(
            session_id="fake",
            channel_id=channel_id,
            funding_outpoint=EscrowOutpoint(txid="11" * 32, vout=0),
            funding_scriptpubkey=b"\x51\x20" + self.funding_key.public_key_xonly.format(),
            peer_node_pubkey=remote,
            local_funding_pubkey=local,
            remote_funding_pubkey=remote,
            capacity_sat=100_000,
            local_claim_sat=50_000,
            remote_claim_sat=50_000,
        )

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> FrozenChannelSnapshot:
        if self.fail_freeze_once:
            self.fail_freeze_once = False
            raise RuntimeError("simulated freeze interruption")
        return self._snapshot(session_id, channel_id)

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: tuple[WitnessUtxo, ...],
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


def _plan(*, invalid_witness: bool = False) -> ChannelInputParentPlan:
    funding_key = PrivateKey(b"\x05" * 32)
    ordinary_pubkey = ORDINARY_KEY.public_key.format(compressed=True)
    ordinary_script = pubkey_to_p2wpkh_script(ordinary_pubkey)
    inputs = [TxInput.from_hex("11" * 32, 0), TxInput.from_hex("22" * 32, 1)]
    outputs = [
        TxOutput(value=49_000, script=b"\x51\x20" + b"\x21" * 32),
        TxOutput(value=49_000, script=b"\x51\x20" + b"\x22" * 32),
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
        session_id="parent-test-session",
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
                    settlement_outputs=(
                        SettlementOutput(
                            output_index=0, value_sat=49_000, script_pubkey=outputs[0].script.hex()
                        ),
                    ),
                    miner_fee_share_sat=1_000,
                ),
                second=EndpointContribution(
                    endpoint_id="second",
                    settlement_outputs=(
                        SettlementOutput(
                            output_index=1, value_sat=49_000, script_pubkey=outputs[1].script.hex()
                        ),
                    ),
                    miner_fee_share_sat=1_000,
                ),
            ),
        ),
        ordinary_witnesses=(
            OrdinaryWitness(input_index=1, witness=(signature.hex(), ordinary_pubkey.hex())),
        ),
    )


def _endpoints(
    *,
    crash_after_sign: bool = False,
    fail_freeze_once: bool = False,
    fail_prepare_once: bool = False,
    mutate_final: bool = False,
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
            fail_freeze_once=fail_freeze_once,
            fail_prepare_once=fail_prepare_once,
            mutate_final=mutate_final,
        ),
        "second": FakeEndpoint(
            local_key=second_key,
            remote_key=first_key,
            funding_key=funding_key,
            first=False,
            shared_signature=shared_signature,
        ),
    }


def test_happy_path_persists_final_bytes_and_publishes_query_first(tmp_path: Path) -> None:
    coordinator = ChannelInputParentCoordinator(tmp_path)
    endpoints = _endpoints()
    record = asyncio.run(coordinator.execute(_plan(), endpoints))

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
    with pytest.raises(ChannelInputParentError, match="channel prevout"):
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
    with pytest.raises(ChannelInputParentError, match="channel prevout"):
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
    with pytest.raises(ChannelInputParentError, match="below channel fee shares"):
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

    resumed = asyncio.run(
        ChannelInputParentCoordinator(tmp_path).resume("parent-test-session", endpoints)
    )
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
        asyncio.run(coordinator.resume("parent-test-session", endpoints))


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
        asyncio.run(coordinator.cancel("parent-test-session", endpoints))

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
    canceled = asyncio.run(coordinator.cancel("parent-test-session", endpoints))
    assert canceled.state is ChannelInputParentState.CANCELED
    assert endpoints["first"].cancel_calls == endpoints["second"].cancel_calls == 1
