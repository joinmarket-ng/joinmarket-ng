from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    get_txid,
    scriptpubkey_to_address,
    serialize_transaction,
)
from jmcore.channel_ring import ChannelRingConfig
from jmcore.channel_ring_store import (
    RingLifecycleState,
    RingParticipantStore,
)
from jmcore.cofunded_ring import (
    BackendLimits,
    ChannelPolicy,
    EndpointRole,
    LocalContribution,
    ManifestOutput,
    PolicyBounds,
    PrivateEdgePlan,
    PrivateParticipant,
    ReadinessAttestation,
    RingCancelPayload,
    RingEdge,
    RingHelloPayload,
    RingInvitePayload,
    RingKeyPair,
    RingManifest,
    RingOpenPayload,
    RingPlanAckPayload,
    RingPlanPayload,
    RingPreparedPayload,
    RingPrivateHelloPayload,
    RingPrivatePlanPayload,
    RingReadySetAckPayload,
    RingReadySetPayload,
    RingSignAckPayload,
    RingSignPayload,
    RingUnsignedPayload,
    SignedReadinessAttestation,
    contact_public_key,
    encode_ring_message,
    manifest_hash,
    sign_attestation,
    sign_payload,
)
from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.models import Offer, OfferType
from jmcore.network import ONION_HOSTID
from jmcore.protocol import parse_jm_message
from jmswap.channel_ring import InitializedChannelRingBackend
from jmswap.lnd import (
    AcceptorObservation,
    FundingNegotiation,
    LndNodeInfo,
    PendingChannelObservation,
    VerifiedChannelRetirementOutcome,
    VerifiedChannelRetirementStatus,
    VerifiedFunding,
    WitnessUtxo,
    build_unsigned_psbt,
)
from jmswap.lnd import (
    EndpointRole as LndEndpointRole,
)
from jmwallet.backends.base import UTXO, Transaction
from jmwallet.wallet.models import UTXOInfo
from pydantic import ValidationError

from maker.bot import MakerBot
from maker.channel_ring import (
    MakerRingError,
    MakerRingParticipant,
    _chain_hash,
    reconcile_ring_records,
)
from maker.coinjoin import CoinJoinSession as MakerCoinJoinSession
from maker.coinjoin import CoinJoinState
from maker.maker_session import MakerSession


def _secret(index: int) -> bytes:
    return index.to_bytes(32, "big")


def test_regtest_chain_hash_matches_genesis_block() -> None:
    genesis = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
    assert _chain_hash("regtest") == bytes.fromhex(genesis)[::-1]


def _key(index: int) -> str:
    return RingKeyPair.from_secret(_secret(index)).public_key


def _node(index: int) -> str:
    return bytes(CKey.from_secret_bytes(_secret(index)).pub).hex()


ONION = "a" * 56 + ".onion:9735"
LOCAL_NODE = _node(30)
LOCAL_EQUAL_SCRIPT = "5120" + "91" * 32
LOCAL_CHANGE_SCRIPT = "5120" + "92" * 32
OUTGOING_SCRIPT = "5120" + "a1" * 32


def _policy() -> ChannelPolicy:
    return ChannelPolicy(
        fundee_csv_delay=144,
        fundee_reserve=10_000,
        min_depth=3,
        opener_csv_delay=144,
        opener_reserve=10_000,
    )


def _limits() -> BackendLimits:
    return BackendLimits(
        network="regtest",
        offer_type="tr0absoffer",
        min_channel_capacity=500_000,
        max_channel_capacity=2_000_000,
        max_push_amount=499_999,
        dust_limit=354,
        max_reserve=50_000,
        max_commitment_fee=20_000,
        max_pending_channels=4,
    )


def _participant(key: str, node: str) -> PrivateParticipant:
    return PrivateParticipant(
        participant_key=key,
        node_id=node,
        onion_endpoint=ONION,
        backend_limits=_limits(),
    )


def _config(tmp_path: Path) -> ChannelRingConfig:
    return ChannelRingConfig(
        enabled=True,
        lnd_grpc_url="https://127.0.0.1:10009",
        lnd_tls_cert_path=tmp_path / "tls.cert",
        lnd_macaroon_path=tmp_path / "admin.macaroon",
        onion_endpoint=ONION,
        min_channel_capacity=500_000,
        max_channel_capacity=2_000_000,
        max_push=499_999,
        opener_reserve=10_000,
        fundee_reserve=10_000,
        maximum_commitment_fee=20_000,
        spendable_margin=10_000,
        allowed_csv_delays=(144,),
        minimum_csv_delay=144,
        maximum_csv_delay=144,
        open_timeout_seconds=10.0,
        phase_timeout_seconds=10.0,
        readiness_timeout_seconds=10.0,
        persistence_directory=tmp_path / "rings",
    )


class FakeLnd:
    def __init__(self) -> None:
        self.incoming: asyncio.Future[AcceptorObservation] | None = None
        self.canceled: list[str] = []
        self.retired: list[str] = []
        self.resumed_outgoing: list[FundingNegotiation] = []
        self.resumed_verified: list[tuple[VerifiedFunding | None, str | None]] = []
        self.resumed_incoming: list[object] = []
        self.resumed_incoming_points: list[str | None] = []
        self.pending_points: list[str] = []
        self.verified: VerifiedFunding | None = None
        self.acceptor_timeouts: list[float] = []

    async def run_channel_acceptor(
        self,
        expected: Any,
        bounds: Any,
        *,
        timeout_seconds: float,
        ready: asyncio.Event | None = None,
    ) -> AcceptorObservation:
        del bounds
        self.acceptor_timeouts.append(timeout_seconds)
        self.incoming = asyncio.get_running_loop().create_future()
        if ready is not None:
            ready.set()
        return await self.incoming

    async def start_external_channel(self, request: Any) -> FundingNegotiation:
        assert request.capacity_sat >= 900_000
        assert request.push_sat == 300_000
        assert request.pending_channel_id == bytes.fromhex("12" * 32)
        assert self.incoming is not None
        if not self.incoming.done():
            self.incoming.set_result(
                AcceptorObservation(
                    pending_channel_id=bytes.fromhex("11" * 32),
                    opener_node_id=_node(20),
                    capacity_sat=1_000_000,
                    push_msat=400_000_000,
                )
            )
        return FundingNegotiation(
            pending_channel_id=request.pending_channel_id,
            peer_node_id=request.peer_node_id,
            funding_address=scriptpubkey_to_address(bytes.fromhex(OUTGOING_SCRIPT), "regtest"),
            funding_script_pubkey=bytes.fromhex(OUTGOING_SCRIPT),
            capacity_sat=request.capacity_sat,
            push_sat=request.push_sat,
            opener_reserve_sat=request.opener_reserve_sat,
            fundee_reserve_sat=request.fundee_reserve_sat,
            opener_csv_delay=request.opener_csv_delay,
            fundee_csv_delay=request.fundee_csv_delay,
            min_depth=request.min_depth,
        )

    async def verify_external_funding(
        self,
        negotiation: FundingNegotiation,
        raw_transaction_hex: str,
        funding_txid: str,
        funding_vout: int,
        input_utxos: list[WitnessUtxo],
        *,
        timeout_seconds: float,
    ) -> VerifiedFunding:
        del timeout_seconds
        psbt, txid = build_unsigned_psbt(raw_transaction_hex, input_utxos)
        assert txid == funding_txid
        assert negotiation.funding_script_pubkey.hex() == OUTGOING_SCRIPT
        self.verified = VerifiedFunding(
            pending_channel_id=negotiation.pending_channel_id,
            funding_txid=funding_txid,
            funding_vout=funding_vout,
            unsigned_psbt=psbt,
        )
        return self.verified

    async def pending_channel_observation(
        self, expected: Any, role: LndEndpointRole, *, timeout_seconds: float
    ) -> PendingChannelObservation:
        del timeout_seconds
        opener = role is LndEndpointRole.OPENER
        return PendingChannelObservation(
            role=role,
            local_node_id=expected.opener_node_id if opener else expected.fundee_node_id,
            remote_node_id=expected.fundee_node_id if opener else expected.opener_node_id,
            channel_point=expected.channel_point,
            capacity_sat=expected.capacity_sat,
            local_balance_sat=(
                expected.opener_contribution_sat - 1_000 - 660
                if opener
                else expected.fundee_contribution_sat
            ),
            remote_balance_sat=(
                expected.fundee_contribution_sat
                if opener
                else expected.opener_contribution_sat - 1_000 - 660
            ),
            commit_fee_sat=1_000,
            commitment_overhead_sat=660,
            local_reserve_sat=10_000,
            remote_reserve_sat=10_000,
            commitment_type=7,
            initiator=1 if opener else 2,
            private=True,
        )

    async def cancel_external_channel(
        self, pending_channel_id: bytes, *, input_signatures_added: bool
    ) -> None:
        assert not input_signatures_added
        self.canceled.append(pending_channel_id.hex())

    async def retire_verified_external_channel(
        self, authorization: Any
    ) -> VerifiedChannelRetirementOutcome:
        self.retired.append(authorization.channel_point)
        return VerifiedChannelRetirementOutcome(
            status=VerifiedChannelRetirementStatus.ABANDONED,
            channel_point=authorization.channel_point,
            unsigned_txid=authorization.unsigned_txid,
        )

    def resume_external_channel(
        self,
        negotiation: FundingNegotiation,
        *,
        verified: VerifiedFunding | None = None,
        observed_channel_point: str | None = None,
    ) -> None:
        self.resumed_outgoing.append(negotiation)
        self.resumed_verified.append((verified, observed_channel_point))

    def resume_inbound_channel(
        self, expected: object, *, observed_channel_point: str | None = None
    ) -> None:
        self.resumed_incoming.append(expected)
        self.resumed_incoming_points.append(observed_channel_point)

    async def pending_channel_present(self, channel_point: str) -> bool:
        self.pending_points.append(channel_point)
        return True


