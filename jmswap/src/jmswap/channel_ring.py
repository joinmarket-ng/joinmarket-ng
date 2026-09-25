"""Validated LND initialization for co-funded channel-ring participants."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import cast

from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeConfig
from jmcore.cofunded_ring import (
    BackendLimits,
    PrivateParticipant,
    RingNetwork,
    Tr0OfferType,
)

from jmswap.lnd import LndBackend, LndCapabilityError, LndNodeInfo

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
            " not prove readily spendable UTXOs are available",
            balance,
            RECOMMENDED_ONCHAIN_RESERVE_SAT,
        )
