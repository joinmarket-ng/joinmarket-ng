"""Focused cryptographic checks for the bounded channel-input escrow module."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable

import pytest
from coincurve import PrivateKey
from jmcore.bitcoin import create_p2tr_scriptpubkey, parse_transaction_bytes
from jmcore.musig2 import nonce_agg, sign_partial
from jmcore.taproot import TaprootTx, TxIn, TxOut, taproot_merkle_root

from jmswap.bitcoin_escrow import (
    ChannelInputEscrowTree,
    UnsignedTimeoutSpend,
    build_success_spend,
    build_timeout_spend,
    finalize_timeout_spend,
    parse_escrow_transaction,
    timeout_nonce,
    timeout_session,
    verify_success_spend,
    verify_timeout,
)
from jmswap.errors import ChannelInputEscrowError
from jmswap.spend import SwapOutpoint

MAKER_ESCROW = PrivateKey((1).to_bytes(32, "big"))
PEER_ESCROW = PrivateKey((2).to_bytes(32, "big"))
MAKER_CLAIM = PrivateKey((3).to_bytes(32, "big"))
PREIMAGE = bytes.fromhex("11" * 32)
PAYMENT_HASH = hashlib.sha256(PREIMAGE).digest()
MAKER_SCRIPT = create_p2tr_scriptpubkey(bytes.fromhex("44" * 32))
PEER_SCRIPT = create_p2tr_scriptpubkey(bytes.fromhex("55" * 32))
ESCROW_VALUE = 100_000
PEER_CLAIM = 30_000
COMPENSATION = 250
TIMEOUT_FEE = 200
SUCCESS_FEE = 180
TIMEOUT_HEIGHT = 850_000


def _tree() -> ChannelInputEscrowTree:
    return ChannelInputEscrowTree(
        maker_escrow_pubkey=MAKER_ESCROW.public_key.format(compressed=True),
        peer_escrow_pubkey=PEER_ESCROW.public_key.format(compressed=True),
        maker_claim_pubkey=MAKER_CLAIM.public_key.format(compressed=True),
        payment_hash=PAYMENT_HASH,
    )


def _outpoint(tree: ChannelInputEscrowTree) -> SwapOutpoint:
    return SwapOutpoint(
        txid="ab" * 32,
        vout=4,
        value=ESCROW_VALUE,
        scriptpubkey=tree.funding_output_script(),
    )


def _unsigned_timeout() -> tuple[ChannelInputEscrowTree, SwapOutpoint, UnsignedTimeoutSpend]:
    tree = _tree()
    outpoint = _outpoint(tree)
    return (
        tree,
        outpoint,
        build_timeout_spend(
            tree,
            outpoint,
            MAKER_SCRIPT,
            PEER_SCRIPT,
            PEER_CLAIM,
            COMPENSATION,
            TIMEOUT_FEE,
            TIMEOUT_HEIGHT,
        ),
    )


def _signed_timeout() -> tuple[ChannelInputEscrowTree, SwapOutpoint, TaprootTx]:
    tree, outpoint, spend = _unsigned_timeout()
    maker_nonce, maker_pubnonce = timeout_nonce(tree.maker_escrow_pubkey, b"\x01" * 32)
    peer_nonce, peer_pubnonce = timeout_nonce(tree.peer_escrow_pubkey, b"\x02" * 32)
    session = timeout_session(tree, nonce_agg([maker_pubnonce, peer_pubnonce]), spend.sighash)
    maker_partial = sign_partial(maker_nonce, MAKER_ESCROW.secret, session)
    peer_partial = sign_partial(peer_nonce, PEER_ESCROW.secret, session)
    tx = finalize_timeout_spend(
        tree,
        spend,
        maker_pubnonce,
        peer_pubnonce,
        maker_partial,
        peer_partial,
    )
    return tree, outpoint, tx


def _verify_timeout(tree: ChannelInputEscrowTree, outpoint: SwapOutpoint, tx: TaprootTx) -> bool:
    return verify_timeout(
        tree,
        outpoint,
        MAKER_SCRIPT,
        PEER_SCRIPT,
        PEER_CLAIM,
        COMPENSATION,
        TIMEOUT_FEE,
        TIMEOUT_HEIGHT,
        tx,
    )


def test_tree_has_fixed_success_leaf_and_output() -> None:
    tree = _tree()
    expected_leaf = bytes([0xA8, 0x20]) + PAYMENT_HASH + bytes([0x88, 0x20])
    expected_leaf += MAKER_CLAIM.public_key.format(compressed=True)[1:] + bytes([0xAC])
    leaf, control = tree.success_control_block()

    assert tree.success_leaf == expected_leaf
    assert tree.merkle_root() == taproot_merkle_root((0xC0, expected_leaf))
    assert len(tree.internal_xonly()) == 32
    assert tree.funding_output_script() == b"\x51\x20" + tree.tweaked_aggregate_xonly()
    assert leaf == expected_leaf
    assert len(control) == 33


def test_timeout_is_fully_signed_and_serializes_deterministically() -> None:
    tree, outpoint, tx = _signed_timeout()

    assert _verify_timeout(tree, outpoint, tx)
    assert tx.outputs[0].value == ESCROW_VALUE - PEER_CLAIM - COMPENSATION - TIMEOUT_FEE
    assert tx.outputs[1].value == PEER_CLAIM + COMPENSATION
    assert tx.inputs[0].sequence == 0xFFFFFFFE
    raw = tx.serialize()
    parsed = parse_transaction_bytes(raw)
    assert parsed.version == 2
    assert parsed.locktime == TIMEOUT_HEIGHT
    assert parsed.inputs[0].txid_le[::-1].hex() == outpoint.txid
    assert parsed.witnesses == tx.witnesses
    assert _verify_timeout(tree, outpoint, parse_escrow_transaction(raw.hex()))
    assert (
        tx.txid()
        == TaprootTx(
            inputs=tx.inputs,
            outputs=tx.outputs,
            version=tx.version,
            locktime=tx.locktime,
        ).txid()
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda tx: tx.inputs.__setitem__(
            0, TxIn("cd" * 32, tx.inputs[0].vout, tx.inputs[0].sequence)
        ),
        lambda tx: setattr(tx, "locktime", TIMEOUT_HEIGHT + 1),
        lambda tx: tx.inputs.__setitem__(0, TxIn(tx.inputs[0].txid, tx.inputs[0].vout, 0xFFFFFFFD)),
        lambda tx: tx.outputs.__setitem__(
            0, TxOut(tx.outputs[0].value - 1, tx.outputs[0].scriptpubkey)
        ),
        lambda tx: tx.outputs.__setitem__(0, TxOut(tx.outputs[0].value, PEER_SCRIPT)),
        lambda tx: tx.outputs.reverse(),
        lambda tx: tx.witnesses[0].__setitem__(0, b"\x00" * 64),
    ],
)
def test_timeout_verifier_rejects_mutations(mutate: Callable[[TaprootTx], None]) -> None:
    tree, outpoint, tx = _signed_timeout()
    changed = copy.deepcopy(tx)
    mutate(changed)

    with pytest.raises(ChannelInputEscrowError):
        _verify_timeout(tree, outpoint, changed)


def test_timeout_finalization_rejects_invalid_partial_signatures() -> None:
    tree, _, spend = _unsigned_timeout()
    maker_nonce, maker_pubnonce = timeout_nonce(tree.maker_escrow_pubkey, b"\x01" * 32)
    peer_nonce, peer_pubnonce = timeout_nonce(tree.peer_escrow_pubkey, b"\x02" * 32)
    session = timeout_session(tree, nonce_agg([maker_pubnonce, peer_pubnonce]), spend.sighash)
    maker_partial = sign_partial(maker_nonce, MAKER_ESCROW.secret, session)
    peer_partial = sign_partial(peer_nonce, PEER_ESCROW.secret, session)

    with pytest.raises(ChannelInputEscrowError, match="maker timeout partial"):
        finalize_timeout_spend(
            tree,
            spend,
            maker_pubnonce,
            peer_pubnonce,
            b"\x00" * 32,
            peer_partial,
        )
    with pytest.raises(ChannelInputEscrowError, match="peer timeout partial"):
        finalize_timeout_spend(
            tree,
            spend,
            maker_pubnonce,
            peer_pubnonce,
            maker_partial,
            b"\x00" * 32,
        )


def test_timeout_builder_rejects_invalid_economics_and_funding_script() -> None:
    tree = _tree()
    bad_outpoint = SwapOutpoint("ab" * 32, 4, ESCROW_VALUE, b"\x51\x20" + b"\x00" * 32)

    with pytest.raises(ChannelInputEscrowError, match="funding script"):
        build_timeout_spend(
            tree,
            bad_outpoint,
            MAKER_SCRIPT,
            PEER_SCRIPT,
            PEER_CLAIM,
            COMPENSATION,
            TIMEOUT_FEE,
            TIMEOUT_HEIGHT,
        )
    with pytest.raises(ChannelInputEscrowError, match="outputs"):
        build_timeout_spend(
            tree,
            _outpoint(tree),
            MAKER_SCRIPT,
            PEER_SCRIPT,
            0,
            0,
            TIMEOUT_FEE,
            TIMEOUT_HEIGHT,
        )


def test_success_spend_verifies_and_serializes() -> None:
    tree = _tree()
    outpoint = _outpoint(tree)
    tx = build_success_spend(
        tree, outpoint, PREIMAGE, MAKER_CLAIM.secret, MAKER_SCRIPT, SUCCESS_FEE
    )

    assert verify_success_spend(tree, outpoint, MAKER_SCRIPT, SUCCESS_FEE, tx)
    parsed = parse_transaction_bytes(tx.serialize())
    assert parsed.version == 2
    assert parsed.locktime == 0
    assert parsed.inputs[0].sequence == 0xFFFFFFFD
    assert parsed.witnesses == tx.witnesses
    assert verify_success_spend(
        tree,
        outpoint,
        MAKER_SCRIPT,
        SUCCESS_FEE,
        parse_escrow_transaction(tx.serialize().hex()),
    )
    assert tx.txid() == TaprootTx(inputs=tx.inputs, outputs=tx.outputs).txid()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda tx: tx.witnesses[0].__setitem__(1, b"\x22" * 32),
        lambda tx: tx.inputs.__setitem__(0, TxIn(tx.inputs[0].txid, tx.inputs[0].vout, 0xFFFFFFFE)),
        lambda tx: tx.outputs.__setitem__(
            0, TxOut(tx.outputs[0].value - 1, tx.outputs[0].scriptpubkey)
        ),
        lambda tx: tx.witnesses[0].__setitem__(2, b"\x51"),
        lambda tx: tx.witnesses[0].__setitem__(3, b"\xc0" + b"\x00" * 32),
        lambda tx: tx.witnesses[0].__setitem__(0, b"\x00" * 64),
    ],
)
def test_success_verifier_rejects_mutations(mutate: Callable[[TaprootTx], None]) -> None:
    tree = _tree()
    outpoint = _outpoint(tree)
    tx = build_success_spend(
        tree, outpoint, PREIMAGE, MAKER_CLAIM.secret, MAKER_SCRIPT, SUCCESS_FEE
    )
    changed = copy.deepcopy(tx)
    mutate(changed)

    with pytest.raises(ChannelInputEscrowError):
        verify_success_spend(tree, outpoint, MAKER_SCRIPT, SUCCESS_FEE, changed)


def test_success_builder_rejects_wrong_preimage_and_fee() -> None:
    tree = _tree()
    outpoint = _outpoint(tree)

    with pytest.raises(ChannelInputEscrowError, match="preimage"):
        build_success_spend(
            tree, outpoint, b"\x22" * 32, MAKER_CLAIM.secret, MAKER_SCRIPT, SUCCESS_FEE
        )
    with pytest.raises(ChannelInputEscrowError, match="positive"):
        build_success_spend(
            tree, outpoint, PREIMAGE, MAKER_CLAIM.secret, MAKER_SCRIPT, ESCROW_VALUE
        )


def test_tree_rejects_invalid_keys_and_hash() -> None:
    with pytest.raises(ChannelInputEscrowError):
        ChannelInputEscrowTree(
            b"\x02" * 33, PEER_ESCROW.public_key.format(compressed=True), b"", b""
        )

    maker_key = MAKER_ESCROW.public_key.format(compressed=True)
    with pytest.raises(ChannelInputEscrowError, match="distinct"):
        ChannelInputEscrowTree(maker_key, maker_key, MAKER_CLAIM.public_key.format(), PAYMENT_HASH)


def test_parse_escrow_transaction_rejects_non_witness_serialization() -> None:
    tree, _, spend = _unsigned_timeout()
    assert tree.funding_output_script()

    with pytest.raises(ChannelInputEscrowError, match="SegWit"):
        parse_escrow_transaction(spend.tx._serialize_no_witness().hex())


def test_success_verifier_rejects_changed_fee() -> None:
    tree = _tree()
    outpoint = _outpoint(tree)
    tx = build_success_spend(
        tree, outpoint, PREIMAGE, MAKER_CLAIM.secret, MAKER_SCRIPT, SUCCESS_FEE
    )

    with pytest.raises(ChannelInputEscrowError):
        verify_success_spend(tree, outpoint, MAKER_SCRIPT, SUCCESS_FEE + 1, tx)


def test_timeout_verifier_rejects_changed_fee() -> None:
    tree, outpoint, tx = _signed_timeout()

    with pytest.raises(ChannelInputEscrowError):
        verify_timeout(
            tree,
            outpoint,
            MAKER_SCRIPT,
            PEER_SCRIPT,
            PEER_CLAIM,
            COMPENSATION,
            TIMEOUT_FEE + 1,
            TIMEOUT_HEIGHT,
            tx,
        )


def test_timeout_rejects_invalid_output_script() -> None:
    tree = _tree()
    with pytest.raises(ChannelInputEscrowError, match="truncated"):
        build_timeout_spend(
            tree,
            _outpoint(tree),
            b"\x4c",
            PEER_SCRIPT,
            PEER_CLAIM,
            COMPENSATION,
            TIMEOUT_FEE,
            TIMEOUT_HEIGHT,
        )


def test_success_transaction_requires_exact_funding_outpoint() -> None:
    tree = _tree()
    outpoint = _outpoint(tree)
    tx = build_success_spend(
        tree, outpoint, PREIMAGE, MAKER_CLAIM.secret, MAKER_SCRIPT, SUCCESS_FEE
    )
    replacement = SwapOutpoint("cd" * 32, outpoint.vout, outpoint.value, outpoint.scriptpubkey)

    with pytest.raises(ChannelInputEscrowError, match="exact escrow outpoint"):
        verify_success_spend(tree, replacement, MAKER_SCRIPT, SUCCESS_FEE, tx)


def test_timeout_rejects_timestamp_locktime() -> None:
    tree = _tree()
    with pytest.raises(ChannelInputEscrowError, match="block height"):
        build_timeout_spend(
            tree,
            _outpoint(tree),
            MAKER_SCRIPT,
            PEER_SCRIPT,
            PEER_CLAIM,
            COMPENSATION,
            TIMEOUT_FEE,
            500_000_000,
        )


def test_timeout_witness_must_be_one_default_signature() -> None:
    tree, outpoint, tx = _signed_timeout()
    tx.witnesses = [[tx.witnesses[0][0], b"extra"]]

    with pytest.raises(ChannelInputEscrowError, match="witness"):
        _verify_timeout(tree, outpoint, tx)


def test_success_witness_must_be_script_path_shape() -> None:
    tree = _tree()
    outpoint = _outpoint(tree)
    tx = build_success_spend(
        tree, outpoint, PREIMAGE, MAKER_CLAIM.secret, MAKER_SCRIPT, SUCCESS_FEE
    )
    tx.witnesses = [[tx.witnesses[0][0]]]

    with pytest.raises(ChannelInputEscrowError, match="witness"):
        verify_success_spend(tree, outpoint, MAKER_SCRIPT, SUCCESS_FEE, tx)
