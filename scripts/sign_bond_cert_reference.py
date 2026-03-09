#!/usr/bin/env python3
"""Sign a fidelity bond certificate using a BIP39 mnemonic (for migration).

This script signs a joinmarket-ng fidelity bond certificate message using the
private key derived from a BIP39 mnemonic at the fidelity bond derivation path
``m/84'/0'/0'/2/<timenumber>``.

This is the recommended migration path for users coming from the reference
JoinMarket implementation (``joinmarket-clientserver``) who have a hot wallet.
The reference implementation's ``wallet-tool.py signmessage`` command has a
bug (it cannot sign messages with fidelity bond paths because
``BTC_Timelocked_P2WSH`` does not override the inherited ``sign_message()``
method -- the private key is returned as a ``(key_bytes, locktime)`` tuple
but ``sign_message()`` expects raw bytes).

This script works around that bug by deriving the private key directly from
the BIP39 mnemonic (the same seed words used when creating the reference
wallet), computing the correct BIP32 path for the bond, and signing the
certificate message in the Electrum recoverable format that ``import-certificate``
expects.

This script is FULLY SELF-CONTAINED -- it does not import from jmcore or
jmwallet.  The only external dependency is ``coincurve``.

USAGE:
  python scripts/sign_bond_cert_reference.py \\
      --locktime 2026-02 \\
      --cert-pubkey <hex> \\
      --cert-expiry <period_number>

  # With BIP39 passphrase:
  python scripts/sign_bond_cert_reference.py \\
      --locktime 2026-02 \\
      --cert-pubkey <hex> \\
      --cert-expiry <period_number> \\
      --passphrase

REQUIREMENTS:
  pip install coincurve

WORKFLOW:
  1. Run ``jm-wallet generate-hot-keypair`` to get the cert pubkey and privkey
  2. Run ``jm-wallet prepare-certificate-message`` to get the cert expiry
  3. Run this script with the bond locktime, cert pubkey, and cert expiry
  4. Import the signature: ``jm-wallet import-certificate <bond_address> \\
       --cert-signature '<base64_signature>' --cert-expiry <period_number>``
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import struct
import sys

# secp256k1 curve order -- used for BIP32 child key derivation
SECP256K1_N = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16
)

# Timenumber constants (matches reference implementation)
TIMELOCK_EPOCH_YEAR = 2020
TIMELOCK_EPOCH_MONTH = 1
MONTHS_IN_YEAR = 12
TIMENUMBER_COUNT = 960  # 80 years * 12 months (Jan 2020 - Dec 2099)


# ---------------------------------------------------------------------------
# Timenumber calculation
# ---------------------------------------------------------------------------


def locktime_to_timenumber(year: int, month: int) -> int:
    """Convert year/month to JoinMarket timenumber index (0-959)."""
    if month < 1 or month > 12:
        raise ValueError(f"Month must be 1-12, got {month}")
    timenumber = (year - TIMELOCK_EPOCH_YEAR) * MONTHS_IN_YEAR + (
        month - TIMELOCK_EPOCH_MONTH
    )
    if timenumber < 0 or timenumber >= TIMENUMBER_COUNT:
        raise ValueError(
            f"Locktime {year}-{month:02d} is outside the valid range "
            f"(2020-01 through 2099-12), timenumber={timenumber}"
        )
    return timenumber


# ---------------------------------------------------------------------------
# Bitcoin message hashing
# ---------------------------------------------------------------------------


def _bitcoin_message_hash(message: str) -> bytes:
    """Compute the Bitcoin message hash (double SHA-256 with prefix).

    Format: SHA256(SHA256("\\x18Bitcoin Signed Message:\\n" + varint(len) + message))
    """
    prefix = b"\x18Bitcoin Signed Message:\n"
    msg_bytes = message.encode("utf-8")
    msg_len = len(msg_bytes)
    if msg_len < 253:
        varint = bytes([msg_len])
    elif msg_len < 0x10000:
        varint = b"\xfd" + msg_len.to_bytes(2, "little")
    elif msg_len < 0x100000000:
        varint = b"\xfe" + msg_len.to_bytes(4, "little")
    else:
        varint = b"\xff" + msg_len.to_bytes(8, "little")
    full_msg = prefix + varint + msg_bytes
    return hashlib.sha256(hashlib.sha256(full_msg).digest()).digest()


# ---------------------------------------------------------------------------
# BIP32 key derivation (inline, same as sign_bond_mnemonic.py)
# ---------------------------------------------------------------------------


def _derive_key_from_mnemonic(
    mnemonic: str,
    path_indices: list[int],
    passphrase: str = "",
) -> tuple[bytes, bytes]:
    """Derive a private key and public key from a BIP39 mnemonic.

    Args:
        mnemonic: BIP39 mnemonic phrase (12 or 24 words).
        path_indices: BIP32 path as list of uint32 indices.
        passphrase: Optional BIP39 passphrase.

    Returns:
        Tuple of (private_key_bytes, compressed_public_key_bytes).
    """
    from coincurve import PrivateKey

    # BIP39: mnemonic -> seed (PBKDF2-HMAC-SHA512, 2048 rounds)
    mnemonic_bytes = mnemonic.encode("utf-8")
    salt = ("mnemonic" + passphrase).encode("utf-8")
    seed = hashlib.pbkdf2_hmac("sha512", mnemonic_bytes, salt, 2048)

    # BIP32: seed -> master key
    master_hmac = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    key_bytes = master_hmac[:32]
    chain_code = master_hmac[32:]

    # Derive child keys along the path
    for index in path_indices:
        hardened = index >= 0x80000000
        if hardened:
            data = b"\x00" + key_bytes + struct.pack(">I", index)
        else:
            pubkey = PrivateKey(key_bytes).public_key.format(compressed=True)
            data = pubkey + struct.pack(">I", index)

        child_hmac = hmac.new(chain_code, data, hashlib.sha512).digest()
        child_key_offset = int.from_bytes(child_hmac[:32], "big")
        parent_key_int = int.from_bytes(key_bytes, "big")
        child_key_int = (parent_key_int + child_key_offset) % SECP256K1_N

        key_bytes = child_key_int.to_bytes(32, "big")
        chain_code = child_hmac[32:]

    privkey = PrivateKey(key_bytes)
    pubkey = privkey.public_key.format(compressed=True)
    return key_bytes, pubkey


def _make_bond_path(timenumber: int) -> list[int]:
    """Build the BIP32 path indices for m/84'/0'/0'/2/<timenumber>."""
    return [
        84 + 0x80000000,  # 84'
        0 + 0x80000000,  # 0'
        0 + 0x80000000,  # 0'
        2,  # fidelity bond branch (unhardened)
        timenumber,  # locktime child (unhardened)
    ]


