"""Taker binds local credential use to the active wallet's private ledger ID."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from _taker_test_helpers import make_taker_config
from jmcore.market_store import MarketStore
from jmcore.paths import get_market_store_path
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService

from taker.podle_manager import ExternalPoDLEPoolError
from taker.taker import Taker


@pytest.mark.asyncio
async def test_wallet_binding_and_taker_shutdown(tmp_path: Path) -> None:
    backend = AsyncMock(spec=BlockchainBackend)
    wallet = WalletService("all " * 11 + "all", backend, network="regtest", data_dir=tmp_path)
    wallet.activate_market_ledger(history_confirmed=True)
    taker = Taker(wallet, backend, make_taker_config(data_dir=tmp_path))
    taker.directory_client = AsyncMock()
    manager = taker.podle_manager
    assert manager._wallet_id == wallet.market_wallet_id
    assert manager.external_count() == 0
    assert manager._market_store is not None

    await taker.stop(close_wallet=False)
    assert manager._market_store is None
    with pytest.raises(ExternalPoDLEPoolError, match="closed"):
        with manager._locked_state():
            pass
    assert len(wallet.market_wallet_id) == 64
    backend.close.assert_not_called()
    with MarketStore(get_market_store_path(tmp_path), wallet_id=wallet.market_wallet_id) as store:
        assert store.wallet_state() == "ready"


@pytest.mark.parametrize("ledger_state", ["absent", "legacy", "ready", "recovery_required"])
@pytest.mark.parametrize("default_taker_dir", [False, True])
async def test_taker_rejects_split_state_directories_before_initialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ledger_state: str,
    default_taker_dir: bool,
) -> None:
    wallet_dir = tmp_path / "wallet-state"
    other_dir = tmp_path / "other-state"
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(other_dir))
    backend = AsyncMock(spec=BlockchainBackend)
    wallet = WalletService("all " * 11 + "all", backend, network="regtest", data_dir=wallet_dir)
    if ledger_state == "legacy":
        MarketStore(get_market_store_path(wallet_dir)).close()
    elif ledger_state != "absent":
        wallet.activate_market_ledger(history_confirmed=True)
        if ledger_state == "recovery_required":
            with MarketStore(
                get_market_store_path(wallet_dir), wallet_id=wallet.market_wallet_id
            ) as store:
                store.mark_recovery_required()
    before = {path: path.read_bytes() for path in wallet_dir.rglob("*") if path.is_file()}
    directory_client = Mock()
    monkeypatch.setattr("taker.taker.MultiDirectoryClient", directory_client)
    try:
        with pytest.raises(ValueError, match="must use the same data directory"):
            Taker(
                wallet,
                backend,
                make_taker_config(data_dir=None if default_taker_dir else other_dir),
            )
        directory_client.assert_not_called()
        assert not get_market_store_path(other_dir).exists()
        assert not (other_dir / "cmtdata").exists()
        assert {
            path: path.read_bytes() for path in wallet_dir.rglob("*") if path.is_file()
        } == before
    finally:
        await wallet.close()


@pytest.mark.parametrize("path_form", ["relative", "symlink", "default"])
async def test_taker_accepts_equivalent_wallet_state_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_form: str
) -> None:
    wallet_dir = tmp_path / "wallet-state"
    backend = AsyncMock(spec=BlockchainBackend)
    wallet = WalletService("all " * 11 + "all", backend, network="regtest", data_dir=wallet_dir)
    wallet.activate_market_ledger(history_confirmed=True)
    if path_form == "relative":
        monkeypatch.chdir(tmp_path)
        configured_dir: Path | None = Path("wallet-state")
    elif path_form == "symlink":
        configured_dir = tmp_path / "wallet-alias"
        configured_dir.symlink_to(wallet_dir, target_is_directory=True)
    else:
        monkeypatch.setenv("JOINMARKET_DATA_DIR", str(wallet_dir))
        configured_dir = None
    taker = Taker(wallet, backend, make_taker_config(data_dir=configured_dir))
    taker.directory_client = AsyncMock()
    try:
        assert taker.podle_manager.filepath == wallet_dir / "cmtdata" / "commitments.json"
        assert taker.podle_manager.external_count() == 0
        assert taker.podle_manager._native_ledger_seen
    finally:
        await taker.stop()


async def test_taker_without_explicit_wallet_directory_keeps_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOINMARKET_DATA_DIR", str(tmp_path / "default-state"))
    backend = AsyncMock(spec=BlockchainBackend)
    wallet = WalletService("all " * 11 + "all", backend, network="regtest")
    configured_dir = tmp_path / "taker-state"
    taker = Taker(wallet, backend, make_taker_config(data_dir=configured_dir))
    taker.directory_client = AsyncMock()
    try:
        assert taker.podle_manager.filepath == configured_dir / "cmtdata" / "commitments.json"
        assert not get_market_store_path(configured_dir).exists()
    finally:
        await taker.stop()


async def test_taker_created_before_activation_observes_wallet_claims(tmp_path: Path) -> None:
    backend = AsyncMock(spec=BlockchainBackend)
    wallet = WalletService("all " * 11 + "all", backend, network="regtest", data_dir=tmp_path)
    taker = Taker(wallet, backend, make_taker_config(data_dir=tmp_path))
    taker.directory_client = AsyncMock()
    try:
        wallet.activate_market_ledger(history_confirmed=True)
        commitment = "11" * 32
        with MarketStore(
            get_market_store_path(tmp_path), wallet_id=wallet.market_wallet_id
        ) as store:
            assert store.claim_local(commitment)
        assert not taker.podle_manager._claim_local_commitment(commitment)
        assert taker.podle_manager._native_ledger_seen
    finally:
        await taker.stop()
