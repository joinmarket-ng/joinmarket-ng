"""
Tests for jmcore.crypto
"""

import base64
import hashlib
import struct
from typing import Any

from coincurve import PrivateKey

from jmcore.crypto import (
    KeyPair,
    NickIdentity,
    base58_encode,
    base58check_encode,
    bitcoin_message_hash,
    bitcoin_message_hash_bytes,
    ecdsa_sign,
    ecdsa_verify,
    generate_jm_nick,
    get_ascii_cert_msg,
    get_cert_msg,
    mnemonic_to_seed,
    strip_signature_padding,
    verify_bitcoin_message_signature,
    verify_fidelity_bond_proof,
    verify_raw_ecdsa,
    verify_signature,
)


def test_base58_encode():
    # Simple test case
    # "hello" in hex is 68656c6c6f
    # 0x68656c6c6f = 448378203247
    # 448378203247 in base58 is Cn8eVZg
    assert base58_encode(b"hello") == "Cn8eVZg"

    # Empty bytes -> ""
    assert base58_encode(b"") == ""

    # Null bytes
    assert base58_encode(b"\x00") == "1"
    assert base58_encode(b"\x00\x00") == "11"


def test_generate_jm_nick():
    nick = generate_jm_nick()
    # v5 nicks for reference implementation compatibility
    assert nick.startswith("J5")
    # Check general structure if possible, but it's hash based


def test_ecdsa_sign_verify():
    """Test ECDSA signing and verification used for BTC signature in !ioauth."""
    priv_key = PrivateKey()
    priv_key_bytes = priv_key.secret
    pub_key_bytes = priv_key.public_key.format(compressed=True)

    # Sign a hex string message (as used in maker's !ioauth)
    message = "0123456789abcdef" * 4  # 64-char hex string (like NaCl pubkey)
    sig_b64 = ecdsa_sign(message, priv_key_bytes)

    # Verify with same message and pubkey
    assert ecdsa_verify(message, sig_b64, pub_key_bytes)

    # Verify fails with wrong message
    assert not ecdsa_verify("wrong message", sig_b64, pub_key_bytes)

    # Verify fails with wrong pubkey
    wrong_key = PrivateKey().public_key.format(compressed=True)
    assert not ecdsa_verify(message, sig_b64, wrong_key)


def test_ecdsa_verify_invalid_signature():
    """Test that ecdsa_verify handles invalid signatures gracefully."""
    priv_key = PrivateKey()
    pub_key_bytes = priv_key.public_key.format(compressed=True)

    # Invalid base64
    assert not ecdsa_verify("test", "not-valid-base64!!!", pub_key_bytes)

    # Valid base64 but invalid signature
    import base64

    invalid_sig = base64.b64encode(b"x" * 64).decode()
    assert not ecdsa_verify("test", invalid_sig, pub_key_bytes)


def test_keypair_signing():
    kp = KeyPair()
    msg = b"hello world"
    sig = kp.sign(msg)

    assert kp.verify(msg, sig)
    assert not kp.verify(b"other msg", sig)

    # Verify with another key
    kp2 = KeyPair()
    assert not kp2.verify(msg, sig)


def test_verify_signature_utility():
    kp = KeyPair()
    msg = b"test message"
    sig = kp.sign(msg)
    pub_hex = kp.public_key_hex()

    assert verify_signature(pub_hex, msg, sig)
    assert not verify_signature(pub_hex, b"wrong", sig)

    # Invalid pubkey
    assert not verify_signature("invalidhex", msg, sig)


def test_verify_raw_ecdsa():
    """Test raw ECDSA verification with pre-hashed message."""
    priv_key = PrivateKey()
    pub_key_bytes = priv_key.public_key.format(compressed=True)

    # Create a message hash
    message = b"test message for raw ecdsa"
    msg_hash = hashlib.sha256(message).digest()

    # Sign without additional hashing
    sig = priv_key.sign(msg_hash, hasher=None)

    # Verify should succeed
    assert verify_raw_ecdsa(msg_hash, sig, pub_key_bytes)

    # Different message should fail
    wrong_hash = hashlib.sha256(b"wrong message").digest()
    assert not verify_raw_ecdsa(wrong_hash, sig, pub_key_bytes)


def test_strip_signature_padding():
    """Test stripping leading 0xff padding from DER signatures."""
    # A valid DER signature starts with 0x30
    der_sig = b"\x30\x45\x02\x21\x00" + b"r" * 32 + b"\x02\x20" + b"s" * 32

    # Padding with 0xff (reference impl uses rjust)
    padded = b"\xff\xff" + der_sig
    assert strip_signature_padding(padded) == der_sig

    # No padding
    assert strip_signature_padding(der_sig) == der_sig

    # Full 72-byte padding (reference implementation format)
    full_padded = der_sig.rjust(72, b"\xff")
    assert strip_signature_padding(full_padded) == der_sig


