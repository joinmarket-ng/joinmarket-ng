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
import errno
import os
import stat
import tempfile
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from jmcore.paths import get_default_data_dir
from loguru import logger
from pydantic.dataclasses import dataclass

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX platforms
    msvcrt = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from jmcore.bitcoin import CoinjoinAnalysis

    from jmwallet.backends.base import BlockchainBackend
    from jmwallet.wallet.models import UTXOInfo


# Once a pending transaction reaches this many confirmations we stop polling
# it from the background monitors. The first confirmation already flips
# ``success`` to True (see ``update_transaction_confirmation*``), but this
# upper bound is a safety net for ghost/duplicate rows that – for whatever
# reason – were never finalized: there is no privacy or accounting value in
# polling the same already-deeply-confirmed txid forever.
PENDING_CONFIRMATION_TRACKING_MAX = 6

# Timeouts describe local monitoring, not transaction validity. Keep a stable
# reason prefix so history can distinguish them without a CSV schema change.
MONITORING_TIMEOUT_REASON_PREFIX = "Monitoring timed out:"


class HistoryWriteError(Exception):
    """Raised when a history entry cannot be persisted to disk."""


# Role a history row records. "maker" / "taker" denote CoinJoin participation;
# "send" a plain wallet spend; "deposit" an incoming payment (only written by
# on-chain history reconstruction for imported wallets, never by live flows).
HistoryRole = Literal["maker", "taker", "send", "deposit"]

# Provenance of a history row. "protocol" rows are written at protocol time by
# this wallet's own maker/taker/send activity and are authoritative.
# "onchain" rows are best-effort guesses reconstructed from blockchain data
# for a wallet imported from seed (see ``jmwallet.history_reconstruction``).
HistorySource = Literal["protocol", "onchain"]

VALID_HISTORY_SOURCES: frozenset[str] = frozenset({"protocol", "onchain"})


@dataclass
class TransactionHistoryEntry:
    """A single CoinJoin transaction record."""

    # Timestamps
    timestamp: str  # ISO format
    completed_at: str = ""  # ISO format

    # Role and outcome
    # "maker" / "taker" denote CoinJoin participation; "send" denotes a plain
    # (non-CoinJoin) wallet spend recorded so that the destination and change
    # addresses are persistently marked as used, even if Bitcoin Core's
    # transaction history later loses sight of them (for example, after an
    # interrupted full rescan or when the smart-scan window does not cover
    # the spend). "deposit" denotes an incoming payment; live flows never
    # write deposits (they are only reconstructed from chain data for
    # imported wallets).
    role: HistoryRole = "taker"
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

    # Comma-separated addresses corresponding to ``utxos_used`` (one address
    # per spent input, in the same order when known). Populated at entry
    # creation time so that ``get_used_addresses`` can blacklist input
    # addresses without re-querying the backend later. Defaults to "" for
    # backwards compatibility with rows written before this column existed;
    # such legacy rows can be backfilled out-of-band via ``gettransaction``.
    # Kept last in the field order so the existing CSV header migration
    # (which assigns trailing legacy cells positionally to appended columns)
    # continues to work for files written by older releases.
    source_addresses: str = ""

    # Provenance of this row (see ``HistorySource``): "protocol" for rows
    # written by live maker/taker/send flows (authoritative), "onchain" for
    # rows reconstructed from blockchain data after a seed import (best-effort
    # guesses: role and fees are inferred from the transaction structure).
    # Appended after ``source_addresses`` so the positional trailing-cell
    # migration for legacy headers keeps working.
    source: HistorySource = "protocol"

    # Aggregate value of this wallet's inputs, in satoshis. Live maker flows
    # populate this for the JAM-compatible yield report. Older history rows
    # default to zero because their spent input values were not retained.
    # Keep new fields appended so positional legacy-header migration remains
    # able to recover rows written against an older header.
    input_value: int = 0

    # Index of this wallet's destination CoinJoin output in the serialized
    # transaction. Kept last so older CSV rows safely default to unknown.
    # Neutrino needs the exact vout to verify a confirmed output by address.
    destination_vout: int = -1

    # Neutral transfer amount (in satoshis) across all roles (maker/taker CJ denomination,
    # send output amount, deposit input amount). Appended last for legacy CSV migration.
    amount: int = 0

    # Configured intent and any privacy-relevant fallback are distinct from
    # ``broadcast_method``, which records what actually happened. Keep these
    # appended for positional compatibility with every earlier CSV schema.
    broadcast_policy: str = ""
    broadcast_fallback_reason: str = ""

    @property
    def transfer_amount(self) -> int:
        """Return the neutral amount, including rows written before it existed."""
        return self.amount if self.amount != 0 else self.cj_amount


HISTORY_FILENAME = "history.csv"
LEGACY_HISTORY_FILENAME = "coinjoin_history.csv"
HISTORY_LOCK_SUFFIX = ".lock"

# A normal CoinJoin has no more than two outputs per participant (CoinJoin and
# change). New rows retain the exact vout; this cap bounds compatibility scans
# of manually edited or legacy history rows.
_MAX_LEGACY_DESTINATION_VOUTS = 64


def destination_vout_candidates(destination_vout: int, peer_count: int | None) -> range:
    """Return an exact output index or bounded candidates for legacy history."""
    if destination_vout >= 0:
        return range(destination_vout, destination_vout + 1)
    if peer_count is None or peer_count < 0:
        return range(_MAX_LEGACY_DESTINATION_VOUTS)
    return range(min(2 * (peer_count + 1), _MAX_LEGACY_DESTINATION_VOUTS))


async def verify_history_destination_output(
    backend: BlockchainBackend,
    *,
    txid: str,
    destination_address: str,
    destination_vout: int,
    peer_count: int | None,
    start_height: int | None,
) -> bool:
    """Verify a history destination by exact vout or bounded legacy scan."""
    for vout in destination_vout_candidates(destination_vout, peer_count):
        if await backend.verify_tx_output(
            txid=txid,
            vout=vout,
            address=destination_address,
            start_height=start_height,
            include_mempool=False,
        ):
            return True
    return False


def _get_history_path(data_dir: Path | None = None) -> Path:
    """Get the path to the history CSV file.

    The canonical filename is ``history.csv`` (covers CoinJoin maker/taker
    rounds and plain wallet sends; see ``TransactionHistoryEntry.role``).
    Legacy filename migration happens while holding the history lock in
    ``_resolve_history_path_unlocked``.

    Args:
        data_dir: Optional data directory (defaults to
            ``get_default_data_dir()``).

    Returns:
        Path to ``history.csv`` in the data directory.
    """
    if data_dir is None:
        data_dir = get_default_data_dir()
    if not data_dir.exists():
        data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(data_dir, 0o700)
    return data_dir / HISTORY_FILENAME


