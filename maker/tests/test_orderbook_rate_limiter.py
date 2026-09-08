"""
Tests for the OrderbookRateLimiter class.

The rate limiter protects makers from spam attacks that flood orderbook requests.
Now includes exponential backoff and ban functionality.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.models import NetworkType, Offer, OfferType

from maker.bot import DirectConnectionRateLimiter, MakerBot, OrderbookRateLimiter
from maker.config import MakerConfig
from maker.rate_limiting import (
    DEFAULT_DIRECT_ORDERBOOK_RESPONSE_BURST,
    DEFAULT_DIRECT_ORDERBOOK_RESPONSE_REFILL_PER_SECOND,
    DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_BURST,
    DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_REFILL_PER_SECOND,
    ProcessWideTokenBucket,
)


class TestOrderbookRateLimiter:
    """Tests for OrderbookRateLimiter class."""

    def test_first_request_allowed(self):
        """Test that the first request from a peer is always allowed."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=10.0)

        assert limiter.check("J5peer1") is True

    def test_second_request_within_interval_blocked(self):
        """Test that rapid subsequent requests are blocked."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=10.0)

        # First request - allowed
        assert limiter.check("J5peer1") is True

        # Immediate second request - blocked
        assert limiter.check("J5peer1") is False

    def test_request_after_interval_allowed(self):
        """Test that requests are allowed after the interval expires."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=0.1)  # 100ms interval

        # First request - allowed
        assert limiter.check("J5peer1") is True

        # Wait for interval to expire
        time.sleep(0.15)

        # Second request after interval - allowed
        assert limiter.check("J5peer1") is True

    def test_different_peers_independent(self):
        """Test that rate limiting is per-peer, not global."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=10.0)

        # First request from peer1 - allowed
        assert limiter.check("J5peer1") is True

        # First request from peer2 - also allowed (different peer)
        assert limiter.check("J5peer2") is True

        # Second request from peer1 - blocked
        assert limiter.check("J5peer1") is False

        # Second request from peer2 - also blocked
        assert limiter.check("J5peer2") is False

    def test_violation_count_tracked(self):
        """Test that violation counts are tracked per peer."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=10.0)

        # First request - allowed, no violations
        assert limiter.check("J5peer1") is True
        assert limiter.get_violation_count("J5peer1") == 0

        # Blocked requests increment violation count
        assert limiter.check("J5peer1") is False
        assert limiter.get_violation_count("J5peer1") == 1

        assert limiter.check("J5peer1") is False
        assert limiter.get_violation_count("J5peer1") == 2

        # Different peer has independent count
        assert limiter.get_violation_count("J5peer2") == 0

    def test_cleanup_old_entries(self):
        """Test that cleanup removes stale entries."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=10.0)

        # Add some entries
        limiter.check("J5peer1")
        limiter.check("J5peer1")  # Creates a violation
        limiter.check("J5peer2")

        # Entries should exist
        assert "J5peer1" in limiter._last_response
        assert "J5peer2" in limiter._last_response
        assert limiter.get_violation_count("J5peer1") == 1

        # Cleanup with max_age=0 removes everything
        limiter.cleanup_old_entries(max_age=0)

        assert "J5peer1" not in limiter._last_response
        assert "J5peer2" not in limiter._last_response
        assert limiter.get_violation_count("J5peer1") == 0

    def test_cleanup_preserves_recent_entries(self):
        """Test that cleanup preserves recent entries."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=10.0)

        # Add an entry
        limiter.check("J5peer1")

        # Cleanup with large max_age keeps the entry
        limiter.cleanup_old_entries(max_age=3600)

        assert "J5peer1" in limiter._last_response

    def test_default_values(self):
        """Test that default values are applied correctly."""
        limiter = OrderbookRateLimiter()

        # Should use defaults from bot.py (1 req per 10s)
        assert limiter.interval == 10.0

    def test_exponential_backoff_warning_threshold(self):
        """Test that exponential backoff activates at warning threshold."""
        limiter = OrderbookRateLimiter(
            rate_limit=1,
            interval=10.0,
            violation_warning_threshold=5,
            violation_severe_threshold=15,
        )

        # First request - allowed
        assert limiter.check("J5spammer") is True

        # Generate 6 violations (passes warning threshold of 5)
        for _ in range(6):
            assert limiter.check("J5spammer") is False

        violations = limiter.get_violation_count("J5spammer")
        assert violations == 6

        # Check that effective interval is now 6x base (moderate backoff)
        effective = limiter._get_effective_interval(violations)
        assert effective == 60.0  # 10s * 6

    def test_exponential_backoff_severe_threshold(self):
        """Test that severe backoff activates at severe threshold."""
        limiter = OrderbookRateLimiter(
            rate_limit=1,
            interval=10.0,
            violation_warning_threshold=5,
            violation_severe_threshold=15,
        )

        # First request - allowed
        assert limiter.check("J5spammer") is True

        # Generate 16 violations (passes severe threshold of 15)
        for _ in range(16):
            assert limiter.check("J5spammer") is False

        violations = limiter.get_violation_count("J5spammer")
        assert violations == 16

        # Check that effective interval is now 30x base (severe backoff)
        effective = limiter._get_effective_interval(violations)
        assert effective == 300.0  # 10s * 30

    def test_ban_after_threshold(self):
        """Test that peers are banned after exceeding violation threshold."""
        limiter = OrderbookRateLimiter(
            rate_limit=1, interval=10.0, violation_ban_threshold=10, ban_duration=3600.0
        )

        # First request - allowed
        assert limiter.check("J5spammer") is True
        assert not limiter.is_banned("J5spammer")

        # Generate violations up to ban threshold
        for i in range(10):
            assert limiter.check("J5spammer") is False
            assert not limiter.is_banned("J5spammer")  # Not yet banned

        # Next check should ban the peer
        assert limiter.check("J5spammer") is False
        assert limiter.is_banned("J5spammer")

        # Subsequent requests should still be blocked
        assert limiter.check("J5spammer") is False
        assert limiter.is_banned("J5spammer")

    def test_ban_expiration(self):
        """Test that bans expire after the configured duration."""
        limiter = OrderbookRateLimiter(
            rate_limit=1,
            interval=0.1,
            violation_ban_threshold=5,
            ban_duration=0.2,  # 200ms ban
        )

        # First request - allowed
        assert limiter.check("J5spammer") is True

        # Generate violations to trigger ban
        for _ in range(6):
            limiter.check("J5spammer")

        # Should be banned
        assert limiter.is_banned("J5spammer")

        # Wait for ban to expire
        time.sleep(0.25)

        # Ban should be expired
        assert not limiter.is_banned("J5spammer")

        # Should be able to make requests again (violations reset)
        assert limiter.check("J5spammer") is True
        assert limiter.get_violation_count("J5spammer") == 0

    def test_banned_peer_cannot_respond_until_expiry(self):
        """Test that banned peers cannot get responses until ban expires."""
        limiter = OrderbookRateLimiter(
            rate_limit=1,
            interval=0.05,
            violation_ban_threshold=3,
            ban_duration=0.5,  # Longer ban
        )

        # Get banned
        limiter.check("J5spammer")
        for _ in range(4):
            limiter.check("J5spammer")

        assert limiter.is_banned("J5spammer")

        # Try multiple times during ban - all should fail
        for i in range(5):
            time.sleep(0.06)  # Wait longer than normal interval
            result = limiter.check("J5spammer")
            assert result is False, f"Iteration {i}: Expected False but got {result}"
            assert limiter.is_banned("J5spammer")

        # Wait for ban expiry
        time.sleep(0.5)

        # Should work again after ban expires
        assert limiter.check("J5spammer") is True
        assert not limiter.is_banned("J5spammer")

    def test_cleanup_expired_bans(self):
        """Test that cleanup removes expired bans and resets violations."""
        limiter = OrderbookRateLimiter(
            rate_limit=1, interval=0.1, violation_ban_threshold=5, ban_duration=0.1
        )

        # Get peer banned
        limiter.check("J5spammer")
        for _ in range(6):
            limiter.check("J5spammer")

        assert limiter.is_banned("J5spammer")
        assert "J5spammer" in limiter._banned_peers

        # Wait for ban to expire
        time.sleep(0.15)

        # Cleanup should remove expired ban
        limiter.cleanup_old_entries(max_age=1.0)

        assert not limiter.is_banned("J5spammer")
        assert "J5spammer" not in limiter._banned_peers
        assert limiter.get_violation_count("J5spammer") == 0

    def test_different_peers_banned_independently(self):
        """Test that bans are per-peer, not global."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=10.0, violation_ban_threshold=5)

        # Ban peer1
        limiter.check("J5peer1")
        for _ in range(6):
            limiter.check("J5peer1")
        assert limiter.is_banned("J5peer1")

        # peer2 should still work
        assert limiter.check("J5peer2") is True
        assert not limiter.is_banned("J5peer2")

    def test_get_statistics(self):
        """Test that statistics are correctly gathered."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=10.0, violation_ban_threshold=10)

        # Generate some activity
        limiter.check("J5Alice")
        limiter.check("J5Alice")  # 1 violation

        limiter.check("J5Bob")
        for _ in range(20):
            limiter.check("J5Bob")  # Gets to 19 violations (1st check succeeds), then banned

        limiter.check("J5Charlie")
        for _ in range(5):
            limiter.check("J5Charlie")  # 5 violations

        stats = limiter.get_statistics()

        # Check basic stats (1 + 19 + 5 = 25 total)
        assert stats["total_violations"] == 25
        assert stats["tracked_peers"] == 3

        # Check banned peers
        assert "J5Bob" in stats["banned_peers"]
        assert "J5Alice" not in stats["banned_peers"]
        assert "J5Charlie" not in stats["banned_peers"]

        # Check top violators are sorted correctly
        assert len(stats["top_violators"]) == 3
        assert stats["top_violators"][0] == ("J5Bob", 19)
        assert stats["top_violators"][1] == ("J5Charlie", 5)
        assert stats["top_violators"][2] == ("J5Alice", 1)

    def test_get_statistics_empty(self):
        """Test statistics with no activity."""
        limiter = OrderbookRateLimiter()
        stats = limiter.get_statistics()

        assert stats["total_violations"] == 0
        assert stats["tracked_peers"] == 0
        assert stats["banned_peers"] == []
        assert stats["top_violators"] == []

    def test_identity_state_is_bounded_with_oldest_eviction(self):
        limiter = OrderbookRateLimiter(max_tracked_identities=2)

        assert limiter.check("J5first") is True
        assert limiter.check("J5second") is True
        assert limiter.check("J5third") is True

        assert list(limiter._tracked_identities) == ["J5second", "J5third"]
        assert "J5first" not in limiter._last_response
        assert len(limiter._tracked_identities) == 2


