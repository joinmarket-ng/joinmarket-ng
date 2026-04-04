"""
Orderbook aggregation logic across multiple directory nodes.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from jmcore.bond_calc import calculate_timelocked_fidelity_bond_value
from jmcore.btc_script import derive_bond_address
from jmcore.mempool_api import MempoolAPI
from jmcore.models import FidelityBond, Offer, OrderBook
from loguru import logger

if TYPE_CHECKING:
    from jmwallet.backends.base import BlockchainBackend

from jmwallet.backends.base import BondVerificationRequest

from orderbook_watcher.directory_client import DirectoryClient
from orderbook_watcher.health_checker import MakerHealthChecker


class DirectoryNodeStatus:
    def __init__(
        self,
        node_id: str,
        tracking_started: datetime | None = None,
        grace_period_seconds: int = 0,
    ) -> None:
        self.node_id = node_id
        self.connected = False
        self.last_connected: datetime | None = None
        self.last_disconnected: datetime | None = None
        self.connection_attempts = 0
        self.successful_connections = 0
        self.total_uptime_seconds = 0.0
        self.current_session_start: datetime | None = None
        self.tracking_started = tracking_started or datetime.now(UTC)
        self.grace_period_seconds = grace_period_seconds
        # Exponential backoff state
        self.retry_delay: float = 4.0  # Initial retry delay in seconds
        self.retry_delay_max: float = 3600.0  # Max retry delay (1 hour)
        self.retry_delay_multiplier: float = 1.5

    def mark_connected(self, current_time: datetime | None = None) -> None:
        now = current_time or datetime.now(UTC)
        self.connected = True
        self.last_connected = now
        self.current_session_start = now
        self.successful_connections += 1
        # Reset retry delay on successful connection
        self.retry_delay = 4.0

    def mark_disconnected(self, current_time: datetime | None = None) -> None:
        now = current_time or datetime.now(UTC)
        if self.connected and self.current_session_start:
            # Only count uptime after grace period
            grace_end_ts = self.tracking_started.timestamp() + self.grace_period_seconds
            session_start_ts = self.current_session_start.timestamp()
            now_ts = now.timestamp()

            # Calculate the actual uptime to record (only after grace period)
            if now_ts > grace_end_ts:
                # Some or all of the session is after grace period
                counted_start = max(session_start_ts, grace_end_ts)
                session_duration = now_ts - counted_start
                self.total_uptime_seconds += max(0, session_duration)

        self.connected = False
        self.last_disconnected = now
        self.current_session_start = None

    def get_uptime_percentage(self, current_time: datetime | None = None) -> float:
        if not self.tracking_started:
            return 0.0
        now = current_time or datetime.now(UTC)
        elapsed = (now - self.tracking_started).total_seconds()

        # If we're still in grace period, return 100% uptime
        if elapsed < self.grace_period_seconds:
            return 100.0

        # Calculate total time excluding grace period
        total_time = elapsed - self.grace_period_seconds
        if total_time == 0:
            return 0.0

        # Calculate uptime, but only count time after grace period ends
        grace_end = self.tracking_started.timestamp() + self.grace_period_seconds
        current_uptime = self.total_uptime_seconds

        if self.connected and self.current_session_start:
            # Only count uptime after grace period ended
            session_start_ts = self.current_session_start.timestamp()
            if session_start_ts < grace_end:
                # Session started during grace period, only count time after grace ended
                uptime_duration = now.timestamp() - grace_end
            else:
                # Session started after grace period
                uptime_duration = (now - self.current_session_start).total_seconds()
            current_uptime += max(0, uptime_duration)

        return (current_uptime / total_time) * 100.0

    def to_dict(self, current_time: datetime | None = None) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "connected": self.connected,
            "last_connected": self.last_connected.isoformat() if self.last_connected else None,
            "last_disconnected": self.last_disconnected.isoformat()
            if self.last_disconnected
            else None,
            "connection_attempts": self.connection_attempts,
            "successful_connections": self.successful_connections,
            "uptime_percentage": round(self.get_uptime_percentage(current_time), 2),
            "tracking_started": self.tracking_started.isoformat()
            if self.tracking_started
            else None,
            "retry_delay": self.retry_delay,
        }

    def get_next_retry_delay(self) -> float:
        """Get the next retry delay using exponential backoff."""
        current_delay = self.retry_delay
        # Increase delay for next retry
        self.retry_delay = min(self.retry_delay * self.retry_delay_multiplier, self.retry_delay_max)
        return current_delay


class OrderbookAggregator:
    def __init__(
        self,
        directory_nodes: list[tuple[str, int]],
        network: str,
        mempool_api_url: str,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9050,
        timeout: float = 30.0,
        max_retry_attempts: int = 3,
        retry_delay: float = 5.0,
        max_message_size: int = 2097152,
        uptime_grace_period: int = 60,
        stream_isolation: bool = False,
        blockchain_backend: BlockchainBackend | None = None,
    ) -> None:
        self.directory_nodes = directory_nodes
        self.network = network
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.stream_isolation = stream_isolation
        self.timeout = timeout
        self.mempool_api_url = mempool_api_url
        self.max_retry_attempts = max_retry_attempts
        self.retry_delay = retry_delay
        self.max_message_size = max_message_size
        self.uptime_grace_period = uptime_grace_period
        self.blockchain_backend = blockchain_backend

        # Build mempool proxy URL and pre-compute isolation credentials
        self._dir_username: str | None = None
        self._dir_password: str | None = None
        self._hc_username: str | None = None
        self._hc_password: str | None = None
        if stream_isolation:
            from jmcore.tor_isolation import (  # noqa: PLC0415
                IsolationCategory,
                build_isolated_proxy_url,
                get_isolation_credentials,
            )

            socks_proxy = build_isolated_proxy_url(
                socks_host, socks_port, IsolationCategory.MEMPOOL
            )
            dir_c = get_isolation_credentials(IsolationCategory.DIRECTORY)
            self._dir_username = dir_c.username
            self._dir_password = dir_c.password
            hc_c = get_isolation_credentials(IsolationCategory.HEALTH_CHECK)
            self._hc_username = hc_c.username
            self._hc_password = hc_c.password
        else:
            socks_proxy = f"socks5h://{socks_host}:{socks_port}"
        self.mempool_api: MempoolAPI | None = None
        self._socks_test_task: asyncio.Task[Any] | None = None
        if mempool_api_url:
            logger.info(f"Configuring MempoolAPI with SOCKS proxy: {socks_proxy}")
            mempool_timeout = 60.0
            self.mempool_api = MempoolAPI(
                base_url=mempool_api_url, socks_proxy=socks_proxy, timeout=mempool_timeout
            )
            self._socks_test_task = asyncio.create_task(self._test_socks_connection())
        else:
            logger.info("Mempool API disabled by configuration; external mempool lookups are off")
        self.current_orderbook: OrderBook = OrderBook()
        self._lock = asyncio.Lock()
        self.clients: dict[str, DirectoryClient] = {}
        self.listener_tasks: list[asyncio.Task[Any]] = []
        self._bond_calculation_task: asyncio.Task[Any] | None = None
        self._bond_queue: asyncio.Queue[OrderBook] = asyncio.Queue()
        self._bond_cache: dict[str, FidelityBond] = {}
        self._last_offers_hash: int = 0
        self._mempool_semaphore = asyncio.Semaphore(5)
        self.node_statuses: dict[str, DirectoryNodeStatus] = {}
        self._retry_tasks: list[asyncio.Task[Any]] = []

        # Maker health checker for direct reachability verification
        self.health_checker = MakerHealthChecker(
            network=network,
            socks_host=socks_host,
            socks_port=socks_port,
            timeout=timeout,
            check_interval=600.0,  # Check each maker at most once per 10 minutes
            max_concurrent_checks=5,
            socks_username=self._hc_username,
            socks_password=self._hc_password,
        )

        for onion_address, port in directory_nodes:
            node_id = f"{onion_address}:{port}"
            self.node_statuses[node_id] = DirectoryNodeStatus(
                node_id, grace_period_seconds=uptime_grace_period
            )

    def _handle_client_disconnect(self, onion_address: str, port: int) -> None:
        node_id = f"{onion_address}:{port}"
        client = self.clients.pop(node_id, None)
        if client:
            client.stop()
        self._schedule_reconnect(onion_address, port)

    def _schedule_reconnect(self, onion_address: str, port: int) -> None:
        node_id = f"{onion_address}:{port}"
        self._retry_tasks = [task for task in self._retry_tasks if not task.done()]
        if any(task.get_name() == f"retry:{node_id}" for task in self._retry_tasks):
            logger.debug(f"Retry already scheduled for {node_id}")
            return
        retry_task = asyncio.create_task(self._retry_failed_connection(onion_address, port))
        retry_task.set_name(f"retry:{node_id}")
        self._retry_tasks.append(retry_task)
        logger.info(f"Scheduled retry task for {node_id}")

    async def fetch_from_directory(
        self, onion_address: str, port: int
    ) -> tuple[list[Offer], list[FidelityBond], str]:
        node_id = f"{onion_address}:{port}"
        logger.info(f"Fetching orderbook from directory: {node_id}")
        client = DirectoryClient(
            onion_address,
            port,
            self.network,
            socks_host=self.socks_host,
            socks_port=self.socks_port,
            timeout=self.timeout,
            max_message_size=self.max_message_size,
            socks_username=self._dir_username,
            socks_password=self._dir_password,
        )
        try:
            await client.connect()
            offers, bonds = await client.fetch_orderbooks()

            for offer in offers:
                offer.directory_node = node_id
            for bond in bonds:
                bond.directory_node = node_id

            return offers, bonds, node_id
        except Exception as e:
            logger.error(f"Failed to fetch from directory {node_id}: {e}")
            return [], [], node_id
        finally:
            await client.close()

    async def update_orderbook(self) -> OrderBook:
        tasks = [
            self.fetch_from_directory(onion_address, port)
            for onion_address, port in self.directory_nodes
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        new_orderbook = OrderBook(timestamp=datetime.now(UTC))

        for result in results:
            if isinstance(result, BaseException):
                logger.error(f"Directory fetch failed: {result}")
                continue

            offers, bonds, node_id = result
            if offers or bonds:
                new_orderbook.add_offers(offers, node_id)
                new_orderbook.add_fidelity_bonds(bonds, node_id)

        await self._calculate_bond_values(new_orderbook)

        async with self._lock:
            self.current_orderbook = new_orderbook

        logger.info(
            f"Updated orderbook: {len(new_orderbook.offers)} offers, "
            f"{len(new_orderbook.fidelity_bonds)} bonds from "
            f"{len(new_orderbook.directory_nodes)} directory nodes"
        )

        return new_orderbook

    async def get_orderbook(self) -> OrderBook:
        async with self._lock:
            return self.current_orderbook

    async def _background_bond_calculator(self) -> None:
        while True:
            try:
                orderbook = await self._bond_queue.get()
                await self._calculate_bond_values(orderbook)
                for offer in orderbook.offers:
                    if offer.fidelity_bond_data:
                        matching_bonds = [
                            b
                            for b in orderbook.fidelity_bonds
                            if b.counterparty == offer.counterparty
                            and b.utxo_txid == offer.fidelity_bond_data.get("utxo_txid")
                        ]
                        if matching_bonds and matching_bonds[0].bond_value is not None:
                            offer.fidelity_bond_value = matching_bonds[0].bond_value
                logger.debug("Background bond calculation completed")
            except Exception as e:
                logger.error(f"Error in background bond calculator: {e}")

    async def _periodic_directory_connection_status(self) -> None:
        """Background task to periodically log directory connection status.

        This runs every 10 minutes to provide visibility into orderbook
        connectivity. Shows:
        - Total directory servers configured
        - Currently connected servers with uptime percentage
        - Disconnected servers (if any)
        """
        # First log after 5 minutes (give time for initial connection)
        await asyncio.sleep(300)

        while True:
            try:
                total_servers = len(self.directory_nodes)
                connected_nodes = []
                disconnected_nodes = []

                for node_id, status in self.node_statuses.items():
                    if status.connected:
                        uptime_pct = status.get_uptime_percentage()
                        connected_nodes.append(f"{node_id} ({uptime_pct:.1f}% uptime)")
                    else:
                        disconnected_nodes.append(node_id)

                connected_count = len(connected_nodes)

                if disconnected_nodes:
                    disconnected_str = ", ".join(disconnected_nodes[:5])
                    if len(disconnected_nodes) > 5:
                        disconnected_str += f", ... and {len(disconnected_nodes) - 5} more"
                    logger.warning(
                        f"Directory connection status: {connected_count}/{total_servers} connected. "
                        f"Disconnected: [{disconnected_str}]"
                    )
                else:
                    connected_str = ", ".join(connected_nodes[:5])
                    if len(connected_nodes) > 5:
                        connected_str += f", ... and {len(connected_nodes) - 5} more"
                    logger.info(
                        f"Directory connection status: {connected_count}/{total_servers} connected "
                        f"[{connected_str}]"
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

    async def _periodic_peerlist_refresh(self) -> None:
        """Background task to periodically refresh peerlists and clean up stale offers.

        This runs every 5 minutes to:
        1. Refresh peerlists from all connected directories
        2. Remove offers from nicks that are no longer in ANY directory's peerlist
        3. Deduplicate offers that use the same fidelity bond UTXO

        IMPORTANT: Cleanup only happens if ALL directories with peerlist support report
        the nick as gone. We err on the side of showing offers when in doubt.
        """
        # Initial wait to let connections stabilize
        await asyncio.sleep(120)

        while True:
            try:
                # Collect active nicks from ALL connected directories
                # Always collect nicks even if refresh fails - use cached state
                all_active_nicks: set[str] = set()
                directories_checked = 0
                directories_with_peerlist_support = 0
                refresh_failures = 0

                for node_id, client in self.clients.items():
                    try:
                        # Refresh peerlist for this client
                        # This updates the client's active_peers tracking
                        await client.get_peerlist_with_features()

                        # Track if this directory supports peerlist
                        if client._peerlist_supported:
                            directories_with_peerlist_support += 1
                    except Exception as e:
                        logger.debug(f"Failed to refresh peerlist from {node_id}: {e}")
                        refresh_failures += 1

                    # ALWAYS collect active nicks, even if refresh failed
                    # The client's _active_peers contains accumulated state from previous responses
                    active_nicks = client.get_active_nicks()
                    all_active_nicks.update(active_nicks)
                    directories_checked += 1

                    logger.debug(f"Directory {node_id}: {len(active_nicks)} active nicks")

                if directories_checked > 0:
                    logger.info(
                        f"Peerlist refresh: {len(all_active_nicks)} unique nicks "
                        f"across {directories_checked} directories "
                        f"({directories_with_peerlist_support} support GETPEERLIST, "
                        f"{refresh_failures} refresh failures)"
                    )

                    # Clean up offers from nicks that are no longer in ANY directory's peerlist
                    # Only do this if at least one directory supports GETPEERLIST AND
                    # we successfully refreshed from all peerlist-supporting directories
                    if directories_with_peerlist_support > 0 and refresh_failures == 0:
                        total_removed = 0
                        all_stale_nicks: set[str] = set()
                        for client in self.clients.values():
                            # Get all nicks with offers in this client
                            client_nicks = {key[0] for key in client.offers}

                            # Remove offers from nicks not in the global active list
                            stale_nicks = client_nicks - all_active_nicks
                            all_stale_nicks.update(stale_nicks)
                            for nick in stale_nicks:
                                removed = client.remove_offers_for_nick(nick)
                                total_removed += removed

                        if total_removed > 0:
                            logger.info(
                                f"Peerlist cleanup: removed {total_removed} offers from "
                                f"{len(all_stale_nicks)} nicks no longer in any peerlist"
                            )
                    elif refresh_failures > 0:
                        logger.debug(
                            "Skipping peerlist cleanup due to refresh failures - "
                            "avoiding false positives"
                        )

                    # After peerlist refresh, check makers that still don't have features
                    # This handles the case where directory doesn't support peerlist_features
                    # or the maker's features weren't included in the peerlist
                    try:
                        await self._check_makers_without_features()
                    except Exception as e:
                        logger.debug(f"Error checking makers without features: {e}")

                # Sleep for 5 minutes before next refresh
                await asyncio.sleep(300)

            except asyncio.CancelledError:
                logger.info("Peerlist refresh task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in peerlist refresh task: {e}")
                await asyncio.sleep(300)

        logger.info("Peerlist refresh task stopped")

    def _prioritize_makers_for_scan(
        self,
        makers: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        """Sort makers for scanning: bonded (desc bond value), then bondless (asc fee).

        During sybil attacks the orderbook may contain thousands of fake offers.
        Scanning them in arbitrary order wastes the limited concurrent-check
        slots on attackers while legitimate makers never get their features
        discovered.  By processing bonded makers first (highest bond value first)
        and then bondless makers in ascending fee order we ensure the most
        trustworthy and cheapest liquidity is scanned before the spam.

        Args:
            makers: List of (nick, location) tuples to prioritize.

        Returns:
            Sorted list of (nick, location) tuples.
        """
        # Build lookup: nick -> (best_bond_value, lowest_fee_rate)
        # We scan all clients for the best bond value and lowest fee for each nick.
        nick_bond_value: dict[str, int] = {}
        nick_fee_rate: dict[str, float] = {}

        for client in self.clients.values():
            for key, offer_ts in client.offers.items():
                nick = key[0]
                offer = offer_ts.offer

                # Track best (highest) bond value
                bond_val = offer.fidelity_bond_value
                if bond_val > nick_bond_value.get(nick, 0):
                    nick_bond_value[nick] = bond_val

                # Track lowest fee rate for ordering bondless makers.
                # Normalise both offer types to a comparable float:
                #   - Relative offers: the cjfee string is already a rate
                #     (e.g. "0.003" = 0.3%)
                #   - Absolute offers: express as rate relative to maxsize
                #     so that we have a rough comparison metric.
                try:
                    if offer.is_absolute_fee():
                        fee_rate = float(int(offer.cjfee)) / max(offer.maxsize, 1)
                    else:
                        fee_rate = float(offer.cjfee)
                except (ValueError, TypeError, ZeroDivisionError):
                    fee_rate = float("inf")

                if fee_rate < nick_fee_rate.get(nick, float("inf")):
                    nick_fee_rate[nick] = fee_rate

        def sort_key(item: tuple[str, str]) -> tuple[int, float, float]:
            nick = item[0]
            bond = nick_bond_value.get(nick, 0)
            fee = nick_fee_rate.get(nick, float("inf"))
            # Primary: bonded first (group 0) vs bondless (group 1)
            # Secondary for bonded: descending bond value (negate)
            # Secondary for bondless: ascending fee rate
            if bond > 0:
                return (0, -bond, fee)
            return (1, 0, fee)

        sorted_makers = sorted(makers, key=sort_key)

        # Log a brief summary
        bonded_count = sum(1 for n, _ in sorted_makers if nick_bond_value.get(n, 0) > 0)
        logger.info(
            f"Scan priority: {bonded_count} bonded makers first, "
            f"{len(sorted_makers) - bonded_count} bondless (fee-ascending)"
        )
        return sorted_makers

    async def _check_makers_without_features(self) -> None:
        """Check makers that have offers but no features discovered yet.

        This is called after peerlist refresh to ensure we discover features
        for makers whose features weren't included in the peerlist (e.g., from
        reference implementation directories that don't support peerlist_features).

        Makers are scanned in priority order: bonded makers first (descending
        bond value), then bondless makers in ascending fee order.  This ensures
        legitimate makers get their features discovered before sybil spam.
        """
        makers_to_check: list[tuple[str, str]] = []

        for _node_id, client in self.clients.items():
            for key, _offer_ts in client.offers.items():
                nick = key[0]
                # Check if this peer has no features
                peer_features = client.peer_features.get(nick, {})
                if not peer_features:
                    # Try to find location
                    location = client._active_peers.get(nick)
                    if location and location != "NOT-SERVING-ONION":
                        makers_to_check.append((nick, location))

        # Deduplicate by location (keep first nick per location after priority sort)
        unique_makers = {loc: (nick, loc) for nick, loc in makers_to_check}
        makers_list = self._prioritize_makers_for_scan(list(unique_makers.values()))

        if not makers_list:
            return

        logger.info(f"Feature discovery: Checking {len(makers_list)} makers without features")

        # Check makers and extract features from handshake
        health_statuses = await self.health_checker.check_makers_batch(makers_list, force=True)

        # Update features in directory clients' peer_features cache
        features_discovered = 0
        for _location, status in health_statuses.items():
            if status.reachable and status.features.features:
                features_dict = status.features.to_dict()
                features_discovered += 1
                # Update all clients with this peer's features using merge
                # (never overwrite/downgrade existing features)
                for client in self.clients.values():
                    if status.nick in client._active_peers:
                        client._merge_peer_features(status.nick, features_dict)
                        # Also update cached offers
                        client._update_offer_features(status.nick, features_dict)

        if features_discovered > 0:
            logger.info(f"Feature discovery: Discovered features for {features_discovered} makers")

    async def _periodic_maker_health_check(self) -> None:
        """Background task to periodically check maker reachability via direct connections.

        This runs every 15 minutes to:
        1. Collect all makers with valid onion addresses from current offers
        2. Check if they're reachable via direct Tor connection
        3. Extract features from handshake (for directories without peerlist_features)
        4. Log reachability statistics for monitoring

        NOTE: This task does NOT remove offers from unreachable makers. The directory
        server may still have a valid connection to the maker even if we can't reach
        them directly. We trust the directory's peerlist for offer validity.
        """
        # Initial wait to let orderbook populate - reduced from 10 minutes to 2 minutes
        # to discover features faster for newly connected makers
        await asyncio.sleep(120)  # 2 minutes

        # Health check interval: check makers every 15 minutes
        check_interval = 900.0

        while True:
            try:
                # Collect all unique makers with their locations from all connected clients
                makers_to_check: dict[str, tuple[str, str]] = {}  # location -> (nick, location)

                for _node_id, client in self.clients.items():
                    offers_with_ts = client.get_offers_with_timestamps()
                    for offer_ts in offers_with_ts:
                        offer = offer_ts.offer
                        nick = offer.counterparty

                        # Try to find location from peerlist
                        active_peers = client._active_peers  # nick -> location
                        location = active_peers.get(nick)

                        # If location not found or is NOT-SERVING-ONION, skip health check
                        if not location or location == "NOT-SERVING-ONION":
                            continue

                        # Only check each unique location once (makers may have multiple offers)
                        if location not in makers_to_check:
                            makers_to_check[location] = (nick, location)

                if not makers_to_check:
                    logger.debug(
                        "Maker health check: No makers with valid onion addresses to check"
                    )
                else:
                    logger.info(
                        f"Maker health check: Checking reachability of {len(makers_to_check)} "
                        "unique makers"
                    )

                    # Sort by priority: bonded first (desc value), then bondless (asc fee)
                    makers_list = self._prioritize_makers_for_scan(list(makers_to_check.values()))
                    health_statuses = await self.health_checker.check_makers_batch(makers_list)

                    # Log unreachable makers for debugging but DO NOT remove offers
                    # The directory server may still have a valid connection to the maker
                    # even if we can't reach them directly. Trust the directory's peerlist
                    # for offer validity, not our own direct connection attempts.
                    unreachable_locations = self.health_checker.get_unreachable_locations(
                        max_consecutive_failures=3
                    )

                    reachable_count = sum(1 for s in health_statuses.values() if s.reachable)
                    logger.info(
                        f"Maker health check: {reachable_count}/{len(makers_to_check)} makers "
                        f"directly reachable, {len(unreachable_locations)} unreachable"
                    )

                # Sleep until next check
                await asyncio.sleep(check_interval)

            except asyncio.CancelledError:
                logger.info("Maker health check task cancelled")
                break
            except Exception as e:
                logger.error(f"Error in maker health check task: {e}")
                await asyncio.sleep(check_interval)

        logger.info("Maker health check task stopped")

    async def _connect_to_node(self, onion_address: str, port: int) -> DirectoryClient | None:
        node_id = f"{onion_address}:{port}"
        status = self.node_statuses[node_id]
        status.connection_attempts += 1

        logger.info(f"Connecting to directory: {node_id}")

        def on_disconnect() -> None:
            logger.info(f"Directory node {node_id} disconnected")
            status.mark_disconnected()
            self._handle_client_disconnect(onion_address, port)

        client = DirectoryClient(
            onion_address,
            port,
            self.network,
            socks_host=self.socks_host,
            socks_port=self.socks_port,
            timeout=self.timeout,
            max_message_size=self.max_message_size,
            on_disconnect=on_disconnect,
            socks_username=self._dir_username,
            socks_password=self._dir_password,
        )

        try:
            await client.connect()
            status.mark_connected()
            logger.info(f"Successfully connected to directory: {node_id} (our nick: {client.nick})")
            return client

        except Exception as e:
            logger.warning(f"Connection to directory {node_id} failed: {e}")
            await client.close()
            status.mark_disconnected()
            self._schedule_reconnect(onion_address, port)
            return None

    async def _retry_failed_connection(self, onion_address: str, port: int) -> None:
        """Retry connecting to a directory with exponential backoff."""
        node_id = f"{onion_address}:{port}"
        status = self.node_statuses[node_id]

        while True:
            # Get next retry delay with exponential backoff
            retry_delay = status.get_next_retry_delay()
            logger.info(f"Waiting {retry_delay:.1f}s before retrying connection to {node_id}")
            await asyncio.sleep(retry_delay)

            if node_id in self.clients:
                logger.debug(f"Node {node_id} already connected, stopping retry")
                return

            logger.info(f"Retrying connection to directory {node_id}...")
            client = await self._connect_to_node(onion_address, port)

            if client:
                self.clients[node_id] = client
                task = asyncio.create_task(client.listen_continuously())
                self.listener_tasks.append(task)
                logger.info(f"Successfully reconnected to directory: {node_id}")
                return

    async def start_continuous_listening(self) -> None:
        logger.info("Starting continuous listening on all directory nodes")

        self._bond_calculation_task = asyncio.create_task(self._background_bond_calculator())

        # Start periodic directory connection status logging task
        status_task = asyncio.create_task(self._periodic_directory_connection_status())
        self.listener_tasks.append(status_task)

        # Start periodic peerlist refresh task for cleanup
        peerlist_task = asyncio.create_task(self._periodic_peerlist_refresh())
        self.listener_tasks.append(peerlist_task)

        # Start periodic maker health check task (direct onion reachability verification)
        # This extracts features from handshake and tracks reachability but does NOT remove offers
        health_check_task = asyncio.create_task(self._periodic_maker_health_check())
        self.listener_tasks.append(health_check_task)

        connection_tasks = [
            self._connect_to_node(onion_address, port)
            for onion_address, port in self.directory_nodes
        ]

        clients = await asyncio.gather(*connection_tasks, return_exceptions=True)

        for (onion_address, port), result in zip(self.directory_nodes, clients, strict=True):
            node_id = f"{onion_address}:{port}"

            if isinstance(result, BaseException):
                logger.error(f"Connection to {node_id} raised exception: {result}")
                retry_task = asyncio.create_task(self._retry_failed_connection(onion_address, port))
                self._retry_tasks.append(retry_task)
                logger.info(f"Scheduled retry task for {node_id}")
            elif result is not None:
                self.clients[node_id] = result
                task = asyncio.create_task(result.listen_continuously())
                self.listener_tasks.append(task)
                logger.info(f"Started listener task for {node_id}")
            else:
                retry_task = asyncio.create_task(self._retry_failed_connection(onion_address, port))
                self._retry_tasks.append(retry_task)
                logger.info(f"Scheduled retry task for {node_id}")

        # Start early feature discovery task - runs once after initial connections settle
        early_feature_task = asyncio.create_task(self._early_feature_discovery())
        self.listener_tasks.append(early_feature_task)

    async def _early_feature_discovery(self) -> None:
        """Run feature discovery shortly after startup to populate features quickly.

        This is a one-shot task that runs after a brief delay to allow initial
        offers to be received, then checks makers without features.
        """
        try:
            # Wait for initial offers to arrive (30 seconds should be enough for !orderbook responses)
            await asyncio.sleep(30)
            logger.info("Running early feature discovery for makers without features")
            await self._check_makers_without_features()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Early feature discovery error: {e}")

    async def stop_listening(self) -> None:
        logger.info("Stopping all directory listeners")

        if self._bond_calculation_task:
            self._bond_calculation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._bond_calculation_task

        for task in self._retry_tasks:
            task.cancel()

        if self._retry_tasks:
            await asyncio.gather(*self._retry_tasks, return_exceptions=True)

        for client in self.clients.values():
            client.stop()

        for task in self.listener_tasks:
            task.cancel()

        if self.listener_tasks:
            await asyncio.gather(*self.listener_tasks, return_exceptions=True)

        for node_id, client in self.clients.items():
            self.node_statuses[node_id].mark_disconnected()
            await client.close()

        self.clients.clear()
        self.listener_tasks.clear()
        self._retry_tasks.clear()

    async def get_live_orderbook(self, calculate_bonds: bool = True) -> OrderBook:
        """Get the live orderbook with deduplication and bond value calculation.

        This method implements several key cleanup mechanisms:
        1. Deduplicates offers by bond UTXO - if multiple nicks use the same bond,
           only the most recently received offer is kept
        2. Deduplicates bonds by UTXO key
        3. Links bonds to offers and calculates bond values

        Returns:
            OrderBook with deduplicated offers and calculated bond values
        """
        orderbook = OrderBook(timestamp=datetime.now(UTC))

        # Collect all offers with timestamps from all connected clients
        # We'll use timestamp to determine which nick's offer to keep when same bond is used
        all_offers_with_timestamps: list[tuple[Offer, float, str | None, str]] = []

        total_offers_from_directories = 0
        total_bonds_from_directories = 0

        for node_id, client in self.clients.items():
            offers_with_ts = client.get_offers_with_timestamps()
            bonds = client.get_current_bonds()
            total_offers_from_directories += len(offers_with_ts)
            total_bonds_from_directories += len(bonds)

            # Log detailed info about offers from this directory
            offers_with_bonds_count = sum(1 for o in offers_with_ts if o.bond_utxo_key)
            logger.debug(
                f"Directory {node_id}: {len(offers_with_ts)} offers "
                f"({offers_with_bonds_count} with bonds), {len(bonds)} bonds"
            )

            for offer_ts in offers_with_ts:
                offer = offer_ts.offer
                offer.directory_node = node_id
                all_offers_with_timestamps.append(
                    (offer, offer_ts.received_at, offer_ts.bond_utxo_key, node_id)
                )

            for bond in bonds:
                bond.directory_node = node_id
            orderbook.add_fidelity_bonds(bonds, node_id)

        logger.info(
            f"Collected {total_offers_from_directories} offers and "
            f"{total_bonds_from_directories} bonds from {len(self.clients)} directories"
        )

        # Deduplicate offers by (bond UTXO, counterparty, oid)
        # A maker can have multiple offers (different oids) backed by the same bond - we keep all of them
        # For different makers using the same bond UTXO, keep only the most recent one per oid
        # This handles the case where a maker restarts with a new nick but same bond
        # We also track all directory_nodes that announced each offer for statistics
        #
        # Key: (bond_utxo_key, oid) - allows multiple oids per bond from same maker
        # Value: (offer, timestamp, directory_nodes, counterparty) - track counterparty for restart detection
        bond_oid_to_best_offer: dict[
            tuple[str, int], tuple[Offer, float, list[str], str]
        ] = {}  # (bond_utxo, oid) -> (offer, timestamp, directory_nodes, counterparty)
        offers_without_bond: list[Offer] = []

        # Track statistics for logging
        total_offers_processed = len(all_offers_with_timestamps)
        offers_with_bonds = 0
        offers_without_bonds = 0
        bond_replacements = 0

        for offer, timestamp, bond_utxo_key, _node_id in all_offers_with_timestamps:
            if bond_utxo_key:
                # Offer has a fidelity bond - key by (bond_utxo, oid) to preserve multiple offers
                offers_with_bonds += 1
                dedup_key = (bond_utxo_key, offer.oid)
                existing = bond_oid_to_best_offer.get(dedup_key)
                if existing is None:
                    # First offer for this (bond, oid) combination
                    directory_nodes = [offer.directory_node] if offer.directory_node else []
                    logger.debug(
                        f"Bond deduplication: First offer for bond {bond_utxo_key[:20]}... "
                        f"from {offer.counterparty} (oid={offer.oid})"
                    )
                    bond_oid_to_best_offer[dedup_key] = (
                        offer,
                        timestamp,
                        directory_nodes,
                        offer.counterparty,
                    )
                else:
                    old_offer, old_timestamp, directory_nodes, old_counterparty = existing
                    # Check if this is the same maker
                    is_same_maker = old_counterparty == offer.counterparty

                    if is_same_maker:
                        # Same maker from different directory - merge directory_nodes
                        if offer.directory_node and offer.directory_node not in directory_nodes:
                            directory_nodes.append(offer.directory_node)
                        # Keep newer timestamp but preserve accumulated directory_nodes
                        if timestamp > old_timestamp:
                            bond_oid_to_best_offer[dedup_key] = (
                                offer,
                                timestamp,
                                directory_nodes,
                                offer.counterparty,
                            )
                        logger.debug(
                            f"Bond deduplication: Same maker {offer.counterparty} (oid={offer.oid}) "
                            f"seen on {len(directory_nodes)} directories for bond {bond_utxo_key[:20]}..."
                        )
                    else:
                        # Different maker using same bond UTXO with same oid
                        # This is the "maker restart with new nick" scenario
                        # Only replace if timestamp difference suggests legitimate restart (>60s)
                        # Otherwise it might be clock skew between directories
                        time_diff = timestamp - old_timestamp

                        if abs(time_diff) < 60:
                            # Likely clock skew between directories, not a real restart
                            # Keep the older one (more stable) and log warning
                            logger.warning(
                                f"Bond deduplication: Ignoring potential duplicate from {offer.counterparty} "
                                f"(oid={offer.oid}) - same bond as {old_offer.counterparty} "
                                f"(oid={old_offer.oid}) with only {abs(time_diff):.1f}s difference "
                                f"[bond UTXO: {bond_utxo_key[:20]}...]. Likely clock skew."
                            )
                        elif time_diff > 0:
                            # Newer offer, likely legitimate maker restart
                            bond_replacements += 1
                            logger.info(
                                f"Bond deduplication: Replacing offer from {old_offer.counterparty} "
                                f"(oid={old_offer.oid}) with {offer.counterparty} (oid={offer.oid}) "
                                f"[same bond UTXO: {bond_utxo_key[:20]}..., "
                                f"age_diff={time_diff:.1f}s]"
                            )
                            # Reset directory_nodes for the new maker's offer
                            new_directory_nodes = (
                                [offer.directory_node] if offer.directory_node else []
                            )
                            bond_oid_to_best_offer[dedup_key] = (
                                offer,
                                timestamp,
                                new_directory_nodes,
                                offer.counterparty,
                            )
                        # else: older offer from different maker, ignore
            else:
                # Offer without bond
                offers_without_bonds += 1
                logger.debug(
                    f"Offer without bond from {offer.counterparty} (oid={offer.oid}, "
                    f"ordertype={offer.ordertype.value})"
                )
                offers_without_bond.append(offer)

        # Only log summary if there's something interesting (replacements or at debug level)
        if bond_replacements > 0:
            logger.info(
                f"Bond deduplication: Replaced {bond_replacements} offers from makers who "
                f"restarted with same bond. Result: {len(bond_oid_to_best_offer)} unique bond offers + "
                f"{len(offers_without_bond)} non-bond offers"
            )
        else:
            logger.debug(
                f"Bond deduplication: Processed {total_offers_processed} offers "
                f"({offers_with_bonds} with bonds, {offers_without_bonds} without bonds). "
                f"Result: {len(bond_oid_to_best_offer)} unique bond offers + "
                f"{len(offers_without_bond)} non-bond offers"
            )

        # Build final offers list - set directory_nodes from the accumulated list during bond dedup
        deduplicated_offers: list[Offer] = []
        for offer, _ts, directory_nodes, _counterparty in bond_oid_to_best_offer.values():
            offer.directory_nodes = directory_nodes
            deduplicated_offers.append(offer)
        deduplicated_offers.extend(offers_without_bond)

        # Group offers by (counterparty, oid) to merge across directories
        # Track all directory_nodes that announced each offer for statistics
        # NOTE: Bond offers already have directory_nodes populated from bond deduplication
        offer_key_to_offer: dict[tuple[str, int], Offer] = {}
        for offer in deduplicated_offers:
            key = (offer.counterparty, offer.oid)
            if key not in offer_key_to_offer:
                # First time seeing this offer
                # Preserve existing directory_nodes (from bond deduplication) or initialize
                if not offer.directory_nodes and offer.directory_node:
                    offer.directory_nodes = [offer.directory_node]
                offer_key_to_offer[key] = offer
            else:
                # Duplicate from another directory - merge directory_nodes
                existing_offer = offer_key_to_offer[key]
                if (
                    offer.directory_node
                    and offer.directory_node not in existing_offer.directory_nodes
                ):
                    existing_offer.directory_nodes.append(offer.directory_node)
                # Merge features from multiple directories
                for feature, value in offer.features.items():
                    if value and not existing_offer.features.get(feature):
                        existing_offer.features[feature] = value

        # Add deduplicated offers to orderbook
        for offer in offer_key_to_offer.values():
            orderbook.offers.append(offer)
            # Track all unique directory nodes in the orderbook
            for node in offer.directory_nodes:
                if node not in orderbook.directory_nodes:
                    orderbook.directory_nodes.append(node)

        # Deduplicate bonds by UTXO key while tracking all directories that announced them
        unique_bonds: dict[str, FidelityBond] = {}
        for bond in orderbook.fidelity_bonds:
            cache_key = f"{bond.utxo_txid}:{bond.utxo_vout}"
            if cache_key not in unique_bonds:
                # First time seeing this bond - initialize directory_nodes
                if bond.directory_node:
                    bond.directory_nodes = [bond.directory_node]
                unique_bonds[cache_key] = bond
            else:
                # Same bond from different directory - merge directory_nodes
                existing_bond = unique_bonds[cache_key]
                if bond.directory_node and bond.directory_node not in existing_bond.directory_nodes:
                    existing_bond.directory_nodes.append(bond.directory_node)
        orderbook.fidelity_bonds = list(unique_bonds.values())

        if calculate_bonds:
            cached_count = 0
            for bond in orderbook.fidelity_bonds:
                cache_key = f"{bond.utxo_txid}:{bond.utxo_vout}"
                if cache_key in self._bond_cache:
                    cached_bond = self._bond_cache[cache_key]
                    bond.bond_value = cached_bond.bond_value
                    bond.amount = cached_bond.amount
                    bond.utxo_confirmation_timestamp = cached_bond.utxo_confirmation_timestamp
                    cached_count += 1

            if cached_count > 0:
                logger.debug(
                    f"Loaded {cached_count}/{len(orderbook.fidelity_bonds)} bonds from cache"
                )

            await self._calculate_bond_values(orderbook)

            for bond in orderbook.fidelity_bonds:
                if bond.bond_value is not None:
                    cache_key = f"{bond.utxo_txid}:{bond.utxo_vout}"
                    self._bond_cache[cache_key] = bond

            # Link fidelity bonds to offers
            # First pass: Link bonds that are already attached to offers (via fidelity_bond_data)
            for offer in orderbook.offers:
                if offer.fidelity_bond_data:
                    matching_bonds = [
                        b
                        for b in orderbook.fidelity_bonds
                        if b.counterparty == offer.counterparty
                        and b.utxo_txid == offer.fidelity_bond_data.get("utxo_txid")
                    ]
                    if matching_bonds and matching_bonds[0].bond_value is not None:
                        offer.fidelity_bond_value = matching_bonds[0].bond_value

            # Second pass: Link standalone bonds to offers that don't have bond data yet
            # This handles cases where the bond announcement arrived separately from the offer
            # (e.g., reference implementation makers responding to !orderbook requests)
            for offer in orderbook.offers:
                if not offer.fidelity_bond_data:
                    # Find any bonds from this counterparty
                    matching_bonds = [
                        b for b in orderbook.fidelity_bonds if b.counterparty == offer.counterparty
                    ]
                    if matching_bonds:
                        # Use the bond with highest value (or first if values not calculated)
                        bond = max(
                            matching_bonds,
                            key=lambda b: b.bond_value if b.bond_value is not None else 0,
                        )
                        # Attach bond data to offer
                        if bond.fidelity_bond_data:
                            offer.fidelity_bond_data = bond.fidelity_bond_data
                            if bond.bond_value is not None:
                                offer.fidelity_bond_value = bond.bond_value
                        logger.debug(
                            f"Linked standalone bond from {bond.counterparty} "
                            f"(txid={bond.utxo_txid[:16]}...) to offer oid={offer.oid}"
                        )

        # Populate bond directory_nodes from linked offers
        # Bonds are counted in all directories where:
        # 1. They were directly announced (already in directory_nodes from deduplication)
        # 2. Their associated offers appeared (merged here)
        for bond in orderbook.fidelity_bonds:
            # Find all offers from this counterparty and collect their directory_nodes
            maker_offers = [o for o in orderbook.offers if o.counterparty == bond.counterparty]
            if maker_offers:
                # Merge offer directory_nodes with bond's existing directory_nodes
                all_directories: set[str] = set(bond.directory_nodes)  # Start with bond's own
                for offer in maker_offers:
                    all_directories.update(offer.directory_nodes)
                bond.directory_nodes = sorted(all_directories)
            # else: No offers, keep bond's directory_nodes from deduplication (if any)

        # Populate direct reachability and features from health check cache
        # This provides valuable information about whether makers are reachable beyond
        # what the directory server reports, and extracts features from handshake
        for offer in orderbook.offers:
            # Try to find the maker's location from any directory client
            location = None
            for client in self.clients.values():
                location = client._active_peers.get(offer.counterparty)
                if location and location != "NOT-SERVING-ONION":
                    break

            if location and location != "NOT-SERVING-ONION":
                # Check if we have health status for this location
                health_status = self.health_checker.health_status.get(location)
                if health_status:
                    offer.directly_reachable = health_status.reachable
                    # Merge features from handshake if available
                    # Health check provides authoritative features from direct connection
                    if health_status.features:
                        # Merge health check features with existing features
                        # Health check features take precedence (most recent/direct)
                        health_features = health_status.features.to_dict()
                        for feature, value in health_features.items():
                            if value:  # Only merge true features
                                offer.features[feature] = value

        return orderbook

    async def _calculate_bond_value_single(
        self, bond: FidelityBond, current_time: int
    ) -> FidelityBond:
        if bond.bond_value is not None:
            return bond

        if self.mempool_api is None:
            return bond

        async with self._mempool_semaphore:
            try:
                tx_data = await self.mempool_api.get_transaction(bond.utxo_txid)
                if not tx_data or not tx_data.status.confirmed:
                    logger.debug(f"Bond {bond.utxo_txid}:{bond.utxo_vout} not confirmed")
                    return bond

                if bond.utxo_vout >= len(tx_data.vout):
                    logger.warning(
                        f"Invalid vout {bond.utxo_vout} for tx {bond.utxo_txid} "
                        f"(only {len(tx_data.vout)} outputs)"
                    )
                    return bond

                utxo = tx_data.vout[bond.utxo_vout]
                amount = utxo.value
                confirmation_time = tx_data.status.block_time or current_time

                bond_value = calculate_timelocked_fidelity_bond_value(
                    amount, confirmation_time, bond.locktime, current_time
                )

                bond.bond_value = bond_value
                bond.amount = amount
                bond.utxo_confirmation_timestamp = confirmation_time

                logger.debug(
                    f"Bond {bond.counterparty}: value={bond_value}, "
                    f"amount={amount}, locktime={datetime.utcfromtimestamp(bond.locktime)}, "
                    f"confirmed={datetime.utcfromtimestamp(confirmation_time)}"
                )

            except Exception as e:
                logger.error(f"Failed to calculate bond value for {bond.utxo_txid}: {e}")
                logger.debug(
                    f"Bond data: txid={bond.utxo_txid}, vout={bond.utxo_vout}, amount={bond.amount}"
                )

        return bond

    async def _calculate_bond_values(self, orderbook: OrderBook) -> None:
        """Calculate bond values, using blockchain backend if available, else mempool API."""
        if self.blockchain_backend is not None:
            await self._calculate_bond_values_via_backend(orderbook)
        else:
            await self._calculate_bond_values_via_mempool(orderbook)

    async def _calculate_bond_values_via_backend(self, orderbook: OrderBook) -> None:
        """Calculate bond values using the blockchain backend's verify_bonds().

        This path is used when a full node or neutrino backend is configured,
        providing trustless and private bond verification without external API calls.
        """
        current_time = int(datetime.now(UTC).timestamp())

        # Build verification requests from bonds that need values
        bonds_to_verify: list[tuple[FidelityBond, BondVerificationRequest]] = []

        for bond in orderbook.fidelity_bonds:
            if bond.bond_value is not None:
                continue

            # Get utxo_pub and locktime from bond data
            bond_data = bond.fidelity_bond_data
            utxo_pub_hex: str | None = None
            locktime: int = bond.locktime

            if bond_data:
                utxo_pub_hex = bond_data.get("utxo_pub")

            if not utxo_pub_hex and bond.script:
                # Fallback: the `script` field stores utxo_pub hex
                utxo_pub_hex = bond.script

            if not utxo_pub_hex:
                logger.debug(f"Bond {bond.utxo_txid}:{bond.utxo_vout} missing utxo_pub, skipping")
                continue

            try:
                utxo_pub_bytes = bytes.fromhex(utxo_pub_hex)
                bond_addr = derive_bond_address(utxo_pub_bytes, locktime, self.network)
            except Exception as e:
                logger.debug(
                    f"Failed to derive bond address for {bond.utxo_txid}:{bond.utxo_vout}: {e}"
                )
                continue

            request = BondVerificationRequest(
                txid=bond.utxo_txid,
                vout=bond.utxo_vout,
                utxo_pub=utxo_pub_bytes,
                locktime=locktime,
                address=bond_addr.address,
                scriptpubkey=bond_addr.scriptpubkey.hex(),
            )
            bonds_to_verify.append((bond, request))

        if not bonds_to_verify:
            return

        logger.info(f"Verifying {len(bonds_to_verify)} bonds via blockchain backend...")

        try:
            assert self.blockchain_backend is not None
            results = await self.blockchain_backend.verify_bonds(
                [req for _, req in bonds_to_verify]
            )
        except Exception as e:
            logger.error(f"Backend bond verification failed: {e}")
            return

        # Update bond objects with results
        for (bond, _request), result in zip(bonds_to_verify, results, strict=True):
            if not result.valid:
                logger.debug(f"Bond {bond.utxo_txid}:{bond.utxo_vout} invalid: {result.error}")
                continue

            bond_value = calculate_timelocked_fidelity_bond_value(
                result.value, result.block_time, bond.locktime, current_time
            )

            bond.bond_value = bond_value
            bond.amount = result.value
            bond.utxo_confirmation_timestamp = result.block_time
            bond.utxo_confirmations = result.confirmations

            logger.debug(
                f"Bond {bond.counterparty}: value={bond_value}, "
                f"amount={result.value}, locktime={datetime.utcfromtimestamp(bond.locktime)}, "
                f"confirmed={datetime.utcfromtimestamp(result.block_time)}"
            )

        valid_count = sum(1 for r in results if r.valid)
        logger.info(f"Bond verification complete: {valid_count}/{len(bonds_to_verify)} verified")

    async def _calculate_bond_values_via_mempool(self, orderbook: OrderBook) -> None:
        """Calculate bond values via mempool API (legacy path)."""
        if self.mempool_api is None:
            logger.debug("Skipping mempool bond calculation (mempool API disabled)")
            return

        current_time = int(datetime.now(UTC).timestamp())

        tasks = [
            self._calculate_bond_value_single(bond, current_time)
            for bond in orderbook.fidelity_bonds
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _test_socks_connection(self) -> None:
        """Test SOCKS proxy connection on startup."""
        if self.mempool_api is None:
            return

        try:
            success = await self.mempool_api.test_connection()
            if success:
                logger.info("SOCKS proxy connection test successful")
            else:
                logger.warning(
                    "SOCKS proxy connection test failed - bond value calculation may not work"
                )
        except Exception as e:
            logger.error(f"SOCKS proxy connection test error: {e}")
            logger.warning("Bond value calculation may not work without SOCKS proxy")
