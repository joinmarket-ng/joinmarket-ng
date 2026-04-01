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

import struct
from typing import Any

from jmcore.bitcoin import (
    decode_varint,
    get_hrp,
    scriptpubkey_to_address,
)
from jmcore.constants import MAX_MONEY
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

        if times_seen_cj_addr != 1:
            return (
                False,
                f"CJ address appears {times_seen_cj_addr} times (expected 1)",
            )

        if times_seen_change_addr != 1:
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
        tx_bytes = bytes.fromhex(tx_hex)

        offset = 0

        int.from_bytes(tx_bytes[offset : offset + 4], "little")
        offset += 4

        if tx_bytes[offset] == 0x00:
            marker = tx_bytes[offset]
            flag = tx_bytes[offset + 1]
            if marker == 0x00 and flag == 0x01:
                offset += 2

        input_count, offset = decode_varint(tx_bytes, offset)

        inputs = []
        for _ in range(input_count):
            txid = tx_bytes[offset : offset + 32][::-1].hex()
            offset += 32

            vout = int.from_bytes(tx_bytes[offset : offset + 4], "little")
            offset += 4

            script_len, offset = decode_varint(tx_bytes, offset)
            offset += script_len

            int.from_bytes(tx_bytes[offset : offset + 4], "little")
            offset += 4

            inputs.append({"txid": txid, "vout": vout})

        output_count, offset = decode_varint(tx_bytes, offset)

        outputs = []
        for _ in range(output_count):
            value = struct.unpack("<q", tx_bytes[offset : offset + 8])[0]
            if value < 0 or value > MAX_MONEY:
                return None
            offset += 8

            script_len, offset = decode_varint(tx_bytes, offset)
            script_pubkey = tx_bytes[offset : offset + script_len]
            offset += script_len

            # Convert to network string for scriptpubkey_to_address
            network_str = network.value if isinstance(network, NetworkType) else network
            address = script_to_address(script_pubkey, network_str)

            outputs.append({"value": value, "address": address})

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
