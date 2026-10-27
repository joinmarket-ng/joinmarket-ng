"""Managed payment admission and original-node history use distinct evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bitcointx.core.key import CKey

from jmswap.managed_nodes import (
    ManagedNodeError,
    ManagedNodePaths,
    NodeRegistry,
    NodeState,
    RegistryStore,
    allocate_node,
    new_registry,
)
from jmswap.managed_payments import ManagedPaymentSource

WALLET = "ab" * 32
OLD = bytes(CKey(b"\x11" * 32).pub).hex()
NEW = bytes(CKey(b"\x12" * 32).pub).hex()
OTHER = bytes(CKey(b"\x13" * 32).pub).hex()


@pytest.fixture
def source(tmp_path: Path) -> ManagedPaymentSource:
    store = RegistryStore(ManagedNodePaths.for_wallet(tmp_path, "regtest", WALLET))
    registry = new_registry("regtest", WALLET)
    for mixdepth, pubkey in ((0, OLD), (1, OTHER)):
        node = allocate_node(
            registry, mixdepth, birthday=1, ports=(20001 + mixdepth * 2, 20002 + mixdepth * 2)
        )
        node.state = NodeState.ACTIVE
        node.identity_pubkey = pubkey
    store.update(lambda current: registry)
    return ManagedPaymentSource(store, WALLET, 0)


def test_replacement_adoption_never_rebinds_original_history(source: ManagedPaymentSource) -> None:
    original = source.active_endpoint()

    def rotate(registry: NodeRegistry | None) -> NodeRegistry:
        assert registry is not None
        registry.nodes[0].state = NodeState.RETIRING
        replacement = allocate_node(registry, 0, birthday=2, ports=(20005, 20006))
        replacement.state, replacement.identity_pubkey = NodeState.ACTIVE, NEW
        return registry

    source.store.update(rotate)
    replacement = source.active_endpoint()
    assert replacement.identity_pubkey == NEW and replacement.rpc_port == 20006
    assert source.historical_endpoint(OLD) == original
    assert source.historical_endpoint(NEW) == replacement
    assert original.rpc_port == 20002 and original.tls_cert != replacement.tls_cert


@pytest.mark.parametrize(
    "state", [NodeState.PROVISIONING, NodeState.RETIRING, NodeState.RECOVERING, NodeState.RETIRED]
)
def test_history_remains_available_without_an_active_source(
    source: ManagedPaymentSource,
    state: NodeState,
) -> None:
    registry = source.store.load()
    assert registry is not None
    registry.nodes[0].state = state
    source.store.update(lambda current: registry)
    fresh = ManagedPaymentSource(source.store, WALLET, 0)
    before = source.store.paths.registry.read_bytes()
    with pytest.raises(ManagedNodeError, match="no active"):
        fresh.active_endpoint()
    assert fresh.historical_endpoint(OLD).identity_pubkey == OLD
    with pytest.raises(ManagedNodeError, match="unavailable or ambiguous"):
        fresh.historical_endpoint(OTHER)
    assert source.store.paths.registry.read_bytes() == before


@pytest.mark.parametrize("unknown", ["absent", "malformed", "unsupported", "foreign", "network"])
def test_unknown_metadata_refuses_new_payments_but_preserves_a_trusted_snapshot(
    source: ManagedPaymentSource,
    unknown: str,
) -> None:
    original = source.active_endpoint()
    path = source.store.paths.registry
    document = json.loads(path.read_text())
    if unknown == "absent":
        path.unlink()
    elif unknown == "malformed":
        path.write_text("{broken")
    else:
        if unknown == "unsupported":
            document["version"] = 2
        elif unknown == "foreign":
            document["wallet_identity"] = "ef" * 32
        else:
            document["network"] = "signet"
        path.write_text(json.dumps(document))
    with pytest.raises(ManagedNodeError):
        source.active_endpoint()
    assert source.historical_endpoint(OLD) == original
    # A fresh process has no authoritative endpoint if metadata is lost.
    with pytest.raises(ManagedNodeError):
        ManagedPaymentSource(source.store, WALLET, 0).historical_endpoint(OLD)


def test_ambiguous_identities_never_authorize_or_select_history(
    source: ManagedPaymentSource,
) -> None:
    registry = source.store.load()
    assert registry is not None
    registry.nodes[0].state = NodeState.RETIRING
    duplicate = allocate_node(registry, 0, birthday=2, ports=(20005, 20006))
    duplicate.state, duplicate.identity_pubkey = NodeState.ACTIVE, OLD
    source.store.update(lambda current: registry)
    with pytest.raises(ManagedNodeError, match="not unique"):
        source.active_endpoint()
    with pytest.raises(ManagedNodeError, match="unavailable or ambiguous"):
        source.historical_endpoint(OLD)


def test_a_cached_identity_binding_is_never_overwritten(source: ManagedPaymentSource) -> None:
    original = source.active_endpoint()
    registry = source.store.load()
    assert registry is not None
    registry.nodes[0].rpc_port = 21002
    source.store.update(lambda current: registry)
    with pytest.raises(ManagedNodeError, match="binding changed"):
        source.active_endpoint()
    assert source.historical_endpoint(OLD) == original


def test_partial_active_identity_cannot_authorize_payment(source: ManagedPaymentSource) -> None:
    registry = source.store.load()
    assert registry is not None
    registry.nodes[0].identity_pubkey = None
    source.store.update(lambda current: registry)
    with pytest.raises(ManagedNodeError, match="no active"):
        source.active_endpoint()
