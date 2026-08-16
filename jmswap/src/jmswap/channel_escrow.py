"""Typed endpoint boundary for confirmation-gated channel-input escrow.

Invoice, claim, timeout, and balance artifacts must never be exposed to the
taker or aggregate logs. This module deliberately carries those artifacts only
between the maker's escrow coordinator and its Lightning and chain backends.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, cast, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jmswap.channel_funding import (
    ChannelInputEscrowCapability,
    require_channel_input_escrow_capabilities,
)
from jmswap.errors import ChannelInputEscrowError, LightningError
from jmswap.lightning import PaymentStatus

_MAX_BLOCK_HEIGHT_LOCKTIME = 500_000_000
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_FROZEN_MODEL_CONFIG = ConfigDict(
    extra="forbid", frozen=True, strict=True, ser_json_bytes="hex", val_json_bytes="hex"
)
_MUTABLE_MODEL_CONFIG = ConfigDict(
    extra="forbid", strict=True, ser_json_bytes="hex", val_json_bytes="hex"
)


def _validate_hex(value: str, *, byte_length: int | None = None, name: str) -> str:
    if not value or len(value) % 2 or any(character not in _HEX_DIGITS for character in value):
        raise ValueError(f"{name} must be non-empty hexadecimal")
    if byte_length is not None and len(value) != byte_length * 2:
        raise ValueError(f"{name} must be exactly {byte_length} bytes")
    return value


class EscrowOutpoint(BaseModel):
    """An on-chain outpoint controlled by the channel-input escrow."""

    model_config = _FROZEN_MODEL_CONFIG

    txid: str
    vout: int = Field(ge=0, le=0xFFFFFFFF)

    @field_validator("txid")
    @classmethod
    def validate_txid(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="txid")


class ChannelSnapshot(BaseModel):
    """A quiescent, fully accounted channel state suitable for consumption."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    funding_outpoint: EscrowOutpoint
    capacity_sats: int = Field(gt=0)
    maker_claim_sats: int = Field(ge=0)
    peer_claim_sats: int = Field(ge=0)
    quiescent: bool
    active_htlcs: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_settlement_state(self) -> ChannelSnapshot:
        if not self.quiescent:
            raise ValueError("channel snapshot must be quiescent")
        if self.active_htlcs != 0:
            raise ValueError("channel snapshot must have no active HTLCs")
        if self.capacity_sats != self.maker_claim_sats + self.peer_claim_sats:
            raise ValueError("channel claims must exactly conserve capacity")
        return self


class ChannelInputEscrowTerms(BaseModel):
    """Economic and height-budget constraints for one channel-input escrow."""

    model_config = _FROZEN_MODEL_CONFIG

    snapshot: ChannelSnapshot
    authorizer_pubkey: str
    payment_hash: str
    invoice: str = Field(min_length=1)
    invoice_payee_pubkey: str
    invoice_payment_secret: str
    invoice_amount_msat: int = Field(gt=0)
    invoice_expiry_unix: int = Field(gt=0)
    invoice_min_final_cltv_delta: int = Field(ge=1)
    invoice_verified: bool
    premium_sats: int = Field(ge=0)
    timeout_compensation_sats: int = Field(ge=0)
    escrow_value_sats: int = Field(gt=0)
    timeout_fee_sats: int = Field(ge=0)
    timeout_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    peer_settle_deadline: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    success_broadcast_deadline: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    parent_confirmation_depth: int = Field(ge=1)
    success_confirmation_depth: int = Field(ge=1)
    success_confirmation_budget_blocks: int = Field(ge=1)
    reorg_margin_blocks: int = Field(ge=0)
    safety_delta_blocks: int = Field(ge=1)
    preimage_budget_blocks: int = Field(ge=0)

    @field_validator("payment_hash", "invoice_payment_secret")
    @classmethod
    def validate_payment_hash(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="payment hash or secret")

    @field_validator("invoice_payee_pubkey", "authorizer_pubkey")
    @classmethod
    def validate_invoice_payee(cls, value: str) -> str:
        _validate_hex(value, byte_length=33, name="escrow endpoint public key")
        if not value.startswith(("02", "03")):
            raise ValueError("escrow endpoint public key must be compressed")
        return value

    @model_validator(mode="after")
    def validate_economics_and_deadlines(self) -> ChannelInputEscrowTerms:
        if not self.invoice_verified:
            raise ValueError("signed invoice fields must be independently verified")
        peer_payment_sats = self.snapshot.peer_claim_sats + self.premium_sats
        if self.invoice_amount_msat != 1000 * peer_payment_sats:
            raise ValueError("invoice amount must equal 1000 * (peer claim + premium)")
        if self.snapshot.capacity_sats < self.escrow_value_sats:
            raise ValueError("channel capacity must cover the escrow value")
        timeout_minimum = (
            self.snapshot.peer_claim_sats + self.timeout_compensation_sats + self.timeout_fee_sats
        )
        if self.escrow_value_sats <= timeout_minimum:
            raise ValueError("escrow value must exceed peer claim, compensation, and timeout fee")
        if self.peer_settle_deadline >= self.success_broadcast_deadline:
            raise ValueError("peer settlement deadline must precede the success broadcast deadline")
        if (
            self.success_broadcast_deadline
            + self.success_confirmation_budget_blocks
            + self.reorg_margin_blocks
            >= self.timeout_height
        ):
            raise ValueError("broadcast deadline and confirmation budget must precede timeout")
        return self


class EscrowParentEvidence(BaseModel):
    """Maker-local evidence that the exact escrow parent is safe to authorize."""

    model_config = _FROZEN_MODEL_CONFIG

    parent_tx_hex: str
    parent_txid: str
    channel_funding_outpoint: EscrowOutpoint
    channel_input_index: int = Field(ge=0, le=0xFFFFFFFF)
    escrow_outpoint: EscrowOutpoint
    escrow_value_sats: int = Field(gt=0)
    escrow_scriptpubkey: str
    all_inputs_nonmalleable: bool
    exact_transaction_verified: bool

    @field_validator("parent_tx_hex", "escrow_scriptpubkey")
    @classmethod
    def validate_hex_artifact(cls, value: str) -> str:
        return _validate_hex(value, name="transaction or script artifact")

    @field_validator("parent_txid")
    @classmethod
    def validate_parent_txid(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="parent_txid")

    @model_validator(mode="after")
    def validate_independent_checks(self) -> EscrowParentEvidence:
        if not self.all_inputs_nonmalleable:
            raise ValueError("all parent inputs must be nonmalleable")
        if not self.exact_transaction_verified:
            raise ValueError("exact parent transaction must be independently verified")
        if self.escrow_outpoint.txid != self.parent_txid:
            raise ValueError("escrow outpoint must belong to the parent transaction")
        return self


class EscrowTimeoutEvidence(BaseModel):
    """Maker-local evidence for the fully signed timeout recovery transaction."""

    model_config = _FROZEN_MODEL_CONFIG

    timeout_tx_hex: str
    timeout_txid: str
    spent_escrow_outpoint: EscrowOutpoint
    locktime: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    maker_output_sats: int = Field(ge=0)
    peer_output_sats: int = Field(ge=0)
    fee_sats: int = Field(ge=0)
    fully_signed: bool
    signature_verified: bool

    @field_validator("timeout_tx_hex")
    @classmethod
    def validate_timeout_tx_hex(cls, value: str) -> str:
        return _validate_hex(value, name="timeout_tx_hex")

    @field_validator("timeout_txid")
    @classmethod
    def validate_timeout_txid(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="timeout_txid")

    @model_validator(mode="after")
    def validate_timeout_signatures(self) -> EscrowTimeoutEvidence:
        if not self.fully_signed:
            raise ValueError("timeout transaction must be fully signed")
        if not self.signature_verified:
            raise ValueError("timeout transaction signature must be verified")
        return self


