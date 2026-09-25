"""Durable taker coordinator for JMP-0010 co-funded channel rings."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from jmcore.bitcoin import get_txid, parse_transaction, scriptpubkey_to_address
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
    MAX_RING_CIPHERTEXT_BYTES,
    ChannelPolicy,
    EndpointRole,
    LocalContribution,
    ManifestOutput,
    PolicyBounds,
    PreparedIncomingState,
    PreparedOutgoingState,
    PrivateEdgePlan,
    PrivateParticipant,
    ReadinessAttestation,
    ReadinessState,
    RingCancelPayload,
    RingEdge,
    RingHelloPayload,
    RingInvitePayload,
    RingManifest,
    RingOpenPayload,
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
    decode_ring_message,
    encode_ring_message,
    manifest_hash,
    ring_hash,
    sign_attestation,
    sign_payload,
    validate_hello_for_invite,
    verify_payload,
    verify_ready_payload,
)
from jmcore.models import is_taproot_offer_type
from jmswap.channel_ring import InitializedChannelRingBackend
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
from jmswap.lnd import EndpointRole as LndEndpointRole
from jmswap.lnd import (
    TransactionPresence as LndTransactionPresence,
)
from loguru import logger

from taker.orderbook import calculate_cj_fee, offer_supports_cofunded_channel_ring_v1
from taker.ring_planner import (
    ParticipantChannelLimits,
    RingParty,
    plan_cofunded_ring,
)
from taker.tx_builder import (
    ChannelEndpointContribution,
    FinalizedRingChannelOutput,
    FinalizedRingTransactionPlan,
    build_coinjoin_tx,
)

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend

    from taker.coinjoin_session import CoinJoinSession


class TakerRingError(Exception):
    """A strict all-party ring round cannot safely advance."""


_CHAIN_HASHES = {
    "mainnet": "000000000019d6689c085ae165831e934ff763ae46a2a6c172b3f1b60a8ce26f",
    "testnet": "000000000933ea01ad0ee984209779baaec3ced90fa3f408719526f8d77f4943",
    "signet": "00000008819873e925422c1ff0f99f7c3bdb3adc1d93b6f4e2af5f996e3157b",
    "regtest": "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206",
}


def _chain_hash(network: str) -> bytes:
    try:
        return bytes.fromhex(_CHAIN_HASHES[network])[::-1]
    except KeyError as exc:
        raise TakerRingError(f"unsupported channel-ring network {network!r}") from exc


def _signed(payload: RingPayloadType, secret_hex: str) -> RingPayloadType:
    return sign_payload(payload, bytes.fromhex(secret_hex))


# Negotiation states that only a live in-process round can advance: the taker drives
# every phase, so a restart leaves them permanently stuck rather than resumable.
_UNRESUMABLE_NEGOTIATION_STATES = frozenset(
    {
        RingLifecycleState.ACCEPTOR_ARMED,
        RingLifecycleState.PREPARED,
        RingLifecycleState.PSBT_VERIFIED,
        RingLifecycleState.READY,
        RingLifecycleState.SIGNING,
    }
)


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
        raise TakerRingError("retained readiness does not match verified funding")
    return f"{pending_state.unsigned_txid}:{pending_state.output_index}"


class TakerRingCoordinator:
    """Coordinate one all-party ring revision and act as the local taker participant."""

    def __init__(
        self,
        session: CoinJoinSession,
        *,
        config: ChannelRingConfig,
        store: RingParticipantStore,
        initialized_backend: InitializedChannelRingBackend,
        chain_backend: BlockchainBackend,
        session_identity: str,
    ) -> None:
        self.session = session
        self.config = config
        self.store = store
        self.initialized_backend = initialized_backend
        self.lnd = initialized_backend.backend
        self.chain_backend = chain_backend
        self.session_identity = session_identity
        self.record_key: RingRecordKey | None = None
        self.acceptor_task: asyncio.Task[AcceptorObservation] | None = None
        self.sign_authorized = False
        self._peer_by_key: dict[str, str] = {}
        self._participant_by_key: dict[str, PrivateParticipant] = {}
        self._edge_by_id: dict[str, PrivateEdgePlan] = {}
        self._prepared_by_key: dict[str, RingPreparedPayload] = {}
        self._negotiation: FundingNegotiation | None = None
        self._residuals: dict[str, int] = {}

    def _record(self) -> RingParticipantRecord:
        if self.record_key is None:
            raise TakerRingError("local ring record has not been created")
        record = self.store.load(self.record_key)
        if record is None:
            raise TakerRingError("durable local ring record is missing")
        return record

    @property
    def local_key(self) -> str:
        return self._record().ring_public_key

    @property
    def local_secret(self) -> str:
        return self._record().ring_secret

    def _sign(self, payload: RingPayloadType) -> RingPayloadType:
        return _signed(payload, self.local_secret)

    def _set_phase(self, phase: str) -> None:
        from taker.models import TakerState

        self.session._taker.state = TakerState(phase)

    def _preconditions(self) -> None:
        makers = self.session.maker_sessions
        minimum = max(self.config.minimum_makers, self.session.config.minimum_makers)
        if self.session.is_sweep:
            raise TakerRingError("channel-ring mode does not support sweep/no-change takers")
        if getattr(self.session.wallet, "address_type", None) != "p2tr":
            raise TakerRingError("channel-ring mode requires a p2tr wallet")
        if not is_taproot_offer_type(self.session.config.preferred_offer_type):
            raise TakerRingError("channel-ring mode requires a tr0 preferred offer")
        if len(makers) < minimum:
            raise TakerRingError("selected maker count is below the configured ring minimum")
        if not self.chain_backend.has_mempool_access() or not (
            self.chain_backend.can_get_confirmations_by_txid()
        ):
            raise TakerRingError("ring cancellation requires a full absence-proving backend")
        if any(
            not offer_supports_cofunded_channel_ring_v1(maker.offer) for maker in makers.values()
        ):
            raise TakerRingError("every selected maker must advertise cofunded_channel_ring_v1")
        offer_types = {maker.offer.ordertype for maker in makers.values()}
        if offer_types != {self.session.config.preferred_offer_type}:
            raise TakerRingError("ring makers must use the exact preferred tr0 offer type")

    async def _send(self, nick: str, payload: RingPayloadType) -> None:
        maker = self.session.maker_sessions[nick]
        if maker.crypto is None:
            raise TakerRingError("ring peer has no authenticated encryption session")
        encrypted = maker.crypto.encrypt(encode_ring_message(payload))
        await self.session.directory_client.send_privmsg(
            nick,
            "ring",
            encrypted,
            log_routing=True,
            force_channel=maker.comm_channel,
        )

    async def _receive(
        self,
        expected_type: type[Any],
        *,
        expected_counts: int = 1,
        timeout: float | None = None,
    ) -> dict[str, list[RingPayloadType]]:
        nicks = list(self.session.maker_sessions)
        counts = {nick: expected_counts for nick in nicks} if expected_counts > 1 else None
        responses = await self.session.directory_client.wait_for_responses(
            expected_nicks=nicks,
            expected_command="!ring",
            timeout=self.config.phase_timeout_seconds if timeout is None else timeout,
            expected_counts=counts,
        )
        result: dict[str, list[RingPayloadType]] = {}
        for nick in nicks:
            response = responses.get(nick)
            if response is None or response.get("error"):
                raise TakerRingError(f"ring peer failed during {expected_type.message_type}")
            values = response["data"]
            encoded_values = values if isinstance(values, list) else [values]
            if len(encoded_values) != expected_counts:
                raise TakerRingError(
                    f"ring peer returned {len(encoded_values)} responses, "
                    f"expected {expected_counts}"
                )
            maker = self.session.maker_sessions[nick]
            assert maker.crypto is not None
            decoded: list[RingPayloadType] = []
            for encoded in encoded_values:
                encrypted = str(encoded).split()[0]
                if len(encrypted) > MAX_RING_CIPHERTEXT_BYTES:
                    raise TakerRingError("ring peer returned an oversized encrypted response")
                payload = decode_ring_message(maker.crypto.decrypt(encrypted))
                if not isinstance(payload, expected_type) or not verify_payload(payload):
                    raise TakerRingError("ring peer returned an invalid signed response type")
                record = self._record()
                if payload.round_nonce != record.round_nonce or payload.revision != record.revision:
                    raise TakerRingError("ring peer response belongs to another revision")
                known_key = next(
                    (key for key, peer_nick in self._peer_by_key.items() if peer_nick == nick), None
                )
                if known_key is not None and payload.signer_key != known_key:
                    raise TakerRingError("ring peer substituted its participant key")
                decoded.append(payload)
            result[nick] = decoded
        return result

    async def _exchange(
        self,
        payloads: Mapping[str, RingPayloadType],
        expected_type: type[Any],
        *,
        expected_counts: int = 1,
        timeout: float | None = None,
    ) -> dict[str, list[RingPayloadType]]:
        if set(payloads) != set(self.session.maker_sessions):
            raise TakerRingError("ring phase payloads do not cover exactly the selected makers")
        await asyncio.gather(*(self._send(nick, payload) for nick, payload in payloads.items()))
        return await self._receive(expected_type, expected_counts=expected_counts, timeout=timeout)

    def _finalize_fees_and_inputs(self, mixdepth: int) -> tuple[int, dict[str, int]]:
        maker_count = len(self.session.maker_sessions)
        participant_count = maker_count + 1
        input_count = len(self.session.preselected_utxos) + sum(
            len(maker.utxos) for maker in self.session.maker_sessions.values()
        )
        input_types, _ = self.session._build_script_type_lists(
            self.session.preselected_utxos, 2 * participant_count
        )
        tx_fee = self.session._estimate_tx_fee(
            input_count,
            2 * participant_count,
            input_types=input_types,
            output_types=["p2tr"] * (2 * participant_count),
        )
        total_maker_fee = sum(
            calculate_cj_fee(maker.offer, self.session.cj_amount)
            for maker in self.session.maker_sessions.values()
        )
        required = self.session.cj_amount + total_maker_fee + tx_fee
        selected = self.session.preselected_utxos
        if sum(utxo.value for utxo in selected) < required:
            selected = self.session.wallet.select_utxos(
                mixdepth,
                required,
                self.session.config.taker_utxo_age,
                include_utxos=self.session.preselected_utxos,
                exclude=self.session.wallet.get_locked_input_outpoints(),
            )
        extra = {(utxo.txid, utxo.vout) for utxo in selected} - self.session.reserved_inputs
        if extra and not self.session.wallet.reserve_coinjoin_inputs(
            extra,
            ttl=self.session.input_lock_ttl_sec(),
            owner=self.session.input_lock_owner,
        ):
            raise TakerRingError("additional ring inputs are locked by another round")
        self.session.reserved_inputs |= extra
        self.session.selected_utxos = selected

        # Recompute once after final input selection. Input count can change, but no
        # fee or residual is permitted to change after the invitation is persisted.
        input_count = len(selected) + sum(
            len(maker.utxos) for maker in self.session.maker_sessions.values()
        )
        input_types, _ = self.session._build_script_type_lists(selected, 2 * participant_count)
        tx_fee = self.session._estimate_tx_fee(
            input_count,
            2 * participant_count,
            input_types=input_types,
            output_types=["p2tr"] * (2 * participant_count),
        )
        taker_residual = (
            sum(utxo.value for utxo in selected) - self.session.cj_amount - total_maker_fee - tx_fee
        )
        residuals = {"taker": taker_residual}
        for nick, maker in self.session.maker_sessions.items():
            residuals[nick] = (
                sum(int(utxo["value"]) for utxo in maker.utxos)
                - self.session.cj_amount
                + calculate_cj_fee(maker.offer, self.session.cj_amount)
                - maker.offer.txfee
            )
        if any(value <= 0 for value in residuals.values()):
            raise TakerRingError("ring participant has no positive finalized residual")
        return tx_fee, residuals

    def _limits(self, participant: PrivateParticipant) -> ParticipantChannelLimits:
        backend = participant.backend_limits
        return ParticipantChannelLimits(
            min_channel_capacity=backend.min_channel_capacity,
            max_channel_capacity=backend.max_channel_capacity,
            max_push_amount=backend.max_push_amount,
            dust_limit=backend.dust_limit,
            outgoing_reserve=self.config.opener_reserve,
            incoming_reserve=self.config.fundee_reserve,
            commitment_fee=min(backend.max_commitment_fee, self.config.maximum_commitment_fee),
            channel_overhead=self.config.taproot_commitment_overhead,
            spendable_margin=self.config.spendable_margin,
        )

    def _policy(self) -> ChannelPolicy:
        delay = secrets.choice(self.config.allowed_csv_delays)
        return ChannelPolicy(
            fundee_csv_delay=delay,
            fundee_reserve=self.config.fundee_reserve,
            min_depth=self.config.confirmation_depth,
            opener_csv_delay=delay,
            opener_reserve=self.config.opener_reserve,
        )

    async def _invite(self, tx_fee: int, residuals: dict[str, int]) -> None:
        self._set_phase("ring_inviting")
        logger.info("Channel-ring phase: authenticated invitation")
        record = RingParticipantRecord.fresh(
            round_nonce=secrets.token_hex(32),
            revision=0,
            taker_session_identity=self.session_identity,
            local_role=RingParticipantRole.TAKER,
            local_position=0,
            local_input_outpoints=tuple(
                Outpoint(txid=utxo.txid, vout=utxo.vout) for utxo in self.session.selected_utxos
            ),
            input_lock_owner=self.session.input_lock_owner,
        )
        self.store.save(record)
        self.record_key = record.key
        invite = cast(
            RingInvitePayload,
            self._sign(
                RingInvitePayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    network=cast(Any, self.session.config.network.value),
                    offer_type=cast(Any, self.session.config.preferred_offer_type.value),
                    expiry=int(time.time() + self.config.phase_timeout_seconds),
                    policy_bounds=PolicyBounds(
                        min_csv_delay=self.config.minimum_csv_delay,
                        max_csv_delay=self.config.maximum_csv_delay,
                        min_depth=self.config.confirmation_depth,
                        max_depth=self.config.confirmation_depth,
                        min_reserve=min(self.config.opener_reserve, self.config.fundee_reserve),
                        max_reserve=max(self.config.opener_reserve, self.config.fundee_reserve),
                    ),
                )
            ),
        )
        responses = await self._exchange(
            {nick: invite for nick in self.session.maker_sessions}, RingHelloPayload
        )
        participants = [self.initialized_backend.private_participant(record.ring_public_key)]
        for nick, items in responses.items():
            hello = cast(RingHelloPayload, items[0])
            validate_hello_for_invite(invite, hello)
            if hello.signer_key in self._peer_by_key:
                raise TakerRingError("two selected makers returned the same ring key")
            self._peer_by_key[hello.signer_key] = nick
            participants.append(hello.participant)
        self._participant_by_key = {item.participant_key: item for item in participants}
        self.store.transition(
            record.key,
            RingLifecycleState.INVITED,
            updates={
                "coordinator_participants": tuple(participants),
                "coordinator_reached_keys": tuple(self._peer_by_key),
                "coordinator_tx_fee": tx_fee,
            },
        )
        self._residuals = residuals

    def _build_plans(self) -> tuple[dict[str, RingPlanPayload], RingPlanPayload]:
        local = self._record()
        party_id_by_key = {local.ring_public_key: "taker"}
        party_id_by_key.update({key: nick for key, nick in self._peer_by_key.items()})
        parties = [
            RingParty(
                party_id=party_id_by_key[participant.participant_key],
                residual=self._residuals[party_id_by_key[participant.participant_key]],
                limits=self._limits(participant),
            )
            for participant in self._participant_by_key.values()
        ]
        planned = plan_cofunded_ring(parties)
        key_by_party_id = {party_id: key for key, party_id in party_id_by_key.items()}
        cycle_keys = [key_by_party_id[party_id] for party_id in planned.cycle]
        policy = self._policy()
        private_edges: list[PrivateEdgePlan] = []
        for edge in planned.edges:
            private = PrivateEdgePlan(
                edge_id=secrets.token_hex(32),
                pending_channel_id=secrets.token_hex(32),
                opener_key=key_by_party_id[edge.opener_id],
                acceptor_key=key_by_party_id[edge.fundee_id],
                capacity=edge.capacity,
                push_amount=edge.push_amount,
                policy=policy,
            )
            private_edges.append(private)
            self._edge_by_id[private.edge_id] = private
        contributions = {item.party_id: item for item in planned.contributions}
        plans_by_key: dict[str, RingPlanPayload] = {}
        for position, key in enumerate(cycle_keys):
            party_id = party_id_by_key[key]
            contribution = contributions[party_id]
            plans_by_key[key] = cast(
                RingPlanPayload,
                self._sign(
                    RingPlanPayload(
                        round_nonce=local.round_nonce,
                        revision=local.revision,
                        signer_key=local.ring_public_key,
                        network=cast(Any, self.session.config.network.value),
                        offer_type=cast(Any, self.session.config.preferred_offer_type.value),
                        cycle_keys=cycle_keys,
                        position=position,
                        contribution=LocalContribution(
                            participant_key=key,
                            residual=contribution.residual,
                            outgoing=contribution.outgoing,
                            incoming=contribution.incoming,
                        ),
                        predecessor=self._participant_by_key[
                            cycle_keys[(position - 1) % len(cycle_keys)]
                        ],
                        successor=self._participant_by_key[
                            cycle_keys[(position + 1) % len(cycle_keys)]
                        ],
                        incoming_edge=private_edges[(position - 1) % len(private_edges)],
                        outgoing_edge=private_edges[position],
                    )
                ),
            )
        return plans_by_key, plans_by_key[local.ring_public_key]

    def _acceptor_expectation(self, plan: RingPlanPayload) -> InboundChannelExpectation:
        edge = plan.incoming_edge
        return InboundChannelExpectation(
            pending_channel_id=bytes.fromhex(edge.pending_channel_id),
            opener_node_id=plan.predecessor.node_id,
            chain_hash=_chain_hash(plan.network),
            capacity_sat=edge.capacity,
            push_msat=edge.push_amount * 1000,
            opener_reserve_sat=edge.policy.opener_reserve,
            fundee_reserve_sat=edge.policy.fundee_reserve,
            opener_csv_delay=edge.policy.opener_csv_delay,
            fundee_csv_delay=edge.policy.fundee_csv_delay,
            min_depth=edge.policy.min_depth,
        )

    async def _plan(self) -> RingPlanPayload:
        self._set_phase("ring_planning")
        logger.info("Channel-ring phase: complete-cycle planning")
        plans_by_key, local_plan = self._build_plans()
        record = self._record()
        self.store.transition(
            record.key,
            RingLifecycleState.PLANNED,
            updates={
                "local_position": local_plan.position,
                "plan": local_plan,
                "plan_hash": ring_hash("plan", local_plan).hex(),
                "local_contribution": local_plan.contribution,
                "incoming_edge": FundingEdgeRecord(
                    plan=local_plan.incoming_edge, peer=local_plan.predecessor
                ),
                "outgoing_edge": FundingEdgeRecord(
                    plan=local_plan.outgoing_edge, peer=local_plan.successor
                ),
                "pending_channel_ids": (
                    local_plan.incoming_edge.pending_channel_id,
                    local_plan.outgoing_edge.pending_channel_id,
                ),
                "coordinator_plans": plans_by_key,
            },
        )
        payloads = {
            self._peer_by_key[key]: plan
            for key, plan in plans_by_key.items()
            if key != self.local_key
        }
        responses = await self._exchange(payloads, RingPlanAckPayload)
        for nick, items in responses.items():
            ack = cast(RingPlanAckPayload, items[0])
            peer_key = next(key for key, value in self._peer_by_key.items() if value == nick)
            if ack.plan_hash != ring_hash("plan", plans_by_key[peer_key]).hex():
                raise TakerRingError("maker acknowledged a different recipient plan")

        ready = asyncio.Event()
        self.acceptor_task = asyncio.create_task(
            self.lnd.run_channel_acceptor(
                self._acceptor_expectation(local_plan),
                AcceptorBounds(
                    min_csv_delay=self.config.minimum_csv_delay,
                    max_csv_delay=self.config.maximum_csv_delay,
                ),
                timeout_seconds=self.config.phase_timeout_seconds,
                ready=ready,
            )
        )
        await asyncio.wait_for(ready.wait(), self.config.open_timeout_seconds)
        self.store.transition(record.key, RingLifecycleState.ACCEPTOR_ARMED)
        return local_plan

    async def _open_local(
        self, plan: RingPlanPayload
    ) -> tuple[FundingNegotiation, AcceptorObservation]:
        if self.acceptor_task is None:
            raise TakerRingError("local incoming channel acceptor is not armed")
        edge = plan.outgoing_edge
        request = ExternalChannelRequest(
            pending_channel_id=bytes.fromhex(edge.pending_channel_id),
            peer_node_id=plan.successor.node_id,
            peer_host=plan.successor.onion_endpoint,
            capacity_sat=edge.capacity,
            push_sat=edge.push_amount,
            opener_reserve_sat=edge.policy.opener_reserve,
            fundee_reserve_sat=edge.policy.fundee_reserve,
            opener_csv_delay=edge.policy.opener_csv_delay,
            fundee_csv_delay=edge.policy.fundee_csv_delay,
            min_depth=edge.policy.min_depth,
            timeout_seconds=self.config.open_timeout_seconds,
        )
        record = self._record()
        self.store.transition(
            record.key,
            RingLifecycleState.ACCEPTOR_ARMED,
            updates={"outgoing_open_started": True},
        )
        outgoing, incoming = await asyncio.gather(
            self.lnd.start_external_channel(request), self.acceptor_task
        )
        return outgoing, incoming

    async def _open(self, local_plan: RingPlanPayload) -> None:
        self._set_phase("ring_opening")
        logger.info("Channel-ring phase: private channel preparation")
        record = self._record()
        plans = record.coordinator_plans
        payloads = {
            nick: cast(
                RingOpenPayload,
                self._sign(
                    RingOpenPayload(
                        round_nonce=record.round_nonce,
                        revision=record.revision,
                        signer_key=record.ring_public_key,
                        plan_hash=ring_hash("plan", plans[key]).hex(),
                    )
                ),
            )
            for key, nick in self._peer_by_key.items()
        }
        await asyncio.gather(*(self._send(nick, payload) for nick, payload in payloads.items()))
        remote_task = asyncio.create_task(
            self._receive(RingPreparedPayload, timeout=self.config.open_timeout_seconds)
        )
        local_task = asyncio.create_task(self._open_local(local_plan))
        remote, (negotiation, incoming) = await asyncio.gather(remote_task, local_task)
        self._negotiation = negotiation
        local_prepared = RingPreparedPayload(
            round_nonce=record.round_nonce,
            revision=record.revision,
            signer_key=record.ring_public_key,
            outgoing=PreparedOutgoingState(
                pending_channel_id=local_plan.outgoing_edge.pending_channel_id,
                opener_key=local_plan.outgoing_edge.opener_key,
                acceptor_key=local_plan.outgoing_edge.acceptor_key,
                script_pubkey=negotiation.funding_script_pubkey.hex(),
                capacity=local_plan.outgoing_edge.capacity,
                push_amount=local_plan.outgoing_edge.push_amount,
                policy=local_plan.outgoing_edge.policy,
            ),
            incoming=PreparedIncomingState(
                pending_channel_id=incoming.pending_channel_id.hex(),
                opener_key=local_plan.incoming_edge.opener_key,
                acceptor_key=local_plan.incoming_edge.acceptor_key,
                opener_node_id=incoming.opener_node_id,
                capacity=incoming.capacity_sat,
                push_amount=incoming.push_msat // 1000,
                policy=local_plan.incoming_edge.policy,
            ),
        )
        prepared = {record.ring_public_key: local_prepared}
        for nick, items in remote.items():
            payload = cast(RingPreparedPayload, items[0])
            prepared[payload.signer_key] = payload
        self._validate_prepared(prepared)
        self._prepared_by_key = prepared
        self.store.transition(
            record.key,
            RingLifecycleState.PREPARED,
            updates={
                "prepared_outgoing": local_prepared.outgoing,
                "prepared_incoming": local_prepared.incoming,
                "outgoing_funding_address": negotiation.funding_address,
                "coordinator_prepared": prepared,
            },
        )

    def _validate_prepared(self, prepared: Mapping[str, RingPreparedPayload]) -> None:
        if set(prepared) != set(self._participant_by_key):
            raise TakerRingError("prepared reports do not cover every ring participant")
        scripts: set[str] = set()
        for key, payload in prepared.items():
            plan = self._record().coordinator_plans[key]
            if payload.outgoing.model_dump(exclude={"script_pubkey"}) != PreparedOutgoingState(
                pending_channel_id=plan.outgoing_edge.pending_channel_id,
                opener_key=plan.outgoing_edge.opener_key,
                acceptor_key=plan.outgoing_edge.acceptor_key,
                script_pubkey=payload.outgoing.script_pubkey,
                capacity=plan.outgoing_edge.capacity,
                push_amount=plan.outgoing_edge.push_amount,
                policy=plan.outgoing_edge.policy,
            ).model_dump(exclude={"script_pubkey"}):
                raise TakerRingError("prepared outgoing report differs from its plan")
            successor = plan.successor.participant_key
            reciprocal = prepared[successor].incoming
            if (
                reciprocal.pending_channel_id != payload.outgoing.pending_channel_id
                or reciprocal.opener_key != key
                or reciprocal.acceptor_key != successor
                or reciprocal.opener_node_id != self._participant_by_key[key].node_id
                or reciprocal.capacity != payload.outgoing.capacity
                or reciprocal.push_amount != payload.outgoing.push_amount
                or reciprocal.policy != payload.outgoing.policy
            ):
                raise TakerRingError("successor incoming report is not reciprocal to outgoing")
            if payload.outgoing.script_pubkey in scripts:
                raise TakerRingError("two negotiated channels returned the same funding script")
            scripts.add(payload.outgoing.script_pubkey)

    def _transaction_plan(self) -> FinalizedRingTransactionPlan:
        record = self._record()
        assert record.plan is not None
        participant_ids = [
            "taker" if key == record.ring_public_key else self._peer_by_key[key]
            for key in record.plan.cycle_keys
        ]
        outputs: list[FinalizedRingChannelOutput] = []
        for index, opener_key in enumerate(record.plan.cycle_keys):
            fundee_key = record.plan.cycle_keys[(index + 1) % len(record.plan.cycle_keys)]
            edge = record.coordinator_plans[opener_key].outgoing_edge
            prepared = self._prepared_by_key[opener_key].outgoing
            opener_id = participant_ids[index]
            fundee_id = participant_ids[(index + 1) % len(participant_ids)]
            outputs.append(
                FinalizedRingChannelOutput(
                    edge_id=edge.edge_id,
                    script_pubkey=prepared.script_pubkey,
                    address=scriptpubkey_to_address(
                        bytes.fromhex(prepared.script_pubkey), self.session.config.network.value
                    ),
                    capacity=edge.capacity,
                    opener=ChannelEndpointContribution(
                        participant_id=opener_id,
                        amount=record.coordinator_plans[opener_key].contribution.outgoing,
                    ),
                    fundee=ChannelEndpointContribution(
                        participant_id=fundee_id,
                        amount=record.coordinator_plans[fundee_key].contribution.incoming,
                    ),
                )
            )
        return FinalizedRingTransactionPlan(
            network=self.session.config.network.value,
            participant_ids=participant_ids,
            residuals={
                participant_id: self._residuals[participant_id]
                for participant_id in participant_ids
            },
            channel_outputs=outputs,
        )

    def _build_unsigned(
        self, destination: str, tx_fee: int
    ) -> tuple[RingUnsignedPayload, list[WitnessUtxo]]:
        maker_data = {
            nick: {
                "utxos": maker.utxos,
                "cj_addr": maker.cj_address,
                "change_addr": maker.change_address,
                "cjfee": calculate_cj_fee(maker.offer, self.session.cj_amount),
                "txfee": maker.offer.txfee,
            }
            for nick, maker in self.session.maker_sessions.items()
        }
        ring_plan = self._transaction_plan()
        selected = self.session.selected_utxos
        self.session.cj_destination = destination
        self.session.taker_change_address = ""
        unsigned, metadata = build_coinjoin_tx(
            taker_utxos=[
                {
                    "txid": utxo.txid,
                    "vout": utxo.vout,
                    "value": utxo.value,
                    "scriptpubkey": utxo.scriptpubkey,
                }
                for utxo in selected
            ],
            taker_cj_address=destination,
            taker_change_address="",
            taker_total_input=sum(utxo.value for utxo in selected),
            maker_data=maker_data,
            cj_amount=self.session.cj_amount,
            tx_fee=tx_fee,
            network=self.session.config.network.value,
            ring_plan=ring_plan,
        )
        transaction = parse_transaction(unsigned.hex())
        expected_fee = tx_fee + sum(
            maker.offer.txfee for maker in self.session.maker_sessions.values()
        )
        total_input = sum(utxo.value for utxo in selected) + sum(
            int(utxo["value"])
            for maker in self.session.maker_sessions.values()
            for utxo in maker.utxos
        )
        if len(transaction.outputs) != 2 * len(ring_plan.participant_ids):
            raise TakerRingError("ring transaction output count changed after planning")
        if total_input - sum(output.value for output in transaction.outputs) != expected_fee:
            raise TakerRingError("ring transaction fee differs from the finalized fee treatment")

        record = self._record()
        assert record.plan is not None
        edge_vout: dict[str, int] = {}
        equal_indices: list[int] = []
        for index, ((_, kind), edge_id) in enumerate(
            zip(metadata["output_owners"], metadata["channel_edges"], strict=True)
        ):
            if kind == "cj":
                equal_indices.append(index)
            elif kind == "channel" and edge_id is not None:
                edge_vout[edge_id] = index
        edges = []
        for output in ring_plan.channel_outputs:
            private = self._edge_by_id[output.edge_id]
            edges.append(
                RingEdge(
                    opener_key=private.opener_key,
                    acceptor_key=private.acceptor_key,
                    pending_channel_id=private.pending_channel_id,
                    capacity=private.capacity,
                    output_index=edge_vout[output.edge_id],
                    script_pubkey=output.script_pubkey,
                    policy=private.policy,
                )
            )
        outputs = [
            ManifestOutput(index=index, amount=output.value, script_pubkey=output.scriptpubkey)
            for index, output in enumerate(transaction.outputs)
        ]
        prevouts = {
            (utxo.txid, utxo.vout): WitnessUtxo(
                value_sat=utxo.value, script_pubkey=bytes.fromhex(utxo.scriptpubkey)
            )
            for utxo in selected
        }
        prevouts.update(
            {
                (str(utxo["txid"]), int(utxo["vout"])): WitnessUtxo(
                    value_sat=int(utxo["value"]),
                    script_pubkey=bytes.fromhex(str(utxo["scriptpubkey"])),
                )
                for maker in self.session.maker_sessions.values()
                for utxo in maker.utxos
            }
        )
        witnesses = [prevouts[(tx_input.txid, tx_input.vout)] for tx_input in transaction.inputs]
        psbt, txid = build_unsigned_psbt(unsigned.hex(), witnesses)
        manifest = RingManifest(
            network=cast(Any, self.session.config.network.value),
            round_nonce=record.round_nonce,
            revision=record.revision,
            unsigned_tx_hash=hashlib.sha256(unsigned).hexdigest(),
            unsigned_txid=txid,
            participant_keys=record.plan.cycle_keys,
            edges=edges,
            equal_output_indices=equal_indices,
            outputs=outputs,
        )
        self.session.unsigned_tx = unsigned
        self.session.tx_metadata = metadata
        return cast(
            RingUnsignedPayload,
            self._sign(
                RingUnsignedPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    unsigned_tx=unsigned.hex(),
                    psbt=base64.b64encode(psbt).decode("ascii"),
                    manifest=manifest,
                )
            ),
        ), witnesses

    def _pending_expectations(
        self, manifest: RingManifest
    ) -> tuple[PendingChannelExpectation, PendingChannelExpectation]:
        record = self._record()
        assert record.plan is not None
        index = manifest.participant_keys.index(record.ring_public_key)
        outgoing_edge = manifest.edges[index]
        incoming_edge = manifest.edges[(index - 1) % len(manifest.edges)]
        local_node = self.initialized_backend.node_info.identity_pubkey

        def make(
            edge: RingEdge,
            private: PrivateEdgePlan,
            opener_node: str,
            fundee_node: str,
        ) -> PendingChannelExpectation:
            return PendingChannelExpectation(
                pending_channel_id=bytes.fromhex(edge.pending_channel_id),
                channel_point=f"{manifest.unsigned_txid}:{edge.output_index}",
                opener_node_id=opener_node,
                fundee_node_id=fundee_node,
                capacity_sat=edge.capacity,
                opener_contribution_sat=edge.capacity - private.push_amount,
                fundee_contribution_sat=private.push_amount,
                opener_reserve_sat=edge.policy.opener_reserve,
                fundee_reserve_sat=edge.policy.fundee_reserve,
                commitment_overhead_sat=self.config.taproot_commitment_overhead,
            )

        return (
            make(
                outgoing_edge,
                record.plan.outgoing_edge,
                local_node,
                record.plan.successor.node_id,
            ),
            make(
                incoming_edge,
                record.plan.incoming_edge,
                record.plan.predecessor.node_id,
                local_node,
            ),
        )

    def _readiness_state(
        self,
        manifest: RingManifest,
        expected: PendingChannelExpectation,
        observation: PendingChannelObservation,
        role: EndpointRole,
    ) -> ReadinessState:
        record = self._record()
        opener = observation.role is LndEndpointRole.OPENER
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
            opener_balance=(
                observation.local_balance_sat if opener else observation.remote_balance_sat
            ),
            fundee_balance=(
                observation.remote_balance_sat if opener else observation.local_balance_sat
            ),
            push_amount=expected.fundee_contribution_sat,
            opener_reserve=(
                observation.local_reserve_sat if opener else observation.remote_reserve_sat
            ),
            fundee_reserve=(
                observation.remote_reserve_sat if opener else observation.local_reserve_sat
            ),
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

    def _attest(self, state: ReadinessState) -> SignedReadinessAttestation:
        record = self._record()
        return sign_attestation(
            ReadinessAttestation(
                edge=state.pending_channel_id,
                manifest_hash=state.manifest_hash,
                revision=state.revision,
                role=state.role,
                round_nonce=state.round_nonce,
                signer_key=state.signer_key,
                state_hash=ring_hash("ready-state", state).hex(),
            ),
            bytes.fromhex(record.ring_secret),
        )

    async def _verify_local(
        self, unsigned: RingUnsignedPayload, witnesses: list[WitnessUtxo]
    ) -> tuple[ReadinessState, ReadinessState]:
        if self._negotiation is None:
            raise TakerRingError("local outgoing funding negotiation is missing")
        outgoing_expected, incoming_expected = self._pending_expectations(unsigned.manifest)
        verified = await self.lnd.verify_external_funding(
            self._negotiation,
            unsigned.unsigned_tx,
            unsigned.manifest.unsigned_txid,
            int(outgoing_expected.channel_point.rpartition(":")[2]),
            witnesses,
            timeout_seconds=self.config.readiness_timeout_seconds,
        )
        # Persist the PsbtVerify boundary before either endpoint observation.
        # From this point onward cancellation requires exact chain absence and
        # verified-channel abandonment for both local endpoint records.
        record = self._record()
        outgoing_index = unsigned.manifest.participant_keys.index(record.ring_public_key)
        incoming_index = (outgoing_index - 1) % len(unsigned.manifest.edges)
        record = self.store.transition(
            record.key,
            RingLifecycleState.PSBT_VERIFIED,
            updates={
                "unsigned_psbt": verified.unsigned_psbt.hex(),
                "unsigned_tx": unsigned.unsigned_tx,
                "manifest": unsigned.manifest,
                "outgoing_edge": cast(FundingEdgeRecord, record.outgoing_edge).model_copy(
                    update={
                        "funding_outpoint": Outpoint(
                            txid=unsigned.manifest.unsigned_txid,
                            vout=unsigned.manifest.edges[outgoing_index].output_index,
                        )
                    }
                ),
                "incoming_edge": cast(FundingEdgeRecord, record.incoming_edge).model_copy(
                    update={
                        "funding_outpoint": Outpoint(
                            txid=unsigned.manifest.unsigned_txid,
                            vout=unsigned.manifest.edges[incoming_index].output_index,
                        )
                    }
                ),
                "chain_status": RingChainStatus(exact_txid=unsigned.manifest.unsigned_txid),
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
                raise TakerRingError("local pending channel exceeds the commitment-fee policy")
            if not observation.private:
                raise TakerRingError("local pending ring channel is not private")
        return (
            self._readiness_state(
                unsigned.manifest, outgoing_expected, outgoing_observation, EndpointRole.OPENER
            ),
            self._readiness_state(
                unsigned.manifest, incoming_expected, incoming_observation, EndpointRole.FUNDEE
            ),
        )

    def _validate_readiness(
        self,
        manifest: RingManifest,
        remote: Mapping[str, list[RingPayloadType]],
        local_states: Sequence[ReadinessState],
    ) -> tuple[SignedReadinessAttestation, ...]:
        states: dict[tuple[str, EndpointRole, str], ReadinessState] = {}
        attestations: list[SignedReadinessAttestation] = []
        for state in local_states:
            signed = self._attest(state)
            states[(state.pending_channel_id, state.role, state.signer_key)] = state
            attestations.append(signed)
        for items in remote.values():
            for item in items:
                ready = cast(RingReadyPayload, item)
                if not verify_ready_payload(ready):
                    raise TakerRingError("maker readiness endpoint signature is invalid")
                state = ready.state
                slot = (state.pending_channel_id, state.role, state.signer_key)
                if slot in states:
                    raise TakerRingError("duplicate or substituted readiness endpoint")
                edge = next(
                    (
                        edge
                        for edge in manifest.edges
                        if edge.pending_channel_id == state.pending_channel_id
                    ),
                    None,
                )
                if edge is None:
                    raise TakerRingError("readiness refers to an unknown edge")
                expected_signer = (
                    edge.opener_key if state.role is EndpointRole.OPENER else edge.acceptor_key
                )
                if (
                    state.signer_key != expected_signer
                    or state.script_pubkey != edge.script_pubkey
                    or state.capacity != edge.capacity
                    or state.output_index != edge.output_index
                    or state.policy != edge.policy
                    or state.manifest_hash != manifest_hash(manifest).hex()
                    or state.unsigned_tx_hash != manifest.unsigned_tx_hash
                    or state.unsigned_txid != manifest.unsigned_txid
                ):
                    raise TakerRingError("readiness private state differs from its manifest edge")
                states[slot] = state
                attestations.append(
                    SignedReadinessAttestation(
                        attestation=ready.attestation, signature=ready.endpoint_signature
                    )
                )
        for edge in manifest.edges:
            opener = states.get((edge.pending_channel_id, EndpointRole.OPENER, edge.opener_key))
            fundee = states.get((edge.pending_channel_id, EndpointRole.FUNDEE, edge.acceptor_key))
            if opener is None or fundee is None:
                raise TakerRingError("complete two-endpoint readiness is missing")
            comparable = {
                "role",
                "signer_key",
                "state_salt",
            }
            if opener.model_dump(exclude=comparable) != fundee.model_dump(exclude=comparable):
                raise TakerRingError("opener and fundee readiness states are not reciprocal")
        if len(attestations) != 2 * len(manifest.edges):
            raise TakerRingError("readiness set does not contain exactly two states per edge")
        return tuple(attestations)

    async def _readiness_and_sign(
        self, unsigned: RingUnsignedPayload, witnesses: list[WitnessUtxo]
    ) -> None:
        self._set_phase("ring_verifying")
        logger.info("Channel-ring phase: exact funding verification")
        record = self._record()
        payloads = {nick: unsigned for nick in self.session.maker_sessions}
        local_task = asyncio.create_task(self._verify_local(unsigned, witnesses))
        remote_task = asyncio.create_task(
            self._exchange(
                payloads,
                RingReadyPayload,
                expected_counts=2,
                timeout=self.config.readiness_timeout_seconds,
            )
        )
        try:
            local_states, remote = await asyncio.gather(local_task, remote_task)
        except Exception:
            local_task.cancel()
            remote_task.cancel()
            await asyncio.gather(local_task, remote_task, return_exceptions=True)
            raise
        record = self._record()
        record = self.store.transition(
            record.key,
            RingLifecycleState.PSBT_VERIFIED,
            updates={
                "outgoing_pending_state": local_states[0],
                "incoming_pending_state": local_states[1],
            },
        )
        readiness = self._validate_readiness(unsigned.manifest, remote, local_states)
        self._set_phase("ring_readiness")
        logger.info("Channel-ring phase: complete reciprocal readiness")
        readiness_hash = ring_hash("ready-set", readiness).hex()
        local_readiness = tuple(self._attest(state) for state in local_states)
        self.store.transition(
            record.key,
            RingLifecycleState.READY,
            updates={
                "local_readiness": local_readiness,
                "readiness_set": readiness,
                "readiness_hash": readiness_hash,
            },
        )
        ready_set = cast(
            RingReadySetPayload,
            self._sign(
                RingReadySetPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    manifest=unsigned.manifest,
                    attestations=list(readiness),
                )
            ),
        )
        ready_acks = await self._exchange(
            {nick: ready_set for nick in self.session.maker_sessions}, RingReadySetAckPayload
        )
        acknowledged: list[str] = []
        for items in ready_acks.values():
            ack = cast(RingReadySetAckPayload, items[0])
            if ack.readiness_hash != readiness_hash:
                raise TakerRingError("maker acknowledged a different readiness set")
            acknowledged.append(ack.signer_key)
        self.store.transition(
            record.key,
            RingLifecycleState.READY,
            updates={"coordinator_ready_ack_keys": tuple(acknowledged)},
        )

        sign = cast(
            RingSignPayload,
            self._sign(
                RingSignPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    manifest_hash=manifest_hash(unsigned.manifest).hex(),
                    unsigned_tx_hash=unsigned.manifest.unsigned_tx_hash,
                )
            ),
        )
        self.store.transition(
            record.key,
            RingLifecycleState.READY,
            updates={"coordinator_sign_authorization_sent": True},
        )
        self._set_phase("ring_authorizing_signatures")
        logger.info("Channel-ring phase: durable signing authorization")
        self.sign_authorized = True
        sign_acks = await self._exchange(
            {nick: sign for nick in self.session.maker_sessions}, RingSignAckPayload
        )
        signed_keys: list[str] = []
        for items in sign_acks.values():
            sign_ack = cast(RingSignAckPayload, items[0])
            if (
                sign_ack.manifest_hash != sign.manifest_hash
                or sign_ack.unsigned_tx_hash != sign.unsigned_tx_hash
            ):
                raise TakerRingError("maker sign acknowledgment differs from authorization")
            signed_keys.append(sign_ack.signer_key)
        self.store.transition(
            record.key,
            RingLifecycleState.SIGNING,
            updates={"coordinator_sign_ack_keys": tuple(signed_keys)},
        )

    async def prepare(self, destination: str, mixdepth: int) -> bool:
        """Negotiate through durable SIGNING, or cancel every reached participant."""

        try:
            self._preconditions()
            tx_fee, residuals = self._finalize_fees_and_inputs(mixdepth)
            await self._invite(tx_fee, residuals)
            local_plan = await self._plan()
            await self._open(local_plan)
            unsigned, witnesses = self._build_unsigned(destination, tx_fee)
            await self._readiness_and_sign(unsigned, witnesses)
            logger.info("Co-funded channel ring reached durable all-party signing authorization")
            return True
        except Exception as exc:
            logger.error(f"Co-funded channel ring aborted before completion ({type(exc).__name__})")
            if self.record_key is not None and not self.sign_authorized:
                await self.cancel("coordinator_failure")
            return False

    async def _prove_absent(self, record: RingParticipantRecord) -> RingChainStatus:
        if record.manifest is None:
            raise TakerRingError("verified local ring record has no manifest")
        exact = await self.chain_backend.get_transaction(record.manifest.unsigned_txid)
        if exact is not None:
            raise TakerRingError("exact ring transaction is present in mempool or chain")
        for outpoint in record.local_input_outpoints:
            if await self.chain_backend.get_utxo(outpoint.txid, outpoint.vout) is None:
                raise TakerRingError("local input is spent while exact ring transaction is absent")
        return RingChainStatus(
            exact_txid=record.manifest.unsigned_txid,
            mempool=StoreTransactionPresence.ABSENT,
            chain=StoreTransactionPresence.ABSENT,
            checked_at=time.time(),
        )

    async def _retire_local(self, record: RingParticipantRecord) -> None:
        action = record.retirement_action()
        if action is RingRetirementAction.SHIM_CANCEL:
            cancel_outgoing = record.outgoing_open_started or record.prepared_outgoing is not None
            record = self.store.transition(record.key, RingLifecycleState.RETIRING)
            if cancel_outgoing and record.outgoing_edge is not None:
                pending_id = record.outgoing_edge.plan.pending_channel_id
                await self.lnd.cancel_external_channel(
                    bytes.fromhex(pending_id), input_signatures_added=False
                )
            self.store.transition(record.key, RingLifecycleState.RETIRED)
            return
        status = await self._prove_absent(record)
        record = self.store.transition(
            record.key, RingLifecycleState.RETIRING, updates={"chain_status": status}
        )
        if record.manifest is None or record.unsigned_psbt is None:
            raise TakerRingError("verified local retirement lacks exact funding evidence")
        for edge in record.manifest.edges:
            if edge.pending_channel_id not in record.pending_channel_ids:
                continue
            verified = VerifiedFunding(
                pending_channel_id=bytes.fromhex(edge.pending_channel_id),
                funding_txid=record.manifest.unsigned_txid,
                funding_vout=edge.output_index,
                unsigned_psbt=bytes.fromhex(record.unsigned_psbt),
            )
            await self.lnd.retire_verified_external_channel(
                VerifiedChannelRetirementAuthorization(
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
            )
        self.store.transition(record.key, RingLifecycleState.RETIRED)

    async def cancel(self, reason_code: str) -> None:
        """Best-effort signed peer cancellation plus mandatory safe local retirement."""

        record = self._record()
        if (
            record.state
            in {
                RingLifecycleState.SIGNING,
                RingLifecycleState.SIGNED,
                RingLifecycleState.BROADCAST,
                RingLifecycleState.CONFIRMED_OPEN,
            }
            or record.local_input_signature_created
        ):
            return
        if self.acceptor_task is not None and not self.acceptor_task.done():
            self.acceptor_task.cancel()
            await asyncio.gather(self.acceptor_task, return_exceptions=True)
        cancel = cast(
            RingCancelPayload,
            self._sign(
                RingCancelPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.ring_public_key,
                    reason_code=reason_code,
                    canceled_pending_ids=sorted(
                        {
                            plan.outgoing_edge.pending_channel_id
                            for plan in record.coordinator_plans.values()
                        }
                    ),
                )
            ),
        )
        # Every selected maker was reached by the invitation send, even when a
        # missing hello prevented its fresh participant key from being learned.
        reached = {nick: cancel for nick in self.session.maker_sessions}
        if reached:
            try:
                responses = await self._exchange(reached, RingCancelPayload)
                for items in responses.values():
                    response = cast(RingCancelPayload, items[0])
                    if response.reason_code != reason_code:
                        raise TakerRingError("ring cancellation response changed the reason")
            except Exception as exc:
                logger.warning(f"Not every ring peer durably acknowledged cancellation: {exc}")
        try:
            await self._retire_local(self._record())
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

    def mark_local_signature_creation(self) -> None:
        record = self._record()
        if record.state is not RingLifecycleState.SIGNING:
            raise TakerRingError("local CoinJoin signing lacks all maker sign acknowledgments")
        self.store.transition(
            record.key,
            RingLifecycleState.SIGNING,
            updates={"local_input_signature_created": True},
        )

    def mark_local_signatures(self, signatures: Sequence[Mapping[str, Any]]) -> None:
        if not signatures:
            raise TakerRingError("local ring signing produced no signatures")
        record = self._record()
        encoded = tuple(
            f"{item['txid']}:{item['vout']}:{','.join(cast(Sequence[str], item['witness']))}"
            for item in signatures
        )
        self.store.transition(
            record.key,
            RingLifecycleState.SIGNED,
            updates={
                "local_input_signature_sent": True,
                "local_signatures": encoded,
            },
        )

    def persist_final_transaction(self, tx_hex: str) -> None:
        record = self._record()
        if record.state is not RingLifecycleState.SIGNED or record.manifest is None:
            raise TakerRingError("final ring transaction persistence is out of order")
        if get_txid(tx_hex) != record.manifest.unsigned_txid:
            raise TakerRingError("assembled final transaction differs from the verified ring")
        self.store.transition(
            record.key,
            RingLifecycleState.BROADCAST,
            updates={"final_tx": tx_hex},
        )


async def reconcile_taker_ring_records(
    store: RingParticipantStore,
    initialized_backend: InitializedChannelRingBackend,
    chain_backend: BlockchainBackend,
    acceptor_tasks: dict[str, asyncio.Task[object]] | None = None,
    *,
    acceptor_timeout_seconds: float = 600.0,
    active_session_identities: frozenset[str] = frozenset(),
) -> None:
    """Resume retained taker channels and rebroadcast only exact durable final transactions.

    A negotiation that is no longer live in this process cannot be resumed: the taker
    drives the round, and its peers' sessions are gone. Such records are restored into
    the backend so recovery tooling can act on them, then escalated so they are visible
    instead of silently holding ring capacity forever.
    """

    report = store.load_all()
    if report.corruptions:
        return
    for record in report.records:
        if not record.active:
            continue
        stranded = (
            record.taker_session_identity not in active_session_identities
            and record.state in _UNRESUMABLE_NEGOTIATION_STATES
            and not record.local_signatures
        )
        try:
            if (
                record.plan is not None
                and record.state is RingLifecycleState.ACCEPTOR_ARMED
                and not stranded
            ):
                plan = record.plan
                task_key = record.key.filename
                existing = None if acceptor_tasks is None else acceptor_tasks.get(task_key)
                if existing is None or existing.done():
                    task = asyncio.create_task(
                        initialized_backend.backend.run_channel_acceptor(
                            InboundChannelExpectation(
                                pending_channel_id=bytes.fromhex(
                                    plan.incoming_edge.pending_channel_id
                                ),
                                opener_node_id=plan.predecessor.node_id,
                                chain_hash=_chain_hash(plan.network),
                                capacity_sat=plan.incoming_edge.capacity,
                                push_msat=plan.incoming_edge.push_amount * 1000,
                                opener_reserve_sat=plan.incoming_edge.policy.opener_reserve,
                                fundee_reserve_sat=plan.incoming_edge.policy.fundee_reserve,
                                opener_csv_delay=plan.incoming_edge.policy.opener_csv_delay,
                                fundee_csv_delay=plan.incoming_edge.policy.fundee_csv_delay,
                                min_depth=plan.incoming_edge.policy.min_depth,
                            ),
                            AcceptorBounds(),
                            timeout_seconds=acceptor_timeout_seconds,
                        )
                    )
                    if acceptor_tasks is not None:
                        acceptor_tasks[task_key] = task
            if record.plan is not None and record.state in {
                RingLifecycleState.PREPARED,
                RingLifecycleState.PSBT_VERIFIED,
                RingLifecycleState.READY,
                RingLifecycleState.SIGNING,
            }:
                plan = record.plan
                initialized_backend.backend.resume_inbound_channel(
                    InboundChannelExpectation(
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
                    ),
                    observed_channel_point=_retained_observed_point(
                        record.incoming_pending_state,
                        _retained_verified_funding(record, record.incoming_edge),
                        EndpointRole.FUNDEE,
                    ),
                )
                if record.prepared_outgoing is not None and record.outgoing_funding_address:
                    prepared = record.prepared_outgoing
                    outgoing_verified = _retained_verified_funding(record, record.outgoing_edge)
                    initialized_backend.backend.resume_external_channel(
                        FundingNegotiation(
                            pending_channel_id=bytes.fromhex(prepared.pending_channel_id),
                            peer_node_id=plan.successor.node_id,
                            funding_address=record.outgoing_funding_address,
                            funding_script_pubkey=bytes.fromhex(prepared.script_pubkey),
                            capacity_sat=prepared.capacity,
                            push_sat=prepared.push_amount,
                            opener_reserve_sat=prepared.policy.opener_reserve,
                            fundee_reserve_sat=prepared.policy.fundee_reserve,
                            opener_csv_delay=prepared.policy.opener_csv_delay,
                            fundee_csv_delay=prepared.policy.fundee_csv_delay,
                            min_depth=prepared.policy.min_depth,
                        ),
                        verified=outgoing_verified,
                        observed_channel_point=_retained_observed_point(
                            record.outgoing_pending_state,
                            outgoing_verified,
                            EndpointRole.OPENER,
                        ),
                    )
            if stranded:
                store.transition(
                    record.key,
                    RingLifecycleState.RECOVERY_REQUIRED,
                    updates={
                        "retry": record.retry.model_copy(
                            update={
                                "last_error": (
                                    "interrupted ring negotiation cannot be resumed by the "
                                    "taker; operator recovery is required"
                                )
                            }
                        )
                    },
                )
                continue
            if record.state in {RingLifecycleState.SIGNED, RingLifecycleState.BROADCAST}:
                exact = None
                if record.manifest is not None:
                    exact = await chain_backend.get_transaction(record.manifest.unsigned_txid)
                if exact is None and record.final_tx is not None:
                    await chain_backend.broadcast_transaction(record.final_tx)
                    if record.state is RingLifecycleState.SIGNED:
                        record = store.transition(record.key, RingLifecycleState.BROADCAST)
                    if record.manifest is not None:
                        exact = await chain_backend.get_transaction(record.manifest.unsigned_txid)
                elif exact is not None and record.state is RingLifecycleState.SIGNED:
                    record = store.transition(record.key, RingLifecycleState.BROADCAST)
                if record.manifest is None:
                    continue
                if exact is not None and exact.confirmations > 0:
                    store.transition(
                        record.key,
                        RingLifecycleState.CONFIRMED_OPEN,
                        updates={
                            "chain_status": RingChainStatus(
                                exact_txid=record.manifest.unsigned_txid,
                                mempool=StoreTransactionPresence.ABSENT,
                                chain=StoreTransactionPresence.PRESENT,
                                confirmations=exact.confirmations,
                                checked_at=time.time(),
                            )
                        },
                    )
                    continue
                missing_input = False
                if exact is None:
                    for outpoint in record.local_input_outpoints:
                        if await chain_backend.get_utxo(outpoint.txid, outpoint.vout) is None:
                            missing_input = True
                            break
                if exact is None and missing_input:
                    store.transition(
                        record.key,
                        RingLifecycleState.RECOVERY_REQUIRED,
                        updates={
                            "retry": record.retry.model_copy(
                                update={
                                    "last_error": (
                                        "local ring input is spent while exact funding "
                                        "transaction is absent"
                                    )
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