def test_verify_raw_ecdsa_with_leading_padding():
    """Test raw ECDSA verification with 0xff-padded signature (reference format)."""
    priv_key = PrivateKey()
    pub_key_bytes = priv_key.public_key.format(compressed=True)

    message = b"padded signature test"
    msg_hash = hashlib.sha256(message).digest()
    sig = priv_key.sign(msg_hash, hasher=None)

    # Pad signature with leading 0xff to 72 bytes (reference impl format)
    padded_sig = sig.rjust(72, b"\xff")

    # Should still verify
    assert verify_raw_ecdsa(msg_hash, padded_sig, pub_key_bytes)


def test_get_cert_msg():
    """Test certificate message format."""
    cert_pub = bytes.fromhex("0258efb077960d6848f001904857f062fa453de26c1ad8736f55497254f56e8a74")
    cert_expiry = 1

    msg = get_cert_msg(cert_pub, cert_expiry)
    expected = b"fidelity-bond-cert|" + cert_pub + b"|1"
    assert msg == expected


def test_get_ascii_cert_msg():
    """Test ASCII certificate message format."""
    cert_pub = bytes.fromhex("0258efb077960d6848f001904857f062fa453de26c1ad8736f55497254f56e8a74")
    cert_expiry = 1

    msg = get_ascii_cert_msg(cert_pub, cert_expiry)
    expected = (
        b"fidelity-bond-cert|"
        + b"0258efb077960d6848f001904857f062fa453de26c1ad8736f55497254f56e8a74"
        + b"|1"
    )
    assert msg == expected


def test_verify_bitcoin_message_signature():
    """Test Bitcoin message signature verification."""
    priv_key = PrivateKey()
    pub_key_bytes = priv_key.public_key.format(compressed=True)

    message = b"test message for bitcoin signing"

    # Create Bitcoin message hash
    prefix = b"\x18Bitcoin Signed Message:\n"
    varint = bytes([len(message)])
    full_msg = prefix + varint + message
    msg_hash = hashlib.sha256(hashlib.sha256(full_msg).digest()).digest()

    # Sign the hash
    sig = priv_key.sign(msg_hash, hasher=None)

    # Verify
    assert verify_bitcoin_message_signature(message, sig, pub_key_bytes)

    # Wrong message should fail
    assert not verify_bitcoin_message_signature(b"wrong", sig, pub_key_bytes)


def test_verify_fidelity_bond_proof_invalid_base64():
    """Test bond verification with invalid base64."""
    is_valid, data, error = verify_fidelity_bond_proof("not valid base64!!!", "J5maker", "J5taker")
    assert not is_valid
    assert data is None
    assert "base64" in error.lower()


def test_verify_fidelity_bond_proof_wrong_length():
    """Test bond verification with wrong length."""
    wrong_len_data = base64.b64encode(b"x" * 100).decode()
    is_valid, data, error = verify_fidelity_bond_proof(wrong_len_data, "J5maker", "J5taker")
    assert not is_valid
    assert data is None
    assert "length" in error.lower()


def _bitcoin_message_hash(message: bytes) -> bytes:
    """Helper: delegates to jmcore.crypto.bitcoin_message_hash_bytes."""
    return bitcoin_message_hash_bytes(message)