class TestProcessWideTokenBucket:
    """Tests for aggregate maker work budgets."""

    def test_burst_is_refilled_using_monotonic_time(self):
        now = [0.0]
        limiter = ProcessWideTokenBucket(2, 1.0, clock=lambda: now[0])

        assert limiter.try_consume() is True
        assert limiter.try_consume() is True
        assert limiter.try_consume() is False

        now[0] = 1.0
        assert limiter.try_consume() is True

    @pytest.mark.parametrize(
        ("burst", "refill_per_second", "one_token_delay"),
        [
            (
                DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_BURST,
                DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_REFILL_PER_SECOND,
                0.05,
            ),
            (
                DEFAULT_DIRECT_ORDERBOOK_RESPONSE_BURST,
                DEFAULT_DIRECT_ORDERBOOK_RESPONSE_REFILL_PER_SECOND,
                0.5,
            ),
        ],
        ids=["directory", "direct"],
    )
    def test_default_response_budget_boundary_and_refill(
        self, burst: int, refill_per_second: float, one_token_delay: float
    ) -> None:
        now = [0.0]
        limiter = ProcessWideTokenBucket(burst, refill_per_second, clock=lambda: now[0])

        assert all(limiter.try_consume() for _ in range(burst))
        assert limiter.try_consume() is False
        now[0] = one_token_delay - 0.001
        assert limiter.try_consume() is False
        now[0] = one_token_delay
        assert limiter.try_consume() is True


