#!/usr/bin/env python3
"""
Generate Tor v3 hidden service keys.

This script generates the Ed25519 keypair required for a Tor v3 hidden service.
The keys can be used to create a deterministic .onion address.

Output files:
    - hs_ed25519_secret_key: 64-byte secret key (with Tor header)
    - hs_ed25519_public_key: 32-byte public key (with Tor header)
    - hostname: The .onion address

Usage:
    python generate_tor_keys.py [output_dir]

Example:
    python generate_tor_keys.py ./tor_keys
    # Creates: ./tor_keys/hs_ed25519_secret_key
    #          ./tor_keys/hs_ed25519_public_key
    #          ./tor_keys/hostname

Files are created owner-only (0600). Existing key files are never overwritten.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path

# Try cryptography first (more common), fall back to nacl
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        PublicFormat,
    )

    def generate_keypair() -> tuple[bytes, bytes]:
        """Generate Ed25519 keypair using cryptography library."""
        private_key = Ed25519PrivateKey.generate()
        # Get raw private key bytes (seed, 32 bytes)
        private_bytes = private_key.private_bytes(
            encoding=Encoding.Raw,
            format=PrivateFormat.Raw,
            encryption_algorithm=NoEncryption(),
        )
        # Get raw public key bytes (32 bytes)
        public_bytes = private_key.public_key().public_bytes(
            encoding=Encoding.Raw,
            format=PublicFormat.Raw,
        )
        return private_bytes, public_bytes

except ImportError:
    try:
        import nacl.signing

        def generate_keypair() -> tuple[bytes, bytes]:
            """Generate Ed25519 keypair using PyNaCl."""
            signing_key = nacl.signing.SigningKey.generate()
            return bytes(signing_key), bytes(signing_key.verify_key)

    except ImportError:
        print("Error: Neither 'cryptography' nor 'PyNaCl' is installed.")
        print("Install one of: pip install cryptography OR pip install pynacl")
        sys.exit(1)


def expand_ed25519_key(seed: bytes) -> bytes:
    """
    Expand a 32-byte Ed25519 seed to 64-byte expanded secret key.

    Tor uses the expanded form where the first 32 bytes are the
    clamped hash and the last 32 bytes are the prefix for signing.
    """
    h = hashlib.sha512(seed).digest()
    # Clamp the first 32 bytes as per Ed25519 spec
    h_list = list(h)
    h_list[0] &= 248
    h_list[31] &= 127
    h_list[31] |= 64
    return bytes(h_list)


def compute_onion_address(public_key: bytes) -> str:
    """
    Compute the v3 onion address from the public key.

    The address is: base32(public_key || checksum || version)
    where checksum = sha3_256(".onion checksum" || public_key || version)[:2]
    and version = 0x03
    """
    version = b"\x03"
    checksum_prefix = b".onion checksum"
    checksum_data = checksum_prefix + public_key + version
    checksum = hashlib.sha3_256(checksum_data).digest()[:2]
    address_bytes = public_key + checksum + version
    address = base64.b32encode(address_bytes).decode().lower()
    return f"{address}.onion"


def create_tor_secret_key_file(expanded_key: bytes) -> bytes:
    """Create the hs_ed25519_secret_key file content with Tor header."""
    # Tor uses a specific header for the secret key file
    # Format: "== ed25519v1-secret: type0 ==\x00\x00\x00" (32 bytes) + expanded_key (64 bytes)
    header = b"== ed25519v1-secret: type0 ==\x00\x00\x00"
    return header + expanded_key


def create_tor_public_key_file(public_key: bytes) -> bytes:
    """Create the hs_ed25519_public_key file content with Tor header."""
    # Format: "== ed25519v1-public: type0 ==\x00\x00\x00" (32 bytes) + public_key (32 bytes)
    header = b"== ed25519v1-public: type0 ==\x00\x00\x00"
    return header + public_key


def write_new_private_file(path: Path, content: bytes) -> None:
    """Create ``path`` with mode 0600, refusing existing files and symlinks.

    The mode is applied at creation, so the key is never readable by other
    users, and O_EXCL plus O_NOFOLLOW never write through a planted link.
    """
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())


def main() -> int:
    """Generate Tor hidden service keys."""
    # Determine output directory
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path.cwd()

    # A new directory is owner-only. An existing one is left as the caller set it;
    # the files themselves are created private either way.
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    secret_key_path = output_dir / "hs_ed25519_secret_key"
    public_key_path = output_dir / "hs_ed25519_public_key"
    hostname_path = output_dir / "hostname"
    existing = [
        p
        for p in (secret_key_path, public_key_path, hostname_path)
        if os.path.lexists(p)
    ]
    if existing:
        print(
            "Error: refusing to overwrite existing files (an onion identity may be lost):"
        )
        for path in existing:
            print(f"  {path}")
        print("Choose an empty output directory or move these files away first.")
        return 1

    # Generate keypair
    print("Generating Ed25519 keypair...")
    seed, public_key = generate_keypair()

    # Expand the seed to 64-byte secret key
    expanded_key = expand_ed25519_key(seed)

    # Compute onion address
    onion_address = compute_onion_address(public_key)

    # Create file contents
    secret_key_content = create_tor_secret_key_file(expanded_key)
    public_key_content = create_tor_public_key_file(public_key)

    # This is a key generation script - writing the secret key is the intended behavior
    write_new_private_file(
        secret_key_path, secret_key_content
    )  # codeql[py/clear-text-storage-sensitive-data]
    write_new_private_file(public_key_path, public_key_content)
    write_new_private_file(hostname_path, f"{onion_address}\n".encode())

    print(f"Generated Tor hidden service keys in: {output_dir}")
    print(f"Onion address: {onion_address}")
    print()
    print("Files created:")
    print(f"  {secret_key_path}")
    print(f"  {public_key_path}")
    print(f"  {hostname_path}")
    print()
    print(
        "To use with Docker, set TOR_HS_ED25519_SECRET_KEY_BASE64 environment variable:"
    )
    # This is a key generation script - outputting the key is the intended behavior
    # codeql[py/clear-text-logging-sensitive-data]
    print(
        f"  export TOR_HS_ED25519_SECRET_KEY_BASE64='{base64.b64encode(secret_key_content).decode()}'"
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
