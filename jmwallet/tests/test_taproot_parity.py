"""Test Taproot signing with keys that require odd-Y parity negation (BIP341)."""

from __future__ import annotations

import hashlib

from coincurve import PrivateKey, PublicKeyXOnly
from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    create_p2tr_scriptpubkey,
    taproot_tweak_privkey,
    taproot_tweak_pubkey,
)

from jmwallet.wallet.signing import SIGHASH_DEFAULT, compute_sighash_taproot, sign_p2tr_input


def _find_odd_y_privkey() -> bytes:
    """Find a private key whose compressed public key has prefix 0x03 (odd Y).

    BIP341 requires negating the private key when the internal public key
    has an odd Y coordinate. This helper finds such a key deterministically
    by iterating from a seed until we find one with prefix 0x03.
    """
    seed = hashlib.sha256(b"find_odd_y_test_key").digest()
    for i in range(1000):
        candidate = hashlib.sha256(seed + i.to_bytes(4, "big")).digest()
        priv = PrivateKey(candidate)
        if priv.public_key.format(compressed=True)[0] == 0x03:
            return candidate
    raise RuntimeError("Could not find odd-Y key in 1000 iterations")


def _find_even_y_privkey() -> bytes:
    """Find a private key with even Y coordinate (prefix 0x02) for comparison."""
    seed = hashlib.sha256(b"find_even_y_test_key").digest()
    for i in range(1000):
        candidate = hashlib.sha256(seed + i.to_bytes(4, "big")).digest()
        priv = PrivateKey(candidate)
        if priv.public_key.format(compressed=True)[0] == 0x02:
            return candidate
    raise RuntimeError("Could not find even-Y key in 1000 iterations")


def test_taproot_signing_odd_y_key():
    """Sign and verify a Taproot input using a key whose Y is odd.

    This exercises the BIP341-mandated path in taproot_tweak_privkey()
    where the private key scalar is negated before tweaking:
        if pub.format(compressed=True)[0] == 0x03:
            d = (SECP256K1_N - d) % SECP256K1_N
    """
    priv_bytes = _find_odd_y_privkey()
    priv = PrivateKey(priv_bytes)

    # Verify we really have an odd-Y key
    assert priv.public_key.format(compressed=True)[0] == 0x03, (
        "Test key should have odd Y coordinate"
    )

    internal_pub = priv.public_key.format(compressed=True)[1:]

    # Tweak the keys using the project's functions
    _, tweaked_pub = taproot_tweak_pubkey(internal_pub)
    tweaked_priv_bytes = taproot_tweak_privkey(priv_bytes)
    tweaked_priv = PrivateKey(tweaked_priv_bytes)

    # Build a test transaction
    spk = create_p2tr_scriptpubkey(tweaked_pub)
    tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[TxInput(txid_le=bytes(32), vout=0, scriptsig=b"", sequence=0xFFFFFFFF)],
        outputs=[TxOutput(value=90000, script=spk)],
        locktime=0,
        witnesses=[],
    )

    prevouts_values = [100000]
    prevouts_scripts = [spk]

    # Sign
    sig = sign_p2tr_input(
        tx,
        input_index=0,
        prevouts_values=prevouts_values,
        prevouts_scripts=prevouts_scripts,
        private_key=tweaked_priv,
        sighash_type=SIGHASH_DEFAULT,
    )

    assert len(sig) == 64, "SIGHASH_DEFAULT should produce 64-byte signature"

    # Verify
    sighash = compute_sighash_taproot(
        tx,
        input_index=0,
        prevouts_values=prevouts_values,
        prevouts_scripts=prevouts_scripts,
        sighash_type=SIGHASH_DEFAULT,
    )
    tweaked_pub_obj = PublicKeyXOnly(tweaked_pub)
    assert tweaked_pub_obj.verify(sig, sighash), (
        "Signature must verify for odd-Y key after BIP341 negation"
    )


def test_taproot_signing_even_y_key():
    """Sign and verify with an even-Y key to confirm both paths work."""
    priv_bytes = _find_even_y_privkey()
    priv = PrivateKey(priv_bytes)

    assert priv.public_key.format(compressed=True)[0] == 0x02

    internal_pub = priv.public_key.format(compressed=True)[1:]
    _, tweaked_pub = taproot_tweak_pubkey(internal_pub)
    tweaked_priv_bytes = taproot_tweak_privkey(priv_bytes)
    tweaked_priv = PrivateKey(tweaked_priv_bytes)

    spk = create_p2tr_scriptpubkey(tweaked_pub)
    tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[TxInput(txid_le=bytes(32), vout=0, scriptsig=b"", sequence=0xFFFFFFFF)],
        outputs=[TxOutput(value=90000, script=spk)],
        locktime=0,
        witnesses=[],
    )

    prevouts_values = [100000]
    prevouts_scripts = [spk]

    sig = sign_p2tr_input(
        tx,
        input_index=0,
        prevouts_values=prevouts_values,
        prevouts_scripts=prevouts_scripts,
        private_key=tweaked_priv,
        sighash_type=SIGHASH_DEFAULT,
    )

    assert len(sig) == 64

    sighash = compute_sighash_taproot(
        tx,
        input_index=0,
        prevouts_values=prevouts_values,
        prevouts_scripts=prevouts_scripts,
        sighash_type=SIGHASH_DEFAULT,
    )
    tweaked_pub_obj = PublicKeyXOnly(tweaked_pub)
    assert tweaked_pub_obj.verify(sig, sighash)
