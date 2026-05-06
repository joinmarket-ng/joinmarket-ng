"""BIP340 Schnorr signatures over secp256k1.

Thin typed wrapper around ``coincurve``'s ``sign_schnorr`` /
``PublicKeyXOnly.verify`` (which themselves wrap libsecp256k1's
audited BIP340 implementation). The primary consumer is the JMP-0005
bond attestation layer, which signs and verifies 32-byte challenge
hashes.

Why a wrapper instead of using coincurve directly?
    * Coincurve exposes ``PrivateKey.sign_schnorr(msg, aux_randomness)``
      and ``PublicKeyXOnly.verify(sig, msg)``. Those return / accept
      raw ``bytes``; the wrapper enforces sizes (32-byte secret key,
      32-byte x-only pubkey, 32-byte message, 32-byte aux randomness,
      64-byte signature) so a length mistake fails fast at the call
      site instead of producing a confusing libsecp error string.
    * BIP340 only signs 32-byte messages. Higher layers that want to
      sign variable-length payloads MUST pre-hash with a JoinMarket-
      specific tagged hash (see :func:`tagged_hash`).
    * Aux randomness is an explicit keyword argument here; ``None``
      maps to coincurve's ``None`` (deterministic per BIP340 spec) and
      ``bytes`` of length 32 maps to coincurve's same-shape argument.
      Coincurve's empty-bytestring "auto-generated" mode is not
      exposed: callers either commit to determinism or supply their
      own randomness, never hand off entropy responsibility implicitly.
"""

from __future__ import annotations

import hashlib
from typing import Final

from coincurve import PrivateKey, PublicKeyXOnly

# secp256k1 group order n.
_GROUP_ORDER: Final[int] = 0xFFFFFFFF_FFFFFFFF_FFFFFFFF_FFFFFFFE_BAAEDCE6_AF48A03B_BFD25E8C_D0364141


class SchnorrError(ValueError):
    """Raised when a Schnorr operation rejects its input."""


def _check_secret_key(sk: bytes) -> None:
    if len(sk) != 32:
        raise SchnorrError(f"secret key must be 32 bytes, got {len(sk)}")
    n = int.from_bytes(sk, "big")
    if n == 0 or n >= _GROUP_ORDER:
        # Matches BIP340: the secret key must be in [1, n-1]. coincurve
        # would also reject this but with a less specific message.
        raise SchnorrError("secret key must be in [1, n-1]")


def _check_message(msg: bytes) -> None:
    if len(msg) != 32:
        raise SchnorrError(f"message must be 32 bytes, got {len(msg)}")


def _check_xonly(pk: bytes) -> None:
    if len(pk) != 32:
        raise SchnorrError(f"x-only public key must be 32 bytes, got {len(pk)}")


def _check_signature(sig: bytes) -> None:
    if len(sig) != 64:
        raise SchnorrError(f"signature must be 64 bytes, got {len(sig)}")


def derive_xonly_pubkey(secret_key: bytes) -> bytes:
    """Return the 32-byte BIP340 x-only public key for *secret_key*.

    The y-parity flip required by BIP340 (negate the secret key when
    the corresponding pubkey has odd y) is performed implicitly by
    libsecp256k1 inside ``sign``. Callers who need the x-only pubkey
    only for verification do not need to track parity themselves.
    """
    _check_secret_key(secret_key)
    pk_xonly = PublicKeyXOnly.from_secret(secret_key)
    return pk_xonly.format()


def sign(secret_key: bytes, message: bytes, *, aux_rand: bytes | None = None) -> bytes:
    """Produce a 64-byte BIP340 Schnorr signature.

    *message* MUST be exactly 32 bytes; higher-layer protocols that
    want to authenticate a longer payload should pre-hash it with
    :func:`tagged_hash` (or any other domain-separated hash that
    yields a 32-byte digest).

    *aux_rand*:
        ``None`` selects deterministic signing (BIP340 default).
        ``bytes`` of length 32 supplies fresh randomness, which is the
        BIP340-recommended mode for production deployments because it
        masks side channels from the secret key. Any other length is
        rejected.
    """
    _check_secret_key(secret_key)
    _check_message(message)
    if aux_rand is not None and len(aux_rand) != 32:
        raise SchnorrError(f"aux_rand must be 32 bytes or None, got {len(aux_rand)}")
    sk = PrivateKey(secret_key)
    return sk.sign_schnorr(message, aux_randomness=aux_rand)


def verify(xonly_pubkey: bytes, message: bytes, signature: bytes) -> bool:
    """Verify a 64-byte BIP340 Schnorr signature.

    Returns ``True`` if and only if the signature is valid under the
    given x-only public key and 32-byte message. Returns ``False``
    (instead of raising) on cryptographic failure -- malformed inputs
    (wrong-length pubkey/sig/msg) raise :class:`SchnorrError` so that
    encoding bugs are loud while honest mismatches stay quiet.

    All-zero / out-of-range pubkeys, signatures with ``r >= p`` or
    ``s >= n``, and pubkeys that are not on the curve are rejected by
    libsecp256k1 and surface as ``False`` here, matching the BIP340
    "verification result" semantics in the canonical test vectors.
    """
    _check_xonly(xonly_pubkey)
    _check_message(message)
    _check_signature(signature)
    try:
        pk = PublicKeyXOnly(xonly_pubkey)
    except ValueError:
        # Pubkey is not a valid x-coordinate on the curve. BIP340
        # treats this as a verification failure, not an exception.
        return False
    try:
        return bool(pk.verify(signature, message))
    except ValueError:
        # Signature length is the only ValueError coincurve raises
        # here, and we already enforced 64 bytes -- treat any
        # remaining ValueError as a hard rejection.
        return False


def tagged_hash(tag: str, *parts: bytes) -> bytes:
    """BIP340 tagged hash: ``SHA256(SHA256(tag) || SHA256(tag) || data)``.

    The *tag* is encoded as UTF-8. *parts* are concatenated in order
    to form the data; this avoids forcing every caller to do the
    concatenation by hand and to maintain a stable canonical encoding
    for multi-field payloads.

    Returned digest is 32 bytes, suitable for use as ``message`` in
    :func:`sign` / :func:`verify`.
    """
    tag_hash = hashlib.sha256(tag.encode("utf-8")).digest()
    h = hashlib.sha256()
    h.update(tag_hash)
    h.update(tag_hash)
    for part in parts:
        h.update(part)
    return h.digest()
