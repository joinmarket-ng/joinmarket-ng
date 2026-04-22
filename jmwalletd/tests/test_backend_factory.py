"""Tests for the per-wallet backend factory (:mod:`jmwalletd._backend`)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from jmwalletd._backend import (
    _wallet_name_for_mnemonic,
    get_backend,
    reset_backend,
    wallet_name_for_service,
)


@pytest.fixture(autouse=True)
def _clear_backend_cache() -> Iterator[None]:
    """Ensure each test starts with a clean backend cache."""
    reset_backend()
    yield
    reset_backend()


def _make_settings_stub(
    *,
    backend_type: str = "descriptor_wallet",
    descriptor_wallet_name: str = "jm_default",
    network: str = "regtest",
) -> object:
    """Build a minimal stub of ``jmcore.settings.get_settings`` output."""

    class _Bitcoin:
        def __init__(self) -> None:
            self.backend_type = backend_type
            self.descriptor_wallet_name = descriptor_wallet_name
            self.rpc_url = "http://127.0.0.1:18443"
            self.rpc_user = "test"
            self.rpc_password = "test"

    class _Network:
        def __init__(self) -> None:
            self.value = network

    class _NetworkConfig:
        def __init__(self) -> None:
            self.network = _Network()

    class _Settings:
        def __init__(self) -> None:
            self.bitcoin = _Bitcoin()
            self.network_config = _NetworkConfig()
            self.wallet = type("_W", (), {"scan_start_height": 0, "scan_lookback_blocks": 0})()

        def get_neutrino_add_peers(self) -> list[str]:
            return []

    return _Settings()


class TestPerWalletDescriptorBackend:
    """Descriptor backend cache must key on per-wallet bitcoind wallet name.

    Before this isolation fix the daemon shared a single bitcoind descriptor
    wallet across all JoinMarket wallets in a single process lifetime. The
    second unlocked JM wallet's descriptors were never imported (Bitcoin
    Core saw the wallet as already set up), so ``listunspent`` returned
    zero UTXOs.
    """

    @pytest.mark.asyncio
    async def test_different_mnemonics_get_different_backends(self, tmp_path: Path) -> None:
        mnemonic_a = "abandon " * 11 + "about"
        mnemonic_b = "legal " * 11 + "winner"

        with patch("jmcore.settings.get_settings", return_value=_make_settings_stub()):
            backend_a = await get_backend(tmp_path, mnemonic=mnemonic_a, network="regtest")
            backend_b = await get_backend(tmp_path, mnemonic=mnemonic_b, network="regtest")

        assert backend_a is not backend_b
        assert backend_a.wallet_name != backend_b.wallet_name

    @pytest.mark.asyncio
    async def test_same_mnemonic_reuses_cached_backend(self, tmp_path: Path) -> None:
        mnemonic = "abandon " * 11 + "about"
        with patch("jmcore.settings.get_settings", return_value=_make_settings_stub()):
            first = await get_backend(tmp_path, mnemonic=mnemonic, network="regtest")
            second = await get_backend(tmp_path, mnemonic=mnemonic, network="regtest")
        assert first is second

    @pytest.mark.asyncio
    async def test_force_new_does_not_pollute_cache(self, tmp_path: Path) -> None:
        mnemonic = "abandon " * 11 + "about"
        with patch("jmcore.settings.get_settings", return_value=_make_settings_stub()):
            cached = await get_backend(tmp_path, mnemonic=mnemonic, network="regtest")
            forced = await get_backend(
                tmp_path, force_new=True, mnemonic=mnemonic, network="regtest"
            )
            refetched = await get_backend(tmp_path, mnemonic=mnemonic, network="regtest")
        assert forced is not cached
        assert refetched is cached

    @pytest.mark.asyncio
    async def test_wallet_service_resolves_same_name_as_mnemonic(self, tmp_path: Path) -> None:
        """wallet_service-based lookup must match the mnemonic-based one."""
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
        from jmwallet.wallet.service import WalletService

        mnemonic = "abandon " * 11 + "about"
        # Build a real WalletService (its backend is a throwaway stub).
        ws = WalletService(
            mnemonic=mnemonic,
            backend=DescriptorWalletBackend(
                rpc_url="http://x",
                rpc_user="u",
                rpc_password="p",
                wallet_name="throwaway",
            ),
            data_dir=tmp_path,
            network="regtest",
        )

        expected = _wallet_name_for_mnemonic(mnemonic, "regtest")
        assert wallet_name_for_service(ws) == expected

    @pytest.mark.asyncio
    async def test_mnemonic_without_network_raises(self, tmp_path: Path) -> None:
        with (
            patch("jmcore.settings.get_settings", return_value=_make_settings_stub()),
            pytest.raises(ValueError, match="network is required"),
        ):
            await get_backend(tmp_path, mnemonic="abandon " * 11 + "about")

    @pytest.mark.asyncio
    async def test_no_mnemonic_falls_back_to_settings_wallet_name(self, tmp_path: Path) -> None:
        settings = _make_settings_stub(descriptor_wallet_name="legacy_jm_wallet")
        with patch("jmcore.settings.get_settings", return_value=settings):
            backend = await get_backend(tmp_path)
        assert backend.wallet_name == "legacy_jm_wallet"
