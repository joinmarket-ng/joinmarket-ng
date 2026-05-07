"""Build the CLSAG anonymity ring for a tx-extension attestation round.

JMP-0006 variant (a): the same full ring is sent to every selected
maker, varying only the ``signer_idx``. Membership is drawn from the
union of (a) every selected maker's bonded UTXO and (b) bonded UTXOs
of *unselected* makers from the orderbook acting as decoys. Ring
members are deduplicated by ``(utxo_pub, txid, vout)`` and shuffled
deterministically by run_id so the assignment is reproducible by
verifiers given the same orderbook snapshot.

This module is pure / I/O-free. The taker is expected to feed it a
filtered orderbook snapshot and the set of selected counterparties.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass

from jmcore.clsag_attestation import RingMember
from jmcore.models import Offer
from loguru import logger


class RingAssemblyError(ValueError):
    """Raised when the orderbook can't supply a ring of the requested size."""


@dataclass(frozen=True, slots=True)
class RingAssembly:
    """Result of one ring-assembly call.

    ``ring`` is the full member list; ``signer_idx_by_nick`` maps
    each selected maker's nick to its ring index. Every nick in
    ``selected_nicks`` (passed to :func:`assemble_ring`) appears as
    a key here; if any selected maker can't be placed (e.g. missing
    bond data) the assembler raises rather than silently dropping
    them, since the round can't proceed without their slot.
    """

    ring: list[RingMember]
    signer_idx_by_nick: dict[str, int]

    @property
    def set_size(self) -> int:
        return len(self.ring)


def _outpoint_bytes(txid_hex: str, vout: int) -> bytes:
    """Encode (txid, vout) as the 36-byte form used by RingMember."""
    return bytes.fromhex(txid_hex)[::-1] + vout.to_bytes(4, "little")


def _xonly_from_compressed(pub_hex: str) -> bytes:
    """Strip the parity byte from a 33-byte compressed secp256k1 pubkey."""
    raw = bytes.fromhex(pub_hex)
    if len(raw) != 33:
        raise RingAssemblyError(f"compressed pubkey must be 33 bytes, got {len(raw)}")
    return raw[1:]


def _ring_member_from_offer(offer: Offer) -> RingMember | None:
    """Materialize a RingMember from an offer's fidelity_bond_data, or None.

    Returns ``None`` for offers without a usable bond — the caller
    decides whether that's fatal (selected maker) or skippable
    (decoy candidate).
    """
    bd = offer.fidelity_bond_data
    if not bd:
        return None
    try:
        pub = _xonly_from_compressed(bd["utxo_pub"])
        op = _outpoint_bytes(bd["utxo_txid"], int(bd["utxo_vout"]))
    except (KeyError, ValueError) as e:
        logger.debug(f"skipping {offer.counterparty}: bad bond data ({e})")
        return None
    return RingMember(pubkey_xonly=pub, outpoint=op)


def _seeded_shuffle(items: list[RingMember], run_id: bytes) -> list[RingMember]:
    """Deterministically shuffle by run_id so verifiers reproduce ordering."""
    seed = int.from_bytes(hashlib.sha256(b"jmng/ring_shuffle/v1" + run_id).digest()[:8], "big")
    rng = random.Random(seed)  # noqa: S311 - not used for crypto, only ordering
    out = list(items)
    rng.shuffle(out)
    return out


def assemble_ring(
    *,
    selected_offers: dict[str, Offer],
    decoy_pool: list[Offer],
    target_set_size: int,
    run_id: bytes,
    min_set_size: int = 25,
) -> RingAssembly:
    """Build a deterministic CLSAG ring from selected makers + decoys.

    Args:
        selected_offers: Mapping nick -> Offer for makers participating
            in this round. Each must carry usable
            ``fidelity_bond_data`` (raise otherwise — without their
            bond they can't sign for a slot).
        decoy_pool: Other offers from the orderbook to consider as
            decoys. Offers without bond data, with bond data
            colliding with a selected maker, or that fail to decode
            are silently dropped.
        target_set_size: Desired ring cardinality (selected + decoys).
            Must be >= min_set_size.
        run_id: 32-byte run identifier used as a deterministic shuffle
            seed; identical inputs produce identical rings.
        min_set_size: Lower bound on ring cardinality (defaults to
            JMP-0006's 25). The assembler will refuse to return a
            ring smaller than this regardless of ``target_set_size``.

    Raises:
        RingAssemblyError: if any selected maker lacks a usable bond,
            or if the merged pool can't reach ``min_set_size``.
    """
    if target_set_size < min_set_size:
        raise RingAssemblyError(
            f"target_set_size {target_set_size} below min_set_size {min_set_size}"
        )
    if not selected_offers:
        raise RingAssemblyError("selected_offers must be non-empty")

    # Step 1: place every selected maker. Bond-less selection is fatal.
    selected_members: dict[str, RingMember] = {}
    for nick, offer in selected_offers.items():
        member = _ring_member_from_offer(offer)
        if member is None:
            raise RingAssemblyError(f"selected maker {nick} has no usable fidelity bond data")
        selected_members[nick] = member

    # Track outpoints already in the ring to dedup decoys.
    seen_outpoints: set[bytes] = {m.outpoint for m in selected_members.values()}
    seen_pubkeys: set[bytes] = {m.pubkey_xonly for m in selected_members.values()}

    # Step 2: gather decoys, dropping bad/colliding ones.
    decoys: list[RingMember] = []
    for offer in decoy_pool:
        if offer.counterparty in selected_offers:
            continue  # already placed
        member = _ring_member_from_offer(offer)
        if member is None:
            continue
        if member.outpoint in seen_outpoints or member.pubkey_xonly in seen_pubkeys:
            continue
        decoys.append(member)
        seen_outpoints.add(member.outpoint)
        seen_pubkeys.add(member.pubkey_xonly)

    needed_decoys = target_set_size - len(selected_members)
    available = len(selected_members) + len(decoys)

    if available < min_set_size:
        # Genuinely too few makers in the orderbook to form even a
        # minimum-size ring. Abort: pretending otherwise would be a
        # privacy hazard.
        raise RingAssemblyError(
            f"cannot reach min_set_size {min_set_size}: "
            f"only {available} members available "
            f"({len(selected_members)} selected + {len(decoys)} decoys)"
        )

    if len(decoys) < needed_decoys:
        # Resilience path: scale target down to what's available
        # rather than aborting outright. This is what makes regtest /
        # signet runs (and degraded-mainnet conditions) usable. We
        # log it so operators notice the shrink.
        effective_target = available
        logger.warning(
            f"ring assembly: shrinking target from {target_set_size} to "
            f"{effective_target} (only {len(decoys)} decoys available, needed "
            f"{needed_decoys}); still >= min_set_size {min_set_size}"
        )
        needed_decoys = len(decoys)
    # else: full target reachable, keep needed_decoys unchanged.

    # Step 3: trim decoys to needed count, deterministically.
    decoys = _seeded_shuffle(decoys, run_id + b"/decoy_pick")[:needed_decoys]

    # Step 4: shuffle the merged ring deterministically.
    merged = list(selected_members.values()) + decoys
    shuffled = _seeded_shuffle(merged, run_id)

    # Step 5: derive signer_idx by looking up each selected member's
    # outpoint in the shuffled list.
    pos_by_outpoint: dict[bytes, int] = {m.outpoint: i for i, m in enumerate(shuffled)}
    signer_idx_by_nick = {nick: pos_by_outpoint[m.outpoint] for nick, m in selected_members.items()}

    return RingAssembly(ring=shuffled, signer_idx_by_nick=signer_idx_by_nick)
