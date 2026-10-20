"""Managed node wallet creation, unlocking, and the no-reinitialization rule."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from jmcore.lightning_keys import WalletLightningKeys
from mnemonic import Mnemonic

from jmswap.lnd_config import LndEnvironment
from jmswap.managed_lnd import LOCKED, NON_EXISTING, SERVER_ACTIVE, LndProcessError, ManagedLnd
from jmswap.managed_nodes import (
    ManagedNode,
    ManagedNodeError,
    ManagedNodePaths,
    NodeState,
    RegistryStore,
    allocate_node,
    new_registry,
)
from jmswap.managed_provision import allocate_missing, create_nodes
from jmswap.managed_supervisor import Supervisor

SEED = Mnemonic.to_seed(" ".join(["all"] * 12), "")
WALLET = "ab" * 32


def environment(tmp_path: Path) -> LndEnvironment:
    return LndEnvironment(
        network="signet",
        lnd_binary=tmp_path / "missing-lnd",
        bitcoind_host="127.0.0.1:38332",
        bitcoind_user="u",
        bitcoind_password="p",
    )


class FakeUnlocker:
    calls: list[tuple[str, Any]] = []

    def __init__(self, channel: object) -> None:
        pass

    async def InitWallet(self, request: Any, timeout: float) -> None:  # noqa: N802
        self.calls.append(("init", request))

    async def UnlockWallet(self, request: Any, timeout: float) -> None:  # noqa: N802
        self.calls.append(("unlock", request))


@pytest.fixture
def fake_lnd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ManagedLnd, list[tuple[str, Any]], list[int]]:
    from jmswap.lndrpc import walletunlocker_pb2_grpc

    registry = new_registry("signet", WALLET)
    node = allocate_node(registry, 2, birthday=1_700_000_000, ports=(20001, 20002))
    paths = ManagedNodePaths.for_wallet(tmp_path, "signet", WALLET)
    lnd = ManagedLnd(node, paths, environment(tmp_path))
    states: list[int] = []
    calls: list[tuple[str, Any]] = []
    FakeUnlocker.calls = calls

    @contextlib.asynccontextmanager
    async def channel(*, macaroon: bool) -> AsyncIterator[None]:
        yield None

    async def wallet_state() -> int:
        state = states.pop(0) if len(states) > 1 else states[0]
        if state == SERVER_ACTIVE:
            paths.macaroon(node).parent.mkdir(parents=True, exist_ok=True)
            paths.macaroon(node).write_bytes(b"macaroon")
        return state

    paths.tls_cert(node).parent.mkdir(parents=True)
    paths.tls_cert(node).write_bytes(b"cert")
    monkeypatch.setattr(lnd, "_channel", channel)
    monkeypatch.setattr(lnd, "wallet_state", wallet_state)
    monkeypatch.setattr(walletunlocker_pb2_grpc, "WalletUnlockerStub", FakeUnlocker)
    return lnd, calls, states


async def test_new_wallet_uses_the_derived_identity(fake_lnd: Any) -> None:
    lnd, calls, states = fake_lnd
    states.extend([NON_EXISTING, SERVER_ACTIVE])
    keys = WalletLightningKeys(SEED, "signet")

    await lnd.ensure_unlocked(keys, allow_create=True)

    [(kind, request)] = calls
    scope = lnd.node.scope("signet")
    assert kind == "init"
    assert request.extended_master_key == keys.node_master_key(scope)
    assert request.wallet_password == keys.unlock_password(scope)
    assert request.extended_master_key_birthday_timestamp == 1_700_000_000
    assert request.recovery_window == 0
    assert not request.cipher_seed_mnemonic


async def test_missing_wallet_is_never_created_unless_allowed(fake_lnd: Any) -> None:
    lnd, calls, states = fake_lnd
    states.append(NON_EXISTING)

    with pytest.raises(LndProcessError, match="explicit recovery"):
        await lnd.ensure_unlocked(WalletLightningKeys(SEED, "signet"), allow_create=False)
    assert calls == []


async def test_locked_wallet_is_unlocked_with_the_derived_password(fake_lnd: Any) -> None:
    lnd, calls, states = fake_lnd
    states.extend([LOCKED, SERVER_ACTIVE])
    keys = WalletLightningKeys(SEED, "signet")

    await lnd.ensure_unlocked(keys, allow_create=False)

    assert [kind for kind, _ in calls] == ["unlock"]
    assert calls[0][1].wallet_password == keys.unlock_password(lnd.node.scope("signet"))


def _store_with(tmp_path: Path, state: NodeState) -> tuple[RegistryStore, ManagedNode]:
    store = RegistryStore(ManagedNodePaths.for_wallet(tmp_path, "signet", WALLET))
    registry = new_registry("signet", WALLET)
    node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    node.state = state
    store.update(lambda current: registry)
    return store, node


@pytest.mark.parametrize("state", [NodeState.ACTIVE, NodeState.RETIRING, NodeState.RECOVERING])
async def test_supervisor_never_starts_a_node_without_its_wallet(
    tmp_path: Path, state: NodeState
) -> None:
    store, node = _store_with(tmp_path, state)
    supervisor = Supervisor(store, WalletLightningKeys(SEED, "signet"), environment(tmp_path))

    # The lnd binary does not exist, so any start attempt would raise.
    assert await supervisor._start(node.name) is None
    assert not store.paths.node_dir(node).exists()


async def test_init_refuses_existing_state(tmp_path: Path) -> None:
    store, _ = _store_with(tmp_path, NodeState.ACTIVE)
    before = store.paths.registry.read_bytes()

    with pytest.raises(ManagedNodeError, match="already exists"):
        await create_nodes(
            store,
            WalletLightningKeys(SEED, "signet"),
            environment(tmp_path),
            wallet_identity=WALLET,
            mixdepths=range(2),
        )
    assert store.paths.registry.read_bytes() == before


async def test_init_refuses_orphaned_node_data(tmp_path: Path) -> None:
    store = RegistryStore(ManagedNodePaths.for_wallet(tmp_path, "signet", WALLET))
    (store.paths.nodes_dir / "0123456789abcdef-0-0").mkdir(parents=True)

    with pytest.raises(ManagedNodeError, match="already exists"):
        await create_nodes(
            store,
            WalletLightningKeys(SEED, "signet"),
            environment(tmp_path),
            wallet_identity=WALLET,
            mixdepths=range(2),
        )
    assert not store.paths.registry.exists()


def test_raised_mixdepth_count_only_adds_unknown_mixdepths(tmp_path: Path) -> None:
    store, node = _store_with(tmp_path, NodeState.RETIRED)

    assert allocate_missing(store, range(3)) == [1, 2]
    registry = store.load()
    assert registry is not None
    assert registry.operational(0) is None
    assert {n.mixdepth for n in registry.nodes if n.state is NodeState.PROVISIONING} == {1, 2}
