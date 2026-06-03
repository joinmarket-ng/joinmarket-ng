"""Tests for the wallet-owned swap key authority (WalletSwapKeysMixin)."""

from __future__ import annotations

import hashlib

import pytest
from coincurve import PublicKey
from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    parse_transaction,
    script_to_p2wsh_scriptpubkey,
    serialize_transaction,
)

# Reuse the swap HTLC script builder the taker uses, so the witness we build
# here is exactly what would be spent on-chain.
from taker.swap.script import SwapScript

from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.signing import compute_sighash_segwit
from jmwallet.wallet.swap_keys import SwapKeyMaterial, WalletSwapKeysMixin

_SEED_A = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
_SEED_B = bytes.fromhex("ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff")


class _Provider(WalletSwapKeysMixin):
    """Minimal stand-in exposing only the master key the mixin needs."""

    def __init__(self, seed: bytes) -> None:
        self.master_key = HDKey.from_seed(seed)


def test_create_swap_key_material_index_in_range() -> None:
    provider = _Provider(_SEED_A)
    for _ in range(20):
        material = provider.create_swap_key_material()
        assert 1 <= material.index <= (1 << 31) - 1


def test_material_is_deterministic_for_same_seed_and_index() -> None:
    a = _Provider(_SEED_A).derive_swap_key_material(42)
    b = _Provider(_SEED_A).derive_swap_key_material(42)
    assert isinstance(a, SwapKeyMaterial)
    assert a.preimage == b.preimage
    assert a.preimage_hash == b.preimage_hash
    assert a.claim_pubkey == b.claim_pubkey
    assert a.preimage_hash == hashlib.sha256(a.preimage).digest()


def test_material_differs_across_seeds() -> None:
    a = _Provider(_SEED_A).derive_swap_key_material(42)
    b = _Provider(_SEED_B).derive_swap_key_material(42)
    assert a.preimage != b.preimage
    assert a.claim_pubkey != b.claim_pubkey


def test_material_differs_across_indices() -> None:
    provider = _Provider(_SEED_A)
    preimages = {provider.derive_swap_key_material(i).preimage for i in range(1, 9)}
    assert len(preimages) == 8


@pytest.mark.parametrize("index", [0, -1, 1 << 31])
def test_derive_rejects_out_of_range_index(index: int) -> None:
    with pytest.raises(ValueError, match="swap index"):
        _Provider(_SEED_A).derive_swap_key_material(index)


def test_storage_key_is_stable_32_bytes() -> None:
    provider = _Provider(_SEED_A)
    key1 = provider.derive_swap_storage_key()
    key2 = provider.derive_swap_storage_key()
    assert len(key1) == 32
    assert key1 == key2
    assert key1 != _Provider(_SEED_B).derive_swap_storage_key()


def test_storage_key_disjoint_from_swap_preimages() -> None:
    provider = _Provider(_SEED_A)
    storage = provider.derive_swap_storage_key()
    # The first swap index must not collide with the storage-key derivation.
    assert storage != provider.derive_swap_key_material(1).preimage


def _build_unsigned_claim_tx(
    witness_script: bytes, value: int
) -> tuple[ParsedTransaction, TxInput]:
    scriptpubkey = script_to_p2wsh_scriptpubkey(witness_script).hex()
    tx_input = TxInput.from_hex(
        "aa" * 32, 0, sequence=0xFFFFFFFF, value=value, scriptpubkey=scriptpubkey
    )
    tx_output = TxOutput.from_address("bcrt1qw508d6qejxtdg4y5r3zarvary0c5xw7kygt080", value - 500)
    unsigned_hex = serialize_transaction(2, [tx_input], [tx_output], 0).hex()
    return parse_transaction(unsigned_hex), tx_input


def test_build_swap_claim_witness_produces_valid_signature() -> None:
    provider = _Provider(_SEED_A)
    index = 1234
    material = provider.derive_swap_key_material(index)

    # Build the real HTLC script with the wallet-derived claim pubkey.
    refund_priv = HDKey.from_seed(b"\x01" * 32).get_private_key_bytes()
    refund_pub = PublicKey.from_valid_secret(refund_priv).format(compressed=True)
    script = SwapScript(
        preimage_hash=material.preimage_hash,
        claim_pubkey=material.claim_pubkey,
        refund_pubkey=refund_pub,
        timeout_blockheight=800_100,
    )
    witness_script = script.witness_script()
    value = 50_000

    parsed, _ = _build_unsigned_claim_tx(witness_script, value)
    witness = provider.build_swap_claim_witness(parsed, 0, witness_script, value, index)

    # Witness stack must be [signature, preimage, witness_script].
    assert len(witness) == 3
    signature, preimage, ws = witness
    assert preimage == material.preimage
    assert ws == witness_script
    assert signature[-1] == 0x01  # SIGHASH_ALL
    assert signature[0] == 0x30  # DER sequence marker

    # The signature must verify against the derived claim public key over the
    # BIP-143 sighash, proving the wallet signed with the matching private key.
    sighash = compute_sighash_segwit(parsed, 0, witness_script, value, 0x01)
    pub = PublicKey(material.claim_pubkey)
    assert pub.verify(signature[:-1], sighash, hasher=None)
