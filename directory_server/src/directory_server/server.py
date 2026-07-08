"""
Main directory server implementation using asyncio.

Implements Open/Closed Principle: extensible without modification.
"""

import asyncio
import json
from datetime import UTC, datetime

from jmcore.models import MessageEnvelope, NetworkType, PeerStatus
from jmcore.network import ConnectionPool, TCPConnection
from jmcore.notifications import get_notifier
from jmcore.protocol import MessageType
from jmcore.rate_limiter import RateLimitAction, RateLimiter
from jmcore.settings import DirectoryServerSettings
from jmcore.tasks import spawn_task
from jmcore.version import __version__
from loguru import logger

from directory_server.handshake_handler import HandshakeError, HandshakeHandler
from directory_server.health import HealthCheckServer
from directory_server.heartbeat import HeartbeatConfig, HeartbeatManager
from directory_server.message_router import MessageRouter
from directory_server.peer_registry import PeerRegistry


def build_motd(user_motd: str) -> str:
    """
    Build the MOTD string with version information.

    If the user-provided MOTD doesn't contain version info, append it.
    This ensures clients can see the JoinMarket NG version like:
    "JoinMarket NG version: 0.9.0"
    """
    version_line = f"JoinMarket NG version: {__version__}"

    # If user already included version info, use their MOTD as-is
    if "VERSION" in user_motd.upper():
        return user_motd

    # Append version info to user's MOTD
    return f"{user_motd}\n{version_line}"


