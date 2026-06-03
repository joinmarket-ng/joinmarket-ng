"""
Wallet-owned authority for reverse-submarine-swap key material.

The taker must never touch raw private keys. All swap secrets are therefore
derived, held, and used inside the wallet service, which exposes only:

* public material the taker needs to build the swap request and HTLC script
  (preimage hash, claim public key), plus the preimage itself (which is
  published on-chain the moment the claim is spent, so it is not a long-term
  secret),
* a fully assembled claim witness when it is time to spend a lockup output,
* a symmetric key for encrypting swap-recovery records at rest.

Everything is derived deterministically from the wallet seed via BIP-85, so a
swap can be recovered from the persisted ``swap_index`` alone after restoring
the wallet, with no additional secret to back up.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from coincurve import PrivateKey
from jmcore.bitcoin import ParsedTransaction

from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.bip85 import derive_private_key, derive_symmetric_key
from jmwallet.wallet.signing import sign_p2wsh_input

# Storage-encryption key lives at HEX index 0; swap secrets use indices >= 1
# so a preimage derivation can never collide with the storage key.
_STORAGE_KEY_INDEX = 0
_MIN_SWAP_INDEX = 1
_MAX_SWAP_INDEX = (1 << 31) - 1  # BIP-85 hardened index ceiling
# Disjoint index offset for preimage HEX derivation so it never lands on the
# storage-key index even though both use the HEX application.
_PREIMAGE_HEX_OFFSET = _MIN_SWAP_INDEX


@dataclass(frozen=True)
class SwapKeyMaterial:
    """Public-facing swap key material handed to the taker.

    Deliberately excludes the claim private key, which never leaves the wallet.
    """

    index: int
    preimage: bytes
    preimage_hash: bytes
    claim_pubkey: bytes

    @property
    def preimage_hex(self) -> str:
        return self.preimage.hex()

    @property
    def preimage_hash_hex(self) -> str:
        return self.preimage_hash.hex()

    @property
    def claim_pubkey_hex(self) -> str:
        return self.claim_pubkey.hex()


class WalletSwapKeysMixin:
    """Mixin adding swap key derivation and signing to ``WalletService``."""

    master_key: HDKey

    def derive_swap_storage_key(self) -> bytes:
        """Return a 32-byte symmetric key for encrypting swap records.

        Stable for the lifetime of the wallet seed, so records survive
        restarts and can be decrypted again after a seed restore.
        """
        return derive_symmetric_key(self.master_key, index=_STORAGE_KEY_INDEX, num_bytes=32)

    def _claim_privkey(self, index: int) -> bytes:
        return derive_private_key(self.master_key, index)

    def _preimage(self, index: int) -> bytes:
        # HEX application in a disjoint index range from the storage key.
        return derive_symmetric_key(
            self.master_key, index=index + _PREIMAGE_HEX_OFFSET, num_bytes=32
        )

    def derive_swap_key_material(self, index: int) -> SwapKeyMaterial:
        """Re-derive public swap material for a known ``index``."""
        if not _MIN_SWAP_INDEX <= index <= _MAX_SWAP_INDEX:
            raise ValueError(f"swap index {index} out of range")
        preimage = self._preimage(index)
        preimage_hash = hashlib.sha256(preimage).digest()
        claim_pubkey = PrivateKey(self._claim_privkey(index)).public_key.format(compressed=True)
        return SwapKeyMaterial(
            index=index,
            preimage=preimage,
            preimage_hash=preimage_hash,
            claim_pubkey=claim_pubkey,
        )

    def create_swap_key_material(self) -> SwapKeyMaterial:
        """Pick a fresh random swap index and derive its material."""
        index = secrets.randbelow(_MAX_SWAP_INDEX - _MIN_SWAP_INDEX + 1) + _MIN_SWAP_INDEX
        return self.derive_swap_key_material(index)

    def build_swap_claim_witness(
        self,
        tx: ParsedTransaction,
        input_index: int,
        witness_script: bytes,
        value: int,
        swap_index: int,
    ) -> list[bytes]:
        """Sign and assemble the HTLC claim witness for a lockup output.

        The claim private key is derived, used, and discarded entirely inside
        the wallet; the taker receives only the finished witness stack
        ``[signature, preimage, witness_script]`` destined for the public chain.
        """
        privkey = PrivateKey(self._claim_privkey(swap_index))
        signature = sign_p2wsh_input(
            tx=tx,
            input_index=input_index,
            witness_script=witness_script,
            value=value,
            private_key=privkey,
        )
        preimage = self._preimage(swap_index)
        return [signature, preimage, witness_script]
