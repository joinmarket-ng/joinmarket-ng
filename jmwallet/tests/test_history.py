"""
Tests for transaction history tracking.
"""

from __future__ import annotations

import csv
import struct
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jmwallet.backends.base import Transaction
from jmwallet.history import (
    YIELD_GENERATOR_REPORT_HEADER,
    HistoryWriteError,
    TransactionHistoryEntry,
    _parse_utxos,
    append_history_entry,
    cleanup_stale_pending_transactions,
    create_maker_history_entry,
    create_send_history_entry,
    create_taker_history_entry,
    detect_coinjoin_peer_count,
    format_yield_generator_report,
    get_address_history_types,
    get_history_stats,
    get_history_stats_for_period,
    get_pending_transactions,
    get_used_addresses,
    mark_pending_transaction_failed,
    read_history,
    update_all_pending_transactions,
    update_awaiting_transaction_signed,
    update_pending_transaction_txid,
    update_send_awaiting_broadcast,
    update_taker_awaiting_transaction_broadcast,
    update_transaction_confirmation,
    update_transaction_confirmation_with_detection,
    update_transaction_peer_count,
)


def _make_pending_maker_entry(
    *,
    cj_address: str = "bc1qtest...",
    change_address: str = "bc1qchange...",
    txid: str = "",
    taker_nick: str = "J5taker",
    cj_amount: int = 1_000_000,
    fee_received: int = 250,
    txfee_contribution: int = 50,
    our_utxos: list[tuple[str, int]] | None = None,
    network: str = "mainnet",
) -> TransactionHistoryEntry:
    """Create a pending maker history entry with standard defaults."""
    return create_maker_history_entry(
        taker_nick=taker_nick,
        cj_amount=cj_amount,
        fee_received=fee_received,
        txfee_contribution=txfee_contribution,
        cj_address=cj_address,
        change_address=change_address,
        our_utxos=our_utxos or [("abc123", 0)],
        txid=txid,
        network=network,
    )


def _make_pending_taker_entry(
    *,
    destination: str = "bc1qdest...",
    change_address: str = "bc1qchange...",
    txid: str = "",
    maker_nicks: list[str] | None = None,
    cj_amount: int = 1_000_000,
    total_maker_fees: int = 500,
    mining_fee: int = 100,
    source_mixdepth: int = 0,
    selected_utxos: list[tuple[str, int]] | None = None,
    broadcast_method: str = "self",
    network: str = "mainnet",
    failure_reason: str | None = None,
) -> TransactionHistoryEntry:
    """Create a pending taker history entry with standard defaults."""
    kwargs: dict[str, object] = dict(
        maker_nicks=maker_nicks or ["J5maker1"],
        cj_amount=cj_amount,
        total_maker_fees=total_maker_fees,
        mining_fee=mining_fee,
        destination=destination,
        change_address=change_address,
        source_mixdepth=source_mixdepth,
        selected_utxos=selected_utxos or [("utxo1", 0)],
        txid=txid,
        broadcast_method=broadcast_method,
        network=network,
    )
    if failure_reason is not None:
        kwargs["failure_reason"] = failure_reason
    return create_taker_history_entry(**kwargs)  # type: ignore[arg-type]


class TestTransactionHistoryEntry:
    """Tests for TransactionHistoryEntry dataclass."""

    def test_default_values(self) -> None:
        """Test default values are set correctly."""
        entry = TransactionHistoryEntry(timestamp="2024-01-01T00:00:00")
        assert entry.role == "taker"
        assert entry.success is True
        assert entry.cj_amount == 0
        assert entry.net_fee == 0
        assert entry.network == "mainnet"

    def test_maker_entry(self) -> None:
        """Test maker entry creation."""
        entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            txid="abc123" * 10 + "abcd",
            cj_amount=1_000_000,
            fee_received=250,
            txfee_contribution=100,
            net_fee=150,
        )
        assert entry.role == "maker"
        assert entry.fee_received == 250
        assert entry.net_fee == 150

    def test_taker_entry(self) -> None:
        """Test taker entry creation."""
        entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="taker",
            txid="def456" * 10 + "defg",
            cj_amount=500_000,
            total_maker_fees_paid=1000,
            mining_fee_paid=500,
            net_fee=-1500,
        )
        assert entry.role == "taker"
        assert entry.total_maker_fees_paid == 1000
        assert entry.net_fee == -1500


class TestAppendAndReadHistory:
    """Tests for appending and reading history."""

    def test_append_and_read_single_entry(self, temp_data_dir: Path) -> None:
        """Test appending and reading a single entry."""
        entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="taker",
            txid="abc123def456" * 5 + "abcd",
            cj_amount=1_000_000,
        )

        append_history_entry(entry, temp_data_dir)
        entries = read_history(temp_data_dir)

        assert len(entries) == 1
        assert entries[0].txid == entry.txid
        assert entries[0].cj_amount == 1_000_000

    def test_append_multiple_entries(self, temp_data_dir: Path) -> None:
        """Test appending multiple entries."""
        for i in range(3):
            entry = TransactionHistoryEntry(
                timestamp=f"2024-01-0{i + 1}T00:00:00",
                role="maker" if i % 2 == 0 else "taker",
                txid=f"txid{i}" * 16,
                cj_amount=(i + 1) * 100_000,
            )
            append_history_entry(entry, temp_data_dir)

        entries = read_history(temp_data_dir)
        assert len(entries) == 3

    def test_append_history_failure_raises_error(self, temp_data_dir: Path) -> None:
        """Test that append_history_entry raises HistoryWriteError on failure."""
        entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="taker",
            txid="fail_txid" * 16,
            cj_amount=1_000_000,
        )

        with patch("jmwallet.history.open", side_effect=OSError("simulated write error")):
            with pytest.raises(HistoryWriteError, match="simulated write error"):
                append_history_entry(entry, temp_data_dir)

    def test_read_with_role_filter(self, temp_data_dir: Path) -> None:
        """Test reading with role filter."""
        # Add maker entry
        maker_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            txid="maker_tx" * 8,
            cj_amount=500_000,
        )
        append_history_entry(maker_entry, temp_data_dir)

        # Add taker entry
        taker_entry = TransactionHistoryEntry(
            timestamp="2024-01-02T00:00:00",
            role="taker",
            txid="taker_tx" * 8,
            cj_amount=600_000,
        )
        append_history_entry(taker_entry, temp_data_dir)

        # Read only maker entries
        maker_entries = read_history(temp_data_dir, role_filter="maker")
        assert len(maker_entries) == 1
        assert maker_entries[0].role == "maker"

        # Read only taker entries
        taker_entries = read_history(temp_data_dir, role_filter="taker")
        assert len(taker_entries) == 1
        assert taker_entries[0].role == "taker"

    def test_read_with_limit(self, temp_data_dir: Path) -> None:
        """Test reading with limit."""
        for i in range(5):
            entry = TransactionHistoryEntry(
                timestamp=f"2024-01-0{i + 1}T00:00:00",
                txid=f"txid{i}" * 16,
                cj_amount=(i + 1) * 100_000,
            )
            append_history_entry(entry, temp_data_dir)

        entries = read_history(temp_data_dir, limit=3)
        assert len(entries) == 3
        # Most recent first
        assert entries[0].timestamp == "2024-01-05T00:00:00"

    def test_read_empty_history(self, temp_data_dir: Path) -> None:
        """Test reading when no history exists."""
        entries = read_history(temp_data_dir)
        assert entries == []

    def test_on_disk_order_is_chronological_after_update(self, temp_data_dir: Path) -> None:
        """After an atomic rewrite the CSV rows must be oldest-first.

        Regression test: previously ``_write_history_entries_atomic`` received
        entries already sorted newest-first by ``read_history`` and wrote them
        back in that reversed order.  A subsequent ``append_history_entry`` call
        would then place the new (newest) entry at the *bottom*, making the
        on-disk file non-chronological and confusing for users inspecting the
        raw CSV.
        """
        import csv as csv_mod

        # Append two entries with increasing timestamps.
        for i in range(3):
            entry = TransactionHistoryEntry(
                timestamp=f"2024-01-0{i + 1}T10:00:00",
                role="maker",
                txid=f"{'a' * 60}{i:04d}",
                cj_amount=1_000_000,
                failure_reason="Awaiting transaction",
            )
            append_history_entry(entry, temp_data_dir)

        # Trigger an atomic rewrite (update_awaiting_transaction_signed reads then rewrites).
        update_awaiting_transaction_signed(
            destination_address="",
            txid=f"{'a' * 60}0000",
            fee_received=500,
            txfee_contribution=50,
            data_dir=temp_data_dir,
        )

        # Append a new entry after the rewrite.
        new_entry = TransactionHistoryEntry(
            timestamp="2024-01-04T10:00:00",
            role="maker",
            txid="b" * 64,
            cj_amount=2_000_000,
        )
        append_history_entry(new_entry, temp_data_dir)

        # Read the raw CSV and check rows are in ascending timestamp order.
        history_path = temp_data_dir / "history.csv"
        with open(history_path, newline="", encoding="utf-8") as f:
            reader = csv_mod.DictReader(f)
            timestamps = [row["timestamp"] for row in reader]

        assert timestamps == sorted(timestamps), (
            f"On-disk rows are not in chronological order: {timestamps}"
        )


class TestParseUtxos:
    """Tests for _parse_utxos helper."""

    def test_empty_string(self) -> None:
        """Test parsing UTXOs from empty string."""
        assert _parse_utxos("") == set()

    def test_whitespace_only(self) -> None:
        """Test parsing UTXOs from whitespace-only string."""
        assert _parse_utxos("  ") == set()

    def test_single_utxo(self) -> None:
        """Test parsing a single UTXO."""
        assert _parse_utxos("aabb0011:0") == {"aabb0011:0"}

    def test_multiple_utxos(self) -> None:
        """Test parsing multiple UTXOs."""
        assert _parse_utxos("aabb0011:0,ccdd2233:1,eeff4455:2") == {
            "aabb0011:0",
            "ccdd2233:1",
            "eeff4455:2",
        }

    def test_two_utxos(self) -> None:
        """Test parsing two UTXOs."""
        assert _parse_utxos("aabb0011:0,ccdd2233:1") == {"aabb0011:0", "ccdd2233:1"}


class TestHistoryStats:
    """Tests for aggregate statistics."""

    def test_empty_stats(self, temp_data_dir: Path) -> None:
        """Test stats with no history."""
        stats = get_history_stats(temp_data_dir)
        assert stats["total_coinjoins"] == 0
        assert stats["maker_coinjoins"] == 0
        assert stats["taker_coinjoins"] == 0
        assert stats["total_volume"] == 0
        assert stats["successful_volume"] == 0
        assert stats["utxos_disclosed"] == 0

    def test_stats_with_entries(self, temp_data_dir: Path) -> None:
        """Test stats with multiple entries."""
        # Add maker entry
        maker_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            txid="maker_tx" * 8,
            cj_amount=1_000_000,
            fee_received=500,
            success=True,
            utxos_used="aabb0011:0,ccdd2233:1",
        )
        append_history_entry(maker_entry, temp_data_dir)

        # Add taker entry
        taker_entry = TransactionHistoryEntry(
            timestamp="2024-01-02T00:00:00",
            role="taker",
            txid="taker_tx" * 8,
            cj_amount=2_000_000,
            total_maker_fees_paid=1000,
            mining_fee_paid=200,
            success=True,
        )
        append_history_entry(taker_entry, temp_data_dir)

        stats = get_history_stats(temp_data_dir)
        assert stats["total_coinjoins"] == 2
        assert stats["maker_coinjoins"] == 1
        assert stats["taker_coinjoins"] == 1
        assert stats["total_volume"] == 3_000_000
        assert stats["successful_volume"] == 3_000_000
        assert stats["total_fees_earned"] == 500
        assert stats["total_fees_paid"] == 1200
        assert stats["success_rate"] == 100.0
        assert stats["utxos_disclosed"] == 2  # 2 UTXOs from maker entry

    def test_failed_maker_entry_excluded_from_earnings(self, temp_data_dir: Path) -> None:
        """Regression: failed maker entries with a signed-but-never-broadcast tx
        still have ``fee_received`` set, but must not inflate ``total_fees_earned``.

        See the notification bug where a failed + a successful round both showed
        fee_received=410 and the daily summary reported 820 sats earned instead
        of 410.
        """
        failed_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            completed_at="2024-01-01T01:00:00",
            role="maker",
            success=False,
            failure_reason="Transaction not found after 60 minutes - likely never broadcast",
            txid="failed_tx" * 8,
            cj_amount=1_328_246,
            fee_received=410,  # recorded at signing time, but never broadcast
        )
        success_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T02:00:00",
            completed_at="2024-01-01T02:30:00",
            role="maker",
            success=True,
            txid="ok_tx" * 13,
            cj_amount=1_328_246,
            fee_received=410,
        )
        append_history_entry(failed_entry, temp_data_dir)
        append_history_entry(success_entry, temp_data_dir)

        stats = get_history_stats(temp_data_dir)
        assert stats["total_coinjoins"] == 2
        assert stats["successful_coinjoins"] == 1
        assert stats["failed_coinjoins"] == 1
        # Only the successful round's fee should be counted.
        assert stats["total_fees_earned"] == 410

    def test_send_entries_excluded_from_coinjoin_stats(self, temp_data_dir: Path) -> None:
        """``role="send"`` entries must not skew CoinJoin success rate / volume / counts."""
        cj_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            success=True,
            txid="cj_tx" * 13,
            cj_amount=1_000_000,
            fee_received=500,
            utxos_used="aabb:0",
        )
        send_entry = create_send_history_entry(
            destination="bc1qdest1234567890abcdef1234567890abcdef1234",
            change_address="bc1qchange234567890abcdef1234567890abcdef12",
            amount=5_000_000,
            mining_fee=300,
            source_mixdepth=2,
            selected_utxos=[("send_utxo", 0)],
            txid="send_tx" * 9 + "abcd",
            success=True,
        )
        append_history_entry(cj_entry, temp_data_dir)
        append_history_entry(send_entry, temp_data_dir)

        stats = get_history_stats(temp_data_dir)
        # The send must not be counted as a CoinJoin.
        assert stats["total_coinjoins"] == 1
        assert stats["maker_coinjoins"] == 1
        assert stats["taker_coinjoins"] == 0
        assert stats["successful_coinjoins"] == 1
        assert stats["total_volume"] == 1_000_000  # only the CJ
        assert stats["success_rate"] == 100.0
        # But the send addresses ARE recorded as used (so the next-unused
        # pointer skips them on subsequent syncs).
        used = get_used_addresses(temp_data_dir)
        assert send_entry.destination_address in used
        assert send_entry.change_address in used

    def test_failed_taker_entry_excluded_from_fees_paid(self, temp_data_dir: Path) -> None:
        """Regression: failed taker entries should not contribute to ``total_fees_paid``."""
        failed_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            completed_at="2024-01-01T01:00:00",
            role="taker",
            success=False,
            failure_reason="aborted",
            txid="failed_tk" * 8,
            cj_amount=2_000_000,
            total_maker_fees_paid=800,
            mining_fee_paid=200,
        )
        success_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T02:00:00",
            completed_at="2024-01-01T02:30:00",
            role="taker",
            success=True,
            txid="ok_tk" * 13,
            cj_amount=2_000_000,
            total_maker_fees_paid=800,
            mining_fee_paid=200,
        )
        append_history_entry(failed_entry, temp_data_dir)
        append_history_entry(success_entry, temp_data_dir)

        stats = get_history_stats(temp_data_dir)
        assert stats["total_fees_paid"] == 1000  # only the successful round