class FakeChain:
    def __init__(self) -> None:
        self.utxos: dict[tuple[str, int], UTXO] = {}
        self.transactions: dict[str, Transaction] = {}
        self.broadcasts: list[str] = []
        self.block_height = 100

    async def get_block_height(self) -> int:
        return self.block_height

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        return self.utxos.get((txid, vout))

    async def get_transaction(self, txid: str) -> Transaction | None:
        return self.transactions.get(txid)

    async def broadcast_transaction(self, tx_hex: str) -> str:
        self.broadcasts.append(tx_hex)
        return get_txid(tx_hex)

    @staticmethod
    def has_mempool_access() -> bool:
        return True

    @staticmethod
    def can_get_confirmations_by_txid() -> bool:
        return True


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.config = _config(tmp_path)
        self.lnd = FakeLnd()
        self.chain = FakeChain()
        self.store = RingParticipantStore(
            self.config.persistence_path(tmp_path),
            max_active_sessions=4,
            max_verified_sessions=2,
        )
        self.offer = Offer(
            counterparty="maker",
            ordertype=OfferType.TR0_ABSOLUTE,
            oid=0,
            minsize=100_000,
            maxsize=5_000_000,
            txfee=2_000,
            cjfee=1_000,
        )
        local_utxo = UTXOInfo(
            txid="01" * 32,
            vout=0,
            value=2_001_000,
            address="bcrt1plocal",
            confirmations=10,
            scriptpubkey="5120" + "81" * 32,
            path="m/86'/1'/0'/0/0",
            mixdepth=0,
        )
        self.session = SimpleNamespace(
            taker_nick="taker",
            commitment=bytes.fromhex("99" * 32),
            offer=self.offer,
            amount=1_000_000,
            our_utxos={(local_utxo.txid, local_utxo.vout): local_utxo},
            cj_address=scriptpubkey_to_address(bytes.fromhex(LOCAL_EQUAL_SCRIPT), "regtest"),
            change_address=scriptpubkey_to_address(bytes.fromhex(LOCAL_CHANGE_SCRIPT), "regtest"),
        )
        self.chain.utxos[(local_utxo.txid, local_utxo.vout)] = UTXO(
            txid=local_utxo.txid,
            vout=local_utxo.vout,
            value=local_utxo.value,
            address=local_utxo.address,
            confirmations=local_utxo.confirmations,
            scriptpubkey=local_utxo.scriptpubkey,
        )
        self.initialized = InitializedChannelRingBackend(
            backend=self.lnd,  # type: ignore[arg-type]
            node_info=LndNodeInfo(
                identity_pubkey=LOCAL_NODE,
                version="0.21.1-beta",
                network="regtest",
                synced_to_chain=True,
                wallet_synced=True,
                feature_bits=frozenset({81}),
                advertised_uris=(f"{LOCAL_NODE}@{ONION}",),
            ),
            onion_endpoint=ONION,
            backend_limits=_limits(),
        )
        self.machine = MakerRingParticipant(
            self.session,  # type: ignore[arg-type]
            config=self.config,
            store=self.store,
            initialized_backend=self.initialized,
            chain_backend=self.chain,  # type: ignore[arg-type]
        )
        self.taker_secret = _secret(10)
        self.taker_key = _key(10)
        self.other_secrets = {_key(index): _secret(index) for index in (10, 12, 13)}

    def signed(self, payload: Any) -> Any:
        return sign_payload(payload, self.taker_secret)

    def invite(self, *, revision: int = 0) -> RingInvitePayload:
        return self.signed(
            RingInvitePayload(
                round_nonce="aa" * 32,
                revision=revision,
                signer_key=self.taker_key,
                network="regtest",
                offer_type="tr0absoffer",
                expiry=int(time.time()) + 60,
                policy_bounds=PolicyBounds(
                    min_csv_delay=144,
                    max_csv_delay=144,
                    min_depth=3,
                    max_depth=3,
                    min_reserve=10_000,
                    max_reserve=10_000,
                ),
            )
        )

    def record(self) -> Any:
        return self.machine._record()

    def plan(self, *, residual: int = 1_000_000) -> RingPlanPayload:
        local = self.record().ring_public_key
        cycle = [self.taker_key, local, _key(12), _key(13)]
        outgoing = residual - 400_000
        return self.signed(
            RingPlanPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=self.taker_key,
                network="regtest",
                offer_type="tr0absoffer",
                cycle_keys=cycle,
                position=1,
                contribution=LocalContribution(
                    participant_key=local,
                    residual=residual,
                    outgoing=outgoing,
                    incoming=400_000,
                ),
                predecessor=_participant(self.taker_key, _node(20)),
                successor=_participant(_key(12), _node(22)),
                incoming_edge=PrivateEdgePlan(
                    edge_id="21" * 32,
                    pending_channel_id="11" * 32,
                    opener_key=self.taker_key,
                    acceptor_key=local,
                    capacity=1_000_000,
                    push_amount=400_000,
                    policy=_policy(),
                ),
                outgoing_edge=PrivateEdgePlan(
                    edge_id="22" * 32,
                    pending_channel_id="12" * 32,
                    opener_key=local,
                    acceptor_key=_key(12),
                    capacity=outgoing + 300_000,
                    push_amount=300_000,
                    policy=_policy(),
                ),
            )
        )

    async def through_open(self) -> None:
        await self.machine.handle(self.invite())
        acknowledgments = await self.machine.handle(self.plan())
        assert isinstance(acknowledgments[0], RingPlanAckPayload)
        plan_hash = acknowledgments[0].plan_hash
        await self.machine.handle(
            self.signed(
                RingOpenPayload(
                    round_nonce="aa" * 32,
                    revision=0,
                    signer_key=self.taker_key,
                    plan_hash=plan_hash,
                )
            )
        )

    async def unsigned(
        self,
        *,
        outgoing_script: str = OUTGOING_SCRIPT,
        locktime: int = 0,
        outgoing_capacity_delta: int = 0,
    ) -> RingUnsignedPayload:
        record = self.record()
        local = record.ring_public_key
        assert record.plan is not None
        keys = [self.taker_key, local, _key(12), _key(13)]
        capacities = [
            record.plan.incoming_edge.capacity,
            record.plan.outgoing_edge.capacity + outgoing_capacity_delta,
            800_000,
            700_000,
        ]
        pending = ["11" * 32, "12" * 32, "13" * 32, "14" * 32]
        scripts = ["5120" + f"{index + 30:064x}" for index in range(4)]
        scripts[1] = outgoing_script
        edges = [
            RingEdge(
                opener_key=keys[index],
                acceptor_key=keys[(index + 1) % 4],
                pending_channel_id=pending[index],
                capacity=capacities[index],
                output_index=index + 4,
                script_pubkey=scripts[index],
                policy=_policy(),
            )
            for index in range(4)
        ]
        equal_scripts = ["5120" + f"{index + 100:064x}" for index in range(4)]
        equal_scripts[1] = LOCAL_EQUAL_SCRIPT
        outputs = [
            ManifestOutput(index=index, amount=1_000_000, script_pubkey=script)
            for index, script in enumerate(equal_scripts)
        ] + [
            ManifestOutput(
                index=edge.output_index,
                amount=edge.capacity,
                script_pubkey=edge.script_pubkey,
            )
            for edge in edges
        ]
        input_specs = [
            ("01" * 32, 0, 2_001_000, "5120" + "81" * 32),
            ("02" * 32, 0, 2_000_000, "5120" + "82" * 32),
            ("03" * 32, 0, 2_000_000, "5120" + "83" * 32),
            ("04" * 32, 0, 2_000_000, "5120" + "84" * 32),
        ]
        tx = serialize_transaction(
            2,
            [
                TxInput.from_hex(
                    txid,
                    vout,
                    sequence=0xFFFFFFFF if locktime == 0 else 0xFFFFFFFE,
                )
                for txid, vout, _, _ in input_specs
            ],
            [TxOutput.from_hex(item.script_pubkey, item.amount) for item in outputs],
            locktime,
        )
        for txid, vout, value, script in input_specs[1:]:
            self.chain.utxos[(txid, vout)] = UTXO(
                txid=txid,
                vout=vout,
                value=value,
                address="",
                confirmations=10,
                scriptpubkey=script,
            )
        witnesses = [
            WitnessUtxo(value_sat=value, script_pubkey=bytes.fromhex(script))
            for _, _, value, script in input_specs
        ]
        psbt, txid = build_unsigned_psbt(tx.hex(), witnesses)
        manifest = RingManifest(
            network="regtest",
            round_nonce="aa" * 32,
            revision=0,
            unsigned_tx_hash=hashlib.sha256(tx).hexdigest(),
            unsigned_txid=txid,
            participant_keys=keys,
            edges=edges,
            equal_output_indices=[0, 1, 2, 3],
            outputs=outputs,
        )
        return self.signed(
            RingUnsignedPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=self.taker_key,
                unsigned_tx=tx.hex(),
                psbt=base64.b64encode(psbt).decode(),
                manifest=manifest,
            )
        )

    def ready_set(self, manifest: RingManifest) -> RingReadySetPayload:
        record = self.record()
        local = set(record.local_readiness)
        attestations: list[SignedReadinessAttestation] = list(local)
        local_slots = {
            (item.attestation.edge, item.attestation.role, item.attestation.signer_key)
            for item in local
        }
        for edge in manifest.edges:
            for role, signer in (
                (EndpointRole.OPENER, edge.opener_key),
                (EndpointRole.FUNDEE, edge.acceptor_key),
            ):
                if (edge.pending_channel_id, role, signer) in local_slots:
                    continue
                attestation = ReadinessAttestation(
                    edge=edge.pending_channel_id,
                    manifest_hash=manifest_hash(manifest).hex(),
                    revision=manifest.revision,
                    role=role,
                    round_nonce=manifest.round_nonce,
                    signer_key=signer,
                    state_hash="55" * 32,
                )
                attestations.append(sign_attestation(attestation, self.other_secrets[signer]))
        return self.signed(
            RingReadySetPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=self.taker_key,
                manifest=manifest,
                attestations=attestations,
            )
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def test_complete_participant_flow_and_signature_persistence(harness: Harness) -> None:
    hello = await harness.machine.handle(harness.invite())
    assert isinstance(hello[0], RingHelloPayload)
    assert hello[0].participant.node_id == LOCAL_NODE
    assert harness.record().state is RingLifecycleState.INVITED
    acknowledgments = await harness.machine.handle(harness.plan())
    assert isinstance(acknowledgments[0], RingPlanAckPayload)
    assert harness.record().state is RingLifecycleState.ACCEPTOR_ARMED
    prepared = await harness.machine.handle(
        harness.signed(
            RingOpenPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=harness.taker_key,
                plan_hash=acknowledgments[0].plan_hash,
            )
        )
    )
    assert isinstance(prepared[0], RingPreparedPayload)
    assert prepared[0].outgoing.script_pubkey == OUTGOING_SCRIPT
    assert "script_pubkey" not in prepared[0].incoming.model_dump()

    unsigned = await harness.unsigned()
    ready = await harness.machine.handle(unsigned)
    assert len(ready) == 2
    assert harness.record().state is RingLifecycleState.READY
    ready_acks = await harness.machine.handle(harness.ready_set(unsigned.manifest))
    assert isinstance(ready_acks[0], RingReadySetAckPayload)
    sign_acks = await harness.machine.handle(
        harness.signed(
            RingSignPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=harness.taker_key,
                manifest_hash=manifest_hash(unsigned.manifest).hex(),
                unsigned_tx_hash=unsigned.manifest.unsigned_tx_hash,
            )
        )
    )
    assert isinstance(sign_acks[0], RingSignAckPayload)
    harness.machine.prepare_coinjoin_signing(unsigned.unsigned_tx)
    created = harness.record()
    assert created.state is RingLifecycleState.SIGNING
    assert created.local_input_signature_created and not created.local_input_signature_sent
    harness.machine.mark_signatures_sent(["signature"])
    sent = harness.record()
    assert sent.state is RingLifecycleState.SIGNED
    assert sent.local_signatures == ("signature",)
    assert sent.local_input_signature_sent


