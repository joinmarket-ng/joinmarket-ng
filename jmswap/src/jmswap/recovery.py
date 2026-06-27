"""
Swap recovery: refund an on-chain HTLC whose timelock has expired.

If a swap stalls (the counterparty disappears after the funder has locked coins
on-chain), the funder reclaims via the refund leaf once the absolute locktime is
reached. This module builds and broadcasts that refund, given the funder's swap
key, and the predicate for when a persisted contract is refundable.
"""

from __future__ import annotations

from jmswap.chain import ChainBackend
from jmswap.models import SwapContract, SwapDirection, SwapState
from jmswap.spend import SwapOutpoint, build_refund_tx, estimate_fee
from jmswap.swap_tree import SwapTree


def is_refundable(contract: SwapContract, current_height: int) -> bool:
    """True if ``contract`` has on-chain funds the funder can refund now.

    The funder is the provider on a swap-out and the client on a swap-in. A swap
    is refundable once its funding output is confirmed, the absolute locktime has
    been reached, and it has not already completed.
    """
    if contract.state in (SwapState.CLAIMED, SwapState.REFUNDED, SwapState.FAILED):
        return False
    if contract.lockup_txid is None or contract.lockup_vout is None:
        return False
    return current_height >= contract.locktime


async def refund(
    contract: SwapContract,
    tree: SwapTree,
    refund_privkey: bytes,
    destination_spk: bytes,
    chain: ChainBackend,
) -> str | None:
    """Build and broadcast a refund for ``contract``; return the refund txid.

    Returns ``None`` if the funding output has already been spent (claimed), in
    which case no refund is needed or possible.
    """
    if contract.lockup_txid is None or contract.lockup_vout is None:
        return None
    spend = await chain.find_spending_tx(contract.lockup_txid, contract.lockup_vout)
    if spend is not None:
        return None  # already claimed/refunded
    outpoint = SwapOutpoint(
        txid=contract.lockup_txid,
        vout=contract.lockup_vout,
        value=contract.onchain_amount,
        scriptpubkey=tree.funding_output_script(),
    )
    feerate = await chain.estimate_feerate()
    fee = estimate_fee(150, feerate)
    tx = build_refund_tx(tree, outpoint, refund_privkey, destination_spk, fee)
    txid = await chain.broadcast(tx.serialize().hex())
    contract.spend_txid = txid
    contract.advance(SwapState.REFUNDED)
    return txid


def funder_is_provider(direction: SwapDirection) -> bool:
    """The funder (and thus the refund key holder) is the provider on swap-out."""
    return direction == SwapDirection.SWAP_OUT