class TestHistoryStatsForPeriod:
    """Tests for time-filtered aggregate statistics."""

    def test_empty_history(self, temp_data_dir: Path) -> None:
        """Test stats for period with no history at all."""
        stats = get_history_stats_for_period(24, data_dir=temp_data_dir)
        assert stats["total_coinjoins"] == 0
        assert stats["successful_coinjoins"] == 0
        assert stats["failed_coinjoins"] == 0
        assert stats["total_volume"] == 0
        assert stats["successful_volume"] == 0
        assert stats["utxos_disclosed"] == 0

    def test_entries_within_period(self, temp_data_dir: Path) -> None:
        """Test that recent entries are included in the period."""
        now = datetime.now()
        recent_ts = (now - timedelta(hours=1)).isoformat()

        entry = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="recent_tx" * 8,
            cj_amount=1_000_000,
            fee_received=500,
            success=True,
        )
        append_history_entry(entry, temp_data_dir)

        stats = get_history_stats_for_period(24, data_dir=temp_data_dir)
        assert stats["total_coinjoins"] == 1
        assert stats["successful_coinjoins"] == 1
        assert stats["total_volume"] == 1_000_000
        assert stats["total_fees_earned"] == 500

    def test_entries_outside_period(self, temp_data_dir: Path) -> None:
        """Test that old entries are excluded from the period."""
        old_ts = (datetime.now() - timedelta(hours=48)).isoformat()

        entry = TransactionHistoryEntry(
            timestamp=old_ts,
            role="maker",
            txid="old_tx" * 8,
            cj_amount=1_000_000,
            fee_received=500,
            success=True,
        )
        append_history_entry(entry, temp_data_dir)

        stats = get_history_stats_for_period(24, data_dir=temp_data_dir)
        assert stats["total_coinjoins"] == 0

    def test_mixed_entries(self, temp_data_dir: Path) -> None:
        """Test with entries both inside and outside the period."""
        now = datetime.now()
        recent_ts = (now - timedelta(hours=2)).isoformat()
        old_ts = (now - timedelta(hours=48)).isoformat()

        recent = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="recent_tx" * 8,
            cj_amount=1_000_000,
            fee_received=500,
            success=True,
        )
        old = TransactionHistoryEntry(
            timestamp=old_ts,
            role="maker",
            txid="old_tx1234" * 7,
            cj_amount=2_000_000,
            fee_received=1000,
            success=True,
        )
        append_history_entry(recent, temp_data_dir)
        append_history_entry(old, temp_data_dir)

        stats = get_history_stats_for_period(24, data_dir=temp_data_dir)
        assert stats["total_coinjoins"] == 1
        assert stats["total_volume"] == 1_000_000
        assert stats["total_fees_earned"] == 500

    def test_role_filter(self, temp_data_dir: Path) -> None:
        """Test filtering by role within the period."""
        now = datetime.now()
        recent_ts = (now - timedelta(hours=1)).isoformat()

        maker_entry = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="maker_tx1" * 8,
            cj_amount=1_000_000,
            fee_received=500,
            success=True,
        )
        taker_entry = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="taker",
            txid="taker_tx1" * 8,
            cj_amount=2_000_000,
            total_maker_fees_paid=800,
            mining_fee_paid=200,
            success=True,
        )
        append_history_entry(maker_entry, temp_data_dir)
        append_history_entry(taker_entry, temp_data_dir)

        stats = get_history_stats_for_period(24, role_filter="maker", data_dir=temp_data_dir)
        assert stats["total_coinjoins"] == 1
        assert stats["maker_coinjoins"] == 1
        assert stats["taker_coinjoins"] == 0

    def test_failed_entries_counted(self, temp_data_dir: Path) -> None:
        """Test that failed entries with completed_at are counted as failed."""
        now = datetime.now()
        recent_ts = (now - timedelta(hours=1)).isoformat()

        failed = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="failed_tx" * 8,
            cj_amount=1_000_000,
            success=False,
            completed_at=recent_ts,
            failure_reason="Taker timeout",
        )
        append_history_entry(failed, temp_data_dir)

        stats = get_history_stats_for_period(24, data_dir=temp_data_dir)
        assert stats["total_coinjoins"] == 1
        assert stats["successful_coinjoins"] == 0
        assert stats["failed_coinjoins"] == 1

    def test_invalid_timestamp_skipped(self, temp_data_dir: Path) -> None:
        """Test that entries with invalid timestamps are skipped gracefully."""
        # Add a valid entry
        now = datetime.now()
        valid_ts = (now - timedelta(hours=1)).isoformat()
        valid = TransactionHistoryEntry(
            timestamp=valid_ts,
            role="maker",
            txid="valid_tx12" * 7,
            cj_amount=1_000_000,
            success=True,
        )
        append_history_entry(valid, temp_data_dir)

        # Add an entry with invalid timestamp
        invalid = TransactionHistoryEntry(
            timestamp="not-a-timestamp",
            role="maker",
            txid="invalid_tx" * 7,
            cj_amount=500_000,
            success=True,
        )
        append_history_entry(invalid, temp_data_dir)

        # Should only count the valid entry
        stats = get_history_stats_for_period(24, data_dir=temp_data_dir)
        assert stats["total_coinjoins"] == 1
        assert stats["total_volume"] == 1_000_000

    def test_successful_volume_excludes_failed(self, temp_data_dir: Path) -> None:
        """Test that successful_volume only includes successful entries."""
        now = datetime.now()
        recent_ts = (now - timedelta(hours=1)).isoformat()

        # Add successful entry
        success = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="success_tx" * 7,
            cj_amount=1_000_000,
            fee_received=500,
            success=True,
            utxos_used="aabb0011:0,ccdd2233:1",
        )
        append_history_entry(success, temp_data_dir)

        # Add failed entry
        failed = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="failed_tx1" * 7,
            cj_amount=2_000_000,
            success=False,
            completed_at=recent_ts,
            failure_reason="Taker timeout",
            utxos_used="eeff4455:0",
        )
        append_history_entry(failed, temp_data_dir)

        stats = get_history_stats_for_period(24, data_dir=temp_data_dir)
        assert stats["total_coinjoins"] == 2
        assert stats["total_volume"] == 3_000_000
        assert stats["successful_volume"] == 1_000_000
        assert stats["utxos_disclosed"] == 3  # 2 from success + 1 from failed

    def test_utxos_disclosed_counts_all_entries(self, temp_data_dir: Path) -> None:
        """Test that utxos_disclosed counts UTXOs from all entries (not just successful)."""
        now = datetime.now()
        recent_ts = (now - timedelta(hours=1)).isoformat()

        # Entry with 3 UTXOs
        entry1 = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="entry1_tx1" * 7,
            cj_amount=500_000,
            success=True,
            utxos_used="aa:0,bb:1,cc:2",
        )
        append_history_entry(entry1, temp_data_dir)

        # Entry with 1 UTXO (failed - UTXOs still disclosed)
        entry2 = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="entry2_tx1" * 7,
            cj_amount=300_000,
            success=False,
            completed_at=recent_ts,
            failure_reason="Taker disappeared",
            utxos_used="dd:0",
        )
        append_history_entry(entry2, temp_data_dir)

        # Entry with no UTXOs recorded
        entry3 = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="entry3_tx1" * 7,
            cj_amount=200_000,
            success=False,
            completed_at=recent_ts,
            failure_reason="Early failure",
            utxos_used="",
        )
        append_history_entry(entry3, temp_data_dir)

        stats = get_history_stats_for_period(24, data_dir=temp_data_dir)
        assert stats["utxos_disclosed"] == 4  # 3 + 1 + 0

    def test_utxos_disclosed_deduplicates_across_entries(self, temp_data_dir: Path) -> None:
        """Test that utxos_disclosed deduplicates the same UTXO across entries.

        If the same UTXO is disclosed in multiple CoinJoin attempts, it should
        only be counted once.  Users care about how many distinct UTXOs external
        observers know about, not how many disclosure events occurred.
        """
        now = datetime.now()
        recent_ts = (now - timedelta(hours=1)).isoformat()

        # First attempt: discloses aa:0 and bb:1
        entry1 = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="entry1_tx1" * 7,
            cj_amount=500_000,
            success=False,
            completed_at=recent_ts,
            failure_reason="Taker disappeared",
            utxos_used="aa:0,bb:1",
        )
        append_history_entry(entry1, temp_data_dir)

        # Second attempt: discloses aa:0 again (same UTXO) and cc:2 (new)
        entry2 = TransactionHistoryEntry(
            timestamp=recent_ts,
            role="maker",
            txid="entry2_tx1" * 7,
            cj_amount=500_000,
            success=True,
            utxos_used="aa:0,cc:2",
        )
        append_history_entry(entry2, temp_data_dir)

        stats = get_history_stats_for_period(24, data_dir=temp_data_dir)
        # aa:0 appears in both entries but should only be counted once
        # Unique set: {aa:0, bb:1, cc:2} = 3
        assert stats["utxos_disclosed"] == 3

    """Tests for helper functions."""

    def test_create_maker_history_entry(self) -> None:
        """Test create_maker_history_entry helper."""
        entry = create_maker_history_entry(
            taker_nick="J5testuser123456",
            cj_amount=1_000_000,
            fee_received=250,
            txfee_contribution=50,
            cj_address="bc1qtest...",
            change_address="bc1qchange...",
            our_utxos=[("abc123", 0), ("def456", 1)],
            txid="txid" * 16,
            network="regtest",
        )

        assert entry.role == "maker"
        assert entry.cj_amount == 1_000_000
        assert entry.fee_received == 250
        assert entry.txfee_contribution == 50
        assert entry.net_fee == 200  # 250 - 50
        assert entry.counterparty_nicks == "J5testuser123456"
        assert entry.peer_count is None  # Makers don't know peer count
        assert "abc123:0" in entry.utxos_used
        assert entry.network == "regtest"

    def test_create_taker_history_entry(self) -> None:
        """Test create_taker_history_entry helper."""
        entry = create_taker_history_entry(
            maker_nicks=["J5maker1", "J5maker2", "J5maker3"],
            cj_amount=2_000_000,
            total_maker_fees=900,
            mining_fee=300,
            destination="bc1qdest...",
            change_address="bc1qchange...",
            source_mixdepth=0,
            selected_utxos=[("utxo1", 0), ("utxo2", 1)],
            txid="txid" * 16,
            broadcast_method="self",
            network="mainnet",
        )

        assert entry.role == "taker"
        assert entry.cj_amount == 2_000_000
        assert entry.total_maker_fees_paid == 900
        assert entry.mining_fee_paid == 300
        assert entry.net_fee == -1200  # -(900 + 300)
        assert entry.peer_count == 3
        assert "J5maker1" in entry.counterparty_nicks
        assert entry.destination_address == "bc1qdest..."
        assert entry.change_address == "bc1qchange..."
        assert entry.source_mixdepth == 0
        assert entry.broadcast_method == "self"

    def test_create_taker_history_entry_failed(self) -> None:
        """Test create_taker_history_entry for failed CoinJoin."""
        entry = create_taker_history_entry(
            maker_nicks=["J5maker1"],
            cj_amount=500_000,
            total_maker_fees=0,
            mining_fee=0,
            destination="bc1qdest...",
            change_address="bc1qchange...",
            source_mixdepth=0,
            selected_utxos=[],
            txid="",
            success=False,
            failure_reason="Maker timeout",
        )

        assert entry.success is False
        assert entry.failure_reason == "Maker timeout"
        assert entry.txid == ""


