"""BIP340 conformance + API surface tests for :mod:`jmcore.schnorr`.

Vectors 0-14 from the canonical BIP340 ``test-vectors.csv``
(https://github.com/bitcoin/bips/blob/master/bip-0340/test-vectors.csv)
are inlined verbatim. Vectors 15-18 use variable-length messages and
are deliberately omitted: :func:`jmcore.schnorr.sign` / ``verify``
restrict ``message`` to 32 bytes (the only shape JMP-0005 / JMP-0006
need), so those vectors do not apply to this wrapper.

A separate set of API-shape tests exercise the wrapper's input
validation -- the surface that exists *because* of the wrapper rather
than the underlying libsecp256k1 binding.
"""

from __future__ import annotations

import hashlib

import pytest

import jmcore.schnorr as schnorr

# ---------------------------------------------------------------------------
# BIP340 conformance vectors (index 0-14 from the canonical CSV).
# Each tuple is (index, secret_key_hex, public_key_hex, aux_rand_hex,
# message_hex, signature_hex, expect_valid, comment).
# Empty secret_key_hex means the row is verify-only.
# ---------------------------------------------------------------------------
BIP340_VECTORS: list[tuple[int, str, str, str, str, str, bool, str]] = [
    (
        0,
        "0000000000000000000000000000000000000000000000000000000000000003",
        "F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9",
        "0000000000000000000000000000000000000000000000000000000000000000",
        "0000000000000000000000000000000000000000000000000000000000000000",
        "E907831F80848D1069A5371B402410364BDF1C5F8307B0084C55F1CE2DCA821525F66A4A85EA8B71E482A74F382D2CE5EBEEE8FDB2172F477DF4900D310536C0",
        True,
        "",
    ),
    (
        1,
        "B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "0000000000000000000000000000000000000000000000000000000000000001",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "6896BD60EEAE296DB48A229FF71DFE071BDE413E6D43F917DC8DCF8C78DE33418906D11AC976ABCCB20B091292BFF4EA897EFCB639EA871CFA95F6DE339E4B0A",
        True,
        "",
    ),
    (
        2,
        "C90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74020BBEA63B14E5C9",
        "DD308AFEC5777E13121FA72B9CC1B7CC0139715309B086C960E18FD969774EB8",
        "C87AA53824B4D7AE2EB035A2B5BBBCCC080E76CDC6D1692C4B0B62D798E6D906",
        "7E2D58D8B3BCDF1ABADEC7829054F90DDA9805AAB56C77333024B9D0A508B75C",
        "5831AAEED7B44BB74E5EAB94BA9D4294C49BCF2A60728D8B4C200F50DD313C1BAB745879A5AD954A72C45A91C3A51D3C7ADEA98D82F8481E0E1E03674A6F3FB7",
        True,
        "",
    ),
    (
        3,
        "0B432B2677937381AEF05BB02A66ECD012773062CF3FA2549E44F58ED2401710",
        "25D1DFF95105F5253C4022F628A996AD3A0D95FBF21D468A1B33F8C160D8F517",
        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF",
        "7EB0509757E246F19449885651611CB965ECC1A187DD51B64FDA1EDC9637D5EC97582B9CB13DB3933705B32BA982AF5AF25FD78881EBB32771FC5922EFC66EA3",
        True,
        "test fails if msg is reduced modulo p or n",
    ),
    (
        4,
        "",
        "D69C3509BB99E412E68B0FE8544E72837DFA30746D8BE2AA65975F29D22DC7B9",
        "",
        "4DF3C3F68FCC83B27E9D42C90431A72499F17875C81A599B566C9889B9696703",
        "00000000000000000000003B78CE563F89A0ED9414F5AA28AD0D96D6795F9C6376AFB1548AF603B3EB45C9F8207DEE1060CB71C04E80F593060B07D28308D7F4",
        True,
        "",
    ),
    (
        5,
        "",
        "EEFDEA4CDB677750A420FEE807EACF21EB9898AE79B9768766E4FAA04A2D4A34",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E17776969E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
        False,
        "public key not on the curve",
    ),
    (
        6,
        "",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "FFF97BD5755EEEA420453A14355235D382F6472F8568A18B2F057A14602975563CC27944640AC607CD107AE10923D9EF7A73C643E166BE5EBEAFA34B1AC553E2",
        False,
        "has_even_y(R) is false",
    ),
    (
        7,
        "",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "1FA62E331EDBC21C394792D2AB1100A7B432B013DF3F6FF4F99FCB33E0E1515F28890B3EDB6E7189B630448B515CE4F8622A954CFE545735AAEA5134FCCDB2BD",
        False,
        "negated message",
    ),
    (
        8,
        "",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E177769961764B3AA9B2FFCB6EF947B6887A226E8D7C93E00C5ED0C1834FF0D0C2E6DA6",
        False,
        "negated s value",
    ),
    (
        9,
        "",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "0000000000000000000000000000000000000000000000000000000000000000123DDA8328AF9C23A94C1FEECFD123BA4FB73476F0D594DCB65C6425BD186051",
        False,
        "sG - eP is infinite (R.x == 0)",
    ),
    (
        10,
        "",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "00000000000000000000000000000000000000000000000000000000000000017615FBAF5AE28864013C099742DEADB4DBA87F11AC6754F93780D5A1837CF197",
        False,
        "sG - eP is infinite (R.x == 1)",
    ),
    (
        11,
        "",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "4A298DACAE57395A15D0795DDBFD1DCB564DA82B0F269BC70A74F8220429BA1D69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
        False,
        "sig[0:32] is not an X coordinate on the curve",
    ),
    (
        12,
        "",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F69E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
        False,
        "sig[0:32] is equal to field size",
    ),
    (
        13,
        "",
        "DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E177769FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141",
        False,
        "sig[32:64] is equal to curve order",
    ),
    (
        14,
        "",
        "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC30",
        "",
        "243F6A8885A308D313198A2E03707344A4093822299F31D0082EFA98EC4E6C89",
        "6CFF5C3BA86C69EA4B7376F31A9BCB4F74C1976089B2D9963DA2E5543E17776969E89B4C5564D00349106B8497785DD7D1D713A8AE82B32FA79D5F7FC407D39B",
        False,
        "public key X coordinate exceeds field size",
    ),
]


