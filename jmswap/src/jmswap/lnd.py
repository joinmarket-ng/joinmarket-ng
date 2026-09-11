"""Strict LND backend for externally funded, co-funded final Taproot channels."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import struct
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

from jmcore.bitcoin import (
    TxOutput,
    address_to_scriptpubkey,
    decode_varint,
    encode_varint,
    get_txid,
    parse_transaction,
    serialize_output,
    serialize_transaction,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from jmswap.lndrpc import lightning_pb2 as ln

os.environ.setdefault("GRPC_SSL_CIPHER_SUITES", "HIGH+ECDSA")

FINAL_TAPROOT_COMMITMENT = 7
FINAL_TAPROOT_FEATURE_BITS = frozenset({80, 81})
FINAL_TAPROOT_COMMITMENT_OVERHEAD_SAT = 660
MIN_FINAL_TAPROOT_LND_VERSION = (0, 21, 0)
_PSBT_MAGIC = b"\x70\x73\x62\x74\xff"
_PSBT_GLOBAL_UNSIGNED_TX = 0x00
_PSBT_IN_WITNESS_UTXO = 0x01
_ONION_CONNECT_ATTEMPT_SECONDS = 125.0
_MAINTENANCE_RPC_TIMEOUT_SECONDS = 10.0
_MAX_MAINTENANCE_PAYMENT_TIMEOUT_SECONDS = 60
_MAX_MAINTENANCE_CLTV_EXPOSURE = 288
_MAINTENANCE_FINAL_CLTV_PADDING = 3
_UINT32_MAX = (1 << 32) - 1
_UINT64_MAX = (1 << 64) - 1
_INT64_MAX = (1 << 63) - 1
_MAX_CUSTOM_MESSAGE_PAYLOAD_BYTES = 8192

Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
NodeId = Annotated[str, StringConstraints(pattern=r"^(02|03)[0-9a-f]{64}$")]
ScriptHex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]+$")]
Bytes32 = Annotated[bytes, Field(min_length=32, max_length=32)]


class LndError(Exception):
    """Base error for the external channel backend."""


class LndCapabilityError(LndError):
    """The connected LND node cannot safely provide the required channel type."""


class LndValidationError(LndError):
    """LND or caller data differs from the negotiated channel contract."""


class LndTimeoutError(LndError):
    """An LND state transition did not complete before its explicit deadline."""


class LndRetirementRpcError(LndError):
    """LND failed a verified-channel retirement RPC."""


class LndRetirementPostconditionError(LndError):
    """LND still reports a channel point after claiming to abandon it."""


class LndAmbiguousShimError(LndError):
    """A resumed funding shim cannot be proven absent rather than already consumed."""


class LndRpcError(LndError):
    """An LND RPC failed; the message carries the reason LND reported."""


class LndEscrowCredentialError(LndError):
    """A ChannelEscrowRPC call was attempted without its dedicated macaroon."""


@dataclass(frozen=True)
class MaintenanceChannel:
    """One LND channel and both published directional forwarding policies."""

    channel_point: str
    channel_id: int
    peer_node_id: str
    active: bool
    private: bool
    local_balance_sat: int
    remote_balance_sat: int
    local_reserve_sat: int
    remote_reserve_sat: int
    pending_htlc_count: int
    local_alias: int | None
    peer_alias: int | None
    local_base_fee_msat: int
    local_fee_ppm: int
    remote_base_fee_msat: int
    remote_fee_ppm: int
    local_cltv_delta: int
    remote_cltv_delta: int


@dataclass(frozen=True)
class PeerRouteHop:
    """A caller-selected directed private route hint hop."""

    node_id: str
    channel_id: int
    base_fee_msat: int
    fee_ppm: int
    cltv_delta: int


@dataclass(frozen=True)
class MaintenanceInvoice:
    """An invoice created with exactly the caller-supplied private route hint."""

    payment_hash: str
    payment_request: str
    expires_at: int
    preimage: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class MaintenancePayment:
    """A single observed payment state without retry or enrollment semantics."""

    status: Literal["unknown", "in_flight", "succeeded", "failed"]
    fee_msat: int | None


@dataclass(frozen=True)
class _MaintenanceExactRoute:
    """Validated invoice data needed for one exact maintenance route."""

    payment_hash: bytes
    payment_addr: bytes
    amount_msat: int
    helper_peer_node_id: str
    return_peer_node_id: str
    helper_channel_alias: int
    return_channel_alias: int
    final_expiry: int
    helper_expiry: int
    total_time_lock: int


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EscrowOutpoint(StrictModel):
    """A Bitcoin funding outpoint in conventional display byte order."""

    txid: Hex32
    vout: Annotated[int, Field(ge=0, le=0xFFFFFFFF)]


class LndEscrowStage(IntEnum):
    """Durable final-Taproot channel-input signing stages reported by ChannelEscrowRPC."""

    LOCKED = 0
    PARENT_PREPARED = 1
    PARENT_SIGNED = 2
    FINALIZED = 3


class FrozenChannelSnapshot(StrictModel):
    """Quiescent final-Taproot channel data returned by an escrow freeze."""

    session_id: Annotated[str, StringConstraints(min_length=1)]
    channel_id: Annotated[str, StringConstraints(min_length=1)]
    funding_outpoint: EscrowOutpoint
    funding_scriptpubkey: Annotated[bytes, Field(min_length=34, max_length=34)]
    peer_node_pubkey: Annotated[bytes, Field(min_length=33, max_length=33)]
    local_funding_pubkey: Annotated[bytes, Field(min_length=33, max_length=33)]
    remote_funding_pubkey: Annotated[bytes, Field(min_length=33, max_length=33)]
    capacity_sat: Annotated[int, Field(gt=0)]
    local_claim_sat: Annotated[int, Field(ge=0)]
    remote_claim_sat: Annotated[int, Field(ge=0)]
    quiescent: bool = True
    active_htlcs: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def validate_final_taproot_funding_data(self) -> FrozenChannelSnapshot:
        if self.funding_scriptpubkey[:2] != b"\x51\x20":
            raise ValueError("funding script must be P2TR")
        for name, pubkey in (
            ("peer node public key", self.peer_node_pubkey),
            ("local funding public key", self.local_funding_pubkey),
            ("remote funding public key", self.remote_funding_pubkey),
        ):
            if pubkey[0] not in (0x02, 0x03):
                raise ValueError(f"{name} must be a compressed public key")
        if self.local_funding_pubkey == self.remote_funding_pubkey:
            raise ValueError("funding public keys must differ")
        if self.local_claim_sat + self.remote_claim_sat != self.capacity_sat:
            raise ValueError("channel claims must exactly conserve capacity")
        if not self.quiescent or self.active_htlcs != 0:
            raise ValueError("frozen channel must be quiescent without active HTLCs")
        return self


class LndEscrowSigningAttempt(StrictModel):
    """An active MuSig2 nonce attempt for one immutable prepared parent."""

    attempt_id: Annotated[bytes, Field(min_length=32, max_length=32)]
    public_nonce: Annotated[bytes, Field(min_length=66, max_length=66)]


class LndFinalizedEscrowParent(StrictModel):
    """A finalized parent transaction and its final-Taproot input witness."""

    tx_hex: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]+$")]
    txid: Hex32
    channel_input_index: Annotated[int, Field(ge=0)]
    channel_input_witness: tuple[Annotated[bytes, Field(min_length=64, max_length=64)], ...]

    @model_validator(mode="after")
    def validate_channel_witness(self) -> LndFinalizedEscrowParent:
        if len(self.channel_input_witness) != 1:
            raise ValueError("final-Taproot channel input must have one witness element")
        return self


class LndEscrowStatus(StrictModel):
    """Durable escrow evidence needed to resume a final-Taproot signing session."""

    stage: LndEscrowStage
    parent_txid: Hex32 | None
    finalized: bool
    durable_local_public_nonce: Annotated[bytes, Field(min_length=66, max_length=66)] | None = None
    durable_remote_public_nonce: Annotated[bytes, Field(min_length=66, max_length=66)] | None = None
    durable_local_partial_signature: (
        Annotated[bytes, Field(min_length=32, max_length=32)] | None
    ) = None
    finalized_parent: LndFinalizedEscrowParent | None = None
    active_attempt: LndEscrowSigningAttempt | None = None

    @model_validator(mode="after")
    def validate_stage_evidence(self) -> LndEscrowStatus:
        durable = (
            self.durable_local_public_nonce,
            self.durable_remote_public_nonce,
            self.durable_local_partial_signature,
        )
        if (self.parent_txid is not None) != (self.stage >= LndEscrowStage.PARENT_PREPARED):
            raise ValueError("escrow stage and parent transaction evidence differ")
        if self.finalized != (self.stage is LndEscrowStage.FINALIZED):
            raise ValueError("escrow stage and finalization evidence differ")
        if self.stage >= LndEscrowStage.PARENT_SIGNED:
            if not all(value is not None for value in durable):
                raise ValueError("signed escrow stage lacks durable signing evidence")
        elif any(value is not None for value in durable):
            raise ValueError("unsigned escrow stage includes durable signing evidence")
        if (self.finalized_parent is not None) != self.finalized:
            raise ValueError("escrow stage and finalized parent evidence differ")
        if self.stage is LndEscrowStage.FINALIZED and self.active_attempt is not None:
            raise ValueError("finalized escrow cannot retain an active signing attempt")
        return self


class ChannelInputCandidate(StrictModel):
    """A safe, unfrozen final-Taproot channel available for channel-input selection."""

    channel_id: Annotated[int, Field(ge=0)]
    funding_outpoint: EscrowOutpoint
    peer_node_pubkey: NodeId
    capacity_sat: Annotated[int, Field(gt=0)]
    local_balance_sat: Annotated[int, Field(ge=0)]
    remote_balance_sat: Annotated[int, Field(ge=0)]
    private: bool
    active: bool
    default_channel_state: bool
    pending_htlc_count: Annotated[int, Field(ge=0)]
    commitment_type: int

    @model_validator(mode="after")
    def validate_balances(self) -> ChannelInputCandidate:
        if (
            self.local_balance_sat > self.capacity_sat
            or self.remote_balance_sat > self.capacity_sat
        ):
            raise ValueError("channel balances cannot exceed channel capacity")
        if not self.active or not self.default_channel_state or self.pending_htlc_count != 0:
            raise ValueError(
                "channel candidate must be active, default state, and free of pending HTLCs"
            )
        if self.commitment_type != FINAL_TAPROOT_COMMITMENT:
            raise ValueError("channel candidate must use final-Taproot commitments")
        return self


class ChannelInputForceCloseOutcome(StrictModel):
    """The locally initiated commitment transaction from recovery force-close."""

    funding_outpoint: EscrowOutpoint
    commitment_txid: Hex32


class LndNodeInfo(StrictModel):
    identity_pubkey: NodeId
    version: str
    network: str
    synced_to_chain: bool
    wallet_synced: bool
    feature_bits: frozenset[int]
    advertised_uris: tuple[str, ...]


class WitnessUtxo(StrictModel):
    value_sat: Annotated[int, Field(gt=0)]
    script_pubkey: bytes

    @field_validator("script_pubkey")
    @classmethod
    def validate_native_segwit(cls, script: bytes) -> bytes:
        is_v0 = len(script) in (22, 34) and script[0] == 0 and script[1] == len(script) - 2
        is_v1 = len(script) == 34 and script[:2] == b"\x51\x20"
        if not (is_v0 or is_v1):
            raise ValueError("witness UTXO must be a native SegWit v0 or v1 output")
        return script


class ExternalChannelRequest(StrictModel):
    pending_channel_id: Bytes32
    peer_node_id: NodeId
    peer_host: Annotated[str, StringConstraints(min_length=3, max_length=300)]
    capacity_sat: Annotated[int, Field(gt=0)]
    push_sat: Annotated[int, Field(gt=0)]
    opener_reserve_sat: Annotated[int, Field(gt=0)]
    fundee_reserve_sat: Annotated[int, Field(gt=0)]
    opener_csv_delay: Annotated[int, Field(ge=1, le=2016)]
    fundee_csv_delay: Annotated[int, Field(ge=1, le=2016)]
    min_depth: Annotated[int, Field(ge=1, le=144)]
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 30.0
    scid_alias: bool = False

    @model_validator(mode="after")
    def validate_amounts(self) -> ExternalChannelRequest:
        if self.push_sat >= self.capacity_sat:
            raise ValueError("push_sat must be less than channel capacity")
        for name, reserve in (
            ("opener_reserve_sat", self.opener_reserve_sat),
            ("fundee_reserve_sat", self.fundee_reserve_sat),
        ):
            if reserve * 5 >= self.capacity_sat:
                raise ValueError(f"{name} must be less than 20% of capacity")
        return self


class FundingNegotiation(StrictModel):
    pending_channel_id: Bytes32
    peer_node_id: NodeId
    funding_address: str
    funding_script_pubkey: bytes
    capacity_sat: int
    push_sat: int
    opener_reserve_sat: int
    fundee_reserve_sat: int
    opener_csv_delay: int
    fundee_csv_delay: int
    min_depth: int


class VerifiedFunding(StrictModel):
    pending_channel_id: Bytes32
    funding_txid: Hex32
    funding_vout: Annotated[int, Field(ge=0)]
    unsigned_psbt: bytes

    @model_validator(mode="after")
    def validate_unsigned_txid(self) -> VerifiedFunding:
        try:
            if not self.unsigned_psbt.startswith(_PSBT_MAGIC):
                raise ValueError("VerifiedFunding must contain a PSBT")
            key_length, offset = decode_varint(self.unsigned_psbt, len(_PSBT_MAGIC))
            key_end = offset + key_length
            if self.unsigned_psbt[offset:key_end] != bytes([_PSBT_GLOBAL_UNSIGNED_TX]):
                raise ValueError("VerifiedFunding PSBT must begin with the unsigned transaction")
            value_length, value_offset = decode_varint(self.unsigned_psbt, key_end)
            unsigned_transaction = self.unsigned_psbt[value_offset : value_offset + value_length]
            if len(unsigned_transaction) != value_length:
                raise ValueError("VerifiedFunding PSBT unsigned transaction is truncated")
            if get_txid(unsigned_transaction.hex()) != self.funding_txid:
                raise ValueError("VerifiedFunding TXID differs from its unsigned transaction")
        except (IndexError, struct.error) as exc:
            raise ValueError("VerifiedFunding contains an invalid PSBT") from exc
        return self

    @property
    def channel_point(self) -> str:
        return f"{self.funding_txid}:{self.funding_vout}"


class TransactionPresence(StrEnum):
    ABSENT = "absent"
    PRESENT = "present"
    UNKNOWN = "unknown"


class FundingTransactionChainStatus(StrictModel):
    """Caller-observed mempool and confirmed-chain status for one exact TXID."""

    unsigned_txid: Hex32
    mempool: TransactionPresence
    chain: TransactionPresence


class VerifiedChannelRetirementAuthorization(StrictModel):
    """Explicit evidence required before destructive verified-channel retirement."""

    verified_funding: VerifiedFunding
    channel_point: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}:[0-9]+$")]
    unsigned_txid: Hex32
    chain_status: FundingTransactionChainStatus
    local_coinjoin_input_signature_created: bool
    local_coinjoin_input_signature_sent: bool
    funding_transaction_broadcast: bool
    final_transaction_observed: bool


class VerifiedChannelRetirementStatus(StrEnum):
    ABANDONED = "abandoned"
    ALREADY_ABSENT = "already_absent"


class VerifiedChannelRetirementOutcome(StrictModel):
    status: VerifiedChannelRetirementStatus
    channel_point: str
    unsigned_txid: Hex32
    lnd_status: str = ""


def can_advertise_cofunded_channel_ring_v1(
    *,
    safe_verified_retirement_live_validated: bool,
    retained_state_reconciliation: bool,
    strict_anti_grief_limits: bool,
) -> bool:
    """Gate ring advertising on one complete backend lifecycle strategy."""
    return safe_verified_retirement_live_validated or (
        retained_state_reconciliation and strict_anti_grief_limits
    )


# This is set only because the stock-LND two-node test is a required CI job.
COFUNDED_CHANNEL_RING_V1_SAFE_RETIREMENT_LIVE_VALIDATED = True
COFUNDED_CHANNEL_RING_V1_CAPABLE = can_advertise_cofunded_channel_ring_v1(
    safe_verified_retirement_live_validated=(
        COFUNDED_CHANNEL_RING_V1_SAFE_RETIREMENT_LIVE_VALIDATED
    ),
    retained_state_reconciliation=False,
    strict_anti_grief_limits=False,
)


class AcceptorBounds(StrictModel):
    # Every ring channel is a final Taproot commitment, whose outputs are dust below
    # 354 satoshis, which is also exactly what stock LND proposes for this type. A
    # lower limit would let a peer create commitment outputs that relay poorly.
    min_dust_limit_sat: Annotated[int, Field(ge=0)] = 354
    max_dust_limit_sat: Annotated[int, Field(gt=0)] = 10_000
    min_max_value_in_flight_msat: Annotated[int, Field(ge=0)] = 0
    max_max_value_in_flight_msat: Annotated[int, Field(gt=0)] = 21_000_000 * 100_000_000_000
    min_min_htlc_msat: Annotated[int, Field(ge=0)] = 0
    max_min_htlc_msat: Annotated[int, Field(gt=0)] = 100_000_000
    min_fee_per_kw: Annotated[int, Field(gt=0)] = 1
    # The opener pays the commitment fee out of its own ring contribution, so an
    # absurd rate is a griefing surface that the exact readiness accounting only
    # catches once a channel already exists. Anchor commitments cap the rate at
    # 10 sat/vB (2,500 sat/kw) by default, so this leaves generous headroom while
    # still rejecting a nonsense rate before anything is negotiated.
    max_fee_per_kw: Annotated[int, Field(gt=0)] = 12_500
    min_csv_delay: Annotated[int, Field(gt=0)] = 1
    max_csv_delay: Annotated[int, Field(gt=0)] = 2016
    min_accepted_htlcs: Annotated[int, Field(gt=0)] = 1
    max_accepted_htlcs: Annotated[int, Field(gt=0)] = 483


class InboundChannelExpectation(StrictModel):
    pending_channel_id: Bytes32
    opener_node_id: NodeId
    chain_hash: Bytes32
    capacity_sat: Annotated[int, Field(gt=0)]
    push_msat: Annotated[int, Field(gt=0)]
    opener_reserve_sat: Annotated[int, Field(gt=0)]
    fundee_reserve_sat: Annotated[int, Field(gt=0)]
    opener_csv_delay: Annotated[int, Field(ge=1, le=2016)]
    fundee_csv_delay: Annotated[int, Field(ge=1, le=2016)]
    min_depth: Annotated[int, Field(ge=1, le=144)]
    scid_alias: bool = False

    @model_validator(mode="after")
    def validate_policy(self) -> InboundChannelExpectation:
        if self.push_msat >= self.capacity_sat * 1000:
            raise ValueError("push amount must be less than channel capacity")
        if self.opener_reserve_sat * 5 >= self.capacity_sat:
            raise ValueError("opener reserve must be less than 20% of capacity")
        if self.fundee_reserve_sat * 5 >= self.capacity_sat:
            raise ValueError("fundee reserve must be less than 20% of capacity")
        return self


class AcceptorDecision(StrictModel):
    accept: bool
    error: str = ""


class AcceptorObservation(StrictModel):
    pending_channel_id: Bytes32
    opener_node_id: NodeId
    capacity_sat: int
    push_msat: int


class EndpointRole(StrEnum):
    OPENER = "opener"
    FUNDEE = "fundee"


class PendingChannelExpectation(StrictModel):
    pending_channel_id: Bytes32
    channel_point: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}:[0-9]+$")]
    opener_node_id: NodeId
    fundee_node_id: NodeId
    capacity_sat: Annotated[int, Field(gt=0)]
    opener_contribution_sat: Annotated[int, Field(gt=0)]
    fundee_contribution_sat: Annotated[int, Field(gt=0)]
    opener_reserve_sat: Annotated[int, Field(gt=0)]
    fundee_reserve_sat: Annotated[int, Field(gt=0)]
    commitment_overhead_sat: Annotated[int, Field(ge=0)] = FINAL_TAPROOT_COMMITMENT_OVERHEAD_SAT

    @model_validator(mode="after")
    def validate_contributions(self) -> PendingChannelExpectation:
        if self.opener_contribution_sat + self.fundee_contribution_sat != self.capacity_sat:
            raise ValueError("endpoint contributions must sum to capacity")
        return self


class PendingChannelObservation(StrictModel):
    role: EndpointRole
    local_node_id: NodeId
    remote_node_id: NodeId
    channel_point: str
    capacity_sat: int
    local_balance_sat: int
    remote_balance_sat: int
    commit_fee_sat: int
    commitment_overhead_sat: int
    local_reserve_sat: int
    remote_reserve_sat: int
    commitment_type: int
    initiator: int
    private: bool


class EndpointReadiness(StrictModel):
    opener: PendingChannelObservation
    fundee: PendingChannelObservation


class ReadinessStatement(StrictModel):
    pending_channel_id: Hex32
    channel_point: str
    unsigned_txid: Hex32
    funding_script_pubkey: ScriptHex
    capacity_sat: Annotated[int, Field(gt=0)]
    push_sat: Annotated[int, Field(gt=0)]
    opener_node_id: NodeId
    fundee_node_id: NodeId
    chain_hash: Hex32

    def canonical_bytes(self) -> bytes:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        return b"jmswap-lnd-readiness-v1\x00" + payload


@dataclass
class _PendingOpen:
    negotiation: FundingNegotiation
    stream: Any
    verified: VerifiedFunding | None = None
    # False for a shim restored after a restart with no durable proof of whether
    # PsbtVerify already consumed it. Such a shim can never be reported as safely
    # canceled, because LND answers "no funding intent found" both for a shim that
    # never existed and for one that verification consumed.
    shim_state_known: bool = True


@dataclass
class _InboundAcceptorRegistration:
    expected: InboundChannelExpectation
    bounds: AcceptorBounds
    future: asyncio.Future[AcceptorObservation]


def _psbt_kv(key_type: int, value: bytes) -> bytes:
    key = bytes([key_type])
    return encode_varint(1) + key + encode_varint(len(value)) + value


def build_unsigned_psbt(
    raw_transaction_hex: str, input_utxos: list[WitnessUtxo]
) -> tuple[bytes, str]:
    """Build a complete BIP174 PSBT without changing the transaction TXID."""
    try:
        transaction_bytes = bytes.fromhex(raw_transaction_hex)
        tx = parse_transaction(raw_transaction_hex)
    except (ValueError, IndexError, struct.error) as exc:
        raise LndValidationError("external funding transaction is invalid") from exc
    if len(input_utxos) != len(tx.inputs):
        raise LndValidationError(
            f"input_utxos has {len(input_utxos)} entries, transaction has {len(tx.inputs)} inputs"
        )
    if any(tx_input.scriptsig for tx_input in tx.inputs):
        raise LndValidationError(
            "external funding inputs must use native SegWit with empty scriptSig"
        )
    if tx.has_witness:
        raise LndValidationError("external funding transaction must not contain witness data")
    unsigned_tx = serialize_transaction(
        tx.version, tx.inputs, tx.outputs, tx.locktime, witnesses=None
    )
    if unsigned_tx != transaction_bytes:
        raise LndValidationError("external funding transaction must use canonical serialization")
    txid = get_txid(raw_transaction_hex)
    if get_txid(unsigned_tx.hex()) != txid:
        raise LndValidationError("removing witnesses changed the funding transaction TXID")

    psbt = _PSBT_MAGIC + _psbt_kv(_PSBT_GLOBAL_UNSIGNED_TX, unsigned_tx) + b"\x00"
    for utxo in input_utxos:
        witness_utxo = serialize_output(TxOutput(value=utxo.value_sat, script=utxo.script_pubkey))
        psbt += _psbt_kv(_PSBT_IN_WITNESS_UTXO, witness_utxo) + b"\x00"
    psbt += b"\x00" * len(tx.outputs)
    return psbt, txid


def validate_contribution_accounting(
    observation: PendingChannelObservation, expected: PendingChannelExpectation
) -> None:
    """Validate LND's persisted commitment-fee accounting for either endpoint."""
    if observation.commit_fee_sat < 0:
        raise LndValidationError("negative commitment fee in PendingChannels")
    if observation.role is EndpointRole.OPENER:
        opener_reported_gross = observation.local_balance_sat + observation.commit_fee_sat
        fundee_balance = observation.remote_balance_sat
    else:
        opener_reported_gross = observation.remote_balance_sat + observation.commit_fee_sat
        fundee_balance = observation.local_balance_sat
    if observation.commitment_overhead_sat != expected.commitment_overhead_sat:
        raise LndValidationError(
            f"commitment overhead {observation.commitment_overhead_sat} "
            f"!= {expected.commitment_overhead_sat}"
        )
    expected_reported_gross = expected.opener_contribution_sat - expected.commitment_overhead_sat
    if opener_reported_gross != expected_reported_gross:
        raise LndValidationError(
            f"opener balance plus persisted commitment fee {opener_reported_gross} "
            f"!= {expected_reported_gross}"
        )
    if fundee_balance != expected.fundee_contribution_sat:
        raise LndValidationError(
            f"fundee pushed balance {fundee_balance} != {expected.fundee_contribution_sat}"
        )
    if (
        observation.local_balance_sat
        + observation.remote_balance_sat
        + observation.commit_fee_sat
        + observation.commitment_overhead_sat
        != expected.capacity_sat
    ):
        raise LndValidationError(
            "pending balances, commitment fee, and commitment overhead do not equal capacity"
        )


