"""
Bitcoin transaction signing utilities for P2WPKH and P2WSH inputs.

BIP-143 sighash computation delegates to ``python-bitcointx``'s
``SignatureHash`` so consensus-critical preimage construction lives in
an audited library. The transaction is serialized via the in-tree
``ParsedTransaction`` and handed to bitcointx's ``CTransaction``.
"""

from __future__ import annotations

from bitcointx.core import CTransaction, CTxOut
from bitcointx.core.key import CKey, XOnlyPubKey
from bitcointx.core.script import (
    SIGVERSION_TAPROOT,
    SIGVERSION_WITNESS_V0,
    CScript,
    SIGHASH_Type,
    SignatureHash,
    SignatureHashSchnorr,
)
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
)
from jmcore.crypto import verify_strict_ecdsa

# BIP341 sighash type flags.
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
                SIGHASH_Type(sighash_type),
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
    private_key: CKey,
    sighash_type: int = 1,
) -> bytes:
    """Sign a P2WPKH input.

    Args:
        tx: The transaction to sign
        input_index: Index of the input to sign
        script_code: The scriptCode for signing (P2PKH script for P2WPKH)
        value: The value of the input being spent (in satoshis)
        private_key: Signing private key
        sighash_type: Sighash type (default SIGHASH_ALL = 1)

    Returns:
        DER-encoded signature with sighash type byte appended
    """
    if sighash_type != 1:
        raise TransactionSigningError(
            f"Unsupported sighash type {sighash_type}; only SIGHASH_ALL (0x01) allowed for signing"
        )

    sighash = compute_sighash_segwit(tx, input_index, script_code, value, sighash_type)

    # Sign the pre-hashed sighash (it is already SHA256d).
    signature = private_key.sign(sighash, _ecdsa_sig_grind_low_r=False)

    return signature + bytes([sighash_type])


def verify_p2wpkh_signature(
    tx: ParsedTransaction,
    input_index: int,
    script_code: bytes,
    value: int,
    signature: bytes,
    pubkey: bytes,
) -> bool:
    """Verify a P2WPKH signature.

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
    try:
        # Extract sighash type from last byte of signature
        if not signature:
            return False
        sighash_type = signature[-1]
        der_signature = signature[:-1]

        sighash = compute_sighash_segwit(tx, input_index, script_code, value, sighash_type)

        return verify_strict_ecdsa(sighash, der_signature, pubkey)
    except Exception:
        return False


def create_witness_stack(signature: bytes, pubkey_bytes: bytes) -> list[bytes]:
    return [signature, pubkey_bytes]


def sign_p2wsh_input(
    tx: ParsedTransaction,
    input_index: int,
    witness_script: bytes,
    value: int,
    private_key: CKey,
    sighash_type: int = 1,
) -> bytes:
    """Sign a P2WSH input.

    For P2WSH, the scriptCode in BIP143 signing is the witness script itself.

    Args:
        tx: The transaction to sign
        input_index: Index of the input to sign
        witness_script: The witness script (e.g., timelocked freeze script)
        value: The value of the input being spent (in satoshis)
        private_key: Signing private key
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

    # Sign the pre-hashed sighash (it is already SHA256d).
    signature = private_key.sign(sighash, _ecdsa_sig_grind_low_r=False)

    return signature + bytes([sighash_type])


