"""Centralized transaction-signing interface for the wallet.

Issue #518: the most security-critical part of the application is the code
that accesses private keys to produce signatures.  Historically four separate
call sites (the taker's CoinJoin session, the maker's CoinJoin session, the
reusable ``direct_send`` helper, and the CLI ``send`` command) each fetched the
signing key, read ``key.private_key`` and called the low-level
``sign_p2wpkh_input`` / ``sign_p2wsh_input`` primitives.

This mixin consolidates that logic so private-key access happens in exactly one
place.  Callers hand the wallet an unsigned transaction, the input index, and
the ``UTXOInfo`` being spent; the wallet returns a :class:`SignedInput`
describing the resulting signature, public key and witness stack.  The various
callers format that result for their own wire protocols without ever touching
key material.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from jmcore.btc_script import mk_freeze_script

from jmwallet.wallet.signing import (
    ParsedTransaction,
    TransactionSigningError,
    create_p2wpkh_script_code,
    create_p2wsh_witness_stack,
    create_witness_stack,
    sign_p2wpkh_input,
    sign_p2wsh_input,
)

if TYPE_CHECKING:
    from jmwallet.wallet.bip32 import HDKey
    from jmwallet.wallet.models import UTXOInfo


@dataclass(frozen=True)
class SignedInput:
    """Result of signing a single transaction input.

    Attributes:
        signature: DER-encoded signature with the sighash-type byte appended.
        pubkey: Compressed public key bytes for the signing key.
        witness: The complete witness stack for this input. For P2WPKH this is
            ``[signature, pubkey]``; for a timelocked P2WSH fidelity bond it is
            ``[signature, witness_script]``.
    """

    signature: bytes
    pubkey: bytes
    witness: list[bytes]


class WalletSigningMixin:
    """Mixin centralizing all private-key access used for transaction signing.

    The host class (``WalletService``) must provide ``get_key_for_address``.
    """

    # Declared for mypy -- actually provided by the host WalletService.
    def get_key_for_address(self, address: str) -> HDKey | None:  # pragma: no cover
        raise NotImplementedError

    def sign_input(
        self,
        tx: ParsedTransaction,
        input_index: int,
        utxo: UTXOInfo,
    ) -> SignedInput:
        """Sign a single input belonging to this wallet.

        This is the single entry point through which private keys are accessed
        to produce transaction signatures. Callers select inputs and assemble
        the final transaction, but never handle key material directly.

        Args:
            tx: The parsed unsigned transaction being signed.
            input_index: Index of the input to sign within ``tx``.
            utxo: The wallet UTXO being spent at ``input_index``.

        Returns:
            A :class:`SignedInput` with the signature, public key and witness
            stack.

        Raises:
            TransactionSigningError: If the signing key is unknown, or the UTXO
                is a P2WSH output without an associated locktime (which would
                otherwise be impossible to sign).
        """
        key = self.get_key_for_address(utxo.address)
        if key is None:
            raise TransactionSigningError(f"Missing key for address {utxo.address}")

        pubkey_bytes = key.get_public_key_bytes(compressed=True)
        private_key = key.private_key

        if utxo.is_timelocked and utxo.locktime is not None:
            # Timelocked P2WSH fidelity bond.
            witness_script = mk_freeze_script(pubkey_bytes.hex(), utxo.locktime)
            signature = sign_p2wsh_input(
                tx=tx,
                input_index=input_index,
                witness_script=witness_script,
                value=utxo.value,
                private_key=private_key,
            )
            witness = create_p2wsh_witness_stack(signature, witness_script)
            return SignedInput(signature=signature, pubkey=pubkey_bytes, witness=witness)

        if utxo.is_p2wsh:
            # A P2WSH output we don't have a locktime for cannot be signed.
            raise TransactionSigningError(
                f"Cannot sign P2WSH UTXO {utxo.txid}:{utxo.vout} - locktime not available"
            )

        # Regular P2WPKH input.
        script_code = create_p2wpkh_script_code(pubkey_bytes)
        signature = sign_p2wpkh_input(
            tx=tx,
            input_index=input_index,
            script_code=script_code,
            value=utxo.value,
            private_key=private_key,
        )
        witness = create_witness_stack(signature, pubkey_bytes)
        return SignedInput(signature=signature, pubkey=pubkey_bytes, witness=witness)


__all__ = ["SignedInput", "WalletSigningMixin"]
