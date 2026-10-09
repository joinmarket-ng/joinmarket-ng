"""Wallet-derived authorizer for ChannelEscrow partial signatures.

The patched LND node cosigns an escrow parent only with a BIP340 signature, by
the key enrolled for the channel, over :func:`authorization_digest`. The digest
encoding is a cross-language contract with
``lnd/patches/0003-feat-channelescrowrpc-require-an-enrolled-authorizer.patch``
(``authorizationDigest``); both sides pin the same test vector.

The key is derived from the wallet's binary BIP39 seed with SLIP-0021, outside
the BIP32 spending tree, like the credential market keys. One key serves every
channel of the wallet on a network: it is only ever sent to the user's own LND.
This is an in-process capability, not guaranteed memory zeroization.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import struct
from collections.abc import Sequence
from threading import RLock

from bitcointx.core.key import CKey, XOnlyPubKey

from jmcore.constants import SECP256K1_N
from jmcore.crypto import mnemonic_to_seed

ESCROW_AUTHORIZATION_POLICY = 1
_TAG = b"JoinMarketNG/channel-escrow-auth/v1"
_APPLICATION = b"JoinMarket NG channel escrow authorizer"


class EscrowAuthorizerError(Exception):
    """An escrow authorization cannot be produced safely."""


def _slip21(node: bytes, label: bytes) -> bytes:
    return hmac.digest(node[:32], b"\x00" + label, "sha512")


def _tagged_hash(tag: bytes, message: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag).digest()
    return hashlib.sha256(tag_hash + tag_hash + message).digest()


def authorization_digest(
    *,
    chain_hash: bytes,
    node_pubkey: bytes,
    funding_txid: bytes,
    output_index: int,
    session_id: bytes,
    parent_tx: bytes,
    prevouts: Sequence[tuple[int, bytes]],
    channel_input_index: int,
    policy: int = ESCROW_AUTHORIZATION_POLICY,
) -> bytes:
    """Return the tagged digest LND verifies before releasing a partial.

    ``chain_hash`` (genesis) and ``funding_txid`` are in internal byte order,
    as ``ChannelPoint.funding_txid`` is sent. ``prevouts`` are the parent's
    ordered spent outputs as ``(value_sat, pk_script)``.
    """
    for name, value, size in (
        ("chain_hash", chain_hash, 32),
        ("node_pubkey", node_pubkey, 33),
        ("funding_txid", funding_txid, 32),
        ("session_id", session_id, 32),
    ):
        if not isinstance(value, bytes) or len(value) != size:
            raise EscrowAuthorizerError(f"{name} must be {size} bytes")
    if not parent_tx or not prevouts:
        raise EscrowAuthorizerError("an exact prepared parent is required")
    try:
        message = bytearray(struct.pack("<I", policy))
        message += chain_hash + node_pubkey + funding_txid
        message += struct.pack("<I", output_index)
        message += session_id + hashlib.sha256(parent_tx).digest()
        message += struct.pack("<I", len(prevouts))
        for amount, script in prevouts:
            message += struct.pack("<qI", amount, len(script)) + bytes(script)
        message += struct.pack("<I", channel_input_index)
    except struct.error as exc:
        raise EscrowAuthorizerError("authorization field out of range") from exc
    return _tagged_hash(_TAG, bytes(message))


class EscrowAuthorizer:
    """Signs escrow authorizations without exporting the private key."""

    __slots__ = ("_key", "_lock", "public_key")

    def __init__(self, seed: bytes, network: str) -> None:
        if not isinstance(seed, bytes) or len(seed) != 64:
            raise ValueError("The escrow authorizer requires a 64-byte binary BIP39 seed")
        node = hmac.digest(b"Symmetric key seed", seed, "sha512")
        for label in (_APPLICATION, b"v1", b"network", network.encode("ascii"), b"bip340"):
            node = _slip21(node, label)
        # Rejection sampling preserves scalar uniformity; never reduce modulo n.
        for counter in range(256):
            secret = _slip21(node, str(counter).encode("ascii"))[32:]
            if 0 < int.from_bytes(secret, "big") < SECP256K1_N:
                self._key: CKey | None = CKey.from_secret_bytes(secret, compressed=True)
                break
        else:  # pragma: no cover - probability below 2**-256
            raise EscrowAuthorizerError("Could not derive a valid authorizer scalar")
        self.public_key = bytes(XOnlyPubKey(self._key.pub))
        self._lock = RLock()

    @classmethod
    def from_mnemonic(cls, mnemonic: str, passphrase: str, network: str) -> EscrowAuthorizer:
        return cls(mnemonic_to_seed(mnemonic, passphrase), network)

    def close(self) -> None:
        """Revoke future signatures, including through retained references."""
        with self._lock:
            self._key = None

    def sign(self, digest: bytes) -> bytes:
        """Return a 64-byte BIP340 signature over an authorization digest."""
        if not isinstance(digest, bytes) or len(digest) != 32:
            raise EscrowAuthorizerError("authorization digest must be 32 bytes")
        with self._lock:
            if self._key is None:
                raise EscrowAuthorizerError("The escrow authorizer is closed")
            return bytes(self._key.sign_schnorr_no_tweak(digest, aux=secrets.token_bytes(32)))
