"""
Message routing logic for forwarding messages between peers.

Implements Single Responsibility Principle: only handles message routing.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterator

from jmcore.models import MessageEnvelope, NetworkType, PeerInfo, PeerStatus
from jmcore.protocol import FeatureSet, MessageType, create_peerlist_entry, parse_jm_message
from jmcore.rate_limiter import TokenBucket
from loguru import logger

from directory_server.peer_registry import PeerRegistry

SendCallback = Callable[[str, bytes, str | None], Awaitable[None]]
FailedSendCallback = Callable[[str, str], Awaitable[None]]
PongCallback = Callable[[str, str], None]
BroadcastTarget = tuple[str, str | None] | tuple[str, str | None, str]

# Default batch size for concurrent broadcasts to limit memory usage
# This can be overridden via Settings.broadcast_batch_size
DEFAULT_BROADCAST_BATCH_SIZE = 50
_PUBLIC_INGRESS_BYTES_PER_SEC = 32 * 1024
_PUBLIC_INGRESS_BURST_BYTES = 256 * 1024
_PUBLIC_OUTGOING_BYTES_PER_SEC = 16 * 1024 * 1024
_PUBLIC_OUTGOING_BURST_BYTES = 64 * 1024 * 1024
_MAX_OFFERS_PER_OWNER = 256
_MAX_OFFER_ID_LENGTH = 64


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
        # Offers belong to a connection generation, not just a reusable peer key.
        self._peer_offers: dict[tuple[str, str], set[str]] = {}
        self._public_ingress_buckets: dict[tuple[str, str], TokenBucket] = {}
        self._public_drop_counts: dict[tuple[str, str], int] = {}
        self._public_outgoing_bucket = TokenBucket(
            capacity=_PUBLIC_OUTGOING_BURST_BYTES,
            refill_rate=float(_PUBLIC_OUTGOING_BYTES_PER_SEC),
        )
        self._public_outgoing_drop_count = 0

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
            logger.bind(sensitive=True).warning(
                f"Dropping message from stale or unhandshaked peer: {from_key}"
            )
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
            logger.bind(sensitive=True).warning(
                f"Public message not addressed to PUBLIC: {to_nick}"
            )
            return

        from_peer = self.peer_registry.get_by_key(from_key)
        if not from_peer:
            logger.bind(sensitive=True).warning(f"Unknown peer sending public message: {from_key}")
            return
        if from_nick != from_peer.nick:
            logger.bind(sensitive=True).warning(
                f"Dropping public message claiming {from_nick} from connection {from_peer.nick}"
            )
            return

        envelope_bytes = envelope.to_bytes()
        offer_owner = (from_key, connection_id)
        if not self._consume_public_ingress(offer_owner, len(envelope_bytes)):
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
                    if len(order_id) > _MAX_OFFER_ID_LENGTH:
                        self._record_public_drop(offer_owner, "oversized order ID")
                        return
                    offers = self._peer_offers.get(offer_owner)
                    if offers is None:
                        offers = set()
                        self._peer_offers[offer_owner] = offers
                    if order_id not in offers and len(offers) >= _MAX_OFFERS_PER_OWNER:
                        self._record_public_drop(offer_owner, "offer capacity exhausted")
                        return
                    offers.add(order_id)
                    logger.bind(sensitive=True).trace(
                        f"Tracked offer {order_id} from {from_nick} (total offers: {len(offers)})"
                    )
                except (ValueError, IndexError):
                    pass
            elif (
                len(message_parts) == 2
                and message_parts[0] in {"cancel", "!cancel"}
                and message_parts[1].isascii()
                and message_parts[1].isdecimal()
            ):
                offer_owner = (from_key, connection_id)
                offers = self._peer_offers.get(offer_owner)
                if offers is not None:
                    offers.discard(message_parts[1])
                    if not offers:
                        self._peer_offers.pop(offer_owner, None)
                logger.bind(sensitive=True).trace(
                    f"Removed canceled offer {message_parts[1]} from {from_nick}"
                )

        recipient_count = sum(
            1
            for peer_key, _peer, _target_connection_id in self.peer_registry.iter_connected_owners(
                from_peer.network
            )
            if peer_key != from_key
        )
        if not self._consume_public_outgoing(len(envelope_bytes), recipient_count):
            return

        # Use generator to avoid building full target list in memory
        def target_generator() -> Iterator[BroadcastTarget]:
            for peer_key, peer, target_connection_id in self.peer_registry.iter_connected_owners(
                from_peer.network
            ):
                if peer_key != from_key:
                    yield (peer_key, peer.nick, target_connection_id)

        # Execute sends in batches to limit memory usage
        sent_count = await self._batched_broadcast_iter(target_generator(), envelope_bytes)

        logger.bind(sensitive=True).trace(
            f"Broadcasted public message from {from_nick} to {sent_count} peers"
        )

    def _consume_public_ingress(self, owner: tuple[str, str], byte_count: int) -> bool:
        bucket = self._public_ingress_buckets.get(owner)
        if bucket is None:
            bucket = TokenBucket(
                capacity=_PUBLIC_INGRESS_BURST_BYTES,
                refill_rate=float(_PUBLIC_INGRESS_BYTES_PER_SEC),
            )
            self._public_ingress_buckets[owner] = bucket
        if bucket.consume(byte_count):
            return True

        self._record_public_drop(owner, "rate-limited")

        return False

    def _record_public_drop(self, owner: tuple[str, str], reason: str) -> None:
        drop_count = self._public_drop_counts.get(owner, 0) + 1
        self._public_drop_counts[owner] = drop_count
        if drop_count % 50 == 1:
            logger.bind(sensitive=True).debug(f"Dropping {reason} public message from {owner[0]}")

    def _consume_public_outgoing(self, message_size: int, recipient_count: int) -> bool:
        if recipient_count == 0:
            return True
        if self._public_outgoing_bucket.consume(message_size * recipient_count):
            return True

        self._public_outgoing_drop_count += 1
        if self._public_outgoing_drop_count % 50 == 1:
            logger.debug("Dropping public broadcast because outgoing capacity is exhausted")
        return False

    async def _safe_send(
        self,
        peer_key: str,
        data: bytes,
        nick: str | None = None,
        expected_connection_id: str | None = None,
        failed: set[tuple[str, str]] | None = None,
    ) -> None:
        """Send with exception handling to prevent one failed send from affecting others."""
        failed = failed if failed is not None else set()
        expected_connection_id = expected_connection_id or self.peer_registry.get_connection_id(
            peer_key
        )
        if expected_connection_id is None:
            return
        failed_owner = (peer_key, expected_connection_id)
        # Skip if this peer already failed in current operation
        if failed_owner in failed:
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
            logger.warning("Failed to send to peer")
            logger.bind(sensitive=True).warning(f"Failed to send to {nick or peer_key}: {e}")
            # Mark peer as failed to prevent repeated attempts
            failed.add(failed_owner)
            # Notify server to clean up this peer
            if self.on_send_failed:
                try:
                    await self._call_failed(peer_key, expected_connection_id)
                except Exception as cleanup_err:
                    logger.bind(sensitive=True).trace(
                        f"Error in on_send_failed callback: {cleanup_err}"
                    )

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
        # A disconnect broadcast re-enters this method through on_send_failed, and
        # broadcasts also run concurrently. Keep failures local to this call.
        failed: set[tuple[str, str]] = set()

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
            if (peer_key, connection_id) in failed:
                continue
            batch.append((peer_key, nick, connection_id))

            if len(batch) >= self.broadcast_batch_size:
                tasks = [self._safe_send(pk, data, n, cid, failed) for pk, n, cid in batch]
                await asyncio.gather(*tasks)
                total_sent += len(batch)
                batch = []

        # Process remaining items
        if batch:
            tasks = [self._safe_send(pk, data, n, cid, failed) for pk, n, cid in batch]
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
        logger.info("Routing private message")
        logger.bind(sensitive=True).info(
            f"PRIVMSG routing: {from_nick} -> {to_nick} (rest: {rest[:50]}...)"
        )

        # Diagnostic: warn if the message appears to lack a signature.
        # The JoinMarket protocol appends "<pubkey_hex> <sig_base64>" to all
        # privmsgs.  A missing signature will cause the recipient to reject
        # the message with "Sig not properly appended to privmsg".
        rest_parts = rest.split()
        if len(rest_parts) < 3:
            # Need at least: command, pubkey, sig
            logger.bind(sensitive=True).warning(
                f"PRIVMSG from {from_nick} -> {to_nick} appears to lack a "
                f"signature (only {len(rest_parts)} space-separated tokens). "
                f"Relaying anyway but recipient will likely reject it. "
                f"Sender peer_key: {from_key}"
            )

        to_peer = self.peer_registry.get_by_nick(to_nick)
        if not to_peer or to_peer.status != PeerStatus.HANDSHAKED:
            logger.warning("Private message target peer not found")
            logger.bind(sensitive=True).warning(f"Target peer not found: {to_nick}")
            logger.bind(sensitive=True).info(
                f"Registered peer nicks: {list(self.peer_registry._peers)}"
            )
            return

        from_peer = self.peer_registry.get_by_key(from_key)
        if not from_peer or from_peer.network != to_peer.network:
            logger.warning("Network mismatch or unknown sender")
            return
        if from_nick != from_peer.nick:
            logger.bind(sensitive=True).warning(
                f"Dropping private message claiming {from_nick} from connection {from_peer.nick}"
            )
            return

        to_peer_key = to_peer.nick
        to_connection_id = self.peer_registry.get_connection_id(to_peer_key)
        if to_connection_id is None:
            return
        try:
            logger.bind(sensitive=True).info(f"Sending to peer_key: {to_peer_key}")
            await self._call_send(to_peer_key, envelope.to_bytes(), to_connection_id)
            logger.info("Private message routed")
            logger.bind(sensitive=True).info(
                f"Successfully routed private message: {from_nick} -> {to_nick}"
            )

            await self._send_peer_location(to_peer_key, from_peer, to_connection_id)
        except Exception as e:
            logger.warning("Failed to route private message")
            logger.bind(sensitive=True).warning(
                f"Failed to route private message to {to_nick}: {e}"
            )
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
            logger.bind(sensitive=True).trace(f"Sent PONG to {from_key}")
        except Exception as e:
            logger.bind(sensitive=True).trace(f"Failed to send PONG: {e}")

    def _handle_pong(self, from_key: str, connection_id: str | None = None) -> None:
        """Handle a PONG response from a peer.

        Delegates to the heartbeat module via callback to clear pong_pending.
        """
        logger.bind(sensitive=True).trace(f"Received PONG from {from_key}")
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
        logger.bind(sensitive=True).debug(
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
                logger.bind(sensitive=True).debug(f"Sent empty peerlist to {to_key}")
            except Exception as e:
                logger.bind(sensitive=True).warning(f"Failed to send peerlist to {to_key}: {e}")
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
                logger.bind(sensitive=True).warning(
                    f"Failed to send peerlist chunk {chunks_sent + 1} to {to_key}: {e}"
                )
                return

        logger.bind(sensitive=True).debug(
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
            logger.bind(sensitive=True).warning(
                f"Peer {peer_info.nick} has features dict {peer_info.features} but "
                f"FeatureSet is empty after 'v is True' filter"
            )
        entry = create_peerlist_entry(peer_info.nick, peer_info.location_string, features=features)
        envelope = MessageEnvelope(message_type=MessageType.PEERLIST, payload=entry)

        try:
            await self._call_send(to_key, envelope.to_bytes(), expected_connection_id)
        except Exception as e:
            logger.bind(sensitive=True).trace(f"Failed to send peer location: {e}")

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

        logger.bind(sensitive=True).info(
            f"Broadcasted disconnect for {peer.nick} to {sent_count} peers"
        )

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
        """Remove public routing state for a disconnected peer generation."""
        if expected_connection_id is None:
            for owner in [owner for owner in self._peer_offers if owner[0] == peer_key]:
                self._peer_offers.pop(owner, None)
            for owner in [owner for owner in self._public_ingress_buckets if owner[0] == peer_key]:
                self._public_ingress_buckets.pop(owner, None)
                self._public_drop_counts.pop(owner, None)
            return
        owner = (peer_key, expected_connection_id)
        self._peer_offers.pop(owner, None)
        self._public_ingress_buckets.pop(owner, None)
        self._public_drop_counts.pop(owner, None)

    async def _call_send(
        self, peer_key: str, data: bytes, expected_connection_id: str | None
    ) -> None:
        if expected_connection_id is not None and not self.peer_registry.is_current_owner(
            peer_key, expected_connection_id
        ):
            return
        await self.send_callback(peer_key, data, expected_connection_id)

    async def _call_failed(self, peer_key: str, expected_connection_id: str) -> None:
        if self.on_send_failed is None:
            return
        await self.on_send_failed(peer_key, expected_connection_id)

    def _call_pong(self, peer_key: str, expected_connection_id: str) -> None:
        if self.on_pong is None:
            return
        self.on_pong(peer_key, expected_connection_id)
