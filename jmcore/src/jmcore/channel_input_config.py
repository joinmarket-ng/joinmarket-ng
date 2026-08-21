"""Strict runtime configuration for experimental channel-input v1."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from coincurve import PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jmcore.channel_input import MAX_FEE_RATE_SAT_VB
from jmcore.constants import MAX_MONEY

_MAX_CHANNEL_CAPACITY_SAT = 100_000_000
_CHANNEL_POINT_RE = re.compile(r"^[0-9a-f]{64}:(?:0|[1-9][0-9]{0,9})$")
_COMPRESSED_PUBKEY_RE = re.compile(r"^(?:02|03)[0-9a-f]{64}$")


class ChannelInputSettings(BaseModel):
    """User-facing channel-input v1 settings shared by maker and taker."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    lnd_grpc_url: str = ""
    lnd_tls_cert_path: Path | None = None
    lnd_macaroon_path: Path | None = None
    lnd_escrow_macaroon_path: Path | None = None
    allowed_channel_points: list[str] = Field(default_factory=list)
    allowed_peer_node_pubkeys: list[str] = Field(default_factory=list)
    minimum_makers: int = Field(default=3, ge=3, le=20)
    min_channel_capacity_sat: int = Field(default=100_000, ge=1, le=MAX_MONEY)
    max_channel_capacity_sat: int = Field(default=100_000_000, ge=1, le=MAX_MONEY)
    max_claim_adjustment_sat: int = Field(default=50_000, gt=0, le=MAX_MONEY)
    max_miner_fee_share_sat: int = Field(default=50_000, gt=0, le=MAX_MONEY)
    max_parent_fee_sat: int = Field(default=1_000_000, gt=0, le=MAX_MONEY)
    max_fee_rate_sat_vb: int = Field(default=1_000, gt=0, le=MAX_FEE_RATE_SAT_VB)
    recovery_delay_blocks: int = Field(default=12, ge=6, le=2016)
    phase_timeout_seconds: float = Field(default=120.0, ge=10.0, le=600.0)
    max_active_sessions: int = Field(default=4, ge=1, le=16)
    persistence_directory: Path | None = None


class ChannelInputConfig(BaseModel):
    """Validated immutable runtime policy for channel-input v1."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: bool = False
    lnd_grpc_url: str = ""
    lnd_tls_cert_path: Path | None = None
    lnd_macaroon_path: Path | None = None
    lnd_escrow_macaroon_path: Path | None = None
    allowed_channel_points: tuple[str, ...] = ()
    allowed_peer_node_pubkeys: tuple[str, ...] = ()
    minimum_makers: int = Field(default=3, ge=3, le=20)
    min_channel_capacity_sat: int = Field(default=100_000, ge=1, le=MAX_MONEY)
    max_channel_capacity_sat: int = Field(default=100_000_000, ge=1, le=MAX_MONEY)
    max_claim_adjustment_sat: int = Field(default=50_000, gt=0, le=MAX_MONEY)
    max_miner_fee_share_sat: int = Field(default=50_000, gt=0, le=MAX_MONEY)
    max_parent_fee_sat: int = Field(default=1_000_000, gt=0, le=MAX_MONEY)
    max_fee_rate_sat_vb: int = Field(default=1_000, gt=0, le=MAX_FEE_RATE_SAT_VB)
    recovery_delay_blocks: int = Field(default=12, ge=6, le=2016)
    phase_timeout_seconds: float = Field(default=120.0, ge=10.0, le=600.0)
    max_active_sessions: int = Field(default=4, ge=1, le=16)
    persistence_directory: Path | None = None

    @classmethod
    def from_settings(cls, settings: ChannelInputSettings) -> ChannelInputConfig:
        values = settings.model_dump()
        values["allowed_channel_points"] = tuple(settings.allowed_channel_points)
        values["allowed_peer_node_pubkeys"] = tuple(settings.allowed_peer_node_pubkeys)
        return cls(**values)

    @field_validator("lnd_grpc_url")
    @classmethod
    def validate_lnd_endpoint(cls, value: str) -> str:
        if not value:
            return value
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("lnd_grpc_url must be a TLS loopback host:port")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("lnd_grpc_url must contain a valid port") from exc
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or port is None:
            raise ValueError("lnd_grpc_url must use a private loopback endpoint")
        return value

    @field_validator("allowed_channel_points")
    @classmethod
    def validate_allowed_channel_points(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_channel_points must be unique")
        for channel_point in value:
            if _CHANNEL_POINT_RE.fullmatch(channel_point) is None:
                raise ValueError("allowed_channel_points must be lowercase <64hex>:<uint32> values")
            vout = int(channel_point.rpartition(":")[2])
            if vout > 0xFFFFFFFF:
                raise ValueError("allowed channel point vout must be a uint32")
        return value

    @field_validator("allowed_peer_node_pubkeys")
    @classmethod
    def validate_allowed_peer_node_pubkeys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("allowed_peer_node_pubkeys must be unique")
        for public_key in value:
            if _COMPRESSED_PUBKEY_RE.fullmatch(public_key) is None:
                raise ValueError(
                    "allowed_peer_node_pubkeys must be lowercase compressed secp256k1 keys"
                )
            try:
                PublicKey(bytes.fromhex(public_key))
            except ValueError as exc:
                raise ValueError(
                    "allowed_peer_node_pubkeys must contain valid secp256k1 keys"
                ) from exc
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> ChannelInputConfig:
        if self.min_channel_capacity_sat > self.max_channel_capacity_sat:
            raise ValueError("minimum channel capacity exceeds maximum")
        if self.max_channel_capacity_sat > _MAX_CHANNEL_CAPACITY_SAT:
            raise ValueError("maximum channel capacity exceeds the conservative 1 BTC cap")
        if self.enabled:
            if not self.lnd_grpc_url:
                raise ValueError("enabled channel input requires lnd_grpc_url")
            if self.lnd_tls_cert_path is None:
                raise ValueError("enabled channel input requires lnd_tls_cert_path")
            if self.lnd_macaroon_path is None:
                raise ValueError("enabled channel input requires lnd_macaroon_path")
            if self.lnd_escrow_macaroon_path is None:
                raise ValueError("enabled channel input requires lnd_escrow_macaroon_path")
            if not self.allowed_channel_points:
                raise ValueError(
                    "enabled channel input requires at least one allowed channel point"
                )
            capacity_fee_limit = self.min_channel_capacity_sat // 2
            if self.max_claim_adjustment_sat > capacity_fee_limit:
                raise ValueError("max_claim_adjustment_sat exceeds half minimum channel capacity")
            if self.max_miner_fee_share_sat > capacity_fee_limit:
                raise ValueError("max_miner_fee_share_sat exceeds half minimum channel capacity")
        return self

    def persistence_path(self, data_directory: Path) -> Path:
        return self.persistence_directory or data_directory / "channel-input"
