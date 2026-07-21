"""
Deposit-address management commands: reserve, label, release, and list.

These let CLI users (not only the jmwalletd HTTP API) hand out and set aside
deposit addresses. A reserved address is persisted in the BIP-329 metadata
store (``jm:reserved`` records), is never reissued as the next unused deposit
address, is hidden from the concise ``jm-wallet info`` view, and is shown with
its optional label in ``jm-wallet info --extended``.

Typical use case: give a distinct address to each of several payers and label
them, so none is accidentally reused.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from jmcore.cli_common import (
    ResolvedBackendSettings,
    resolve_backend_settings,
    resolve_mnemonic,
    setup_cli,
)
from loguru import logger

from jmwallet.cli import app

if TYPE_CHECKING:
    from jmwallet.wallet.service import WalletService

address_app = typer.Typer(
    name="address",
    help="Manage deposit addresses: reserve, label, release, and list.",
    no_args_is_help=True,
)
app.add_typer(address_app, name="address")


@dataclass
class _AddressContext:
    """Resolved options shared by all ``jm-wallet address`` subcommands."""

    mnemonic: str
    bip39_passphrase: str
    backend_settings: ResolvedBackendSettings
    creation_height: int | None
    max_sats_freeze_reuse: int
    reconstruct_history: bool


@address_app.callback()
def _address_main(
    ctx: typer.Context,
    mnemonic_file: Annotated[
        Path | None,
        typer.Option("--mnemonic-file", "-f", help="Path to mnemonic file", envvar="MNEMONIC_FILE"),
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool,
        typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase interactively"),
    ] = False,
    network: Annotated[str | None, typer.Option("--network", "-n", help="Bitcoin network")] = None,
    backend_type: Annotated[
        str | None,
        typer.Option("--backend", "-b", help="Backend: descriptor_wallet | neutrino"),
    ] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    neutrino_url: Annotated[
        str | None, typer.Option("--neutrino-url", envvar="NEUTRINO_URL")
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (defaults to <data-dir>/config.toml)",
        ),
    ] = None,
    log_level: Annotated[str | None, typer.Option("--log-level", "-l", help="Log level")] = None,
) -> None:
    """Resolve the wallet/backend once for every ``address`` subcommand."""
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if not resolved:
            raise ValueError("No mnemonic provided")
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    backend_settings = resolve_backend_settings(
        settings,
        network=network,
        backend_type=backend_type,
        rpc_url=rpc_url,
        neutrino_url=neutrino_url,
        data_dir=data_dir,
    )

    ctx.obj = _AddressContext(
        mnemonic=resolved.mnemonic,
        bip39_passphrase=resolved.bip39_passphrase,
        backend_settings=backend_settings,
        creation_height=resolved.creation_height,
        max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
        reconstruct_history=settings.wallet.reconstruct_history,
    )


async def _build_wallet(c: _AddressContext) -> tuple[WalletService, str]:
    """Construct a ``WalletService`` (offline; caller syncs if needed)."""
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.service import WalletService

    bs = c.backend_settings
    wallet_fingerprint = get_mnemonic_fingerprint(c.mnemonic, c.bip39_passphrase or "")

    backend: DescriptorWalletBackend | NeutrinoBackend
    if bs.backend_type == "neutrino":
        backend = NeutrinoBackend(
            neutrino_url=bs.neutrino_url,
            network=bs.network,
            scan_start_height=bs.scan_start_height,
            add_peers=bs.neutrino_add_peers,
            tls_cert_path=bs.neutrino_tls_cert,
            auth_token=bs.neutrino_auth_token,
        )
    elif bs.backend_type == "descriptor_wallet":
        backend = DescriptorWalletBackend(
            rpc_url=bs.rpc_url,
            rpc_user=bs.rpc_user,
            rpc_password=bs.rpc_password,
            wallet_name=generate_wallet_name(wallet_fingerprint, bs.network),
        )
    else:
        raise ValueError(f"Unknown backend type: {bs.backend_type}")

    if c.creation_height is not None:
        backend.set_wallet_creation_height(c.creation_height)

    wallet = WalletService(
        mnemonic=c.mnemonic,
        backend=backend,
        network=bs.network,
        mixdepth_count=5,
        passphrase=c.bip39_passphrase,
        data_dir=bs.data_dir,
        max_sats_freeze_reuse=c.max_sats_freeze_reuse,
        reconstruct_history=c.reconstruct_history,
    )
    return wallet, bs.backend_type


async def _sync_wallet(wallet: WalletService, backend_type: str) -> None:
    """Sync the wallet against the backend (needed to pick a fresh address)."""
    if backend_type == "neutrino":
        logger.info("Waiting for neutrino to sync...")
        if not await wallet.backend.wait_for_sync(timeout=300.0):
            logger.error("Neutrino sync timeout")
            raise typer.Exit(1)
    await wallet.sync_with_registered_bonds()


def _require_owned(wallet: WalletService, address: str) -> int:
    """Return the mixdepth of ``address`` or exit if it is not ours."""
    path = wallet._find_address_path(address)
    if path is None:
        logger.error(f"Address does not belong to this wallet: {address}")
        raise typer.Exit(1)
    return path[0]


@address_app.command("new")
def address_new(
    ctx: typer.Context,
    mixdepth: Annotated[int, typer.Argument(help="Mixdepth (0-4)")] = 0,
    label: Annotated[
        str, typer.Option("--label", "-l", help="Optional label to reserve the address under")
    ] = "",
) -> None:
    """Generate a fresh deposit address, reserve it, and optionally label it.

    The address is verified against the backend, persisted so it is never
    reissued, and (with --label) shown with its label in ``info --extended``.
    """
    asyncio.run(_address_new(ctx.obj, mixdepth, label))


async def _address_new(c: _AddressContext, mixdepth: int, label: str) -> None:
    wallet, backend_type = await _build_wallet(c)
    try:
        await _sync_wallet(wallet, backend_type)
        if mixdepth < 0 or mixdepth >= wallet.mixdepth_count:
            logger.error(f"Mixdepth must be 0-{wallet.mixdepth_count - 1}")
            raise typer.Exit(1)
        address = await wallet.get_new_address_verified(mixdepth)
        clean_label = label.strip()
        if clean_label:
            wallet.reserve_address(address, clean_label)
        print(address)
        if clean_label:
            print(f'Reserved under label "{clean_label}".', file=sys.stderr)
    finally:
        await wallet.close()


@address_app.command("label")
def address_label(
    ctx: typer.Context,
    address: Annotated[str, typer.Argument(help="Deposit address to reserve/label")],
    label: Annotated[str, typer.Argument(help="Label to attach (use quotes for spaces)")],
) -> None:
    """Reserve an existing deposit address and attach a label to it.

    Reserving hides the address from ``jm-wallet info``, shows it with the
    label in ``info --extended``, and prevents it from being reissued.
    """
    asyncio.run(_address_label(ctx.obj, address, label))


async def _address_label(c: _AddressContext, address: str, label: str) -> None:
    wallet, _ = await _build_wallet(c)
    try:
        md = _require_owned(wallet, address)
        wallet.reserve_address(address, label.strip())
        print(f'Reserved md{md} address {address} with label "{label.strip()}".')
    finally:
        await wallet.close()


@address_app.command("release")
def address_release(
    ctx: typer.Context,
    address: Annotated[str, typer.Argument(help="Reserved address to release")],
) -> None:
    """Remove a reservation/label so the address is no longer set aside.

    An address that has real on-chain history is still never reissued.
    """
    asyncio.run(_address_release(ctx.obj, address))


async def _address_release(c: _AddressContext, address: str) -> None:
    wallet, _ = await _build_wallet(c)
    try:
        if wallet.unreserve_address(address):
            print(f"Released reservation for {address}.")
        else:
            print(f"Address was not reserved: {address}.")
    finally:
        await wallet.close()


@address_app.command("list")
def address_list(ctx: typer.Context) -> None:
    """List reserved deposit addresses with their mixdepth and label."""
    asyncio.run(_address_list(ctx.obj))


async def _address_list(c: _AddressContext) -> None:
    wallet, _ = await _build_wallet(c)
    try:
        reserved = wallet.get_reserved_addresses()
        if not reserved:
            print("No reserved addresses.")
            return
        print(f"Reserved addresses ({len(reserved)}):")
        for address, label in sorted(reserved.items()):
            path = wallet._find_address_path(address)
            md = f"md{path[0]}" if path else "md?"
            label_str = f'  "{label}"' if label else ""
            print(f"  {md}  {address}{label_str}")
    finally:
        await wallet.close()
