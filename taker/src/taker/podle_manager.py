"""
Manager for PoDLE commitments (used for retry tracking).
"""

from __future__ import annotations

import asyncio
import bisect
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jmcore.commitment_blacklist import get_blacklist
from jmcore.external_podle import ExternalPoDLE
from jmcore.market_store import MarketStore
from jmcore.paths import get_default_data_dir, get_market_store_path, get_used_commitments_path
from jmcore.podle import PoDLECommitment, generate_podle
from jmcore.secure_files import exclusive_file_lock
from loguru import logger

from taker.podle import ExtendedPoDLECommitment, get_eligible_podle_utxos

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from jmwallet.wallet.models import UTXOInfo


_MAX_EXTERNAL_CANDIDATES = 32


class ExternalPoDLEPoolError(Exception):
    """External PoDLE storage could not be safely read or updated."""


class PoDLEManager:
    """Manages tracking of used PoDLE commitments."""

    def __init__(self, data_dir: Path | None = None, *, wallet_id: str | None = None):
        effective_data_dir = get_default_data_dir() if data_dir is None else data_dir
        self.filepath = get_used_commitments_path(effective_data_dir)
        self._market_store_path = get_market_store_path(effective_data_dir)
        self._market_store_intent_path = self._market_store_path.with_name(
            f"{self._market_store_path.name}.wallet-ledger"
        )
        self._wallet_id = wallet_id
        self._market_store: MarketStore | None = None
        self._market_store_identity: tuple[int, int] | None = None
        self._native_ledger_seen = False
        self._native_ledger_refused = False
        self._closed = False
        self.used_commitments: set[str] = set()
        self.external_commitments: Any = {}
        self.external_v1: dict[str, ExternalPoDLE] = {}
        self.external_v1_cursor: str | None = None
        self._storage_healthy = True
        self._load()

    def close(self) -> None:
        """Release the cached market store and prevent further manager use."""
        if self._closed:
            return
        self._closed = True
        self._close_market_store()

    def _close_market_store(self) -> None:
        """Close and discard an internal store handle."""
        if self._market_store is not None:
            self._market_store.close()
            self._market_store = None
        self._market_store_identity = None

    def _market_store_file_identity(self) -> tuple[int, int]:
        """Return the existing database inode, rejecting path replacement and symlinks."""
        if self._market_store_path.is_symlink():
            raise ExternalPoDLEPoolError("Market store path is invalid")
        try:
            status = self._market_store_path.stat()
        except OSError as exc:
            raise ExternalPoDLEPoolError("Could not inspect market store") from exc
        return status.st_dev, status.st_ino

    def _native_store_locked(self) -> MarketStore | None:
        """Return the active wallet ledger while the commitments sidecar lock is held.

        A legacy v1 market store is deliberately not used for local claims. Once a
        v2 ledger was observed, removing either durable ledger artifact is unsafe
        even if this process still has a usable SQLite connection.
        """
        if self._closed:
            raise ExternalPoDLEPoolError("PoDLE manager is closed")
        if self._native_ledger_refused:
            raise ExternalPoDLEPoolError(
                "Wallet ledger was previously replaced or became unavailable"
            )

        database_exists = os.path.lexists(self._market_store_path)
        intent_exists = os.path.lexists(self._market_store_intent_path)
        if self._native_ledger_seen and (not database_exists or not intent_exists):
            self._native_ledger_refused = True
            raise ExternalPoDLEPoolError("Wallet ledger artifacts are missing")
        if not database_exists and not intent_exists:
            self._close_market_store()
            return None

        if database_exists:
            current_identity = self._market_store_file_identity()
            if (
                self._market_store is not None
                and self._market_store_identity is not None
                and self._market_store_identity != current_identity
            ):
                if self._native_ledger_seen:
                    self._native_ledger_refused = True
                    raise ExternalPoDLEPoolError("Wallet ledger database was replaced")
                self._close_market_store()
        else:
            current_identity = None

        if self._market_store is None:
            try:
                store = MarketStore(self._market_store_path, wallet_id=self._wallet_id)
                self._market_store = store
                self._market_store_identity = current_identity
                is_wallet_ledger = store.is_wallet_ledger
            except Exception as exc:
                self._close_market_store()
                raise ExternalPoDLEPoolError("Could not open wallet ledger") from exc
            if not is_wallet_ledger:
                if self._native_ledger_seen:
                    self._native_ledger_refused = True
                    raise ExternalPoDLEPoolError("Wallet ledger was replaced or damaged")
                return None
            self._native_ledger_seen = True
        elif not self._native_ledger_seen:
            try:
                if not self._market_store.is_wallet_ledger:
                    return None
            except Exception as exc:
                self._close_market_store()
                raise ExternalPoDLEPoolError("Could not validate wallet ledger") from exc
            self._native_ledger_seen = True

        store = self._market_store
        if store is None:  # pragma: no cover - protected by the branches above
            raise ExternalPoDLEPoolError("Wallet ledger is unavailable")
        try:
            store.check_commitments_path(self.filepath)
        except Exception as exc:
            if self._native_ledger_seen:
                self._native_ledger_refused = True
            raise ExternalPoDLEPoolError("Wallet ledger does not match commitments state") from exc
        return store

    @staticmethod
    def _store_local_available(store: MarketStore, commitment: str) -> bool:
        """Translate ledger read failures into the manager's fail-closed error."""
        try:
            return store.is_available_for_local(commitment)
        except Exception as exc:
            raise ExternalPoDLEPoolError("Could not check wallet ledger ownership") from exc

    @staticmethod
    def _store_hold_local(store: MarketStore, commitment: str) -> bool:
        """Translate ledger hold failures into the manager's fail-closed error."""
        try:
            return store.hold_local(commitment)
        except Exception as exc:
            raise ExternalPoDLEPoolError("Could not hold wallet ledger commitment") from exc

    @staticmethod
    def _store_claim_local(store: MarketStore, commitment: str) -> bool:
        """Translate ledger claim failures into the manager's fail-closed error."""
        try:
            return store.claim_local(commitment)
        except Exception as exc:
            raise ExternalPoDLEPoolError("Could not claim wallet ledger commitment") from exc

    def _load(self) -> None:
        """Load commitment state without replacing a caller's unsaved used set."""
        self._reload(merge_used=True)

    def _reload(self, *, merge_used: bool) -> None:
        """Parse persisted state and fail closed on malformed external records."""
        if not self.filepath.exists():
            return
        try:
            with open(self.filepath, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("commitment state must be a JSON object")
            used = data.get("used", [])
            external = data.get("external", {})
            external_v1 = data.get("external_v1", {})
            external_v1_cursor = data.get("external_v1_cursor")
            if not isinstance(used, list) or not all(isinstance(item, str) for item in used):
                raise ValueError("used commitments must be a string list")
            if not isinstance(external_v1, dict):
                raise ValueError("external_v1 commitment mapping must be an object")
            if external_v1_cursor is not None and not isinstance(external_v1_cursor, str):
                raise ValueError("external_v1 cursor must be a commitment string or null")
            parsed_external: dict[str, ExternalPoDLE] = {}
            for commitment, record_data in external_v1.items():
                if not isinstance(commitment, str) or not isinstance(record_data, dict):
                    raise ValueError("external_v1 contains an invalid record")
                record = ExternalPoDLE.model_validate(record_data)
                if record.commitment != commitment:
                    raise ValueError("external_v1 commitment key does not match its record")
                parsed_external[commitment] = record

            previous_used = self.used_commitments if merge_used else set()
            self.used_commitments = set(used) | previous_used
            self.external_commitments = external
            self.external_v1 = parsed_external
            self.external_v1_cursor = external_v1_cursor
            self._storage_healthy = True
            logger.debug(f"Loaded {len(self.used_commitments)} used PoDLE commitments")
        except Exception:
            self._storage_healthy = False
            logger.error("Failed to load PoDLE commitment state; external pool disabled")

    @contextmanager
    def _locked_file(self) -> Iterator[None]:
        """Serialize access to the commitments sidecar without reading its JSON."""
        if self._closed:
            raise ExternalPoDLEPoolError("PoDLE manager is closed")
        lock_path = self.filepath.with_name(f"{self.filepath.name}.lock")
        try:
            with exclusive_file_lock(lock_path):
                yield
        except OSError as exc:
            self._storage_healthy = False
            raise ExternalPoDLEPoolError("Could not open external PoDLE pool lock") from exc

    @contextmanager
    def _locked_state(self) -> Iterator[None]:
        """Serialize state mutation across processes and reload while locked."""
        with self._locked_file():
            self._reload(merge_used=True)
            if not self._storage_healthy:
                raise ExternalPoDLEPoolError("External PoDLE pool is corrupt")
            yield

    def _save_locked(self) -> None:
        """Durably replace private state while the sidecar lock is held."""
        data = {
            "used": sorted(self.used_commitments),
            "external": self.external_commitments,
            "external_v1": {
                commitment: record.model_dump(mode="json")
                for commitment, record in self.external_v1.items()
            },
            "external_v1_cursor": self.external_v1_cursor,
        }
        encoded = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
        fd = -1
        temp_path: str | None = None
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{self.filepath.name}.", dir=self.filepath.parent
            )
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=True) as f:
                fd = -1
                f.write(encoded)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.filepath)
            if os.name != "nt":
                directory_fd = os.open(self.filepath.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            self._storage_healthy = False
            raise ExternalPoDLEPoolError("Could not durably save external PoDLE pool") from exc
        finally:
            if fd != -1:
                os.close(fd)
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass

    def _save(self) -> None:
        """Persist legacy/local commitment tracking through the durable store."""
        try:
            with self._locked_state():
                self._save_locked()
        except ExternalPoDLEPoolError:
            logger.error("Failed to save PoDLE commitment state")

    def _can_continue_legacy_after_lock_failure(self) -> bool:
        """Recognize the legacy cases where local issuance historically continued."""
        return (
            not self._closed
            and not self._native_ledger_seen
            and not self._native_ledger_refused
            and not os.path.lexists(self._market_store_intent_path)
            and (not os.path.lexists(self._market_store_path) or self._market_store is not None)
        )

    def _save_legacy_local_claim_locked(self, commitment: str) -> bool:
        """Keep legacy local claim and save suppression behavior under the file lock."""
        self.used_commitments.add(commitment)
        self._reload(merge_used=True)
        if not self._storage_healthy:
            logger.error("Failed to save PoDLE commitment state")
            return True
        try:
            self._save_locked()
        except ExternalPoDLEPoolError:
            logger.error("Failed to save PoDLE commitment state")
        return True

    def _claim_legacy_local_without_lock(self, commitment: str) -> bool:
        """Match the historic local fallback when the sidecar lock itself fails."""
        if commitment in self.used_commitments:
            return False
        self.used_commitments.add(commitment)
        self._save()
        return True

    def _is_available_for_local(self, commitment: str) -> bool:
        """Check JSON and, when enabled, global ledger ownership without claiming."""
        previous_health = self._storage_healthy
        try:
            with self._locked_file():
                store = self._native_store_locked()
                if store is None:
                    return commitment not in self.used_commitments
                self._reload(merge_used=True)
                if not self._storage_healthy:
                    raise ExternalPoDLEPoolError("External PoDLE pool is corrupt")
                return commitment not in self.used_commitments and self._store_local_available(
                    store, commitment
                )
        except ExternalPoDLEPoolError:
            if self._can_continue_legacy_after_lock_failure():
                self._storage_healthy = previous_health
                return commitment not in self.used_commitments
            raise

    def _local_selection_exclusions(self) -> set[str]:
        """Read the shared projection once per selection pass, without holding a lock at yield."""
        previous_health = self._storage_healthy
        try:
            with self._locked_file():
                store = self._native_store_locked()
                if store is None:
                    return set(self.used_commitments)
                self._reload(merge_used=True)
                if not self._storage_healthy:
                    raise ExternalPoDLEPoolError("External PoDLE pool is corrupt")
                try:
                    return self.used_commitments | store.unavailable_local_commitments()
                except Exception as exc:
                    raise ExternalPoDLEPoolError(
                        "Could not snapshot wallet ledger ownership"
                    ) from exc
        except ExternalPoDLEPoolError:
            if self._can_continue_legacy_after_lock_failure():
                self._storage_healthy = previous_health
                return set(self.used_commitments)
            raise

    def _claim_local_commitment(self, commitment: str) -> bool:
        """Durably consume one local commitment before exposing its proof.

        The legacy JSON-only path intentionally retains its historical behavior:
        an unsuccessful local save is logged but does not revoke the in-memory
        claim. A native ledger claim is irreversible, so its JSON projection must
        succeed before this method reports success.
        """
        previous_health = self._storage_healthy
        try:
            with self._locked_file():
                store = self._native_store_locked()
                if store is None:
                    if commitment in self.used_commitments:
                        return False
                    return self._save_legacy_local_claim_locked(commitment)
                self._reload(merge_used=True)
                if not self._storage_healthy:
                    raise ExternalPoDLEPoolError("External PoDLE pool is corrupt")
                if commitment in self.used_commitments:
                    return False
                if not self._store_claim_local(store, commitment):
                    return False
                self.used_commitments.add(commitment)
                self._save_locked()
                return True
        except ExternalPoDLEPoolError:
            if self._can_continue_legacy_after_lock_failure():
                self._storage_healthy = previous_health
                return self._claim_legacy_local_without_lock(commitment)
            raise

    def import_external(self, record: ExternalPoDLE) -> bool:
        """Atomically add a strict external credential, returning whether it was new."""
        if not isinstance(record, ExternalPoDLE):
            raise TypeError("record must be an ExternalPoDLE")
        with self._locked_state():
            store = self._native_store_locked()
            commitment = record.commitment
            if commitment in self.used_commitments:
                return False
            existing = self.external_v1.get(commitment)
            if existing is not None:
                return False
            if store is not None and not self._store_hold_local(store, commitment):
                # A failed JSON projection can leave the same local hold behind.
                # Only an unconsumed hold owned by this wallet (or legacy NULL
                # ownership) may be retried; market and used rows stay blocked.
                if not self._store_local_available(store, commitment):
                    return False
            self.external_v1[commitment] = record
            self._save_locked()
            return True

    def external_count(self) -> int:
        """Return the number of unconsumed strict external credentials."""
        try:
            with self._locked_state():
                store = self._native_store_locked()
                return sum(
                    commitment not in self.used_commitments
                    and (store is None or self._store_local_available(store, commitment))
                    for commitment in self.external_v1
                )
        except ExternalPoDLEPoolError:
            return 0

    def _external_candidates(self, network: str, max_retries: int) -> list[ExternalPoDLE]:
        """Snapshot a bounded, rotating candidate window before backend I/O."""
        if max_retries < 1:
            return []
        with self._locked_state():
            store = self._native_store_locked()
            commitments = sorted(
                commitment
                for commitment, record in self.external_v1.items()
                if commitment not in self.used_commitments
                and (store is None or self._store_local_available(store, commitment))
                and record.network == network
                and record.index < max_retries
            )
            if not commitments:
                return []

            start = 0
            if self.external_v1_cursor is not None:
                start = bisect.bisect_right(commitments, self.external_v1_cursor)
                if start == len(commitments):
                    start = 0
            selected = commitments[start : start + _MAX_EXTERNAL_CANDIDATES]
            if len(selected) < _MAX_EXTERNAL_CANDIDATES:
                selected.extend(commitments[: _MAX_EXTERNAL_CANDIDATES - len(selected)])

            self.external_v1_cursor = selected[-1]
            self._save_locked()
            return [self.external_v1[commitment] for commitment in selected]

    @staticmethod
    async def _external_record_is_usable(
        backend: BlockchainBackend,
        record: ExternalPoDLE,
        cj_amount: int,
        min_confirmations: int,
        min_percent: int,
        blacklist: Any,
    ) -> bool:
        """Use only authoritative chain data to validate an imported credential."""
        if blacklist.is_blacklisted(record.commitment):
            return False
        try:
            if backend.requires_neutrino_metadata():
                result = await backend.verify_utxo_with_metadata(
                    txid=record.outpoint.txid,
                    vout=record.outpoint.vout,
                    scriptpubkey=record.scriptpubkey,
                    blockheight=record.blockheight,
                )
                if not result.valid or not result.scriptpubkey_matches:
                    return False
                value = result.value
                confirmations = result.confirmations
            else:
                utxo = await backend.get_utxo(record.outpoint.txid, record.outpoint.vout)
                if utxo is None or utxo.scriptpubkey.lower() != record.scriptpubkey:
                    return False
                # listunspent does not expose the block height for wallet-owned
                # outputs. Its exact-outpoint response remains authoritative for
                # unspentness, script, value, and confirmations.
                if utxo.height is not None and utxo.height != record.blockheight:
                    return False
                value = utxo.value
                confirmations = utxo.confirmations
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

        minimum_value = cj_amount * min_percent // 100
        return (
            type(value) is int
            and type(confirmations) is int
            and value >= minimum_value
            and confirmations >= min_confirmations
        )

    async def consume_external(
        self,
        backend: BlockchainBackend,
        network: str,
        cj_amount: int,
        min_confirmations: int,
        min_percent: int,
        max_retries: int,
    ) -> ExtendedPoDLECommitment | None:
        """Validate then atomically claim one external credential, or fail closed."""
        if (
            type(cj_amount) is not int
            or type(min_confirmations) is not int
            or type(min_percent) is not int
            or type(max_retries) is not int
            or cj_amount < 0
            or min_confirmations < 1
            or not 1 <= min_percent <= 100
        ):
            return None
        try:
            blacklist = get_blacklist()
            candidates = self._external_candidates(network, max_retries)
        except Exception:
            logger.error("Cannot safely read external PoDLE pool or blacklist")
            return None

        for record in candidates:
            try:
                usable = await self._external_record_is_usable(
                    backend,
                    record,
                    cj_amount,
                    min_confirmations,
                    min_percent,
                    blacklist,
                )
            except Exception:
                return None
            if not usable:
                continue
            try:
                with self._locked_state():
                    current = self.external_v1.get(record.commitment)
                    if current != record or record.commitment in self.used_commitments:
                        continue
                    if blacklist.is_blacklisted(record.commitment):
                        continue
                    store = self._native_store_locked()
                    if store is not None and not self._store_claim_local(store, record.commitment):
                        continue
                    self.used_commitments.add(record.commitment)
                    del self.external_v1[record.commitment]
                    self._save_locked()
            except Exception:
                return None
            return ExtendedPoDLECommitment(
                commitment=record.to_podle_commitment(),
                scriptpubkey=record.scriptpubkey,
                blockheight=record.blockheight,
            )
        return None

    def get_utxo_retry_count(self, utxo_str: str, private_key: bytes, max_retries: int) -> int:
        """
        Get the number of times a UTXO has been used for PoDLE commitments.

        Checks indices 0..(max_retries-1) in reverse order and returns the highest
        index + 1 where a commitment is found in used_commitments.

        Note: Only used in tests. Production code uses lazy evaluation in
        generate_fresh_commitment() to avoid generating all commitments upfront.

        Returns:
            0 if UTXO is fresh (no used commitments)
            1-max_retries if UTXO has been used that many times
        """
        # Early termination: stop at first match (reverse order)
        for i in reversed(range(max_retries)):
            try:
                podle = generate_podle(private_key, utxo_str, i)
                commitment_hex = podle.commitment.hex()
                if not self._is_available_for_local(commitment_hex):
                    return i + 1  # Found highest used index
            except ExternalPoDLEPoolError:
                return i + 1
            except Exception:
                continue
        return 0  # No used commitments found

    def generate_fresh_commitment(
        self,
        wallet_utxos: list[UTXOInfo],
        cj_amount: int,
        private_key_getter: Callable[[str], bytes | None],
        min_confirmations: int = 5,
        min_percent: int = 20,
        max_retries: int = 3,
    ) -> ExtendedPoDLECommitment | None:
        """
        Generate a fresh PoDLE commitment for a CoinJoin.

        Iterates through eligible UTXOs and tries indices 0..max_retries-1 until
        finding an unused commitment. UTXOs are pre-sorted by confirmations and value,
        so fresh UTXOs (which succeed at index 0) are naturally preferred.

        Args:
            wallet_utxos: Available wallet UTXOs
            cj_amount: CoinJoin amount
            private_key_getter: Function to get private key for address
            min_confirmations: Minimum UTXO confirmations required
            min_percent: Minimum UTXO value as % of cj_amount
            max_retries: Maximum number of retries per UTXO (default: 3)

        Returns:
            ExtendedPoDLECommitment or None if no fresh commitment available
        """
        candidates = self._iter_fresh_commitments(
            wallet_utxos,
            cj_amount,
            private_key_getter,
            min_confirmations,
            min_percent,
            max_retries,
        )
        for utxo, podle in candidates:
            commitment_hex = podle.commitment.hex()
            try:
                if not self._claim_local_commitment(commitment_hex):
                    continue
            except ExternalPoDLEPoolError:
                logger.error("Could not safely claim fresh PoDLE commitment")
                return None

            logger.info("Generated fresh PoDLE commitment")
            logger.bind(sensitive=True).info(
                "Generated fresh PoDLE for {} using index {} (utxo value={}, confs={})",
                podle.utxo,
                podle.index,
                utxo.value,
                utxo.confirmations,
            )

            return ExtendedPoDLECommitment(
                commitment=podle,
                scriptpubkey=utxo.scriptpubkey,
                blockheight=utxo.height,
            )

        logger.error("Failed to generate any fresh PoDLE commitment from available UTXOs")
        return None

    def get_fresh_commitment_utxos(
        self,
        wallet_utxos: list[UTXOInfo],
        cj_amount: int,
        private_key_getter: Callable[[str], bytes | None],
        min_confirmations: int = 5,
        min_percent: int = 20,
        max_retries: int = 3,
    ) -> list[UTXOInfo]:
        """Return PoDLE-capable UTXOs without consuming a commitment index."""
        fresh: list[UTXOInfo] = []
        seen: set[tuple[str, int]] = set()
        for utxo, _ in self._iter_fresh_commitments(
            wallet_utxos,
            cj_amount,
            private_key_getter,
            min_confirmations,
            min_percent,
            max_retries,
        ):
            outpoint = (utxo.txid, utxo.vout)
            if outpoint not in seen:
                fresh.append(utxo)
                seen.add(outpoint)
        return fresh

    def _iter_fresh_commitments(
        self,
        wallet_utxos: list[UTXOInfo],
        cj_amount: int,
        private_key_getter: Callable[[str], bytes | None],
        min_confirmations: int,
        min_percent: int,
        max_retries: int,
    ) -> Iterator[tuple[UTXOInfo, PoDLECommitment]]:
        eligible_utxos = get_eligible_podle_utxos(
            wallet_utxos, cj_amount, min_confirmations, min_percent
        )
        if not eligible_utxos:
            logger.warning("No eligible UTXOs for PoDLE")
            return

        try:
            exclusions = self._local_selection_exclusions()
        except ExternalPoDLEPoolError:
            logger.error("Could not safely read PoDLE selection state")
            return

        try:
            blacklist = get_blacklist()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not load commitment blacklist: {exc}")
            blacklist = None

        for utxo in eligible_utxos:
            private_key = private_key_getter(utxo.address)
            if private_key is None:
                continue

            utxo_str = f"{utxo.txid}:{utxo.vout}"
            found = False
            for index in range(max_retries):
                try:
                    podle = generate_podle(private_key, utxo_str, index)
                    commitment_hex = podle.commitment.hex()
                    if commitment_hex in self.used_commitments:
                        logger.debug("PoDLE commitment retry index already used")
                        logger.bind(sensitive=True).debug(
                            "PoDLE commitment for {} index {} already used", utxo_str, index
                        )
                        continue
                    if blacklist is not None and blacklist.is_blacklisted(commitment_hex):
                        logger.debug("PoDLE commitment retry index is blacklisted")
                        logger.bind(sensitive=True).debug(
                            "PoDLE commitment for {} index {} is blacklisted", utxo_str, index
                        )
                        try:
                            self._claim_local_commitment(commitment_hex)
                        except ExternalPoDLEPoolError:
                            logger.error("Could not safely record blacklisted PoDLE commitment")
                            return
                        continue
                    if commitment_hex in exclusions:
                        logger.debug("PoDLE commitment retry index is unavailable")
                        continue
                    found = True
                    yield utxo, podle
                    break
                except Exception as exc:
                    logger.warning("Failed to generate PoDLE commitment")
                    logger.bind(sensitive=True).warning(
                        "Failed to generate PoDLE for {} index {}: {}", utxo_str, index, exc
                    )
            if not found:
                logger.debug("Skipping UTXO after all PoDLE retry indices were used")
                logger.bind(sensitive=True).debug(
                    "Skipping {}:{} after all {} PoDLE retry indices were used",
                    utxo.txid,
                    utxo.vout,
                    max_retries,
                )
