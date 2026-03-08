"""
Bitcoin transaction signing utilities for P2WPKH and P2WSH inputs.

Uses the unified transaction models from jmcore.bitcoin.  The signing
functions access byte-oriented properties (``txid_le``, ``sequence_bytes``,
``version_bytes``, ``locktime_bytes``) to construct the exact BIP-143
sighash preimage.
"""

from __future__ import annotations

from coincurve import PrivateKey
from jmcore.bitcoin import (
    ParsedTransaction,
    TxInput,
    TxOutput,
    create_p2wpkh_script_code,
    decode_varint,
    encode_varint,
    hash256,
    parse_transaction_bytes,
    sha256,
    tagged_hash,
)

SIGHASH_DEFAULT = 0x00
SIGHASH_ALL = 0x01
SIGHASH_NONE = 0x02
SIGHASH_SINGLE = 0x03
SIGHASH_ANYONECANPAY = 0x80

# Backward-compat alias: old code imports ``Transaction`` from here.
Transaction = ParsedTransaction

# Alias for backward compatibility
read_varint = decode_varint


class TransactionSigningError(Exception):
    pass


def deserialize_transaction(tx_bytes: bytes) -> ParsedTransaction:
    """Deserialize a raw transaction for signing.

    Delegates to :func:`jmcore.bitcoin.parse_transaction_bytes` which now
    returns typed ``TxInput`` / ``TxOutput`` objects with the dual-accessor
    API required by the signing code.

    Raises:
        TransactionSigningError: If the transaction bytes cannot be parsed.
    """
    try:
        return parse_transaction_bytes(tx_bytes)
    except Exception as e:
        raise TransactionSigningError(f"Failed to parse transaction: {e}") from e


def compute_sighash_segwit(
    tx: ParsedTransaction,
    input_index: int,
    script_code: bytes,
    value: int,
    sighash_type: int,
) -> bytes:
    try:
        if input_index >= len(tx.inputs):
            raise TransactionSigningError("Input index out of range")

        hash_prevouts = hash256(
            b"".join(inp.txid_le + inp.vout.to_bytes(4, "little") for inp in tx.inputs)
        )
        hash_sequence = hash256(b"".join(inp.sequence_bytes for inp in tx.inputs))
        hash_outputs = hash256(
            b"".join(
                out.value.to_bytes(8, "little") + encode_varint(len(out.script)) + out.script
                for out in tx.outputs
            )
        )

        target_input = tx.inputs[input_index]

        preimage = (
            tx.version_bytes
            + hash_prevouts
            + hash_sequence
            + target_input.txid_le
            + target_input.vout.to_bytes(4, "little")
            + encode_varint(len(script_code))
            + script_code
            + value.to_bytes(8, "little")
            + target_input.sequence_bytes
            + hash_outputs
            + tx.locktime_bytes
            + sighash_type.to_bytes(4, "little")
        )

        return hash256(preimage)

    except Exception as e:
        raise TransactionSigningError(f"Failed to compute sighash: {e}") from e


def sign_p2wpkh_input(
    tx: ParsedTransaction,
    input_index: int,
    script_code: bytes,
    value: int,
    private_key: PrivateKey,
    sighash_type: int = 1,
) -> bytes:
    """Sign a P2WPKH input using coincurve.

    Args:
        tx: The transaction to sign
        input_index: Index of the input to sign
        script_code: The scriptCode for signing (P2PKH script for P2WPKH)
        value: The value of the input being spent (in satoshis)
        private_key: coincurve PrivateKey instance
        sighash_type: Sighash type (default SIGHASH_ALL = 1)

    Returns:
        DER-encoded signature with sighash type byte appended
    """
    sighash = compute_sighash_segwit(tx, input_index, script_code, value, sighash_type)

    # Sign the pre-hashed sighash (it's already SHA256d)
    # coincurve's sign() with hasher=None skips hashing
    signature = private_key.sign(sighash, hasher=None)

    return signature + bytes([sighash_type])


