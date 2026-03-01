"""
JoinMarket wallet service with mixdepth support.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jmcore.btc_script import mk_freeze_script
from loguru import logger

from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.address import script_to_p2wsh_address
from jmwallet.wallet.bip32 import HDKey, mnemonic_to_seed
from jmwallet.wallet.coin_selection import CoinSelectionMixin
from jmwallet.wallet.constants import DEFAULT_SCAN_RANGE, FIDELITY_BOND_BRANCH
from jmwallet.wallet.display import WalletDisplayMixin
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.sync import WalletSyncMixin
from jmwallet.wallet.utxo_metadata import UTXOMetadataStore, load_metadata_store

# Re-export constants so external code importing from service.py still works
__all__ = [
    "DEFAULT_SCAN_RANGE",
    "FIDELITY_BOND_BRANCH",
    "WalletService",
]


class WalletService(WalletSyncMixin, CoinSelectionMixin, WalletDisplayMixin):
    """
    JoinMarket wallet service.
    Manages BIP84 hierarchical deterministic wallet with mixdepths.

    Derivation path: m/84'/0'/{mixdepth}'/{change}/{index}
    - mixdepth: 0-4 (JoinMarket isolation levels)
    - change: 0 (external/receive), 1 (internal/change)
    - index: address index
    """

    def __init__(
        self,
        mnemonic: str,
        backend: BlockchainBackend,
        network: str = "mainnet",
        mixdepth_count: int = 5,
        gap_limit: int = 20,
        data_dir: Path | None = None,
        passphrase: str = "",
    ):
        self.mnemonic = mnemonic
        self.backend = backend
        self.network = network
        self.mixdepth_count = mixdepth_count
        self.gap_limit = gap_limit
        self.data_dir = data_dir
        self.passphrase = passphrase

        seed = mnemonic_to_seed(mnemonic, passphrase)
        self.master_key = HDKey.from_seed(seed)

        coin_type = 0 if network == "mainnet" else 1
        self.root_path = f"m/84'/{coin_type}'"

        # Log fingerprint for debugging (helps identify passphrase issues)
        fingerprint = self.master_key.derive("m/0").fingerprint.hex()
        logger.info(
            f"Initialized wallet: fingerprint={fingerprint}, "
            f"mixdepths={mixdepth_count}, network={network}, "
            f"passphrase={'(set)' if passphrase else '(none)'}"
        )

        self.address_cache: dict[str, tuple[int, int, int]] = {}
        self.utxo_cache: dict[int, list[UTXOInfo]] = {}
        # Track addresses that have ever had UTXOs (including spent ones)
        # This is used to correctly label addresses as "used-empty" vs "new"
        self.addresses_with_history: set[str] = set()
        # Track addresses currently reserved for in-progress CoinJoin sessions
        # These addresses have been shared with a taker but the CoinJoin hasn't
        # completed yet. They must not be reused until the session ends.
        self.reserved_addresses: set[str] = set()
        # Cache for fidelity bond locktimes (address -> locktime)
        self.fidelity_bond_locktime_cache: dict[str, int] = {}

        # UTXO metadata store for frozen state and labels (BIP-329)
        self.metadata_store: UTXOMetadataStore | None = None
        if data_dir is not None:
            self.metadata_store = load_metadata_store(data_dir)

    # -- Key derivation & address generation (Group A) ----------------------

    def get_address(self, mixdepth: int, change: int, index: int) -> str:
        """Get address for given path"""
        if mixdepth >= self.mixdepth_count:
            raise ValueError(f"Mixdepth {mixdepth} exceeds maximum {self.mixdepth_count}")

        path = f"{self.root_path}/{mixdepth}'/{change}/{index}"
        key = self.master_key.derive(path)
        address = key.get_address(self.network)

        self.address_cache[address] = (mixdepth, change, index)

        return address

    def get_receive_address(self, mixdepth: int, index: int) -> str:
        """Get external (receive) address"""
        return self.get_address(mixdepth, 0, index)

    def get_change_address(self, mixdepth: int, index: int) -> str:
        """Get internal (change) address"""
        return self.get_address(mixdepth, 1, index)

    def get_account_xpub(self, mixdepth: int) -> str:
        """
        Get the extended public key (xpub) for a mixdepth account.

        Derives the key at path m/84'/coin'/mixdepth' and returns its xpub.
        This xpub can be used in Bitcoin Core descriptors for efficient scanning.

        Args:
            mixdepth: The mixdepth (account) number (0-4)

        Returns:
            xpub/tpub string for the account
        """
        account_path = f"{self.root_path}/{mixdepth}'"
        account_key = self.master_key.derive(account_path)
        return account_key.get_xpub(self.network)

    def get_account_zpub(self, mixdepth: int) -> str:
        """
        Get the BIP84 extended public key (zpub) for a mixdepth account.

        Derives the key at path m/84'/coin'/mixdepth' and returns its zpub.
        zpub explicitly indicates this is a native segwit (P2WPKH) wallet.

        Args:
            mixdepth: The mixdepth (account) number (0-4)

        Returns:
            zpub/vpub string for the account
        """
        account_path = f"{self.root_path}/{mixdepth}'"
        account_key = self.master_key.derive(account_path)
        return account_key.get_zpub(self.network)

    def get_scan_descriptors(self, scan_range: int = DEFAULT_SCAN_RANGE) -> list[dict[str, Any]]:
        """
        Generate descriptors for efficient UTXO scanning with Bitcoin Core.

        Creates wpkh() descriptors with xpub and range for all mixdepths,
        both external (receive) and internal (change) addresses.

        Using descriptors with ranges is much more efficient than scanning
        individual addresses, as Bitcoin Core can scan the entire range in
        a single pass through the UTXO set.

        Args:
            scan_range: Maximum index to scan (default 1000, Bitcoin Core's default)

        Returns:
            List of descriptor dicts for use with scantxoutset:
            [{"desc": "wpkh(xpub.../0/*)", "range": [0, 999]}, ...]
        """
        descriptors = []

        for mixdepth in range(self.mixdepth_count):
            xpub = self.get_account_xpub(mixdepth)

            # External (receive) addresses: .../0/*
            descriptors.append({"desc": f"wpkh({xpub}/0/*)", "range": [0, scan_range - 1]})

            # Internal (change) addresses: .../1/*
            descriptors.append({"desc": f"wpkh({xpub}/1/*)", "range": [0, scan_range - 1]})

        logger.debug(
            f"Generated {len(descriptors)} descriptors for {self.mixdepth_count} mixdepths "
            f"with range [0, {scan_range - 1}]"
        )
        return descriptors

    def get_fidelity_bond_key(self, index: int, locktime: int) -> HDKey:
        """
        Get the HD key for a fidelity bond.

        Fidelity bond path: m/84'/coin'/0'/2/index
        The locktime is NOT in the derivation path, but stored separately.

        Args:
            index: Address index within the fidelity bond branch
            locktime: Unix timestamp for the timelock (stored in path notation as :locktime)

        Returns:
            HDKey for the fidelity bond
        """
        # Fidelity bonds always use mixdepth 0, branch 2
        path = f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{index}"
        return self.master_key.derive(path)

    def get_fidelity_bond_address(self, index: int, locktime: int) -> str:
        """
        Get a fidelity bond P2WSH address.

        Creates a timelocked script: <locktime> OP_CLTV OP_DROP <pubkey> OP_CHECKSIG
        wrapped in P2WSH.

        Args:
            index: Address index within the fidelity bond branch
            locktime: Unix timestamp for the timelock

        Returns:
            P2WSH address for the fidelity bond
        """
        key = self.get_fidelity_bond_key(index, locktime)
        pubkey_hex = key.get_public_key_bytes(compressed=True).hex()

        # Create the timelock script
        script = mk_freeze_script(pubkey_hex, locktime)

        # Convert to P2WSH address
        address = script_to_p2wsh_address(script, self.network)

        # Cache with special path notation including locktime
        # Path format: m/84'/coin'/0'/2/index:locktime
        self.address_cache[address] = (0, FIDELITY_BOND_BRANCH, index)
        # Also store the locktime in a separate cache for fidelity bonds
        self.fidelity_bond_locktime_cache[address] = locktime

        logger.trace(f"Created fidelity bond address {address} with locktime {locktime}")
        return address

    def get_fidelity_bond_script(self, index: int, locktime: int) -> bytes:
        """
        Get the redeem script for a fidelity bond.

        Args:
            index: Address index within the fidelity bond branch
            locktime: Unix timestamp for the timelock

        Returns:
            Timelock redeem script bytes
        """
        key = self.get_fidelity_bond_key(index, locktime)
        pubkey_hex = key.get_public_key_bytes(compressed=True).hex()
        return mk_freeze_script(pubkey_hex, locktime)

    def get_locktime_for_address(self, address: str) -> int | None:
        """
        Get the locktime for a fidelity bond address.

        Args:
            address: The fidelity bond address

        Returns:
            Locktime as Unix timestamp, or None if not a fidelity bond address
        """
        return self.fidelity_bond_locktime_cache.get(address)

    def get_private_key(self, mixdepth: int, change: int, index: int) -> bytes:
        """Get private key for given path"""
        path = f"{self.root_path}/{mixdepth}'/{change}/{index}"
        key = self.master_key.derive(path)
        return key.get_private_key_bytes()

    def get_key_for_address(self, address: str) -> HDKey | None:
        """Get HD key for a known address"""
        if address not in self.address_cache:
            return None

        mixdepth, change, index = self.address_cache[address]
        path = f"{self.root_path}/{mixdepth}'/{change}/{index}"
        return self.master_key.derive(path)

    # -- Balance & UTXO queries (Group G) -----------------------------------

    async def get_balance(
        self, mixdepth: int, include_fidelity_bonds: bool = True, min_confirmations: int = 0
    ) -> int:
        """Get balance for a mixdepth.

        Args:
            mixdepth: Mixdepth to get balance for
            include_fidelity_bonds: If True (default), include fidelity bond UTXOs.
                                    If False, exclude fidelity bond UTXOs.
            min_confirmations: Minimum confirmations required (default: 0).

        Note:
            Frozen UTXOs are excluded from balance calculations.
        """
        if mixdepth not in self.utxo_cache:
            await self.sync_mixdepth(mixdepth)

        utxos = self.utxo_cache.get(mixdepth, [])
        utxos = [u for u in utxos if not u.frozen]
        if not include_fidelity_bonds:
            utxos = [u for u in utxos if not u.is_fidelity_bond]
        if min_confirmations > 0:
            utxos = [u for u in utxos if u.confirmations >= min_confirmations]
        return sum(utxo.value for utxo in utxos)

    async def get_balance_for_offers(self, mixdepth: int, min_confirmations: int = 0) -> int:
        """Get balance available for maker offers (excludes fidelity bond UTXOs).

        Fidelity bonds should never be automatically spent in CoinJoins,
        so makers must exclude them when calculating available offer amounts.
        """
        return await self.get_balance(
            mixdepth, include_fidelity_bonds=False, min_confirmations=min_confirmations
        )

    async def get_utxos(self, mixdepth: int) -> list[UTXOInfo]:
        """Get UTXOs for a mixdepth, syncing if not cached."""
        if mixdepth not in self.utxo_cache:
            await self.sync_mixdepth(mixdepth)
        return self.utxo_cache.get(mixdepth, [])

    def find_utxo_by_address(self, address: str) -> UTXOInfo | None:
        """
        Find a UTXO by its address across all mixdepths.

        This is useful for matching CoinJoin outputs to history entries.
        Returns the first matching UTXO found, or None if address not found.

        Args:
            address: Bitcoin address to search for

        Returns:
            UTXOInfo if found, None otherwise
        """
        for mixdepth in range(self.mixdepth_count):
            utxos = self.utxo_cache.get(mixdepth, [])
            for utxo in utxos:
                if utxo.address == address:
                    return utxo
        return None

    async def get_total_balance(
        self, include_fidelity_bonds: bool = True, min_confirmations: int = 0
    ) -> int:
        """Get total balance across all mixdepths.

        Args:
            include_fidelity_bonds: If True (default), include fidelity bond UTXOs.
                                    If False, exclude fidelity bond UTXOs.
            min_confirmations: Minimum confirmations required (default: 0).

        Note:
            Frozen UTXOs are excluded from balance calculations.
        """
        total = 0
        for mixdepth in range(self.mixdepth_count):
            balance = await self.get_balance(
                mixdepth,
                include_fidelity_bonds=include_fidelity_bonds,
                min_confirmations=min_confirmations,
            )
            total += balance
        return total

    async def get_fidelity_bond_balance(self, mixdepth: int) -> int:
        """Get balance of fidelity bond UTXOs for a mixdepth.

        Note:
            Frozen UTXOs are excluded from balance calculations.
        """
        if mixdepth not in self.utxo_cache:
            await self.sync_mixdepth(mixdepth)

        utxos = self.utxo_cache.get(mixdepth, [])
        return sum(utxo.value for utxo in utxos if utxo.is_fidelity_bond and not utxo.frozen)

    # -- Address index management (Group I) ---------------------------------

    def get_next_address_index(self, mixdepth: int, change: int) -> int:
        """
        Get next unused address index for mixdepth/change.

        Returns the highest index + 1 among all addresses that have ever been used,
        ensuring we never reuse addresses. An address is considered "used" if it:
        - Has current UTXOs
        - Had UTXOs in the past (tracked in addresses_with_history)
        - Appears in CoinJoin history (even if never funded)

        We always return one past the highest used index, even if lower indices
        appear unused. Those may have been skipped for a reason (e.g., shared in
        a failed CoinJoin, or spent in an internal transfer).
        """
        max_index = -1

        # Check addresses with current UTXOs
        utxos = self.utxo_cache.get(mixdepth, [])
        for utxo in utxos:
            if utxo.address in self.address_cache:
                md, ch, idx = self.address_cache[utxo.address]
                if md == mixdepth and ch == change and idx > max_index:
                    max_index = idx

        # Check addresses that ever had blockchain activity (including spent)
        for address in self.addresses_with_history:
            if address in self.address_cache:
                md, ch, idx = self.address_cache[address]
                if md == mixdepth and ch == change and idx > max_index:
                    max_index = idx

        # Check CoinJoin history for addresses that may have been shared
        # but never received funds (e.g., failed CoinJoins)
        if self.data_dir:
            from jmwallet.history import get_used_addresses

            cj_addresses = get_used_addresses(self.data_dir)
            for address in cj_addresses:
                if address in self.address_cache:
                    md, ch, idx = self.address_cache[address]
                    if md == mixdepth and ch == change and idx > max_index:
                        max_index = idx

        # Check addresses reserved for in-progress CoinJoin sessions
        # These have been shared with takers but the session hasn't completed yet
        for address in self.reserved_addresses:
            if address in self.address_cache:
                md, ch, idx = self.address_cache[address]
                if md == mixdepth and ch == change and idx > max_index:
                    max_index = idx

        return max_index + 1

    def reserve_addresses(self, addresses: set[str]) -> None:
        """
        Reserve addresses for an in-progress CoinJoin session.

        Once addresses are shared with a taker (in !ioauth message), they must not
        be reused even if the CoinJoin fails. This method marks addresses as reserved
        so get_next_address_index() will skip past them.

        Note: Addresses stay reserved until the wallet is restarted, since they may
        have been logged by counterparties. The CoinJoin history file provides
        persistent tracking across restarts.

        Args:
            addresses: Set of addresses to reserve (typically cj_address + change_address)
        """
        self.reserved_addresses.update(addresses)
        logger.debug(f"Reserved {len(addresses)} addresses: {addresses}")

    async def sync(self) -> dict[int, list[UTXOInfo]]:
        """Sync wallet (alias for sync_all for backward compatibility)."""
        return await self.sync_all()

    def get_new_address(self, mixdepth: int) -> str:
        """Get next unused receive address for a mixdepth."""
        next_index = self.get_next_address_index(mixdepth, 0)
        return self.get_receive_address(mixdepth, next_index)

    async def close(self) -> None:
        """Close backend connection"""
        await self.backend.close()

    # -- UTXO metadata (Group J) -------------------------------------------

    def _apply_frozen_state(self) -> None:
        """Apply frozen state from metadata store to all cached UTXOs.

        Called after sync operations to mark UTXOs that are frozen according
        to the persisted metadata. Also applies labels from metadata.

        Re-reads the metadata file from disk on each call to pick up changes
        made by other processes (e.g., ``jm-wallet freeze`` while maker is running).
        """
        if self.metadata_store is None:
            return

        # Re-read from disk to pick up changes from other processes
        self.metadata_store.load()

        frozen_outpoints = self.metadata_store.get_frozen_outpoints()

        frozen_count = 0
        for utxos in self.utxo_cache.values():
            for utxo in utxos:
                outpoint = utxo.outpoint
                utxo.frozen = outpoint in frozen_outpoints
                if utxo.frozen:
                    frozen_count += 1
                # Apply label from metadata if not already set
                stored_label = self.metadata_store.get_label(outpoint)
                if stored_label is not None and utxo.label is None:
                    utxo.label = stored_label

        if frozen_count > 0:
            logger.debug(f"Applied frozen state to {frozen_count} UTXO(s)")

    def freeze_utxo(self, outpoint: str) -> None:
        """Freeze a UTXO by outpoint (persisted to disk).

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.

        Raises:
            RuntimeError: If no metadata store is available (no data_dir).
        """
        if self.metadata_store is None:
            raise RuntimeError("Cannot freeze UTXOs without a data directory")
        self.metadata_store.freeze(outpoint)
        # Update the in-memory UTXO cache
        for utxos in self.utxo_cache.values():
            for utxo in utxos:
                if utxo.outpoint == outpoint:
                    utxo.frozen = True
                    return

    def unfreeze_utxo(self, outpoint: str) -> None:
        """Unfreeze a UTXO by outpoint (persisted to disk).

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.

        Raises:
            RuntimeError: If no metadata store is available (no data_dir).
        """
        if self.metadata_store is None:
            raise RuntimeError("Cannot unfreeze UTXOs without a data directory")
        self.metadata_store.unfreeze(outpoint)
        # Update the in-memory UTXO cache
        for utxos in self.utxo_cache.values():
            for utxo in utxos:
                if utxo.outpoint == outpoint:
                    utxo.frozen = False
                    return

    def toggle_freeze_utxo(self, outpoint: str) -> bool:
        """Toggle frozen state of a UTXO by outpoint (persisted to disk).

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.

        Returns:
            True if now frozen, False if now unfrozen.

        Raises:
            RuntimeError: If no metadata store is available (no data_dir).
        """
        if self.metadata_store is None:
            raise RuntimeError("Cannot toggle freeze without a data directory")
        now_frozen = self.metadata_store.toggle_freeze(outpoint)
        # Update the in-memory UTXO cache
        for utxos in self.utxo_cache.values():
            for utxo in utxos:
                if utxo.outpoint == outpoint:
                    utxo.frozen = now_frozen
                    break
        return now_frozen

    def is_utxo_frozen(self, outpoint: str) -> bool:
        """Check if a UTXO is frozen.

        Args:
            outpoint: Outpoint string in ``txid:vout`` format.

        Returns:
            True if frozen, False otherwise.
        """
        if self.metadata_store is None:
            return False
        return self.metadata_store.is_frozen(outpoint)