class TestMakerBotRateLimiting:
    """Tests for rate limiting integration in MakerBot."""

    @pytest.fixture
    def mock_wallet(self):
        """Create a mock wallet service."""
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        return wallet

    @pytest.fixture
    def mock_backend(self):
        """Create a mock blockchain backend."""
        return MagicMock()

    @pytest.fixture
    def config(self):
        """Create a test maker config with custom rate limit settings."""
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            orderbook_rate_limit=1,
            orderbook_rate_interval=5.0,
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        """Create a MakerBot instance for testing."""
        return MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )

    def test_bot_has_rate_limiter(self, maker_bot):
        """Test that MakerBot initializes with a rate limiter."""
        assert hasattr(maker_bot, "_orderbook_rate_limiter")
        assert isinstance(maker_bot._orderbook_rate_limiter, OrderbookRateLimiter)

    def test_bot_rate_limiter_uses_config(self, maker_bot):
        """Test that rate limiter uses config values."""
        assert maker_bot._orderbook_rate_limiter.interval == 5.0

    def test_bot_has_independent_orderbook_response_limiters(self, maker_bot):
        assert maker_bot._directory_orderbook_response_limiter is not (
            maker_bot._direct_orderbook_response_limiter
        )
        assert maker_bot._directory_orderbook_response_limiter.burst == 200
        assert maker_bot._directory_orderbook_response_limiter.refill_per_second == 20.0
        assert maker_bot._direct_orderbook_response_limiter.burst == 20
        assert maker_bot._direct_orderbook_response_limiter.refill_per_second == 2.0

    @pytest.mark.asyncio
    async def test_directory_and_direct_responses_use_independent_response_budgets(self, maker_bot):
        maker_bot.current_offers = [
            Offer(
                counterparty=maker_bot.nick,
                ordertype=OfferType.SW0_RELATIVE,
                oid=0,
                minsize=10_000,
                maxsize=100_000,
                txfee=100,
                cjfee="0.0001",
            )
        ]
        maker_bot.fidelity_bond = MagicMock()
        maker_bot._directory_orderbook_response_limiter = ProcessWideTokenBucket(1, 0.0)
        maker_bot._direct_orderbook_response_limiter = ProcessWideTokenBucket(1, 0.0)
        directory_client = MagicMock(send_private_message=AsyncMock())
        direct_connection = MagicMock(send=AsyncMock())
        maker_bot.directory_clients["directory"] = directory_client

        with patch(
            "maker.protocol_handlers.create_fidelity_bond_proof", return_value="proof"
        ) as create_proof:
            await maker_bot._send_offers_to_taker("J5DirectoryTaker")
            await maker_bot._send_offers_via_direct_connection("J5DirectTaker", direct_connection)

        assert create_proof.call_count == 2
        directory_client.send_private_message.assert_awaited_once()
        direct_connection.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_directory_response_suppression_has_no_sent_offers_log(self, maker_bot):
        maker_bot._directory_orderbook_response_limiter = ProcessWideTokenBucket(1, 0.0)
        assert maker_bot._directory_orderbook_response_limiter.try_consume() is True

        with (
            patch("maker.protocol_handlers.logger.info") as info_log,
            patch("maker.bot.logger.warning") as warning_log,
        ):
            await maker_bot._handle_pubmsg("J5First!PUBLIC!orderbook")
            await maker_bot._handle_pubmsg("J5Second!PUBLIC!orderbook")

        info_log.assert_not_called()
        warning_log.assert_called_once_with(
            "Suppressing !orderbook response "
            "(directory response budget exhausted; refills automatically)"
        )

    def test_config_default_rate_limit_values(self, mock_wallet, mock_backend):
        """Test default rate limit values in MakerConfig."""
        default_config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )

        assert default_config.orderbook_rate_limit == 1
        assert default_config.orderbook_rate_interval == 10.0
        assert default_config.orderbook_violation_ban_threshold == 100
        assert default_config.orderbook_violation_warning_threshold == 10
        assert default_config.orderbook_violation_severe_threshold == 50
        assert default_config.orderbook_ban_duration == 3600.0

    def test_config_custom_rate_limit_values(self):
        """Test custom rate limit values in MakerConfig."""
        config = MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
            orderbook_rate_limit=5,
            orderbook_rate_interval=30.0,
            orderbook_violation_ban_threshold=50,
            orderbook_violation_warning_threshold=5,
            orderbook_violation_severe_threshold=25,
            orderbook_ban_duration=7200.0,
        )

        assert config.orderbook_rate_limit == 5
        assert config.orderbook_rate_interval == 30.0
        assert config.orderbook_violation_ban_threshold == 50
        assert config.orderbook_violation_warning_threshold == 5
        assert config.orderbook_violation_severe_threshold == 25
        assert config.orderbook_ban_duration == 7200.0

    def test_config_rate_limit_validation(self):
        """Test that invalid rate limit values are rejected."""
        # rate_limit must be >= 1
        with pytest.raises(ValueError):
            MakerConfig(
                mnemonic="test " * 12,
                directory_servers=["localhost:5222"],
                network=NetworkType.REGTEST,
                orderbook_rate_limit=0,
            )

        # interval must be >= 1.0
        with pytest.raises(ValueError):
            MakerConfig(
                mnemonic="test " * 12,
                directory_servers=["localhost:5222"],
                network=NetworkType.REGTEST,
                orderbook_rate_interval=0.5,
            )

        # ban_threshold must be >= 1
        with pytest.raises(ValueError):
            MakerConfig(
                mnemonic="test " * 12,
                directory_servers=["localhost:5222"],
                network=NetworkType.REGTEST,
                orderbook_violation_ban_threshold=0,
            )

        # ban_duration must be >= 60.0
        with pytest.raises(ValueError):
            MakerConfig(
                mnemonic="test " * 12,
                directory_servers=["localhost:5222"],
                network=NetworkType.REGTEST,
                orderbook_ban_duration=30.0,
            )


