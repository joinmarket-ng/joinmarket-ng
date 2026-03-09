"""
Send transaction command.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

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
def send(
    destination: Annotated[str, typer.Argument(help="Destination address")],
    amount: Annotated[int, typer.Option("--amount", "-a", help="Amount in sats (0 for sweep)")] = 0,
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    mixdepth: Annotated[int, typer.Option("--mixdepth", "-m", help="Source mixdepth")] = 0,
    fee_rate: Annotated[
        float | None,
        typer.Option(
            "--fee-rate",
            help="Manual fee rate in sat/vB (e.g. 1.5). "
            "Mutually exclusive with --block-target. "
            "Defaults to 3-block estimation.",
        ),
    ] = None,
    block_target: Annotated[
        int | None,
        typer.Option(
            "--block-target",
            help="Target blocks for fee estimation (1-1008). Defaults to 3.",
        ),
    ] = None,
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
    broadcast: Annotated[
        bool, typer.Option("--broadcast", help="Broadcast the transaction")
    ] = True,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation prompt")] = False,
    select_utxos: Annotated[
        bool,
        typer.Option(
            "--select-utxos",
            "-s",
            help="Interactively select UTXOs (fzf-like TUI)",
        ),
    ] = False,
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
    """Send a simple transaction from wallet to an address."""
    settings = setup_cli(log_level)

    # Validate mutual exclusivity

    if fee_rate is not None and block_target is not None:
        logger.error("Cannot specify both --fee-rate and --block-target")
        raise typer.Exit(1)

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

    # Use configured default block target if not specified
    if block_target is None and fee_rate is None:
        block_target = settings.wallet.default_fee_block_target

    asyncio.run(
        _send_transaction(
            resolved_mnemonic,
            destination,
            amount,
            mixdepth,
            fee_rate,
            block_target,
            backend_settings,
            broadcast,
            yes,
            select_utxos,
            resolved_bip39_passphrase,
        )
    )


async def _send_transaction(
    mnemonic: str,
    destination: str,
    amount: int,
    mixdepth: int,
    fee_rate: float | None,
    block_target: int | None,
    backend_settings: ResolvedBackendSettings,
    broadcast: bool,
    skip_confirmation: bool,
    interactive_utxo_selection: bool,
    bip39_passphrase: str = "",
) -> None:
    """Send transaction implementation."""
    import math

    from jmwallet.backends.bitcoin_core import BitcoinCoreBackend
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.bond_registry import load_registry
    from jmwallet.wallet.service import WalletService
    from jmwallet.wallet.signing import (
        create_p2wpkh_script_code,
        create_p2wsh_witness_stack,
        deserialize_transaction,
        encode_varint,
        sign_p2wpkh_input,
        sign_p2wsh_input,
    )

    # Load fidelity bond addresses from registry
    bond_registry = load_registry(backend_settings.data_dir)
    fidelity_bond_addresses: list[tuple[str, int, int]] = [
        (bond.address, bond.locktime, bond.index)
        for bond in bond_registry.bonds
        if bond.network == backend_settings.network
    ]

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

    # Resolve fee rate
    # Get mempool minimum fee (if available) as a floor
    mempool_min_fee: float | None = None
    try:
        mempool_min_fee = await backend.get_mempool_min_fee()
        if mempool_min_fee is not None:
            logger.debug(f"Mempool min fee: {mempool_min_fee:.2f} sat/vB")
    except Exception:
        # Backend may not support this method
        pass

    if fee_rate is not None:
        resolved_fee_rate = fee_rate
        # Check against mempool min fee
        if mempool_min_fee is not None and resolved_fee_rate < mempool_min_fee:
            logger.warning(
                f"Manual fee rate {resolved_fee_rate:.2f} sat/vB is below node's minimum relay "
                f"fee {mempool_min_fee:.2f} sat/vB. Using mempool minimum instead. "
                f"To use lower fee rates, configure minrelaytxfee in your Bitcoin node's "
                f"bitcoin.conf (see DOCS.md for details)."
            )
            resolved_fee_rate = mempool_min_fee
        logger.info(f"Using manual fee rate: {resolved_fee_rate:.2f} sat/vB")
    else:
        # Use backend fee estimation
        target = block_target if block_target is not None else 3
        resolved_fee_rate = await backend.estimate_fee(target)
        # Check against mempool min fee
        if mempool_min_fee is not None and resolved_fee_rate < mempool_min_fee:
            logger.info(
                f"Estimated fee {resolved_fee_rate:.2f} sat/vB is below mempool min "
                f"{mempool_min_fee:.2f} sat/vB, using mempool min"
            )
            resolved_fee_rate = mempool_min_fee
        logger.info(f"Fee estimation for {target} blocks: {resolved_fee_rate:.2f} sat/vB")

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=backend_settings.network,
        mixdepth_count=5,
        passphrase=bip39_passphrase,
        data_dir=backend_settings.data_dir,
    )

    try:
        # Use descriptor wallet sync if available
        if backend_settings.backend_type == "descriptor_wallet" and isinstance(
            backend, DescriptorWalletBackend
        ):
            bond_count = len(fidelity_bond_addresses)
            base_wallet_ready = await wallet.is_descriptor_wallet_ready(fidelity_bond_count=0)
            full_wallet_ready = await wallet.is_descriptor_wallet_ready(
                fidelity_bond_count=bond_count
            )

            if not base_wallet_ready:
                logger.info("Descriptor wallet not set up. Setting up...")
                await wallet.setup_descriptor_wallet(
                    rescan=True,
                    fidelity_bond_addresses=fidelity_bond_addresses if bond_count else None,
                )
            elif not full_wallet_ready and bond_count > 0:
                logger.info("Importing fidelity bond addresses...")
                await wallet.import_fidelity_bond_addresses(fidelity_bond_addresses, rescan=True)

            await wallet.sync_with_descriptor_wallet(
                fidelity_bond_addresses=fidelity_bond_addresses if bond_count else None
            )
        else:
            await wallet.sync_all(fidelity_bond_addresses or None)

        balance = await wallet.get_balance(mixdepth)
        logger.info(f"Mixdepth {mixdepth} balance: {balance:,} sats")

        # Fetch UTXOs early for interactive selection
        utxos = await wallet.get_utxos(mixdepth)
        if not utxos:
            logger.error("No UTXOs available")
            raise typer.Exit(1)

        # Interactive UTXO selection if requested
        if interactive_utxo_selection:
            from jmwallet.history import get_utxo_label
            from jmwallet.utxo_selector import select_utxos_interactive

            # Populate labels for each UTXO based on history
            for utxo in utxos:
                utxo.label = get_utxo_label(utxo.address, backend_settings.data_dir)

            try:
                selected_utxos = select_utxos_interactive(utxos, amount)
                if not selected_utxos:
                    logger.info("UTXO selection cancelled")
                    return
                utxos = selected_utxos
                logger.info(f"Selected {len(utxos)} UTXOs")
            except RuntimeError as e:
                logger.error(f"Cannot use interactive UTXO selection: {e}")
                raise typer.Exit(1)
        else:
            # Auto-selection: filter out frozen and fidelity bond UTXOs
            # (frozen UTXOs must never be auto-spent; fidelity bonds must be
            # explicitly selected via interactive mode)
            spendable = [u for u in utxos if not u.frozen and not u.is_fidelity_bond]
            frozen_count = len(utxos) - len(spendable)
            if frozen_count > 0:
                logger.info(
                    f"Excluding {frozen_count} frozen/fidelity-bond UTXO(s) from auto-selection"
                )
            utxos = spendable
            if not utxos:
                logger.error(
                    "No spendable UTXOs available (all UTXOs are frozen or fidelity bonds)"
                )
                raise typer.Exit(1)

        # Calculate totals based on selected UTXOs
        total_input = sum(u.value for u in utxos)
        num_inputs = len(utxos)

        if amount == 0:
            # Sweep selected UTXOs
            send_amount = total_input
        else:
            send_amount = amount

        if send_amount > total_input:
            logger.error(f"Insufficient funds: need {send_amount:,}, have {total_input:,}")
            raise typer.Exit(1)

        # Estimate transaction size
        from jmcore.bitcoin import estimate_vsize, get_address_type

        try:
            dest_type = get_address_type(destination)
        except ValueError:
            logger.warning(f"Could not determine address type for {destination}, assuming P2WPKH")
            dest_type = "p2wpkh"

        input_types = ["p2wpkh"] * num_inputs
        output_types = [dest_type]

        # Initial assumption: we have change if not sweeping
        if amount > 0:
            output_types.append("p2wpkh")  # Change is always P2WPKH

        estimated_vsize = estimate_vsize(input_types, output_types)
        estimated_fee = math.ceil(estimated_vsize * resolved_fee_rate)

        if amount == 0:
            # Sweep: subtract fee from send amount
            send_amount = total_input - estimated_fee
            if send_amount <= 0:
                logger.error("Balance too low to cover fees")
                raise typer.Exit(1)
            change_amount = 0
        else:
            change_amount = total_input - send_amount - estimated_fee
            if change_amount < 0:
                logger.error(f"Insufficient funds after fee: need {send_amount + estimated_fee:,}")
                raise typer.Exit(1)
            if change_amount < 546:  # Dust threshold
                # Add to fee instead
                estimated_fee += change_amount
                change_amount = 0
                # Re-estimate without change output
                output_types.pop()  # Remove change output
                estimated_vsize = estimate_vsize(input_types, output_types)
                estimated_fee = math.ceil(estimated_vsize * resolved_fee_rate)

        num_outputs = len(output_types)

        # Use new format_amount for display
        from jmcore.bitcoin import format_amount

        logger.info(f"Sending {format_amount(send_amount)} to {destination}")
        logger.info(f"Fee: {format_amount(estimated_fee)} ({resolved_fee_rate:.2f} sat/vB)")
        if change_amount > 0:
            logger.info(f"Change: {format_amount(change_amount)}")

        # Prompt for confirmation before building transaction
        from jmcore.confirmation import confirm_transaction

        try:
            confirmed = confirm_transaction(
                operation="send",
                amount=send_amount,
                destination=destination,
                fee=estimated_fee,
                additional_info={
                    "Source Mixdepth": mixdepth,
                    "Change": format_amount(change_amount) if change_amount > 0 else "None",
                    "Fee Rate": f"{resolved_fee_rate:.2f} sat/vB",
                },
                skip_confirmation=skip_confirmation,
            )
            if not confirmed:
                logger.info("Transaction cancelled by user")
                return
        except RuntimeError as e:
            logger.error(str(e))
            raise typer.Exit(1)

        # Build unsigned transaction
        from bitcointx import ChainParams
        from bitcointx.wallet import CCoinAddress, CCoinAddressError

        from jmwallet.wallet.address import pubkey_to_p2wpkh_script

        # Convert destination to scriptPubKey — CCoinAddress validates the
        # bech32 checksum, rejects wrong-network addresses, and handles all
        # supported address types (P2WPKH, P2WSH, P2TR, …).
        network_to_chain = {
            "mainnet": "bitcoin",
            "testnet": "bitcoin/testnet",
            "signet": "bitcoin/signet",
            "regtest": "bitcoin/regtest",
        }
        chain = network_to_chain.get(backend_settings.network, "bitcoin")
        try:
            with ChainParams(chain):
                dest_script = bytes(CCoinAddress(destination).to_scriptPubKey())
        except CCoinAddressError:
            logger.error(f"Invalid address (bad checksum, format, or wrong network): {destination}")
            raise typer.Exit(1)

        # Build raw transaction
        version = (2).to_bytes(4, "little")

        # Determine transaction locktime - must be >= max CLTV locktime if spending timelocked UTXOs
        import time

        max_locktime = 0
        has_timelocked = False
        current_time = int(time.time())
        for utxo in utxos:
            if utxo.is_timelocked and utxo.locktime is not None:
                has_timelocked = True
                if utxo.locktime > max_locktime:
                    max_locktime = utxo.locktime
                if utxo.locktime > current_time:
                    logger.error(
                        f"Cannot spend timelocked UTXO {utxo.txid}:{utxo.vout} - "
                        f"locktime {utxo.locktime} is in the future "
                        f"(current time: {current_time})"
                    )
                    raise typer.Exit(1)

        locktime = max_locktime.to_bytes(4, "little")

        # Inputs
        inputs_data = bytearray()
        for utxo in utxos:
            txid_bytes = bytes.fromhex(utxo.txid)[::-1]  # Little-endian
            inputs_data.extend(txid_bytes)
            inputs_data.extend(utxo.vout.to_bytes(4, "little"))
            inputs_data.append(0)  # Empty scriptSig for SegWit
            # For timelocked UTXOs, sequence must be < 0xFFFFFFFF to enable locktime
            if has_timelocked:
                inputs_data.extend((0xFFFFFFFE).to_bytes(4, "little"))  # Enable locktime
            else:
                inputs_data.extend((0xFFFFFFFF).to_bytes(4, "little"))  # Sequence

        # Outputs
        outputs_data = bytearray()
        # Destination
        outputs_data.extend(send_amount.to_bytes(8, "little"))
        outputs_data.extend(encode_varint(len(dest_script)))
        outputs_data.extend(dest_script)

        # Change (if any)
        if change_amount > 0:
            change_index = wallet.get_next_address_index(mixdepth, 1)
            change_addr = wallet.get_change_address(mixdepth, change_index)
            change_key = wallet.get_key_for_address(change_addr)
            if change_key:
                change_script = pubkey_to_p2wpkh_script(
                    change_key.get_public_key_bytes(compressed=True).hex()
                )
                outputs_data.extend(change_amount.to_bytes(8, "little"))
                outputs_data.extend(encode_varint(len(change_script)))
                outputs_data.extend(change_script)

        # Assemble unsigned transaction (without witness)
        unsigned_tx = (
            version
            + encode_varint(len(utxos))
            + bytes(inputs_data)
            + encode_varint(num_outputs)
            + bytes(outputs_data)
            + locktime
        )

        # Sign the transaction
        tx = deserialize_transaction(unsigned_tx)
        witnesses: list[list[bytes]] = []

        for i, utxo in enumerate(utxos):
            key = wallet.get_key_for_address(utxo.address)
            if not key:
                logger.error(f"Missing key for address {utxo.address}")
                raise typer.Exit(1)

            pubkey_bytes = key.get_public_key_bytes(compressed=True)

            # Check if this is a timelocked (fidelity bond) UTXO
            if utxo.is_timelocked and utxo.locktime is not None:
                # P2WSH signing for fidelity bonds
                from jmcore.btc_script import mk_freeze_script

                witness_script = mk_freeze_script(pubkey_bytes.hex(), utxo.locktime)
                signature = sign_p2wsh_input(
                    tx=tx,
                    input_index=i,
                    witness_script=witness_script,
                    value=utxo.value,
                    private_key=key.private_key,
                )
                witnesses.append(create_p2wsh_witness_stack(signature, witness_script))
            elif utxo.is_p2wsh:
                # P2WSH UTXO detected but locktime not known - this shouldn't happen
                # if the wallet was synced correctly with fidelity bond locktimes
                logger.error(
                    f"Cannot sign P2WSH UTXO {utxo.txid}:{utxo.vout} - "
                    f"locktime not available. This UTXO appears to be a fidelity bond "
                    f"but was not synced with its locktime information."
                )
                raise typer.Exit(1)
            else:
                # P2WPKH signing for regular UTXOs
                script_code = create_p2wpkh_script_code(pubkey_bytes)
                signature = sign_p2wpkh_input(
                    tx=tx,
                    input_index=i,
                    script_code=script_code,
                    value=utxo.value,
                    private_key=key.private_key,
                )
                witnesses.append([signature, pubkey_bytes])

        # Build signed transaction with witness
        signed_tx = bytearray()
        signed_tx.extend(version)
        signed_tx.extend(b"\x00\x01")  # Marker and flag for SegWit
        signed_tx.extend(encode_varint(len(utxos)))
        signed_tx.extend(inputs_data)
        signed_tx.extend(encode_varint(num_outputs))
        signed_tx.extend(outputs_data)

        # Witness stack
        for witness_stack in witnesses:
            signed_tx.extend(encode_varint(len(witness_stack)))
            for item in witness_stack:
                signed_tx.extend(encode_varint(len(item)))
                signed_tx.extend(item)

        signed_tx.extend(locktime)

        tx_hex = bytes(signed_tx).hex()
        print(f"\nSigned Transaction ({len(signed_tx)} bytes):")
        print(f"{tx_hex[:80]}...")

        if broadcast:
            logger.info("Broadcasting transaction...")
            txid = await backend.broadcast_transaction(tx_hex)
            print("\nTransaction broadcast successfully!")
            print(f"TXID: {txid}")
        else:
            print("\nTransaction NOT broadcast (--broadcast not set)")
            print(f"Full hex: {tx_hex}")

    finally:
        await wallet.close()
