"""Command-line owner for node-local channel-maintenance service."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from jmcore.channel_maintenance import ChannelMaintenanceConfig
from jmcore.channel_ring import ChannelRingConfig, ChannelRingSettings
from jmcore.cli_common import setup_cli
from jmcore.settings import JoinMarketSettings

from jmswap.channel_ring import InitializedChannelRingBackend, initialize_channel_ring_backend
from jmswap.lnd import LndBackend, LndNodeInfo
from jmswap.maintenance import (
    MaintenanceError,
    MaintenanceService,
    maintenance_status,
    reconcile_maintenance_admission,
    resolve_maintenance_payment,
)

_VALID_NETWORKS = frozenset({"mainnet", "testnet", "signet", "regtest"})
_SCID_ALIAS_FEATURE_BITS = frozenset({46, 47})


@dataclass(frozen=True)
class MaintenanceCommandConfig:
    """Fully resolved settings for one explicitly selected LND identity."""

    ring: ChannelRingConfig
    maintenance: ChannelMaintenanceConfig
    network: str
    offer_type: str
    directory: Path


def build_parser() -> argparse.ArgumentParser:
    """Build the maintenance command parser."""
    parser = argparse.ArgumentParser(description="Run node-local channel maintenance")
    parser.add_argument(
        "--config",
        type=Path,
        metavar="PATH",
        help="Config file path, overriding JOINMARKET_CONFIG_FILE",
    )
    parser.add_argument(
        "--role",
        choices=("maker", "taker"),
        default="maker",
        help="Role whose LND channel-ring settings identify this node (default: maker)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        metavar="PATH",
        help="Data directory override",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "status", "reconcile-admission", "resolve-payment"),
        default="run",
        help="Command to run (default: run)",
    )
    parser.add_argument(
        "--channel-point",
        dest="channel_points",
        action="append",
        metavar="TXID:VOUT",
        help="Existing enrolled channel point to reconcile (repeatable)",
    )
    parser.add_argument(
        "--confirm-idle",
        action="store_true",
        help=(
            "Attest that the operator reconciled both local spend registries and verified "
            "the channels are idle"
        ),
    )
    parser.add_argument(
        "--payment-hash",
        metavar="HASH",
        help="Exact current maintenance payment hash whose terminal outcome was verified",
    )
    parser.add_argument(
        "--terminal-status",
        choices=("succeeded", "failed"),
        help="Independently verified terminal payment status",
    )
    parser.add_argument(
        "--confirm-no-inflight",
        action="store_true",
        help=(
            "Attest that the operator verified the terminal outcome and that no HTLCs "
            "remain in flight"
        ),
    )
    return parser


def load_settings(config_path: Path | None, data_dir: Path | None) -> JoinMarketSettings:
    """Load settings through the shared CLI precedence and config-path handling."""
    return setup_cli(data_dir=data_dir, config_file=config_path)


def resolve_command_config(
    settings: JoinMarketSettings, role: Literal["maker", "taker"]
) -> MaintenanceCommandConfig:
    """Select exactly one role's private channel-ring identity and maintenance policy."""
    ring_settings: ChannelRingSettings
    offer_type: str
    if role == "maker":
        ring_settings = settings.maker.channel_ring
        offer_type = settings.maker.offer_type
    else:
        ring_settings = settings.taker.channel_ring
        offer_type = settings.taker.preferred_offer_type.value

    if not ring_settings.enabled:
        raise ValueError(f"{role} channel ring must be enabled")
    if not ring_settings.private_coordination:
        raise ValueError(f"{role} channel ring must enable private_coordination")

    network = settings.network_config.network.value
    if network not in _VALID_NETWORKS:
        raise ValueError(f"unsupported channel-ring network: {network}")

    directory = settings.get_data_dir()
    ring = ChannelRingConfig.from_settings(ring_settings, data_directory=directory)
    if ring.maintenance_directory is None:  # Defensive: private coordination resolves this default.
        raise ValueError("private channel ring requires a maintenance directory")

    return MaintenanceCommandConfig(
        ring=ring,
        maintenance=ChannelMaintenanceConfig.from_settings(settings.channel_maintenance),
        network=network,
        offer_type=offer_type,
        directory=ring.maintenance_directory,
    )


