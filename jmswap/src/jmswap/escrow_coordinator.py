"""Durable lifecycle orchestration for channel-input escrow sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from jmswap.channel_escrow import (
    ChainObservationRequest,
    ChannelInputEscrowBackend,
    ChannelInputEscrowRecord,
    ChannelInputEscrowStore,
    ChannelSnapshot,
    EscrowChainObservation,
    EscrowLifecycleState,
    FinalizedEscrowParent,
    FreezeChannelRequest,
    ParentAuthorizationRequest,
    ParentBroadcastRequest,
    PaymentObservationRequest,
    PreparedChannelInputEscrow,
    PrepareTimeoutRequest,
    RecoveryRequest,
    SinglePartPaymentRequest,
    SuccessSpendRequest,
    require_channel_input_escrow_backend,
)
from jmswap.errors import ChannelInputEscrowError
from jmswap.lightning import PaymentStatus


class ChannelInputEscrowCoordinator:
    """Coordinate durable escrow artifacts with an idempotent endpoint backend."""

    def __init__(self, backend: object, store: ChannelInputEscrowStore) -> None:
        self._backend: ChannelInputEscrowBackend = require_channel_input_escrow_backend(backend)
        self._store = store
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    def _load_required(self, session_id: str) -> ChannelInputEscrowRecord:
        record = self._store.load(session_id)
        if record is None:
            raise ChannelInputEscrowError(f"unknown channel-input escrow session: {session_id}")
        return record.model_copy(deep=True)

    def _save(self, record: ChannelInputEscrowRecord, expected_revision: int) -> None:
        self._store.save(record, expected_revision=expected_revision)

    def find_parent(self, parent_txid: str) -> ChannelInputEscrowRecord | None:
        """Find the unique durable session claiming an exact parent transaction ID."""
        try:
            raw_txid = bytes.fromhex(parent_txid)
        except ValueError:
            raise ChannelInputEscrowError("invalid escrow parent transaction ID") from None
        if len(raw_txid) != 32 or parent_txid != parent_txid.lower():
            raise ChannelInputEscrowError("invalid escrow parent transaction ID")

        matches: list[ChannelInputEscrowRecord] = []
        for record in self._store.list():
            evidence = (
                record.prepared.parent_evidence
                if record.prepared is not None
                else record.negotiated_parent_evidence
            )
            if evidence is not None and evidence.parent_txid == parent_txid:
                matches.append(record)
        if len(matches) > 1:
            raise ChannelInputEscrowError("multiple escrow sessions claim the same parent")
        return matches[0].model_copy(deep=True) if matches else None

    @staticmethod
    def _prepared_required(record: ChannelInputEscrowRecord) -> PreparedChannelInputEscrow:
        if record.prepared is None:
            raise ChannelInputEscrowError("escrow session lacks a prepared timeout artifact")
        return record.prepared

    async def freeze_channel(self, request: FreezeChannelRequest) -> ChannelSnapshot:
        """Freeze and quiesce a channel through the validated backend."""
        snapshot = await self._backend.freeze_quiesce_channel(request)
        if snapshot.session_id != request.session_id or snapshot.channel_id != request.channel_id:
            raise ChannelInputEscrowError("backend froze a different escrow session or channel")
        return snapshot

    async def prepare_timeout(self, request: PrepareTimeoutRequest) -> ChannelInputEscrowRecord:
        """Persist negotiated terms, then obtain and persist the exact timeout artifact."""
        session_id = request.terms.snapshot.session_id
        async with self._lock_for(session_id):
            existing = self._store.load(session_id)
            if existing is None:
                record = ChannelInputEscrowRecord(
                    terms=request.terms,
                    negotiated_parent_evidence=request.parent_evidence,
                )
                self._store.save(record)
            else:
                record = existing.model_copy(deep=True)
                if record.terms != request.terms:
                    raise ChannelInputEscrowError(
                        "prepared terms conflict with durable escrow terms"
                    )
                if (
                    record.negotiated_parent_evidence is not None
                    and record.negotiated_parent_evidence != request.parent_evidence
                ):
                    raise ChannelInputEscrowError(
                        "negotiated parent evidence conflicts with durable escrow"
                    )
                if (
                    record.prepared is not None
                    and record.prepared.parent_evidence != request.parent_evidence
                ):
                    raise ChannelInputEscrowError(
                        "prepared parent evidence conflicts with durable escrow"
                    )
                if (
                    record.prepared is not None
                    and record.state is not EscrowLifecycleState.NEGOTIATED
                ):
                    return record
                if record.state is not EscrowLifecycleState.NEGOTIATED:
                    raise ChannelInputEscrowError(
                        "escrow state lacks its required prepared artifact"
                    )
                if record.negotiated_parent_evidence is None:
                    expected_revision = record.revision
                    record.bind_negotiated_parent(request.parent_evidence)
                    self._save(record, expected_revision)

            prepared = await self._backend.prepare_signed_timeout(request)
            if (
                prepared.terms != request.terms
                or prepared.parent_evidence != request.parent_evidence
            ):
                raise ChannelInputEscrowError(
                    "backend prepared timeout does not match negotiated parent"
                )
            expected_revision = record.revision
            record.attach_prepared(prepared)
            record.advance(EscrowLifecycleState.TIMEOUT_SIGNED)
            self._save(record, expected_revision)
            return record

    async def authorize_parent(self, session_id: str) -> ChannelInputEscrowRecord:
        """Authorize the exact prepared parent after timeout evidence is durable."""
        async with self._lock_for(session_id):
            record = self._load_required(session_id)
            if record.state is not EscrowLifecycleState.TIMEOUT_SIGNED:
                if record.authorization is None:
                    raise ChannelInputEscrowError("escrow is not ready for parent authorization")
                return record
            prepared = self._prepared_required(record)
            authorization = await self._backend.authorize_parent(
                ParentAuthorizationRequest(prepared=prepared)
            )
            expected_revision = record.revision
            record.attach_authorization(authorization)
            record.advance(EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED)
            self._save(record, expected_revision)
            return record

    async def finalize_parent(
        self, session_id: str, finalized_parent: FinalizedEscrowParent
    ) -> ChannelInputEscrowRecord:
        """Durably attach exact all-input signing evidence before parent broadcast."""
        async with self._lock_for(session_id):
            record = self._load_required(session_id)
            if record.state is EscrowLifecycleState.PARENT_SIGNED:
                if record.finalized_parent != finalized_parent:
                    raise ChannelInputEscrowError(
                        "finalized parent conflicts with durable evidence"
                    )
                return record
            if record.state is not EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED:
                raise ChannelInputEscrowError("escrow is not ready for parent finalization")
            expected_revision = record.revision
            record.attach_finalized_parent(finalized_parent)
            record.advance(EscrowLifecycleState.PARENT_SIGNED)
            self._save(record, expected_revision)
            return record

    async def broadcast_parent(self, session_id: str) -> ChannelInputEscrowRecord:
        """Broadcast the exact authorized parent and retain its durable evidence."""
        async with self._lock_for(session_id):
            record = self._load_required(session_id)
            if record.state is not EscrowLifecycleState.PARENT_SIGNED:
                if record.parent_broadcast is None:
                    raise ChannelInputEscrowError("escrow is not ready for parent broadcast")
                return record
            prepared = self._prepared_required(record)
            if record.authorization is None:
                raise ChannelInputEscrowError("parent_signed escrow lacks authorization evidence")
            if record.finalized_parent is None:
                raise ChannelInputEscrowError(
                    "parent_signed escrow lacks finalized parent evidence"
                )
            broadcast = await self._backend.broadcast_parent(
                ParentBroadcastRequest(
                    prepared=prepared,
                    authorization=record.authorization,
                    finalized_parent=record.finalized_parent,
                )
            )
            expected_revision = record.revision
            record.attach_parent_broadcast(broadcast)
            record.advance(EscrowLifecycleState.PARENT_PENDING)
            self._save(record, expected_revision)
            return record

    async def _observe_chain_locked(
        self, record: ChannelInputEscrowRecord
    ) -> ChannelInputEscrowRecord:
        prepared = self._prepared_required(record)
        observation = await self._backend.observe_escrow_chain(
            ChainObservationRequest(
                session_id=record.session_id, parent_txid=prepared.parent_evidence.parent_txid
            )
        )
        record = await self._ensure_timeout_evidence_locked(record, observation)
        expected_revision = record.revision
        record.attach_chain_observation(observation)
        self._advance_from_observation(record, observation)
        self._save(record, expected_revision)
        return record

    async def _ensure_timeout_evidence_locked(
        self, record: ChannelInputEscrowRecord, observation: EscrowChainObservation
    ) -> ChannelInputEscrowRecord:
        if (
            observation.timeout_confirmations
            and observation.timeout_confirmations >= 1
            and record.payment_result is not None
            and record.payment_result.status is PaymentStatus.SUCCEEDED
            and record.timeout_broadcast is None
        ):
            timeout = await self._backend.broadcast_timeout_recovery(RecoveryRequest(record=record))
            expected_revision = record.revision
            record.attach_timeout_broadcast(timeout)
            self._save(record, expected_revision)
        return record

    @staticmethod
    def _advance_from_observation(
        record: ChannelInputEscrowRecord, observation: EscrowChainObservation
    ) -> None:
        if observation.conflict_txid is not None:
            payment_pending = (
                record.payment_result is not None
                and record.payment_result.status is PaymentStatus.PENDING
            )
            if payment_pending:
                if record.state is not EscrowLifecycleState.PARENT_REORGED:
                    record.advance(EscrowLifecycleState.PARENT_REORGED)
                return
            target = (
                EscrowLifecycleState.CROSS_LAYER_LOSS
                if record.payment_result is not None
                and record.payment_result.status is PaymentStatus.SUCCEEDED
                else EscrowLifecycleState.COMMITMENT_CONFLICT
            )
            if record.state in {
                EscrowLifecycleState.CHANNEL_SIGNATURE_RELEASED,
                EscrowLifecycleState.PARENT_SIGNED,
                EscrowLifecycleState.PARENT_PENDING,
            }:
                record.advance(EscrowLifecycleState.COMMITMENT_CONFLICT)
            elif record.state is EscrowLifecycleState.PARENT_REORGED:
                record.advance(target)
            elif record.state not in {
                EscrowLifecycleState.COMMITMENT_CONFLICT,
                EscrowLifecycleState.CROSS_LAYER_LOSS,
            }:
                record.advance(EscrowLifecycleState.PARENT_REORGED)
                record.advance(target)
            return

        if (
            observation.in_best_chain
            and observation.parent_confirmations < record.terms.parent_confirmation_depth
        ):
            if record.state in {
                EscrowLifecycleState.PARENT_CONFIRMED,
                EscrowLifecycleState.PAYMENT_IN_FLIGHT,
                EscrowLifecycleState.SUCCESS_SPEND_PENDING,
                EscrowLifecycleState.TIMEOUT_PENDING,
                EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
                EscrowLifecycleState.TIMEOUT_CONFIRMED,
                EscrowLifecycleState.PAID_TIMEOUT_LOSS,
            }:
                record.advance(EscrowLifecycleState.PARENT_REORGED)
            return

        if not observation.in_best_chain:
            if record.state in {
                EscrowLifecycleState.PARENT_CONFIRMED,
                EscrowLifecycleState.PAYMENT_IN_FLIGHT,
                EscrowLifecycleState.SUCCESS_SPEND_PENDING,
                EscrowLifecycleState.TIMEOUT_PENDING,
                EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
                EscrowLifecycleState.TIMEOUT_CONFIRMED,
                EscrowLifecycleState.PAID_TIMEOUT_LOSS,
            }:
                record.advance(EscrowLifecycleState.PARENT_REORGED)
            return

        if (
            record.state is EscrowLifecycleState.PARENT_PENDING
            and observation.parent_confirmations >= record.terms.parent_confirmation_depth
        ):
            record.advance(EscrowLifecycleState.PARENT_CONFIRMED)
        if (
            record.state is EscrowLifecycleState.SUCCESS_SPEND_PENDING
            and (observation.timeout_confirmations or 0) >= 1
        ):
            record.advance(EscrowLifecycleState.PAID_TIMEOUT_LOSS)
        elif (
            record.state is EscrowLifecycleState.SUCCESS_SPEND_PENDING
            and (observation.success_confirmations or 0) >= record.terms.success_confirmation_depth
        ):
            record.advance(EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED)
        elif (
            record.state is EscrowLifecycleState.TIMEOUT_PENDING
            and (observation.timeout_confirmations or 0) >= 1
        ):
            record.advance(EscrowLifecycleState.TIMEOUT_CONFIRMED)

    async def observe_chain(self, session_id: str) -> ChannelInputEscrowRecord:
        """Persist a fresh chain observation and its guarded lifecycle transition."""
        async with self._lock_for(session_id):
            return await self._observe_chain_locked(self._load_required(session_id))

    async def _send_payment_locked(
        self, record: ChannelInputEscrowRecord
    ) -> ChannelInputEscrowRecord:
        if record.payment_request is None:
            raise ChannelInputEscrowError("escrow lacks a durable payment request")
        result = await self._backend.send_single_part_payment(record.payment_request)
        expected_revision = record.revision
        record.attach_payment_result(result)
        record.advance(EscrowLifecycleState.PAYMENT_IN_FLIGHT)
        self._save(record, expected_revision)
        if result.status is PaymentStatus.SUCCEEDED:
            return await self._ensure_success_spend_locked(record)
        return record

    async def start_payment(
        self, session_id: str, excluded_channel_ids: Sequence[str], requested_at_unix: int
    ) -> ChannelInputEscrowRecord:
        """Persist a confirmation-gated payment request before submitting it once."""
        async with self._lock_for(session_id):
            record = self._load_required(session_id)
            if record.state is EscrowLifecycleState.PAYMENT_IN_FLIGHT:
                if (
                    record.payment_result is not None
                    and record.payment_result.status is PaymentStatus.SUCCEEDED
                ):
                    return await self._ensure_success_spend_locked(record)
                return record
            if record.state is not EscrowLifecycleState.PARENT_CONFIRMED:
                raise ChannelInputEscrowError("payment requires a confirmed escrow parent")
            record = await self._observe_chain_locked(record)
            if record.state is not EscrowLifecycleState.PARENT_CONFIRMED:
                raise ChannelInputEscrowError("payment requires a fresh confirmed escrow parent")
            if record.payment_request is None:
                prepared = self._prepared_required(record)
                observation = record.chain_observation
                if observation is None:
                    raise ChannelInputEscrowError("confirmed escrow lacks a chain observation")
                request = SinglePartPaymentRequest(
                    prepared=prepared,
                    chain_observation=observation,
                    excluded_channel_ids=tuple(excluded_channel_ids),
                    requested_at_height=observation.observed_height,
                    requested_at_unix=requested_at_unix,
                )
                expected_revision = record.revision
                record.attach_payment_request(request)
                self._save(record, expected_revision)
            return await self._send_payment_locked(record)

    async def observe_payment(self, session_id: str) -> ChannelInputEscrowRecord:
        """Persist a monotone backend payment observation and recover success promptly."""
        async with self._lock_for(session_id):
            record = self._load_required(session_id)
            if record.payment_result is None:
                raise ChannelInputEscrowError(
                    "payment observation requires a durable backend payment ID"
                )
            result = await self._backend.observe_escrow_payment(
                PaymentObservationRequest(
                    session_id=session_id, payment_id=record.payment_result.payment_id
                )
            )
            expected_revision = record.revision
            record.attach_payment_result(result)
            if (
                record.state is EscrowLifecycleState.PARENT_REORGED
                and record.chain_observation is not None
                and record.chain_observation.conflict_txid is not None
                and result.status is PaymentStatus.FAILED
            ):
                record.advance(EscrowLifecycleState.COMMITMENT_CONFLICT)
            self._save(record, expected_revision)
            if record.state in {
                EscrowLifecycleState.COMMITMENT_CONFLICT,
                EscrowLifecycleState.CROSS_LAYER_LOSS,
            }:
                return record
            if result.status is PaymentStatus.SUCCEEDED:
                return await self._ensure_success_spend_locked(record)
            return record

    async def _ensure_success_spend_locked(
        self, record: ChannelInputEscrowRecord
    ) -> ChannelInputEscrowRecord:
        valid_states = {EscrowLifecycleState.PAYMENT_IN_FLIGHT}
        if record.state is EscrowLifecycleState.PARENT_REORGED:
            if record.state_before_reorg not in {
                EscrowLifecycleState.PAYMENT_IN_FLIGHT,
                EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            }:
                raise ChannelInputEscrowError("reorganized escrow cannot build a success spend")
        elif record.state not in valid_states:
            if record.state is EscrowLifecycleState.SUCCESS_SPEND_PENDING:
                return record
            raise ChannelInputEscrowError("success spend requires an in-flight escrow payment")
        if (
            record.payment_result is None
            or record.payment_result.status is not PaymentStatus.SUCCEEDED
        ):
            raise ChannelInputEscrowError("success spend requires a settled escrow payment")
        if record.success_spend is None:
            spend = await self._backend.build_broadcast_success_spend(
                SuccessSpendRequest(
                    prepared=self._prepared_required(record), payment_result=record.payment_result
                )
            )
            expected_revision = record.revision
            record.attach_success_spend(spend)
            self._save(record, expected_revision)
        return await self._observe_chain_locked_after_success(record)

    async def _observe_chain_locked_after_success(
        self, record: ChannelInputEscrowRecord
    ) -> ChannelInputEscrowRecord:
        record = await self._observe_chain_locked(record)
        if record.state is EscrowLifecycleState.PAYMENT_IN_FLIGHT:
            expected_revision = record.revision
            record.advance(EscrowLifecycleState.SUCCESS_SPEND_PENDING)
            self._save(record, expected_revision)
        return record

    async def ensure_success_spend(self, session_id: str) -> ChannelInputEscrowRecord:
        """Build once and monitor the success spend for a settled payment."""
        async with self._lock_for(session_id):
            return await self._ensure_success_spend_locked(self._load_required(session_id))

    async def broadcast_timeout(self, session_id: str) -> ChannelInputEscrowRecord:
        """Broadcast the exact timeout recovery only after failure or no payment offer."""
        async with self._lock_for(session_id):
            record = self._load_required(session_id)
            if record.state not in {
                EscrowLifecycleState.PARENT_CONFIRMED,
                EscrowLifecycleState.PAYMENT_IN_FLIGHT,
                EscrowLifecycleState.TIMEOUT_PENDING,
            }:
                raise ChannelInputEscrowError(
                    "timeout recovery requires an active confirmed escrow"
                )
            if (
                record.payment_result is not None
                and record.payment_result.status is not PaymentStatus.FAILED
            ):
                raise ChannelInputEscrowError(
                    "timeout recovery requires a failed or unoffered payment"
                )
            if record.timeout_broadcast is None:
                timeout = await self._backend.broadcast_timeout_recovery(
                    RecoveryRequest(record=record)
                )
                expected_revision = record.revision
                record.attach_timeout_broadcast(timeout)
                self._save(record, expected_revision)
            record = await self._observe_chain_locked(record)
            if record.state in {
                EscrowLifecycleState.PARENT_CONFIRMED,
                EscrowLifecycleState.PAYMENT_IN_FLIGHT,
            }:
                expected_revision = record.revision
                record.advance(EscrowLifecycleState.TIMEOUT_PENDING)
                self._save(record, expected_revision)
            return record

    async def fee_bump_success(self, session_id: str) -> ChannelInputEscrowRecord:
        """Append a validated success-spend replacement and refresh chain evidence."""
        async with self._lock_for(session_id):
            record = self._load_required(session_id)
            if record.state is not EscrowLifecycleState.SUCCESS_SPEND_PENDING and not (
                record.state is EscrowLifecycleState.PARENT_REORGED
                and record.state_before_reorg is EscrowLifecycleState.SUCCESS_SPEND_PENDING
            ):
                raise ChannelInputEscrowError("success fee bump requires a pending success spend")
            replacement = await self._backend.fee_bump_success_spend(RecoveryRequest(record=record))
            expected_revision = record.revision
            record.attach_success_spend(replacement)
            self._save(record, expected_revision)
            return await self._observe_chain_locked(record)

    async def recover_reorg(self, session_id: str) -> ChannelInputEscrowRecord:
        """Rebroadcast durable artifacts before resolving or restoring a parent reorg."""
        async with self._lock_for(session_id):
            record = self._load_required(session_id)
            if record.state is not EscrowLifecycleState.PARENT_REORGED:
                raise ChannelInputEscrowError("reorg recovery requires parent_reorged state")
            prepared = self._prepared_required(record)
            observation = await self._backend.observe_escrow_chain(
                ChainObservationRequest(
                    session_id=record.session_id,
                    parent_txid=prepared.parent_evidence.parent_txid,
                )
            )
            record = await self._ensure_timeout_evidence_locked(record, observation)
            if observation.conflict_txid is not None:
                expected_revision = record.revision
                record.attach_chain_observation(observation)
                self._advance_from_observation(record, observation)
                self._save(record, expected_revision)
                return record
            if record.recovery_required:
                recovery = await self._backend.rebroadcast_recovery_artifacts(
                    RecoveryRequest(record=record)
                )
                expected_revision = record.revision
                record.attach_recovery_broadcast(recovery)
                self._save(record, expected_revision)
            expected_revision = record.revision
            record.attach_chain_observation(observation)
            self._advance_from_observation(record, observation)
            if (
                record.state is EscrowLifecycleState.PARENT_REORGED
                and observation.in_best_chain
                and observation.parent_confirmations >= record.terms.parent_confirmation_depth
                and record.state_before_reorg is not None
            ):
                record.advance(record.state_before_reorg)
                self._advance_from_observation(record, observation)
            self._save(record, expected_revision)
            return record

    async def resume_durable_sessions(self) -> list[ChannelInputEscrowRecord]:
        """Reconcile endpoint snapshots, then resume only already durable side effects."""
        backend_records = await self._backend.resume_durable_sessions()
        local_records = {record.session_id: record for record in self._store.list()}
        errors: list[ChannelInputEscrowError] = []
        backend_session_ids: set[str] = set()
        for backend_record in backend_records:
            if backend_record.session_id in backend_session_ids:
                errors.append(
                    ChannelInputEscrowError("backend returned a duplicate escrow session")
                )
                continue
            backend_session_ids.add(backend_record.session_id)
            local = local_records.get(backend_record.session_id)
            if local is None:
                errors.append(ChannelInputEscrowError("backend returned an unknown escrow session"))
            elif backend_record.terms != local.terms:
                errors.append(
                    ChannelInputEscrowError("backend escrow terms conflict with local record")
                )
            elif backend_record.revision > local.revision:
                errors.append(
                    ChannelInputEscrowError("backend escrow revision is ahead of local authority")
                )
            elif backend_record.revision == local.revision and backend_record != local:
                errors.append(
                    ChannelInputEscrowError("same escrow revision has different backend contents")
                )
        if errors:
            raise errors[0]

        resumed: list[ChannelInputEscrowRecord] = []
        for session_id in local_records:
            try:
                record = self._load_required(session_id)
                if (
                    record.state is EscrowLifecycleState.NEGOTIATED
                    and record.negotiated_parent_evidence is not None
                ):
                    record = await self.prepare_timeout(
                        PrepareTimeoutRequest(
                            terms=record.terms,
                            parent_evidence=record.negotiated_parent_evidence,
                        )
                    )
                if record.state is EscrowLifecycleState.TIMEOUT_SIGNED:
                    record = await self.authorize_parent(session_id)
                if record.state is EscrowLifecycleState.PARENT_SIGNED:
                    record = await self.broadcast_parent(session_id)
                if record.state in {
                    EscrowLifecycleState.PARENT_PENDING,
                    EscrowLifecycleState.PARENT_CONFIRMED,
                    EscrowLifecycleState.PAYMENT_IN_FLIGHT,
                    EscrowLifecycleState.SUCCESS_SPEND_PENDING,
                    EscrowLifecycleState.TIMEOUT_PENDING,
                    EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
                    EscrowLifecycleState.TIMEOUT_CONFIRMED,
                    EscrowLifecycleState.PAID_TIMEOUT_LOSS,
                }:
                    record = await self.observe_chain(session_id)
                if (
                    record.state is EscrowLifecycleState.PARENT_CONFIRMED
                    and record.payment_request is not None
                    and record.payment_result is None
                ):
                    async with self._lock_for(session_id):
                        record = await self._send_payment_locked(self._load_required(session_id))
                payment_active = record.state is EscrowLifecycleState.PAYMENT_IN_FLIGHT or (
                    record.state is EscrowLifecycleState.PARENT_REORGED
                    and record.state_before_reorg is EscrowLifecycleState.PAYMENT_IN_FLIGHT
                )
                if payment_active:
                    if (
                        record.payment_result is not None
                        and record.payment_result.status is PaymentStatus.SUCCEEDED
                    ):
                        record = await self.ensure_success_spend(session_id)
                    elif record.payment_result is not None:
                        record = await self.observe_payment(session_id)
                if record.state is EscrowLifecycleState.PARENT_REORGED:
                    record = await self.recover_reorg(session_id)
                resumed.append(record)
            except ChannelInputEscrowError as exc:
                errors.append(exc)
            except Exception as exc:  # noqa: BLE001 - continue remaining durable recoveries
                errors.append(ChannelInputEscrowError(str(exc)))
        if errors:
            raise errors[0]
        return resumed
