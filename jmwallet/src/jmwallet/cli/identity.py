"""Explicit wallet identity registration and selection, independent of recovery."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from jmcore.cli_common import resolve_mnemonic, select_mnemonic_source, setup_cli
from jmcore.cli_help import SortedTyper
from jmcore.settings import JoinMarketSettings
from jmcore.wallet_metadata import (
    UPGRADE_GUIDANCE,
    WalletIdentity,
    register_identity,
    registered_identities,
    select_identity,
    selected_identity,
)
from loguru import logger

from jmwallet.backends.descriptor_wallet import get_mnemonic_fingerprint
from jmwallet.cli import app

identity_app = SortedTyper(help="Remember public wallet identities, never passphrases.")
app.add_typer(identity_app, name="identity")


def confirm_and_register(
    settings: JoinMarketSettings,
    mnemonic_file: Path,
    *,
    prompt_bip39_passphrase: bool = False,
    no_bip39_passphrase: bool = False,
    confirm_bip39_passphrase: bool = False,
    select: bool = False,
    yes: bool = False,
    password: str | None = None,
) -> None:
    if no_bip39_passphrase and prompt_bip39_passphrase:
        raise ValueError("Choose either no passphrase or an interactive passphrase")
    resolved = resolve_mnemonic(
        settings,
        mnemonic_file=mnemonic_file,
        bip39_passphrase="" if no_bip39_passphrase else None,
        prompt_bip39_passphrase=prompt_bip39_passphrase,
        validate_identity=False,
        password=password,
    )
    assert resolved is not None
    passphrase = resolved.bip39_passphrase
    if confirm_bip39_passphrase and passphrase:
        repeated = typer.prompt("Repeat BIP39 passphrase", hide_input=True)
        # BIP39 specifies NFKD, not trimming or case folding.
        import unicodedata

        if unicodedata.normalize("NFKD", repeated) != unicodedata.normalize("NFKD", passphrase):
            raise ValueError("BIP39 passphrases do not match; identity was not registered")
    identity = WalletIdentity(
        fingerprint=get_mnemonic_fingerprint(resolved.mnemonic, passphrase),
        bip39="required" if passphrase else "none",
    )
    typer.echo(f"Wallet fingerprint: {identity.fingerprint}")
    typer.echo(f"Network: {settings.network_config.network}")
    typer.echo(f"BIP39 passphrase: {identity.bip39}")
    typer.echo(
        "Metadata remembers this public identity and passphrase status, never the passphrase."
    )
    if not yes:
        typer.confirm("Register this identity" + (" and select it?" if select else "?"), abort=True)
    register_identity(mnemonic_file, identity)
    if select:
        select_identity(mnemonic_file, identity.fingerprint)
    typer.echo("Identity registered" + (" and selected." if select else ". Selection unchanged."))


def _path(settings: JoinMarketSettings, mnemonic_file: Path | None) -> Path:
    source = select_mnemonic_source(settings, mnemonic_file=mnemonic_file)
    if source is None or source.path is None:
        raise ValueError("Identity registration requires a mnemonic file; use --mnemonic-file")
    return source.path


@identity_app.command("register")
def register(
    mnemonic_file: Annotated[Path | None, typer.Option("--mnemonic-file", "-f")] = None,
    prompt_bip39_passphrase: Annotated[bool, typer.Option("--prompt-bip39-passphrase")] = False,
    no_bip39_passphrase: Annotated[bool, typer.Option("--no-bip39-passphrase")] = False,
    confirm_bip39_passphrase: Annotated[bool, typer.Option("--confirm-bip39-passphrase")] = False,
    select: Annotated[
        bool, typer.Option("--select", help="Explicitly select the registered identity")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Confirm metadata disclosure noninteractively")
    ] = False,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
    config_file: Annotated[Path | None, typer.Option("--config-file")] = None,
) -> None:
    settings = setup_cli(None, data_dir=data_dir, config_file=config_file)
    try:
        confirm_and_register(
            settings,
            _path(settings, mnemonic_file),
            prompt_bip39_passphrase=prompt_bip39_passphrase,
            no_bip39_passphrase=no_bip39_passphrase,
            confirm_bip39_passphrase=confirm_bip39_passphrase,
            select=select,
            yes=yes,
        )
    except (OSError, ValueError) as exc:
        logger.error(str(exc))
        raise typer.Exit(1) from exc


@identity_app.command("list")
def list_identities(
    mnemonic_file: Annotated[Path | None, typer.Option("--mnemonic-file", "-f")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
    config_file: Annotated[Path | None, typer.Option("--config-file")] = None,
) -> None:
    settings = setup_cli(None, data_dir=data_dir, config_file=config_file)
    try:
        path = _path(settings, mnemonic_file)
        identities = registered_identities(path)
        # Listing also works before a default has been selected.
        from jmcore.wallet_metadata import load_mnemonic_meta

        selection = load_mnemonic_meta(path, strict=True).get("selected_identity")
        if selection is not None:
            selected_identity(path)  # Do not display an invalid default as valid.
        rows = [
            identity.model_dump() | {"selected": fp == selection}
            for fp, identity in identities.items()
        ]
        if json_output:
            typer.echo(json.dumps(rows))
        elif not rows:
            typer.echo(UPGRADE_GUIDANCE)
        else:
            for row in rows:
                typer.echo(
                    f"{row['fingerprint']}  BIP39: {row['bip39']}"
                    + ("  selected" if row["selected"] else "")
                )
    except (OSError, ValueError) as exc:
        logger.error(str(exc))
        raise typer.Exit(1) from exc


@identity_app.command("select")
def select(
    fingerprint: str,
    mnemonic_file: Annotated[Path | None, typer.Option("--mnemonic-file", "-f")] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
    config_file: Annotated[Path | None, typer.Option("--config-file")] = None,
) -> None:
    settings = setup_cli(None, data_dir=data_dir, config_file=config_file)
    try:
        select_identity(_path(settings, mnemonic_file), fingerprint)
    except (OSError, ValueError) as exc:
        logger.error(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Selected wallet identity: {fingerprint.strip().lower()}")
