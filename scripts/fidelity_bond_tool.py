#!/usr/bin/env python3
"""
Fidelity Bond Tool - Fetch, parse, and analyze JoinMarket fidelity bond proofs.

This tool provides utilities for working with fidelity bond proofs:
  - Fetch bond proofs from mainnet makers via Tor
  - Parse and validate bond proof structure and signatures
  - Analyze bond proof components (signatures, UTXO, locktime, etc.)

Usage:
  # Fetch bond proof from a mainnet maker
  python scripts/fidelity_bond_tool.py fetch <maker_nick> [--directory <onion>] [--output <file>]

  # Parse and analyze a bond proof
  python scripts/fidelity_bond_tool.py parse <proof_file_or_b64> [--maker <nick>] [--taker <nick>]

  # Fetch and parse in one command
  python scripts/fidelity_bond_tool.py fetch-parse <maker_nick> [--directory <onion>]

Examples:
  # Fetch from known maker
  python scripts/fidelity_bond_tool.py fetch J52jbDvERjd3N4Mr

  # Parse from file
  python scripts/fidelity_bond_tool.py parse /tmp/mainnet_bond_proof.txt

  # Parse from base64 string
  python scripts/fidelity_bond_tool.py parse "//8wRAIg..." --maker J5Maker --taker J5Taker
"""

import argparse
import asyncio
import base64
import json
import struct
import sys
from pathlib import Path

# Add jmcore to path
sys.path.insert(0, str(Path(__file__).parent.parent / "jmcore" / "src"))

from jmcore.crypto import NickIdentity, verify_bitcoin_message_signature
from jmcore.network import connect_via_tor
from jmcore.protocol import MessageType


# ============================================================================
# FETCH: Get bond proof from mainnet maker
# ============================================================================


async def fetch_bond_proof(
    maker_nick: str,
    directory_onion: str = "nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion",
    directory_port: int = 5222,
    output_file: str | None = None,
    timeout: int = 60,
) -> dict | None:
    """
    Fetch bond proof from a mainnet maker.

    Args:
        maker_nick: Nick of the maker to fetch from
        directory_onion: Onion address of directory server
        directory_port: Port of directory server
        output_file: Optional file to save the proof
        timeout: Timeout in seconds

    Returns:
        Dict with maker_nick, taker_nick, proof, or None if failed
    """
    print(f"Connecting to directory {directory_onion}:{directory_port} via Tor...")

    # Create our identity
    nick_identity = NickIdentity()
    our_nick = nick_identity.nick
    print(f"Our nick: {our_nick}")

    try:
        # Connect via Tor
        conn = await connect_via_tor(
            onion_address=directory_onion,
            port=directory_port,
            socks_host="127.0.0.1",
            socks_port=9050,
            max_message_size=2097152,
            timeout=60.0,
        )
        print("Connected to directory!")

        # Send handshake
        handshake = {
            "type": MessageType.HANDSHAKE.value,
            "line": json.dumps(
                {
                    "app-name": "joinmarket",
                    "directory": False,
                    "location-string": "NOT-SERVING-ONION",
                    "proto-ver": 5,
                    "features": {"neutrino_compat": False, "peerlist_features": True},
                    "nick": our_nick,
                    "network": "mainnet",
                }
            ),
        }
        await conn.send(json.dumps(handshake).encode())
        print("Sent handshake, waiting for response...")

        # Wait for handshake response
        for _ in range(10):
            try:
                data = await asyncio.wait_for(conn.receive(), timeout=5.0)
                if data:
                    msg = json.loads(data.decode())
                    if msg.get("type") == MessageType.HANDSHAKE.value:
                        print("Handshake accepted!")
                        break
            except asyncio.TimeoutError:
                continue

        # Send !orderbook request
        orderbook_msg = {
            "type": MessageType.PUBMSG.value,
            "line": f"{our_nick}!PUBLIC!!orderbook",
        }
        await conn.send(json.dumps(orderbook_msg).encode())
        print(f"Sent !orderbook request, waiting for responses from {maker_nick}...")

        # Listen for responses
        bond_proof = None
        start = asyncio.get_event_loop().time()

        while asyncio.get_event_loop().time() - start < timeout:
            try:
                data = await asyncio.wait_for(conn.receive(), timeout=5.0)
                if not data:
                    continue

                msg = json.loads(data.decode())
                line = msg.get("line", "")

                # Look for PRIVMSG from the maker
                if msg.get("type") == MessageType.PRIVMSG.value and maker_nick in line:
                    print(f"\nReceived PRIVMSG from {maker_nick}!")

                    # Check for tbond
                    if "tbond" in line.lower():
                        # Extract bond proof
                        parts = line.split("!tbond ")
                        if len(parts) >= 2:
                            bond_parts = parts[1].split()
                            if bond_parts:
                                bond_proof = bond_parts[0]
                                print(
                                    f"  BOND PROOF FOUND! Length: {len(bond_proof)} chars"
                                )
                                break

            except asyncio.TimeoutError:
                print(".", end="", flush=True)
                continue
            except Exception as e:
                print(f"Error: {e}")
                continue

        await conn.close()

        if bond_proof:
            result = {
                "maker_nick": maker_nick,
                "taker_nick": our_nick,
                "proof": bond_proof,
            }

            # Save to file if requested
            if output_file:
                with open(output_file, "w") as f:
                    f.write(f"maker_nick={maker_nick}\n")
                    f.write(f"taker_nick={our_nick}\n")
                    f.write(f"proof={bond_proof}\n")
                print(f"\nSaved to {output_file}")

            return result
        else:
            print(f"\nNo bond proof received from {maker_nick}")
            print("The maker might be offline or not have a bond configured.")
            return None

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        return None


