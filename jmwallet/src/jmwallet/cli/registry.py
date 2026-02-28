"""
Bond registry commands: registry-list, registry-show, registry-sync.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from jmcore.cli_common import (
    ResolvedBackendSettings,
    resolve_backend_settings,
    resolve_mnemonic,
    setup_cli,
    setup_logging,
)
from loguru import logger

from jmwallet.cli import app

if TYPE_CHECKING:
    from jmwallet.wallet.bond_registry import BondRegistry


@app.command("registry-list")
def registry_list(
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    funded_only: Annotated[
        bool,
        typer.Option("--funded-only", "-f", help="Show only funded bonds"),
    ] = False,
    active_only: Annotated[
        bool,
        typer.Option("--active-only", "-a", help="Show only active (funded & not expired) bonds"),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
    log_level: Annotated[str, typer.Option("--log-level", "-l")] = "WARNING",
) -> None:
    """List all fidelity bonds in the registry."""
    setup_logging(log_level)

    from jmcore.paths import get_default_data_dir

    from jmwallet.wallet.bond_registry import load_registry

    resolved_data_dir = data_dir if data_dir else get_default_data_dir()
    registry = load_registry(resolved_data_dir)

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
        print(f"Registry: {resolved_data_dir / 'fidelity_bonds.json'}")
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

        # Value
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


@app.command("registry-show")
def registry_show(
    address: Annotated[str, typer.Argument(help="Bond address to show")],
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", "-j", help="Output as JSON"),
    ] = False,
    log_level: Annotated[str, typer.Option("--log-level", "-l")] = "WARNING",
) -> None:
    """Show detailed information about a specific fidelity bond."""
    setup_logging(log_level)

    from jmcore.btc_script import disassemble_script
    from jmcore.paths import get_default_data_dir

    from jmwallet.wallet.bond_registry import load_registry

    resolved_data_dir = data_dir if data_dir else get_default_data_dir()
    registry = load_registry(resolved_data_dir)

    bond = registry.get_bond_by_address(address)
    if not bond:
        print(f"\nBond not found: {address}")
        print(f"Registry: {resolved_data_dir / 'fidelity_bonds.json'}")
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


@app.command("registry-sync")
def registry_sync(
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    network: Annotated[str | None, typer.Option("--network", "-n")] = None,
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
    """Sync fidelity bond funding status from the blockchain."""
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
    except (FileNotFoundError, ValueError, UnicodeDecodeError) as e:
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

    from jmwallet.wallet.bond_registry import load_registry

    registry = load_registry(backend_settings.data_dir)

    if not registry.bonds:
        print("\nNo bonds in registry to sync.")
        print("Use 'generate-bond-address' to create bonds first.")
        raise typer.Exit(0)

    asyncio.run(
        _sync_bonds_async(
            registry,
            resolved_mnemonic,
            backend_settings,
            resolved_bip39_passphrase,
        )
    )


async def _sync_bonds_async(
    registry: BondRegistry,
    mnemonic: str,
    backend_settings: ResolvedBackendSettings,
    bip39_passphrase: str = "",
) -> None:
    """Async implementation of bond syncing."""
    from jmwallet.backends import BitcoinCoreBackend
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.bond_registry import save_registry
    from jmwallet.wallet.service import WalletService

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
    else:
        backend = BitcoinCoreBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
        )

    # For descriptor wallet, ensure bond addresses are imported
    if backend_settings.backend_type == "descriptor_wallet" and registry.bonds:
        wallet = WalletService(
            mnemonic=mnemonic,
            backend=backend,
            network=backend_settings.network,
            mixdepth_count=5,
            passphrase=bip39_passphrase,
            data_dir=backend_settings.data_dir,
        )

        # Check if wallet is set up (base wallet without bonds)
        is_ready = await wallet.is_descriptor_wallet_ready(fidelity_bond_count=0)

        if not is_ready:
            # Wallet doesn't exist at all - set it up with bonds
            fidelity_bond_addresses = [(b.address, b.locktime, b.index) for b in registry.bonds]
            logger.info("Descriptor wallet not found. Setting up with bonds...")
            await wallet.setup_descriptor_wallet(
                fidelity_bond_addresses=fidelity_bond_addresses,
                rescan=True,
                smart_scan=True,
                background_full_rescan=True,
            )
        else:
            # Wallet exists - check if bonds are imported
            full_wallet_ready = await wallet.is_descriptor_wallet_ready(
                fidelity_bond_count=len(registry.bonds)
            )

            if not full_wallet_ready:
                # Base wallet exists but bonds missing - import them
                fidelity_bond_addresses = [(b.address, b.locktime, b.index) for b in registry.bonds]
                logger.info(f"Importing {len(fidelity_bond_addresses)} bond addresses...")
                await wallet.import_fidelity_bond_addresses(
                    fidelity_bond_addresses=fidelity_bond_addresses,
                    rescan=True,
                )

    print(f"\nSyncing {len(registry.bonds)} bonds...")
    print("-" * 60)

    # Get all bond addresses
    addresses = [bond.address for bond in registry.bonds]

    # Scan all addresses at once
    try:
        utxos = await backend.get_utxos(addresses)
    except Exception as e:
        logger.error(f"Failed to scan UTXOs: {e}")
        print(f"\nError scanning blockchain: {e}")
        return

    # Build a map of address -> UTXOs
    utxo_map: dict[str, list] = {}
    for utxo in utxos:
        if utxo.address not in utxo_map:
            utxo_map[utxo.address] = []
        utxo_map[utxo.address].append(utxo)

    updated = 0
    for bond in registry.bonds:
        bond_utxos = utxo_map.get(bond.address, [])
        if bond_utxos:
            # Use the first UTXO (there should typically only be one)
            utxo = bond_utxos[0]
            registry.update_utxo_info(
                address=bond.address,
                txid=utxo.txid,
                vout=utxo.vout,
                value=utxo.value,
                confirmations=utxo.confirmations,
            )
            updated += 1
            from jmcore.bitcoin import sats_to_btc

            btc_value = sats_to_btc(utxo.value)
            print(f"  {bond.address[:20]}... FUNDED ({btc_value:.8f} BTC)")
        else:
            if bond.is_funded:
                # Was funded but now isn't - might have been spent
                logger.warning(f"Bond {bond.address[:20]}... previously funded, now empty")
                print(f"  {bond.address[:20]}... SPENT or UNFUNDED")
            else:
                print(f"  {bond.address[:20]}... not funded")

    print("-" * 60)

    if updated > 0:
        save_registry(registry, backend_settings.data_dir)
        print(f"\nUpdated {updated} bond(s). Registry saved.")
    else:
        print("\nNo updates needed.")

    # Show summary
    funded = registry.get_funded_bonds()
    active = registry.get_active_bonds()
    print(f"\nTotal bonds: {len(registry.bonds)}")
    print(f"Funded: {len(funded)}")
    print(f"Active (funded & not expired): {len(active)}")