class TestRateLimiterLogThrottling:
    """Tests for log throttling in rate limiter violations."""

    def test_violation_count_for_log_throttling(self):
        """Test that violation counts can be used for log throttling."""
        limiter = OrderbookRateLimiter(rate_limit=1, interval=10.0)

        # Initial request
        limiter.check("J5spammer")

        # Simulate 25 spam requests
        for _ in range(25):
            limiter.check("J5spammer")

        # Should have 25 violations
        count = limiter.get_violation_count("J5spammer")
        assert count == 25

        # Logic: only log every 10th violation (violations 10, 20, 30...)
        # This prevents log flooding from the rate limiter itself
        should_log_1 = count % 10 == 0  # 25 % 10 != 0, so False
        should_log_2 = (count + 5) % 10 == 0  # 30 % 10 == 0, so True

        assert should_log_1 is False
        assert should_log_2 is True


class TestDirectConnectionRateLimiter:
    """Tests for DirectConnectionRateLimiter class.

    This rate limiter tracks by connection address (not nick) to prevent
    nick rotation attacks on direct hidden service connections.
    """

    def test_first_message_allowed(self):
        """Test that the first message from a connection is allowed."""
        limiter = DirectConnectionRateLimiter()
        assert limiter.check_message("127.0.0.1:54321") is True

    def test_message_rate_limiting(self):
        """Test that messages are rate limited after burst is exhausted."""
        limiter = DirectConnectionRateLimiter(
            message_rate_per_sec=2.0,
            message_burst=5,
        )
        conn_id = "127.0.0.1:54321"

        # First 5 messages should be allowed (burst)
        for i in range(5):
            assert limiter.check_message(conn_id) is True, f"Message {i + 1} should be allowed"

        # 6th message should be blocked (burst exhausted)
        assert limiter.check_message(conn_id) is False

    def test_message_tokens_refill(self):
        """Test that message tokens refill over time."""
        limiter = DirectConnectionRateLimiter(
            message_rate_per_sec=10.0,  # 10 per second = 1 per 100ms
            message_burst=2,
        )
        conn_id = "127.0.0.1:54321"

        # Exhaust burst
        assert limiter.check_message(conn_id) is True
        assert limiter.check_message(conn_id) is True
        assert limiter.check_message(conn_id) is False

        # Wait for tokens to refill
        time.sleep(0.15)  # Should get at least 1 token

        # Should be allowed again
        assert limiter.check_message(conn_id) is True

    def test_first_orderbook_request_allowed(self):
        """Test that the first orderbook request is allowed."""
        limiter = DirectConnectionRateLimiter()
        assert limiter.check_orderbook("127.0.0.1:54321") is True

    def test_orderbook_rate_limiting(self):
        """Test that orderbook requests are rate limited."""
        limiter = DirectConnectionRateLimiter(orderbook_interval=10.0)
        conn_id = "127.0.0.1:54321"

        # First request - allowed
        assert limiter.check_orderbook(conn_id) is True

        # Immediate second request - blocked
        assert limiter.check_orderbook(conn_id) is False

    def test_orderbook_request_after_interval(self):
        """Test that orderbook requests are allowed after interval."""
        limiter = DirectConnectionRateLimiter(orderbook_interval=0.1)
        conn_id = "127.0.0.1:54321"

        # First request
        assert limiter.check_orderbook(conn_id) is True

        # Wait for interval
        time.sleep(0.15)

        # Should be allowed again
        assert limiter.check_orderbook(conn_id) is True

    def test_orderbook_ban_after_violations(self):
        """Test that connections are banned after orderbook violations."""
        limiter = DirectConnectionRateLimiter(
            orderbook_interval=10.0,
            orderbook_ban_threshold=5,
            ban_duration=3600.0,
        )
        conn_id = "127.0.0.1:54321"

        # First request - allowed
        assert limiter.check_orderbook(conn_id) is True
        assert not limiter.is_banned(conn_id)

        # Generate violations up to threshold (5 violations = ban)
        # Each failed check increments violation count
        for i in range(4):
            assert limiter.check_orderbook(conn_id) is False
            assert not limiter.is_banned(conn_id), f"Iteration {i}: should not be banned yet"

        # 5th violation triggers ban
        assert limiter.check_orderbook(conn_id) is False
        assert limiter.is_banned(conn_id)

    def test_ban_blocks_messages(self):
        """Test that banned connections can't send messages."""
        limiter = DirectConnectionRateLimiter(
            orderbook_interval=10.0,
            orderbook_ban_threshold=3,
        )
        conn_id = "127.0.0.1:54321"

        # Get banned via orderbook spam
        limiter.check_orderbook(conn_id)
        for _ in range(4):
            limiter.check_orderbook(conn_id)

        assert limiter.is_banned(conn_id)

        # Messages should also be blocked
        assert limiter.check_message(conn_id) is False

    def test_ban_expiration(self):
        """Test that bans expire after duration."""
        limiter = DirectConnectionRateLimiter(
            orderbook_interval=10.0,
            orderbook_ban_threshold=3,
            ban_duration=0.1,  # 100ms ban
        )
        conn_id = "127.0.0.1:54321"

        # Get banned
        limiter.check_orderbook(conn_id)
        for _ in range(4):
            limiter.check_orderbook(conn_id)

        assert limiter.is_banned(conn_id)

        # Wait for ban to expire
        time.sleep(0.15)

        # Should no longer be banned
        assert not limiter.is_banned(conn_id)

        # Should be able to send messages again
        assert limiter.check_message(conn_id) is True

    def test_independent_connection_tracking(self):
        """Test that different connections are tracked independently."""
        limiter = DirectConnectionRateLimiter(
            orderbook_interval=10.0,
            orderbook_ban_threshold=3,
        )

        # Ban connection 1
        limiter.check_orderbook("127.0.0.1:11111")
        for _ in range(4):
            limiter.check_orderbook("127.0.0.1:11111")

        assert limiter.is_banned("127.0.0.1:11111")

        # Connection 2 should work fine
        assert limiter.check_orderbook("127.0.0.1:22222") is True
        assert not limiter.is_banned("127.0.0.1:22222")

    def test_statistics(self):
        """Test that statistics are correctly gathered."""
        limiter = DirectConnectionRateLimiter(
            orderbook_interval=10.0,
            orderbook_ban_threshold=10,
        )

        # Generate activity
        limiter.check_orderbook("127.0.0.1:11111")
        limiter.check_orderbook("127.0.0.1:11111")  # 1 violation

        limiter.check_orderbook("127.0.0.1:22222")
        for _ in range(15):
            limiter.check_orderbook("127.0.0.1:22222")  # Gets banned

        stats = limiter.get_statistics()

        assert stats["total_violations"] > 0
        assert "127.0.0.1:22222" in stats["banned_connections"]
        assert "127.0.0.1:11111" not in stats["banned_connections"]
        assert len(stats["top_violators"]) >= 2

    def test_cleanup_removes_stale_entries(self):
        """Test that cleanup removes stale tracking entries."""
        limiter = DirectConnectionRateLimiter()

        # Add some connections
        limiter.check_message("127.0.0.1:11111")
        limiter.check_message("127.0.0.1:22222")

        # Entries should exist
        assert "127.0.0.1:11111" in limiter._message_last_update
        assert "127.0.0.1:22222" in limiter._message_last_update

        # Cleanup with max_age=0 removes everything
        limiter.cleanup_old_entries(max_age=0)

        assert "127.0.0.1:11111" not in limiter._message_last_update
        assert "127.0.0.1:22222" not in limiter._message_last_update

    def test_identity_state_is_bounded_with_oldest_eviction(self):
        limiter = DirectConnectionRateLimiter(max_tracked_identities=2)

        assert limiter.check_message("127.0.0.1:11111") is True
        assert limiter.check_orderbook("127.0.0.1:22222") is True
        assert limiter.check_message("127.0.0.1:33333") is True

        assert list(limiter._tracked_identities) == ["127.0.0.1:22222", "127.0.0.1:33333"]
        assert "127.0.0.1:11111" not in limiter._message_tokens
        assert len(limiter._tracked_identities) == 2


