"""
Network primitives and connection management.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from jmcore.crypto import NickIdentity

# Host ID for direct peer-to-peer onion connections
# Used for message signing to prevent replay attacks
# Reference: jmdaemon/onionmc.py self.hostid = "onion-network"
ONION_HOSTID = "onion-network"


class ConnectionError(Exception):
    pass


class Connection(ABC):
    @abstractmethod
    async def send(self, data: bytes) -> None:
        pass

    @abstractmethod
    async def receive(self) -> bytes:
        pass

    @abstractmethod
    async def close(self) -> None:
        pass

    @abstractmethod
    def is_connected(self) -> bool:
        pass


class TCPConnection(Connection):
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        max_message_size: int = 2097152,  # 2MB
    ):
        self.reader = reader
        self.writer = writer
        self.max_message_size = max_message_size
        self._connected = True
        self._send_lock = asyncio.Lock()
        self._receive_lock = asyncio.Lock()

    async def send(self, data: bytes) -> None:
        if not self._connected:
            raise ConnectionError("Connection closed")
        if len(data) > self.max_message_size:
            raise ValueError(f"Message too large: {len(data)} > {self.max_message_size}")

        async with self._send_lock:
            if not self._connected:
                raise ConnectionError("Connection closed")

            message_to_send = data + b"\r\n"
            logger.trace(f"TCPConnection.send: sending {len(message_to_send)} bytes")
            try:
                self.writer.write(message_to_send)
                await self.writer.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                self._connected = False
                raise ConnectionError(f"Send failed: {e}") from e

    async def receive(self) -> bytes:
        if not self._connected:
            raise ConnectionError("Connection closed")

        async with self._receive_lock:
            if not self._connected:
                raise ConnectionError("Connection closed")

            try:
                data = await self.reader.readuntil(b"\n")
                stripped = data.rstrip(b"\r\n")
                logger.trace(f"TCPConnection.receive: received {len(stripped)} bytes")
                return stripped
            except asyncio.LimitOverrunError as e:
                logger.error(f"Message too large (>{self.max_message_size} bytes)")
                raise ConnectionError("Message too large") from e
            except asyncio.IncompleteReadError as e:
                self._connected = False
                logger.trace("TCPConnection.receive: connection closed by peer")
                raise ConnectionError("Connection closed by peer") from e
            except (builtins.ConnectionError, OSError) as e:
                self._connected = False
                logger.trace(f"TCPConnection.receive: connection error: {e}")
                raise ConnectionError(f"Connection lost: {e}") from e

    async def close(self) -> None:
        if not self._connected:
            return
        self._connected = False
        self.writer.close()
        await self.writer.wait_closed()

    def is_connected(self) -> bool:
        return self._connected


class ConnectionPool:
    def __init__(self, max_connections: int = 1000):
        self.max_connections = max_connections
        self.connections: dict[str, Connection] = {}

    def add(self, peer_id: str, connection: Connection) -> None:
        if len(self.connections) >= self.max_connections:
            raise ConnectionError(f"Connection pool full ({self.max_connections})")
        self.connections[peer_id] = connection

    def get(self, peer_id: str) -> Connection | None:
        return self.connections.get(peer_id)

    def remove(self, peer_id: str) -> None:
        if peer_id in self.connections:
            del self.connections[peer_id]

    async def close_all(self) -> None:
        connections_snapshot = list(self.connections.values())
        for conn in connections_snapshot:
            await conn.close()
        self.connections.clear()

    def __len__(self) -> int:
        return len(self.connections)


async def connect_direct(
    host: str,
    port: int,
    max_message_size: int = 2097152,  # 2MB
    timeout: float = 30.0,
) -> TCPConnection:
    """Connect directly via TCP without Tor (for local development/testing)."""
    try:
        logger.info(f"Connecting directly to {host}:{port}")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, limit=max_message_size),
            timeout=timeout,
        )
        logger.info(f"Connected to {host}:{port}")
        return TCPConnection(reader, writer, max_message_size)
    except Exception as e:
        logger.error(f"Failed to connect to {host}:{port}: {e}")
        raise ConnectionError(f"Direct connection failed: {e}") from e


async def connect_via_tor(
    onion_address: str,
    port: int,
    socks_host: str = "127.0.0.1",
    socks_port: int = 9050,
    max_message_size: int = 2097152,  # 2MB
    timeout: float = 120.0,
    socks_username: str | None = None,
    socks_password: str | None = None,
) -> TCPConnection:
    """
    Connect to an onion address via Tor SOCKS5 proxy.

    The timeout covers the entire SOCKS5 connection lifecycle including
    TCP handshake to the proxy, SOCKS5 negotiation, Tor circuit building,
    and PoW solving (if the destination has Tor PoW defense enabled).

    Under normal conditions, Tor circuit establishment takes 5-15 seconds.
    With PoW defense active (during DoS attacks), the PoW solving can add
    significant time. Tor's internal circuit timeout is ~120 seconds, so
    the default matches that.

    When ``socks_username`` and ``socks_password`` are provided, they are
    sent during SOCKS5 authentication.  Tor's ``IsolateSOCKSAuth`` (enabled
    by default) uses these to assign the connection to a distinct circuit,
    enabling stream isolation between different connection categories.
    """
    try:
        import socket

        import socks

        sock = socks.socksocket(socket.AF_INET, socket.SOCK_STREAM)
        sock.set_proxy(
            socks.SOCKS5,
            socks_host,
            socks_port,
            username=socks_username,
            password=socks_password,
        )
        sock.settimeout(timeout)

        logger.info(f"Connecting to {onion_address}:{port} via Tor ({socks_host}:{socks_port})")
        await asyncio.get_event_loop().run_in_executor(None, sock.connect, (onion_address, port))

        sock.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=sock, limit=max_message_size)

        logger.info(f"Connected to {onion_address}:{port}")
        return TCPConnection(reader, writer, max_message_size)

    except Exception as e:
        logger.error(f"Failed to connect to {onion_address}:{port} via Tor: {e}")
        raise ConnectionError(f"Tor connection failed: {e}") from e


class HiddenServiceListener:
    """
    TCP listener for accepting direct peer connections via Tor hidden service.

    This is used by makers to accept direct connections from takers,
    bypassing the directory server for lower latency.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        max_message_size: int = 2097152,
        on_connection: Callable[[TCPConnection, str], Coroutine[Any, Any, None]] | None = None,
    ):
        """
        Initialize hidden service listener.

        Args:
            host: Local address to bind to (typically 127.0.0.1 for Tor)
            port: Local port to bind to (0 for auto-assign)
            max_message_size: Maximum message size in bytes
            on_connection: Callback when new connection is accepted
        """
        self.host = host
        self.port = port
        self.max_message_size = max_message_size
        self.on_connection = on_connection
        self.server: asyncio.Server | None = None  # type: ignore[no-any-unimported]
        self.running = False
        self._bound_port: int = 0

    @property
    def bound_port(self) -> int:
        """Get the actual port the server is bound to."""
        return self._bound_port

    async def start(self) -> int:
        """
        Start listening for connections.

        Returns:
            The port number the server is bound to
        """
        self.server = await asyncio.start_server(
            self._handle_connection,
            self.host,
            self.port,
            limit=self.max_message_size,
        )
        self.running = True

        # Get the actual bound port
        addrs = self.server.sockets[0].getsockname() if self.server.sockets else None
        if addrs:
            self._bound_port = addrs[1]
        else:
            self._bound_port = self.port

        logger.info(f"Hidden service listener started on {self.host}:{self._bound_port}")
        return self._bound_port

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle incoming connection."""
        peer_addr = writer.get_extra_info("peername")
        peer_str = f"{peer_addr[0]}:{peer_addr[1]}" if peer_addr else "unknown"
        logger.info(f"Accepted connection from {peer_str}")

        connection = TCPConnection(reader, writer, self.max_message_size)

        if self.on_connection:
            try:
                await self.on_connection(connection, peer_str)
            except Exception as e:
                logger.error(f"Error handling connection from {peer_str}: {e}")
                await connection.close()

    async def stop(self) -> None:
        """Stop the listener."""
        self.running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        logger.info("Hidden service listener stopped")

    async def serve_forever(self) -> None:
        """Run the server until stopped."""
        if self.server:
            await self.server.serve_forever()


class PeerStatus:
    """Connection status for OnionPeer."""

    UNCONNECTED = 0
    CONNECTING = 1
    CONNECTED = 2
    HANDSHAKED = 3
    DISCONNECTED = 4


class OnionPeerError(Exception):
    """Base exception for OnionPeer errors."""


class OnionPeerConnectionError(OnionPeerError):
    """Error during connection to peer."""


class OnionPeer:
    """
    Represents a direct peer connection over Tor.

    Used by takers to establish direct connections to makers,
    bypassing the directory server for private message exchange.
    This improves privacy by preventing directories from seeing
    who is communicating with whom.

    Connection Flow:
    1. Taker gets maker's onion address from peerlist
    2. Taker creates OnionPeer and calls try_to_connect()
    3. Connection is established via Tor SOCKS proxy
    4. Handshake is performed (same protocol as directory)
    5. Messages can be sent/received directly

    State Machine:
    UNCONNECTED -> CONNECTING -> CONNECTED -> HANDSHAKED -> DISCONNECTED
                      |              |             |
                      v              v             v
                   (failure)    (failure)     (disconnect)
                      |              |             |
                      v              v             v
                  UNCONNECTED   DISCONNECTED  DISCONNECTED
    """

    def __init__(
        self,
        nick: str,
        location: str,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9050,
        timeout: float = 120.0,
        max_message_size: int = 2097152,
        on_message: Callable[[str, bytes], Coroutine[Any, Any, None]] | None = None,
        on_disconnect: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        on_handshake_complete: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        nick_identity: NickIdentity | None = None,
        socks_username: str | None = None,
        socks_password: str | None = None,
    ):
        """
        Initialize OnionPeer.

        Args:
            nick: Peer's JoinMarket nick
            location: Peer's onion address (host:port)
            socks_host: SOCKS proxy host for Tor
            socks_port: SOCKS proxy port for Tor
            timeout: Connection timeout in seconds (covers SOCKS + Tor circuit + PoW)
            max_message_size: Maximum message size in bytes
            on_message: Callback when message received (nick, data)
            on_disconnect: Callback when peer disconnects (nick)
            on_handshake_complete: Callback when handshake completes (nick)
            nick_identity: Our nick identity for signing messages (required for
                          compatibility with reference implementation)
            socks_username: SOCKS5 username for Tor stream isolation (optional)
            socks_password: SOCKS5 password for Tor stream isolation (optional)
        """
        self.nick = nick
        self.location = location
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.socks_username = socks_username
        self.socks_password = socks_password
        self.timeout = timeout
        self.max_message_size = max_message_size
        self.on_message = on_message
        self.on_disconnect = on_disconnect
        self.on_handshake_complete = on_handshake_complete
        self.nick_identity = nick_identity

        # Parse location
        self._hostname: str | None = None
        self._port: int | None = None
        self._parse_location()

        # Connection state
        self._status = PeerStatus.UNCONNECTED
        self._connection: TCPConnection | None = None
        self._receive_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._lock = asyncio.Lock()

        # Retry/backoff state
        self._connect_attempts = 0
        self._max_connect_attempts = 3
        self._base_backoff = 2.0  # seconds
        self._last_connect_attempt: float = 0.0

        # Features advertised by the peer in its handshake response. Populated
        # after a successful handshake. Empty dict means handshake did not
        # include a features field (e.g., older peers) -- callers should treat
        # this as "unknown" rather than "not supported".
        self.peer_features: dict[str, Any] = {}

    def _parse_location(self) -> None:
        """Parse location string into hostname and port."""
        if self.location == "NOT-SERVING-ONION":
            self._hostname = None
            self._port = None
            return

        try:
            host, port_str = self.location.split(":")
            self._hostname = host
            self._port = int(port_str)
        except (ValueError, AttributeError) as e:
            logger.warning(f"Invalid peer location: {self.location}: {e}")
            self._hostname = None
            self._port = None

    @property
    def hostname(self) -> str | None:
        """Get peer's hostname."""
        return self._hostname

    @property
    def port(self) -> int | None:
        """Get peer's port."""
        return self._port

    def status(self) -> int:
        """Get current connection status."""
        return self._status

    def supports_feature(self, feature: str) -> bool | None:
        """Return True/False if we know peer's support for feature, None if unknown.

        Returns:
            True if the peer advertised the feature in its handshake.
            False if the handshake response included features but not this one.
            None if no handshake has completed yet or the response had no
            features field (treat as unknown, e.g., legacy peers).
        """
        if not self.peer_features:
            return None
        return bool(self.peer_features.get(feature, False))

    def is_connected(self) -> bool:
        """Check if peer is connected and ready to send messages."""
        return self._status == PeerStatus.HANDSHAKED

    def is_connecting(self) -> bool:
        """Check if connection is in progress."""
        return self._status == PeerStatus.CONNECTING

    def can_connect(self) -> bool:
        """Check if we can attempt to connect to this peer."""
        if self._hostname is None or self._port is None:
            return False
        return self._status not in (
            PeerStatus.CONNECTING,
            PeerStatus.CONNECTED,
            PeerStatus.HANDSHAKED,
        )

    async def connect(
        self,
        our_nick: str,
        our_location: str,
        network: str,
    ) -> bool:
        """
        Connect to the peer and perform handshake.

        Args:
            our_nick: Our JoinMarket nick
            our_location: Our onion address or NOT-SERVING-ONION
            network: Bitcoin network (mainnet, testnet, signet, regtest)

        Returns:
            True if connection and handshake succeeded
        """
        async with self._lock:
            if not self.can_connect():
                logger.debug(f"Cannot connect to peer {self.nick}: status={self._status}")
                return False

            self._status = PeerStatus.CONNECTING
            self._connect_attempts += 1
            self._last_connect_attempt = asyncio.get_event_loop().time()

        try:
            logger.info(f"Connecting to peer {self.nick} at {self.location}")

            # Connect via Tor
            if self._hostname and self._hostname.endswith(".onion"):
                self._connection = await connect_via_tor(
                    self._hostname,
                    self._port or 5222,
                    self.socks_host,
                    self.socks_port,
                    self.max_message_size,
                    self.timeout,
                    socks_username=self.socks_username,
                    socks_password=self.socks_password,
                )
            else:
                # Direct connection (for testing)
                self._connection = await connect_direct(
                    self._hostname or "localhost",
                    self._port or 5222,
                    self.max_message_size,
                    self.timeout,
                )

            async with self._lock:
                self._status = PeerStatus.CONNECTED

            # Perform handshake
            await self._handshake(our_nick, our_location, network)

            async with self._lock:
                self._status = PeerStatus.HANDSHAKED
                self._connect_attempts = 0  # Reset on success

            logger.info(f"Connected and handshaked with peer {self.nick}")

            # Start receive loop
            self._receive_task = asyncio.create_task(self._receive_loop())

            if self.on_handshake_complete:
                await self.on_handshake_complete(self.nick)

            return True

        except Exception as e:
            logger.warning(f"Failed to connect to peer {self.nick}: {e}")
            async with self._lock:
                self._status = PeerStatus.DISCONNECTED
            if self._connection:
                await self._connection.close()
                self._connection = None
            return False

    async def _handshake(self, our_nick: str, our_location: str, network: str) -> None:
        """Perform handshake with peer (same protocol as directory)."""
        if not self._connection:
            raise OnionPeerConnectionError("Not connected")

        # Import here to avoid circular dependency
        import json

        from jmcore.protocol import (
            MessageType,
            create_handshake_request,
        )

        # Send handshake request
        # Reference implementation uses {"type": 793, "line": "<json-string>"}
        handshake = create_handshake_request(
            nick=our_nick,
            location=our_location,
            network=network,
            directory=False,
        )
        msg = json.dumps({"type": MessageType.HANDSHAKE.value, "line": json.dumps(handshake)})
        await self._connection.send(msg.encode("utf-8"))

        # Wait for handshake response
        response_data = await asyncio.wait_for(self._connection.receive(), timeout=self.timeout)
        response = json.loads(response_data.decode("utf-8"))

        if response.get("type") != MessageType.HANDSHAKE.value:
            raise OnionPeerConnectionError(f"Expected HANDSHAKE, got type {response.get('type')}")

        # Reference implementation sends {"type": 793, "line": "<json-string>"}
        line = response.get("line", "")
        try:
            data = json.loads(line) if line else {}
        except json.JSONDecodeError as e:
            raise OnionPeerConnectionError(f"Invalid handshake response: {line[:100]}") from e

        # Peer-to-peer handshake response format (different from directory response)
        # Validate the response fields
        app_name = data.get("app-name")
        proto_ver = data.get("proto-ver")
        is_directory = data.get("directory", False)
        peer_network = data.get("network")

        if app_name != "joinmarket":
            raise OnionPeerConnectionError(f"Invalid app-name: {app_name}")

        if proto_ver != 5:
            raise OnionPeerConnectionError(f"Incompatible protocol version: {proto_ver}")

        if is_directory:
            raise OnionPeerConnectionError("Expected non-directory peer")

        # Verify network matches
        if peer_network != network:
            raise OnionPeerConnectionError(
                f"Network mismatch: expected {network}, got {peer_network}"
            )

        # Record advertised features (e.g., {"neutrino_compat": True}). Used by
        # callers to filter incompatible peers before sending protocol messages.
        raw_features = data.get("features", {})
        if isinstance(raw_features, dict):
            self.peer_features = dict(raw_features)
        else:
            self.peer_features = {}

        logger.debug(f"Handshake with peer {self.nick} successful")

    async def _receive_loop(self) -> None:
        """Background task to receive messages from peer."""
        if not self._connection:
            return

        try:
            while self._status == PeerStatus.HANDSHAKED and self._connection.is_connected():
                try:
                    data = await self._connection.receive()
                    if self.on_message:
                        await self.on_message(self.nick, data)
                except ConnectionError:
                    break
                except Exception as e:
                    logger.warning(f"Error receiving from peer {self.nick}: {e}")
                    break
        finally:
            await self._handle_disconnect()

    async def _handle_disconnect(self) -> None:
        """Handle peer disconnection."""
        async with self._lock:
            if self._status == PeerStatus.DISCONNECTED:
                return
            self._status = PeerStatus.DISCONNECTED

        logger.info(f"Peer {self.nick} disconnected")

        if self._connection:
            await self._connection.close()
            self._connection = None

        if self.on_disconnect:
            await self.on_disconnect(self.nick)

    async def send(self, data: bytes) -> bool:
        """
        Send data to peer.

        Args:
            data: Raw message bytes to send

        Returns:
            True if send succeeded
        """
        if not self.is_connected() or not self._connection:
            return False

        try:
            await self._connection.send(data)
            return True
        except Exception as e:
            logger.warning(f"Failed to send to peer {self.nick}: {e}")
            await self._handle_disconnect()
            return False

    async def send_privmsg(self, our_nick: str, command: str, message: str) -> bool:
        """
        Send a private message to peer.

        Messages are signed with our nick identity for authentication.
        The reference implementation verifies all private messages, whether
        received via directory relay or direct peer connection.

        Args:
            our_nick: Our JoinMarket nick
            command: Command name (e.g., "fill", "pubkey")
            message: Message content (will be signed if nick_identity is set)

        Returns:
            True if send succeeded
        """
        import json

        from jmcore.protocol import MessageType, format_jm_message

        # Sign message if we have nick identity
        # Reference implementation expects: "<command> <message> <pubkey_hex> <sig_b64>"
        # where signature is over: message + ONION_HOSTID
        if self.nick_identity:
            signed_message = self.nick_identity.sign_message(message, ONION_HOSTID)
        else:
            # No signature - will fail with reference implementation
            # but may work with our own maker
            signed_message = message

        # Format: from_nick!to_nick!command message
        jm_msg = format_jm_message(our_nick, self.nick, command, signed_message)
        msg = json.dumps({"type": MessageType.PRIVMSG.value, "line": jm_msg})
        return await self.send(msg.encode("utf-8"))

    async def disconnect(self) -> None:
        """Disconnect from peer."""
        if self._receive_task:
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
            self._receive_task = None

        await self._handle_disconnect()

    def try_to_connect(
        self,
        our_nick: str,
        our_location: str,
        network: str,
    ) -> asyncio.Task | None:  # type: ignore[type-arg]
        """
        Try to connect to peer asynchronously (non-blocking).

        This method is called opportunistically when we want to send
        a message but don't have a direct connection yet. The message
        is sent via directory relay, but we start a background connection
        for future messages.

        Args:
            our_nick: Our JoinMarket nick
            our_location: Our onion address
            network: Bitcoin network

        Returns:
            Task if connection attempt started, None if skipped
        """
        if not self.can_connect():
            return None

        # Check backoff
        now = asyncio.get_event_loop().time()
        if self._connect_attempts > 0:
            backoff = self._base_backoff * (2 ** min(self._connect_attempts - 1, 5))
            if now - self._last_connect_attempt < backoff:
                logger.debug(
                    f"Skipping connect to {self.nick}: backoff {backoff:.1f}s "
                    f"(attempt {self._connect_attempts})"
                )
                return None

        # Check max attempts
        if self._connect_attempts >= self._max_connect_attempts:
            logger.debug(f"Giving up on peer {self.nick} after {self._connect_attempts} attempts")
            return None

        return asyncio.create_task(
            self.connect(our_nick, our_location, network),
            name=f"peer_connect_{self.nick}",
        )

    def __repr__(self) -> str:
        return f"OnionPeer(nick={self.nick!r}, location={self.location!r}, status={self._status})"
