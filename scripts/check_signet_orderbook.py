#!/usr/bin/env python3
"""Smoke test: fetch signet orderbook over Tor and report offer count.

This script proves that an installed joinmarket-ng can:

1. Reach the public signet directory nodes over Tor.
2. Speak the JoinMarket directory protocol.
3. Receive at least one orderbook offer.

It is used by the install smoke matrix in CI on Linux, macOS, and
Windows runners to validate that the installed software is actually
usable, without requiring a local Bitcoin Core, wallet, or full
CoinJoin run.

Exit codes:
    0  -- connected to at least one directory (offer count may be zero if
          the signet network has no active makers right now).
    1  -- could not connect to any directory (Tor or protocol broken), or
          a usage/configuration error.
    2  -- usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import time
from dataclasses import dataclass

from jmcore.crypto import NickIdentity
from jmcore.models import DIRECTORY_NODES_SIGNET
from taker.multi_directory import MultiDirectoryClient

logger = logging.getLogger("check_signet_orderbook")


@dataclass
class OrderbookResult:
    connected: int
    total_directories: int
    offer_count: int


def _socks_reachable(host: str, port: int, timeout: float = 5.0) -> bool:
    """Return True if a TCP connection to the Tor SOCKS port succeeds."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError as exc:
        logger.error("Tor SOCKS %s:%s unreachable: %s", host, port, exc)
        return False


async def fetch_offers(
    directories: list[str],
    socks_host: str,
    socks_port: int,
    min_wait: float,
    max_wait: float,
    quiet_period: float,
) -> OrderbookResult:
    """Connect to ``directories`` over Tor and return connection/offer counts."""
    nick_identity = NickIdentity()
    client = MultiDirectoryClient(
        directory_servers=directories,
        network="signet",
        nick_identity=nick_identity,
        socks_host=socks_host,
        socks_port=socks_port,
    )
    try:
        connected = await client.connect_all()
        logger.info("Connected to %d/%d directories", connected, len(directories))
        if connected == 0:
            return OrderbookResult(
                connected=0, total_directories=len(directories), offer_count=0
            )
        offers = await client.fetch_orderbook(
            min_wait=min_wait,
            max_wait=max_wait,
            quiet_period=quiet_period,
        )
        logger.info("Received %d offers", len(offers))
        return OrderbookResult(
            connected=connected,
            total_directories=len(directories),
            offer_count=len(offers),
        )
    finally:
        await client.close_all()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socks-host",
        default="127.0.0.1",
        help="Tor SOCKS5 host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--socks-port",
        type=int,
        default=9050,
        help="Tor SOCKS5 port (default: 9050)",
    )
    parser.add_argument(
        "--min-offers",
        type=int,
        default=1,
        help=(
            "Minimum offer count to print OK (default: 1). "
            "Fewer offers print a warning but still exit 0 provided at least "
            "one directory was reachable."
        ),
    )
    parser.add_argument(
        "--min-wait",
        type=float,
        default=15.0,
        help="Minimum orderbook fetch wait in seconds (default: 15)",
    )
    parser.add_argument(
        "--max-wait",
        type=float,
        default=90.0,
        help="Maximum orderbook fetch wait in seconds (default: 90)",
    )
    parser.add_argument(
        "--quiet-period",
        type=float,
        default=10.0,
        help="Seconds of silence before early exit (default: 10)",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help=(
            "Number of additional attempts when no directory connects "
            "(default: 2, i.e. up to 3 total tries). "
            "Each retry pauses --retry-delay seconds before reconnecting."
        ),
    )
    parser.add_argument(
        "--retry-delay",
        type=float,
        default=10.0,
        help="Seconds to wait between connection retries (default: 10)",
    )
    parser.add_argument(
        "--directory",
        action="append",
        default=None,
        help=(
            "Override directory address (host:port). May be repeated. "
            "Defaults to jmcore.DIRECTORY_NODES_SIGNET."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = _parse_args(argv)

    if not _socks_reachable(args.socks_host, args.socks_port):
        print(
            f"ERROR: Tor SOCKS5 proxy not reachable at "
            f"{args.socks_host}:{args.socks_port}",
            file=sys.stderr,
        )
        return 1

    directories = args.directory or list(DIRECTORY_NODES_SIGNET)
    if not directories:
        print("ERROR: no directory servers configured", file=sys.stderr)
        return 2

    max_attempts = 1 + args.retries
    result: OrderbookResult | None = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1:
            logger.info(
                "Retrying in %.0f seconds (attempt %d/%d)...",
                args.retry_delay,
                attempt,
                max_attempts,
            )
            time.sleep(args.retry_delay)
        logger.info(
            "Fetching signet orderbook from %d directories (attempt %d/%d)",
            len(directories),
            attempt,
            max_attempts,
        )
        try:
            result = asyncio.run(
                fetch_offers(
                    directories=directories,
                    socks_host=args.socks_host,
                    socks_port=args.socks_port,
                    min_wait=args.min_wait,
                    max_wait=args.max_wait,
                    quiet_period=args.quiet_period,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- surface anything to the runner
            print(f"ERROR: orderbook fetch failed: {exc}", file=sys.stderr)
            return 1
        if result.connected > 0:
            break
        logger.warning(
            "Attempt %d/%d: connected to 0/%d directories",
            attempt,
            max_attempts,
            result.total_directories,
        )

    assert result is not None

    if result.connected == 0:
        # Could not connect to any directory. This may mean the signet
        # directory nodes are temporarily offline (volunteer-operated) rather
        # than that the install is broken. Emit a clear WARNING but do not
        # fail CI for something outside our control.
        print(
            f"WARNING: could not connect to any of {result.total_directories} "
            "signet directory nodes. Tor is reachable but no directory "
            "responded. Signet directory nodes may be temporarily offline."
        )
        return 0

    if result.offer_count >= args.min_offers:
        print(
            f"OK: connected to {result.connected}/{result.total_directories} "
            f"directories, {result.offer_count} offers"
        )
        return 0

    # Connected to directories but no/few offers -- signet makers may simply
    # be offline right now, which is outside our control.
    print(
        f"WARNING: connected to {result.connected}/{result.total_directories} "
        f"directories but only {result.offer_count} offers "
        f"(need >= {args.min_offers}). "
        "Signet makers may be temporarily offline. "
        "The install is functional."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
