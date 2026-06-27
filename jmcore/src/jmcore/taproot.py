"""
BIP341/BIP342 Taproot primitives: tapscript construction, taptrees, control
blocks, key/script-path sighash, and Schnorr signing.

This module is the on-chain building block for the JoinMarket swap protocol
(``jmswap``), but it contains no swap-specific logic: it is a general Taproot
toolkit built on ``coincurve`` (libsecp256k1), matching the rest of ``jmcore``.

References:
    - BIP340 (Schnorr signatures)
    - BIP341 (Taproot)
    - BIP342 (Tapscript)
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field

from coincurve import PrivateKey, PublicKey, PublicKeyXOnly

from jmcore.bitcoin import (
    create_p2tr_scriptpubkey,
    encode_varint,
    scriptpubkey_to_address,
    sha256,
    tagged_hash,
    taproot_tweak_pubkey,
)
from jmcore.constants import SECP256K1_N

# BIP342 tapscript leaf version (the only one Bitcoin currently defines).
LEAF_VERSION_TAPSCRIPT = 0xC0

# BIP341 / BIP342 sighash type flags.
SIGHASH_DEFAULT = 0x00
SIGHASH_ALL = 0x01
SIGHASH_NONE = 0x02
SIGHASH_SINGLE = 0x03
SIGHASH_ANYONECANPAY = 0x80

# Subset of opcodes used by swap scripts.
OP_0 = 0x00
OP_1 = 0x51
OP_DROP = 0x75
OP_SIZE = 0x82
OP_EQUAL = 0x87
OP_EQUALVERIFY = 0x88
OP_HASH160 = 0xA9
OP_SHA256 = 0xA8
OP_CHECKSIG = 0xAC
OP_CHECKSIGVERIFY = 0xAD
OP_CHECKLOCKTIMEVERIFY = 0xB1
OP_CHECKSEQUENCEVERIFY = 0xB2


class TaprootError(Exception):
    """Raised on invalid Taproot data or signing operations."""


def ripemd160(data: bytes) -> bytes:
    """RIPEMD160(data)."""
    return hashlib.new("ripemd160", data).digest()


# ---------------------------------------------------------------------------
# Script construction
# ---------------------------------------------------------------------------


def push_data(data: bytes) -> bytes:
    """Minimal-encoding data push for a Bitcoin script element."""
    n = len(data)
    if n < 0x4C:
        return bytes([n]) + data
    if n <= 0xFF:
        return bytes([0x4C, n]) + data
    if n <= 0xFFFF:
        return bytes([0x4D]) + struct.pack("<H", n) + data
    return bytes([0x4E]) + struct.pack("<I", n) + data


def encode_script_num(n: int) -> bytes:
    """Encode an integer as a minimally-encoded CScriptNum (little-endian signed)."""
    if n == 0:
        return b""
    negative = n < 0
    absvalue = -n if negative else n
    out = bytearray()
    while absvalue:
        out.append(absvalue & 0xFF)
        absvalue >>= 8
    if out[-1] & 0x80:
        out.append(0x80 if negative else 0x00)
    elif negative:
        out[-1] |= 0x80
    return bytes(out)


def push_script_num(n: int) -> bytes:
    """Minimal push of a non-negative script number.

    Matches Bitcoin Core / bitcoinjs ``script.number.encode`` + minimal push:
    ``0`` is ``OP_0``, ``1..16`` are ``OP_1..OP_16``, larger values are a
    minimal CScriptNum data push.
    """
    if n < 0:
        raise TaprootError("push_script_num expects a non-negative number")
    if n == 0:
        return bytes([OP_0])
    if 1 <= n <= 16:
        return bytes([0x50 + n])  # OP_1 .. OP_16
    return push_data(encode_script_num(n))


def construct_script(items: list[int | bytes]) -> bytes:
    """Build a Bitcoin script from opcodes (``int``) and data pushes (``bytes``).

    Integers in ``0..16`` are encoded as ``OP_N`` when they appear as opcodes;
    callers that want a numeric push (e.g. a CLTV locktime) should pass the
    already-encoded :func:`encode_script_num` bytes.
    """
    out = bytearray()
    for item in items:
        if isinstance(item, int):
            # Integers are raw opcode bytes. Numeric *pushes* (e.g. a CLTV
            # locktime or the size literal 32) must be passed as bytes via
            # encode_script_num() so they are never confused with opcodes.
            if item == OP_0 or OP_1 <= item <= 0xFF:
                out.append(item)
            else:
                raise TaprootError(f"opcode out of range: {item:#x}")
        elif isinstance(item, (bytes, bytearray)):
            out += push_data(bytes(item))
        else:
            raise TaprootError(f"unsupported script item: {item!r}")
    return bytes(out)


def to_xonly(pubkey: bytes) -> bytes:
    """Convert a 33-byte compressed (or already 32-byte x-only) pubkey to x-only."""
    if not isinstance(pubkey, (bytes, bytearray)):
        raise TaprootError("pubkey must be bytes")
    if len(pubkey) == 32:
        return bytes(pubkey)
    if len(pubkey) != 33 or pubkey[0] not in (0x02, 0x03):
        raise TaprootError("expected 33-byte compressed pubkey")
    PublicKey(bytes(pubkey))  # validate
    return bytes(pubkey[1:])


# ---------------------------------------------------------------------------
# Taptree (BIP341 merkle tree of tapscript leaves)
# ---------------------------------------------------------------------------

# A leaf is ``(leaf_version, script)``. A tree is either a leaf or a list of
# subtrees (built into a balanced tree, matching BIP341 / boltz-core).
TapLeaf = tuple[int, bytes]
TapTreeNode = TapLeaf | list["TapTreeNode"]


def tapleaf_hash(script: bytes, leaf_version: int = LEAF_VERSION_TAPSCRIPT) -> bytes:
    """BIP341 TapLeaf hash: tagged_hash('TapLeaf', leaf_version || ser(script))."""
    return tagged_hash("TapLeaf", bytes([leaf_version]) + encode_varint(len(script)) + script)


def _tapbranch_hash(a: bytes, b: bytes) -> bytes:
    return tagged_hash("TapBranch", min(a, b) + max(a, b))


def taproot_tree_helper(script_tree: TapTreeNode) -> tuple[list[tuple[int, bytes, bytes]], bytes]:
    """Build a balanced taptree, returning ``(leaves_with_paths, merkle_root)``.

    Each entry of ``leaves_with_paths`` is ``(leaf_version, script, merkle_path)``
    where ``merkle_path`` is the concatenation of sibling hashes for that leaf.
    """
    if isinstance(script_tree, tuple):
        leaf_version, script = script_tree
        h = tapleaf_hash(script, leaf_version)
        return [(leaf_version, script, b"")], h
    items = list(script_tree)
    if len(items) == 0:
        raise TaprootError("empty script tree")
    if len(items) == 1:
        return taproot_tree_helper(items[0])
    split = (len(items) + 1) // 2
    left, left_h = taproot_tree_helper(items[:split])
    right, right_h = taproot_tree_helper(items[split:])
    out = [(v, s, c + right_h) for (v, s, c) in left]
    out += [(v, s, c + left_h) for (v, s, c) in right]
    return out, _tapbranch_hash(left_h, right_h)


def taproot_merkle_root(script_tree: TapTreeNode) -> bytes:
    """Return the BIP341 merkle root of a taptree."""
    _, root = taproot_tree_helper(script_tree)
    return root


def taproot_output_script(internal_xonly: bytes, script_tree: TapTreeNode | None = None) -> bytes:
    """Return the witness-v1 scriptPubKey for ``internal_xonly`` + optional tree."""
    if len(internal_xonly) != 32:
        raise TaprootError("internal pubkey must be 32-byte x-only")
    merkle_root = taproot_merkle_root(script_tree) if script_tree is not None else b""
    _, output_key = taproot_tweak_pubkey(internal_xonly, merkle_root or None)
    return create_p2tr_scriptpubkey(output_key)


def taproot_address(
    internal_xonly: bytes, script_tree: TapTreeNode | None = None, network: str = "mainnet"
) -> str:
    """Return the bech32m P2TR address for ``internal_xonly`` + optional tree."""
    spk = taproot_output_script(internal_xonly, script_tree)
    return scriptpubkey_to_address(spk, network)


def control_block(
    internal_xonly: bytes, script_tree: TapTreeNode, leaf_index: int
) -> tuple[bytes, bytes]:
    """Return ``(leaf_script, control_block)`` for a script-path spend.

    ``leaf_index`` indexes into the flattened leaf list produced by
    :func:`taproot_tree_helper` (insertion order of the leaves passed in).
    """
    leaves, merkle_root = taproot_tree_helper(script_tree)
    if leaf_index < 0 or leaf_index >= len(leaves):
        raise TaprootError("leaf_index out of range")
    leaf_version, script, path = leaves[leaf_index]
    output_parity, _ = taproot_tweak_pubkey(internal_xonly, merkle_root or None)
    cb = bytes([leaf_version | output_parity]) + internal_xonly + path
    return script, cb


# ---------------------------------------------------------------------------
# Minimal Taproot transaction model (for swap claim / refund / cooperative spends)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TxIn:
    """A transaction input referencing a previous output by display txid + vout."""

    txid: str  # big-endian display txid (as shown by block explorers)
    vout: int
    sequence: int = 0xFFFFFFFF

    @property
    def outpoint(self) -> bytes:
        return bytes.fromhex(self.txid)[::-1] + self.vout.to_bytes(4, "little")


@dataclass(frozen=True)
class TxOut:
    """A transaction output: value in sats + scriptPubKey bytes."""

    value: int
    scriptpubkey: bytes


@dataclass
class TaprootTx:
    """A SegWit transaction used to spend Taproot swap outputs.

    Witnesses are supplied at serialization time, so the same unsigned skeleton
    is used for sighash computation and for final serialization.
    """

    inputs: list[TxIn]
    outputs: list[TxOut]
    version: int = 2
    locktime: int = 0
    witnesses: list[list[bytes]] = field(default_factory=list)

    def _serialize_no_witness(self) -> bytes:
        out = struct.pack("<I", self.version)
        out += encode_varint(len(self.inputs))
        for inp in self.inputs:
            out += inp.outpoint + encode_varint(0) + struct.pack("<I", inp.sequence)
        out += encode_varint(len(self.outputs))
        for o in self.outputs:
            out += (
                o.value.to_bytes(8, "little") + encode_varint(len(o.scriptpubkey)) + o.scriptpubkey
            )
        out += struct.pack("<I", self.locktime)
        return out

    def serialize(self) -> bytes:
        """Serialize with the SegWit marker/flag and witness stacks."""
        if len(self.witnesses) != len(self.inputs):
            raise TaprootError("witness count must match input count")
        out = struct.pack("<I", self.version)
        out += b"\x00\x01"  # marker + flag
        out += encode_varint(len(self.inputs))
        for inp in self.inputs:
            out += inp.outpoint + encode_varint(0) + struct.pack("<I", inp.sequence)
        out += encode_varint(len(self.outputs))
        for o in self.outputs:
            out += (
                o.value.to_bytes(8, "little") + encode_varint(len(o.scriptpubkey)) + o.scriptpubkey
            )
        for stack in self.witnesses:
            out += encode_varint(len(stack))
            for item in stack:
                out += encode_varint(len(item)) + item
        out += struct.pack("<I", self.locktime)
        return out

    def txid(self) -> str:
        from jmcore.bitcoin import hash256

        return hash256(self._serialize_no_witness())[::-1].hex()


def taproot_sighash(
    tx: TaprootTx,
    input_index: int,
    prevout_values: list[int],
    prevout_scripts: list[bytes],
    sighash_type: int = SIGHASH_DEFAULT,
    leaf_hash: bytes | None = None,
) -> bytes:
    """Compute the BIP341 (key-path) / BIP342 (script-path) Taproot sighash.

    ``leaf_hash`` is the 32-byte tapleaf hash for a script-path spend, or
    ``None`` for a key-path spend. All prevout values and scriptPubKeys for the
    transaction's inputs must be supplied (Taproot commits to all of them).
    """
    n = len(tx.inputs)
    if input_index >= n:
        raise TaprootError("input index out of range")
    if len(prevout_values) != n or len(prevout_scripts) != n:
        raise TaprootError("prevouts length must match inputs length")

    anyonecanpay = bool(sighash_type & SIGHASH_ANYONECANPAY)
    out_kind = sighash_type & 0x03

    hash_prevouts = hash_amounts = hash_scripts = hash_sequences = b""
    if not anyonecanpay:
        hash_prevouts = sha256(b"".join(i.outpoint for i in tx.inputs))
        hash_amounts = sha256(b"".join(v.to_bytes(8, "little") for v in prevout_values))
        hash_scripts = sha256(b"".join(encode_varint(len(s)) + s for s in prevout_scripts))
        hash_sequences = sha256(b"".join(struct.pack("<I", i.sequence) for i in tx.inputs))

    hash_outputs = b""
    if out_kind not in (SIGHASH_NONE, SIGHASH_SINGLE):
        hash_outputs = sha256(
            b"".join(
                o.value.to_bytes(8, "little") + encode_varint(len(o.scriptpubkey)) + o.scriptpubkey
                for o in tx.outputs
            )
        )

    preimage = bytes([0x00])  # epoch
    preimage += bytes([sighash_type])
    preimage += struct.pack("<I", tx.version)
    preimage += struct.pack("<I", tx.locktime)
    if not anyonecanpay:
        preimage += hash_prevouts + hash_amounts + hash_scripts + hash_sequences
    if out_kind not in (SIGHASH_NONE, SIGHASH_SINGLE):
        preimage += hash_outputs

    spend_type = 0x02 if leaf_hash is not None else 0x00  # ext_flag*2 + annex(0)
    preimage += bytes([spend_type])

    if anyonecanpay:
        target = tx.inputs[input_index]
        preimage += target.outpoint
        preimage += prevout_values[input_index].to_bytes(8, "little")
        preimage += encode_varint(len(prevout_scripts[input_index])) + prevout_scripts[input_index]
        preimage += struct.pack("<I", target.sequence)
    else:
        preimage += struct.pack("<I", input_index)

    if out_kind == SIGHASH_SINGLE:
        if input_index < len(tx.outputs):
            o = tx.outputs[input_index]
            preimage += sha256(
                o.value.to_bytes(8, "little") + encode_varint(len(o.scriptpubkey)) + o.scriptpubkey
            )
        else:
            preimage += b"\x00" * 32

    if leaf_hash is not None:
        preimage += leaf_hash
        preimage += bytes([0x00])  # key version
        preimage += b"\xff\xff\xff\xff"  # codeseparator position (none)

    return tagged_hash("TapSighash", preimage)


def sign_taproot_keypath(
    tx: TaprootTx,
    input_index: int,
    prevout_values: list[int],
    prevout_scripts: list[bytes],
    output_privkey: bytes,
    sighash_type: int = SIGHASH_DEFAULT,
    aux_rand: bytes = b"\x00" * 32,
) -> bytes:
    """Produce a BIP340 Schnorr key-path signature for ``input_index``.

    ``output_privkey`` must already be the tweaked output key (the caller applies
    :func:`jmcore.bitcoin.taproot_tweak_privkey` for a script-tree commitment, or
    passes the raw key for a key-path-only output).
    """
    sighash = taproot_sighash(tx, input_index, prevout_values, prevout_scripts, sighash_type)
    sig = PrivateKey(output_privkey).sign_schnorr(sighash, aux_rand)
    if sighash_type != SIGHASH_DEFAULT:
        sig += bytes([sighash_type])
    return sig


def sign_taproot_scriptpath(
    tx: TaprootTx,
    input_index: int,
    prevout_values: list[int],
    prevout_scripts: list[bytes],
    leaf_script: bytes,
    privkey: bytes,
    leaf_version: int = LEAF_VERSION_TAPSCRIPT,
    sighash_type: int = SIGHASH_DEFAULT,
    aux_rand: bytes = b"\x00" * 32,
) -> bytes:
    """Produce a BIP342 Schnorr script-path signature for a tapscript leaf."""
    lh = tapleaf_hash(leaf_script, leaf_version)
    sighash = taproot_sighash(
        tx, input_index, prevout_values, prevout_scripts, sighash_type, leaf_hash=lh
    )
    sig = PrivateKey(privkey).sign_schnorr(sighash, aux_rand)
    if sighash_type != SIGHASH_DEFAULT:
        sig += bytes([sighash_type])
    return sig


def verify_schnorr(xonly_pubkey: bytes, signature: bytes, message: bytes) -> bool:
    """Verify a 64-byte BIP340 Schnorr signature over ``message`` (a 32-byte hash)."""
    try:
        if len(signature) not in (64, 65):
            return False
        return bool(PublicKeyXOnly(xonly_pubkey).verify(signature[:64], message))
    except Exception:  # noqa: BLE001 - verification must never raise
        return False


def xonly_from_privkey(privkey: bytes) -> bytes:
    """Return the 32-byte x-only public key for a private key."""
    return PrivateKey(privkey).public_key.format(compressed=True)[1:]


def negate_privkey_if_odd(privkey: bytes) -> bytes:
    """Return a private key whose public key has even Y (BIP340 convention)."""
    pk = PrivateKey(privkey)
    if pk.public_key.format(compressed=True)[0] == 0x03:
        d = (SECP256K1_N - int.from_bytes(privkey, "big")) % SECP256K1_N
        return d.to_bytes(32, "big")
    return privkey
