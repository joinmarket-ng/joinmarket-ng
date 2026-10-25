"""Tests for message deduplication module."""

from __future__ import annotations

import time

import pytest

from jmcore.deduplication import (
    DeduplicationStats,
    MessageDeduplicator,
    ResponseDeduplicator,
)


class TestDeduplicationStats:
    """Tests for DeduplicationStats dataclass."""

    def test_default_values(self) -> None:
        """Test default values are zero."""
        stats = DeduplicationStats()
        assert stats.total_processed == 0
        assert stats.duplicates_dropped == 0
        assert stats.unique_messages == 0

    def test_duplicate_rate_zero_processed(self) -> None:
        """Test duplicate rate is 0 when nothing processed."""
        stats = DeduplicationStats()
        assert stats.duplicate_rate == 0.0

    def test_duplicate_rate_calculation(self) -> None:
        """Test duplicate rate calculation."""
        stats = DeduplicationStats(
            total_processed=100,
            duplicates_dropped=25,
            unique_messages=75,
        )
        assert stats.duplicate_rate == 25.0

    def test_duplicate_rate_all_duplicates(self) -> None:
        """Test duplicate rate when all are duplicates."""
        stats = DeduplicationStats(
            total_processed=10,
            duplicates_dropped=10,
            unique_messages=0,
        )
        assert stats.duplicate_rate == 100.0


