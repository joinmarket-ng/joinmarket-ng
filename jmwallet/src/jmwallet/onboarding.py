"""Guided setup configuration, kept separate from wallet/node creation."""

from __future__ import annotations

import copy
import os
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

import typer
from click import Choice
from jmcore.config_file import ConfigFileError, ConfigScalar, update_config_values
from jmcore.constants import BITCOIN_RPC_PORTS
from jmcore.models import NetworkType
from jmcore.paths import get_default_data_dir
from jmcore.secure_files import read_sensitive_file
from jmcore.settings import validate_config_document
from pydantic import ValidationError

app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


class LightningMode(StrEnum):
    OFF = "off"
    MANAGED = "managed"
    MANUAL = "manual"


def _config_path(data_dir: Path | None, config_file: Path | None) -> Path:
    if config_file is not None:
        return config_file.expanduser().absolute()
    if data_dir is not None:
        return data_dir.expanduser().absolute() / "config.toml"
    override = os.environ.get("JOINMARKET_CONFIG_FILE")
    return (
        Path(override).expanduser().absolute()
        if override
        else get_default_data_dir() / "config.toml"
    )


def _contents(path: Path) -> bytes | None:
    try:
        return read_sensitive_file(path)
    except FileNotFoundError:
        return None


def _endpoint(value: str) -> tuple[str, int]:
    try:
        parsed = urlsplit("//" + value)
        if (
            not parsed.hostname
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        return parsed.hostname, parsed.port
    except ValueError:
        raise typer.BadParameter("Tor endpoints must be HOST:PORT or [IPv6]:PORT") from None


@app.command()
def setup(
    data_dir: Annotated[
        Path | None, typer.Option("--data-dir", help="Application data directory")
    ] = None,
    config_file: Annotated[
        Path | None, typer.Option("--config-file", help="Configuration file")
    ] = None,
    network: Annotated[
        NetworkType | None, typer.Option("--network", help="JoinMarket network")
    ] = None,
    bitcoin_network: Annotated[
        NetworkType | None,
        typer.Option("--bitcoin-network", help="Explicit blockchain-network override"),
    ] = None,
    taproot: Annotated[
        bool | None,
        typer.Option("--taproot/--no-taproot", help="Optional global Taproot pit switch"),
    ] = None,
    lightning: Annotated[
        LightningMode | None, typer.Option("--lightning", help="off, managed, or manual Lightning")
    ] = None,
    rpc_url: Annotated[
        str | None, typer.Option("--rpc-url", help="Bitcoin Core RPC endpoint")
    ] = None,
    rpc_cookie_file: Annotated[
        Path | None,
        typer.Option(
            "--rpc-cookie-file", help="Bitcoin Core cookie path (not read during configuration)"
        ),
    ] = None,
    tor_socks: Annotated[
        str | None, typer.Option("--tor-socks", help="Tor SOCKS HOST:PORT")
    ] = None,
    tor_control: Annotated[
        str | None, typer.Option("--tor-control", help="Tor control HOST:PORT")
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes", help="Accept configuration defaults; never implies fresh-wallet history"
        ),
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Validate and preview without writing")
    ] = False,
    configure_only: Annotated[
        bool,
        typer.Option("--configure-only", help="Configure only; do not create wallets or nodes"),
    ] = False,
) -> None:
    """Configure the standard setup without assuming wallet freshness.

    This stage creates no wallet or node and performs no backend or credential
    lookup. Existing values remain authoritative unless selected for editing.
    """
    path = _config_path(data_dir, config_file)
    try:
        target = path.resolve(strict=False)
        original = _contents(path)
        document = tomllib.loads(original.decode("utf-8")) if original is not None else {}
        defaults = validate_config_document(document)
    except (OSError, RuntimeError, ValueError, ValidationError):
        typer.echo("Existing configuration cannot be validated; no changes written.", err=True)
        raise typer.Exit(1) from None

    selected_network = network or defaults.network_config.network
    if network is None and not yes:
        selected_network = NetworkType(
            typer.prompt(
                "Network (mainnet/testnet/signet/regtest)",
                default=selected_network.value,
                type=Choice([item.value for item in NetworkType]),
            )
        )
    selected_taproot = taproot if taproot is not None else defaults.wallet.address_type == "p2tr"
    if taproot is None and not yes:
        selected_taproot = typer.confirm(
            "Enable the optional Taproot pit?", default=selected_taproot
        )
    selected_lightning = lightning or LightningMode(defaults.lightning.mode)
    if lightning is None and not selected_taproot:
        selected_lightning = LightningMode.OFF
    if lightning is None and not yes and selected_taproot:
        selected_lightning = LightningMode(
            typer.prompt(
                "Lightning (off/managed/manual)",
                default=selected_lightning.value,
                type=Choice([item.value for item in LightningMode]),
            )
        )

    updates: dict[str, dict[str, ConfigScalar]] = {
        "network_config": {"network": selected_network.value},
        "wallet": {"address_type": "p2tr" if selected_taproot else "p2wpkh"},
        "lightning": {"mode": selected_lightning.value},
    }
    if bitcoin_network is not None:
        updates["network_config"]["bitcoin_network"] = bitcoin_network.value
    chain = bitcoin_network or defaults.network_config.bitcoin_network or selected_network
    selected_url = rpc_url or defaults.bitcoin.rpc_url
    if rpc_url is None and "rpc_url" not in document.get("bitcoin", {}):
        selected_url = f"http://127.0.0.1:{BITCOIN_RPC_PORTS[chain.value]}"
    if rpc_url is None and not yes and defaults.bitcoin.backend_type != "neutrino":
        # Existing endpoints may contain inline authentication. Never display
        # that default or echo a replacement containing credentials.
        selected_url = typer.prompt(
            "Bitcoin Core RPC URL (hidden; Enter keeps current)",
            default=selected_url,
            show_default=False,
            hide_input=True,
        )
    updates["bitcoin"] = {"rpc_url": selected_url}
    if rpc_cookie_file is not None:
        updates["bitcoin"].update(
            rpc_cookie_file=str(rpc_cookie_file.expanduser().absolute()),
            rpc_user="",
            rpc_password="",
        )
    if tor_socks is not None:
        host, port = _endpoint(tor_socks)
        updates.setdefault("tor", {}).update(socks_host=host, socks_port=port)
    if tor_control is not None:
        host, port = _endpoint(tor_control)
        updates.setdefault("tor", {}).update(
            control_enabled=True, control_host=host, control_port=port
        )

    candidate = copy.deepcopy(document)
    for section, values in updates.items():
        candidate.setdefault(section, {}).update(values)
    try:
        planned = validate_config_document(candidate)
        if planned.lightning.mode == "managed" and (
            planned.bitcoin.backend_type != "descriptor_wallet" or not planned.tor.control_enabled
        ):
            raise ValueError("managed nodes require Core and Tor control")
        typer.echo(f"Wallet pit: {planned.wallet.address_type}")
        typer.echo(f"Lightning: {planned.lightning.mode}")
        typer.echo(f"Network: {planned.network_config.network.value}; blockchain: {chain.value}")
        if dry_run:
            typer.echo("Preview only; no files written.")
            return
        if not yes and not typer.confirm("Save this configuration?", default=False):
            typer.echo("No changes written.")
            return
        # Do not apply decisions based on a config edited during prompting.
        if path.resolve(strict=False) != target or _contents(path) != original:
            raise ConfigFileError("configuration changed while choosing setup options")
        update_config_values(path, updates, validate=validate_config_document)
    except (ConfigFileError, OSError, RuntimeError, ValueError, ValidationError):
        typer.echo(
            "Configuration could not be safely applied; "
            "rerun setup or review the selected settings.",
            err=True,
        )
        raise typer.Exit(1) from None
    typer.echo(f"Configuration saved: {path}")
    if configure_only:
        typer.echo("Configuration only; wallet and node state unchanged.")
        return
    typer.echo("Configuration stage complete; wallet and Lightning provisioning was not performed.")


def main(args: list[str] | None = None) -> None:
    app(args=args, prog_name="jm-ng setup")


if __name__ == "__main__":
    main()
