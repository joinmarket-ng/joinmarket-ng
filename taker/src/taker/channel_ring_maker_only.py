"""Maker-only private ring coordination, with no taker Lightning endpoint.

The coordinator still owns the CoinJoin inputs and authenticates the same ring
messages. Its key and journal are independent of every channel endpoint.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from jmcore.bitcoin import (
    address_to_scriptpubkey,
    get_txid,
    parse_transaction,
    scriptpubkey_to_address,
)
from jmcore.channel_ring import ChannelRingConfig
from jmcore.channel_ring_store import Outpoint, RingChainStatus, TransactionPresence
from jmcore.cofunded_ring import (
    MAX_RING_CIPHERTEXT_BYTES,
    BackendLimits,
    ChannelPolicy,
    EndpointRole,
    LocalContribution,
    ManifestOutput,
    PolicyBounds,
    PrivateEdgePlan,
    PrivateParticipant,
    ReadinessState,
    RingCancelPayload,
    RingEdge,
    RingHelloPayload,
    RingInvitePayload,
    RingKeyPair,
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
    sign_payload,
    validate_hello_for_invite,
    verify_payload,
    verify_ready_payload,
)
from jmcore.constants import DUST_THRESHOLD
from jmcore.models import is_taproot_offer_type
from jmswap.lnd import WitnessUtxo, build_unsigned_psbt
from loguru import logger

from taker.channel_ring import TakerRingError
from taker.channel_ring_coordinator_store import (
    CoordinatorPhase,
    CoordinatorRecord,
    CoordinatorStore,
)
from taker.orderbook import offer_supports_private_channel_ring
from taker.ring_planner import ParticipantChannelLimits, RingParty, plan_cofunded_ring
from taker.tx_builder import (
    ChannelEndpointContribution,
    FinalizedOrdinaryMakerChange,
    FinalizedRingChannelOutput,
    FinalizedRingTransactionPlan,
    build_coinjoin_tx,
)

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend

    from taker.coinjoin_session import CoinJoinSession


class MakerOnlyRingCoordinator:
    """Coordinate channels among makers while keeping taker change ordinary."""

    def __init__(
        self,
        session: CoinJoinSession,
        *,
        config: ChannelRingConfig,
        store: CoordinatorStore,
        chain_backend: BlockchainBackend,
        session_identity: str,
    ) -> None:
        self.session = session
        self.config = config
        self.store = store
        self.chain_backend = chain_backend
        self.session_identity = session_identity
        self.record_filename: str | None = None
        self.acceptor_task: None = None
        self.sign_authorized = False
        self._peer_by_key: dict[str, str] = {}
        self._participant_by_key: dict[str, PrivateParticipant] = {}
        self._edge_by_id: dict[str, PrivateEdgePlan] = {}
        self._prepared_by_key: dict[str, RingPreparedPayload] = {}
        self._residuals: dict[str, int] = {}
        self._setup_deadline: float | None = None

    @property
    def session_identity_key(self) -> str:
        return self.session_identity

    @property
    def has_durable_record(self) -> bool:
        """A journal filename exists before any invitation can leave this process."""
        return self.record_filename is not None

    def _record(self) -> CoordinatorRecord:
        if self.record_filename is None:
            raise TakerRingError("durable maker-only coordinator journal is missing")
        record = next(
            (item for item in self.store.load_all() if item.filename == self.record_filename), None
        )
        if record is None:
            raise TakerRingError("durable maker-only coordinator journal is missing")
        return record

    def _sign(self, payload: RingPayloadType) -> RingPayloadType:
        record = self._record()
        return sign_payload(payload, bytes.fromhex(record.signer_secret))

    def _ring_sessions(self) -> dict[str, Any]:
        if not self.session.ring_maker_sessions:
            raise TakerRingError("authenticated ring maker subset has not been frozen")
        return self.session.ring_maker_sessions

    def _deadline_timeout(self, timeout: float) -> float:
        if self._setup_deadline is None:
            raise TakerRingError("channel-ring setup deadline has not started")
        remaining = self._setup_deadline - time.monotonic()
        if remaining <= 0:
            raise TakerRingError("channel-ring setup deadline expired")
        return min(timeout, remaining)

    async def _await_setup_action(
        self, action: Callable[[], Awaitable[Any]], timeout: float
    ) -> Any:
        return await asyncio.wait_for(action(), timeout=self._deadline_timeout(timeout))

    def _set_phase(self, phase: str) -> None:
        from taker.models import TakerState

        self.session._taker.state = TakerState(phase)

    def _freeze_authenticated_roles(self) -> None:
        if self.session.ring_maker_sessions or self.session.ordinary_maker_sessions:
            return
        candidates = self.session.ring_candidate_nicks or {
            nick
            for nick, maker in self.session.maker_sessions.items()
            if offer_supports_private_channel_ring(maker.offer)
        }
        ring = {
            nick: self.session.maker_sessions[nick]
            for nick in sorted(candidates)
            if nick in self.session.maker_sessions
            and self.session.maker_sessions[nick].responded_auth
            and offer_supports_private_channel_ring(self.session.maker_sessions[nick].offer)
        }
        ordinary = {
            nick: maker
            for nick, maker in sorted(self.session.maker_sessions.items())
            if nick not in candidates and maker.responded_auth
        }
        if len(ring) != len(candidates) or len(ring) < max(3, self.config.minimum_makers):
            raise TakerRingError("fewer than three ring-capable makers authenticated")
        if len(ring) + len(ordinary) != len(self.session.maker_sessions):
            raise TakerRingError("selected maker roles are not fully authenticated")
        self.session.ring_maker_sessions = ring
        self.session.ordinary_maker_sessions = ordinary

    def _preconditions(self) -> None:
        if self.session.is_sweep:
            raise TakerRingError("channel-ring mode does not support sweep/no-change takers")
        if getattr(self.session.wallet, "address_type", None) != "p2tr":
            raise TakerRingError("channel-ring mode requires a p2tr wallet")
        if not is_taproot_offer_type(self.session.config.preferred_offer_type):
            raise TakerRingError("channel-ring mode requires a tr0 preferred offer")
        if not self.chain_backend.has_mempool_access() or not (
            self.chain_backend.can_get_confirmations_by_txid()
        ):
            raise TakerRingError("ring cancellation requires a full absence-proving backend")
        self._freeze_authenticated_roles()
        if len(self.session.maker_sessions) < self.session.config.minimum_makers:
            raise TakerRingError("selected maker count is below the CoinJoin minimum")
        offer_types = {maker.offer.ordertype for maker in self._ring_sessions().values()}
        if offer_types != {self.session.config.preferred_offer_type}:
            raise TakerRingError("ring makers must use the exact preferred tr0 offer type")
        self.session.freeze_maker_fee_plan()

    async def _send(self, nick: str, payload: RingPayloadType) -> None:
        maker = self._ring_sessions()[nick]
        if maker.crypto is None:
            raise TakerRingError("ring peer has no authenticated encryption session")
        encrypted = maker.crypto.encrypt(encode_ring_message(payload))
        await self._await_setup_action(
            lambda: self.session.directory_client.send_privmsg(
                nick, "ring", encrypted, log_routing=True, force_channel=maker.comm_channel
            ),
            self.config.phase_timeout_seconds,
        )

    async def _receive(
        self, expected_type: type[Any], *, expected_counts: int = 1, timeout: float | None = None
    ) -> dict[str, list[RingPayloadType]]:
        nicks = list(self._ring_sessions())
        counts = {nick: expected_counts for nick in nicks} if expected_counts > 1 else None
        responses = await self._await_setup_action(
            lambda: self.session.directory_client.wait_for_responses(
                expected_nicks=nicks,
                expected_command="!ring",
                timeout=self._deadline_timeout(
                    self.config.phase_timeout_seconds if timeout is None else timeout
                ),
                expected_counts=counts,
            ),
            self.config.phase_timeout_seconds if timeout is None else timeout,
        )
        result: dict[str, list[RingPayloadType]] = {}
        for nick in nicks:
            response = responses.get(nick)
            if response is None or response.get("error"):
                raise TakerRingError(f"ring peer failed during {expected_type.message_type}")
            values = response["data"]
            encoded_values = values if isinstance(values, list) else [values]
            if len(encoded_values) != expected_counts:
                raise TakerRingError("ring peer returned an unexpected number of responses")
            maker = self._ring_sessions()[nick]
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
        if set(payloads) != set(self._ring_sessions()):
            raise TakerRingError("ring phase payloads do not cover exactly the frozen ring makers")
        await asyncio.gather(*(self._send(nick, payload) for nick, payload in payloads.items()))
        return await self._receive(expected_type, expected_counts=expected_counts, timeout=timeout)

    def _finalize_fees_and_inputs(self, mixdepth: int) -> tuple[int, dict[str, int]]:
        maker_fee_plan = self.session.maker_fee_plan()
        participant_count = len(self.session.maker_sessions) + 1
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
        required = self.session.cj_amount + sum(maker_fee_plan.values()) + tx_fee
        selected = self.session.preselected_utxos
        if sum(utxo.value for utxo in selected) < required:
            if self.session.strict_input_selection:
                raise TakerRingError("Explicit input UTXOs are insufficient after negotiated fees")
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
        residuals = {
            "taker": sum(utxo.value for utxo in selected)
            - self.session.cj_amount
            - sum(maker_fee_plan.values())
            - tx_fee
        }
        for nick, maker in self.session.maker_sessions.items():
            residuals[nick] = (
                sum(int(utxo["value"]) for utxo in maker.utxos)
                - self.session.cj_amount
                + maker_fee_plan[nick]
                - maker.offer.txfee
            )
        if any(value <= 0 for value in residuals.values()):
            raise TakerRingError("ring participant has no positive finalized residual")
        if residuals["taker"] < DUST_THRESHOLD or any(
            residuals[nick] < DUST_THRESHOLD for nick in self.session.ordinary_maker_sessions
        ):
            raise TakerRingError("final fee plan leaves an ordinary change below dust")
        return tx_fee, residuals

    def _limits(self, backend: BackendLimits) -> ParticipantChannelLimits:
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

    async def _invite(self, tx_fee: int, residuals: dict[str, int], mixdepth: int) -> None:
        if any(utxo.mixdepth != mixdepth for utxo in self.session.selected_utxos):
            raise TakerRingError("ring inputs must all belong to the source mixdepth")
        self._setup_deadline = time.monotonic() + self.config.setup_timeout_seconds
        required_until = (
            time.monotonic()
            + self.config.setup_timeout_seconds
            + self.config.hold_safety_margin_seconds
        )
        for maker in self.session.maker_sessions.values():
            if maker.hold_deadline is None or maker.hold_deadline <= required_until:
                raise TakerRingError("authenticated maker hold does not cover channel-ring setup")
        self._set_phase("ring_inviting")
        reached_nicks = tuple(sorted(self._ring_sessions()))
        signer = RingKeyPair.from_secret(secrets.token_bytes(32))
        record = CoordinatorRecord.fresh(
            signer=signer,
            round_nonce=secrets.token_hex(32),
            network=cast(Any, self.session.config.network.value),
            wallet_identity=self.store.wallet_identity,
            session_identity=self.session_identity,
            source_mixdepth=mixdepth,
            input_outpoints=tuple(
                Outpoint(txid=utxo.txid, vout=utxo.vout) for utxo in self.session.selected_utxos
            ),
            input_lock_owner=self.session.input_lock_owner,
            reached_nicks=reached_nicks,
        )
        self.store.save(record)
        self.record_filename = record.filename
        invite = cast(
            RingInvitePayload,
            self._sign(
                RingInvitePayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.signer_key,
                    network=cast(Any, self.session.config.network.value),
                    offer_type=cast(Any, self.session.config.preferred_offer_type.value),
                    expiry=int(
                        time.time() + self._deadline_timeout(self.config.phase_timeout_seconds)
                    ),
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
        remote = await self._exchange({nick: invite for nick in reached_nicks}, RingHelloPayload)
        participants: dict[str, PrivateParticipant] = {}
        for nick, items in remote.items():
            hello = cast(RingHelloPayload, items[0])
            validate_hello_for_invite(invite, hello)
            if hello.participant.backend_limits.max_pending_channels < 2:
                raise TakerRingError("ring maker cannot retain two simultaneous pending channels")
            if hello.signer_key in participants:
                raise TakerRingError("two makers returned the same ring key")
            self._peer_by_key[hello.signer_key] = nick
            participants[hello.signer_key] = hello.participant
        self._participant_by_key = participants
        self.store.transition(
            record.filename,
            CoordinatorPhase.PLANNING,
            participants=tuple(participants.values()),
            participant_nicks=self._peer_by_key,
        )
        self._residuals = residuals

    def _build_plans(self) -> dict[str, RingPlanPayload]:
        record = self._record()
        parties = [
            RingParty(
                party_id=self._peer_by_key[participant.participant_key],
                residual=self._residuals[self._peer_by_key[participant.participant_key]],
                limits=self._limits(participant.backend_limits),
            )
            for participant in self._participant_by_key.values()
        ]
        planned = plan_cofunded_ring(parties, taker_participates=False)
        key_by_nick = {nick: key for key, nick in self._peer_by_key.items()}
        cycle_keys = [key_by_nick[nick] for nick in planned.cycle]
        policy = self._policy()
        private_edges: list[PrivateEdgePlan] = []
        for edge in planned.edges:
            private = PrivateEdgePlan(
                edge_id=secrets.token_hex(32),
                pending_channel_id=secrets.token_hex(32),
                opener_key=key_by_nick[edge.opener_id],
                acceptor_key=key_by_nick[edge.fundee_id],
                capacity=edge.capacity,
                push_amount=edge.push_amount,
                policy=policy,
            )
            private_edges.append(private)
            self._edge_by_id[private.edge_id] = private
        contributions = {item.party_id: item for item in planned.contributions}
        plans_by_key: dict[str, RingPlanPayload] = {}
        for position, key in enumerate(cycle_keys):
            nick = self._peer_by_key[key]
            contribution = contributions[nick]
            plans_by_key[key] = cast(
                RingPlanPayload,
                self._sign(
                    RingPlanPayload(
                        round_nonce=record.round_nonce,
                        revision=record.revision,
                        signer_key=record.signer_key,
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
        return plans_by_key

    async def _plan(self) -> None:
        self._set_phase("ring_planning")
        plans = self._build_plans()
        record = self._record()
        self.store.transition(record.filename, CoordinatorPhase.OPENING, plans=plans)
        responses = await self._exchange(
            {self._peer_by_key[key]: plan for key, plan in plans.items()}, RingPlanAckPayload
        )
        for nick, items in responses.items():
            ack = cast(RingPlanAckPayload, items[0])
            key = next(key for key, peer_nick in self._peer_by_key.items() if nick == peer_nick)
            if ack.plan_hash != ring_hash("plan", plans[key]).hex():
                raise TakerRingError("maker acknowledged a different recipient plan")

    async def _open(self) -> None:
        self._set_phase("ring_opening")
        record = self._record()
        responses = await self._exchange(
            {
                nick: cast(
                    RingOpenPayload,
                    self._sign(
                        RingOpenPayload(
                            round_nonce=record.round_nonce,
                            revision=record.revision,
                            signer_key=record.signer_key,
                            plan_hash=ring_hash("plan", record.plans[key]).hex(),
                        )
                    ),
                )
                for key, nick in self._peer_by_key.items()
            },
            RingPreparedPayload,
            timeout=self.config.open_timeout_seconds,
        )
        prepared = {
            cast(RingPreparedPayload, items[0]).signer_key: cast(RingPreparedPayload, items[0])
            for items in responses.values()
        }
        if set(prepared) != set(self._participant_by_key):
            raise TakerRingError("prepared reports do not cover every ring maker")
        scripts: set[str] = set()
        for key, payload in prepared.items():
            plan = record.plans[key]
            if (
                payload.outgoing.pending_channel_id != plan.outgoing_edge.pending_channel_id
                or payload.outgoing.opener_key != key
                or payload.outgoing.acceptor_key != plan.successor.participant_key
                or payload.outgoing.capacity != plan.outgoing_edge.capacity
                or payload.outgoing.push_amount != plan.outgoing_edge.push_amount
                or payload.outgoing.policy != plan.outgoing_edge.policy
            ):
                raise TakerRingError("prepared outgoing report differs from its plan")
            successor = plan.successor.participant_key
            incoming = prepared[successor].incoming
            if (
                incoming.pending_channel_id != payload.outgoing.pending_channel_id
                or incoming.opener_key != key
                or incoming.acceptor_key != successor
                or incoming.opener_node_id != self._participant_by_key[key].node_id
                or incoming.capacity != payload.outgoing.capacity
                or incoming.push_amount != payload.outgoing.push_amount
                or incoming.policy != payload.outgoing.policy
            ):
                raise TakerRingError("prepared endpoints are not reciprocal")
            if payload.outgoing.script_pubkey in scripts:
                raise TakerRingError("two negotiated channels returned the same funding script")
            scripts.add(payload.outgoing.script_pubkey)
        self._prepared_by_key = prepared
        self.store.transition(record.filename, CoordinatorPhase.OPENING, prepared=prepared)

    def _transaction_plan(self, mixdepth: int) -> FinalizedRingTransactionPlan:
        record = self._record()
        if set(record.prepared) != set(record.plans) or not record.plans:
            raise TakerRingError("maker channel funding scripts are not fully prepared")
        cycle_keys = next(iter(record.plans.values())).cycle_keys
        ring_ids = [self._peer_by_key[key] for key in cycle_keys]
        participants = [
            "taker",
            *sorted(self._ring_sessions()),
            *sorted(self.session.ordinary_maker_sessions),
        ]
        outputs: list[FinalizedRingChannelOutput] = []
        for index, opener_key in enumerate(cycle_keys):
            fundee_key = cycle_keys[(index + 1) % len(cycle_keys)]
            edge = record.plans[opener_key].outgoing_edge
            prepared = record.prepared[opener_key].outgoing
            outputs.append(
                FinalizedRingChannelOutput(
                    edge_id=edge.edge_id,
                    script_pubkey=prepared.script_pubkey,
                    address=scriptpubkey_to_address(
                        bytes.fromhex(prepared.script_pubkey), self.session.config.network.value
                    ),
                    capacity=edge.capacity,
                    opener=ChannelEndpointContribution(
                        participant_id=ring_ids[index],
                        amount=record.plans[opener_key].contribution.outgoing,
                    ),
                    fundee=ChannelEndpointContribution(
                        participant_id=ring_ids[(index + 1) % len(ring_ids)],
                        amount=record.plans[fundee_key].contribution.incoming,
                    ),
                )
            )
        change = self.session.wallet.get_new_internal_address(mixdepth)
        if address_to_scriptpubkey(change).hex()[:4] != "5120":
            raise TakerRingError("maker-only taker change must use a Taproot address")
        self.session.taker_change_address = change
        return FinalizedRingTransactionPlan(
            network=self.session.config.network.value,
            participant_ids=participants,
            residuals={party: self._residuals[party] for party in participants},
            ring_participant_ids=ring_ids,
            channel_outputs=outputs,
            ordinary_taker_change=FinalizedOrdinaryMakerChange(
                address=change,
                script_pubkey=address_to_scriptpubkey(change).hex(),
                amount=self._residuals["taker"],
            ),
            ordinary_maker_changes={
                nick: FinalizedOrdinaryMakerChange(
                    address=maker.change_address,
                    script_pubkey=address_to_scriptpubkey(maker.change_address).hex(),
                    amount=self._residuals[nick],
                )
                for nick, maker in self.session.ordinary_maker_sessions.items()
            },
        )

    def _build_unsigned(
        self, destination: str, tx_fee: int, mixdepth: int
    ) -> tuple[RingUnsignedPayload, list[WitnessUtxo]]:
        ring_plan = self._transaction_plan(mixdepth)
        selected = self.session.selected_utxos
        self.session.cj_destination = destination
        maker_fee_plan = self.session.maker_fee_plan()
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
            taker_change_address=self.session.taker_change_address,
            taker_total_input=sum(utxo.value for utxo in selected),
            maker_data={
                nick: {
                    "utxos": maker.utxos,
                    "cj_addr": maker.cj_address,
                    "change_addr": maker.change_address,
                    "cjfee": maker_fee_plan[nick],
                    "txfee": maker.offer.txfee,
                }
                for nick, maker in self.session.maker_sessions.items()
            },
            cj_amount=self.session.cj_amount,
            tx_fee=tx_fee,
            network=self.session.config.network.value,
            ring_plan=ring_plan,
        )
        transaction = parse_transaction(unsigned.hex())
        total_input = sum(utxo.value for utxo in selected) + sum(
            int(utxo["value"])
            for maker in self.session.maker_sessions.values()
            for utxo in maker.utxos
        )
        expected_fee = tx_fee + sum(
            maker.offer.txfee for maker in self.session.maker_sessions.values()
        )
        if len(transaction.outputs) != 2 * len(ring_plan.participant_ids) or (
            total_input - sum(output.value for output in transaction.outputs) != expected_fee
        ):
            raise TakerRingError("maker-only ring transaction fee or output count changed")
        edge_vout: dict[str, int] = {}
        equal_indices: list[int] = []
        for index, ((_, kind), edge_id) in enumerate(
            zip(metadata["output_owners"], metadata["channel_edges"], strict=True)
        ):
            if kind == "cj":
                equal_indices.append(index)
            elif kind == "channel" and edge_id is not None:
                edge_vout[edge_id] = index
        edges = [
            RingEdge(
                opener_key=private.opener_key,
                acceptor_key=private.acceptor_key,
                pending_channel_id=private.pending_channel_id,
                capacity=private.capacity,
                output_index=edge_vout[private.edge_id],
                script_pubkey=output.script_pubkey,
                policy=private.policy,
            )
            for output in ring_plan.channel_outputs
            for private in [self._edge_by_id[output.edge_id]]
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
        witnesses = [prevouts[(item.txid, item.vout)] for item in transaction.inputs]
        psbt, txid = build_unsigned_psbt(unsigned.hex(), witnesses)
        record = self._record()
        cycle_keys = next(iter(record.plans.values())).cycle_keys
        manifest = RingManifest(
            network=cast(Any, self.session.config.network.value),
            round_nonce=record.round_nonce,
            revision=record.revision,
            unsigned_tx_hash=hashlib.sha256(unsigned).hexdigest(),
            unsigned_txid=txid,
            participant_keys=cycle_keys,
            edges=edges,
            equal_output_indices=equal_indices,
            outputs=[
                ManifestOutput(index=index, amount=output.value, script_pubkey=output.scriptpubkey)
                for index, output in enumerate(transaction.outputs)
            ],
        )
        self.session.unsigned_tx = unsigned
        self.session.tx_metadata = metadata
        return (
            cast(
                RingUnsignedPayload,
                self._sign(
                    RingUnsignedPayload(
                        round_nonce=record.round_nonce,
                        revision=record.revision,
                        signer_key=record.signer_key,
                        unsigned_tx=unsigned.hex(),
                        psbt=base64.b64encode(psbt).decode("ascii"),
                        manifest=manifest,
                    )
                ),
            ),
            witnesses,
        )

    def _validate_readiness(
        self, manifest: RingManifest, remote: Mapping[str, list[RingPayloadType]]
    ) -> tuple[SignedReadinessAttestation, ...]:
        states: dict[tuple[str, EndpointRole, str], ReadinessState] = {}
        attestations: list[SignedReadinessAttestation] = []
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
                    raise TakerRingError("readiness state differs from its manifest edge")
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
            if opener.model_dump(exclude={"role", "signer_key", "state_salt"}) != fundee.model_dump(
                exclude={"role", "signer_key", "state_salt"}
            ):
                raise TakerRingError("opener and fundee readiness states are not reciprocal")
        if len(attestations) != 2 * len(manifest.edges):
            raise TakerRingError("readiness set lacks exactly two states per edge")
        return tuple(attestations)

    async def _readiness_and_sign(
        self, unsigned: RingUnsignedPayload, witnesses: list[WitnessUtxo]
    ) -> None:
        self._set_phase("ring_verifying")
        record = self._record()
        psbt, txid = build_unsigned_psbt(unsigned.unsigned_tx, witnesses)
        if (
            txid != unsigned.manifest.unsigned_txid
            or base64.b64encode(psbt).decode("ascii") != unsigned.psbt
        ):
            raise TakerRingError("maker-only funding PSBT differs from the signed manifest")
        # This durable intent precedes any maker seeing the funding transaction.
        record = self.store.transition(
            record.filename,
            CoordinatorPhase.UNSIGNED,
            unsigned_tx=unsigned.unsigned_tx,
            unsigned_psbt=psbt.hex(),
            manifest=unsigned.manifest,
            chain_status=RingChainStatus(exact_txid=txid),
        )
        remote = await self._exchange(
            {nick: unsigned for nick in self._ring_sessions()},
            RingReadyPayload,
            expected_counts=2,
            timeout=self.config.readiness_timeout_seconds,
        )
        readiness = self._validate_readiness(unsigned.manifest, remote)
        readiness_hash = ring_hash("ready-set", readiness).hex()
        record = self.store.transition(
            record.filename, CoordinatorPhase.READY, readiness_set=readiness
        )
        self._set_phase("ring_readiness")
        ready_set = cast(
            RingReadySetPayload,
            self._sign(
                RingReadySetPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.signer_key,
                    manifest=unsigned.manifest,
                    attestations=list(readiness),
                )
            ),
        )
        ready_acks = await self._exchange(
            {nick: ready_set for nick in self._ring_sessions()}, RingReadySetAckPayload
        )
        acknowledged: list[str] = []
        for items in ready_acks.values():
            ack = cast(RingReadySetAckPayload, items[0])
            if ack.readiness_hash != readiness_hash:
                raise TakerRingError("maker acknowledged another readiness set")
            acknowledged.append(ack.signer_key)
        record = self.store.transition(
            record.filename, CoordinatorPhase.READY, ready_ack_keys=tuple(acknowledged)
        )
        sign = cast(
            RingSignPayload,
            self._sign(
                RingSignPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.signer_key,
                    manifest_hash=manifest_hash(unsigned.manifest).hex(),
                    unsigned_tx_hash=unsigned.manifest.unsigned_tx_hash,
                )
            ),
        )
        record = self.store.transition(
            record.filename, CoordinatorPhase.SIGN_AUTHORIZED, sign_authorization_sent=True
        )
        self.sign_authorized = True
        self._set_phase("ring_authorizing_signatures")
        sign_acks = await self._exchange(
            {nick: sign for nick in self._ring_sessions()}, RingSignAckPayload
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
            record.filename, CoordinatorPhase.SIGNING, sign_ack_keys=tuple(signed_keys)
        )

    async def prepare(self, destination: str, mixdepth: int) -> bool:
        """Advance only with complete maker attestations or retain unresolved state."""
        try:
            self._preconditions()
            tx_fee, residuals = self._finalize_fees_and_inputs(mixdepth)
            await self._invite(tx_fee, residuals, mixdepth)
            await self._plan()
            await self._open()
            unsigned, witnesses = self._build_unsigned(destination, tx_fee, mixdepth)
            await self._readiness_and_sign(unsigned, witnesses)
            return True
        except Exception as exc:
            logger.error("Maker-only ring setup failed: {}", type(exc).__name__)
            if self.record_filename is not None and not self.sign_authorized:
                await self.cancel("coordinator_failure")
            return False

    async def cancel(self, reason_code: str) -> None:
        """Retire only after every reached maker acknowledges and the TX is absent."""
        self._setup_deadline = float("inf")
        record = self._record()
        if record.sign_authorization_sent or record.input_signature_creation_intent:
            return
        # Persist uncertainty before contacting makers; an interrupted cancellation
        # must not release taker inputs or start a new round on missing replies.
        record = self.store.transition(record.filename, CoordinatorPhase.RECOVERY_REQUIRED)
        cancel = cast(
            RingCancelPayload,
            self._sign(
                RingCancelPayload(
                    round_nonce=record.round_nonce,
                    revision=record.revision,
                    signer_key=record.signer_key,
                    reason_code=reason_code,
                    canceled_pending_ids=sorted(
                        {plan.outgoing_edge.pending_channel_id for plan in record.plans.values()}
                    ),
                )
            ),
        )
        try:
            replies = await self._exchange(
                {nick: cancel for nick in record.reached_nicks}, RingCancelPayload
            )
            for nick, items in replies.items():
                response = cast(RingCancelPayload, items[0])
                if response.reason_code != reason_code:
                    raise TakerRingError("ring cancellation response changed the reason")
                key = next(
                    (key for key, peer in record.participant_nicks.items() if peer == nick), None
                )
                if key is not None and key in record.plans:
                    plan = record.plans[key]
                    planned_ids = {
                        plan.incoming_edge.pending_channel_id,
                        plan.outgoing_edge.pending_channel_id,
                    }
                    if not set(response.canceled_pending_ids).issubset(planned_ids):
                        raise TakerRingError("maker cancellation reported an unplanned channel")
                    if key in record.prepared and (
                        plan.outgoing_edge.pending_channel_id not in response.canceled_pending_ids
                    ):
                        raise TakerRingError("prepared maker channel was not canceled")
                elif response.canceled_pending_ids:
                    raise TakerRingError("unplanned maker reported a pending channel")
            if record.manifest is not None:
                if (
                    await self.chain_backend.get_transaction(record.manifest.unsigned_txid)
                    is not None
                ):
                    raise TakerRingError("exact ring transaction is present in mempool or chain")
                for outpoint in record.input_outpoints:
                    if await self.chain_backend.get_utxo(outpoint.txid, outpoint.vout) is None:
                        raise TakerRingError("taker input is spent while the ring TX is absent")
                status = RingChainStatus(
                    exact_txid=record.manifest.unsigned_txid,
                    mempool=TransactionPresence.ABSENT,
                    chain=TransactionPresence.ABSENT,
                    checked_at=time.time(),
                )
            else:
                status = record.chain_status
            self.store.transition(
                record.filename,
                CoordinatorPhase.RETIRED,
                chain_status=status,
                canceled_nicks=record.reached_nicks,
            )
        except Exception as exc:
            logger.warning(
                "Maker-only ring cancellation remains unresolved: {}", type(exc).__name__
            )

    def mark_local_signature_creation(self) -> None:
        record = self._record()
        if record.phase is not CoordinatorPhase.SIGNING:
            raise TakerRingError("local CoinJoin signing lacks all maker sign acknowledgments")
        self.store.transition(
            record.filename, CoordinatorPhase.SIGNING, input_signature_creation_intent=True
        )

    def mark_local_signatures(self, signatures: Sequence[Mapping[str, Any]]) -> None:
        if not signatures:
            raise TakerRingError("local maker-only ring signing produced no signatures")
        record = self._record()
        if not record.input_signature_creation_intent:
            raise TakerRingError("maker-only signing was not durably authorized")
        encoded = tuple(
            f"{item['txid']}:{item['vout']}:{','.join(cast(Sequence[str], item['witness']))}"
            for item in signatures
        )
        self.store.transition(
            record.filename,
            CoordinatorPhase.SIGNED,
            input_signature_sent=True,
            local_signatures=encoded,
        )

    def persist_final_transaction(self, tx_hex: str) -> None:
        record = self._record()
        if record.phase is not CoordinatorPhase.SIGNED or record.manifest is None:
            raise TakerRingError("final maker-only transaction persistence is out of order")
        if get_txid(tx_hex) != record.manifest.unsigned_txid:
            raise TakerRingError("assembled final transaction differs from the signed ring")
        self.store.transition(record.filename, CoordinatorPhase.BROADCAST, final_tx=tx_hex)


async def reconcile_maker_only_ring_records(
    store: CoordinatorStore,
    chain_backend: BlockchainBackend,
    *,
    active_session_identities: frozenset[str] = frozenset(),
) -> None:
    """Surface stranded rounds and rebroadcast only an exact durable final transaction.

    A restarted coordinator cannot cancel the maker's channel shims: the
    authenticated maker session that carried its encrypted !ring messages is
    gone. Never infer cancellation from a missing transaction or an expired
    hold. Retain inputs until a separate, provable recovery path exists.
    """
    for record in store.load_all():
        if not record.active or record.session_identity in active_session_identities:
            continue
        try:
            transaction = (
                await chain_backend.get_transaction(record.manifest.unsigned_txid)
                if record.manifest is not None
                else None
            )
            if transaction is not None and transaction.confirmations >= 1:
                assert record.manifest is not None
                # Earlier phases cannot transition directly to CONFIRMED. Persist
                # uncertainty first so an interruption remains fail closed.
                if record.phase not in {
                    CoordinatorPhase.BROADCAST,
                    CoordinatorPhase.RECOVERY_REQUIRED,
                }:
                    record = store.transition(record.filename, CoordinatorPhase.RECOVERY_REQUIRED)
                store.transition(
                    record.filename,
                    CoordinatorPhase.CONFIRMED,
                    chain_status=RingChainStatus(
                        exact_txid=record.manifest.unsigned_txid,
                        chain=TransactionPresence.PRESENT,
                        confirmations=transaction.confirmations,
                        checked_at=time.time(),
                    ),
                )
                continue
            if (
                record.phase in {CoordinatorPhase.BROADCAST, CoordinatorPhase.RECOVERY_REQUIRED}
                and record.final_tx is not None
                and record.manifest is not None
            ):
                if transaction is None:
                    await chain_backend.broadcast_transaction(record.final_tx)
                continue
            if record.phase is not CoordinatorPhase.RECOVERY_REQUIRED:
                store.transition(record.filename, CoordinatorPhase.RECOVERY_REQUIRED)
            logger.bind(sensitive=True).error(
                "Maker-only ring {} cannot resume an authenticated maker session; "
                "retaining its input locks for operator recovery",
                record.filename,
            )
        except Exception as exc:
            if record.phase is not CoordinatorPhase.RECOVERY_REQUIRED:
                store.transition(record.filename, CoordinatorPhase.RECOVERY_REQUIRED)
            logger.bind(sensitive=True).error(
                "Maker-only ring {} chain reconciliation failed ({}); retaining its input locks",
                record.filename,
                type(exc).__name__,
            )