def verify_p2wpkh_signature(
    tx: ParsedTransaction,
    input_index: int,
    script_code: bytes,
    value: int,
    signature: bytes,
    pubkey: bytes,
) -> bool:
    """Verify a P2WPKH signature using coincurve.

    Args:
        tx: The transaction containing the input
        input_index: Index of the input to verify
        script_code: The scriptCode (P2PKH script for P2WPKH)
        value: The value of the input being spent (in satoshis)
        signature: DER-encoded signature with sighash type byte appended
        pubkey: Public key bytes (compressed or uncompressed)

    Returns:
        True if signature is valid, False otherwise
    """
    from coincurve import PublicKey

    try:
        # Extract sighash type from last byte of signature
        if not signature:
            return False
        sighash_type = signature[-1]
        der_signature = signature[:-1]

        sighash = compute_sighash_segwit(tx, input_index, script_code, value, sighash_type)

        # Verify signature against sighash
        # coincurve verify(signature, message, hasher=None)
        public_key = PublicKey(pubkey)
        return public_key.verify(der_signature, sighash, hasher=None)
    except Exception:
        return False


def create_witness_stack(signature: bytes, pubkey_bytes: bytes) -> list[bytes]:
    return [signature, pubkey_bytes]


def sign_p2wsh_input(
    tx: ParsedTransaction,
    input_index: int,
    witness_script: bytes,
    value: int,
    private_key: PrivateKey,
    sighash_type: int = 1,
) -> bytes:
    """Sign a P2WSH input using coincurve.

    For P2WSH, the scriptCode in BIP143 signing is the witness script itself.

    Args:
        tx: The transaction to sign
        input_index: Index of the input to sign
        witness_script: The witness script (e.g., timelocked freeze script)
        value: The value of the input being spent (in satoshis)
        private_key: coincurve PrivateKey instance
        sighash_type: Sighash type (default SIGHASH_ALL = 1)

    Returns:
        DER-encoded signature with sighash type byte appended
    """
    # For P2WSH, the scriptCode is the witness script itself
    sighash = compute_sighash_segwit(tx, input_index, witness_script, value, sighash_type)

    # Sign the pre-hashed sighash (it's already SHA256d)
    signature = private_key.sign(sighash, hasher=None)

    return signature + bytes([sighash_type])


def create_p2wsh_witness_stack(signature: bytes, witness_script: bytes) -> list[bytes]:
    """Create witness stack for P2WSH input.

    For timelocked scripts (CLTV), the witness is: [signature, witness_script]

    Args:
        signature: DER signature with sighash byte
        witness_script: The witness script (e.g., freeze script)

    Returns:
        Witness stack: [signature, witness_script]
    """
    return [signature, witness_script]


def sign_p2tr_input(
    tx: ParsedTransaction,
    input_index: int,
    prevouts_values: list[int],
    prevouts_scripts: list[bytes],
    private_key: PrivateKey,
    sighash_type: int = SIGHASH_DEFAULT,
) -> bytes:
    """Sign a P2TR (Taproot) key-path spend input using coincurve.

    Args:
        tx: The transaction to sign
        input_index: Index of the input to sign
        prevouts_values: List of all input values being spent (in satoshis)
        prevouts_scripts: List of all input scriptPubKeys being spent
        private_key: coincurve PrivateKey instance
        sighash_type: Sighash type (default SIGHASH_DEFAULT = 0)

    Returns:
        64-byte Schnorr signature (or 65 bytes if sighash_type != DEFAULT)
    """
    sighash = compute_sighash_taproot(
        tx, input_index, prevouts_values, prevouts_scripts, sighash_type
    )

    # BIP340 Schnorr signing
    # coincurve's sign_schnorr()
    signature = private_key.sign_schnorr(sighash)

    if sighash_type != SIGHASH_DEFAULT:
        signature += bytes([sighash_type])

    return signature


def verify_p2tr_signature(
    tx: ParsedTransaction,
    input_index: int,
    prevouts_values: list[int],
    prevouts_scripts: list[bytes],
    signature: bytes,
    x_only_pubkey: bytes,
) -> bool:
    """Verify a BIP341 Taproot (Schnorr) signature for a key-path spend.

    Args:
        tx: The parsed transaction.
        input_index: Index of the input being verified.
        prevouts_values: Values of ALL prevouts (not just the one being signed).
        prevouts_scripts: ScriptPubKeys of ALL prevouts.
        signature: 64-byte Schnorr signature (or 65 with sighash byte).
        x_only_pubkey: 32-byte x-only public key from the P2TR scriptPubKey.

    Returns:
        True if the signature is valid, False otherwise.
    """
    try:
        from coincurve import PublicKeyXOnly

        if len(signature) == 65:
            sighash_type = signature[-1]
            raw_sig = signature[:64]
        else:
            sighash_type = SIGHASH_DEFAULT
            raw_sig = signature

        sighash = compute_sighash_taproot(
            tx, input_index, prevouts_values, prevouts_scripts, sighash_type
        )

        xonly_pub = PublicKeyXOnly(x_only_pubkey)
        return bool(xonly_pub.verify(raw_sig, sighash))
    except Exception:
        return False


