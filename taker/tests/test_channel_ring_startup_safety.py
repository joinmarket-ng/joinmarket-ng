"""Ring startup must reject unresolved journals before inspecting funding nodes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from taker.taker import Taker


@pytest.mark.parametrize("reason", ["corrupt", "active_limit", "verified_limit", "healthy"])
async def test_funding_enablement_follows_durable_capacity_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reason: str
) -> None:
    import jmswap.channel_ring_nodes as node_module

    import taker.channel_ring as ring_module

    report = SimpleNamespace(
        corruptions=("unsupported journal",) if reason == "corrupt" else (),
        records=(SimpleNamespace(active=True, verified_unresolved=reason == "verified_limit"),)
        if reason != "corrupt"
        else (),
    )
    store = SimpleNamespace(load_all=lambda: report, directory=tmp_path)
    enabled: list[bool] = []

    class FakePool:
        def __init__(self) -> None:
            self.store = store

        async def enable_funding(self) -> None:
            enabled.append(True)

    async def initialize(*args: Any, **kwargs: Any) -> FakePool:
        del args, kwargs
        return FakePool()

    async def reconcile(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    monkeypatch.setattr(node_module, "initialize_channel_ring_nodes", initialize)
    monkeypatch.setattr(node_module, "channel_ring_wallet_identity", lambda _: "wallet")
    monkeypatch.setattr(ring_module, "reconcile_taker_ring_records", reconcile)
    taker = Taker.__new__(Taker)
    taker.config = cast(
        Any,
        SimpleNamespace(
            channel_ring=SimpleNamespace(
                enabled=True,
                nodes={},
                max_active_sessions=1 if reason == "active_limit" else 2,
                max_verified_sessions=1,
                phase_timeout_seconds=10.0,
                persistence_path=lambda _: tmp_path,
            ),
            network=SimpleNamespace(value="regtest"),
            preferred_offer_type=SimpleNamespace(value="tr0absoffer"),
            address_type="p2tr",
            external_podle_mode="disabled",
            market_fault_exclusion=False,
            bitcoin_network=None,
            data_dir=tmp_path,
        ),
    )
    taker.backend = cast(
        Any,
        SimpleNamespace(
            has_mempool_access=lambda: True,
            can_get_confirmations_by_txid=lambda: True,
        ),
    )
    taker.wallet = cast(
        Any,
        SimpleNamespace(
            master_key=SimpleNamespace(get_public_key_bytes=lambda: b"wallet"),
            mixdepth_count=5,
        ),
    )
    taker._channel_ring_recovery_tasks = {}
    taker._channel_ring_coordinator_store = None
    taker.config.channel_ring.taker_joins = True

    async def disconnected() -> int:
        return 0

    taker.directory_client = cast(
        Any, SimpleNamespace(connect_all=disconnected, last_connection_result=None)
    )
    monkeypatch.setattr(Taker, "_renew_channel_ring_input_locks", lambda self: True)
    failure = (
        "Failed to connect to any directory server"
        if reason == "healthy"
        else "durable capacity is exhausted or contains corrupt"
    )
    with pytest.raises(RuntimeError, match=failure):
        await taker.connect()
    assert enabled == ([True] if reason == "healthy" else [])


async def test_unknown_coordinator_journal_blocks_lnd_inspection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import jmswap.channel_ring_nodes as nodes_module
    from jmcore.channel_ring import ChannelRingConfig

    from taker.channel_ring_coordinator_store import CoordinatorStoreError

    directory = tmp_path / "channel-ring" / "coordinators"
    directory.mkdir(parents=True, mode=0o700)
    unknown = directory / ("aa" * 32 + "-0-" + "bb" * 32 + ".json")
    unknown.write_text('{"journal_kind":"unknown_future_kind"}')
    unknown.chmod(0o600)

    async def never_initialize(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("LND inspection ran before coordinator journal validation")

    monkeypatch.setattr(nodes_module, "initialize_channel_ring_nodes", never_initialize)
    monkeypatch.setattr(nodes_module, "channel_ring_wallet_identity", lambda _: "44" * 32)
    taker = Taker.__new__(Taker)
    taker.config = cast(
        Any,
        SimpleNamespace(
            channel_ring=ChannelRingConfig(enabled=True),
            network=SimpleNamespace(value="regtest"),
            preferred_offer_type=SimpleNamespace(value="tr0absoffer"),
            address_type="p2tr",
            external_podle_mode="disabled",
            market_fault_exclusion=False,
            bitcoin_network=None,
            data_dir=tmp_path,
        ),
    )
    taker.backend = cast(
        Any,
        SimpleNamespace(
            has_mempool_access=lambda: True,
            can_get_confirmations_by_txid=lambda: True,
        ),
    )
    taker.wallet = cast(
        Any,
        SimpleNamespace(
            master_key=SimpleNamespace(get_public_key_bytes=lambda: b"wallet"),
            mixdepth_count=5,
        ),
    )
    with pytest.raises(CoordinatorStoreError, match="strict validation"):
        await taker.connect()
    assert unknown.exists()
