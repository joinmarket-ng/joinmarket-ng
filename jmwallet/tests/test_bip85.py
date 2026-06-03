"""Tests for BIP-85 deterministic entropy derivation."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from coincurve import PrivateKey

from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.bip85 import (
    APP_HEX,
    APP_WIF,
    BIP85_PURPOSE,
    bip85_entropy,
    derive_private_key,
    derive_symmetric_key,
)

# secp256k1 group order; valid private keys are in [1, N-1].
_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

# A fixed seed (not a real wallet) for deterministic derivation tests.
_SEED = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")


def test_hmac_entropy_step_matches_bip85_spec_vector() -> None:
    """Lock the ``HMAC-SHA512(bip-entropy-from-k)`` transform to the spec.

    BIP-85 test case ``m/83696968'/0'/0'`` publishes both the derived child
    private key ``k`` and the resulting 64 bytes of entropy. ``HDKey`` cannot
    import the spec's master xprv, so we verify the transform that ``bip85_entropy``
    applies on top of the BIP32 derivation directly against the published values.
    """
    derived_key = bytes.fromhex("cca20ccb0e9a90feb0912870c3323b24874b0ca3d8018c4b96d0b97c0e82ded0")
    expected_entropy = bytes.fromhex(
        "efecfbccffea313214232d29e71563d941229afb4338c21f9517c41aaa0d16f0"
        "0b83d2a09ef747e7a64e8e2bd5a14869e693da66ce94ac2da570ab7ee48618f7"
    )
    full = hmac.new(b"bip-entropy-from-k", derived_key, hashlib.sha512).digest()
    assert full == expected_entropy


def test_constants() -> None:
    assert BIP85_PURPOSE == 83696968
    assert APP_WIF == 2
    assert APP_HEX == 128169


def test_entropy_is_deterministic() -> None:
    master = HDKey.from_seed(_SEED)
    a = bip85_entropy(master, APP_HEX, [32, 0], 32)
    b = bip85_entropy(master, APP_HEX, [32, 0], 32)
    assert a == b
    assert len(a) == 32


def test_entropy_truncation_length() -> None:
    master = HDKey.from_seed(_SEED)
    for n in (1, 16, 32, 64):
        assert len(bip85_entropy(master, APP_HEX, [n, 0], n)) == n


@pytest.mark.parametrize("n", [0, 65, -1])
def test_entropy_rejects_out_of_range_length(n: int) -> None:
    master = HDKey.from_seed(_SEED)
    with pytest.raises(ValueError, match="num_bytes"):
        bip85_entropy(master, APP_HEX, [0], n)


def test_different_indices_yield_different_entropy() -> None:
    master = HDKey.from_seed(_SEED)
    values = {bip85_entropy(master, APP_HEX, [32, i], 32) for i in range(8)}
    assert len(values) == 8


def test_different_seeds_yield_different_entropy() -> None:
    a = bip85_entropy(HDKey.from_seed(_SEED), APP_HEX, [32, 0], 32)
    b = bip85_entropy(HDKey.from_seed(b"\xff" * 32), APP_HEX, [32, 0], 32)
    assert a != b


def test_symmetric_key_default_length() -> None:
    master = HDKey.from_seed(_SEED)
    assert len(derive_symmetric_key(master)) == 32
    assert len(derive_symmetric_key(master, index=5, num_bytes=16)) == 16


def test_derive_private_key_is_valid_scalar() -> None:
    master = HDKey.from_seed(_SEED)
    priv = derive_private_key(master, 7)
    assert len(priv) == 32
    scalar = int.from_bytes(priv, "big")
    assert 1 <= scalar < _SECP256K1_N
    # Must be usable as a secp256k1 key.
    pub = PrivateKey(priv).public_key.format(compressed=True)
    assert len(pub) == 33
