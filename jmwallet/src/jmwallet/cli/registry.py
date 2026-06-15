"""
Bond registry command: registry-show.

registry-list and registry-sync have been merged into list-bonds
(see bonds.py).  list-bonds works in two modes:
  - Without --mnemonic-file: shows bonds from the local registry (offline).
  - With --mnemonic-file: scans the blockchain and updates the registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from jmcore.cli_common import setup_cli
from loguru import logger

from jmwallet.cli import app


@app.command("registry-show", no_args_is_help=True)
def registry_show(
    address: Annotated[str, typer.Argument(help="Bond address to show")],
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    wallet_fingerprint: Annotated[
        str | None,
        typer.Option(
            "--wallet-fingerprint",
            help=(
                "Select the per-wallet bond registry by its 8-char hex BIP32 "
                "master fingerprint. Use this instead of --mnemonic-file when "
                "you already know the fingerprint (e.g. from 'jm-wallet info'). "
                "When neither is provided and exactly one wallet has a "
                "registry in the data directory, that wallet is selected "
                "automatically."
            ),
        ),
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
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
    log_level: Annotated[str, typer.Option("--log-level", "-l")] = "WARNING",
) -> None:
    """Show detailed information about a specific fidelity bond."""
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

    from jmcore.btc_script import disassemble_script

    from jmwallet.cli._wallet_selection import resolve_wallet_fingerprint
    from jmwallet.wallet.bond_registry import (
        get_registry_path,
        list_registry_fingerprints,
        load_registry,
    )

    resolved_data_dir = data_dir if data_dir else settings.get_data_dir()

    # Per-wallet registry scoping (issue #492) requires a wallet identity
    # to know which file to read. Accept it via --mnemonic-file,
    # --wallet-fingerprint, or auto-detect when only one wallet is present.
    fingerprint = resolve_wallet_fingerprint(
        settings,
        mnemonic_file=mnemonic_file,
        wallet_fingerprint=wallet_fingerprint,
        prompt_bip39_passphrase=prompt_bip39_passphrase,
        list_known_fingerprints=lambda: list_registry_fingerprints(resolved_data_dir),
        command_label="jm-wallet registry-show",
        fall_back_to_configured_mnemonic=True,
    )
    if fingerprint is None:
        logger.error(
            "registry-show requires a wallet identity. Pass --mnemonic-file "
            "(with --prompt-bip39-passphrase if needed) or --wallet-fingerprint."
        )
        raise typer.Exit(1)

    registry = load_registry(resolved_data_dir, fingerprint)
    registry_path = get_registry_path(resolved_data_dir, fingerprint)

    bond = registry.get_bond_by_address(address)
    if not bond:
        print(f"\nBond not found: {address}")
        print(f"Registry: {registry_path}")
        raise typer.Exit(1)

    if json_output:
        import json

        print(json.dumps(bond.model_dump(), indent=2))
        return

    print("\n" + "=" * 80)
    print("FIDELITY BOND DETAILS")
    print("=" * 80)
    print(f"\nAddress:          {bond.address}")
    print(f"Network:          {bond.network}")
    print(f"Index:            {bond.index}")
    print(f"Path:             {bond.path}")
    print(f"Public Key:       {bond.pubkey}")
    print()
    print(f"Locktime:         {bond.locktime} ({bond.locktime_human})")
    if bond.is_expired:
        print("Status:           EXPIRED (can be spent)")
    else:
        remaining = bond.time_until_unlock
        days = remaining // 86400
        hours = (remaining % 86400) // 3600
        print(f"Status:           LOCKED ({days}d {hours}h remaining)")
    print()
    print("-" * 80)
    print("WITNESS SCRIPT")
    print("-" * 80)
    witness_script = bytes.fromhex(bond.witness_script_hex)
    print(f"Hex:          {bond.witness_script_hex}")
    print(f"Disassembled: {disassemble_script(witness_script)}")
    print()
    print("-" * 80)
    print("FUNDING STATUS")
    print("-" * 80)
    if bond.is_funded:
        print(f"TXID:         {bond.txid}")
        print(f"Vout:         {bond.vout}")
        print(f"Value:        {bond.value:,} sats")
        print(f"Confirmations: {bond.confirmations}")
    else:
        print("Not funded (or not yet synced)")
    print()
    print(f"Created:      {bond.created_at}")
    print("=" * 80 + "\n")
