"""Tests for jmwalletd.wallet_ops — wallet file operations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mnemonic import Mnemonic

from jmwalletd.wallet_ops import (
    _get_network,
    _load_wallet_file,
    _save_wallet_file,
    create_wallet,
    open_wallet,
    open_wallet_with_mnemonic,
    recover_wallet,
)


def _make_descriptor_backend(block_height: int = 800000) -> MagicMock:
    """Return a mock that passes isinstance checks for DescriptorWalletBackend."""
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

    mock = MagicMock(spec=DescriptorWalletBackend)
    mock.get_block_height = AsyncMock(return_value=block_height)
    return mock


def _make_neutrino_backend(block_height: int = 800000) -> MagicMock:
    """Return a mock that does NOT pass isinstance checks for DescriptorWalletBackend."""
    from jmwallet.backends.neutrino import NeutrinoBackend

    mock = MagicMock(spec=NeutrinoBackend)
    mock.get_block_height = AsyncMock(return_value=block_height)
    return mock


def _make_wallet_settings() -> SimpleNamespace:
    """Return non-default values so forwarding tests detect hard-coded defaults."""
    return SimpleNamespace(
        mixdepth_count=3,
        gap_limit=9,
        scan_range=400,
        max_sats_freeze_reuse=12_345,
        reconstruct_history=False,
        smart_scan=False,
        background_full_rescan=False,
    )


class TestWalletFileIO:
    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "test.jmdat"
        password = "test_password_123"
        mnemonic = "abandon " * 11 + "about"

        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic=mnemonic,
            password=password,
            wallet_type="sw-fb",
        )
        assert wallet_path.exists()

        loaded_mnemonic, creation_height = _load_wallet_file(
            wallet_path=wallet_path, password=password
        )
        assert loaded_mnemonic == mnemonic
        assert creation_height is None  # No creation_height stored

    def test_load_wrong_password(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "test.jmdat"
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="test mnemonic",
            password="correct",
            wallet_type="sw",
        )

        with pytest.raises(ValueError, match="[Ww]rong|[Ii]nvalid|[Dd]ecrypt"):
            _load_wallet_file(wallet_path=wallet_path, password="wrong")

    def test_load_tightens_existing_wallet_mode(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "test.jmdat"
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="abandon " * 11 + "about",
            password="password",
            wallet_type="sw",
        )
        wallet_path.chmod(0o644)

        _load_wallet_file(wallet_path=wallet_path, password="password")

        assert wallet_path.stat().st_mode & 0o777 == 0o600

    def test_save_creates_file(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "new_wallet.jmdat"
        assert not wallet_path.exists()
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="test",
            password="pass",
            wallet_type="sw",
        )
        assert wallet_path.exists()
        content = wallet_path.read_bytes()
        assert len(content) > 16  # At least the salt

    def test_save_uses_argon2id_format(self, tmp_path: Path) -> None:
        """New wallet files start with the ``JMNG`` magic + Argon2id header."""
        wallet_path = tmp_path / "argon.jmdat"
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="abandon " * 11 + "about",
            password="pw",
            wallet_type="sw",
        )

        content = wallet_path.read_bytes()
        # Magic + version(1) + kdf_id(1) + m_cost(4) + t_cost(4) + p_cost(1) + salt(16)
        assert content[:4] == b"JMNG"
        assert content[4] == 1  # format version
        assert content[5] == 1  # kdf_id = Argon2id
        # m_cost == 19_456 (KiB)
        assert int.from_bytes(content[6:10], "big") == 19_456
        assert int.from_bytes(content[10:14], "big") == 2  # t_cost
        assert content[14] == 1  # parallelism

    def test_load_legacy_pbkdf2_file(self, tmp_path: Path) -> None:
        """Wallet files written with the old PBKDF2 format still load (back-compat)."""
        import base64
        import json
        import os

        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        wallet_path = tmp_path / "legacy.jmdat"
        password = "legacy-password"
        mnemonic = "abandon " * 11 + "about"

        # Reproduce the pre-Argon2id on-disk format: raw 16-byte salt followed
        # by a Fernet token whose key is PBKDF2-SHA256(password, 600k iters).
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        token = Fernet(key).encrypt(
            json.dumps(
                {"mnemonic": mnemonic, "wallet_type": "sw", "creation_height": 820_000}
            ).encode()
        )
        wallet_path.write_bytes(salt + token)

        loaded_mnemonic, creation_height = _load_wallet_file(
            wallet_path=wallet_path, password=password
        )
        assert loaded_mnemonic == mnemonic
        assert creation_height == 820_000

    def test_load_rejects_unknown_version(self, tmp_path: Path) -> None:
        """A JMNG-prefixed file with an unknown version byte is rejected cleanly."""
        wallet_path = tmp_path / "future.jmdat"
        # Magic + version 99 + kdf_id 1 + m(4) + t(4) + p(1) + salt(16) + dummy
        wallet_path.write_bytes(
            b"JMNG"
            + bytes([99, 1])
            + (19_456).to_bytes(4, "big")
            + (2).to_bytes(4, "big")
            + bytes([1])
            + b"\x00" * 16
            + b"junk"
        )
        with pytest.raises(ValueError, match="version"):
            _load_wallet_file(wallet_path=wallet_path, password="anything")

    def test_load_rejects_unknown_kdf(self, tmp_path: Path) -> None:
        """A JMNG-prefixed file with an unknown KDF id is rejected cleanly."""
        wallet_path = tmp_path / "unknown_kdf.jmdat"
        wallet_path.write_bytes(
            b"JMNG"
            + bytes([1, 99])  # version 1, kdf 99
            + (19_456).to_bytes(4, "big")
            + (2).to_bytes(4, "big")
            + bytes([1])
            + b"\x00" * 16
            + b"junk"
        )
        with pytest.raises(ValueError, match="KDF"):
            _load_wallet_file(wallet_path=wallet_path, password="anything")

    def test_load_rejects_truncated_argon2id_header(self, tmp_path: Path) -> None:
        """A JMNG-prefixed file shorter than the full header is rejected."""
        wallet_path = tmp_path / "truncated.jmdat"
        wallet_path.write_bytes(b"JMNG" + bytes([1, 1]) + b"\x00" * 3)
        with pytest.raises(ValueError, match="truncated"):
            _load_wallet_file(wallet_path=wallet_path, password="anything")

    def test_load_argon2id_wrong_password(self, tmp_path: Path) -> None:
        """Argon2id files also raise ValueError on a bad password."""
        wallet_path = tmp_path / "argon_wrong_pw.jmdat"
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="test mnemonic",
            password="correct",
            wallet_type="sw",
        )
        with pytest.raises(ValueError, match="[Ww]rong|[Ii]nvalid|[Dd]ecrypt"):
            _load_wallet_file(wallet_path=wallet_path, password="wrong")


class TestCreateWallet:
    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_creates_wallet_descriptor_backend(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "new.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()
        wallet_settings = _make_wallet_settings()

        with patch("jmwalletd.wallet_ops._get_wallet_settings", return_value=wallet_settings):
            ws, seedphrase = await create_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw-fb",
                data_dir=tmp_path,
            )
        assert ws is mock_ws
        assert isinstance(seedphrase, str)
        assert len(seedphrase.split()) == 12
        assert Mnemonic("english").check(seedphrase)
        assert wallet_path.exists()
        stored_seedphrase, _ = _load_wallet_file(wallet_path=wallet_path, password="password")
        assert stored_seedphrase == seedphrase
        assert wallet_path.stat().st_mode & 0o777 == 0o600
        assert wallet_path.parent.stat().st_mode & 0o777 == 0o700

        # Verify network was passed through.
        mock_ws_cls.assert_called_once()
        wallet_kwargs = mock_ws_cls.call_args.kwargs
        assert wallet_kwargs["network"] == "mainnet"

        assert wallet_kwargs["mixdepth_count"] == wallet_settings.mixdepth_count
        assert wallet_kwargs["gap_limit"] == wallet_settings.gap_limit
        assert wallet_kwargs["scan_range"] == wallet_settings.scan_range
        assert wallet_kwargs["max_sats_freeze_reuse"] == wallet_settings.max_sats_freeze_reuse
        assert wallet_kwargs["reconstruct_history"] == wallet_settings.reconstruct_history

        # Descriptor backend: setup_descriptor_wallet called with no rescan.
        mock_ws.setup_descriptor_wallet.assert_awaited_once_with(rescan=False)

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_creates_wallet_neutrino_backend(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "new_neutrino.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_neutrino_backend()

        ws, seedphrase = await create_wallet(
            wallet_path=wallet_path,
            password="password",
            wallet_type="sw",
            data_dir=tmp_path,
        )
        assert ws is mock_ws
        assert wallet_path.exists()

        # Neutrino backend: setup_descriptor_wallet must NOT be called.
        mock_ws.setup_descriptor_wallet.assert_not_awaited()
        # sync() must still be called.
        mock_ws.sync.assert_awaited_once()

    @patch("jmwalletd.wallet_ops._get_network", return_value="signet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_creates_wallet_signet(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "signet.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()

        ws, _ = await create_wallet(
            wallet_path=wallet_path,
            password="password",
            wallet_type="sw",
            data_dir=tmp_path,
        )
        assert ws is mock_ws
        mock_ws_cls.assert_called_once()
        assert mock_ws_cls.call_args.kwargs["network"] == "signet"
        mock_ws.setup_descriptor_wallet.assert_awaited_once_with(rescan=False)

    async def test_invalid_wallet_type(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "bad.jmdat"
        with pytest.raises(ValueError, match="[Uu]nsupported|[Ii]nvalid"):
            await create_wallet(
                wallet_path=wallet_path,
                password="pass",
                wallet_type="invalid-type",
                data_dir=tmp_path,
            )

    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    async def test_entropy_failure_prevents_backend_and_file_creation(
        self,
        mock_get_backend: AsyncMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "failed.jmdat"

        with (
            patch(
                "jmwallet.mnemonic.secrets.token_bytes",
                side_effect=OSError("operating-system CSPRNG unavailable"),
            ),
            pytest.raises(OSError, match="CSPRNG unavailable"),
        ):
            await create_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw",
                data_dir=tmp_path,
            )

        mock_get_backend.assert_not_awaited()
        assert not wallet_path.exists()

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_descriptor_setup_failure_leaves_no_wallet_file(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "retryable.jmdat"
        mock_ws = MagicMock()
        mock_ws.setup_descriptor_wallet = AsyncMock(side_effect=RuntimeError("rpc unavailable"))
        mock_ws.sync = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()

        with pytest.raises(RuntimeError, match="rpc unavailable"):
            await create_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw",
                data_dir=tmp_path,
            )

        assert not wallet_path.exists()
        mock_ws.sync.assert_not_awaited()

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_sync_failure_leaves_no_wallet_file(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "retryable.jmdat"
        mock_ws = MagicMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws.sync = AsyncMock(side_effect=RuntimeError("sync failed"))
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()

        with pytest.raises(RuntimeError, match="sync failed"):
            await create_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw",
                data_dir=tmp_path,
            )

        assert not wallet_path.exists()

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_same_name_can_be_retried_after_setup_failure(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "retryable.jmdat"
        mock_ws = MagicMock()
        mock_ws.setup_descriptor_wallet = AsyncMock(
            side_effect=[RuntimeError("rpc unavailable"), None]
        )
        mock_ws.sync = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()

        with pytest.raises(RuntimeError, match="rpc unavailable"):
            await create_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw",
                data_dir=tmp_path,
            )

        wallet_service, seedphrase = await create_wallet(
            wallet_path=wallet_path,
            password="password",
            wallet_type="sw",
            data_dir=tmp_path,
        )

        assert wallet_service is mock_ws
        assert wallet_path.exists()
        loaded_mnemonic, _ = _load_wallet_file(wallet_path=wallet_path, password="password")
        assert loaded_mnemonic == seedphrase
        assert mock_ws.setup_descriptor_wallet.await_count == 2
        mock_ws.sync.assert_awaited_once()

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    @patch("jmwallet.mnemonic.generate_wallet_mnemonic", return_value="abandon " * 11 + "about")
    async def test_concurrent_same_name_loser_performs_no_initialization(
        self,
        mock_generate_mnemonic: MagicMock,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "concurrent.jmdat"
        backend_started = asyncio.Event()
        release_backend = asyncio.Event()
        backend = _make_descriptor_backend()

        async def get_backend(**_kwargs: object) -> MagicMock:
            backend_started.set()
            await release_backend.wait()
            return backend

        mock_get_backend.side_effect = get_backend
        mock_ws = MagicMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws.sync = AsyncMock()
        mock_ws_cls.return_value = mock_ws

        winner = asyncio.create_task(
            create_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw",
                data_dir=tmp_path,
            )
        )
        await backend_started.wait()

        with pytest.raises(FileExistsError, match="operation already in progress"):
            await create_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw",
                data_dir=tmp_path,
            )

        assert mock_generate_mnemonic.call_count == 1
        assert mock_get_backend.await_count == 1
        assert mock_ws_cls.call_count == 0

        release_backend.set()
        wallet_service, _ = await winner

        assert wallet_service is mock_ws
        assert wallet_path.exists()

    async def test_existing_wallet_is_never_removed(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "wallets" / "existing.jmdat"
        wallet_path.parent.mkdir(parents=True)
        wallet_path.write_bytes(b"wallet owned by another request")

        with pytest.raises(FileExistsError, match="already exists"):
            await create_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw",
                data_dir=tmp_path,
            )

        assert wallet_path.read_bytes() == b"wallet owned by another request"


class TestGetNetwork:
    def test_prefers_bitcoin_network_when_set(self) -> None:
        mock_settings = MagicMock()
        mock_settings.network_config.network.value = "testnet"
        mock_settings.network_config.bitcoin_network.value = "regtest"

        with patch("jmcore.settings.get_settings", return_value=mock_settings):
            assert _get_network() == "regtest"

    def test_falls_back_to_protocol_network(self) -> None:
        mock_settings = MagicMock()
        mock_settings.network_config.network.value = "signet"
        mock_settings.network_config.bitcoin_network = None

        with patch("jmcore.settings.get_settings", return_value=mock_settings):
            assert _get_network() == "signet"


class TestRecoverWallet:
    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_recovers_wallet_descriptor_backend(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "recovered.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        seedphrase = "abandon " * 11 + "about"

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.sync_with_registered_bonds = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()
        wallet_settings = _make_wallet_settings()

        with patch("jmwalletd.wallet_ops._get_wallet_settings", return_value=wallet_settings):
            ws = await recover_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw",
                seedphrase=seedphrase,
                data_dir=tmp_path,
            )
        assert ws is mock_ws
        assert wallet_path.exists()
        mock_ws_cls.assert_called_once()
        assert mock_ws_cls.call_args.kwargs["network"] == "mainnet"

        mock_ws.setup_descriptor_wallet.assert_awaited_once_with(
            include_all_fidelity_bonds=False,
            rescan_existing=True,
            smart_scan=wallet_settings.smart_scan,
            background_full_rescan=wallet_settings.background_full_rescan,
        )
        # Bond-aware sync so recovered fidelity bonds are scanned and surfaced.
        mock_ws.sync_with_registered_bonds.assert_awaited_once()

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_sw_fb_recovery_imports_all_bonds_and_uses_requested_scan_range(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "recovered_fb.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        mock_ws = MagicMock()
        mock_ws.sync_with_registered_bonds = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()
        wallet_settings = _make_wallet_settings()

        with patch("jmwalletd.wallet_ops._get_wallet_settings", return_value=wallet_settings):
            await recover_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw-fb",
                seedphrase="abandon " * 11 + "about",
                data_dir=tmp_path,
                scan_range=2_500,
            )

        assert mock_ws_cls.call_args.kwargs["scan_range"] == 2_500
        mock_ws.setup_descriptor_wallet.assert_awaited_once_with(
            include_all_fidelity_bonds=True,
            rescan_existing=True,
            smart_scan=wallet_settings.smart_scan,
            background_full_rescan=wallet_settings.background_full_rescan,
        )

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_recovery_setup_failure_leaves_no_wallet_file(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "retryable.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        mock_ws = MagicMock()
        mock_ws.setup_descriptor_wallet = AsyncMock(side_effect=RuntimeError("rpc unavailable"))
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()

        with pytest.raises(RuntimeError, match="rpc unavailable"):
            await recover_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw-fb",
                seedphrase="abandon " * 11 + "about",
                data_dir=tmp_path,
            )

        assert not wallet_path.exists()
        assert (wallet_path.parent / ".wallet-operation.lock").exists()

    async def test_recovery_rejects_a_concurrent_wallet_reservation(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "wallets" / "different-name.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
        import os

        reservation = wallet_path.parent / ".wallet-operation.lock"
        reservation_fd = os.open(reservation, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(reservation_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        try:
            with pytest.raises(FileExistsError, match="operation already in progress"):
                await recover_wallet(
                    wallet_path=wallet_path,
                    password="password",
                    wallet_type="sw-fb",
                    seedphrase="abandon " * 11 + "about",
                    data_dir=tmp_path,
                )
        finally:
            fcntl.flock(reservation_fd, fcntl.LOCK_UN)
            os.close(reservation_fd)

        assert reservation.exists()

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_recovers_wallet_neutrino_backend(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "recovered_neutrino.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        seedphrase = "abandon " * 11 + "about"

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.sync_with_registered_bonds = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws.discover_fidelity_bonds = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_neutrino_backend()

        ws = await recover_wallet(
            wallet_path=wallet_path,
            password="password",
            wallet_type="sw",
            seedphrase=seedphrase,
            data_dir=tmp_path,
        )
        assert ws is mock_ws

        # Neutrino backend: setup_descriptor_wallet must NOT be called.
        mock_ws.setup_descriptor_wallet.assert_not_awaited()
        mock_ws.sync_with_registered_bonds.assert_awaited_once()
        # A plain "sw" wallet has no bond branch to discover.
        mock_ws.discover_fidelity_bonds.assert_not_awaited()

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_sw_fb_recovery_on_neutrino_discovers_bonds(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Light-client recovery scans the canonical bond branch explicitly."""
        wallet_path = tmp_path / "wallets" / "recovered_neutrino_fb.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        mock_ws = MagicMock()
        mock_ws.sync_with_registered_bonds = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws.discover_fidelity_bonds = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_neutrino_backend()

        await recover_wallet(
            wallet_path=wallet_path,
            password="password",
            wallet_type="sw-fb",
            seedphrase="abandon " * 11 + "about",
            data_dir=tmp_path,
        )

        mock_ws.setup_descriptor_wallet.assert_not_awaited()
        mock_ws.discover_fidelity_bonds.assert_awaited_once()
        assert wallet_path.exists()

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_scan_range_below_configured_default_is_widened(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Legacy gaplimit-style values (e.g. JAM's default 6) must not shrink
        descriptor coverage below the configured [wallet].scan_range."""
        wallet_path = tmp_path / "wallets" / "recovered_small_range.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        mock_ws = MagicMock()
        mock_ws.sync_with_registered_bonds = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()
        wallet_settings = _make_wallet_settings()

        with patch("jmwalletd.wallet_ops._get_wallet_settings", return_value=wallet_settings):
            await recover_wallet(
                wallet_path=wallet_path,
                password="password",
                wallet_type="sw-fb",
                seedphrase="abandon " * 11 + "about",
                data_dir=tmp_path,
                scan_range=6,
            )

        assert mock_ws_cls.call_args.kwargs["scan_range"] == wallet_settings.scan_range


class TestOpenWallet:
    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_opens_wallet_descriptor_backend(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "existing.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        # Create the encrypted wallet file first
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="abandon " * 11 + "about",
            password="password",
            wallet_type="sw-fb",
        )

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.sync_with_registered_bonds = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()
        wallet_settings = _make_wallet_settings()

        with patch("jmwalletd.wallet_ops._get_wallet_settings", return_value=wallet_settings):
            ws = await open_wallet(
                wallet_path=wallet_path,
                password="password",
                data_dir=tmp_path,
            )
        assert ws is mock_ws
        mock_ws_cls.assert_called_once()
        assert mock_ws_cls.call_args.kwargs["network"] == "mainnet"

        mock_ws.setup_descriptor_wallet.assert_awaited_once_with(
            smart_scan=wallet_settings.smart_scan,
            background_full_rescan=wallet_settings.background_full_rescan,
        )
        # Bond-aware sync so funded fidelity bonds are surfaced in /utxos.
        mock_ws.sync_with_registered_bonds.assert_awaited_once()

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_opens_wallet_neutrino_backend(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Neutrino backend: setup_descriptor_wallet must not be called on unlock."""
        wallet_path = tmp_path / "wallets" / "neutrino.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="abandon " * 11 + "about",
            password="password",
            wallet_type="sw",
        )

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.sync_with_registered_bonds = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_neutrino_backend()

        ws = await open_wallet(
            wallet_path=wallet_path,
            password="password",
            data_dir=tmp_path,
        )
        assert ws is mock_ws

        # Neutrino backend: setup_descriptor_wallet must NOT be called.
        mock_ws.setup_descriptor_wallet.assert_not_awaited()
        # sync (bond-aware) must still be called.
        mock_ws.sync_with_registered_bonds.assert_awaited_once()

    async def test_open_nonexistent(self, tmp_path: Path) -> None:
        wallet_path = tmp_path / "nonexistent.jmdat"
        with pytest.raises((FileNotFoundError, ValueError)):
            await open_wallet(
                wallet_path=wallet_path,
                password="pass",
                data_dir=tmp_path,
            )

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_open_wrong_password(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "test.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="abandon " * 11 + "about",
            password="correct_password",
            wallet_type="sw",
        )

        with pytest.raises(ValueError, match="[Ww]rong|[Ii]nvalid|[Dd]ecrypt"):
            await open_wallet(
                wallet_path=wallet_path,
                password="wrong_password",
                data_dir=tmp_path,
            )

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_open_wallet_with_mnemonic_returns_seedphrase(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        wallet_path = tmp_path / "wallets" / "existing.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)
        mnemonic = "abandon " * 11 + "about"
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic=mnemonic,
            password="password",
            wallet_type="sw-fb",
        )

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()

        ws, seedphrase = await open_wallet_with_mnemonic(
            wallet_path=wallet_path,
            password="password",
            data_dir=tmp_path,
            sync_on_open=False,
        )

        assert ws is mock_ws
        assert seedphrase == mnemonic


class TestCreationHeight:
    """Tests for wallet creation height (birthday) feature."""

    def test_save_and_load_with_creation_height(self, tmp_path: Path) -> None:
        """Saving with creation_height and loading returns the height."""
        wallet_path = tmp_path / "test.jmdat"
        password = "test_password_123"
        mnemonic = "abandon " * 11 + "about"

        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic=mnemonic,
            password=password,
            wallet_type="sw-fb",
            creation_height=800000,
        )
        assert wallet_path.exists()

        loaded_mnemonic, creation_height = _load_wallet_file(
            wallet_path=wallet_path, password=password
        )
        assert loaded_mnemonic == mnemonic
        assert creation_height == 800000

    def test_save_without_creation_height_backward_compat(self, tmp_path: Path) -> None:
        """Old wallet files without creation_height load with None."""
        wallet_path = tmp_path / "old_wallet.jmdat"
        password = "test"
        mnemonic = "abandon " * 11 + "about"

        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic=mnemonic,
            password=password,
            wallet_type="sw",
        )

        loaded_mnemonic, creation_height = _load_wallet_file(
            wallet_path=wallet_path, password=password
        )
        assert loaded_mnemonic == mnemonic
        assert creation_height is None

    def test_load_with_invalid_creation_height_type_returns_none(self, tmp_path: Path) -> None:
        """Invalid creation_height types in wallet file are ignored."""
        import base64
        import json
        import os

        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        wallet_path = tmp_path / "invalid_birthday.jmdat"
        password = "test_password_123"

        # Manually craft an encrypted wallet payload with a string creation_height.
        salt = os.urandom(16)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        key = base64.urlsafe_b64encode(kdf.derive(password.encode()))
        fernet = Fernet(key)
        payload = {
            "mnemonic": "abandon " * 11 + "about",
            "wallet_type": "sw",
            "creation_height": "820000",
        }
        wallet_path.write_bytes(salt + fernet.encrypt(json.dumps(payload).encode()))

        loaded_mnemonic, creation_height = _load_wallet_file(
            wallet_path=wallet_path, password=password
        )
        assert loaded_mnemonic == "abandon " * 11 + "about"
        assert creation_height is None

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_create_wallet_stores_creation_height(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        """create_wallet queries block height and stores it in the .jmdat file."""
        wallet_path = tmp_path / "wallets" / "birthday.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend(block_height=850000)

        _, seedphrase = await create_wallet(
            wallet_path=wallet_path,
            password="password",
            wallet_type="sw-fb",
            data_dir=tmp_path,
        )

        # Verify creation_height was stored in the file
        loaded_mnemonic, creation_height = _load_wallet_file(
            wallet_path=wallet_path, password="password"
        )
        assert loaded_mnemonic == seedphrase
        assert creation_height == 850000

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_create_wallet_graceful_on_block_height_failure(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        """create_wallet still works even if get_block_height fails."""
        wallet_path = tmp_path / "wallets" / "no_birthday.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        mock_backend = _make_descriptor_backend()
        mock_backend.get_block_height = AsyncMock(side_effect=RuntimeError("RPC down"))
        mock_get_backend.return_value = mock_backend

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws

        _, seedphrase = await create_wallet(
            wallet_path=wallet_path,
            password="password",
            wallet_type="sw",
            data_dir=tmp_path,
        )

        # Wallet created successfully, but no creation_height
        loaded_mnemonic, creation_height = _load_wallet_file(
            wallet_path=wallet_path, password="password"
        )
        assert loaded_mnemonic == seedphrase
        assert creation_height is None

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_open_wallet_with_creation_height_calls_backend(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        """open_wallet_with_mnemonic calls set_wallet_creation_height on backend."""
        wallet_path = tmp_path / "wallets" / "with_birthday.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        # Save a wallet file WITH creation_height
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="abandon " * 11 + "about",
            password="password",
            wallet_type="sw-fb",
            creation_height=790000,
        )

        mock_backend = _make_descriptor_backend()
        mock_get_backend.return_value = mock_backend

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws

        await open_wallet_with_mnemonic(
            wallet_path=wallet_path,
            password="password",
            data_dir=tmp_path,
            sync_on_open=False,
        )

        # Backend should have been told the creation height
        mock_backend.set_wallet_creation_height.assert_called_once_with(790000)

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_open_wallet_without_creation_height_clears_backend_hint(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        """open_wallet_with_mnemonic clears backend creation height when wallet has none."""
        wallet_path = tmp_path / "wallets" / "no_birthday.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        # Save a wallet file WITHOUT creation_height (old format)
        _save_wallet_file(
            wallet_path=wallet_path,
            mnemonic="abandon " * 11 + "about",
            password="password",
            wallet_type="sw",
        )

        mock_backend = _make_neutrino_backend()
        mock_get_backend.return_value = mock_backend

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws_cls.return_value = mock_ws

        await open_wallet_with_mnemonic(
            wallet_path=wallet_path,
            password="password",
            data_dir=tmp_path,
            sync_on_open=False,
        )

        # Backend should be explicitly cleared to avoid stale hint reuse.
        mock_backend.set_wallet_creation_height.assert_called_once_with(None)

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_open_wallet_clears_stale_creation_height_between_wallets(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Opening a wallet without birthday clears stale hint from prior wallet."""
        wallets_dir = tmp_path / "wallets"
        wallets_dir.mkdir(parents=True, exist_ok=True)

        wallet_with_height = wallets_dir / "with_birthday.jmdat"
        wallet_without_height = wallets_dir / "without_birthday.jmdat"

        _save_wallet_file(
            wallet_path=wallet_with_height,
            mnemonic="abandon " * 11 + "about",
            password="password",
            wallet_type="sw-fb",
            creation_height=790000,
        )
        _save_wallet_file(
            wallet_path=wallet_without_height,
            mnemonic="abandon " * 11 + "about",
            password="password",
            wallet_type="sw",
        )

        # Reuse the same backend mock to simulate cached backend instance.
        mock_backend = _make_neutrino_backend()
        mock_get_backend.return_value = mock_backend

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws_cls.return_value = mock_ws

        await open_wallet_with_mnemonic(
            wallet_path=wallet_with_height,
            password="password",
            data_dir=tmp_path,
            sync_on_open=False,
        )
        mock_backend.set_wallet_creation_height.assert_called_once_with(790000)

        mock_backend.set_wallet_creation_height.reset_mock()

        await open_wallet_with_mnemonic(
            wallet_path=wallet_without_height,
            password="password",
            data_dir=tmp_path,
            sync_on_open=False,
        )
        mock_backend.set_wallet_creation_height.assert_called_once_with(None)

    @patch("jmwalletd.wallet_ops._get_network", return_value="mainnet")
    @patch("jmwalletd._backend.get_backend", new_callable=AsyncMock)
    @patch("jmwallet.wallet.service.WalletService")
    async def test_recover_wallet_does_not_store_creation_height(
        self,
        mock_ws_cls: MagicMock,
        mock_get_backend: AsyncMock,
        mock_get_network: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Recovered wallets should NOT have creation_height (unknown birthday)."""
        wallet_path = tmp_path / "wallets" / "recovered.jmdat"
        wallet_path.parent.mkdir(parents=True, exist_ok=True)

        mock_ws = MagicMock()
        mock_ws.sync = AsyncMock()
        mock_ws.sync_with_registered_bonds = AsyncMock()
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()

        await recover_wallet(
            wallet_path=wallet_path,
            password="password",
            wallet_type="sw",
            seedphrase="abandon " * 11 + "about",
            data_dir=tmp_path,
        )

        # Recovered wallet should have no creation_height
        loaded_mnemonic, creation_height = _load_wallet_file(
            wallet_path=wallet_path, password="password"
        )
        assert loaded_mnemonic == "abandon " * 11 + "about"
        assert creation_height is None
