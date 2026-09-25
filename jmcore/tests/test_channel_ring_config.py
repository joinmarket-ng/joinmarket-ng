from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from jmcore.channel_ring import ChannelRingConfig, ChannelRingSettings
from jmcore.protocol import (
    FEATURE_COFUNDED_CHANNEL_RING_V1,
    FEATURE_PEERLIST_FEATURES,
    FEATURE_PING,
    FeatureSet,
    create_handshake_request,
)


def enabled_values() -> dict[str, object]:
    return {
        "enabled": True,
        "lnd_grpc_url": "https://127.0.0.1:10009",
        "lnd_tls_cert_path": Path("/tmp/lnd/tls.cert"),
        "lnd_macaroon_path": Path("/tmp/lnd/admin.macaroon"),
        "onion_endpoint": "a" * 56 + ".onion:9735",
    }


def test_only_one_cofunded_ring_feature_is_defined() -> None:
    import jmcore.protocol as protocol

    cofunded_features = {
        value
        for name, value in vars(protocol).items()
        if name.startswith("FEATURE_") and isinstance(value, str) and "channel" in value
    }
    assert cofunded_features == {"cofunded_channel_ring_v1"}
    assert FEATURE_COFUNDED_CHANNEL_RING_V1 == "cofunded_channel_ring_v1"
    for forbidden in (
        "channel_change",
        "channel_ring",
        "combine",
        "single-funder",
        "pool",
        "knapsack",
        "fallback",
    ):
        assert forbidden not in cofunded_features


def test_capability_parser_recognizes_exact_feature() -> None:
    parsed = FeatureSet.from_handshake(
        {"features": {FEATURE_COFUNDED_CHANNEL_RING_V1: True, "channel_ring": False}}
    )
    assert parsed.features == {FEATURE_COFUNDED_CHANNEL_RING_V1}


def test_disabled_handshake_is_byte_for_byte_unchanged_and_has_no_ring_feature() -> None:
    features = FeatureSet(features={FEATURE_PEERLIST_FEATURES, FEATURE_PING})
    payload = create_handshake_request(
        nick="J5disabled",
        location="NOT-SERVING-ONION",
        network="regtest",
        features=features,
    )
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    assert encoded == (
        b'{"app-name":"joinmarket","directory":false,'
        b'"location-string":"NOT-SERVING-ONION","proto-ver":5,'
        b'"features":{"peerlist_features":true,"ping":true},'
        b'"nick":"J5disabled","network":"regtest"}'
    )
    assert FEATURE_COFUNDED_CHANNEL_RING_V1.encode() not in encoded


def test_runtime_config_is_strict_but_settings_coerce_environment_values() -> None:
    with pytest.raises(ValidationError, match="valid boolean"):
        ChannelRingConfig(enabled="false")  # type: ignore[arg-type]
    settings = ChannelRingSettings(enabled="false", max_active_sessions="4")  # type: ignore[arg-type]
    config = ChannelRingConfig.from_settings(settings)
    assert config.enabled is False
    assert config.max_active_sessions == 4


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"lnd_grpc_url": ""}, "lnd_grpc_url"),
        ({"lnd_tls_cert_path": None}, "lnd_tls_cert_path"),
        ({"lnd_macaroon_path": None}, "lnd_macaroon_path"),
        ({"onion_endpoint": ""}, "onion_endpoint"),
        ({"lnd_grpc_url": "https://shared.example:10009"}, "loopback"),
        ({"onion_endpoint": "127.0.0.1:9735"}, "v3 onion"),
        (
            {"onion_endpoint": "02" + "11" * 32 + "@" + "a" * 56 + ".onion:9735"},
            "without a node ID",
        ),
        ({"taproot_commitment_overhead": 659}, "fixed at 660"),
        ({"max_channel_capacity": 100_000_001}, "1 BTC cap"),
        ({"maximum_commitment_fee": 1_000_001}, "anti-grief cap"),
        ({"max_verified_sessions": 5}, "cannot exceed"),
    ],
)
def test_enabled_configuration_fails_closed(change: dict[str, object], message: str) -> None:
    values = enabled_values()
    values.update(change)
    with pytest.raises(ValidationError, match=message):
        ChannelRingConfig(**values)


def test_enabled_configuration_accepts_conservative_policy() -> None:
    config = ChannelRingConfig(**enabled_values())
    assert config.enabled
    assert config.allowed_csv_delays == (72, 144, 288, 432)
    assert config.persistence_path(Path("/data")) == Path("/data/channel-ring")