class PreparedChannelInputEscrow(BaseModel):
    """Terms paired with exact parent and timeout artifacts ready for authorization."""

    model_config = _FROZEN_MODEL_CONFIG

    terms: ChannelInputEscrowTerms
    parent_evidence: EscrowParentEvidence
    timeout_evidence: EscrowTimeoutEvidence

    @model_validator(mode="after")
    def validate_prepared_escrow(self) -> PreparedChannelInputEscrow:
        if not self.parent_evidence.all_inputs_nonmalleable:
            raise ValueError("all parent inputs must be nonmalleable")
        if not self.parent_evidence.exact_transaction_verified:
            raise ValueError("exact parent transaction must be independently verified")
        if not self.timeout_evidence.fully_signed:
            raise ValueError("timeout transaction must be fully signed")
        if not self.timeout_evidence.signature_verified:
            raise ValueError("timeout transaction signature must be verified")
        if self.parent_evidence.escrow_value_sats != self.terms.escrow_value_sats:
            raise ValueError("parent escrow value must match escrow terms")
        if self.parent_evidence.channel_funding_outpoint != self.terms.snapshot.funding_outpoint:
            raise ValueError("parent must consume the frozen channel funding outpoint")
        if self.parent_evidence.escrow_outpoint != self.timeout_evidence.spent_escrow_outpoint:
            raise ValueError("timeout must spend the exact parent escrow outpoint")
        if self.timeout_evidence.locktime != self.terms.timeout_height:
            raise ValueError("timeout locktime must match escrow timeout height")
        expected_peer = self.terms.snapshot.peer_claim_sats + self.terms.timeout_compensation_sats
        expected_maker = (
            self.terms.escrow_value_sats
            - self.terms.snapshot.peer_claim_sats
            - self.terms.timeout_compensation_sats
            - self.terms.timeout_fee_sats
        )
        if self.timeout_evidence.peer_output_sats != expected_peer:
            raise ValueError("timeout peer output does not match peer claim and compensation")
        if self.timeout_evidence.maker_output_sats != expected_maker:
            raise ValueError("timeout maker output does not match escrow equation")
        if self.timeout_evidence.fee_sats != self.terms.timeout_fee_sats:
            raise ValueError("timeout fee must match escrow terms")
        return self


class EscrowRouteEvidence(BaseModel):
    """A single-part Lightning route and its relevant CLTV deadline evidence."""

    model_config = _FROZEN_MODEL_CONFIG

    part_count: int = Field(ge=1)
    route_channel_ids: tuple[str, ...] = Field(min_length=1)
    final_expiry_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    outgoing_expiry_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)

    @field_validator("route_channel_ids")
    @classmethod
    def validate_route_channel_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not channel_id for channel_id in value):
            raise ValueError("route channel IDs must be non-empty")
        return value

    def validate_against(self, terms: ChannelInputEscrowTerms) -> None:
        """Raise unless this route leaves enough independent on-chain time."""
        if self.part_count != 1:
            raise ChannelInputEscrowError("escrow payment must use exactly one route part")
        if terms.snapshot.channel_id in self.route_channel_ids:
            raise ChannelInputEscrowError("escrow route must exclude the consumed channel")
        if not terms.peer_settle_deadline < self.final_expiry_height <= self.outgoing_expiry_height:
            raise ChannelInputEscrowError("route expiry heights are not ordered")
        if terms.timeout_height - self.outgoing_expiry_height < terms.safety_delta_blocks:
            raise ChannelInputEscrowError("route leaves insufficient timeout safety delta")
        if (
            self.outgoing_expiry_height + terms.preimage_budget_blocks
            > terms.success_broadcast_deadline
        ):
            raise ChannelInputEscrowError("route leaves insufficient preimage broadcast budget")


class ParentAuthorization(BaseModel):
    """Authorization for the exact parent after timeout recovery is secured."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    parent_txid: str
    prepared_parent_tx_hex: str
    signed_parent_tx_hex: str
    timeout_txid: str
    channel_funding_outpoint: EscrowOutpoint
    channel_input_index: int = Field(ge=0, le=0xFFFFFFFF)
    signer_pubkey: str
    signature: bytes = Field(min_length=1)
    sighash: Literal["all", "default"]
    parent_fully_signed: bool
    signed_parent_txid_verified: bool
    signature_verified: bool
    commits_all_inputs: bool
    commits_all_outputs: bool

    @field_validator("parent_txid", "timeout_txid")
    @classmethod
    def validate_authorized_txid(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="authorized transaction ID")

    @field_validator("prepared_parent_tx_hex", "signed_parent_tx_hex")
    @classmethod
    def validate_authorized_parent_hex(cls, value: str) -> str:
        return _validate_hex(value, name="authorized parent transaction")

    @field_validator("signer_pubkey")
    @classmethod
    def validate_signer_pubkey(cls, value: str) -> str:
        _validate_hex(value, byte_length=33, name="authorization signer public key")
        if not value.startswith(("02", "03")):
            raise ValueError("authorization signer public key must be compressed")
        return value

    @model_validator(mode="after")
    def validate_authorization_evidence(self) -> ParentAuthorization:
        if not self.parent_fully_signed:
            raise ValueError("parent transaction must be fully signed before release")
        if not self.signed_parent_txid_verified:
            raise ValueError("signed parent transaction ID must be independently verified")
        if not self.signature_verified:
            raise ValueError("parent authorization signature must be independently verified")
        if not self.commits_all_inputs or not self.commits_all_outputs:
            raise ValueError("parent authorization must commit to every input and output")
        return self


class EscrowParentBroadcast(BaseModel):
    """Evidence that the exact fully authorized parent was broadcast."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    parent_txid: str
    broadcast_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)

    @field_validator("parent_txid")
    @classmethod
    def validate_broadcast_parent_txid(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="broadcast parent transaction ID")


