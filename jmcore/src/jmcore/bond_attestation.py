"""K-of-N concatenated BIP340 Schnorr bond attestation (JMP-0006).

A taker that runs the multi-round transaction-extension protocol must
prove to potential late joiners that the in-flight CoinJoin is real and
not a surveillance honeypot. JMP-0006 specifies that the taker collects
``K`` independent BIP340 Schnorr signatures from the top-bond round-0
makers (ranked by ``bond_value``, ties broken by ``(txid, vout)``).

This module implements only the *primitive*: building the canonical
attestation message, packing/unpacking the wire blob, and verifying the
signatures. Bond-value lookup, threshold checks against the orderbook,
and signer selection live in the taker, where the orderbook cache is
available.

Wire format (matches JMP-0006 byte-for-byte)::

    <bond_count:1b>
      <bond_utxo_1:36b><pubkey_1:32b><sig_1:64b>
      <bond_utxo_2:36b><pubkey_2:32b><sig_2:64b>
      ...
      <bond_utxo_K:36b><pubkey_K:32b><sig_K:64b>

Per-signer record is fixed at 132 bytes; the full blob is
``1 + 132 * K`` bytes. Records are emitted in canonical order (ascending
``(txid, vout)``) so that two honest implementations always produce
byte-identical attestations.

The ``bond_utxo`` field uses Bitcoin's standard ``OutPoint``
serialization: 32-byte little-endian ``txid`` followed by a 4-byte
little-endian ``vout``. This is the same format used in raw Bitcoin
transactions (``CTxIn::prevout``), which keeps the attestation aligned
with how every other on-the-wire reference to a UTXO is encoded in
JoinMarket.

The signed message is::

    "jmng/tx_extension_v1/attest" || run_id || round_no_be

with ``run_id`` exactly 32 bytes (JMP-0006 section "CoinJoin run
identifiers") and ``round_no`` a 16-bit unsigned integer encoded
big-endian. Big-endian is chosen for the signed transcript because it
matches the network-byte-order convention used elsewhere in the
JoinMarket signed-message space (PoDLE commitments, ``!ioauth`` proofs);
the wire-level ``OutPoint`` LE encoding is a separate concern from the
signed transcript.

The 64-byte raw message is small enough to feed directly into the
BIP340 verifier, so no intermediate tagged hash is required (BIP340
itself folds in domain separation via the ``BIP0340/challenge`` tag).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final

import jmcore.schnorr as schnorr

# JMP-0006 wire-format constants. Pinned here so a typo in one place
# can never silently disagree with the byte layout asserted in tests.
ATTEST_DOMAIN_TAG: Final[bytes] = b"jmng/tx_extension_v1/attest"
RUN_ID_SIZE: Final[int] = 32
ROUND_NO_SIZE: Final[int] = 2  # uint16 big-endian
OUTPOINT_SIZE: Final[int] = 36  # 32B txid LE + 4B vout LE
PUBKEY_SIZE: Final[int] = 32  # BIP340 x-only
SIGNATURE_SIZE: Final[int] = 64  # BIP340
RECORD_SIZE: Final[int] = OUTPOINT_SIZE + PUBKEY_SIZE + SIGNATURE_SIZE
COUNT_SIZE: Final[int] = 1
MAX_BOND_COUNT: Final[int] = 0xFF  # one-byte count field
MAX_ROUND_NO: Final[int] = 0xFFFF  # uint16


class BondAttestationError(Exception):
    """Raised when an attestation is malformed, mis-ordered, or invalid."""


@dataclass(frozen=True)
class BondOutpoint:
    """A Bitcoin UTXO outpoint, in JoinMarket's customary high-level form.

    ``txid`` is the human-display form: 32 bytes in *display* (big-endian)
    order, the same way ``bitcoin-cli`` prints transaction hashes. The
    wire encoder reverses these bytes to match Bitcoin's internal
    little-endian serialization, so callers never have to think about
    endianness themselves.
    """

    txid: bytes
    vout: int

    def __post_init__(self) -> None:
        if len(self.txid) != 32:
            raise BondAttestationError(
                f"txid must be 32 bytes (display order), got {len(self.txid)}"
            )
        if not 0 <= self.vout <= 0xFFFFFFFF:
            raise BondAttestationError(f"vout out of uint32 range: {self.vout}")

    def to_wire(self) -> bytes:
        """Return the 36-byte ``OutPoint`` serialization (LE txid + LE vout)."""
        return self.txid[::-1] + struct.pack("<I", self.vout)

    @classmethod
    def from_wire(cls, blob: bytes) -> BondOutpoint:
        if len(blob) != OUTPOINT_SIZE:
            raise BondAttestationError(
                f"outpoint wire blob must be {OUTPOINT_SIZE} bytes, got {len(blob)}"
            )
        txid_le = blob[:32]
        (vout,) = struct.unpack("<I", blob[32:36])
        return cls(txid=txid_le[::-1], vout=vout)

    def sort_key(self) -> tuple[bytes, int]:
        """Canonical ordering: ascending ``(txid_display, vout)``.

        Spec text: "sorted ascending by ``bond_outpoint = txid:vout``".
        Display-order txid sort gives the same total order as wire-order
        txid sort *up to byte reversal*; the spec doesn't pin which one,
        but display-order matches every other JoinMarket comparison of
        UTXOs (orderbook keys, blacklist entries, debug logs) so we use
        it consistently.
        """
        return (self.txid, self.vout)


@dataclass(frozen=True)
class BondSignerInput:
    """One signer's (outpoint, x-only pubkey, BIP340 signature) tuple.

    This is the unit the taker collects from each round-0 maker before
    packing the wire blob. ``pubkey`` MUST equal the orderbook-published
    ``utxo_pubkey`` for ``outpoint`` (JMP-0006 section "Bond
    Attestation"); the taker is responsible for checking that consistency
    against its orderbook cache before accepting a contribution. The
    primitive verifier here only checks BIP340 validity against the
    pubkey supplied in the blob.
    """

    outpoint: BondOutpoint
    pubkey: bytes
    signature: bytes

    def __post_init__(self) -> None:
        if len(self.pubkey) != PUBKEY_SIZE:
            raise BondAttestationError(
                f"pubkey must be {PUBKEY_SIZE} bytes (BIP340 x-only), got {len(self.pubkey)}"
            )
        if len(self.signature) != SIGNATURE_SIZE:
            raise BondAttestationError(
                f"signature must be {SIGNATURE_SIZE} bytes (BIP340), got {len(self.signature)}"
            )


def build_attest_message(run_id: bytes, round_no: int) -> bytes:
    """Construct the canonical 32-byte BIP340 message for one signer.

    The raw ``ATTEST_DOMAIN_TAG || run_id || round_no_be`` payload is
    61 bytes (27 + 32 + 2). BIP340 verification accepts only 32-byte
    messages in our wrapper, so we fold the variable-length payload
    through ``schnorr.tagged_hash`` with a domain tag derived from
    JMP-0006's protocol identifier. The doubled-tag prefix ensures
    domain separation from any other JoinMarket signed message that
    might happen to start with the same bytes.
    """
    if len(run_id) != RUN_ID_SIZE:
        raise BondAttestationError(f"run_id must be {RUN_ID_SIZE} bytes, got {len(run_id)}")
    if not 0 <= round_no <= MAX_ROUND_NO:
        raise BondAttestationError(f"round_no must fit in uint16, got {round_no}")
    payload = ATTEST_DOMAIN_TAG + run_id + struct.pack(">H", round_no)
    return schnorr.tagged_hash("jmng/tx_extension_v1/attest", payload)


def sign_attestation(
    secret_key: bytes,
    run_id: bytes,
    round_no: int,
    aux_rand: bytes | None = None,
) -> bytes:
    """Produce a single signer's BIP340 contribution to the attestation.

    The taker calls this on each round-0 maker (over the existing
    end-to-end-encrypted session); each maker uses the private key
    associated with its bond UTXO's published ``utxo_pubkey``.
    """
    msg = build_attest_message(run_id, round_no)
    return schnorr.sign(secret_key, msg, aux_rand=aux_rand)


def pack_attestation(signers: list[BondSignerInput]) -> bytes:
    """Serialize a sorted list of signers to the JMP-0006 wire blob.

    Caller-supplied ``signers`` MUST already be in canonical order
    (ascending ``(txid, vout)``); the function asserts this rather than
    silently re-sorting, so a buggy caller fails loudly instead of
    producing an attestation that disagrees with what every other
    implementation would emit.

    Duplicate outpoints are also rejected: the spec implicitly assumes
    distinct signers (one signature per bond UTXO), and a dup would
    cheaply double-count a single signer's bond toward the threshold.
    """
    if not signers:
        raise BondAttestationError("attestation must contain at least one signer")
    if len(signers) > MAX_BOND_COUNT:
        raise BondAttestationError(
            f"attestation supports at most {MAX_BOND_COUNT} signers, got {len(signers)}"
        )

    seen: set[tuple[bytes, int]] = set()
    prev_key: tuple[bytes, int] | None = None
    for s in signers:
        key = s.outpoint.sort_key()
        if key in seen:
            raise BondAttestationError(
                f"duplicate bond outpoint in attestation: {s.outpoint.txid.hex()}:{s.outpoint.vout}"
            )
        if prev_key is not None and key <= prev_key:
            raise BondAttestationError(
                "signers must be in ascending (txid, vout) order; "
                f"{s.outpoint.txid.hex()}:{s.outpoint.vout} is not "
                "after the previous entry"
            )
        seen.add(key)
        prev_key = key

    out = bytearray()
    out.append(len(signers))
    for s in signers:
        out += s.outpoint.to_wire()
        out += s.pubkey
        out += s.signature
    return bytes(out)


def unpack_attestation(blob: bytes) -> list[BondSignerInput]:
    """Parse a wire blob into a list of signer records.

    Performs structural validation only (length, count consistency,
    ordering, no duplicates). Cryptographic verification and
    bond-value/orderbook checks are the caller's job.
    """
    if len(blob) < COUNT_SIZE:
        raise BondAttestationError("attestation blob is empty")
    count = blob[0]
    if count == 0:
        raise BondAttestationError("attestation declares zero signers")
    expected_len = COUNT_SIZE + count * RECORD_SIZE
    if len(blob) != expected_len:
        raise BondAttestationError(
            f"attestation length mismatch: declared {count} signers "
            f"=> expected {expected_len} bytes, got {len(blob)}"
        )

    signers: list[BondSignerInput] = []
    prev_key: tuple[bytes, int] | None = None
    for i in range(count):
        off = COUNT_SIZE + i * RECORD_SIZE
        outpoint = BondOutpoint.from_wire(blob[off : off + OUTPOINT_SIZE])
        pubkey = blob[off + OUTPOINT_SIZE : off + OUTPOINT_SIZE + PUBKEY_SIZE]
        sig = blob[off + OUTPOINT_SIZE + PUBKEY_SIZE : off + RECORD_SIZE]
        key = outpoint.sort_key()
        if prev_key is not None and key <= prev_key:
            raise BondAttestationError(
                f"signer #{i} is not strictly after #{i - 1} in canonical order"
            )
        prev_key = key
        signers.append(BondSignerInput(outpoint=outpoint, pubkey=pubkey, signature=sig))
    return signers


def verify_attestation(
    blob: bytes,
    run_id: bytes,
    round_no: int,
    *,
    expected_count: int | None = None,
) -> list[BondSignerInput]:
    """Structurally parse and cryptographically verify an attestation.

    Returns the parsed signer list on success. Raises
    ``BondAttestationError`` on any structural problem or signature
    failure.

    ``expected_count`` is an optional defensive check for callers that
    have a fixed K (e.g. JMP-0006's recommended ``K=3``); leaving it
    ``None`` accepts any non-zero count and lets the caller layer its
    own threshold policy on top of the parsed result.
    """
    signers = unpack_attestation(blob)
    if expected_count is not None and len(signers) != expected_count:
        raise BondAttestationError(f"expected exactly {expected_count} signers, got {len(signers)}")

    msg = build_attest_message(run_id, round_no)
    for i, s in enumerate(signers):
        if not schnorr.verify(s.pubkey, msg, s.signature):
            raise BondAttestationError(
                f"BIP340 signature {i} failed verification "
                f"(outpoint={s.outpoint.txid.hex()}:{s.outpoint.vout})"
            )
    return signers
