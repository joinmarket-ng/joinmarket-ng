"""
Wallet synchronization mixins.

Contains all sync-related methods: address-by-address scanning, descriptor-based
sync, descriptor wallet setup, and address path resolution.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jmcore.bitcoin import btc_to_sats, format_amount, get_hrp
from loguru import logger

from jmwallet.backends.base import BlockchainBackend
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.constants import DEFAULT_SCAN_RANGE, FIDELITY_BOND_BRANCH
from jmwallet.wallet.models import UTXOInfo


def _make_utxo_info(
    *,
    txid: str,
    vout: int,
    value: int,
    address: str,
    confirmations: int,
    scriptpubkey: str,
    path: str,
    mixdepth: int,
    height: int | None = None,
    locktime: int | None = None,
) -> UTXOInfo:
    """Factory for UTXOInfo construction, eliminating repeated kwarg blocks."""
    return UTXOInfo(
        txid=txid,
        vout=vout,
        value=value,
        address=address,
        confirmations=confirmations,
        scriptpubkey=scriptpubkey,
        path=path,
        mixdepth=mixdepth,
        height=height,
        locktime=locktime,
    )


class WalletSyncMixin:
    """Mixin providing wallet synchronization capabilities.

    Expects the host class to provide the attributes and methods defined
    on ``WalletService`` (backend, address_cache, utxo_cache, etc.).
    """

    # Declared for mypy -- actually set by the host class __init__
    backend: BlockchainBackend
    master_key: HDKey
    root_path: str
    network: str
    mixdepth_count: int
    gap_limit: int
    data_dir: Path | None
    address_cache: dict[str, tuple[int, int, int]]
    utxo_cache: dict[int, list[UTXOInfo]]
    addresses_with_history: set[str]
    fidelity_bond_locktime_cache: dict[str, int]
    address_type: str

    # Methods provided by the host class
    def get_address(self, mixdepth: int, change: int, index: int) -> str:
        raise NotImplementedError

    def get_account_xpub(self, mixdepth: int) -> str:
        raise NotImplementedError

    def get_fidelity_bond_address(self, index: int, locktime: int) -> str:
        raise NotImplementedError

    def _apply_frozen_state(self) -> None:
        raise NotImplementedError

    # -- Address-by-address sync (Groups B+C) --------------------------------

    async def sync_mixdepth(self, mixdepth: int) -> list[UTXOInfo]:
        """
        Sync a mixdepth with the blockchain.
        Scans addresses up to gap limit.
        """
        utxos: list[UTXOInfo] = []

        for change in [0, 1]:
            consecutive_empty = 0
            index = 0

            while consecutive_empty < self.gap_limit:
                # Scan in batches of gap_limit size for performance
                batch_size = self.gap_limit
                addresses = []

                for i in range(batch_size):
                    address = self.get_address(mixdepth, change, index + i)
                    addresses.append(address)

                # Fetch UTXOs for the whole batch
                backend_utxos = await self.backend.get_utxos(addresses)

                # Group results by address
                utxos_by_address: dict[str, list] = {addr: [] for addr in addresses}
                for utxo in backend_utxos:
                    if utxo.address in utxos_by_address:
                        utxos_by_address[utxo.address].append(utxo)

                # Process batch results in order
                for i, address in enumerate(addresses):
                    addr_utxos = utxos_by_address[address]

                    if addr_utxos:
                        consecutive_empty = 0
                        # Track that this address has had UTXOs
                        self.addresses_with_history.add(address)
                        for utxo in addr_utxos:
                            path = f"{self.root_path}/{mixdepth}'/{change}/{index + i}"
                            utxos.append(
                                _make_utxo_info(
                                    txid=utxo.txid,
                                    vout=utxo.vout,
                                    value=utxo.value,
                                    address=address,
                                    confirmations=utxo.confirmations,
                                    scriptpubkey=utxo.scriptpubkey,
                                    path=path,
                                    mixdepth=mixdepth,
                                    height=utxo.height,
                                )
                            )
                    else:
                        consecutive_empty += 1

                    if consecutive_empty >= self.gap_limit:
                        break

                index += batch_size

            logger.debug(
                f"Synced mixdepth {mixdepth} change {change}: "
                f"scanned ~{index} addresses, found "
                f"{len([u for u in utxos if u.path.split('/')[-2] == str(change)])} UTXOs"
            )

        self.utxo_cache[mixdepth] = utxos
        return utxos

    async def sync_fidelity_bonds(self, locktimes: list[int]) -> list[UTXOInfo]:
        """
        Sync fidelity bond UTXOs with specific locktimes.

        Fidelity bonds use mixdepth 0, branch 2, with path format:
        m/84'/coin'/0'/2/index:locktime

        Args:
            locktimes: List of Unix timestamps to scan for

        Returns:
            List of fidelity bond UTXOs found
        """
        utxos: list[UTXOInfo] = []

        if not locktimes:
            logger.debug("No locktimes provided for fidelity bond sync")
            return utxos

        for locktime in locktimes:
            consecutive_empty = 0
            index = 0

            while consecutive_empty < self.gap_limit:
                # Generate addresses for this locktime
                addresses = []
                for i in range(self.gap_limit):
                    address = self.get_fidelity_bond_address(index + i, locktime)
                    addresses.append(address)

                # Fetch UTXOs
                backend_utxos = await self.backend.get_utxos(addresses)

                # Group by address
                utxos_by_address: dict[str, list] = {addr: [] for addr in addresses}
                for utxo in backend_utxos:
                    if utxo.address in utxos_by_address:
                        utxos_by_address[utxo.address].append(utxo)

                # Process results
                for i, address in enumerate(addresses):
                    addr_utxos = utxos_by_address[address]

                    if addr_utxos:
                        consecutive_empty = 0
                        # Track that this address has had UTXOs
                        self.addresses_with_history.add(address)
                        for utxo in addr_utxos:
                            # Path includes locktime notation
                            path = (
                                f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{index + i}:{locktime}"
                            )
                            utxo_info = _make_utxo_info(
                                txid=utxo.txid,
                                vout=utxo.vout,
                                value=utxo.value,
                                address=address,
                                confirmations=utxo.confirmations,
                                scriptpubkey=utxo.scriptpubkey,
                                path=path,
                                mixdepth=0,  # Fidelity bonds always in mixdepth 0
                                height=utxo.height,
                                locktime=locktime,  # Store locktime for P2WSH signing
                            )
                            utxos.append(utxo_info)
                            logger.info(
                                f"Found fidelity bond UTXO: {utxo.txid}:{utxo.vout} "
                                f"value={utxo.value} locktime={locktime}"
                            )
                    else:
                        consecutive_empty += 1

                    if consecutive_empty >= self.gap_limit:
                        break

                index += self.gap_limit

        # Add fidelity bond UTXOs to mixdepth 0 cache
        if utxos:
            if 0 not in self.utxo_cache:
                self.utxo_cache[0] = []
            self.utxo_cache[0].extend(utxos)
            logger.info(f"Found {len(utxos)} fidelity bond UTXOs")

        return utxos

    async def discover_fidelity_bonds(
        self,
        max_index: int = 1,
        progress_callback: Any | None = None,
    ) -> list[UTXOInfo]:
        """
        Discover fidelity bonds by scanning all 960 possible locktimes.

        This is used during wallet recovery when the user doesn't know which
        locktimes they used. It generates addresses for all valid timenumbers
        (0-959, representing Jan 2020 through Dec 2099) and scans for UTXOs.

        For descriptor_wallet backend, this method will import addresses into
        the wallet as it scans in batches, then clean up addresses that had no UTXOs.

        The scan is optimized by:
        1. Using index=0 only (most users only use one address per locktime)
        2. Batching address generation and UTXO queries
        3. Optionally extending index range only for locktimes with funds

        Args:
            max_index: Maximum address index to scan per locktime (default 1).
                      Higher values increase scan time linearly.
            progress_callback: Optional callback(timenumber, total) for progress updates

        Returns:
            List of discovered fidelity bond UTXOs
        """
        from jmcore.timenumber import TIMENUMBER_COUNT, timenumber_to_timestamp

        logger.info(
            f"Starting fidelity bond discovery scan "
            f"({TIMENUMBER_COUNT} timelocks × {max_index} index(es))"
        )

        discovered_utxos: list[UTXOInfo] = []
        batch_size = 100  # Process timenumbers in batches
        descriptor_backend: DescriptorWalletBackend | None = (
            self.backend if isinstance(self.backend, DescriptorWalletBackend) else None
        )

        # Build the full address map across all timenumbers first.
        all_address_to_locktime: dict[str, tuple[int, int]] = {}
        for timenumber in range(TIMENUMBER_COUNT):
            locktime = timenumber_to_timestamp(timenumber)
            for idx in range(max_index):
                address = self.get_fidelity_bond_address(idx, locktime)
                all_address_to_locktime[address] = (locktime, idx)

        # For descriptor wallets, import all addresses in batches WITHOUT triggering
        # a per-batch rescan.  A single blockchain rescan is run after all descriptors
        # are imported so Bitcoin Core never rejects a batch with RPC -4
        # "Wallet is currently rescanning".
        if descriptor_backend is not None:
            all_bond_addrs = [
                (addr, lt, idx) for addr, (lt, idx) in all_address_to_locktime.items()
            ]
            for batch_start in range(0, len(all_bond_addrs), batch_size):
                batch = all_bond_addrs[batch_start : batch_start + batch_size]
                batch_end = batch_start + len(batch)
                try:
                    await self.import_fidelity_bond_addresses(
                        fidelity_bond_addresses=batch,
                        rescan=False,
                    )
                except Exception as e:
                    logger.error(f"Failed to import batch {batch_start}-{batch_end}: {e}")

                if progress_callback:
                    progress_callback(batch_end, TIMENUMBER_COUNT)

            # Single rescan after all descriptors are registered.
            logger.info(
                "All fidelity bond addresses imported, starting full rescan from genesis..."
            )
            await descriptor_backend.start_background_rescan(0)
            await descriptor_backend.wait_for_rescan_complete(
                poll_interval=5.0,
                progress_callback=lambda p: logger.debug(f"Rescan progress: {p:.1%}"),
            )

            # Query all UTXOs in a single call after rescan completes.
            all_addresses = list(all_address_to_locktime.keys())
            address_to_locktime = all_address_to_locktime
            try:
                backend_utxos = await self.backend.get_utxos(all_addresses)
            except Exception as e:
                logger.error(f"Failed to fetch UTXOs after rescan: {e}")
                backend_utxos = []
        else:
            # Non-descriptor backends: scan in batches and query UTXOs per batch.
            backend_utxos = []
            address_to_locktime = all_address_to_locktime
            all_addresses_list = list(all_address_to_locktime.keys())
            for batch_start in range(0, len(all_addresses_list), batch_size):
                batch_addrs = all_addresses_list[batch_start : batch_start + batch_size]
                batch_end = batch_start + len(batch_addrs)
                try:
                    batch_utxos = await self.backend.get_utxos(batch_addrs)
                    backend_utxos.extend(batch_utxos)
                except Exception as e:
                    logger.error(f"Failed to scan batch {batch_start}-{batch_end}: {e}")

                if progress_callback:
                    progress_callback(batch_end, TIMENUMBER_COUNT)

        from jmcore.timenumber import format_locktime_date

        # Process found UTXOs
        for utxo in backend_utxos:
            if utxo.address in address_to_locktime:
                locktime, idx = address_to_locktime[utxo.address]
                path = f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{idx}:{locktime}"

                utxo_info = _make_utxo_info(
                    txid=utxo.txid,
                    vout=utxo.vout,
                    value=utxo.value,
                    address=utxo.address,
                    confirmations=utxo.confirmations,
                    scriptpubkey=utxo.scriptpubkey,
                    path=path,
                    mixdepth=0,
                    height=utxo.height,
                    locktime=locktime,
                )
                discovered_utxos.append(utxo_info)

                logger.info(
                    f"Discovered fidelity bond: {utxo.txid}:{utxo.vout} "
                    f"value={utxo.value:,} sats, locktime={format_locktime_date(locktime)}"
                )

        # Add discovered UTXOs to mixdepth 0 cache
        if discovered_utxos:
            if 0 not in self.utxo_cache:
                self.utxo_cache[0] = []
            # Avoid duplicates
            existing_outpoints = {(u.txid, u.vout) for u in self.utxo_cache[0]}
            for utxo_info in discovered_utxos:
                if (utxo_info.txid, utxo_info.vout) not in existing_outpoints:
                    self.utxo_cache[0].append(utxo_info)

            logger.info(f"Discovery complete: found {len(discovered_utxos)} fidelity bond(s)")
        else:
            logger.info("Discovery complete: no fidelity bonds found")

        return discovered_utxos

    async def sync_all(
        self,
        fidelity_bond_addresses: list[tuple[str, int, int]] | None = None,
    ) -> dict[int, list[UTXOInfo]]:
        """
        Sync all mixdepths, optionally including fidelity bond addresses.

        Args:
            fidelity_bond_addresses: Optional list of (address, locktime, index) tuples
                                    for fidelity bonds to scan with wallet descriptors

        Returns:
            Dictionary mapping mixdepth to list of UTXOs
        """
        logger.info("Syncing all mixdepths...")

        # Try efficient descriptor-based sync if backend supports it
        if self.backend.supports_descriptor_scan:
            result = await self._sync_all_with_descriptors(fidelity_bond_addresses)
            if result is not None:
                self._apply_frozen_state()
                return result
            # Fall back to address-by-address sync on failure
            logger.warning("Descriptor scan failed, falling back to address scan")

        # Legacy address-by-address scanning
        # Pre-register ALL wallet addresses (all mixdepths × both branches × gap_limit)
        # with the backend before the first get_utxos call triggers any rescan.
        # Without this, light-client backends (Neutrino) fire the initial rescan on the
        # first get_utxos call with only the *external* addresses registered, causing
        # change (internal) addresses to be missed entirely.
        if self.backend.supports_watch_address:
            for pre_mixdepth in range(self.mixdepth_count):
                for pre_change in [0, 1]:
                    for pre_index in range(self.gap_limit):
                        addr = self.get_address(pre_mixdepth, pre_change, pre_index)
                        await self.backend.add_watch_address(addr)
            logger.debug(
                f"Pre-registered {self.mixdepth_count * 2 * self.gap_limit} addresses "
                "with backend before initial rescan"
            )

        result = {}
        for mixdepth in range(self.mixdepth_count):
            utxos = await self.sync_mixdepth(mixdepth)
            result[mixdepth] = utxos
        logger.info(f"Sync complete: {sum(len(u) for u in result.values())} total UTXOs")
        self._apply_frozen_state()
        return result

    # -- Descriptor-based sync (Group D) ------------------------------------

    async def _sync_all_with_descriptors(
        self,
        fidelity_bond_addresses: list[tuple[str, int, int]] | None = None,
    ) -> dict[int, list[UTXOInfo]] | None:
        """
        Sync all mixdepths using efficient descriptor scanning.

        This scans the entire wallet in a single UTXO set pass using xpub descriptors,
        which is much faster than scanning addresses individually (especially on mainnet
        where a full UTXO set scan takes ~90 seconds).

        Args:
            fidelity_bond_addresses: Optional list of (address, locktime, index) tuples to scan
                                    in the same pass as wallet descriptors

        Returns:
            Dictionary mapping mixdepth to list of UTXOInfo, or None on failure
        """
        # Generate descriptors for all mixdepths and build a lookup table
        scan_range = max(DEFAULT_SCAN_RANGE, self.gap_limit * 10)
        descriptors: list[str | dict[str, Any]] = []
        # Map descriptor string (without checksum) -> (mixdepth, change)
        desc_to_path: dict[str, tuple[int, int]] = {}
        # Map fidelity bond address -> (locktime, index)
        bond_address_to_info: dict[str, tuple[int, int]] = {}

        wrapper = "tr" if self.address_type == "p2tr" else "wpkh"
        for mixdepth in range(self.mixdepth_count):
            xpub = self.get_account_xpub(mixdepth)

            # External (receive) addresses: .../0/*
            desc_ext = f"{wrapper}({xpub}/0/*)"
            descriptors.append({"desc": desc_ext, "range": [0, scan_range - 1]})
            desc_to_path[desc_ext] = (mixdepth, 0)

            # Internal (change) addresses: .../1/*
            desc_int = f"{wrapper}({xpub}/1/*)"
            descriptors.append({"desc": desc_int, "range": [0, scan_range - 1]})
            desc_to_path[desc_int] = (mixdepth, 1)

            if self.address_type == "p2tr":
                # Also import wpkh() descriptors over the same BIP86 paths so
                # that P2WPKH addresses generated for legacy CoinJoin takers
                # are discoverable on rescan.
                wpkh_ext = f"wpkh({xpub}/0/*)"
                descriptors.append({"desc": wpkh_ext, "range": [0, scan_range - 1]})
                desc_to_path[wpkh_ext] = (mixdepth, 0)

                wpkh_int = f"wpkh({xpub}/1/*)"
                descriptors.append({"desc": wpkh_int, "range": [0, scan_range - 1]})
                desc_to_path[wpkh_int] = (mixdepth, 1)

        # Add fidelity bond addresses to the scan
        if fidelity_bond_addresses:
            expected_hrp = get_hrp(self.network)
            valid_bonds = []
            for address, locktime, index in fidelity_bond_addresses:
                # Skip addresses whose bech32 HRP doesn't match the current network
                # (e.g. mainnet bc1q... addresses loaded into a regtest/signet wallet)
                addr_hrp = address.split("1")[0].lower() if "1" in address else ""
                if addr_hrp != expected_hrp:
                    logger.warning(
                        f"Skipping fidelity bond address {address!r}: network mismatch "
                        f"(expected HRP {expected_hrp!r}, got {addr_hrp!r})"
                    )
                    continue
                valid_bonds.append((address, locktime, index))

            if valid_bonds:
                logger.info(f"Including {len(valid_bonds)} fidelity bond address(es) in scan")
            for address, locktime, index in valid_bonds:
                descriptors.append(f"addr({address})")
                bond_address_to_info[address] = (locktime, index)
                # Cache the address with the correct index from registry
                self.address_cache[address] = (0, FIDELITY_BOND_BRANCH, index)
                self.fidelity_bond_locktime_cache[address] = locktime

        # Get current block height for confirmation calculation
        try:
            tip_height = await self.backend.get_block_height()
        except Exception as e:
            logger.error(f"Failed to get block height for descriptor scan: {e}")
            return None

        # Perform the scan
        scan_result = await self.backend.scan_descriptors(descriptors)
        if not scan_result or not scan_result.get("success", False):
            return None

        # Parse results and organize by mixdepth
        result: dict[int, list[UTXOInfo]] = {md: [] for md in range(self.mixdepth_count)}
        fidelity_bond_utxos: list[UTXOInfo] = []

        for utxo_data in scan_result.get("unspents", []):
            desc = utxo_data.get("desc", "")

            # Check if this is a fidelity bond address result
            # Fidelity bond descriptors are returned as: addr(bc1q...)#checksum
            if "#" in desc:
                desc_base = desc.split("#")[0]
            else:
                desc_base = desc

            if desc_base.startswith("addr(") and desc_base.endswith(")"):
                bond_address = desc_base[5:-1]
                if bond_address in bond_address_to_info:
                    # This is a fidelity bond UTXO
                    locktime, index = bond_address_to_info[bond_address]
                    confirmations = 0
                    utxo_height = utxo_data.get("height", 0)
                    if utxo_height > 0:
                        confirmations = tip_height - utxo_height + 1

                    # Path format for fidelity bonds: m/84'/0'/0'/2/index:locktime
                    path = f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{index}:{locktime}"

                    utxo_info = _make_utxo_info(
                        txid=utxo_data["txid"],
                        vout=utxo_data["vout"],
                        value=btc_to_sats(utxo_data["amount"]),
                        address=bond_address,
                        confirmations=confirmations,
                        scriptpubkey=utxo_data.get("scriptPubKey", ""),
                        path=path,
                        mixdepth=0,  # Fidelity bonds in mixdepth 0
                        height=utxo_height if utxo_height > 0 else None,
                        locktime=locktime,
                    )
                    fidelity_bond_utxos.append(utxo_info)
                    logger.info(
                        f"Found fidelity bond UTXO: {utxo_info.txid}:{utxo_info.vout} "
                        f"value={utxo_info.value} locktime={locktime} index={index}"
                    )
                    continue

            # Parse the descriptor to extract change and index for regular wallet UTXOs
            # Descriptor format from Bitcoin Core when using xpub:
            # wpkh([fingerprint/change/index]pubkey)#checksum
            # The fingerprint is the parent xpub's fingerprint
            path_info = self._parse_descriptor_path(desc, desc_to_path)

            if path_info is None:
                logger.warning(f"Could not parse path from descriptor: {desc}")
                continue

            mixdepth, change, index = path_info

            # Calculate confirmations
            confirmations = 0
            utxo_height = utxo_data.get("height", 0)
            if utxo_height > 0:
                confirmations = tip_height - utxo_height + 1

            # Generate the address and cache it
            address = self.get_address(mixdepth, change, index)

            # Track that this address has had UTXOs
            self.addresses_with_history.add(address)

            # Build path string
            path = f"{self.root_path}/{mixdepth}'/{change}/{index}"

            utxo_info = _make_utxo_info(
                txid=utxo_data["txid"],
                vout=utxo_data["vout"],
                value=btc_to_sats(utxo_data["amount"]),
                address=address,
                confirmations=confirmations,
                scriptpubkey=utxo_data.get("scriptPubKey", ""),
                path=path,
                mixdepth=mixdepth,
                height=utxo_height if utxo_height > 0 else None,
            )
            result[mixdepth].append(utxo_info)

        # Add fidelity bond UTXOs to mixdepth 0
        if fidelity_bond_utxos:
            result[0].extend(fidelity_bond_utxos)

        # Update cache
        self.utxo_cache = result

        total_utxos = sum(len(u) for u in result.values())
        total_value = sum(sum(u.value for u in utxos) for utxos in result.values())
        bond_count = len(fidelity_bond_utxos)
        if bond_count > 0:
            logger.info(
                f"Descriptor sync complete: {total_utxos} UTXOs "
                f"({bond_count} fidelity bond(s)), {format_amount(total_value)} total"
            )
        else:
            logger.info(
                f"Descriptor sync complete: {total_utxos} UTXOs, {format_amount(total_value)} total"
            )

        return result

    async def setup_descriptor_wallet(
        self,
        scan_range: int = DEFAULT_SCAN_RANGE,
        fidelity_bond_addresses: list[tuple[str, int, int]] | None = None,
        rescan: bool = True,
        check_existing: bool = True,
        smart_scan: bool = True,
        background_full_rescan: bool = True,
    ) -> bool:
        """
        Setup descriptor wallet backend for efficient UTXO tracking.

        This imports wallet descriptors into Bitcoin Core's descriptor wallet,
        enabling fast UTXO queries via listunspent instead of slow scantxoutset.

        By default, uses smart scan for fast startup (~1 minute instead of 20+ minutes)
        with a background full rescan to catch any older transactions.

        Should be called once on first use or when restoring a wallet.
        Subsequent operations will be much faster.

        Args:
            scan_range: Address index range to import (default 1000)
            fidelity_bond_addresses: Optional list of (address, locktime, index) tuples
            rescan: Whether to rescan blockchain
            check_existing: If True, checks if wallet is already set up and skips import
            smart_scan: If True and rescan=True, scan from ~1 year ago for fast startup.
                       A full rescan runs in background to catch older transactions.
            background_full_rescan: If True and smart_scan=True, run full rescan in background

        Returns:
            True if setup completed successfully

        Raises:
            RuntimeError: If backend is not DescriptorWalletBackend

        Example:
            # Fast setup with smart scan (default) - starts quickly, full scan in background
            await wallet.setup_descriptor_wallet(rescan=True)

            # Full scan from genesis (slow but complete) - use for wallet recovery
            await wallet.setup_descriptor_wallet(rescan=True, smart_scan=False)

            # No rescan (for brand new wallets with no history)
            await wallet.setup_descriptor_wallet(rescan=False)
        """
        if not isinstance(self.backend, DescriptorWalletBackend):
            raise RuntimeError(
                "setup_descriptor_wallet() requires DescriptorWalletBackend. "
                "Current backend does not support descriptor wallets."
            )

        # Check if already set up (unless explicitly disabled)
        if check_existing:
            expected_count = self.mixdepth_count * 2  # external + internal per mixdepth
            if fidelity_bond_addresses:
                expected_count += len(fidelity_bond_addresses)

            if await self.backend.is_wallet_setup(expected_descriptor_count=expected_count):
                logger.info("Descriptor wallet already set up, skipping import")
                return True

        # Generate descriptors for all mixdepths
        descriptors = self._generate_import_descriptors(scan_range)

        # Add fidelity bond addresses
        if fidelity_bond_addresses:
            logger.info(f"Including {len(fidelity_bond_addresses)} fidelity bond addresses")
            for address, locktime, index in fidelity_bond_addresses:
                descriptors.append(
                    {
                        "desc": f"addr({address})",
                        "internal": False,
                    }
                )
                # Cache the address info
                self.address_cache[address] = (0, FIDELITY_BOND_BRANCH, index)
                self.fidelity_bond_locktime_cache[address] = locktime

        # Setup wallet and import descriptors
        logger.info("Setting up descriptor wallet...")
        await self.backend.setup_wallet(
            descriptors,
            rescan=rescan,
            smart_scan=smart_scan,
            background_full_rescan=background_full_rescan,
        )
        logger.info("Descriptor wallet setup complete")
        return True

    async def is_descriptor_wallet_ready(self, fidelity_bond_count: int = 0) -> bool:
        """
        Check if descriptor wallet is already set up and ready to use.

        Args:
            fidelity_bond_count: Expected number of fidelity bond addresses

        Returns:
            True if wallet is set up with all expected descriptors

        Example:
            if await wallet.is_descriptor_wallet_ready():
                # Just sync
                utxos = await wallet.sync_with_descriptor_wallet()
            else:
                # First time - import descriptors
                await wallet.setup_descriptor_wallet(rescan=True)
        """
        if not isinstance(self.backend, DescriptorWalletBackend):
            return False

        expected_count = self.mixdepth_count * 2  # external + internal per mixdepth
        if fidelity_bond_count > 0:
            expected_count += fidelity_bond_count

        return await self.backend.is_wallet_setup(expected_descriptor_count=expected_count)

    async def import_fidelity_bond_addresses(
        self,
        fidelity_bond_addresses: list[tuple[str, int, int]],
        rescan: bool = True,
    ) -> bool:
        """
        Import fidelity bond addresses into the descriptor wallet.

        This is used to add fidelity bond addresses that weren't included
        in the initial wallet setup. Fidelity bonds use P2WSH addresses
        (timelocked scripts) that are not part of the standard BIP84 derivation,
        so they must be explicitly imported.

        Args:
            fidelity_bond_addresses: List of (address, locktime, index) tuples
            rescan: Whether to rescan the blockchain for these addresses

        Returns:
            True if import succeeded

        Raises:
            RuntimeError: If backend is not DescriptorWalletBackend
        """
        if not isinstance(self.backend, DescriptorWalletBackend):
            raise RuntimeError("import_fidelity_bond_addresses() requires DescriptorWalletBackend")

        if not fidelity_bond_addresses:
            return True

        # Build descriptors for the bond addresses
        descriptors = []
        for address, locktime, index in fidelity_bond_addresses:
            descriptors.append(
                {
                    "desc": f"addr({address})",
                    "internal": False,
                }
            )
            # Cache the address info
            self.address_cache[address] = (0, FIDELITY_BOND_BRANCH, index)
            self.fidelity_bond_locktime_cache[address] = locktime

        logger.info(f"Importing {len(descriptors)} fidelity bond address(es)...")
        await self.backend.import_descriptors(descriptors, rescan=rescan)
        logger.info("Fidelity bond addresses imported")
        return True

    def _generate_import_descriptors(
        self, scan_range: int = DEFAULT_SCAN_RANGE
    ) -> list[dict[str, Any]]:
        """
        Generate descriptors for importdescriptors RPC.

        Creates descriptors for all mixdepths (external and internal addresses)
        with proper formatting for Bitcoin Core's importdescriptors.

        Args:
            scan_range: Maximum index to import

        Returns:
            List of descriptor dicts for importdescriptors
        """
        descriptors = []

        wrapper = "tr" if self.address_type == "p2tr" else "wpkh"
        for mixdepth in range(self.mixdepth_count):
            xpub = self.get_account_xpub(mixdepth)

            # External (receive) addresses: .../0/*
            descriptors.append(
                {
                    "desc": f"{wrapper}({xpub}/0/*)",
                    "range": [0, scan_range - 1],
                    "internal": False,
                }
            )

            # Internal (change) addresses: .../1/*
            descriptors.append(
                {
                    "desc": f"{wrapper}({xpub}/1/*)",
                    "range": [0, scan_range - 1],
                    "internal": True,
                }
            )

        logger.debug(
            f"Generated {len(descriptors)} import descriptors for "
            f"{self.mixdepth_count} mixdepths with range [0, {scan_range - 1}]"
        )
        return descriptors

    # -- Descriptor wallet fast path (Group E) ------------------------------

    async def sync_with_descriptor_wallet(
        self,
        fidelity_bond_addresses: list[tuple[str, int, int]] | None = None,
    ) -> dict[int, list[UTXOInfo]]:
        """
        Sync wallet using descriptor wallet backend (fast listunspent).

        This is MUCH faster than scantxoutset because it only queries the
        wallet's tracked UTXOs, not the entire UTXO set.

        Args:
            fidelity_bond_addresses: Optional fidelity bond addresses to include

        Returns:
            Dictionary mapping mixdepth to list of UTXOs

        Raises:
            RuntimeError: If backend is not DescriptorWalletBackend
        """
        if not isinstance(self.backend, DescriptorWalletBackend):
            raise RuntimeError("sync_with_descriptor_wallet() requires DescriptorWalletBackend")

        logger.info("Syncing via descriptor wallet (listunspent)...")

        # Get the current descriptor range from Bitcoin Core and cache it
        # This is used by _find_address_path to know how far to scan
        current_range = await self.backend.get_max_descriptor_range()
        self._current_descriptor_range = current_range
        logger.debug(f"Current descriptor range: [0, {current_range}]")

        # Pre-populate address cache for the entire descriptor range
        # This is more efficient than deriving addresses one by one during lookup
        await self._populate_address_cache(current_range)

        # Get all wallet UTXOs at once
        all_utxos = await self.backend.get_all_utxos()

        # Organize UTXOs by mixdepth
        result: dict[int, list[UTXOInfo]] = {md: [] for md in range(self.mixdepth_count)}
        fidelity_bond_utxos: list[UTXOInfo] = []

        # Build fidelity bond address lookup
        # Note: Normalize addresses to lowercase for consistent comparison
        # (bech32 addresses are case-insensitive but Python string comparison is not)
        bond_address_to_info: dict[str, tuple[int, int]] = {}
        if fidelity_bond_addresses:
            for address, locktime, index in fidelity_bond_addresses:
                addr_lower = address.lower()
                bond_address_to_info[addr_lower] = (locktime, index)
                self.address_cache[addr_lower] = (0, FIDELITY_BOND_BRANCH, index)
                self.fidelity_bond_locktime_cache[addr_lower] = locktime
            logger.debug(f"Registered {len(bond_address_to_info)} fidelity bond addresses for sync")

        for utxo in all_utxos:
            # Normalize address to lowercase for consistent comparison
            # (bech32 addresses are case-insensitive but Python string comparison is not)
            original_address = utxo.address
            address = original_address.lower()

            # Check if this is a fidelity bond
            if address in bond_address_to_info:
                locktime, index = bond_address_to_info[address]
                path = f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{index}:{locktime}"
                # Track that this address has had UTXOs
                self.addresses_with_history.add(address)
                utxo_info = _make_utxo_info(
                    txid=utxo.txid,
                    vout=utxo.vout,
                    value=utxo.value,
                    address=original_address,  # Preserve original case
                    confirmations=utxo.confirmations,
                    scriptpubkey=utxo.scriptpubkey,
                    path=path,
                    mixdepth=0,
                    height=utxo.height,
                    locktime=locktime,
                )
                fidelity_bond_utxos.append(utxo_info)
                logger.debug(
                    f"Recognized fidelity bond UTXO: {address[:20]}... "
                    f"value={utxo.value} locktime={locktime}"
                )
                continue

            # Try to find address in cache (should be pre-populated now)
            path_info = self.address_cache.get(address)
            if path_info is None:
                # Fallback to derivation scan (shouldn't happen often now)
                path_info = self._find_address_path(address)
            if path_info is None:
                # Check if this is a P2WSH address (likely a fidelity bond we don't know about)
                # P2WSH: OP_0 (0x00) + PUSH32 (0x20) + 32-byte hash = 68 hex chars
                if len(utxo.scriptpubkey) == 68 and utxo.scriptpubkey.startswith("0020"):
                    # Check if this P2WSH address is a known fidelity bond from the registry
                    # This handles external bonds that may have been imported but not matched above
                    cached_locktime = self.fidelity_bond_locktime_cache.get(address)
                    if cached_locktime is not None:
                        # This is a known fidelity bond from the registry
                        # Get index from address_cache (should have been set during import)
                        cached = self.address_cache.get(address)
                        index = cached[2] if cached else -1
                        path = (
                            f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{index}:{cached_locktime}"
                        )
                        self.addresses_with_history.add(address)
                        utxo_info = _make_utxo_info(
                            txid=utxo.txid,
                            vout=utxo.vout,
                            value=utxo.value,
                            address=original_address,  # Preserve original case
                            confirmations=utxo.confirmations,
                            scriptpubkey=utxo.scriptpubkey,
                            path=path,
                            mixdepth=0,
                            height=utxo.height,
                            locktime=cached_locktime,
                        )
                        fidelity_bond_utxos.append(utxo_info)
                        logger.debug(
                            f"Recognized P2WSH as fidelity bond from registry: "
                            f"{address[:20]}... locktime={cached_locktime}"
                        )
                        continue
                    # Unknown P2WSH - silently skip (fidelity bonds we don't know about)
                    logger.trace(f"Skipping unknown P2WSH address {address}")
                    continue
                logger.debug(f"Unknown address {address}, skipping")
                continue

            mixdepth, change, index = path_info

            # Check if this is a fidelity bond address (branch 2)
            # This handles cases where the address was added to address_cache but
            # the UTXO wasn't matched in bond_address_to_info (e.g., external bonds)
            if change == FIDELITY_BOND_BRANCH:
                # Get locktime from cache
                bond_locktime: int | None = None
                bond_locktime = self.fidelity_bond_locktime_cache.get(address)

                if bond_locktime is not None:
                    path = f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{index}:{bond_locktime}"
                    self.addresses_with_history.add(address)
                    utxo_info = _make_utxo_info(
                        txid=utxo.txid,
                        vout=utxo.vout,
                        value=utxo.value,
                        address=original_address,  # Preserve original case
                        confirmations=utxo.confirmations,
                        scriptpubkey=utxo.scriptpubkey,
                        path=path,
                        mixdepth=0,
                        height=utxo.height,
                        locktime=bond_locktime,
                    )
                    fidelity_bond_utxos.append(utxo_info)
                    logger.debug(
                        f"Recognized fidelity bond from cache: "
                        f"{address[:20]}... locktime={bond_locktime} index={index}"
                    )
                    continue
                else:
                    # Fidelity bond address without locktime - skip with warning
                    logger.warning(
                        f"Fidelity bond address {address[:20]}... found without locktime, skipping"
                    )
                    continue

            path = f"{self.root_path}/{mixdepth}'/{change}/{index}"

            # Track that this address has had UTXOs
            self.addresses_with_history.add(address)

            utxo_info = _make_utxo_info(
                txid=utxo.txid,
                vout=utxo.vout,
                value=utxo.value,
                address=original_address,  # Preserve original case
                confirmations=utxo.confirmations,
                scriptpubkey=utxo.scriptpubkey,
                path=path,
                mixdepth=mixdepth,
                height=utxo.height,
            )
            result[mixdepth].append(utxo_info)

        # Add fidelity bonds to mixdepth 0
        if fidelity_bond_utxos:
            result[0].extend(fidelity_bond_utxos)

        # Update cache
        self.utxo_cache = result

        # Fetch all addresses with transaction history (including spent)
        # This is important to track addresses that have been used but are now empty
        addresses_beyond_range: list[str] = []
        try:
            if hasattr(self.backend, "get_addresses_with_history"):
                history_addresses = await self.backend.get_addresses_with_history()
                for address in history_addresses:
                    # Check if this address belongs to our wallet
                    # Use _find_address_path which checks cache first, then derives if needed
                    path_info = self._find_address_path(address)
                    if path_info is not None:
                        self.addresses_with_history.add(address)
                    else:
                        # Address not found in current range - may be beyond descriptor range
                        addresses_beyond_range.append(address)
                logger.debug(f"Tracked {len(self.addresses_with_history)} addresses with history")
                if addresses_beyond_range:
                    logger.info(
                        f"Found {len(addresses_beyond_range)} address(es) from history "
                        f"not in current range [0, {current_range}], searching extended range..."
                    )
        except Exception as e:
            logger.debug(f"Could not fetch addresses with history: {e}")

        # Search for addresses beyond the current range
        # This handles wallets previously used with different software (e.g., reference impl)
        # that may have used addresses at indices beyond our current descriptor range
        if addresses_beyond_range:
            extended_addresses_found = 0
            for address in addresses_beyond_range:
                path_info = self._find_address_path_extended(address)
                if path_info is not None:
                    self.addresses_with_history.add(address)
                    extended_addresses_found += 1
            if extended_addresses_found > 0:
                logger.info(
                    f"Found {extended_addresses_found} address(es) in extended range search"
                )

        # Check if descriptor range needs to be upgraded
        # This handles wallets that have grown beyond the initial range
        try:
            upgraded = await self.check_and_upgrade_descriptor_range(gap_limit=100)
            if upgraded:
                # Re-populate address cache with the new range
                new_range = await self.backend.get_max_descriptor_range()
                await self._populate_address_cache(new_range)
        except Exception as e:
            logger.warning(f"Could not check/upgrade descriptor range: {e}")

        total_utxos = sum(len(u) for u in result.values())
        total_value = sum(sum(u.value for u in utxos) for utxos in result.values())
        logger.info(
            f"Descriptor wallet sync complete: {total_utxos} UTXOs, "
            f"{format_amount(total_value)} total"
        )

        self._apply_frozen_state()
        return result

    async def check_and_upgrade_descriptor_range(
        self,
        gap_limit: int = 100,
    ) -> bool:
        """
        Check if descriptor range needs upgrading and upgrade if necessary.

        This method detects if the wallet has used addresses beyond the current
        descriptor range and automatically upgrades the range if needed.

        The algorithm:
        1. Get the current descriptor range from Bitcoin Core
        2. Check addresses with history to find the highest used index
        3. If highest used index + gap_limit > current range, upgrade

        Args:
            gap_limit: Number of empty addresses to maintain beyond highest used

        Returns:
            True if upgrade was performed, False otherwise

        Raises:
            RuntimeError: If backend is not DescriptorWalletBackend
        """
        if not isinstance(self.backend, DescriptorWalletBackend):
            raise RuntimeError(
                "check_and_upgrade_descriptor_range() requires DescriptorWalletBackend"
            )

        # Get current range
        current_range = await self.backend.get_max_descriptor_range()
        logger.debug(f"Current descriptor range: [0, {current_range}]")

        # Find highest used index across all mixdepths/branches
        highest_used = await self._find_highest_used_index_from_history()

        # Calculate required range
        required_range = highest_used + gap_limit + 1

        if required_range <= current_range:
            logger.debug(
                f"Descriptor range sufficient: highest used={highest_used}, "
                f"current range={current_range}"
            )
            return False

        # Need to upgrade
        logger.info(
            f"Upgrading descriptor range: highest used={highest_used}, "
            f"current={current_range}, new={required_range}"
        )

        # Generate descriptors with new range
        descriptors = self._generate_import_descriptors(required_range)

        # Upgrade (no rescan needed - addresses already exist in blockchain)
        await self.backend.upgrade_descriptor_ranges(descriptors, required_range, rescan=False)

        # Update our cached range
        self._current_descriptor_range = required_range

        logger.info(f"Descriptor range upgraded to [0, {required_range}]")
        return True

    async def _find_highest_used_index_from_history(self) -> int:
        """
        Find the highest address index that has ever been used.

        Uses addresses_with_history which is populated from Bitcoin Core's
        transaction history.

        Returns:
            Highest used address index, or -1 if no addresses used
        """
        highest_index = -1

        # Check addresses from blockchain history
        for address in self.addresses_with_history:
            if address in self.address_cache:
                _, _, index = self.address_cache[address]
                if index > highest_index:
                    highest_index = index

        # Also check current UTXOs
        for mixdepth in range(self.mixdepth_count):
            utxos = self.utxo_cache.get(mixdepth, [])
            for utxo in utxos:
                if utxo.address in self.address_cache:
                    _, _, index = self.address_cache[utxo.address]
                    if index > highest_index:
                        highest_index = index

        return highest_index

    async def _populate_address_cache(self, max_index: int) -> None:
        """
        Pre-populate the address cache for efficient address lookups.

        This derives addresses for all mixdepths and branches up to max_index,
        storing them in the address_cache for O(1) lookups during sync.

        Args:
            max_index: Maximum address index to derive (typically the descriptor range)
        """
        import time

        # Only populate if we haven't already cached enough addresses
        current_cache_size = len(self.address_cache)
        expected_size = self.mixdepth_count * 2 * max_index  # mixdepths * branches * indices

        # If cache already has enough entries, skip
        if current_cache_size >= expected_size * 0.9:  # 90% threshold
            logger.debug(f"Address cache already populated ({current_cache_size} entries)")
            return

        total_addresses = expected_size
        logger.info(
            f"Populating address cache for range [0, {max_index}] "
            f"({total_addresses:,} addresses)..."
        )

        start_time = time.time()
        count = 0
        last_log_time = start_time

        for mixdepth in range(self.mixdepth_count):
            for change in [0, 1]:
                for index in range(max_index):
                    # get_address automatically caches
                    self.get_address(mixdepth, change, index)
                    count += 1

                    # Log progress every 5 seconds for large caches
                    current_time = time.time()
                    if current_time - last_log_time >= 5.0:
                        progress = count / total_addresses * 100
                        elapsed = current_time - start_time
                        rate = count / elapsed if elapsed > 0 else 0
                        remaining = (total_addresses - count) / rate if rate > 0 else 0
                        logger.info(
                            f"Address cache progress: {count:,}/{total_addresses:,} "
                            f"({progress:.1f}%) - ETA: {remaining:.0f}s"
                        )
                        last_log_time = current_time

        elapsed = time.time() - start_time
        logger.info(
            f"Address cache populated with {len(self.address_cache):,} entries in {elapsed:.1f}s"
        )

    # -- Address path resolution (Group F) ----------------------------------

    def _find_address_path(
        self, address: str, max_scan: int | None = None
    ) -> tuple[int, int, int] | None:
        """
        Find the derivation path for an address.

        First checks the cache, then checks the fidelity bond registry,
        then tries to derive and match.

        Args:
            address: Bitcoin address
            max_scan: Maximum index to scan per branch. If None, uses the current
                     descriptor range from _current_descriptor_range or DEFAULT_SCAN_RANGE.

        Returns:
            Tuple of (mixdepth, change, index) or None if not found
        """
        # Check cache first
        if address in self.address_cache:
            return self.address_cache[address]

        # Check fidelity bond registry if data_dir is available
        # Fidelity bond addresses use branch 2 and aren't in the normal cache
        if self.data_dir:
            try:
                from jmwallet.wallet.bond_registry import load_registry

                registry = load_registry(self.data_dir)
                bond = registry.get_bond_by_address(address)
                if bond is not None:
                    # Found in fidelity bond registry - cache it and return
                    path_info = (0, FIDELITY_BOND_BRANCH, bond.index)
                    self.address_cache[address] = path_info
                    # Also cache the locktime
                    self.fidelity_bond_locktime_cache[address] = bond.locktime
                    logger.debug(
                        f"Found address {address[:20]}... in fidelity bond registry "
                        f"(index={bond.index}, locktime={bond.locktime})"
                    )
                    return path_info
            except Exception as e:
                logger.trace(f"Could not check bond registry: {e}")

        # Determine scan range - use the current descriptor range if available
        if max_scan is None:
            max_scan = int(getattr(self, "_current_descriptor_range", DEFAULT_SCAN_RANGE))

        # Try to find by deriving addresses (expensive but necessary)
        # We must scan up to the descriptor range to find all addresses
        for mixdepth in range(self.mixdepth_count):
            for change in [0, 1]:
                for index in range(max_scan):
                    derived_addr = self.get_address(mixdepth, change, index)
                    if derived_addr == address:
                        return (mixdepth, change, index)

        return None

    def _find_address_path_extended(
        self, address: str, extend_by: int = 5000
    ) -> tuple[int, int, int] | None:
        """
        Find the derivation path for an address, searching beyond the current range.

        This is used for addresses from transaction history that might be at
        indices beyond the current descriptor range (e.g., from previous use
        with a different wallet software).

        Args:
            address: Bitcoin address
            extend_by: How far beyond the current range to search

        Returns:
            Tuple of (mixdepth, change, index) or None if not found
        """
        # Check cache first
        if address in self.address_cache:
            return self.address_cache[address]

        current_range = int(getattr(self, "_current_descriptor_range", DEFAULT_SCAN_RANGE))
        extended_max = current_range + extend_by

        # Search from current_range to extended_max (the normal range was already searched)
        for mixdepth in range(self.mixdepth_count):
            for change in [0, 1]:
                for index in range(current_range, extended_max):
                    derived_addr = self.get_address(mixdepth, change, index)
                    if derived_addr == address:
                        logger.info(
                            f"Found address at extended index {index} "
                            f"(beyond current range {current_range})"
                        )
                        return (mixdepth, change, index)

        return None

    def _parse_descriptor_path(
        self,
        desc: str,
        desc_to_path: dict[str, tuple[int, int]],
    ) -> tuple[int, int, int] | None:
        """
        Parse a descriptor to extract mixdepth, change, and index.

        When using xpub descriptors, Bitcoin Core returns a descriptor showing
        the path RELATIVE to the xpub we provided:
        wpkh([fingerprint/change/index]pubkey)#checksum

        We need to match this back to the original descriptor to determine mixdepth.

        Args:
            desc: Descriptor string from scantxoutset result
            desc_to_path: Mapping of descriptor (without checksum) to (mixdepth, change)

        Returns:
            Tuple of (mixdepth, change, index) or None if parsing fails
        """
        # Remove checksum
        if "#" in desc:
            desc_base = desc.split("#")[0]
        else:
            desc_base = desc

        # Extract the relative path [fingerprint/change/index] and pubkey
        # Pattern: wpkh([fingerprint/change/index]pubkey) or tr(...)
        match = re.search(r"(?:wpkh|tr)\(\[[\da-f]+/(\d+)/(\d+)\]([\da-f]+)\)", desc_base, re.I)
        if not match:
            return None

        change_from_desc = int(match.group(1))
        index = int(match.group(2))
        pubkey = match.group(3)

        # Find which descriptor this matches by checking all our descriptors
        # We need to derive the key and check if it matches the pubkey
        for base_desc, (mixdepth, change) in desc_to_path.items():
            if change == change_from_desc:
                # Verify by deriving the key and comparing pubkeys
                try:
                    derived_key = self.master_key.derive(
                        f"{self.root_path}/{mixdepth}'/{change}/{index}"
                    )
                    derived_pubkey = derived_key.get_public_key_bytes(compressed=True).hex()
                    if derived_pubkey == pubkey:
                        return (mixdepth, change, index)
                except Exception:
                    continue

        return None
