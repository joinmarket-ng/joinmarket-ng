"""Tests for BIP341 Taproot key-path signing."""

from __future__ import annotations

import hashlib

import pytest
from bitcointx.core.key import CKey, XOnlyPubKey
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
    SIGHASH_SINGLE,
    TransactionSigningError,
    compute_sighash_taproot,
    sign_p2tr_input,
    verify_p2tr_signature,
)


def _find_key_with_parity(parity_byte: int) -> CKey:
    seed = hashlib.sha256(f"parity{parity_byte}".encode()).digest()
    for index in range(1000):
        key = CKey(hashlib.sha256(seed + index.to_bytes(4, "big")).digest())
        if bytes(key.pub)[0] == parity_byte:
            return key
    raise RuntimeError("could not find key with requested parity")


def _spend_tx(scriptpubkey: bytes, outputs: list[TxOutput] | None = None) -> ParsedTransaction:
    return ParsedTransaction(
        version=2,
        has_witness=True,
        inputs=[TxInput(txid_le=bytes(32), vout=0, scriptsig=b"", sequence=0xFFFFFFFF)],
        outputs=outputs if outputs is not None else [TxOutput(value=90_000, script=scriptpubkey)],
        locktime=0,
        witnesses=[],
    )


@pytest.mark.parametrize("parity_byte", [0x02, 0x03])
def test_taproot_keypath_signing(parity_byte: int) -> None:
    private_key = _find_key_with_parity(parity_byte)
    internal_xonly = bytes(private_key.xonly_pub)
    _parity, output_xonly = taproot_tweak_pubkey(internal_xonly)
    output_private_key = CKey(taproot_tweak_privkey(private_key.secret_bytes))
    scriptpubkey = create_p2tr_scriptpubkey(output_xonly)
    tx = _spend_tx(scriptpubkey)

    signature = sign_p2tr_input(
        tx, 0, [100_000], [scriptpubkey], output_private_key, SIGHASH_DEFAULT
    )
    sighash = compute_sighash_taproot(tx, 0, [100_000], [scriptpubkey])

    assert len(signature) == 64
    assert XOnlyPubKey(output_xonly).verify_schnorr(sighash, signature)
    assert verify_p2tr_signature(tx, 0, [100_000], [scriptpubkey], signature, output_xonly)


def test_taproot_sighash_requires_all_prevouts() -> None:
    tx = _spend_tx(create_p2tr_scriptpubkey(b"\x11" * 32))

    with pytest.raises(TransactionSigningError, match="Prevouts length"):
        compute_sighash_taproot(tx, 0, [], [])


@pytest.mark.parametrize("sighash_type", [-1, 4, 0x80, 0x84, 0xFF])
def test_taproot_sighash_rejects_invalid_hash_type(sighash_type: int) -> None:
    tx = _spend_tx(create_p2tr_scriptpubkey(b"\x11" * 32))
    with pytest.raises(TransactionSigningError, match="sighash type"):
        compute_sighash_taproot(tx, 0, [100_000], [tx.outputs[0].script], sighash_type)


def test_taproot_sighash_rejects_negative_input_and_missing_single_output() -> None:
    tx = _spend_tx(create_p2tr_scriptpubkey(b"\x11" * 32), outputs=[])
    with pytest.raises(TransactionSigningError, match="Input index"):
        compute_sighash_taproot(tx, -1, [100_000], [b"\x51"])
    with pytest.raises(TransactionSigningError, match="corresponding output"):
        compute_sighash_taproot(tx, 0, [100_000], [b"\x51"], SIGHASH_SINGLE)


def test_taproot_verification_rejects_noncanonical_signatures_and_non_ckey_signer() -> None:
    key = CKey(b"\x01" * 32)
    scriptpubkey = create_p2tr_scriptpubkey(bytes(key.xonly_pub))
    tx = _spend_tx(scriptpubkey)

    assert not verify_p2tr_signature(
        tx, 0, [100_000], [scriptpubkey], b"\x00" * 63, bytes(key.xonly_pub)
    )
    assert not verify_p2tr_signature(
        tx, 0, [100_000], [scriptpubkey], b"\x00" * 64 + b"\x00", bytes(key.xonly_pub)
    )
    with pytest.raises(TransactionSigningError, match="CKey"):
        sign_p2tr_input(tx, 0, [100_000], [scriptpubkey], b"\x01" * 32)  # type: ignore[arg-type]
