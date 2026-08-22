from __future__ import annotations

from types import SimpleNamespace

from jmcore.models import Offer, OfferType
from jmcore.protocol import FEATURE_CHANNEL_INPUT_V1, FEATURE_COFUNDED_CHANNEL_RING_V1

from taker.coinjoin_session import CoinJoinSession
from taker.orderbook import offer_supports_channel_input_v1, offer_supports_cofunded_channel_ring_v1
from taker.taker import Taker


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


def test_taker_recognizes_channel_input_capability() -> None:
    assert offer_supports_channel_input_v1(
        Offer(
            counterparty="maker",
            oid=0,
            ordertype=OfferType.TR0_ABSOLUTE,
            minsize=100_000,
            maxsize=1_000_000,
            txfee=0,
            cjfee=500,
            features={FEATURE_CHANNEL_INPUT_V1: True},
        )
    )
    assert not offer_supports_channel_input_v1(offer(supported=False))


def test_channel_input_selection_requires_explicit_exact_type_feature_offer() -> None:
    taker = Taker.__new__(Taker)
    taker._session = CoinJoinSession()
    taker.config = SimpleNamespace(
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
        channel_input=SimpleNamespace(enabled=True, minimum_makers=2),
        channel_ring=SimpleNamespace(enabled=False),
        minimum_makers=1,
    )
    explicit = Offer(
        counterparty="explicit",
        oid=0,
        ordertype=OfferType.TR0_ABSOLUTE,
        minsize=100_000,
        maxsize=1_000_000,
        txfee=0,
        cjfee=500,
        features={FEATURE_CHANNEL_INPUT_V1: True},
    )
    unknown = explicit.model_copy(update={"counterparty": "unknown", "features": {}})
    wrong_type = explicit.model_copy(
        update={"counterparty": "wrong-type", "ordertype": OfferType.TR0_RELATIVE}
    )

    filtered, error = taker._channel_input_filter_offers([explicit, unknown, wrong_type], 1)

    assert filtered == [explicit]
    assert error is None
    prepared, required_features = taker._prepare_channel_input_offer_selection([explicit], None, 1)
    assert prepared == [explicit]
    assert required_features == {FEATURE_CHANNEL_INPUT_V1}
    assert taker._channel_input_selection_error(0, 1) is not None


def test_channel_input_request_rejects_sweeps_and_insufficient_makers() -> None:
    taker = Taker.__new__(Taker)
    taker._session = CoinJoinSession()
    taker.config = SimpleNamespace(
        channel_input=SimpleNamespace(enabled=True, minimum_makers=3),
        minimum_makers=2,
    )

    assert taker._channel_input_request_error(0, 3) == (
        "Channel-input mode rejects sweep/no-change CoinJoins"
    )
    assert taker._channel_input_request_error(100_000, 2) == (
        "Channel-input mode requires at least 3 selected makers"
    )