class TestMessageDeduplicator:
    """Tests for MessageDeduplicator class."""

    def test_first_message_not_duplicate(self) -> None:
        """First message should not be marked as duplicate."""
        dedup = MessageDeduplicator()
        fingerprint = MessageDeduplicator.make_fingerprint("alice", "fill", "order1")

        is_dup, source, count = dedup.is_duplicate(fingerprint, "dir1.onion")

        assert is_dup is False
        assert source == "dir1.onion"
        assert count == 1

    def test_second_message_is_duplicate(self) -> None:
        """Second message with same fingerprint should be duplicate."""
        dedup = MessageDeduplicator()
        fingerprint = MessageDeduplicator.make_fingerprint("alice", "fill", "order1")

        # First message
        dedup.is_duplicate(fingerprint, "dir1.onion")

        # Second message from different source
        is_dup, source, count = dedup.is_duplicate(fingerprint, "dir2.onion")

        assert is_dup is True
        assert source == "dir1.onion"  # First source
        assert count == 2

    def test_multiple_duplicates_count(self) -> None:
        """Multiple duplicates should be counted correctly."""
        dedup = MessageDeduplicator()
        fingerprint = MessageDeduplicator.make_fingerprint("alice", "fill", "order1")

        # Receive same message 5 times
        for i in range(5):
            is_dup, source, count = dedup.is_duplicate(fingerprint, f"dir{i}.onion")
            if i == 0:
                assert is_dup is False
                assert count == 1
            else:
                assert is_dup is True
                assert count == i + 1
                assert source == "dir0.onion"  # Always first source

    def test_different_fingerprints_not_duplicate(self) -> None:
        """Different fingerprints should not be considered duplicates."""
        dedup = MessageDeduplicator()
        fp1 = MessageDeduplicator.make_fingerprint("alice", "fill", "order1")
        fp2 = MessageDeduplicator.make_fingerprint("alice", "fill", "order2")
        fp3 = MessageDeduplicator.make_fingerprint("bob", "fill", "order1")

        is_dup1, _, _ = dedup.is_duplicate(fp1, "dir1")
        is_dup2, _, _ = dedup.is_duplicate(fp2, "dir1")
        is_dup3, _, _ = dedup.is_duplicate(fp3, "dir1")

        assert is_dup1 is False
        assert is_dup2 is False
        assert is_dup3 is False

    def test_window_expiry(self) -> None:
        """Messages outside window should not be considered duplicates."""
        dedup = MessageDeduplicator(window_seconds=0.1)  # 100ms window
        fingerprint = MessageDeduplicator.make_fingerprint("alice", "fill", "order1")

        # First message
        dedup.is_duplicate(fingerprint, "dir1.onion")

        # Wait for window to expire
        time.sleep(0.15)

        # Second message should NOT be duplicate (window expired)
        is_dup, source, count = dedup.is_duplicate(fingerprint, "dir2.onion")

        assert is_dup is False
        assert source == "dir2.onion"  # New first source
        assert count == 1

    def test_expiry_drops_only_expired_entries(self, monkeypatch: pytest.MonkeyPatch) -> None:
        clock = [0.0]
        monkeypatch.setattr("jmcore.deduplication.time.monotonic", lambda: clock[0])
        dedup = MessageDeduplicator(window_seconds=30.0)
        for i in range(10):
            clock[0] = float(i)
            dedup.is_duplicate(f"fp{i}", "dir1")

        clock[0] = 34.5
        dedup.is_duplicate("new", "dir1")

        assert len(dedup) == 6
        assert dedup.is_duplicate("fp4", "dir1")[0] is False
        assert dedup.is_duplicate("fp5", "dir1")[0] is True

    def test_check_cost_does_not_grow_with_window_size(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = [0.0]
        monkeypatch.setattr("jmcore.deduplication.time.monotonic", lambda: clock[0])
        dedup = MessageDeduplicator(window_seconds=30.0)
        for i in range(50_000):
            clock[0] = i / 2000
            dedup.is_duplicate(f"flood{i}", "dir1")

        start = time.perf_counter()
        for i in range(2_000):
            dedup.is_duplicate(f"probe{i}", "dir1")
        elapsed = time.perf_counter() - start

        assert len(dedup) == 52_000
        assert elapsed < 0.5

    def test_make_fingerprint_format(self) -> None:
        """Test fingerprint format."""
        fp = MessageDeduplicator.make_fingerprint("alice", "fill", "order123")
        assert fp == "alice:fill:order123"

    def test_make_fingerprint_empty_arg(self) -> None:
        """Test fingerprint with empty first arg."""
        fp = MessageDeduplicator.make_fingerprint("alice", "orderbook")
        assert fp == "alice:orderbook:"

    def test_stats_tracking(self) -> None:
        """Test that stats are tracked correctly."""
        dedup = MessageDeduplicator()
        fp1 = MessageDeduplicator.make_fingerprint("alice", "fill", "order1")
        fp2 = MessageDeduplicator.make_fingerprint("bob", "fill", "order2")

        # 2 unique messages
        dedup.is_duplicate(fp1, "dir1")
        dedup.is_duplicate(fp2, "dir1")
        # 3 duplicates
        dedup.is_duplicate(fp1, "dir2")
        dedup.is_duplicate(fp1, "dir3")
        dedup.is_duplicate(fp2, "dir2")

        stats = dedup.stats
        assert stats.total_processed == 5
        assert stats.unique_messages == 2
        assert stats.duplicates_dropped == 3
        assert stats.duplicate_rate == 60.0

    def test_reset_stats(self) -> None:
        """Test reset_stats clears counters."""
        dedup = MessageDeduplicator()
        fp = MessageDeduplicator.make_fingerprint("alice", "fill", "order1")

        dedup.is_duplicate(fp, "dir1")
        dedup.is_duplicate(fp, "dir2")
        dedup.reset_stats()

        assert dedup.stats.total_processed == 0
        assert dedup.stats.unique_messages == 0
        assert dedup.stats.duplicates_dropped == 0

    def test_clear(self) -> None:
        """Test clear removes all tracked messages and stats."""
        dedup = MessageDeduplicator()
        fp = MessageDeduplicator.make_fingerprint("alice", "fill", "order1")

        dedup.is_duplicate(fp, "dir1")
        assert len(dedup) == 1

        dedup.clear()

        assert len(dedup) == 0
        assert dedup.stats.total_processed == 0
        # Same message should not be duplicate after clear
        is_dup, _, _ = dedup.is_duplicate(fp, "dir2")
        assert is_dup is False

    def test_len(self) -> None:
        """Test __len__ returns tracked message count."""
        dedup = MessageDeduplicator()

        assert len(dedup) == 0

        dedup.is_duplicate("fp1", "dir1")
        assert len(dedup) == 1

        dedup.is_duplicate("fp2", "dir1")
        assert len(dedup) == 2

        # Duplicate doesn't increase count
        dedup.is_duplicate("fp1", "dir2")
        assert len(dedup) == 2


class TestResponseDeduplicator:
    """Tests for ResponseDeduplicator class."""

    def test_first_response_is_new(self) -> None:
        """First response from a maker should be marked as new."""
        dedup = ResponseDeduplicator()

        is_new = dedup.add_response("maker1", "pubkey", {"key": "abc123"}, "dir1")

        assert is_new is True

    def test_duplicate_response_from_same_maker(self) -> None:
        """Duplicate response from same maker should return False."""
        dedup = ResponseDeduplicator()

        dedup.add_response("maker1", "pubkey", {"key": "abc123"}, "dir1")
        is_new = dedup.add_response("maker1", "pubkey", {"key": "abc123"}, "dir2")

        assert is_new is False

    def test_different_makers_not_duplicate(self) -> None:
        """Responses from different makers should not be duplicates."""
        dedup = ResponseDeduplicator()

        is_new1 = dedup.add_response("maker1", "pubkey", {"key": "abc"}, "dir1")
        is_new2 = dedup.add_response("maker2", "pubkey", {"key": "xyz"}, "dir1")

        assert is_new1 is True
        assert is_new2 is True

    def test_different_commands_not_duplicate(self) -> None:
        """Responses for different commands should not be duplicates."""
        dedup = ResponseDeduplicator()

        is_new1 = dedup.add_response("maker1", "pubkey", {"key": "abc"}, "dir1")
        is_new2 = dedup.add_response("maker1", "ioauth", {"utxos": []}, "dir1")

        assert is_new1 is True
        assert is_new2 is True

    def test_get_responses(self) -> None:
        """Test retrieving responses for a command."""
        dedup = ResponseDeduplicator()

        dedup.add_response("maker1", "pubkey", "key1", "dir1")
        dedup.add_response("maker2", "pubkey", "key2", "dir1")
        dedup.add_response("maker1", "ioauth", "auth1", "dir1")

        pubkey_responses = dedup.get_responses("pubkey")
        assert len(pubkey_responses) == 2
        assert "maker1" in pubkey_responses
        assert "maker2" in pubkey_responses
        assert pubkey_responses["maker1"].data == "key1"

        ioauth_responses = dedup.get_responses("ioauth")
        assert len(ioauth_responses) == 1

    def test_get_responses_empty(self) -> None:
        """Test get_responses returns empty dict for unknown command."""
        dedup = ResponseDeduplicator()
        responses = dedup.get_responses("unknown")
        assert responses == {}

    def test_get_response_count(self) -> None:
        """Test response counting."""
        dedup = ResponseDeduplicator()

        assert dedup.get_response_count("pubkey") == 0

        dedup.add_response("maker1", "pubkey", "key1", "dir1")
        assert dedup.get_response_count("pubkey") == 1

        dedup.add_response("maker2", "pubkey", "key2", "dir2")
        assert dedup.get_response_count("pubkey") == 2

        # Duplicate doesn't increase count
        dedup.add_response("maker1", "pubkey", "key1", "dir3")
        assert dedup.get_response_count("pubkey") == 2

    def test_has_response(self) -> None:
        """Test checking if response exists."""
        dedup = ResponseDeduplicator()

        assert dedup.has_response("maker1", "pubkey") is False

        dedup.add_response("maker1", "pubkey", "key1", "dir1")

        assert dedup.has_response("maker1", "pubkey") is True
        assert dedup.has_response("maker1", "ioauth") is False
        assert dedup.has_response("maker2", "pubkey") is False

    def test_stats_tracking(self) -> None:
        """Test stats are tracked correctly."""
        dedup = ResponseDeduplicator()

        # 2 unique responses
        dedup.add_response("maker1", "pubkey", "key1", "dir1")
        dedup.add_response("maker2", "pubkey", "key2", "dir1")
        # 2 duplicates
        dedup.add_response("maker1", "pubkey", "key1", "dir2")
        dedup.add_response("maker2", "pubkey", "key2", "dir2")

        stats = dedup.stats
        assert stats.total_processed == 4
        assert stats.unique_messages == 2
        assert stats.duplicates_dropped == 2
        assert stats.duplicate_rate == 50.0

    def test_reset(self) -> None:
        """Test reset clears all responses and stats."""
        dedup = ResponseDeduplicator()

        dedup.add_response("maker1", "pubkey", "key1", "dir1")
        dedup.add_response("maker1", "pubkey", "key1", "dir2")  # duplicate

        dedup.reset()

        assert dedup.get_response_count("pubkey") == 0
        assert dedup.stats.total_processed == 0
        # Same response should be new after reset
        assert dedup.add_response("maker1", "pubkey", "key1", "dir1") is True

    def test_reset_command(self) -> None:
        """Test reset_command clears only specific command."""
        dedup = ResponseDeduplicator()

        dedup.add_response("maker1", "pubkey", "key1", "dir1")
        dedup.add_response("maker1", "ioauth", "auth1", "dir1")

        dedup.reset_command("pubkey")

        assert dedup.get_response_count("pubkey") == 0
        assert dedup.get_response_count("ioauth") == 1

    def test_duplicate_count_tracking(self) -> None:
        """Test that duplicate count is tracked per response."""
        dedup = ResponseDeduplicator()

        dedup.add_response("maker1", "pubkey", "key1", "dir1")
        dedup.add_response("maker1", "pubkey", "key1", "dir2")
        dedup.add_response("maker1", "pubkey", "key1", "dir3")

        responses = dedup.get_responses("pubkey")
        assert responses["maker1"].duplicate_count == 2
