"""Tests for jmcore.taproot against BIP341 vectors and native primitives."""

from __future__ import annotations

import pytest
from bitcointx.core import CTransaction, CTxOut
from bitcointx.core.key import CKey, XOnlyPubKey
from bitcointx.core.script import CScript, SIGHASH_Type, SignatureHashSchnorr

from jmcore.bitcoin import (
    parse_transaction,
    tagged_hash,
    taproot_tweak_privkey,
    taproot_tweak_pubkey,
)
from jmcore.taproot import (
    LEAF_VERSION_TAPSCRIPT,
    OP_CHECKSIG,
    OP_EQUALVERIFY,
    OP_HASH160,
    SIGHASH_SINGLE,
    TaprootError,
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

BIP341_INTERNAL = bytes.fromhex("187791b6f712a8ea41c8ecdd0ee77fab3e85263b37e1ec18a3651926b3a6cf27")
BIP341_LEAF_SCRIPT = bytes.fromhex(
    "20d85a959b0290bf19bb89ed43c916be835475d013da4b362117393e25a48229b8ac"
)
BIP341_MERKLE_ROOT = "5b75adecf53548f3ec6ad7d78383bf84cc57b55a3127c72b9a2481752dd88b21"
BIP341_SPK = "5120147c9c57132f6e7ecddba9800bb0c4449251c92a1e60371ee77557b6620f3ea3"
BIP341_CONTROL_BLOCK = "c1187791b6f712a8ea41c8ecdd0ee77fab3e85263b37e1ec18a3651926b3a6cf27"
BIP341_RAW_UNSIGNED_TX = bytes.fromhex(
    "02000000097de20cbff686da83a54981d2b9bab3586f4ca7e48f57f5b55963115f3b334e9c010000000000000000"
    "d7b7cab57b1393ace2d064f4d4a2cb8af6def61273e127517d44759b6dafdd990000000000ffffffff"
    "f8e1f583384333689228c5d28eac13366be082dc57441760d957275419a418420000000000ffffffff"
    "f0689180aa63b30cb162a73c6d2a38b7eeda2a83ece74310fda0843ad604853b0100000000feffffff"
    "aa5202bdf6d8ccd2ee0f0202afbbb7461d9264a25e5bfd3c5a52ee1239e0ba6c0000000000feffffff"
    "956149bdc66faa968eb2be2d2faa29718acbfe3941215893a2a3446d32acd050000000000000000"
    "000e664b9773b88c09c32cb70a2a3e4da0ced63b7ba3b22f848531bbb1d5d5f4c94010000000000000000"
    "e9aa6b8e6c9de67619e6a3924ae25696bb7b694bb677a632a74ef7eadfd4eabf0000000000ffffffff"
    "a778eb6a263dc090464cd125c466b5a99667720b1c110468831d058aa1b82af10100000000ffffffff"
    "0200ca9a3b000000001976a91406afd46bcdfd22ef94ac122aa11f241244a37ecc88ac"
    "807840cb0000000020ac9a87f5594be208f8532db38cff670c450ed2fea8fcdefcc9a663f78bab962b0065cd1d"
)
BIP341_PREVOUT_VALUES = [
    420000000,
    462000000,
    294000000,
    504000000,
    630000000,
    378000000,
    672000000,
    546000000,
    588000000,
]
BIP341_PREVOUT_SCRIPTS = [
    bytes.fromhex(value)
    for value in (
        "512053a1f6e454df1aa2776a2814a721372d6258050de330b3c6d10ee8f4e0dda343",
        "5120147c9c57132f6e7ecddba9800bb0c4449251c92a1e60371ee77557b6620f3ea3",
        "76a914751e76e8199196d454941c45d1b3a323f1433bd688ac",
        "5120e4d810fd50586274face62b8a807eb9719cef49c04177cc6b76a9a4251d5450e",
        "512091b64d5324723a985170e4dc5a0f84c041804f2cd12660fa5dec09fc21783605",
        "00147dd65592d0ab2fe0d0257d571abf032cd9db93dc",
        "512075169f4001aa68f15bbed28b218df1d0a62cbbcf1188c6665110c293c907b831",
        "5120712447206d7a5238acc7ff53fbe94a3b64539ad291c7cdbc490b7577e4b17df5",
        "512077e30a5522dd9f894c3f8b8bd4c4b2cf82ca7da8a3ea6a239655c39c050ab220",
    )
]


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

    expected = tagged_hash(
        "TapLeaf",
        bytes([LEAF_VERSION_TAPSCRIPT])
        + encode_varint(len(BIP341_LEAF_SCRIPT))
        + BIP341_LEAF_SCRIPT,
    )
    assert tapleaf_hash(BIP341_LEAF_SCRIPT) == expected


def test_two_leaf_tree_sorted_branch() -> None:
    leaf_a = (LEAF_VERSION_TAPSCRIPT, b"\x51")
    leaf_b = (LEAF_VERSION_TAPSCRIPT, b"\x52")
    ha = tapleaf_hash(b"\x51")
    hb = tapleaf_hash(b"\x52")
    assert taproot_merkle_root([leaf_a, leaf_b]) == tagged_hash(
        "TapBranch", min(ha, hb) + max(ha, hb)
    )


def test_to_xonly_roundtrip() -> None:
    pubkey = bytes(CKey(b"\x01" * 32).pub)
    assert to_xonly(pubkey) == pubkey[1:]
    assert to_xonly(pubkey[1:]) == pubkey[1:]


def test_encode_script_num() -> None:
    assert encode_script_num(0) == b""
    assert encode_script_num(1) == b"\x01"
    assert encode_script_num(32) == b"\x20"
    assert encode_script_num(0x80) == b"\x80\x00"


def _htlc_claim_leaf(preimage_hash: bytes, claim_xonly: bytes) -> bytes:
    return construct_script(
        [OP_HASH160, ripemd160(preimage_hash), OP_EQUALVERIFY, claim_xonly, OP_CHECKSIG]
    )


def test_script_path_spend_roundtrip() -> None:
    claim = CKey(b"\x01" * 32)
    refund = CKey(b"\x02" * 32)
    preimage_hash = tagged_hash("x", b"\x07" * 32)
    claim_xonly = bytes(claim.xonly_pub)
    claim_leaf = _htlc_claim_leaf(preimage_hash, claim_xonly)
    refund_leaf = construct_script([bytes(refund.xonly_pub), OP_CHECKSIG])
    spk = taproot_output_script(
        claim_xonly, [(LEAF_VERSION_TAPSCRIPT, claim_leaf), (LEAF_VERSION_TAPSCRIPT, refund_leaf)]
    )
    tx = TaprootTx(
        inputs=[TxIn(txid="11" * 32, vout=0)], outputs=[TxOut(value=9000, scriptpubkey=spk)]
    )

    sig = sign_taproot_scriptpath(tx, 0, [10000], [spk], claim_leaf, claim)
    sighash = taproot_sighash(tx, 0, [10000], [spk], leaf_hash=tapleaf_hash(claim_leaf))
    assert verify_schnorr(claim_xonly, sig, sighash)


def test_key_path_spend_roundtrip() -> None:
    internal = CKey(b"\x03" * 32)
    internal_xonly = bytes(internal.xonly_pub)
    spk = taproot_output_script(internal_xonly)
    tx = TaprootTx(
        inputs=[TxIn(txid="22" * 32, vout=1)], outputs=[TxOut(value=4900, scriptpubkey=spk)]
    )
    output_key = CKey(taproot_tweak_privkey(internal.secret_bytes))

    sig = sign_taproot_keypath(tx, 0, [5000], [spk], output_key)
    sighash = taproot_sighash(tx, 0, [5000], [spk])
    _, output_xonly = taproot_tweak_pubkey(internal_xonly)
    assert XOnlyPubKey(output_xonly).verify_schnorr(sighash, sig)


def test_tx_serialize_witness_roundtrip() -> None:
    spk = bytes.fromhex(BIP341_SPK)
    tx = TaprootTx(
        inputs=[TxIn(txid="33" * 32, vout=2)],
        outputs=[TxOut(value=1234, scriptpubkey=spk)],
        witnesses=[[b"\xaa" * 64]],
    )
    raw = tx.serialize()
    assert raw[4:6] == b"\x00\x01"
    assert isinstance(tx.txid(), str) and len(tx.txid()) == 64


def test_bip341_sighash_vector_matches_native() -> None:
    parsed = parse_transaction(BIP341_RAW_UNSIGNED_TX.hex())
    tx = TaprootTx(
        inputs=[
            TxIn(txid=txin.txid, vout=txin.vout, sequence=txin.sequence) for txin in parsed.inputs
        ],
        outputs=[TxOut(value=txout.value, scriptpubkey=txout.script) for txout in parsed.outputs],
        version=parsed.version,
        locktime=parsed.locktime,
    )
    expected = bytes.fromhex("2514a6272f85cfa0f45eb907fcb0d121b808ed37c6ea160a5a9046ed5526d555")

    actual = taproot_sighash(tx, 0, BIP341_PREVOUT_VALUES, BIP341_PREVOUT_SCRIPTS, SIGHASH_SINGLE)
    native_tx = CTransaction.deserialize(BIP341_RAW_UNSIGNED_TX)
    native_spent_outputs = [
        CTxOut(value, CScript(script))
        for value, script in zip(BIP341_PREVOUT_VALUES, BIP341_PREVOUT_SCRIPTS, strict=True)
    ]
    native = SignatureHashSchnorr(
        native_tx, 0, native_spent_outputs, hashtype=SIGHASH_Type(SIGHASH_SINGLE)
    )

    assert actual == expected
    assert actual == native


@pytest.mark.parametrize("sighash_type", [-1, 4, 0x80, 0x84, 0xFF])
def test_taproot_sighash_rejects_invalid_hash_types(sighash_type: int) -> None:
    tx = TaprootTx(inputs=[TxIn(txid="00" * 32, vout=0)], outputs=[])
    with pytest.raises(TaprootError, match="sighash type"):
        taproot_sighash(tx, 0, [1], [b"\x51"], sighash_type)


def test_taproot_sighash_rejects_invalid_single_and_leaf_hash() -> None:
    tx = TaprootTx(inputs=[TxIn(txid="00" * 32, vout=0)], outputs=[])
    with pytest.raises(TaprootError, match="corresponding output"):
        taproot_sighash(tx, 0, [1], [b"\x51"], SIGHASH_SINGLE)
    with pytest.raises(TaprootError, match="leaf_hash"):
        taproot_sighash(tx, 0, [1], [b"\x51"], leaf_hash=b"\x00" * 31)


def test_taproot_sighash_rejects_negative_input_index() -> None:
    tx = TaprootTx(inputs=[TxIn(txid="00" * 32, vout=0)], outputs=[])
    with pytest.raises(TaprootError, match="input index"):
        taproot_sighash(tx, -1, [1], [b"\x51"])


def test_verify_schnorr_rejects_noncanonical_taproot_signature_length() -> None:
    assert not verify_schnorr(bytes(CKey(b"\x01" * 32).xonly_pub), b"\x00" * 65, b"\x00" * 32)
