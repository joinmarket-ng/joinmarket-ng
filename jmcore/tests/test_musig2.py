"""Tests for the native python-bitcointx BIP327 MuSig2 adapter."""

from __future__ import annotations

import pytest
from bitcointx.core.key import CKey, XOnlyPubKey

from jmcore.bitcoin import tagged_hash
from jmcore.musig2 import (
    MuSig2Error,
    apply_xonly_tweak,
    get_session,
    key_agg,
    nonce_agg,
    nonce_gen,
    partial_sig_agg,
    partial_sig_verify,
    sign_partial,
)

BIP327_PUBKEYS = [
    bytes.fromhex("02F9308A019258C31049344F85F89D5229B531C845836F99B08601F113BCE036F9"),
    bytes.fromhex("03DFF1D77F2A671C5F36183726DB2341BE58FEAE1DA2DECED843240F7B502BA659"),
    bytes.fromhex("023590A94E768F8E1815C2F24B4D80A8E3149316C3518CE7B7AD338368D038CA66"),
]
BIP327_KEYAGG_CASES = [
    ([0, 1, 2], "90539EEDE565F5D054F32CC0C220126889ED1E5D193BAF15AEF344FE59D4610C"),
    ([2, 1, 0], "6204DE8B083426DC6EAF9502D27024D53FC826BF7D2012148A0575435DF54B2B"),
    ([0, 0, 0], "B436E3BAD62B8CD409969A224731C193D051162D8C5AE8B109306127DA3AA935"),
    ([0, 0, 1, 1], "69BC22BFA5D106306E48A20679DE1D7389386124D07571D0D872686028C26A3E"),
]


def _key(value: int) -> CKey:
    return CKey(value.to_bytes(32, "big"))


@pytest.mark.parametrize("indices,expected", BIP327_KEYAGG_CASES)
def test_bip327_key_agg(indices: list[int], expected: str) -> None:
    keys = [BIP327_PUBKEYS[i] for i in indices]
    assert key_agg(keys).aggregate_xonly().hex().upper() == expected


def test_key_agg_order_matters() -> None:
    a = key_agg([BIP327_PUBKEYS[0], BIP327_PUBKEYS[1]]).aggregate_xonly()
    b = key_agg([BIP327_PUBKEYS[1], BIP327_PUBKEYS[0]]).aggregate_xonly()
    assert a != b


def test_key_agg_rejects_bad_pubkey() -> None:
    with pytest.raises(MuSig2Error):
        key_agg([b"\x02" + b"\x00" * 31])


def _two_of_two_sign(msg: bytes, tweaks: list[bytes]) -> tuple[bytes, bytes]:
    sk1, sk2 = _key(1), _key(2)
    pk1, pk2 = bytes(sk1.pub), bytes(sk2.pub)
    pubkeys = [pk1, pk2]
    sn1, pn1 = nonce_gen(pk1, b"\x11" * 32)
    sn2, pn2 = nonce_gen(pk2, b"\x22" * 32)
    session = get_session(nonce_agg([pn1, pn2]), pubkeys, tweaks, msg)
    ps1 = sign_partial(sn1, sk1.secret_bytes, session)
    ps2 = sign_partial(sn2, sk2.secret_bytes, session)
    assert partial_sig_verify(ps1, pn1, pk1, session)
    assert partial_sig_verify(ps2, pn2, pk2, session)
    return partial_sig_agg([ps1, ps2], session), session.ctx.aggregate_xonly()


def test_musig2_sign_roundtrip_untweaked() -> None:
    msg = b"\x42" * 32
    sig, agg_xonly = _two_of_two_sign(msg, [])
    assert len(sig) == 64
    assert XOnlyPubKey(agg_xonly).verify_schnorr(msg, sig)


def test_musig2_sign_roundtrip_with_taptweak() -> None:
    msg = b"\x55" * 32
    base = key_agg([bytes(_key(3).pub), bytes(_key(4).pub)]).aggregate_xonly()
    tweak = tagged_hash("TapTweak", base + b"\x11" * 32)
    sig, agg_xonly = _two_of_two_sign(msg, [tweak])
    assert XOnlyPubKey(agg_xonly).verify_schnorr(msg, sig)


def test_partial_sig_verify_rejects_wrong_or_malformed_nonce() -> None:
    msg = b"\x01" * 32
    sk1, sk2 = _key(1), _key(2)
    pk1, pk2 = bytes(sk1.pub), bytes(sk2.pub)
    sn1, pn1 = nonce_gen(pk1, b"\x33" * 32)
    _, pn2 = nonce_gen(pk2, b"\x44" * 32)
    session = get_session(nonce_agg([pn1, pn2]), [pk1, pk2], [], msg)
    ps1 = sign_partial(sn1, sk1.secret_bytes, session)
    assert not partial_sig_verify(ps1, pn2, pk1, session)
    assert not partial_sig_verify(b"\xff" * 32, pn1, pk1, session)


def test_secret_nonce_is_single_use() -> None:
    sk1, sk2 = _key(1), _key(2)
    pk1, pk2 = bytes(sk1.pub), bytes(sk2.pub)
    secnonce, pubnonce = nonce_gen(pk1, b"\x55" * 32)
    _, peer_nonce = nonce_gen(pk2, b"\x66" * 32)
    session = get_session(nonce_agg([pubnonce, peer_nonce]), [pk1, pk2], [], b"\x77" * 32)

    sign_partial(secnonce, sk1.secret_bytes, session)
    with pytest.raises(MuSig2Error, match="already been consumed"):
        sign_partial(secnonce, sk1.secret_bytes, session)


def test_apply_xonly_tweak_out_of_range() -> None:
    from jmcore.constants import SECP256K1_N

    ctx = key_agg([BIP327_PUBKEYS[0], BIP327_PUBKEYS[1]])
    with pytest.raises(MuSig2Error):
        apply_xonly_tweak(ctx, SECP256K1_N.to_bytes(32, "big"))
    with pytest.raises(MuSig2Error):
        apply_xonly_tweak(ctx, b"\x00" * 31)
