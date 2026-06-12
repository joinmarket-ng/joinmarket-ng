"""
Encrypted persistence for reverse-submarine-swap recovery.

A reverse submarine swap locks the provider's funds in an on-chain P2WSH
output that the taker claims by revealing the HTLC preimage. The secrets
required to claim that output are derived deterministically from the wallet
seed (BIP-85), so we never persist raw private keys: a record only stores the
``swap_index`` plus public swap metadata. After a crash or restart (or even a
full wallet restore from seed), the wallet re-derives the preimage and claim
key from ``swap_index`` and the lockup can be swept.

Records are still encrypted at rest with a wallet-derived symmetric key
(BIP-85 HEX application) so the on-disk metadata (swap ids, lockup addresses,
amounts) does not leak to anyone who does not control the wallet.

File layout (per wallet fingerprint)::

    <data_dir>/swaps/<fingerprint>/<swap_id>.swap

Each ``.swap`` file is ``salt (16 bytes) || Fernet(token)``, mirroring the
mnemonic-encryption scheme already used in ``jmcore.cli_common``. A fresh
random salt per write makes the ciphertext unique on every save while keeping
recovery fully deterministic from the wallet seed plus the stored salt.
"""

from __future__ import annotations

import base64
import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from jmcore.paths import get_swaps_dir
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from taker.swap.keys import SwapWallet

logger = logging.getLogger(__name__)

# Match the mnemonic-encryption parameters in jmcore.cli_common so the whole
# project uses one well-reviewed KDF configuration.
_SALT_BYTES = 16
_KDF_ITERATIONS = 600_000


class SwapRecordStatus(StrEnum):
    """Lifecycle of a persisted swap-recovery record."""

    PENDING_LOCKUP = "pending_lockup"  # Swap created, lockup not yet detected
    LOCKED = "locked"  # Lockup output confirmed on-chain, claimable
    BROADCAST = "broadcast"  # CoinJoin spending the lockup has been broadcast
    RESOLVED = "resolved"  # CoinJoin confirmed; preimage spent normally
    RECOVERED = "recovered"  # Reclaimed via a standalone recovery claim tx
    REFUNDED = "refunded"  # Provider refunded after CLTV; nothing to do
    ABANDONED = "abandoned"  # Operator gave up (e.g. secrets lost before lockup)


# Statuses for which no further recovery action is possible or required.
TERMINAL_STATUSES: frozenset[SwapRecordStatus] = frozenset(
    {
        SwapRecordStatus.RESOLVED,
        SwapRecordStatus.RECOVERED,
        SwapRecordStatus.REFUNDED,
        SwapRecordStatus.ABANDONED,
    }
)


class SwapRecord(BaseModel):
    """Everything needed to unilaterally claim a swap lockup output.

    No raw private key is stored: ``swap_index`` lets the wallet re-derive the
    preimage and claim key on demand via BIP-85.
    """

    swap_id: str = Field(description="Provider swap identifier (payment hash hex)")
    network: str = Field(description="Bitcoin network name (mainnet/testnet/signet/regtest)")

    # Wallet-derived key identifier and HTLC script.
    swap_index: int = Field(description="BIP-85 index for the wallet-derived claim key/preimage")
    redeem_script_hex: str = Field(description="HTLC witness script (hex)")
    lockup_address: str = Field(description="P2WSH lockup address")
    timeout_block_height: int = Field(description="CLTV height after which provider can refund")

    # Lockup outpoint (filled once the lockup is detected on-chain).
    txid: str = Field(default="", description="Lockup transaction id (empty until detected)")
    vout: int = Field(default=0, description="Lockup output index")
    value: int = Field(default=0, description="Lockup output value in sats")

    status: SwapRecordStatus = Field(default=SwapRecordStatus.PENDING_LOCKUP)
    coinjoin_txid: str | None = Field(
        default=None, description="Txid of the CoinJoin that spends the lockup, if broadcast"
    )
    recovery_txid: str | None = Field(
        default=None, description="Txid of a standalone recovery claim tx, if broadcast"
    )

    @property
    def witness_script(self) -> bytes:
        return bytes.fromhex(self.redeem_script_hex)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def has_lockup(self) -> bool:
        """True once we know the on-chain outpoint to claim."""
        return bool(self.txid) and self.value > 0


