"""
Tests for NIP-04 encryption/decryption and Nostr event signing.

Tests cover:
- NIP-04 encrypt/decrypt roundtrip with random keypairs
- Shared secret symmetry (Alice->Bob == Bob->Alice)
- Invalid input handling (bad hex, missing separator, wrong key)
- Event ID computation (deterministic)
- Schnorr event signing and verification
- DM event creation with correct structure
"""

from __future__ import annotations

import json
import secrets

import pytest
from coincurve import PublicKeyXOnly

from taker.swap.nip04 import (
    compute_event_id,
    compute_shared_secret,
    create_nip04_dm_event,
    nip04_decrypt,
    nip04_encrypt,
    privkey_to_xonly_pubkey,
    serialize_event_for_id,
    sign_event,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _random_keypair() -> tuple[bytes, str]:
    """Generate a random secp256k1 keypair.

    Returns:
        (privkey_bytes, xonly_pubkey_hex)
    """
    privkey = secrets.token_bytes(32)
    pubkey = privkey_to_xonly_pubkey(privkey)
    return privkey, pubkey


# ---------------------------------------------------------------------------
# Tests: Shared Secret
# ---------------------------------------------------------------------------


class TestComputeSharedSecret:
    """Tests for ECDH shared secret computation."""

    def test_symmetry(self) -> None:
        """Alice(privA, pubB) must equal Bob(privB, pubA)."""
        priv_a, pub_a = _random_keypair()
        priv_b, pub_b = _random_keypair()

        ss_ab = compute_shared_secret(priv_a, pub_b)
        ss_ba = compute_shared_secret(priv_b, pub_a)
        assert ss_ab == ss_ba

    def test_length(self) -> None:
        """Shared secret must be exactly 32 bytes (x-coordinate)."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()

        ss = compute_shared_secret(priv_a, pub_b)
        assert len(ss) == 32

    def test_deterministic(self) -> None:
        """Same inputs produce the same shared secret."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()

        ss1 = compute_shared_secret(priv_a, pub_b)
        ss2 = compute_shared_secret(priv_a, pub_b)
        assert ss1 == ss2

    def test_different_keys_different_secret(self) -> None:
        """Different keypairs produce different secrets."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()
        _, pub_c = _random_keypair()

        ss_ab = compute_shared_secret(priv_a, pub_b)
        ss_ac = compute_shared_secret(priv_a, pub_c)
        assert ss_ab != ss_ac


# ---------------------------------------------------------------------------
# Tests: NIP-04 Encrypt/Decrypt
# ---------------------------------------------------------------------------


class TestNip04EncryptDecrypt:
    """Tests for NIP-04 encryption and decryption roundtrip."""

    def test_basic_roundtrip(self) -> None:
        """Encrypt with Alice's key, decrypt with Bob's key."""
        priv_a, pub_a = _random_keypair()
        priv_b, pub_b = _random_keypair()

        plaintext = '{"method": "createswap", "type": "reversesubmarine"}'
        encrypted = nip04_encrypt(plaintext, priv_a, pub_b)
        decrypted = nip04_decrypt(encrypted, priv_b, pub_a)
        assert decrypted == plaintext

    def test_empty_string(self) -> None:
        """Empty plaintext should roundtrip correctly."""
        priv_a, pub_a = _random_keypair()
        priv_b, pub_b = _random_keypair()

        encrypted = nip04_encrypt("", priv_a, pub_b)
        decrypted = nip04_decrypt(encrypted, priv_b, pub_a)
        assert decrypted == ""

    def test_unicode_content(self) -> None:
        """Unicode characters should survive encrypt/decrypt."""
        priv_a, pub_a = _random_keypair()
        priv_b, pub_b = _random_keypair()

        plaintext = '{"memo": "test with unicode: \u00e9\u00e0\u00fc\u2603"}'
        encrypted = nip04_encrypt(plaintext, priv_a, pub_b)
        decrypted = nip04_decrypt(encrypted, priv_b, pub_a)
        assert decrypted == plaintext

    def test_large_payload(self) -> None:
        """Large payloads should work (> 1 AES block)."""
        priv_a, pub_a = _random_keypair()
        priv_b, pub_b = _random_keypair()

        plaintext = json.dumps({"data": "x" * 10_000})
        encrypted = nip04_encrypt(plaintext, priv_a, pub_b)
        decrypted = nip04_decrypt(encrypted, priv_b, pub_a)
        assert decrypted == plaintext

    def test_wire_format(self) -> None:
        """Encrypted content must have the base64?iv=base64 format."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()

        encrypted = nip04_encrypt("hello", priv_a, pub_b)
        assert "?iv=" in encrypted

        ct_b64, iv_b64 = encrypted.split("?iv=", 1)
        # Both parts should be valid base64
        import base64

        base64.b64decode(ct_b64)
        iv_bytes = base64.b64decode(iv_b64)
        assert len(iv_bytes) == 16

    def test_different_iv_each_time(self) -> None:
        """Each encryption should use a random IV."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()

        enc1 = nip04_encrypt("same message", priv_a, pub_b)
        enc2 = nip04_encrypt("same message", priv_a, pub_b)
        # Ciphertext should differ because IV is random
        assert enc1 != enc2

    def test_wrong_key_fails(self) -> None:
        """Decrypting with the wrong key should fail."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()
        priv_c, _ = _random_keypair()

        encrypted = nip04_encrypt("secret", priv_a, pub_b)
        # priv_c cannot decrypt a message meant for pub_b
        with pytest.raises(Exception):
            nip04_decrypt(encrypted, priv_c, pub_b)

    def test_missing_iv_separator(self) -> None:
        """Content without ?iv= separator should raise ValueError."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()

        with pytest.raises(ValueError, match="missing.*iv"):
            nip04_decrypt("justbase64stuff", priv_a, pub_b)

    def test_invalid_base64(self) -> None:
        """Invalid base64 should raise ValueError."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()

        with pytest.raises(ValueError, match="(base64|IV length)"):
            nip04_decrypt("not!valid?iv=also!not!valid", priv_a, pub_b)

    def test_invalid_iv_length(self) -> None:
        """IV that is not 16 bytes should raise ValueError."""
        import base64

        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()

        ct_b64 = base64.b64encode(b"x" * 32).decode()
        iv_b64 = base64.b64encode(b"short").decode()  # Only 5 bytes
        content = f"{ct_b64}?iv={iv_b64}"

        with pytest.raises(ValueError, match="IV length"):
            nip04_decrypt(content, priv_a, pub_b)


# ---------------------------------------------------------------------------
# Tests: x-only pubkey derivation
# ---------------------------------------------------------------------------


class TestPrivkeyToXonlyPubkey:
    """Tests for private key to x-only public key derivation."""

    def test_length(self) -> None:
        """X-only pubkey should be 64 hex chars (32 bytes)."""
        privkey = secrets.token_bytes(32)
        pubkey = privkey_to_xonly_pubkey(privkey)
        assert len(pubkey) == 64

    def test_hex_format(self) -> None:
        """Result should be valid hex."""
        privkey = secrets.token_bytes(32)
        pubkey = privkey_to_xonly_pubkey(privkey)
        bytes.fromhex(pubkey)  # Should not raise

    def test_deterministic(self) -> None:
        """Same privkey always produces the same pubkey."""
        privkey = secrets.token_bytes(32)
        pub1 = privkey_to_xonly_pubkey(privkey)
        pub2 = privkey_to_xonly_pubkey(privkey)
        assert pub1 == pub2


# ---------------------------------------------------------------------------
# Tests: Event ID and Signing
# ---------------------------------------------------------------------------


class TestEventId:
    """Tests for Nostr event ID computation."""

    def test_deterministic(self) -> None:
        """Same event data produces the same ID."""
        event = {
            "pubkey": "a" * 64,
            "created_at": 1700000000,
            "kind": 25582,
            "tags": [["p", "b" * 64]],
            "content": "encrypted stuff",
        }
        id1 = compute_event_id(event)
        id2 = compute_event_id(event)
        assert id1 == id2

    def test_correct_format(self) -> None:
        """Event ID should be 64 hex chars (SHA256)."""
        event = {
            "pubkey": "a" * 64,
            "created_at": 1700000000,
            "kind": 25582,
            "tags": [],
            "content": "test",
        }
        event_id = compute_event_id(event)
        assert len(event_id) == 64
        bytes.fromhex(event_id)  # Should not raise

    def test_serialization_format(self) -> None:
        """Serialization must match NIP-01 spec: [0, pubkey, created_at, kind, tags, content]."""
        event = {
            "pubkey": "aa" * 32,
            "created_at": 1234567890,
            "kind": 1,
            "tags": [["p", "bb" * 32]],
            "content": "hello",
        }
        serialized = serialize_event_for_id(event)
        parsed = json.loads(serialized)
        assert parsed[0] == 0
        assert parsed[1] == "aa" * 32
        assert parsed[2] == 1234567890
        assert parsed[3] == 1
        assert parsed[4] == [["p", "bb" * 32]]
        assert parsed[5] == "hello"


class TestSignEvent:
    """Tests for Schnorr event signing."""

    def test_sign_produces_valid_schnorr(self) -> None:
        """Signed event should have a valid BIP-340 Schnorr signature."""
        privkey, pubkey_hex = _random_keypair()

        event = {
            "pubkey": pubkey_hex,
            "created_at": 1700000000,
            "kind": 25582,
            "tags": [["p", "b" * 64]],
            "content": "encrypted content",
        }

        signed = sign_event(event, privkey)

        assert "id" in signed
        assert "sig" in signed
        assert len(signed["sig"]) == 128  # 64-byte Schnorr sig = 128 hex

        # Verify with coincurve's PublicKeyXOnly
        xonly_pk = PublicKeyXOnly(bytes.fromhex(pubkey_hex))
        id_bytes = bytes.fromhex(signed["id"])
        sig_bytes = bytes.fromhex(signed["sig"])
        assert xonly_pk.verify(sig_bytes, id_bytes)

    def test_sign_sets_id_field(self) -> None:
        """sign_event must set the 'id' field on the event dict."""
        privkey, pubkey_hex = _random_keypair()
        event = {
            "pubkey": pubkey_hex,
            "created_at": 1700000000,
            "kind": 1,
            "tags": [],
            "content": "",
        }
        signed = sign_event(event, privkey)
        assert signed["id"] == compute_event_id(event)

    def test_wrong_pubkey_fails_verification(self) -> None:
        """Signature should not verify against a different public key."""
        priv_a, pub_a = _random_keypair()
        _, pub_b = _random_keypair()

        event = {
            "pubkey": pub_a,
            "created_at": 1700000000,
            "kind": 1,
            "tags": [],
            "content": "test",
        }
        signed = sign_event(event, priv_a)

        # Verify against wrong pubkey should fail
        wrong_pk = PublicKeyXOnly(bytes.fromhex(pub_b))
        id_bytes = bytes.fromhex(signed["id"])
        sig_bytes = bytes.fromhex(signed["sig"])
        assert not wrong_pk.verify(sig_bytes, id_bytes)


# ---------------------------------------------------------------------------
# Tests: create_nip04_dm_event
# ---------------------------------------------------------------------------


class TestCreateNip04DmEvent:
    """Tests for the combined DM event creation function."""

    def test_event_structure(self) -> None:
        """Created event should have all required Nostr fields."""
        priv_a, pub_a = _random_keypair()
        _, pub_b = _random_keypair()

        event = create_nip04_dm_event("hello", priv_a, pub_b)

        assert event["pubkey"] == pub_a
        assert event["kind"] == 25582
        assert isinstance(event["created_at"], int)
        assert event["tags"] == [["p", pub_b]]
        assert "?iv=" in event["content"]  # NIP-04 encrypted
        assert "id" in event
        assert "sig" in event

    def test_content_is_decryptable(self) -> None:
        """The encrypted content should be decryptable by the recipient."""
        priv_a, pub_a = _random_keypair()
        priv_b, pub_b = _random_keypair()

        original = '{"method": "createswap"}'
        event = create_nip04_dm_event(original, priv_a, pub_b)

        decrypted = nip04_decrypt(event["content"], priv_b, pub_a)
        assert decrypted == original

    def test_custom_kind(self) -> None:
        """Should allow custom event kinds."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()

        event = create_nip04_dm_event("test", priv_a, pub_b, kind=4)
        assert event["kind"] == 4

    def test_signature_is_valid(self) -> None:
        """The event signature should be cryptographically valid."""
        priv_a, pub_a = _random_keypair()
        _, pub_b = _random_keypair()

        event = create_nip04_dm_event("test", priv_a, pub_b)

        xonly_pk = PublicKeyXOnly(bytes.fromhex(pub_a))
        id_bytes = bytes.fromhex(event["id"])
        sig_bytes = bytes.fromhex(event["sig"])
        assert xonly_pk.verify(sig_bytes, id_bytes)

    def test_event_id_matches_content(self) -> None:
        """Event ID should be SHA256 of the canonical serialization."""
        priv_a, _ = _random_keypair()
        _, pub_b = _random_keypair()

        event = create_nip04_dm_event("hello", priv_a, pub_b)
        recomputed = compute_event_id(event)
        assert event["id"] == recomputed
