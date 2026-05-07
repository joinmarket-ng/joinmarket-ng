"""Taker-side CLSAG bond-attestation collector (JMP-0006).

A ZKP-mode taker that wants to publish ``!cjext`` first builds an
anonymity set of N maker bond keys (drawn from the orderbook) and
then collects K independent CLSAG ring signatures over that exact
ring, each produced by a *different* maker holding a corresponding
secret key. The taker then packs the N pubkeys + N outpoints + K
signatures into the ``bond_attestation_b64`` field carried by
``!cjext``.

This module implements that collection step as a pure async routine
with the IRC transport injected as a callable. The contract:

  * Caller hands in N candidates (``Candidate``: nick, x-only pubkey,
    outpoint) and a target ``k`` plus optional ``slack`` to overshoot
    the K candidates contacted (so a small number of refusals or
    timeouts don't sink the run).

  * Caller injects ``send_attestreq``: an async callable taking the
    candidate's nick and the request bytes/dict and returning the
    maker's response (or raising on timeout). This module never
    speaks IRC directly.

  * The collector verifies every returned signature against the
    advertised ring and discards malformed / wrong-key-image / wrong-
    signer-index responses. Duplicate key images across responses
    indicate a misbehaving maker (or coordinated Sybil) and are
    rejected at collection time so the resulting :class:`Attestation`
    always satisfies :func:`verify_attestation`'s uniqueness check.

  * On success the collector returns a ready-to-pack
    :class:`Attestation`. On failure (insufficient distinct valid
    signers within the budget) it raises
    :class:`InsufficientAttestationsError` carrying both the partial
    set and the per-candidate failure reasons so the taker can log a
    diagnostic and either retry with a larger N or fall back.

Variant (a) of JMP-0006 is implemented: the full ring is sent
upfront in every ``!attestreq`` so each signer signs over the same N
pubkeys. This is the simplest contract and matches what
``sign_ring`` requires; the privacy tradeoff (each contacted maker
learns the full anonymity set) is acceptable because the orderbook
is public anyway.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Final

from jmcore.clsag_attestation import (
    Attestation,
    RingMember,
    verify_ring,
)

# Default fan-out slack: contact ``k + ceil(k/2)`` candidates in the
# first wave so a handful of refusals/timeouts don't force a second
# round-trip. Bounded by N anyway.
_DEFAULT_SLACK_FRACTION: Final[float] = 0.5

# Per-attestreq deadline. The maker only has to compute one CLSAG, so
# anything longer than a few seconds is almost certainly a hung peer.
DEFAULT_PER_REQ_TIMEOUT_S: Final[float] = 8.0


@dataclass(frozen=True)
class Candidate:
    """One member of the anonymity set drawn from the orderbook."""

    nick: str
    pubkey_xonly: bytes  # 32 bytes, BIP340 x-only
    outpoint: bytes  # 36 bytes, txid LE || vout LE32

    def to_ring_member(self) -> RingMember:
        return RingMember(pubkey_xonly=self.pubkey_xonly, outpoint=self.outpoint)


@dataclass(frozen=True)
class AttestRequest:
    """The taker -> maker request payload built by the collector.

    Wire encoding lives in the protocol layer; this dataclass is the
    structured form the IRC sender renders. ``signer_idx`` tells the
    target maker which ring slot belongs to it (it is also redundant
    with the maker's own pubkey but lets the maker fail fast on a
    mis-built ring).
    """

    run_id: bytes
    round_no: int
    ring: tuple[RingMember, ...]
    signer_idx: int


@dataclass(frozen=True)
class AttestResponse:
    """The maker -> taker response. ``signature`` is the raw 33+32+32*N CLSAG sig."""

    signature: bytes


# Sentinel exception types for inversion of control with the transport layer.
class AttestationTransportError(Exception):
    """Raised by the injected ``send_attestreq`` to signal a transport-level
    failure (timeout, peer disconnect, malformed wire reply). The
    collector treats these uniformly as "this candidate didn't
    answer" and moves on; cryptographic verification failures are a
    separate path (the response is structurally valid but doesn't
    verify, indicating a buggy or malicious maker)."""


@dataclass(frozen=True)
class CandidateFailure:
    """Why one candidate didn't contribute a usable signature."""

    nick: str
    reason: str  # short stable code, e.g. "timeout", "verify_failed"
    detail: str = ""


