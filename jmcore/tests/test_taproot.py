"""Tests for jmcore.taproot against official BIP341 vectors and roundtrips."""

from __future__ import annotations

from coincurve import PrivateKey, PublicKeyXOnly

from jmcore.bitcoin import tagged_hash
from jmcore.taproot import (
    LEAF_VERSION_TAPSCRIPT,
    OP_CHECKSIG,
    OP_EQUALVERIFY,
    OP_HASH160,
    TaprootTx,
    TxIn,
    TxOut,
    construct_script,
    control_block,
    encode_script_num,
    ripemd160,
    sign_taproot_keypath,
    sign_taproot_scriptpath,
    tapleaf_hash,
    taproot_merkle_root,
    taproot_output_script,
    taproot_sighash,
    to_xonly,
    verify_schnorr,
)

# A single-leaf scriptPubKey case from the BIP341 wallet test vectors.
BIP341_INTERNAL = bytes.fromhex("187791b6f712a8ea41c8ecdd0ee77fab3e85263b37e1ec18a3651926b3a6cf27")
BIP341_LEAF_SCRIPT = bytes.fromhex(
    "20d85a959b0290bf19bb89ed43c916be835475d013da4b362117393e25a48229b8ac"
)
BIP341_MERKLE_ROOT = "5b75adecf53548f3ec6ad7d78383bf84cc57b55a3127c72b9a2481752dd88b21"
BIP341_SPK = "5120147c9c57132f6e7ecddba9800bb0c4449251c92a1e60371ee77557b6620f3ea3"
BIP341_CONTROL_BLOCK = "c1187791b6f712a8ea41c8ecdd0ee77fab3e85263b37e1ec18a3651926b3a6cf27"


def test_bip341_single_leaf_merkle_root() -> None:
    tree = (LEAF_VERSION_TAPSCRIPT, BIP341_LEAF_SCRIPT)
    assert taproot_merkle_root(tree).hex() == BIP341_MERKLE_ROOT


def test_bip341_single_leaf_output_script() -> None:
    tree = (LEAF_VERSION_TAPSCRIPT, BIP341_LEAF_SCRIPT)
    assert taproot_output_script(BIP341_INTERNAL, tree).hex() == BIP341_SPK


def test_bip341_single_leaf_control_block() -> None:
    tree = (LEAF_VERSION_TAPSCRIPT, BIP341_LEAF_SCRIPT)
    _, cb = control_block(BIP341_INTERNAL, tree, 0)
    assert cb.hex() == BIP341_CONTROL_BLOCK


def test_tapleaf_hash_matches_tagged_hash() -> None:
    from jmcore.bitcoin import encode_varint

    script = BIP341_LEAF_SCRIPT
    expected = tagged_hash(
        "TapLeaf", bytes([LEAF_VERSION_TAPSCRIPT]) + encode_varint(len(script)) + script
    )
    assert tapleaf_hash(script) == expected


def test_two_leaf_tree_sorted_branch() -> None:
    leaf_a = (LEAF_VERSION_TAPSCRIPT, b"\x51")  # OP_1
    leaf_b = (LEAF_VERSION_TAPSCRIPT, b"\x52")  # OP_2
    ha = tapleaf_hash(b"\x51")
    hb = tapleaf_hash(b"\x52")
    expected = tagged_hash("TapBranch", min(ha, hb) + max(ha, hb))
    assert taproot_merkle_root([leaf_a, leaf_b]) == expected


def test_to_xonly_roundtrip() -> None:
    pk = PrivateKey().public_key.format(compressed=True)
    assert to_xonly(pk) == pk[1:]
    assert to_xonly(pk[1:]) == pk[1:]


def test_encode_script_num() -> None:
    assert encode_script_num(0) == b""
    assert encode_script_num(1) == b"\x01"
    assert encode_script_num(32) == b"\x20"
    assert encode_script_num(0x80) == b"\x80\x00"


def _htlc_claim_leaf(preimage_hash: bytes, claim_xonly: bytes) -> bytes:
    return construct_script(
        [
            OP_HASH160,
            ripemd160(preimage_hash),
            OP_EQUALVERIFY,
            claim_xonly,
            OP_CHECKSIG,
        ]
    )


def test_script_path_spend_roundtrip() -> None:
    """A BIP342 script-path Schnorr signature verifies against its own sighash."""
    claim = PrivateKey()
    refund = PrivateKey()
    preimage = b"\x07" * 32
    preimage_hash = tagged_hash("x", preimage)[:32]
    claim_xonly = claim.public_key.format(compressed=True)[1:]
    refund_xonly = refund.public_key.format(compressed=True)[1:]
    claim_leaf = _htlc_claim_leaf(preimage_hash, claim_xonly)
    refund_leaf = construct_script([refund_xonly, OP_CHECKSIG])
    tree = [(LEAF_VERSION_TAPSCRIPT, claim_leaf), (LEAF_VERSION_TAPSCRIPT, refund_leaf)]
    spk = taproot_output_script(claim.public_key.format(compressed=True)[1:], tree)

    tx = TaprootTx(
        inputs=[TxIn(txid="11" * 32, vout=0)],
        outputs=[TxOut(value=9000, scriptpubkey=spk)],
    )
    sig = sign_taproot_scriptpath(tx, 0, [10000], [spk], claim_leaf, claim.secret)
    lh = tapleaf_hash(claim_leaf)
    sighash = taproot_sighash(tx, 0, [10000], [spk], leaf_hash=lh)
    assert verify_schnorr(claim_xonly, sig, sighash)


def test_key_path_spend_roundtrip() -> None:
    internal = PrivateKey()
    internal_xonly = internal.public_key.format(compressed=True)[1:]
    spk = taproot_output_script(internal_xonly, None)
    tx = TaprootTx(
        inputs=[TxIn(txid="22" * 32, vout=1)],
        outputs=[TxOut(value=4900, scriptpubkey=spk)],
    )
    from jmcore.bitcoin import taproot_tweak_privkey

    tweaked = taproot_tweak_privkey(internal.secret, None)
    sig = sign_taproot_keypath(tx, 0, [5000], [spk], tweaked)
    sighash = taproot_sighash(tx, 0, [5000], [spk])
    _, output_key = __import__(
        "jmcore.bitcoin", fromlist=["taproot_tweak_pubkey"]
    ).taproot_tweak_pubkey(internal_xonly, None)
    assert PublicKeyXOnly(output_key).verify(sig, sighash)


def test_tx_serialize_witness_roundtrip() -> None:
    spk = bytes.fromhex(BIP341_SPK)
    tx = TaprootTx(
        inputs=[TxIn(txid="33" * 32, vout=2)],
        outputs=[TxOut(value=1234, scriptpubkey=spk)],
        witnesses=[[b"\xaa" * 64]],
    )
    raw = tx.serialize()
    assert raw[4:6] == b"\x00\x01"  # segwit marker + flag
    assert isinstance(tx.txid(), str) and len(tx.txid()) == 64
