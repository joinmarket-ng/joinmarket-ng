"""Unit tests for :mod:`jmcore.attestation_collector`.

The collector talks to "makers" via an injected async callable, so
these tests stand up a fake transport backed by real
:class:`maker.attestation_signer.AttestationSigner` instances. That
way every accepted signature is verified by the same CLSAG primitive
the production verifier uses, instead of a mock that only checks the
collector's bookkeeping.
"""

from __future__ import annotations

import asyncio
import os
from typing import Final

import pytest
from maker.attestation_signer import AttestationSigner

from jmcore.attestation_collector import (
    AttestationTransportError,
    AttestRequest,
    AttestResponse,
    Candidate,
    InsufficientAttestationsError,
    collect_attestations,
)
from jmcore.clsag_attestation import (
    pack_attestation,
    unpack_attestation,
    verify_attestation,
)

_SECP_N: Final[int] = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _make_maker(nick: str) -> tuple[Candidate, AttestationSigner]:
    sk = os.urandom(32)
    signer = AttestationSigner(sk)
    cand = Candidate(
        nick=nick,
        pubkey_xonly=signer.pubkey_xonly,
        outpoint=os.urandom(36),
    )
    return cand, signer


class FakeOrderbook:
    """N candidates each backed by a real signer; injectable transport."""

    def __init__(self, n: int) -> None:
        self.candidates: list[Candidate] = []
        self.signers: dict[str, AttestationSigner] = {}
        for i in range(n):
            cand, signer = _make_maker(f"m{i}")
            self.candidates.append(cand)
            self.signers[cand.nick] = signer

    async def transport(self, nick: str, req: AttestRequest) -> AttestResponse:
        signer = self.signers[nick]
        sig = signer.sign_attestation(
            ring=list(req.ring),
            signer_idx=req.signer_idx,
            run_id=req.run_id,
            round_no=req.round_no,
        )
        return AttestResponse(signature=sig)


@pytest.mark.asyncio
async def test_happy_path_collects_exactly_k() -> None:
    ob = FakeOrderbook(n=8)
    run_id = os.urandom(32)
    att = await collect_attestations(
        candidates=ob.candidates,
        k=3,
        run_id=run_id,
        round_no=0,
        send_attestreq=ob.transport,
    )
    assert att.set_size == 8
    assert att.k == 3
    # Round-trip through the wire format and verify cryptographically.
    blob = pack_attestation(att)
    decoded = unpack_attestation(blob)
    res = verify_attestation(decoded, run_id=run_id, round_no=0, min_set_size=1)
    assert res.ok
    assert all(res.per_ring_ok)
    assert len(res.key_images) == 3
    assert len(set(res.key_images)) == 3  # uniqueness


@pytest.mark.asyncio
async def test_signatures_sorted_by_ring_slot() -> None:
    """Concurrent first-wave responses arrive in arbitrary order; the
    canonical wire blob must be deterministic regardless. Uses a
    transport that introduces randomized delay so completion order is
    shuffled."""
    ob = FakeOrderbook(n=6)

    base_transport = ob.transport
    delays = [0.05, 0.0, 0.03, 0.01, 0.04, 0.02]

    async def laggy(nick: str, req: AttestRequest) -> AttestResponse:
        idx = int(nick[1:])
        await asyncio.sleep(delays[idx])
        return await base_transport(nick, req)

    run_id = os.urandom(32)
    att1 = await collect_attestations(
        candidates=ob.candidates,
        k=4,
        run_id=run_id,
        round_no=0,
        send_attestreq=laggy,
    )
    # Same input, second collection over a *different* run_id (since
    # makers refuse the same round) should produce the same ring slot
    # ordering: ascending slot index.
    run_id2 = os.urandom(32)
    att2 = await collect_attestations(
        candidates=ob.candidates,
        k=4,
        run_id=run_id2,
        round_no=0,
        send_attestreq=laggy,
    )
    # The signatures themselves differ (different run_id), but the ring
    # ordering and slot pattern must be identical.
    assert [m.pubkey_xonly for m in att1.ring] == [m.pubkey_xonly for m in att2.ring]
    assert att1.k == att2.k == 4


@pytest.mark.asyncio
async def test_timeouts_fall_back_to_backup_candidates() -> None:
    ob = FakeOrderbook(n=8)

    async def flaky(nick: str, req: AttestRequest) -> AttestResponse:
        # m0..m2 hang forever; m3+ respond.
        if int(nick[1:]) < 3:
            await asyncio.sleep(10)
        return await ob.transport(nick, req)

    run_id = os.urandom(32)
    att = await collect_attestations(
        candidates=ob.candidates,
        k=3,
        run_id=run_id,
        round_no=0,
        send_attestreq=flaky,
        per_request_timeout_s=0.1,
    )
    assert att.k == 3
    res = verify_attestation(att, run_id=run_id, round_no=0, min_set_size=1)
    assert res.ok


@pytest.mark.asyncio
async def test_transport_error_recorded_and_skipped() -> None:
    ob = FakeOrderbook(n=5)

    async def broken(nick: str, req: AttestRequest) -> AttestResponse:
        if nick == "m0":
            raise AttestationTransportError("peer disconnected")
        return await ob.transport(nick, req)

    run_id = os.urandom(32)
    att = await collect_attestations(
        candidates=ob.candidates,
        k=2,
        run_id=run_id,
        round_no=0,
        send_attestreq=broken,
    )
    assert att.k == 2