async def test_order_signature_residual_and_replay_fail_closed(harness: Harness) -> None:
    with pytest.raises(MakerRingError, match="invitation"):
        await harness.machine.handle(harness.plan())

    invite = harness.invite()
    first = await harness.machine.handle(invite)
    assert await harness.machine.handle(invite) == first
    bad_signature = invite.model_copy(update={"sig": "00" * 64})
    with pytest.raises(MakerRingError, match="conflicting invitation|invalid BIP340"):
        await harness.machine.handle(bad_signature)

    with pytest.raises(MakerRingError, match="below advertised residual"):
        await harness.machine.handle(harness.plan(residual=999_999))
    good_plan = harness.plan()
    await harness.machine.handle(good_plan)
    changed = good_plan.model_copy(update={"sig": "01" * 64})
    with pytest.raises(MakerRingError, match="signature|replay"):
        await harness.machine.handle(changed)


async def test_rejected_plan_retires_revision_and_acknowledges_later_cancel(
    harness: Harness,
) -> None:
    invite = harness.invite()
    bounds = invite.policy_bounds.model_copy(update={"max_reserve": 15_000})
    invite = harness.signed(invite.model_copy(update={"policy_bounds": bounds, "sig": "00" * 64}))
    await harness.machine.handle(invite)
    plan = harness.plan()
    policy = plan.incoming_edge.policy.model_copy(update={"fundee_reserve": 15_000})
    incoming = plan.incoming_edge.model_copy(update={"policy": policy})
    rejected = harness.signed(plan.model_copy(update={"incoming_edge": incoming, "sig": "00" * 64}))

    with pytest.raises(MakerRingError, match="configured policy"):
        await harness.machine.handle(rejected)

    responses = harness.machine.reject_plan(rejected)
    assert isinstance(responses[0], RingCancelPayload)
    assert responses[0].reason_code == "participant_rejection"
    assert harness.record().state is RingLifecycleState.RETIRED
    assert await harness.machine.handle(rejected) == responses

    coordinator_cancel = harness.signed(
        RingCancelPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=harness.taker_key,
            reason_code="coordinator_failure",
        )
    )
    acknowledgments = await harness.machine.handle(coordinator_cancel)
    assert isinstance(acknowledgments[0], RingCancelPayload)
    assert acknowledgments[0].reason_code == "coordinator_failure"
    assert harness.record().state is RingLifecycleState.RETIRED


