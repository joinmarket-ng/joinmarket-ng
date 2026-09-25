"""Validated LND initialization for co-funded channel-ring participants."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from jmcore.channel_ring import ChannelRingConfig
from jmcore.cofunded_ring import (
    BackendLimits,
    PrivateParticipant,
    RingNetwork,
    Tr0OfferType,
)

from jmswap.lnd import LndBackend, LndNodeInfo


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
    network: str,
    offer_type: str,
) -> InitializedChannelRingBackend:
    """Create and fully validate LND before the ring feature is advertised."""
    if not config.enabled:
        raise ValueError("channel ring backend cannot initialize while disabled")
    if network not in {"mainnet", "testnet", "signet", "regtest"}:
        raise ValueError("unsupported channel ring network")
    if offer_type not in {"tr0absoffer", "tr0reloffer"}:
        raise ValueError("channel ring requires a tr0 offer type")
    if config.lnd_tls_cert_path is None or config.lnd_macaroon_path is None:
        raise ValueError("channel ring backend requires LND TLS certificate and macaroon paths")
    grpc_target = config.lnd_grpc_url.removeprefix("https://")
    backend = LndBackend.from_paths(
        grpc_target,
        config.lnd_tls_cert_path,
        config.lnd_macaroon_path,
        network=network,
    )
    try:
        info = await backend.check_production_cofunded_channel_ring_v1(
            config.onion_endpoint,
            timeout_seconds=config.open_timeout_seconds,
        )
    except Exception:
        await backend.close()
        raise
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
        onion_endpoint=config.onion_endpoint,
        backend_limits=limits,
    )
