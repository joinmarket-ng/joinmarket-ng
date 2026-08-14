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

from typing import Any

from jmcore.bitcoin import (
    decode_varint,
    get_hrp,
    scriptpubkey_to_address,
)
from jmcore.bitcoin import (
    parse_transaction as parse_jmcore_transaction,
)
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
) -> tuple[bool, str]:
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

        my_total_in = sum(utxo.value for utxo in our_utxos.values())

        real_cjfee = calculate_cj_fee(offer_type, cjfee, amount)

        expected_change_value = my_total_in - amount - txfee + real_cjfee

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

        times_seen_cj_addr = 0
        times_seen_change_addr = 0
        combine_total = 0

        for output in tx_outputs:
            output_addr = output["address"]
            output_value = output["value"]

            if output_addr == cj_address:
                times_seen_cj_addr += 1
                if output_value < amount:
                    return (
                        False,
                        f"CJ output value too low: {output_value} < {amount}",
                    )

            if output_addr == change_address:
                times_seen_change_addr += 1
                if output_value < expected_change_value:
                    return (
                        False,
                        f"Change output value too low: {output_value} < {expected_change_value}",
                    )

            if combine_address is not None and output_addr == combine_address:
                # The maker's change goes into a shared maker-to-maker 2-of-2 output.
                # For pooled combine it is one output; for the knapsack it is several
                # grid pieces. Sum them and check the total below.
                combine_total += output_value

        if times_seen_cj_addr != 1:
            return (
                False,
                f"CJ address appears {times_seen_cj_addr} times (expected 1)",
            )

        # Maker-to-maker channel-as-change: this maker's change is in a shared 2-of-2
        # funding output (one pooled output, or several grid pieces) instead of a plain
        # change output, so we require the shared total (recomputed by us) to cover at
        # least our own change.
        if ring_outputs is not None:
            # True-pooling ring: this maker's change is split into contributions to two
            # co-funded shared outputs. Require positive contributions at distinct,
            # singleton outputs, and prove each output includes a nonzero partner share.
            if not ring_outputs:
                return (False, "Ring relay contains no outputs")
            addresses = [addr for addr, _ in ring_outputs]
            if len(set(addresses)) != len(addresses):
                return (False, "Ring relay contains duplicate output addresses")
            if any(contrib <= 0 for _, contrib in ring_outputs):
                return (False, "Ring contributions must be positive")
            if sum(c for _, c in ring_outputs) != expected_change_value:
                return (
                    False,
                    f"Ring contributions {sum(c for _, c in ring_outputs)} "
                    f"!= our change {expected_change_value}",
                )
            outputs_at: dict[str, list[int]] = {}
            for o in tx_outputs:
                outputs_at.setdefault(o["address"], []).append(o["value"])
            for addr, contrib in ring_outputs:
                values = outputs_at.get(addr, [])
                if len(values) != 1:
                    return (False, f"Ring address {addr} appears {len(values)} times (expected 1)")
                if values[0] <= contrib:
                    return (
                        False,
                        f"Ring output {addr} value {values[0]} does not include partner funding",
                    )
        elif combine_address is not None:
            if combine_total < (combine_value or expected_change_value):
                return (
                    False,
                    f"Shared channel output total too low: {combine_total} "
                    f"< {combine_value or expected_change_value}",
                )
        elif times_seen_change_addr != 1:
            return (
                False,
                f"Change address appears {times_seen_change_addr} times (expected 1)",
            )

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