async def test_script_and_residual_substitution_and_wrong_open_binding_are_rejected(
    harness: Harness,
) -> None:
    await harness.machine.handle(harness.invite())
    ack = await harness.machine.handle(harness.plan())
    assert isinstance(ack[0], RingPlanAckPayload)
    with pytest.raises(MakerRingError, match="plan hash"):
        await harness.machine.handle(
            harness.signed(
                RingOpenPayload(
                    round_nonce="aa" * 32,
                    revision=0,
                    signer_key=harness.taker_key,
                    plan_hash="ff" * 32,
                )
            )
        )
    await harness.machine.handle(
        harness.signed(
            RingOpenPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=harness.taker_key,
                plan_hash=ack[0].plan_hash,
            )
        )
    )
    substituted = await harness.unsigned(outgoing_script="5120" + "ff" * 32)
    with pytest.raises(MakerRingError, match="substituted.*script"):
        await harness.machine.handle(substituted)
    residual_substitution = await harness.unsigned(outgoing_capacity_delta=-1)
    with pytest.raises(MakerRingError, match="substituted a local edge"):
        await harness.machine.handle(residual_substitution)


async def test_height_locktime_is_verified_before_external_psbt(harness: Harness) -> None:
    await harness.through_open()
    unsigned = await harness.unsigned(locktime=100)

    ready = await harness.machine.handle(unsigned)

    assert len(ready) == 2
    assert harness.lnd.verified is not None


async def test_future_height_locktime_is_rejected_before_external_psbt(harness: Harness) -> None:
    await harness.through_open()
    unsigned = await harness.unsigned(locktime=harness.chain.block_height + 13)

    with pytest.raises(MakerRingError, match="ahead of chain tip"):
        await harness.machine.handle(unsigned)

    assert harness.lnd.verified is None


async def test_paid_fee_above_advertised_raw_fee_preserves_acknowledged_residual(
    harness: Harness,
) -> None:
    await harness.machine.handle(harness.invite())
    plan = harness.plan(residual=1_001_000)
    acknowledgments = await harness.machine.handle(plan)
    assert isinstance(acknowledgments[0], RingPlanAckPayload)
    await harness.machine.handle(
        harness.signed(
            RingOpenPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=harness.taker_key,
                plan_hash=acknowledgments[0].plan_hash,
            )
        )
    )
    unsigned = await harness.unsigned()
    await harness.machine.handle(unsigned)

    record = harness.record()
    assert record.local_contribution == plan.contribution
    assert record.manifest is not None
    local_index = record.manifest.participant_keys.index(record.ring_public_key)
    outgoing = record.manifest.edges[local_index]
    assert outgoing.capacity - plan.outgoing_edge.push_amount == plan.contribution.outgoing
    assert plan.incoming_edge.push_amount == plan.contribution.incoming
    assert record.outgoing_pending_state is not None
    assert record.outgoing_pending_state.opener_balance == plan.contribution.outgoing - 1_660
    assert record.incoming_pending_state is not None
    assert record.incoming_pending_state.fundee_balance == plan.contribution.incoming


async def test_wrong_push_pending_id_psbt_and_incomplete_readiness_are_rejected(
    tmp_path: Path,
) -> None:
    wrong_push = Harness(tmp_path / "push")
    await wrong_push.machine.handle(wrong_push.invite())
    plan = wrong_push.plan()
    edge = plan.outgoing_edge.model_copy(update={"push_amount": 299_999})
    plan = wrong_push.signed(plan.model_copy(update={"outgoing_edge": edge, "sig": "00" * 64}))
    with pytest.raises(MakerRingError, match="outgoing edge"):
        await wrong_push.machine.handle(plan)

    wrong_pending = Harness(tmp_path / "pending")
    await wrong_pending.through_open()
    unsigned = await wrong_pending.unsigned()
    edges = list(unsigned.manifest.edges)
    edges[1] = edges[1].model_copy(update={"pending_channel_id": "ff" * 32})
    manifest = RingManifest(**{**unsigned.manifest.model_dump(), "edges": edges})
    changed = wrong_pending.signed(
        RingUnsignedPayload(
            **{
                **unsigned.model_dump(),
                "manifest": manifest,
                "sig": "00" * 64,
            }
        )
    )
    with pytest.raises(MakerRingError, match="substituted a local edge"):
        await wrong_pending.machine.handle(changed)

    wrong_psbt = Harness(tmp_path / "psbt")
    await wrong_psbt.through_open()
    unsigned = await wrong_psbt.unsigned()
    tx_inputs = [
        WitnessUtxo(value_sat=2_001_001, script_pubkey=bytes.fromhex("5120" + "81" * 32)),
        WitnessUtxo(value_sat=2_000_000, script_pubkey=bytes.fromhex("5120" + "82" * 32)),
        WitnessUtxo(value_sat=2_000_000, script_pubkey=bytes.fromhex("5120" + "83" * 32)),
        WitnessUtxo(value_sat=2_000_000, script_pubkey=bytes.fromhex("5120" + "84" * 32)),
    ]
    psbt, _ = build_unsigned_psbt(unsigned.unsigned_tx, tx_inputs)
    changed = wrong_psbt.signed(
        RingUnsignedPayload(
            **{
                **unsigned.model_dump(),
                "psbt": base64.b64encode(psbt).decode(),
                "sig": "00" * 64,
            }
        )
    )
    with pytest.raises(MakerRingError, match="PSBT prevouts"):
        await wrong_psbt.machine.handle(changed)

    incomplete = Harness(tmp_path / "ready")
    await incomplete.through_open()
    unsigned = await incomplete.unsigned()
    await incomplete.machine.handle(unsigned)
    ready_set = incomplete.ready_set(unsigned.manifest)
    with pytest.raises(ValidationError, match="at least 8|two endpoint attestations"):
        RingReadySetPayload(
            **{
                **ready_set.model_dump(),
                "attestations": ready_set.attestations[:-1],
            }
        )


