"""Tests for channel-as-change Taproot primitives and change split/combine."""

from __future__ import annotations

import random

import pytest
from coincurve import PrivateKey, PublicKeyXOnly
from jmcore.bitcoin import create_p2tr_scriptpubkey, taproot_tweak_pubkey
from jmcore.taproot import TxOut, taproot_sighash

from jmswap.channel import (
    ChangeSplit,
    CombinedFunding,
    channel_funding_address,
    channel_funding_output,
    channel_internal_xonly,
    combined_funding_output,
    cooperative_close,
    plan_combined_channel,
    sorted_funding_keys,
    split_change,
)
from jmswap.spend import SwapOutpoint


def _keys() -> tuple[PrivateKey, PrivateKey]:
    return PrivateKey(), PrivateKey()


def test_sorted_funding_keys_canonical() -> None:
    a, b = _keys()
    ka = a.public_key.format(compressed=True)
    kb = b.public_key.format(compressed=True)
    assert sorted_funding_keys(ka, kb) == sorted_funding_keys(kb, ka)


def test_funding_output_is_keyspend_p2tr() -> None:
    a, b = _keys()
    ka, kb = a.public_key.format(), b.public_key.format()
    spk, internal = channel_funding_output(ka, kb)
    assert spk[:2] == bytes([0x51, 0x20])
    _, output_key = taproot_tweak_pubkey(internal, None)
    assert spk[2:] == output_key
    # order-independent (sorted keys)
    spk2, _ = channel_funding_output(kb, ka)
    assert spk == spk2
    assert channel_internal_xonly(ka, kb) == internal


def test_funding_address_network() -> None:
    a, b = _keys()
    addr = channel_funding_address(a.public_key.format(), b.public_key.format(), "regtest")
    assert addr.startswith("bcrt1p")


def test_cooperative_close_verifies_under_funding_key() -> None:
    a, b = _keys()
    ka, kb = a.public_key.format(), b.public_key.format()
    spk, _ = channel_funding_output(ka, kb)
    funding = SwapOutpoint(txid="ab" * 32, vout=0, value=200_000, scriptpubkey=spk)
    outs = [
        TxOut(value=120_000, scriptpubkey=create_p2tr_scriptpubkey(b"\x02" * 32)),
        TxOut(value=79_000, scriptpubkey=create_p2tr_scriptpubkey(b"\x03" * 32)),
    ]
    tx = cooperative_close(ka, kb, funding, outs, a.secret, b.secret)
    assert len(tx.witnesses[0]) == 1
    sig = tx.witnesses[0][0]
    sighash = taproot_sighash(tx, 0, [funding.value], [funding.scriptpubkey])
    assert PublicKeyXOnly(spk[2:]).verify(sig, sighash)


def test_split_change_sum_preserving_and_randomized() -> None:
    split = split_change(1_000_000, 4, rng=random.Random(1))
    assert split.verify()
    assert len(split.amounts) == 4
    assert sum(split.amounts) == 1_000_000
    assert all(a >= 330 for a in split.amounts)
    # randomized: a different seed gives a different partition
    other = split_change(1_000_000, 4, rng=random.Random(2))
    assert other.amounts != split.amounts
    assert sum(other.amounts) == 1_000_000


def test_split_change_single_part_is_identity() -> None:
    split = split_change(500_000, 1)
    assert split.amounts == (500_000,)
    assert split.verify()


def test_split_change_rejects_too_small() -> None:
    with pytest.raises(ValueError):
        split_change(500, 4, dust=330)


def test_change_split_verify_detects_tamper() -> None:
    bad = ChangeSplit(total=1000, amounts=(400, 400))
    assert not bad.verify()


def test_combined_funding_sum_and_verify() -> None:
    combined = CombinedFunding(residuals=(300_000, 250_000, 450_000), fee=600)
    assert combined.total == 1_000_000 - 600
    assert combined.verify(999_400)
    assert not combined.verify(1_000_000)


def test_combined_funding_output_is_keyspend_p2tr_and_order_independent() -> None:
    a, b = _keys()
    ka, kb = a.public_key.format(), b.public_key.format()
    spk, internal = combined_funding_output([ka, kb])
    assert spk[:2] == bytes([0x51, 0x20])
    _, output_key = taproot_tweak_pubkey(internal, None)
    assert spk[2:] == output_key
    # order independent, and equals the 2-key channel funding output
    assert combined_funding_output([kb, ka])[0] == spk
    assert channel_funding_output(ka, kb)[0] == spk


def test_combined_funding_output_rejects_single_maker() -> None:
    a, _ = _keys()
    with pytest.raises(ValueError):
        combined_funding_output([a.public_key.format()])


def test_plan_combined_channel_push_economics() -> None:
    a, b = _keys()
    ka, kb = a.public_key.format(), b.public_key.format()
    plan = plan_combined_channel([(ka, 300_000), (kb, 250_000)], fee=400)
    assert plan.total == 550_000 - 400
    assert plan.funder_pubkey == ka
    assert plan.peer_pubkey == kb
    # the funder pushes the peer its own change; each keeps its balance
    assert plan.push_to_peer_msat == 250_000 * 1000
    assert plan.funder_balance() == 300_000


def test_combined_output_cooperative_close_verifies() -> None:
    """Two makers cooperatively close their pooled shared output (in-family)."""
    a, b = _keys()
    ka, kb = a.public_key.format(), b.public_key.format()
    spk, _ = combined_funding_output([ka, kb])
    funding = SwapOutpoint(txid="ab" * 32, vout=0, value=550_000, scriptpubkey=spk)
    outs = [TxOut(value=549_500, scriptpubkey=create_p2tr_scriptpubkey(b"\x02" * 32))]
    tx = cooperative_close(ka, kb, funding, outs, a.secret, b.secret)
    sighash = taproot_sighash(tx, 0, [funding.value], [funding.scriptpubkey])
    assert PublicKeyXOnly(spk[2:]).verify(tx.witnesses[0][0], sighash)
