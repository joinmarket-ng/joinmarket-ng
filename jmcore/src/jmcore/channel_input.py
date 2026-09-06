"""Canonical models and authenticated wire codec for channel-input protocol v1.

This module deliberately does not share the JMP-0010 domain used by the
co-funded channel-ring protocol.  Channel-input messages are exchanged inside
an authenticated JoinMarket session, but retain their own transcript and
BIP340 authentication so they can be persisted and independently verified.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, TypeAlias, TypeVar, cast

from bitcointx.core.key import CKey, CPubKey, XOnlyPubKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jmcore.bitcoin import (
    ParsedTransaction,
    estimate_vsize,
    hash256,
    parse_transaction_bytes,
    serialize_transaction,
)
from jmcore.constants import MAX_MONEY

CHANNEL_INPUT_VERSION = 1
CHANNEL_INPUT_DOMAIN = "JMCI001"
MAX_CHANNEL_INPUT_PAYLOAD_BYTES = 256 * 1024
MAX_CHANNEL_INPUT_ENCODED_BYTES = 4 * ((MAX_CHANNEL_INPUT_PAYLOAD_BYTES + 2) // 3) + 128
# NaCl Box adds a 24-byte nonce and 16-byte authenticator before base64 encoding.
MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES = 4 * ((MAX_CHANNEL_INPUT_ENCODED_BYTES + 40 + 2) // 3)
MAX_CHANNEL_INPUT_JSON_DEPTH = 12
MIN_PARTICIPANTS = 2
MAX_PARTICIPANTS = 32
MIN_CHANNELS = 1
MAX_CHANNELS = 32
MAX_FEE_RATE_SAT_VB = 10_000
MAX_ATTEMPT = 2_147_483_647
MAX_ESTIMATED_TRANSACTION_ITEMS = 252

Hex32 = str
Hex33 = str
Hex64 = str
Network: TypeAlias = Literal["mainnet", "testnet", "signet", "regtest"]
EndpointRole: TypeAlias = Literal["first", "second"]


class ChannelInputValidationError(ValueError):
    """Raised when a channel-input object cannot safely be accepted."""


class ChannelInputModel(BaseModel):
    """Strict immutable base used by every channel-input schema."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _validate_hex(value: str, byte_length: int | None, field_name: str) -> str:
    if not value or value.lower() != value or len(value) % 2:
        raise ValueError(f"{field_name} must be non-empty lowercase hexadecimal")
    if byte_length is not None and len(value) != byte_length * 2:
        raise ValueError(f"{field_name} must be {byte_length}-byte lowercase hexadecimal")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be lowercase hexadecimal") from exc
    return value


def _validate_xonly_key(value: str, field_name: str = "participant_key") -> str:
    _validate_hex(value, 32, field_name)
    try:
        if not XOnlyPubKey(bytes.fromhex(value)).is_fullyvalid():
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid BIP340 public key") from exc
    return value


def _validate_node_key(value: str, field_name: str = "node_key") -> str:
    _validate_hex(value, 33, field_name)
    raw = bytes.fromhex(value)
    if raw[0] not in (2, 3):
        raise ValueError(f"{field_name} must be a compressed secp256k1 public key")
    try:
        if not CPubKey(raw).is_fullyvalid():
            raise ValueError
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid secp256k1 public key") from exc
    return value


def _validate_script(value: str, field_name: str = "script_pubkey") -> str:
    _validate_hex(value, None, field_name)
    if len(value) > 20_000:
        raise ValueError(f"{field_name} exceeds 10000 bytes")
    return value


def _validate_p2tr_script(value: str, field_name: str = "settlement_script_pubkey") -> str:
    _validate_script(value, field_name)
    if len(value) != 68 or not value.startswith("5120"):
        raise ValueError(f"{field_name} must be a P2TR script_pubkey")
    return value