async def test_cancel_before_verify_after_verify_and_after_sign_boundaries(tmp_path: Path) -> None:
    invited = Harness(tmp_path / "invited")
    await invited.machine.handle(invited.invite())
    cancel = invited.signed(
        RingCancelPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=invited.taker_key,
            reason_code="test_cancel",
        )
    )
    response = await invited.machine.handle(cancel)
    assert invited.record().state is RingLifecycleState.RETIRED
    assert await invited.machine.handle(cancel) == response

    planned = Harness(tmp_path / "planned")
    await planned.machine.handle(planned.invite())
    await planned.machine.handle(planned.plan())
    planned_cancel = planned.signed(
        RingCancelPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=planned.taker_key,
            reason_code="before_open",
            canceled_pending_ids=["11" * 32, "12" * 32],
        )
    )
    planned_response = await planned.machine.handle(planned_cancel)
    assert isinstance(planned_response[0], RingCancelPayload)
    assert planned.record().state is RingLifecycleState.RETIRED
    assert planned.record().outgoing_open_started is False
    assert planned.lnd.canceled == []
    assert planned_response[0].canceled_pending_ids == []

    prepared = Harness(tmp_path / "prepared")
    await prepared.through_open()
    prepared_cancel = prepared.signed(
        RingCancelPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=prepared.taker_key,
            reason_code="test_cancel",
            canceled_pending_ids=["11" * 32, "12" * 32],
        )
    )
    prepared_response = await prepared.machine.handle(prepared_cancel)
    assert isinstance(prepared_response[0], RingCancelPayload)
    assert prepared.record().state is RingLifecycleState.RETIRED
    assert prepared.lnd.canceled == ["12" * 32]
    assert prepared_response[0].canceled_pending_ids == ["12" * 32]

    verified = Harness(tmp_path / "verified")
    await verified.through_open()
    unsigned = await verified.unsigned()
    await verified.machine.handle(unsigned)
    verified_cancel = verified.signed(
        RingCancelPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=verified.taker_key,
            reason_code="test_cancel",
            canceled_pending_ids=["11" * 32, "12" * 32],
        )
    )
    await verified.machine.handle(verified_cancel)
    assert verified.record().state is RingLifecycleState.RETIRED
    assert len(verified.lnd.retired) == 2

    signed = Harness(tmp_path / "signed")
    await signed.through_open()
    unsigned = await signed.unsigned()
    await signed.machine.handle(unsigned)
    await signed.machine.handle(signed.ready_set(unsigned.manifest))
    await signed.machine.handle(
        signed.signed(
            RingSignPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=signed.taker_key,
                manifest_hash=manifest_hash(unsigned.manifest).hex(),
                unsigned_tx_hash=unsigned.manifest.unsigned_tx_hash,
            )
        )
    )
    with pytest.raises(MakerRingError, match="forbidden"):
        await signed.machine.handle(
            signed.signed(
                RingCancelPayload(
                    round_nonce="aa" * 32,
                    revision=0,
                    signer_key=signed.taker_key,
                    reason_code="too_late",
                )
            )
        )


async def test_partial_psbt_verification_requires_verified_retirement(harness: Harness) -> None:
    await harness.through_open()
    unsigned = await harness.unsigned()

    async def fail_observation(*args: Any, **kwargs: Any) -> PendingChannelObservation:
        del args, kwargs
        raise RuntimeError("pending observation failed")

    harness.lnd.pending_channel_observation = fail_observation  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="pending observation failed"):
        await harness.machine.handle(unsigned)

    verified = harness.record()
    assert verified.state is RingLifecycleState.PSBT_VERIFIED
    assert verified.unsigned_psbt is not None
    cancel = harness.signed(
        RingCancelPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=harness.taker_key,
            reason_code="partial_verify",
            canceled_pending_ids=["11" * 32, "12" * 32],
        )
    )
    await harness.machine.handle(cancel)
    assert harness.record().state is RingLifecycleState.RETIRED
    assert harness.lnd.canceled == []
    assert len(harness.lnd.retired) == 2


async def test_restart_rehydrates_prepared_and_rebroadcasts_exact_final_tx(
    harness: Harness,
) -> None:
    await harness.through_open()
    await reconcile_ring_records(
        harness.store,
        harness.initialized,
        harness.chain,  # type: ignore[arg-type]
        {},
    )
    assert len(harness.lnd.resumed_outgoing) == 1
    assert len(harness.lnd.resumed_incoming) == 1

    unsigned = await harness.unsigned()
    await harness.machine.handle(unsigned)
    await harness.machine.handle(harness.ready_set(unsigned.manifest))
    await harness.machine.handle(
        harness.signed(
            RingSignPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=harness.taker_key,
                manifest_hash=manifest_hash(unsigned.manifest).hex(),
                unsigned_tx_hash=unsigned.manifest.unsigned_tx_hash,
            )
        )
    )
    harness.machine.prepare_coinjoin_signing(unsigned.unsigned_tx)
    harness.machine.mark_signatures_sent(["signature"])
    await harness.machine.observe_final_transaction(unsigned.unsigned_tx)
    await reconcile_ring_records(
        harness.store,
        harness.initialized,
        harness.chain,  # type: ignore[arg-type]
        {},
    )
    assert harness.chain.broadcasts == [unsigned.unsigned_tx]

    harness.chain.transactions[unsigned.manifest.unsigned_txid] = Transaction(
        txid=unsigned.manifest.unsigned_txid,
        raw=unsigned.unsigned_tx,
        confirmations=1,
        block_height=100,
    )
    await reconcile_ring_records(
        harness.store,
        harness.initialized,
        harness.chain,  # type: ignore[arg-type]
        {},
    )
    assert harness.record().state is RingLifecycleState.CONFIRMED_OPEN
    assert harness.chain.broadcasts == [unsigned.unsigned_tx]


async def test_reconcile_escalation_skips_live_sessions(harness: Harness) -> None:
    await harness.through_open()
    unsigned = await harness.unsigned()
    await harness.machine.handle(unsigned)
    await harness.machine.handle(harness.ready_set(unsigned.manifest))
    await harness.machine.handle(
        harness.signed(
            RingSignPayload(
                round_nonce="aa" * 32,
                revision=0,
                signer_key=harness.taker_key,
                manifest_hash=manifest_hash(unsigned.manifest).hex(),
                unsigned_tx_hash=unsigned.manifest.unsigned_tx_hash,
            )
        )
    )
    harness.machine.prepare_coinjoin_signing(unsigned.unsigned_tx)
    live = harness.record()
    assert live.state is RingLifecycleState.SIGNING

    # A record belonging to a live in-process session is resumed but not escalated.
    await reconcile_ring_records(
        harness.store,
        harness.initialized,
        harness.chain,  # type: ignore[arg-type]
        {},
        active_session_identities=frozenset({live.taker_session_identity}),
    )
    assert harness.record().state is RingLifecycleState.SIGNING

    # The same record with no live session is restart wreckage and escalates.
    await reconcile_ring_records(
        harness.store,
        harness.initialized,
        harness.chain,  # type: ignore[arg-type]
        {},
    )
    assert harness.record().state is RingLifecycleState.RECOVERY_REQUIRED


