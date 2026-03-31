"""
Shared DirectoryClient for connecting to JoinMarket directory nodes.

This module provides a unified client for:
- Orderbook watcher (passive monitoring)
- Maker (announcing offers)
- Taker (fetching orderbooks and coordinating CoinJoins)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import struct
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from loguru import logger

from jmcore.btc_script import mk_freeze_script, redeem_script_to_p2wsh_script
from jmcore.crypto import NickIdentity, verify_fidelity_bond_proof
from jmcore.models import FidelityBond, Offer, OfferType
from jmcore.network import (
    ONION_HOSTID,
    TCPConnection,
    connect_direct,
    connect_via_tor,
)
from jmcore.network import (
    ConnectionError as NetworkConnectionError,
)
from jmcore.protocol import (
    COMMAND_PREFIX,
    FEATURE_NEUTRINO_COMPAT,
    FEATURE_PEERLIST_FEATURES,
    JM_VERSION,
    NICK_PEERLOCATOR_SEPARATOR,
    FeatureSet,
    MessageType,
    create_handshake_request,
    parse_peerlist_entry,
    peer_supports_neutrino_compat,
)


class OfferWithTimestamp:
    """Wrapper for Offer with metadata for staleness tracking."""

    __slots__ = ("offer", "received_at", "bond_utxo_key")

    def __init__(self, offer: Offer, received_at: float, bond_utxo_key: str | None = None) -> None:
        self.offer = offer
        self.received_at = received_at
        # Bond UTXO key (txid:vout) for deduplication across nick changes
        self.bond_utxo_key = bond_utxo_key


class DirectoryClientError(Exception):
    """Error raised by DirectoryClient operations."""


def parse_fidelity_bond_proof(
    proof_base64: str, maker_nick: str, taker_nick: str, verify: bool = True
) -> dict[str, Any] | None:
    """
    Parse and optionally verify a fidelity bond proof from base64-encoded binary data.

    Args:
        proof_base64: Base64-encoded bond proof
        maker_nick: Maker's nick
        taker_nick: Taker's nick (requesting party)
        verify: If True, verify both signatures in the proof (default: True)

    Returns:
        Dict with bond details or None if parsing/verification fails
    """
    # First, verify the signatures if requested
    if verify:
        is_valid, verified_data, error = verify_fidelity_bond_proof(
            proof_base64, maker_nick, taker_nick
        )
        if not is_valid:
            logger.warning(f"Fidelity bond proof verification failed for {maker_nick}: {error}")
            return None

    # Parse the proof data (also extracts redeem script)
    try:
        decoded_data = base64.b64decode(proof_base64)
    except (binascii.Error, ValueError) as e:
        logger.warning(f"Failed to decode bond proof: {e}")
        return None

    if len(decoded_data) != 252:
        logger.warning(f"Invalid bond proof length: {len(decoded_data)}, expected 252")
        return None

    try:
        unpacked_data = struct.unpack("<72s72s33sH33s32sII", decoded_data)

        txid = unpacked_data[5]
        vout = unpacked_data[6]
        locktime = unpacked_data[7]
        utxo_pub = unpacked_data[4]
        cert_pub = unpacked_data[2]
        cert_expiry_raw = unpacked_data[3]
        cert_expiry = cert_expiry_raw * 2016

        utxo_pub_hex = binascii.hexlify(utxo_pub).decode("ascii")
        redeem_script = mk_freeze_script(utxo_pub_hex, locktime)
        redeem_script_hex = binascii.hexlify(redeem_script).decode("ascii")
        p2wsh_script = redeem_script_to_p2wsh_script(redeem_script)
        p2wsh_script_hex = binascii.hexlify(p2wsh_script).decode("ascii")

        return {
            "maker_nick": maker_nick,
            "taker_nick": taker_nick,
            "utxo_txid": binascii.hexlify(txid).decode("ascii"),
            "utxo_vout": vout,
            "locktime": locktime,
            "utxo_pub": utxo_pub_hex,
            "cert_pub": binascii.hexlify(cert_pub).decode("ascii"),
            "cert_expiry": cert_expiry,
            "proof": proof_base64,
            "redeem_script": redeem_script_hex,
            "p2wsh_script": p2wsh_script_hex,
        }
    except Exception as e:
        logger.warning(f"Failed to unpack bond proof: {e}")
        return None


class DirectoryClient:
    """
    Client for connecting to JoinMarket directory servers.

    Supports:
    - Direct TCP connections (for local/dev)
    - Tor connections (for .onion addresses)
    - Handshake protocol
    - Peerlist fetching
    - Orderbook fetching
    - Continuous listening for updates
    """

    def __init__(
        self,
        host: str,
        port: int,
        network: str,
        nick_identity: NickIdentity | None = None,
        location: str = "NOT-SERVING-ONION",
        socks_host: str = "127.0.0.1",
        socks_port: int = 9050,
        timeout: float = 120.0,
        max_message_size: int = 2097152,
        on_disconnect: Callable[[], None] | None = None,
        neutrino_compat: bool = False,
        peerlist_timeout: float = 60.0,
        socks_username: str | None = None,
        socks_password: str | None = None,
    ) -> None:
        """
        Initialize DirectoryClient.

        Args:
            host: Directory server hostname or .onion address
            port: Directory server port
            network: Bitcoin network (mainnet, testnet, signet, regtest)
            nick_identity: NickIdentity for message signing (generated if None)
            location: Our location string (onion address or NOT-SERVING-ONION)
            socks_host: SOCKS proxy host for Tor
            socks_port: SOCKS proxy port for Tor
            timeout: Connection timeout in seconds (covers SOCKS + Tor circuit + PoW)
            max_message_size: Maximum message size in bytes
            on_disconnect: Callback when connection drops
            neutrino_compat: Advertise support for Neutrino-compatible UTXO metadata
            peerlist_timeout: Timeout for first PEERLIST chunk (default 60s, subsequent chunks use 5s)
            socks_username: SOCKS5 username for Tor stream isolation (optional)
            socks_password: SOCKS5 password for Tor stream isolation (optional)
        """
        self.host = host
        self.port = port
        self.network = network
        self.location = location
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.socks_username = socks_username
        self.socks_password = socks_password
        self.timeout = timeout
        self.max_message_size = max_message_size
        self.connection: TCPConnection | None = None
        self.nick_identity = nick_identity or NickIdentity(JM_VERSION)
        self.nick = self.nick_identity.nick
        # hostid retained for possible future use (e.g., logging, debugging)
        # Note: NOT used for message signing - always use ONION_HOSTID constant instead
        self.hostid = host
        # Offers indexed by (counterparty, oid) with timestamp metadata
        self.offers: dict[tuple[str, int], OfferWithTimestamp] = {}
        # Bonds indexed by UTXO key (txid:vout)
        self.bonds: dict[str, FidelityBond] = {}
        # Reverse index: bond UTXO key -> set of (counterparty, oid) keys that use this bond
        # Used for deduplication when same bond is used by different nicks
        self._bond_to_offers: dict[str, set[tuple[str, int]]] = {}
        self.peer_features: dict[str, dict[str, bool]] = {}  # nick -> features dict
        # Active peers from last peerlist (nick -> location)
        self._active_peers: dict[str, str] = {}
        self.running = False
        self.on_disconnect = on_disconnect
        self.initial_orderbook_received = False
        self.last_orderbook_request_time: float = 0.0
        self.last_offer_received_time: float | None = None
        self.neutrino_compat = neutrino_compat

        # Version negotiation state (set after handshake)
        self.negotiated_version: int | None = None
        self.directory_neutrino_compat: bool = False
        self.directory_peerlist_features: bool = False  # True if directory supports F: suffix

        # Directory metadata from handshake
        self.directory_motd: str | None = None
        self.directory_nick: str | None = None
        self.directory_proto_ver_min: int | None = None
        self.directory_proto_ver_max: int | None = None
        self.directory_features: dict[str, bool] = {}

        # Timing intervals
        self.peerlist_check_interval = 1800.0
        self.orderbook_refresh_interval = 1800.0
        self.orderbook_retry_interval = 300.0
        self.zero_offer_retry_interval = 600.0

        # Peerlist support tracking
        # If the directory doesn't support getpeerlist (e.g., reference implementation),
        # we track this to avoid spamming unsupported requests
        self._peerlist_supported: bool | None = None  # None = unknown, True/False = known
        self._last_peerlist_request_time: float = 0.0
        self._peerlist_min_interval: float = 60.0  # Minimum seconds between peerlist requests
        self._peerlist_timeout: float = peerlist_timeout  # Timeout for first peerlist chunk
        self._peerlist_chunk_timeout: float = (
            5.0  # Timeout between chunks (end of chunked response)
        )
        self._peerlist_timeout_count: int = 0  # Track consecutive timeouts

        # Message buffer for messages received while waiting for specific responses
        # (e.g., PEERLIST). These messages should be processed, not discarded.
        self._message_buffer: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def connect(self) -> None:
        """Connect to the directory server and perform handshake."""
        try:
            logger.debug(f"DirectoryClient.connect: connecting to {self.host}:{self.port}")
            if not self.host.endswith(".onion"):
                self.connection = await connect_direct(
                    self.host,
                    self.port,
                    self.max_message_size,
                    self.timeout,
                )
                logger.debug("DirectoryClient.connect: direct connection established")
            else:
                self.connection = await connect_via_tor(
                    self.host,
                    self.port,
                    self.socks_host,
                    self.socks_port,
                    self.max_message_size,
                    self.timeout,
                    socks_username=self.socks_username,
                    socks_password=self.socks_password,
                )
                logger.debug("DirectoryClient.connect: tor connection established")
            logger.debug("DirectoryClient.connect: starting handshake")
            await self._handshake()
            logger.debug("DirectoryClient.connect: handshake complete")
        except Exception as e:
            logger.error(f"Failed to connect to {self.host}:{self.port}: {e}", exc_info=True)
            # Clean up connection if handshake failed
            if self.connection:
                with contextlib.suppress(Exception):
                    await self.connection.close()
                self.connection = None
            raise DirectoryClientError(f"Connection failed: {e}") from e

    async def _handshake(self) -> None:
        """
        Perform directory server handshake with feature negotiation.

        We use proto-ver=5 for reference implementation compatibility.
        Features like neutrino_compat are negotiated independently via
        the features dict in the handshake payload.
        """
        if not self.connection:
            raise DirectoryClientError("Not connected")

        # Build our feature set - always include peerlist_features to indicate we support
        # the extended peerlist format with F: suffix for feature information
        our_features: set[str] = {FEATURE_PEERLIST_FEATURES}
        if self.neutrino_compat:
            our_features.add(FEATURE_NEUTRINO_COMPAT)
        feature_set = FeatureSet(features=our_features)

        # Send our handshake with current version and features
        handshake_data = create_handshake_request(
            nick=self.nick,
            location=self.location,
            network=self.network,
            directory=False,
            features=feature_set,
        )
        logger.debug(f"DirectoryClient._handshake: created handshake data: {handshake_data}")
        handshake_msg = {
            "type": MessageType.HANDSHAKE.value,
            "line": json.dumps(handshake_data),
        }
        logger.debug("DirectoryClient._handshake: sending handshake message")
        await self.connection.send(json.dumps(handshake_msg).encode("utf-8"))
        logger.debug("DirectoryClient._handshake: handshake sent, waiting for response")

        # Receive and parse directory's response
        response_data = await asyncio.wait_for(self.connection.receive(), timeout=self.timeout)
        logger.debug(f"DirectoryClient._handshake: received response: {response_data[:200]!r}")
        response = json.loads(response_data.decode("utf-8"))

        if response["type"] not in (MessageType.HANDSHAKE.value, MessageType.DN_HANDSHAKE.value):
            raise DirectoryClientError(f"Unexpected response type: {response['type']}")

        handshake_response = json.loads(response["line"])
        if not handshake_response.get("accepted", False):
            raise DirectoryClientError("Handshake rejected")

        # Extract directory's version range
        # Reference directories only send "proto-ver" (single value, typically 5)
        dir_ver_min = handshake_response.get("proto-ver-min")
        dir_ver_max = handshake_response.get("proto-ver-max")

        if dir_ver_min is None or dir_ver_max is None:
            # Reference directory: only sends single proto-ver
            dir_version = handshake_response.get("proto-ver", 5)
            dir_ver_min = dir_ver_max = dir_version

        # Verify compatibility with our version (we only support v5)
        if not (dir_ver_min <= JM_VERSION <= dir_ver_max):
            raise DirectoryClientError(
                f"No compatible protocol version: we support v{JM_VERSION}, "
                f"directory supports [{dir_ver_min}, {dir_ver_max}]"
            )

        # Use v5 (our only supported version)
        self.negotiated_version = JM_VERSION

        # Check if directory supports Neutrino-compatible metadata
        self.directory_neutrino_compat = peer_supports_neutrino_compat(handshake_response)

        # Check if directory supports peerlist_features (extended peerlist with F: suffix)
        dir_features = handshake_response.get("features", {})
        self.directory_peerlist_features = dir_features.get(FEATURE_PEERLIST_FEATURES, False)

        # Store directory metadata
        self.directory_motd = handshake_response.get("motd")
        self.directory_nick = handshake_response.get("nick")
        self.directory_proto_ver_min = dir_ver_min
        self.directory_proto_ver_max = dir_ver_max
        self.directory_features = dir_features

        logger.info(
            f"Handshake successful with {self.host}:{self.port} (nick: {self.nick}, "
            f"negotiated_version: v{self.negotiated_version}, "
            f"neutrino_compat: {self.directory_neutrino_compat}, "
            f"peerlist_features: {self.directory_peerlist_features})"
        )

    async def get_peerlist(self) -> list[str] | None:
        """
        Fetch the current list of connected peers.

        Note: Reference implementation directories do NOT support GETPEERLIST.
        This method shares peerlist support tracking with get_peerlist_with_features().

        The directory may send multiple PEERLIST messages (chunked response) to
        avoid overwhelming slow Tor connections. This method accumulates peers
        from all chunks.

        Returns:
            List of active peer nicks. Returns empty list if directory doesn't
            support GETPEERLIST. Returns None if rate-limited (use cached data).
        """
        result = await self._fetch_peerlist()
        if result is None:
            return None
        return [nick for nick, _location, _features in result]

    async def get_peerlist_with_features(self) -> list[tuple[str, str, FeatureSet]]:
        """
        Fetch the current list of connected peers with their features.

        Uses the standard GETPEERLIST message. If the directory supports
        peerlist_features, the response will include F: suffix with features.

        Note: Reference implementation directories do NOT support GETPEERLIST.
        This method tracks whether the directory supports it and skips requests
        to unsupported directories to avoid spamming warnings in their logs.

        The directory may send multiple PEERLIST messages (chunked response) to
        avoid overwhelming slow Tor connections. This method accumulates peers
        from all chunks until no more PEERLIST messages arrive within the
        inter-chunk timeout.

        Returns:
            List of (nick, location, features) tuples for active peers.
            Features will be empty for directories that don't support peerlist_features.
            Returns empty list if directory doesn't support GETPEERLIST or is rate-limited.
        """
        result = await self._fetch_peerlist()
        if result is None:
            return []
        return result

    async def _fetch_peerlist(self) -> list[tuple[str, str, FeatureSet]] | None:
        """
        Internal method to fetch the peerlist with features from the directory.

        Handles connection checks, peerlist support detection, rate limiting,
        sending the GETPEERLIST request, and accumulating chunked responses.

        Returns:
            List of (nick, location, features) tuples for active peers.
            Returns empty list if directory doesn't support GETPEERLIST.
            Returns None if rate-limited (caller should use cached data).
        """
        if not self.connection:
            raise DirectoryClientError("Not connected")

        # Skip if we already know this directory doesn't support GETPEERLIST
        # (only applies to directories that didn't announce peerlist_features)
        if self._peerlist_supported is False and not self.directory_peerlist_features:
            logger.debug("Skipping GETPEERLIST - directory doesn't support it")
            return []

        # Rate-limit peerlist requests to avoid spamming
        current_time = time.time()
        if current_time - self._last_peerlist_request_time < self._peerlist_min_interval:
            logger.debug(
                f"Skipping GETPEERLIST - rate limited "
                f"(last request {current_time - self._last_peerlist_request_time:.1f}s ago)"
            )
            return None

        self._last_peerlist_request_time = current_time

        getpeerlist_msg = {"type": MessageType.GETPEERLIST.value, "line": ""}
        logger.debug("Sending GETPEERLIST request")
        await self.connection.send(json.dumps(getpeerlist_msg).encode("utf-8"))

        start_time = asyncio.get_event_loop().time()

        # Timeout for waiting for the first PEERLIST response
        # Use longer timeout for directories that support peerlist_features
        first_response_timeout = (
            self._peerlist_timeout if self.directory_peerlist_features else self.timeout
        )

        # Timeout between chunks - when this expires after receiving at least one
        # PEERLIST message, we know the directory has finished sending all chunks
        inter_chunk_timeout = self._peerlist_chunk_timeout

        # Accumulate peers from multiple PEERLIST chunks
        all_peers: list[tuple[str, str, FeatureSet]] = []
        chunks_received = 0
        got_first_response = False

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time

            # Determine timeout for this receive
            if not got_first_response:
                # Waiting for first PEERLIST - use full timeout
                remaining = first_response_timeout - elapsed
                if remaining <= 0:
                    self._handle_peerlist_timeout()
                    return []
                receive_timeout = remaining
            else:
                # Already received at least one chunk - use shorter inter-chunk timeout
                receive_timeout = inter_chunk_timeout

            try:
                response_data = await asyncio.wait_for(
                    self.connection.receive(), timeout=receive_timeout
                )
                response = json.loads(response_data.decode("utf-8"))
                msg_type = response.get("type")

                if msg_type == MessageType.PEERLIST.value:
                    got_first_response = True
                    chunks_received += 1
                    peerlist_str = response.get("line", "")
                    chunk_peers = self._handle_peerlist_response(peerlist_str)
                    all_peers.extend(chunk_peers)
                    logger.debug(
                        f"Received PEERLIST chunk {chunks_received} with "
                        f"{len(chunk_peers)} peers (total: {len(all_peers)})"
                    )
                    # Continue to check for more chunks
                    continue

                # Buffer unexpected messages (like PUBMSG offers) for later processing
                logger.trace(
                    f"Buffering unexpected message type {msg_type} while waiting for PEERLIST"
                )
                await self._message_buffer.put(response)

            except TimeoutError:
                if not got_first_response:
                    # Never received any PEERLIST - this is a real timeout
                    self._handle_peerlist_timeout()
                    return []
                # Received at least one chunk, inter-chunk timeout means we're done
                logger.debug(
                    f"Peerlist complete: received {len(all_peers)} peers "
                    f"in {chunks_received} chunks"
                )
                break

            except Exception as e:
                logger.warning(f"Error receiving/parsing message while waiting for PEERLIST: {e}")
                elapsed = asyncio.get_event_loop().time() - start_time
                if not got_first_response and elapsed > first_response_timeout:
                    self._handle_peerlist_timeout()
                    return []
                # If we already have some data, return what we have
                if got_first_response:
                    break

        # Success - reset timeout counter and mark as supported
        self._peerlist_timeout_count = 0
        self._peerlist_supported = True

        logger.info(f"Received {len(all_peers)} active peers from {self.host}:{self.port}")
        return all_peers

    def _handle_peerlist_timeout(self) -> None:
        """Handle timeout when waiting for PEERLIST response."""
        self._peerlist_timeout_count += 1

        if self.directory_peerlist_features:
            # Directory announced peerlist_features during handshake, so it supports
            # GETPEERLIST. Timeout is likely due to large peerlist or network issues.
            logger.warning(
                f"Timed out waiting for PEERLIST from {self.host}:{self.port} "
                f"(attempt {self._peerlist_timeout_count}) - "
                "peerlist may be large or network is slow"
            )
            # Don't disable peerlist requests - directory supports it, just slow
        else:
            # Directory didn't announce peerlist_features - likely reference impl
            logger.info(
                f"Timed out waiting for PEERLIST from {self.host}:{self.port} - "
                "directory likely doesn't support GETPEERLIST (reference implementation)"
            )
            self._peerlist_supported = False

    def _handle_peerlist_response(self, peerlist_str: str) -> list[tuple[str, str, FeatureSet]]:
        """
        Process a PEERLIST response and update internal state.

        Note: Some directories send multiple partial PEERLIST responses (one per peer)
        instead of a single complete list. We handle this by only adding/updating
        peers from each response, not removing nicks that aren't present.

        Removal of stale offers is handled by:
        1. Explicit disconnect markers (;D suffix) in peerlist entries
        2. The periodic peerlist refresh in OrderbookAggregator
        3. Staleness cleanup for directories without GETPEERLIST support

        Args:
            peerlist_str: Comma-separated list of peer entries

        Returns:
            List of active peers (nick, location, features) in this response
        """
        logger.debug(f"Peerlist string: {peerlist_str}")

        # Mark peerlist as supported since we got a valid response
        self._peerlist_supported = True

        if not peerlist_str:
            # Empty peerlist response - just return empty list
            # Don't remove offers as this might be a partial response
            return []

        peers: list[tuple[str, str, FeatureSet]] = []
        explicitly_disconnected: list[str] = []

        for entry in peerlist_str.split(","):
            # Skip empty entries
            if not entry or not entry.strip():
                continue
            # Skip entries without separator - these are metadata (e.g., 'peerlist_features')
            # from the reference implementation, not actual peer entries
            if NICK_PEERLOCATOR_SEPARATOR not in entry:
                logger.debug(f"Skipping metadata entry in peerlist: '{entry}'")
                continue
            try:
                nick, location, disconnected, features = parse_peerlist_entry(entry)
                logger.debug(
                    f"Parsed peer: {nick} at {location}, "
                    f"disconnected={disconnected}, features={features.to_comma_string()}"
                )
                if disconnected:
                    # Nick explicitly marked as disconnected - remove their offers
                    explicitly_disconnected.append(nick)
                else:
                    peers.append((nick, location, features))
                    # Update/add this nick to active peers
                    self._active_peers[nick] = location
                    # Merge features into peer_features cache (never overwrite/downgrade)
                    # This prevents losing features when receiving peerlist from directories
                    # that don't support peerlist_features
                    features_dict = features.to_dict()
                    self._merge_peer_features(nick, features_dict)

                    # Update features on any cached offers for this peer
                    # This fixes the race condition where offers are stored before
                    # peerlist response arrives with features
                    self._update_offer_features(nick, features_dict)
            except ValueError as e:
                logger.warning(f"Failed to parse peerlist entry '{entry}': {e}")
                continue

        # Only remove offers for nicks that are explicitly marked as disconnected
        for nick in explicitly_disconnected:
            self.remove_offers_for_nick(nick)

        logger.trace(
            f"Received {len(peers)} active peers with features from {self.host}:{self.port}"
            + (
                f", {len(explicitly_disconnected)} explicitly disconnected"
                if explicitly_disconnected
                else ""
            )
        )
        return peers

    async def listen_for_messages(self, duration: float = 5.0) -> list[dict[str, Any]]:
        """
        Listen for messages for a specified duration.

        This method collects all messages received within the specified duration.
        It properly handles connection closed errors by raising DirectoryClientError.

        Args:
            duration: How long to listen in seconds

        Returns:
            List of received messages

        Raises:
            DirectoryClientError: If not connected or connection is lost
        """
        if not self.connection:
            raise DirectoryClientError("Not connected")

        # Check connection state before starting
        if not self.connection.is_connected():
            raise DirectoryClientError("Connection closed")

        messages: list[dict[str, Any]] = []
        start_time = asyncio.get_event_loop().time()

        # First, drain any buffered messages into our result list
        # These are messages that were received while waiting for other responses
        while not self._message_buffer.empty():
            try:
                buffered_msg = self._message_buffer.get_nowait()
                logger.trace(
                    f"Processing buffered message type {buffered_msg.get('type')}: "
                    f"{buffered_msg.get('line', '')[:80]}..."
                )
                messages.append(buffered_msg)
            except asyncio.QueueEmpty:
                break

        # Track consecutive errors to prevent tight loops on persistent failures
        consecutive_errors = 0
        max_consecutive_errors = 5

        while asyncio.get_event_loop().time() - start_time < duration:
            try:
                remaining_time = duration - (asyncio.get_event_loop().time() - start_time)
                if remaining_time <= 0:
                    break

                response_data = await asyncio.wait_for(
                    self.connection.receive(), timeout=remaining_time
                )
                response = json.loads(response_data.decode("utf-8"))
                logger.trace(
                    f"Received message type {response.get('type')}: "
                    f"{response.get('line', '')[:80]}..."
                )
                messages.append(response)
                consecutive_errors = 0

            except TimeoutError:
                # Normal timeout - no more messages within duration
                break
            except NetworkConnectionError as e:
                # Connection-level errors from our network layer - always propagate
                raise DirectoryClientError(f"Connection lost: {e}") from e
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                # System-level connection errors that bypassed our network layer
                raise DirectoryClientError(f"Connection lost: {e}") from e
            except Exception as e:
                # Other errors (JSON parse, etc) - log and continue, but with a limit
                consecutive_errors += 1
                logger.warning(f"Error processing message: {e}")
                if consecutive_errors >= max_consecutive_errors:
                    raise DirectoryClientError(
                        f"Too many consecutive errors ({consecutive_errors}), last error: {e}"
                    ) from e
                continue

        logger.trace(f"Collected {len(messages)} messages in {duration}s")
        return messages

    async def fetch_orderbooks(
        self,
        *,
        max_wait: float = 120.0,
        min_wait: float = 30.0,
        quiet_period: float = 15.0,
    ) -> tuple[list[Offer], list[FidelityBond]]:
        """
        Fetch orderbooks from all connected peers.

        Uses adaptive listening: collects offers in small time chunks and exits early
        when no new offers have arrived for ``quiet_period`` seconds (after at least
        ``min_wait`` seconds have elapsed).

        Trusts the directory's orderbook as authoritative - if a maker has an offer
        in the directory, they are considered online. The directory server maintains
        the connection state and removes offers when makers disconnect.

        Args:
            max_wait: Hard ceiling in seconds (default 120). Based on empirical Tor
                testing: 95th percentile ~101s, 99th percentile ~115s.
            min_wait: Minimum seconds before early exit is allowed (default 30).
                Prevents cutting off slow Tor responses during the initial burst.
            quiet_period: Seconds without new offers to trigger early exit (default 15).
                After min_wait, if no new offers arrive for this long, all responsive
                makers are assumed to have replied.

        Returns:
            Tuple of (offers, fidelity_bonds)
        """
        # Use get_peerlist_with_features to populate peer_features cache for neutrino_compat
        # detection. The peerlist itself is not used for offer filtering.
        peers_with_features = await self.get_peerlist_with_features()
        offers: list[Offer] = []
        bonds: list[FidelityBond] = []
        bond_utxo_set: set[str] = set()

        # Log peer count for visibility (but don't filter based on peerlist)
        if peers_with_features:
            logger.info(f"Found {len(peers_with_features)} peers on {self.host}:{self.port}")

        if not self.connection:
            raise DirectoryClientError("Not connected")

        pubmsg = {
            "type": MessageType.PUBMSG.value,
            "line": f"{self.nick}!PUBLIC!!orderbook",
        }
        await self.connection.send(json.dumps(pubmsg).encode("utf-8"))
        logger.debug("Sent !orderbook broadcast to PUBLIC")

        # Adaptive orderbook listening: instead of waiting a fixed duration, we listen
        # in small chunks and exit early once offers stop arriving. This dramatically
        # reduces wait times when the network is responsive (e.g., regtest: ~2s instead
        # of 120s) while still handling slow Tor responses on mainnet.
        #
        # Parameters:
        #   max_wait: Hard ceiling (default 120s). Based on empirical Tor testing:
        #     - 95th percentile response time: ~101s
        #     - 99th percentile: ~115s, max observed: ~119s
        #   min_wait: Floor before early exit is allowed (default 30s). Prevents
        #     cutting off slow Tor responses during the initial burst of replies.
        #   quiet_period: Seconds without new offers before exiting (default 15s).
        #     After min_wait, if no new offers arrive for this long, we assume all
        #     responsive makers have replied.

        # Sanity: clamp min_wait and quiet_period so they fit within max_wait
        min_wait = min(min_wait, max_wait)
        quiet_period = min(quiet_period, max_wait - min_wait) if max_wait > min_wait else 0.0

        logger.info(
            f"Listening for offers (max={max_wait}s, min={min_wait}s, quiet={quiet_period}s)..."
        )

        # Offer type prefixes for lightweight detection during listening.
        # Full parsing happens after collection -- this is just for counting.
        offer_prefixes = (
            "sw0absoffer",
            "sw0reloffer",
            "swabsoffer",
            "swreloffer",
            "tr0absoffer",
            "tr0reloffer",
            "traabsoffer",
            "trareloffer",
        )

        messages: list[dict[str, Any]] = []
        offer_count = 0
        start_time = asyncio.get_event_loop().time()
        last_offer_time = start_time
        chunk_duration = 1.0  # Listen in 1s chunks for responsiveness

        while True:
            elapsed = asyncio.get_event_loop().time() - start_time
            if elapsed >= max_wait:
                break

            # Check early exit: past min_wait and no new offers for quiet_period
            if elapsed >= min_wait and quiet_period > 0:
                silence = asyncio.get_event_loop().time() - last_offer_time
                if silence >= quiet_period:
                    logger.info(
                        f"No new offers for {silence:.1f}s after {offer_count} offers, "
                        f"exiting early at {elapsed:.1f}s"
                    )
                    break

            remaining = max_wait - elapsed
            listen_time = min(chunk_duration, remaining)
            if listen_time <= 0:
                break

            chunk = await self.listen_for_messages(duration=listen_time)
            new_offers = 0
            for msg in chunk:
                messages.append(msg)
                # Lightweight offer detection: check if the line contains an offer type
                line = msg.get("line", "")
                if any(prefix in line for prefix in offer_prefixes):
                    new_offers += 1

            if new_offers > 0:
                offer_count += new_offers
                last_offer_time = asyncio.get_event_loop().time()
                logger.debug(f"+{new_offers} offers (total: {offer_count}) at {elapsed:.1f}s")

        total_elapsed = asyncio.get_event_loop().time() - start_time
        logger.info(
            f"Collected {len(messages)} messages ({offer_count} offers) in {total_elapsed:.1f}s"
        )

        for response in messages:
            try:
                msg_type = response.get("type")
                line = response["line"]

                # Handle PEERLIST messages to keep peer features and active peers updated
                if msg_type == MessageType.PEERLIST.value:
                    try:
                        self._handle_peerlist_response(line)
                        logger.debug("Processed PEERLIST during orderbook fetch")
                    except Exception as e:
                        logger.debug(f"Failed to process PEERLIST: {e}")
                    continue

                if msg_type not in (MessageType.PUBMSG.value, MessageType.PRIVMSG.value):
                    logger.debug(f"Skipping message type {msg_type}")
                    continue

                logger.debug(f"Processing message type {msg_type}: {line[:100]}...")

                parts = line.split(COMMAND_PREFIX)
                if len(parts) < 3:
                    logger.debug(f"Message has insufficient parts: {len(parts)}")
                    continue

                from_nick = parts[0]
                to_nick = parts[1]
                rest = COMMAND_PREFIX.join(parts[2:])

                if not rest.strip():
                    logger.debug("Empty message content")
                    continue

                result = self._parse_offer_from_message(rest, from_nick, to_nick, msg_type)
                if result is not None:
                    offer, bond_data, _neutrino_compat = result
                    offers.append(offer)

                    if bond_data:
                        utxo_str = f"{bond_data['utxo_txid']}:{bond_data['utxo_vout']}"
                        if utxo_str not in bond_utxo_set:
                            bond_utxo_set.add(utxo_str)
                            bond = FidelityBond(
                                counterparty=from_nick,
                                utxo_txid=bond_data["utxo_txid"],
                                utxo_vout=bond_data["utxo_vout"],
                                locktime=bond_data["locktime"],
                                script=bond_data["utxo_pub"],
                                utxo_confirmations=0,
                                cert_expiry=bond_data["cert_expiry"],
                                fidelity_bond_data=bond_data,
                            )
                            bonds.append(bond)
                else:
                    logger.debug(f"Message not an offer: {rest[:50]}...")

            except Exception as e:
                logger.warning(f"Failed to process message: {e}")
                continue

        # NOTE: We trust the directory's orderbook as authoritative.
        # If a maker has an offer in the directory, they are considered online.
        # The directory server maintains the connection state and removes offers
        # when makers disconnect. Peerlist responses may be delayed or unavailable,
        # so we don't filter offers based on peerlist presence.
        #
        # This prevents incorrectly rejecting valid offers from active makers
        # whose peerlist entry hasn't been received yet.

        logger.info(
            f"Fetched {len(offers)} offers and {len(bonds)} fidelity bonds from "
            f"{self.host}:{self.port}"
        )
        return offers, bonds

    async def send_public_message(self, message: str) -> None:
        """
        Send a public message to all peers.

        Args:
            message: Message to broadcast
        """
        if not self.connection:
            raise DirectoryClientError("Not connected")

        pubmsg = {
            "type": MessageType.PUBMSG.value,
            "line": f"{self.nick}!PUBLIC!{message}",
        }
        await self.connection.send(json.dumps(pubmsg).encode("utf-8"))

    async def send_private_message(self, recipient: str, command: str, data: str) -> None:
        """
        Send a signed private message to a specific peer.

        JoinMarket requires all private messages to be signed with the sender's
        nick private key. The signature is appended to the message:
        Format: "!<command> <data> <pubkey_hex> <signature>"

        The message-to-sign is: data + hostid (to prevent replay attacks)
        Note: Only the data is signed, NOT the command prefix.

        Args:
            recipient: Target peer nick
            command: Command name (without ! prefix, e.g., 'fill', 'auth', 'tx')
            data: Command arguments to send (will be signed)
        """
        if not self.connection:
            raise DirectoryClientError("Not connected")

        # Sign just the data (not the command) with our nick identity
        # Reference: rawmessage = ' '.join(message[1:].split(' ')[1:-2])
        # This means they extract [1:-2] which is the args, not the command
        # So we sign: data + hostid
        # IMPORTANT: Always use ONION_HOSTID ("onion-network"), NOT the directory hostname.
        # The reference implementation uses a fixed hostid for ALL onion message channels
        # (see jmdaemon/onionmc.py line 635: self.hostid = "onion-network")
        signed_data = self.nick_identity.sign_message(data, ONION_HOSTID)

        # JoinMarket message format: from_nick!to_nick!command <args>
        # The COMMAND_PREFIX ("!") is used ONLY as a field separator between
        # from_nick, to_nick, and the message content. The command itself
        # does NOT have a "!" prefix.
        # Format: "<command> <signed_data>" where signed_data = "<data> <pubkey_hex> <sig_b64>"
        full_message = f"{command} {signed_data}"

        privmsg = {
            "type": MessageType.PRIVMSG.value,
            "line": f"{self.nick}!{recipient}!{full_message}",
        }
        await self.connection.send(json.dumps(privmsg).encode("utf-8"))

    async def close(self) -> None:
        """Close the connection to the directory server."""
        if self.connection:
            try:
                # NOTE: We skip sending DISCONNECT (801) because the reference implementation
                # crashes on unhandled control messages.
                pass
            except Exception:
                pass
            finally:
                await self.connection.close()
                self.connection = None

    def stop(self) -> None:
        """Stop continuous listening."""
        self.running = False

    async def listen_continuously(self, request_orderbook: bool = True) -> None:
        """
        Continuously listen for messages and update internal offer/bond caches.

        This method runs indefinitely until stop() is called or connection is lost.
        Used by orderbook_watcher and maker to maintain live orderbook state.

        Args:
            request_orderbook: If True, send !orderbook request on startup to get
                current offers from makers. Set to False for maker bots that don't
                need to receive other offers.
        """
        if not self.connection:
            raise DirectoryClientError("Not connected")

        logger.info(f"Starting continuous listening on {self.host}:{self.port}")
        self.running = True

        # Fetch peerlist with features to populate peer_features cache
        # This allows us to know which features each maker supports
        # Note: This may return empty if directory doesn't support GETPEERLIST (reference impl)
        try:
            await self.get_peerlist_with_features()
            if self._peerlist_supported:
                logger.info(f"Populated peer_features cache with {len(self.peer_features)} peers")
            else:
                logger.info(
                    "Directory doesn't support GETPEERLIST - peer features will be "
                    "learned from offer messages"
                )
        except Exception as e:
            logger.warning(f"Failed to fetch peerlist with features: {e}")

        # Request current orderbook from makers
        if request_orderbook:
            try:
                pubmsg = {
                    "type": MessageType.PUBMSG.value,
                    "line": f"{self.nick}!PUBLIC!!orderbook",
                }
                await self.connection.send(json.dumps(pubmsg).encode("utf-8"))
                logger.info("Sent !orderbook request to get current offers")
            except Exception as e:
                logger.warning(f"Failed to send !orderbook request: {e}")

        # Track when we last sent an orderbook request (to avoid spamming)
        last_orderbook_request = time.time()
        orderbook_request_min_interval = 60.0  # Minimum 60 seconds between requests

        while self.running:
            try:
                # First check if we have buffered messages from previous operations
                # (e.g., messages received while waiting for PEERLIST)
                if not self._message_buffer.empty():
                    message = await self._message_buffer.get()
                    logger.trace("Processing buffered message from queue")
                else:
                    # Read next message with timeout
                    data = await asyncio.wait_for(self.connection.receive(), timeout=5.0)

                    if not data:
                        logger.warning(f"Connection to {self.host}:{self.port} closed")
                        break

                    message = json.loads(data.decode("utf-8"))
                msg_type = message.get("type")
                line = message.get("line", "")

                # Handle PEERLIST responses (from periodic or automatic requests)
                if msg_type == MessageType.PEERLIST.value:
                    try:
                        self._handle_peerlist_response(line)
                    except Exception as e:
                        logger.debug(f"Failed to process PEERLIST: {e}")
                    continue

                # Process PUBMSG and PRIVMSG to update offers/bonds cache
                # Reference implementation sends offer responses to !orderbook via PRIVMSG
                if msg_type in (MessageType.PUBMSG.value, MessageType.PRIVMSG.value):
                    try:
                        parts = line.split(COMMAND_PREFIX)
                        if len(parts) >= 3:
                            from_nick = parts[0]
                            to_nick = parts[1]
                            rest = COMMAND_PREFIX.join(parts[2:])

                            # Accept PUBLIC broadcasts or messages addressed to us
                            if to_nick == "PUBLIC" or to_nick == self.nick:
                                # If we don't have features for this peer, it's a new peer.
                                # Track them with empty features for now - we'll get their features
                                # from the initial peerlist or from their offer messages
                                is_new_peer = from_nick not in self.peer_features
                                current_time = time.time()

                                if is_new_peer:
                                    # Track new peer - merge empty features (will be a no-op
                                    # if we already know their features from another source)
                                    # Features will be populated from offer messages or peerlist
                                    self._merge_peer_features(from_nick, {})
                                    logger.debug(f"Discovered new peer: {from_nick}")

                                    # If directory supports peerlist_features, request updated peerlist
                                    # to get this peer's features immediately
                                    if (
                                        self.directory_peerlist_features
                                        and self._peerlist_supported
                                    ):
                                        try:
                                            # Request peerlist to get features for new peer
                                            # This is a background task - don't block message processing
                                            asyncio.create_task(
                                                self._refresh_peerlist_for_new_peer()
                                            )
                                        except Exception as e:
                                            logger.debug(
                                                f"Failed to request peerlist for new peer: {e}"
                                            )

                                    # Request orderbook from new peer (rate-limited)
                                    if (
                                        request_orderbook
                                        and current_time - last_orderbook_request
                                        > orderbook_request_min_interval
                                    ):
                                        try:
                                            pubmsg = {
                                                "type": MessageType.PUBMSG.value,
                                                "line": f"{self.nick}!PUBLIC!!orderbook",
                                            }
                                            await self.connection.send(
                                                json.dumps(pubmsg).encode("utf-8")
                                            )
                                            last_orderbook_request = current_time
                                            logger.info(
                                                f"Sent !orderbook request for new peer {from_nick}"
                                            )
                                        except Exception as e:
                                            logger.debug(f"Failed to send !orderbook: {e}")

                                # Parse offer announcements
                                result = self._parse_offer_from_message(
                                    rest, from_nick, to_nick, msg_type
                                )
                                if result is not None:
                                    offer, bond_data, _neutrino_compat = result

                                    # Store bond in bonds cache
                                    if bond_data:
                                        utxo_str = (
                                            f"{bond_data['utxo_txid']}:{bond_data['utxo_vout']}"
                                        )
                                        bond = FidelityBond(
                                            counterparty=from_nick,
                                            utxo_txid=bond_data["utxo_txid"],
                                            utxo_vout=bond_data["utxo_vout"],
                                            locktime=bond_data["locktime"],
                                            script=bond_data["utxo_pub"],
                                            utxo_confirmations=0,
                                            cert_expiry=bond_data["cert_expiry"],
                                            fidelity_bond_data=bond_data,
                                        )
                                        self.bonds[utxo_str] = bond

                                    # Extract bond UTXO key for deduplication
                                    bond_utxo_key: str | None = None
                                    if bond_data:
                                        bond_utxo_key = (
                                            f"{bond_data['utxo_txid']}:{bond_data['utxo_vout']}"
                                        )

                                    # Update cache using tuple key
                                    offer_key = (from_nick, offer.oid)
                                    self._store_offer(offer_key, offer, bond_utxo_key)

                                    # Track this peer as "known" even if peerlist didn't
                                    # return features. This prevents re-triggering new peer
                                    # logic for every message from this peer.
                                    if from_nick not in self.peer_features:
                                        self.peer_features[from_nick] = {}

                                    logger.debug(
                                        f"Updated offer cache: {from_nick} "
                                        f"{offer.ordertype.value} oid={offer.oid}"
                                        + (" (with bond)" if bond_data else "")
                                    )
                    except Exception as e:
                        logger.debug(f"Failed to process PUBMSG: {e}")

            except TimeoutError:
                continue
            except asyncio.CancelledError:
                logger.info(f"Continuous listening on {self.host}:{self.port} cancelled")
                break
            except Exception as e:
                logger.error(f"Error in continuous listening: {e}")
                if self.on_disconnect:
                    self.on_disconnect()
                break

        self.running = False
        logger.info(f"Stopped continuous listening on {self.host}:{self.port}")

    def _parse_offer_from_message(
        self,
        rest: str,
        from_nick: str,
        to_nick: str,
        msg_type: str | None,
    ) -> tuple[Offer, dict[str, Any] | None, bool] | None:
        """
        Parse an offer from a message's content part.

        Handles all offer types (sw0reloffer, sw0absoffer, swreloffer, swabsoffer),
        optional fidelity bond proof, and the deprecated !neutrino flag.

        Args:
            rest: The message content after from_nick!to_nick! (may contain !-separated flags)
            from_nick: The sender's nick
            to_nick: The recipient nick (or "PUBLIC")
            msg_type: The message type value (PUBMSG or PRIVMSG)

        Returns:
            Tuple of (offer, bond_data, neutrino_compat) if parsing succeeds, None otherwise.
            bond_data is the parsed fidelity bond dict or None.
            neutrino_compat is True if the deprecated !neutrino flag was present.
        """
        offer_types = [
            "sw0absoffer",
            "sw0reloffer",
            "swabsoffer",
            "swreloffer",
            "tr0absoffer",
            "tr0reloffer",
            "traabsoffer",
            "trareloffer",
        ]
        for offer_type in offer_types:
            if not rest.startswith(offer_type):
                continue

            # Split on '!' to extract flags (neutrino, tbond)
            # Format: sw0reloffer 0 750000 790107726787 500 0.001!neutrino!tbond <proof>
            rest_parts = rest.split(COMMAND_PREFIX)
            offer_line = rest_parts[0]
            bond_data: dict[str, Any] | None = None
            neutrino_compat = False

            # Parse flags after the offer line
            for flag_part in rest_parts[1:]:
                if flag_part.startswith("neutrino"):
                    # NOTE: !neutrino in offers is deprecated - primary detection is via
                    # handshake features. Parsing kept for backwards compatibility.
                    neutrino_compat = True
                    logger.debug(f"Maker {from_nick} requires neutrino_compat")
                elif flag_part.startswith("tbond "):
                    bond_parts = flag_part[6:].split()
                    if bond_parts:
                        bond_proof_b64 = bond_parts[0]
                        # For PRIVMSG, the maker signs with taker's actual nick.
                        # For PUBMSG/PUBLIC, both nicks are the maker's (self-signed).
                        is_privmsg = msg_type == MessageType.PRIVMSG.value
                        taker_nick_for_proof = (
                            to_nick if (is_privmsg or to_nick != "PUBLIC") else from_nick
                        )
                        bond_data = parse_fidelity_bond_proof(
                            bond_proof_b64, from_nick, taker_nick_for_proof
                        )
                        if bond_data:
                            logger.debug(
                                f"Parsed fidelity bond from {from_nick}: "
                                f"txid={bond_data['utxo_txid'][:16]}..., "
                                f"locktime={bond_data['locktime']}"
                            )

            offer_parts = offer_line.split()
            if len(offer_parts) < 6:
                logger.warning(f"Offer from {from_nick} has {len(offer_parts)} parts, need 6")
                return None

            try:
                oid = int(offer_parts[1])
                minsize = int(offer_parts[2])
                maxsize = int(offer_parts[3])
                txfee = int(offer_parts[4])
                cjfee_str = offer_parts[5]

                if offer_type in ["sw0absoffer", "swabsoffer"]:
                    cjfee = str(int(cjfee_str))
                else:
                    cjfee = str(Decimal(cjfee_str))

                offer = Offer(
                    counterparty=from_nick,
                    oid=oid,
                    ordertype=OfferType(offer_type),
                    minsize=minsize,
                    maxsize=maxsize,
                    txfee=txfee,
                    cjfee=cjfee,
                    fidelity_bond_value=0,
                    fidelity_bond_data=bond_data,
                    neutrino_compat=neutrino_compat,
                    features=self.peer_features.get(from_nick, {}),
                )

                logger.debug(
                    f"Parsed {offer_type} from {from_nick}: "
                    f"oid={oid}, size={minsize}-{maxsize}, fee={cjfee}, "
                    f"has_bond={bond_data is not None}, neutrino_compat={neutrino_compat}"
                )
                return offer, bond_data, neutrino_compat
            except Exception as e:
                logger.warning(f"Failed to parse {offer_type} from {from_nick}: {e}")
                return None

        # No offer type matched
        return None

    def _store_offer(
        self,
        offer_key: tuple[str, int],
        offer: Offer,
        bond_utxo_key: str | None = None,
    ) -> None:
        """
        Store an offer with timestamp and handle bond-based deduplication.

        When a maker restarts with a new nick but the same fidelity bond, we need to
        remove the old offer(s) associated with that bond to prevent duplicates.

        Args:
            offer_key: Tuple of (counterparty, oid)
            offer: The offer to store
            bond_utxo_key: Bond UTXO key (txid:vout) if offer has a fidelity bond
        """
        current_time = time.time()

        # If this offer has a fidelity bond, check for and remove old offers with same bond
        if bond_utxo_key:
            # Get all offer keys that previously used this bond
            old_offer_keys = self._bond_to_offers.get(bond_utxo_key, set()).copy()

            # Remove old offers from DIFFERENT makers using same bond (maker restart scenario)
            # Keep multiple offers from SAME maker (same counterparty, different oids)
            for old_key in old_offer_keys:
                if (
                    old_key != offer_key
                    and old_key in self.offers
                    and old_key[0] != offer_key[0]  # Different counterparty
                ):
                    logger.debug(
                        f"Removing stale offer from {old_key[0]} oid={old_key[1]} - "
                        f"same bond UTXO now used by {offer_key[0]}"
                    )
                    del self.offers[old_key]

            # Update bond -> offers mapping: add this offer to the set
            if bond_utxo_key not in self._bond_to_offers:
                self._bond_to_offers[bond_utxo_key] = set()
            self._bond_to_offers[bond_utxo_key].add(offer_key)
        else:
            # Remove this offer from any previous bond mapping
            old_offer_data = self.offers.get(offer_key)
            if old_offer_data and old_offer_data.bond_utxo_key:
                old_bond_key = old_offer_data.bond_utxo_key
                if old_bond_key in self._bond_to_offers:
                    self._bond_to_offers[old_bond_key].discard(offer_key)

        # Store the new offer with timestamp
        self.offers[offer_key] = OfferWithTimestamp(
            offer=offer, received_at=current_time, bond_utxo_key=bond_utxo_key
        )

    def _update_offer_features(self, nick: str, features: dict[str, bool]) -> int:
        """
        Update features on all cached offers for a specific peer.

        This is called when we receive updated feature information from peerlist,
        ensuring that offers stored before features were known get updated.

        Args:
            nick: The nick to update features for
            features: New features dict to apply

        Returns:
            Number of offers updated
        """
        updated = 0
        for key, offer_ts in self.offers.items():
            if key[0] == nick:
                # Update features on the cached offer
                # Merge new features with any existing ones (new features take precedence)
                for feature, value in features.items():
                    if value:  # Only set true features
                        offer_ts.offer.features[feature] = value
                updated += 1

        if updated > 0:
            logger.debug(
                f"Updated features on {updated} cached offer(s) for {nick}: "
                f"{[k for k, v in features.items() if v]}"
            )

        return updated

    def _merge_peer_features(self, nick: str, new_features: dict[str, bool]) -> None:
        """
        Merge new features into the peer_features cache for a nick.

        Features are cumulative - once a peer advertises a feature, we keep it.
        This prevents losing features when receiving updates from directories
        that don't support peerlist_features.

        Args:
            nick: The peer's nick
            new_features: New features dict to merge (only True values are added)
        """
        existing = self.peer_features.get(nick, {})
        for feature, value in new_features.items():
            if value:  # Only set true features, never downgrade
                existing[feature] = value
        self.peer_features[nick] = existing

    def remove_offers_for_nick(self, nick: str) -> int:
        """
        Remove all offers from a specific nick (e.g., when nick goes offline).

        This is the equivalent of the reference implementation's on_nick_leave callback.

        Args:
            nick: The nick to remove offers for

        Returns:
            Number of offers removed
        """
        keys_to_remove = [key for key in self.offers if key[0] == nick]
        removed = 0

        for key in keys_to_remove:
            offer_data = self.offers.pop(key, None)
            if offer_data:
                removed += 1
                # Clean up bond mapping
                if offer_data.bond_utxo_key and offer_data.bond_utxo_key in self._bond_to_offers:
                    self._bond_to_offers[offer_data.bond_utxo_key].discard(key)

        if removed > 0:
            logger.info(f"Removed {removed} offers for nick {nick} (left/offline)")

        # Also remove from peer_features and active_peers
        self.peer_features.pop(nick, None)
        self._active_peers.pop(nick, None)

        # Remove any bonds from this nick
        bonds_to_remove = [k for k, v in self.bonds.items() if v.counterparty == nick]
        for bond_key in bonds_to_remove:
            del self.bonds[bond_key]

        return removed

    async def _refresh_peerlist_for_new_peer(self) -> None:
        """
        Refresh peerlist to get features for newly discovered peers.

        This is called as a background task when a new peer is discovered
        to immediately fetch their features from the directory's peerlist.
        """
        try:
            # Small delay to batch multiple new peer discoveries
            await asyncio.sleep(2.0)

            # Request peerlist - this will update peer_features
            peers = await self.get_peerlist_with_features()
            if peers:
                logger.debug(
                    f"Refreshed peerlist for new peer discovery: {len(peers)} active peers"
                )
        except Exception as e:
            logger.debug(f"Failed to refresh peerlist for new peer: {e}")

    def get_active_nicks(self) -> set[str]:
        """Get set of nicks from the last peerlist update."""
        return set(self._active_peers.keys())

    def cleanup_stale_offers(self, max_age_seconds: float = 1800.0) -> int:
        """
        Remove offers that haven't been re-announced within the staleness threshold.

        This is a fallback cleanup mechanism for directories that don't support
        GETPEERLIST (reference implementation). For offers with fidelity bonds,
        bond-based deduplication handles most cases, but this catches offers
        from makers that silently went offline.

        Args:
            max_age_seconds: Maximum age in seconds before an offer is considered stale.
                Default is 30 minutes (1800 seconds).

        Returns:
            Number of stale offers removed
        """
        current_time = time.time()
        stale_keys: list[tuple[str, int]] = []

        for key, offer_data in self.offers.items():
            age = current_time - offer_data.received_at
            if age > max_age_seconds:
                stale_keys.append(key)

        removed = 0
        for key in stale_keys:
            removed_offer: OfferWithTimestamp | None = self.offers.pop(key, None)
            if removed_offer:
                removed += 1
                # Clean up bond mapping
                if (
                    removed_offer.bond_utxo_key
                    and removed_offer.bond_utxo_key in self._bond_to_offers
                ):
                    self._bond_to_offers[removed_offer.bond_utxo_key].discard(key)
                logger.debug(
                    f"Removed stale offer from {key[0]} oid={key[1]} "
                    f"(age={current_time - removed_offer.received_at:.0f}s)"
                )

        if removed > 0:
            logger.info(f"Cleaned up {removed} stale offers (older than {max_age_seconds}s)")

        return removed

    def get_current_offers(self) -> list[Offer]:
        """Get the current list of cached offers."""
        return [offer_data.offer for offer_data in self.offers.values()]

    def get_offers_with_timestamps(self) -> list[OfferWithTimestamp]:
        """Get offers with their timestamp metadata."""
        return list(self.offers.values())

    def get_current_bonds(self) -> list[FidelityBond]:
        """Get the current list of cached fidelity bonds."""
        return list(self.bonds.values())

    def supports_extended_utxo_format(self) -> bool:
        """
        Check if we should use extended UTXO format with this directory.

        Extended format (txid:vout:scriptpubkey:blockheight) is used when
        both sides advertise neutrino_compat feature. Protocol version
        is not checked - features are negotiated independently.

        Returns:
            True if extended UTXO format should be used
        """
        return self.neutrino_compat and self.directory_neutrino_compat

    def get_negotiated_version(self) -> int:
        """
        Get the negotiated protocol version.

        Returns:
            Negotiated version (always 5 with feature-based approach)
        """
        return self.negotiated_version if self.negotiated_version is not None else JM_VERSION
