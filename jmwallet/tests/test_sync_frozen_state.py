"""Regression tests: per-mixdepth sync must apply frozen metadata.

``WalletService.get_balance`` cold-syncs a single mixdepth through
``sync_mixdepth``. Before the fix, that path skipped ``_apply_frozen_state``
(only the full-sync paths called it), so frozen UTXOs were reported as
spendable whenever the cache was cold. The tumbler then planned sweeps of
mixdepths whose funds were entirely frozen, and stalled at execution time.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jmwallet.backends.base import UTXO
from jmwallet.wallet.service import WalletService
from jmwallet.wallet.utxo_metadata import UTXOMetadataStore

FROZEN_TXID = "aa" * 32


def _make_wallet(tmp_path: Path, test_mnemonic: str) -> WalletService:
    backend = MagicMock()
    backend.close = AsyncMock()
    ws = WalletService(
        mnemonic=test_mnemonic,
        backend=backend,
        network="regtest",
        data_dir=tmp_path,
        mixdepth_count=2,
    )
    ws.metadata_store = UTXOMetadataStore(path=tmp_path / "wallet_metadata_test.jsonl")

    funded_address = ws.get_address(0, 0, 0)

    async def get_utxos(addresses: list[str]) -> list[UTXO]:
        if funded_address in addresses:
            return [
                UTXO(
                    txid=FROZEN_TXID,
                    vout=0,
                    value=50_000,
                    address=funded_address,
                    confirmations=6,
                    scriptpubkey="0014" + "ab" * 20,
                    height=100,
                )
            ]
        return []

    backend.get_utxos = AsyncMock(side_effect=get_utxos)
    return ws


@pytest.mark.asyncio
async def test_cold_cache_balance_excludes_frozen_utxos(tmp_path: Path, test_mnemonic: str) -> None:
    ws = _make_wallet(tmp_path, test_mnemonic)
    # Freeze the UTXO in the metadata store (as a previous session would have).
    ws.metadata_store.freeze(f"{FROZEN_TXID}:0")

    # Cold cache: get_balance triggers sync_mixdepth internally.
    assert ws.utxo_cache == {}
    balance = await ws.get_balance(0)

    assert balance == 0
    cached = ws.utxo_cache[0]
    assert len(cached) == 1
    assert cached[0].frozen is True


@pytest.mark.asyncio
async def test_cold_cache_balance_counts_unfrozen_utxos(tmp_path: Path, test_mnemonic: str) -> None:
    """Sanity check: without frozen metadata the same UTXO is spendable."""
    ws = _make_wallet(tmp_path, test_mnemonic)

    balance = await ws.get_balance(0)

    assert balance == 50_000
    assert ws.utxo_cache[0][0].frozen is False
