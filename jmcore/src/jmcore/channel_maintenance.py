"""Validated policy shared by the node-local Lightning maintenance service."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from jmcore.constants import MAX_MONEY


class ChannelMaintenanceConfig(BaseModel):
    """Bounded local policy for zero-fee circular channel maintenance.

    The caller supplies the private state directory separately because it owns
    deployment and identity configuration. This model deliberately contains no
    peer, directory, or wallet credentials.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    enabled: bool = True
    interval_min_seconds: int = Field(default=900, ge=1, le=86_400)
    interval_max_seconds: int = Field(default=3_600, ge=1, le=86_400)
    amount_min_sat: int = Field(default=1_000, ge=1, le=MAX_MONEY)
    amount_max_sat: int = Field(default=50_000, ge=1, le=MAX_MONEY)
    liquidity_margin_sat: int = Field(default=10_000, ge=0, le=MAX_MONEY)
    max_spendable_percent: int = Field(default=10, ge=1, le=10)
    payment_timeout_seconds: int = Field(default=30, ge=1, le=30)
    peer_query_timeout_seconds: int = Field(default=5, ge=1, le=5)

    @model_validator(mode="after")
    def validate_ranges(self) -> ChannelMaintenanceConfig:
        if self.interval_min_seconds > self.interval_max_seconds:
            raise ValueError("maintenance interval minimum exceeds maximum")
        if self.amount_min_sat > self.amount_max_sat:
            raise ValueError("maintenance amount minimum exceeds maximum")
        return self

    @classmethod
    def from_settings(cls, settings: ChannelMaintenanceSettings) -> ChannelMaintenanceConfig:
        return cls(**settings.model_dump())


class ChannelMaintenanceSettings(ChannelMaintenanceConfig):
    """Environment/TOML coercion before strict runtime configuration construction."""

    model_config = ConfigDict(extra="forbid", strict=False, frozen=True)
