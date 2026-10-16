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

# BIP65: nLockTime values below this threshold are block heights, at or above
# are Unix timestamps.
LOCKTIME_THRESHOLD = 500_000_000

# Tolerance (seconds) for a time-based nLockTime that sits slightly in the future
# due to clock skew between the taker and maker. A legitimate fidelity-bond spend
# always uses an nLockTime in the past (the bond must already be unlocked).
LOCKTIME_FUTURE_TOLERANCE_SEC = 2 * 60 * 60

# Tolerance (blocks) for a height-based nLockTime that sits slightly ahead of our
# own view of the chain tip (our backend may lag the taker's by a block or two).
# Reference/JAM sw0 takers set nLockTime to the current block height for
# anti-fee-sniping (jmclient.wallet.compute_tx_locktime), or up to 99 blocks
# behind it; roughly matches the 2-hour time-based tolerance above (~12 blocks
# at 10 min/block on mainnet, comfortably generous on faster testnets/regtest).
LOCKTIME_HEIGHT_FUTURE_TOLERANCE_BLOCKS = 12


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
    current_block_height: int | None = None,
    external_prevouts: dict[tuple[str, int], tuple[int, bytes]] | None = None,
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
        current_block_height: Our current view of the chain tip, used to
            validate a height-based nLockTime (reference/JAM sw0 takers set
            this for anti-fee-sniping). ``None`` rejects any height-based
            locktime outright (fail closed when the tip is unknown).
        external_prevouts: ``(txid, vout) -> (value, scriptPubKey)`` for inputs
            we contribute without owning them in the wallet (channel funding
            outputs of a prepared buyout). Their value must be present in the
            transaction and is expected back in our change output, so the
            values must already have been verified against the chain. They can
            never also be wallet UTXOs.

    Returns:
        (is_valid, error_message)
    """
    try:
        tx = parse_transaction(tx_hex, network=network)

        if tx is None:
            return False, "Failed to parse transaction"

        tx_inputs = tx["inputs"]
        tx_outputs = tx["outputs"]

        locktime_ok, locktime_error = _verify_locktime(
            tx.get("locktime", 0), current_block_height=current_block_height
        )
        if not locktime_ok:
            return False, locktime_error

        external = external_prevouts or {}
        our_utxo_set = set(our_utxos.keys())
        overlapping = our_utxo_set & set(external)
        if overlapping:
            return False, f"External inputs overlap our wallet UTXOs: {overlapping}"

        required_input_set = our_utxo_set | set(external)
        tx_utxo_set = {(inp["txid"], inp["vout"]) for inp in tx_inputs}

        if not tx_utxo_set.issuperset(required_input_set):
            missing = required_input_set - tx_utxo_set
            return False, f"Our UTXOs not included in transaction: {missing}"

        my_total_in = sum(utxo.value for utxo in our_utxos.values()) + sum(
            value for value, _ in external.values()
        )

        real_cjfee = calculate_cj_fee(offer_type, cjfee, amount)

        expected_change_value = my_total_in - amount - txfee + real_cjfee

        potentially_earned = real_cjfee - txfee

        if potentially_earned < 0:
            return (
                False,
                f"Negative profit calculated: {potentially_earned} sats "
                f"(cjfee={real_cjfee}, txfee={txfee})",
            )

        logger.bind(sensitive=True).debug(f"Potentially earned: {potentially_earned} sats")
        logger.bind(sensitive=True).debug(f"Expected change value: {expected_change_value} sats")
        logger.bind(sensitive=True).debug(
            f"CJ address: {cj_address}, Change address: {change_address}"
        )

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

        logger.debug("Transaction verification PASSED ✓")
        return True, ""

    except Exception as e:
        logger.error("Transaction verification failed")
        logger.bind(sensitive=True).error(f"Transaction verification exception: {e}")
        return False, f"Verification error: {e}"


def _verify_locktime(
    locktime: int,
    now: int | None = None,
    current_block_height: int | None = None,
) -> tuple[bool, str]:
    """Defense-in-depth check on the transaction-wide nLockTime.

    Two legitimate uses of a non-zero nLockTime: reference/JAM sw0 takers set
    a height-based nLockTime to the current block tip (or up to ~99 blocks
    behind it) for anti-fee-sniping (jmclient.wallet.compute_tx_locktime) --
    this is the ecosystem-standard default, not an edge case -- and a
    fidelity-bond spend uses a time-based CLTV that already lies in the past.
    Either way the guard's job is only to reject a locktime that would strand
    our signed inputs behind a lock that hasn't opened yet: a far-future
    height or time-based value.

    Args:
        locktime: Transaction nLockTime field.
        now: Current Unix time (defaults to wall clock); injectable for tests.
        current_block_height: Our current view of the chain tip, needed to
            bound a height-based locktime. ``None`` rejects any non-zero
            height-based locktime outright (fail closed when the tip is
            unknown, rather than accept an unbounded height).

    Returns:
        (is_valid, error_message)
    """
    if locktime == 0:
        return True, ""

    if locktime < LOCKTIME_THRESHOLD:
        if current_block_height is None:
            return (
                False,
                f"Block-height nLockTime {locktime} cannot be verified without a known chain tip",
            )
        max_height = current_block_height + LOCKTIME_HEIGHT_FUTURE_TOLERANCE_BLOCKS
        if locktime > max_height:
            return (
                False,
                f"nLockTime height {locktime} is ahead of our chain tip "
                f"{current_block_height} (max {max_height}); refusing to lock "
                "our inputs behind a future height",
            )
        return True, ""

    import time

    current = int(time.time()) if now is None else now
    if locktime > current + LOCKTIME_FUTURE_TOLERANCE_SEC:
        return (
            False,
            f"nLockTime {locktime} is in the future (now={current}); refusing to "
            "lock our inputs behind a future timelock",
        )

    return True, ""


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
        return {"inputs": inputs, "outputs": outputs, "locktime": parsed.locktime}

    except Exception as e:
        logger.error("Failed to parse transaction")
        logger.bind(sensitive=True).error(f"Failed to parse transaction: {e}")
        return None


def find_output_index(
    tx_hex: str,
    address: str,
    network: NetworkType = NetworkType.MAINNET,
) -> int:
    """Return the verified transaction output index paying ``address``."""
    tx = parse_transaction(tx_hex, network=network)
    if tx is None:
        return -1
    return next(
        (index for index, output in enumerate(tx["outputs"]) if output["address"] == address),
        -1,
    )


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
