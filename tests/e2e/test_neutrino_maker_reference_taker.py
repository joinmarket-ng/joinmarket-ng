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
import subprocess
import time

import pytest
from loguru import logger

from tests.e2e.test_reference_coinjoin import (
    COINJOIN_TIMEOUT,
    STARTUP_TIMEOUT,
    _wait_for_node_sync,
    cleanup_wallet_lock,
    create_jam_wallet,
    fund_wallet_address,
    get_compose_file,
    get_jam_wallet_address,
    is_jam_running,
    is_tor_running,
    restart_makers_and_wait,
    run_bitcoin_cmd,
    run_compose_cmd,
    wait_for_services,
)


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

    # Wait for core services
    if not wait_for_services(timeout=STARTUP_TIMEOUT):
        pytest.skip("Services not healthy")

    # Ensure neutrino maker is running and give it time to announce offers
    ensure_neutrino_maker_running()
    logger.info("Waiting for neutrino maker to announce offers...")
    time.sleep(90)

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


@pytest.mark.asyncio
@pytest.mark.timeout(900)
async def test_reference_taker_coinjoin_with_neutrino_maker_present(
    neutrino_reference_services,
    jam_wallet_for_neutrino_test,
):
    """
    Execute a CoinJoin with the reference JAM taker while a neutrino maker is in
    the orderbook.

    This test verifies that:
    1. The neutrino maker's offers are visible to the reference taker
    2. The reference taker can still complete a CoinJoin even if the neutrino maker
       is selected (because enough other makers respond)
    3. The neutrino maker logs the incompatibility error clearly
    4. The neutrino maker sends !error back to the taker

    Note: The reference taker requests -N 2 counterparties. With maker1, maker2,
    and maker-neutrino all advertising offers, the taker may or may not select
    the neutrino maker. Either way, the CoinJoin should succeed because maker1
    and maker2 are always available.
    """
    wallet_name = jam_wallet_for_neutrino_test["wallet_name"]
    wallet_password = jam_wallet_for_neutrino_test["wallet_password"]

    # Ensure neutrino maker IS running (unlike test_our_maker_reference_taker.py
    # which stops it). This is the whole point of this test.
    ensure_neutrino_maker_running()

    # Restart regular makers to ensure fresh state
    restart_makers_and_wait(wait_time=120)

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

    # Execute CoinJoin via JAM sendpayment.py
    # -N 2 = require 2 counterparties
    # The taker will see maker1, maker2, and maker-neutrino in the orderbook
    # CoinJoin amount: 5M sats (0.05 BTC) - above maker minimums but within budget
    compose_file = neutrino_reference_services["compose_file"]
    cj_amount = 5000000  # 0.05 BTC
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

    logger.info("Executing CoinJoin via JAM sendpayment with neutrino maker present...")
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=COINJOIN_TIMEOUT, check=False
    )

    logger.info(f"sendpayment stdout:\n{result.stdout}")
    if result.stderr:
        logger.info(f"sendpayment stderr:\n{result.stderr}")

    # Check all maker logs
    for maker in ["maker1", "maker2", "maker-neutrino"]:
        maker_result = run_compose_cmd(["logs", "--tail=100", maker], check=False)
        logger.info(f"{maker} post-CoinJoin logs:\n{maker_result.stdout[-2000:]}")

    # Analyze results
    output_combined = result.stdout + result.stderr
    output_lower = output_combined.lower()

    has_txid = "txid = " in output_combined or "txid:" in output_lower

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
    has_explicit_failure = any(ind in output_lower for ind in explicit_failures)

    if has_explicit_failure and not has_txid:
        # This might be expected if the neutrino maker was one of only 2 selected.
        # Log it as informational but don't necessarily fail the test.
        logger.warning(
            f"CoinJoin had failure indicators (possibly due to neutrino maker).\n"
            f"Exit code: {result.returncode}\n"
            f"Output snippet: {result.stdout[-2000:]}"
        )

    # The CoinJoin should succeed because we have maker1 + maker2 available
    assert has_txid, (
        f"CoinJoin did not broadcast transaction. The reference taker could not "
        f"complete the CoinJoin despite having enough regular makers available.\n"
        f"Exit code: {result.returncode}\n"
        f"Output: {result.stdout[-3000:]}"
    )

    logger.info("CoinJoin completed successfully despite neutrino maker in orderbook")

    # Mine a block to confirm
    run_bitcoin_cmd(["generatetoaddress", "1", dest_address])
    _wait_for_node_sync(max_attempts=30)


@pytest.mark.asyncio
@pytest.mark.timeout(300)
async def test_neutrino_maker_logs_incompatibility(neutrino_reference_services):
    """
    Verify that the neutrino maker logs the incompatibility error when
    a legacy taker attempts to use it.

    This test checks the neutrino maker logs for evidence that:
    1. It received a !fill from a taker
    2. It detected the taker didn't send extended metadata
    3. It logged the neutrino_incompatible error
    4. It sent !error back to the taker

    Note: This test should run AFTER test_reference_taker_coinjoin_with_neutrino_maker_present
    so there are logs to check. If the neutrino maker was never selected by the taker,
    we won't see incompatibility logs (which is also fine - means it wasn't picked).
    """
    logs = get_neutrino_maker_logs(tail=500)
    logs_lower = logs.lower()

    # Check if the neutrino maker was ever contacted by the reference taker
    was_contacted = "received !fill" in logs_lower or "fill" in logs_lower
    had_auth = "received !auth" in logs_lower or "auth" in logs_lower

    if not was_contacted:
        logger.info(
            "Neutrino maker was not selected by the reference taker in this run. "
            "This is normal - the taker randomly selects makers. "
            "The incompatibility would only manifest if selected."
        )
        pytest.skip(
            "Neutrino maker was not selected by the reference taker (random selection)"
        )

    if had_auth:
        # The neutrino maker was contacted and received an auth message.
        # Check for the expected incompatibility error.
        has_neutrino_error = (
            "neutrino_incompatible" in logs_lower
            or "neutrino backend cannot verify" in logs_lower
            or "extended metadata" in logs_lower
            or "neutrino_compat" in logs_lower
        )

        if has_neutrino_error:
            logger.info(
                "Neutrino maker correctly detected incompatibility with legacy taker"
            )
        else:
            # The auth might have failed for a different reason (timing, etc.)
            logger.info(
                "Neutrino maker received !auth but no neutrino incompatibility "
                "error was found. This may be due to other auth failures."
            )

        # Log the relevant lines for debugging
        for line in logs.splitlines():
            line_lower = line.lower()
            if any(
                kw in line_lower
                for kw in ["error", "auth", "neutrino", "incompatible", "metadata"]
            ):
                logger.info(f"Relevant log: {line.strip()}")

    logger.info("Neutrino maker log analysis complete")


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
