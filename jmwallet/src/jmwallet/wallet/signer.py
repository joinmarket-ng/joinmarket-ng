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
from hashlib import sha256
from typing import TYPE_CHECKING

from bitcointx.core.key import CKey
from jmcore.bitcoin import pubkey_to_p2wpkh_script, taproot_tweak_privkey
from jmcore.btc_script import mk_freeze_script

from jmwallet.wallet.signing import (
    ParsedTransaction,
    TransactionSigningError,
    create_p2wpkh_script_code,
    create_p2wsh_witness_stack,
    create_witness_stack,
    sign_p2tr_input,
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
        prevout_values: list[int] | None = None,
        prevout_scripts: list[bytes] | None = None,
    ) -> SignedInput:
        """Sign a single input belonging to this wallet.

        This is the single entry point through which private keys are accessed
        to produce transaction signatures. Callers select inputs and assemble
        the final transaction, but never handle key material directly.

        Args:
            tx: The parsed unsigned transaction being signed.
            input_index: Index of the input to sign within ``tx``.
            utxo: The wallet UTXO being spent at ``input_index``.
            prevout_values: Amounts of every input in ``tx`` ordered by index.
                Required only for taproot (P2TR) inputs, whose BIP341 sighash
                commits to all spent amounts.
            prevout_scripts: scriptPubKeys of every input in ``tx`` ordered by
                index. Required only for taproot inputs.

        Returns:
            A :class:`SignedInput` with the signature, public key and witness
            stack. For P2TR the ``pubkey`` is the 32-byte x-only output key and
            the ``signature`` is a 64-byte BIP340 Schnorr signature.

        Raises:
            TransactionSigningError: If the signing key is unknown, the UTXO is
                a P2WSH output without an associated locktime, or a taproot
                input is signed without the full prevout set.
        """
        if input_index < 0 or input_index >= len(tx.inputs):
            raise TransactionSigningError(f"Input index out of range: {input_index}")
        tx_input = tx.inputs[input_index]
        if tx_input.txid.lower() != utxo.txid.lower() or tx_input.vout != utxo.vout:
            raise TransactionSigningError(
                f"UTXO {utxo.txid}:{utxo.vout} does not match transaction input {input_index}"
            )

        try:
            key = self.get_key_for_address(utxo.address)
        except Exception as exc:
            raise TransactionSigningError(f"Missing key for address {utxo.address}") from exc
        if key is None:
            raise TransactionSigningError(f"Missing key for address {utxo.address}")

        if utxo.is_p2tr:
            if prevout_values is None or prevout_scripts is None:
                raise TransactionSigningError("Taproot inputs require the full prevout set to sign")
            # BIP86 key-path spend: sign with the tweaked output key over the
            # full prevout set; the witness is the single Schnorr signature.
            xonly = key.get_p2tr_output_xonly()
            output_private_key = CKey(taproot_tweak_privkey(key.private_key.secret_bytes))
            signature = sign_p2tr_input(
                tx=tx,
                input_index=input_index,
                prevouts_values=prevout_values,
                prevouts_scripts=prevout_scripts,
                private_key=output_private_key,
            )
            return SignedInput(signature=signature, pubkey=xonly, witness=[signature])

        pubkey_bytes = key.get_public_key_bytes(compressed=True)
        private_key = key.private_key

        if utxo.is_timelocked and utxo.locktime is not None:
            # Timelocked P2WSH fidelity bond.
            if not utxo.is_p2wsh:
                raise TransactionSigningError(
                    f"Fidelity bond UTXO {utxo.txid}:{utxo.vout} is not P2WSH"
                )
            witness_script = mk_freeze_script(pubkey_bytes.hex(), utxo.locktime)
            expected_scriptpubkey = b"\x00\x20" + sha256(witness_script).digest()
            if utxo.scriptpubkey.lower() != expected_scriptpubkey.hex():
                raise TransactionSigningError(
                    f"Fidelity bond script does not match wallet key for {utxo.txid}:{utxo.vout}"
                )
            if tx.locktime < utxo.locktime:
                raise TransactionSigningError(
                    f"Transaction locktime {tx.locktime} is below fidelity bond locktime "
                    f"{utxo.locktime}"
                )
            if tx_input.sequence == 0xFFFFFFFF:
                raise TransactionSigningError(
                    f"Transaction input {input_index} has a final sequence and cannot satisfy CLTV"
                )
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

        if not utxo.is_p2wpkh:
            raise TransactionSigningError(
                f"Unsupported scriptPubKey for UTXO {utxo.txid}:{utxo.vout}; "
                "only native P2WPKH and fidelity bond P2WSH inputs are supported"
            )

        expected_scriptpubkey = pubkey_to_p2wpkh_script(pubkey_bytes)
        if utxo.scriptpubkey.lower() != expected_scriptpubkey.hex():
            raise TransactionSigningError(
                f"P2WPKH script does not match wallet key for {utxo.txid}:{utxo.vout}"
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
