"""
Cryptographic primitives for JoinMarket.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets

from coincurve import PrivateKey, PublicKey
from coincurve import verify_signature as coincurve_verify
from mnemonic import Mnemonic

from jmcore.protocol import JM_VERSION

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
NICK_HASH_LENGTH = 10
NICK_MAX_ENCODED = 14


class CryptoError(Exception):
    pass


def base58_encode(data: bytes) -> str:
    """Encode bytes as Base58 (no checksum)."""
    num = int.from_bytes(data, "big")

    result = ""
    while num > 0:
        num, remainder = divmod(num, 58)
        result = BASE58_ALPHABET[remainder] + result

    for byte in data:
        if byte == 0:
            result = BASE58_ALPHABET[0] + result
        else:
            break

    return result


def base58check_encode(payload: bytes) -> str:
    """Encode bytes with Base58Check (with double-SHA256 checksum)."""
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    return base58_encode(payload + checksum)


def mnemonic_to_seed(mnemonic: str, passphrase: str = "") -> bytes:
    """
    Convert BIP39 mnemonic to 64-byte seed using PBKDF2-HMAC-SHA512.

    Delegates to the audited ``mnemonic`` library, which applies the
    NFKD Unicode normalization to both the mnemonic and passphrase
    before PBKDF2 as required by BIP39. The previous hand-rolled
    implementation skipped normalization, so non-ASCII passphrases
    derived seeds that did not match the BIP39 spec (and did not match
    the legacy joinmarket-clientserver, which also uses this library).
    For ASCII passphrases (the overwhelmingly common case) the output
    is bit-identical to the previous implementation.

    Args:
        mnemonic: Space-separated mnemonic words
        passphrase: Optional BIP39 passphrase (default empty)

    Returns:
        64-byte seed
    """
    return Mnemonic.to_seed(mnemonic, passphrase=passphrase)


def generate_jm_nick(version: int = JM_VERSION) -> str:
    privkey_bytes = secrets.token_bytes(32)
    private_key = PrivateKey(privkey_bytes)
    # Use compressed pubkey (33 bytes) - matches reference implementation
    pubkey_bytes = private_key.public_key.format(compressed=True)

    pubkey_hex = binascii.hexlify(pubkey_bytes)
    nick_pkh_raw = hashlib.sha256(pubkey_hex).digest()[:NICK_HASH_LENGTH]
    nick_pkh = base58_encode(nick_pkh_raw)

    nick_pkh += "O" * (NICK_MAX_ENCODED - len(nick_pkh))

    return f"J{version}{nick_pkh}"


def bitcoin_message_hash(message: str) -> bytes:
    """
    Hash a message using Bitcoin's message signing format.

    Format: SHA256(SHA256("\\x18Bitcoin Signed Message:\\n" + varint(len) + message))
    """
    prefix = b"\x18Bitcoin Signed Message:\n"

    # Encode message to bytes
    msg_bytes = message.encode("utf-8")

    # Create varint for message length
    msg_len = len(msg_bytes)
    if msg_len < 253:
        varint = bytes([msg_len])
    elif msg_len < 0x10000:
        varint = b"\xfd" + msg_len.to_bytes(2, "little")
    elif msg_len < 0x100000000:
        varint = b"\xfe" + msg_len.to_bytes(4, "little")
    else:
        varint = b"\xff" + msg_len.to_bytes(8, "little")

    # Full message to hash
    full_msg = prefix + varint + msg_bytes

    # Double SHA256
    return hashlib.sha256(hashlib.sha256(full_msg).digest()).digest()


def bitcoin_message_hash_bytes(message: bytes) -> bytes:
    """
    Hash a raw bytes message using Bitcoin's message signing format.

    Format: SHA256(SHA256("\\x18Bitcoin Signed Message:\\n" + varint(len) + message))

    This is the same as bitcoin_message_hash but takes raw bytes instead of a string.
    Used for certificate messages that contain binary data (pubkeys).

    Args:
        message: Raw message bytes (NOT pre-hashed)

    Returns:
        32-byte message hash
    """
    prefix = b"\x18Bitcoin Signed Message:\n"
    msg_len = len(message)

    # Encode length as Bitcoin varint
    if msg_len < 253:
        varint = bytes([msg_len])
    elif msg_len < 0x10000:
        varint = b"\xfd" + msg_len.to_bytes(2, "little")
    elif msg_len < 0x100000000:
        varint = b"\xfe" + msg_len.to_bytes(4, "little")
    else:
        varint = b"\xff" + msg_len.to_bytes(8, "little")

    full_msg = prefix + varint + message
    return hashlib.sha256(hashlib.sha256(full_msg).digest()).digest()


def ecdsa_sign(message: str, private_key_bytes: bytes) -> str:
    """
    Sign a message with ECDSA using Bitcoin message format.

    Args:
        message: The message to sign (as string)
        private_key_bytes: 32-byte private key

    Returns:
        Base64-encoded signature
    """
    # Hash using Bitcoin message format
    msg_hash = bitcoin_message_hash(message)

    # Sign with coincurve (raw signature, no additional hashing)
    priv_key = PrivateKey(private_key_bytes)
    signature = priv_key.sign(msg_hash, hasher=None)

    return base64.b64encode(signature).decode("ascii")


def ecdsa_verify(message: str, signature_b64: str, pubkey_bytes: bytes) -> bool:
    """
    Verify an ECDSA signature using Bitcoin message format.

    Args:
        message: The signed message (as string)
        signature_b64: Base64-encoded signature
        pubkey_bytes: Compressed public key (33 bytes)

    Returns:
        True if signature is valid
    """
    try:
        # Hash using Bitcoin message format
        msg_hash = bitcoin_message_hash(message)

        # Decode signature from base64
        signature = base64.b64decode(signature_b64)

        # Verify with coincurve (hasher=None because we already hashed)
        return coincurve_verify(signature, msg_hash, pubkey_bytes, hasher=None)
    except Exception:
        return False


class NickIdentity:
    """
    Encapsulates a JoinMarket nick identity with signing capabilities.

    Each participant has a nick identity consisting of:
    - A private key for signing messages
    - A public key derived from the private key
    - A nick derived from hash(hex(pubkey))

    All private messages must be signed with this key for nick authentication.
    """

    def __init__(self, version: int = 5, private_key_bytes: bytes | None = None):
        """
        Create a new nick identity.

        Args:
            version: JoinMarket protocol version (default 5)
            private_key_bytes: Optional 32-byte private key (random if not provided)
        """
        if private_key_bytes is None:
            # Match reference: hashlib.sha256(os.urandom(16)).digest()
            private_key_bytes = hashlib.sha256(secrets.token_bytes(16)).digest()

        self._private_key_bytes = private_key_bytes
        self._private_key = PrivateKey(private_key_bytes)
        self._public_key = self._private_key.public_key
        self._version = version

        # Derive nick from pubkey hash
        # Reference uses COMPRESSED pubkey (33 bytes) - the 0x01 suffix indicates compressed
        pubkey_bytes = self._public_key.format(compressed=True)
        pubkey_hex = binascii.hexlify(pubkey_bytes)
        nick_pkh_raw = hashlib.sha256(pubkey_hex).digest()[:NICK_HASH_LENGTH]
        nick_pkh = base58_encode(nick_pkh_raw)
        nick_pkh += "O" * (NICK_MAX_ENCODED - len(nick_pkh))
        self._nick = f"J{version}{nick_pkh}"

    @property
    def nick(self) -> str:
        """The JoinMarket nick (e.g., J5xxx...)."""
        return self._nick

    @property
    def public_key_hex(self) -> str:
        """Public key as hex string (compressed, 33 bytes)."""
        return self._public_key.format(compressed=True).hex()

    def sign_message(self, message: str, hostid: str = "") -> str:
        """
        Sign a message for transmission using Bitcoin message signing format.

        Args:
            message: The message content (without pubkey/sig)
            hostid: Directory server hostid (appended to message before signing)

        Returns:
            Signed message string: "<message> <pubkey_hex> <sig_base64>"
        """
        # Message to sign is message + hostid (as per reference implementation)
        msg_to_sign = message + hostid

        # Hash using Bitcoin message format (double SHA256 with prefix)
        msg_hash = bitcoin_message_hash(msg_to_sign)

        # Sign the pre-hashed message (raw signature, no additional hashing)
        signature = self._private_key.sign(msg_hash, hasher=None)

        # Encode signature as base64
        sig_b64 = base64.b64encode(signature).decode("ascii")

        return f"{message} {self.public_key_hex} {sig_b64}"


class KeyPair:
    def __init__(self, private_key: PrivateKey | None = None):
        if private_key is None:
            private_key = PrivateKey()
        self._private_key = private_key
        self._public_key = private_key.public_key

    @property
    def private_key(self) -> PrivateKey:
        return self._private_key

    @property
    def public_key(self) -> PublicKey:
        return self._public_key

    def sign(self, message: bytes) -> bytes:
        """Sign a message with SHA256 hashing."""
        return self._private_key.sign(message)

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            return self._public_key.verify(signature, message)
        except Exception:
            return False

    def public_key_bytes(self) -> bytes:
        return self._public_key.format(compressed=True)

    def public_key_hex(self) -> str:
        return self.public_key_bytes().hex()


def verify_signature(public_key_hex: str, message: bytes, signature: bytes) -> bool:
    try:
        public_key_bytes = bytes.fromhex(public_key_hex)
        return coincurve_verify(signature, message, public_key_bytes)
    except Exception:
        return False


def strip_signature_padding(signature: bytes) -> bytes:
    """
    Strip padding bytes from a DER signature.

    The reference JoinMarket implementation pads signatures to 72 bytes using
    rjust(72, b'\\xff'). This function finds the DER header (0x30) and strips
    any leading padding bytes.

    Args:
        signature: Possibly padded DER signature

    Returns:
        DER signature with padding removed
    """
    try:
        # Find the DER sequence header (0x30)
        idx = signature.index(b"\x30")
        return signature[idx:]
    except ValueError:
        # No 0x30 found, try stripping trailing zeros (our legacy format)
        return signature.rstrip(b"\x00")


def verify_raw_ecdsa(message_hash: bytes, signature_der: bytes, pubkey_bytes: bytes) -> bool:
    """
    Verify an ECDSA signature on a pre-hashed message.

    Args:
        message_hash: The message hash (already SHA256'd)
        signature_der: DER-encoded signature (may have leading 0xff padding or trailing zeros)
        pubkey_bytes: Compressed public key (33 bytes)

    Returns:
        True if signature is valid
    """
    try:
        # Strip padding from signature (handles both leading 0xff and trailing 0x00)
        sig = strip_signature_padding(signature_der)
        if len(sig) == 0:
            return False

        # Use PublicKey.verify with hasher=None for raw verification
        pubkey = PublicKey(pubkey_bytes)
        return pubkey.verify(sig, message_hash, hasher=None)
    except Exception:
        return False


def get_cert_msg(cert_pub: bytes, cert_expiry: int) -> bytes:
    """
    Get the binary certificate message for signing/verification.

    This is the format used by the reference implementation:
    b'fidelity-bond-cert|' + cert_pub + b'|' + str(cert_expiry).encode('ascii')

    Args:
        cert_pub: Certificate public key (33 bytes)
        cert_expiry: Certificate expiry (encoded as cert_expiry_encoded, NOT multiplied by 2016)

    Returns:
        Message bytes for signing
    """
    return b"fidelity-bond-cert|" + cert_pub + b"|" + str(cert_expiry).encode("ascii")


def get_ascii_cert_msg(cert_pub: bytes, cert_expiry: int) -> bytes:
    """
    Get the ASCII certificate message for signing/verification.

    This is an alternative format where the pubkey is hex-encoded:
    b'fidelity-bond-cert|' + hexlify(cert_pub) + b'|' + str(cert_expiry).encode('ascii')

    Args:
        cert_pub: Certificate public key (33 bytes)
        cert_expiry: Certificate expiry (encoded as cert_expiry_encoded, NOT multiplied by 2016)

    Returns:
        Message bytes for signing
    """
    return (
        b"fidelity-bond-cert|"
        + binascii.hexlify(cert_pub)
        + b"|"
        + str(cert_expiry).encode("ascii")
    )


def verify_bitcoin_message_signature(message: bytes, signature: bytes, pubkey_bytes: bytes) -> bool:
    """
    Verify an ECDSA signature using Bitcoin message signing format.

    The message is hashed using Bitcoin's message signing format:
    SHA256(SHA256("\\x18Bitcoin Signed Message:\\n" + varint(len) + message))

    Args:
        message: Raw message bytes (NOT pre-hashed)
        signature: DER-encoded signature (may have padding)
        pubkey_bytes: Compressed public key (33 bytes)

    Returns:
        True if signature is valid
    """
    try:
        msg_hash = bitcoin_message_hash_bytes(message)
        return verify_raw_ecdsa(msg_hash, signature, pubkey_bytes)
    except Exception:
        return False


def verify_fidelity_bond_proof(
    proof_base64: str,
    maker_nick: str,
    taker_nick: str,
) -> tuple[bool, dict[str, str | int | bytes] | None, str]:
    """
    Verify a fidelity bond proof by checking both signatures.

    The proof structure (252 bytes total):
    - 72 bytes: Nick signature (signs "taker_nick|maker_nick" with Bitcoin message format)
    - 72 bytes: Certificate signature (signs cert message with Bitcoin message format)
    - 33 bytes: Certificate public key
    - 2 bytes: Certificate expiry (blocks / 2016)
    - 33 bytes: UTXO public key
    - 32 bytes: TXID (little-endian)
    - 4 bytes: Vout (little-endian)
    - 4 bytes: Locktime (little-endian)

    The nick signature message format is:
        (taker_nick + '|' + maker_nick).encode('ascii')

    The certificate signature message has two valid formats:
    1. Binary: b'fidelity-bond-cert|' + cert_pub + b'|' + str(cert_expiry).encode('ascii')
    2. ASCII: b'fidelity-bond-cert|' + hexlify(cert_pub) + b'|' + str(cert_expiry).encode('ascii')

    Both signatures use Bitcoin message signing format (double SHA256 with prefix).

    Args:
        proof_base64: Base64-encoded bond proof
        maker_nick: Maker's JoinMarket nick
        taker_nick: Taker's nick (the verifier)

    Returns:
        Tuple of (is_valid, bond_data, error_message)
        bond_data contains parsed fields if successful
    """
    import struct

    try:
        decoded_data = base64.b64decode(proof_base64)
    except Exception as e:
        return False, None, f"Failed to decode base64: {e}"

    if len(decoded_data) != 252:
        return False, None, f"Invalid proof length: {len(decoded_data)}, expected 252"

    try:
        # Unpack the proof structure
        unpacked = struct.unpack("<72s72s33sH33s32sII", decoded_data)

        nick_sig = unpacked[0]  # 72 bytes (padded DER signature)
        cert_sig = unpacked[1]  # 72 bytes (padded DER signature)
        cert_pub = unpacked[2]  # 33 bytes
        cert_expiry_encoded = unpacked[3]  # 2 bytes (blocks / 2016)
        utxo_pub = unpacked[4]  # 33 bytes
        txid = unpacked[5]  # 32 bytes (little-endian)
        vout = unpacked[6]  # 4 bytes
        locktime = unpacked[7]  # 4 bytes

        cert_expiry = cert_expiry_encoded * 2016

        # Strip leading 0xff padding from signatures (reference impl uses rjust with 0xff)
        try:
            nick_sig_stripped = nick_sig[nick_sig.index(b"\x30") :]
        except ValueError:
            return False, None, "Nick signature DER header not found"

        try:
            cert_sig_stripped = cert_sig[cert_sig.index(b"\x30") :]
        except ValueError:
            return False, None, "Certificate signature DER header not found"

        # 1. Verify nick signature
        # The nick signature proves the maker controls the certificate key
        # It signs "(taker_nick|maker_nick)" with the certificate private key
        nick_msg = (taker_nick + "|" + maker_nick).encode("ascii")

        if not verify_bitcoin_message_signature(nick_msg, nick_sig_stripped, cert_pub):
            return False, None, "Nick signature verification failed"

        # 2. Verify certificate signature
        # The certificate is signed by the UTXO key
        # It signs the cert message (two formats supported for compatibility)
        cert_msg = get_cert_msg(cert_pub, cert_expiry_encoded)
        ascii_cert_msg = get_ascii_cert_msg(cert_pub, cert_expiry_encoded)

        if not verify_bitcoin_message_signature(
            cert_msg, cert_sig_stripped, utxo_pub
        ) and not verify_bitcoin_message_signature(ascii_cert_msg, cert_sig_stripped, utxo_pub):
            return False, None, "Certificate signature verification failed"

        # Both signatures are valid
        bond_data = {
            "utxo_txid": txid.hex(),
            "utxo_vout": vout,
            "locktime": locktime,
            "utxo_pub": utxo_pub.hex(),
            "cert_pub": cert_pub.hex(),
            "cert_expiry": cert_expiry,
            "maker_nick": maker_nick,
            "taker_nick": taker_nick,
        }

        return True, bond_data, ""

    except Exception as e:
        return False, None, f"Failed to verify bond proof: {e}"