class TestMakerBotDirectConnectionRateLimiting:
    """Tests for direct connection rate limiting integration in MakerBot."""

    @pytest.fixture
    def mock_wallet(self):
        """Create a mock wallet service."""
        wallet = MagicMock()
        wallet.mixdepth_count = 5
        wallet.utxo_cache = {}
        return wallet

    @pytest.fixture
    def mock_backend(self):
        """Create a mock blockchain backend."""
        return MagicMock()

    @pytest.fixture
    def config(self):
        """Create a test maker config."""
        return MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        )

    @pytest.fixture
    def maker_bot(self, mock_wallet, mock_backend, config):
        """Create a MakerBot instance for testing."""
        return MakerBot(
            wallet=mock_wallet,
            backend=mock_backend,
            config=config,
        )

    def test_bot_has_direct_connection_rate_limiter(self, maker_bot):
        """Test that MakerBot initializes with a direct connection rate limiter."""
        assert hasattr(maker_bot, "_direct_connection_rate_limiter")
        assert isinstance(maker_bot._direct_connection_rate_limiter, DirectConnectionRateLimiter)

    def test_direct_limiter_is_stricter(self, maker_bot):
        """Test that direct connection limiter has stricter settings."""
        direct_limiter = maker_bot._direct_connection_rate_limiter

        # Direct connections should have stricter settings than directory
        assert direct_limiter.orderbook_interval == 30.0  # Longer than 10s
        assert direct_limiter.orderbook_ban_threshold == 10  # Faster than 100


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