async def test_reconcile_rearms_acceptor_with_configured_timeout(harness: Harness) -> None:
    await harness.machine.handle(harness.invite())
    await harness.machine.handle(harness.plan())
    assert harness.record().state is RingLifecycleState.ACCEPTOR_ARMED
    task = harness.machine.acceptor_task
    assert task is not None and not task.done()
    harness.machine.cancel_acceptor_task()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()

    tasks: dict[str, asyncio.Task[object]] = {}
    await reconcile_ring_records(
        harness.store,
        harness.initialized,
        harness.chain,  # type: ignore[arg-type]
        tasks,
        acceptor_timeout_seconds=123.0,
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert harness.lnd.acceptor_timeouts[-1] == 123.0
    for pending in tasks.values():
        pending.cancel()
    await asyncio.gather(*tasks.values(), return_exceptions=True)


@pytest.mark.parametrize(
    "state,enabled,validated",
    [
        (CoinJoinState.PUBKEY_SENT, True, True),
        (CoinJoinState.IOAUTH_SENT, False, False),
        (CoinJoinState.IOAUTH_SENT, True, False),
    ],
)
async def test_encrypted_ring_dispatch_requires_ioauth_and_enabled_validated_feature(
    state: CoinJoinState, enabled: bool, validated: bool
) -> None:
    inner = SimpleNamespace(
        state=state,
        taker_nick="taker",
        session_timeout_sec=300,
        validate_channel=lambda source: True,
    )
    session = MakerSession(inner)  # type: ignore[arg-type]
    bot = SimpleNamespace(
        config=SimpleNamespace(channel_ring=SimpleNamespace(enabled=enabled)),
        channel_ring_capability_validated=validated,
        _channel_ring_backend=None,
        _channel_ring_store=None,
    )
    await session.on_ring(bot, "ring ciphertext", "dir:test")  # type: ignore[arg-type]
    assert session.ring_participant is None


class _WireCrypto:
    is_encrypted = True

    def __init__(self, plaintext: str) -> None:
        self.plaintext = plaintext
        self.decrypted: list[str] = []

    def decrypt(self, encrypted: str) -> str:
        self.decrypted.append(encrypted)
        return self.plaintext

    @staticmethod
    def encrypt(message: str) -> str:
        del message
        return "ZW5jcnlwdGVk"


class _WireParticipant:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.handled: list[Any] = []

    async def handle(self, payload: Any) -> list[Any]:
        self.handled.append(payload)
        return [self.response]


class _RetiringCancelWireParticipant(_WireParticipant):
    def __init__(self, response: Any) -> None:
        super().__init__(response)
        self.record = SimpleNamespace(state=RingLifecycleState.INVITED, active=True)

    async def handle(self, payload: Any) -> list[Any]:
        self.handled.append(payload)
        self.record.state = RingLifecycleState.RETIRED
        self.record.active = False
        return [self.response]

    def _record(self) -> Any:
        return self.record


class _RejectingWireParticipant(_WireParticipant):
    def __init__(self, response: Any) -> None:
        super().__init__(response)
        self.record = SimpleNamespace(state=RingLifecycleState.INVITED, active=True)

    async def handle(self, payload: Any) -> list[Any]:
        self.handled.append(payload)
        raise MakerRingError("channel policy differs from local configured policy")

    def reject_plan(self, payload: RingPlanPayload) -> list[Any]:
        assert self.handled == [payload]
        self.record.state = RingLifecycleState.RETIRED
        self.record.active = False
        return [self.response]

    def _record(self) -> Any:
        return self.record


class _FailingCancelWireParticipant(_WireParticipant):
    def __init__(self, state: RingLifecycleState) -> None:
        super().__init__(None)
        self.record = SimpleNamespace(state=state, active=True)

    async def handle(self, payload: Any) -> list[Any]:
        self.handled.append(payload)
        raise MakerRingError("ring cancellation cannot safely retire")

    def _record(self) -> Any:
        return self.record


class _UnretiredCancelWireParticipant(_WireParticipant):
    def __init__(self, response: Any) -> None:
        super().__init__(response)
        self.record = SimpleNamespace(state=RingLifecycleState.RETIRING, active=True)

    def _record(self) -> Any:
        return self.record


def _wire_maker_bot(maker_identity: NickIdentity, selected: Any) -> Any:
    bot: Any = MakerBot.__new__(MakerBot)
    bot.config = SimpleNamespace(channel_ring=SimpleNamespace(enabled=True))
    bot.channel_ring_capability_validated = True
    bot._channel_ring_backend = object()
    bot._channel_ring_store = object()
    bot.directory_clients = {"selected": selected}
    bot.direct_connections = {}
    bot._generation = lambda generation_id: None
    bot.nick_identity = maker_identity
    bot.nick = maker_identity.nick
    bot._active_podle_outpoints = {}
    bot._reserved_commitments = set()
    return bot


@pytest.mark.parametrize("source", ["dir:selected", "direct"])
async def test_signed_ring_dispatch_replies_only_on_source_channel(
    harness: Harness, source: str
) -> None:
    taker_identity = NickIdentity()
    maker_identity = NickIdentity()
    request = harness.invite()
    crypto = _WireCrypto(encode_ring_message(request))
    inner = SimpleNamespace(
        state=CoinJoinState.IOAUTH_SENT,
        taker_nick=taker_identity.nick,
        session_timeout_sec=300,
        validate_channel=lambda received: received == source,
        crypto=crypto,
    )
    session = MakerSession(inner)  # type: ignore[arg-type]
    participant = _WireParticipant(request)
    session.ring_participant = participant  # type: ignore[assignment]
    selected = SimpleNamespace(send_private_message=AsyncMock())
    other = SimpleNamespace(send_private_message=AsyncMock())
    direct = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(
        config=SimpleNamespace(channel_ring=SimpleNamespace(enabled=True)),
        channel_ring_capability_validated=True,
        _channel_ring_backend=object(),
        _channel_ring_store=object(),
        directory_clients={"selected": selected, "other": other},
        direct_connections={taker_identity.nick: direct},
        nick_identity=maker_identity,
        nick=maker_identity.nick,
    )
    ciphertext = "Y2lwaGVydGV4dA=="
    signed = taker_identity.sign_message(ciphertext, ONION_HOSTID)

    await session.on_ring(bot, f"ring {signed}", source)  # type: ignore[arg-type]

    assert crypto.decrypted == [ciphertext]
    assert participant.handled == [request]
    if source == "dir:selected":
        selected.send_private_message.assert_awaited_once_with(
            taker_identity.nick, "ring", "ZW5jcnlwdGVk"
        )
        other.send_private_message.assert_not_awaited()
        direct.send.assert_not_awaited()
    else:
        selected.send_private_message.assert_not_awaited()
        other.send_private_message.assert_not_awaited()
        direct.send.assert_awaited_once()
        envelope = json.loads(direct.send.await_args.args[0])
        parsed = parse_jm_message(envelope["line"])
        assert parsed is not None
        ok, command, data = verify_signed_privmsg(parsed[0], parsed[2], ONION_HOSTID)
        assert (ok, command, data) == (True, "ring", "ZW5jcnlwdGVk")


async def test_signed_plan_rejection_releases_owned_podle_for_fresh_commitment(
    harness: Harness,
) -> None:
    taker_identity = NickIdentity()
    maker_identity = NickIdentity()
    await harness.machine.handle(harness.invite())
    request = harness.plan()
    response = harness.signed(
        RingCancelPayload(
            round_nonce=request.round_nonce,
            revision=request.revision,
            signer_key=harness.taker_key,
            reason_code="participant_rejection",
        )
    )
    crypto = _WireCrypto(encode_ring_message(request))
    release_inputs = Mock()
    outpoint = ("04" * 32, 0)
    podle_outpoint = ("05" * 32, 1)
    commitment = "06" * 32
    inner = SimpleNamespace(
        state=CoinJoinState.IOAUTH_SENT,
        taker_nick=taker_identity.nick,
        session_timeout_sec=300,
        validate_channel=lambda received: received == "dir:selected",
        crypto=crypto,
        wallet=SimpleNamespace(release_coinjoin_inputs=release_inputs),
        our_utxos={outpoint: object()},
        input_lock_owner="maker:round-owner",
        commitment=bytes.fromhex(commitment),
    )
    session = MakerSession(inner)  # type: ignore[arg-type]
    session.ring_participant = _RejectingWireParticipant(response)  # type: ignore[assignment]
    selected = SimpleNamespace(send_private_message=AsyncMock())
    bot = _wire_maker_bot(maker_identity, selected)
    bot._reserved_commitments.add(commitment)
    assert bot._reserve_podle_outpoint(podle_outpoint, session) is True
    ciphertext = "Y2lwaGVydGV4dA=="
    signed = taker_identity.sign_message(ciphertext, ONION_HOSTID)

    await session.on_ring(bot, f"ring {signed}", "dir:selected")  # type: ignore[arg-type]

    release_inputs.assert_called_once_with({outpoint}, owner="maker:round-owner")
    assert podle_outpoint not in bot._active_podle_outpoints
    assert session.podle_outpoint is None
    assert session.ring_participant is not None
    assert commitment in bot._reserved_commitments
    next_session = MakerSession(
        cast(
            MakerCoinJoinSession,
            SimpleNamespace(
                taker_nick=taker_identity.nick,
                session_timeout_sec=300,
                commitment=bytes.fromhex("07" * 32),
            ),
        )
    )
    assert bot._reserve_podle_outpoint(podle_outpoint, next_session) is True
    selected.send_private_message.assert_awaited_once_with(
        taker_identity.nick, "ring", "ZW5jcnlwdGVk"
    )


async def test_signed_private_plan_rejection_uses_durable_plan_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker_identity = NickIdentity()
    maker_identity = NickIdentity()
    cycle = [_key(index) for index in (10, 11, 12, 13)]

    def private_participant(index: int) -> dict[str, object]:
        return {
            "participant_key": _key(index),
            "encryption_key": contact_public_key(f"{index:064x}"),
            "backend_limits": _limits().model_dump(),
        }

    def signed_hello(index: int) -> dict[str, object]:
        hello = RingPrivateHelloPayload.model_validate(
            {
                "round_nonce": "aa" * 32,
                "revision": 0,
                "signer_key": _key(index),
                "participant": private_participant(index),
            }
        )
        signed_hello = sign_payload(hello, _secret(index))
        assert isinstance(signed_hello, RingPrivateHelloPayload)
        return signed_hello.model_dump()

    request = RingPrivatePlanPayload.model_validate(
        {
            "round_nonce": "aa" * 32,
            "revision": 0,
            "signer_key": cycle[0],
            "network": "regtest",
            "offer_type": "tr0absoffer",
            "cycle_keys": cycle,
            "position": 1,
            "contribution": {
                "participant_key": cycle[1],
                "residual": 300_000,
                "outgoing": 180_000,
                "incoming": 120_000,
            },
            "predecessor": private_participant(10),
            "successor": private_participant(12),
            "incoming_edge": {
                "edge_id": "21" * 32,
                "pending_channel_id": "11" * 32,
                "opener_key": cycle[0],
                "acceptor_key": cycle[1],
                "capacity": 250_000,
                "push_amount": 120_000,
                "policy": _policy().model_dump(),
            },
            "outgoing_edge": {
                "edge_id": "22" * 32,
                "pending_channel_id": "12" * 32,
                "opener_key": cycle[1],
                "acceptor_key": cycle[2],
                "capacity": 300_000,
                "push_amount": 100_000,
                "policy": _policy().model_dump(),
            },
            "neighbor_hellos": [signed_hello(index) for index in (11, 10, 12)],
        }
    )
    response = RingCancelPayload(
        round_nonce="aa" * 32,
        revision=0,
        signer_key=_key(10),
        reason_code="participant_rejection",
    )
    crypto = _WireCrypto("private plan")
    inner = SimpleNamespace(
        state=CoinJoinState.IOAUTH_SENT,
        taker_nick=taker_identity.nick,
        session_timeout_sec=300,
        validate_channel=lambda received: received == "dir:selected",
        crypto=crypto,
        wallet=SimpleNamespace(release_coinjoin_inputs=Mock()),
        our_utxos={},
        input_lock_owner="maker:round-owner",
        commitment=b"",
    )
    session = MakerSession(inner)  # type: ignore[arg-type]
    participant = _RejectingWireParticipant(response)
    session.ring_participant = participant  # type: ignore[assignment]
    selected = SimpleNamespace(send_private_message=AsyncMock())
    bot = _wire_maker_bot(maker_identity, selected)
    ciphertext = "Y2lwaGVydGV4dA=="
    signed = taker_identity.sign_message(ciphertext, ONION_HOSTID)
    monkeypatch.setattr("maker.maker_session.decode_ring_message", lambda _plaintext: request)

    await session.on_ring(bot, f"ring {signed}", "dir:selected")  # type: ignore[arg-type]

    assert participant.handled == [request]
    selected.send_private_message.assert_awaited_once_with(
        taker_identity.nick, "ring", "ZW5jcnlwdGVk"
    )


async def test_ring_cancel_releases_coinjoin_input_locks(harness: Harness) -> None:
    taker_identity = NickIdentity()
    maker_identity = NickIdentity()
    request = harness.signed(
        RingCancelPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=harness.taker_key,
            reason_code="test_cancel",
        )
    )
    crypto = _WireCrypto(encode_ring_message(request))
    release_inputs = Mock()
    outpoint = ("01" * 32, 0)
    inner = SimpleNamespace(
        state=CoinJoinState.IOAUTH_SENT,
        taker_nick=taker_identity.nick,
        session_timeout_sec=300,
        validate_channel=lambda received: received == "dir:selected",
        crypto=crypto,
        wallet=SimpleNamespace(release_coinjoin_inputs=release_inputs),
        our_utxos={outpoint: object()},
    )
    session = MakerSession(inner)  # type: ignore[arg-type]
    session.ring_participant = _RetiringCancelWireParticipant(request)  # type: ignore[assignment]
    selected = SimpleNamespace(send_private_message=AsyncMock())
    bot = _wire_maker_bot(maker_identity, selected)
    podle_outpoint = ("03" * 32, 2)
    assert bot._reserve_podle_outpoint(podle_outpoint, session) is True
    ciphertext = "Y2lwaGVydGV4dA=="
    signed = taker_identity.sign_message(ciphertext, ONION_HOSTID)

    await session.on_ring(bot, f"ring {signed}", "dir:selected")  # type: ignore[arg-type]

    release_inputs.assert_called_once_with({outpoint})
    assert podle_outpoint not in bot._active_podle_outpoints
    assert session.podle_outpoint is None


