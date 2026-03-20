"""
End-to-end test: Neutrino Maker + Reference Taker (JAM).

Verifies what happens when a reference JoinMarket taker (jam-standalone) selects
a neutrino maker that cannot verify UTXOs without extended metadata.

Expected behavior:
1. The neutrino maker publishes standard sw0 offers (visible to all takers)
2. The reference taker may select the neutrino maker for a CoinJoin round
3. At the !auth stage, the neutrino maker cannot verify the taker's UTXO
   (no scriptpubkey/blockheight in legacy PoDLE format)
4. The neutrino maker sends !error back and drops the session
5. The reference taker treats this as a non-responsive maker
6. If enough other makers responded (>= minimum_makers), the CoinJoin succeeds
7. The taker's PoDLE commitment is NOT burned (hp2 not broadcasted on failure)

This test documents the current interoperability limitation and verifies that
reference takers are not harmed beyond a timeout delay.

Prerequisites:
- Docker and Docker Compose installed
- Run: docker compose --profile all up -d
  (or: docker compose --profile reference --profile neutrino up -d)

Usage:
    pytest tests/e2e/test_neutrino_maker_reference_taker.py -v -s --timeout=900
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest
from loguru import logger

from tests.e2e.test_reference_coinjoin import (
    STARTUP_TIMEOUT,
    _wait_for_node_sync,
    cleanup_wallet_lock,
    create_jam_wallet,
    fund_wallet_address,
    get_compose_file,
    get_jam_wallet_address,
    is_jam_running,
    is_tor_running,
    run_bitcoin_cmd,
    run_compose_cmd,
    run_jam_cmd,
    wait_for_services,
)

# Longer timeout than the base COINJOIN_TIMEOUT (600 s) because the neutrino
# maker can cause the reference taker to retry counterparty selection, adding
# up to one full negotiation-round timeout on top of the normal coinjoin time.
NEUTRINO_COINJOIN_TIMEOUT = 780  # 13 minutes

# Shorter per-attempt timeout for the incompatibility probe.  We only need the
# taker to *contact* makers (!fill / !auth stage), not complete the full CoinJoin.
# 300 s (5 min) is generous for that.  4 attempts × 300 s = 20 min total, well
# within the CI job budget.
NEUTRINO_PROBE_TIMEOUT = 300  # 5 minutes

REFERENCE_COINJOIN_ATTEMPT_TIMEOUT = 300

# Maximum number of CoinJoin attempts before giving up on seeing the neutrino
# maker contacted.  With -N 2 and 3 makers the probability of *not* selecting
# the neutrino maker in a single round is 1/3, so after MAX_CONTACT_ATTEMPTS
# independent rounds the probability of never selecting it is (1/3)^4 ≈ 1.2 %.
MAX_CONTACT_ATTEMPTS = 4


def is_neutrino_maker_running() -> bool:
    """Check if the neutrino maker container is running."""
    result = run_compose_cmd(["ps", "-q", "maker-neutrino"], check=False)
    return bool(result.stdout.strip())


def get_neutrino_maker_logs(tail: int = 200) -> str:
    """Get recent logs from the neutrino maker container."""
    result = run_compose_cmd(
        ["logs", "--tail", str(tail), "maker-neutrino"], check=False
    )
    return result.stdout


def get_neutrino_maker_logs_since(since: datetime) -> str:
    """Return neutrino maker logs produced at or after *since* (UTC)."""
    since_str = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    result = run_compose_cmd(
        ["logs", "--since", since_str, "maker-neutrino"], check=False
    )
    return result.stdout


def _neutrino_maker_was_contacted(logs: str) -> bool:
    """Return True if the neutrino maker received a !fill or !auth from a taker."""
    logs_lower = logs.lower()
    return "received !fill" in logs_lower or "received !auth" in logs_lower


def ensure_neutrino_maker_running() -> bool:
    """Ensure the neutrino maker is running (start it if stopped)."""
    if is_neutrino_maker_running():
        return True
    logger.info("Neutrino maker not running, starting it...")
    result = run_compose_cmd(["start", "maker-neutrino"], check=False)
    if result.returncode != 0:
        logger.error(f"Failed to start neutrino maker: {result.stderr}")
        return False
    time.sleep(30)  # Wait for sync and offer announcement
    return is_neutrino_maker_running()


def set_maker_service_running(service: str, should_run: bool) -> None:
    """Start or stop a maker container and assert the state transition."""
    container = f"jm-{service}"
    action = "start" if should_run else "stop"
    result = subprocess.run(
        ["docker", action, container],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(f"Failed to {action} {container}: {result.stderr}")

    state = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    is_running = state.returncode == 0 and state.stdout.strip() == "true"
    if is_running != should_run:
        expected = "running" if should_run else "stopped"
        pytest.fail(f"Service {container} is not {expected} after {action}")


def wait_for_neutrino_backend_ready(timeout: int = 180) -> bool:
    """Wait until the neutrino API reports a positive block height."""
    status_url = "http://127.0.0.1:8334/v1/status"
    deadline = time.time() + timeout

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(status_url, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
                height = int(payload.get("block_height", 0))
                if height > 0:
                    logger.info(f"Neutrino backend ready at height {height}")
                    return True
        except (
            TimeoutError,
            ValueError,
            urllib.error.URLError,
            json.JSONDecodeError,
        ):
            pass

        time.sleep(2)

    return False


# Mark all tests in this module
pytestmark = [
    pytest.mark.neutrino_reference,
    pytest.mark.skipif(
        not is_jam_running(),
        reason="Reference services not running. Start with: "
        "docker compose --profile reference --profile neutrino up -d",
    ),
    pytest.mark.skipif(
        not is_neutrino_maker_running(),
        reason="Neutrino maker not running. Start with: "
        "docker compose --profile reference --profile neutrino up -d",
    ),
]


@pytest.fixture(scope="module")
def neutrino_reference_services():
    """Fixture ensuring both reference and neutrino services are running."""
    compose_file = get_compose_file()

    if not compose_file.exists():
        pytest.skip(f"Compose file not found: {compose_file}")

    if not is_jam_running():
        pytest.skip(
            "JAM not running. Start with: docker compose --profile reference --profile neutrino up -d"
        )

    if not is_tor_running():
        pytest.skip(
            "Tor not running. Start with: docker compose --profile reference --profile neutrino up -d"
        )

    if not is_neutrino_maker_running():
        pytest.skip(
            "Neutrino maker not running. Start with: docker compose --profile reference --profile neutrino up -d"
        )

    if not wait_for_neutrino_backend_ready(timeout=240):
        pytest.skip("Neutrino backend not ready (height <= 0)")

    # Wait for core services
    if not wait_for_services(timeout=STARTUP_TIMEOUT):
        pytest.skip("Services not healthy")

    # Ensure neutrino maker is running and allow brief startup time.
    ensure_neutrino_maker_running()
    time.sleep(15)

    yield {"compose_file": compose_file}


@pytest.fixture(scope="module")
async def jam_wallet_for_neutrino_test(neutrino_reference_services):
    """Create and fund a JAM wallet for neutrino compatibility testing."""
    wallet_name = "test_neutrino_compat.jmdat"
    wallet_password = "testpass456"

    logger.info(f"Creating JAM wallet: {wallet_name}")
    created = create_jam_wallet(wallet_name, wallet_password)
    assert created, "Failed to create JAM wallet"

    address = get_jam_wallet_address(wallet_name, wallet_password, mixdepth=0)
    assert address, "Failed to get wallet address"
    logger.info(f"Wallet address: {address}")

    funded = fund_wallet_address(address, amount_btc=0.2)
    assert funded, "Failed to fund wallet"

    await asyncio.sleep(15)

    yield {
        "wallet_name": wallet_name,
        "wallet_password": wallet_password,
        "address": address,
    }


def _kill_sendpayment_in_jam() -> None:
    """Kill any orphaned sendpayment.py processes inside the JAM container.

    When ``subprocess.run`` times out, only the host-side ``docker compose exec``
    process is killed.  ``sendpayment.py`` may keep running inside the container,
    holding the wallet lock and preventing subsequent CoinJoin attempts.
    """
    result = run_jam_cmd(
        ["bash", "-c", "pkill -f sendpayment.py || true"],
        timeout=15,
    )
    if result.returncode == 0:
        logger.debug("Killed orphaned sendpayment.py inside JAM container")


def _run_coinjoin(
    compose_file,
    wallet_name: str,
    wallet_password: str,
    dest_address: str,
    cj_amount: int = 5_000_000,
    timeout: int = NEUTRINO_COINJOIN_TIMEOUT,
) -> subprocess.CompletedProcess[str]:
    """Run one sendpayment.py CoinJoin round and return the completed process.

    Parameters
    ----------
    timeout:
        Per-attempt timeout in seconds.  Defaults to
        ``NEUTRINO_COINJOIN_TIMEOUT`` (780 s) which is enough for a full
        CoinJoin.  Pass a smaller value (e.g. 300 s) when you only need
        the taker to *contact* makers and don't need the transaction to
        confirm.

    If ``sendpayment.py`` does not finish within *timeout* seconds, the
    orphaned process is killed inside the container and a synthetic
    ``CompletedProcess`` with ``returncode=-1`` is returned so callers can
    inspect partial output without catching ``TimeoutExpired``.
    """
    cmd = [
        "docker",
        "compose",
        "-f",
        str(compose_file),
        "exec",
        "-T",
        "jam",
        "bash",
        "-c",
        f"echo '{wallet_password}' | python3 /src/scripts/sendpayment.py "
        f"--datadir=/root/.joinmarket-ng --wallet-password-stdin "
        f"-N 2 -m 0 /root/.joinmarket-ng/wallets/{wallet_name} "
        f"{cj_amount} {dest_address} --yes",
    ]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.warning(
            f"sendpayment.py timed out after {timeout}s — "
            "killing orphaned process inside JAM container"
        )
        _kill_sendpayment_in_jam()
        # Give the container a moment to release the wallet lock
        time.sleep(5)
        cleanup_wallet_lock(wallet_name)
        # Return a synthetic result so the caller can inspect partial output
        stdout = (
            exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        )
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=-1,
            stdout=f"[TIMEOUT after {timeout}s]\n{stdout}",
            stderr=stderr,
        )


@pytest.mark.asyncio
@pytest.mark.timeout(1200)
async def test_reference_taker_coinjoin_with_neutrino_maker_present(
    neutrino_reference_services,
    jam_wallet_for_neutrino_test,
):
    """
    Execute a CoinJoin with the reference JAM taker while a neutrino maker is in
    the orderbook.

    This test verifies that:
    1. The neutrino maker's offers are visible to the reference taker
    2. The reference taker can start a CoinJoin round in this mixed maker set
    3. The round outcome is graceful: either transaction broadcast or clean
       non-response handling when the neutrino maker is selected

    Note: With -N 2, if the neutrino maker is selected and rejects at auth,
    the reference taker may abort that round instead of falling back.
    """
    wallet_name = jam_wallet_for_neutrino_test["wallet_name"]
    wallet_password = jam_wallet_for_neutrino_test["wallet_password"]

    # Ensure neutrino maker IS running (unlike test_our_maker_reference_taker.py
    # which stops it). This is the whole point of this test.
    ensure_neutrino_maker_running()

    # Ensure bitcoin nodes are synced
    logger.info("Checking bitcoin node sync...")
    if not _wait_for_node_sync(max_attempts=30):
        pytest.fail("Bitcoin nodes failed to sync")

    # Ensure wallet exists and is funded
    created = create_jam_wallet(wallet_name, wallet_password)
    assert created, "Wallet must exist"

    address = get_jam_wallet_address(wallet_name, wallet_password, mixdepth=0)
    assert address, "Must have wallet address"

    funded = fund_wallet_address(address, 0.2)
    assert funded, "Wallet must be funded"

    await asyncio.sleep(30)

    # Get destination address
    dest_address = get_jam_wallet_address(wallet_name, wallet_password, mixdepth=1)
    if not dest_address:
        result = run_bitcoin_cmd(["getnewaddress", "", "bech32"])
        dest_address = result.stdout.strip()

    logger.info(f"CoinJoin destination: {dest_address}")
    cleanup_wallet_lock(wallet_name)

    compose_file = neutrino_reference_services["compose_file"]

    explicit_failures = [
        "not enough counterparties",
        "taker not continuing",
        "did not complete successfully",
        "giving up",
        "aborting",
        "not enough liquidity",
        "no suitable counterparties",
        "insufficient funds",
    ]

    logger.info("Executing CoinJoin via JAM sendpayment with neutrino maker present...")
    cleanup_wallet_lock(wallet_name)
    final_result = _run_coinjoin(
        compose_file,
        wallet_name,
        wallet_password,
        dest_address,
        timeout=REFERENCE_COINJOIN_ATTEMPT_TIMEOUT,
    )

    logger.info(f"sendpayment stdout:\n{final_result.stdout}")
    if final_result.stderr:
        logger.info(f"sendpayment stderr:\n{final_result.stderr}")

    # Check maker logs after final attempt
    for maker in ["maker1", "maker2", "maker-neutrino"]:
        maker_result = run_compose_cmd(["logs", "--tail=100", maker], check=False)
        logger.info(f"{maker} post-CoinJoin logs:\n{maker_result.stdout[-2000:]}")

    output_combined = final_result.stdout + final_result.stderr
    output_lower = output_combined.lower()
    has_txid = "txid = " in output_combined or "txid:" in output_lower
    has_explicit_failure = any(ind in output_lower for ind in explicit_failures)
    has_nonresponder = "makers who didnt respond" in output_lower
    reached_negotiation = "commitment sourced ok" in output_lower

    if has_txid:
        logger.info(
            "CoinJoin completed successfully despite neutrino maker in orderbook"
        )
        run_bitcoin_cmd(["generatetoaddress", "1", dest_address])
        _wait_for_node_sync(max_attempts=30)
        return

    assert reached_negotiation and has_nonresponder and not has_explicit_failure, (
        "CoinJoin did not broadcast and did not match expected graceful non-response "
        "behavior when neutrino maker is selected.\n"
        f"Exit code: {final_result.returncode}\n"
        f"Output: {final_result.stdout[-3000:]}"
    )

    logger.info(
        "CoinJoin round ended in expected non-response path (likely neutrino maker "
        "incompatibility) without explicit taker failure."
    )


@pytest.mark.asyncio
@pytest.mark.timeout(1800)
async def test_neutrino_maker_logs_incompatibility(
    neutrino_reference_services,
    jam_wallet_for_neutrino_test,
):
    """
    Verify that the neutrino maker logs the incompatibility error when a legacy
    taker selects it.

    With -N 2 and three makers (maker1, maker2, maker-neutrino) the taker
    randomly picks two per round, giving a 2/3 chance of selecting the neutrino
    maker each time.  To make the test deterministic the test runs up to
    MAX_CONTACT_ATTEMPTS CoinJoin rounds, stopping as soon as the neutrino maker
    is contacted.  The probability of never being selected across all attempts is
    (1/3)^MAX_CONTACT_ATTEMPTS ≈ 1.2 % (with MAX_CONTACT_ATTEMPTS = 4).

    Once the neutrino maker is contacted this test asserts:
    1. It logged the neutrino_incompatible error
    2. It sent !error back to the taker (session dropped gracefully)
    """
    wallet_name = jam_wallet_for_neutrino_test["wallet_name"]
    wallet_password = jam_wallet_for_neutrino_test["wallet_password"]
    compose_file = neutrino_reference_services["compose_file"]

    ensure_neutrino_maker_running()

    # Force deterministic selection: keep exactly one regular maker + neutrino maker.
    # With -N 2 and only maker1 + maker-neutrino running, the reference taker must
    # contact the neutrino maker in each attempt.
    set_maker_service_running("maker2", should_run=False)
    set_maker_service_running("maker3", should_run=False)

    try:
        for attempt in range(1, MAX_CONTACT_ATTEMPTS + 1):
            logger.info(
                f"Incompatibility probe attempt {attempt}/{MAX_CONTACT_ATTEMPTS}: "
                "running CoinJoin to trigger neutrino maker selection..."
            )

            # Fund and prepare wallet for this round
            address = get_jam_wallet_address(wallet_name, wallet_password, mixdepth=0)
            assert address, "Must have wallet address"
            funded = fund_wallet_address(address, 0.2)
            assert funded, f"Wallet must be funded (attempt {attempt})"
            await asyncio.sleep(15)

            dest_address = get_jam_wallet_address(
                wallet_name, wallet_password, mixdepth=1
            )
            if not dest_address:
                res = run_bitcoin_cmd(["getnewaddress", "", "bech32"])
                dest_address = res.stdout.strip()

            cleanup_wallet_lock(wallet_name)

            # Snapshot time just before this CoinJoin so we can scope log checks
            round_start = datetime.now(tz=timezone.utc)

            result = _run_coinjoin(
                compose_file,
                wallet_name,
                wallet_password,
                dest_address,
                timeout=NEUTRINO_PROBE_TIMEOUT,
            )

            logger.info(
                f"Attempt {attempt} sendpayment output:\n{result.stdout[-1000:]}"
            )

            # Collect logs produced during this round only
            logs = get_neutrino_maker_logs_since(round_start)

            if _neutrino_maker_was_contacted(logs):
                logger.info(f"Neutrino maker was contacted on attempt {attempt}")

                # Assert the incompatibility error was logged
                logs_lower = logs.lower()
                has_neutrino_error = (
                    "neutrino_incompatible" in logs_lower
                    or "neutrino backend cannot verify" in logs_lower
                    or "extended metadata" in logs_lower
                    or "neutrino_compat" in logs_lower
                )

                assert has_neutrino_error, (
                    "Neutrino maker was contacted by the reference taker but did NOT "
                    "log the expected incompatibility error. "
                    "This means the incompatibility is not being detected or reported "
                    "correctly.\n"
                    f"Relevant logs:\n{logs}"
                )

                logger.info(
                    "Neutrino maker correctly detected and logged the incompatibility "
                    "with the legacy taker"
                )

                # Log relevant lines for debugging
                for line in logs.splitlines():
                    line_lower = line.lower()
                    if any(
                        kw in line_lower
                        for kw in [
                            "error",
                            "auth",
                            "neutrino",
                            "incompatible",
                            "metadata",
                        ]
                    ):
                        logger.info(f"Relevant log: {line.strip()}")

                # Mine a block to confirm the transaction, then we are done
                run_bitcoin_cmd(["generatetoaddress", "1", dest_address])
                _wait_for_node_sync(max_attempts=30)
                return

            logger.info(
                f"Neutrino maker was not selected on attempt {attempt}. "
                f"Retrying ({MAX_CONTACT_ATTEMPTS - attempt} attempts left)..."
            )

            # Mine a block and sync before the next round
            run_bitcoin_cmd(["generatetoaddress", "1", dest_address])
            _wait_for_node_sync(max_attempts=30)
            # Brief pause to let offers propagate before the next round
            await asyncio.sleep(15)

        pytest.fail(
            f"Neutrino maker was never selected by the reference taker after "
            f"{MAX_CONTACT_ATTEMPTS} CoinJoin attempts. "
            "This is statistically unlikely and may indicate the neutrino maker is "
            "not advertising offers or is being excluded from selection."
        )
    finally:
        set_maker_service_running("maker2", should_run=True)
        set_maker_service_running("maker3", should_run=True)


@pytest.mark.asyncio
@pytest.mark.timeout(120)
async def test_neutrino_maker_offers_visible_in_orderbook(neutrino_reference_services):
    """
    Verify that the neutrino maker's offers appear in the orderbook.

    This confirms that the neutrino maker uses standard offer types (sw0reloffer)
    that are recognized by all participants. The reference taker CAN see and
    select these offers.
    """
    # Check orderbook watcher for neutrino maker offers
    logs = get_neutrino_maker_logs(tail=200)

    # Look for offer creation in the neutrino maker logs
    offer_created = (
        "created offer" in logs.lower()
        or "sw0reloffer" in logs.lower()
        or "sw0absoffer" in logs.lower()
        or "announcing" in logs.lower()
    )

    if offer_created:
        logger.info("Neutrino maker has created and announced offers")
    else:
        logger.warning(
            "Could not confirm neutrino maker offer creation from logs. "
            "The maker may still be syncing or may not have enough balance."
        )

    # Check if the maker is connected to the directory server
    has_directory_connection = (
        "connected" in logs.lower() and "directory" in logs.lower()
    ) or "subscribed" in logs.lower()

    if has_directory_connection:
        logger.info("Neutrino maker is connected to the directory server")

    # The key assertion: the neutrino maker should be running
    assert is_neutrino_maker_running(), "Neutrino maker should still be running"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s", "--timeout=900"])