class InsufficientAttestationsError(Exception):
    """Raised when fewer than ``k`` distinct valid signatures were
    collected within the candidate budget.

    Carries the partial attestation and per-candidate failure log so
    the taker can decide whether to retry or fall back to plain
    coinjoin. Holding the partial attestation around is intentional:
    a future retry can salvage the signatures it already has rather
    than re-asking the same makers (and hitting their per-round
    cache).
    """

    partial: Attestation
    failures: tuple[CandidateFailure, ...]

    def __init__(self, partial: Attestation, failures: tuple[CandidateFailure, ...]) -> None:
        super().__init__(
            f"got {partial.k} valid attestations, needed {partial.k + 1}+; "
            f"{len(failures)} candidate failure(s)"
        )
        # dataclass-style init avoided so we can set attrs after super().__init__
        object.__setattr__(self, "partial", partial)
        object.__setattr__(self, "failures", failures)


# Type of the transport callable the caller injects.
SendAttestReq = Callable[[str, AttestRequest], Awaitable[AttestResponse]]


@dataclass
class _CollectionState:
    """Mutable bookkeeping kept off the public surface."""

    accepted: list[tuple[int, bytes]] = field(default_factory=list)
    """``(candidate_idx, signature)`` for each verified contribution.

    The candidate_idx (== ring slot) is preserved so the final
    :class:`Attestation` can sort signatures into the deterministic
    order verifiers expect: ascending by ring slot. Without that, two
    different collection orderings would produce different wire blobs
    for the same logical attestation.
    """

    seen_key_images: set[bytes] = field(default_factory=set)
    """Reject a maker that somehow returns a key image already produced
    by another candidate. JMP-0006 guarantees uniqueness across honest
    distinct makers; collision means either a maker is impersonating
    another's bond key or an honest maker was contacted twice — either
    way, drop the second."""

    failures: list[CandidateFailure] = field(default_factory=list)


async def collect_attestations(
    *,
    candidates: list[Candidate],
    k: int,
    run_id: bytes,
    round_no: int,
    send_attestreq: SendAttestReq,
    slack_fraction: float = _DEFAULT_SLACK_FRACTION,
    per_request_timeout_s: float = DEFAULT_PER_REQ_TIMEOUT_S,
) -> Attestation:
    """Collect ``k`` valid CLSAG attestations over ``candidates``.

    Strategy: fire ``min(N, k + ceil(k * slack_fraction))`` requests in
    the first wave, then top up with sequential retries against the
    untried candidates until either ``k`` valid signatures are in or
    the candidate pool is exhausted.

    The first-wave fan-out is parallel because the per-maker cost is
    O(1) CLSAG sign; serializing would just stretch latency by a
    factor of N for no benefit.
    """
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    if len(candidates) < k:
        raise ValueError(f"need at least k={k} candidates, got {len(candidates)}")

    n = len(candidates)
    ring = tuple(c.to_ring_member() for c in candidates)
    state = _CollectionState()

    # First wave: contact k + slack candidates concurrently. Cap at N.
    first_wave_size = min(n, k + max(1, int(k * slack_fraction)))
    first_wave_indices = list(range(first_wave_size))
    backup_indices = list(range(first_wave_size, n))

    await _run_wave(
        wave_indices=first_wave_indices,
        candidates=candidates,
        ring=ring,
        run_id=run_id,
        round_no=round_no,
        send_attestreq=send_attestreq,
        per_request_timeout_s=per_request_timeout_s,
        target_k=k,
        state=state,
    )

    # Sequential top-up: cheaper than another big concurrent wave when
    # only a couple of slots are missing, and avoids burning the entire
    # backup pool when one extra signature is enough.
    while len(state.accepted) < k and backup_indices:
        idx = backup_indices.pop(0)
        await _try_one(
            cand_idx=idx,
            candidate=candidates[idx],
            ring=ring,
            run_id=run_id,
            round_no=round_no,
            send_attestreq=send_attestreq,
            per_request_timeout_s=per_request_timeout_s,
            state=state,
        )

    # Sort by ring slot for canonical output ordering.
    state.accepted.sort(key=lambda pair: pair[0])
    sigs = [sig for _, sig in state.accepted]

    if len(sigs) < k:
        partial = Attestation(ring=list(ring), ring_signatures=sigs)
        raise InsufficientAttestationsError(partial=partial, failures=tuple(state.failures))

    # Trim to exactly k (in case slack produced more than asked) so the
    # caller's K matches what the orderbook advertised.
    sigs = sigs[:k]
    return Attestation(ring=list(ring), ring_signatures=sigs)


