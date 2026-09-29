"""
Send transaction command.
"""

from __future__ import annotations

import asyncio
import math
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
from jmwallet.wallet.spend import (
    DEFAULT_MAX_FEE_RATE_SAT_VB as MAX_MANUAL_FEE_RATE_SAT_VB,
)
from jmwallet.wallet.spend import (
    DUST_THRESHOLD,
    DirectTxOutput,
    ExcessiveFeeRateError,
    build_and_sign_direct_tx,
    enforce_fee_rate_cap,
    estimate_fee,
    resolve_broadcast_txid,
    resolve_direct_send_locktime,
)

if TYPE_CHECKING:
    from jmwallet.backends.base import MempoolAcceptResult
    from jmwallet.history import TransactionHistoryEntry
    from jmwallet.wallet.models import UTXOInfo
    from jmwallet.wallet.service import WalletService


def _resolve_broadcast_txid(tx_hex: str, backend_txid: str | None) -> str:
    """Return the txid committed by the signed transaction bytes."""
    return resolve_broadcast_txid(tx_hex, backend_txid)


def _finalize_send_history_entry(
    send_entry: TransactionHistoryEntry,
    *,
    txid: str,
    success: bool,
    failure_reason: str,
    data_dir: Path,
    history_persisted: bool,
) -> None:
    """Best-effort finalization for a successfully appended send history row."""
    if not history_persisted:
        return

    from jmwallet.history import update_send_awaiting_broadcast

    try:
        updated = update_send_awaiting_broadcast(
            send_entry,
            txid=txid,
            success=success,
            failure_reason=failure_reason,
            data_dir=data_dir,
        )
        if not updated:
            logger.warning("Could not find pre-broadcast send history entry to finalize")
    except Exception as exc:
        logger.warning("Failed to finalize send history entry")
        logger.bind(sensitive=True).warning(f"Failed to finalize send history entry: {exc}")


def _mempool_policy_failure_reason(result: MempoolAcceptResult) -> str:
    """Return a user-visible, most-specific failed preflight explanation."""
    reject_details = getattr(result, "reject_details", None)
    if isinstance(reject_details, str) and reject_details:
        return reject_details
    reject_reason = getattr(result, "reject_reason", None)
    if isinstance(reject_reason, str) and reject_reason:
        return reject_reason
    package_error = getattr(result, "package_error", None)
    if isinstance(package_error, str) and package_error:
        return package_error
    return "Bitcoin Core testmempoolaccept did not explicitly allow the transaction"


