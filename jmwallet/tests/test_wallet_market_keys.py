"""Market capability integration at the wallet's BIP39 seed boundary."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jmcore.market_keys import MarketKeysClosedError, MarketKeyScope, WalletMarketKeys
from jmcore.market_store import MarketStore, MarketStoreUnavailableError
from jmcore.paths import get_market_store_path, get_used_commitments_path

from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.service import WalletService

MNEMONIC = "all " * 11 + "all"
SCOPE = MarketKeyScope(network="regtest", chain_hash="11" * 32, role="buyer", period=4)


def test_wallet_uses_binary_seed_without_changing_spending_tree(tmp_path: Path) -> None:
    backend = AsyncMock(spec=BlockchainBackend)
    wallet = WalletService(MNEMONIC, backend, network="regtest", data_dir=tmp_path)
    seed = mnemonic_to_seed(MNEMONIC)
    expected = WalletMarketKeys(seed, "regtest")
    assert wallet.market_keys.signing_public_key(SCOPE) == expected.signing_public_key(SCOPE)
    assert wallet.market_keys.encryption_public_key(SCOPE) == expected.encryption_public_key(SCOPE)
    assert wallet.market_wallet_id == expected.ledger_identity()
    spending_key = HDKey.from_seed(seed).derive("m/84'/1'/0'/0/0")
    assert wallet.get_address(0, 0, 0) == spending_key.get_address("regtest")
    assert not list(tmp_path.rglob("*market*"))
    assert backend.mock_calls == []


def test_passphrase_changes_market_keys(tmp_path: Path) -> None:
    backend = AsyncMock(spec=BlockchainBackend)
    first = WalletService(MNEMONIC, backend, network="regtest", data_dir=tmp_path)
    second = WalletService(
        MNEMONIC, backend, network="regtest", data_dir=tmp_path, passphrase="synthetic"
    )
    first_keys, second_keys = first.market_keys, second.market_keys
    assert first_keys.signing_public_key(SCOPE) != second_keys.signing_public_key(SCOPE)
    assert first_keys.encryption_public_key(SCOPE) != second_keys.encryption_public_key(SCOPE)


@pytest.mark.asyncio
async def test_close_revokes_capability_even_when_backend_close_fails(tmp_path: Path) -> None:
    backend = AsyncMock(spec=BlockchainBackend)
    backend.close.side_effect = RuntimeError("synthetic backend failure")
    wallet = WalletService(MNEMONIC, backend, network="regtest", data_dir=tmp_path)
    retained = wallet.market_keys
    with pytest.raises(RuntimeError, match="backend failure"):
        await wallet.close()
    with pytest.raises(MarketKeysClosedError):
        retained.signing_public_key(SCOPE)


def test_explicit_wallet_ledger_activation_and_recovery_guard(tmp_path: Path) -> None:
    backend = AsyncMock(spec=BlockchainBackend)
    wallet = WalletService(MNEMONIC, backend, network="regtest", data_dir=tmp_path)
    path = get_market_store_path(tmp_path)
    with pytest.raises(ValueError, match="confirmed complete history"):
        wallet.activate_market_ledger()
    assert not path.parent.exists()

    wallet.activate_market_ledger(history_confirmed=True)
    with MarketStore(path, wallet_id=wallet.market_wallet_id) as store:
        store.check_commitments_path(get_used_commitments_path(tmp_path))
        assert store.wallet_state() == "ready"
        store.mark_recovery_required()
    with pytest.raises(MarketStoreUnavailableError, match="explicit recovery"):
        wallet.activate_market_ledger(history_confirmed=True)
    assert backend.mock_calls == []


def test_revoked_wallet_cannot_activate_market_ledger(tmp_path: Path) -> None:
    wallet = WalletService(
        MNEMONIC, AsyncMock(spec=BlockchainBackend), network="regtest", data_dir=tmp_path
    )
    wallet.market_keys.close()
    with pytest.raises(MarketKeysClosedError):
        wallet.activate_market_ledger(history_confirmed=True)
    assert not get_market_store_path(tmp_path).exists()
