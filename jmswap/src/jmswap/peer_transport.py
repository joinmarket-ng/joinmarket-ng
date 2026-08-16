"""Durable authenticated custom-message transport for maker-ring escrows."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol, TypeAlias, runtime_checkable

from jmcore.bitcoin import get_txid, parse_transaction
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jmswap.channel_escrow import ChannelInputEscrowTerms, EscrowLifecycleState
from jmswap.errors import ChannelInputEscrowError
from jmswap.ring_authorization import (
    RingChannelPoint,
    RingEdgeAuthorizationRecord,
    RingEdgeAuthorizationStore,
    RingEdgeRole,
)

ESCROW_PEER_PROTOCOL_VERSION: Literal[1] = 1
ESCROW_PEER_CUSTOM_MESSAGE_TYPE = 53_357
MAX_LND_CUSTOM_MESSAGE_PAYLOAD = 65_533
MAX_ESCROW_PEER_ENVELOPE_BYTES = MAX_LND_CUSTOM_MESSAGE_PAYLOAD
MAX_JOURNAL_MESSAGES_PER_DIRECTION = 256

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_FROZEN_MODEL_CONFIG = ConfigDict(extra="forbid", frozen=True, strict=True)
_JOURNAL_MODEL_CONFIG = ConfigDict(extra="forbid", strict=True)
_MESSAGE_ID_DOMAIN = b"joinmarket-maker-ring-escrow-peer-v1\x00"


def _validate_hex(value: str, *, byte_length: int | None = None, name: str) -> str:
    if not value or len(value) % 2 or any(character not in _HEX_DIGITS for character in value):
        raise ValueError(f"{name} must be non-empty hexadecimal")
    if byte_length is not None and len(value) != byte_length * 2:
        raise ValueError(f"{name} must be exactly {byte_length} bytes")
    return value.lower()


def _validate_compressed_pubkey(value: str, *, name: str) -> str:
    normalized = _validate_hex(value, byte_length=33, name=name)
    if not normalized.startswith(("02", "03")):
        raise ValueError(f"{name} must be a compressed public key")
    return normalized


def _validate_scriptpubkey(value: str, *, name: str) -> str:
    normalized = _validate_hex(value, name=name)
    if len(normalized) > 10_000:
        raise ValueError(f"{name} must be at most 5000 bytes")
    return normalized


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )


class EscrowProtocolRole(StrEnum):
    """Economic endpoint role in the channel-input escrow protocol."""

    MAKER = "maker"
    PEER = "peer"


class EscrowInvoiceProposalPayload(BaseModel):
    """Endpoint-private invoice proposal issued by the economic peer."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["escrow_invoice_proposal"] = "escrow_invoice_proposal"
    role: Literal[EscrowProtocolRole.PEER]
    invoice: str = Field(min_length=1)
    payment_hash: str
    payee_pubkey: str
    payment_secret: str
    amount_msat: int = Field(gt=0)
    expiry_unix: int = Field(gt=0)
    min_final_cltv_delta: int = Field(ge=1)

    @field_validator("payment_hash", "payment_secret")
    @classmethod
    def validate_payment_identifiers(cls, value: str, info: object) -> str:
        return _validate_hex(
            value, byte_length=32, name=str(getattr(info, "field_name", "payment identifier"))
        )

    @field_validator("payee_pubkey")
    @classmethod
    def validate_payee_pubkey(cls, value: str) -> str:
        return _validate_compressed_pubkey(value, name="payee public key")