@pytest.mark.parametrize("vector", BIP340_VECTORS, ids=lambda v: f"bip340-{v[0]}")
def test_bip340_verify(vector: tuple[int, str, str, str, str, str, bool, str]) -> None:
    """Every canonical 32-byte-message BIP340 vector verifies as expected."""
    _, _sk_hex, pk_hex, _aux_hex, msg_hex, sig_hex, expect_valid, _comment = vector
    pk = bytes.fromhex(pk_hex)
    msg = bytes.fromhex(msg_hex)
    sig = bytes.fromhex(sig_hex)
    assert schnorr.verify(pk, msg, sig) is expect_valid


@pytest.mark.parametrize(
    "vector",
    [v for v in BIP340_VECTORS if v[1]],
    ids=lambda v: f"bip340-sign-{v[0]}",
)
def test_bip340_sign_round_trip(
    vector: tuple[int, str, str, str, str, str, bool, str],
) -> None:
    """Sign-then-verify reproduces the canonical signature for each vector."""
    _, sk_hex, pk_hex, aux_hex, msg_hex, sig_hex, expect_valid, _comment = vector
    assert expect_valid, "sign vectors must be verifiable"
    sk = bytes.fromhex(sk_hex)
    msg = bytes.fromhex(msg_hex)
    aux = bytes.fromhex(aux_hex) if aux_hex else None
    sig = schnorr.sign(sk, msg, aux_rand=aux)
    assert sig.hex().upper() == sig_hex
    pk = bytes.fromhex(pk_hex)
    assert schnorr.verify(pk, msg, sig)


# ---------------------------------------------------------------------------
# Wrapper-surface tests (not part of BIP340 itself).
# ---------------------------------------------------------------------------


def test_derive_xonly_pubkey_matches_vector_1() -> None:
    sk = bytes.fromhex("B7E151628AED2A6ABF7158809CF4F3C762E7160F38B4DA56A784D9045190CFEF")
    expected = bytes.fromhex("DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659")
    assert schnorr.derive_xonly_pubkey(sk) == expected


def test_sign_rejects_secret_key_zero() -> None:
    with pytest.raises(schnorr.SchnorrError, match="in \\[1, n-1\\]"):
        schnorr.sign(bytes(32), bytes(32))


def test_sign_rejects_secret_key_at_or_above_curve_order() -> None:
    n_bytes = (0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141).to_bytes(
        32, "big"
    )
    with pytest.raises(schnorr.SchnorrError, match="in \\[1, n-1\\]"):
        schnorr.sign(n_bytes, bytes(32))


