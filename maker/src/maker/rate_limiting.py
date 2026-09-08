"""
Rate limiting for maker bot connections.

Provides two rate limiters:
- OrderbookRateLimiter: Per-peer rate limiting for orderbook requests with exponential backoff
- DirectConnectionRateLimiter: Per-connection rate limiting for direct hidden service connections
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from threading import Lock
from typing import Any

from loguru import logger

# Default hostid for onion network (matches reference implementation)
DEFAULT_HOSTID = "onion-network"

# Rate limiting defaults for orderbook requests
# These protect against spam attacks that can flood logs and exhaust resources
DEFAULT_ORDERBOOK_RATE_LIMIT = 1  # Max orderbook responses per peer per interval
DEFAULT_ORDERBOOK_RATE_INTERVAL = 10.0  # Interval in seconds (10s = 6 req/min)

# Violation thresholds for exponential backoff and banning
DEFAULT_VIOLATION_BAN_THRESHOLD = 100  # Ban peer after this many violations
DEFAULT_VIOLATION_WARNING_THRESHOLD = 10  # Start exponential backoff after this
DEFAULT_VIOLATION_SEVERE_THRESHOLD = 50  # Severe backoff threshold
DEFAULT_BAN_DURATION = 3600.0  # Ban duration in seconds (1 hour)
DEFAULT_MAX_TRACKED_IDENTITIES = 10_000

# Process-wide work budgets. These do not key state by remote identity.
# Keep discovery transports independent so direct floods cannot spend directory
# capacity. Directory admissions cover every offer and connected directory send.
DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_BURST = 200
DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_REFILL_PER_SECOND = 20.0
DEFAULT_DIRECT_ORDERBOOK_RESPONSE_BURST = 20
DEFAULT_DIRECT_ORDERBOOK_RESPONSE_REFILL_PER_SECOND = 2.0
DEFAULT_HP2_ADMISSION_BURST = 120
DEFAULT_HP2_ADMISSION_REFILL_PER_SECOND = 2.0
DEFAULT_HP2_RELAY_WORK_BURST = 2
DEFAULT_HP2_RELAY_WORK_REFILL_PER_SECOND = 1.0 / 30.0


class ProcessWideTokenBucket:
    """A thread-safe monotonic token bucket for aggregate maker work."""

    def __init__(
        self,
        burst: int,
        refill_per_second: float,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if burst <= 0:
            raise ValueError("burst must be positive")
        if refill_per_second < 0:
            raise ValueError("refill_per_second must be non-negative")
        self.burst = burst
        self.refill_per_second = refill_per_second
        self._clock = time.monotonic if clock is None else clock
        self._tokens = float(burst)
        self._last_refill = self._clock()
        self._lock = Lock()

    def try_consume(self) -> bool:
        """Consume one token immediately, returning False when depleted."""
        with self._lock:
            now = self._clock()
            elapsed = max(0.0, now - self._last_refill)
            self._tokens = min(self.burst, self._tokens + elapsed * self.refill_per_second)
            self._last_refill = now
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


class OrderbookRateLimiter:
    """
    Per-peer rate limiter for orderbook requests with exponential backoff and banning.

    Prevents DoS attacks by limiting how often each peer can request the orderbook.
    Uses a timestamp-based approach with escalating penalties:

    1. Normal operation: 1 response per interval (default 10s)
    2. After 10 violations: Exponential backoff starts (60s interval)
    3. After 50 violations: Severe backoff (300s = 5min interval)
    4. After 100 violations: Permanent ban until cleanup/restart

    This is crucial because:
    1. !orderbook responses include fidelity bond proofs which are expensive to compute
    2. Unlimited responses can flood log files
    3. A bad actor can exhaust maker resources by spamming requests

    Note: This limiter tracks by peer_id which can be either a nick (for directory messages)
    or a connection address (for direct connections). For direct connections, use the
    peer address to prevent nick rotation attacks.
    """

    def __init__(
        self,
        rate_limit: int = DEFAULT_ORDERBOOK_RATE_LIMIT,
        interval: float = DEFAULT_ORDERBOOK_RATE_INTERVAL,
        violation_ban_threshold: int = DEFAULT_VIOLATION_BAN_THRESHOLD,
        violation_warning_threshold: int = DEFAULT_VIOLATION_WARNING_THRESHOLD,
        violation_severe_threshold: int = DEFAULT_VIOLATION_SEVERE_THRESHOLD,
        ban_duration: float = DEFAULT_BAN_DURATION,
        max_tracked_identities: int = DEFAULT_MAX_TRACKED_IDENTITIES,
        directory_sources: frozenset[str] = frozenset(),
    ) -> None:
        """
        Initialize the rate limiter.

        Args:
            rate_limit: Maximum number of responses per interval (currently unused,
                       always 1 response per interval for simplicity)
            interval: Base time window in seconds
            violation_ban_threshold: Ban peer after this many violations
            violation_warning_threshold: Start exponential backoff after this
            violation_severe_threshold: Severe backoff threshold
            ban_duration: How long to ban peers (seconds)
            max_tracked_identities: Maximum peer states retained in memory.
            directory_sources: Known directory identities eligible for fanout suppression.
        """
        if max_tracked_identities <= 0:
            raise ValueError("max_tracked_identities must be positive")
        self.interval = interval
        self.violation_ban_threshold = violation_ban_threshold
        self.violation_warning_threshold = violation_warning_threshold
        self.violation_severe_threshold = violation_severe_threshold
        self.ban_duration = ban_duration
        self.directory_sources = directory_sources

        self._last_response: dict[str, float] = {}
        self._violation_counts: dict[str, int] = {}
        self._banned_peers: dict[str, float] = {}  # peer_nick -> ban_timestamp
        self._fanout_sources: dict[str, set[str]] = {}
        self._fanout_duplicates = 0
        self.max_tracked_identities = max_tracked_identities
        self._tracked_identities: OrderedDict[str, None] = OrderedDict()

    def _track_identity(self, peer_nick: str) -> None:
        """Retain bounded peer state, evicting the first-seen entry in O(1)."""
        if peer_nick in self._tracked_identities:
            return
        if len(self._tracked_identities) >= self.max_tracked_identities:
            oldest_peer, _ = self._tracked_identities.popitem(last=False)
            self._last_response.pop(oldest_peer, None)
            self._violation_counts.pop(oldest_peer, None)
            self._banned_peers.pop(oldest_peer, None)
            self._fanout_sources.pop(oldest_peer, None)
        self._tracked_identities[peer_nick] = None

    def _record_admitted_source(self, peer_nick: str, source: str | None) -> None:
        """Start a fanout window using only a configured directory source."""
        if source is not None and source in self.directory_sources:
            self._fanout_sources[peer_nick] = {source}
        else:
            self._fanout_sources.pop(peer_nick, None)

    def check(self, peer_nick: str, source: str | None = None) -> bool:
        """
        Check if we should respond to an orderbook request from this peer.

        A first copy from each other configured directory is suppressed without
        a violation during the base interval following an admitted request.

        Returns True if allowed, False if rate limited or banned.
        """
        now = time.monotonic()
        self._track_identity(peer_nick)

        # Check if peer is banned
        if peer_nick in self._banned_peers:
            ban_time = self._banned_peers[peer_nick]
            remaining = self.ban_duration - (now - ban_time)
            if remaining > 0:
                # Still banned, increment violation count
                new_violations = self._violation_counts.get(peer_nick, 0) + 1
                self._violation_counts[peer_nick] = new_violations
                logger.debug(
                    f"Rejecting request from banned peer {peer_nick} "
                    f"(remaining={remaining:.0f}s, violations={new_violations})"
                )
                return False
            else:
                # Ban expired, reset state completely
                logger.debug(
                    f"Ban expired for {peer_nick}, resetting rate limit state "
                    f"(was banned for {self.ban_duration}s with "
                    f"{self._violation_counts.get(peer_nick, 0)} violations)"
                )
                del self._banned_peers[peer_nick]
                self._violation_counts[peer_nick] = 0
                # Reset last response time so they can immediately get a response
                self._last_response[peer_nick] = 0.0
                self._fanout_sources.pop(peer_nick, None)

        violations = self._violation_counts.get(peer_nick, 0)

        # Check if peer should be banned based on violations
        if violations >= self.violation_ban_threshold:
            self._banned_peers[peer_nick] = now
            # Get backoff history for detailed logging
            backoff_level = self._get_backoff_level_name(violations)
            logger.warning(
                f"BANNED peer {peer_nick} for {self.ban_duration}s "
                f"after {violations} rate limit violations (final backoff: {backoff_level})"
            )
            return False

        # Calculate effective interval with exponential backoff
        effective_interval = self._get_effective_interval(violations)

        last = self._last_response.get(peer_nick, 0.0)
        elapsed = now - last

        if elapsed >= effective_interval:
            self._last_response[peer_nick] = now
            self._record_admitted_source(peer_nick, source)
            logger.trace(f"Allowed request from {peer_nick} (violations={violations})")
            return True

        seen_sources = self._fanout_sources.get(peer_nick)
        if (
            effective_interval == self.interval
            and elapsed < self.interval
            and source is not None
            and source in self.directory_sources
            and seen_sources
            and source not in seen_sources
        ):
            seen_sources.add(source)
            self._fanout_duplicates += 1
            return False

        # Rate limited - record violation
        new_violations = violations + 1
        self._violation_counts[peer_nick] = new_violations
        time_until_allowed = effective_interval - elapsed
        backoff_level = self._get_backoff_level_name(new_violations)
        logger.debug(
            f"Rate limited {peer_nick}: violations={new_violations}, "
            f"backoff={backoff_level}, wait={time_until_allowed:.1f}s"
        )
        return False

    def _get_effective_interval(self, violations: int) -> float:
        """
        Calculate effective rate limit interval based on violation count.

        Implements exponential backoff:
        - 0-10 violations: base interval (10s)
        - 11-50 violations: 6x base interval (60s)
        - 51-99 violations: 30x base interval (300s = 5min)
        - 100+ violations: banned (handled separately)

        Args:
            violations: Number of violations for this peer

        Returns:
            Effective interval in seconds
        """
        if violations < self.violation_warning_threshold:
            return self.interval
        elif violations < self.violation_severe_threshold:
            # Moderate backoff: 6x base interval
            return self.interval * 6
        else:
            # Severe backoff: 30x base interval
            return self.interval * 30

    def _get_backoff_level_name(self, violations: int) -> str:
        """Get human-readable backoff level name for logging."""
        if violations >= self.violation_ban_threshold:
            return "BANNED"
        elif violations >= self.violation_severe_threshold:
            return "SEVERE"
        elif violations >= self.violation_warning_threshold:
            return "MODERATE"
        else:
            return "NORMAL"

    def get_violation_count(self, peer_nick: str) -> int:
        """Get the number of rate limit violations for a peer."""
        return self._violation_counts.get(peer_nick, 0)

    def is_banned(self, peer_nick: str) -> bool:
        """Check if a peer is currently banned."""
        if peer_nick not in self._banned_peers:
            return False

        now = time.monotonic()
        ban_time = self._banned_peers[peer_nick]
        if now - ban_time < self.ban_duration:
            return True

        # Ban expired, clean up and reset violations
        del self._banned_peers[peer_nick]
        self._violation_counts[peer_nick] = 0
        self._last_response[peer_nick] = 0.0
        self._fanout_sources.pop(peer_nick, None)
        return False

    def cleanup_old_entries(self, max_age: float = 3600.0) -> None:
        """Remove entries older than max_age to prevent memory growth."""
        now = time.monotonic()

        # Clean up old responses
        stale_peers = [peer for peer, last in self._last_response.items() if now - last > max_age]
        for peer in stale_peers:
            del self._last_response[peer]
            self._fanout_sources.pop(peer, None)
            # Don't reset violation counts for stale peers - preserve ban history
            # Only reset if they're not banned
            if peer not in self._banned_peers:
                self._violation_counts.pop(peer, None)

        # Clean up expired bans
        expired_bans = [
            peer
            for peer, ban_time in self._banned_peers.items()
            if now - ban_time > self.ban_duration
        ]
        for peer in expired_bans:
            del self._banned_peers[peer]
            self._violation_counts[peer] = 0  # Reset violations after ban expires
            self._fanout_sources.pop(peer, None)

    def get_statistics(self) -> dict[str, Any]:
        """
        Get rate limiter statistics for monitoring.

        Returns:
            Dict with keys:
                - total_violations: Total violation count across all peers
                - tracked_peers: Number of peers being tracked
                - banned_peers: List of currently banned peer nicks
                - top_violators: List of (nick, violations) tuples, top 10
                - fanout_duplicates: Cumulative cross-directory duplicates suppressed
        """
        now = time.monotonic()

        # Get currently banned peers (check for expired bans)
        banned = [
            nick
            for nick, ban_time in self._banned_peers.items()
            if now - ban_time < self.ban_duration
        ]

        # Get top violators (sorted by violation count)
        top_violators = sorted(
            [(nick, count) for nick, count in self._violation_counts.items() if count > 0],
            key=lambda x: x[1],
            reverse=True,
        )[:10]

        return {
            "total_violations": sum(self._violation_counts.values()),
            "tracked_peers": len(self._last_response),
            "banned_peers": banned,
            "top_violators": top_violators,
            "fanout_duplicates": self._fanout_duplicates,
        }


class DirectConnectionRateLimiter:
    """
    Rate limiter for direct hidden service connections.

    Unlike the nick-based OrderbookRateLimiter, this tracks by connection address
    to prevent nick rotation attacks where attackers use a different nick per request.

    Since Tor creates a new circuit for each connection, the "peer address" from
    the local perspective will be 127.0.0.1:random_port. However, we can still
    track by this local port since each attacking circuit gets a unique port.

    This provides:
    1. Per-connection message rate limiting (general flood protection)
    2. Per-connection orderbook request limiting (specific attack mitigation)
    3. Connection banning after excessive violations
    """

    def __init__(
        self,
        # General message rate limiting
        message_rate_per_sec: float = 5.0,
        message_burst: int = 20,
        # Orderbook-specific limiting (stricter)
        orderbook_interval: float = 30.0,  # Longer interval for direct connections
        orderbook_ban_threshold: int = 10,  # Faster banning for direct attackers
        ban_duration: float = 3600.0,
        max_tracked_identities: int = DEFAULT_MAX_TRACKED_IDENTITIES,
    ):
        """
        Initialize the direct connection rate limiter.

        Args:
            message_rate_per_sec: Max sustained message rate per connection
            message_burst: Allowed burst of messages
            orderbook_interval: Minimum interval between orderbook requests
            orderbook_ban_threshold: Ban after this many orderbook violations
            ban_duration: How long to ban connections (seconds)
            max_tracked_identities: Maximum connection states retained in memory.
        """
        if max_tracked_identities <= 0:
            raise ValueError("max_tracked_identities must be positive")
        self.message_rate_per_sec = message_rate_per_sec
        self.message_burst = message_burst
        self.orderbook_interval = orderbook_interval
        self.orderbook_ban_threshold = orderbook_ban_threshold
        self.ban_duration = ban_duration

        # Track message tokens per connection (token bucket)
        self._message_tokens: dict[str, float] = {}
        self._message_last_update: dict[str, float] = {}

        # Track orderbook requests per connection
        self._orderbook_last: dict[str, float] = {}
        self._orderbook_violations: dict[str, int] = {}

        # Banned connections
        self._banned: dict[str, float] = {}
        self.max_tracked_identities = max_tracked_identities
        self._tracked_identities: OrderedDict[str, None] = OrderedDict()

    def _track_identity(self, conn_id: str) -> None:
        """Retain bounded connection state, evicting the first-seen entry in O(1)."""
        if conn_id in self._tracked_identities:
            return
        if len(self._tracked_identities) >= self.max_tracked_identities:
            oldest_conn, _ = self._tracked_identities.popitem(last=False)
            self._message_tokens.pop(oldest_conn, None)
            self._message_last_update.pop(oldest_conn, None)
            self._orderbook_last.pop(oldest_conn, None)
            self._orderbook_violations.pop(oldest_conn, None)
            self._banned.pop(oldest_conn, None)
        self._tracked_identities[conn_id] = None

    def check_message(self, conn_id: str) -> bool:
        """
        Check if a message from this connection should be allowed.

        Uses token bucket algorithm for general rate limiting.

        Args:
            conn_id: Connection identifier (e.g., "127.0.0.1:54321")

        Returns:
            True if allowed, False if rate limited
        """
        now = time.monotonic()
        self._track_identity(conn_id)

        # Check if banned
        if conn_id in self._banned:
            if now - self._banned[conn_id] < self.ban_duration:
                return False
            # Ban expired
            del self._banned[conn_id]
            self._orderbook_violations.pop(conn_id, None)

        # Token bucket: refill tokens based on time elapsed
        last_update = self._message_last_update.get(conn_id, now)
        current_tokens = self._message_tokens.get(conn_id, float(self.message_burst))

        # Add tokens based on elapsed time
        elapsed = now - last_update
        new_tokens = min(self.message_burst, current_tokens + elapsed * self.message_rate_per_sec)

        if new_tokens >= 1.0:
            # Allow message, consume one token
            self._message_tokens[conn_id] = new_tokens - 1.0
            self._message_last_update[conn_id] = now
            return True

        # Rate limited
        self._message_tokens[conn_id] = new_tokens
        self._message_last_update[conn_id] = now
        return False

    def check_orderbook(self, conn_id: str) -> bool:
        """
        Check if an orderbook request from this connection should be allowed.

        Uses stricter limiting than general messages since orderbook responses
        are expensive (fidelity bond proofs).

        Args:
            conn_id: Connection identifier

        Returns:
            True if allowed, False if rate limited or banned
        """
        now = time.monotonic()
        self._track_identity(conn_id)

        # Check if banned
        if conn_id in self._banned:
            if now - self._banned[conn_id] < self.ban_duration:
                violations = self._orderbook_violations.get(conn_id, 0) + 1
                self._orderbook_violations[conn_id] = violations
                return False
            # Ban expired
            del self._banned[conn_id]
            self._orderbook_violations.pop(conn_id, None)

        # Check time since last orderbook request
        last = self._orderbook_last.get(conn_id, 0.0)
        if now - last >= self.orderbook_interval:
            self._orderbook_last[conn_id] = now
            return True

        # Rate limited - record violation
        violations = self._orderbook_violations.get(conn_id, 0) + 1
        self._orderbook_violations[conn_id] = violations

        # Check if should be banned
        if violations >= self.orderbook_ban_threshold:
            self._banned[conn_id] = now
            logger.warning(
                f"BANNED direct connection {conn_id} for {self.ban_duration}s "
                f"after {violations} orderbook violations"
            )

        return False

    def is_banned(self, conn_id: str) -> bool:
        """Check if a connection is currently banned."""
        if conn_id not in self._banned:
            return False
        now = time.monotonic()
        if now - self._banned[conn_id] < self.ban_duration:
            return True
        # Ban expired
        del self._banned[conn_id]
        return False

    def get_violation_count(self, conn_id: str) -> int:
        """Get violation count for a connection."""
        return self._orderbook_violations.get(conn_id, 0)

    def cleanup_old_entries(self, max_age: float = 3600.0) -> None:
        """Remove stale entries to prevent memory growth."""
        now = time.monotonic()

        # Clean up old message tracking
        stale = [
            conn_id for conn_id, last in self._message_last_update.items() if now - last > max_age
        ]
        for conn_id in stale:
            self._message_tokens.pop(conn_id, None)
            self._message_last_update.pop(conn_id, None)

        # Clean up old orderbook tracking (but not violations for banned)
        stale = [
            conn_id
            for conn_id, last in self._orderbook_last.items()
            if now - last > max_age and conn_id not in self._banned
        ]
        for conn_id in stale:
            self._orderbook_last.pop(conn_id, None)
            self._orderbook_violations.pop(conn_id, None)

        # Clean up expired bans
        expired = [
            conn_id
            for conn_id, ban_time in self._banned.items()
            if now - ban_time > self.ban_duration
        ]
        for conn_id in expired:
            del self._banned[conn_id]
            self._orderbook_violations.pop(conn_id, None)

    def get_statistics(self) -> dict[str, Any]:
        """Get rate limiter statistics for monitoring."""
        now = time.monotonic()
        banned = [
            conn_id
            for conn_id, ban_time in self._banned.items()
            if now - ban_time < self.ban_duration
        ]
        total_violations = sum(self._orderbook_violations.values())
        top_violators = sorted(
            [
                (conn_id, count)
                for conn_id, count in self._orderbook_violations.items()
                if count > 0
            ],
            key=lambda x: x[1],
            reverse=True,
        )[:10]

        return {
            "total_violations": total_violations,
            "tracked_connections": len(self._message_last_update),
            "banned_connections": banned,
            "top_violators": top_violators,
        }