def _derive_key(storage_key: bytes, salt: bytes) -> bytes:
    """Stretch a wallet-derived storage key with a per-file salt into a Fernet key.

    The wallet already provides 32 bytes of high-entropy key material; the
    PBKDF2 pass binds it to the per-file salt so each record encrypts under a
    distinct key while remaining deterministically recoverable.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(storage_key))


class SwapPersistenceError(Exception):
    """Raised when a swap record cannot be read or decrypted."""


class SwapPersistence:
    """Encrypted, per-wallet store of :class:`SwapRecord` objects.

    The encryption key is derived from ``storage_key`` (a 32-byte symmetric key
    the wallet derives from its seed via BIP-85) plus a per-file salt. The same
    wallet always yields the same ``storage_key``, so records survive restarts
    and can be decrypted again after restoring the wallet from its seed.
    """

    _SUFFIX = ".swap"

    def __init__(
        self,
        storage_key: bytes,
        *,
        data_dir: Path | str | None = None,
        fingerprint: str | None = None,
    ) -> None:
        if not storage_key:
            raise ValueError("storage_key must be non-empty to derive the encryption key")
        self._storage_key = storage_key
        self._dir = get_swaps_dir(data_dir, fingerprint)

    @property
    def directory(self) -> Path:
        return self._dir

    def _path_for(self, swap_id: str) -> Path:
        safe = "".join(c for c in swap_id if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError(f"swap_id {swap_id!r} has no filesystem-safe characters")
        return self._dir / f"{safe}{self._SUFFIX}"

    def save(self, record: SwapRecord) -> Path:
        """Encrypt and atomically write a swap record.

        A fresh random salt is generated on every call, so the ciphertext
        differs each time even for identical plaintext, while remaining
        decryptable with the wallet seed material.
        """
        salt = os.urandom(_SALT_BYTES)
        key = _derive_key(self._storage_key, salt)
        token = Fernet(key).encrypt(record.model_dump_json().encode("utf-8"))
        blob = salt + token

        path = self._path_for(record.swap_id)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        logger.debug("Persisted swap record %s (status=%s)", record.swap_id, record.status)
        return path

    def _decrypt(self, blob: bytes, path: Path) -> SwapRecord:
        if len(blob) < _SALT_BYTES:
            raise SwapPersistenceError(f"Swap record too short to be valid: {path}")
        salt, token = blob[:_SALT_BYTES], blob[_SALT_BYTES:]
        key = _derive_key(self._storage_key, salt)
        try:
            plaintext = Fernet(key).decrypt(token)
        except InvalidToken as exc:
            raise SwapPersistenceError(
                f"Cannot decrypt swap record (wrong wallet seed or corrupt file): {path}"
            ) from exc
        return SwapRecord.model_validate_json(plaintext)

    def load(self, swap_id: str) -> SwapRecord | None:
        path = self._path_for(swap_id)
        if not path.exists():
            return None
        return self._decrypt(path.read_bytes(), path)

    def list_records(self) -> list[SwapRecord]:
        """Return every decryptable record in this wallet's swap directory.

        Files that cannot be decrypted (e.g. belonging to a different wallet
        sharing the directory) are skipped with a warning rather than raising,
        so a single foreign file never blocks recovery of the rest.
        """
        records: list[SwapRecord] = []
        for path in sorted(self._dir.glob(f"*{self._SUFFIX}")):
            try:
                records.append(self._decrypt(path.read_bytes(), path))
            except SwapPersistenceError as exc:
                logger.warning("Skipping unreadable swap record %s: %s", path.name, exc)
        return records

    def list_unresolved(self) -> list[SwapRecord]:
        """Return records that still need recovery attention."""
        return [r for r in self.list_records() if not r.is_terminal]

    def delete(self, swap_id: str) -> bool:
        path = self._path_for(swap_id)
        if path.exists():
            path.unlink()
            return True
        return False


def build_swap_persistence(wallet: SwapWallet) -> SwapPersistence | None:
    """Construct a :class:`SwapPersistence` bound to ``wallet``'s seed and dir.

    Returns None when the wallet has no on-disk ``data_dir`` (e.g. ephemeral
    test wallets), in which case swap recovery records cannot be persisted.
    """
    if wallet.data_dir is None:
        logger.debug("Wallet has no data_dir; swap recovery persistence disabled")
        return None
    return SwapPersistence(
        wallet.derive_swap_storage_key(),
        data_dir=wallet.data_dir,
        fingerprint=wallet.wallet_fingerprint,
    )
