from __future__ import annotations

from jmcore.models import Offer, OfferType
from jmcore.protocol import FEATURE_COFUNDED_CHANNEL_RING_V1

from taker.orderbook import offer_supports_cofunded_channel_ring_v1


def offer(*, supported: bool) -> Offer:
    return Offer(
        counterparty="maker",
        oid=0,
        ordertype=OfferType.TR0_ABSOLUTE,
        minsize=100_000,
        maxsize=1_000_000,
        txfee=0,
        cjfee=500,
        features={FEATURE_COFUNDED_CHANNEL_RING_V1: True} if supported else {},
    )


def test_taker_recognizes_ring_capability_without_selecting_on_it() -> None:
    assert offer_supports_cofunded_channel_ring_v1(offer(supported=True))
    assert not offer_supports_cofunded_channel_ring_v1(offer(supported=False))
