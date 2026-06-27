"""
NIP-04 encryption/decryption and Nostr event signing (NIP-01).

Encryption: ECDH shared secret (secp256k1 x-coordinate) + AES-256-CBC.
Signing: BIP-340 Schnorr over SHA256 of the canonical event serialization.

References:
    - NIP-01: https://github.com/nostr-protocol/nips/blob/master/01.md
    - NIP-04: https://github.com/nostr-protocol/nips/blob/master/04.md
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
    """Compute the NIP-04 ECDH shared secret (raw x-coordinate of the point)."""
    their_pk = PublicKey(bytes.fromhex("02" + their_pubkey_hex))
    our_sk = PrivateKey(our_privkey)
    shared_point = their_pk.multiply(our_sk.secret)
    return shared_point.format(compressed=True)[1:]


def nip04_encrypt(plaintext: str, our_privkey: bytes, their_pubkey_hex: str) -> str:
    """Encrypt a message using NIP-04, returning ``base64(ct)?iv=base64(iv)``."""
    shared_secret = compute_shared_secret(our_privkey, their_pubkey_hex)
    iv = os.urandom(16)
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(shared_secret), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    ct_b64 = base64.b64encode(ciphertext).decode("ascii")
    iv_b64 = base64.b64encode(iv).decode("ascii")
    return f"{ct_b64}?iv={iv_b64}"


def nip04_decrypt(encrypted_content: str, our_privkey: bytes, their_pubkey_hex: str) -> str:
    """Decrypt a NIP-04 encrypted message."""
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
    """Derive the x-only (Schnorr) Nostr public key hex from a private key."""
    return PrivateKey(privkey).public_key.format(compressed=True)[1:].hex()


def serialize_event_for_id(event: dict[str, Any]) -> bytes:
    """Serialize an event for NIP-01 ID computation."""
    serialized = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return serialized.encode("utf-8")


def compute_event_id(event: dict[str, Any]) -> str:
    """Compute the NIP-01 event id (SHA256 of the serialized event)."""
    return hashlib.sha256(serialize_event_for_id(event)).hexdigest()


def sign_event(event: dict[str, Any], privkey: bytes) -> dict[str, Any]:
    """Sign a Nostr event in place, adding ``id`` and ``sig`` (BIP-340)."""
    event_id = compute_event_id(event)
    event["id"] = event_id
    event["sig"] = PrivateKey(privkey).sign_schnorr(bytes.fromhex(event_id)).hex()
    return event


def verify_event(event: dict[str, Any]) -> bool:
    """Verify a Nostr event's id and Schnorr signature."""
    from coincurve import PublicKeyXOnly

    try:
        if compute_event_id(event) != event.get("id"):
            return False
        return bool(
            PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(
                bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])
            )
        )
    except Exception:  # noqa: BLE001 - verification must never raise
        return False


def create_nip04_dm_event(
    content: str, our_privkey: bytes, recipient_pubkey_hex: str, kind: int = 25582
) -> dict[str, Any]:
    """Create a signed NIP-04 encrypted DM event addressed to a recipient."""
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


def create_event(
    content: str, our_privkey: bytes, kind: int, tags: list[list[str]] | None = None
) -> dict[str, Any]:
    """Create a signed plaintext (public) Nostr event."""
    event: dict[str, Any] = {
        "pubkey": privkey_to_xonly_pubkey(our_privkey),
        "created_at": int(time.time()),
        "kind": kind,
        "tags": tags or [],
        "content": content,
    }
    return sign_event(event, our_privkey)
