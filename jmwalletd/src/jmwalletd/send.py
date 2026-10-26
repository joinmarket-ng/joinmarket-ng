"""Direct-send helper for jmwalletd.

Builds, signs, broadcasts, and records direct (non-coinjoin) transactions
in history.csv following the two-phase history persistence pattern.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from jmwallet.history import (
    append_history_entry,
    create_send_history_entry,
    update_send_awaiting_broadcast,
)
from jmwallet.wallet.spend import (
    DEFAULT_MAX_FEE_RATE_SAT_VB,
    DirectSendResult,
    prepare_direct_send,
    resolve_broadcast_txid,
)

if TYPE_CHECKING:
    from jmwallet.wallet.service import WalletService


async def do_direct_send(
    *,
    wallet_service: WalletService,
    mixdepth: int,
    amount_sats: int,
    destination: str,
    fee_rate: float | None = None,
    fee_target_blocks: int | None = None,
    tx_fee_factor: float = 0.0,
    max_fee_rate_sat_vb: float = DEFAULT_MAX_FEE_RATE_SAT_VB,
    input_utxos: list[str] | None = None,
    rbf: bool = True,
) -> DirectSendResult:
    """Build, sign, broadcast, and record a direct (non-coinjoin) transaction.

    ``fee_rate`` (sat/vB) takes priority; otherwise ``fee_target_blocks``
    drives backend estimation (``None`` keeps the spend module's default
    target). ``tx_fee_factor`` applies the reference fee-rate randomization,
    and ``max_fee_rate_sat_vb`` applies the operator's configured hard cap.
    ``input_utxos``, when given, spends exactly those ``txid:vout`` outpoints
    instead of running automatic coin selection (issue #587); see
    :func:`jmwallet.wallet.spend.prepare_direct_send` for the validation
    rules. ``rbf`` defaults to BIP125 replacement signaling and can be disabled
    per transaction without disabling anti-fee-sniping locktime.

    Follows the same two-phase history-write pattern as the CLI send command:

    1. A ``role="send"`` row is written to ``history.csv`` with
       ``failure_reason="awaiting broadcast"`` **before** the broadcast call.
       This permanently marks destination and change addresses as used,
       even if broadcast fails — the signed bytes are already outside
       the wallet's control.
    2. After broadcast resolves (success or failure) the row is patched
       in-place via :func:`~jmwallet.history.update_send_awaiting_broadcast`.

    A pre-broadcast persistence failure aborts before broadcasting. Finalization
    failures remain best-effort because the transaction may already be public.
    """
    from jmcore.paths import get_default_data_dir
    from jmwalletd._backend import get_backend

    data_dir: Path = wallet_service.data_dir or get_default_data_dir()
    backend = await get_backend(data_dir, wallet_service=wallet_service)

    # Preserve fidelity bond UTXOs on every backend. Plain sync omits branch 2
    # on address-scanning backends and can drop the bond immediately before an
    # expired-bond sweep.
    await wallet_service.sync_with_registered_bonds()

    logger.bind(sensitive=True).info(
        "Direct send: {} sats from mixdepth {} to {} (max fee rate {:.2f} sat/vB)",
        amount_sats or "sweep",
        mixdepth,
        destination,
        max_fee_rate_sat_vb,
    )

    extra_kwargs: dict[str, int] = {}
    if fee_target_blocks is not None:
        extra_kwargs["fee_target_blocks"] = fee_target_blocks

    # --- Phase 1: build + sign (no broadcast yet) ---
    prepared = await prepare_direct_send(
        wallet=wallet_service,
        backend=backend,
        mixdepth=mixdepth,
        amount_sats=amount_sats,
        destination=destination,
        fee_rate=fee_rate,
        tx_fee_factor=tx_fee_factor,
        max_fee_rate_sat_vb=max_fee_rate_sat_vb,
        input_utxos=input_utxos,
        rbf=rbf,
        **extra_kwargs,
    )

    # --- Phase 1b: persist pending history row BEFORE broadcast ---
    network: str = getattr(wallet_service, "network", "mainnet")
    wallet_fingerprint: str = getattr(wallet_service, "wallet_fingerprint", "") or getattr(
        wallet_service, "fingerprint", ""
    )
    send_entry = create_send_history_entry(
        destination=prepared.destination,
        change_address=prepared.change_address,
        amount=prepared.send_amount,
        mining_fee=prepared.fee,
        source_mixdepth=mixdepth,
        selected_utxos=prepared.selected_utxos,
        txid="",
        success=False,
        failure_reason="awaiting broadcast",
        network=network,
        wallet_fingerprint=wallet_fingerprint,
        source_addresses=prepared.source_addresses,
    )
    append_history_entry(send_entry, data_dir=data_dir)
    history_persisted = True

    # --- Phase 2: broadcast ---
    txid = ""
    broadcast_exc: Exception | None = None
    try:
        logger.bind(sensitive=True).info(
            "Broadcasting direct-send transaction ({} bytes)",
            len(bytes.fromhex(prepared.tx_hex)),
        )
        broadcast_txid = await backend.broadcast_transaction(prepared.tx_hex)
        txid = resolve_broadcast_txid(
            prepared.tx_hex,
            broadcast_txid,
            local_txid=prepared.txid,
        )
        logger.bind(sensitive=True).info("Broadcast OK: {}", txid)
    except Exception as exc:
        broadcast_exc = exc
        logger.error("Broadcast failed")
        logger.bind(sensitive=True).error("Broadcast failed: {}", exc)

    # --- Phase 3: finalize history row (success or failure) ---
    if history_persisted:
        try:
            updated = update_send_awaiting_broadcast(
                send_entry,
                txid=txid,
                success=broadcast_exc is None,
                failure_reason=""
                if broadcast_exc is None
                else f"broadcast failed: {broadcast_exc}",
                data_dir=data_dir,
            )
            if not updated:
                logger.warning("Could not find pre-broadcast send history entry to finalize")
        except Exception as exc:
            logger.warning("Failed to finalize send history entry")
            logger.bind(sensitive=True).warning("Failed to finalize send history entry: {}", exc)

    if broadcast_exc is not None:
        raise broadcast_exc

    return DirectSendResult(
        txid=txid,
        tx_hex=prepared.tx_hex,
        fee=prepared.fee,
        fee_rate=prepared.fee_rate,
        send_amount=prepared.send_amount,
        change_amount=prepared.change_amount,
        num_inputs=prepared.num_inputs,
        num_outputs=prepared.num_outputs,
        inputs=prepared.inputs,
        outputs=prepared.outputs,
        version=prepared.version,
        locktime=prepared.locktime,
    )
