"""Durable local seller state for the native credential market.

This module deliberately has no network, wallet, or payment-backend dependency.
Callers validate settlement before calling :meth:`MarketStore.finalize`; this
store only makes the resulting allocation and delivery irreversible.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, Literal, cast

from bitcointx.core.key import CKey, CPubKey  # type: ignore[import-not-found]
from nacl.exceptions import CryptoError
from nacl.public import PublicKey, SealedBox
from pydantic import ValidationError
from typing_extensions import TypedDict

from jmcore.credential_market import (
    MARKET_PODLE_RETRIES,
    Allocation,
    BondCredential,
    CredentialPackage,
    Delivery,
    MarketAuthorization,
    MarketError,
    MarketModel,
    MarketQuote,
    PaymentTerms,
    SignedDocument,
    bond_resource,
    canonical,
    decode_document,
    document_hash,
    period_at_height,
    sign_document,
    validate_credential,
    verify_authorization,
)
from jmcore.external_podle import ExternalPoDLE
from jmcore.market_keys import BoundMarketKeys
from jmcore.secure_files import atomic_write_private, exclusive_file_lock, read_private_file

Product = Literal["podle", "bond"]
PaymentRail = Literal["onchain", "lightning"]
WalletLedgerDiagnosisState = Literal[
    "absent", "legacy", "disabled", "activating", "ready", "recovery_required", "unavailable"
]


class WalletLedgerDiagnosis(TypedDict):
    """A momentary, non-authoritative wallet ledger inspection result."""

    state: WalletLedgerDiagnosisState
    schema_version: str | None
    reason: str


_LEGACY_SCHEMA_VERSION = "1"
_WALLET_LEDGER_SCHEMA_VERSION = "2"
_WALLET_LEDGER_INTENT_VERSION = 1
_MAX_ACTIVE_QUOTES = 64
_MAX_QUOTE_TTL = 86400
_MAX_REQUEST_ID_LENGTH = 32
_MAX_OPAQUE_ID_LENGTH = 256
_MAX_SETTLEMENT_REF_LENGTH = 4096
_HEX32_LENGTH = 64


class MarketStoreError(Exception):
    """The local market store cannot safely complete an operation."""


class MarketStoreConflictError(MarketStoreError):
    """An immutable market identifier or reservation has already been used."""


class MarketStoreUnavailableError(MarketStoreError):
    """No eligible inventory or payment request is available."""


class MarketStoreExpiredError(MarketStoreError):
    """A quote is no longer live and cannot be settled."""


class MarketStoreCorruptError(MarketStoreError):
    """On-disk store contents are invalid and must not be reset implicitly."""


class MarketStore:
    """A synchronous, process-safe durable seller lifecycle store.

    Every mutation uses ``BEGIN IMMEDIATE`` with SQLite ``synchronous=FULL``.
    A reservation may release its inventory on expiry, but its payment request is
    deliberately retained forever so a late payment cannot fund another quote.
    """

    def __init__(self, path: Path, *, wallet_id: str | None = None, create: bool = True) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        if path.is_symlink():
            raise MarketStoreError("market store path must not be a symlink")
        if wallet_id is not None:
            self._require_hex32(wallet_id, "wallet_id")

        self.path = path
        self.wallet_id = wallet_id
        self._closed = False
        self._lock = threading.RLock()
        if not create and not self.path.is_file():
            raise MarketStoreUnavailableError("market store does not exist")
        self._prepare_parent()
        if not self.path.exists() and self._intent_artifact_exists():
            raise MarketStoreCorruptError("wallet ledger intent exists without its market store")
        created = self._create_private_database_file() if create else False
        try:
            self._connection = sqlite3.connect(
                self.path if create else self.path.absolute().as_uri() + "?mode=rw",
                uri=not create,
                isolation_level=None,
                check_same_thread=False,
                timeout=30.0,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA busy_timeout = 30000")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = DELETE")
            self._connection.execute("PRAGMA synchronous = FULL")
            self._initialize(created)
            self._harden_permissions()
        except (MarketStoreError, OSError, sqlite3.DatabaseError) as exc:
            if hasattr(self, "_connection"):
                with suppress(sqlite3.Error):
                    self._connection.close()
            if isinstance(exc, MarketStoreError):
                raise
            raise MarketStoreCorruptError("could not open private market store") from exc

    @property
    def is_wallet_ledger(self) -> bool:
        """Whether this store has a validated wallet-scoped PoDLE ledger."""
        with self._lock:
            if self._schema_version(self._connection) != _WALLET_LEDGER_SCHEMA_VERSION:
                return False
            self._verify_wallet_ledger(self._connection)
            return True

    def wallet_state(self) -> Literal["disabled", "activating", "ready", "recovery_required"]:
        """Return the bound wallet's durable activation state.

        A missing ledger row is deliberately treated as disabled only after the
        enclosing database schema has been validated.
        """
        with self._lock:
            version = self._schema_version(self._connection)
            if version == _LEGACY_SCHEMA_VERSION:
                return "disabled"
            self._require_wallet_ledger(self._connection)
            if self.wallet_id is None:
                return "disabled"
            row = self._connection.execute(
                "SELECT state FROM wallet_ledger WHERE wallet_id = ?", (self.wallet_id,)
            ).fetchone()
            if row is None:
                return "disabled"
            state = row["state"]
            if state not in ("disabled", "activating", "ready", "recovery_required"):
                raise MarketStoreCorruptError("wallet ledger state is invalid")
            return cast(Literal["disabled", "activating", "ready", "recovery_required"], state)

    @classmethod
    def diagnose_wallet(
        cls, path: Path, *, wallet_id: str, commitments_path: Path
    ) -> WalletLedgerDiagnosis:
        """Inspect wallet-ledger artifacts without changing files or permissions.

        This is deliberately only a momentary observation. Callers must still open
        and validate the store normally before taking an operational action.
        """

        unavailable = cls._wallet_diagnosis("unavailable", None, "inspection unavailable")
        if not isinstance(path, Path) or not isinstance(commitments_path, Path):
            return unavailable
        try:
            cls._require_hex32(wallet_id, "wallet_id")
            canonical_path = cls._canonical_commitments_path(commitments_path)
            database_before = cls._artifact_signature(path)
            intent_path = path.with_name(f"{path.name}.wallet-ledger")
            intent_before = cls._artifact_signature(intent_path)
            if cls._has_sqlite_sidecars(path):
                return unavailable
        except (MarketStoreError, OSError, TypeError, ValueError):
            return unavailable

        if database_before is None:
            if intent_before is None:
                diagnosis = cls._wallet_diagnosis("absent", None, "no ledger artifacts")
            else:
                diagnosis = cls._wallet_diagnosis(
                    "recovery_required", None, "ledger artifacts are incomplete"
                )
            return cls._finish_wallet_diagnosis(
                path, intent_path, database_before, intent_before, diagnosis
            )
        if not cls._signature_is_regular(database_before):
            diagnosis = cls._wallet_diagnosis(
                "recovery_required", None, "market store artifact is invalid"
            )
            return cls._finish_wallet_diagnosis(
                path, intent_path, database_before, intent_before, diagnosis
            )
        if intent_before is not None and not cls._signature_is_regular(intent_before):
            diagnosis = cls._wallet_diagnosis(
                "recovery_required", None, "ledger intent artifact is invalid"
            )
            return cls._finish_wallet_diagnosis(
                path, intent_path, database_before, intent_before, diagnosis
            )

        connection: sqlite3.Connection | None = None
        diagnosis = cls._wallet_diagnosis("recovery_required", None, "wallet ledger is invalid")
        try:
            connection = sqlite3.connect(
                path.absolute().as_uri() + "?mode=ro&immutable=1",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=30.0,
            )
            connection.row_factory = sqlite3.Row
            inspection = cls.__new__(cls)
            inspection.path = path
            inspection.wallet_id = wallet_id
            inspection._connection = connection
            inspection._closed = False
            inspection._lock = threading.RLock()
            inspection._verify_schema(readonly=True)
            version = inspection._schema_version(connection)
            if version == _LEGACY_SCHEMA_VERSION:
                diagnosis = cls._wallet_diagnosis("legacy", version, "legacy market store")
            else:
                bound_path = inspection._verify_wallet_ledger(connection, readonly=True)
                if bound_path != canonical_path:
                    diagnosis = cls._wallet_diagnosis(
                        "recovery_required", version, "commitments binding does not match"
                    )
                else:
                    state = inspection._wallet_state_for_connection(connection, wallet_id)
                    diagnosis = cls._wallet_diagnosis(state, version, f"wallet ledger is {state}")
        except sqlite3.DatabaseError:
            diagnosis = cls._wallet_diagnosis("unavailable", None, "inspection unavailable")
        except (MarketStoreError, OSError, TypeError, ValueError):
            diagnosis = cls._wallet_diagnosis("recovery_required", None, "wallet ledger is invalid")
        finally:
            if connection is not None:
                with suppress(sqlite3.Error):
                    connection.close()

        return cls._finish_wallet_diagnosis(
            path, intent_path, database_before, intent_before, diagnosis
        )

    @classmethod
    def _finish_wallet_diagnosis(
        cls,
        path: Path,
        intent_path: Path,
        database_before: tuple[int, int, int, int, int, int] | None,
        intent_before: tuple[int, int, int, int, int, int] | None,
        diagnosis: WalletLedgerDiagnosis,
    ) -> WalletLedgerDiagnosis:
        try:
            if (
                cls._has_sqlite_sidecars(path)
                or cls._artifact_signature(path) != database_before
                or cls._artifact_signature(intent_path) != intent_before
            ):
                return cls._wallet_diagnosis("unavailable", None, "inspection unavailable")
        except OSError:
            return cls._wallet_diagnosis("unavailable", None, "inspection unavailable")
        return diagnosis

    @staticmethod
    def _wallet_diagnosis(
        state: WalletLedgerDiagnosisState, schema_version: str | None, reason: str
    ) -> WalletLedgerDiagnosis:
        return {"state": state, "schema_version": schema_version, "reason": reason}

    @staticmethod
    def _artifact_signature(path: Path) -> tuple[int, int, int, int, int, int] | None:
        try:
            artifact = os.lstat(path)
        except FileNotFoundError:
            return None
        return (
            artifact.st_dev,
            artifact.st_ino,
            artifact.st_mode,
            artifact.st_size,
            artifact.st_mtime_ns,
            artifact.st_ctime_ns,
        )

    @staticmethod
    def _signature_is_regular(signature: tuple[int, int, int, int, int, int]) -> bool:
        return stat.S_ISREG(signature[2])

    @staticmethod
    def _has_sqlite_sidecars(path: Path) -> bool:
        return any(
            os.path.lexists(path.with_name(f"{path.name}{suffix}"))
            for suffix in ("-journal", "-wal", "-shm")
        )

    def check_commitments_path(self, path: Path) -> None:
        """Require an exact canonical binding to the activated commitments file."""
        canonical_path = self._canonical_commitments_path(path)
        with self._lock:
            self._require_wallet_ledger(self._connection, canonical_path)

    def activate_wallet(self, commitments_path: Path, *, history_confirmed: bool = False) -> None:
        """Explicitly bind and import the complete legacy PoDLE history once.

        The caller's confirmation is required because a missing or incomplete JSON
        file cannot prove that it contains all historical commitment use.
        """
        if self.wallet_id is None:
            raise MarketStoreError("wallet activation requires a bound wallet_id")
        if history_confirmed is not True:
            raise MarketStoreError("wallet activation requires history_confirmed=True")
        canonical_path = self._canonical_commitments_path(commitments_path)
        commitments_lock = canonical_path.with_name(f"{canonical_path.name}.lock")
        init_lock = self.path.with_name(f"{self.path.name}.init.lock")
        with exclusive_file_lock(commitments_lock), exclusive_file_lock(init_lock), self._lock:
            self._activate_wallet_locked(canonical_path)

    def recover_wallet(self, commitments_path: Path, *, history_confirmed: bool = False) -> None:
        """Explicitly merge a complete confirmed history into a failed v2 activation."""

        if self.wallet_id is None:
            raise MarketStoreError("wallet recovery requires a bound wallet_id")
        if history_confirmed is not True:
            raise MarketStoreError("wallet recovery requires history_confirmed=True")
        canonical_path = self._canonical_commitments_path(commitments_path)
        self._require_existing_regular_commitments_history(canonical_path)
        commitments_lock = canonical_path.with_name(f"{canonical_path.name}.lock")
        init_lock = self.path.with_name(f"{self.path.name}.init.lock")
        with exclusive_file_lock(commitments_lock), exclusive_file_lock(init_lock), self._lock:
            self._require_existing_regular_commitments_history(canonical_path)
            self._recover_wallet_locked(canonical_path)

    def mark_recovery_required(self) -> None:
        """Permanently stop wallet-scoped issuance until an explicit recovery exists."""
        if self.wallet_id is None:
            raise MarketStoreError("marking recovery requires a bound wallet_id")
        with self._transaction() as connection:
            self._require_wallet_ledger(connection)
            self._ensure_wallet_row(connection, self.wallet_id)
            connection.execute(
                "UPDATE wallet_ledger SET state = 'recovery_required' WHERE wallet_id = ?",
                (self.wallet_id,),
            )

    @classmethod
    def rebind_wallet(
        cls,
        path: Path,
        *,
        wallet_id: str,
        previous_commitments_path: Path,
        commitments_path: Path,
        history_confirmed: bool = False,
        writers_stopped: bool = False,
    ) -> None:
        """Explicitly complete a data-directory move, including interrupted attempts.

        All writers of both the original and destination directories must be stopped.
        This changes a binding in an existing complete ledger, never copies or rebuilds
        one. A durable transition blocks ordinary operations until both artifacts agree.
        """
        if history_confirmed is not True or writers_stopped is not True:
            raise MarketStoreError("rebinding requires confirmed history and stopped writers")
        cls._require_hex32(wallet_id, "wallet_id")
        source = cls._canonical_commitments_path(previous_commitments_path)
        target = cls._canonical_commitments_path(commitments_path)
        if source == target:
            raise MarketStoreError("rebinding requires different commitments paths")
        if not isinstance(path, Path) or path.is_symlink() or not path.is_file():
            raise MarketStoreError("rebinding requires an existing regular market store")
        cls._require_existing_regular_commitments_history(target)
        path = path.resolve()
        history_lock = target.with_name(f"{target.name}.lock")
        init_lock = path.with_name(f"{path.name}.init.lock")
        with exclusive_file_lock(history_lock), exclusive_file_lock(init_lock):
            # Normal open deliberately refuses a pending transition. This private
            # maintenance handle validates that exact transition before any mutation.
            store = cls.__new__(cls)
            store.path = path
            store.wallet_id = wallet_id
            store._closed = False
            store._lock = threading.RLock()
            try:
                store._connection = sqlite3.connect(
                    path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=30.0
                )
                store._connection.row_factory = sqlite3.Row
                store._connection.execute("PRAGMA foreign_keys = ON")
                store._connection.execute("PRAGMA synchronous = FULL")
                store._rebind_wallet_locked(source, target)
            except (OSError, sqlite3.DatabaseError) as exc:
                raise MarketStoreCorruptError("could not complete wallet ledger rebinding") from exc
            finally:
                if hasattr(store, "_connection"):
                    store.close()

    def _rebind_wallet_locked(self, source: Path, target: Path) -> None:
        paths = (source, target)
        marker = canonical({"from": str(source), "to": str(target)}).decode("ascii")
        with self._transaction() as connection:
            self._verify_schema(rebind_paths=paths)
            bound = self._verify_wallet_ledger(connection, rebind_paths=paths)
            if self._wallet_state_for_connection(connection, self.wallet_id) == "disabled":
                raise MarketStoreUnavailableError("rebinding requires an activated wallet")
            pending = connection.execute(
                "SELECT value FROM metadata WHERE key = 'commitments_rebind'"
            ).fetchone()
            if bound == target and pending is None:
                completed = connection.execute(
                    "SELECT value FROM metadata WHERE key = 'commitments_rebind_completed'"
                ).fetchone()
                if completed is None or completed["value"] != marker:
                    raise MarketStoreError(
                        "previous commitments path does not match completed rebinding"
                    )
                return
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES ('commitments_rebind', ?) "
                "ON CONFLICT(key) DO NOTHING",
                (marker,),
            )

        # The committed marker gates local use and seller issuance throughout the
        # history merge and the non-atomic update of SQLite plus the intent file.
        used, external = self._read_commitments_history(target, require_existing=True)
        with self._transaction() as connection:
            self._verify_schema(rebind_paths=paths)
            self._import_commitments_history(connection, used, external)
            connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'commitments_path'", (str(target),)
            )
        self._write_wallet_intent(target)
        with self._transaction() as connection:
            self._verify_schema(rebind_paths=paths)
            connection.execute(
                "INSERT INTO metadata (key, value) VALUES ('commitments_rebind_completed', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (marker,),
            )
            connection.execute("DELETE FROM metadata WHERE key = 'commitments_rebind'")
            self._verify_wallet_ledger(connection, target)

    def hold_local(self, commitment: str) -> bool:
        """Record a local PoDLE hold unless the global commitment is unavailable."""
        self._require_hex32(commitment, "commitment")
        with self._transaction() as connection:
            self._require_local_ledger_access(connection, mutation=True)
            row = self._ownership_row(connection, commitment)
            if row is not None:
                return False
            self._ensure_bound_wallet_disabled_row(connection)
            connection.execute(
                """
                INSERT INTO podle_ownership (commitment, wallet_id, state, inventory_id)
                VALUES (?, ?, 'held_local', NULL)
                """,
                (commitment, self.wallet_id),
            )
            return True

    def claim_local(self, commitment: str) -> bool:
        """Make local PoDLE use permanent without racing market inventory insertion."""
        self._require_hex32(commitment, "commitment")
        with self._transaction() as connection:
            self._require_local_ledger_access(connection, mutation=True)
            row = self._ownership_row(connection, commitment)
            if row is None:
                self._ensure_bound_wallet_disabled_row(connection)
                connection.execute(
                    """
                    INSERT INTO podle_ownership (commitment, wallet_id, state, inventory_id)
                    VALUES (?, ?, 'used', NULL)
                    """,
                    (commitment, self.wallet_id),
                )
                return True
            if row["state"] != "held_local":
                return False
            owner = row["wallet_id"]
            if owner is not None and owner != self.wallet_id:
                return False
            self._ensure_bound_wallet_disabled_row(connection)
            connection.execute(
                """
                UPDATE podle_ownership
                SET wallet_id = ?, state = 'used'
                WHERE commitment = ? AND state = 'held_local'
                """,
                (self.wallet_id, commitment),
            )
            return True

    def is_available_for_local(self, commitment: str) -> bool:
        """Return whether a commitment remains locally usable for this wallet."""
        self._require_hex32(commitment, "commitment")
        with self._lock:
            if not self._require_local_ledger_access(self._connection, mutation=False):
                return False
            row = self._ownership_row(self._connection, commitment)
            if row is None:
                return True
            return row["state"] == "held_local" and (
                row["wallet_id"] is None or row["wallet_id"] == self.wallet_id
            )

    def unavailable_local_commitments(self) -> set[str]:
        """Snapshot exclusions for selection; final use still requires claim_local."""
        with self._lock:
            if not self._require_local_ledger_access(self._connection, mutation=False):
                raise MarketStoreUnavailableError("wallet ledger is not available for local use")
            rows = self._connection.execute(
                """
                SELECT commitment FROM podle_ownership
                WHERE state != 'held_local'
                   OR (wallet_id IS NOT NULL AND wallet_id IS NOT ?)
                UNION ALL
                SELECT resource FROM inventory WHERE product = 'podle'
                """,
                (self.wallet_id,),
            )
            return {str(row[0]) for row in rows}

    def close(self) -> None:
        """Close the SQLite connection after flushing already committed state."""
        with self._lock:
            if not self._closed:
                self._connection.close()
                self._closed = True

    def __enter__(self) -> MarketStore:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def add_inventory(
        self,
        product: Product,
        resource: str,
        credential: dict[str, Any] | None,
    ) -> None:
        """Add one credential resource that has not been offered before.

        PoDLE inventory is fully validated on insert. Bond inventory may omit its
        certificate while a quote waits for the bond owner to provide it.
        """
        self._require_product(product)
        self._require_hex32(resource, "resource")
        encoded_credential, certificate_pubkey = self._validate_inventory_credential(
            product, resource, credential
        )
        with self._transaction() as connection:
            wallet_ledger = self._require_seller_wallet_ready(connection)
            existing = connection.execute(
                "SELECT id FROM inventory WHERE product = ? AND resource = ?",
                (product, resource),
            ).fetchone()
            if existing is not None:
                raise MarketStoreConflictError("inventory resource has already been recorded")
            if wallet_ledger and product == "podle":
                ownership = self._ownership_row(connection, resource)
                if ownership is not None:
                    raise MarketStoreConflictError("PoDLE commitment already has an owner")
            if wallet_ledger:
                cursor = connection.execute(
                    """
                    INSERT INTO inventory (
                        product, resource, credential, certificate_pubkey, state,
                        reservation_quote_id, wallet_id
                    ) VALUES (?, ?, ?, ?, 'available', NULL, ?)
                    """,
                    (product, resource, encoded_credential, certificate_pubkey, self.wallet_id),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO inventory (
                        product, resource, credential, certificate_pubkey, state,
                        reservation_quote_id
                    ) VALUES (?, ?, ?, ?, 'available', NULL)
                    """,
                    (product, resource, encoded_credential, certificate_pubkey),
                )
            if wallet_ledger and product == "podle":
                connection.execute(
                    """
                    INSERT INTO podle_ownership (commitment, wallet_id, state, inventory_id)
                    VALUES (?, ?, 'market_inventory', ?)
                    """,
                    (resource, self.wallet_id, cursor.lastrowid),
                )

    def add_payment(self, terms: PaymentTerms, payment_id: str) -> None:
        """Queue one validated, never-reassignable payment request."""
        if not isinstance(terms, PaymentTerms):
            raise TypeError("terms must be a PaymentTerms")
        try:
            validated_terms = PaymentTerms.model_validate(terms.model_dump(mode="json"))
        except ValidationError as exc:
            raise MarketError("invalid payment terms") from exc
        self._require_opaque_identifier(payment_id, "payment_id", _MAX_OPAQUE_ID_LENGTH)
        encoded_terms = canonical(validated_terms)
        with self._transaction() as connection:
            self._require_seller_wallet_ready(connection)
            existing = connection.execute(
                "SELECT payment_id FROM payments WHERE payment_id = ? OR request = ?",
                (payment_id, validated_terms.request),
            ).fetchone()
            if existing is not None:
                raise MarketStoreConflictError("payment id or request has already been recorded")
            connection.execute(
                """
                INSERT INTO payments (payment_id, rail, request, terms, state, quote_id)
                VALUES (?, ?, ?, ?, 'available', NULL)
                """,
                (payment_id, validated_terms.rail, validated_terms.request, encoded_terms),
            )

    def create_quote(
        self,
        authorization: SignedDocument,
        seller_key: CKey | BoundMarketKeys,
        buyer_pubkey: str,
        product: Product,
        certificate_pubkey: str | None,
        rail: PaymentRail,
        max_price_sats: int,
        now: int,
        height: int,
        *,
        ttl: int = 300,
        request_id: str,
    ) -> SignedDocument:
        """Reserve matching inventory and payment terms, then return a signed quote.

        ``request_id`` is an immutable idempotency key. Replaying every parameter
        exactly returns the original signed quote; changing any bound parameter is
        rejected, even after the quote has expired.
        """
        self._require_signed_document(authorization, "authorization")
        seller_pubkey = self._seller_pubkey(seller_key)
        self._require_hex32(buyer_pubkey, "buyer_pubkey")
        self._validate_sealed_box_pubkey(buyer_pubkey)
        self._require_product(product)
        self._validate_certificate_pubkey(product, certificate_pubkey)
        self._require_rail(rail)
        self._require_int(max_price_sats, "max_price_sats", minimum=1)
        self._require_int(now, "now", minimum=1)
        self._require_int(height, "height", minimum=1)
        self._require_int(ttl, "ttl", minimum=1, maximum=_MAX_QUOTE_TTL)
        if rail == "lightning" and ttl > 900:
            raise MarketError("Lightning quote lifetime exceeds 900 seconds")
        self._require_request_id(request_id)
        fingerprint = canonical(
            {
                "authorization": authorization.model_dump(mode="json"),
                "seller_pubkey": seller_pubkey,
                "buyer_pubkey": buyer_pubkey,
                "product": product,
                "certificate_pubkey": certificate_pubkey,
                "rail": rail,
                "max_price_sats": max_price_sats,
                "ttl": ttl,
            }
        )

        with self._transaction() as connection:
            wallet_ledger = self._require_seller_wallet_ready(connection)
            self._require_bound_seller_wallet(connection, seller_key, wallet_ledger)
            self._expire_live_quotes(connection, now)
            existing = connection.execute(
                "SELECT * FROM quotes WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if bytes(existing["request_fingerprint"]) != fingerprint:
                    raise MarketStoreConflictError(
                        "request_id is bound to different quote parameters"
                    )
                if isinstance(seller_key, BoundMarketKeys):
                    authority, _quote = self._verified_quote_from_row(existing)
                    seller_key.validate_seller_authorization(authority)
                return self._signed_document_from_storage(bytes(existing["quote_document"]))

            authority = self._validate_authorization(authorization, seller_pubkey, height)
            if isinstance(seller_key, BoundMarketKeys):
                seller_key.validate_seller_authorization(authority)
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM quotes WHERE state = 'live'"
            ).fetchone()
            if active is None or int(active["count"]) >= _MAX_ACTIVE_QUOTES:
                raise MarketStoreUnavailableError("maximum active quote capacity reached")

            payment_row = connection.execute(
                """
                SELECT payment_id, terms FROM payments
                WHERE state = 'available' AND rail = ?
                ORDER BY rowid
                LIMIT 1
                """,
                (rail,),
            ).fetchone()
            if payment_row is None:
                raise MarketStoreUnavailableError("no payment request is available for this rail")
            payment = self._payment_terms_from_storage(bytes(payment_row["terms"]))
            if payment.amount_sats > max_price_sats:
                raise MarketStoreUnavailableError(
                    "available payment request exceeds buyer price limit"
                )

            inventory_row = self._select_inventory(
                connection,
                authority,
                product,
                certificate_pubkey,
                wallet_ledger,
            )
            if inventory_row is None:
                raise MarketStoreUnavailableError("no matching credential inventory is available")

            quote_id = secrets.token_hex(32)
            quote = MarketQuote(
                authorization=authorization,
                quote_id=quote_id,
                buyer_pubkey=buyer_pubkey,
                product=product,
                resource=str(inventory_row["resource"]),
                certificate_pubkey=certificate_pubkey,
                payment=payment,
                created_at=now,
                expires_at=now + ttl,
            )
            quote_document = self._sign_document(quote, seller_key)
            encoded_quote = canonical(quote_document)
            cursor = connection.execute(
                """
                UPDATE inventory
                SET state = 'reserved', reservation_quote_id = ?
                WHERE id = ? AND state = 'available'
                """,
                (quote_id, int(inventory_row["id"])),
            )
            if cursor.rowcount != 1:
                raise MarketStoreUnavailableError("inventory was reserved by another writer")
            cursor = connection.execute(
                """
                UPDATE payments
                SET state = 'reserved', quote_id = ?
                WHERE payment_id = ? AND state = 'available'
                """,
                (quote_id, str(payment_row["payment_id"])),
            )
            if cursor.rowcount != 1:
                raise MarketStoreUnavailableError("payment request was reserved by another writer")
            connection.execute(
                """
                INSERT INTO quotes (
                    quote_id, request_id, request_fingerprint, quote_document,
                    buyer_pubkey, product, resource, certificate_pubkey, inventory_id,
                    payment_id, created_at, expires_at, state, pending_credential,
                    settlement_ref, package, sealed_delivery
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', ?, NULL, NULL, NULL)
                """,
                (
                    quote_id,
                    request_id,
                    fingerprint,
                    encoded_quote,
                    buyer_pubkey,
                    product,
                    str(inventory_row["resource"]),
                    certificate_pubkey,
                    int(inventory_row["id"]),
                    str(payment_row["payment_id"]),
                    now,
                    now + ttl,
                    inventory_row["credential"],
                ),
            )
            return quote_document

    def get_quote(self, quote_id: str) -> SignedDocument:
        """Return the locally persisted signed quote, including terminal quotes."""
        self._require_hex32(quote_id, "quote_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)
            ).fetchone()
        if row is None:
            raise MarketStoreUnavailableError("quote does not exist")
        self._verified_quote_from_row(row)
        return self._signed_document_from_storage(bytes(row["quote_document"]))

    def pending(self, now: int) -> list[SignedDocument]:
        """Return at most 64 live quotes after expiring stale reservations."""
        self._require_int(now, "now", minimum=1)
        with self._transaction() as connection:
            if self._schema_version(connection) == _WALLET_LEDGER_SCHEMA_VERSION:
                self._require_wallet_ledger(connection)
            self._expire_live_quotes(connection, now)
            rows = connection.execute(
                """
                SELECT * FROM quotes
                WHERE state = 'live'
                ORDER BY created_at, quote_id
                LIMIT ?
                """,
                (_MAX_ACTIVE_QUOTES,),
            ).fetchall()
            for row in rows:
                self._verified_quote_from_row(row)
            return [
                self._signed_document_from_storage(bytes(row["quote_document"])) for row in rows
            ]

    def attach_credential(self, quote_id: str, credential: dict[str, Any]) -> None:
        """Persist one credential supplied while the corresponding quote is live."""
        self._require_hex32(quote_id, "quote_id")
        if type(credential) is not dict:
            raise TypeError("credential must be a dict")
        encoded_credential = canonical(credential)
        with self._transaction() as connection:
            wallet_ledger = self._require_seller_wallet_ready(connection)
            row = self._quote_row(connection, quote_id)
            if row is None:
                raise MarketStoreUnavailableError("quote does not exist")
            if row["state"] != "live":
                raise MarketStoreExpiredError("quote is no longer live")
            self._require_quote_wallet_ownership(connection, row, wallet_ledger)
            authority, quote = self._verified_quote_from_row(row)
            allocation = self._allocation_for_quote(authority, quote)
            self._validate_credential_for_allocation(authority, allocation, credential)
            previous = row["pending_credential"]
            if previous is not None:
                if bytes(previous) != encoded_credential:
                    raise MarketStoreConflictError("quote already has a different credential")
                return
            connection.execute(
                "UPDATE quotes SET pending_credential = ? WHERE quote_id = ?",
                (encoded_credential, quote_id),
            )

    def finalize(
        self,
        quote_id: str,
        seller_key: CKey | BoundMarketKeys,
        settlement_ref: str,
        now: int,
        height: int,
    ) -> CredentialPackage:
        """Irreversibly sign and persist an allocation after caller-owned settlement.

        A finalized quote is idempotent only for its original settlement reference.
        The persisted package and encrypted delivery are committed before this method
        returns, so a crash cannot cause a second allocation for the same resource.
        """
        self._require_hex32(quote_id, "quote_id")
        seller_pubkey = self._seller_pubkey(seller_key)
        self._require_opaque_identifier(
            settlement_ref, "settlement_ref", _MAX_SETTLEMENT_REF_LENGTH
        )
        self._require_int(now, "now", minimum=1)
        self._require_int(height, "height", minimum=1)

        with self._transaction() as connection:
            wallet_ledger = self._require_seller_wallet_ready(connection)
            self._require_bound_seller_wallet(connection, seller_key, wallet_ledger)
            row = self._quote_row(connection, quote_id)
            if row is None:
                raise MarketStoreUnavailableError("quote does not exist")
            authority, quote = self._verified_quote_from_row(row)
            if isinstance(seller_key, BoundMarketKeys):
                seller_key.validate_seller_authorization(authority)
            self._require_matching_seller(authority, seller_pubkey)
            self._require_quote_wallet_ownership(connection, row, wallet_ledger)
            if row["state"] == "finalized":
                if row["settlement_ref"] != settlement_ref:
                    raise MarketStoreConflictError(
                        "quote was finalized with a different settlement reference"
                    )
                package = row["package"]
                if package is None:
                    raise MarketStoreCorruptError("finalized quote has no credential package")
                return self._package_from_storage(bytes(package))

            self._expire_live_quotes(connection, now)
            row = self._quote_row(connection, quote_id)
            if row is None or row["state"] != "live":
                raise MarketStoreExpiredError("quote has expired")
            if authority.period != period_at_height(height):
                raise MarketStoreExpiredError("quote is from a different retarget period")
            if quote.expires_at <= now:
                raise MarketStoreExpiredError("quote has expired")

            settlement_owner = connection.execute(
                "SELECT quote_id FROM quotes WHERE settlement_ref = ?", (settlement_ref,)
            ).fetchone()
            if settlement_owner is not None:
                raise MarketStoreConflictError("settlement reference has already been used")
            inventory = connection.execute(
                "SELECT state, reservation_quote_id FROM inventory WHERE id = ?",
                (int(row["inventory_id"]),),
            ).fetchone()
            if (
                inventory is None
                or inventory["state"] != "reserved"
                or inventory["reservation_quote_id"] != quote_id
            ):
                raise MarketStoreCorruptError("quote inventory reservation is missing")
            payment = connection.execute(
                "SELECT state, quote_id, terms FROM payments WHERE payment_id = ?",
                (str(row["payment_id"]),),
            ).fetchone()
            if payment is None or payment["state"] != "reserved" or payment["quote_id"] != quote_id:
                raise MarketStoreCorruptError("quote payment reservation is missing")
            if quote.payment != self._payment_terms_from_storage(bytes(payment["terms"])):
                raise MarketStoreCorruptError("stored quote payment does not match reservation")
            pending_credential = row["pending_credential"]
            if pending_credential is None:
                raise MarketStoreUnavailableError("quote has no attached credential")
            credential = self._credential_from_storage(bytes(pending_credential))
            allocation = self._allocation_for_quote(authority, quote)
            self._validate_credential_for_allocation(authority, allocation, credential)
            allocation_document = self._sign_document(allocation, seller_key)
            delivery_document = self._sign_document(
                Delivery(allocation=document_hash(allocation_document.body), credential=credential),
                seller_key,
            )
            package = CredentialPackage(
                authorization=quote.authorization,
                allocation=allocation_document,
                delivery=delivery_document,
            )
            package.verify()
            encoded_package = canonical(package)
            sealed_delivery = self._seal_package(package, quote.buyer_pubkey)
            connection.execute(
                """
                UPDATE quotes
                SET state = 'finalized', settlement_ref = ?, package = ?, sealed_delivery = ?
                WHERE quote_id = ? AND state = 'live'
                """,
                (settlement_ref, encoded_package, sealed_delivery, quote_id),
            )
            connection.execute(
                """
                UPDATE inventory SET state = 'consumed'
                WHERE id = ? AND state = 'reserved' AND reservation_quote_id = ?
                """,
                (int(row["inventory_id"]), quote_id),
            )
            connection.execute(
                """
                UPDATE payments SET state = 'consumed'
                WHERE payment_id = ? AND state = 'reserved' AND quote_id = ?
                """,
                (str(row["payment_id"]), quote_id),
            )
            return package

    def get_delivery(self, quote_id: str) -> str | None:
        """Return only the cached sealed-box package for an authenticated service path."""
        self._require_hex32(quote_id, "quote_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT sealed_delivery FROM quotes WHERE quote_id = ?", (quote_id,)
            ).fetchone()
        if row is None:
            raise MarketStoreUnavailableError("quote does not exist")
        delivery = row["sealed_delivery"]
        if delivery is None:
            return None
        if not isinstance(delivery, str):
            raise MarketStoreCorruptError("sealed delivery is invalid")
        return delivery

    def get_package(self, quote_id: str) -> CredentialPackage | None:
        """Return trusted local package evidence for explicit CLI export/import only."""
        self._require_hex32(quote_id, "quote_id")
        with self._lock:
            row = self._connection.execute(
                "SELECT package FROM quotes WHERE quote_id = ?", (quote_id,)
            ).fetchone()
        if row is None:
            raise MarketStoreUnavailableError("quote does not exist")
        package = row["package"]
        if package is None:
            return None
        return self._package_from_storage(bytes(package))

    def _prepare_parent(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.path.parent, 0o700)
        except OSError as exc:
            raise MarketStoreError("could not create private market store directory") from exc

    def _create_private_database_file(self) -> bool:
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            return False
        except OSError as exc:
            raise MarketStoreError("could not create private market store") from exc
        try:
            if os.name != "nt":
                os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        return True

    def _initialize(self, created: bool) -> None:
        lock_path = self.path.with_name(f"{self.path.name}.init.lock")
        with exclusive_file_lock(lock_path), self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if created:
                    self._create_schema()
                else:
                    self._verify_schema()
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def _create_schema(self) -> None:
        statements = (
            """
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE inventory (
                id INTEGER PRIMARY KEY,
                product TEXT NOT NULL CHECK (product IN ('podle', 'bond')),
                resource TEXT NOT NULL,
                credential BLOB,
                certificate_pubkey TEXT,
                state TEXT NOT NULL CHECK (state IN ('available', 'reserved', 'consumed')),
                reservation_quote_id TEXT,
                UNIQUE (product, resource)
            )
            """,
            """
            CREATE TABLE payments (
                payment_id TEXT PRIMARY KEY,
                rail TEXT NOT NULL CHECK (rail IN ('onchain', 'lightning')),
                request TEXT NOT NULL UNIQUE,
                terms BLOB NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('available', 'reserved', 'consumed')),
                quote_id TEXT UNIQUE
            )
            """,
            """
            CREATE TABLE quotes (
                quote_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                request_fingerprint BLOB NOT NULL,
                quote_document BLOB NOT NULL,
                buyer_pubkey TEXT NOT NULL,
                product TEXT NOT NULL CHECK (product IN ('podle', 'bond')),
                resource TEXT NOT NULL,
                certificate_pubkey TEXT,
                inventory_id INTEGER NOT NULL REFERENCES inventory(id),
                payment_id TEXT NOT NULL UNIQUE REFERENCES payments(payment_id),
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('live', 'expired', 'finalized')),
                pending_credential BLOB,
                settlement_ref TEXT UNIQUE,
                package BLOB,
                sealed_delivery TEXT
            )
            """,
            "CREATE INDEX quotes_live_expiry ON quotes(state, expires_at)",
            "INSERT INTO metadata (key, value) VALUES ('schema_version', '1')",
        )
        for statement in statements:
            self._connection.execute(statement)

    def _verify_schema(
        self, *, readonly: bool = False, rebind_paths: tuple[Path, Path] | None = None
    ) -> None:
        integrity = self._connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise MarketStoreCorruptError("market store integrity check failed")
        tables = self._table_names(self._connection)
        if not {"metadata", "inventory", "payments", "quotes"}.issubset(tables):
            raise MarketStoreCorruptError("market store schema is missing")
        version = self._schema_version(self._connection)
        ledger_tables = {"wallet_ledger", "podle_ownership"}
        if version == _LEGACY_SCHEMA_VERSION:
            if tables.intersection(ledger_tables) or self._intent_artifact_exists():
                raise MarketStoreCorruptError(
                    "legacy store has interrupted wallet ledger activation"
                )
            return
        if not ledger_tables.issubset(tables):
            raise MarketStoreCorruptError("wallet ledger schema is missing")
        self._verify_wallet_ledger(self._connection, readonly=readonly, rebind_paths=rebind_paths)

    def _schema_version(self, connection: sqlite3.Connection) -> Literal["1", "2"]:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row["value"] not in (
            _LEGACY_SCHEMA_VERSION,
            _WALLET_LEDGER_SCHEMA_VERSION,
        ):
            raise MarketStoreCorruptError("unsupported market store schema")
        return cast(Literal["1", "2"], row["value"])

    @staticmethod
    def _table_names(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    @staticmethod
    def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _has_foreign_key(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        target_table: str,
        target_column: str,
    ) -> bool:
        return any(
            row["from"] == column and row["table"] == target_table and row["to"] == target_column
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        )

    @staticmethod
    def _has_unique_key(
        connection: sqlite3.Connection, table: str, columns: tuple[str, ...]
    ) -> bool:
        table_columns = connection.execute(f"PRAGMA table_info({table})").fetchall()
        primary_key = tuple(
            str(row["name"])
            for row in sorted(table_columns, key=lambda row: int(row["pk"]))
            if int(row["pk"]) > 0
        )
        if primary_key == columns:
            return True
        for index in connection.execute(f"PRAGMA index_list({table})"):
            if not int(index["unique"]) or int(index["partial"]):
                continue
            index_columns = tuple(
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index["name"],)
                )
            )
            if index_columns == columns:
                return True
        return False

    def _require_wallet_ledger(
        self, connection: sqlite3.Connection, expected_path: Path | None = None
    ) -> Path:
        if self._schema_version(connection) != _WALLET_LEDGER_SCHEMA_VERSION:
            raise MarketStoreError("wallet ledger has not been activated")
        return self._wallet_ledger_path(connection, expected_path)

    def _verify_wallet_ledger(
        self,
        connection: sqlite3.Connection,
        expected_path: Path | None = None,
        *,
        readonly: bool = False,
        rebind_paths: tuple[Path, Path] | None = None,
    ) -> Path:
        if self._schema_version(connection) != _WALLET_LEDGER_SCHEMA_VERSION:
            raise MarketStoreError("wallet ledger has not been activated")
        expected_columns = {
            "wallet_ledger": {"wallet_id", "state"},
            "podle_ownership": {"commitment", "wallet_id", "state", "inventory_id"},
            "inventory": {
                "id",
                "product",
                "resource",
                "credential",
                "certificate_pubkey",
                "state",
                "reservation_quote_id",
                "wallet_id",
            },
        }
        for table, columns in expected_columns.items():
            if not columns.issubset(self._table_columns(connection, table)):
                raise MarketStoreCorruptError("wallet ledger table has an invalid layout")
        if (
            not self._has_unique_key(connection, "wallet_ledger", ("wallet_id",))
            or not self._has_unique_key(connection, "podle_ownership", ("commitment",))
            or not self._has_unique_key(connection, "podle_ownership", ("inventory_id",))
            or not self._has_foreign_key(
                connection, "inventory", "wallet_id", "wallet_ledger", "wallet_id"
            )
            or not self._has_foreign_key(
                connection, "podle_ownership", "wallet_id", "wallet_ledger", "wallet_id"
            )
            or not self._has_foreign_key(
                connection, "podle_ownership", "inventory_id", "inventory", "id"
            )
        ):
            raise MarketStoreCorruptError("wallet ledger foreign keys are missing")
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise MarketStoreCorruptError("wallet ledger foreign key check failed")
        wallet_states: set[str] = set()
        for row in connection.execute("SELECT wallet_id, state FROM wallet_ledger"):
            try:
                self._require_hex32(str(row["wallet_id"]), "stored wallet_id")
            except ValueError as exc:
                raise MarketStoreCorruptError("wallet ledger wallet_id is invalid") from exc
            if row["state"] not in ("disabled", "activating", "ready", "recovery_required"):
                raise MarketStoreCorruptError("wallet ledger state is invalid")
            wallet_states.add(str(row["state"]))
        if not wallet_states or wallet_states == {"disabled"}:
            raise MarketStoreCorruptError("wallet ledger has no activation state")
        ownership_rows = connection.execute(
            """
            SELECT ownership.commitment, ownership.wallet_id, ownership.state,
                   ownership.inventory_id, inventory.product, inventory.resource,
                   inventory.wallet_id AS inventory_wallet_id
            FROM podle_ownership AS ownership
            LEFT JOIN inventory ON inventory.id = ownership.inventory_id
            """
        ).fetchall()
        for ownership in ownership_rows:
            try:
                self._require_hex32(str(ownership["commitment"]), "stored commitment")
            except ValueError as exc:
                raise MarketStoreCorruptError("wallet ledger commitment is invalid") from exc
            state = ownership["state"]
            if state not in ("held_local", "used", "market_inventory"):
                raise MarketStoreCorruptError("PoDLE ownership state is invalid")
            if state == "market_inventory":
                if (
                    ownership["inventory_id"] is None
                    or ownership["product"] != "podle"
                    or ownership["resource"] != ownership["commitment"]
                    or ownership["inventory_wallet_id"] != ownership["wallet_id"]
                ):
                    raise MarketStoreCorruptError("market PoDLE ownership does not match inventory")
            elif ownership["inventory_id"] is not None:
                raise MarketStoreCorruptError("local PoDLE ownership references market inventory")
        inventory_rows = connection.execute(
            """
            SELECT inventory.id, inventory.resource, inventory.wallet_id,
                   ownership.commitment, ownership.wallet_id AS ownership_wallet_id,
                   ownership.state, ownership.inventory_id
            FROM inventory
            LEFT JOIN podle_ownership AS ownership ON ownership.inventory_id = inventory.id
            WHERE inventory.product = 'podle'
            """
        ).fetchall()
        for inventory in inventory_rows:
            if (
                inventory["commitment"] != inventory["resource"]
                or inventory["ownership_wallet_id"] != inventory["wallet_id"]
                or inventory["state"] != "market_inventory"
                or inventory["inventory_id"] != inventory["id"]
            ):
                raise MarketStoreCorruptError("market inventory has no matching PoDLE ownership")
        return self._wallet_ledger_path(
            connection, expected_path, readonly=readonly, rebind_paths=rebind_paths
        )

    def _wallet_ledger_path(
        self,
        connection: sqlite3.Connection,
        expected_path: Path | None = None,
        *,
        readonly: bool = False,
        rebind_paths: tuple[Path, Path] | None = None,
    ) -> Path:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'commitments_path'"
        ).fetchone()
        if row is None or not isinstance(row["value"], str):
            raise MarketStoreCorruptError("wallet ledger commitments path is missing")
        try:
            metadata_path = self._canonical_commitments_path(Path(row["value"]))
        except (MarketStoreError, TypeError, ValueError) as exc:
            raise MarketStoreCorruptError("wallet ledger commitments path is invalid") from exc
        if str(metadata_path) != row["value"]:
            raise MarketStoreCorruptError("wallet ledger commitments path is not canonical")
        intent_path = self._read_wallet_intent(readonly=readonly)
        pending = connection.execute(
            "SELECT value FROM metadata WHERE key = 'commitments_rebind'"
        ).fetchone()
        if pending is not None:
            if rebind_paths is None:
                raise MarketStoreCorruptError(
                    "wallet ledger rebinding requires explicit completion"
                )
            source, target = rebind_paths
            marker = canonical({"from": str(source), "to": str(target)}).decode("ascii")
            if pending["value"] != marker or (metadata_path, intent_path) not in (
                (source, source),
                (target, source),
                (target, target),
            ):
                raise MarketStoreCorruptError("wallet ledger rebinding artifacts do not match")
            return metadata_path
        if intent_path != metadata_path:
            raise MarketStoreCorruptError("wallet ledger intent does not match database binding")
        if rebind_paths is not None and metadata_path not in rebind_paths:
            raise MarketStoreError("previous commitments path does not match wallet ledger binding")
        if expected_path is not None and metadata_path != expected_path:
            raise MarketStoreError("commitments path does not match wallet ledger binding")
        return metadata_path

    def _intent_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.wallet-ledger")

    def _intent_artifact_exists(self) -> bool:
        return os.path.lexists(self._intent_path())

    @staticmethod
    def _canonical_commitments_path(path: Path) -> Path:
        if not isinstance(path, Path):
            raise TypeError("commitments_path must be a pathlib.Path")
        if ".." in path.parts:
            raise ValueError("commitments_path must not contain parent traversal")
        if path.is_symlink():
            raise MarketStoreError("commitments_path must not be a symlink")
        try:
            canonical_path = path.resolve(strict=False)
        except OSError as exc:
            raise MarketStoreError("could not canonicalize commitments_path") from exc
        if canonical_path.is_symlink():
            raise MarketStoreError("commitments_path must not be a symlink")
        return canonical_path

    @staticmethod
    def _wallet_intent_bytes(commitments_path: Path) -> bytes:
        return (
            json.dumps(
                {
                    "commitments_path": str(commitments_path),
                    "version": _WALLET_LEDGER_INTENT_VERSION,
                },
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            + b"\n"
        )

    def _write_wallet_intent(self, commitments_path: Path) -> None:
        intent_path = self._intent_path()
        if intent_path.is_symlink():
            raise MarketStoreCorruptError("wallet ledger intent must not be a symlink")
        try:
            atomic_write_private(intent_path, self._wallet_intent_bytes(commitments_path))
            self._fsync_parent(intent_path.parent)
        except OSError as exc:
            raise MarketStoreError("could not durably write wallet ledger intent") from exc

    @staticmethod
    def _fsync_parent(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _read_wallet_intent(self, *, readonly: bool = False) -> Path:
        intent_path = self._intent_path()
        if not self._intent_artifact_exists() or intent_path.is_symlink():
            raise MarketStoreCorruptError("wallet ledger intent is missing or invalid")
        try:
            if readonly:
                descriptor = os.open(
                    intent_path,
                    os.O_RDONLY
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0),
                )
                try:
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        raise OSError("wallet ledger intent must be a regular file")
                    with os.fdopen(descriptor, "rb") as intent_file:
                        descriptor = -1
                        raw = intent_file.read()
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
            else:
                raw = read_private_file(intent_path)
            data = json.loads(raw)
            if (
                not isinstance(data, dict)
                or set(data) != {"version", "commitments_path"}
                or data["version"] != _WALLET_LEDGER_INTENT_VERSION
                or not isinstance(data["commitments_path"], str)
            ):
                raise ValueError("invalid wallet ledger intent")
            commitments_path = self._canonical_commitments_path(Path(data["commitments_path"]))
            if str(commitments_path) != data["commitments_path"]:
                raise ValueError("wallet ledger intent path is not canonical")
            if raw != self._wallet_intent_bytes(commitments_path):
                raise ValueError("wallet ledger intent is not canonical JSON")
            return commitments_path
        except (MarketStoreError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MarketStoreCorruptError("wallet ledger intent is invalid") from exc

    def _activate_wallet_locked(self, commitments_path: Path) -> None:
        version = self._schema_version(self._connection)
        if version == _WALLET_LEDGER_SCHEMA_VERSION:
            self._verify_wallet_ledger(self._connection, commitments_path)
            state = self._wallet_state_for_connection(self._connection, self.wallet_id)
            if state == "ready":
                return
            if state in ("activating", "recovery_required"):
                raise MarketStoreUnavailableError("wallet ledger requires explicit recovery")
        else:
            self._write_wallet_intent(commitments_path)

        with self._transaction() as connection:
            if version == _LEGACY_SCHEMA_VERSION:
                self._upgrade_to_wallet_ledger(connection, commitments_path)
            else:
                self._require_wallet_ledger(connection, commitments_path)
            state = self._wallet_state_for_connection(connection, self.wallet_id)
            if state == "ready":
                return
            if state in ("activating", "recovery_required"):
                raise MarketStoreUnavailableError("wallet ledger requires explicit recovery")
            self._ensure_wallet_row(connection, self.wallet_id)
            connection.execute(
                "UPDATE wallet_ledger SET state = 'activating' WHERE wallet_id = ?",
                (self.wallet_id,),
            )

        try:
            used, external = self._read_commitments_history(commitments_path)
            with self._transaction() as connection:
                self._require_wallet_ledger(connection, commitments_path)
                if self._wallet_state_for_connection(connection, self.wallet_id) != "activating":
                    raise MarketStoreCorruptError(
                        "wallet ledger activation state changed unexpectedly"
                    )
                self._import_commitments_history(connection, used, external)
                connection.execute(
                    "UPDATE wallet_ledger SET state = 'ready' WHERE wallet_id = ?",
                    (self.wallet_id,),
                )
        except Exception:
            self._set_recovery_required_after_activation_failure(commitments_path)
            raise

    def _recover_wallet_locked(self, commitments_path: Path) -> None:
        self._verify_schema()
        if self._schema_version(self._connection) != _WALLET_LEDGER_SCHEMA_VERSION:
            raise MarketStoreError("wallet ledger has not been activated")
        self._verify_wallet_ledger(self._connection, commitments_path)
        state = self._wallet_state_for_connection(self._connection, self.wallet_id)
        if state == "ready":
            return
        if state == "disabled":
            raise MarketStoreUnavailableError("wallet ledger must be activated before recovery")
        if state not in ("activating", "recovery_required"):
            raise MarketStoreCorruptError("wallet ledger recovery state is invalid")

        # Persist the fail-closed state before looking at caller-confirmed history.
        with self._transaction() as connection:
            self._verify_schema()
            self._verify_wallet_ledger(connection, commitments_path)
            state = self._wallet_state_for_connection(connection, self.wallet_id)
            if state == "ready":
                return
            if state == "disabled":
                raise MarketStoreUnavailableError("wallet ledger must be activated before recovery")
            if state not in ("activating", "recovery_required"):
                raise MarketStoreCorruptError("wallet ledger recovery state is invalid")
            connection.execute(
                "UPDATE wallet_ledger SET state = 'recovery_required' WHERE wallet_id = ?",
                (self.wallet_id,),
            )

        used, external = self._read_commitments_history(commitments_path, require_existing=True)
        with self._transaction() as connection:
            self._verify_schema()
            self._verify_wallet_ledger(connection, commitments_path)
            if self._wallet_state_for_connection(connection, self.wallet_id) != "recovery_required":
                raise MarketStoreCorruptError("wallet ledger recovery state changed unexpectedly")
            self._import_commitments_history(connection, used, external)
            connection.execute(
                "UPDATE wallet_ledger SET state = 'ready' WHERE wallet_id = ?",
                (self.wallet_id,),
            )

    def _upgrade_to_wallet_ledger(
        self, connection: sqlite3.Connection, commitments_path: Path
    ) -> None:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row["value"] != _LEGACY_SCHEMA_VERSION:
            raise MarketStoreCorruptError(
                "wallet ledger migration does not start from legacy schema"
            )
        connection.execute(
            """
            CREATE TABLE wallet_ledger (
                wallet_id TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (
                    state IN ('disabled', 'activating', 'ready', 'recovery_required')
                )
            )
            """
        )
        connection.execute(
            "ALTER TABLE inventory ADD COLUMN wallet_id TEXT REFERENCES wallet_ledger(wallet_id)"
        )
        connection.execute(
            """
            CREATE TABLE podle_ownership (
                commitment TEXT PRIMARY KEY,
                wallet_id TEXT REFERENCES wallet_ledger(wallet_id),
                state TEXT NOT NULL CHECK (state IN ('held_local', 'used', 'market_inventory')),
                inventory_id INTEGER UNIQUE REFERENCES inventory(id)
            )
            """
        )
        connection.execute("CREATE INDEX wallet_ledger_state ON wallet_ledger(state)")
        connection.execute(
            "UPDATE metadata SET value = ? WHERE key = 'schema_version'",
            (_WALLET_LEDGER_SCHEMA_VERSION,),
        )
        connection.execute(
            "INSERT INTO metadata (key, value) VALUES ('commitments_path', ?)",
            (str(commitments_path),),
        )
        inventory = connection.execute(
            "SELECT id, resource FROM inventory WHERE product = 'podle' ORDER BY id"
        ).fetchall()
        for row in inventory:
            resource = str(row["resource"])
            self._require_hex32(resource, "stored PoDLE inventory resource")
            connection.execute(
                """
                INSERT INTO podle_ownership (commitment, wallet_id, state, inventory_id)
                VALUES (?, NULL, 'market_inventory', ?)
                """,
                (resource, int(row["id"])),
            )

    def _read_commitments_history(
        self, commitments_path: Path, *, require_existing: bool = False
    ) -> tuple[set[str], dict[str, ExternalPoDLE]]:
        if not require_existing and not commitments_path.exists():
            if commitments_path.is_symlink():
                raise MarketStoreCorruptError("commitments path must not be a symlink")
            return set(), {}
        try:
            raw = read_private_file(commitments_path)
            data = json.loads(raw)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise MarketStoreCorruptError("could not read commitments history") from exc
        if not isinstance(data, dict):
            raise MarketStoreCorruptError("commitments history must be a JSON object")
        used = data.get("used")
        external_v1 = data.get("external_v1", {})
        if not isinstance(used, list) or not isinstance(external_v1, dict):
            raise MarketStoreCorruptError("commitments history has an invalid PoDLE section")
        imported_used: set[str] = set()
        for commitment in used:
            try:
                self._require_hex32(commitment, "used commitment")
            except ValueError as exc:
                raise MarketStoreCorruptError(
                    "commitments history has an invalid used commitment"
                ) from exc
            imported_used.add(commitment)
        imported_external: dict[str, ExternalPoDLE] = {}
        for commitment, record_data in external_v1.items():
            if not isinstance(commitment, str) or not isinstance(record_data, dict):
                raise MarketStoreCorruptError("commitments history has an invalid external PoDLE")
            try:
                self._require_hex32(commitment, "external commitment")
                record = ExternalPoDLE.model_validate(record_data)
            except (ValidationError, ValueError) as exc:
                raise MarketStoreCorruptError(
                    "commitments history has an invalid external PoDLE"
                ) from exc
            if record.commitment != commitment:
                raise MarketStoreCorruptError("external PoDLE key does not match its commitment")
            imported_external[commitment] = record
        return imported_used, imported_external

    @staticmethod
    def _require_existing_regular_commitments_history(commitments_path: Path) -> None:
        try:
            history = os.lstat(commitments_path)
        except FileNotFoundError as exc:
            raise MarketStoreError("commitments history must exist for recovery") from exc
        except OSError as exc:
            raise MarketStoreError("could not inspect commitments history for recovery") from exc
        if not stat.S_ISREG(history.st_mode):
            raise MarketStoreError("commitments history must be a regular file for recovery")

    def _import_commitments_history(
        self,
        connection: sqlite3.Connection,
        used: set[str],
        external: dict[str, ExternalPoDLE],
    ) -> None:
        for commitment in sorted(used):
            row = self._ownership_row(connection, commitment)
            if row is not None and row["state"] == "market_inventory":
                raise MarketStoreCorruptError("used commitment collides with market inventory")
            if row is None:
                connection.execute(
                    """
                    INSERT INTO podle_ownership (commitment, wallet_id, state, inventory_id)
                    VALUES (?, NULL, 'used', NULL)
                    """,
                    (commitment,),
                )
            elif row["state"] == "held_local":
                connection.execute(
                    """
                    UPDATE podle_ownership
                    SET state = 'used', inventory_id = NULL
                    WHERE commitment = ?
                    """,
                    (commitment,),
                )
        for commitment in sorted(external):
            if commitment in used:
                continue
            row = self._ownership_row(connection, commitment)
            if row is None:
                connection.execute(
                    """
                    INSERT INTO podle_ownership (commitment, wallet_id, state, inventory_id)
                    VALUES (?, NULL, 'held_local', NULL)
                    """,
                    (commitment,),
                )
                continue
            if row["state"] == "market_inventory":
                raise MarketStoreCorruptError("external PoDLE collides with market inventory")
            if row["state"] == "used":
                continue
            # This shared JSON projection may include another wallet's hold.
            # Its existing authoritative owner remains unchanged across switches.

    def _set_recovery_required_after_activation_failure(self, commitments_path: Path) -> None:
        try:
            with self._transaction() as connection:
                self._require_wallet_ledger(connection, commitments_path)
                self._ensure_wallet_row(connection, self.wallet_id)
                connection.execute(
                    "UPDATE wallet_ledger SET state = 'recovery_required' WHERE wallet_id = ?",
                    (self.wallet_id,),
                )
        except Exception:
            # The committed activating state remains fail-closed if recovery marking cannot persist.
            pass

    def _ownership_row(self, connection: sqlite3.Connection, commitment: str) -> sqlite3.Row | None:
        ownership = cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT commitment, wallet_id, state, inventory_id FROM podle_ownership WHERE commitment = ?",
                (commitment,),
            ).fetchone(),
        )
        if ownership is not None:
            return ownership
        inventory = connection.execute(
            """
            SELECT id FROM inventory
            WHERE product = 'podle' AND resource = ?
            """,
            (commitment,),
        ).fetchone()
        if inventory is not None:
            raise MarketStoreCorruptError("market inventory has no matching PoDLE ownership")
        return None

    @staticmethod
    def _ensure_wallet_row(connection: sqlite3.Connection, wallet_id: str | None) -> None:
        if wallet_id is None:
            return
        connection.execute(
            "INSERT INTO wallet_ledger (wallet_id, state) VALUES (?, 'disabled') ON CONFLICT DO NOTHING",
            (wallet_id,),
        )

    def _ensure_bound_wallet_disabled_row(self, connection: sqlite3.Connection) -> None:
        self._ensure_wallet_row(connection, self.wallet_id)

    @staticmethod
    def _wallet_state_for_connection(
        connection: sqlite3.Connection, wallet_id: str | None
    ) -> Literal["disabled", "activating", "ready", "recovery_required"]:
        if wallet_id is None:
            return "disabled"
        row = connection.execute(
            "SELECT state FROM wallet_ledger WHERE wallet_id = ?", (wallet_id,)
        ).fetchone()
        if row is None:
            return "disabled"
        state = row["state"]
        if state not in ("disabled", "activating", "ready", "recovery_required"):
            raise MarketStoreCorruptError("wallet ledger state is invalid")
        return cast(Literal["disabled", "activating", "ready", "recovery_required"], state)

    def _require_seller_wallet_ready(self, connection: sqlite3.Connection) -> bool:
        if self._schema_version(connection) == _LEGACY_SCHEMA_VERSION:
            return False
        self._require_wallet_ledger(connection)
        if (
            self.wallet_id is None
            or self._wallet_state_for_connection(connection, self.wallet_id) != "ready"
        ):
            raise MarketStoreUnavailableError("wallet ledger is not ready for market issuance")
        return True

    def _require_local_ledger_access(
        self, connection: sqlite3.Connection, *, mutation: bool
    ) -> bool:
        self._require_wallet_ledger(connection)
        if self.wallet_id is not None:
            state = self._wallet_state_for_connection(connection, self.wallet_id)
            if state in ("activating", "recovery_required"):
                if mutation:
                    raise MarketStoreUnavailableError(
                        "wallet ledger is not available for local use"
                    )
                return False
            return True
        blocked = connection.execute(
            """
            SELECT 1 FROM wallet_ledger
            WHERE state IN ('activating', 'recovery_required') LIMIT 1
            """
        ).fetchone()
        if blocked is not None:
            if mutation:
                raise MarketStoreUnavailableError(
                    "wallet identity is required while recovery is pending"
                )
            return False
        return True

    def _require_quote_wallet_ownership(
        self, connection: sqlite3.Connection, quote: sqlite3.Row, wallet_ledger: bool
    ) -> None:
        if not wallet_ledger:
            return
        inventory = connection.execute(
            "SELECT wallet_id FROM inventory WHERE id = ?", (int(quote["inventory_id"]),)
        ).fetchone()
        if inventory is None:
            raise MarketStoreCorruptError("quote inventory is missing")
        if inventory["wallet_id"] not in (None, self.wallet_id):
            raise MarketStoreUnavailableError("quote inventory belongs to a different wallet")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise MarketStoreError("market store is closed")
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                yield self._connection
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
            finally:
                self._harden_permissions()

    def _harden_permissions(self) -> None:
        try:
            os.chmod(self.path, 0o600)
            for suffix in ("-journal", "-wal", "-shm"):
                journal = self.path.with_name(f"{self.path.name}{suffix}")
                if journal.exists():
                    os.chmod(journal, 0o600)
        except OSError as exc:
            raise MarketStoreError("could not secure private market store files") from exc

    @staticmethod
    def _require_int(value: int, name: str, *, minimum: int, maximum: int | None = None) -> None:
        if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
            maximum_message = f" and at most {maximum}" if maximum is not None else ""
            raise ValueError(f"{name} must be an integer at least {minimum}{maximum_message}")

    @staticmethod
    def _require_hex32(value: str, name: str) -> None:
        if not isinstance(value, str) or len(value) != _HEX32_LENGTH:
            raise ValueError(f"{name} must be 32 bytes of lowercase hexadecimal")
        try:
            decoded = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be 32 bytes of lowercase hexadecimal") from exc
        if value != decoded.hex():
            raise ValueError(f"{name} must be 32 bytes of lowercase hexadecimal")

    @staticmethod
    def _require_product(product: Product) -> None:
        if product not in ("podle", "bond"):
            raise ValueError("product must be podle or bond")

    @staticmethod
    def _require_rail(rail: PaymentRail) -> None:
        if rail not in ("onchain", "lightning"):
            raise ValueError("rail must be onchain or lightning")

    @staticmethod
    def _require_opaque_identifier(value: str, name: str, maximum: int) -> None:
        if not isinstance(value, str) or not value or len(value) > maximum or not value.isascii():
            raise ValueError(
                f"{name} must be a non-empty ASCII string no longer than {maximum} bytes"
            )

    def _require_request_id(self, request_id: str) -> None:
        if not isinstance(request_id, str) or len(request_id) != _MAX_REQUEST_ID_LENGTH:
            raise ValueError("request_id must be 16 bytes of lowercase hexadecimal")
        try:
            decoded = bytes.fromhex(request_id)
        except ValueError as exc:
            raise ValueError("request_id must be 16 bytes of lowercase hexadecimal") from exc
        if request_id != decoded.hex():
            raise ValueError("request_id must be 16 bytes of lowercase hexadecimal")

    @staticmethod
    def _require_signed_document(document: SignedDocument, name: str) -> None:
        if not isinstance(document, SignedDocument):
            raise TypeError(f"{name} must be a SignedDocument")

    @staticmethod
    def _seller_pubkey(seller_key: CKey | BoundMarketKeys) -> str:
        if isinstance(seller_key, BoundMarketKeys):
            return seller_key.signing_public_key().hex()
        if isinstance(seller_key, CKey):
            return bytes(seller_key.pub).hex()
        raise TypeError("seller_key must be a CKey or BoundMarketKeys")

    @staticmethod
    def _sign_document(body: MarketModel, seller_key: CKey | BoundMarketKeys) -> SignedDocument:
        if isinstance(seller_key, BoundMarketKeys):
            return seller_key.sign_document(body)
        return sign_document(body, seller_key)

    def _require_bound_seller_wallet(
        self,
        connection: sqlite3.Connection,
        seller_key: CKey | BoundMarketKeys,
        wallet_ledger: bool,
    ) -> None:
        if not isinstance(seller_key, BoundMarketKeys):
            return
        if not wallet_ledger or self.wallet_id is None:
            raise MarketStoreUnavailableError("wallet-bound seller requires a ready wallet ledger")
        if seller_key.wallet_id != self.wallet_id:
            raise MarketStoreConflictError("wallet-bound seller does not match the store wallet")

    @staticmethod
    def _validate_sealed_box_pubkey(value: str) -> None:
        try:
            recipient = PublicKey(bytes.fromhex(value))
            SealedBox(recipient).encrypt(b"")
        except (CryptoError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("buyer_pubkey must be a valid X25519 public key") from exc

    def _validate_certificate_pubkey(
        self, product: Product, certificate_pubkey: str | None
    ) -> None:
        if product == "bond":
            if certificate_pubkey is None:
                raise ValueError("bond quotes require certificate_pubkey")
            self._require_compressed_pubkey(certificate_pubkey, "certificate_pubkey")
        elif certificate_pubkey is not None:
            raise ValueError("podle quotes cannot include certificate_pubkey")

    @staticmethod
    def _require_compressed_pubkey(value: str, name: str) -> None:
        if not isinstance(value, str) or len(value) != 66 or value[:2] not in ("02", "03"):
            raise ValueError(f"{name} must be a compressed lowercase secp256k1 public key")
        try:
            decoded = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a compressed lowercase secp256k1 public key") from exc
        if value != decoded.hex() or not CPubKey(decoded).is_fullyvalid():
            raise ValueError(f"{name} must be a compressed lowercase secp256k1 public key")

    def _validate_inventory_credential(
        self,
        product: Product,
        resource: str,
        credential: dict[str, Any] | None,
    ) -> tuple[bytes | None, str | None]:
        if product == "podle":
            if type(credential) is not dict:
                raise ValueError("podle inventory requires a credential dict")
            try:
                podle = ExternalPoDLE.model_validate(credential)
            except ValidationError as exc:
                raise MarketError("invalid PoDLE inventory credential") from exc
            if podle.commitment != resource:
                raise MarketError("PoDLE inventory resource does not match credential")
            if podle.index >= MARKET_PODLE_RETRIES:
                raise MarketError("Market PoDLE index exceeds the standard maker retry range")
            return canonical(podle), None
        if credential is None:
            return None, None
        if type(credential) is not dict:
            raise TypeError("credential must be a dict or None")
        try:
            bond = BondCredential.model_validate(credential)
            bond.verify()
        except (MarketError, ValidationError, ValueError) as exc:
            raise MarketError("invalid bond inventory credential") from exc
        if resource != bond_resource(bond.bond, bond.cert_expiry - 1):
            raise MarketError("bond inventory resource does not match credential period")
        self._require_compressed_pubkey(bond.cert_pubkey, "bond certificate public key")
        return canonical(bond), bond.cert_pubkey

    def _validate_authorization(
        self,
        authorization: SignedDocument,
        seller_pubkey: str,
        height: int,
    ) -> MarketAuthorization:
        try:
            authority = verify_authorization(authorization)
        except (MarketError, ValidationError, ValueError) as exc:
            raise MarketError("invalid seller authorization") from exc
        self._require_matching_seller(authority, seller_pubkey)
        if authority.period != period_at_height(height):
            raise MarketStoreExpiredError("authorization is from a different retarget period")
        return authority

    @staticmethod
    def _require_matching_seller(authority: MarketAuthorization, seller_pubkey: str) -> None:
        if authority.seller_pubkey != seller_pubkey:
            raise MarketStoreConflictError("seller key does not match authorization")

    def _select_inventory(
        self,
        connection: sqlite3.Connection,
        authority: MarketAuthorization,
        product: Product,
        certificate_pubkey: str | None,
        wallet_ledger: bool,
    ) -> sqlite3.Row | None:
        if product == "podle":
            if wallet_ledger:
                return cast(
                    sqlite3.Row | None,
                    connection.execute(
                        """
                        SELECT inventory.id, inventory.resource, inventory.credential
                        FROM inventory
                        JOIN podle_ownership ON podle_ownership.inventory_id = inventory.id
                        WHERE inventory.product = 'podle' AND inventory.state = 'available'
                          AND podle_ownership.state = 'market_inventory'
                          AND (podle_ownership.wallet_id IS NULL OR podle_ownership.wallet_id = ?)
                        ORDER BY inventory.id
                        LIMIT 1
                        """,
                        (self.wallet_id,),
                    ).fetchone(),
                )
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    """
                    SELECT id, resource, credential FROM inventory
                    WHERE product = 'podle' AND state = 'available'
                    ORDER BY id
                    LIMIT 1
                    """
                ).fetchone(),
            )
        expected_resource = bond_resource(authority.bond, authority.period)
        wallet_clause = ""
        parameters: tuple[str | None, ...] = (expected_resource, certificate_pubkey)
        if wallet_ledger:
            wallet_clause = " AND (wallet_id IS NULL OR wallet_id = ?)"
            parameters = (expected_resource, certificate_pubkey, self.wallet_id)
        return cast(
            sqlite3.Row | None,
            connection.execute(
                f"""
                SELECT id, resource, credential FROM inventory
                WHERE product = 'bond' AND resource = ? AND state = 'available'
                  AND (certificate_pubkey IS NULL OR certificate_pubkey = ?)
                  {wallet_clause}
                ORDER BY id
                LIMIT 1
                """,
                parameters,
            ).fetchone(),
        )

    @staticmethod
    def _expire_live_quotes(connection: sqlite3.Connection, now: int) -> None:
        expired = connection.execute(
            "SELECT quote_id, inventory_id FROM quotes WHERE state = 'live' AND expires_at <= ?",
            (now,),
        ).fetchall()
        for row in expired:
            connection.execute(
                "UPDATE quotes SET state = 'expired' WHERE quote_id = ? AND state = 'live'",
                (str(row["quote_id"]),),
            )
            connection.execute(
                """
                UPDATE inventory SET state = 'available', reservation_quote_id = NULL
                WHERE id = ? AND state = 'reserved' AND reservation_quote_id = ?
                """,
                (int(row["inventory_id"]), str(row["quote_id"])),
            )

    @staticmethod
    def _quote_row(connection: sqlite3.Connection, quote_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute("SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)).fetchone(),
        )

    def _verified_quote_from_row(self, row: sqlite3.Row) -> tuple[MarketAuthorization, MarketQuote]:
        try:
            document = self._signed_document_from_storage(bytes(row["quote_document"]))
            candidate = MarketQuote.model_validate(document.body)
            authority = verify_authorization(candidate.authorization)
            quote = document.verified(MarketQuote, authority.seller_pubkey)
        except (MarketError, ValidationError, ValueError) as exc:
            raise MarketStoreCorruptError("stored quote is not a valid signed document") from exc
        if (
            quote.quote_id != row["quote_id"]
            or quote.buyer_pubkey != row["buyer_pubkey"]
            or quote.product != row["product"]
            or quote.resource != row["resource"]
            or quote.certificate_pubkey != row["certificate_pubkey"]
            or quote.created_at != row["created_at"]
            or quote.expires_at != row["expires_at"]
        ):
            raise MarketStoreCorruptError("stored quote indexes do not match signed quote")
        if quote.product == "bond" and quote.resource != bond_resource(
            authority.bond, authority.period
        ):
            raise MarketStoreCorruptError("stored bond quote resource is invalid")
        return authority, quote

    @staticmethod
    def _allocation_for_quote(authority: MarketAuthorization, quote: MarketQuote) -> Allocation:
        return Allocation(
            authorization=document_hash(quote.authorization.body),
            allocation_id=quote.quote_id,
            buyer_tag=hashlib.sha256(bytes.fromhex(quote.buyer_pubkey)).hexdigest(),
            product=quote.product,
            resource=quote.resource,
            certificate_pubkey=quote.certificate_pubkey,
        )

    @staticmethod
    def _validate_credential_for_allocation(
        authority: MarketAuthorization,
        allocation: Allocation,
        credential: dict[str, Any],
    ) -> None:
        try:
            validate_credential(authority, allocation, credential)
        except (MarketError, ValidationError, ValueError) as exc:
            raise MarketError("credential does not match quote") from exc

    @staticmethod
    def _signed_document_from_storage(value: bytes) -> SignedDocument:
        try:
            return SignedDocument.model_validate(decode_document(value))
        except (MarketError, ValidationError, ValueError) as exc:
            raise MarketStoreCorruptError("stored signed document is invalid") from exc

    @staticmethod
    def _payment_terms_from_storage(value: bytes) -> PaymentTerms:
        try:
            return PaymentTerms.model_validate(decode_document(value))
        except (MarketError, ValidationError, ValueError) as exc:
            raise MarketStoreCorruptError("stored payment terms are invalid") from exc

    @staticmethod
    def _credential_from_storage(value: bytes) -> dict[str, Any]:
        try:
            credential = decode_document(value)
        except MarketError as exc:
            raise MarketStoreCorruptError("stored credential is invalid") from exc
        return credential

    @staticmethod
    def _package_from_storage(value: bytes) -> CredentialPackage:
        try:
            package = CredentialPackage.model_validate(decode_document(value))
            package.verify()
            return package
        except (MarketError, ValidationError, ValueError) as exc:
            raise MarketStoreCorruptError("stored credential package is invalid") from exc

    @staticmethod
    def _seal_package(package: CredentialPackage, buyer_pubkey: str) -> str:
        try:
            recipient = PublicKey(bytes.fromhex(buyer_pubkey))
            ciphertext = SealedBox(recipient).encrypt(canonical(package))
        except (TypeError, ValueError) as exc:
            raise MarketStoreError("could not seal credential package") from exc
        return base64.b64encode(bytes(ciphertext)).decode("ascii")
