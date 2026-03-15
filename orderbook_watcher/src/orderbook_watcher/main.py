"""
Main entry point for the orderbook watcher.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from typing import TYPE_CHECKING

from jmcore.crypto import NickIdentity
from jmcore.notifications import get_notifier
from jmcore.paths import remove_nick_state, write_nick_state
from jmcore.protocol import JM_VERSION
from jmcore.settings import get_settings
from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
from jmwallet.backends.neutrino import NeutrinoBackend
from loguru import logger

from orderbook_watcher.aggregator import OrderbookAggregator
from orderbook_watcher.config import get_directory_nodes
from orderbook_watcher.server import OrderbookServer

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend


def setup_logging(level: str) -> None:
    logger.remove()

    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=level,
        colorize=True,
    )


def _create_blockchain_backend(settings: object) -> BlockchainBackend | None:
    """Create a blockchain backend for bond verification if Bitcoin settings are configured.

    Returns a BitcoinCoreBackend for full node configurations, a NeutrinoBackend for
    neutrino configurations, or None to fall back to the mempool API.
    """
    bitcoin_settings = settings.bitcoin  # type: ignore[attr-defined]
    backend_type = bitcoin_settings.backend_type

    if backend_type in ("scantxoutset", "descriptor_wallet"):
        rpc_url = bitcoin_settings.rpc_url
        rpc_user = bitcoin_settings.rpc_user
        rpc_password = bitcoin_settings.rpc_password.get_secret_value()

        if not rpc_url or not rpc_user:
            logger.debug("Bitcoin RPC not configured, falling back to mempool API")
            return None

        logger.info(f"Using Bitcoin Core backend for bond verification (RPC: {rpc_url})")
        return BitcoinCoreBackend(
            rpc_url=rpc_url,
            rpc_user=rpc_user,
            rpc_password=rpc_password,
        )

    if backend_type == "neutrino":
        neutrino_url = bitcoin_settings.neutrino_url

        if not neutrino_url:
            logger.debug("Neutrino URL not configured, falling back to mempool API")
            return None

        network = settings.network_config.network.value  # type: ignore[attr-defined]

        logger.info(f"Using neutrino backend for bond verification (URL: {neutrino_url})")
        return NeutrinoBackend(
            neutrino_url=neutrino_url,
            network=network,
            scan_start_height=settings.wallet.scan_start_height,  # type: ignore[attr-defined]
            connect_peers=settings.get_neutrino_connect_peers(),  # type: ignore[attr-defined]
        )

    logger.debug(f"Unknown backend type '{backend_type}', falling back to mempool API")
    return None


async def run_watcher(log_level: str | None = None) -> None:
    settings = get_settings()
    # Use CLI log level if provided, otherwise fall back to settings
    effective_log_level = log_level if log_level else settings.logging.level
    setup_logging(effective_log_level)

    network = settings.network_config.network
    watcher_settings = settings.orderbook_watcher
    data_dir = settings.get_data_dir()

    # Generate a nick for the orderbook watcher
    nick_identity = NickIdentity(JM_VERSION)
    watcher_nick = nick_identity.nick

    logger.info("=" * 80)
    logger.info("Starting JoinMarket Orderbook Watcher")
    logger.info(f"Network: {network.value}")
    logger.info(f"Nick: {watcher_nick}")
    logger.info(f"HTTP server: {watcher_settings.http_host}:{watcher_settings.http_port}")
    logger.info(f"Update interval: {watcher_settings.update_interval}s")
    if watcher_settings.mempool_api_url:
        logger.info(f"Mempool API: {watcher_settings.mempool_api_url}")
    else:
        logger.warning("Mempool API not configured")

    # Directory nodes from env var (DIRECTORY_NODES) or config
    directory_nodes_str = os.environ.get("DIRECTORY_NODES", "")
    if not directory_nodes_str:
        # Fall back to directory servers from network config
        if settings.network_config.directory_servers:
            directory_nodes_str = ",".join(settings.network_config.directory_servers)
        else:
            # Use default directory servers
            directory_nodes_str = ",".join(settings.get_directory_servers())

    directory_nodes = get_directory_nodes(directory_nodes_str)
    if not directory_nodes:
        logger.error("No directory nodes configured. Set DIRECTORY_NODES environment variable.")
        logger.error("Example: DIRECTORY_NODES=node1.onion:5222,node2.onion:5222")
        sys.exit(1)

    logger.info(f"Directory nodes: {len(directory_nodes)}")
    for node in directory_nodes:
        logger.info(f"  - {node[0]}:{node[1]}")
    logger.info("=" * 80)

    # Write nick state file for external tracking
    write_nick_state(data_dir, "orderbook", watcher_nick)
    logger.info(f"Nick state written to {data_dir}/state/orderbook.nick")

    # Create blockchain backend for bond verification if configured
    blockchain_backend = _create_blockchain_backend(settings)

    aggregator = OrderbookAggregator(
        directory_nodes=directory_nodes,
        network=network.value,
        socks_host=settings.tor.socks_host,
        socks_port=settings.tor.socks_port,
        timeout=watcher_settings.connection_timeout,
        mempool_api_url=watcher_settings.mempool_api_url,
        max_message_size=watcher_settings.max_message_size,
        uptime_grace_period=watcher_settings.uptime_grace_period,
        stream_isolation=settings.tor.stream_isolation,
        blockchain_backend=blockchain_backend,
    )

    server = OrderbookServer(watcher_settings, aggregator)

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def shutdown_handler() -> None:
        logger.info("Received shutdown signal")
        shutdown_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_handler)

    try:
        # Send startup notification immediately (including nick)
        notifier = get_notifier(settings, component_name="Orderbook")
        await notifier.notify_startup(
            component="Orderbook Watcher",
            network=network.value,
            nick=watcher_nick,
        )
        await server.start()
        await shutdown_event.wait()
    except asyncio.CancelledError:
        logger.info("Watcher cancelled")
    except Exception as e:
        logger.error(f"Watcher error: {e}")
        raise
    finally:
        # Clean up nick state file on shutdown
        remove_nick_state(data_dir, "orderbook")
        await server.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="JoinMarket Orderbook Watcher")
    parser.add_argument(
        "--log-level",
        "-l",
        default=None,
        help="Log level (default: from config or INFO)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_watcher(log_level=args.log_level))
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
