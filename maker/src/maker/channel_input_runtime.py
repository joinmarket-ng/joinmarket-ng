"""Startup recovery for durable maker channel-input negotiations."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from jmcore.bitcoin import get_txid
from jmcore.channel_input import (
    MAX_CHANNELS,
    ChannelInputApprovalSetPayload,
    ChannelInputCancelPayload,
    ChannelInputFinalizePayload,
    ChannelInputInvitePayload,
    ChannelInputPayload,
    ChannelInputPayloadType,
    ChannelInputPreparePayload,
    ChannelInputProposalPayload,
    ChannelInputSignPayload,
    ChannelInputValidationError,
    Network,
    Outpoint,
)
from jmcore.channel_input_config import ChannelInputConfig
from jmswap.lnd import (
    ChannelInputCandidate,
    EscrowOutpoint,
    FrozenChannelSnapshot,
    LndEscrowSigningAttempt,
    LndEscrowStage,
    LndEscrowStatus,
    LndFinalizedEscrowParent,
    WitnessUtxo,
)

from maker.channel_input import (
    MakerChannelInputChannelState,
    MakerChannelInputNegotiation,
    MakerChannelInputStage,
    MakerFrozenChannelInput,
)
from maker.channel_input_store import MakerChannelInputStore, MakerChannelInputStoreError


class MakerChannelInputRuntimeError(Exception):
    """A persisted maker channel-input negotiation cannot safely resume."""


class MakerChannelInputBackend(Protocol):
    """The narrow ChannelEscrowRPC surface required by startup recovery."""

    async def find_channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus | None: ...

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool: ...

    async def freeze_channel_input(
        self, session_id: str, channel_id: str
    ) -> FrozenChannelSnapshot: ...

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo],
        channel_input_index: int,
    ) -> str: ...

    async def begin_channel_input_signing(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        *,
        restart_attempt: bool = False,
    ) -> LndEscrowSigningAttempt: ...

    async def recover_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent: ...

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes: ...

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent: ...


class MakerChannelInputRegistry:
    """Own restored negotiations and serialize their startup recovery."""

    def __init__(
        self,
        *,
        store: MakerChannelInputStore,
        config: ChannelInputConfig,
        network: Network,
    ) -> None:
        self._store = store
        self._config = config
        self._network = network
        self._negotiations: dict[tuple[str, int], MakerChannelInputNegotiation] = {}
        self._locks: dict[tuple[str, int], asyncio.Lock] = {}
        self._admission_lock = asyncio.Lock()
        self._load()

    @classmethod
    def load(
        cls,
        *,
        store: MakerChannelInputStore,
        config: ChannelInputConfig,
        network: Network,
    ) -> MakerChannelInputRegistry:
        """Restore a registry only when every persisted record is trustworthy."""

        return cls(store=store, config=config, network=network)

    @property
    def session_count(self) -> int:
        """Return the number of active restored session attempts."""

        return sum(
            negotiation.stage is not MakerChannelInputStage.CANCELED
            for negotiation in self._negotiations.values()
        )

    @property
    def recovery_outpoints(self) -> tuple[EscrowOutpoint, ...]:
        """Return all persisted freeze-intent outpoints in stable order."""

        outpoints = [
            intent.funding_outpoint
            for negotiation in self._negotiations.values()
            if negotiation.stage is not MakerChannelInputStage.CANCELED
            for intent in negotiation.freeze_intents
        ]
        return tuple(sorted(outpoints, key=lambda outpoint: (outpoint.txid, outpoint.vout)))

    def get(self, session_id: str, attempt: int) -> MakerChannelInputNegotiation | None:
        """Return one restored negotiation for later encrypted dispatch."""

        return self._negotiations.get((session_id, attempt))

    def lock_for(self, session_id: str, attempt: int) -> asyncio.Lock:
        """Return the lock that serializes one session attempt."""

        try:
            return self._locks[(session_id, attempt)]
        except KeyError as exc:
            raise MakerChannelInputRuntimeError("unknown channel-input session attempt") from exc

    async def reconcile_all(self, backend: MakerChannelInputBackend, local_node_key: str) -> None:
        """Reconcile each restored session against its exact LND escrow state."""

        for negotiation in self._negotiations.values():
            if negotiation.local_node_key != local_node_key:
                raise MakerChannelInputRuntimeError(
                    "persisted channel-input session belongs to another local node"
                )
        for key in sorted(self._negotiations):
            async with self._locks[key]:
                await self._reconcile_one(self._negotiations[key], backend)

    async def handle_payload(
        self,
        payload: ChannelInputPayloadType,
        *,
        coordinator_actor: str,
        local_actor: str,
        local_node_key: str,
        backend: MakerChannelInputBackend,
        candidates: Sequence[ChannelInputCandidate],
        settlement_script_for: Callable[[ChannelInputCandidate], str],
        current_time: int,
        current_height: int,
    ) -> tuple[ChannelInputPayload, ...]:
        """Apply one authenticated inbound payload and return durable local responses."""

        if isinstance(payload, ChannelInputInvitePayload):
            return await self._handle_invite(
                payload,
                coordinator_actor=coordinator_actor,
                local_actor=local_actor,
                local_node_key=local_node_key,
                backend=backend,
                candidates=candidates,
                settlement_script_for=settlement_script_for,
            )
        if isinstance(payload, ChannelInputCancelPayload):
            return await self._handle_cancel(
                payload,
                coordinator_actor=coordinator_actor,
                local_actor=local_actor,
                local_node_key=local_node_key,
                backend=backend,
            )
        if not isinstance(
            payload,
            (
                ChannelInputProposalPayload,
                ChannelInputApprovalSetPayload,
                ChannelInputPreparePayload,
                ChannelInputSignPayload,
                ChannelInputFinalizePayload,
            ),
        ):
            raise ChannelInputValidationError("unsupported inbound channel-input payload")

        key = (payload.session_id, payload.attempt)
        try:
            lock = self._locks[key]
            negotiation = self._negotiations[key]
        except KeyError as exc:
            raise MakerChannelInputRuntimeError("unknown channel-input session attempt") from exc
        async with lock:
            self._require_local_identity(negotiation, local_actor, local_node_key)
            if negotiation.stage in {
                MakerChannelInputStage.CANCEL_INTENDED,
                MakerChannelInputStage.CANCELED,
            }:
                raise MakerChannelInputRuntimeError(
                    "channel-input cancellation is terminal for non-cancel payloads"
                )
            if isinstance(payload, ChannelInputProposalPayload):
                approval = negotiation.accept_proposal(
                    coordinator_actor,
                    payload,
                    current_time=current_time,
                    current_height=current_height,
                )
                self._save(negotiation)
                return (approval,)
            if isinstance(payload, ChannelInputApprovalSetPayload):
                ack = negotiation.accept_approval_set(coordinator_actor, payload)
                self._save(negotiation)
                return (ack,)
            if isinstance(payload, ChannelInputPreparePayload):
                negotiation.accept_prepare(coordinator_actor, payload)
                self._save(negotiation)
                await self._recover_prepared_or_later(negotiation, backend)
                return tuple(state.prepared for state in negotiation.channel_states)
            if isinstance(payload, ChannelInputSignPayload):
                partial = negotiation.accept_sign(coordinator_actor, payload)
                self._save(negotiation)
                if partial is None:
                    partial_signature = await backend.sign_channel_input_parent(
                        negotiation.invite.session_id,
                        EscrowOutpoint(
                            txid=payload.funding_outpoint.txid,
                            vout=payload.funding_outpoint.vout,
                        ),
                        negotiation.signing_attempt_for(payload.funding_outpoint),
                        bytes.fromhex(payload.peer_prepared.public_nonce),
                    )
                    self._record_partial(negotiation, payload.funding_outpoint, partial_signature)
                    state = self._channel_state(negotiation, payload.funding_outpoint)
                    assert state is not None and state.partial is not None
                    partial = state.partial
                return (partial,)
            assert isinstance(payload, ChannelInputFinalizePayload)
            finalized = negotiation.accept_finalize(coordinator_actor, payload)
            self._save(negotiation)
            if finalized is None:
                result = await backend.finalize_channel_input_parent(
                    negotiation.invite.session_id,
                    EscrowOutpoint(
                        txid=payload.funding_outpoint.txid,
                        vout=payload.funding_outpoint.vout,
                    ),
                    negotiation.signing_attempt_for(payload.funding_outpoint),
                    bytes.fromhex(payload.peer_partial.partial_signature),
                )
                self._record_finalized(negotiation, payload.funding_outpoint, result)
                state = self._channel_state(negotiation, payload.funding_outpoint)
                assert state is not None and state.finalized is not None
                finalized = state.finalized
            return (finalized,)

    async def _handle_cancel(
        self,
        cancel: ChannelInputCancelPayload,
        *,
        coordinator_actor: str,
        local_actor: str,
        local_node_key: str,
        backend: MakerChannelInputBackend,
    ) -> tuple[ChannelInputPayload, ...]:
        """Serialize admission with a cancellation before touching escrow state."""

        key = (cancel.session_id, cancel.attempt)
        async with self._admission_lock:
            try:
                negotiation = self._negotiations[key]
                lock = self._locks[key]
            except KeyError as exc:
                raise MakerChannelInputRuntimeError(
                    "unknown channel-input session attempt"
                ) from exc
            async with lock:
                self._require_local_identity(negotiation, local_actor, local_node_key)
                if negotiation.stage is MakerChannelInputStage.CANCELED:
                    ack = negotiation.accept_cancel(coordinator_actor, cancel)
                    assert ack is not None
                    return (ack,)
                negotiation.validate_cancel(coordinator_actor, cancel)
                await self._preflight_cancellation(negotiation, backend)
                accepted = negotiation.accept_cancel(coordinator_actor, cancel)
                if accepted is not None:
                    return (accepted,)
                self._save(negotiation)
                return (await self._resume_cancellation(negotiation, backend),)

    async def _handle_invite(
        self,
        invite: ChannelInputInvitePayload,
        *,
        coordinator_actor: str,
        local_actor: str,
        local_node_key: str,
        backend: MakerChannelInputBackend,
        candidates: Sequence[ChannelInputCandidate],
        settlement_script_for: Callable[[ChannelInputCandidate], str],
    ) -> tuple[ChannelInputPayload, ...]:
        key = (invite.session_id, invite.attempt)
        async with self._admission_lock:
            negotiation = self._negotiations.get(key)
            if negotiation is not None:
                self._require_matching_invite(
                    negotiation,
                    invite,
                    coordinator_actor=coordinator_actor,
                    local_actor=local_actor,
                    local_node_key=local_node_key,
                )
                async with self._locks[key]:
                    if negotiation.stage is MakerChannelInputStage.CANCELED:
                        raise MakerChannelInputRuntimeError(
                            "canceled channel-input invitation is terminal"
                        )
                    if negotiation.stage is MakerChannelInputStage.FREEZE_INTENDED:
                        await self._recover_freezes(negotiation, backend)
                    elif negotiation.stage is MakerChannelInputStage.INVITED:
                        raise MakerChannelInputRuntimeError(
                            "channel-input invitation has no durable freeze intent"
                        )
                    return tuple(negotiation.hellos)

            if self.session_count >= self._store.max_active_sessions:
                raise MakerChannelInputRuntimeError("maximum active channel-input sessions reached")
            unavailable = {
                (intent.funding_outpoint.txid, intent.funding_outpoint.vout)
                for existing in self._negotiations.values()
                if existing.stage is not MakerChannelInputStage.CANCELED
                for intent in existing.freeze_intents
            }
            selected = tuple(
                candidate
                for candidate in sorted(
                    candidates,
                    key=lambda candidate: (
                        candidate.funding_outpoint.txid,
                        candidate.funding_outpoint.vout,
                    ),
                )
                if (
                    candidate.funding_outpoint.txid,
                    candidate.funding_outpoint.vout,
                )
                not in unavailable
            )[:MAX_CHANNELS]
            if not selected:
                raise MakerChannelInputRuntimeError("no channel-input candidates are available")
            negotiation = MakerChannelInputNegotiation.from_invite(
                local_actor=local_actor,
                coordinator_actor=coordinator_actor,
                invite=invite,
                config=self._config,
                network=self._network,
                local_node_key=local_node_key,
            )
            settlement_scripts = {
                f"{candidate.funding_outpoint.txid}:{candidate.funding_outpoint.vout}": (
                    settlement_script_for(candidate)
                )
                for candidate in selected
            }
            negotiation.plan_freeze(selected, settlement_scripts=settlement_scripts)
            self._save(negotiation)
            self._negotiations[key] = negotiation
            self._locks[key] = asyncio.Lock()
            async with self._locks[key]:
                await self._recover_freezes(negotiation, backend)
            return tuple(negotiation.hellos)

    @staticmethod
    def _require_matching_invite(
        negotiation: MakerChannelInputNegotiation,
        invite: ChannelInputInvitePayload,
        *,
        coordinator_actor: str,
        local_actor: str,
        local_node_key: str,
    ) -> None:
        if coordinator_actor != negotiation.coordinator_actor:
            raise ChannelInputValidationError("channel-input invite came from a different actor")
        if invite != negotiation.invite:
            raise ChannelInputValidationError(
                "channel-input invite conflicts with prior invitation"
            )
        if local_actor != negotiation.local_actor or local_node_key != negotiation.local_node_key:
            raise MakerChannelInputRuntimeError(
                "channel-input invitation differs from local identity"
            )

    @staticmethod
    def _require_local_identity(
        negotiation: MakerChannelInputNegotiation, local_actor: str, local_node_key: str
    ) -> None:
        if local_actor != negotiation.local_actor or local_node_key != negotiation.local_node_key:
            raise MakerChannelInputRuntimeError(
                "channel-input session belongs to another local identity"
            )

    def _load(self) -> None:
        freeze_outpoints: set[tuple[str, int]] = set()
        try:
            report = self._store.load_all()
        except MakerChannelInputStoreError as exc:
            raise MakerChannelInputRuntimeError(
                "could not load channel-input recovery state"
            ) from exc
        if report.corruptions:
            raise MakerChannelInputRuntimeError("channel-input recovery state is corrupt")
        for snapshot in report.records:
            if snapshot.stage is MakerChannelInputStage.CANCELED and snapshot.invite.expiry <= int(
                time.time()
            ):
                try:
                    self._store.delete(snapshot)
                except MakerChannelInputStoreError as exc:
                    raise MakerChannelInputRuntimeError(
                        "could not prune expired channel-input tombstone"
                    ) from exc
                continue
            key = (snapshot.invite.session_id, snapshot.invite.attempt)
            if key in self._negotiations:
                raise MakerChannelInputRuntimeError("duplicate channel-input session attempt")
            if snapshot.config != self._config:
                raise MakerChannelInputRuntimeError(
                    "persisted channel-input configuration differs from runtime configuration"
                )
            if snapshot.network != self._network:
                raise MakerChannelInputRuntimeError(
                    "persisted channel-input network differs from runtime network"
                )
            try:
                negotiation = MakerChannelInputNegotiation.from_snapshot(snapshot)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise MakerChannelInputRuntimeError(
                    "channel-input recovery state is invalid"
                ) from exc
            if negotiation.stage is MakerChannelInputStage.CANCELED:
                self._negotiations[key] = negotiation
                self._locks[key] = asyncio.Lock()
                continue
            for intent in negotiation.freeze_intents:
                outpoint = (intent.funding_outpoint.txid, intent.funding_outpoint.vout)
                if outpoint in freeze_outpoints:
                    raise MakerChannelInputRuntimeError(
                        "channel-input funding outpoint belongs to multiple session attempts"
                    )
                freeze_outpoints.add(outpoint)
            self._negotiations[key] = negotiation
            self._locks[key] = asyncio.Lock()

    async def _reconcile_one(
        self, negotiation: MakerChannelInputNegotiation, backend: MakerChannelInputBackend
    ) -> None:
        stage = negotiation.stage
        if stage is MakerChannelInputStage.CANCELED:
            return
        if stage is MakerChannelInputStage.CANCEL_INTENDED:
            await self._resume_cancellation(negotiation, backend)
            return
        if stage is MakerChannelInputStage.INVITED:
            return
        if stage is MakerChannelInputStage.FREEZE_INTENDED:
            await self._recover_freezes(negotiation, backend)
            return
        if stage in {
            MakerChannelInputStage.HELLOS_READY,
            MakerChannelInputStage.PROPOSAL_APPROVED,
            MakerChannelInputStage.APPROVAL_SET_ACKED,
        }:
            await self._require_locked_frozen_inputs(negotiation, backend)
            return
        await self._recover_prepared_or_later(negotiation, backend)

    async def _recover_freezes(
        self, negotiation: MakerChannelInputNegotiation, backend: MakerChannelInputBackend
    ) -> None:
        frozen: list[MakerFrozenChannelInput] = []
        session_id = negotiation.invite.session_id
        for intent in negotiation.freeze_intents:
            status = await backend.find_channel_input_escrow_status(
                session_id, intent.funding_outpoint
            )
            if status is not None and status.stage is not LndEscrowStage.LOCKED:
                raise MakerChannelInputRuntimeError(
                    "frozen channel is already in a later escrow stage"
                )
        for intent in negotiation.freeze_intents:
            self._save(negotiation)
            snapshot = await backend.freeze_channel_input(session_id, intent.channel_id)
            frozen.append(
                MakerFrozenChannelInput(
                    snapshot=snapshot,
                    settlement_script_pubkey=intent.settlement_script_pubkey,
                )
            )
        try:
            negotiation.install_frozen_inputs(frozen)
        except (ChannelInputValidationError, TypeError, ValueError) as exc:
            raise MakerChannelInputRuntimeError(
                "recovered frozen channel data conflicts with intent"
            ) from exc
        self._save(negotiation)

    async def _resume_cancellation(
        self,
        negotiation: MakerChannelInputNegotiation,
        backend: MakerChannelInputBackend,
    ) -> ChannelInputPayload:
        """Query every escrow before resuming any of them after a durable intent."""

        if negotiation.stage is MakerChannelInputStage.CANCELED:
            assert negotiation.cancel_ack is not None
            return negotiation.cancel_ack
        if negotiation.stage is not MakerChannelInputStage.CANCEL_INTENDED:
            raise MakerChannelInputRuntimeError("cancellation recovery lacks durable intent")
        statuses = await self._preflight_cancellation(negotiation, backend)
        for outpoint, escrow_outpoint, status in statuses:
            if status is None:
                continue
            self._save(negotiation)
            resumed = await backend.cancel_channel_input_escrow(
                negotiation.invite.session_id, escrow_outpoint
            )
            if not resumed:
                raise MakerChannelInputRuntimeError(
                    "backend did not resume canceled channel escrow"
                )
        try:
            ack = negotiation.record_canceled(tuple(outpoint for outpoint, _, _ in statuses))
        except (ChannelInputValidationError, TypeError, ValueError) as exc:
            raise MakerChannelInputRuntimeError(
                "cancellation recovery conflicts with durable state"
            ) from exc
        self._save(negotiation)
        return ack

    async def _preflight_cancellation(
        self, negotiation: MakerChannelInputNegotiation, backend: MakerChannelInputBackend
    ) -> list[tuple[Outpoint, EscrowOutpoint, LndEscrowStatus | None]]:
        """Reject every partial or terminal escrow before any cancel mutation."""

        if any(
            state.partial is not None
            or state.finalize_request is not None
            or state.finalized is not None
            for state in negotiation.channel_states
        ):
            raise MakerChannelInputRuntimeError(
                "cancellation conflicts with durable partial evidence"
            )
        statuses: list[tuple[Outpoint, EscrowOutpoint, LndEscrowStatus | None]] = []
        has_later_escrow_evidence = False
        for outpoint in negotiation._expected_local_outpoints():
            escrow_outpoint = EscrowOutpoint(txid=outpoint.txid, vout=outpoint.vout)
            status = await backend.find_channel_input_escrow_status(
                negotiation.invite.session_id, escrow_outpoint
            )
            allowed_stages = (
                {LndEscrowStage.LOCKED}
                if negotiation.proposal is None
                else {LndEscrowStage.LOCKED, LndEscrowStage.PARENT_PREPARED}
            )
            if status is not None and status.stage not in allowed_stages:
                has_later_escrow_evidence = True
            statuses.append((outpoint, escrow_outpoint, status))
        if has_later_escrow_evidence:
            raise MakerChannelInputRuntimeError(
                "cancellation conflicts with signed or finalized escrow evidence"
            )
        return statuses

    async def _require_locked_frozen_inputs(
        self, negotiation: MakerChannelInputNegotiation, backend: MakerChannelInputBackend
    ) -> None:
        session_id = negotiation.invite.session_id
        for frozen in negotiation.frozen_inputs:
            status = await backend.find_channel_input_escrow_status(
                session_id, frozen.snapshot.funding_outpoint
            )
            if status is None or status.stage is not LndEscrowStage.LOCKED:
                raise MakerChannelInputRuntimeError(
                    "frozen channel escrow state conflicts with recovery"
                )

    async def _recover_prepared_or_later(
        self, negotiation: MakerChannelInputNegotiation, backend: MakerChannelInputBackend
    ) -> None:
        assert negotiation.proposal is not None
        transcript = negotiation.proposal.transcript
        parent_txid = get_txid(transcript.unsigned_parent_tx)
        prevouts = tuple(
            WitnessUtxo(value_sat=item.value_sat, script_pubkey=bytes.fromhex(item.script_pubkey))
            for item in transcript.input_prevouts
        )
        descriptors = {
            descriptor.funding_outpoint: descriptor for descriptor in transcript.channels
        }
        recovery: list[tuple[Outpoint, EscrowOutpoint, int, LndEscrowStatus]] = []
        for terms in negotiation.proposal.recipient_terms:
            outpoint = terms.funding_outpoint
            escrow_outpoint = EscrowOutpoint(txid=outpoint.txid, vout=outpoint.vout)
            descriptor = descriptors.get(outpoint)
            if descriptor is None:
                raise MakerChannelInputRuntimeError(
                    "local recipient is absent from transcript descriptors"
                )
            status = await backend.find_channel_input_escrow_status(
                negotiation.invite.session_id, escrow_outpoint
            )
            if status is None:
                raise MakerChannelInputRuntimeError("prepared channel has no exact session escrow")
            self._preflight_prepared_status(negotiation, outpoint, status, parent_txid)
            recovery.append((outpoint, escrow_outpoint, descriptor.input_index, status))

        for outpoint, escrow_outpoint, input_index, status in recovery:
            state = self._channel_state(negotiation, outpoint)
            if status.stage is LndEscrowStage.LOCKED:
                assert state is None
                self._save(negotiation)
                actual_txid = await backend.prepare_channel_input_parent(
                    negotiation.invite.session_id,
                    escrow_outpoint,
                    transcript.unsigned_parent_tx,
                    prevouts,
                    input_index,
                )
                if actual_txid != parent_txid:
                    raise MakerChannelInputRuntimeError(
                        "backend prepared an unexpected parent transaction"
                    )
                self._save(negotiation)
                attempt = await backend.begin_channel_input_signing(
                    negotiation.invite.session_id, escrow_outpoint
                )
                self._record_prepared(negotiation, outpoint, parent_txid, attempt)
                continue
            if status.parent_txid != parent_txid:
                raise MakerChannelInputRuntimeError(
                    "backend parent transaction differs from transcript"
                )
            if status.stage is LndEscrowStage.PARENT_PREPARED:
                await self._recover_parent_prepared(
                    negotiation, backend, outpoint, escrow_outpoint, status
                )
            elif status.stage is LndEscrowStage.PARENT_SIGNED:
                await self._recover_parent_signed(
                    negotiation, backend, outpoint, escrow_outpoint, status
                )
            elif status.stage is LndEscrowStage.FINALIZED:
                self._recover_finalized(negotiation, outpoint, status)
            else:
                raise MakerChannelInputRuntimeError("unknown escrow recovery stage")

    def _preflight_prepared_status(
        self,
        negotiation: MakerChannelInputNegotiation,
        outpoint: Outpoint,
        status: LndEscrowStatus,
        parent_txid: str,
    ) -> None:
        state = self._channel_state(negotiation, outpoint)
        if status.stage is LndEscrowStage.LOCKED:
            if state is not None:
                raise MakerChannelInputRuntimeError("locked channel has durable signing state")
            return
        if status.parent_txid != parent_txid:
            raise MakerChannelInputRuntimeError(
                "backend parent transaction differs from transcript"
            )
        if status.stage is LndEscrowStage.PARENT_PREPARED:
            if state is None:
                return
            if any(
                value is not None
                for value in (state.partial, state.finalize_request, state.finalized)
            ):
                raise MakerChannelInputRuntimeError(
                    "backend escrow stage precedes durable signing evidence"
                )
            if status.active_attempt is not None:
                attempt_matches = self._attempt_matches_state(
                    status.active_attempt, state.attempt_id, state.prepared.public_nonce
                )
                if not attempt_matches and not state.restart_intended:
                    raise MakerChannelInputRuntimeError(
                        "backend signing attempt differs from durable state"
                    )
            if status.active_attempt is None and state.sign_request is not None:
                raise MakerChannelInputRuntimeError("backend lost an authorized signing attempt")
            return
        if status.stage in (LndEscrowStage.PARENT_SIGNED, LndEscrowStage.FINALIZED):
            state = self._require_signed_state(negotiation, outpoint, status)
            if status.stage is LndEscrowStage.FINALIZED and (
                state.finalize_request is None or status.finalized_parent is None
            ):
                raise MakerChannelInputRuntimeError(
                    "finalized escrow lacks durable finalization evidence"
                )
            return
        raise MakerChannelInputRuntimeError("unknown escrow recovery stage")

    async def _recover_parent_prepared(
        self,
        negotiation: MakerChannelInputNegotiation,
        backend: MakerChannelInputBackend,
        outpoint: Outpoint,
        escrow_outpoint: EscrowOutpoint,
        status: LndEscrowStatus,
    ) -> None:
        assert status.parent_txid is not None
        state = self._channel_state(negotiation, outpoint)
        if state is None:
            self._save(negotiation)
            attempt = await backend.begin_channel_input_signing(
                negotiation.invite.session_id, escrow_outpoint
            )
            self._record_prepared(negotiation, outpoint, status.parent_txid, attempt)
            return
        if status.active_attempt is not None:
            if self._attempt_matches_state(
                status.active_attempt, state.attempt_id, state.prepared.public_nonce
            ):
                if not state.restart_intended:
                    return
            elif state.restart_intended:
                self._rotate_prepared(
                    negotiation, outpoint, status.parent_txid, status.active_attempt
                )
                return
            else:
                raise MakerChannelInputRuntimeError(
                    "backend signing attempt differs from durable state"
                )
        if any(
            value is not None
            for value in (
                state.sign_request,
                state.partial,
                state.finalize_request,
                state.finalized,
            )
        ):
            raise MakerChannelInputRuntimeError("backend lost an authorized signing attempt")
        if not state.restart_intended:
            try:
                negotiation.plan_unsigned_prepared_rotation(outpoint)
            except (ChannelInputValidationError, TypeError, ValueError) as exc:
                raise MakerChannelInputRuntimeError(
                    "backend signing attempt conflicts with recovery state"
                ) from exc
            self._save(negotiation)
        attempt = await backend.begin_channel_input_signing(
            negotiation.invite.session_id, escrow_outpoint, restart_attempt=True
        )
        self._rotate_prepared(negotiation, outpoint, status.parent_txid, attempt)

    def _rotate_prepared(
        self,
        negotiation: MakerChannelInputNegotiation,
        outpoint: Outpoint,
        parent_txid: str,
        attempt: LndEscrowSigningAttempt,
    ) -> None:
        try:
            negotiation.rotate_unsigned_prepared(outpoint, parent_txid, attempt)
        except (ChannelInputValidationError, TypeError, ValueError) as exc:
            raise MakerChannelInputRuntimeError(
                "backend signing attempt conflicts with recovery state"
            ) from exc
        self._save(negotiation)

    async def _recover_parent_signed(
        self,
        negotiation: MakerChannelInputNegotiation,
        backend: MakerChannelInputBackend,
        outpoint: Outpoint,
        escrow_outpoint: EscrowOutpoint,
        status: LndEscrowStatus,
    ) -> None:
        state = self._require_signed_state(negotiation, outpoint, status)
        if state.partial is None:
            assert status.durable_local_partial_signature is not None
            self._record_partial(negotiation, outpoint, status.durable_local_partial_signature)
            recovered_state = self._channel_state(negotiation, outpoint)
            assert recovered_state is not None
            state = recovered_state
        if state.finalize_request is not None and state.finalized is None:
            self._save(negotiation)
            result = await backend.recover_channel_input_parent(
                negotiation.invite.session_id,
                escrow_outpoint,
                bytes.fromhex(state.finalize_request.peer_partial.partial_signature),
            )
            self._record_finalized(negotiation, outpoint, result)

    def _recover_finalized(
        self, negotiation: MakerChannelInputNegotiation, outpoint: Outpoint, status: LndEscrowStatus
    ) -> None:
        state = self._require_signed_state(negotiation, outpoint, status)
        if state.finalize_request is None or status.finalized_parent is None:
            raise MakerChannelInputRuntimeError(
                "finalized escrow lacks durable finalization evidence"
            )
        if state.partial is None:
            assert status.durable_local_partial_signature is not None
            self._record_partial(negotiation, outpoint, status.durable_local_partial_signature)
        self._record_finalized(negotiation, outpoint, status.finalized_parent)

    @staticmethod
    def _channel_state(
        negotiation: MakerChannelInputNegotiation, outpoint: Outpoint
    ) -> MakerChannelInputChannelState | None:
        return next(
            (state for state in negotiation.channel_states if state.funding_outpoint == outpoint),
            None,
        )

    def _require_signed_state(
        self, negotiation: MakerChannelInputNegotiation, outpoint: Outpoint, status: LndEscrowStatus
    ) -> MakerChannelInputChannelState:
        state = self._channel_state(negotiation, outpoint)
        if state is None or state.sign_request is None:
            raise MakerChannelInputRuntimeError("signed escrow lacks durable signing authorization")
        if state.finalized is not None and status.stage is not LndEscrowStage.FINALIZED:
            raise MakerChannelInputRuntimeError(
                "backend escrow stage precedes durable final evidence"
            )
        if status.active_attempt is not None and not self._attempt_matches_state(
            status.active_attempt, state.attempt_id, state.prepared.public_nonce
        ):
            raise MakerChannelInputRuntimeError(
                "backend signing attempt differs from durable state"
            )
        if status.durable_local_public_nonce != bytes.fromhex(
            state.prepared.public_nonce
        ) or status.durable_remote_public_nonce != bytes.fromhex(
            state.sign_request.peer_prepared.public_nonce
        ):
            raise MakerChannelInputRuntimeError(
                "backend signing nonces differ from durable authorization"
            )
        if state.partial is not None and status.durable_local_partial_signature != bytes.fromhex(
            state.partial.partial_signature
        ):
            raise MakerChannelInputRuntimeError(
                "backend partial signature differs from durable evidence"
            )
        return state

    @staticmethod
    def _attempt_matches_state(
        attempt: LndEscrowSigningAttempt, attempt_id: str, public_nonce: str
    ) -> bool:
        return attempt.attempt_id.hex() == attempt_id and attempt.public_nonce.hex() == public_nonce

    def _record_prepared(
        self,
        negotiation: MakerChannelInputNegotiation,
        outpoint: Outpoint,
        parent_txid: str,
        attempt: LndEscrowSigningAttempt,
    ) -> None:
        try:
            negotiation.record_prepared(outpoint, parent_txid, attempt)
        except (ChannelInputValidationError, TypeError, ValueError) as exc:
            raise MakerChannelInputRuntimeError(
                "backend signing attempt conflicts with recovery state"
            ) from exc
        self._save(negotiation)

    def _record_partial(
        self, negotiation: MakerChannelInputNegotiation, outpoint: Outpoint, partial: bytes
    ) -> None:
        try:
            negotiation.record_partial(outpoint, partial)
        except (ChannelInputValidationError, TypeError, ValueError) as exc:
            raise MakerChannelInputRuntimeError(
                "backend partial signature conflicts with recovery"
            ) from exc
        self._save(negotiation)

    def _record_finalized(
        self,
        negotiation: MakerChannelInputNegotiation,
        outpoint: Outpoint,
        result: LndFinalizedEscrowParent,
    ) -> None:
        try:
            negotiation.record_finalized(outpoint, result)
        except (ChannelInputValidationError, TypeError, ValueError) as exc:
            raise MakerChannelInputRuntimeError(
                "backend finalization conflicts with recovery"
            ) from exc
        self._save(negotiation)

    def _save(self, negotiation: MakerChannelInputNegotiation) -> None:
        try:
            self._store.save(negotiation.snapshot())
        except MakerChannelInputStoreError as exc:
            raise MakerChannelInputRuntimeError(
                "could not persist channel-input recovery state"
            ) from exc
