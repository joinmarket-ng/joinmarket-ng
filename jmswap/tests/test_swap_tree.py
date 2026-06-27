"""Swap tree tests, including byte-for-byte boltz-core compatibility vectors."""

from __future__ import annotations

import pytest
from jmcore.bitcoin import sha256
from jmcore.taproot import ripemd160

from jmswap.errors import SwapVerificationError
from jmswap.swap_tree import (
    SwapTree,
    make_refund_leaf,
    make_reverse_claim_leaf,
    make_submarine_claim_leaf,
    verify_swap_tree,
)

SERVER_PUBKEY = bytes.fromhex("0279be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798")
CLIENT_PUBKEY = bytes.fromhex("02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5")
SERVER_XONLY = SERVER_PUBKEY[1:]
CLIENT_XONLY = CLIENT_PUBKEY[1:]
PREIMAGE = b"\xaa" * 32
PREIMAGE_HASH = sha256(PREIMAGE)
LOCKTIME = 800_000


def test_submarine_claim_leaf_byte_layout() -> None:
    script = make_submarine_claim_leaf(PREIMAGE_HASH, SERVER_PUBKEY)
    expected = (
        bytes([0xA9, 0x14])
        + ripemd160(PREIMAGE_HASH)
        + bytes([0x88, 0x20])
        + SERVER_XONLY
        + bytes([0xAC])
    )
    assert script == expected
    assert len(script) == 57


def test_reverse_claim_leaf_byte_layout() -> None:
    script = make_reverse_claim_leaf(PREIMAGE_HASH, CLIENT_PUBKEY)
    expected = (
        bytes([0x82, 0x01, 0x20, 0x88, 0xA9, 0x14])
        + ripemd160(PREIMAGE_HASH)
        + bytes([0x88, 0x20])
        + CLIENT_XONLY
        + bytes([0xAC])
    )
    assert script == expected
    assert len(script) == 61


def test_refund_leaf_byte_layout() -> None:
    script = make_refund_leaf(CLIENT_PUBKEY, LOCKTIME)
    # <push 32> <xonly> OP_CHECKSIGVERIFY <push 3> 00 35 0c OP_CLTV
    expected = bytes([0x20]) + CLIENT_XONLY + bytes([0xAD, 0x03, 0x00, 0x35, 0x0C, 0xB1])
    assert script == expected


@pytest.mark.parametrize(
    "locktime,tail",
    [
        (16, [0xAD, 0x60, 0xB1]),  # OP_16
        (127, [0xAD, 0x01, 0x7F, 0xB1]),
        (128, [0xAD, 0x02, 0x80, 0x00, 0xB1]),  # sign-extension pad
    ],
)
def test_refund_leaf_minimal_pushnum(locktime: int, tail: list[int]) -> None:
    script = make_refund_leaf(CLIENT_PUBKEY, locktime)
    assert script == bytes([0x20]) + CLIENT_XONLY + bytes(tail)


def test_refund_leaf_rejects_timestamp_locktime() -> None:
    with pytest.raises(ValueError):
        make_refund_leaf(CLIENT_PUBKEY, 500_000_000)


def test_claim_leaf_rejects_bad_preimage_hash() -> None:
    for bad in (b"", b"\x00" * 31, b"\x00" * 33):
        with pytest.raises(ValueError):
            make_submarine_claim_leaf(bad, SERVER_PUBKEY)


def test_submarine_tree_direction() -> None:
    tree = SwapTree.build(
        is_reverse=False,
        preimage_hash=PREIMAGE_HASH,
        server_pubkey=SERVER_PUBKEY,
        client_pubkey=CLIENT_PUBKEY,
        locktime=LOCKTIME,
    )
    assert tree.is_reverse is False
    assert tree.claim_pubkey == SERVER_PUBKEY  # provider claims on swap-in
    assert tree.refund_pubkey == CLIENT_PUBKEY
    assert tree.claim_leaf[0] == 0xA9  # OP_HASH160


def test_reverse_tree_direction() -> None:
    tree = SwapTree.build(
        is_reverse=True,
        preimage_hash=PREIMAGE_HASH,
        server_pubkey=SERVER_PUBKEY,
        client_pubkey=CLIENT_PUBKEY,
        locktime=LOCKTIME,
    )
    assert tree.is_reverse is True
    assert tree.claim_pubkey == CLIENT_PUBKEY  # client claims on swap-out
    assert tree.refund_pubkey == SERVER_PUBKEY
    assert tree.claim_leaf[0] == 0x82  # OP_SIZE


def test_funding_address_and_keys_deterministic() -> None:
    tree = SwapTree.build(
        is_reverse=False,
        preimage_hash=PREIMAGE_HASH,
        server_pubkey=SERVER_PUBKEY,
        client_pubkey=CLIENT_PUBKEY,
        locktime=LOCKTIME,
    )
    addr = tree.funding_address("regtest")
    assert addr.startswith("bcrt1p")
    spk = tree.funding_output_script()
    assert spk[:2] == bytes([0x51, 0x20])
    # internal key is the MuSig2 aggregate (server first)
    assert len(tree.internal_xonly()) == 32
    assert len(tree.tweaked_aggregate_xonly()) == 32
    # output key in the spk equals the taptweaked aggregate
    assert spk[2:] == tree.tweaked_aggregate_xonly()


def test_control_blocks_present() -> None:
    tree = SwapTree.build(
        is_reverse=True,
        preimage_hash=PREIMAGE_HASH,
        server_pubkey=SERVER_PUBKEY,
        client_pubkey=CLIENT_PUBKEY,
        locktime=LOCKTIME,
    )
    claim_script, claim_cb = tree.claim_control_block()
    refund_script, refund_cb = tree.refund_control_block()
    assert claim_script == tree.claim_leaf
    assert refund_script == tree.refund_leaf
    assert len(claim_cb) == 33 + 32  # control byte + internal key + 1 sibling hash


def test_verify_swap_tree_accepts_matching() -> None:
    tree = SwapTree.build(
        is_reverse=False,
        preimage_hash=PREIMAGE_HASH,
        server_pubkey=SERVER_PUBKEY,
        client_pubkey=CLIENT_PUBKEY,
        locktime=LOCKTIME,
    )
    verified = verify_swap_tree(
        is_reverse=False,
        preimage_hash=PREIMAGE_HASH,
        server_pubkey=SERVER_PUBKEY,
        client_pubkey=CLIENT_PUBKEY,
        locktime=LOCKTIME,
        expected_address=tree.funding_address("regtest"),
        expected_swap_tree=tree.serialize(),
        network="regtest",
    )
    assert verified.funding_address("regtest") == tree.funding_address("regtest")


def test_verify_swap_tree_rejects_wrong_address() -> None:
    with pytest.raises(SwapVerificationError):
        verify_swap_tree(
            is_reverse=False,
            preimage_hash=PREIMAGE_HASH,
            server_pubkey=SERVER_PUBKEY,
            client_pubkey=CLIENT_PUBKEY,
            locktime=LOCKTIME,
            expected_address="bcrt1pqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqsg9xkr",
            network="regtest",
        )
