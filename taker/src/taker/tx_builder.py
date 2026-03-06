"""
Transaction builder for CoinJoin transactions.

Builds the unsigned CoinJoin transaction from:
- Taker's UTXOs and change address
- Maker UTXOs, CJ addresses, and change addresses
- CoinJoin amount and fees
"""

from __future__ import annotations

import logging
from typing import Any

from jmcore.bitcoin import (
    TxInput,
    TxOutput,
    encode_varint,
    hash256,
    parse_transaction_bytes,
    serialize_transaction,
)
from pydantic.dataclasses import dataclass

logger = logging.getLogger(__name__)


# Alias for backward compatibility
varint = encode_varint


@dataclass
class CoinJoinTxData:
    """Data for building a CoinJoin transaction."""

    # Taker data
    taker_inputs: list[TxInput]
    taker_cj_output: TxOutput
    taker_change_output: TxOutput | None

    # Maker data (by nick)
    maker_inputs: dict[str, list[TxInput]]
    maker_cj_outputs: dict[str, TxOutput]
    maker_change_outputs: dict[str, TxOutput]

    # Amounts
    cj_amount: int
    total_maker_fee: int
    tx_fee: int


class CoinJoinTxBuilder:
    """
    Builds CoinJoin transactions.

    The transaction structure:
    - Inputs: Taker inputs + Maker inputs (shuffled)
    - Outputs: Equal CJ outputs + Change outputs (shuffled)
    """

    def __init__(self, network: str = "mainnet"):
        self.network = network

    def build_unsigned_tx(self, tx_data: CoinJoinTxData) -> tuple[bytes, dict[str, Any]]:
        """
        Build an unsigned CoinJoin transaction.

        Args:
            tx_data: Transaction data with all inputs and outputs

        Returns:
            (tx_bytes, metadata) where metadata maps inputs/outputs to owners
        """
        import random

        # Collect all inputs with owner info
        all_inputs: list[tuple[TxInput, str]] = []

        for inp in tx_data.taker_inputs:
            all_inputs.append((inp, "taker"))

        for nick, inputs in tx_data.maker_inputs.items():
            for inp in inputs:
                all_inputs.append((inp, nick))

        # Collect all outputs with owner info
        all_outputs: list[tuple[TxOutput, str, str]] = []  # (output, owner, type)

        # CJ outputs (equal amounts)
        all_outputs.append((tx_data.taker_cj_output, "taker", "cj"))
        for nick, out in tx_data.maker_cj_outputs.items():
            all_outputs.append((out, nick, "cj"))

        # Change outputs
        if tx_data.taker_change_output:
            all_outputs.append((tx_data.taker_change_output, "taker", "change"))
        for nick, out in tx_data.maker_change_outputs.items():
            all_outputs.append((out, nick, "change"))

        # Shuffle for privacy
        random.shuffle(all_inputs)
        random.shuffle(all_outputs)

        # Build metadata
        metadata = {
            "input_owners": [owner for _, owner in all_inputs],
            "output_owners": [(owner, out_type) for _, owner, out_type in all_outputs],
            "input_values": [inp.value for inp, _ in all_inputs],
            "fee": tx_data.tx_fee,
        }

        # Serialize transaction
        tx_bytes = self._serialize_tx(
            inputs=[inp for inp, _ in all_inputs],
            outputs=[out for out, _, _ in all_outputs],
        )

        return tx_bytes, metadata

    def _serialize_tx(self, inputs: list[TxInput], outputs: list[TxOutput]) -> bytes:
        """Serialize transaction to bytes.

        For unsigned transactions, we use non-SegWit format (no marker/flag/witness).
        The SegWit marker (0x00, 0x01) is only added when witnesses are present.
        """
        return serialize_transaction(
            version=2,
            inputs=inputs,
            outputs=outputs,
            locktime=0,
            witnesses=None,
        )

    def add_signatures(
        self,
        tx_bytes: bytes,
        signatures: dict[str, list[dict[str, Any]]],
        metadata: dict[str, Any],
    ) -> bytes:
        """
        Add signatures to transaction.

        Every input must have a matching signature. A CoinJoin transaction with
        any unsigned input is invalid and must never be broadcast.

        Args:
            tx_bytes: Unsigned transaction
            signatures: Dict of nick -> list of signature info
            metadata: Transaction metadata with input owners

        Returns:
            Signed transaction bytes

        Raises:
            ValueError: If any input is missing a signature
        """
        from loguru import logger as log

        # Parse unsigned tx using jmcore
        parsed = parse_transaction_bytes(tx_bytes)

        log.debug(f"add_signatures: {len(parsed.inputs)} inputs, {len(parsed.outputs)} outputs")
        log.debug(f"input_owners: {metadata.get('input_owners', [])}")
        log.debug(f"signatures keys: {list(signatures.keys())}")

        # Build witness data
        new_witnesses: list[list[bytes]] = []
        input_owners = metadata["input_owners"]
        unsigned_inputs: list[str] = []

        for i, owner in enumerate(input_owners):
            inp = parsed.inputs[i]
            log.debug(f"Input {i}: owner={owner}, txid={inp.txid[:16]}..., vout={inp.vout}")

            if owner in signatures:
                # Find matching signature
                for sig_info in signatures[owner]:
                    if sig_info.get("txid") == inp.txid and sig_info.get("vout") == inp.vout:
                        witness = sig_info.get("witness", [])
                        new_witnesses.append([bytes.fromhex(w) for w in witness])
                        log.debug(f"  -> Found matching signature, witness len={len(witness)}")
                        break
                else:
                    unsigned_inputs.append(
                        f"input {i} (owner={owner}, txid={inp.txid[:16]}...:{inp.vout})"
                    )
                    new_witnesses.append([])
            else:
                unsigned_inputs.append(
                    f"input {i} (owner={owner}, txid={inp.txid[:16]}...:{inp.vout})"
                )
                new_witnesses.append([])

        if unsigned_inputs:
            raise ValueError(
                f"Cannot assemble transaction: {len(unsigned_inputs)} input(s) missing "
                f"signatures: {', '.join(unsigned_inputs)}. "
                f"All inputs must be signed for a valid transaction."
            )

        # Reserialize with witnesses using jmcore
        return serialize_transaction(
            version=parsed.version,
            inputs=parsed.inputs,
            outputs=parsed.outputs,
            locktime=parsed.locktime,
            witnesses=new_witnesses,
        )

    def get_txid(self, tx_bytes: bytes) -> str:
        """Calculate txid (double SHA256 of non-witness data)."""
        parsed = parse_transaction_bytes(tx_bytes)

        # Serialize without witness for txid calculation
        data = serialize_transaction(
            version=parsed.version,
            inputs=parsed.inputs,
            outputs=parsed.outputs,
            locktime=parsed.locktime,
            witnesses=None,
        )

        return hash256(data)[::-1].hex()


