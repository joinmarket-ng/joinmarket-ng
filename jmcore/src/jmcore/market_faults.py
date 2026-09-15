"""Bounded cache for signed credential-market fault evidence.

Evidence is deliberately separate from chain verification.  Receiving a signed
fault only establishes that it is internally valid; it becomes a CoinJoin
exclusion only when a currently verified offer proves the same bond claim.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jmcore.credential_market import (
    MAX_MARKET_BYTES,
    BondReference,
    FaultProof,
    MarketAuthorization,
    MarketError,
    bond_resource,
    canonical,
    decode_document,
    period_at_height,
)
from jmcore.secure_files import atomic_write_private, exclusive_file_lock, read_private_file

_MAX_ENTRIES = 256
_MAX_NEGATIVE_HASHES = 128
_MAX_PERSISTED_BYTES = 4 * 1024 * 1024
_MAX_FAILED_VERIFICATIONS_PER_SECOND = 8.0
_PERSISTENCE_NAME = "market_faults.json"
_NETWORKS = frozenset({"mainnet", "testnet", "signet", "regtest"})


@dataclass(frozen=True)
class _FaultEntry:
    """A cryptographically verified proof and its self-authorized bond claim."""

    raw: bytes
    authorization: MarketAuthorization


class MarketFaultCache:
    """Keep a small set of signed fault proofs until a verified offer matches one.

    ``ingest`` never writes to disk.  A proof is persisted only by
    :meth:`excluded_nicks`, after its exact bond metadata has matched a normal
    backend-verified offer at the current certificate period.
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._entries: OrderedDict[str, _FaultEntry] = OrderedDict()
        self._promoted: OrderedDict[str, None] = OrderedDict()
        self._negative_hashes: OrderedDict[str, None] = OrderedDict()
        self._verification_tokens = _MAX_FAILED_VERIFICATIONS_PER_SECOND
        self._last_refill = time.monotonic()
        self._path = Path(data_dir) / _PERSISTENCE_NAME if data_dir is not None else None
        self._load_persisted()

    def __len__(self) -> int:
        """Return the number of verified, in-memory fault resources."""
        return len(self._entries)

    def ingest(self, raw: bytes) -> bool:
        """Verify and retain one canonical fault proof without persisting it.

        Malformed, duplicate-key, non-canonical, oversized, and invalidly signed
        documents are rejected.  A monotonic token bucket limits signature-heavy
        failures before ``FaultProof.verify`` is reached.
        """
        return self._ingest(raw, rate_limit=True, promoted=False)

    def excluded_nicks(
        self,
        offers: Iterable[Any],
        *,
        network: str,
        height: int,
    ) -> set[str]:
        """Return nicks sharing a sanctioned, independently verified bond.

        The offer must have been successfully verified by the normal bond path.
        A zero current bond weight is not an exclusion bypass, but an unverified
        bond never becomes tainted merely because its outpoint was claimed in a
        signed market authorization.
        """
        try:
            current_period = period_at_height(height)
        except MarketError:
            return set()
        if network not in _NETWORKS:
            return set()

        excluded: set[str] = set()
        matched_resources: list[str] = []
        cached_offers = tuple(offers)
        for resource, entry in tuple(self._entries.items()):
            bond = entry.authorization.bond
            if bond.network != network or current_period not in (
                entry.authorization.period,
                entry.authorization.period + 1,
            ):
                continue
            matched = False
            for offer in cached_offers:
                if not self._matches_verified_offer(offer, bond):
                    continue
                nick = getattr(offer, "counterparty", None)
                if isinstance(nick, str):
                    excluded.add(nick)
                    matched = True
            if matched:
                self._entries.move_to_end(resource)
                matched_resources.append(resource)

        for resource in matched_resources:
            self._promote(resource)
        return excluded

    def excludes_verified_bond(self, bond: BondReference, *, height: int) -> bool:
        """Check collateral after the caller independently verifies its chain UTXO."""
        period = period_at_height(height)
        for fault_period in (period, period - 1):
            if fault_period < 0:
                continue
            resource = bond_resource(bond, fault_period)
            entry = self._entries.get(resource)
            if entry is not None and entry.authorization.bond == bond:
                self._promote(resource)
                return True
        return False

    def _ingest(
        self,
        raw: bytes,
        *,
        rate_limit: bool,
        promoted: bool,
        expected_network: str | None = None,
    ) -> bool:
        if not isinstance(raw, bytes) or len(raw) > MAX_MARKET_BYTES:
            return False

        raw_hash = hashlib.sha256(raw).hexdigest()
        if raw_hash in self._negative_hashes:
            self._negative_hashes.move_to_end(raw_hash)
            return False

        try:
            document = decode_document(raw)
            if (
                document.get("kind") != "fault"
                or type(document.get("version")) is not int
                or document.get("version") != 1
            ):
                raise MarketError("Unsupported fault proof version or kind")
            if canonical(document) != raw:
                raise MarketError("Market fault proof is not canonical")
            proof = FaultProof.model_validate(document)
        except Exception:
            self._remember_negative(raw_hash)
            return False

        if rate_limit and not self._take_verification_slot():
            return False
        try:
            authorization = proof.verify()
        except Exception:
            self._remember_negative(raw_hash)
            return False
        if expected_network is not None and authorization.bond.network != expected_network:
            self._remember_negative(raw_hash)
            return False

        resource = bond_resource(authorization.bond, authorization.period)
        if resource in self._entries:
            self._entries.move_to_end(resource)
        else:
            if len(self._entries) >= _MAX_ENTRIES:
                evictable = next((key for key in self._entries if key not in self._promoted), None)
                if evictable is None:
                    return False
                del self._entries[evictable]
            self._entries[resource] = _FaultEntry(raw=raw, authorization=authorization)
        if promoted:
            self._promoted[resource] = None
        return True

    def _take_verification_slot(self) -> bool:
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        self._verification_tokens = min(
            _MAX_FAILED_VERIFICATIONS_PER_SECOND,
            self._verification_tokens + elapsed * _MAX_FAILED_VERIFICATIONS_PER_SECOND,
        )
        if self._verification_tokens < 1:
            return False
        self._verification_tokens -= 1
        return True

    def _remember_negative(self, raw_hash: str) -> None:
        self._negative_hashes[raw_hash] = None
        self._negative_hashes.move_to_end(raw_hash)
        while len(self._negative_hashes) > _MAX_NEGATIVE_HASHES:
            self._negative_hashes.popitem(last=False)

    @staticmethod
    def _matches_verified_offer(offer: Any, bond: BondReference) -> bool:
        if getattr(offer, "fidelity_bond_verified", None) is not True:
            return False
        data = getattr(offer, "fidelity_bond_data", None)
        if not isinstance(data, dict):
            return False
        txid = data.get("utxo_txid")
        vout = data.get("utxo_vout")
        pubkey = data.get("utxo_pub")
        locktime = data.get("locktime")
        return (
            isinstance(txid, str)
            and txid == bond.outpoint.txid
            and type(vout) is int
            and vout == bond.outpoint.vout
            and isinstance(pubkey, str)
            and pubkey == bond.pubkey
            and type(locktime) is int
            and locktime == bond.locktime
        )

    def _promote(self, resource: str) -> None:
        if resource not in self._entries:
            return
        if resource in self._promoted:
            self._promoted.move_to_end(resource)
            return
        self._promoted[resource] = None
        self._promoted.move_to_end(resource)
        self._persist()

    def _load_persisted(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = read_private_file(self._path)
            if len(raw) > _MAX_PERSISTED_BYTES:
                return
            document = json.loads(raw.decode("ascii"), object_pairs_hook=self._unique_object)
            if not isinstance(document, dict) or set(document) != {"proofs", "version"}:
                return
            if document["version"] != 1 or not isinstance(document["proofs"], dict):
                return
            for network, values in document["proofs"].items():
                if network not in _NETWORKS or not isinstance(values, list):
                    continue
                for encoded in values:
                    proof = self._decode_persisted_proof(encoded)
                    if proof is not None:
                        self._ingest(
                            proof,
                            rate_limit=False,
                            promoted=True,
                            expected_network=network,
                        )
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            return

    def _persist(self) -> None:
        if self._path is None:
            return
        try:
            with exclusive_file_lock(self._path.with_suffix(".lock")):
                self._load_persisted()
                self._write_snapshot()
        except OSError:
            return

    def _write_snapshot(self) -> None:
        if self._path is None:
            return
        proofs: dict[str, list[str]] = {}
        for resource in self._promoted:
            entry = self._entries.get(resource)
            if entry is None:
                continue
            network = entry.authorization.bond.network
            proofs.setdefault(network, []).append(base64.b64encode(entry.raw).decode("ascii"))
        try:
            encoded = json.dumps(
                {"version": 1, "proofs": proofs},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
            if len(encoded) <= _MAX_PERSISTED_BYTES:
                atomic_write_private(self._path, encoded)
        except OSError:
            return

    @staticmethod
    def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    @staticmethod
    def _decode_persisted_proof(value: Any) -> bytes | None:
        if not isinstance(value, str) or not value.isascii():
            return None
        try:
            raw = base64.b64decode(value, validate=True)
        except ValueError:
            return None
        if len(raw) > MAX_MARKET_BYTES or base64.b64encode(raw).decode("ascii") != value:
            return None
        return raw