class EscrowChainObservation(BaseModel):
    """Chain state observed by the maker's escrow monitor."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    parent_txid: str
    parent_confirmations: int = Field(ge=0)
    in_best_chain: bool
    conflict_txid: str | None = None
    conflict_confirmations: int | None = Field(default=None, ge=0)
    conflicting_channel_outpoint: EscrowOutpoint | None = None
    conflict_spends_channel: bool | None = None
    success_txid: str | None = None
    success_confirmations: int | None = Field(default=None, ge=0)
    timeout_txid: str | None = None
    timeout_confirmations: int | None = Field(default=None, ge=0)
    observed_height: int = Field(ge=0)

    @field_validator("parent_txid", "conflict_txid", "success_txid", "timeout_txid")
    @classmethod
    def validate_conflict_txid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_hex(value, byte_length=32, name="conflict_txid")

    @model_validator(mode="after")
    def validate_chain_coherence(self) -> EscrowChainObservation:
        if not self.in_best_chain and self.parent_confirmations != 0:
            raise ValueError("a parent outside the best chain cannot have confirmations")
        if self.in_best_chain and self.conflict_txid is not None:
            raise ValueError("a best-chain parent cannot have a confirmed conflict")
        if self.conflict_txid is not None:
            if self.conflict_txid == self.parent_txid:
                raise ValueError("parent transaction cannot conflict with itself")
            if (self.conflict_confirmations or 0) < 1:
                raise ValueError("parent conflict must have at least one confirmation")
            if self.conflicting_channel_outpoint is None or not self.conflict_spends_channel:
                raise ValueError("parent conflict must verify the frozen channel input spend")
        elif (
            self.conflict_confirmations is not None
            or self.conflicting_channel_outpoint is not None
            or self.conflict_spends_channel is not None
        ):
            raise ValueError("conflict evidence requires a conflict transaction ID")
        if (self.success_confirmations or 0) and (self.timeout_confirmations or 0):
            raise ValueError("success and timeout spends cannot both confirm")
        if (self.success_confirmations or 0) and self.success_txid is None:
            raise ValueError("success confirmations require the observed success transaction ID")
        if (self.timeout_confirmations or 0) and self.timeout_txid is None:
            raise ValueError("timeout confirmations require the observed timeout transaction ID")
        if not self.in_best_chain and (
            (self.success_confirmations or 0) or (self.timeout_confirmations or 0)
        ):
            raise ValueError("a parent outside the best chain cannot have confirmed descendants")
        descendant_confirmations = max(
            self.success_confirmations or 0, self.timeout_confirmations or 0
        )
        if descendant_confirmations and self.parent_confirmations <= descendant_confirmations:
            raise ValueError("a descendant cannot have as many confirmations as its parent")
        return self


class EscrowLifecycleState(StrEnum):
    """Durable states of the channel-input escrow protocol."""

    NEGOTIATED = "negotiated"
    TIMEOUT_SIGNED = "timeout_signed"
    PARENT_SIGNED = "parent_signed"
    PARENT_PENDING = "parent_pending"
    PARENT_CONFIRMED = "parent_confirmed"
    PAYMENT_IN_FLIGHT = "payment_in_flight"
    SUCCESS_SPEND_PENDING = "success_spend_pending"
    TIMEOUT_PENDING = "timeout_pending"
    SUCCESS_SPEND_CONFIRMED = "success_spend_confirmed"
    TIMEOUT_CONFIRMED = "timeout_confirmed"
    PAID_TIMEOUT_LOSS = "paid_timeout_loss"
    COMMITMENT_CONFLICT = "commitment_conflict"
    PARENT_REORGED = "parent_reorged"
    CROSS_LAYER_LOSS = "cross_layer_loss"
    ABORTED = "aborted"


_ALLOWED_TRANSITIONS: dict[EscrowLifecycleState, frozenset[EscrowLifecycleState]] = {
    EscrowLifecycleState.NEGOTIATED: frozenset(
        {
            EscrowLifecycleState.TIMEOUT_SIGNED,
            EscrowLifecycleState.ABORTED,
        }
    ),
    EscrowLifecycleState.TIMEOUT_SIGNED: frozenset(
        {
            EscrowLifecycleState.PARENT_SIGNED,
            EscrowLifecycleState.ABORTED,
        }
    ),
    EscrowLifecycleState.PARENT_SIGNED: frozenset(
        {
            EscrowLifecycleState.PARENT_PENDING,
            EscrowLifecycleState.COMMITMENT_CONFLICT,
        }
    ),
    EscrowLifecycleState.PARENT_PENDING: frozenset(
        {
            EscrowLifecycleState.PARENT_CONFIRMED,
            EscrowLifecycleState.COMMITMENT_CONFLICT,
        }
    ),
    EscrowLifecycleState.PARENT_CONFIRMED: frozenset(
        {
            EscrowLifecycleState.PAYMENT_IN_FLIGHT,
            EscrowLifecycleState.TIMEOUT_PENDING,
            EscrowLifecycleState.PARENT_REORGED,
        }
    ),
    EscrowLifecycleState.PAYMENT_IN_FLIGHT: frozenset(
        {
            EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            EscrowLifecycleState.TIMEOUT_PENDING,
            EscrowLifecycleState.PARENT_REORGED,
        }
    ),
    EscrowLifecycleState.SUCCESS_SPEND_PENDING: frozenset(
        {
            EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
            EscrowLifecycleState.PARENT_REORGED,
        }
    ),
    EscrowLifecycleState.TIMEOUT_PENDING: frozenset(
        {
            EscrowLifecycleState.TIMEOUT_CONFIRMED,
            EscrowLifecycleState.PARENT_REORGED,
        }
    ),
    EscrowLifecycleState.TIMEOUT_CONFIRMED: frozenset({EscrowLifecycleState.PARENT_REORGED}),
    EscrowLifecycleState.PARENT_REORGED: frozenset(
        {
            EscrowLifecycleState.COMMITMENT_CONFLICT,
            EscrowLifecycleState.CROSS_LAYER_LOSS,
        }
    ),
    EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED: frozenset({EscrowLifecycleState.PARENT_REORGED}),
    EscrowLifecycleState.PAID_TIMEOUT_LOSS: frozenset({EscrowLifecycleState.PARENT_REORGED}),
    EscrowLifecycleState.COMMITMENT_CONFLICT: frozenset(),
    EscrowLifecycleState.CROSS_LAYER_LOSS: frozenset(),
    EscrowLifecycleState.ABORTED: frozenset(),
}


class ChannelInputEscrowRecord(BaseModel):
    """Mutable durable state and maker-local artifacts for one escrow session."""

    model_config = _MUTABLE_MODEL_CONFIG

    terms: ChannelInputEscrowTerms
    state: EscrowLifecycleState = EscrowLifecycleState.NEGOTIATED
    prepared: PreparedChannelInputEscrow | None = None
    authorization: ParentAuthorization | None = None
    parent_broadcast: EscrowParentBroadcast | None = None
    chain_observation: EscrowChainObservation | None = None
    payment_request: SinglePartPaymentRequest | None = None
    payment_result: SinglePartPaymentResult | None = None
    success_spends: tuple[EscrowSpendResult, ...] = ()
    timeout_broadcast: EscrowTimeoutBroadcast | None = None
    recovery_broadcasts: tuple[RecoveryBroadcastResult, ...] = ()
    recovery_required: bool = False
    state_before_reorg: EscrowLifecycleState | None = None
    revision: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_bound_artifacts(self) -> ChannelInputEscrowRecord:
        if self.prepared is not None and self.prepared.terms != self.terms:
            raise ValueError("prepared artifact terms must match the escrow record")
        if self.authorization is not None:
            if self.authorization.session_id != self.terms.snapshot.session_id:
                raise ValueError("parent authorization session must match escrow record")
            if self.prepared is None:
                raise ValueError("parent authorization requires a prepared timeout artifact")
            if self.authorization.parent_txid != self.prepared.parent_evidence.parent_txid:
                raise ValueError("parent authorization must bind the prepared parent")
            if (
                self.authorization.prepared_parent_tx_hex
                != self.prepared.parent_evidence.parent_tx_hex
            ):
                raise ValueError("parent authorization must bind the prepared parent bytes")
            if self.authorization.timeout_txid != self.prepared.timeout_evidence.timeout_txid:
                raise ValueError("parent authorization must bind the prepared timeout")
            if self.authorization.channel_funding_outpoint != self.terms.snapshot.funding_outpoint:
                raise ValueError("parent authorization must spend the frozen channel")
            if (
                self.authorization.channel_input_index
                != self.prepared.parent_evidence.channel_input_index
            ):
                raise ValueError("parent authorization input index must match prepared escrow")
            if self.authorization.signer_pubkey != self.terms.authorizer_pubkey:
                raise ValueError("parent authorization signer must match negotiated authorizer")
        if self.payment_result is not None:
            try:
                self.payment_result.validate_against(self.terms)
            except ChannelInputEscrowError as exc:
                raise ValueError(str(exc)) from exc
            if self.payment_request is None:
                raise ValueError("payment result requires its durable payment request")
            if self.payment_result.attempt_height != self.payment_request.requested_at_height:
                raise ValueError("payment result attempt height must match its payment request")
        if self.payment_request is not None and (
            self.prepared is None or self.payment_request.prepared != self.prepared
        ):
            raise ValueError("payment request must bind the prepared escrow")
        if self.parent_broadcast is not None:
            if self.parent_broadcast.session_id != self.session_id:
                raise ValueError("parent broadcast session must match escrow record")
            if self.prepared is None:
                raise ValueError("parent broadcast requires prepared escrow evidence")
            if self.parent_broadcast.parent_txid != self.prepared.parent_evidence.parent_txid:
                raise ValueError("parent broadcast transaction must match prepared escrow")
        if self.chain_observation is not None:
            self._validate_chain_observation(self.chain_observation, error_type=ValueError)
        for index, success_spend in enumerate(self.success_spends):
            if self.prepared is None:
                raise ValueError("success spend requires prepared escrow evidence")
            try:
                success_spend.validate_against(self.prepared)
            except ChannelInputEscrowError as exc:
                raise ValueError(str(exc)) from exc
            if (
                self.payment_result is None
                or self.payment_result.status is not PaymentStatus.SUCCEEDED
                or self.payment_result.settled_height != success_spend.fulfillment_height
            ):
                raise ValueError("success spend requires matching settled payment evidence")
            expected_replaced_txid = self.success_spends[index - 1].txid if index else None
            if success_spend.replaces_txid != expected_replaced_txid:
                raise ValueError("success spend replacement chain is not contiguous")
            if index:
                previous_spend = self.success_spends[index - 1]
                if (
                    success_spend.txid == previous_spend.txid
                    or success_spend.fee_sats <= previous_spend.fee_sats
                    or success_spend.fee_rate_milli_sat_vb <= previous_spend.fee_rate_milli_sat_vb
                    or success_spend.broadcast_height < previous_spend.broadcast_height
                ):
                    raise ValueError(
                        "success replacement must increase fee, feerate, and preserve ordering"
                    )
        if self.timeout_broadcast is not None:
            if self.prepared is None:
                raise ValueError("timeout broadcast requires prepared escrow evidence")
            if self.timeout_broadcast.session_id != self.session_id:
                raise ValueError("timeout broadcast session must match escrow record")
            if self.timeout_broadcast.parent_txid != self.prepared.parent_evidence.parent_txid:
                raise ValueError("timeout broadcast parent must match prepared escrow")
            if self.timeout_broadcast.timeout_txid != self.prepared.timeout_evidence.timeout_txid:
                raise ValueError("timeout broadcast transaction must match prepared recovery")
            if self.timeout_broadcast.broadcast_height <= self.terms.timeout_height:
                raise ValueError("timeout recovery cannot broadcast before its eligible height")
        for recovery in self.recovery_broadcasts:
            error = self._recovery_broadcast_error(recovery)
            if error is not None:
                raise ValueError(error)
        states_requiring_prepared = {
            EscrowLifecycleState.TIMEOUT_SIGNED,
            EscrowLifecycleState.PARENT_SIGNED,
            EscrowLifecycleState.PARENT_PENDING,
            EscrowLifecycleState.PARENT_CONFIRMED,
            EscrowLifecycleState.PAYMENT_IN_FLIGHT,
            EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            EscrowLifecycleState.TIMEOUT_PENDING,
            EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
            EscrowLifecycleState.TIMEOUT_CONFIRMED,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
            EscrowLifecycleState.PARENT_REORGED,
            EscrowLifecycleState.COMMITMENT_CONFLICT,
            EscrowLifecycleState.CROSS_LAYER_LOSS,
        }
        states_requiring_authorization = states_requiring_prepared - {
            EscrowLifecycleState.TIMEOUT_SIGNED
        }
        states_requiring_payment = {
            EscrowLifecycleState.PAYMENT_IN_FLIGHT,
            EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
            EscrowLifecycleState.CROSS_LAYER_LOSS,
        }
        if self.state is EscrowLifecycleState.PARENT_REORGED:
            valid_prior_states = {
                source
                for source, targets in _ALLOWED_TRANSITIONS.items()
                if EscrowLifecycleState.PARENT_REORGED in targets
            }
            if self.state_before_reorg not in valid_prior_states:
                raise ValueError("parent_reorged state requires a valid prior lifecycle state")
        elif self.state_before_reorg is not None:
            raise ValueError("only parent_reorged state may retain its prior lifecycle state")
        if self.state is not EscrowLifecycleState.PARENT_REORGED and self.recovery_required:
            raise ValueError("only parent_reorged state may require recovery rebroadcast")
        if (
            self.state
            in {
                EscrowLifecycleState.PARENT_PENDING,
                EscrowLifecycleState.PARENT_CONFIRMED,
                EscrowLifecycleState.PAYMENT_IN_FLIGHT,
                EscrowLifecycleState.SUCCESS_SPEND_PENDING,
                EscrowLifecycleState.TIMEOUT_PENDING,
                EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
                EscrowLifecycleState.TIMEOUT_CONFIRMED,
                EscrowLifecycleState.PAID_TIMEOUT_LOSS,
                EscrowLifecycleState.PARENT_REORGED,
                EscrowLifecycleState.CROSS_LAYER_LOSS,
            }
            and self.parent_broadcast is None
        ):
            raise ValueError(f"{self.state} state requires exact parent broadcast evidence")
        chain_error = self._state_chain_error(self.state)
        if chain_error is not None:
            raise ValueError(chain_error)
        evidence_state = self.state_before_reorg or self.state
        if evidence_state in states_requiring_prepared and self.prepared is None:
            raise ValueError(f"{evidence_state} state requires prepared timeout evidence")
        if evidence_state in states_requiring_authorization and self.authorization is None:
            raise ValueError(f"{evidence_state} state requires parent authorization")
        if evidence_state in states_requiring_payment and self.payment_result is None:
            raise ValueError(f"{evidence_state} state requires payment and route evidence")
        if evidence_state in states_requiring_payment and self.payment_request is None:
            raise ValueError(f"{evidence_state} state requires a durable payment request")
        states_requiring_settlement = {
            EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
            EscrowLifecycleState.CROSS_LAYER_LOSS,
        }
        if evidence_state in states_requiring_settlement and (
            self.payment_result is None or self.payment_result.status is not PaymentStatus.SUCCEEDED
        ):
            raise ValueError(f"{evidence_state} state requires settled payment evidence")
        if evidence_state is EscrowLifecycleState.TIMEOUT_PENDING and (
            self.payment_result is not None
            and self.payment_result.status is not PaymentStatus.FAILED
        ):
            raise ValueError("timeout_pending state requires failed or unoffered payment evidence")
        if evidence_state is EscrowLifecycleState.COMMITMENT_CONFLICT and (
            self.payment_result is not None
            and self.payment_result.status is not PaymentStatus.FAILED
        ):
            raise ValueError(
                "commitment_conflict state requires failed or unoffered payment evidence"
            )
        if (
            evidence_state
            in {
                EscrowLifecycleState.SUCCESS_SPEND_PENDING,
                EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
                EscrowLifecycleState.PAID_TIMEOUT_LOSS,
                EscrowLifecycleState.CROSS_LAYER_LOSS,
            }
            and not self.success_spends
        ):
            raise ValueError(f"{evidence_state} state requires a success spend transaction")
        if (
            evidence_state
            in {
                EscrowLifecycleState.TIMEOUT_PENDING,
                EscrowLifecycleState.TIMEOUT_CONFIRMED,
                EscrowLifecycleState.PAID_TIMEOUT_LOSS,
            }
            and self.timeout_broadcast is None
        ):
            raise ValueError(f"{evidence_state} state requires a timeout spend transaction")
        return self

    @property
    def session_id(self) -> str:
        """Return the durable session ID from the immutable channel snapshot."""
        return self.terms.snapshot.session_id

    @property
    def route(self) -> EscrowRouteEvidence | None:
        """Return persisted route evidence when a payment was attempted."""
        return self.payment_result.route if self.payment_result is not None else None

    @property
    def success_spend(self) -> EscrowSpendResult | None:
        """Return the latest persisted success spend or fee replacement."""
        return self.success_spends[-1] if self.success_spends else None

    @property
    def payment_id(self) -> str | None:
        """Return the persisted backend payment identifier when present."""
        return self.payment_result.payment_id if self.payment_result is not None else None

    @property
    def peer_settle_deadline_violated(self) -> bool:
        """Report a late peer fulfillment that still arrived before the outgoing expiry."""
        return bool(
            self.payment_result is not None
            and self.payment_result.settled_height is not None
            and self.payment_result.settled_height > self.terms.peer_settle_deadline
        )

    @property
    def success_spend_txid(self) -> str | None:
        """Return the persisted success-spend transaction ID when present."""
        return self.success_spend.txid if self.success_spend is not None else None

    @property
    def timeout_spend_txid(self) -> str | None:
        """Return the persisted timeout transaction ID when broadcast."""
        return self.timeout_broadcast.timeout_txid if self.timeout_broadcast is not None else None

    def _touch(self) -> None:
        self.revision += 1
        self.updated_at = datetime.now(UTC)

    def _chain_observation_error(self, observation: EscrowChainObservation) -> str | None:
        if observation.session_id != self.session_id:
            return "chain observation session does not match escrow record"
        if self.prepared is None:
            return "chain observation requires prepared escrow evidence"
        if observation.parent_txid != self.prepared.parent_evidence.parent_txid:
            return "chain observation parent does not match prepared escrow"
        if observation.success_txid is not None and observation.success_txid not in {
            spend.txid for spend in self.success_spends
        }:
            return "chain observation success spend does not match durable evidence"
        if (
            observation.timeout_txid is not None
            and observation.timeout_txid != self.prepared.timeout_evidence.timeout_txid
        ):
            return "chain observation timeout spend does not match prepared recovery"
        if (
            observation.conflicting_channel_outpoint is not None
            and observation.conflicting_channel_outpoint != self.terms.snapshot.funding_outpoint
        ):
            return "chain observation conflict does not spend the frozen channel outpoint"
        return None

    def _validate_chain_observation(
        self, observation: EscrowChainObservation, *, error_type: type[Exception]
    ) -> None:
        error = self._chain_observation_error(observation)
        if error is not None:
            raise error_type(error)

    def _state_chain_error(self, state: EscrowLifecycleState) -> str | None:
        states_requiring_confirmed_parent = {
            EscrowLifecycleState.PARENT_CONFIRMED,
            EscrowLifecycleState.PAYMENT_IN_FLIGHT,
            EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            EscrowLifecycleState.TIMEOUT_PENDING,
            EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
            EscrowLifecycleState.TIMEOUT_CONFIRMED,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
        }
        if state in states_requiring_confirmed_parent:
            if self.chain_observation is None:
                return f"{state} state requires exact parent confirmation evidence"
            if not self.chain_observation.in_best_chain:
                return f"{state} state requires the parent in the best chain"
            if self.chain_observation.parent_confirmations < self.terms.parent_confirmation_depth:
                return f"{state} state requires the selected parent confirmation depth"
        if state in {
            EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
        } and (
            self.chain_observation is None
            or self.success_spend is None
            or self.chain_observation.observed_height < self.success_spend.broadcast_height
        ):
            return f"{state} state requires a parent observation after the success broadcast"
        if state in {
            EscrowLifecycleState.TIMEOUT_PENDING,
            EscrowLifecycleState.TIMEOUT_CONFIRMED,
        } and (
            self.chain_observation is None
            or self.timeout_broadcast is None
            or self.chain_observation.observed_height < self.timeout_broadcast.broadcast_height
        ):
            return f"{state} state requires a parent observation after the timeout broadcast"
        if state is EscrowLifecycleState.PARENT_REORGED and (
            self.chain_observation is None or self.chain_observation.in_best_chain
        ):
            return "parent_reorged state requires evidence outside the best chain"
        if state in {
            EscrowLifecycleState.COMMITMENT_CONFLICT,
            EscrowLifecycleState.CROSS_LAYER_LOSS,
        } and (
            self.chain_observation is None
            or self.chain_observation.in_best_chain
            or self.chain_observation.conflict_txid is None
        ):
            return f"{state} state requires confirmed parent-input conflict evidence"
        if state is EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED and (
            self.chain_observation is None
            or (self.chain_observation.success_confirmations or 0)
            < self.terms.success_confirmation_depth
        ):
            return "success_spend_confirmed state requires selected-depth success evidence"
        if state in {
            EscrowLifecycleState.TIMEOUT_CONFIRMED,
            EscrowLifecycleState.PAID_TIMEOUT_LOSS,
        } and (
            self.chain_observation is None
            or (self.chain_observation.timeout_confirmations or 0) < 1
        ):
            return f"{state} state requires confirmed exact-timeout evidence"
        return None

    def _recovery_broadcast_error(self, result: RecoveryBroadcastResult) -> str | None:
        if result.session_id != self.session_id:
            return "recovery broadcast session does not match escrow record"
        if self.prepared is None or result.parent_txid != self.prepared.parent_evidence.parent_txid:
            return "recovery broadcast parent does not match prepared escrow"
        known_txids = {result.parent_txid, *(spend.txid for spend in self.success_spends)}
        if self.timeout_broadcast is not None:
            known_txids.add(self.timeout_broadcast.timeout_txid)
        if not set(result.broadcast_txids).issubset(known_txids):
            return "recovery broadcast contains an unknown transaction"
        required_txids = {result.parent_txid}
        if self.state_before_reorg is EscrowLifecycleState.SUCCESS_SPEND_PENDING:
            if self.success_spend is None:
                return "success recovery requires durable success spend evidence"
            required_txids.add(self.success_spend.txid)
        if self.state_before_reorg is EscrowLifecycleState.TIMEOUT_PENDING:
            if self.timeout_broadcast is None:
                return "timeout recovery requires durable timeout broadcast evidence"
            required_txids.add(self.timeout_broadcast.timeout_txid)
        if not required_txids.issubset(result.broadcast_txids):
            return "recovery broadcast omitted a required parent or descendant transaction"
        return None

    def attach_prepared(self, prepared: PreparedChannelInputEscrow) -> None:
        """Attach the exact timeout artifact before parent authorization."""
        if self.state is not EscrowLifecycleState.NEGOTIATED:
            raise ChannelInputEscrowError("timeout artifact can only be attached while negotiated")
        if prepared.terms != self.terms:
            raise ChannelInputEscrowError("prepared artifact terms do not match this record")
        self.prepared = prepared
        self._touch()

    def attach_authorization(self, authorization: ParentAuthorization) -> None:
        """Attach a parent authorization only after timeout signing is durable."""
        if self.state is not EscrowLifecycleState.TIMEOUT_SIGNED:
            raise ChannelInputEscrowError("parent authorization requires timeout_signed state")
        if self.prepared is None:
            raise ChannelInputEscrowError("parent authorization requires a prepared timeout")
        if authorization.session_id != self.session_id:
            raise ChannelInputEscrowError("parent authorization session does not match record")
        if authorization.parent_txid != self.prepared.parent_evidence.parent_txid:
            raise ChannelInputEscrowError("parent authorization does not match parent transaction")
        if authorization.prepared_parent_tx_hex != self.prepared.parent_evidence.parent_tx_hex:
            raise ChannelInputEscrowError(
                "parent authorization does not bind prepared parent bytes"
            )
        if authorization.timeout_txid != self.prepared.timeout_evidence.timeout_txid:
            raise ChannelInputEscrowError("parent authorization does not match timeout transaction")
        if authorization.channel_funding_outpoint != self.terms.snapshot.funding_outpoint:
            raise ChannelInputEscrowError("parent authorization does not spend the frozen channel")
        if authorization.channel_input_index != self.prepared.parent_evidence.channel_input_index:
            raise ChannelInputEscrowError("parent authorization input index does not match record")
        if authorization.signer_pubkey != self.terms.authorizer_pubkey:
            raise ChannelInputEscrowError("parent authorization signer does not match record")
        self.authorization = authorization
        self._touch()

    def attach_parent_broadcast(self, result: EscrowParentBroadcast) -> None:
        """Bind broadcast evidence for the exact authorized parent."""
        if self.state is not EscrowLifecycleState.PARENT_SIGNED:
            raise ChannelInputEscrowError("parent broadcast requires parent_signed state")
        if result.session_id != self.session_id:
            raise ChannelInputEscrowError("parent broadcast session does not match record")
        if self.prepared is None or result.parent_txid != self.prepared.parent_evidence.parent_txid:
            raise ChannelInputEscrowError("parent broadcast does not match prepared escrow")
        self.parent_broadcast = result
        self._touch()

    def attach_chain_observation(self, observation: EscrowChainObservation) -> None:
        """Persist exact-parent and descendant chain evidence for guarded transitions."""
        self._validate_chain_observation(observation, error_type=ChannelInputEscrowError)
        self.chain_observation = observation
        self._touch()

    def attach_payment_request(self, request: SinglePartPaymentRequest) -> None:
        """Persist the exact fresh confirmation gate before attempting payment."""
        if self.state is not EscrowLifecycleState.PARENT_CONFIRMED:
            raise ChannelInputEscrowError("payment request requires parent_confirmed state")
        if self.prepared is None or request.prepared != self.prepared:
            raise ChannelInputEscrowError("payment request does not bind the prepared escrow")
        if self.chain_observation is None or request.chain_observation != self.chain_observation:
            raise ChannelInputEscrowError(
                "payment request does not use the latest chain observation"
            )
        if self.payment_request is not None and request != self.payment_request:
            raise ChannelInputEscrowError("escrow permits only one durable payment attempt")
        if self.payment_request is None:
            self.payment_request = request
            self._touch()

    def attach_payment_result(self, result: SinglePartPaymentResult) -> None:
        """Bind an initial payment or a monotone observation of that payment."""
        observing_in_flight = self.state is EscrowLifecycleState.PAYMENT_IN_FLIGHT
        observing_during_reorg = (
            self.state is EscrowLifecycleState.PARENT_REORGED
            and self.state_before_reorg is EscrowLifecycleState.PAYMENT_IN_FLIGHT
        )
        if self.state is not EscrowLifecycleState.PARENT_CONFIRMED and not (
            observing_in_flight or observing_during_reorg
        ):
            raise ChannelInputEscrowError("payment evidence requires an active payment state")
        result.validate_against(self.terms)
        if self.payment_request is None:
            raise ChannelInputEscrowError("payment result requires a durable payment request")
        if result.attempt_height != self.payment_request.requested_at_height:
            raise ChannelInputEscrowError(
                "payment result attempt height does not match its request"
            )
        if (
            self.state is EscrowLifecycleState.PARENT_CONFIRMED
            and result.status is PaymentStatus.FAILED
        ):
            raise ChannelInputEscrowError("failed payment cannot enter payment_in_flight state")
        if self.payment_result is not None:
            if result.payment_id != self.payment_result.payment_id:
                raise ChannelInputEscrowError("payment observation changed the backend payment ID")
            if result.route != self.payment_result.route:
                raise ChannelInputEscrowError("payment observation changed the selected route")
            if result.attempt_height != self.payment_result.attempt_height:
                raise ChannelInputEscrowError("payment observation changed the attempt height")
            if result.observed_height < self.payment_result.observed_height:
                raise ChannelInputEscrowError("payment observation height moved backwards")
            if self.payment_result.status is not PaymentStatus.PENDING:
                if result != self.payment_result:
                    raise ChannelInputEscrowError("terminal payment evidence cannot change")
                return
        self.payment_result = result
        self._touch()

    def attach_success_spend(self, result: EscrowSpendResult) -> None:
        """Bind the immediate success spend before recording it as pending."""
        replacing_pending_spend = self.state is EscrowLifecycleState.SUCCESS_SPEND_PENDING
        during_payment_reorg = (
            self.state is EscrowLifecycleState.PARENT_REORGED
            and self.state_before_reorg
            in {
                EscrowLifecycleState.PAYMENT_IN_FLIGHT,
                EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            }
        )
        if self.state is not EscrowLifecycleState.PAYMENT_IN_FLIGHT and not (
            replacing_pending_spend or during_payment_reorg
        ):
            raise ChannelInputEscrowError("success spend requires an active in-flight payment")
        if result.session_id != self.session_id:
            raise ChannelInputEscrowError("success spend session does not match record")
        if self.prepared is None or result.parent_txid != self.prepared.parent_evidence.parent_txid:
            raise ChannelInputEscrowError("success spend parent does not match record")
        if (
            self.payment_result is None
            or self.payment_result.status is not PaymentStatus.SUCCEEDED
            or self.payment_result.settled_height != result.fulfillment_height
        ):
            raise ChannelInputEscrowError(
                "success spend requires matching settled payment evidence"
            )
        result.validate_against(self.prepared)
        expected_replaced_txid = self.success_spend.txid if self.success_spend is not None else None
        if result.replaces_txid != expected_replaced_txid:
            raise ChannelInputEscrowError("success spend does not extend the replacement chain")
        if self.success_spend is not None and (
            result.txid == self.success_spend.txid
            or result.fee_sats <= self.success_spend.fee_sats
            or result.fee_rate_milli_sat_vb <= self.success_spend.fee_rate_milli_sat_vb
            or result.broadcast_height < self.success_spend.broadcast_height
        ):
            raise ChannelInputEscrowError(
                "success replacement must increase fee, feerate, and preserve broadcast ordering"
            )
        self.success_spends = (*self.success_spends, result)
        if (
            during_payment_reorg
            and self.state_before_reorg is EscrowLifecycleState.PAYMENT_IN_FLIGHT
        ):
            self.state_before_reorg = EscrowLifecycleState.SUCCESS_SPEND_PENDING
        self._touch()

    def attach_timeout_broadcast(self, result: EscrowTimeoutBroadcast) -> None:
        """Bind timeout broadcast evidence before recording recovery as pending."""
        during_active_reorg = (
            self.state is EscrowLifecycleState.PARENT_REORGED
            and self.state_before_reorg
            in {
                EscrowLifecycleState.PARENT_CONFIRMED,
                EscrowLifecycleState.PAYMENT_IN_FLIGHT,
                EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            }
        )
        if (
            self.state
            not in {
                EscrowLifecycleState.PARENT_CONFIRMED,
                EscrowLifecycleState.PAYMENT_IN_FLIGHT,
                EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            }
            and not during_active_reorg
        ):
            raise ChannelInputEscrowError(
                "timeout broadcast requires an active confirmed-parent recovery state"
            )
        if result.session_id != self.session_id:
            raise ChannelInputEscrowError("timeout broadcast session does not match record")
        if self.prepared is None:
            raise ChannelInputEscrowError("timeout broadcast requires prepared recovery evidence")
        if result.parent_txid != self.prepared.parent_evidence.parent_txid:
            raise ChannelInputEscrowError("timeout broadcast parent does not match record")
        if result.timeout_txid != self.prepared.timeout_evidence.timeout_txid:
            raise ChannelInputEscrowError("timeout broadcast transaction does not match recovery")
        if result.broadcast_height <= self.terms.timeout_height:
            raise ChannelInputEscrowError(
                "timeout recovery cannot broadcast before its eligible height"
            )
        self.timeout_broadcast = result
        if (
            during_active_reorg
            and self.state_before_reorg is not EscrowLifecycleState.SUCCESS_SPEND_PENDING
        ):
            self.state_before_reorg = EscrowLifecycleState.TIMEOUT_PENDING
        self._touch()

    def attach_recovery_broadcast(self, result: RecoveryBroadcastResult) -> None:
        """Persist a restart or reorganization rebroadcast attempt."""
        if self.state is not EscrowLifecycleState.PARENT_REORGED:
            raise ChannelInputEscrowError("recovery rebroadcast requires parent_reorged state")
        error = self._recovery_broadcast_error(result)
        if error is not None:
            raise ChannelInputEscrowError(error)
        self.recovery_broadcasts = (*self.recovery_broadcasts, result)
        self.recovery_required = False
        self._touch()

    def advance(self, target: EscrowLifecycleState) -> None:
        """Advance once along the guarded lifecycle, accepting idempotent retries."""
        if target is self.state:
            return
        restores_reorg_state = (
            self.state is EscrowLifecycleState.PARENT_REORGED
            and self.state_before_reorg is not None
            and target is self.state_before_reorg
        )
        if target not in _ALLOWED_TRANSITIONS[self.state] and not restores_reorg_state:
            raise ChannelInputEscrowError(f"cannot transition from {self.state} to {target}")
        if target is EscrowLifecycleState.TIMEOUT_SIGNED and self.prepared is None:
            raise ChannelInputEscrowError("timeout_signed requires a prepared timeout artifact")
        if target is EscrowLifecycleState.PARENT_SIGNED and self.authorization is None:
            raise ChannelInputEscrowError("parent_signed requires exact parent authorization")
        if target is EscrowLifecycleState.PARENT_PENDING and self.parent_broadcast is None:
            raise ChannelInputEscrowError("parent_pending requires exact parent broadcast evidence")
        if target is EscrowLifecycleState.PAYMENT_IN_FLIGHT and (
            self.payment_request is None or self.payment_result is None
        ):
            raise ChannelInputEscrowError(
                "payment_in_flight requires a durable request and route evidence"
            )
        if target is EscrowLifecycleState.SUCCESS_SPEND_PENDING and self.success_spend is None:
            raise ChannelInputEscrowError("success_spend_pending requires a broadcast spend")
        if target is EscrowLifecycleState.TIMEOUT_PENDING and self.timeout_broadcast is None:
            raise ChannelInputEscrowError("timeout_pending requires a broadcast timeout recovery")
        if target is EscrowLifecycleState.TIMEOUT_PENDING and (
            self.payment_result is not None
            and self.payment_result.status is not PaymentStatus.FAILED
        ):
            raise ChannelInputEscrowError(
                "timeout_pending requires failed or unoffered payment evidence"
            )
        if target is EscrowLifecycleState.PAID_TIMEOUT_LOSS and (
            self.payment_result is None or self.payment_result.status is not PaymentStatus.SUCCEEDED
        ):
            raise ChannelInputEscrowError("paid_timeout_loss requires settled payment evidence")
        if target is EscrowLifecycleState.CROSS_LAYER_LOSS and (
            self.payment_result is None or self.payment_result.status is not PaymentStatus.SUCCEEDED
        ):
            raise ChannelInputEscrowError("cross_layer_loss requires settled payment evidence")
        if target is EscrowLifecycleState.COMMITMENT_CONFLICT and (
            self.payment_result is not None
            and self.payment_result.status is not PaymentStatus.FAILED
        ):
            raise ChannelInputEscrowError(
                "commitment_conflict requires failed or unoffered payment evidence"
            )
        if restores_reorg_state and self.recovery_required:
            raise ChannelInputEscrowError(
                "parent reorganization requires a durable rebroadcast attempt"
            )
        chain_error = self._state_chain_error(target)
        if chain_error is not None:
            raise ChannelInputEscrowError(chain_error)
        if target is EscrowLifecycleState.PARENT_REORGED:
            self.state_before_reorg = {
                EscrowLifecycleState.SUCCESS_SPEND_CONFIRMED: EscrowLifecycleState.SUCCESS_SPEND_PENDING,
                EscrowLifecycleState.TIMEOUT_CONFIRMED: EscrowLifecycleState.TIMEOUT_PENDING,
                EscrowLifecycleState.PAID_TIMEOUT_LOSS: EscrowLifecycleState.SUCCESS_SPEND_PENDING,
            }.get(self.state, self.state)
            self.recovery_required = True
        elif self.state is EscrowLifecycleState.PARENT_REORGED and (
            restores_reorg_state
            or target
            in {
                EscrowLifecycleState.COMMITMENT_CONFLICT,
                EscrowLifecycleState.CROSS_LAYER_LOSS,
            }
        ):
            self.state_before_reorg = None
            self.recovery_required = False
        self.state = target
        self._touch()


class FreezeChannelRequest(BaseModel):
    """Request to freeze and quiesce the existing channel."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)


