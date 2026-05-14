"""
CLI commands for directory server management.
"""

import argparse
import json
import sys
from urllib.error import URLError
from urllib.request import urlopen

from jmcore.cli_common import setup_logging
from jmcore.settings import get_settings


def format_status_output(stats: dict) -> str:
    lines = [
        "=== Directory Server Status ===",
        f"Network: {stats['network']}",
        f"Uptime: {stats['uptime_seconds']:.0f}s ({stats['uptime_seconds'] / 3600:.1f}h)",
        f"Status: {stats['server_status']}",
        f"Connected peers: {stats['connected_peers']['total']}/{stats['max_peers']}",
    ]

    if stats["connected_peers"]["nicks"]:
        lines.append(f"  Nicks: {', '.join(stats['connected_peers']['nicks'][:20])}")
        if len(stats["connected_peers"]["nicks"]) > 20:
            remaining = len(stats["connected_peers"]["nicks"]) - 20
            lines.append(f"  ... and {remaining} more")

    lines.extend(
        [
            f"Passive peers (orderbook watchers): {stats['passive_peers']['total']}",
        ]
    )

    if stats["passive_peers"]["nicks"]:
        lines.append(f"  Nicks: {', '.join(stats['passive_peers']['nicks'][:20])}")
        if len(stats["passive_peers"]["nicks"]) > 20:
            remaining = len(stats["passive_peers"]["nicks"]) - 20
            lines.append(f"  ... and {remaining} more")

    lines.extend(
        [
            f"Active peers (makers): {stats['active_peers']['total']}",
        ]
    )

    if stats["active_peers"]["nicks"]:
        lines.append(f"  Nicks: {', '.join(stats['active_peers']['nicks'][:20])}")
        if len(stats["active_peers"]["nicks"]) > 20:
            remaining = len(stats["active_peers"]["nicks"]) - 20
            lines.append(f"  ... and {remaining} more")

    lines.extend(
        [
            f"Active connections: {stats['active_connections']}",
        ]
    )

    # Add offer stats if available
    if "offers" in stats:
        offer_stats = stats["offers"]
        lines.extend(
            [
                "",
                "Offers:",
                f"  Total offers: {offer_stats['total_offers']}",
                f"  Peers with offers: {offer_stats['peers_with_offers']}",
            ]
        )

        # Show peers with more than 2 offers
        if offer_stats.get("peers_many_offers"):
            peers_many = offer_stats["peers_many_offers"]
            if peers_many:
                lines.append("  Peers with >2 offers:")
                for nick, count in peers_many:
                    lines.append(f"    {nick}: {count} offers")

    # Add rate limiter stats if available
    if "rate_limiter" in stats:
        rl_stats = stats["rate_limiter"]
        lines.extend(
            [
                "",
                "Rate Limiting:",
                f"  Tracked connections: {rl_stats['tracked_peers']}",
                f"  Total violations: {rl_stats['total_violations']}",
            ]
        )

        # Show top violators if any
        if rl_stats.get("top_violators"):
            top_violators = rl_stats["top_violators"][:5]  # Show top 5
            if top_violators:
                lines.append("  Top violators (by connection):")
                for conn_id, count in top_violators:
                    # Connection IDs are IP:port format - display as-is
                    lines.append(f"    {conn_id}: {count} violations")

    lines.append("===============================")

    return "\n".join(lines)


def status_command(args: argparse.Namespace) -> int:
    url = f"http://{args.host}:{args.port}/status"

    try:
        with urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode())

            if args.json:
                print(json.dumps(data, indent=2))
            else:
                print(format_status_output(data))

            return 0

    except URLError as e:
        print(f"Error: Could not connect to server at {url}", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as e:
        print("Error: Invalid JSON response from server", file=sys.stderr)
        print(f"Details: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def health_command(args: argparse.Namespace) -> int:
    url = f"http://{args.host}:{args.port}/health"

    try:
        with urlopen(url, timeout=5) as response:
            data = json.loads(response.read().decode())

            if args.json:
                print(json.dumps(data, indent=2))
            else:
                status = data.get("status", "unknown")
                print(f"Server status: {status}")

            return 0 if data.get("status") == "healthy" else 1

    except URLError:
        print("Error: Server unhealthy or unreachable", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def main() -> None:
    from jmcore.process_hardening import harden_current_process

    # Disable core dumps and ptrace before loading directory-node keys.
    harden_current_process()

    # Load settings to get defaults from config
    settings = get_settings()

    parser = argparse.ArgumentParser(description="JoinMarket Directory Server CLI")
    parser.add_argument(
        "--host",
        default=settings.directory_server.health_check_host,
        help=f"Health check server host (default: {settings.directory_server.health_check_host})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=settings.directory_server.health_check_port,
        help=f"Health check server port (default: {settings.directory_server.health_check_port})",
    )
    parser.add_argument(
        "--log-level",
        "-l",
        default=settings.logging.level,
        help=f"Log level (default: {settings.logging.level})",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    status_parser = subparsers.add_parser("status", help="Get server status")
    status_parser.add_argument("--json", action="store_true", help="Output as JSON")
    status_parser.set_defaults(func=status_command)

    health_parser = subparsers.add_parser("health", help="Check server health")
    health_parser.add_argument("--json", action="store_true", help="Output as JSON")
    health_parser.set_defaults(func=health_command)

    args = parser.parse_args()

    # Configure logging
    setup_logging(args.log_level)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