def test_verify_fidelity_bond_proof_roundtrip():
    """Test creating and verifying a bond proof using reference impl format."""
    # Generate keys
    utxo_priv_key = PrivateKey()
    utxo_pub_key = utxo_priv_key.public_key.format(compressed=True)

    cert_priv_key = PrivateKey()
    cert_pub_key = cert_priv_key.public_key.format(compressed=True)

    maker_nick = "J5testmaker123"
    taker_nick = "J5testtaker456"
    cert_expiry_encoded = 52  # Blocks / 2016

    # 1. Create certificate signature (utxo key signs cert message)
    # Reference format: b'fidelity-bond-cert|' + cert_pub + b'|' + str(cert_expiry).encode('ascii')
    cert_msg = get_cert_msg(cert_pub_key, cert_expiry_encoded)
    cert_msg_hash = _bitcoin_message_hash(cert_msg)
    cert_sig = utxo_priv_key.sign(cert_msg_hash, hasher=None)

    # 2. Create nick signature (cert key signs taker_nick|maker_nick)
    nick_msg = (taker_nick + "|" + maker_nick).encode("ascii")
    nick_msg_hash = _bitcoin_message_hash(nick_msg)
    nick_sig = cert_priv_key.sign(nick_msg_hash, hasher=None)

    # 3. Pad signatures to 72 bytes using rjust with 0xff (reference format)
    nick_sig_padded = nick_sig.rjust(72, b"\xff")
    cert_sig_padded = cert_sig.rjust(72, b"\xff")

    # Create proof
    txid = b"a" * 32
    vout = 0
    locktime = 800000

    proof_data = struct.pack(
        "<72s72s33sH33s32sII",
        nick_sig_padded,
        cert_sig_padded,
        cert_pub_key,
        cert_expiry_encoded,
        utxo_pub_key,
        txid,
        vout,
        locktime,
    )

    proof_b64 = base64.b64encode(proof_data).decode()

    # Verify the proof
    is_valid, data, error = verify_fidelity_bond_proof(proof_b64, maker_nick, taker_nick)

    assert is_valid, f"Verification failed: {error}"
    assert data is not None
    assert data["maker_nick"] == maker_nick
    assert data["taker_nick"] == taker_nick
    assert data["utxo_pub"] == utxo_pub_key.hex()
    assert data["cert_pub"] == cert_pub_key.hex()
    assert data["locktime"] == locktime
    assert data["utxo_vout"] == vout


def test_verify_fidelity_bond_proof_wrong_taker():
    """Test that verification fails with wrong taker nick."""
    utxo_priv_key = PrivateKey()
    utxo_pub_key = utxo_priv_key.public_key.format(compressed=True)

    cert_priv_key = PrivateKey()
    cert_pub_key = cert_priv_key.public_key.format(compressed=True)

    maker_nick = "J5maker"
    correct_taker = "J5correct"
    wrong_taker = "J5wrong"
    cert_expiry_encoded = 52

    # Create signatures for correct_taker
    cert_msg = get_cert_msg(cert_pub_key, cert_expiry_encoded)
    cert_msg_hash = _bitcoin_message_hash(cert_msg)
    cert_sig = utxo_priv_key.sign(cert_msg_hash, hasher=None)

    nick_msg = (correct_taker + "|" + maker_nick).encode("ascii")
    nick_msg_hash = _bitcoin_message_hash(nick_msg)
    nick_sig = cert_priv_key.sign(nick_msg_hash, hasher=None)

    nick_sig_padded = nick_sig.rjust(72, b"\xff")
    cert_sig_padded = cert_sig.rjust(72, b"\xff")

    proof_data = struct.pack(
        "<72s72s33sH33s32sII",
        nick_sig_padded,
        cert_sig_padded,
        cert_pub_key,
        cert_expiry_encoded,
        utxo_pub_key,
        b"b" * 32,
        0,
        800000,
    )

    proof_b64 = base64.b64encode(proof_data).decode()

    # Verification should fail with wrong taker
    is_valid, data, error = verify_fidelity_bond_proof(proof_b64, maker_nick, wrong_taker)
    assert not is_valid
    assert "nick signature" in error.lower()


