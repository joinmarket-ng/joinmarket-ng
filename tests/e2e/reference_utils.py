"""
Shared utilities for reference implementation tests (JAM).

Delegates compose and container operations to ``docker_utils`` for
parallel-test-suite isolation support.
"""

from __future__ import annotations

import subprocess
import time

from loguru import logger

from tests.e2e.docker_utils import (
    get_compose_file,  # noqa: F401 – re-exported for existing callers
    run_compose_cmd,  # noqa: F401 – re-exported for existing callers
    run_container_cmd,  # noqa: F401 – re-exported for existing callers
)


def is_service_running(service: str) -> bool:
    """Check if a Docker service is running."""
    result = run_compose_cmd(["ps", "-q", service])
    return bool(result.stdout.strip())


def run_jam_cmd(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a command inside the jam container."""
    return run_container_cmd("jam", args, timeout)


def run_jam_maker_cmd(
    maker: str, args: list[str], timeout: int = 60
) -> subprocess.CompletedProcess[str]:
    """Run a command inside a jam maker container (e.g. jam-maker1)."""
    return run_container_cmd(maker, args, timeout)


def run_bitcoin_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a bitcoin-cli command on the main node."""
    return run_container_cmd(
        "bitcoin",
        [
            "bitcoin-cli",
            "-regtest",
            "-rpcuser=test",
            "-rpcpassword=test",
        ]
        + args,
    )


def run_bitcoin_jam_cmd(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a bitcoin-cli command against the bitcoin-jam node."""
    return run_container_cmd(
        "bitcoin-jam",
        [
            "bitcoin-cli",
            "-regtest",
            "-rpcuser=test",
            "-rpcpassword=test",
            "-rpcport=18445",
        ]
        + args,
    )


def cleanup_wallet_lock(container: str, wallet_name: str) -> None:
    """Remove stale wallet lock file if it exists."""
    lock_file = f"/root/.joinmarket-ng/wallets/.{wallet_name}.lock"
    result = run_container_cmd(container, ["rm", "-f", lock_file], timeout=10)
    if result.returncode == 0:
        logger.debug(f"Cleaned up lock file in {container}: {lock_file}")


def create_jam_wallet(
    container: str, wallet_name: str = "wallet.jmdat", password: str = "password"
) -> bool:
    """
    Create a wallet in a jam container using the expect script.
    """
    # Clean up any stale lock file
    cleanup_wallet_lock(container, wallet_name)

    # Check if wallet already exists
    result = run_container_cmd(
        container,
        ["ls", f"/root/.joinmarket-ng/wallets/{wallet_name}"],
        timeout=30,
    )
    if result.returncode == 0:
        logger.info(f"Wallet {wallet_name} already exists in {container}")
        return True

    # Check if expect is available
    result = run_container_cmd(container, ["which", "expect"], timeout=10)
    if result.returncode != 0:
        logger.info(f"Installing expect in {container}...")
        run_container_cmd(container, ["apt-get", "update"], timeout=60)
        run_container_cmd(container, ["apt-get", "install", "-y", "expect"], timeout=60)

    # Run the expect script to create wallet
    # Note: Ensure create_wallet.exp is mounted in the container
    logger.info(f"Creating wallet {wallet_name} in {container}...")
    result = run_container_cmd(
        container,
        ["expect", "/scripts/create_wallet.exp", password, wallet_name],
        timeout=120,
    )

    if result.returncode != 0:
        logger.error(f"Wallet creation failed in {container}: {result.stderr}")
        return False

    logger.info(f"Wallet created successfully: {wallet_name}")
    return True


def get_jam_wallet_address(
    container: str,
    wallet_name: str = "wallet.jmdat",
    password: str = "password",
    mixdepth: int = 0,
) -> str | None:
    """Get a receive address from jam wallet."""
    cleanup_wallet_lock(container, wallet_name)

    cmd = [
        "bash",
        "-c",
        f"echo '{password}' | python3 /src/scripts/wallet-tool.py "
        f"--datadir=/root/.joinmarket-ng "
        f"--wallet-password-stdin "
        f"/root/.joinmarket-ng/wallets/{wallet_name} display",
    ]

    result = run_container_cmd(container, cmd, timeout=60)

    if result.returncode != 0:
        logger.error(f"Failed to get wallet info from {container}: {result.stderr}")
        return None

    # Parse output to find address
    lines = result.stdout.split("\n")
    for line in lines:
        if f"/{mixdepth}'/0/" in line and (
            "new" in line.lower() or "addr" in line.lower()
        ):
            parts = line.split()
            for part in parts:
                if part.startswith("bcrt1") or part.startswith("bc1"):
                    return part

    # Fallback search
    for line in lines:
        if f"/{mixdepth}'/0/" in line:
            parts = line.split()
            for part in parts:
                if part.startswith("bcrt1") or part.startswith("bc1"):
                    return part

    return None


def fund_address(address: str, amount_btc: float = 1.0) -> bool:
    """Fund an address using the main bitcoin node's mining wallet."""
    funder_wallet = "fidelity_funder"

    # Check balance
    result = run_bitcoin_cmd(["-rpcwallet=" + funder_wallet, "getbalance"])
    if result.returncode != 0:
        # Try creating/loading funder wallet
        run_bitcoin_cmd(
            [
                "createwallet",
                funder_wallet,
                "false",
                "false",
                "",
                "false",
                "true",
                "true",
            ]
        )
        # Mine to it
        addr = run_bitcoin_cmd(
            ["-rpcwallet=" + funder_wallet, "getnewaddress"]
        ).stdout.strip()
        run_bitcoin_cmd(["generatetoaddress", "101", addr])
        result = run_bitcoin_cmd(["-rpcwallet=" + funder_wallet, "getbalance"])

    try:
        balance = float(result.stdout.strip())
    except ValueError:
        balance = 0.0

    if balance < amount_btc:
        # Mine more
        addr = run_bitcoin_cmd(
            ["-rpcwallet=" + funder_wallet, "getnewaddress"]
        ).stdout.strip()
        run_bitcoin_cmd(["generatetoaddress", "101", addr])

    # Send
    logger.info(f"Sending {amount_btc} BTC to {address}...")
    result = run_bitcoin_cmd(
        ["-rpcwallet=" + funder_wallet, "sendtoaddress", address, str(amount_btc)]
    )

    if result.returncode != 0:
        logger.error(f"Failed to send: {result.stderr}")
        return False

    # Mine confirmation
    addr = run_bitcoin_cmd(
        ["-rpcwallet=" + funder_wallet, "getnewaddress"]
    ).stdout.strip()
    run_bitcoin_cmd(["generatetoaddress", "1", addr])

    # Sync
    _wait_for_node_sync()
    return True


def _wait_for_node_sync(max_attempts: int = 30) -> bool:
    """Wait for nodes to sync."""
    for _ in range(max_attempts):
        r1 = run_bitcoin_cmd(["getblockcount"])
        r2 = run_bitcoin_jam_cmd(["getblockcount"])
        if r1.returncode == 0 and r2.returncode == 0:
            try:
                c1 = int(r1.stdout.strip())
                c2 = int(r2.stdout.strip())
                if c1 == c2:
                    return True
            except ValueError:
                pass
        time.sleep(1)
    return False
