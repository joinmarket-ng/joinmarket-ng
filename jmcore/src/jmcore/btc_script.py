"""
Bitcoin script utilities for fidelity bonds.

Uses python-bitcointx for script operations where appropriate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from bitcointx.core.script import (
    OP_0,
    OP_CHECKLOCKTIMEVERIFY,
    OP_CHECKSIG,
    OP_CHECKSIGVERIFY,
    OP_DROP,
    CScript,
    CScriptOp,
)


def mk_freeze_script(pubkey_hex: str, locktime: int) -> bytes:
    """
    Create a timelocked script using OP_CHECKLOCKTIMEVERIFY.

    Script format: <locktime> OP_CHECKLOCKTIMEVERIFY OP_DROP <pubkey> OP_CHECKSIG

    Args:
        pubkey_hex: Compressed public key as hex string (33 bytes)
        locktime: Unix timestamp for the locktime

    Returns:
        Script as bytes
    """
    pubkey_bytes = bytes.fromhex(pubkey_hex)
    if len(pubkey_bytes) != 33:
        raise ValueError(f"Invalid pubkey length: {len(pubkey_bytes)}, expected 33")

    # Use python-bitcointx to build the script
    script = CScript([locktime, OP_CHECKLOCKTIMEVERIFY, OP_DROP, pubkey_bytes, OP_CHECKSIG])
    return bytes(script)


def disassemble_script(script_bytes: bytes) -> str:
    """
    Disassemble a Bitcoin script into human-readable form.

    Uses python-bitcointx for proper parsing.

    Args:
        script_bytes: Script bytes

    Returns:
        Human-readable script representation
    """
    script = CScript(script_bytes)
    parts: list[str] = []

    for op in script:
        if isinstance(op, CScriptOp):
            parts.append(str(op))
        elif isinstance(op, bytes):
            # Data push - try to interpret as number if small
            if len(op) <= 5:
                try:
                    num = _decode_scriptnum(op)
                    parts.append(str(num))
                except (ValueError, IndexError):
                    parts.append(f"<{op.hex()}>")
            else:
                parts.append(f"<{op.hex()}>")
        elif isinstance(op, int):
            parts.append(str(op))
        else:
            parts.append(repr(op))

    return " ".join(parts)


def _decode_scriptnum(data: bytes) -> int:
    """
    Decode a script number from bytes.

    Args:
        data: Encoded script number bytes

    Returns:
        Decoded integer
    """
    if len(data) == 0:
        return 0

    # Little-endian with sign bit in MSB
    result = int.from_bytes(data, "little")
    if data[-1] & 0x80:
        # Negative number - clear sign bit and negate
        result = -(result & ~(0x80 << ((len(data) - 1) * 8)))

    return result


def redeem_script_to_p2wsh_script(redeem_script: bytes) -> bytes:
    """
    Convert a redeem script to P2WSH scriptPubKey.

    Args:
        redeem_script: The redeem script bytes

    Returns:
        P2WSH scriptPubKey (OP_0 <32-byte-hash>)
    """
    script_hash = hashlib.sha256(redeem_script).digest()
    script = CScript([OP_0, script_hash])
    return bytes(script)


@dataclass(frozen=True)
class BondAddressInfo:
    """Derived address information for a fidelity bond UTXO."""

    address: str
    """Bech32 P2WSH address (e.g. bc1q... or tb1q...)"""
    scriptpubkey: bytes
    """P2WSH scriptPubKey bytes (OP_0 <32-byte-hash>)"""
    witness_script: bytes
    """The timelocked witness script: <locktime> OP_CLTV OP_DROP <pubkey> OP_CHECKSIG"""


def derive_bond_address(
    utxo_pub: bytes,
    locktime: int,
    network: str = "mainnet",
) -> BondAddressInfo:
    """
    Derive the P2WSH fidelity bond address from a bond proof's public key and locktime.

    Given the UTXO public key and locktime from a fidelity bond proof, this reconstructs
    the timelocked witness script and derives the P2WSH address. This address can then be
    used by any backend (full node, neutrino, mempool) to look up the bond UTXO.

    Args:
        utxo_pub: 33-byte compressed public key from the bond proof
        locktime: Locktime value from the bond proof (Unix timestamp)
        network: Bitcoin network ("mainnet", "testnet", "signet", "regtest")

    Returns:
        BondAddressInfo with address, scriptpubkey, and witness_script

    Raises:
        ValueError: If utxo_pub is not 33 bytes
    """
    # Import here to avoid circular imports (bitcoin.py imports from btc_script.py)
    from jmcore.bitcoin import scriptpubkey_to_address

    if len(utxo_pub) != 33:
        raise ValueError(f"Invalid utxo_pub length: {len(utxo_pub)}, expected 33")

    # Reconstruct the timelocked witness script
    witness_script = mk_freeze_script(utxo_pub.hex(), locktime)

    # Hash to P2WSH scriptPubKey
    scriptpubkey = redeem_script_to_p2wsh_script(witness_script)

    # Derive bech32 address
    address = scriptpubkey_to_address(scriptpubkey, network)

    return BondAddressInfo(
        address=address,
        scriptpubkey=scriptpubkey,
        witness_script=witness_script,
    )


# =============================================================================
# Taproot (BIP341) fidelity bonds
# =============================================================================

# BIP341 NUMS point H, with provably-unknown discrete log, used as the taproot
# internal key so the bond output has no usable key-path spend. Until the bond
# is spent, the output is indistinguishable from any other key-path P2TR.
BIP341_NUMS_H = bytes.fromhex("50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0")

# BIP342 tapscript leaf version.
TAPROOT_LEAF_VERSION = 0xC0


def mk_taproot_freeze_tapleaf(pubkey_xonly: bytes, locktime: int) -> bytes:
    """
    Build the timelocked tapscript leaf for a Taproot fidelity bond.

    Script format: <32B x-only> OP_CHECKSIGVERIFY <locktime> OP_CHECKLOCKTIMEVERIFY

    Putting OP_CHECKLOCKTIMEVERIFY last leaves the locktime on the stack as a
    truthy result, so the OP_DROP needed by the legacy <locktime> CLTV DROP
    form is unnecessary. This matches the construction used by CoinSwap.

    Args:
        pubkey_xonly: 32-byte x-only public key
        locktime: Unix timestamp for the locktime

    Returns:
        Tapscript leaf bytes
    """
    if len(pubkey_xonly) != 32:
        raise ValueError(f"Invalid x-only pubkey length: {len(pubkey_xonly)}, expected 32")

    script = CScript([pubkey_xonly, OP_CHECKSIGVERIFY, locktime, OP_CHECKLOCKTIMEVERIFY])
    return bytes(script)


def tapleaf_hash(leaf_script: bytes, leaf_version: int = TAPROOT_LEAF_VERSION) -> bytes:
    """Compute the BIP341 TapLeaf hash for a single tapscript leaf."""
    from jmcore.bitcoin import encode_varint, tagged_hash

    return tagged_hash(
        "TapLeaf",
        bytes([leaf_version]) + encode_varint(len(leaf_script)) + leaf_script,
    )


@dataclass(frozen=True)
class TaprootBondInfo:
    """Derived address information for a Taproot fidelity bond UTXO."""

    address: str
    """Bech32m P2TR address (e.g. bc1p... or bcrt1p...)"""
    scriptpubkey: bytes
    """P2TR scriptPubKey bytes (OP_1 <32-byte-output-key>)"""
    output_pubkey: bytes
    """32-byte x-only taproot output key"""
    tapleaf_script: bytes
    """The timelocked tapscript leaf: <xonly> OP_CHECKSIGVERIFY <locktime> OP_CLTV"""
    control_block: bytes
    """BIP341 control block for a script-path spend of the single leaf"""


def derive_taproot_bond_address(
    pubkey_xonly: bytes,
    locktime: int,
    network: str = "mainnet",
) -> TaprootBondInfo:
    """
    Derive the P2TR fidelity bond address from a bond key and locktime.

    The bond commits the timelocked tapscript leaf into a taproot output that
    uses the BIP341 NUMS point as its internal key, so the only spend path is
    the CLTV-gated script path. Before being spent, the output looks like an
    ordinary key-path P2TR.

    Args:
        pubkey_xonly: 32-byte x-only bond public key
        locktime: Locktime value (Unix timestamp)
        network: Bitcoin network ("mainnet", "testnet", "signet", "regtest")

    Returns:
        TaprootBondInfo with address, scriptpubkey, output key, leaf and control block

    Raises:
        ValueError: If pubkey_xonly is not 32 bytes
    """
    from jmcore.bitcoin import (
        create_p2tr_scriptpubkey,
        pubkey_to_p2tr_address,
        taproot_tweak_pubkey,
    )

    if len(pubkey_xonly) != 32:
        raise ValueError(f"Invalid x-only pubkey length: {len(pubkey_xonly)}, expected 32")

    tapleaf_script = mk_taproot_freeze_tapleaf(pubkey_xonly, locktime)
    merkle_root = tapleaf_hash(tapleaf_script)

    parity, output_pubkey = taproot_tweak_pubkey(BIP341_NUMS_H, merkle_root)
    scriptpubkey = create_p2tr_scriptpubkey(output_pubkey)
    address = pubkey_to_p2tr_address(output_pubkey, network)

    # Single-leaf tree: control block is just the leaf version (OR parity) plus
    # the internal key, with an empty Merkle path.
    control_block = bytes([TAPROOT_LEAF_VERSION | parity]) + BIP341_NUMS_H

    return TaprootBondInfo(
        address=address,
        scriptpubkey=scriptpubkey,
        output_pubkey=output_pubkey,
        tapleaf_script=tapleaf_script,
        control_block=control_block,
    )
