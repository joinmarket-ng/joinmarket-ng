"""Tests for jmwalletd.state — DaemonState and CoinjoinState."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jmwalletd.state import CoinjoinState, DaemonState


class TestCoinjoinState:
    def test_values(self) -> None:
        assert CoinjoinState.TAKER_RUNNING == 0
        assert CoinjoinState.MAKER_RUNNING == 1
        assert CoinjoinState.NOT_RUNNING == 2

    def test_is_int_enum(self) -> None:
        assert int(CoinjoinState.TAKER_RUNNING) == 0


class TestDaemonState:
    def test_initial_state(self, data_dir: Path) -> None:
        state = DaemonState(data_dir=data_dir)
        assert state.wallet_service is None
        assert state.wallet_name == ""
        assert state.coinjoin_state == CoinjoinState.NOT_RUNNING
        assert state.maker_running is False
        assert state.taker_running is False
        assert state.rescanning is False
        assert state.rescan_progress == 0.0

    def test_wallet_loaded_false_initially(self, data_dir: Path) -> None:
        state = DaemonState(data_dir=data_dir)
        assert state.wallet_loaded is False

    def test_wallet_loaded_true_when_set(self, daemon_state: DaemonState) -> None:
        daemon_state.wallet_service = MagicMock()
        assert daemon_state.wallet_loaded is True

    def test_wallets_dir(self, daemon_state: DaemonState) -> None:
        assert daemon_state.wallets_dir == daemon_state.data_dir / "wallets"

    def test_list_wallets_empty(self, daemon_state: DaemonState) -> None:
        assert daemon_state.list_wallets() == []

    def test_list_wallets_with_files(self, daemon_state: DaemonState) -> None:
        # Create some wallet files
        (daemon_state.wallets_dir / "alpha.jmdat").touch()
        (daemon_state.wallets_dir / "beta.jmdat").touch()
        (daemon_state.wallets_dir / "not_a_wallet.txt").touch()
        wallets = daemon_state.list_wallets()
        assert wallets == ["alpha.jmdat", "beta.jmdat"]

    @pytest.mark.asyncio
    async def test_lock_wallet_when_not_loaded(self, daemon_state: DaemonState) -> None:
        already = await daemon_state.lock_wallet()
        assert already is True

    @pytest.mark.asyncio
    async def test_lock_wallet_when_loaded(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        daemon_state.wallet_service = mock_wallet_service
        daemon_state.wallet_name = "w.jmdat"
        already = await daemon_state.lock_wallet()
        assert already is False
        assert daemon_state.wallet_service is None
        assert daemon_state.wallet_name == ""
        assert daemon_state.coinjoin_state == CoinjoinState.NOT_RUNNING

    @pytest.mark.asyncio
    async def test_lock_wallet_resets_token_authority(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        daemon_state.wallet_service = mock_wallet_service
        daemon_state.wallet_name = "w.jmdat"
        daemon_state.token_authority.issue("w.jmdat")
        await daemon_state.lock_wallet()
        assert daemon_state.token_authority._wallet_name == ""

    @pytest.mark.asyncio
    async def test_lock_wallet_stops_running_maker(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        """Locking the wallet while a maker is running must stop the maker."""
        daemon_state.wallet_service = mock_wallet_service
        daemon_state.wallet_name = "w.jmdat"
        daemon_state.activate_coinjoin_state(CoinjoinState.MAKER_RUNNING)

        mock_maker = MagicMock()
        mock_maker.stop = AsyncMock()
        daemon_state._maker_ref = mock_maker

        async def _noop() -> None:
            await asyncio.sleep(10)  # simulate a long-running maker task

        task = asyncio.create_task(_noop())
        daemon_state._maker_task = task

        await daemon_state.lock_wallet()

        mock_maker.stop.assert_awaited_once()
        assert task.cancelled()
        assert daemon_state._maker_ref is None
        assert daemon_state._maker_task is None
        assert daemon_state.coinjoin_state == CoinjoinState.NOT_RUNNING

    def test_activate_coinjoin_state_maker(self, daemon_state: DaemonState) -> None:
        daemon_state.activate_coinjoin_state(CoinjoinState.MAKER_RUNNING)
        assert daemon_state.coinjoin_state == CoinjoinState.MAKER_RUNNING
        assert daemon_state.maker_running is True
        assert daemon_state.taker_running is False

    def test_activate_coinjoin_state_taker(self, daemon_state: DaemonState) -> None:
        daemon_state.activate_coinjoin_state(CoinjoinState.TAKER_RUNNING)
        assert daemon_state.taker_running is True
        assert daemon_state.maker_running is False

    def test_activate_coinjoin_not_running(self, daemon_state: DaemonState) -> None:
        daemon_state.activate_coinjoin_state(CoinjoinState.MAKER_RUNNING)
        daemon_state.activate_coinjoin_state(CoinjoinState.NOT_RUNNING)
        assert daemon_state.maker_running is False
        assert daemon_state.taker_running is False

    def test_ws_client_lifecycle(self, daemon_state: DaemonState) -> None:
        queue = daemon_state.register_ws_client()
        assert queue in daemon_state._ws_clients
        daemon_state.unregister_ws_client(queue)
        assert queue not in daemon_state._ws_clients

    def test_broadcast_ws(self, daemon_state: DaemonState) -> None:
        queue = daemon_state.register_ws_client()
        daemon_state.broadcast_ws({"coinjoin_state": 2})
        msg = queue.get_nowait()
        assert '"coinjoin_state": 2' in msg

    def test_broadcast_ws_full_queue_removed(self, daemon_state: DaemonState) -> None:
        """If a WS client's queue is full, it should be removed."""
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        daemon_state._ws_clients.add(queue)
        # Fill the queue
        queue.put_nowait("first")
        # Broadcasting should not raise; the full queue is silently dropped
        daemon_state.broadcast_ws({"test": True})
        assert queue not in daemon_state._ws_clients
