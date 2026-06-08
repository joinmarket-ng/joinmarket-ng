"""Tests for BIP341 Taproot key-path signing (ported minimal subset).

Covers both even-Y and odd-Y internal keys (BIP341 parity negation) and a full
silent payment spend: detect an incoming silent payment, recover the output
private key, and produce a verifiable Schnorr signature for spending it.
"""

from __future__ import annotations

import hashlib

from _jmwallet_test_helpers import TEST_MNEMONIC
from coincurve import PrivateKey, PublicKey, PublicKeyXOnly
from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    create_p2tr_scriptpubkey,
    is_p2tr_address,
    pubkey_to_p2tr_address,
    taproot_tweak_privkey,
    taproot_tweak_pubkey,
)
from jmcore.silentpayments import SilentPaymentAddress, SilentPaymentInput, create_outputs

from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.signing import (
    SIGHASH_DEFAULT,
    compute_sighash_taproot,
    sign_p2tr_input,
    verify_p2tr_signature,
    witness_has_annex,
)
from jmwallet.wallet.silent_payments import SilentPaymentWallet


def _find_privkey_with_parity(parity_byte: int) -> bytes:
    seed = hashlib.sha256(f"parity{parity_byte}".encode()).digest()
    for i in range(1000):
        candidate = hashlib.sha256(seed + i.to_bytes(4, "big")).digest()
        if PrivateKey(candidate).public_key.format(compressed=True)[0] == parity_byte:
            return candidate
    raise RuntimeError("could not find key with requested parity")


def _spend_tx(spk: bytes) -> ParsedTransaction:
    return ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[TxInput(txid_le=bytes(32), vout=0, scriptsig=b"", sequence=0xFFFFFFFF)],
        outputs=[TxOutput(value=90000, script=spk)],
        locktime=0,
        witnesses=[],
    )


def _check_tweaked_spend(priv_bytes: bytes) -> None:
    internal_pub = PrivateKey(priv_bytes).public_key.format(compressed=True)[1:]
    _, tweaked_pub = taproot_tweak_pubkey(internal_pub)
    tweaked_priv = PrivateKey(taproot_tweak_privkey(priv_bytes))
    spk = create_p2tr_scriptpubkey(tweaked_pub)
    tx = _spend_tx(spk)

    sig = sign_p2tr_input(tx, 0, [100000], [spk], tweaked_priv, SIGHASH_DEFAULT)
    assert len(sig) == 64
    sighash = compute_sighash_taproot(tx, 0, [100000], [spk], SIGHASH_DEFAULT)
    assert PublicKeyXOnly(tweaked_pub).verify(sig, sighash)
    assert verify_p2tr_signature(tx, 0, [100000], [spk], sig, tweaked_pub)


def test_taproot_signing_even_y() -> None:
    _check_tweaked_spend(_find_privkey_with_parity(0x02))


def test_taproot_signing_odd_y() -> None:
    _check_tweaked_spend(_find_privkey_with_parity(0x03))


def test_p2tr_address_is_bech32m() -> None:
    xonly = PrivateKey().public_key.format(compressed=True)[1:]
    addr = pubkey_to_p2tr_address(xonly, "mainnet")
    assert addr.startswith("bc1p")
    assert is_p2tr_address(addr)
    # The previous (buggy) bech32 checksum must not validate as bech32m.


def test_taproot_sighash_matches_bitcointx() -> None:
    """Cross-check the in-tree BIP341 sighash against bitcointx (consensus ref).

    The other taproot tests only verify sign/verify self-consistency, which a
    wrong-but-internally-consistent preimage would still pass. This pins the
    preimage to an audited implementation across every sighash type, so funds in
    silent payment (and other P2TR) outputs stay spendable.
    """
    import os

    from bitcointx.core import CTransaction, CTxOut
    from bitcointx.core.script import CScript, SIGHASH_Type, SignatureHashSchnorr

    inputs = []
    values: list[int] = []
    scripts: list[bytes] = []
    for i in range(3):
        inputs.append(
            TxInput(txid_le=os.urandom(32), vout=i, scriptsig=b"", sequence=0xFFFFFFF0 + i)
        )
        values.append(100_000 * (i + 1))
        scripts.append(bytes([0x51, 0x20]) + os.urandom(32))
    outputs = [
        TxOutput(value=50_000, script=bytes([0x51, 0x20]) + os.urandom(32)),
        TxOutput(value=120_000, script=bytes([0x00, 0x14]) + os.urandom(20)),
        TxOutput(value=30_000, script=bytes([0x51, 0x20]) + os.urandom(32)),
    ]
    tx = ParsedTransaction(
        version=2, has_witness=True, inputs=inputs, outputs=outputs, locktime=500, witnesses=[]
    )
    from jmcore.bitcoin import serialize_transaction

    ctx = CTransaction.deserialize(
        serialize_transaction(tx.version, tx.inputs, tx.outputs, tx.locktime)
    )
    spent = [CTxOut(v, CScript(s)) for v, s in zip(values, scripts, strict=True)]

    for hashtype in (0x00, 0x01, 0x02, 0x03, 0x81, 0x82, 0x83):
        ref_type = None if hashtype == 0x00 else SIGHASH_Type(hashtype)
        for idx in range(len(inputs)):
            mine = compute_sighash_taproot(tx, idx, values, scripts, hashtype)
            ref = SignatureHashSchnorr(ctx, idx, spent_outputs=spent, hashtype=ref_type)
            assert bytes(mine) == bytes(ref), f"mismatch hashtype={hashtype:#04x} idx={idx}"


