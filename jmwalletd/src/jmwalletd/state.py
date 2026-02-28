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

from jmwalletd.auth import JMTokenAuthority


class CoinjoinState(enum.IntEnum):
    """Matches reference implementation's coinjoin state constants."""

    TAKER_RUNNING = 0
    MAKER_RUNNING = 1
    NOT_RUNNING = 2


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
        self.wallet_name: str = ""
        self.wallet_password: str = ""  # kept for re-unlock verification

        # Coinjoin state
        self.coinjoin_state = CoinjoinState.NOT_RUNNING
        self.maker_running: bool = False
        self.taker_running: bool = False
        self.current_schedule: list[list[str | int | float]] | None = None
        self.offer_list: list[dict[str, str | int | float]] | None = None
        self.nickname: str | None = None

        # Runtime references to active taker/maker instances (for stop signals).
        self._taker_ref: Any = None
        self._maker_ref: Any = None

        # asyncio.Task handles for the background _run_maker / _run_taker coroutines.
        self._maker_task: asyncio.Task[None] | None = None
        self._taker_task: asyncio.Task[None] | None = None

        # Rescan state
        self.rescanning: bool = False
        self.rescan_progress: float = 0.0

        # In-memory config overrides (configset values, not persisted)
        self.config_overrides: dict[str, dict[str, str]] = {}

        # Data directory for wallet files, SSL certs, etc.
        self.data_dir = data_dir or Path.home() / ".joinmarket-ng"

        # WebSocket notification hub
        self._ws_clients: set[asyncio.Queue[str]] = set()

    @property
    def wallet_loaded(self) -> bool:
        """Return True if a wallet is currently unlocked."""
        return self.wallet_service is not None

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

        self.wallet_service = None
        self.wallet_name = ""
        self.wallet_password = ""
        self.maker_running = False
        self.taker_running = False
        self.coinjoin_state = CoinjoinState.NOT_RUNNING
        self.current_schedule = None
        self.offer_list = None
        self.nickname = None
        self._taker_ref = None
        self._maker_ref = None
        self._maker_task = None
        self._taker_task = None
        self.config_overrides.clear()
        self.token_authority.reset()
        return False  # was not locked, we just locked it

    def activate_coinjoin_state(self, state: CoinjoinState) -> None:
        """Update the coinjoin state and notify WebSocket clients."""
        self.coinjoin_state = state
        if state == CoinjoinState.MAKER_RUNNING:
            self.maker_running = True
            self.taker_running = False
        elif state == CoinjoinState.TAKER_RUNNING:
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
