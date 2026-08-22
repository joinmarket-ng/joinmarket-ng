"""Crash-safe taker ownership and recovery for channel-input negotiations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Protocol

from jmcore.channel_input import (
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputApprovalSetPayload,
    ChannelInputCancelAckPayload,
    ChannelInputCancelPayload,
    ChannelInputFinalizedPayload,
    ChannelInputFinalizePayload,
    ChannelInputHelloPayload,
    ChannelInputPartialPayload,
    ChannelInputPreparedPayload,
    ChannelInputPreparePayload,
    ChannelInputProposalPayload,
    ChannelInputSignPayload,
    ChannelInputStatusPayload,
    ChannelInputStatusRequestPayload,
    ChannelInputTranscript,
    ChannelInputValidationError,
    EndpointTerms,
    Network,
    PolicyCeilings,
)
from jmcore.channel_input_config import ChannelInputConfig
from jmswap.lnd import (
    ChannelInputCandidate,
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
)

from taker.channel_input_negotiation import (
    TakerChannelInputNegotiation,
    TakerChannelInputNegotiationError,
    TakerChannelInputStage,
)
from taker.channel_input_store import TakerChannelInputStore, TakerChannelInputStoreError


class TakerChannelInputRuntimeError(Exception):
    """A taker channel-input attempt cannot safely be recovered or advanced."""


class TakerChannelInputBackend(Protocol):
    """The minimal LND escrow surface required by the taker runtime."""

    async def find_channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus | None: ...

    async def freeze_channel_input(
        self, session_id: str, channel_id: str
    ) -> FrozenChannelSnapshot: ...

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool: ...


class TakerChannelInputRegistry:
    """Own durable taker negotiations and serialize each attempt's LND transitions."""

    def __init__(
        self,
        *,
        store: TakerChannelInputStore,
        config: ChannelInputConfig,
        network: Network,
        local_actor: str,
    ) -> None:
        self._store = store
        self._config = config
        self._network = network
        self._local_actor = local_actor
        self._negotiations: dict[tuple[str, int], TakerChannelInputNegotiation] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._admission_lock = asyncio.Lock()
        self._load()

    @classmethod
    def load(
        cls,
        *,
        store: TakerChannelInputStore,
        config: ChannelInputConfig,
        network: Network,
        local_actor: str,
    ) -> TakerChannelInputRegistry:
        """Restore all snapshots or fail before any capability is advertised."""

        return cls(store=store, config=config, network=network, local_actor=local_actor)

    @property
    def session_count(self) -> int:
        return sum(
            negotiation.stage is not TakerChannelInputStage.CANCELED
            for negotiation in self._negotiations.values()
        )

    @property
    def active_negotiations(self) -> tuple[TakerChannelInputNegotiation, ...]:
        """Return active negotiations in a stable immutable order."""

        return tuple(
            negotiation
            for _, negotiation in sorted(self._negotiations.items())
            if negotiation.stage is not TakerChannelInputStage.CANCELED
        )

    @property
    def recovery_outpoints(self) -> tuple[EscrowOutpoint, ...]:
        """Return active durable freeze intents in a stable order."""

        return tuple(
            sorted(
                (
                    intent.funding_outpoint
                    for negotiation in self._negotiations.values()
                    if negotiation.stage is not TakerChannelInputStage.CANCELED
                    for _, intent in negotiation.freeze_intents
                ),
                key=lambda outpoint: (outpoint.txid, outpoint.vout),
            )
        )

    def get(self, session_id: str, attempt: int) -> TakerChannelInputNegotiation | None:
        return self._negotiations.get((session_id, attempt))

    def lock_for(self, session_id: str, attempt: int) -> asyncio.Lock:
        try:
            return self._locks[(session_id, attempt)]
        except KeyError as exc:
            raise TakerChannelInputRuntimeError("unknown channel-input session attempt") from exc

    async def create(
        self,
        *,
        selected_nicks: Sequence[str],
        secret_key: bytes,
        session_id: str,
        attempt: int,
        expiry: int,
        max_parent_fee_sat: int | None = None,
        max_fee_rate_sat_vb: int | None = None,
        min_recovery_delta: int | None = None,
        max_recovery_delta: int | None = None,
    ) -> TakerChannelInputNegotiation:
        """Create and persist a coordinator attempt before exposing it in memory."""

        key = (session_id, attempt)
        async with self._admission_lock:
            if key in self._negotiations:
                raise TakerChannelInputRuntimeError("duplicate channel-input session attempt")
            if self.session_count >= self._store.max_active_sessions:
                raise TakerChannelInputRuntimeError("maximum active channel-input sessions reached")
            negotiation = TakerChannelInputNegotiation.create(
                network=self._network,
                local_actor=self._local_actor,
                selected_nicks=selected_nicks,
                secret_key=secret_key,
                session_id=session_id,
                attempt=attempt,
                expiry=expiry,
                max_parent_fee_sat=(
                    max_parent_fee_sat
                    if max_parent_fee_sat is not None
                    else self._config.max_parent_fee_sat
                ),
                max_fee_rate_sat_vb=(
                    max_fee_rate_sat_vb
                    if max_fee_rate_sat_vb is not None
                    else self._config.max_fee_rate_sat_vb
                ),
                min_recovery_delta=(
                    min_recovery_delta
                    if min_recovery_delta is not None
                    else self._config.recovery_delay_blocks
                ),
                max_recovery_delta=(
                    max_recovery_delta
                    if max_recovery_delta is not None
                    else self._config.recovery_delay_blocks
                ),
            )
            self._validate_config_relevance(negotiation)
            self._save(negotiation)
            self._negotiations[key] = negotiation
            self._locks[key] = asyncio.Lock()
            return negotiation

    async def accept_remote_hello(
        self,
        session_id: str,
        attempt: int,
        nick: str,
        remote_hello: ChannelInputHelloPayload,
        *,
        backend: TakerChannelInputBackend,
        local_node_key: str,
        candidates: Sequence[ChannelInputCandidate],
        settlement_script_pubkey: str,
        quoted_miner_fee_share_sat: int,
        policy_ceilings: PolicyCeilings,
    ) -> ChannelInputHelloPayload:
        """Persist a maker hello's freeze intent, then return its durable local hello."""

        negotiation = self._require(session_id, attempt)
        async with self._admission_lock:
            async with self.lock_for(session_id, attempt):
                self._require_active_local_identity(negotiation, local_node_key)
                candidate = self._candidate_for_remote_hello(negotiation, candidates, remote_hello)
                try:
                    negotiation.record_freeze_intent(
                        nick,
                        remote_hello,
                        candidate,
                        local_node_key=local_node_key,
                        settlement_script_pubkey=settlement_script_pubkey,
                        quoted_miner_fee_share_sat=quoted_miner_fee_share_sat,
                        policy_ceilings=policy_ceilings,
                    )
                except (ChannelInputValidationError, TypeError, ValueError) as exc:
                    raise TakerChannelInputRuntimeError(
                        "remote hello conflicts with local channel state"
                    ) from exc
                self._save(negotiation)
                await self._recover_freezes(negotiation, backend, nicks=(nick,))
                local = dict(negotiation.local_hellos).get(nick)
                if local is None:
                    raise TakerChannelInputRuntimeError(
                        "freeze recovery did not produce a local hello"
                    )
                return local

    async def plan_cancellation(
        self, session_id: str, attempt: int, reason_code: str
    ) -> ChannelInputCancelPayload:
        """Sign and durably record cancellation before handing it to transport."""

        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                cancel = negotiation.plan_cancel(reason_code)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "could not plan channel-input cancellation"
                ) from exc
            self._save(negotiation)
            return cancel

    async def plan_proposals(
        self,
        session_id: str,
        attempt: int,
        transcript: ChannelInputTranscript,
        endpoint_terms: Sequence[EndpointTerms],
        *,
        current_time: int,
        current_height: int,
    ) -> tuple[tuple[str, ChannelInputProposalPayload], ...]:
        """Durably build the per-maker proposal plan before transport exposes it."""

        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                proposals = negotiation.plan_proposals(
                    transcript,
                    endpoint_terms,
                    current_time=current_time,
                    current_height=current_height,
                )
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "could not plan channel-input proposals"
                ) from exc
            self._save(negotiation)
            return proposals

    async def accept_maker_approval(
        self,
        session_id: str,
        attempt: int,
        nick: str,
        approval: ChannelInputApprovalPayload,
    ) -> None:
        """Durably accept one maker approval, including an exact replay."""

        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                negotiation.accept_approval(nick, approval)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "maker approval conflicts with negotiation"
                ) from exc
            self._save(negotiation)

    async def build_approval_set(
        self, session_id: str, attempt: int
    ) -> ChannelInputApprovalSetPayload:
        """Durably build the complete approval set before transport exposes it."""

        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                approval_set = negotiation.build_approval_set()
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "could not build channel-input approval set"
                ) from exc
            self._save(negotiation)
            return approval_set

    async def accept_maker_approval_ack(
        self,
        session_id: str,
        attempt: int,
        nick: str,
        ack: ChannelInputApprovalSetAckPayload,
    ) -> None:
        """Durably accept one maker approval-set ACK, including an exact replay."""

        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                negotiation.accept_approval_ack(nick, ack)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "maker approval-set ACK conflicts with negotiation"
                ) from exc
            self._save(negotiation)

    async def build_prepare(self, session_id: str, attempt: int) -> ChannelInputPreparePayload:
        """Durably build the signed prepare authorization without sending it."""

        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                prepare = negotiation.build_prepare()
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "could not build channel-input prepare"
                ) from exc
            self._save(negotiation)
            return prepare

    async def accept_remote_prepared(
        self, session_id: str, attempt: int, nick: str, payload: ChannelInputPreparedPayload
    ) -> None:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                negotiation.accept_remote_prepared(nick, payload)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "maker prepared conflicts with negotiation"
                ) from exc
            self._save(negotiation)

    async def record_local_prepared(
        self, session_id: str, attempt: int, nick: str, signing_attempt: LndEscrowSigningAttempt
    ) -> ChannelInputPreparedPayload:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                prepared = negotiation.record_local_prepared(nick, signing_attempt)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "could not record local prepared evidence"
                ) from exc
            self._save(negotiation)
            return prepared

    async def plan_local_prepared_rotation(self, session_id: str, attempt: int, nick: str) -> None:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                negotiation.plan_local_prepared_rotation(nick)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "could not plan local prepared rotation"
                ) from exc
            self._save(negotiation)

    async def build_sign_request(
        self, session_id: str, attempt: int, nick: str
    ) -> ChannelInputSignPayload:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                request = negotiation.build_sign_request(nick)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "could not build channel-input sign request"
                ) from exc
            self._save(negotiation)
            return request

    async def accept_remote_partial(
        self, session_id: str, attempt: int, nick: str, payload: ChannelInputPartialPayload
    ) -> None:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                negotiation.accept_remote_partial(nick, payload)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "maker partial conflicts with negotiation"
                ) from exc
            self._save(negotiation)

    async def record_local_partial(
        self, session_id: str, attempt: int, nick: str, partial_signature: bytes
    ) -> ChannelInputPartialPayload:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                partial = negotiation.record_local_partial(nick, partial_signature)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "could not record local partial evidence"
                ) from exc
            self._save(negotiation)
            return partial

    async def build_finalize_request(
        self, session_id: str, attempt: int, nick: str
    ) -> ChannelInputFinalizePayload:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                request = negotiation.build_finalize_request(nick)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "could not build channel-input finalize request"
                ) from exc
            self._save(negotiation)
            return request

    async def accept_remote_finalized(
        self, session_id: str, attempt: int, nick: str, payload: ChannelInputFinalizedPayload
    ) -> None:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                negotiation.accept_remote_finalized(nick, payload)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError("maker finalized evidence conflicts") from exc
            self._save(negotiation)

    async def build_status_request(
        self, session_id: str, attempt: int, nick: str
    ) -> ChannelInputStatusRequestPayload:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                request = negotiation.build_status_request(nick)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError("could not build status request") from exc
            self._save(negotiation)
            return request

    async def accept_remote_status(
        self, session_id: str, attempt: int, nick: str, response: ChannelInputStatusPayload
    ) -> None:
        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                negotiation.accept_remote_status(nick, response)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "maker status conflicts with negotiation"
                ) from exc
            self._save(negotiation)

    async def accept_maker_cancel_ack(
        self,
        session_id: str,
        attempt: int,
        nick: str,
        ack: ChannelInputCancelAckPayload,
    ) -> None:
        """Persist a maker cancellation ACK before any local escrow resume."""

        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            try:
                negotiation.accept_cancel_ack(nick, ack)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "maker cancellation ACK conflicts with recovery"
                ) from exc
            self._save(negotiation)

    async def attempt_local_cancellation_completion(
        self, session_id: str, attempt: int, backend: TakerChannelInputBackend
    ) -> None:
        """Resume local escrows and finish only once every maker ACK is durable."""

        negotiation = self._require(session_id, attempt)
        async with self.lock_for(session_id, attempt):
            await self._resume_cancellation(negotiation, backend)

    async def reconcile_all(self, backend: TakerChannelInputBackend, local_node_key: str) -> None:
        """Reconcile every durable negotiation before enabling its directory capability."""

        for negotiation in self._negotiations.values():
            self._require_active_local_identity(negotiation, local_node_key)
        for key in sorted(self._negotiations):
            async with self._locks[key]:
                await self._reconcile_one(self._negotiations[key], backend)

    def _load(self) -> None:
        try:
            report = self._store.load_all()
        except TakerChannelInputStoreError as exc:
            raise TakerChannelInputRuntimeError(
                "could not load channel-input recovery state"
            ) from exc
        if report.corruptions:
            raise TakerChannelInputRuntimeError("channel-input recovery state is corrupt")

        active_outpoints: set[tuple[str, int]] = set()
        for snapshot in report.records:
            if snapshot.stage is TakerChannelInputStage.CANCELED and snapshot.invite.expiry <= int(
                time.time()
            ):
                try:
                    self._store.delete(snapshot)
                except TakerChannelInputStoreError as exc:
                    raise TakerChannelInputRuntimeError(
                        "could not prune expired channel-input tombstone"
                    ) from exc
                continue
            key = (snapshot.invite.session_id, snapshot.invite.attempt)
            if key in self._negotiations:
                raise TakerChannelInputRuntimeError("duplicate channel-input session attempt")
            if snapshot.network != self._network:
                raise TakerChannelInputRuntimeError(
                    "persisted channel-input network differs from runtime network"
                )
            if snapshot.local_actor != self._local_actor:
                raise TakerChannelInputRuntimeError(
                    "persisted channel-input session belongs to another taker identity"
                )
            try:
                negotiation = TakerChannelInputNegotiation.from_snapshot(snapshot)
            except (
                TakerChannelInputNegotiationError,
                ChannelInputValidationError,
                TypeError,
                ValueError,
            ) as exc:
                raise TakerChannelInputRuntimeError(
                    "channel-input recovery state is invalid"
                ) from exc
            self._validate_active_freeze_evidence(negotiation)
            self._validate_config_relevance(negotiation)
            if negotiation.stage is not TakerChannelInputStage.CANCELED:
                funding_outpoints = {
                    (hello.funding_outpoint.txid, hello.funding_outpoint.vout)
                    for _, hello in negotiation.remote_hellos
                } | {
                    (intent.funding_outpoint.txid, intent.funding_outpoint.vout)
                    for _, intent in negotiation.freeze_intents
                }
                for outpoint in funding_outpoints:
                    if outpoint in active_outpoints:
                        raise TakerChannelInputRuntimeError(
                            "channel-input funding outpoint belongs to multiple session attempts"
                        )
                    active_outpoints.add(outpoint)
            self._negotiations[key] = negotiation
            self._locks[key] = asyncio.Lock()

    @staticmethod
    def _validate_active_freeze_evidence(negotiation: TakerChannelInputNegotiation) -> None:
        if negotiation.stage is TakerChannelInputStage.CANCELED:
            return
        remote_nicks = {nick for nick, _ in negotiation.remote_hellos}
        intent_nicks = {nick for nick, _ in negotiation.freeze_intents}
        local_nicks = {nick for nick, _ in negotiation.local_hellos}
        frozen_nicks = {nick for nick, _ in negotiation.frozen_inputs}
        if remote_nicks != intent_nicks or local_nicks != frozen_nicks:
            raise TakerChannelInputRuntimeError(
                "active channel-input hello evidence is not backed by durable freeze evidence"
            )

    def _validate_config_relevance(self, negotiation: TakerChannelInputNegotiation) -> None:
        invite = negotiation.invite
        if (
            invite.max_parent_fee_sat > self._config.max_parent_fee_sat
            or invite.max_fee_rate_sat_vb > self._config.max_fee_rate_sat_vb
            or invite.min_recovery_delta < self._config.recovery_delay_blocks
        ):
            raise TakerChannelInputRuntimeError(
                "persisted channel-input policy exceeds runtime configuration"
            )
        allowed_outpoints = set(self._config.allowed_channel_points)
        allowed_peers = set(self._config.allowed_peer_node_pubkeys)
        for _, intent in negotiation.freeze_intents:
            point = f"{intent.funding_outpoint.txid}:{intent.funding_outpoint.vout}"
            if (
                point not in allowed_outpoints
                or (allowed_peers and intent.peer_node_pubkey not in allowed_peers)
                or not self._config.min_channel_capacity_sat
                <= intent.capacity_sat
                <= self._config.max_channel_capacity_sat
                or intent.policy_ceilings.max_miner_fee_share_sat
                > self._config.max_miner_fee_share_sat
                or intent.policy_ceilings.max_claim_adjustment_sat
                > self._config.max_claim_adjustment_sat
            ):
                raise TakerChannelInputRuntimeError(
                    "persisted channel-input intent conflicts with runtime configuration"
                )

    def _require(self, session_id: str, attempt: int) -> TakerChannelInputNegotiation:
        try:
            return self._negotiations[(session_id, attempt)]
        except KeyError as exc:
            raise TakerChannelInputRuntimeError("unknown channel-input session attempt") from exc

    def _require_active_local_identity(
        self, negotiation: TakerChannelInputNegotiation, local_node_key: str
    ) -> None:
        if negotiation.local_actor != self._local_actor:
            raise TakerChannelInputRuntimeError(
                "channel-input session belongs to another taker identity"
            )
        if negotiation.stage is TakerChannelInputStage.CANCELED:
            return
        if any(hello.peer_node_key != local_node_key for _, hello in negotiation.remote_hellos):
            raise TakerChannelInputRuntimeError(
                "persisted channel-input session belongs to another local node"
            )

    def _candidate_for_remote_hello(
        self,
        negotiation: TakerChannelInputNegotiation,
        candidates: Sequence[ChannelInputCandidate],
        remote_hello: ChannelInputHelloPayload,
    ) -> ChannelInputCandidate:
        unavailable = {
            (intent.funding_outpoint.txid, intent.funding_outpoint.vout)
            for other in self._negotiations.values()
            if other is not negotiation and other.stage is not TakerChannelInputStage.CANCELED
            for _, intent in other.freeze_intents
        }
        matching = tuple(
            candidate
            for candidate in candidates
            if candidate.peer_node_pubkey == remote_hello.node_key
            and candidate.funding_outpoint.txid == remote_hello.funding_outpoint.txid
            and candidate.funding_outpoint.vout == remote_hello.funding_outpoint.vout
            and candidate.capacity_sat == remote_hello.capacity_sat
            and (candidate.funding_outpoint.txid, candidate.funding_outpoint.vout)
            not in unavailable
        )
        if len(matching) != 1:
            raise TakerChannelInputRuntimeError(
                "remote hello does not match one eligible local channel candidate"
            )
        return matching[0]

    async def _reconcile_one(
        self, negotiation: TakerChannelInputNegotiation, backend: TakerChannelInputBackend
    ) -> None:
        if negotiation.stage is TakerChannelInputStage.CANCELED:
            return
        if negotiation.stage is TakerChannelInputStage.FREEZE_INTENDED:
            await self._recover_freezes(negotiation, backend)
            return
        if negotiation.stage in {
            TakerChannelInputStage.HELLOS_READY,
            TakerChannelInputStage.PROPOSALS_READY,
            TakerChannelInputStage.APPROVAL_SET_READY,
        }:
            await self._require_locked_frozen_inputs(negotiation, backend)
            return
        if negotiation.stage is TakerChannelInputStage.CANCEL_INTENDED:
            await self._resume_cancellation(negotiation, backend)
            return
        if negotiation.stage in {
            TakerChannelInputStage.INVITED,
            TakerChannelInputStage.PREPARE_READY,
        }:
            return
        raise TakerChannelInputRuntimeError("unsupported channel-input recovery stage")

    async def _recover_freezes(
        self,
        negotiation: TakerChannelInputNegotiation,
        backend: TakerChannelInputBackend,
        *,
        nicks: tuple[str, ...] | None = None,
    ) -> None:
        intents = tuple(
            (nick, intent)
            for nick, intent in negotiation.freeze_intents
            if nicks is None or nick in nicks
        )
        statuses = [
            await backend.find_channel_input_escrow_status(
                negotiation.invite.session_id, intent.funding_outpoint
            )
            for _, intent in intents
        ]
        if any(status is not None and status.stage > LndEscrowStage.LOCKED for status in statuses):
            raise TakerChannelInputRuntimeError("frozen channel is already in a later escrow stage")
        for nick, intent in intents:
            self._save(negotiation)
            snapshot = await backend.freeze_channel_input(
                negotiation.invite.session_id, intent.channel_id
            )
            try:
                negotiation.install_frozen_channel(nick, snapshot)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise TakerChannelInputRuntimeError(
                    "recovered frozen channel data conflicts with intent"
                ) from exc
            self._save(negotiation)

    async def _require_locked_frozen_inputs(
        self, negotiation: TakerChannelInputNegotiation, backend: TakerChannelInputBackend
    ) -> None:
        for _, frozen in negotiation.frozen_inputs:
            status = await backend.find_channel_input_escrow_status(
                negotiation.invite.session_id, frozen.snapshot.funding_outpoint
            )
            if status is None or status.stage is not LndEscrowStage.LOCKED:
                raise TakerChannelInputRuntimeError(
                    "frozen channel escrow state conflicts with recovery"
                )

    async def _resume_cancellation(
        self, negotiation: TakerChannelInputNegotiation, backend: TakerChannelInputBackend
    ) -> None:
        if negotiation.stage is TakerChannelInputStage.CANCELED:
            return
        if negotiation.stage is not TakerChannelInputStage.CANCEL_INTENDED:
            raise TakerChannelInputRuntimeError("cancellation recovery lacks durable intent")
        escrows = tuple(
            EscrowOutpoint(txid=hello.funding_outpoint.txid, vout=hello.funding_outpoint.vout)
            for _, hello in negotiation.local_hellos
        )
        statuses = [
            await backend.find_channel_input_escrow_status(negotiation.invite.session_id, outpoint)
            for outpoint in escrows
        ]
        allowed = (
            {LndEscrowStage.LOCKED}
            if negotiation.transcript is None
            else {LndEscrowStage.LOCKED, LndEscrowStage.PARENT_PREPARED}
        )
        if any(status is not None and status.stage not in allowed for status in statuses):
            raise TakerChannelInputRuntimeError(
                "cancellation conflicts with signed or finalized escrow evidence"
            )
        for outpoint, status in zip(escrows, statuses, strict=True):
            if status is None:
                continue
            self._save(negotiation)
            if not await backend.cancel_channel_input_escrow(
                negotiation.invite.session_id, outpoint
            ):
                raise TakerChannelInputRuntimeError(
                    "backend did not resume canceled channel escrow"
                )
        if len(negotiation.cancel_acks) != len(negotiation.selected_nicks):
            return
        try:
            negotiation.record_local_resumed(
                tuple(
                    sorted(
                        (hello.funding_outpoint for _, hello in negotiation.local_hellos),
                        key=lambda outpoint: outpoint.key(),
                    )
                )
            )
        except (ChannelInputValidationError, TypeError, ValueError) as exc:
            raise TakerChannelInputRuntimeError(
                "cancellation recovery conflicts with durable state"
            ) from exc
        self._save(negotiation)

    def _save(self, negotiation: TakerChannelInputNegotiation) -> None:
        try:
            self._store.save(negotiation.snapshot())
        except TakerChannelInputStoreError as exc:
            raise TakerChannelInputRuntimeError(
                "could not persist channel-input recovery state"
            ) from exc
