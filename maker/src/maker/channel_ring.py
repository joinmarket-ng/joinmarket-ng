"""Maker participant state machine for JMP-0014 co-funded channel rings."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Awaitable, Sequence
from typing import TYPE_CHECKING, TypeVar, cast

from jmcore.bitcoin import address_to_scriptpubkey, get_txid, parse_transaction
from jmcore.channel_ring import ChannelRingConfig
from jmcore.channel_ring_store import (
    FundingEdgeRecord,
    Outpoint,
    RingChainStatus,
    RingLifecycleState,
    RingParticipantRecord,
    RingParticipantRole,
    RingParticipantStore,
    RingRecordKey,
    RingRetirementAction,
)
from jmcore.channel_ring_store import (
    TransactionPresence as StoreTransactionPresence,
)
from jmcore.cofunded_ring import (
    EndpointRole,
    PreparedIncomingState,
    PreparedOutgoingState,
    PrivateEdgePlan,
    ReadinessAttestation,
    ReadinessState,
    RingCancelPayload,
    RingEdge,
    RingHelloPayload,
    RingInvitePayload,
    RingManifest,
    RingOpenPayload,
    RingPayload,
    RingPayloadType,
    RingPlanAckPayload,
    RingPlanPayload,
    RingPreparedPayload,
    RingReadyPayload,
    RingReadySetAckPayload,
    RingReadySetPayload,
    RingSignAckPayload,
    RingSignPayload,
    RingUnsignedPayload,
    SignedReadinessAttestation,
    canonical_json,
    decode_ring_message,
    encode_ring_message,
    manifest_hash,
    ring_hash,
    sign_attestation,
    sign_payload,
    validate_plan_for_invite,
    verify_payload,
    verify_ready_set,
)
from jmcore.constants import GENESIS_BLOCK_HASHES
from jmcore.models import calculate_cj_fee, is_taproot_offer_type
from jmswap.lnd import (
    AcceptorBounds,
    AcceptorObservation,
    ExternalChannelRequest,
    FundingNegotiation,
    FundingTransactionChainStatus,
    InboundChannelExpectation,
    PendingChannelExpectation,
    PendingChannelObservation,
    VerifiedChannelRetirementAuthorization,
    VerifiedFunding,
    WitnessUtxo,
    build_unsigned_psbt,
    validate_contribution_accounting,
)
from jmswap.lnd import (
    EndpointRole as LndEndpointRole,
)
from jmswap.lnd import (
    TransactionPresence as LndTransactionPresence,
)
from loguru import logger

if TYPE_CHECKING:
    from jmswap.channel_ring_nodes import BoundChannelRingNode, ChannelRingNodePool
    from jmwallet.backends.base import BlockchainBackend

    from maker.maker_session import MakerSession


class MakerRingError(Exception):
    """A ring request is invalid for this authenticated maker session."""


def _chain_hash(network: str) -> bytes:
    try:
        return bytes.fromhex(GENESIS_BLOCK_HASHES[network])[::-1]
    except KeyError as exc:
        raise MakerRingError(f"unsupported channel-ring network {network!r}") from exc


def _request_text(payload: RingPayload) -> str:
    return canonical_json(payload).decode("ascii")


def _signed(payload: RingPayloadType, secret_hex: str) -> RingPayloadType:
    return sign_payload(payload, bytes.fromhex(secret_hex))


def _retained_verified_funding(
    record: RingParticipantRecord, edge: FundingEdgeRecord | None
) -> VerifiedFunding | None:
    """Rebuild the durable proof that PsbtVerify already ran for one local edge."""
    if record.unsigned_psbt is None or edge is None or edge.funding_outpoint is None:
        return None
    return VerifiedFunding(
        pending_channel_id=bytes.fromhex(edge.plan.pending_channel_id),
        funding_txid=edge.funding_outpoint.txid,
        funding_vout=edge.funding_outpoint.vout,
        unsigned_psbt=bytes.fromhex(record.unsigned_psbt),
    )


def _retained_observed_point(
    pending_state: ReadinessState | None,
    verified: VerifiedFunding | None,
    expected_role: EndpointRole,
) -> str | None:
    """Return the point this participant durably attested observing, if any."""
    if pending_state is None or verified is None:
        return None
    if (
        pending_state.role is not expected_role
        or pending_state.pending_channel_id != verified.pending_channel_id.hex()
        or pending_state.unsigned_txid != verified.funding_txid
        or pending_state.output_index != verified.funding_vout
    ):
        raise MakerRingError("retained readiness does not match verified funding")
    return f"{pending_state.unsigned_txid}:{pending_state.output_index}"


def _funding_negotiation(record: RingParticipantRecord) -> FundingNegotiation:
    prepared = record.prepared_outgoing
    plan = record.plan
    if prepared is None or plan is None or record.outgoing_funding_address is None:
        raise MakerRingError("outgoing funding negotiation is not durable")
    policy = prepared.policy
    return FundingNegotiation(
        pending_channel_id=bytes.fromhex(prepared.pending_channel_id),
        peer_node_id=plan.successor.node_id,
        funding_address=record.outgoing_funding_address,
        funding_script_pubkey=bytes.fromhex(prepared.script_pubkey),
        capacity_sat=prepared.capacity,
        push_sat=prepared.push_amount,
        opener_reserve_sat=policy.opener_reserve,
        fundee_reserve_sat=policy.fundee_reserve,
        opener_csv_delay=policy.opener_csv_delay,
        fundee_csv_delay=policy.fundee_csv_delay,
        min_depth=policy.min_depth,
    )


class MakerRingParticipant:
    """One authenticated maker's durable participant state machine."""

    def __init__(
        self,
        session: MakerSession,
        *,
        config: ChannelRingConfig,
        store: RingParticipantStore,
        initialized_backend: BoundChannelRingNode,
        chain_backend: BlockchainBackend,
    ) -> None:
        self.session = session
        if any(
            utxo.mixdepth != initialized_backend.binding.source_mixdepth
            for utxo in session.our_utxos.values()
        ):
            raise MakerRingError("ring inputs must all belong to the bound source mixdepth")
        self.config = config
        self.store = store
        self.initialized_backend = initialized_backend
        self.lnd = initialized_backend.backend
        self.chain_backend = chain_backend
        self.record_key: RingRecordKey | None = None
        self.acceptor_task: asyncio.Task[object] | None = None

    def cancel_acceptor_task(self) -> None:
        """Stop the incoming-channel acceptor when its session ends without a ring."""
        if self.acceptor_task is not None and not self.acceptor_task.done():
            self.acceptor_task.cancel()

    @property
    def session_identity(self) -> str:
        return f"{self.session.taker_nick}:{self.session.commitment.hex()}"

    def holds_active_record(self) -> bool:
        """Report whether durable ring state may still bind this session's inputs.

        Unknown or unreadable state is reported as bound: releasing inputs a
        ring may already have committed to a channel is unrecoverable, while
        retained locks expire on their own TTL.
        """
        try:
            return self._record().active
        except Exception:
            return True

    def _record(self) -> RingParticipantRecord:
        if self.record_key is None:
            raise MakerRingError("ring invitation has not been accepted")
        record = self.store.load(self.record_key)
        if record is None:
            raise MakerRingError("durable ring participant record is missing")
        return record

    def _check_common(self, payload: RingPayload, record: RingParticipantRecord) -> None:
        if not verify_payload(payload):
            raise MakerRingError("ring payload has an invalid BIP340 signature")
        if payload.round_nonce != record.round_nonce or payload.revision != record.revision:
            raise MakerRingError("ring payload belongs to another nonce or revision")
        if record.invite is None or payload.signer_key != record.invite.signer_key:
            raise MakerRingError("ring payload is not signed by the bound taker key")

    def _cached(
        self, payload: RingPayload, record: RingParticipantRecord
    ) -> list[RingPayloadType] | None:
        previous = record.handled_requests.get(payload.type)
        if previous is None:
            return None
        if previous != _request_text(payload):
            raise MakerRingError(f"conflicting replay of {payload.type}")
        return [
            decode_ring_message(message)
            for message in record.handled_responses.get(payload.type, ())
        ]

    def _cache_updates(
        self,
        record: RingParticipantRecord,
        request: RingPayload,
        responses: Sequence[RingPayloadType],
    ) -> dict[str, object]:
        requests = dict(record.handled_requests)
        encoded_responses = dict(record.handled_responses)
        requests[request.type] = _request_text(request)
        encoded_responses[request.type] = tuple(encode_ring_message(item) for item in responses)
        return {"handled_requests": requests, "handled_responses": encoded_responses}

    async def handle(self, payload: RingPayloadType) -> list[RingPayloadType]:
        if isinstance(payload, RingInvitePayload):
            return await self._invite(payload)
        record = self._record()
        self._check_common(payload, record)
        cached = self._cached(payload, record)
        if cached is not None:
            return cached
        if isinstance(payload, RingPlanPayload):
            return await self._plan(payload, record)
        if isinstance(payload, RingOpenPayload):
            return await self._open(payload, record)
        if isinstance(payload, RingUnsignedPayload):
            return await self._unsigned(payload, record)
        if isinstance(payload, RingReadySetPayload):
            return await self._ready_set(payload, record)
        if isinstance(payload, RingSignPayload):
            return await self._sign(payload, record)
        if isinstance(payload, RingCancelPayload):
            return await self._cancel(payload, record)
        raise MakerRingError(f"maker cannot receive {payload.type} in the participant flow")

    async def _invite(self, payload: RingInvitePayload) -> list[RingPayloadType]:
        if not verify_payload(payload):
            raise MakerRingError("ring invitation has an invalid BIP340 signature")
        if payload.expiry < int(time.time()):
            raise MakerRingError("ring invitation has expired")
        if payload.network != self.initialized_backend.backend_limits.network:
            raise MakerRingError("ring invitation network does not match LND")
        if payload.offer_type != self.session.offer.ordertype.value:
            raise MakerRingError("ring invitation does not match the selected offer")
        if not is_taproot_offer_type(self.session.offer.ordertype):
            raise MakerRingError("channel rings require a tr0 CoinJoin session")

        report = self.store.load_all()
        if report.corruptions:
            raise MakerRingError("ring store contains unresolved corrupt records")
        same_round = [
            item
            for item in report.records
            if item.taker_session_identity == self.session_identity
            and item.round_nonce == payload.round_nonce
        ]
        for item in same_round:
            if item.revision == payload.revision:
                if item.invite is None or _request_text(item.invite) != _request_text(payload):
                    raise MakerRingError("conflicting invitation for an existing revision")
                self.record_key = item.key
                cached = self._cached(payload, item)
                if cached is None:
                    raise MakerRingError("durable invitation response is incomplete")
                return cached
        if same_round:
            latest = max(same_round, key=lambda item: item.revision)
            if payload.revision <= latest.revision:
                raise MakerRingError("ring revision did not increase")
            if latest.state is not RingLifecycleState.RETIRED:
                raise MakerRingError("previous ring revision is not fully retired")

        record = RingParticipantRecord.fresh(
            node_binding=self.initialized_backend.binding,
            round_nonce=payload.round_nonce,
            revision=payload.revision,
            taker_session_identity=self.session_identity,
            local_role=RingParticipantRole.MAKER,
            local_position=0,
            local_input_outpoints=tuple(
                Outpoint(txid=txid, vout=vout) for txid, vout in self.session.our_utxos
            ),
            input_lock_owner=getattr(
                getattr(self.session, "inner", self.session),
                "input_lock_owner",
                f"maker:{self.session_identity}",
            ),
        )
        participant = self.initialized_backend.private_participant(record.ring_public_key)
        hello = _signed(
            RingHelloPayload(
                round_nonce=record.round_nonce,
                revision=record.revision,
                signer_key=record.ring_public_key,
                participant=participant,
            ),
            record.ring_secret,
        )
        updates = self._cache_updates(record, payload, [hello])
        record = RingParticipantRecord(**{**record.model_dump(), "invite": payload, **updates})
        self.store.save(record)
        self.record_key = record.key
        return [hello]

    def _validate_policy(self, plan: RingPlanPayload) -> None:
        for edge in (plan.incoming_edge, plan.outgoing_edge):
            policy = edge.policy
            if (
                policy.min_depth != self.config.confirmation_depth
                or policy.opener_reserve != self.config.opener_reserve
                or policy.fundee_reserve != self.config.fundee_reserve
                or policy.opener_csv_delay not in self.config.allowed_csv_delays
                or policy.fundee_csv_delay not in self.config.allowed_csv_delays
            ):
                raise MakerRingError("channel policy differs from local configured policy")

    def _validate_plan_accounting(self, plan: RingPlanPayload) -> None:
        contribution = plan.contribution
        input_value = sum(utxo.value for utxo in self.session.our_utxos.values())
        realized_fee = calculate_cj_fee(
            self.session.offer.ordertype,
            self.session.offer.cjfee,
            self.session.amount,
        )
        residual = input_value - self.session.amount + realized_fee - self.session.offer.txfee
        if contribution.residual != residual:
            raise MakerRingError(
                f"planned residual {contribution.residual} differs from "
                f"finalized residual {residual}"
            )
        outgoing_floor = (
            self.config.opener_reserve
            + self.config.maximum_commitment_fee
            + self.config.taproot_commitment_overhead
            + self.config.spendable_margin
        )
        incoming_floor = max(
            self.initialized_backend.backend_limits.dust_limit + 1,
            self.config.fundee_reserve + self.config.spendable_margin,
        )
        if contribution.outgoing < outgoing_floor or contribution.incoming < incoming_floor:
            raise MakerRingError("local split is below configured contribution floors")
        if plan.outgoing_edge.capacity != contribution.outgoing + plan.outgoing_edge.push_amount:
            raise MakerRingError("outgoing edge does not account for the local contribution")
        if plan.incoming_edge.push_amount != contribution.incoming:
            raise MakerRingError("incoming edge does not account for the local contribution")

        local_limits = self.initialized_backend.backend_limits
        for edge, peer in (
            (plan.incoming_edge, plan.predecessor),
            (plan.outgoing_edge, plan.successor),
        ):
            peer_limits = peer.backend_limits
            minimum = max(local_limits.min_channel_capacity, peer_limits.min_channel_capacity)
            maximum = min(local_limits.max_channel_capacity, peer_limits.max_channel_capacity)
            if not minimum <= edge.capacity <= maximum:
                raise MakerRingError("edge capacity is outside endpoint backend limits")
            if edge.push_amount > min(local_limits.max_push_amount, peer_limits.max_push_amount):
                raise MakerRingError("edge push exceeds endpoint backend limits")
        predecessor_gross = plan.incoming_edge.capacity - contribution.incoming
        predecessor_floor = max(
            plan.predecessor.backend_limits.dust_limit + 1,
            plan.incoming_edge.policy.opener_reserve
            + plan.predecessor.backend_limits.max_commitment_fee
            + self.config.taproot_commitment_overhead,
        )
        if predecessor_gross < predecessor_floor:
            raise MakerRingError("predecessor contribution is below its advertised floor")
        successor_floor = max(
            plan.successor.backend_limits.dust_limit + 1,
            plan.outgoing_edge.policy.fundee_reserve,
        )
        if plan.outgoing_edge.push_amount < successor_floor:
            raise MakerRingError("successor contribution is below its advertised floor")

    def _acceptor_expectation(self, plan: RingPlanPayload) -> InboundChannelExpectation:
        edge = plan.incoming_edge
        policy = edge.policy
        return InboundChannelExpectation(
            scid_alias=True,
            pending_channel_id=bytes.fromhex(edge.pending_channel_id),
            opener_node_id=plan.predecessor.node_id,
            chain_hash=_chain_hash(plan.network),
            capacity_sat=edge.capacity,
            push_msat=edge.push_amount * 1000,
            opener_reserve_sat=policy.opener_reserve,
            fundee_reserve_sat=policy.fundee_reserve,
            opener_csv_delay=policy.opener_csv_delay,
            fundee_csv_delay=policy.fundee_csv_delay,
            min_depth=policy.min_depth,
        )

    def _arm_acceptor(self, plan: RingPlanPayload) -> asyncio.Event:
        if self.acceptor_task is not None and not self.acceptor_task.done():
            raise MakerRingError("incoming channel acceptor is already armed")
        ready = asyncio.Event()
        bounds = AcceptorBounds(
            min_csv_delay=self.config.minimum_csv_delay,
            max_csv_delay=self.config.maximum_csv_delay,
        )
        self.acceptor_task = asyncio.create_task(
            self.lnd.run_channel_acceptor(
                self._acceptor_expectation(plan),
                bounds,
                timeout_seconds=self.config.phase_timeout_seconds,
                ready=ready,
            )
        )
        return ready

    async def _plan(
        self, payload: RingPlanPayload, record: RingParticipantRecord
    ) -> list[RingPayloadType]:
        if record.state is not RingLifecycleState.INVITED or record.invite is None:
            raise MakerRingError("ring_plan is out of order")
        validate_plan_for_invite(record.invite, payload)
        # The coordinator can be outside this cycle, and maker endpoints cannot
        # infer which (if any) endpoint belongs to the taker. Check cycle size,
        # never a guessed number of participating makers.
        if len(payload.cycle_keys) < max(3, self.config.minimum_makers):
            raise MakerRingError("ring plan has too few channel participants")
        if payload.cycle_keys[payload.position] != record.ring_public_key:
            raise MakerRingError("ring plan local position does not identify this maker")
        self._validate_policy(payload)
        self._validate_plan_accounting(payload)
        candidate_pending_ids = {
            payload.incoming_edge.pending_channel_id,
            payload.outgoing_edge.pending_channel_id,
        }
        if len(candidate_pending_ids) != 2:
            raise MakerRingError("local ring edges reuse a pending channel ID")
        known_pending_ids = {
            pending_id
            for known in self.store.load_all().records
            if known.key != record.key
            for pending_id in known.pending_channel_ids
        }
        if candidate_pending_ids & known_pending_ids:
            raise MakerRingError("ring plan reuses a retained pending channel ID")
        plan_hash = ring_hash("plan", payload).hex()
        incoming = FundingEdgeRecord(plan=payload.incoming_edge, peer=payload.predecessor)
        outgoing = FundingEdgeRecord(plan=payload.outgoing_edge, peer=payload.successor)
        record = self.store.transition(
            record.key,
            RingLifecycleState.PLANNED,
            updates={
                "local_position": payload.position,
                "plan": payload,
                "plan_hash": plan_hash,
                "local_contribution": payload.contribution,
                "incoming_edge": incoming,
                "outgoing_edge": outgoing,
                "pending_channel_ids": (
                    payload.incoming_edge.pending_channel_id,
                    payload.outgoing_edge.pending_channel_id,
                ),
            },
        )
        ready = self._arm_acceptor(payload)
        try:
            await asyncio.wait_for(ready.wait(), self.config.open_timeout_seconds)
        except Exception:
            if self.acceptor_task is not None:
                self.acceptor_task.cancel()
            raise
        ack = cast(
            RingPayloadType,
            _signed(
                RingPlanAckPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    plan_hash=plan_hash,
                ),
                record.ring_secret,
            ),
        )
        updates = self._cache_updates(record, payload, [ack])
        self.store.transition(record.key, RingLifecycleState.ACCEPTOR_ARMED, updates=updates)
        return [ack]

    async def _open(
        self, payload: RingOpenPayload, record: RingParticipantRecord
    ) -> list[RingPayloadType]:
        if record.state is not RingLifecycleState.ACCEPTOR_ARMED or record.plan is None:
            raise MakerRingError("ring_open is out of order")
        if payload.plan_hash != record.plan_hash:
            raise MakerRingError("ring_open plan hash differs from the acknowledged plan")
        if self.acceptor_task is None:
            raise MakerRingError("incoming channel acceptor is not armed")
        plan = record.plan
        edge = plan.outgoing_edge
        policy = edge.policy
        request = ExternalChannelRequest(
            scid_alias=True,
            pending_channel_id=bytes.fromhex(edge.pending_channel_id),
            peer_node_id=plan.successor.node_id,
            peer_host=plan.successor.onion_endpoint,
            capacity_sat=edge.capacity,
            push_sat=edge.push_amount,
            opener_reserve_sat=policy.opener_reserve,
            fundee_reserve_sat=policy.fundee_reserve,
            opener_csv_delay=policy.opener_csv_delay,
            fundee_csv_delay=policy.fundee_csv_delay,
            min_depth=policy.min_depth,
            timeout_seconds=self.config.open_timeout_seconds,
        )
        record = self.store.transition(
            record.key,
            RingLifecycleState.ACCEPTOR_ARMED,
            updates={"outgoing_open_started": True},
        )
        outgoing, incoming = await asyncio.gather(
            _log_preparation_leg(
                "outgoing channel negotiation", self.lnd.start_external_channel(request)
            ),
            _log_preparation_leg("incoming channel acceptance", self.acceptor_task),
        )
        if not isinstance(incoming, AcceptorObservation):
            raise MakerRingError("incoming acceptor returned an invalid observation")
        prepared_outgoing = PreparedOutgoingState(
            pending_channel_id=edge.pending_channel_id,
            opener_key=edge.opener_key,
            acceptor_key=edge.acceptor_key,
            script_pubkey=outgoing.funding_script_pubkey.hex(),
            capacity=edge.capacity,
            push_amount=edge.push_amount,
            policy=edge.policy,
        )
        incoming_edge = plan.incoming_edge
        prepared_incoming = PreparedIncomingState(
            pending_channel_id=incoming.pending_channel_id.hex(),
            opener_key=incoming_edge.opener_key,
            acceptor_key=incoming_edge.acceptor_key,
            opener_node_id=incoming.opener_node_id,
            capacity=incoming.capacity_sat,
            push_amount=incoming.push_msat // 1000,
            policy=incoming_edge.policy,
        )
        response = cast(
            RingPayloadType,
            _signed(
                RingPreparedPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    outgoing=prepared_outgoing,
                    incoming=prepared_incoming,
                ),
                record.ring_secret,
            ),
        )
        updates = {
            "prepared_outgoing": prepared_outgoing,
            "prepared_incoming": prepared_incoming,
            "outgoing_funding_address": outgoing.funding_address,
            **self._cache_updates(record, payload, [response]),
        }
        self.store.transition(record.key, RingLifecycleState.PREPARED_NOT_VERIFIED, updates=updates)
        return [response]

    def _validate_manifest(
        self, payload: RingUnsignedPayload, record: RingParticipantRecord
    ) -> None:
        plan = record.plan
        prepared = record.prepared_outgoing
        contribution = record.local_contribution
        if plan is None or prepared is None or contribution is None:
            raise MakerRingError("local plan is incomplete")
        manifest = payload.manifest
        if manifest.network != plan.network or manifest.participant_keys != plan.cycle_keys:
            raise MakerRingError("manifest differs from the acknowledged cycle")
        local_index = manifest.participant_keys.index(record.ring_public_key)
        incoming = manifest.edges[(local_index - 1) % len(manifest.edges)]
        outgoing = manifest.edges[local_index]
        for actual, expected in (
            (incoming, plan.incoming_edge),
            (outgoing, plan.outgoing_edge),
        ):
            if (
                actual.pending_channel_id != expected.pending_channel_id
                or actual.opener_key != expected.opener_key
                or actual.acceptor_key != expected.acceptor_key
                or actual.capacity != expected.capacity
                or actual.policy != expected.policy
            ):
                raise MakerRingError("manifest substituted a local edge")
        if outgoing.script_pubkey != prepared.script_pubkey:
            raise MakerRingError("manifest substituted the negotiated outgoing funding script")
        equal_outputs = [manifest.outputs[index] for index in manifest.equal_output_indices]
        if any(output.amount != self.session.amount for output in equal_outputs):
            raise MakerRingError("manifest equal outputs do not have the CoinJoin amount")
        local_equal_script = address_to_scriptpubkey(self.session.cj_address).hex()
        if sum(output.script_pubkey == local_equal_script for output in equal_outputs) != 1:
            raise MakerRingError("manifest does not contain exactly one local equal output")
        change_script = address_to_scriptpubkey(self.session.change_address).hex()
        if any(output.script_pubkey == change_script for output in manifest.outputs):
            raise MakerRingError("ring transaction contains a plain local change output")

        transaction = parse_transaction(payload.unsigned_tx)
        tx_inputs = {(item.txid, item.vout) for item in transaction.inputs}
        if not tx_inputs.issuperset(self.session.our_utxos):
            raise MakerRingError("ring transaction omits a selected local input")
        local_accounted = self.session.amount + contribution.outgoing + contribution.incoming
        realized_fee = calculate_cj_fee(
            self.session.offer.ordertype, self.session.offer.cjfee, self.session.amount
        )
        local_available = (
            sum(item.value for item in self.session.our_utxos.values())
            + realized_fee
            - self.session.offer.txfee
        )
        if local_accounted != local_available:
            raise MakerRingError("ring manifest violates finalized local contribution accounting")

    async def _resolve_witness_utxos(self, unsigned_tx: str) -> list[WitnessUtxo]:
        transaction = parse_transaction(unsigned_tx)
        result: list[WitnessUtxo] = []
        for tx_input in transaction.inputs:
            key = (tx_input.txid, tx_input.vout)
            local = self.session.our_utxos.get(key)
            if local is not None:
                value = local.value
                script = local.scriptpubkey
            else:
                found = await self.chain_backend.get_utxo(*key)
                if found is None or not found.scriptpubkey:
                    raise MakerRingError(f"cannot resolve ring prevout {key[0]}:{key[1]}")
                value = found.value
                script = found.scriptpubkey
            result.append(WitnessUtxo(value_sat=value, script_pubkey=bytes.fromhex(script)))
        return result

    def _pending_expectations(
        self, manifest: RingManifest, record: RingParticipantRecord
    ) -> tuple[PendingChannelExpectation, PendingChannelExpectation]:
        if record.plan is None:
            raise MakerRingError("local plan is missing")
        plan = record.plan
        index = manifest.participant_keys.index(record.ring_public_key)
        incoming_edge = manifest.edges[(index - 1) % len(manifest.edges)]
        outgoing_edge = manifest.edges[index]
        local_node = self.initialized_backend.node_info.identity_pubkey

        def make(
            public_edge: RingEdge,
            private_edge: PrivateEdgePlan,
            opener_node: str,
            fundee_node: str,
        ) -> PendingChannelExpectation:
            return PendingChannelExpectation(
                pending_channel_id=bytes.fromhex(public_edge.pending_channel_id),
                channel_point=f"{manifest.unsigned_txid}:{public_edge.output_index}",
                opener_node_id=opener_node,
                fundee_node_id=fundee_node,
                capacity_sat=public_edge.capacity,
                opener_contribution_sat=public_edge.capacity - private_edge.push_amount,
                fundee_contribution_sat=private_edge.push_amount,
                opener_reserve_sat=public_edge.policy.opener_reserve,
                fundee_reserve_sat=public_edge.policy.fundee_reserve,
                commitment_overhead_sat=self.config.taproot_commitment_overhead,
            )

        outgoing = make(outgoing_edge, plan.outgoing_edge, local_node, plan.successor.node_id)
        incoming = make(incoming_edge, plan.incoming_edge, plan.predecessor.node_id, local_node)
        return outgoing, incoming

    def _readiness_state(
        self,
        record: RingParticipantRecord,
        manifest: RingManifest,
        expected: PendingChannelExpectation,
        observation: PendingChannelObservation,
        role: EndpointRole,
    ) -> ReadinessState:
        opener = observation.role is LndEndpointRole.OPENER
        opener_balance = observation.local_balance_sat if opener else observation.remote_balance_sat
        fundee_balance = observation.remote_balance_sat if opener else observation.local_balance_sat
        edge = next(
            item
            for item in manifest.edges
            if item.pending_channel_id == expected.pending_channel_id.hex()
        )
        return ReadinessState(
            pending_channel_id=edge.pending_channel_id,
            opener_key=edge.opener_key,
            acceptor_key=edge.acceptor_key,
            script_pubkey=edge.script_pubkey,
            capacity=edge.capacity,
            opener_balance=opener_balance,
            fundee_balance=fundee_balance,
            push_amount=expected.fundee_contribution_sat,
            opener_reserve=observation.local_reserve_sat
            if opener
            else observation.remote_reserve_sat,
            fundee_reserve=observation.remote_reserve_sat
            if opener
            else observation.local_reserve_sat,
            commitment_fee=observation.commit_fee_sat,
            commitment_overhead=observation.commitment_overhead_sat,
            policy=edge.policy,
            role=role,
            signer_key=record.ring_public_key,
            round_nonce=record.round_nonce,
            revision=record.revision,
            manifest_hash=manifest_hash(manifest).hex(),
            unsigned_tx_hash=manifest.unsigned_tx_hash,
            unsigned_txid=manifest.unsigned_txid,
            output_index=edge.output_index,
            state_salt=secrets.token_hex(32),
        )

    def _ready_payload(
        self, state: ReadinessState, record: RingParticipantRecord
    ) -> RingReadyPayload:
        attestation = ReadinessAttestation(
            edge=state.pending_channel_id,
            manifest_hash=state.manifest_hash,
            revision=state.revision,
            role=state.role,
            round_nonce=state.round_nonce,
            signer_key=state.signer_key,
            state_hash=ring_hash("ready-state", state).hex(),
        )
        endpoint = sign_attestation(attestation, bytes.fromhex(record.ring_secret))
        payload = RingReadyPayload(
            round_nonce=record.round_nonce,
            revision=record.revision,
            signer_key=record.ring_public_key,
            state=state,
            attestation=attestation,
            endpoint_signature=endpoint.signature,
        )
        return cast(RingReadyPayload, _signed(payload, record.ring_secret))

    async def _unsigned(
        self, payload: RingUnsignedPayload, record: RingParticipantRecord
    ) -> list[RingPayloadType]:
        if record.state is not RingLifecycleState.PREPARED_NOT_VERIFIED:
            raise MakerRingError("ring_unsigned is out of order")
        self._validate_manifest(payload, record)
        witnesses = await self._resolve_witness_utxos(payload.unsigned_tx)
        expected_psbt, expected_txid = build_unsigned_psbt(payload.unsigned_tx, witnesses)
        supplied_psbt = base64.b64decode(payload.psbt, validate=True)
        if supplied_psbt != expected_psbt:
            raise MakerRingError("ring PSBT prevouts or encoding differ from resolved transaction")
        if expected_txid != payload.manifest.unsigned_txid:
            raise MakerRingError("ring PSBT transaction ID differs from manifest")
        outgoing_expected, incoming_expected = self._pending_expectations(payload.manifest, record)
        negotiation = _funding_negotiation(record)
        # LND can consume its funding shim before returning from PsbtVerify.
        # A crash at that boundary must not leave a PREPARED journal that permits
        # pre-verification shim cancellation.
        record = self.store.transition(
            record.key,
            RingLifecycleState.VERIFYING,
            updates={
                "unsigned_psbt": expected_psbt.hex(),
                "unsigned_tx": payload.unsigned_tx,
                "manifest": payload.manifest,
                "chain_status": RingChainStatus(exact_txid=payload.manifest.unsigned_txid),
            },
        )
        verified = await self.lnd.verify_external_funding(
            negotiation,
            payload.unsigned_tx,
            payload.manifest.unsigned_txid,
            int(outgoing_expected.channel_point.rpartition(":")[2]),
            witnesses,
            timeout_seconds=self.config.readiness_timeout_seconds,
        )
        if verified.unsigned_psbt != expected_psbt:
            raise MakerRingError("LND returned a different unsigned funding PSBT")
        # PsbtVerify consumes the opener's funding shim. Persist that point of no
        # return before querying either endpoint so a partial observation failure
        # cannot fall back to pre-verification shim cancellation.
        chain_status = RingChainStatus(exact_txid=payload.manifest.unsigned_txid)
        record = self.store.transition(
            record.key,
            RingLifecycleState.PSBT_VERIFIED,
            updates={
                "unsigned_psbt": verified.unsigned_psbt.hex(),
                "unsigned_tx": payload.unsigned_tx,
                "manifest": payload.manifest,
                "outgoing_edge": cast(FundingEdgeRecord, record.outgoing_edge).model_copy(
                    update={
                        "funding_outpoint": Outpoint(
                            txid=payload.manifest.unsigned_txid,
                            vout=int(outgoing_expected.channel_point.rpartition(":")[2]),
                        )
                    }
                ),
                "incoming_edge": cast(FundingEdgeRecord, record.incoming_edge).model_copy(
                    update={
                        "funding_outpoint": Outpoint(
                            txid=payload.manifest.unsigned_txid,
                            vout=int(incoming_expected.channel_point.rpartition(":")[2]),
                        )
                    }
                ),
                "chain_status": chain_status,
            },
        )
        outgoing_observation, incoming_observation = await asyncio.gather(
            self.lnd.pending_channel_observation(
                outgoing_expected,
                LndEndpointRole.OPENER,
                timeout_seconds=self.config.readiness_timeout_seconds,
            ),
            self.lnd.pending_channel_observation(
                incoming_expected,
                LndEndpointRole.FUNDEE,
                timeout_seconds=self.config.readiness_timeout_seconds,
            ),
        )
        for observation, expected in (
            (outgoing_observation, outgoing_expected),
            (incoming_observation, incoming_expected),
        ):
            validate_contribution_accounting(observation, expected)
            if observation.commit_fee_sat > self.config.maximum_commitment_fee:
                raise MakerRingError("negotiated commitment fee exceeds local policy")
            if not observation.private:
                raise MakerRingError("negotiated ring channel is not private")

        outgoing_state = self._readiness_state(
            record, payload.manifest, outgoing_expected, outgoing_observation, EndpointRole.OPENER
        )
        incoming_state = self._readiness_state(
            record, payload.manifest, incoming_expected, incoming_observation, EndpointRole.FUNDEE
        )
        record = self.store.transition(
            record.key,
            RingLifecycleState.PSBT_VERIFIED,
            updates={
                "outgoing_pending_state": outgoing_state,
                "incoming_pending_state": incoming_state,
            },
        )
        ready = [
            self._ready_payload(outgoing_state, record),
            self._ready_payload(incoming_state, record),
        ]
        local_readiness = tuple(
            SignedReadinessAttestation(
                attestation=item.attestation,
                signature=item.endpoint_signature,
            )
            for item in ready
        )
        updates = {
            "local_readiness": local_readiness,
            **self._cache_updates(record, payload, ready),
        }
        self.store.transition(record.key, RingLifecycleState.READY, updates=updates)
        return cast(list[RingPayloadType], ready)

    async def _ready_set(
        self, payload: RingReadySetPayload, record: RingParticipantRecord
    ) -> list[RingPayloadType]:
        if record.state is not RingLifecycleState.READY or record.manifest is None:
            raise MakerRingError("ring_ready_set is out of order")
        if payload.manifest != record.manifest:
            raise MakerRingError("readiness set substituted the verified manifest")
        if not verify_ready_set(payload):
            raise MakerRingError("readiness set contains an invalid endpoint signature")
        supplied = set(payload.attestations)
        if not set(record.local_readiness).issubset(supplied):
            raise MakerRingError("readiness set omits or substitutes a local endpoint state")
        readiness_hash = ring_hash("ready-set", payload.attestations).hex()
        ack = cast(
            RingPayloadType,
            _signed(
                RingReadySetAckPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    readiness_hash=readiness_hash,
                ),
                record.ring_secret,
            ),
        )
        updates = {
            "readiness_set": tuple(payload.attestations),
            "readiness_hash": readiness_hash,
            **self._cache_updates(record, payload, [ack]),
        }
        self.store.transition(record.key, RingLifecycleState.READY, updates=updates)
        return [ack]

    async def _sign(
        self, payload: RingSignPayload, record: RingParticipantRecord
    ) -> list[RingPayloadType]:
        if (
            record.state is not RingLifecycleState.READY
            or record.manifest is None
            or record.readiness_hash is None
        ):
            raise MakerRingError("ring_sign is out of order or readiness is incomplete")
        if payload.manifest_hash != manifest_hash(record.manifest).hex():
            raise MakerRingError("ring_sign manifest hash differs from verified manifest")
        if payload.unsigned_tx_hash != record.manifest.unsigned_tx_hash:
            raise MakerRingError("ring_sign transaction hash differs from verified transaction")
        ack = cast(
            RingPayloadType,
            _signed(
                RingSignAckPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    manifest_hash=payload.manifest_hash,
                    unsigned_tx_hash=payload.unsigned_tx_hash,
                ),
                record.ring_secret,
            ),
        )
        updates = self._cache_updates(record, payload, [ack])
        self.store.transition(record.key, RingLifecycleState.SIGNING, updates=updates)
        return [ack]

    def prepare_coinjoin_signing(self, tx_hex: str) -> None:
        record = self._record()
        if record.state is not RingLifecycleState.SIGNING:
            raise MakerRingError("ring CoinJoin signature was not authorized")
        if record.unsigned_tx != tx_hex or record.manifest is None:
            raise MakerRingError("!tx differs from the exact verified ring transaction")
        if hashlib.sha256(bytes.fromhex(tx_hex)).hexdigest() != record.manifest.unsigned_tx_hash:
            raise MakerRingError("!tx hash differs from the verified ring manifest")
        if record.readiness_hash is None or len(record.readiness_set) != 2 * len(
            record.manifest.edges
        ):
            raise MakerRingError("complete ring readiness is not durable")
        self.store.transition(
            record.key,
            RingLifecycleState.SIGNING,
            updates={"local_input_signature_created": True},
        )

    def mark_signatures_sent(self, signatures: Sequence[str]) -> None:
        if not signatures:
            raise MakerRingError("ring signing produced no local signatures")
        record = self._record()
        if (
            record.state is not RingLifecycleState.SIGNING
            or not record.local_input_signature_created
        ):
            raise MakerRingError("ring signature persistence is out of order")
        self.store.transition(
            record.key,
            RingLifecycleState.SIGNED,
            updates={
                "local_input_signature_sent": True,
                "local_signatures": tuple(signatures),
            },
        )

    async def observe_final_transaction(self, tx_hex: str) -> None:
        record = self._record()
        if record.state not in {RingLifecycleState.SIGNED, RingLifecycleState.BROADCAST}:
            raise MakerRingError("final ring transaction arrived before local signing")
        if get_txid(tx_hex) != cast(RingManifest, record.manifest).unsigned_txid:
            raise MakerRingError("final transaction differs from the signed ring transaction")
        self.store.transition(
            record.key,
            RingLifecycleState.BROADCAST,
            updates={"final_tx": tx_hex},
        )

    async def _prove_absent(self, record: RingParticipantRecord) -> RingChainStatus:
        if record.manifest is None:
            raise MakerRingError("verified ring record has no manifest")
        if not self.chain_backend.has_mempool_access() or not (
            self.chain_backend.can_get_confirmations_by_txid()
        ):
            raise MakerRingError("backend cannot prove exact transaction absence")
        transaction = await self.chain_backend.get_transaction(record.manifest.unsigned_txid)
        if transaction is not None:
            raise MakerRingError("ring transaction is present in mempool or chain")
        for outpoint in record.local_input_outpoints:
            if await self.chain_backend.get_utxo(outpoint.txid, outpoint.vout) is None:
                raise MakerRingError(
                    "backend cannot prove transaction absence because a local input is spent"
                )
        return RingChainStatus(
            exact_txid=record.manifest.unsigned_txid,
            mempool=StoreTransactionPresence.ABSENT,
            chain=StoreTransactionPresence.ABSENT,
            checked_at=time.time(),
        )

    async def _retire_verified(self, record: RingParticipantRecord) -> None:
        if record.manifest is None or record.unsigned_psbt is None:
            raise MakerRingError("verified retirement lacks exact funding data")
        manifest = record.manifest
        unsigned_psbt = record.unsigned_psbt
        status = await self._prove_absent(record)
        record = self.store.transition(
            record.key,
            RingLifecycleState.RETIRING,
            updates={"chain_status": status},
        )
        for edge in manifest.edges:
            if edge.pending_channel_id not in record.pending_channel_ids:
                continue
            verified = VerifiedFunding(
                pending_channel_id=bytes.fromhex(edge.pending_channel_id),
                funding_txid=manifest.unsigned_txid,
                funding_vout=edge.output_index,
                unsigned_psbt=bytes.fromhex(unsigned_psbt),
            )
            authorization = VerifiedChannelRetirementAuthorization(
                verified_funding=verified,
                channel_point=verified.channel_point,
                unsigned_txid=verified.funding_txid,
                chain_status=FundingTransactionChainStatus(
                    unsigned_txid=verified.funding_txid,
                    mempool=LndTransactionPresence.ABSENT,
                    chain=LndTransactionPresence.ABSENT,
                ),
                local_coinjoin_input_signature_created=False,
                local_coinjoin_input_signature_sent=False,
                funding_transaction_broadcast=False,
                final_transaction_observed=False,
            )
            await self.lnd.retire_verified_external_channel(authorization)
        self.store.transition(record.key, RingLifecycleState.RETIRED)

    async def _cancel(
        self, payload: RingCancelPayload, record: RingParticipantRecord
    ) -> list[RingPayloadType]:
        if record.state is RingLifecycleState.SIGNING or record.local_input_signature_created:
            raise MakerRingError("ring cancellation is forbidden after signing authorization")
        if payload.canceled_pending_ids and not set(record.pending_channel_ids).issubset(
            payload.canceled_pending_ids
        ):
            raise MakerRingError("ring_cancel does not identify both local pending channels")
        if self.acceptor_task is not None and not self.acceptor_task.done():
            self.acceptor_task.cancel()
            await asyncio.gather(self.acceptor_task, return_exceptions=True)
        canceled: list[str] = []
        try:
            action = record.retirement_action()
            if action is RingRetirementAction.SHIM_CANCEL:
                cancel_outgoing = (
                    record.outgoing_open_started or record.prepared_outgoing is not None
                )
                record = self.store.transition(record.key, RingLifecycleState.RETIRING)
                if cancel_outgoing and record.outgoing_edge is not None:
                    pending_id = record.outgoing_edge.plan.pending_channel_id
                    await self.lnd.cancel_external_channel(
                        bytes.fromhex(pending_id), input_signatures_added=False
                    )
                    canceled.append(pending_id)
                record = self.store.transition(record.key, RingLifecycleState.RETIRED)
            elif action is RingRetirementAction.ABANDON_VERIFIED or record.state in {
                RingLifecycleState.PSBT_VERIFIED,
                RingLifecycleState.READY,
            }:
                await self._retire_verified(record)
                record = self._record()
                canceled.extend(record.pending_channel_ids)
            else:
                raise MakerRingError("ring revision cannot be safely canceled")
        except Exception as exc:
            current = self._record()
            if current.can_transition_to(RingLifecycleState.RECOVERY_REQUIRED):
                self.store.transition(
                    current.key,
                    RingLifecycleState.RECOVERY_REQUIRED,
                    updates={
                        "retry": current.retry.model_copy(update={"last_error": str(exc)[:2000]})
                    },
                )
            raise
        response = cast(
            RingPayloadType,
            _signed(
                RingCancelPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    reason_code=payload.reason_code,
                    canceled_pending_ids=sorted(canceled),
                ),
                record.ring_secret,
            ),
        )
        retired = self._record()
        updates = self._cache_updates(retired, payload, [response])
        self.store.transition(retired.key, RingLifecycleState.RETIRED, updates=updates)
        return [response]