class TestPendingTransactions:
    """Tests for pending transaction functionality."""

    def test_create_maker_entry_is_pending(self) -> None:
        """Test that newly created maker entries are marked as pending."""
        entry = _make_pending_maker_entry(txid="test_txid_123")

        # Should be marked as pending initially
        assert entry.success is False
        assert entry.failure_reason == "Pending confirmation"
        assert entry.confirmations == 0
        assert entry.confirmed_at == ""
        assert entry.completed_at == ""

    def test_create_taker_entry_is_pending(self) -> None:
        """Test that newly created taker entries are marked as pending by default."""
        entry = _make_pending_taker_entry(txid="test_txid_456")

        # Should be pending by default (Awaiting transaction)
        assert entry.success is False
        assert entry.failure_reason == "Awaiting transaction"
        assert entry.confirmations == 0
        assert entry.confirmed_at == ""
        assert entry.completed_at == ""

    def test_get_pending_transactions(self, temp_data_dir: Path) -> None:
        """Test retrieving pending transactions."""
        # Add a pending entry
        pending_entry = _make_pending_maker_entry(txid="pending_tx")
        append_history_entry(pending_entry, temp_data_dir)

        # Add a confirmed entry
        confirmed_entry = TransactionHistoryEntry(
            timestamp="2024-01-02T00:00:00",
            role="maker",
            txid="confirmed_tx",
            cj_amount=2_000_000,
            success=True,
            confirmations=6,
        )
        append_history_entry(confirmed_entry, temp_data_dir)

        # Get pending transactions
        pending = get_pending_transactions(temp_data_dir)

        assert len(pending) == 1
        assert pending[0].txid == "pending_tx"
        assert pending[0].success is False

    def test_get_pending_transactions_skips_shadowed_duplicate(self, temp_data_dir: Path) -> None:
        """A pending row shadowed by a successful sibling must not be polled.

        Regression test: previously a duplicated history file with both a
        stale ``success=False, confirmations=0`` row and a finalized
        ``success=True`` row for the same txid would keep returning the
        pending row from ``get_pending_transactions`` indefinitely – the
        update path matched the successful row first and never finalized
        the pending one. Now the pending row is filtered out.
        """
        wallet_fp = "deadbeef"
        pending = _make_pending_maker_entry(txid="ghost_tx")
        pending.wallet_fingerprint = wallet_fp
        append_history_entry(pending, temp_data_dir)

        confirmed = TransactionHistoryEntry(
            timestamp="2024-01-02T00:00:00",
            role="maker",
            txid="ghost_tx",
            cj_amount=1_000_000,
            success=True,
            confirmations=174,
            confirmed_at="2024-01-02T01:00:00",
            completed_at="2024-01-02T01:00:00",
            wallet_fingerprint=wallet_fp,
        )
        append_history_entry(confirmed, temp_data_dir)

        result = get_pending_transactions(temp_data_dir, wallet_fingerprint=wallet_fp)
        assert result == []

    def test_get_pending_transactions_caps_at_tracking_max(self, temp_data_dir: Path) -> None:
        """Entries with confirmations >= the cap are no longer polled."""
        from jmwallet.history import PENDING_CONFIRMATION_TRACKING_MAX

        # Force-construct a row that is still flagged "pending" (success=False,
        # completed_at=="") but has somehow accumulated many confirmations –
        # that is exactly the stuck state we want to stop polling.
        stuck = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            txid="stuck_tx",
            cj_amount=1_000_000,
            success=False,
            confirmations=PENDING_CONFIRMATION_TRACKING_MAX,
            wallet_fingerprint="cafef00d",
        )
        append_history_entry(stuck, temp_data_dir)

        result = get_pending_transactions(temp_data_dir, wallet_fingerprint="cafef00d")
        assert result == []

    def test_update_transaction_confirmation_prefers_pending_duplicate(
        self, temp_data_dir: Path
    ) -> None:
        """When duplicates exist, the pending row is the one that gets finalized."""
        wallet_fp = "deadbeef"
        # Simulate the buggy state: confirmed sibling was written first.
        confirmed = TransactionHistoryEntry(
            timestamp="2024-01-02T00:00:00",
            role="maker",
            txid="dup_tx",
            cj_amount=1_000_000,
            success=True,
            confirmations=10,
            confirmed_at="2024-01-02T01:00:00",
            completed_at="2024-01-02T01:00:00",
            wallet_fingerprint=wallet_fp,
        )
        append_history_entry(confirmed, temp_data_dir)
        pending = _make_pending_maker_entry(txid="dup_tx")
        pending.wallet_fingerprint = wallet_fp
        append_history_entry(pending, temp_data_dir)

        result = update_transaction_confirmation(
            "dup_tx", 11, temp_data_dir, wallet_fingerprint=wallet_fp
        )
        assert result is True

        rows = [e for e in read_history(temp_data_dir) if e.txid == "dup_tx"]
        # The previously-pending row must now be finalized.
        finalized_rows = [e for e in rows if e.success and e.completed_at]
        assert len(finalized_rows) == 2

    def test_update_transaction_confirmation(self, temp_data_dir: Path) -> None:
        """Test updating transaction confirmation status."""
        # Create and save a pending entry
        entry = _make_pending_maker_entry(txid="test_tx_update")
        append_history_entry(entry, temp_data_dir)

        # Verify it's pending
        pending = get_pending_transactions(temp_data_dir)
        assert len(pending) == 1

        # Update with 1 confirmation
        result = update_transaction_confirmation("test_tx_update", 1, temp_data_dir)
        assert result is True

        # Verify it's no longer pending
        pending = get_pending_transactions(temp_data_dir)
        assert len(pending) == 0

        # Read the entry and verify it's marked as successful
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].success is True
        assert entries[0].confirmations == 1
        assert entries[0].confirmed_at != ""
        assert entries[0].completed_at != ""
        assert entries[0].failure_reason == ""

    def test_update_transaction_confirmation_incremental(self, temp_data_dir: Path) -> None:
        """Test updating confirmations incrementally."""
        # Create and save a pending entry
        entry = _make_pending_maker_entry(txid="test_tx_incremental")
        append_history_entry(entry, temp_data_dir)

        # Update with 1 confirmation
        update_transaction_confirmation("test_tx_incremental", 1, temp_data_dir)

        # Update with 6 confirmations
        update_transaction_confirmation("test_tx_incremental", 6, temp_data_dir)

        # Verify confirmations were updated
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].confirmations == 6
        assert entries[0].success is True

    def test_update_nonexistent_transaction(self, temp_data_dir: Path) -> None:
        """Test updating a transaction that doesn't exist."""
        result = update_transaction_confirmation("nonexistent_tx", 1, temp_data_dir)
        assert result is False

    def test_update_transaction_confirmation_preserves_file_on_write_failure(
        self, temp_data_dir: Path
    ) -> None:
        """Atomic rewrite failure should not corrupt existing history."""
        entry = _make_pending_maker_entry(txid="atomic_fail_txid")
        append_history_entry(entry, temp_data_dir)

        with patch("jmwallet.history.os.replace", side_effect=OSError("simulated fs error")):
            result = update_transaction_confirmation("atomic_fail_txid", 2, temp_data_dir)

        assert result is False

        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].txid == "atomic_fail_txid"
        assert entries[0].success is False
        assert entries[0].confirmations == 0


class TestPendingConfirmationRefresh:
    """Tests for update_all_pending_transactions behavior."""

    @pytest.mark.asyncio
    async def test_mempool_seen_but_zero_conf_stays_pending(self, temp_data_dir: Path) -> None:
        entry = _make_pending_maker_entry(txid="mempool_txid")
        append_history_entry(entry, temp_data_dir)

        mock_backend = MagicMock()
        mock_backend.has_mempool_access.return_value = True
        mock_backend.get_transaction = AsyncMock(
            return_value=Transaction(
                txid="mempool_txid",
                raw="00",
                confirmations=0,
                block_height=None,
            )
        )

        updated = await update_all_pending_transactions(mock_backend, data_dir=temp_data_dir)
        assert updated == 0

        pending = get_pending_transactions(temp_data_dir)
        assert len(pending) == 1
        assert pending[0].txid == "mempool_txid"
        assert pending[0].success is False
        assert pending[0].confirmations == 0

    @pytest.mark.asyncio
    async def test_positive_confirmations_mark_confirmed(self, temp_data_dir: Path) -> None:
        entry = _make_pending_maker_entry(txid="confirmed_txid")
        append_history_entry(entry, temp_data_dir)

        mock_backend = MagicMock()
        mock_backend.has_mempool_access.return_value = True
        mock_backend.get_transaction = AsyncMock(
            return_value=Transaction(
                txid="confirmed_txid",
                raw="00",
                confirmations=3,
                block_height=123,
            )
        )

        updated = await update_all_pending_transactions(mock_backend, data_dir=temp_data_dir)
        assert updated == 1

        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].txid == "confirmed_txid"
        assert entries[0].success is True
        assert entries[0].confirmations == 3


class TestUsedAddressTracking:
    """Tests for used address tracking and txid discovery."""

    def test_get_used_addresses_empty(self, temp_data_dir: Path) -> None:
        """Test get_used_addresses with no history."""

        used = get_used_addresses(temp_data_dir)
        assert len(used) == 0
        assert isinstance(used, set)

    def test_get_used_addresses_with_history(self, temp_data_dir: Path) -> None:
        """Test get_used_addresses returns addresses from history."""

        # Add entries with different addresses
        entry1 = _make_pending_maker_entry(
            cj_address="bc1qtest1address111111",
            txid="txid1" * 16,
        )
        append_history_entry(entry1, temp_data_dir)

        entry2 = create_taker_history_entry(
            maker_nicks=["J5maker1"],
            cj_amount=2_000_000,
            total_maker_fees=500,
            mining_fee=100,
            destination="bc1qtest2address222222",
            change_address="bc1qtakerchange...",
            source_mixdepth=0,
            selected_utxos=[("utxo1", 0)],
            txid="txid2" * 16,
        )
        append_history_entry(entry2, temp_data_dir)

        # Get used addresses
        used = get_used_addresses(temp_data_dir)

        assert len(used) == 4  # 2 CJ addresses (maker+taker) + 2 change addresses
        assert "bc1qtest1address111111" in used
        assert "bc1qtest2address222222" in used
        assert "bc1qtakerchange..." in used

    def test_get_used_addresses_deduplication(self, temp_data_dir: Path) -> None:
        """Test that get_used_addresses deduplicates addresses."""

        # Add two entries with the same destination address
        entry1 = create_maker_history_entry(
            taker_nick="J5taker1",
            cj_amount=1_000_000,
            fee_received=250,
            txfee_contribution=50,
            cj_address="bc1qsameaddress123456",
            change_address="bc1qchange...",
            our_utxos=[("abc123", 0)],
            txid="txid1" * 16,
        )
        append_history_entry(entry1, temp_data_dir)

        entry2 = create_maker_history_entry(
            taker_nick="J5taker2",
            cj_amount=2_000_000,
            fee_received=500,
            txfee_contribution=100,
            cj_address="bc1qsameaddress123456",
            change_address="bc1qchange...",
            our_utxos=[("def456", 0)],
            txid="txid2" * 16,
        )
        append_history_entry(entry2, temp_data_dir)

        # Should only have one address despite two entries
        used = get_used_addresses(temp_data_dir)
        assert len(used) == 2  # CJ address + change address
        assert "bc1qsameaddress123456" in used

    def test_get_used_addresses_includes_pending(self, temp_data_dir: Path) -> None:
        """Test that get_used_addresses includes pending transactions."""

        # Add a pending entry (no txid)
        entry = _make_pending_maker_entry(
            cj_address="bc1qpending12345678",
            txid="",  # No txid yet - pending
        )
        append_history_entry(entry, temp_data_dir)

        # Address should still be marked as used (privacy!)
        used = get_used_addresses(temp_data_dir)
        assert len(used) == 2  # CJ address + change address
        assert "bc1qpending12345678" in used

    def test_update_pending_transaction_txid(self, temp_data_dir: Path) -> None:
        """Test updating pending transaction with discovered txid."""

        # Create a pending entry without txid
        entry = _make_pending_maker_entry(
            cj_address="bc1qdiscovered123456",
            txid="",  # No txid initially
        )
        append_history_entry(entry, temp_data_dir)

        # Verify it's pending without txid
        pending = get_pending_transactions(temp_data_dir)
        assert len(pending) == 1
        assert pending[0].txid == ""
        assert pending[0].destination_address == "bc1qdiscovered123456"

        # Update with discovered txid
        result = update_pending_transaction_txid(
            destination_address="bc1qdiscovered123456",
            txid="discovered_txid_12345678",
            data_dir=temp_data_dir,
        )
        assert result is True

        # Verify txid was updated
        pending = get_pending_transactions(temp_data_dir)
        assert len(pending) == 1
        assert pending[0].txid == "discovered_txid_12345678"
        assert pending[0].destination_address == "bc1qdiscovered123456"

    def test_update_pending_transaction_txid_nonexistent(self, temp_data_dir: Path) -> None:
        """Test update_pending_transaction_txid with nonexistent address."""

        result = update_pending_transaction_txid(
            destination_address="bc1qnonexistent1234",
            txid="some_txid",
            data_dir=temp_data_dir,
        )
        assert result is False

    def test_update_pending_transaction_txid_already_has_txid(self, temp_data_dir: Path) -> None:
        """Test that update_pending_transaction_txid only updates entries without txid."""

        # Create entry that already has a txid
        entry = _make_pending_maker_entry(
            cj_address="bc1qalreadyhas123456",
            txid="original_txid_12345678",
        )
        append_history_entry(entry, temp_data_dir)

        # Try to update - should not match (entry has txid)
        result = update_pending_transaction_txid(
            destination_address="bc1qalreadyhas123456",
            txid="new_txid_different",
            data_dir=temp_data_dir,
        )
        assert result is False

        # Verify original txid unchanged
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].txid == "original_txid_12345678"

    def test_get_used_addresses_includes_change_addresses(self, temp_data_dir: Path) -> None:
        """Test that get_used_addresses includes both CJ and change addresses."""

        # Add entry with both cj_address and change_address
        entry = _make_pending_maker_entry(
            cj_address="bc1qcoinjoin123456",
            change_address="bc1qchange789012345",
            txid="txid1" * 16,
        )
        append_history_entry(entry, temp_data_dir)

        # Both addresses should be in the used set
        used = get_used_addresses(temp_data_dir)
        assert len(used) == 2  # 1 CJ address + 1 change address
        assert "bc1qcoinjoin123456" in used
        assert "bc1qchange789012345" in used

    def test_get_used_addresses_includes_source_addresses(self, temp_data_dir: Path) -> None:
        """Source (input) addresses must be blacklisted as used.

        Layer 3 of the deposit-address-reuse fix: spent deposit addresses
        are persisted on the history row so they survive backend amnesia.
        """
        entry = create_taker_history_entry(
            maker_nicks=["J5peer1"],
            cj_amount=1_000_000,
            total_maker_fees=300,
            mining_fee=200,
            destination="bc1qdestination0000",
            change_address="bc1qchange00000000",
            source_mixdepth=0,
            selected_utxos=[("abc", 0), ("def", 1)],
            source_addresses=["bc1qinput0000000000", "bc1qinput1111111111"],
        )
        append_history_entry(entry, temp_data_dir)

        used = get_used_addresses(temp_data_dir)
        assert "bc1qdestination0000" in used
        assert "bc1qchange00000000" in used
        assert "bc1qinput0000000000" in used
        assert "bc1qinput1111111111" in used

    def test_get_used_addresses_ignores_empty_source_field(self, temp_data_dir: Path) -> None:
        """Legacy rows with empty source_addresses must not produce empty entries."""
        entry = create_taker_history_entry(
            maker_nicks=["J5peer1"],
            cj_amount=1_000_000,
            total_maker_fees=300,
            mining_fee=200,
            destination="bc1qdest2",
            change_address="",
            source_mixdepth=0,
            selected_utxos=[("abc", 0)],
        )
        append_history_entry(entry, temp_data_dir)

        used = get_used_addresses(temp_data_dir)
        assert "" not in used
        assert used == {"bc1qdest2"}


