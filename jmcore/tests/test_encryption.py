"""
Tests for jmcore.encryption - NaCl end-to-end encryption for CoinJoin protocol.
"""

from __future__ import annotations

import base64

import pytest
from nacl.exceptions import CryptoError

from jmcore.encryption import (
    CryptoSession,
    NaclError,
    create_encryption_box,
    decode_decrypt,
    encrypt_encode,
    get_pubkey,
    init_keypair,
    init_pubkey,
)


def test_init_keypair():
    """Test creating a new NaCl keypair."""
    keypair = init_keypair()
    assert keypair is not None
    # PyNaCl PrivateKey exposes 32-byte raw private key via bytes(...)
    # and its matching public key via the .public_key attribute.
    assert hasattr(keypair, "public_key")
    assert len(bytes(keypair)) == 32  # NaCl secret keys are 32 bytes
    assert len(bytes(keypair.public_key)) == 32  # NaCl public keys are 32 bytes


def test_get_pubkey_hex():
    """Test extracting public key as hex."""
    keypair = init_keypair()
    pubkey_hex = get_pubkey(keypair, as_hex=True)

    assert isinstance(pubkey_hex, str)
    assert len(pubkey_hex) == 64  # 32 bytes as hex = 64 chars
    # Should be valid hex
    bytes.fromhex(pubkey_hex)


def test_get_pubkey_bytes():
    """Test extracting public key as bytes."""
    keypair = init_keypair()
    pubkey_bytes = get_pubkey(keypair, as_hex=False)

    assert isinstance(pubkey_bytes, bytes)
    assert len(pubkey_bytes) == 32


def test_get_pubkey_invalid_input():
    """Test get_pubkey with invalid input."""
    with pytest.raises(NaclError, match="not a nacl keypair"):
        get_pubkey("not a keypair", as_hex=True)


def test_init_pubkey():
    """Test creating a public key from hex."""
    keypair = init_keypair()
    pubkey_hex = get_pubkey(keypair, as_hex=True)
    assert isinstance(pubkey_hex, str)

    # Recreate the public key from hex
    pubkey = init_pubkey(pubkey_hex)
    assert pubkey is not None
    # PyNaCl PublicKey exposes the 32 raw bytes via bytes(...).
    assert len(bytes(pubkey)) == 32


def test_init_pubkey_invalid_hex():
    """Test init_pubkey with invalid hex."""
    with pytest.raises(NaclError, match="Invalid hex format"):
        init_pubkey("not valid hex!!!")


def test_init_pubkey_wrong_length():
    """Test init_pubkey with wrong length."""
    # 31 bytes instead of 32
    short_hex = "a" * 62
    with pytest.raises(NaclError, match="Public key must be 32 bytes"):
        init_pubkey(short_hex)

    # 33 bytes instead of 32
    long_hex = "a" * 66
    with pytest.raises(NaclError, match="Public key must be 32 bytes"):
        init_pubkey(long_hex)


def test_create_encryption_box():
    """Test creating an encryption box for two parties."""
    # Create two keypairs (simulating taker and maker)
    taker_keypair = init_keypair()
    maker_keypair = init_keypair()

    # Get maker's public key
    maker_pubkey_hex = get_pubkey(maker_keypair, as_hex=True)
    assert isinstance(maker_pubkey_hex, str)
    maker_pubkey = init_pubkey(maker_pubkey_hex)

    # Taker creates box for communicating with maker
    box = create_encryption_box(taker_keypair, maker_pubkey)
    assert box is not None


def test_create_encryption_box_invalid_inputs():
    """Test create_encryption_box with invalid inputs."""
    keypair = init_keypair()
    pubkey_hex = get_pubkey(keypair, as_hex=True)
    assert isinstance(pubkey_hex, str)
    pubkey = init_pubkey(pubkey_hex)

    # Invalid counterparty key
    with pytest.raises(NaclError, match="not a public key"):
        create_encryption_box(keypair, "not a key")

    # Invalid our keypair
    with pytest.raises(NaclError, match="not a nacl keypair"):
        create_encryption_box("not a keypair", pubkey)


