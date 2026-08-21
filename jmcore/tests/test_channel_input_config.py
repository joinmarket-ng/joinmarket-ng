from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from jmcore.channel_input_config import ChannelInputConfig, ChannelInputSettings

VALID_CHANNEL_POINT = "a" * 64 + ":0"
VALID_PEER_KEY = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"


def enabled_settings(tmp_path: Path, **overrides: object) -> ChannelInputSettings:
    values: dict[str, object] = {
        "enabled": True,
        "lnd_grpc_url": "https://127.0.0.1:10009",
        "lnd_tls_cert_path": tmp_path / "tls.cert",
        "lnd_macaroon_path": tmp_path / "admin.macaroon",
        "lnd_escrow_macaroon_path": tmp_path / "channel-escrow.macaroon",
        "allowed_channel_points": [VALID_CHANNEL_POINT],
        "allowed_peer_node_pubkeys": [VALID_PEER_KEY],
        "minimum_makers": 5,
        "min_channel_capacity_sat": 4_000_000,
        "max_channel_capacity_sat": 50_000_000,
        "max_claim_adjustment_sat": 500_000,
        "max_miner_fee_share_sat": 600_000,
        "max_parent_fee_sat": 700_000,
        "max_fee_rate_sat_vb": 900,
        "recovery_delay_blocks": 24,
        "phase_timeout_seconds": 180.0,
        "max_active_sessions": 6,
        "persistence_directory": tmp_path / "channel-input-state",
    }
    values.update(overrides)
    return ChannelInputSettings(**values)


def test_disabled_defaults_are_inert_and_persistent_path_is_derived(tmp_path: Path) -> None:
    config = ChannelInputConfig()

    assert config.enabled is False
    assert config.lnd_grpc_url == ""
    assert config.persistence_path(tmp_path) == tmp_path / "channel-input"


def test_settings_convert_all_lists_to_immutable_runtime_tuples(tmp_path: Path) -> None:
    settings = enabled_settings(tmp_path)
    config = ChannelInputConfig.from_settings(settings)

    assert config.model_dump() == {
        **settings.model_dump(),
        "allowed_channel_points": (VALID_CHANNEL_POINT,),
        "allowed_peer_node_pubkeys": (VALID_PEER_KEY,),
    }
    assert config.persistence_path(tmp_path) == tmp_path / "channel-input-state"


def test_enabled_documented_defaults_form_a_valid_policy(tmp_path: Path) -> None:
    config = ChannelInputConfig.from_settings(
        ChannelInputSettings(
            enabled=True,
            lnd_grpc_url="https://127.0.0.1:10009",
            lnd_tls_cert_path=tmp_path / "tls.cert",
            lnd_macaroon_path=tmp_path / "admin.macaroon",
            lnd_escrow_macaroon_path=tmp_path / "channel-escrow.macaroon",
            allowed_channel_points=[VALID_CHANNEL_POINT],
        )
    )

    assert config.max_claim_adjustment_sat == config.min_channel_capacity_sat // 2
    assert config.max_miner_fee_share_sat == config.min_channel_capacity_sat // 2


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"lnd_grpc_url": "http://127.0.0.1:10009"}, "TLS loopback"),
        ({"lnd_grpc_url": "https://lnd.example:10009"}, "private loopback"),
        ({"lnd_tls_cert_path": None}, "lnd_tls_cert_path"),
        ({"lnd_macaroon_path": None}, "lnd_macaroon_path"),
        ({"lnd_escrow_macaroon_path": None}, "lnd_escrow_macaroon_path"),
        ({"allowed_channel_points": []}, "allowed channel point"),
        ({"allowed_channel_points": ["A" * 64 + ":0"]}, "lowercase"),
        ({"allowed_channel_points": ["a" * 64 + ":4294967296"]}, "uint32"),
        ({"allowed_channel_points": [VALID_CHANNEL_POINT, VALID_CHANNEL_POINT]}, "unique"),
        ({"allowed_peer_node_pubkeys": ["02" + "00" * 32]}, "valid secp256k1"),
        ({"allowed_peer_node_pubkeys": [VALID_PEER_KEY, VALID_PEER_KEY]}, "unique"),
        (
            {"min_channel_capacity_sat": 60_000_000, "max_channel_capacity_sat": 50_000_000},
            "minimum channel",
        ),
        ({"max_channel_capacity_sat": 100_000_001}, "conservative 1 BTC"),
        ({"max_claim_adjustment_sat": 2_000_001}, "half minimum"),
        ({"max_miner_fee_share_sat": 2_000_001}, "half minimum"),
    ],
)
def test_enabled_invalid_configuration_is_rejected(
    tmp_path: Path, overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        ChannelInputConfig.from_settings(enabled_settings(tmp_path, **overrides))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_makers", 2),
        ("minimum_makers", 21),
        ("max_parent_fee_sat", 0),
        ("max_fee_rate_sat_vb", 0),
        ("max_fee_rate_sat_vb", 10_001),
        ("recovery_delay_blocks", 5),
        ("recovery_delay_blocks", 2017),
        ("phase_timeout_seconds", 9.9),
        ("phase_timeout_seconds", 600.1),
        ("max_active_sessions", 0),
        ("max_active_sessions", 17),
    ],
)
def test_policy_field_bounds_are_rejected(field: str, value: int | float) -> None:
    with pytest.raises(ValidationError):
        ChannelInputSettings(**{field: value})
