"""
Tests for MakerHealthChecker - direct maker reachability verification.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.protocol import FEATURE_NEUTRINO_COMPAT, MessageType

from orderbook_watcher.health_checker import MakerHealthChecker


@pytest.fixture
def health_checker() -> MakerHealthChecker:
    """Create a MakerHealthChecker instance for testing."""
    return MakerHealthChecker(
        network="regtest",
        socks_host="127.0.0.1",
        socks_port=9050,
        timeout=5.0,
        check_interval=60.0,
        max_concurrent_checks=5,
    )


@pytest.fixture
def mock_connection() -> MagicMock:
    """Create a mock TCP connection."""
    conn = MagicMock()
    conn.send = AsyncMock()
    conn.receive = AsyncMock()
    conn.close = AsyncMock()
    return conn


def create_peer_handshake_response(
    features: dict[str, bool] | None = None,
) -> bytes:
    """Create a mock peer (maker) handshake response in client format.

    Non-directory peers respond with HANDSHAKE (793) using the client format,
    which does NOT contain "accepted" -- receiving a valid response implicitly
    means acceptance.
    """
    if features is None:
        features = {}

    response_data = {
        "app-name": "joinmarket",
        "directory": False,
        "location-string": "testmaker1234567890abcdef1234567890abcdef12345678.onion:5222",
        "proto-ver": 5,
        "features": features,
        "nick": "J5maker",
        "network": "regtest",
    }

    response = {
        "type": MessageType.HANDSHAKE.value,
        "line": json.dumps(response_data),
    }

    return json.dumps(response).encode("utf-8")


def create_dn_handshake_response(
    accepted: bool = True,
    features: dict[str, bool] | None = None,
) -> bytes:
    """Create a mock directory node handshake response.

    Directory nodes respond with DN_HANDSHAKE (795) which includes an explicit
    "accepted" field and server-format fields like proto-ver-min/max and motd.
    """
    if features is None:
        features = {}

    response_data = {
        "app-name": "joinmarket",
        "directory": True,
        "proto-ver-min": 5,
        "proto-ver-max": 5,
        "features": features,
        "accepted": accepted,
        "nick": "J5directory",
        "network": "regtest",
        "motd": "Welcome",
    }

    response = {
        "type": MessageType.DN_HANDSHAKE.value,
        "line": json.dumps(response_data),
    }

    return json.dumps(response).encode("utf-8")


class TestMakerHealthChecker:
    """Test MakerHealthChecker functionality."""

    @pytest.mark.asyncio
    async def test_batch_deadline_closes_stalled_handshake(self) -> None:
        checker = MakerHealthChecker(
            network="regtest", timeout=10, max_concurrent_checks=1, max_batch_duration=0.2
        )

        async def stall() -> bytes:
            await asyncio.Event().wait()
            return b""

        connection = MagicMock()
        connection.send = AsyncMock()
        connection.receive = AsyncMock(side_effect=stall)
        connection.close = AsyncMock()
        locations = ["first.onion:5222", "later.onion:5222"]

        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=connection
        ) as dial:
            result = await checker.check_makers_batch(
                [("first", locations[0]), ("later", locations[1])]
            )

        assert result == {}
        dial.assert_awaited_once()
        connection.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_not_serving_onion_skipped(self, health_checker: MakerHealthChecker) -> None:
        """Test that NOT-SERVING-ONION makers are skipped."""
        status = await health_checker.check_maker("J5test", "NOT-SERVING-ONION")

        assert not status.reachable
        assert status.error == "NOT-SERVING-ONION"
        assert status.consecutive_failures == 0
        assert status.nick == "J5test"

    @pytest.mark.asyncio
    async def test_invalid_location_format(self, health_checker: MakerHealthChecker) -> None:
        """Test handling of invalid location format."""
        status = await health_checker.check_maker("J5test", "invalid")

        assert not status.reachable
        assert status.error is not None and "Invalid location" in status.error
        assert status.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_non_onion_location_rejected_by_default(
        self,
    ) -> None:
        health_checker = MakerHealthChecker(network="mainnet")
        with patch("orderbook_watcher.health_checker.connect_via_tor") as connect_mock:
            status = await health_checker.check_maker("J5test", "127.0.0.1:80")

        assert not status.reachable
        assert status.error is not None and "Non-onion" in status.error
        connect_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_clearnet_location_requires_explicit_opt_in(
        self, mock_connection: MagicMock
    ) -> None:
        checker = MakerHealthChecker(network="mainnet", allow_clearnet_connections=True)
        mock_connection.receive.return_value = create_peer_handshake_response()

        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ) as connect_mock:
            status = await checker.check_maker("J5test", "127.0.0.1:80")

        assert status.reachable
        assert connect_mock.call_args.args[:2] == ("127.0.0.1", 80)

    def test_regtest_enables_clearnet_development_mode(self) -> None:
        checker = MakerHealthChecker(network="regtest")

        assert checker.allow_clearnet_connections

    @pytest.mark.asyncio
    async def test_successful_health_check(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test successful maker health check."""
        # Mock successful connection and handshake
        mock_connection.receive.return_value = create_peer_handshake_response(
            features={FEATURE_NEUTRINO_COMPAT: True},
        )

        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            status = await health_checker.check_maker(
                "J5maker", "test123456789012345678901234567890123456789012345678.onion:5222"
            )

        assert status.reachable
        assert status.error is None
        assert status.consecutive_failures == 0
        assert status.last_success_time is not None
        assert FEATURE_NEUTRINO_COMPAT in status.features
        mock_connection.send.assert_called_once()
        mock_connection.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_connection_timeout(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test health check with connection timeout."""
        # Mock timeout during receive
        mock_connection.receive.side_effect = TimeoutError()

        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            status = await health_checker.check_maker(
                "J5maker", "test123456789012345678901234567890123456789012345678.onion:5222"
            )

        assert not status.reachable
        assert status.error == "Connection timeout"
        assert status.consecutive_failures == 1
        assert status.last_success_time is None
        mock_connection.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_handshake_rejected_by_directory(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test health check when DN_HANDSHAKE from a directory rejects us."""
        mock_connection.receive.return_value = create_dn_handshake_response(accepted=False)

        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            status = await health_checker.check_maker(
                "J5maker", "test123456789012345678901234567890123456789012345678.onion:5222"
            )

        assert not status.reachable
        assert status.error is not None and "Handshake rejected" in status.error
        assert status.consecutive_failures == 1

    @pytest.mark.asyncio
    async def test_consecutive_failures_tracking(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test that consecutive failures are tracked correctly."""
        location = "test123456789012345678901234567890123456789012345678.onion:5222"
        mock_connection.receive.side_effect = TimeoutError()

        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            # First failure
            status1 = await health_checker.check_maker("J5maker", location, force=True)
            assert status1.consecutive_failures == 1

            # Second failure
            status2 = await health_checker.check_maker("J5maker", location, force=True)
            assert status2.consecutive_failures == 2

            # Third failure
            status3 = await health_checker.check_maker("J5maker", location, force=True)
            assert status3.consecutive_failures == 3

    @pytest.mark.asyncio
    async def test_failure_then_success_resets_counter(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test that successful check resets consecutive failure counter."""
        location = "test123456789012345678901234567890123456789012345678.onion:5222"

        # First failure
        mock_connection.receive.side_effect = TimeoutError()
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            status1 = await health_checker.check_maker("J5maker", location, force=True)
            assert status1.consecutive_failures == 1

        # Then success
        mock_connection.receive.side_effect = None
        mock_connection.receive.return_value = create_peer_handshake_response()
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            status2 = await health_checker.check_maker("J5maker", location, force=True)
            assert status2.consecutive_failures == 0
            assert status2.reachable

    @pytest.mark.asyncio
    async def test_rate_limiting(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test that health checks are rate-limited."""
        location = "test123456789012345678901234567890123456789012345678.onion:5222"
        mock_connection.receive.return_value = create_peer_handshake_response()

        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            # First check
            status1 = await health_checker.check_maker("J5maker", location)
            first_check_time = status1.last_check_time

            # Immediate second check should be skipped (rate limited)
            status2 = await health_checker.check_maker("J5maker", location)
            assert status2.last_check_time == first_check_time

            # Force flag bypasses rate limiting
            status3 = await health_checker.check_maker("J5maker", location, force=True)
            assert status3.last_check_time > first_check_time

    @pytest.mark.asyncio
    async def test_batch_check_makers(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test checking multiple makers in batch."""
        makers = [
            ("J5maker1", "test1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
            ("J5maker2", "test2aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
            ("J5maker3", "test3aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
        ]

        # Mock different responses for each maker
        mock_connection.receive.side_effect = [
            create_peer_handshake_response(features={FEATURE_NEUTRINO_COMPAT: True}),
            TimeoutError(),
            create_peer_handshake_response(features={}),
        ]

        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            results = await health_checker.check_makers_batch(makers)

        assert len(results) == 3

        # First maker: success with neutrino
        assert results[makers[0][1]].reachable
        assert FEATURE_NEUTRINO_COMPAT in results[makers[0][1]].features

        # Second maker: timeout
        assert not results[makers[1][1]].reachable
        assert results[makers[1][1]].consecutive_failures == 1

        # Third maker: success without neutrino
        assert results[makers[2][1]].reachable
        assert FEATURE_NEUTRINO_COMPAT not in results[makers[2][1]].features

    @pytest.mark.asyncio
    async def test_batch_check_enforces_global_budget(self) -> None:
        checker = MakerHealthChecker(network="regtest", max_checks_per_batch=2)
        checked: list[str] = []

        async def mock_check(nick: str, location: str, force: bool = False) -> MagicMock:
            del nick, force
            checked.append(location)
            return MagicMock(reachable=True)

        checker.check_maker = mock_check  # type: ignore[assignment]
        makers = [(f"J5maker{i}", f"maker{i}.onion:5222") for i in range(4)]

        results = await checker.check_makers_batch(makers, force=True)

        assert checked == [makers[0][1], makers[1][1]]
        assert set(results) == set(checked)

    @pytest.mark.asyncio
    async def test_health_status_cache_evicts_oldest_location(self) -> None:
        checker = MakerHealthChecker(network="regtest", max_status_entries=2)

        with patch("orderbook_watcher.health_checker.connect_via_tor", side_effect=TimeoutError):
            for location in ("one.onion:1", "two.onion:2", "three.onion:3"):
                await checker.check_maker("J5test", location)

        assert list(checker.health_status) == ["two.onion:2", "three.onion:3"]

    @pytest.mark.asyncio
    async def test_invalid_location_is_not_retained(self) -> None:
        checker = MakerHealthChecker(network="regtest", max_status_entries=2)

        status = await checker.check_maker("J5test", "x" * 10_000)

        assert not status.reachable
        assert checker.health_status == {}

    @pytest.mark.asyncio
    async def test_concurrent_check_limit(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test that concurrent checks are limited by semaphore."""
        # Create many makers to test concurrency limit
        makers = [
            (f"J5maker{i}", f"test{i:056d}.onion:5222")
            for i in range(10)  # More than max_concurrent_checks (5)
        ]

        mock_connection.receive.return_value = create_peer_handshake_response()

        # Track concurrent connections
        concurrent_count = 0
        max_concurrent = 0

        async def mock_connect(
            onion_address: str,  # noqa: ARG001
            port: int,  # noqa: ARG001
            socks_host: str = "127.0.0.1",  # noqa: ARG001
            socks_port: int = 9050,  # noqa: ARG001
            max_message_size: int = 2097152,  # noqa: ARG001
            timeout: float = 30.0,  # noqa: ARG001, ASYNC109
        ) -> MagicMock:
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.01)  # Simulate connection delay
            concurrent_count -= 1
            return mock_connection

        with patch("orderbook_watcher.health_checker.connect_via_tor", side_effect=mock_connect):
            await health_checker.check_makers_batch(makers, force=True)

        # Should not exceed max_concurrent_checks (5)
        assert max_concurrent <= health_checker.max_concurrent_checks

    @pytest.mark.asyncio
    async def test_get_unreachable_locations(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test getting unreachable locations based on failure threshold."""
        makers = [
            ("J5maker1", "test1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
            ("J5maker2", "test2aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
            ("J5maker3", "test3aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
        ]

        # Maker1: 3 failures (should be unreachable at threshold=3)
        mock_connection.receive.side_effect = TimeoutError()
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            for _ in range(3):
                await health_checker.check_maker(makers[0][0], makers[0][1], force=True)

        # Maker2: 1 failure then success (should be healthy/reachable)
        mock_connection.receive.side_effect = TimeoutError()
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            await health_checker.check_maker(makers[1][0], makers[1][1], force=True)

        # Then maker2 succeeds
        mock_connection.receive.side_effect = None
        mock_connection.receive.return_value = create_peer_handshake_response()
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            await health_checker.check_maker(makers[1][0], makers[1][1], force=True)

        # Maker3: Success (reachable)
        mock_connection.receive.side_effect = None
        mock_connection.receive.return_value = create_peer_handshake_response()
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            await health_checker.check_maker(makers[2][0], makers[2][1])

        unreachable = health_checker.get_unreachable_locations(max_consecutive_failures=3)

        assert makers[0][1] in unreachable  # 3 failures
        assert makers[1][1] not in unreachable  # Recovered after failure
        assert makers[2][1] not in unreachable  # Success

    @pytest.mark.asyncio
    async def test_get_feature_map(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test getting feature map for successfully checked makers."""
        makers = [
            ("J5maker1", "test1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
            ("J5maker2", "test2aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
        ]

        # Maker1: Success with features
        mock_connection.receive.return_value = create_peer_handshake_response(
            features={FEATURE_NEUTRINO_COMPAT: True}
        )
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            await health_checker.check_maker(makers[0][0], makers[0][1])

        # Maker2: Failure (no features)
        mock_connection.receive.side_effect = TimeoutError()
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            await health_checker.check_maker(makers[1][0], makers[1][1])

        feature_map = health_checker.get_feature_map()

        assert makers[0][1] in feature_map
        assert FEATURE_NEUTRINO_COMPAT in feature_map[makers[0][1]]
        assert makers[1][1] not in feature_map  # Failed, no features

    @pytest.mark.asyncio
    async def test_clear_status(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test clearing health status for a location."""
        location = "test123456789012345678901234567890123456789012345678.onion:5222"

        # Create a status
        mock_connection.receive.return_value = create_peer_handshake_response()
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            await health_checker.check_maker("J5maker", location)

        assert location in health_checker.health_status

        # Clear status
        health_checker.clear_status(location)

        assert location not in health_checker.health_status

    @pytest.mark.asyncio
    async def test_exception_in_batch_check(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test that exceptions in batch check are handled gracefully."""
        makers = [
            ("J5maker1", "test1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
            ("J5maker2", "test2aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.onion:5222"),
        ]

        # First maker raises exception
        mock_connection.receive.side_effect = [
            RuntimeError("Connection error"),
            create_peer_handshake_response(),
        ]

        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            results = await health_checker.check_makers_batch(makers)

        # Both should have results
        assert len(results) == 2

        # First maker should have error status
        assert not results[makers[0][1]].reachable
        error_msg = results[makers[0][1]].error
        assert error_msg is not None and "Connection error" in error_msg

        # Second maker should be successful
        assert results[makers[1][1]].reachable

    @pytest.mark.asyncio
    async def test_features_preserved_on_failure(
        self, health_checker: MakerHealthChecker, mock_connection: MagicMock
    ) -> None:
        """Test that features are preserved when maker becomes unreachable."""
        location = "test123456789012345678901234567890123456789012345678.onion:5222"

        # First check: success with features
        mock_connection.receive.return_value = create_peer_handshake_response(
            features={FEATURE_NEUTRINO_COMPAT: True}
        )
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            status1 = await health_checker.check_maker("J5maker", location)

        assert FEATURE_NEUTRINO_COMPAT in status1.features

        # Second check: failure
        mock_connection.receive.side_effect = TimeoutError()
        with patch(
            "orderbook_watcher.health_checker.connect_via_tor", return_value=mock_connection
        ):
            status2 = await health_checker.check_maker("J5maker", location, force=True)

        # Features should be preserved from successful check
        assert FEATURE_NEUTRINO_COMPAT in status2.features
        assert not status2.reachable
