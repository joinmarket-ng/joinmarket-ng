"""Tests for taker.ring_assembly."""

from __future__ import annotations

import os
from typing import Any

import pytest
from coincurve import PrivateKey
from jmcore.models import Offer, OfferType

from taker.ring_assembly import (
    RingAssemblyError,
    _outpoint_bytes,
    _xonly_from_compressed,
    assemble_ring,
)


def _bond_data(seed: int) -> dict[str, Any]:
    """Make plausible fidelity_bond_data with a real secp256k1 pubkey."""
    sk = PrivateKey(bytes([seed % 256] * 32) if seed > 0 else os.urandom(32))
    pub_compressed = sk.public_key.format(compressed=True).hex()
    return {
        "utxo_txid": os.urandom(32).hex(),
        "utxo_vout": seed % 4,
        "utxo_pub": pub_compressed,
    }


def _offer(nick: str, *, with_bond: bool = True, seed: int = 0) -> Offer:
    return Offer(
        counterparty=nick,
        oid=0,
        ordertype=OfferType.SW0_ABSOLUTE,
        minsize=100,
        maxsize=10_000_000,
        txfee=0,
        cjfee=0,
        fidelity_bond_value=10_000 if with_bond else 0,
        fidelity_bond_data=_bond_data(seed) if with_bond else None,
    )


def _orderbook(n: int, start: int = 0) -> list[Offer]:
    return [_offer(f"m{i}", seed=i + 1 + start) for i in range(n)]


# ---- helper utilities ----


def test_outpoint_bytes_round_trip() -> None:
    txid = "00" * 32
    op = _outpoint_bytes(txid, 5)
    assert len(op) == 36
    assert op[-4:] == (5).to_bytes(4, "little")


def test_xonly_strips_parity_byte() -> None:
    pub = bytes([0x02]) + os.urandom(32)
    assert len(_xonly_from_compressed(pub.hex())) == 32


def test_xonly_rejects_wrong_length() -> None:
    with pytest.raises(RingAssemblyError, match="33 bytes"):
        _xonly_from_compressed("00" * 32)


# ---- assemble_ring happy paths ----


def test_assemble_ring_basic() -> None:
    pool = _orderbook(30)
    selected = {"m0": pool[0], "m1": pool[1], "m2": pool[2]}
    decoys = pool[3:]
    run_id = os.urandom(32)

    result = assemble_ring(
        selected_offers=selected,
        decoy_pool=decoys,
        target_set_size=25,
        run_id=run_id,
        min_set_size=25,
    )
    assert result.set_size == 25
    assert set(result.signer_idx_by_nick.keys()) == {"m0", "m1", "m2"}
    # Indices are unique and within range.
    indices = list(result.signer_idx_by_nick.values())
    assert len(set(indices)) == 3
    assert all(0 <= i < 25 for i in indices)


def test_assemble_ring_is_deterministic_for_same_run_id() -> None:
    pool = _orderbook(40)
    selected = {"m0": pool[0], "m1": pool[1]}
    decoys = pool[2:]
    run_id = b"\x42" * 32

    a = assemble_ring(
        selected_offers=selected,
        decoy_pool=decoys,
        target_set_size=25,
        run_id=run_id,
        min_set_size=25,
    )
    b = assemble_ring(
        selected_offers=selected,
        decoy_pool=decoys,
        target_set_size=25,
        run_id=run_id,
        min_set_size=25,
    )

    assert a.ring == b.ring
    assert a.signer_idx_by_nick == b.signer_idx_by_nick


def test_assemble_ring_differs_with_different_run_id() -> None:
    pool = _orderbook(40)
    selected = {"m0": pool[0]}
    decoys = pool[1:]

    a = assemble_ring(
        selected_offers=selected,
        decoy_pool=decoys,
        target_set_size=25,
        run_id=b"\x01" * 32,
        min_set_size=25,
    )
    b = assemble_ring(
        selected_offers=selected,
        decoy_pool=decoys,
        target_set_size=25,
        run_id=b"\x02" * 32,
        min_set_size=25,
    )
    # Astronomically unlikely to collide.
    assert a.ring != b.ring


def test_assemble_ring_decoy_pool_excludes_selected() -> None:
    pool = _orderbook(30)
    selected = {"m0": pool[0]}
    # Pass full pool as decoys; m0 should be filtered out, not duplicated.
    result = assemble_ring(
        selected_offers=selected,
        decoy_pool=pool,
        target_set_size=25,
        run_id=os.urandom(32),
        min_set_size=25,
    )
    assert result.set_size == 25
    # Only one ring slot maps to m0.
    m0_outpoint = result.ring[result.signer_idx_by_nick["m0"]].outpoint
    matching = [m for m in result.ring if m.outpoint == m0_outpoint]
    assert len(matching) == 1


# ---- error paths ----


