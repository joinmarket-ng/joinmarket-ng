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

from jmswap.bitcoin_escrow import (
    ChannelInputEscrowTree,
    UnsignedTimeoutSpend,
    build_success_spend,
    build_timeout_spend,
    finalize_timeout_spend,
    parse_escrow_transaction,
    timeout_nonce,
    timeout_session,
    verify_success_spend,
    verify_timeout,
)
from jmswap.bond import select_bonded_providers, verify_provider_bond
from jmswap.chain import ChainBackend, FakeChainBackend
from jmswap.channel import (
    ChangeSplit,
    CombinedChannelPlan,
    CombinedFunding,
    channel_funding_address,
    channel_funding_output,
    combined_funding_address,
    combined_funding_output,
    cooperative_close,
    plan_combined_channel,
    split_change,
)
from jmswap.channel_escrow import (
    ChainObservationRequest,
    ChannelInputEscrowBackend,
    ChannelInputEscrowRecord,
    ChannelInputEscrowStore,
    ChannelInputEscrowTerms,
    ChannelSnapshot,
    EscrowChainObservation,
    EscrowLifecycleState,
    EscrowOutpoint,
    EscrowParentBroadcast,
    EscrowParentEvidence,
    EscrowRouteEvidence,
    EscrowSpendResult,
    EscrowTimeoutBroadcast,
    EscrowTimeoutEvidence,
    FreezeChannelRequest,
    ParentAuthorization,
    ParentAuthorizationRequest,
    ParentBroadcastRequest,
    PaymentObservationRequest,
    PreparedChannelInputEscrow,
    PrepareTimeoutRequest,
    RecoveryBroadcastResult,
    RecoveryRequest,
    SinglePartPaymentRequest,
    SinglePartPaymentResult,
    SuccessSpendRequest,
    require_channel_input_escrow_backend,
)
from jmswap.channel_funding import (
    REQUIRED_CHANNEL_INPUT_ESCROW_CAPABILITIES,
    ChannelInputEscrowCapability,
    complete_external_channel_funding,
    missing_channel_input_escrow_capabilities,
    request_external_channel_funding,
    require_channel_input_escrow_capabilities,
)
from jmswap.client import SwapClient
from jmswap.errors import (
    ChainError,
    ChannelInputEscrowError,
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
    "ChainObservationRequest",
    "ChannelInputEscrowBackend",
    "ChannelInputEscrowCapability",
    "ChannelInputEscrowError",
    "ChannelInputEscrowRecord",
    "ChannelInputEscrowStore",
    "ChannelInputEscrowTerms",
    "ChannelInputEscrowTree",
    "ChannelSnapshot",
    "ChangeSplit",
    "CombinedChannelPlan",
    "CombinedFunding",
    "FakeChainBackend",
    "FakeLightningBackend",
    "FakeLightningNetwork",
    "FakeSwapWallet",
    "EscrowChainObservation",
    "EscrowLifecycleState",
    "EscrowOutpoint",
    "EscrowParentBroadcast",
    "EscrowParentEvidence",
    "EscrowRouteEvidence",
    "EscrowSpendResult",
    "EscrowTimeoutBroadcast",
    "EscrowTimeoutEvidence",
    "FreezeChannelRequest",
    "InsufficientLiquidityError",
    "LightningBackend",
    "LightningError",
    "ParentAuthorization",
    "ParentAuthorizationRequest",
    "ParentBroadcastRequest",
    "PaymentObservationRequest",
    "PreparedChannelInputEscrow",
    "PrepareTimeoutRequest",
    "ProviderTerms",
    "REQUIRED_CHANNEL_INPUT_ESCROW_CAPABILITIES",
    "RecoveryBroadcastResult",
    "RecoveryRequest",
    "SinglePartPaymentRequest",
    "SinglePartPaymentResult",
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
    "SuccessSpendRequest",
    "UnsignedTimeoutSpend",
    "build_success_spend",
    "build_timeout_spend",
    "channel_funding_address",
    "channel_funding_output",
    "combined_funding_address",
    "combined_funding_output",
    "complete_external_channel_funding",
    "cooperative_close",
    "finalize_timeout_spend",
    "plan_combined_channel",
    "missing_channel_input_escrow_capabilities",
    "parse_escrow_transaction",
    "require_channel_input_escrow_capabilities",
    "require_channel_input_escrow_backend",
    "request_external_channel_funding",
    "select_bonded_providers",
    "verify_provider_bond",
    "is_refundable",
    "refund",
    "split_change",
    "timeout_nonce",
    "timeout_session",
    "verify_success_spend",
    "verify_swap_tree",
    "verify_timeout",
]
