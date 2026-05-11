"""Reusable direct-send (non-CoinJoin) transaction building, signing, and broadcasting.

This module contains the core spending logic extracted from the CLI so that both
the CLI and the ``jmwalletd`` HTTP daemon can share it without duplication.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from jmcore.bitcoin import estimate_vsize, get_address_type
from loguru import logger

from jmwallet.wallet.address import pubkey_to_p2wpkh_script
from jmwallet.wallet.signing import (
    create_p2wpkh_script_code,
    create_p2wsh_witness_stack,
    deserialize_transaction,
    encode_varint,
    sign_p2wpkh_input,
    sign_p2wsh_input,
)

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend
    from jmwallet.wallet.models import UTXOInfo
    from jmwallet.wallet.service import WalletService


DUST_THRESHOLD = 546


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
) -> list[UTXOInfo]:
    """Filter UTXOs to only those safe for auto-spending."""
    result = []
    for u in utxos:
        if not include_frozen and u.frozen:
            continue
        if not include_fidelity_bonds and u.is_fidelity_bond:
            continue
        result.append(u)
    return result


def estimate_fee(
    utxos: list[UTXOInfo],
    destination: str,
    fee_rate: float,
    *,
    has_change: bool,
) -> tuple[int, int]:
    """Estimate the transaction fee and vsize.

    Returns ``(fee, vsize)``.
    """
    input_types = ["p2wpkh"] * len(utxos)
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
) -> tuple[bytes, bytes, bytes, bytes, int]:
    """Build an unsigned raw transaction.

    Returns ``(unsigned_tx, version_bytes, inputs_data, outputs_data, locktime_int)``.
    """
    version = (2).to_bytes(4, "little")

    # Determine locktime from timelocked UTXOs
    max_locktime = 0
    has_timelocked = False
    current_time = int(time.time())
    for utxo in utxos:
        if utxo.is_timelocked and utxo.locktime is not None:
            has_timelocked = True
            if utxo.locktime > max_locktime:
                max_locktime = utxo.locktime
            if utxo.locktime > current_time:
                msg = (
                    f"Cannot spend timelocked UTXO {utxo.txid}:{utxo.vout} — "
                    f"locktime {utxo.locktime} is in the future (now: {current_time})"
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
    """Sign all inputs and return witness stacks."""
    tx = deserialize_transaction(unsigned_tx)
    witnesses: list[list[bytes]] = []

    for i, utxo in enumerate(utxos):
        key = wallet.get_key_for_address(utxo.address)
        if not key:
            msg = f"Missing signing key for address {utxo.address}"
            raise ValueError(msg)

        pubkey_bytes = key.get_public_key_bytes(compressed=True)

        if utxo.is_timelocked and utxo.locktime is not None:
            # P2WSH fidelity bond
            from jmcore.btc_script import mk_freeze_script

            witness_script = mk_freeze_script(pubkey_bytes.hex(), utxo.locktime)
            signature = sign_p2wsh_input(
                tx=tx,
                input_index=i,
                witness_script=witness_script,
                value=utxo.value,
                private_key=key.private_key,
            )
            witnesses.append(create_p2wsh_witness_stack(signature, witness_script))
        elif utxo.is_p2wsh:
            msg = f"Cannot sign P2WSH UTXO {utxo.txid}:{utxo.vout} — locktime not available"
            raise ValueError(msg)
        else:
            # P2WPKH
            script_code = create_p2wpkh_script_code(pubkey_bytes)
            signature = sign_p2wpkh_input(
                tx=tx,
                input_index=i,
                script_code=script_code,
                value=utxo.value,
                private_key=key.private_key,
            )
            witnesses.append([signature, pubkey_bytes])

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
    if fee_rate is None:
        fee_rate = await backend.estimate_fee(target_blocks=fee_target_blocks)
        logger.debug("Estimated fee rate: {:.2f} sat/vB ({} blocks)", fee_rate, fee_target_blocks)

    # --- UTXO selection ---
    utxos: list[UTXOInfo]
    if amount_sats == 0:
        # Sweep: use all spendable UTXOs.
        raw_utxos = await wallet.get_utxos(mixdepth)
        utxos = select_spendable_utxos(raw_utxos)
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
            fee += change_amount
            change_amount = 0
            # Re-estimate without change output
            fee, _vsize = estimate_fee(utxos, destination, fee_rate, has_change=False)

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
        utxos, dest_script, send_amount, change_script, change_amount
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