async def _select_input_utxos(
    wallet: WalletService,
    backend_settings: ResolvedBackendSettings,
    amount: int,
    mixdepth: int | None,
    interactive: bool,
    destination: str,
    fee_rate: float,
) -> tuple[list[UTXOInfo], int] | None:
    """Pick the transaction inputs, interactively or automatically.

    Interactive mode shows the whole wallet in the selector for a full
    overview; the source mixdepth is pinned by ``mixdepth`` when given
    explicitly, otherwise derived from the selection (a spend always draws
    from a single mixdepth; the TUI enforces this).

    Returns:
        ``(utxos, source_mixdepth)``, or ``None`` when the user cancelled
        the interactive selection.

    Raises:
        typer.Exit: When no (spendable) UTXOs are available or the selector
            cannot run.
    """
    if interactive:
        from jmwallet.utxo_selector import select_utxos_interactive

        locked_outpoints = {
            (txid.lower(), vout) for txid, vout in wallet.get_locked_input_outpoints()
        }
        utxos: list[UTXOInfo] = []
        for md in range(wallet.mixdepth_count):
            utxos.extend(await wallet.get_utxos(md))
        if not utxos:
            logger.error("No UTXOs available")
            raise typer.Exit(1)

        # Populate only unlabeled non-bond UTXOs from wallet internals. BIP-329
        # user labels and fidelity-bond labels must remain visible in the selector.
        for utxo in utxos:
            if utxo.label is None and not utxo.is_fidelity_bond:
                utxo.label = wallet.get_utxo_label_from_wallet(utxo.address)

        try:
            selected_utxos = select_utxos_interactive(
                utxos,
                amount,
                allowed_mixdepth=mixdepth,
                excluded_outpoints=locked_outpoints,
            )
        except RuntimeError as e:
            logger.error("Cannot use interactive UTXO selection")
            logger.bind(sensitive=True).error(f"Cannot use interactive UTXO selection: {e}")
            raise typer.Exit(1)
        if not selected_utxos:
            logger.info("UTXO selection cancelled")
            return None
        mixdepth = selected_utxos[0].mixdepth
        logger.info(f"Selected {len(selected_utxos)} UTXOs from mixdepth {mixdepth}")
        return selected_utxos, mixdepth

    if amount > 0:
        from jmwallet.wallet.spend import select_automatic_direct_send_inputs

        try:
            selection, selected_mixdepth = await select_automatic_direct_send_inputs(
                wallet=wallet,
                amount_sats=amount,
                destination=destination,
                fee_rate=fee_rate,
                mixdepth=mixdepth,
            )
        except ValueError as exc:
            logger.error("Automatic input selection failed")
            logger.bind(sensitive=True).error(str(exc))
            raise typer.Exit(1) from exc
        logger.info(f"Selected {len(selection.utxos)} UTXO(s) from mixdepth {selected_mixdepth}")
        return selection.utxos, selected_mixdepth

    if mixdepth is None:
        logger.error("--mixdepth is required when sweeping (--amount 0)")
        raise typer.Exit(1)

    balance = await wallet.get_balance(mixdepth)
    logger.bind(sensitive=True).info(f"Mixdepth {mixdepth} balance: {balance:,} sats")

    utxos = await wallet.get_utxos(mixdepth)
    if not utxos:
        logger.error("No UTXOs available")
        raise typer.Exit(1)

    # Auto-selection: filter out frozen and fidelity bond UTXOs
    # (frozen UTXOs must never be auto-spent; fidelity bonds must be
    # explicitly selected via interactive mode)
    locked_outpoints = {(txid.lower(), vout) for txid, vout in wallet.get_locked_input_outpoints()}
    blocked_regular_scripts = {
        utxo.scriptpubkey
        for utxo in utxos
        if not utxo.is_fidelity_bond and (utxo.txid, utxo.vout) in locked_outpoints
    }
    spendable = [
        utxo
        for utxo in utxos
        if not utxo.frozen
        and not utxo.is_fidelity_bond
        and (utxo.txid, utxo.vout) not in locked_outpoints
        and utxo.scriptpubkey not in blocked_regular_scripts
    ]
    frozen_count = len(utxos) - len(spendable)
    if frozen_count > 0:
        logger.info(
            f"Excluding {frozen_count} frozen, fidelity-bond, or CoinJoin-locked UTXO(s) "
            "from auto-selection"
        )
    if not spendable:
        logger.error("No spendable UTXOs available (all UTXOs are frozen or fidelity bonds)")
        raise typer.Exit(1)
    return spendable, mixdepth


