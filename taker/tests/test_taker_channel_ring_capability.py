from __future__ import annotations

from jmcore.models import Offer, OfferType
from jmcore.protocol import FEATURE_PRIVATE_CHANNEL_RING

from taker.orderbook import offer_supports_private_channel_ring


def offer(*, supported: bool) -> Offer:
    return Offer(
        counterparty="maker",
        oid=0,
        ordertype=OfferType.TR0_ABSOLUTE,
        minsize=100_000,
        maxsize=1_000_000,
        txfee=0,
        cjfee=500,
        features={FEATURE_PRIVATE_CHANNEL_RING: True} if supported else {},
    )


def test_taker_recognizes_ring_capability_without_selecting_on_it() -> None:
    assert offer_supports_private_channel_ring(offer(supported=True))
    assert not offer_supports_private_channel_ring(offer(supported=False))