@pytest.mark.parametrize("failure", ["invalid", "failed", "post-sign", "unretired"])
async def test_unsafe_ring_cancel_keeps_podle_reservation(harness: Harness, failure: str) -> None:
    taker_identity = NickIdentity()
    maker_identity = NickIdentity()
    cancel = harness.signed(
        RingCancelPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=harness.taker_key,
            reason_code="test_cancel",
        )
    )
    plaintext = "invalid ring payload" if failure == "invalid" else encode_ring_message(cancel)
    crypto = _WireCrypto(plaintext)
    release_inputs = Mock()
    inner = SimpleNamespace(
        state=CoinJoinState.IOAUTH_SENT,
        taker_nick=taker_identity.nick,
        session_timeout_sec=300,
        validate_channel=lambda received: received == "dir:selected",
        crypto=crypto,
        wallet=SimpleNamespace(release_coinjoin_inputs=release_inputs),
        our_utxos={("08" * 32, 0): object()},
    )
    session = MakerSession(inner)  # type: ignore[arg-type]
    if failure == "unretired":
        session.ring_participant = _UnretiredCancelWireParticipant(cancel)  # type: ignore[assignment]
    elif failure != "invalid":
        state = (
            RingLifecycleState.RECOVERY_REQUIRED
            if failure == "failed"
            else RingLifecycleState.SIGNING
        )
        session.ring_participant = _FailingCancelWireParticipant(state)  # type: ignore[assignment]
    selected = SimpleNamespace(send_private_message=AsyncMock())
    bot = _wire_maker_bot(maker_identity, selected)
    podle_outpoint = ("09" * 32, 3)
    assert bot._reserve_podle_outpoint(podle_outpoint, session) is True
    ciphertext = "Y2lwaGVydGV4dA=="
    signed = taker_identity.sign_message(ciphertext, ONION_HOSTID)

    await session.on_ring(bot, f"ring {signed}", "dir:selected")  # type: ignore[arg-type]

    assert bot._active_podle_outpoints[podle_outpoint] is session
    assert session.podle_outpoint == podle_outpoint
    release_inputs.assert_not_called()


