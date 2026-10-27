"""Managed node registry: allocation, persistence safety, and ring wiring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jmswap.managed_nodes import (
    ManagedNodeError,
    ManagedNodePaths,
    NodeRegistry,
    NodeState,
    RegistryError,
    RegistryStore,
    allocate_node,
    new_registry,
    ring_nodes,
)

WALLET = "ab" * 32
ONION = "a" * 56 + ".onion:9735"


def store_at(tmp_path: Path) -> RegistryStore:
    return RegistryStore(ManagedNodePaths.for_wallet(tmp_path, "signet", WALLET))


def test_full_wallet_owner_is_checked_even_when_path_prefixes_collide(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.update(lambda current: two_node_registry())
    other_identity = WALLET[:16] + "cd" * 24
    other_paths = ManagedNodePaths.for_wallet(tmp_path, "signet", other_identity)
    assert other_paths.root == store.paths.root
    with pytest.raises(RegistryError, match="different wallet"):
        RegistryStore(other_paths).load()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("scope", ["network", "wallet"])
def test_update_never_writes_foreign_authority(
    tmp_path: Path,
    existing: bool,
    scope: str,
) -> None:
    store = store_at(tmp_path)
    if existing:
        store.update(lambda current: two_node_registry())
    before = store.paths.registry.read_bytes() if existing else None
    foreign = two_node_registry()
    if scope == "network":
        foreign.network = "regtest"
    else:
        foreign.wallet_identity = "cd" * 32
    with pytest.raises(RegistryError, match="another network|different wallet"):
        store.update(lambda current: foreign)
    if before is None:
        assert not store.paths.registry.exists()
    else:
        assert store.paths.registry.read_bytes() == before


def two_node_registry() -> NodeRegistry:
    registry = new_registry("signet", WALLET)
    allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    allocate_node(registry, 1, birthday=1, ports=(20003, 20004))
    return registry


def test_paths_are_scoped_by_network_and_wallet(tmp_path: Path) -> None:
    signet = ManagedNodePaths.for_wallet(tmp_path, "signet", WALLET)
    mainnet = ManagedNodePaths.for_wallet(tmp_path, "mainnet", WALLET)
    other = ManagedNodePaths.for_wallet(tmp_path, "signet", "cd" * 32)

    assert len({signet.root, mainnet.root, other.root}) == 3
    assert signet.root == tmp_path / "lightning" / "signet" / WALLET[:16]


def test_allocation_uses_the_epoch_and_never_reuses_a_generation() -> None:
    registry = two_node_registry()
    first = registry.nodes[0]
    assert (first.epoch, first.generation, first.state) == (registry.epoch, 0, "provisioning")
    assert first.name == f"jm-{registry.epoch}-m0-g0"

    with pytest.raises(ManagedNodeError, match="already has an operational node"):
        allocate_node(registry, 0, birthday=2)

    first.state = NodeState.RETIRED
    second = allocate_node(registry, 0, birthday=2, ports=(20001, 20002))
    assert second.generation == 1


def test_fresh_registries_never_share_an_epoch() -> None:
    assert new_registry("signet", WALLET).epoch != new_registry("signet", WALLET).epoch


def test_generation_exhaustion_is_an_error() -> None:
    registry = new_registry("signet", WALLET)
    node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    node.generation = 999
    node.state = NodeState.RETIRED

    with pytest.raises(ManagedNodeError, match="exhausted"):
        allocate_node(registry, 0, birthday=1, ports=(20003, 20004))


def test_allocated_ports_avoid_live_nodes() -> None:
    registry = new_registry("signet", WALLET)
    first = allocate_node(registry, 0, birthday=1)
    second = allocate_node(registry, 1, birthday=1)

    ports = {first.p2p_port, first.rpc_port, second.p2p_port, second.rpc_port}
    assert len(ports) == 4


@pytest.mark.parametrize(
    "change",
    [
        lambda nodes: nodes[1].__setattr__("mixdepth", 0),
        lambda nodes: nodes[1].__setattr__("rpc_port", nodes[0].p2p_port),
    ],
    ids=["two operational nodes on a mixdepth", "shared port"],
)
def test_inconsistent_registries_are_rejected(change: object) -> None:
    registry = two_node_registry()
    change(registry.nodes)  # type: ignore[operator]

    with pytest.raises(ValueError):
        NodeRegistry.model_validate(registry.model_dump())


def test_missing_registry_loads_as_none_and_update_persists(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert store.load() is None

    store.update(lambda current: two_node_registry())

    loaded = store.load()
    assert loaded is not None and len(loaded.nodes) == 2
    backup = json.loads((store.paths.backup_dir / "registry.json").read_text())
    assert backup == json.loads(store.paths.registry.read_text())
    assert store.paths.registry.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize(
    "raw",
    [b"{not json", b'{"version": 2}', b"[]", b'{"version": 1, "network": "signet"}'],
    ids=["malformed", "future version", "not an object", "incomplete"],
)
def test_unreadable_registry_is_refused_and_left_untouched(tmp_path: Path, raw: bytes) -> None:
    store = store_at(tmp_path)
    store.paths.root.mkdir(parents=True)
    store.paths.registry.write_bytes(raw)
    store.paths.registry.chmod(0o600)

    with pytest.raises(RegistryError):
        store.load()
    with pytest.raises(RegistryError):
        store.update(lambda current: two_node_registry())
    assert store.paths.registry.read_bytes() == raw


def test_registry_of_another_network_is_refused(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.update(lambda current: two_node_registry())
    mainnet = RegistryStore(
        ManagedNodePaths(store.paths.root, "mainnet", store.paths.wallet_identity)
    )

    with pytest.raises(RegistryError, match="another network"):
        mainnet.load()


def test_failed_update_keeps_the_previous_registry(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.update(lambda current: two_node_registry())
    before = store.paths.registry.read_bytes()

    def broken(current: NodeRegistry | None) -> NodeRegistry:
        assert current is not None
        current.nodes[1].mixdepth = 0
        return current

    with pytest.raises(ValueError):
        store.update(broken)
    assert store.paths.registry.read_bytes() == before


def test_only_one_command_runs_the_processes(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with (
        store.exclusive_processes(),
        pytest.raises(ManagedNodeError, match="another jm-lightning command"),
        store_at(tmp_path).exclusive_processes(),
    ):
        pass
    with store.exclusive_processes():
        pass


def test_ring_nodes_map_only_active_nodes_but_keep_retiring_ones(tmp_path: Path) -> None:
    paths = ManagedNodePaths.for_wallet(tmp_path, "signet", WALLET)
    registry = two_node_registry()
    for node in registry.nodes:
        node.identity_pubkey = "02" + "11" * 32
        node.onion_endpoint = ONION
    registry.nodes[0].state = NodeState.ACTIVE
    registry.nodes[1].state = NodeState.RETIRING
    replacement = allocate_node(registry, 1, birthday=2, ports=(20005, 20006))

    nodes, mapping = ring_nodes(registry, paths)

    assert set(nodes) == {registry.nodes[0].name, registry.nodes[1].name}
    assert mapping == {0: registry.nodes[0].name}
    assert replacement.name not in nodes
    settings = nodes[registry.nodes[0].name]
    assert settings.lnd_grpc_url == "https://127.0.0.1:20002"
    assert settings.lnd_macaroon_path == paths.macaroon(registry.nodes[0])


def test_wallet_info_lines_flag_low_reserves_and_stopped_nodes() -> None:
    from jmswap.managed_status import NodeStatus, mixdepth_lines

    def row(mixdepth: int, **changes: object) -> NodeStatus:
        base: dict[str, object] = {
            "name": f"n{mixdepth}",
            "mixdepth": mixdepth,
            "generation": 0,
            "state": NodeState.ACTIVE,
            "identity_pubkey": "02" + f"{mixdepth:02x}" * 32,
            "onion_endpoint": ONION,
            "running": True,
            "synced": True,
            "confirmed_sats": 30_000,
            "channels": 2,
            "outbound_sats": 1_000,
            "inbound_sats": 2_000,
            "reserve_ok": True,
        }
        return NodeStatus.model_validate({**base, **changes})

    lines = mixdepth_lines(
        [row(1, confirmed_sats=5_000, reserve_ok=False), row(0), row(2, running=False)],
        25_000,
    )

    assert lines[0] == (
        "  Mixdepth 0: 020000000000  reserve 30,000, 2 channels, out 1,000, in 2,000 sats"
    )
    assert lines[1].endswith("(reserve below 25,000)")
    assert lines[2] == "  Mixdepth 2: 020202020202  not running"
    assert lines[3] == "  Start the nodes with `jm-lightning run`"
