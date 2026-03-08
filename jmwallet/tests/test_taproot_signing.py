import hashlib

from coincurve import PrivateKey, PublicKeyXOnly
from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    create_p2tr_scriptpubkey,
    taproot_tweak_pubkey,
)

from jmwallet.wallet.signing import SIGHASH_DEFAULT, compute_sighash_taproot, sign_p2tr_input


def test_taproot_sighash_and_signing():
    # 1. Setup keys
    # Use a fixed key for reproducibility
    priv_bytes = bytes.fromhex("1234567890123456789012345678901234567890123456789012345678901234")
    priv = PrivateKey(priv_bytes)
    internal_pub = priv.public_key.format(compressed=True)[1:]  # x-only

    # Tweak it for Taproot key-path spend
    y_parity, tweaked_pub = taproot_tweak_pubkey(internal_pub)

    # Calculating tweak manually to derive tweaked private key
    # Tweak = tagged_hash("TapTweak", internal_pub + h)
    # Since h is b"" for keypath spend:
    tag_hash = hashlib.sha256(b"TapTweak").digest()
    tweak = hashlib.sha256(tag_hash + tag_hash + internal_pub).digest()

    # Tweaked private key: q = p + tweak
    tweaked_priv = priv.add(tweak)

    # 2. Setup transaction
    tx = ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[TxInput(txid_le=bytes(32), vout=0, scriptsig=b"", sequence=0xFFFFFFFF)],
        outputs=[TxOutput(value=90000, script=create_p2tr_scriptpubkey(tweaked_pub))],
        locktime=0,
        witnesses=[],
    )

    # Prevouts (required for Taproot sighash)
    prevouts_values = [100000]
    prevouts_scripts = [create_p2tr_scriptpubkey(tweaked_pub)]

    # 3. Compute sighash
    sighash = compute_sighash_taproot(
        tx,
        input_index=0,
        prevouts_values=prevouts_values,
        prevouts_scripts=prevouts_scripts,
        sighash_type=SIGHASH_DEFAULT,
    )

    assert len(sighash) == 32

    # 4. Sign
    sig = sign_p2tr_input(
        tx,
        input_index=0,
        prevouts_values=prevouts_values,
        prevouts_scripts=prevouts_scripts,
        private_key=tweaked_priv,
        sighash_type=SIGHASH_DEFAULT,
    )

    assert len(sig) == 64  # Schnorr sig for SIGHASH_DEFAULT

    # 5. Verify using coincurve
    # Note: PublicKeyXOnly.verify expects (sig, message)
    tweaked_pub_obj = PublicKeyXOnly(tweaked_pub)
    assert tweaked_pub_obj.verify(sig, sighash)

    print("Taproot signing and verification successful!")