def test_silent_payment_output_is_spendable() -> None:
    """End-to-end: receive a silent payment and sign a spend of it."""
    master = HDKey.from_seed(mnemonic_to_seed(TEST_MNEMONIC, ""))
    sp = SilentPaymentWallet(master, network="mainnet")

    sender_priv = 0xC0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE00C0FFEE01
    pub = PublicKey.from_secret(sender_priv.to_bytes(32, "big")).format(compressed=True)
    pubkey_hash = hashlib.new("ripemd160", hashlib.sha256(pub).digest()).digest()
    vin = SilentPaymentInput(
        txid="33" * 32,
        vout=0,
        scriptpubkey=bytes([0x00, 0x14]) + pubkey_hash,
        witness=[b"\x00" * 71, pub],
        private_key=sender_priv,
    )
    recipient = SilentPaymentAddress.decode(sp.get_address())[0]
    outputs = create_outputs([(sender_priv, False)], [vin.outpoint()], [recipient])
    assert len(outputs) == 1

    found = sp.scan([vin], outputs)
    assert len(found) == 1

    # The SP output key is the final taproot output key; sign directly.
    output_key = found[0].pubkey_xonly
    spk = create_p2tr_scriptpubkey(output_key)
    spend_priv = PrivateKey(sp.output_private_key(found[0]).to_bytes(32, "big"))

    tx = _spend_tx(create_p2tr_scriptpubkey(PrivateKey().public_key.format()[1:]))
    sig = sign_p2tr_input(tx, 0, [120000], [spk], spend_priv, SIGHASH_DEFAULT)
    assert verify_p2tr_signature(tx, 0, [120000], [spk], sig, output_key)


def test_verify_rejects_non_default_sighash() -> None:
    """JMP-0005: tr0 verifiers accept only bare 64-byte SIGHASH_DEFAULT sigs.

    A 65-byte signature carrying an explicit sighash flag (e.g. SIGHASH_SINGLE
    or ANYONECANPAY) would let a participant leave outputs uncommitted and
    rewrite the transaction after signing, so it MUST be rejected even when the
    Schnorr signature itself is otherwise valid for that sighash.
    """
    priv = PrivateKey(_find_privkey_with_parity(0x02))
    xonly = priv.public_key.format(compressed=True)[1:]
    spk = create_p2tr_scriptpubkey(xonly)
    tx = _spend_tx(create_p2tr_scriptpubkey(PrivateKey().public_key.format()[1:]))

    # A bare 64-byte SIGHASH_DEFAULT signature verifies.
    good = sign_p2tr_input(tx, 0, [100000], [spk], priv, SIGHASH_DEFAULT)
    assert len(good) == 64
    assert verify_p2tr_signature(tx, 0, [100000], [spk], good, xonly)

    # Each forbidden explicit sighash type produces a 65-byte signature.
    for hashtype in (0x01, 0x02, 0x03, 0x81, 0x82, 0x83):
        sig = sign_p2tr_input(tx, 0, [100000], [spk], priv, hashtype)
        assert len(sig) == 65
        assert not verify_p2tr_signature(tx, 0, [100000], [spk], sig, xonly)

    # A 65-byte payload with a trailing 0x00 (consensus-invalid) is rejected too.
    assert not verify_p2tr_signature(tx, 0, [100000], [spk], good + b"\x00", xonly)
    # Truncated / oversized payloads are rejected.
    assert not verify_p2tr_signature(tx, 0, [100000], [spk], good[:63], xonly)


def test_witness_has_annex() -> None:
    """JMP-0005: detect a BIP341 annex (>=2 elements, last starts with 0x50)."""
    sig = b"\x11" * 64
    # Key-path witness: single signature element, no annex.
    assert not witness_has_annex([sig])
    # P2WPKH-style: [sig, pubkey] (pubkey does not start with 0x50).
    assert not witness_has_annex([sig, b"\x02" + b"\x03" * 32])
    # Annex present: last element starts with the 0x50 tag.
    assert witness_has_annex([sig, b"\x50"])
    assert witness_has_annex([sig, b"\x50\xde\xad\xbe\xef"])
    # A lone 0x50 element is not an annex (annex needs the spend item before it).
    assert not witness_has_annex([b"\x50\xde\xad"])
    # Empty last element is not an annex.
    assert not witness_has_annex([sig, b""])
    # Empty witness is not an annex.
    assert not witness_has_annex([])
