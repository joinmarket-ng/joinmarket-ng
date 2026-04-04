#!/usr/bin/env python3
"""Test orderbook watcher feature detection for a target maker nick."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "jmcore" / "src"))
sys.path.insert(0, str(REPO_ROOT / "orderbook_watcher" / "src"))

DEFAULT_DIRECTORY = (
    "nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion:5222"
)
DEFAULT_TARGET_NICK = "J57wPBk1VfjSP5Te"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", default="mainnet", help="Bitcoin network to use")
    parser.add_argument(
        "--directory",
        action="append",
        default=[],
        help=(
            "Directory server in host:port format. "
            "Can be passed multiple times. Defaults to the mainnet NG directory."
        ),
    )
    parser.add_argument(
        "--target-nick",
        default=DEFAULT_TARGET_NICK,
        help="Maker nick to search for in the orderbook",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=30,
        help="Seconds to wait for peerlist and offers before checking",
    )
    return parser.parse_args()


def parse_directory_nodes(host_ports: list[str]) -> list[tuple[str, int]]:
    nodes: list[tuple[str, int]] = []
    for host_port in host_ports:
        host, port = host_port.rsplit(":", 1)
        nodes.append((host, int(port)))
    return nodes


async def run(args: argparse.Namespace) -> None:
    from jmcore.settings import JoinMarketSettings
    from orderbook_watcher.aggregator import OrderbookAggregator

    settings = JoinMarketSettings(network=args.network)
    settings.network_config.directory_servers = args.directory or [DEFAULT_DIRECTORY]

    print(f"Using directories: {settings.get_directory_servers()}")
    directory_nodes = parse_directory_nodes(settings.get_directory_servers())

    aggregator = OrderbookAggregator(
        directory_nodes=directory_nodes,
        network=settings.network_config.network,
        socks_host=settings.tor.socks_host,
        socks_port=settings.tor.socks_port,
    )

    await aggregator.start_continuous_listening()
    try:
        print(f"\nWaiting {args.wait_seconds} seconds for connection and peerlist...")
        await asyncio.sleep(args.wait_seconds)

        orderbook = await aggregator.get_live_orderbook()
        print(
            f"\nCollected {len(orderbook.offers)} offers "
            f"from {len(aggregator.clients)} directories"
        )

        for offer in orderbook.offers:
            if offer.counterparty == args.target_nick:
                print(f"\nFound offer from {args.target_nick}:")
                print(f"  OrderID: {offer.oid}")
                print(f"  Type: {offer.ordertype}")
                print(f"  Features: {offer.features}")
                break
        else:
            print(f"\nMaker {args.target_nick} not found in orderbook")

        for node_str, client in aggregator.clients.items():
            print(f"\nDirectory {node_str}:")
            print(
                f"  peerlist_features_supported: {client.peerlist_features_supported}"
            )
            print(f"  peer_features cache size: {len(client.peer_features)}")
            if args.target_nick in client.peer_features:
                print(
                    f"  Features for {args.target_nick}: {client.peer_features[args.target_nick]}"
                )
    finally:
        await aggregator.stop_listening()


def main() -> None:
    args = parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