class TestUpdateAwaitingTransactionSigned:
    """Tests for updating 'Awaiting transaction' entries when tx is signed."""

    def test_update_awaiting_transaction_signed_basic(self, temp_data_dir: Path) -> None:
        """Test updating an 'Awaiting transaction' entry with tx details."""
        # Create a pending entry with "Awaiting transaction" status
        # (simulating what happens during !ioauth)
        entry = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=1_000_000,
            fee_received=0,  # Unknown during !ioauth
            txfee_contribution=0,  # Unknown during !ioauth
            cj_address="bc1qawaiting12345678",
            change_address="bc1qchange12345678",
            our_utxos=[("abc123", 0)],
            txid=None,  # No txid during !ioauth
        )
        entry.failure_reason = "Awaiting transaction"
        append_history_entry(entry, temp_data_dir)

        # Verify it's stored with "Awaiting transaction" status
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].failure_reason == "Awaiting transaction"
        assert entries[0].txid == ""
        assert entries[0].fee_received == 0

        # Now update when transaction is signed
        result = update_awaiting_transaction_signed(
            destination_address="bc1qawaiting12345678",
            txid="signed_tx_1234567890abcdef",
            fee_received=250,
            txfee_contribution=50,
            data_dir=temp_data_dir,
        )
        assert result is True

        # Verify the entry was updated correctly
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].txid == "signed_tx_1234567890abcdef"
        assert entries[0].fee_received == 250
        assert entries[0].txfee_contribution == 50
        assert entries[0].net_fee == 200  # 250 - 50
        assert entries[0].failure_reason == "Pending confirmation"  # Now awaiting confirmation

    def test_update_awaiting_transaction_signed_nonexistent(self, temp_data_dir: Path) -> None:
        """Test that update fails when no matching entry exists."""
        result = update_awaiting_transaction_signed(
            destination_address="bc1qnonexistent1234",
            txid="some_txid",
            fee_received=100,
            txfee_contribution=25,
            data_dir=temp_data_dir,
        )
        assert result is False

    def test_update_awaiting_transaction_signed_only_matches_awaiting(
        self, temp_data_dir: Path
    ) -> None:
        """Test that update only matches 'Awaiting transaction' entries."""
        # Create an entry with different failure_reason
        entry = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=1_000_000,
            fee_received=250,
            txfee_contribution=50,
            cj_address="bc1qpending12345678",
            change_address="bc1qchange12345678",
            our_utxos=[("abc123", 0)],
            txid=None,
        )
        # Default failure_reason is "Pending confirmation", not "Awaiting transaction"
        append_history_entry(entry, temp_data_dir)

        # Should NOT update because failure_reason is "Pending confirmation"
        result = update_awaiting_transaction_signed(
            destination_address="bc1qpending12345678",
            txid="new_txid",
            fee_received=500,
            txfee_contribution=100,
            data_dir=temp_data_dir,
        )
        assert result is False

        # Original values should be unchanged
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].fee_received == 250  # Unchanged

    def test_update_awaiting_transaction_signed_preserves_other_fields(
        self, temp_data_dir: Path
    ) -> None:
        """Test that updating preserves other fields like cj_amount, taker nick, etc."""
        entry = create_maker_history_entry(
            taker_nick="J5specifictaker",
            cj_amount=5_000_000,
            fee_received=0,
            txfee_contribution=0,
            cj_address="bc1qpreserve12345678",
            change_address="bc1qchangepreserve",
            our_utxos=[("utxo1", 0), ("utxo2", 1)],
            txid=None,
            network="signet",
        )
        entry.failure_reason = "Awaiting transaction"
        append_history_entry(entry, temp_data_dir)

        # Update with tx details
        result = update_awaiting_transaction_signed(
            destination_address="bc1qpreserve12345678",
            txid="preserved_txid_123456",
            fee_received=1000,
            txfee_contribution=200,
            data_dir=temp_data_dir,
        )
        assert result is True

        # Verify other fields are preserved
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].counterparty_nicks == "J5specifictaker"
        assert entries[0].cj_amount == 5_000_000
        assert entries[0].change_address == "bc1qchangepreserve"
        assert entries[0].network == "signet"
        assert "utxo1:0" in entries[0].utxos_used
        assert "utxo2:1" in entries[0].utxos_used

    def test_update_awaiting_transaction_signed_wont_match_with_existing_txid(
        self, temp_data_dir: Path
    ) -> None:
        """Test that entries with existing txid are not matched."""
        # Create entry that already has a txid (shouldn't be possible normally,
        # but test defensive coding)
        entry = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=1_000_000,
            fee_received=0,
            txfee_contribution=0,
            cj_address="bc1qwithtxid12345678",
            change_address="bc1qchange12345678",
            our_utxos=[("abc123", 0)],
            txid="existing_txid_123",  # Already has txid
        )
        entry.failure_reason = "Awaiting transaction"
        append_history_entry(entry, temp_data_dir)

        # Should NOT match because txid already exists
        result = update_awaiting_transaction_signed(
            destination_address="bc1qwithtxid12345678",
            txid="new_txid_456",
            fee_received=500,
            txfee_contribution=100,
            data_dir=temp_data_dir,
        )
        assert result is False

        # Original txid should be unchanged
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].txid == "existing_txid_123"


class TestUpdateTakerAwaitingTransactionBroadcast:
    """Tests for update_taker_awaiting_transaction_broadcast function."""

    def test_update_taker_awaiting_transaction_broadcast_basic(self, temp_data_dir: Path) -> None:
        """Test basic update of taker 'Awaiting transaction' entry after broadcast."""
        # Create a taker entry with "Awaiting transaction" status
        entry = create_taker_history_entry(
            maker_nicks=["J5maker1", "J5maker2"],
            cj_amount=1_000_000,
            total_maker_fees=500,
            mining_fee=0,  # Will be updated
            destination="bc1qtakerdest12345678",
            change_address="bc1qtakerchange123",
            source_mixdepth=0,
            selected_utxos=[("utxo1", 0)],
            txid="",  # Empty before broadcast
            failure_reason="Awaiting transaction",
        )
        append_history_entry(entry, temp_data_dir)

        # Update after broadcast
        result = update_taker_awaiting_transaction_broadcast(
            destination_address="bc1qtakerdest12345678",
            change_address="bc1qtakerchange123",
            txid="broadcast_txid_abcdef123456",
            mining_fee=250,
            data_dir=temp_data_dir,
        )
        assert result is True

        # Verify entry was updated
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].txid == "broadcast_txid_abcdef123456"
        assert entries[0].mining_fee_paid == 250
        assert entries[0].net_fee == -(500 + 250)  # -(maker_fees + mining_fee)
        assert entries[0].failure_reason == "Pending confirmation"

    def test_update_taker_awaiting_transaction_broadcast_nonexistent(
        self, temp_data_dir: Path
    ) -> None:
        """Test that update fails when no matching entry exists."""
        result = update_taker_awaiting_transaction_broadcast(
            destination_address="bc1qnonexistent1234",
            change_address="bc1qnonexistentchange",
            txid="some_txid",
            mining_fee=100,
            data_dir=temp_data_dir,
        )
        assert result is False

    def test_update_taker_awaiting_transaction_broadcast_only_matches_awaiting(
        self, temp_data_dir: Path
    ) -> None:
        """Test that update only matches 'Awaiting transaction' entries."""
        # Create an entry with different failure_reason
        entry = _make_pending_taker_entry(
            destination="bc1qtakerpending123",
            change_address="bc1qtakerchange456",
            failure_reason="Pending confirmation",  # Different status
        )
        append_history_entry(entry, temp_data_dir)

        # Should NOT update because failure_reason is not "Awaiting transaction"
        result = update_taker_awaiting_transaction_broadcast(
            destination_address="bc1qtakerpending123",
            change_address="bc1qtakerchange456",
            txid="new_txid",
            mining_fee=200,
            data_dir=temp_data_dir,
        )
        assert result is False

        # Original values should be unchanged
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].mining_fee_paid == 100  # Unchanged

    def test_update_taker_awaiting_transaction_broadcast_requires_both_addresses(
        self, temp_data_dir: Path
    ) -> None:
        """Test that update requires both destination and change address to match."""
        entry = create_taker_history_entry(
            maker_nicks=["J5maker1"],
            cj_amount=1_000_000,
            total_maker_fees=500,
            mining_fee=0,
            destination="bc1qtakerdest789",
            change_address="bc1qtakerchange789",
            source_mixdepth=0,
            selected_utxos=[("utxo1", 0)],
            txid="",
            failure_reason="Awaiting transaction",
        )
        append_history_entry(entry, temp_data_dir)

        # Should NOT match - wrong change address
        result = update_taker_awaiting_transaction_broadcast(
            destination_address="bc1qtakerdest789",
            change_address="bc1qwrongchange",  # Wrong change address
            txid="new_txid",
            mining_fee=200,
            data_dir=temp_data_dir,
        )
        assert result is False

        # Should NOT match - wrong destination
        result = update_taker_awaiting_transaction_broadcast(
            destination_address="bc1qwrongdest",  # Wrong destination
            change_address="bc1qtakerchange789",
            txid="new_txid",
            mining_fee=200,
            data_dir=temp_data_dir,
        )
        assert result is False

    def test_update_taker_awaiting_transaction_broadcast_preserves_other_fields(
        self, temp_data_dir: Path
    ) -> None:
        """Test that updating preserves other fields like cj_amount, maker nicks, etc."""
        entry = create_taker_history_entry(
            maker_nicks=["J5maker1", "J5maker2", "J5maker3"],
            cj_amount=5_000_000,
            total_maker_fees=1500,
            mining_fee=0,
            destination="bc1qtakerpreserve123",
            change_address="bc1qtakerchangepreserve",
            source_mixdepth=2,
            selected_utxos=[("utxo1", 0), ("utxo2", 1)],
            txid="",
            broadcast_method="random-maker",
            network="signet",
            failure_reason="Awaiting transaction",
        )
        append_history_entry(entry, temp_data_dir)

        # Update with tx details
        result = update_taker_awaiting_transaction_broadcast(
            destination_address="bc1qtakerpreserve123",
            change_address="bc1qtakerchangepreserve",
            txid="preserved_taker_txid_123",
            mining_fee=300,
            data_dir=temp_data_dir,
        )
        assert result is True

        # Verify other fields are preserved
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].counterparty_nicks == "J5maker1,J5maker2,J5maker3"
        assert entries[0].peer_count == 3
        assert entries[0].cj_amount == 5_000_000
        assert entries[0].total_maker_fees_paid == 1500
        assert entries[0].source_mixdepth == 2
        assert entries[0].broadcast_method == "random-maker"
        assert entries[0].network == "signet"
        assert "utxo1:0" in entries[0].utxos_used
        assert "utxo2:1" in entries[0].utxos_used

    def test_update_taker_awaiting_transaction_broadcast_wont_match_with_existing_txid(
        self, temp_data_dir: Path
    ) -> None:
        """Test that entries with existing txid are not matched."""
        entry = _make_pending_taker_entry(
            destination="bc1qtakerwithtxid123",
            change_address="bc1qtakerchangetxid",
            txid="existing_taker_txid",  # Already has txid
            failure_reason="Awaiting transaction",
        )
        append_history_entry(entry, temp_data_dir)

        # Should NOT match because txid already exists
        result = update_taker_awaiting_transaction_broadcast(
            destination_address="bc1qtakerwithtxid123",
            change_address="bc1qtakerchangetxid",
            txid="new_txid_456",
            mining_fee=200,
            data_dir=temp_data_dir,
        )
        assert result is False

        # Original txid should be unchanged
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].txid == "existing_taker_txid"

    def test_update_taker_awaiting_transaction_broadcast_sweep_no_change(
        self, temp_data_dir: Path
    ) -> None:
        """Test updating entry when sweep has no change output.

        In sweep mode with no dust, there may be no change output in the final transaction.
        The history entry should be created with empty change_address, and the update
        should match successfully with empty change_address.
        """
        # Create entry with empty change_address (sweep with no change output)
        entry = create_taker_history_entry(
            maker_nicks=["J5maker1", "J5maker2"],
            cj_amount=1_000_000,
            total_maker_fees=500,
            mining_fee=0,
            destination="bc1qtakersweep12345",
            change_address="",  # No change output in sweep
            source_mixdepth=0,
            selected_utxos=[("utxo1", 0)],
            txid="",
            failure_reason="Awaiting transaction",
        )
        append_history_entry(entry, temp_data_dir)

        # Update with empty change_address (matching the entry)
        result = update_taker_awaiting_transaction_broadcast(
            destination_address="bc1qtakersweep12345",
            change_address="",  # No change in actual transaction
            txid="sweep_txid_abcdef123",
            mining_fee=200,
            data_dir=temp_data_dir,
        )
        assert result is True

        # Verify entry was updated
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].txid == "sweep_txid_abcdef123"
        assert entries[0].mining_fee_paid == 200
        assert entries[0].change_address == ""  # Still empty
        assert entries[0].failure_reason == "Pending confirmation"


