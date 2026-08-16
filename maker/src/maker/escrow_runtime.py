"""Durable maker endpoint runtime for channel-input escrow protocol v1.

This module deliberately owns only endpoint-private handshake and signing state.
The coordinator remains the authority for the post-broadcast maker lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from jmswap.bitcoin_escrow import ChannelInputEscrowTree
from jmswap.channel_escrow import (
    ChainObservationRequest,
    ChannelInputEscrowRecord,
    ChannelInputEscrowStore,
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowChainObservation,
    EscrowOutpoint,
    EscrowParentBroadcast,
    EscrowParentEvidence,
    EscrowSpendResult,
    EscrowTimeoutBroadcast,
    EscrowTimeoutEvidence,
    FreezeChannelRequest,
    ParentAuthorization,
    ParentAuthorizationRequest,
    ParentBroadcastRequest,
    PaymentObservationRequest,
    PreparedChannelInputEscrow,
    PrepareTimeoutRequest,
    RecoveryBroadcastResult,
    RecoveryRequest,
    SinglePartPaymentRequest,
    SinglePartPaymentResult,
    SuccessSpendRequest,
)
from jmswap.errors import ChannelInputEscrowError
from jmswap.lnd import LndEscrowInvoice, LndEscrowStage, LndEscrowStatus
from jmswap.peer_transport import (
    EndpointAbortPayload,
    EndpointAbortReason,
    EndpointTermsPayload,
    EscrowInvoiceProposalPayload,
    EscrowPeerEnvelope,
    EscrowProtocolRole,
    EscrowPublicKeysPayload,
    ParentMuSig2NoncePayload,
    ParentMuSig2PartialSignaturePayload,
    ParentPreparationPayload,
    ParentPrevoutPayload,
    TimeoutMuSig2NoncePayload,
    TimeoutMuSig2PartialSignaturePayload,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from maker.escrow_endpoint import (
    ChannelInputParentSigningBackend,
    EndpointEscrowSigner,
    EndpointParentSigner,
)
from maker.escrow_keys import (
    EscrowEndpointRole,
    WalletEscrowKeyDeriver,
    derive_invoice_preimage,
)

_MODEL_CONFIG = ConfigDict(extra="forbid", strict=True)
_FROZEN_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)


@runtime_checkable
class EscrowInvoiceBackend(Protocol):
    """Invoice operations needed by the endpoint handshake."""

    async def create_escrow_invoice(
        self,
        amount_msat: int,
        preimage: bytes,
        expiry_seconds: int,
        min_final_cltv_delta: int,
        memo: str = "",
    ) -> LndEscrowInvoice: ...

    async def decode_escrow_invoice(self, invoice: str) -> LndEscrowInvoice: ...


@runtime_checkable
class EscrowFreezeBackend(ChannelInputParentSigningBackend, Protocol):
    """LND operations that have no chain or payment responsibility."""

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> ChannelSnapshot: ...

    async def channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus: ...


@runtime_checkable
class EscrowHeightSource(Protocol):
    """Current chain height for negotiating a new term set."""

    async def block_height(self) -> int: ...


@runtime_checkable
class EscrowChainAdapter(Protocol):
    """Chain/recovery methods delegated by the endpoint backend."""

    async def register_frozen_channel(self, session_id: str, outpoint: EscrowOutpoint) -> None: ...

    async def broadcast_parent(self, request: ParentBroadcastRequest) -> EscrowParentBroadcast: ...

    async def observe_escrow_chain(
        self, request: ChainObservationRequest
    ) -> EscrowChainObservation: ...

    async def build_broadcast_success_spend(
        self, request: SuccessSpendRequest
    ) -> EscrowSpendResult: ...

    async def broadcast_timeout_recovery(
        self, request: RecoveryRequest
    ) -> EscrowTimeoutBroadcast: ...

    async def fee_bump_success_spend(self, request: RecoveryRequest) -> EscrowSpendResult: ...

    async def rebroadcast_recovery_artifacts(
        self, request: RecoveryRequest
    ) -> RecoveryBroadcastResult: ...


@runtime_checkable
class EscrowPaymentAdapter(Protocol):
    """Payment methods delegated by the endpoint backend."""

    async def send_single_part_payment(
        self, request: SinglePartPaymentRequest
    ) -> SinglePartPaymentResult: ...

    async def observe_escrow_payment(
        self, request: PaymentObservationRequest
    ) -> SinglePartPaymentResult: ...


@runtime_checkable
class EscrowPeerBrokerProtocol(Protocol):
    """Narrow durable broker surface required by endpoint protocol recovery."""

    def start(self) -> None: ...

    async def close(self) -> None: ...

    def binding(self, edge_id: str) -> Any: ...

    def pending(self, edge_id: str) -> tuple[EscrowPeerEnvelope, ...]: ...

    async def send(self, edge_id: str, payload: Any) -> EscrowPeerEnvelope: ...

    async def receive(
        self,
        edge_id: str,
        expected_type: Any,
        *,
        predicate: Any = None,
        timeout: float | None = None,
    ) -> EscrowPeerEnvelope: ...

    def complete(self, envelope: EscrowPeerEnvelope) -> None: ...


@dataclass(frozen=True, slots=True)
class EndpointEscrowRuntimePolicy:
    """Immutable endpoint economics and timing policy for one deployment."""

    premium_sats: int
    timeout_compensation_sats: int
    escrow_value_sats: int
    timeout_fee_sats: int
    invoice_expiry_seconds: int
    invoice_min_final_cltv_delta: int
    peer_settle_delta_blocks: int
    success_broadcast_delta_blocks: int
    timeout_delta_blocks: int
    parent_confirmation_depth: int = 1
    success_confirmation_depth: int = 1
    success_confirmation_budget_blocks: int = 1
    reorg_margin_blocks: int = 0
    safety_delta_blocks: int = 1
    preimage_budget_blocks: int = 0
    peer_message_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        integers = (
            self.premium_sats,
            self.timeout_compensation_sats,
            self.escrow_value_sats,
            self.timeout_fee_sats,
            self.invoice_expiry_seconds,
            self.invoice_min_final_cltv_delta,
            self.peer_settle_delta_blocks,
            self.success_broadcast_delta_blocks,
            self.timeout_delta_blocks,
            self.parent_confirmation_depth,
            self.success_confirmation_depth,
            self.success_confirmation_budget_blocks,
            self.reorg_margin_blocks,
            self.safety_delta_blocks,
            self.preimage_budget_blocks,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in integers):
            raise TypeError("escrow runtime policy values must be integers")
        if (
            isinstance(self.peer_message_timeout_seconds, bool)
            or not isinstance(self.peer_message_timeout_seconds, (int, float))
            or self.peer_message_timeout_seconds <= 0
        ):
            raise ValueError("peer_message_timeout_seconds must be positive")
        if (
            self.premium_sats < 0
            or self.timeout_compensation_sats < 0
            or self.timeout_fee_sats < 0
            or self.escrow_value_sats <= 0
            or self.invoice_expiry_seconds <= 0
            or self.invoice_min_final_cltv_delta <= 0
            or self.peer_settle_delta_blocks < 0
            or self.success_broadcast_delta_blocks < 0
            or self.timeout_delta_blocks <= 0
            or self.parent_confirmation_depth <= 0
            or self.success_confirmation_depth <= 0
            or self.success_confirmation_budget_blocks <= 0
            or self.reorg_margin_blocks < 0
            or self.safety_delta_blocks <= 0
            or self.preimage_budget_blocks < 0
        ):
            raise ValueError("escrow runtime policy has an invalid non-positive value")
        if not (
            self.peer_settle_delta_blocks < self.success_broadcast_delta_blocks
            and self.success_broadcast_delta_blocks
            + self.success_confirmation_budget_blocks
            + self.reorg_margin_blocks
            < self.timeout_delta_blocks
        ):
            raise ValueError("escrow runtime policy deadlines are not safely ordered")


class EndpointEscrowSessionRecord(BaseModel):
    """Private-safe, replayable evidence for one endpoint side of an edge."""

    model_config = _MODEL_CONFIG

    edge_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    role: EscrowEndpointRole
    snapshot: ChannelSnapshot
    frozen_channel_registered: bool = False
    local_keys: EscrowPublicKeysPayload
    remote_keys: EscrowPublicKeysPayload | None = None
    invoice_proposal: EscrowInvoiceProposalPayload | None = None
    terms: ChannelInputEscrowTerms | None = None
    parent: ParentPreparationPayload | None = None
    timeout_local_nonce: TimeoutMuSig2NoncePayload | None = None
    timeout_remote_nonce: TimeoutMuSig2NoncePayload | None = None
    timeout_local_partial: TimeoutMuSig2PartialSignaturePayload | None = None
    timeout_remote_partial: TimeoutMuSig2PartialSignaturePayload | None = None
    timeout_evidence: EscrowTimeoutEvidence | None = None
    parent_local_nonce: ParentMuSig2NoncePayload | None = None
    parent_remote_nonce: ParentMuSig2NoncePayload | None = None
    parent_local_partial: ParentMuSig2PartialSignaturePayload | None = None
    parent_remote_partial: ParentMuSig2PartialSignaturePayload | None = None
    authorization: ParentAuthorization | None = None
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_protocol_state(self) -> EndpointEscrowSessionRecord:
        if self.snapshot.session_id != self.edge_id:
            raise ValueError("endpoint snapshot session must match edge ID")
        expected_local = (
            EscrowProtocolRole.MAKER
            if self.role is EscrowEndpointRole.MAKER
            else EscrowProtocolRole.PEER
        )
        if self.local_keys.role is not expected_local:
            raise ValueError("endpoint local public keys do not match economic role")
        if self.remote_keys is not None:
            expected_remote = (
                EscrowProtocolRole.PEER
                if self.role is EscrowEndpointRole.MAKER
                else EscrowProtocolRole.MAKER
            )
            if self.remote_keys.role is not expected_remote:
                raise ValueError("endpoint remote public keys do not match economic role")
        if self.invoice_proposal is not None:
            if (
                self.remote_keys is None
                or self.invoice_proposal.role is not EscrowProtocolRole.PEER
            ):
                raise ValueError("endpoint invoice proposal requires peer public keys")
        if self.terms is not None:
            if self.remote_keys is None or self.invoice_proposal is None:
                raise ValueError("endpoint terms require keys and invoice proposal")
            maker_keys = (
                self.local_keys if self.role is EscrowEndpointRole.MAKER else self.remote_keys
            )
            if (
                self.terms.snapshot != self.snapshot
                or self.terms.authorizer_pubkey != maker_keys.escrow_pubkey
                or self.terms.payment_hash != self.invoice_proposal.payment_hash
            ):
                raise ValueError("endpoint terms do not bind canonical keys and snapshot")
        if self.parent is not None:
            if self.terms is None:
                raise ValueError("endpoint parent requires terms")
            if (
                self.parent.channel_input_index >= len(self.parent.ordered_prevouts)
                or self.parent.ordered_prevouts[self.parent.channel_input_index].value_sats
                != self.snapshot.capacity_sats
                or self.parent.ordered_prevouts[self.parent.channel_input_index].scriptpubkey
                != self.snapshot.funding_scriptpubkey
                or self.parent.escrow_value_sats != self.terms.escrow_value_sats
            ):
                raise ValueError("endpoint parent does not bind canonical terms")
        for nonce in (self.timeout_local_nonce, self.timeout_remote_nonce):
            if nonce is not None and (
                self.parent is None or nonce.round_id != self.parent.round_id
            ):
                raise ValueError("timeout nonce requires the prepared parent round")
        for partial in (self.timeout_local_partial, self.timeout_remote_partial):
            if partial is not None and (
                self.parent is None or partial.round_id != self.parent.round_id
            ):
                raise ValueError("timeout partial requires the prepared parent round")
        if self.timeout_evidence is not None:
            if (
                self.parent is None
                or self.terms is None
                or self.timeout_local_nonce is None
                or self.timeout_remote_nonce is None
                or self.timeout_local_partial is None
                or self.timeout_remote_partial is None
                or self.timeout_evidence.spent_escrow_outpoint.txid != self.parent.parent_txid
                or self.timeout_evidence.spent_escrow_outpoint.vout
                != self.parent.escrow_output_index
                or self.timeout_evidence.locktime != self.terms.timeout_height
            ):
                raise ValueError("timeout evidence lacks its exact signing predecessors")
        for parent_nonce in (self.parent_local_nonce, self.parent_remote_nonce):
            if parent_nonce is not None and (
                self.parent is None
                or self.timeout_evidence is None
                or parent_nonce.parent_txid != self.parent.parent_txid
            ):
                raise ValueError("parent nonce requires timeout evidence and prepared parent")
        for parent_partial in (self.parent_local_partial, self.parent_remote_partial):
            if parent_partial is not None and (
                self.parent is None
                or self.parent_local_nonce is None
                or self.parent_remote_nonce is None
                or parent_partial.parent_txid != self.parent.parent_txid
                or parent_partial.round_id != self.parent_local_nonce.round_id
                or parent_partial.round_id != self.parent_remote_nonce.round_id
            ):
                raise ValueError("parent partial lacks its exact nonce transcript")
        if self.authorization is not None:
            if (
                self.parent is None
                or self.timeout_evidence is None
                or self.parent_local_partial is None
                or self.parent_remote_partial is None
                or self.authorization.session_id != self.edge_id
                or self.authorization.parent_txid != self.parent.parent_txid
                or self.authorization.timeout_txid != self.timeout_evidence.timeout_txid
            ):
                raise ValueError("authorization lacks its exact parent signing evidence")
        return self

    def advance(self, **updates: object) -> EndpointEscrowSessionRecord:
        """Return a next immutable semantic record revision."""
        return type(self).model_validate(
            {**self.model_dump(), **updates, "revision": self.revision + 1}
        )


class EndpointEscrowSessionStore:
    """Locked one-file-per-edge endpoint journal with atomic compare-and-swap writes."""

    def __init__(self, directory: str | Path) -> None:
        self._directory = Path(directory)
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._directory, 0o700)
        if not self._directory.is_dir() or stat.S_IMODE(self._directory.stat().st_mode) != 0o700:
            raise ChannelInputEscrowError(
                "endpoint escrow journal directory has unsafe permissions"
            )

    def _path(self, edge_id: str) -> Path:
        if len(edge_id) != 64 or any(character not in "0123456789abcdef" for character in edge_id):
            raise ChannelInputEscrowError("endpoint escrow edge ID is invalid")
        return self._directory / f"{edge_id}.json"

    @contextlib.contextmanager
    def _lock(self, edge_id: str) -> Any:
        descriptor = os.open(
            self._path(edge_id).with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600
        )
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load(self, edge_id: str) -> EndpointEscrowSessionRecord | None:
        path = self._path(edge_id)
        if not path.exists():
            return None
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ChannelInputEscrowError("endpoint escrow journal has unsafe permissions")
        try:
            record = EndpointEscrowSessionRecord.model_validate_json(path.read_bytes())
        except Exception as exc:  # noqa: BLE001 - journal corruption must fail closed
            raise ChannelInputEscrowError("endpoint escrow journal is invalid") from exc
        if record.edge_id != edge_id:
            raise ChannelInputEscrowError("endpoint escrow journal edge does not match")
        return record

    def list(self) -> list[EndpointEscrowSessionRecord]:
        records: list[EndpointEscrowSessionRecord] = []
        for path in self._directory.glob("*.json"):
            try:
                record = EndpointEscrowSessionRecord.model_validate_json(path.read_bytes())
                if path == self._path(record.edge_id):
                    records.append(record)
            except Exception:  # noqa: BLE001 - leave corrupt evidence for operator recovery
                continue
        return sorted(records, key=lambda record: record.edge_id)

    def save(
        self, record: EndpointEscrowSessionRecord, *, expected_revision: int | None = None
    ) -> None:
        validated = EndpointEscrowSessionRecord.model_validate(record.model_dump())
        path = self._path(validated.edge_id)
        with self._lock(validated.edge_id):
            current = self.load(validated.edge_id)
            if current is None:
                if expected_revision is not None:
                    raise ChannelInputEscrowError("new endpoint record cannot use compare-and-swap")
            elif validated.revision < current.revision:
                raise ChannelInputEscrowError("endpoint escrow journal is newer than this write")
            elif validated.revision == current.revision:
                if validated != current:
                    raise ChannelInputEscrowError("endpoint escrow journal revision collision")
                return
            elif expected_revision != current.revision:
                raise ChannelInputEscrowError("endpoint escrow journal compare-and-swap failed")
            descriptor, temporary = tempfile.mkstemp(
                dir=self._directory, prefix=".endpoint-", suffix=".tmp"
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(validated.model_dump_json().encode("ascii"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                directory_descriptor = os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary)
                raise


class ChannelInputEscrowRuntime:
    """Concrete maker backend and economic-peer service for one escrow deployment."""

    # Enabled only by the production composition after recovery validation passes.
    channel_input_escrow_capabilities: frozenset[Any] = frozenset()

    def __init__(
        self,
        *,
        lnd: EscrowFreezeBackend,
        key_deriver: WalletEscrowKeyDeriver,
        broker: EscrowPeerBrokerProtocol,
        chain: EscrowChainAdapter,
        payment: EscrowPaymentAdapter,
        height_source: EscrowHeightSource,
        policy: EndpointEscrowRuntimePolicy,
        endpoint_store: EndpointEscrowSessionStore,
        coordinator_store: ChannelInputEscrowStore | None = None,
    ) -> None:
        if not isinstance(lnd, EscrowFreezeBackend) or not isinstance(lnd, EscrowInvoiceBackend):
            raise TypeError("lnd lacks the required escrow endpoint operations")
        if not isinstance(key_deriver, WalletEscrowKeyDeriver):
            raise TypeError("key_deriver must be a WalletEscrowKeyDeriver")
        if not isinstance(broker, EscrowPeerBrokerProtocol):
            raise TypeError("broker lacks the required durable escrow methods")
        if not isinstance(chain, EscrowChainAdapter):
            raise TypeError("chain lacks the required escrow delegation methods")
        if not isinstance(payment, EscrowPaymentAdapter):
            raise TypeError("payment lacks the required escrow delegation methods")
        if not isinstance(height_source, EscrowHeightSource):
            raise TypeError("height_source must provide block_height")
        self._lnd = lnd
        self._signer = EndpointEscrowSigner(key_deriver)
        self._parent_signer = EndpointParentSigner(lnd)
        self._broker = broker
        self._chain = chain
        self._payment = payment
        self._height_source = height_source
        self._policy = policy
        self._endpoint_store = endpoint_store
        self._coordinator_store = coordinator_store
        self._locks: dict[str, asyncio.Lock] = {}
        self._service_tasks: dict[str, asyncio.Task[None]] = {}

    def start(self, edge_ids: tuple[str, ...] = ()) -> None:
        """Start broker ownership and peer services for known finalized ring edges."""
        self._broker.start()
        for edge_id in edge_ids:
            record = self._endpoint_store.load(edge_id)
            if (
                record is not None and record.role is EscrowEndpointRole.MAKER
            ) or edge_id in self._service_tasks:
                continue
            if edge_id not in self._service_tasks:
                self._service_tasks[edge_id] = asyncio.create_task(
                    self._serve_peer(edge_id), name=f"escrow-peer-{edge_id[:12]}"
                )

    async def close(self) -> None:
        """Cancel peer services before closing the shared broker subscription."""
        tasks = tuple(self._service_tasks.values())
        self._service_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._broker.close()

    def recover_known_finalized_ring_edges(self, edge_ids: tuple[str, ...]) -> None:
        """Register durable peer services without invoking coordinator payment behavior."""
        self.start(edge_ids)

    @staticmethod
    def canonical_snapshot(snapshot: ChannelSnapshot, role: EscrowEndpointRole) -> ChannelSnapshot:
        """Convert LND-local values into the economic-maker orientation."""
        if role is EscrowEndpointRole.MAKER:
            return snapshot
        if role is not EscrowEndpointRole.PEER:
            raise ChannelInputEscrowError("invalid endpoint role")
        return snapshot.model_copy(
            update={
                "local_funding_pubkey": snapshot.remote_funding_pubkey,
                "remote_funding_pubkey": snapshot.local_funding_pubkey,
                "maker_claim_sats": snapshot.peer_claim_sats,
                "peer_claim_sats": snapshot.maker_claim_sats,
            }
        )

    async def initiate(self, edge_id: str, channel_id: str) -> ChannelSnapshot:
        """Freeze a maker-owned edge, journal initiation, then send maker public keys."""
        snapshot = await self.freeze_quiesce_channel(
            FreezeChannelRequest(session_id=edge_id, channel_id=channel_id)
        )
        record = self._require(edge_id)
        await self._broker.send(edge_id, record.local_keys)
        return snapshot

    async def complete_maker_handshake(
        self, edge_id: str, *, timeout: float | None = None
    ) -> ChannelInputEscrowTerms:
        """Receive peer keys/invoice, verify them independently, and send exact terms."""
        async with self._lock_for(edge_id):
            record = self._require_role(edge_id, EscrowEndpointRole.MAKER)
            await self._broker.send(edge_id, record.local_keys)
            record = await self._ensure_remote_keys(record, timeout)
            record, proposal = await self._ensure_invoice_proposal(record, timeout)
            decoded = await self._lnd.decode_escrow_invoice(proposal.invoice)
            self._require_invoice_matches(proposal, decoded)
            if record.terms is None:
                record = self._persist(
                    record, terms=await self._build_terms(record, proposal, decoded)
                )
            terms = record.terms
            if terms is None:
                raise ChannelInputEscrowError("maker terms were not persisted")
            await self._broker.send(edge_id, EndpointTermsPayload(terms=terms))
            return terms

    async def freeze_quiesce_channel(self, request: FreezeChannelRequest) -> ChannelSnapshot:
        """Freeze, canonically journal, and register monitoring before witness release."""
        edge_id = self._edge_id(request.session_id)
        await self._cancel_service(edge_id)
        async with self._lock_for(edge_id):
            existing = self._endpoint_store.load(edge_id)
            if existing is not None:
                if (
                    existing.role is not EscrowEndpointRole.MAKER
                    or existing.snapshot.channel_id != request.channel_id
                ):
                    raise ChannelInputEscrowError("conflicting endpoint role or frozen channel")
                return existing.snapshot
            frozen = await self._lnd.freeze_channel_input(edge_id, request.channel_id)
            snapshot = self.canonical_snapshot(frozen, EscrowEndpointRole.MAKER)
            if snapshot.session_id != edge_id or snapshot.channel_id != request.channel_id:
                raise ChannelInputEscrowError("LND froze a different channel escrow session")
            keys = self._signer.derive_keys(edge_id, EscrowEndpointRole.MAKER).public_payload()
            await self._chain.register_frozen_channel(edge_id, snapshot.funding_outpoint)
            record = EndpointEscrowSessionRecord(
                edge_id=edge_id,
                role=EscrowEndpointRole.MAKER,
                snapshot=snapshot,
                frozen_channel_registered=True,
                local_keys=keys,
            )
            self._endpoint_store.save(record)
            return snapshot

    async def prepare_signed_timeout(
        self, request: PrepareTimeoutRequest
    ) -> PreparedChannelInputEscrow:
        """Maker timeout-before-parent signing sequence, durable at every receipt."""
        edge_id = self._edge_id(request.terms.snapshot.session_id)
        async with self._lock_for(edge_id):
            record = self._require_role(edge_id, EscrowEndpointRole.MAKER)
            self._require_terms(record, request.terms)
            self._require_frozen_registration(record)
            parent = self._parent_payload(request.parent_evidence)
            if record.parent is not None and record.parent != parent:
                raise ChannelInputEscrowError("conflicting escrow parent preparation")
            if record.parent is None:
                record = self._persist(record, parent=parent)
            if record.timeout_evidence is not None:
                return PreparedChannelInputEscrow(
                    terms=request.terms,
                    parent_evidence=request.parent_evidence,
                    timeout_evidence=record.timeout_evidence,
                )
            await self._parent_signer.prepare_parent(
                edge_id, record.snapshot.funding_outpoint, parent
            )
            await self._broker.send(edge_id, parent)
            record = await self._complete_timeout(record)
            if record.timeout_evidence is None:
                raise ChannelInputEscrowError("timeout evidence was not persisted")
            return PreparedChannelInputEscrow(
                terms=request.terms,
                parent_evidence=request.parent_evidence,
                timeout_evidence=record.timeout_evidence,
            )

    async def authorize_parent(self, request: ParentAuthorizationRequest) -> ParentAuthorization:
        """Release only the exact locally persisted LND witness after timeout evidence."""
        prepared = request.prepared
        edge_id = self._edge_id(prepared.terms.snapshot.session_id)
        async with self._lock_for(edge_id):
            record = self._require_role(edge_id, EscrowEndpointRole.MAKER)
            self._require_terms(record, prepared.terms)
            if record.timeout_evidence != prepared.timeout_evidence or record.parent is None:
                raise ChannelInputEscrowError("timeout evidence is not the durable parent evidence")
            record = await self._reconcile_lnd(record)
            if record.authorization is not None:
                return record.authorization
            parent = record.parent
            timeout_evidence = record.timeout_evidence
            if parent is None or timeout_evidence is None:
                raise ChannelInputEscrowError("parent signing prerequisites were not persisted")
            if record.parent_local_partial is not None:
                local_nonce = record.parent_local_nonce
                if local_nonce is None:
                    raise ChannelInputEscrowError("durable parent partial lacks its local nonce")
                await self._broker.send(edge_id, local_nonce)
                await self._broker.send(edge_id, record.parent_local_partial)
                record, remote_partial = await self._ensure_parent_remote_partial(record)
                artifact = await self._parent_signer.recover_parent(
                    edge_id=edge_id,
                    funding_outpoint=record.snapshot.funding_outpoint,
                    parent=parent,
                    timeout_evidence=timeout_evidence,
                    remote_partial_payload=remote_partial,
                    round_id=local_nonce.round_id,
                )
                authorization = self._authorization(record, artifact)
                return (
                    self._persist_exact(record, "authorization", authorization).authorization
                    or authorization
                )
            context = await self._parent_signer.begin_signing(
                edge_id=edge_id,
                role=record.role,
                funding_outpoint=record.snapshot.funding_outpoint,
                parent=parent,
                timeout_evidence=timeout_evidence,
            )
            local_nonce = self._parent_signer.nonce_payload(context)
            record = self._persist_exact(record, "parent_local_nonce", local_nonce)
            await self._broker.send(edge_id, local_nonce)
            record, remote_nonce = await self._ensure_parent_remote_nonce(record)
            local_partial = await self._parent_signer.sign_parent(context, remote_nonce)
            record = self._persist_exact(record, "parent_local_partial", local_partial)
            await self._broker.send(edge_id, local_partial)
            record, remote_partial = await self._ensure_parent_remote_partial(record)
            artifact = await self._parent_signer.finalize_parent(context, remote_partial)
            authorization = self._authorization(record, artifact)
            return (
                self._persist_exact(record, "authorization", authorization).authorization
                or authorization
            )

    async def handle_inbound(self, envelope: EscrowPeerEnvelope) -> None:
        """Handle a peer-side handshake/signing receipt after its broker acceptance."""
        edge_id = self._edge_id(envelope.edge_id)
        payload = envelope.payload
        try:
            if (
                isinstance(payload, EscrowPublicKeysPayload)
                and payload.role is EscrowProtocolRole.MAKER
            ):
                await self._accept_maker_initiation(edge_id, envelope, payload)
            elif isinstance(payload, EndpointTermsPayload):
                await self._accept_terms(edge_id, envelope, payload)
            elif isinstance(payload, ParentPreparationPayload):
                await self._accept_parent(edge_id, envelope, payload)
            else:
                raise ChannelInputEscrowError("unexpected escrow peer payload")
        except ChannelInputEscrowError:
            await self._send_abort(edge_id, payload)
            raise

    async def _serve_peer(self, edge_id: str) -> None:
        while True:
            record = self._endpoint_store.load(edge_id)
            if record is None:
                envelope = await self._broker.receive(
                    edge_id, EscrowPublicKeysPayload, timeout=self._message_timeout()
                )
                await self.handle_inbound(envelope)
                continue
            if record.role is EscrowEndpointRole.MAKER:
                return
            record = await self._ensure_peer_offer(record)
            if record.terms is None:
                envelope = await self._broker.receive(
                    edge_id, EndpointTermsPayload, timeout=self._message_timeout()
                )
                payload = envelope.payload
                if not isinstance(payload, EndpointTermsPayload):
                    raise ChannelInputEscrowError("invalid peer terms payload")
                await self._accept_terms(edge_id, envelope, payload)
                continue
            if record.parent is None:
                envelope = await self._broker.receive(
                    edge_id, ParentPreparationPayload, timeout=self._message_timeout()
                )
                payload = envelope.payload
                if not isinstance(payload, ParentPreparationPayload):
                    raise ChannelInputEscrowError("invalid peer parent payload")
                await self._accept_parent(edge_id, envelope, payload)
                continue
            await self._recover_peer_signing(record)
            return

    async def _accept_maker_initiation(
        self, edge_id: str, envelope: EscrowPeerEnvelope, maker_keys: EscrowPublicKeysPayload
    ) -> None:
        async with self._lock_for(edge_id):
            existing = self._endpoint_store.load(edge_id)
            if existing is not None:
                if (
                    existing.role is not EscrowEndpointRole.PEER
                    or existing.remote_keys != maker_keys
                ):
                    raise ChannelInputEscrowError("conflicting endpoint role or maker keys")
                self._broker.complete(envelope)
                record = existing
            else:
                binding = self._broker.binding(edge_id)
                channel_id = f"{binding.channel_point.txid}:{binding.channel_point.vout}"
                frozen = await self._lnd.freeze_channel_input(edge_id, channel_id)
                snapshot = self.canonical_snapshot(frozen, EscrowEndpointRole.PEER)
                if snapshot.session_id != edge_id:
                    raise ChannelInputEscrowError("LND froze a different peer escrow session")
                local_keys = self._signer.derive_keys(
                    edge_id, EscrowEndpointRole.PEER
                ).public_payload()
                await self._chain.register_frozen_channel(edge_id, snapshot.funding_outpoint)
                record = EndpointEscrowSessionRecord(
                    edge_id=edge_id,
                    role=EscrowEndpointRole.PEER,
                    snapshot=snapshot,
                    frozen_channel_registered=True,
                    local_keys=local_keys,
                    remote_keys=maker_keys,
                )
                self._endpoint_store.save(record)
                self._broker.complete(envelope)
            await self._ensure_peer_offer(record)

    async def _accept_terms(
        self, edge_id: str, envelope: EscrowPeerEnvelope, payload: EndpointTermsPayload
    ) -> None:
        async with self._lock_for(edge_id):
            record = self._require_role(edge_id, EscrowEndpointRole.PEER)
            if record.invoice_proposal is None or record.remote_keys is None:
                raise ChannelInputEscrowError("peer terms arrived before endpoint initialization")
            self._validate_peer_terms(record, payload.terms)
            record = self._persist_exact(record, "terms", payload.terms)
            self._broker.complete(envelope)

    async def _accept_parent(
        self, edge_id: str, envelope: EscrowPeerEnvelope, parent: ParentPreparationPayload
    ) -> None:
        async with self._lock_for(edge_id):
            record = self._require_role(edge_id, EscrowEndpointRole.PEER)
            if record.terms is None or record.remote_keys is None:
                raise ChannelInputEscrowError("peer parent arrived before accepted terms")
            self._validate_parent_against_terms(record, parent)
            if record.parent is not None and record.parent != parent:
                raise ChannelInputEscrowError("conflicting peer parent preparation")
            if record.parent is None:
                record = self._persist(record, parent=parent)
            self._broker.complete(envelope)
            await self._recover_peer_signing(record)

    async def _complete_timeout(
        self, record: EndpointEscrowSessionRecord
    ) -> EndpointEscrowSessionRecord:
        context = self._timeout_context(record)
        local_nonce = self._signer.nonce_payload(context)
        record = self._persist_exact(record, "timeout_local_nonce", local_nonce)
        await self._broker.send(record.edge_id, local_nonce)
        record, remote_nonce = await self._ensure_timeout_remote_nonce(record)
        local_partial = self._signer.sign_timeout(context, remote_nonce)
        record = self._persist_exact(record, "timeout_local_partial", local_partial)
        await self._broker.send(record.edge_id, local_partial)
        record, remote_partial = await self._ensure_timeout_remote_partial(record)
        evidence = self._signer.finalize_timeout(
            context, remote_nonce, local_partial, remote_partial
        )
        return self._persist_exact(record, "timeout_evidence", evidence)

    async def _complete_peer_timeout(
        self, record: EndpointEscrowSessionRecord
    ) -> EndpointEscrowSessionRecord:
        context = self._timeout_context(record)
        record, maker_nonce = await self._ensure_timeout_remote_nonce(record)
        local_nonce = self._signer.nonce_payload(context)
        record = self._persist_exact(record, "timeout_local_nonce", local_nonce)
        await self._broker.send(record.edge_id, local_nonce)
        record, maker_partial = await self._ensure_timeout_remote_partial(record)
        local_partial = self._signer.sign_timeout(context, maker_nonce)
        record = self._persist_exact(record, "timeout_local_partial", local_partial)
        await self._broker.send(record.edge_id, local_partial)
        evidence = self._signer.finalize_timeout(context, maker_nonce, local_partial, maker_partial)
        return self._persist_exact(record, "timeout_evidence", evidence)

    async def _complete_peer_parent(
        self, record: EndpointEscrowSessionRecord
    ) -> EndpointEscrowSessionRecord:
        if record.parent is None or record.timeout_evidence is None:
            raise ChannelInputEscrowError("peer parent signing lacks timeout evidence")
        parent = record.parent
        timeout_evidence = record.timeout_evidence
        if record.parent_local_partial is not None:
            local_nonce = record.parent_local_nonce
            if local_nonce is None:
                raise ChannelInputEscrowError("durable peer partial lacks its local nonce")
            await self._broker.send(record.edge_id, local_nonce)
            await self._broker.send(record.edge_id, record.parent_local_partial)
            record, maker_partial = await self._ensure_parent_remote_partial(record)
            artifact = await self._parent_signer.recover_parent(
                edge_id=record.edge_id,
                funding_outpoint=record.snapshot.funding_outpoint,
                parent=parent,
                timeout_evidence=timeout_evidence,
                remote_partial_payload=maker_partial,
                round_id=local_nonce.round_id,
            )
            return self._persist_exact(
                record, "authorization", self._authorization(record, artifact)
            )
        record, maker_nonce = await self._ensure_parent_remote_nonce(record)
        context = await self._parent_signer.begin_signing(
            edge_id=record.edge_id,
            role=record.role,
            funding_outpoint=record.snapshot.funding_outpoint,
            parent=parent,
            timeout_evidence=timeout_evidence,
            peer_round_id=maker_nonce.round_id,
        )
        local_nonce = self._parent_signer.nonce_payload(context)
        record = self._persist_exact(record, "parent_local_nonce", local_nonce)
        await self._broker.send(record.edge_id, local_nonce)
        record, maker_partial = await self._ensure_parent_remote_partial(record)
        local_partial = await self._parent_signer.sign_parent(context, maker_nonce)
        record = self._persist_exact(record, "parent_local_partial", local_partial)
        await self._broker.send(record.edge_id, local_partial)
        artifact = await self._parent_signer.finalize_parent(context, maker_partial)
        return self._persist_exact(record, "authorization", self._authorization(record, artifact))

    async def _ensure_peer_offer(
        self, record: EndpointEscrowSessionRecord
    ) -> EndpointEscrowSessionRecord:
        if record.role is not EscrowEndpointRole.PEER or record.remote_keys is None:
            raise ChannelInputEscrowError("peer endpoint offer lacks maker public keys")
        self._require_frozen_registration(record)
        await self._broker.send(record.edge_id, record.local_keys)
        if record.invoice_proposal is None:
            local = self._signer.derive_keys(record.edge_id, EscrowEndpointRole.PEER)
            invoice = await self._lnd.create_escrow_invoice(
                1000 * (record.snapshot.peer_claim_sats + self._policy.premium_sats),
                derive_invoice_preimage(local.nonce_seed, edge_id=record.edge_id, role=record.role),
                self._policy.invoice_expiry_seconds,
                self._policy.invoice_min_final_cltv_delta,
            )
            record = self._persist(record, invoice_proposal=self._proposal(invoice))
        proposal = record.invoice_proposal
        if proposal is None:
            raise ChannelInputEscrowError("peer invoice proposal was not persisted")
        await self._broker.send(record.edge_id, proposal)
        return record

    async def _recover_peer_signing(self, record: EndpointEscrowSessionRecord) -> None:
        if record.parent is None or record.terms is None:
            raise ChannelInputEscrowError("peer signing recovery lacks parent terms")
        await self._parent_signer.prepare_parent(
            record.edge_id, record.snapshot.funding_outpoint, record.parent
        )
        if record.timeout_evidence is None:
            record = await self._complete_peer_timeout(record)
        record = await self._reconcile_lnd(record)
        if record.authorization is None:
            await self._complete_peer_parent(record)

    async def _reconcile_lnd(
        self, record: EndpointEscrowSessionRecord
    ) -> EndpointEscrowSessionRecord:
        """Repair endpoint journal gaps from validated durable LND evidence."""
        status = await self._lnd.channel_input_escrow_status(
            record.edge_id, record.snapshot.funding_outpoint
        )
        if status.stage is LndEscrowStage.LOCKED:
            if record.parent is None:
                return record
            await self._parent_signer.prepare_parent(
                record.edge_id, record.snapshot.funding_outpoint, record.parent
            )
            if record.parent_local_partial is not None or record.authorization is not None:
                raise ChannelInputEscrowError("LND signing state is behind endpoint authority")
            return record

        if record.parent is None or status.parent_txid != record.parent.parent_txid:
            raise ChannelInputEscrowError("LND prepared parent conflicts with endpoint authority")
        parent = record.parent
        if status.stage is LndEscrowStage.PARENT_PREPARED:
            if record.parent_local_partial is not None or record.authorization is not None:
                raise ChannelInputEscrowError("LND signing state is behind endpoint authority")
            if status.active_attempt is not None and record.parent_local_nonce is not None:
                expected_nonce = bytes.fromhex(record.parent_local_nonce.public_nonce)
                if status.active_attempt.public_nonce != expected_nonce:
                    raise ChannelInputEscrowError(
                        "active LND signing attempt conflicts with endpoint authority"
                    )
            return record

        if record.timeout_evidence is None or record.parent_remote_nonce is None:
            raise ChannelInputEscrowError("LND released a signature without endpoint prerequisites")
        local_nonce = status.durable_local_public_nonce
        remote_nonce = status.durable_remote_public_nonce
        local_partial = status.durable_local_partial_signature
        if local_nonce is None or remote_nonce is None or local_partial is None:
            raise ChannelInputEscrowError("LND durable signing evidence is incomplete")
        if remote_nonce.hex() != record.parent_remote_nonce.public_nonce:
            raise ChannelInputEscrowError("LND remote nonce conflicts with endpoint authority")
        round_id = (
            self._parent_signer.maker_round_id(record.edge_id, parent.parent_txid, local_nonce)
            if record.role is EscrowEndpointRole.MAKER
            else record.parent_remote_nonce.round_id
        )
        record = self._persist_exact(
            record,
            "parent_local_nonce",
            ParentMuSig2NoncePayload(
                round_id=round_id,
                parent_txid=parent.parent_txid,
                public_nonce=local_nonce.hex(),
            ),
        )
        record = self._persist_exact(
            record,
            "parent_local_partial",
            ParentMuSig2PartialSignaturePayload(
                round_id=round_id,
                parent_txid=parent.parent_txid,
                partial_signature=local_partial.hex(),
            ),
        )

        if status.stage is LndEscrowStage.PARENT_SIGNED:
            if (
                status.active_attempt is not None
                and status.active_attempt.public_nonce != local_nonce
            ):
                raise ChannelInputEscrowError(
                    "active LND signing attempt conflicts with durable evidence"
                )
            return record

        artifact = status.finalized_parent
        if artifact is None or record.parent_remote_partial is None:
            raise ChannelInputEscrowError("finalized LND parent lacks endpoint signing evidence")
        return self._persist_exact(record, "authorization", self._authorization(record, artifact))

    async def _ensure_remote_keys(
        self, record: EndpointEscrowSessionRecord, timeout: float | None
    ) -> EndpointEscrowSessionRecord:
        if record.remote_keys is not None:
            self._complete_persisted_receipt(record.edge_id, record.remote_keys)
            return record
        envelope = await self._broker.receive(
            record.edge_id, EscrowPublicKeysPayload, timeout=self._message_timeout(timeout)
        )
        payload = envelope.payload
        if (
            not isinstance(payload, EscrowPublicKeysPayload)
            or payload.role is not EscrowProtocolRole.PEER
        ):
            raise ChannelInputEscrowError("peer did not provide peer escrow keys")
        record = self._persist_remote_keys(record, payload)
        self._broker.complete(envelope)
        return record

    async def _ensure_invoice_proposal(
        self, record: EndpointEscrowSessionRecord, timeout: float | None
    ) -> tuple[EndpointEscrowSessionRecord, EscrowInvoiceProposalPayload]:
        if record.invoice_proposal is not None:
            self._complete_persisted_receipt(record.edge_id, record.invoice_proposal)
            return record, record.invoice_proposal
        envelope = await self._broker.receive(
            record.edge_id, EscrowInvoiceProposalPayload, timeout=self._message_timeout(timeout)
        )
        proposal = envelope.payload
        if not isinstance(proposal, EscrowInvoiceProposalPayload):
            raise ChannelInputEscrowError("peer did not provide an invoice proposal")
        record = self._persist(record, invoice_proposal=proposal)
        self._broker.complete(envelope)
        return record, proposal

    async def _ensure_timeout_remote_nonce(
        self, record: EndpointEscrowSessionRecord
    ) -> tuple[EndpointEscrowSessionRecord, TimeoutMuSig2NoncePayload]:
        if record.timeout_remote_nonce is not None:
            self._complete_persisted_receipt(record.edge_id, record.timeout_remote_nonce)
            return record, record.timeout_remote_nonce
        envelope = await self._broker.receive(
            record.edge_id, TimeoutMuSig2NoncePayload, timeout=self._message_timeout()
        )
        payload = envelope.payload
        if not isinstance(payload, TimeoutMuSig2NoncePayload):
            raise ChannelInputEscrowError("invalid peer timeout nonce")
        record = self._persist(record, timeout_remote_nonce=payload)
        self._broker.complete(envelope)
        return record, payload

    async def _ensure_timeout_remote_partial(
        self, record: EndpointEscrowSessionRecord
    ) -> tuple[EndpointEscrowSessionRecord, TimeoutMuSig2PartialSignaturePayload]:
        if record.timeout_remote_partial is not None:
            self._complete_persisted_receipt(record.edge_id, record.timeout_remote_partial)
            return record, record.timeout_remote_partial
        envelope = await self._broker.receive(
            record.edge_id, TimeoutMuSig2PartialSignaturePayload, timeout=self._message_timeout()
        )
        payload = envelope.payload
        if not isinstance(payload, TimeoutMuSig2PartialSignaturePayload):
            raise ChannelInputEscrowError("invalid peer timeout partial signature")
        record = self._persist(record, timeout_remote_partial=payload)
        self._broker.complete(envelope)
        return record, payload

    async def _ensure_parent_remote_nonce(
        self, record: EndpointEscrowSessionRecord
    ) -> tuple[EndpointEscrowSessionRecord, ParentMuSig2NoncePayload]:
        if record.parent_remote_nonce is not None:
            self._complete_persisted_receipt(record.edge_id, record.parent_remote_nonce)
            return record, record.parent_remote_nonce
        envelope = await self._broker.receive(
            record.edge_id, ParentMuSig2NoncePayload, timeout=self._message_timeout()
        )
        payload = envelope.payload
        if not isinstance(payload, ParentMuSig2NoncePayload):
            raise ChannelInputEscrowError("invalid peer parent nonce")
        record = self._persist(record, parent_remote_nonce=payload)
        self._broker.complete(envelope)
        return record, payload

    async def _ensure_parent_remote_partial(
        self, record: EndpointEscrowSessionRecord
    ) -> tuple[EndpointEscrowSessionRecord, ParentMuSig2PartialSignaturePayload]:
        if record.parent_remote_partial is not None:
            self._complete_persisted_receipt(record.edge_id, record.parent_remote_partial)
            return record, record.parent_remote_partial
        envelope = await self._broker.receive(
            record.edge_id, ParentMuSig2PartialSignaturePayload, timeout=self._message_timeout()
        )
        payload = envelope.payload
        if not isinstance(payload, ParentMuSig2PartialSignaturePayload):
            raise ChannelInputEscrowError("invalid peer parent partial signature")
        record = self._persist(record, parent_remote_partial=payload)
        self._broker.complete(envelope)
        return record, payload

    async def _build_terms(
        self,
        record: EndpointEscrowSessionRecord,
        proposal: EscrowInvoiceProposalPayload,
        decoded: LndEscrowInvoice,
    ) -> ChannelInputEscrowTerms:
        height = await self._height_source.block_height()
        if isinstance(height, bool) or not isinstance(height, int) or height < 0:
            raise ChannelInputEscrowError("height source returned an invalid height")
        return ChannelInputEscrowTerms(
            snapshot=record.snapshot,
            authorizer_pubkey=record.local_keys.escrow_pubkey,
            payment_hash=decoded.payment_hash,
            invoice=decoded.invoice,
            invoice_payee_pubkey=decoded.payee_pubkey,
            invoice_payment_secret=decoded.payment_secret,
            invoice_amount_msat=decoded.amount_msat,
            invoice_expiry_unix=decoded.expiry_unix,
            invoice_min_final_cltv_delta=decoded.min_final_cltv_delta,
            invoice_verified=True,
            premium_sats=self._policy.premium_sats,
            timeout_compensation_sats=self._policy.timeout_compensation_sats,
            escrow_value_sats=self._policy.escrow_value_sats,
            timeout_fee_sats=self._policy.timeout_fee_sats,
            timeout_height=height + self._policy.timeout_delta_blocks,
            peer_settle_deadline=height + self._policy.peer_settle_delta_blocks,
            success_broadcast_deadline=height + self._policy.success_broadcast_delta_blocks,
            parent_confirmation_depth=self._policy.parent_confirmation_depth,
            success_confirmation_depth=self._policy.success_confirmation_depth,
            success_confirmation_budget_blocks=self._policy.success_confirmation_budget_blocks,
            reorg_margin_blocks=self._policy.reorg_margin_blocks,
            safety_delta_blocks=self._policy.safety_delta_blocks,
            preimage_budget_blocks=self._policy.preimage_budget_blocks,
        )

    def _validate_peer_terms(
        self, record: EndpointEscrowSessionRecord, terms: ChannelInputEscrowTerms
    ) -> None:
        proposal = record.invoice_proposal
        if (
            proposal is None
            or record.remote_keys is None
            or terms.snapshot != record.snapshot
            or terms.authorizer_pubkey != record.remote_keys.escrow_pubkey
        ):
            raise ChannelInputEscrowError("peer terms do not match canonical frozen snapshot")
        if (
            terms.invoice != proposal.invoice
            or terms.payment_hash != proposal.payment_hash
            or terms.invoice_payee_pubkey != proposal.payee_pubkey
            or terms.invoice_payment_secret != proposal.payment_secret
            or terms.invoice_amount_msat != proposal.amount_msat
            or terms.invoice_expiry_unix != proposal.expiry_unix
            or terms.invoice_min_final_cltv_delta != proposal.min_final_cltv_delta
            or not terms.invoice_verified
        ):
            raise ChannelInputEscrowError("peer terms do not match the verified invoice")
        expected = self._policy
        if (
            terms.premium_sats != expected.premium_sats
            or terms.timeout_compensation_sats != expected.timeout_compensation_sats
            or terms.escrow_value_sats != expected.escrow_value_sats
            or terms.timeout_fee_sats != expected.timeout_fee_sats
            or terms.parent_confirmation_depth != expected.parent_confirmation_depth
            or terms.success_confirmation_depth != expected.success_confirmation_depth
            or terms.success_confirmation_budget_blocks
            != expected.success_confirmation_budget_blocks
            or terms.reorg_margin_blocks != expected.reorg_margin_blocks
            or terms.safety_delta_blocks != expected.safety_delta_blocks
            or terms.preimage_budget_blocks != expected.preimage_budget_blocks
        ):
            raise ChannelInputEscrowError("peer terms conflict with endpoint policy")
        if terms.timeout_height - terms.peer_settle_deadline != (
            expected.timeout_delta_blocks - expected.peer_settle_delta_blocks
        ) or terms.timeout_height - terms.success_broadcast_deadline != (
            expected.timeout_delta_blocks - expected.success_broadcast_delta_blocks
        ):
            raise ChannelInputEscrowError("peer terms deadlines conflict with endpoint policy")

    def _timeout_context(self, record: EndpointEscrowSessionRecord) -> Any:
        if record.terms is None or record.parent is None or record.remote_keys is None:
            raise ChannelInputEscrowError("timeout signing lacks durable endpoint artifacts")
        local = self._signer.derive_keys(record.edge_id, record.role)
        maker_keys = (
            record.local_keys if record.role is EscrowEndpointRole.MAKER else record.remote_keys
        )
        peer_keys = (
            record.remote_keys if record.role is EscrowEndpointRole.MAKER else record.local_keys
        )
        return self._signer.build_timeout_context(
            local_keys=local,
            maker_keys=maker_keys,
            peer_keys=peer_keys,
            terms=record.terms,
            parent=record.parent,
        )

    def _persist_remote_keys(
        self, record: EndpointEscrowSessionRecord, keys: EscrowPublicKeysPayload
    ) -> EndpointEscrowSessionRecord:
        if record.remote_keys is not None and record.remote_keys != keys:
            raise ChannelInputEscrowError("conflicting remote escrow public keys")
        return record if record.remote_keys == keys else self._persist(record, remote_keys=keys)

    def _persist(
        self, record: EndpointEscrowSessionRecord, **updates: object
    ) -> EndpointEscrowSessionRecord:
        candidate = record.advance(**updates)
        self._endpoint_store.save(candidate, expected_revision=record.revision)
        return candidate

    def _persist_exact(
        self, record: EndpointEscrowSessionRecord, field: str, value: object
    ) -> EndpointEscrowSessionRecord:
        existing = getattr(record, field)
        if existing is not None:
            if existing != value:
                raise ChannelInputEscrowError(f"conflicting durable endpoint {field}")
            return record
        return self._persist(record, **{field: value})

    def _message_timeout(self, requested: float | None = None) -> float:
        if requested is None:
            return self._policy.peer_message_timeout_seconds
        if isinstance(requested, bool) or not isinstance(requested, (int, float)) or requested <= 0:
            raise ChannelInputEscrowError("peer message timeout must be positive")
        return float(requested)

    def _require(self, edge_id: str) -> EndpointEscrowSessionRecord:
        record = self._endpoint_store.load(edge_id)
        if record is None:
            raise ChannelInputEscrowError("endpoint escrow session was not initialized")
        return record

    def _require_role(self, edge_id: str, role: EscrowEndpointRole) -> EndpointEscrowSessionRecord:
        record = self._require(edge_id)
        if record.role is not role:
            raise ChannelInputEscrowError("endpoint escrow role conflict")
        return record

    @staticmethod
    def _require_terms(record: EndpointEscrowSessionRecord, terms: ChannelInputEscrowTerms) -> None:
        if record.terms != terms:
            raise ChannelInputEscrowError("escrow terms are not the durable endpoint terms")

    @staticmethod
    def _require_frozen_registration(record: EndpointEscrowSessionRecord) -> None:
        if not record.frozen_channel_registered:
            raise ChannelInputEscrowError("frozen channel monitor registration is not durable")

    def _lock_for(self, edge_id: str) -> asyncio.Lock:
        return self._locks.setdefault(edge_id, asyncio.Lock())

    async def _cancel_service(self, edge_id: str) -> None:
        task = self._service_tasks.pop(edge_id, None)
        if task is None or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _complete_persisted_receipt(self, edge_id: str, payload: object) -> None:
        for envelope in self._broker.pending(edge_id):
            if envelope.payload == payload:
                self._broker.complete(envelope)

    @staticmethod
    def _edge_id(edge_id: str) -> str:
        if not isinstance(edge_id, str) or len(edge_id) != 64:
            raise ChannelInputEscrowError("edge ID must be a 32-byte hexadecimal string")
        try:
            value = bytes.fromhex(edge_id)
        except ValueError as exc:
            raise ChannelInputEscrowError("edge ID must be a 32-byte hexadecimal string") from exc
        if len(value) != 32:
            raise ChannelInputEscrowError("edge ID must be a 32-byte hexadecimal string")
        return value.hex()

    @staticmethod
    def _proposal(invoice: LndEscrowInvoice) -> EscrowInvoiceProposalPayload:
        return EscrowInvoiceProposalPayload(
            role=EscrowProtocolRole.PEER,
            invoice=invoice.invoice,
            payment_hash=invoice.payment_hash,
            payee_pubkey=invoice.payee_pubkey,
            payment_secret=invoice.payment_secret,
            amount_msat=invoice.amount_msat,
            expiry_unix=invoice.expiry_unix,
            min_final_cltv_delta=invoice.min_final_cltv_delta,
        )

    @staticmethod
    def _require_invoice_matches(
        proposal: EscrowInvoiceProposalPayload, decoded: LndEscrowInvoice
    ) -> None:
        if (
            proposal.invoice.lower() != decoded.invoice
            or proposal.payment_hash != decoded.payment_hash
            or proposal.payee_pubkey != decoded.payee_pubkey
            or proposal.payment_secret != decoded.payment_secret
            or proposal.amount_msat != decoded.amount_msat
            or proposal.expiry_unix != decoded.expiry_unix
            or proposal.min_final_cltv_delta != decoded.min_final_cltv_delta
        ):
            raise ChannelInputEscrowError("peer invoice proposal does not match decoded invoice")

    @staticmethod
    def _parent_payload(evidence: EscrowParentEvidence) -> ParentPreparationPayload:
        return ParentPreparationPayload(
            round_id=hashlib.sha256(
                b"joinmarket-ng/channel-input-escrow/v1/parent\x00"
                + bytes.fromhex(evidence.parent_txid)
            ).hexdigest(),
            parent_tx_hex=evidence.parent_tx_hex,
            parent_txid=evidence.parent_txid,
            channel_input_index=evidence.channel_input_index,
            ordered_prevouts=tuple(
                ParentPrevoutPayload(value_sats=item.value_sats, scriptpubkey=item.scriptpubkey)
                for item in evidence.ordered_prevouts
            ),
            escrow_output_index=evidence.escrow_outpoint.vout,
            escrow_value_sats=evidence.escrow_value_sats,
            escrow_scriptpubkey=evidence.escrow_scriptpubkey,
        )

    @staticmethod
    def _validate_parent_against_terms(
        record: EndpointEscrowSessionRecord, parent: ParentPreparationPayload
    ) -> None:
        terms = record.terms
        if terms is None or record.remote_keys is None:
            raise ChannelInputEscrowError("parent preparation lacks canonical endpoint terms")
        maker_keys = (
            record.remote_keys if record.role is EscrowEndpointRole.PEER else record.local_keys
        )
        peer_keys = (
            record.local_keys if record.role is EscrowEndpointRole.PEER else record.remote_keys
        )
        tree = ChannelInputEscrowTree(
            maker_escrow_pubkey=bytes.fromhex(maker_keys.escrow_pubkey),
            peer_escrow_pubkey=bytes.fromhex(peer_keys.escrow_pubkey),
            maker_claim_pubkey=bytes.fromhex(maker_keys.maker_claim_pubkey or ""),
            payment_hash=bytes.fromhex(terms.payment_hash),
        )
        if (
            parent.escrow_value_sats != terms.escrow_value_sats
            or parent.channel_input_index >= len(parent.ordered_prevouts)
            or parent.ordered_prevouts[parent.channel_input_index].value_sats
            != terms.snapshot.capacity_sats
            or parent.ordered_prevouts[parent.channel_input_index].scriptpubkey
            != terms.snapshot.funding_scriptpubkey
            or parent.escrow_scriptpubkey != tree.funding_output_script().hex()
        ):
            raise ChannelInputEscrowError("parent preparation does not match peer escrow terms")

    @staticmethod
    def _authorization(record: EndpointEscrowSessionRecord, artifact: Any) -> ParentAuthorization:
        if record.parent is None or record.timeout_evidence is None:
            raise ChannelInputEscrowError("parent authorization lacks durable timeout evidence")
        return ParentAuthorization(
            session_id=record.edge_id,
            parent_txid=record.parent.parent_txid,
            prepared_parent_tx_hex=record.parent.parent_tx_hex,
            timeout_txid=record.timeout_evidence.timeout_txid,
            channel_funding_outpoint=record.snapshot.funding_outpoint,
            channel_input_index=record.parent.channel_input_index,
            channel_input_witness=artifact.channel_input_witness,
            sighash="default",
            signature_verified=True,
            channel_witness_txid_verified=True,
            commits_all_inputs=True,
            commits_all_outputs=True,
        )

    async def _send_abort(self, edge_id: str, payload: object) -> None:
        round_id = getattr(payload, "round_id", "00" * 32)
        if not isinstance(round_id, str) or len(round_id) != 64:
            round_id = "00" * 32
        with contextlib.suppress(Exception):
            await self._broker.send(
                edge_id,
                EndpointAbortPayload(round_id=round_id, reason=EndpointAbortReason.SIGNING_FAILED),
            )

    # Chain and payment capabilities intentionally delegate to the specialized adapters.
    async def broadcast_parent(self, request: ParentBroadcastRequest) -> EscrowParentBroadcast:
        return await self._chain.broadcast_parent(request)

    async def observe_escrow_chain(
        self, request: ChainObservationRequest
    ) -> EscrowChainObservation:
        return await self._chain.observe_escrow_chain(request)

    async def send_single_part_payment(
        self, request: SinglePartPaymentRequest
    ) -> SinglePartPaymentResult:
        return await self._payment.send_single_part_payment(request)

    async def observe_escrow_payment(
        self, request: PaymentObservationRequest
    ) -> SinglePartPaymentResult:
        return await self._payment.observe_escrow_payment(request)

    async def build_broadcast_success_spend(
        self, request: SuccessSpendRequest
    ) -> EscrowSpendResult:
        return await self._chain.build_broadcast_success_spend(request)

    async def broadcast_timeout_recovery(self, request: RecoveryRequest) -> EscrowTimeoutBroadcast:
        return await self._chain.broadcast_timeout_recovery(request)

    async def fee_bump_success_spend(self, request: RecoveryRequest) -> EscrowSpendResult:
        return await self._chain.fee_bump_success_spend(request)

    async def rebroadcast_recovery_artifacts(
        self, request: RecoveryRequest
    ) -> RecoveryBroadcastResult:
        return await self._chain.rebroadcast_recovery_artifacts(request)

    async def resume_durable_sessions(self) -> list[ChannelInputEscrowRecord]:
        """Reconcile LND only against local endpoint authority; never create a new round."""
        if self._coordinator_store is None:
            return []
        endpoint_records = {record.edge_id: record for record in self._endpoint_store.list()}
        records: list[ChannelInputEscrowRecord] = []
        for record in self._coordinator_store.list():
            endpoint = endpoint_records.get(record.session_id)
            if endpoint is None or endpoint.role is not EscrowEndpointRole.MAKER:
                continue
            endpoint = await self._reconcile_lnd(endpoint)
            if endpoint.terms != record.terms:
                raise ChannelInputEscrowError("coordinator terms exceed endpoint authority")
            if (
                record.prepared is not None
                and endpoint.timeout_evidence != record.prepared.timeout_evidence
            ):
                raise ChannelInputEscrowError(
                    "coordinator timeout evidence exceeds endpoint authority"
                )
            if record.authorization is not None and endpoint.authorization != record.authorization:
                raise ChannelInputEscrowError(
                    "coordinator authorization exceeds endpoint authority"
                )
            records.append(record)
        return records


# Short names retained for callers that prefer the component-oriented spelling.
MakerEscrowRuntime = ChannelInputEscrowRuntime
EscrowRuntimePolicy = EndpointEscrowRuntimePolicy
