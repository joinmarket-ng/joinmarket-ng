"""Strict runtime configuration for co-funded channel rings."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jmcore.constants import MAX_MONEY

FINAL_TAPROOT_COMMITMENT_OVERHEAD_SAT = 660

_V3_ONION_ENDPOINT = re.compile(r"^[a-z2-7]{56}\.onion:(?:[1-9][0-9]{0,4})$")
_MAX_CHANNEL_CAPACITY_SAT = 100_000_000
_MAX_PUSH_SAT = 50_000_000
_MAX_COMMITMENT_FEE_SAT = 1_000_000


class ChannelRingSettings(BaseModel):
    """User-facing settings shared by maker and taker."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    lnd_grpc_url: str = ""
    lnd_tls_cert_path: Path | None = None
    lnd_macaroon_path: Path | None = None
    onion_endpoint: str = ""
    minimum_makers: int = Field(default=3, ge=3, le=31)
    min_channel_capacity: int = Field(default=1_000_000, ge=1, le=MAX_MONEY)
    max_channel_capacity: int = Field(default=20_000_000, ge=1, le=MAX_MONEY)
    max_push: int = Field(default=500_000, ge=1, le=MAX_MONEY)
    opener_reserve: int = Field(default=10_000, ge=1, le=MAX_MONEY)
    fundee_reserve: int = Field(default=10_000, ge=1, le=MAX_MONEY)
    maximum_commitment_fee: int = Field(default=100_000, ge=1, le=MAX_MONEY)
    taproot_commitment_overhead: int = FINAL_TAPROOT_COMMITMENT_OVERHEAD_SAT
    spendable_margin: int = Field(default=100_000, ge=1, le=MAX_MONEY)
    confirmation_depth: int = Field(default=3, ge=1, le=144)
    minimum_csv_delay: int = Field(default=72, ge=1, le=2016)
    maximum_csv_delay: int = Field(default=432, ge=1, le=2016)
    allowed_csv_delays: list[int] = Field(default_factory=lambda: [72, 144, 288, 432])
    phase_timeout_seconds: float = Field(default=120.0, ge=10.0, le=600.0)
    open_timeout_seconds: float = Field(default=60.0, ge=10.0, le=300.0)
    readiness_timeout_seconds: float = Field(default=120.0, ge=10.0, le=600.0)
    max_active_sessions: int = Field(default=4, ge=1, le=16)
    max_verified_sessions: int = Field(default=2, ge=1, le=8)
    persistence_directory: Path | None = None