# ============================================================================
# PARSE: Analyze bond proof structure and signatures
# ============================================================================


def parse_bond_proof(proof_b64: str, maker_nick: str, taker_nick: str) -> dict:
    """
    Parse a fidelity bond proof and return all fields.

    Args:
        proof_b64: Base64-encoded bond proof
        maker_nick: Nick of the maker
        taker_nick: Nick of the taker

    Returns:
        Dict with parsed fields or error
    """
    # Decode from base64
    try:
        proof_bytes = base64.b64decode(proof_b64)
    except Exception as e:
        return {"error": f"Failed to decode base64: {e}"}

    if len(proof_bytes) != 252:
        return {"error": f"Invalid proof length: {len(proof_bytes)}, expected 252"}

    # Unpack according to reference format
    try:
        unpacked = struct.unpack("<72s72s33sH33s32sII", proof_bytes)
    except Exception as e:
        return {"error": f"Failed to unpack: {e}"}

    (
        nick_sig_padded,
        cert_sig_padded,
        cert_pub,
        cert_expiry_encoded,
        utxo_pub,
        txid_bytes,
        vout,
        locktime,
    ) = unpacked

    # Find DER signature start (0x30 byte)
    try:
        nick_sig_start = nick_sig_padded.index(b"\x30")
        nick_sig = nick_sig_padded[nick_sig_start:]
    except ValueError:
        nick_sig = None
        nick_sig_start = -1

    try:
        cert_sig_start = cert_sig_padded.index(b"\x30")
        cert_sig = cert_sig_padded[cert_sig_start:]
    except ValueError:
        cert_sig = None
        cert_sig_start = -1

    # Verify nick signature
    nick_msg = (taker_nick + "|" + maker_nick).encode("ascii")
    nick_sig_valid: bool | str | None = None
    if nick_sig and cert_pub:
        try:
            nick_sig_valid = verify_bitcoin_message_signature(
                nick_msg, nick_sig, cert_pub
            )
        except Exception as e:
            nick_sig_valid = f"Error: {e}"

    # Verify cert signature
    # Binary format: pubkey as raw bytes (reference implementation default)
    cert_msg_binary = (
        b"fidelity-bond-cert|"
        + cert_pub
        + b"|"
        + str(cert_expiry_encoded).encode("ascii")
    )
    # ASCII format: pubkey as hex string (for cold storage / Sparrow compatibility)
    cert_msg_ascii = (
        b"fidelity-bond-cert|"
        + cert_pub.hex().encode("ascii")
        + b"|"
        + str(cert_expiry_encoded).encode("ascii")
    )
    cert_sig_valid: bool | str | None = None
    cert_sig_format: str | None = None
    cert_msg_used = cert_msg_binary  # Default for display
    if cert_sig and utxo_pub:
        try:
            # Try binary format first (hot wallet / self-signed)
            if verify_bitcoin_message_signature(cert_msg_binary, cert_sig, utxo_pub):
                cert_sig_valid = True
                cert_sig_format = "binary"
                cert_msg_used = cert_msg_binary
            # Try ASCII format (cold wallet / Sparrow signed)
            elif verify_bitcoin_message_signature(cert_msg_ascii, cert_sig, utxo_pub):
                cert_sig_valid = True
                cert_sig_format = "ascii"
                cert_msg_used = cert_msg_ascii
            else:
                cert_sig_valid = False
        except Exception as e:
            cert_sig_valid = f"Error: {e}"

    # Convert txid to hex
    txid_display = txid_bytes.hex()

    result = {
        "proof_length": len(proof_bytes),
        "nick_signature": {
            "padded_hex": nick_sig_padded.hex(),
            "der_start_offset": nick_sig_start,
            "der_sig_hex": nick_sig.hex() if nick_sig else None,
            "message": nick_msg.decode("ascii"),
            "message_hex": nick_msg.hex(),
            "valid": nick_sig_valid,
        },
        "cert_signature": {
            "padded_hex": cert_sig_padded.hex(),
            "der_start_offset": cert_sig_start,
            "der_sig_hex": cert_sig.hex() if cert_sig else None,
            "message": cert_msg_used.decode("ascii", errors="replace"),
            "message_hex": cert_msg_used.hex(),
            "message_format": cert_sig_format,
            "valid": cert_sig_valid,
        },
        "cert_pubkey": {
            "hex": cert_pub.hex(),
            "compressed": len(cert_pub) == 33,
            "prefix": hex(cert_pub[0]) if cert_pub else None,
        },
        "cert_expiry": {
            "encoded": cert_expiry_encoded,
            "blocks": cert_expiry_encoded * 2016,
            "explanation": f"{cert_expiry_encoded} retarget periods = {cert_expiry_encoded * 2016} blocks",
        },
        "utxo_pubkey": {
            "hex": utxo_pub.hex(),
            "compressed": len(utxo_pub) == 33,
            "prefix": hex(utxo_pub[0]) if utxo_pub else None,
            "matches_cert_pub": utxo_pub == cert_pub,
        },
        "utxo": {
            "txid": txid_display,
            "vout": vout,
            "outpoint": f"{txid_display}:{vout}",
        },
        "locktime": {
            "value": locktime,
            "unix_timestamp": locktime,
        },
    }

    return result


