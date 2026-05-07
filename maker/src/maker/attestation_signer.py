"""Maker-side bond-attestation signer (JMP-0006 §"Bond Attestation").

A round-0 maker that wants to be eligible for inclusion in a taker's
``!cjext`` anonymity set holds the secret key controlling its bond
UTXO and signs CLSAG ring signatures on demand. JMP-0006 imposes a
hard rule: a maker MUST NOT sign more than one attestation per
``(run_id, round_no)``, otherwise its key image (deterministic within
a run) appears twice and the whole attestation is rejected as a Sybil
attempt by every late-joining verifier.

This module owns that invariant. It is intentionally narrow:

  * Exactly one CLSAG-eligible secret key per signer instance, with
    parity normalized to even-Y at construction time so the cached
    x-only pubkey matches the BIP340 lift of every ring.

  * A per-process ``(run_id, round_no) -> key_image`` cache rejects
    duplicate ``!attestreq`` for the same round, even if the requested
    ring differs (the key image is ring-independent).

  * The cache is bounded; entries are dropped LRU-style once a soft
    cap is hit, so a long-lived maker process doesn't accumulate
    state across thousands of runs. Stale entries are also evicted
    via :meth:`AttestationSigner.forget_run` once the maker observes
    the run terminate, but the LRU bound is the safety net.

The cryptographic primitive itself (``sign_ring``, ``compute_key_image``)
lives in ``jmcore.clsag_attestation``; this module only orchestrates
the duplicate-detection invariant and the parity-normalization detail
that the maker's wallet key isn't guaranteed to land on an even-Y
point.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Final

from coincurve import PrivateKey
from jmcore.clsag_attestation import RingMember, compute_key_image, sign_ring

# JMP-0006 doesn't pin a process-wide cache size; this is a defensive
# upper bound. A maker servicing 16 takers x 16 rounds at peak would
# fit comfortably; legitimate usage stays well below.
_DEFAULT_CACHE_LIMIT: Final[int] = 4096

# secp256k1 group order, used to negate odd-Y secrets so the cached
# x-only pubkey lifts back to the same point as `secret * G`.
_SECP256K1_N: Final[int] = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _normalize_to_even_y(secret_key: bytes) -> tuple[bytes, bytes]:
    """Return ``(secret_key_even_y, x_only_pubkey)``.

    BIP340 / CLSAG rings carry x-only pubkeys whose implicit y is even.
    A wallet-derived secret has 50/50 parity; if its public point has
    odd y we negate the scalar so the public point flips to even y.
    The caller then advertises the resulting 32-byte x-only pubkey
    in the orderbook and uses the (possibly negated) secret to sign.
    """
    if len(secret_key) != 32:
        raise ValueError(f"secret_key must be 32 bytes, got {len(secret_key)}")
    pk = PrivateKey(secret_key).public_key.format(compressed=True)
    if pk[0] == 0x02:
        return secret_key, pk[1:]
    flipped = (_SECP256K1_N - int.from_bytes(secret_key, "big")) % _SECP256K1_N
    sk2 = flipped.to_bytes(32, "big")
    return sk2, pk[1:]


@dataclass(frozen=True)
class DuplicateAttestationError(Exception):
    """Raised when an ``!attestreq`` reuses a previously-served ``(run_id, round_no)``.

    Carries the cached key image so the protocol handler can include
    it in the rejection (a benign caller can use it to dedupe its own
    state; a malicious caller learns nothing it couldn't observe by
    asking another participant of the same run).
    """

    run_id: bytes
    round_no: int
    key_image: bytes

    def __str__(self) -> str:  # pragma: no cover - exercised via __repr__
        return (
            f"duplicate attestation for run {self.run_id.hex()[:16]}.. round {self.round_no}; "
            f"key_image={self.key_image.hex()[:16]}.."
        )


class AttestationSigner:
    """Holds one bond key and serves CLSAG attestations under JMP-0006.

    Thread-safety: not thread-safe; the maker bot dispatches PRIVMSGs
    serially from the IRC event loop, so a per-instance lock would
    only buy us misleading robustness. If a future caller wants
    concurrent signing it must wrap this class with a lock.
    """

    def __init__(
        self,
        secret_key: bytes,
        *,
        cache_limit: int = _DEFAULT_CACHE_LIMIT,
    ) -> None:
        if cache_limit < 1:
            raise ValueError(f"cache_limit must be >= 1, got {cache_limit}")
        self._secret_key, self._pubkey_xonly = _normalize_to_even_y(secret_key)
        self._cache_limit = cache_limit
        # Insertion-ordered for cheap LRU eviction.
        self._cache: OrderedDict[tuple[bytes, int], bytes] = OrderedDict()

    @classmethod
    def from_coincurve_private_key(
        cls,
        private_key: PrivateKey,
        *,
        cache_limit: int = _DEFAULT_CACHE_LIMIT,
    ) -> AttestationSigner:
        """Convenience constructor: accepts the coincurve key the wallet hands out."""
        return cls(private_key.secret, cache_limit=cache_limit)

    @property
    def pubkey_xonly(self) -> bytes:
        """The 32-byte BIP340 x-only pubkey advertised in the orderbook."""
        return self._pubkey_xonly

    def key_image_for(self, run_id: bytes) -> bytes:
        """Cheap pre-flight: what key image will this signer expose in ``run_id``?

        The maker can hand this back in a refused ``!attestreq`` (e.g.
        when the requested round is already cached) so the requester
        can correlate without paying to build a full ring signature.
        """
        return compute_key_image(self._secret_key, run_id)

    def sign_attestation(
        self,
        *,
        ring: list[RingMember],
        signer_idx: int,
        run_id: bytes,
        round_no: int,
    ) -> bytes:
        """Produce one CLSAG ring signature, enforcing the per-round invariant.

        Raises :class:`DuplicateAttestationError` if a different ring
        for the same ``(run_id, round_no)`` was already signed; the
        cache is keyed on ``(run_id, round_no)`` precisely so two
        rings for the same round both fail (per JMP-0006).
        """
        if ring[signer_idx].pubkey_xonly != self._pubkey_xonly:
            raise ValueError(
                "ring[signer_idx] does not match this signer's bond pubkey; "
                "the taker built a ring without our advertised x-only key"
            )
        key = (run_id, round_no)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            raise DuplicateAttestationError(run_id=run_id, round_no=round_no, key_image=cached)

        sig = sign_ring(
            secret_key=self._secret_key,
            ring=ring,
            signer_idx=signer_idx,
            run_id=run_id,
            round_no=round_no,
        )
        # The first 33 bytes of a CLSAG signature are the (compressed)
        # key image; cache that rather than recomputing it.
        key_image = sig[:33]
        self._cache[key] = key_image
        if len(self._cache) > self._cache_limit:
            self._cache.popitem(last=False)
        return sig

    def forget_run(self, run_id: bytes) -> int:
        """Drop every cached entry for ``run_id``.

        Returns the number of entries removed. Intended to be called
        when the maker observes the run finalize (``!txfreeze`` /
        ``!sigfinal``) or abort, so cache size stays proportional to
        in-flight runs rather than to lifetime workload.
        """
        to_drop = [k for k in self._cache if k[0] == run_id]
        for k in to_drop:
            del self._cache[k]
        return len(to_drop)

    def cache_size(self) -> int:
        """Test/observability hook: current cache occupancy."""
        return len(self._cache)


__all__ = [
    "AttestationSigner",
    "DuplicateAttestationError",
]
