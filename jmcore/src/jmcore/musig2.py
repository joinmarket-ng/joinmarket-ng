"""JoinMarket adapters for python-bitcointx's native BIP327 MuSig2 API."""

from __future__ import annotations

from bitcointx.core.musig import (
    KeyAggContext,
    MuSig2Error,
    SecNonce,
    Session,
    apply_xonly_tweak,
    get_session,
    key_agg,
    nonce_agg,
    nonce_gen,
    partial_sig_agg,
    sign_partial,
)
from bitcointx.core.musig import (
    partial_sig_verify as _partial_sig_verify,
)


def partial_sig_verify(psig: bytes, pubnonce: bytes, pubkey: bytes, session: Session) -> bool:
    """Verify a partial signature without propagating malformed peer data."""
    try:
        return _partial_sig_verify(psig, pubnonce, pubkey, session)
    except MuSig2Error:
        return False


__all__ = (
    "MuSig2Error",
    "KeyAggContext",
    "SecNonce",
    "Session",
    "key_agg",
    "apply_xonly_tweak",
    "nonce_gen",
    "nonce_agg",
    "get_session",
    "sign_partial",
    "partial_sig_verify",
    "partial_sig_agg",
)