def _open_owner_only_regular(path: Path, flags: int) -> int:
    """Open a regular history-related file without following symlinks."""
    secure_flags = (
        flags
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        fd = os.open(path, secure_flags, 0o600)
    except OSError as exc:
        raise HistoryWriteError(f"Could not securely open {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise HistoryWriteError(f"History file {path} is not a regular file")
        os.fchmod(fd, 0o600)
        return fd
    except Exception:
        os.close(fd)
        raise


def _ensure_regular_owner_only(path: Path) -> None:
    """Validate and harden an existing history-related path."""
    try:
        path.lstat()
    except FileNotFoundError:
        return
    fd = _open_owner_only_regular(path, os.O_RDONLY)
    os.close(fd)


def _history_lock_path(history_path: Path) -> Path:
    return history_path.with_name(f"{history_path.name}{HISTORY_LOCK_SUFFIX}")


@contextmanager
def _history_lock(history_path: Path, *, shared: bool = False) -> Iterator[None]:
    """Serialize history access through a stable owner-only sidecar lock.

    Readers pass ``shared=True`` so they do not queue behind each other. Only
    POSIX supports a shared mode; the Windows fallback stays exclusive.
    """
    lock_fd = _open_owner_only_regular(_history_lock_path(history_path), os.O_RDWR | os.O_CREAT)
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            if os.fstat(lock_fd).st_size == 0:
                os.write(lock_fd, b"\0")
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - unknown platform
            raise HistoryWriteError("Advisory file locking is unavailable on this platform")
        locked = True
        yield
    finally:
        try:
            if locked and fcntl is not None:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            elif locked and msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(lock_fd)


def _resolve_history_path_unlocked(history_path: Path) -> Path:
    """Perform legacy filename migration while the canonical sidecar is locked."""
    legacy_path = history_path.with_name(LEGACY_HISTORY_FILENAME)
    if legacy_path.exists() and not history_path.exists():
        _ensure_regular_owner_only(legacy_path)
        try:
            legacy_path.rename(history_path)
            logger.info(f"Migrated history file: {LEGACY_HISTORY_FILENAME} -> {HISTORY_FILENAME}")
        except OSError as exc:
            logger.warning(
                f"Could not rename {legacy_path} to {history_path}: {exc}; "
                "continuing to read from the legacy filename"
            )
            return legacy_path
    _ensure_regular_owner_only(history_path)
    return history_path


def _resolve_history_path_read_only(history_path: Path) -> Path:
    """Validate the canonical history path without migrating a legacy filename."""
    _ensure_regular_owner_only(history_path)
    return history_path


@contextmanager
def _locked_history_path(data_dir: Path | None = None, *, shared: bool = False) -> Iterator[Path]:
    """Yield the active history path while holding the advisory lock."""
    canonical_path = _get_history_path(data_dir)
    with _history_lock(canonical_path, shared=shared):
        if shared:
            yield _resolve_history_path_read_only(canonical_path)
        else:
            yield _resolve_history_path_unlocked(canonical_path)


def _legacy_history_filename_needs_migration(canonical_path: Path) -> bool:
    """Return whether only the legacy history filename is currently present."""
    legacy_path = canonical_path.with_name(LEGACY_HISTORY_FILENAME)
    return legacy_path.exists() and not canonical_path.exists()


def _history_header_is_stale(data_dir: Path | None = None) -> bool:
    """Return whether the on-disk history header needs migrating."""
    try:
        canonical_path = _get_history_path(data_dir)
        if not canonical_path.exists():
            return False
        actual = _read_csv_header(canonical_path)
    except Exception:
        return False
    return actual is not None and actual != _get_fieldnames()


def _history_needs_migration(data_dir: Path | None = None) -> bool:
    """Return whether a read must first take the exclusive migration lock."""
    try:
        canonical_path = _get_history_path(data_dir)
        return _legacy_history_filename_needs_migration(canonical_path) or _history_header_is_stale(
            data_dir
        )
    except Exception:
        return False


def _get_fieldnames() -> list[str]:
    """Get the list of field names for the CSV."""
    return [f.name for f in fields(TransactionHistoryEntry)]


def _read_csv_header(history_path: Path) -> list[str] | None:
    """Return the header (first row) of the CSV file, or None if absent/empty."""
    if not history_path.exists():
        return None
    try:
        fd = _open_owner_only_regular(history_path, os.O_RDONLY)
        with os.fdopen(fd, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            try:
                return next(reader)
            except StopIteration:
                return None
    except (HistoryWriteError, OSError) as e:
        logger.warning(f"Could not read history header for migration check: {e}")
        return None


# Networks a history row's ``network`` column may legitimately hold. Used to
# tell a correctly-aligned row from one whose columns were shifted by a stale
# reordered header (see ``_rewrite_reordered_history``).
_KNOWN_NETWORKS = frozenset({"mainnet", "testnet", "testnet4", "signet", "regtest"})


def _looks_like_fingerprint(value: str) -> bool:
    """True for an empty or 8-char-hex wallet fingerprint."""
    fp = value.strip().lower()
    if fp == "":
        return True
    if len(fp) != 8:
        return False
    try:
        bytes.fromhex(fp)
    except ValueError:
        return False
    return True


def _row_fields_consistent(row: Mapping[str, str]) -> bool:
    """Heuristic: do this row's anchor cells make sense for their columns?

    Used to disambiguate which header order a raw row was written in. The
    ``network`` and ``wallet_fingerprint`` columns are good anchors because
    their value spaces (a small known-network set; empty-or-8-hex) barely
    overlap with the addresses / nicks / amounts that land in them when the
    columns are shifted by one position.
    """
    net = (row.get("network") or "").strip()
    net_ok = net == "" or net in _KNOWN_NETWORKS
    return net_ok and _looks_like_fingerprint(row.get("wallet_fingerprint") or "")


def _reconcile_row_order(
    cells: list[str], actual: list[str], expected: list[str]
) -> dict[str, str]:
    """Map a raw CSV row's cells to column names, picking the right order.

    A reordered-header file can contain rows written in either the on-disk
    (``actual``) order or, when a newer writer appended against the stale
    header, the canonical (``expected``) order. Prefer the on-disk order and
    only fall back to canonical when the on-disk interpretation is internally
    inconsistent but the canonical one is.
    """
    by_actual = {actual[i]: cells[i] for i in range(min(len(actual), len(cells)))}
    if _row_fields_consistent(by_actual):
        return by_actual
    by_expected = {expected[i]: cells[i] for i in range(min(len(expected), len(cells)))}
    if _row_fields_consistent(by_expected):
        return by_expected
    return by_actual


def _rewrite_reordered_history(history_path: Path, actual: list[str], expected: list[str]) -> None:
    """Rewrite a history CSV whose header has all columns in the wrong order.

    Reconstructs each row via :func:`_reconcile_row_order` and rewrites the
    file atomically in canonical column order so subsequent appends and reads
    stay aligned.
    """
    raw_rows: list[list[str]] = []
    try:
        fd = _open_owner_only_regular(history_path, os.O_RDONLY)
        with os.fdopen(fd, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            try:
                next(reader)  # skip stale header
            except StopIteration:
                return
            for cells in reader:
                if cells:
                    raw_rows.append(cells)
    except (HistoryWriteError, OSError) as e:
        raise HistoryWriteError(f"Failed to read {history_path} for reorder migration: {e}") from e

    entries: list[TransactionHistoryEntry] = []
    for cells in raw_rows:
        entry = _row_to_entry(_reconcile_row_order(cells, actual, expected))
        if entry is not None:
            entries.append(entry)
    if not _write_history_entries_atomic(entries, history_path):
        raise HistoryWriteError(f"Failed to migrate {history_path} to the canonical header order")


def _ensure_history_header_current(history_path: Path) -> None:
    """Migrate a legacy history CSV to the current header layout.

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
    expected_existing = [name for name in expected if name in actual]
    if missing and set(actual) == set(expected_existing) and actual != expected_existing:
        # The file is both missing newly-appended columns and using an older
        # reordered layout. This occurs when ``source`` is introduced on top of
        # a pre-existing file whose ``source_addresses`` column was in the old
        # position. The generic missing-column migration maps cells by the
        # stale header and cannot recover rows a newer writer already appended
        # in canonical order against that header. Reconcile each row against
        # both interpretations first; _row_to_entry supplies defaults for the
        # genuinely missing appended columns.
        logger.info(
            "Migrating history CSV: normalizing reordered legacy header and "
            f"adding columns {missing}"
        )
        _rewrite_reordered_history(history_path, actual, expected)
        return
    if not missing:
        # Same columns, different order. This happens when a field is moved
        # in the dataclass declaration (e.g. ``source_addresses`` relocated to
        # last). The danger is subtle: ``csv.DictWriter`` writes appended rows
        # in canonical ``_get_fieldnames()`` order, but ``csv.DictReader``
        # maps cells by the on-disk header. With a stale reordered header the
        # two disagree, so every column after the moved field is shifted on
        # read (a maker's ``wallet_fingerprint`` silently reads back as one of
        # its input addresses, ``network`` reads back as the fingerprint, and
        # the per-wallet pending lookup never matches -> confirmed CoinJoins
        # stay stuck on ``success=False``). Rewrite to the canonical order,
        # reconstructing each row by whichever interpretation (on-disk vs
        # canonical) is internally consistent so rows a newer writer already
        # appended in canonical order against this stale header are recovered.
        logger.info(
            f"Migrating history CSV: normalizing reordered header "
            f"(on-disk order differs from canonical {len(expected)}-column layout)"
        )
        _rewrite_reordered_history(history_path, actual, expected)
        return

    logger.info(
        f"Migrating history CSV: adding columns {missing} "
        f"(legacy {len(actual)}-column header detected)"
    )
    # Read raw rows assuming the *expected* layout so cells previously
    # written past the legacy header (e.g. wallet_fingerprint appended
    # by a 0.28.x writer against a 0.27.x header) are recovered.
    raw_rows: list[dict[str, str]] = []
    try:
        fd = _open_owner_only_regular(history_path, os.O_RDONLY)
        with os.fdopen(fd, newline="", encoding="utf-8") as f:
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
    except (HistoryWriteError, OSError) as e:
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
        destination_vout = max(int(_get("destination_vout", "-1") or -1), -1)
    except ValueError:
        destination_vout = -1

    try:
        return TransactionHistoryEntry(
            timestamp=_get("timestamp"),
            completed_at=_get("completed_at"),
            role=cast(HistoryRole, _get("role", "taker")),
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
            source_addresses=_get("source_addresses"),
            broadcast_method=_get("broadcast_method"),
            broadcast_policy=_get("broadcast_policy"),
            broadcast_fallback_reason=_get("broadcast_fallback_reason"),
            network=_get("network", "mainnet"),
            wallet_fingerprint=_get("wallet_fingerprint"),
            # Rows written before the column existed (or by a corrupted
            # writer) default to the authoritative "protocol" provenance.
            source=cast(
                HistorySource,
                _get("source", "protocol")
                if _get("source", "protocol") in VALID_HISTORY_SOURCES
                else "protocol",
            ),
            input_value=int(_get("input_value", "0") or 0),
            destination_vout=destination_vout,
            amount=(
                int(_get("amount"))
                if _get("amount") and _get("amount") not in ("", "None")
                else int(_get("cj_amount", "0") or 0)
            ),
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
    fieldnames = _get_fieldnames()
    try:
        with _locked_history_path(data_dir) as history_path:
            # Migrate legacy headers so new columns are not appended past the
            # on-disk header (which would otherwise silently lose data on read).
            _ensure_history_header_current(history_path)
            fd = _open_owner_only_regular(history_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
            with os.fdopen(fd, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                if os.fstat(f.fileno()).st_size == 0:
                    writer.writeheader()

                row = {f.name: getattr(entry, f.name) for f in fields(entry)}
                writer.writerow(row)
                f.flush()
                os.fsync(f.fileno())

        logger.bind(sensitive=True).debug(
            f"Appended history entry: txid={entry.txid[:16]}... role={entry.role}"
        )
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
        _ensure_regular_owner_only(history_path)
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
            os.fchmod(temp_file.fileno(), 0o600)
            writer = csv.DictWriter(temp_file, fieldnames=fieldnames)
            writer.writeheader()
            for entry in sorted_entries:
                row = {f.name: getattr(entry, f.name) for f in fields(entry)}
                writer.writerow(row)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        os.replace(temp_path, history_path)
        _fsync_parent_directory(history_path.parent)
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


def _fsync_parent_directory(parent: Path) -> None:
    """Persist a completed rename when the platform supports directory fsync."""
    if os.name == "nt":  # pragma: no cover - directory descriptors are POSIX-only
        return
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        directory_fd = os.open(parent, directory_flags)
    except OSError as exc:
        if exc.errno in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
            return
        raise
    try:
        try:
            os.fsync(directory_fd)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP}:
                raise
    finally:
        os.close(directory_fd)


def read_history(
    data_dir: Path | None = None,
    limit: int | None = None,
    role_filter: HistoryRole | None = None,
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
    try:
        entries: list[TransactionHistoryEntry] | None = None
        # Header migration rewrites the file, so it needs the exclusive lock.
        # Taking it only when the header is actually stale lets the common
        # read path use a shared lock instead of serializing every reader.
        if _history_needs_migration(data_dir):
            with _locked_history_path(data_dir) as history_path:
                # Recheck after acquiring the exclusive sidecar lock because
                # another process may have migrated the filename or header
                # after the initial read-only probe.
                try:
                    _ensure_history_header_current(history_path)
                except HistoryWriteError as exc:
                    logger.warning(f"History header migration failed during read: {exc}")

                # If a filesystem error prevented the legacy rename, preserve
                # the established fallback by reading that file under the
                # exclusive lock. Shared readers only ever select canonical.
                if history_path.name == LEGACY_HISTORY_FILENAME:
                    entries = _read_history_entries_unlocked(history_path)

        if entries is None:
            with _locked_history_path(data_dir, shared=True) as history_path:
                if not history_path.exists():
                    return []
                entries = _read_history_entries_unlocked(history_path)
    except Exception as e:
        logger.error(f"Failed to read history: {e}")
        return []

    if role_filter:
        entries = [entry for entry in entries if entry.role == role_filter]
    if wallet_fingerprint is not None:
        entries = [entry for entry in entries if entry.wallet_fingerprint == wallet_fingerprint]

    # Sort by timestamp (most recent first) and apply limit
    entries.sort(key=lambda e: e.timestamp, reverse=True)
    if limit:
        entries = entries[:limit]

    return entries


def _read_history_entries_unlocked(history_path: Path) -> list[TransactionHistoryEntry]:
    """Read entries while the caller holds the history lock when needed."""
    entries: list[TransactionHistoryEntry] = []
    try:
        fd = _open_owner_only_regular(history_path, os.O_RDONLY)
        with os.fdopen(fd, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                entry = _row_to_entry(row)
                if entry is not None:
                    entries.append(entry)
    except Exception as exc:
        raise HistoryWriteError(f"Failed to read history: {exc}") from exc
    return entries


# Reference joinmarket-clientserver yield-generator statement format. The Earn
# report (e.g. JAM) consumes the ``/wallet/yieldgen/report`` API as a list of
# comma-separated rows in exactly this 8-column shape, so we synthesize them
# from the maker rows of ``history.csv`` (joinmarket-ng's single source of
# truth) instead of maintaining a separate ``yigen-statement.csv`` file.
YIELD_GENERATOR_REPORT_HEADER: list[str] = [
    "timestamp",
    "cj amount/satoshi",
    "my input count",
    "my input value/satoshi",
    "cjfee/satoshi",
    "earned/satoshi",
    "confirm time/min",
    "notes",
]

# Reference timestamp format used in yigen-statement.csv rows.
_YIELD_GENERATOR_TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M:%S"


def _format_yield_generator_timestamp(iso_timestamp: str) -> str:
    """Reformat an ISO timestamp to the reference ``%Y/%m/%d %H:%M:%S`` form."""
    try:
        return datetime.fromisoformat(iso_timestamp).strftime(_YIELD_GENERATOR_TIMESTAMP_FORMAT)
    except (ValueError, TypeError):
        return iso_timestamp


def _yield_generator_confirm_minutes(entry: TransactionHistoryEntry) -> str:
    """Return confirm time in minutes (rounded) for a maker entry, or ``""``.

    Computed from the broadcast (``timestamp``) and first-confirmation
    (``confirmed_at``) times when both are present.
    """
    if not entry.confirmed_at or not entry.timestamp:
        return ""
    try:
        start = datetime.fromisoformat(entry.timestamp)
        confirmed = datetime.fromisoformat(entry.confirmed_at)
    except (ValueError, TypeError):
        return ""
    minutes = (confirmed - start).total_seconds() / 60.0
    if minutes < 0:
        return ""
    return str(round(minutes, 2))


def format_yield_generator_report(
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> list[str]:
    """Build the yield-generator earnings report as reference-format CSV rows.

    Returns a list of comma-separated strings: a header row and one row per
    *successful* maker CoinJoin from ``history.csv`` (the row's
    ``fee_received`` is the cjfee earned, and ``net_fee`` is the amount earned
    after the mining-fee contribution).
    Reconstructed on-chain guesses are excluded because their maker role and
    gross cjfee cannot be established from public transaction data, while this
    compatibility report has no provenance field and promises exact earnings.

    The ``my input value/satoshi`` column is the aggregate value captured when
    the maker selected its inputs. Rows written before that value was retained
    report ``0``; the earnings columns (``cjfee``/``earned``) are exact.

    Args:
        data_dir: Data directory holding ``history.csv``.
        wallet_fingerprint: When set, restrict to one wallet's maker rows.
    """
    rows: list[str] = [",".join(YIELD_GENERATOR_REPORT_HEADER)]

    entries = read_history(
        data_dir,
        role_filter="maker",
        wallet_fingerprint=wallet_fingerprint,
    )
    # read_history returns most-recent-first; the statement reads chronologically.
    for entry in sorted(entries, key=lambda e: e.timestamp):
        if not entry.success or entry.source != "protocol":
            continue
        input_count = len(_parse_utxos(entry.utxos_used))
        row = [
            _format_yield_generator_timestamp(entry.timestamp),
            str(entry.cj_amount),
            str(input_count),
            str(entry.input_value),
            str(entry.fee_received),
            str(entry.net_fee),
            _yield_generator_confirm_minutes(entry),
            "",  # notes
        ]
        rows.append(",".join(row))

    return rows


def count_other_wallet_entries(
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
    role_filter: HistoryRole | None = None,
) -> int:
    """Count history rows that a per-wallet view hides for ``wallet_fingerprint``.

    Returns the number of rows whose ``wallet_fingerprint`` differs from the
    active wallet (including legacy rows with an empty fingerprint). Used by
    ``jm-wallet history`` to tell the user how many entries are excluded so
    the per-wallet scoping is never silent (they can pass ``--all-wallets``).

    When ``wallet_fingerprint`` is ``None`` (no scoping) this returns ``0``.
    """
    if wallet_fingerprint is None:
        return 0
    entries = read_history(data_dir, role_filter=role_filter)
    return sum(entry.wallet_fingerprint != wallet_fingerprint for entry in entries)


def purge_reconstructed_entries(
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> int:
    """Remove on-chain-reconstructed rows (``source="onchain"``) from history.

    Used by ``jm-wallet reconstruct-history`` to rebuild the guessed portion
    of a wallet's history from scratch without touching authoritative
    protocol-time rows. When ``wallet_fingerprint`` is given, only that
    wallet's reconstructed rows are removed.

    Returns:
        The number of rows removed.

    Raises:
        HistoryWriteError: If the pruned file cannot be written back.
    """
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return 0
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        kept = [
            e
            for e in entries
            if not (
                e.source == "onchain"
                and (wallet_fingerprint is None or e.wallet_fingerprint == wallet_fingerprint)
            )
        ]
        removed = len(entries) - len(kept)
        if removed == 0:
            return 0
        if not _write_history_entries_atomic(kept, history_path):
            raise HistoryWriteError(f"Failed to rewrite {history_path} after purging entries")
    logger.info(f"Purged {removed} reconstructed history entries")
    return removed


def delete_wallet_history_entries(
    wallet_fingerprint: str,
    data_dir: Path | None = None,
) -> int:
    """Remove every history row belonging to one wallet.

    The history CSV is shared by all wallets in a data directory. This helper
    performs a fingerprint-scoped atomic rewrite while holding the existing
    history lock, preserving other wallets' rows and unscoped legacy rows.

    Returns:
        The number of rows removed.

    Raises:
        HistoryWriteError: If the pruned file cannot be written back.
    """
    fingerprint = wallet_fingerprint.strip().lower()
    if len(fingerprint) != 8:
        raise ValueError("wallet fingerprint must be exactly 8 hex characters")
    try:
        bytes.fromhex(fingerprint)
    except ValueError as exc:
        raise ValueError("wallet fingerprint must be valid hexadecimal") from exc

    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return 0
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        kept = [entry for entry in entries if entry.wallet_fingerprint != fingerprint]
        removed = len(entries) - len(kept)
        if removed == 0:
            return 0
        if not _write_history_entries_atomic(kept, history_path):
            raise HistoryWriteError(
                f"Failed to rewrite {history_path} after deleting wallet history"
            )
    logger.info(f"Deleted {removed} history entries for wallet {fingerprint}")
    return removed


def list_history_fingerprints(data_dir: Path | None = None) -> list[str]:
    """List distinct wallet fingerprints recorded in the history CSV.

    Used by ``jm-wallet history`` to auto-select the active wallet when
    only one is present in the data directory, and to print a helpful
    error listing the available choices when several are. Empty
    fingerprints (legacy rows written before issue #473 added the
    column) are excluded; callers can fall back to ``--all-wallets``
    if they want to see those rows.

    Args:
        data_dir: Optional data directory (defaults to
            ``get_default_data_dir()``).

    Returns:
        Sorted list of distinct non-empty wallet fingerprints found in
        ``history.csv``. Returns an empty list when the file is missing
        or unreadable.
    """
    return sorted(
        {entry.wallet_fingerprint for entry in read_history(data_dir) if entry.wallet_fingerprint}
    )


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
    # Plain non-CoinJoin sends are tracked in the same CSV (for the address-reuse
    # ledger consumed by ``get_used_addresses``) but must not skew CoinJoin
    # success rate / volume / counts. Restrict the rest of the aggregates to
    # CoinJoin roles only.
    cj_entries = [e for e in entries if e.role in ("maker", "taker")]
    successful = [e for e in cj_entries if e.success]
    failed = [e for e in cj_entries if not e.success and e.completed_at]
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
    # Plain ``send`` entries do not disclose UTXOs to peers, so they are excluded.
    all_disclosed: set[str] = set()
    for e in cj_entries:
        all_disclosed |= _parse_utxos(e.utxos_used)

    return {
        "total_coinjoins": len(cj_entries),
        "maker_coinjoins": len(maker_entries),
        "taker_coinjoins": len(taker_entries),
        "successful_coinjoins": len(successful),
        "failed_coinjoins": len(failed),
        "total_volume": sum(e.cj_amount for e in cj_entries),
        "successful_volume": sum(e.cj_amount for e in successful),
        "total_fees_earned": sum(e.fee_received for e in successful_maker_entries),
        "total_fees_paid": sum(
            e.total_maker_fees_paid + e.mining_fee_paid for e in successful_taker_entries
        ),
        "success_rate": len(successful) / len(cj_entries) * 100 if cj_entries else 0.0,
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
    role_filter: HistoryRole | None = None,
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
    source_addresses: list[str] | None = None,
    input_value: int = 0,
    destination_vout: int = -1,
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
        wallet_fingerprint: Fingerprint of the wallet creating the entry
        source_addresses: Addresses corresponding to ``our_utxos``
        input_value: Total value of ``our_utxos`` in satoshis
        destination_vout: Destination CoinJoin output index, or -1 when unknown

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
        source_addresses=",".join(source_addresses) if source_addresses else "",
        network=network,
        wallet_fingerprint=wallet_fingerprint,
        input_value=input_value,
        destination_vout=destination_vout,
        amount=cj_amount,
    )


def get_pending_transactions(
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
    *,
    include_failed: bool = False,
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
        include_failed: Also check unsuccessful completed rows with a recorded
            txid. A local timeout or broadcast error cannot prove that a signed
            transaction will never confirm. Explicit wallet refresh uses this
            to reconcile old failed rows without restarting background monitoring
            or reviving unsigned attempts. Only verified confirmation repairs them.

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
        and (not e.completed_at or (include_failed and bool(e.txid)))
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
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return False
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        target = _select_entry_for_confirmation_update(entries, txid, wallet_fingerprint)
        if target is None:
            return False

        target.confirmations = confirmations
        if confirmations > 0 and not target.success:
            target.success = True
            target.failure_reason = ""
            target.confirmed_at = datetime.now().isoformat()
            target.completed_at = target.confirmed_at
            logger.bind(sensitive=True).info(
                f"Transaction {txid[:16]}... confirmed with {confirmations} confirmations"
            )
        elif confirmations > 0:
            logger.bind(sensitive=True).debug(
                f"Updated confirmations for {txid[:16]}...: {confirmations}"
            )

        return _write_history_entries_atomic(entries, history_path)


def abandon_transaction(
    txid: str,
    reason: str,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """Explicitly mark a transaction as abandoned in local history.

    Sets ``completed_at`` and ``failure_reason`` so ``get_pending_transactions``
    will no longer return the entry by default. Confirmation reconciliation
    still checks recorded txids: local abandonment cannot invalidate a signed
    transaction. ``success`` and ``confirmations`` are left unchanged.

    Args:
        txid: Transaction ID to abandon
        reason: Human-readable reason (stored in ``failure_reason``)
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only match entries belonging to this
            wallet.

    Returns:
        True if transaction was found and updated, False otherwise
    """
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return False
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        target = _select_entry_for_confirmation_update(entries, txid, wallet_fingerprint)
        if target is None:
            return False

        now = datetime.now().isoformat()
        target.completed_at = now
        target.failure_reason = reason
        logger.warning("Transaction abandoned")
        logger.bind(sensitive=True).warning(f"Transaction {txid[:16]}... abandoned: {reason}")
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
    initial_entries = read_history(data_dir)
    initial_target = _select_entry_for_confirmation_update(
        initial_entries, txid, wallet_fingerprint
    )
    if initial_target is None:
        return False

    detected_count: int | None = None
    if (
        confirmations > 0
        and not initial_target.success
        and initial_target.role == "maker"
        and initial_target.peer_count is None
        and backend is not None
        and initial_target.cj_amount > 0
    ):
        detected_count = await detect_coinjoin_peer_count(backend, txid, initial_target.cj_amount)

    # Peer detection performs network I/O. Reload after that await so a maker
    # session that appended a pre-reveal row in the meantime is not erased by
    # rewriting the stale snapshot read above.
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return False
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        target = _select_entry_for_confirmation_update(entries, txid, wallet_fingerprint)
        if target is None:
            return False

        target.confirmations = confirmations
        if confirmations > 0 and not target.success:
            target.success = True
            target.failure_reason = ""
            target.confirmed_at = datetime.now().isoformat()
            target.completed_at = target.confirmed_at
            logger.bind(sensitive=True).info(
                f"Transaction {txid[:16]}... confirmed with {confirmations} confirmations"
            )
        elif confirmations > 0:
            logger.bind(sensitive=True).debug(
                f"Updated confirmations for {txid[:16]}...: {confirmations}"
            )

        if detected_count is not None and target.role == "maker" and target.peer_count is None:
            target.peer_count = detected_count
            logger.bind(sensitive=True).info(
                f"Detected {detected_count} participants in CoinJoin {txid[:16]}..."
            )

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
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return False
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        for entry in entries:
            if entry.destination_address == destination_address and not entry.txid:
                if (
                    wallet_fingerprint is not None
                    and entry.wallet_fingerprint != wallet_fingerprint
                ):
                    continue
                entry.txid = txid
                logger.bind(sensitive=True).info(
                    f"Updated pending transaction for {destination_address[:20]}... "
                    f"with txid {txid[:16]}..."
                )
                return _write_history_entries_atomic(entries, history_path)
        return False


def update_awaiting_transaction_signed(
    destination_address: str,
    txid: str,
    fee_received: int,
    txfee_contribution: int,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
    destination_vout: int = -1,
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
        destination_vout: Destination CoinJoin output index, or -1 when unknown
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only match entries belonging to this
            wallet (issue #473).

    Returns:
        True if a matching entry was found and updated, False otherwise
    """
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return False
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        for entry in entries:
            if (
                entry.destination_address == destination_address
                and entry.failure_reason == "Awaiting transaction"
                and not entry.txid
            ):
                if (
                    wallet_fingerprint is not None
                    and entry.wallet_fingerprint != wallet_fingerprint
                ):
                    continue
                entry.txid = txid
                entry.fee_received = fee_received
                entry.txfee_contribution = txfee_contribution
                entry.net_fee = fee_received - txfee_contribution
                entry.destination_vout = destination_vout
                entry.failure_reason = "Pending confirmation"
                logger.bind(sensitive=True).info(
                    f"Updated awaiting transaction for {destination_address[:20]}... "
                    f"with txid {txid[:16]}..., fee={fee_received} sats"
                )
                return _write_history_entries_atomic(entries, history_path)
        return False


def update_taker_awaiting_transaction_broadcast(
    destination_address: str,
    change_address: str,
    txid: str,
    mining_fee: int,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
    broadcast_method: str | None = None,
    broadcast_policy: str | None = None,
    broadcast_fallback_reason: str | None = None,
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
        broadcast_method: Actual successful broadcast method.
        broadcast_policy: Configured broadcast policy.
        broadcast_fallback_reason: Stable reason for a self-broadcast fallback.

    Returns:
        True if a matching entry was found and updated, False otherwise
    """
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return False
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        for entry in entries:
            if (
                entry.destination_address == destination_address
                and entry.change_address == change_address
                and entry.failure_reason == "Awaiting transaction"
                and not entry.txid
            ):
                if (
                    wallet_fingerprint is not None
                    and entry.wallet_fingerprint != wallet_fingerprint
                ):
                    continue
                entry.txid = txid
                entry.mining_fee_paid = mining_fee
                entry.net_fee = -(entry.total_maker_fees_paid + mining_fee)
                if broadcast_method is not None:
                    entry.broadcast_method = broadcast_method
                if broadcast_policy is not None:
                    entry.broadcast_policy = broadcast_policy
                if broadcast_fallback_reason is not None:
                    entry.broadcast_fallback_reason = broadcast_fallback_reason
                entry.failure_reason = "Pending confirmation"
                logger.bind(sensitive=True).info(
                    f"Updated awaiting transaction for {destination_address[:20]}... "
                    f"with txid {txid[:16]}..., mining_fee={mining_fee} sats"
                )
                return _write_history_entries_atomic(entries, history_path)
        return False


def update_send_awaiting_broadcast(
    pending_entry: TransactionHistoryEntry,
    *,
    txid: str,
    success: bool,
    failure_reason: str,
    data_dir: Path | None = None,
) -> bool:
    """Finalize a "send" history row created with ``failure_reason="awaiting broadcast"``.

    The send CLI calls :func:`create_send_history_entry` and immediately
    appends the row so the destination/change addresses are persisted to
    disk *before* the broadcast attempt. After broadcast resolves (success
    or failure) the same row is updated in place with the final outcome.

    Args:
        pending_entry: The in-memory entry that was just appended. Its wallet,
            timestamp, addresses, and selected UTXOs identify the row.
        txid: Final transaction ID (empty string if broadcast failed).
        success: True if the transaction was broadcast successfully.
        failure_reason: Final failure reason (empty string on success).
        data_dir: Optional data directory.

    Returns:
        True if a matching row was found and rewritten, False otherwise.
    """
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return False
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        for entry in entries:
            if (
                entry.role == "send"
                and entry.wallet_fingerprint == pending_entry.wallet_fingerprint
                and entry.timestamp == pending_entry.timestamp
                and entry.destination_address == pending_entry.destination_address
                and entry.change_address == pending_entry.change_address
                and entry.utxos_used == pending_entry.utxos_used
                and entry.failure_reason == "awaiting broadcast"
                and not entry.txid
            ):
                entry.txid = txid
                entry.success = success
                entry.failure_reason = failure_reason
                entry.completed_at = entry.timestamp
                return _write_history_entries_atomic(entries, history_path)
        return False


def mark_pending_transaction_failed(
    destination_address: str,
    failure_reason: str,
    data_dir: Path | None = None,
    txid: str | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """
    Mark a pending transaction as failed by matching the destination address and optionally txid.

    This records a local outcome, not proof that a signed transaction cannot
    confirm. Timeouts use ``expire_pending_transaction_monitoring`` so they
    have a distinct reason. Recorded txids remain eligible for confirmation
    reconciliation on an explicit wallet refresh.

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
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return False
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        for entry in entries:
            if (
                entry.destination_address == destination_address
                and not entry.success
                and entry.confirmations == 0
                and not entry.completed_at
            ):
                if txid is not None and entry.txid != txid:
                    continue
                if (
                    wallet_fingerprint is not None
                    and entry.wallet_fingerprint != wallet_fingerprint
                ):
                    continue

                entry.success = False
                entry.failure_reason = failure_reason
                entry.completed_at = datetime.now().isoformat()
                txid_str = f" (txid: {entry.txid[:16]}...)" if entry.txid else ""
                logger.bind(sensitive=True).info(
                    f"Marked pending transaction for {destination_address[:20]}...{txid_str} "
                    f"as failed: {failure_reason}"
                )
                return _write_history_entries_atomic(entries, history_path)
        return False


def expire_pending_transaction_monitoring(
    entry: TransactionHistoryEntry,
    max_age_minutes: int,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> bool:
    """Stop active monitoring at its deadline without declaring chain failure.

    Call before backend I/O so even very old entries on an upgraded installation
    do not restart polling. A successful concurrent confirmation is protected by
    ``mark_pending_transaction_failed``'s locked state check. Return whether the
    deadline has elapsed, including when the row was already finalized.

    This only updates local history. It does not abandon the transaction in
    Bitcoin Core, release inputs, or prevent explicit late-confirmation recovery.
    Invalid timestamps do not establish an elapsed deadline.
    """
    try:
        timestamp = datetime.fromisoformat(entry.timestamp)
    except ValueError:
        return False
    age = datetime.now(tz=timestamp.tzinfo) - timestamp
    if age < timedelta(minutes=max_age_minutes):
        return False

    mark_pending_transaction_failed(
        destination_address=entry.destination_address,
        failure_reason=(
            f"{MONITORING_TIMEOUT_REASON_PREFIX} "
            f"{'confirmation' if entry.txid else 'transaction discovery'} "
            f"deadline of {max_age_minutes} minutes elapsed"
        ),
        data_dir=data_dir,
        txid=entry.txid,
        wallet_fingerprint=wallet_fingerprint,
    )
    return True


def cleanup_stale_pending_transactions(
    max_age_minutes: int = 60,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> int:
    """
    Explicit compatibility operation to mark old pending history rows failed.

    Do not call this during automatic monitoring or wallet display: age does
    not prove failure. This only changes local bookkeeping and cannot cancel
    a transaction. Recorded txids remain eligible for confirmation reconciliation.

    Args:
        max_age_minutes: Mark entries older than this as failed (default: 60)
        data_dir: Optional data directory
        wallet_fingerprint: If provided, only clean up stale pending entries
            for the given wallet (issue #473). Other wallets' pending entries
            in the same shared CSV are left untouched.

    Returns:
        Number of entries marked as failed
    """
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return 0
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        count = 0
        now = datetime.now()

        for entry in entries:
            if not entry.success and entry.confirmations == 0 and not entry.completed_at:
                if (
                    wallet_fingerprint is not None
                    and entry.wallet_fingerprint != wallet_fingerprint
                ):
                    continue
                try:
                    timestamp = datetime.fromisoformat(entry.timestamp)
                    age_minutes = (now - timestamp).total_seconds() / 60

                    if age_minutes >= max_age_minutes:
                        entry.completed_at = now.isoformat()
                        entry.failure_reason = (
                            "Cleaned up: pending for "
                            f"{int(age_minutes)} minutes without confirmation"
                        )
                        txid_str = f" (txid: {entry.txid[:16]}...)" if entry.txid else ""
                        logger.bind(sensitive=True).info(
                            f"Marked stale pending entry{txid_str} as failed "
                            f"(age: {int(age_minutes)} minutes)"
                        )
                        count += 1
                except (ValueError, TypeError) as exc:
                    logger.debug(f"Error parsing timestamp for entry: {exc}")
                    continue

        if count == 0:
            return 0
        return count if _write_history_entries_atomic(entries, history_path) else 0


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
    broadcast_policy: str = "",
    network: str = "mainnet",
    success: bool = False,  # Default to pending
    failure_reason: str = "Awaiting transaction",
    wallet_fingerprint: str = "",
    source_addresses: list[str] | None = None,
    destination_vout: int = -1,
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
        broadcast_policy: Configured broadcast policy, when known
        network: Network name
        success: Whether the CoinJoin succeeded (default False for pending)
        failure_reason: Reason for failure if any (default "Awaiting transaction")
        destination_vout: Destination CoinJoin output index, or -1 when unknown

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
        source_addresses=",".join(source_addresses) if source_addresses else "",
        broadcast_method=broadcast_method,
        broadcast_policy=broadcast_policy,
        network=network,
        wallet_fingerprint=wallet_fingerprint,
        destination_vout=destination_vout,
        amount=cj_amount,
    )


def create_send_history_entry(
    destination: str,
    change_address: str,
    amount: int,
    mining_fee: int,
    source_mixdepth: int,
    selected_utxos: list[tuple[str, int]],
    txid: str = "",
    success: bool = True,
    failure_reason: str = "",
    network: str = "mainnet",
    wallet_fingerprint: str = "",
    source_addresses: list[str] | None = None,
) -> TransactionHistoryEntry:
    """Create a history entry for a plain (non-CoinJoin) wallet send.

    The point of recording these entries is privacy/correctness of the
    next-unused-address pointer: once the wallet has signed a transaction
    that exposes ``destination`` and/or ``change_address``, both must be
    treated as used regardless of broadcast outcome (the signed bytes are
    already out of the wallet's control) and regardless of whether Bitcoin
    Core's transaction history still surfaces the transaction (an
    interrupted background rescan or a smart-scan window that drops the
    spend would otherwise leave the addresses looking fresh).

    ``get_used_addresses()`` consumes every row regardless of ``role``, so
    persisting a row with ``role="send"`` is enough to keep
    ``WalletService.get_next_address_index()`` from handing the same
    address out twice.

    Args:
        destination: Destination address (recorded so we never propose it
            as a fresh deposit address if it happens to be one of ours).
        change_address: Change output address (always ours; empty if the
            send had no change, e.g., a sweep).
        amount: Amount sent to ``destination`` in sats.
        mining_fee: Mining fee paid in sats.
        source_mixdepth: Source mixdepth the spend was funded from.
        selected_utxos: List of ``(txid, vout)`` tuples for our inputs.
        txid: Transaction ID (empty string if not yet known / not broadcast).
        success: Whether the transaction was successfully broadcast.
        failure_reason: Reason for failure if any.
        network: Network name.
        wallet_fingerprint: Wallet fingerprint for issue #473 scoping.

    Returns:
        A ``TransactionHistoryEntry`` ready to be appended via
        :func:`append_history_entry`.
    """
    now = datetime.now().isoformat()
    return TransactionHistoryEntry(
        timestamp=now,
        completed_at=now,
        role="send",
        success=success,
        failure_reason=failure_reason,
        confirmations=0,
        confirmed_at="",
        txid=txid,
        cj_amount=0,
        amount=amount,
        peer_count=None,
        counterparty_nicks="",
        mining_fee_paid=mining_fee,
        net_fee=-mining_fee,
        source_mixdepth=source_mixdepth,
        destination_address=destination,
        change_address=change_address,
        utxos_used=",".join(f"{t}:{v}" for t, v in selected_utxos),
        source_addresses=",".join(source_addresses) if source_addresses else "",
        broadcast_method="self",
        network=network,
        wallet_fingerprint=wallet_fingerprint,
    )


def get_used_addresses(
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> set[str]:
    """
    Get all addresses that have been used in CoinJoin history.

    Returns destination addresses (CoinJoin outputs), change addresses, and
    source (input) addresses from all history entries, regardless of success
    or confirmation status.

    This is critical for privacy: once an address has been shared with peers
    (even if the transaction failed or wasn't confirmed), it should never be
    reused. Input addresses are included so that a spent deposit address is
    never proposed again, even if the backend later loses sight of the
    spending transaction (e.g., interrupted rescan, smart-scan window miss).

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
        if entry.source_addresses:
            for addr in entry.source_addresses.split(","):
                addr = addr.strip()
                if addr:
                    used_addresses.add(addr)

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

    Plain wallet spends (``role="send"``) are intentionally excluded: their
    destination and change addresses are ordinary deposits/change, not CoinJoin
    outputs, so classifying them here would mislabel them (issue #517).

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
        # Only CoinJoin participation (maker/taker) produces CoinJoin output and
        # change addresses. Plain wallet spends (role="send", e.g. internal
        # mixdepth-to-mixdepth transfers used to reset PoDLE retry counters) are
        # recorded only to keep their addresses marked as used; classifying their
        # destination as "cj_out" or change as "change" would mislabel them as
        # CoinJoin outputs and create false privacy expectations (issue #517).
        # Reconstructed deposits are likewise ordinary receives, not CoinJoin
        # outputs.
        if entry.role in ("send", "deposit"):
            continue

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


def get_protocol_coinjoin_output_outpoints(
    current_utxos: Iterable[UTXOInfo],
    *,
    network: str,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> set[str]:
    """Return current outpoints that exactly match protocol CoinJoin outputs.

    Address-level history is insufficient for coin selection because a later
    payment can reuse a CoinJoin destination. New rows identify the exact
    ``(txid, vout)``; legacy rows without an output index fall back to matching
    the creating transaction, destination address, and equal-output amount.
    Best-effort on-chain reconstruction is intentionally excluded.
    """
    entries = [
        entry
        for entry in read_history(data_dir, wallet_fingerprint=wallet_fingerprint)
        if entry.network == network
        and entry.success
        and entry.txid
        and entry.destination_address
        and entry.cj_amount > 0
        and entry.role in ("maker", "taker")
        and entry.source == "protocol"
    ]
    exact_outpoints = {
        (entry.txid, entry.destination_vout) for entry in entries if entry.destination_vout >= 0
    }
    legacy_outputs = {
        (entry.txid, entry.destination_address, entry.cj_amount)
        for entry in entries
        if entry.destination_vout < 0
    }
    return {
        utxo.outpoint
        for utxo in current_utxos
        if (utxo.txid, utxo.vout) in exact_outpoints
        or (utxo.txid, utxo.address, utxo.value) in legacy_outputs
    }


def _index_outpoint_addresses(
    targets: Iterable[UTXOInfo],
    entry_inputs: Iterable[list[tuple[str, str]] | None],
) -> tuple[dict[str, str], set[str]]:
    """Index known outpoints by address, excluding conflicting metadata."""
    outpoint_addresses = {utxo.outpoint: utxo.address for utxo in targets}
    ambiguous_outpoints: set[str] = set()
    for inputs in entry_inputs:
        if inputs is None:
            continue
        for outpoint, address in inputs:
            existing = outpoint_addresses.get(outpoint)
            if existing is not None and existing != address:
                ambiguous_outpoints.add(outpoint)
                continue
            outpoint_addresses[outpoint] = address

    for outpoint in ambiguous_outpoints:
        outpoint_addresses.pop(outpoint, None)
    return outpoint_addresses, ambiguous_outpoints


def get_coinjoin_lineage_outpoints(
    current_utxos: Iterable[UTXOInfo],
    *,
    network: str,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> set[str]:
    """Return current outpoints with CoinJoin-only provenance.

    A successful maker/taker equal output starts a private lineage. Change joins
    that lineage only when every exact input outpoint in an authoritative
    protocol row is already private. This excludes deposit-derived and mixed
    change, imported rows with potentially partial input discovery, and legacy
    rows without complete input metadata.

    Outpoints, rather than addresses, are tracked so a later payment to a reused
    CoinJoin address remains warning-eligible.
    """
    targets = list(current_utxos)
    target_outpoints = {utxo.outpoint for utxo in targets}
    targets_by_outpoint = {utxo.outpoint: utxo for utxo in targets}
    entries = [
        entry
        for entry in read_history(data_dir, wallet_fingerprint=wallet_fingerprint)
        if entry.network == network
    ]
    successful_coinjoins = [
        entry
        for entry in entries
        if entry.success and entry.cj_amount > 0 and entry.role in ("maker", "taker")
    ]

    # Build an exact outpoint -> address index from current coins and from every
    # later transaction that recorded the outpoint as one of its wallet inputs.
    entry_inputs: dict[int, list[tuple[str, str]] | None] = {}
    for index, entry in enumerate(entries):
        outpoints = [value.strip() for value in entry.utxos_used.split(",") if value.strip()]
        addresses = [value.strip() for value in entry.source_addresses.split(",") if value.strip()]
        pairs = (
            list(zip(outpoints, addresses, strict=True))
            if len(outpoints) == len(addresses)
            else None
        )
        entry_inputs[index] = pairs if pairs else None
    outpoint_addresses, _ambiguous_outpoints = _index_outpoint_addresses(
        targets, entry_inputs.values()
    )

    outputs_by_tx_address: dict[tuple[str, str], set[str]] = defaultdict(set)
    for outpoint, address in outpoint_addresses.items():
        txid, separator, _vout = outpoint.rpartition(":")
        if separator and txid:
            outputs_by_tx_address[(txid, address)].add(outpoint)

    lineage: set[str] = set()
    for entry in successful_coinjoins:
        if entry.txid and entry.destination_address:
            candidates = outputs_by_tx_address[(entry.txid, entry.destination_address)]
            if len(candidates) != 1:
                continue
            candidate = next(iter(candidates))
            current = targets_by_outpoint.get(candidate)
            if current is not None and current.value != entry.cj_amount:
                continue
            lineage.add(candidate)

    requirements: dict[str, set[str]] = defaultdict(set)
    invalid_change_outpoints: set[str] = set()
    for index, entry in enumerate(entries):
        if not entry.success or not entry.txid or not entry.change_address:
            continue
        change_outpoints = outputs_by_tx_address[(entry.txid, entry.change_address)]
        if not change_outpoints:
            continue
        if len(change_outpoints) != 1:
            invalid_change_outpoints.update(change_outpoints)
            continue
        if entry.destination_address == entry.change_address:
            invalid_change_outpoints.update(change_outpoints)
            continue
        inputs = entry_inputs[index]
        if (
            entry.source != "protocol"
            or entry.role not in ("maker", "taker", "send")
            or inputs is None
        ):
            invalid_change_outpoints.update(change_outpoints)
            continue
        input_outpoints = {outpoint for outpoint, _address in inputs}
        for change_outpoint in change_outpoints:
            requirements[change_outpoint].update(input_outpoints)

    lineage.difference_update(invalid_change_outpoints)

    # Resolve the dependency graph with a queue. Duplicate/conflicting or
    # reconstructed change rows fail closed and cannot enter the lineage.
    unresolved: dict[str, set[str]] = {}
    dependents: dict[str, set[str]] = defaultdict(set)
    queue = deque(lineage)
    for change_outpoint, input_outpoints in requirements.items():
        if change_outpoint in invalid_change_outpoints or change_outpoint in lineage:
            continue
        missing = input_outpoints - lineage
        if not missing:
            lineage.add(change_outpoint)
            queue.append(change_outpoint)
            continue
        unresolved[change_outpoint] = missing
        for input_outpoint in missing:
            dependents[input_outpoint].add(change_outpoint)

    while queue:
        private_outpoint = queue.popleft()
        for change_outpoint in dependents.pop(private_outpoint, set()):
            remaining_inputs = unresolved.get(change_outpoint)
            if remaining_inputs is None:
                continue
            remaining_inputs.discard(private_outpoint)
            if remaining_inputs:
                continue
            unresolved.pop(change_outpoint)
            lineage.add(change_outpoint)
            queue.append(change_outpoint)

    return lineage & target_outpoints


def _parse_complete_input_metadata(entry: TransactionHistoryEntry) -> list[tuple[str, str]] | None:
    """Return validated input outpoint/address pairs, or None when incomplete."""
    outpoints = [value.strip() for value in entry.utxos_used.split(",")]
    addresses = [value.strip() for value in entry.source_addresses.split(",")]
    if (
        not outpoints
        or len(outpoints) != len(addresses)
        or any(
            not outpoint or not address
            for outpoint, address in zip(outpoints, addresses, strict=True)
        )
        or len(set(outpoints)) != len(outpoints)
    ):
        return None

    for outpoint in outpoints:
        txid, separator, vout = outpoint.rpartition(":")
        if not separator or len(txid) != 64 or not vout.isdecimal():
            return None
        try:
            bytes.fromhex(txid)
        except ValueError:
            return None

    return list(zip(outpoints, addresses, strict=True))


def get_maker_rotation_lineage_outpoints(
    current_utxos: Iterable[UTXOInfo],
    *,
    network: str,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> set[str]:
    """Return current outpoints eligible for strict maker rotation lineage.

    Roots must be exact successful protocol CoinJoin outputs recorded by a maker
    or taker row. Change propagates only through successful protocol maker/taker
    CoinJoin rows whose complete input outpoint/address metadata proves that all
    inputs are already in the lineage. Ambiguous, reconstructed, legacy, failed,
    mixed, and out-of-scope history fails closed.

    Unlike :func:`get_coinjoin_lineage_outpoints`, this helper deliberately does
    not propagate lineage through plain protocol sends. It is intended for maker
    rotation decisions, where only a continuous protocol CoinJoin lineage is
    acceptable.
    """
    targets = list(current_utxos)
    target_outpoints = {utxo.outpoint for utxo in targets}
    targets_by_outpoint = {utxo.outpoint: utxo for utxo in targets}
    entries = [
        entry
        for entry in read_history(data_dir, wallet_fingerprint=wallet_fingerprint)
        if entry.network == network
    ]
    entry_inputs = {
        index: _parse_complete_input_metadata(entry) for index, entry in enumerate(entries)
    }
    outpoint_addresses, ambiguous_outpoints = _index_outpoint_addresses(
        targets, entry_inputs.values()
    )

    outputs_by_tx_address: dict[tuple[str, str], set[str]] = defaultdict(set)
    for outpoint, address in outpoint_addresses.items():
        txid, separator, _vout = outpoint.rpartition(":")
        if separator and txid:
            outputs_by_tx_address[(txid, address)].add(outpoint)

    lineage: set[str] = set()
    invalid_outpoints = set(ambiguous_outpoints)
    root_claims: set[str] = set()
    for index, entry in enumerate(entries):
        if not entry.txid or not entry.destination_address or entry.destination_vout < 0:
            continue
        root_outpoint = f"{entry.txid}:{entry.destination_vout}"
        if outpoint_addresses.get(root_outpoint) != entry.destination_address:
            continue
        if root_outpoint in root_claims:
            invalid_outpoints.add(root_outpoint)
            continue
        root_claims.add(root_outpoint)
        if (
            entry.success
            and entry.source == "protocol"
            and entry.role in ("maker", "taker")
            and entry.cj_amount > 0
        ):
            current = targets_by_outpoint.get(root_outpoint)
            if current is None or current.value == entry.cj_amount:
                lineage.add(root_outpoint)
        else:
            invalid_outpoints.add(root_outpoint)

    requirements: dict[str, set[str]] = {}
    for index, entry in enumerate(entries):
        if not entry.txid or not entry.change_address:
            continue
        change_outpoints = outputs_by_tx_address[(entry.txid, entry.change_address)]
        if len(change_outpoints) != 1:
            invalid_outpoints.update(change_outpoints)
            continue
        change_outpoint = next(iter(change_outpoints))
        if change_outpoint in requirements:
            invalid_outpoints.add(change_outpoint)
            continue
        inputs = entry_inputs[index]
        if (
            not entry.success
            or entry.source != "protocol"
            or entry.role not in ("maker", "taker")
            or entry.cj_amount <= 0
            or entry.destination_address == entry.change_address
            or inputs is None
        ):
            invalid_outpoints.add(change_outpoint)
            continue
        requirements[change_outpoint] = {outpoint for outpoint, _address in inputs}

    lineage.difference_update(invalid_outpoints)
    unresolved: dict[str, set[str]] = {}
    dependents: dict[str, set[str]] = defaultdict(set)
    queue = deque(lineage)
    for change_outpoint, input_outpoints in requirements.items():
        if change_outpoint in invalid_outpoints or change_outpoint in lineage:
            continue
        missing = input_outpoints - lineage
        if not missing:
            lineage.add(change_outpoint)
            queue.append(change_outpoint)
            continue
        unresolved[change_outpoint] = missing
        for input_outpoint in missing:
            dependents[input_outpoint].add(change_outpoint)

    while queue:
        private_outpoint = queue.popleft()
        for change_outpoint in dependents.pop(private_outpoint, set()):
            remaining_inputs = unresolved.get(change_outpoint)
            if remaining_inputs is None:
                continue
            remaining_inputs.discard(private_outpoint)
            if remaining_inputs:
                continue
            unresolved.pop(change_outpoint)
            lineage.add(change_outpoint)
            queue.append(change_outpoint)

    return lineage & target_outpoints


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


# Address-origin tags persisted by import-time on-chain label reconstruction.
# These follow the BIP-329 ``jm:used:<origin>`` convention documented in
# ``jmwallet.wallet.utxo_metadata`` and are consumed back by
# ``UTXOMetadataStore.get_coinjoin_address_types`` for the wallet display.
ORIGIN_CJ_OUT = "cj_out"
ORIGIN_CJ_CHANGE = "cj_change"
ORIGIN_DEPOSIT = "deposit"
ORIGIN_NON_CJ_CHANGE = "non_cj_change"

# Origins that mark an address as already classified by on-chain analysis, so a
# later reconstruction pass can skip re-fetching its creating transaction.
CLASSIFIED_ORIGINS = frozenset(
    {ORIGIN_CJ_OUT, ORIGIN_CJ_CHANGE, ORIGIN_DEPOSIT, ORIGIN_NON_CJ_CHANGE}
)


def classify_imported_output(
    analysis: CoinjoinAnalysis,
    value: int,
    is_external: bool,
) -> str:
    """Classify a wallet output from its creating transaction's structure.

    Returns one of the address-origin tags (:data:`ORIGIN_CJ_OUT`,
    :data:`ORIGIN_CJ_CHANGE`, :data:`ORIGIN_DEPOSIT`,
    :data:`ORIGIN_NON_CJ_CHANGE`) following the same rules legacy
    joinmarket-clientserver uses to label coins on display:

    - an equal-amount CoinJoin output -> ``cj_out``
    - our other output inside a CoinJoin (the change) -> ``cj_change``
    - a non-CoinJoin coin on the external branch -> ``deposit``
    - a non-CoinJoin coin on the internal branch -> ``non_cj_change``

    The CoinJoin cases are decided purely by output value (parity with the
    reference), so an equal-amount output is labeled ``cj_out`` regardless of
    which branch it sits on.
    """
    if analysis.is_coinjoin and value == analysis.cj_amount:
        return ORIGIN_CJ_OUT
    if analysis.is_coinjoin:
        return ORIGIN_CJ_CHANGE
    return ORIGIN_DEPOSIT if is_external else ORIGIN_NON_CJ_CHANGE


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
            logger.warning("Could not fetch transaction for peer count detection")
            logger.bind(sensitive=True).warning(
                f"Could not fetch transaction {txid} for peer count detection"
            )
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

        logger.bind(sensitive=True).debug(
            f"Detected {equal_amount_count} equal-amount outputs "
            f"({cj_amount:,} sats each) in tx {txid[:16]}..."
        )
        return equal_amount_count

    except Exception as e:
        logger.warning("Failed to detect peer count")
        logger.bind(sensitive=True).warning(
            f"Failed to detect peer count for tx {txid[:16]}...: {e}"
        )
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
    with _locked_history_path(data_dir) as history_path:
        if not history_path.exists():
            return False
        _ensure_history_header_current(history_path)
        entries = _read_history_entries_unlocked(history_path)
        for entry in entries:
            if entry.txid == txid and entry.peer_count is None:
                entry.peer_count = peer_count
                logger.bind(sensitive=True).info(
                    f"Updated peer count for tx {txid[:16]}... to {peer_count}"
                )
                return _write_history_entries_atomic(entries, history_path)
        return False


async def update_all_pending_transactions(
    backend: BlockchainBackend | Any,
    data_dir: Path | None = None,
    wallet_fingerprint: str | None = None,
) -> int:
    """
    Update the status of all pending transactions using the blockchain backend.

    This function is called when displaying wallet info to ensure recorded
    transactions are updated with their current confirmation status. Failed
    rows with a txid are also checked, including timeouts from older versions.
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
    pending = get_pending_transactions(
        data_dir, wallet_fingerprint=wallet_fingerprint, include_failed=True
    )
    if not pending:
        return 0

    updated_count = 0
    # Full node / mempool-API backends report confirmation depth via
    # get_transaction(). Light clients (Neutrino) are mempool-only there
    # (get_transaction reports confirmations=0 and returns None once a tx
    # confirms), so they must confirm via verify_tx_output() against the output
    # address. has_mempool_access() is not a reliable proxy: neutrino with the
    # watched mempool tracker has mempool access yet still cannot report
    # confirmations by txid.
    can_lookup_by_txid = backend.can_get_confirmations_by_txid()

    for entry in pending:
        if not entry.txid:
            # Can't check without txid
            continue

        try:
            if can_lookup_by_txid:
                # Full node: can check confirmation depth directly by txid
                tx_info = await backend.get_transaction(entry.txid)
                if tx_info is not None:
                    # Only mark as success after first block confirmation.
                    if tx_info.confirmations > 0:
                        update_transaction_confirmation(
                            txid=entry.txid,
                            confirmations=tx_info.confirmations,
                            data_dir=data_dir,
                            wallet_fingerprint=wallet_fingerprint,
                        )
                        updated_count += 1
                        logger.debug(
                            f"Updated pending tx {entry.txid[:16]}... "
                            f"({tx_info.confirmations} confs)"
                        )
            else:
                # Neutrino: get_transaction cannot report confirmations, so
                # confirm via a compact-filter match on the output address.
                if not entry.destination_address:
                    continue

                try:
                    current_height = await backend.get_block_height()
                except Exception:
                    current_height = None

                verified = await verify_history_destination_output(
                    backend,
                    txid=entry.txid,
                    destination_address=entry.destination_address,
                    destination_vout=entry.destination_vout,
                    peer_count=entry.peer_count,
                    start_height=current_height,
                )

                if verified:
                    update_transaction_confirmation(
                        txid=entry.txid,
                        confirmations=1,
                        data_dir=data_dir,
                        wallet_fingerprint=wallet_fingerprint,
                    )
                    updated_count += 1
                    logger.bind(sensitive=True).debug(
                        f"Updated pending tx {entry.txid[:16]}... via Neutrino"
                    )

        except Exception as e:
            logger.bind(sensitive=True).debug(
                f"Could not update pending tx {entry.txid[:16]}...: {e}"
            )

    if updated_count > 0:
        logger.info(f"Updated {updated_count} pending transaction(s)")

    return updated_count
