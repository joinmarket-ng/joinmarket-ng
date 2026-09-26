from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest
from bitcointx.core.key import CKey
from jmcore.bitcoin import scriptpubkey_to_address
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeConfig, RingNodeBinding
from jmcore.channel_ring_store import RingLifecycleState, RingParticipantStore, RingRetirementAction
from jmcore.cofunded_ring import (
    BackendLimits,
    EndpointRole,
    PreparedIncomingState,
    PreparedOutgoingState,
    PrivateParticipant,
    ReadinessAttestation,
    ReadinessState,
    RingCancelPayload,
    RingHelloPayload,
    RingPayloadType,
    RingPlanAckPayload,
    RingPreparedPayload,
    RingReadyPayload,
    RingReadySetAckPayload,
    RingReadySetPayload,
    RingSignAckPayload,
    RingSignPayload,
    RingUnsignedPayload,
    manifest_hash,
    ring_hash,
    sign_attestation,
    sign_payload,
    verify_payload,
)
from jmcore.models import Offer, OfferType
from jmswap.channel_ring_nodes import BoundChannelRingNode
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
from jmswap.lnd import EndpointRole as LndEndpointRole
from jmwallet.backends.base import UTXO, Transaction
from jmwallet.wallet.models import UTXOInfo

import taker.channel_ring as channel_ring
from taker.channel_ring import (
    TakerRingCoordinator,
    TakerRingError,
    _chain_hash,
    reconcile_taker_ring_records,
)
from taker.coinjoin_session import CoinJoinSession
from taker.models import MakerSession

ONION = "a" * 56 + ".onion:9735"


def test_regtest_chain_hash_matches_genesis_block() -> None:
    genesis = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
    assert _chain_hash("regtest") == bytes.fromhex(genesis)[::-1]


def _secret(index: int) -> bytes:
    return index.to_bytes(32, "big")


def _ring_key(index: int) -> str:
    return bytes(CKey(_secret(index)).xonly_pub).hex()


def _node(index: int) -> str:
    return CKey(_secret(index)).pub.hex()


def _script(index: int) -> str:
    return "5120" + f"{index:064x}"


def _address(index: int) -> str:
    return scriptpubkey_to_address(bytes.fromhex(_script(index)), "regtest")


def _funding_script(pending_id: bytes | str) -> str:
    raw = pending_id if isinstance(pending_id, bytes) else bytes.fromhex(pending_id)
    return "5120" + hashlib.sha256(raw).hexdigest()


def _limits(offer_type: str = "tr0absoffer") -> BackendLimits:
    return BackendLimits(
        network="regtest",
        offer_type=offer_type,
        min_channel_capacity=500_000,
        max_channel_capacity=2_000_000,
        max_push_amount=499_999,
        dust_limit=354,
        max_reserve=50_000,
        max_commitment_fee=20_000,
        max_pending_channels=4,
    )


