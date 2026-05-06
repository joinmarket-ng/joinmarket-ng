"""Tests for the K-of-N concatenated Schnorr bond attestation primitive."""

from __future__ import annotations

import os
import struct

import pytest

import jmcore.schnorr as schnorr
from jmcore.bond_attestation import (
    ATTEST_DOMAIN_TAG,
    COUNT_SIZE,
    MAX_BOND_COUNT,
    MAX_ROUND_NO,
    OUTPOINT_SIZE,
    PUBKEY_SIZE,
    RECORD_SIZE,
    RUN_ID_SIZE,
    SIGNATURE_SIZE,
    BondAttestationError,
    BondOutpoint,
    BondSignerInput,
    build_attest_message,
    pack_attestation,
    sign_attestation,
    unpack_attestation,
    verify_attestation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gen_signer(
    seed: int, vout: int = 0, txid: bytes | None = None
) -> tuple[bytes, BondOutpoint, bytes]:
    """Build a (secret_key, outpoint, xonly_pubkey) triple from a seed.

    Deterministic so tests can compute expected ordering without rolling
    fresh randomness on every invocation.
    """
    sk = seed.to_bytes(32, "big")
    pk = schnorr.derive_xonly_pubkey(sk)
    if txid is None:
        # 32-byte display-order txid derived from the seed; vary the
        # high bytes so canonical sort order is actually exercised.
        txid = seed.to_bytes(32, "big")
    outpoint = BondOutpoint(txid=txid, vout=vout)
    return sk, outpoint, pk


def _build_signed(
    sk: bytes, outpoint: BondOutpoint, pk: bytes, run_id: bytes, round_no: int
) -> BondSignerInput:
    sig = sign_attestation(sk, run_id, round_no)
    return BondSignerInput(outpoint=outpoint, pubkey=pk, signature=sig)


def _make_run_id(seed: int = 1) -> bytes:
    return bytes([seed]) * 32


# ---------------------------------------------------------------------------
# Wire-format constants
# ---------------------------------------------------------------------------


def test_constants_match_spec() -> None:
    """JMP-0006 wire format is fixed; surface it in tests so silent drift fails."""
    assert RUN_ID_SIZE == 32
    assert OUTPOINT_SIZE == 36
    assert PUBKEY_SIZE == 32
    assert SIGNATURE_SIZE == 64
    assert RECORD_SIZE == 132
    assert COUNT_SIZE == 1
    assert MAX_BOND_COUNT == 0xFF
    assert MAX_ROUND_NO == 0xFFFF
    assert ATTEST_DOMAIN_TAG == b"jmng/tx_extension_v1/attest"


# ---------------------------------------------------------------------------
# BondOutpoint
# ---------------------------------------------------------------------------


def test_outpoint_wire_round_trip() -> None:
    txid = bytes(range(32))  # 0x00..0x1f, display order
    op = BondOutpoint(txid=txid, vout=7)
    wire = op.to_wire()
    assert len(wire) == OUTPOINT_SIZE
    # Bitcoin OutPoint serialization: txid LE then vout LE u32.
    assert wire[:32] == txid[::-1]
    assert struct.unpack("<I", wire[32:])[0] == 7

    parsed = BondOutpoint.from_wire(wire)
    assert parsed == op


def test_outpoint_rejects_bad_txid_length() -> None:
    with pytest.raises(BondAttestationError, match="txid must be 32 bytes"):
        BondOutpoint(txid=b"\x00" * 31, vout=0)


@pytest.mark.parametrize("bad_vout", [-1, 1 << 32])
def test_outpoint_rejects_out_of_range_vout(bad_vout: int) -> None:
    with pytest.raises(BondAttestationError, match="vout out of uint32"):
        BondOutpoint(txid=b"\x00" * 32, vout=bad_vout)


def test_outpoint_from_wire_rejects_bad_length() -> None:
    with pytest.raises(BondAttestationError, match="must be 36 bytes"):
        BondOutpoint.from_wire(b"\x00" * 35)


def test_outpoint_sort_key_is_display_order() -> None:
    a = BondOutpoint(txid=b"\x00" * 32, vout=5)
    b = BondOutpoint(txid=b"\x00" * 32, vout=6)
    c = BondOutpoint(txid=b"\x01" + b"\x00" * 31, vout=0)
    sorted_ops = sorted([c, b, a], key=BondOutpoint.sort_key)
    assert sorted_ops == [a, b, c]


# ---------------------------------------------------------------------------
# BondSignerInput
# ---------------------------------------------------------------------------


def test_signer_input_validates_pubkey_length() -> None:
    op = BondOutpoint(txid=b"\x00" * 32, vout=0)
    with pytest.raises(BondAttestationError, match="pubkey must be 32 bytes"):
        BondSignerInput(outpoint=op, pubkey=b"\x00" * 31, signature=b"\x00" * 64)


def test_signer_input_validates_signature_length() -> None:
    op = BondOutpoint(txid=b"\x00" * 32, vout=0)
    with pytest.raises(BondAttestationError, match="signature must be 64 bytes"):
        BondSignerInput(outpoint=op, pubkey=b"\x00" * 32, signature=b"\x00" * 63)


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def test_build_attest_message_is_32_bytes_and_deterministic() -> None:
    m1 = build_attest_message(_make_run_id(), round_no=1)
    m2 = build_attest_message(_make_run_id(), round_no=1)
    assert m1 == m2
    assert len(m1) == 32


def test_build_attest_message_binds_run_id() -> None:
    m1 = build_attest_message(_make_run_id(1), round_no=1)
    m2 = build_attest_message(_make_run_id(2), round_no=1)
    assert m1 != m2


def test_build_attest_message_binds_round_no() -> None:
    m1 = build_attest_message(_make_run_id(), round_no=1)
    m2 = build_attest_message(_make_run_id(), round_no=2)
    assert m1 != m2


def test_build_attest_message_round_no_endianness() -> None:
    """Sanity check: changing the high byte of round_no must change the digest."""
    m_lo = build_attest_message(_make_run_id(), round_no=0x0001)
    m_hi = build_attest_message(_make_run_id(), round_no=0x0100)
    assert m_lo != m_hi


def test_build_attest_message_rejects_bad_run_id() -> None:
    with pytest.raises(BondAttestationError, match="run_id must be 32 bytes"):
        build_attest_message(b"\x00" * 31, round_no=1)


@pytest.mark.parametrize("bad", [-1, 1 << 16])
def test_build_attest_message_rejects_bad_round_no(bad: int) -> None:
    with pytest.raises(BondAttestationError, match="uint16"):
        build_attest_message(_make_run_id(), round_no=bad)


def test_build_attest_message_uses_tagged_hash_domain_separation() -> None:
    """Computing the same payload with a different tag must give a different digest."""
    run_id = _make_run_id()
    payload = ATTEST_DOMAIN_TAG + run_id + struct.pack(">H", 1)
    canonical = build_attest_message(run_id, round_no=1)
    other = schnorr.tagged_hash("some/other/tag", payload)
    assert canonical != other


# ---------------------------------------------------------------------------
# Round-trip: sign -> pack -> unpack -> verify
# ---------------------------------------------------------------------------


def test_full_round_trip_k3() -> None:
    """K=3 is the JMP-0006 recommended threshold; keep it as the headline test."""
    run_id = _make_run_id()
    round_no = 1
    raw = [
        _gen_signer(seed=11, vout=0),
        _gen_signer(seed=22, vout=1),
        _gen_signer(seed=33, vout=2),
    ]
    signers = [_build_signed(sk, op, pk, run_id, round_no) for sk, op, pk in raw]
    signers.sort(key=lambda s: s.outpoint.sort_key())

    blob = pack_attestation(signers)
    assert len(blob) == COUNT_SIZE + 3 * RECORD_SIZE

    parsed = unpack_attestation(blob)
    assert parsed == signers

    verified = verify_attestation(blob, run_id, round_no, expected_count=3)
    assert verified == signers


def test_full_round_trip_k1() -> None:
    """K=1 is degenerate but legal at the wire layer; reject only at policy."""
    run_id = _make_run_id()
    sk, op, pk = _gen_signer(seed=42)
    signer = _build_signed(sk, op, pk, run_id, round_no=1)
    blob = pack_attestation([signer])
    assert verify_attestation(blob, run_id, round_no=1) == [signer]


def test_full_round_trip_k_max() -> None:
    """The 1-byte count field caps K at 255; verify the boundary works."""
    run_id = _make_run_id()
    round_no = 1
    signers = []
    for seed in range(1, MAX_BOND_COUNT + 1):
        sk, op, pk = _gen_signer(seed=seed, vout=seed)
        signers.append(_build_signed(sk, op, pk, run_id, round_no))
    signers.sort(key=lambda s: s.outpoint.sort_key())
    blob = pack_attestation(signers)
    assert len(blob) == COUNT_SIZE + MAX_BOND_COUNT * RECORD_SIZE
    verify_attestation(blob, run_id, round_no, expected_count=MAX_BOND_COUNT)


# ---------------------------------------------------------------------------
# Pack-side validation
# ---------------------------------------------------------------------------


def test_pack_rejects_empty_list() -> None:
    with pytest.raises(BondAttestationError, match="at least one signer"):
        pack_attestation([])


def test_pack_rejects_more_than_max_signers() -> None:
    """One past the 1-byte count limit must fail loudly."""
    run_id = _make_run_id()
    signers = []
    for seed in range(MAX_BOND_COUNT + 1):
        sk, op, pk = _gen_signer(seed=seed + 1, vout=seed)
        signers.append(_build_signed(sk, op, pk, run_id, round_no=1))
    signers.sort(key=lambda s: s.outpoint.sort_key())
    with pytest.raises(BondAttestationError, match="at most 255"):
        pack_attestation(signers)


def test_pack_rejects_unsorted_signers() -> None:
    """Caller must pre-sort; the packer asserts canonical order."""
    run_id = _make_run_id()
    a = _build_signed(*_gen_signer(seed=1, vout=0), run_id=run_id, round_no=1)
    b = _build_signed(*_gen_signer(seed=2, vout=1), run_id=run_id, round_no=1)
    pair = sorted([a, b], key=lambda s: s.outpoint.sort_key())
    reversed_pair = list(reversed(pair))
    with pytest.raises(BondAttestationError, match="ascending"):
        pack_attestation(reversed_pair)


def test_pack_rejects_duplicate_outpoint() -> None:
    """Same UTXO twice would cheaply double-count one bond toward the threshold."""
    run_id = _make_run_id()
    sk, op, pk = _gen_signer(seed=7, vout=3)
    first = _build_signed(sk, op, pk, run_id, round_no=1)
    # Reusing the same outpoint with a fresh signature still has the same key.
    second = BondSignerInput(
        outpoint=op,
        pubkey=pk,
        signature=sign_attestation(sk, run_id, round_no=1, aux_rand=b"\x01" * 32),
    )
    with pytest.raises(BondAttestationError, match="duplicate bond outpoint"):
        pack_attestation([first, second])


# ---------------------------------------------------------------------------
# Unpack-side validation
# ---------------------------------------------------------------------------


def test_unpack_rejects_empty_blob() -> None:
    with pytest.raises(BondAttestationError, match="empty"):
        unpack_attestation(b"")


def test_unpack_rejects_zero_count() -> None:
    with pytest.raises(BondAttestationError, match="zero signers"):
        unpack_attestation(b"\x00")


def test_unpack_rejects_truncated_blob() -> None:
    """Declared count larger than the trailing bytes must fail length check."""
    blob = b"\x02" + b"\x00" * (RECORD_SIZE - 1)  # claims 2 records, has < 1
    with pytest.raises(BondAttestationError, match="length mismatch"):
        unpack_attestation(blob)


def test_unpack_rejects_overlong_blob() -> None:
    """Trailing junk after the declared records is also a hard error."""
    blob = b"\x01" + b"\x00" * RECORD_SIZE + b"\x00"  # one extra trailing byte
    with pytest.raises(BondAttestationError, match="length mismatch"):
        unpack_attestation(blob)


def test_unpack_rejects_unsorted_records() -> None:
    run_id = _make_run_id()
    a = _build_signed(*_gen_signer(seed=1, vout=0), run_id=run_id, round_no=1)
    b = _build_signed(*_gen_signer(seed=2, vout=1), run_id=run_id, round_no=1)
    pair = sorted([a, b], key=lambda s: s.outpoint.sort_key())
    # Manually assemble a wire blob with the records swapped.
    bad = bytes([2])
    bad += pair[1].outpoint.to_wire() + pair[1].pubkey + pair[1].signature
    bad += pair[0].outpoint.to_wire() + pair[0].pubkey + pair[0].signature
    with pytest.raises(BondAttestationError, match="canonical order"):
        unpack_attestation(bad)


# ---------------------------------------------------------------------------
# Verification: signature & message binding
# ---------------------------------------------------------------------------


def test_verify_rejects_tampered_signature() -> None:
    """Flipping a single bit in any signature must fail verification."""
    run_id = _make_run_id()
    raw = [_gen_signer(seed=s, vout=s) for s in (5, 6, 7)]
    signers = [_build_signed(sk, op, pk, run_id, 1) for sk, op, pk in raw]
    signers.sort(key=lambda s: s.outpoint.sort_key())
    blob = bytearray(pack_attestation(signers))
    # Flip the LSB of the second signer's signature inside the blob.
    sig_off = COUNT_SIZE + RECORD_SIZE + OUTPOINT_SIZE + PUBKEY_SIZE
    blob[sig_off] ^= 0x01
    with pytest.raises(BondAttestationError, match="signature 1 failed"):
        verify_attestation(bytes(blob), run_id, 1)


def test_verify_rejects_swapped_signer() -> None:
    """Swapping pubkey ↔ signature across two signers must fail verification.

    This is the surveillance-resistance property: an attacker who
    obtains two valid attestations cannot mix-and-match their components
    without invalidating the BIP340 check.
    """
    run_id = _make_run_id()
    raw = [_gen_signer(seed=s, vout=s) for s in (10, 20)]
    signers = [_build_signed(sk, op, pk, run_id, 1) for sk, op, pk in raw]
    signers.sort(key=lambda s: s.outpoint.sort_key())
    swapped = [
        BondSignerInput(
            outpoint=signers[0].outpoint,
            pubkey=signers[0].pubkey,
            signature=signers[1].signature,
        ),
        BondSignerInput(
            outpoint=signers[1].outpoint,
            pubkey=signers[1].pubkey,
            signature=signers[0].signature,
        ),
    ]
    blob = pack_attestation(swapped)
    with pytest.raises(BondAttestationError, match="failed verification"):
        verify_attestation(blob, run_id, 1)


def test_verify_rejects_wrong_run_id() -> None:
    """A signature from one run must not verify against a different run."""
    run_id_a = _make_run_id(1)
    run_id_b = _make_run_id(2)
    sk, op, pk = _gen_signer(seed=99)
    signer = _build_signed(sk, op, pk, run_id_a, round_no=1)
    blob = pack_attestation([signer])
    with pytest.raises(BondAttestationError, match="failed verification"):
        verify_attestation(blob, run_id_b, round_no=1)


def test_verify_rejects_wrong_round_no() -> None:
    """A signature from one round must not verify against another round."""
    run_id = _make_run_id()
    sk, op, pk = _gen_signer(seed=99)
    signer = _build_signed(sk, op, pk, run_id, round_no=1)
    blob = pack_attestation([signer])
    with pytest.raises(BondAttestationError, match="failed verification"):
        verify_attestation(blob, run_id, round_no=2)


def test_verify_expected_count_mismatch() -> None:
    """Defensive `expected_count` check rejects K-mismatch before verifying signatures."""
    run_id = _make_run_id()
    raw = [_gen_signer(seed=s, vout=s) for s in (1, 2)]
    signers = [_build_signed(sk, op, pk, run_id, 1) for sk, op, pk in raw]
    signers.sort(key=lambda s: s.outpoint.sort_key())
    blob = pack_attestation(signers)
    with pytest.raises(BondAttestationError, match="expected exactly 3"):
        verify_attestation(blob, run_id, 1, expected_count=3)


def test_verify_expected_count_match() -> None:
    """Matching `expected_count` should pass through silently."""
    run_id = _make_run_id()
    raw = [_gen_signer(seed=s, vout=s) for s in (1, 2, 3)]
    signers = [_build_signed(sk, op, pk, run_id, 1) for sk, op, pk in raw]
    signers.sort(key=lambda s: s.outpoint.sort_key())
    blob = pack_attestation(signers)
    out = verify_attestation(blob, run_id, 1, expected_count=3)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# Wire-blob byte layout
# ---------------------------------------------------------------------------


def test_blob_byte_layout_matches_jmp0006() -> None:
    """Spot-check that the packed bytes lie at exactly the spec'd offsets."""
    run_id = _make_run_id()
    raw = [_gen_signer(seed=s, vout=s) for s in (1, 2, 3)]
    signers = [_build_signed(sk, op, pk, run_id, 1) for sk, op, pk in raw]
    signers.sort(key=lambda s: s.outpoint.sort_key())
    blob = pack_attestation(signers)

    assert blob[0] == 3  # bond_count
    for i, s in enumerate(signers):
        off = COUNT_SIZE + i * RECORD_SIZE
        assert blob[off : off + OUTPOINT_SIZE] == s.outpoint.to_wire()
        assert blob[off + OUTPOINT_SIZE : off + OUTPOINT_SIZE + PUBKEY_SIZE] == s.pubkey
        assert blob[off + OUTPOINT_SIZE + PUBKEY_SIZE : off + RECORD_SIZE] == s.signature


# ---------------------------------------------------------------------------
# Cross-check with the bare schnorr primitive
# ---------------------------------------------------------------------------


def test_signature_matches_direct_schnorr_call() -> None:
    """`sign_attestation` is just a thin wrapper; verify the signed bytes line up."""
    run_id = _make_run_id()
    round_no = 7
    sk = (123).to_bytes(32, "big")
    pk = schnorr.derive_xonly_pubkey(sk)
    msg = build_attest_message(run_id, round_no)
    direct = schnorr.sign(sk, msg)
    via_helper = sign_attestation(sk, run_id, round_no)
    # BIP340 with default aux_rand=None is deterministic per BIP340.
    assert direct == via_helper
    assert schnorr.verify(pk, msg, direct)


def test_aux_rand_changes_signature_but_keeps_validity() -> None:
    """Different aux_rand should yield different (still-valid) signatures."""
    run_id = _make_run_id()
    sk = (321).to_bytes(32, "big")
    pk = schnorr.derive_xonly_pubkey(sk)
    sig_none = sign_attestation(sk, run_id, round_no=1)
    sig_fixed = sign_attestation(sk, run_id, round_no=1, aux_rand=b"\xaa" * 32)
    assert sig_none != sig_fixed
    msg = build_attest_message(run_id, round_no=1)
    assert schnorr.verify(pk, msg, sig_none)
    assert schnorr.verify(pk, msg, sig_fixed)


# ---------------------------------------------------------------------------
# Stress: random signer permutations always sort to the same canonical blob
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trial", range(8))
def test_canonical_ordering_is_permutation_invariant(trial: int) -> None:
    """No matter what order the taker collects signatures, the final blob is unique."""
    run_id = _make_run_id(trial + 1)
    raw = [_gen_signer(seed=s + 1 + trial * 10, vout=s) for s in range(5)]
    signers = [_build_signed(sk, op, pk, run_id, 1) for sk, op, pk in raw]
    canonical = sorted(signers, key=lambda s: s.outpoint.sort_key())
    blob_canonical = pack_attestation(canonical)

    # Shuffle deterministically using os.urandom-free permutation.
    rng_seed = hash((trial, "perm")) & 0xFFFF
    permuted = signers[:]
    permuted.sort(key=lambda s: rng_seed ^ int.from_bytes(s.outpoint.txid[:4], "big"))
    permuted.sort(key=lambda s: s.outpoint.sort_key())  # re-canonicalize
    assert pack_attestation(permuted) == blob_canonical


def test_random_high_entropy_round_trip() -> None:
    """Smoke test with cryptographically random material end-to-end."""
    run_id = os.urandom(32)
    round_no = int.from_bytes(os.urandom(2), "big")
    signers = []
    for _ in range(3):
        sk = os.urandom(32)
        # Reject the (vanishingly unlikely) zero/oversize key the same way
        # BIP340 itself would; just retry.
        while int.from_bytes(sk, "big") == 0:
            sk = os.urandom(32)
        pk = schnorr.derive_xonly_pubkey(sk)
        op = BondOutpoint(txid=os.urandom(32), vout=int.from_bytes(os.urandom(4), "big"))
        signers.append(_build_signed(sk, op, pk, run_id, round_no))
    signers.sort(key=lambda s: s.outpoint.sort_key())
    blob = pack_attestation(signers)
    assert verify_attestation(blob, run_id, round_no, expected_count=3) == signers
