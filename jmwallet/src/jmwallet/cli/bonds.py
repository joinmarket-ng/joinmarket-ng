"""
Fidelity bond commands: list-bonds, generate-bond-address, recover-bonds.
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
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    network: Annotated[str | None, typer.Option("--network", "-n", help="Bitcoin network")] = None,
    backend_type: Annotated[
        str | None,
        typer.Option(
            "--backend", "-b", help="Backend: scantxoutset | descriptor_wallet | neutrino"
        ),
    ] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    locktimes: Annotated[
        list[int] | None, typer.Option("--locktime", "-L", help="Locktime(s) to scan for")
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    funded_only: Annotated[
        bool,
        typer.Option("--funded-only", help="Show only funded bonds (offline mode)"),
    ] = False,
    active_only: Annotated[
        bool,
        typer.Option("--active-only", help="Show only active bonds (offline mode)"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON (offline mode)"),
    ] = False,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """
    List all fidelity bonds in the wallet.

    Without --mnemonic-file: shows bonds from the local registry (offline, fast).
    With --mnemonic-file: scans the blockchain for bonds and updates the registry.
    """
    settings = setup_cli(log_level)

    # If no mnemonic provided, show bonds from registry (offline mode)
    if mnemonic_file is None and not any(v is not None for v in [rpc_url, backend_type]):
        _list_bonds_offline(
            data_dir=data_dir or settings.get_data_dir(),
            funded_only=funded_only,
            active_only=active_only,
            json_output=json_output,
        )
        return

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

    # Resolve backend settings with CLI overrides taking priority
    backend = resolve_backend_settings(
        settings,
        network=network,
        backend_type=backend_type,
        rpc_url=rpc_url,
        data_dir=data_dir,
    )

    asyncio.run(
        _list_fidelity_bonds(
            resolved_mnemonic,
            backend,
            locktimes or [],
            resolved_bip39_passphrase,
        )
    )


def _list_bonds_offline(
    data_dir: Path,
    funded_only: bool = False,
    active_only: bool = False,
    json_output: bool = False,
) -> None:
    """List bonds from the local registry without blockchain access."""
    from jmwallet.wallet.bond_registry import load_registry

    registry = load_registry(data_dir)

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
        print(f"Registry: {data_dir / 'fidelity_bonds.json'}")
        print(
            "\nTIP: Use --mnemonic-file to scan the blockchain for bonds,\n"
            "     or 'jm-wallet generate-bond-address' to create one."
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

    print("\nNote: Values are from last sync. Use --mnemonic-file to refresh from blockchain.")


async def _list_fidelity_bonds(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    locktimes: list[int],
    bip39_passphrase: str = "",
) -> None:
    """List fidelity bonds implementation."""
    from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
    from jmwallet.wallet.bond_registry import (
        FidelityBondInfo as RegistryBondInfo,
    )
    from jmwallet.wallet.bond_registry import (
        load_registry,
        save_registry,
    )
    from jmwallet.wallet.service import FIDELITY_BOND_BRANCH, WalletService

    # Import fidelity bond utilities from maker
    try:
        from maker.fidelity import find_fidelity_bonds
    except ImportError:
        logger.error("Failed to import fidelity bond utilities")
        raise typer.Exit(1)

    network = backend_settings.network
    data_dir = backend_settings.data_dir

    backend = BitcoinCoreBackend(
        rpc_url=backend_settings.rpc_url,
        rpc_user=backend_settings.rpc_user,
        rpc_password=backend_settings.rpc_password,
    )

    # Use large gap limit (1000) for discovery mode when scanning with --locktime
    gap_limit = 1000 if locktimes else 20
    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=network,
        mixdepth_count=5,
        passphrase=bip39_passphrase,
        data_dir=data_dir,
    )

    # Verify metadata store is writable before syncing (fail fast on read-only mounts)
    if wallet.metadata_store is not None:
        try:
            wallet.metadata_store.verify_writable()
        except OSError as e:
            logger.error(f"Cannot run freeze command: {e}")
            raise typer.Exit(1)
    else:
        logger.error("Cannot freeze UTXOs without a data directory")
        raise typer.Exit(1)

    try:
        # Load known bonds from registry for optimized scanning
        bond_registry = load_registry(data_dir)
        fidelity_bond_addresses: list[tuple[str, int, int]] = []
        network_bonds = [bond for bond in bond_registry.bonds if bond.network == network]
        if network_bonds:
            fidelity_bond_addresses = [
                (bond.address, bond.locktime, bond.index) for bond in network_bonds
            ]
            logger.info(
                f"Loading {len(fidelity_bond_addresses)} known bond(s) from registry for scanning"
            )

        # Sync wallet + known bonds in single pass
        await wallet.sync_all(fidelity_bond_addresses)

        # If user provided locktimes, also scan with large gap limit to discover new bonds
        if locktimes:
            logger.info(f"Scanning for undiscovered bonds with gap_limit={gap_limit}")
            await wallet.sync_fidelity_bonds(locktimes)

        bonds = await find_fidelity_bonds(wallet)

        if not bonds:
            print("\nNo fidelity bonds found in wallet.")
            if not locktimes:
                print("TIP: Use --locktime to specify locktime(s) to scan for undiscovered bonds")
                print(
                    "     Or use 'jm-wallet generate-bond-address' to create a new bond "
                    "and register it"
                )
            return

        # Group bonds by address to detect multiple UTXOs at the same address.
        # Per the reference implementation, only the single highest-value UTXO
        # at each address is used as a fidelity bond.
        from collections import defaultdict

        bonds_by_address: dict[str, list] = defaultdict(list)
        utxo_map_by_outpoint: dict[tuple[str, int], Any] = {}
        for utxos_list in wallet.utxo_cache.values():
            for utxo in utxos_list:
                utxo_map_by_outpoint[(utxo.txid, utxo.vout)] = utxo

        for bond in bonds:
            utxo = utxo_map_by_outpoint.get((bond.txid, bond.vout))
            addr = utxo.address if utxo else f"unknown-{bond.txid}:{bond.vout}"
            bonds_by_address[addr].append(bond)

        # For each address, select the best UTXO (highest bond_value)
        best_bonds: list = []
        for addr, addr_bonds in bonds_by_address.items():
            best = max(addr_bonds, key=lambda b: b.bond_value)
            best_bonds.append((addr, best, addr_bonds))

        # Sort by bond value (highest first)
        best_bonds.sort(key=lambda x: x[1].bond_value, reverse=True)

        print(f"\nFound {len(best_bonds)} fidelity bond(s):\n")
        print("=" * 120)

        from jmcore.bitcoin import format_amount

        # Track registry updates
        registry_updated = False
        coin_type = 0 if network == "mainnet" else 1

        for i, (addr, bond, all_addr_bonds) in enumerate(best_bonds, 1):
            locktime_dt = datetime.fromtimestamp(bond.locktime)
            expired = datetime.now().timestamp() > bond.locktime
            status = "EXPIRED" if expired else "ACTIVE"
            print(f"Bond #{i}: [{status}]")
            print(f"  UTXO:        {bond.txid}:{bond.vout}")
            print(f"  Value:       {format_amount(bond.value)}")
            print(f"  Locktime:    {bond.locktime} ({locktime_dt.strftime('%Y-%m-%d %H:%M:%S')})")
            print(f"  Confirms:    {bond.confirmation_time}")
            print(f"  Bond Value:  {bond.bond_value:,}")
            if len(all_addr_bonds) > 1:
                total_sats = sum(b.value for b in all_addr_bonds)
                print(
                    f"  WARNING:     {len(all_addr_bonds)} UTXOs at this address "
                    f"(total {format_amount(total_sats)}). "
                    f"Only the largest UTXO is used as a fidelity bond."
                )
            print("-" * 120)

            # Update registry with the best UTXO for this address
            utxo_info = utxo_map_by_outpoint.get((bond.txid, bond.vout))
            if utxo_info:
                existing_bond = bond_registry.get_bond_by_address(addr)
                if existing_bond:
                    if bond_registry.update_utxo_info(
                        address=addr,
                        txid=bond.txid,
                        vout=bond.vout,
                        value=bond.value,
                        confirmations=utxo_info.confirmations,
                    ):
                        registry_updated = True
                        logger.debug(f"Updated registry entry for {addr[:20]}...")
                elif locktimes:
                    # New bond discovered via --locktime scan, add to registry
                    path_parts = utxo_info.path.split("/")
                    index_locktime = path_parts[-1]
                    idx = int(index_locktime.split(":")[0]) if ":" in index_locktime else 0

                    from jmcore.btc_script import mk_freeze_script

                    key = wallet.get_fidelity_bond_key(idx, bond.locktime)
                    pubkey_hex = key.get_public_key_bytes(compressed=True).hex()
                    witness_script = mk_freeze_script(pubkey_hex, bond.locktime)
                    path = f"m/84'/{coin_type}'/0'/{FIDELITY_BOND_BRANCH}/{idx}"

                    from jmcore.timenumber import format_locktime_date

                    new_bond = RegistryBondInfo(
                        address=addr,
                        locktime=bond.locktime,
                        locktime_human=format_locktime_date(bond.locktime),
                        index=idx,
                        path=path,
                        pubkey=pubkey_hex,
                        witness_script_hex=witness_script.hex(),
                        network=network,
                        created_at=datetime.now().isoformat(),
                        txid=bond.txid,
                        vout=bond.vout,
                        value=bond.value,
                        confirmations=utxo_info.confirmations,
                    )
                    bond_registry.add_bond(new_bond)
                    registry_updated = True
                    logger.info(f"Added new bond to registry: {addr[:20]}...")

        # Save registry if any updates were made
        if registry_updated:
            save_registry(bond_registry, data_dir)
            print(f"\nRegistry updated: {data_dir / 'fidelity_bonds.json'}")

    finally:
        await wallet.close()


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
    index: Annotated[int, typer.Option("--index", "-i", help="Address index")] = 0,
    network: Annotated[str | None, typer.Option("--network", "-n")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
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
    settings = setup_cli(log_level)

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
        load_registry,
        save_registry,
    )
    from jmwallet.wallet.service import FIDELITY_BOND_BRANCH

    seed = mnemonic_to_seed(resolved_mnemonic, resolved_bip39_passphrase)
    master_key = HDKey.from_seed(seed)

    coin_type = 0 if resolved_network == "mainnet" else 1
    path = f"m/84'/{coin_type}'/0'/{FIDELITY_BOND_BRANCH}/{index}"

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
        registry = load_registry(resolved_data_dir)
        existing_bond = registry.get_bond_by_address(address)
        if existing_bond:
            existing = True
            logger.info(f"Bond already exists in registry (created: {existing_bond.created_at})")
        else:
            bond_info = create_bond_info(
                address=address,
                locktime=locktime,
                index=index,
                path=path,
                pubkey_hex=pubkey_hex,
                witness_script=witness_script,
                network=resolved_network,
            )
            registry.add_bond(bond_info)
            save_registry(registry, resolved_data_dir)
            saved = True

    print("\n" + "=" * 80)
    print("FIDELITY BOND ADDRESS")
    print("=" * 80)
    print(f"\nAddress:      {address}")
    print(f"Locktime:     {locktime} ({locktime_dt.strftime('%Y-%m-%d %H:%M:%S')})")
    print(f"Index:        {index}")
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
        print(f"\nSaved to registry: {resolved_data_dir / 'fidelity_bonds.json'}")
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
        typer.Option(
            "--backend", "-b", help="Backend: scantxoutset | descriptor_wallet | neutrino"
        ),
    ] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    neutrino_url: Annotated[
        str | None, typer.Option("--neutrino-url", envvar="NEUTRINO_URL")
    ] = None,
    max_index: Annotated[
        int,
        typer.Option(
            "--max-index", "-i", help="Max address index per locktime to scan (default 1)"
        ),
    ] = 1,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
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

    The scan checks address index 0 by default (most wallets only use index 0).
    Use --max-index to scan more addresses per locktime if needed.
    """
    settings = setup_cli(log_level)

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
            max_index,
            resolved_bip39_passphrase,
        )
    )