def _config(tmp_path: Path, minimum_makers: int = 3) -> ChannelRingConfig:
    return ChannelRingConfig(
        enabled=True,
        nodes={
            "local": ChannelRingNodeConfig(
                lnd_grpc_url="https://127.0.0.1:10009",
                lnd_tls_cert_path=tmp_path / "tls.cert",
                lnd_macaroon_path=tmp_path / "admin.macaroon",
                onion_endpoint=ONION,
            )
        },
        mixdepth_nodes={0: "local"},
        node_binding_directory=tmp_path / "node-bindings",
        minimum_makers=minimum_makers,
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


class FakeNodePool:
    def __init__(self, node: BoundChannelRingNode) -> None:
        self.node = node

    def for_binding(self, binding: RingNodeBinding) -> BoundChannelRingNode:
        assert binding == self.node.binding
        return self.node


class FakeWallet:
    address_type = "p2tr"
    wallet_fingerprint = "test"

    def __init__(self, utxo: UTXOInfo) -> None:
        self.utxo = utxo
        self.locked: set[tuple[str, int]] = {(utxo.txid, utxo.vout)}

    def get_locked_input_outpoints(self) -> set[tuple[str, int]]:
        return set(self.locked)

    def reserve_coinjoin_inputs(self, outpoints: set[tuple[str, int]]) -> bool:
        self.locked.update(outpoints)
        return True

    def select_utxos(self, *args: Any, **kwargs: Any) -> list[UTXOInfo]:
        del args, kwargs
        return [self.utxo]


class FakeChain:
    def __init__(self, local: UTXOInfo) -> None:
        self.utxos = {
            (local.txid, local.vout): UTXO(
                txid=local.txid,
                vout=local.vout,
                value=local.value,
                address=local.address,
                confirmations=10,
                scriptpubkey=local.scriptpubkey,
            )
        }
        self.transactions: dict[str, Transaction] = {}
        self.broadcasts: list[str] = []

    @staticmethod
    def has_mempool_access() -> bool:
        return True

    @staticmethod
    def can_get_confirmations_by_txid() -> bool:
        return True

    async def get_transaction(self, txid: str) -> Transaction | None:
        return self.transactions.get(txid)

    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        return self.utxos.get((txid, vout))

    async def broadcast_transaction(self, tx_hex: str) -> str:
        self.broadcasts.append(tx_hex)
        return "00" * 32


class FakeLnd:
    def __init__(self) -> None:
        self.incoming: Any = None
        self.negotiation: FundingNegotiation | None = None
        self.canceled: list[str] = []
        self.retired: list[str] = []
        self.fail_pending_observation = False
        self.resumed_verified: list[tuple[Any, str | None]] = []
        self.resumed_incoming_points: list[str | None] = []
        self.unsigned_distributed = asyncio.Event()

    async def run_channel_acceptor(
        self,
        expected: Any,
        bounds: Any,
        *,
        timeout_seconds: float,
        ready: asyncio.Event | None = None,
    ) -> AcceptorObservation:
        del bounds, timeout_seconds
        self.incoming = expected
        if ready is not None:
            ready.set()
        while self.negotiation is None:
            await asyncio.sleep(0)
        return AcceptorObservation(
            pending_channel_id=expected.pending_channel_id,
            opener_node_id=expected.opener_node_id,
            capacity_sat=expected.capacity_sat,
            push_msat=expected.push_msat,
        )

    async def start_external_channel(self, request: Any) -> FundingNegotiation:
        funding_script = _funding_script(request.pending_channel_id)
        negotiation = FundingNegotiation(
            pending_channel_id=request.pending_channel_id,
            peer_node_id=request.peer_node_id,
            funding_address=scriptpubkey_to_address(bytes.fromhex(funding_script), "regtest"),
            funding_script_pubkey=bytes.fromhex(funding_script),
            capacity_sat=request.capacity_sat,
            push_sat=request.push_sat,
            opener_reserve_sat=request.opener_reserve_sat,
            fundee_reserve_sat=request.fundee_reserve_sat,
            opener_csv_delay=request.opener_csv_delay,
            fundee_csv_delay=request.fundee_csv_delay,
            min_depth=request.min_depth,
        )
        self.negotiation = negotiation
        return negotiation

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
        return VerifiedFunding(
            pending_channel_id=negotiation.pending_channel_id,
            funding_txid=funding_txid,
            funding_vout=funding_vout,
            unsigned_psbt=psbt,
        )

    async def pending_channel_observation(
        self, expected: Any, role: LndEndpointRole, *, timeout_seconds: float
    ) -> PendingChannelObservation:
        await asyncio.wait_for(self.unsigned_distributed.wait(), timeout_seconds)
        if self.fail_pending_observation:
            raise RuntimeError("pending observation failed")
        opener = role is LndEndpointRole.OPENER
        opener_balance = expected.opener_contribution_sat - 1_000 - 660
        fundee_balance = expected.fundee_contribution_sat
        return PendingChannelObservation(
            role=role,
            local_node_id=expected.opener_node_id if opener else expected.fundee_node_id,
            remote_node_id=expected.fundee_node_id if opener else expected.opener_node_id,
            channel_point=expected.channel_point,
            capacity_sat=expected.capacity_sat,
            local_balance_sat=opener_balance if opener else fundee_balance,
            remote_balance_sat=fundee_balance if opener else opener_balance,
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

    def resume_inbound_channel(
        self, expected: Any, *, observed_channel_point: str | None = None
    ) -> None:
        del expected
        self.resumed_incoming_points.append(observed_channel_point)

    def resume_external_channel(
        self,
        negotiation: Any,
        *,
        verified: Any | None = None,
        observed_channel_point: str | None = None,
    ) -> None:
        del negotiation
        self.resumed_verified.append((verified, observed_channel_point))


class MockCoordinator(TakerRingCoordinator):
    def __init__(
        self,
        *args: Any,
        malicious_ready: bool = False,
        missing_sign_ack: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.peer_secrets = {f"maker-{index}": _secret(index) for index in range(1, 6)}
        self.sent_payloads: list[RingPayloadType] = []
        self.sent_nicks: list[str] = []
        self.exchange_recipients: list[frozenset[str]] = []
        self.malicious_ready = malicious_ready
        self.missing_sign_ack = missing_sign_ack

    def _peer_sign(self, nick: str, payload: RingPayloadType) -> RingPayloadType:
        return sign_payload(payload, self.peer_secrets[nick])

    async def _send(self, nick: str, payload: RingPayloadType) -> None:
        self.sent_nicks.append(nick)
        self.sent_payloads.append(payload)

    def _prepared(self, key: str) -> RingPreparedPayload:
        record = self._record()
        plan = record.coordinator_plans[key]
        outgoing = plan.outgoing_edge
        incoming = plan.incoming_edge
        return RingPreparedPayload(
            round_nonce=record.round_nonce,
            revision=record.revision,
            signer_key=key,
            outgoing=PreparedOutgoingState(
                pending_channel_id=outgoing.pending_channel_id,
                opener_key=outgoing.opener_key,
                acceptor_key=outgoing.acceptor_key,
                script_pubkey=_funding_script(outgoing.pending_channel_id),
                capacity=outgoing.capacity,
                push_amount=outgoing.push_amount,
                policy=outgoing.policy,
            ),
            incoming=PreparedIncomingState(
                pending_channel_id=incoming.pending_channel_id,
                opener_key=incoming.opener_key,
                acceptor_key=incoming.acceptor_key,
                opener_node_id=plan.predecessor.node_id,
                capacity=incoming.capacity,
                push_amount=incoming.push_amount,
                policy=incoming.policy,
            ),
        )

    def _ready(self, nick: str, unsigned: RingUnsignedPayload) -> list[RingPayloadType]:
        record = self._record()
        key = next(key for key, peer in self._peer_by_key.items() if peer == nick)
        plan = record.coordinator_plans[key]
        result: list[RingPayloadType] = []
        for edge, role in (
            (plan.outgoing_edge, EndpointRole.OPENER),
            (plan.incoming_edge, EndpointRole.FUNDEE),
        ):
            public = next(
                item
                for item in unsigned.manifest.edges
                if item.pending_channel_id == edge.pending_channel_id
            )
            opener_contribution = edge.capacity - edge.push_amount
            state = ReadinessState(
                pending_channel_id=edge.pending_channel_id,
                opener_key=edge.opener_key,
                acceptor_key=edge.acceptor_key,
                script_pubkey=public.script_pubkey,
                capacity=edge.capacity,
                opener_balance=opener_contribution - 1_000 - 660,
                fundee_balance=edge.push_amount,
                push_amount=edge.push_amount,
                opener_reserve=10_000,
                fundee_reserve=10_000,
                commitment_fee=1_000,
                commitment_overhead=660,
                policy=edge.policy,
                role=role,
                signer_key=key,
                round_nonce=record.round_nonce,
                revision=record.revision,
                manifest_hash=manifest_hash(unsigned.manifest).hex(),
                unsigned_tx_hash=unsigned.manifest.unsigned_tx_hash,
                unsigned_txid=unsigned.manifest.unsigned_txid,
                output_index=public.output_index,
                state_salt=hashlib.sha256(f"{nick}:{role}".encode()).hexdigest(),
            )
            if self.malicious_ready and nick == "maker-1" and role is EndpointRole.OPENER:
                state = state.model_copy(update={"output_index": (state.output_index + 1) % 8})
            attestation = ReadinessAttestation(
                edge=state.pending_channel_id,
                manifest_hash=state.manifest_hash,
                revision=state.revision,
                role=state.role,
                round_nonce=state.round_nonce,
                signer_key=state.signer_key,
                state_hash=ring_hash("ready-state", state).hex(),
            )
            endpoint = sign_attestation(attestation, self.peer_secrets[nick])
            result.append(
                self._peer_sign(
                    nick,
                    RingReadyPayload(
                        round_nonce=record.round_nonce,
                        revision=record.revision,
                        signer_key=key,
                        state=state,
                        attestation=attestation,
                        endpoint_signature=endpoint.signature,
                    ),
                )
            )
        return result

    async def _receive(
        self,
        expected_type: type[Any],
        *,
        expected_counts: int = 1,
        timeout: float | None = None,
    ) -> dict[str, list[RingPayloadType]]:
        del expected_counts, timeout
        if expected_type is not RingPreparedPayload:
            raise AssertionError(f"unexpected receive phase {expected_type}")
        result = {}
        for key, nick in self._peer_by_key.items():
            result[nick] = [self._peer_sign(nick, self._prepared(key))]
        return result

    async def _exchange(
        self,
        payloads: Any,
        expected_type: type[Any],
        *,
        expected_counts: int = 1,
        timeout: float | None = None,
    ) -> dict[str, list[RingPayloadType]]:
        del expected_counts, timeout
        self.exchange_recipients.append(frozenset(payloads))
        self.sent_payloads.extend(payloads.values())
        record = self._record()
        result: dict[str, list[RingPayloadType]] = {}
        if expected_type is RingHelloPayload:
            for nick in payloads:
                key = _ring_key(int(nick[-1]))
                result[nick] = [
                    self._peer_sign(
                        nick,
                        RingHelloPayload(
                            round_nonce=record.round_nonce,
                            revision=record.revision,
                            signer_key=key,
                            participant=PrivateParticipant(
                                participant_key=key,
                                node_id=_node(int(nick[-1])),
                                onion_endpoint=ONION,
                                backend_limits=_limits(
                                    self.session.config.preferred_offer_type.value
                                ),
                            ),
                        ),
                    )
                ]
        elif expected_type is RingPlanAckPayload:
            for nick, plan in payloads.items():
                key = cast(Any, plan).contribution.participant_key
                result[nick] = [
                    self._peer_sign(
                        nick,
                        RingPlanAckPayload(
                            round_nonce=record.round_nonce,
                            revision=record.revision,
                            signer_key=key,
                            plan_hash=ring_hash("plan", plan).hex(),
                        ),
                    )
                ]
        elif expected_type is RingReadyPayload:
            cast(FakeLnd, self.lnd).unsigned_distributed.set()
            for nick, unsigned in payloads.items():
                result[nick] = self._ready(nick, cast(RingUnsignedPayload, unsigned))
        elif expected_type is RingReadySetAckPayload:
            for nick, ready_set in payloads.items():
                key = next(key for key, peer in self._peer_by_key.items() if peer == nick)
                result[nick] = [
                    self._peer_sign(
                        nick,
                        RingReadySetAckPayload(
                            round_nonce=record.round_nonce,
                            revision=record.revision,
                            signer_key=key,
                            readiness_hash=ring_hash(
                                "ready-set", cast(RingReadySetPayload, ready_set).attestations
                            ).hex(),
                        ),
                    )
                ]
        elif expected_type is RingSignAckPayload:
            for nick, sign in payloads.items():
                if self.missing_sign_ack and nick == "maker-1":
                    raise RuntimeError("missing sign acknowledgment")
                key = next(key for key, peer in self._peer_by_key.items() if peer == nick)
                authorization = cast(RingSignPayload, sign)
                result[nick] = [
                    self._peer_sign(
                        nick,
                        RingSignAckPayload(
                            round_nonce=record.round_nonce,
                            revision=record.revision,
                            signer_key=key,
                            manifest_hash=authorization.manifest_hash,
                            unsigned_tx_hash=authorization.unsigned_tx_hash,
                        ),
                    )
                ]
        elif expected_type is RingCancelPayload:
            for nick, cancel in payloads.items():
                key = next(
                    (key for key, peer in self._peer_by_key.items() if peer == nick),
                    _ring_key(int(nick[-1])),
                )
                result[nick] = [
                    self._peer_sign(
                        nick,
                        cast(RingCancelPayload, cancel).model_copy(update={"signer_key": key}),
                    )
                ]
        else:
            raise AssertionError(f"unexpected exchange phase {expected_type}")
        return result


def _harness(
    tmp_path: Path,
    *,
    malicious_ready: bool = False,
    missing_sign_ack: bool = False,
    mixed: bool = False,
    ring_makers: int = 3,
    offer_type: OfferType = OfferType.TR0_ABSOLUTE,
) -> MockCoordinator:
    config = _config(tmp_path, minimum_makers=ring_makers)
    local = UTXOInfo(
        txid="01" * 32,
        vout=0,
        value=2_100_000,
        address=_address(100),
        confirmations=10,
        scriptpubkey=_script(100),
        path="m/86'/1'/0'/0/0",
        mixdepth=0,
    )
    wallet = FakeWallet(local)
    chain = FakeChain(local)
    session = CoinJoinSession()
    taker_config = SimpleNamespace(
        minimum_makers=3,
        preferred_offer_type=offer_type,
        network=SimpleNamespace(value="regtest"),
        taker_utxo_age=1,
        dust_threshold=354,
        data_dir=tmp_path,
        round_up_cj_fees=False,
        equalize_cj_fees=False,
    )
    session.attach(
        SimpleNamespace(
            wallet=wallet,
            backend=chain,
            config=taker_config,
            directory_client=SimpleNamespace(),
        )
    )
    session.cj_amount = 1_000_000
    session.preselected_utxos = [local]
    session.reserved_inputs = {(local.txid, local.vout)}
    session._fee_rate = 1.0
    session._randomized_fee_rate = 1.0
    for index in range(1, ring_makers + (2 if mixed else 1)):
        nick = f"maker-{index}"
        offer = Offer(
            counterparty=nick,
            ordertype=offer_type,
            oid=index,
            minsize=100_000,
            maxsize=5_000_000,
            txfee=2_000,
            cjfee="0.0001" if offer_type is OfferType.TR0_RELATIVE else 1_000,
            features={"private_channel_ring": True} if index <= ring_makers else {},
        )
        session.maker_sessions[nick] = MakerSession(
            nick=nick,
            offer=offer,
            utxos=[
                {
                    "txid": f"{index + 1:064x}",
                    "vout": 0,
                    "value": 2_001_000,
                    "address": _address(200 + index),
                    "scriptpubkey": _script(200 + index),
                }
            ],
            cj_address=_address(300 + index),
            change_address=_address(400 + index),
            responded_auth=True,
            hold_seconds=1_000,
            hold_deadline=time.monotonic() + 1_000,
        )
    if mixed:
        session.ring_candidate_nicks = {f"maker-{index}" for index in range(1, ring_makers + 1)}
    lnd = FakeLnd()
    initialized = BoundChannelRingNode(
        binding=RingNodeBinding(
            network="regtest",
            wallet_identity="00" * 32,
            source_mixdepth=0,
            node_name="local",
            local_node_id=_node(20),
        ),
        backend=cast(Any, lnd),
        node_info=LndNodeInfo(
            identity_pubkey=_node(20),
            version="0.21.1-beta",
            network="regtest",
            synced_to_chain=True,
            wallet_synced=True,
            feature_bits=frozenset({81}),
            advertised_uris=(f"{_node(20)}@{ONION}",),
        ),
        onion_endpoint=ONION,
        backend_limits=_limits(offer_type.value),
    )
    store = RingParticipantStore(
        config.persistence_path(tmp_path), max_active_sessions=4, max_verified_sessions=2
    )
    return MockCoordinator(
        session,
        config=config,
        store=store,
        initialized_backend=initialized,
        chain_backend=cast(Any, chain),
        session_identity="test-session",
        malicious_ready=malicious_ready,
        missing_sign_ack=missing_sign_ack,
    )


async def test_complete_coordinator_reaches_signing_with_exact_ring(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path)
    assert await coordinator.prepare(_address(300), 0)
    record = coordinator._record()
    assert record.state is RingLifecycleState.SIGNING
    assert record.manifest is not None
    assert len(record.manifest.participant_keys) == 4
    assert len(record.manifest.outputs) == 8
    assert record.coordinator_tx_fee is not None and record.coordinator_tx_fee > 0
    assert len(record.readiness_set) == 8
    assert set(record.coordinator_ready_ack_keys) == set(coordinator._peer_by_key)
    assert set(record.coordinator_sign_ack_keys) == set(coordinator._peer_by_key)

    await coordinator.cancel("too_late")
    assert coordinator._record().state is RingLifecycleState.SIGNING


async def test_coordinator_signer_is_not_a_channel_endpoint(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path, mixed=True, ring_makers=2)
    assert await coordinator.prepare(_address(300), 0)
    record = coordinator._record()
    assert record.manifest is not None
    assert coordinator.coordinator_key not in record.manifest.participant_keys
    assert coordinator.coordinator_key != record.ring_public_key
    restarted = _harness(tmp_path, mixed=True, ring_makers=2)
    restarted.record_key = record.key
    assert restarted.coordinator_key == coordinator.coordinator_key
    for plan in record.coordinator_plans.values():
        assert plan.signer_key == coordinator.coordinator_key
        assert coordinator.coordinator_key not in plan.cycle_keys
        assert verify_payload(plan)
    assert set(record.coordinator_plans) == set(record.manifest.participant_keys)
    assert coordinator.sent_payloads
    assert all(
        payload.signer_key == coordinator.coordinator_key and verify_payload(payload)
        for payload in coordinator.sent_payloads
    )


async def test_mixed_coordinator_contacts_only_ring_makers_and_builds_two_n_outputs(
    tmp_path: Path,
) -> None:
    coordinator = _harness(tmp_path, mixed=True)

    assert await coordinator.prepare(_address(300), 0)
    record = coordinator._record()
    assert set(coordinator.session.ring_maker_sessions) == {"maker-1", "maker-2", "maker-3"}
    assert set(coordinator.session.ordinary_maker_sessions) == {"maker-4"}
    assert set(coordinator._peer_by_key.values()) == {"maker-1", "maker-2", "maker-3"}
    assert coordinator.exchange_recipients
    assert all(
        recipients == frozenset({"maker-1", "maker-2", "maker-3"})
        for recipients in coordinator.exchange_recipients
    )
    assert set(coordinator.sent_nicks) <= {"maker-1", "maker-2", "maker-3"}
    assert record.manifest is not None
    assert len(record.manifest.outputs) == 10


async def test_three_ring_participants_with_one_ordinary_maker(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path, mixed=True, ring_makers=2)
    assert await coordinator.prepare(_address(300), 0)
    record = coordinator._record()
    assert record.state is RingLifecycleState.SIGNING
    assert set(coordinator.session.ring_maker_sessions) == {"maker-1", "maker-2"}
    assert set(coordinator.session.ordinary_maker_sessions) == {"maker-3"}
    assert len(coordinator._peer_by_key) == 2
    assert record.manifest is not None
    assert len(record.manifest.participant_keys) == 3
    assert len(record.manifest.outputs) == 8


async def test_rounds_non_grid_relative_fees_once_for_an_all_ring_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _harness(tmp_path, offer_type=OfferType.TR0_RELATIVE)
    coordinator.session.config.round_up_cj_fees = True
    for maker in coordinator.session.maker_sessions.values():
        maker.offer.cjfee = "0.00015"

    observed: dict[str, int] = {}
    original_build = channel_ring.build_coinjoin_tx

    def record_fee_data(**kwargs: Any) -> tuple[bytes, dict[str, Any]]:
        observed.update({nick: int(data["cjfee"]) for nick, data in kwargs["maker_data"].items()})
        return original_build(**kwargs)

    monkeypatch.setattr(channel_ring, "build_coinjoin_tx", record_fee_data)

    assert await coordinator.prepare(_address(300), 0)
    assert coordinator.session.maker_fee_plan() == {
        "maker-1": 200,
        "maker-2": 200,
        "maker-3": 200,
    }
    assert observed == coordinator.session.maker_fee_plan()
    assert set(coordinator.session.ordinary_maker_sessions) == set()


async def test_equalized_mixed_plan_uses_frozen_fees_for_ring_and_ordinary_makers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _harness(tmp_path, mixed=True)
    coordinator.session.config.equalize_cj_fees = True
    for fee, maker in zip(
        (1_000, 2_000, 3_000, 4_000),
        coordinator.session.maker_sessions.values(),
        strict=True,
    ):
        maker.offer.cjfee = fee

    coordinator._preconditions()
    frozen_plan = coordinator.session.maker_fee_plan()
    assert frozen_plan == dict.fromkeys(coordinator.session.maker_sessions, 4_000)
    for maker in coordinator.session.maker_sessions.values():
        maker.offer.cjfee = 1

    observed: dict[str, int] = {}
    original_build = channel_ring.build_coinjoin_tx

    def record_fee_data(**kwargs: Any) -> tuple[bytes, dict[str, Any]]:
        observed.update({nick: int(data["cjfee"]) for nick, data in kwargs["maker_data"].items()})
        return original_build(**kwargs)

    monkeypatch.setattr(channel_ring, "build_coinjoin_tx", record_fee_data)

    assert await coordinator.prepare(_address(300), 0)
    assert observed == frozen_plan
    assert coordinator._residuals["maker-4"] == 1_003_000
    assert all(
        coordinator._residuals[nick] == 1_003_000 for nick in ("maker-1", "maker-2", "maker-3")
    )


async def test_all_ring_plan_keeps_fees_frozen_after_preconditions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = _harness(tmp_path)
    coordinator._preconditions()
    frozen_plan = coordinator.session.maker_fee_plan()
    for maker in coordinator.session.maker_sessions.values():
        maker.offer.cjfee = 9_999

    observed: dict[str, int] = {}
    original_build = channel_ring.build_coinjoin_tx

    def record_fee_data(**kwargs: Any) -> tuple[bytes, dict[str, Any]]:
        observed.update({nick: int(data["cjfee"]) for nick, data in kwargs["maker_data"].items()})
        return original_build(**kwargs)

    monkeypatch.setattr(channel_ring, "build_coinjoin_tx", record_fee_data)

    assert await coordinator.prepare(_address(300), 0)
    assert observed == frozen_plan
    assert not coordinator.session.ordinary_maker_sessions


async def test_equalized_fee_plan_rejects_local_channel_floor_before_invitation(
    tmp_path: Path,
) -> None:
    coordinator = _harness(tmp_path)
    coordinator.session.config.equalize_cj_fees = True
    for fee, maker in zip(
        (350_000, 1_000, 1_000), coordinator.session.maker_sessions.values(), strict=True
    ):
        maker.offer.cjfee = fee

    assert not await coordinator.prepare(_address(300), 0)
    assert coordinator.record_key is None
    assert cast(FakeLnd, coordinator.lnd).negotiation is None


async def test_expired_maker_hold_aborts_before_invitation_or_lnd_work(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path)
    coordinator.session.maker_sessions["maker-1"].hold_deadline = time.monotonic()

    assert not await coordinator.prepare(_address(300), 0)
    assert coordinator.record_key is None
    assert cast(FakeLnd, coordinator.lnd).negotiation is None


async def test_short_ordinary_hold_aborts_before_any_ring_invitation(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path, mixed=True)
    coordinator.session.maker_sessions["maker-4"].hold_deadline = time.monotonic()

    assert not await coordinator.prepare(_address(300), 0)
    assert coordinator.record_key is None
    assert cast(FakeLnd, coordinator.lnd).negotiation is None


async def test_failed_ring_candidate_can_be_replaced_before_freeze(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path, mixed=True)
    failed = coordinator.session.maker_sessions.pop("maker-3")
    coordinator.session.remove_maker_session("maker-3")
    replacement = failed
    replacement.nick = "maker-5"
    coordinator.session.maker_sessions["maker-5"] = replacement
    coordinator.session.ring_candidate_nicks.add("maker-5")

    assert await coordinator.prepare(_address(300), 0)
    assert set(coordinator.session.ring_maker_sessions) == {"maker-1", "maker-2", "maker-5"}
    assert set(coordinator.session.ordinary_maker_sessions) == {"maker-4"}


async def test_ordinary_replacement_preserves_role_counts_before_freeze(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path, mixed=True)
    ordinary = coordinator.session.maker_sessions.pop("maker-4")
    coordinator.session.remove_maker_session("maker-4")
    ordinary.nick = "maker-6"
    coordinator.session.maker_sessions["maker-6"] = ordinary

    assert await coordinator.prepare(_address(300), 0)
    assert len(coordinator.session.ring_maker_sessions) == 3
    assert len(coordinator.session.ordinary_maker_sessions) == 1
    assert set(coordinator.session.ordinary_maker_sessions) == {"maker-6"}


def test_frozen_roles_deny_replacement_selection(tmp_path: Path) -> None:
    from taker.taker import Taker

    coordinator = _harness(tmp_path, mixed=True)
    coordinator._preconditions()
    taker = Taker.__new__(Taker)
    taker._session = coordinator.session
    taker.config = SimpleNamespace(channel_ring=SimpleNamespace(enabled=True))

    with pytest.raises(ValueError, match="roles are frozen"):
        taker._select_channel_ring_makers([], 0, None)


def test_ring_never_expands_explicit_inputs_after_fees(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path)
    coordinator.session.strict_input_selection = True
    coordinator.session.cj_amount = coordinator.session.preselected_utxos[0].value
    coordinator._preconditions()

    with patch.object(coordinator.session.wallet, "select_utxos") as select:
        with pytest.raises(TakerRingError, match="Explicit input UTXOs are insufficient"):
            coordinator._finalize_fees_and_inputs(0)
        select.assert_not_called()
    assert not coordinator.has_durable_record
    assert coordinator.session.selected_utxos == []


async def test_setup_deadline_bounds_and_stops_the_next_action(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path)
    coordinator._setup_deadline = time.monotonic() + 2.0
    assert coordinator._bounded_setup_timeout(10.0) <= 2.0
    coordinator._setup_deadline = time.monotonic()
    called = False

    async def external_action() -> None:
        nonlocal called
        called = True

    with pytest.raises(TakerRingError, match="deadline expired"):
        await coordinator._await_setup_action(external_action, 10.0)
    assert called is False


def test_capability_flip_before_freeze_is_not_a_ring_session(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path)
    coordinator.session.ring_candidate_nicks = {"maker-1", "maker-2", "maker-3"}
    coordinator.session.maker_sessions["maker-3"].offer.features = {}

    with pytest.raises(TakerRingError, match="no longer advertises"):
        coordinator._preconditions()
    assert not coordinator.session.ring_maker_sessions


async def test_malicious_readiness_aborts_and_safely_retires(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path, malicious_ready=True)
    assert not await coordinator.prepare(_address(300), 0)
    assert coordinator._record().state is RingLifecycleState.RETIRED
    assert cast(FakeLnd, coordinator.lnd).canceled == []
    assert len(cast(FakeLnd, coordinator.lnd).retired) == 2


async def test_preverify_cancel_only_cancels_local_outgoing_shim(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path)
    coordinator._preconditions()
    tx_fee, residuals = coordinator._finalize_fees_and_inputs(0)
    await coordinator._invite(tx_fee, residuals)
    local_plan = await coordinator._plan()
    await coordinator._open(local_plan)

    await coordinator.cancel("preverify")

    assert coordinator._record().state is RingLifecycleState.RETIRED
    assert cast(FakeLnd, coordinator.lnd).canceled == [local_plan.outgoing_edge.pending_channel_id]


async def test_cancel_before_ring_open_never_invents_a_shim(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path)
    coordinator._preconditions()
    tx_fee, residuals = coordinator._finalize_fees_and_inputs(0)
    await coordinator._invite(tx_fee, residuals)
    await coordinator._plan()

    await coordinator.cancel("before_open")

    record = coordinator._record()
    assert record.state is RingLifecycleState.RETIRED
    assert record.outgoing_open_started is False
    assert cast(FakeLnd, coordinator.lnd).canceled == []


async def test_partial_psbt_verification_uses_verified_retirement(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path)
    lnd = cast(FakeLnd, coordinator.lnd)
    lnd.fail_pending_observation = True

    assert not await coordinator.prepare(_address(300), 0)

    record = coordinator._record()
    assert record.state is RingLifecycleState.RETIRED
    assert record.unsigned_psbt is not None
    assert lnd.canceled == []
    assert len(lnd.retired) == 2


async def test_interrupted_local_psbt_verification_preserves_uncertain_state(
    tmp_path: Path,
) -> None:
    coordinator = _harness(tmp_path)
    lnd = cast(FakeLnd, coordinator.lnd)

    async def fail_verify(*args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        raise RuntimeError("connection lost during PsbtVerify")

    lnd.verify_external_funding = fail_verify  # type: ignore[method-assign]
    assert not await coordinator.prepare(_address(300), 0)
    intent = coordinator._record()
    assert intent.state in {RingLifecycleState.VERIFYING, RingLifecycleState.RECOVERY_REQUIRED}
    assert intent.unsigned_psbt is not None
    assert intent.manifest is not None
    assert lnd.canceled == []
    assert lnd.retired == []


async def test_missing_sign_ack_is_retained_without_cancellation(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path, missing_sign_ack=True)
    assert not await coordinator.prepare(_address(300), 0)
    record = coordinator._record()
    assert record.state is RingLifecycleState.READY
    assert record.coordinator_sign_authorization_sent
    assert not cast(FakeLnd, coordinator.lnd).canceled


async def test_stranded_negotiation_escalates_only_without_a_live_round(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path, missing_sign_ack=True)
    assert not await coordinator.prepare(_address(300), 0)
    record = coordinator._record()
    assert record.state is RingLifecycleState.READY

    # A live round still owns this record, so reconciliation must not disturb it.
    await reconcile_taker_ring_records(
        coordinator.store,
        FakeNodePool(coordinator.initialized_backend),
        cast(Any, coordinator.chain_backend),
        active_session_identities=frozenset({record.taker_session_identity}),
    )
    assert coordinator._record().state is RingLifecycleState.READY

    # With no live round the taker can never drive this negotiation again, so it is
    # escalated for the operator instead of holding ring capacity silently.
    await reconcile_taker_ring_records(
        coordinator.store,
        FakeNodePool(coordinator.initialized_backend),
        cast(Any, coordinator.chain_backend),
    )
    escalated = coordinator._record()
    assert escalated.state is RingLifecycleState.RECOVERY_REQUIRED
    assert "cannot be resumed" in escalated.retry.last_error


async def test_restart_rebroadcasts_and_confirms_exact_final_transaction(tmp_path: Path) -> None:
    coordinator = _harness(tmp_path)
    assert await coordinator.prepare(_address(300), 0)
    coordinator.mark_local_signature_creation()
    coordinator.mark_local_signatures([{"txid": "01" * 32, "vout": 0, "witness": ["11" * 64]}])
    unsigned = coordinator.session.unsigned_tx.hex()
    coordinator.persist_final_transaction(unsigned)
    record = coordinator._record()
    chain = cast(FakeChain, coordinator.chain_backend)

    await reconcile_taker_ring_records(
        coordinator.store, FakeNodePool(coordinator.initialized_backend), cast(Any, chain)
    )
    assert chain.broadcasts == [unsigned]

    assert record.manifest is not None
    chain.transactions[record.manifest.unsigned_txid] = Transaction(
        txid=record.manifest.unsigned_txid,
        raw=unsigned,
        confirmations=1,
        block_height=100,
    )
    await reconcile_taker_ring_records(
        coordinator.store, FakeNodePool(coordinator.initialized_backend), cast(Any, chain)
    )
    assert coordinator._record().state is RingLifecycleState.CONFIRMED_OPEN
    assert chain.broadcasts == [unsigned]


@pytest.mark.parametrize("signatures_persisted", [False, True])
async def test_fresh_taker_restart_does_not_cancel_or_publish_incomplete_signing(
    tmp_path: Path, signatures_persisted: bool
) -> None:
    original = _harness(tmp_path)
    assert await original.prepare(_address(300), 0)
    original.mark_local_signature_creation()
    if signatures_persisted:
        original.mark_local_signatures([{"txid": "01" * 32, "vout": 0, "witness": ["11" * 64]}])
    before = original._record()
    assert before.local_input_signature_created
    assert before.final_tx is None
    restarted = _harness(tmp_path)
    assert restarted.store is not original.store
    assert restarted.store.load(before.key) == before
    chain = cast(FakeChain, restarted.chain_backend)
    lnd = cast(FakeLnd, restarted.lnd)
    await reconcile_taker_ring_records(
        restarted.store, FakeNodePool(restarted.initialized_backend), cast(Any, chain)
    )
    after = restarted.store.load(before.key)
    assert after is not None
    assert after.state is (
        RingLifecycleState.SIGNED if signatures_persisted else RingLifecycleState.RECOVERY_REQUIRED
    )
    assert after.local_input_signature_created
    assert after.local_signatures == before.local_signatures
    assert after.local_input_outpoints == before.local_input_outpoints
    assert after.input_lock_owner == before.input_lock_owner
    assert after.retirement_action() is RingRetirementAction.BLOCKED
    assert after.active
    assert lnd.canceled == lnd.retired == []
    assert chain.broadcasts == []
