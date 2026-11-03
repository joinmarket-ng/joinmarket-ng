"""Daemon wallet creation prepares commitment history only for an unused data dir."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jmwalletd.wallet_ops as wallet_ops
from jmcore.podle import generate_podle
from jmcore.settings import WalletSettings
from jmwallet.wallet.service import WalletService
from taker.config import TakerConfig
from taker.podle_manager import CommitmentHistoryError, PoDLEManager
from taker.taker import Taker

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

    def save(**kwargs: object) -> None:
        if save_error is not None:
            raise save_error
        path = kwargs["wallet_path"]
        assert isinstance(path, Path)
        path.write_bytes(b"saved generated wallet")

    with (
        patch("jmwalletd._backend.get_backend", new_callable=AsyncMock, return_value=backend),
        patch("jmwallet.wallet.service.WalletService", return_value=wallet_service),
        patch.object(wallet_ops, "_get_network", return_value="regtest"),
        patch.object(wallet_ops, "_get_wallet_settings", return_value=settings),
        patch.object(wallet_ops, "_save_wallet_file", side_effect=save),
    ):
        yield


async def _create(data_dir: Path, name: str = "new.jmdat") -> str:
    _wallet, mnemonic = await wallet_ops.create_wallet(
        wallet_path=data_dir / "wallets" / name,
        password="password",
        wallet_type="sw",
        data_dir=data_dir,
    )
    return mnemonic


async def test_first_created_wallet_can_start_a_taker(tmp_path: Path) -> None:
    with pytest.raises(CommitmentHistoryError):
        PoDLEManager(tmp_path)
    with _offline_wallet_creation():
        mnemonic = await _create(tmp_path)
    manager = PoDLEManager(tmp_path, mnemonic=mnemonic)
    assert manager.used_commitments == set()


async def test_first_wallet_with_real_empty_sync_and_encrypted_save_can_start_taker(
    tmp_path: Path,
) -> None:
    backend = MagicMock()
    backend.supports_descriptor_scan = True
    backend.supports_watch_address = False
    backend.get_block_height = AsyncMock(return_value=100)
    backend.scan_descriptors = AsyncMock(return_value={"success": True, "unspents": []})
    backend.close = AsyncMock()
    settings = WalletSettings(mixdepth_count=1, gap_limit=6, scan_range=100)
    with (
        patch("jmwalletd._backend.get_backend", new_callable=AsyncMock, return_value=backend),
        patch.object(wallet_ops, "_get_network", return_value="regtest"),
        patch.object(wallet_ops, "_get_wallet_settings", return_value=settings),
    ):
        wallet, mnemonic = await wallet_ops.create_wallet(
            wallet_path=tmp_path / "wallets" / "new.jmdat",
            password="password",
            wallet_type="sw",
            data_dir=tmp_path,
        )
    try:
        backend.scan_descriptors.assert_awaited_once()
        assert (tmp_path / "wallets" / "new.jmdat").is_file()
        assert not (tmp_path / "history.csv").exists()
        assert not (tmp_path / "coinjoin_history.csv").exists()
        assert PoDLEManager(tmp_path, mnemonic=mnemonic).used_commitments == set()
    finally:
        await wallet.close()


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


async def test_create_then_recover_cannot_reuse_a_previous_commitment(tmp_path: Path) -> None:
    new_mnemonic = "legal winner thank year wave sausage worth useful legal winner thank yellow"
    prior = generate_podle(b"\x01" * 32, f"{'ab' * 32}:0", 0)
    with (
        _offline_wallet_creation(),
        patch("jmwallet.mnemonic.generate_wallet_mnemonic", return_value=new_mnemonic),
    ):
        await _create(tmp_path)
        await wallet_ops.recover_wallet(
            wallet_path=tmp_path / "wallets" / "recovered.jmdat",
            password="password",
            wallet_type="sw",
            seedphrase=_MNEMONIC,
            data_dir=tmp_path,
        )
    assert (tmp_path / "wallets" / "recovered.jmdat").is_file()
    backend = MagicMock()
    backend.can_provide_neutrino_metadata.return_value = False
    recovered = WalletService(_MNEMONIC, backend, network="regtest", data_dir=tmp_path)
    config = TakerConfig(
        mnemonic=_MNEMONIC,
        network="regtest",
        directory_servers=["localhost:5222"],
        data_dir=tmp_path,
    )
    with pytest.raises(CommitmentHistoryError, match="does not authorize this wallet"):
        Taker(recovered, backend, config)
    assert (
        prior.commitment.hex()
        not in PoDLEManager(
            tmp_path,
            mnemonic=new_mnemonic,
        ).used_commitments
    )


@pytest.mark.parametrize(
    "late_path", ["wallets/.recovered.jmdat", "cmtdata/commitments.json.corrupt.1"]
)
async def test_create_rechecks_state_after_sync(tmp_path: Path, late_path: str) -> None:
    evidence = tmp_path / late_path

    async def sync() -> None:
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_bytes(b"trusted previous activity")

    with (
        _offline_wallet_creation(),
        patch("jmwallet.wallet.service.WalletService") as service_class,
    ):
        service_class.return_value.sync = AsyncMock(side_effect=sync)
        await _create(tmp_path)
    assert evidence.read_bytes() == b"trusted previous activity"
    assert not (tmp_path / "cmtdata" / "commitments.json").exists()