def print_analysis(data: dict, indent: int = 0) -> None:
    """Pretty print the analysis."""
    prefix = "  " * indent

    if "error" in data:
        print(f"{prefix}ERROR: {data['error']}")
        return

    for key, value in data.items():
        if isinstance(value, dict):
            print(f"{prefix}{key}:")
            print_analysis(value, indent + 1)
        elif isinstance(value, bool):
            symbol = "✓" if value else "✗"
            print(f"{prefix}{key}: {symbol} ({value})")
        elif isinstance(value, str) and key.endswith("_hex") and len(value) > 80:
            print(f"{prefix}{key}: {value[:60]}...")
        else:
            print(f"{prefix}{key}: {value}")


# ============================================================================
# CLI Interface
# ============================================================================


def cmd_fetch(args):
    """Fetch bond proof from mainnet maker."""
    result = asyncio.run(
        fetch_bond_proof(
            maker_nick=args.maker_nick,
            directory_onion=args.directory,
            output_file=args.output,
            timeout=args.timeout,
        )
    )

    if result:
        print("\n" + "=" * 80)
        print("SUCCESS: Bond proof fetched")
        print("=" * 80)
        print(f"Maker: {result['maker_nick']}")
        print(f"Taker: {result['taker_nick']}")
        print(f"Proof: {result['proof'][:60]}...")
        return 0
    else:
        return 1


