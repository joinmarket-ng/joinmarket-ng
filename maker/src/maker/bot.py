"""
Main maker bot implementation.

Coordinates all maker components:
- Wallet synchronization
- Directory server connections
- Offer creation and announcement
- CoinJoin protocol handling
"""

from __future__ import annotations

import asyncio
import time

from jmcore.commitment_blacklist import set_blacklist_path
from jmcore.crypto import NickIdentity
from jmcore.deduplication import MessageDeduplicator
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer
from jmcore.network import HiddenServiceListener, TCPConnection
from jmcore.notifications import get_notifier
from jmcore.paths import read_nick_state
from jmcore.protocol import (
    JM_VERSION,
)
from jmcore.rate_limiter import RateLimiter
from jmcore.tasks import parse_directory_address
from jmcore.tor_control import (
    EphemeralHiddenService,
    TorControlClient,
    TorControlError,
)
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService
from loguru import logger

from maker.background_tasks import BackgroundTasksMixin
from maker.coinjoin import CoinJoinSession
from maker.config import MakerConfig
from maker.direct_connection import DirectConnectionMixin
from maker.fidelity import (
    FidelityBondInfo,
    create_fidelity_bond_proof,
    find_fidelity_bonds,
    get_best_fidelity_bond,
)
from maker.offers import OfferManager
from maker.protocol_handlers import ProtocolHandlersMixin
from maker.rate_limiting import (
    DirectConnectionRateLimiter,
    OrderbookRateLimiter,
)


