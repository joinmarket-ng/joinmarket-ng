"""
Protocols decoupling the taker swap flow from the wallet implementation.

The taker must never handle raw private keys. Instead it depends on these
narrow structural interfaces, which the wallet service satisfies. This keeps
``taker.swap`` free of any wallet/crypto imports while routing every key
operation (secret derivation, claim signing, storage-key derivation) through
the wallet.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from jmcore.bitcoin import ParsedTransaction


@runtime_checkable
class SwapKeyMaterialLike(Protocol):
    """Public swap key material returned by the wallet (no private key).

    Declared with read-only properties so a frozen dataclass implementation
    (the wallet's ``SwapKeyMaterial``) structurally conforms.
    """

    @property
    def index(self) -> int: ...

    @property
    def preimage(self) -> bytes: ...

    @property
    def preimage_hash(self) -> bytes: ...

    @property
    def claim_pubkey(self) -> bytes: ...


@runtime_checkable
class SwapKeyProvider(Protocol):
    """Wallet-side authority for all swap key operations."""

    def derive_swap_storage_key(self) -> bytes:
        """Return a 32-byte symmetric key for encrypting swap records."""
        ...

    def create_swap_key_material(self) -> SwapKeyMaterialLike:
        """Pick a fresh swap index and derive its public material."""
        ...

    def derive_swap_key_material(self, index: int) -> SwapKeyMaterialLike:
        """Re-derive public material for a known swap index."""
        ...

    def build_swap_claim_witness(
        self,
        tx: ParsedTransaction,
        input_index: int,
        witness_script: bytes,
        value: int,
        swap_index: int,
    ) -> list[bytes]:
        """Sign and assemble the HTLC claim witness for a lockup output."""
        ...
