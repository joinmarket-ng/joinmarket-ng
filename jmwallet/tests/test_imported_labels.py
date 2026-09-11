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

import json
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
        # Imported equal-output analysis is display-only. It cannot grant the
        # protocol-backed md0 merge exemption.
        assert cj_out.coinjoin_output is False
        assert cj_change.coinjoin_output is False

    @pytest.mark.asyncio
    async def test_imported_label_survives_wallet_restart_without_merge_authority(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({"cjtx": _coinjoin_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        original = _utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")
        ws.utxo_cache = {0: [original]}

        assert await ws.reconstruct_imported_labels() == 1
        ws.metadata_store.set_label(original.outpoint, "personal note")

        restarted = _wallet(backend, tmp_path, test_mnemonic, test_network)
        restored = _utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")
        restored.label = "personal note"
        restarted.utxo_cache = {0: [restored]}
        restarted._apply_frozen_state()

        assert restored.coinjoin_output is False
        assert restored.label == "personal note"

    @pytest.mark.asyncio
    async def test_protocol_provenance_survives_restart_without_history_file(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        original = _utxo(txid="a" * 64, value=CJ_AMOUNT, address="bcrt1qcjout")
        entry = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=original.value,
            fee_received=100,
            txfee_contribution=50,
            cj_address=original.address,
            change_address="bcrt1qcjchange",
            our_utxos=[("cc" * 32, 0)],
            txid=original.txid,
            network=test_network,
            wallet_fingerprint=ws.wallet_fingerprint,
            destination_vout=original.vout,
        )
        entry.success = True
        append_history_entry(entry, tmp_path)
        ws.utxo_cache = {0: [original]}

        assert await ws.reconstruct_imported_labels() == 0
        assert original.coinjoin_output is True
        (tmp_path / "history.csv").unlink()

        restarted = _wallet(backend, tmp_path, test_mnemonic, test_network)
        restored = _utxo(txid=original.txid, value=original.value, address=original.address)
        restarted.utxo_cache = {0: [restored]}
        restarted._apply_frozen_state()

        assert restored.coinjoin_output is True
        assert await restarted.reconstruct_imported_labels() == 0
        assert restored.coinjoin_output is True

    @pytest.mark.asyncio
    async def test_forged_coinjoin_shape_cannot_authorize_md0_merge(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({"fake-a": _coinjoin_raw(), "fake-b": _coinjoin_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        deposits = [
            _utxo(txid="fake-a", value=CJ_AMOUNT, address="bcrt1qfakea", index=1),
            _utxo(txid="fake-b", value=CJ_AMOUNT, address="bcrt1qfakeb", index=2),
        ]
        ws.utxo_cache = {0: deposits}

        assert await ws.reconstruct_imported_labels() == 2
        assert [utxo.label for utxo in deposits] == ["cj-out", "cj-out"]
        assert all(not utxo.coinjoin_output for utxo in deposits)
        assert await ws.get_balance_for_offers(0, min_confirmations=1) == CJ_AMOUNT
        with pytest.raises(ValueError, match="privacy reasons"):
            ws.select_utxos(0, CJ_AMOUNT + 1, min_confirmations=1)

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
        # Second call is a no-op (coin classified); transaction not re-fetched.
        backend.get_transaction.reset_mock()
        assert await ws.reconstruct_imported_labels() == 0
        backend.get_transaction.assert_not_awaited()
        # force=True re-runs, but the coin is already classified so nothing new.
        assert await ws.reconstruct_imported_labels(force=True) == 0

    @pytest.mark.asyncio
    async def test_coins_arriving_while_running_are_classified_on_next_sync(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        """A deposit received after the first pass gets its origin without a restart."""
        backend = _make_backend({"cjtx": _coinjoin_raw(), "paytx": _payment_raw()})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        ws.utxo_cache = {0: [_utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")]}
        assert await ws.reconstruct_imported_labels() == 1

        deposit = _utxo(txid="paytx", value=50_000, address="bcrt1qdep", change=0)
        ws.utxo_cache[0].append(deposit)
        backend.get_transaction.reset_mock()

        assert await ws.reconstruct_imported_labels() == 1
        backend.get_transaction.assert_awaited_once_with("paytx")
        assert ws.metadata_store.get_address_origins("bcrt1qdep") == {"deposit"}

    @pytest.mark.asyncio
    async def test_failed_fetch_is_not_retried_until_forced(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = Mock()
        backend.get_transaction = AsyncMock(return_value=None)
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        ws.utxo_cache = {0: [_utxo(txid="missing", value=CJ_AMOUNT, address="bcrt1qcjout")]}

        assert await ws.reconstruct_imported_labels() == 0
        backend.get_transaction.assert_awaited_once()
        # Same unclassified coin: the backend is not queried again on every sync.
        assert await ws.reconstruct_imported_labels() == 0
        backend.get_transaction.assert_awaited_once()
        # Clearing the attempted set (force, or a rescan) retries it.
        assert await ws.reconstruct_imported_labels(force=True) == 0
        assert backend.get_transaction.await_count == 2

    @pytest.mark.asyncio
    async def test_confirmed_history_origins_are_persisted(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        """Own CoinJoin history labels its output/change addresses in the JSONL export."""
        backend = _make_backend({})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        cj_addr = ws.get_address(0, 0, 0)
        change_addr = ws.get_address(0, 1, 0)
        entry = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=CJ_AMOUNT,
            fee_received=100,
            txfee_contribution=50,
            cj_address=cj_addr,
            change_address=change_addr,
            our_utxos=[("cc" * 32, 0)],
            txid="a" * 64,
            network=test_network,
            wallet_fingerprint=ws.wallet_fingerprint,
        )
        entry.success = True
        append_history_entry(entry, tmp_path)
        # Both outputs already spent: no current coin, so this cannot come from
        # the on-chain pass; the address record still gets a detailed origin.
        ws.utxo_cache = {0: []}

        assert await ws.reconstruct_imported_labels() == 0

        backend.get_transaction.assert_not_awaited()
        assert ws.metadata_store.get_address_origins(cj_addr) == {"cj_out"}
        assert ws.metadata_store.get_address_origins(change_addr) == {"cj_change"}
        labels = {
            rec["ref"]: rec["label"]
            for rec in map(json.loads, ws.metadata_store.path.read_text().splitlines())
            if rec["type"] == "addr"
        }
        assert labels[cj_addr] == "jm:used:cj_out"
        assert labels[change_addr] == "jm:used:cj_change"
        # Idempotent: a second pass changes nothing on disk.
        mtime = ws.metadata_store.path.stat().st_mtime_ns
        assert await ws.reconstruct_imported_labels() == 0
        assert ws.metadata_store.path.stat().st_mtime_ns == mtime

    @pytest.mark.asyncio
    async def test_history_origin_upgrades_bare_used_marker(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        cj_addr = ws.get_address(0, 0, 0)
        ws.metadata_store.mark_address_used(cj_addr)
        assert ws.metadata_store.get_address_origins(cj_addr) == set()
        entry = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=CJ_AMOUNT,
            fee_received=100,
            txfee_contribution=50,
            cj_address=cj_addr,
            change_address=ws.get_address(0, 1, 0),
            our_utxos=[("cc" * 32, 0)],
            txid="a" * 64,
            network=test_network,
            wallet_fingerprint=ws.wallet_fingerprint,
        )
        entry.success = True
        append_history_entry(entry, tmp_path)
        ws.utxo_cache = {0: []}

        await ws.reconstruct_imported_labels()

        assert ws.metadata_store.get_address_origins(cj_addr) == {"cj_out"}

    @pytest.mark.asyncio
    async def test_pending_and_foreign_history_addresses_get_no_origin(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        pending_addr = ws.get_address(0, 0, 0)
        # Unconfirmed (success=False): must not be labeled until it confirms.
        append_history_entry(
            create_maker_history_entry(
                taker_nick="J5taker",
                cj_amount=CJ_AMOUNT,
                fee_received=100,
                txfee_contribution=50,
                cj_address=pending_addr,
                change_address=ws.get_address(0, 1, 0),
                our_utxos=[("cc" * 32, 0)],
                txid="a" * 64,
                network=test_network,
                wallet_fingerprint=ws.wallet_fingerprint,
            ),
            tmp_path,
        )
        # Confirmed, but the destination is not one of this wallet's addresses
        # (e.g. a taker paying an external wallet): never written to our file.
        external = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=CJ_AMOUNT,
            fee_received=100,
            txfee_contribution=50,
            cj_address="bcrt1qexternal",
            change_address="bcrt1qforeignchange",
            our_utxos=[("dd" * 32, 0)],
            txid="b" * 64,
            network=test_network,
            wallet_fingerprint=ws.wallet_fingerprint,
        )
        external.success = True
        append_history_entry(external, tmp_path)
        ws.utxo_cache = {0: []}

        await ws.reconstruct_imported_labels()

        assert not ws.metadata_store.is_address_used(pending_addr)
        assert not ws.metadata_store.is_address_used("bcrt1qexternal")
        assert not ws.metadata_store.is_address_used("bcrt1qforeignchange")

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
    async def test_local_history_cj_outputs_are_mergeable_in_md0(
        self, tmp_path, test_mnemonic, test_network
    ) -> None:
        backend = _make_backend({})
        ws = _wallet(backend, tmp_path, test_mnemonic, test_network)
        cj_outputs = [
            _utxo(txid="a" * 64, value=60_000, address="bcrt1qcjout1", index=1),
            _utxo(txid="b" * 64, value=40_000, address="bcrt1qcjout2", index=2),
        ]
        reused_deposit = _utxo(
            txid="d" * 64,
            value=70_000,
            address=cj_outputs[0].address,
            index=1,
        )
        for utxo in cj_outputs:
            entry = create_maker_history_entry(
                taker_nick="J5taker",
                cj_amount=utxo.value,
                fee_received=100,
                txfee_contribution=50,
                cj_address=utxo.address,
                change_address=f"{utxo.address}-change",
                our_utxos=[("cc" * 32, 0)],
                txid=utxo.txid,
                network=test_network,
                wallet_fingerprint=ws.wallet_fingerprint,
                destination_vout=utxo.vout,
            )
            entry.success = True
            append_history_entry(entry, tmp_path)
        ws.utxo_cache = {0: [*cj_outputs, reused_deposit]}

        assert await ws.reconstruct_imported_labels() == 0

        backend.get_transaction.assert_not_awaited()
        assert [utxo.label for utxo in cj_outputs] == ["cj-out", "cj-out"]
        assert all(utxo.coinjoin_output for utxo in cj_outputs)
        assert ws.metadata_store.get_coinjoin_output_outpoints() == {
            utxo.outpoint for utxo in cj_outputs
        }
        assert reused_deposit.label is None
        assert reused_deposit.coinjoin_output is False
        assert await ws.get_balance_for_offers(0, min_confirmations=1) == 100_000
        selected = ws.select_utxos_with_merge(
            0,
            90_000,
            min_confirmations=1,
            merge_algorithm="greedy",
        )
        assert {utxo.outpoint for utxo in selected} == {utxo.outpoint for utxo in cj_outputs}

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
        utxo = _utxo(txid="cjtx", value=CJ_AMOUNT, address="bcrt1qcjout")
        ws.utxo_cache = {0: [utxo]}

        assert await ws.reconstruct_imported_labels() == 0
        backend.get_transaction.assert_not_awaited()
        assert utxo.label is None

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
