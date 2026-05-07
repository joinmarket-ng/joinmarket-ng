"""CLSAG-style linkable ring-signature bond attestation (JMP-0006).

A taker that runs the multi-round transaction-extension protocol must
prove to potential late joiners that the in-flight CoinJoin is real and
not a surveillance honeypot. JMP-0006 specifies that the taker collects
``K`` independent CLSAG ring signatures from the round-0 makers, each
ring drawn from a wide anonymity set ``N`` of currently-bonded round-0
makers in the orderbook. This module is the ``jmcore`` Python surface
over the Rust ``nwabisabi`` CLSAG primitives plus the canonical wire
encoding from JMP-0006.

Three things live here, and nothing else:

1. The canonical attestation message constructor (binds to ``run_id``
   and ``round_no``).
2. Helpers to sign a single ring (``sign_ring``) and to produce the
   per-run rotating key image without paying for a full signature
   (``compute_key_image``).
3. The full-blob pack/unpack/verify functions for the
   ``bond_attestation_b64`` field carried inside ``!cjext``.

Bond-value lookup, ring-membership selection, and quality-threshold
checks against the orderbook live in the taker / orderbook_watcher
where the orderbook cache is available; this module only provides the
cryptographic primitive.

Wire format (matches JMP-0006 byte-for-byte)::

    <set_size:2b BE>
      <pubkey_1:32b>...<pubkey_N:32b>     (x-only / BIP340)
      <outpoint_1:36b>...<outpoint_N:36b> (txid LE || vout LE32)
    <sig_count:1b>                          (== K)
      <ring_sig_1> ... <ring_sig_K>

Each ``ring_sig_i`` is the canonical CLSAG blob produced by
``nwabisabi.clsag_sign``: ``33 + 32 + 32 * N`` bytes
(``key_image`` || ``c0`` || ``s_1`` ... ``s_N``).

The signed transcript per ring is::

    "jmng/tx_extension_v1/attest" || run_id || round_no_be

with ``run_id`` exactly 32 bytes (JMP-0006 "CoinJoin run identifiers")
and ``round_no`` encoded as a 16-bit big-endian unsigned integer. The
27-byte ASCII tag is an explicit domain separator so a CLSAG signing
oracle cannot be coerced into producing a signature meaningful in some
other JoinMarket context. Big-endian for ``round_no`` matches the
network-byte-order convention already used elsewhere in JoinMarket's
signed-message space (PoDLE commitments, ``!ioauth`` proofs); the
wire-level ``OutPoint`` LE encoding is a separate concern from the
signed transcript.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

import nwabisabi

# JMP-0006 wire-format constants. Pinned here so a typo in one place
# cannot silently produce blobs that decode against a slightly-wrong
# constant elsewhere.
_X_ONLY_PUBKEY_LEN: Final[int] = 32
_OUTPOINT_LEN: Final[int] = 36  # 32-byte txid LE + 4-byte vout LE32
_KEY_IMAGE_LEN: Final[int] = 33
_SCALAR_LEN: Final[int] = 32
_SET_SIZE_LEN: Final[int] = 2  # uint16 big-endian
_SIG_COUNT_LEN: Final[int] = 1  # uint8


# Per-ring signature size: key image + c0 + N response scalars.
def _ring_sig_size(set_size: int) -> int:
    return _KEY_IMAGE_LEN + _SCALAR_LEN + _SCALAR_LEN * set_size


# JMP-0006 hard bounds on the anonymity-set parameters. ``MIN_K`` and
# ``MAX_K`` bracket the number of independent ring signatures a taker
# is allowed to bundle; ``MAX_SET_SIZE`` is a hard upper bound on the
# ring width to prevent attestation-amplification DoS.
MIN_K: Final[int] = 1
MAX_K: Final[int] = 16
MIN_SET_SIZE: Final[int] = 1  # absolute floor; policy floor is 25
MAX_SET_SIZE: Final[int] = 2**16 - 1  # uint16 ceiling

# Default taker policy. The verifier-side defaults from JMP-0006
# §"Quality check" live here so callers don't redefine them in three
# different places.
DEFAULT_K: Final[int] = 3
DEFAULT_MIN_SET_SIZE: Final[int] = 25
DEFAULT_MIN_QUALITY_FRACTION: Final[float] = 0.5

ATTESTATION_DST: Final[bytes] = b"jmng/tx_extension_v1/attest"
RUN_ID_LEN: Final[int] = 32


def attestation_message(run_id: bytes, round_no: int) -> bytes:
    """Build the canonical CLSAG ring-signature message.

    ``run_id`` MUST be exactly 32 bytes; ``round_no`` MUST fit in a
    uint16 (0..65535 inclusive). Returns the 61-byte transcript
    ``"jmng/tx_extension_v1/attest" || run_id || round_no_be``.
    """
    if len(run_id) != RUN_ID_LEN:
        raise ValueError(f"run_id must be {RUN_ID_LEN} bytes, got {len(run_id)}")
    if not 0 <= round_no <= 0xFFFF:
        raise ValueError(f"round_no must fit in uint16, got {round_no}")
    return ATTESTATION_DST + run_id + struct.pack(">H", round_no)


@dataclass(frozen=True)
class RingMember:
    """A single anonymity-set entry: an x-only pubkey + bond outpoint.

    Both fields come from the orderbook and travel together so a
    verifier can re-fetch the bond value out-of-band from its own
    orderbook view.
    """

    pubkey_xonly: bytes  # 32 bytes, BIP340 x-only encoding
    outpoint: bytes  # 36 bytes, 32-byte txid LE + 4-byte vout LE32

    def __post_init__(self) -> None:
        if len(self.pubkey_xonly) != _X_ONLY_PUBKEY_LEN:
            raise ValueError(
                f"pubkey_xonly must be {_X_ONLY_PUBKEY_LEN} bytes, got {len(self.pubkey_xonly)}"
            )
        if len(self.outpoint) != _OUTPOINT_LEN:
            raise ValueError(f"outpoint must be {_OUTPOINT_LEN} bytes, got {len(self.outpoint)}")


def sign_ring(
    *,
    secret_key: bytes,
    ring: list[RingMember],
    signer_idx: int,
    run_id: bytes,
    round_no: int,
) -> bytes:
    """Produce one CLSAG ring signature over the JMP-0006 transcript.

    Returns the canonical ``33 + 32 + 32 * N`` byte blob suitable for
    embedding in :func:`pack_attestation` (or for direct transport via
    ``!attest`` during attestation collection).
    """
    if not 0 <= signer_idx < len(ring):
        raise ValueError(f"signer_idx {signer_idx} out of range for ring of size {len(ring)}")
    msg = attestation_message(run_id, round_no)
    pubkeys = [m.pubkey_xonly for m in ring]
    return nwabisabi.clsag_sign(secret_key, pubkeys, signer_idx, run_id, msg)


def compute_key_image(secret_key: bytes, run_id: bytes) -> bytes:
    """Compute the per-run rotating key image without producing a signature.

    Useful for pre-flight Sybil checks: a maker can refuse a second
    ``!attestreq`` for the same ``(run_id, round_no)`` by comparing
    the requested key image against a cached one.
    """
    if len(run_id) != RUN_ID_LEN:
        raise ValueError(f"run_id must be {RUN_ID_LEN} bytes, got {len(run_id)}")
    return nwabisabi.clsag_key_image(secret_key, run_id)


def verify_ring(
    *,
    signature: bytes,
    ring: list[RingMember],
    run_id: bytes,
    round_no: int,
) -> tuple[bool, bytes]:
    """Verify a single CLSAG ring signature.

    Returns ``(ok, key_image)``. The key image is returned even on
    failure so the caller can still dedupe at the gossip layer.
    """
    msg = attestation_message(run_id, round_no)
    pubkeys = [m.pubkey_xonly for m in ring]
    return nwabisabi.clsag_verify(signature, pubkeys, run_id, msg)


# ---------------------------------------------------------------------------
# Full attestation blob (carried inside !cjext as bond_attestation_b64)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attestation:
    """A decoded ``bond_attestation_b64`` payload.

    The ring-member list is shared across all ``ring_signatures``; each
    signature carries its own key image (deduplication primitive) and
    the same anonymity set is implicit from ``ring``.
    """

    ring: list[RingMember]
    ring_signatures: list[bytes]  # each: 33 + 32 + 32 * len(ring) bytes

    @property
    def set_size(self) -> int:
        return len(self.ring)

    @property
    def k(self) -> int:
        return len(self.ring_signatures)


def pack_attestation(att: Attestation) -> bytes:
    """Serialize an :class:`Attestation` to its canonical wire blob.

    The output is the raw bytes; base64 wrapping is the caller's
    responsibility (so the same routine drives both PUBMSG-broadcast
    encoding and on-disk persistence for tests).
    """
    n = att.set_size
    k = att.k
    if not MIN_SET_SIZE <= n <= MAX_SET_SIZE:
        raise ValueError(f"set_size {n} outside [{MIN_SET_SIZE}, {MAX_SET_SIZE}]")
    if not MIN_K <= k <= MAX_K:
        raise ValueError(f"sig_count {k} outside [{MIN_K}, {MAX_K}]")
    expected_sig_size = _ring_sig_size(n)
    for idx, sig in enumerate(att.ring_signatures):
        if len(sig) != expected_sig_size:
            raise ValueError(
                f"ring_signatures[{idx}] is {len(sig)} bytes, expected {expected_sig_size}"
            )

    out = bytearray()
    out += struct.pack(">H", n)
    for m in att.ring:
        out += m.pubkey_xonly
    for m in att.ring:
        out += m.outpoint
    out += struct.pack(">B", k)
    for sig in att.ring_signatures:
        out += sig
    return bytes(out)


def unpack_attestation(blob: bytes) -> Attestation:
    """Parse a canonical attestation blob produced by :func:`pack_attestation`.

    Raises :class:`ValueError` on any structural inconsistency (length
    mismatch, ``set_size`` / ``sig_count`` out of bounds, trailing
    bytes). Cryptographic verification is :func:`verify_attestation`'s
    job; this routine only enforces the wire shape.
    """
    if len(blob) < _SET_SIZE_LEN + _SIG_COUNT_LEN:
        raise ValueError(f"attestation too short: {len(blob)} bytes")
    cursor = 0
    (n,) = struct.unpack(">H", blob[cursor : cursor + _SET_SIZE_LEN])
    cursor += _SET_SIZE_LEN
    if not MIN_SET_SIZE <= n <= MAX_SET_SIZE:
        raise ValueError(f"set_size {n} outside [{MIN_SET_SIZE}, {MAX_SET_SIZE}]")

    pubkeys_end = cursor + n * _X_ONLY_PUBKEY_LEN
    outpoints_end = pubkeys_end + n * _OUTPOINT_LEN
    if len(blob) < outpoints_end + _SIG_COUNT_LEN:
        raise ValueError(
            f"attestation truncated: need {outpoints_end + _SIG_COUNT_LEN} bytes, got {len(blob)}"
        )
    pubkeys = [
        blob[cursor + i * _X_ONLY_PUBKEY_LEN : cursor + (i + 1) * _X_ONLY_PUBKEY_LEN]
        for i in range(n)
    ]
    cursor = pubkeys_end
    outpoints = [
        blob[cursor + i * _OUTPOINT_LEN : cursor + (i + 1) * _OUTPOINT_LEN] for i in range(n)
    ]
    cursor = outpoints_end

    (k,) = struct.unpack(">B", blob[cursor : cursor + _SIG_COUNT_LEN])
    cursor += _SIG_COUNT_LEN
    if not MIN_K <= k <= MAX_K:
        raise ValueError(f"sig_count {k} outside [{MIN_K}, {MAX_K}]")

    sig_size = _ring_sig_size(n)
    expected_total = cursor + sig_size * k
    if len(blob) != expected_total:
        raise ValueError(
            f"attestation length mismatch: expected {expected_total} bytes, got {len(blob)}"
        )

    sigs = [blob[cursor + i * sig_size : cursor + (i + 1) * sig_size] for i in range(k)]

    ring = [
        RingMember(pubkey_xonly=pk, outpoint=op) for pk, op in zip(pubkeys, outpoints, strict=True)
    ]
    return Attestation(ring=ring, ring_signatures=sigs)


@dataclass(frozen=True)
class AttestationVerification:
    """Outcome of :func:`verify_attestation`.

    ``ok`` collapses every per-ring check, the policy check (``set_size
    >= min_set_size``), and the duplicate-key-image check into a single
    boolean. Callers that want detail (e.g. to log which ring failed)
    should consult ``per_ring_ok`` and ``key_images``.
    """

    ok: bool
    per_ring_ok: list[bool]
    key_images: list[bytes]
    set_size: int
    k: int


def verify_attestation(
    att: Attestation,
    *,
    run_id: bytes,
    round_no: int,
    min_set_size: int = DEFAULT_MIN_SET_SIZE,
) -> AttestationVerification:
    """Verify every ring signature and enforce the policy floors.

    Quality-fraction checking (``min_anonymity_set_quality_fraction``)
    is *not* done here because it requires the verifier's own
    orderbook view; callers (orderbook_watcher / late-joining maker)
    layer it on top of ``ok = True``.
    """
    msg = attestation_message(run_id, round_no)
    pubkeys = [m.pubkey_xonly for m in att.ring]
    per_ring_ok: list[bool] = []
    key_images: list[bytes] = []
    for sig in att.ring_signatures:
        ok_i, ki = nwabisabi.clsag_verify(sig, pubkeys, run_id, msg)
        per_ring_ok.append(bool(ok_i))
        key_images.append(ki)

    size_ok = att.set_size >= min_set_size
    unique_ki = len(set(key_images)) == len(key_images)
    ok = size_ok and unique_ki and all(per_ring_ok)

    return AttestationVerification(
        ok=ok,
        per_ring_ok=per_ring_ok,
        key_images=key_images,
        set_size=att.set_size,
        k=att.k,
    )


__all__ = [
    "ATTESTATION_DST",
    "Attestation",
    "AttestationVerification",
    "DEFAULT_K",
    "DEFAULT_MIN_QUALITY_FRACTION",
    "DEFAULT_MIN_SET_SIZE",
    "MAX_K",
    "MAX_SET_SIZE",
    "MIN_K",
    "MIN_SET_SIZE",
    "RUN_ID_LEN",
    "RingMember",
    "attestation_message",
    "compute_key_image",
    "pack_attestation",
    "sign_ring",
    "unpack_attestation",
    "verify_attestation",
    "verify_ring",
]
