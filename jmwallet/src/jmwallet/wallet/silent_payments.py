"""
Silent Payments (BIP352) wallet integration.

This module ties the BIP352 cryptographic primitives in
:mod:`jmcore.silentpayments` to the HD wallet seed. It derives the scan and
spend key pair from the wallet master key using the BIP352-recommended
derivation paths and exposes:

- the wallet's static silent payment address (for publishing, e.g. to receive
  anonymous donations to a maker),
- optional labeled addresses (including the reserved ``m=0`` change label),
- transaction scanning to detect incoming silent payments, and
- recovery of the taproot output private key needed to spend a detected output.

Derivation paths (BIP352 / BIP43 / BIP44), always hardened for the account
level keys::

     scan_private_key: m / 352' / coin_type' / account' / 1' / 0
    spend_private_key: m / 352' / coin_type' / account' / 0' / 0

``coin_type`` is 0 for mainnet and 1 otherwise, matching the rest of the wallet.

Privacy note: silent payment outputs always land as fresh, unlinkable taproot
UTXOs. JoinMarket treats them like mixdepth-0 deposits, which must never be
co-spent with fidelity bonds or other deposits without first being mixed. See
``docs/technical/silent_payments.md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jmcore.bitcoin import NetworkType, scriptpubkey_to_address
from jmcore.silentpayments import (
    FoundOutput,
    SilentPaymentAddress,
    SilentPaymentInput,
    create_label_tweak,
    create_labeled_address,
    scan_transaction,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from jmwallet.wallet.bip32 import HDKey

# BIP43 purpose for silent payments.
SILENT_PAYMENT_PURPOSE = 352

# Reserved change label (BIP352): never published, always scanned for.
CHANGE_LABEL = 0


class SilentPaymentWallet:
    """Derives and operates a wallet's silent payment key pair from the seed."""

    def __init__(self, master_key: HDKey, network: str = "mainnet", account: int = 0) -> None:
        self.network = network
        self.account = account
        coin_type = 0 if network == "mainnet" else 1
        base = f"m/{SILENT_PAYMENT_PURPOSE}'/{coin_type}'/{account}'"
        self._scan_key = master_key.derive(f"{base}/1'/0")
        self._spend_key = master_key.derive(f"{base}/0'/0")

    @property
    def scan_privkey(self) -> int:
        return int.from_bytes(self._scan_key.get_private_key_bytes(), "big")

    @property
    def spend_privkey(self) -> int:
        return int.from_bytes(self._spend_key.get_private_key_bytes(), "big")

    @property
    def scan_pubkey(self) -> bytes:
        return self._scan_key.get_public_key_bytes(compressed=True)

    @property
    def spend_pubkey(self) -> bytes:
        return self._spend_key.get_public_key_bytes(compressed=True)

    def get_address(self, label: int | None = None) -> str:
        """Return the wallet's silent payment address (optionally labeled).

        The reserved change label ``m=0`` must never be handed out and is
        rejected here; use it only internally for change detection.
        """
        if label is None:
            return SilentPaymentAddress(
                scan_pubkey=self.scan_pubkey, spend_pubkey=self.spend_pubkey
            ).encode(self.network)
        if label == CHANGE_LABEL:
            raise ValueError("The m=0 change label must never be published")
        return create_labeled_address(self.scan_privkey, self.spend_pubkey, label, self.network)

    def precompute_labels(self, labels: Sequence[int]) -> dict[bytes, int]:
        """Build the label-point -> tweak map for scanning (always incl. change)."""
        from coincurve import PublicKey

        result: dict[bytes, int] = {}
        for m in {CHANGE_LABEL, *labels}:
            tweak = create_label_tweak(self.scan_privkey, m)
            label_point = PublicKey.from_secret(tweak.to_bytes(32, "big")).format(compressed=True)
            result[label_point] = tweak
        return result

    def scan(
        self,
        inputs: Sequence[SilentPaymentInput],
        taproot_outputs: Sequence[bytes],
        labels: Sequence[int] = (),
    ) -> list[FoundOutput]:
        """Scan one transaction for silent payments to this wallet."""
        return scan_transaction(
            self.scan_privkey,
            self.spend_pubkey,
            inputs,
            taproot_outputs,
            self.precompute_labels(labels),
        )

    def output_private_key(self, found: FoundOutput) -> int:
        """Private key (scalar) to spend a detected silent payment output."""
        return found.output_private_key(self.spend_privkey)

    def output_address(self, found: FoundOutput) -> str:
        """The P2TR address of a detected silent payment output."""
        scriptpubkey = bytes([0x51, 0x20]) + found.pubkey_xonly
        return scriptpubkey_to_address(scriptpubkey, self.network)


def network_hrp(network: str | NetworkType) -> str:
    """Silent payment HRP (``sp`` mainnet, ``tsp`` testnets) for a network."""
    network = NetworkType(network) if isinstance(network, str) else network
    return "sp" if network == NetworkType.MAINNET else "tsp"