def test_encrypt_decrypt_roundtrip():
    """Test encrypting and decrypting a message."""
    # Create two keypairs
    alice_keypair = init_keypair()
    bob_keypair = init_keypair()

    # Setup encryption boxes
    bob_pubkey_hex = get_pubkey(bob_keypair, as_hex=True)
    assert isinstance(bob_pubkey_hex, str)
    bob_pubkey = init_pubkey(bob_pubkey_hex)

    alice_pubkey_hex = get_pubkey(alice_keypair, as_hex=True)
    assert isinstance(alice_pubkey_hex, str)
    alice_pubkey = init_pubkey(alice_pubkey_hex)

    # Alice creates box to send to Bob
    alice_to_bob = create_encryption_box(alice_keypair, bob_pubkey)

    # Bob creates box to receive from Alice
    bob_from_alice = create_encryption_box(bob_keypair, alice_pubkey)

    # Alice encrypts a message
    plaintext = "Hello Bob, this is a secret CoinJoin message!"
    ciphertext = encrypt_encode(plaintext, alice_to_bob)

    # Ciphertext should be base64
    assert isinstance(ciphertext, str)
    base64.b64decode(ciphertext)  # Should not raise

    # Bob decrypts the message
    decrypted = decode_decrypt(ciphertext, bob_from_alice)
    assert decrypted.decode("utf-8") == plaintext


def test_encrypt_bytes():
    """Test encrypting bytes instead of string."""
    alice_keypair = init_keypair()
    bob_keypair = init_keypair()

    bob_pubkey_hex = get_pubkey(bob_keypair, as_hex=True)
    assert isinstance(bob_pubkey_hex, str)
    bob_pubkey = init_pubkey(bob_pubkey_hex)

    alice_to_bob = create_encryption_box(alice_keypair, bob_pubkey)

    plaintext_bytes = b"Binary data \x00\x01\x02"
    ciphertext = encrypt_encode(plaintext_bytes, alice_to_bob)

    assert isinstance(ciphertext, str)
    base64.b64decode(ciphertext)


def test_crypto_session_init():
    """Test CryptoSession initialization."""
    session = CryptoSession()

    assert session.keypair is not None
    assert session.box is None
    assert session.counterparty_pubkey == ""
    assert not session.is_encrypted


def test_crypto_session_get_pubkey():
    """Test getting public key from CryptoSession."""
    session = CryptoSession()
    pubkey = session.get_pubkey_hex()

    assert isinstance(pubkey, str)
    assert len(pubkey) == 64
    bytes.fromhex(pubkey)  # Should be valid hex


def test_crypto_session_setup_encryption():
    """Test setting up encryption in CryptoSession."""
    session1 = CryptoSession()
    session2 = CryptoSession()

    # Exchange public keys
    pubkey1 = session1.get_pubkey_hex()
    pubkey2 = session2.get_pubkey_hex()

    # Setup encryption
    session1.setup_encryption(pubkey2)
    session2.setup_encryption(pubkey1)

    assert session1.is_encrypted
    assert session2.is_encrypted
    assert session1.counterparty_pubkey == pubkey2
    assert session2.counterparty_pubkey == pubkey1


def test_crypto_session_encrypt_decrypt():
    """Test encrypting and decrypting with CryptoSession."""
    # Simulate taker and maker sessions
    taker = CryptoSession()
    maker = CryptoSession()

    # Exchange keys
    taker.setup_encryption(maker.get_pubkey_hex())
    maker.setup_encryption(taker.get_pubkey_hex())

    # Taker sends message to maker
    message = "!auth revelation_data_here"
    encrypted = taker.encrypt(message)

    # Encrypted should be base64
    assert isinstance(encrypted, str)
    base64.b64decode(encrypted)

    # Maker decrypts
    decrypted = maker.decrypt(encrypted)
    assert decrypted == message


