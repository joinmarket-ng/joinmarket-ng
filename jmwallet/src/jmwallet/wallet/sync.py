"""
Wallet synchronization mixins.

Contains all sync-related methods: address-by-address scanning, descriptor-based
sync, descriptor wallet setup, and address path resolution.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jmcore.bitcoin import btc_to_sats, format_amount, get_hrp
from loguru import logger

from jmwallet.backends.base import BlockchainBackend
from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend
from jmwallet.wallet.bip32 import HDKey
from jmwallet.wallet.constants import (
    DEFAULT_SCAN_RANGE,
    FIDELITY_BOND_BRANCH,
    MAX_DESCRIPTOR_RANGE,
)
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


def _bond_utxo_signature(bond: Any) -> tuple[Any, ...]:
    """Return a comparable snapshot of a bond's UTXO state (announced + extras).

    Used to decide whether reconciling a registered bond during sync actually
    changed anything, so a steady-state sync does not rewrite the registry
    file. Ignores confirmations (they drift every block and are not
    identity-relevant for this purpose).
    """
    return (
        bond.txid,
        bond.vout,
        bond.value,
        tuple(sorted((u.txid, u.vout, u.value) for u in bond.extra_utxos)),
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
    scan_range: int
    data_dir: Path | None
    wallet_fingerprint: str
    address_cache: dict[str, tuple[int, int, int]]
    utxo_cache: dict[int, list[UTXOInfo]]
    addresses_with_history: set[str]
    metadata_store: Any  # UTXOMetadataStore | None (deferred import)
    fidelity_bond_locktime_cache: dict[str, int]
    max_sats_freeze_reuse: int
    # Lazily-built canonical bond address cache; see
    # ``_canonical_bond_address_map`` below for what populates it.
    _canonical_bond_addresses: dict[str, tuple[int, int]] | None
    # Guards the once-per-process import-label reconstruction pass.
    _imported_labels_scanned: bool
    # Guards the once-per-process import-history reconstruction pass.
    _imported_history_scanned: bool
    # Tracks a deferred/capped imported backfill within this process.
    _imported_history_started: bool
    # Config toggle ([wallet] reconstruct_history): when False, no on-chain
    # history rows are ever reconstructed automatically.
    reconstruct_history_enabled: bool
    # Forced-address-reuse defense state (see WalletService.__init__): addresses
    # and outpoints this process has positively observed funded across syncs.
    _observed_funded_addresses: set[str]
    _observed_outpoints: set[str]

    # Methods provided by the host class
    def get_address(self, mixdepth: int, change: int, index: int) -> str:
        raise NotImplementedError

    def get_account_xpub(self, mixdepth: int) -> str:
        raise NotImplementedError

    def get_fidelity_bond_address(self, index: int, locktime: int) -> str:
        raise NotImplementedError

    def get_fidelity_bond_key(self, index: int, locktime: int) -> HDKey:
        raise NotImplementedError

    def get_fidelity_bond_script(self, index: int, locktime: int) -> bytes:
        raise NotImplementedError

    def _apply_frozen_state(self) -> None:
        raise NotImplementedError

    def _auto_freeze_reused_address_utxos(
        self,
        observed_funded_addresses: set[str],
        observed_outpoints: set[str],
        prior_funded_addresses: set[str],
    ) -> int:
        raise NotImplementedError

    # -- Persistent address-history tracking --------------------------------

    def _snapshot_funded_addresses(self) -> set[str]:
        """Snapshot the addresses that already held a UTXO at the start of a sync.

        Captured before ``utxo_cache`` is rebuilt. Used by the
        forced-address-reuse auto-freeze to require that a reuse address was
        *empty* before the new arrival: per
        https://en.bitcoin.it/wiki/Privacy#Forced_address_reuse, a forced
        payment to an already-spent (empty) used address should be frozen,
        whereas coins arriving on an address that still holds funds should be
        fully spent together (so we do not freeze those).
        """
        return {utxo.address for utxos in self.utxo_cache.values() for utxo in utxos}

    def _freeze_reused_after_sync(self, prior_funded_addresses: set[str]) -> None:
        """Run the forced-reuse auto-freeze, then record what this sync observed.

        ``self._observed_funded_addresses`` / ``self._observed_outpoints`` carry
        only what *prior* syncs observed at this point (they are updated below,
        after the freeze decision), so a coin discovered for the first time on
        this sync is never mistaken for forced reuse (issue #542).
        """
        # On the first sync after a restart ``utxo_cache`` had no pre-sync
        # snapshot. A persisted seen outpoint that is still present proves its
        # address never became empty, so a second payment must remain spendable
        # with the original coin.
        effective_prior_funded = prior_funded_addresses | {
            utxo.address
            for utxos in self.utxo_cache.values()
            for utxo in utxos
            if utxo.outpoint in self._observed_outpoints
        }
        self._auto_freeze_reused_address_utxos(
            self._observed_funded_addresses,
            self._observed_outpoints,
            effective_prior_funded,
        )
        # Accumulate this sync's observations so the next sync can tell a
        # genuine forced-reuse arrival (new outpoint on a previously funded,
        # now-empty address) from a first-use or late-discovered coin.
        newly_funded: set[str] = set()
        newly_seen: set[str] = set()
        for utxos in self.utxo_cache.values():
            for utxo in utxos:
                if utxo.is_fidelity_bond:
                    continue
                self._observed_outpoints.add(utxo.outpoint)
                self._observed_funded_addresses.add(utxo.address)
                newly_seen.add(utxo.outpoint)
                newly_funded.add(utxo.address)
        # Persist the observations so the defense survives restarts (issue
        # #559): an address emptied before a restart and refunded after it is
        # still recognized as reuse. The store dedupes and writes only when
        # something changed.
        store = getattr(self, "metadata_store", None)
        if store is not None:
            try:
                store.record_reuse_observations(newly_funded, newly_seen)
            except Exception as exc:  # pragma: no cover - disk failures are rare
                logger.warning(f"Failed to persist reuse-freeze observations: {exc}")

    def _record_history_address(self, address: str, origin: str | None = None) -> None:
        """Mark ``address`` as having on-chain history (current or spent).

        Updates both the in-memory ``addresses_with_history`` set and the
        persistent BIP-329 metadata store (when configured). This is the
        single entry point used by every sync path; calling ``set.add()``
        directly would skip persistence and reintroduce the deposit-address
        reuse bug after the funded UTXO is spent.
        """
        if not address:
            return
        already_in_memory = address in self.addresses_with_history
        self.addresses_with_history.add(address)
        store = getattr(self, "metadata_store", None)
        if store is None or already_in_memory:
            return
        try:
            store.mark_address_used(address, origin)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not persist used address {address[:12]}...: {exc}")

    def _record_history_addresses(
        self, addresses: Iterable[str], origin: str | None = None
    ) -> None:
        """Batched variant of :meth:`_record_history_address` for hot loops."""
        new_addresses: list[str] = []
        for address in addresses:
            if address and address not in self.addresses_with_history:
                self.addresses_with_history.add(address)
                new_addresses.append(address)
        if not new_addresses:
            return
        store = getattr(self, "metadata_store", None)
        if store is None:
            return
        try:
            store.mark_addresses_used(new_addresses, origin)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not persist {len(new_addresses)} used addresses: {exc}")

    # -- Imported-wallet CoinJoin label reconstruction ----------------------

    @staticmethod
    def _is_external_path(path: str) -> bool:
        """Return True when a derivation path is on the external (receive) branch.

        Paths look like ``m/84'/<coin>'/<md>'/<change>/<index>``; the ``change``
        element (``0`` external, ``1`` internal) is second from the end.
        """
        parts = path.split("/")
        return len(parts) >= 2 and parts[-2] == "0"

    async def reconstruct_imported_state_safe(self) -> None:
        """Best-effort wrapper: history/label reconstruction must never break a sync."""
        try:
            await self.reconstruct_imported_history()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"Import history reconstruction skipped: {exc}")
        try:
            await self.reconstruct_imported_labels()
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug(f"Import label reconstruction skipped: {exc}")

    async def reconstruct_imported_history(
        self,
        *,
        force: bool = False,
        max_transactions: int = 1000,
    ) -> int:
        """Reconstruct CoinJoin/send/deposit history rows from chain data.

        A wallet recovered from seed has no ``history.csv`` rows, so past
        CoinJoins (role, fees, peers), sends, and deposits are invisible.
        This pass enumerates the wallet's confirmed on-chain transactions,
        classifies each with the same equal-output heuristic the legacy
        client uses, and persists best-effort rows tagged ``source="onchain"``
        (see :mod:`jmwallet.history_reconstruction`).

        The automatic pass (run after every bond-aware sync) is conservative:
        it starts only when the wallet has no history, but can continue when
        earlier capped passes already wrote on-chain rows. It runs at most once
        per process after completing the backlog and never touches existing
        rows. The ``jm-wallet reconstruct-history`` CLI uses ``force=True`` to
        re-run after purging previous on-chain rows.

        Args:
            force: Re-run even if the wallet already has history rows or a
                pass already completed in this process.
            max_transactions: Safety cap on transactions classified per pass.

        Returns:
            The number of history rows created.
        """
        if self.data_dir is None:
            return 0
        if not force:
            if not getattr(self, "reconstruct_history_enabled", True):
                return 0
            if getattr(self, "_imported_history_scanned", False):
                return 0
        if not getattr(self.backend, "supports_tx_enumeration", False):
            self._imported_history_scanned = True
            return 0

        from jmwallet.history import read_history
        from jmwallet.history_reconstruction import reconstruct_history_from_chain

        existing = read_history(self.data_dir, wallet_fingerprint=self.wallet_fingerprint)
        if not existing:
            self._imported_history_started = True
        elif (
            not force
            and not self._imported_history_started
            and not any(entry.source == "onchain" for entry in existing)
        ):
            # A wallet whose history is entirely protocol-recorded is not an
            # imported-history backfill. Once an empty wallet has entered the
            # workflow, however, protocol activity must not cancel a pass that
            # was deferred while Core completed its background rescan.
            self._imported_history_scanned = True
            return 0

        # Bitcoin Core's default recovery path starts with a recent smart scan
        # and continues with a full rescan in the background. Reconstructing
        # while that rescan is active would persist a partial history and make
        # the result look complete. Leave the process guard unset so the next
        # sync retries after Core finishes.
        if not force and isinstance(self.backend, DescriptorWalletBackend):
            status = await self.backend.get_rescan_status()
            if not isinstance(status, dict):
                logger.info(
                    "Deferring imported history reconstruction because Bitcoin Core "
                    "rescan status is unavailable"
                )
                return 0
            if status.get("in_progress"):
                logger.info(
                    "Deferring imported history reconstruction until the active "
                    "Bitcoin Core rescan completes"
                )
                return 0

        result = await reconstruct_history_from_chain(
            self.backend,
            address_paths=self.address_cache,
            network=self.network,
            wallet_fingerprint=self.wallet_fingerprint,
            data_dir=self.data_dir,
            max_transactions=max_transactions,
        )
        # A capped pass has more backlog. Keep the guard unset so a later sync
        # in this process continues after the already-persisted txids.
        self._imported_history_scanned = not result.capped
        return result.created

    async def reconstruct_imported_labels(
        self,
        *,
        force: bool = False,
        max_transactions: int = 1000,
    ) -> int:
        """Reconstruct CoinJoin labels for an imported wallet from chain data.

        JoinMarket-NG derives ``cj-out`` / ``cj-change`` address statuses from a
        local CoinJoin history file written by this wallet's own maker/taker
        activity. A wallet recovered from seed (or otherwise imported) has no
        such file, so every funded coin falls back to ``deposit`` (external) or
        ``non-cj-change`` (internal), even when it actually came from a CoinJoin.

        This scans the current UTXO set, fetches the transaction that created
        each unclassified funded coin, applies the same equal-output CoinJoin
        heuristic the legacy client uses (:func:`jmcore.bitcoin.analyze_coinjoin_outputs`),
        and persists the derived origin (``cj_out`` / ``cj_change`` / ``deposit``
        / ``non_cj_change``) into the BIP-329 metadata store. The wallet display
        then surfaces the correct status via
        :meth:`UTXOMetadataStore.get_coinjoin_address_types`.

        The pass is best-effort and bounded: it runs at most once per process
        (unless ``force`` is set), skips addresses the local history already
        classifies and addresses already classified on a previous run, dedupes
        work by transaction, and degrades silently to the existing fallback when
        the backend cannot return a transaction. Only the pre-existing imported
        backlog needs this: coins received while running are either this
        wallet's own CoinJoins (recorded in history) or genuine deposits (already
        labeled correctly).

        Args:
            force: Re-run even if a pass already completed in this process.
            max_transactions: Safety cap on transactions fetched in one pass.

        Returns:
            The number of coins newly classified.
        """
        store = getattr(self, "metadata_store", None)
        if store is None or self.data_dir is None:
            # Without persistence we would re-fetch every transaction on each
            # display; only run when results can be cached.
            return 0
        if getattr(self, "_imported_labels_scanned", False) and not force:
            return 0

        from jmcore.bitcoin import analyze_coinjoin_outputs, parse_transaction

        from jmwallet.history import (
            CLASSIFIED_ORIGINS,
            ORIGIN_CJ_CHANGE,
            ORIGIN_CJ_OUT,
            classify_imported_output,
            get_address_history_types,
        )

        # Addresses this wallet's own CoinJoin history already classifies are
        # authoritative; never override them with a heuristic guess (issue #517).
        authoritative = set(
            get_address_history_types(self.data_dir, wallet_fingerprint=self.wallet_fingerprint)
        )

        # Group unclassified funded coins by the transaction that created them.
        by_txid: dict[str, list[UTXOInfo]] = {}
        for utxos in self.utxo_cache.values():
            for utxo in utxos:
                if utxo.is_fidelity_bond:
                    continue
                if utxo.address in authoritative:
                    continue
                if store.get_address_origins(utxo.address) & CLASSIFIED_ORIGINS:
                    continue
                by_txid.setdefault(utxo.txid, []).append(utxo)

        if not by_txid:
            self._imported_labels_scanned = True
            return 0

        origin_to_addresses: dict[str, list[str]] = {}
        classified = 0
        fetched = 0
        for txid, utxos in by_txid.items():
            if fetched >= max_transactions:
                logger.warning(
                    f"Import label reconstruction hit the {max_transactions}-transaction "
                    "cap; remaining coins will be classified on a later run."
                )
                break
            try:
                tx = await self.backend.get_transaction(txid)
            except Exception as exc:  # pragma: no cover - backend/network dependent
                logger.debug(f"Could not fetch tx {txid[:16]}... for labels: {exc}")
                continue
            fetched += 1
            if tx is None or not tx.raw:
                continue
            try:
                analysis = analyze_coinjoin_outputs(parse_transaction(tx.raw).outputs)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"Could not analyze tx {txid[:16]}... for labels: {exc}")
                continue
            for utxo in utxos:
                origin = classify_imported_output(
                    analysis, utxo.value, self._is_external_path(utxo.path)
                )
                origin_to_addresses.setdefault(origin, []).append(utxo.address)
                # Surface the label on the in-memory UTXO for /utxos and
                # interactive coin selection, without clobbering a user label.
                if utxo.label is None:
                    if origin == ORIGIN_CJ_OUT:
                        utxo.label = "cj-out"
                    elif origin == ORIGIN_CJ_CHANGE:
                        utxo.label = "cj-change"
                classified += 1

        for origin, addresses in origin_to_addresses.items():
            try:
                store.mark_addresses_used(addresses, origin)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"Could not persist {origin} origins: {exc}")

        self._imported_labels_scanned = True
        if classified:
            logger.info(
                f"Reconstructed CoinJoin labels for {classified} imported coin(s) "
                f"from {fetched} transaction(s)."
            )
        return classified

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
                        self._record_history_address(address)
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
        # Apply frozen metadata so per-mixdepth (cold-cache) syncs report the
        # same spendable set as the full-sync paths. Without this, callers
        # like ``get_balance`` count frozen UTXOs as spendable until the next
        # full sync runs, which made the tumbler plan sweeps of mixdepths
        # whose funds were entirely frozen.
        self._apply_frozen_state()
        return utxos

    async def sync_fidelity_bonds(
        self,
        locktimes: list[int],
        *,
        bond_addresses: list[tuple[str, int, int]] | None = None,
    ) -> list[UTXOInfo]:
        """
        Sync fidelity bond UTXOs with specific locktimes.

        Fidelity bonds use mixdepth 0, branch 2, with path format:
        m/84'/coin'/0'/2/timenumber:locktime

        Each locktime maps to exactly one timenumber (BIP32 child index).

        Args:
            locktimes: List of Unix timestamps to scan for when explicit bond
                addresses are not supplied.
            bond_addresses: Exact ``(address, locktime, index)`` entries to scan.
                This is required for externally registered bonds whose address
                cannot be derived from this wallet's seed.

        Returns:
            List of fidelity bond UTXOs found
        """
        from jmcore.timenumber import timestamp_to_timenumber

        utxos: list[UTXOInfo] = []

        if not locktimes and not bond_addresses:
            logger.debug("No locktimes provided for fidelity bond sync")
            return utxos

        # Each locktime has exactly one address (timenumber = BIP32 child index)
        addresses: list[str] = []
        address_to_info: dict[str, tuple[int, int]] = {}  # addr -> (locktime, timenumber)

        if bond_addresses is not None:
            for address, locktime, index in bond_addresses:
                addresses.append(address)
                address_to_info[address.lower()] = (locktime, index)
        else:
            for locktime in locktimes:
                timenumber = timestamp_to_timenumber(locktime)
                address = self.get_fidelity_bond_address(timenumber, locktime)
                addresses.append(address)
                address_to_info[address.lower()] = (locktime, timenumber)

        # Ensure these bond addresses are scanned over the wallet's full
        # history before querying. Light-client backends (Neutrino) only rescan
        # new blocks for already-watched addresses; a bond address registered
        # after the initial sync would otherwise miss its (already-confirmed)
        # funding output. No-op for full-node/descriptor backends.
        await self.backend.ensure_addresses_scanned(addresses)

        # Fetch UTXOs for all addresses at once
        backend_utxos = await self.backend.get_utxos(addresses)

        # Group by address
        utxos_by_address: dict[str, list] = {addr.lower(): [] for addr in addresses}
        for utxo in backend_utxos:
            address_lower = utxo.address.lower()
            if address_lower in utxos_by_address:
                utxos_by_address[address_lower].append(utxo)

        # Process results
        for address in addresses:
            address_lower = address.lower()
            addr_utxos = utxos_by_address[address_lower]
            if addr_utxos:
                locktime, timenumber = address_to_info[address_lower]
                self._record_history_address(address)
                for utxo in addr_utxos:
                    path = f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{timenumber}:{locktime}"
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

        # Add fidelity bond UTXOs to mixdepth 0 cache
        if utxos:
            if 0 not in self.utxo_cache:
                self.utxo_cache[0] = []
            existing_outpoints = {(u.txid, u.vout) for u in self.utxo_cache[0]}
            for utxo_info in utxos:
                outpoint = (utxo_info.txid, utxo_info.vout)
                if outpoint not in existing_outpoints:
                    self.utxo_cache[0].append(utxo_info)
                    existing_outpoints.add(outpoint)
            logger.info(f"Found {len(utxos)} fidelity bond UTXOs")

        self._self_register_bond_utxos(
            utxos,
            scanned_addresses={address.lower() for address in addresses},
        )

        return utxos

    async def discover_fidelity_bonds(
        self,
        progress_callback: Any | None = None,
        rescan_progress_callback: Any | None = None,
    ) -> list[UTXOInfo]:
        """
        Discover fidelity bonds by scanning all 960 possible locktimes.

        This is used during wallet recovery when the user doesn't know which
        locktimes they used. It generates addresses for all valid timenumbers
        (0-959, representing Jan 2020 through Dec 2099) and scans for UTXOs.

        For descriptor_wallet backend, this method will import addresses into
        the wallet as it scans in batches, then clean up addresses that had no UTXOs.

        Each timenumber maps to exactly one BIP32 child index and one locktime,
        matching the reference JoinMarket implementation.

        Args:
            progress_callback: Optional callback(current, total) for progress updates
            rescan_progress_callback: Optional callback(progress) with 0.0-1.0 for rescan

        Returns:
            List of discovered fidelity bond UTXOs
        """
        from jmcore.timenumber import TIMENUMBER_COUNT, timenumber_to_timestamp

        logger.info(f"Starting fidelity bond discovery scan ({TIMENUMBER_COUNT} timelocks)")

        discovered_utxos: list[UTXOInfo] = []
        batch_size = 100  # Process timenumbers in batches
        descriptor_backend: DescriptorWalletBackend | None = (
            self.backend if isinstance(self.backend, DescriptorWalletBackend) else None
        )

        # Build the full address map across all timenumbers.
        # Each timenumber has exactly one address (timenumber = BIP32 child index).
        all_address_to_locktime: dict[str, tuple[int, int]] = {}
        for timenumber in range(TIMENUMBER_COUNT):
            locktime = timenumber_to_timestamp(timenumber)
            address = self.get_fidelity_bond_address(timenumber, locktime)
            all_address_to_locktime[address] = (locktime, timenumber)

        # For descriptor wallets, import all addresses in batches WITHOUT triggering
        # a per-batch rescan.  A single blockchain rescan is run after all descriptors
        # are imported so Bitcoin Core never rejects a batch with RPC -4
        # "Wallet is currently rescanning".
        if descriptor_backend is not None:
            # ``discover_fidelity_bonds`` can be called directly on a fresh
            # ``WalletService`` (without a prior ``sync_all``). Ensure the
            # descriptor wallet is initialised before importing address
            # descriptors, otherwise backend calls fail with RPC -18 / "wallet
            # not loaded".
            expected_count = self.mixdepth_count * 2
            if not await descriptor_backend.is_wallet_setup(
                expected_descriptor_count=expected_count
            ):
                logger.info(
                    "Descriptor wallet not initialised; running setup before bond discovery"
                )
                await self.setup_descriptor_wallet(rescan=False)

            all_bond_addrs = [
                (addr, lt, idx) for addr, (lt, idx) in all_address_to_locktime.items()
            ]
            total_addrs = len(all_bond_addrs)
            for batch_start in range(0, total_addrs, batch_size):
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
                    progress_callback(batch_end, total_addrs)

            # Single rescan after all descriptors are registered. The backend
            # floors the start height at the wallet creation height when one is
            # configured, so this does not always scan from genesis.
            logger.info(
                "All fidelity bond addresses imported, starting blockchain rescan "
                "(from the wallet creation height when configured, otherwise genesis). "
                "This may take a long time on mainnet (1-2+ hours with HDD)..."
            )
            await descriptor_backend.start_background_rescan(0)
            await descriptor_backend.wait_for_rescan_complete(
                poll_interval=5.0,
                progress_callback=rescan_progress_callback,
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
            total_addrs = len(all_addresses_list)
            for batch_start in range(0, total_addrs, batch_size):
                batch_addrs = all_addresses_list[batch_start : batch_start + batch_size]
                batch_end = batch_start + len(batch_addrs)
                try:
                    batch_utxos = await self.backend.get_utxos(batch_addrs)
                    backend_utxos.extend(batch_utxos)
                except Exception as e:
                    logger.error(f"Failed to scan batch {batch_start}-{batch_end}: {e}")

                if progress_callback:
                    progress_callback(batch_end, total_addrs)

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

        # Snapshot the addresses already funded *before* this sync rebuilds the
        # cache, for the forced-address-reuse auto-freeze (issue #529): it
        # freezes only a *newly observed* UTXO whose address this process saw
        # funded and then emptied, leaving the original deposit, coins present
        # at startup, and coins discovered late (issue #542) spendable.
        prior_funded_addresses = self._snapshot_funded_addresses()

        # Lazy-init: ensure descriptor wallet is loaded and seeded with our
        # descriptors before scanning. Production paths call
        # ``setup_descriptor_wallet`` explicitly (jmwalletd.wallet_ops); this
        # guard makes ``WalletService(...).sync()`` work directly in tests and
        # ad-hoc usage without each caller having to remember the setup step.
        if isinstance(self.backend, DescriptorWalletBackend):
            expected_count = self.mixdepth_count * 2
            if fidelity_bond_addresses:
                expected_count += len(fidelity_bond_addresses)
            needs_setup = not await self.backend.is_wallet_setup(
                expected_descriptor_count=expected_count
            )
            if not needs_setup:
                expected_bases: set[str] = set()
                for mixdepth in range(self.mixdepth_count):
                    xpub = self.get_account_xpub(mixdepth)
                    expected_bases.add(f"wpkh({xpub}/0/*)")
                    expected_bases.add(f"wpkh({xpub}/1/*)")
                descriptors = await self.backend.list_descriptors()
                actual_bases = {str(item.get("desc", "")).split("#", 1)[0] for item in descriptors}
                if not expected_bases.issubset(actual_bases):
                    logger.info(
                        "Descriptor wallet loaded but does not contain this wallet's descriptors; "
                        "running setup before sync"
                    )
                    needs_setup = True
            if needs_setup:
                logger.info("Descriptor wallet not initialised; running setup before sync")
                # Rescan only when fidelity bonds are supplied: a bond's
                # timelock address may already be funded (the bond was created
                # and paid before this sync), and importing its ``addr()``
                # descriptor without a rescan tracks it only from "now", hiding
                # the already-confirmed UTXO. A brand-new wallet with no bonds
                # has no prior history, so ``rescan=False`` keeps setup fast.
                await self.setup_descriptor_wallet(
                    fidelity_bond_addresses=fidelity_bond_addresses,
                    rescan=bool(fidelity_bond_addresses),
                    check_existing=False,
                )

        # Try efficient descriptor-based sync if backend supports it
        if self.backend.supports_descriptor_scan:
            result = await self._sync_all_with_descriptors(fidelity_bond_addresses)
            if result is not None:
                self._freeze_reused_after_sync(prior_funded_addresses)
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

        # Scan fidelity bond addresses too. The legacy address-by-address path
        # (used by light-client backends such as Neutrino) only walks the
        # regular mixdepth branches, so without this the supplied bond
        # addresses on the timelock branch (.../2/...) are never queried and
        # funded bonds stay invisible. ``sync_fidelity_bonds`` derives the same
        # addresses from the locktimes and appends any found UTXOs to
        # ``utxo_cache[0]``. ``sync_mixdepth`` returns the very list it stores in
        # ``utxo_cache``, so ``result[0]`` is that same list and is updated in
        # place -- no manual mirroring needed.
        if fidelity_bond_addresses:
            expected_hrp = get_hrp(self.network)
            valid_bonds = [
                (address, locktime, index)
                for address, locktime, index in fidelity_bond_addresses
                if (address.split("1")[0].lower() if "1" in address else "") == expected_hrp
            ]
            if valid_bonds:
                await self.sync_fidelity_bonds(
                    sorted({locktime for _, locktime, _ in valid_bonds}),
                    bond_addresses=valid_bonds,
                )

        logger.info(f"Sync complete: {sum(len(u) for u in result.values())} total UTXOs")
        self._freeze_reused_after_sync(prior_funded_addresses)
        self._apply_frozen_state()
        return result

    def load_registered_bond_addresses(self) -> list[tuple[str, int, int]]:
        """Load this wallet's fidelity bond addresses from the per-wallet registry.

        Reads ``fidelity_bonds_<fingerprint>.json`` (scoped to this wallet) and
        returns the ``(address, locktime, index)`` tuples for bonds matching the
        wallet's network, ready to pass into :meth:`sync_all`,
        :meth:`sync_with_descriptor_wallet`, or :meth:`setup_descriptor_wallet`.

        Returns an empty list when ``data_dir`` is unset (the registry is
        file-backed) or no matching bonds are recorded. The legacy shared
        ``fidelity_bonds.json`` fallback is disabled so foreign bonds are never
        scanned under this wallet (issue #492).
        """
        if self.data_dir is None:
            return []

        from jmwallet.wallet.bond_registry import load_registry

        registry = load_registry(
            self.data_dir,
            self.wallet_fingerprint,
            allow_legacy_fallback=False,
        )
        return [
            (bond.address, bond.locktime, bond.index)
            for bond in registry.bonds
            if bond.network == self.network
        ]

    async def sync_with_registered_bonds(self) -> dict[int, list[UTXOInfo]]:
        """Sync the wallet including any fidelity bonds from the registry.

        This is the bond-aware counterpart to :meth:`sync`. It loads the
        wallet's registered fidelity bond addresses (see
        :meth:`load_registered_bond_addresses`), ensures the base descriptor
        wallet and the bonds' watch-only descriptors are imported for
        descriptor-wallet backends, and then syncs so the bond UTXOs are
        scanned into ``utxo_cache`` alongside the regular mixdepth UTXOs.

        Callers that surface wallet state to users (the jmwalletd daemon's
        ``/utxos`` and ``/display`` endpoints, and the ``jm-wallet`` CLI
        commands ``info``/``send``/``freeze``/``sync-bonds``) must use this
        instead of :meth:`sync`; otherwise funded fidelity bonds are invisible
        because the bond branch (``.../2/...``) is not part of the standard
        descriptor import (matching legacy joinmarket-clientserver behavior,
        where bonds appear in the UTXO list).

        The base descriptor wallet is set up on first use (idempotently, so it
        is a no-op when the daemon has already called
        :meth:`setup_descriptor_wallet`). Backends that are not descriptor
        wallets (e.g. neutrino) fall back to :meth:`sync_all`, which already
        scans the supplied bond addresses.
        """
        bond_addresses = self.load_registered_bond_addresses()

        if not isinstance(self.backend, DescriptorWalletBackend):
            # Non-descriptor backends scan bond addresses directly in sync_all.
            result = await self.sync_all(bond_addresses or None)
            await self.reconstruct_imported_state_safe()
            return result

        # Ensure the base descriptor wallet exists before scanning. This is a
        # no-op when it is already set up (``setup_descriptor_wallet`` checks
        # first), so it is safe even though the daemon also sets it up
        # explicitly. It is required for the CLI paths, which (unlike the
        # daemon) rely on this method to perform first-time setup.
        base_ready = await self.is_descriptor_wallet_ready(fidelity_bond_count=0)
        if not base_ready:
            # First-time setup imports the base descriptors and any registered
            # bonds together (with a rescan), so a wallet restored with bonds
            # already funded is fully populated in one pass.
            await self.setup_descriptor_wallet(
                rescan=True,
                fidelity_bond_addresses=bond_addresses or None,
            )
        elif bond_addresses:
            # Base wallet already set up: import only the bond descriptors that
            # are not already present. We inspect the actual descriptor set
            # rather than rely on a descriptor *count* check, because the base
            # wallet may import more descriptors than ``mixdepth_count * 2``
            # (Bitcoin Core records internal/external variants), so a
            # count-based "ready" test would report the bonds as present and
            # silently skip importing them (leaving funded bonds invisible).
            imported = await self._imported_bond_addresses()
            missing = [b for b in bond_addresses if b[0].lower() not in imported]
            if missing:
                await self.import_fidelity_bond_addresses(missing, rescan=True)

        result = await self.sync_with_descriptor_wallet(bond_addresses or None)
        await self.reconstruct_imported_state_safe()
        return result

    async def _imported_bond_addresses(self) -> set[str]:
        """Return the set of fidelity bond addresses (lowercased) already imported.

        Parses the descriptor wallet's ``addr(<address>)`` descriptors so callers
        can tell which registered bonds still need importing. Returns an empty set
        for non-descriptor backends or when listing fails.
        """
        if not isinstance(self.backend, DescriptorWalletBackend):
            return set()
        try:
            descriptors = await self.backend.list_descriptors()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(f"Could not list descriptors to check bond imports: {exc}")
            return set()

        imported: set[str] = set()
        for item in descriptors:
            desc = str(item.get("desc", ""))
            base = desc.split("#", 1)[0]
            if base.startswith("addr(") and base.endswith(")"):
                imported.add(base[5:-1].lower())
        return imported

    def _canonical_bond_address_map(self) -> dict[str, tuple[int, int]]:
        """Return every canonical fidelity-bond address mapped to (locktime, timenumber).

        Every fidelity bond address is deterministically derivable from the
        wallet's seed (``m/84'/coin'/0'/2/<timenumber>``, one address per
        timenumber, 960 total covering Jan 2020 - Dec 2099). This lets
        :meth:`sync_with_descriptor_wallet` recognize a bond UTXO that
        Bitcoin Core already tracks even when the local bond registry has no
        matching entry -- e.g. a previous ``recover-bonds`` run (or a
        pre-#492 jmwallet version) imported the ``addr()`` descriptor, but
        the registry entry was never written, was lost, or sits unclaimed in
        the legacy shared file because the per-wallet migration's pubkey
        check did not match it. Without this fallback such a UTXO is
        silently dropped from the wallet's balance (it hits the "unknown
        P2WSH" branch below) even though it demonstrably belongs to this
        wallet.

        Computed once per process and cached: it is pure key derivation with
        no backend calls, but 960 BIP32 derivations still cost roughly a
        second, so it is only paid the first time an unrecognized P2WSH UTXO
        is actually seen (see call site).
        """
        if self._canonical_bond_addresses is None:
            from jmcore.timenumber import TIMENUMBER_COUNT, timenumber_to_timestamp

            from jmwallet.wallet.address import script_to_p2wsh_address

            mapping: dict[str, tuple[int, int]] = {}
            for timenumber in range(TIMENUMBER_COUNT):
                locktime = timenumber_to_timestamp(timenumber)
                # Derive the address WITHOUT ``get_fidelity_bond_address``,
                # which caches every address it produces into
                # ``address_cache`` and ``fidelity_bond_locktime_cache``.
                # Building the full 960-entry map through it would seed both
                # caches with every timenumber, so the wallet-info display
                # (which iterates ``fidelity_bond_locktime_cache``) would list
                # all 960 addresses as bonds, and later bond UTXOs would be
                # recognized via the cache path instead of the canonical path
                # (so self-registration would miss them). Deriving the script
                # directly has no caching side effect.
                script = self.get_fidelity_bond_script(timenumber, locktime)
                address = script_to_p2wsh_address(script, self.network).lower()
                mapping[address] = (locktime, timenumber)
            self._canonical_bond_addresses = mapping
        return self._canonical_bond_addresses

    def _resolve_bond_locktime(self, address_lower: str) -> tuple[int, int] | None:
        """Resolve ``(locktime, index)`` for one of this wallet's bond addresses.

        Checks the in-memory ``fidelity_bond_locktime_cache`` first (populated
        by registry-aware syncs and bond address generation), then falls back
        to the canonical timenumber-derived address map (see
        :meth:`_canonical_bond_address_map`). A canonical match is cached so
        subsequent lookups and displays recognize the address directly.

        Returns ``None`` when the address is not a fidelity bond address of
        this wallet. ``address_lower`` must already be lowercased (bech32 is
        case-insensitive, the caches are keyed lowercase).
        """
        locktime = self.fidelity_bond_locktime_cache.get(address_lower)
        if locktime is not None:
            cached = self.address_cache.get(address_lower)
            if cached is not None:
                return locktime, cached[2]
        canonical = self._canonical_bond_address_map().get(address_lower)
        if canonical is not None:
            canonical_locktime, timenumber = canonical
            self.address_cache[address_lower] = (0, FIDELITY_BOND_BRANCH, timenumber)
            self.fidelity_bond_locktime_cache[address_lower] = canonical_locktime
            return canonical_locktime, timenumber
        if locktime is not None:
            # Known locktime but no cached index (and not canonically
            # derivable, e.g. an imported external bond). The index is only
            # cosmetic (used in the display path string); signing needs the
            # locktime.
            return locktime, -1
        return None

    def _self_register_bond_utxos(
        self,
        bond_utxos: list[UTXOInfo],
        *,
        scanned_addresses: set[str] | None = None,
    ) -> None:
        """Persist and refresh recognized fidelity bond UTXOs in the registry.

        ``bond_utxos`` are all fidelity bond UTXOs recognized during a sync
        (whether matched from the registry, the cache, or by canonical
        derivation). The registry stores a single UTXO per bond, so when an
        address holds more than one UTXO we must pick one deterministically;
        we pick the largest, matching the reference implementation,
        ``recover-bonds`` and ``sync-bonds`` (an address is meant to be funded
        once and only the biggest UTXO counts as the bond). Choosing by value
        rather than by scan order is what makes the recorded value stable
        regardless of the order ``listunspent`` returns the UTXOs. Then:

        * adds a new registry entry when the address is canonically derivable
          from this wallet's seed but not yet recorded -- so a bond Bitcoin
          Core already tracks (e.g. from a past ``recover-bonds`` run or a
          lost/unmigrated registry entry) is self-healed without the user
          re-running ``recover-bonds`` / ``import-bond``; and

        * refreshes the stored UTXO info (txid/vout/value/confirmations) of an
          already-registered bond when it no longer matches the chosen UTXO.
          A registered bond was previously matched via the direct-match path
          and never reconciled, so whichever UTXO happened to be recorded
          first (by scan order, or by an older build) stayed frozen in the
          registry: ``jm-wallet list-bonds`` kept showing that stale UTXO even
          when a different/larger one was present on-chain. Reconciling here
          mirrors what the dedicated ``jm-wallet sync-bonds`` command does, so
          a plain ``info`` sync no longer leaves the registry out of date.

        The registry file is only rewritten when something actually changed,
        so steady-state syncs do not churn it. Best-effort and non-fatal: a
        failure here only means the same reconciliation is retried on the next
        sync, never that a recognized UTXO disappears from the current result.
        """
        if self.data_dir is None or (not bond_utxos and not scanned_addresses):
            return
        try:
            from jmwallet.wallet.bond_registry import (
                BondUtxo,
                create_bond_info,
                load_registry,
                save_registry,
            )

            registry = load_registry(
                self.data_dir, self.wallet_fingerprint, allow_legacy_fallback=False
            )

            # Group every recognized UTXO by bond address. The registry keeps
            # the largest as the announced bond and the rest as ``extra_utxos``
            # (locked, but not part of the bond); ``set_bond_utxos`` does that
            # split. Grouping all UTXOs (not just the largest) is what lets a
            # second UTXO on the same address be surfaced by ``list-bonds``.
            utxos_by_address: dict[str, list[BondUtxo]] = {}
            for utxo in bond_utxos:
                utxos_by_address.setdefault(utxo.address, []).append(
                    BondUtxo(
                        txid=utxo.txid,
                        vout=utxo.vout,
                        value=utxo.value,
                        confirmations=utxo.confirmations,
                    )
                )

            registered = 0
            refreshed = 0
            for address, addr_utxos in utxos_by_address.items():
                addr_lower = address.lower()
                existing = registry.get_bond_by_address(address)
                if existing is not None:
                    # Refresh the stored UTXO set (announced + extras) to what
                    # this sync sees; no canonical derivation needed since the
                    # bond is already recorded. Covers external/cold bonds too,
                    # exactly like ``sync-bonds``. Only counts as a change when
                    # the resulting UTXO state actually differs, so steady-state
                    # syncs do not rewrite the registry file.
                    before = _bond_utxo_signature(existing)
                    registry.set_bond_utxos(address, addr_utxos)
                    if _bond_utxo_signature(existing) != before:
                        refreshed += 1
                    continue
                canonical = self._canonical_bond_address_map().get(addr_lower)
                if canonical is None:
                    # Not a canonically-derivable bond (e.g. an external/cold
                    # bond not yet registered); those are managed via their own
                    # registry flow (cold-wallet commands / recover-bonds).
                    continue
                locktime, timenumber = canonical
                key = self.get_fidelity_bond_key(timenumber, locktime)
                pubkey_hex = key.get_public_key_bytes(compressed=True).hex()
                witness_script = self.get_fidelity_bond_script(timenumber, locktime)
                path = f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{timenumber}"
                bond_info = create_bond_info(
                    address=address,
                    locktime=locktime,
                    index=timenumber,
                    path=path,
                    pubkey_hex=pubkey_hex,
                    witness_script=witness_script,
                    network=self.network,
                )
                registry.add_bond(bond_info)
                registry.set_bond_utxos(address, addr_utxos)
                registered += 1

            # A complete bond-aware scan also proves which registered
            # addresses are now empty. Clear stale funding metadata after a
            # redemption instead of leaving offline list-bonds views funded.
            found_addresses = {address.lower() for address in utxos_by_address}
            for existing in registry.bonds:
                address_lower = existing.address.lower()
                if (
                    scanned_addresses is None
                    or address_lower not in scanned_addresses
                    or address_lower in found_addresses
                ):
                    continue
                before = _bond_utxo_signature(existing)
                registry.set_bond_utxos(existing.address, [])
                if _bond_utxo_signature(existing) != before:
                    refreshed += 1

            if registered or refreshed:
                save_registry(registry, self.data_dir, self.wallet_fingerprint)
                logger.info(
                    f"Reconciled fidelity bond registry during sync: {registered} added "
                    f"(recognized via canonical derivation), {refreshed} refreshed to the "
                    "current UTXO set"
                )
        except Exception as exc:
            logger.warning(f"Could not reconcile recognized fidelity bond(s): {exc}")

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
        # Generate descriptors for all mixdepths and build a lookup table.
        # ``scan_range`` is the explicit descriptor lookahead set on the
        # service (default 1000). The old ``max(1000, gap_limit * 10)``
        # formula was dropped in favor of an explicit setting (issue #475).
        scan_range = self.scan_range
        descriptors: list[str | dict[str, Any]] = []
        # Map descriptor string (without checksum) -> (mixdepth, change)
        desc_to_path: dict[str, tuple[int, int]] = {}
        # Map fidelity bond address -> (locktime, index)
        bond_address_to_info: dict[str, tuple[int, int]] = {}

        for mixdepth in range(self.mixdepth_count):
            xpub = self.get_account_xpub(mixdepth)

            # External (receive) addresses: .../0/*
            desc_ext = f"wpkh({xpub}/0/*)"
            descriptors.append({"desc": desc_ext, "range": [0, scan_range - 1]})
            desc_to_path[desc_ext] = (mixdepth, 0)

            # Internal (change) addresses: .../1/*
            desc_int = f"wpkh({xpub}/1/*)"
            descriptors.append({"desc": desc_int, "range": [0, scan_range - 1]})
            desc_to_path[desc_int] = (mixdepth, 1)

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
                # Keyed lowercase: bech32 is case-insensitive but Python
                # string comparison is not.
                addr_lower = address.lower()
                bond_address_to_info[addr_lower] = (locktime, index)
                # Cache the address with the correct index from registry
                self.address_cache[addr_lower] = (0, FIDELITY_BOND_BRANCH, index)
                self.fidelity_bond_locktime_cache[addr_lower] = locktime

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
                # Resolve the bond's locktime: from the supplied bond
                # addresses when given, otherwise from the locktime cache or
                # the canonical timenumber derivation. The fallback matters
                # because plain ``sync()`` / ``sync_all()`` calls (e.g. the
                # daemon's direct-send refresh) pass no bond addresses, yet
                # the descriptor wallet still returns the imported bond
                # UTXOs; without the locktime the bond would be treated as a
                # regular spendable UTXO and then fail to sign (P2WSH needs
                # its witness script, which embeds the locktime).
                bond_info = bond_address_to_info.get(
                    bond_address.lower()
                ) or self._resolve_bond_locktime(bond_address.lower())
                if bond_info is not None:
                    # This is a fidelity bond UTXO
                    locktime, index = bond_info
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
            source_address = str(utxo_data.get("address", ""))
            if path_info is None and source_address:
                source_address_lower = source_address.lower()
                path_info = self.address_cache.get(source_address_lower) or self._find_address_path(
                    source_address_lower
                )

            if path_info is None:
                logger.warning(f"Could not parse path from descriptor: {desc}")
                continue

            mixdepth, change, index = path_info

            # Calculate confirmations
            confirmations = 0
            utxo_height = utxo_data.get("height", 0)
            if utxo_height > 0:
                confirmations = tip_height - utxo_height + 1

            # An address-cache hit can resolve to the fidelity bond branch
            # (e.g. a bond descriptor without the ``addr(...)`` form). Such a
            # UTXO must carry its locktime; emitting it as a regular UTXO
            # would let coin selection auto-spend it and then fail to sign
            # the P2WSH input.
            if change == FIDELITY_BOND_BRANCH and source_address:
                bond_info = self._resolve_bond_locktime(source_address.lower())
                if bond_info is None:
                    logger.warning(
                        f"Fidelity bond address {source_address[:20]}... found without "
                        "locktime, skipping"
                    )
                    continue
                locktime, bond_index = bond_info
                path = f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/{bond_index}:{locktime}"
                self._record_history_address(source_address)
                fidelity_bond_utxos.append(
                    _make_utxo_info(
                        txid=utxo_data["txid"],
                        vout=utxo_data["vout"],
                        value=btc_to_sats(utxo_data["amount"]),
                        address=source_address,
                        confirmations=confirmations,
                        scriptpubkey=utxo_data.get("scriptPubKey", ""),
                        path=path,
                        mixdepth=0,
                        height=utxo_height if utxo_height > 0 else None,
                        locktime=locktime,
                    )
                )
                continue

            # Generate the address and cache it
            address = (
                source_address if source_address else self.get_address(mixdepth, change, index)
            )

            # Track that this address has had UTXOs
            self._record_history_address(address)

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

        if fidelity_bond_addresses:
            self._self_register_bond_utxos(
                fidelity_bond_utxos,
                scanned_addresses=set(bond_address_to_info),
            )

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
        scan_range: int | None = None,
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
            scan_range: Address index range to import. When ``None`` (default),
                resolves to ``self.scan_range`` (configured via
                ``[wallet].scan_range``, default 1000). Distinct from
                ``gap_limit`` which is the BIP44 trailing-empty threshold.
                The legacy ``max(DEFAULT_SCAN_RANGE, gap_limit * 10)`` formula
                was removed (issue #475).
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

        if scan_range is None:
            scan_range = self.scan_range

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
        if scan_range > MAX_DESCRIPTOR_RANGE:
            logger.warning(
                f"Requested scan_range {scan_range} exceeds Bitcoin Core's "
                f"per-descriptor range limit of {MAX_DESCRIPTOR_RANGE}; "
                f"clamping to {MAX_DESCRIPTOR_RANGE}. Bitcoin Core would "
                "otherwise reject importdescriptors with 'Range is too large'. "
                "See docs/technical/wallet-scanning.md."
            )
            scan_range = MAX_DESCRIPTOR_RANGE

        descriptors = []

        for mixdepth in range(self.mixdepth_count):
            xpub = self.get_account_xpub(mixdepth)

            # External (receive) addresses: .../0/*
            descriptors.append(
                {
                    "desc": f"wpkh({xpub}/0/*)",
                    "range": [0, scan_range - 1],
                    "internal": False,
                }
            )

            # Internal (change) addresses: .../1/*
            descriptors.append(
                {
                    "desc": f"wpkh({xpub}/1/*)",
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

        # Snapshot the addresses already funded before this sync rebuilds the
        # cache, for the forced-address-reuse auto-freeze (issues #529, #542).
        prior_funded_addresses = self._snapshot_funded_addresses()

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
        # Bond addresses recognized below via canonical timenumber derivation
        # (see ``_canonical_bond_address_map``). Tracked only to log the
        # recognition once per address rather than once per UTXO (a bond
        # address can hold several UTXOs). Registry reconciliation itself is
        # driven off ``fidelity_bond_utxos`` after the loop.
        canonically_recognized_bond_addresses: set[str] = set()
        unknown_p2wsh_count = 0

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
                self._record_history_address(address)
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
                        self._record_history_address(address)
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
                    # Not in the registry/cache either. Every fidelity bond
                    # address is deterministically derivable from the seed
                    # (m/84'/coin'/0'/2/<timenumber>), so check the canonical
                    # map before giving up: Bitcoin Core can already be
                    # tracking this UTXO (e.g. a past ``recover-bonds`` run,
                    # or a bond registry entry that was lost or never
                    # migrated to the per-wallet file) even though we have no
                    # local record of it. See ``_canonical_bond_address_map``.
                    canonical = self._canonical_bond_address_map().get(address)
                    if canonical is not None:
                        canonical_locktime, canonical_index = canonical
                        self.address_cache[address] = (
                            0,
                            FIDELITY_BOND_BRANCH,
                            canonical_index,
                        )
                        self.fidelity_bond_locktime_cache[address] = canonical_locktime
                        path = (
                            f"{self.root_path}/0'/{FIDELITY_BOND_BRANCH}/"
                            f"{canonical_index}:{canonical_locktime}"
                        )
                        self._record_history_address(address)
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
                            locktime=canonical_locktime,
                        )
                        fidelity_bond_utxos.append(utxo_info)
                        if address not in canonically_recognized_bond_addresses:
                            canonically_recognized_bond_addresses.add(address)
                            logger.warning(
                                f"Recognized fidelity bond address {address[:20]}... "
                                f"(locktime={canonical_locktime}) via canonical derivation; "
                                "it was not in the bond registry and will be self-registered"
                            )
                        continue
                    # Genuinely unknown P2WSH (not one of our fidelity bonds).
                    unknown_p2wsh_count += 1
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
                    self._record_history_address(address)
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
            self._record_history_address(address)

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

        if unknown_p2wsh_count:
            logger.debug(
                f"Skipped {unknown_p2wsh_count} unknown P2WSH UTXO(s): scriptPubKey "
                "matches neither a registered fidelity bond nor a canonical "
                "timenumber-derived bond address for this wallet"
            )

        # Add fidelity bonds to mixdepth 0
        if fidelity_bond_utxos:
            result[0].extend(fidelity_bond_utxos)

        # Reconcile the bond registry with what this sync recognized: add
        # canonically-derived bonds that Core tracks but the registry lacks,
        # and refresh the stored UTXO info of already-registered bonds so a
        # bond that was recorded from an arbitrary earlier UTXO (by scan order)
        # is not left showing a stale value in list-bonds. Feed every
        # recognized bond UTXO (registry-matched, cache-matched, and canonical)
        # so an address with several UTXOs is reconciled to a single
        # deterministic one. ``_self_register_bond_utxos`` writes only when
        # something changed.
        if fidelity_bond_addresses:
            self._self_register_bond_utxos(
                fidelity_bond_utxos,
                scanned_addresses={address.lower() for address, _, _ in fidelity_bond_addresses},
            )
        elif fidelity_bond_utxos:
            self._self_register_bond_utxos(fidelity_bond_utxos)

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
                        self._record_history_address(address)
                    else:
                        # Address not found in current range - may be beyond descriptor range
                        addresses_beyond_range.append(address)
                logger.debug(f"Tracked {len(self.addresses_with_history)} addresses with history")
                if addresses_beyond_range:
                    logger.debug(
                        f"Found {len(addresses_beyond_range)} address(es) from history "
                        f"not in current range [0, {current_range}]; will filter and "
                        f"search extended range if any are ours"
                    )
        except Exception as e:
            # Address-history enumeration failure is a privacy-critical
            # event: if we silently continue, the descriptor-range upgrade
            # path and the deposit-address picker will operate on a
            # partial view and may propose a previously funded address as
            # a fresh deposit. Log loudly. The persisted BIP-329 store
            # still holds whatever was learned previously (we never
            # downgrade it), so subsequent ``info``/``send`` runs that
            # don't trip the same RPC failure will recover.
            logger.error(
                f"Could not fetch addresses with history: {e}. "
                f"Proposed deposit addresses will be checked against "
                f"the persisted used-address store, but the in-memory "
                f"enumeration is incomplete for this run."
            )

        # Resolve addresses beyond the current descriptor range.
        #
        # The Bitcoin Core wallet holds two kinds of descriptors for us:
        # ranged ``wpkh(xpub/0/*)`` / ``wpkh(xpub/1/*)`` descriptors per
        # mixdepth, and standalone ``addr(<bech32>)`` descriptors for our
        # fidelity bond addresses. Any of those addresses can show up in
        # ``listreceivedbyaddress`` (used by get_addresses_with_history) once
        # they have transaction history. That RPC is ismine-only by
        # construction, so external counterparties from CoinJoin co-spends do
        # not appear; defensive checks below keep the sync robust if a future
        # backend ever leaks a non-ours address through.
        #
        # Naively running _find_address_path_extended on each missing address
        # is a ~50,000-derivation BIP32 scan per address and, for anything not
        # actually reachable via our wpkh derivation (fidelity bonds, external
        # counterparties), runs to completion. That easily blocks MakerBot
        # startup past test timeouts before the bot connects to directories.
        #
        # Instead, ask Bitcoin Core via getaddressinfo:
        #   - ismine=False  -> external (e.g. counterparty); skip.
        #   - desc is wpkh  -> parse the embedded (change, index) and verify
        #                      the pubkey derives from this wallet's master
        #                      key. Match -> exact path in O(mixdepths).
        #   - desc is addr() or other non-wpkh -> our fidelity bonds and any
        #                      other non-ranged imports live here. They have
        #                      no BIP32 path to recover; skip the extended
        #                      scan rather than spending tens of seconds on
        #                      it. UTXOs at fidelity bond addresses are still
        #                      resolved via the bond_address_to_info path
        #                      above when the caller passes the registry.
        #   - desc missing  -> skip. ismine descriptor wallets always emit a
        #                      desc; absence means it isn't one of our ranged
        #                      wpkh derivations and the BIP32 fallback would
        #                      not find it anyway. Avoids a multi-second stall
        #                      on MakerBot startup. The legacy BIP32 fallback
        #                      below only runs when the backend lacks
        #                      getaddressinfo entirely (older Core / test
        #                      mocks).
        backend_has_get_address_info = getattr(self.backend, "get_address_info", None) is not None
        if addresses_beyond_range and backend_has_get_address_info:
            get_address_info = self.backend.get_address_info  # type: ignore[attr-defined]
            # Prefer the JSON-RPC batch path when the backend exposes it
            # (DescriptorWalletBackend does). Batching collapses N HTTP
            # round-trips into ceil(N/chunk) and is ~20x faster on localhost
            # and dramatically more on remote / Tor-fronted Core endpoints.
            # Falls back to a sequential loop for backends/test mocks that
            # don't implement ``batch_get_address_info``.
            batch_lookup = getattr(self.backend, "batch_get_address_info", None)
            addresses_list = list(addresses_beyond_range)
            if batch_lookup is not None:
                try:
                    infos: list[dict | None] = await batch_lookup(addresses_list)
                except Exception as e:
                    logger.debug(f"batch_get_address_info failed, falling back to serial: {e}")
                    infos = []
                    for address in addresses_list:
                        try:
                            infos.append(await get_address_info(address))
                        except Exception as inner:
                            logger.trace(f"getaddressinfo failed for {address[:20]}...: {inner}")
                            infos.append(None)
            else:
                infos = []
                for address in addresses_list:
                    try:
                        infos.append(await get_address_info(address))
                    except Exception as e:
                        logger.trace(f"getaddressinfo failed for {address[:20]}...: {e}")
                        infos.append(None)

            resolved = 0
            skipped_external = 0
            skipped_non_wpkh = 0
            skipped_no_desc = 0
            for address, info in zip(addresses_list, infos):
                if info is None:
                    # RPC failed entirely; we can't tell if this is ours.
                    # Skip rather than spend tens of seconds on a BIP32 scan
                    # that would almost always come up empty for addresses
                    # we couldn't even getaddressinfo on.
                    skipped_no_desc += 1
                    continue
                if not info.get("ismine"):
                    skipped_external += 1
                    continue
                desc = info.get("desc", "")
                if not desc:
                    # ismine=True but no descriptor returned. For descriptor
                    # wallets Core always returns a desc for ismine addresses;
                    # absence means this isn't one of our ranged wpkh
                    # derivations (or Core is too old to report it). Skip the
                    # multi-second BIP32 fallback either way: if it WERE one
                    # of ours the desc would have been present.
                    skipped_no_desc += 1
                    continue
                path_info = self._resolve_descriptor_path(desc)
                if path_info is None:
                    # Descriptor doesn't decode into one of our wpkh
                    # derivations: typically an addr() import for a
                    # fidelity bond, or some other non-ranged descriptor.
                    # Nothing more to do here.
                    skipped_non_wpkh += 1
                    continue
                self.address_cache[address] = path_info
                self._record_history_address(address)
                resolved += 1
            if skipped_external:
                logger.debug(
                    f"Skipped {skipped_external} external address(es) beyond range "
                    f"(not ismine - e.g., CoinJoin counterparties)"
                )
            if skipped_non_wpkh:
                logger.debug(
                    f"Skipped {skipped_non_wpkh} ismine address(es) with non-wpkh "
                    f"descriptor (e.g., addr() imports for fidelity bonds)"
                )
            if skipped_no_desc:
                logger.debug(
                    f"Skipped {skipped_no_desc} address(es) beyond range with no "
                    f"resolvable descriptor (would not be reachable via BIP32 scan)"
                )
            if resolved:
                logger.debug(f"Resolved {resolved} address(es) beyond range via getaddressinfo")
        elif addresses_beyond_range:
            # Fallback BIP32 derivation scan. Only reached when the backend
            # doesn't expose get_address_info at all (older Core / test mocks).
            # We deliberately do NOT fall back here when get_address_info
            # exists but returned None/empty desc: that scan is O(mixdepths *
            # 2 * 5000) derivations per address and can stall MakerBot startup
            # past test timeouts; if the address were one of our wpkh
            # derivations, Core would have returned its descriptor.
            extended_addresses_found = 0
            for address in addresses_beyond_range:
                path_info = self._find_address_path_extended(address)
                if path_info is not None:
                    self._record_history_address(address)
                    extended_addresses_found += 1
            if extended_addresses_found > 0:
                logger.info(
                    f"Found {extended_addresses_found} address(es) in extended range search"
                )

        # Check if descriptor range needs to be upgraded. This keeps the
        # descriptor lookahead window ahead of the highest used address as
        # the wallet grows, using the configured BIP44 ``gap_limit`` as the
        # trailing buffer (see docs/technical/wallet-scanning.md).
        try:
            upgraded = await self.check_and_upgrade_descriptor_range(gap_limit=self.gap_limit)
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

        self._freeze_reused_after_sync(prior_funded_addresses)
        self._apply_frozen_state()
        return result

    async def check_and_upgrade_descriptor_range(
        self,
        gap_limit: int = 20,
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
            gap_limit: BIP44 trailing-empty buffer to keep beyond the highest
                used address (defaults to the wallet's configured gap_limit).

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

        # Bitcoin Core rejects descriptor ranges spanning more than
        # MAX_DESCRIPTOR_RANGE indices ("Range is too large"). If a wallet has
        # used addresses beyond that, we can only track up to the limit; clamp
        # so the upgrade succeeds rather than failing wholesale.
        if required_range > MAX_DESCRIPTOR_RANGE:
            logger.warning(
                f"Required descriptor range {required_range} (highest used "
                f"{highest_used} + gap_limit {gap_limit}) exceeds Bitcoin Core's "
                f"limit of {MAX_DESCRIPTOR_RANGE}; clamping to "
                f"{MAX_DESCRIPTOR_RANGE}. Addresses beyond index "
                f"{MAX_DESCRIPTOR_RANGE - 1} cannot be tracked. See "
                "docs/technical/wallet-scanning.md."
            )
            required_range = MAX_DESCRIPTOR_RANGE

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

                registry = load_registry(self.data_dir, self.wallet_fingerprint)
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

    def _resolve_descriptor_path(self, desc: str) -> tuple[int, int, int] | None:
        """
        Parse a ``wpkh`` descriptor and resolve ``(mixdepth, change, index)``.

        Used to translate Bitcoin Core's ``getaddressinfo`` descriptor for an
        address into a wallet path without scanning derivations. Verifies the
        descriptor's pubkey against this wallet's master key. Returns ``None``
        if the descriptor is not a ranged ``wpkh`` (e.g., an ``addr(...)``
        import for a fidelity bond) or if no mixdepth derives the same pubkey
        — in either case the address has no BIP32 path here for us to record.
        """
        if "#" in desc:
            desc = desc.split("#")[0]
        match = re.search(r"wpkh\(\[[\da-f]+/(\d+)/(\d+)\]([\da-f]+)\)", desc, re.I)
        if not match:
            return None
        change_from_desc = int(match.group(1))
        index = int(match.group(2))
        pubkey = match.group(3).lower()
        for mixdepth in range(self.mixdepth_count):
            try:
                derived_key = self.master_key.derive(
                    f"{self.root_path}/{mixdepth}'/{change_from_desc}/{index}"
                )
            except Exception:
                continue
            derived_pubkey = derived_key.get_public_key_bytes(compressed=True).hex().lower()
            if derived_pubkey == pubkey:
                return (mixdepth, change_from_desc, index)
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
        # Pattern: wpkh([fingerprint/change/index]pubkey)
        match = re.search(r"wpkh\(\[[\da-f]+/(\d+)/(\d+)\]([\da-f]+)\)", desc_base, re.I)
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
