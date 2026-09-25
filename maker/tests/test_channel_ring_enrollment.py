"""Enrollment is explicit and never starts a maker, sync, or channel operation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from jmcore.channel_ring import ChannelRingNodeSettings, ChannelRingSettings
from jmcore.models import NetworkType, OfferType
from jmcore.settings import JoinMarketSettings, MakerSettings, TakerSettings, WalletSettings
from typer.testing import CliRunner

from maker import cli

NODE = "0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
MNEMONIC = "abandon " * 11 + "about"


def settings(tmp_path: Path) -> JoinMarketSettings:
    ring = ChannelRingSettings(
        enabled=True,
        nodes={
            "local": ChannelRingNodeSettings(
                lnd_grpc_url="127.0.0.1:10009",
                lnd_tls_cert_path=tmp_path / "cert",
                lnd_macaroon_path=tmp_path / "macaroon",
                onion_endpoint="a" * 56 + ".onion:9735",
            )
        },
        mixdepth_nodes={0: "local"},
        node_binding_directory=tmp_path / "bindings",
    )
    result = JoinMarketSettings(
        wallet=WalletSettings(address_type="p2tr"),
        maker=MakerSettings(offer_type="tr0absoffer", channel_ring=ring),
        taker=TakerSettings(preferred_offer_type=OfferType.TR0_ABSOLUTE, channel_ring=ring),
    )
    result.network_config.network = NetworkType.REGTEST
    return result


@pytest.mark.parametrize("component", ["maker", "taker"])
def test_enrollment_loads_correct_settings_without_starting_runtime(
    component: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = settings(tmp_path)
    monkeypatch.setattr(cli, "setup_cli", lambda *args, **kwargs: configured)
    monkeypatch.setattr("jmcore.process_hardening.harden_current_process", lambda: None)
    monkeypatch.setattr(
        cli,
        "resolve_mnemonic",
        lambda *args, **kwargs: SimpleNamespace(
            mnemonic=MNEMONIC,
            bip39_passphrase="",
        ),
    )
    start = Mock(side_effect=AssertionError("enrollment must not start a maker"))
    monkeypatch.setattr(cli, "MakerBot", start)
    wallet = Mock(side_effect=AssertionError("enrollment must not sync or allocate wallet state"))
    monkeypatch.setattr(cli, "create_wallet_service", wallet)
    enroll = AsyncMock(return_value=(object(),))
    monkeypatch.setattr("jmswap.channel_ring_nodes.enroll_configured_ring_nodes", enroll)

    result = CliRunner().invoke(
        cli.app,
        [
            "enroll-ring-nodes",
            "--component",
            component,
            "--expected-node",
            f"local={NODE}",
            "--acknowledge-prior-use",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "no channels were changed" in result.output
    enroll.assert_awaited_once()
    args, kwargs = enroll.call_args
    assert args[0].mixdepth_nodes == {0: "local"}
    assert args[0].node_binding_directory == tmp_path / "bindings"
    assert kwargs["expected_node_ids"] == {"local": NODE}
    assert kwargs["network"] == "regtest"
    assert kwargs["offer_type"] == "tr0absoffer"
    assert len(kwargs["wallet_identity"]) == 64
    assert kwargs["acknowledge_prior_use"] is True
    start.assert_not_called()
    wallet.assert_not_called()


def test_missing_acknowledgment_rejects_before_loading_wallet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = Mock(side_effect=AssertionError("must reject before loading settings or secrets"))
    monkeypatch.setattr(cli, "setup_cli", setup)
    result = CliRunner().invoke(
        cli.app,
        [
            "enroll-ring-nodes",
            "--component",
            "maker",
            "--expected-node",
            f"local={NODE}",
        ],
    )
    assert result.exit_code != 0
    assert "acknowledge" in result.output
    setup.assert_not_called()


def test_duplicate_expected_names_are_not_silently_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = Mock(side_effect=AssertionError("must reject ambiguous enrollment"))
    monkeypatch.setattr(cli, "setup_cli", setup)
    result = CliRunner().invoke(
        cli.app,
        [
            "enroll-ring-nodes",
            "--component",
            "maker",
            "--expected-node",
            f"local={NODE}",
            "--expected-node",
            f"local={NODE}",
            "--acknowledge-prior-use",
        ],
    )
    assert result.exit_code != 0
    assert "unique" in result.output
    setup.assert_not_called()