def _require_unique(values: Sequence[Any], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {field_name}")


def _native_segwit_script_type(script_pubkey: str, field_name: str) -> str:
    """Return the exact script type accepted by the v1 fee-rate estimator."""

    _validate_script(script_pubkey, field_name)
    if len(script_pubkey) == 44 and script_pubkey.startswith("0014"):
        return "p2wpkh"
    if len(script_pubkey) == 68 and script_pubkey.startswith("0020"):
        return "p2wsh"
    if len(script_pubkey) == 68 and script_pubkey.startswith("5120"):
        return "p2tr"
    raise ValueError(f"{field_name} must be a native P2WPKH, P2WSH, or P2TR script_pubkey")


class Outpoint(ChannelInputModel):
    """Canonical funding or parent input outpoint."""

    txid: Hex32
    vout: int = Field(ge=0, le=0xFFFFFFFF)

    @model_validator(mode="after")
    def validate_outpoint(self) -> Outpoint:
        _validate_hex(self.txid, 32, "txid")
        return self

    def key(self) -> tuple[str, int]:
        return self.txid, self.vout


class InputPrevout(ChannelInputModel):
    """The full prevout required to account for a parent transaction fee."""

    outpoint: Outpoint
    value_sat: int = Field(ge=1, le=MAX_MONEY)
    script_pubkey: str

    @model_validator(mode="after")
    def validate_prevout(self) -> InputPrevout:
        _native_segwit_script_type(self.script_pubkey, "prevout script_pubkey")
        return self


class SettlementOutput(ChannelInputModel):
    """One exact output assigned to a private endpoint term."""

    output_index: int = Field(ge=0, le=65_535)
    amount_sat: int = Field(ge=1, le=MAX_MONEY)
    script_pubkey: str

    @model_validator(mode="after")
    def validate_output(self) -> SettlementOutput:
        _validate_script(self.script_pubkey, "settlement script_pubkey")
        return self


class PolicyCeilings(ChannelInputModel):
    """Limits an endpoint is prepared to sign after its channel is frozen."""

    max_miner_fee_share_sat: int = Field(ge=0, le=MAX_MONEY)
    max_claim_adjustment_sat: int = Field(ge=0, le=MAX_MONEY)
    max_settlement_outputs: int = Field(ge=1, le=64)


class ChannelDescriptor(ChannelInputModel):
    """Public commitment to one channel input and its two opaque endpoint terms."""

    funding_outpoint: Outpoint
    input_index: int = Field(ge=0, le=65_535)
    capacity_sat: int = Field(ge=1, le=MAX_MONEY)
    first_participant_key: Hex32
    second_participant_key: Hex32
    first_terms_hash: Hex32
    second_terms_hash: Hex32

    @model_validator(mode="after")
    def validate_descriptor(self) -> ChannelDescriptor:
        _validate_xonly_key(self.first_participant_key, "first_participant_key")
        _validate_xonly_key(self.second_participant_key, "second_participant_key")
        _validate_hex(self.first_terms_hash, 32, "first_terms_hash")
        _validate_hex(self.second_terms_hash, 32, "second_terms_hash")
        if self.first_terms_hash == self.second_terms_hash:
            raise ValueError("channel descriptor terms hashes must differ")
        if self.first_participant_key == self.second_participant_key:
            raise ValueError("channel descriptor endpoint participant keys must differ")
        return self

    def terms_hashes(self) -> tuple[str, str]:
        return self.first_terms_hash, self.second_terms_hash


class EndpointTerms(ChannelInputModel):
    """Private endpoint authorization terms committed by a channel descriptor."""

    session_id: Hex32
    attempt: int = Field(ge=0, le=MAX_ATTEMPT)
    funding_outpoint: Outpoint
    input_index: int = Field(ge=0, le=65_535)
    participant_key: Hex32
    role: EndpointRole
    local_node_key: Hex33
    peer_node_key: Hex33
    capacity_sat: int = Field(ge=1, le=MAX_MONEY)
    frozen_local_claim_sat: int = Field(ge=0, le=MAX_MONEY)
    frozen_remote_claim_sat: int = Field(ge=0, le=MAX_MONEY)
    settlement_outputs: tuple[SettlementOutput, ...] = Field(min_length=1, max_length=64)
    miner_fee_share_sat: int = Field(ge=0, le=MAX_MONEY)
    claim_adjustment_sat: int = Field(ge=-MAX_MONEY, le=MAX_MONEY)
    policy_ceilings: PolicyCeilings

    @model_validator(mode="after")
    def validate_terms(self) -> EndpointTerms:
        _validate_hex(self.session_id, 32, "session_id")
        _validate_xonly_key(self.participant_key)
        _validate_node_key(self.local_node_key, "local_node_key")
        _validate_node_key(self.peer_node_key, "peer_node_key")
        if self.local_node_key == self.peer_node_key:
            raise ValueError("endpoint local and peer node keys must differ")
        if self.frozen_local_claim_sat + self.frozen_remote_claim_sat != self.capacity_sat:
            raise ValueError("frozen claims must conserve channel capacity")
        if self.miner_fee_share_sat > self.policy_ceilings.max_miner_fee_share_sat:
            raise ValueError("miner fee share exceeds endpoint policy ceiling")
        if abs(self.claim_adjustment_sat) > self.policy_ceilings.max_claim_adjustment_sat:
            raise ValueError("claim adjustment exceeds endpoint policy ceiling")
        if len(self.settlement_outputs) > self.policy_ceilings.max_settlement_outputs:
            raise ValueError("settlement output count exceeds endpoint policy ceiling")
        _require_unique(
            [output.output_index for output in self.settlement_outputs], "settlement output_index"
        )
        expected = (
            self.frozen_local_claim_sat - self.miner_fee_share_sat + self.claim_adjustment_sat
        )
        if (
            expected <= 0
            or sum(output.amount_sat for output in self.settlement_outputs) != expected
        ):
            raise ValueError(
                "settlement outputs do not match frozen claim, fee share, and adjustment"
            )
        return self


class ChannelInputTranscript(ChannelInputModel):
    """Global public commitment for one immutable parent transaction attempt."""

    v: Literal[1] = 1
    network: Network
    session_id: Hex32
    attempt: int = Field(ge=0, le=MAX_ATTEMPT)
    unsigned_parent_tx: str
    input_prevouts: tuple[InputPrevout, ...] = Field(
        min_length=1, max_length=MAX_ESTIMATED_TRANSACTION_ITEMS
    )
    total_fee_sat: int = Field(ge=0, le=MAX_MONEY)
    fee_rate_sat_vb: int = Field(ge=0, le=MAX_FEE_RATE_SAT_VB)
    expiry: int = Field(ge=1, le=0x7FFFFFFFFFFFFFFF)
    recovery_height: int = Field(ge=0, le=0xFFFFFFFF)
    participant_keys: tuple[Hex32, ...] = Field(
        min_length=MIN_PARTICIPANTS, max_length=MAX_PARTICIPANTS
    )
    coordinator_key: Hex32
    channels: tuple[ChannelDescriptor, ...] = Field(
        min_length=MIN_CHANNELS, max_length=MAX_CHANNELS
    )

    @model_validator(mode="after")
    def validate_transcript(self) -> ChannelInputTranscript:
        _validate_hex(self.session_id, 32, "session_id")
        for participant_key in self.participant_keys:
            _validate_xonly_key(participant_key)
        _require_unique(self.participant_keys, "participant_key")
        _validate_xonly_key(self.coordinator_key, "coordinator_key")
        if self.coordinator_key not in self.participant_keys:
            raise ValueError("coordinator_key must be in transcript roster")
        _require_unique(
            [prevout.outpoint.key() for prevout in self.input_prevouts], "input prevout"
        )
        _require_unique(
            [channel.funding_outpoint.key() for channel in self.channels],
            "channel funding outpoint",
        )
        _require_unique([channel.input_index for channel in self.channels], "channel input_index")
        term_hashes = [
            term_hash for channel in self.channels for term_hash in channel.terms_hashes()
        ]
        _require_unique(term_hashes, "endpoint terms hash")
        try:
            transaction = _parse_canonical_unsigned_transaction(self.unsigned_parent_tx)
        except ChannelInputValidationError as exc:
            raise ValueError(str(exc)) from exc
        if len(transaction.inputs) != len(self.input_prevouts):
            raise ValueError("input prevout count does not match unsigned parent transaction")
        if len(transaction.outputs) > MAX_ESTIMATED_TRANSACTION_ITEMS:
            raise ValueError("unsigned parent transaction has too many outputs for fee estimation")
        transaction_outpoints = [(item.txid, item.vout) for item in transaction.inputs]
        declared_outpoints = [item.outpoint.key() for item in self.input_prevouts]
        if transaction_outpoints != declared_outpoints:
            raise ValueError("input prevouts do not match unsigned parent transaction input order")
        for channel in self.channels:
            if (
                channel.first_participant_key not in self.participant_keys
                or channel.second_participant_key not in self.participant_keys
            ):
                raise ValueError("channel descriptor participant is absent from transcript roster")
            if channel.input_index >= len(self.input_prevouts):
                raise ValueError("channel input_index is outside unsigned parent transaction")
            if self.input_prevouts[channel.input_index].outpoint != channel.funding_outpoint:
                raise ValueError("channel funding outpoint does not match parent input")
            if self.input_prevouts[channel.input_index].value_sat != channel.capacity_sat:
                raise ValueError("channel capacity does not match funding prevout value")
        total_input = sum(prevout.value_sat for prevout in self.input_prevouts)
        total_output = sum(output.value for output in transaction.outputs)
        if total_input - total_output != self.total_fee_sat:
            raise ValueError("declared total fee does not match parent transaction prevouts")
        input_types = [
            _native_segwit_script_type(prevout.script_pubkey, "prevout script_pubkey")
            for prevout in self.input_prevouts
        ]
        output_types = [
            _native_segwit_script_type(output.script.hex(), "parent output script_pubkey")
            for output in transaction.outputs
        ]
        estimated_vsize = estimate_vsize(input_types, output_types)
        actual_fee_rate = (self.total_fee_sat + estimated_vsize - 1) // estimated_vsize
        if self.fee_rate_sat_vb != actual_fee_rate:
            raise ValueError(
                "declared fee rate does not match estimated parent transaction fee rate"
            )
        return self

    def all_terms_hashes(self) -> frozenset[str]:
        return frozenset(
            term_hash for channel in self.channels for term_hash in channel.terms_hashes()
        )

    def terms_hashes_for(self, participant_key: str) -> frozenset[str]:
        """Return the exact opaque endpoint commitments assigned to one roster key."""

        assignments: set[str] = set()
        for channel in self.channels:
            if channel.first_participant_key == participant_key:
                assignments.add(channel.first_terms_hash)
            if channel.second_participant_key == participant_key:
                assignments.add(channel.second_terms_hash)
        return frozenset(assignments)


class ChannelInputPayload(ChannelInputModel):
    """Fields authenticated by every channel-input wire envelope."""

    message_type: ClassVar[str]
    v: Literal[1] = 1
    type: str
    session_id: Hex32
    attempt: int = Field(ge=0, le=MAX_ATTEMPT)
    signer_key: Hex32
    sig: Hex64 = "00" * 64

    @model_validator(mode="after")
    def validate_common(self) -> ChannelInputPayload:
        if self.type != self.message_type:
            raise ValueError(f"payload type must be {self.message_type}")
        _validate_hex(self.session_id, 32, "session_id")
        _validate_xonly_key(self.signer_key, "signer_key")
        _validate_hex(self.sig, 64, "sig")
        return self


class ChannelInputRecoveryInitPayload(ChannelInputPayload):
    """Coordinator request to establish fresh process-local recovery encryption."""

    message_type: ClassVar[str] = "channel_input_recovery_init"
    type: Literal["channel_input_recovery_init"] = "channel_input_recovery_init"
    recovery_id: Hex32
    ephemeral_pubkey: Hex32
    expires_at: int = Field(ge=1, le=0x7FFFFFFFFFFFFFFF)

    @model_validator(mode="after")
    def validate_recovery_init(self) -> ChannelInputRecoveryInitPayload:
        _validate_hex(self.recovery_id, 32, "recovery_id")
        _validate_hex(self.ephemeral_pubkey, 32, "ephemeral_pubkey")
        if self.ephemeral_pubkey == "00" * 32:
            raise ValueError("ephemeral_pubkey must not be zero")
        return self


class ChannelInputRecoveryAcceptPayload(ChannelInputPayload):
    """Participant commitment to one exact recovery initiation and fresh key."""

    message_type: ClassVar[str] = "channel_input_recovery_accept"
    type: Literal["channel_input_recovery_accept"] = "channel_input_recovery_accept"
    recovery_id: Hex32
    init_hash: Hex32
    ephemeral_pubkey: Hex32

    @model_validator(mode="after")
    def validate_recovery_accept(self) -> ChannelInputRecoveryAcceptPayload:
        _validate_hex(self.recovery_id, 32, "recovery_id")
        _validate_hex(self.init_hash, 32, "init_hash")
        _validate_hex(self.ephemeral_pubkey, 32, "ephemeral_pubkey")
        if self.ephemeral_pubkey == "00" * 32:
            raise ValueError("ephemeral_pubkey must not be zero")
        return self


class ChannelInputRecoveryConfirmPayload(ChannelInputPayload):
    """Encrypted coordinator proof of possession for one accepted recovery key."""

    message_type: ClassVar[str] = "channel_input_recovery_confirm"
    type: Literal["channel_input_recovery_confirm"] = "channel_input_recovery_confirm"
    recovery_id: Hex32
    accept_hash: Hex32

    @model_validator(mode="after")
    def validate_recovery_confirm(self) -> ChannelInputRecoveryConfirmPayload:
        _validate_hex(self.recovery_id, 32, "recovery_id")
        _validate_hex(self.accept_hash, 32, "accept_hash")
        return self


class ChannelInputInvitePayload(ChannelInputPayload):
    message_type: ClassVar[str] = "channel_input_invite"
    type: Literal["channel_input_invite"] = "channel_input_invite"
    network: Network
    expiry: int = Field(ge=1, le=0x7FFFFFFFFFFFFFFF)
    max_parent_fee_sat: int = Field(ge=0, le=MAX_MONEY)
    max_fee_rate_sat_vb: int = Field(ge=0, le=MAX_FEE_RATE_SAT_VB)
    min_recovery_delta: int = Field(ge=0, le=0xFFFFFFFF)
    max_recovery_delta: int = Field(ge=0, le=0xFFFFFFFF)

    @model_validator(mode="after")
    def validate_recovery_bounds(self) -> ChannelInputInvitePayload:
        if self.min_recovery_delta > self.max_recovery_delta:
            raise ValueError("recovery delta bounds are invalid")
        return self


class ChannelInputHelloPayload(ChannelInputPayload):
    message_type: ClassVar[str] = "channel_input_hello"
    type: Literal["channel_input_hello"] = "channel_input_hello"
    participant_key: Hex32
    node_key: Hex33
    peer_node_key: Hex33
    funding_outpoint: Outpoint
    capacity_sat: int = Field(ge=1, le=MAX_MONEY)
    funding_script_pubkey: str
    frozen_local_claim_sat: int = Field(ge=0, le=MAX_MONEY)
    frozen_remote_claim_sat: int = Field(ge=0, le=MAX_MONEY)
    settlement_script_pubkey: str
    quoted_miner_fee_share_sat: int = Field(ge=0, le=MAX_MONEY)
    policy_ceilings: PolicyCeilings

    @model_validator(mode="after")
    def validate_hello(self) -> ChannelInputHelloPayload:
        _validate_xonly_key(self.participant_key)
        _validate_node_key(self.node_key)
        _validate_node_key(self.peer_node_key, "peer_node_key")
        if self.node_key == self.peer_node_key:
            raise ValueError("hello local and peer node keys must differ")
        _validate_p2tr_script(self.funding_script_pubkey, "funding_script_pubkey")
        if self.frozen_local_claim_sat + self.frozen_remote_claim_sat != self.capacity_sat:
            raise ValueError("hello frozen claims must conserve channel capacity")
        _validate_p2tr_script(self.settlement_script_pubkey)
        if self.signer_key != self.participant_key:
            raise ValueError("hello signer_key must match participant_key")
        if self.quoted_miner_fee_share_sat > self.policy_ceilings.max_miner_fee_share_sat:
            raise ValueError("quoted fee share exceeds endpoint policy ceiling")
        return self


class ChannelInputProposalPayload(ChannelInputPayload):
    message_type: ClassVar[str] = "channel_input_proposal"
    type: Literal["channel_input_proposal"] = "channel_input_proposal"
    transcript: ChannelInputTranscript
    transcript_hash: Hex32
    recipient_key: Hex32
    recipient_terms: tuple[EndpointTerms, ...] = Field(min_length=1, max_length=MAX_CHANNELS)

    @model_validator(mode="after")
    def validate_proposal(self) -> ChannelInputProposalPayload:
        if self.session_id != self.transcript.session_id or self.attempt != self.transcript.attempt:
            raise ValueError("proposal envelope does not match transcript session or attempt")
        if self.signer_key != self.transcript.coordinator_key:
            raise ValueError("proposal signer must match transcript coordinator")
        _validate_xonly_key(self.recipient_key, "recipient_key")
        if self.recipient_key not in self.transcript.participant_keys:
            raise ValueError("proposal recipient is not in transcript roster")
        if self.recipient_key == self.transcript.coordinator_key:
            raise ValueError("proposal recipient must differ from transcript coordinator")
        _validate_hex(self.transcript_hash, 32, "transcript_hash")
        if self.transcript_hash != transcript_hash(self.transcript).hex():
            raise ValueError("proposal transcript_hash does not match transcript")
        terms_hashes = [endpoint_terms_hash(terms).hex() for terms in self.recipient_terms]
        _require_unique(terms_hashes, "recipient terms hash")
        for terms, terms_hash in zip(self.recipient_terms, terms_hashes, strict=True):
            if terms.session_id != self.session_id or terms.attempt != self.attempt:
                raise ValueError("recipient terms do not match proposal session or attempt")
            if terms.participant_key != self.recipient_key:
                raise ValueError("recipient terms do not belong to proposal recipient")
            if terms_hash not in self.transcript.terms_hashes_for(self.recipient_key):
                raise ValueError("recipient terms hash is not assigned by transcript descriptor")
        if set(terms_hashes) != self.transcript.terms_hashes_for(self.recipient_key):
            raise ValueError("proposal recipient terms do not exactly match descriptor ownership")
        return self


class ChannelInputApprovalPayload(ChannelInputPayload):
    message_type: ClassVar[str] = "channel_input_approval"
    type: Literal["channel_input_approval"] = "channel_input_approval"
    transcript_hash: Hex32
    terms_hashes: tuple[Hex32, ...] = Field(max_length=MAX_CHANNELS)

    @model_validator(mode="after")
    def validate_approval(self) -> ChannelInputApprovalPayload:
        _validate_hex(self.transcript_hash, 32, "transcript_hash")
        for terms_hash in self.terms_hashes:
            _validate_hex(terms_hash, 32, "terms_hash")
        _require_unique(self.terms_hashes, "approval terms_hash")
        return self


class ChannelInputApprovalSetPayload(ChannelInputPayload):
    message_type: ClassVar[str] = "channel_input_approvals"
    type: Literal["channel_input_approvals"] = "channel_input_approvals"
    transcript: ChannelInputTranscript
    approvals: tuple[ChannelInputApprovalPayload, ...] = Field(
        min_length=MIN_PARTICIPANTS, max_length=MAX_PARTICIPANTS
    )

    @model_validator(mode="after")
    def validate_approval_set(self) -> ChannelInputApprovalSetPayload:
        if self.session_id != self.transcript.session_id or self.attempt != self.transcript.attempt:
            raise ValueError("approval set envelope does not match transcript session or attempt")
        if self.signer_key != self.transcript.coordinator_key:
            raise ValueError("approval set signer must match transcript coordinator")
        return self


class ChannelInputApprovalSetAckPayload(ChannelInputPayload):
    """Participant acknowledgment of one exact complete signed approval set."""

    message_type: ClassVar[str] = "channel_input_approval_ack"
    type: Literal["channel_input_approval_ack"] = "channel_input_approval_ack"
    transcript_hash: Hex32
    approval_set_hash: Hex32

    @model_validator(mode="after")
    def validate_ack(self) -> ChannelInputApprovalSetAckPayload:
        _validate_hex(self.transcript_hash, 32, "transcript_hash")
        _validate_hex(self.approval_set_hash, 32, "approval_set_hash")
        return self


class ChannelInputPreparePayload(ChannelInputPayload):
    """Coordinator authorization to begin backend preparation after all observations."""

    message_type: ClassVar[str] = "channel_input_prepare"
    type: Literal["channel_input_prepare"] = "channel_input_prepare"
    transcript_hash: Hex32
    approval_set_hash: Hex32
    approval_set: ChannelInputApprovalSetPayload
    approval_set_acks: tuple[ChannelInputApprovalSetAckPayload, ...] = Field(
        min_length=MIN_PARTICIPANTS, max_length=MAX_PARTICIPANTS
    )

    @model_validator(mode="after")
    def validate_prepare(self) -> ChannelInputPreparePayload:
        transcript = self.approval_set.transcript
        _validate_hex(self.transcript_hash, 32, "transcript_hash")
        _validate_hex(self.approval_set_hash, 32, "approval_set_hash")
        if self.session_id != transcript.session_id or self.attempt != transcript.attempt:
            raise ValueError("prepare envelope does not match approval set session or attempt")
        if self.transcript_hash != transcript_hash(transcript).hex():
            raise ValueError("prepare transcript_hash does not match approval set")
        if self.approval_set_hash != approval_set_hash(self.approval_set).hex():
            raise ValueError("prepare approval_set_hash does not match approval set")
        if self.signer_key != transcript.coordinator_key:
            raise ValueError("prepare signer must match transcript coordinator")
        return self


class ChannelInputChannelPayload(ChannelInputPayload):
    """Common channel-scoped post-approval authentication fields."""

    transcript_hash: Hex32
    funding_outpoint: Outpoint

    @model_validator(mode="after")
    def validate_channel_binding(self) -> ChannelInputChannelPayload:
        _validate_hex(self.transcript_hash, 32, "transcript_hash")
        return self


class ChannelInputStatusRequestPayload(ChannelInputChannelPayload):
    """Coordinator request for a channel endpoint's current prepare status."""

    message_type: ClassVar[str] = "channel_input_status_request"
    type: Literal["channel_input_status_request"] = "channel_input_status_request"
    query_id: Hex32

    @model_validator(mode="after")
    def validate_query_id(self) -> ChannelInputStatusRequestPayload:
        _validate_hex(self.query_id, 32, "query_id")
        return self


class ChannelInputPreparedPayload(ChannelInputChannelPayload):
    message_type: ClassVar[str] = "channel_input_prepared"
    type: Literal["channel_input_prepared"] = "channel_input_prepared"
    public_nonce: str

    @model_validator(mode="after")
    def validate_nonce(self) -> ChannelInputPreparedPayload:
        _validate_hex(self.public_nonce, 66, "public_nonce")
        return self


class ChannelInputSignPayload(ChannelInputChannelPayload):
    message_type: ClassVar[str] = "channel_input_sign"
    type: Literal["channel_input_sign"] = "channel_input_sign"
    local_public_nonce: str
    peer_prepared: ChannelInputPreparedPayload

    @model_validator(mode="after")
    def validate_nonce(self) -> ChannelInputSignPayload:
        _validate_hex(self.local_public_nonce, 66, "local_public_nonce")
        if (
            self.peer_prepared.session_id != self.session_id
            or self.peer_prepared.attempt != self.attempt
            or self.peer_prepared.transcript_hash != self.transcript_hash
            or self.peer_prepared.funding_outpoint != self.funding_outpoint
        ):
            raise ValueError("peer prepared does not match sign channel binding")
        if self.local_public_nonce == self.peer_prepared.public_nonce:
            raise ValueError("local and peer public nonces must differ")
        return self


class ChannelInputPartialPayload(ChannelInputChannelPayload):
    message_type: ClassVar[str] = "channel_input_partial"
    type: Literal["channel_input_partial"] = "channel_input_partial"
    local_public_nonce: str
    peer_public_nonce: str
    partial_signature: Hex32

    @model_validator(mode="after")
    def validate_partial(self) -> ChannelInputPartialPayload:
        _validate_hex(self.local_public_nonce, 66, "local_public_nonce")
        _validate_hex(self.peer_public_nonce, 66, "peer_public_nonce")
        _validate_hex(self.partial_signature, 32, "partial_signature")
        if self.local_public_nonce == self.peer_public_nonce:
            raise ValueError("local and peer public nonces must differ")
        return self


class ChannelInputFinalizePayload(ChannelInputChannelPayload):
    message_type: ClassVar[str] = "channel_input_finalize"
    type: Literal["channel_input_finalize"] = "channel_input_finalize"
    local_public_nonce: str
    peer_partial: ChannelInputPartialPayload

    @model_validator(mode="after")
    def validate_partial(self) -> ChannelInputFinalizePayload:
        _validate_hex(self.local_public_nonce, 66, "local_public_nonce")
        if (
            self.peer_partial.session_id != self.session_id
            or self.peer_partial.attempt != self.attempt
            or self.peer_partial.transcript_hash != self.transcript_hash
            or self.peer_partial.funding_outpoint != self.funding_outpoint
        ):
            raise ValueError("peer partial does not match finalize channel binding")
        if self.local_public_nonce != self.peer_partial.peer_public_nonce:
            raise ValueError("finalize local nonce does not match peer partial orientation")
        return self


class ChannelInputFinalizedPayload(ChannelInputChannelPayload):
    """Authenticated evidence of one channel's final Taproot parent witness."""

    message_type: ClassVar[str] = "channel_input_finalized"
    type: Literal["channel_input_finalized"] = "channel_input_finalized"
    channel_signed_parent_tx: str
    txid: Hex32

    @model_validator(mode="after")
    def validate_finalized(self) -> ChannelInputFinalizedPayload:
        _validate_hex(self.txid, 32, "txid")
        try:
            raw = bytes.fromhex(
                _validate_hex(self.channel_signed_parent_tx, None, "channel_signed_parent_tx")
            )
            transaction = parse_transaction_bytes(raw)
            canonical = serialize_transaction(
                transaction.version,
                transaction.inputs,
                transaction.outputs,
                transaction.locktime,
                transaction.witnesses if transaction.has_witness else None,
            )
        except (ValueError, IndexError) as exc:
            raise ValueError("channel_signed_parent_tx must be a canonical transaction") from exc
        if canonical != raw:
            raise ValueError("channel_signed_parent_tx must be canonical")
        if any(tx_input.scriptsig for tx_input in transaction.inputs):
            raise ValueError("channel_signed_parent_tx must not contain scriptSig data")
        if not transaction.has_witness or len(transaction.witnesses) != len(transaction.inputs):
            raise ValueError(
                "channel_signed_parent_tx must include a witness vector for every input"
            )
        nonempty_witnesses = [witness for witness in transaction.witnesses if witness]
        if len(nonempty_witnesses) != 1:
            raise ValueError("channel_signed_parent_tx must contain exactly one channel witness")
        channel_witness = nonempty_witnesses[0]
        if len(channel_witness) != 1 or len(channel_witness[0]) != 64:
            raise ValueError(
                "channel_signed_parent_tx channel witness must be one 64-byte Taproot signature"
            )
        unsigned = serialize_transaction(
            transaction.version,
            transaction.inputs,
            transaction.outputs,
            transaction.locktime,
            None,
        )
        if hash256(unsigned)[::-1].hex() != self.txid:
            raise ValueError("channel_signed_parent_tx txid does not match txid")
        return self


class ChannelInputStatusPayload(ChannelInputChannelPayload):
    """Signed endpoint evidence for one exact post-prepare status request."""

    message_type: ClassVar[str] = "channel_input_status"
    type: Literal["channel_input_status"] = "channel_input_status"
    query_id: Hex32
    status_request_hash: Hex32
    escrow_stage: Literal["parent_prepared", "parent_signed", "finalized"]
    attempt_active: bool
    prepared: ChannelInputPreparedPayload
    partial: ChannelInputPartialPayload | None = None
    finalized: ChannelInputFinalizedPayload | None = None

    @model_validator(mode="after")
    def validate_status(self) -> ChannelInputStatusPayload:
        _validate_hex(self.query_id, 32, "query_id")
        _validate_hex(self.status_request_hash, 32, "status_request_hash")
        for evidence in (self.prepared, self.partial, self.finalized):
            if evidence is not None and (
                evidence.session_id != self.session_id
                or evidence.attempt != self.attempt
                or evidence.transcript_hash != self.transcript_hash
                or evidence.funding_outpoint != self.funding_outpoint
                or evidence.signer_key != self.signer_key
            ):
                raise ValueError("status evidence does not match status channel binding or signer")
        if self.escrow_stage == "parent_prepared":
            if not self.attempt_active or self.partial is not None or self.finalized is not None:
                raise ValueError(
                    "parent_prepared status requires active attempt and prepared evidence only"
                )
        elif self.escrow_stage == "parent_signed":
            if self.partial is None or self.finalized is not None:
                raise ValueError("parent_signed status requires prepared and partial evidence only")
        elif self.partial is None or self.finalized is None or self.attempt_active:
            raise ValueError("finalized status requires inactive attempt and complete evidence")
        return self


class ChannelInputCancelPayload(ChannelInputPayload):
    message_type: ClassVar[str] = "channel_input_cancel"
    type: Literal["channel_input_cancel"] = "channel_input_cancel"
    transcript_hash: Hex32
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    partial_released: Literal[False] = False

    @model_validator(mode="after")
    def validate_cancel(self) -> ChannelInputCancelPayload:
        _validate_hex(self.transcript_hash, 32, "transcript_hash")
        return self


class ChannelInputCancelAckPayload(ChannelInputPayload):
    """Maker acknowledgment that every listed channel has resumed after cancellation."""

    message_type: ClassVar[str] = "channel_input_cancel_ack"
    type: Literal["channel_input_cancel_ack"] = "channel_input_cancel_ack"
    transcript_hash: Hex32
    cancel_hash: Hex32
    resumed_outpoints: tuple[Outpoint, ...] = Field(min_length=1, max_length=MAX_CHANNELS)

    @model_validator(mode="after")
    def validate_cancel_ack(self) -> ChannelInputCancelAckPayload:
        _validate_hex(self.transcript_hash, 32, "transcript_hash")
        _validate_hex(self.cancel_hash, 32, "cancel_hash")
        outpoints = tuple(outpoint.key() for outpoint in self.resumed_outpoints)
        _require_unique(outpoints, "resumed outpoint")
        if outpoints != tuple(sorted(outpoints)):
            raise ValueError("resumed outpoints must use stable lexicographic order")
        return self


ChannelInputPayloadType: TypeAlias = (
    ChannelInputRecoveryInitPayload
    | ChannelInputRecoveryAcceptPayload
    | ChannelInputRecoveryConfirmPayload
    | ChannelInputInvitePayload
    | ChannelInputHelloPayload
    | ChannelInputProposalPayload
    | ChannelInputApprovalPayload
    | ChannelInputApprovalSetPayload
    | ChannelInputApprovalSetAckPayload
    | ChannelInputPreparePayload
    | ChannelInputStatusRequestPayload
    | ChannelInputPreparedPayload
    | ChannelInputSignPayload
    | ChannelInputPartialPayload
    | ChannelInputFinalizePayload
    | ChannelInputFinalizedPayload
    | ChannelInputStatusPayload
    | ChannelInputCancelPayload
    | ChannelInputCancelAckPayload
)

ChannelInputPayloadT = TypeVar("ChannelInputPayloadT", bound=ChannelInputPayloadType)

_PAYLOAD_TYPES: dict[str, type[ChannelInputPayload]] = {
    payload_type.message_type: payload_type
    for payload_type in (
        ChannelInputRecoveryInitPayload,
        ChannelInputRecoveryAcceptPayload,
        ChannelInputRecoveryConfirmPayload,
        ChannelInputInvitePayload,
        ChannelInputHelloPayload,
        ChannelInputProposalPayload,
        ChannelInputApprovalPayload,
        ChannelInputApprovalSetPayload,
        ChannelInputApprovalSetAckPayload,
        ChannelInputPreparePayload,
        ChannelInputStatusRequestPayload,
        ChannelInputPreparedPayload,
        ChannelInputSignPayload,
        ChannelInputPartialPayload,
        ChannelInputFinalizePayload,
        ChannelInputFinalizedPayload,
        ChannelInputStatusPayload,
        ChannelInputCancelPayload,
        ChannelInputCancelAckPayload,
    )
}


def _parse_canonical_unsigned_transaction(unsigned_tx: str) -> ParsedTransaction:
    try:
        raw = bytes.fromhex(_validate_hex(unsigned_tx, None, "unsigned_parent_tx"))
        transaction = parse_transaction_bytes(raw)
        canonical = serialize_transaction(
            transaction.version, transaction.inputs, transaction.outputs, transaction.locktime, None
        )
    except (ValueError, IndexError) as exc:
        raise ChannelInputValidationError(
            "unsigned_parent_tx must be a canonical unsigned transaction"
        ) from exc
    if (
        transaction.has_witness
        or any(item.scriptsig for item in transaction.inputs)
        or canonical != raw
    ):
        raise ChannelInputValidationError("unsigned_parent_tx must be canonical and unsigned")
    if not transaction.inputs:
        raise ChannelInputValidationError("unsigned_parent_tx must have at least one input")
    outpoints = [(item.txid, item.vout) for item in transaction.inputs]
    if len(set(outpoints)) != len(outpoints):
        raise ChannelInputValidationError("unsigned_parent_tx has duplicate input outpoints")
    return transaction


def validate_transcript_against_transaction(
    transcript: ChannelInputTranscript,
    unsigned_parent_tx: bytes,
    input_prevouts: Sequence[InputPrevout],
) -> None:
    """Bind a transcript to externally supplied exact unsigned bytes and prevouts."""

    if unsigned_parent_tx.hex() != transcript.unsigned_parent_tx:
        raise ChannelInputValidationError("unsigned parent transaction differs from transcript")
    try:
        supplied = tuple(input_prevouts)
    except TypeError as exc:
        raise ChannelInputValidationError("input prevouts must be a sequence") from exc
    if supplied != transcript.input_prevouts:
        raise ChannelInputValidationError("input prevouts differ from transcript")
    try:
        _parse_canonical_unsigned_transaction(unsigned_parent_tx.hex())
    except ChannelInputValidationError:
        raise


def validate_revealed_endpoint_pair(
    transcript: ChannelInputTranscript,
    descriptor: ChannelDescriptor,
    first: EndpointTerms,
    second: EndpointTerms,
) -> None:
    """Validate the two private terms for one revealed channel pair."""

    if descriptor not in transcript.channels:
        raise ChannelInputValidationError("channel descriptor is absent from transcript")
    if first.role != "first" or second.role != "second":
        raise ChannelInputValidationError("revealed endpoint terms have invalid roles")
    if endpoint_terms_hash(first).hex() != descriptor.first_terms_hash:
        raise ChannelInputValidationError("first endpoint terms hash does not match descriptor")
    if endpoint_terms_hash(second).hex() != descriptor.second_terms_hash:
        raise ChannelInputValidationError("second endpoint terms hash does not match descriptor")
    if first.participant_key != descriptor.first_participant_key:
        raise ChannelInputValidationError("first endpoint participant does not match descriptor")
    if second.participant_key != descriptor.second_participant_key:
        raise ChannelInputValidationError("second endpoint participant does not match descriptor")
    for terms in (first, second):
        if (
            terms.session_id != transcript.session_id
            or terms.attempt != transcript.attempt
            or terms.funding_outpoint != descriptor.funding_outpoint
            or terms.input_index != descriptor.input_index
            or terms.capacity_sat != descriptor.capacity_sat
        ):
            raise ChannelInputValidationError(
                "endpoint terms do not match transcript channel descriptor"
            )
        if terms.participant_key not in transcript.participant_keys:
            raise ChannelInputValidationError(
                "endpoint participant is absent from transcript roster"
            )
    if first.participant_key == second.participant_key:
        raise ChannelInputValidationError("channel endpoints must have distinct participants")
    if first.local_node_key != second.peer_node_key or second.local_node_key != first.peer_node_key:
        raise ChannelInputValidationError("endpoint peer identities are not mirrored")
    if (
        first.frozen_local_claim_sat != second.frozen_remote_claim_sat
        or first.frozen_remote_claim_sat != second.frozen_local_claim_sat
    ):
        raise ChannelInputValidationError("endpoint frozen claims are not mirrored")
    if first.claim_adjustment_sat + second.claim_adjustment_sat != 0:
        raise ChannelInputValidationError("endpoint claim adjustments must sum to zero")
    output_indices = [
        output.output_index for terms in (first, second) for output in terms.settlement_outputs
    ]
    if len(set(output_indices)) != len(output_indices):
        raise ChannelInputValidationError("duplicate settlement output_index across endpoint terms")
    transaction = _parse_canonical_unsigned_transaction(transcript.unsigned_parent_tx)
    for terms in (first, second):
        for settlement_output in terms.settlement_outputs:
            if settlement_output.output_index >= len(transaction.outputs):
                raise ChannelInputValidationError(
                    "settlement output_index is outside parent transaction"
                )
            parent_output = transaction.outputs[settlement_output.output_index]
            if (
                settlement_output.amount_sat != parent_output.value
                or settlement_output.script_pubkey != parent_output.script.hex()
            ):
                raise ChannelInputValidationError(
                    "settlement output does not match parent transaction output"
                )


def validate_hello_for_invite(
    invite: ChannelInputInvitePayload, hello: ChannelInputHelloPayload
) -> None:
    """Verify that a private candidate belongs to the invitation attempt."""

    if not verify_payload(invite):
        raise ChannelInputValidationError("invite BIP340 signature is invalid")
    if not verify_payload(hello):
        raise ChannelInputValidationError("hello BIP340 signature is invalid")
    if invite.session_id != hello.session_id or invite.attempt != hello.attempt:
        raise ChannelInputValidationError("hello does not match invitation session or attempt")
    if hello.quoted_miner_fee_share_sat > invite.max_parent_fee_sat:
        raise ChannelInputValidationError("hello quoted fee exceeds invitation parent fee")


def validate_hello_pair(first: ChannelInputHelloPayload, second: ChannelInputHelloPayload) -> None:
    """Verify that two signed frozen-channel hello claims are reciprocal."""

    if not verify_payload(first):
        raise ChannelInputValidationError("first hello BIP340 signature is invalid")
    if not verify_payload(second):
        raise ChannelInputValidationError("second hello BIP340 signature is invalid")
    if first.session_id != second.session_id or first.attempt != second.attempt:
        raise ChannelInputValidationError("hello pair session or attempt does not match")
    if first.funding_outpoint != second.funding_outpoint:
        raise ChannelInputValidationError("hello pair funding outpoint does not match")
    if first.capacity_sat != second.capacity_sat:
        raise ChannelInputValidationError("hello pair capacity does not match")
    if first.funding_script_pubkey != second.funding_script_pubkey:
        raise ChannelInputValidationError("hello pair funding script does not match")
    if first.participant_key == second.participant_key:
        raise ChannelInputValidationError("hello pair participants must differ")
    if first.node_key != second.peer_node_key or second.node_key != first.peer_node_key:
        raise ChannelInputValidationError("hello pair node identities are not reciprocal")
    if (
        first.frozen_local_claim_sat != second.frozen_remote_claim_sat
        or first.frozen_remote_claim_sat != second.frozen_local_claim_sat
    ):
        raise ChannelInputValidationError("hello pair frozen claims are not reciprocal")


def validate_transcript_for_invite(
    invite: ChannelInputInvitePayload,
    transcript: ChannelInputTranscript,
    *,
    current_time: int,
    current_height: int,
) -> None:
    """Apply invitation fee, expiry, network, and recovery-height limits to a transcript."""

    if current_time < 0:
        raise ChannelInputValidationError("current_time must be non-negative")
    if not 0 <= current_height <= 0xFFFFFFFF:
        raise ChannelInputValidationError("current_height must be a uint32")
    if not verify_payload(invite):
        raise ChannelInputValidationError("invite BIP340 signature is invalid")
    if current_time >= invite.expiry:
        raise ChannelInputValidationError("invite has expired")
    if current_time >= transcript.expiry:
        raise ChannelInputValidationError("transcript has expired")
    if invite.session_id != transcript.session_id or invite.attempt != transcript.attempt:
        raise ChannelInputValidationError("transcript does not match invitation session or attempt")
    if invite.signer_key != transcript.coordinator_key:
        raise ChannelInputValidationError("invite signer does not match transcript coordinator")
    if invite.network != transcript.network:
        raise ChannelInputValidationError("transcript network does not match invitation")
    if transcript.expiry > invite.expiry:
        raise ChannelInputValidationError("transcript expiry exceeds invitation")
    if transcript.total_fee_sat > invite.max_parent_fee_sat:
        raise ChannelInputValidationError("transcript fee exceeds invitation maximum")
    if transcript.fee_rate_sat_vb > invite.max_fee_rate_sat_vb:
        raise ChannelInputValidationError("transcript fee rate exceeds invitation maximum")
    recovery_delta = transcript.recovery_height - current_height
    if not invite.min_recovery_delta <= recovery_delta <= invite.max_recovery_delta:
        raise ChannelInputValidationError("transcript recovery height is outside invitation bounds")


def validate_proposal(proposal: ChannelInputProposalPayload) -> None:
    """Re-run proposal checks at a trust boundary with protocol-specific errors."""

    if not verify_payload(proposal):
        raise ChannelInputValidationError("proposal BIP340 signature is invalid")
    if proposal.signer_key != proposal.transcript.coordinator_key:
        raise ChannelInputValidationError("proposal signer does not match transcript coordinator")


def validate_proposal_for_hellos(
    invite: ChannelInputInvitePayload,
    hellos: Sequence[ChannelInputHelloPayload],
    proposal: ChannelInputProposalPayload,
    *,
    current_time: int,
    current_height: int,
) -> None:
    """Bind a recipient proposal to its prior signed channel hello claims."""

    if not isinstance(hellos, Sequence) or isinstance(hellos, str | bytes):
        raise ChannelInputValidationError("hellos must be a finite sequence")
    if not hellos:
        raise ChannelInputValidationError("hellos must not be empty")

    validate_proposal(proposal)
    validate_transcript_for_invite(
        invite,
        proposal.transcript,
        current_time=current_time,
        current_height=current_height,
    )

    hello_outpoints: set[tuple[str, int]] = set()
    hellos_by_outpoint: dict[tuple[str, int], ChannelInputHelloPayload] = {}
    for hello in hellos:
        if not isinstance(hello, ChannelInputHelloPayload):
            raise ChannelInputValidationError("hellos must contain hello payloads")
        validate_hello_for_invite(invite, hello)
        if (
            hello.participant_key != proposal.recipient_key
            or hello.signer_key != proposal.recipient_key
        ):
            raise ChannelInputValidationError("hello participant does not match proposal recipient")
        outpoint = hello.funding_outpoint.key()
        if outpoint in hello_outpoints:
            raise ChannelInputValidationError("duplicate hello funding outpoint")
        hello_outpoints.add(outpoint)
        hellos_by_outpoint[outpoint] = hello

    terms_outpoints = [terms.funding_outpoint.key() for terms in proposal.recipient_terms]
    if len(set(terms_outpoints)) != len(terms_outpoints):
        raise ChannelInputValidationError(
            "proposal recipient terms have duplicate funding outpoints"
        )
    if hello_outpoints != set(terms_outpoints):
        raise ChannelInputValidationError(
            "hello funding outpoints do not exactly match proposal terms"
        )

    for terms in proposal.recipient_terms:
        hello = hellos_by_outpoint[terms.funding_outpoint.key()]
        try:
            prevout = proposal.transcript.input_prevouts[terms.input_index]
        except IndexError as exc:
            raise ChannelInputValidationError(
                "proposal term input_index is outside transcript prevouts"
            ) from exc
        if prevout.outpoint != hello.funding_outpoint:
            raise ChannelInputValidationError("proposal transcript outpoint does not match hello")
        if prevout.value_sat != hello.capacity_sat:
            raise ChannelInputValidationError("proposal transcript capacity does not match hello")
        if prevout.script_pubkey != hello.funding_script_pubkey:
            raise ChannelInputValidationError(
                "proposal transcript funding script does not match hello"
            )
        if terms.capacity_sat != hello.capacity_sat:
            raise ChannelInputValidationError("proposal term capacity does not match hello")
        if terms.local_node_key != hello.node_key:
            raise ChannelInputValidationError("proposal term local node key does not match hello")
        if terms.peer_node_key != hello.peer_node_key:
            raise ChannelInputValidationError("proposal term peer node key does not match hello")
        if (
            terms.frozen_local_claim_sat != hello.frozen_local_claim_sat
            or terms.frozen_remote_claim_sat != hello.frozen_remote_claim_sat
        ):
            raise ChannelInputValidationError("proposal term frozen claims do not match hello")
        if terms.policy_ceilings != hello.policy_ceilings:
            raise ChannelInputValidationError("proposal term policy ceilings do not match hello")
        if terms.miner_fee_share_sat > hello.quoted_miner_fee_share_sat:
            raise ChannelInputValidationError("proposal term miner fee share exceeds hello quote")
        if any(
            output.script_pubkey != hello.settlement_script_pubkey
            for output in terms.settlement_outputs
        ):
            raise ChannelInputValidationError(
                "proposal term settlement script does not match hello"
            )


def validate_complete_approval_set(approval_set: ChannelInputApprovalSetPayload) -> None:
    """Require exactly one valid approval per roster key and endpoint terms hash."""

    transcript = approval_set.transcript
    expected_transcript_hash = transcript_hash(transcript).hex()
    if not verify_payload(approval_set):
        raise ChannelInputValidationError("approval set BIP340 signature is invalid")
    if approval_set.signer_key != transcript.coordinator_key:
        raise ChannelInputValidationError(
            "approval set signer does not match transcript coordinator"
        )
    if len(approval_set.approvals) != len(transcript.participant_keys):
        raise ChannelInputValidationError(
            "approval set must cover every roster participant exactly once"
        )
    actual_signers = [approval.signer_key for approval in approval_set.approvals]
    if set(actual_signers) != set(transcript.participant_keys) or len(set(actual_signers)) != len(
        actual_signers
    ):
        raise ChannelInputValidationError(
            "approval set must cover every roster participant exactly once"
        )
    actual_terms: list[str] = []
    for approval in approval_set.approvals:
        if (
            approval.session_id != transcript.session_id
            or approval.attempt != transcript.attempt
            or approval.transcript_hash != expected_transcript_hash
        ):
            raise ChannelInputValidationError(
                "approval does not bind this transcript session or attempt"
            )
        if not verify_payload(approval):
            raise ChannelInputValidationError("approval BIP340 signature is invalid")
        if set(approval.terms_hashes) != transcript.terms_hashes_for(approval.signer_key):
            raise ChannelInputValidationError(
                "approval terms hashes do not match transcript descriptor ownership"
            )
        actual_terms.extend(approval.terms_hashes)
    if (
        len(actual_terms) != len(set(actual_terms))
        or set(actual_terms) != transcript.all_terms_hashes()
    ):
        raise ChannelInputValidationError(
            "approval set must cover every endpoint terms hash exactly once"
        )


def validate_recipient_approval(
    approval_set: ChannelInputApprovalSetPayload,
    recipient_key: str,
    local_terms: Sequence[EndpointTerms],
) -> None:
    """Verify a recipient's exact local approval in a complete signed approval set."""

    transcript = approval_set.transcript
    _validate_xonly_key(recipient_key, "recipient_key")
    if recipient_key not in transcript.participant_keys:
        raise ChannelInputValidationError("recipient is absent from transcript roster")
    supplied_hashes = [endpoint_terms_hash(terms).hex() for terms in local_terms]
    if len(supplied_hashes) != len(set(supplied_hashes)):
        raise ChannelInputValidationError("duplicate supplied local terms hash")
    expected_hashes = transcript.terms_hashes_for(recipient_key)
    if set(supplied_hashes) != expected_hashes:
        raise ChannelInputValidationError("supplied local terms do not match descriptor ownership")
    for terms, terms_hash in zip(local_terms, supplied_hashes, strict=True):
        if (
            terms.session_id != transcript.session_id
            or terms.attempt != transcript.attempt
            or terms.participant_key != recipient_key
            or terms_hash not in expected_hashes
        ):
            raise ChannelInputValidationError(
                "supplied local terms do not bind transcript recipient"
            )
    validate_complete_approval_set(approval_set)
    approval = next(
        (item for item in approval_set.approvals if item.signer_key == recipient_key), None
    )
    if approval is None or set(approval.terms_hashes) != expected_hashes:
        raise ChannelInputValidationError("approval set omits recipient exact local approval")


def validate_prepare_payload(prepare: ChannelInputPreparePayload) -> None:
    """Require all roster members to acknowledge one complete approval set exactly once."""

    approval_set = prepare.approval_set
    transcript = approval_set.transcript
    expected_transcript_hash = transcript_hash(transcript).hex()
    expected_approval_set_hash = approval_set_hash(approval_set).hex()
    if not verify_payload(prepare):
        raise ChannelInputValidationError("prepare BIP340 signature is invalid")
    validate_complete_approval_set(approval_set)
    if prepare.signer_key != transcript.coordinator_key:
        raise ChannelInputValidationError("prepare signer does not match transcript coordinator")
    if (
        prepare.session_id != transcript.session_id
        or prepare.attempt != transcript.attempt
        or prepare.transcript_hash != expected_transcript_hash
        or prepare.approval_set_hash != expected_approval_set_hash
    ):
        raise ChannelInputValidationError("prepare does not bind the exact approval set")
    if len(prepare.approval_set_acks) != len(transcript.participant_keys):
        raise ChannelInputValidationError(
            "prepare must contain one approval-set ACK per roster key"
        )
    ack_signers = [ack.signer_key for ack in prepare.approval_set_acks]
    if set(ack_signers) != set(transcript.participant_keys) or len(set(ack_signers)) != len(
        ack_signers
    ):
        raise ChannelInputValidationError(
            "prepare must contain one approval-set ACK per roster key"
        )
    for ack in prepare.approval_set_acks:
        if not verify_payload(ack):
            raise ChannelInputValidationError("approval-set ACK BIP340 signature is invalid")
        if (
            ack.session_id != transcript.session_id
            or ack.attempt != transcript.attempt
            or ack.transcript_hash != expected_transcript_hash
            or ack.approval_set_hash != expected_approval_set_hash
        ):
            raise ChannelInputValidationError(
                "approval-set ACK does not bind the exact approval set"
            )


def validate_channel_payload_for_transcript(
    payload: ChannelInputChannelPayload | ChannelInputCancelPayload,
    transcript: ChannelInputTranscript,
) -> None:
    """Bind a signed channel-scoped payload to one committed transcript."""

    if not verify_payload(payload):
        raise ChannelInputValidationError("channel payload BIP340 signature is invalid")
    if payload.session_id != transcript.session_id or payload.attempt != transcript.attempt:
        raise ChannelInputValidationError(
            "channel payload does not match transcript session or attempt"
        )
    if payload.transcript_hash != transcript_hash(transcript).hex():
        raise ChannelInputValidationError("channel payload transcript hash does not match")
    if isinstance(payload, ChannelInputChannelPayload) and payload.funding_outpoint not in {
        channel.funding_outpoint for channel in transcript.channels
    }:
        raise ChannelInputValidationError(
            "channel payload funding outpoint is absent from transcript"
        )
    if isinstance(payload, ChannelInputFinalizedPayload):
        validate_finalized_for_transcript(payload, transcript)


def validate_status_request_for_transcript(
    request: ChannelInputStatusRequestPayload,
    transcript: ChannelInputTranscript,
    expected_coordinator_key: str,
) -> None:
    """Verify a coordinator's exact authenticated request for one channel status."""

    try:
        _validate_xonly_key(expected_coordinator_key, "expected_coordinator_key")
    except ValueError as exc:
        raise ChannelInputValidationError(str(exc)) from exc
    validate_channel_payload_for_transcript(request, transcript)
    if expected_coordinator_key != transcript.coordinator_key:
        raise ChannelInputValidationError(
            "expected coordinator does not match transcript coordinator"
        )
    if request.signer_key != expected_coordinator_key:
        raise ChannelInputValidationError("status request signer does not match coordinator")


def validate_status_for_transcript(
    status: ChannelInputStatusPayload,
    request: ChannelInputStatusRequestPayload,
    transcript: ChannelInputTranscript,
    expected_participant_key: str,
) -> None:
    """Verify an endpoint's signed recovery status and nested signing evidence."""

    validate_status_request_for_transcript(request, transcript, transcript.coordinator_key)
    try:
        _validate_xonly_key(expected_participant_key, "expected_participant_key")
    except ValueError as exc:
        raise ChannelInputValidationError(str(exc)) from exc
    validate_channel_payload_for_transcript(status, transcript)
    descriptor = _descriptor_for_outpoint(transcript, status.funding_outpoint)
    _opposite_endpoint_key(descriptor, expected_participant_key)
    if status.signer_key != expected_participant_key:
        raise ChannelInputValidationError("status signer does not match channel participant")
    if (
        request.session_id != status.session_id
        or request.attempt != status.attempt
        or request.transcript_hash != status.transcript_hash
        or request.funding_outpoint != status.funding_outpoint
    ):
        raise ChannelInputValidationError("status does not bind status request channel envelope")
    if status.query_id != request.query_id:
        raise ChannelInputValidationError("status query_id does not match status request")
    if status.status_request_hash != payload_hash(request).hex():
        raise ChannelInputValidationError("status does not bind the exact signed status request")
    validate_prepared_for_transcript(status.prepared, transcript, expected_participant_key)
    if status.partial is not None:
        validate_partial_for_transcript(
            status.partial,
            transcript,
            expected_participant_key,
            expected_local_public_nonce=status.prepared.public_nonce,
            expected_peer_public_nonce=status.partial.peer_public_nonce,
        )
    if status.finalized is not None:
        validate_channel_payload_for_transcript(status.finalized, transcript)
        validate_finalized_for_transcript(status.finalized, transcript)


def validate_cancel_ack(
    cancel: ChannelInputCancelPayload,
    ack: ChannelInputCancelAckPayload,
    expected_signer_key: str,
    expected_outpoints: Sequence[Outpoint],
) -> None:
    """Verify a maker's signed replayable acknowledgment of one exact cancellation."""

    if not verify_payload(cancel):
        raise ChannelInputValidationError("cancel BIP340 signature is invalid")
    if not verify_payload(ack):
        raise ChannelInputValidationError("cancel ACK BIP340 signature is invalid")
    try:
        _validate_xonly_key(expected_signer_key, "expected_signer_key")
    except ValueError as exc:
        raise ChannelInputValidationError(str(exc)) from exc
    try:
        expected = tuple(expected_outpoints)
    except TypeError as exc:
        raise ChannelInputValidationError("expected_outpoints must be a sequence") from exc
    if not all(isinstance(outpoint, Outpoint) for outpoint in expected):
        raise ChannelInputValidationError("expected_outpoints must contain outpoints")
    expected_keys = tuple(outpoint.key() for outpoint in expected)
    if len(set(expected_keys)) != len(expected_keys):
        raise ChannelInputValidationError("expected outpoints contain duplicates")
    if expected_keys != tuple(sorted(expected_keys)):
        raise ChannelInputValidationError("expected outpoints must use stable lexicographic order")
    if (
        ack.session_id != cancel.session_id
        or ack.attempt != cancel.attempt
        or ack.transcript_hash != cancel.transcript_hash
    ):
        raise ChannelInputValidationError(
            "cancel ACK does not bind cancel session, attempt, or transcript"
        )
    if ack.signer_key != expected_signer_key:
        raise ChannelInputValidationError("cancel ACK signer does not match expected signer")
    if ack.cancel_hash != cancel_payload_hash(cancel).hex():
        raise ChannelInputValidationError("cancel ACK does not bind the exact signed cancel")
    if tuple(outpoint.key() for outpoint in ack.resumed_outpoints) != expected_keys:
        raise ChannelInputValidationError(
            "cancel ACK resumed outpoints do not exactly match expected"
        )


def _descriptor_for_outpoint(
    transcript: ChannelInputTranscript, funding_outpoint: Outpoint
) -> ChannelDescriptor:
    descriptors = [
        descriptor
        for descriptor in transcript.channels
        if descriptor.funding_outpoint == funding_outpoint
    ]
    if len(descriptors) != 1:
        raise ChannelInputValidationError(
            "channel payload funding outpoint must identify exactly one descriptor"
        )
    return descriptors[0]


def _opposite_endpoint_key(descriptor: ChannelDescriptor, participant_key: str) -> str:
    if participant_key == descriptor.first_participant_key:
        return descriptor.second_participant_key
    if participant_key == descriptor.second_participant_key:
        return descriptor.first_participant_key
    raise ChannelInputValidationError("channel participant is not an endpoint in the descriptor")


def _validate_expected_public_nonce(value: str, field_name: str) -> None:
    try:
        _validate_hex(value, 66, field_name)
    except ValueError as exc:
        raise ChannelInputValidationError(str(exc)) from exc


def validate_prepared_for_transcript(
    payload: ChannelInputPreparedPayload,
    transcript: ChannelInputTranscript,
    participant_key: str,
) -> None:
    """Verify a prepared nonce came from one descriptor endpoint."""

    validate_channel_payload_for_transcript(payload, transcript)
    descriptor = _descriptor_for_outpoint(transcript, payload.funding_outpoint)
    if payload.signer_key != participant_key:
        raise ChannelInputValidationError("prepared signer does not match channel participant")
    _opposite_endpoint_key(descriptor, participant_key)


def validate_sign_for_transcript(
    payload: ChannelInputSignPayload,
    transcript: ChannelInputTranscript,
    recipient_key: str,
    *,
    expected_local_public_nonce: str,
) -> None:
    """Verify a coordinator sign authorization binds current endpoint nonce evidence."""

    validate_channel_payload_for_transcript(payload, transcript)
    descriptor = _descriptor_for_outpoint(transcript, payload.funding_outpoint)
    if payload.signer_key != transcript.coordinator_key:
        raise ChannelInputValidationError("sign signer does not match transcript coordinator")
    peer_key = _opposite_endpoint_key(descriptor, recipient_key)
    _validate_expected_public_nonce(expected_local_public_nonce, "expected_local_public_nonce")
    if payload.local_public_nonce != expected_local_public_nonce:
        raise ChannelInputValidationError("sign local nonce does not match recipient current nonce")
    validate_prepared_for_transcript(payload.peer_prepared, transcript, peer_key)
    if payload.local_public_nonce == payload.peer_prepared.public_nonce:
        raise ChannelInputValidationError("sign local and peer public nonces must differ")


def validate_partial_for_transcript(
    payload: ChannelInputPartialPayload,
    transcript: ChannelInputTranscript,
    participant_key: str,
    *,
    expected_local_public_nonce: str,
    expected_peer_public_nonce: str,
) -> None:
    """Verify a participant partial signature binds the exact ordered nonce pair."""

    validate_channel_payload_for_transcript(payload, transcript)
    descriptor = _descriptor_for_outpoint(transcript, payload.funding_outpoint)
    if payload.signer_key != participant_key:
        raise ChannelInputValidationError("partial signer does not match channel participant")
    _opposite_endpoint_key(descriptor, participant_key)
    _validate_expected_public_nonce(expected_local_public_nonce, "expected_local_public_nonce")
    _validate_expected_public_nonce(expected_peer_public_nonce, "expected_peer_public_nonce")
    if payload.local_public_nonce != expected_local_public_nonce:
        raise ChannelInputValidationError("partial local nonce does not match expected nonce")
    if payload.peer_public_nonce != expected_peer_public_nonce:
        raise ChannelInputValidationError("partial peer nonce does not match expected nonce")


def validate_finalize_for_transcript(
    payload: ChannelInputFinalizePayload,
    transcript: ChannelInputTranscript,
    recipient_key: str,
    *,
    expected_local_public_nonce: str,
    expected_peer_public_nonce: str,
) -> None:
    """Verify a coordinator finalize authorization retains peer partial evidence."""

    validate_channel_payload_for_transcript(payload, transcript)
    descriptor = _descriptor_for_outpoint(transcript, payload.funding_outpoint)
    if payload.signer_key != transcript.coordinator_key:
        raise ChannelInputValidationError("finalize signer does not match transcript coordinator")
    peer_key = _opposite_endpoint_key(descriptor, recipient_key)
    _validate_expected_public_nonce(expected_local_public_nonce, "expected_local_public_nonce")
    _validate_expected_public_nonce(expected_peer_public_nonce, "expected_peer_public_nonce")
    if payload.local_public_nonce != expected_local_public_nonce:
        raise ChannelInputValidationError(
            "finalize local nonce does not match recipient current nonce"
        )
    validate_partial_for_transcript(
        payload.peer_partial,
        transcript,
        peer_key,
        expected_local_public_nonce=expected_peer_public_nonce,
        expected_peer_public_nonce=expected_local_public_nonce,
    )


def validate_finalized_for_transcript(
    payload: ChannelInputFinalizedPayload, transcript: ChannelInputTranscript
) -> None:
    """Bind channel-witness evidence to its exact transcript input."""

    try:
        transaction = parse_transaction_bytes(bytes.fromhex(payload.channel_signed_parent_tx))
        unsigned = serialize_transaction(
            transaction.version, transaction.inputs, transaction.outputs, transaction.locktime, None
        ).hex()
    except (ValueError, IndexError) as exc:
        raise ChannelInputValidationError("channel-signed parent transaction is invalid") from exc
    if unsigned != transcript.unsigned_parent_tx:
        raise ChannelInputValidationError(
            "channel-signed parent differs from transcript unsigned parent"
        )
    descriptor = _descriptor_for_outpoint(transcript, payload.funding_outpoint)
    nonempty_indices = [index for index, witness in enumerate(transaction.witnesses) if witness]
    if nonempty_indices != [descriptor.input_index]:
        raise ChannelInputValidationError(
            "channel witness is not at the funding outpoint transcript input"
        )
    transcript_txid = hash256(bytes.fromhex(transcript.unsigned_parent_tx))[::-1].hex()
    if payload.txid != transcript_txid:
        raise ChannelInputValidationError("channel-signed parent txid does not match transcript")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ChannelInputValidationError("canonical JSON object keys must be strings")
            normalized[key] = _canonical_value(item)
        return normalized
    if isinstance(value, tuple | list):
        return [_canonical_value(item) for item in value]
    if value is None:
        return None
    if type(value) in (str, int, bool):
        return value
    raise ChannelInputValidationError(
        "canonical channel-input JSON supports only integer, string, bool, and null"
    )


def canonical_json(value: Any, *, exclude_signature: bool = False) -> bytes:
    """Return the protocol's unique ASCII JSON representation."""

    normalized = _canonical_value(value)
    if exclude_signature:
        if not isinstance(normalized, dict):
            raise ChannelInputValidationError("only object payloads have signatures")
        normalized = {key: item for key, item in normalized.items() if key != "sig"}
    return json.dumps(
        normalized, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def channel_input_hash(object_type: str, value: Any) -> bytes:
    """Hash one canonical object under the channel-input-only domain."""

    try:
        domain = f"{CHANNEL_INPUT_DOMAIN}/{object_type}/v1\0".encode("ascii")
    except UnicodeEncodeError as exc:
        raise ChannelInputValidationError("channel input hash type must be ASCII") from exc
    return hashlib.sha256(domain + canonical_json(value)).digest()


def transcript_hash(transcript: ChannelInputTranscript) -> bytes:
    return channel_input_hash("transcript", transcript)


def cancellation_binding_hash(
    invite: ChannelInputInvitePayload, transcript: ChannelInputTranscript | None = None
) -> bytes:
    """Return the cancellation binding for the committed negotiation boundary."""

    if transcript is None:
        return payload_hash(invite)
    return transcript_hash(transcript)


def endpoint_terms_hash(terms: EndpointTerms) -> bytes:
    return channel_input_hash("endpoint-terms", terms)


def approval_set_hash(approval_set: ChannelInputApprovalSetPayload) -> bytes:
    """Commit to the complete approval set, including every BIP340 signature."""

    return channel_input_hash("approval-set", approval_set)


def cancel_payload_hash(cancel: ChannelInputCancelPayload) -> bytes:
    """Commit to the complete signed cancellation payload."""

    return channel_input_hash("cancel", cancel)


def payload_hash(payload: ChannelInputPayload) -> bytes:
    return channel_input_hash(f"payload/{payload.type}", _payload_without_signature(payload))


def _payload_without_signature(payload: ChannelInputPayload) -> dict[str, Any]:
    value = payload.model_dump(mode="json")
    del value["sig"]
    return value


@dataclass(frozen=True)
class ChannelInputKeyPair:
    """Fresh BIP340 participant key material for exactly one channel-input attempt."""

    secret_key: bytes
    public_key: str

    @classmethod
    def generate(cls) -> ChannelInputKeyPair:
        while True:
            try:
                return cls.from_secret(secrets.token_bytes(32))
            except ChannelInputValidationError:
                continue

    @classmethod
    def from_secret(cls, secret_key: bytes) -> ChannelInputKeyPair:
        if len(secret_key) != 32:
            raise ChannelInputValidationError("channel input secret key must be 32 bytes")
        try:
            private_key = CKey.from_secret_bytes(secret_key)
        except ValueError as exc:
            raise ChannelInputValidationError(
                "channel input secret key is outside secp256k1 range"
            ) from exc
        return cls(
            secret_key=secret_key,
            public_key=bytes(private_key.pub)[1:].hex(),
        )


def sign_hash(
    secret_key: bytes, message_hash: bytes, *, aux_randomness: bytes | None = None
) -> str:
    """BIP340-sign a 32-byte channel-input hash."""

    if len(message_hash) != 32:
        raise ChannelInputValidationError("BIP340 message hash must be 32 bytes")
    auxiliary = secrets.token_bytes(32) if aux_randomness is None else aux_randomness
    if len(auxiliary) != 32:
        raise ChannelInputValidationError("BIP340 auxiliary randomness must be 32 bytes")
    try:
        return (
            CKey.from_secret_bytes(secret_key)
            .sign_schnorr_no_tweak(message_hash, aux=auxiliary)
            .hex()
        )
    except ValueError as exc:
        raise ChannelInputValidationError("invalid BIP340 secret key") from exc


def verify_hash(public_key: str, message_hash: bytes, signature: str) -> bool:
    try:
        _validate_xonly_key(public_key)
        _validate_hex(signature, 64, "signature")
        return len(message_hash) == 32 and bool(
            XOnlyPubKey(bytes.fromhex(public_key)).verify_schnorr(
                message_hash, bytes.fromhex(signature)
            )
        )
    except ValueError:
        return False


def sign_payload(
    payload: ChannelInputPayloadT, secret_key: bytes, *, aux_randomness: bytes | None = None
) -> ChannelInputPayloadT:
    """Return a copy of the payload signed by its declared fresh participant key."""

    key_pair = ChannelInputKeyPair.from_secret(secret_key)
    if payload.signer_key != key_pair.public_key:
        raise ChannelInputValidationError("payload signer_key does not match secret key")
    return cast(
        ChannelInputPayloadT,
        payload.model_copy(
            update={
                "sig": sign_hash(secret_key, payload_hash(payload), aux_randomness=aux_randomness)
            }
        ),
    )


def verify_payload(payload: ChannelInputPayload) -> bool:
    return verify_hash(payload.signer_key, payload_hash(payload), payload.sig)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ChannelInputValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_json_depth(data: bytes) -> None:
    depth = 0
    in_string = False
    escaped = False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x7B, 0x5B):
            depth += 1
            if depth > MAX_CHANNEL_INPUT_JSON_DEPTH:
                raise ChannelInputValidationError(
                    f"channel input JSON exceeds maximum depth {MAX_CHANNEL_INPUT_JSON_DEPTH}"
                )
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise ChannelInputValidationError("channel input JSON has unbalanced containers")
    if in_string or depth != 0:
        raise ChannelInputValidationError("channel input JSON is incomplete")


def encode_channel_input_message(payload: ChannelInputPayload) -> str:
    """Encode a canonical signed payload for the authenticated session transport."""

    encoded = base64.urlsafe_b64encode(canonical_json(payload)).rstrip(b"=").decode("ascii")
    return f"!channel_input {payload.type} {encoded}"


def decode_channel_input_message(message: str) -> ChannelInputPayloadType:
    """Decode one size-bounded, canonical and strictly typed wire envelope."""

    try:
        message_bytes = message.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ChannelInputValidationError("channel input envelope must be ASCII") from exc
    if len(message_bytes) > MAX_CHANNEL_INPUT_ENCODED_BYTES:
        raise ChannelInputValidationError("channel input envelope exceeds maximum encoded size")
    parts = message.split(" ")
    if len(parts) != 3 or parts[0] != "!channel_input":
        raise ChannelInputValidationError("invalid channel input envelope")
    envelope_type, encoded = parts[1:]
    payload_model = _PAYLOAD_TYPES.get(envelope_type)
    if payload_model is None:
        raise ChannelInputValidationError(f"unknown channel input message type: {envelope_type}")
    if (
        not encoded
        or "=" in encoded
        or any(
            character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in encoded
        )
    ):
        raise ChannelInputValidationError("channel input payload must be unpadded base64url")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ChannelInputValidationError("invalid channel input base64url") from exc
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != encoded:
        raise ChannelInputValidationError("channel input base64url is not canonical")
    if len(raw) > MAX_CHANNEL_INPUT_PAYLOAD_BYTES:
        raise ChannelInputValidationError("channel input payload exceeds maximum decoded size")
    _validate_json_depth(raw)
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ChannelInputValidationError(f"invalid JSON constant: {value}")
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ChannelInputValidationError("channel input payload is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise ChannelInputValidationError("channel input payload must be a JSON object")
    if decoded.get("v") != CHANNEL_INPUT_VERSION:
        raise ChannelInputValidationError("unknown channel input payload version")
    if decoded.get("type") != envelope_type:
        raise ChannelInputValidationError("envelope and payload message types differ")
    try:
        payload = cast(ChannelInputPayloadType, payload_model.model_validate_json(raw))
    except ValueError as exc:
        raise ChannelInputValidationError(f"invalid {envelope_type} payload: {exc}") from exc
    if canonical_json(payload) != raw:
        raise ChannelInputValidationError("channel input payload JSON is not canonical")
    return payload