def evaluate_channel_accept_request(
    request: Any, expected: InboundChannelExpectation, bounds: AcceptorBounds
) -> AcceptorDecision:
    checks: tuple[tuple[bool, str], ...] = (
        (bytes(request.pending_chan_id) == expected.pending_channel_id, "pending channel ID"),
        (request.node_pubkey.hex() == expected.opener_node_id, "opener node"),
        (bytes(request.chain_hash) == expected.chain_hash, "chain hash"),
        (int(request.funding_amt) == expected.capacity_sat, "capacity"),
        (int(request.push_amt) == expected.push_msat, "push amount"),
        (int(request.commitment_type) == FINAL_TAPROOT_COMMITMENT, "commitment type"),
        (int(request.channel_flags) == 0, "private channel flags"),
        (not bool(request.wants_zero_conf), "zero-conf flag"),
        (bool(request.wants_scid_alias) == expected.scid_alias, "SCID alias flag"),
        (int(request.channel_reserve) == expected.fundee_reserve_sat, "fundee reserve"),
        (int(request.csv_delay) == expected.fundee_csv_delay, "fundee CSV policy"),
        (
            bounds.min_csv_delay <= expected.opener_csv_delay <= bounds.max_csv_delay
            and bounds.min_csv_delay <= expected.fundee_csv_delay <= bounds.max_csv_delay,
            "CSV policy bounds",
        ),
        (
            bounds.min_dust_limit_sat <= int(request.dust_limit) <= bounds.max_dust_limit_sat,
            "dust limit policy",
        ),
        (
            bounds.min_max_value_in_flight_msat
            <= int(request.max_value_in_flight)
            <= min(bounds.max_max_value_in_flight_msat, expected.capacity_sat * 1000),
            "max value in flight policy",
        ),
        (
            bounds.min_min_htlc_msat <= int(request.min_htlc) <= bounds.max_min_htlc_msat,
            "minimum HTLC policy",
        ),
        (bounds.min_fee_per_kw <= int(request.fee_per_kw) <= bounds.max_fee_per_kw, "fee policy"),
        (
            bounds.min_accepted_htlcs
            <= int(request.max_accepted_htlcs)
            <= bounds.max_accepted_htlcs,
            "HTLC count policy",
        ),
    )
    for valid, label in checks:
        if not valid:
            return AcceptorDecision(accept=False, error=f"co-funded channel rejected: {label}")
    return AcceptorDecision(accept=True)


def validate_endpoint_readiness(
    opener: PendingChannelObservation,
    fundee: PendingChannelObservation,
    expected: PendingChannelExpectation,
) -> EndpointReadiness:
    if opener.role is not EndpointRole.OPENER or fundee.role is not EndpointRole.FUNDEE:
        raise LndValidationError("both opener and fundee endpoint observations are mandatory")
    for observation in (opener, fundee):
        validate_contribution_accounting(observation, expected)
        if observation.channel_point != expected.channel_point:
            raise LndValidationError("endpoint channel points differ")
    if (
        opener.local_node_id != expected.opener_node_id
        or fundee.local_node_id != expected.fundee_node_id
    ):
        raise LndValidationError("endpoint node identities differ from the channel contract")
    if (
        opener.remote_node_id != fundee.local_node_id
        or fundee.remote_node_id != opener.local_node_id
    ):
        raise LndValidationError("endpoint observations are not reciprocal")
    if opener.local_balance_sat != fundee.remote_balance_sat:
        raise LndValidationError("opener local balance differs from fundee remote balance")
    if opener.remote_balance_sat != fundee.local_balance_sat:
        raise LndValidationError("fundee local balance differs from opener remote balance")
    return EndpointReadiness(opener=opener, fundee=fundee)


