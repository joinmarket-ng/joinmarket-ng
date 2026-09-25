"""Core models and authentication for the JMP-0010 co-funded channel ring."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar, Literal, TypeAlias, cast

from bitcointx.core.key import CKey, CPubKey, XOnlyPubKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jmcore.bitcoin import hash256, parse_transaction_bytes, serialize_transaction
from jmcore.constants import MAX_MONEY

RING_DESIGN = "cofunded_channel_ring_v1"
RING_VERSION = 1
MIN_RING_PARTICIPANTS = 4
MAX_RING_PARTICIPANTS = 32
MAX_RING_PAYLOAD_BYTES = 65_536
MAX_RING_JSON_DEPTH = 10
MAX_REVISION = 2_147_483_647
MAX_RING_ENCODED_BYTES = 4 * MAX_RING_PAYLOAD_BYTES // 3 + 128
# NaCl Box adds a 24-byte nonce and 16-byte authenticator before base64 encoding.
MAX_RING_CIPHERTEXT_BYTES = 4 * ((MAX_RING_ENCODED_BYTES + 40 + 2) // 3)

Hex32 = str
Hex33 = str
Hex64 = str
ScriptHex = str
Tr0OfferType: TypeAlias = Literal["tr0absoffer", "tr0reloffer"]
RingNetwork: TypeAlias = Literal["mainnet", "testnet", "signet", "regtest"]


class RingModel(BaseModel):
    """Strict base for every ring object."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RingValidationError(ValueError):
    """Raised when ring encoding, authentication, or cross-model checks fail."""


def _validate_hex(value: str, byte_length: int, field_name: str) -> str:
    if len(value) != byte_length * 2 or value.lower() != value:
        raise ValueError(f"{field_name} must be {byte_length}-byte lowercase hex")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid lowercase hex") from exc
    return value


def _validate_xonly_key(value: str, field_name: str = "ring key") -> str:
    _validate_hex(value, 32, field_name)
    try:
        if not CPubKey(b"\x02" + bytes.fromhex(value)).is_fullyvalid():
            raise ValueError("invalid curve point")
    except ValueError as exc:
        raise ValueError(f"{field_name} is not a valid BIP340 public key") from exc
    return value


def _validate_node_id(value: str) -> str:
    _validate_hex(value, 33, "node_id")
    raw = bytes.fromhex(value)
    if raw[0] not in (2, 3):
        raise ValueError("node_id must be a compressed secp256k1 public key")
    try:
        if not CPubKey(raw).is_fullyvalid():
            raise ValueError("invalid curve point")
    except ValueError as exc:
        raise ValueError("node_id is not a valid secp256k1 public key") from exc
    return value


def _validate_script(value: str) -> str:
    if not value or len(value) > 10_000 or len(value) % 2:
        raise ValueError("script_pubkey must be non-empty even-length hex of at most 5000 bytes")
    if value.lower() != value:
        raise ValueError("script_pubkey must be lowercase hex")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("script_pubkey must be valid hex") from exc
    return value


def _validate_p2tr_script(value: str, field_name: str = "script_pubkey") -> str:
    _validate_script(value)
    if len(value) != 68 or not value.startswith("5120"):
        raise ValueError(f"{field_name} must be a P2TR script_pubkey")
    return value