def _path_to_string(indices: list[int]) -> str:
    """Convert BIP32 uint32 indices to human-readable path string."""
    parts = ["m"]
    for idx in indices:
        if idx >= 0x80000000:
            parts.append(f"{idx - 0x80000000}'")
        else:
            parts.append(str(idx))
    return "/".join(parts)


# ---------------------------------------------------------------------------
# Recoverable signature
# ---------------------------------------------------------------------------


def sign_certificate(
    private_key_bytes: bytes,
    cert_pubkey_hex: str,
    cert_expiry: int,
) -> str:
    """Sign the certificate message and return base64-encoded recoverable sig.

    The certificate message format is:
        fidelity-bond-cert|<cert_pubkey_hex>|<cert_expiry>

    The signature is in Electrum recoverable format (65 bytes):
        header(1) + R(32) + S(32)

    The header byte encodes compressed P2PKH (offset 31) plus the recovery ID.
    This format is accepted by ``jm-wallet import-certificate``.

    Args:
        private_key_bytes: 32-byte private key of the bond UTXO.
        cert_pubkey_hex: Hex-encoded compressed certificate pubkey.
        cert_expiry: Certificate expiry period number.

    Returns:
        Base64-encoded 65-byte recoverable signature.
    """
    from coincurve import PrivateKey

    cert_msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{cert_expiry}"
    msg_hash = _bitcoin_message_hash(cert_msg)

    privkey = PrivateKey(private_key_bytes)
    # sign_recoverable returns R(32) + S(32) + recovery_id(1)
    sig = privkey.sign_recoverable(msg_hash, hasher=None)

    r = sig[0:32]
    s = sig[32:64]
    recovery_id = sig[64]

    # Electrum format: header(1) + R(32) + S(32)
    # Compressed P2PKH: header offset 31 + recovery_id
    header = 31 + recovery_id
    electrum_sig = bytes([header]) + r + s

    return base64.b64encode(electrum_sig).decode("ascii")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Sign a fidelity bond certificate using a BIP39 mnemonic. "
            "For migration from the reference JoinMarket implementation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # Sign a certificate for a bond locked until Feb 2026:
  %(prog)s --locktime 2026-02 --cert-pubkey 03abcd... --cert-expiry 518

  # With BIP39 passphrase:
  %(prog)s --locktime 2026-02 --cert-pubkey 03abcd... --cert-expiry 518 --passphrase

workflow:
  1. jm-wallet generate-hot-keypair
  2. jm-wallet prepare-certificate-message <bond_address>
  3. %(prog)s --locktime YYYY-MM --cert-pubkey <hex> --cert-expiry <N>
  4. jm-wallet import-certificate <bond_address> \\
       --cert-signature '<base64_sig>' --cert-expiry <N>
