"""Tests for BIP341 Taproot key-path signing."""

from __future__ import annotations

import hashlib

import pytest
from coincurve import PrivateKey, PublicKeyXOnly
from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    create_p2tr_scriptpubkey,
    taproot_tweak_privkey,
    taproot_tweak_pubkey,
)

from jmwallet.wallet.signing import (
    SIGHASH_DEFAULT,
    TransactionSigningError,
    compute_sighash_taproot,
    sign_p2tr_input,
    verify_p2tr_signature,
)


def _find_privkey_with_parity(parity_byte: int) -> bytes:
    seed = hashlib.sha256(f"parity{parity_byte}".encode()).digest()
    for index in range(1000):
        candidate = hashlib.sha256(seed + index.to_bytes(4, "big")).digest()
        if PrivateKey(candidate).public_key.format(compressed=True)[0] == parity_byte:
            return candidate
    raise RuntimeError("could not find key with requested parity")


def _spend_tx(scriptpubkey: bytes) -> ParsedTransaction:
    return ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[TxInput(txid_le=bytes(32), vout=0, scriptsig=b"", sequence=0xFFFFFFFF)],
        outputs=[TxOutput(value=90_000, script=scriptpubkey)],
        locktime=0,
        witnesses=[],
    )


@pytest.mark.parametrize("parity_byte", [0x02, 0x03])
def test_taproot_keypath_signing(parity_byte: int) -> None:
    private_key = _find_privkey_with_parity(parity_byte)
    internal_xonly = PrivateKey(private_key).public_key.format(compressed=True)[1:]
    _parity, output_xonly = taproot_tweak_pubkey(internal_xonly)
    output_private_key = PrivateKey(taproot_tweak_privkey(private_key))
    scriptpubkey = create_p2tr_scriptpubkey(output_xonly)
    tx = _spend_tx(scriptpubkey)

    signature = sign_p2tr_input(
        tx, 0, [100_000], [scriptpubkey], output_private_key, SIGHASH_DEFAULT
    )
    sighash = compute_sighash_taproot(tx, 0, [100_000], [scriptpubkey])

    assert len(signature) == 64
    assert PublicKeyXOnly(output_xonly).verify(signature, sighash)
    assert verify_p2tr_signature(tx, 0, [100_000], [scriptpubkey], signature, output_xonly)


def test_taproot_sighash_requires_all_prevouts() -> None:
    tx = _spend_tx(create_p2tr_scriptpubkey(b"\x11" * 32))

    with pytest.raises(TransactionSigningError, match="Prevouts length"):
        compute_sighash_taproot(tx, 0, [], [])