def test_verify_fidelity_bond_proof_reference_vectors():
    """
    Test with actual test vectors from the reference implementation.

    These are from joinmarket-clientserver/test/jmdaemon/test_orderbookwatch.py
    """

    def hextobin(s: str) -> bytes:
        return bytes.fromhex(s)

    # Test vector 1: nicksig len = 71, certsig len = 71
    fidelity_bond_proof_1: dict[str, Any] = {
        "nick-signature": (
            b"0E\x02!\x00\xdbb\x15\x96\xa0\x87\xb8\x1d\xe05\xddV\xa1\x1bn\x8f"
            + b'q\x90&\x8cG@\x89"2\xb2\x81\x9b\xc00\xa5\xb6\x02 \x03\x14l\xd7BR\xba\x8c:\x88('
            + b"\x8e3l\xac\xf5`T\x87\xfa\xf5\xa9\x1f\x19\xc0\xb6\xe9\xbb\xdc\xc7y\x99"
        ),
        "certificate-signature": (
            "3045022100eb512af938113badb4d7b29e0c22061c51dadb113a9395e"
            + "9ed81a46103391213022029170de414964f07228c4f0d404b1386272bae337f0133f1329d948a"
            + "252fa2a0"
        ),
        "certificate-pubkey": "0258efb077960d6848f001904857f062fa453de26c1ad8736f55497254f56e8a74",
        "certificate-expiry": 1,
        "utxo-pubkey": "02f54f027377e84171296453828aa863c23fc4489453025f49bd3addfb3a359b3d",
        "txid": "84c88fafe0bb75f507fe3bfb29a93d10b2e80c15a63b2943c1a5fecb5a55cba2",
        "vout": 0,
        "locktime": 1640995200,
    }
    maker_nick_1 = "J5A4k9ecQzRRDfBx"
    taker_nick_1 = "J55VZ6U6ZyFDNeuv"

    # Construct the proof in the same way reference impl does
    nick_sig = fidelity_bond_proof_1["nick-signature"].rjust(72, b"\xff")
    cert_sig = hextobin(fidelity_bond_proof_1["certificate-signature"]).rjust(72, b"\xff")

    proof_data = struct.pack(
        "<72s72s33sH33s32sII",
        nick_sig,
        cert_sig,
        hextobin(fidelity_bond_proof_1["certificate-pubkey"]),
        fidelity_bond_proof_1["certificate-expiry"],
        hextobin(fidelity_bond_proof_1["utxo-pubkey"]),
        hextobin(fidelity_bond_proof_1["txid"]),
        fidelity_bond_proof_1["vout"],
        fidelity_bond_proof_1["locktime"],
    )

    proof_b64 = base64.b64encode(proof_data).decode()

    is_valid, data, error = verify_fidelity_bond_proof(proof_b64, maker_nick_1, taker_nick_1)
    assert is_valid, f"Reference vector 1 failed: {error}"
    assert data is not None
    assert data["utxo_txid"] == fidelity_bond_proof_1["txid"]
    assert data["utxo_vout"] == fidelity_bond_proof_1["vout"]
    assert data["locktime"] == fidelity_bond_proof_1["locktime"]


def test_verify_fidelity_bond_proof_reference_vector_2():
    """Test vector 2 from reference: nicksig len = 71, certsig len = 70"""

    def hextobin(s: str) -> bytes:
        return bytes.fromhex(s)

    fidelity_bond_proof: dict[str, Any] = {
        "nick-signature": (
            b"0E\x02!\x00\x80\xc6$\x0c\xa1\x15YS\xacHB\xb33\xfa~\x9f\xb9`\xb3"
            + b"\xfe\xed0\xadHq\xc1~\x03.B\xbb#\x02 #y~]\xd9\xbbX2\xc0\x1b\xe57\xf4\x0f\x1f"
            + b"\xd6$\x01\xf9\x15Z\xc9X\xa5\x18\xbe\x83\x1a&4Y\xd4"
        ),
        "certificate-signature": (
            "304402205669ea394f7381e9abf0b3c013fac2b79d24c02feb86ff153"
            + "cff83c658d7cf7402200b295ace655687f80738f3733c1dc5f1e2b8f351c017a05b8bd31983dd"
            + "4d723f"
        ),
        "certificate-pubkey": "031d1c006a6310dbdf57341efc19c3a43c402379d7ccd2480416cadc7579f973f7",
        "certificate-expiry": 1,
        "utxo-pubkey": "02616c56412eb738a9eacfb0550b43a5a2e77e5d5205ea9e2ca8dfac34e50c9754",
        "txid": "84c88fafe0bb75f507fe3bfb29a93d10b2e80c15a63b2943c1a5fecb5a55cba2",
        "vout": 1,
        "locktime": 1893456000,
    }
    maker_nick = "J54LS6YyJPoseqFS"
    taker_nick = "J55VZ6U6ZyFDNeuv"

    nick_sig = fidelity_bond_proof["nick-signature"].rjust(72, b"\xff")
    cert_sig = hextobin(fidelity_bond_proof["certificate-signature"]).rjust(72, b"\xff")

    proof_data = struct.pack(
        "<72s72s33sH33s32sII",
        nick_sig,
        cert_sig,
        hextobin(fidelity_bond_proof["certificate-pubkey"]),
        fidelity_bond_proof["certificate-expiry"],
        hextobin(fidelity_bond_proof["utxo-pubkey"]),
        hextobin(fidelity_bond_proof["txid"]),
        fidelity_bond_proof["vout"],
        fidelity_bond_proof["locktime"],
    )

    proof_b64 = base64.b64encode(proof_data).decode()

    is_valid, data, error = verify_fidelity_bond_proof(proof_b64, maker_nick, taker_nick)
    assert is_valid, f"Reference vector 2 failed: {error}"