def cmd_parse(args):
    """Parse and analyze bond proof."""
    # Check if arg is a file
    if Path(args.proof).exists():
        filepath = Path(args.proof)
        content = filepath.read_text().strip()

        # Parse file format
        lines = content.split("\n")
        data = {}
        for line in lines:
            if "=" in line:
                key, value = line.split("=", 1)
                data[key] = value

        proof = data.get("proof", "")
        maker_nick = data.get("maker_nick", args.maker or "")
        taker_nick = data.get("taker_nick", args.taker or "")

        print("=" * 80)
        print("FIDELITY BOND PROOF ANALYSIS")
        print("=" * 80)
        print(f"Source: {filepath}")
        print(f"Maker: {maker_nick}")
        print(f"Taker: {taker_nick}")
        print(f"Proof: {proof[:60]}...")
        print("=" * 80)
        print()
    else:
        # Parse from command line
        if not args.maker or not args.taker:
            print(
                "Error: When parsing base64 directly, --maker and --taker are required"
            )
            return 1

        proof = args.proof
        maker_nick = args.maker
        taker_nick = args.taker

        print("=" * 80)
        print("FIDELITY BOND PROOF ANALYSIS")
        print("=" * 80)
        print(f"Maker: {maker_nick}")
        print(f"Taker: {taker_nick}")
        print(f"Proof: {proof[:60]}...")
        print("=" * 80)
        print()

    # Parse the proof
    result = parse_bond_proof(proof, maker_nick, taker_nick)

    # Print analysis
    print_analysis(result)

    # Summary
    if "error" not in result:
        print()
        print("=" * 80)
        print("SUMMARY")
        print("=" * 80)

        nick_valid = result["nick_signature"]["valid"]
        cert_valid = result["cert_signature"]["valid"]

        if nick_valid is True and cert_valid is True:
            print("✓ Both signatures are VALID")
            print(f"✓ UTXO: {result['utxo']['outpoint']}")
            print(f"✓ Locktime: {result['locktime']['value']}")
            print(f"✓ Cert expires after block: {result['cert_expiry']['blocks']}")
            return 0
        else:
            print("✗ Signature validation FAILED")
            if nick_valid is not True:
                print(f"  Nick signature: {nick_valid}")
            if cert_valid is not True:
                print(f"  Cert signature: {cert_valid}")
            return 1
    else:
        return 1


def cmd_fetch_parse(args):
    """Fetch and parse in one command."""
    print("Step 1: Fetching bond proof...")
    print("=" * 80)

    result = asyncio.run(
        fetch_bond_proof(
            maker_nick=args.maker_nick,
            directory_onion=args.directory,
            timeout=args.timeout,
        )
    )

    if not result:
        return 1

    print("\n" + "=" * 80)
    print("Step 2: Parsing bond proof...")
    print("=" * 80)
    print()

    # Parse the proof
    parsed = parse_bond_proof(
        result["proof"], result["maker_nick"], result["taker_nick"]
    )

    # Print analysis
    print_analysis(parsed)

    # Summary
    if "error" not in parsed:
        print()
        print("=" * 80)
        print("SUMMARY")
        print("=" * 80)

        nick_valid = parsed["nick_signature"]["valid"]
        cert_valid = parsed["cert_signature"]["valid"]

        if nick_valid is True and cert_valid is True:
            print("✓ Both signatures are VALID")
            print(f"✓ UTXO: {parsed['utxo']['outpoint']}")
            print(f"✓ Locktime: {parsed['locktime']['value']}")
            print(f"✓ Cert expires after block: {parsed['cert_expiry']['blocks']}")
            return 0
        else:
            print("✗ Signature validation FAILED")
            return 1
    else:
        return 1


def main():
    parser = argparse.ArgumentParser(
        description="Fidelity Bond Tool - Fetch, parse, and analyze JoinMarket fidelity bond proofs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Fetch command
    fetch_parser = subparsers.add_parser(
        "fetch", help="Fetch bond proof from mainnet maker"
    )
    fetch_parser.add_argument("maker_nick", help="Nick of the maker to fetch from")
    fetch_parser.add_argument(
        "--directory",
        default="nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion",
        help="Directory server onion address",
    )
    fetch_parser.add_argument("--output", help="Output file to save proof")
    fetch_parser.add_argument(
        "--timeout", type=int, default=60, help="Timeout in seconds"
    )

    # Parse command
    parse_parser = subparsers.add_parser("parse", help="Parse and analyze bond proof")
    parse_parser.add_argument("proof", help="Bond proof file or base64 string")
    parse_parser.add_argument(
        "--maker", help="Maker nick (required if parsing base64 directly)"
    )
    parse_parser.add_argument(
        "--taker", help="Taker nick (required if parsing base64 directly)"
    )

    # Fetch-parse command
    fetch_parse_parser = subparsers.add_parser(
        "fetch-parse", help="Fetch and parse in one command"
    )
    fetch_parse_parser.add_argument(
        "maker_nick", help="Nick of the maker to fetch from"
    )
    fetch_parse_parser.add_argument(
        "--directory",
        default="nakamotourflxwjnjpnrk7yc2nhkf6r62ed4gdfxmmn5f4saw5q5qoyd.onion",
        help="Directory server onion address",
    )
    fetch_parse_parser.add_argument(
        "--timeout", type=int, default=60, help="Timeout in seconds"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    if args.command == "fetch":
        return cmd_fetch(args)
    elif args.command == "parse":
        return cmd_parse(args)
    elif args.command == "fetch-parse":
        return cmd_fetch_parse(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