def test_crypto_session_encrypt_before_setup():
    """Test that encrypting before setup raises error."""
    session = CryptoSession()

    with pytest.raises(NaclError, match="Encryption not set up"):
        session.encrypt("test message")


def test_crypto_session_decrypt_before_setup():
    """Test that decrypting before setup raises error."""
    session = CryptoSession()

    with pytest.raises(NaclError, match="Encryption not set up"):
        session.decrypt("fake_encrypted_data")


def test_crypto_session_setup_invalid_pubkey():
    """Test setting up encryption with invalid public key."""
    session = CryptoSession()

    with pytest.raises(NaclError):
        session.setup_encryption("invalid hex!!!")


def test_crypto_session_bidirectional():
    """Test bidirectional communication between two sessions."""
    # Simulate full protocol flow
    taker = CryptoSession()
    maker = CryptoSession()

    # Setup encryption (like in !fill phase)
    taker.setup_encryption(maker.get_pubkey_hex())
    maker.setup_encryption(taker.get_pubkey_hex())

    # Taker sends !auth
    auth_msg = "txid:vout|P|P2|sig|e"
    auth_encrypted = taker.encrypt(auth_msg)
    auth_decrypted = maker.decrypt(auth_encrypted)
    assert auth_decrypted == auth_msg

    # Maker sends !ioauth
    ioauth_msg = "utxo_list auth_pub cj_addr change_addr btc_sig"
    ioauth_encrypted = maker.encrypt(ioauth_msg)
    ioauth_decrypted = taker.decrypt(ioauth_encrypted)
    assert ioauth_decrypted == ioauth_msg

    # Taker sends !tx
    tx_msg = base64.b64encode(b"unsigned transaction bytes").decode()
    tx_encrypted = taker.encrypt(tx_msg)
    tx_decrypted = maker.decrypt(tx_encrypted)
    assert tx_decrypted == tx_msg

    # Maker sends !sig
    sig_msg = base64.b64encode(b"signature bytes").decode()
    sig_encrypted = maker.encrypt(sig_msg)
    sig_decrypted = taker.decrypt(sig_encrypted)
    assert sig_decrypted == sig_msg


def test_crypto_session_unicode_messages():
    """Test encryption/decryption of unicode messages."""
    session1 = CryptoSession()
    session2 = CryptoSession()

    session1.setup_encryption(session2.get_pubkey_hex())
    session2.setup_encryption(session1.get_pubkey_hex())

    # Test various unicode characters
    unicode_msg = "Hello 世界 🌍 Bitcoin ₿"
    encrypted = session1.encrypt(unicode_msg)
    decrypted = session2.decrypt(encrypted)

    assert decrypted == unicode_msg


def test_crypto_session_empty_message():
    """Test encrypting and decrypting empty message."""
    session1 = CryptoSession()
    session2 = CryptoSession()

    session1.setup_encryption(session2.get_pubkey_hex())
    session2.setup_encryption(session1.get_pubkey_hex())

    empty = ""
    encrypted = session1.encrypt(empty)
    decrypted = session2.decrypt(encrypted)

    assert decrypted == empty


def test_crypto_session_long_message():
    """Test encryption of long messages."""
    session1 = CryptoSession()
    session2 = CryptoSession()

    session1.setup_encryption(session2.get_pubkey_hex())
    session2.setup_encryption(session1.get_pubkey_hex())

    # Long message (simulate a large transaction)
    long_msg = "x" * 10000
    encrypted = session1.encrypt(long_msg)
    decrypted = session2.decrypt(encrypted)

    assert decrypted == long_msg
    assert len(encrypted) > len(long_msg)  # Encrypted should be larger


