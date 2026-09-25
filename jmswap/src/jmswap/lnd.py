"""Strict LND backend for externally funded, co-funded final Taproot channels."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import struct
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

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


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


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
        (not bool(request.wants_scid_alias), "SCID alias flag"),
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
    _channel: Any = field(default=None, repr=False)
    _stub: Any = field(default=None, repr=False)
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
    ) -> LndBackend:
        return cls(
            host=host,
            tls_cert=Path(tls_cert_path).read_bytes(),
            macaroon_hex=Path(macaroon_path).read_bytes().hex(),
            network=network,
        )

    def _ensure(self) -> None:
        if self._stub is not None:
            return
        import grpc  # type: ignore[import-untyped]

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

    async def close(self) -> None:
        if self._acceptor_task is not None:
            self._acceptor_task.cancel()
            await asyncio.gather(self._acceptor_task, return_exceptions=True)
        self._acceptor_task = None
        self._acceptor_started = None
        self._acceptor_call = None
        self._acceptor_registrations.clear()
        if self._channel is not None:
            await self._channel.close()
        self._channel = None
        self._stub = None

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
            scid_alias=False,
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