@pytest.mark.parametrize("bad_len", [0, 16, 31, 33, 64])
def test_sign_rejects_non_32_byte_message(bad_len: int) -> None:
    sk = (1).to_bytes(32, "big")
    with pytest.raises(schnorr.SchnorrError, match="message must be 32 bytes"):
        schnorr.sign(sk, b"\x00" * bad_len)


@pytest.mark.parametrize("bad_len", [0, 16, 31, 33, 64])
def test_sign_rejects_wrong_aux_rand_length(bad_len: int) -> None:
    sk = (1).to_bytes(32, "big")
    with pytest.raises(schnorr.SchnorrError, match="aux_rand must be 32 bytes"):
        schnorr.sign(sk, bytes(32), aux_rand=b"\x00" * bad_len)


def test_sign_accepts_aux_rand_none() -> None:
    """Deterministic-mode signing works without aux randomness."""
    sk = (1).to_bytes(32, "big")
    msg = bytes(32)
    sig = schnorr.sign(sk, msg, aux_rand=None)
    assert len(sig) == 64
    pk = schnorr.derive_xonly_pubkey(sk)
    assert schnorr.verify(pk, msg, sig)


def test_verify_rejects_wrong_pubkey_length() -> None:
    with pytest.raises(schnorr.SchnorrError, match="x-only public key must be 32 bytes"):
        schnorr.verify(b"\x00" * 33, bytes(32), bytes(64))


def test_verify_rejects_wrong_signature_length() -> None:
    with pytest.raises(schnorr.SchnorrError, match="signature must be 64 bytes"):
        schnorr.verify(bytes(32), bytes(32), bytes(63))


def test_verify_rejects_wrong_message_length() -> None:
    with pytest.raises(schnorr.SchnorrError, match="message must be 32 bytes"):
        schnorr.verify(bytes(32), bytes(31), bytes(64))


def test_tampered_signature_fails_verification() -> None:
    sk = (42).to_bytes(32, "big")
    msg = b"x" * 32
    sig = schnorr.sign(sk, msg, aux_rand=bytes(32))
    pk = schnorr.derive_xonly_pubkey(sk)
    assert schnorr.verify(pk, msg, sig)
    # Flip a bit in s; signature should no longer verify.
    tampered = bytearray(sig)
    tampered[-1] ^= 0x01
    assert not schnorr.verify(pk, msg, bytes(tampered))


def test_signature_is_message_bound() -> None:
    sk = (42).to_bytes(32, "big")
    msg_a = b"a" * 32
    msg_b = b"b" * 32
    sig = schnorr.sign(sk, msg_a, aux_rand=bytes(32))
    pk = schnorr.derive_xonly_pubkey(sk)
    assert schnorr.verify(pk, msg_a, sig)
    assert not schnorr.verify(pk, msg_b, sig)


# ---------------------------------------------------------------------------
# tagged_hash tests
# ---------------------------------------------------------------------------


def test_tagged_hash_matches_manual_construction() -> None:
    tag = "TestTag"
    parts = (b"alpha", b"beta", b"gamma")
    th = hashlib.sha256(tag.encode()).digest()
    expected = hashlib.sha256(th + th + b"alphabetagamma").digest()
    assert schnorr.tagged_hash(tag, *parts) == expected


def test_tagged_hash_different_tags_produce_different_digests() -> None:
    msg = b"same payload"
    a = schnorr.tagged_hash("jmng/foo", msg)
    b = schnorr.tagged_hash("jmng/bar", msg)
    assert a != b


def test_tagged_hash_concatenation_order_matters() -> None:
    a = schnorr.tagged_hash("jmng/test", b"first", b"second")
    b = schnorr.tagged_hash("jmng/test", b"second", b"first")
    assert a != b


def test_tagged_hash_no_parts_is_valid() -> None:
    """A zero-part call hashes only the doubled tag prefix."""
    digest = schnorr.tagged_hash("jmng/test")
    assert len(digest) == 32


def test_tagged_hash_output_usable_as_signing_message() -> None:
    """End-to-end: tagged_hash output is a valid 32-byte signing message."""
    sk = (123).to_bytes(32, "big")
    msg = schnorr.tagged_hash("jmng/bond_attestation_v1", b"payload", b"extra")
    sig = schnorr.sign(sk, msg, aux_rand=bytes(32))
    pk = schnorr.derive_xonly_pubkey(sk)
    assert schnorr.verify(pk, msg, sig)
