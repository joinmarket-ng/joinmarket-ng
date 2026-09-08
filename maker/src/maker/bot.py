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
from collections.abc import Callable
from pathlib import Path

from jmcore.commitment_blacklist import set_blacklist_path
from jmcore.crypto import NickIdentity
from jmcore.deduplication import MessageDeduplicator
from jmcore.directory_client import DirectoryClient
from jmcore.fee_policy import resolve_min_fee_rate
from jmcore.models import Offer
from jmcore.network import HiddenServiceListener, TCPConnection
from jmcore.notifications import get_notifier
from jmcore.paths import read_nick_state
from jmcore.protocol import (
    JM_VERSION,
)
from jmcore.randomness import secure_random
from jmcore.rate_limiter import RateLimiter
from jmcore.tasks import parse_directory_address, spawn_task
from jmcore.tor_control import (
    EphemeralHiddenService,
    TorAuthenticationError,
    TorControlClient,
    TorControlError,
)
from jmcore.version import get_build_ref, get_commit_hash, get_version
from jmwallet.backends.base import BlockchainBackend
from jmwallet.history import get_coinjoin_lineage_outpoints, get_pending_transactions
from jmwallet.wallet.models import UTXOInfo
from jmwallet.wallet.service import WalletService
from loguru import logger

from maker.background_tasks import BackgroundTasksMixin
from maker.config import MakerConfig
from maker.direct_connection import DirectConnectionMixin, DirectConnectionState
from maker.directory_pool import MakerDirectoryPool
from maker.fidelity import (
    ExpiredFidelityBondCertificateError,
    FidelityBondInfo,
    create_fidelity_bond_proof,
    ensure_fidelity_bond_certificate_valid,
    find_fidelity_bonds,
    get_best_fidelity_bond,
)
from maker.generation import GenerationState, MakerGeneration
from maker.maker_session import MakerSession, PendingSignedRound
from maker.offers import OfferManager
from maker.protocol_handlers import ProtocolHandlersMixin
from maker.rate_limiting import (
    DEFAULT_DIRECT_ORDERBOOK_RESPONSE_BURST,
    DEFAULT_DIRECT_ORDERBOOK_RESPONSE_REFILL_PER_SECOND,
    DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_BURST,
    DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_REFILL_PER_SECOND,
    DEFAULT_HP2_ADMISSION_BURST,
    DEFAULT_HP2_ADMISSION_REFILL_PER_SECOND,
    DEFAULT_HP2_RELAY_WORK_BURST,
    DEFAULT_HP2_RELAY_WORK_REFILL_PER_SECOND,
    DirectConnectionRateLimiter,
    OrderbookRateLimiter,
    ProcessWideTokenBucket,
)

# Approximately 64MB of memory for str->float mapping (including overhead)
MAX_LOG_RATE_LIMIT_ENTRIES = 200000
_DETACHED_SHUTDOWN_GRACE_SEC = 0.5
MIN_FEE_POLICY_TTL_SEC = 60.0


def _get_fidelity_bond_linkable_utxos(
    wallet: WalletService,
    data_dir: Path,
) -> list[UTXOInfo]:
    """Return spendable md0 coins that could be linked to an advertised bond."""
    md0_utxos = wallet.get_all_utxos(0, include_fidelity_bonds=False)
    coinjoin_lineage = get_coinjoin_lineage_outpoints(
        md0_utxos,
        network=wallet.network,
        data_dir=data_dir,
        wallet_fingerprint=wallet.wallet_fingerprint,
    )
    return [utxo for utxo in md0_utxos if utxo.outpoint not in coinjoin_lineage]


