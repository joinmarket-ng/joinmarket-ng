"""
Background tasks for the maker bot.

Contains periodic tasks for wallet rescanning, rate limit monitoring,
directory reconnection, and pending transaction monitoring.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from jmcore.crypto import NickIdentity
from jmcore.directory_client import DirectoryClient, DirectoryClientError
from jmcore.models import Offer
from jmcore.notifications import get_notifier
from jmcore.tasks import spawn_task
from jmwallet.backends.base import BlockchainBackend
from loguru import logger

from maker.config import MakerConfig
from maker.protocols import MakerBotProtocol
from maker.rate_limiting import DirectConnectionRateLimiter, OrderbookRateLimiter

if TYPE_CHECKING:
    from jmwallet.history import TransactionHistoryEntry


class BackgroundTasksMixin:
    """Mixin class providing background task methods for MakerBot.

    These methods run as long-lived asyncio tasks and handle periodic
    maintenance operations like wallet rescanning, rate limit monitoring,
    directory server reconnection, and pending transaction tracking.
    """

    # -- Attributes provided by MakerBot --
    running: bool
    config: MakerConfig
    backend: BlockchainBackend
    nick: str
    nick_identity: NickIdentity
    directory_clients: dict[str, DirectoryClient]
    current_offers: list[Offer]
    listen_tasks: list[asyncio.Task[None]]
    _orderbook_rate_limiter: OrderbookRateLimiter
    _direct_connection_rate_limiter: DirectConnectionRateLimiter
    _directory_reconnect_attempts: dict[str, int]
    _all_directories_disconnected: bool
    _mempool_notified_txids: set[str]

    def _prune_done_tasks(self) -> None:
        """Remove completed tasks from listen_tasks to prevent unbounded growth."""
        self.listen_tasks = [t for t in self.listen_tasks if not t.done()]

    async def _periodic_rescan(self: MakerBotProtocol) -> None:
        """Background task to periodically rescan wallet and update offers.

        This runs every `rescan_interval_sec` (default: 10 minutes) to:
        1. Detect external transactions (deposits, Sparrow spends, etc.)
        2. Update pending transaction confirmations
        3. Update offers if balance changed

        This allows the maker to run in the background and adapt to balance
        changes without manual intervention.
        """
        logger.info(
            f"Starting periodic rescan task (interval: {self.config.rescan_interval_sec}s)..."
        )

        while self.running:
            try:
                await asyncio.sleep(self.config.rescan_interval_sec)

                if not self.running:
                    break

                logger.debug("Periodic wallet rescan starting...")
                await self._resync_wallet_and_update_offers()

            except asyncio.CancelledError:
                logger.info("Periodic rescan task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in periodic rescan: {e}")

        logger.info("Periodic rescan task stopped")

    async def _periodic_rate_limit_status(self) -> None:
        """Background task to periodically log rate limiting statistics.

        This runs every hour to provide visibility into spam/abuse without
        flooding logs. Shows:
        - Total violations across all peers
        - Currently banned peers
        - Top violators (by violation count)
        """
        # First log after 10 minutes (give time for initial activity)
        await asyncio.sleep(600)

        while self.running:
            try:
                stats = self._orderbook_rate_limiter.get_statistics()

                # Only log if there's activity worth reporting
                if stats["total_violations"] > 0 or stats["banned_peers"]:
                    banned_count = len(stats["banned_peers"])
                    banned_list = ", ".join(stats["banned_peers"][:5])
                    if banned_count > 5:
                        banned_list += f", ... and {banned_count - 5} more"

                    top_violators_str = ", ".join(
                        f"{nick}({count})" for nick, count in stats["top_violators"][:5]
                    )

                    logger.info(
                        f"Rate limit status: {stats['total_violations']} total violations, "
                        f"{banned_count} banned peer(s)"
                        + (f" [{banned_list}]" if banned_count > 0 else "")
                        + (
                            f", top violators: {top_violators_str}"
                            if stats["top_violators"]
                            else ""
                        )
                    )

                # Also log direct connection rate limiter stats if any activity
                direct_stats = self._direct_connection_rate_limiter.get_statistics()
                if direct_stats["total_violations"] > 0 or direct_stats["banned_connections"]:
                    banned_count = len(direct_stats["banned_connections"])
                    banned_list = ", ".join(direct_stats["banned_connections"][:5])
                    if banned_count > 5:
                        banned_list += f", ... and {banned_count - 5} more"

                    top_violators_str = ", ".join(
                        f"{conn}({count})" for conn, count in direct_stats["top_violators"][:5]
                    )

                    logger.info(
                        f"Direct connection rate limit: {direct_stats['total_violations']} "
                        f"violations, {banned_count} banned connection(s)"
                        + (f" [{banned_list}]" if banned_count > 0 else "")
                        + (f", top: {top_violators_str}" if direct_stats["top_violators"] else "")
                    )

                # Cleanup old entries to prevent memory growth
                self._orderbook_rate_limiter.cleanup_old_entries()
                self._direct_connection_rate_limiter.cleanup_old_entries()

                # Log again in 1 hour
                await asyncio.sleep(3600)

            except asyncio.CancelledError:
                logger.info("Rate limit status task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in rate limit status task: {e}")
                await asyncio.sleep(3600)

        logger.info("Rate limit status task stopped")

    async def _periodic_directory_connection_status(self: MakerBotProtocol) -> None:
        """Background task to periodically log directory connection status.

        This runs every 10 minutes to provide visibility into orderbook
        connectivity. Shows:
        - Total directory servers configured
        - Currently connected servers
        - Disconnected servers (if any)
        """
        # First log after 5 minutes (give time for initial connection)
        await asyncio.sleep(300)

        while self.running:
            try:
                total_servers = len(self.config.directory_servers)
                connected_servers = list(self.directory_clients.keys())
                connected_count = len(connected_servers)
                disconnected_servers = [
                    server for server, _node_id in self._directory_pool.list_disconnected()
                ]

                if disconnected_servers:
                    disconnected_str = ", ".join(disconnected_servers[:5])
                    if len(disconnected_servers) > 5:
                        disconnected_str += f", ... and {len(disconnected_servers) - 5} more"
                    logger.warning(
                        f"Directory connection status: {connected_count}/{total_servers} "
                        f"connected. Disconnected: [{disconnected_str}]"
                    )
                else:
                    # Healthy state: keep at DEBUG so long-running makers do not
                    # spam INFO every 10 minutes. Use --log-level DEBUG to see it.
                    logger.debug(
                        f"Directory connection status: {connected_count}/{total_servers} connected "
                        f"[{', '.join(connected_servers)}]"
                    )

                # Log again in 10 minutes
                await asyncio.sleep(600)

            except asyncio.CancelledError:
                logger.info("Directory connection status task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in directory connection status task: {e}")
                await asyncio.sleep(600)

        logger.info("Directory connection status task stopped")

    async def _connect_to_directory(
        self: MakerBotProtocol, dir_server: str
    ) -> tuple[str, DirectoryClient] | None:
        """
        Connect to a single directory server.

        Thin wrapper over :meth:`MakerDirectoryPool.connect_to_directory`
        retained for backwards compatibility with tests that mock or call
        this method directly. The returned client is *not* yet registered
        in :attr:`directory_clients`; callers are expected to install it
        themselves (matches the legacy contract).

        Args:
            dir_server: Server address in format "host:port" or "host"

        Returns:
            Tuple of (node_id, client) if successful, None on failure
        """
        return await self._directory_pool.connect_to_directory(dir_server)

    async def _connect_to_directories_with_retry(self: MakerBotProtocol) -> None:
        """
        Connect to all configured directory servers with startup retry logic.

        Tor may still be bootstrapping circuits when the maker starts.
        This method retries failed connections with exponential back-off
        until at least one directory is reachable, bounded by
        ``directory_startup_timeout`` seconds (default 120 s). Successful
        connections are registered in :attr:`directory_clients` (the pool
        shares storage with that dict). Listener tasks are started later
        by :meth:`MakerBot.start` once offer announcement has completed.
        """
        await self._directory_pool.connect_all_with_retry(
            timeout=self.config.directory_startup_timeout,
            initial_delay=5.0,
            max_delay=30.0,
            backoff=1.5,
        )

    async def _periodic_directory_reconnect(self: MakerBotProtocol) -> None:
        """
        Background task to periodically reconnect to failed directory servers.

        Attempts to reconnect to disconnected directories at configured intervals.
        On successful reconnection:
        - Starts a listener task for the directory
        - Announces current offers to the newly connected directory
        """
        # Wait for initial connections to settle
        await asyncio.sleep(60)

        logger.info(
            f"Directory reconnection task started "
            f"(interval: {self.config.directory_reconnect_interval}s)"
        )

        while self.running:
            try:
                await asyncio.sleep(self.config.directory_reconnect_interval)

                # Find disconnected directories
                disconnected_servers = self._directory_pool.list_disconnected()

                if not disconnected_servers:
                    continue

                logger.info(
                    f"Attempting to reconnect to {len(disconnected_servers)} "
                    f"disconnected director{'y' if len(disconnected_servers) == 1 else 'ies'}..."
                )

                for dir_server, node_id in disconnected_servers:
                    # Check retry limit
                    max_retries = self.config.directory_reconnect_max_retries
                    attempts = self._directory_reconnect_attempts.get(node_id, 0)

                    if max_retries > 0 and attempts >= max_retries:
                        logger.debug(f"Skipping {node_id}: max retries ({max_retries}) reached")
                        continue

                    # Attempt reconnection
                    result = await self._connect_to_directory(dir_server)

                    if result:
                        new_node_id, client = result
                        self.directory_clients[new_node_id] = client

                        # Reset retry counter on success
                        self._directory_reconnect_attempts.pop(node_id, None)

                        logger.info(f"Reconnected to directory: {dir_server}")

                        # Announce offers to newly connected directory.
                        # Public broadcasts MUST NOT include the fidelity bond
                        # proof. Takers receive the bond via privmsg in
                        # response to !orderbook, matching the reference
                        # protocol (see jmdaemon/message_channel.py in
                        # joinmarket-clientserver, which asserts
                        # `fidelity_bond_proof_msg is None` for public pit
                        # announcements).
                        for offer in self.current_offers:
                            try:
                                offer_msg = self._format_offer_announcement(
                                    offer, include_bond=False
                                )
                                await client.send_public_message(offer_msg)
                            except Exception as e:
                                logger.warning(f"Failed to announce offer to {new_node_id}: {e}")

                        # Start listener task.
                        # Prune completed tasks first to prevent listen_tasks from growing
                        # unboundedly on repeated reconnection cycles
                        self._prune_done_tasks()
                        task = asyncio.create_task(self._listen_client(new_node_id, client))
                        self.listen_tasks.append(task)

                        # Notify reconnection
                        connected_count = len(self.directory_clients)
                        total_count = len(self.config.directory_servers)
                        spawn_task(
                            get_notifier().notify_directory_reconnect(
                                new_node_id, connected_count, total_count
                            )
                        )

                        # If all directories were previously disconnected, send a recovery alert
                        if self._all_directories_disconnected:
                            self._all_directories_disconnected = False
                            spawn_task(
                                get_notifier().notify_all_directories_reconnected(
                                    connected_count, total_count
                                )
                            )
                    else:
                        # Increment retry counter
                        self._directory_reconnect_attempts[node_id] = attempts + 1
                        logger.debug(
                            f"Reconnection to {dir_server} failed "
                            f"(attempt {attempts + 1}"
                            f"{f'/{max_retries}' if max_retries > 0 else ''})"
                        )

            except asyncio.CancelledError:
                logger.info("Directory reconnection task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in directory reconnection task: {e}")

        logger.info("Directory reconnection task stopped")

    async def _monitor_pending_transactions(self) -> None:
        """
        Background task to monitor pending transactions and update their status.

        Checks pending transactions every 60 seconds and updates their confirmation
        status in the history file. Transactions are marked as successful once they
        receive their first confirmation.
        """
        logger.info("Starting pending transaction monitor...")
        check_interval = 60.0  # Check every 60 seconds

        while self.running:
            try:
                await asyncio.sleep(check_interval)
                await self._update_pending_history()

            except asyncio.CancelledError:
                logger.info("Pending transaction monitor cancelled")
                break
            except Exception as e:
                logger.error(f"Error in pending transaction monitor: {e}")

        logger.info("Pending transaction monitor stopped")

    async def _update_pending_history(self: MakerBotProtocol) -> None:
        """Check and update pending transaction confirmations in history.

        For entries without txid, attempts to discover the txid by checking
        if the destination address has received funds.

        Transactions that remain pending longer than pending_tx_timeout_min
        are marked as failed (taker likely never broadcast the transaction).
        """
        from datetime import datetime

        from jmwallet.history import (
            get_pending_transactions,
            mark_pending_transaction_failed,
            update_pending_transaction_txid,
            update_transaction_confirmation_with_detection,
        )

        wallet_fp = self.wallet.wallet_fingerprint
        pending = get_pending_transactions(
            data_dir=self.config.data_dir, wallet_fingerprint=wallet_fp
        )
        if not pending:
            return

        logger.debug(f"Checking {len(pending)} pending transaction(s)...")
        timeout_minutes = self.config.pending_tx_timeout_min

        for entry in pending:
            try:
                # Calculate age of the pending transaction
                timestamp = datetime.fromisoformat(entry.timestamp)
                age_minutes = (datetime.now() - timestamp).total_seconds() / 60

                # If entry has no txid, try to discover it from the blockchain
                if not entry.txid:
                    if entry.destination_address:
                        logger.debug(
                            f"Attempting to discover txid for pending entry "
                            f"(dest: {entry.destination_address[:20]}...)"
                        )
                        # Look for the txid that paid to our CoinJoin address
                        txid = await self._discover_txid_for_address(entry.destination_address)
                        if txid:
                            update_pending_transaction_txid(
                                destination_address=entry.destination_address,
                                txid=txid,
                                data_dir=self.config.data_dir,
                                wallet_fingerprint=wallet_fp,
                            )
                            logger.info(
                                f"Discovered txid {txid[:16]}... for address "
                                f"{entry.destination_address[:20]}..."
                            )
                            # Update entry for confirmation check below
                            entry.txid = txid
                        elif age_minutes >= timeout_minutes:
                            # Timed out waiting for taker to broadcast
                            mark_pending_transaction_failed(
                                destination_address=entry.destination_address,
                                failure_reason=(
                                    f"Timed out after {int(age_minutes)} minutes - "
                                    "taker never broadcast transaction"
                                ),
                                data_dir=self.config.data_dir,
                                wallet_fingerprint=wallet_fp,
                            )
                            continue
                        else:
                            logger.debug(
                                f"No UTXO found for {entry.destination_address[:20]}... "
                                f"(tx may not be confirmed yet, age: {age_minutes:.1f}m)"
                            )
                            continue
                    else:
                        logger.debug("Pending entry has no txid and no destination address")
                        continue

                # Determine confirmation depth. Full nodes report it directly
                # by txid; light clients (Neutrino) are mempool-only there
                # (get_transaction returns None once a tx confirms), so confirm
                # via a chain lookup of the CoinJoin output address instead of
                # mistaking a confirmed CoinJoin for a dropped one.
                if self.backend.can_get_confirmations_by_txid():
                    tx_info = await self.backend.get_transaction(entry.txid)
                    confirmations = tx_info.confirmations if tx_info is not None else None
                else:
                    confirmations = await self._chain_confirmations_for_entry(entry)

                if confirmations is None:
                    # Transaction not found - might have been rejected/replaced
                    # or never made it to the mempool
                    if age_minutes >= timeout_minutes:
                        # Mark as failed - tx was never broadcast or got dropped
                        mark_pending_transaction_failed(
                            destination_address=entry.destination_address,
                            failure_reason=(
                                f"Transaction {entry.txid[:16]}... not found after "
                                f"{int(age_minutes)} minutes - likely never broadcast"
                            ),
                            data_dir=self.config.data_dir,
                            txid=entry.txid,
                            wallet_fingerprint=wallet_fp,
                        )
                    elif age_minutes > 30:
                        # Log warning after 30 minutes
                        logger.warning(
                            f"Transaction {entry.txid[:16]}... not found after "
                            f"{age_minutes:.1f} minutes"
                        )
                    continue

                # First time we observe this CoinJoin in the mempool (still
                # unconfirmed). Dedupe per-process so we do not re-notify on
                # every poll while it waits for a block.
                if confirmations == 0:
                    mempool_notified = self._mempool_notified_txids
                    if entry.txid not in mempool_notified:
                        mempool_notified.add(entry.txid)
                        await get_notifier().notify_mempool(
                            txid=entry.txid,
                            cj_amount=entry.cj_amount,
                        )

                # Mark as successful once it gets first confirmation
                if confirmations > 0 and entry.confirmations == 0:
                    logger.info(
                        f"Transaction {entry.txid[:16]}... confirmed "
                        f"({confirmations} confirmation(s))"
                    )
                    await update_transaction_confirmation_with_detection(
                        txid=entry.txid,
                        confirmations=confirmations,
                        backend=self.backend,
                        data_dir=self.config.data_dir,
                        wallet_fingerprint=wallet_fp,
                    )
                    self._mempool_notified_txids.discard(entry.txid)
                    await get_notifier().notify_confirmed(
                        txid=entry.txid,
                        cj_amount=entry.cj_amount,
                        confirmations=confirmations,
                    )

            except Exception as e:
                txid_str = entry.txid[:16] if entry.txid else "unknown"
                logger.debug(f"Error checking transaction {txid_str}...: {e}")

    async def _discover_txid_for_address(self, address: str) -> str | None:
        """Try to discover the txid for a transaction that paid to an address.

        This is used when a maker history entry doesn't have a txid recorded
        (e.g., from older versions or if the txid wasn't captured).

        Args:
            address: The destination address to check

        Returns:
            Transaction ID if found, None otherwise
        """
        try:
            # Get UTXOs for this address - if there are any, the first one's txid
            # is likely our CoinJoin (assuming fresh addresses are used)
            utxos = await self.backend.get_utxos([address])
            if utxos:
                # Return the txid of the first (and likely only) UTXO
                return utxos[0].txid
            return None
        except Exception as e:
            logger.debug(f"Error discovering txid for {address[:20]}...: {e}")
            return None

    async def _chain_confirmations_for_entry(self, entry: TransactionHistoryEntry) -> int | None:
        """Confirmation depth for a pending entry on a light-client backend.

        Neutrino cannot resolve a transaction by txid, so confirmation is read
        from the CoinJoin output itself: look up UTXOs for the entry's
        destination address and match the recorded txid. Returns the output's
        confirmation count (0 while it is still an unconfirmed mempool entry),
        or None when no matching output is found, which the caller treats the
        same as a missing transaction.
        """
        if not entry.destination_address or not entry.txid:
            return None
        try:
            utxos = await self.backend.get_utxos([entry.destination_address])
        except Exception as e:
            logger.debug(f"Chain confirmation lookup failed for {entry.txid[:16]}...: {e}")
            return None
        match = next((utxo for utxo in utxos if utxo.txid == entry.txid), None)
        if match is None:
            return None
        return match.confirmations if match.confirmations > 0 else 0

    async def _deferred_wallet_resync(self: MakerBotProtocol) -> None:
        """Resync wallet in background after a CoinJoin completes."""
        try:
            # Small delay to allow transaction to propagate
            await asyncio.sleep(2)
            logger.info("Performing deferred wallet resync after CoinJoin...")
            await self._resync_wallet_and_update_offers()
        except Exception as e:
            logger.error(f"Error in deferred wallet resync: {e}")

    async def _listen_client(self: MakerBotProtocol, node_id: str, client: DirectoryClient) -> None:
        """Listen for messages from a specific directory client"""
        logger.info(f"Started listening on {node_id}")

        # Track last cleanup time
        last_cleanup = asyncio.get_event_loop().time()
        cleanup_interval = 60.0  # Clean up timed-out sessions every 60 seconds

        # Track consecutive errors for exponential backoff
        consecutive_errors = 0
        max_consecutive_errors = 10

        while self.running:
            try:
                # Use listen_for_messages with short duration to check running flag frequently
                messages = await client.listen_for_messages(duration=1.0)

                if messages:
                    logger.debug(f"Received {len(messages)} messages from {node_id}")

                for message in messages:
                    await self._handle_message(message, source=f"dir:{node_id}")

                # Periodic cleanup of timed-out sessions
                now = asyncio.get_event_loop().time()
                if now - last_cleanup > cleanup_interval:
                    self._cleanup_timed_out_sessions()
                    last_cleanup = now

                # Reset error counter on successful iteration
                consecutive_errors = 0

            except asyncio.CancelledError:
                logger.info(f"Listener for {node_id} cancelled")
                break
            except DirectoryClientError as e:
                # Connection lost - remove from directory_clients so reconnection task can handle it
                logger.warning(f"Connection lost on {node_id}: {e}")

                # Remove from connected clients
                self.directory_clients.pop(node_id, None)

                # Close the client gracefully
                try:
                    await client.close()
                except Exception:
                    pass

                # Fire-and-forget notification for directory disconnect
                connected_count = len(self.directory_clients)
                total_count = len(self.config.directory_servers)
                spawn_task(
                    get_notifier().notify_directory_disconnect(
                        node_id, connected_count, total_count, reconnecting=True
                    )
                )
                if connected_count == 0:
                    self._all_directories_disconnected = True
                    spawn_task(get_notifier().notify_all_directories_disconnected())
                break
            except Exception as e:
                consecutive_errors += 1
                backoff = min(2**consecutive_errors, 60.0)
                logger.error(
                    f"Error listening on {node_id} (consecutive: {consecutive_errors}): {e}"
                )
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        f"Too many consecutive errors on {node_id}, disconnecting for reconnection"
                    )
                    self.directory_clients.pop(node_id, None)
                    try:
                        await client.close()
                    except Exception:
                        pass
                    break
                await asyncio.sleep(backoff)

        logger.info(f"Stopped listening on {node_id}")

    async def _periodic_summary(self: MakerBotProtocol) -> None:
        """Background task to periodically send summary notifications.

        Sends a notification with CoinJoin stats for the configured period
        (e.g., daily or weekly). Only runs when notify_summary is enabled.

        When ``check_for_updates`` is enabled, the latest release version is
        fetched from GitHub (routed through Tor if configured) and included in
        the summary notification.
        """
        from jmcore.version import check_for_updates_from_github, get_version
        from jmwallet.history import get_history_stats_for_period

        notifier = get_notifier()
        interval_hours = notifier.config.summary_interval_hours
        interval_seconds = interval_hours * 3600

        if interval_hours == 24:
            period_label = "Daily"
        elif interval_hours == 168:
            period_label = "Weekly"
        elif interval_hours == 1:
            period_label = "Hourly"
        else:
            period_label = f"{interval_hours}-Hour"

        logger.info(f"Starting periodic summary task ({period_label}, every {interval_hours}h)...")

        while self.running:
            try:
                await asyncio.sleep(interval_seconds)

                if not self.running:
                    break

                logger.debug(f"Collecting {period_label.lower()} summary stats...")
                stats = get_history_stats_for_period(
                    hours=interval_hours,
                    role_filter="maker",
                    data_dir=self.config.data_dir,
                )

                logger.info(
                    f"{period_label} summary: "
                    f"coinjoins={int(stats['total_coinjoins'])}, "
                    f"successful={int(stats['successful_coinjoins'])}, "
                    f"failed={int(stats['failed_coinjoins'])}, "
                    f"fees={int(stats['total_fees_earned'])} sats, "
                    f"volume={int(stats['successful_volume'])}"
                    f"/{int(stats['total_volume'])} sats, "
                    f"utxos_disclosed={int(stats['utxos_disclosed'])}"
                )

                # Check for updates if enabled
                current_version: str | None = None
                update_available: str | None = None
                if notifier.config.check_for_updates:
                    current_version = get_version()
                    socks_proxy: str | None = None
                    if notifier.config.use_tor:
                        if notifier.config.stream_isolation:
                            from jmcore.tor_isolation import (
                                IsolationCategory,
                                build_isolated_proxy_url,
                            )

                            socks_proxy = build_isolated_proxy_url(
                                notifier.config.tor_socks_host,
                                notifier.config.tor_socks_port,
                                IsolationCategory.UPDATE_CHECK,
                            )
                        else:
                            socks_proxy = (
                                f"socks5h://{notifier.config.tor_socks_host}"
                                f":{notifier.config.tor_socks_port}"
                            )
                    result = await check_for_updates_from_github(
                        socks_proxy=socks_proxy,
                    )
                    if result and result.is_newer:
                        update_available = result.latest_version
                        logger.info(
                            f"Update available: {result.latest_version} "
                            f"(current: {current_version})"
                        )

                # Collect wallet balance info if summary balance is enabled
                total_balance: int | None = None
                utxo_count: int | None = None
                if notifier.config.notify_summary_balance:
                    try:
                        total_balance = await self.wallet.get_total_balance()
                        utxo_count = sum(
                            len(
                                [
                                    u
                                    for u in self.wallet.utxo_cache.get(md, [])
                                    if not u.frozen and not u.is_fidelity_bond
                                ]
                            )
                            for md in range(self.wallet.mixdepth_count)
                        )
                    except Exception as e:
                        logger.warning(f"Failed to collect wallet balance for summary: {e}")

                sent = await notifier.notify_summary(
                    period_label=period_label,
                    total_requests=int(stats["total_coinjoins"]),
                    successful=int(stats["successful_coinjoins"]),
                    failed=int(stats["failed_coinjoins"]),
                    total_earnings=int(stats["total_fees_earned"]),
                    total_volume=int(stats["total_volume"]),
                    successful_volume=int(stats["successful_volume"]),
                    utxos_disclosed=int(stats["utxos_disclosed"]),
                    version=current_version,
                    update_available=update_available,
                    total_balance=total_balance,
                    utxo_count=utxo_count,
                )

                if sent:
                    logger.debug(f"{period_label} summary notification sent")
                else:
                    logger.warning(f"{period_label} summary notification failed to send")

            except asyncio.CancelledError:
                logger.info("Periodic summary task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in periodic summary: {e}")

        logger.info("Periodic summary task stopped")
