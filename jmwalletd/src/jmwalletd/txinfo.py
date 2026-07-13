"""Shared helper to build the ``TxInfo`` response/notification model.

Used by both the direct-send endpoint and the background transaction monitor
so the WebSocket ``txdetails`` payload has a single, consistent shape.
"""

from __future__ import annotations

from jmcore.bitcoin import encode_varint, get_txid, parse_transaction
from jmwalletd.models import TxInfo, TxInput, TxOutput


def build_txinfo_from_hex(
    tx_hex: str,
    network: str,
    *,
    txid: str | None = None,
    confirmations: int | None = None,
) -> TxInfo:
    """Decode a raw transaction hex into a :class:`TxInfo`.

    Args:
        tx_hex: Raw transaction hex.
        network: Network name for address derivation (``mainnet``/``regtest``/...).
        txid: Known txid; computed from ``tx_hex`` when omitted.
        confirmations: Confirmation count to surface in ``txdetails`` (``None``
            for a freshly broadcast transaction).
    """
    parsed = parse_transaction(tx_hex)

    inputs: list[TxInput] = []
    for i, tin in enumerate(parsed.inputs):
        witness = ""
        if i < len(parsed.witnesses):
            stack = parsed.witnesses[i]
            witness = (
                encode_varint(len(stack))
                + b"".join(encode_varint(len(item)) + item for item in stack)
            ).hex()
        inputs.append(
            TxInput(
                outpoint=f"{tin.txid}:{tin.vout}",
                scriptSig=tin.scriptsig_hex,
                nSequence=tin.sequence,
                witness=witness,
            )
        )

    outputs: list[TxOutput] = []
    for out in parsed.outputs:
        try:
            address = out.address(network)
        except Exception:
            address = ""
        outputs.append(
            TxOutput(value_sats=out.value, scriptPubKey=out.scriptpubkey, address=address)
        )

    return TxInfo(
        hex=tx_hex,
        inputs=inputs,
        outputs=outputs,
        txid=txid or get_txid(tx_hex),
        nLockTime=parsed.locktime,
        nVersion=parsed.version,
        confirmations=confirmations,
    )
