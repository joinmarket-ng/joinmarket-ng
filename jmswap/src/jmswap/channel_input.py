"""Validated LND initialization for channel-input v1 capability checks."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from jmcore.channel_input_config import ChannelInputConfig

from jmswap.lnd import (
    ChannelInputCandidate,
    EscrowOutpoint,
    LndBackend,
    LndCapabilityError,
    LndNodeInfo,
)


@dataclass(frozen=True)
class InitializedChannelInputBackend:
    """Backend state proven safe for a future channel-input v1 advertisement."""

    backend: LndBackend = field(repr=False)
    node_info: LndNodeInfo
    candidates: tuple[ChannelInputCandidate, ...]


async def initialize_channel_input_backend(
    config: ChannelInputConfig,
    *,
    network: str,
    offer_type: str,
    recovery_outpoints: Sequence[EscrowOutpoint] = (),
) -> InitializedChannelInputBackend:
    """Create and fully validate LND before channel-input v1 can be advertised."""
    if not config.enabled:
        raise ValueError("channel input backend cannot initialize while disabled")
    if network not in {"mainnet", "testnet", "signet", "regtest"}:
        raise ValueError("unsupported channel input network")
    if offer_type not in {"tr0absoffer", "tr0reloffer"}:
        raise ValueError("channel input requires a tr0 offer type")
    if not config.lnd_grpc_url:
        raise ValueError("channel input backend requires an LND gRPC URL")
    tls_cert_path = config.lnd_tls_cert_path
    macaroon_path = config.lnd_macaroon_path
    escrow_macaroon_path = config.lnd_escrow_macaroon_path
    if tls_cert_path is None or macaroon_path is None or escrow_macaroon_path is None:
        raise ValueError("channel input backend requires existing TLS, admin, and escrow paths")
    if not all(path.is_file() for path in (tls_cert_path, macaroon_path, escrow_macaroon_path)):
        raise ValueError("channel input backend requires existing TLS, admin, and escrow paths")
    if not config.allowed_channel_points:
        raise ValueError("channel input backend requires allowed channel points")

    allowed_outpoints = tuple(
        LndBackend._parse_escrow_outpoint(channel_point)
        for channel_point in config.allowed_channel_points
    )
    allowed_set = frozenset(allowed_outpoints)
    if isinstance(recovery_outpoints, (str, bytes)) or not isinstance(recovery_outpoints, Sequence):
        raise ValueError("recovery_outpoints must be a sequence of funding outpoints")
    if any(not isinstance(outpoint, EscrowOutpoint) for outpoint in recovery_outpoints):
        raise ValueError("recovery outpoint must be an EscrowOutpoint")
    normalized_recovery_outpoints = tuple(
        sorted(
            set(recovery_outpoints),
            key=lambda outpoint: (outpoint.txid, outpoint.vout),
        )
    )
    if any(outpoint not in allowed_set for outpoint in normalized_recovery_outpoints):
        raise ValueError("recovery outpoints must be configured channel points")
    backend = LndBackend.from_paths(
        config.lnd_grpc_url.removeprefix("https://"),
        tls_cert_path,
        macaroon_path,
        network=network,
        escrow_macaroon_path=escrow_macaroon_path,
    )
    try:
        info = await backend.node_info(timeout_seconds=config.phase_timeout_seconds)
        candidates = await backend.list_channel_input_candidates(
            allowed_outpoints=allowed_outpoints,
            allowed_peer_node_pubkeys=config.allowed_peer_node_pubkeys or None,
            timeout_seconds=config.phase_timeout_seconds,
        )
        if any(candidate.funding_outpoint not in allowed_set for candidate in candidates):
            raise LndCapabilityError("LND returned a channel outside the configured allowlist")
        eligible = tuple(
            sorted(
                (
                    candidate
                    for candidate in candidates
                    if config.min_channel_capacity_sat
                    <= candidate.capacity_sat
                    <= config.max_channel_capacity_sat
                ),
                key=lambda candidate: (
                    candidate.funding_outpoint.txid,
                    candidate.funding_outpoint.vout,
                    candidate.channel_id,
                ),
            )
        )
        if not eligible and not normalized_recovery_outpoints:
            raise LndCapabilityError("no configured channel input candidate meets capacity policy")
        probe_outpoint = (
            eligible[0].funding_outpoint if eligible else normalized_recovery_outpoints[0]
        )
        await backend.probe_channel_input_escrow(
            probe_outpoint,
            timeout_seconds=config.phase_timeout_seconds,
        )
    except Exception:
        await backend.close()
        raise
    return InitializedChannelInputBackend(backend=backend, node_info=info, candidates=eligible)