async def read_node_info(config: MaintenanceCommandConfig) -> LndNodeInfo:
    """Read and validate the configured LND identity without initializing maintenance."""
    if config.ring.lnd_tls_cert_path is None or config.ring.lnd_macaroon_path is None:
        raise ValueError("channel ring requires LND TLS certificate and macaroon paths")

    backend = LndBackend.from_paths(
        config.ring.lnd_grpc_url.removeprefix("https://"),
        config.ring.lnd_tls_cert_path,
        config.ring.lnd_macaroon_path,
        network=config.network,
    )
    try:
        info = await backend.node_info(timeout_seconds=config.ring.open_timeout_seconds)
        expected_uri = f"{info.identity_pubkey}@{config.ring.onion_endpoint}"
        if expected_uri not in info.advertised_uris:
            raise ValueError(
                "configured onion endpoint is not advertised by the configured LND node"
            )
        if not info.feature_bits.intersection(_SCID_ALIAS_FEATURE_BITS):
            raise ValueError("private channel ring requires LND option-scid-alias support")
        return info
    finally:
        await backend.close()


async def run_maintenance(config: MaintenanceCommandConfig) -> None:
    """Initialize one validated backend, run its service, and always close it."""
    initialized: InitializedChannelRingBackend = await initialize_channel_ring_backend(
        config.ring,
        network=config.network,
        offer_type=config.offer_type,
    )
    service: MaintenanceService | None = None
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    try:
        service = MaintenanceService(
            initialized.backend,
            config.maintenance,
            config.directory,
            initialized.node_info.identity_pubkey,
        )
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, service.stop)
                installed_signals.append(signum)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - platform dependent.
                pass
        await service.run()
    finally:
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        if service is None:
            await initialized.backend.close()
        else:
            await service.close()


async def show_status(config: MaintenanceCommandConfig) -> dict[str, object]:
    """Return state after a read-only LND identity and alias validation."""
    node_info = await read_node_info(config)
    return maintenance_status(config.directory, node_info.identity_pubkey)


async def reconcile_admission(config: MaintenanceCommandConfig, channel_points: list[str]) -> None:
    """Record explicit operator admission only after a read-only identity check."""
    node_info = await read_node_info(config)
    reconcile_maintenance_admission(
        config.directory,
        node_info.identity_pubkey,
        channel_points,
        confirmed_idle=True,
    )


async def resolve_payment(
    config: MaintenanceCommandConfig,
    payment_hash: str,
    terminal_status: Literal["succeeded", "failed"],
) -> None:
    """Record one explicit terminal payment resolution after identity validation."""
    node_info = await read_node_info(config)
    now = int(time.time())
    resolve_maintenance_payment(
        config.directory,
        node_info.identity_pubkey,
        payment_hash,
        terminal_status,
        confirmed_no_inflight=True,
        recorded_at=now,
        next_due=now + config.maintenance.interval_min_seconds,
    )


def validate_operator_arguments(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject incomplete or misplaced operator attestations before backend access."""
    admission_options_used = args.channel_points is not None or args.confirm_idle
    payment_options_used = (
        args.payment_hash is not None
        or args.terminal_status is not None
        or args.confirm_no_inflight
    )
    if args.command == "reconcile-admission":
        if payment_options_used:
            parser.error("resolve-payment options cannot be used with reconcile-admission")
        if not args.channel_points:
            parser.error("reconcile-admission requires at least one --channel-point")
        if not args.confirm_idle:
            parser.error(
                "reconcile-admission requires --confirm-idle after reconciling both local "
                "spend registries and verifying channels are idle"
            )
    elif args.command == "resolve-payment":
        if admission_options_used:
            parser.error("reconcile-admission options cannot be used with resolve-payment")
        if args.payment_hash is None:
            parser.error("resolve-payment requires --payment-hash")
        if args.terminal_status is None:
            parser.error("resolve-payment requires --terminal-status")
        if not args.confirm_no_inflight:
            parser.error(
                "resolve-payment requires --confirm-no-inflight after verifying the terminal "
                "outcome and no HTLCs remain in flight"
            )
    elif admission_options_used or payment_options_used:
        parser.error(
            "operator reconciliation options require reconcile-admission or resolve-payment"
        )


def main(argv: list[str] | None = None) -> int:
    """Run the maintenance command and return a conventional process status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_operator_arguments(parser, args)
    try:
        settings = load_settings(args.config, args.data_dir)
        config = resolve_command_config(settings, args.role)
        if args.command == "status":
            print(json.dumps(asyncio.run(show_status(config)), sort_keys=True))
        elif args.command == "reconcile-admission":
            asyncio.run(reconcile_admission(config, cast(list[str], args.channel_points)))
            print(json.dumps({"status": "admission_reconciled"}, sort_keys=True))
        elif args.command == "resolve-payment":
            asyncio.run(
                resolve_payment(
                    config,
                    cast(str, args.payment_hash),
                    cast(Literal["succeeded", "failed"], args.terminal_status),
                )
            )
            print(json.dumps({"status": "payment_resolved"}, sort_keys=True))
        else:
            asyncio.run(run_maintenance(config))
    except KeyboardInterrupt:
        return 130
    except (MaintenanceError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
