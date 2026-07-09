"""
UTXO and address metadata persistence using BIP-329 wallet labels export format.

Stores UTXO-level metadata (frozen state, labels) and address-level metadata
(addresses with on-chain history) in a single JSONL file. Each line is a
BIP-329 record. This enables interoperability with external wallets like
Sparrow for coin control and labeling.

BIP-329 format (JSON Lines)::

    {"type": "output", "ref": "txid:vout", "spendable": false}
    {"type": "output", "ref": "txid:vout", "label": "cold storage"}
    {"type": "addr",   "ref": "<address>", "label": "jm:used:deposit"}

The ``spendable`` field maps to frozen state:
    - ``spendable: false`` -> UTXO is frozen
    - ``spendable: true`` or absent -> UTXO is spendable (not frozen)

The ``addr`` records track which on-chain addresses the wallet has ever held
funds at (including spent-then-empty addresses). This is a privacy-critical
guarantee: once an address has been observed with any UTXO it must never be
reissued as a "next unused" deposit address. Light-client backends (Neutrino)
and Bitcoin Core's address-book-bound RPCs alone cannot give us that
guarantee across restarts; the persistent ``addr`` records do.

Label convention (informational, ignored by other BIP-329 consumers):
``jm:used[:<origin>]`` where ``origin`` is one of ``deposit``, ``change``,
``cj_out``, ``cj_in``, ``send`` (or a comma-separated combination). The
``origin`` part is best-effort context; the mere presence of the record is
the privacy-relevant fact.

Reference: https://github.com/bitcoin/bips/blob/master/bip-0329.mediawiki
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

try:
    import fcntl  # POSIX advisory locks; absent on Windows.
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

USED_LABEL_PREFIX = "jm:used"

# Label prefix for addresses the user has set aside ("reserved"). Unlike
# ``jm:used`` markers (which mean the address has on-chain history), a
# ``jm:reserved`` marker means the address was handed out or manually
# reserved by the user: it must not be reissued as a "next unused" deposit
# address and is hidden from the concise ``jm-wallet info`` view, but it does
# NOT imply the address has ever held funds. An optional free-form user label
# follows the prefix: ``jm:reserved`` or ``jm:reserved:<label>``.
RESERVED_LABEL_PREFIX = "jm:reserved"

# Label applied to UTXOs frozen automatically by the forced-address-reuse
# defense (issue #529). The label keeps the record around after a user
# ``unfreeze`` so the same UTXO is never silently re-frozen on the next sync.
AUTO_FREEZE_REUSE_LABEL = "jm:autofrozen:reuse"

# Default lifetime of a temporary CoinJoin UTXO lock. A round that fails,
# crashes, or is killed without releasing its locks will have them auto-expire
# after this many seconds, so funds are never blocked permanently.
DEFAULT_COINJOIN_LOCK_TTL = 600.0


@dataclass
class OutputRecord:
    """A BIP-329 output record for UTXO metadata.

    Attributes:
        ref: Outpoint string in ``txid:vout`` format.
        spendable: Whether the UTXO is spendable. ``False`` means frozen.
            ``None`` means no opinion (importing wallet should not alter state).
        label: Optional human-readable label.
    """

    ref: str
    spendable: bool | None = None
    label: str | None = None
    lock_until: float | None = None

    @property
    def is_frozen(self) -> bool:
        """Whether this UTXO is frozen (not spendable)."""
        return self.spendable is False

    def is_locked(self, now: float) -> bool:
        """Whether this UTXO holds a non-expired temporary CoinJoin lock.

        A lock is a *time-limited* reservation (distinct from a user freeze):
        it is set while an input is committed to an in-flight CoinJoin so that
        another concurrent round (in this or another process, maker or taker)
        does not select the same UTXO and create a conflicting transaction. It
        auto-expires after ``lock_until`` so a crashed/killed round never blocks
        funds forever.
        """
        return self.lock_until is not None and self.lock_until > now

    @property
    def has_metadata(self) -> bool:
        """Whether this record carries any state worth persisting."""
        return self.spendable is not None or self.label is not None or self.lock_until is not None

    def to_dict(self) -> dict[str, str | bool | float]:
        """Serialize to a BIP-329 JSON dict.

        ``jm_lock_until`` is a JoinMarket extension (other BIP-329 consumers
        ignore unknown keys); it carries the temporary CoinJoin lock expiry.
        """
        d: dict[str, str | bool | float] = {"type": "output", "ref": self.ref}
        if self.spendable is not None:
            d["spendable"] = self.spendable
        if self.label is not None:
            d["label"] = self.label
        if self.lock_until is not None:
            d["jm_lock_until"] = self.lock_until
        return d

    @classmethod
    def from_dict(cls, d: dict[str, str | bool | float]) -> OutputRecord | None:
        """Deserialize from a BIP-329 JSON dict.

        Returns None if the record is not a valid output record.
        """
        if d.get("type") != "output":
            return None
        ref = d.get("ref")
        if not isinstance(ref, str):
            return None
        spendable = d.get("spendable")
        if spendable is not None and not isinstance(spendable, bool):
            return None
        label = d.get("label")
        if label is not None and not isinstance(label, str):
            label = str(label)
        lock_until_raw = d.get("jm_lock_until")
        lock_until: float | None
        if isinstance(lock_until_raw, (int, float)) and not isinstance(lock_until_raw, bool):
            lock_until = float(lock_until_raw)
        else:
            lock_until = None
        return cls(ref=ref, spendable=spendable, label=label, lock_until=lock_until)


@dataclass
class AddressRecord:
    """A BIP-329 ``addr`` record marking an address with on-chain history.

    The mere presence of a record means: this address has been observed
    holding (or having held) funds and must never be reissued. The ``label``
    encodes optional origin context using the ``jm:used[:origin]`` convention.

    Attributes:
        ref: Bitcoin address.
        label: ``jm:used`` or ``jm:used:<origin>`` (``deposit``, ``change``,
            ``cj_out``, ``cj_in``, ``send``).
    """

    ref: str
    label: str = USED_LABEL_PREFIX

    @property
    def origins(self) -> set[str]:
        """Decode the comma-separated origin set from the label, if any."""
        if not self.label.startswith(USED_LABEL_PREFIX):
            return set()
        rest = self.label[len(USED_LABEL_PREFIX) :]
        if not rest.startswith(":"):
            return set()
        return {part.strip() for part in rest[1:].split(",") if part.strip()}

    def with_added_origin(self, origin: str | None) -> AddressRecord:
        """Return a copy of this record with ``origin`` merged into the label."""
        if origin is None:
            return self
        origins = self.origins
        if origin in origins:
            return self
        origins.add(origin)
        new_label = f"{USED_LABEL_PREFIX}:{','.join(sorted(origins))}"
        return AddressRecord(ref=self.ref, label=new_label)

    def to_dict(self) -> dict[str, str]:
        """Serialize to a BIP-329 JSON dict."""
        return {"type": "addr", "ref": self.ref, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict[str, str | bool]) -> AddressRecord | None:
        """Deserialize from a BIP-329 JSON dict.

        Returns ``None`` unless this is a ``type=addr`` record bearing our
        ``jm:used`` label convention; addr records labeled by other tools
        (Sparrow user labels etc.) are not treated as used-address markers
        and are preserved verbatim by ``UTXOMetadataStore``.
        """
        if d.get("type") != "addr":
            return None
        ref = d.get("ref")
        label = d.get("label", USED_LABEL_PREFIX)
        if not isinstance(ref, str) or not ref:
            return None
        if not isinstance(label, str):
            return None
        if not label.startswith(USED_LABEL_PREFIX):
            return None
        return cls(ref=ref, label=label)


@dataclass
class ReservedAddressRecord:
    """A BIP-329 ``addr`` record marking an address the user has set aside.

    The presence of a record means: this deposit address was handed out or
    manually reserved and must not be reissued as the next unused address.
    It carries an optional free-form ``user_label`` (e.g. ``"Alice"``) for
    display. Unlike :class:`AddressRecord` (``jm:used``) it does not imply the
    address has on-chain history, so the wallet still shows it distinctly
    ("reserved") rather than as "used-empty".

    Attributes:
        ref: Bitcoin address.
        user_label: Optional human-readable label; empty string if none.
    """

    ref: str
    user_label: str = ""

    @property
    def label(self) -> str:
        """The BIP-329 label string (``jm:reserved`` or ``jm:reserved:<label>``)."""
        if self.user_label:
            return f"{RESERVED_LABEL_PREFIX}:{self.user_label}"
        return RESERVED_LABEL_PREFIX

    def to_dict(self) -> dict[str, str]:
        """Serialize to a BIP-329 JSON dict."""
        return {"type": "addr", "ref": self.ref, "label": self.label}

    @classmethod
    def from_dict(cls, d: dict[str, str | bool]) -> ReservedAddressRecord | None:
        """Deserialize from a BIP-329 ``addr`` record bearing ``jm:reserved``.

        Returns ``None`` for records that are not ours. Everything after the
        ``jm:reserved:`` prefix is treated as the raw user label (so labels
        may contain colons and commas).
        """
        if d.get("type") != "addr":
            return None
        ref = d.get("ref")
        label = d.get("label", RESERVED_LABEL_PREFIX)
        if not isinstance(ref, str) or not ref:
            return None
        if not isinstance(label, str) or not label.startswith(RESERVED_LABEL_PREFIX):
            return None
        rest = label[len(RESERVED_LABEL_PREFIX) :]
        user_label = rest[1:] if rest.startswith(":") else ""
        return cls(ref=ref, user_label=user_label)


@dataclass
class UTXOMetadataStore:
    """In-memory store for UTXO + address metadata backed by a BIP-329 JSONL file.

    Thread-safety: This class is NOT thread-safe. If concurrent access is
    needed, external synchronization must be applied.

    Attributes:
        path: Path to the JSONL file on disk.
        records: Mapping from outpoint (``txid:vout``) to ``OutputRecord``.
        address_records: Mapping from address to ``AddressRecord`` (only those
            we own with our ``jm:used`` label convention).
        foreign_addr_lines: Verbatim BIP-329 ``addr`` records written by
            other tools (Sparrow user labels, etc.). Preserved on save so we
            do not silently drop interoperable metadata we did not create.
    """

    path: Path
    records: dict[str, OutputRecord] = field(default_factory=dict)
    address_records: dict[str, AddressRecord] = field(default_factory=dict)
    reserved_records: dict[str, ReservedAddressRecord] = field(default_factory=dict)
    foreign_addr_lines: list[dict[str, str | bool]] = field(default_factory=list)

    def load(self) -> None:
        """Load metadata from disk.

        Gracefully handles missing files, empty files, and malformed lines.
        Lines that cannot be parsed are logged and skipped.
        """
        self.records.clear()
        self.address_records.clear()
        self.reserved_records.clear()
        self.foreign_addr_lines.clear()

        if not self.path.exists():
            logger.debug(f"No wallet metadata file at {self.path}")
            return

        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as e:
            logger.error(f"Failed to read wallet metadata: {e}")
            return

        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"Malformed JSON at {self.path}:{line_no}: {e}")
                continue

            record_type = data.get("type") if isinstance(data, dict) else None
            if record_type == "output":
                record = OutputRecord.from_dict(data)
                if record is not None:
                    self.records[record.ref] = record
            elif record_type == "addr":
                # Only our ``jm:used`` labels count as used-address markers.
                # Foreign addr records (Sparrow address-book labels etc.) are
                # preserved verbatim so we round-trip third-party metadata.
                label = data.get("label")
                if isinstance(label, str) and label.startswith(USED_LABEL_PREFIX):
                    rec = AddressRecord.from_dict(data)
                    if rec is not None:
                        self.address_records[rec.ref] = rec
                elif isinstance(label, str) and label.startswith(RESERVED_LABEL_PREFIX):
                    reserved = ReservedAddressRecord.from_dict(data)
                    if reserved is not None:
                        self.reserved_records[reserved.ref] = reserved
                else:
                    if isinstance(data, dict):
                        self.foreign_addr_lines.append(data)
            else:
                # BIP-329 says ignore unknown types -- but preserve them so we
                # do not silently drop interoperable data.
                if isinstance(data, dict):
                    self.foreign_addr_lines.append(data)

        frozen_count = sum(1 for r in self.records.values() if r.is_frozen)
        if self.records or self.address_records:
            # Make it clear these counts come from the on-disk persisted
            # state (BIP-329 metadata), not from the current bitcoind
            # sync. A wallet that has been used in the past can carry a
            # nonzero "previously used" address count even when the
            # current node returns zero history (e.g. transient RPC
            # failure, pruned data, mid-rescan), and that is intentional:
            # the persisted store is monotonic so we never re-propose a
            # deposit address that was historically funded.
            logger.debug(
                f"Loaded persisted wallet metadata from {self.path}: "
                f"{len(self.records)} UTXO record(s) ({frozen_count} frozen), "
                f"{len(self.address_records)} previously used address(es), "
                f"and {len(self.foreign_addr_lines)} foreign record(s). "
                f"These are read from disk and reflect historical state, "
                f"not the result of the current bitcoind scan."
            )

    def save(self) -> None:
        """Persist all records to disk.

        Writes the entire file atomically (write to temp, then rename) to
        prevent corruption on crash. ``output`` records, our ``jm:used``
        ``addr`` records, and any foreign records loaded from disk are all
        serialized in a deterministic order.

        Raises:
            OSError: If the file cannot be written (e.g., read-only filesystem).
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # Filter out output records that carry no useful metadata
        outputs_to_write = [r for r in self.records.values() if r.has_metadata]
        outputs_to_write.sort(key=lambda r: r.ref)

        addr_records_to_write = sorted(self.address_records.values(), key=lambda r: r.ref)
        reserved_records_to_write = sorted(self.reserved_records.values(), key=lambda r: r.ref)

        if (
            not outputs_to_write
            and not addr_records_to_write
            and not reserved_records_to_write
            and not self.foreign_addr_lines
        ):
            if self.path.exists():
                try:
                    self.path.unlink()
                    logger.debug("Removed empty wallet metadata file")
                except OSError as e:
                    logger.warning(f"Failed to remove empty metadata file: {e}")
                    raise
            return

        lines: list[str] = []
        lines.extend(json.dumps(r.to_dict(), separators=(",", ":")) for r in outputs_to_write)
        lines.extend(json.dumps(r.to_dict(), separators=(",", ":")) for r in addr_records_to_write)
        lines.extend(
            json.dumps(r.to_dict(), separators=(",", ":")) for r in reserved_records_to_write
        )
        # Foreign records last; sort by (type, ref) for determinism.
        for foreign in sorted(
            self.foreign_addr_lines,
            key=lambda d: (str(d.get("type", "")), str(d.get("ref", ""))),
        ):
            lines.append(json.dumps(foreign, separators=(",", ":")))

        tmp_path = self.path.with_suffix(".tmp")
        try:
            tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            tmp_path.replace(self.path)
        except OSError as e:
            logger.error(f"Failed to save wallet metadata: {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def is_frozen(self, outpoint: str) -> bool:
        """Check if an outpoint is frozen.

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.

        Returns:
            True if the UTXO is frozen (spendable is False).
        """
        record = self.records.get(outpoint)
        return record is not None and record.is_frozen

    def get_frozen_outpoints(self) -> set[str]:
        """Get all frozen outpoints.

        Returns:
            Set of outpoint strings that are frozen.
        """
        return {ref for ref, record in self.records.items() if record.is_frozen}

    def has_record(self, outpoint: str) -> bool:
        """Whether any metadata record exists for ``outpoint``.

        Used by the forced-address-reuse auto-freeze to skip UTXOs the wallet
        already tracks (frozen, labeled, locked, or previously auto-evaluated),
        so a user's explicit unfreeze of a reuse UTXO is never overridden.
        """
        return outpoint in self.records

    def freeze(self, outpoint: str, label: str | None = None) -> None:
        """Freeze a UTXO (set spendable to False) and persist.

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.
            label: Optional label to attach (only set when the record has no
                label yet), e.g. to mark an automatic forced-reuse freeze.
        """
        if outpoint in self.records:
            self.records[outpoint].spendable = False
            if label is not None and self.records[outpoint].label is None:
                self.records[outpoint].label = label
        else:
            self.records[outpoint] = OutputRecord(ref=outpoint, spendable=False, label=label)
        self.save()
        logger.info(f"Frozen UTXO: {outpoint}")

    def unfreeze(self, outpoint: str) -> None:
        """Unfreeze a UTXO (set spendable to True) and persist.

        If the record has no other metadata (no label), it is removed
        entirely since ``spendable=True`` is the default.

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.
        """
        record = self.records.get(outpoint)
        if record is None:
            # Already unfrozen (no record means spendable)
            return

        if record.label is not None or record.lock_until is not None:
            # Keep the record for the label / active CoinJoin lock.
            record.spendable = True
        else:
            # No other metadata -- remove entirely
            del self.records[outpoint]

        self.save()
        logger.info(f"Unfrozen UTXO: {outpoint}")

    def toggle_freeze(self, outpoint: str) -> bool:
        """Toggle the frozen state of a UTXO and persist.

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.

        Returns:
            True if the UTXO is now frozen, False if now unfrozen.
        """
        if self.is_frozen(outpoint):
            self.unfreeze(outpoint)
            return False
        else:
            self.freeze(outpoint)
            return True

    # -- Temporary CoinJoin locks --------------------------------------------
    #
    # A lock is a *time-limited* reservation on an input that is committed to an
    # in-flight CoinJoin. It is persisted in the same JSONL file (via
    # ``jm_lock_until``) so that other processes -- another taker round, or a
    # maker serving a different taker -- re-read it right before coin selection
    # and never pick the same UTXO. Picking the same input twice produces
    # conflicting, mutually double-spending transactions; the one broadcast
    # second is rejected ("insufficient fee, rejecting replacement"). Locks
    # auto-expire (``lock_until``) so a crashed or killed round cannot block
    # funds forever, and acquisition is serialized across processes with an
    # advisory file lock so two processes cannot both win the same UTXO.

    @property
    def _flock_path(self) -> Path:
        return self.path.with_suffix(".lock")

    @contextmanager
    def _exclusive_file_lock(self) -> Iterator[None]:
        """Serialize lock acquisition/release across processes.

        Uses a POSIX advisory lock on a sidecar ``.lock`` file. The metadata
        file itself is replaced via rename on every save, which would break a
        lock held on its inode, so we lock a stable sidecar path instead. On
        platforms without ``fcntl`` this degrades to a no-op (atomic rename
        still prevents corruption; only the rare lost-update race remains).
        """
        if fcntl is None:  # pragma: no cover - non-POSIX platforms
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._flock_path, "w", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _prune_expired_locks(self, now: float) -> None:
        """Clear expired ``lock_until`` markers and drop now-empty records."""
        for ref in list(self.records.keys()):
            record = self.records[ref]
            if record.lock_until is not None and record.lock_until <= now:
                record.lock_until = None
                if not record.has_metadata:
                    del self.records[ref]

    def get_locked_outpoints(self, now: float | None = None) -> set[str]:
        """Return outpoints currently holding a non-expired CoinJoin lock.

        Note: reads in-memory state. Call :meth:`load` first to observe locks
        written by other processes.
        """
        if now is None:
            now = time.time()
        return {ref for ref, record in self.records.items() if record.is_locked(now)}

    def try_lock_outpoints(
        self, outpoints: Iterable[str], ttl: float = DEFAULT_COINJOIN_LOCK_TTL
    ) -> bool:
        """Atomically lock ``outpoints`` for ``ttl`` seconds.

        Reloads on-disk state under an exclusive file lock so concurrent
        processes cannot both acquire the same UTXO. Fails (returning False,
        locking nothing) if any requested outpoint is frozen or already locked
        by another in-flight round.

        Returns:
            True if all outpoints were locked; False on conflict.
        """
        wanted = set(outpoints)
        if not wanted:
            return True
        with self._exclusive_file_lock():
            self.load()
            now = time.time()
            self._prune_expired_locks(now)
            for ref in wanted:
                record = self.records.get(ref)
                if record is not None and (record.is_frozen or record.is_locked(now)):
                    return False
            for ref in wanted:
                record = self.records.get(ref)
                if record is None:
                    record = OutputRecord(ref=ref)
                    self.records[ref] = record
                record.lock_until = now + ttl
            self.save()
        return True

    def release_outpoints(self, outpoints: Iterable[str]) -> None:
        """Clear CoinJoin locks on ``outpoints`` (no-op for unlocked ones)."""
        wanted = set(outpoints)
        if not wanted:
            return
        with self._exclusive_file_lock():
            self.load()
            changed = False
            for ref in wanted:
                record = self.records.get(ref)
                if record is not None and record.lock_until is not None:
                    record.lock_until = None
                    changed = True
                    if not record.has_metadata:
                        del self.records[ref]
            self._prune_expired_locks(time.time())
            if changed:
                self.save()

    def set_label(self, outpoint: str, label: str | None) -> None:
        """Set or clear the label for a UTXO and persist.

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.
            label: Label string, or None to clear.
        """
        if outpoint in self.records:
            self.records[outpoint].label = label
        elif label is not None:
            self.records[outpoint] = OutputRecord(ref=outpoint, label=label)
        else:
            return  # Nothing to do

        # Clean up record if it has no useful metadata
        record = self.records.get(outpoint)
        if record and record.spendable is None and record.label is None:
            del self.records[outpoint]

        self.save()

    def get_label(self, outpoint: str) -> str | None:
        """Get the label for an outpoint.

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.

        Returns:
            Label string, or None if no label set.
        """
        record = self.records.get(outpoint)
        return record.label if record else None

    # -- Address history (BIP-329 ``addr`` records with ``jm:used`` label) --

    def mark_address_used(self, address: str, origin: str | None = None) -> bool:
        """Record an address as having on-chain history.

        Idempotent. If the address is already recorded, only the origin label
        is augmented (best-effort context); the file is rewritten only when
        the record actually changes. Returns ``True`` if disk state changed.
        """
        if not address:
            return False
        existing = self.address_records.get(address)
        if existing is None:
            self.address_records[address] = AddressRecord(
                ref=address,
                label=f"{USED_LABEL_PREFIX}:{origin}" if origin else USED_LABEL_PREFIX,
            )
            self.save()
            return True
        updated = existing.with_added_origin(origin)
        if updated.label == existing.label:
            return False
        self.address_records[address] = updated
        self.save()
        return True

    def mark_addresses_used(
        self,
        addresses: Iterable[str],
        origin: str | None = None,
    ) -> int:
        """Batched variant of :meth:`mark_address_used`.

        Performs a single ``save()`` for many addresses; returns the count of
        records that were created or had their origin extended.
        """
        changed = 0
        for address in addresses:
            if not address:
                continue
            existing = self.address_records.get(address)
            if existing is None:
                self.address_records[address] = AddressRecord(
                    ref=address,
                    label=f"{USED_LABEL_PREFIX}:{origin}" if origin else USED_LABEL_PREFIX,
                )
                changed += 1
                continue
            updated = existing.with_added_origin(origin)
            if updated.label != existing.label:
                self.address_records[address] = updated
                changed += 1
        if changed:
            self.save()
        return changed

    def is_address_used(self, address: str) -> bool:
        """Return True if ``address`` has been recorded as having history."""
        return address in self.address_records

    def get_used_addresses(self) -> set[str]:
        """Return the set of addresses with on-chain history.

        This is the privacy-critical "do not reissue" set, surviving across
        process restarts and backend swaps.
        """
        return set(self.address_records.keys())

    def get_address_origins(self, address: str) -> set[str]:
        """Return the origin tags recorded for ``address`` (empty if none)."""
        record = self.address_records.get(address)
        return record.origins if record else set()

    def get_coinjoin_address_types(self) -> dict[str, str]:
        """Map addresses to a CoinJoin display type from persisted origins.

        Import-time label reconstruction (see
        ``WalletService.reconstruct_imported_labels``) tags addresses with
        ``cj_out`` / ``cj_change`` origins derived from on-chain analysis of
        their creating transaction. This returns those addresses using the
        vocabulary the wallet display expects (``cj_out`` and ``change``,
        matching ``get_address_history_types``), so imported wallets surface
        ``cj-out`` / ``cj-change`` instead of falling back to ``deposit`` /
        ``non-cj-change``.
        """
        result: dict[str, str] = {}
        for address, record in self.address_records.items():
            origins = record.origins
            if "cj_out" in origins:
                result[address] = "cj_out"
            elif "cj_change" in origins:
                result[address] = "change"
        return result

    # -- Reserved addresses (BIP-329 ``addr`` records with ``jm:reserved``) --

    def reserve_address(self, address: str, label: str = "") -> bool:
        """Mark ``address`` as reserved (set aside) with an optional label.

        Idempotent: re-reserving with the same label is a no-op. Changing the
        label updates the record. Returns ``True`` if disk state changed.
        """
        if not address:
            return False
        user_label = label or ""
        existing = self.reserved_records.get(address)
        if existing is not None and existing.user_label == user_label:
            return False
        self.reserved_records[address] = ReservedAddressRecord(ref=address, user_label=user_label)
        self.save()
        return True

    def unreserve_address(self, address: str) -> bool:
        """Remove any reservation for ``address``. Returns ``True`` if changed."""
        if address in self.reserved_records:
            del self.reserved_records[address]
            self.save()
            return True
        return False

    def is_address_reserved(self, address: str) -> bool:
        """Return True if ``address`` is currently reserved."""
        return address in self.reserved_records

    def get_reserved_addresses(self) -> set[str]:
        """Return the set of reserved addresses."""
        return set(self.reserved_records.keys())

    def get_reserved_labels(self) -> dict[str, str]:
        """Return a mapping of reserved address -> user label (may be empty)."""
        return {ref: rec.user_label for ref, rec in self.reserved_records.items()}

    def verify_writable(self) -> None:
        """Verify that the metadata file's directory is writable.

        Attempts to create and immediately remove a temporary file in the
        same directory as the metadata file. This catches read-only mounts
        and permission issues early, before a real save attempt.

        Raises:
            OSError: If the directory is not writable.
        """
        parent = self.path.parent
        parent.mkdir(parents=True, exist_ok=True)
        # Try creating a temp file in the target directory
        try:
            fd = tempfile.NamedTemporaryFile(dir=parent, prefix=".jm_write_test_", delete=True)
            fd.close()
        except OSError as e:
            raise OSError(
                f"Data directory is not writable: {parent}. "
                f"Cannot persist UTXO metadata (frozen state, labels). "
                f"Check mount permissions. Original error: {e}"
            ) from e


def load_metadata_store(
    data_dir: Path,
    fingerprint: str | None = None,
    owned_addresses: Iterable[str] | None = None,
) -> UTXOMetadataStore:
    """Create and load a UTXOMetadataStore from the wallet's metadata file.

    Args:
        data_dir: JoinMarket data directory (e.g., ``~/.joinmarket-ng``).
        fingerprint: Optional 8-char hex wallet fingerprint. When provided,
            the per-wallet path ``wallet_metadata_<fp>.jsonl`` is used and a
            one-shot migration from the legacy shared
            ``wallet_metadata.jsonl`` is attempted on first open.
        owned_addresses: Optional iterable of addresses this wallet derives
            inside its scan range. Used to filter ``addr`` records during
            migration so we do not import another wallet's used-address set
            from the shared file. When ``None`` no ``addr`` records are
            imported (safer default than "all"; the wallet's own sync will
            re-populate any genuinely-funded addresses).

    Returns:
        Loaded UTXOMetadataStore instance.
    """
    from jmcore.paths import get_wallet_metadata_path

    path = get_wallet_metadata_path(data_dir, fingerprint=fingerprint)

    if fingerprint is not None and not path.exists():
        legacy_shared = get_wallet_metadata_path(data_dir, fingerprint=None)
        if legacy_shared.exists() and legacy_shared != path:
            _migrate_shared_metadata(
                legacy_shared,
                path,
                owned_addresses=owned_addresses,
            )

    store = UTXOMetadataStore(path=path)
    store.load()
    return store


def _migrate_shared_metadata(
    shared_path: Path,
    per_wallet_path: Path,
    owned_addresses: Iterable[str] | None,
) -> None:
    """Copy wallet-specific records out of the legacy shared metadata file.

    Pre-partition builds wrote a single ``wallet_metadata.jsonl`` per data
    directory; any wallet opened against that directory would inherit
    every other wallet's used-address and frozen-UTXO state. This
    migration runs once, when the per-wallet file does not yet exist:

    - ``addr`` records are filtered by ``owned_addresses``. Records whose
      ``ref`` address is not derivable by this wallet are skipped so the
      "previously used" set stays wallet-private.
    - ``output`` records (``ref="txid:vout"``) carry no wallet linkage in
      BIP-329, so we copy them all. Records that belong to other wallets
      remain inert here because they will never match a UTXO this wallet
      sees during sync. The slight disk overhead is preferable to losing
      this wallet's frozen-state and labels.
    - Foreign records (BIP-329 types we do not own) are copied verbatim.
    - The shared file is left in place so other wallets opened later in
      the same data dir can run their own migration.

    Best-effort; logs and returns on failure rather than blocking startup.
    """
    owned: set[str] | None = None
    if owned_addresses is not None:
        owned = {addr for addr in owned_addresses if addr}

    try:
        text = shared_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            f"Could not read legacy shared metadata {shared_path} for "
            f"migration into {per_wallet_path.name}: {exc}"
        )
        return

    kept_lines: list[str] = []
    addr_kept = 0
    addr_skipped = 0
    output_kept = 0
    foreign_kept = 0

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            # Preserve unparseable lines verbatim; the loader handles
            # them as foreign content too.
            kept_lines.append(raw)
            continue
        if not isinstance(obj, dict):
            kept_lines.append(raw)
            continue
        rec_type = obj.get("type")
        if rec_type == "addr":
            ref = obj.get("ref")
            if owned is None:
                # Caller provided no ownership info: skip to be safe.
                addr_skipped += 1
                continue
            if isinstance(ref, str) and ref in owned:
                kept_lines.append(raw)
                addr_kept += 1
            else:
                addr_skipped += 1
        elif rec_type == "output":
            kept_lines.append(raw)
            output_kept += 1
        else:
            kept_lines.append(raw)
            foreign_kept += 1

    if not kept_lines:
        logger.debug(
            f"Per-wallet metadata migration from {shared_path.name} into "
            f"{per_wallet_path.name}: no records matched this wallet "
            f"({addr_skipped} addr record(s) skipped as belonging to "
            f"other wallets); creating empty store."
        )
        return

    try:
        per_wallet_path.parent.mkdir(parents=True, exist_ok=True)
        per_wallet_path.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(f"Could not write migrated metadata to {per_wallet_path}: {exc}")
        return

    logger.info(
        f"Migrated {addr_kept} addr + {output_kept} output + "
        f"{foreign_kept} other record(s) from shared "
        f"{shared_path.name} into per-wallet {per_wallet_path.name} "
        f"(skipped {addr_skipped} addr record(s) belonging to other "
        f"wallets). The shared file is preserved so other wallets opened "
        f"in this data directory can run the same migration."
    )
