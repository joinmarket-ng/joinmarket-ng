"""Async wallet-file operation tests."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jmwalletd.wallet_ops as wallet_ops

_EVENT_TIMEOUT = 1.0
_MNEMONIC = "abandon " * 11 + "about"


def _make_wallet_settings() -> SimpleNamespace:
    return SimpleNamespace(
        mixdepth_count=3,
        gap_limit=9,
        scan_range=400,
        max_sats_freeze_reuse=12_345,
        reconstruct_history=False,
        smart_scan=False,
        background_full_rescan=False,
    )


def _make_backend() -> MagicMock:
    backend = MagicMock()
    backend.get_block_height = AsyncMock(return_value=800_000)
    return backend


def _make_wallet_service() -> MagicMock:
    wallet_service = MagicMock()
    wallet_service.sync = AsyncMock()
    wallet_service.sync_with_registered_bonds = AsyncMock()
    wallet_service.setup_descriptor_wallet = AsyncMock()
    wallet_service.discover_fidelity_bonds = AsyncMock()
    return wallet_service


async def _wait_for_event(event: threading.Event) -> None:
    assert await asyncio.to_thread(event.wait, _EVENT_TIMEOUT)


class TestAsyncWalletFileOperations:
    async def test_create_save_keeps_event_loop_responsive(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "wallets" / "new.jmdat"
        save_started = threading.Event()
        release_save = threading.Event()
        save_finished = threading.Event()

        def blocking_save(**_kwargs: object) -> None:
            save_started.set()
            assert release_save.wait(_EVENT_TIMEOUT)
            save_finished.set()

        wallet_service = _make_wallet_service()
        with (
            patch(
                "jmwalletd._backend.get_backend",
                new_callable=AsyncMock,
                return_value=_make_backend(),
            ),
            patch("jmwallet.wallet.service.WalletService", return_value=wallet_service),
            patch.object(wallet_ops, "_get_network", return_value="regtest"),
            patch.object(wallet_ops, "_get_wallet_settings", return_value=_make_wallet_settings()),
            patch.object(wallet_ops, "_save_wallet_file", side_effect=blocking_save),
        ):
            create_task = asyncio.create_task(
                wallet_ops.create_wallet(
                    wallet_path=wallet_path,
                    password="password",
                    wallet_type="sw",
                    data_dir=tmp_path,
                )
            )
            try:
                await _wait_for_event(save_started)
                assert not create_task.done()

                heartbeat = asyncio.Event()
                asyncio.get_running_loop().call_soon(heartbeat.set)
                await asyncio.wait_for(heartbeat.wait(), timeout=_EVENT_TIMEOUT)
                assert not save_finished.is_set()
            finally:
                release_save.set()
                await create_task

    async def test_cancelled_save_keeps_reservation_until_worker_finishes(
        self, tmp_path: Path
    ) -> None:
        wallet_path = tmp_path / "wallets" / "new.jmdat"
        save_started = threading.Event()
        release_save = threading.Event()
        save_finished = threading.Event()

        def failing_save(**_kwargs: object) -> None:
            save_started.set()
            assert release_save.wait(_EVENT_TIMEOUT)
            save_finished.set()
            raise RuntimeError("save failed")

        wallet_service = _make_wallet_service()
        with (
            patch(
                "jmwalletd._backend.get_backend",
                new_callable=AsyncMock,
                return_value=_make_backend(),
            ),
            patch("jmwallet.wallet.service.WalletService", return_value=wallet_service),
            patch.object(wallet_ops, "_get_network", return_value="regtest"),
            patch.object(wallet_ops, "_get_wallet_settings", return_value=_make_wallet_settings()),
            patch.object(wallet_ops, "_save_wallet_file", side_effect=failing_save),
        ):
            create_task = asyncio.create_task(
                wallet_ops.create_wallet(
                    wallet_path=wallet_path,
                    password="password",
                    wallet_type="sw",
                    data_dir=tmp_path,
                )
            )
            await _wait_for_event(save_started)

            create_task.cancel()
            await asyncio.sleep(0)
            create_task.cancel()
            await asyncio.sleep(0)
            assert not create_task.done()

            with pytest.raises(FileExistsError, match="operation already in progress"):
                await wallet_ops.create_wallet(
                    wallet_path=wallet_path,
                    password="password",
                    wallet_type="sw",
                    data_dir=tmp_path,
                )

            release_save.set()
            with pytest.raises(asyncio.CancelledError):
                await create_task
            assert save_finished.is_set()

    async def test_recover_save_runs_in_worker_thread(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "wallets" / "recovered.jmdat"
        worker_thread_ids: list[int] = []

        def save_in_worker(**_kwargs: object) -> None:
            worker_thread_ids.append(threading.get_ident())

        wallet_service = _make_wallet_service()
        with (
            patch(
                "jmwalletd._backend.get_backend",
                new_callable=AsyncMock,
                return_value=_make_backend(),
            ),
            patch("jmwallet.wallet.service.WalletService", return_value=wallet_service),
            patch.object(wallet_ops, "_get_network", return_value="regtest"),
            patch.object(wallet_ops, "_get_wallet_settings", return_value=_make_wallet_settings()),
            patch.object(wallet_ops, "_save_wallet_file", side_effect=save_in_worker),
        ):
            await wallet_ops.recover_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw",
                seedphrase=_MNEMONIC,
                data_dir=tmp_path,
            )

        assert len(worker_thread_ids) == 1
        assert worker_thread_ids[0] != threading.get_ident()

    async def test_open_load_runs_in_worker_thread(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "existing.jmdat"
        wallet_path.write_bytes(b"wallet")
        worker_thread_ids: list[int] = []

        def load_in_worker(**_kwargs: object) -> tuple[str, int | None]:
            worker_thread_ids.append(threading.get_ident())
            return _MNEMONIC, None

        wallet_service = _make_wallet_service()
        with (
            patch(
                "jmwalletd._backend.get_backend",
                new_callable=AsyncMock,
                return_value=_make_backend(),
            ),
            patch("jmwallet.wallet.service.WalletService", return_value=wallet_service),
            patch.object(wallet_ops, "_get_network", return_value="regtest"),
            patch.object(wallet_ops, "_get_wallet_settings", return_value=_make_wallet_settings()),
            patch.object(wallet_ops, "_load_wallet_file", side_effect=load_in_worker),
        ):
            wallet_service_result, mnemonic = await wallet_ops.open_wallet_with_mnemonic(
                wallet_path=wallet_path,
                password="password",
                data_dir=tmp_path,
                sync_on_open=False,
            )

        assert wallet_service_result is wallet_service
        assert mnemonic == _MNEMONIC
        assert len(worker_thread_ids) == 1
        assert worker_thread_ids[0] != threading.get_ident()

    async def test_verify_load_runs_in_worker_thread_and_preserves_errors(
        self, tmp_path: Path
    ) -> None:
        wallet_path = tmp_path / "existing.jmdat"
        wallet_path.write_bytes(b"wallet")
        worker_thread_ids: list[int] = []

        def failing_load(**_kwargs: object) -> tuple[str, int | None]:
            worker_thread_ids.append(threading.get_ident())
            raise ValueError("Wrong password or corrupted wallet file.")

        with (
            patch.object(wallet_ops, "_load_wallet_file", side_effect=failing_load),
            pytest.raises(ValueError, match="Wrong password"),
        ):
            await wallet_ops.verify_wallet_password(wallet_path=wallet_path, password="password")

        assert len(worker_thread_ids) == 1
        assert worker_thread_ids[0] != threading.get_ident()

    async def test_file_operation_helper_preserves_worker_error(self) -> None:
        def fail() -> None:
            raise RuntimeError("KDF failure")

        with pytest.raises(RuntimeError, match="KDF failure"):
            await wallet_ops._run_wallet_file_operation(fail)
