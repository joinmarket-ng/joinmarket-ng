from __future__ import annotations


class SwapError(Exception):
    """Base error for standalone swap operations."""


class ProtocolError(SwapError):
    """Raised when a peer violates the Electrum swap protocol."""


class PersistenceError(SwapError):
    """Raised when persisted swap state fails validation."""