class TestPeerCountDetection:
    """Tests for automatic peer count detection from transaction outputs."""

    @pytest.mark.asyncio
    async def test_detect_coinjoin_peer_count(self) -> None:
        """Test detecting peer count from equal-amount outputs."""

        # Create a minimal valid SegWit transaction with 4 equal outputs of 30,000 sats
        # Format: version(4) + marker(1) + flag(1) + inputs + outputs + witness + locktime(4)

        def encode_varint(n: int) -> bytes:
            if n < 0xFD:
                return bytes([n])
            elif n <= 0xFFFF:
                return b"\xfd" + struct.pack("<H", n)
            elif n <= 0xFFFFFFFF:
                return b"\xfe" + struct.pack("<I", n)
            else:
                return b"\xff" + struct.pack("<Q", n)

        # Version
        tx_bytes = struct.pack("<I", 2)

        # Marker and flag for SegWit
        tx_bytes += b"\x00\x01"

        # Input count (1)
        tx_bytes += encode_varint(1)

        # Input: txid (32 bytes)
        tx_bytes += b"\xaa" * 32

        # Input: vout (4 bytes)
        tx_bytes += struct.pack("<I", 0)

        # Input: scriptSig length + scriptSig (empty for segwit)
        tx_bytes += encode_varint(0)

        # Input: sequence
        tx_bytes += struct.pack("<I", 0xFFFFFFFE)

        # Output count (5: 4 equal + 1 change)
        tx_bytes += encode_varint(5)

        # 4 equal CoinJoin outputs of 30,000 sats
        for i in range(4):
            tx_bytes += struct.pack("<Q", 30000)  # value
            script = b"\x00\x14" + bytes([i] * 20)  # P2WPKH script
            tx_bytes += encode_varint(len(script))
            tx_bytes += script

        # 1 change output of 50,000 sats
        tx_bytes += struct.pack("<Q", 50000)
        script = b"\x00\x14" + b"\x99" * 20
        tx_bytes += encode_varint(len(script))
        tx_bytes += script

        # Witness data for the input
        tx_bytes += encode_varint(2)  # 2 witness items
        tx_bytes += encode_varint(64) + b"\x01" * 64  # signature
        tx_bytes += encode_varint(33) + b"\x02" * 33  # pubkey

        # Locktime
        tx_bytes += struct.pack("<I", 0)

        tx_hex = tx_bytes.hex()

        # Create mock backend
        mock_backend = MagicMock()
        mock_backend.get_transaction = AsyncMock(
            return_value=Transaction(
                txid="test_txid_123",
                raw=tx_hex,
                confirmations=1,
                block_height=100,
            )
        )

        # Detect peer count for 30,000 sat outputs
        peer_count = await detect_coinjoin_peer_count(mock_backend, "test_txid_123", 30000)

        # Should detect 4 equal outputs
        assert peer_count == 4

    @pytest.mark.asyncio
    async def test_detect_coinjoin_peer_count_no_match(self) -> None:
        """Test peer count detection when no outputs match."""
        mock_backend = MagicMock()

        # Transaction with different output amounts
        tx_raw = (
            "020000000001010000000000000000000000000000000000000000000000000000000000000000ffff"
            "ffff0100f2052a01000000160014abcd1234000000000000000000000000000000000000000000"
        )
        mock_backend.get_transaction = AsyncMock(
            return_value=Transaction(
                txid="test_txid_456",
                raw=tx_raw,
                confirmations=1,
            )
        )

        # Try to detect peer count for amount that doesn't exist
        peer_count = await detect_coinjoin_peer_count(mock_backend, "test_txid_456", 50000)

        assert peer_count is None

    @pytest.mark.asyncio
    async def test_detect_coinjoin_peer_count_fetch_fails(self) -> None:
        """Test peer count detection when transaction fetch fails."""
        mock_backend = MagicMock()
        mock_backend.get_transaction = AsyncMock(return_value=None)

        peer_count = await detect_coinjoin_peer_count(mock_backend, "nonexistent", 30000)

        assert peer_count is None

    def test_update_transaction_peer_count(self, temp_data_dir: Path) -> None:
        """Test updating peer count for a maker transaction."""
        # Create maker entry without peer count
        entry = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=30000,
            fee_received=100,
            txfee_contribution=50,
            cj_address="bc1qtest...",
            change_address="bc1qchange...",
            our_utxos=[("abc123", 0)],
            txid="test_tx_12345678",
        )
        append_history_entry(entry, temp_data_dir)

        # Verify peer count is None
        entries = read_history(temp_data_dir)
        assert entries[0].peer_count is None

        # Update peer count
        result = update_transaction_peer_count("test_tx_12345678", 5, temp_data_dir)
        assert result is True

        # Verify peer count was updated
        entries = read_history(temp_data_dir)
        assert entries[0].peer_count == 5

    def test_update_transaction_peer_count_only_updates_none(self, temp_data_dir: Path) -> None:
        """Test that peer count update only affects entries with None peer count."""
        # Create taker entry with existing peer count
        entry = create_taker_history_entry(
            maker_nicks=["J5maker1", "J5maker2", "J5maker3"],
            cj_amount=30000,
            total_maker_fees=500,
            mining_fee=100,
            destination="bc1qdest...",
            change_address="bc1qchange...",
            source_mixdepth=0,
            selected_utxos=[("utxo1", 0)],
            txid="taker_tx_123",
        )
        append_history_entry(entry, temp_data_dir)

        # Try to update peer count (should not update taker entries)
        result = update_transaction_peer_count("taker_tx_123", 10, temp_data_dir)
        assert result is False

        # Verify peer count unchanged
        entries = read_history(temp_data_dir)
        assert entries[0].peer_count == 3  # Original count from 3 makers

    @pytest.mark.asyncio
    async def test_update_confirmation_with_detection(self, temp_data_dir: Path) -> None:
        """Test automatic peer count detection during confirmation update."""
        # Create maker entry
        entry = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=30000,
            fee_received=100,
            txfee_contribution=50,
            cj_address="bc1qtest...",
            change_address="bc1qchange...",
            our_utxos=[("abc123", 0)],
            txid="test_tx_detection",
        )
        append_history_entry(entry, temp_data_dir)

        # Create mock backend
        mock_backend = MagicMock()
        tx_raw = "020000000001..."  # Simplified transaction
        mock_backend.get_transaction = AsyncMock(
            return_value=Transaction(
                txid="test_tx_detection",
                raw=tx_raw,
                confirmations=1,
            )
        )

        # Mock the peer count detection to return 4
        with patch(
            "jmwallet.history.detect_coinjoin_peer_count",
            return_value=4,
        ):
            # Update with detection
            result = await update_transaction_confirmation_with_detection(
                "test_tx_detection",
                1,
                backend=mock_backend,
                data_dir=temp_data_dir,
            )
            assert result is True

        # Verify peer count was detected and saved
        entries = read_history(temp_data_dir)
        assert entries[0].success is True
        assert entries[0].peer_count == 4


class TestMarkPendingTransactionFailed:
    """Tests for marking pending transactions as failed (timeout scenarios)."""

    def test_mark_pending_transaction_failed_basic(self, temp_data_dir: Path) -> None:
        """Test marking a pending transaction as failed."""
        # Create a pending entry without txid (simulating taker never broadcast)
        entry = _make_pending_maker_entry(
            cj_address="bc1qtimeout123456789",
            txid="",  # No txid - taker never broadcast
        )
        append_history_entry(entry, temp_data_dir)

        # Verify it's pending
        pending = get_pending_transactions(temp_data_dir)
        assert len(pending) == 1

        # Mark as failed
        result = mark_pending_transaction_failed(
            destination_address="bc1qtimeout123456789",
            failure_reason="Timed out after 60 minutes - taker never broadcast transaction",
            data_dir=temp_data_dir,
        )
        assert result is True

        # Verify no longer in pending list
        pending = get_pending_transactions(temp_data_dir)
        assert len(pending) == 0

        # Verify entry is marked as failed with appropriate fields
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].success is False
        assert (
            entries[0].failure_reason
            == "Timed out after 60 minutes - taker never broadcast transaction"
        )
        assert entries[0].completed_at != ""  # Should have completion timestamp
        assert entries[0].confirmations == 0  # Never confirmed

    def test_mark_pending_transaction_failed_with_txid(self, temp_data_dir: Path) -> None:
        """Test marking a pending transaction with txid as failed."""
        # Create a pending entry with txid but never confirmed
        entry = _make_pending_maker_entry(
            cj_address="bc1qneverconf12345",
            txid="deadbeef" * 8,  # Has txid but tx was never broadcast/confirmed
        )
        append_history_entry(entry, temp_data_dir)

        # Mark as failed (tx not found on chain)
        result = mark_pending_transaction_failed(
            destination_address="bc1qneverconf12345",
            failure_reason="Transaction not found after 60 minutes - likely never broadcast",
            data_dir=temp_data_dir,
        )
        assert result is True

        # Verify marked as failed
        entries = read_history(temp_data_dir)
        assert entries[0].success is False
        assert "never broadcast" in entries[0].failure_reason

    def test_mark_pending_transaction_failed_nonexistent(self, temp_data_dir: Path) -> None:
        """Test marking nonexistent transaction as failed returns False."""
        result = mark_pending_transaction_failed(
            destination_address="bc1qnonexistent1234",
            failure_reason="Timed out",
            data_dir=temp_data_dir,
        )
        assert result is False

    def test_mark_pending_transaction_failed_already_confirmed(self, temp_data_dir: Path) -> None:
        """Test that already confirmed transactions are not marked as failed."""
        # Create a confirmed entry
        entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            txid="confirmed_tx" * 8,
            cj_amount=1_000_000,
            success=True,
            confirmations=6,
            destination_address="bc1qconfirmed123456",
        )
        append_history_entry(entry, temp_data_dir)

        # Try to mark as failed - should not match (already successful)
        result = mark_pending_transaction_failed(
            destination_address="bc1qconfirmed123456",
            failure_reason="Should not happen",
            data_dir=temp_data_dir,
        )
        assert result is False

        # Verify entry unchanged
        entries = read_history(temp_data_dir)
        assert entries[0].success is True
        assert entries[0].confirmations == 6

    def test_mark_pending_transaction_failed_already_failed(self, temp_data_dir: Path) -> None:
        """Test that already failed transactions are not re-marked (prevents loops)."""
        # Create a failed entry (already marked as failed)
        entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            txid="",
            cj_amount=1_000_000,
            success=False,
            failure_reason="Already failed for another reason",
            confirmations=0,
            completed_at="2024-01-01T01:00:00",  # Already has completion time
            destination_address="bc1qalreadyfailed12",
        )
        append_history_entry(entry, temp_data_dir)

        # Try to mark as failed again - should NOT match because completed_at is set
        # This prevents infinite loops where we keep trying to mark the same entry
        result = mark_pending_transaction_failed(
            destination_address="bc1qalreadyfailed12",
            failure_reason="Timed out after 60 minutes",
            data_dir=temp_data_dir,
        )
        # Should return False since entry is already completed (has completed_at)
        assert result is False

        # The original failure reason should be preserved
        entries = read_history(temp_data_dir)
        assert entries[0].failure_reason == "Already failed for another reason"
        assert entries[0].completed_at == "2024-01-01T01:00:00"

    def test_mark_pending_preserves_other_entries(self, temp_data_dir: Path) -> None:
        """Test that marking one entry as failed preserves other entries."""
        # Create multiple entries
        pending_entry = create_maker_history_entry(
            taker_nick="J5taker1",
            cj_amount=1_000_000,
            fee_received=250,
            txfee_contribution=50,
            cj_address="bc1qpending_target1",
            change_address="bc1qchange1...",
            our_utxos=[("abc123", 0)],
            txid="",
        )
        append_history_entry(pending_entry, temp_data_dir)

        other_pending = create_maker_history_entry(
            taker_nick="J5taker2",
            cj_amount=2_000_000,
            fee_received=500,
            txfee_contribution=100,
            cj_address="bc1qpending_other11",
            change_address="bc1qchange2...",
            our_utxos=[("def456", 0)],
            txid="",
        )
        append_history_entry(other_pending, temp_data_dir)

        confirmed_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            txid="confirmed" * 8,
            cj_amount=3_000_000,
            success=True,
            confirmations=6,
            destination_address="bc1qconfirmed11111",
        )
        append_history_entry(confirmed_entry, temp_data_dir)

        # Mark only the first pending entry as failed
        mark_pending_transaction_failed(
            destination_address="bc1qpending_target1",
            failure_reason="Timed out",
            data_dir=temp_data_dir,
        )

        # Verify all entries preserved
        entries = read_history(temp_data_dir)
        assert len(entries) == 3

        # Check the targeted entry was marked failed
        target = [e for e in entries if e.destination_address == "bc1qpending_target1"][0]
        assert target.success is False
        assert "Timed out" in target.failure_reason

        # Check other pending entry still pending
        other = [e for e in entries if e.destination_address == "bc1qpending_other11"][0]
        assert other.success is False
        assert other.failure_reason == "Pending confirmation"

        # Check confirmed entry still confirmed
        conf = [e for e in entries if e.destination_address == "bc1qconfirmed11111"][0]
        assert conf.success is True
        assert conf.confirmations == 6

    def test_mark_pending_with_txid_disambiguation(self, temp_data_dir: Path) -> None:
        """Test that txid parameter disambiguates entries with same destination address."""
        # Create multiple pending entries with the same destination address but different txids
        # This can happen if the same address was reused (which shouldn't happen but could
        # occur due to bugs or manual intervention)
        entry1 = create_maker_history_entry(
            taker_nick="J5taker1",
            cj_amount=1_000_000,
            fee_received=250,
            txfee_contribution=50,
            cj_address="bc1qsameaddress12345",
            change_address="bc1qchange1...",
            our_utxos=[("abc123", 0)],
            txid="txid_first_entry_11",
        )
        append_history_entry(entry1, temp_data_dir)

        entry2 = create_maker_history_entry(
            taker_nick="J5taker2",
            cj_amount=2_000_000,
            fee_received=500,
            txfee_contribution=100,
            cj_address="bc1qsameaddress12345",  # Same address!
            change_address="bc1qchange2...",
            our_utxos=[("def456", 0)],
            txid="txid_second_entry2",
        )
        append_history_entry(entry2, temp_data_dir)

        # Mark only the second entry as failed using txid for disambiguation
        result = mark_pending_transaction_failed(
            destination_address="bc1qsameaddress12345",
            failure_reason="Transaction not found",
            data_dir=temp_data_dir,
            txid="txid_second_entry2",
        )
        assert result is True

        # Verify only the second entry was marked failed
        entries = read_history(temp_data_dir)
        assert len(entries) == 2

        # First entry should still be pending
        first = [e for e in entries if e.txid == "txid_first_entry_11"][0]
        assert first.success is False
        assert first.failure_reason == "Pending confirmation"
        assert first.completed_at == ""

        # Second entry should be marked failed
        second = [e for e in entries if e.txid == "txid_second_entry2"][0]
        assert second.success is False
        assert second.failure_reason == "Transaction not found"
        assert second.completed_at != ""


