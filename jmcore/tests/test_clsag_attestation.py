"""Tests for the CLSAG-style ring-signature bond attestation primitive."""

from __future__ import annotations

import secrets
import struct

import coincurve
import pytest

from jmcore.clsag_attestation import (
    ATTESTATION_DST,
    DEFAULT_MIN_SET_SIZE,
    MAX_K,
    RUN_ID_LEN,
    Attestation,
    RingMember,
    attestation_message,
    compute_key_image,
    pack_attestation,
    sign_ring,
    unpack_attestation,
    verify_attestation,
    verify_ring,
)

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
#
# CLSAG ring members are BIP340 x-only pubkeys (even-Y lift), so we
# generate a (sk, x_only_pk) pair by deriving the compressed pubkey
# and flipping the secret if y is odd. Same recipe used by the
# nwabisabi PyO3-binding tests.

_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _ring_member() -> tuple[bytes, bytes]:
    while True:
        sk = secrets.token_bytes(32)
        try:
            pk = coincurve.PrivateKey(sk).public_key.format(compressed=True)
        except ValueError:
            continue
        if pk[0] == 0x02:
            return sk, pk[1:]
        flipped = (_SECP256K1_N - int.from_bytes(sk, "big")) % _SECP256K1_N
        sk2 = flipped.to_bytes(32, "big")
        return sk2, pk[1:]


def _make_outpoint(seed: int) -> bytes:
    """Synthetic 36-byte outpoint with deterministic txid; vout in LE32."""
    txid = seed.to_bytes(32, "little")
    vout = struct.pack("<I", seed & 0xFFFF)
    return txid + vout


def _build_ring(n: int) -> tuple[list[bytes], list[RingMember]]:
    """Return (secrets, ring_members) for a ring of size ``n``."""
    members: list[tuple[bytes, bytes]] = [_ring_member() for _ in range(n)]
    secrets_only = [sk for sk, _ in members]
    ring = [
        RingMember(pubkey_xonly=xonly, outpoint=_make_outpoint(i))
        for i, (_, xonly) in enumerate(members)
    ]
    return secrets_only, ring


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def test_attestation_message_canonical_layout() -> None:
    run_id = b"\x11" * RUN_ID_LEN
    msg = attestation_message(run_id, round_no=7)
    assert msg.startswith(ATTESTATION_DST)
    assert msg[len(ATTESTATION_DST) : len(ATTESTATION_DST) + RUN_ID_LEN] == run_id
    assert msg[-2:] == b"\x00\x07"
    assert len(msg) == len(ATTESTATION_DST) + RUN_ID_LEN + 2


def test_attestation_message_rejects_bad_run_id() -> None:
    with pytest.raises(ValueError):
        attestation_message(b"\x00" * 31, round_no=0)


def test_attestation_message_rejects_round_overflow() -> None:
    with pytest.raises(ValueError):
        attestation_message(b"\x00" * RUN_ID_LEN, round_no=0x10000)


# ---------------------------------------------------------------------------
# Per-ring sign / verify / key image
# ---------------------------------------------------------------------------


def test_sign_ring_round_trip() -> None:
    secrets_only, ring = _build_ring(5)
    signer_idx = 2
    run_id = b"\x42" * RUN_ID_LEN
    sig = sign_ring(
        secret_key=secrets_only[signer_idx],
        ring=ring,
        signer_idx=signer_idx,
        run_id=run_id,
        round_no=1,
    )
    assert len(sig) == 33 + 32 + 32 * len(ring)

    ok, ki = verify_ring(signature=sig, ring=ring, run_id=run_id, round_no=1)
    assert ok is True
    assert len(ki) == 33

    direct = compute_key_image(secrets_only[signer_idx], run_id)
    assert direct == ki


def test_sign_ring_rejects_out_of_range_signer_idx() -> None:
    secrets_only, ring = _build_ring(3)
    with pytest.raises(ValueError):
        sign_ring(
            secret_key=secrets_only[0],
            ring=ring,
            signer_idx=5,
            run_id=b"\x00" * RUN_ID_LEN,
            round_no=0,
        )


def test_verify_ring_rejects_tampered_round_no() -> None:
    secrets_only, ring = _build_ring(4)
    sig = sign_ring(
        secret_key=secrets_only[1],
        ring=ring,
        signer_idx=1,
        run_id=b"\xab" * RUN_ID_LEN,
        round_no=2,
    )
    ok, _ = verify_ring(signature=sig, ring=ring, run_id=b"\xab" * RUN_ID_LEN, round_no=3)
    assert ok is False


def test_compute_key_image_rotates_per_run() -> None:
    sk, _ = _ring_member()
    ki_a = compute_key_image(sk, b"A" * RUN_ID_LEN)
    ki_b = compute_key_image(sk, b"B" * RUN_ID_LEN)
    assert ki_a != ki_b


def test_compute_key_image_validates_run_id_length() -> None:
    sk, _ = _ring_member()
    with pytest.raises(ValueError):
        compute_key_image(sk, b"short")


# ---------------------------------------------------------------------------
# RingMember validation
# ---------------------------------------------------------------------------


def test_ring_member_validates_field_lengths() -> None:
    with pytest.raises(ValueError):
        RingMember(pubkey_xonly=b"\x00" * 31, outpoint=b"\x00" * 36)
    with pytest.raises(ValueError):
        RingMember(pubkey_xonly=b"\x00" * 32, outpoint=b"\x00" * 35)


# ---------------------------------------------------------------------------
# Pack / unpack round-trip
# ---------------------------------------------------------------------------