class PrepareTimeoutRequest(BaseModel):
    """Request to prepare a timeout against the exact parent transaction."""

    model_config = _FROZEN_MODEL_CONFIG

    terms: ChannelInputEscrowTerms
    parent_evidence: EscrowParentEvidence


class ParentAuthorizationRequest(BaseModel):
    """Request to authorize a parent only after timeout preparation."""

    model_config = _FROZEN_MODEL_CONFIG

    prepared: PreparedChannelInputEscrow


class ParentBroadcastRequest(BaseModel):
    """Request to broadcast the exact fully authorized escrow parent."""

    model_config = _FROZEN_MODEL_CONFIG

    prepared: PreparedChannelInputEscrow
    authorization: ParentAuthorization

    @model_validator(mode="after")
    def validate_parent_binding(self) -> ParentBroadcastRequest:
        if self.authorization.session_id != self.prepared.terms.snapshot.session_id:
            raise ValueError(
                "parent broadcast authorization session does not match prepared escrow"
            )
        if self.authorization.parent_txid != self.prepared.parent_evidence.parent_txid:
            raise ValueError("parent broadcast authorization does not match prepared parent")
        if self.authorization.prepared_parent_tx_hex != self.prepared.parent_evidence.parent_tx_hex:
            raise ValueError("parent broadcast authorization does not bind prepared parent bytes")
        if self.authorization.timeout_txid != self.prepared.timeout_evidence.timeout_txid:
            raise ValueError("parent broadcast authorization does not match prepared timeout")
        if (
            self.authorization.channel_funding_outpoint
            != self.prepared.parent_evidence.channel_funding_outpoint
            or self.authorization.channel_input_index
            != self.prepared.parent_evidence.channel_input_index
        ):
            raise ValueError("parent broadcast authorization does not match the channel input")
        if self.authorization.signer_pubkey != self.prepared.terms.authorizer_pubkey:
            raise ValueError("parent broadcast authorization signer does not match escrow terms")
        return self


