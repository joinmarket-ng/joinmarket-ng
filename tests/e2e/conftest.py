"""
E2E test configuration and fixtures.

Provides parameterized blockchain backend fixtures for testing
with different backends (Bitcoin Core, Neutrino).

Also provides fixtures for Docker service detection and wallet funding.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.error
import urllib.request
import time
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from loguru import logger

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add custom pytest options for e2e tests."""
    parser.addoption(
        "--neutrino-url",
        action="store",
        default="http://127.0.0.1:8334",
        help="Neutrino REST API URL",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers for e2e tests.

    Markers are defined in pytest.ini but we add descriptions here for clarity.

    Docker profile markers (mutually exclusive):
    - docker: Base marker for any test requiring Docker services
    - e2e: Tests requiring 'docker compose --profile e2e' (our implementation)
    - reference: Tests requiring 'docker compose --profile reference' (JAM web UI for reference JoinMarket)
    - neutrino: Tests requiring 'docker compose --profile neutrino' (light client)
    - reference_maker: Tests requiring 'docker compose --profile reference-maker'

    By default, `pytest` excludes docker-marked tests via pytest.ini addopts.
    To run Docker tests, use `-m docker` or specific profile markers like `-m e2e`.
    """
    # Note: --fail-on-skip is handled by root conftest.py
    pass


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    """Auto-add docker marker to tests that have profile-specific markers.

    This ensures that tests marked with e2e, reference, neutrino, or reference_maker
    are also automatically marked with 'docker', so they get excluded by default.
    """
    docker_marker = pytest.mark.docker

    for item in items:
        # Check if item has any profile-specific marker
        profile_markers = {"e2e", "reference", "neutrino", "reference_maker"}
        item_markers = {marker.name for marker in item.iter_markers()}

        # If the test has a profile marker but not 'docker', add 'docker'
        if item_markers & profile_markers and "docker" not in item_markers:
            item.add_marker(docker_marker)


@pytest.fixture(scope="session")
def neutrino_url(request: pytest.FixtureRequest) -> str:
    """Get the neutrino URL from command line or environment."""
    url = request.config.getoption("--neutrino-url")
    return os.environ.get("NEUTRINO_URL", url)


@pytest.fixture
def bitcoin_rpc_config() -> dict[str, str]:
    """Bitcoin Core RPC configuration from environment or defaults."""
    return {
        "rpc_url": os.environ.get("BITCOIN_RPC_URL", "http://127.0.0.1:18443"),
        "rpc_user": os.environ.get("BITCOIN_RPC_USER", "test"),
        "rpc_password": os.environ.get("BITCOIN_RPC_PASSWORD", "test"),
    }


@pytest_asyncio.fixture
async def bitcoin_core_backend(
    bitcoin_rpc_config: dict[str, str],
) -> AsyncGenerator[BlockchainBackend, None]:
    """Create Bitcoin Core backend for tests."""
    from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

    backend = BitcoinCoreBackend(
        rpc_url=bitcoin_rpc_config["rpc_url"],
        rpc_user=bitcoin_rpc_config["rpc_user"],
        rpc_password=bitcoin_rpc_config["rpc_password"],
    )
    yield backend
    await backend.close()


@pytest_asyncio.fixture
async def neutrino_backend_fixture(
    neutrino_url: str,
) -> AsyncGenerator[BlockchainBackend, None]:
    """Create Neutrino backend for tests."""
    from jmwallet.backends.neutrino import NeutrinoBackend

    backend = NeutrinoBackend(
        neutrino_url=neutrino_url,
        network="regtest",
    )

    # Verify neutrino is available - fail if not
    try:
        height = await backend.get_block_height()
        logger.info(f"Neutrino backend connected, height: {height}")
    except Exception as e:
        pytest.fail(f"Neutrino server not available at {neutrino_url}: {e}")

    yield backend
    await backend.close()


@pytest_asyncio.fixture
async def blockchain_backend(
    request: pytest.FixtureRequest,
    bitcoin_rpc_config: dict[str, str],
) -> AsyncGenerator[BlockchainBackend, None]:
    """
    Bitcoin Core blockchain backend fixture.

    Use this fixture for tests that need Bitcoin Core backend specifically.
    For neutrino tests, use neutrino_backend_fixture.
    """
    from jmwallet.backends.bitcoin_core import BitcoinCoreBackend

    backend = BitcoinCoreBackend(
        rpc_url=bitcoin_rpc_config["rpc_url"],
        rpc_user=bitcoin_rpc_config["rpc_user"],
        rpc_password=bitcoin_rpc_config["rpc_password"],
    )

    yield backend
    await backend.close()


# =============================================================================
# Docker Service Detection
# =============================================================================


def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Check if a TCP port is open."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        result = sock.connect_ex((host, port))
        return result == 0
    finally:
        sock.close()


def is_directory_server_running(host: str = "127.0.0.1", port: int = 5222) -> bool:
    """Check if directory server is running on the specified port."""
    return is_port_open(host, port)


def is_bitcoin_running(host: str = "127.0.0.1", port: int = 18443) -> bool:
    """Check if Bitcoin RPC is accessible."""
    return is_port_open(host, port)


def wait_for_neutrino_ready_if_present(timeout: float = 180.0) -> bool:
    """Wait for local neutrino to have a usable height when running.

    Returns:
        True if neutrino is not running locally or became ready.
        False if neutrino is running but never became ready.
    """
    if not is_port_open("127.0.0.1", 8334, timeout=0.5):
        return True

    deadline = time.time() + timeout
    status_url = "http://127.0.0.1:8334/v1/status"

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(status_url, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
                height = int(payload.get("block_height", 0))
                if height > 0:
                    return True
        except (TimeoutError, ValueError, urllib.error.URLError, json.JSONDecodeError):
            pass

        time.sleep(2)

    return False


@pytest.fixture(scope="session")
def docker_services_available() -> bool:
    """
    Check if Docker services are running.

    Returns True if both Bitcoin and Directory server are accessible.
    This is a session-scoped fixture so it's only checked once.
    """
    bitcoin_ok = is_bitcoin_running()
    directory_ok = is_directory_server_running()

    if not bitcoin_ok:
        logger.warning("Bitcoin Core not accessible on port 18443")
    if not directory_ok:
        logger.warning("Directory server not accessible on port 5222")

    return bitcoin_ok and directory_ok


@pytest.fixture(scope="module")
def require_docker_services(docker_services_available: bool) -> None:
    """
    Skip the test module if Docker services are not running.

    Use this fixture in tests that require the Docker Compose stack.
    """
    if not docker_services_available:
        pytest.skip(
            "Docker services not running. Start with: docker compose up -d\n"
            "Or for full e2e: docker compose --profile all up -d"
        )


@pytest_asyncio.fixture(scope="session")
async def ensure_blockchain_ready() -> None:
    """
    Ensure blockchain has sufficient height for coinbase maturity.

    Mines blocks if needed to reach height > 110.
    This is session-scoped so it only runs once per test session.
    """
    from tests.e2e.rpc_utils import mine_blocks, rpc_call

    try:
        info = await rpc_call("getblockchaininfo")
        height = info.get("blocks", 0)
        logger.info(f"Current blockchain height: {height}")

        if height < 110:
            blocks_needed = 120 - height
            # Mine to a valid P2WPKH address
            addr = "bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080"
            logger.info(f"Mining {blocks_needed} blocks for coinbase maturity...")
            await mine_blocks(blocks_needed, addr)
            logger.info(f"Mined {blocks_needed} blocks, new height: {120}")
    except Exception as e:
        logger.warning(f"Could not ensure blockchain ready: {e}")


@pytest_asyncio.fixture(scope="module")
async def wait_for_directory_server(
    docker_services_available: bool,
) -> AsyncGenerator[None, None]:
    """
    Wait for directory server to be ready and accepting connections.

    This fixture:
    1. Checks if the port is open
    2. Optionally performs a simple handshake check
    """
    if not docker_services_available:
        pytest.skip("Docker services not available")

    max_wait = 30  # seconds
    start = time.time()

    while time.time() - start < max_wait:
        if is_directory_server_running():
            logger.info("Directory server is ready")
            yield
            return
        await asyncio.sleep(1)

    pytest.skip("Directory server did not become ready in time")


@pytest.fixture(scope="function")
def fresh_docker_makers():
    """Restart Docker makers before test to ensure fresh UTXOs.

    This fixture restarts the Docker maker containers to prevent UTXO reuse
    between tests, which can cause transaction verification failures.

    It also stops any non-e2e profile makers that might interfere with tests.

    The wait time is generous to allow for:
    - Container restart
    - Wallet sync with blockchain
    - Directory server reconnection
    - Offer announcement and propagation
    """
    import subprocess

    from jmcore.paths import get_used_commitments_path

    try:
        if not wait_for_neutrino_ready_if_present(timeout=180):
            logger.warning(
                "Neutrino service is reachable but not ready (height <= 0) before maker restart"
            )

        # Stop any non-e2e profile makers that might be running
        # This prevents stale offers from interfering with tests
        subprocess.run(
            ["docker", "stop", "jm-maker"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Clear the taker's used commitments on the host machine
        # The in-process taker uses ~/.joinmarket-ng/cmtdata/commitments.json
        # Without clearing this, the taker may exhaust PoDLE indices for its UTXOs
        taker_commitments = get_used_commitments_path()
        if taker_commitments.exists():
            taker_commitments.unlink()
            logger.info(f"Cleared taker used commitments: {taker_commitments}")

        # Clear commitment blacklists for all makers before restarting
        for maker in ["jm-maker1", "jm-maker2", "jm-maker3", "jm-maker-neutrino"]:
            try:
                subprocess.run(
                    [
                        "docker",
                        "exec",
                        maker,
                        "sh",
                        "-c",
                        "rm -rf /home/jm/.joinmarket-ng/cmtdata/commitmentlist",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                logger.debug(f"Cleared commitment blacklist for {maker}")
            except Exception as e:
                logger.warning(f"Failed to clear commitment blacklist for {maker}: {e}")

        # Restart the e2e profile makers (including neutrino maker for neutrino tests)
        result = subprocess.run(
            [
                "docker",
                "restart",
                "jm-maker1",
                "jm-maker2",
                "jm-maker3",
                "jm-maker-neutrino",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            logger.info("Restarted Docker makers, waiting for startup...")
            # Wait for makers to fully initialize:
            # - Container start: ~5s
            # - Wallet sync: ~10-20s
            # - Directory connection & offer announcement: ~5-10s
            time.sleep(90)
        else:
            logger.warning(f"Failed to restart makers: {result.stderr}")
    except subprocess.TimeoutExpired:
        logger.warning("Docker restart timed out")
    except FileNotFoundError:
        logger.warning("Docker command not found")
    except Exception as e:
        logger.warning(f"Could not restart makers: {e}")

    yield
