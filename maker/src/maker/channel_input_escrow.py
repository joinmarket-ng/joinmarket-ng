"""Maker-local plan for contributing a frozen channel input to a CoinJoin."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from jmcore.bitcoin import scriptpubkey_to_address
from jmcore.models import NetworkType
from jmswap.channel_escrow import (
    ChannelInputEscrowRecord,
    ChannelInputEscrowTerms,
    ParentAuthorization,
    PrepareTimeoutRequest,
)


@runtime_checkable
class ChannelInputEscrowCoordinatorProtocol(Protocol):
    """Coordinator operations required by the maker CoinJoin boundary."""

    async def prepare_timeout(self, request: PrepareTimeoutRequest) -> ChannelInputEscrowRecord: ...

    async def authorize_parent(self, session_id: str) -> ChannelInputEscrowRecord: ...


@dataclass(frozen=True, slots=True)
class ChannelInputEscrowPlan:
    """Private escrow terms and the exact public P2TR output they require."""

    terms: ChannelInputEscrowTerms
    escrow_scriptpubkey: str
    coordinator: ChannelInputEscrowCoordinatorProtocol

    def __post_init__(self) -> None:
        if not isinstance(self.terms, ChannelInputEscrowTerms):
            raise TypeError("channel-input escrow plan requires validated terms")
        if self.terms.escrow_value_sats <= 0:
            raise ValueError("channel-input escrow value must be positive")
        if not isinstance(self.escrow_scriptpubkey, str):
            raise TypeError("escrow scriptPubKey must be hexadecimal text")
        try:
            scriptpubkey = bytes.fromhex(self.escrow_scriptpubkey)
        except ValueError as exc:
            raise ValueError("escrow scriptPubKey must be hexadecimal") from exc
        if len(scriptpubkey) != 34 or not scriptpubkey.startswith(b"\x51\x20"):
            raise ValueError("escrow scriptPubKey must be P2TR OP_1 PUSH32")
        object.__setattr__(self, "escrow_scriptpubkey", scriptpubkey.hex())
        if not isinstance(self.coordinator, ChannelInputEscrowCoordinatorProtocol):
            raise TypeError("escrow coordinator does not implement the required protocol")

    def escrow_address(self, network: NetworkType | str) -> str:
        """Return the network-specific address for the public escrow output."""
        return scriptpubkey_to_address(bytes.fromhex(self.escrow_scriptpubkey), network)


class ChannelInputEscrowPlanProvider(Protocol):
    """Select an escrow plan only after a taker has authenticated its fill."""

    async def __call__(self) -> ChannelInputEscrowPlan | None: ...


def format_channel_input_witness(authorization: ParentAuthorization) -> str:
    """Format the sole authorized channel key-path witness for a JM ``!sig`` response."""
    if not isinstance(authorization, ParentAuthorization):
        raise TypeError("channel witness requires a validated parent authorization")
    if len(authorization.channel_input_witness) != 1:
        raise ValueError("channel authorization must contain exactly one witness item")
    signature = authorization.channel_input_witness[0]
    if len(signature) != 64:
        raise ValueError("channel authorization must contain a 64-byte witness item")
    return (
        f"p2tr:{authorization.channel_funding_outpoint.txid}:"
        f"{authorization.channel_funding_outpoint.vout}:{signature.hex()}"
    )