def test_verify_fidelity_bond_proof_reference_vector_3():
    """Test vector 3 from reference: nicksig len = 70, certsig len = 71"""

    def hextobin(s: str) -> bytes:
        return bytes.fromhex(s)

    fidelity_bond_proof: dict[str, Any] = {
        "nick-signature": (
            b"0D\x02 K)\xe9\x17d\x0b\xc0\x82(\xd1\xa2*l\xd8\x0eJ\xc7\x01NV\xbf"
            + b'\xcb\x02O]\xc0\x11\x01\x01B"\xed\x02 ob\xa1\xf8>\x80U)\xc8\x96\x86\x1b \x0e'
            + b"\x00.\xf8\x86}\xcd\xf8\x82T\xa2\xb5\x8a4\xdb4\xbe\xf3{"
        ),
        "certificate-signature": (
            "3045022100d3beb5660bef33d095f92a3023bbbab15ece48ab2f211fa"
            + "935b62fe8b764c8c002204892deffb4c9aa0d734aa3f55cc8e2baae4a03fc5a9e571b4f671493"
            + "f1254df9"
        ),
        "certificate-pubkey": "03a2d1d15290d6d21204d1153c062970b4ff757a675e47a451fd0ba5c084127807",
        "certificate-expiry": 1,
        "utxo-pubkey": "03b9c12c9c31286772349b986653d07232327b284bd0787ad5829a04ac68f59b89",
        "txid": "70c2995b283db086813d97817264f10b8823b870298d30ab09cb43c6bf2670cf",
        "vout": 0,
        "locktime": 1735689600,
    }
    maker_nick = "J59PRzM6ZsdA5uyJ"
    taker_nick = "J55VZ6U6ZyFDNeuv"

    nick_sig = fidelity_bond_proof["nick-signature"].rjust(72, b"\xff")
    cert_sig = hextobin(fidelity_bond_proof["certificate-signature"]).rjust(72, b"\xff")

    proof_data = struct.pack(
        "<72s72s33sH33s32sII",
        nick_sig,
        cert_sig,
        hextobin(fidelity_bond_proof["certificate-pubkey"]),
        fidelity_bond_proof["certificate-expiry"],
        hextobin(fidelity_bond_proof["utxo-pubkey"]),
        hextobin(fidelity_bond_proof["txid"]),
        fidelity_bond_proof["vout"],
        fidelity_bond_proof["locktime"],
    )

    proof_b64 = base64.b64encode(proof_data).decode()

    is_valid, data, error = verify_fidelity_bond_proof(proof_b64, maker_nick, taker_nick)
    assert is_valid, f"Reference vector 3 failed: {error}"


def test_verify_fidelity_bond_proof_invalid_nick_sig():
    """Test that proof with no DER header in nick sig fails."""

    def hextobin(s: str) -> bytes:
        return bytes.fromhex(s)

    # Invalid nick signature (starts with 'Z' instead of 0x30)
    fidelity_bond_proof: dict[str, Any] = {
        "nick-signature": (
            b"ZD\x02 K)\xe9\x17d\x0b\xc0\x82(\xd1\xa2*l\xd8\x0eJ\xc7\x01NV\xbf"
            + b'\xcb\x02O]\xc0\x11\x01\x01B"\xed\x02 ob\xa1\xf8>\x80U)\xc8\x96\x86\x1b \x0e'
            + b"\x00.\xf8\x86}\xcd\xf8\x82T\xa2\xb5\x8a4\xdb4\xbe\xf3{"
        ),
        "certificate-signature": (
            "3045022100d3beb5660bef33d095f92a3023bbbab15ece48ab2f211fa"
            + "935b62fe8b764c8c002204892deffb4c9aa0d734aa3f55cc8e2baae4a03fc5a9e571b4f671493"
            + "f1254df9"
        ),
        "certificate-pubkey": "03a2d1d15290d6d21204d1153c062970b4ff757a675e47a451fd0ba5c084127807",
        "certificate-expiry": 1,
        "utxo-pubkey": "03b9c12c9c31286772349b986653d07232327b284bd0787ad5829a04ac68f59b89",
        "txid": "70c2995b283db086813d97817264f10b8823b870298d30ab09cb43c6bf2670cf",
        "vout": 0,
        "locktime": 1735689600,
    }
    maker_nick = "J59PRzM6ZsdA5uyJ"
    taker_nick = "J55VZ6U6ZyFDNeuv"

    nick_sig = fidelity_bond_proof["nick-signature"].rjust(72, b"\xff")
    cert_sig = hextobin(fidelity_bond_proof["certificate-signature"]).rjust(72, b"\xff")

    proof_data = struct.pack(
        "<72s72s33sH33s32sII",
        nick_sig,
        cert_sig,
        hextobin(fidelity_bond_proof["certificate-pubkey"]),
        fidelity_bond_proof["certificate-expiry"],
        hextobin(fidelity_bond_proof["utxo-pubkey"]),
        hextobin(fidelity_bond_proof["txid"]),
        fidelity_bond_proof["vout"],
        fidelity_bond_proof["locktime"],
    )

    proof_b64 = base64.b64encode(proof_data).decode()

    is_valid, data, error = verify_fidelity_bond_proof(proof_b64, maker_nick, taker_nick)
    assert not is_valid
    assert "der header not found" in error.lower()


