from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.crypto import NickIdentity
from jmcore.nick_auth import NickAuthMode

from orderbook_watcher.aggregator import OrderbookAggregator


def _mock_client() -> MagicMock:
    client = MagicMock()
    client.connect = AsyncMock(return_value=None)
    client.fetch_orderbooks = AsyncMock(return_value=([], []))
    client.close = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_aggregator_reuses_identity_and_auth_mode_for_all_directory_clients() -> None:
    identity = NickIdentity(private_key_bytes=b"\x01" * 32)
    aggregator = OrderbookAggregator(
        directory_nodes=[("directory.example", 5222)],
        network="regtest",
        mempool_api_url="",
        nick_identity=identity,
        nick_auth_mode=NickAuthMode.REQUIRE_VERIFIED,
        nick_auth_directory_ids={"directory.example:5222": "test:directory-a"},
    )
    clients = [_mock_client(), _mock_client(), _mock_client()]

    with patch("orderbook_watcher.aggregator.DirectoryClient", side_effect=clients) as client_cls:
        await aggregator.fetch_from_directory("directory.example", 5222)
        await aggregator._connect_to_node("directory.example", 5222)
        await aggregator._connect_to_node("directory.example", 5222)

    assert client_cls.call_count == 3
    for call in client_cls.call_args_list:
        assert call.kwargs["nick_identity"] is identity
        assert call.kwargs["nick_auth_mode"] is NickAuthMode.REQUIRE_VERIFIED
        assert call.kwargs["nick_auth_directory_id"] == "test:directory-a"
