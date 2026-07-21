"""
Tests for on-chain history reconstruction (imported wallets).

A wallet recovered from seed has no ``history.csv``; the reconstruction pass
rebuilds best-effort maker/taker/send/deposit rows from chain data (tagged
``source="onchain"``) without ever touching protocol-recorded rows.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
import typer
from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    get_txid,
    parse_transaction,
    scriptpubkey_to_address,
    serialize_transaction,
)

from jmwallet.backends.base import Transaction, WalletTxEntry
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.cli.history_cmd import (
    _require_neutrino_history_support,
    _wait_for_complete_core_history,
)
from jmwallet.history import (
    append_history_entry,
    create_taker_history_entry,
    format_yield_generator_report,
    purge_reconstructed_entries,
    read_history,
)
from jmwallet.history_reconstruction import (
    OwnedInput,
    OwnedOutput,
    ReconstructionResult,
    classify_wallet_transaction,
    reconstruct_history_from_chain,
)
from jmwallet.wallet.service import WalletService

NETWORK = "regtest"
CJ_AMOUNT = 30_000
FINGERPRINT = "deadbeef"


def _script(tag: int) -> bytes:
    """A distinct P2WPKH scriptPubKey per tag."""
    return b"\x00\x14" + bytes([tag]) * 20


def _addr(tag: int) -> str:
    """The address for :func:`_script` (round-trips via jmcore helpers)."""
    address = scriptpubkey_to_address(_script(tag), NETWORK)
    assert address is not None
    return address


def _raw_tx(inputs: list[tuple[str, int]], outputs: list[tuple[int, int]]) -> str:
    """Serialize a tx with (prev_txid, vout) inputs and (value, tag) outputs."""
    ins = [TxInput.from_hex(txid, vout) for txid, vout in inputs]
    outs = [TxOutput(value=value, script=_script(tag)) for value, tag in outputs]
    return serialize_transaction(2, ins, outs, 0).hex()


# Wallet address layout (tags -> derivation paths).
OUR_DEPOSIT = 0x11  # m/.../0'/0/0 (mixdepth 0, external)
OUR_CJ_OUT = 0x22  # m/.../1'/0/0 (mixdepth 1, external)
OUR_CHANGE = 0x33  # m/.../0'/1/0 (mixdepth 0, internal)
OUR_CHANGE_2 = 0x34  # m/.../0'/1/1 (mixdepth 0, internal)
FOREIGN_A = 0x44
FOREIGN_B = 0x55
FOREIGN_C = 0x66
FOREIGN_D = 0x77

ADDRESS_PATHS: dict[str, tuple[int, int, int]] = {
    _addr(OUR_DEPOSIT): (0, 0, 0),
    _addr(OUR_CJ_OUT): (1, 0, 0),
    _addr(OUR_CHANGE): (0, 1, 0),
    _addr(OUR_CHANGE_2): (0, 1, 1),
}


class FakeBackend:
    """Minimal backend exposing tx enumeration for the reconstruction engine."""

    supports_tx_enumeration = True

    def __init__(
        self,
        entries: list[WalletTxEntry],
        raw_by_txid: dict[str, str],
        block_times: dict[int, int] | None = None,
    ) -> None:
        self.entries = entries
        self.raw_by_txid = raw_by_txid
        self.block_times = block_times or {}
        self.enumerate_calls = 0

    async def list_wallet_transactions_since(
        self, cursor: str | None
    ) -> tuple[list[WalletTxEntry], str | None]:
        self.enumerate_calls += 1
        return list(self.entries), "tip"

    async def get_transaction(self, txid: str) -> Transaction | None:
        raw = self.raw_by_txid.get(txid)
        if raw is None:
            return None
        entry = next((item for item in self.entries if item.txid == txid), None)
        block_time = (
            self.block_times.get(entry.block_height)
            if entry is not None and entry.block_height is not None
            else None
        )
        return Transaction(txid=txid, raw=raw, confirmations=3, block_time=block_time)

    async def get_block_time(self, block_height: int) -> int:
        return self.block_times.get(block_height, 1_700_000_000)


def _scenario() -> tuple[FakeBackend, dict[str, str]]:
    """Deposit -> taker CoinJoin (internal destination) -> plain send.

    Returns the backend plus a name -> txid map.
    """
    # 1. Deposit: 100_000 sats arrive on our deposit address from an
    #    unknown funder (prev txid outside the wallet set).
    fund_raw = _raw_tx([("aa" * 32, 0)], [(100_000, OUR_DEPOSIT)])
    fund_txid = get_txid(fund_raw)

    # 2. CoinJoin (we are taker): our 100_000 input, our equal output on
    #    mixdepth 1 plus our change; peers provide their own outputs.
    #    net = (30_000 + 68_500) - 100_000 = -1_500 (maker fees + mining share).
    cj_raw = _raw_tx(
        [(fund_txid, 0), ("bb" * 32, 0), ("cc" * 32, 1)],
        [
            (CJ_AMOUNT, OUR_CJ_OUT),
            (CJ_AMOUNT, FOREIGN_A),
            (CJ_AMOUNT, FOREIGN_B),
            (68_500, OUR_CHANGE),
            (5_000, FOREIGN_C),
            (6_000, FOREIGN_D),
        ],
    )
    cj_txid = get_txid(cj_raw)

    # 3. Plain send: spends our change (68_500), pays 50_000 to a foreign
    #    address, 18_000 back as our change -> mining fee 500.
    send_raw = _raw_tx(
        [(cj_txid, 3)],
        [(50_000, FOREIGN_A), (18_000, OUR_CHANGE_2)],
    )
    send_txid = get_txid(send_raw)

    raws = {fund_txid: fund_raw, cj_txid: cj_raw, send_txid: send_raw}
    entries = [
        WalletTxEntry(txid=fund_txid, confirmations=10, block_height=100),
        WalletTxEntry(txid=cj_txid, confirmations=8, block_height=102),
        WalletTxEntry(txid=send_txid, confirmations=5, block_height=105),
    ]
    backend = FakeBackend(entries, raws, block_times={100: 1_000, 102: 2_000, 105: 3_000})
    return backend, {"fund": fund_txid, "cj": cj_txid, "send": send_txid}


async def _run(backend: FakeBackend, data_dir: Path, **kwargs: int) -> ReconstructionResult:
    return await reconstruct_history_from_chain(
        backend,  # type: ignore[arg-type]
        address_paths=ADDRESS_PATHS,
        network=NETWORK,
        wallet_fingerprint=FINGERPRINT,
        data_dir=data_dir,
        **kwargs,  # type: ignore[arg-type]
    )


class TestClassifyWalletTransaction:
    """Pure classification of a parsed transaction into a history role."""

    def _cj_parsed(self):  # type: ignore[no-untyped-def]
        raw = _raw_tx(
            [("aa" * 32, 0)],
            [
                (CJ_AMOUNT, OUR_CJ_OUT),
                (CJ_AMOUNT, FOREIGN_A),
                (CJ_AMOUNT, FOREIGN_B),
                (20_200, OUR_CHANGE),
                (5_000, FOREIGN_C),
                (6_000, FOREIGN_D),
            ],
        )
        return parse_transaction(raw)

    def test_maker_earns_fee(self) -> None:
        """Our inputs, equal output ours, net gain -> maker."""
        parsed = self._cj_parsed()
        owned_inputs = [OwnedInput("ff" * 32, 0, 50_000, _addr(OUR_DEPOSIT), 0)]
        owned_outputs = [
            OwnedOutput(0, CJ_AMOUNT, _addr(OUR_CJ_OUT), 1, True),
            OwnedOutput(3, 20_200, _addr(OUR_CHANGE), 0, False),
        ]
        result = classify_wallet_transaction(parsed, owned_inputs, owned_outputs, False, NETWORK)
        assert result is not None
        assert result.role == "maker"
        # net = 50_200 - 50_000 = 200 earned (cjfee minus mining contribution)
        assert result.fee_received == 200
        assert result.net_fee == 200
        assert result.cj_amount == CJ_AMOUNT
        assert result.peer_count == 3
        assert result.destination_address == _addr(OUR_CJ_OUT)
        assert result.change_address == _addr(OUR_CHANGE)
        assert result.source_mixdepth == 0

    def test_taker_pays_fees(self) -> None:
        """Our inputs, equal output ours, net loss -> taker."""
        parsed = self._cj_parsed()
        owned_inputs = [OwnedInput("ff" * 32, 0, 52_000, _addr(OUR_DEPOSIT), 0)]
        owned_outputs = [
            OwnedOutput(0, CJ_AMOUNT, _addr(OUR_CJ_OUT), 1, True),
            OwnedOutput(3, 20_200, _addr(OUR_CHANGE), 0, False),
        ]
        result = classify_wallet_transaction(parsed, owned_inputs, owned_outputs, False, NETWORK)
        assert result is not None
        assert result.role == "taker"
        # cost = 52_000 - 50_200 = 1_800 (maker fees + mining share, inseparable)
        assert result.total_maker_fees_paid == 1_800
        assert result.net_fee == -1_800
        assert result.peer_count == 3
        assert result.destination_address == _addr(OUR_CJ_OUT)

    def test_taker_sweep_to_external_destination(self) -> None:
        """CoinJoin where no equal output is ours -> taker paying to external."""
        parsed = self._cj_parsed()
        owned_inputs = [OwnedInput("ff" * 32, 0, 32_000, _addr(OUR_DEPOSIT), 2)]
        owned_outputs: list[OwnedOutput] = []
        result = classify_wallet_transaction(parsed, owned_inputs, owned_outputs, True, NETWORK)
        assert result is not None
        assert result.role == "taker"
        # cost = 32_000 - 0 - cj_amount = 2_000 in fees
        assert result.total_maker_fees_paid == 2_000
        assert result.net_fee == -2_000
        assert result.destination_address == ""  # unknown which equal output is ours
        assert result.source_mixdepth == 2

    def test_plain_send_with_exact_mining_fee(self) -> None:
        raw = _raw_tx([("aa" * 32, 0)], [(50_000, FOREIGN_A), (18_000, OUR_CHANGE)])
        parsed = parse_transaction(raw)
        owned_inputs = [OwnedInput("ff" * 32, 0, 68_500, _addr(OUR_DEPOSIT), 0)]
        owned_outputs = [OwnedOutput(1, 18_000, _addr(OUR_CHANGE), 0, False)]
        result = classify_wallet_transaction(parsed, owned_inputs, owned_outputs, True, NETWORK)
        assert result is not None
        assert result.role == "send"
        assert result.cj_amount == 50_000
        assert result.mining_fee_paid == 500
        assert result.net_fee == -500
        assert result.destination_address == _addr(FOREIGN_A)
        assert result.change_address == _addr(OUR_CHANGE)

    def test_plain_send_unknown_fee_when_inputs_shared(self) -> None:
        """Mining fee cannot be attributed when not all inputs are ours."""
        raw = _raw_tx([("aa" * 32, 0)], [(50_000, FOREIGN_A)])
        parsed = parse_transaction(raw)
        owned_inputs = [OwnedInput("ff" * 32, 0, 30_000, _addr(OUR_DEPOSIT), 0)]
        result = classify_wallet_transaction(parsed, owned_inputs, [], False, NETWORK)
        assert result is not None
        assert result.role == "send"
        assert result.mining_fee_paid == 0

    def test_internal_transfer(self) -> None:
        """All outputs ours -> destination is our external-branch output."""
        raw = _raw_tx([("aa" * 32, 0)], [(40_000, OUR_DEPOSIT), (9_500, OUR_CHANGE)])
        parsed = parse_transaction(raw)
        owned_inputs = [OwnedInput("ff" * 32, 0, 50_000, _addr(OUR_CHANGE_2), 0)]
        owned_outputs = [
            OwnedOutput(0, 40_000, _addr(OUR_DEPOSIT), 0, True),
            OwnedOutput(1, 9_500, _addr(OUR_CHANGE), 0, False),
        ]
        result = classify_wallet_transaction(parsed, owned_inputs, owned_outputs, True, NETWORK)
        assert result is not None
        assert result.role == "send"
        assert result.cj_amount == 40_000
        assert result.destination_address == _addr(OUR_DEPOSIT)
        assert result.mining_fee_paid == 500

    def test_deposit(self) -> None:
        raw = _raw_tx([("aa" * 32, 0)], [(100_000, OUR_DEPOSIT)])
        parsed = parse_transaction(raw)
        owned_outputs = [OwnedOutput(0, 100_000, _addr(OUR_DEPOSIT), 0, True)]
        result = classify_wallet_transaction(parsed, [], owned_outputs, False, NETWORK)
        assert result is not None
        assert result.role == "deposit"
        assert result.cj_amount == 100_000
        assert result.destination_address == _addr(OUR_DEPOSIT)
        assert result.net_fee == 0

    def test_deposit_via_someone_elses_coinjoin(self) -> None:
        """Receiving an equal output without contributing inputs is a deposit."""
        parsed = self._cj_parsed()
        owned_outputs = [OwnedOutput(0, CJ_AMOUNT, _addr(OUR_CJ_OUT), 1, True)]
        result = classify_wallet_transaction(parsed, [], owned_outputs, False, NETWORK)
        assert result is not None
        assert result.role == "deposit"
        assert result.cj_amount == CJ_AMOUNT
        assert result.peer_count == 3

    def test_unrelated_transaction_is_ignored(self) -> None:
        raw = _raw_tx([("aa" * 32, 0)], [(50_000, FOREIGN_A)])
        parsed = parse_transaction(raw)
        assert classify_wallet_transaction(parsed, [], [], False, NETWORK) is None


class TestReconstructHistoryFromChain:
    @pytest.mark.asyncio
    async def test_full_scenario(self, tmp_path: Path) -> None:
        backend, txids = _scenario()

        result = await _run(backend, tmp_path)
        assert result.created == 3

        entries = {e.txid: e for e in read_history(tmp_path, wallet_fingerprint=FINGERPRINT)}
        assert set(entries) == set(txids.values())

        deposit = entries[txids["fund"]]
        assert deposit.role == "deposit"
        assert deposit.cj_amount == 100_000
        assert deposit.source == "onchain"
        assert deposit.success is True

        cj = entries[txids["cj"]]
        assert cj.role == "taker"
        assert cj.cj_amount == CJ_AMOUNT
        assert cj.peer_count == 3
        assert cj.total_maker_fees_paid == 1_500
        assert cj.net_fee == -1_500
        assert cj.destination_address == _addr(OUR_CJ_OUT)
        assert cj.change_address == _addr(OUR_CHANGE)
        assert cj.source_mixdepth == 0
        assert cj.utxos_used == f"{txids['fund']}:0"
        assert cj.source_addresses == _addr(OUR_DEPOSIT)
        assert cj.source == "onchain"

        send = entries[txids["send"]]
        assert send.role == "send"
        assert send.cj_amount == 50_000
        assert send.mining_fee_paid == 500
        assert send.destination_address == _addr(FOREIGN_A)
        assert send.change_address == _addr(OUR_CHANGE_2)

        # Timestamps come from block times, so the rows sort chronologically.
        assert deposit.timestamp < cj.timestamp < send.timestamp

    @pytest.mark.asyncio
    async def test_idempotent_second_run(self, tmp_path: Path) -> None:
        backend, _txids = _scenario()
        first = await _run(backend, tmp_path)
        assert first.created == 3

        second = await _run(backend, tmp_path)
        assert second.created == 0
        assert second.skipped_existing == 3
        assert len(read_history(tmp_path, wallet_fingerprint=FINGERPRINT)) == 3

    @pytest.mark.asyncio
    async def test_protocol_entries_always_win(self, tmp_path: Path) -> None:
        """A txid already recorded at protocol time is never reconstructed."""
        backend, txids = _scenario()
        protocol_entry = create_taker_history_entry(
            maker_nicks=["J5A", "J5B"],
            cj_amount=CJ_AMOUNT,
            total_maker_fees=1_000,
            mining_fee=500,
            destination=_addr(OUR_CJ_OUT),
            change_address=_addr(OUR_CHANGE),
            source_mixdepth=0,
            selected_utxos=[(txids["fund"], 0)],
            txid=txids["cj"],
            network=NETWORK,
            success=True,
            failure_reason="",
            wallet_fingerprint=FINGERPRINT,
        )
        append_history_entry(protocol_entry, tmp_path)

        result = await _run(backend, tmp_path)
        assert result.created == 2
        assert result.skipped_existing == 1

        cj_rows = [
            e
            for e in read_history(tmp_path, wallet_fingerprint=FINGERPRINT)
            if e.txid == txids["cj"]
        ]
        assert len(cj_rows) == 1
        assert cj_rows[0].source == "protocol"
        assert cj_rows[0].counterparty_nicks == "J5A,J5B"

    @pytest.mark.asyncio
    async def test_unconfirmed_transactions_skipped(self, tmp_path: Path) -> None:
        backend, _txids = _scenario()
        for entry in backend.entries:
            entry.confirmations = 0
            entry.block_height = None
        result = await _run(backend, tmp_path)
        assert result.created == 0

    @pytest.mark.asyncio
    async def test_max_transactions_cap(self, tmp_path: Path) -> None:
        backend, _txids = _scenario()
        result = await _run(backend, tmp_path, max_transactions=1)
        assert result.created == 1
        assert result.capped is True

    @pytest.mark.asyncio
    async def test_rejects_invalid_transaction_cap(self, tmp_path: Path) -> None:
        backend, _txids = _scenario()
        with pytest.raises(ValueError, match="at least 1"):
            await _run(backend, tmp_path, max_transactions=0)

    @pytest.mark.asyncio
    async def test_backend_without_enumeration_is_noop(self, tmp_path: Path) -> None:
        backend, _txids = _scenario()
        backend.supports_tx_enumeration = False  # type: ignore[attr-defined]
        result = await _run(backend, tmp_path)
        assert result.created == 0

    @pytest.mark.asyncio
    async def test_purge_reconstructed_entries(self, tmp_path: Path) -> None:
        backend, txids = _scenario()
        protocol_entry = create_taker_history_entry(
            maker_nicks=["J5A"],
            cj_amount=CJ_AMOUNT,
            total_maker_fees=1_000,
            mining_fee=500,
            destination="bcrt1qother",
            change_address="",
            source_mixdepth=0,
            selected_utxos=[],
            txid="pp" * 32,
            network=NETWORK,
            success=True,
            failure_reason="",
            wallet_fingerprint=FINGERPRINT,
        )
        append_history_entry(protocol_entry, tmp_path)
        await _run(backend, tmp_path)
        assert len(read_history(tmp_path, wallet_fingerprint=FINGERPRINT)) == 4

        removed = purge_reconstructed_entries(tmp_path, wallet_fingerprint=FINGERPRINT)
        assert removed == 3
        remaining = read_history(tmp_path, wallet_fingerprint=FINGERPRINT)
        assert len(remaining) == 1
        assert remaining[0].source == "protocol"

    @pytest.mark.asyncio
    async def test_purge_scopes_to_fingerprint(self, tmp_path: Path) -> None:
        backend, _txids = _scenario()
        await _run(backend, tmp_path)
        assert purge_reconstructed_entries(tmp_path, wallet_fingerprint="cafebabe") == 0
        assert len(read_history(tmp_path, wallet_fingerprint=FINGERPRINT)) == 3


class TestWalletServiceAutoReconstruction:
    """Gating of the automatic pass run after bond-aware syncs."""

    def _wallet(
        self,
        backend: FakeBackend,
        tmp_path: Path,
        mnemonic: str,
        *,
        reconstruct_history: bool = True,
    ) -> WalletService:
        ws = WalletService(
            mnemonic=mnemonic,
            backend=backend,  # type: ignore[arg-type]
            network=NETWORK,
            mixdepth_count=5,
            data_dir=tmp_path,
            reconstruct_history=reconstruct_history,
        )
        # The engine recognizes coins via the (synced) address cache; inject
        # the scenario's synthetic layout instead of running a full sync.
        ws.address_cache = dict(ADDRESS_PATHS)
        return ws

    @pytest.mark.asyncio
    async def test_auto_runs_for_wallet_without_history(
        self, tmp_path: Path, test_mnemonic: str
    ) -> None:
        backend, _txids = _scenario()
        ws = self._wallet(backend, tmp_path, test_mnemonic)
        created = await ws.reconstruct_imported_history()
        assert created == 3
        entries = read_history(tmp_path, wallet_fingerprint=ws.wallet_fingerprint)
        assert len(entries) == 3
        assert all(e.source == "onchain" for e in entries)

    @pytest.mark.asyncio
    async def test_auto_skips_wallet_with_existing_history(
        self, tmp_path: Path, test_mnemonic: str
    ) -> None:
        backend, _txids = _scenario()
        ws = self._wallet(backend, tmp_path, test_mnemonic)
        append_history_entry(
            create_taker_history_entry(
                maker_nicks=["J5A"],
                cj_amount=CJ_AMOUNT,
                total_maker_fees=1_000,
                mining_fee=500,
                destination="bcrt1qother",
                change_address="",
                source_mixdepth=0,
                selected_utxos=[],
                txid="pp" * 32,
                network=NETWORK,
                success=True,
                failure_reason="",
                wallet_fingerprint=ws.wallet_fingerprint,
            ),
            tmp_path,
        )
        assert await ws.reconstruct_imported_history() == 0
        assert backend.enumerate_calls == 0
        # An explicit force run still reconstructs (CLI path).
        assert await ws.reconstruct_imported_history(force=True) == 3

    @pytest.mark.asyncio
    async def test_auto_runs_once_per_process(self, tmp_path: Path, test_mnemonic: str) -> None:
        backend, _txids = _scenario()
        ws = self._wallet(backend, tmp_path, test_mnemonic)
        await ws.reconstruct_imported_history()
        assert backend.enumerate_calls == 1
        assert await ws.reconstruct_imported_history() == 0
        assert backend.enumerate_calls == 1

    @pytest.mark.asyncio
    async def test_capped_auto_pass_continues_on_later_sync(
        self, tmp_path: Path, test_mnemonic: str
    ) -> None:
        backend, _txids = _scenario()
        ws = self._wallet(backend, tmp_path, test_mnemonic)

        assert await ws.reconstruct_imported_history(max_transactions=1) == 1
        assert ws._imported_history_scanned is False
        assert await ws.reconstruct_imported_history(max_transactions=1) == 1
        assert await ws.reconstruct_imported_history(max_transactions=1) == 1
        assert len(read_history(tmp_path, wallet_fingerprint=ws.wallet_fingerprint)) == 3

    @pytest.mark.asyncio
    async def test_disabled_by_config_toggle(self, tmp_path: Path, test_mnemonic: str) -> None:
        backend, _txids = _scenario()
        ws = self._wallet(backend, tmp_path, test_mnemonic, reconstruct_history=False)
        assert await ws.reconstruct_imported_history() == 0
        assert backend.enumerate_calls == 0
        # force (the CLI command) bypasses the toggle.
        assert await ws.reconstruct_imported_history(force=True) == 3

    @pytest.mark.asyncio
    async def test_defers_while_bitcoin_core_rescan_is_active(
        self, tmp_path: Path, test_mnemonic: str
    ) -> None:
        backend = Mock(spec=DescriptorWalletBackend)
        backend.supports_tx_enumeration = True
        backend.get_rescan_status = AsyncMock(return_value={"in_progress": True, "progress": 0.5})
        backend.list_wallet_transactions_since = AsyncMock()
        ws = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,  # type: ignore[arg-type]
            network=NETWORK,
            mixdepth_count=5,
            data_dir=tmp_path,
        )

        assert await ws.reconstruct_imported_history() == 0
        assert ws._imported_history_scanned is False
        assert ws._imported_history_started is True
        backend.list_wallet_transactions_since.assert_not_awaited()

        # A live protocol row written while Core scans must not cancel the
        # deferred imported-wallet backfill later in this process.
        append_history_entry(
            create_taker_history_entry(
                maker_nicks=["J5A"],
                cj_amount=CJ_AMOUNT,
                total_maker_fees=1_000,
                mining_fee=500,
                destination="bcrt1qother",
                change_address="",
                source_mixdepth=0,
                selected_utxos=[],
                txid="dd" * 32,
                network=NETWORK,
                success=True,
                failure_reason="",
                wallet_fingerprint=ws.wallet_fingerprint,
            ),
            tmp_path,
        )
        backend.get_rescan_status.return_value = {"in_progress": False}
        backend.list_wallet_transactions_since.return_value = ([], None)

        assert await ws.reconstruct_imported_history() == 0
        backend.list_wallet_transactions_since.assert_awaited_once_with(None)

    @pytest.mark.asyncio
    async def test_defers_when_bitcoin_core_rescan_status_is_unavailable(
        self, tmp_path: Path, test_mnemonic: str
    ) -> None:
        backend = Mock(spec=DescriptorWalletBackend)
        backend.supports_tx_enumeration = True
        backend.get_rescan_status = AsyncMock(return_value=None)
        backend.list_wallet_transactions_since = AsyncMock()
        ws = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,  # type: ignore[arg-type]
            network=NETWORK,
            mixdepth_count=5,
            data_dir=tmp_path,
        )

        assert await ws.reconstruct_imported_history() == 0
        assert ws._imported_history_scanned is False
        backend.list_wallet_transactions_since.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_data_dir_is_noop(self, test_mnemonic: str) -> None:
        backend, _txids = _scenario()
        ws = WalletService(
            mnemonic=test_mnemonic,
            backend=backend,  # type: ignore[arg-type]
            network=NETWORK,
            mixdepth_count=5,
        )
        assert await ws.reconstruct_imported_history() == 0


class TestReconstructHistoryCommandGuards:
    """The explicit command must not purge before backend coverage is complete."""

    @pytest.mark.asyncio
    async def test_waits_for_active_core_rescan(self) -> None:
        backend = Mock()
        backend.get_rescan_status = AsyncMock(return_value={"in_progress": True})
        backend.wait_for_rescan_complete = AsyncMock(return_value=True)

        await _wait_for_complete_core_history(backend)

        backend.wait_for_rescan_complete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_unknown_core_rescan_status(self) -> None:
        backend = Mock()
        backend.get_rescan_status = AsyncMock(return_value=None)

        with pytest.raises(typer.Exit) as exc_info:
            await _wait_for_complete_core_history(backend)

        assert exc_info.value.exit_code == 1

    def test_rejects_neutrino_without_transaction_history(self) -> None:
        capabilities = Mock(detected=True, has_tx_enumeration=False)
        backend = Mock(server_capabilities=capabilities)

        with pytest.raises(typer.Exit) as exc_info:
            _require_neutrino_history_support(backend)

        assert exc_info.value.exit_code == 1


class TestHistorySourceColumn:
    """CSV schema behavior for the new ``source`` column and deposit role."""

    def test_legacy_header_without_source_migrates(self, tmp_path: Path) -> None:
        """A pre-``source`` CSV reads back with source defaulted to protocol."""
        legacy_header = (
            "timestamp,completed_at,role,success,failure_reason,confirmations,"
            "confirmed_at,txid,cj_amount,peer_count,counterparty_nicks,"
            "fee_received,txfee_contribution,total_maker_fees_paid,"
            "mining_fee_paid,net_fee,source_mixdepth,destination_address,"
            "change_address,utxos_used,broadcast_method,network,"
            "wallet_fingerprint,source_addresses"
        )
        row = (
            "2024-01-01T00:00:00,2024-01-01T00:10:00,maker,True,,3,"
            "2024-01-01T00:10:00,ab" + "cd" * 31 + ",30000,,J5taker,"
            "250,50,0,0,200,0,bcrt1qcj,bcrt1qchg,ff:0,,regtest,"
            f"{FINGERPRINT},bcrt1qsrc"
        )
        (tmp_path / "history.csv").write_text(f"{legacy_header}\n{row}\n")

        entries = read_history(tmp_path, wallet_fingerprint=FINGERPRINT)
        assert len(entries) == 1
        assert entries[0].source == "protocol"
        assert entries[0].role == "maker"
        assert entries[0].fee_received == 250

    def test_deposit_role_round_trips(self, tmp_path: Path) -> None:
        from jmwallet.history import TransactionHistoryEntry

        entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="deposit",
            success=True,
            txid="ee" * 32,
            cj_amount=100_000,
            destination_address="bcrt1qdep",
            network=NETWORK,
            wallet_fingerprint=FINGERPRINT,
            source="onchain",
        )
        append_history_entry(entry, tmp_path)
        entries = read_history(tmp_path, wallet_fingerprint=FINGERPRINT)
        assert len(entries) == 1
        assert entries[0].role == "deposit"
        assert entries[0].source == "onchain"

    def test_reordered_legacy_header_and_source_column_migrate(self, tmp_path: Path) -> None:
        """Legacy rows and newer shifted rows survive the combined migration."""
        reordered_header = (
            "timestamp,completed_at,role,success,failure_reason,confirmations,"
            "confirmed_at,txid,cj_amount,peer_count,counterparty_nicks,"
            "fee_received,txfee_contribution,total_maker_fees_paid,"
            "mining_fee_paid,net_fee,source_mixdepth,destination_address,"
            "change_address,utxos_used,source_addresses,broadcast_method,"
            "network,wallet_fingerprint"
        )
        aligned_legacy = (
            "2024-01-01T00:00:00,,maker,True,,1,2024-01-01T00:01:00,legacy,"
            f"30000,,J5,250,50,0,0,200,0,bcrt1qcj,bcrt1qchg,aa:0,bcrt1qsrc,,"
            f"regtest,{FINGERPRINT}"
        )
        # Canonical 25-cell order appended against the stale 24-cell header.
        shifted_new = (
            "2024-01-02T00:00:00,,taker,True,,2,2024-01-02T00:01:00,new,"
            f"30000,3,,0,0,1500,0,-1500,0,bcrt1qcj2,bcrt1qchg2,bb:1,,regtest,"
            f"{FINGERPRINT},bcrt1qsrc2,onchain"
        )
        (tmp_path / "history.csv").write_text(
            f"{reordered_header}\n{aligned_legacy}\n{shifted_new}\n"
        )

        entries = {entry.txid: entry for entry in read_history(tmp_path)}
        assert entries["legacy"].source == "protocol"
        assert entries["legacy"].source_addresses == "bcrt1qsrc"
        assert entries["new"].source == "onchain"
        assert entries["new"].source_addresses == "bcrt1qsrc2"
        assert entries["new"].wallet_fingerprint == FINGERPRINT

    def test_invalid_source_defaults_to_protocol(self, tmp_path: Path) -> None:
        from dataclasses import fields

        from jmwallet.history import TransactionHistoryEntry

        header = ",".join(f.name for f in fields(TransactionHistoryEntry))
        row_values = {
            "timestamp": "2024-01-01T00:00:00",
            "role": "taker",
            "success": "True",
            "txid": "ab" * 32,
            "cj_amount": "1000",
            "network": NETWORK,
            "wallet_fingerprint": FINGERPRINT,
            "source": "garbage",
        }
        row = ",".join(row_values.get(f.name, "") for f in fields(TransactionHistoryEntry))
        (tmp_path / "history.csv").write_text(f"{header}\n{row}\n")

        entries = read_history(tmp_path, wallet_fingerprint=FINGERPRINT)
        assert len(entries) == 1
        assert entries[0].source == "protocol"

    def test_deposit_addresses_not_classified_as_cj_out(self, tmp_path: Path) -> None:
        from jmwallet.history import TransactionHistoryEntry, get_address_history_types

        append_history_entry(
            TransactionHistoryEntry(
                timestamp="2024-01-01T00:00:00",
                role="deposit",
                success=True,
                txid="ee" * 32,
                cj_amount=100_000,
                destination_address="bcrt1qdep",
                network=NETWORK,
                wallet_fingerprint=FINGERPRINT,
                source="onchain",
            ),
            tmp_path,
        )
        assert get_address_history_types(tmp_path, wallet_fingerprint=FINGERPRINT) == {}

    def test_reconstructed_maker_is_excluded_from_exact_yield_report(self, tmp_path: Path) -> None:
        protocol = create_taker_history_entry(
            maker_nicks=["J5A"],
            cj_amount=CJ_AMOUNT,
            total_maker_fees=1_000,
            mining_fee=500,
            destination="bcrt1qdest",
            change_address="bcrt1qchange",
            source_mixdepth=0,
            selected_utxos=[],
            txid="aa" * 32,
            network=NETWORK,
            success=True,
            failure_reason="",
            wallet_fingerprint=FINGERPRINT,
        )
        protocol.role = "maker"
        protocol.fee_received = 500
        protocol.net_fee = 450
        append_history_entry(protocol, tmp_path)
        protocol.txid = "bb" * 32
        protocol.source = "onchain"
        protocol.fee_received = 999_999
        append_history_entry(protocol, tmp_path)

        rows = format_yield_generator_report(tmp_path, wallet_fingerprint=FINGERPRINT)
        assert len(rows) == 3
        assert "999999" not in rows[-1]
        assert ",500,450," in rows[-1]
