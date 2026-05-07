"""Taker-side decoding/collection for tx-extension !attest replies.

Pure functions and a thin async helper around
:class:`jmcore.attestation_collector.AttestationCollector`. This layer
is deliberately I/O-free aside from the fan-out/wait helper, so it can
be unit-tested without spinning up a full :class:`Taker` instance.

The matching outbound issuer (sending !attestreq to selected makers
and orchestrating the per-round attestation phase) lives in a
follow-up layer that wires this module into ``taker.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from jmcore.attestation_wire import AttestPayload, AttestWireError, decode_attest
from jmcore.clsag_attestation import RingMember
from loguru import logger


class _DecryptingCrypto(Protocol):
    """Minimal interface the decoder needs from a maker session crypto."""

    def decrypt(self, ciphertext: str) -> str: ...


@dataclass(frozen=True, slots=True)
class AttestDecodeResult:
    """Outcome of decoding one !attest reply.

    Either ``payload`` is populated and ``error`` is ``None``, or vice
    versa. The ``nick`` is preserved so callers can surface per-maker
    rejection reasons without rebuilding maps.
    """

    nick: str
    payload: AttestPayload | None
    error: str | None

    @property
    def ok(self) -> bool:
        return self.payload is not None


def decode_attest_reply(
    *,
    nick: str,
    response_data: str,
    crypto: _DecryptingCrypto,
    expected_set_size: int,
) -> AttestDecodeResult:
    """Decrypt and decode a single maker's !attest reply.

    The directory wire format is ``<encrypted_token> <signing_pk> <sig>``;
    only the first whitespace-separated token is the encrypted payload
    (the rest is per-message signing metadata applied by the directory
    client). Mirrors the !ioauth decode path in :mod:`taker.taker`.
    """
    parts = response_data.strip().split()
    if not parts:
        return AttestDecodeResult(nick=nick, payload=None, error="empty response")
    encrypted = parts[0]
    try:
        plaintext = crypto.decrypt(encrypted)
    except Exception as e:  # pragma: no cover - defensive: NaCl internals
        return AttestDecodeResult(nick=nick, payload=None, error=f"decrypt failed: {e}")
    try:
        payload = decode_attest(plaintext, expected_set_size=expected_set_size)
    except AttestWireError as e:
        return AttestDecodeResult(nick=nick, payload=None, error=f"wire decode failed: {e}")
    return AttestDecodeResult(nick=nick, payload=payload, error=None)


def index_ring_by_nick(ring: list[RingMember], nick_to_idx: dict[str, int]) -> dict[str, int]:
    """Return a copy of ``nick_to_idx`` with bounds-checked indices.

    Just a guardrail used at the boundary between the round assembler
    and this module: out-of-range or non-unique indices indicate a
    programming error in the round-builder layer rather than a hostile
    maker, so we surface them eagerly.
    """
    n = len(ring)
    seen: dict[int, str] = {}
    out: dict[str, int] = {}
    for nick, idx in nick_to_idx.items():
        if not 0 <= idx < n:
            raise ValueError(f"signer_idx {idx} for {nick} outside ring of size {n}")
        if idx in seen:
            raise ValueError(
                f"duplicate signer_idx {idx} for {nick} (also assigned to {seen[idx]})"
            )
        seen[idx] = nick
        out[nick] = idx
    return out


def log_decode_failures(results: list[AttestDecodeResult]) -> list[str]:
    """Side-effect logger; returns the list of failed nicks."""
    failed: list[str] = []
    for r in results:
        if r.ok:
            continue
        failed.append(r.nick)
        logger.warning(f"!attest from {r.nick}: {r.error}")
    return failed
