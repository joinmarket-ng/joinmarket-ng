"""Durable local seller state for the native credential market.

This module deliberately has no network, wallet, or payment-backend dependency.
Callers validate settlement before calling :meth:`MarketStore.finalize`; this
store only makes the resulting allocation and delivery irreversible.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

from bitcointx.core.key import CKey, CPubKey  # type: ignore[import-not-found]
from jmcore.credential_market import (
    MARKET_PODLE_RETRIES,
    Allocation,
    BondCredential,
    CredentialPackage,
    Delivery,
    MarketAuthorization,
    MarketError,
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
from jmcore.secure_files import exclusive_file_lock
from nacl.exceptions import CryptoError
from nacl.public import PublicKey, SealedBox
from pydantic import ValidationError

Product = Literal["podle", "bond"]
PaymentRail = Literal["onchain", "lightning"]

_SCHEMA_VERSION = "1"
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

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        if path.is_symlink():
            raise MarketStoreError("market store path must not be a symlink")

        self.path = path
        self._closed = False
        self._lock = threading.RLock()
        self._prepare_parent()
        created = self._create_private_database_file()
        try:
            self._connection = sqlite3.connect(
                self.path,
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
        except (OSError, sqlite3.DatabaseError) as exc:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise MarketStoreCorruptError("could not open private market store") from exc

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
            existing = connection.execute(
                "SELECT id FROM inventory WHERE product = ? AND resource = ?",
                (product, resource),
            ).fetchone()
            if existing is not None:
                raise MarketStoreConflictError("inventory resource has already been recorded")
            connection.execute(
                """
                INSERT INTO inventory (
                    product, resource, credential, certificate_pubkey, state,
                    reservation_quote_id
                ) VALUES (?, ?, ?, ?, 'available', NULL)
                """,
                (product, resource, encoded_credential, certificate_pubkey),
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
        seller_key: CKey,
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
            self._expire_live_quotes(connection, now)
            existing = connection.execute(
                "SELECT request_fingerprint, quote_document FROM quotes WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if bytes(existing["request_fingerprint"]) != fingerprint:
                    raise MarketStoreConflictError(
                        "request_id is bound to different quote parameters"
                    )
                return self._signed_document_from_storage(bytes(existing["quote_document"]))

            authority = self._validate_authorization(authorization, seller_pubkey, height)
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
            quote_document = sign_document(quote, seller_key)
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
            row = self._quote_row(connection, quote_id)
            if row is None:
                raise MarketStoreUnavailableError("quote does not exist")
            if row["state"] != "live":
                raise MarketStoreExpiredError("quote is no longer live")
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
        seller_key: CKey,
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
            row = self._quote_row(connection, quote_id)
            if row is None:
                raise MarketStoreUnavailableError("quote does not exist")
            authority, quote = self._verified_quote_from_row(row)
            self._require_matching_seller(authority, seller_pubkey)
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
            allocation_document = sign_document(allocation, seller_key)
            delivery_document = sign_document(
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
        with exclusive_file_lock(lock_path):
            with self._lock:
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

    def _verify_schema(self) -> None:
        integrity = self._connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise MarketStoreCorruptError("market store integrity check failed")
        tables = {
            str(row["name"])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"metadata", "inventory", "payments", "quotes"}.issubset(tables):
            raise MarketStoreCorruptError("market store schema is missing")
        row = self._connection.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row["value"] != _SCHEMA_VERSION:
            raise MarketStoreCorruptError("unsupported market store schema")

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
    def _seller_pubkey(seller_key: CKey) -> str:
        if not isinstance(seller_key, CKey):
            raise TypeError("seller_key must be a CKey")
        return bytes(seller_key.pub).hex()

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
    ) -> sqlite3.Row | None:
        if product == "podle":
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
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT id, resource, credential FROM inventory
                WHERE product = 'bond' AND resource = ? AND state = 'available'
                  AND (certificate_pubkey IS NULL OR certificate_pubkey = ?)
                ORDER BY id
                LIMIT 1
                """,
                (expected_resource, certificate_pubkey),
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