# ==============================================================================
# NickIdentity tests
# ==============================================================================


class TestNickIdentity:
    """Tests for NickIdentity class."""

    def test_nick_format(self):
        """Test that NickIdentity generates a valid J5 nick."""
        identity = NickIdentity(version=5)
        assert identity.nick.startswith("J5")
        assert len(identity.nick) == 16  # J + version digit + 14 chars

    def test_deterministic_from_privkey(self):
        """Test that same private key gives same nick."""
        privkey = hashlib.sha256(b"test_seed").digest()
        id1 = NickIdentity(version=5, private_key_bytes=privkey)
        id2 = NickIdentity(version=5, private_key_bytes=privkey)
        assert id1.nick == id2.nick
        assert id1.public_key_hex == id2.public_key_hex

    def test_public_key_hex_format(self):
        """Test public key hex is 66 chars (33 bytes compressed)."""
        identity = NickIdentity()
        assert len(identity.public_key_hex) == 66
        assert identity.public_key_hex[:2] in ("02", "03")

    def test_sign_message(self):
        """Test message signing produces valid format."""
        identity = NickIdentity()
        signed = identity.sign_message("test_message")
        parts = signed.split(" ")
        assert len(parts) == 3
        assert parts[0] == "test_message"
        assert parts[1] == identity.public_key_hex
        # Third part is base64-encoded signature
        base64.b64decode(parts[2])

    def test_sign_message_with_hostid(self):
        """Test message signing with hostid appended."""
        identity = NickIdentity()
        signed = identity.sign_message("hello", hostid="server123")
        parts = signed.split(" ")
        assert parts[0] == "hello"

    def test_sign_message_verifiable(self):
        """Test that signed messages can be verified."""
        identity = NickIdentity()
        signed = identity.sign_message("verify_me")
        parts = signed.split(" ")
        pubkey_bytes = bytes.fromhex(parts[1])
        assert ecdsa_verify("verify_me", parts[2], pubkey_bytes)

    def test_different_privkeys_different_nicks(self):
        """Test different private keys generate different nicks."""
        id1 = NickIdentity(private_key_bytes=hashlib.sha256(b"key1").digest())
        id2 = NickIdentity(private_key_bytes=hashlib.sha256(b"key2").digest())
        assert id1.nick != id2.nick


# ==============================================================================
# bitcoin_message_hash varint branch tests
# ==============================================================================


class TestBitcoinMessageHashVarint:
    """Test varint encoding branches in bitcoin_message_hash."""

    def test_short_message(self):
        """Message < 253 bytes uses 1-byte varint."""
        h = bitcoin_message_hash("hello")
        assert len(h) == 32

    def test_medium_message(self):
        """Message 253-65535 bytes uses 3-byte varint (0xfd prefix)."""
        msg = "a" * 300
        h = bitcoin_message_hash(msg)
        assert len(h) == 32

    def test_large_message(self):
        """Message 65536+ bytes uses 5-byte varint (0xfe prefix)."""
        msg = "b" * 70000
        h = bitcoin_message_hash(msg)
        assert len(h) == 32


class TestBitcoinMessageHashBytesVarint:
    """Test varint encoding branches in bitcoin_message_hash_bytes."""

    def test_short_message(self):
        """Message < 253 bytes uses 1-byte varint."""
        h = bitcoin_message_hash_bytes(b"hello")
        assert len(h) == 32

    def test_medium_message(self):
        """Message 253-65535 bytes uses 3-byte varint (0xfd prefix)."""
        msg = b"a" * 300
        h = bitcoin_message_hash_bytes(msg)
        assert len(h) == 32

    def test_large_message(self):
        """Message 65536+ bytes uses 5-byte varint (0xfe prefix)."""
        msg = b"b" * 70000
        h = bitcoin_message_hash_bytes(msg)
        assert len(h) == 32

    def test_consistency(self):
        """bitcoin_message_hash and bitcoin_message_hash_bytes should agree for ASCII."""
        msg_str = "test consistency"
        h1 = bitcoin_message_hash(msg_str)
        h2 = bitcoin_message_hash_bytes(msg_str.encode("utf-8"))
        assert h1 == h2


