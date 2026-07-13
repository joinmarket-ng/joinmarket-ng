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
from pathlib import Path
from typing import Any

from loguru import logger

from jmcore.paths import get_default_data_dir
from jmwalletd.auth import JMTokenAuthority


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

        # Coinjoin state
        self.coinjoin_state = CoinjoinState.NOT_RUNNING
        self.maker_running: bool = False
        self.taker_running: bool = False
        self.offer_list: list[dict[str, str | int | float]] | None = None
        self.nickname: str | None = None

        # Runtime references to active taker/maker instances (for stop signals).
        self._taker_ref: Any = None
        self._maker_ref: Any = None

        # asyncio.Task handles for the background _run_maker / _run_taker coroutines.
        self._maker_task: asyncio.Task[None] | None = None
        self._taker_task: asyncio.Task[None] | None = None
        self._wallet_sync_task: asyncio.Task[None] | None = None

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
        self._rescan_task: asyncio.Task[None] | None = None

        # In-memory config overrides (configset values, not persisted)
        self.config_overrides: dict[str, dict[str, str]] = {}

        # Data directory for wallet files, SSL certs, etc.
        self.data_dir = data_dir or get_default_data_dir()

        # WebSocket notification hub
        self._ws_clients: set[asyncio.Queue[str]] = set()

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
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_wallets(self) -> list[str]:
        """List all .jmdat wallet files in the wallets directory."""
        d = self.wallets_dir
        return sorted(f.name for f in d.iterdir() if f.suffix == ".jmdat")

    async def lock_wallet(self) -> bool:
        """Lock the current wallet, stopping any running maker/taker first.

        Returns whether the wallet was already locked.
        """
        if not self.wallet_loaded:
            return True  # already locked

        # Stop the maker if running.
        if self._maker_ref is not None:
            try:
                await self._maker_ref.stop()
            except Exception:
                logger.exception("Error stopping maker during wallet lock")
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
                logger.exception("Error stopping tumbler during wallet lock")
                if not self.tumble_task.done():
                    raise

        # Stop the taker if running.
        if self._taker_ref is not None:
            try:
                await self._taker_ref.stop()
            except Exception:
                logger.exception("Error stopping taker during wallet lock")
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
        self.tumble_runner = None
        self.tumble_task = None
        self.tumble_plan_wallet = None
        self.config_overrides.clear()
        self.token_authority.reset()
        return False  # was not locked, we just locked it

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
        dead: set[asyncio.Queue[str]] = set()
        for q in self._ws_clients:
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                dead.add(q)
        self._ws_clients -= dead

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

    def register_ws_client(self) -> asyncio.Queue[str]:
        """Register a new WebSocket client and return its message queue."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        self._ws_clients.add(q)
        logger.debug("WebSocket client registered (total: {})", len(self._ws_clients))
        return q

    def unregister_ws_client(self, q: asyncio.Queue[str]) -> None:
        """Unregister a WebSocket client."""
        self._ws_clients.discard(q)
        logger.debug("WebSocket client unregistered (total: {})", len(self._ws_clients))

    def reconcile_stale_tumbler_plans(self) -> list[str]:
        """Mark any on-disk tumbler plan left in a non-terminal state as FAILED.

        A ``RUNNING`` or ``PENDING`` plan on disk at startup means the daemon
        exited mid-run (crash, restart, lost power). The backend state (taker
        session, directory connection, wallet sync cursor) is gone, so silently
        resuming would risk double-spending. Instead, mark the plan FAILED with
        a diagnostic so the UI can surface it; the user can then delete the
        plan and build a new one.

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
        from tumbler.plan import PhaseStatus, PlanStatus

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
