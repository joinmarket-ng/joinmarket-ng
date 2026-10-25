"""
Message deduplication for multi-directory connections.

When JoinMarket components connect to N directory servers, they receive each message
N times. This module provides deduplication to avoid processing duplicates.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class DeduplicationStats:
    """Statistics about deduplication activity."""

    total_processed: int = 0
    duplicates_dropped: int = 0
    unique_messages: int = 0

    @property
    def duplicate_rate(self) -> float:
        """Return the percentage of messages that were duplicates."""
        if self.total_processed == 0:
            return 0.0
        return (self.duplicates_dropped / self.total_processed) * 100


@dataclass
class SeenMessage:
    """Record of a seen message."""

    timestamp: float
    source: str
    count: int = 1


class MessageDeduplicator:
    """
    Deduplicates messages received from multiple sources.

    When makers/takers are connected to N directory servers, they receive each
    message N times. This class tracks recently-seen messages to:
    1. Avoid processing duplicates (especially expensive operations like !auth, !tx)
    2. Prevent rate limiter from counting duplicates as violations
    3. Track which source each message came from for better logging

    Design:
    - Simple time-based deduplication window (default 30s)
    - Message fingerprint: from_nick + command + first_arg (e.g., "alice:fill:order123")
    - Tracks first source for each message to enable better logging
    - Auto-cleanup of old entries to prevent memory leaks

    Example:
        >>> dedup = MessageDeduplicator(window_seconds=30.0)
        >>> fp = MessageDeduplicator.make_fingerprint("alice", "fill", "order123")
        >>> is_dup, source, count = dedup.is_duplicate(fp, "dir1.onion")
        >>> print(f"Duplicate: {is_dup}, first source: {source}, count: {count}")
        Duplicate: False, first source: dir1.onion, count: 1
        >>> is_dup, source, count = dedup.is_duplicate(fp, "dir2.onion")
        >>> print(f"Duplicate: {is_dup}, first source: {source}, count: {count}")
        Duplicate: True, first source: dir1.onion, count: 2
    """

    def __init__(self, window_seconds: float = 30.0):
        """
        Initialize deduplicator.

        Args:
            window_seconds: How long to remember messages (default 30s).
                           Should be longer than expected network latency variance.
        """
        self.window_seconds = window_seconds
        # Insertion order is timestamp order, so expiry only pops from the front.
        self._seen: OrderedDict[str, SeenMessage] = OrderedDict()
        self._stats = DeduplicationStats()

    def is_duplicate(self, fingerprint: str, source: str) -> tuple[bool, str, int]:
        """
        Check if this message is a duplicate.

        Args:
            fingerprint: Unique identifier for the message (use make_fingerprint)
            source: Identifier for where message came from (e.g., directory URL)

        Returns:
            Tuple of (is_duplicate, first_source, total_count):
            - is_duplicate: True if seen before within window
            - first_source: Which source saw it first
            - total_count: How many times we've seen this message
        """
        now = time.monotonic()
        self._cleanup(now)
        self._stats.total_processed += 1

        if fingerprint in self._seen:
            entry = self._seen[fingerprint]
            entry.count += 1
            self._stats.duplicates_dropped += 1
            return (True, entry.source, entry.count)

        # First time seeing this message
        self._seen[fingerprint] = SeenMessage(timestamp=now, source=source, count=1)
        self._stats.unique_messages += 1
        return (False, source, 1)

    def _cleanup(self, now: float) -> None:
        """Remove entries older than the window."""
        cutoff = now - self.window_seconds
        while self._seen:
            oldest = next(iter(self._seen.values()))
            if oldest.timestamp >= cutoff:
                break
            self._seen.popitem(last=False)

    @staticmethod
    def make_fingerprint(from_nick: str, command: str, first_arg: str = "") -> str:
        """
        Create a message fingerprint for deduplication.

        The fingerprint uniquely identifies a message based on:
        - Who sent it (from_nick)
        - What command it is (fill, auth, tx, pubkey, ioauth, sig, etc.)
        - The primary identifier (order ID, transaction hash, etc.)

        Args:
            from_nick: Who sent the message
            command: Command name (fill, auth, tx, etc.)
            first_arg: First argument (e.g., order ID for fill)

        Returns:
            String fingerprint like "alice:fill:order123"
        """
        return f"{from_nick}:{command}:{first_arg}"

    @property
    def stats(self) -> DeduplicationStats:
        """Get deduplication statistics."""
        return self._stats

    def reset_stats(self) -> None:
        """Reset statistics counters."""
        self._stats = DeduplicationStats()

    def clear(self) -> None:
        """Clear all seen messages and reset stats."""
        self._seen.clear()
        self.reset_stats()

    def __len__(self) -> int:
        """Return number of messages currently being tracked."""
        return len(self._seen)


class ResponseDeduplicator:
    """
    Specialized deduplicator for taker response collection.

    When a taker sends requests to makers via multiple directory servers,
    it may receive duplicate responses. This class helps collect unique
    responses while tracking duplicates.

    Unlike MessageDeduplicator which uses time-based expiry, this class
    is designed for short-lived request-response cycles and requires
    explicit reset between rounds.

    Example:
        >>> dedup = ResponseDeduplicator()
        >>> # Collect pubkey responses from makers
        >>> dedup.add_response("maker1", "pubkey", pubkey_data, "dir1")
        True  # First response
        >>> dedup.add_response("maker1", "pubkey", pubkey_data, "dir2")
        False  # Duplicate
        >>> responses = dedup.get_responses("pubkey")
        >>> len(responses)
        1
    """

    @dataclass
    class ResponseEntry:
        """A collected response."""

        nick: str
        data: object
        source: str
        timestamp: float = field(default_factory=time.monotonic)
        duplicate_count: int = 0

    def __init__(self) -> None:
        """Initialize response deduplicator."""
        # command -> nick -> ResponseEntry
        self._responses: dict[str, dict[str, ResponseDeduplicator.ResponseEntry]] = {}
        self._stats = DeduplicationStats()

    def add_response(self, nick: str, command: str, data: object, source: str) -> bool:
        """
        Add a response, returning True if it's new (not a duplicate).

        Args:
            nick: The maker nick who sent the response
            command: Response type (pubkey, ioauth, sig, etc.)
            data: The response data
            source: Which directory server it came from

        Returns:
            True if this is a new response, False if duplicate
        """
        self._stats.total_processed += 1

        if command not in self._responses:
            self._responses[command] = {}

        if nick in self._responses[command]:
            # Duplicate response from same maker
            self._responses[command][nick].duplicate_count += 1
            self._stats.duplicates_dropped += 1
            return False

        # New response
        self._responses[command][nick] = self.ResponseEntry(nick=nick, data=data, source=source)
        self._stats.unique_messages += 1
        return True

    def get_responses(self, command: str) -> dict[str, ResponseEntry]:
        """
        Get all unique responses for a command type.

        Args:
            command: Response type to get

        Returns:
            Dict mapping nick -> ResponseEntry
        """
        return self._responses.get(command, {})

    def get_response_count(self, command: str) -> int:
        """Get number of unique responses for a command."""
        return len(self._responses.get(command, {}))

    def has_response(self, nick: str, command: str) -> bool:
        """Check if we have a response from a specific maker."""
        return nick in self._responses.get(command, {})

    @property
    def stats(self) -> DeduplicationStats:
        """Get deduplication statistics."""
        return self._stats

    def reset(self) -> None:
        """Clear all responses and reset stats for next round."""
        self._responses.clear()
        self._stats = DeduplicationStats()

    def reset_command(self, command: str) -> None:
        """Clear responses for a specific command type."""
        if command in self._responses:
            del self._responses[command]
