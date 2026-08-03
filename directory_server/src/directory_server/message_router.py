"""
Message routing logic for forwarding messages between peers.

Implements Single Responsibility Principle: only handles message routing.
"""

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Iterator

from jmcore.models import MessageEnvelope, NetworkType, PeerInfo, PeerStatus
from jmcore.protocol import FeatureSet, MessageType, create_peerlist_entry, parse_jm_message
from loguru import logger

from directory_server.peer_registry import PeerRegistry

SendCallback = Callable[..., Awaitable[None]]
FailedSendCallback = Callable[..., Awaitable[None]]
PongCallback = Callable[..., None]
BroadcastTarget = tuple[str, str | None] | tuple[str, str | None, str]

# Default batch size for concurrent broadcasts to limit memory usage
# This can be overridden via Settings.broadcast_batch_size
DEFAULT_BROADCAST_BATCH_SIZE = 50


class MessageRouter:
    def __init__(
        self,
        peer_registry: PeerRegistry,
        send_callback: SendCallback,
        broadcast_batch_size: int = DEFAULT_BROADCAST_BATCH_SIZE,
        on_send_failed: FailedSendCallback | None = None,
        on_pong: PongCallback | None = None,
    ):
        self.peer_registry = peer_registry
        self.send_callback = send_callback
        self.broadcast_batch_size = broadcast_batch_size
        self.on_send_failed = on_send_failed
        self.on_pong = on_pong
        # Track peers that failed during current operation to avoid repeated attempts
        self._failed_peers: set[tuple[str, str]] = set()
        # Offers belong to a connection generation, not just a reusable peer key.
        self._peer_offers: dict[tuple[str, str], set[str]] = {}

    async def route_message(
        self,
        envelope: MessageEnvelope,
        from_key: str,
        connection_id: str | None = None,
    ) -> None:
        connection_id = connection_id or self.peer_registry.get_connection_id(from_key)
        peer = self.peer_registry.get_by_key(from_key)
        if (
            connection_id is None
            or peer is None
            or peer.status != PeerStatus.HANDSHAKED
            or not self.peer_registry.is_current_owner(from_key, connection_id)
        ):
            logger.warning(f"Dropping message from stale or unhandshaked peer: {from_key}")
            return
        if envelope.message_type == MessageType.PUBMSG:
            await self._handle_public_message(envelope, from_key, connection_id)
        elif envelope.message_type == MessageType.PRIVMSG:
            await self._handle_private_message(envelope, from_key, connection_id)
        elif envelope.message_type == MessageType.GETPEERLIST:
            await self._handle_peerlist_request(from_key, connection_id)
        elif envelope.message_type == MessageType.PING:
            await self._handle_ping(from_key, connection_id)
        elif envelope.message_type == MessageType.PONG:
            self._handle_pong(from_key, connection_id)
        else:
            logger.debug(f"Unhandled message type: {envelope.message_type}")

    async def _handle_public_message(
        self,
        envelope: MessageEnvelope,
        from_key: str,
        connection_id: str | None = None,
    ) -> None:
        connection_id = connection_id or self.peer_registry.get_connection_id(from_key)
        if connection_id is None or not self.peer_registry.is_current_owner(
            from_key, connection_id
        ):
            return
        parsed = parse_jm_message(envelope.payload)
        if not parsed:
            logger.warning("Invalid public message format")
            return

        from_nick, to_nick, rest = parsed
        if to_nick != "PUBLIC":
            logger.warning(f"Public message not addressed to PUBLIC: {to_nick}")
            return

        from_peer = self.peer_registry.get_by_key(from_key)
        if not from_peer:
            logger.warning(f"Unknown peer sending public message: {from_key}")
            return
        if from_nick != from_peer.nick:
            logger.warning(
                f"Dropping public message claiming {from_nick} from connection {from_peer.nick}"
            )
            return

        # Track offers (absorder, absoffer, reloffer, relorder)
        if rest:
            message_parts = rest.split()
            if (
                message_parts
                and message_parts[0]
                in (
                    "!absorder",
                    "!absoffer",
                    "!reloffer",
                    "!relorder",
                    "sw0absorder",
                    "sw0absoffer",
                    "sw0reloffer",
                    "sw0relorder",
                )
                and len(message_parts) >= 2
            ):
                # Extract order ID (second field in offer messages)
                try:
                    order_id = message_parts[1]
                    offer_owner = (from_key, connection_id)
                    if offer_owner not in self._peer_offers:
                        self._peer_offers[offer_owner] = set()
                    self._peer_offers[offer_owner].add(order_id)
                    logger.trace(
                        f"Tracked offer {order_id} from {from_nick} "
                        f"(total offers: {len(self._peer_offers[offer_owner])})"
                    )
                except (ValueError, IndexError):
                    pass

        # Pre-serialize envelope once instead of per-peer
        envelope_bytes = envelope.to_bytes()

        # Use generator to avoid building full target list in memory
        def target_generator() -> Iterator[BroadcastTarget]:
            for peer_key, peer, target_connection_id in self.peer_registry.iter_connected_owners(
                from_peer.network
            ):
                if peer_key != from_key:
                    yield (peer_key, peer.nick, target_connection_id)

        # Execute sends in batches to limit memory usage
        sent_count = await self._batched_broadcast_iter(target_generator(), envelope_bytes)

        logger.trace(f"Broadcasted public message from {from_nick} to {sent_count} peers")

    async def _safe_send(
        self,
        peer_key: str,
        data: bytes,
        nick: str | None = None,
        expected_connection_id: str | None = None,
    ) -> None:
        """Send with exception handling to prevent one failed send from affecting others."""
        expected_connection_id = expected_connection_id or self.peer_registry.get_connection_id(
            peer_key
        )
        if expected_connection_id is None:
            legacy_failure = (peer_key, "")
            if legacy_failure in self._failed_peers:
                return
            try:
                await self.send_callback(peer_key, data)
            except Exception as e:
                logger.warning(f"Failed to send to {nick or peer_key}: {e}")
                self._failed_peers.add(legacy_failure)
                if self.on_send_failed:
                    with contextlib.suppress(Exception):
                        await self.on_send_failed(peer_key)
            return
        failed_owner = (peer_key, expected_connection_id)
        # Skip if this peer already failed in current operation
        if failed_owner in self._failed_peers:
            return
        peer = self.peer_registry.get_by_key(peer_key)
        if (
            peer is None
            or peer.status != PeerStatus.HANDSHAKED
            or not self.peer_registry.is_current_owner(peer_key, expected_connection_id)
        ):
            return

        try:
            await self._call_send(peer_key, data, expected_connection_id)
        except Exception as e:
            logger.warning(f"Failed to send to {nick or peer_key}: {e}")
            # Mark peer as failed to prevent repeated attempts
            self._failed_peers.add(failed_owner)
            # Notify server to clean up this peer
            if self.on_send_failed:
                try:
                    await self._call_failed(peer_key, expected_connection_id)
                except Exception as cleanup_err:
                    logger.trace(f"Error in on_send_failed callback: {cleanup_err}")

    async def _batched_broadcast(self, targets: list[BroadcastTarget], data: bytes) -> int:
        """
        Broadcast data to targets in batches to limit memory usage.

        Instead of creating all coroutines at once (which caused 2GB+ memory usage),
        we process in batches of broadcast_batch_size to keep memory bounded.

        Returns the number of targets processed.
        """
        return await self._batched_broadcast_iter(iter(targets), data)

    async def _batched_broadcast_iter(self, targets: Iterator[BroadcastTarget], data: bytes) -> int:
        """
        Broadcast data to targets from an iterator in batches.

        This is the memory-efficient version that consumes targets lazily,
        only materializing batch_size items at a time.

        Returns the number of targets processed.
        """
        # Clear failed peers set at start of broadcast to allow fresh attempts
        # while still preventing repeated attempts within this broadcast
        self._failed_peers.clear()

        total_sent = 0
        batch: list[tuple[str, str | None, str]] = []

        for target in targets:
            peer_key, nick = target[:2]
            connection_id = (
                target[2] if len(target) == 3 else self.peer_registry.get_connection_id(peer_key)
            )
            if connection_id is None:
                continue
            # Skip peers that have already failed in this broadcast
            if (peer_key, connection_id) in self._failed_peers:
                continue
            batch.append((peer_key, nick, connection_id))

            if len(batch) >= self.broadcast_batch_size:
                tasks = [self._safe_send(pk, data, n, cid) for pk, n, cid in batch]
                await asyncio.gather(*tasks)
                total_sent += len(batch)
                batch = []

        # Process remaining items
        if batch:
            tasks = [self._safe_send(pk, data, n, cid) for pk, n, cid in batch]
            await asyncio.gather(*tasks)
            total_sent += len(batch)

        return total_sent

    async def _handle_private_message(
        self,
        envelope: MessageEnvelope,
        from_key: str,
        connection_id: str | None = None,
    ) -> None:
        connection_id = connection_id or self.peer_registry.get_connection_id(from_key)
        if connection_id is None or not self.peer_registry.is_current_owner(
            from_key, connection_id
        ):
            return
        parsed = parse_jm_message(envelope.payload)
        if not parsed:
            logger.warning("Invalid private message format")
            return

        from_nick, to_nick, rest = parsed
        logger.info(f"PRIVMSG routing: {from_nick} -> {to_nick} (rest: {rest[:50]}...)")

        # Diagnostic: warn if the message appears to lack a signature.
        # The JoinMarket protocol appends "<pubkey_hex> <sig_base64>" to all
        # privmsgs.  A missing signature will cause the recipient to reject
        # the message with "Sig not properly appended to privmsg".
        rest_parts = rest.split()
        if len(rest_parts) < 3:
            # Need at least: command, pubkey, sig
            logger.warning(
                f"PRIVMSG from {from_nick} -> {to_nick} appears to lack a "
                f"signature (only {len(rest_parts)} space-separated tokens). "
                f"Relaying anyway but recipient will likely reject it. "
                f"Sender peer_key: {from_key}"
            )

        to_peer = self.peer_registry.get_by_nick(to_nick)
        if not to_peer or to_peer.status != PeerStatus.HANDSHAKED:
            logger.warning(f"Target peer not found: {to_nick}")
            logger.info(f"Registered peer nicks: {list(self.peer_registry._peers)}")
            return

        from_peer = self.peer_registry.get_by_key(from_key)
        if not from_peer or from_peer.network != to_peer.network:
            logger.warning("Network mismatch or unknown sender")
            return
        if from_nick != from_peer.nick:
            logger.warning(
                f"Dropping private message claiming {from_nick} from connection {from_peer.nick}"
            )
            return

        to_peer_key = to_peer.nick
        to_connection_id = self.peer_registry.get_connection_id(to_peer_key)
        if to_connection_id is None:
            return
        try:
            logger.info(f"Sending to peer_key: {to_peer_key}")
            await self._call_send(to_peer_key, envelope.to_bytes(), to_connection_id)
            logger.info(f"Successfully routed private message: {from_nick} -> {to_nick}")

            await self._send_peer_location(to_peer_key, from_peer, to_connection_id)
        except Exception as e:
            logger.warning(f"Failed to route private message to {to_nick}: {e}")
            # Notify server to clean up this peer's mapping
            if self.on_send_failed:
                with contextlib.suppress(Exception):
                    await self._call_failed(to_peer_key, to_connection_id)

    async def _handle_peerlist_request(
        self, from_key: str, connection_id: str | None = None
    ) -> None:
        connection_id = connection_id or self.peer_registry.get_connection_id(from_key)
        if connection_id is None or not self.peer_registry.is_current_owner(
            from_key, connection_id
        ):
            return
        peer = self.peer_registry.get_by_key(from_key)
        if not peer:
            return

        # Check if requesting peer supports peerlist_features
        include_features = peer.features.get("peerlist_features", False)
        await self.send_peerlist(
            from_key,
            peer.network,
            include_features=include_features,
            expected_connection_id=connection_id,
        )

    async def _handle_ping(self, from_key: str, connection_id: str | None = None) -> None:
        connection_id = connection_id or self.peer_registry.get_connection_id(from_key)
        if connection_id is None or not self.peer_registry.is_current_owner(
            from_key, connection_id
        ):
            return
        pong_envelope = MessageEnvelope(message_type=MessageType.PONG, payload="")
        try:
            await self._call_send(from_key, pong_envelope.to_bytes(), connection_id)
            logger.trace(f"Sent PONG to {from_key}")
        except Exception as e:
            logger.trace(f"Failed to send PONG: {e}")

    def _handle_pong(self, from_key: str, connection_id: str | None = None) -> None:
        """Handle a PONG response from a peer.

        Delegates to the heartbeat module via callback to clear pong_pending.
        """
        logger.trace(f"Received PONG from {from_key}")
        connection_id = connection_id or self.peer_registry.get_connection_id(from_key)
        if self.on_pong and connection_id is not None:
            self._call_pong(from_key, connection_id)

    async def send_peerlist(
        self,
        to_key: str,
        network: NetworkType,
        include_features: bool = False,
        chunk_size: int = 20,
        expected_connection_id: str | None = None,
    ) -> None:
        """
        Send peerlist to a peer in chunks.

        Sends multiple PEERLIST messages to avoid overwhelming slow Tor connections.
        Each chunk contains up to `chunk_size` peer entries. Clients should accumulate
        entries from multiple PEERLIST messages.

        Args:
            to_key: Key of the peer to send to
            network: Network to filter peers by
            include_features: If True, include F: suffix with features for each peer.
                             This is enabled when the requesting peer supports peerlist_features.
            chunk_size: Maximum number of peer entries per PEERLIST message (default: 20)
        """
        logger.debug(
            f"send_peerlist called for {to_key}, network={network}, "
            f"include_features={include_features}"
        )
        expected_connection_id = expected_connection_id or self.peer_registry.get_connection_id(
            to_key
        )

        # Build list of entries
        entries: list[str] = []
        if include_features:
            peers_with_features = self.peer_registry.get_peerlist_with_features(network)
            entries = [
                create_peerlist_entry(nick, loc, features=features)
                for nick, loc, features in peers_with_features
            ]
        else:
            peers = self.peer_registry.get_peerlist_for_network(network)
            entries = [create_peerlist_entry(nick, loc) for nick, loc in peers]

        # Always send at least one response (even if empty) - clients wait for PEERLIST
        if not entries:
            envelope = MessageEnvelope(message_type=MessageType.PEERLIST, payload="")
            try:
                await self._call_send(to_key, envelope.to_bytes(), expected_connection_id)
                logger.debug(f"Sent empty peerlist to {to_key}")
            except Exception as e:
                logger.warning(f"Failed to send peerlist to {to_key}: {e}")
            return

        # Send entries in chunks
        chunks_sent = 0
        for i in range(0, len(entries), chunk_size):
            chunk = entries[i : i + chunk_size]
            peerlist_msg = ",".join(chunk)
            envelope = MessageEnvelope(message_type=MessageType.PEERLIST, payload=peerlist_msg)

            try:
                await self._call_send(to_key, envelope.to_bytes(), expected_connection_id)
                chunks_sent += 1
                # Small delay between chunks to avoid overwhelming the connection
                if i + chunk_size < len(entries):
                    await asyncio.sleep(0.05)
            except Exception as e:
                logger.warning(f"Failed to send peerlist chunk {chunks_sent + 1} to {to_key}: {e}")
                return

        logger.debug(
            f"Sent peerlist to {to_key} ({len(entries)} peers in {chunks_sent} chunks, "
            f"include_features={include_features})"
        )

    async def _send_peer_location(
        self,
        to_key: str,
        peer_info: PeerInfo,
        expected_connection_id: str | None = None,
    ) -> None:
        if peer_info.onion_address == "NOT-SERVING-ONION":
            return

        # Include features if the peer has any - this ensures recipients can learn about
        # the peer's capabilities (e.g., neutrino_compat) when they receive the peerlist update
        features = FeatureSet(features={k for k, v in peer_info.features.items() if v is True})
        # Debug: Log when features are being sent
        if peer_info.features and not features.features:
            logger.warning(
                f"Peer {peer_info.nick} has features dict {peer_info.features} but "
                f"FeatureSet is empty after 'v is True' filter"
            )
        entry = create_peerlist_entry(peer_info.nick, peer_info.location_string, features=features)
        envelope = MessageEnvelope(message_type=MessageType.PEERLIST, payload=entry)

        try:
            await self._call_send(to_key, envelope.to_bytes(), expected_connection_id)
        except Exception as e:
            logger.trace(f"Failed to send peer location: {e}")

    async def broadcast_peer_disconnect(
        self,
        peer_key: str,
        network: NetworkType,
        expected_connection_id: str | None = None,
    ) -> None:
        peer = self.peer_registry.get_by_key(peer_key)
        if not peer or not peer.nick:
            return
        if not self.peer_registry.is_current_owner(peer_key, expected_connection_id):
            return

        entry = create_peerlist_entry(peer.nick, peer.location_string, disconnected=True)
        envelope = MessageEnvelope(message_type=MessageType.PEERLIST, payload=entry)

        # Pre-serialize envelope once instead of per-peer
        envelope_bytes = envelope.to_bytes()

        # Use generator to avoid building full target list in memory
        def target_generator() -> Iterator[BroadcastTarget]:
            for target_key, p, target_connection_id in self.peer_registry.iter_connected_owners(
                network
            ):
                if target_key == peer_key:
                    continue
                yield (target_key, p.nick, target_connection_id)

        # Execute sends in batches to limit memory usage
        sent_count = await self._batched_broadcast_iter(target_generator(), envelope_bytes)

        logger.info(f"Broadcasted disconnect for {peer.nick} to {sent_count} peers")

    async def broadcast_displaced_peer_disconnect(
        self,
        peer: PeerInfo,
        connection_id: str,
    ) -> None:
        """Invalidate state announced by an owner that was atomically displaced."""
        if peer.status != PeerStatus.HANDSHAKED:
            return
        entry = create_peerlist_entry(peer.nick, peer.location_string, disconnected=True)
        envelope_bytes = MessageEnvelope(
            message_type=MessageType.PEERLIST,
            payload=entry,
        ).to_bytes()

        def target_generator() -> Iterator[BroadcastTarget]:
            for (
                target_key,
                target,
                target_connection_id,
            ) in self.peer_registry.iter_connected_owners(peer.network):
                if target_connection_id != connection_id and target.nick != peer.nick:
                    yield (target_key, target.nick, target_connection_id)

        await self._batched_broadcast_iter(target_generator(), envelope_bytes)

    def get_offer_stats(self) -> dict:
        """Get statistics about tracked offers."""
        current_offers = {
            owner: offers
            for owner, offers in self._peer_offers.items()
            if self.peer_registry.is_current_owner(*owner)
        }
        total_offers = sum(len(offers) for offers in current_offers.values())
        peers_with_offers = len([k for k, v in current_offers.items() if v])

        # Find peers with more than 2 offers
        peers_many_offers = []
        for (peer_key, _connection_id), offers in current_offers.items():
            if len(offers) > 2:
                peer_info = self.peer_registry.get_by_key(peer_key)
                nick = peer_info.nick if peer_info else peer_key
                peers_many_offers.append((nick, len(offers)))

        # Sort by offer count descending
        peers_many_offers.sort(key=lambda x: x[1], reverse=True)

        return {
            "total_offers": total_offers,
            "peers_with_offers": peers_with_offers,
            "peers_many_offers": peers_many_offers[:10],  # Top 10
        }

    def remove_peer_offers(self, peer_key: str, expected_connection_id: str | None = None) -> None:
        """Remove offer tracking for a disconnected peer."""
        if expected_connection_id is None:
            for owner in [owner for owner in self._peer_offers if owner[0] == peer_key]:
                self._peer_offers.pop(owner, None)
            return
        self._peer_offers.pop((peer_key, expected_connection_id), None)

    async def _call_send(
        self, peer_key: str, data: bytes, expected_connection_id: str | None
    ) -> None:
        if expected_connection_id is not None and not self.peer_registry.is_current_owner(
            peer_key, expected_connection_id
        ):
            return
        if self._accepts_args(self.send_callback, 3):
            await self.send_callback(peer_key, data, expected_connection_id)
        else:
            await self.send_callback(peer_key, data)

    async def _call_failed(self, peer_key: str, expected_connection_id: str) -> None:
        if self.on_send_failed is None:
            return
        if self._accepts_args(self.on_send_failed, 2):
            await self.on_send_failed(peer_key, expected_connection_id)
        else:
            await self.on_send_failed(peer_key)

    def _call_pong(self, peer_key: str, expected_connection_id: str) -> None:
        if self.on_pong is None:
            return
        if self._accepts_args(self.on_pong, 2):
            self.on_pong(peer_key, expected_connection_id)
        else:
            self.on_pong(peer_key)

    @staticmethod
    def _accepts_args(callback: Callable[..., object], count: int) -> bool:
        try:
            parameters = inspect.signature(callback).parameters.values()
        except (TypeError, ValueError):
            return True
        positional = [
            parameter
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        return len(positional) >= count
