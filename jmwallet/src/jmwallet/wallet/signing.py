"""
Bitcoin transaction signing utilities for P2WPKH and P2WSH inputs.

BIP-143 sighash computation delegates to ``python-bitcointx``'s
``SignatureHash`` so consensus-critical preimage construction lives in
an audited library. The transaction is serialized via the in-tree
``ParsedTransaction`` and handed to bitcointx's ``CTransaction``.
"""

from __future__ import annotations

from bitcointx.core import CTransaction
from bitcointx.core.script import SIGVERSION_WITNESS_V0, CScript, SignatureHash
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
    serialize_transaction,
    sha256,
    tagged_hash,
)

# BIP341 / BIP342 sighash type flags.
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
    """Compute the BIP-143 sighash for a segwit input.

    Delegates to :func:`bitcointx.core.script.SignatureHash` with
    ``SIGVERSION_WITNESS_V0``. The ``ParsedTransaction`` is re-serialized
    and parsed by bitcointx so we never re-implement the BIP-143 preimage
    layout in-tree.
    """
    try:
        if input_index >= len(tx.inputs):
            raise TransactionSigningError("Input index out of range")

        ctx = CTransaction.deserialize(
            serialize_transaction(tx.version, tx.inputs, tx.outputs, tx.locktime)
        )
        return bytes(
            SignatureHash(
                CScript(script_code),
                ctx,
                input_index,
                sighash_type,
                amount=value,
                sigversion=SIGVERSION_WITNESS_V0,
            )
        )

    except TransactionSigningError:
        raise
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
    if sighash_type != 1:
        raise TransactionSigningError(
            f"Unsupported sighash type {sighash_type}; only SIGHASH_ALL (0x01) allowed for signing"
        )

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
    if sighash_type != 1:
        raise TransactionSigningError(
            f"Unsupported sighash type {sighash_type}; only SIGHASH_ALL (0x01) allowed for signing"
        )

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


def compute_sighash_taproot(
    tx: ParsedTransaction,
    input_index: int,
    prevouts_values: list[int],
    prevouts_scripts: list[bytes],
    sighash_type: int = SIGHASH_DEFAULT,
) -> bytes:
    """Compute the BIP341 Taproot sighash for a key-path spend input.

    Only key-path spends are supported (no annex, no script path). All
    prevout values and scriptPubKeys must be provided because Taproot commits
    to every spent output's amount and script.
    """
    if input_index >= len(tx.inputs):
        raise TransactionSigningError("Input index out of range")
    if len(prevouts_values) != len(tx.inputs) or len(prevouts_scripts) != len(tx.inputs):
        raise TransactionSigningError("Prevouts length must match inputs length")

    import struct

    hash_prevouts = b""
    hash_amounts = b""
    hash_script_pubkeys = b""
    hash_sequences = b""
    if not (sighash_type & SIGHASH_ANYONECANPAY):
        hash_prevouts = sha256(
            b"".join(inp.txid_le + inp.vout.to_bytes(4, "little") for inp in tx.inputs)
        )
        hash_amounts = sha256(b"".join(val.to_bytes(8, "little") for val in prevouts_values))
        hash_script_pubkeys = sha256(b"".join(encode_varint(len(s)) + s for s in prevouts_scripts))
        hash_sequences = sha256(b"".join(inp.sequence_bytes for inp in tx.inputs))

    hash_outputs = b""
    if (sighash_type & 0x03) not in (SIGHASH_NONE, SIGHASH_SINGLE):
        hash_outputs = sha256(
            b"".join(
                out.value.to_bytes(8, "little") + encode_varint(len(out.script)) + out.script
                for out in tx.outputs
            )
        )

    preimage = bytes([0x00])  # epoch
    preimage += bytes([sighash_type])
    preimage += tx.version_bytes
    preimage += tx.locktime_bytes
    if not (sighash_type & SIGHASH_ANYONECANPAY):
        preimage += hash_prevouts + hash_amounts + hash_script_pubkeys + hash_sequences
    if (sighash_type & 0x03) not in (SIGHASH_NONE, SIGHASH_SINGLE):
        preimage += hash_outputs

    spend_type = 0x00  # key-path, no annex
    preimage += bytes([spend_type])

    if sighash_type & SIGHASH_ANYONECANPAY:
        target = tx.inputs[input_index]
        preimage += target.txid_le + target.vout.to_bytes(4, "little")
        preimage += prevouts_values[input_index].to_bytes(8, "little")
        preimage += (
            encode_varint(len(prevouts_scripts[input_index])) + prevouts_scripts[input_index]
        )
        preimage += target.sequence_bytes
    else:
        preimage += struct.pack("<I", input_index)

    if (sighash_type & 0x03) == SIGHASH_SINGLE:
        if input_index < len(tx.outputs):
            out = tx.outputs[input_index]
            preimage += sha256(
                out.value.to_bytes(8, "little") + encode_varint(len(out.script)) + out.script
            )
        else:
            preimage += b"\x00" * 32

    return tagged_hash("TapSighash", preimage)


def sign_p2tr_input(
    tx: ParsedTransaction,
    input_index: int,
    prevouts_values: list[int],
    prevouts_scripts: list[bytes],
    private_key: PrivateKey,
    sighash_type: int = SIGHASH_DEFAULT,
) -> bytes:
    """Sign a P2TR key-path spend input, returning a BIP340 Schnorr signature.

    For silent payment outputs the ``private_key`` is the recovered output key
    (``b_spend + tweak``); it is already the final taproot output key, so no
    additional BIP341 TapTweak is applied here.
    """
    sighash = compute_sighash_taproot(
        tx, input_index, prevouts_values, prevouts_scripts, sighash_type
    )
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
    """Verify a BIP341 Taproot (Schnorr) key-path signature."""
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
        return bool(PublicKeyXOnly(x_only_pubkey).verify(raw_sig, sighash))
    except Exception:  # noqa: BLE001 - verification must never raise
        return False


# Re-export from jmcore for backward compatibility
__all__ = [
    "SIGHASH_ALL",
    "SIGHASH_ANYONECANPAY",
    "SIGHASH_DEFAULT",
    "SIGHASH_NONE",
    "SIGHASH_SINGLE",
    "ParsedTransaction",
    "Transaction",
    "TransactionSigningError",
    "TxInput",
    "TxOutput",
    "compute_sighash_segwit",
    "compute_sighash_taproot",
    "create_p2wpkh_script_code",
    "create_p2wsh_witness_stack",
    "create_witness_stack",
    "deserialize_transaction",
    "encode_varint",
    "hash256",
    "read_varint",
    "sign_p2tr_input",
    "sign_p2wpkh_input",
    "sign_p2wsh_input",
    "verify_p2tr_signature",
    "verify_p2wpkh_signature",
]
