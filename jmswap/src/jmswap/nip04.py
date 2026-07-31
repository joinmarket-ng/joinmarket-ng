from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any

from coincurve import PrivateKey, PublicKey, PublicKeyXOnly
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7


def privkey_to_xonly_pubkey(privkey: bytes) -> str:
    return PrivateKey(privkey).public_key.format(compressed=True)[1:].hex()


def _shared_secret(privkey: bytes, pubkey_hex: str) -> bytes:
    return PublicKey(bytes.fromhex("02" + pubkey_hex)).multiply(privkey).format(compressed=True)[1:]


def nip04_encrypt(plaintext: str, privkey: bytes, pubkey_hex: str) -> str:
    secret = _shared_secret(privkey, pubkey_hex)
    iv = os.urandom(16)
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext.encode()) + padder.finalize()
    encryptor = Cipher(algorithms.AES(secret), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return f"{base64.b64encode(ciphertext).decode()}?iv={base64.b64encode(iv).decode()}"


def nip04_decrypt(ciphertext: str, privkey: bytes, pubkey_hex: str) -> str:
    if "?iv=" not in ciphertext:
        raise ValueError("invalid NIP-04 ciphertext")
    payload, encoded_iv = ciphertext.split("?iv=", 1)
    encrypted = base64.b64decode(payload, validate=True)
    iv = base64.b64decode(encoded_iv, validate=True)
    if len(iv) != 16 or not encrypted or len(encrypted) % 16:
        raise ValueError("invalid NIP-04 ciphertext shape")
    decryptor = Cipher(
        algorithms.AES(_shared_secret(privkey, pubkey_hex)), modes.CBC(iv)
    ).decryptor()
    padded = decryptor.update(encrypted) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode()


def event_id(event: dict[str, Any]) -> str:
    encoded = json.dumps(
        [0, event["pubkey"], event["created_at"], event["kind"], event["tags"], event["content"]],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def sign_event(event: dict[str, Any], privkey: bytes) -> dict[str, Any]:
    identifier = event_id(event)
    event["id"] = identifier
    event["sig"] = PrivateKey(privkey).sign_schnorr(bytes.fromhex(identifier)).hex()
    return event


def verify_event(event: dict[str, Any]) -> bool:
    try:
        identifier = event_id(event)
        return identifier == event.get("id") and PublicKeyXOnly(
            bytes.fromhex(event["pubkey"])
        ).verify(bytes.fromhex(event["sig"]), bytes.fromhex(identifier))
    except Exception:  # noqa: BLE001
        return False


def create_event(content: str, privkey: bytes, kind: int, tags: list[list[str]]) -> dict[str, Any]:
    return sign_event(
        {
            "pubkey": privkey_to_xonly_pubkey(privkey),
            "created_at": int(time.time()),
            "kind": kind,
            "tags": tags,
            "content": content,
        },
        privkey,
    )


def create_dm(content: str, privkey: bytes, recipient: str, *, kind: int) -> dict[str, Any]:
    return create_event(
        nip04_encrypt(content, privkey, recipient), privkey, kind, [["p", recipient]]
    )