def test_different_keypairs_cannot_decrypt():
    """Test that a message encrypted with one key cannot be decrypted with different key."""
    alice = CryptoSession()
    bob = CryptoSession()
    eve = CryptoSession()  # Eavesdropper

    # Alice and Bob setup encryption
    alice.setup_encryption(bob.get_pubkey_hex())
    bob.setup_encryption(alice.get_pubkey_hex())

    # Eve tries to setup with Alice's messages but has different key
    eve.setup_encryption(alice.get_pubkey_hex())

    # Alice sends to Bob
    message = "Secret transaction data"
    encrypted = alice.encrypt(message)

    # Bob can decrypt
    assert bob.decrypt(encrypted) == message

    # Eve cannot decrypt (will raise or give garbage)
    with pytest.raises(CryptoError):  # NaCl will raise on invalid decryption
        eve.decrypt(encrypted)


def test_crypto_session_reusable():
    """Test that CryptoSession can encrypt multiple messages."""
    session1 = CryptoSession()
    session2 = CryptoSession()

    session1.setup_encryption(session2.get_pubkey_hex())
    session2.setup_encryption(session1.get_pubkey_hex())

    # Send multiple messages
    messages = [
        "First message",
        "Second message",
        "Third message with more data",
    ]

    for msg in messages:
        encrypted = session1.encrypt(msg)
        decrypted = session2.decrypt(encrypted)
        assert decrypted == msg


# Wire-format compatibility test vector.
#
# These bytes were generated with the legacy ``libnacl`` backend (which is what
# the reference JoinMarket implementation uses on the wire) and asserts that
# our current PyNaCl-based backend decrypts them byte-identically. This guards
# against any accidental switch to a non-standard format (e.g. detached MAC,
# different nonce length) that would break interop with reference makers and
# takers.
LIBNACL_INTEROP_ALICE_SK_HEX = "01" * 32
LIBNACL_INTEROP_BOB_SK_HEX = "02" * 32
LIBNACL_INTEROP_BOB_PK_HEX = "ce8d3ad1ccb633ec7b70c17814a5c76ecd029685050d344745ba05870e587d59"
LIBNACL_INTEROP_ALICE_PK_HEX = "a4e09292b651c278b9772c569f5fa9bb13d906b46ab68c9df9dc2b4409f8a209"
LIBNACL_INTEROP_NONCE_HEX = "000102030405060708090a0b0c0d0e0f1011121314151617"
LIBNACL_INTEROP_PLAINTEXT = b"joinmarket interop test vector v1"
LIBNACL_INTEROP_CIPHERTEXT_HEX = (
    "000102030405060708090a0b0c0d0e0f101112131415161736c590463133f68c"
    "31a400facb0e744388a0eaadf750945ddc2258aea2e8628ab4a35c92c6c4bf52"
    "772538564f62cccaf7"
)


def test_libnacl_wire_vector_decryptable() -> None:
    """PyNaCl must decrypt ciphertext produced by libnacl (reference impl)."""
    from nacl import public

    bob_sk = public.PrivateKey(bytes.fromhex(LIBNACL_INTEROP_BOB_SK_HEX))
    alice_pk = public.PublicKey(bytes.fromhex(LIBNACL_INTEROP_ALICE_PK_HEX))
    box = public.Box(bob_sk, alice_pk)

    ct = bytes.fromhex(LIBNACL_INTEROP_CIPHERTEXT_HEX)
    plaintext = box.decrypt(ct)
    assert plaintext == LIBNACL_INTEROP_PLAINTEXT


def test_pynacl_encryption_matches_libnacl_format() -> None:
    """Encrypting with a fixed nonce must reproduce the libnacl byte layout."""
    from nacl import public

    alice_sk = public.PrivateKey(bytes.fromhex(LIBNACL_INTEROP_ALICE_SK_HEX))
    bob_pk = public.PublicKey(bytes.fromhex(LIBNACL_INTEROP_BOB_PK_HEX))
    box = public.Box(alice_sk, bob_pk)

    nonce = bytes.fromhex(LIBNACL_INTEROP_NONCE_HEX)
    encrypted = box.encrypt(LIBNACL_INTEROP_PLAINTEXT, nonce)
    assert bytes(encrypted).hex() == LIBNACL_INTEROP_CIPHERTEXT_HEX