# ==============================================================================
# Additional edge case tests
# ==============================================================================


class TestBase58CheckEncode:
    """Tests for base58check_encode."""

    def test_basic(self):
        """Test base58check encoding produces non-empty result."""
        result = base58check_encode(b"\x00" + b"\xaa" * 20)
        assert len(result) > 0

    def test_deterministic(self):
        """Same input always gives same output."""
        data = b"\x05" + b"\xbb" * 20
        assert base58check_encode(data) == base58check_encode(data)


class TestMnemonicToSeed:
    """Tests for mnemonic_to_seed (BIP39)."""

    def test_produces_64_bytes(self):
        """BIP39 seed is always 64 bytes."""
        mnemonic = "abandon " * 11 + "about"
        seed = mnemonic_to_seed(mnemonic)
        assert len(seed) == 64

    def test_with_passphrase(self):
        """Different passphrase produces different seed."""
        mnemonic = "abandon " * 11 + "about"
        seed1 = mnemonic_to_seed(mnemonic, passphrase="")
        seed2 = mnemonic_to_seed(mnemonic, passphrase="my_passphrase")
        assert seed1 != seed2

    def test_deterministic(self):
        """Same mnemonic + passphrase always produces same seed."""
        mnemonic = "abandon " * 11 + "about"
        seed1 = mnemonic_to_seed(mnemonic, passphrase="test")
        seed2 = mnemonic_to_seed(mnemonic, passphrase="test")
        assert seed1 == seed2

    def test_bip39_trezor_vector(self):
        """BIP39 spec test vector: 'abandon abandon ... about' / 'TREZOR'.
        Source: https://github.com/trezor/python-mnemonic/blob/master/vectors.json"""
        mnemonic = "abandon " * 11 + "about"
        seed = mnemonic_to_seed(mnemonic, passphrase="TREZOR")
        expected = bytes.fromhex(
            "c55257c360c07c72029aebc1b53c05ed0362ada38ead3e3e9efa3708e53495531"
            "f09a6987599d18264c1e1c92f2cf141630c7a3c4ab7c81b2f001698e7463b04"
        )
        assert seed == expected

    def test_nfkd_normalization_on_passphrase(self):
        """BIP39 requires NFKD normalization of the passphrase before
        PBKDF2. Two visually identical but differently-composed Unicode
        passphrases must produce the same seed."""
        import unicodedata

        mnemonic = "abandon " * 11 + "about"
        # NFC composed (single codepoint U+00E9)
        passphrase_nfc = "caf\u00e9"
        # NFD decomposed (e + combining acute accent)
        passphrase_nfd = "cafe\u0301"
        assert passphrase_nfc != passphrase_nfd  # different byte sequences
        assert unicodedata.normalize("NFKD", passphrase_nfc) == unicodedata.normalize(
            "NFKD", passphrase_nfd
        )
        seed_nfc = mnemonic_to_seed(mnemonic, passphrase=passphrase_nfc)
        seed_nfd = mnemonic_to_seed(mnemonic, passphrase=passphrase_nfd)
        assert seed_nfc == seed_nfd


class TestStripSignaturePaddingEdgeCases:
    """Additional edge cases for strip_signature_padding."""

    def test_no_der_header_strips_trailing_zeros(self):
        """Signature without 0x30 DER header: strip trailing zeros."""
        sig = b"\x01\x02\x03\x00\x00\x00"
        result = strip_signature_padding(sig)
        assert result == b"\x01\x02\x03"

    def test_empty_signature(self):
        """Empty signature returns empty bytes."""
        result = strip_signature_padding(b"")
        assert result == b""

    def test_only_padding(self):
        """Signature that's only zeros returns empty after stripping."""
        result = strip_signature_padding(b"\x00\x00\x00")
        assert result == b""


class TestVerifyRawEcdsaEdgeCases:
    """Additional edge cases for verify_raw_ecdsa."""

    def test_empty_signature(self):
        """Empty signature returns False."""
        pub_key = PrivateKey().public_key.format(compressed=True)
        msg_hash = hashlib.sha256(b"test").digest()
        assert not verify_raw_ecdsa(msg_hash, b"", pub_key)

    def test_invalid_pubkey(self):
        """Invalid public key returns False."""
        msg_hash = hashlib.sha256(b"test").digest()
        assert not verify_raw_ecdsa(msg_hash, b"\x30" + b"\x00" * 70, b"\x00" * 33)

    def test_all_zero_signature_stripped(self):
        """All-zero signature is stripped to empty and returns False."""
        pub_key = PrivateKey().public_key.format(compressed=True)
        msg_hash = hashlib.sha256(b"test").digest()
        assert not verify_raw_ecdsa(msg_hash, b"\x00" * 72, pub_key)