_T = TypeVar("_T")


async def _log_preparation_leg(label: str, leg: Awaitable[_T]) -> _T:
    """Log which private-channel preparation leg finished or stalled, and when."""
    started = time.monotonic()
    try:
        result = await leg
    except BaseException as exc:
        logger.warning(
            "Channel-ring {} failed after {:.0f}s: {}",
            label,
            time.monotonic() - started,
            type(exc).__name__,
        )
        raise
    logger.info("Channel-ring {} ready after {:.0f}s", label, time.monotonic() - started)
    return result


async def reconcile_ring_records(
    store: RingParticipantStore,
    nodes: ChannelRingNodePool,
    chain_backend: BlockchainBackend,
    acceptor_tasks: dict[str, asyncio.Task[object]] | None = None,
    *,
    acceptor_timeout_seconds: float = 600.0,
    active_session_identities: frozenset[str] = frozenset(),
) -> tuple[RingParticipantRecord, ...]:
    """Inspect retained participant records without deleting unresolved evidence.

    Records whose taker session is still live in this process are resumed but never
    escalated: escalation targets restart-orphaned state, not in-flight rounds.

    Returns invitations retired because their session is gone. An ``INVITED``
    maker has sent only a signed hello: no plan, LND operation, or signature
    exists, so the caller may release that owner's input leases.
    """

    report = store.load_all()
    if report.corruptions:
        return ()
    expired_invites: list[RingParticipantRecord] = []
    for record in report.records:
        if not record.active:
            continue
        # Ownership failures must not rewrite evidence or use the current mapping.
        initialized_backend = nodes.for_binding(record.node_binding)
        if (
            record.state is RingLifecycleState.INVITED
            and record.taker_session_identity not in active_session_identities
            and time.time() - record.updated_at >= acceptor_timeout_seconds
        ):
            # Without a live session the taker cannot deliver a plan, and the
            # grace period covers a session registered just after the invite.
            retiring = store.transition(record.key, RingLifecycleState.RETIRING)
            expired_invites.append(store.transition(retiring.key, RingLifecycleState.RETIRED))
            continue
        try:
            if record.plan is not None and record.state in {
                RingLifecycleState.ACCEPTOR_ARMED,
                RingLifecycleState.PREPARED_NOT_VERIFIED,
                RingLifecycleState.PSBT_VERIFIED,
                RingLifecycleState.READY,
                RingLifecycleState.SIGNING,
            }:
                plan = record.plan
                incoming = InboundChannelExpectation(
                    scid_alias=True,
                    pending_channel_id=bytes.fromhex(plan.incoming_edge.pending_channel_id),
                    opener_node_id=plan.predecessor.node_id,
                    chain_hash=_chain_hash(plan.network),
                    capacity_sat=plan.incoming_edge.capacity,
                    push_msat=plan.incoming_edge.push_amount * 1000,
                    opener_reserve_sat=plan.incoming_edge.policy.opener_reserve,
                    fundee_reserve_sat=plan.incoming_edge.policy.fundee_reserve,
                    opener_csv_delay=plan.incoming_edge.policy.opener_csv_delay,
                    fundee_csv_delay=plan.incoming_edge.policy.fundee_csv_delay,
                    min_depth=plan.incoming_edge.policy.min_depth,
                )
                if record.state is RingLifecycleState.ACCEPTOR_ARMED:
                    task_key = record.key.filename
                    existing_task = None if acceptor_tasks is None else acceptor_tasks.get(task_key)
                    if existing_task is None or existing_task.done():
                        task = asyncio.create_task(
                            initialized_backend.backend.run_channel_acceptor(
                                incoming,
                                AcceptorBounds(),
                                timeout_seconds=acceptor_timeout_seconds,
                            )
                        )
                        if acceptor_tasks is not None:
                            acceptor_tasks[task_key] = task
                else:
                    outgoing_verified = _retained_verified_funding(record, record.outgoing_edge)
                    initialized_backend.backend.resume_inbound_channel(
                        incoming,
                        observed_channel_point=_retained_observed_point(
                            record.incoming_pending_state,
                            _retained_verified_funding(record, record.incoming_edge),
                            EndpointRole.FUNDEE,
                        ),
                    )
                    initialized_backend.backend.resume_external_channel(
                        _funding_negotiation(record),
                        verified=outgoing_verified,
                        observed_channel_point=_retained_observed_point(
                            record.outgoing_pending_state,
                            outgoing_verified,
                            EndpointRole.OPENER,
                        ),
                    )
            if record.state in {RingLifecycleState.SIGNED, RingLifecycleState.BROADCAST}:
                transaction = None
                if record.manifest is not None:
                    transaction = await chain_backend.get_transaction(record.manifest.unsigned_txid)
                if transaction is None and record.final_tx is not None:
                    await chain_backend.broadcast_transaction(record.final_tx)
                    if record.state is RingLifecycleState.SIGNED:
                        record = store.transition(record.key, RingLifecycleState.BROADCAST)
                    if record.manifest is not None:
                        transaction = await chain_backend.get_transaction(
                            record.manifest.unsigned_txid
                        )
                elif transaction is not None and record.state is RingLifecycleState.SIGNED:
                    record = store.transition(record.key, RingLifecycleState.BROADCAST)
                if transaction is not None and transaction.confirmations > 0:
                    status = RingChainStatus(
                        exact_txid=record.manifest.unsigned_txid,
                        mempool=StoreTransactionPresence.ABSENT,
                        chain=StoreTransactionPresence.PRESENT,
                        confirmations=transaction.confirmations,
                        checked_at=time.time(),
                    )
                    store.transition(
                        record.key,
                        RingLifecycleState.CONFIRMED_OPEN,
                        updates={"chain_status": status},
                    )
                    continue
                if record.manifest is not None:
                    if transaction is None:
                        missing_inputs = [
                            outpoint
                            for outpoint in record.local_input_outpoints
                            if await chain_backend.get_utxo(outpoint.txid, outpoint.vout) is None
                        ]
                        if missing_inputs:
                            current = store.load(record.key)
                            assert current is not None
                            store.transition(
                                record.key,
                                RingLifecycleState.RECOVERY_REQUIRED,
                                updates={
                                    "retry": current.retry.model_copy(
                                        update={
                                            "last_error": (
                                                "local ring input is spent while the exact "
                                                "funding transaction is absent"
                                            )
                                        }
                                    )
                                },
                            )
                            continue
            points = []
            for edge in (record.incoming_edge, record.outgoing_edge):
                if edge is not None and edge.funding_outpoint is not None:
                    points.append(str(edge.funding_outpoint))
            for point in points:
                await initialized_backend.backend.pending_channel_present(point)
            if record.taker_session_identity not in active_session_identities and (
                record.state
                in {
                    RingLifecycleState.PLANNED,
                    RingLifecycleState.PREPARED,
                    RingLifecycleState.VERIFYING,
                }
                or (
                    record.state is RingLifecycleState.SIGNING
                    and record.local_input_signature_created
                    and not record.local_signatures
                )
            ):
                store.transition(
                    record.key,
                    RingLifecycleState.RECOVERY_REQUIRED,
                    updates={
                        "retry": record.retry.model_copy(
                            update={
                                "last_error": "participant session requires authenticated resume"
                            }
                        )
                    },
                )
        except Exception as exc:
            current = store.load(record.key)
            if current is not None and current.can_transition_to(
                RingLifecycleState.RECOVERY_REQUIRED
            ):
                store.transition(
                    current.key,
                    RingLifecycleState.RECOVERY_REQUIRED,
                    updates={
                        "retry": current.retry.model_copy(update={"last_error": str(exc)[:2000]})
                    },
                )
    return tuple(expired_invites)