class TestCleanupStalePendingTransactions:
    """Tests for cleanup_stale_pending_transactions function."""

    def test_cleanup_old_pending_entries(self, temp_data_dir: Path) -> None:
        """Test that old pending entries are cleaned up."""

        # Create an old pending entry (2 hours ago)
        old_timestamp = (datetime.now() - timedelta(hours=2)).isoformat()
        old_entry = TransactionHistoryEntry(
            timestamp=old_timestamp,
            role="maker",
            txid="old_pending_txid123",
            cj_amount=1_000_000,
            success=False,
            failure_reason="Pending confirmation",
            confirmations=0,
            completed_at="",  # Not completed
            destination_address="bc1qoldpending12345",
        )
        append_history_entry(old_entry, temp_data_dir)

        # Create a recent pending entry (5 minutes ago)
        recent_timestamp = (datetime.now() - timedelta(minutes=5)).isoformat()
        recent_entry = TransactionHistoryEntry(
            timestamp=recent_timestamp,
            role="maker",
            txid="recent_pending_tx12",
            cj_amount=2_000_000,
            success=False,
            failure_reason="Pending confirmation",
            confirmations=0,
            completed_at="",  # Not completed
            destination_address="bc1qrecentpending1",
        )
        append_history_entry(recent_entry, temp_data_dir)

        # Verify both are pending before cleanup
        pending = get_pending_transactions(temp_data_dir)
        assert len(pending) == 2

        # Clean up with 60 minute threshold
        count = cleanup_stale_pending_transactions(max_age_minutes=60, data_dir=temp_data_dir)
        assert count == 1  # Only the old one should be cleaned

        # Verify only recent entry is still pending
        pending = get_pending_transactions(temp_data_dir)
        assert len(pending) == 1
        assert pending[0].txid == "recent_pending_tx12"

        # Verify old entry was marked as failed
        entries = read_history(temp_data_dir)
        old = [e for e in entries if e.txid == "old_pending_txid123"][0]
        assert old.completed_at != ""
        assert "Cleaned up" in old.failure_reason

    def test_cleanup_does_not_touch_confirmed(self, temp_data_dir: Path) -> None:
        """Test that confirmed entries are not affected by cleanup."""

        # Create an old confirmed entry
        old_timestamp = (datetime.now() - timedelta(hours=24)).isoformat()
        confirmed_entry = TransactionHistoryEntry(
            timestamp=old_timestamp,
            role="maker",
            txid="confirmed_txid12345",
            cj_amount=1_000_000,
            success=True,
            failure_reason="",
            confirmations=6,
            completed_at=old_timestamp,
            destination_address="bc1qconfirmed12345",
        )
        append_history_entry(confirmed_entry, temp_data_dir)

        # Clean up
        count = cleanup_stale_pending_transactions(max_age_minutes=60, data_dir=temp_data_dir)
        assert count == 0

        # Verify entry unchanged
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].success is True
        assert entries[0].confirmations == 6

    def test_cleanup_with_no_entries(self, temp_data_dir: Path) -> None:
        """Test cleanup with empty history."""
        count = cleanup_stale_pending_transactions(max_age_minutes=60, data_dir=temp_data_dir)
        assert count == 0

    def test_cleanup_does_not_touch_already_failed(self, temp_data_dir: Path) -> None:
        """Test that already-failed entries are not re-processed."""

        # Create an old failed entry (has completed_at set)
        old_timestamp = (datetime.now() - timedelta(hours=24)).isoformat()
        failed_entry = TransactionHistoryEntry(
            timestamp=old_timestamp,
            role="maker",
            txid="failed_txid1234567",
            cj_amount=1_000_000,
            success=False,
            failure_reason="Original failure reason",
            confirmations=0,
            completed_at=(datetime.now() - timedelta(hours=23)).isoformat(),  # Already completed
            destination_address="bc1qfailed12345678",
        )
        append_history_entry(failed_entry, temp_data_dir)

        # Clean up
        count = cleanup_stale_pending_transactions(max_age_minutes=60, data_dir=temp_data_dir)
        assert count == 0  # Should not be cleaned (already has completed_at)

        # Verify failure reason unchanged
        entries = read_history(temp_data_dir)
        assert entries[0].failure_reason == "Original failure reason"


class TestAddressHistoryTypesAfterConfirmation:
    """Tests for get_address_history_types with confirmed maker entries.

    This tests a specific bug where addresses from confirmed CoinJoin transactions
    were showing as 'non-cj-change' instead of 'cj-out' because the history entry
    was created with success=False and get_address_history_types only returns
    'cj_out' for entries with success=True.
    """

    def test_maker_addresses_after_confirmation(self, temp_data_dir: Path) -> None:
        """Test that get_address_history_types returns correct types after confirmation.

        Bug scenario:
        1. Maker creates history entry with success=False (pending)
        2. Transaction gets confirmed, success=True is set
        3. get_address_history_types should return 'cj_out' for destination_address
           and 'change' for change_address
        """

        cj_address = "bc1q0690ccmpdrhha3eqau3ejha5p7pdyss0kxptzg"
        change_address = "bc1q8gkl5fg55zd4q3ff9jl2fkac287gks9akauaw6"

        # Step 1: Create a pending maker entry (success=False)
        entry = create_maker_history_entry(
            taker_nick="J52rYHcxwx9CxVJ1",
            cj_amount=98192,
            fee_received=0,
            txfee_contribution=0,
            cj_address=cj_address,
            change_address=change_address,
            our_utxos=[("input1", 0), ("input2", 1)],
            txid="81d70553942222a342b27d456475d3dc1b5212336366ab88bfd98cea5c1653e3",
        )
        append_history_entry(entry, temp_data_dir)

        # Verify entry is pending
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].success is False
        assert entries[0].failure_reason == "Pending confirmation"

        # At this point, get_address_history_types should return 'flagged'
        # because success=False
        history_types = get_address_history_types(temp_data_dir)
        assert history_types.get(cj_address) == "flagged"
        assert history_types.get(change_address) == "flagged"

        # Step 2: Confirm the transaction
        result = update_transaction_confirmation(
            txid="81d70553942222a342b27d456475d3dc1b5212336366ab88bfd98cea5c1653e3",
            confirmations=1,
            data_dir=temp_data_dir,
        )
        assert result is True

        # Verify entry is now successful
        entries = read_history(temp_data_dir)
        assert len(entries) == 1
        assert entries[0].success is True
        assert entries[0].confirmations == 1

        # Step 3: Now get_address_history_types should return correct types
        history_types = get_address_history_types(temp_data_dir)
        assert history_types.get(cj_address) == "cj_out", (
            f"Expected 'cj_out' for CJ output address, got {history_types.get(cj_address)}"
        )
        assert history_types.get(change_address) == "change", (
            f"Expected 'change' for change address, got {history_types.get(change_address)}"
        )

    def test_mixed_pending_and_confirmed_entries(self, temp_data_dir: Path) -> None:
        """Test that pending entries are flagged while confirmed are typed correctly."""

        # Create a confirmed entry
        confirmed_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            success=True,
            confirmations=6,
            txid="confirmed_txid_123",
            cj_amount=100000,
            destination_address="bc1qconfirmed_cj_out",
            change_address="bc1qconfirmed_change",
        )
        append_history_entry(confirmed_entry, temp_data_dir)

        # Create a pending entry
        pending_entry = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=50000,
            fee_received=0,
            txfee_contribution=0,
            cj_address="bc1qpending_cj_addr",
            change_address="bc1qpending_change",
            our_utxos=[("input", 0)],
            txid="pending_txid_456",
        )
        append_history_entry(pending_entry, temp_data_dir)

        history_types = get_address_history_types(temp_data_dir)

        # Confirmed addresses should have correct types
        assert history_types.get("bc1qconfirmed_cj_out") == "cj_out"
        assert history_types.get("bc1qconfirmed_change") == "change"

        # Pending addresses should be flagged (since tx not confirmed)
        assert history_types.get("bc1qpending_cj_addr") == "flagged"
        assert history_types.get("bc1qpending_change") == "flagged"

    def test_successful_tx_not_overwritten_by_failed(self, temp_data_dir: Path) -> None:
        """Test that successful tx type is not overwritten by later failed transactions.

        Bug scenario (real-world):
        1. Address bc1q069... used in successful CoinJoin (success=True)
        2. Same address later shared in multiple failed transactions (success=False)
        3. get_address_history_types was incorrectly returning 'flagged' because
           the failed entries came after the successful one and overwrote the type

        The fix ensures that once an address is used in a successful CoinJoin,
        it remains 'cj_out' or 'change' regardless of later failed transactions.
        """

        cj_address = "bc1q0690ccmpdrhha3eqau3ejha5p7pdyss0kxptzg"
        change_address = "bc1q8gkl5fg55zd4q3ff9jl2fkac287gks9akauaw6"

        # First: Create the SUCCESSFUL transaction (this should "win")
        successful_entry = TransactionHistoryEntry(
            timestamp="2026-01-18T05:47:41",
            completed_at="2026-01-18T05:54:55",
            role="maker",
            success=True,
            confirmations=1,
            txid="81d70553942222a342b27d456475d3dc1b5212336366ab88bfd98cea5c1653e3",
            cj_amount=98192,
            peer_count=6,
            destination_address=cj_address,
            change_address=change_address,
        )
        append_history_entry(successful_entry, temp_data_dir)

        # Then: Create multiple FAILED transactions using the same addresses
        # (This can happen when takers retry with the same maker address)
        for i, txid_prefix in enumerate(["2e01625d", "0d238e5e", "c3394222"]):
            failed_entry = TransactionHistoryEntry(
                timestamp=f"2026-01-17T{20 + i}:00:00",
                completed_at=f"2026-01-18T17:59:0{i}",
                role="maker",
                success=False,
                failure_reason="Timed out - taker never broadcast",
                confirmations=0,
                txid=txid_prefix + "00" * 28,
                cj_amount=98192,
                destination_address=cj_address,  # Same address!
                change_address=change_address,  # Same address!
            )
            append_history_entry(failed_entry, temp_data_dir)

        # Verify the address types - successful should take precedence
        history_types = get_address_history_types(temp_data_dir)

        assert history_types.get(cj_address) == "cj_out", (
            f"Expected 'cj_out' for CJ output address used in successful tx, "
            f"got '{history_types.get(cj_address)}'"
        )
        assert history_types.get(change_address) == "change", (
            f"Expected 'change' for change address used in successful tx, "
            f"got '{history_types.get(change_address)}'"
        )

    def test_failed_only_address_is_flagged(self, temp_data_dir: Path) -> None:
        """Test that addresses ONLY used in failed transactions are flagged."""

        # Create only failed entries for this address
        failed_entry = TransactionHistoryEntry(
            timestamp="2026-01-17T20:00:00",
            completed_at="2026-01-18T17:59:00",
            role="maker",
            success=False,
            failure_reason="Timed out",
            confirmations=0,
            txid="failed_only_txid_123",
            cj_amount=50000,
            destination_address="bc1qfailed_only_addr",
            change_address="bc1qfailed_only_chg",
        )
        append_history_entry(failed_entry, temp_data_dir)

        history_types = get_address_history_types(temp_data_dir)

        # These should be flagged since they were never used successfully
        assert history_types.get("bc1qfailed_only_addr") == "flagged"
        assert history_types.get("bc1qfailed_only_chg") == "flagged"


