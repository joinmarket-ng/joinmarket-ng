"""
Cooperative (MuSig2 key-path) swap claim.

The default script-path claim publishes the swap tapscript *and the preimage*
on-chain, so every swap is recognisable and the preimage cross-correlates with
the Lightning HTLC. The cooperative claim instead settles the swap with a 2-of-2
MuSig2 key-path spend: an ordinary BIP340 signature, indistinguishable from any
other P2TR key-path spend, with no preimage or script revealed on-chain.

It is a two-message interactive sign between the claimer and the counterparty.
The claimer (the party holding the preimage) builds the 1-in/1-out claim,
generates its nonce, asks the counterparty to co-sign, and aggregates. The
counterparty co-signs in a single call. If the counterparty does not cooperate,
the caller falls back to the trustless script-path claim.

This module is transport-agnostic: the claimer supplies an async ``cosign``
callable that performs the request/response (over Nostr in production).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from jmcore.musig2 import nonce_agg, sign_partial
from jmcore.taproot import TaprootTx

from jmswap.spend import (
    SwapOutpoint,
    build_cooperative_spend,
    cooperative_nonce,
    cooperative_session,
    finalize_cooperative,
)
from jmswap.swap_tree import SwapTree

# A cosign request: given the claimer's public nonce (hex), return the
# counterparty's ``(public_nonce_hex, partial_signature_hex)``.
Cosigner = Callable[[str], Awaitable[tuple[str, str]]]

# A swap-in cosign request: the *provider* is the claimer and chooses its own
# destination + fee. Given the client's public nonce (hex), return the provider's
# ``(public_nonce_hex, partial_signature_hex, destination_spk_hex, fee)``.
CosignerIn = Callable[[str], Awaitable[tuple[str, str, str, int]]]


def server_cosign_claim(
    tree: SwapTree,
    outpoint: SwapOutpoint,
    destination_spk: bytes,
    fee: int,
    server_privkey: bytes,
    client_pubnonce: bytes,
) -> tuple[bytes, bytes]:
    """Counterparty side: co-sign the claimer's key-path claim in one call.

    Returns ``(server_pubnonce, server_partial_signature)``.
    """
    spend = build_cooperative_spend(tree, outpoint, destination_spk, fee)
    secnonce, pubnonce = cooperative_nonce(tree.server_pubkey)
    aggnonce = nonce_agg([pubnonce, client_pubnonce])
    session = cooperative_session(tree, aggnonce, spend.sighash)
    partial = sign_partial(secnonce, server_privkey, session)
    return pubnonce, partial


async def cooperative_claim_client(
    tree: SwapTree,
    outpoint: SwapOutpoint,
    destination_spk: bytes,
    fee: int,
    client_privkey: bytes,
    cosign: Cosigner,
) -> TaprootTx:
    """Claimer side: drive the cooperative claim, returning the signed key-path tx.

    ``cosign(client_pubnonce_hex) -> (server_pubnonce_hex, server_partial_hex)``
    performs the single round-trip to the counterparty.
    """
    spend = build_cooperative_spend(tree, outpoint, destination_spk, fee)
    client_secnonce, client_pubnonce = cooperative_nonce(tree.client_pubkey)
    server_pubnonce_hex, server_partial_hex = await cosign(client_pubnonce.hex())
    server_pubnonce = bytes.fromhex(server_pubnonce_hex)
    server_partial = bytes.fromhex(server_partial_hex)

    aggnonce = nonce_agg([server_pubnonce, client_pubnonce])
    session = cooperative_session(tree, aggnonce, spend.sighash)
    client_partial = sign_partial(client_secnonce, client_privkey, session)
    return finalize_cooperative(
        tree, spend, server_pubnonce, client_pubnonce, server_partial, client_partial
    )


async def cooperative_claim_swap_in(
    tree: SwapTree,
    outpoint: SwapOutpoint,
    client_privkey: bytes,
    cosign: CosignerIn,
) -> TaprootTx:
    """Swap-in helper: the client co-signs the *provider's* key-path claim.

    On a swap-in the provider receives the on-chain coins, so it chooses the
    destination and fee. ``cosign(client_pubnonce_hex)`` performs the single
    round-trip and returns ``(server_pubnonce_hex, server_partial_hex,
    destination_spk_hex, fee)``. The client finalizes the fully-signed key-path
    transaction (which it then broadcasts on the provider's behalf), revealing no
    preimage or script on-chain.
    """
    client_secnonce, client_pubnonce = cooperative_nonce(tree.client_pubkey)
    server_pubnonce_hex, server_partial_hex, dest_hex, fee = await cosign(client_pubnonce.hex())
    server_pubnonce = bytes.fromhex(server_pubnonce_hex)
    server_partial = bytes.fromhex(server_partial_hex)
    destination_spk = bytes.fromhex(dest_hex)

    spend = build_cooperative_spend(tree, outpoint, destination_spk, int(fee))
    aggnonce = nonce_agg([server_pubnonce, client_pubnonce])
    session = cooperative_session(tree, aggnonce, spend.sighash)
    client_partial = sign_partial(client_secnonce, client_privkey, session)
    return finalize_cooperative(
        tree, spend, server_pubnonce, client_pubnonce, server_partial, client_partial
    )
