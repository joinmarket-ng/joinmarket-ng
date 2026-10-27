"""Managed admission cannot disable or redirect original-node payment tracking."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from bitcointx.core.key import CKey
from jmcore.models import NetworkType
from jmswap.lnd_peer import PaymentResult, PaymentStatus
from jmswap.managed_nodes import NodeState, allocate_node, new_registry
from jmswap.managed_payments import ManagedPaymentEndpoint, ManagedPaymentSource

from taker.podle_purchase import NodePayer, PaymentIdentity, payer_for_mixdepth

PUBLIC = bytes(CKey(b"\x21" * 32).pub)
OLD = bytes(CKey(b"\x22" * 32).pub).hex()
NEW = bytes(CKey(b"\x23" * 32).pub).hex()
HASH = "42" * 32


class Client:
    def __init__(self, identity: str) -> None:
        self.info = SimpleNamespace(identity_pubkey=identity, network="regtest")
        self.node_info = AsyncMock(side_effect=lambda: self.info)
        self.inspect_invoice = AsyncMock(
            return_value=SimpleNamespace(payment_hash=bytes.fromhex(HASH))
        )
        self.track_payment = AsyncMock(return_value=None)
        self.pay = AsyncMock(
            return_value=PaymentResult(PaymentStatus.SUCCEEDED, bytes.fromhex(HASH), None, 0)
        )

    async def __aenter__(self) -> Client:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None


@pytest.fixture
def managed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ManagedPaymentSource, dict[int, Client]]:
    source = ManagedPaymentSource.for_wallet(tmp_path, "regtest", PUBLIC, 0)
    registry = new_registry("regtest", source.wallet_identity)
    node = allocate_node(registry, 0, birthday=1, ports=(20001, 20002))
    node.state, node.identity_pubkey = NodeState.ACTIVE, OLD
    source.store.update(lambda current: registry)
    clients = {20002: Client(OLD), 20004: Client(NEW)}
    monkeypatch.setattr(
        ManagedPaymentEndpoint, "client", lambda endpoint: clients[endpoint.rpc_port]
    )
    return source, clients


async def pay(payer: NodePayer, **kwargs: Any) -> str:
    return await payer.pay(
        "lnbcrt1",
        amount_sats=100,
        fee_limit_sats=20,
        expires_at=int(time.time()) + 60,
        **kwargs,
    )


async def test_retiring_history_is_constructible_without_an_active_mapping_or_external_fallback(
    managed: Any,
) -> None:
    source, clients = managed
    registry = source.store.load()
    registry.nodes[0].state = NodeState.RETIRING
    source.store.update(lambda current: registry)
    payer = payer_for_mixdepth({}, {}, interactive=True, managed_source=lambda md: source)(0)
    assert isinstance(payer, NodePayer)
    fallback = AsyncMock(return_value="shown")
    payer.fallback = SimpleNamespace(pay=fallback)
    assert await pay(payer) == "failed"
    fallback.assert_not_awaited()
    clients[20002].pay.assert_not_awaited()
    # Original-node missing history remains pending, not a failure.
    assert await payer.status(PaymentIdentity(OLD, HASH)) == "pending"
    clients[20002].track_payment.assert_awaited_once_with(bytes.fromhex(HASH))


async def test_identify_pay_rotation_race_never_pays_a_different_identity(managed: Any) -> None:
    source, clients = managed
    payer = NodePayer(managed=source)
    identity = await payer.identify("lnbcrt1")
    assert identity == PaymentIdentity(OLD, HASH)
    registry = source.store.load()
    registry.nodes[0].state = NodeState.RETIRING
    node = allocate_node(registry, 0, birthday=2, ports=(20003, 20004))
    node.state, node.identity_pubkey = NodeState.ACTIVE, NEW
    source.store.update(lambda current: registry)
    assert await pay(payer, expected_payer=identity.payer) == "failed"
    assert await pay(payer) == "paid"
    clients[20002].pay.assert_not_awaited()
    assert clients[20004].pay.await_count == 1
    # A fresh payer after restart resolves the old same-mixdepth identity.
    restarted = NodePayer(managed=ManagedPaymentSource(source.store, source.wallet_identity, 0))
    clients[20002].track_payment.return_value = PaymentResult(
        PaymentStatus.SUCCEEDED, bytes.fromhex(HASH), None, 0
    )
    assert await restarted.status(identity) == "succeeded"
    clients[20004].track_payment.assert_not_awaited()


async def test_registry_loss_does_not_revoke_known_history_or_permit_new_spending(
    managed: Any,
) -> None:
    source, clients = managed
    payer = NodePayer(managed=source)
    identity = await payer.identify("lnbcrt1")
    source.store.paths.registry.unlink()
    assert await pay(payer) == "failed"
    assert await payer.status(identity) == "pending"
    clients[20002].pay.assert_not_awaited()


@pytest.mark.parametrize("wrong", ["identity", "network"])
async def test_actual_node_mismatch_is_refused_without_external_fallback(
    managed: Any, wrong: str
) -> None:
    source, clients = managed
    fallback = SimpleNamespace(pay=AsyncMock(return_value="shown"))
    payer = NodePayer(managed=source, fallback=fallback)
    if wrong == "identity":
        clients[20002].info.identity_pubkey = NEW
    else:
        clients[20002].info.network = "signet"
    assert await pay(payer) == "failed"
    clients[20002].pay.assert_not_awaited()
    fallback.pay.assert_not_awaited()
    with pytest.raises(ValueError, match="original paying node"):
        await payer.status(PaymentIdentity(OLD, HASH))
    clients[20002].track_payment.assert_not_awaited()


async def test_only_an_authorized_terminal_routing_failure_can_show_external_invoice(
    managed: Any,
) -> None:
    source, clients = managed
    fallback = SimpleNamespace(pay=AsyncMock(return_value="shown"))
    payer = NodePayer(managed=source, fallback=fallback)
    clients[20002].pay.side_effect = ConnectionError("ambiguous transport failure")
    with pytest.raises(ConnectionError):
        await pay(payer)
    fallback.pay.assert_not_awaited()
    clients[20002].pay.side_effect = None
    clients[20002].pay.return_value = PaymentResult(
        PaymentStatus.FAILED, bytes.fromhex(HASH), None, 0
    )
    assert await pay(payer) == "shown"
    fallback.pay.assert_awaited_once()


async def test_cli_managed_factory_uses_actual_wallet_scope_even_without_mapping(
    managed: Any, tmp_path: Path
) -> None:
    from taker.cli import _podle_purchaser

    source, clients = managed
    settings: Any = SimpleNamespace(
        lightning=SimpleNamespace(mode="managed"),
        get_data_dir=lambda: tmp_path / "settings",
        network_config=SimpleNamespace(bitcoin_network=None, network=NetworkType.SIGNET),
    )
    config: Any = SimpleNamespace(
        external_podle_mode="prefer", channel_ring=SimpleNamespace(nodes={}, mixdepth_nodes={})
    )
    wallet: Any = SimpleNamespace(
        data_dir=tmp_path,
        network="regtest",
        master_key=SimpleNamespace(get_public_key_bytes=lambda: PUBLIC),
    )
    purchaser = _podle_purchaser(settings, config, interactive=True, wallet=wallet)
    assert purchaser is not None
    payer = purchaser._payer_for(0)
    assert isinstance(payer, NodePayer)
    assert await payer.identify("lnbcrt1") == PaymentIdentity(OLD, HASH)
    assert await pay(payer) == "paid"
    assert clients[20002].pay.await_count == 1
    # No mapped node in another mixdepth means refusal, not a QR fallback.
    other = purchaser._payer_for(1)
    assert isinstance(other, NodePayer)
    assert await pay(other) == "failed"