class ChainObservationRequest(BaseModel):
    """Request to observe confirmation, conflict, and reorg state for one session."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    parent_txid: str

    @field_validator("parent_txid")
    @classmethod
    def validate_observed_parent_txid(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="parent_txid")


class PaymentObservationRequest(BaseModel):
    """Request a durable status update for one escrow payment attempt."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    payment_id: str = Field(min_length=1)


class SinglePartPaymentRequest(BaseModel):
    """Request for a route that cannot consume the escrowed channel."""

    model_config = _FROZEN_MODEL_CONFIG

    prepared: PreparedChannelInputEscrow
    chain_observation: EscrowChainObservation
    excluded_channel_ids: tuple[str, ...] = Field(min_length=1)
    requested_at_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    requested_at_unix: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_payment_gate(self) -> SinglePartPaymentRequest:
        terms = self.prepared.terms
        if terms.snapshot.channel_id not in self.excluded_channel_ids:
            raise ValueError("payment request must exclude the consumed channel")
        if self.chain_observation.session_id != terms.snapshot.session_id:
            raise ValueError("payment observation must match the escrow session")
        if self.chain_observation.parent_txid != self.prepared.parent_evidence.parent_txid:
            raise ValueError("payment observation must match the exact parent")
        if not self.chain_observation.in_best_chain or self.chain_observation.conflict_txid:
            raise ValueError("payment requires the exact parent in the best chain")
        if self.chain_observation.parent_confirmations < terms.parent_confirmation_depth:
            raise ValueError("payment requires the selected parent confirmation depth")
        if self.chain_observation.observed_height != self.requested_at_height:
            raise ValueError("payment requires a fresh parent chain observation")
        if self.requested_at_unix >= terms.invoice_expiry_unix:
            raise ValueError("payment invoice has expired")
        return self