@app.command(no_args_is_help=True)
def send(
    destination: Annotated[str, typer.Argument(help="Destination address")],
    amount: Annotated[
        int,
        typer.Option(
            "--amount",
            "-a",
            help="Amount in sats (0 for sweep; with --select-utxos, defaults to sweep)",
        ),
    ] = 0,
    mnemonic_file: Annotated[
        Path | None, typer.Option("--mnemonic-file", "-f", envvar="MNEMONIC_FILE")
    ] = None,
    prompt_bip39_passphrase: Annotated[
        bool, typer.Option("--prompt-bip39-passphrase", help="Prompt for BIP39 passphrase")
    ] = False,
    mixdepth: Annotated[
        int | None,
        typer.Option(
            "--mixdepth",
            "-m",
            help="Source mixdepth. Fixed-amount automatic sends use the highest funded "
            "mixdepth unless set explicitly; automatic sweeps require this option. "
            "With --select-utxos, it is derived from the selection unless set explicitly.",
        ),
    ] = None,
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
    rbf: Annotated[
        bool,
        typer.Option(
            "--rbf/--no-rbf",
            help="Signal BIP125 replace-by-fee (enabled by default)",
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
    input_utxo: Annotated[
        list[str] | None,
        typer.Option(
            "--input-utxo",
            help="Explicit input UTXO as txid:vout (repeatable). Spends exactly "
            "the given UTXOs (also for sweeps) instead of auto-selecting; every "
            "UTXO must already be unfrozen and belong to --mixdepth. Mutually "
            "exclusive with --select-utxos.",
        ),
    ] = None,
    allow_conflicts: Annotated[
        bool,
        typer.Option(
            "--allow-conflicts",
            help="Allow replacement of named wallet inputs spent by a mempool transaction",
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

    if select_utxos and input_utxo:
        logger.error("Cannot specify both --select-utxos and --input-utxo")
        raise typer.Exit(1)

    if allow_conflicts and not input_utxo:
        logger.error("--allow-conflicts requires at least one --input-utxo")
        raise typer.Exit(1)

    if amount == 0 and mixdepth is None and not select_utxos:
        logger.error("--mixdepth is required when sweeping (--amount 0)")
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
    if allow_conflicts and backend_settings.backend_type != "descriptor_wallet":
        logger.error("--allow-conflicts is supported only with the descriptor_wallet backend")
        raise typer.Exit(1)

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
            mnemonic_file=resolved.mnemonic_file,
            mixdepth_count=settings.wallet.mixdepth_count,
            max_fee_rate_sat_vb=max_fee_rate,
            max_sats_freeze_reuse=settings.wallet.max_sats_freeze_reuse,
            reconstruct_history=settings.wallet.reconstruct_history,
            input_utxos=input_utxo,
            allow_conflicts=allow_conflicts,
            rbf=rbf,
            address_type=settings.wallet.address_type,
        )
    )


async def _send_transaction(
    mnemonic: str,
    destination: str,
    amount: int,
    mixdepth: int | None,
    fee_rate: float | None,
    block_target: int | None,
    backend_settings: ResolvedBackendSettings,
    broadcast: bool,
    skip_confirmation: bool,
    interactive_utxo_selection: bool,
    bip39_passphrase: str = "",
    *,
    creation_height: int | None = None,
    mnemonic_file: Path | None = None,
    mixdepth_count: int = 5,
    max_fee_rate_sat_vb: float = MAX_MANUAL_FEE_RATE_SAT_VB,
    max_sats_freeze_reuse: int = -1,
    reconstruct_history: bool = True,
    input_utxos: list[str] | None = None,
    allow_conflicts: bool = False,
    rbf: bool = True,
    address_type: str = "p2wpkh",
) -> None:
    """Send transaction implementation."""
    if allow_conflicts and not input_utxos:
        logger.error("--allow-conflicts requires at least one --input-utxo")
        raise typer.Exit(1)
    if allow_conflicts and backend_settings.backend_type != "descriptor_wallet":
        logger.error("--allow-conflicts is supported only with the descriptor_wallet backend")
        raise typer.Exit(1)

    from jmwallet.backends.descriptor_wallet import (
        DescriptorWalletBackend,
        generate_wallet_name,
        get_mnemonic_fingerprint,
    )
    from jmwallet.backends.neutrino import NeutrinoBackend
    from jmwallet.wallet.service import WalletService
    from jmwallet.wallet.signing import (
        TransactionSigningError,
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
            scan_start_height=backend_settings.scan_start_height,
            scan_lookback_blocks=backend_settings.scan_lookback_blocks,
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
        mixdepth_count=mixdepth_count,
        passphrase=bip39_passphrase,
        data_dir=backend_settings.data_dir,
        max_sats_freeze_reuse=max_sats_freeze_reuse,
        reconstruct_history=reconstruct_history,
        mnemonic_file=mnemonic_file,
        address_type=address_type,
    )

    try:
        # Bond-aware sync: imports any registered fidelity bond's watch-only
        # ``addr()`` descriptor into Bitcoin Core (and rescans) when missing, so
        # a bond funded after the base wallet was set up is visible and
        # spendable. Detection is by the actual ``addr()`` descriptor set, not a
        # descriptor count (which over-counts the base wallet). Non-descriptor
        # backends (neutrino) scan the bond addresses directly inside this call.
        await wallet.sync_with_registered_bonds()

        # Pick the inputs: explicit --input-utxo, interactive, or automatic.
        locktime_cutoff: int | None = None
        if input_utxos:
            from jmwallet.wallet.spend import resolve_input_utxos

            resolved_mixdepth = mixdepth if mixdepth is not None else 0
            try:
                utxos, locktime_cutoff = await resolve_input_utxos(
                    wallet=wallet,
                    backend=backend,
                    mixdepth=resolved_mixdepth,
                    input_utxos=input_utxos,
                    allow_conflicts=allow_conflicts,
                )
            except ValueError as e:
                logger.error("Explicit input selection failed")
                logger.bind(sensitive=True).error(str(e))
                raise typer.Exit(1)
            mixdepth = resolved_mixdepth
            logger.info(f"Using {len(utxos)} explicitly selected UTXO(s) from mixdepth {mixdepth}")
        else:
            selection = await _select_input_utxos(
                wallet,
                backend_settings,
                amount,
                mixdepth,
                interactive_utxo_selection,
                destination,
                resolved_fee_rate,
            )
            if selection is None:
                raise typer.Exit(1)
            utxos, mixdepth = selection

        # Calculate totals based on selected UTXOs
        total_input = sum(u.value for u in utxos)

        if amount == 0:
            # Sweep selected UTXOs
            send_amount = total_input
        else:
            send_amount = amount

        if send_amount > total_input:
            logger.error("Insufficient funds")
            logger.bind(sensitive=True).error(
                f"Insufficient funds: need {send_amount:,}, have {total_input:,}"
            )
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
            if change_amount < DUST_THRESHOLD:
                minimum_no_change_fee, _ = estimate_fee(
                    utxos,
                    destination,
                    resolved_fee_rate,
                    has_change=False,
                )
                if total_input < send_amount + minimum_no_change_fee:
                    logger.error("Insufficient funds after fee")
                    logger.bind(sensitive=True).error(
                        "Insufficient funds after fee: "
                        f"need {send_amount + minimum_no_change_fee:,}"
                    )
                    raise typer.Exit(1)
                # With no change output, every satoshi not sent is the actual fee.
                estimated_fee = total_input - send_amount
                change_amount = 0

        # Use new format_amount for display
        from jmcore.bitcoin import format_amount

        logger.bind(sensitive=True).info(f"Sending {format_amount(send_amount)} to {destination}")
        logger.info(f"Fee: {format_amount(estimated_fee)} ({resolved_fee_rate:.2f} sat/vB)")
        if change_amount > 0:
            logger.bind(sensitive=True).info(f"Change: {format_amount(change_amount)}")
        if allow_conflicts:
            logger.warning(
                "Conflict replacement is enabled. Recipient payments from conflicting mempool "
                "transactions may be invalidated."
            )

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
                    "Inputs": (
                        f"{len(utxos)} UTXO(s) across "
                        f"{len({utxo.scriptpubkey for utxo in utxos})} address(es)"
                    ),
                    "Change": format_amount(change_amount) if change_amount > 0 else "None",
                    "Miner Fee Rate": f"{resolved_fee_rate:.2f} sat/vB",
                    "Replace-By-Fee": "Enabled" if rbf else "Disabled",
                    **(
                        {
                            "Conflict Replacement": (
                                "Enabled. Recipient payments from conflicting mempool transactions "
                                "may be invalidated."
                            )
                        }
                        if allow_conflicts
                        else {}
                    ),
                },
                skip_confirmation=skip_confirmation,
            )
        except RuntimeError as e:
            logger.error("Transaction confirmation failed")
            logger.bind(sensitive=True).error(str(e))
            raise typer.Exit(1)
        if not confirmed:
            logger.info("Transaction cancelled by user")
            raise typer.Exit(1)

        # Resolve scripts and policy before the shared builder signs anything.
        from bitcointx import ChainParams
        from bitcointx.wallet import CCoinAddress, CCoinAddressError

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
            logger.error("Invalid address (bad checksum, format, or wrong network)")
            logger.bind(sensitive=True).error(
                f"Invalid address (bad checksum, format, or wrong network): {destination}"
            )
            raise typer.Exit(1)

        outputs = [
            DirectTxOutput(
                value_sats=send_amount,
                script_pubkey=dest_script,
                address=destination,
            )
        ]
        change_addr = ""
        if change_amount > 0:
            change_addr = wallet.get_new_internal_address(mixdepth)
            change_key = wallet.get_key_for_address(change_addr)
            if not change_key:
                logger.error(
                    "Failed to derive change key for selected change address; "
                    "cannot build a safe transaction"
                )
                raise typer.Exit(1)

            # Derive the script from the wallet's own change address instead of
            # a hardcoded P2WPKH template, so a Taproot (BIP86) wallet does not
            # pay change to a script its tr() descriptors cannot see.
            try:
                with ChainParams(chain):
                    change_script = bytes(CCoinAddress(change_addr).to_scriptPubKey())
            except CCoinAddressError as exc:
                logger.error(
                    "Failed to encode change address script; cannot build a safe transaction"
                )
                logger.bind(sensitive=True).error(f"Invalid change address {change_addr}: {exc}")
                raise typer.Exit(1) from exc

            outputs.append(
                DirectTxOutput(
                    value_sats=change_amount,
                    script_pubkey=change_script,
                    address=change_addr,
                )
            )

        try:
            locktime = await resolve_direct_send_locktime(
                backend=backend,
                utxos=utxos,
                locktime_cutoff=locktime_cutoff,
            )
            built = build_and_sign_direct_tx(
                wallet=wallet,
                utxos=utxos,
                outputs=outputs,
                locktime=locktime,
                rbf=rbf,
            )
        except (TransactionSigningError, ValueError) as exc:
            logger.error("Transaction construction or signing failed")
            logger.bind(sensitive=True).error(str(exc))
            raise typer.Exit(1) from exc

        utxos = built.inputs
        tx_hex = built.raw.hex()
        print(f"\nSigned Transaction ({len(built.raw)} bytes):")
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
        append_history_entry(send_entry, data_dir=backend_settings.data_dir)
        history_persisted = True

        if allow_conflicts:
            try:
                policy_result = await backend.test_mempool_accept(tx_hex)
                if policy_result.allowed is not True:
                    failure_reason = _mempool_policy_failure_reason(policy_result)
                    _finalize_send_history_entry(
                        send_entry,
                        txid="",
                        success=False,
                        failure_reason=failure_reason,
                        data_dir=backend_settings.data_dir,
                        history_persisted=history_persisted,
                    )
                    logger.error("Bitcoin Core mempool policy rejected the transaction")
                    logger.bind(sensitive=True).error(
                        "Bitcoin Core mempool policy rejected: {}", failure_reason
                    )
                    raise typer.Exit(1)
                logger.info("Bitcoin Core mempool policy preflight accepted the replacement")
            except typer.Exit:
                raise
            except Exception as exc:
                failure_reason = f"Bitcoin Core testmempoolaccept unavailable or malformed: {exc}"
                _finalize_send_history_entry(
                    send_entry,
                    txid="",
                    success=False,
                    failure_reason=failure_reason,
                    data_dir=backend_settings.data_dir,
                    history_persisted=history_persisted,
                )
                logger.error("Bitcoin Core mempool policy preflight failed")
                logger.bind(sensitive=True).error(failure_reason)
                raise typer.Exit(1) from exc

        if broadcast:
            logger.info("Broadcasting transaction...")
            try:
                backend_txid = await backend.broadcast_transaction(tx_hex)
            except Exception:
                _finalize_send_history_entry(
                    send_entry,
                    txid="",
                    success=False,
                    failure_reason="broadcast failed",
                    data_dir=backend_settings.data_dir,
                    history_persisted=history_persisted,
                )
                raise
            txid = _resolve_broadcast_txid(tx_hex, backend_txid)
            print("\nTransaction broadcast successfully!")
            print(f"TXID: {txid}")
            _finalize_send_history_entry(
                send_entry,
                txid=txid,
                success=True,
                failure_reason="",
                data_dir=backend_settings.data_dir,
                history_persisted=history_persisted,
            )
        else:
            print("\nTransaction NOT broadcast (--no-broadcast set)")
            print(f"Full hex: {tx_hex}")

    finally:
        await wallet.close()