class TestWalletFingerprintIsolation:
    """Issue #473: history must be scoped per wallet via wallet_fingerprint."""

    FP_A = "aabbccdd"
    FP_B = "11223344"

    def _make(self, fp: str, txid: str, addr: str) -> TransactionHistoryEntry:
        return create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=100_000,
            fee_received=10,
            txfee_contribution=5,
            cj_address=addr,
            change_address="bc1qchg" + fp,
            our_utxos=[(txid, 0)],
            txid=txid,
            network="regtest",
            wallet_fingerprint=fp,
        )

    def test_read_history_filters_by_fingerprint(self, temp_data_dir: Path) -> None:
        append_history_entry(self._make(self.FP_A, "a" * 64, "bc1qaaaa"), temp_data_dir)
        append_history_entry(self._make(self.FP_B, "b" * 64, "bc1qbbbb"), temp_data_dir)

        a_only = read_history(temp_data_dir, wallet_fingerprint=self.FP_A)
        b_only = read_history(temp_data_dir, wallet_fingerprint=self.FP_B)
        all_entries = read_history(temp_data_dir)

        assert len(a_only) == 1 and a_only[0].destination_address == "bc1qaaaa"
        assert len(b_only) == 1 and b_only[0].destination_address == "bc1qbbbb"
        assert len(all_entries) == 2

    def test_get_used_addresses_filters_by_fingerprint(self, temp_data_dir: Path) -> None:
        append_history_entry(self._make(self.FP_A, "a" * 64, "bc1qaaaa"), temp_data_dir)
        append_history_entry(self._make(self.FP_B, "b" * 64, "bc1qbbbb"), temp_data_dir)

        addrs_a = get_used_addresses(temp_data_dir, wallet_fingerprint=self.FP_A)
        addrs_b = get_used_addresses(temp_data_dir, wallet_fingerprint=self.FP_B)

        assert "bc1qaaaa" in addrs_a and "bc1qbbbb" not in addrs_a
        assert "bc1qbbbb" in addrs_b and "bc1qaaaa" not in addrs_b

    def test_get_pending_transactions_scoped(self, temp_data_dir: Path) -> None:
        # Pending entries (no txid)
        a = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=100_000,
            fee_received=10,
            txfee_contribution=5,
            cj_address="bc1qaaaa",
            change_address="bc1qchg-a",
            our_utxos=[("a" * 64, 0)],
            txid="",
            network="regtest",
            wallet_fingerprint=self.FP_A,
        )
        b = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=200_000,
            fee_received=20,
            txfee_contribution=5,
            cj_address="bc1qbbbb",
            change_address="bc1qchg-b",
            our_utxos=[("b" * 64, 0)],
            txid="",
            network="regtest",
            wallet_fingerprint=self.FP_B,
        )
        append_history_entry(a, temp_data_dir)
        append_history_entry(b, temp_data_dir)

        pa = get_pending_transactions(temp_data_dir, wallet_fingerprint=self.FP_A)
        pb = get_pending_transactions(temp_data_dir, wallet_fingerprint=self.FP_B)
        assert len(pa) == 1 and pa[0].destination_address == "bc1qaaaa"
        assert len(pb) == 1 and pb[0].destination_address == "bc1qbbbb"

    def test_legacy_entries_without_fingerprint_visible_unfiltered(
        self, temp_data_dir: Path
    ) -> None:
        """Backwards compat: pre-#473 rows have empty fingerprint and remain
        visible when no filter is supplied."""
        legacy = create_maker_history_entry(
            taker_nick="J5taker",
            cj_amount=100_000,
            fee_received=10,
            txfee_contribution=5,
            cj_address="bc1qlegacy",
            change_address="bc1qchg-legacy",
            our_utxos=[("c" * 64, 0)],
            txid="c" * 64,
            network="regtest",
            # No wallet_fingerprint argument -> empty string default.
        )
        assert legacy.wallet_fingerprint == ""
        append_history_entry(legacy, temp_data_dir)

        all_entries = read_history(temp_data_dir)
        assert len(all_entries) == 1

        # When filtering by a real fingerprint, legacy rows are correctly hidden
        # to keep new wallets isolated from pre-existing shared history.
        scoped = read_history(temp_data_dir, wallet_fingerprint=self.FP_A)
        assert scoped == []


# Header used by the v0.27.x release, before the wallet_fingerprint column
# was appended in v0.28.0 (issue #473).
_LEGACY_V027_HEADER = (
    "timestamp,completed_at,role,success,failure_reason,confirmations,"
    "confirmed_at,txid,cj_amount,peer_count,counterparty_nicks,fee_received,"
    "txfee_contribution,total_maker_fees_paid,mining_fee_paid,net_fee,"
    "source_mixdepth,destination_address,change_address,utxos_used,"
    "broadcast_method,network"
)


class TestLegacyHeaderMigration:
    """0.28.0 added a wallet_fingerprint column without migrating legacy CSV
    files. New writes against an old header silently produced rows with an
    extra trailing cell that csv.DictReader dropped on read, leaving makers
    unable to update their pending entries to success=True. The daily
    summary therefore reported successful=0 indefinitely."""

    FP = "deadbeef"

    def test_pure_legacy_file_migrates_on_append(self, temp_data_dir: Path) -> None:
        """Pre-existing v0.27.x CSV with no new rows: appending must rewrite
        the header so subsequent rows include the wallet_fingerprint cell."""
        path = temp_data_dir / "coinjoin_history.csv"
        path.write_text(
            f"{_LEGACY_V027_HEADER}\n"
            "2026-01-01T00:00:00,2026-01-01T00:01:00,maker,True,,1,"
            "2026-01-01T00:01:00,oldtx,200000,,,1000,0,0,0,0,0,,,,,mainnet\n"
        )

        entry = TransactionHistoryEntry(
            timestamp=datetime.now().isoformat(),
            role="maker",
            success=False,
            failure_reason="Pending confirmation",
            txid="newtx",
            cj_amount=100_000,
            wallet_fingerprint=self.FP,
        )
        append_history_entry(entry, temp_data_dir)

        # The legacy row's missing fingerprint stays empty (correct: that
        # row predates the column and cannot belong to any wallet).
        # The new row's fingerprint must round-trip.
        all_entries = read_history(temp_data_dir)
        by_txid = {e.txid: e for e in all_entries}
        assert by_txid["oldtx"].wallet_fingerprint == ""
        assert by_txid["newtx"].wallet_fingerprint == self.FP

        # Filtered read must surface the new entry, not silently drop it.
        scoped = read_history(temp_data_dir, wallet_fingerprint=self.FP)
        assert len(scoped) == 1
        assert scoped[0].txid == "newtx"

    def test_corrupted_file_recovers_trailing_fingerprint(self, temp_data_dir: Path) -> None:
        """File already in the broken state: legacy 22-col header with rows
        that 0.28.x writers appended a 23rd cell to. Migration must recover
        those trailing cells into wallet_fingerprint instead of discarding
        them."""
        path = temp_data_dir / "coinjoin_history.csv"
        # Use recent timestamps so the 24h stats window includes them.
        recent = datetime.now().isoformat()
        path.write_text(
            f"{_LEGACY_V027_HEADER}\n"
            f"{recent},{recent},maker,True,,1,"
            f"{recent},confirmedtx,200000,,,1000,0,0,0,0,0,,,,,"
            f"mainnet,{self.FP}\n"
            f"{recent},,maker,False,Pending confirmation,0,,"
            "pendingtx,300000,,,1500,0,0,0,0,0,,,,,"
            f"mainnet,{self.FP}\n"
        )

        entries = read_history(temp_data_dir, wallet_fingerprint=self.FP)
        # Both rows must be recovered under the active wallet's filter.
        assert {e.txid for e in entries} == {"confirmedtx", "pendingtx"}
        assert all(e.wallet_fingerprint == self.FP for e in entries)

        # And confirming the pending entry must now succeed end-to-end so
        # the periodic summary sees successful_coinjoins > 0.
        assert update_transaction_confirmation(
            "pendingtx", 1, temp_data_dir, wallet_fingerprint=self.FP
        )
        stats = get_history_stats_for_period(
            24, role_filter="maker", data_dir=temp_data_dir, wallet_fingerprint=self.FP
        )
        assert stats["successful_coinjoins"] == 2

    def test_migration_is_idempotent(self, temp_data_dir: Path) -> None:
        """Calling read/append on an already-current file must not rewrite
        it (no spurious churn) and must preserve all rows verbatim."""
        entry = TransactionHistoryEntry(
            timestamp="2026-02-02T00:00:00",
            role="maker",
            success=True,
            txid="abc",
            cj_amount=1,
            wallet_fingerprint=self.FP,
        )
        append_history_entry(entry, temp_data_dir)
        path = temp_data_dir / "history.csv"
        before = path.read_text()

        # Re-read several times: file content must be byte-identical.
        for _ in range(3):
            read_history(temp_data_dir)
        assert path.read_text() == before


# A complete-but-reordered header as written by builds where
# ``source_addresses`` was inserted right after ``utxos_used`` instead of last.
# All 24 canonical columns are present, only the order differs. Appending a row
# (written by DictWriter in canonical order) against this header shifts every
# column after ``utxos_used`` on read: ``wallet_fingerprint`` reads back as a
# source address and ``network`` reads back as the fingerprint.
_REORDERED_HEADER = (
    "timestamp,completed_at,role,success,failure_reason,confirmations,"
    "confirmed_at,txid,cj_amount,peer_count,counterparty_nicks,fee_received,"
    "txfee_contribution,total_maker_fees_paid,mining_fee_paid,net_fee,"
    "source_mixdepth,destination_address,change_address,utxos_used,"
    "source_addresses,broadcast_method,network,wallet_fingerprint"
)


class TestReorderedHeaderMigration:
    """A field moved in the dataclass declaration (``source_addresses`` to
    last) left older CSV files with a complete-but-reordered header. The
    migration used to bail out on reorder ("all columns present"), so new
    appends, written in canonical column order by ``DictWriter``, were shifted
    against the stale header. On read a maker's ``wallet_fingerprint`` then
    surfaced as one of its input addresses and ``network`` as the fingerprint,
    breaking per-wallet pending lookups and the ``jm-wallet history`` wallet
    auto-selection (which listed addresses as fingerprints)."""

    FP = "30e919c2"

    def test_append_against_reordered_header_stays_aligned(self, temp_data_dir: Path) -> None:
        path = temp_data_dir / "history.csv"
        path.write_text(_REORDERED_HEADER + "\n")

        entry = TransactionHistoryEntry(
            timestamp="2026-05-30T21:02:12",
            role="maker",
            success=False,
            failure_reason="Awaiting transaction",
            cj_amount=1_000_000,
            destination_address="tb1qdest",
            change_address="tb1qchange",
            utxos_used="aa:0",
            source_addresses="tb1qsrc1,tb1qsrc2",
            network="signet",
            wallet_fingerprint=self.FP,
        )
        append_history_entry(entry, temp_data_dir)

        got = read_history(temp_data_dir)[0]
        assert got.wallet_fingerprint == self.FP
        assert got.network == "signet"
        assert got.source_addresses == "tb1qsrc1,tb1qsrc2"
        assert got.broadcast_method == ""

    def test_recovers_already_shifted_rows(self, temp_data_dir: Path) -> None:
        """A file that already contains both a correctly-aligned legacy row
        and a row a newer writer appended in canonical order against the stale
        header. Migration must keep the aligned row and un-shift the other."""
        path = temp_data_dir / "history.csv"
        aligned = (
            "2026-05-29T10:00:00,,maker,True,,1,2026-05-29T10:01:00,goodtx,"
            "200000,,Jnick,1000,0,0,0,0,0,tb1qd,tb1qc,bb:0,tb1qsrcA,,signet,"
            f"{self.FP}"
        )
        shifted = (
            "2026-05-30T21:02:12,,maker,False,Awaiting transaction,0,,,1000000,"
            f",Jnick2,0,0,0,0,0,0,tb1qd2,tb1qc2,cc:0,,signet,{self.FP},tb1qsrcB"
        )
        path.write_text(_REORDERED_HEADER + "\n" + aligned + "\n" + shifted + "\n")

        rows = {e.txid: e for e in read_history(temp_data_dir)}
        assert rows["goodtx"].wallet_fingerprint == self.FP
        assert rows["goodtx"].source_addresses == "tb1qsrcA"
        # The pending shifted row, recovered under the active fingerprint.
        recovered = next(e for e in read_history(temp_data_dir) if not e.txid)
        assert recovered.wallet_fingerprint == self.FP
        assert recovered.network == "signet"
        assert recovered.source_addresses == "tb1qsrcB"
        assert recovered.broadcast_method == ""

    def test_recovered_pending_confirms_end_to_end(self, temp_data_dir: Path) -> None:
        """The whole point: once recovered, a pending maker row matches its
        wallet's fingerprint so the confirmation update flips it to success."""
        path = temp_data_dir / "history.csv"
        recent = datetime.now().isoformat()
        shifted = (
            f"{recent},,maker,False,Awaiting transaction,0,,pendingtx,1000000,"
            f",Jnick,0,0,0,0,0,0,tb1qd,tb1qc,cc:0,,signet,{self.FP},tb1qsrcB"
        )
        path.write_text(_REORDERED_HEADER + "\n" + shifted + "\n")

        # Before the fix this lookup found nothing (fingerprint read as an
        # address), so the pending row never confirmed.
        assert update_transaction_confirmation(
            "pendingtx", 1, temp_data_dir, wallet_fingerprint=self.FP
        )
        stats = get_history_stats_for_period(
            24, role_filter="maker", data_dir=temp_data_dir, wallet_fingerprint=self.FP
        )
        assert stats["successful_coinjoins"] == 1

    def test_migration_is_idempotent(self, temp_data_dir: Path) -> None:
        path = temp_data_dir / "history.csv"
        path.write_text(
            _REORDERED_HEADER + "\n"
            "2026-05-30T21:02:12,,maker,False,Awaiting transaction,0,,tx,1000000,"
            f",Jnick,0,0,0,0,0,0,tb1qd,tb1qc,cc:0,,signet,{self.FP},tb1qsrcB\n"
        )
        read_history(temp_data_dir)  # one-shot rewrite to canonical order
        after_first = path.read_text()
        for _ in range(3):
            read_history(temp_data_dir)
        assert path.read_text() == after_first