class EndpointTermsPayload(BaseModel):
    """Final endpoint-private escrow terms between the two maker endpoints."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["endpoint_terms"] = "endpoint_terms"
    terms: ChannelInputEscrowTerms

    @field_validator("terms")
    @classmethod
    def normalize_terms_hex(cls, value: ChannelInputEscrowTerms) -> ChannelInputEscrowTerms:
        funding_outpoint = value.snapshot.funding_outpoint.model_copy(
            update={"txid": value.snapshot.funding_outpoint.txid.lower()}
        )
        snapshot = value.snapshot.model_copy(update={"funding_outpoint": funding_outpoint})
        return value.model_copy(
            update={
                "snapshot": snapshot,
                "authorizer_pubkey": value.authorizer_pubkey.lower(),
                "payment_hash": value.payment_hash.lower(),
                "invoice_payee_pubkey": value.invoice_payee_pubkey.lower(),
                "invoice_payment_secret": value.invoice_payment_secret.lower(),
            }
        )


class EscrowPublicKeysPayload(BaseModel):
    """Fresh public keys and one endpoint's timeout destination."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["escrow_public_keys"] = "escrow_public_keys"
    role: EscrowProtocolRole
    escrow_pubkey: str
    timeout_destination_scriptpubkey: str
    maker_claim_pubkey: str | None = None

    @field_validator("escrow_pubkey", "maker_claim_pubkey")
    @classmethod
    def validate_pubkeys(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _validate_compressed_pubkey(
            value, name=str(getattr(info, "field_name", "public key"))
        )

    @field_validator("timeout_destination_scriptpubkey")
    @classmethod
    def validate_scripts(cls, value: str, info: object) -> str:
        return _validate_scriptpubkey(value, name=str(getattr(info, "field_name", "scriptpubkey")))

    @model_validator(mode="after")
    def validate_role_keys(self) -> EscrowPublicKeysPayload:
        if self.role is EscrowProtocolRole.MAKER and self.maker_claim_pubkey is None:
            raise ValueError("maker public-key exchange requires the maker claim key")
        if self.role is EscrowProtocolRole.PEER and self.maker_claim_pubkey is not None:
            raise ValueError("peer public-key exchange cannot declare the maker claim key")
        return self


class TimeoutMuSig2NoncePayload(BaseModel):
    """A public MuSig2 nonce for the timeout transaction."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["timeout_musig2_nonce"] = "timeout_musig2_nonce"
    round_id: str
    timeout_txid: str
    timeout_sighash: str
    public_nonce: str

    @field_validator("round_id", "timeout_txid", "timeout_sighash")
    @classmethod
    def validate_identifiers(cls, value: str, info: object) -> str:
        return _validate_hex(
            value, byte_length=32, name=str(getattr(info, "field_name", "identifier"))
        )

    @field_validator("public_nonce")
    @classmethod
    def validate_nonce(cls, value: str) -> str:
        return _validate_hex(value, byte_length=66, name="timeout MuSig2 public nonce")


class TimeoutMuSig2PartialSignaturePayload(BaseModel):
    """A public MuSig2 partial signature for the timeout transaction."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["timeout_musig2_partial_signature"] = "timeout_musig2_partial_signature"
    round_id: str
    timeout_txid: str
    timeout_sighash: str
    partial_signature: str

    @field_validator("round_id", "timeout_txid", "timeout_sighash")
    @classmethod
    def validate_identifiers(cls, value: str, info: object) -> str:
        return _validate_hex(
            value, byte_length=32, name=str(getattr(info, "field_name", "identifier"))
        )

    @field_validator("partial_signature")
    @classmethod
    def validate_partial_signature(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="timeout MuSig2 partial signature")


class ParentPrevoutPayload(BaseModel):
    """An ordered parent transaction input prevout commitment."""

    model_config = _FROZEN_MODEL_CONFIG

    value_sats: int = Field(gt=0)
    scriptpubkey: str

    @field_validator("scriptpubkey")
    @classmethod
    def validate_scriptpubkey(cls, value: str) -> str:
        return _validate_scriptpubkey(value, name="prevout scriptpubkey")


class ParentPreparationPayload(BaseModel):
    """Complete parent transaction commitment before MuSig2 signing."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["parent_preparation"] = "parent_preparation"
    round_id: str
    parent_tx_hex: str
    parent_txid: str
    channel_input_index: int = Field(ge=0, le=0xFFFFFFFF)
    ordered_prevouts: tuple[ParentPrevoutPayload, ...] = Field(min_length=1)
    escrow_output_index: int = Field(ge=0, le=0xFFFFFFFF)
    escrow_value_sats: int = Field(gt=0)
    escrow_scriptpubkey: str

    @field_validator("round_id", "parent_txid")
    @classmethod
    def validate_identifiers(cls, value: str, info: object) -> str:
        return _validate_hex(
            value, byte_length=32, name=str(getattr(info, "field_name", "identifier"))
        )

    @field_validator("parent_tx_hex")
    @classmethod
    def validate_parent_tx_hex(cls, value: str) -> str:
        return _validate_hex(value, name="parent transaction")

    @field_validator("escrow_scriptpubkey")
    @classmethod
    def validate_escrow_scriptpubkey(cls, value: str) -> str:
        return _validate_scriptpubkey(value, name="escrow scriptpubkey")

    @model_validator(mode="after")
    def validate_parent_transaction(self) -> ParentPreparationPayload:
        if self.channel_input_index >= len(self.ordered_prevouts):
            raise ValueError("channel input index must reference an ordered prevout")
        try:
            parsed = parse_transaction(self.parent_tx_hex)
            calculated_txid = get_txid(self.parent_tx_hex)
        except Exception as exc:  # noqa: BLE001 - normalize transaction parser failures
            raise ValueError("parent transaction is invalid") from exc
        if calculated_txid != self.parent_txid:
            raise ValueError("parent transaction ID does not match parent transaction")
        if len(parsed.inputs) != len(self.ordered_prevouts):
            raise ValueError("parent transaction input count does not match ordered prevouts")
        if self.escrow_output_index >= len(parsed.outputs):
            raise ValueError("escrow output index does not exist in parent transaction")
        escrow_output = parsed.outputs[self.escrow_output_index]
        if (
            escrow_output.value != self.escrow_value_sats
            or escrow_output.script.hex() != self.escrow_scriptpubkey
        ):
            raise ValueError("parent transaction escrow output does not match commitment")
        return self


class ParentMuSig2NoncePayload(BaseModel):
    """A public MuSig2 nonce for the committed parent transaction round."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["parent_musig2_nonce"] = "parent_musig2_nonce"
    round_id: str
    parent_txid: str
    public_nonce: str

    @field_validator("round_id", "parent_txid")
    @classmethod
    def validate_identifiers(cls, value: str, info: object) -> str:
        return _validate_hex(
            value, byte_length=32, name=str(getattr(info, "field_name", "identifier"))
        )

    @field_validator("public_nonce")
    @classmethod
    def validate_nonce(cls, value: str) -> str:
        return _validate_hex(value, byte_length=66, name="parent MuSig2 public nonce")


class ParentMuSig2PartialSignaturePayload(BaseModel):
    """A MuSig2 partial signature for the committed parent transaction round."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["parent_musig2_partial_signature"] = "parent_musig2_partial_signature"
    round_id: str
    parent_txid: str
    partial_signature: str

    @field_validator("round_id", "parent_txid")
    @classmethod
    def validate_identifiers(cls, value: str, info: object) -> str:
        return _validate_hex(
            value, byte_length=32, name=str(getattr(info, "field_name", "identifier"))
        )

    @field_validator("partial_signature")
    @classmethod
    def validate_partial_signature(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="parent MuSig2 partial signature")


class EndpointAbortReason(StrEnum):
    """Safe endpoint-private escrow abort reason codes."""

    TERMS_REJECTED = "terms_rejected"
    PARENT_REJECTED = "parent_rejected"
    SIGNING_FAILED = "signing_failed"
    DEADLINE_EXPIRED = "deadline_expired"
    LOCAL_SHUTDOWN = "local_shutdown"


class EndpointAbortPayload(BaseModel):
    """Endpoint-private protocol abort without free-text disclosure."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["endpoint_abort"] = "endpoint_abort"
    round_id: str
    reason: EndpointAbortReason

    @field_validator("round_id")
    @classmethod
    def validate_round_id(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="round ID")


class RecoveryStatusPayload(BaseModel):
    """Durable lifecycle recovery state with optional transaction identifiers."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["recovery_status"] = "recovery_status"
    state: EscrowLifecycleState
    parent_txid: str | None = None
    timeout_txid: str | None = None
    success_txid: str | None = None

    @field_validator("parent_txid", "timeout_txid", "success_txid")
    @classmethod
    def validate_txids(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _validate_hex(value, byte_length=32, name=str(getattr(info, "field_name", "txid")))


class AcknowledgmentPayload(BaseModel):
    """Acknowledgment of one exactly identified peer envelope."""

    model_config = _FROZEN_MODEL_CONFIG

    kind: Literal["acknowledgment"] = "acknowledgment"
    acknowledged_message_id: str

    @field_validator("acknowledged_message_id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        return _validate_hex(value, byte_length=32, name="acknowledged message ID")


EscrowPeerPayload: TypeAlias = Annotated[
    EscrowInvoiceProposalPayload
    | EndpointTermsPayload
    | EscrowPublicKeysPayload
    | TimeoutMuSig2NoncePayload
    | TimeoutMuSig2PartialSignaturePayload
    | ParentPreparationPayload
    | ParentMuSig2NoncePayload
    | ParentMuSig2PartialSignaturePayload
    | EndpointAbortPayload
    | RecoveryStatusPayload
    | AcknowledgmentPayload,
    Field(discriminator="kind"),
]

_PARENT_ROUND_PAYLOAD_TYPES = (
    ParentMuSig2NoncePayload,
    ParentMuSig2PartialSignaturePayload,
)
_ESCROW_PEER_PAYLOAD_TYPES: tuple[type[BaseModel], ...] = (
    EscrowInvoiceProposalPayload,
    EndpointTermsPayload,
    EscrowPublicKeysPayload,
    TimeoutMuSig2NoncePayload,
    TimeoutMuSig2PartialSignaturePayload,
    ParentPreparationPayload,
    ParentMuSig2NoncePayload,
    ParentMuSig2PartialSignaturePayload,
    EndpointAbortPayload,
    RecoveryStatusPayload,
    AcknowledgmentPayload,
)


class EscrowPeerBinding(BaseModel):
    """Immutable local identity binding for one finalized maker-ring channel."""

    model_config = _FROZEN_MODEL_CONFIG

    version: Literal[1] = ESCROW_PEER_PROTOCOL_VERSION
    edge_id: str
    authorization_id: str
    session_id: str
    channel_point: RingChannelPoint
    local_role: RingEdgeRole
    local_node_id: str
    peer_node_id: str

    @field_validator("edge_id", "authorization_id", "session_id")
    @classmethod
    def validate_identifiers(cls, value: str, info: object) -> str:
        return _validate_hex(
            value, byte_length=32, name=str(getattr(info, "field_name", "identifier"))
        )

    @field_validator("local_node_id", "peer_node_id")
    @classmethod
    def validate_node_ids(cls, value: str, info: object) -> str:
        return _validate_compressed_pubkey(value, name=str(getattr(info, "field_name", "node ID")))

    @model_validator(mode="after")
    def validate_session_and_peers(self) -> EscrowPeerBinding:
        if self.session_id != self.edge_id:
            raise ValueError("maker-ring peer transport v1 requires session_id to equal edge_id")
        if self.local_node_id == self.peer_node_id:
            raise ValueError("maker-ring peer transport endpoints must be distinct")
        return self

    @classmethod
    def from_authorization_record(
        cls, record: RingEdgeAuthorizationRecord, local_node_id: str
    ) -> EscrowPeerBinding:
        """Create a binding only after the authorization has a finalized channel point."""
        if not isinstance(record, RingEdgeAuthorizationRecord):
            raise ChannelInputEscrowError("ring edge authorization record is invalid")
        if record.channel_point is None:
            raise ChannelInputEscrowError(
                "maker-ring escrow peer transport requires a channel point"
            )
        authorization = record.authorization
        try:
            role = authorization.role_for(record.local_nick)
            expected_local = (
                authorization.funder_ln_node
                if role is RingEdgeRole.FUNDER
                else authorization.receiver_ln_node
            ).partition("@")[0]
            expected_peer = authorization.peer_ln_node(record.local_nick).partition("@")[0]
            normalized_local = _validate_compressed_pubkey(local_node_id, name="local LND node ID")
        except (TypeError, ValueError, ChannelInputEscrowError) as exc:
            raise ChannelInputEscrowError("maker-ring escrow peer binding is invalid") from exc
        if normalized_local != expected_local:
            raise ChannelInputEscrowError("local LND node ID does not match authorization role")
        try:
            return cls(
                edge_id=authorization.edge_id,
                authorization_id=authorization.authorization_id,
                session_id=authorization.edge_id,
                channel_point=record.channel_point,
                local_role=role,
                local_node_id=normalized_local,
                peer_node_id=expected_peer,
            )
        except ValidationError as exc:
            raise ChannelInputEscrowError("maker-ring escrow peer binding is invalid") from exc


class EscrowPeerEnvelope(BaseModel):
    """Authenticated, chained, canonical wire envelope for one maker-ring edge."""

    model_config = _FROZEN_MODEL_CONFIG

    version: Literal[1] = ESCROW_PEER_PROTOCOL_VERSION
    edge_id: str
    authorization_id: str
    session_id: str
    channel_point: RingChannelPoint
    sender_node_id: str
    recipient_node_id: str
    sequence: int = Field(ge=1)
    previous_message_id: str | None = None
    payload: EscrowPeerPayload
    message_id: str

    @field_validator("edge_id", "authorization_id", "session_id", "message_id")
    @classmethod
    def validate_identifiers(cls, value: str, info: object) -> str:
        return _validate_hex(
            value, byte_length=32, name=str(getattr(info, "field_name", "identifier"))
        )

    @field_validator("previous_message_id")
    @classmethod
    def validate_previous_message_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_hex(value, byte_length=32, name="previous message ID")

    @field_validator("sender_node_id", "recipient_node_id")
    @classmethod
    def validate_node_ids(cls, value: str, info: object) -> str:
        return _validate_compressed_pubkey(value, name=str(getattr(info, "field_name", "node ID")))

    @model_validator(mode="after")
    def validate_message_commitment(self) -> EscrowPeerEnvelope:
        if self.session_id != self.edge_id:
            raise ValueError("maker-ring peer transport v1 requires session_id to equal edge_id")
        if self.sender_node_id == self.recipient_node_id:
            raise ValueError("sender and recipient node IDs must be distinct")
        if (self.sequence == 1) != (self.previous_message_id is None):
            raise ValueError("only the first message in a direction may omit previous_message_id")
        if self.message_id != self.computed_message_id():
            raise ValueError("message ID does not match canonical envelope body")
        return self

    def canonical_body(self) -> bytes:
        """Return the domain-independent canonical body committed by ``message_id``."""
        return _canonical_json(self.model_dump(mode="json", exclude={"message_id"}))

    def computed_message_id(self) -> str:
        """Return the domain-separated deterministic envelope identifier."""
        return hashlib.sha256(_MESSAGE_ID_DOMAIN + self.canonical_body()).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        binding: EscrowPeerBinding,
        sequence: int,
        previous_message_id: str | None,
        payload: EscrowPeerPayload,
    ) -> EscrowPeerEnvelope:
        """Construct an outbound envelope and calculate its deterministic message ID."""
        values: dict[str, object] = {
            "version": ESCROW_PEER_PROTOCOL_VERSION,
            "edge_id": binding.edge_id,
            "authorization_id": binding.authorization_id,
            "session_id": binding.session_id,
            "channel_point": binding.channel_point,
            "sender_node_id": binding.local_node_id,
            "recipient_node_id": binding.peer_node_id,
            "sequence": sequence,
            "previous_message_id": previous_message_id,
            "payload": payload,
        }
        canonical_values = dict(values)
        canonical_values["channel_point"] = binding.channel_point.model_dump(mode="json")
        canonical_values["payload"] = payload.model_dump(mode="json")
        body = _canonical_json(canonical_values)
        values["message_id"] = hashlib.sha256(_MESSAGE_ID_DOMAIN + body).hexdigest()
        try:
            return cls.model_validate(values)
        except ValidationError as exc:
            raise ChannelInputEscrowError("invalid maker-ring escrow peer envelope") from exc

    def encode(self) -> bytes:
        """Encode a strict ASCII wire message within the LND custom-message limit."""
        encoded = _canonical_json(self.model_dump(mode="json"))
        if len(encoded) > MAX_ESCROW_PEER_ENVELOPE_BYTES:
            raise ChannelInputEscrowError("maker-ring escrow peer envelope exceeds size limit")
        return encoded

    @classmethod
    def decode(cls, data: bytes) -> EscrowPeerEnvelope:
        """Decode and verify one strict canonical wire message."""
        if not isinstance(data, bytes) or not data:
            raise ChannelInputEscrowError("maker-ring escrow peer envelope must be non-empty bytes")
        if len(data) > MAX_ESCROW_PEER_ENVELOPE_BYTES:
            raise ChannelInputEscrowError("maker-ring escrow peer envelope exceeds size limit")
        try:
            data.decode("ascii")
            envelope = cls.model_validate_json(data)
        except (UnicodeDecodeError, ValidationError, ValueError) as exc:
            raise ChannelInputEscrowError("malformed maker-ring escrow peer envelope") from exc
        if data != envelope.encode():
            raise ChannelInputEscrowError("maker-ring escrow peer envelope is not canonical")
        return envelope


@dataclass(frozen=True, slots=True)
class RawReceivedCustomMessage:
    """A received LND custom message bound to LND's authenticated peer identity."""

    peer_node_id: str
    data: bytes


@runtime_checkable
class EscrowPeerCustomMessageBackend(Protocol):
    """Structural LND custom-message backend boundary."""

    async def send_custom_message(
        self,
        peer_node_id: str,
        data: bytes,
        message_type: int = ESCROW_PEER_CUSTOM_MESSAGE_TYPE,
    ) -> None:
        """Send one custom message to an authenticated LND peer."""

    def subscribe_custom_messages(
        self, message_type: int = ESCROW_PEER_CUSTOM_MESSAGE_TYPE
    ) -> AsyncIterator[RawReceivedCustomMessage]:
        """Yield received custom messages of the requested type."""


@runtime_checkable
class RingEdgeAuthorizationReader(Protocol):
    """The small durable authorization-store surface required by this transport."""

    def load(self, edge_id: str) -> RingEdgeAuthorizationRecord | None:
        """Return the authorization record for an edge, if it is known locally."""


class EscrowPeerJournalRecord(BaseModel):
    """Strict durable history for the two directed streams of one escrow edge."""

    model_config = _JOURNAL_MODEL_CONFIG

    binding: EscrowPeerBinding
    inbound: tuple[EscrowPeerEnvelope, ...] = ()
    outbound: tuple[EscrowPeerEnvelope, ...] = ()
    acknowledged_outbound_ids: tuple[str, ...] = ()
    dispatched_inbound_ids: tuple[str, ...] = ()
    revision: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime

    @field_validator("acknowledged_outbound_ids", "dispatched_inbound_ids")
    @classmethod
    def validate_acknowledgments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(
            _validate_hex(item, byte_length=32, name="acknowledged message ID") for item in value
        )
        if len(set(normalized)) != len(normalized):
            raise ValueError("acknowledged message IDs must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_history(self) -> EscrowPeerJournalRecord:
        if len(self.inbound) > MAX_JOURNAL_MESSAGES_PER_DIRECTION:
            raise ValueError("inbound journal message limit exceeded")
        if len(self.outbound) > MAX_JOURNAL_MESSAGES_PER_DIRECTION:
            raise ValueError("outbound journal message limit exceeded")
        if self.created_at.tzinfo != UTC or self.updated_at.tzinfo != UTC:
            raise ValueError("journal timestamps must use UTC")
        self._validate_direction(self.inbound, self.binding, inbound=True)
        self._validate_direction(self.outbound, self.binding, inbound=False)
        outbound_ids = {envelope.message_id for envelope in self.outbound}
        if not set(self.acknowledged_outbound_ids).issubset(outbound_ids):
            raise ValueError("journal acknowledges an unknown outbound message")
        for envelope in self.inbound:
            if (
                isinstance(envelope.payload, AcknowledgmentPayload)
                and envelope.payload.acknowledged_message_id not in outbound_ids
            ):
                raise ValueError("inbound acknowledgment references an unknown outbound message")
        dispatchable_ids = {
            envelope.message_id
            for envelope in self.inbound
            if not isinstance(envelope.payload, AcknowledgmentPayload)
        }
        if not set(self.dispatched_inbound_ids).issubset(dispatchable_ids):
            raise ValueError("journal dispatches an unknown inbound message")
        return self

    @staticmethod
    def _validate_direction(
        envelopes: tuple[EscrowPeerEnvelope, ...],
        binding: EscrowPeerBinding,
        *,
        inbound: bool,
    ) -> None:
        sender_node_id = binding.peer_node_id if inbound else binding.local_node_id
        recipient_node_id = binding.local_node_id if inbound else binding.peer_node_id
        previous_message_id: str | None = None
        for sequence, envelope in enumerate(envelopes, start=1):
            if envelope.sequence != sequence:
                raise ValueError("journal direction sequence is not contiguous")
            if envelope.previous_message_id != previous_message_id:
                raise ValueError("journal direction previous-message chain is invalid")
            if (
                envelope.sender_node_id != sender_node_id
                or envelope.recipient_node_id != recipient_node_id
            ):
                raise ValueError("journal direction endpoint binding is invalid")
            if (
                envelope.edge_id != binding.edge_id
                or envelope.authorization_id != binding.authorization_id
                or envelope.session_id != binding.session_id
                or envelope.channel_point != binding.channel_point
            ):
                raise ValueError("journal direction maker-ring binding is invalid")
            previous_message_id = envelope.message_id


class EscrowPeerJournal:
    """Crash-safe, locked one-file-per-edge journal for exact peer envelopes."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)

    def _path(self, binding: EscrowPeerBinding) -> Path:
        digest = hashlib.sha256(
            f"{binding.edge_id}:{binding.session_id}".encode("ascii")
        ).hexdigest()
        return self._dir / f"{digest}.json"

    @contextlib.contextmanager
    def _lock(self, binding: EscrowPeerBinding) -> Iterator[None]:
        lock_path = self._path(binding).with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _load(self, binding: EscrowPeerBinding) -> EscrowPeerJournalRecord | None:
        path = self._path(binding)
        if not path.exists():
            return None
        try:
            record = EscrowPeerJournalRecord.model_validate_json(path.read_bytes())
        except (OSError, ValidationError, ValueError) as exc:
            raise ChannelInputEscrowError("persisted escrow peer journal is malformed") from exc
        if record.binding != binding:
            raise ChannelInputEscrowError("persisted escrow peer journal binding conflicts")
        return record

    @staticmethod
    def _new_record(binding: EscrowPeerBinding) -> EscrowPeerJournalRecord:
        now = datetime.now(UTC)
        return EscrowPeerJournalRecord(binding=binding, revision=0, created_at=now, updated_at=now)

    def _write(self, record: EscrowPeerJournalRecord) -> None:
        path = self._path(record.binding)
        data = _canonical_json(record.model_dump(mode="json"))
        fd, temporary_path = tempfile.mkstemp(dir=self._dir, prefix=".escrow-peer-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as file_handle:
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
        descriptor = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _updated(record: EscrowPeerJournalRecord, **updates: object) -> EscrowPeerJournalRecord:
        values = record.model_dump()
        values.update(updates)
        values["revision"] = record.revision + 1
        values["updated_at"] = datetime.now(UTC)
        try:
            return EscrowPeerJournalRecord.model_validate(values)
        except ValidationError as exc:
            raise ChannelInputEscrowError("escrow peer journal update is invalid") from exc

    def prepare_outbound(
        self, binding: EscrowPeerBinding, payload: EscrowPeerPayload
    ) -> EscrowPeerEnvelope:
        """Persist and return the next outbound envelope before network transmission."""
        with self._lock(binding):
            record = self._load(binding) or self._new_record(binding)
            if len(record.outbound) >= MAX_JOURNAL_MESSAGES_PER_DIRECTION:
                raise ChannelInputEscrowError("outbound escrow peer journal message limit exceeded")
            previous = record.outbound[-1].message_id if record.outbound else None
            envelope = EscrowPeerEnvelope.create(
                binding=binding,
                sequence=len(record.outbound) + 1,
                previous_message_id=previous,
                payload=payload,
            )
            self._write(self._updated(record, outbound=record.outbound + (envelope,)))
            return envelope

    def prepare_or_replay_outbound(
        self, binding: EscrowPeerBinding, payload: EscrowPeerPayload
    ) -> EscrowPeerEnvelope:
        """Return an exact prior application envelope or persist a new semantic message."""
        if isinstance(payload, AcknowledgmentPayload):
            raise ChannelInputEscrowError(
                "application escrow peer messages cannot be acknowledgments"
            )
        with self._lock(binding):
            record = self._load(binding) or self._new_record(binding)
            for envelope in record.outbound:
                if isinstance(envelope.payload, AcknowledgmentPayload):
                    continue
                if envelope.payload == payload:
                    return envelope
                if envelope.payload.kind == payload.kind:
                    if isinstance(payload, RecoveryStatusPayload):
                        continue
                    if (
                        isinstance(payload, _PARENT_ROUND_PAYLOAD_TYPES)
                        and isinstance(envelope.payload, _PARENT_ROUND_PAYLOAD_TYPES)
                        and envelope.payload.round_id != payload.round_id
                    ):
                        continue
                    raise ChannelInputEscrowError(
                        "conflicting escrow peer semantic retry for protocol version 1"
                    )
            if len(record.outbound) >= MAX_JOURNAL_MESSAGES_PER_DIRECTION:
                raise ChannelInputEscrowError("outbound escrow peer journal message limit exceeded")
            previous = record.outbound[-1].message_id if record.outbound else None
            envelope = EscrowPeerEnvelope.create(
                binding=binding,
                sequence=len(record.outbound) + 1,
                previous_message_id=previous,
                payload=payload,
            )
            self._write(self._updated(record, outbound=record.outbound + (envelope,)))
            return envelope

    def prepare_acknowledgment(
        self, binding: EscrowPeerBinding, message_id: str
    ) -> EscrowPeerEnvelope:
        """Persist one idempotent outbound acknowledgment for an accepted inbound message."""
        with self._lock(binding):
            record = self._load(binding)
            if record is None or message_id not in {item.message_id for item in record.inbound}:
                raise ChannelInputEscrowError(
                    "cannot acknowledge an unknown inbound escrow peer message"
                )
            for envelope in record.outbound:
                if (
                    isinstance(envelope.payload, AcknowledgmentPayload)
                    and envelope.payload.acknowledged_message_id == message_id
                ):
                    return envelope
            if len(record.outbound) >= MAX_JOURNAL_MESSAGES_PER_DIRECTION:
                raise ChannelInputEscrowError("outbound escrow peer journal message limit exceeded")
            previous = record.outbound[-1].message_id if record.outbound else None
            envelope = EscrowPeerEnvelope.create(
                binding=binding,
                sequence=len(record.outbound) + 1,
                previous_message_id=previous,
                payload=AcknowledgmentPayload(acknowledged_message_id=message_id),
            )
            self._write(self._updated(record, outbound=record.outbound + (envelope,)))
            return envelope

    def record_inbound(self, binding: EscrowPeerBinding, envelope: EscrowPeerEnvelope) -> bool:
        """Persist a first receipt, or return false for an exact duplicate receipt."""
        with self._lock(binding):
            record = self._load(binding) or self._new_record(binding)
            if (
                envelope.sender_node_id != binding.peer_node_id
                or envelope.recipient_node_id != binding.local_node_id
                or envelope.edge_id != binding.edge_id
                or envelope.authorization_id != binding.authorization_id
                or envelope.session_id != binding.session_id
                or envelope.channel_point != binding.channel_point
            ):
                raise ChannelInputEscrowError(
                    "inbound escrow peer envelope endpoint binding is invalid"
                )
            if envelope.sequence <= len(record.inbound):
                existing = record.inbound[envelope.sequence - 1]
                if existing == envelope:
                    return False
                raise ChannelInputEscrowError(
                    "inbound escrow peer sequence conflicts with durable history"
                )
            if envelope.sequence != len(record.inbound) + 1:
                raise ChannelInputEscrowError("inbound escrow peer sequence has a gap")
            expected_previous = record.inbound[-1].message_id if record.inbound else None
            if envelope.previous_message_id != expected_previous:
                raise ChannelInputEscrowError(
                    "inbound escrow peer previous-message chain is invalid"
                )
            if len(record.inbound) >= MAX_JOURNAL_MESSAGES_PER_DIRECTION:
                raise ChannelInputEscrowError("inbound escrow peer journal message limit exceeded")
            acknowledged_ids = record.acknowledged_outbound_ids
            if isinstance(envelope.payload, AcknowledgmentPayload):
                acknowledged_id = envelope.payload.acknowledged_message_id
                if acknowledged_id not in {item.message_id for item in record.outbound}:
                    raise ChannelInputEscrowError(
                        "acknowledgment references an unknown outbound escrow peer message"
                    )
                if acknowledged_id not in acknowledged_ids:
                    acknowledged_ids += (acknowledged_id,)
            self._write(
                self._updated(
                    record,
                    inbound=record.inbound + (envelope,),
                    acknowledged_outbound_ids=acknowledged_ids,
                )
            )
            return True

    def pending_inbound(self, binding: EscrowPeerBinding) -> tuple[EscrowPeerEnvelope, ...]:
        """Return accepted non-ACK messages not yet completed by an application consumer."""
        with self._lock(binding):
            record = self._load(binding)
            if record is None:
                return ()
            dispatched = set(record.dispatched_inbound_ids)
            return tuple(
                envelope
                for envelope in record.inbound
                if not isinstance(envelope.payload, AcknowledgmentPayload)
                and envelope.message_id not in dispatched
            )

    def mark_dispatched(self, binding: EscrowPeerBinding, message_id: str) -> None:
        """Mark one accepted non-ACK message after its application consumer resumes."""
        with self._lock(binding):
            record = self._load(binding)
            dispatchable_ids = {
                envelope.message_id
                for envelope in (() if record is None else record.inbound)
                if not isinstance(envelope.payload, AcknowledgmentPayload)
            }
            if record is None or message_id not in dispatchable_ids:
                raise ChannelInputEscrowError(
                    "cannot dispatch an unknown inbound escrow peer message"
                )
            if message_id in record.dispatched_inbound_ids:
                return
            self._write(
                self._updated(
                    record,
                    dispatched_inbound_ids=record.dispatched_inbound_ids + (message_id,),
                )
            )

    def outbound(self, binding: EscrowPeerBinding) -> tuple[EscrowPeerEnvelope, ...]:
        """Return exact persisted outbound envelopes in their original order."""
        with self._lock(binding):
            record = self._load(binding)
            return () if record is None else record.outbound

    def load(self, binding: EscrowPeerBinding) -> EscrowPeerJournalRecord | None:
        """Load the strict journal record for inspection or recovery."""
        with self._lock(binding):
            return self._load(binding)


class EscrowPeerTransport:
    """Authenticate, persist, acknowledge, dispatch, and replay maker-ring escrow messages."""

    def __init__(
        self,
        backend: EscrowPeerCustomMessageBackend,
        authorization_store: RingEdgeAuthorizationReader | RingEdgeAuthorizationStore,
        journal: EscrowPeerJournal,
        local_node_id: str,
    ) -> None:
        try:
            self._local_node_id = _validate_compressed_pubkey(
                local_node_id, name="local LND node ID"
            )
        except (TypeError, ValueError) as exc:
            raise ChannelInputEscrowError("local LND node ID is invalid") from exc
        self._backend = backend
        self._authorization_store = authorization_store
        self._journal = journal

    def binding(self, edge_id: str) -> EscrowPeerBinding:
        """Resolve a durable finalized authorization into a local peer binding."""
        try:
            record = self._authorization_store.load(edge_id)
        except (TypeError, ValueError, ChannelInputEscrowError) as exc:
            raise ChannelInputEscrowError("maker-ring escrow peer edge is invalid") from exc
        if record is None:
            raise ChannelInputEscrowError("maker-ring escrow peer edge is unknown")
        return EscrowPeerBinding.from_authorization_record(record, self._local_node_id)

    def prepare_or_replay(self, edge_id: str, payload: EscrowPeerPayload) -> EscrowPeerEnvelope:
        """Durably select an exact outbound replay or prepare one new application message."""
        return self._journal.prepare_or_replay_outbound(self.binding(edge_id), payload)

    async def send_envelope(self, edge_id: str, envelope: EscrowPeerEnvelope) -> None:
        """Transmit one exact outbound envelope that is already present in the local journal."""
        if not isinstance(envelope, EscrowPeerEnvelope):
            raise ChannelInputEscrowError("outbound escrow peer envelope is invalid")
        binding = self.binding(edge_id)
        if envelope not in self._journal.outbound(binding):
            raise ChannelInputEscrowError("outbound escrow peer envelope is not durably persisted")
        await self._send_envelope(binding, envelope)

    def pending(self, edge_id: str) -> tuple[EscrowPeerEnvelope, ...]:
        """Return accepted application messages that still require explicit completion."""
        return self._journal.pending_inbound(self.binding(edge_id))

    def complete(self, edge_id: str, message_id: str) -> None:
        """Mark one accepted application message complete after durable application handling."""
        self._journal.mark_dispatched(self.binding(edge_id), message_id)

    async def send(self, edge_id: str, payload: EscrowPeerPayload) -> EscrowPeerEnvelope:
        """Durably prepare, then send one new outbound maker-ring message."""
        binding = self.binding(edge_id)
        envelope = self._journal.prepare_outbound(binding, payload)
        await self.send_envelope(edge_id, envelope)
        return envelope

    async def accept(self, raw: RawReceivedCustomMessage) -> EscrowPeerEnvelope | None:
        """Accept one authenticated raw LND message and return only its first non-ACK receipt."""
        if not isinstance(raw, RawReceivedCustomMessage) or not isinstance(raw.data, bytes):
            raise ChannelInputEscrowError("received LND custom message is invalid")
        envelope = EscrowPeerEnvelope.decode(raw.data)
        binding = self.binding(envelope.edge_id)
        self._validate_inbound_binding(binding, raw.peer_node_id, envelope)
        first_receipt = self._journal.record_inbound(binding, envelope)
        if isinstance(envelope.payload, AcknowledgmentPayload):
            return None
        acknowledgment = self._journal.prepare_acknowledgment(binding, envelope.message_id)
        try:
            await self.send_envelope(binding.edge_id, acknowledgment)
        except Exception as exc:  # noqa: BLE001 - receipt stays durable for exact replay
            logger.warning(
                "Unable to send escrow peer acknowledgment for {}: {}",
                envelope.message_id,
                exc,
            )
        return envelope if first_receipt else None

    async def messages(self) -> AsyncIterator[EscrowPeerEnvelope]:
        """Yield first receipts from the backend subscription after durable acceptance."""
        async for raw in self._backend.subscribe_custom_messages(ESCROW_PEER_CUSTOM_MESSAGE_TYPE):
            envelope = await self.accept(raw)
            if envelope is not None:
                yield envelope
                self._journal.mark_dispatched(self.binding(envelope.edge_id), envelope.message_id)

    async def pending_messages(self, edge_id: str) -> AsyncIterator[EscrowPeerEnvelope]:
        """Redeliver accepted application messages left incomplete across a restart."""
        for envelope in self.pending(edge_id):
            yield envelope
            self.complete(edge_id, envelope.message_id)

    async def replay(self, edge_id: str) -> tuple[EscrowPeerEnvelope, ...]:
        """Resend exact persisted outbound envelopes in their original order."""
        binding = self.binding(edge_id)
        envelopes = self._journal.outbound(binding)
        for envelope in envelopes:
            await self.send_envelope(edge_id, envelope)
        return envelopes

    async def _send_envelope(
        self, binding: EscrowPeerBinding, envelope: EscrowPeerEnvelope
    ) -> None:
        if (
            envelope.sender_node_id != binding.local_node_id
            or envelope.recipient_node_id != binding.peer_node_id
        ):
            raise ChannelInputEscrowError(
                "outbound escrow peer envelope endpoint binding is invalid"
            )
        await self._backend.send_custom_message(
            binding.peer_node_id,
            envelope.encode(),
            ESCROW_PEER_CUSTOM_MESSAGE_TYPE,
        )

    @staticmethod
    def _validate_inbound_binding(
        binding: EscrowPeerBinding, authenticated_peer_node_id: str, envelope: EscrowPeerEnvelope
    ) -> None:
        try:
            authenticated_peer = _validate_compressed_pubkey(
                authenticated_peer_node_id, name="authenticated LND peer node ID"
            )
        except (TypeError, ValueError) as exc:
            raise ChannelInputEscrowError("authenticated LND peer node ID is invalid") from exc
        if authenticated_peer != binding.peer_node_id:
            raise ChannelInputEscrowError(
                "authenticated LND peer does not match maker-ring binding"
            )
        if (
            envelope.authorization_id != binding.authorization_id
            or envelope.session_id != binding.session_id
            or envelope.channel_point != binding.channel_point
            or envelope.sender_node_id != binding.peer_node_id
            or envelope.recipient_node_id != binding.local_node_id
        ):
            raise ChannelInputEscrowError(
                "inbound escrow peer envelope does not match maker-ring binding"
            )


class EscrowPeerBroker:
    """Durable application delivery over one escrow peer transport subscription."""

    def __init__(self, transport: EscrowPeerTransport) -> None:
        if not isinstance(transport, EscrowPeerTransport):
            raise ChannelInputEscrowError("escrow peer broker transport is invalid")
        self._transport = transport
        self._conditions: dict[str, asyncio.Condition] = {}
        self._task: asyncio.Task[None] | None = None
        self._terminal_error: Exception | None = None
        self._closed = False

    def start(self) -> None:
        """Start the single custom-message subscription task once."""
        if self._closed:
            raise ChannelInputEscrowError("escrow peer broker is closed")
        if self._terminal_error is not None:
            return
        if self._task is None:
            self._task = asyncio.create_task(self._consume(), name="escrow-peer-broker")

    async def close(self) -> None:
        """Cancel and await the subscription task, waking any waiting receivers."""
        if self._closed:
            return
        self._closed = True
        task = self._task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._notify_all()

    async def send(self, edge_id: str, payload: EscrowPeerPayload) -> EscrowPeerEnvelope:
        """Transmit an atomically selected exact application envelope."""
        envelope = self._transport.prepare_or_replay(edge_id, payload)
        await self._transport.send_envelope(edge_id, envelope)
        return envelope

    def binding(self, edge_id: str) -> EscrowPeerBinding:
        """Return the finalized transport binding for a known escrow edge."""
        return self._transport.binding(edge_id)

    def pending(self, edge_id: str) -> tuple[EscrowPeerEnvelope, ...]:
        """Return durable application receipts not yet completed by a consumer."""
        return self._transport.pending(edge_id)

    async def receive(
        self,
        edge_id: str,
        expected_type: type[BaseModel] | tuple[type[BaseModel], ...],
        *,
        predicate: Callable[[EscrowPeerEnvelope], bool] | None = None,
        timeout: float | None = None,
    ) -> EscrowPeerEnvelope:
        """Return the first matching durable receipt without marking it complete."""
        expected_types = self._validate_expected_type(expected_type)
        self.start()
        condition = self._conditions.setdefault(edge_id, asyncio.Condition())
        try:
            async with asyncio.timeout(timeout):
                async with condition:
                    while True:
                        for envelope in self._transport.pending(edge_id):
                            if isinstance(envelope.payload, expected_types) and (
                                predicate is None or predicate(envelope)
                            ):
                                return envelope
                        self._raise_terminal_error()
                        if self._closed:
                            raise ChannelInputEscrowError("escrow peer broker is closed")
                        await condition.wait()
        except TimeoutError as exc:
            raise ChannelInputEscrowError("timed out waiting for escrow peer message") from exc

    def complete(self, envelope: EscrowPeerEnvelope) -> None:
        """Complete exactly one bound inbound receipt after application persistence."""
        if not isinstance(envelope, EscrowPeerEnvelope):
            raise ChannelInputEscrowError("inbound escrow peer envelope is invalid")
        binding = self._transport.binding(envelope.edge_id)
        if (
            envelope.sender_node_id != binding.peer_node_id
            or envelope.recipient_node_id != binding.local_node_id
            or envelope.authorization_id != binding.authorization_id
            or envelope.session_id != binding.session_id
            or envelope.channel_point != binding.channel_point
        ):
            raise ChannelInputEscrowError("inbound escrow peer envelope binding is invalid")
        if envelope not in self._transport.pending(envelope.edge_id):
            raise ChannelInputEscrowError("inbound escrow peer envelope is not pending")
        self._transport.complete(envelope.edge_id, envelope.message_id)

    async def _consume(self) -> None:
        try:
            async for raw in self._transport._backend.subscribe_custom_messages(
                ESCROW_PEER_CUSTOM_MESSAGE_TYPE
            ):
                envelope = EscrowPeerEnvelope.decode(raw.data)
                await self._transport.accept(raw)
                if envelope.message_id in {
                    item.message_id for item in self._transport.pending(envelope.edge_id)
                }:
                    await self._notify_edge(envelope.edge_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize backend and decoder failures for receivers
            self._terminal_error = exc
            await self._notify_all()
        else:
            self._terminal_error = RuntimeError("escrow peer custom-message subscription ended")
            await self._notify_all()

    async def _notify_edge(self, edge_id: str) -> None:
        condition = self._conditions.get(edge_id)
        if condition is None:
            return
        async with condition:
            condition.notify_all()

    async def _notify_all(self) -> None:
        for condition in tuple(self._conditions.values()):
            async with condition:
                condition.notify_all()

    def _raise_terminal_error(self) -> None:
        if self._terminal_error is not None:
            raise ChannelInputEscrowError(
                "escrow peer subscription failed"
            ) from self._terminal_error

    @staticmethod
    def _validate_expected_type(
        expected_type: type[BaseModel] | tuple[type[BaseModel], ...],
    ) -> tuple[type[BaseModel], ...]:
        expected_types = (expected_type,) if isinstance(expected_type, type) else expected_type
        if not expected_types or not all(
            isinstance(item, type) and item in _ESCROW_PEER_PAYLOAD_TYPES for item in expected_types
        ):
            raise ChannelInputEscrowError("expected type must be an escrow peer payload model")
        return expected_types
