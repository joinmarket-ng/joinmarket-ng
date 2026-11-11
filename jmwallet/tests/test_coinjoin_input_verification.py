"""Neutrino discovery candidates require verification before CoinJoin use."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from jmwallet.backends.base import BlockchainBackend, UTXOVerificationResult
from jmwallet.backends.neutrino import NeutrinoBackend
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import InputVerificationUnavailableError, WalletService


@pytest.fixture
def wallet(test_mnemonic: str, tmp_path: Path) -> WalletService:
    backend = Mock(spec=BlockchainBackend)
    backend.requires_neutrino_metadata.return_value = True
    backend.verify_wallet_utxo_with_metadata = AsyncMock()
    service = WalletService(test_mnemonic, backend, network="regtest", data_dir=tmp_path)
    service.utxo_cache[0] = [
        UTXOInfo(
            txid="ab" * 32,
            vout=0,
            value=100_000,
            address=service.get_address(0, 0, 0),
            scriptpubkey="0014" + "ab" * 20,
            confirmations=100,
            height=100,
            path="m/84'/1'/0'/0/0",
            mixdepth=0,
        )
    ]
    return service


@pytest.mark.asyncio
async def test_verified_creation_metadata_replaces_cached_depth(wallet: WalletService) -> None:
    coin = wallet.utxo_cache[0][0]
    wallet.backend.verify_wallet_utxo_with_metadata.return_value = UTXOVerificationResult(
        valid=True, value=coin.value, confirmations=7, block_height=193
    )

    assert await wallet.verify_coinjoin_inputs([coin], 5) == set()
    assert coin.confirmations == 7
    assert coin.height == 193


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        UTXOVerificationResult(valid=False, error="UTXO not found"),
        UTXOVerificationResult(valid=False, error="UTXO spent"),
        UTXOVerificationResult(valid=True, value=100_000, confirmations=1),
        UTXOVerificationResult(valid=True, value=90_000, confirmations=10),
    ],
)
async def test_rejection_does_not_delete_discovery_or_history(
    wallet: WalletService, result: UTXOVerificationResult
) -> None:
    coin = wallet.utxo_cache[0][0]
    wallet._record_history_address(coin.address)
    wallet.backend.verify_wallet_utxo_with_metadata.return_value = result
    persisted = {path: path.read_bytes() for path in wallet.data_dir.glob("*.jsonl")}

    assert await wallet.verify_coinjoin_inputs([coin], 5) == {(coin.txid, coin.vout)}
    assert wallet.utxo_cache[0] == [coin]
    assert coin.address in wallet.addresses_with_history
    assert all(path.read_bytes() == content for path, content in persisted.items())


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["inconclusive", "exception", "missing-metadata"])
async def test_uncertainty_aborts_without_excluding_funds(
    wallet: WalletService, failure: str
) -> None:
    coin = wallet.utxo_cache[0][0]
    if failure == "inconclusive":
        wallet.backend.verify_wallet_utxo_with_metadata.return_value = UTXOVerificationResult(
            valid=False, conclusive=False, error="temporarily unavailable"
        )
    elif failure == "exception":
        wallet.backend.verify_wallet_utxo_with_metadata.side_effect = TimeoutError()
    else:
        coin.height = None

    with pytest.raises(InputVerificationUnavailableError):
        await wallet.verify_coinjoin_inputs([coin], 5)
    assert wallet.utxo_cache[0] == [coin]


@pytest.mark.asyncio
async def test_input_verification_preserves_cancellation(wallet: WalletService) -> None:
    wallet.backend.verify_wallet_utxo_with_metadata.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await wallet.verify_coinjoin_inputs(wallet.utxo_cache[0], 5)


@pytest.mark.asyncio
async def test_core_discovery_contract_is_unchanged(wallet: WalletService) -> None:
    wallet.backend.requires_neutrino_metadata.return_value = False
    assert await wallet.verify_coinjoin_inputs(wallet.utxo_cache[0], 5) == set()
    wallet.backend.verify_wallet_utxo_with_metadata.assert_not_awaited()


@pytest.mark.asyncio
async def test_wallet_height_above_lagging_tip_is_not_conclusive() -> None:
    backend = NeutrinoBackend(neutrino_url="http://localhost:8334", network="regtest")
    backend.get_block_height = AsyncMock(return_value=100)
    try:
        wallet_result = await backend.verify_wallet_utxo_with_metadata(
            "ab" * 32, 0, "0014" + "ab" * 20, 101
        )
        peer_result = await backend.verify_utxo_with_metadata("ab" * 32, 0, "0014" + "ab" * 20, 101)
        assert wallet_result.unavailable
        assert not peer_result.valid
        assert peer_result.conclusive
    finally:
        await backend.close()
