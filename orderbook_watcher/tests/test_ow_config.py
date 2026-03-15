"""
Tests for configuration management.
"""

from unittest.mock import MagicMock, patch

from jmcore.settings import OrderbookWatcherSettings

from orderbook_watcher.config import get_directory_nodes
from orderbook_watcher.main import _create_blockchain_backend


def test_default_settings() -> None:
    settings = OrderbookWatcherSettings()
    assert settings.http_port == 8000
    assert settings.update_interval == 60


def test_directory_nodes_parsing() -> None:
    nodes = get_directory_nodes("node1.onion:5222,node2.onion:5223")
    assert len(nodes) == 2
    assert nodes[0] == ("node1.onion", 5222)
    assert nodes[1] == ("node2.onion", 5223)


def test_directory_nodes_default_port() -> None:
    nodes = get_directory_nodes("node1.onion")
    assert len(nodes) == 1
    assert nodes[0] == ("node1.onion", 5222)


def test_empty_directory_nodes() -> None:
    nodes = get_directory_nodes("")
    assert len(nodes) == 0


def test_mempool_urls() -> None:
    settings = OrderbookWatcherSettings(
        mempool_api_url="https://api.example.com",
        mempool_web_url="https://web.example.com",
    )
    assert settings.mempool_api_url == "https://api.example.com"
    assert settings.mempool_web_url == "https://web.example.com"


class TestCreateBlockchainBackend:
    """Tests for _create_blockchain_backend() passing connect_peers to NeutrinoBackend."""

    def _make_settings(
        self,
        backend_type: str = "neutrino",
        neutrino_url: str = "http://127.0.0.1:8334",
        connect_peers: list[str] | None = None,
        scan_start_height: int | None = None,
    ) -> MagicMock:
        settings = MagicMock()
        settings.bitcoin.backend_type = backend_type
        settings.bitcoin.neutrino_url = neutrino_url
        settings.network_config.network.value = "signet"
        settings.wallet.scan_start_height = scan_start_height
        settings.get_neutrino_connect_peers.return_value = connect_peers or []
        return settings

    def test_neutrino_backend_passes_connect_peers(self) -> None:
        """_create_blockchain_backend() passes connect_peers to NeutrinoBackend."""
        peers = ["peer1.example.com:38333", "peer2.example.com:38333"]
        settings = self._make_settings(connect_peers=peers)

        mock_backend = MagicMock()
        with patch("orderbook_watcher.main.NeutrinoBackend", return_value=mock_backend) as mock_cls:
            result = _create_blockchain_backend(settings)

        mock_cls.assert_called_once_with(
            neutrino_url="http://127.0.0.1:8334",
            network="signet",
            scan_start_height=None,
            connect_peers=peers,
        )
        assert result is mock_backend

    def test_neutrino_backend_empty_connect_peers(self) -> None:
        """_create_blockchain_backend() passes empty list when no peers configured."""
        settings = self._make_settings(connect_peers=[])

        mock_backend = MagicMock()
        with patch("orderbook_watcher.main.NeutrinoBackend", return_value=mock_backend) as mock_cls:
            _create_blockchain_backend(settings)

        mock_cls.assert_called_once_with(
            neutrino_url="http://127.0.0.1:8334",
            network="signet",
            scan_start_height=None,
            connect_peers=[],
        )