async def _run_wave(
    *,
    wave_indices: list[int],
    candidates: list[Candidate],
    ring: tuple[RingMember, ...],
    run_id: bytes,
    round_no: int,
    send_attestreq: SendAttestReq,
    per_request_timeout_s: float,
    target_k: int,
    state: _CollectionState,
) -> None:
    """Fire all ``wave_indices`` concurrently and absorb results into ``state``."""
    tasks = [
        asyncio.create_task(
            _try_one(
                cand_idx=i,
                candidate=candidates[i],
                ring=ring,
                run_id=run_id,
                round_no=round_no,
                send_attestreq=send_attestreq,
                per_request_timeout_s=per_request_timeout_s,
                state=state,
            )
        )
        for i in wave_indices
    ]
    # Don't bail early on first ``target_k`` -- letting the rest finish
    # is fine and gives the slack signatures we asked for; the collector
    # trims to exactly k after the fact. Cancelling in-flight tasks
    # would also waste the makers' work and risk leaving them with
    # half-cached state.
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=False)
    _ = target_k  # currently unused; kept for future early-bail tuning


async def _try_one(
    *,
    cand_idx: int,
    candidate: Candidate,
    ring: tuple[RingMember, ...],
    run_id: bytes,
    round_no: int,
    send_attestreq: SendAttestReq,
    per_request_timeout_s: float,
    state: _CollectionState,
) -> None:
    """Contact one candidate, verify the response, mutate ``state``."""
    request = AttestRequest(
        run_id=run_id,
        round_no=round_no,
        ring=ring,
        signer_idx=cand_idx,
    )
    try:
        response = await asyncio.wait_for(
            send_attestreq(candidate.nick, request),
            timeout=per_request_timeout_s,
        )
    except TimeoutError:
        state.failures.append(CandidateFailure(nick=candidate.nick, reason="timeout"))
        return
    except AttestationTransportError as exc:
        state.failures.append(
            CandidateFailure(nick=candidate.nick, reason="transport", detail=str(exc))
        )
        return
    except Exception as exc:  # noqa: BLE001 - transport callables can raise anything
        state.failures.append(
            CandidateFailure(nick=candidate.nick, reason="transport_unexpected", detail=repr(exc))
        )
        return

    sig = response.signature
    expected_size = 33 + 32 + 32 * len(ring)
    if len(sig) != expected_size:
        state.failures.append(
            CandidateFailure(
                nick=candidate.nick,
                reason="bad_size",
                detail=f"got {len(sig)}, expected {expected_size}",
            )
        )
        return

    ok, key_image = verify_ring(signature=sig, ring=list(ring), run_id=run_id, round_no=round_no)
    if not ok:
        state.failures.append(CandidateFailure(nick=candidate.nick, reason="verify_failed"))
        return

    if key_image in state.seen_key_images:
        # Two candidates returned the same key image. Either the same
        # maker was contacted twice (collector bug) or one is signing
        # with another's secret key (impersonation). Either way the
        # final blob would be rejected by JMP-0006's uniqueness check,
        # so refuse here.
        state.failures.append(
            CandidateFailure(
                nick=candidate.nick,
                reason="duplicate_key_image",
                detail=key_image.hex()[:16],
            )
        )
        return

    state.seen_key_images.add(key_image)
    state.accepted.append((cand_idx, sig))


__all__ = [
    "AttestRequest",
    "AttestResponse",
    "AttestationTransportError",
    "Candidate",
    "CandidateFailure",
    "InsufficientAttestationsError",
    "SendAttestReq",
    "collect_attestations",
]
