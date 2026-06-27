"""
Multi-directory client for managing connections to multiple directory servers.

Provides a unified interface for connecting to multiple directory servers
and aggregating orderbook data. Implements multi-directory aware nick
tracking - a nick is only considered "gone" when ALL directories report
it as disconnected.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any

from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.deduplication import ResponseDeduplicator
from jmcore.directory_client import DirectoryClient
from jmcore.directory_pool import DirectoryClientPool
from jmcore.models import Offer
from jmcore.network import ONION_HOSTID, OnionPeer
from jmcore.protocol import NOT_SERVING_ONION_HOSTNAME, parse_jm_message
from loguru import logger


@dataclass(frozen=True)
class ChannelBinding:
    """
    Resolved transport binding for sending privmsgs to a single nick.

    Returned by :meth:`MultiDirectoryClient.bind_session` and consumed as
    the ``force_channel`` argument of :meth:`send_privmsg`. Callers should
    cache the binding for the lifetime of a session (one CoinJoin) so all
    messages to that nick travel the same channel, since makers reject
    mixed-channel sessions.

    Attributes:
        nick: The remote nick this binding targets.
        channel_id: Either ``"direct"`` (use the established onion-to-onion
            peer connection) or ``"directory:<server>"`` to relay through
            a specific directory server.
        peer_location: Optional human-readable onion address of the peer,
            for logging purposes only.
    """

    nick: str
    channel_id: str
    peer_location: str | None = None

    @property
    def is_direct(self) -> bool:
        """True if this binding routes through a direct peer connection."""
        return self.channel_id == "direct"


class MultiDirectoryClient(DirectoryClientPool):
    """
    Wrapper for managing multiple DirectoryClient connections.

    Provides a unified interface for connecting to multiple directory servers
    and aggregating orderbook data. Implements multi-directory aware nick
    tracking - a nick is only considered "gone" when ALL directories report
    it as disconnected.

    Direct Peer Connections:
    When enabled (prefer_direct_connections=True), the client will establish
    direct Tor connections to makers when possible, bypassing directory servers
    for private messages. This improves privacy by preventing directories from
    observing who is communicating with whom.

    Connection flow:
    1. First message to a maker goes via directory relay
    2. Opportunistically starts direct connection in background
    3. Subsequent messages prefer direct connection if available
    4. Falls back to directory relay if direct connection fails

    This prevents premature maker removal when:
    - A maker temporarily disconnects from one directory but remains on others
    - Directory connections are flaky or experiencing network issues
    - There's a race condition between directory updates

    Reference: JoinMarket onionmc.py lines 1078-1103
    """

    def __init__(
        self,
        directory_servers: list[str],
        network: str,
        nick_identity: NickIdentity,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9050,
        connection_timeout: float = 120.0,
        neutrino_compat: bool = False,
        on_nick_leave: Any | None = None,
        prefer_direct_connections: bool = True,
        our_location: str = "NOT-SERVING-ONION",
        stream_isolation: bool = False,
    ):
        # Connection / SOCKS / credential setup is delegated to the
        # DirectoryClientPool base; it handles directory_servers, network,
        # nick_identity, SOCKS params, connection_timeout, stream_isolation,
        # the clients dict, and _dir_creds / _peer_creds.
        super().__init__(
            directory_servers=directory_servers,
            network=network,
            nick_identity=nick_identity,
            socks_host=socks_host,
            socks_port=socks_port,
            connection_timeout=connection_timeout,
            stream_isolation=stream_isolation,
        )

        # Taker-specific state below.
        self.nick = nick_identity.nick
        self.neutrino_compat = neutrino_compat
        self.on_nick_leave = on_nick_leave

        # Direct peer connection settings
        self.prefer_direct_connections = prefer_direct_connections
        self.our_location = our_location
        # Peer connections indexed by nick
        self._peer_connections: dict[str, OnionPeer] = {}
        # Background tasks for pending connections
        self._pending_connect_tasks: dict[str, asyncio.Task[bool]] = {}

        # Unified message queue for direct peer messages
        # Messages from direct peers are queued here and consumed by wait_for_responses
        self._direct_message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        # Multi-directory nick tracking
        # Format: active_nicks[nick] = {server1: True, server2: True, ...}
        # True = nick is present on this server, False = gone from this server
        # A nick is only considered completely gone when ALL servers report False
        self._active_nicks: dict[str, dict[str, bool]] = {}

    def _build_client_kwargs(self, host: str, port: int) -> dict[str, Any]:
        """Inject the taker's ``neutrino_compat`` flag into the base kwargs."""
        kwargs = super()._build_client_kwargs(host, port)
        kwargs["neutrino_compat"] = self.neutrino_compat
        return kwargs

    def _update_nick_status(self, nick: str, server: str, is_present: bool) -> None:
        """
        Update a nick's presence status on a specific directory server.

        If this causes the nick to become completely gone (absent from ALL servers),
        triggers the on_nick_leave callback.
        """
        if nick not in self._active_nicks:
            self._active_nicks[nick] = {}

        old_status = self._active_nicks[nick].get(server)
        self._active_nicks[nick][server] = is_present

        # Check if this update causes the nick to be completely gone
        if not is_present and old_status is True:
            # Nick just disappeared from this directory
            # Check if it's still present on any other directory
            if not any(status for status in self._active_nicks[nick].values()):
                logger.info(
                    f"Nick {nick} has left all directories "
                    f"(servers: {list(self._active_nicks[nick].keys())})"
                )
                if self.on_nick_leave:
                    self.on_nick_leave(nick)
                # Clean up the entry
                del self._active_nicks[nick]
        elif is_present and old_status is False:
            logger.debug(f"Nick {nick} returned to server {server}")

    def is_nick_active(self, nick: str) -> bool:
        """
        Check if a nick is active on at least one directory server.

        Returns:
            True if nick is present on at least one server
        """
        if nick not in self._active_nicks:
            return False
        return any(status for status in self._active_nicks[nick].values())

    def sync_nicks_with_peerlist(self, server: str, active_nicks: set[str]) -> None:
        """
        Synchronize nick tracking with a directory's peerlist.

        This is called after fetching a peerlist from a directory to update
        the nick tracking state. Nicks not in the peerlist are marked as gone
        from that directory.

        Args:
            server: The server identifier reporting the peerlist
            active_nicks: Set of nicks currently active on this server
        """
        # Mark all nicks in the peerlist as present
        for nick in active_nicks:
            self._update_nick_status(nick, server, True)

        # Mark nicks we're tracking but not in this peerlist as gone from this server
        for nick in list(self._active_nicks.keys()):
            if server in self._active_nicks[nick] and nick not in active_nicks:
                self._update_nick_status(nick, server, False)

    # =========================================================================
    # Direct Peer Connection Methods
    # =========================================================================

    def _get_peer_location(self, nick: str) -> str | None:
        """
        Get a maker's onion location from the peerlist.

        Args:
            nick: Maker's JoinMarket nick

        Returns:
            Onion address (host:port) or None if not found/not serving
        """
        for client in self.clients.values():
            location = client._active_peers.get(nick)
            if location and location != NOT_SERVING_ONION_HOSTNAME:
                return location
        return None

    def _should_try_direct_connect(self, nick: str) -> bool:
        """
        Check if we should attempt a direct connection to this peer.

        Returns False if:
        - Direct connections are disabled
        - We already have a connected peer
        - Peer doesn't serve an onion address
        - Connection attempt is already in progress
        """
        if not self.prefer_direct_connections:
            return False

        # Already connected?
        if nick in self._peer_connections:
            peer = self._peer_connections[nick]
            if peer.is_connected() or peer.is_connecting():
                return False

        # Connection attempt in progress?
        if nick in self._pending_connect_tasks:
            task = self._pending_connect_tasks[nick]
            if not task.done():
                return False

        # Has a valid onion address?
        location = self._get_peer_location(nick)
        return location is not None

    def _get_connected_peer(self, nick: str) -> OnionPeer | None:
        """
        Get a connected peer by nick.

        Returns:
            OnionPeer if connected and handshaked, None otherwise
        """
        peer = self._peer_connections.get(nick)
        if peer and peer.is_connected():
            return peer
        return None

    def bind_session(self, nick: str) -> ChannelBinding | None:
        """
        Pick and pin a single transport channel for a session with ``nick``.

        Encapsulates the channel-selection algorithm that callers
        previously open-coded by reaching into private attributes
        (``_get_connected_peer``, ``_active_nicks``, ``clients``,
        ``_active_peers``). The order is:

        1. If a direct peer connection is established and direct
           connections are preferred, bind to ``"direct"``.
        2. Otherwise, prefer a directory server that our nick-tracking
           layer marks as currently carrying ``nick``.
        3. Otherwise, fall back to any directory whose peerlist lists the
           nick.
        4. Otherwise, fall back to an arbitrary connected directory.

        Returns ``None`` only when no directories are connected, in which
        case the caller cannot send anything anyway.

        The returned :class:`ChannelBinding` should be reused for every
        subsequent message to ``nick`` in the same session: makers reject
        sessions whose ``!fill`` and ``!auth`` arrive via different
        channels.
        """
        peer = self._get_connected_peer(nick)
        peer_location = self._get_peer_location(nick)
        if peer is not None and self.prefer_direct_connections:
            return ChannelBinding(
                nick=nick,
                channel_id="direct",
                peer_location=peer_location,
            )

        # Prefer directories that have explicitly tracked this nick as active.
        target_directories: list[str] = []
        if nick in self._active_nicks:
            for server, is_active in self._active_nicks[nick].items():
                if is_active and server in self.clients:
                    target_directories.append(server)

        # Fall back to directories whose peerlist lists this nick.
        if not target_directories:
            for server, client in self.clients.items():
                if nick in client._active_peers:
                    target_directories.append(server)

        # Last-resort fallback: any connected directory.
        if not target_directories:
            target_directories = list(self.clients.keys())

        if not target_directories:
            return None

        chosen = target_directories[0]
        return ChannelBinding(
            nick=nick,
            channel_id=f"directory:{chosen}",
            peer_location=peer_location,
        )

    def upgrade_channel_prefer_direct(self, nick: str, current_channel: str) -> str:
        """Opportunistically upgrade a session channel to a direct connection.

        A session pins one channel before sending ``!fill`` (see
        :meth:`bind_session`). When ``!fill`` is sent via a directory while a
        direct connection is still being established, that connection often
        finishes handshaking before the taker sends ``!auth``/``!tx``. This
        mirrors the reference taker, which routes each privmsg
        opportunistically (``jmdaemon/onionmc.py::_privmsg``): once the direct
        peer is handshaked, later messages travel directly.

        Makers accept such mid-session directory->direct switches (the signed
        ``hostid`` is the fixed ``onion-network`` for all onion transports), so
        upgrading improves privacy and latency without breaking compatibility.

        We only ever upgrade directory->direct, never the reverse: if the
        session is already direct we keep it, and we never downgrade a direct
        session to a directory relay here.

        Args:
            nick: The maker nick whose session channel to re-evaluate.
            current_channel: The channel currently pinned for the session
                (``"direct"`` or ``"directory:<server>"``).

        Returns:
            ``"direct"`` if a handshaked direct connection is now available,
            otherwise ``current_channel`` unchanged.
        """
        if not self.prefer_direct_connections:
            return current_channel
        if current_channel == "direct":
            return current_channel
        if self._get_connected_peer(nick) is not None:
            logger.debug(
                f"Upgrading session channel for {nick} from "
                f"'{current_channel}' to 'direct' (direct connection now ready)"
            )
            return "direct"
        return current_channel

    def try_direct_connect(self, nick: str) -> None:
        """
        Public alias for ``_try_direct_connect``.

        Kicks off an opportunistic background connection attempt to
        ``nick``. Safe to call repeatedly: the underlying method is a
        no-op when a connection (or pending task) already exists.
        """
        self._try_direct_connect(nick)

    def get_peer_location(self, nick: str) -> str | None:
        """Public alias for ``_get_peer_location``."""
        return self._get_peer_location(nick)

    def get_connected_peer(self, nick: str) -> OnionPeer | None:
        """Public alias for ``_get_connected_peer``."""
        return self._get_connected_peer(nick)

    def get_pending_connect_task(self, nick: str) -> asyncio.Task[bool] | None:
        """Return the in-flight direct-connect task for ``nick``, if any."""
        return self._pending_connect_tasks.get(nick)

    async def _on_peer_message(self, nick: str, data: bytes) -> None:
        """
        Handle message received from a direct peer connection.

        Messages are forwarded to the unified direct message queue for processing
        by wait_for_responses(). The message is enriched with the sender's nick
        to match the format expected by the response processing logic.
        """
        try:
            import json

            msg = json.loads(data.decode("utf-8"))
            logger.debug(f"Received direct message from {nick}: type={msg.get('type')}")

            # Enrich message with sender nick for wait_for_responses to identify
            msg["from_nick"] = nick
            msg["from_direct"] = True

            # Queue for processing by wait_for_responses
            await self._direct_message_queue.put(msg)
        except Exception as e:
            logger.warning(f"Error processing peer message from {nick}: {e}")

    async def _on_peer_disconnect(self, nick: str) -> None:
        """Handle peer disconnection."""
        logger.debug(f"Peer {nick} disconnected")
        # Clean up but don't remove from _peer_connections immediately
        # in case we want to reconnect

    async def _on_peer_handshake_complete(self, nick: str) -> None:
        """Handle successful peer handshake."""
        logger.info(f"Direct connection established with {nick}")

    def _try_direct_connect(self, nick: str) -> None:
        """
        Opportunistically try to establish a direct connection to a maker.

        This is called asynchronously when sending a message via directory relay.
        The connection attempt runs in the background and future messages will
        use the direct connection if it succeeds.
        """
        if not self._should_try_direct_connect(nick):
            return

        location = self._get_peer_location(nick)
        if not location:
            return

        # Create peer if needed
        if nick not in self._peer_connections:
            peer = OnionPeer(
                nick=nick,
                location=location,
                socks_host=self.socks_host,
                socks_port=self.socks_port,
                timeout=self.connection_timeout,
                on_message=self._on_peer_message,
                on_disconnect=self._on_peer_disconnect,
                on_handshake_complete=self._on_peer_handshake_complete,
                nick_identity=self.nick_identity,
                socks_username=self._peer_creds[0],
                socks_password=self._peer_creds[1],
            )
            self._peer_connections[nick] = peer
        else:
            peer = self._peer_connections[nick]

        # Start connection in background
        task = peer.try_to_connect(
            our_nick=self.nick,
            our_location=self.our_location,
            network=self.network,
        )
        if task:
            self._pending_connect_tasks[nick] = task
            logger.debug(f"Started background connection to {nick} at {location}")

    async def _cleanup_peer_connections(self) -> None:
        """Clean up all peer connections (called on close)."""
        # Cancel pending connection tasks
        for nick, task in self._pending_connect_tasks.items():
            if not task.done():
                task.cancel()
        self._pending_connect_tasks.clear()

        # Disconnect all peers
        for nick, peer in self._peer_connections.items():
            try:
                await peer.disconnect()
            except Exception as e:
                logger.debug(f"Error disconnecting from peer {nick}: {e}")
        self._peer_connections.clear()

    async def connect_all(self) -> int:
        """Connect to all directory servers in parallel.

        Thin compatibility wrapper around
        :meth:`DirectoryClientPool.connect_all_parallel` that preserves
        the historical name used by the taker codebase.
        """
        return await self.connect_all_parallel()

    async def close_all(self) -> None:
        """Close all directory and peer connections.

        Peer (direct onion) connections are torn down first so any
        outgoing per-peer messages have a chance to flush before we
        close the relay channels. Directory client teardown is handled
        by :meth:`DirectoryClientPool.close_all`.
        """
        await self._cleanup_peer_connections()
        await super().close_all()

    async def fetch_orderbook(
        self,
        max_wait: float = 120.0,
        min_wait: float = 30.0,
        quiet_period: float = 15.0,
    ) -> list[Offer]:
        """
        Fetch orderbook from all connected directory servers in parallel.

        Trusts the directory's orderbook as authoritative - if a maker has an offer
        in the directory, they are considered online. This avoids incorrectly filtering
        offers as "stale" based on slow peerlist responses.

        Args:
            max_wait: Hard ceiling in seconds (default: 120s).
            min_wait: Minimum seconds before early exit is allowed (default: 30s).
            quiet_period: Seconds of silence before exiting early (default: 15s).
        """
        all_offers: list[Offer] = []
        seen_offers: set[tuple[str, int]] = set()

        async def fetch_from_server(
            server: str, client: DirectoryClient
        ) -> tuple[str, list[Offer]]:
            """Fetch offers from a single directory server."""
            try:
                offers, _bonds = await client.fetch_orderbooks(
                    max_wait=max_wait, min_wait=min_wait, quiet_period=quiet_period
                )
                return (server, offers)
            except Exception as e:
                logger.warning(f"Failed to fetch orderbook from {server}: {e}")
                return (server, [])

        # Fetch from all directories in parallel
        tasks = [fetch_from_server(server, client) for server, client in self.clients.items()]
        results = await asyncio.gather(*tasks)

        # Aggregate and deduplicate offers
        for server, offers in results:
            for offer in offers:
                key = (offer.counterparty, offer.oid)
                if key not in seen_offers:
                    seen_offers.add(key)
                    all_offers.append(offer)

        return all_offers

    async def send_privmsg(
        self,
        recipient: str,
        command: str,
        data: str,
        log_routing: bool = False,
        force_channel: str | None = None,
    ) -> str:
        """Send a private message, respecting channel consistency for CoinJoin sessions.

        CRITICAL: Within a single CoinJoin session, all messages to a maker MUST use the
        same communication channel (either direct or a specific directory). Mixing channels
        causes the maker to reject messages as they appear to be from different sessions.

        Message routing priority (when force_channel is None):
        1. Direct peer connection (if connected and prefer_direct_connections=True)
        2. Directory relay (fallback)

        Args:
            recipient: Target maker nick
            command: Command name (without ! prefix)
            data: Command arguments
            log_routing: If True, log detailed routing information
            force_channel: If set, only use this channel:
                - "direct" = peer-to-peer onion connection
                - "directory:<host>:<port>" = relay through specific directory

        Returns:
            Channel used: "direct" or "directory:<host>:<port>"
        """
        # Get maker's direct onion location if available
        maker_location = self._get_peer_location(recipient)

        # If force_channel is set, use only that channel
        if force_channel:
            if force_channel == "direct":
                peer = self._get_connected_peer(recipient)
                if not peer:
                    raise RuntimeError(
                        f"Forced to use direct channel but no connection to {recipient}"
                    )
                success = await peer.send_privmsg(self.nick, command, data)
                if not success:
                    raise RuntimeError(f"Failed to send to {recipient} via direct connection")
                if log_routing:
                    logger.debug(
                        f"Sent !{command} to {recipient} via DIRECT connection "
                        f"(onion: {maker_location})"
                    )
                return "direct"
            elif force_channel.startswith("directory:"):
                # Extract host:port from "directory:host:port"
                server = force_channel[10:]  # Skip "directory:"
                client = self.clients.get(server)
                if not client:
                    raise RuntimeError(f"Forced to use directory {server} but not connected")
                await client.send_private_message(recipient, command, data)
                if log_routing:
                    logger.debug(
                        f"Sent !{command} to {recipient} via directory {server} "
                        f"(maker onion: {maker_location}, using relay)"
                    )
                return force_channel
            else:
                raise ValueError(f"Invalid force_channel: {force_channel}")

        # No forced channel - choose best available
        # Try direct connection first if available
        if self.prefer_direct_connections:
            peer = self._get_connected_peer(recipient)
            if peer:
                try:
                    success = await peer.send_privmsg(self.nick, command, data)
                    if success:
                        if log_routing:
                            logger.debug(
                                f"Sent !{command} to {recipient} via DIRECT connection "
                                f"(onion: {maker_location})"
                            )
                        return "direct"
                except Exception as e:
                    logger.debug(f"Direct send to {recipient} failed: {e}")

        # Fall back to directory relay
        # Opportunistically start direct connection for future messages
        if self.prefer_direct_connections and maker_location:
            self._try_direct_connect(recipient)

        # Identify valid directories for this recipient
        target_directories = []

        # Check active nicks tracking first
        if recipient in self._active_nicks:
            for server, is_active in self._active_nicks[recipient].items():
                if is_active and server in self.clients:
                    target_directories.append(server)

        # If not found in tracking (e.g. startup race), try all clients that list the peer
        if not target_directories:
            for server, client in self.clients.items():
                if recipient in client._active_peers:
                    target_directories.append(server)

        # If still not found, fall back to all connected clients (broadcast)
        if not target_directories:
            target_directories = list(self.clients.keys())

        # Shuffle to load balance
        random.shuffle(target_directories)

        # Send via the first working directory
        # We strictly send to ONE directory to avoid message duplication
        for server in target_directories:
            client = self.clients.get(server)
            if not client:
                continue

            try:
                await client.send_private_message(recipient, command, data)
                if log_routing:
                    directory = f"{client.host}:{client.port}"
                    if maker_location:
                        logger.debug(
                            f"Sent !{command} to {recipient} via directory {directory} "
                            f"(maker onion: {maker_location}, using relay)"
                        )
                    else:
                        logger.debug(f"Sent !{command} to {recipient} via directory {directory}")
                # Success - return the channel used
                return f"directory:{server}"
            except Exception as e:
                logger.warning(f"Failed to send privmsg via {server}: {e}")

        raise RuntimeError(f"Failed to send !{command} to {recipient} via any directory")

    async def wait_for_responses(
        self,
        expected_nicks: list[str],
        expected_command: str,
        timeout: float = 60.0,
        expected_counts: dict[str, int] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Wait for responses from multiple makers at once.

        Listens for responses from BOTH:
        - Directory server message streams (via client.listen_for_messages())
        - Direct peer connections (via self._direct_message_queue)

        Returns a dict of nick -> response data for all makers that responded.
        Responses can include:
        - Normal responses matching expected_command
        - Error responses marked with "error": True

        Error handling:
        - Makers may send !error messages instead of the expected response
        - These indicate protocol failures (e.g., blacklisted PoDLE commitment)
        - Errors are returned in the response dict with {"error": True, "data": "reason"}

        Deduplication:
        - When connected to multiple directory servers, the same response may arrive
          multiple times. ResponseDeduplicator tracks which responses we've seen
          and logs duplicates for debugging.

        Special handling for !sig:
        - Makers send multiple !sig messages (one per UTXO)
        - We accumulate all messages in a list instead of keeping just the last one
        - Use expected_counts to specify how many signatures to expect per maker
        - Returns as soon as all expected signatures are received

        Args:
            expected_nicks: List of maker nicks to expect responses from
            expected_command: Command to wait for (e.g., "!pubkey", "!sig")
            timeout: Maximum time to wait in seconds
            expected_counts: For !sig, dict of nick -> expected signature count
        """
        # Track if this command expects multiple messages per maker
        accumulate_responses = expected_command == "!sig"

        responses: dict[str, dict[str, Any]] = {}
        remaining_nicks = set(expected_nicks)
        deduplicator = ResponseDeduplicator()
        # For !sig accumulation: track seen data per nick to drop cross-directory
        # duplicates (the same signature relayed by multiple directory servers).
        seen_sig_data: dict[str, set[str]] = {}
        start_time = asyncio.get_event_loop().time()

        def is_complete() -> bool:
            """Check if we have all expected responses."""
            if remaining_nicks:
                return False
            if accumulate_responses and expected_counts:
                # For !sig, check if we have all expected signatures
                for nick, expected in expected_counts.items():
                    if nick not in responses:
                        return False
                    received = len(responses[nick].get("data", []))
                    if received < expected:
                        return False
            return True

        def process_message(msg: dict[str, Any], source: str) -> None:
            """Process a single message from any source (directory or direct)."""
            nonlocal responses, remaining_nicks

            line = msg.get("line", "")
            if not line:
                return

            # Attribute strictly to the authenticated sender. Substring matching
            # let any peer claim another maker's nick by embedding it in payload.
            parsed = parse_jm_message(line)
            if parsed is None:
                return
            from_nick = parsed[0]
            if from_nick not in expected_nicks:
                return

            ok, command, _data = verify_signed_privmsg(from_nick, parsed[2], ONION_HOSTID)
            if not ok:
                logger.warning(f"Dropping unverified message from {from_nick} via {source}")
                return

            # Preserve the downstream payload contract (data plus pubkey/sig suffix).
            payload = parsed[2].split(" ", 1)[1].strip() if " " in parsed[2] else ""

            if command == "error":
                if not deduplicator.add_response(from_nick, "error", line, source):
                    return
                responses[from_nick] = {"error": True, "data": _data or "Unknown error"}
                remaining_nicks.discard(from_nick)
                logger.warning(f"Received error from {from_nick}: {_data}")
                return

            if command != expected_command.lstrip("!"):
                return

            if not accumulate_responses:
                if not deduplicator.add_response(from_nick, expected_command, line, source):
                    logger.debug(f"Duplicate {expected_command} from {from_nick} via {source}")
                    return
                responses[from_nick] = {"data": payload}
                remaining_nicks.discard(from_nick)
                logger.debug(f"Received {expected_command} from {from_nick} via {source}")
            else:
                # Accumulate !sig messages, deduplicating identical content relayed
                # by multiple directories.
                nick_seen = seen_sig_data.setdefault(from_nick, set())
                if payload in nick_seen:
                    return
                nick_seen.add(payload)
                if from_nick not in responses:
                    responses[from_nick] = {"data": []}
                    remaining_nicks.discard(from_nick)
                responses[from_nick]["data"].append(payload)
                logger.debug(
                    f"Received {expected_command} #{len(responses[from_nick]['data'])} "
                    f"from {from_nick} via {source}"
                )

        while not is_complete():
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= timeout:
                if not accumulate_responses:
                    logger.warning(
                        f"Timeout waiting for {expected_command} from: {remaining_nicks}"
                    )
                elif expected_counts:
                    # Log which makers haven't sent all signatures
                    for nick, expected in expected_counts.items():
                        received = len(responses.get(nick, {}).get("data", []))
                        if received < expected:
                            logger.warning(f"Timeout: {nick} sent {received}/{expected} signatures")
                break

            remaining_time = min(5.0, timeout - elapsed)  # Listen in 5s chunks

            # First, drain any pending direct peer messages (non-blocking)
            while True:
                try:
                    msg = self._direct_message_queue.get_nowait()
                    process_message(msg, "direct")
                except asyncio.QueueEmpty:
                    break

            # Check if we have everything after processing direct messages
            if is_complete():
                break

            # Listen to all directory clients concurrently for shorter duration
            # Use 1s chunks to allow more frequent checking of direct message queue
            listen_duration = min(1.0, remaining_time)

            async def listen_to_client(
                server: str, client: DirectoryClient
            ) -> list[tuple[str, dict[str, Any]]]:
                try:
                    messages = await client.listen_for_messages(duration=listen_duration)
                    return [(server, msg) for msg in messages]
                except Exception as e:
                    logger.debug(f"Error listening to {server}: {e}")
                    return []

            # Gather messages from all directories concurrently
            results = await asyncio.gather(
                *[listen_to_client(s, c) for s, c in self.clients.items()]
            )
            for result_list in results:
                for server, msg in result_list:
                    process_message(msg, f"directory:{server}")

        # Log deduplication stats if there were duplicates
        stats = deduplicator.stats
        if stats.duplicates_dropped > 0:
            logger.debug(
                f"Response deduplication: {stats.unique_messages} unique, "
                f"{stats.duplicates_dropped} duplicates dropped "
                f"({stats.duplicate_rate:.1f}% duplicate rate)"
            )

        return responses

    async def wait_for_response(
        self,
        from_nick: str,
        expected_command: str,
        timeout: float = 30.0,
    ) -> dict[str, Any] | None:
        """Wait for a specific response from a maker (legacy method)."""
        responses = await self.wait_for_responses([from_nick], expected_command, timeout)
        return responses.get(from_nick)
