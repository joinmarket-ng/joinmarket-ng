"""Validated LND initialization for co-funded channel-ring participants."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

import grpc
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeConfig
from jmcore.channel_ring_store import RingLifecycleState, RingParticipantRecord
from jmcore.cofunded_ring import (
    BackendLimits,
    PrivateParticipant,
    RingNetwork,
    Tr0OfferType,
)

from jmswap.buyout_messages import Outpoint
from jmswap.lnd import LndBackend, LndCapabilityError, LndError, LndNodeInfo
from jmswap.lnd_escrow import LndEscrowClient, LndEscrowError, LndEscrowRpcError

# Advisory ceiling of LND's public-anchor reserve policy (10k per channel,
# capped at 100k). Private ring channels are excluded from that opening check.
# WalletBalance is not a measure of readily spendable fee-bump UTXOs.
RECOMMENDED_ONCHAIN_RESERVE_SAT = 100_000

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InitializedChannelRingBackend:
    """Backend state that is safe to use for capability advertising."""

    backend: LndBackend = field(repr=False)
    node_info: LndNodeInfo
    onion_endpoint: str
    backend_limits: BackendLimits

    def private_participant(self, participant_key: str) -> PrivateParticipant:
        return PrivateParticipant(
            participant_key=participant_key,
            node_id=self.node_info.identity_pubkey,
            onion_endpoint=self.onion_endpoint,
            backend_limits=self.backend_limits,
        )


async def initialize_channel_ring_backend(
    config: ChannelRingConfig,
    *,
    node: ChannelRingNodeConfig,
    network: str,
    offer_type: str,
    recovery: bool = False,
) -> InitializedChannelRingBackend:
    """Create and fully validate LND before the ring feature is advertised."""
    if not config.enabled and not recovery:
        raise ValueError("channel ring backend cannot initialize while disabled")
    if network not in {"mainnet", "testnet", "signet", "regtest"}:
        raise ValueError("unsupported channel ring network")
    if offer_type not in {"tr0absoffer", "tr0reloffer"}:
        raise ValueError("channel ring requires a tr0 offer type")
    grpc_target = node.lnd_grpc_url.removeprefix("https://")
    backend = LndBackend.from_paths(
        grpc_target,
        node.lnd_tls_cert_path,
        node.lnd_macaroon_path,
        network=network,
    )
    try:
        info = await backend.check_production_private_channel_ring(
            node.onion_endpoint,
            timeout_seconds=config.open_timeout_seconds,
        )
    except BaseException:
        await backend.close()
        raise
    if 47 not in info.feature_bits:
        await backend.close()
        raise LndCapabilityError("channel ring LND must advertise SCID alias feature bit 47")
    await _warn_on_low_onchain_reserve(backend, min(config.open_timeout_seconds, 10.0))
    limits = BackendLimits(
        network=cast(RingNetwork, network),
        offer_type=cast(Tr0OfferType, offer_type),
        min_channel_capacity=config.min_channel_capacity,
        max_channel_capacity=config.max_channel_capacity,
        max_push_amount=config.max_push,
        dust_limit=354,
        max_reserve=max(config.opener_reserve, config.fundee_reserve),
        max_commitment_fee=config.maximum_commitment_fee,
        max_pending_channels=config.max_verified_sessions,
    )
    return InitializedChannelRingBackend(
        backend=backend,
        node_info=info,
        onion_endpoint=node.onion_endpoint,
        backend_limits=limits,
    )


async def _warn_on_low_onchain_reserve(backend: LndBackend, timeout_seconds: float) -> None:
    """Warn about low confirmed balance, not prove fee-bump funds are usable.

    Private ring channels are excluded from LND's public-channel opening
    reserve check, so the node may have channels and no wallet UTXOs.
    """
    try:
        balance = await backend.confirmed_onchain_balance(timeout_seconds=timeout_seconds)
    except Exception as exc:
        logger.warning("Could not read the ring LND on-chain balance: %s", type(exc).__name__)
        return
    if type(balance) is not int:
        logger.warning("Could not read the ring LND on-chain balance")
    elif balance < RECOMMENDED_ONCHAIN_RESERVE_SAT:
        logger.warning(
            "Ring LND has %d sat confirmed on chain, below the %d sat advisory"
            " wallet target for anchor force-close fee bumps; this balance does"
            " not prove readily spendable UTXOs are available. Fund it only from"
            " this node's source mixdepth",
            balance,
            RECOMMENDED_ONCHAIN_RESERVE_SAT,
        )


class EscrowEnrollment(StrEnum):
    """Outcome of enrolling one ring channel for channel-escrow buyouts."""

    ENROLLED = "enrolled"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


async def enroll_escrow_channels(
    backend: LndBackend,
    points: Iterable[tuple[str, int]],
    authorizer_pubkey: bytes,
    *,
    timeout_seconds: float = 30.0,
) -> dict[tuple[str, int], EscrowEnrollment]:
    """Enroll ring channels under this wallet's escrow authorizer; never raises.

    Enrollment lets a later buyout of the channel be signed (only with this
    wallet's authorization). It is not needed for ring operation, so a node
    without the hardened escrow patch (``UNSUPPORTED``) or any failure leaves
    the channel working but not buyout-capable. Re-enrolling the same key is
    idempotent. Points are ``(display txid, vout)``.
    """
    outcomes: dict[tuple[str, int], EscrowEnrollment] = {}
    pending = list(dict.fromkeys(points))
    if not pending:
        return outcomes
    try:
        async with LndEscrowClient(
            backend.host,
            backend.tls_cert,
            bytes.fromhex(backend.macaroon_hex),
            timeout=timeout_seconds,
        ) as client:
            for txid, vout in pending:
                try:
                    await client.authorize_channel(
                        Outpoint(txid=txid, vout=vout), authorizer_pubkey
                    )
                except LndEscrowRpcError as exc:
                    if exc.code is grpc.StatusCode.UNIMPLEMENTED:
                        outcomes[(txid, vout)] = EscrowEnrollment.UNSUPPORTED
                        continue
                    raise
                outcomes[(txid, vout)] = EscrowEnrollment.ENROLLED
    except (LndEscrowError, OSError, ValueError) as exc:
        # Only the error class and status reach the log, never backend detail.
        logger.warning(
            "Ring channel escrow enrollment failed (%s); the channel works but cannot be "
            "bought out until `jm-maker authorize-ring-channels` succeeds",
            exc,
        )
    for point in pending:
        outcomes.setdefault(point, EscrowEnrollment.FAILED)
    if EscrowEnrollment.UNSUPPORTED in outcomes.values():
        logger.info("Ring LND has no escrow authorization; its channels are not buyout-capable")
    return outcomes


def ring_channel_points(record: RingParticipantRecord) -> tuple[tuple[str, int], ...]:
    """This participant's channel points in a ring record, as ``(txid, vout)``."""
    return tuple(
        (edge.funding_outpoint.txid, edge.funding_outpoint.vout)
        for edge in (record.incoming_edge, record.outgoing_edge)
        if edge is not None and edge.funding_outpoint is not None
    )


async def enroll_confirmed_ring_channels(
    backend: LndBackend, record: RingParticipantRecord, authorizer_pubkey: bytes | None
) -> None:
    """Best-effort enrollment of a ring's channels when its funding confirms.

    Channels confirmed before this release are enrolled only by the explicit
    ``jm-maker authorize-ring-channels`` command.
    """
    if authorizer_pubkey is None:
        return
    await enroll_escrow_channels(backend, ring_channel_points(record), authorizer_pubkey)


def wallet_escrow_authorizer_pubkey(wallet: object) -> bytes | None:
    """The wallet's escrow authorizer key, or ``None`` for a wallet without one."""
    authorizer = getattr(wallet, "escrow_authorizer", None)
    public_key = getattr(authorizer, "public_key", None)
    return public_key if isinstance(public_key, bytes) else None


class RingAuthorizationError(Exception):
    """Recorded ring channels cannot be enrolled safely."""


async def authorize_recorded_ring_channels(
    config: ChannelRingConfig,
    records: Sequence[RingParticipantRecord],
    *,
    network: str,
    wallet_identity: str,
    authorizer_pubkey: bytes,
) -> dict[tuple[str, int], EscrowEnrollment]:
    """Enroll the channels of this wallet's confirmed ring records.

    Each record is enrolled on the LND node its journal binding names, never by
    the current mixdepth mapping. Records of another wallet or network refuse
    the whole run before any RPC, as does a node missing from the config. Every
    configured endpoint must then resolve to the recorded LND identity before
    any enrollment: the other endpoint of a channel knows the same funding
    outpoint, so a repointed name would otherwise enroll this wallet's key there.
    """
    by_node: dict[str, list[tuple[str, int]]] = {}
    expected_ids: dict[str, set[str]] = {}
    for record in records:
        if record.state is not RingLifecycleState.CONFIRMED_OPEN:
            continue
        binding = record.node_binding
        if (binding.network, binding.wallet_identity) != (network, wallet_identity):
            raise RingAuthorizationError("ring journal belongs to a different wallet or network")
        if binding.node_name not in config.nodes:
            raise RingAuthorizationError(
                f"journal-recorded LND node {binding.node_name!r} is not configured"
            )
        by_node.setdefault(binding.node_name, []).extend(ring_channel_points(record))
        expected_ids.setdefault(binding.node_name, set()).add(binding.local_node_id)
    backends: dict[str, LndBackend] = {}
    try:
        for name in by_node:
            node = config.nodes[name]
            backend = LndBackend.from_paths(
                node.lnd_grpc_url.removeprefix("https://"),
                node.lnd_tls_cert_path,
                node.lnd_macaroon_path,
                network=network,
            )
            backends[name] = backend
            try:
                info = await backend.node_info()
            except (LndError, grpc.RpcError, OSError) as exc:
                raise RingAuthorizationError(
                    f"LND node {name!r} identity could not be verified ({type(exc).__name__})"
                ) from None
            if info.network != network or expected_ids[name] != {info.identity_pubkey}:
                raise RingAuthorizationError(
                    f"configured LND node {name!r} resolves to another LND identity"
                )
        outcomes: dict[tuple[str, int], EscrowEnrollment] = {}
        for name, points in by_node.items():
            outcomes.update(await enroll_escrow_channels(backends[name], points, authorizer_pubkey))
        return outcomes
    finally:
        for backend in backends.values():
            await backend.close()