class DirectoryServer:
    def __init__(self, settings: DirectoryServerSettings, network: NetworkType, server_nick: str):
        self.settings = settings
        self.network = network
        self.server_nick = server_nick

        self.peer_registry = PeerRegistry(max_peers=settings.max_peers)
        self.connections = ConnectionPool(max_connections=settings.max_peers)
        self.peer_key_to_conn_id: dict[str, str] = {}

        # Heartbeat manager for peer liveness detection
        heartbeat_config = HeartbeatConfig(
            sweep_interval_sec=settings.heartbeat_sweep_interval,
            idle_threshold_sec=settings.heartbeat_idle_threshold,
            hard_evict_sec=settings.heartbeat_hard_evict,
            pong_wait_sec=settings.heartbeat_pong_wait,
        )
        self.heartbeat = HeartbeatManager(
            peer_registry=self.peer_registry,
            send_callback=self._send_to_peer,
            evict_callback=self._evict_peer,
            config=heartbeat_config,
            server_nick=server_nick,
        )

        self.message_router = MessageRouter(
            peer_registry=self.peer_registry,
            send_callback=self._send_to_peer,
            broadcast_batch_size=settings.broadcast_batch_size,
            on_send_failed=self._handle_send_failed,
            on_pong=self.heartbeat.handle_pong,
        )
        self.handshake_handler = HandshakeHandler(
            network=self.network,
            server_nick=server_nick,
            motd=build_motd(settings.motd),
        )
        # Rate limit by connection ID to prevent nick spoofing attacks.
        # A malicious peer could claim another's nick and spam to get them rate limited.
        # Using connection ID ensures each physical connection has its own bucket.
        self.rate_limiter = RateLimiter(
            rate_limit=settings.message_rate_limit,
            burst_limit=settings.message_burst_limit,
            disconnect_threshold=settings.rate_limit_disconnect_threshold
            if settings.rate_limit_disconnect_threshold > 0
            else None,
        )

        self.server: asyncio.Server | None = None
        self._shutdown = False
        self._start_time = datetime.now(UTC)
        self._client_tasks: set[asyncio.Task[None]] = set()
        self.health_server = HealthCheckServer(
            host=settings.health_check_host, port=settings.health_check_port
        )

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self._client_connected,
            self.settings.host,
            self.settings.port,
            limit=self.settings.max_message_size,
        )

        addr = self.server.sockets[0].getsockname()
        logger.info(
            f"Directory server started on {addr[0]}:{addr[1]} (network: {self.network.value})"
        )

        self.health_server.start(self)
        self.heartbeat.start()

        async with self.server:
            await self.server.serve_forever()

    async def _client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Wrapper to track client handler tasks for proper shutdown."""
        task = asyncio.current_task()
        if task:
            self._client_tasks.add(task)
            task.add_done_callback(self._client_tasks.discard)
        await self._handle_client(reader, writer)

    async def stop(self) -> None:
        logger.info("Shutting down directory server...")
        self._shutdown = True

        await self.heartbeat.stop()
        self.health_server.stop()

        # Cancel all client handler tasks before closing server
        # This is required for Python 3.12+ where wait_closed() waits for handlers
        if self._client_tasks:
            logger.debug(f"Cancelling {len(self._client_tasks)} client handler tasks...")
            for task in self._client_tasks:
                task.cancel()
            # Wait for tasks to finish cancellation with timeout
            await asyncio.wait(self._client_tasks, timeout=5.0)
            self._client_tasks.clear()

        if self.server:
            self.server.close()
            # Use timeout on wait_closed() as safety net for edge cases
            try:
                await asyncio.wait_for(self.server.wait_closed(), timeout=5.0)
            except TimeoutError:
                logger.warning("Server wait_closed() timed out after 5s")

        await self.connections.close_all()
        logger.info("Directory server stopped")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer_addr = writer.get_extra_info("peername")
        conn_id = f"{peer_addr[0]}:{peer_addr[1]}"
        logger.trace(f"New connection from {conn_id}")

        transport = writer.transport
        # Set reasonable write buffer limits (64KB high, 16KB low)
        # This allows some buffering while preventing memory bloat
        transport.set_write_buffer_limits(high=65536, low=16384)  # type: ignore[union-attr]
        sock = transport.get_extra_info("socket")
        if sock:
            import socket

            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        connection = TCPConnection(reader, writer, self.settings.max_message_size)
        peer_key: str | None = None

        try:
            self.connections.add(conn_id, connection)
            peer_key = await self._perform_handshake(connection, conn_id)
            if not peer_key:
                return

            await self._handle_peer_messages(connection, conn_id, peer_key)

        except Exception as e:
            logger.error(f"Error handling client {conn_id}: {e}")
        finally:
            await self._cleanup_peer(connection, conn_id, peer_key)

    async def _perform_handshake(self, connection: TCPConnection, conn_id: str) -> str | None:
        try:
            logger.trace(f"[{conn_id}] Waiting for handshake message...")
            data = await asyncio.wait_for(connection.receive(), timeout=30.0)
            logger.trace(f"[{conn_id}] Received {len(data)} bytes: {data[:200]!r}...")

            envelope = MessageEnvelope.from_bytes(
                data,
                max_line_length=self.settings.max_line_length,
                max_json_nesting_depth=self.settings.max_json_nesting_depth,
            )
            logger.trace(
                f"[{conn_id}] Parsed envelope: type={envelope.message_type}, payload_len={len(envelope.payload)}"
            )

            if envelope.message_type != MessageType.HANDSHAKE:
                logger.warning(f"[{conn_id}] Expected handshake, got {envelope.message_type}")
                return None

            logger.trace(f"[{conn_id}] Processing handshake payload: {envelope.payload[:200]}")
            peer_info, response = self.handshake_handler.process_handshake(
                envelope.payload, conn_id
            )
            logger.trace(
                f"[{conn_id}] Handshake processed: peer_nick={peer_info.nick}, location={peer_info.location_string}"
            )

            response_envelope = MessageEnvelope(
                message_type=MessageType.DN_HANDSHAKE, payload=json.dumps(response)
            )
            response_bytes = response_envelope.to_bytes()
            logger.trace(f"[{conn_id}] Sending handshake response: {len(response_bytes)} bytes")
            logger.trace(f"[{conn_id}] Response content: {response_bytes[:200]!r}...")

            try:
                await connection.send(response_bytes)
                logger.trace(f"[{conn_id}] Handshake response sent successfully")
                # Small delay to let client process the handshake response
                await asyncio.sleep(0.05)
            except Exception as e:
                logger.error(f"[{conn_id}] Failed to send handshake response: {e}")
                raise

            peer_location = peer_info.location_string
            self.peer_registry.register(peer_info)
            logger.trace(f"[{conn_id}] Peer registered in registry")

            peer_key = peer_info.nick if peer_location == "NOT-SERVING-ONION" else peer_location
            self.peer_registry.update_status(peer_key, PeerStatus.HANDSHAKED)
            self.peer_key_to_conn_id[peer_key] = conn_id
            logger.trace(f"[{conn_id}] Peer key mapped: {peer_key}")

            logger.trace(f"[{conn_id}] Handshake complete for {peer_key} (nick={peer_info.nick})")

            return peer_key

        except HandshakeError as e:
            logger.warning(f"[{conn_id}] Handshake failed: {e}")
            return None
        except TimeoutError:
            logger.warning(f"[{conn_id}] Handshake timeout (30s)")
            return None
        except Exception as e:
            logger.error(f"[{conn_id}] Handshake error: {e}", exc_info=True)
            return None

    async def _handle_peer_messages(
        self, connection: TCPConnection, conn_id: str, peer_key: str
    ) -> None:
        peer_info = self.peer_registry.get_by_key(peer_key)
        if not peer_info:
            return

        logger.info(f"Peer {peer_info.nick} connected from {peer_info.location_string}")

        # Fire-and-forget notification for peer connect
        total_peers = self.peer_registry.count()
        spawn_task(
            get_notifier().notify_peer_connected(
                peer_info.nick, peer_info.location_string, total_peers
            )
        )

        while connection.is_connected() and not self._shutdown:
            try:
                data = await connection.receive()

                # Update last_seen on every received message for heartbeat liveness
                self.peer_registry.update_last_seen(peer_key)

                # Rate limiting by connection ID to prevent nick spoofing attacks.
                # A malicious peer could claim another's nick in handshake and spam
                # to get the legitimate peer rate-limited. Using conn_id ensures
                # each physical connection is rate-limited independently.
                action, delay = self.rate_limiter.check(conn_id)

                if action == RateLimitAction.DISCONNECT:
                    violations = self.rate_limiter.get_violation_count(conn_id)
                    logger.warning(
                        f"Rate limit exceeded for {peer_info.nick} ({conn_id}): "
                        f"{violations} violations, disconnecting"
                    )
                    # Fire-and-forget notification for rate limit ban
                    spawn_task(
                        get_notifier().notify_peer_banned(
                            peer_info.nick,
                            "Rate limit exceeded",
                            self.settings.rate_limit_disconnect_threshold,
                        )
                    )
                    break
                elif action == RateLimitAction.DELAY:
                    violations = self.rate_limiter.get_violation_count(conn_id)
                    if violations % 50 == 1:  # Log every 50th violation to avoid spam
                        logger.debug(
                            f"Rate limiting {peer_info.nick} ({conn_id}): "
                            f"{violations} violations, delay={delay:.2f}s"
                        )
                    # Drop message but stay connected - this is the "slowdown" approach
                    continue

                envelope = MessageEnvelope.from_bytes(
                    data,
                    max_line_length=self.settings.max_line_length,
                    max_json_nesting_depth=self.settings.max_json_nesting_depth,
                )

                await self.message_router.route_message(envelope, peer_key)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error processing message from {peer_info.nick}: {e}")
                break

    async def _cleanup_peer(
        self, connection: TCPConnection, conn_id: str, peer_key: str | None
    ) -> None:
        # When called from a cancelled task (e.g. during server.stop()),
        # pending cancellation causes every await to raise CancelledError,
        # preventing connection.close() from completing and leaking
        # StreamWriter objects.  Suppress pending cancellation so cleanup
        # awaits can finish; the task will end naturally after this returns.
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling() > 0:
                task.uncancel()

        try:
            if peer_key:
                peer_info = self.peer_registry.get_by_key(peer_key)

                if peer_info:
                    logger.info(f"Peer {peer_info.nick} disconnected")
                    # Fire-and-forget notification for peer disconnect
                    total_peers = (
                        self.peer_registry.count() - 1
                    )  # Minus 1 since we're about to unregister
                    spawn_task(get_notifier().notify_peer_disconnected(peer_info.nick, total_peers))
                    await self.message_router.broadcast_peer_disconnect(
                        peer_info.location_string, peer_info.network
                    )
                    self.peer_registry.unregister(peer_key)

                if peer_key in self.peer_key_to_conn_id:
                    del self.peer_key_to_conn_id[peer_key]

                # Clean up offer tracking
                self.message_router.remove_peer_offers(peer_key)

            # Clean up rate limiter state (keyed by conn_id, not peer_key)
            self.rate_limiter.remove_peer(conn_id)
        finally:
            self.connections.remove(conn_id)
            try:
                await connection.close()
            except Exception as e:
                logger.trace(f"Error closing connection: {e}")

    async def _send_to_peer(self, peer_location: str, data: bytes) -> None:
        peer_key = peer_location

        conn_id = self.peer_key_to_conn_id.get(peer_key)
        if not conn_id:
            raise ValueError(f"No connection for peer: {peer_location}")

        connection = self.connections.get(conn_id)
        if not connection:
            raise ValueError(f"No connection for conn_id: {conn_id}")

        await connection.send(data)

    async def _handle_send_failed(self, peer_key: str) -> None:
        """
        Called when sending to a peer fails.

        Removes the peer from both the connection mapping and the registry
        to prevent further send attempts to this dead connection.
        """
        if peer_key in self.peer_key_to_conn_id:
            logger.debug(f"Removing failed peer mapping: {peer_key}")
            del self.peer_key_to_conn_id[peer_key]

        # Also unregister from peer registry to prevent further routing attempts
        peer_info = self.peer_registry.get_by_key(peer_key)
        if peer_info:
            logger.debug(f"Unregistering failed peer: {peer_key}")
            self.peer_registry.unregister(peer_key)

    async def _evict_peer(self, peer_key: str, reason: str) -> None:
        """Evict a peer due to heartbeat timeout.

        Broadcasts disconnect, unregisters from registry, closes the
        underlying TCP connection, and cleans up all mappings.
        """
        peer_info = self.peer_registry.get_by_key(peer_key)
        if not peer_info:
            return

        logger.info(f"Evicting peer {peer_info.nick} ({peer_key}): {reason}")

        # Broadcast disconnect to other peers
        try:
            await self.message_router.broadcast_peer_disconnect(
                peer_info.location_string, peer_info.network
            )
        except Exception as e:
            logger.debug(f"Failed to broadcast disconnect for {peer_key}: {e}")

        # Fire-and-forget notification
        total_peers = self.peer_registry.count() - 1
        spawn_task(get_notifier().notify_peer_disconnected(peer_info.nick, total_peers))

        # Unregister from registry
        self.peer_registry.unregister(peer_key)
        self.message_router.remove_peer_offers(peer_key)

        # Close the underlying connection
        conn_id = self.peer_key_to_conn_id.pop(peer_key, None)
        if conn_id:
            self.rate_limiter.remove_peer(conn_id)
            connection = self.connections.get(conn_id)
            if connection:
                self.connections.remove(conn_id)
                try:
                    await connection.close()
                except Exception as e:
                    logger.trace(f"Error closing evicted connection: {e}")

    def is_healthy(self) -> bool:
        return (
            self.server is not None
            and not self._shutdown
            and self.peer_registry.count() < self.settings.max_peers
        )

    def get_stats(self) -> dict:
        return {
            "network": self.network.value,
            "connected_peers": self.peer_registry.count(),
            "max_peers": self.settings.max_peers,
            "active_connections": len(self.connections),
            "rate_limit_violations": self.rate_limiter.get_stats()["total_violations"],
        }

    def get_detailed_stats(self) -> dict:
        uptime = (datetime.now(UTC) - self._start_time).total_seconds()
        registry_stats = self.peer_registry.get_stats()

        connected_peers = self.peer_registry.get_all_connected()
        passive_peers = self.peer_registry.get_passive_peers()
        active_peers = self.peer_registry.get_active_peers()

        offer_stats = self.message_router.get_offer_stats()

        return {
            "network": self.network.value,
            "uptime_seconds": uptime,
            "server_status": "running" if not self._shutdown else "stopping",
            "max_peers": self.settings.max_peers,
            "stats": registry_stats,
            "rate_limiter": self.rate_limiter.get_stats(),
            "offers": offer_stats,
            "connected_peers": {
                "total": len(connected_peers),
                "nicks": [p.nick for p in connected_peers],
            },
            "passive_peers": {
                "total": len(passive_peers),
                "nicks": [p.nick for p in passive_peers],
            },
            "active_peers": {
                "total": len(active_peers),
                "nicks": [p.nick for p in active_peers],
            },
            "active_connections": len(self.connections),
        }

    def log_status(self) -> None:
        stats = self.get_detailed_stats()
        logger.info("=== Directory Server Status ===")
        logger.info(f"Network: {stats['network']}")
        logger.info(f"Uptime: {stats['uptime_seconds']:.0f}s")
        logger.info(f"Status: {stats['server_status']}")
        logger.info(f"Connected peers: {stats['connected_peers']['total']}/{stats['max_peers']}")
        logger.info(f"  Nicks: {', '.join(stats['connected_peers']['nicks'][:10])}")
        if len(stats["connected_peers"]["nicks"]) > 10:
            logger.info(f"  ... and {len(stats['connected_peers']['nicks']) - 10} more")
        logger.info(f"Passive peers (orderbook watchers): {stats['passive_peers']['total']}")
        logger.info(f"  Nicks: {', '.join(stats['passive_peers']['nicks'][:10])}")
        if len(stats["passive_peers"]["nicks"]) > 10:
            logger.info(f"  ... and {len(stats['passive_peers']['nicks']) - 10} more")
        logger.info(f"Active peers (makers): {stats['active_peers']['total']}")
        logger.info(f"  Nicks: {', '.join(stats['active_peers']['nicks'][:10])}")
        if len(stats["active_peers"]["nicks"]) > 10:
            logger.info(f"  ... and {len(stats['active_peers']['nicks']) - 10} more")
        logger.info(f"Active connections: {stats['active_connections']}")
        logger.info("===============================")
