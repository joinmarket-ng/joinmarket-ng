"""
BIP327 MuSig2 multi-signatures over secp256k1.

A from-scratch, dependency-light implementation (built on ``coincurve`` for the
elliptic-curve arithmetic) of the parts of BIP327 needed by the JoinMarket swap
protocol: key aggregation, x-only tweaking (for the BIP341 TapTweak), nonce
generation/aggregation, partial signing and partial-signature aggregation.

The aggregate signature produced by :func:`partial_sig_agg` is an ordinary
64-byte BIP340 Schnorr signature, verifiable under the (tweaked) aggregate
x-only public key, which is what makes a cooperative Taproot swap spend
indistinguishable from any other key-path P2TR spend.

Reference: https://github.com/bitcoin/bips/blob/master/bip-0327.mediawiki
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import TypeAlias

from coincurve import PrivateKey, PublicKey

from jmcore.bitcoin import tagged_hash
from jmcore.constants import SECP256K1_N as N

# secp256k1 field/group helpers ------------------------------------------------

# The point at infinity is represented as ``None``.
Point: TypeAlias = PublicKey | None

_G = PublicKey.from_valid_secret((1).to_bytes(32, "big"))


class MuSig2Error(Exception):
    """Raised on invalid MuSig2 inputs or protocol violations."""


def _cpoint(data: bytes) -> PublicKey:
    if len(data) != 33:
        raise MuSig2Error("expected 33-byte compressed point")
    try:
        return PublicKey(data)
    except Exception as exc:  # noqa: BLE001
        raise MuSig2Error("invalid point encoding") from exc


def _cbytes(point: Point) -> bytes:
    if point is None:
        raise MuSig2Error("cannot serialize the point at infinity")
    return point.format(compressed=True)


def _xbytes(point: Point) -> bytes:
    return _cbytes(point)[1:]


def _has_even_y(point: Point) -> bool:
    return _cbytes(point)[0] == 0x02


def _point_negate(point: Point) -> Point:
    if point is None:
        return None
    return point.multiply((N - 1).to_bytes(32, "big"))


def _point_add(a: Point, b: Point) -> Point:
    if a is None:
        return b
    if b is None:
        return a
    try:
        return a.combine([b])
    except ValueError:
        # a + (-a) = infinity
        return None


def _point_mul(point: Point, scalar: int) -> Point:
    scalar %= N
    if point is None or scalar == 0:
        return None
    return point.multiply(scalar.to_bytes(32, "big"))


def _scalar_base_mul(scalar: int) -> Point:
    scalar %= N
    if scalar == 0:
        return None
    return PublicKey.from_valid_secret(scalar.to_bytes(32, "big"))


def _int(data: bytes) -> int:
    return int.from_bytes(data, "big")


def _bytes32(value: int) -> bytes:
    return (value % N).to_bytes(32, "big")


# Key aggregation --------------------------------------------------------------


@dataclass
class KeyAggContext:
    """Aggregate key context: the aggregate point ``Q`` and tweak accumulators."""

    Q: PublicKey
    gacc: int
    tacc: int

    def aggregate_xonly(self) -> bytes:
        return _xbytes(self.Q)

    def aggregate_pubkey(self) -> bytes:
        return _cbytes(self.Q)


def _hash_keys(pubkeys: list[bytes]) -> bytes:
    return tagged_hash("KeyAgg list", b"".join(pubkeys))


def _get_second_key(pubkeys: list[bytes]) -> bytes:
    first = pubkeys[0]
    for pk in pubkeys:
        if pk != first:
            return pk
    return b"\x00" * 33


def _key_agg_coeff(pubkeys: list[bytes], pk: bytes, pk2: bytes, ell: bytes) -> int:
    if pk == pk2:
        return 1
    return _int(tagged_hash("KeyAgg coefficient", ell + pk)) % N


def key_agg(pubkeys: list[bytes]) -> KeyAggContext:
    """Aggregate a list of 33-byte compressed public keys (BIP327 ``KeyAgg``).

    The order is significant and is preserved (callers that need the Boltz
    convention put the server key first).
    """
    if not pubkeys:
        raise MuSig2Error("need at least one public key")
    for pk in pubkeys:
        if len(pk) != 33:
            raise MuSig2Error("public keys must be 33-byte compressed")
    ell = _hash_keys(pubkeys)
    pk2 = _get_second_key(pubkeys)
    Q: Point = None
    for pk in pubkeys:
        a = _key_agg_coeff(pubkeys, pk, pk2, ell)
        Q = _point_add(Q, _point_mul(_cpoint(pk), a))
    if Q is None:
        raise MuSig2Error("aggregate key is the point at infinity")
    return KeyAggContext(Q=Q, gacc=1, tacc=0)


def apply_xonly_tweak(ctx: KeyAggContext, tweak: bytes) -> KeyAggContext:
    """Apply a BIP341 x-only tweak (e.g. the TapTweak merkle commitment)."""
    if len(tweak) != 32:
        raise MuSig2Error("tweak must be 32 bytes")
    t = _int(tweak)
    if t >= N:
        raise MuSig2Error("tweak is out of range")
    g = 1 if _has_even_y(ctx.Q) else N - 1
    Q = _point_add(_point_mul(ctx.Q, g), _scalar_base_mul(t))
    if Q is None:
        raise MuSig2Error("tweaked aggregate key is the point at infinity")
    return KeyAggContext(Q=Q, gacc=(g * ctx.gacc) % N, tacc=(t + g * ctx.tacc) % N)


# Nonces -----------------------------------------------------------------------


@dataclass
class SecNonce:
    """A secret nonce pair (k1, k2). Never reuse or persist across sessions."""

    k1: int
    k2: int
    pubkey: bytes  # the signer's 33-byte public key, bound to the nonce


def nonce_gen(pubkey: bytes, rand: bytes | None = None) -> tuple[SecNonce, bytes]:
    """Generate a secret/public nonce pair. Returns ``(secnonce, pubnonce)``.

    ``pubnonce`` is 66 bytes: two 33-byte compressed points ``R1 || R2``.
    """
    if len(pubkey) != 33:
        raise MuSig2Error("public key must be 33-byte compressed")
    rand = rand if rand is not None else secrets.token_bytes(32)
    k1 = 1 + _int(tagged_hash("MuSig2 nonce k1", rand + pubkey)) % (N - 1)
    k2 = 1 + _int(tagged_hash("MuSig2 nonce k2", rand + pubkey)) % (N - 1)
    r1 = _scalar_base_mul(k1)
    r2 = _scalar_base_mul(k2)
    pubnonce = _cbytes(r1) + _cbytes(r2)
    return SecNonce(k1=k1, k2=k2, pubkey=pubkey), pubnonce


def nonce_agg(pubnonces: list[bytes]) -> bytes:
    """Aggregate signers' public nonces into a single 66-byte aggregate nonce."""
    if not pubnonces:
        raise MuSig2Error("need at least one public nonce")
    out = b""
    for j in (0, 1):
        agg: Point = None
        for pn in pubnonces:
            if len(pn) != 66:
                raise MuSig2Error("public nonce must be 66 bytes")
            agg = _point_add(agg, _cpoint(pn[j * 33 : j * 33 + 33]))
        # Per BIP327, an infinity component is serialised as all-zero; we use the
        # generator placeholder which the session-value step treats as infinity.
        out += b"\x00" * 33 if agg is None else _cbytes(agg)
    return out