async def test_ring_cancel_does_not_release_another_session_podle_reservation(
    harness: Harness,
) -> None:
    taker_identity = NickIdentity()
    maker_identity = NickIdentity()
    cancel = harness.signed(
        RingCancelPayload(
            round_nonce="aa" * 32,
            revision=0,
            signer_key=harness.taker_key,
            reason_code="test_cancel",
        )
    )
    crypto = _WireCrypto(encode_ring_message(cancel))
    inner = SimpleNamespace(
        state=CoinJoinState.IOAUTH_SENT,
        taker_nick=taker_identity.nick,
        session_timeout_sec=300,
        validate_channel=lambda received: received == "dir:selected",
        crypto=crypto,
        wallet=SimpleNamespace(release_coinjoin_inputs=Mock()),
        our_utxos={("0a" * 32, 0): object()},
    )
    session = MakerSession(inner)  # type: ignore[arg-type]
    session.ring_participant = _RetiringCancelWireParticipant(cancel)  # type: ignore[assignment]
    other_session = MakerSession(
        cast(
            MakerCoinJoinSession,
            SimpleNamespace(taker_nick="other", session_timeout_sec=300, commitment=b""),
        )
    )
    selected = SimpleNamespace(send_private_message=AsyncMock())
    bot = _wire_maker_bot(maker_identity, selected)
    podle_outpoint = ("0b" * 32, 4)
    session.podle_outpoint = podle_outpoint
    other_session.podle_outpoint = podle_outpoint
    bot._active_podle_outpoints[podle_outpoint] = other_session
    ciphertext = "Y2lwaGVydGV4dA=="
    signed = taker_identity.sign_message(ciphertext, ONION_HOSTID)

    await session.on_ring(bot, f"ring {signed}", "dir:selected")  # type: ignore[arg-type]

    assert bot._active_podle_outpoints[podle_outpoint] is other_session
    assert other_session.podle_outpoint == podle_outpoint


def test_active_ring_record_retains_maker_input_locks() -> None:
    release_inputs = Mock()
    outpoint = ("02" * 32, 1)
    inner = SimpleNamespace(
        taker_nick="taker",
        session_timeout_sec=300,
        wallet=SimpleNamespace(release_coinjoin_inputs=release_inputs),
        our_utxos={outpoint: object()},
        input_lock_owner="maker:persisted-owner",
    )
    session = MakerSession(inner)  # type: ignore[arg-type]
    session.ring_participant = SimpleNamespace(  # type: ignore[assignment]
        _record=lambda: SimpleNamespace(
            active=True,
            key=SimpleNamespace(filename="active-ring.json"),
        )
    )

    session.release_input_locks()

    release_inputs.assert_not_called()


def test_restart_renews_maker_ring_lock_with_persisted_owner() -> None:
    outpoint = SimpleNamespace(txid="03" * 32, vout=2)
    wallet = SimpleNamespace(renew_coinjoin_inputs=Mock(return_value=True))
    bot = MakerBot.__new__(MakerBot)
    bot.wallet = wallet  # type: ignore[assignment]
    bot.config = SimpleNamespace(  # type: ignore[assignment]
        pending_tx_timeout_min=30,
        pending_tx_abandon_hours=6,
    )
    bot._channel_ring_store = cast(
        RingParticipantStore,
        SimpleNamespace(
            load_all=lambda: SimpleNamespace(
                records=(
                    SimpleNamespace(
                        active=True,
                        local_input_outpoints=(outpoint,),
                        input_lock_owner="maker:persisted-owner",
                    ),
                ),
                corruptions=(),
            )
        ),
    )

    assert bot._renew_channel_ring_input_locks()
    wallet.renew_coinjoin_inputs.assert_called_once_with(
        {(outpoint.txid, outpoint.vout)},
        ttl=6 * 3600,
        owner="maker:persisted-owner",
    )


@pytest.mark.asyncio
async def test_periodic_reconciliation_aborts_when_ring_lock_renewal_fails() -> None:
    bot = MakerBot.__new__(MakerBot)
    bot.running = True
    bot._fatal_error = None
    bot.listen_tasks = []
    bot._channel_ring_store = object()  # type: ignore[assignment]
    bot._channel_ring_backend = object()  # type: ignore[assignment]
    bot.active_sessions = {}
    bot._channel_ring_recovery_tasks = {}
    bot.config = SimpleNamespace(  # type: ignore[assignment]
        channel_ring=SimpleNamespace(phase_timeout_seconds=120.0)
    )
    with (
        patch.object(bot, "_renew_channel_ring_input_locks", return_value=False),
        patch("maker.channel_ring.reconcile_ring_records", new=AsyncMock()) as reconcile,
    ):
        with pytest.raises(RuntimeError, match="input-lock renewal failed"):
            await bot._periodic_channel_ring_reconciliation()

    reconcile.assert_not_awaited()
    assert bot.running is False
    assert isinstance(bot._fatal_error, RuntimeError)
