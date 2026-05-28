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
    0  -- fetched at least ``--min-offers`` offers (default 1).
    1  -- failed to reach any directory or zero offers received.
    2  -- usage error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys

from jmcore.crypto import NickIdentity
from jmcore.models import DIRECTORY_NODES_SIGNET
from taker.multi_directory import MultiDirectoryClient

logger = logging.getLogger("check_signet_orderbook")


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
) -> int:
    """Connect to ``directories`` over Tor and return total offer count."""
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
            return 0
        offers = await client.fetch_orderbook(
            min_wait=min_wait,
            max_wait=max_wait,
            quiet_period=quiet_period,
        )
        logger.info("Received %d offers", len(offers))
        return len(offers)
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
        help="Minimum offer count required for success (default: 1)",
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

    logger.info("Fetching signet orderbook from %d directories", len(directories))
    try:
        offer_count = asyncio.run(
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

    if offer_count >= args.min_offers:
        print(f"OK: signet orderbook reachable, {offer_count} offers")
        return 0

    print(
        f"ERROR: only {offer_count} offers (need >= {args.min_offers})",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
