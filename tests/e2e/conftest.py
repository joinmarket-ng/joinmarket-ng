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
import ssl
import subprocess
import urllib.error
import urllib.request
import time
from collections.abc import AsyncGenerator
from pathlib import Path
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
        default=None,
        help="Neutrino REST API URL (auto-detected if not set)",
    )
    parser.addoption(
        "--neutrino-tls-cert",
        action="store",
        default=None,
        help="Path to neutrino TLS certificate",
    )
    parser.addoption(
        "--neutrino-auth-token",
        action="store",
        default=None,
        help="Neutrino API auth token",
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

    This ensures that tests marked with e2e, reference, neutrino,
    reference_maker, neutrino_reference, or tumbler_e2e are also
    automatically marked with 'docker', so they get excluded by default.
    """
    docker_marker = pytest.mark.docker

    for item in items:
        # Check if item has any profile-specific marker
        profile_markers = {
            "e2e",
            "reference",
            "neutrino",
            "reference_maker",
            "neutrino_reference",
            "tumbler_e2e",
        }
        item_markers = {marker.name for marker in item.iter_markers()}

        # If the test has a profile marker but not 'docker', add 'docker'
        if item_markers & profile_markers and "docker" not in item_markers:
            item.add_marker(docker_marker)


def _read_neutrino_credential(filename: str) -> str | None:
    """Read a neutrino credential file from the Docker volume.

    Copies the file from the neutrino container to read its contents.
    Returns None if the container is not running or the file doesn't exist.
    """
    from tests.e2e.docker_utils import docker_exec, get_container_name

    container = get_container_name("neutrino")
    return docker_exec(container, ["cat", f"/data/neutrino/{filename}"], timeout=5)


def _extract_neutrino_tls_cert(tmp_dir: Path) -> str | None:
    """Copy the neutrino TLS cert from Docker volume to a local temp file.

    Returns the path to the extracted cert or None.
    """
    from tests.e2e.docker_utils import docker_cp, get_container_name

    container = get_container_name("neutrino")
    cert_path = tmp_dir / "tls.cert"
    if docker_cp(f"{container}:/data/neutrino/tls.cert", str(cert_path)):
        if cert_path.exists():
            return str(cert_path)
    return None


def _resolve_neutrino_url(explicit_url: str | None, auth_token: str | None) -> str:
    """Resolve neutrino URL, upgrading stale HTTP config when auth is enabled.

    In TLS/auth mode, neutrino-api serves HTTPS only. Some CI/base workflow
    paths may still provide ``NEUTRINO_URL=http://...``; when an auth token is
    present, that URL must be upgraded to HTTPS or requests will fail with
    "client sent an HTTP request to an HTTPS server".
    """
    if explicit_url is not None:
        if auth_token and explicit_url.startswith("http://"):
            upgraded = "https://" + explicit_url.removeprefix("http://")
            logger.warning(
                "Neutrino auth token detected; upgrading URL from "
                f"{explicit_url} to {upgraded}"
            )
            return upgraded
        return explicit_url

    if auth_token:
        return "https://127.0.0.1:8334"
    return "http://127.0.0.1:8334"


@pytest.fixture(scope="session")
def neutrino_url(
    request: pytest.FixtureRequest,
    neutrino_auth_token: str | None,
) -> str:
    """Get the neutrino URL from command line or environment.

    Auto-detects HTTPS vs HTTP based on whether auth credentials are
    available, and upgrades stale ``http://`` overrides to ``https://`` when
    auth is enabled.
    """
    explicit_url = request.config.getoption("--neutrino-url")
    if explicit_url is None:
        explicit_url = os.environ.get("NEUTRINO_URL")

    return _resolve_neutrino_url(explicit_url, neutrino_auth_token)


@pytest.fixture(scope="session")
def neutrino_tls_cert(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> str | None:
    """Get the neutrino TLS certificate path.

    Reads from CLI option, environment, or extracts from Docker volume.
    """
    cert = request.config.getoption("--neutrino-tls-cert")
    if cert is not None:
        return cert
    cert = os.environ.get("NEUTRINO_TLS_CERT")
    if cert is not None:
        return cert

    # Try to extract from Docker volume
    tmp_dir = tmp_path_factory.mktemp("neutrino_creds")
    return _extract_neutrino_tls_cert(tmp_dir)


@pytest.fixture(scope="session")
def neutrino_auth_token(request: pytest.FixtureRequest) -> str | None:
    """Get the neutrino auth token.

    Reads from CLI option, environment, or Docker volume.
    """
    token = request.config.getoption("--neutrino-auth-token")
    if token is not None:
        return token
    token = os.environ.get("NEUTRINO_AUTH_TOKEN")
    if token is not None:
        return token

    # Try to read from Docker volume
    return _read_neutrino_credential("auth_token")


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
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

    backend = DescriptorWalletBackend(
        rpc_url=bitcoin_rpc_config["rpc_url"],
        rpc_user=bitcoin_rpc_config["rpc_user"],
        rpc_password=bitcoin_rpc_config["rpc_password"],
    )
    yield backend
    await backend.close()


@pytest_asyncio.fixture
async def neutrino_backend_fixture(
    neutrino_url: str,
    neutrino_tls_cert: str | None,
    neutrino_auth_token: str | None,
) -> AsyncGenerator[BlockchainBackend, None]:
    """Create Neutrino backend for tests."""
    from jmwallet.backends.neutrino import NeutrinoBackend

    backend = NeutrinoBackend(
        neutrino_url=neutrino_url,
        network="regtest",
        tls_cert_path=neutrino_tls_cert,
        auth_token=neutrino_auth_token,
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
    from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

    backend = DescriptorWalletBackend(
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


def is_directory_server_running(
    host: str = "127.0.0.1", port: int | None = None
) -> bool:
    """Check if directory server is running on the specified port."""
    if port is None:
        from tests.e2e.docker_utils import get_directory_port

        port = get_directory_port()
    return is_port_open(host, port)


def is_bitcoin_running(host: str = "127.0.0.1", port: int | None = None) -> bool:
    """Check if Bitcoin RPC is accessible."""
    if port is None:
        from tests.e2e.docker_utils import get_bitcoin_rpc_port

        port = get_bitcoin_rpc_port()
    return is_port_open(host, port)


def wait_for_neutrino_ready_if_present(timeout: float = 180.0) -> bool:
    """Wait for local neutrino to have a usable height when running.

    Tries HTTPS with auth token first, then falls back to plain HTTP.
    When TLS is enabled, reads the auth token from the Docker volume and
    uses an unverified SSL context (health check only).

    Returns:
        True if neutrino is not running locally or became ready.
        False if neutrino is running but never became ready.
    """
    from tests.e2e.docker_utils import get_neutrino_port

    neutrino_port = get_neutrino_port()

    if not is_port_open("127.0.0.1", neutrino_port, timeout=0.5):
        return True

    deadline = time.time() + timeout

    # Try to read auth token for TLS mode
    token = _read_neutrino_credential("auth_token")

    # Build URL and request based on TLS availability
    if token:
        status_url = f"https://127.0.0.1:{neutrino_port}/v1/status"
        # Skip cert verification for health check
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    else:
        status_url = f"http://127.0.0.1:{neutrino_port}/v1/status"
        ctx = None

    while time.time() < deadline:
        try:
            req = urllib.request.Request(status_url)
            if token:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=2, context=ctx) as response:
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
    from tests.e2e.docker_utils import get_bitcoin_rpc_port, get_directory_port

    btc_port = get_bitcoin_rpc_port()
    dir_port = get_directory_port()
    bitcoin_ok = is_bitcoin_running(port=btc_port)
    directory_ok = is_directory_server_running(port=dir_port)

    if not bitcoin_ok:
        logger.warning(f"Bitcoin Core not accessible on port {btc_port}")
    if not directory_ok:
        logger.warning(f"Directory server not accessible on port {dir_port}")

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


def _wait_for_maker_offers(min_offers: int = 2, timeout: float = 120.0) -> bool:
    """Poll the orderbook watcher until at least *min_offers* offers appear.

    Uses the HTTP ``/orderbook.json`` endpoint of the orderbook-watcher service
    which reflects offers that makers have actually published to the directory.
    This is a reliable readiness signal: an offer only appears once the maker
    has connected to the directory *and* broadcast its ``!orderbook`` response.

    Returns True when the condition is met, False on timeout.  Logs a warning on
    timeout but does not raise so callers can decide how to handle the situation.
    """
    import urllib.error
    import urllib.request

    from tests.e2e.docker_utils import get_orderbook_watcher_url

    url = f"{get_orderbook_watcher_url()}/orderbook.json"
    deadline = time.time() + timeout
    poll_interval = 2.0

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                n = len(data.get("offers", []))
                if n >= min_offers:
                    logger.info(
                        f"Orderbook watcher has {n} offers (>= {min_offers}), makers ready"
                    )
                    return True
                logger.debug(
                    f"Orderbook watcher has {n}/{min_offers} offers, waiting..."
                )
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            # Watcher may not be available in all test configurations
            pass
        time.sleep(poll_interval)

    logger.warning(
        f"Timed out after {timeout:.0f}s waiting for {min_offers} offer(s) "
        "in orderbook watcher. Proceeding anyway - test may skip."
    )
    return False


@pytest.fixture(scope="function")
def fresh_docker_makers():
    """Restart Docker makers before test to ensure fresh UTXOs.

    This fixture restarts the Docker maker containers to prevent UTXO reuse
    between tests, which can cause transaction verification failures.

    It also stops any non-e2e profile makers that might interfere with tests
    and clears commitment blacklists for all active makers.

    Instead of a fixed sleep, it polls the orderbook watcher until at least 2
    offers appear, which is the reliable signal that makers are connected to the
    directory and have published their offers.
    """

    from jmcore.paths import get_used_commitments_path

    from tests.e2e.docker_utils import (
        docker_exec,
        docker_inspect_running,
        get_container_name,
    )

    try:
        if not wait_for_neutrino_ready_if_present(timeout=180):
            logger.warning(
                "Neutrino service is reachable but not ready (height <= 0) before maker restart"
            )

        # Stop any non-e2e profile makers that might be running
        # This prevents stale offers from interfering with tests
        maker_container = get_container_name("maker")
        subprocess.run(
            ["docker", "stop", maker_container],
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

        # Clear commitment blacklists for all active e2e makers before restarting.
        # Includes maker4 and maker5 to prevent blacklist carry-over from prior tests.
        # Also clears wallet metadata (frozen UTXO state) to prevent the
        # auto-freeze-on-reuse feature from freezing the maker's UTXOs after
        # restart: on restart, utxo_cache starts empty but addresses_with_history
        # is loaded from the persisted metadata, causing all UTXOs at previously-
        # used addresses to be mis-classified as "forced reuse" and frozen.
        maker_services = [
            "maker1",
            "maker2",
            "maker3",
            "maker4",
            "maker5",
            "maker-neutrino",
        ]
        for service in maker_services:
            container = get_container_name(service)
            try:
                docker_exec(
                    container,
                    [
                        "sh",
                        "-c",
                        "rm -rf /home/jm/.joinmarket-ng/cmtdata/commitmentlist"
                        " /home/jm/.joinmarket-ng/wallet_metadata_*.jsonl",
                    ],
                    timeout=10,
                )
                logger.debug(
                    f"Cleared commitment blacklist and wallet metadata for {container}"
                )
            except Exception as e:
                logger.warning(f"Failed to clear state for {container}: {e}")

        # Restart the e2e profile makers (including neutrino maker for neutrino tests).
        # Only restart the non-heavy ones; maker4/maker5 are left running to keep
        # their UTXOs stable. Skip containers that are not running (e.g.
        # maker-neutrino is absent in the standard e2e profile) so that a
        # missing container does not cause docker restart to exit non-zero and
        # prevent the readiness wait from running.
        restart_services = ["maker1", "maker2", "maker3", "maker-neutrino"]
        restart_containers = [
            get_container_name(s)
            for s in restart_services
            if docker_inspect_running(get_container_name(s))
        ]
        if restart_containers:
            result = subprocess.run(
                ["docker", "restart", *restart_containers],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                logger.info(
                    f"Restarted {len(restart_containers)} maker(s), "
                    "polling orderbook watcher for readiness..."
                )
            else:
                logger.warning(f"Partial maker restart failure: {result.stderr}")
            _wait_for_maker_offers(min_offers=1, timeout=120)
        else:
            logger.warning("No maker containers found to restart")
    except subprocess.TimeoutExpired:
        logger.warning("Docker restart timed out")
    except FileNotFoundError:
        logger.warning("Docker command not found")
    except Exception as e:
        logger.warning(f"Could not restart makers: {e}")

    yield