class TestVerifyBitcoinMessageSignatureEdgeCases:
    """Edge cases for verify_bitcoin_message_signature."""

    def test_invalid_pubkey(self):
        """Invalid public key returns False (exception caught)."""
        assert not verify_bitcoin_message_signature(b"msg", b"\x30\x00", b"\x00" * 33)

    def test_empty_message(self):
        """Empty message with valid sig structure returns False for wrong key."""
        priv_key = PrivateKey()
        pub_key = priv_key.public_key.format(compressed=True)
        wrong_pub = PrivateKey().public_key.format(compressed=True)
        msg = b""
        msg_hash = bitcoin_message_hash_bytes(msg)
        sig = priv_key.sign(msg_hash, hasher=None)
        assert verify_bitcoin_message_signature(msg, sig, pub_key)
        assert not verify_bitcoin_message_signature(msg, sig, wrong_pub)


class TestVerifyFidelityBondProofEdgeCases:
    """Additional edge cases for verify_fidelity_bond_proof."""

    def test_invalid_cert_sig_no_der_header(self):
        """Test that proof with no DER header in cert sig fails."""
        utxo_priv_key = PrivateKey()
        utxo_pub_key = utxo_priv_key.public_key.format(compressed=True)

        cert_priv_key = PrivateKey()
        cert_pub_key = cert_priv_key.public_key.format(compressed=True)

        maker_nick = "J5maker"
        taker_nick = "J5taker"
        cert_expiry_encoded = 52

        # Create valid nick signature
        nick_msg = (taker_nick + "|" + maker_nick).encode("ascii")
        nick_msg_hash = _bitcoin_message_hash(nick_msg)
        nick_sig = cert_priv_key.sign(nick_msg_hash, hasher=None)
        nick_sig_padded = nick_sig.rjust(72, b"\xff")

        # Create cert sig without 0x30 header (all 'Z' bytes)
        cert_sig_padded = b"Z" * 72

        proof_data = struct.pack(
            "<72s72s33sH33s32sII",
            nick_sig_padded,
            cert_sig_padded,
            cert_pub_key,
            cert_expiry_encoded,
            utxo_pub_key,
            b"a" * 32,
            0,
            800000,
        )

        proof_b64 = base64.b64encode(proof_data).decode()
        is_valid, data, error = verify_fidelity_bond_proof(proof_b64, maker_nick, taker_nick)
        assert not is_valid
        assert "certificate signature" in error.lower() or "der header" in error.lower()

    def test_valid_proof_with_ascii_cert_format(self):
        """Test bond proof verification with ASCII certificate format."""
        utxo_priv_key = PrivateKey()
        utxo_pub_key = utxo_priv_key.public_key.format(compressed=True)

        cert_priv_key = PrivateKey()
        cert_pub_key = cert_priv_key.public_key.format(compressed=True)

        maker_nick = "J5makerascii"
        taker_nick = "J5takerascii"
        cert_expiry_encoded = 10

        # Create certificate signature using ASCII format
        ascii_cert_msg = get_ascii_cert_msg(cert_pub_key, cert_expiry_encoded)
        cert_msg_hash = _bitcoin_message_hash(ascii_cert_msg)
        cert_sig = utxo_priv_key.sign(cert_msg_hash, hasher=None)

        # Create nick signature
        nick_msg = (taker_nick + "|" + maker_nick).encode("ascii")
        nick_msg_hash = _bitcoin_message_hash(nick_msg)
        nick_sig = cert_priv_key.sign(nick_msg_hash, hasher=None)

        nick_sig_padded = nick_sig.rjust(72, b"\xff")
        cert_sig_padded = cert_sig.rjust(72, b"\xff")

        proof_data = struct.pack(
            "<72s72s33sH33s32sII",
            nick_sig_padded,
            cert_sig_padded,
            cert_pub_key,
            cert_expiry_encoded,
            utxo_pub_key,
            b"b" * 32,
            1,
            900000,
        )

        proof_b64 = base64.b64encode(proof_data).decode()
        is_valid, data, error = verify_fidelity_bond_proof(proof_b64, maker_nick, taker_nick)
        assert is_valid, f"ASCII cert format verification failed: {error}"
        assert data is not None
        assert data["cert_expiry"] == cert_expiry_encoded * 2016
