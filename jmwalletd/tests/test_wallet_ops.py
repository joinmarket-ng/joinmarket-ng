"""Tests for jmwalletd.wallet_ops — wallet file operations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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

        ws, seedphrase = await create_wallet(
            wallet_path=wallet_path,
            password="password",
            wallet_type="sw-fb",
            data_dir=tmp_path,
        )
        assert ws is mock_ws
        assert isinstance(seedphrase, str)
        assert len(seedphrase.split()) >= 12
        assert wallet_path.exists()

        # Verify network was passed through.
        mock_ws_cls.assert_called_once()
        assert mock_ws_cls.call_args.kwargs["network"] == "mainnet"

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
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()

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

        # Recovery with descriptor backend: needs full rescan (default rescan=True).
        mock_ws.setup_descriptor_wallet.assert_awaited_once_with()

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
        mock_ws.setup_descriptor_wallet = AsyncMock()
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
        mock_ws.sync.assert_awaited_once()


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
        mock_ws.setup_descriptor_wallet = AsyncMock()
        mock_ws_cls.return_value = mock_ws
        mock_get_backend.return_value = _make_descriptor_backend()

        ws = await open_wallet(
            wallet_path=wallet_path,
            password="password",
            data_dir=tmp_path,
        )
        assert ws is mock_ws
        mock_ws_cls.assert_called_once()
        assert mock_ws_cls.call_args.kwargs["network"] == "mainnet"

        # Descriptor backend: setup_descriptor_wallet called with default rescan.
        mock_ws.setup_descriptor_wallet.assert_awaited_once_with()

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
        # sync() must still be called.
        mock_ws.sync.assert_awaited_once()

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