@dataclass
class LndBackend:
    host: str
    tls_cert: bytes = field(repr=False)
    macaroon_hex: str = field(repr=False)
    network: str = "regtest"
    escrow_macaroon_hex: str | None = field(default=None, repr=False)
    _channel: Any = field(default=None, repr=False)
    _escrow_channel: Any = field(default=None, repr=False)
    _stub: Any = field(default=None, repr=False)
    _router_stub: Any = field(default=None, repr=False)
    _invoices_stub: Any = field(default=None, repr=False)
    _escrow_stub: Any = field(default=None, repr=False)
    _pending: dict[bytes, _PendingOpen] = field(default_factory=dict, repr=False)
    _seen_pending_ids: set[bytes] = field(default_factory=set, repr=False)
    _accepted: dict[bytes, InboundChannelExpectation] = field(default_factory=dict, repr=False)
    _observed_verified_points: dict[bytes, str] = field(default_factory=dict, repr=False)
    _acceptor_registrations: dict[bytes, _InboundAcceptorRegistration] = field(
        default_factory=dict, repr=False
    )
    _acceptor_call: Any = field(default=None, repr=False)
    _acceptor_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _acceptor_started: asyncio.Future[None] | None = field(default=None, repr=False)

    @classmethod
    def from_paths(
        cls,
        host: str,
        tls_cert_path: str | Path,
        macaroon_path: str | Path,
        network: str = "regtest",
        escrow_macaroon_path: str | Path | None = None,
    ) -> LndBackend:
        return cls(
            host=host,
            tls_cert=Path(tls_cert_path).read_bytes(),
            macaroon_hex=Path(macaroon_path).read_bytes().hex(),
            network=network,
            escrow_macaroon_hex=(
                Path(escrow_macaroon_path).read_bytes().hex()
                if escrow_macaroon_path is not None
                else None
            ),
        )

    def _ensure(self) -> None:
        if self._stub is not None:
            return
        import grpc

        from jmswap.lndrpc import lightning_pb2_grpc

        ssl = grpc.ssl_channel_credentials(self.tls_cert)

        def add_macaroon(_context: Any, callback: Any) -> None:
            callback((("macaroon", self.macaroon_hex),), None)

        credentials = grpc.composite_channel_credentials(
            ssl, grpc.metadata_call_credentials(add_macaroon)
        )
        self._channel = grpc.aio.secure_channel(self.host, credentials)
        self._stub = lightning_pb2_grpc.LightningStub(  # type: ignore[no-untyped-call]
            self._channel
        )

    def _ensure_router(self) -> None:
        self._ensure()
        if self._router_stub is not None:
            return
        if self._channel is None:
            raise LndRpcError("LND RouterRPC requires an authenticated LND channel")
        from jmswap.lndrpc import router_pb2_grpc

        self._router_stub = router_pb2_grpc.RouterStub(  # type: ignore[no-untyped-call]
            self._channel
        )

    def _ensure_invoices(self) -> None:
        self._ensure()
        if self._invoices_stub is not None:
            return
        if self._channel is None:
            raise LndRpcError("LND InvoicesRPC requires an authenticated LND channel")
        from jmswap.lndrpc import invoices_pb2_grpc

        self._invoices_stub = invoices_pb2_grpc.InvoicesStub(  # type: ignore[no-untyped-call]
            self._channel
        )

    def _ensure_escrow(self) -> None:
        if self._escrow_stub is not None:
            return
        if not self.escrow_macaroon_hex:
            raise LndEscrowCredentialError(
                "ChannelEscrowRPC requires a dedicated escrow macaroon credential"
            )
        import grpc

        from jmswap.lndrpc import channelescrow_pb2_grpc

        ssl = grpc.ssl_channel_credentials(self.tls_cert)

        def add_escrow_macaroon(_context: Any, callback: Any) -> None:
            callback((("macaroon", self.escrow_macaroon_hex),), None)

        credentials = grpc.composite_channel_credentials(
            ssl, grpc.metadata_call_credentials(add_escrow_macaroon)
        )
        self._escrow_channel = grpc.aio.secure_channel(self.host, credentials)
        self._escrow_stub = channelescrow_pb2_grpc.ChannelEscrowStub(  # type: ignore[no-untyped-call]
            self._escrow_channel
        )

    async def close(self) -> None:
        if self._acceptor_task is not None:
            self._acceptor_task.cancel()
            await asyncio.gather(self._acceptor_task, return_exceptions=True)
        self._acceptor_task = None
        self._acceptor_started = None
        self._acceptor_call = None
        self._acceptor_registrations.clear()
        channels = (self._escrow_channel, self._channel)
        closed: set[int] = set()
        for channel in channels:
            if channel is not None and id(channel) not in closed:
                await channel.close()
                closed.add(id(channel))
        self._channel = None
        self._escrow_channel = None
        self._stub = None
        self._router_stub = None
        self._invoices_stub = None
        self._escrow_stub = None

    @staticmethod
    def _escrow_session_id(session_id: str) -> bytes:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("escrow session ID must be a non-empty string")
        return hashlib.sha256(session_id.encode()).digest()

    @staticmethod
    def _parse_escrow_outpoint(channel_point: str) -> EscrowOutpoint:
        try:
            txid, vout = channel_point.rsplit(":", 1)
            return EscrowOutpoint(txid=txid, vout=int(vout))
        except (TypeError, ValueError) as exc:
            raise ValueError("channel point must use lowercase txid:vout format") from exc

    @staticmethod
    def _rpc_escrow_outpoint(outpoint: EscrowOutpoint) -> Any:
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        return escrow.ChannelPoint(
            funding_txid=bytes.fromhex(outpoint.txid)[::-1],
            output_index=outpoint.vout,
        )

    @staticmethod
    def _rpc_escrow_txid(raw_hash: bytes) -> str:
        if len(raw_hash) != 32:
            raise LndValidationError("ChannelEscrowRPC returned an invalid transaction ID")
        return raw_hash[::-1].hex()

    @staticmethod
    def _validate_timeout(timeout_seconds: float) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")

    @staticmethod
    def _validate_maintenance_timeout(timeout_seconds: int) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= _MAX_MAINTENANCE_PAYMENT_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"timeout_seconds must be an integer from 1 to {_MAX_MAINTENANCE_PAYMENT_TIMEOUT_SECONDS}"
            )

    @staticmethod
    def _validate_maintenance_uint(
        name: str, value: int, maximum: int, *, minimum: int = 0
    ) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")

    @staticmethod
    def _is_not_found(error: Exception) -> bool:
        import grpc

        code = getattr(error, "code", None)
        return callable(code) and code() == grpc.StatusCode.NOT_FOUND

    @staticmethod
    def _maintenance_payment_from_update(update: Any) -> MaintenancePayment:
        try:
            status = int(update.status)
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError("RouterRPC returned an invalid payment update") from exc
        if status == ln.Payment.SUCCEEDED:
            fee_msat = int(update.fee_msat)
            if fee_msat != 0:
                raise LndValidationError(
                    f"maintenance payment settled with nonzero fee: {fee_msat} msat"
                )
            return MaintenancePayment(status="succeeded", fee_msat=fee_msat)
        if status == ln.Payment.FAILED:
            return MaintenancePayment(status="failed", fee_msat=None)
        if status in (ln.Payment.IN_FLIGHT, ln.Payment.INITIATED):
            return MaintenancePayment(status="in_flight", fee_msat=None)
        return MaintenancePayment(status="unknown", fee_msat=None)

    @classmethod
    def _maintenance_node_id(cls, name: str, value: Any) -> str:
        if not isinstance(value, str):
            raise LndValidationError(f"{name} must be a compressed public key")
        try:
            return cls._validate_channel_input_peer_filter(value)
        except ValueError as exc:
            raise LndValidationError(f"{name} must be a compressed public key") from exc

    @classmethod
    def _maintenance_exact_route_from_invoice(
        cls, invoice: Any, local_identity: str, block_height: Any
    ) -> _MaintenanceExactRoute:
        """Validate the fixed invoice shape and derive all route locktimes."""
        try:
            cls._validate_maintenance_uint("block height", block_height, _UINT32_MAX)
            destination = cls._maintenance_node_id("invoice destination", invoice.destination)
            if destination != local_identity:
                raise LndValidationError("maintenance invoice destination is not this LND node")

            amount_sat = invoice.num_satoshis
            amount_msat = invoice.num_msat
            cls._validate_maintenance_uint(
                "invoice amount in satoshis", amount_sat, _INT64_MAX // 1000, minimum=1
            )
            cls._validate_maintenance_uint(
                "invoice amount in millisatoshis", amount_msat, _INT64_MAX, minimum=1000
            )
            if amount_msat != amount_sat * 1000:
                raise LndValidationError("maintenance invoice amount must be whole satoshis")

            timestamp = invoice.timestamp
            expiry = invoice.expiry
            cls._validate_maintenance_uint("invoice timestamp", timestamp, _INT64_MAX, minimum=1)
            cls._validate_maintenance_uint("invoice expiry", expiry, _INT64_MAX, minimum=1)
            if timestamp + expiry <= int(time.time()):
                raise LndValidationError("maintenance invoice has expired")

            payment_hash = bytes.fromhex(str(invoice.payment_hash))
            if len(payment_hash) != 32 or payment_hash.hex() != str(invoice.payment_hash):
                raise LndValidationError("maintenance invoice has an invalid payment hash")
            payment_addr = bytes(invoice.payment_addr)
            if len(payment_addr) != 32:
                raise LndValidationError("maintenance invoice has an invalid payment address")
            if len(invoice.blinded_paths) != 0:
                raise LndValidationError("maintenance invoice must not contain blinded paths")
            if len(invoice.route_hints) != 1 or len(invoice.route_hints[0].hop_hints) != 2:
                raise LndValidationError(
                    "maintenance invoice must contain exactly one two-hop route hint"
                )

            helper_hint, return_hint = invoice.route_hints[0].hop_hints
            helper_peer = cls._maintenance_node_id("maintenance helper peer", helper_hint.node_id)
            return_peer = cls._maintenance_node_id("maintenance return peer", return_hint.node_id)
            if len({local_identity, helper_peer, return_peer}) != 3:
                raise LndValidationError(
                    "maintenance route peers must be distinct from this LND node"
                )
            for label, hint in (("helper", helper_hint), ("return", return_hint)):
                cls._validate_maintenance_uint(
                    f"{label} channel alias", hint.chan_id, _UINT64_MAX, minimum=1
                )
                cls._validate_maintenance_uint(f"{label} base fee", hint.fee_base_msat, _UINT32_MAX)
                cls._validate_maintenance_uint(
                    f"{label} proportional fee", hint.fee_proportional_millionths, _UINT32_MAX
                )
                cls._validate_maintenance_uint(
                    f"{label} CLTV delta",
                    hint.cltv_expiry_delta,
                    _MAX_MAINTENANCE_CLTV_EXPOSURE,
                    minimum=1,
                )
                if hint.fee_base_msat != 0 or hint.fee_proportional_millionths != 0:
                    raise LndValidationError("maintenance route hints must charge zero fees")

            cltv_expiry = invoice.cltv_expiry
            cls._validate_maintenance_uint(
                "invoice CLTV expiry",
                cltv_expiry,
                _MAX_MAINTENANCE_CLTV_EXPOSURE,
                minimum=1,
            )
            final_expiry = block_height + cltv_expiry + _MAINTENANCE_FINAL_CLTV_PADDING
            helper_expiry = final_expiry + return_hint.cltv_expiry_delta
            total_time_lock = helper_expiry + helper_hint.cltv_expiry_delta
            if total_time_lock - block_height > _MAX_MAINTENANCE_CLTV_EXPOSURE:
                raise LndValidationError("maintenance route CLTV exposure exceeds 288 blocks")
            if (
                final_expiry > _UINT32_MAX
                or helper_expiry > _UINT32_MAX
                or total_time_lock > _UINT32_MAX
            ):
                raise LndValidationError("maintenance route CLTV height exceeds uint32")
            return _MaintenanceExactRoute(
                payment_hash=payment_hash,
                payment_addr=payment_addr,
                amount_msat=amount_msat,
                helper_peer_node_id=helper_peer,
                return_peer_node_id=return_peer,
                helper_channel_alias=helper_hint.chan_id,
                return_channel_alias=return_hint.chan_id,
                final_expiry=final_expiry,
                helper_expiry=helper_expiry,
                total_time_lock=total_time_lock,
            )
        except LndValidationError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(f"LND returned an invalid maintenance invoice: {exc}") from exc

    @classmethod
    def _validate_maintenance_route_channels(
        cls, response: Any, route: _MaintenanceExactRoute, outgoing_channel_id: int
    ) -> None:
        """Bind the caller's first hop and invoice return alias to local channels."""
        try:
            outgoing: list[tuple[int, str, int, bool, bool]] = []
            returns: list[tuple[int, str, int, bool, bool]] = []
            for channel in response.channels:
                channel_id = channel.chan_id
                peer_node_id = cls._maintenance_node_id("channel peer", channel.remote_pubkey)
                peer_alias = channel.peer_scid_alias
                active = channel.active
                private = channel.private
                cls._validate_maintenance_uint("channel ID", channel_id, _UINT64_MAX, minimum=1)
                cls._validate_maintenance_uint("channel peer alias", peer_alias, _UINT64_MAX)
                if not isinstance(active, bool) or not isinstance(private, bool):
                    raise ValueError("channel activity and privacy flags must be booleans")
                observed = (channel_id, peer_node_id, peer_alias, active, private)
                if channel_id == outgoing_channel_id:
                    outgoing.append(observed)
                if (
                    peer_node_id == route.return_peer_node_id
                    and peer_alias == route.return_channel_alias
                ):
                    returns.append(observed)
            if len(outgoing) != 1:
                raise LndValidationError("outgoing maintenance channel was not uniquely found")
            if outgoing[0][1] != route.helper_peer_node_id:
                raise LndValidationError(
                    "outgoing maintenance channel does not belong to helper peer"
                )
            if not outgoing[0][3] or not outgoing[0][4]:
                raise LndValidationError("outgoing maintenance channel must be active and private")
            if len(returns) != 1:
                raise LndValidationError("return maintenance channel was not uniquely found")
            return_channel = returns[0]
            if return_channel[0] == outgoing_channel_id:
                raise LndValidationError(
                    "return maintenance channel must differ from outgoing channel"
                )
            if not return_channel[3] or not return_channel[4]:
                raise LndValidationError("return maintenance channel must be active and private")
        except LndValidationError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(
                f"LND returned invalid maintenance channel data: {exc}"
            ) from exc

    @staticmethod
    def _maintenance_payment_from_attempt(attempt: Any, payment_hash: bytes) -> MaintenancePayment:
        try:
            status = int(attempt.status)
            if status == ln.HTLCAttempt.SUCCEEDED:
                if not attempt.HasField("route"):
                    raise ValueError("successful attempt omitted its route")
                route = attempt.route
                if int(route.total_fees_msat) != 0 or int(route.total_fees) != 0:
                    raise LndValidationError("maintenance payment settled with nonzero route fees")
                if any(int(hop.fee_msat) != 0 or int(hop.fee) != 0 for hop in route.hops):
                    raise LndValidationError(
                        "maintenance payment settled with nonzero route hop fees"
                    )
                preimage = bytes(attempt.preimage)
                if preimage and (
                    len(preimage) != 32 or hashlib.sha256(preimage).digest() != payment_hash
                ):
                    raise LndValidationError("maintenance payment returned an invalid preimage")
                return MaintenancePayment(status="succeeded", fee_msat=0)
            if status == ln.HTLCAttempt.FAILED:
                return MaintenancePayment(status="failed", fee_msat=None)
            if status == ln.HTLCAttempt.IN_FLIGHT:
                return MaintenancePayment(status="in_flight", fee_msat=None)
            raise ValueError(f"unknown HTLC attempt status {status}")
        except LndValidationError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(
                f"RouterRPC returned an invalid maintenance attempt: {exc}"
            ) from exc

    @staticmethod
    def _maintenance_channel_policies(edge: Any, peer_node_id: str) -> tuple[Any, Any] | None:
        try:
            node1_is_peer = str(edge.node1_pub) == peer_node_id
            node2_is_peer = str(edge.node2_pub) == peer_node_id
            if node1_is_peer == node2_is_peer:
                return None
            local_field, remote_field = (
                ("node2_policy", "node1_policy")
                if node1_is_peer
                else ("node1_policy", "node2_policy")
            )
            if not edge.HasField(local_field) or not edge.HasField(remote_field):
                return None
            return getattr(edge, local_field), getattr(edge, remote_field)
        except (AttributeError, TypeError, ValueError):
            return None

    @classmethod
    def _maintenance_channel_policy_pair(
        cls, edges: Any, channel_point: str, peer_node_id: str
    ) -> tuple[Any, Any] | None:
        for edge in edges:
            if str(edge.chan_point) != channel_point:
                continue
            policies = cls._maintenance_channel_policies(edge, peer_node_id)
            if policies is not None:
                return policies
        return None

    @staticmethod
    def _validate_channel_input_peer_filter(peer_node_pubkey: str) -> str:
        if (
            not isinstance(peer_node_pubkey, str)
            or re.fullmatch(r"(?:02|03)[0-9a-f]{64}", peer_node_pubkey) is None
        ):
            raise ValueError("allowed peer public key must be a lowercase compressed public key")
        return peer_node_pubkey

    @staticmethod
    def _is_escrow_session_mismatch(error: Exception) -> bool:
        return LndBackend._escrow_error_details(error) == "escrow session does not match lock"

    @staticmethod
    def _is_escrow_channel_not_found(error: Exception) -> bool:
        return LndBackend._escrow_error_details(error) == "channel not found"

    @staticmethod
    def _escrow_error_details(error: Exception) -> str | None:
        import grpc

        status_code = getattr(error, "code", None)
        details = getattr(error, "details", None)
        if callable(status_code) and callable(details) and status_code() == grpc.StatusCode.UNKNOWN:
            result = details()
            return result if isinstance(result, str) else None
        return None

    async def list_channel_input_candidates(
        self,
        *,
        allowed_outpoints: Sequence[EscrowOutpoint] | None = None,
        allowed_peer_node_pubkeys: Sequence[str] | None = None,
        timeout_seconds: float = 10.0,
    ) -> tuple[ChannelInputCandidate, ...]:
        """List eligible final-Taproot channels without changing their escrow state."""
        self._validate_timeout(timeout_seconds)
        if allowed_outpoints is not None and (
            isinstance(allowed_outpoints, (str, bytes))
            or not isinstance(allowed_outpoints, Sequence)
        ):
            raise ValueError("allowed_outpoints must be a sequence of funding outpoints")
        if allowed_peer_node_pubkeys is not None and (
            isinstance(allowed_peer_node_pubkeys, (str, bytes))
            or not isinstance(allowed_peer_node_pubkeys, Sequence)
        ):
            raise ValueError("allowed_peer_node_pubkeys must be a sequence of public keys")
        allowed_points: frozenset[EscrowOutpoint] | None = None
        if allowed_outpoints is not None:
            for point in allowed_outpoints:
                if not isinstance(point, EscrowOutpoint):
                    raise ValueError("allowed outpoint must be an EscrowOutpoint")
            allowed_points = frozenset(allowed_outpoints)
        allowed_peers: frozenset[str] | None = None
        if allowed_peer_node_pubkeys is not None:
            allowed_peers = frozenset(
                self._validate_channel_input_peer_filter(peer) for peer in allowed_peer_node_pubkeys
            )
        self._ensure()
        try:
            response = await asyncio.wait_for(
                self._stub.ListChannels(ln.ListChannelsRequest()), timeout_seconds
            )
        except TimeoutError as exc:
            raise LndTimeoutError("ListChannels timed out") from exc
        except Exception as exc:
            raise LndRpcError(f"ListChannels failed: {exc}") from exc
        try:
            candidates: list[ChannelInputCandidate] = []
            for channel in response.channels:
                if (
                    not isinstance(channel.active, bool)
                    or not isinstance(channel.commitment_type, int)
                    or not isinstance(channel.chan_status_flags, str)
                    or not isinstance(channel.capacity, int)
                    or not isinstance(channel.pending_htlcs, Sequence)
                ):
                    raise ValueError("invalid channel eligibility fields")
                if (
                    not channel.active
                    or channel.commitment_type != FINAL_TAPROOT_COMMITMENT
                    or channel.chan_status_flags != "ChanStatusDefault"
                    or channel.capacity <= 0
                    or len(channel.pending_htlcs) != 0
                ):
                    continue
                candidate = ChannelInputCandidate(
                    channel_id=channel.chan_id,
                    funding_outpoint=self._parse_escrow_outpoint(channel.channel_point),
                    peer_node_pubkey=channel.remote_pubkey,
                    capacity_sat=channel.capacity,
                    local_balance_sat=channel.local_balance,
                    remote_balance_sat=channel.remote_balance,
                    private=channel.private,
                    active=channel.active,
                    default_channel_state=channel.chan_status_flags == "ChanStatusDefault",
                    pending_htlc_count=len(channel.pending_htlcs),
                    commitment_type=channel.commitment_type,
                )
                if (allowed_points is None or candidate.funding_outpoint in allowed_points) and (
                    allowed_peers is None or candidate.peer_node_pubkey in allowed_peers
                ):
                    candidates.append(candidate)
            return tuple(
                sorted(
                    candidates,
                    key=lambda candidate: (
                        candidate.funding_outpoint.txid,
                        candidate.funding_outpoint.vout,
                        candidate.channel_id,
                    ),
                )
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(f"ListChannels returned invalid channel data: {exc}") from exc

    async def probe_channel_input_escrow(
        self, funding_outpoint: EscrowOutpoint, *, timeout_seconds: float = 10.0
    ) -> None:
        """Prove the dedicated escrow service is live without changing channel state."""
        self._validate_timeout(timeout_seconds)
        if not isinstance(funding_outpoint, EscrowOutpoint):
            raise ValueError("funding_outpoint must be an EscrowOutpoint")
        self._ensure_escrow()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        probe_session_id = self._escrow_session_id(
            f"channel-input-escrow-probe:{secrets.token_hex(32)}"
        )
        try:
            await asyncio.wait_for(
                self._escrow_stub.Status(
                    escrow.StatusRequest(
                        channel_point=self._rpc_escrow_outpoint(funding_outpoint),
                        session_id=probe_session_id,
                    )
                ),
                timeout_seconds,
            )
        except TimeoutError as exc:
            raise LndTimeoutError("ChannelEscrowRPC Status probe timed out") from exc
        except Exception as exc:
            # Both domain errors prove the dedicated escrow service processed
            # the request. "channel not found" is expected after a configured
            # channel's funding outpoint was legitimately spent by a completed
            # round; an absent or unauthorized escrow service fails with a
            # transport-level status instead.
            if self._is_escrow_session_mismatch(exc) or self._is_escrow_channel_not_found(exc):
                return
            raise LndCapabilityError(f"ChannelEscrowRPC Status probe failed: {exc}") from exc
        raise LndCapabilityError(
            "ChannelEscrowRPC Status probe unexpectedly found a random escrow session"
        )

    async def force_close_channel_input_recovery(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_txid: str,
        recovery_height: int,
        current_height: int,
        *,
        timeout_seconds: float = 10.0,
    ) -> ChannelInputForceCloseOutcome:
        """Force-close only after durable signature release and transcript recovery height."""
        self._validate_timeout(timeout_seconds)
        if not isinstance(parent_txid, str) or re.fullmatch(r"[0-9a-f]{64}", parent_txid) is None:
            raise ValueError("parent transaction ID must be a lowercase 32-byte hexadecimal value")
        for name, height in (
            ("recovery_height", recovery_height),
            ("current_height", current_height),
        ):
            if (
                isinstance(height, bool)
                or not isinstance(height, int)
                or not 0 <= height <= 0xFFFFFFFF
            ):
                raise ValueError(f"{name} must be a valid block height")

        try:
            status = await asyncio.wait_for(
                self.channel_input_escrow_status(session_id, funding_outpoint), timeout_seconds
            )
        except TimeoutError as exc:
            raise LndTimeoutError("ChannelEscrowRPC Status timed out") from exc
        except LndError:
            raise
        except Exception as exc:
            raise LndRpcError(f"ChannelEscrowRPC Status failed: {exc}") from exc
        if status.stage not in (LndEscrowStage.PARENT_SIGNED, LndEscrowStage.FINALIZED):
            raise LndValidationError("force-close recovery requires a signed escrow parent")
        if status.parent_txid != parent_txid:
            raise LndValidationError(
                "escrow parent transaction ID differs from recovery transcript"
            )
        if current_height < recovery_height:
            raise LndValidationError(
                "force-close recovery is not yet permitted by transcript height"
            )

        self._ensure()
        request = ln.CloseChannelRequest(
            channel_point=ln.ChannelPoint(
                funding_txid_bytes=bytes.fromhex(funding_outpoint.txid)[::-1],
                output_index=funding_outpoint.vout,
            ),
            force=True,
        )
        try:
            stream = self._stub.CloseChannel(request)
            update = await asyncio.wait_for(stream.read(), timeout_seconds)
        except TimeoutError as exc:
            raise LndTimeoutError("CloseChannel timed out waiting for close_pending") from exc
        except Exception as exc:
            raise LndRpcError(f"CloseChannel failed: {exc}") from exc
        try:
            if update.WhichOneof("update") != "close_pending":
                raise ValueError("CloseChannel did not return a close_pending update")
            pending = update.close_pending
            if not pending.local_close_tx:
                raise ValueError("CloseChannel close_pending update is not locally initiated")
            raw_txid = bytes(pending.txid)
            if len(raw_txid) != 32:
                raise ValueError("CloseChannel returned an invalid commitment transaction ID")
            return ChannelInputForceCloseOutcome(
                funding_outpoint=funding_outpoint,
                commitment_txid=raw_txid[::-1].hex(),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(
                f"CloseChannel returned invalid close_pending data: {exc}"
            ) from exc

    async def _resolve_escrow_channel_point(self, channel_id: str) -> EscrowOutpoint:
        if ":" in channel_id:
            return self._parse_escrow_outpoint(channel_id)
        self._ensure()
        response = await self._stub.ListChannels(ln.ListChannelsRequest())
        for channel in response.channels:
            if str(channel.chan_id) == channel_id:
                return self._parse_escrow_outpoint(str(channel.channel_point))
        raise LndValidationError(f"LND channel not found: {channel_id}")

    async def freeze_channel_input(self, session_id: str, channel_id: str) -> FrozenChannelSnapshot:
        """Freeze one existing final-Taproot channel for an externally coordinated parent."""
        self._ensure_escrow()
        point = await self._resolve_escrow_channel_point(channel_id)
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.FreezeChannel(
            escrow.FreezeChannelRequest(
                channel_point=self._rpc_escrow_outpoint(point),
                session_id=self._escrow_session_id(session_id),
            )
        )
        try:
            if not response.HasField("funding_outpoint"):
                raise ValueError("missing frozen funding outpoint")
            frozen_point = EscrowOutpoint(
                txid=self._rpc_escrow_txid(bytes(response.funding_outpoint.funding_txid)),
                vout=int(response.funding_outpoint.output_index),
            )
            if frozen_point != point:
                raise ValueError("frozen outpoint differs from the requested channel")
            return FrozenChannelSnapshot(
                session_id=session_id,
                channel_id=channel_id,
                funding_outpoint=frozen_point,
                funding_scriptpubkey=bytes(response.funding_script),
                peer_node_pubkey=bytes(response.peer_pubkey),
                local_funding_pubkey=bytes(response.local_funding_pubkey),
                remote_funding_pubkey=bytes(response.remote_funding_pubkey),
                capacity_sat=int(response.capacity_sat),
                local_claim_sat=int(response.local_claim_sat),
                remote_claim_sat=int(response.remote_claim_sat),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(
                "ChannelEscrowRPC returned invalid frozen final-Taproot channel data"
            ) from exc

    @staticmethod
    def _escrow_prevouts(
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo | tuple[int, bytes]],
        channel_input_index: int,
        funding_outpoint: EscrowOutpoint,
    ) -> tuple[bytes, list[WitnessUtxo]]:
        try:
            raw_parent_tx = bytes.fromhex(parent_tx_hex)
            parent = parse_transaction(parent_tx_hex)
            if not raw_parent_tx or len(input_utxos) != len(parent.inputs):
                raise ValueError("parent transaction and prevouts differ in input count")
            if not 0 <= channel_input_index < len(parent.inputs):
                raise ValueError("channel input index is outside the parent transaction")
            channel_input = parent.inputs[channel_input_index]
            if (
                channel_input.txid != funding_outpoint.txid
                or channel_input.vout != funding_outpoint.vout
            ):
                raise ValueError("channel input does not spend the frozen funding outpoint")
            prevouts = [
                utxo
                if isinstance(utxo, WitnessUtxo)
                else WitnessUtxo(value_sat=utxo[0], script_pubkey=utxo[1])
                for utxo in input_utxos
            ]
        except (IndexError, TypeError, ValueError) as exc:
            raise ValueError("invalid final-Taproot parent transaction or prevouts") from exc
        return raw_parent_tx, prevouts

    async def prepare_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        parent_tx_hex: str,
        input_utxos: Sequence[WitnessUtxo | tuple[int, bytes]],
        channel_input_index: int,
    ) -> str:
        """Durably bind LND to one exact parent and complete BIP341 prevouts."""
        raw_parent_tx, prevouts = self._escrow_prevouts(
            parent_tx_hex, input_utxos, channel_input_index, funding_outpoint
        )
        expected_txid = get_txid(parent_tx_hex)
        self._ensure_escrow()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.PrepareParent(
            escrow.PrepareParentRequest(
                channel_point=self._rpc_escrow_outpoint(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
                raw_parent_tx=raw_parent_tx,
                prev_outs=[
                    escrow.PrevOut(value_sat=utxo.value_sat, pk_script=utxo.script_pubkey)
                    for utxo in prevouts
                ],
                channel_input_index=channel_input_index,
            )
        )
        response_txid = self._rpc_escrow_txid(bytes(response.txid))
        if response_txid != expected_txid:
            raise LndValidationError("ChannelEscrowRPC authorized an unexpected parent transaction")
        return response_txid

    async def begin_channel_input_signing(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        *,
        restart_attempt: bool = False,
    ) -> LndEscrowSigningAttempt:
        """Create or retrieve the active MuSig2 attempt for the prepared parent."""
        if not isinstance(restart_attempt, bool):
            raise ValueError("restart_attempt must be a boolean")
        self._ensure_escrow()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.BeginSigning(
            escrow.BeginSigningRequest(
                channel_point=self._rpc_escrow_outpoint(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
                restart_attempt=restart_attempt,
            )
        )
        try:
            return LndEscrowSigningAttempt(
                attempt_id=bytes(response.attempt_id),
                public_nonce=bytes(response.local_public_nonce),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(
                "ChannelEscrowRPC returned invalid MuSig2 attempt data"
            ) from exc

    async def sign_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_public_nonce: bytes,
    ) -> bytes:
        """Persist and return this endpoint's partial signature for the active attempt."""
        if len(remote_public_nonce) != 66:
            raise ValueError("remote MuSig2 public nonce must be exactly 66 bytes")
        self._ensure_escrow()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.SignParent(
            escrow.SignParentRequest(
                channel_point=self._rpc_escrow_outpoint(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
                attempt_id=attempt.attempt_id,
                remote_public_nonce=remote_public_nonce,
            )
        )
        partial_signature = bytes(response.local_partial_signature)
        if len(partial_signature) != 32:
            raise LndValidationError(
                "ChannelEscrowRPC returned an invalid MuSig2 partial signature"
            )
        return partial_signature

    async def finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt: LndEscrowSigningAttempt,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        """Finalize an active attempt; the external coordinator owns subsequent broadcast."""
        return await self._finalize_channel_input_parent(
            session_id, funding_outpoint, attempt.attempt_id, remote_partial_signature
        )

    async def recover_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        """Finalize from durable partial-signature evidence after an attempt ID is lost."""
        return await self._finalize_channel_input_parent(
            session_id, funding_outpoint, b"", remote_partial_signature
        )

    async def _finalize_channel_input_parent(
        self,
        session_id: str,
        funding_outpoint: EscrowOutpoint,
        attempt_id: bytes,
        remote_partial_signature: bytes,
    ) -> LndFinalizedEscrowParent:
        if len(attempt_id) not in (0, 32):
            raise ValueError("MuSig2 attempt ID must be empty or exactly 32 bytes")
        if len(remote_partial_signature) != 32:
            raise ValueError("remote MuSig2 partial signature must be exactly 32 bytes")
        self._ensure_escrow()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.FinalizeParent(
            escrow.FinalizeParentRequest(
                channel_point=self._rpc_escrow_outpoint(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
                attempt_id=attempt_id,
                remote_partial_signature=remote_partial_signature,
            )
        )
        return self._parse_finalized_escrow_parent(
            bytes(response.raw_tx), self._rpc_escrow_txid(bytes(response.txid)), funding_outpoint
        )

    @staticmethod
    def _parse_finalized_escrow_parent(
        raw_tx: bytes, txid: str, funding_outpoint: EscrowOutpoint
    ) -> LndFinalizedEscrowParent:
        try:
            tx_hex = raw_tx.hex()
            if get_txid(tx_hex) != txid:
                raise ValueError("finalized parent transaction ID differs from its bytes")
            parsed = parse_transaction(tx_hex)
            matching_indices = [
                index
                for index, tx_input in enumerate(parsed.inputs)
                if tx_input.txid == funding_outpoint.txid and tx_input.vout == funding_outpoint.vout
            ]
            if len(matching_indices) != 1 or len(parsed.witnesses) != len(parsed.inputs):
                raise ValueError("finalized parent lacks one exact channel input")
            channel_input_index = matching_indices[0]
            witness = tuple(parsed.witnesses[channel_input_index])
            if any(
                parsed.witnesses[index]
                for index in range(len(parsed.inputs))
                if index != channel_input_index
            ):
                raise ValueError("finalized parent includes unexpected non-channel witnesses")
            return LndFinalizedEscrowParent(
                tx_hex=tx_hex,
                txid=txid,
                channel_input_index=channel_input_index,
                channel_input_witness=witness,
            )
        except (IndexError, TypeError, ValueError) as exc:
            raise LndValidationError(
                "ChannelEscrowRPC returned invalid final-Taproot channel witness data"
            ) from exc

    async def channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus:
        """Return and validate durable state for a final-Taproot channel-input session."""
        self._ensure_escrow()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.Status(
            escrow.StatusRequest(
                channel_point=self._rpc_escrow_outpoint(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
            )
        )
        try:
            stage = LndEscrowStage(int(response.stage))
            active_attempt_id = bytes(response.active_attempt_id)
            active_nonce = bytes(response.active_local_public_nonce)
            if bool(active_attempt_id) != bool(active_nonce):
                raise ValueError("active attempt ID and nonce differ")
            parent_txid = (
                self._rpc_escrow_txid(bytes(response.parent_txid)) if response.parent_txid else None
            )
            finalized_parent = None
            if response.finalized_parent_tx:
                if parent_txid is None:
                    raise ValueError("finalized parent lacks a parent transaction ID")
                finalized_parent = self._parse_finalized_escrow_parent(
                    bytes(response.finalized_parent_tx), parent_txid, funding_outpoint
                )
            return LndEscrowStatus(
                stage=stage,
                parent_txid=parent_txid,
                finalized=bool(response.finalized),
                durable_local_public_nonce=(
                    bytes(response.durable_local_public_nonce)
                    if response.durable_local_public_nonce
                    else None
                ),
                durable_remote_public_nonce=(
                    bytes(response.durable_remote_public_nonce)
                    if response.durable_remote_public_nonce
                    else None
                ),
                durable_local_partial_signature=(
                    bytes(response.durable_local_partial_signature)
                    if response.durable_local_partial_signature
                    else None
                ),
                finalized_parent=finalized_parent,
                active_attempt=(
                    LndEscrowSigningAttempt(attempt_id=active_attempt_id, public_nonce=active_nonce)
                    if active_attempt_id
                    else None
                ),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(
                "ChannelEscrowRPC returned inconsistent durable status"
            ) from exc

    async def find_channel_input_escrow_status(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> LndEscrowStatus | None:
        """Return exact durable state, or None when this session owns no lock."""

        try:
            return await self.channel_input_escrow_status(session_id, funding_outpoint)
        except Exception as exc:
            if self._is_escrow_session_mismatch(exc):
                return None
            raise

    async def cancel_channel_input_escrow(
        self, session_id: str, funding_outpoint: EscrowOutpoint
    ) -> bool:
        """Cancel a pre-sign escrow session and report whether its channel link resumed."""
        self._ensure_escrow()
        from jmswap.lndrpc import channelescrow_pb2 as escrow

        response = await self._escrow_stub.Cancel(
            escrow.CancelRequest(
                channel_point=self._rpc_escrow_outpoint(funding_outpoint),
                session_id=self._escrow_session_id(session_id),
            )
        )
        return bool(response.link_resumed)

    async def node_info(self, *, timeout_seconds: float = 10.0) -> LndNodeInfo:
        self._ensure()
        try:
            response = await asyncio.wait_for(
                self._stub.GetInfo(ln.GetInfoRequest()), timeout_seconds
            )
        except TimeoutError as exc:
            raise LndTimeoutError("GetInfo timed out") from exc
        chains = [str(chain.network) for chain in response.chains]
        feature_bits = frozenset(int(bit) for bit in response.features)
        final_features = []
        for bit in FINAL_TAPROOT_FEATURE_BITS:
            feature = response.features.get(bit)
            if feature is None:
                continue
            if (
                not bool(feature.is_known)
                or str(feature.name) != "simple-taproot-chans"
                or bool(feature.is_required) != (bit % 2 == 0)
            ):
                raise LndCapabilityError("LND final Taproot feature metadata is inconsistent")
            final_features.append(bit)
        if len(final_features) > 1:
            raise LndCapabilityError("LND advertises conflicting final Taproot feature bits")
        info = LndNodeInfo(
            identity_pubkey=str(response.identity_pubkey),
            version=str(response.version),
            network=chains[0] if len(chains) == 1 else "",
            synced_to_chain=bool(response.synced_to_chain),
            wallet_synced=bool(response.wallet_synced),
            feature_bits=feature_bits,
            advertised_uris=tuple(str(uri) for uri in response.uris),
        )
        version_match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)", info.version)
        version = tuple(int(part) for part in version_match.groups()) if version_match else ()
        if version < MIN_FINAL_TAPROOT_LND_VERSION:
            raise LndCapabilityError(f"LND {info.version!r} lacks production Taproot channels")
        if info.network != self.network:
            raise LndCapabilityError(f"LND network {info.network!r} != required {self.network!r}")
        if not info.synced_to_chain or not info.wallet_synced:
            raise LndCapabilityError("LND chain wallet is not fully synced")
        if not final_features:
            raise LndCapabilityError("LND does not advertise final Taproot feature bit 80/81")
        return info

    async def check_production_cofunded_channel_ring_v1(
        self,
        onion_endpoint: str,
        *,
        timeout_seconds: float = 10.0,
    ) -> LndNodeInfo:
        """Validate the complete production backend and bind its onion URI to GetInfo."""
        info = await self.node_info(timeout_seconds=timeout_seconds)
        if not COFUNDED_CHANNEL_RING_V1_SAFE_RETIREMENT_LIVE_VALIDATED:
            raise LndCapabilityError("safe verified-channel retirement is not live validated")
        if not COFUNDED_CHANNEL_RING_V1_CAPABLE:
            raise LndCapabilityError("co-funded channel ring lifecycle is incomplete")
        expected_uri = f"{info.identity_pubkey}@{onion_endpoint}"
        if expected_uri not in info.advertised_uris:
            raise LndCapabilityError(
                "configured onion endpoint is not advertised by the GetInfo node identity"
            )
        return info

    async def connect_peer(
        self, node_id: NodeId, host: str, *, timeout_seconds: float = 10.0
    ) -> None:
        self._ensure()
        request = ln.ConnectPeerRequest(
            addr=ln.LightningAddress(pubkey=node_id, host=host),
            perm=False,
            timeout=max(1, int(timeout_seconds)),
        )
        try:
            await asyncio.wait_for(self._stub.ConnectPeer(request), timeout_seconds)
        except TimeoutError as exc:
            raise LndTimeoutError("ConnectPeer timed out") from exc
        except Exception as exc:
            if "already connected" not in str(exc).lower():
                raise

    async def list_maintenance_channels(self) -> tuple[MaintenanceChannel, ...]:
        """Return channel observations with both policies, omitting incomplete graph edges."""
        self._ensure()
        try:
            channels = await asyncio.wait_for(
                self._stub.ListChannels(ln.ListChannelsRequest()), _MAINTENANCE_RPC_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise LndTimeoutError("ListChannels timed out for maintenance") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"ListChannels failed for maintenance: {exc}") from exc
        try:
            graph = await asyncio.wait_for(
                self._stub.DescribeGraph(ln.ChannelGraphRequest(include_unannounced=True)),
                _MAINTENANCE_RPC_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise LndTimeoutError("DescribeGraph timed out for maintenance") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"DescribeGraph failed for maintenance: {exc}") from exc

        observations: list[MaintenanceChannel] = []
        try:
            for channel in channels.channels:
                policy_pair = self._maintenance_channel_policy_pair(
                    graph.edges, str(channel.channel_point), str(channel.remote_pubkey)
                )
                if policy_pair is None:
                    continue
                local_policy, remote_policy = policy_pair
                local_alias = int(channel.alias_scids[0]) if channel.alias_scids else None
                peer_alias = int(channel.peer_scid_alias) if channel.peer_scid_alias else None
                observations.append(
                    MaintenanceChannel(
                        channel_point=str(channel.channel_point),
                        channel_id=int(channel.chan_id),
                        peer_node_id=str(channel.remote_pubkey),
                        active=bool(channel.active),
                        private=bool(channel.private),
                        local_balance_sat=int(channel.local_balance),
                        remote_balance_sat=int(channel.remote_balance),
                        local_reserve_sat=int(channel.local_constraints.chan_reserve_sat),
                        remote_reserve_sat=int(channel.remote_constraints.chan_reserve_sat),
                        pending_htlc_count=len(channel.pending_htlcs),
                        local_alias=local_alias,
                        peer_alias=peer_alias,
                        local_base_fee_msat=int(local_policy.fee_base_msat),
                        local_fee_ppm=int(local_policy.fee_rate_milli_msat),
                        remote_base_fee_msat=int(remote_policy.fee_base_msat),
                        remote_fee_ppm=int(remote_policy.fee_rate_milli_msat),
                        local_cltv_delta=int(local_policy.time_lock_delta),
                        remote_cltv_delta=int(remote_policy.time_lock_delta),
                    )
                )
            return tuple(observations)
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(
                f"LND returned invalid maintenance channel data: {exc}"
            ) from exc

    async def send_peer_message(self, peer_node_id: str, message_type: int, payload: bytes) -> None:
        """Send one bounded unknown-odd custom message to an authenticated peer."""
        peer = self._validate_channel_input_peer_filter(peer_node_id)
        self._validate_maintenance_uint("message_type", message_type, 0xFFFFFFFF, minimum=32768)
        if message_type % 2 != 1:
            raise ValueError("message_type must be an odd custom message type")
        if not isinstance(payload, bytes) or len(payload) > _MAX_CUSTOM_MESSAGE_PAYLOAD_BYTES:
            raise ValueError(
                f"payload must be bytes no longer than {_MAX_CUSTOM_MESSAGE_PAYLOAD_BYTES} bytes"
            )
        self._ensure()
        request = ln.SendCustomMessageRequest(
            peer=bytes.fromhex(peer), type=message_type, data=payload
        )
        try:
            await asyncio.wait_for(
                self._stub.SendCustomMessage(request), _MAINTENANCE_RPC_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise LndTimeoutError("SendCustomMessage timed out") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"SendCustomMessage failed: {exc}") from exc

    async def subscribe_peer_messages(self, message_type: int) -> AsyncIterator[tuple[str, bytes]]:
        """Yield bounded messages of one custom type with LND-attested peer identities."""
        self._validate_maintenance_uint("message_type", message_type, 0xFFFFFFFF, minimum=32768)
        if message_type % 2 != 1:
            raise ValueError("message_type must be an odd custom message type")
        self._ensure()
        stream: Any = None
        try:
            stream = self._stub.SubscribeCustomMessages(ln.SubscribeCustomMessagesRequest())
            async for message in stream:
                if int(message.type) != message_type:
                    continue
                payload = bytes(message.data)
                if len(payload) > _MAX_CUSTOM_MESSAGE_PAYLOAD_BYTES:
                    continue
                peer = bytes(message.peer)
                if len(peer) != 33 or peer[0] not in (0x02, 0x03):
                    continue
                yield peer.hex(), payload
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"SubscribeCustomMessages failed: {exc}") from exc
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.cancel()

    async def create_maintenance_invoice(
        self, amount_sat: int, hops: Sequence[PeerRouteHop], expiry_seconds: int
    ) -> MaintenanceInvoice:
        """Create a hold invoice with one exact private route hint supplied by the caller."""
        self._validate_maintenance_uint("amount_sat", amount_sat, (1 << 63) - 1, minimum=1)
        self._validate_maintenance_uint("expiry_seconds", expiry_seconds, (1 << 63) - 1, minimum=1)
        if isinstance(hops, (str, bytes)) or not isinstance(hops, Sequence) or not hops:
            raise ValueError("hops must be a non-empty sequence of PeerRouteHop values")

        hint_hops: list[Any] = []
        for hop in hops:
            if not isinstance(hop, PeerRouteHop):
                raise ValueError("hops must contain only PeerRouteHop values")
            node_id = self._validate_channel_input_peer_filter(hop.node_id)
            self._validate_maintenance_uint(
                "hop.channel_id", hop.channel_id, (1 << 64) - 1, minimum=1
            )
            self._validate_maintenance_uint("hop.base_fee_msat", hop.base_fee_msat, 0xFFFFFFFF)
            self._validate_maintenance_uint("hop.fee_ppm", hop.fee_ppm, 0xFFFFFFFF)
            self._validate_maintenance_uint("hop.cltv_delta", hop.cltv_delta, 0xFFFFFFFF, minimum=1)
            hint_hops.append(
                ln.HopHint(
                    node_id=node_id,
                    chan_id=hop.channel_id,
                    fee_base_msat=hop.base_fee_msat,
                    fee_proportional_millionths=hop.fee_ppm,
                    cltv_expiry_delta=hop.cltv_delta,
                )
            )

        self._ensure_invoices()
        from jmswap.lndrpc import invoices_pb2 as invoices

        created_at = int(time.time())
        preimage = secrets.token_bytes(32)
        payment_hash = hashlib.sha256(preimage).digest()
        request = invoices.AddHoldInvoiceRequest(
            hash=payment_hash,
            value=amount_sat,
            expiry=expiry_seconds,
            private=False,
            route_hints=[ln.RouteHint(hop_hints=hint_hops)],
        )
        try:
            response = await asyncio.wait_for(
                self._invoices_stub.AddHoldInvoice(request), _MAINTENANCE_RPC_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise LndTimeoutError("AddHoldInvoice timed out") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"AddHoldInvoice failed: {exc}") from exc
        try:
            payment_request = str(response.payment_request)
            if not payment_request:
                raise ValueError("missing payment request")
            return MaintenanceInvoice(
                payment_hash=payment_hash.hex(),
                payment_request=payment_request,
                expires_at=created_at + expiry_seconds,
                preimage=preimage.hex(),
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(
                f"AddHoldInvoice returned invalid maintenance invoice: {exc}"
            ) from exc

    @classmethod
    def _validate_maintenance_invoice_settlement(
        cls,
        invoice: Any,
        payment_hash: bytes,
        return_channel_id: int,
        amount_sat: int,
        expected_htlc_state: int,
    ) -> None:
        """Bind a terminal or accepted hold invoice to one expected incoming HTLC."""
        expected_amount_msat = amount_sat * 1000
        try:
            observed_hash = bytes(invoice.r_hash)
            if len(observed_hash) != 32 or observed_hash != payment_hash:
                raise LndValidationError(
                    "maintenance invoice payment hash differs from the request"
                )
            for name, observed, expected in (
                ("maintenance invoice value", invoice.value, amount_sat),
                ("maintenance invoice value_msat", invoice.value_msat, expected_amount_msat),
                (
                    "maintenance invoice paid amount",
                    invoice.amt_paid_msat,
                    expected_amount_msat,
                ),
            ):
                cls._validate_maintenance_uint(name, observed, _INT64_MAX, minimum=1)
                if observed != expected:
                    raise LndValidationError(f"{name} differs from the expected maintenance amount")

            non_canceled_htlcs: list[tuple[Any, int]] = []
            for htlc in invoice.htlcs:
                htlc_state = int(htlc.state)
                if htlc_state != ln.CANCELED:
                    non_canceled_htlcs.append((htlc, htlc_state))
            if len(non_canceled_htlcs) != 1:
                raise LndValidationError(
                    "maintenance invoice must contain exactly one non-canceled HTLC"
                )
            htlc, htlc_state = non_canceled_htlcs[0]
            if htlc_state != expected_htlc_state:
                raise LndValidationError("maintenance invoice HTLC has an unexpected state")
            cls._validate_maintenance_uint(
                "maintenance invoice HTLC channel ID", htlc.chan_id, _UINT64_MAX, minimum=1
            )
            cls._validate_maintenance_uint(
                "maintenance invoice HTLC amount", htlc.amt_msat, _INT64_MAX, minimum=1
            )
            if htlc.chan_id != return_channel_id:
                raise LndValidationError("maintenance invoice HTLC arrived on the wrong channel")
            if htlc.amt_msat != expected_amount_msat:
                raise LndValidationError("maintenance invoice HTLC amount differs from the request")
        except LndValidationError:
            raise
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(f"LND returned an invalid maintenance invoice: {exc}") from exc

    async def reconcile_maintenance_invoice(
        self, payment_hash: str, preimage: str, return_channel_id: int, amount_sat: int
    ) -> None:
        """Settle an accepted hold invoice only after binding its incoming HTLC."""
        if not isinstance(payment_hash, str) or re.fullmatch(r"[0-9a-f]{64}", payment_hash) is None:
            raise ValueError("payment_hash must be a lowercase 32-byte hexadecimal value")
        if not isinstance(preimage, str) or re.fullmatch(r"[0-9a-f]{64}", preimage) is None:
            raise ValueError("preimage must be a lowercase 32-byte hexadecimal value")
        self._validate_maintenance_uint(
            "return_channel_id", return_channel_id, _UINT64_MAX, minimum=1
        )
        self._validate_maintenance_uint("amount_sat", amount_sat, _INT64_MAX // 1000, minimum=1)
        payment_hash_bytes = bytes.fromhex(payment_hash)
        preimage_bytes = bytes.fromhex(preimage)
        if hashlib.sha256(preimage_bytes).digest() != payment_hash_bytes:
            raise ValueError("preimage does not match payment_hash")

        self._ensure_invoices()
        from jmswap.lndrpc import invoices_pb2 as invoices

        try:
            invoice = await asyncio.wait_for(
                self._stub.LookupInvoice(ln.PaymentHash(r_hash=payment_hash_bytes)),
                _MAINTENANCE_RPC_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise LndTimeoutError("LookupInvoice timed out for maintenance") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"LookupInvoice failed for maintenance: {exc}") from exc

        try:
            state = int(invoice.state)
        except (AttributeError, TypeError, ValueError) as exc:
            raise LndValidationError(f"LND returned an invalid maintenance invoice: {exc}") from exc
        if state in (ln.Invoice.OPEN, ln.Invoice.CANCELED):
            return
        if state == ln.Invoice.ACCEPTED:
            try:
                self._validate_maintenance_invoice_settlement(
                    invoice,
                    payment_hash_bytes,
                    return_channel_id,
                    amount_sat,
                    ln.ACCEPTED,
                )
            except LndValidationError:
                try:
                    await asyncio.wait_for(
                        self._invoices_stub.CancelInvoice(
                            invoices.CancelInvoiceMsg(payment_hash=payment_hash_bytes)
                        ),
                        _MAINTENANCE_RPC_TIMEOUT_SECONDS,
                    )
                except TimeoutError as exc:
                    raise LndTimeoutError("CancelInvoice timed out for maintenance") from exc
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise LndRpcError(f"CancelInvoice failed for maintenance: {exc}") from exc
                return
            try:
                await asyncio.wait_for(
                    self._invoices_stub.SettleInvoice(
                        invoices.SettleInvoiceMsg(preimage=preimage_bytes)
                    ),
                    _MAINTENANCE_RPC_TIMEOUT_SECONDS,
                )
            except TimeoutError as exc:
                raise LndTimeoutError("SettleInvoice timed out for maintenance") from exc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise LndRpcError(f"SettleInvoice failed for maintenance: {exc}") from exc
            return
        if state == ln.Invoice.SETTLED:
            self._validate_maintenance_invoice_settlement(
                invoice,
                payment_hash_bytes,
                return_channel_id,
                amount_sat,
                ln.SETTLED,
            )
            return
        raise LndValidationError(f"maintenance invoice has unknown state {state}")

    async def send_maintenance_payment(
        self, payment_request: str, outgoing_channel_id: int, timeout_seconds: int
    ) -> MaintenancePayment:
        """Attempt one validated zero-fee self-payment over one exact route."""
        if not isinstance(payment_request, str) or not payment_request:
            raise ValueError("payment_request must be a non-empty string")
        self._validate_maintenance_uint(
            "outgoing_channel_id", outgoing_channel_id, _UINT64_MAX, minimum=1
        )
        self._validate_maintenance_timeout(timeout_seconds)
        self._ensure()
        try:
            invoice = await asyncio.wait_for(
                self._stub.DecodePayReq(ln.PayReqString(pay_req=payment_request)),
                _MAINTENANCE_RPC_TIMEOUT_SECONDS,
            )
            info = await asyncio.wait_for(
                self._stub.GetInfo(ln.GetInfoRequest()), _MAINTENANCE_RPC_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise LndTimeoutError("maintenance invoice validation timed out") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"maintenance invoice validation failed: {exc}") from exc

        local_identity = self._maintenance_node_id(
            "LND identity", getattr(info, "identity_pubkey", None)
        )
        route = self._maintenance_exact_route_from_invoice(
            invoice, local_identity, getattr(info, "block_height", None)
        )
        try:
            channels = await asyncio.wait_for(
                self._stub.ListChannels(ln.ListChannelsRequest()), _MAINTENANCE_RPC_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise LndTimeoutError("ListChannels timed out for maintenance payment") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"ListChannels failed for maintenance payment: {exc}") from exc
        self._validate_maintenance_route_channels(channels, route, outgoing_channel_id)

        self._ensure_router()
        from jmswap.lndrpc import router_pb2 as router

        exact_route = ln.Route(
            total_time_lock=route.total_time_lock,
            total_fees_msat=0,
            total_amt_msat=route.amount_msat,
            first_hop_amount_msat=route.amount_msat,
            hops=[
                ln.Hop(
                    chan_id=outgoing_channel_id,
                    pub_key=route.helper_peer_node_id,
                    amt_to_forward_msat=route.amount_msat,
                    fee_msat=0,
                    expiry=route.helper_expiry,
                ),
                ln.Hop(
                    chan_id=route.helper_channel_alias,
                    pub_key=route.return_peer_node_id,
                    amt_to_forward_msat=route.amount_msat,
                    fee_msat=0,
                    expiry=route.final_expiry,
                ),
                ln.Hop(
                    chan_id=route.return_channel_alias,
                    pub_key=local_identity,
                    amt_to_forward_msat=route.amount_msat,
                    fee_msat=0,
                    expiry=route.final_expiry,
                    mpp_record=ln.MPPRecord(
                        payment_addr=route.payment_addr, total_amt_msat=route.amount_msat
                    ),
                ),
            ],
        )
        try:
            attempt = await asyncio.wait_for(
                self._router_stub.SendToRouteV2(
                    router.SendToRouteRequest(
                        payment_hash=route.payment_hash, route=exact_route, skip_temp_err=False
                    )
                ),
                timeout_seconds,
            )
            return self._maintenance_payment_from_attempt(attempt, route.payment_hash)
        except TimeoutError:
            return MaintenancePayment(status="in_flight", fee_msat=None)
        except asyncio.CancelledError:
            raise
        except LndError:
            raise
        except Exception as exc:
            raise LndRpcError(f"RouterRPC SendToRouteV2 failed: {exc}") from exc

    async def track_maintenance_payment(
        self, payment_hash: str, timeout_seconds: int = 5
    ) -> MaintenancePayment:
        """Observe one existing payment without sending another attempt."""
        if not isinstance(payment_hash, str) or re.fullmatch(r"[0-9a-f]{64}", payment_hash) is None:
            raise ValueError("payment_hash must be a lowercase 32-byte hexadecimal value")
        self._validate_maintenance_timeout(timeout_seconds)
        self._ensure_router()
        from jmswap.lndrpc import router_pb2 as router

        stream: Any = None
        try:
            stream = self._router_stub.TrackPaymentV2(
                router.TrackPaymentRequest(
                    payment_hash=bytes.fromhex(payment_hash), no_inflight_updates=False
                )
            )
            update = await asyncio.wait_for(stream.read(), timeout_seconds)
            return self._maintenance_payment_from_update(update)
        except TimeoutError:
            return MaintenancePayment(status="in_flight", fee_msat=None)
        except asyncio.CancelledError:
            raise
        except LndError:
            raise
        except Exception as exc:
            if self._is_not_found(exc):
                return MaintenancePayment(status="unknown", fee_msat=None)
            raise LndRpcError(f"RouterRPC TrackPaymentV2 failed: {exc}") from exc
        finally:
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.cancel()

    async def set_private_channel_zero_fee(self, channel_point: str) -> None:
        """Set a private channel's local forwarding fee to zero without changing its CLTV."""
        point = self._parse_escrow_outpoint(channel_point)
        self._ensure()
        try:
            channels = await asyncio.wait_for(
                self._stub.ListChannels(ln.ListChannelsRequest()), _MAINTENANCE_RPC_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise LndTimeoutError("ListChannels timed out for channel policy update") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"ListChannels failed for channel policy update: {exc}") from exc
        channel = next(
            (item for item in channels.channels if str(item.channel_point) == channel_point), None
        )
        if channel is None:
            raise LndValidationError(f"private maintenance channel not found: {channel_point}")
        if not bool(channel.private):
            raise LndValidationError("refusing to change forwarding policy on a public channel")

        try:
            graph = await asyncio.wait_for(
                self._stub.DescribeGraph(ln.ChannelGraphRequest(include_unannounced=True)),
                _MAINTENANCE_RPC_TIMEOUT_SECONDS,
            )
        except TimeoutError as exc:
            raise LndTimeoutError("DescribeGraph timed out for channel policy update") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"DescribeGraph failed for channel policy update: {exc}") from exc
        policy_pair = self._maintenance_channel_policy_pair(
            graph.edges, channel_point, str(channel.remote_pubkey)
        )
        if policy_pair is None:
            raise LndValidationError("private maintenance channel lacks a local routing policy")
        local_policy = policy_pair[0]

        request = ln.PolicyUpdateRequest(
            chan_point=ln.ChannelPoint(funding_txid_str=point.txid, output_index=point.vout),
            base_fee_msat=0,
            fee_rate_ppm=0,
            time_lock_delta=int(local_policy.time_lock_delta),
        )
        try:
            response = await asyncio.wait_for(
                self._stub.UpdateChannelPolicy(request), _MAINTENANCE_RPC_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise LndTimeoutError("UpdateChannelPolicy timed out") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise LndRpcError(f"UpdateChannelPolicy failed: {exc}") from exc
        if response.failed_updates:
            raise LndRpcError(
                f"UpdateChannelPolicy failed: {response.failed_updates[0].update_error}"
            )

    async def start_external_channel(self, request: ExternalChannelRequest) -> FundingNegotiation:
        """Negotiate one externally funded channel, reporting failures as backend errors.

        A rejected open is normal in a ring: the peer's acceptor refuses anything
        outside the negotiated contract, and LND relays that reason back here as a
        transport error. Callers get it as an ``LndError`` carrying the reason rather
        than a bare gRPC exception they would have to recognise themselves.
        """
        try:
            return await self._negotiate_external_channel(request)
        except LndError:
            raise
        except Exception as exc:
            raise LndRpcError(f"OpenChannel failed: {exc}") from exc

    async def _negotiate_external_channel(
        self, request: ExternalChannelRequest
    ) -> FundingNegotiation:
        self._ensure()
        pending_id = bytes(request.pending_channel_id)
        if pending_id in self._seen_pending_ids:
            raise LndValidationError("pending channel ID was already used by this backend")
        self._seen_pending_ids.add(pending_id)
        deadline = time.monotonic() + request.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            try:
                await self.connect_peer(
                    request.peer_node_id,
                    request.peer_host,
                    timeout_seconds=max(0.1, min(_ONION_CONNECT_ATTEMPT_SECONDS, remaining)),
                )
                break
            except Exception:
                if ".onion" not in request.peer_host or deadline <= time.monotonic():
                    raise
                await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        open_request = ln.OpenChannelRequest(
            node_pubkey=bytes.fromhex(request.peer_node_id),
            local_funding_amount=request.capacity_sat,
            push_sat=request.push_sat,
            private=True,
            commitment_type=ln.TAPROOT,
            zero_conf=False,
            scid_alias=request.scid_alias,
            remote_csv_delay=request.fundee_csv_delay,
            max_local_csv=request.opener_csv_delay,
            remote_chan_reserve_sat=request.fundee_reserve_sat,
            funding_shim=ln.FundingShim(
                psbt_shim=ln.PsbtShim(pending_chan_id=pending_id, no_publish=True)
            ),
        )
        stream: Any = None
        update: Any = None
        last_error: Exception | None = None
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                stream = self._stub.OpenChannel(open_request)
                update = await asyncio.wait_for(stream.read(), remaining)
                last_error = None
                break
            except TimeoutError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                if "not online" not in str(exc).lower() or attempt == 2:
                    raise
                await asyncio.sleep(
                    min(0.25 * (attempt + 1), max(0.0, deadline - time.monotonic()))
                )
                await self.connect_peer(
                    request.peer_node_id,
                    request.peer_host,
                    timeout_seconds=max(0.1, deadline - time.monotonic()),
                )
        if update is None:
            with contextlib.suppress(Exception):
                await self._cancel_pending_id(pending_id, timeout_seconds=2.0)
            raise LndTimeoutError(
                "OpenChannel did not negotiate funding before timeout"
            ) from last_error
        if update.WhichOneof("update") != "psbt_fund":
            raise LndValidationError(
                f"expected psbt_fund update, got {update.WhichOneof('update')}"
            )
        if update.pending_chan_id and bytes(update.pending_chan_id) != pending_id:
            raise LndValidationError("LND substituted the caller-supplied pending channel ID")
        funding = update.psbt_fund
        if int(funding.funding_amount) != request.capacity_sat:
            raise LndValidationError("LND negotiated a different channel capacity")
        funding_script = address_to_scriptpubkey(str(funding.funding_address))
        if len(funding_script) != 34 or funding_script[:2] != b"\x51\x20":
            raise LndValidationError("LND negotiated funding output is not P2TR")
        negotiation = FundingNegotiation(
            pending_channel_id=pending_id,
            peer_node_id=request.peer_node_id,
            funding_address=str(funding.funding_address),
            funding_script_pubkey=funding_script,
            capacity_sat=request.capacity_sat,
            push_sat=request.push_sat,
            opener_reserve_sat=request.opener_reserve_sat,
            fundee_reserve_sat=request.fundee_reserve_sat,
            opener_csv_delay=request.opener_csv_delay,
            fundee_csv_delay=request.fundee_csv_delay,
            min_depth=request.min_depth,
        )
        self._pending[pending_id] = _PendingOpen(negotiation=negotiation, stream=stream)
        return negotiation

    async def verify_external_funding(
        self,
        negotiation: FundingNegotiation,
        raw_transaction_hex: str,
        funding_txid: Hex32,
        funding_vout: int,
        input_utxos: list[WitnessUtxo],
        *,
        timeout_seconds: float = 30.0,
    ) -> VerifiedFunding:
        pending = self._pending.get(bytes(negotiation.pending_channel_id))
        if pending is None or pending.negotiation != negotiation:
            raise LndValidationError("unknown or substituted funding negotiation")
        psbt, actual_txid = build_unsigned_psbt(raw_transaction_hex, input_utxos)
        if actual_txid != funding_txid:
            raise LndValidationError(
                f"funding TXID {funding_txid} != unsigned transaction TXID {actual_txid}"
            )
        tx = parse_transaction(raw_transaction_hex)
        if funding_vout < 0 or funding_vout >= len(tx.outputs):
            raise LndValidationError("funding outpoint index is outside the transaction")
        output = tx.outputs[funding_vout]
        if (
            output.script != negotiation.funding_script_pubkey
            or output.value != negotiation.capacity_sat
        ):
            raise LndValidationError("funding outpoint does not match LND's negotiated output")
        if pending.verified is not None:
            if (
                pending.verified.funding_txid != funding_txid
                or pending.verified.funding_vout != funding_vout
            ):
                raise LndValidationError(
                    "pending channel was already verified with another outpoint"
                )
            return pending.verified
        self._ensure()
        transition = ln.FundingTransitionMsg(
            psbt_verify=ln.FundingPsbtVerify(
                funded_psbt=psbt,
                pending_chan_id=negotiation.pending_channel_id,
                skip_finalize=True,
            )
        )
        try:
            await asyncio.wait_for(self._stub.FundingStateStep(transition), timeout_seconds)
        except TimeoutError as exc:
            raise LndTimeoutError("FundingStateStep PsbtVerify timed out") from exc
        except LndError:
            raise
        except Exception as exc:
            raise LndRpcError(f"FundingStateStep PsbtVerify failed: {exc}") from exc
        verified = VerifiedFunding(
            pending_channel_id=negotiation.pending_channel_id,
            funding_txid=funding_txid,
            funding_vout=funding_vout,
            unsigned_psbt=psbt,
        )
        pending.verified = verified
        return verified

    def resume_external_channel(
        self,
        negotiation: FundingNegotiation,
        *,
        verified: VerifiedFunding | None = None,
        observed_channel_point: str | None = None,
    ) -> None:
        """Restore an exact retained PSBT shim after the participant process restarts.

        ``verified`` and ``observed_channel_point`` are durable evidence that this
        participant already completed PsbtVerify and observed the pending channel at
        that exact point before persisting readiness. Restoring them keeps a resumed
        endpoint eligible for guarded retirement; omitting them marks the shim state
        unknown, so cancellation can no longer be reported as success.
        """
        pending_id = bytes(negotiation.pending_channel_id)
        if verified is not None and verified.pending_channel_id != negotiation.pending_channel_id:
            raise LndValidationError("retained verified funding belongs to another shim")
        if observed_channel_point is not None and (
            verified is None or observed_channel_point != verified.channel_point
        ):
            raise LndValidationError("retained observation does not match the verified point")
        existing = self._pending.get(pending_id)
        if existing is not None:
            if existing.negotiation != negotiation:
                raise LndValidationError("retained outgoing negotiation conflicts with memory")
            if verified is not None and existing.verified not in {None, verified}:
                raise LndValidationError("retained verified funding conflicts with memory")
            if verified is not None and existing.verified is None:
                existing.verified = verified
                existing.shim_state_known = True
        else:
            self._seen_pending_ids.add(pending_id)
            self._pending[pending_id] = _PendingOpen(
                negotiation=negotiation,
                stream=None,
                verified=verified,
                shim_state_known=verified is not None,
            )
        if observed_channel_point is not None:
            self._restore_observed_verified_point(pending_id, observed_channel_point)

    def resume_inbound_channel(
        self,
        expected: InboundChannelExpectation,
        *,
        observed_channel_point: str | None = None,
    ) -> None:
        """Restore the exact accepted inbound contract used to validate retained state.

        ``observed_channel_point`` is durable evidence that this participant already
        observed the pending channel at that exact point, which keeps a resumed fundee
        endpoint eligible for guarded retirement.
        """
        pending_id = bytes(expected.pending_channel_id)
        existing = self._accepted.get(pending_id)
        if existing is not None and existing != expected:
            raise LndValidationError("retained inbound expectation conflicts with memory")
        self._accepted[pending_id] = expected
        if observed_channel_point is not None:
            self._restore_observed_verified_point(pending_id, observed_channel_point)

    def _restore_observed_verified_point(self, pending_id: bytes, channel_point: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}:[0-9]+", channel_point) is None:
            raise LndValidationError("invalid retained channel point")
        existing = self._observed_verified_points.get(pending_id)
        if existing is not None and existing != channel_point:
            raise LndValidationError("retained channel point conflicts with memory")
        self._observed_verified_points[pending_id] = channel_point

    async def _cancel_pending_id(self, pending_id: bytes, *, timeout_seconds: float) -> None:
        self._ensure()
        transition = ln.FundingTransitionMsg(
            shim_cancel=ln.FundingShimCancel(pending_chan_id=pending_id)
        )
        await asyncio.wait_for(self._stub.FundingStateStep(transition), timeout_seconds)

    async def cancel_external_channel(
        self,
        pending_channel_id: Bytes32,
        *,
        input_signatures_added: bool,
        timeout_seconds: float = 10.0,
    ) -> None:
        if input_signatures_added:
            raise LndValidationError("refusing to cancel after funding input signatures were added")
        pending_id = bytes(pending_channel_id)
        pending = self._pending.get(pending_id)
        if pending is not None and pending.verified is not None:
            raise LndValidationError(
                "cannot cancel after PsbtVerify consumed the LND funding shim; "
                "cancel before verification"
            )
        try:
            await self._cancel_pending_id(pending_id, timeout_seconds=timeout_seconds)
        except TimeoutError as exc:
            raise LndTimeoutError("FundingStateStep shim_cancel timed out") from exc
        except Exception as exc:
            markers = (
                "not found",
                "no funding intent found",
                "unknown pending",
                "already canceled",
                "already cancelled",
            )
            if not any(marker in str(exc).lower() for marker in markers):
                if isinstance(exc, LndError):
                    raise
                raise LndRpcError(f"FundingStateStep shim_cancel failed: {exc}") from exc
            # LND reports the same absence whether the shim never existed or a
            # previous PsbtVerify consumed it. That is only safe to treat as a
            # successful cancellation when this process created the shim and knows
            # verification never ran; a shim restored after a restart is ambiguous,
            # so the caller must reconcile instead of recording a clean retirement.
            shim_state_known = (
                pending.shim_state_known
                if pending is not None
                else pending_id in self._seen_pending_ids
            )
            if not shim_state_known:
                raise LndAmbiguousShimError(
                    "unknown or resumed funding shim is absent from LND, which cannot distinguish "
                    "a canceled shim from one consumed by PsbtVerify; reconcile instead"
                ) from exc
        self._pending.pop(pending_id, None)

    async def run_channel_acceptor(
        self,
        expected: InboundChannelExpectation,
        bounds: AcceptorBounds,
        *,
        timeout_seconds: float,
        ready: asyncio.Event | None = None,
    ) -> AcceptorObservation:
        self._ensure()
        pending_id = bytes(expected.pending_channel_id)
        registration = self._acceptor_registrations.get(pending_id)
        if registration is None:
            registration = _InboundAcceptorRegistration(
                expected=expected,
                bounds=bounds,
                future=asyncio.get_running_loop().create_future(),
            )
            self._acceptor_registrations[pending_id] = registration
        elif registration.expected != expected or registration.bounds != bounds:
            raise LndValidationError("conflicting inbound channel expectation")

        if self._acceptor_task is None or self._acceptor_task.done():
            started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            task = asyncio.create_task(self._dispatch_channel_acceptor(started))
            self._acceptor_task = task
            self._acceptor_started = started
        task = self._acceptor_task
        startup = self._acceptor_started
        if task is None or startup is None:
            self._discard_registration(pending_id, registration)
            raise LndRpcError("ChannelAcceptor startup state is inconsistent")
        try:
            await asyncio.wait_for(asyncio.shield(startup), timeout_seconds)
        except TimeoutError as exc:
            task.cancel()
            self._discard_registration(pending_id, registration)
            raise LndTimeoutError("ChannelAcceptor stream did not start before timeout") from exc
        except BaseException:
            self._discard_registration(pending_id, registration)
            raise
        if task.done():
            error = task.exception()
            if error is not None:
                self._discard_registration(pending_id, registration)
                raise error
            if not registration.future.done():
                self._discard_registration(pending_id, registration)
                raise LndRpcError("ChannelAcceptor stream closed during startup")
            if registration.future.cancelled():
                self._discard_registration(pending_id, registration)
                raise LndRpcError("ChannelAcceptor stream was canceled during startup")
            registration_error = registration.future.exception()
            if registration_error is not None:
                self._discard_registration(pending_id, registration)
                raise registration_error
        if ready is not None:
            ready.set()

        try:
            return await asyncio.wait_for(registration.future, timeout_seconds)
        except TimeoutError as exc:
            raise LndTimeoutError(
                "expected inbound channel was not accepted before timeout"
            ) from exc
        finally:
            if registration.future.done():
                self._discard_registration(pending_id, registration)

    def _discard_registration(
        self, pending_id: bytes, registration: _InboundAcceptorRegistration
    ) -> None:
        if self._acceptor_registrations.get(pending_id) is registration:
            self._acceptor_registrations.pop(pending_id, None)

    async def _dispatch_channel_acceptor(self, started: asyncio.Future[None] | None = None) -> None:
        try:
            call = self._stub.ChannelAcceptor()
        except BaseException as exc:
            if started is not None and not started.done():
                started.set_exception(exc)
            raise
        self._acceptor_call = call
        try:
            # Creating a grpc.aio call only schedules connection setup. Wait for the
            # RPC to connect before allowing the participant to tell its predecessor
            # to open, otherwise LND may still apply its default acceptance policy.
            await call.wait_for_connection()
            if started is not None and not started.done():
                started.set_result(None)
            async for request in call:
                pending_id = bytes(request.pending_chan_id)
                registration = self._acceptor_registrations.get(pending_id)
                if registration is None:
                    await call.write(
                        ln.ChannelAcceptResponse(
                            accept=False,
                            pending_chan_id=request.pending_chan_id,
                            error="co-funded channel rejected: pending channel ID",
                        )
                    )
                    continue

                expected = registration.expected
                decision = evaluate_channel_accept_request(request, expected, registration.bounds)
                await call.write(
                    ln.ChannelAcceptResponse(
                        accept=decision.accept,
                        pending_chan_id=request.pending_chan_id,
                        error=decision.error,
                        csv_delay=expected.opener_csv_delay if decision.accept else 0,
                        reserve_sat=expected.opener_reserve_sat if decision.accept else 0,
                        min_accept_depth=expected.min_depth if decision.accept else 0,
                        zero_conf=False,
                    )
                )
                if registration.future.done():
                    continue
                if not decision.accept:
                    registration.future.set_exception(LndValidationError(decision.error))
                    continue
                observation = AcceptorObservation(
                    pending_channel_id=pending_id,
                    opener_node_id=request.node_pubkey.hex(),
                    capacity_sat=int(request.funding_amt),
                    push_msat=int(request.push_amt),
                )
                self._accepted[pending_id] = expected
                registration.future.set_result(observation)
        except asyncio.CancelledError:
            if started is not None and not started.done():
                started.cancel()
            for registration in self._acceptor_registrations.values():
                if not registration.future.done():
                    registration.future.cancel()
        except Exception as exc:
            if started is not None and not started.done():
                started.set_exception(exc)
            for registration in self._acceptor_registrations.values():
                if not registration.future.done():
                    registration.future.set_exception(exc)
        finally:
            call.cancel()
            self._acceptor_call = None
            self._acceptor_task = None
            self._acceptor_started = None

    async def pending_channel_observation(
        self,
        expected: PendingChannelExpectation,
        role: EndpointRole,
        *,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.25,
    ) -> PendingChannelObservation:
        self._ensure()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                response = await asyncio.wait_for(
                    self._stub.PendingChannels(ln.PendingChannelsRequest()),
                    min(5.0, max(0.01, deadline - time.monotonic())),
                )
            except TimeoutError:
                response = None
            if response is not None:
                for pending_open in response.pending_open_channels:
                    channel = pending_open.channel
                    if str(channel.channel_point) != expected.channel_point:
                        continue
                    observation = self._parse_pending_observation(
                        channel, int(pending_open.commit_fee), expected, role
                    )
                    validate_contribution_accounting(observation, expected)
                    self._record_observed_verified_point(expected, role)
                    return observation
            if time.monotonic() >= deadline:
                raise LndTimeoutError(
                    f"PendingChannels did not report exact point {expected.channel_point}"
                )
            await asyncio.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def _record_observed_verified_point(
        self, expected: PendingChannelExpectation, role: EndpointRole
    ) -> None:
        pending_id = bytes(expected.pending_channel_id)
        if role is EndpointRole.OPENER:
            pending = self._pending.get(pending_id)
            if pending is None or pending.verified is None:
                return
            if pending.verified.channel_point == expected.channel_point:
                self._observed_verified_points[pending_id] = expected.channel_point
            return

        accepted = self._accepted.get(pending_id)
        if accepted is None:
            return
        if (
            accepted.opener_node_id == expected.opener_node_id
            and accepted.capacity_sat == expected.capacity_sat
            and accepted.push_msat == expected.fundee_contribution_sat * 1000
            and accepted.opener_reserve_sat == expected.opener_reserve_sat
            and accepted.fundee_reserve_sat == expected.fundee_reserve_sat
        ):
            self._observed_verified_points[pending_id] = expected.channel_point

    @staticmethod
    def _pending_channel_points(response: Any) -> set[str]:
        collections = (
            response.pending_open_channels,
            response.pending_closing_channels,
            response.pending_force_closing_channels,
            response.waiting_close_channels,
        )
        return {
            str(item.channel.channel_point) for collection in collections for item in collection
        }

    async def _list_pending_channel_points(self, *, timeout_seconds: float) -> set[str]:
        self._ensure()
        try:
            response = await asyncio.wait_for(
                self._stub.PendingChannels(ln.PendingChannelsRequest()), timeout_seconds
            )
        except TimeoutError as exc:
            raise LndTimeoutError("PendingChannels timed out during channel retirement") from exc
        except Exception as exc:
            raise LndRetirementRpcError("PendingChannels failed during channel retirement") from exc
        return self._pending_channel_points(response)

    async def pending_channel_present(
        self, channel_point: str, *, timeout_seconds: float = 10.0
    ) -> bool:
        """Return whether any PendingChannels category contains the exact point."""
        if re.fullmatch(r"[0-9a-f]{64}:[0-9]+", channel_point) is None:
            raise LndValidationError("invalid channel point")
        points = await self._list_pending_channel_points(timeout_seconds=timeout_seconds)
        return channel_point in points

    @staticmethod
    def _validate_retirement_authorization(
        authorization: VerifiedChannelRetirementAuthorization,
    ) -> None:
        verified = authorization.verified_funding
        if authorization.channel_point != verified.channel_point:
            raise LndValidationError("retirement channel point differs from VerifiedFunding")
        if authorization.unsigned_txid != verified.funding_txid:
            raise LndValidationError("retirement unsigned TXID differs from VerifiedFunding")
        if authorization.chain_status.unsigned_txid != verified.funding_txid:
            raise LndValidationError("chain status TXID differs from VerifiedFunding")
        if authorization.chain_status.mempool is not TransactionPresence.ABSENT:
            raise LndValidationError("funding TXID is present or uncertain in the mempool")
        if authorization.chain_status.chain is not TransactionPresence.ABSENT:
            raise LndValidationError("funding TXID is present or uncertain in the chain")
        if authorization.local_coinjoin_input_signature_created:
            raise LndValidationError("local CoinJoin input signature was already created")
        if authorization.local_coinjoin_input_signature_sent:
            raise LndValidationError("local CoinJoin input signature was already sent")
        if authorization.funding_transaction_broadcast:
            raise LndValidationError("funding transaction was already broadcast")
        if authorization.final_transaction_observed:
            raise LndValidationError("a final funding transaction was already observed")

    async def retire_verified_external_channel(
        self,
        authorization: VerifiedChannelRetirementAuthorization,
        *,
        timeout_seconds: float = 10.0,
        poll_interval: float = 0.1,
    ) -> VerifiedChannelRetirementOutcome:
        """Abandon one verified external channel only while its funding TX is impossible.

        ``AbandonChannel`` is destructive and unsafe for active, signed, published,
        observed, or uncertain funding transactions. Here it is restricted to an exact
        locally verified point whose unsigned funding transaction is proven absent from
        both mempool and chain, before this participant has created any input signature.
        """
        self._validate_retirement_authorization(authorization)
        verified = authorization.verified_funding
        pending_id = bytes(verified.pending_channel_id)
        local_pending = self._pending.get(pending_id)
        if local_pending is not None and local_pending.verified != verified:
            raise LndValidationError("local verified funding differs from retirement request")

        points = await self._list_pending_channel_points(timeout_seconds=timeout_seconds)
        if verified.channel_point not in points:
            self._pending.pop(pending_id, None)
            self._accepted.pop(pending_id, None)
            self._observed_verified_points.pop(pending_id, None)
            return VerifiedChannelRetirementOutcome(
                status=VerifiedChannelRetirementStatus.ALREADY_ABSENT,
                channel_point=verified.channel_point,
                unsigned_txid=verified.funding_txid,
            )

        locally_expected_point = self._observed_verified_points.get(pending_id)
        if locally_expected_point != verified.channel_point:
            raise LndValidationError(
                "pending channel was not locally observed at the exact verified point"
            )

        request = ln.AbandonChannelRequest(
            channel_point=ln.ChannelPoint(
                funding_txid_str=verified.funding_txid,
                output_index=verified.funding_vout,
            ),
            pending_funding_shim_only=False,
            i_know_what_i_am_doing=True,
        )
        try:
            response = await asyncio.wait_for(self._stub.AbandonChannel(request), timeout_seconds)
        except TimeoutError as exc:
            raise LndTimeoutError("AbandonChannel timed out") from exc
        except Exception as exc:
            raise LndRetirementRpcError("AbandonChannel failed") from exc

        deadline = time.monotonic() + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            points = await self._list_pending_channel_points(
                timeout_seconds=max(0.01, min(5.0, remaining))
            )
            if verified.channel_point not in points:
                break
            if remaining <= 0:
                raise LndRetirementPostconditionError(
                    f"PendingChannels still contains {verified.channel_point} after AbandonChannel"
                )
            await asyncio.sleep(min(poll_interval, max(0.0, remaining)))

        self._pending.pop(pending_id, None)
        self._accepted.pop(pending_id, None)
        self._observed_verified_points.pop(pending_id, None)
        return VerifiedChannelRetirementOutcome(
            status=VerifiedChannelRetirementStatus.ABANDONED,
            channel_point=verified.channel_point,
            unsigned_txid=verified.funding_txid,
            lnd_status=str(response.status),
        )

    @staticmethod
    def _parse_pending_observation(
        channel: Any,
        commit_fee: int,
        expected: PendingChannelExpectation,
        role: EndpointRole,
    ) -> PendingChannelObservation:
        opener = role is EndpointRole.OPENER
        expected_remote = expected.fundee_node_id if opener else expected.opener_node_id
        expected_initiator = ln.INITIATOR_LOCAL if opener else ln.INITIATOR_REMOTE
        expected_local_reserve = (
            expected.opener_reserve_sat if opener else expected.fundee_reserve_sat
        )
        expected_remote_reserve = (
            expected.fundee_reserve_sat if opener else expected.opener_reserve_sat
        )
        checks = (
            (str(channel.remote_node_pub) == expected_remote, "remote node"),
            (int(channel.capacity) == expected.capacity_sat, "capacity"),
            (int(channel.commitment_type) == FINAL_TAPROOT_COMMITMENT, "commitment type"),
            (int(channel.initiator) == expected_initiator, "initiator role"),
            (bool(channel.private), "private flag"),
            (int(channel.local_chan_reserve_sat) == expected_local_reserve, "local reserve"),
            (int(channel.remote_chan_reserve_sat) == expected_remote_reserve, "remote reserve"),
        )
        for valid, label in checks:
            if not valid:
                raise LndValidationError(f"PendingChannels {label} mismatch")
        commitment_overhead = (
            int(channel.capacity)
            - int(channel.local_balance)
            - int(channel.remote_balance)
            - commit_fee
        )
        return PendingChannelObservation(
            role=role,
            local_node_id=expected.opener_node_id if opener else expected.fundee_node_id,
            remote_node_id=str(channel.remote_node_pub),
            channel_point=str(channel.channel_point),
            capacity_sat=int(channel.capacity),
            local_balance_sat=int(channel.local_balance),
            remote_balance_sat=int(channel.remote_balance),
            commit_fee_sat=commit_fee,
            commitment_overhead_sat=commitment_overhead,
            local_reserve_sat=int(channel.local_chan_reserve_sat),
            remote_reserve_sat=int(channel.remote_chan_reserve_sat),
            commitment_type=int(channel.commitment_type),
            initiator=int(channel.initiator),
            private=bool(channel.private),
        )

    async def sign_readiness(
        self, statement: ReadinessStatement, *, timeout_seconds: float = 10.0
    ) -> str:
        self._ensure()
        response = await asyncio.wait_for(
            self._stub.SignMessage(ln.SignMessageRequest(msg=statement.canonical_bytes())),
            timeout_seconds,
        )
        return str(response.signature)

    async def verify_readiness_signature(
        self,
        statement: ReadinessStatement,
        signature: str,
        expected_node_id: NodeId,
        *,
        timeout_seconds: float = 10.0,
    ) -> bool:
        """Ask LND to recover a graph-known signer; false is never treated as readiness."""
        self._ensure()
        response = await asyncio.wait_for(
            self._stub.VerifyMessage(
                ln.VerifyMessageRequest(msg=statement.canonical_bytes(), signature=signature)
            ),
            timeout_seconds,
        )
        return bool(response.valid) and str(response.pubkey) == expected_node_id
