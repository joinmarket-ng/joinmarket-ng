"""
Swap input module for taker CoinJoin privacy enhancement.

This module implements submarine swap (reverse: LN -> on-chain) functionality
to provide the taker with an additional CoinJoin input that covers all fees,
making the taker's on-chain footprint indistinguishable from a maker's.

Protocol: Electrum-compatible swap server protocol over Nostr DMs.
Script type: P2WSH HTLC (preimage + signature claim path).

Architecture:
- models.py: Data models for swap providers, requests, responses, and state
- script.py: HTLC witness script construction, address derivation, and claim witness
- nip04.py: NIP-04 encryption/decryption and Nostr event signing
- nostr.py: Nostr relay client for provider discovery and encrypted RPC
- client.py: High-level swap client orchestrating discovery and swap execution
"""

from __future__ import annotations

from taker.swap.client import SwapClient
from taker.swap.ln_client import LndConnection, LndRestClient
from taker.swap.models import (
    ReverseSwapRequest,
    ReverseSwapResponse,
    SwapInput,
    SwapProvider,
    SwapState,
)
from taker.swap.nip04 import (
    create_nip04_dm_event,
    nip04_decrypt,
    nip04_encrypt,
    privkey_to_xonly_pubkey,
)
from taker.swap.nostr import NostrSwapRPC
from taker.swap.script import SwapScript

__all__ = [
    "LndConnection",
    "LndRestClient",
    "NostrSwapRPC",
    "SwapClient",
    "SwapInput",
    "SwapProvider",
    "SwapScript",
    "SwapState",
    "ReverseSwapRequest",
    "ReverseSwapResponse",
    "create_nip04_dm_event",
    "nip04_decrypt",
    "nip04_encrypt",
    "privkey_to_xonly_pubkey",
]