def _all_unique(values: Sequence[Any], field_name: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(f"duplicate {field_name}")


class ChannelPolicy(RingModel):
    """Complete private TAPROOT channel policy negotiated by both endpoints."""

    commitment_type: Literal["TAPROOT"] = "TAPROOT"
    fundee_csv_delay: int = Field(ge=1, le=2016)
    fundee_reserve: int = Field(ge=0, le=MAX_MONEY)
    min_depth: int = Field(ge=1, le=144)
    opener_csv_delay: int = Field(ge=1, le=2016)
    opener_reserve: int = Field(ge=0, le=MAX_MONEY)
    private: Literal[True] = True
    zero_conf: Literal[False] = False


class PolicyBounds(RingModel):
    """Invitation bounds applied before a backend is selected."""

    min_csv_delay: int = Field(ge=1, le=2016)
    max_csv_delay: int = Field(ge=1, le=2016)
    min_depth: int = Field(ge=1, le=144)
    max_depth: int = Field(ge=1, le=144)
    min_reserve: int = Field(ge=0, le=MAX_MONEY)
    max_reserve: int = Field(ge=0, le=MAX_MONEY)

    @model_validator(mode="after")
    def validate_ranges(self) -> PolicyBounds:
        if self.min_csv_delay > self.max_csv_delay:
            raise ValueError("CSV delay bounds do not intersect")
        if self.min_depth > self.max_depth:
            raise ValueError("confirmation depth bounds do not intersect")
        if self.min_reserve > self.max_reserve:
            raise ValueError("reserve bounds do not intersect")
        return self

    def accepts(self, policy: ChannelPolicy) -> bool:
        return (
            self.min_csv_delay <= policy.opener_csv_delay <= self.max_csv_delay
            and self.min_csv_delay <= policy.fundee_csv_delay <= self.max_csv_delay
            and self.min_depth <= policy.min_depth <= self.max_depth
            and self.min_reserve <= policy.opener_reserve <= self.max_reserve
            and self.min_reserve <= policy.fundee_reserve <= self.max_reserve
        )


class BackendLimits(RingModel):
    """Bounded Lightning backend capabilities used during planning."""

    network: RingNetwork
    offer_type: Tr0OfferType
    min_channel_capacity: int = Field(ge=1, le=MAX_MONEY)
    max_channel_capacity: int = Field(ge=1, le=MAX_MONEY)
    max_push_amount: int = Field(ge=1, le=MAX_MONEY)
    dust_limit: int = Field(ge=0, le=MAX_MONEY)
    max_reserve: int = Field(ge=0, le=MAX_MONEY)
    max_commitment_fee: int = Field(ge=0, le=MAX_MONEY)
    max_pending_channels: int = Field(ge=1, le=64)
    external_psbt: Literal[True] = True
    no_publish: Literal[True] = True
    retained_pending_state: Literal[True] = True
    taproot: Literal[True] = True

    @model_validator(mode="after")
    def validate_capacity_range(self) -> BackendLimits:
        if self.min_channel_capacity > self.max_channel_capacity:
            raise ValueError("backend channel capacity bounds do not intersect")
        if self.max_push_amount > self.max_channel_capacity:
            raise ValueError("max_push_amount exceeds max_channel_capacity")
        return self


class PrivateParticipant(RingModel):
    """Participant data distributed only over encrypted ring messages."""

    participant_key: Hex32
    node_id: Hex33
    onion_endpoint: str = Field(min_length=1, max_length=320)
    backend_limits: BackendLimits

    @model_validator(mode="after")
    def validate_identity(self) -> PrivateParticipant:
        _validate_xonly_key(self.participant_key, "participant_key")
        _validate_node_id(self.node_id)
        try:
            endpoint = self.onion_endpoint.encode("ascii").decode("ascii")
        except UnicodeError as exc:
            raise ValueError("onion_endpoint must be ASCII") from exc
        host, separator, port_text = endpoint.rpartition(":")
        if (
            not separator
            or not host.endswith(".onion")
            or len(host) != 62
            or any(char not in "abcdefghijklmnopqrstuvwxyz234567" for char in host[:-6])
            or not port_text.isdigit()
            or not 1 <= int(port_text) <= 65535
        ):
            raise ValueError("onion_endpoint must be a v3 onion host and valid port")
        return self


class LocalContribution(RingModel):
    """Private finalized residual split for one participant."""

    participant_key: Hex32
    residual: int = Field(ge=1, le=MAX_MONEY)
    outgoing: int = Field(ge=1, le=MAX_MONEY)
    incoming: int = Field(ge=1, le=MAX_MONEY)

    @model_validator(mode="after")
    def validate_accounting(self) -> LocalContribution:
        _validate_xonly_key(self.participant_key, "participant_key")
        if self.outgoing + self.incoming != self.residual:
            raise ValueError("outgoing and incoming contributions must equal residual")
        return self


class PublicParticipant(RingModel):
    """Public participant identity, containing only its fresh ring key."""

    participant_key: Hex32

    @model_validator(mode="after")
    def validate_key(self) -> PublicParticipant:
        _validate_xonly_key(self.participant_key, "participant_key")
        return self


class RingEdge(RingModel):
    """One public directed edge and its exact transaction output."""

    opener_key: Hex32
    acceptor_key: Hex32
    pending_channel_id: Hex32
    capacity: int = Field(ge=1, le=MAX_MONEY)
    output_index: int = Field(ge=0, le=65_535)
    script_pubkey: ScriptHex
    policy: ChannelPolicy

    @model_validator(mode="after")
    def validate_edge(self) -> RingEdge:
        _validate_hex(self.pending_channel_id, 32, "pending_channel_id")
        _validate_xonly_key(self.opener_key, "opener_key")
        _validate_xonly_key(self.acceptor_key, "acceptor_key")
        _validate_p2tr_script(self.script_pubkey, "channel script_pubkey")
        if self.opener_key == self.acceptor_key:
            raise ValueError("an edge must have two distinct endpoints")
        return self


class ManifestOutput(RingModel):
    """One output in transaction order."""

    index: int = Field(ge=0, le=65_535)
    amount: int = Field(ge=0, le=MAX_MONEY)
    script_pubkey: ScriptHex

    @model_validator(mode="after")
    def validate_script_hex(self) -> ManifestOutput:
        _validate_script(self.script_pubkey)
        return self


class RingManifest(RingModel):
    """Public transaction and cycle commitment. Private node data is intentionally absent."""

    v: Literal[1] = 1
    network: RingNetwork
    round_nonce: Hex32
    revision: int = Field(ge=0, le=MAX_REVISION)
    unsigned_tx_hash: Hex32
    unsigned_txid: Hex32
    participant_keys: list[Hex32] = Field(
        min_length=MIN_RING_PARTICIPANTS, max_length=MAX_RING_PARTICIPANTS
    )
    edges: list[RingEdge] = Field(
        min_length=MIN_RING_PARTICIPANTS, max_length=MAX_RING_PARTICIPANTS
    )
    equal_output_indices: list[int] = Field(
        min_length=MIN_RING_PARTICIPANTS, max_length=MAX_RING_PARTICIPANTS
    )
    outputs: list[ManifestOutput] = Field(min_length=8, max_length=64)

    @model_validator(mode="after")
    def validate_manifest(self) -> RingManifest:
        _validate_hex(self.round_nonce, 32, "round_nonce")
        _validate_hex(self.unsigned_tx_hash, 32, "unsigned_tx_hash")
        _validate_hex(self.unsigned_txid, 32, "unsigned_txid")
        count = len(self.participant_keys)
        if len(self.edges) != count or len(self.equal_output_indices) != count:
            raise ValueError("manifest participant, edge, and equal-output counts differ")
        if len(self.outputs) != 2 * count:
            raise ValueError(
                "manifest must contain exactly one equal and one channel output per participant"
            )

        keys = self.participant_keys
        for key in keys:
            _validate_xonly_key(key, "participant_key")
        _all_unique(keys, "participant key")
        _all_unique([edge.pending_channel_id for edge in self.edges], "pending_channel_id")
        _all_unique([edge.script_pubkey for edge in self.edges], "channel script_pubkey")
        _all_unique([edge.output_index for edge in self.edges], "channel output_index")
        _all_unique(self.equal_output_indices, "equal output_index")

        for index, edge in enumerate(self.edges):
            if edge.opener_key != keys[index] or edge.acceptor_key != keys[(index + 1) % count]:
                raise ValueError("edges do not form the declared directed cycle")

        output_indices = [output.index for output in self.outputs]
        _all_unique(output_indices, "manifest output index")
        _all_unique(
            [output.script_pubkey for output in self.outputs], "manifest output script_pubkey"
        )
        if sorted(output_indices) != list(range(len(self.outputs))):
            raise ValueError("manifest outputs must cover every transaction output in order")
        output_by_index = {output.index: output for output in self.outputs}
        if set(self.equal_output_indices) & {edge.output_index for edge in self.edges}:
            raise ValueError("equal and channel output indices overlap")
        declared_indices = set(self.equal_output_indices) | {
            edge.output_index for edge in self.edges
        }
        if declared_indices != set(output_indices):
            raise ValueError("manifest output classifications do not cover every output")
        for index in self.equal_output_indices:
            if index not in output_by_index:
                raise ValueError("equal output index is absent from outputs")
            if output_by_index[index].amount < 1:
                raise ValueError("equal output amount must be positive")
            _validate_p2tr_script(
                output_by_index[index].script_pubkey,
                "tr0 equal output",
            )
        for edge in self.edges:
            output = output_by_index.get(edge.output_index)
            if (
                output is None
                or output.amount != edge.capacity
                or output.script_pubkey != edge.script_pubkey
            ):
                raise ValueError("channel edge does not match its transaction output")
        if sum(output.amount for output in self.outputs) > MAX_MONEY:
            raise ValueError("manifest output sum exceeds MAX_MONEY")
        return self


class PendingChannelState(RingModel):
    """Exact private pending state independently observed by one endpoint."""

    pending_channel_id: Hex32
    opener_key: Hex32
    acceptor_key: Hex32
    script_pubkey: ScriptHex
    capacity: int = Field(ge=1, le=MAX_MONEY)
    opener_balance: int = Field(ge=1, le=MAX_MONEY)
    fundee_balance: int = Field(ge=1, le=MAX_MONEY)
    push_amount: int = Field(ge=1, le=MAX_MONEY)
    opener_reserve: int = Field(ge=0, le=MAX_MONEY)
    fundee_reserve: int = Field(ge=0, le=MAX_MONEY)
    commitment_fee: int = Field(ge=0, le=MAX_MONEY)
    commitment_overhead: int = Field(ge=0, le=MAX_MONEY)
    policy: ChannelPolicy

    @model_validator(mode="after")
    def validate_state(self) -> PendingChannelState:
        _validate_hex(self.pending_channel_id, 32, "pending_channel_id")
        _validate_xonly_key(self.opener_key, "opener_key")
        _validate_xonly_key(self.acceptor_key, "acceptor_key")
        _validate_p2tr_script(self.script_pubkey, "channel script_pubkey")
        if (
            self.opener_balance
            + self.fundee_balance
            + self.commitment_fee
            + self.commitment_overhead
            != self.capacity
        ):
            raise ValueError(
                "endpoint balances, commitment fee, and commitment overhead "
                "do not equal channel capacity"
            )
        if self.push_amount != self.fundee_balance:
            raise ValueError("push_amount must equal the fundee contribution")
        if self.opener_reserve != self.policy.opener_reserve:
            raise ValueError("opener reserve does not match channel policy")
        if self.fundee_reserve != self.policy.fundee_reserve:
            raise ValueError("fundee reserve does not match channel policy")
        return self


class EndpointRole(StrEnum):
    OPENER = "opener"
    FUNDEE = "fundee"


class ReadinessState(PendingChannelState):
    """Salted private state retained after external PSBT verification."""

    role: EndpointRole
    signer_key: Hex32
    round_nonce: Hex32
    revision: int = Field(ge=0, le=MAX_REVISION)
    manifest_hash: Hex32
    unsigned_tx_hash: Hex32
    unsigned_txid: Hex32
    output_index: int = Field(ge=0, le=65_535)
    state_salt: Hex32

    @model_validator(mode="after")
    def validate_readiness(self) -> ReadinessState:
        _validate_xonly_key(self.signer_key, "signer_key")
        _validate_hex(self.round_nonce, 32, "round_nonce")
        _validate_hex(self.manifest_hash, 32, "manifest_hash")
        _validate_hex(self.unsigned_tx_hash, 32, "unsigned_tx_hash")
        _validate_hex(self.unsigned_txid, 32, "unsigned_txid")
        _validate_hex(self.state_salt, 32, "state_salt")
        expected_signer = self.opener_key if self.role == EndpointRole.OPENER else self.acceptor_key
        if self.signer_key != expected_signer:
            raise ValueError("readiness signer does not match its endpoint role")
        return self


class ReadinessAttestation(RingModel):
    """Public commitment to one endpoint's complete salted readiness state."""

    edge: Hex32
    manifest_hash: Hex32
    revision: int = Field(ge=0, le=MAX_REVISION)
    role: EndpointRole
    round_nonce: Hex32
    signer_key: Hex32
    state_hash: Hex32

    @model_validator(mode="after")
    def validate_attestation(self) -> ReadinessAttestation:
        _validate_hex(self.edge, 32, "edge")
        _validate_hex(self.manifest_hash, 32, "manifest_hash")
        _validate_hex(self.round_nonce, 32, "round_nonce")
        _validate_xonly_key(self.signer_key, "signer_key")
        _validate_hex(self.state_hash, 32, "state_hash")
        return self


class SignedReadinessAttestation(RingModel):
    attestation: ReadinessAttestation
    signature: Hex64

    @model_validator(mode="after")
    def validate_signature_hex(self) -> SignedReadinessAttestation:
        _validate_hex(self.signature, 64, "signature")
        return self


class RingPayload(RingModel):
    """Fields shared by every canonical ring message payload."""

    message_type: ClassVar[str]
    v: Literal[1] = 1
    type: str
    round_nonce: Hex32
    revision: int = Field(ge=0, le=MAX_REVISION)
    signer_key: Hex32
    sig: Hex64 = "00" * 64

    @model_validator(mode="after")
    def validate_common(self) -> RingPayload:
        if self.type != self.message_type:
            raise ValueError(f"payload type must be {self.message_type}")
        _validate_hex(self.round_nonce, 32, "round_nonce")
        _validate_xonly_key(self.signer_key, "signer_key")
        _validate_hex(self.sig, 64, "sig")
        return self


class RingInvitePayload(RingPayload):
    message_type: ClassVar[str] = "ring_invite"
    type: Literal["ring_invite"] = "ring_invite"
    network: RingNetwork
    offer_type: Tr0OfferType
    expiry: int = Field(ge=1, le=MAX_REVISION)
    policy_bounds: PolicyBounds


class RingHelloPayload(RingPayload):
    message_type: ClassVar[str] = "ring_hello"
    type: Literal["ring_hello"] = "ring_hello"
    participant: PrivateParticipant

    @model_validator(mode="after")
    def validate_signer(self) -> RingHelloPayload:
        if self.signer_key != self.participant.participant_key:
            raise ValueError("hello signer_key does not match participant_key")
        return self


class PrivateEdgePlan(RingModel):
    edge_id: Hex32
    pending_channel_id: Hex32
    opener_key: Hex32
    acceptor_key: Hex32
    capacity: int = Field(ge=1, le=MAX_MONEY)
    push_amount: int = Field(ge=1, le=MAX_MONEY)
    policy: ChannelPolicy

    @model_validator(mode="after")
    def validate_values(self) -> PrivateEdgePlan:
        _validate_hex(self.edge_id, 32, "edge_id")
        _validate_hex(self.pending_channel_id, 32, "pending_channel_id")
        _validate_xonly_key(self.opener_key, "opener_key")
        _validate_xonly_key(self.acceptor_key, "acceptor_key")
        if self.push_amount >= self.capacity:
            raise ValueError("push_amount must be below channel capacity")
        return self


class RingPlanPayload(RingPayload):
    message_type: ClassVar[str] = "ring_plan"
    type: Literal["ring_plan"] = "ring_plan"
    network: RingNetwork
    offer_type: Tr0OfferType
    cycle_keys: list[Hex32] = Field(
        min_length=MIN_RING_PARTICIPANTS, max_length=MAX_RING_PARTICIPANTS
    )
    position: int = Field(ge=0, le=MAX_RING_PARTICIPANTS - 1)
    contribution: LocalContribution
    predecessor: PrivateParticipant
    successor: PrivateParticipant
    incoming_edge: PrivateEdgePlan
    outgoing_edge: PrivateEdgePlan

    @model_validator(mode="after")
    def validate_local_plan(self) -> RingPlanPayload:
        for key in self.cycle_keys:
            _validate_xonly_key(key, "cycle key")
        _all_unique(self.cycle_keys, "cycle key")
        if self.signer_key not in self.cycle_keys:
            raise ValueError("plan signer must be the taker's cycle key")
        if self.position >= len(self.cycle_keys):
            raise ValueError("local plan position is outside the cycle")
        local_key = self.cycle_keys[self.position]
        if self.contribution.participant_key != local_key:
            raise ValueError("local contribution does not identify the plan recipient")
        predecessor_key = self.cycle_keys[(self.position - 1) % len(self.cycle_keys)]
        successor_key = self.cycle_keys[(self.position + 1) % len(self.cycle_keys)]
        if self.predecessor.participant_key != predecessor_key:
            raise ValueError("predecessor does not match cycle")
        if self.successor.participant_key != successor_key:
            raise ValueError("successor does not match cycle")
        if (
            self.incoming_edge.opener_key != predecessor_key
            or self.incoming_edge.acceptor_key != local_key
            or self.outgoing_edge.opener_key != local_key
            or self.outgoing_edge.acceptor_key != successor_key
        ):
            raise ValueError("local edges do not match cycle neighbors")
        if self.incoming_edge.push_amount != self.contribution.incoming:
            raise ValueError("incoming push does not match local contribution")
        if self.network != self.predecessor.backend_limits.network:
            raise ValueError("predecessor backend network mismatch")
        if self.network != self.successor.backend_limits.network:
            raise ValueError("successor backend network mismatch")
        if self.offer_type != self.predecessor.backend_limits.offer_type:
            raise ValueError("predecessor offer type mismatch")
        if self.offer_type != self.successor.backend_limits.offer_type:
            raise ValueError("successor offer type mismatch")
        return self


class RingPlanAckPayload(RingPayload):
    message_type: ClassVar[str] = "ring_plan_ack"
    type: Literal["ring_plan_ack"] = "ring_plan_ack"
    plan_hash: Hex32

    @model_validator(mode="after")
    def validate_hash(self) -> RingPlanAckPayload:
        _validate_hex(self.plan_hash, 32, "plan_hash")
        return self


class RingOpenPayload(RingPayload):
    """Taker authorization to begin both planned backend channel attempts."""

    message_type: ClassVar[str] = "ring_open"
    type: Literal["ring_open"] = "ring_open"
    plan_hash: Hex32

    @model_validator(mode="after")
    def validate_hash(self) -> RingOpenPayload:
        _validate_hex(self.plan_hash, 32, "plan_hash")
        return self


class PreparedOutgoingState(RingModel):
    """Opener data known before the global funding PSBT exists."""

    pending_channel_id: Hex32
    opener_key: Hex32
    acceptor_key: Hex32
    script_pubkey: ScriptHex
    capacity: int = Field(ge=1, le=MAX_MONEY)
    push_amount: int = Field(ge=1, le=MAX_MONEY)
    policy: ChannelPolicy

    @model_validator(mode="after")
    def validate_state(self) -> PreparedOutgoingState:
        _validate_hex(self.pending_channel_id, 32, "pending_channel_id")
        _validate_xonly_key(self.opener_key, "opener_key")
        _validate_xonly_key(self.acceptor_key, "acceptor_key")
        _validate_p2tr_script(self.script_pubkey, "channel script_pubkey")
        if self.push_amount >= self.capacity:
            raise ValueError("push_amount must be below channel capacity")
        return self


class PreparedIncomingState(RingModel):
    """Fundee acceptor observation, which intentionally has no funding script."""

    pending_channel_id: Hex32
    opener_key: Hex32
    acceptor_key: Hex32
    opener_node_id: Hex33
    capacity: int = Field(ge=1, le=MAX_MONEY)
    push_amount: int = Field(ge=1, le=MAX_MONEY)
    policy: ChannelPolicy

    @model_validator(mode="after")
    def validate_state(self) -> PreparedIncomingState:
        _validate_hex(self.pending_channel_id, 32, "pending_channel_id")
        _validate_xonly_key(self.opener_key, "opener_key")
        _validate_xonly_key(self.acceptor_key, "acceptor_key")
        _validate_node_id(self.opener_node_id)
        if self.push_amount >= self.capacity:
            raise ValueError("push_amount must be below channel capacity")
        return self


class RingPreparedPayload(RingPayload):
    message_type: ClassVar[str] = "ring_prepared"
    type: Literal["ring_prepared"] = "ring_prepared"
    outgoing: PreparedOutgoingState
    incoming: PreparedIncomingState

    @model_validator(mode="after")
    def validate_distinct_edges(self) -> RingPreparedPayload:
        if self.outgoing.pending_channel_id == self.incoming.pending_channel_id:
            raise ValueError("prepared states must cover distinct edges")
        if self.outgoing.opener_key != self.signer_key:
            raise ValueError("outgoing state opener does not match signer")
        if self.incoming.acceptor_key != self.signer_key:
            raise ValueError("incoming state fundee does not match signer")
        return self


def _read_compact_size(data: bytes, offset: int) -> tuple[int, int]:
    if offset >= len(data):
        raise ValueError("truncated PSBT compact size")
    prefix = data[offset]
    offset += 1
    widths = {0xFD: 2, 0xFE: 4, 0xFF: 8}
    width = widths.get(prefix)
    if width is None:
        return prefix, offset
    if offset + width > len(data):
        raise ValueError("truncated PSBT compact size")
    value = int.from_bytes(data[offset : offset + width], "little")
    minimum = {2: 0xFD, 4: 0x10000, 8: 0x100000000}[width]
    if value < minimum:
        raise ValueError("non-canonical PSBT compact size")
    return value, offset + width


def _read_psbt_map(data: bytes, offset: int) -> tuple[dict[bytes, bytes], int]:
    entries: dict[bytes, bytes] = {}
    while True:
        key_length, offset = _read_compact_size(data, offset)
        if key_length == 0:
            return entries, offset
        if offset + key_length > len(data):
            raise ValueError("truncated PSBT key")
        key = data[offset : offset + key_length]
        offset += key_length
        if key in entries:
            raise ValueError("duplicate PSBT key")
        value_length, offset = _read_compact_size(data, offset)
        if offset + value_length > len(data):
            raise ValueError("truncated PSBT value")
        entries[key] = data[offset : offset + value_length]
        offset += value_length


def _psbt_unsigned_transaction(psbt: bytes) -> bytes:
    if not psbt.startswith(b"psbt\xff"):
        raise ValueError("psbt does not have the PSBT magic prefix")
    global_map, offset = _read_psbt_map(psbt, 5)
    unsigned_tx = global_map.get(b"\x00")
    if unsigned_tx is None:
        raise ValueError("PSBT has no global unsigned transaction")
    try:
        transaction = parse_transaction_bytes(unsigned_tx)
    except (ValueError, IndexError, struct.error) as exc:
        raise ValueError("PSBT global unsigned transaction is invalid") from exc
    if transaction.has_witness or any(tx_input.scriptsig for tx_input in transaction.inputs):
        raise ValueError("PSBT global transaction is not unsigned")
    for _ in range(len(transaction.inputs) + len(transaction.outputs)):
        _, offset = _read_psbt_map(psbt, offset)
    if offset != len(psbt):
        raise ValueError("PSBT map count does not match its unsigned transaction")
    return unsigned_tx


class RingUnsignedPayload(RingPayload):
    message_type: ClassVar[str] = "ring_unsigned"
    type: Literal["ring_unsigned"] = "ring_unsigned"
    unsigned_tx: str = Field(min_length=2, max_length=MAX_RING_PAYLOAD_BYTES)
    psbt: str = Field(min_length=1, max_length=MAX_RING_PAYLOAD_BYTES)
    manifest: RingManifest

    @model_validator(mode="after")
    def validate_unsigned(self) -> RingUnsignedPayload:
        if self.unsigned_tx.lower() != self.unsigned_tx or len(self.unsigned_tx) % 2:
            raise ValueError("unsigned_tx must be even-length lowercase hex")
        try:
            transaction_bytes = bytes.fromhex(self.unsigned_tx)
            transaction = parse_transaction_bytes(transaction_bytes)
        except (ValueError, IndexError, struct.error) as exc:
            raise ValueError("unsigned_tx must be a valid unsigned transaction") from exc
        canonical_transaction = serialize_transaction(
            transaction.version,
            transaction.inputs,
            transaction.outputs,
            transaction.locktime,
            witnesses=None,
        )
        if (
            transaction.has_witness
            or any(tx_input.scriptsig for tx_input in transaction.inputs)
            or canonical_transaction != transaction_bytes
        ):
            raise ValueError("unsigned_tx must be canonical and contain no signatures or witness")
        if (
            transaction.version != 2
            or transaction.locktime != 0
            or any(tx_input.sequence != 0xFFFFFFFF for tx_input in transaction.inputs)
        ):
            raise ValueError(
                "unsigned_tx must use version 2, zero locktime, and final input sequences"
            )
        outpoints = [(tx_input.txid, tx_input.vout) for tx_input in transaction.inputs]
        if len(set(outpoints)) != len(outpoints):
            raise ValueError("unsigned_tx must not contain duplicate input outpoints")
        try:
            psbt_bytes = base64.b64decode(self.psbt, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("psbt must be padded base64") from exc
        if base64.b64encode(psbt_bytes).decode("ascii") != self.psbt:
            raise ValueError("psbt must use canonical padded base64")
        if _psbt_unsigned_transaction(psbt_bytes) != transaction_bytes:
            raise ValueError("PSBT global unsigned transaction does not match unsigned_tx")
        if self.round_nonce != self.manifest.round_nonce or self.revision != self.manifest.revision:
            raise ValueError("unsigned payload round does not match manifest")
        if hashlib.sha256(transaction_bytes).hexdigest() != self.manifest.unsigned_tx_hash:
            raise ValueError("unsigned transaction hash does not match manifest")
        if hash256(transaction_bytes)[::-1].hex() != self.manifest.unsigned_txid:
            raise ValueError("unsigned transaction ID does not match manifest")
        actual_outputs = [
            (index, output.value, output.scriptpubkey)
            for index, output in enumerate(transaction.outputs)
        ]
        manifest_outputs = [
            (output.index, output.amount, output.script_pubkey) for output in self.manifest.outputs
        ]
        if actual_outputs != manifest_outputs:
            raise ValueError("unsigned transaction outputs do not match manifest")
        return self


class RingReadyPayload(RingPayload):
    message_type: ClassVar[str] = "ring_ready"
    type: Literal["ring_ready"] = "ring_ready"
    state: ReadinessState
    attestation: ReadinessAttestation
    endpoint_signature: Hex64

    @model_validator(mode="after")
    def validate_ready(self) -> RingReadyPayload:
        _validate_hex(self.endpoint_signature, 64, "endpoint_signature")
        if (
            self.round_nonce != self.state.round_nonce
            or self.revision != self.state.revision
            or self.signer_key != self.state.signer_key
        ):
            raise ValueError("ready payload envelope does not match readiness state")
        expected_hash = ring_hash("ready-state", self.state).hex()
        if self.attestation.state_hash != expected_hash:
            raise ValueError("attestation state_hash does not match readiness state")
        if (
            self.attestation.edge != self.state.pending_channel_id
            or self.attestation.manifest_hash != self.state.manifest_hash
            or self.attestation.revision != self.state.revision
            or self.attestation.role != self.state.role
            or self.attestation.round_nonce != self.state.round_nonce
            or self.attestation.signer_key != self.state.signer_key
        ):
            raise ValueError("attestation does not match readiness state")
        return self


class RingReadySetPayload(RingPayload):
    message_type: ClassVar[str] = "ring_ready_set"
    type: Literal["ring_ready_set"] = "ring_ready_set"
    manifest: RingManifest
    attestations: list[SignedReadinessAttestation] = Field(min_length=8, max_length=64)

    @model_validator(mode="after")
    def validate_complete_set(self) -> RingReadySetPayload:
        if self.round_nonce != self.manifest.round_nonce or self.revision != self.manifest.revision:
            raise ValueError("readiness set round does not match manifest")
        expected_manifest_hash = manifest_hash(self.manifest).hex()
        expected: set[tuple[str, EndpointRole, str]] = set()
        for edge in self.manifest.edges:
            expected.add((edge.pending_channel_id, EndpointRole.OPENER, edge.opener_key))
            expected.add((edge.pending_channel_id, EndpointRole.FUNDEE, edge.acceptor_key))
        actual = {
            (item.attestation.edge, item.attestation.role, item.attestation.signer_key)
            for item in self.attestations
        }
        if len(actual) != len(self.attestations) or actual != expected:
            raise ValueError(
                "readiness set must contain exactly two endpoint attestations per edge"
            )
        for item in self.attestations:
            if (
                item.attestation.manifest_hash != expected_manifest_hash
                or item.attestation.round_nonce != self.manifest.round_nonce
                or item.attestation.revision != self.manifest.revision
            ):
                raise ValueError("readiness attestation refers to another ring revision")
        return self


class RingReadySetAckPayload(RingPayload):
    """Durable participant acknowledgment of the complete readiness set."""

    message_type: ClassVar[str] = "ring_ready_set_ack"
    type: Literal["ring_ready_set_ack"] = "ring_ready_set_ack"
    readiness_hash: Hex32

    @model_validator(mode="after")
    def validate_hash(self) -> RingReadySetAckPayload:
        _validate_hex(self.readiness_hash, 32, "readiness_hash")
        return self


class RingSignPayload(RingPayload):
    message_type: ClassVar[str] = "ring_sign"
    type: Literal["ring_sign"] = "ring_sign"
    manifest_hash: Hex32
    unsigned_tx_hash: Hex32

    @model_validator(mode="after")
    def validate_hashes(self) -> RingSignPayload:
        _validate_hex(self.manifest_hash, 32, "manifest_hash")
        _validate_hex(self.unsigned_tx_hash, 32, "unsigned_tx_hash")
        return self


class RingSignAckPayload(RingPayload):
    """Durable participant acknowledgment that its state is SIGNING."""

    message_type: ClassVar[str] = "ring_sign_ack"
    type: Literal["ring_sign_ack"] = "ring_sign_ack"
    manifest_hash: Hex32
    unsigned_tx_hash: Hex32

    @model_validator(mode="after")
    def validate_hashes(self) -> RingSignAckPayload:
        _validate_hex(self.manifest_hash, 32, "manifest_hash")
        _validate_hex(self.unsigned_tx_hash, 32, "unsigned_tx_hash")
        return self


class RingCancelPayload(RingPayload):
    """Cancellation result for every backend attempt canceled in this revision."""

    message_type: ClassVar[str] = "ring_cancel"
    type: Literal["ring_cancel"] = "ring_cancel"
    reason_code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    canceled_pending_ids: list[Hex32] = Field(
        default_factory=list,
        max_length=MAX_RING_PARTICIPANTS,
        description="Backend attempts canceled before a new revision can begin",
    )

    @model_validator(mode="after")
    def validate_pending_ids(self) -> RingCancelPayload:
        for pending_id in self.canceled_pending_ids:
            _validate_hex(pending_id, 32, "pending_channel_id")
        _all_unique(self.canceled_pending_ids, "canceled pending_channel_id")
        return self


RingPayloadType: TypeAlias = (
    RingInvitePayload
    | RingHelloPayload
    | RingPlanPayload
    | RingPlanAckPayload
    | RingOpenPayload
    | RingPreparedPayload
    | RingUnsignedPayload
    | RingReadyPayload
    | RingReadySetPayload
    | RingReadySetAckPayload
    | RingSignPayload
    | RingSignAckPayload
    | RingCancelPayload
)

_PAYLOAD_TYPES: dict[str, type[RingPayload]] = {
    payload_type.message_type: payload_type
    for payload_type in (
        RingInvitePayload,
        RingHelloPayload,
        RingPlanPayload,
        RingPlanAckPayload,
        RingOpenPayload,
        RingPreparedPayload,
        RingUnsignedPayload,
        RingReadyPayload,
        RingReadySetPayload,
        RingReadySetAckPayload,
        RingSignPayload,
        RingSignAckPayload,
        RingCancelPayload,
    )
}


def validate_hello_for_invite(invite: RingInvitePayload, hello: RingHelloPayload) -> None:
    """Reject a private hello that belongs to a different tr0 round or network."""

    limits = hello.participant.backend_limits
    if invite.round_nonce != hello.round_nonce or invite.revision != hello.revision:
        raise RingValidationError("hello round does not match invite")
    if invite.network != limits.network:
        raise RingValidationError("hello backend network does not match invite")
    if invite.offer_type != limits.offer_type:
        raise RingValidationError("hello tr0 offer type does not match invite")


def validate_plan_for_invite(invite: RingInvitePayload, plan: RingPlanPayload) -> None:
    """Bind a taker-signed local plan to the exact invitation and policy bounds."""

    if invite.round_nonce != plan.round_nonce or invite.revision != plan.revision:
        raise RingValidationError("plan round does not match invite")
    if plan.signer_key != invite.signer_key:
        raise RingValidationError("plan is not signed by the inviting taker")
    if plan.network != invite.network or plan.offer_type != invite.offer_type:
        raise RingValidationError("plan network or offer type does not match invite")
    incoming_policy_ok = invite.policy_bounds.accepts(plan.incoming_edge.policy)
    outgoing_policy_ok = invite.policy_bounds.accepts(plan.outgoing_edge.policy)
    if not incoming_policy_ok or not outgoing_policy_ok:
        raise RingValidationError("plan channel policy is outside invitation bounds")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RingValidationError("canonical JSON object keys must be strings")
            result[key] = _canonical_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if type(value) in (str, int, bool):
        return value
    raise RingValidationError("canonical ring JSON supports only integer, string, and bool schemas")


def canonical_json(value: Any, *, exclude_signature: bool = False) -> bytes:
    """Return deterministic UTF-8 JSON for the protocol's restricted schemas."""

    normalized = _canonical_value(value)
    if exclude_signature:
        if not isinstance(normalized, dict):
            raise RingValidationError("only object payloads have signatures")
        normalized = {key: item for key, item in normalized.items() if key != "sig"}
    return json.dumps(
        normalized,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def ring_hash(object_type: str, value: Any) -> bytes:
    """Compute the exact JMP-0010 domain-separated object hash."""

    try:
        domain = f"JMP0010/{object_type}/v1\0".encode("ascii")
    except UnicodeEncodeError as exc:
        raise RingValidationError("ring hash type must be ASCII") from exc
    return hashlib.sha256(domain + canonical_json(value)).digest()


def manifest_hash(manifest: RingManifest) -> bytes:
    return ring_hash("manifest", manifest)


def payload_hash(payload: RingPayload) -> bytes:
    domain = f"JMP0010/{payload.type}/v1\0".encode("ascii")
    return hashlib.sha256(domain + canonical_json(payload, exclude_signature=True)).digest()


@dataclass(frozen=True)
class RingKeyPair:
    """Fresh per-revision BIP340 key material."""

    secret_key: bytes
    public_key: str

    @classmethod
    def generate(cls) -> RingKeyPair:
        while True:
            try:
                return cls.from_secret(secrets.token_bytes(32))
            except RingValidationError:
                continue

    @classmethod
    def from_secret(cls, secret_key: bytes) -> RingKeyPair:
        if len(secret_key) != 32:
            raise RingValidationError("ring secret key must be 32 bytes")
        try:
            private_key = CKey(secret_key)
        except ValueError as exc:
            raise RingValidationError("ring secret key is outside the secp256k1 range") from exc
        public_key = bytes(private_key.xonly_pub).hex()
        return cls(secret_key=secret_key, public_key=public_key)


def sign_hash(
    secret_key: bytes, message_hash: bytes, *, aux_randomness: bytes | None = None
) -> str:
    """Sign a 32-byte hash with BIP340; production signatures use fresh auxiliary randomness."""

    if len(message_hash) != 32:
        raise RingValidationError("BIP340 message hash must be 32 bytes")
    auxiliary = secrets.token_bytes(32) if aux_randomness is None else aux_randomness
    if len(auxiliary) != 32:
        raise RingValidationError("BIP340 auxiliary randomness must be 32 bytes")
    try:
        return CKey(secret_key).sign_schnorr_no_tweak(message_hash, aux=auxiliary).hex()
    except ValueError as exc:
        raise RingValidationError("invalid BIP340 secret key") from exc


def verify_hash(public_key: str, message_hash: bytes, signature: str) -> bool:
    try:
        _validate_xonly_key(public_key)
        _validate_hex(signature, 64, "signature")
        if len(message_hash) != 32:
            return False
        return bool(
            XOnlyPubKey(bytes.fromhex(public_key)).verify_schnorr(
                message_hash, bytes.fromhex(signature)
            )
        )
    except ValueError:
        return False


def sign_payload(
    payload: RingPayloadType,
    secret_key: bytes,
    *,
    aux_randomness: bytes | None = None,
) -> RingPayloadType:
    key_pair = RingKeyPair.from_secret(secret_key)
    if payload.signer_key != key_pair.public_key:
        raise RingValidationError("payload signer_key does not match secret key")
    signature = sign_hash(secret_key, payload_hash(payload), aux_randomness=aux_randomness)
    return cast(RingPayloadType, payload.model_copy(update={"sig": signature}))


def verify_payload(payload: RingPayload) -> bool:
    return verify_hash(payload.signer_key, payload_hash(payload), payload.sig)


def sign_attestation(
    attestation: ReadinessAttestation,
    secret_key: bytes,
    *,
    aux_randomness: bytes | None = None,
) -> SignedReadinessAttestation:
    key_pair = RingKeyPair.from_secret(secret_key)
    if attestation.signer_key != key_pair.public_key:
        raise RingValidationError("attestation signer_key does not match secret key")
    signature = sign_hash(
        secret_key,
        ring_hash("ready", attestation),
        aux_randomness=aux_randomness,
    )
    return SignedReadinessAttestation(attestation=attestation, signature=signature)


def verify_attestation(signed: SignedReadinessAttestation) -> bool:
    return verify_hash(
        signed.attestation.signer_key,
        ring_hash("ready", signed.attestation),
        signed.signature,
    )


def verify_ready_payload(payload: RingReadyPayload) -> bool:
    """Verify the public endpoint signature carried with a private readiness payload."""

    return verify_hash(
        payload.attestation.signer_key,
        ring_hash("ready", payload.attestation),
        payload.endpoint_signature,
    )


def verify_ready_set(payload: RingReadySetPayload) -> bool:
    """Verify every endpoint signature in a structurally complete readiness set."""

    return all(verify_attestation(item) for item in payload.attestations)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RingValidationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _validate_encoded_depth(data: bytes, max_depth: int = MAX_RING_JSON_DEPTH) -> None:
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
            if depth > max_depth:
                raise RingValidationError(f"ring JSON exceeds maximum depth {max_depth}")
        elif byte in (0x7D, 0x5D):
            depth -= 1
            if depth < 0:
                raise RingValidationError("ring JSON has unbalanced containers")
    if in_string or depth != 0:
        raise RingValidationError("ring JSON is incomplete")


def encode_ring_message(payload: RingPayload) -> str:
    encoded = base64.urlsafe_b64encode(canonical_json(payload)).rstrip(b"=").decode("ascii")
    return f"!ring {payload.type} {encoded}"


def decode_ring_message(message: str) -> RingPayloadType:
    """Parse one canonical envelope with size, depth, duplicate, type, and version checks."""

    try:
        message_bytes = message.encode("ascii")
    except UnicodeEncodeError as exc:
        raise RingValidationError("ring envelope must be ASCII") from exc
    if len(message_bytes) > MAX_RING_ENCODED_BYTES:
        raise RingValidationError("ring envelope exceeds maximum encoded size")
    parts = message.split(" ")
    if len(parts) != 3 or parts[0] != "!ring":
        raise RingValidationError("invalid ring envelope")
    envelope_type, encoded = parts[1:]
    payload_model = _PAYLOAD_TYPES.get(envelope_type)
    if payload_model is None:
        raise RingValidationError(f"unknown ring message type: {envelope_type}")
    if (
        not encoded
        or "=" in encoded
        or any(
            char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
            for char in encoded
        )
    ):
        raise RingValidationError("ring envelope payload must be unpadded base64url")
    try:
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RingValidationError("invalid ring envelope base64url") from exc
    if len(raw) > MAX_RING_PAYLOAD_BYTES:
        raise RingValidationError("ring payload exceeds maximum decoded size")
    _validate_encoded_depth(raw)
    try:
        decoded = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                RingValidationError(f"invalid JSON constant: {value}")
            ),
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RingValidationError("ring payload is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise RingValidationError("ring payload must be a JSON object")
    if decoded.get("v") != RING_VERSION:
        raise RingValidationError("unknown ring payload version")
    if decoded.get("type") != envelope_type:
        raise RingValidationError("envelope and payload message types differ")
    try:
        payload = cast(RingPayloadType, payload_model.model_validate_json(raw))
    except ValueError as exc:
        raise RingValidationError(f"invalid {envelope_type} payload: {exc}") from exc
    if canonical_json(payload) != raw:
        raise RingValidationError("ring payload JSON is not canonical")
    return payload