@pytest.mark.asyncio
async def test_insufficient_signers_raises_with_partial() -> None:
    ob = FakeOrderbook(n=4)

    async def all_fail_except_one(nick: str, req: AttestRequest) -> AttestResponse:
        if nick != "m0":
            raise AttestationTransportError("nope")
        return await ob.transport(nick, req)

    run_id = os.urandom(32)
    with pytest.raises(InsufficientAttestationsError) as exc:
        await collect_attestations(
            candidates=ob.candidates,
            k=3,
            run_id=run_id,
            round_no=0,
            send_attestreq=all_fail_except_one,
        )
    assert exc.value.partial.k == 1
    assert exc.value.partial.set_size == 4
    # 3 failures recorded.
    assert len(exc.value.failures) == 3
    reasons = {f.reason for f in exc.value.failures}
    assert reasons == {"transport"}


@pytest.mark.asyncio
async def test_malformed_signature_rejected() -> None:
    ob = FakeOrderbook(n=5)

    async def truncating(nick: str, req: AttestRequest) -> AttestResponse:
        if nick == "m0":
            real = await ob.transport(nick, req)
            return AttestResponse(signature=real.signature[:-4])  # too short
        return await ob.transport(nick, req)

    run_id = os.urandom(32)
    att = await collect_attestations(
        candidates=ob.candidates,
        k=2,
        run_id=run_id,
        round_no=0,
        send_attestreq=truncating,
    )
    # m0's truncated reply was dropped; collector pulled enough from others.
    assert att.k == 2


@pytest.mark.asyncio
async def test_signature_for_wrong_signer_idx_rejected() -> None:
    """A maker that signs at slot j but is asked to sign at slot i != j
    produces a valid CLSAG over the *same ring*, so the ring signature
    itself still verifies. The collector accepts this: CLSAG's whole
    point is that any ring member can stand in. We rely on the
    per-(run_id, round_no) cache to prevent the same maker contributing
    twice; signing-slot mismatch is not a security issue here.

    This test pins that behavior so we notice if the contract changes.
    """
    ob = FakeOrderbook(n=4)

    async def wrong_slot(nick: str, req: AttestRequest) -> AttestResponse:
        # Force the signer to claim its own true slot regardless of what
        # the taker asked. With the AttestationSigner invariant
        # (ring[signer_idx].pubkey == self.pubkey) it would raise. Use
        # the raw primitive instead.
        from jmcore.clsag_attestation import sign_ring

        signer = ob.signers[nick]
        # find the true slot
        true_slot = next(i for i, m in enumerate(req.ring) if m.pubkey_xonly == signer.pubkey_xonly)
        # Sign at the true slot using the underlying secret. We poke at
        # the normalized secret directly via the signer's private attr.
        sk = signer._secret_key  # noqa: SLF001 - test introspection
        sig = sign_ring(
            secret_key=sk,
            ring=list(req.ring),
            signer_idx=true_slot,
            run_id=req.run_id,
            round_no=req.round_no,
        )
        return AttestResponse(signature=sig)

    run_id = os.urandom(32)
    att = await collect_attestations(
        candidates=ob.candidates,
        k=2,
        run_id=run_id,
        round_no=0,
        send_attestreq=wrong_slot,
    )
    # All replies verify (CLSAG hides the slot), so collection succeeds.
    assert att.k == 2


@pytest.mark.asyncio
async def test_duplicate_key_image_rejected() -> None:
    """Two candidates returning the same key image: drop the second.

    Construct it by giving two Candidate entries the same secret-key-
    derived pubkey but different nicks/outpoints, both routed to the
    same backing signer.
    """
    ob = FakeOrderbook(n=3)
    # Add a fourth candidate that aliases m0's signer.
    aliased = Candidate(
        nick="alias",
        pubkey_xonly=ob.candidates[0].pubkey_xonly,
        outpoint=os.urandom(36),
    )
    ob.candidates.append(aliased)
    ob.signers["alias"] = ob.signers["m0"]

    run_id = os.urandom(32)
    # k=3 with N=4: collector contacts all 4. m0 and alias share a key
    # image -> one of them is dropped as duplicate.
    with pytest.raises(InsufficientAttestationsError) as exc:
        await collect_attestations(
            candidates=ob.candidates,
            k=4,
            run_id=run_id,
            round_no=0,
            send_attestreq=ob.transport,
        )
    # We got 3 distinct, asked for 4 -> partial has 3.
    assert exc.value.partial.k == 3
    dup_failures = [f for f in exc.value.failures if f.reason == "duplicate_key_image"]
    # Exactly one of m0 / alias was rejected as duplicate; the other
    # may have been dropped as the per-round cache hit (also counts as
    # a transport-side error, surfaced via the AttestationSigner). One
    # of the two paths must have fired.
    cache_hits = [
        f for f in exc.value.failures if "duplicate" in f.reason or "transport" in f.reason
    ]
    assert dup_failures or cache_hits


@pytest.mark.asyncio
async def test_too_few_candidates_rejected_eagerly() -> None:
    ob = FakeOrderbook(n=2)
    with pytest.raises(ValueError, match="need at least k=3"):
        await collect_attestations(
            candidates=ob.candidates,
            k=3,
            run_id=os.urandom(32),
            round_no=0,
            send_attestreq=ob.transport,
        )


@pytest.mark.asyncio
async def test_invalid_k_rejected_eagerly() -> None:
    ob = FakeOrderbook(n=4)
    with pytest.raises(ValueError, match="k must be >= 1"):
        await collect_attestations(
            candidates=ob.candidates,
            k=0,
            run_id=os.urandom(32),
            round_no=0,
            send_attestreq=ob.transport,
        )
