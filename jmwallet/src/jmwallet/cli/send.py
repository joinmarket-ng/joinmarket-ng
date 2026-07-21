"""
Send transaction command.
"""

from __future__ import annotations

import asyncio
import math
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
from jmwallet.wallet.spend import (
    DEFAULT_MAX_FEE_RATE_SAT_VB as MAX_MANUAL_FEE_RATE_SAT_VB,
)
from jmwallet.wallet.spend import (
    DUST_THRESHOLD,
    ExcessiveFeeRateError,
    enforce_fee_rate_cap,
    estimate_fee,
)


@app.command(no_args_is_help=True)
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
        typer.Option("--backend", "-b", help="Backend: descriptor_wallet | neutrino"),
    ] = None,
    rpc_url: Annotated[str | None, typer.Option("--rpc-url", envvar="BITCOIN_RPC_URL")] = None,
    neutrino_url: Annotated[
        str | None, typer.Option("--neutrino-url", envvar="NEUTRINO_URL")
    ] = None,
    broadcast: Annotated[
        bool,
        typer.Option(
            "--broadcast/--no-broadcast",
            help="Broadcast the transaction (use --no-broadcast to skip)",
        ),
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
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", "-l", help="Log level"),
    ] = None,
) -> None:
    """Send a simple transaction from wallet to an address."""
    settings = setup_cli(log_level, data_dir=data_dir, config_file=config_file)

    # Validate mutual exclusivity

    if fee_rate is not None and block_target is not None:
        logger.error("Cannot specify both --fee-rate and --block-target")
        raise typer.Exit(1)

    # Effective cap comes from settings (with hard-coded fallback). The same
    # cap is also enforced after backend fee estimation in _send_transaction
    # below, so the estimated path is protected too, not just the manual-rate
    # CLI path.
    max_fee_rate = settings.wallet.max_fee_rate_sat_vb

    if fee_rate is not None:
        if not math.isfinite(fee_rate) or fee_rate <= 0:
            logger.error("--fee-rate must be a finite number greater than 0")
            raise typer.Exit(1)
        if fee_rate > max_fee_rate:
            logger.error(
                f"--fee-rate {fee_rate:.2f} sat/vB exceeds safety maximum "
                f"({max_fee_rate:.0f} sat/vB)"
            )
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
            creation_height=resolved_creation_height,
            max_fee_rate_sat_vb=max_fee_rate,
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
            reconstruct_history=settings.wallet.reconstruct_history,
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
    *,
    creation_height: int | None = None,
    max_fee_rate_sat_vb: float = MAX_MANUAL_FEE_RATE_SAT_VB,
    max_sats_freeze_reuse: int = -1,
    reconstruct_history: bool = True,
) -> None:
    """Send transaction implementation."""
    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.service import WalletService
    from jmwallet.wallet.signing import (
        TransactionSigningError,
        deserialize_transaction,
        encode_varint,
    )

    # The wallet name is derived from the master fingerprint. Registered
    # fidelity bonds are loaded and imported by ``sync_with_registered_bonds``
    # below, so they do not need to be collected here.
    wallet_fingerprint = get_mnemonic_fingerprint(mnemonic, bip39_passphrase)

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
            fee_estimate_url=backend_settings.fee_estimate_url,
            fee_estimate_proxy=backend_settings.fee_estimate_proxy,
        )
        logger.info("Waiting for neutrino to sync...")
        synced = await backend.wait_for_sync(timeout=300.0)
        if not synced:
            logger.error("Neutrino sync timeout")
            return
    elif backend_settings.backend_type == "descriptor_wallet":
        wallet_name = generate_wallet_name(wallet_fingerprint, backend_settings.network)
        backend = DescriptorWalletBackend(
            rpc_url=backend_settings.rpc_url,
            rpc_user=backend_settings.rpc_user,
            rpc_password=backend_settings.rpc_password,
            wallet_name=wallet_name,
        )
    else:
        raise ValueError(f"Unknown backend type: {backend_settings.backend_type}")

    if creation_height is not None:
        backend.set_wallet_creation_height(creation_height)

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
                f"bitcoin.conf (see docs/technical/configuration.md, 'Minimum Relay Fee')."
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

    # Enforce the cap on the final rate, including any mempool-minimum floor.
    try:
        enforce_fee_rate_cap(resolved_fee_rate, max_fee_rate_sat_vb, source="resolved")
    except ExcessiveFeeRateError as exc:
        logger.error(str(exc))
        raise typer.Exit(1) from exc

    wallet = WalletService(
        mnemonic=mnemonic,
        backend=backend,
        network=backend_settings.network,
        mixdepth_count=5,
        passphrase=bip39_passphrase,
        data_dir=backend_settings.data_dir,
        max_sats_freeze_reuse=max_sats_freeze_reuse,
        reconstruct_history=reconstruct_history,
    )

    try:
        # Bond-aware sync: imports any registered fidelity bond's watch-only
        # ``addr()`` descriptor into Bitcoin Core (and rescans) when missing, so
        # a bond funded after the base wallet was set up is visible and
        # spendable. Detection is by the actual ``addr()`` descriptor set, not a
        # descriptor count (which over-counts the base wallet). Non-descriptor
        # backends (neutrino) scan the bond addresses directly inside this call.
        await wallet.sync_with_registered_bonds()

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
                utxo.label = get_utxo_label(
                    utxo.address,
                    backend_settings.data_dir,
                    wallet_fingerprint=wallet.wallet_fingerprint,
                )

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

        if amount == 0:
            # Sweep selected UTXOs
            send_amount = total_input
        else:
            send_amount = amount

        if send_amount > total_input:
            logger.error(f"Insufficient funds: need {send_amount:,}, have {total_input:,}")
            raise typer.Exit(1)

        # Size each selected input by script type. Expired fidelity bonds are
        # P2WSH and have a larger witness than regular P2WPKH inputs.
        estimated_fee, _ = estimate_fee(
            utxos,
            destination,
            resolved_fee_rate,
            has_change=amount > 0,
        )

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
            if change_amount < DUST_THRESHOLD:
                # With no change output, every satoshi not sent is the actual fee.
                estimated_fee = total_input - send_amount
                change_amount = 0

        num_outputs = 1 + int(change_amount > 0)

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
                mining_fee=estimated_fee,
                additional_info={
                    "Source Mixdepth": mixdepth,
                    "Change": format_amount(change_amount) if change_amount > 0 else "None",
                    "Miner Fee Rate": f"{resolved_fee_rate:.2f} sat/vB",
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
        max_locktime = 0
        has_timelocked = False
        locktime_cutoff = 0
        if any(utxo.is_timelocked for utxo in utxos):
            locktime_cutoff = await backend.get_median_time_past()
        for utxo in utxos:
            if utxo.is_timelocked and utxo.locktime is not None:
                has_timelocked = True
                if utxo.locktime > max_locktime:
                    max_locktime = utxo.locktime
                if utxo.locktime >= locktime_cutoff:
                    logger.error(
                        f"Cannot spend timelocked UTXO {utxo.txid}:{utxo.vout} - "
                        f"locktime {utxo.locktime} has not passed chain time "
                        f"{locktime_cutoff}"
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
        change_addr = ""
        if change_amount > 0:
            change_index = wallet.get_next_address_index(mixdepth, 1)
            change_addr = wallet.get_change_address(mixdepth, change_index)
            change_key = wallet.get_key_for_address(change_addr)
            if not change_key:
                logger.error(
                    "Failed to derive change key for selected change address; "
                    "cannot build a safe transaction"
                )
                raise typer.Exit(1)

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

        # Sign the transaction. Key access and signing are delegated to the
        # wallet (issue #518) so private keys never leave the wallet boundary.
        tx = deserialize_transaction(unsigned_tx)
        witnesses: list[list[bytes]] = []

        for i, utxo in enumerate(utxos):
            try:
                signed = wallet.sign_input(tx, i, utxo)
            except TransactionSigningError as exc:
                logger.error(str(exc))
                raise typer.Exit(1) from exc
            witnesses.append(signed.witness)

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

        # Persist a "send" history entry BEFORE broadcasting so that the
        # destination and change addresses are recorded as used even if the
        # broadcast itself fails or the process is killed mid-broadcast. Once
        # we have a signed transaction, the addresses are committed: the
        # signed bytes can be re-broadcast by anyone holding them, so the
        # wallet must never propose those addresses as fresh again, even
        # without Bitcoin Core seeing the transaction. ``get_used_addresses``
        # consumes this entry so ``WalletService.get_next_address_index``
        # advances past these addresses on subsequent runs.
        from jmwallet.history import (
            append_history_entry,
            create_send_history_entry,
            update_send_awaiting_broadcast,
        )

        selected_outpoints = [(u.txid, u.vout) for u in utxos]
        selected_input_addresses = [u.address for u in utxos]
        send_entry = create_send_history_entry(
            destination=destination,
            change_address=change_addr,
            amount=send_amount,
            mining_fee=estimated_fee,
            source_mixdepth=mixdepth,
            selected_utxos=selected_outpoints,
            txid="",
            success=False,
            failure_reason="awaiting broadcast",
            network=backend_settings.network,
            wallet_fingerprint=wallet.wallet_fingerprint,
            source_addresses=selected_input_addresses,
        )
        try:
            append_history_entry(send_entry, data_dir=backend_settings.data_dir)
        except Exception as e:
            # Persistence failure should not block the user from broadcasting;
            # surface a warning and continue.
            logger.warning(f"Failed to persist send history entry: {e}")

        if broadcast:
            logger.info("Broadcasting transaction...")
            try:
                txid = await backend.broadcast_transaction(tx_hex)
            except Exception:
                try:
                    update_send_awaiting_broadcast(
                        send_entry,
                        txid="",
                        success=False,
                        failure_reason="broadcast failed",
                        data_dir=backend_settings.data_dir,
                    )
                except Exception as e:
                    logger.warning(f"Failed to finalize send history entry: {e}")
                raise
            print("\nTransaction broadcast successfully!")
            print(f"TXID: {txid}")
            try:
                update_send_awaiting_broadcast(
                    send_entry,
                    txid=txid,
                    success=True,
                    failure_reason="",
                    data_dir=backend_settings.data_dir,
                )
            except Exception as e:
                logger.warning(f"Failed to finalize send history entry: {e}")
        else:
            print("\nTransaction NOT broadcast (--no-broadcast set)")
            print(f"Full hex: {tx_hex}")

    finally:
        await wallet.close()