# Session values ---------------------------------------------------------------


@dataclass
class Session:
    ctx: KeyAggContext
    b: int
    R: PublicKey
    e: int
    pubkeys: list[bytes]
    pk2: bytes
    ell: bytes


def _aggnonce_point(data: bytes, j: int) -> Point:
    chunk = data[j * 33 : j * 33 + 33]
    if chunk == b"\x00" * 33:
        return None
    return _cpoint(chunk)


def get_session(aggnonce: bytes, pubkeys: list[bytes], tweaks: list[bytes], msg: bytes) -> Session:
    """Compute the per-session values (b, R, e) shared by all signers."""
    if len(aggnonce) != 66:
        raise MuSig2Error("aggregate nonce must be 66 bytes")
    ctx = key_agg(pubkeys)
    for tw in tweaks:
        ctx = apply_xonly_tweak(ctx, tw)
    q_x = _xbytes(ctx.Q)
    b = _int(tagged_hash("MuSig2 nonce coefficient", aggnonce + q_x + msg)) % N
    r1 = _aggnonce_point(aggnonce, 0)
    r2 = _aggnonce_point(aggnonce, 1)
    R = _point_add(r1, _point_mul(r2, b))
    if R is None:
        R = _G  # BIP327: use G as the effective nonce point if R is infinity
    e = _int(tagged_hash("BIP0340/challenge", _xbytes(R) + q_x + msg)) % N
    ell = _hash_keys(pubkeys)
    pk2 = _get_second_key(pubkeys)
    return Session(ctx=ctx, b=b, R=R, e=e, pubkeys=pubkeys, pk2=pk2, ell=ell)