class SinglePartPaymentResult(BaseModel):
    """Payment state and route evidence returned by a single-part payment attempt."""

    model_config = _FROZEN_MODEL_CONFIG

    payment_id: str = Field(min_length=1)
    status: PaymentStatus
    preimage: str | None = None
    attempt_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    observed_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    settled_height: int | None = Field(default=None, ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    route: EscrowRouteEvidence

    @field_validator("preimage")
    @classmethod
    def validate_preimage(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_hex(value, byte_length=32, name="preimage")

    @model_validator(mode="after")
    def validate_payment_state(self) -> SinglePartPaymentResult:
        if self.observed_height < self.attempt_height:
            raise ValueError("payment observation cannot precede the payment attempt")
        if self.status is PaymentStatus.SUCCEEDED:
            if self.preimage is None or self.settled_height is None:
                raise ValueError(
                    "a succeeded escrow payment must return preimage and settlement height"
                )
            if not self.attempt_height <= self.settled_height <= self.observed_height:
                raise ValueError("payment settlement height must be within its observation window")
        elif self.preimage is not None or self.settled_height is not None:
            raise ValueError("an unsettled escrow payment cannot return settlement evidence")
        return self

    def validate_against(self, terms: ChannelInputEscrowTerms) -> None:
        """Validate route policy and any returned preimage against escrow terms."""
        self.route.validate_against(terms)
        if (
            self.route.final_expiry_height
            < self.attempt_height + terms.invoice_min_final_cltv_delta
        ):
            raise ChannelInputEscrowError("route does not honor the invoice minimum final CLTV")
        if (
            self.settled_height is not None
            and self.settled_height > self.route.outgoing_expiry_height
        ):
            raise ChannelInputEscrowError("payment settled after the outgoing HTLC expiry")
        if self.preimage is not None:
            actual_hash = hashlib.sha256(bytes.fromhex(self.preimage)).hexdigest()
            if actual_hash != terms.payment_hash:
                raise ChannelInputEscrowError("payment preimage does not match escrow hash")


class SuccessSpendRequest(BaseModel):
    """Request to build and broadcast the success spend after payment settlement."""

    model_config = _FROZEN_MODEL_CONFIG

    prepared: PreparedChannelInputEscrow
    payment_result: SinglePartPaymentResult

    @model_validator(mode="after")
    def validate_success_evidence(self) -> SuccessSpendRequest:
        if self.payment_result.status is not PaymentStatus.SUCCEEDED:
            raise ValueError("success spend requires a settled Lightning payment")
        try:
            self.payment_result.validate_against(self.prepared.terms)
        except ChannelInputEscrowError as exc:
            raise ValueError(str(exc)) from exc
        if self.payment_result.settled_height is None:
            raise ValueError("success spend requires a payment settlement height")
        if self.payment_result.settled_height > self.prepared.terms.success_broadcast_deadline:
            raise ValueError("payment fulfillment arrived after the success broadcast deadline")
        return self


class EscrowSpendResult(BaseModel):
    """Evidence that the immediate RBF-capable success spend was broadcast."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    parent_txid: str
    spent_escrow_outpoint: EscrowOutpoint
    txid: str
    tx_hex: str
    escrow_input_value_sats: int = Field(gt=0)
    additional_input_value_sats: int = Field(ge=0)
    maker_controlled_output_sats: int = Field(gt=0)
    fee_sats: int = Field(ge=0)
    fee_rate_milli_sat_vb: int = Field(gt=0)
    fulfillment_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    broadcast_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)
    rbf_enabled: bool
    preimage_verified: bool
    success_path_verified: bool
    transaction_verified: bool
    all_outputs_maker_controlled: bool
    replacement_policy_verified: bool = False
    replaces_txid: str | None = None

    @field_validator("parent_txid", "txid", "replaces_txid")
    @classmethod
    def validate_spend_txid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_hex(value, byte_length=32, name="spend transaction ID")

    @field_validator("tx_hex")
    @classmethod
    def validate_spend_hex(cls, value: str) -> str:
        return _validate_hex(value, name="success spend transaction")

    @model_validator(mode="after")
    def validate_spend_policy(self) -> EscrowSpendResult:
        if not self.rbf_enabled:
            raise ValueError("success spend must opt in to RBF")
        if not self.preimage_verified:
            raise ValueError("success spend must use a verified payment preimage")
        if not self.success_path_verified:
            raise ValueError("success spend must use the verified escrow success path")
        if not self.transaction_verified:
            raise ValueError("success spend transaction must be independently verified")
        if not self.all_outputs_maker_controlled:
            raise ValueError("every success spend output must be maker controlled")
        if self.replaces_txid is not None:
            if self.replaces_txid == self.txid:
                raise ValueError("success replacement must have a distinct transaction ID")
            if not self.replacement_policy_verified:
                raise ValueError(
                    "success replacement must satisfy verified relay replacement policy"
                )
        if self.broadcast_height < self.fulfillment_height:
            raise ValueError("success spend cannot precede payment fulfillment")
        total_inputs = self.escrow_input_value_sats + self.additional_input_value_sats
        total_outputs_and_fee = self.maker_controlled_output_sats + self.fee_sats
        if total_inputs != total_outputs_and_fee:
            raise ValueError("success spend inputs must equal outputs plus fee")
        return self

    def validate_against(self, prepared: PreparedChannelInputEscrow) -> None:
        """Bind a success broadcast to its session, parent, and deadline budget."""
        terms = prepared.terms
        if self.session_id != terms.snapshot.session_id:
            raise ChannelInputEscrowError("success spend session does not match prepared escrow")
        if self.parent_txid != prepared.parent_evidence.parent_txid:
            raise ChannelInputEscrowError("success spend parent does not match prepared escrow")
        if self.spent_escrow_outpoint != prepared.parent_evidence.escrow_outpoint:
            raise ChannelInputEscrowError(
                "success spend does not consume the prepared escrow outpoint"
            )
        if self.escrow_input_value_sats != prepared.terms.escrow_value_sats:
            raise ChannelInputEscrowError("success spend escrow input value does not match terms")
        if self.broadcast_height > terms.success_broadcast_deadline:
            raise ChannelInputEscrowError("success spend missed its broadcast deadline")
        if self.broadcast_height - self.fulfillment_height > terms.preimage_budget_blocks:
            raise ChannelInputEscrowError("success spend exceeded its preimage handling budget")


class EscrowTimeoutBroadcast(BaseModel):
    """Evidence that the exact pre-signed timeout recovery was broadcast."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    parent_txid: str
    timeout_txid: str
    broadcast_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)

    @field_validator("parent_txid", "timeout_txid")
    @classmethod
    def validate_timeout_broadcast_txid(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="timeout broadcast transaction ID")


class RecoveryBroadcastResult(BaseModel):
    """Evidence of an endpoint recovery rebroadcast attempt."""

    model_config = _FROZEN_MODEL_CONFIG

    session_id: str = Field(min_length=1)
    parent_txid: str
    broadcast_txids: tuple[str, ...] = Field(min_length=1)
    broadcast_height: int = Field(ge=0, lt=_MAX_BLOCK_HEIGHT_LOCKTIME)

    @field_validator("parent_txid")
    @classmethod
    def validate_recovery_parent_txid(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="recovery parent transaction ID")

    @field_validator("broadcast_txids")
    @classmethod
    def validate_recovery_txids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for txid in value:
            _validate_hex(txid, byte_length=32, name="recovery transaction ID")
        if len(set(value)) != len(value):
            raise ValueError("recovery transaction IDs must be unique")
        return value


ChannelInputEscrowRecord.model_rebuild()


class RecoveryRequest(BaseModel):
    """Request to rebroadcast maker-local recovery artifacts after interruption."""

    model_config = _FROZEN_MODEL_CONFIG

    record: ChannelInputEscrowRecord

    @field_validator("record")
    @classmethod
    def snapshot_recovery_record(cls, value: ChannelInputEscrowRecord) -> ChannelInputEscrowRecord:
        return cast(ChannelInputEscrowRecord, value.model_copy(deep=True))


@runtime_checkable
class ChannelInputEscrowBackend(Protocol):
    """Endpoint operations required for the complete escrow lifecycle."""

    channel_input_escrow_capabilities: frozenset[ChannelInputEscrowCapability]

    async def freeze_quiesce_channel(self, request: FreezeChannelRequest) -> ChannelSnapshot: ...

    async def prepare_signed_timeout(
        self, request: PrepareTimeoutRequest
    ) -> PreparedChannelInputEscrow: ...

    async def authorize_parent(
        self, request: ParentAuthorizationRequest
    ) -> ParentAuthorization: ...

    async def broadcast_parent(self, request: ParentBroadcastRequest) -> EscrowParentBroadcast: ...

    async def observe_escrow_chain(
        self, request: ChainObservationRequest
    ) -> EscrowChainObservation: ...

    async def send_single_part_payment(
        self, request: SinglePartPaymentRequest
    ) -> SinglePartPaymentResult: ...

    async def observe_escrow_payment(
        self, request: PaymentObservationRequest
    ) -> SinglePartPaymentResult: ...

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

    async def resume_durable_sessions(self) -> list[ChannelInputEscrowRecord]: ...


def require_channel_input_escrow_backend(backend: object) -> ChannelInputEscrowBackend:
    """Require both the full capability declaration and concrete adapter methods."""
    try:
        require_channel_input_escrow_capabilities(backend)
    except LightningError as exc:
        raise ChannelInputEscrowError(str(exc)) from exc
    if not isinstance(backend, ChannelInputEscrowBackend):
        raise ChannelInputEscrowError("backend lacks the channel-input escrow adapter contract")
    return backend


class ChannelInputEscrowStore:
    """A directory of crash-safe, one-file-per-session escrow records."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        if not session_id:
            raise ChannelInputEscrowError("session ID cannot be empty")
        digest = hashlib.sha256(session_id.encode()).hexdigest()
        return self._dir / f"{digest}.json"

    @contextlib.contextmanager
    def _session_lock(self, session_id: str) -> Iterator[None]:
        lock_path = self._path(session_id).with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def save(
        self, record: ChannelInputEscrowRecord, *, expected_revision: int | None = None
    ) -> None:
        """Persist ``record`` atomically, rejecting stale compare-and-swap writers."""
        validated = ChannelInputEscrowRecord.model_validate(record.model_dump())
        path = self._path(validated.session_id)
        data = validated.model_dump_json()
        with self._session_lock(validated.session_id):
            if path.exists():
                persisted = ChannelInputEscrowRecord.model_validate_json(path.read_text())
                if validated.revision < persisted.revision:
                    raise ChannelInputEscrowError(
                        "refusing to overwrite newer escrow recovery state"
                    )
                if validated.revision == persisted.revision:
                    if validated == persisted:
                        return
                    raise ChannelInputEscrowError(
                        "escrow revision collision contains different state"
                    )
                if expected_revision is None or expected_revision != persisted.revision:
                    raise ChannelInputEscrowError(
                        "escrow compare-and-swap expected revision has changed"
                    )
            elif expected_revision is not None:
                raise ChannelInputEscrowError(
                    "cannot apply an expected revision to a new escrow session"
                )
            fd, temporary_path = tempfile.mkstemp(dir=self._dir, prefix=".escrow-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as file_handle:
                    file_handle.write(data)
                    file_handle.flush()
                    os.fsync(file_handle.fileno())
                os.replace(temporary_path, path)
                self._sync_directory()
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(temporary_path)
                raise

    def _sync_directory(self) -> None:
        directory_fd = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def load(self, session_id: str) -> ChannelInputEscrowRecord | None:
        """Load a session or return ``None`` when it has not been persisted."""
        path = self._path(session_id)
        if not path.exists():
            return None
        record = cast(
            ChannelInputEscrowRecord, ChannelInputEscrowRecord.model_validate_json(path.read_text())
        )
        if record.session_id != session_id:
            raise ChannelInputEscrowError("persisted escrow session does not match requested ID")
        return record

    def list(self) -> list[ChannelInputEscrowRecord]:
        """Return valid records, leaving any corrupt files untouched for recovery review."""
        records: list[ChannelInputEscrowRecord] = []
        for path in sorted(self._dir.glob("*.json")):
            try:
                record = cast(
                    ChannelInputEscrowRecord,
                    ChannelInputEscrowRecord.model_validate_json(path.read_text()),
                )
                if path != self._path(record.session_id):
                    continue
                records.append(record)
            except Exception:  # noqa: BLE001 - keep corrupt recovery artifacts intact
                continue
        return records
