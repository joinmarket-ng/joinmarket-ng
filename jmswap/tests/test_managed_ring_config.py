"""Channel ring configuration wired from the managed node registry."""

from __future__ import annotations

from pathlib import Path

import pytest
from jmcore.channel_ring import ChannelRingConfig, ChannelRingNodeSettings, ChannelRingSettings
from jmcore.crypto import mnemonic_to_seed

from jmswap.lightning_context import channel_ring_config, wallet_identity_of
from jmswap.managed_nodes import (
    ManagedNodePaths,
    NodeState,
    RegistryStore,
    allocate_node,
    new_registry,
)

MNEMONIC = " ".join(["abandon"] * 11 + ["about"])
ONION = "a" * 56 + ".onion:9735"


def write_registry(data_dir: Path, *, states: tuple[NodeState, ...]) -> ManagedNodePaths:
    """A registry for MNEMONIC on signet; returns its paths."""
    identity = wallet_identity_of(mnemonic_to_seed(MNEMONIC, ""))
    paths = ManagedNodePaths.for_wallet(data_dir, "signet", identity)
    registry = new_registry("signet", identity)
    for mixdepth, state in enumerate(states):
        node = allocate_node(
            registry, mixdepth, birthday=1, ports=(21000 + 2 * mixdepth, 21001 + 2 * mixdepth)
        )
        node.state = state
        node.identity_pubkey = "02" + f"{mixdepth:02x}" * 32
        node.onion_endpoint = ONION
    RegistryStore(paths).update(lambda current: registry)
    return paths


def build(
    mode: str,
    ring: ChannelRingSettings,
    data_dir: Path,
    reserve_sats: tuple[int, int] | None = (25_000, 150_000),
) -> ChannelRingConfig:
    return channel_ring_config(
        mode,
        ring,
        mnemonic=MNEMONIC,
        passphrase="",
        data_dir=data_dir,
        network="signet",
        reserve_sats=reserve_sats,
    )


def test_managed_mode_maps_active_nodes_and_enables_rings(tmp_path: Path) -> None:
    paths = write_registry(tmp_path, states=(NodeState.ACTIVE, NodeState.RETIRING))
    registry = RegistryStore(paths).load()
    assert registry is not None

    config = build("managed", ChannelRingSettings(confirmation_depth=6), tmp_path)

    assert config.enabled
    assert config.confirmation_depth == 6
    assert config.mixdepth_nodes == {0: registry.nodes[0].name}
    assert set(config.nodes) == {registry.nodes[0].name, registry.nodes[1].name}
    assert config.node_binding_directory == paths.bindings_dir
    assert config.reserve_funding == (25_000, 150_000)


def test_a_zero_reserve_minimum_funds_no_reserve(tmp_path: Path) -> None:
    write_registry(tmp_path, states=(NodeState.ACTIVE,))

    config = build("managed", ChannelRingSettings(), tmp_path, reserve_sats=(0, 150_000))

    assert config.mixdepth_nodes and config.reserve_funding is None


def test_managed_mode_keeps_an_explicit_opt_out(tmp_path: Path) -> None:
    write_registry(tmp_path, states=(NodeState.ACTIVE,))

    config = build("managed", ChannelRingSettings(enabled=False), tmp_path)

    assert not config.enabled
    assert config.mixdepth_nodes


def test_managed_mode_without_nodes_leaves_rings_disabled(tmp_path: Path) -> None:
    config = build("managed", ChannelRingSettings(), tmp_path)

    assert not config.enabled and not config.nodes
    assert not (tmp_path / "lightning").exists()


def test_managed_mode_refuses_explicit_nodes(tmp_path: Path) -> None:
    ring = ChannelRingSettings(
        nodes={
            "mine": ChannelRingNodeSettings(
                lnd_grpc_url="https://127.0.0.1:10009",
                lnd_tls_cert_path=tmp_path / "tls.cert",
                lnd_macaroon_path=tmp_path / "admin.macaroon",
                onion_endpoint=ONION,
            )
        }
    )

    with pytest.raises(ValueError, match="lightning.mode = 'manual'"):
        build("managed", ring, tmp_path)


def test_unreadable_registry_is_a_config_error(tmp_path: Path) -> None:
    paths = write_registry(tmp_path, states=(NodeState.ACTIVE,))
    paths.registry.write_text("{}")

    with pytest.raises(ValueError, match="malformed or unsupported"):
        build("managed", ChannelRingSettings(), tmp_path)


@pytest.mark.parametrize("mode", ["off", "manual"])
def test_other_modes_use_the_explicit_settings(tmp_path: Path, mode: str) -> None:
    write_registry(tmp_path, states=(NodeState.ACTIVE,))

    config = build(mode, ChannelRingSettings(enabled=True), tmp_path)

    assert config.enabled and not config.nodes and not config.mixdepth_nodes
    # Only nodes JoinMarket manages get CoinJoin outputs as reserve.
    assert config.reserve_funding is None
