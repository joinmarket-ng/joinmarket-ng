"""Daemon state management.

The ``DaemonState`` class is the single source of truth for the running
daemon.  It holds the current wallet service, maker/taker state, auth
authority, config overrides, and WebSocket notification hub.

This is intentionally a plain class (not a Pydantic model) because it holds
runtime objects like WalletService that are not serialisable.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from jmcore.paths import get_default_data_dir
from jmcore.secure_files import ensure_private_directory
from jmwalletd.auth import JMTokenAuthority
from jmwalletd.errors import WalletLifecycleQueueFull
from jmwalletd.log_buffer import get_log_buffer

if TYPE_CHECKING:
    from jmcore.settings import JoinMarketSettings
    from jmcore.wallet_market import WalletMarketSellerOptions
    from taker.wallet_market import WalletMarketSeller


class CoinjoinState(enum.IntEnum):
    """Matches reference implementation's coinjoin state constants.

    ``TUMBLER_RUNNING`` is a jm-ng extension used while a :mod:`tumbler`
    plan is executing; it is distinct from ``TAKER_RUNNING`` so that direct
    single-shot taker runs and tumbler runs can be mutually excluded from
    one another without conflating the two.
    """

    TAKER_RUNNING = 0
    MAKER_RUNNING = 1
    NOT_RUNNING = 2
    TUMBLER_RUNNING = 3


class WebSocketControl(enum.Enum):
    """Control events consumed by a WebSocket writer."""

    CLOSE = enum.auto()


@dataclass(frozen=True)
class WebSocketNotification:
    """A notification bound to the wallet generation that created it."""

    generation: int
    text: str


@dataclass(eq=False)
class WebSocketClient:
    """A registered WebSocket client and its authorization state."""

    queue: asyncio.Queue[WebSocketNotification | WebSocketControl]
    generation: int | None = None


@dataclass
class UnlockFailure:
    """State for bounded password retry delays for one wallet file."""

    failures: int
    retry_after: float


_UNLOCK_BACKOFF_BASE_SECONDS = 1.0
_UNLOCK_BACKOFF_MAX_SECONDS = 30.0
_UNLOCK_BACKOFF_MAX_FAILURES = 6
_MAX_PREAUTH_WS_CLIENTS = 32
_MAX_WALLET_LIFECYCLE_OPERATIONS = 8


class WebSocketRegistrationLimit(Exception):
    """Raised when pending WebSocket authentications reach their cap."""


class DaemonState:
    """Mutable singleton holding all daemon runtime state.

    This is created once at app startup and injected into route handlers
    via FastAPI dependency injection.
    """

    def __init__(self, data_dir: Path | None = None) -> None:
        # Auth
        self.token_authority = JMTokenAuthority()

        # Wallet
        self.wallet_service: Any = None  # WalletService | None
        self.wallet_mnemonic: str = ""
        self.wallet_name: str = ""
        self.wallet_password: str = ""  # kept for re-unlock verification
        self.wallet_lifecycle_lock = asyncio.Lock()
        self._wallet_lifecycle_operations = 0
        self._unlock_failures: dict[str, UnlockFailure] = {}

        # Coinjoin state
        self.coinjoin_state = CoinjoinState.NOT_RUNNING
        self.maker_running: bool = False
        self.taker_running: bool = False
        self.offer_list: list[dict[str, str | int | float]] | None = None
        self.nickname: str | None = None
        self.last_broadcast_policy: str | None = None
        self.last_broadcast_method: str | None = None
        self.last_broadcast_fallback_reason: str | None = None

        # Runtime references to active taker/maker instances (for stop signals).
        self._taker_ref: Any = None
        self._maker_ref: Any = None

        # asyncio.Task handles for the background _run_maker / _run_taker coroutines.
        self._maker_task: asyncio.Task[None] | None = None
        self._taker_task: asyncio.Task[None] | None = None
        self._wallet_sync_task: asyncio.Task[None] | None = None
        self._market_seller_ref: WalletMarketSeller | None = None
        self._market_seller_task: asyncio.Task[None] | None = None
        self._market_seller_error: str | None = None

        # Background transaction monitor: pushes a WebSocket notification for
        # every wallet transaction (deposits, coinjoins, sends), not just
        # direct-send (issue #560). ``_tx_broadcast_notified`` records txids
        # whose first-seen (mempool) notification was already emitted -- by the
        # direct-send path or the monitor -- so the two never duplicate it.
        self._tx_monitor_task: asyncio.Task[None] | None = None
        self._tx_monitor_ready: asyncio.Event | None = None
        self._tx_broadcast_notified: set[str] = set()

        # Tumbler runtime. ``tumble_runner`` is a ``tumbler.runner.TumbleRunner``
        # and ``tumble_task`` is the task running ``runner.run()``. They are kept
        # as dedicated fields (rather than reusing ``_taker_ref`` / ``_taker_task``)
        # so that direct single-shot taker runs cannot be interfered with by the
        # tumbler router and vice versa. ``tumble_plan_wallet`` records which
        # wallet the currently running / pending plan belongs to; this is always
        # ``wallet_name`` while ``tumble_runner`` is set but is kept separately
        # so the router can surface the originating wallet even during a stop
        # race.
        self.tumble_runner: Any = None
        self.tumble_task: asyncio.Task[Any] | None = None
        self.tumble_plan_wallet: str | None = None

        # Rescan state. ``rescanning``/``rescan_progress`` are daemon-side
        # flags updated by the rescan endpoint and the background wallet sync;
        # ``live_rescan_status`` combines them with Bitcoin Core's own
        # ``getwalletinfo.scanning`` state, which is the source of truth.
        self.rescanning: bool = False
        self.rescan_progress: float = 0.0
        self.rescan_error: str | None = None
        self._rescan_task: asyncio.Task[None] | None = None

        # In-memory config overrides (configset values, not persisted)
        self.config_overrides: dict[str, dict[str, str]] = {}

        # Data directory for wallet files, SSL certs, etc.
        self.data_dir = data_dir or get_default_data_dir()

        # WebSocket notification hub
        self._wallet_generation = 0
        self._ws_clients: set[WebSocketClient] = set()

    @property
    def wallet_loaded(self) -> bool:
        """Return True if a wallet is currently unlocked."""
        return self.wallet_service is not None

    async def live_rescan_status(self) -> tuple[bool, float | None]:
        """Return ``(rescanning, progress)`` with Bitcoin Core as source of truth.

        The in-memory ``rescanning`` flag only tracks work started by this
        daemon and can go stale (e.g. the HTTP call driving the rescan fails
        while Core keeps scanning server-side, or the scan was triggered by
        wallet creation/recovery or an external client). When the backend
        exposes ``get_rescan_status`` (descriptor wallets), query Core's
        ``getwalletinfo.scanning`` directly so both the flag and the progress
        fraction reflect reality. Falls back to the in-memory flags for other
        backends or on RPC errors.
        """
        backend = getattr(self.wallet_service, "backend", None)
        get_status = getattr(backend, "get_rescan_status", None)
        if get_status is not None:
            try:
                status = await get_status()
            except Exception as exc:
                logger.debug("Could not query backend rescan status: {}", exc)
                status = None
            if status is not None and status.get("in_progress"):
                progress = status.get("progress")
                return True, float(progress) if progress is not None else self.rescan_progress
        # Core is not scanning (or we could not ask). The daemon-side flag
        # still covers wallet-side sync work that is not a Core scan.
        if self.rescanning:
            return True, self.rescan_progress
        return False, None

    @property
    def wallets_dir(self) -> Path:
        """Return the directory where wallet files are stored."""
        d = self.data_dir / "wallets"
        ensure_private_directory(d)
        return d

    def list_wallets(self) -> list[str]:
        """List all .jmdat wallet files in the wallets directory."""
        d = self.wallets_dir
        return sorted(f.name for f in d.iterdir() if f.suffix == ".jmdat")

    async def lock_wallet(self) -> bool:
        """Serialize and lock the current wallet."""
        async with self.wallet_lifecycle_lock:
            return await self._lock_wallet()

    @contextlib.asynccontextmanager
    async def wallet_lifecycle_admission(self) -> AsyncIterator[None]:
        """Reserve bounded capacity and serialize an unauthenticated lifecycle operation.

        The reservation includes the active operation and callers waiting for
        ``wallet_lifecycle_lock``. It is made before the first await and is
        released for successful, failed, and canceled requests.
        """
        if self._wallet_lifecycle_operations >= _MAX_WALLET_LIFECYCLE_OPERATIONS:
            raise WalletLifecycleQueueFull()
        self._wallet_lifecycle_operations += 1
        try:
            async with self.wallet_lifecycle_lock:
                yield
        finally:
            self._wallet_lifecycle_operations -= 1

    async def _lock_wallet(self) -> bool:
        """Lock the current wallet, stopping any running maker/taker first.

        The caller must hold :attr:`wallet_lifecycle_lock`.

        Returns whether the wallet was already locked.
        """
        if not self.wallet_loaded:
            return True  # already locked

        # Invalidate and close clients before stopping wallet activities. This
        # ensures no state changes generated while locking can reach a session
        # authenticated against the wallet being discarded.
        self._invalidate_ws_clients()
        self.token_authority.reset()

        market_keys = getattr(self.wallet_service, "market_keys", None)
        if market_keys is not None:
            market_keys.close()

        # Keep the wallet reserved if cleanup fails. Another wallet must not be
        # installed while an old seller still owns transport or ledger resources.
        await self.stop_market_seller()

        # Stop the maker if running.
        if self._maker_ref is not None:
            try:
                await self._maker_ref.stop()
            except Exception:
                logger.error("Error stopping maker during wallet lock")
                logger.bind(sensitive=True).exception("Error stopping maker during wallet lock")
        if self._maker_task is not None and not self._maker_task.done():
            self._maker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._maker_task

        # Stop any in-flight tumbler through the runner's bounded lifecycle so
        # the persisted plan reaches a truthful terminal state before the
        # wallet service is discarded.
        if self.tumble_runner is not None and self.tumble_task is not None:
            try:
                await self.tumble_runner.stop_and_wait(self.tumble_task)
            except Exception:
                logger.error("Error stopping tumbler during wallet lock")
                logger.bind(sensitive=True).exception("Error stopping tumbler during wallet lock")
                if not self.tumble_task.done():
                    raise

        # Stop the taker if running.
        if self._taker_ref is not None:
            try:
                await self._taker_ref.stop()
            except Exception:
                logger.error("Error stopping taker during wallet lock")
                logger.bind(sensitive=True).exception("Error stopping taker during wallet lock")
        if self._taker_task is not None and not self._taker_task.done():
            self._taker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._taker_task

        # Stop any background wallet sync task.
        if self._wallet_sync_task is not None and not self._wallet_sync_task.done():
            self._wallet_sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._wallet_sync_task

        # Stop the background transaction monitor.
        if self._tx_monitor_task is not None and not self._tx_monitor_task.done():
            self._tx_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._tx_monitor_task

        # Stop any background rescan-tracking task. This only stops the
        # daemon-side progress tracking; a rescan already accepted by Bitcoin
        # Core keeps running server-side.
        if self._rescan_task is not None and not self._rescan_task.done():
            self._rescan_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._rescan_task

        self.wallet_service = None
        self.wallet_mnemonic = ""
        self.wallet_name = ""
        self.wallet_password = ""
        self.maker_running = False
        self.taker_running = False
        self.coinjoin_state = CoinjoinState.NOT_RUNNING
        self.offer_list = None
        self.nickname = None
        self.last_broadcast_policy = None
        self.last_broadcast_method = None
        self.last_broadcast_fallback_reason = None
        self._taker_ref = None
        self._maker_ref = None
        self._maker_task = None
        self._taker_task = None
        self._wallet_sync_task = None
        self._rescan_task = None
        self._tx_monitor_task = None
        self._tx_monitor_ready = None
        self._tx_broadcast_notified.clear()
        self.rescanning = False
        self.rescan_progress = 0.0
        self.rescan_error = None
        self.tumble_runner = None
        self.tumble_task = None
        self.tumble_plan_wallet = None
        self.config_overrides.clear()
        get_log_buffer().clear()
        return False  # was not locked, we just locked it

    def market_seller_status(self) -> dict[str, str | None]:
        """Return current-wallet seller status, without exposing private ledger identity."""
        if self._market_seller_ref is None:
            result: dict[str, str | None] = {
                "state": "stopped",
                "nickname": None,
                "seller_pubkey": None,
            }
        else:
            result = self._market_seller_ref.status()
            if self._market_seller_task is not None and not self._market_seller_task.done():
                result["state"] = "starting"
        result["error"] = self._market_seller_error
        return result

    async def start_market_seller(
        self, settings: JoinMarketSettings, options: WalletMarketSellerOptions
    ) -> None:
        """Register startup while the caller holds ``wallet_lifecycle_lock``.

        Chain and Tor work runs outside that lock. A wallet lock can therefore
        revoke its capabilities and cancel startup without waiting for backend I/O.
        """
        from jmwalletd.errors import NoWalletFound, ServiceAlreadyStarted
        from taker.wallet_market import WalletMarketSeller

        if self.wallet_service is None:
            raise NoWalletFound()
        if self._market_seller_ref is not None:
            if self._market_seller_ref.running or (
                self._market_seller_task is not None and not self._market_seller_task.done()
            ):
                raise ServiceAlreadyStarted("Market seller is already starting or running.")
            await self.stop_market_seller()
        wallet = self.wallet_service
        generation = self._wallet_generation
        seller = WalletMarketSeller(wallet, settings, options)
        self._market_seller_ref = seller
        self._market_seller_error = None

        async def start() -> None:
            try:
                await seller.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._market_seller_ref is seller:
                    self._market_seller_error = "Market seller startup failed."
                logger.error("Market seller startup failed")
                logger.bind(sensitive=True).exception("Market seller startup failed")
            finally:
                if (
                    self._market_seller_ref is seller
                    and self.wallet_service is wallet
                    and self._wallet_generation == generation
                ):
                    self._market_seller_task = None
                    self.broadcast_ws({"market_seller": self.market_seller_status()})

        self._market_seller_task = asyncio.create_task(start(), name="wallet-market-seller-start")

    async def stop_market_seller(self) -> None:
        """Stop only this wallet's seller while the caller holds the lifecycle lock."""
        seller = self._market_seller_ref
        task = self._market_seller_task
        if seller is not None:
            await seller.stop()
        if task is not None and task is not asyncio.current_task():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._market_seller_ref = None
        self._market_seller_task = None
        self._market_seller_error = None

    def activate_coinjoin_state(self, state: CoinjoinState) -> None:
        """Update the coinjoin state and notify WebSocket clients."""
        self.coinjoin_state = state
        if state == CoinjoinState.MAKER_RUNNING:
            self.maker_running = True
            self.taker_running = False
        elif state in (CoinjoinState.TAKER_RUNNING, CoinjoinState.TUMBLER_RUNNING):
            # The tumbler drives takers internally; surface it as taker activity
            # for legacy UI elements that only inspect ``taker_running``.
            self.taker_running = True
            self.maker_running = False
        else:
            self.maker_running = False
            self.taker_running = False

        self.broadcast_ws({"coinjoin_state": int(state)})

    def broadcast_ws(self, message: dict[str, Any]) -> None:
        """Send a JSON message to all authenticated WebSocket clients."""
        import json

        text = json.dumps(message)
        notification = WebSocketNotification(generation=self._wallet_generation, text=text)
        dead: set[WebSocketClient] = set()
        for client in self._ws_clients:
            if client.generation != self._wallet_generation:
                continue
            try:
                client.queue.put_nowait(notification)
            except asyncio.QueueFull:
                dead.add(client)
        for client in dead:
            self._close_ws_client(client)

    def mark_tx_broadcast(self, txid: str) -> None:
        """Record that a first-seen WebSocket notification was emitted for ``txid``.

        Lets the direct-send path and the background monitor cooperate so a
        transaction is announced at most once when it first appears.
        """
        if txid:
            self._tx_broadcast_notified.add(txid)

    def start_tx_monitor(self, *, baseline_existing: bool = True) -> None:
        """Start the background transaction monitor for the loaded wallet.

        Idempotent (a running monitor is left in place). Does nothing when no
        wallet is loaded or the backend cannot enumerate transactions (e.g. a
        light client without that capability), in which case WebSocket tx
        notifications are limited to the inline direct-send broadcast.
        """
        ws = self.wallet_service
        if ws is None:
            return
        if not getattr(ws.backend, "supports_tx_enumeration", False):
            logger.info(
                "Backend does not support transaction enumeration; "
                "WebSocket transaction notifications are limited to direct-send."
            )
            return
        if self._tx_monitor_task is not None and not self._tx_monitor_task.done():
            return

        from jmwalletd.tx_monitor import run_tx_monitor

        self._tx_broadcast_notified.clear()
        self._tx_monitor_ready = asyncio.Event()
        self._tx_monitor_task = asyncio.create_task(
            run_tx_monitor(
                self,
                ready=self._tx_monitor_ready,
                initialization_task=self._wallet_sync_task,
                baseline_existing=baseline_existing,
            )
        )

    async def wait_tx_monitor_ready(self, timeout: float = 30.0) -> bool:
        """Wait briefly for the monitor's silent history baseline.

        Lifecycle endpoints call this before returning so activity initiated by
        the client cannot land in the baseline window. A backend outage is
        bounded by ``timeout`` and reported to the caller as not ready.
        """
        ready = self._tx_monitor_ready
        if ready is None:
            return True
        ready_wait = asyncio.create_task(ready.wait())
        wait_for: set[asyncio.Future[Any]] = {ready_wait}
        monitor_task = self._tx_monitor_task
        if monitor_task is not None:
            wait_for.add(monitor_task)
        try:
            done, _pending = await asyncio.wait(
                wait_for,
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not ready_wait.done():
                ready_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ready_wait

        if ready.is_set():
            return True
        if monitor_task is not None and monitor_task in done:
            with contextlib.suppress(asyncio.CancelledError):
                exc = monitor_task.exception()
                if exc is not None:
                    logger.warning("Transaction monitor stopped before baseline: {}", exc)
            return False
        if not done:
            logger.warning("Transaction monitor baseline was not ready within {}s", timeout)
            return False
        return ready.is_set()

    def register_ws_client(self) -> WebSocketClient:
        """Register an unauthenticated WebSocket client when capacity permits."""
        preauth_clients = sum(client.generation is None for client in self._ws_clients)
        if preauth_clients >= _MAX_PREAUTH_WS_CLIENTS:
            logger.warning("WebSocket pre-auth client limit reached")
            raise WebSocketRegistrationLimit()
        client = WebSocketClient(queue=asyncio.Queue(maxsize=256))
        self._ws_clients.add(client)
        logger.debug("WebSocket client registered (total: {})", len(self._ws_clients))
        return client

    def unlock_retry_delay(self, wallet_name: str) -> float:
        """Return the remaining password retry delay for a wallet file."""
        failure = self._unlock_failures.get(wallet_name)
        if failure is None:
            return 0.0
        return max(0.0, failure.retry_after - time.monotonic())

    def record_unlock_failure(self, wallet_name: str) -> float:
        """Record a failed password attempt and return its retry delay."""
        previous = self._unlock_failures.get(wallet_name)
        failures = min(
            (previous.failures if previous is not None else 0) + 1,
            _UNLOCK_BACKOFF_MAX_FAILURES,
        )
        delay = min(
            _UNLOCK_BACKOFF_BASE_SECONDS * (2 ** (failures - 1)),
            _UNLOCK_BACKOFF_MAX_SECONDS,
        )
        self._unlock_failures[wallet_name] = UnlockFailure(
            failures=failures,
            retry_after=time.monotonic() + delay,
        )
        return delay

    def reset_unlock_failures(self, wallet_name: str) -> None:
        """Clear password retry state after a successful unlock."""
        self._unlock_failures.pop(wallet_name, None)

    def authenticate_ws_client(self, client: WebSocketClient) -> bool:
        """Bind a registered client to the current wallet generation."""
        if client not in self._ws_clients or not self.wallet_loaded:
            return False
        client.generation = self._wallet_generation
        return True

    def ws_client_is_current(self, client: WebSocketClient, generation: int) -> bool:
        """Return whether a notification can still be sent to ``client``."""
        return (
            client in self._ws_clients
            and client.generation == generation
            and generation == self._wallet_generation
        )

    def unregister_ws_client(self, client: WebSocketClient) -> None:
        """Unregister a WebSocket client."""
        self._ws_clients.discard(client)
        logger.debug("WebSocket client unregistered (total: {})", len(self._ws_clients))

    def _invalidate_ws_clients(self) -> None:
        """Invalidate all clients and tell their writers to close promptly."""
        self._wallet_generation += 1
        clients = tuple(self._ws_clients)
        for client in clients:
            self._close_ws_client(client)

    def _close_ws_client(self, client: WebSocketClient) -> None:
        """Remove a client and replace queued notifications with a close event."""
        self._ws_clients.discard(client)
        client.generation = None
        while True:
            try:
                client.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        client.queue.put_nowait(WebSocketControl.CLOSE)

    def reconcile_stale_tumbler_plans(self) -> list[str]:
        """Reconcile stale on-disk tumbler plans after daemon startup.

        A ``RUNNING`` or ``PENDING`` plan on disk at startup means the daemon
        exited mid-run (crash, restart, lost power). The backend state (taker
        session, directory connection, wallet sync cursor) is gone, so silently
        resuming would risk double-spending. The sole exception is a current
        taker phase that has already broadcast and is waiting only for its
        persisted txid to confirm. That plan is reset to ``PENDING`` so the
        runner resumes the confirmation gate without replaying the CoinJoin.
        All other stale plans become ``FAILED`` with a diagnostic.

        Returns the list of wallet names whose plans were touched, for
        logging / metrics.
        """
        # Local import to avoid a circular dependency at module import time.
        from tumbler.persistence import (
            SCHEDULES_SUBDIR,
            PlanCorruptError,
            load_plan,
            save_plan,
        )
        from tumbler.plan import (
            PhaseStatus,
            PlanStatus,
            is_safely_resumable_confirmation_wait,
            reset_plan_for_resume,
        )

        schedules_dir = self.data_dir / SCHEDULES_SUBDIR
        if not schedules_dir.exists():
            return []

        reconciled: list[str] = []
        for path in sorted(schedules_dir.glob("*.yaml")):
            try:
                plan = load_plan(path.stem, self.data_dir)
            except (PlanCorruptError, OSError) as exc:
                logger.warning("Skipping unreadable plan at {}: {}", path, exc)
                continue
            if plan.status not in (PlanStatus.RUNNING, PlanStatus.PENDING):
                continue
            if plan.status == PlanStatus.RUNNING and is_safely_resumable_confirmation_wait(plan):
                reset_plan_for_resume(plan)
            else:
                plan.status = PlanStatus.FAILED
                plan.error = "daemon restarted mid-run"
                current = plan.current()
                if current is not None and current.status == PhaseStatus.RUNNING:
                    current.status = PhaseStatus.FAILED
                    current.error = "daemon restarted mid-run"
            try:
                save_plan(plan, self.data_dir)
            except OSError as exc:  # pragma: no cover - disk full, permissions
                logger.warning("Failed to persist reconciled plan at {}: {}", path, exc)
                continue
            reconciled.append(plan.wallet_name)
        if reconciled:
            logger.info("Reconciled {} stale tumbler plan(s) on startup", len(reconciled))
        return reconciled
