"""
Tests for uptime tracking in server orderbook output.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from jmcore.models import OrderBook
from jmcore.settings import OrderbookWatcherSettings

from orderbook_watcher.aggregator import DirectoryNodeStatus, OrderbookAggregator
from orderbook_watcher.server import OrderbookServer


def test_server_includes_uptime_in_directory_stats() -> None:
    """Test that uptime stats are included in the orderbook JSON output."""
    # Create mock settings
    settings = OrderbookWatcherSettings()

    # Create mock aggregator
    aggregator = MagicMock(spec=OrderbookAggregator)
    aggregator.directory_nodes = [("node1.onion", 5222), ("node2.onion", 5222)]
    aggregator.mempool_api_url = "http://dummy.api"
    aggregator.clients = {}  # No client metadata for this test

    # Create node statuses with some uptime data
    start_time = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    node1_status = DirectoryNodeStatus("node1.onion:5222", tracking_started=start_time)
    node1_status.mark_connected(start_time)
    node1_status.mark_disconnected(start_time + timedelta(minutes=5))

    node2_status = DirectoryNodeStatus("node2.onion:5222", tracking_started=start_time)
    node2_status.mark_connected(start_time)

    aggregator.node_statuses = {
        "node1.onion:5222": node1_status,
        "node2.onion:5222": node2_status,
    }

    # Create server
    server = OrderbookServer(settings, aggregator)

    # Create a mock orderbook
    orderbook = OrderBook(timestamp=start_time + timedelta(minutes=10))

    # Format the orderbook
    result = server._format_orderbook(orderbook)

    # Verify directory_stats includes uptime information
    assert "directory_stats" in result
    directory_stats = result["directory_stats"]

    # Check node1
    assert "node1.onion:5222" in directory_stats
    node1_stats = directory_stats["node1.onion:5222"]
    assert "uptime_percentage" in node1_stats
    assert "connection_attempts" in node1_stats
    assert "successful_connections" in node1_stats
    assert "tracking_started" in node1_stats
    assert node1_stats["connected"] is False
    assert node1_stats["uptime_percentage"] == 50.0  # 5 min connected / 10 min total

    # Check node2
    assert "node2.onion:5222" in directory_stats
    node2_stats = directory_stats["node2.onion:5222"]
    assert "uptime_percentage" in node2_stats
    assert node2_stats["connected"] is True
    assert node2_stats["uptime_percentage"] == 100.0  # Still connected


def test_server_handles_nodes_without_status() -> None:
    """Test that nodes without connection status still appear in directory_stats."""
    settings = OrderbookWatcherSettings()

    aggregator = MagicMock(spec=OrderbookAggregator)
    aggregator.directory_nodes = [("node1.onion", 5222), ("node2.onion", 5222)]
    aggregator.mempool_api_url = "http://dummy.api"
    aggregator.node_statuses = {}  # No status data
    aggregator.clients = {}  # No client metadata for this test

    server = OrderbookServer(settings, aggregator)
    orderbook = OrderBook(timestamp=datetime.now(UTC))

    result = server._format_orderbook(orderbook)

    # Both nodes should still appear in stats with offer counts
    assert "node1.onion:5222" in result["directory_stats"]
    assert "node2.onion:5222" in result["directory_stats"]
    assert result["directory_stats"]["node1.onion:5222"]["offer_count"] == 0
    assert result["directory_stats"]["node2.onion:5222"]["offer_count"] == 0
