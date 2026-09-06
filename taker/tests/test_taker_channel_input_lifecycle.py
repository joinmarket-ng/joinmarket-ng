"""Taker lifecycle dispatch for channel-input CoinJoin parents."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore.directory_pool import DirectoryConnectionResult

from taker.models import TakerState
from taker.taker import Taker


@pytest.mark.asyncio
async def test_channel_build_dispatch_prepares_one_coordinator(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    taker = cast(Any, Taker.__new__(Taker))
    persistence_path = MagicMock(return_value=tmp_path / "channel-input")
    taker.config = SimpleNamespace(
        data_dir=tmp_path,
        channel_ring=SimpleNamespace(enabled=False),
        channel_input=SimpleNamespace(enabled=True, persistence_path=persistence_path),
    )
    taker.backend = MagicMock()
    taker._channel_input_backend = MagicMock()
    taker._channel_input_registry = MagicMock()
    taker._channel_input_store = MagicMock()
    session = SimpleNamespace(
        channel_input_coordinator=None,
        last_failure_reason=None,
        _phase_build_tx=AsyncMock(),
    )
    taker._session = session

    coordinator = MagicMock()
    coordinator.prepare = AsyncMock()
    factory = MagicMock(return_value=coordinator)
    monkeypatch.setattr("taker.channel_input_coordinator.TakerChannelInputCoordinator", factory)

    assert await taker._build_transaction_phase("destination", 2)

    assert taker.state is TakerState.BUILDING_TX
    assert session.channel_input_coordinator is coordinator
    coordinator.prepare.assert_awaited_once_with("destination", 2)
    session._phase_build_tx.assert_not_awaited()
    assert factory.call_args.kwargs["parent_record_directory"] == (
        tmp_path / "channel-input" / "parents"
    )


@pytest.mark.asyncio
async def test_channel_final_broadcast_uses_coordinator_and_exact_input_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    taker = cast(Any, Taker.__new__(Taker))
    coordinator = SimpleNamespace(publish=AsyncMock(return_value="11" * 32))
    session = SimpleNamespace(
        selected_utxos=[],
        maker_sessions={
            "maker": SimpleNamespace(
                offer=SimpleNamespace(
                    counterparty="maker", fidelity_bond_value=0, fidelity_bond_data=None
                ),
                utxos=[],
            )
        },
        channel_input_coordinator=coordinator,
        final_tx=b"final",
        tx_metadata={"input_values": [100_000, 50_000], "output_owners": []},
        cj_amount=100_000,
        cj_destination="destination",
        taker_change_address="",
        txid="",
        podle_commitment=None,
        broadcast_policy="self",
        broadcast_method="self",
        broadcast_fallback_reason="",
        _minimum_fee_rate_sat_vb=1.0,
        maker_fee_plan=MagicMock(return_value={"maker": 200}),
        _phase_broadcast=AsyncMock(return_value="normal"),
        _get_taker_cj_output_index=MagicMock(return_value=0),
    )
    taker._session = session
    taker.config = SimpleNamespace(data_dir=tmp_path)
    taker.wallet = SimpleNamespace(wallet_fingerprint="wallet")
    taker.directory_client = SimpleNamespace(clients={})
    taker.confirmation_callback = MagicMock(return_value=True)
    taker._update_pending_transaction_now = AsyncMock()

    tx = SimpleNamespace(
        inputs=[object(), object()],
        outputs=[SimpleNamespace(value=149_000)],
    )
    monkeypatch.setattr("taker.taker.deserialize_transaction", lambda _raw: tx)
    monkeypatch.setattr("taker.taker.calculate_tx_vsize", lambda _raw: 100)
    monkeypatch.setattr("taker.taker.calculate_cj_fee", lambda _offer, _amount, _round_up: 200)
    monkeypatch.setattr(
        "taker.taker.update_taker_awaiting_transaction_broadcast", lambda **_kwargs: True
    )
    monkeypatch.setattr(
        "taker.taker.get_notifier",
        lambda: SimpleNamespace(notify_coinjoin_complete=lambda *_args, **_kwargs: None),
    )
    monkeypatch.setattr("taker.taker.spawn_task", lambda _task: None)

    assert await taker._finalize_and_broadcast("destination") == "11" * 32

    assert taker.confirmation_callback.call_args.kwargs["mining_fee"] == 1_000
    coordinator.publish.assert_awaited_once_with()
    session._phase_broadcast.assert_not_awaited()
    assert taker.state is TakerState.COMPLETE


@pytest.mark.asyncio
async def test_channel_connect_rejects_incapable_backend_before_lnd_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = cast(Any, Taker.__new__(Taker))
    taker.config = SimpleNamespace(channel_input=SimpleNamespace(enabled=True))
    taker.backend = SimpleNamespace(
        has_mempool_access=MagicMock(return_value=False),
        can_get_confirmations_by_txid=MagicMock(return_value=False),
    )
    taker._channel_input_registry = MagicMock()
    initializer = AsyncMock()
    monkeypatch.setattr("jmswap.channel_input.initialize_channel_input_backend", initializer)

    with pytest.raises(RuntimeError, match="Channel-input taker requires"):
        await taker.connect()

    initializer.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_connect_reconciles_parents_after_directory_connection_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    events: list[str] = []
    taker = cast(Any, Taker.__new__(Taker))
    taker.config = SimpleNamespace(
        data_dir=tmp_path,
        network=SimpleNamespace(value="regtest"),
        preferred_offer_type=SimpleNamespace(value="sw0reloffer"),
        channel_ring=SimpleNamespace(enabled=False),
        channel_input=SimpleNamespace(
            enabled=True,
            persistence_path=MagicMock(return_value=tmp_path / "channel-input"),
        ),
    )
    taker.backend = SimpleNamespace(
        has_mempool_access=MagicMock(return_value=True),
        can_get_confirmations_by_txid=MagicMock(return_value=True),
    )
    taker._channel_input_registry = SimpleNamespace(
        recovery_outpoints=(),
        reconcile_all=AsyncMock(side_effect=lambda *_: events.append("local-reconcile")),
    )
    taker._channel_input_backend = None
    taker._channel_ring_backend = None
    taker._background_tasks = []
    taker.directory_client = SimpleNamespace(
        enable_channel_input_v1=MagicMock(),
        connect_all=AsyncMock(side_effect=lambda: _record_connect(events)),
        close_all=AsyncMock(side_effect=lambda: _record_close_directories(events)),
        last_connection_result=DirectoryConnectionResult(connected=1, total=1),
    )
    initialized = SimpleNamespace(
        backend=SimpleNamespace(close=AsyncMock(side_effect=lambda: _record_close_backend(events))),
        node_info=SimpleNamespace(identity_pubkey="02" + "11" * 32),
    )
    initializer = AsyncMock(return_value=initialized)
    reconcile = AsyncMock(side_effect=lambda *_: _record_parent_reconcile(events))
    monkeypatch.setattr("jmswap.channel_input.initialize_channel_input_backend", initializer)
    monkeypatch.setattr(
        "taker.channel_input_recovery.reconcile_taker_channel_input_records", reconcile
    )

    await taker.connect()

    assert events[:3] == ["local-reconcile", "connect", "parent-reconcile"]
    assert taker.running is True
    for task in taker._background_tasks:
        task.cancel()
    await asyncio.gather(*taker._background_tasks, return_exceptions=True)
    taker._background_tasks.clear()

    events.clear()
    taker._channel_input_backend = None
    taker.running = False
    reconcile.side_effect = RuntimeError("unsafe durable parent")
    with pytest.raises(RuntimeError, match="unsafe durable parent"):
        await taker.connect()

    assert events == ["local-reconcile", "connect", "close-directories", "close-backend"]
    assert taker._channel_input_backend is None


def _record_connect(events: list[str]) -> int:
    events.append("connect")
    return 1


def _record_close_directories(events: list[str]) -> None:
    events.append("close-directories")


def _record_close_backend(events: list[str]) -> None:
    events.append("close-backend")


def _record_parent_reconcile(events: list[str]) -> None:
    events.append("parent-reconcile")