def test_pack_unpack_round_trip_minimum_k() -> None:
    secrets_only, ring = _build_ring(8)
    run_id = b"\x77" * RUN_ID_LEN
    round_no = 4
    sigs = [
        sign_ring(
            secret_key=secrets_only[i],
            ring=ring,
            signer_idx=i,
            run_id=run_id,
            round_no=round_no,
        )
        for i in (0, 3, 6)
    ]
    att = Attestation(ring=ring, ring_signatures=sigs)
    blob = pack_attestation(att)
    decoded = unpack_attestation(blob)

    assert decoded.set_size == att.set_size
    assert decoded.k == att.k == 3
    assert [m.pubkey_xonly for m in decoded.ring] == [m.pubkey_xonly for m in ring]
    assert [m.outpoint for m in decoded.ring] == [m.outpoint for m in ring]
    assert decoded.ring_signatures == sigs

    # Wire size matches the JMP-0006 formula.
    assert len(blob) == 2 + 32 * 8 + 36 * 8 + 1 + 3 * (33 + 32 + 32 * 8)


def test_pack_rejects_inconsistent_signature_size() -> None:
    _, ring = _build_ring(6)
    bad_sig = b"\x00" * (33 + 32 + 32 * 5)  # wrong N
    with pytest.raises(ValueError, match="ring_signatures"):
        pack_attestation(Attestation(ring=ring, ring_signatures=[bad_sig]))


def test_pack_rejects_k_out_of_bounds() -> None:
    _, ring = _build_ring(2)
    valid_sig = b"\x00" * (33 + 32 + 32 * len(ring))
    with pytest.raises(ValueError, match="sig_count"):
        pack_attestation(Attestation(ring=ring, ring_signatures=[]))
    with pytest.raises(ValueError, match="sig_count"):
        pack_attestation(Attestation(ring=ring, ring_signatures=[valid_sig] * (MAX_K + 1)))


def test_unpack_rejects_truncated_blob() -> None:
    with pytest.raises(ValueError):
        unpack_attestation(b"\x00")


def test_unpack_rejects_trailing_bytes() -> None:
    secrets_only, ring = _build_ring(4)
    sig = sign_ring(
        secret_key=secrets_only[0],
        ring=ring,
        signer_idx=0,
        run_id=b"\x10" * RUN_ID_LEN,
        round_no=0,
    )
    blob = pack_attestation(Attestation(ring=ring, ring_signatures=[sig]))
    with pytest.raises(ValueError, match="length mismatch"):
        unpack_attestation(blob + b"\x00")


# ---------------------------------------------------------------------------
# Full verification (verify_attestation)
# ---------------------------------------------------------------------------


def test_verify_attestation_accepts_valid_blob() -> None:
    # Use a small ring and override min_set_size so we don't have to
    # build a 25-member ring just to exercise the policy floor.
    secrets_only, ring = _build_ring(6)
    run_id = b"\x99" * RUN_ID_LEN
    sigs = [
        sign_ring(
            secret_key=secrets_only[i],
            ring=ring,
            signer_idx=i,
            run_id=run_id,
            round_no=2,
        )
        for i in (0, 2, 4)
    ]
    att = Attestation(ring=ring, ring_signatures=sigs)
    res = verify_attestation(att, run_id=run_id, round_no=2, min_set_size=6)
    assert res.ok is True
    assert res.per_ring_ok == [True, True, True]
    assert len(set(res.key_images)) == 3
    assert res.set_size == 6
    assert res.k == 3


def test_verify_attestation_rejects_below_min_set_size() -> None:
    secrets_only, ring = _build_ring(4)
    run_id = b"\x55" * RUN_ID_LEN
    sigs = [
        sign_ring(
            secret_key=secrets_only[0],
            ring=ring,
            signer_idx=0,
            run_id=run_id,
            round_no=0,
        )
    ]
    att = Attestation(ring=ring, ring_signatures=sigs)
    res = verify_attestation(att, run_id=run_id, round_no=0, min_set_size=DEFAULT_MIN_SET_SIZE)
    assert res.ok is False
    assert res.per_ring_ok == [True]


def test_verify_attestation_rejects_duplicate_key_images() -> None:
    # Two rings produced by the same signer in the same run share the
    # same key image -> attestation MUST be rejected even though both
    # signatures verify on their own.
    secrets_only, ring = _build_ring(5)
    run_id = b"\x33" * RUN_ID_LEN
    sig_a = sign_ring(
        secret_key=secrets_only[1],
        ring=ring,
        signer_idx=1,
        run_id=run_id,
        round_no=1,
    )
    sig_b = sign_ring(
        secret_key=secrets_only[1],
        ring=ring,
        signer_idx=1,
        run_id=run_id,
        round_no=1,
    )
    att = Attestation(ring=ring, ring_signatures=[sig_a, sig_b])
    res = verify_attestation(att, run_id=run_id, round_no=1, min_set_size=5)
    assert res.ok is False
    # Both signatures verify -> fault is purely the duplicate key image.
    assert res.per_ring_ok == [True, True]
    assert res.key_images[0] == res.key_images[1]


def test_verify_attestation_rejects_tampered_signature() -> None:
    secrets_only, ring = _build_ring(4)
    run_id = b"\x22" * RUN_ID_LEN
    sig = bytearray(
        sign_ring(
            secret_key=secrets_only[0],
            ring=ring,
            signer_idx=0,
            run_id=run_id,
            round_no=0,
        )
    )
    sig[-1] ^= 0xFF  # flip last response scalar
    att = Attestation(ring=ring, ring_signatures=[bytes(sig)])
    res = verify_attestation(att, run_id=run_id, round_no=0, min_set_size=4)
    assert res.ok is False
    assert res.per_ring_ok == [False]
