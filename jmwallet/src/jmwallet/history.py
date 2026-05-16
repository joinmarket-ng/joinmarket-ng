"""
Transaction history tracking for CoinJoin operations.

Stores a simple CSV log of all CoinJoin transactions with key metadata:
- Role (maker/taker)
- Fees (paid/received)
- Peer count (only known by takers; None for makers)
- Transaction details
"""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Mapping
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from jmcore.paths import get_default_data_dir
from loguru import logger
from pydantic.dataclasses import dataclass

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend


# Once a pending transaction reaches this many confirmations we stop polling
# it from the background monitors. The first confirmation already flips
# ``success`` to True (see ``update_transaction_confirmation*``), but this
# upper bound is a safety net for ghost/duplicate rows that – for whatever
# reason – were never finalized: there is no privacy or accounting value in
# polling the same already-deeply-confirmed txid forever.
PENDING_CONFIRMATION_TRACKING_MAX = 6


class HistoryWriteError(Exception):
    """Raised when a history entry cannot be persisted to disk."""


@dataclass
class TransactionHistoryEntry:
    """A single CoinJoin transaction record."""

    # Timestamps
    timestamp: str  # ISO format
    completed_at: str = ""  # ISO format

    # Role and outcome
    role: Literal["maker", "taker"] = "taker"
    success: bool = True
    failure_reason: str = ""

    # Confirmation tracking
    confirmations: int = 0  # Number of confirmations (0 = unconfirmed/pending)
    confirmed_at: str = ""  # ISO format - when first confirmation was seen

    # Core transaction data
    txid: str = ""
    cj_amount: int = 0  # satoshis

    # Peer information
    peer_count: int | None = None  # None for makers (unknown), count for takers
    counterparty_nicks: str = ""  # comma-separated

    # Fee information (in satoshis)
    fee_received: int = 0  # Only for makers - cjfee earned
    txfee_contribution: int = 0  # Mining fee contribution
    total_maker_fees_paid: int = 0  # Only for takers
    mining_fee_paid: int = 0  # Only for takers

    # Net profit/cost
    net_fee: int = 0  # Positive = profit, negative = cost

    # UTXO/address info
    source_mixdepth: int = 0
    destination_address: str = ""
    change_address: str = ""  # Change output address (must also be blacklisted!)
    utxos_used: str = ""  # comma-separated txid:vout

    # Broadcast method
    broadcast_method: str = ""  # "self", "maker:<nick>", etc.

    # Network
    network: str = "mainnet"

    # Wallet identity (issue #473): scopes the entry to a specific wallet so
    # that switching wallets from the same data directory does not surface
    # another wallet's history (and especially not its phantom pending
    # transactions). The value is the BIP32 master m/0 fingerprint hex
    # (8 chars) of the wallet that produced the entry. Defaults to an empty
    # string for backwards compatibility with pre-existing CSV files written
    # before this column existed; legacy rows are treated as belonging to no
    # known wallet and are therefore filtered out when a wallet filter is
    # active.
    wallet_fingerprint: str = ""


def _get_history_path(data_dir: Path | None = None) -> Path:
    """
    Get the path to the history CSV file.

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())

    Returns:
        Path to coinjoin_history.csv in the data directory
    """
    if data_dir is None:
        data_dir = get_default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "coinjoin_history.csv"


def _get_fieldnames() -> list[str]:
    """Get the list of field names for the CSV."""
    return [f.name for f in fields(TransactionHistoryEntry)]