class ChannelRingConfig(BaseModel):
    """Validated runtime policy carried as one nested maker/taker config value."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: bool = False
    lnd_grpc_url: str = ""
    lnd_tls_cert_path: Path | None = None
    lnd_macaroon_path: Path | None = None
    onion_endpoint: str = ""
    minimum_makers: int = Field(default=3, ge=3, le=31)
    min_channel_capacity: int = Field(default=1_000_000, ge=1, le=MAX_MONEY)
    max_channel_capacity: int = Field(default=20_000_000, ge=1, le=MAX_MONEY)
    max_push: int = Field(default=500_000, ge=1, le=MAX_MONEY)
    opener_reserve: int = Field(default=10_000, ge=1, le=MAX_MONEY)
    fundee_reserve: int = Field(default=10_000, ge=1, le=MAX_MONEY)
    maximum_commitment_fee: int = Field(default=100_000, ge=1, le=MAX_MONEY)
    taproot_commitment_overhead: int = FINAL_TAPROOT_COMMITMENT_OVERHEAD_SAT
    spendable_margin: int = Field(default=100_000, ge=1, le=MAX_MONEY)
    confirmation_depth: int = Field(default=3, ge=1, le=144)
    minimum_csv_delay: int = Field(default=72, ge=1, le=2016)
    maximum_csv_delay: int = Field(default=432, ge=1, le=2016)
    allowed_csv_delays: tuple[int, ...] = (72, 144, 288, 432)
    phase_timeout_seconds: float = Field(default=120.0, ge=10.0, le=600.0)
    open_timeout_seconds: float = Field(default=60.0, ge=10.0, le=300.0)
    readiness_timeout_seconds: float = Field(default=120.0, ge=10.0, le=600.0)
    max_active_sessions: int = Field(default=4, ge=1, le=16)
    max_verified_sessions: int = Field(default=2, ge=1, le=8)
    persistence_directory: Path | None = None

    @classmethod
    def from_settings(cls, settings: ChannelRingSettings) -> ChannelRingConfig:
        values = settings.model_dump()
        values["allowed_csv_delays"] = tuple(settings.allowed_csv_delays)
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

    @field_validator("onion_endpoint")
    @classmethod
    def validate_onion_endpoint(cls, value: str) -> str:
        if not value:
            return value
        if "@" in value or not _V3_ONION_ENDPOINT.fullmatch(value):
            raise ValueError("onion_endpoint must be a v3 onion host:port without a node ID")
        port = int(value.rpartition(":")[2])
        if port > 65535:
            raise ValueError("onion_endpoint port must be at most 65535")
        return value

    @field_validator("allowed_csv_delays")
    @classmethod
    def validate_csv_choices(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or len(value) > 8 or len(set(value)) != len(value):
            raise ValueError("allowed_csv_delays must contain 1 to 8 unique choices")
        if tuple(sorted(value)) != value or any(delay < 1 or delay > 2016 for delay in value):
            raise ValueError("allowed_csv_delays must be sorted values between 1 and 2016")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> ChannelRingConfig:
        if self.taproot_commitment_overhead != FINAL_TAPROOT_COMMITMENT_OVERHEAD_SAT:
            raise ValueError("taproot_commitment_overhead is fixed at 660 satoshis")
        if self.min_channel_capacity > self.max_channel_capacity:
            raise ValueError("minimum channel capacity exceeds maximum")
        if self.max_channel_capacity > _MAX_CHANNEL_CAPACITY_SAT:
            raise ValueError("maximum channel capacity exceeds the conservative 1 BTC cap")
        if self.max_push > min(_MAX_PUSH_SAT, self.min_channel_capacity - 1):
            raise ValueError("max_push must remain below every permitted channel capacity")
        if self.maximum_commitment_fee > _MAX_COMMITMENT_FEE_SAT:
            raise ValueError("maximum commitment fee exceeds the anti-grief cap")
        for name, reserve in (
            ("opener_reserve", self.opener_reserve),
            ("fundee_reserve", self.fundee_reserve),
        ):
            if reserve * 5 >= self.min_channel_capacity:
                raise ValueError(f"{name} must be below 20% of minimum channel capacity")
        required_spendable = (
            self.opener_reserve
            + self.fundee_reserve
            + self.maximum_commitment_fee
            + self.taproot_commitment_overhead
            + self.spendable_margin
        )
        if required_spendable >= self.min_channel_capacity:
            raise ValueError("reserves, fees, overhead, and margin consume minimum capacity")
        if self.minimum_csv_delay > self.maximum_csv_delay:
            raise ValueError("minimum CSV delay exceeds maximum")
        if any(
            delay < self.minimum_csv_delay or delay > self.maximum_csv_delay
            for delay in self.allowed_csv_delays
        ):
            raise ValueError("allowed CSV delays must fall within configured bounds")
        if self.max_verified_sessions > self.max_active_sessions:
            raise ValueError("max_verified_sessions cannot exceed max_active_sessions")
        if self.enabled:
            if not self.lnd_grpc_url:
                raise ValueError("enabled channel ring requires lnd_grpc_url")
            if self.lnd_tls_cert_path is None:
                raise ValueError("enabled channel ring requires lnd_tls_cert_path")
            if self.lnd_macaroon_path is None:
                raise ValueError("enabled channel ring requires lnd_macaroon_path")
            if not self.onion_endpoint:
                raise ValueError("enabled channel ring requires onion_endpoint")
        return self

    def persistence_path(self, data_directory: Path) -> Path:
        return self.persistence_directory or data_directory / "channel-ring"