""",
    )
    parser.add_argument(
        "--locktime",
        required=True,
        help="Bond locktime as YYYY-MM (e.g. 2026-02 for February 2026)",
    )
    parser.add_argument(
        "--cert-pubkey",
        required=True,
        help="Certificate public key hex (from generate-hot-keypair)",
    )
    parser.add_argument(
        "--cert-expiry",
        required=True,
        type=int,
        help="Certificate expiry period number (from prepare-certificate-message)",
    )
    parser.add_argument(
        "--passphrase",
        action="store_true",
        help="Prompt for a BIP39 passphrase (default: no passphrase)",
    )

    args = parser.parse_args()

    # Parse locktime
    try:
        parts = args.locktime.split("-")
        if len(parts) != 2:
            raise ValueError("Expected YYYY-MM format")
        year = int(parts[0])
        month = int(parts[1])
    except (ValueError, IndexError) as e:
        print(f"Error: Invalid locktime format '{args.locktime}': {e}", file=sys.stderr)
        print("Expected format: YYYY-MM (e.g. 2026-02)", file=sys.stderr)
        sys.exit(1)

    try:
        timenumber = locktime_to_timenumber(year, month)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Validate cert pubkey
    cert_pubkey_hex = args.cert_pubkey.strip()
    try:
        cert_pubkey_bytes = bytes.fromhex(cert_pubkey_hex)
        if len(cert_pubkey_bytes) != 33:
            raise ValueError("Certificate pubkey must be 33 bytes (compressed)")
        if cert_pubkey_bytes[0] not in (0x02, 0x03):
            raise ValueError("Invalid compressed pubkey prefix")
    except ValueError as e:
        print(f"Error: Invalid certificate pubkey: {e}", file=sys.stderr)
        sys.exit(1)

    path_indices = _make_bond_path(timenumber)
    path_str = _path_to_string(path_indices)

    print(f"Bond locktime:   {year}-{month:02d}", file=sys.stderr)
    print(f"Timenumber:      {timenumber}", file=sys.stderr)
    print(f"Derivation path: {path_str}", file=sys.stderr)
    print(f"Cert pubkey:     {cert_pubkey_hex}", file=sys.stderr)
    print(f"Cert expiry:     {args.cert_expiry}", file=sys.stderr)
    print(file=sys.stderr)

    # Read mnemonic securely
    print("Enter your BIP39 mnemonic (12 or 24 words):", file=sys.stderr)
    print("(input is hidden)", file=sys.stderr)
    mnemonic = getpass.getpass(prompt="> ")

    if not mnemonic.strip():
        print("Error: Empty mnemonic", file=sys.stderr)
        sys.exit(1)

    words = mnemonic.strip().split()
    if len(words) not in (12, 15, 18, 21, 24):
        print(f"Error: Expected 12-24 words, got {len(words)}", file=sys.stderr)
        sys.exit(1)

    # Optional passphrase
    passphrase = ""
    if args.passphrase:
        passphrase = getpass.getpass(prompt="BIP39 passphrase: ")

    # Derive key
    print(f"\nDeriving key from {path_str}...", file=sys.stderr)
    try:
        privkey_bytes, pubkey_bytes = _derive_key_from_mnemonic(
            mnemonic.strip(), path_indices, passphrase
        )
    except Exception as e:
        print(f"Error: Key derivation failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Clear mnemonic from memory (best-effort)
    mnemonic = "x" * len(mnemonic)  # noqa: F841
    del mnemonic

    print(f"Bond pubkey:     {pubkey_bytes.hex()}", file=sys.stderr)

    # Sign certificate
    print("Signing certificate...", file=sys.stderr)
    try:
        signature_b64 = sign_certificate(
            privkey_bytes, cert_pubkey_hex, args.cert_expiry
        )
    except Exception as e:
        print(f"Error: Signing failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        privkey_bytes = b"\x00" * 32  # noqa: F841
        del privkey_bytes

    # Output
    cert_msg = f"fidelity-bond-cert|{cert_pubkey_hex}|{args.cert_expiry}"
    print(file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print("CERTIFICATE SIGNATURE (base64):", file=sys.stderr)
    print("=" * 80, file=sys.stderr)
    print(signature_b64)  # stdout -- can be piped
    print("=" * 80, file=sys.stderr)
    print(file=sys.stderr)
    print("Certificate message:", file=sys.stderr)
    print(f"  {cert_msg}", file=sys.stderr)
    print(file=sys.stderr)
    print("Import with:", file=sys.stderr)
    print(
        f"  jm-wallet import-certificate <bond_address> \\\n"
        f"    --cert-signature '{signature_b64}' \\\n"
        f"    --cert-expiry {args.cert_expiry}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
