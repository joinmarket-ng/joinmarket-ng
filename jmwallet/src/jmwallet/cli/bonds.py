"""
Fidelity bond commands: list-bonds, generate-bond-address, sync-bonds,
recover-bonds.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from jmcore.cli_common import (
    ResolvedBackendSettings,
    resolve_backend_settings,
    resolve_mnemonic,
    setup_cli,
)
from loguru import logger

from jmwallet.cli import app


@app.command()
def list_bonds(
    mnemonic_file: Annotated[
        Path | None,
        typer.Option(
            "--mnemonic-file",
            "-f",
            envvar="MNEMONIC_FILE",
            help=(
                "Select the per-wallet bond registry by deriving its "
                "fingerprint from this mnemonic file. This does NOT scan the "
                "blockchain; use 'jm-wallet recover-bonds' to discover bonds "
                "on-chain."
            ),
        ),
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
                "When neither --mnemonic-file nor this flag is provided and "
                "exactly one wallet has a registry in the data directory, that "
                "wallet is selected automatically."
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
    funded_only: Annotated[
        bool,
        typer.Option("--funded-only", help="Show only funded bonds"),
    ] = False,
    active_only: Annotated[
        bool,
        typer.Option("--active-only", help="Show only active bonds"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """
    List fidelity bonds from the local registry (offline, no blockchain access).

    This command only reads the per-wallet registry; it never scans the
    blockchain. Registered-but-unfunded bonds (created with
    generate-bond-address or import-bond but not yet funded) are shown with an
    UNFUNDED status. Funded status and values reflect the last on-chain sync.

    To refresh funded status from the blockchain, use 'jm-wallet sync-bonds'
    (fast, known bonds) or 'jm-wallet recover-bonds' (full discovery scan). The
    per-wallet registry is selected by the fingerprint derived from
    --mnemonic-file, taken from --wallet-fingerprint, the configured wallet, or
    auto-detected when only one wallet's registry exists in the data dir.
    """
    settings = setup_cli(log_level, data_dir=data_dir)
    resolved_data_dir = data_dir if data_dir else settings.get_data_dir()

    from jmwallet.cli._wallet_selection import resolve_wallet_fingerprint
    from jmwallet.wallet.bond_registry import list_registry_fingerprints

    fingerprint = resolve_wallet_fingerprint(
        settings,
        mnemonic_file=mnemonic_file,
        wallet_fingerprint=wallet_fingerprint,
        prompt_bip39_passphrase=prompt_bip39_passphrase,
        list_known_fingerprints=lambda: list_registry_fingerprints(resolved_data_dir),
        command_label="jm-wallet list-bonds",
        fall_back_to_configured_mnemonic=True,
    )
    if fingerprint is None:
        logger.error(
            "No bond registry found in this data directory and no wallet "
            "identity was provided. Pass --mnemonic-file (with "
            "--prompt-bip39-passphrase if needed) or --wallet-fingerprint."
        )
        raise typer.Exit(1)
    _list_bonds_offline(
        data_dir=resolved_data_dir,
        fingerprint=fingerprint,
        funded_only=funded_only,
        active_only=active_only,
        json_output=json_output,
    )


def _list_bonds_offline(
    data_dir: Path,
    fingerprint: str,
    funded_only: bool = False,
    active_only: bool = False,
    json_output: bool = False,
) -> None:
    """List bonds from the local registry without blockchain access."""
    from jmwallet.wallet.bond_registry import get_registry_path, load_registry

    registry = load_registry(data_dir, fingerprint)
    registry_path = get_registry_path(data_dir, fingerprint)

    if active_only:
        bonds = registry.get_active_bonds()
    elif funded_only:
        bonds = registry.get_funded_bonds()
    else:
        bonds = registry.bonds

    if json_output:
        import json

        output = [bond.model_dump() for bond in bonds]
        print(json.dumps(output, indent=2))
        return

    if not bonds:
        print("\nNo fidelity bonds found in registry.")
        print(f"Registry: {registry_path}")
        print(
            "\nTIP: Use 'jm-wallet generate-bond-address' to create one,\n"
            "     or 'jm-wallet recover-bonds' to scan the blockchain for "
            "existing bonds."
        )
        return

    print(f"\nFidelity Bonds ({len(bonds)} total)")
    print("=" * 120)
    header = f"{'Address':<64} {'Locktime':<20} {'Status':<15} {'Value':>15} {'Index':>6}"
    print(header)
    print("-" * 120)

    for bond in bonds:
        # Status
        if bond.is_funded and not bond.is_expired:
            status = "ACTIVE"
        elif bond.is_funded and bond.is_expired:
            status = "EXPIRED (funded)"
        elif bond.is_expired:
            status = "EXPIRED"
        else:
            status = "UNFUNDED"

        value_str = f"{bond.value:,} sats" if bond.value else "-"
        print(
            f"{bond.address:<64} {bond.locktime_human:<20} {status:<15} "
            f"{value_str:>15} {bond.index:>6}"
        )

    print("=" * 120)

    # Show best bond if any active
    best = registry.get_best_bond()
    if best:
        print(f"\nBest bond for advertising: {best.address[:20]}...{best.address[-8:]}")
        print(f"  Value: {best.value:,} sats, Unlock in: {best.time_until_unlock:,}s")

    print("\nNote: Values are from the last sync. Use 'jm-wallet sync-bonds' to refresh.")


@app.command("generate-bond-address")
def generate_bond_address(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    locktime: Annotated[
        int, typer.Option("--locktime", "-L", help="Locktime as Unix timestamp")
    ] = 0,
    locktime_date: Annotated[
        str | None,
        typer.Option("--locktime-date", "-d", help="Locktime as YYYY-MM (must be 1st of month)"),
    ] = None,
    network: Annotated[str | None, typer.Option("--network", "-n")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    no_save: Annotated[
        bool,
        typer.Option("--no-save", help="Do not save the bond to the registry"),
    ] = False,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """Generate a fidelity bond (timelocked P2WSH) address."""
    settings = setup_cli(log_level, data_dir=data_dir)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if not resolved:
            raise ValueError("No mnemonic provided")
        resolved_mnemonic = resolved.mnemonic
        resolved_bip39_passphrase = resolved.bip39_passphrase
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Resolve network from config if not provided
    resolved_network = network if network is not None else settings.network_config.network.value

    # Resolve data directory from config if not provided
    resolved_data_dir = data_dir if data_dir is not None else settings.get_data_dir()

    # Parse and validate locktime
    from jmcore.timenumber import is_valid_locktime, parse_locktime_date

    if locktime_date:
        try:
            # Use timenumber module for proper parsing and validation
            locktime = parse_locktime_date(locktime_date)
        except ValueError as e:
            logger.error(f"Invalid locktime date: {e}")
            logger.info("Use format: YYYY-MM or YYYY-MM-DD (must be 1st of month)")
            logger.info("Valid range: 2020-01 to 2099-12")
            raise typer.Exit(1)

    if locktime <= 0:
        logger.error("Locktime is required. Use --locktime or --locktime-date")
        raise typer.Exit(1)

    # Validate locktime is a valid timenumber (1st of month, midnight UTC)
    if not is_valid_locktime(locktime):
        from jmcore.timenumber import get_nearest_valid_locktime

        suggested = get_nearest_valid_locktime(locktime, round_up=True)
        suggested_dt = datetime.fromtimestamp(suggested)
        logger.warning(
            f"Locktime {locktime} is not a valid fidelity bond locktime "
            f"(must be 1st of month at midnight UTC)"
        )
        logger.info(f"Suggested locktime: {suggested} ({suggested_dt.strftime('%Y-%m-%d')})")
        logger.info("Use --locktime-date YYYY-MM for correct format")
        raise typer.Exit(1)

    # Validate locktime is in the future
    if locktime <= datetime.now().timestamp():
        logger.warning("Locktime is in the past - the bond will be immediately spendable")

    from jmcore.btc_script import disassemble_script, mk_freeze_script

    from jmwallet.wallet.address import script_to_p2wsh_address
    from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
    from jmwallet.wallet.bond_registry import (
        create_bond_info,
        get_registry_path,
        load_registry,
        make_wallet_ownership_predicate,
        migrate_legacy_registry,
        save_registry,
    )
    from jmwallet.wallet.service import FIDELITY_BOND_BRANCH

    seed = mnemonic_to_seed(resolved_mnemonic, resolved_bip39_passphrase)
    master_key = HDKey.from_seed(seed)
    wallet_fingerprint = master_key.derive("m/0").fingerprint.hex()

    coin_type = 0 if resolved_network == "mainnet" else 1
    root_path = f"m/84'/{coin_type}'"

    # Compute the timenumber from the locktime (this is the BIP32 child index)
    from jmcore.timenumber import timestamp_to_timenumber

    timenumber = timestamp_to_timenumber(locktime)
    path = f"{root_path}/0'/{FIDELITY_BOND_BRANCH}/{timenumber}"

    key = master_key.derive(path)
    pubkey_hex = key.get_public_key_bytes(compressed=True).hex()

    witness_script = mk_freeze_script(pubkey_hex, locktime)
    address = script_to_p2wsh_address(witness_script, resolved_network)

    locktime_dt = datetime.fromtimestamp(locktime)
    disassembled = disassemble_script(witness_script)

    # Save to registry unless --no-save
    saved = False
    existing = False
    if not no_save:
        # This command does not open a WalletService, so run the one-shot
        # legacy migration here to claim this wallet's own bonds out of the
        # shared registry. Then load with the legacy fallback disabled so
        # foreign bonds are never copied into this wallet's file (#492).
        migrate_legacy_registry(
            resolved_data_dir,
            wallet_fingerprint,
            make_wallet_ownership_predicate(master_key, root_path),
        )
        registry = load_registry(resolved_data_dir, wallet_fingerprint, allow_legacy_fallback=False)
        existing_bond = registry.get_bond_by_address(address)
        if existing_bond:
            existing = True
            logger.info(f"Bond already exists in registry (created: {existing_bond.created_at})")
        else:
            bond_info = create_bond_info(
                address=address,
                locktime=locktime,
                index=timenumber,
                path=path,
                pubkey_hex=pubkey_hex,
                witness_script=witness_script,
                network=resolved_network,
            )
            registry.add_bond(bond_info)
            save_registry(registry, resolved_data_dir, wallet_fingerprint)
            saved = True

    print("\n" + "=" * 80)
    print("FIDELITY BOND ADDRESS")
    print("=" * 80)
    print(f"\nAddress:      {address}")
    print(f"Locktime:     {locktime} ({locktime_dt.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"Timenumber:   {timenumber}")
    print(f"Network:      {resolved_network}")
    print(f"Path:         {path}")
    print()
    print("-" * 80)
    print("WITNESS SCRIPT (redeemScript)")
    print("-" * 80)
    print(f"Hex:          {witness_script.hex()}")
    print(f"Disassembled: {disassembled}")
    print("-" * 80)
    if saved:
        print(f"\nSaved to registry: {get_registry_path(resolved_data_dir, wallet_fingerprint)}")
    elif existing:
        print("\nBond already in registry (not updated)")
    elif no_save:
        print("\nNot saved to registry (--no-save)")
    print("\n" + "=" * 80)
    print("IMPORTANT: Funds sent to this address are LOCKED until the locktime!")
    print("           Make sure you have backed up your mnemonic.")
    print()
    print("WARNING: You should send coins to this address only once.")
    print("         Only the single biggest value UTXO will be announced")
    print("         as a fidelity bond. Sending coins multiple times will")
    print("         NOT increase fidelity bond value.")
    print("=" * 80 + "\n")


@app.command("import-bond")
def import_bond(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    locktime: Annotated[
        int, typer.Option("--locktime", "-L", help="Locktime as Unix timestamp")
    ] = 0,
    locktime_date: Annotated[
        str | None,
        typer.Option("--locktime-date", "-d", help="Locktime as YYYY-MM (must be 1st of month)"),
    ] = None,
    timenumber: Annotated[
        int | None,
        typer.Option("--timenumber", "-t", help="Timenumber (0-959). Auto-derived if omitted."),
    ] = None,
    path_spec: Annotated[
        str | None,
        typer.Option(
            "--path",
            "-p",
            help="Full derivation path with locktime, e.g. m/84'/0'/0'/2/73:1740787200",
        ),
    ] = None,
    network: Annotated[str | None, typer.Option("--network", "-n")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """
    Manually import a fidelity bond into the registry.

    Use this when you know the exact derivation path and locktime of a bond
    that was not discovered automatically. The bond address and keys are
    derived from your mnemonic.

    Examples:
        jm-wallet import-bond --locktime-date 2026-02
        jm-wallet import-bond --path "m/84'/0'/0'/2/73:1740787200"
        jm-wallet import-bond --timenumber 73
    """
    settings = setup_cli(log_level, data_dir=data_dir)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if not resolved:
            raise ValueError("No mnemonic provided")
        resolved_mnemonic = resolved.mnemonic
        resolved_bip39_passphrase = resolved.bip39_passphrase
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    resolved_network = network if network is not None else settings.network_config.network.value
    resolved_data_dir = data_dir if data_dir is not None else settings.get_data_dir()

    from jmcore.timenumber import (
        is_valid_locktime,
        parse_locktime_date,
        timenumber_to_timestamp,
        timestamp_to_timenumber,
    )

    # Parse --path if provided: m/84'/0'/0'/2/73:1740787200
    if path_spec:
        parts = path_spec.rstrip("/").split("/")
        last = parts[-1]
        if ":" in last:
            parsed_timenumber = int(last.split(":")[0])
            parsed_locktime = int(last.split(":")[1])
        else:
            parsed_timenumber = int(last)
            parsed_locktime = timenumber_to_timestamp(parsed_timenumber)

        if timenumber is None:
            timenumber = parsed_timenumber
        if locktime == 0:
            locktime = parsed_locktime

    # Parse --locktime-date
    if locktime_date:
        try:
            locktime = parse_locktime_date(locktime_date)
        except ValueError as e:
            logger.error(f"Invalid locktime date: {e}")
            raise typer.Exit(1)

    # Derive timenumber from locktime or vice versa
    if locktime > 0 and timenumber is None:
        timenumber = timestamp_to_timenumber(locktime)
    elif timenumber is not None and locktime == 0:
        locktime = timenumber_to_timestamp(timenumber)

    if locktime <= 0 or timenumber is None:
        logger.error("Must specify one of: --locktime, --locktime-date, --timenumber, or --path")
        raise typer.Exit(1)

    if not is_valid_locktime(locktime):
        logger.error(f"Locktime {locktime} is not a valid fidelity bond locktime")
        raise typer.Exit(1)

    # Verify consistency
    expected_timenumber = timestamp_to_timenumber(locktime)
    if timenumber != expected_timenumber:
        logger.error(
            f"Timenumber {timenumber} does not match locktime {locktime} "
            f"(expected timenumber {expected_timenumber})"
        )
        raise typer.Exit(1)

    from jmcore.btc_script import mk_freeze_script

    from jmwallet.wallet.address import script_to_p2wsh_address
    from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
    from jmwallet.wallet.bond_registry import (
        create_bond_info,
        get_registry_path,
        load_registry,
        make_wallet_ownership_predicate,
        migrate_legacy_registry,
        save_registry,
    )
    from jmwallet.wallet.service import FIDELITY_BOND_BRANCH

    seed = mnemonic_to_seed(resolved_mnemonic, resolved_bip39_passphrase)
    master_key = HDKey.from_seed(seed)
    wallet_fingerprint = master_key.derive("m/0").fingerprint.hex()

    coin_type = 0 if resolved_network == "mainnet" else 1
    root_path = f"m/84'/{coin_type}'"
    deriv_path = f"{root_path}/0'/{FIDELITY_BOND_BRANCH}/{timenumber}"

    key = master_key.derive(deriv_path)
    pubkey_hex = key.get_public_key_bytes(compressed=True).hex()
    witness_script = mk_freeze_script(pubkey_hex, locktime)
    address = script_to_p2wsh_address(witness_script, resolved_network)

    # Save to registry. This command does not open a WalletService, so run
    # the one-shot legacy migration here and load with the legacy fallback
    # disabled to avoid copying foreign bonds into this wallet's file (#492).
    migrate_legacy_registry(
        resolved_data_dir,
        wallet_fingerprint,
        make_wallet_ownership_predicate(master_key, root_path),
    )
    registry = load_registry(resolved_data_dir, wallet_fingerprint, allow_legacy_fallback=False)
    existing = registry.get_bond_by_address(address)
    if existing:
        print(f"\nBond already in registry (created: {existing.created_at})")
        print(f"Address: {address}")
        raise typer.Exit(0)

    bond_info = create_bond_info(
        address=address,
        locktime=locktime,
        index=timenumber,
        path=deriv_path,
        pubkey_hex=pubkey_hex,
        witness_script=witness_script,
        network=resolved_network,
    )
    registry.add_bond(bond_info)
    save_registry(registry, resolved_data_dir, wallet_fingerprint)

    from jmcore.timenumber import format_locktime_date

    locktime_str = format_locktime_date(locktime)
    print("\n" + "=" * 80)
    print("FIDELITY BOND IMPORTED")
    print("=" * 80)
    print(f"\nAddress:      {address}")
    print(f"Locktime:     {locktime} ({locktime_str})")
    print(f"Timenumber:   {timenumber}")
    print(f"Path:         {deriv_path}")
    print(f"Network:      {resolved_network}")
    print(f"\nSaved to: {get_registry_path(resolved_data_dir, wallet_fingerprint)}")
    print()
    print(
        "Next steps:\n"
        "  - If the bond was funded recently, run 'jm-wallet sync-bonds' to\n"
        "    refresh its on-chain value.\n"
        "  - If it was funded in an older block the wallet has not scanned yet,\n"
        "    run 'jm-wallet rescan' first so Bitcoin Core scans historical\n"
        "    blocks for this address; a plain sync only tracks new activity."
    )
    print("=" * 80 + "\n")


@app.command("sync-bonds")
def sync_bonds(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
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
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """
    Refresh funded status of bonds already in the registry (fast).

    Syncs only the bond addresses already recorded in the per-wallet registry
    and updates their on-chain UTXO info (value, confirmations). Unlike
    recover-bonds, this does NOT scan all 960 possible timelocks, so it is the
    quick way to reflect a funding transaction after creating a bond with
    generate-bond-address. Use recover-bonds instead when you need to discover
    bonds whose addresses are not yet in the registry.
    """
    settings = setup_cli(log_level, data_dir=data_dir)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if not resolved:
            raise ValueError("No mnemonic provided")
        resolved_mnemonic = resolved.mnemonic
        resolved_bip39_passphrase = resolved.bip39_passphrase
        resolved_creation_height = resolved.creation_height
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

    asyncio.run(
        _sync_bonds_async(
            resolved_mnemonic,
            backend_settings,
            resolved_bip39_passphrase,
            creation_height=resolved_creation_height,
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
        )
    )


async def _sync_bonds_async(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str = "",
    *,
    creation_height: int | None = None,
    max_sats_freeze_reuse: int = -1,
) -> None:
    """Sync only the bond addresses already present in the registry."""
    from jmcore.bitcoin import format_amount

    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.bond_registry import (
        get_registry_path,
        load_registry,
        save_registry,
    )
    from jmwallet.wallet.service import WalletService

    network = backend_settings.network
    data_dir = backend_settings.data_dir

    # Create backend based on type
    backend: DescriptorWalletBackend | NeutrinoBackend
    if backend_settings.backend_type == "neutrino":
        backend = NeutrinoBackend(
            neutrino_url=backend_settings.neutrino_url,
            network=network,
            scan_start_height=backend_settings.scan_start_height,
            add_peers=backend_settings.neutrino_add_peers,
            tls_cert_path=backend_settings.neutrino_tls_cert,
            auth_token=backend_settings.neutrino_auth_token,
        )
        logger.info("Waiting for neutrino to sync...")
        synced = await backend.wait_for_sync(timeout=300.0)
        if not synced:
            logger.error("Neutrino sync timeout")
            return
    elif backend_settings.backend_type == "descriptor_wallet":
        fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase)
        wallet_name = generate_wallet_name(fingerprint, network)
        backend = DescriptorWalletBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
            wallet_name=wallet_name,
        )
        await backend.create_wallet()
    else:
        raise ValueError(f"Unknown backend type: {backend_settings.backend_type}")

    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=5,
        passphrase=bip39_passphrase,
        data_dir=data_dir,
        max_sats_freeze_reuse=max_sats_freeze_reuse,
    )

    try:
        # Migration ran at wallet open; disable the legacy fallback so foreign
        # bonds are never copied into this wallet's file on save (#492).
        registry = load_registry(data_dir, wallet.wallet_fingerprint, allow_legacy_fallback=False)
        network_bonds = [bond for bond in registry.bonds if bond.network == network]

        if not network_bonds:
            print("\nNo fidelity bonds in the registry for this network to sync.")
            print("Use 'jm-wallet generate-bond-address' to create one,")
            print("or 'jm-wallet recover-bonds' to discover bonds on-chain.")
            return

        fidelity_bond_addresses = [
            (bond.address, bond.locktime, bond.index) for bond in network_bonds
        ]
        print(f"\nSyncing {len(fidelity_bond_addresses)} registered bond address(es)...")
        # Use the bond-aware sync so the bond's watch-only ``addr()`` descriptor
        # is imported into Bitcoin Core (and rescanned) when missing. A plain
        # ``sync_all`` only scans descriptors already imported, so a bond funded
        # after the base wallet was set up would never appear (issue: funded
        # fidelity bond shown as locked with 0 sats).
        await wallet.sync_with_registered_bonds()

        # Map each bond address to its highest-value UTXO. Per the reference
        # implementation only the single largest UTXO at an address is used.
        best_utxo_by_address: dict[str, Any] = {}
        for utxos_list in wallet.utxo_cache.values():
            for utxo in utxos_list:
                current = best_utxo_by_address.get(utxo.address)
                if current is None or utxo.value > current.value:
                    best_utxo_by_address[utxo.address] = utxo

        funded = 0
        for bond in network_bonds:
            bond_utxo = best_utxo_by_address.get(bond.address)
            if bond_utxo is not None:
                registry.update_utxo_info(
                    address=bond.address,
                    txid=bond_utxo.txid,
                    vout=bond_utxo.vout,
                    value=bond_utxo.value,
                    confirmations=bond_utxo.confirmations,
                )
                funded += 1

        save_registry(registry, data_dir, wallet.wallet_fingerprint)

        print("-" * 60)
        print(f"Funded bonds:   {funded}")
        print(f"Unfunded bonds: {len(network_bonds) - funded}")
        for bond in sorted(network_bonds, key=lambda b: b.locktime):
            bond_utxo = best_utxo_by_address.get(bond.address)
            status = format_amount(bond_utxo.value) if bond_utxo is not None else "UNFUNDED"
            print(f"  {bond.address}  {status}")
        print("-" * 60)
        print(f"Registry updated: {get_registry_path(data_dir, wallet.wallet_fingerprint)}")
        print("Run 'jm-wallet list-bonds' to view the refreshed registry.")

    finally:
        await wallet.close()


@app.command("recover-bonds")
def recover_bonds(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
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
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """
    Recover fidelity bonds by scanning all 960 possible timelocks.

    This command scans the blockchain for fidelity bonds at all valid
    timenumber locktimes (Jan 2020 through Dec 2099). Use this when
    recovering a wallet from mnemonic and you don't know which locktimes
    were used for fidelity bonds.

    Each timenumber (0-959) maps to exactly one address, matching the
    reference JoinMarket implementation.
    """
    settings = setup_cli(log_level, data_dir=data_dir)

    try:
        resolved = resolve_mnemonic(
            settings,
            mnemonic_file=mnemonic_file,
            prompt_bip39_passphrase=prompt_bip39_passphrase,
        )
        if not resolved:
            raise ValueError("No mnemonic provided")
        resolved_mnemonic = resolved.mnemonic
        resolved_bip39_passphrase = resolved.bip39_passphrase
        resolved_creation_height = resolved.creation_height
    except (FileNotFoundError, ValueError) as e:
        logger.error(str(e))
        raise typer.Exit(1)

    # Resolve backend settings
    backend_settings = resolve_backend_settings(
        settings,
        network=network,
        backend_type=backend_type,
        rpc_url=rpc_url,
        neutrino_url=neutrino_url,
        data_dir=data_dir,
    )

    asyncio.run(
        _recover_bonds_async(
            resolved_mnemonic,
            backend_settings,
            resolved_bip39_passphrase,
            creation_height=resolved_creation_height,
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
        )
    )


async def _recover_bonds_async(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str = "",
    *,
    creation_height: int | None = None,
    max_sats_freeze_reuse: int = -1,
) -> None:
    """Async implementation of fidelity bond recovery."""
    from jmcore.timenumber import TIMENUMBER_COUNT

    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.bond_registry import (
        create_bond_info,
        get_registry_path,
        load_registry,
        save_registry,
    )
    from jmwallet.wallet.service import FIDELITY_BOND_BRANCH, WalletService

    # Create backend based on type
    backend: DescriptorWalletBackend | NeutrinoBackend
    if backend_settings.backend_type == "neutrino":
        backend = NeutrinoBackend(
            neutrino_url=backend_settings.neutrino_url,
            network=backend_settings.network,
            scan_start_height=backend_settings.scan_start_height,
            add_peers=backend_settings.neutrino_add_peers,
            tls_cert_path=backend_settings.neutrino_tls_cert,
            auth_token=backend_settings.neutrino_auth_token,
        )
        logger.info("Waiting for neutrino to sync...")
        synced = await backend.wait_for_sync(timeout=300.0)
        if not synced:
            logger.error("Neutrino sync timeout")
            return
    elif backend_settings.backend_type == "descriptor_wallet":
        fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase)
        wallet_name = generate_wallet_name(fingerprint, backend_settings.network)
        backend = DescriptorWalletBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
            wallet_name=wallet_name,
        )
        # Must create/load wallet before importing descriptors
        await backend.create_wallet()
    else:
        raise ValueError(f"Unknown backend type: {backend_settings.backend_type}")

    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=backend_settings.network,
        mixdepth_count=5,
        passphrase=bip39_passphrase,
        data_dir=backend_settings.data_dir,
        max_sats_freeze_reuse=max_sats_freeze_reuse,
    )

    print("\nScanning for fidelity bonds...")
    print(f"Timelocks to scan: {TIMENUMBER_COUNT} (Jan 2020 - Dec 2099)")
    print(f"Total addresses: {TIMENUMBER_COUNT:,}")
    print("-" * 60)

    # Progress callbacks
    def progress_callback(current: int, total: int) -> None:
        percent = (current / total) * 100
        print(f"\rProgress: {current}/{total} addresses ({percent:.1f}%)...", end="", flush=True)

    def rescan_progress_callback(progress: float) -> None:
        print(f"\rRescan progress: {progress:.1%}...", end="", flush=True)

    try:
        # Discover fidelity bonds
        discovered_utxos = await wallet.discover_fidelity_bonds(
            progress_callback=progress_callback,
            rescan_progress_callback=rescan_progress_callback,
        )

        print()  # Newline after progress
        print("-" * 60)

        if not discovered_utxos:
            print("\nNo fidelity bonds found.")
            return

        # Group discovered UTXOs by address to handle multiple UTXOs at the
        # same bond address.  Per the reference implementation, only the single
        # biggest-value UTXO is used as a fidelity bond.
        from collections import defaultdict

        utxos_by_address: dict[str, list] = defaultdict(list)
        for utxo in discovered_utxos:
            utxos_by_address[utxo.address].append(utxo)

        print(
            f"\nDiscovered {len(utxos_by_address)} fidelity bond address(es) "
            f"({len(discovered_utxos)} UTXO(s) total):"
        )
        print()

        # Load registry and add discovered bonds. Migration ran at wallet
        # open, so disable the legacy fallback to avoid persisting foreign
        # bonds on save (#492).
        registry = load_registry(
            backend_settings.data_dir, wallet.wallet_fingerprint, allow_legacy_fallback=False
        )
        new_bonds = 0

        from jmcore.bitcoin import format_amount
        from jmcore.timenumber import format_locktime_date

        coin_type = 0 if backend_settings.network == "mainnet" else 1

        for address, addr_utxos in utxos_by_address.items():
            # Pick the largest UTXO by value
            best_utxo = max(addr_utxos, key=lambda u: u.value)

            # Extract timenumber and locktime from path
            # Path format: m/84'/coin'/0'/2/timenumber:locktime
            path_parts = best_utxo.path.split("/")
            index_locktime = path_parts[-1]
            if ":" in index_locktime:
                idx_str, locktime_str = index_locktime.split(":")
                idx = int(idx_str)
                locktime = int(locktime_str)
            else:
                idx = int(index_locktime)
                locktime = best_utxo.locktime or 0

            # Show discovered bond
            locktime_date_str = format_locktime_date(locktime) if locktime else "unknown"
            print(f"  Address:   {address}")
            print(f"  Value:     {format_amount(best_utxo.value)}")
            print(f"  Locktime:  {locktime_date_str}")
            print(f"  TXID:      {best_utxo.txid}:{best_utxo.vout}")
            if len(addr_utxos) > 1:
                total_sats = sum(u.value for u in addr_utxos)
                print(
                    f"  WARNING:   {len(addr_utxos)} UTXOs at this address "
                    f"(total {format_amount(total_sats)}). "
                    f"Only the largest UTXO is used as a fidelity bond."
                )
            print()

            # Check if already in registry
            existing = registry.get_bond_by_address(address)
            if existing:
                # Update UTXO info with the largest UTXO
                registry.update_utxo_info(
                    address=address,
                    txid=best_utxo.txid,
                    vout=best_utxo.vout,
                    value=best_utxo.value,
                    confirmations=best_utxo.confirmations,
                )
            else:
                # Add new bond to registry
                key = wallet.get_fidelity_bond_key(idx, locktime)
                pubkey_hex = key.get_public_key_bytes(compressed=True).hex()

                from jmcore.btc_script import mk_freeze_script

                witness_script = mk_freeze_script(pubkey_hex, locktime)
                path = f"m/84'/{coin_type}'/0'/{FIDELITY_BOND_BRANCH}/{idx}"

                bond_info = create_bond_info(
                    address=address,
                    locktime=locktime,
                    index=idx,
                    path=path,
                    pubkey_hex=pubkey_hex,
                    witness_script=witness_script,
                    network=backend_settings.network,
                )
                # Set UTXO info
                bond_info.txid = best_utxo.txid
                bond_info.vout = best_utxo.vout
                bond_info.value = best_utxo.value
                bond_info.confirmations = best_utxo.confirmations

                registry.add_bond(bond_info)
                new_bonds += 1

        # Save registry
        save_registry(registry, backend_settings.data_dir, wallet.wallet_fingerprint)

        print("-" * 60)
        print(f"Added {new_bonds} new bond(s) to registry")
        print(f"Updated {len(utxos_by_address) - new_bonds} existing bond(s)")
        print(
            f"Registry saved to: "
            f"{get_registry_path(backend_settings.data_dir, wallet.wallet_fingerprint)}"
        )

    finally:
        await wallet.close()
