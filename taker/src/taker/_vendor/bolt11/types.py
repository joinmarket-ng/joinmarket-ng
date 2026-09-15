import json
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from .exceptions import (
    Bolt11DescriptionException,
    Bolt11NoMinFinalCltvException,
    Bolt11NoPaymentHashException,
    Bolt11NoPaymentSecretException,
    Bolt11NoSignatureException,
)
from .models.fallback import Fallback
from .models.features import Features
from .models.routehint import RouteHint
from .models.signature import Signature
from .models.tags import TagChar, Tags


class MilliSatoshi(int):
    """A thousandth of a satoshi."""

    @classmethod
    def from_btc(cls, btc: Decimal) -> "MilliSatoshi":
        return cls(btc * 100_000_000_000)

    @property
    def btc(self) -> Decimal:
        return Decimal(self) / 100_000_000_000

    @property
    def sat(self) -> int:
        return self // 1000


@dataclass
class Bolt11:
    """Bolt11 Lightning invoice."""

    currency: str
    date: int
    tags: Tags
    amount_msat: Optional[MilliSatoshi] = None
    signature: Optional[Signature] = None

    def validate(self, strict: bool = False) -> None:
        if not self.tags.get(TagChar.payment_hash):
            raise Bolt11NoPaymentHashException()
        if not self.tags.get(TagChar.payment_secret):
            raise Bolt11NoPaymentSecretException()
        if not self.signature:
            raise Bolt11NoSignatureException()
        if strict and not self.tags.get(TagChar.min_final_cltv_expiry):
            raise Bolt11NoMinFinalCltvException()
        if (
            self.tags.get(TagChar.description)
            and self.tags.get(TagChar.description_hash)
            or not self.tags.get(TagChar.description)
            and not self.tags.get(TagChar.description_hash)
        ):
            raise Bolt11DescriptionException()

    def has_expired(self) -> bool:
        return time.time() > self.date + self.expiry

    def is_mainnet(self) -> bool:
        return self.currency == "bc"

    def is_testnet(self) -> bool:
        return self.currency == "tb"

    def is_signet(self) -> bool:
        return self.currency == "tbs"

    def is_regtest(self) -> bool:
        return self.currency == "bcrt"

    @property
    def description(self) -> Optional[str]:
        tag = self.tags.get(TagChar.description)
        return tag.data if tag else None

    @property
    def description_hash(self) -> Optional[str]:
        tag = self.tags.get(TagChar.description_hash)
        return tag.data if tag else None

    @property
    def metadata(self) -> Optional[str]:
        tag = self.tags.get(TagChar.metadata)
        return tag.data if tag else None

    # backwards compatibility
    @property
    def dt(self) -> datetime:
        return self.date_time

    @property
    def date_time(self) -> datetime:
        return datetime.fromtimestamp(self.date)

    @property
    def expiry(self) -> int:
        tag = self.tags.get(TagChar.expire_time)
        if not tag:
            return 3600
        return tag.data

    @property
    def expiry_date(self) -> datetime:
        return datetime.fromtimestamp(self.date + self.expiry)

    @property
    def expiry_time(self) -> int:
        return self.date + self.expiry

    @property
    def features(self) -> Optional[Features]:
        tag = self.tags.get(TagChar.features)
        return tag.data if tag else None

    @property
    def fallback(self) -> Optional[Fallback]:
        tag = self.tags.get(TagChar.fallback)
        return tag.data if tag else None

    @property
    def route_hints(self) -> Optional[List[RouteHint]]:
        return self.tags.get_route_hints()

    @property
    def min_final_cltv_expiry(self) -> int:
        tag = self.tags.get(TagChar.min_final_cltv_expiry)
        return tag.data if tag else 18

    @property
    def has_payment_hash(self) -> bool:
        return self.tags.get(TagChar.payment_hash) is not None

    @property
    def payment_hash(self) -> str:
        payment_hash = self.tags.get(TagChar.payment_hash)
        if not payment_hash:
            raise Bolt11NoPaymentHashException()
        return payment_hash.data

    @property
    def payment_secret(self) -> Optional[str]:
        tag = self.tags.get(TagChar.payment_secret)
        return tag.data if tag else None

    @property
    def payee(self) -> Optional[str]:
        tag = self.tags.get(TagChar.payee)
        return tag.data if tag else None

    @property
    def data(self) -> dict:
        data = {
            "currency": self.currency,
            "amount_msat": int(self.amount_msat) if self.amount_msat else 0,
            "date": self.date,
            "signature": self.signature.hex if self.signature else "",
            "expiry": self.expiry,
        }
        if self.has_payment_hash:
            data["payment_hash"] = self.payment_hash
        if self.payment_secret:
            data["payment_secret"] = self.payment_secret
        if self.description:
            data["description"] = self.description
        if self.description_hash:
            data["description_hash"] = self.description_hash
        if self.metadata:
            data["metadata"] = self.metadata
        if self.features:
            data["features"] = self.features.readable
        if self.fallback:
            data["fallback"] = self.fallback.address
        if self.route_hints:
            data["route_hints"] = [
                [route._asdict() for route in route_hint.routes] for route_hint in self.route_hints
            ]
        if self.min_final_cltv_expiry:
            data["min_final_cltv_expiry"] = self.min_final_cltv_expiry
        if self.payee:
            data["payee"] = self.payee
        return data

    @property
    def json(self) -> str:
        return json.dumps(self.data)
