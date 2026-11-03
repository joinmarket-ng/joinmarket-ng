"""Daemon wallet creation prepares commitment history only for an unused data dir."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jmwalletd.wallet_ops as wallet_ops
from taker.podle_manager import CommitmentHistoryError, PoDLEManager

_MNEMONIC = "abandon " * 11 + "about"


@contextmanager
def _offline_wallet_creation(save_error: Exception | None = None) -> Iterator[None]:
    backend = MagicMock()
    backend.get_block_height = AsyncMock(return_value=800_000)
    wallet_service = MagicMock()
    wallet_service.sync = AsyncMock()
    wallet_service.sync_with_registered_bonds = AsyncMock()
    wallet_service.discover_fidelity_bonds = AsyncMock()
    settings = SimpleNamespace(
        mixdepth_count=5,
        gap_limit=6,
        scan_range=100,
        max_sats_freeze_reuse=-1,
        reconstruct_history=False,
        smart_scan=False,
        background_full_rescan=False,
    )
    with (
        patch("jmwalletd._backend.get_backend", new_callable=AsyncMock, return_value=backend),
        patch("jmwallet.wallet.service.WalletService", return_value=wallet_service),
        patch.object(wallet_ops, "_get_network", return_value="regtest"),
        patch.object(wallet_ops, "_get_wallet_settings", return_value=settings),
        patch.object(wallet_ops, "_save_wallet_file", side_effect=save_error),
    ):
        yield


async def _create(data_dir: Path, name: str = "new.jmdat") -> None:
    await wallet_ops.create_wallet(
        wallet_path=data_dir / "wallets" / name,
        password="password",
        wallet_type="sw",
        data_dir=data_dir,
    )


async def test_first_created_wallet_can_start_a_taker(tmp_path: Path) -> None:
    with pytest.raises(CommitmentHistoryError):
        PoDLEManager(tmp_path)
    with _offline_wallet_creation():
        await _create(tmp_path)
    manager = PoDLEManager(tmp_path)
    assert manager.used_commitments == set()


async def test_wallet_created_beside_another_wallet_keeps_history_missing(tmp_path: Path) -> None:
    (tmp_path / "wallets").mkdir()
    (tmp_path / "wallets" / "old.jmdat").write_bytes(b"wallet")
    with _offline_wallet_creation():
        await _create(tmp_path)
    with pytest.raises(CommitmentHistoryError, match="missing"):
        PoDLEManager(tmp_path)


async def test_failed_creation_does_not_initialize_history(tmp_path: Path) -> None:
    with _offline_wallet_creation(save_error=OSError("disk full")), pytest.raises(OSError):
        await _create(tmp_path)
    assert not (tmp_path / "cmtdata" / "commitments.json").exists()


async def test_recovered_wallet_never_initializes_history(tmp_path: Path) -> None:
    with _offline_wallet_creation():
        await wallet_ops.recover_wallet(
            wallet_path=tmp_path / "wallets" / "recovered.jmdat",
            password="password",
            wallet_type="sw",
            seedphrase=_MNEMONIC,
            data_dir=tmp_path,
        )
    with pytest.raises(CommitmentHistoryError, match="missing"):
        PoDLEManager(tmp_path)
