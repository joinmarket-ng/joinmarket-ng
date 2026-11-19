"""Background transaction monitor.

Polls the wallet backend for new transactions and pushes a WebSocket
notification (``{"txid": ..., "txdetails": {...}}``) for each, so Jam and other
clients can refresh balances without polling (issue #560). Unlike the reference
implementation -- which only announced direct-send transactions -- this covers
every wallet transaction: external deposits, maker/taker coinjoins, and sends.

A transaction is announced twice at most: once when first seen (in the
mempool) and once when it first confirms (the ``txdetails`` then carries a
``confirmations`` count). The direct-send endpoint announces its own
transaction inline and marks it so the monitor does not duplicate the
first-seen notification (it still reports the later confirmation).

The monitor also records confirmations it observes in this wallet's
``history.csv`` (issue #672). A one-shot CoinJoin started through the API tears
down its taker, and with it the taker's confirmation monitor, right after
broadcast, so without this its history row would stay pending after the
transaction confirms. The silent first pass reconciles rows whose transaction
confirmed while the daemon was stopped or the wallet was locked.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from jmwalletd.txinfo import build_txinfo_from_hex

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from jmwalletd.state import DaemonState

_POLL_INTERVAL_SEC = 5.0


async def run_tx_monitor(
    state: DaemonState,
    poll_interval: float = _POLL_INTERVAL_SEC,
    ready: asyncio.Event | None = None,
    initialization_task: asyncio.Task[None] | None = None,
    baseline_existing: bool = True,
) -> None:
    """Poll the backend and broadcast a notification per new/confirmed tx.

    Runs until cancelled (on wallet lock). The first pass establishes a silent
    baseline of pre-existing transactions so only activity that occurs after
    the wallet is loaded is announced.
    """
    from jmwalletd.wallet_ops import _get_network

    ws = state.wallet_service
    if ws is None:
        return
    backend = ws.backend
    network = _get_network()

    # Neutrino registers watch addresses during wallet sync. Enumerating before
    # that task completes can establish an incomplete baseline and later hide
    # transactions discovered by the rescan as if they predated this session.
    if initialization_task is not None:
        await asyncio.shield(initialization_task)

    cursor: str | None = None
    confirmed_notified: set[str] = set()
    first_pass = baseline_existing
    if not first_pass and ready is not None:
        ready.set()

    while True:
        try:
            entries, next_cursor = await backend.list_wallet_transactions_since(cursor)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - transient RPC issues
            logger.debug(f"tx monitor: enumeration failed: {exc}")
            await asyncio.sleep(poll_interval)
            continue

        confirmed = {
            entry.txid: entry.confirmations for entry in entries if entry.confirmations > 0
        }
        notification_failed = False
        for entry in entries:
            txid = entry.txid
            confirmations = entry.confirmations

            if first_pass:
                # Baseline: mark pre-existing transactions as seen without
                # announcing them, so we only notify on activity after load.
                state._tx_broadcast_notified.add(txid)
                if confirmations >= 1:
                    confirmed_notified.add(txid)
                continue

            try:
                if txid not in state._tx_broadcast_notified:
                    notified = await _notify(
                        state,
                        backend,
                        network,
                        txid,
                        confirmations,
                        entry.raw,
                        suppress_if_first_seen=True,
                    )
                    if notified:
                        state._tx_broadcast_notified.add(txid)
                        if confirmations >= 1:
                            confirmed_notified.add(txid)
                    else:
                        notification_failed = True
                elif confirmations >= 1 and txid not in confirmed_notified:
                    if await _notify(state, backend, network, txid, confirmations, entry.raw):
                        confirmed_notified.add(txid)
                    else:
                        notification_failed = True
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("tx monitor: failed to notify transaction")
                logger.bind(sensitive=True).debug(
                    f"tx monitor: failed to notify {txid[:16]}...: {exc}"
                )
                notification_failed = True

        # A confirmed transaction may only appear once for a given backend
        # cursor. Keep the old cursor when notification construction failed so
        # the backend returns that transaction again on the next poll.
        if not notification_failed:
            cursor = next_cursor

        # The baseline is only complete once the backend actually enumerated
        # (a non-None cursor). Before the wallet is set up/loaded the backend
        # returns an empty list and a None cursor; staying in ``first_pass``
        # until then avoids announcing the wallet's entire history the moment
        # it finishes loading.
        if first_pass and cursor is not None:
            first_pass = False
            if ready is not None:
                ready.set()

        # After readiness so history I/O and maker peer-count lookups never
        # delay unlock.
        if confirmed:
            await _reconcile_history(state, backend, confirmed)
        await asyncio.sleep(poll_interval)


async def _reconcile_history(
    state: DaemonState,
    backend: BlockchainBackend,
    confirmed: dict[str, int],
) -> None:
    """Mark this wallet's unresolved history rows confirmed by the backend.

    Only recorded txids that the backend reports confirmed are updated, which
    matches the explicit ``jm-wallet info`` refresh: failed rows with a txid
    (e.g. a local monitoring timeout) are repaired too, because a local outcome
    cannot prove a signed transaction never confirmed. Rows are never created.

    A running maker (directly or inside a tumble) owns its pending rows: its
    own monitor sends the confirmation notification and detects the peer
    count, so this monitor leaves them alone until the maker stops.
    """
    from jmwallet.history import (
        get_pending_transactions,
        update_transaction_confirmation_with_detection,
    )
    from jmwalletd.state import CoinjoinState

    ws = state.wallet_service
    fingerprint = getattr(ws, "wallet_fingerprint", "") if ws is not None else ""
    if not fingerprint:
        # Unscoped updates could touch another wallet's rows in a shared data dir.
        return

    try:
        unresolved = get_pending_transactions(
            state.data_dir, wallet_fingerprint=fingerprint, include_failed=True
        )
        maker_active = state.coinjoin_state in (
            CoinjoinState.MAKER_RUNNING,
            CoinjoinState.TUMBLER_RUNNING,
        )
        for row in unresolved:
            confirmations = confirmed.get(row.txid) if row.txid else None
            if confirmations is None:
                continue
            if row.role == "maker" and maker_active and not row.completed_at:
                continue
            await update_transaction_confirmation_with_detection(
                txid=row.txid,
                confirmations=confirmations,
                backend=backend,
                data_dir=state.data_dir,
                wallet_fingerprint=fingerprint,
            )
            logger.info("tx monitor: recorded history confirmation")
            logger.bind(sensitive=True).info(
                f"tx monitor: marked {row.txid[:16]}... confirmed in history "
                f"({confirmations} confirmation(s))"
            )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("tx monitor: failed to record history confirmations")
        logger.bind(sensitive=True).warning(
            f"tx monitor: history confirmation update failed: {exc}"
        )


async def _notify(
    state: DaemonState,
    backend: BlockchainBackend,
    network: str,
    txid: str,
    confirmations: int,
    raw: str = "",
    *,
    suppress_if_first_seen: bool = False,
) -> bool:
    # Prefer the raw hex supplied inline by the backend (e.g. neutrino's
    # /v1/transactions, whose confirmed txs are not fetchable via
    # get_transaction); otherwise fetch it (full-node backends).
    tx_hex = raw
    if not tx_hex:
        tx = await backend.get_transaction(txid)
        if tx is None or not tx.raw:
            return False
        tx_hex = tx.raw
    txinfo = build_txinfo_from_hex(tx_hex, network, txid=txid, confirmations=confirmations)
    # Direct-send can broadcast while this coroutine is awaiting the backend.
    # Re-check immediately before the synchronous websocket send to avoid a
    # duplicate first-seen frame.
    if suppress_if_first_seen and txid in state._tx_broadcast_notified:
        return True
    state.broadcast_ws({"txid": txinfo.txid, "txdetails": txinfo.model_dump()})
    logger.debug(f"tx monitor: notification sent (confirmations={confirmations})")
    logger.bind(sensitive=True).debug(
        f"tx monitor: notified {txid[:16]}... (confirmations={confirmations})"
    )
    return True
