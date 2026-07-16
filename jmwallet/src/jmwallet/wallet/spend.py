"""Reusable direct-send (non-CoinJoin) transaction building, signing, and broadcasting.

This module contains the core spending logic extracted from the CLI so that both
the CLI and the ``jmwalletd`` HTTP daemon can share it without duplication.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING

from jmcore.bitcoin import estimate_vsize, get_address_type
from jmcore.btc_script import mk_freeze_script
from loguru import logger

from jmwallet.wallet.address import pubkey_to_p2wpkh_script
from jmwallet.wallet.signing import (
    deserialize_transaction,
    encode_varint,
)

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from jmwallet.wallet.models import UTXOInfo
    from jmwallet.wallet.service import WalletService


DUST_THRESHOLD = 546

# Default safety cap on fee rate (sat/vB) used by direct-send transactions.
# This is the fallback when callers don't override via the
# ``max_fee_rate_sat_vb`` parameter (typically wired from
# ``WalletSettings.max_fee_rate_sat_vb``).  It protects against:
#
# * Backends that report wildly inflated fee estimates (RPC bug, hijacked
#   fee oracle, hostile rogue node).
# * UI / scripting bugs that pass a fee rate in the wrong unit (BTC/kvB
#   instead of sat/vB), or with a misplaced decimal point.
# * Malicious upper-layer code attempting to grief a wallet by burning the
#   entire balance to fees.
#
# Above this cap a transaction is refused with :class:`ExcessiveFeeRateError`
# rather than silently broadcasting.
DEFAULT_MAX_FEE_RATE_SAT_VB: float = 1_000.0


class ExcessiveFeeRateError(ValueError):
    """Raised when a resolved fee rate exceeds the configured safety cap.

    Subclasses :class:`ValueError` so existing ``except ValueError`` handlers
    in the CLI and HTTP layers continue to behave correctly (refuse the
    transaction with a user-visible error) without needing to know about the
    new exception type.
    """


def enforce_fee_rate_cap(fee_rate: float, max_fee_rate_sat_vb: float, *, source: str) -> None:
    """Reject *fee_rate* if it exceeds the configured cap.

    Parameters
    ----------
    fee_rate:
        The candidate fee rate in sat/vB.
    max_fee_rate_sat_vb:
        The safety cap.  Must be positive.
    source:
        Human-readable description of where the rate came from
        (``"manual"``, ``"backend estimate"``, ...).  Included verbatim in
        the error message to make misconfiguration easy to debug.

    Raises
    ------
    ExcessiveFeeRateError
        If ``fee_rate`` exceeds ``max_fee_rate_sat_vb``.
    """
    if not math.isfinite(fee_rate) or fee_rate <= 0:
        msg = f"{source} fee rate must be a finite positive number, got {fee_rate!r}"
        raise ExcessiveFeeRateError(msg)
    if fee_rate > max_fee_rate_sat_vb:
        msg = (
            f"{source} fee rate {fee_rate:.2f} sat/vB exceeds safety cap "
            f"{max_fee_rate_sat_vb:.2f} sat/vB. "
            "Raise the cap explicitly (settings.wallet.max_fee_rate_sat_vb) "
            "only if you really intend to pay this much."
        )
        raise ExcessiveFeeRateError(msg)


@dataclass
class DirectSendResult:
    """Result returned by :func:`direct_send`."""

    txid: str
    tx_hex: str
    fee: int
    fee_rate: float
    send_amount: int
    change_amount: int
    num_inputs: int
    num_outputs: int
    inputs: list[dict[str, object]] = field(default_factory=list)
    outputs: list[dict[str, object]] = field(default_factory=list)


# Map of wallet networks to python-bitcointx chain parameter names. Used so
# CCoinAddress can both verify the bech32/base58 checksum AND reject
# addresses from a different network than the wallet is configured for.
_NETWORK_CHAIN_PARAMS: dict[str, str] = {
    "mainnet": "bitcoin",
    "testnet": "bitcoin/testnet",
    "signet": "bitcoin/signet",
    "regtest": "bitcoin/regtest",
}


def _decode_bech32_scriptpubkey(address: str, *, network: str | None = None) -> bytes:
    """Decode a Bitcoin address into its scriptPubKey bytes.

    Delegates to ``python-bitcointx``'s :class:`CCoinAddress`, which
    verifies the BIP173/BIP350 checksum (bech32 / bech32m), rejects
    wrong-network addresses under the active :class:`ChainParams`, and
    supports every standard address type (P2WPKH, P2WSH, P2TR, P2PKH,
    P2SH).

    Args:
        address: Destination Bitcoin address as a string.
        network: Wallet network. When provided, address parsing happens
            inside :class:`bitcointx.ChainParams` for the matching chain
            so a mainnet address is rejected on testnet (and vice versa).

    Raises:
        ValueError: If the address is malformed, has a bad checksum, or
            does not belong to the requested network.
    """
    # Imported lazily to keep test-import cost low and to keep the
    # bitcointx dependency optional for callers that never touch
    # direct-send.
    from bitcointx import ChainParams
    from bitcointx.wallet import CCoinAddress, CCoinAddressError

    chain = _NETWORK_CHAIN_PARAMS.get(network) if network is not None else None
    if network is not None and chain is None:
        msg = f"Unsupported network for address decoding: {network!r}"
        raise ValueError(msg)

    try:
        if chain is not None:
            with ChainParams(chain):
                return bytes(CCoinAddress(address).to_scriptPubKey())
        return bytes(CCoinAddress(address).to_scriptPubKey())
    except CCoinAddressError as exc:
        msg = f"Invalid destination address {address!r} (bad checksum, format, or wrong network)"
        raise ValueError(msg) from exc


def select_spendable_utxos(
    utxos: list[UTXOInfo],
    *,
    include_frozen: bool = False,
    include_fidelity_bonds: bool = False,
    locktime_cutoff: int | None = None,
) -> list[UTXOInfo]:
    """Filter UTXOs to only those safe for auto-spending.

    Frozen UTXOs and all fidelity bonds are excluded by default. Setting
    ``include_fidelity_bonds`` admits only bonds whose locktime is strictly
    below ``locktime_cutoff``. The cutoff should be chain median-time-past for
    transaction construction; it defaults to the host time for display-only
    callers.
    """
    cutoff = int(time.time()) if locktime_cutoff is None else locktime_cutoff
    result = []
    for u in utxos:
        if not include_frozen and u.frozen:
            continue
        if u.is_fidelity_bond:
            if not include_fidelity_bonds:
                continue
            if u.locktime is None or u.locktime >= cutoff:
                continue
        result.append(u)
    return result


def _is_signable_fidelity_bond(wallet: WalletService, utxo: UTXOInfo) -> bool:
    """Return whether this wallet derives the script key for a bond UTXO."""
    if not utxo.is_fidelity_bond or utxo.locktime is None or not utxo.is_p2wsh:
        return False
    try:
        key = wallet.get_key_for_address(utxo.address)
    except Exception:
        return False
    if key is None:
        return False
    witness_script = mk_freeze_script(
        key.get_public_key_bytes(compressed=True).hex(), utxo.locktime
    )
    expected_scriptpubkey = b"\x00\x20" + sha256(witness_script).digest()
    return utxo.scriptpubkey.lower() == expected_scriptpubkey.hex()


def estimate_fee(
    utxos: list[UTXOInfo],
    destination: str,
    fee_rate: float,
    *,
    has_change: bool,
) -> tuple[int, int]:
    """Estimate the transaction fee and vsize.

    P2WSH inputs (expired fidelity bonds being swept) are larger than P2WPKH
    inputs (their witness carries the timelock script), so size them as such
    or the resulting fee rate falls below the requested one (and potentially
    below the relay floor).

    Returns ``(fee, vsize)``.
    """
    input_types = ["p2wsh" if u.is_p2wsh else "p2wpkh" for u in utxos]
    try:
        dest_type = get_address_type(destination)
    except ValueError:
        dest_type = "p2wpkh"

    output_types = [dest_type]
    if has_change:
        output_types.append("p2wpkh")

    vsize = estimate_vsize(input_types, output_types)
    return math.ceil(vsize * fee_rate), vsize


def _build_unsigned_tx(
    utxos: list[UTXOInfo],
    dest_script: bytes,
    send_amount: int,
    change_script: bytes | None,
    change_amount: int,
    *,
    locktime_cutoff: int | None = None,
) -> tuple[bytes, bytes, bytes, bytes, int]:
    """Build an unsigned raw transaction.

    Returns ``(unsigned_tx, version_bytes, inputs_data, outputs_data, locktime_int)``.
    """
    version = (2).to_bytes(4, "little")

    # Determine locktime from timelocked UTXOs
    max_locktime = 0
    has_timelocked = False
    cutoff = int(time.time()) if locktime_cutoff is None else locktime_cutoff
    for utxo in utxos:
        if utxo.is_timelocked and utxo.locktime is not None:
            has_timelocked = True
            if utxo.locktime > max_locktime:
                max_locktime = utxo.locktime
            if utxo.locktime >= cutoff:
                msg = (
                    f"Cannot spend timelocked UTXO {utxo.txid}:{utxo.vout}: "
                    f"locktime {utxo.locktime} has not passed chain time {cutoff}"
                )
                raise ValueError(msg)

    locktime = max_locktime.to_bytes(4, "little")

    # Inputs
    inputs_data = bytearray()
    for utxo in utxos:
        txid_bytes = bytes.fromhex(utxo.txid)[::-1]  # big-endian → little-endian
        inputs_data.extend(txid_bytes)
        inputs_data.extend(utxo.vout.to_bytes(4, "little"))
        inputs_data.append(0)  # empty scriptSig for SegWit
        seq = 0xFFFFFFFE if has_timelocked else 0xFFFFFFFF
        inputs_data.extend(seq.to_bytes(4, "little"))

    # Outputs
    num_outputs = 1
    outputs_data = bytearray()
    outputs_data.extend(send_amount.to_bytes(8, "little"))
    outputs_data.extend(encode_varint(len(dest_script)))
    outputs_data.extend(dest_script)

    if change_amount > 0 and change_script is not None:
        num_outputs += 1
        outputs_data.extend(change_amount.to_bytes(8, "little"))
        outputs_data.extend(encode_varint(len(change_script)))
        outputs_data.extend(change_script)

    unsigned_tx = (
        version
        + encode_varint(len(utxos))
        + bytes(inputs_data)
        + encode_varint(num_outputs)
        + bytes(outputs_data)
        + locktime
    )
    return unsigned_tx, version, bytes(inputs_data), bytes(outputs_data), num_outputs


def _sign_inputs(
    unsigned_tx: bytes,
    utxos: list[UTXOInfo],
    wallet: WalletService,
) -> list[list[bytes]]:
    """Sign all inputs and return witness stacks.

    Key access and signing are delegated to the wallet (issue #518) so private
    keys never leave the wallet boundary.
    """
    tx = deserialize_transaction(unsigned_tx)
    witnesses: list[list[bytes]] = []

    for i, utxo in enumerate(utxos):
        signed = wallet.sign_input(tx, i, utxo)
        witnesses.append(signed.witness)

    return witnesses


def _assemble_signed_tx(
    version: bytes,
    inputs_data: bytes,
    num_outputs: int,
    outputs_data: bytes,
    locktime_bytes: bytes,
    witnesses: list[list[bytes]],
    num_inputs: int,
) -> bytes:
    """Assemble a fully signed SegWit transaction."""
    signed = bytearray()
    signed.extend(version)
    signed.extend(b"\x00\x01")  # SegWit marker + flag
    signed.extend(encode_varint(num_inputs))
    signed.extend(inputs_data)
    signed.extend(encode_varint(num_outputs))
    signed.extend(outputs_data)

    for witness_stack in witnesses:
        signed.extend(encode_varint(len(witness_stack)))
        for item in witness_stack:
            signed.extend(encode_varint(len(item)))
            signed.extend(item)

    signed.extend(locktime_bytes)
    return bytes(signed)


async def direct_send(
    *,
    wallet: WalletService,
    backend: BlockchainBackend,
    mixdepth: int,
    amount_sats: int,
    destination: str,
    fee_rate: float | None = None,
    fee_target_blocks: int = 6,
    max_fee_rate_sat_vb: float = DEFAULT_MAX_FEE_RATE_SAT_VB,
) -> DirectSendResult:
    """Build, sign, and broadcast a direct (non-CoinJoin) transaction.

    Parameters
    ----------
    wallet:
        An initialised and synced :class:`WalletService`.
    backend:
        The blockchain backend for fee estimation and broadcasting.
    mixdepth:
        The mixdepth (account) to spend from.
    amount_sats:
        Amount in satoshis to send.  ``0`` means sweep the entire mixdepth.
    destination:
        Destination Bitcoin address (bech32 only).
    fee_rate:
        Explicit fee rate in sat/vB.  When *None*, the rate is estimated
        from the backend using *fee_target_blocks*.
    fee_target_blocks:
        Number of blocks for fee estimation (ignored when *fee_rate* is set).
    max_fee_rate_sat_vb:
        Safety cap on the fee rate (sat/vB).  The resolved rate (manual or
        from backend estimation) is rejected with
        :class:`ExcessiveFeeRateError` when it exceeds this value.  Defaults
        to :data:`DEFAULT_MAX_FEE_RATE_SAT_VB`; daemon and CLI callers wire
        this from ``settings.wallet.max_fee_rate_sat_vb``.

    Returns
    -------
    DirectSendResult
    """
    if not destination.startswith(("bc1", "tb1", "bcrt1")):
        msg = "Only bech32 addresses are currently supported"
        raise ValueError(msg)

    # Validate the destination address up front (checksum + HRP + network).
    # We compute the scriptPubKey now so a malformed address fails fast,
    # before any fee estimation or UTXO selection side effects.
    network = getattr(wallet, "network", None)
    dest_script = _decode_bech32_scriptpubkey(destination, network=network)

    # --- Fee rate resolution ---
    if fee_rate is not None:
        enforce_fee_rate_cap(fee_rate, max_fee_rate_sat_vb, source="manual")
    else:
        fee_rate = await backend.estimate_fee(target_blocks=fee_target_blocks)
        logger.debug("Estimated fee rate: {:.2f} sat/vB ({} blocks)", fee_rate, fee_target_blocks)
        enforce_fee_rate_cap(fee_rate, max_fee_rate_sat_vb, source="backend estimate")

    # --- UTXO selection ---
    utxos: list[UTXOInfo]
    locktime_cutoff: int | None = None
    if amount_sats == 0:
        # Sweep regular coins by default. If there are none, admit expired
        # hot-wallet bonds. This supports explicit bond-redemption flows that
        # freeze every other coin without making bonds part of normal
        # auto-selection or linking them to unrelated funds.
        raw_utxos = await wallet.get_utxos(mixdepth)
        utxos = select_spendable_utxos(raw_utxos)
        if not utxos and any(u.is_fidelity_bond and not u.frozen for u in raw_utxos):
            locktime_cutoff = await backend.get_median_time_past()
            bond_candidates = select_spendable_utxos(
                raw_utxos,
                include_fidelity_bonds=True,
                locktime_cutoff=locktime_cutoff,
            )
            utxos = [u for u in bond_candidates if _is_signable_fidelity_bond(wallet, u)]
    else:
        # Non-sweep: use greedy coin selection to pick the minimum UTXOs needed.
        # This avoids building oversized transactions when the wallet has many UTXOs.
        # Add a generous fee buffer (5× estimated fee) to ensure enough inputs.
        fee_buffer = max(10_000, int(amount_sats * 0.05))
        try:
            utxos = wallet.select_utxos(mixdepth, amount_sats + fee_buffer)
        except ValueError:
            # Fallback: use all spendable UTXOs if coin selection fails
            # (e.g. many dust UTXOs where the sum exceeds target but individually small).
            raw_utxos = await wallet.get_utxos(mixdepth)
            utxos = select_spendable_utxos(raw_utxos)

    if not utxos:
        msg = f"No spendable UTXOs in mixdepth {mixdepth}"
        raise ValueError(msg)

    total_input = sum(u.value for u in utxos)
    is_sweep = amount_sats == 0

    # --- Fee estimation ---
    has_change = not is_sweep
    fee, _vsize = estimate_fee(utxos, destination, fee_rate, has_change=has_change)

    if is_sweep:
        send_amount = total_input - fee
        if send_amount <= 0:
            msg = "Insufficient funds after fee deduction for sweep"
            raise ValueError(msg)
        change_amount = 0
    else:
        send_amount = amount_sats
        change_amount = total_input - send_amount - fee
        if change_amount < 0:
            msg = f"Insufficient funds: need {send_amount + fee}, have {total_input}"
            raise ValueError(msg)
        if change_amount < DUST_THRESHOLD:
            # With no change output, every satoshi not sent is the actual fee.
            # Keep the reported fee consistent with the serialized transaction.
            fee = total_input - send_amount
            change_amount = 0

    # --- Destination scriptPubKey ---
    # (already validated and computed at the top of this function)

    # --- Change output ---
    change_script: bytes | None = None
    if change_amount > 0:
        change_index = wallet.get_next_address_index(mixdepth, 1)
        change_addr = wallet.get_change_address(mixdepth, change_index)
        change_key = wallet.get_key_for_address(change_addr)
        if change_key is None:
            msg = f"Cannot derive key for change address {change_addr}"
            raise ValueError(msg)
        change_script = pubkey_to_p2wpkh_script(
            change_key.get_public_key_bytes(compressed=True).hex()
        )

    # --- Build unsigned tx ---
    unsigned_tx, version, inputs_data, outputs_data, num_outputs = _build_unsigned_tx(
        utxos,
        dest_script,
        send_amount,
        change_script,
        change_amount,
        locktime_cutoff=locktime_cutoff,
    )

    # --- Sign ---
    witnesses = _sign_inputs(unsigned_tx, utxos, wallet)

    # --- Assemble signed tx ---
    locktime_bytes = unsigned_tx[-4:]
    signed_tx = _assemble_signed_tx(
        version, inputs_data, num_outputs, outputs_data, locktime_bytes, witnesses, len(utxos)
    )
    tx_hex = signed_tx.hex()

    # --- Broadcast ---
    logger.info("Broadcasting direct-send transaction ({} bytes)", len(signed_tx))
    broadcast_txid = await backend.broadcast_transaction(tx_hex)
    txid = broadcast_txid or ""

    logger.info("Broadcast OK: {}", txid)
    return DirectSendResult(
        txid=txid,
        tx_hex=tx_hex,
        fee=fee,
        fee_rate=fee_rate,
        send_amount=send_amount,
        change_amount=change_amount,
        num_inputs=len(utxos),
        num_outputs=num_outputs,
        inputs=[
            {
                "outpoint": f"{u.txid}:{u.vout}",
                "scriptSig": "",
                "nSequence": 0xFFFFFFFE
                if any(ut.is_timelocked and ut.locktime is not None for ut in utxos)
                else 0xFFFFFFFF,
                "witness": "",
            }
            for u in utxos
        ],
        outputs=[
            {"value_sats": send_amount, "scriptPubKey": dest_script.hex(), "address": destination},
        ],
    )
