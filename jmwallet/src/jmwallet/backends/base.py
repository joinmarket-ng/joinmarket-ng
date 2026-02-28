"""
Base blockchain backend interface.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from pydantic.dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class UTXO:
    txid: str
    vout: int
    value: int
    address: str
    confirmations: int
    scriptpubkey: str
    height: int | None = None


@dataclass
class Transaction:
    txid: str
    raw: str
    confirmations: int
    block_height: int | None = None
    block_time: int | None = None


@dataclass
class UTXOVerificationResult:
    """
    Result of UTXO verification with metadata.

    Used by neutrino_compat feature for Neutrino-compatible verification.
    """

    valid: bool
    value: int = 0
    confirmations: int = 0
    error: str | None = None
    scriptpubkey_matches: bool = False


@dataclass
class BondVerificationRequest:
    """Request to verify a single fidelity bond UTXO.

    All fields are derived from the bond proof data. The address and scriptpubkey
    are pre-computed by the caller using ``derive_bond_address(utxo_pub, locktime)``.
    """

    txid: str
    """Transaction ID (hex, big-endian)"""
    vout: int
    """Output index"""
    utxo_pub: bytes
    """33-byte compressed public key from bond proof"""
    locktime: int
    """Locktime from bond proof (Unix timestamp)"""
    address: str
    """Derived P2WSH bech32 address"""
    scriptpubkey: str
    """Derived P2WSH scriptPubKey (hex)"""


@dataclass
class BondVerificationResult:
    """Result of verifying a single fidelity bond UTXO."""

    txid: str
    """Transaction ID"""
    vout: int
    """Output index"""
    value: int
    """UTXO value in satoshis (0 if verification failed)"""
    confirmations: int
    """Number of confirmations (0 if unconfirmed or failed)"""
    block_time: int
    """Confirmation timestamp (0 if unconfirmed or failed)"""
    valid: bool
    """Whether the bond UTXO exists, is unspent, and has positive confirmations"""
    error: str | None = None
    """Error description if verification failed"""


class BlockchainBackend(ABC):
    """
    Abstract blockchain backend interface.
    Implementations provide access to blockchain data without requiring
    Bitcoin Core wallet functionality (avoiding BerkeleyDB issues).
    """

    @abstractmethod
    async def get_utxos(self, addresses: list[str]) -> list[UTXO]:
        """Get UTXOs for given addresses"""

    @abstractmethod
    async def get_address_balance(self, address: str) -> int:
        """Get balance for an address in satoshis"""

    @abstractmethod
    async def broadcast_transaction(self, tx_hex: str) -> str:
        """Broadcast transaction, returns txid"""

    @abstractmethod
    async def get_transaction(self, txid: str) -> Transaction | None:
        """Get transaction by txid"""

    @abstractmethod
    async def estimate_fee(self, target_blocks: int) -> float:
        """Estimate fee in sat/vbyte for target confirmation blocks.

        Returns:
            Fee rate in sat/vB. Can be fractional (e.g., 0.5 sat/vB).
        """

    async def get_mempool_min_fee(self) -> float | None:
        """Get the minimum fee rate (in sat/vB) for transaction to be accepted into mempool.

        This is used as a floor for fee estimation to ensure transactions are
        relayed and accepted into the mempool. Returns None if not supported
        or unavailable (e.g., light clients).

        Returns:
            Minimum fee rate in sat/vB, or None if unavailable.
        """
        return None

    def can_estimate_fee(self) -> bool:
        """Check if this backend can perform fee estimation.

        Full node backends (Bitcoin Core) can estimate fees.
        Light client backends (Neutrino) typically cannot.

        Returns:
            True if backend supports fee estimation, False otherwise.
        """
        return True

    def has_mempool_access(self) -> bool:
        """Check if this backend can access unconfirmed transactions in the mempool.

        Full node backends (Bitcoin Core) and API backends (Mempool.space) have
        mempool access and can verify transactions immediately after broadcast.

        Light client backends (Neutrino using BIP157/158) cannot access the mempool
        and can only see transactions after they're confirmed in a block. This
        affects broadcast verification strategy - see BroadcastPolicy docs.

        Returns:
            True if backend can see unconfirmed transactions, False otherwise.
        """
        return True

    @abstractmethod
    async def get_block_height(self) -> int:
        """Get current blockchain height"""

    @abstractmethod
    async def get_block_time(self, block_height: int) -> int:
        """Get block time (unix timestamp) for given height"""

    @abstractmethod
    async def get_block_hash(self, block_height: int) -> str:
        """Get block hash for given height"""

    @abstractmethod
    async def get_utxo(self, txid: str, vout: int) -> UTXO | None:
        """Get a specific UTXO from the blockchain UTXO set (gettxout).
        Returns None if the UTXO does not exist or has been spent."""

    async def scan_descriptors(
        self, descriptors: Sequence[str | dict[str, Any]]
    ) -> dict[str, Any] | None:
        """
        Scan the UTXO set using output descriptors.

        This is an efficient alternative to scanning individual addresses,
        especially useful for HD wallets where xpub descriptors with ranges
        can scan thousands of addresses in a single UTXO set pass.

        Example descriptors:
            - "addr(bc1q...)" - single address
            - "wpkh(xpub.../0/*)" - HD wallet addresses (default range 0-1000)
            - {"desc": "wpkh(xpub.../0/*)", "range": [0, 999]} - explicit range

        Args:
            descriptors: List of output descriptors (strings or dicts with range)

        Returns:
            Scan result dict with:
                - success: bool
                - unspents: list of found UTXOs
                - total_amount: sum of all found UTXOs
            Returns None if not supported or on failure.

        Note:
            Not all backends support descriptor scanning. The default implementation
            returns None. Override in backends that support it (e.g., Bitcoin Core).
        """
        # Default: not supported
        return None

    async def verify_utxo_with_metadata(
        self,
        txid: str,
        vout: int,
        scriptpubkey: str,
        blockheight: int,
    ) -> UTXOVerificationResult:
        """
        Verify a UTXO using provided metadata (neutrino_compat feature).

        This method allows light clients to verify UTXOs without needing
        arbitrary blockchain queries by using metadata provided by the peer.

        The implementation should:
        1. Use scriptpubkey to add the UTXO to watch list (for Neutrino)
        2. Use blockheight as a hint for efficient rescan
        3. Verify the UTXO exists with matching scriptpubkey
        4. Return the UTXO value and confirmations

        Default implementation falls back to get_utxo() for full node backends.

        Args:
            txid: Transaction ID
            vout: Output index
            scriptpubkey: Expected scriptPubKey (hex)
            blockheight: Block height where UTXO was confirmed

        Returns:
            UTXOVerificationResult with verification status and UTXO data
        """
        # Default implementation for full node backends
        # Just uses get_utxo() directly since we can query any UTXO
        utxo = await self.get_utxo(txid, vout)

        if utxo is None:
            return UTXOVerificationResult(
                valid=False,
                error="UTXO not found or spent",
            )

        # Verify scriptpubkey matches
        scriptpubkey_matches = utxo.scriptpubkey.lower() == scriptpubkey.lower()

        if not scriptpubkey_matches:
            return UTXOVerificationResult(
                valid=False,
                value=utxo.value,
                confirmations=utxo.confirmations,
                error="ScriptPubKey mismatch",
                scriptpubkey_matches=False,
            )

        return UTXOVerificationResult(
            valid=True,
            value=utxo.value,
            confirmations=utxo.confirmations,
            scriptpubkey_matches=True,
        )

    def requires_neutrino_metadata(self) -> bool:
        """
        Check if this backend requires Neutrino-compatible metadata for UTXO verification.

        Full node backends can verify any UTXO directly.
        Light client backends need scriptpubkey and blockheight hints.

        Returns:
            True if backend requires metadata for verification
        """
        return False

    def can_provide_neutrino_metadata(self) -> bool:
        """
        Check if this backend can provide Neutrino-compatible metadata to peers.

        This determines whether to advertise neutrino_compat feature to the network.
        Backends should return True if they can provide extended UTXO format with
        scriptpubkey and blockheight fields.

        Full node backends (Bitcoin Core) can provide this metadata.
        Light client backends (Neutrino) typically cannot reliably provide it for all UTXOs.

        Returns:
            True if backend can provide scriptpubkey and blockheight for its UTXOs
        """
        # Default: Full nodes can provide metadata, light clients cannot
        return not self.requires_neutrino_metadata()

    async def verify_tx_output(
        self,
        txid: str,
        vout: int,
        address: str,
        start_height: int | None = None,
    ) -> bool:
        """
        Verify that a specific transaction output exists (was broadcast and confirmed).

        This is useful for verifying a transaction was successfully broadcast when
        we know at least one of its output addresses (e.g., our coinjoin destination).

        For full node backends, this uses get_transaction().
        For light clients (neutrino), this uses UTXO lookup with the address hint.

        Args:
            txid: Transaction ID to verify
            vout: Output index to check
            address: The address that should own this output
            start_height: Optional block height hint for light clients (improves performance)

        Returns:
            True if the output exists (transaction was broadcast), False otherwise
        """
        # Default implementation for full node backends
        tx = await self.get_transaction(txid)
        return tx is not None

    async def verify_bonds(
        self,
        bonds: list[BondVerificationRequest],
    ) -> list[BondVerificationResult]:
        """Verify multiple fidelity bond UTXOs in bulk.

        This is the primary method for verifying fidelity bonds. Each backend can
        override this for optimal performance:
        - Bitcoin Core: JSON-RPC batch of gettxout calls (2 HTTP requests total)
        - Neutrino: batch address rescan + individual UTXO lookups
        - Mempool: parallel HTTP requests

        The default implementation calls get_utxo() sequentially with a semaphore.

        Args:
            bonds: List of bond verification requests with pre-computed addresses

        Returns:
            List of verification results, one per input bond (same order)
        """
        if not bonds:
            return []

        current_height = await self.get_block_height()
        semaphore = asyncio.Semaphore(10)

        async def _verify_one(bond: BondVerificationRequest) -> BondVerificationResult:
            async with semaphore:
                try:
                    utxo = await self.get_utxo(bond.txid, bond.vout)
                    if utxo is None:
                        return BondVerificationResult(
                            txid=bond.txid,
                            vout=bond.vout,
                            value=0,
                            confirmations=0,
                            block_time=0,
                            valid=False,
                            error="UTXO not found or spent",
                        )
                    if utxo.confirmations <= 0:
                        return BondVerificationResult(
                            txid=bond.txid,
                            vout=bond.vout,
                            value=utxo.value,
                            confirmations=0,
                            block_time=0,
                            valid=False,
                            error="UTXO unconfirmed",
                        )
                    # Get the block time for the confirmation block
                    conf_height = current_height - utxo.confirmations + 1
                    block_time = await self.get_block_time(conf_height)
                    return BondVerificationResult(
                        txid=bond.txid,
                        vout=bond.vout,
                        value=utxo.value,
                        confirmations=utxo.confirmations,
                        block_time=block_time,
                        valid=True,
                    )
                except Exception as e:
                    logger.warning(
                        "Bond verification failed for %s:%d: %s",
                        bond.txid,
                        bond.vout,
                        e,
                    )
                    return BondVerificationResult(
                        txid=bond.txid,
                        vout=bond.vout,
                        value=0,
                        confirmations=0,
                        block_time=0,
                        valid=False,
                        error=str(e),
                    )

        results = await asyncio.gather(*[_verify_one(b) for b in bonds])
        return list(results)

    async def close(self) -> None:
        """Close backend connection"""
        pass