def _read_csv_header(history_path: Path) -> list[str] | None:
    """Return the header (first row) of the CSV file, or None if absent/empty."""
    if not history_path.exists():
        return None
    try:
        with open(history_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            try:
                return next(reader)
            except StopIteration:
                return None
    except OSError as e:
        logger.warning(f"Could not read history header for migration check: {e}")
        return None


def _ensure_history_header_current(history_path: Path) -> None:
    """Migrate a legacy ``coinjoin_history.csv`` to the current header.

    When the dataclass gains a new field (for example
    ``wallet_fingerprint`` introduced for issue #473), an existing CSV
    written by an older version still has the old header. ``DictWriter``
    happily appends rows whose dict has extra keys not in the on-disk
    header, producing rows with more cells than columns. ``DictReader``
    then drops the extra cells into a ``None``-keyed catch-all and the
    new field silently reads back as empty — which broke per-wallet
    pending lookups, leaving makers' confirmed CoinJoins stuck on
    ``success=False`` and the daily summary reporting zero successful
    rounds.

    This helper detects a stale header and rewrites the file in place
    (preserving every row, with missing columns defaulted) so callers
    never have to know the file was upgraded. Idempotent and cheap
    when the header already matches.
    """
    expected = _get_fieldnames()
    actual = _read_csv_header(history_path)
    if actual is None or actual == expected:
        return

    missing = [name for name in expected if name not in actual]
    if not missing:
        # Header has all expected columns (perhaps reordered); leave it.
        return

    logger.info(
        f"Migrating coinjoin history CSV: adding columns {missing} "
        f"(legacy {len(actual)}-column header detected)"
    )
    # Read raw rows assuming the *expected* layout so cells previously
    # written past the legacy header (e.g. wallet_fingerprint appended
    # by a 0.28.x writer against a 0.27.x header) are recovered.
    raw_rows: list[dict[str, str]] = []
    try:
        with open(history_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            try:
                next(reader)  # skip stale header
            except StopIteration:
                return
            for cells in reader:
                if not cells:
                    continue
                # Map cells positionally to the legacy header where they
                # exist; any trailing cells correspond to columns appended
                # by a newer writer (in dataclass declaration order).
                row: dict[str, str] = {}
                for idx, cell in enumerate(cells):
                    if idx < len(actual):
                        row[actual[idx]] = cell
                    else:
                        # Trailing cells: assign to the next missing column
                        offset = idx - len(actual)
                        if offset < len(missing):
                            row[missing[offset]] = cell
                raw_rows.append(row)
    except OSError as e:
        raise HistoryWriteError(f"Failed to read {history_path} for migration: {e}") from e

    entries: list[TransactionHistoryEntry] = [
        e for e in (_row_to_entry(r) for r in raw_rows) if e is not None
    ]
    if not _write_history_entries_atomic(entries, history_path):
        raise HistoryWriteError(f"Failed to migrate {history_path} to the current header layout")


def _row_to_entry(row: Mapping[str, str | None]) -> TransactionHistoryEntry | None:
    """Convert a CSV row dict to a TransactionHistoryEntry.

    Returns None when the row cannot be parsed (malformed). Tolerant of
    missing columns so legacy rows from earlier schema versions still
    parse with sensible defaults.
    """

    def _get(key: str, default: str = "") -> str:
        value = row.get(key, default)
        return value if value is not None else default

    try:
        return TransactionHistoryEntry(
            timestamp=_get("timestamp"),
            completed_at=_get("completed_at"),
            role=_get("role", "taker"),  # type: ignore[arg-type]
            success=_get("success", "True").lower() == "true",
            failure_reason=_get("failure_reason"),
            confirmations=int(_get("confirmations", "0") or 0),
            confirmed_at=_get("confirmed_at"),
            txid=_get("txid"),
            cj_amount=int(_get("cj_amount", "0") or 0),
            peer_count=(
                int(_get("peer_count"))
                if _get("peer_count") and _get("peer_count") not in ("", "None")
                else None
            ),
            counterparty_nicks=_get("counterparty_nicks"),
            fee_received=int(_get("fee_received", "0") or 0),
            txfee_contribution=int(_get("txfee_contribution", "0") or 0),
            total_maker_fees_paid=int(_get("total_maker_fees_paid", "0") or 0),
            mining_fee_paid=int(_get("mining_fee_paid", "0") or 0),
            net_fee=int(_get("net_fee", "0") or 0),
            source_mixdepth=int(_get("source_mixdepth", "0") or 0),
            destination_address=_get("destination_address"),
            change_address=_get("change_address"),
            utxos_used=_get("utxos_used"),
            broadcast_method=_get("broadcast_method"),
            network=_get("network", "mainnet"),
            wallet_fingerprint=_get("wallet_fingerprint"),
        )
    except (ValueError, KeyError) as e:
        logger.warning(f"Skipping malformed history row: {e}")
        return None


def append_history_entry(
    entry: TransactionHistoryEntry,
    data_dir: Path | None = None,
) -> None:
    """
    Append a transaction history entry to the CSV file.

    Args:
        entry: The transaction history entry to append
        data_dir: Optional data directory (defaults to get_default_data_dir())

    Raises:
        HistoryWriteError: If the entry cannot be written to disk.
    """
    history_path = _get_history_path(data_dir)
    fieldnames = _get_fieldnames()

    # Migrate legacy headers so new columns are not appended past the
    # on-disk header (which would otherwise silently lose data on read).
    _ensure_history_header_current(history_path)

    # Check if file exists to determine if we need to write header
    write_header = not history_path.exists()

    try:
        with open(history_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()

            # Convert entry to dict
            row = {f.name: getattr(entry, f.name) for f in fields(entry)}
            writer.writerow(row)

        logger.debug(f"Appended history entry: txid={entry.txid[:16]}... role={entry.role}")
    except Exception as e:
        raise HistoryWriteError(f"Failed to write history entry: {e}") from e


def _write_history_entries_atomic(
    entries: list[TransactionHistoryEntry], history_path: Path
) -> bool:
    """Rewrite history CSV atomically to avoid partial-file corruption.

    Entries are written in chronological order (oldest first) so that the
    on-disk file is consistently ordered and new entries appended via
    ``append_history_entry`` maintain that order.
    """
    fieldnames = _get_fieldnames()
    temp_path: Path | None = None

    # Write oldest-first so the file stays in chronological order and new
    # appends (which go to the end) remain consistent.
    sorted_entries = sorted(entries, key=lambda e: e.timestamp)

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            dir=history_path.parent,
            prefix=f"{history_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            writer = csv.DictWriter(temp_file, fieldnames=fieldnames)
            writer.writeheader()
            for entry in sorted_entries:
                row = {f.name: getattr(entry, f.name) for f in fields(entry)}
                writer.writerow(row)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, history_path)
        return True
    except Exception as e:
        logger.error(f"Failed to update history: {e}")
        return False
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def read_history(
    data_dir: Path | None = None,
    limit: int | None = None,
    role_filter: Literal["maker", "taker"] | None = None,
    wallet_fingerprint: str | None = None,
) -> list[TransactionHistoryEntry]:
    """
    Read transaction history from the CSV file.

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())
        limit: Maximum number of entries to return (most recent first)
        role_filter: Filter by role (maker/taker)
        wallet_fingerprint: If provided, only return entries belonging to this
            wallet (matched by their ``wallet_fingerprint`` column). Entries
            without a recorded fingerprint (legacy rows from before issue #473)
            are excluded from the filtered view to prevent another wallet's
            entries from leaking into the active wallet's history.

    Returns:
        List of TransactionHistoryEntry objects
    """
    history_path = _get_history_path(data_dir)

    if not history_path.exists():
        return []

    # Recover fingerprint cells appended past a stale legacy header so
    # per-wallet filters and updates work for pre-existing files even
    # when no new write has occurred yet.
    try:
        _ensure_history_header_current(history_path)
    except HistoryWriteError as e:
        # Migration is best-effort on read; keep going so the user can
        # still inspect history even if the file is read-only.
        logger.warning(f"History header migration failed during read: {e}")

    entries: list[TransactionHistoryEntry] = []

    try:
        with open(history_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                entry = _row_to_entry(row)
                if entry is None:
                    continue

                # Apply role filter
                if role_filter and entry.role != role_filter:
                    continue

                # Apply wallet filter (issue #473): legacy rows without a
                # recorded fingerprint do not match any specific wallet
                # and are therefore hidden from the per-wallet view.
                if wallet_fingerprint is not None:
                    if entry.wallet_fingerprint != wallet_fingerprint:
                        continue

                entries.append(entry)

    except Exception as e:
        logger.error(f"Failed to read history: {e}")
        return []

    # Sort by timestamp (most recent first) and apply limit
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    if limit:
        entries = entries[:limit]

    return entries


def _parse_utxos(utxos_used: str) -> set[str]:
    """Parse a comma-separated utxos_used string into a set of UTXO identifiers.

    Args:
        utxos_used: Comma-separated string of "txid:vout" pairs

    Returns:
        Set of UTXO identifier strings (empty set if input is empty)
    """
    if not utxos_used or not utxos_used.strip():
        return set()
    return set(utxos_used.split(","))


def _compute_stats(entries: list[TransactionHistoryEntry]) -> dict[str, int | float]:
    """
    Compute aggregate statistics from a list of history entries.

    Args:
        entries: List of TransactionHistoryEntry objects to aggregate

    Returns:
        Dict with statistics:
        - total_coinjoins: Total number of CoinJoins
        - maker_coinjoins: Number as maker
        - taker_coinjoins: Number as taker
        - successful_coinjoins: Number of successful CoinJoins
        - failed_coinjoins: Number of failed CoinJoins
        - total_volume: Total CJ amount in sats (all requests)
        - successful_volume: CJ amount in sats (successful only)
        - total_fees_earned: Total fees earned as maker (successful CoinJoins only).
              Failed rows can carry a non-zero ``fee_received`` because the
              fee is recorded at signing time before broadcast; those amounts
              are excluded because no coins actually moved.
        - total_fees_paid: Total fees paid as taker (successful CoinJoins only),
              for the same reason as ``total_fees_earned``.
        - success_rate: Percentage of successful CoinJoins
        - utxos_disclosed: Number of unique UTXOs disclosed to takers (via !ioauth).
              Deduplicated across entries so the same UTXO disclosed in multiple
              CoinJoin attempts is only counted once.
    """
    if not entries:
        return {
            "total_coinjoins": 0,
            "maker_coinjoins": 0,
            "taker_coinjoins": 0,
            "successful_coinjoins": 0,
            "failed_coinjoins": 0,
            "total_volume": 0,
            "successful_volume": 0,
            "total_fees_earned": 0,
            "total_fees_paid": 0,
            "success_rate": 0.0,
            "utxos_disclosed": 0,
        }

    maker_entries = [e for e in entries if e.role == "maker"]
    taker_entries = [e for e in entries if e.role == "taker"]
    successful = [e for e in entries if e.success]
    failed = [e for e in entries if not e.success and e.completed_at]
    # Fees are recorded at signing time (before broadcast), so failed entries
    # may carry a non-zero ``fee_received`` / ``total_maker_fees_paid`` that
    # never actually moved coins (for example, when the taker abandons after
    # collecting signatures and the transaction is never broadcast). Only
    # count fees from rows whose CoinJoin actually succeeded.
    successful_maker_entries = [e for e in maker_entries if e.success]
    successful_taker_entries = [e for e in taker_entries if e.success]

    # Collect all unique UTXOs disclosed across all entries.  The same UTXO may
    # appear in multiple CoinJoin attempts; users care about how many distinct
    # UTXOs external observers know about, not how many disclosure events occurred.
    all_disclosed: set[str] = set()
    for e in entries:
        all_disclosed |= _parse_utxos(e.utxos_used)

    return {
        "total_coinjoins": len(entries),
        "maker_coinjoins": len(maker_entries),
        "taker_coinjoins": len(taker_entries),
        "successful_coinjoins": len(successful),
        "failed_coinjoins": len(failed),
        "total_volume": sum(e.cj_amount for e in entries),
        "successful_volume": sum(e.cj_amount for e in successful),
        "total_fees_earned": sum(e.fee_received for e in successful_maker_entries),
        "total_fees_paid": sum(
            e.total_maker_fees_paid + e.mining_fee_paid for e in successful_taker_entries
        ),
        "success_rate": len(successful) / len(entries) * 100 if entries else 0.0,
        "utxos_disclosed": len(all_disclosed),
    }


def get_history_stats(
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> dict[str, int | float]:
    """
    Get aggregate statistics from transaction history.

    Args:
        data_dir: Optional data directory.
        wallet_fingerprint: If provided, restrict statistics to entries
            belonging to the given wallet (issue #473).

    Returns:
        Dict with statistics (see _compute_stats for full list).
    """
    entries = read_history(data_dir, wallet_fingerprint=wallet_fingerprint)
    return _compute_stats(entries)


def get_history_stats_for_period(
    hours: float,
    role_filter: Literal["maker", "taker"] | None = None,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> dict[str, int | float]:
    """
    Get aggregate statistics for a specific time period.

    Filters history entries to only include those within the last `hours` hours,
    then computes the same aggregate statistics as get_history_stats().

    This is used by the periodic summary notification to report daily/weekly stats.

    Args:
        hours: Number of hours to look back (e.g., 24 for daily, 168 for weekly)
        role_filter: Optional filter by role ("maker" or "taker")
        data_dir: Optional data directory
        wallet_fingerprint: If provided, restrict statistics to entries
            belonging to the given wallet (issue #473).

    Returns:
        Dict with statistics (see _compute_stats for full list).
    """
    entries = read_history(data_dir, role_filter=role_filter, wallet_fingerprint=wallet_fingerprint)

    if not entries:
        return _compute_stats([])

    cutoff = datetime.now() - timedelta(hours=hours)

    filtered: list[TransactionHistoryEntry] = []
    for entry in entries:
        try:
            entry_time = datetime.fromisoformat(entry.timestamp)
            if entry_time >= cutoff:
                filtered.append(entry)
        except (ValueError, TypeError):
            continue

    return _compute_stats(filtered)


def create_maker_history_entry(
    taker_nick: str,
    cj_amount: int,
    fee_received: int,
    txfee_contribution: int,
    cj_address: str,
    change_address: str,
    our_utxos: list[tuple[str, int]],
    txid: str | None = None,
    network: str = "mainnet",
    wallet_fingerprint: str = "",
) -> TransactionHistoryEntry:
    """
    Create a history entry for a maker CoinJoin (initially marked as pending).

    The transaction is created with success=False and confirmations=0 to indicate
    it's pending confirmation. A background task should later update this entry
    once the transaction is confirmed on-chain.

    Args:
        taker_nick: The taker's nick
        cj_amount: CoinJoin amount in sats
        fee_received: CoinJoin fee received
        txfee_contribution: Mining fee contribution
        cj_address: Our CoinJoin output address
        change_address: Our change output address
        our_utxos: List of (txid, vout) tuples for our inputs
        txid: Transaction ID (may not be known by maker)
        network: Network name

    Returns:
        TransactionHistoryEntry ready to be appended (marked as pending)
    """
    now = datetime.now().isoformat()
    net_fee = fee_received - txfee_contribution

    return TransactionHistoryEntry(
        timestamp=now,
        completed_at="",  # Not completed until confirmed
        role="maker",
        success=False,  # Pending confirmation
        failure_reason="Pending confirmation",
        confirmations=0,
        confirmed_at="",
        txid=txid or "",
        cj_amount=cj_amount,
        peer_count=None,  # Makers don't know total peer count
        counterparty_nicks=taker_nick,
        fee_received=fee_received,
        txfee_contribution=txfee_contribution,
        net_fee=net_fee,
        source_mixdepth=0,  # Would need to determine from UTXOs
        destination_address=cj_address,
        change_address=change_address,
        utxos_used=",".join(f"{txid}:{vout}" for txid, vout in our_utxos),
        network=network,
        wallet_fingerprint=wallet_fingerprint,
    )


def get_pending_transactions(
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> list[TransactionHistoryEntry]:
    """
    Get all pending (unconfirmed) transactions from history.

    Returns entries that are:
    - Not yet confirmed (success=False, confirmations=0)
    - Not yet completed (completed_at is empty) - excludes failed transactions
    - Either have a txid waiting for confirmation, or no txid yet (needs discovery)
    - Without a sibling row (same txid + wallet_fingerprint) that is already
      marked successful. Such ghost duplicates would otherwise cause the
      background monitors to poll the same already-confirmed txid forever
      (the duplicated successful row keeps absorbing the update via
      ``update_transaction_confirmation*``, leaving this stale pending row
      untouched).
    - With ``confirmations < PENDING_CONFIRMATION_TRACKING_MAX``. This is a
      safety net: under normal flow ``confirmations`` flips above 0 only via
      ``update_transaction_confirmation*`` which simultaneously sets
      ``success=True`` (so the row is no longer pending). If a row somehow
      ends up with confirmations >= the cap while still pending, we simply
      stop polling it.

    Args:
        data_dir: Optional data directory.
        wallet_fingerprint: If provided, only return pending entries for the
            given wallet (issue #473). This is what prevents another wallet's
            phantom pending transactions from showing up under a freshly
            generated wallet.

    Returns:
        List of pending entries (includes entries without txid)
    """
    entries = read_history(data_dir, wallet_fingerprint=wallet_fingerprint)

    # Index already-successful txids per wallet so we can drop any pending
    # row that is shadowed by a successful sibling. Keying on
    # ``(wallet_fingerprint, txid)`` keeps the check correct when several
    # wallets share the same data directory.
    successful_txids: set[tuple[str, str]] = {
        (e.wallet_fingerprint, e.txid) for e in entries if e.success and e.txid
    }

    return [
        e
        for e in entries
        if not e.success
        and e.confirmations < PENDING_CONFIRMATION_TRACKING_MAX
        and not e.completed_at
        and (not e.txid or (e.wallet_fingerprint, e.txid) not in successful_txids)
    ]


def _select_entry_for_confirmation_update(
    entries: list[TransactionHistoryEntry],
    txid: str,
    wallet_fingerprint: str | None,
) -> TransactionHistoryEntry | None:
    """Pick the right history row to update for a given confirmed txid.

    Multiple rows can share the same ``txid`` (for example a maker history
    file that – through past bugs or schema migrations – ended up with both
    a stale pending row and a finalized row for the same CoinJoin). When we
    learn about a new confirmation, we want to land on the still-pending
    row so that ``success`` actually flips to True; otherwise the pending
    row is never finalized and the background monitors poll the same txid
    forever.

    Selection rules:
    1. Honor the optional ``wallet_fingerprint`` filter.
    2. Prefer the first matching row that is not yet successful.
    3. Otherwise fall back to the first matching row (preserves the
       previous "just bump confirmations" behavior for already-finalized
       rows).
    """

    matches = [
        e
        for e in entries
        if e.txid == txid
        and (wallet_fingerprint is None or e.wallet_fingerprint == wallet_fingerprint)
    ]
    if not matches:
        return None
    for entry in matches:
        if not entry.success:
            return entry
    return matches[0]


def update_transaction_confirmation(
    txid: str,
    confirmations: int,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """
    Update a transaction's confirmation status in the history file.

    This function rewrites the entire CSV file with the updated entry.
    If confirmations > 0, marks the transaction as successful.

    Note: This is the synchronous version. For makers who want automatic
    peer count detection, use update_transaction_confirmation_with_detection().

    Args:
        txid: Transaction ID to update
        confirmations: Current number of confirmations
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only match entries belonging to this
            wallet (issue #473). Prevents updating another wallet's entry when
            multiple wallets share the same data directory.

    Returns:
        True if transaction was found and updated, False otherwise
    """
    history_path = _get_history_path(data_dir)
    if not history_path.exists():
        return False

    entries = read_history(data_dir)
    target = _select_entry_for_confirmation_update(entries, txid, wallet_fingerprint)
    if target is None:
        return False

    target.confirmations = confirmations
    if confirmations > 0 and not target.success:
        # Mark as successful on first confirmation
        target.success = True
        target.failure_reason = ""
        target.confirmed_at = datetime.now().isoformat()
        target.completed_at = target.confirmed_at
        logger.info(f"Transaction {txid[:16]}... confirmed with {confirmations} confirmations")
    elif confirmations > 0:
        # Already marked as successful, just update confirmation count
        logger.debug(f"Updated confirmations for {txid[:16]}...: {confirmations}")

    return _write_history_entries_atomic(entries, history_path)


def abandon_transaction(
    txid: str,
    reason: str,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """Mark a pending transaction as abandoned so the monitor stops checking it.

    Sets ``completed_at`` and ``failure_reason`` so ``get_pending_transactions``
    will no longer return the entry.  ``success`` is left ``False`` and
    ``confirmations`` stays ``0``.

    Args:
        txid: Transaction ID to abandon
        reason: Human-readable reason (stored in ``failure_reason``)
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only match entries belonging to this
            wallet.

    Returns:
        True if transaction was found and updated, False otherwise
    """
    history_path = _get_history_path(data_dir)
    if not history_path.exists():
        return False

    entries = read_history(data_dir)
    target = _select_entry_for_confirmation_update(entries, txid, wallet_fingerprint)
    if target is None:
        return False

    now = datetime.now().isoformat()
    target.completed_at = now
    target.failure_reason = reason
    logger.warning(f"Transaction {txid[:16]}... abandoned: {reason}")
    return _write_history_entries_atomic(entries, history_path)


async def update_transaction_confirmation_with_detection(
    txid: str,
    confirmations: int,
    backend: BlockchainBackend | Any | None = None,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """
    Update transaction confirmation and detect peer count for makers.

    This async version can detect the CoinJoin peer count by analyzing the
    transaction outputs when it confirms. This is useful for makers who don't
    know the peer count during the CoinJoin.

    Args:
        txid: Transaction ID to update
        confirmations: Current number of confirmations
        backend: Blockchain backend for fetching transaction (optional, for peer detection)
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only match entries belonging to this
            wallet (issue #473).

    Returns:
        True if transaction was found and updated, False otherwise
    """
    history_path = _get_history_path(data_dir)
    if not history_path.exists():
        return False

    entries = read_history(data_dir)
    target = _select_entry_for_confirmation_update(entries, txid, wallet_fingerprint)
    if target is None:
        return False

    target.confirmations = confirmations
    if confirmations > 0 and not target.success:
        # Mark as successful on first confirmation
        target.success = True
        target.failure_reason = ""
        target.confirmed_at = datetime.now().isoformat()
        target.completed_at = target.confirmed_at
        logger.info(f"Transaction {txid[:16]}... confirmed with {confirmations} confirmations")

        # Detect peer count for makers
        if (
            target.role == "maker"
            and target.peer_count is None
            and backend is not None
            and target.cj_amount > 0
        ):
            detected_count = await detect_coinjoin_peer_count(backend, txid, target.cj_amount)
            if detected_count is not None:
                target.peer_count = detected_count
                logger.info(f"Detected {detected_count} participants in CoinJoin {txid[:16]}...")

    elif confirmations > 0:
        # Already marked as successful, just update confirmation count
        logger.debug(f"Updated confirmations for {txid[:16]}...: {confirmations}")

    return _write_history_entries_atomic(entries, history_path)


def update_pending_transaction_txid(
    destination_address: str,
    txid: str,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """
    Update a pending transaction's txid by matching the destination address.

    This is used when a maker doesn't initially know the txid (didn't receive !push),
    but can discover it later by finding which transaction paid to the CoinJoin address.

    Args:
        destination_address: The CoinJoin destination address to match
        txid: The discovered transaction ID
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only match entries belonging to this
            wallet (issue #473).

    Returns:
        True if a matching entry was found and updated, False otherwise
    """
    history_path = _get_history_path(data_dir)
    if not history_path.exists():
        return False

    entries = read_history(data_dir)
    updated = False

    for entry in entries:
        # Match by destination address and empty txid (pending without txid)
        if entry.destination_address == destination_address and not entry.txid:
            if wallet_fingerprint is not None and entry.wallet_fingerprint != wallet_fingerprint:
                continue
            entry.txid = txid
            logger.info(
                f"Updated pending transaction for {destination_address[:20]}... "
                f"with txid {txid[:16]}..."
            )
            updated = True
            break

    if not updated:
        return False

    return _write_history_entries_atomic(entries, history_path)


def update_awaiting_transaction_signed(
    destination_address: str,
    txid: str,
    fee_received: int,
    txfee_contribution: int,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """
    Update a pending "Awaiting transaction" entry when the maker signs the tx.

    This is called after the maker successfully signs a transaction. The entry
    was created earlier (during !ioauth) with failure_reason="Awaiting transaction"
    to ensure the addresses were recorded before revealing them.

    Args:
        destination_address: The CoinJoin destination address to match
        txid: The transaction ID
        fee_received: CoinJoin fee earned
        txfee_contribution: Mining fee contribution
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only match entries belonging to this
            wallet (issue #473).

    Returns:
        True if a matching entry was found and updated, False otherwise
    """
    history_path = _get_history_path(data_dir)
    if not history_path.exists():
        return False

    entries = read_history(data_dir)
    updated = False

    for entry in entries:
        # Match by destination address and "Awaiting transaction" status
        if (
            entry.destination_address == destination_address
            and entry.failure_reason == "Awaiting transaction"
            and not entry.txid  # Should not have txid yet
        ):
            if wallet_fingerprint is not None and entry.wallet_fingerprint != wallet_fingerprint:
                continue
            entry.txid = txid
            entry.fee_received = fee_received
            entry.txfee_contribution = txfee_contribution
            entry.net_fee = fee_received - txfee_contribution
            entry.failure_reason = "Pending confirmation"  # Now awaiting confirmation
            logger.info(
                f"Updated awaiting transaction for {destination_address[:20]}... "
                f"with txid {txid[:16]}..., fee={fee_received} sats"
            )
            updated = True
            break

    if not updated:
        return False

    return _write_history_entries_atomic(entries, history_path)


def update_taker_awaiting_transaction_broadcast(
    destination_address: str,
    change_address: str,
    txid: str,
    mining_fee: int,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """
    Update a pending "Awaiting transaction" entry when the taker broadcasts the tx.

    This is called after the taker successfully broadcasts a transaction. The entry
    was created earlier (before sending !tx) with failure_reason="Awaiting transaction"
    to ensure the addresses were recorded before revealing them.

    Args:
        destination_address: The CoinJoin destination address to match
        change_address: The change address to match (for extra precision)
        txid: The transaction ID
        mining_fee: Actual mining fee paid (may differ from estimate)
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only match entries belonging to this
            wallet (issue #473).

    Returns:
        True if a matching entry was found and updated, False otherwise
    """
    history_path = _get_history_path(data_dir)
    if not history_path.exists():
        return False

    entries = read_history(data_dir)
    updated = False

    for entry in entries:
        # Match by destination + change address and "Awaiting transaction" status
        # Both addresses must match exactly (including empty string for no change)
        if (
            entry.destination_address == destination_address
            and entry.change_address == change_address
            and entry.failure_reason == "Awaiting transaction"
            and not entry.txid  # Should not have txid yet
        ):
            if wallet_fingerprint is not None and entry.wallet_fingerprint != wallet_fingerprint:
                continue
            entry.txid = txid
            entry.mining_fee_paid = mining_fee
            entry.net_fee = -(entry.total_maker_fees_paid + mining_fee)
            entry.failure_reason = "Pending confirmation"  # Now awaiting confirmation
            logger.info(
                f"Updated awaiting transaction for {destination_address[:20]}... "
                f"with txid {txid[:16]}..., mining_fee={mining_fee} sats"
            )
            updated = True
            break

    if not updated:
        return False

    return _write_history_entries_atomic(entries, history_path)


def mark_pending_transaction_failed(
    destination_address: str,
    failure_reason: str,
    data_dir: Path | None = None,
    txid: str | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """
    Mark a pending transaction as failed by matching the destination address and optionally txid.

    This is used when a pending CoinJoin times out - the taker never broadcast
    the transaction, so we mark it as failed rather than leaving it pending
    indefinitely.

    Args:
        destination_address: The CoinJoin destination address to match
        failure_reason: Reason for marking as failed (e.g., "Timed out after 60 minutes")
        data_dir: Optional data directory
        txid: Optional transaction ID for more precise matching (when multiple entries
              share the same destination address)
        wallet_fingerprint: If provided, only match entries belonging to this
            wallet (issue #473).

    Returns:
        True if a matching entry was found and updated, False otherwise
    """
    history_path = _get_history_path(data_dir)
    if not history_path.exists():
        return False

    entries = read_history(data_dir)
    updated = False

    for entry in entries:
        # Match by destination address and pending status
        # (success=False, confirmations=0, no completed_at)
        if (
            entry.destination_address == destination_address
            and not entry.success
            and entry.confirmations == 0
            and not entry.completed_at
        ):
            # If txid is provided, also match by txid
            if txid is not None and entry.txid != txid:
                continue

            if wallet_fingerprint is not None and entry.wallet_fingerprint != wallet_fingerprint:
                continue

            entry.success = False
            entry.failure_reason = failure_reason
            entry.completed_at = datetime.now().isoformat()
            # Keep confirmations at 0 to distinguish from confirmed then reorged
            txid_str = f" (txid: {entry.txid[:16]}...)" if entry.txid else ""
            logger.info(
                f"Marked pending transaction for {destination_address[:20]}...{txid_str} "
                f"as failed: {failure_reason}"
            )
            updated = True
            break

    if not updated:
        return False

    return _write_history_entries_atomic(entries, history_path)


def cleanup_stale_pending_transactions(
    max_age_minutes: int = 60,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> int:
    """
    Mark all stale pending transactions as failed.

    This is a cleanup function for entries that got stuck in pending state
    (e.g., from before the timeout feature was implemented, or due to bugs).

    Args:
        max_age_minutes: Mark entries older than this as failed (default: 60)
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only clean up stale pending entries
            for the given wallet (issue #473). Other wallets' pending entries
            in the same shared CSV are left untouched.

    Returns:
        Number of entries marked as failed
    """
    history_path = _get_history_path(data_dir)
    if not history_path.exists():
        return 0

    # Read full history (no wallet filter) so the rewrite preserves all entries
    entries = read_history(data_dir)
    count = 0
    now = datetime.now()

    for entry in entries:
        # Only process pending entries (success=False, confirmations=0, no completed_at)
        if not entry.success and entry.confirmations == 0 and not entry.completed_at:
            # Skip entries that don't belong to the active wallet when a
            # filter is set so we don't mutate other wallets' pending state.
            if wallet_fingerprint is not None and entry.wallet_fingerprint != wallet_fingerprint:
                continue
            try:
                timestamp = datetime.fromisoformat(entry.timestamp)
                age_minutes = (now - timestamp).total_seconds() / 60

                if age_minutes >= max_age_minutes:
                    entry.completed_at = now.isoformat()
                    entry.failure_reason = (
                        f"Cleaned up: pending for {int(age_minutes)} minutes without confirmation"
                    )
                    txid_str = f" (txid: {entry.txid[:16]}...)" if entry.txid else ""
                    logger.info(
                        f"Marked stale pending entry{txid_str} as failed "
                        f"(age: {int(age_minutes)} minutes)"
                    )
                    count += 1
            except (ValueError, TypeError) as e:
                logger.debug(f"Error parsing timestamp for entry: {e}")
                continue

    if count == 0:
        return 0

    if _write_history_entries_atomic(entries, history_path):
        return count
    return 0


def create_taker_history_entry(
    maker_nicks: list[str],
    cj_amount: int,
    total_maker_fees: int,
    mining_fee: int,
    destination: str,
    change_address: str,
    source_mixdepth: int,
    selected_utxos: list[tuple[str, int]],
    txid: str = "",
    broadcast_method: str = "self",
    network: str = "mainnet",
    success: bool = False,  # Default to pending
    failure_reason: str = "Awaiting transaction",
    wallet_fingerprint: str = "",
) -> TransactionHistoryEntry:
    """
    Create a history entry for a taker CoinJoin.

    This should be called BEFORE sending !tx to makers, to ensure addresses
    are recorded before they're revealed. Initially created with
    failure_reason="Awaiting transaction", then updated after broadcast.

    The transaction is created with success=False and confirmations=0 by default
    to indicate it's pending confirmation. A background task should later update
    this entry once the transaction is confirmed on-chain.

    Args:
        maker_nicks: List of maker nicks
        cj_amount: CoinJoin amount in sats
        total_maker_fees: Total maker fees paid
        mining_fee: Mining fee paid (may be 0 initially, updated after signing)
        destination: Destination address (CoinJoin output)
        change_address: Change output address (must be recorded for privacy!)
        source_mixdepth: Source mixdepth
        selected_utxos: List of (txid, vout) tuples for our inputs
        txid: Transaction ID (empty string if not yet known)
        broadcast_method: How the tx was/will be broadcast
        network: Network name
        success: Whether the CoinJoin succeeded (default False for pending)
        failure_reason: Reason for failure if any (default "Awaiting transaction")

    Returns:
        TransactionHistoryEntry ready to be appended
    """
    now = datetime.now().isoformat()
    net_fee = -(total_maker_fees + mining_fee)  # Negative = cost

    return TransactionHistoryEntry(
        timestamp=now,
        completed_at="" if not success else now,
        role="taker",
        success=success,
        failure_reason=failure_reason,
        confirmations=0,
        confirmed_at="",
        txid=txid,
        cj_amount=cj_amount,
        peer_count=len(maker_nicks),
        counterparty_nicks=",".join(maker_nicks),
        total_maker_fees_paid=total_maker_fees,
        mining_fee_paid=mining_fee,
        net_fee=net_fee,
        source_mixdepth=source_mixdepth,
        destination_address=destination,
        change_address=change_address,
        utxos_used=",".join(f"{txid}:{vout}" for txid, vout in selected_utxos),
        broadcast_method=broadcast_method,
        network=network,
        wallet_fingerprint=wallet_fingerprint,
    )


def get_used_addresses(
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> set[str]:
    """
    Get all addresses that have been used in CoinJoin history.

    Returns both destination addresses (CoinJoin outputs) and change addresses
    from all history entries, regardless of success or confirmation status.

    This is critical for privacy: once an address has been shared with peers
    (even if the transaction failed or wasn't confirmed), it should never be
    reused.

    Args:
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only consider entries belonging to
            the given wallet (issue #473).

    Returns:
        Set of addresses that should not be reused
    """
    entries = read_history(data_dir, wallet_fingerprint=wallet_fingerprint)
    used_addresses = set()

    for entry in entries:
        if entry.destination_address:
            used_addresses.add(entry.destination_address)
        if entry.change_address:
            used_addresses.add(entry.change_address)

    return used_addresses


def get_address_history_types(
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> dict[str, str]:
    """
    Get the history type for each address used in CoinJoin history.

    This maps addresses to their role in CoinJoin transactions:
    - "cj_out": CoinJoin output address (destination) - from successful CJ
    - "change": Change address - from successful CJ
    - "flagged": Address was shared but ALL transactions using it failed

    Priority: successful transactions take precedence over failed ones.
    Once an address is used in a successful CoinJoin, it remains cj_out/change
    even if later transactions using the same address failed.

    Args:
        data_dir: Optional data directory (defaults to get_default_data_dir())
        wallet_fingerprint: If provided, only consider entries belonging to
            the given wallet (issue #473).

    Returns:
        Dict mapping address -> type string
    """
    entries = read_history(data_dir, wallet_fingerprint=wallet_fingerprint)
    address_types: dict[str, str] = {}

    for entry in entries:
        if entry.destination_address:
            # CoinJoin output address
            if entry.success:
                # Successful transaction - mark as cj_out (overrides any previous flagged)
                address_types[entry.destination_address] = "cj_out"
            else:
                # Transaction failed - only mark as flagged if not already used successfully
                if entry.destination_address not in address_types:
                    address_types[entry.destination_address] = "flagged"

        if entry.change_address:
            # Change address
            if entry.success:
                # Successful transaction - mark as change (overrides any previous flagged)
                address_types[entry.change_address] = "change"
            else:
                # Transaction failed - only mark as flagged if not already used successfully
                if entry.change_address not in address_types:
                    address_types[entry.change_address] = "flagged"

    return address_types


def get_utxo_label(
    address: str,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> str:
    """
    Get a human-readable label for a UTXO based on its address history.

    Labels are derived from CoinJoin history:
    - "cj-out": CoinJoin output (equal-amount output from successful CJ)
    - "cj-change": CoinJoin change output (change from successful CJ)
    - "deposit": External deposit (not from CoinJoin)
    - "flagged": Address was shared but transaction failed

    Args:
        address: The address to get a label for
        data_dir: Optional data directory (defaults to get_default_data_dir())
        wallet_fingerprint: If provided, only consider entries belonging to
            the given wallet (issue #473).

    Returns:
        Human-readable label for the UTXO
    """
    history_types = get_address_history_types(data_dir, wallet_fingerprint=wallet_fingerprint)

    if address in history_types:
        history_type = history_types[address]
        if history_type == "cj_out":
            return "cj-out"
        elif history_type == "change":
            return "cj-change"
        elif history_type == "flagged":
            return "flagged"

    # If not in history, it's a deposit (external receive)
    return "deposit"


async def detect_coinjoin_peer_count(
    backend: BlockchainBackend | Any,
    txid: str,
    cj_amount: int,
) -> int | None:
    """
    Detect the number of CoinJoin participants by counting equal-amount outputs.

    When makers participate in a CoinJoin, they don't know the total number of
    participants. However, once the transaction confirms, we can analyze it to
    count outputs with the CoinJoin amount.

    Args:
        backend: Blockchain backend to fetch transaction data
        txid: Transaction ID to analyze
        cj_amount: The CoinJoin amount in satoshis

    Returns:
        Number of equal-amount outputs (peer count), or None if detection fails
    """
    try:
        from jmcore.bitcoin import parse_transaction

        # Fetch the transaction
        tx = await backend.get_transaction(txid)
        if not tx:
            logger.warning(f"Could not fetch transaction {txid} for peer count detection")
            return None

        # Parse the raw transaction to get outputs
        parsed_tx = parse_transaction(tx.raw)

        # Count outputs with the CoinJoin amount
        equal_amount_count = sum(1 for output in parsed_tx.outputs if output["value"] == cj_amount)

        if equal_amount_count == 0:
            logger.warning(
                f"No outputs matching CoinJoin amount {cj_amount} sats in tx {txid[:16]}..."
            )
            return None

        logger.debug(
            f"Detected {equal_amount_count} equal-amount outputs "
            f"({cj_amount:,} sats each) in tx {txid[:16]}..."
        )
        return equal_amount_count

    except Exception as e:
        logger.warning(f"Failed to detect peer count for tx {txid[:16]}...: {e}")
        return None


def update_transaction_peer_count(
    txid: str,
    peer_count: int,
    data_dir: Path | None = None,
) -> bool:
    """
    Update a transaction's peer count in the history file.

    This is used for makers to update the peer count after detecting it
    from the confirmed transaction's equal-amount outputs.

    Args:
        txid: Transaction ID to update
        peer_count: Detected peer count
        data_dir: Optional data directory

    Returns:
        True if transaction was found and updated, False otherwise
    """
    history_path = _get_history_path(data_dir)
    if not history_path.exists():
        return False

    entries = read_history(data_dir)
    updated = False

    for entry in entries:
        if entry.txid == txid and entry.peer_count is None:
            entry.peer_count = peer_count
            logger.info(f"Updated peer count for tx {txid[:16]}... to {peer_count}")
            updated = True
            break

    if not updated:
        return False

    return _write_history_entries_atomic(entries, history_path)


async def update_all_pending_transactions(
    backend: BlockchainBackend | Any,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> int:
    """
    Update the status of all pending transactions using the blockchain backend.

    This function is called when displaying wallet info or history to ensure
    pending transactions are updated with their current confirmation status.
    Particularly important for one-shot coinjoin commands that exit before
    the background monitor can update the status.

    Args:
        backend: Blockchain backend to query transaction status
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only update pending entries for the
            given wallet (issue #473).

    Returns:
        Number of transactions that were updated
    """
    pending = get_pending_transactions(data_dir, wallet_fingerprint=wallet_fingerprint)
    if not pending:
        return 0

    updated_count = 0
    has_mempool = backend.has_mempool_access()

    for entry in pending:
        if not entry.txid:
            # Can't check without txid
            continue

        try:
            if has_mempool:
                # Full node: can check mempool directly
                tx_info = await backend.get_transaction(entry.txid)
                if tx_info is not None:
                    # Only mark as success after first block confirmation.
                    if tx_info.confirmations > 0:
                        update_transaction_confirmation(
                            txid=entry.txid,
                            confirmations=tx_info.confirmations,
                            data_dir=data_dir,
                        )
                        updated_count += 1
                        logger.debug(
                            f"Updated pending tx {entry.txid[:16]}... "
                            f"({tx_info.confirmations} confs)"
                        )
            else:
                # Neutrino: can only check confirmed blocks
                if not entry.destination_address:
                    continue

                try:
                    current_height = await backend.get_block_height()
                except Exception:
                    current_height = None

                verified = await backend.verify_tx_output(
                    txid=entry.txid,
                    vout=0,  # CJ outputs are typically first
                    address=entry.destination_address,
                    start_height=current_height,
                )

                if verified:
                    update_transaction_confirmation(
                        txid=entry.txid,
                        confirmations=1,
                        data_dir=data_dir,
                    )
                    updated_count += 1
                    logger.debug(f"Updated pending tx {entry.txid[:16]}... via Neutrino")

        except Exception as e:
            logger.debug(f"Could not update pending tx {entry.txid[:16]}...: {e}")

    if updated_count > 0:
        logger.info(f"Updated {updated_count} pending transaction(s)")

    return updated_count
