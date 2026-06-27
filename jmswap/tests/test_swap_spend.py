"""Roundtrip tests for swap output spending (claim, refund, cooperative).

Each test signs a spend and verifies the resulting Schnorr signature against the
exact sighash the corresponding spending path commits to. Combined with the
BIP341-vector-validated sighash in jmcore, this establishes consensus validity
of the constructed witnesses without needing a full node.
"""

from __future__ import annotations

import pytest
from coincurve import PrivateKey, PublicKeyXOnly
from jmcore.bitcoin import create_p2tr_scriptpubkey, sha256
from jmcore.taproot import tapleaf_hash, taproot_sighash, verify_schnorr

from jmswap.spend import (
    SwapOutpoint,
    build_claim_tx,
    build_cooperative_spend,
    build_refund_tx,
    cooperative_session,
    cooperative_spend_local,
    estimate_fee,
)
from jmswap.swap_tree import SwapTree

PREIMAGE = b"\x33" * 32
PREIMAGE_HASH = sha256(PREIMAGE)
DEST_SPK = create_p2tr_scriptpubkey(b"\x07" * 32)


def _keys() -> tuple[PrivateKey, PrivateKey]:
    return PrivateKey(), PrivateKey()


def _tree(server: PrivateKey, client: PrivateKey, is_reverse: bool) -> SwapTree:
    return SwapTree.build(
        is_reverse=is_reverse,
        preimage_hash=PREIMAGE_HASH,
        server_pubkey=server.public_key.format(compressed=True),
        client_pubkey=client.public_key.format(compressed=True),
        locktime=800_000,
    )


def _outpoint(tree: SwapTree, value: int = 100_000) -> SwapOutpoint:
    return SwapOutpoint(
        txid="ab" * 32, vout=0, value=value, scriptpubkey=tree.funding_output_script()
    )


def test_estimate_fee() -> None:
    assert estimate_fee(150, 1.0) == 150
    assert estimate_fee(150, 2.5) == 375


def test_swapin_claim_by_server() -> None:
    """swap-in: provider (server) claims the submarine HTLC with the preimage."""
    server, client = _keys()
    tree = _tree(server, client, is_reverse=False)
    outpoint = _outpoint(tree)
    tx = build_claim_tx(tree, outpoint, PREIMAGE, server.secret, DEST_SPK, fee=200)
    # witness layout: [sig, preimage, claim_leaf, control_block]
    wit = tx.witnesses[0]
    assert wit[1] == PREIMAGE
    assert wit[2] == tree.claim_leaf
    lh = tapleaf_hash(tree.claim_leaf)
    sighash = taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey], leaf_hash=lh)
    claim_xonly = server.public_key.format(compressed=True)[1:]
    assert verify_schnorr(claim_xonly, wit[0], sighash)
    assert len(tx.serialize()) > 0


def test_swapout_claim_by_client() -> None:
    """swap-out: client claims the reverse HTLC with the preimage."""
    server, client = _keys()
    tree = _tree(server, client, is_reverse=True)
    outpoint = _outpoint(tree)
    tx = build_claim_tx(tree, outpoint, PREIMAGE, client.secret, DEST_SPK, fee=200)
    lh = tapleaf_hash(tree.claim_leaf)
    sighash = taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey], leaf_hash=lh)
    claim_xonly = client.public_key.format(compressed=True)[1:]
    assert verify_schnorr(claim_xonly, tx.witnesses[0][0], sighash)


def test_refund_sets_locktime_and_sequence() -> None:
    server, client = _keys()
    tree = _tree(server, client, is_reverse=False)
    outpoint = _outpoint(tree)
    # swap-in refund is by the client (funder)
    tx = build_refund_tx(tree, outpoint, client.secret, DEST_SPK, fee=200)
    assert tx.locktime == 800_000
    assert tx.inputs[0].sequence == 0xFFFFFFFE
    wit = tx.witnesses[0]
    assert wit[1] == tree.refund_leaf
    lh = tapleaf_hash(tree.refund_leaf)
    sighash = taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey], leaf_hash=lh)
    refund_xonly = client.public_key.format(compressed=True)[1:]
    assert verify_schnorr(refund_xonly, wit[0], sighash)


def test_cooperative_keypath_spend_verifies_under_funding_key() -> None:
    """The happy-path MuSig2 spend yields a BIP340 sig valid for the output key."""
    server, client = _keys()
    tree = _tree(server, client, is_reverse=True)
    outpoint = _outpoint(tree)
    tx = cooperative_spend_local(
        tree,
        outpoint,
        DEST_SPK,
        fee=150,
        server_privkey=server.secret,
        client_privkey=client.secret,
    )
    assert len(tx.witnesses[0]) == 1  # single key-path signature
    sig = tx.witnesses[0][0]
    assert len(sig) == 64
    sighash = taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey])
    output_xonly = outpoint.scriptpubkey[2:]
    assert output_xonly == tree.tweaked_aggregate_xonly()
    assert PublicKeyXOnly(output_xonly).verify(sig, sighash)


def test_cooperative_interactive_matches_local() -> None:
    """Exercise the split (server/client) interactive primitives explicitly."""
    from jmcore.musig2 import nonce_agg, nonce_gen, sign_partial

    from jmswap.spend import finalize_cooperative

    server, client = _keys()
    tree = _tree(server, client, is_reverse=False)
    outpoint = _outpoint(tree)
    spend = build_cooperative_spend(tree, outpoint, DEST_SPK, fee=150)

    sn_s, pn_s = nonce_gen(tree.server_pubkey)
    sn_c, pn_c = nonce_gen(tree.client_pubkey)
    aggnonce = nonce_agg([pn_s, pn_c])
    session = cooperative_session(tree, aggnonce, spend.sighash)
    ps_s = sign_partial(sn_s, server.secret, session)
    ps_c = sign_partial(sn_c, client.secret, session)
    tx = finalize_cooperative(tree, spend, pn_s, pn_c, ps_s, ps_c)
    sighash = taproot_sighash(tx, 0, [outpoint.value], [outpoint.scriptpubkey])
    assert PublicKeyXOnly(outpoint.scriptpubkey[2:]).verify(tx.witnesses[0][0], sighash)


def test_fee_exceeds_value_raises() -> None:
    server, client = _keys()
    tree = _tree(server, client, is_reverse=False)
    outpoint = _outpoint(tree, value=100)
    with pytest.raises(ValueError):
        build_claim_tx(tree, outpoint, PREIMAGE, server.secret, DEST_SPK, fee=200)