class MakerBot(BackgroundTasksMixin, ProtocolHandlersMixin, DirectConnectionMixin):
    """
    Main maker bot coordinating all components.
    """

    def __init__(
        self,
        wallet: WalletService,
        backend: BlockchainBackend,
        config: MakerConfig,
    ):
        self.wallet = wallet
        self.backend = backend
        self.config = config

        # Create nick identity for signing messages
        self.nick_identity = NickIdentity(JM_VERSION)
        self.nick = self.nick_identity.nick

        self.offer_manager = OfferManager(self.wallet, config, self.nick)

        self.directory_clients: dict[str, DirectoryClient] = {}
        self.active_sessions: dict[str, CoinJoinSession] = {}
        self.current_offers: list[Offer] = []
        self.fidelity_bond: FidelityBondInfo | None = None
        self.current_block_height: int = 0  # Cached block height for bond proof generation

        self.running = False
        self.listen_tasks: list[asyncio.Task[None]] = []

        # Lock to prevent concurrent processing of the same session
        # Key: taker_nick, Value: asyncio.Lock
        # This prevents race conditions when duplicate messages arrive via multiple
        # directory servers or direct connections
        self._session_locks: dict[str, asyncio.Lock] = {}

        # Hidden service listener for direct peer connections
        self.hidden_service_listener: HiddenServiceListener | None = None
        self.direct_connections: dict[str, TCPConnection] = {}

        # Tor control for dynamic hidden service creation
        self._tor_control: TorControlClient | None = None
        self._ephemeral_hidden_service: EphemeralHiddenService | None = None

        # Generic per-peer rate limiter (token bucket algorithm)
        # Generous burst (100 msgs) but low sustained rate (10 msg/s)
        self._message_rate_limiter = RateLimiter(
            rate_limit=config.message_rate_limit,
            burst_limit=config.message_burst_limit,
        )

        # Rate limiter for orderbook requests to prevent spam attacks
        self._orderbook_rate_limiter = OrderbookRateLimiter(
            rate_limit=config.orderbook_rate_limit,
            interval=config.orderbook_rate_interval,
            violation_ban_threshold=config.orderbook_violation_ban_threshold,
            violation_warning_threshold=config.orderbook_violation_warning_threshold,
            violation_severe_threshold=config.orderbook_violation_severe_threshold,
            ban_duration=config.orderbook_ban_duration,
        )

        # Rate limiter specifically for direct hidden service connections
        # This tracks by connection address (not nick) to prevent nick rotation attacks
        # where attackers use a different nick per request
        self._direct_connection_rate_limiter = DirectConnectionRateLimiter(
            message_rate_per_sec=5.0,  # Stricter than directory (5 msg/s vs 10)
            message_burst=20,  # Smaller burst
            orderbook_interval=30.0,  # Longer interval (30s vs 10s)
            orderbook_ban_threshold=10,  # Faster ban (10 violations vs 100)
            ban_duration=config.orderbook_ban_duration,
        )

        # Message deduplicator to handle receiving same message from multiple directories
        # This prevents processing duplicates and avoids false rate limit violations
        self._message_deduplicator = MessageDeduplicator(window_seconds=30.0)

        # Track failed directory reconnection attempts
        # Key: node_id (host:port), Value: number of reconnection attempts
        self._directory_reconnect_attempts: dict[str, int] = {}

        # Track last log time for rate-limited logging
        # Key: log_key, Value: timestamp of last log
        self._rate_limited_log_times: dict[str, float] = {}

        # Own wallet nicks to exclude from CoinJoin sessions (self-CoinJoin protection)
        # Read the taker nick from state file if running both components from same wallet
        self._own_wallet_nicks: set[str] = set()
        taker_nick = read_nick_state(config.data_dir, "taker")
        if taker_nick:
            self._own_wallet_nicks.add(taker_nick)
            logger.info(f"Self-CoinJoin protection: excluding taker nick {taker_nick}")

    async def _setup_tor_hidden_service(self) -> str | None:
        """
        Create an ephemeral hidden service via Tor control port.

        Also configures Tor-level DoS defenses (intro point rate limiting, PoW)
        based on the hidden_service_dos configuration.

        Returns:
            The .onion address if successful, None otherwise
        """
        if not self.config.tor_control.enabled:
            logger.debug("Tor control port integration disabled")
            return None

        try:
            logger.info(
                f"Connecting to Tor control port at "
                f"{self.config.tor_control.host}:{self.config.tor_control.port}..."
            )

            self._tor_control = TorControlClient(
                control_host=self.config.tor_control.host,
                control_port=self.config.tor_control.port,
                cookie_path=self.config.tor_control.cookie_path,
                password=self.config.tor_control.password.get_secret_value()
                if self.config.tor_control.password
                else None,
            )

            await self._tor_control.connect()
            await self._tor_control.authenticate()

            # Get Tor version and capabilities for logging and DoS defense setup
            try:
                tor_version = await self._tor_control.get_version()
                logger.info(f"Connected to Tor {tor_version}")
                caps = await self._tor_control.get_capabilities()
            except TorControlError:
                logger.debug("Could not get Tor version (non-critical)")
                caps = None

            # Create ephemeral hidden service
            # Maps external port (advertised) to our local serving port
            dos_config = self.config.hidden_service_dos
            logger.info(
                f"Creating ephemeral hidden service on port {self.config.onion_serving_port} -> "
                f"{self.config.tor_target_host}:{self.config.onion_serving_port}..."
            )

            self._ephemeral_hidden_service = (
                await self._tor_control.create_ephemeral_hidden_service(
                    ports=[
                        (
                            self.config.onion_serving_port,
                            f"{self.config.tor_target_host}:{self.config.onion_serving_port}",
                        )
                    ],
                    # Don't discard private key in case we want to log it for debugging
                    discard_pk=True,
                    # Don't detach - we want the service to be removed when we disconnect
                    detach=False,
                    # Apply max_streams limit if configured (DoS protection)
                    max_streams=dos_config.max_streams,
                    # Apply Tor-level DoS defenses (intro point rate limiting, PoW)
                    # These must be set at creation time for ephemeral hidden services
                    dos_config=dos_config,
                )
            )

            logger.info(
                f"Created ephemeral hidden service: {self._ephemeral_hidden_service.onion_address}"
            )

            # Log summary of active defenses (only those actually applied to ephemeral HS)
            defenses = []
            # Note: intro_dos is NOT supported for ephemeral HS, don't list it as active
            # Note: PoW via ADD_ONION requires Tor 0.4.9.2+
            if dos_config.pow_enabled and caps and caps.has_add_onion_pow:
                defenses.append("PoW=enabled")
            if dos_config.max_streams:
                defenses.append(f"max_streams={dos_config.max_streams}")
            if defenses:
                logger.info(f"Tor DoS defenses active: {', '.join(defenses)}")
            else:
                logger.info(
                    "No Tor-level DoS defenses active for ephemeral HS "
                    "(requires Tor 0.4.9.2+ for PoW, or use persistent HS in torrc)"
                )

            return self._ephemeral_hidden_service.onion_address

        except TorControlError as e:
            logger.warning(
                f"Could not create ephemeral hidden service via Tor control port: {e}\n"
                f"  Tor control configured: "
                f"{self.config.tor_control.host}:{self.config.tor_control.port}\n"
                f"  Cookie path: {self.config.tor_control.cookie_path}\n"
                f"  → Maker will advertise 'NOT-SERVING-ONION' and rely on directory routing.\n"
                f"  → For better privacy, ensure Tor is running with control port enabled:\n"
                f"     ControlPort {self.config.tor_control.port}\n"
                f"     CookieAuthentication 1"
            )
            # Clean up partial connection
            if self._tor_control:
                await self._tor_control.close()
                self._tor_control = None
            return None

    async def _cleanup_tor_hidden_service(self) -> None:
        """Clean up Tor control connection (hidden service is auto-removed)."""
        if self._tor_control:
            try:
                await self._tor_control.close()
                logger.debug("Closed Tor control connection")
            except Exception as e:
                logger.warning(f"Error closing Tor control connection: {e}")
            self._tor_control = None
            self._ephemeral_hidden_service = None

    async def _regenerate_nick(self) -> None:
        """
        Regenerate nick identity for privacy (currently disabled).

        Nick regeneration is disabled because:
        1. Reference implementation doesn't regenerate nicks after CoinJoin
        2. Fidelity bond makers need stable identity for reputation
        3. Causes timing issues with !push (taker waits ~60s to collect signatures)
        4. Privacy is maintained through Tor hidden services

        Future consideration: Could be re-enabled as opt-in feature with grace period.
        """
        pass

    async def start(self) -> None:
        """
        Start the maker bot.

        Flow:
        1. Initialize commitment blacklist
        2. Sync wallet with blockchain
        3. Create ephemeral hidden service if tor_control enabled
        4. Connect to directory servers
        5. Create and announce offers
        6. Listen for taker requests
        """
        try:
            logger.info(f"Starting maker bot (nick: {self.nick})")

            # Log wallet name if using descriptor wallet backend
            from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

            if isinstance(self.backend, DescriptorWalletBackend):
                logger.info(f"Using wallet: {self.backend.wallet_name}")

            # Initialize commitment blacklist with configured data directory
            set_blacklist_path(data_dir=self.config.data_dir)

            # Load fidelity bond addresses for optimized scanning
            # We scan wallet + fidelity bonds in a single pass to avoid two separate
            # scantxoutset calls (which take ~90s each on mainnet)
            from jmcore.paths import get_default_data_dir
            from jmwallet.wallet.bond_registry import load_registry

            resolved_data_dir = (
                self.config.data_dir if self.config.data_dir else get_default_data_dir()
            )
            fidelity_bond_addresses: list[tuple[str, int, int]] = []

            # Fidelity bonds are explicitly disabled
            if self.config.no_fidelity_bond:
                logger.info(
                    "Fidelity bonds disabled (--no-fidelity-bond). Running without bond proof."
                )
            # Option 1: Manual specification via fidelity_bond_index + locktimes (bypasses registry)
            # This is useful when running in Docker or when you don't have a registry yet
            elif (
                self.config.fidelity_bond_index is not None and self.config.fidelity_bond_locktimes
            ):
                logger.info(
                    f"Using manual fidelity bond specification: "
                    f"index={self.config.fidelity_bond_index}, "
                    f"locktimes={self.config.fidelity_bond_locktimes}"
                )
                for locktime in self.config.fidelity_bond_locktimes:
                    address = self.wallet.get_fidelity_bond_address(
                        self.config.fidelity_bond_index, locktime
                    )
                    fidelity_bond_addresses.append(
                        (address, locktime, self.config.fidelity_bond_index)
                    )
                    logger.info(
                        f"Generated fidelity bond address for locktime {locktime}: {address}"
                    )
            # Option 2: Load from registry (default)
            else:
                bond_registry = load_registry(resolved_data_dir)
                if bond_registry.bonds:
                    # Extract (address, locktime, index) tuples from registry
                    fidelity_bond_addresses = [
                        (bond.address, bond.locktime, bond.index) for bond in bond_registry.bonds
                    ]
                    logger.info(
                        f"Loaded {len(fidelity_bond_addresses)} "
                        f"fidelity bond address(es) from registry"
                    )

            logger.info("Syncing wallet and fidelity bonds...")

            # Setup descriptor wallet if needed (one-time operation)
            if isinstance(self.backend, DescriptorWalletBackend):
                # Check if base wallet is set up (without counting bonds)
                base_wallet_ready = await self.wallet.is_descriptor_wallet_ready(
                    fidelity_bond_count=0
                )
                # Check if wallet with bonds is set up
                full_wallet_ready = await self.wallet.is_descriptor_wallet_ready(
                    fidelity_bond_count=len(fidelity_bond_addresses)
                )

                if not base_wallet_ready:
                    # First time setup - import everything including bonds
                    logger.info("Descriptor wallet not set up. Importing descriptors...")
                    await self.wallet.setup_descriptor_wallet(
                        rescan=True,
                        fidelity_bond_addresses=fidelity_bond_addresses,
                    )
                    logger.info("Descriptor wallet setup complete")
                elif not full_wallet_ready and fidelity_bond_addresses:
                    # Base wallet exists but bonds are missing - import just the bonds
                    logger.info(
                        "Descriptor wallet exists but fidelity bond addresses not imported. "
                        "Importing bond addresses..."
                    )
                    await self.wallet.import_fidelity_bond_addresses(
                        fidelity_bond_addresses, rescan=True
                    )

                # Use fast descriptor wallet sync
                await self.wallet.sync_with_descriptor_wallet(fidelity_bond_addresses)
            else:
                # Use standard sync (scantxoutset for scantxoutset, BIP157/158 for neutrino)
                await self.wallet.sync_all(fidelity_bond_addresses)

            # Update bond registry with UTXO info from the scan (only if using registry)
            if self.config.fidelity_bond_index is None and fidelity_bond_addresses:
                from jmwallet.wallet.bond_registry import save_registry

                bond_registry = load_registry(resolved_data_dir)
                for bond in bond_registry.bonds:
                    # Find the UTXO for this bond address in mixdepth 0
                    bond_utxo = next(
                        (
                            utxo
                            for utxo in self.wallet.utxo_cache.get(0, [])
                            if utxo.address == bond.address
                        ),
                        None,
                    )
                    if bond_utxo:
                        # Update the bond registry with UTXO info
                        bond.txid = bond_utxo.txid
                        bond.vout = bond_utxo.vout
                        bond.value = bond_utxo.value
                        bond.confirmations = bond_utxo.confirmations
                        logger.debug(
                            f"Updated bond {bond.address[:20]}... with UTXO "
                            f"{bond_utxo.txid[:16]}...:{bond_utxo.vout}, value={bond_utxo.value}"
                        )

                # Save updated registry
                save_registry(bond_registry, resolved_data_dir)

            # Get current block height for bond proof generation
            self.current_block_height = await self.backend.get_block_height()
            logger.debug(f"Current block height: {self.current_block_height}")

            total_balance = await self.wallet.get_total_balance()
            logger.info(f"Wallet synced. Total balance: {total_balance:,} sats")

            # Find fidelity bond for proof generation
            # If a specific bond is selected in config, use it; otherwise use the best one
            if self.config.no_fidelity_bond:
                self.fidelity_bond = None
                logger.warning("Fidelity bond disabled (offers will have no bond proof)")
            elif self.config.selected_fidelity_bond:
                # User specified a specific bond
                sel_txid, sel_vout = self.config.selected_fidelity_bond
                bonds = await find_fidelity_bonds(self.wallet)
                self.fidelity_bond = next(
                    (b for b in bonds if b.txid == sel_txid and b.vout == sel_vout), None
                )
                if self.fidelity_bond:
                    logger.info(
                        f"Using selected fidelity bond: {sel_txid[:16]}...:{sel_vout}, "
                        f"value={self.fidelity_bond.value:,} sats, "
                        f"bond_value={self.fidelity_bond.bond_value:,}"
                    )
                else:
                    logger.warning(
                        f"Selected fidelity bond {sel_txid[:16]}...:{sel_vout} not found, "
                        "falling back to best available"
                    )
                    self.fidelity_bond = await get_best_fidelity_bond(self.wallet)
            else:
                # Auto-select the best (largest bond value) fidelity bond
                self.fidelity_bond = await get_best_fidelity_bond(self.wallet)
            if self.fidelity_bond:
                logger.info(
                    f"Fidelity bond found: {self.fidelity_bond.txid[:16]}..., "
                    f"value={self.fidelity_bond.value:,} sats, "
                    f"bond_value={self.fidelity_bond.bond_value:,}"
                )
            else:
                logger.warning("No fidelity bond found (offers will have no bond proof)")

            logger.info("Creating offers...")
            self.current_offers = await self.offer_manager.create_offers()

            # If no offers due to insufficient balance, wait and retry
            retry_count = 0
            max_retries = 30  # 5 minutes max wait (30 * 10s)
            while not self.current_offers and retry_count < max_retries:
                retry_count += 1
                logger.warning(
                    f"No offers created (insufficient balance?). "
                    f"Waiting 10s and retrying... (attempt {retry_count}/{max_retries})"
                )
                await asyncio.sleep(10)

                # Re-sync wallet to check for new funds
                from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

                if isinstance(self.backend, DescriptorWalletBackend):
                    await self.wallet.sync_with_descriptor_wallet()
                else:
                    await self.wallet.sync_all()
                total_balance = await self.wallet.get_total_balance()
                logger.info(f"Wallet re-synced. Total balance: {total_balance:,} sats")

                self.current_offers = await self.offer_manager.create_offers()

            if not self.current_offers:
                logger.error(
                    f"No offers created after {max_retries} retries. "
                    "Please fund the wallet and restart."
                )
                return

            # Log summary of created offers
            logger.info(f"Created {len(self.current_offers)} offer(s) to announce:")
            for offer in self.current_offers:
                fee_display = (
                    f"{float(offer.cjfee) * 100:.4f}%"
                    if offer.ordertype.value.endswith("reloffer")
                    else f"{offer.cjfee} sats"
                )
                logger.info(
                    f"  oid={offer.oid}: {offer.ordertype.value}, "
                    f"size={offer.minsize:,}-{offer.maxsize:,} sats, fee={fee_display}"
                )

            # Set up ephemeral hidden service via Tor control port if enabled
            # This must happen before connecting to directory servers so we can
            # advertise the onion address
            ephemeral_onion = await self._setup_tor_hidden_service()
            if ephemeral_onion:
                # Override onion_host with the dynamically created one
                object.__setattr__(self.config, "onion_host", ephemeral_onion)
                logger.info(f"Using ephemeral onion address: {ephemeral_onion}")

            # Determine the onion address to advertise
            onion_host = self.config.onion_host

            logger.info("Connecting to directory servers...")
            for dir_server in self.config.directory_servers:
                try:
                    host, port = parse_directory_address(dir_server)

                    # Determine location for handshake:
                    # If we have an onion_host configured (static or ephemeral),
                    # advertise it with port
                    # Otherwise, use NOT-SERVING-ONION
                    if onion_host:
                        location = f"{onion_host}:{self.config.onion_serving_port}"
                    else:
                        location = "NOT-SERVING-ONION"

                    # Advertise neutrino_compat if our backend can provide extended UTXO metadata.
                    # This tells Neutrino takers that we can provide scriptpubkey and blockheight.
                    # Full nodes (Bitcoin Core) can provide this; light clients (Neutrino) cannot.
                    neutrino_compat = self.backend.can_provide_neutrino_metadata()

                    # Create DirectoryClient with SOCKS config for Tor connections
                    dir_username: str | None = None
                    dir_password: str | None = None
                    if self.config.stream_isolation:
                        from jmcore.tor_isolation import (
                            IsolationCategory,
                            get_isolation_credentials,
                        )

                        dir_creds = get_isolation_credentials(IsolationCategory.DIRECTORY)
                        dir_username = dir_creds.username
                        dir_password = dir_creds.password

                    client = DirectoryClient(
                        host=host,
                        port=port,
                        network=self.config.network.value,
                        nick_identity=self.nick_identity,
                        location=location,
                        socks_host=self.config.socks_host,
                        socks_port=self.config.socks_port,
                        timeout=self.config.connection_timeout,
                        neutrino_compat=neutrino_compat,
                        socks_username=dir_username,
                        socks_password=dir_password,
                    )

                    await client.connect()
                    node_id = f"{host}:{port}"
                    self.directory_clients[node_id] = client

                    logger.info(f"Connected to directory: {dir_server}")

                except Exception as e:
                    logger.error(f"Failed to connect to {dir_server}: {e}")

            if not self.directory_clients:
                logger.error("Failed to connect to any directory server")
                return

            # Start hidden service listener if we have an onion address (static or ephemeral)
            if onion_host:
                logger.info(
                    f"Starting hidden service listener on "
                    f"{self.config.onion_serving_host}:{self.config.onion_serving_port}..."
                )
                self.hidden_service_listener = HiddenServiceListener(
                    host=self.config.onion_serving_host,
                    port=self.config.onion_serving_port,
                    on_connection=self._on_direct_connection,
                )
                await self.hidden_service_listener.start()
                logger.info(f"Hidden service listener started (onion: {onion_host})")

            logger.info("Announcing offers...")
            await self._announce_offers()

            logger.info("Maker bot started. Listening for takers...")
            self.running = True

            # Start listening on all directory clients
            for node_id, client in self.directory_clients.items():
                task = asyncio.create_task(self._listen_client(node_id, client))
                self.listen_tasks.append(task)

            # If hidden service listener is running, start serve_forever task
            if self.hidden_service_listener:
                task = asyncio.create_task(self.hidden_service_listener.serve_forever())
                self.listen_tasks.append(task)

            # Start background task to monitor pending transactions
            monitor_task = asyncio.create_task(self._monitor_pending_transactions())
            self.listen_tasks.append(monitor_task)

            # Start periodic wallet rescan task
            rescan_task = asyncio.create_task(self._periodic_rescan())
            self.listen_tasks.append(rescan_task)

            # Start periodic rate limit status logging task
            status_task = asyncio.create_task(self._periodic_rate_limit_status())
            self.listen_tasks.append(status_task)

            # Start periodic directory connection status logging task
            conn_status_task = asyncio.create_task(self._periodic_directory_connection_status())
            self.listen_tasks.append(conn_status_task)

            # Start periodic directory reconnection task
            reconnect_task = asyncio.create_task(self._periodic_directory_reconnect())
            self.listen_tasks.append(reconnect_task)

            # Start periodic summary notification task (if enabled)
            notifier = get_notifier()
            if notifier.config.notify_summary:
                summary_task = asyncio.create_task(self._periodic_summary())
                self.listen_tasks.append(summary_task)
            else:
                logger.info("Periodic summary notifications disabled (notify_summary=false)")

            # Wait for all listening tasks to complete
            await asyncio.gather(*self.listen_tasks, return_exceptions=True)

        except Exception as e:
            logger.error(f"Failed to start maker bot: {e}")
            raise

    async def stop(self) -> None:
        """Stop the maker bot"""
        logger.info("Stopping maker bot...")
        self.running = False

        # Cancel all listening tasks
        for task in self.listen_tasks:
            task.cancel()

        if self.listen_tasks:
            await asyncio.gather(*self.listen_tasks, return_exceptions=True)

        # Stop hidden service listener
        if self.hidden_service_listener:
            await self.hidden_service_listener.stop()

        # Clean up Tor control connection (ephemeral hidden service auto-removed)
        await self._cleanup_tor_hidden_service()

        # Close all direct connections
        for conn in self.direct_connections.values():
            try:
                await conn.close()
            except Exception:
                pass
        self.direct_connections.clear()

        # Close all directory clients
        for client in self.directory_clients.values():
            try:
                await client.close()
            except Exception:
                pass

        await self.wallet.close()
        logger.info("Maker bot stopped")

    def _get_session_lock(self, taker_nick: str) -> asyncio.Lock:
        """Get or create a lock for a session to prevent concurrent processing."""
        if taker_nick not in self._session_locks:
            self._session_locks[taker_nick] = asyncio.Lock()
        return self._session_locks[taker_nick]

    def _cleanup_session_lock(self, taker_nick: str) -> None:
        """Clean up session lock when session is removed."""
        self._session_locks.pop(taker_nick, None)

    def _log_rate_limited(self, key: str, message: str, interval_sec: float = 10.0) -> None:
        """Log a warning message with rate limiting to avoid log spam.

        Args:
            key: Unique key for this log type (used for rate limiting)
            message: The warning message to log
            interval_sec: Minimum seconds between logs with the same key
        """
        now = time.time()
        last_log_time = self._rate_limited_log_times.get(key, 0.0)
        if now - last_log_time >= interval_sec:
            logger.warning(message)
            self._rate_limited_log_times[key] = now

    def _cleanup_timed_out_sessions(self) -> None:
        """Remove timed-out sessions from active_sessions and clean up rate limiter."""
        timed_out = [
            nick for nick, session in self.active_sessions.items() if session.is_timed_out()
        ]

        for nick in timed_out:
            session = self.active_sessions[nick]
            age = int(asyncio.get_event_loop().time() - session.created_at)
            logger.warning(
                f"Cleaning up timed-out session with {nick} (state: {session.state}, age: {age}s)"
            )
            del self.active_sessions[nick]
            self._cleanup_session_lock(nick)

        # Periodically cleanup old rate limiter entries to prevent memory growth
        self._orderbook_rate_limiter.cleanup_old_entries()

    async def _resync_wallet_and_update_offers(self) -> None:
        """Re-sync wallet and update offers if balance changed.

        This is the core rescan logic used by both post-CoinJoin resync
        and periodic rescan. It:
        1. Saves the current max balance
        2. Re-syncs the wallet
        3. If max balance changed, recreates and re-announces offers
        """
        # Get current max balance available for offers before resync (excludes fidelity bonds)
        old_max_balance = 0
        for mixdepth in range(self.wallet.mixdepth_count):
            balance = await self.wallet.get_balance_for_offers(
                mixdepth, min_confirmations=self.config.min_confirmations
            )
            old_max_balance = max(old_max_balance, balance)

        # Sync wallet (use descriptor wallet if available for fast sync)
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        if isinstance(self.backend, DescriptorWalletBackend):
            await self.wallet.sync_with_descriptor_wallet()
        else:
            await self.wallet.sync_all()

        # Update current block height
        self.current_block_height = await self.backend.get_block_height()
        logger.debug(f"Updated block height: {self.current_block_height}")

        # Update pending history immediately after sync (in case of restart)
        await self._update_pending_history()

        # Get new max balance for offers after resync (excludes fidelity bonds)
        new_max_balance = 0
        for mixdepth in range(self.wallet.mixdepth_count):
            balance = await self.wallet.get_balance_for_offers(
                mixdepth, min_confirmations=self.config.min_confirmations
            )
            new_max_balance = max(new_max_balance, balance)

        total_balance = await self.wallet.get_total_balance()
        logger.info(f"Wallet re-synced. Total balance: {total_balance:,} sats")

        # If max balance changed, update offers
        if old_max_balance != new_max_balance:
            logger.info(
                f"Max balance changed: {old_max_balance:,} -> {new_max_balance:,} sats. "
                "Updating offers..."
            )
            await self._update_offers()
        else:
            logger.debug(f"Max balance unchanged at {new_max_balance:,} sats")

    async def _update_offers(self) -> None:
        """Recreate and re-announce offers based on current wallet state.

        Called when wallet balance changes (after CoinJoin, external transaction,
        or deposit). This allows the maker to adapt to changing balances without
        requiring a restart.
        """
        try:
            new_offers = await self.offer_manager.create_offers()

            if not new_offers:
                logger.warning(
                    "No offers could be created (insufficient balance?). "
                    "Keeping existing offers active."
                )
                return

            # Check if offers actually changed (compare maxsize for each offer by ID)
            offers_changed = False
            if self.current_offers and new_offers:
                # Build lookup by offer ID for comparison
                old_offers_by_id = {o.oid: o for o in self.current_offers}
                new_offers_by_id = {o.oid: o for o in new_offers}

                # Check if offer count changed
                if set(old_offers_by_id.keys()) != set(new_offers_by_id.keys()):
                    offers_changed = True
                else:
                    # Check if any offer's maxsize changed
                    for oid, new_offer in new_offers_by_id.items():
                        old_offer = old_offers_by_id.get(oid)
                        if old_offer is None or old_offer.maxsize != new_offer.maxsize:
                            offers_changed = True
                            break

                if not offers_changed:
                    logger.debug("Offer maxsizes unchanged, skipping re-announcement")
                    return
            else:
                offers_changed = True  # First time or recovering from no offers

            # Regenerate nick when offers change for additional privacy
            # This makes it harder for observers to track maker activity over time
            await self._regenerate_nick()

            # Update offers with new nick (OfferManager.maker_nick was updated by _regenerate_nick)
            for offer in new_offers:
                offer.counterparty = self.nick

            self.current_offers = new_offers
            await self._announce_offers()
            offer_summary = ", ".join(f"oid={o.oid}:{o.maxsize:,}" for o in new_offers)
            logger.info(f"Updated and re-announced {len(new_offers)} offer(s): {offer_summary}")
        except Exception as e:
            logger.error(f"Failed to update offers: {e}")

    async def _announce_offers(self) -> None:
        """Announce offers to all connected directory servers (public broadcast, NO bonds)"""
        for offer in self.current_offers:
            offer_msg = self._format_offer_announcement(offer, include_bond=False)

            for client in self.directory_clients.values():
                try:
                    await client.send_public_message(offer_msg)
                    logger.debug("Announced offer to directory")
                except Exception as e:
                    logger.error(f"Failed to announce offer: {e}")

    def _format_offer_announcement(self, offer: Offer, include_bond: bool = False) -> str:
        """Format offer for announcement.

        Format: <ordertype> <oid> <minsize> <maxsize> <txfee> <cjfee>[!tbond <proof>]

        Args:
            offer: The offer to format
            include_bond: If True, append fidelity bond proof (for PRIVMSG only)

        Note:
            According to the JoinMarket protocol:
            - Public broadcasts: NO fidelity bond proof
            - Private responses to !orderbook: Include !tbond <proof>
        """

        order_type_str = offer.ordertype.value

        # NOTE: Don't include nick!PUBLIC! prefix here - send_public_message() adds it
        msg = (
            f"{order_type_str} "
            f"{offer.oid} {offer.minsize} {offer.maxsize} "
            f"{offer.txfee} {offer.cjfee}"
        )

        # Append fidelity bond proof ONLY for private responses
        if include_bond and self.fidelity_bond is not None:
            # For private response, we use the requesting taker's nick
            # The ownership signature proves we control the UTXO
            bond_proof = create_fidelity_bond_proof(
                bond=self.fidelity_bond,
                maker_nick=self.nick,
                taker_nick=self.nick,  # Will be updated when sending to specific taker
                current_block_height=self.current_block_height,
            )
            if bond_proof:
                msg += f"!tbond {bond_proof}"
                logger.debug(
                    f"Added fidelity bond proof to offer (proof length: {len(bond_proof)})"
                )

        return msg
