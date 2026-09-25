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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from jmcore.credential_market import (
    MAX_MARKET_BYTES,
    BondReference,
    ConflictingCertificate,
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
_MAX_DEFERRED_VERIFICATIONS = 64
_PERSISTENCE_NAME = "market_faults.json"
_NETWORKS = frozenset({"mainnet", "testnet", "signet", "regtest"})


@dataclass(frozen=True)
class _FaultEntry:
    """A cryptographically verified proof and its self-authorized bond claim.

    ``conflict`` marks evidence that is only meaningful while the rented period
    is current; ``observed_height`` records the block height at which this node
    itself saw the accused certificate in a live, verified offer.
    """

    raw: bytes
    authorization: MarketAuthorization
    conflict: ConflictingCertificate | None = None
    observed_height: int | None = None

    @property
    def corroborated(self) -> bool:
        """Report whether the entry may be applied as a sanction right now."""
        return self.conflict is None or self.observed_height is not None


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
        # Well-formed proofs that arrived while the verification budget was
        # spent. They are retried first, so a burst of invalid proofs delays
        # valid evidence instead of dropping it.
        self._deferred: OrderedDict[str, bytes] = OrderedDict()
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
        failures before ``FaultProof.verify`` is reached. Well-formed proofs over
        the budget are queued (bounded, oldest kept) and retried before later
        input; ``False`` then means "not verified yet".
        """
        while self._deferred and self._verification_available():
            _hash, deferred = self._deferred.popitem(last=False)
            self._ingest(deferred, rate_limit=True, promoted=False)
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
            if not entry.corroborated:
                entry = self._corroborate(resource, entry, cached_offers, current_period, height)
                if not entry.corroborated:
                    continue
            if entry.observed_height is not None and entry.observed_height > height:
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

    def _corroborate(
        self,
        resource: str,
        entry: _FaultEntry,
        offers: tuple[Any, ...],
        current_period: int,
        height: int,
    ) -> _FaultEntry:
        """Upgrade a conflict candidate only from a live offer seen inside the lease.

        Certificates carry no activation height, so a certificate that is
        perfectly valid today may simply have been issued after the rented
        period ended.  Only this node observing the accused certificate in a
        currently advertised, backend-verified offer while the rented period is
        the current one rules that out.  A proof that first arrives afterwards
        stays inert forever.
        """
        conflict = entry.conflict
        if conflict is None or current_period != entry.authorization.period:
            return entry
        if not any(
            self._matches_verified_offer(offer, entry.authorization.bond)
            and self._matches_conflicting_certificate(offer, conflict)
            for offer in offers
        ):
            return entry
        observed = replace(entry, observed_height=height)
        self._entries[resource] = observed
        return observed

    def excludes_verified_bond(self, bond: BondReference, *, height: int) -> bool:
        """Check collateral after the caller independently verifies its chain UTXO.

        A conflict candidate counts only once this node already corroborated it
        against a live offer; a bare bond reference never promotes one.
        """
        period = period_at_height(height)
        periods = {value for value in (period, period - 1) if value >= 0}
        match: str | None = None
        for resource, entry in self._entries.items():
            if (
                entry.corroborated
                and (entry.observed_height is None or entry.observed_height <= height)
                and entry.authorization.bond == bond
                and entry.authorization.period in periods
            ):
                match = resource
                break
        if match is None:
            return False
        self._promote(match)
        return True

    def _ingest(
        self,
        raw: bytes,
        *,
        rate_limit: bool,
        promoted: bool,
        expected_network: str | None = None,
        observed_height: int | None = None,
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
            if raw_hash not in self._deferred and (
                len(self._deferred) < _MAX_DEFERRED_VERIFICATIONS
            ):
                self._deferred[raw_hash] = raw
            return False
        try:
            authorization, conflict = proof.verify_detailed()
        except Exception:
            self._remember_negative(raw_hash)
            return False
        if expected_network is not None and authorization.bond.network != expected_network:
            self._remember_negative(raw_hash)
            return False
        if observed_height is not None and not self._observation_is_credible(
            conflict, authorization, observed_height
        ):
            return False

        resource = bond_resource(authorization.bond, authorization.period)
        if conflict is not None:
            # Keep unproven conflict candidates out of the shared bond identity so
            # they can neither hide nor evict directly usable fault evidence.
            resource = f"{resource}:{raw_hash}"
        if resource in self._entries:
            existing = self._entries[resource]
            if observed_height is not None and (
                existing.observed_height is None or observed_height < existing.observed_height
            ):
                self._entries[resource] = replace(existing, observed_height=observed_height)
            self._entries.move_to_end(resource)
        else:
            if len(self._entries) >= _MAX_ENTRIES:
                evictable = next(
                    (
                        key
                        for key, cached in self._entries.items()
                        if key not in self._promoted and cached.conflict is not None
                    ),
                    None,
                )
                if evictable is None and conflict is None:
                    evictable = next(
                        (key for key in self._entries if key not in self._promoted), None
                    )
                if evictable is None:
                    return False
                del self._entries[evictable]
            self._entries[resource] = _FaultEntry(
                raw=raw,
                authorization=authorization,
                conflict=conflict,
                observed_height=observed_height,
            )
        if promoted:
            self._promoted[resource] = None
        return True

    @staticmethod
    def _observation_is_credible(
        conflict: ConflictingCertificate | None,
        authorization: MarketAuthorization,
        observed_height: int,
    ) -> bool:
        """Accept a restored observation only if it was recorded inside the lease."""
        if conflict is None or type(observed_height) is not int:
            return False
        try:
            return period_at_height(observed_height) == authorization.period
        except MarketError:
            return False

    def _verification_available(self) -> bool:
        now = time.monotonic()
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        self._verification_tokens = min(
            _MAX_FAILED_VERIFICATIONS_PER_SECOND,
            self._verification_tokens + elapsed * _MAX_FAILED_VERIFICATIONS_PER_SECOND,
        )
        return self._verification_tokens >= 1

    def _take_verification_slot(self) -> bool:
        if not self._verification_available():
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

    @staticmethod
    def _matches_conflicting_certificate(offer: Any, conflict: ConflictingCertificate) -> bool:
        data = getattr(offer, "fidelity_bond_data", None)
        if not isinstance(data, dict):
            return False
        cert_pub = data.get("cert_pub")
        cert_expiry = data.get("cert_expiry")
        return (
            isinstance(cert_pub, str)
            and cert_pub == conflict.cert_pubkey
            and type(cert_expiry) is int
            and cert_expiry == conflict.cert_expiry_height
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
            if not isinstance(document, dict) or set(document) != {
                "observations",
                "proofs",
                "version",
            }:
                return
            if document.get("version") != 1 or not isinstance(document.get("proofs"), dict):
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
            self._load_observations(document["observations"])
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            return

    def _load_observations(self, observations: Any) -> None:
        """Restore locally corroborated conflicts; anything unclear stays absent.

        A missing or malformed record is never treated as evidence that a
        conflict was witnessed. Incomplete local state loses the sanction
        instead of inventing one.
        """
        if not isinstance(observations, dict):
            return
        for network, values in observations.items():
            if network not in _NETWORKS or not isinstance(values, list):
                continue
            for record in values:
                if not isinstance(record, dict) or set(record) != {"height", "proof"}:
                    continue
                height = record["height"]
                proof = self._decode_persisted_proof(record["proof"])
                if proof is None or type(height) is not int:
                    continue
                self._ingest(
                    proof,
                    rate_limit=False,
                    promoted=True,
                    expected_network=network,
                    observed_height=height,
                )

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
        observations: dict[str, list[dict[str, Any]]] = {}
        for resource in self._promoted:
            entry = self._entries.get(resource)
            if entry is None:
                continue
            network = entry.authorization.bond.network
            encoded_proof = base64.b64encode(entry.raw).decode("ascii")
            if entry.conflict is not None:
                if entry.observed_height is None:
                    continue
                observations.setdefault(network, []).append(
                    {"height": entry.observed_height, "proof": encoded_proof}
                )
            else:
                proofs.setdefault(network, []).append(encoded_proof)
        try:
            encoded = json.dumps(
                {"version": 1, "proofs": proofs, "observations": observations},
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
