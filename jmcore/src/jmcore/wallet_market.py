"""Local wallet-market operation models, independent of the seller runtime package."""

from __future__ import annotations

from pydantic import Field, field_validator

from jmcore.constants import MAX_MONEY
from jmcore.credential_market import MarketModel, Product
from jmcore.external_podle import ExternalPoDLEOutpoint


class WalletMarketSellerOptions(MarketModel):
    """Explicit, ephemeral seller options for one unlocked wallet session."""

    bond: ExternalPoDLEOutpoint
    products: list[Product] = Field(min_length=1, max_length=2)
    price_sats: int = Field(ge=1, le=MAX_MONEY)
    quote_ttl: int = Field(default=300, ge=1, le=900)

    @field_validator("products")
    @classmethod
    def unique_products(cls, value: list[Product]) -> list[Product]:
        if len(set(value)) != len(value):
            raise ValueError("Seller products must be unique")
        return value
