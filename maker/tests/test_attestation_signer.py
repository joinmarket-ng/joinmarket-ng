"""Unit tests for :mod:`maker.attestation_signer`.

These tests exercise the JMP-0006 invariants the signer enforces, not
the underlying CLSAG primitive (which has its own coverage in
``jmcore/tests/test_clsag_attestation.py``). The focus here is:

  * parity normalization (the wallet hands us either parity at random),
  * one-attestation-per-(run_id, round_no) refusal,
  * cross-run independence,
  * bounded cache + ``forget_run`` cleanup hooks.
"""

from __future__ import annotations

import os

import pytest
from coincurve import PrivateKey
from jmcore.clsag_attestation import (
    RingMember,
    compute_key_image,
    verify_ring,
)

from maker.attestation_signer import (
    AttestationSigner,
    DuplicateAttestationError,
)

_SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _xonly_for(secret: bytes) -> bytes:
    pk = PrivateKey(secret).public_key.format(compressed=True)
    if pk[0] == 0x02:
        return pk[1:]
    flipped = (_SECP_N - int.from_bytes(secret, "big")) % _SECP_N
    return PrivateKey(flipped.to_bytes(32, "big")).public_key.format(compressed=True)[1:]


def _ring_with_signer_at(signer_sk: bytes, idx: int, total: int) -> tuple[list[RingMember], int]:
    """Build a ring of ``total`` members with the signer's pubkey at ``idx``."""
    members: list[RingMember] = []
    for i in range(total):
        if i == idx:
            members.append(
                RingMember(
                    pubkey_xonly=_xonly_for(signer_sk),
                    outpoint=os.urandom(36),
                )
            )
        else:
            decoy = os.urandom(32)
            members.append(
                RingMember(
                    pubkey_xonly=_xonly_for(decoy),
                    outpoint=os.urandom(36),
                )
            )
    return members, idx


def test_signer_normalizes_odd_y_secret() -> None:
    """A secret whose pubkey has odd Y must be flipped so the cached
    x-only key still verifies.

    Strategy: try random secrets until we find one with odd-Y pubkey,
    then assert the signer advertises the *negated* secret's x-only
    pubkey and that signing+verifying round-trips against that pubkey.
    """
    while True:
        sk = os.urandom(32)
        pk = PrivateKey(sk).public_key.format(compressed=True)
        if pk[0] == 0x03:  # odd Y -> normalization will fire
            break

    signer = AttestationSigner(sk)
    expected_xonly = pk[1:]  # same x coordinate, normalization just flips parity
    assert signer.pubkey_xonly == expected_xonly

    ring, idx = _ring_with_signer_at(sk, idx=0, total=4)
    # The ring helper independently normalizes; the signer's advertised
    # pubkey must agree byte-for-byte with the ring entry.
    ring[0] = RingMember(pubkey_xonly=signer.pubkey_xonly, outpoint=ring[0].outpoint)

    run_id = os.urandom(32)
    sig = signer.sign_attestation(ring=ring, signer_idx=idx, run_id=run_id, round_no=0)
    ok, ki = verify_ring(signature=sig, ring=ring, run_id=run_id, round_no=0)
    assert ok
    assert ki == signer.key_image_for(run_id)


def test_signer_rejects_ring_without_its_pubkey() -> None:
    sk = os.urandom(32)
    signer = AttestationSigner(sk)
    # Ring built around a *different* secret at index 1.
    other = os.urandom(32)
    ring, _ = _ring_with_signer_at(other, idx=1, total=3)
    with pytest.raises(ValueError, match="does not match this signer"):
        signer.sign_attestation(ring=ring, signer_idx=1, run_id=os.urandom(32), round_no=0)


def test_duplicate_round_raises_with_cached_key_image() -> None:
    sk = os.urandom(32)
    signer = AttestationSigner(sk)
    run_id = os.urandom(32)
    ring, idx = _ring_with_signer_at(sk, idx=2, total=5)
    ring[2] = RingMember(pubkey_xonly=signer.pubkey_xonly, outpoint=ring[2].outpoint)

    sig1 = signer.sign_attestation(ring=ring, signer_idx=idx, run_id=run_id, round_no=7)

    # Same round, *different ring* (different decoys) must still be refused.
    ring2, _ = _ring_with_signer_at(sk, idx=2, total=5)
    ring2[2] = RingMember(pubkey_xonly=signer.pubkey_xonly, outpoint=ring2[2].outpoint)
    with pytest.raises(DuplicateAttestationError) as exc:
        signer.sign_attestation(ring=ring2, signer_idx=2, run_id=run_id, round_no=7)
    assert exc.value.run_id == run_id
    assert exc.value.round_no == 7
    assert exc.value.key_image == sig1[:33]


