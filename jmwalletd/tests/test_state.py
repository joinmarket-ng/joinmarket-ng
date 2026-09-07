"""Tests for jmwalletd.state — DaemonState and CoinjoinState."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jmwalletd.errors import WalletLifecycleQueueFull
from jmwalletd.log_buffer import get_log_buffer
from jmwalletd.state import (
    CoinjoinState,
    DaemonState,
    WebSocketControl,
    WebSocketNotification,
    WebSocketRegistrationLimit,
)


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
        assert state.wallet_mnemonic == ""
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
        assert daemon_state.wallets_dir.stat().st_mode & 0o777 == 0o700

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
        daemon_state.wallet_mnemonic = "abandon " * 11 + "about"
        daemon_state.wallet_name = "w.jmdat"
        already = await daemon_state.lock_wallet()
        assert already is False
        assert daemon_state.wallet_service is None
        assert daemon_state.wallet_mnemonic == ""
        assert daemon_state.wallet_name == ""
        assert daemon_state.coinjoin_state == CoinjoinState.NOT_RUNNING

    @pytest.mark.asyncio
    async def test_lock_wallet_clears_logs_only_on_loaded_transition(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        log_buffer = get_log_buffer()
        log_buffer.clear()
        try:
            log_buffer.append("wallet session log\n")
            daemon_state.wallet_service = mock_wallet_service

            assert await daemon_state.lock_wallet() is False
            assert log_buffer.text() == ""

            log_buffer.append("daemon startup log\n")

            assert await daemon_state.lock_wallet() is True
            assert log_buffer.text() == "daemon startup log\n"
        finally:
            log_buffer.clear()

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
        client = daemon_state.register_ws_client()
        assert client in daemon_state._ws_clients
        daemon_state.unregister_ws_client(client)
        assert client not in daemon_state._ws_clients

    def test_ws_preauth_registration_is_bounded_and_reclaimed(
        self, daemon_state: DaemonState
    ) -> None:
        clients = [daemon_state.register_ws_client() for _ in range(32)]

        with pytest.raises(WebSocketRegistrationLimit):
            daemon_state.register_ws_client()

        daemon_state.unregister_ws_client(clients[0])
        replacement = daemon_state.register_ws_client()
        assert replacement in daemon_state._ws_clients

    @pytest.mark.asyncio
    async def test_wallet_lifecycle_admission_is_bounded_and_reclaimed(
        self, daemon_state: DaemonState
    ) -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        tasks: list[asyncio.Task[None]] = []

        async def hold_admission(*, notify: bool = False) -> None:
            async with daemon_state.wallet_lifecycle_admission():
                if notify:
                    entered.set()
                await release.wait()

        try:
            first = asyncio.create_task(hold_admission(notify=True))
            tasks.append(first)
            await entered.wait()

            waiting = [asyncio.create_task(hold_admission()) for _ in range(7)]
            tasks.extend(waiting)
            await asyncio.sleep(0)
            assert daemon_state._wallet_lifecycle_operations == 8

            with pytest.raises(WalletLifecycleQueueFull):
                async with daemon_state.wallet_lifecycle_admission():
                    pytest.fail("queue admission should reject excess operations")

            waiting[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting[0]
            assert daemon_state._wallet_lifecycle_operations == 7

            replacement = asyncio.create_task(hold_admission())
            tasks.append(replacement)
            await asyncio.sleep(0)
            assert daemon_state._wallet_lifecycle_operations == 8

            release.set()
            await asyncio.gather(first, *waiting[1:], replacement)
            assert daemon_state._wallet_lifecycle_operations == 0
        finally:
            release.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def test_broadcast_ws(self, daemon_state: DaemonState) -> None:
        daemon_state.wallet_service = MagicMock()
        client = daemon_state.register_ws_client()
        assert daemon_state.authenticate_ws_client(client)
        daemon_state.broadcast_ws({"coinjoin_state": 2})
        notification = client.queue.get_nowait()
        assert isinstance(notification, WebSocketNotification)
        assert '"coinjoin_state": 2' in notification.text

    def test_broadcast_ws_full_queue_removed(self, daemon_state: DaemonState) -> None:
        """If a WS client's queue is full, it should be removed."""
        daemon_state.wallet_service = MagicMock()
        client = daemon_state.register_ws_client()
        client.queue = asyncio.Queue(maxsize=1)
        assert daemon_state.authenticate_ws_client(client)
        daemon_state.broadcast_ws({"first": True})
        # Broadcasting should not raise; the full queue is silently dropped
        daemon_state.broadcast_ws({"test": True})
        assert client not in daemon_state._ws_clients
        assert client.queue.get_nowait() is WebSocketControl.CLOSE

    def test_unauthenticated_ws_client_does_not_receive_broadcast(
        self, daemon_state: DaemonState
    ) -> None:
        client = daemon_state.register_ws_client()
        daemon_state.broadcast_ws({"txid": "private"})
        assert client.queue.empty()

    @pytest.mark.asyncio
    async def test_lock_wallet_invalidates_ws_clients(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        daemon_state.wallet_service = mock_wallet_service
        client = daemon_state.register_ws_client()
        assert daemon_state.authenticate_ws_client(client)
        daemon_state.broadcast_ws({"txid": "before-lock"})

        await daemon_state.lock_wallet()

        assert client not in daemon_state._ws_clients
        assert client.generation is None
        assert client.queue.get_nowait() is WebSocketControl.CLOSE

    @pytest.mark.asyncio
    async def test_lock_wallet_stops_running_taker(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        """Locking the wallet while a taker is running must stop the taker."""
        daemon_state.wallet_service = mock_wallet_service
        daemon_state.wallet_name = "w.jmdat"
        daemon_state.activate_coinjoin_state(CoinjoinState.TAKER_RUNNING)

        mock_taker = MagicMock()
        mock_taker.stop = AsyncMock()
        daemon_state._taker_ref = mock_taker

        async def _noop() -> None:
            await asyncio.sleep(10)

        task = asyncio.create_task(_noop())
        daemon_state._taker_task = task

        await daemon_state.lock_wallet()

        mock_taker.stop.assert_awaited_once()
        assert task.cancelled()
        assert daemon_state._taker_ref is None
        assert daemon_state._taker_task is None
        assert daemon_state.coinjoin_state == CoinjoinState.NOT_RUNNING

    @pytest.mark.asyncio
    async def test_lock_wallet_uses_tumbler_stop_lifecycle(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        daemon_state.wallet_service = mock_wallet_service
        daemon_state.wallet_name = "w.jmdat"
        runner = MagicMock()
        runner.stop_and_wait = AsyncMock()
        task = MagicMock()
        task.done.return_value = True
        daemon_state.tumble_runner = runner
        daemon_state.tumble_task = task

        await daemon_state.lock_wallet()

        runner.stop_and_wait.assert_awaited_once_with(task)
        assert daemon_state.tumble_runner is None
        assert daemon_state.tumble_task is None

    @pytest.mark.asyncio
    async def test_lock_wallet_stops_wallet_sync_task(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        """Locking the wallet cancels any background wallet sync task."""
        daemon_state.wallet_service = mock_wallet_service
        daemon_state.wallet_name = "w.jmdat"

        async def _sync() -> None:
            await asyncio.sleep(10)

        sync_task = asyncio.create_task(_sync())
        daemon_state._wallet_sync_task = sync_task

        await daemon_state.lock_wallet()

        assert sync_task.cancelled()
        assert daemon_state._wallet_sync_task is None

    @pytest.mark.asyncio
    async def test_lock_wallet_stops_rescan_task_and_resets_flags(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        """Locking the wallet cancels rescan tracking and clears the flags."""
        daemon_state.wallet_service = mock_wallet_service
        daemon_state.wallet_name = "w.jmdat"
        daemon_state.rescanning = True
        daemon_state.rescan_progress = 0.5

        async def _rescan() -> None:
            await asyncio.sleep(10)

        rescan_task = asyncio.create_task(_rescan())
        daemon_state._rescan_task = rescan_task

        await daemon_state.lock_wallet()

        assert rescan_task.cancelled()
        assert daemon_state._rescan_task is None
        assert daemon_state.rescanning is False
        assert daemon_state.rescan_progress == 0.0

    @pytest.mark.asyncio
    async def test_live_rescan_status_prefers_core_state(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        """Core's getwalletinfo.scanning wins over the stale in-memory flag."""
        daemon_state.wallet_service = mock_wallet_service
        mock_wallet_service.backend.get_rescan_status = AsyncMock(
            return_value={"in_progress": True, "progress": 0.18, "duration": 17}
        )
        daemon_state.rescanning = False

        rescanning, progress = await daemon_state.live_rescan_status()
        assert rescanning is True
        assert progress == 0.18

    @pytest.mark.asyncio
    async def test_live_rescan_status_daemon_flag_when_core_idle(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        """Wallet-side sync work (not a Core scan) still reports rescanning."""
        daemon_state.wallet_service = mock_wallet_service
        mock_wallet_service.backend.get_rescan_status = AsyncMock(
            return_value={"in_progress": False}
        )
        daemon_state.rescanning = True
        daemon_state.rescan_progress = 0.0

        rescanning, progress = await daemon_state.live_rescan_status()
        assert rescanning is True
        assert progress == 0.0

    @pytest.mark.asyncio
    async def test_live_rescan_status_fallback_on_rpc_error(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        daemon_state.wallet_service = mock_wallet_service
        mock_wallet_service.backend.get_rescan_status = AsyncMock(
            side_effect=RuntimeError("rpc down")
        )
        daemon_state.rescanning = True
        daemon_state.rescan_progress = 0.3

        rescanning, progress = await daemon_state.live_rescan_status()
        assert rescanning is True
        assert progress == 0.3

    @pytest.mark.asyncio
    async def test_live_rescan_status_no_wallet(self, daemon_state: DaemonState) -> None:
        rescanning, progress = await daemon_state.live_rescan_status()
        assert rescanning is False
        assert progress is None

    @pytest.mark.asyncio
    async def test_lock_wallet_maker_stop_raises(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        """Locking wallet handles exceptions from maker.stop() gracefully."""
        daemon_state.wallet_service = mock_wallet_service
        daemon_state.wallet_name = "w.jmdat"

        mock_maker = MagicMock()
        mock_maker.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        daemon_state._maker_ref = mock_maker

        # Should not raise
        await daemon_state.lock_wallet()
        assert daemon_state.wallet_service is None

    @pytest.mark.asyncio
    async def test_lock_wallet_taker_stop_raises(
        self, daemon_state: DaemonState, mock_wallet_service: MagicMock
    ) -> None:
        """Locking wallet handles exceptions from taker.stop() gracefully."""
        daemon_state.wallet_service = mock_wallet_service
        daemon_state.wallet_name = "w.jmdat"

        mock_taker = MagicMock()
        mock_taker.stop = AsyncMock(side_effect=RuntimeError("stop failed"))
        daemon_state._taker_ref = mock_taker

        # Should not raise
        await daemon_state.lock_wallet()
        assert daemon_state.wallet_service is None
