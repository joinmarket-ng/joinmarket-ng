"""
BIP-85 deterministic entropy derivation (Deterministic Entropy From BIP32
Keychains).

BIP-85 lets a single BIP32 master key act as the root of arbitrary
application secrets. For each application a unique child private key ``k`` is
derived using a fully hardened path ``m/83696968'/{app_no}'/{index...}'`` and
then run through ``HMAC-SHA512(key="bip-entropy-from-k", msg=k)`` to produce 64
bytes of entropy, truncated to the number of bytes the application needs.

We use this to derive, from the wallet seed alone and with no extra secret to
back up:

* a symmetric key for encrypting swap-recovery records (HEX app, 128169'),
* per-swap HTLC claim private keys (XPRV-WIF app, 2'),
* per-swap HTLC preimages (HEX app, 128169', in a disjoint index range).

Reference: https://github.com/bitcoin/bips/blob/master/bip-0085.mediawiki
"""

from __future__ import annotations

import hashlib
import hmac

from jmwallet.wallet.bip32 import HDKey

# BIP-85 fixed purpose node: ASCII "8569" style magic from the spec.
BIP85_PURPOSE = 83696968

# Application numbers (see BIP-85 "Applications").
APP_WIF = 2  # HD-Seed WIF: 256-bit secret exponent -> private key
APP_HEX = 128169  # Raw hex entropy of a chosen byte length

_HMAC_KEY = b"bip-entropy-from-k"


def bip85_entropy(master: HDKey, app_no: int, indices: list[int], num_bytes: int = 64) -> bytes:
    """Derive BIP-85 entropy from a master key.

    Args:
        master: The BIP32 master (root) key.
        app_no: BIP-85 application number.
        indices: Remaining hardened path indices after the application number.
        num_bytes: Number of leading entropy bytes to return (1..64).

    Returns:
        ``num_bytes`` bytes of application entropy.
    """
    if not 1 <= num_bytes <= 64:
        raise ValueError(f"num_bytes must be in 1..64, got {num_bytes}")
    path_parts = [str(BIP85_PURPOSE), str(app_no), *(str(i) for i in indices)]
    path = "m/" + "/".join(f"{p}'" for p in path_parts)
    child = master.derive(path)
    k = child.get_private_key_bytes()
    full = hmac.new(_HMAC_KEY, k, hashlib.sha512).digest()
    return full[:num_bytes]


def derive_symmetric_key(master: HDKey, index: int = 0, num_bytes: int = 32) -> bytes:
    """Derive a raw symmetric key via the BIP-85 HEX application.

    Path: ``m/83696968'/128169'/{num_bytes}'/{index}'``.
    """
    return bip85_entropy(master, APP_HEX, [num_bytes, index], num_bytes)


def derive_private_key(master: HDKey, index: int) -> bytes:
    """Derive a 32-byte secp256k1 private key via the BIP-85 WIF application.

    Path: ``m/83696968'/2'/{index}'``. The most-significant 256 bits of the
    entropy are the secret exponent.
    """
    return bip85_entropy(master, APP_WIF, [index], 32)
