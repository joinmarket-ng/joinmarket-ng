#!/usr/bin/env python3
"""Derive the fidelity bond public key from a JoinMarket fidelity bond xpub.

This script extracts the compressed public key needed by ``create-bond-address``
from the fidelity bond extended public key shown by the reference JoinMarket
implementation's ``wallet-tool.py display`` command.

The reference implementation uses a non-standard BIP32 derivation path for
fidelity bonds: ``m/84'/0'/0'/2/<timenumber>`` where ``timenumber`` is a
monthly index counting from January 2020 (timenumber 0) through December 2099
(timenumber 959).  Branch ``/2`` is the fidelity bond branch and both it and
the timenumber child are unhardened, so the pubkey can be derived from the
account-level xpub without the mnemonic.

The fidelity bond xpub is displayed by ``wallet-tool.py display`` on the line
labelled ``fbonds-mpk-xpub...`` for mixdepth 0, or on the sub-header line for
the ``m/84'/0'/0'/2`` internal branch.  Both are the same key -- the account
xpub at ``m/84'/0'/0'``.

This script is FULLY SELF-CONTAINED -- it does not import from jmcore or
jmwallet.  The only external dependency is ``coincurve`` (for secp256k1 point
operations).

USAGE:
  # From the fbonds-mpk line (same as account xpub):
  python scripts/derive_bond_pubkey.py \\
      --xpub xpub6Cat... \\
      --locktime 2026-02

  # From the /2 branch sub-header (this is the /2 child xpub):
  python scripts/derive_bond_pubkey.py \\
      --xpub xpub6FPn... \\
      --locktime 2026-02 \\
      --branch-xpub

  # Quick: just show the timenumber for a locktime
  python scripts/derive_bond_pubkey.py --locktime 2026-02 --info

REQUIREMENTS:
  pip install coincurve

OUTPUT:
  Prints the 33-byte compressed public key hex, ready to paste into:
    jm-wallet create-bond-address <pubkey_hex> --locktime-date YYYY-MM
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import struct
import sys

# ---------------------------------------------------------------------------
# Timenumber calculation (matches reference implementation)
# ---------------------------------------------------------------------------

TIMELOCK_EPOCH_YEAR = 2020
TIMELOCK_EPOCH_MONTH = 1  # January
MONTHS_IN_YEAR = 12
TIMENUMBER_COUNT = 960  # 80 years * 12 months (Jan 2020 - Dec 2099)


def locktime_to_timenumber(year: int, month: int) -> int:
    """Convert a year/month to the JoinMarket timenumber index.

    The timenumber is the BIP32 child index under the ``/2`` fidelity bond
    branch.  Timenumber 0 = January 2020, timenumber 1 = February 2020, etc.

    Args:
        year: Full year (e.g. 2026)
        month: Month 1-12

    Returns:
        Timenumber (0-959)

    Raises:
        ValueError: If the date is outside the valid range
    """
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


def timenumber_to_locktime(timenumber: int) -> tuple[int, int]:
    """Convert a timenumber back to year/month.

    Args:
        timenumber: Index 0-959

    Returns:
        Tuple of (year, month)
    """
    year = TIMELOCK_EPOCH_YEAR + timenumber // MONTHS_IN_YEAR
    month = TIMELOCK_EPOCH_MONTH + timenumber % MONTHS_IN_YEAR
    return year, month


def timenumber_to_timestamp(timenumber: int) -> int:
    """Convert a timenumber to a Unix timestamp (first second of the month).

    Args:
        timenumber: Index 0-959

    Returns:
        Unix timestamp
    """
    from calendar import timegm
    from datetime import datetime

    year, month = timenumber_to_locktime(timenumber)
    return timegm(datetime(year, month, 1, 0, 0, 0, 0).timetuple())


# ---------------------------------------------------------------------------
# Base58 / xpub deserialization
# ---------------------------------------------------------------------------

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _base58_decode(s: str) -> bytes:
    """Decode a Base58Check encoded string to bytes (without checksum)."""
    n = 0
    for c in s:
        idx = BASE58_ALPHABET.index(c)
        n = n * 58 + idx

    # Convert to bytes
    result = []
    while n > 0:
        result.append(n & 0xFF)
        n >>= 8
    result.reverse()

    # Preserve leading zeros
    pad = 0
    for c in s:
        if c == "1":
            pad += 1
        else:
            break

    raw = bytes(pad) + bytes(result)

    # Verify checksum (last 4 bytes)
    payload = raw[:-4]
    checksum = raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if checksum != expected:
        raise ValueError("Invalid Base58Check checksum")

    return payload


def parse_xpub(xpub_str: str) -> tuple[bytes, bytes, int, int]:
    """Deserialize a BIP32 extended public key (xpub/tpub).

    Format (78 bytes):
      4 bytes: version (0x0488B21E for xpub, 0x043587CF for tpub)
      1 byte:  depth
      4 bytes: parent fingerprint
      4 bytes: child number
      32 bytes: chain code
      33 bytes: compressed public key

    Args:
        xpub_str: Base58Check encoded extended public key

    Returns:
        Tuple of (public_key_33bytes, chain_code_32bytes, depth, child_number)
    """
    data = _base58_decode(xpub_str)

    if len(data) != 78:
        raise ValueError(f"Invalid xpub length: {len(data)}, expected 78")

    version = data[0:4]
    depth = data[4]
    # parent_fingerprint = data[5:9]
    child_number = struct.unpack(">I", data[9:13])[0]
    chain_code = data[13:45]
    key_data = data[45:78]

    # Version check
    if version not in (b"\x04\x88\xb2\x1e", b"\x04\x35\x87\xcf"):
        raise ValueError(
            f"Unknown xpub version: {version.hex()}. "
            f"Expected 0488b21e (xpub) or 043587cf (tpub)"
        )

    if key_data[0] not in (0x02, 0x03):
        raise ValueError(
            f"Invalid public key prefix byte: 0x{key_data[0]:02x}. "
            f"Expected 0x02 or 0x03 (compressed public key)"
        )

    return key_data, chain_code, depth, child_number


# ---------------------------------------------------------------------------
# BIP32 public child key derivation
# ---------------------------------------------------------------------------


def _point_add(pubkey1: bytes, pubkey2: bytes) -> bytes:
    """Add two compressed public key points on secp256k1.

    Uses coincurve for the actual EC math.

    Args:
        pubkey1: 33-byte compressed public key
        pubkey2: 33-byte compressed public key

    Returns:
        33-byte compressed public key (sum)
    """
    from coincurve import PublicKey

    pk1 = PublicKey(pubkey1)
    result = pk1.combine([PublicKey(pubkey2)])
    return result.format(compressed=True)


def _scalar_to_pubkey(scalar: bytes) -> bytes:
    """Derive a compressed public key from a 32-byte scalar (multiply by G).

    Args:
        scalar: 32-byte big-endian integer

    Returns:
        33-byte compressed public key
    """
    from coincurve import PrivateKey

    return PrivateKey(scalar).public_key.format(compressed=True)


def derive_child_pubkey(
    parent_pubkey: bytes,
    parent_chain_code: bytes,
    index: int,
) -> tuple[bytes, bytes]:
    """Derive a child public key from a parent public key (BIP32 CKD_pub).

    Only supports unhardened derivation (index < 0x80000000).

    Args:
        parent_pubkey: 33-byte compressed parent public key
        parent_chain_code: 32-byte parent chain code
        index: Child index (must be < 0x80000000 for unhardened)

    Returns:
        Tuple of (child_pubkey_33bytes, child_chain_code_32bytes)

    Raises:
        ValueError: If index is hardened (>= 0x80000000)
    """
    if index >= 0x80000000:
        raise ValueError(
            f"Cannot derive hardened child from xpub (index={index}). "
            f"Hardened derivation requires the private key."
        )

    # CKD_pub: HMAC-SHA512(chain_code, compressed_pubkey + index_bytes)
    data = parent_pubkey + struct.pack(">I", index)
    child_hmac = hmac.new(parent_chain_code, data, hashlib.sha512).digest()

    child_offset = child_hmac[:32]
    child_chain_code = child_hmac[32:]

    # Child pubkey = point(child_offset) + parent_pubkey
    offset_pubkey = _scalar_to_pubkey(child_offset)
    child_pubkey = _point_add(parent_pubkey, offset_pubkey)

    return child_pubkey, child_chain_code


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def derive_bond_pubkey(
    xpub_str: str, year: int, month: int, *, branch_xpub: bool
) -> str:
    """Derive the fidelity bond public key for a given locktime.

    Args:
        xpub_str: Extended public key (xpub/tpub) -- either the account xpub
                  (from fbonds-mpk line) or the /2 branch xpub.
        year: Locktime year (e.g. 2026)
        month: Locktime month (1-12)
        branch_xpub: If True, xpub is already the /2 branch child.
                     If False, xpub is the account key and we derive /2 first.

    Returns:
        Compressed public key as hex string (66 chars)
    """
    timenumber = locktime_to_timenumber(year, month)

    pubkey, chain_code, depth, _child_num = parse_xpub(xpub_str)

    if not branch_xpub:
        # Account xpub -> derive /2 (fidelity bond branch)
        pubkey, chain_code = derive_child_pubkey(pubkey, chain_code, 2)

    # Derive /<timenumber> (the locktime-specific child)
    child_pubkey, _child_cc = derive_child_pubkey(pubkey, chain_code, timenumber)

    return child_pubkey.hex()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Derive fidelity bond public key from JoinMarket xpub.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # From the fbonds-mpk line (account xpub at m/84'/0'/0'):
  %(prog)s --xpub xpub6Cat... --locktime 2026-02

  # From the /2 branch sub-header xpub:
  %(prog)s --xpub xpub6FPn... --locktime 2026-02 --branch-xpub

  # Show timenumber info only:
  %(prog)s --locktime 2026-02 --info
""",
    )
    parser.add_argument(
        "--xpub",
        help=(
            "Extended public key from the reference JoinMarket wallet. "
            "Either the account xpub from the 'fbonds-mpk-' line in "
            "'wallet-tool.py display' output, or the /2 branch xpub "
            "shown on the 'internal addresses m/84'/0'/0'/2' sub-header."
        ),
    )
    parser.add_argument(
        "--locktime",
        required=True,
        help="Locktime as YYYY-MM (e.g. 2026-02 for February 2026)",
    )
    parser.add_argument(
        "--branch-xpub",
        action="store_true",
        default=False,
        help=(
            "Set this if the xpub is the /2 branch child (from the "
            "'internal addresses m/84'/0'/0'/2' line) rather than "
            "the account-level xpub (from the 'fbonds-mpk-' line)."
        ),
    )
    parser.add_argument(
        "--info",
        action="store_true",
        default=False,
        help="Just show the timenumber and derivation path, no pubkey derivation.",
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

    timestamp = timenumber_to_timestamp(timenumber)
    path_suffix = f"2/{timenumber}"
    display_path = f"m/84'/0'/0'/2/{timenumber}:{timestamp}"

    if args.info:
        print(f"Locktime:        {year}-{month:02d}")
        print(f"Unix timestamp:  {timestamp}")
        print(f"Timenumber:      {timenumber}")
        print(f"BIP32 path:      m/84'/0'/0'/{path_suffix}")
        print(f"Display path:    {display_path}")
        return

    if not args.xpub:
        print("Error: --xpub is required (unless using --info)", file=sys.stderr)
        sys.exit(1)

    try:
        pubkey_hex = derive_bond_pubkey(
            args.xpub, year, month, branch_xpub=args.branch_xpub
        )
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Output
    print(f"Locktime:        {year}-{month:02d}")
    print(f"Unix timestamp:  {timestamp}")
    print(f"Timenumber:      {timenumber}")
    xpub_type = "branch /2 xpub" if args.branch_xpub else "account xpub"
    print(f"Xpub type:       {xpub_type}")
    print(f"BIP32 path:      m/84'/0'/0'/{path_suffix}")
    print(f"Display path:    {display_path}")
    print()
    print(f"Public key:      {pubkey_hex}")
    print()
    print("To create the bond address in joinmarket-ng:")
    print(
        f"  jm-wallet create-bond-address {pubkey_hex} --locktime-date {year}-{month:02d}"
    )


if __name__ == "__main__":
    main()
