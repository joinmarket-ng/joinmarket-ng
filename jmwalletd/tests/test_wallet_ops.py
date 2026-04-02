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


def _make_descriptor_backend() -> MagicMock:
    """Return a mock that passes isinstance checks for DescriptorWalletBackend."""
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

    mock = MagicMock(spec=DescriptorWalletBackend)
    return mock


def _make_neutrino_backend() -> MagicMock:
    """Return a mock that does NOT pass isinstance checks for DescriptorWalletBackend."""
    from jmwallet.backends.neutrino import NeutrinoBackend

    mock = MagicMock(spec=NeutrinoBackend)
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

        loaded_mnemonic = _load_wallet_file(wallet_path=wallet_path, password=password)
        assert loaded_mnemonic == mnemonic

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
