"""
NIP-04 encryption/decryption and Nostr event signing.

Implements the NIP-04 encrypted direct message protocol used by the
Electrum swap server for RPC communication over Nostr (kind 25582).

Encryption: ECDH shared secret (secp256k1) + AES-256-CBC.
Signing: Schnorr signatures on SHA256(serialized event).

Wire format for encrypted content:
    base64(ciphertext) + "?iv=" + base64(iv)

References:
- NIP-04: https://github.com/nostr-protocol/nips/blob/master/04.md
- Electrum swap server: uses kind 25582 (ephemeral range) for DMs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any

from coincurve import PrivateKey, PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


def compute_shared_secret(our_privkey: bytes, their_pubkey_hex: str) -> bytes:
    """Compute the NIP-04 ECDH shared secret.

    NIP-04 uses the raw x-coordinate of the ECDH point as the AES key.
    The peer's pubkey is an x-only (32-byte) Nostr pubkey — we prepend 0x02
    to form a valid compressed public key for ECDH.

    Args:
        our_privkey: Our 32-byte private key.
        their_pubkey_hex: Peer's x-only public key (64-char hex).

    Returns:
        32-byte shared secret (x-coordinate of ECDH point).
    """
    # Nostr pubkeys are x-only (32 bytes). Prepend 0x02 to get compressed form.
    compressed_pubkey = bytes.fromhex("02" + their_pubkey_hex)
    their_pk = PublicKey(compressed_pubkey)

    our_sk = PrivateKey(our_privkey)

    # coincurve's ecdh() returns SHA256(compressed_point) by default.
    # NIP-04 wants the raw x-coordinate (first 32 bytes of the uncompressed point).
    # We use multiply() to get the raw point.
    shared_point = their_pk.multiply(our_sk.secret)
    # The shared point is a compressed public key (33 bytes: prefix + x).
    # Extract just the x-coordinate (bytes 1..33).
    x_coord = shared_point.format(compressed=True)[1:]
    return x_coord


def nip04_encrypt(plaintext: str, our_privkey: bytes, their_pubkey_hex: str) -> str:
    """Encrypt a message using NIP-04.

    Args:
        plaintext: Message to encrypt (UTF-8 string).
        our_privkey: Our 32-byte private key.
        their_pubkey_hex: Recipient's x-only public key (64-char hex).

    Returns:
        Encrypted content in NIP-04 wire format: "base64(ciphertext)?iv=base64(iv)".
    """
    shared_secret = compute_shared_secret(our_privkey, their_pubkey_hex)
    iv = os.urandom(16)

    # AES-256-CBC with PKCS7 padding
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()

    cipher = Cipher(algorithms.AES(shared_secret), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    ct_b64 = base64.b64encode(ciphertext).decode("ascii")
    iv_b64 = base64.b64encode(iv).decode("ascii")
    return f"{ct_b64}?iv={iv_b64}"


def nip04_decrypt(encrypted_content: str, our_privkey: bytes, their_pubkey_hex: str) -> str:
    """Decrypt a NIP-04 encrypted message.

    Args:
        encrypted_content: Encrypted content in NIP-04 wire format.
        our_privkey: Our 32-byte private key.
        their_pubkey_hex: Sender's x-only public key (64-char hex).

    Returns:
        Decrypted plaintext string.

    Raises:
        ValueError: If decryption fails or content format is invalid.
    """
    if "?iv=" not in encrypted_content:
        raise ValueError("Invalid NIP-04 content: missing '?iv=' separator")

    ct_b64, iv_b64 = encrypted_content.split("?iv=", 1)

    try:
        ciphertext = base64.b64decode(ct_b64)
        iv = base64.b64decode(iv_b64)
    except Exception as e:
        raise ValueError(f"Invalid NIP-04 base64 encoding: {e}") from e

    if len(iv) != 16:
        raise ValueError(f"Invalid NIP-04 IV length: {len(iv)} (expected 16)")

    shared_secret = compute_shared_secret(our_privkey, their_pubkey_hex)

    cipher = Cipher(algorithms.AES(shared_secret), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = PKCS7(128).unpadder()
    plaintext = unpadder.update(padded) + unpadder.finalize()
    return plaintext.decode("utf-8")


def privkey_to_xonly_pubkey(privkey: bytes) -> str:
    """Derive the x-only Nostr public key from a private key.

    Nostr uses x-only (Schnorr) public keys — just the 32-byte x-coordinate.

    Args:
        privkey: 32-byte secp256k1 private key.

    Returns:
        64-char hex x-only public key.
    """
    pk = PrivateKey(privkey)
    # Compressed pubkey is 33 bytes: prefix (02 or 03) + x-coordinate.
    compressed = pk.public_key.format(compressed=True)
    return compressed[1:].hex()


def serialize_event_for_id(event: dict[str, Any]) -> bytes:
    """Serialize a Nostr event for ID computation (NIP-01).

    The event ID is SHA256 of the JSON serialization:
        [0, pubkey, created_at, kind, tags, content]

    Args:
        event: Event dict with pubkey, created_at, kind, tags, content.

    Returns:
        UTF-8 encoded JSON bytes for hashing.
    """
    serialized = json.dumps(
        [
            0,
            event["pubkey"],
            event["created_at"],
            event["kind"],
            event["tags"],
            event["content"],
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return serialized.encode("utf-8")


def compute_event_id(event: dict[str, Any]) -> str:
    """Compute the event ID (SHA256 of serialized event).

    Args:
        event: Unsigned event dict.

    Returns:
        64-char hex event ID.
    """
    serialized = serialize_event_for_id(event)
    return hashlib.sha256(serialized).hexdigest()


def sign_event(event: dict[str, Any], privkey: bytes) -> dict[str, Any]:
    """Sign a Nostr event with a Schnorr signature.

    Computes the event ID and signs it using BIP-340 Schnorr.
    The private key may need to be negated if the corresponding public key
    has an odd y-coordinate (BIP-340 requirement).

    Args:
        event: Unsigned event dict (must have pubkey, created_at, kind, tags, content).
        privkey: 32-byte private key corresponding to event["pubkey"].

    Returns:
        Signed event dict with "id" and "sig" fields added.
    """
    event_id = compute_event_id(event)
    event["id"] = event_id

    id_bytes = bytes.fromhex(event_id)

    pk = PrivateKey(privkey)
    # coincurve's sign_schnorr signs using BIP-340 (x-only pubkey convention).
    # It handles the y-coordinate parity internally.
    sig = pk.sign_schnorr(id_bytes)
    event["sig"] = sig.hex()

    return event


def create_nip04_dm_event(
    content: str,
    our_privkey: bytes,
    recipient_pubkey_hex: str,
    kind: int = 25582,
) -> dict[str, Any]:
    """Create a signed, encrypted NIP-04 DM event.

    Args:
        content: Plaintext message to encrypt and send.
        our_privkey: Our 32-byte ephemeral private key.
        recipient_pubkey_hex: Recipient's x-only public key (64-char hex).
        kind: Nostr event kind (25582 for swap DMs).

    Returns:
        Signed event dict ready for relay submission.
    """
    our_pubkey = privkey_to_xonly_pubkey(our_privkey)
    encrypted = nip04_encrypt(content, our_privkey, recipient_pubkey_hex)

    event: dict[str, Any] = {
        "pubkey": our_pubkey,
        "created_at": int(time.time()),
        "kind": kind,
        "tags": [["p", recipient_pubkey_hex]],
        "content": encrypted,
    }

    return sign_event(event, our_privkey)