def test_different_rounds_same_run_are_allowed() -> None:
    sk = os.urandom(32)
    signer = AttestationSigner(sk)
    run_id = os.urandom(32)
    for round_no in range(3):
        ring, idx = _ring_with_signer_at(sk, idx=0, total=4)
        ring[0] = RingMember(pubkey_xonly=signer.pubkey_xonly, outpoint=ring[0].outpoint)
        sig = signer.sign_attestation(ring=ring, signer_idx=0, run_id=run_id, round_no=round_no)
        ok, _ = verify_ring(signature=sig, ring=ring, run_id=run_id, round_no=round_no)
        assert ok
    assert signer.cache_size() == 3


def test_different_runs_have_independent_state() -> None:
    sk = os.urandom(32)
    signer = AttestationSigner(sk)
    run_a = b"a" * 32
    run_b = b"b" * 32
    ring, idx = _ring_with_signer_at(sk, idx=0, total=3)
    ring[0] = RingMember(pubkey_xonly=signer.pubkey_xonly, outpoint=ring[0].outpoint)

    signer.sign_attestation(ring=ring, signer_idx=0, run_id=run_a, round_no=0)
    # Same round, different run -> allowed.
    signer.sign_attestation(ring=ring, signer_idx=0, run_id=run_b, round_no=0)
    assert signer.cache_size() == 2

    # Key images differ across runs (per JMP-0006 rotation).
    ki_a = signer.key_image_for(run_a)
    ki_b = signer.key_image_for(run_b)
    assert ki_a != ki_b
    # And they match the underlying primitive.
    sk_norm = signer._secret_key  # noqa: SLF001 - test introspection
    assert ki_a == compute_key_image(sk_norm, run_a)


def test_forget_run_clears_only_targeted_run() -> None:
    sk = os.urandom(32)
    signer = AttestationSigner(sk)
    run_a = b"a" * 32
    run_b = b"b" * 32
    ring, _ = _ring_with_signer_at(sk, idx=0, total=3)
    ring[0] = RingMember(pubkey_xonly=signer.pubkey_xonly, outpoint=ring[0].outpoint)

    for r in (0, 1, 2):
        signer.sign_attestation(ring=ring, signer_idx=0, run_id=run_a, round_no=r)
    signer.sign_attestation(ring=ring, signer_idx=0, run_id=run_b, round_no=0)

    dropped = signer.forget_run(run_a)
    assert dropped == 3
    assert signer.cache_size() == 1
    # Round 0 of run_a is now signable again (the maker presumably
    # observed the run terminate before forgetting).
    signer.sign_attestation(ring=ring, signer_idx=0, run_id=run_a, round_no=0)
    assert signer.cache_size() == 2


def test_lru_cache_bound_evicts_oldest() -> None:
    sk = os.urandom(32)
    signer = AttestationSigner(sk, cache_limit=3)
    ring, _ = _ring_with_signer_at(sk, idx=0, total=2)
    ring[0] = RingMember(pubkey_xonly=signer.pubkey_xonly, outpoint=ring[0].outpoint)
    run_id = os.urandom(32)

    for r in range(4):
        signer.sign_attestation(ring=ring, signer_idx=0, run_id=run_id, round_no=r)

    assert signer.cache_size() == 3
    # Round 0 evicted -> can be re-signed (this is the price of the LRU
    # bound; legitimate workloads stay below the limit).
    signer.sign_attestation(ring=ring, signer_idx=0, run_id=run_id, round_no=0)


def test_invalid_secret_length_rejected() -> None:
    with pytest.raises(ValueError, match="secret_key must be 32 bytes"):
        AttestationSigner(b"\x00" * 31)


def test_invalid_cache_limit_rejected() -> None:
    with pytest.raises(ValueError, match="cache_limit must be >= 1"):
        AttestationSigner(os.urandom(32), cache_limit=0)


def test_from_coincurve_private_key() -> None:
    pk = PrivateKey()
    signer = AttestationSigner.from_coincurve_private_key(pk)
    # Should produce the same x-only pubkey as the manual constructor.
    direct = AttestationSigner(pk.secret)
    assert signer.pubkey_xonly == direct.pubkey_xonly