def calculate_tx_fee(
    num_taker_inputs: int,
    num_maker_inputs: int,
    num_outputs: int,
    fee_rate: float,
) -> int:
    """
    Calculate transaction fee based on estimated vsize.

    SegWit P2WPKH inputs: ~68 vbytes each
    P2WPKH outputs: 31 vbytes each
    Overhead: ~11 vbytes

    Args:
        fee_rate: Fee rate in sat/vB (can be fractional, e.g. 0.5)

    Returns:
        Fee in satoshis (rounded up to ensure minimum relay fee)
    """
    # Estimate virtual size
    input_vsize = (num_taker_inputs + num_maker_inputs) * 68
    output_vsize = num_outputs * 31
    overhead = 11

    vsize = input_vsize + output_vsize + overhead

    # Round up to ensure we pay at least the minimum
    import math

    return math.ceil(vsize * fee_rate)


def build_coinjoin_tx(
    # Taker data
    taker_utxos: list[dict[str, Any]],
    taker_cj_address: str,
    taker_change_address: str,
    taker_total_input: int,
    # Maker data
    maker_data: dict[str, dict[str, Any]],  # nick -> {utxos, cj_addr, change_addr, cjfee, txfee}
    # Amounts
    cj_amount: int,
    tx_fee: int,
    network: str = "mainnet",
    dust_threshold: int = 27300,  # Default to DUST_THRESHOLD from jmcore.constants
) -> tuple[bytes, dict[str, Any]]:
    """
    Build a complete CoinJoin transaction.

    Args:
        taker_utxos: List of taker's UTXOs
        taker_cj_address: Taker's CJ output address
        taker_change_address: Taker's change address (empty string if no change needed)
        taker_total_input: Total value of taker's inputs
        maker_data: Dict of maker nick -> {utxos, cj_addr, change_addr, cjfee, txfee}
        cj_amount: Equal CoinJoin output amount
        tx_fee: Total transaction fee
        network: Network name
        dust_threshold: Minimum output value in satoshis (default: 27300)

    Returns:
        (tx_bytes, metadata)
    """
    try:
        builder = CoinJoinTxBuilder(network)

        # Build taker inputs
        taker_inputs = []
        for i, u in enumerate(taker_utxos):
            if u.get("txid") is None:
                logger.error(f"Taker UTXO {i} has None txid")
            if u.get("value") is None:
                logger.error(f"Taker UTXO {i} has None value")
            taker_inputs.append(
                TxInput.from_hex(
                    txid=u["txid"],
                    vout=u["vout"],
                    value=u["value"],
                    scriptpubkey=u.get("scriptpubkey", ""),
                )
            )

        # Calculate taker's fees paid to makers
        total_maker_fee = sum(m["cjfee"] for m in maker_data.values())

        # Taker's change = total_input - cj_amount - maker_fees - tx_fee
        taker_change = taker_total_input - cj_amount - total_maker_fee - tx_fee

        # Taker CJ output
        taker_cj_output = TxOutput.from_address(taker_cj_address, cj_amount)

        # Taker change output (if any)
        taker_change_output = None
        if taker_change > dust_threshold and taker_change_address:
            taker_change_output = TxOutput.from_address(taker_change_address, taker_change)
        elif taker_change > 0:
            logger.warning(
                f"Taker change {taker_change} sats "
                + (
                    "has no address (sweep mode)"
                    if not taker_change_address
                    else f"is below dust threshold ({dust_threshold})"
                )
                + ", no change output will be created"
            )

        # Build maker data
        maker_inputs: dict[str, list[TxInput]] = {}
        maker_cj_outputs: dict[str, TxOutput] = {}
        maker_change_outputs: dict[str, TxOutput] = {}

        for nick, data in maker_data.items():
            # Maker inputs
            inputs = []
            for i, u in enumerate(data["utxos"]):
                if u.get("txid") is None:
                    logger.error(f"Maker {nick} UTXO {i} has None txid")
                if u.get("value") is None:
                    logger.error(f"Maker {nick} UTXO {i} has None value")
                inputs.append(
                    TxInput.from_hex(
                        txid=u["txid"],
                        vout=u["vout"],
                        value=u["value"],
                        scriptpubkey=u.get("scriptpubkey", ""),
                    )
                )
            maker_inputs[nick] = inputs

            # Maker CJ output (cj_amount)
            maker_cj_outputs[nick] = TxOutput.from_address(data["cj_addr"], cj_amount)

            # Maker change output
            # Formula: change = inputs - cj_amount - txfee + cjfee
            # (Maker pays txfee, receives cjfee from taker)
            maker_total_input = sum(u["value"] for u in data["utxos"])
            maker_txfee = data.get("txfee", 0)
            maker_change = maker_total_input - cj_amount - maker_txfee + data["cjfee"]

            logger.debug(
                f"Maker {nick} change calculation: "
                f"inputs={maker_total_input}, cj_amount={cj_amount}, "
                f"cjfee={data['cjfee']}, txfee={maker_txfee}, change={maker_change}, "
                f"dust_threshold={dust_threshold}"
            )

            if maker_change < 0:
                # Negative change means maker's UTXOs are insufficient
                # This can happen if UTXO verification failed (value=0) or if UTXOs were spent
                raise ValueError(
                    f"Maker {nick} has insufficient funds: inputs={maker_total_input} sats, "
                    f"required={cj_amount + maker_txfee - data['cjfee']} sats, "
                    f"change={maker_change} sats. Maker's UTXOs may have been spent."
                )
            elif maker_change > dust_threshold:
                maker_change_outputs[nick] = TxOutput.from_address(data["change_addr"], maker_change)
            else:
                logger.warning(
                    f"Maker {nick} change {maker_change} sats is below dust threshold "
                    f"({dust_threshold}), "
                    "no change output will be created"
                )

        tx_data = CoinJoinTxData(
            taker_inputs=taker_inputs,
            taker_cj_output=taker_cj_output,
            taker_change_output=taker_change_output,
            maker_inputs=maker_inputs,
            maker_cj_outputs=maker_cj_outputs,
            maker_change_outputs=maker_change_outputs,
            cj_amount=cj_amount,
            total_maker_fee=total_maker_fee,
            tx_fee=tx_fee,
        )

        return builder.build_unsigned_tx(tx_data)
    except Exception as e:
        import traceback

        logger.error(f"Failed to build transaction: {e}")
        logger.error(traceback.format_exc())
        raise