def verify_p2wsh_signature(
    tx: ParsedTransaction,
    input_index: int,
    witness_script: bytes,
    value: int,
    signature: bytes,
    pubkey: bytes,
) -> bool:
    """Verify a P2WSH BIP143 signature against its witness script."""
    try:
        if not signature:
            return False
        sighash_type = signature[-1]
        sighash = compute_sighash_segwit(tx, input_index, witness_script, value, sighash_type)
        return verify_strict_ecdsa(sighash, signature[:-1], pubkey)
    except Exception:
        return False


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
    """Compute the BIP341 sighash for a key-path spend."""
    if input_index < 0 or input_index >= len(tx.inputs):
        raise TransactionSigningError("Input index out of range")
    if len(prevouts_values) != len(tx.inputs) or len(prevouts_scripts) != len(tx.inputs):
        raise TransactionSigningError("Prevouts length must match inputs length")
    native_sighash_type = _native_taproot_sighash_type(sighash_type)
    if (sighash_type & 0x03) == SIGHASH_SINGLE and input_index >= len(tx.outputs):
        raise TransactionSigningError("SIGHASH_SINGLE requires a corresponding output")

    try:
        native_tx = CTransaction.deserialize(
            serialize_transaction(tx.version, tx.inputs, tx.outputs, tx.locktime)
        )
        spent_outputs = _taproot_spent_outputs(prevouts_values, prevouts_scripts)
        return bytes(
            SignatureHashSchnorr(
                native_tx,
                input_index,
                spent_outputs,
                hashtype=native_sighash_type,
                sigversion=SIGVERSION_TAPROOT,
            )
        )
    except TransactionSigningError:
        raise
    except (TypeError, ValueError) as exc:
        raise TransactionSigningError(f"Failed to compute Taproot sighash: {exc}") from exc


def sign_p2tr_input(
    tx: ParsedTransaction,
    input_index: int,
    prevouts_values: list[int],
    prevouts_scripts: list[bytes],
    private_key: CKey,
    sighash_type: int = SIGHASH_DEFAULT,
) -> bytes:
    """Sign a P2TR key-path input with its already-tweaked output key."""
    if not isinstance(private_key, CKey):
        raise TransactionSigningError("Taproot signing key must be a CKey")
    sighash = compute_sighash_taproot(
        tx, input_index, prevouts_values, prevouts_scripts, sighash_type
    )
    signature = private_key.sign_schnorr_no_tweak(sighash)
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
    """Verify a BIP341 key-path Schnorr signature."""
    try:
        if len(signature) == 64:
            sighash_type = SIGHASH_DEFAULT
            raw_signature = signature
        elif len(signature) == 65 and signature[-1] != SIGHASH_DEFAULT:
            sighash_type = signature[-1]
            raw_signature = signature[:64]
        else:
            return False
        sighash = compute_sighash_taproot(
            tx, input_index, prevouts_values, prevouts_scripts, sighash_type
        )
        return XOnlyPubKey(x_only_pubkey).verify_schnorr(sighash, raw_signature)
    except Exception:  # noqa: BLE001 - verification must never raise
        return False


def _native_taproot_sighash_type(sighash_type: int) -> SIGHASH_Type | None:
    if not isinstance(sighash_type, int) or isinstance(sighash_type, bool):
        raise TransactionSigningError("Taproot sighash type must be an integer")
    valid_sighash_types = {
        SIGHASH_DEFAULT,
        SIGHASH_ALL,
        SIGHASH_NONE,
        SIGHASH_SINGLE,
        SIGHASH_ANYONECANPAY | SIGHASH_ALL,
        SIGHASH_ANYONECANPAY | SIGHASH_NONE,
        SIGHASH_ANYONECANPAY | SIGHASH_SINGLE,
    }
    if sighash_type not in valid_sighash_types:
        raise TransactionSigningError(f"Invalid Taproot sighash type: {sighash_type:#x}")
    return None if sighash_type == SIGHASH_DEFAULT else SIGHASH_Type(sighash_type)


def _taproot_spent_outputs(values: list[int], scripts: list[bytes]) -> list[CTxOut]:
    outputs: list[CTxOut] = []
    for index, (value, scriptpubkey) in enumerate(zip(values, scripts, strict=True)):
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**63:
            raise TransactionSigningError(
                f"Taproot prevout value at index {index} is outside the signed 64-bit range"
            )
        if not isinstance(scriptpubkey, bytes):
            raise TransactionSigningError(f"Taproot prevout script at index {index} must be bytes")
        outputs.append(CTxOut(value, CScript(scriptpubkey)))
    return outputs


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
    "verify_p2wsh_signature",
]
