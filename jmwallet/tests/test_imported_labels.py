"""
Tests for import-time CoinJoin label reconstruction.

A wallet recovered from seed (or otherwise imported) has no local CoinJoin
history file, so its coins would all fall back to ``deposit`` / ``non-cj-change``
on display even when they came from CoinJoins. ``reconstruct_imported_labels``
re-derives the correct labels from on-chain data (the same equal-output
heuristic the legacy joinmarket-clientserver uses) and persists them so the
wallet display surfaces ``cj-out`` / ``cj-change``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from jmcore.bitcoin import TxInput, TxOutput, serialize_transaction

from jmwallet.backends.base import Transaction
from jmwallet.history import append_history_entry, create_maker_history_entry
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService

CJ_AMOUNT = 30_000


def _raw_tx(outputs: list[tuple[int, int]]) -> str:
    """Serialize a 1-input tx with the given (value, script-tag) outputs."""
    inp = TxInput.from_hex("aa" * 32, 0)
    outs = [TxOutput(value=value, script=b"\x00\x14" + bytes([tag]) * 20) for value, tag in outputs]
    return serialize_transaction(2, [inp], outs, 0).hex()


def _coinjoin_raw() -> str:
    """A CoinJoin: 3 equal CJ_AMOUNT outputs + 3 distinct change outputs."""
    return _raw_tx(
        [(CJ_AMOUNT, 1), (CJ_AMOUNT, 2), (CJ_AMOUNT, 3), (7_001, 4), (8_002, 5), (9_003, 6)]
    )


def _payment_raw() -> str:
    """A plain payment: one spend output + one change output (not a CoinJoin)."""
    return _raw_tx([(50_000, 1), (1_234, 2)])


def _make_backend(raw_by_txid: dict[str, str]) -> Mock:
    backend = Mock()

    async def _get_tx(txid: str) -> Transaction | None:
        raw = raw_by_txid.get(txid)
        if raw is None:
            return None
        return Transaction(txid=txid, raw=raw, confirmations=3)

    backend.get_transaction = AsyncMock(side_effect=_get_tx)
    return backend


def _utxo(
    *,
    txid: str,
    value: int,
    address: str,
    change: int = 1,
    mixdepth: int = 0,
    index: int = 0,
    locktime: int | None = None,
) -> UTXOInfo:
    path = f"m/84'/1'/{mixdepth}'/{change}/{index}"
    return UTXOInfo(
        txid=txid,
        vout=0,
        value=value,
        address=address,
        confirmations=3,
        scriptpubkey="0014" + "11" * 20,
        path=path,
        mixdepth=mixdepth,
        locktime=locktime,
    )


def _wallet(backend: Mock, tmp_path: Path, mnemonic: str, network: str) -> WalletService:
    return WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=5,
        data_dir=tmp_path,
    )


class TestReconstructImportedLabels:
    @pytest.mark.asyncio
    async def test_classifies_coinjoin_out_and_change(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({"cjtx": _coinjoin_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        cj_out = _utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")
        cj_change = _utxo(txid="cjtx", value=7_001, address="bcrt1qcjchange")
        ws.utxo_cache = {0: [cj_out, cj_change]}

        classified = await ws.reconstruct_imported_labels()

        assert classified == 2
        # Persisted origins map to the display vocabulary.
        types = ws.metadata_store.get_coinjoin_address_types()
        assert types == {"bcrt1qcjout": "cj_out", "bcrt1qcjchange": "change"}
        # In-memory labels are surfaced for the /utxos view.
        assert cj_out.label == "cj-out"
        assert cj_change.label == "cj-change"

    @pytest.mark.asyncio
    async def test_non_coinjoin_is_deposit_or_non_cj_change(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({"paytx": _payment_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        deposit = _utxo(txid="paytx", value=50_000, address="bcrt1qdep", change=0)
        change = _utxo(txid="paytx", value=1_234, address="bcrt1qchg", change=1)
        ws.utxo_cache = {0: [deposit, change]}

        classified = await ws.reconstruct_imported_labels()

        assert classified == 2
        # Non-CoinJoin coins are not surfaced as CoinJoin types in the display.
        assert ws.metadata_store.get_coinjoin_address_types() == {}
        # The origins are still recorded (so the coins are not re-fetched later).
        assert ws.metadata_store.get_address_origins("bcrt1qdep") == {"deposit"}
        assert ws.metadata_store.get_address_origins("bcrt1qchg") == {"non_cj_change"}
        # No CoinJoin label is applied to plain coins.
        assert deposit.label is None
        assert change.label is None

    @pytest.mark.asyncio
    async def test_runs_once_per_process_until_forced(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({"cjtx": _coinjoin_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        ws.utxo_cache = {0: [_utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")]}

        assert await ws.reconstruct_imported_labels() == 1
        # Second call is a no-op (guard flag set); transaction not re-fetched.
        backend.get_transaction.reset_mock()
        assert await ws.reconstruct_imported_labels() == 0
        backend.get_transaction.assert_not_awaited()
        # force=True re-runs, but the coin is already classified so nothing new.
        assert await ws.reconstruct_imported_labels(force=True) == 0

    @pytest.mark.asyncio
    async def test_skips_already_classified_addresses(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({"cjtx": _coinjoin_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        ws.metadata_store.mark_address_used("bcrt1qcjout", "cj_out")
        ws.utxo_cache = {0: [_utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")]}

        assert await ws.reconstruct_imported_labels() == 0
        backend.get_transaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_addresses_in_local_history(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        """Addresses this wallet's own CoinJoin history classifies are authoritative."""
        backend = _make_backend({"cjtx": _coinjoin_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        append_history_entry(
            create_maker_history_entry(
                taker_nick="J5taker",
                cj_amount=CJ_AMOUNT,
                fee_received=100,
                txfee_contribution=50,
                cj_address="bcrt1qcjout",
                change_address="bcrt1qcjchange",
                our_utxos=[("bb" * 32, 0)],
                txid="cjtx",
                network=test_network,
                wallet_fingerprint=ws.wallet_fingerprint,
            ),
            tmp_path,
        )
        ws.utxo_cache = {0: [_utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")]}

        assert await ws.reconstruct_imported_labels() == 0
        backend.get_transaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_fidelity_bonds(self, tmp_path, test_mnemonic, test_network) -> None:
        backend = _make_backend({"bondtx": _coinjoin_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        ws.utxo_cache = {
            0: [_utxo(txid="bondtx", value=CJ_AMOUNT, address="bcrt1qbond", locktime=2_000_000_000)]
        }

        assert await ws.reconstruct_imported_labels() == 0
        backend.get_transaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_metadata_store_is_noop(self, test_mnemonic, test_network) -> None:
        backend = _make_backend({"cjtx": _coinjoin_raw()})
        # No data_dir -> no metadata store -> reconstruction would re-fetch every
        # display, so it is disabled.
        ws = WalletService(
            mnemonic=test_mnemonic, backend=backend, network=test_network, mixdepth_count=5
        )
        ws.utxo_cache = {0: [_utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")]}

        assert await ws.reconstruct_imported_labels() == 0
        backend.get_transaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_backend_failure_degrades_gracefully(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = Mock()
        backend.get_transaction = AsyncMock(return_value=None)
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        utxo = _utxo(txid="missing", value=CJ_AMOUNT, address="bcrt1qcjout")
        ws.utxo_cache = {0: [utxo]}

        assert await ws.reconstruct_imported_labels() == 0
        assert utxo.label is None
        assert ws.metadata_store.get_coinjoin_address_types() == {}

    @pytest.mark.asyncio
    async def test_safe_wrapper_swallows_errors(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = Mock()
        backend.get_transaction = AsyncMock(side_effect=RuntimeError("boom"))
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        ws.utxo_cache = {0: [_utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")]}

        # Must never raise: a labeling failure cannot break sync.
        await ws.reconstruct_imported_state_safe()


class TestImportedLabelDisplayIntegration:
    @pytest.mark.asyncio
    async def test_display_surfaces_cj_out_for_imported_wallet(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({"cjtx": _coinjoin_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        # A real internal address holding a CoinJoin-amount coin.
        cj_addr = ws.get_address(0, 1, 0)
        ws.utxo_cache = {0: [_utxo(txid="cjtx", value=CJ_AMOUNT, address=cj_addr, change=1)]}

        # Before reconstruction the imported coin is mislabeled as non-cj-change.
        before = ws.get_address_info_for_mixdepth(0, 1, history_addresses={})
        assert before[0].status == "non-cj-change"

        await ws.reconstruct_imported_labels()

        # After reconstruction the display surfaces the true CoinJoin origin,
        # even though the local CoinJoin history file is empty.
        after = ws.get_address_info_for_mixdepth(0, 1, history_addresses={})
        assert after[0].address == cj_addr
        assert after[0].status == "cj-out"

    @pytest.mark.asyncio
    async def test_sync_with_registered_bonds_triggers_reconstruction(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        """The bond-aware sync used by /display and /utxos runs reconstruction."""
        backend = _make_backend({})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        # Non-descriptor path: sync_all is the underlying sync.
        ws.backend = object()  # type: ignore[assignment]
        ws.sync_all = AsyncMock(return_value={0: []})
        ws.reconstruct_imported_labels = AsyncMock(return_value=0)  # type: ignore[method-assign]

        await ws.sync_with_registered_bonds()

        ws.reconstruct_imported_labels.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_local_history_wins_over_onchain_guess(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        """A flagged (pending) local-history entry must not be overridden."""
        backend = _make_backend({"cjtx": _coinjoin_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        cj_addr = ws.get_address(0, 1, 0)
        # Persist a metadata cj_out origin (as reconstruction would).
        ws.metadata_store.mark_address_used(cj_addr, "cj_out")
        ws.utxo_cache = {0: [_utxo(txid="cjtx", value=CJ_AMOUNT, address=cj_addr, change=1)]}

        # The caller-provided (local-history) type takes precedence on conflict.
        infos = ws.get_address_info_for_mixdepth(0, 1, history_addresses={cj_addr: "change"})
        assert infos[0].status == "cj-change"