def sign_partial(secnonce: SecNonce, privkey: bytes, session: Session) -> bytes:
    """Produce this signer's 32-byte partial signature."""
    k1, k2 = secnonce.k1, secnonce.k2
    if not _has_even_y(session.R):
        k1, k2 = N - k1, N - k2
    d_ = _int(privkey) % N
    if d_ == 0:
        raise MuSig2Error("invalid signing key")
    if PrivateKey(privkey).public_key.format(compressed=True) != secnonce.pubkey:
        raise MuSig2Error("nonce pubkey does not match signing key")
    g = 1 if _has_even_y(session.ctx.Q) else N - 1
    d = (g * session.ctx.gacc * d_) % N
    a = _key_agg_coeff(session.pubkeys, secnonce.pubkey, session.pk2, session.ell)
    s = (k1 + session.b * k2 + session.e * a * d) % N
    # Sanity check (catches a wrong nonce/key pairing before it hits the chain).
    if not _partial_verify_internal(s, secnonce.pubkey, _point_from_secnonce(secnonce), session):
        raise MuSig2Error("internal partial signature verification failed")
    return _bytes32(s)


def _point_from_secnonce(secnonce: SecNonce) -> bytes:
    r1 = _scalar_base_mul(secnonce.k1)
    r2 = _scalar_base_mul(secnonce.k2)
    return _cbytes(r1) + _cbytes(r2)


def _partial_verify_internal(s: int, pubkey: bytes, pubnonce: bytes, session: Session) -> bool:
    R1 = _cpoint(pubnonce[0:33])
    R2 = _cpoint(pubnonce[33:66])
    Re = _point_add(R1, _point_mul(R2, session.b))
    if not _has_even_y(session.R):
        Re = _point_negate(Re)
    a = _key_agg_coeff(session.pubkeys, pubkey, session.pk2, session.ell)
    g = 1 if _has_even_y(session.ctx.Q) else N - 1
    g_ = (g * session.ctx.gacc) % N
    P = _point_mul(_cpoint(pubkey), g_)
    lhs = _scalar_base_mul(s)
    rhs = _point_add(Re, _point_mul(P, (session.e * a) % N))
    return _cbytes(lhs) == _cbytes(rhs) if (lhs is not None and rhs is not None) else lhs is rhs


def partial_sig_verify(psig: bytes, pubnonce: bytes, pubkey: bytes, session: Session) -> bool:
    """Verify another signer's partial signature."""
    if len(psig) != 32:
        return False
    s = _int(psig)
    if s >= N:
        return False
    return _partial_verify_internal(s, pubkey, pubnonce, session)


def partial_sig_agg(psigs: list[bytes], session: Session) -> bytes:
    """Aggregate partial signatures into a 64-byte BIP340 Schnorr signature."""
    s = 0
    for psig in psigs:
        if len(psig) != 32:
            raise MuSig2Error("partial signature must be 32 bytes")
        si = _int(psig)
        if si >= N:
            raise MuSig2Error("partial signature out of range")
        s = (s + si) % N
    g = 1 if _has_even_y(session.ctx.Q) else N - 1
    s = (s + session.e * g * session.ctx.tacc) % N
    return _xbytes(session.R) + _bytes32(s)
