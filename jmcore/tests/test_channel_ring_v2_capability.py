from __future__ import annotations

import pytest

from jmcore.protocol import ALL_FEATURES, FEATURE_COFUNDED_CHANNEL_RING_V2, FeatureSet


def test_private_ring_v2_is_a_known_feature() -> None:
    assert FEATURE_COFUNDED_CHANNEL_RING_V2 in ALL_FEATURES


@pytest.mark.parametrize("value", [False, 0, 1, "true", None])
def test_private_ring_v2_handshake_requires_a_literal_true(value: object) -> None:
    parsed = FeatureSet.from_handshake({"features": {FEATURE_COFUNDED_CHANNEL_RING_V2: value}})

    assert FEATURE_COFUNDED_CHANNEL_RING_V2 not in parsed


def test_private_ring_v2_handshake_accepts_a_literal_true() -> None:
    parsed = FeatureSet.from_handshake({"features": {FEATURE_COFUNDED_CHANNEL_RING_V2: True}})

    assert parsed.features == {FEATURE_COFUNDED_CHANNEL_RING_V2}
