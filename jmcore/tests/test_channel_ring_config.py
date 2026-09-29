from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jmcore.channel_ring import (
    RING_SETUP_HOLD_SLACK_SECONDS,
    ChannelRingConfig,
    ChannelRingSettings,
)
from jmcore.protocol import (
    FEATURE_PEERLIST_FEATURES,
    FEATURE_PING,
    FEATURE_PRIVATE_CHANNEL_RING,
    FeatureSet,
    create_handshake_request,
)


def enabled_values() -> dict[str, Any]:
    return {
        "enabled": True,
        "nodes": {
            "md0": {
                "lnd_grpc_url": "https://127.0.0.1:10009",
                "lnd_tls_cert_path": Path("/tmp/lnd/tls.cert"),
                "lnd_macaroon_path": Path("/tmp/lnd/admin.macaroon"),
                "onion_endpoint": "a" * 56 + ".onion:9735",
            }
        },
        "mixdepth_nodes": {0: "md0"},
        "node_binding_directory": Path("/tmp/node-bindings"),
    }


def test_only_one_private_channel_ring_feature_is_defined() -> None:
    import jmcore.protocol as protocol

    channel_ring_features = {
        value
        for name, value in vars(protocol).items()
        if name.startswith("FEATURE_") and isinstance(value, str) and "channel" in value
    }
    assert channel_ring_features == {"private_channel_ring"}
    assert FEATURE_PRIVATE_CHANNEL_RING == "private_channel_ring"
    for forbidden in (
        "channel_change",
        "channel_ring",
        "combine",
        "single-funder",
        "pool",
        "knapsack",
        "fallback",
    ):
        assert forbidden not in channel_ring_features


def test_capability_parser_recognizes_exact_feature() -> None:
    parsed = FeatureSet.from_handshake(
        {"features": {FEATURE_PRIVATE_CHANNEL_RING: True, "channel_ring": False}}
    )
    assert parsed.features == {FEATURE_PRIVATE_CHANNEL_RING}


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
    assert FEATURE_PRIVATE_CHANNEL_RING.encode() not in encoded


def test_runtime_config_is_strict_but_settings_coerce_environment_values() -> None:
    with pytest.raises(ValidationError, match="valid boolean"):
        ChannelRingConfig(enabled="false")  # type: ignore[arg-type]
    settings = ChannelRingSettings(enabled="false", max_active_sessions="4")  # type: ignore[arg-type]
    config = ChannelRingConfig.from_settings(settings)
    assert config.enabled is False
    assert config.max_active_sessions == 4


def test_named_node_settings_convert_toml_mixdepth_keys() -> None:
    import tomllib

    settings = ChannelRingSettings.model_validate(
        tomllib.loads("""
enabled = true
mixdepth_nodes = { 0 = "md0" }
node_binding_directory = "/tmp/bindings"
[nodes.md0]
lnd_grpc_url = "127.0.0.1:10009"
lnd_tls_cert_path = "/tmp/lnd/tls.cert"
lnd_macaroon_path = "/tmp/lnd/admin.macaroon"
onion_endpoint = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:9735"
""")
    )
    config = ChannelRingConfig.from_settings(settings)
    assert config.mixdepth_nodes == {0: "md0"}
    assert config.nodes["md0"].lnd_tls_cert_path == Path("/tmp/lnd/tls.cert")


@pytest.mark.parametrize("mapping", [{0: "missing"}, {0: "md0", 1: "md0"}, {-1: "md0"}])
def test_enabled_mapping_must_be_explicit_distinct_and_valid(mapping: dict[int, str]) -> None:
    with pytest.raises(ValidationError):
        ChannelRingConfig(**(enabled_values() | {"mixdepth_nodes": mapping}))


def test_taker_participation_defaults_to_local_node_and_opt_out_needs_no_lnd() -> None:
    configured = ChannelRingConfig(**enabled_values())
    assert configured.taker_joins
    opted_out = ChannelRingConfig(**(enabled_values() | {"taker_participates": False}))
    assert not opted_out.taker_joins
    without_lnd = ChannelRingConfig(enabled=True)
    assert not without_lnd.taker_joins
    assert not ChannelRingConfig.from_settings(ChannelRingSettings(enabled=True)).taker_joins
    with pytest.raises(ValidationError, match="participating taker requires"):
        ChannelRingConfig(enabled=True, taker_participates=True)


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
    if any(key.startswith("lnd_") or key == "onion_endpoint" for key in change):
        values["nodes"]["md0"].update(change)
    else:
        values.update(change)
    with pytest.raises(ValidationError, match=message):
        ChannelRingConfig(**values)


def test_enabled_configuration_accepts_conservative_policy() -> None:
    config = ChannelRingConfig(**enabled_values())
    assert config.enabled
    assert config.allowed_csv_delays == (72, 144, 288, 432)
    assert config.setup_timeout_seconds == 600.0
    assert config.maker_setup_hold_seconds == 660.0
    assert config.maker_setup_hold_seconds == (
        config.setup_timeout_seconds
        + config.hold_safety_margin_seconds
        + RING_SETUP_HOLD_SLACK_SECONDS
    )
    assert config.persistence_path(Path("/data")) == Path("/data/channel-ring")


def test_two_capable_makers_is_the_minimum_when_taker_joins() -> None:
    settings = ChannelRingSettings(**enabled_values(), minimum_makers=2)
    config = ChannelRingConfig.from_settings(settings)
    assert config.minimum_makers == 2
    assert ChannelRingSettings(**enabled_values()).minimum_makers == 2
    assert (
        ChannelRingConfig.from_settings(ChannelRingSettings(**enabled_values())).minimum_makers == 2
    )
    assert ChannelRingConfig().minimum_makers == 2
    with pytest.raises(ValidationError, match="greater than or equal to 2"):
        ChannelRingSettings(minimum_makers=1)


def test_default_setup_hold_tracks_timeout_and_margin() -> None:
    config = ChannelRingConfig(
        **enabled_values(), setup_timeout_seconds=700.0, hold_safety_margin_seconds=45.0
    )

    assert config.maker_setup_hold_seconds == 700.0 + 45.0 + RING_SETUP_HOLD_SLACK_SECONDS


def test_setup_hold_without_slack_is_rejected() -> None:
    with pytest.raises(ValidationError, match="ring setup hold slack"):
        ChannelRingConfig(
            **enabled_values(),
            maker_setup_hold_seconds=600.0 + 30.0,
        )


@pytest.mark.parametrize("setup_timeout_seconds", [0, 1_801])
def test_setup_timeout_is_positive_and_bounded(setup_timeout_seconds: int) -> None:
    with pytest.raises(ValidationError):
        ChannelRingConfig(setup_timeout_seconds=setup_timeout_seconds)
