"""Strict parsing and verification of typed maker signature responses."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from jmcore.bitcoin import parse_transaction_bytes
from jmcore.taproot import SIGHASH_DEFAULT, TaprootTx, TxIn, TxOut, taproot_sighash, verify_schnorr

_P2TR_RESPONSE_RE = re.compile(r"^p2tr:([0-9a-f]{64}):(0|[1-9][0-9]*):([0-9a-fA-F]{128})$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class P2TRSignatureFormatError(ValueError):
    """Raised when a typed P2TR maker response is malformed."""


@dataclass(frozen=True)
class P2TRMakerSignature:
    """A parsed aggregate Taproot key-path witness from a maker."""

    txid: str
    vout: int
    signature: bytes


def parse_p2tr_maker_signature(payload: str) -> P2TRMakerSignature:
    """Parse the exact ``p2tr:<txid>:<vout>:<signature_hex>`` response format."""
    match = _P2TR_RESPONSE_RE.fullmatch(payload)
    if match is None:
        raise P2TRSignatureFormatError("invalid P2TR maker signature payload")

    txid, vout_string, signature_hex = match.groups()
    vout = int(vout_string)
    if vout > 0xFFFFFFFF:
        raise P2TRSignatureFormatError("invalid P2TR maker signature payload")
    return P2TRMakerSignature(txid=txid, vout=vout, signature=bytes.fromhex(signature_hex))


def normalize_scriptpubkey(scriptpubkey: str) -> str:
    """Return a canonical nonempty hex scriptPubKey or raise a generic validation error."""
    if (
        not isinstance(scriptpubkey, str)
        or len(scriptpubkey) % 2
        or not _HEX_RE.fullmatch(scriptpubkey)
    ):
        raise ValueError("invalid scriptPubKey")
    return scriptpubkey.lower()


def verify_p2tr_keypath_signature(
    unsigned_tx: bytes,
    input_index: int,
    prevout_values: Sequence[int],
    prevout_scripts: Sequence[bytes],
    signature: bytes,
) -> bool:
    """Verify a default-sighash P2TR key-path signature against all transaction prevouts."""
    try:
        if not isinstance(input_index, int) or isinstance(input_index, bool):
            return False
        if not isinstance(signature, bytes) or len(signature) != 64:
            return False

        parsed = parse_transaction_bytes(unsigned_tx)
        if parsed.has_witness or input_index < 0 or input_index >= len(parsed.inputs):
            return False
        if len(prevout_values) != len(parsed.inputs) or len(prevout_scripts) != len(parsed.inputs):
            return False
        if any(
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
            or value > 0xFFFFFFFFFFFFFFFF
            for value in prevout_values
        ):
            return False
        if any(not isinstance(script, bytes) or not script for script in prevout_scripts):
            return False

        target_script = prevout_scripts[input_index]
        if len(target_script) != 34 or not target_script.startswith(b"\x51\x20"):
            return False

        taproot_tx = TaprootTx(
            version=parsed.version,
            inputs=[
                TxIn(txid=tx_input.txid, vout=tx_input.vout, sequence=tx_input.sequence)
                for tx_input in parsed.inputs
            ],
            outputs=[
                TxOut(value=output.value, scriptpubkey=output.script) for output in parsed.outputs
            ],
            locktime=parsed.locktime,
        )
        sighash = taproot_sighash(
            taproot_tx,
            input_index,
            list(prevout_values),
            list(prevout_scripts),
            SIGHASH_DEFAULT,
        )
        return verify_schnorr(target_script[2:], signature, sighash)
    except Exception:  # noqa: BLE001 - malformed peer data must fail closed
        return False