class MakerBot(BackgroundTasksMixin, ProtocolHandlersMixin, DirectConnectionMixin):
    """
    Main maker bot coordinating all components.
    """

    def __init__(
        self,
        wallet: WalletService,
        backend: BlockchainBackend,
        config: MakerConfig,
        nick_change_callback: Callable[[str, str], None] | None = None,
    ):
        self.wallet = wallet
        self.backend = backend
        self.config = config
        self._nick_change_callback = nick_change_callback
        self._static_onion_configured = config.onion_host is not None
        self.minimum_fee_rate_sat_vb = config.min_fee_rate_sat_vb
        self._minimum_fee_policy_resolved_at: float | None = None
        self._minimum_fee_policy_lock = asyncio.Lock()
        self._minimum_fee_policy_warning_emitted = False

        # Create nick identity for signing messages
        self.nick_identity = NickIdentity(JM_VERSION)
        self.nick = self.nick_identity.nick

        self.directory_clients: dict[str, DirectoryClient] = {}
        # Shared connection plumbing (parsing, SOCKS isolation creds,
        # DirectoryClient construction, retry loop). The pool stores its
        # connected clients in self.directory_clients via the shared-dict
        # binding below, so tests and existing call sites that mutate
        # self.directory_clients directly continue to work unchanged.
        self._directory_pool = MakerDirectoryPool(
            config=config,
            nick_identity=self.nick_identity,
            neutrino_compat=backend.can_provide_neutrino_metadata(),
        )
        self._directory_pool.clients = self.directory_clients
        self.offer_manager = OfferManager(self.wallet, config, self.nick)
        self.active_sessions: dict[tuple[int, str], MakerSession] = {}
        self._reserved_commitments: set[str] = set()
        self._active_podle_outpoints: dict[tuple[str, int], MakerSession] = {}
        self.current_offers: list[Offer] = []
        self.fidelity_bond: FidelityBondInfo | None = None
        self.current_block_height: int = 0  # Cached block height for bond proof generation

        self.running = False
        self.listen_tasks: list[asyncio.Task[None]] = []
        self._session_cleanup_task: asyncio.Task[None] | None = None
        self._session_handler_task_count = 0
        self._detached_handler_tasks: set[asyncio.Task[None]] = set()
        self._pending_signed_rounds: dict[tuple[int, str, str], PendingSignedRound] = {}
        self._pending_signed_rounds_lock = asyncio.Lock()
        self._fatal_error: Exception | None = None

        # Session locks now live on each `MakerSession` (one asyncio.Lock per
        # taker_nick) so we no longer keep a parallel dict on the bot.

        # Hidden service listener for direct peer connections
        self.hidden_service_listener: HiddenServiceListener | None = None
        self.direct_connections: dict[str, TCPConnection] = {}
        self._direct_connection_states: dict[TCPConnection, DirectConnectionState] = {}

        # Tor control for dynamic hidden service creation
        self._tor_control: TorControlClient | None = None
        self._ephemeral_hidden_service: EphemeralHiddenService | None = None
        self.generations: dict[int, MakerGeneration] = {}
        self.current_generation_id = 0
        self._generation_lock = asyncio.Lock()
        self._identity_renewal_task: asyncio.Task[None] | None = None

        # Generic per-peer rate limiter (token bucket algorithm)
        # Generous burst (100 msgs) but low sustained rate (10 msg/s)
        self._message_rate_limiter = RateLimiter(
            rate_limit=config.message_rate_limit,
            burst_limit=config.message_burst_limit,
        )

        # Fidelity bond addresses loaded at startup, kept for periodic rescans so
        # newly funded bonds are detected without requiring a restart.
        self._fidelity_bond_addresses: list[tuple[str, int, int]] = []

        # Rate limiter for orderbook requests to prevent spam attacks
        directory_sources = set()
        for server in config.directory_servers:
            try:
                host, port = parse_directory_address(server)
            except ValueError:
                # The directory pool reports invalid endpoints when connecting.
                continue
            directory_sources.add(f"dir:{host}:{port}")
        self._orderbook_rate_limiter = OrderbookRateLimiter(
            rate_limit=config.orderbook_rate_limit,
            interval=config.orderbook_rate_interval,
            violation_ban_threshold=config.orderbook_violation_ban_threshold,
            violation_warning_threshold=config.orderbook_violation_warning_threshold,
            violation_severe_threshold=config.orderbook_violation_severe_threshold,
            ban_duration=config.orderbook_ban_duration,
            directory_sources=frozenset(directory_sources),
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

        self._directory_orderbook_response_limiter = ProcessWideTokenBucket(
            DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_BURST,
            DEFAULT_DIRECTORY_ORDERBOOK_RESPONSE_REFILL_PER_SECOND,
        )
        self._direct_orderbook_response_limiter = ProcessWideTokenBucket(
            DEFAULT_DIRECT_ORDERBOOK_RESPONSE_BURST,
            DEFAULT_DIRECT_ORDERBOOK_RESPONSE_REFILL_PER_SECOND,
        )
        self._orderbook_stats_started_at = time.monotonic()
        self._orderbook_response_counts = {
            "directory_admitted": 0,
            "directory_suppressed": 0,
            "direct_admitted": 0,
            "direct_suppressed": 0,
        }
        self._hp2_admission_limiter = ProcessWideTokenBucket(
            DEFAULT_HP2_ADMISSION_BURST,
            DEFAULT_HP2_ADMISSION_REFILL_PER_SECOND,
        )
        self._hp2_relay_work_limiter = ProcessWideTokenBucket(
            DEFAULT_HP2_RELAY_WORK_BURST,
            DEFAULT_HP2_RELAY_WORK_REFILL_PER_SECOND,
        )

        # Message deduplicator to handle receiving same message from multiple directories
        # This prevents processing duplicates and avoids false rate limit violations
        self._message_deduplicator = MessageDeduplicator(window_seconds=30.0)

        self._hp2_own_broadcast_semaphore = asyncio.Semaphore(1)
        self._hp2_relay_broadcast_semaphore = asyncio.Semaphore(1)

        # Track failed directory reconnection attempts
        # Key: node_id (host:port), Value: number of reconnection attempts
        self._directory_reconnect_attempts: dict[str, int] = {}

        # Track whether all directories were previously disconnected, so we can
        # send a recovery notification when at least one reconnects
        self._all_directories_disconnected: bool = False

        # Track CoinJoin txids we have already sent a mempool notification for,
        # so we do not re-notify on every pending-confirmation poll cycle.
        self._mempool_notified_txids: set[str] = set()

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

        initial_generation = MakerGeneration(
            generation_id=0,
            nick_identity=self.nick_identity,
            offer_manager=self.offer_manager,
            directory_pool=self._directory_pool,
            directory_clients=self.directory_clients,
            current_offers=self.current_offers,
            direct_connections=self.direct_connections,
            direct_connection_states=self._direct_connection_states,
            reconnect_attempts=self._directory_reconnect_attempts,
        )
        self.generations[0] = initial_generation

    def _generation(self, generation_id: int | None = None) -> MakerGeneration | None:
        """Resolve an explicit generation, defaulting only for legacy callers."""
        return self.generations.get(
            self.current_generation_id if generation_id is None else generation_id
        )

    def _generation_clients(self, generation_id: int) -> dict[str, DirectoryClient]:
        generation = self._generation(generation_id)
        if generation is None:
            return {}
        # Existing embedders and tests may replace the current alias directly.
        return (
            self.directory_clients
            if generation_id == self.current_generation_id
            else generation.directory_clients
        )

    def _activate_generation(self, generation: MakerGeneration) -> None:
        """Switch compatibility aliases after a generation has become current."""
        self.current_generation_id = generation.generation_id
        self.nick_identity = generation.nick_identity
        self.nick = generation.nick_identity.nick
        self.offer_manager = generation.offer_manager
        self._directory_pool = generation.directory_pool
        self.directory_clients = generation.directory_clients
        self.current_offers = generation.current_offers
        self.hidden_service_listener = generation.hidden_service_listener
        self._tor_control = generation.tor_control
        self._ephemeral_hidden_service = generation.ephemeral_hidden_service
        self.direct_connections = generation.direct_connections
        self._direct_connection_states = generation.direct_connection_states
        self._directory_reconnect_attempts = generation.reconnect_attempts

    def _publish_nick_change(self, old_nick: str, new_nick: str) -> None:
        """Publish a completed identity cutover to the embedding process."""
        if self._nick_change_callback is None:
            return
        try:
            self._nick_change_callback(old_nick, new_nick)
        except Exception as exc:
            logger.warning(f"Could not publish maker nick change: {exc}")

    def _open_directory_outage(self) -> None:
        """Emit one critical notification for a process-wide directory outage."""
        if self._all_directories_disconnected:
            return
        self._all_directories_disconnected = True
        spawn_task(get_notifier().notify_all_directories_disconnected())

    def _resolve_directory_outage(self, connected_count: int) -> None:
        """Emit one recovery notification for an outstanding process-wide outage."""
        if connected_count <= 0 or not self._all_directories_disconnected:
            return
        self._all_directories_disconnected = False
        spawn_task(
            get_notifier().notify_all_directories_reconnected(
                connected_count, len(self.config.directory_servers)
            )
        )

    def _current_session_key(self, taker_nick: str) -> tuple[int, str]:
        return (self.current_generation_id, taker_nick)

    def _seed_mempool_notification_state(self) -> None:
        """Remember pending transactions inherited from a prior maker process."""
        pending_txids = {
            entry.txid
            for entry in get_pending_transactions(
                data_dir=self.config.data_dir,
                wallet_fingerprint=self.wallet.wallet_fingerprint,
            )
            if entry.txid
        }
        self._mempool_notified_txids.update(pending_txids)
        if pending_txids:
            logger.debug(
                f"Monitoring {len(pending_txids)} pre-existing pending transaction(s) "
                "without replaying mempool notifications"
            )

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

        # Retry on transient auth failures (e.g. cookie file not yet fully written by Tor)
        max_auth_retries = 5
        auth_retry_delay = 3.0
        last_auth_error: TorAuthenticationError | None = None
        for attempt in range(1, max_auth_retries + 1):
            try:
                return await self._try_setup_tor_hidden_service()
            except TorAuthenticationError as e:
                last_auth_error = e
                logger.warning(
                    f"Tor authentication failed (attempt {attempt}/{max_auth_retries}): {e} "
                    f"— retrying in {auth_retry_delay}s..."
                )
                await asyncio.sleep(auth_retry_delay)
            except TorControlError as e:
                # Non-auth errors are not retried — log and fall back gracefully
                logger.warning(
                    f"Could not create ephemeral hidden service via Tor control port: {e}\n"
                    f"  Tor control configured: "
                    f"{self.config.tor_control.host}:{self.config.tor_control.port}\n"
                    f"  Cookie path: {self.config.tor_control.cookie_path}\n"
                    f"  → Maker will advertise 'NOT-SERVING-ONION' and rely on directory routing."
                )
                return None

        # All retries exhausted — log warning and fall back to NOT-SERVING-ONION
        logger.warning(
            f"Could not authenticate to Tor control port after {max_auth_retries} attempts: "
            f"{last_auth_error}\n"
            f"  Tor control configured: "
            f"{self.config.tor_control.host}:{self.config.tor_control.port}\n"
            f"  Cookie path: {self.config.tor_control.cookie_path}\n"
            f"  → Maker will advertise 'NOT-SERVING-ONION' and rely on directory routing.\n"
            f"  → Ensure the Tor cookie file is readable and Tor has fully started."
        )
        return None

    async def _try_setup_tor_hidden_service(self) -> str | None:
        """
        Single attempt to create an ephemeral hidden service via Tor control port.
        Raises TorControlError (including TorAuthenticationError) on failure.
        """
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
                    # Ask Tor to omit the service private key from its response.
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

        except TorControlError:
            # Clean up partial connection before re-raising
            if self._tor_control:
                await self._tor_control.close()
                self._tor_control = None
            raise

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

    async def _initialize_minimum_fee_policy(self, *, announce: bool = True) -> None:
        """Resolve the fee floor once, or disclose that this backend cannot enforce it."""
        if not self.backend.can_lookup_arbitrary_utxos():
            if announce and not getattr(self, "_minimum_fee_policy_warning_emitted", False):
                logger.warning(
                    "Low-fee CoinJoin signing protection is unavailable because this backend "
                    "cannot look up arbitrary prevouts"
                )
                self._minimum_fee_policy_warning_emitted = True
            return
        # !fill refreshes this policy, so an unauthenticated peer can drive the
        # backend calls below. The floor tracks mempool conditions that move at
        # most once per block, so a recent value is reused instead.
        now = time.monotonic()
        resolved_at = self._minimum_fee_policy_resolved_at
        if resolved_at is not None and now - resolved_at < MIN_FEE_POLICY_TTL_SEC:
            return
        async with self._minimum_fee_policy_lock:
            now = time.monotonic()
            resolved_at = self._minimum_fee_policy_resolved_at
            if resolved_at is not None and now - resolved_at < MIN_FEE_POLICY_TTL_SEC:
                return
            self.minimum_fee_rate_sat_vb = await resolve_min_fee_rate(
                self.backend,
                static_floor=self.config.min_fee_rate_sat_vb,
                block_target=self.config.min_fee_block_target,
                max_fee_rate=self.config.max_fee_rate_sat_vb,
            )
            self._minimum_fee_policy_resolved_at = time.monotonic()
            if announce:
                logger.info(
                    "Resolved minimum CoinJoin miner fee rate: "
                    f"{self.minimum_fee_rate_sat_vb:.2f} sat/vB"
                )

    async def _create_replacement_generation(self) -> MakerGeneration | None:
        """Prepare independent identity, offer, directory, and Tor resources."""
        if self._static_onion_configured:
            logger.warning(
                "Skipping maker identity renewal because static onion services are not isolatable"
            )
            return None

        generation_id = max(self.generations, default=-1) + 1
        identity = NickIdentity(JM_VERSION)
        offer_manager = OfferManager(self.wallet, self.config, identity.nick)
        offers = await offer_manager.create_offers()
        listener: HiddenServiceListener | None = None
        tor_control: TorControlClient | None = None
        service: EphemeralHiddenService | None = None
        directory_pool: MakerDirectoryPool | None = None
        try:
            onion_host: str | None = None
            listener_port: int | None = None
            if self.config.tor_control.enabled:
                listener = HiddenServiceListener(
                    host=self.config.onion_serving_host,
                    port=0,
                    on_connection=lambda connection, peer: self._on_direct_connection(
                        connection, peer, generation_id=generation_id
                    ),
                )
                await listener.start()
                listener_port = listener.bound_port
                tor_control = TorControlClient(
                    control_host=self.config.tor_control.host,
                    control_port=self.config.tor_control.port,
                    cookie_path=self.config.tor_control.cookie_path,
                    password=(
                        self.config.tor_control.password.get_secret_value()
                        if self.config.tor_control.password
                        else None
                    ),
                )
                await tor_control.connect()
                await tor_control.authenticate()
                service = await tor_control.create_ephemeral_hidden_service(
                    ports=[
                        (
                            self.config.onion_serving_port,
                            f"{self.config.tor_target_host}:{listener.bound_port}",
                        )
                    ],
                    discard_pk=True,
                    detach=False,
                    max_streams=self.config.hidden_service_dos.max_streams,
                    dos_config=self.config.hidden_service_dos,
                )
                onion_host = service.onion_address

            directory_pool = MakerDirectoryPool(
                config=self.config,
                nick_identity=identity,
                neutrino_compat=self.backend.can_provide_neutrino_metadata(),
                onion_host=onion_host,
                # This is the peer-visible virtual onion port. Tor maps it
                # independently to listener_port above.
                onion_serving_port=self.config.onion_serving_port,
            )
            generation = MakerGeneration(
                generation_id=generation_id,
                nick_identity=identity,
                offer_manager=offer_manager,
                directory_pool=directory_pool,
                current_offers=offers,
                hidden_service_listener=listener,
                tor_control=tor_control,
                ephemeral_hidden_service=service,
                onion_host=onion_host,
                listener_port=listener_port,
            )
            directory_pool.clients = generation.directory_clients
            return generation
        except Exception:
            if directory_pool is not None:
                await directory_pool.close_all()
            if service is not None and tor_control is not None:
                try:
                    await tor_control.delete_ephemeral_hidden_service(service.service_id)
                except Exception:
                    pass
            if tor_control is not None:
                try:
                    await tor_control.close()
                except Exception:
                    pass
            if listener is not None:
                await listener.stop()
            raise

    async def _close_generation(self, generation: MakerGeneration) -> None:
        """Close only resources owned by one retired generation."""
        if generation.state is GenerationState.CLOSED:
            return
        generation.state = GenerationState.CLOSED
        current_task = asyncio.current_task()
        tasks = [task for task in generation.tasks if task is not current_task]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(set(tasks), timeout=2.0)
        direct_states = (
            self._direct_connection_states
            if generation.generation_id == self.current_generation_id
            else generation.direct_connection_states
        )
        direct_connections = (
            self.direct_connections
            if generation.generation_id == self.current_generation_id
            else generation.direct_connections
        )
        for connection in set(direct_states) | set(direct_connections.values()):
            try:
                await connection.close()
            except Exception:
                pass
        direct_connections.clear()
        direct_states.clear()
        if generation.hidden_service_listener is not None:
            await generation.hidden_service_listener.stop()
        if generation.ephemeral_hidden_service is not None and generation.tor_control is not None:
            try:
                await generation.tor_control.delete_ephemeral_hidden_service(
                    generation.ephemeral_hidden_service.service_id
                )
            except Exception as exc:
                logger.warning(f"Failed to remove retired ephemeral hidden service: {exc}")
        if generation.tor_control is not None:
            try:
                await generation.tor_control.close()
            except Exception:
                pass
        clients = list(generation.directory_clients.values())
        generation.directory_clients.clear()
        for client in clients:
            try:
                await client.close()
            except Exception:
                pass

    async def _retire_generation_after_grace(self, generation_id: int, deadline: float) -> None:
        await asyncio.sleep(max(0.0, deadline - time.monotonic()))
        generation = self.generations.get(generation_id)
        if (
            generation is None
            or generation.grace_deadline != deadline
            or generation_id == self.current_generation_id
        ):
            return
        for key, session in list(self.active_sessions.items()):
            if key[0] == generation_id:
                await self._expire_timed_out_session(key, session)
        async with self._pending_signed_rounds_lock:
            pending_keys = [
                pending_key
                for pending_key in self._pending_signed_rounds
                if pending_key[0] == generation_id
            ]
            for pending_key in pending_keys:
                self._pending_signed_rounds.pop(pending_key, None)
        await self._close_generation(generation)
        self.generations.pop(generation_id, None)

    async def _rollback_generation_rotation(
        self, old: MakerGeneration, replacement: MakerGeneration
    ) -> None:
        """Discard a replacement and restore the current generation's serving state."""
        await self._close_generation(replacement)
        if not self.running:
            return

        async with self._generation_lock:
            current = self._generation()
            if old.state is GenerationState.CLOSED or (current is not None and current is not old):
                return
            if self.generations.get(old.generation_id) is not old:
                self.generations[old.generation_id] = old
            self._activate_generation(old)
            old.state = GenerationState.ACCEPTING
            old.grace_deadline = None

        listener = old.hidden_service_listener
        if listener is not None and not listener.running:
            try:
                if old.listener_port is not None:
                    listener.port = old.listener_port
                await listener.start()
            except Exception as exc:
                logger.warning(f"Failed to restore maker listener after identity rollback: {exc}")

        if old.directory_clients:
            return
        self._open_directory_outage()
        try:
            connected = await old.directory_pool.connect_all_with_retry(
                timeout=self.config.directory_startup_timeout,
                initial_delay=5.0,
                max_delay=30.0,
                backoff=1.5,
            )
        except Exception as exc:
            logger.warning(f"Failed to restore maker directories after identity rollback: {exc}")
            return
        if connected > 0:
            await self._announce_generation_offers(old)
            self._start_generation_listeners(old)
            self._resolve_directory_outage(len(old.directory_clients))

    async def _rotate_generation(self) -> bool:
        """Retire one identity, wait quietly, then publish its replacement."""
        replacement = await self._create_replacement_generation()
        if replacement is None:
            return False
        async with self._generation_lock:
            old = self._generation()
            if old is None or not self.running:
                await self._close_generation(replacement)
                return False
            old.state = GenerationState.GRACE
            old.grace_deadline = time.monotonic() + max(
                self.config.identity_grace_sec, self.config.session_timeout_sec
            )
            if old.hidden_service_listener is not None:
                # Stop admitting new direct sockets. Existing accepted socket
                # handlers remain alive for generation-pinned continuations.
                await old.hidden_service_listener.stop()
            deadline = old.grace_deadline

        try:
            await self._wait_for_generation_sessions(old.generation_id, deadline)
            await self._close_generation_directory_clients(old)
            quiet_delay = secure_random.uniform(
                self.config.identity_rotation_quiet_min_sec,
                self.config.identity_rotation_quiet_max_sec,
            )
            if quiet_delay > 0:
                await asyncio.sleep(quiet_delay)

            connected = await replacement.directory_pool.connect_all_with_retry(
                timeout=self.config.directory_startup_timeout,
                initial_delay=5.0,
                max_delay=30.0,
                backoff=1.5,
            )
            if connected <= 0:
                raise RuntimeError("Replacement generation could not connect to a directory")
        except asyncio.CancelledError:
            await asyncio.shield(self._rollback_generation_rotation(old, replacement))
            raise
        except Exception as exc:
            logger.warning("Maker identity rotation failed, restoring previous identity")
            logger.bind(sensitive=True).debug(f"Maker identity rotation failure: {exc}")
            await self._rollback_generation_rotation(old, replacement)
            return False

        async with self._generation_lock:
            if not self.running or self._generation() is not old:
                await self._close_generation(replacement)
                return False
            self.generations[replacement.generation_id] = replacement
            self._activate_generation(replacement)
            self._publish_nick_change(old.nick_identity.nick, replacement.nick_identity.nick)
            await self._announce_generation_offers(replacement)
            self._start_generation_listeners(replacement)
            self._resolve_directory_outage(len(replacement.directory_clients))
            logger.bind(sensitive=True).info(
                f"Maker identity generation cut over: {old.nick_identity.nick} -> "
                f"{replacement.nick_identity.nick}"
            )
            try:
                spawn_task(
                    get_notifier().notify_nick_change(
                        old.nick_identity.nick, replacement.nick_identity.nick
                    )
                )
            except Exception as exc:
                logger.warning(f"Could not schedule maker nick-change notification: {exc}")
            retirement_task = asyncio.create_task(
                self._retire_generation_after_grace(old.generation_id, deadline),
                name=f"maker-generation-retire-{old.generation_id}",
            )
            old.tasks.append(retirement_task)
        return True

    async def _wait_for_generation_sessions(self, generation_id: int, deadline: float) -> None:
        """Keep retired directory routes until generation-pinned work finishes."""
        while self.running and time.monotonic() < deadline:
            has_sessions = any(key[0] == generation_id for key in self.active_sessions)
            async with self._pending_signed_rounds_lock:
                has_pending = any(key[0] == generation_id for key in self._pending_signed_rounds)
            if not has_sessions and not has_pending:
                return
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    async def _close_generation_directory_clients(self, generation: MakerGeneration) -> None:
        """Silently retire directory presence through normal TCP disconnects."""
        clients = list(generation.directory_clients.values())
        generation.directory_clients.clear()
        for client in clients:
            try:
                await client.close()
            except Exception as exc:
                logger.bind(sensitive=True).debug(
                    f"Failed to close retired generation directory client: {exc}"
                )

    async def _identity_renewal_scheduler(self) -> None:
        """Renew on one secure random delay per cycle, independent of activity."""
        while self.running:
            delay = secure_random.uniform(
                self.config.identity_renewal_min_sec, self.config.identity_renewal_max_sec
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            if self.running:
                try:
                    await self._rotate_generation()
                except Exception as exc:
                    logger.warning(f"Maker identity renewal failed: {exc}")

    def _start_generation_listeners(self, generation: MakerGeneration) -> None:
        for node_id, client in generation.directory_clients.items():
            task = asyncio.create_task(
                self._listen_client(node_id, client, generation_id=generation.generation_id)
            )
            generation.tasks.append(task)
            self.listen_tasks.append(task)
        if generation.hidden_service_listener is not None:
            task = asyncio.create_task(generation.hidden_service_listener.serve_forever())
            generation.tasks.append(task)
            self.listen_tasks.append(task)

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
            version = get_version()
            commit = get_commit_hash() or "unknown"
            build_ref = get_build_ref() or "unknown"
            logger.info(f"Starting maker bot (version={version}, commit={commit}, ref={build_ref})")
            logger.bind(sensitive=True).info(f"Starting maker bot (nick: {self.nick})")

            await self._initialize_minimum_fee_policy()

            # Pending history survives restarts. Treat transactions inherited
            # from a prior process as already notified so startup monitoring
            # does not replay their mempool notifications.
            self._seed_mempool_notification_state()

            # Log wallet name if using descriptor wallet backend
            from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

            if isinstance(self.backend, DescriptorWalletBackend):
                logger.info("Using descriptor wallet backend")
                logger.bind(sensitive=True).info(f"Using wallet: {self.backend.wallet_name}")

            # Initialize commitment blacklist with configured data directory
            set_blacklist_path(data_dir=self.config.data_dir)

            # Load fidelity bond addresses for optimized scanning
            # We scan wallet + fidelity bonds in a single pass to avoid extra
            # backend round-trips during sync.
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
                    f"locktimes={self.config.fidelity_bond_locktimes}"
                )
                for locktime in self.config.fidelity_bond_locktimes:
                    from jmcore.timenumber import timestamp_to_timenumber

                    timenumber = timestamp_to_timenumber(locktime)
                    address = self.wallet.get_fidelity_bond_address(timenumber, locktime)
                    fidelity_bond_addresses.append((address, locktime, timenumber))
                    logger.bind(sensitive=True).info(
                        f"Generated fidelity bond address for locktime {locktime}: {address}"
                    )
            # Option 2: Load from registry (default)
            else:
                bond_registry = load_registry(
                    resolved_data_dir,
                    self.wallet.wallet_fingerprint,
                    allow_legacy_fallback=False,
                )
                # Registry entries describe the Bitcoin address network. The
                # protocol network can intentionally differ (reference clients
                # use "testnet" messaging while tests settle on regtest).
                network_bonds = [
                    bond for bond in bond_registry.bonds if bond.network == self.wallet.network
                ]
                if network_bonds:
                    # Extract (address, locktime, index) tuples from registry
                    fidelity_bond_addresses = [
                        (bond.address, bond.locktime, bond.index) for bond in network_bonds
                    ]
                    logger.info(
                        f"Loaded {len(fidelity_bond_addresses)} "
                        f"fidelity bond address(es) from registry"
                    )

            logger.info("Syncing wallet and fidelity bonds...")

            # Store bond addresses on the instance so periodic rescans can use them
            # to detect newly funded bonds without requiring a restart.
            self._fidelity_bond_addresses = fidelity_bond_addresses

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
                # Use standard sync (BIP157/158 for neutrino, mempool API, etc.)
                await self.wallet.sync_all(fidelity_bond_addresses)
            await self.wallet.reconstruct_imported_state_safe()

            # Update bond registry with UTXO info from the scan (only if using registry)
            if self.config.fidelity_bond_index is None and fidelity_bond_addresses:
                from jmwallet.wallet.bond_registry import save_registry

                bond_registry = load_registry(
                    resolved_data_dir,
                    self.wallet.wallet_fingerprint,
                    allow_legacy_fallback=False,
                )
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
                        logger.bind(sensitive=True).debug(
                            f"Updated bond {bond.address[:20]}... with UTXO "
                            f"{bond_utxo.txid[:16]}...:{bond_utxo.vout}, value={bond_utxo.value}"
                        )

                # Save updated registry
                save_registry(bond_registry, resolved_data_dir, self.wallet.wallet_fingerprint)

            # Get current block height for bond proof generation
            self.current_block_height = await self.backend.get_block_height()
            logger.debug(f"Current block height: {self.current_block_height}")

            total_balance = await self.wallet.get_total_balance()
            logger.bind(sensitive=True).info(
                f"Wallet synced. Total balance: {total_balance:,} sats"
            )

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
                    logger.bind(sensitive=True).info(
                        f"Using selected fidelity bond: {sel_txid[:16]}...:{sel_vout}, "
                        f"value={self.fidelity_bond.value:,} sats, "
                        f"bond_value={self.fidelity_bond.bond_value:,}"
                    )
                else:
                    logger.warning(
                        "Selected fidelity bond not found, falling back to best available"
                    )
                    logger.bind(sensitive=True).warning(
                        f"Selected fidelity bond {sel_txid[:16]}...:{sel_vout} not found, "
                        "falling back to best available"
                    )
                    self.fidelity_bond = await get_best_fidelity_bond(
                        self.wallet, current_block_height=self.current_block_height
                    )
            else:
                # Auto-select the best (largest bond value) fidelity bond
                self.fidelity_bond = await get_best_fidelity_bond(
                    self.wallet, current_block_height=self.current_block_height
                )
            if self.fidelity_bond:
                ensure_fidelity_bond_certificate_valid(
                    self.fidelity_bond,
                    self.current_block_height,
                )
                logger.bind(sensitive=True).info(
                    f"Fidelity bond found: {self.fidelity_bond.txid[:16]}..., "
                    f"value={self.fidelity_bond.value:,} sats, "
                    f"bond_value={self.fidelity_bond.bond_value:,}"
                )
                md0_utxos = _get_fidelity_bond_linkable_utxos(
                    self.wallet,
                    resolved_data_dir,
                )
                if md0_utxos:
                    total_md0 = sum(u.value for u in md0_utxos)
                    logger.warning(
                        "PRIVACY RISK: regular mixdepth 0 UTXOs can be linked to your fidelity bond"
                    )
                    logger.bind(sensitive=True).warning(
                        f"PRIVACY RISK: You have a fidelity bond AND "
                        f"{len(md0_utxos)} regular UTXO(s) ({total_md0:,} sats) "
                        f"in mixdepth 0.\n"
                        f"Using md0 UTXOs in coinjoins can link your identity "
                        f"to your fidelity bond.\n"
                        f"Recommendation: sweep md0 funds to mixdepth 1 as a "
                        f"taker coinjoin, then freeze or spend the md0 UTXOs."
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
                await self.wallet.reconstruct_imported_state_safe()
                total_balance = await self.wallet.get_total_balance()
                logger.bind(sensitive=True).info(
                    f"Wallet re-synced. Total balance: {total_balance:,} sats"
                )

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
            initial_generation = self.generations[0]
            ephemeral_onion = await self._setup_tor_hidden_service()
            initial_generation.tor_control = self._tor_control
            initial_generation.ephemeral_hidden_service = self._ephemeral_hidden_service
            if ephemeral_onion:
                # Override onion_host with the dynamically created one
                object.__setattr__(self.config, "onion_host", ephemeral_onion)
                logger.info("Using an ephemeral onion service")
                logger.bind(sensitive=True).info(
                    f"Using ephemeral onion address: {ephemeral_onion}"
                )

            # Determine the onion address to advertise
            onion_host = self.config.onion_host

            logger.info("Connecting to directory servers...")
            await self._connect_to_directories_with_retry()

            # Start hidden service listener if we have an onion address (static or ephemeral)
            if onion_host:
                logger.info("Starting hidden service listener")
                logger.bind(sensitive=True).info(
                    f"Starting hidden service listener on "
                    f"{self.config.onion_serving_host}:{self.config.onion_serving_port}..."
                )
                self.hidden_service_listener = HiddenServiceListener(
                    host=self.config.onion_serving_host,
                    port=self.config.onion_serving_port,
                    on_connection=lambda connection, peer: self._on_direct_connection(
                        connection, peer, generation_id=0
                    ),
                )
                await self.hidden_service_listener.start()
                initial_generation.hidden_service_listener = self.hidden_service_listener
                initial_generation.listener_port = self.hidden_service_listener.bound_port
                logger.info("Hidden service listener started")
                logger.bind(sensitive=True).info(
                    f"Hidden service listener started (onion: {onion_host})"
                )

            logger.info("Announcing offers...")
            await self._announce_offers()

            initial_generation.current_offers = self.current_offers
            initial_generation.onion_host = onion_host

            logger.info("Maker bot started. Listening for takers...")
            self.running = True

            self._start_session_cleanup_task()

            # Start listening on all directory clients
            self._start_generation_listeners(initial_generation)

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

            self._identity_renewal_task = asyncio.create_task(
                self._identity_renewal_scheduler(), name="maker-identity-renewal"
            )
            self.listen_tasks.append(self._identity_renewal_task)

            # Start periodic summary notification task (if enabled)
            notifier = get_notifier()
            if notifier.config.notify_summary:
                summary_task = asyncio.create_task(self._periodic_summary())
                self.listen_tasks.append(summary_task)
            else:
                logger.info("Periodic summary notifications disabled (notify_summary=false)")

            # Wait for all listening tasks to complete
            await asyncio.gather(*self.listen_tasks, return_exceptions=True)
            if self._fatal_error is not None:
                raise self._fatal_error

        except ExpiredFidelityBondCertificateError:
            raise
        except Exception as e:
            logger.error("Failed to start maker bot")
            logger.bind(sensitive=True).error(f"Failed to start maker bot: {e}")
            raise

    async def stop(self) -> None:
        """Stop the maker bot"""
        logger.info("Stopping maker bot...")
        self.running = False
        if self._identity_renewal_task is not None:
            self._identity_renewal_task.cancel()

        # Stop the dedicated reaper, then independently expire every exact
        # session object. Handler cancellation and cleanup are bounded inside
        # _expire_timed_out_session, including cancellation-suppressing tasks.
        cleanup_task = self._session_cleanup_task
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.wait({cleanup_task}, timeout=1.0)

        expiration_tasks = [
            asyncio.create_task(self._expire_timed_out_session(key, session))
            for key, session in list(self.active_sessions.items())
        ]
        if expiration_tasks:
            _, pending_expirations = await asyncio.wait(expiration_tasks, timeout=1.5)
            for expiration_task in pending_expirations:
                expiration_task.cancel()

        await self._drain_pending_signed_rounds()

        # Cancellation-suppressing handlers remain strongly referenced until
        # they actually finish. Shutdown sends another cancellation request and
        # waits only for a bounded grace period.
        detached_tasks = set(self._detached_handler_tasks)
        for detached_task in detached_tasks:
            detached_task.cancel()
        if detached_tasks:
            _, pending_detached = await asyncio.wait(
                detached_tasks, timeout=_DETACHED_SHUTDOWN_GRACE_SEC
            )
            if pending_detached:
                logger.warning(
                    f"{len(pending_detached)} detached maker handler task(s) "
                    "still suppress cancellation after shutdown grace period"
                )

        # Cancel all listening tasks after detached handlers have released
        # their directory dispatchers.
        for listener_task in self.listen_tasks:
            listener_task.cancel()

        if self.listen_tasks:
            _, pending_listeners = await asyncio.wait(set(self.listen_tasks), timeout=2.0)
            for listener_task in pending_listeners:
                listener_task.cancel()
        self.listen_tasks.clear()
        self._session_cleanup_task = None

        # Close every generation. The compatibility aliases point at the
        # current record, so generation-owned cleanup also handles replacement
        # transports that are not present in those aliases.
        for generation in list(self.generations.values()):
            await self._close_generation(generation)
        self.generations.clear()

        # Do not close the wallet here as it might be shared (e.g. in jmwalletd)
        # The caller is responsible for managing the wallet lifecycle.
        # await self.wallet.close()
        logger.info("Maker bot stopped")

    def _start_session_cleanup_task(self) -> None:
        """Start the bot-wide session reaper exactly once."""
        if self._session_cleanup_task is not None and not self._session_cleanup_task.done():
            return
        self._session_cleanup_task = asyncio.create_task(
            self._periodic_session_cleanup(), name="maker-session-cleanup"
        )
        self.listen_tasks.append(self._session_cleanup_task)

    def _abort_for_fatal_error(self, error: Exception) -> None:
        """Stop listener tasks so ``start`` can propagate a fatal background error."""
        if self._fatal_error is None:
            self._fatal_error = error
        self.running = False

        current_task = asyncio.current_task()
        for listener_task in self.listen_tasks:
            if listener_task is not current_task:
                listener_task.cancel()

    def _log_rate_limited(
        self, key: str, message: str, level: str = "warning", interval: float = 10.0
    ) -> None:
        """
        Logs a message with rate limiting to prevent log spam.
        """
        now = time.time()

        # Enforce maximum size to prevent memory leak
        if (
            key not in self._rate_limited_log_times
            and len(self._rate_limited_log_times) >= MAX_LOG_RATE_LIMIT_ENTRIES
        ):
            # Remove the oldest entry (dictionaries preserve insertion order in Python 3.7+)
            try:
                oldest_key = next(iter(self._rate_limited_log_times))
                del self._rate_limited_log_times[oldest_key]
            except (StopIteration, KeyError):
                pass

        last_time = self._rate_limited_log_times.get(key, 0.0)
        if now - last_time >= interval:
            # Use getattr to call the correct logger method (e.g., logger.warning, logger.info)
            log_method = getattr(logger, level, logger.warning)
            log_method(message)
            self._rate_limited_log_times[key] = now

    async def _cleanup_timed_out_sessions(self) -> None:
        """Remove timed-out sessions from active_sessions and clean up rate limiter."""
        for sid, session in list(self.active_sessions.items()):
            if session.is_timed_out():
                await self._expire_timed_out_session(sid, session)

        await self._prune_pending_signed_rounds()

        # Ensure rate limiters are cleaned up periodically
        self._direct_connection_rate_limiter.cleanup_old_entries()

    def _release_commitment_reservation(self, commitment: str) -> None:
        """Release a commitment reserved by an in-flight local session."""
        self._reserved_commitments.discard(commitment.lower())

    def _reserve_podle_outpoint(self, outpoint: tuple[str, int], session: MakerSession) -> bool:
        """Reserve one validated PoDLE UTXO for a single local session."""
        owner = self._active_podle_outpoints.get(outpoint)
        if owner is not None and owner is not session:
            return False
        self._active_podle_outpoints[outpoint] = session
        session.podle_outpoint = outpoint
        return True

    def _release_podle_outpoint(self, session: MakerSession) -> None:
        """Release a PoDLE UTXO reservation if this session still owns it."""
        outpoint = session.podle_outpoint
        if outpoint is None:
            return
        if self._active_podle_outpoints.get(outpoint) is session:
            self._active_podle_outpoints.pop(outpoint, None)
        session.podle_outpoint = None

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
        locked_outpoints = self.wallet.get_locked_input_outpoints()
        restrict_md0 = not self.config.allow_mixdepth_zero_merge
        md0_mergeable_outpoints = (
            await self.wallet.get_maker_rotation_lineage_outpoints() if restrict_md0 else None
        )
        for mixdepth in range(self.wallet.mixdepth_count):
            balance = await self.wallet.get_balance_for_offers(
                mixdepth,
                min_confirmations=self.config.min_confirmations,
                restrict_md0=restrict_md0,
                md0_mergeable_outpoints=md0_mergeable_outpoints,
                exclude=locked_outpoints,
            )
            old_max_balance = max(old_max_balance, balance)

        # Sync wallet (use descriptor wallet if available for fast sync)
        from jmwallet.backends.descriptor_wallet import DescriptorWalletBackend

        if isinstance(self.backend, DescriptorWalletBackend):
            await self.wallet.sync_with_descriptor_wallet(self._fidelity_bond_addresses)
        else:
            await self.wallet.sync_all(self._fidelity_bond_addresses)
        await self.wallet.reconstruct_imported_state_safe()

        # Update current block height
        self.current_block_height = await self.backend.get_block_height()
        logger.debug(f"Updated block height: {self.current_block_height}")
        if self.fidelity_bond is not None:
            ensure_fidelity_bond_certificate_valid(
                self.fidelity_bond,
                self.current_block_height,
            )

        # Update pending history immediately after sync (in case of restart)
        await self._update_pending_history()

        # Get new max balance for offers after resync (excludes fidelity bonds)
        new_max_balance = 0
        locked_outpoints = self.wallet.get_locked_input_outpoints()
        md0_mergeable_outpoints = (
            await self.wallet.get_maker_rotation_lineage_outpoints() if restrict_md0 else None
        )
        for mixdepth in range(self.wallet.mixdepth_count):
            balance = await self.wallet.get_balance_for_offers(
                mixdepth,
                min_confirmations=self.config.min_confirmations,
                restrict_md0=restrict_md0,
                md0_mergeable_outpoints=md0_mergeable_outpoints,
                exclude=locked_outpoints,
            )
            new_max_balance = max(new_max_balance, balance)

        total_balance = await self.wallet.get_total_balance()

        # If max balance changed, update offers and log at INFO so operators
        # see balance/offer churn. When unchanged this rescan is a routine
        # no-op (every `rescan_interval_sec`, default 10 min) and is logged
        # at DEBUG to avoid flooding logs of long-running makers.
        if old_max_balance != new_max_balance:
            logger.info("Wallet balance changed, updating offers")
            logger.bind(sensitive=True).info(
                f"Wallet re-synced. Total balance: {total_balance:,} sats"
            )
            logger.bind(sensitive=True).info(
                f"Max balance changed: {old_max_balance:,} -> {new_max_balance:,} sats. "
                "Updating offers..."
            )
            await self._update_offers()
        else:
            logger.bind(sensitive=True).debug(
                f"Wallet re-synced (no change). Total balance: {total_balance:,} sats, "
                f"max offer balance: {new_max_balance:,} sats"
            )

    async def _update_offers(self) -> None:
        """Recreate and re-announce offers based on current wallet state.

        Called when wallet balance changes (after CoinJoin, external transaction,
        or deposit). This allows the maker to adapt to changing balances without
        requiring a restart.
        """
        try:
            new_offers = await self.offer_manager.create_offers()

            if self.current_offers == new_offers:
                logger.debug("Offers unchanged, skipping re-announcement")
                return

            old_oids = {offer.oid for offer in self.current_offers}
            new_oids = {offer.oid for offer in new_offers}
            canceled_oids = old_oids - new_oids

            # Regenerate nick when offers change for additional privacy
            # This makes it harder for observers to track maker activity over time
            await self._regenerate_nick()

            # Update offers with new nick (OfferManager.maker_nick was updated by _regenerate_nick)
            for offer in new_offers:
                offer.counterparty = self.nick

            self.current_offers = new_offers
            current_generation = self._generation()
            if current_generation is not None:
                current_generation.current_offers = new_offers

            delay_max = self.config.offer_reannounce_delay_max
            if delay_max > 0:
                delay = secure_random.uniform(0, delay_max)
                logger.info(
                    f"Delaying offer re-announcement by {delay:.0f}s (max {delay_max}s) for privacy"
                )
                await asyncio.sleep(delay)

            await self._cancel_offers(canceled_oids)
            if new_offers:
                await self._announce_offers()
                offer_summary = ", ".join(f"oid={o.oid}:{o.maxsize:,}" for o in new_offers)
                logger.info(f"Updated and re-announced {len(new_offers)} offer(s): {offer_summary}")
            else:
                logger.warning("Withdrew all offers because no fillable liquidity remains")
        except Exception as e:
            logger.error("Failed to update offers")
            logger.bind(sensitive=True).error(f"Failed to update offers: {e}")

    async def _cancel_offers(self, offer_ids: set[int]) -> None:
        """Withdraw OIDs using the reference-compatible public cancel command."""
        generation = self._generation()
        if generation is not None:
            await self._cancel_generation_offers(generation, offer_ids)

    async def _cancel_generation_offers(
        self, generation: MakerGeneration, offer_ids: set[int]
    ) -> None:
        """Withdraw OIDs using the clients that announced this generation."""
        for offer_id in sorted(offer_ids):
            for client in self._generation_clients(generation.generation_id).values():
                try:
                    await client.send_public_message(f"cancel {offer_id}")
                    logger.debug(f"Canceled offer {offer_id} on directory")
                except Exception as e:
                    logger.error(f"Failed to cancel offer {offer_id}: {e}")

    async def _announce_offers(self) -> None:
        """Announce offers to all connected directory servers (public broadcast, NO bonds)"""
        generation = self._generation()
        if generation is not None:
            generation.current_offers = self.current_offers
            await self._announce_generation_offers(generation)

    async def _announce_generation_offers(self, generation: MakerGeneration) -> None:
        """Announce offers using only the identity that owns them."""
        for offer in generation.current_offers:
            offer_msg = self._format_offer_announcement(offer, include_bond=False)

            for client in generation.directory_clients.values():
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
