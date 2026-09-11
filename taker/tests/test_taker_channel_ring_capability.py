from __future__ import annotations

from types import SimpleNamespace

from jmcore.models import Offer, OfferType
from jmcore.protocol import (
    FEATURE_CHANNEL_INPUT_V1,
    FEATURE_COFUNDED_CHANNEL_RING_V1,
    FEATURE_COFUNDED_CHANNEL_RING_V2,
)

from taker.coinjoin_session import CoinJoinSession
from taker.orderbook import (
    offer_supports_channel_input_v1,
    offer_supports_cofunded_channel_ring_v1,
    offer_supports_cofunded_channel_ring_v2,
)
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


def test_taker_recognizes_private_ring_capability_only_when_literal_true() -> None:
    private = offer(supported=False).model_copy(
        update={"features": {FEATURE_COFUNDED_CHANNEL_RING_V2: True}}
    )
    assert offer_supports_cofunded_channel_ring_v2(private)
    for invalid_value in (False, 1, "true"):
        invalid = private.model_copy(
            update={"features": {FEATURE_COFUNDED_CHANNEL_RING_V2: invalid_value}}
        )
        assert not offer_supports_cofunded_channel_ring_v2(invalid)


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


def test_private_ring_selection_requires_v2_without_v1_fallback() -> None:
    taker = Taker.__new__(Taker)
    taker._session = CoinJoinSession()
    taker.config = SimpleNamespace(
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
        channel_ring=SimpleNamespace(enabled=True, private_coordination=True),
    )
    v2_offer = offer(supported=False).model_copy(
        update={"counterparty": "v2", "features": {FEATURE_COFUNDED_CHANNEL_RING_V2: True}}
    )
    v1_offer = offer(supported=True).model_copy(update={"counterparty": "v1"})
    unknown_offer = offer(supported=False).model_copy(update={"counterparty": "unknown"})

    filtered, error = taker._ring_filter_offers([v2_offer, v1_offer, unknown_offer], 1)
    assert filtered == [v2_offer]
    assert error is None
    prepared, required_features = taker._prepare_ring_offer_selection([v2_offer], None, 1)
    assert prepared == [v2_offer]
    assert required_features == {FEATURE_COFUNDED_CHANNEL_RING_V2}

    filtered, error = taker._ring_filter_offers([v2_offer, v1_offer], 2)
    assert filtered == [v2_offer]
    assert error is not None
    assert FEATURE_COFUNDED_CHANNEL_RING_V2 in error


def test_legacy_ring_selection_and_disabled_ring_behavior_are_unchanged() -> None:
    legacy = Taker.__new__(Taker)
    legacy._session = CoinJoinSession()
    legacy.config = SimpleNamespace(
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
        channel_ring=SimpleNamespace(enabled=True, private_coordination=False),
    )
    v1_offer = offer(supported=True)
    v2_offer = offer(supported=False).model_copy(
        update={"counterparty": "v2", "features": {FEATURE_COFUNDED_CHANNEL_RING_V2: True}}
    )

    filtered, error = legacy._ring_filter_offers([v1_offer, v2_offer], 1)
    assert filtered == [v1_offer]
    assert error is None
    _prepared, required_features = legacy._prepare_ring_offer_selection([v1_offer], None, 1)
    assert required_features == {FEATURE_COFUNDED_CHANNEL_RING_V1}

    disabled = Taker.__new__(Taker)
    disabled._session = CoinJoinSession()
    disabled.config = SimpleNamespace(
        preferred_offer_type=OfferType.TR0_ABSOLUTE,
        channel_ring=SimpleNamespace(enabled=False, private_coordination=True),
    )
    filtered, error = disabled._ring_filter_offers([v1_offer, v2_offer], 2)
    assert filtered == [v1_offer, v2_offer]
    assert error is None
    _prepared, required_features = disabled._prepare_ring_offer_selection(
        [v1_offer, v2_offer], None, 2
    )
    assert required_features is None
