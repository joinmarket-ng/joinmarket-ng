"""
On-chain CoinJoin history reconstruction for imported wallets.

A wallet recovered from seed has no local ``history.csv``: every past
CoinJoin, send, and deposit is invisible to ``jm-wallet history`` even though
the transactions are fully recoverable from chain data. This module rebuilds a
best-effort history the same way the legacy joinmarket-clientserver
``wallet-tool history`` command classifies transactions on the fly, but
persists the result as regular history rows tagged ``source="onchain"`` so
they are clearly distinguishable from authoritative protocol-time rows.

Heuristics (all pure functions of on-chain data):

- A transaction is a CoinJoin when its output structure matches the
  equal-output pattern (:func:`jmcore.bitcoin.analyze_coinjoin_outputs`).
- Our role in a CoinJoin follows from the net value change of our coins:
  a maker earns a fee (``net >= 0``), a taker pays fees (``net < 0``).
- Maker ``fee_received`` is the net gain (cjfee earned minus the mining-fee
  contribution; the two cannot be separated on-chain).
- Taker fees lump maker fees and our mining-fee share together in
  ``total_maker_fees_paid`` (again inseparable on-chain).
- Peer counts follow the protocol-row conventions: taker rows count the
  makers (equal outputs minus our own), maker rows count all equal outputs
  (matching ``detect_coinjoin_peer_count``).
- Non-CoinJoin transactions become ``send`` (we funded inputs) or
  ``deposit`` (we only received) rows.

Known limitations: counterparty nicks are unknowable; a maker whose cjfee was
smaller than its mining-fee contribution is misclassified as taker; the
per-transaction mining fee of a CoinJoin cannot be split from maker fees.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from jmcore.bitcoin import (
    ParsedTransaction,
    address_to_scriptpubkey,
    analyze_coinjoin_outputs,
    parse_transaction,
    scriptpubkey_to_address,
)
from loguru import logger

from jmwallet.history import (
    HistoryRole,
    TransactionHistoryEntry,
    append_history_entry,
    read_history,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from jmwallet.backends.base import BlockchainBackend, WalletTxEntry


@dataclass
class OwnedInput:
    """One of our coins spent by a transaction (prevout resolved locally)."""

    txid: str
    vout: int
    value: int
    address: str
    mixdepth: int


@dataclass
class OwnedOutput:
    """One of our coins created by a transaction."""

    vout: int
    value: int
    address: str
    mixdepth: int
    is_external: bool


@dataclass
class ClassifiedTransaction:
    """Result of classifying a single wallet transaction from chain data."""

    role: HistoryRole
    cj_amount: int
    peer_count: int | None
    fee_received: int
    total_maker_fees_paid: int
    mining_fee_paid: int
    net_fee: int
    source_mixdepth: int
    destination_address: str
    change_address: str
    utxos_used: str
    source_addresses: str


@dataclass
class ReconstructionResult:
    """Outcome of a reconstruction pass."""

    scanned: int = 0
    created: int = 0
    skipped_existing: int = 0
    capped: bool = False


def classify_wallet_transaction(
    parsed: ParsedTransaction,
    owned_inputs: list[OwnedInput],
    owned_outputs: list[OwnedOutput],
    all_inputs_ours: bool,
    network: str = "mainnet",
) -> ClassifiedTransaction | None:
    """Classify a wallet transaction into a history role with inferred fees.

    Args:
        parsed: The parsed transaction.
        owned_inputs: Our inputs (prevouts resolved to our addresses).
        owned_outputs: Our outputs.
        all_inputs_ours: True when every input of the transaction spends one
            of our coins (lets us compute the exact mining fee for sends).
        network: Network name (for foreign-script address rendering).

    Returns:
        A :class:`ClassifiedTransaction`, or ``None`` when the transaction
        does not involve the wallet at all.
    """
    if not owned_inputs and not owned_outputs:
        return None

    analysis = analyze_coinjoin_outputs(parsed.outputs)
    total_output_value = sum(out.value for out in parsed.outputs)
    our_input_value = sum(i.value for i in owned_inputs)
    our_output_value = sum(o.value for o in owned_outputs)
    net = our_output_value - our_input_value

    utxos_used = ",".join(f"{i.txid}:{i.vout}" for i in owned_inputs)
    source_addresses = ",".join(i.address for i in owned_inputs)
    source_mixdepth = (
        Counter(i.mixdepth for i in owned_inputs).most_common(1)[0][0] if owned_inputs else 0
    )

    if not owned_inputs:
        # Incoming payment. This includes receiving the equal-amount output of
        # someone else's CoinJoin (e.g. a taker in another wallet paying us).
        destination = next(
            (o.address for o in owned_outputs if o.is_external),
            owned_outputs[0].address,
        )
        return ClassifiedTransaction(
            role="deposit",
            cj_amount=our_output_value,
            peer_count=analysis.cj_count if analysis.is_coinjoin else None,
            fee_received=0,
            total_maker_fees_paid=0,
            mining_fee_paid=0,
            net_fee=0,
            source_mixdepth=0,
            destination_address=destination,
            change_address="",
            utxos_used="",
            source_addresses="",
        )

    if analysis.is_coinjoin:
        our_equal = [o for o in owned_outputs if o.value == analysis.cj_amount]
        our_other = [o for o in owned_outputs if o.value != analysis.cj_amount]
        change_address = our_other[0].address if our_other else ""

        if net >= 0:
            # We came out ahead: we earned a fee as maker. ``fee_received``
            # is net of our mining-fee contribution (inseparable on-chain).
            return ClassifiedTransaction(
                role="maker",
                cj_amount=analysis.cj_amount,
                # Matches the equal-output count that the maker confirmation
                # monitor backfills via ``detect_coinjoin_peer_count``.
                peer_count=analysis.cj_count,
                fee_received=net,
                total_maker_fees_paid=0,
                mining_fee_paid=0,
                net_fee=net,
                source_mixdepth=source_mixdepth,
                destination_address=our_equal[0].address if our_equal else "",
                change_address=change_address,
                utxos_used=utxos_used,
                source_addresses=source_addresses,
            )

        # We paid for the join: taker. The cost lumps maker fees and our
        # mining-fee share together (inseparable on-chain).
        cost = -net
        if our_equal:
            destination = our_equal[0].address
        else:
            # Sweep/payment to an external wallet: the equal-amount output is
            # not ours, so the cj_amount itself left the wallet on purpose and
            # only the remainder is fees.
            destination = ""
            cost = max(0, cost - analysis.cj_amount)
        return ClassifiedTransaction(
            role="taker",
            cj_amount=analysis.cj_amount,
            # Protocol taker rows record the number of *makers*; one of the
            # equal outputs is our own, so exclude it from the count.
            peer_count=max(0, analysis.cj_count - 1),
            fee_received=0,
            total_maker_fees_paid=cost,
            mining_fee_paid=0,
            net_fee=-cost,
            source_mixdepth=source_mixdepth,
            destination_address=destination,
            change_address=change_address,
            utxos_used=utxos_used,
            source_addresses=source_addresses,
        )

    # Plain (non-CoinJoin) spend from this wallet.
    owned_vouts = {o.vout for o in owned_outputs}
    foreign_outputs = [
        (idx, out) for idx, out in enumerate(parsed.outputs) if idx not in owned_vouts
    ]
    mining_fee = max(0, our_input_value - total_output_value) if all_inputs_ours else 0

    if foreign_outputs:
        amount = sum(out.value for _, out in foreign_outputs)
        _, largest = max(foreign_outputs, key=lambda item: item[1].value)
        try:
            destination = scriptpubkey_to_address(bytes(largest.script), network)
        except ValueError:
            destination = ""
        change_address = next((o.address for o in owned_outputs if not o.is_external), "")
    else:
        # Internal transfer: every output is ours. A single output is a sweep
        # to ourselves. With several outputs, the change returns to the source
        # mixdepth, so an output on a *different* mixdepth is the transfer
        # destination (JoinMarket internal transfers move coins across
        # mixdepths, typically to the internal branch, so branch alone cannot
        # identify the destination). External-branch and largest-value are
        # fallbacks for the ambiguous same-mixdepth case.
        if len(owned_outputs) == 1:
            dest_out = owned_outputs[0]
        else:
            cross_mixdepth = [o for o in owned_outputs if o.mixdepth != source_mixdepth]
            if cross_mixdepth:
                dest_out = max(cross_mixdepth, key=lambda o: o.value)
            else:
                dest_out = next(
                    (o for o in owned_outputs if o.is_external),
                    max(owned_outputs, key=lambda o: o.value),
                )
        destination = dest_out.address
        amount = dest_out.value
        change_candidates = [o for o in owned_outputs if o is not dest_out]
        change_address = next(
            (o.address for o in change_candidates if not o.is_external),
            change_candidates[0].address if change_candidates else "",
        )
    return ClassifiedTransaction(
        role="send",
        cj_amount=amount,
        peer_count=None,
        fee_received=0,
        total_maker_fees_paid=0,
        mining_fee_paid=mining_fee,
        net_fee=-mining_fee,
        source_mixdepth=source_mixdepth,
        destination_address=destination,
        change_address=change_address,
        utxos_used=utxos_used,
        source_addresses=source_addresses,
    )


async def reconstruct_history_from_chain(
    backend: BlockchainBackend,
    *,
    address_paths: Mapping[str, tuple[int, int, int]],
    network: str,
    wallet_fingerprint: str,
    data_dir: Path,
    max_transactions: int = 1000,
) -> ReconstructionResult:
    """Reconstruct history rows from chain data and persist them.

    Enumerates every confirmed wallet transaction via the backend's
    transaction enumeration (``listsinceblock`` on Bitcoin Core; the watched
    transaction log on neutrino-api 1.4.0+), classifies each with
    :func:`classify_wallet_transaction`, and appends a ``source="onchain"``
    row per transaction that is not already recorded for this wallet.
    Protocol-time rows always win: a txid already present in the wallet's
    history (whatever its source) is never touched (issue #517 policy).

    Args:
        backend: Blockchain backend (must support transaction enumeration).
        address_paths: Mapping of wallet address -> ``(mixdepth, change,
            index)`` used to recognize our scripts (typically
            ``WalletService.address_cache`` after a sync).
        network: Network name recorded on the entries.
        wallet_fingerprint: Wallet scoping fingerprint for the entries.
        data_dir: Data directory holding ``history.csv``.
        max_transactions: Safety cap on transactions classified in one pass.

    Returns:
        A :class:`ReconstructionResult` with pass statistics.
    """
    if max_transactions < 1:
        raise ValueError("max_transactions must be at least 1")

    result = ReconstructionResult()
    if not getattr(backend, "supports_tx_enumeration", False):
        logger.debug("Backend does not support tx enumeration; skipping history reconstruction")
        return result

    wallet_entries, _cursor = await backend.list_wallet_transactions_since(None)
    if not wallet_entries:
        return result

    # Deduplicate by txid, preferring the deepest-confirmed record.
    by_txid: dict[str, WalletTxEntry] = {}
    for entry in wallet_entries:
        existing = by_txid.get(entry.txid)
        if existing is None or entry.confirmations > existing.confirmations:
            by_txid[entry.txid] = entry
    wallet_txids = set(by_txid)

    known_txids = {
        e.txid for e in read_history(data_dir, wallet_fingerprint=wallet_fingerprint) if e.txid
    }

    # Only confirmed transactions: mempool activity is the live flows'
    # responsibility, and unconfirmed history rows would fight the pending
    # transaction monitors.
    candidates = [e for e in by_txid.values() if e.confirmations > 0]
    candidates.sort(
        key=lambda e: (e.block_height if e.block_height is not None else 1 << 62, e.txid)
    )

    # Map our scriptPubKeys to (address, mixdepth, is_external).
    script_owner: dict[bytes, tuple[str, int, bool]] = {}
    for address, (mixdepth, change, _index) in address_paths.items():
        try:
            script_owner[address_to_scriptpubkey(address)] = (address, mixdepth, change == 0)
        except Exception:  # pragma: no cover - malformed cache entries
            continue

    parsed_cache: dict[str, ParsedTransaction | None] = {}
    tx_block_time: dict[str, int] = {}
    height_time_cache: dict[int, int] = {}

    async def get_parsed(txid: str) -> ParsedTransaction | None:
        """Parse a wallet tx, preferring inline raw hex from the enumeration."""
        if txid in parsed_cache:
            return parsed_cache[txid]
        raw = by_txid[txid].raw if txid in by_txid else ""
        if not raw:
            try:
                tx = await backend.get_transaction(txid)
            except Exception as exc:  # pragma: no cover - backend dependent
                logger.debug(f"Could not fetch tx {txid[:16]}...: {exc}")
                tx = None
            if tx is not None:
                raw = tx.raw
                if tx.block_time:
                    tx_block_time[txid] = tx.block_time
        parsed: ParsedTransaction | None = None
        if raw:
            try:
                parsed = parse_transaction(raw)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"Could not parse tx {txid[:16]}...: {exc}")
        parsed_cache[txid] = parsed
        return parsed

    async def get_timestamp(entry: WalletTxEntry) -> str:
        """Best-effort ISO timestamp for a confirmed wallet transaction."""
        block_time = tx_block_time.get(entry.txid)
        if block_time is None and entry.block_height is not None:
            if entry.block_height in height_time_cache:
                block_time = height_time_cache[entry.block_height]
            else:
                try:
                    block_time = await backend.get_block_time(entry.block_height)
                    height_time_cache[entry.block_height] = block_time
                except Exception:  # pragma: no cover - backend dependent
                    block_time = None
        if block_time:
            return datetime.fromtimestamp(block_time).isoformat()
        return datetime.now().isoformat()

    for entry in candidates:
        if entry.txid in known_txids:
            result.skipped_existing += 1
            continue
        if result.scanned >= max_transactions:
            result.capped = True
            logger.warning(
                f"History reconstruction hit the {max_transactions}-transaction cap; "
                "run `jm-wallet reconstruct-history --keep-existing` to continue "
                "without rebuilding the same rows."
            )
            break

        parsed = await get_parsed(entry.txid)
        result.scanned += 1
        if parsed is None:
            continue

        owned_outputs: list[OwnedOutput] = []
        for vout, out in enumerate(parsed.outputs):
            owner = script_owner.get(bytes(out.script))
            if owner is not None:
                address, mixdepth, is_external = owner
                owned_outputs.append(
                    OwnedOutput(
                        vout=vout,
                        value=out.value,
                        address=address,
                        mixdepth=mixdepth,
                        is_external=is_external,
                    )
                )

        owned_inputs: list[OwnedInput] = []
        all_inputs_ours = True
        for tin in parsed.inputs:
            prev_txid = tin.txid
            # A prevout paying this wallet necessarily belongs to a wallet
            # transaction, so any prev txid outside the enumeration set is
            # someone else's coin.
            if prev_txid not in wallet_txids:
                all_inputs_ours = False
                continue
            prev_parsed = await get_parsed(prev_txid)
            if prev_parsed is None or tin.vout >= len(prev_parsed.outputs):
                all_inputs_ours = False
                continue
            prev_out = prev_parsed.outputs[tin.vout]
            owner = script_owner.get(bytes(prev_out.script))
            if owner is None:
                all_inputs_ours = False
                continue
            owned_inputs.append(
                OwnedInput(
                    txid=prev_txid,
                    vout=tin.vout,
                    value=prev_out.value,
                    address=owner[0],
                    mixdepth=owner[1],
                )
            )

        classified = classify_wallet_transaction(
            parsed, owned_inputs, owned_outputs, all_inputs_ours, network
        )
        if classified is None:
            continue

        timestamp = await get_timestamp(entry)
        history_entry = TransactionHistoryEntry(
            timestamp=timestamp,
            completed_at=timestamp,
            role=classified.role,
            success=True,
            failure_reason="",
            confirmations=entry.confirmations,
            confirmed_at=timestamp,
            txid=entry.txid,
            cj_amount=classified.cj_amount,
            peer_count=classified.peer_count,
            counterparty_nicks="",
            fee_received=classified.fee_received,
            txfee_contribution=0,
            total_maker_fees_paid=classified.total_maker_fees_paid,
            mining_fee_paid=classified.mining_fee_paid,
            net_fee=classified.net_fee,
            source_mixdepth=classified.source_mixdepth,
            destination_address=classified.destination_address,
            change_address=classified.change_address,
            utxos_used=classified.utxos_used,
            source_addresses=classified.source_addresses,
            broadcast_method="",
            network=network,
            wallet_fingerprint=wallet_fingerprint,
            source="onchain",
        )
        append_history_entry(history_entry, data_dir)
        result.created += 1

    if result.created:
        logger.info(
            f"Reconstructed {result.created} history entries from "
            f"{result.scanned} on-chain transaction(s)."
        )
    return result
