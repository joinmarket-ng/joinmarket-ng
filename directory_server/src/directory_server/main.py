"""
Main entry point for the directory server.
"""

import asyncio
import signal
import sys

from jmcore.crypto import generate_jm_nick
from jmcore.notifications import get_notifier
from jmcore.paths import remove_nick_state, write_nick_state
from jmcore.settings import get_settings
from jmcore.tasks import spawn_task
from loguru import logger

from directory_server.server import DirectoryServer


def setup_logging(level: str) -> None:
    logger.remove()

    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level=level,
        colorize=True,
    )


async def run_server() -> None:
    settings = get_settings()
    setup_logging(settings.logging.level)

    # Initialize notifier with settings before creating server
    # This ensures DirectoryServer can use get_notifier() with config file settings
    notifier = get_notifier(settings, component_name="Directory")

    network = settings.network_config.network
    # Generate random nick like any other peer (matches reference implementation)
    server_nick = generate_jm_nick()
    data_dir = settings.get_data_dir()

    logger.info("=" * 80)
    logger.info("Starting JoinMarket NG Directory Server")
    logger.info(f"Network: {network.value}")
    logger.info(f"Server nick: {server_nick}")
    logger.info(f"Port: {settings.directory_server.port}")
    logger.info(f"Max peers: {settings.directory_server.max_peers}")
    logger.info("=" * 80)

    # Write nick state file for external tracking
    write_nick_state(data_dir, "directory", server_nick)
    logger.info(f"Nick state written to {data_dir}/state/directory.nick")

    server = DirectoryServer(settings.directory_server, network, server_nick)

    loop = asyncio.get_running_loop()

    def shutdown_handler() -> None:
        logger.info("Received shutdown signal")
        spawn_task(server.stop())

    def status_handler() -> None:
        logger.info("Received status signal")
        server.log_status()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_handler)

    loop.add_signal_handler(signal.SIGUSR1, status_handler)

    try:
        # Send startup notification (including nick)
        await notifier.notify_startup(
            component="Directory Server",
            network=network.value,
            nick=server_nick,
        )
        await server.start()
    except Exception as e:
        logger.error(f"Server error: {e}")
        raise
    finally:
        # Clean up nick state file on shutdown
        remove_nick_state(data_dir, "directory")
        await server.stop()


def main() -> None:
    try:
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
