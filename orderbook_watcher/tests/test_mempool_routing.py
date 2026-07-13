"""Tests for orderbook watcher mempool transport selection."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.main import run_watcher


async def _finish_mempool_task(aggregator: OrderbookAggregator) -> None:
    task = aggregator._mempool_test_task
    if task is not None:
        await task


@pytest.mark.asyncio
async def test_tor_mode_uses_isolated_socks_transport() -> None:
    """The privacy-preserving default passes the isolated SOCKS URL to MempoolAPI."""
    api = MagicMock()
    api.test_connection = AsyncMock(return_value=True)
    proxy_url = "socks5h://mempool:secret@127.0.0.1:9050"

    with (
        patch("orderbook_watcher.aggregator.MempoolAPI", return_value=api) as api_cls,
        patch("jmcore.tor_isolation.build_isolated_proxy_url", return_value=proxy_url),
    ):
        aggregator = OrderbookAggregator(
            directory_nodes=[],
            network="regtest",
            mempool_api_url="https://mempool.example/api",
            stream_isolation=True,
        )
        await _finish_mempool_task(aggregator)

    assert aggregator.mempool_api_use_tor is True
    assert api_cls.call_args.kwargs["socks_proxy"] == proxy_url
    assert api_cls.call_args.kwargs["trust_env"] is False


@pytest.mark.asyncio
async def test_direct_mode_passes_no_proxy_and_ignores_environment() -> None:
    """Explicit direct mode does not create a SOCKS transport or trust env proxies."""
    api = MagicMock()
    api.test_connection = AsyncMock(return_value=True)

    with patch("orderbook_watcher.aggregator.MempoolAPI", return_value=api) as api_cls:
        aggregator = OrderbookAggregator(
            directory_nodes=[],
            network="regtest",
            mempool_api_url="http://127.0.0.1:8999",
            mempool_api_use_tor=False,
        )
        await _finish_mempool_task(aggregator)

    assert aggregator.mempool_api_use_tor is False
    assert api_cls.call_args.kwargs["socks_proxy"] is None
    assert api_cls.call_args.kwargs["trust_env"] is False


@pytest.mark.asyncio
async def test_run_watcher_passes_mempool_transport_setting(tmp_path: Path) -> None:
    """The setting is wired from run_watcher into the aggregator constructor."""
    settings = MagicMock()
    settings.logging.level = "INFO"
    settings.network_config.network.value = "regtest"
    settings.network_config.directory_servers = []
    settings.get_directory_servers.return_value = ["directory.onion:5222"]
    settings.get_data_dir.return_value = tmp_path
    settings.tor.socks_host = "127.0.0.1"
    settings.tor.socks_port = 9050
    settings.tor.stream_isolation = True
    settings.orderbook_watcher.http_host = "127.0.0.1"
    settings.orderbook_watcher.http_port = 8000
    settings.orderbook_watcher.update_interval = 60
    settings.orderbook_watcher.connection_timeout = 30.0
    settings.orderbook_watcher.mempool_api_url = "http://127.0.0.1:8999"
    settings.orderbook_watcher.mempool_api_use_tor = False
    settings.orderbook_watcher.max_message_size = 2097152
    settings.orderbook_watcher.uptime_grace_period = 60
    nick_identity = MagicMock(nick="J5abc")
    notifier = MagicMock(notify_startup=AsyncMock())
    server = MagicMock(start=AsyncMock(), stop=AsyncMock())
    shutdown_event = MagicMock(wait=AsyncMock())

    with (
        patch("orderbook_watcher.main.get_settings", return_value=settings),
        patch("orderbook_watcher.main.setup_logging"),
        patch("orderbook_watcher.main.NickIdentity", return_value=nick_identity),
        patch(
            "orderbook_watcher.main.get_directory_nodes", return_value=[("directory.onion", 5222)]
        ),
        patch("orderbook_watcher.main.write_nick_state"),
        patch("orderbook_watcher.main._create_blockchain_backend", return_value=None),
        patch("orderbook_watcher.main.OrderbookAggregator") as aggregator_cls,
        patch("orderbook_watcher.main.OrderbookServer", return_value=server),
        patch("orderbook_watcher.main.get_notifier", return_value=notifier),
        patch("orderbook_watcher.main.remove_nick_state"),
        patch("orderbook_watcher.main.asyncio.get_running_loop", return_value=MagicMock()),
        patch("orderbook_watcher.main.asyncio.Event", return_value=shutdown_event),
    ):
        await run_watcher()

    assert aggregator_cls.call_args.kwargs["mempool_api_use_tor"] is False