async def _recover_bonds_async(
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    max_index: int,
    bip39_passphrase: str = "",
) -> None:
    """Async implementation of fidelity bond recovery."""
    from jmcore.timenumber import TIMENUMBER_COUNT

    from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.bond_registry import (
        create_bond_info,
        load_registry,
        save_registry,
    )
    from jmwallet.wallet.service import FIDELITY_BOND_BRANCH, WalletService

    # Create backend based on type
    backend: BitcoinCoreBackend | DescriptorWalletBackend | NeutrinoBackend
    if backend_settings.backend_type == "neutrino":
        backend = NeutrinoBackend(
            neutrino_url=backend_settings.neutrino_url, network=backend_settings.network
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
        backend = BitcoinCoreBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
        )

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=backend_settings.network,
        mixdepth_count=5,
        passphrase=bip39_passphrase,
        data_dir=backend_settings.data_dir,
    )

    print("\nScanning for fidelity bonds...")
    print(f"Timelocks to scan: {TIMENUMBER_COUNT} (Jan 2020 - Dec 2099)")
    print(f"Addresses per timelock: {max_index}")
    print(f"Total addresses: {TIMENUMBER_COUNT * max_index:,}")
    print("-" * 60)

    # Progress callback
    def progress_callback(current: int, total: int) -> None:
        percent = (current / total) * 100
        print(f"\rProgress: {current}/{total} timelocks ({percent:.1f}%)...", end="", flush=True)

    try:
        # Discover fidelity bonds
        discovered_utxos = await wallet.discover_fidelity_bonds(
            max_index=max_index,
            progress_callback=progress_callback,
        )

        print()  # Newline after progress
        print("-" * 60)

        if not discovered_utxos:
            print("\nNo fidelity bonds found.")
            print("If you expected to find bonds, try increasing --max-index")
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

        # Load registry and add discovered bonds
        registry = load_registry(backend_settings.data_dir)
        new_bonds = 0

        from jmcore.bitcoin import format_amount
        from jmcore.timenumber import format_locktime_date

        coin_type = 0 if backend_settings.network == "mainnet" else 1

        for address, addr_utxos in utxos_by_address.items():
            # Pick the largest UTXO by value
            best_utxo = max(addr_utxos, key=lambda u: u.value)

            # Extract index and locktime from path
            # Path format: m/84'/coin'/0'/2/index:locktime
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
        save_registry(registry, backend_settings.data_dir)

        print("-" * 60)
        print(f"Added {new_bonds} new bond(s) to registry")
        print(f"Updated {len(utxos_by_address) - new_bonds} existing bond(s)")
        print(f"Registry saved to: {backend_settings.data_dir / 'fidelity_bonds.json'}")

    finally:
        await wallet.close()
