"""
Transaction verification for makers.

This is THE MOST CRITICAL security component. Any bug here can result in loss of funds!

The maker must verify that the unsigned CoinJoin transaction proposed by the taker:
1. Includes all maker's UTXOs as inputs
2. Pays the correct CoinJoin amount to maker's CJ address
3. Pays the correct change amount to maker's change address
4. Results in positive profit for maker (cjfee - txfee > 0)
5. Contains no unexpected outputs
6. Is well-formed and valid

Reference: joinmarket-clientserver/src/jmclient/maker.py:verify_unsigned_tx()
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jmcore.bitcoin import (
    decode_varint,
    get_hrp,
    scriptpubkey_to_address,
)
from jmcore.bitcoin import (
    parse_transaction as parse_jmcore_transaction,
)
from jmcore.constants import DUST_THRESHOLD
from jmcore.models import NetworkType, OfferType
from jmcore.models import calculate_cj_fee as calculate_cj_fee
from jmwallet.wallet.models import UTXOInfo
from loguru import logger

# Aliases for backward compatibility
read_varint = decode_varint
get_bech32_hrp = get_hrp


class TransactionVerificationError(Exception):
    """Raised when transaction verification fails"""

    pass


VerificationResult = tuple[bool, str]


@dataclass(frozen=True)
class _EscrowDetails:
    additional_total: int
    address: str
    value: int


@dataclass(frozen=True)
class _OutputCounts:
    cj: int
    change: int
    escrow: int
    combine_total: int


def _verify_additional_inputs(
    additional_input_values: dict[tuple[str, int], int],
    our_utxo_set: set[tuple[str, int]],
    tx_inputs: list[dict[str, Any]],
) -> tuple[int, str | None]:
    if not additional_input_values:
        return 0, "Escrow verification requires a channel input"

    additional_total = 0
    for outpoint, value in additional_input_values.items():
        if (
            not isinstance(outpoint, tuple)
            or len(outpoint) != 2
            or not isinstance(outpoint[0], str)
            or not isinstance(outpoint[1], int)
            or isinstance(outpoint[1], bool)
            or not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            return 0, "Invalid additional input value"
        if outpoint in our_utxo_set:
            return 0, "Additional input must not be a wallet UTXO"
        input_count = sum(
            1 for tx_input in tx_inputs if (tx_input["txid"], tx_input["vout"]) == outpoint
        )
        if input_count != 1:
            return 0, f"Additional input {outpoint} appears {input_count} times (expected 1)"
        additional_total += value

    return additional_total, None


def _verify_escrow_output_specification(
    escrow_output: tuple[str, int],
    cj_address: str,
    change_address: str,
    combine_address: str | None,
    ring_outputs: list[tuple[str, int]] | None,
) -> tuple[tuple[str, int] | None, str | None]:
    if (
        not isinstance(escrow_output, tuple)
        or len(escrow_output) != 2
        or not isinstance(escrow_output[0], str)
        or not escrow_output[0]
        or not isinstance(escrow_output[1], int)
        or isinstance(escrow_output[1], bool)
        or escrow_output[1] <= 0
    ):
        return None, "Invalid escrow output"

    escrow_address, escrow_value = escrow_output
    if len({cj_address, change_address, escrow_address}) != 3:
        return None, "Escrow output address collides with a maker output address"
    if combine_address is not None or ring_outputs is not None:
        return None, "Escrow output cannot be combined with channel change"
    return (escrow_address, escrow_value), None


def _verify_escrow_details(
    additional_input_values: dict[tuple[str, int], int] | None,
    escrow_output: tuple[str, int] | None,
    our_utxo_set: set[tuple[str, int]],
    tx_inputs: list[dict[str, Any]],
    cj_address: str,
    change_address: str,
    combine_address: str | None,
    ring_outputs: list[tuple[str, int]] | None,
) -> tuple[_EscrowDetails | None, str | None]:
    if (additional_input_values is None) != (escrow_output is None):
        return None, "Escrow verification requires additional input values and an escrow output"
    if additional_input_values is None or escrow_output is None:
        return None, None

    additional_total, error = _verify_additional_inputs(
        additional_input_values, our_utxo_set, tx_inputs
    )
    if error is not None:
        return None, error
    output, error = _verify_escrow_output_specification(
        escrow_output, cj_address, change_address, combine_address, ring_outputs
    )
    if error is not None:
        return None, error

    assert output is not None
    return _EscrowDetails(additional_total, *output), None


def _verify_escrow_allocation(escrow: _EscrowDetails, expected_change_value: int) -> str | None:
    if expected_change_value < 0:
        return f"Escrow allocation leaves negative change: {expected_change_value} sats"
    if 0 < expected_change_value <= DUST_THRESHOLD:
        return f"Escrow allocation leaves dust change: {expected_change_value} sats"
    return None


def _verify_maker_outputs(
    tx_outputs: list[dict[str, Any]],
    cj_address: str,
    change_address: str,
    amount: int,
    expected_change_value: int,
    combine_address: str | None,
    escrow: _EscrowDetails | None,
) -> tuple[_OutputCounts | None, str | None]:
    times_seen_cj_addr = 0
    times_seen_change_addr = 0
    times_seen_escrow_addr = 0
    combine_total = 0
    escrow_address = escrow.address if escrow is not None else ""
    escrow_value = escrow.value if escrow is not None else 0

    for output in tx_outputs:
        output_addr = output["address"]
        output_value = output["value"]

        if output_addr == cj_address:
            times_seen_cj_addr += 1
            if escrow is not None and output_value != amount:
                return None, f"CJ output value {output_value} != {amount}"
            if escrow is None and output_value < amount:
                return None, f"CJ output value too low: {output_value} < {amount}"

        if output_addr == change_address:
            times_seen_change_addr += 1
            if escrow is not None and output_value != expected_change_value:
                return None, f"Change output value {output_value} != {expected_change_value}"
            if escrow is None and output_value < expected_change_value:
                return (
                    None,
                    f"Change output value too low: {output_value} < {expected_change_value}",
                )

        if output_addr == escrow_address:
            times_seen_escrow_addr += 1
            if output_value != escrow_value:
                return None, f"Escrow output value {output_value} != {escrow_value}"

        if combine_address is not None and output_addr == combine_address:
            combine_total += output_value

    return _OutputCounts(
        times_seen_cj_addr, times_seen_change_addr, times_seen_escrow_addr, combine_total
    ), None


def _verify_cj_address_count(times_seen_cj_addr: int) -> str | None:
    if times_seen_cj_addr != 1:
        return f"CJ address appears {times_seen_cj_addr} times (expected 1)"
    return None


def _verify_escrow_output_counts(counts: _OutputCounts, expected_change_value: int) -> str | None:
    if counts.escrow != 1:
        return f"Escrow address appears {counts.escrow} times (expected 1)"
    if expected_change_value == 0:
        if counts.change != 0:
            return "Change output must be absent when escrow remainder is zero"
    elif counts.change != 1:
        return f"Change address appears {counts.change} times (expected 1)"
    return None


def _verify_ring_outputs(
    ring_outputs: list[tuple[str, int]],
    tx_outputs: list[dict[str, Any]],
    expected_change_value: int,
) -> str | None:
    if not ring_outputs:
        return "Ring relay contains no outputs"
    addresses = [addr for addr, _ in ring_outputs]
    if len(set(addresses)) != len(addresses):
        return "Ring relay contains duplicate output addresses"
    if any(contrib <= 0 for _, contrib in ring_outputs):
        return "Ring contributions must be positive"
    if sum(contrib for _, contrib in ring_outputs) != expected_change_value:
        return (
            f"Ring contributions {sum(contrib for _, contrib in ring_outputs)} "
            f"!= our change {expected_change_value}"
        )

    outputs_at: dict[str, list[int]] = {}
    for output in tx_outputs:
        outputs_at.setdefault(output["address"], []).append(output["value"])
    for address, contribution in ring_outputs:
        values = outputs_at.get(address, [])
        if len(values) != 1:
            return f"Ring address {address} appears {len(values)} times (expected 1)"
        if values[0] <= contribution:
            return f"Ring output {address} value {values[0]} does not include partner funding"
    return None


def _verify_change_destination(
    counts: _OutputCounts,
    tx_outputs: list[dict[str, Any]],
    expected_change_value: int,
    combine_address: str | None,
    combine_value: int | None,
    ring_outputs: list[tuple[str, int]] | None,
) -> str | None:
    if ring_outputs is not None:
        return _verify_ring_outputs(ring_outputs, tx_outputs, expected_change_value)
    if combine_address is not None:
        required_value = combine_value or expected_change_value
        if counts.combine_total < required_value:
            return f"Shared channel output total too low: {counts.combine_total} < {required_value}"
        return None
    if counts.change != 1:
        return f"Change address appears {counts.change} times (expected 1)"
    return None


def verify_unsigned_transaction(
    tx_hex: str,
    our_utxos: dict[tuple[str, int], UTXOInfo],
    cj_address: str,
    change_address: str,
    amount: int,
    cjfee: str | int,
    txfee: int,
    offer_type: OfferType,
    network: NetworkType = NetworkType.MAINNET,
    combine_address: str | None = None,
    combine_value: int | None = None,
    ring_outputs: list[tuple[str, int]] | None = None,
    additional_input_values: dict[tuple[str, int], int] | None = None,
    escrow_output: tuple[str, int] | None = None,
) -> VerificationResult:
    """
    Verify unsigned CoinJoin transaction proposed by taker.

    CRITICAL SECURITY FUNCTION - Any bug can result in loss of funds!

    Args:
        tx_hex: Unsigned transaction hex
        our_utxos: Our UTXOs that should be in the transaction
        cj_address: Our CoinJoin output address
        change_address: Our change output address
        amount: CoinJoin amount (satoshis)
        cjfee: CoinJoin fee (format depends on offer_type)
        txfee: Transaction fee we're contributing (satoshis)
        offer_type: Offer type (absolute or relative fee)
        network: Network type for address encoding

    Returns:
        (is_valid, error_message)
    """
    try:
        tx = parse_transaction(tx_hex, network=network)

        if tx is None:
            return False, "Failed to parse transaction"

        tx_inputs = tx["inputs"]
        tx_outputs = tx["outputs"]

        our_utxo_set = set(our_utxos.keys())
        tx_utxo_set = {(inp["txid"], inp["vout"]) for inp in tx_inputs}

        if not tx_utxo_set.issuperset(our_utxo_set):
            missing = our_utxo_set - tx_utxo_set
            return False, f"Our UTXOs not included in transaction: {missing}"

        escrow, error = _verify_escrow_details(
            additional_input_values,
            escrow_output,
            our_utxo_set,
            tx_inputs,
            cj_address,
            change_address,
            combine_address,
            ring_outputs,
        )
        if error is not None:
            return False, error

        additional_total = escrow.additional_total if escrow is not None else 0
        escrow_value = escrow.value if escrow is not None else 0
        my_total_in = sum(utxo.value for utxo in our_utxos.values()) + additional_total

        real_cjfee = calculate_cj_fee(offer_type, cjfee, amount)

        expected_change_value = my_total_in - amount - txfee + real_cjfee - escrow_value

        if escrow is not None:
            error = _verify_escrow_allocation(escrow, expected_change_value)
            if error is not None:
                return False, error

        potentially_earned = real_cjfee - txfee

        if potentially_earned < 0:
            return (
                False,
                f"Negative profit calculated: {potentially_earned} sats "
                f"(cjfee={real_cjfee}, txfee={txfee})",
            )

        logger.info(f"Potentially earned: {potentially_earned} sats")
        logger.info(f"Expected change value: {expected_change_value} sats")
        logger.info(f"CJ address: {cj_address}, Change address: {change_address}")

        counts, error = _verify_maker_outputs(
            tx_outputs,
            cj_address,
            change_address,
            amount,
            expected_change_value,
            combine_address,
            escrow,
        )
        if error is not None:
            return False, error

        assert counts is not None
        error = _verify_cj_address_count(counts.cj)
        if error is not None:
            return False, error

        if escrow is not None:
            error = _verify_escrow_output_counts(counts, expected_change_value)
            if error is not None:
                return False, error
            logger.info("Escrow transaction verification PASSED")
            return True, ""

        error = _verify_change_destination(
            counts,
            tx_outputs,
            expected_change_value,
            combine_address,
            combine_value,
            ring_outputs,
        )
        if error is not None:
            return False, error

        logger.info("Transaction verification PASSED ✓")
        return True, ""

    except Exception as e:
        logger.error(f"Transaction verification exception: {e}")
        return False, f"Verification error: {e}"


def parse_transaction(
    tx_hex: str, network: NetworkType = NetworkType.MAINNET
) -> dict[str, Any] | None:
    """
    Parse Bitcoin transaction hex.

    This is a simplified parser for CoinJoin transactions.
    For production, use a proper Bitcoin library.

    Args:
        tx_hex: Transaction hex string
        network: Network type for address encoding

    Returns:
        {
            'inputs': [{'txid': str, 'vout': int}, ...],
            'outputs': [{'address': str, 'value': int}, ...],
        }
    """
    try:
        parsed = parse_jmcore_transaction(tx_hex)

        # Keep maker-side policy checks while delegating structural parsing.
        # Permit v3 for TRUC policy compatibility (BIP-431, draft).
        if parsed.version not in (1, 2, 3):
            return None
        if len(parsed.inputs) == 0 or len(parsed.outputs) == 0:
            return None

        network_str = network.value if isinstance(network, NetworkType) else network
        outputs = [
            {
                "value": output.value,
                "address": script_to_address(output.script, network_str),
            }
            for output in parsed.outputs
        ]

        inputs = [{"txid": inp.txid, "vout": inp.vout} for inp in parsed.inputs]
        return {"inputs": inputs, "outputs": outputs}

    except Exception as e:
        logger.error(f"Failed to parse transaction: {e}")
        return None


def script_to_address(script: bytes, network: str = "mainnet") -> str:
    """
    Convert scriptPubKey to address.

    Uses jmcore.bitcoin.scriptpubkey_to_address for supported script types.
    Falls back to hex for unsupported types.

    Args:
        script: scriptPubKey bytes
        network: Network type string

    Returns:
        Address string, or hex if unsupported script type
    """
    try:
        return scriptpubkey_to_address(script, network)
    except ValueError:
        # Unsupported script type, return hex
        return script.hex()
