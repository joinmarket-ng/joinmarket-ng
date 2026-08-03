"""
Heartbeat liveness detection for directory server peers.

Implements the PING/PONG heartbeat protocol compatible with joinmarket-rs.
The directory server periodically probes idle peers and evicts unresponsive ones.

Protocol flow (matching joinmarket-rs behavior):
1. Every ``sweep_interval_sec`` seconds, run a heartbeat sweep.
2. Hard-evict all peers whose ``last_seen`` exceeds ``hard_evict_sec``.
3. Probe peers idle longer than ``idle_threshold_sec``:
   - Ping-capable peers: send PING (type 798), mark ``pong_pending``.
   - Non-ping makers: send unicast ``!orderbook`` to elicit an offer
     re-announcement (which updates ``last_seen``).
   - Non-ping takers/watchers: no probe -- they will be hard-evicted
     when ``hard_evict_sec`` is reached.
4. Wait ``pong_wait_sec`` seconds for responses.
5. Evict all peers still having ``pong_pending = True``.

Timing constants match joinmarket-rs defaults:
- sweep_interval_sec = 60
- idle_threshold_sec = 600  (10 minutes)
- hard_evict_sec    = 1500  (25 minutes)
- pong_wait_sec     = 30
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from jmcore.models import MessageEnvelope
from jmcore.protocol import MessageType
from loguru import logger

from directory_server.peer_registry import PeerRegistry

# Type aliases
SendCallback = Callable[[str, bytes, str], Awaitable[None]]
EvictCallback = Callable[[str, str, str], Awaitable[None]]


@dataclass
class HeartbeatConfig:
    """Heartbeat timing configuration.

    Defaults match joinmarket-rs for interoperability.
    """

    sweep_interval_sec: float = 60.0
    idle_threshold_sec: float = 600.0  # 10 minutes
    hard_evict_sec: float = 1500.0  # 25 minutes
    pong_wait_sec: float = 30.0


class HeartbeatManager:
    """Manages periodic heartbeat sweeps for the directory server.

    Args:
        peer_registry: Registry to query peer state and last_seen.
        send_callback: Async callable ``(peer_key, data_bytes, connection_id) -> None``.
        evict_callback: Async callable ``(peer_key, reason, connection_id) -> None``
            that disconnects and unregisters a peer.
        config: Timing configuration.
    """

    def __init__(
        self,
        peer_registry: PeerRegistry,
        send_callback: SendCallback,
        evict_callback: EvictCallback,
        config: HeartbeatConfig | None = None,
        server_nick: str = "",
    ) -> None:
        self.peer_registry = peer_registry
        self.send_callback = send_callback
        self.evict_callback = evict_callback
        self.config = config or HeartbeatConfig()
        self.server_nick = server_nick

        # Peers waiting for a PONG reply
        self._pong_pending: set[tuple[str, str]] = set()
        self._task: asyncio.Task[None] | None = None

    # -- public API --

    def start(self) -> None:
        """Start the background heartbeat loop."""
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="heartbeat")
        logger.info(
            f"Heartbeat started (sweep={self.config.sweep_interval_sec}s, "
            f"idle={self.config.idle_threshold_sec}s, "
            f"hard_evict={self.config.hard_evict_sec}s, "
            f"pong_wait={self.config.pong_wait_sec}s)"
        )

    async def stop(self) -> None:
        """Stop the background heartbeat loop."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        self._pong_pending.clear()
        logger.info("Heartbeat stopped")

    def handle_pong(self, peer_key: str, connection_id: str | None = None) -> None:
        """Called by the message router when a PONG is received.

        Clears ``pong_pending`` so the peer is not evicted.
        """
        connection_id = connection_id or self.peer_registry.get_connection_id(peer_key)
        owner = (peer_key, connection_id) if connection_id is not None else None
        if owner is not None and owner in self._pong_pending:
            self._pong_pending.discard(owner)
            logger.trace(f"Heartbeat: PONG received from {peer_key}")

    def forget_owner(self, peer_key: str, connection_id: str) -> None:
        """Remove heartbeat state belonging to an exact connection generation."""
        self._pong_pending.discard((peer_key, connection_id))

    @property
    def pong_pending(self) -> frozenset[tuple[str, str]]:
        """Read-only view of peers with pending pongs (for testing)."""
        return frozenset(self._pong_pending)

    # -- internal --

    async def _loop(self) -> None:
        """Main heartbeat loop."""
        while True:
            await asyncio.sleep(self.config.sweep_interval_sec)
            try:
                await self._sweep()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Heartbeat sweep failed")

    async def _sweep(self) -> None:
        """Run a single heartbeat sweep."""
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        hard_cutoff = now - timedelta(seconds=self.config.hard_evict_sec)
        idle_cutoff = now - timedelta(seconds=self.config.idle_threshold_sec)

        # Phase 1: hard-evict peers that exceeded the absolute timeout
        hard_evict_peers = self.peer_registry.get_peers_idle_since(hard_cutoff)
        hard_evicted: set[tuple[str, str]] = set()
        for peer_key, peer_info, connection_id in hard_evict_peers:
            logger.info(
                f"Heartbeat: hard-evicting {peer_info.nick} ({peer_key}) "
                f"-- idle since {peer_info.last_seen}"
            )
            owner = (peer_key, connection_id)
            hard_evicted.add(owner)
            self._pong_pending.discard(owner)
            await self._call_evict(peer_key, "idle timeout", connection_id)

        # Phase 2: probe idle peers (idle > idle_threshold but < hard_evict)
        idle_peers = self.peer_registry.get_peers_idle_since(idle_cutoff)
        pinged_owners: list[tuple[str, str]] = []

        for peer_key, peer_info, connection_id in idle_peers:
            # Skip peers already hard-evicted above
            owner = (peer_key, connection_id)
            if owner in hard_evicted or not self.peer_registry.is_current_owner(
                peer_key, connection_id
            ):
                continue

            if self.peer_registry.supports_ping(peer_key, connection_id):
                # Send PING to ping-capable peers
                self._pong_pending.add(owner)
                if await self._send_ping(peer_key, connection_id):
                    pinged_owners.append(owner)
            elif self.peer_registry.is_maker(peer_key, connection_id):
                # Send unicast !orderbook to non-ping makers to elicit a response
                await self._send_orderbook_probe(peer_key, peer_info.nick, connection_id)
            # else: non-ping takers/watchers -- no probe, will be hard-evicted later

        if not pinged_owners:
            return

        logger.debug(f"Heartbeat: pinged {len(pinged_owners)} idle peers, waiting for PONGs")

        # Phase 3: wait for PONGs
        await asyncio.sleep(self.config.pong_wait_sec)

        # Phase 4: evict peers that did not respond
        timed_out = [owner for owner in pinged_owners if owner in self._pong_pending]
        for peer_key, connection_id in timed_out:
            peer = self.peer_registry.get_by_key(peer_key)
            nick = peer.nick if peer else peer_key
            logger.info(f"Heartbeat: evicting {nick} ({peer_key}) -- no PONG response")
            self._pong_pending.discard((peer_key, connection_id))
            await self._call_evict(peer_key, "pong timeout", connection_id)

        if timed_out:
            logger.info(f"Heartbeat: evicted {len(timed_out)} unresponsive peers")

    async def _send_ping(self, peer_key: str, connection_id: str) -> bool:
        """Send a PING message to a peer."""
        ping_envelope = MessageEnvelope(message_type=MessageType.PING, payload="")
        try:
            await self._call_send(peer_key, ping_envelope.to_bytes(), connection_id)
            logger.trace(f"Heartbeat: sent PING to {peer_key}")
            return True
        except Exception as e:
            logger.debug(f"Heartbeat: failed to send PING to {peer_key}: {e}")
            # Send failure likely means peer is already gone; evict immediately
            self._pong_pending.discard((peer_key, connection_id))
            await self._call_evict(peer_key, f"send failed: {e}", connection_id)
            return False

    async def _send_orderbook_probe(self, peer_key: str, nick: str, connection_id: str) -> None:
        """Send a unicast !orderbook to a non-ping maker to probe liveness.

        The maker will respond with its offers, which updates last_seen on the
        next message receive.  This matches the joinmarket-rs fallback for
        peers that don't support PING.

        The probe is formatted as a standard PUBMSG addressed to PUBLIC so the
        reference implementation routes it through ``on_pubmsg`` (which handles
        ``!orderbook``) rather than ``on_privmsg`` (which requires a signature).
        Although the wire message is sent unicast to the target peer, the
        payload uses the correct ``from_nick!PUBLIC!orderbook`` format.
        """
        from jmcore.protocol import COMMAND_PREFIX

        # Build a properly formatted PUBMSG:
        # from_nick!PUBLIC!orderbook
        # The reference client splits on COMMAND_PREFIX ("!") and checks
        # to_nick == "PUBLIC" to route to on_pubmsg.
        payload = f"{self.server_nick}{COMMAND_PREFIX}PUBLIC{COMMAND_PREFIX}orderbook"
        probe_envelope = MessageEnvelope(
            message_type=MessageType.PUBMSG,
            payload=payload,
        )
        try:
            await self._call_send(peer_key, probe_envelope.to_bytes(), connection_id)
            logger.trace(f"Heartbeat: sent !orderbook probe to {nick} ({peer_key})")
        except Exception as e:
            logger.debug(f"Heartbeat: failed to send !orderbook probe to {peer_key}: {e}")

    async def _call_send(self, peer_key: str, data: bytes, connection_id: str) -> None:
        if not self.peer_registry.is_current_owner(peer_key, connection_id):
            return
        await self.send_callback(peer_key, data, connection_id)

    async def _call_evict(self, peer_key: str, reason: str, connection_id: str) -> None:
        await self.evict_callback(peer_key, reason, connection_id)
