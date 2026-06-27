"""
jmswap: a standalone bidirectional Taproot Lightning swap provider/client library.

The package is deliberately self-contained (depending only on ``jmcore`` for the
Bitcoin/Taproot primitives) so it can be extracted and reused outside
JoinMarket. The public surface:

* :class:`~jmswap.server.SwapServer` - the provider role (the maker side).
* :class:`~jmswap.client.SwapClient` - the user role.
* :class:`~jmswap.models.ProviderTerms` / :class:`~jmswap.models.SwapDirection`
  and the request/response/contract models.
* Backends: :class:`~jmswap.lightning.LightningBackend`
  (+ :class:`~jmswap.ldk.LdkServerBackend`, :class:`~jmswap.lightning.FakeLightningBackend`),
  :class:`~jmswap.chain.ChainBackend`, :class:`~jmswap.wallet.SwapWallet`.
* Transport: :class:`~jmswap.nostr.SwapNostrNode` (advertise / discover / RPC).
* :class:`~jmswap.swap_tree.SwapTree` and the on-chain spend builders.
"""

from __future__ import annotations

from jmswap.chain import ChainBackend, FakeChainBackend
from jmswap.channel import (
    ChangeSplit,
    CombinedFunding,
    channel_funding_address,
    channel_funding_output,
    cooperative_close,
    split_change,
)
from jmswap.client import SwapClient
from jmswap.errors import (
    ChainError,
    InsufficientLiquidityError,
    LightningError,
    SwapError,
    SwapProtocolError,
    SwapStateError,
    SwapVerificationError,
)
from jmswap.lightning import (
    FakeLightningBackend,
    FakeLightningNetwork,
    LightningBackend,
)
from jmswap.models import (
    ProviderTerms,
    SwapContract,
    SwapDirection,
    SwapRequest,
    SwapResponse,
    SwapState,
)
from jmswap.nostr import SwapNostrNode
from jmswap.persistence import SwapStore
from jmswap.recovery import is_refundable, refund
from jmswap.server import SwapServer
from jmswap.swap_tree import SwapTree, verify_swap_tree
from jmswap.wallet import FakeSwapWallet, SwapWallet

__all__ = [
    "ChainBackend",
    "ChainError",
    "ChangeSplit",
    "CombinedFunding",
    "FakeChainBackend",
    "FakeLightningBackend",
    "FakeLightningNetwork",
    "FakeSwapWallet",
    "InsufficientLiquidityError",
    "LightningBackend",
    "LightningError",
    "ProviderTerms",
    "SwapClient",
    "SwapContract",
    "SwapDirection",
    "SwapError",
    "SwapNostrNode",
    "SwapProtocolError",
    "SwapRequest",
    "SwapResponse",
    "SwapServer",
    "SwapState",
    "SwapStateError",
    "SwapStore",
    "SwapTree",
    "SwapVerificationError",
    "SwapWallet",
    "channel_funding_address",
    "channel_funding_output",
    "cooperative_close",
    "is_refundable",
    "refund",
    "split_change",
    "verify_swap_tree",
]