def test_assemble_ring_rejects_selected_without_bond() -> None:
    selected = {"m0": _offer("m0", with_bond=False)}
    with pytest.raises(RingAssemblyError, match="no usable fidelity bond"):
        assemble_ring(
            selected_offers=selected,
            decoy_pool=_orderbook(30, start=100),
            target_set_size=25,
            run_id=os.urandom(32),
            min_set_size=25,
        )


def test_assemble_ring_rejects_target_below_min() -> None:
    pool = _orderbook(30)
    with pytest.raises(RingAssemblyError, match="below min_set_size"):
        assemble_ring(
            selected_offers={"m0": pool[0]},
            decoy_pool=pool[1:],
            target_set_size=10,
            run_id=os.urandom(32),
            min_set_size=25,
        )


def test_assemble_ring_rejects_empty_selected() -> None:
    with pytest.raises(RingAssemblyError, match="non-empty"):
        assemble_ring(
            selected_offers={},
            decoy_pool=_orderbook(30),
            target_set_size=25,
            run_id=os.urandom(32),
        )


def test_assemble_ring_aborts_when_below_min_set_size() -> None:
    # 3 selected + 5 decoys = 8 total, min_set_size=25 -> abort.
    pool = _orderbook(8)
    selected = {f"m{i}": pool[i] for i in range(3)}
    with pytest.raises(RingAssemblyError, match="cannot reach min_set_size"):
        assemble_ring(
            selected_offers=selected,
            decoy_pool=pool[3:],
            target_set_size=25,
            run_id=os.urandom(32),
            min_set_size=25,
        )


def test_assemble_ring_shrinks_target_to_available_when_above_min() -> None:
    # 3 selected + 12 decoys = 15 total, target 25, min 10 -> shrink to 15.
    pool = _orderbook(15)
    selected = {f"m{i}": pool[i] for i in range(3)}
    result = assemble_ring(
        selected_offers=selected,
        decoy_pool=pool[3:],
        target_set_size=25,
        run_id=os.urandom(32),
        min_set_size=10,
    )
    assert result.set_size == 15
    assert set(result.signer_idx_by_nick.keys()) == {"m0", "m1", "m2"}


def test_assemble_ring_shrinks_to_exactly_min_set_size() -> None:
    # 1 selected + 9 decoys = 10 total, min 10 -> exact min.
    pool = _orderbook(10)
    result = assemble_ring(
        selected_offers={"m0": pool[0]},
        decoy_pool=pool[1:],
        target_set_size=25,
        run_id=os.urandom(32),
        min_set_size=10,
    )
    assert result.set_size == 10


def test_assemble_ring_regtest_friendly_min_set_size() -> None:
    # Tiny regtest orderbook: 2 selected + 2 decoys, min_set_size=4.
    pool = _orderbook(4)
    selected = {f"m{i}": pool[i] for i in range(2)}
    result = assemble_ring(
        selected_offers=selected,
        decoy_pool=pool[2:],
        target_set_size=4,
        run_id=os.urandom(32),
        min_set_size=4,
    )
    assert result.set_size == 4
    assert len(result.signer_idx_by_nick) == 2


def test_assemble_ring_skips_decoys_with_bad_bond_data() -> None:
    # Build 30 valid offers + a few corrupt ones; should still assemble 25.
    pool = _orderbook(30)
    bad1 = _offer("bad1", with_bond=True, seed=999)
    assert bad1.fidelity_bond_data is not None
    bad1.fidelity_bond_data["utxo_pub"] = "not_hex"
    bad2 = _offer("bad2", with_bond=False)  # no bond at all
    decoys = [bad1, bad2] + pool[1:]
    result = assemble_ring(
        selected_offers={"m0": pool[0]},
        decoy_pool=decoys,
        target_set_size=25,
        run_id=os.urandom(32),
        min_set_size=25,
    )
    assert result.set_size == 25


def test_assemble_ring_dedupes_decoy_pubkey_collision() -> None:
    pool = _orderbook(30)
    # Forge a decoy that re-uses m0's pubkey: must be skipped.
    assert pool[0].fidelity_bond_data is not None
    collision = _offer("attacker", seed=500)
    assert collision.fidelity_bond_data is not None
    collision.fidelity_bond_data["utxo_pub"] = pool[0].fidelity_bond_data["utxo_pub"]
    selected = {"m0": pool[0]}
    decoys = [collision] + pool[1:]
    result = assemble_ring(
        selected_offers=selected,
        decoy_pool=decoys,
        target_set_size=25,
        run_id=os.urandom(32),
        min_set_size=25,
    )
    assert result.set_size == 25
    pubkeys = {m.pubkey_xonly for m in result.ring}
    assert len(pubkeys) == 25  # no duplicates leaked through


def test_assemble_ring_full_ring_can_grow_above_min() -> None:
    pool = _orderbook(60)
    selected = {f"m{i}": pool[i] for i in range(5)}
    decoys = pool[5:]
    result = assemble_ring(
        selected_offers=selected,
        decoy_pool=decoys,
        target_set_size=50,
        run_id=os.urandom(32),
        min_set_size=25,
    )
    assert result.set_size == 50
    assert len(result.signer_idx_by_nick) == 5
