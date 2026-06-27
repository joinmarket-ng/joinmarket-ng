"""Exception hierarchy for the jmswap library."""

from __future__ import annotations


class SwapError(Exception):
    """Base class for all swap errors."""


class SwapProtocolError(SwapError):
    """A peer violated the swap protocol (malformed request/response)."""


class SwapVerificationError(SwapError):
    """A trust-minimization check failed (address, signature, amount mismatch)."""


class SwapStateError(SwapError):
    """An operation was attempted in an invalid swap state."""


class SwapTimeoutError(SwapError):
    """A swap step did not complete within its deadline."""


class LightningError(SwapError):
    """The Lightning backend reported a failure."""


class ChainError(SwapError):
    """The chain backend reported a failure (broadcast, lookup)."""


class InsufficientLiquidityError(SwapError):
    """The provider does not have enough liquidity to honor the swap."""