class TestLegacyFilenameMigration:
    """The history CSV was renamed from coinjoin_history.csv to history.csv.

    Existing installs must keep working without manual intervention: the
    first time any history API is called against a data directory that
    still has the legacy filename, the file must be renamed in place and
    all subsequent reads/writes must target the new name.
    """

    def test_legacy_file_renamed_in_place(self, temp_data_dir: Path) -> None:
        legacy_path = temp_data_dir / "coinjoin_history.csv"
        new_path = temp_data_dir / "history.csv"
        # Seed the data dir with the legacy filename only.
        entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="taker",
            success=True,
            txid="legacy_tx" * 7 + "abc",
            cj_amount=42,
        )
        # Append once via the public API while pretending the file is at
        # the legacy location: write rows directly, then trigger a read.
        from jmwallet.history import _get_fieldnames

        with open(legacy_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_get_fieldnames())
            writer.writeheader()
            writer.writerow({name: getattr(entry, name) for name in _get_fieldnames()})

        assert legacy_path.exists() and not new_path.exists()

        rows = read_history(temp_data_dir)
        assert {e.txid for e in rows} == {entry.txid}
        # After the read, the file must be at the new location and the
        # legacy name must be gone.
        assert new_path.exists()
        assert not legacy_path.exists()

    def test_both_files_present_keeps_canonical_and_warns(self, temp_data_dir: Path) -> None:
        """If both names exist (e.g., user manually restored a backup), do
        not silently overwrite the canonical file; keep both untouched and
        operate on ``history.csv``."""
        legacy_path = temp_data_dir / "coinjoin_history.csv"
        new_path = temp_data_dir / "history.csv"
        from jmwallet.history import _get_fieldnames

        # Same header, different content, in each file.
        legacy_entry = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00", role="maker", txid="legacy", cj_amount=1
        )
        new_entry = TransactionHistoryEntry(
            timestamp="2024-01-02T00:00:00", role="maker", txid="canonical", cj_amount=2
        )
        for path, e in ((legacy_path, legacy_entry), (new_path, new_entry)):
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_get_fieldnames())
                writer.writeheader()
                writer.writerow({name: getattr(e, name) for name in _get_fieldnames()})

        rows = read_history(temp_data_dir)
        # Only the canonical file's content is surfaced.
        assert {e.txid for e in rows} == {"canonical"}
        # Both files remain on disk (legacy not destroyed).
        assert legacy_path.exists()
        assert new_path.exists()


class TestSendHistoryEntry:
    """Tests for plain (non-CoinJoin) send history entries.

    Regression for the bug where ``jm-wallet send`` did not record the
    destination/change addresses, leaving the wallet to propose the same
    addresses again on the next sync whenever Bitcoin Core's transaction
    history did not surface the spend (e.g., outside the smart-scan
    window or after an interrupted background rescan).
    """

    def test_round_trip_persists_addresses_as_used(self, temp_data_dir: Path) -> None:
        entry = create_send_history_entry(
            destination="bc1qdest1234567890abcdef1234567890abcdef1234",
            change_address="bc1qchg1234567890abcdef1234567890abcdef1234",
            amount=666,
            mining_fee=178,
            source_mixdepth=4,
            selected_utxos=[("aabb" * 16, 3), ("ccdd" * 16, 7)],
            txid="ee" * 32,
            success=True,
        )
        append_history_entry(entry, temp_data_dir)

        round_trip = read_history(temp_data_dir)
        assert len(round_trip) == 1
        assert round_trip[0].role == "send"
        assert round_trip[0].destination_address == entry.destination_address
        assert round_trip[0].change_address == entry.change_address
        assert round_trip[0].source_mixdepth == 4
        assert round_trip[0].net_fee == -178  # signed: cost
        assert round_trip[0].utxos_used == f"{'aabb' * 16}:3,{'ccdd' * 16}:7"

        used = get_used_addresses(temp_data_dir)
        assert entry.destination_address in used
        assert entry.change_address in used

    def test_sweep_send_has_no_change_address(self, temp_data_dir: Path) -> None:
        entry = create_send_history_entry(
            destination="bc1qdest1234567890abcdef1234567890abcdef1234",
            change_address="",
            amount=10_000,
            mining_fee=200,
            source_mixdepth=0,
            selected_utxos=[("aa" * 32, 0)],
            success=True,
        )
        append_history_entry(entry, temp_data_dir)

        used = get_used_addresses(temp_data_dir)
        assert entry.destination_address in used
        # Empty change must not be reported as a "used" address.
        assert "" not in used

    def test_role_filter_excludes_send(self, temp_data_dir: Path) -> None:
        """``role_filter="maker"|"taker"`` must not surface send entries."""
        send = create_send_history_entry(
            destination="bc1qdest1234567890abcdef1234567890abcdef1234",
            change_address="bc1qchg1234567890abcdef1234567890abcdef1234",
            amount=10_000,
            mining_fee=200,
            source_mixdepth=0,
            selected_utxos=[("aa" * 32, 0)],
            success=True,
        )
        maker = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="maker",
            success=True,
            txid="cd" * 32,
            cj_amount=1_000_000,
        )
        append_history_entry(send, temp_data_dir)
        append_history_entry(maker, temp_data_dir)

        assert {e.role for e in read_history(temp_data_dir, role_filter="maker")} == {"maker"}
        assert read_history(temp_data_dir, role_filter="taker") == []

    def test_update_send_awaiting_broadcast_success(self, temp_data_dir: Path) -> None:
        pending = create_send_history_entry(
            destination="bc1qdest1234567890abcdef1234567890abcdef1234",
            change_address="bc1qchg1234567890abcdef1234567890abcdef1234",
            amount=666,
            mining_fee=178,
            source_mixdepth=4,
            selected_utxos=[("aa" * 32, 0)],
            txid="",
            success=False,
            failure_reason="awaiting broadcast",
        )
        append_history_entry(pending, temp_data_dir)

        ok = update_send_awaiting_broadcast(
            pending,
            txid="dd" * 32,
            success=True,
            failure_reason="",
            data_dir=temp_data_dir,
        )
        assert ok is True

        rows = read_history(temp_data_dir)
        assert len(rows) == 1
        assert rows[0].success is True
        assert rows[0].txid == "dd" * 32
        assert rows[0].failure_reason == ""
        assert rows[0].completed_at == rows[0].timestamp

    def test_update_send_awaiting_broadcast_failure(self, temp_data_dir: Path) -> None:
        pending = create_send_history_entry(
            destination="bc1qdest1234567890abcdef1234567890abcdef1234",
            change_address="",
            amount=10_000,
            mining_fee=200,
            source_mixdepth=0,
            selected_utxos=[("aa" * 32, 0)],
            txid="",
            success=False,
            failure_reason="awaiting broadcast",
        )
        append_history_entry(pending, temp_data_dir)

        ok = update_send_awaiting_broadcast(
            pending,
            txid="",
            success=False,
            failure_reason="broadcast failed",
            data_dir=temp_data_dir,
        )
        assert ok is True

        rows = read_history(temp_data_dir)
        assert len(rows) == 1
        assert rows[0].success is False
        assert rows[0].failure_reason == "broadcast failed"
        # Even though the broadcast failed, the addresses must remain in the
        # used-set so a fresh wallet does not re-issue them.
        used = get_used_addresses(temp_data_dir)
        assert pending.destination_address in used

    def test_send_addresses_not_classified_as_coinjoin(self, temp_data_dir: Path) -> None:
        """Regression for issue #517.

        A plain wallet send (internal mixdepth-to-mixdepth transfer) must not
        mark its destination as a CoinJoin output or its change as CoinJoin
        change. Otherwise ``jm-wallet info --extended`` mislabels them as
        ``cj-out`` / ``cj-change`` and creates false privacy expectations.
        """
        dest = "bc1qdest1234567890abcdef1234567890abcdef1234"
        change = "bc1qchg1234567890abcdef1234567890abcdef1234"
        send = create_send_history_entry(
            destination=dest,
            change_address=change,
            amount=20_000,
            mining_fee=200,
            source_mixdepth=0,
            selected_utxos=[("aa" * 32, 0)],
            txid="bb" * 32,
            success=True,
        )
        append_history_entry(send, temp_data_dir)

        types = get_address_history_types(temp_data_dir)
        assert dest not in types
        assert change not in types

        # Addresses are still treated as used so they are not re-issued.
        used = get_used_addresses(temp_data_dir)
        assert dest in used
        assert change in used

    def test_real_coinjoin_still_classified_with_send_present(self, temp_data_dir: Path) -> None:
        """Excluding ``send`` rows must not suppress genuine CoinJoin classification.

        If the same address is later used as a real CoinJoin output, it must
        still be reported as ``cj_out``.
        """
        shared = "bc1qshared1234567890abcdef1234567890abcdef12"
        send = create_send_history_entry(
            destination=shared,
            change_address="",
            amount=20_000,
            mining_fee=200,
            source_mixdepth=0,
            selected_utxos=[("aa" * 32, 0)],
            txid="bb" * 32,
            success=True,
        )
        taker = TransactionHistoryEntry(
            timestamp="2024-01-01T00:00:00",
            role="taker",
            success=True,
            txid="cd" * 32,
            cj_amount=1_000_000,
            destination_address=shared,
        )
        append_history_entry(send, temp_data_dir)
        append_history_entry(taker, temp_data_dir)

        types = get_address_history_types(temp_data_dir)
        assert types.get(shared) == "cj_out"


class TestYieldGeneratorReport:
    """``format_yield_generator_report`` synthesizes the reference earn report."""

    def _maker(
        self,
        *,
        cj_amount: int,
        fee_received: int,
        txfee_contribution: int,
        success: bool = True,
        timestamp: str = "2024-01-01T10:00:00",
        confirmed_at: str = "2024-01-01T10:06:00",
        txid: str = "ab",
        fingerprint: str = "deadbeef",
    ) -> TransactionHistoryEntry:
        return TransactionHistoryEntry(
            timestamp=timestamp,
            confirmed_at=confirmed_at,
            role="maker",
            success=success,
            confirmations=1 if success else 0,
            txid=txid * 32,
            cj_amount=cj_amount,
            counterparty_nicks="J5xtaker",
            fee_received=fee_received,
            txfee_contribution=txfee_contribution,
            net_fee=fee_received - txfee_contribution,
            utxos_used=f"{txid * 32}:0,{txid * 32}:1",
            network="regtest",
            wallet_fingerprint=fingerprint,
        )

    def test_empty_history_returns_header_and_marker(self, temp_data_dir: Path) -> None:
        rows = format_yield_generator_report(temp_data_dir)
        assert rows[0] == ",".join(YIELD_GENERATOR_REPORT_HEADER)
        assert rows[1].endswith(",Connected")
        assert len(rows) == 2

    def test_successful_maker_row_mapped_to_reference_format(self, temp_data_dir: Path) -> None:
        append_history_entry(
            self._maker(cj_amount=100_000, fee_received=2_680, txfee_contribution=200),
            temp_data_dir,
        )
        rows = format_yield_generator_report(temp_data_dir)
        assert len(rows) == 3
        cols = rows[-1].split(",")
        assert cols[0] == "2024/01/01 10:00:00"  # reference timestamp format
        assert cols[1] == "100000"  # cj amount
        assert cols[2] == "2"  # input count from utxos_used
        assert cols[3] == "0"  # input value not retained
        assert cols[4] == "2680"  # cjfee == fee_received
        assert cols[5] == str(2_680 - 200)  # earned == net_fee
        assert cols[6] == "6.0"  # confirm minutes (10:00 -> 10:06)
        assert cols[7] == ""  # notes

    def test_pending_or_failed_maker_rows_excluded(self, temp_data_dir: Path) -> None:
        append_history_entry(
            self._maker(
                cj_amount=50_000,
                fee_received=0,
                txfee_contribution=0,
                success=False,
                txid="cd",
            ),
            temp_data_dir,
        )
        rows = format_yield_generator_report(temp_data_dir)
        assert len(rows) == 2  # header + marker only

    def test_taker_rows_excluded(self, temp_data_dir: Path) -> None:
        taker = create_taker_history_entry(
            maker_nicks=["J5maker"],
            cj_amount=100_000,
            total_maker_fees=300,
            mining_fee=200,
            destination="bcrt1qdest",
            change_address="bcrt1qchange",
            source_mixdepth=0,
            selected_utxos=[("ee" * 32, 0)],
            network="regtest",
            wallet_fingerprint="deadbeef",
        )
        append_history_entry(taker, temp_data_dir)
        rows = format_yield_generator_report(temp_data_dir)
        assert len(rows) == 2  # no maker earnings

    def test_multiple_rows_sorted_chronologically(self, temp_data_dir: Path) -> None:
        append_history_entry(
            self._maker(
                cj_amount=2,
                fee_received=20,
                txfee_contribution=1,
                timestamp="2024-02-01T10:00:00",
                txid="22",
            ),
            temp_data_dir,
        )
        append_history_entry(
            self._maker(
                cj_amount=1,
                fee_received=10,
                txfee_contribution=1,
                timestamp="2024-01-01T10:00:00",
                txid="11",
            ),
            temp_data_dir,
        )
        rows = format_yield_generator_report(temp_data_dir)
        # header + marker + 2 earnings, oldest first
        assert len(rows) == 4
        assert rows[2].split(",")[1] == "1"  # Jan row first
        assert rows[3].split(",")[1] == "2"  # Feb row second

    def test_wallet_fingerprint_filter(self, temp_data_dir: Path) -> None:
        append_history_entry(
            self._maker(
                cj_amount=1,
                fee_received=10,
                txfee_contribution=1,
                fingerprint="aaaaaaaa",
                txid="aa",
            ),
            temp_data_dir,
        )
        append_history_entry(
            self._maker(
                cj_amount=2,
                fee_received=20,
                txfee_contribution=1,
                fingerprint="bbbbbbbb",
                txid="bb",
            ),
            temp_data_dir,
        )
        rows = format_yield_generator_report(temp_data_dir, wallet_fingerprint="aaaaaaaa")
        assert len(rows) == 3  # only wallet aaaaaaaa's row
        assert rows[-1].split(",")[1] == "1"