def compute_sighash_taproot(
    tx: ParsedTransaction,
    input_index: int,
    prevouts_values: list[int],
    prevouts_scripts: list[bytes],
    sighash_type: int = SIGHASH_DEFAULT,
) -> bytes:
    """Compute Taproot (BIP341) sighash for an input.

    Only implements key-path spends (no annex, no script path).
    """
    if input_index >= len(tx.inputs):
        raise TransactionSigningError("Input index out of range")

    if len(prevouts_values) != len(tx.inputs) or len(prevouts_scripts) != len(tx.inputs):
        raise TransactionSigningError("Prevouts length must match inputs length")

    import struct

    # Common hashes
    hash_prevouts = b""
    hash_amounts = b""
    hash_script_pubkeys = b""
    hash_sequences = b""

    # BIP341 Common parts
    if not (sighash_type & SIGHASH_ANYONECANPAY):
        hash_prevouts = sha256(
            b"".join(inp.txid_le + inp.vout.to_bytes(4, "little") for inp in tx.inputs)
        )
        hash_amounts = sha256(b"".join(val.to_bytes(8, "little") for val in prevouts_values))
        hash_script_pubkeys = sha256(b"".join(encode_varint(len(s)) + s for s in prevouts_scripts))
        hash_sequences = sha256(b"".join(inp.sequence_bytes for inp in tx.inputs))

    hash_outputs = b""
    if (sighash_type & 0x03) != SIGHASH_NONE and (sighash_type & 0x03) != SIGHASH_SINGLE:
        hash_outputs = sha256(
            b"".join(
                out.value.to_bytes(8, "little") + encode_varint(len(out.script)) + out.script
                for out in tx.outputs
            )
        )

    # Construction of preimage
    # 1. Control (epoch)
    preimage = bytes([0x00])

    # 2. Sighash type
    preimage += bytes([sighash_type])

    # 3. Version
    preimage += tx.version_bytes

    # 4. Locktime
    preimage += tx.locktime_bytes

    # common portions
    if not (sighash_type & SIGHASH_ANYONECANPAY):
        preimage += hash_prevouts
        preimage += hash_amounts
        preimage += hash_script_pubkeys
        preimage += hash_sequences

    if (sighash_type & 0x03) != SIGHASH_NONE and (sighash_type & 0x03) != SIGHASH_SINGLE:
        preimage += hash_outputs

    # 10. spend_type (bits 0=annex, 1=script_path)
    # We only support key-path with no annex
    spend_type = 0x00
    preimage += bytes([spend_type])

    # 11. input specific data
    if sighash_type & SIGHASH_ANYONECANPAY:
        target_input = tx.inputs[input_index]
        preimage += target_input.txid_le + target_input.vout.to_bytes(4, "little")
        preimage += prevouts_values[input_index].to_bytes(8, "little")
        preimage += (
            encode_varint(len(prevouts_scripts[input_index])) + prevouts_scripts[input_index]
        )
        preimage += target_input.sequence_bytes
    else:
        preimage += struct.pack("<I", input_index)

    # annex would go here (not supported)

    if (sighash_type & 0x03) == SIGHASH_SINGLE:
        if input_index < len(tx.outputs):
            out = tx.outputs[input_index]
            preimage += sha256(
                out.value.to_bytes(8, "little") + encode_varint(len(out.script)) + out.script
            )
        else:
            preimage += b"\x00" * 32

    return tagged_hash("TapSighash", preimage)


# Re-export from jmcore for backward compatibility
__all__ = [
    "ParsedTransaction",
    "Transaction",
    "TransactionSigningError",
    "TxInput",
    "TxOutput",
    "compute_sighash_segwit",
    "create_p2wpkh_script_code",
    "create_p2wsh_witness_stack",
    "create_witness_stack",
    "deserialize_transaction",
    "encode_varint",
    "hash256",
    "read_varint",
    "sign_p2wpkh_input",
    "sign_p2wsh_input",
    "verify_p2wpkh_signature",
    "SIGHASH_ALL",
    "SIGHASH_NONE",
    "SIGHASH_SINGLE",
    "SIGHASH_ANYONECANPAY",
    "SIGHASH_DEFAULT",
    "compute_sighash_taproot",
    "sign_p2tr_input",
    "verify_p2tr_signature",
]
