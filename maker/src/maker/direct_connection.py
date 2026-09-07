"""
Direct connection handling for the maker bot.

Contains methods for handling incoming direct (onion) peer connections,
including message parsing, handshake handling, and connection lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer
from jmcore.network import ONION_HOSTID, TCPConnection
from jmcore.network import ConnectionError as NetworkConnectionError
from jmcore.nick_auth import NickAuthMode
from jmcore.protocol import (
    COMMAND_PREFIX,
    FEATURE_NEUTRINO_COMPAT,
    FEATURE_NICK_AUTH,
    FEATURE_PEERLIST_FEATURES,
    FEATURE_PING,
    FeatureSet,
    MessageType,
    create_handshake_request,
)
from jmwallet.backends.base import BlockchainBackend
from loguru import logger

from maker.config import MakerConfig
from maker.protocols import MakerBotProtocol
from maker.rate_limiting import DirectConnectionRateLimiter

_MAX_DIRECT_CONNECTIONS = 256
_DIRECT_CONNECTION_IDLE_TIMEOUT_SEC = 60.0
_DIRECT_CONNECTION_UNAUTHENTICATED_TIMEOUT_SEC = 60.0


@dataclass(slots=True)
class DirectConnectionState:
    """Socket-local identity that becomes immutable after signature verification.

    The first verified sender overrides a mismatched provisional handshake nick.
    This favors availability after a forged preclaim without allowing one socket
    to act as multiple verified identities.
    """

    nick: str | None = None
    verified: bool = False


async def _handle_direct_public_message(
    bot: MakerBotProtocol,
    state: DirectConnectionState,
    sender_nick: str,
    command: str,
    connection: TCPConnection,
    peer_str: str,
    generation_id: int,
    unauthenticated_deadline: float,
) -> bool:
    """Handle a public direct message and report whether the socket stays open."""
    public_command = command[7:]
    if public_command != "orderbook":
        logger.trace(f"Unknown PUBLIC command from {sender_nick} via direct: {public_command}")
        return True

    if not bot._direct_connection_rate_limiter.check_orderbook(peer_str):
        violations = bot._direct_connection_rate_limiter.get_violation_count(peer_str)
        if bot._direct_connection_rate_limiter.is_banned(peer_str):
            logger.debug(
                f"Ignoring orderbook request from banned connection {peer_str} "
                f"(nick: {sender_nick})"
            )
            await connection.close()
            return False
        logger.debug(
            f"Rate limiting orderbook request from {peer_str} "
            f"(nick: {sender_nick}, violations: {violations})"
        )
        return True

    logger.trace(
        f"Received !orderbook request from {sender_nick} via direct connection, sending offers"
    )
    if state.verified:
        await bot._send_offers_via_direct_connection(sender_nick, connection, generation_id)
        return True

    remaining_unauthenticated_time = unauthenticated_deadline - time.monotonic()
    if remaining_unauthenticated_time <= 0:
        logger.bind(sensitive=True).debug(
            f"Direct connection from {peer_str} did not authenticate in time"
        )
        return False
    await asyncio.wait_for(
        bot._send_offers_via_direct_connection(sender_nick, connection, generation_id),
        timeout=remaining_unauthenticated_time,
    )
    return True


async def _process_direct_message(
    bot: MakerBotProtocol,
    connection: TCPConnection,
    data: bytes,
    peer_str: str,
    generation_id: int,
    unauthenticated_deadline: float,
) -> bool:
    """Process one direct message and report whether the socket stays open."""
    generation = bot._generation(generation_id)
    if generation is None:
        return False
    state = generation.direct_connection_states.get(connection)
    if state is None:
        return False

    if not bot._direct_connection_rate_limiter.check_message(peer_str):
        logger.debug("Rate limiting direct-connection message flood")
        logger.bind(sensitive=True).debug(f"Rate limiting message from {peer_str} (message flood)")
        return True

    if state.verified:
        handshake_handled = await bot._try_handle_handshake(
            connection, data, peer_str, generation_id
        )
    else:
        remaining_unauthenticated_time = unauthenticated_deadline - time.monotonic()
        if remaining_unauthenticated_time <= 0:
            logger.bind(sensitive=True).debug(
                f"Direct connection from {peer_str} did not authenticate in time"
            )
            return False
        handshake_handled = await asyncio.wait_for(
            bot._try_handle_handshake(connection, data, peer_str, generation_id),
            timeout=remaining_unauthenticated_time,
        )
    if handshake_handled:
        return True

    if state.nick is None:
        logger.warning("Dropping message before direct handshake")
        logger.bind(sensitive=True).warning(
            f"Dropping message before direct handshake from {peer_str}"
        )
        return True

    parsed = bot._parse_direct_message(data, generation_id)
    if parsed is None:
        data_str = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        logger.trace("Received unparseable direct message")
        logger.bind(sensitive=True).trace(
            f"Unparseable direct message from {peer_str}: {data_str!r}"
        )
        msg_preview = data_str[:100] + "..." if len(data_str) > 100 else data_str
        bot._log_rate_limited(
            f"direct_parse_fail:{peer_str}",
            f"Failed to parse direct message from {peer_str}: {msg_preview!r}",
            interval=10,
        )
        return True

    sender_nick, command, message_data = parsed
    logger.trace(f"Direct message from {sender_nick}: cmd={command}")

    if command.startswith("PUBLIC:"):
        if sender_nick != state.nick:
            logger.warning(
                f"Dropping direct message claiming {sender_nick} on {state.nick}'s connection"
            )
            return True
        return await _handle_direct_public_message(
            bot,
            state,
            sender_nick,
            command,
            connection,
            peer_str,
            generation_id,
            unauthenticated_deadline,
        )

    if state.verified:
        if sender_nick != state.nick:
            logger.warning(
                f"Dropping verified direct message from {sender_nick} on {state.nick}'s connection"
            )
            return True
    else:
        if sender_nick != state.nick:
            logger.trace(
                f"Verified sender {sender_nick} overrides provisional direct "
                f"handshake nick {state.nick} from {peer_str}"
            )
        state.nick = sender_nick
        state.verified = True
        # This map is a non-authoritative routing/lifecycle hint. Duplicate
        # verified senders are allowed and the newest socket is its current hint.
        generation.direct_connections[sender_nick] = connection

    full_message = f"{command} {message_data}" if message_data else command
    if command == "fill":
        await bot._handle_fill(
            sender_nick, full_message, source="direct", generation_id=generation_id
        )
    elif command == "auth":
        await bot._handle_auth(
            sender_nick, full_message, source="direct", generation_id=generation_id
        )
    elif command == "tx":
        await bot._handle_tx(
            sender_nick, full_message, source="direct", generation_id=generation_id
        )
    elif command == "push":
        await bot._handle_push(
            sender_nick, full_message, source="direct", generation_id=generation_id
        )
    else:
        logger.trace(f"Unknown direct command from {sender_nick}: {command}")
    return True


class DirectConnectionMixin:
    """Mixin class providing direct connection handling methods for MakerBot.

    These methods handle incoming connections from takers via the hidden service,
    including message parsing, handshake protocol, and message routing.
    """

    # -- Attributes provided by MakerBot --
    running: bool
    config: MakerConfig
    backend: BlockchainBackend
    nick: str
    nick_identity: NickIdentity
    current_offers: list[Offer]
    directory_clients: dict[str, DirectoryClient]
    direct_connections: dict[str, TCPConnection]
    _direct_connection_states: dict[TCPConnection, DirectConnectionState]
    _direct_connection_rate_limiter: DirectConnectionRateLimiter

    def _remove_direct_connection(
        self: MakerBotProtocol, connection: TCPConnection, generation_id: int | None = None
    ) -> None:
        """Forget one socket without disturbing a newer routing hint."""
        generation_id = self.current_generation_id if generation_id is None else generation_id
        generation = self._generation(generation_id)
        if generation is None:
            return
        states = generation.direct_connection_states
        connections = generation.direct_connections
        states.pop(connection, None)
        for nick, registered_connection in list(connections.items()):
            if registered_connection is connection:
                del connections[nick]

    def _parse_direct_message(
        self: MakerBotProtocol, data: bytes, generation_id: int | None = None
    ) -> tuple[str, str, str] | None:
        """Parse a direct connection message supporting both formats.

        The reference implementation uses OnionCustomMessage format:
            {"type": 685, "line": "from_nick!to_nick!command data"}
        Where type 685 = PRIVMSG, type 687 = PUBMSG.

        Returns:
            (sender_nick, command, message_data) tuple or None if parsing fails.
            For PUBMSG (orderbook), returns (sender_nick, "PUBLIC:orderbook", "").
        """
        try:
            generation_id = self.current_generation_id if generation_id is None else generation_id
            generation = self._generation(generation_id)
            if generation is None:
                return None
            message = json.loads(data.decode("utf-8"))
        except json.JSONDecodeError:
            return None

        # Check for reference implementation format: {"type": int, "line": str}
        if "type" in message and "line" in message:
            msg_type = message.get("type")
            line = message.get("line", "")

            # Handle PUBMSG (687) - typically orderbook requests
            if msg_type == MessageType.PUBMSG.value:
                # Parse line format: from_nick!PUBLIC!command
                parts = line.split(COMMAND_PREFIX)
                if len(parts) < 3:
                    logger.trace(f"Invalid PUBMSG line format: {line[:50]}...")
                    return None

                sender_nick = parts[0]
                to_nick = parts[1]
                rest = COMMAND_PREFIX.join(parts[2:]).strip().lstrip("!")

                if to_nick == "PUBLIC":
                    # Return special marker for public messages
                    logger.trace(
                        f"Received PUBMSG from {sender_nick} via direct connection: {rest}"
                    )
                    return (sender_nick, f"PUBLIC:{rest}", "")
                else:
                    logger.trace(f"Ignoring PUBMSG with non-PUBLIC target: {to_nick}")
                    return None

            # Handle PRIVMSG (685) for CoinJoin protocol
            if msg_type != MessageType.PRIVMSG.value:
                logger.trace(f"Ignoring message type {msg_type} on direct connection")
                return None

            # Parse line format: from_nick!to_nick!command data
            parts = line.split(COMMAND_PREFIX)
            if len(parts) < 3:
                logger.warning(f"Invalid line format: {line[:50]}...")
                return None

            sender_nick = parts[0]
            to_nick = parts[1]
            rest = COMMAND_PREFIX.join(parts[2:])

            # Check if message is for us
            if to_nick != generation.nick_identity.nick:
                logger.trace(
                    f"Ignoring message not for us: to={to_nick}, us={generation.nick_identity.nick}"
                )
                return None

            # Authenticate the same signed envelope used through directories.
            rest = rest.strip().lstrip("!")
            authenticated, command, msg_data = verify_signed_privmsg(
                sender_nick, rest, ONION_HOSTID
            )
            if not authenticated:
                logger.warning(f"Dropping unauthenticated direct message from {sender_nick}")
                return None

            return (sender_nick, command, msg_data)

        return None

    async def _try_handle_handshake(
        self: MakerBotProtocol,
        connection: TCPConnection,
        data: bytes,
        peer_str: str,
        generation_id: int | None = None,
    ) -> bool:
        """Try to handle a handshake request on a direct connection.

        The connecting peer sends HANDSHAKE (793). A reciprocal peer HANDSHAKE
        is optional in the base protocol; this maker sends one to advertise
        negotiated extensions. DN_HANDSHAKE (795) is directory-only.

        Args:
            connection: The TCP connection
            data: Raw message data
            peer_str: Peer identifier string for logging

        Returns:
            True if this was a handshake message (handled), False otherwise.
        """
        try:
            generation_id = self.current_generation_id if generation_id is None else generation_id
            generation = self._generation(generation_id)
            if generation is None:
                return True
            states = generation.direct_connection_states
            message = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return False

        # Check for handshake message type (793 = HANDSHAKE)
        if message.get("type") != MessageType.HANDSHAKE.value:
            return False

        # Parse the handshake request
        try:
            line = message.get("line", "")
            handshake_data = json.loads(line) if isinstance(line, str) else line
        except json.JSONDecodeError:
            logger.warning("Invalid direct-connection handshake JSON")
            logger.bind(sensitive=True).warning(f"Invalid handshake JSON from {peer_str}")
            return True  # Was a handshake message, just malformed

        peer_nick = handshake_data.get("nick", "unknown")
        peer_network = handshake_data.get("network", "")

        state = states.setdefault(connection, DirectConnectionState())
        if state.nick is not None and state.nick != peer_nick:
            logger.warning(
                f"Rejecting nick change on direct connection from {peer_str}: "
                f"{state.nick} -> {peer_nick}"
            )
            await connection.close()
            self._remove_direct_connection(connection, generation_id)
            return True

        # Parse peer's advertised features (supports both dict and comma-string formats)
        peer_features_raw = handshake_data.get("features", "")
        peer_features = FeatureSet()
        if isinstance(peer_features_raw, dict):
            # Reference implementation format: {"peerlist_features": True, ...}
            for feature_name, enabled in peer_features_raw.items():
                if enabled:
                    peer_features.features.add(feature_name)
        elif isinstance(peer_features_raw, str) and peer_features_raw:
            # Comma-separated string format: "neutrino_compat,peerlist_features"
            peer_features = FeatureSet.from_comma_string(peer_features_raw)
        peer_version = handshake_data.get("version", handshake_data.get("proto-ver", "unknown"))

        logger.trace("Received direct-connection handshake")
        logger.bind(sensitive=True).trace(f"Received handshake from {peer_nick} at {peer_str}")
        logger.trace(
            f"Peer {peer_nick} handshake details: version={peer_version}, "
            f"network={peer_network or 'unspecified'}, "
            f"features={peer_features.to_comma_string() or 'none'}"
        )

        # Validate network
        # testnet and signet share the same address encoding (bech32 HRP "tb",
        # version byte 0x6F) so the reference implementation advertises "testnet"
        # for both.  Accept either when we are on signet (or testnet).
        testnet_family = {"testnet", "signet"}
        our_net = self.config.network.value
        if peer_network and peer_network != our_net:
            if not (peer_network in testnet_family and our_net in testnet_family):
                logger.warning(
                    f"Network mismatch from {peer_nick}: "
                    f"{peer_network} != {our_net}. "
                    f"Not responding to handshake."
                )
                return True

        # The unsigned handshake nick is only a socket-local hint. It neither
        # reserves the nick globally nor authenticates later private messages.
        state.nick = peer_nick

        # Build our feature set for the handshake
        features = FeatureSet(features={FEATURE_PEERLIST_FEATURES, FEATURE_PING})
        if self.backend.can_provide_neutrino_metadata():
            features.features.add(FEATURE_NEUTRINO_COMPAT)
        if self.config.nick_auth_mode is not NickAuthMode.DISABLED:
            features.features.add(FEATURE_NICK_AUTH)

        # Determine our location string (onion address or NOT-SERVING-ONION)
        onion_host = generation.onion_host
        if onion_host:
            our_location = f"{onion_host}:{self.config.onion_serving_port}"
        else:
            our_location = "NOT-SERVING-ONION"

        # Advertise optional capabilities with a reciprocal peer HANDSHAKE.
        response_data = create_handshake_request(
            nick=generation.nick_identity.nick,
            location=our_location,
            network=self.config.network.value,
            directory=False,
            features=features,
        )
        response_msg = {
            "type": MessageType.HANDSHAKE.value,
            "line": json.dumps(response_data),
        }
        try:
            await connection.send(json.dumps(response_msg).encode("utf-8"))
            logger.trace(
                f"Sent handshake to {peer_nick} (features: {features.to_comma_string() or 'none'})"
            )
        except Exception as e:
            logger.warning("Failed to send direct-connection handshake")
            logger.bind(sensitive=True).warning(f"Failed to send handshake to {peer_str}: {e}")

        return True

    async def _on_direct_connection(
        self: MakerBotProtocol,
        connection: TCPConnection,
        peer_str: str,
        generation_id: int | None = None,
    ) -> None:
        """Handle incoming direct connection from a taker via hidden service.

        Direct connections support three message formats:

        1. Handshake request (health check / feature discovery):
           {"type": 793, "line": "<json handshake data>"}
           Maker responds with handshake response including features.

        2. Reference implementation format (OnionCustomMessage):
           {"type": 685, "line": "from_nick!to_nick!command data"}
           Where type 685 = PRIVMSG.

        3. Our simplified format:
           {"nick": "sender", "cmd": "command", "data": "..."}

        This bypasses the directory server for lower latency once the taker
        knows the maker's onion address (from the peerlist).

        Rate Limiting Strategy:
        - Direct connections are rate limited by connection address (peer_str), not by nick
        - This prevents nick rotation attacks where attackers use different nicks per request
        - Attackers connecting directly to the onion bypass directory-level protections
        - Connection-based limiting is stricter: faster bans, longer intervals
        """
        logger.trace("Handling direct connection")
        logger.bind(sensitive=True).trace(f"Handling direct connection from {peer_str}")

        # Check if this connection is already banned
        if self._direct_connection_rate_limiter.is_banned(peer_str):
            logger.debug("Rejecting banned direct connection")
            logger.bind(sensitive=True).debug(
                f"Rejecting direct connection from banned address {peer_str}"
            )
            await connection.close()
            return

        generation_id = self.current_generation_id if generation_id is None else generation_id
        generation = self._generation(generation_id)
        if generation is None:
            await connection.close()
            return
        states = generation.direct_connection_states

        # Generation state is authoritative. The compatibility aliases point
        # at the current generation, but counting only a nick routing hint or
        # the current generation would let duplicate nicks and rotations evade
        # this process-wide socket bound. This check and insertion deliberately
        # have no await between them.
        if connection not in states:
            direct_connection_count = sum(
                len(generation.direct_connection_states) for generation in self.generations.values()
            )
            if direct_connection_count >= _MAX_DIRECT_CONNECTIONS:
                logger.warning("Rejecting direct connection because the socket limit is reached")
                logger.bind(sensitive=True).warning(
                    f"Rejecting direct connection from {peer_str}: "
                    f"{direct_connection_count}/{_MAX_DIRECT_CONNECTIONS} sockets in use"
                )
                await connection.close()
                return
            states[connection] = DirectConnectionState()

        unauthenticated_deadline = time.monotonic() + _DIRECT_CONNECTION_UNAUTHENTICATED_TIMEOUT_SEC

        try:
            # Keep connection open and process messages
            while self.running and connection.is_connected():
                state = states.get(connection)
                if state is None:
                    break
                receive_timeout = _DIRECT_CONNECTION_IDLE_TIMEOUT_SEC
                if not state.verified:
                    remaining_unauthenticated_time = unauthenticated_deadline - time.monotonic()
                    if remaining_unauthenticated_time <= 0:
                        logger.bind(sensitive=True).debug(
                            f"Direct connection from {peer_str} did not authenticate in time"
                        )
                        break
                    receive_timeout = min(receive_timeout, remaining_unauthenticated_time)
                try:
                    # The idle timeout applies to every socket. Before a sender
                    # verifies, the shorter remaining accept-time window also
                    # bounds handshakes and arbitrary traffic.
                    data = await asyncio.wait_for(connection.receive(), timeout=receive_timeout)
                    if not data:
                        logger.bind(sensitive=True).debug(
                            f"Direct connection from {peer_str} closed"
                        )
                        break

                    if not await _process_direct_message(
                        self,
                        connection,
                        data,
                        peer_str,
                        generation_id,
                        unauthenticated_deadline,
                    ):
                        break

                except TimeoutError:
                    logger.bind(sensitive=True).debug(
                        f"Direct connection from {peer_str} timed out waiting for a message"
                    )
                    break
                except NetworkConnectionError as e:
                    # Remote closed the TCP connection. This is routine for
                    # orderbook-watcher health checks and directory-handshake
                    # discovery probes, which connect, read the handshake
                    # response, and disconnect. Log at TRACE so real problems
                    # (parse errors, unexpected exceptions) still surface.
                    logger.bind(sensitive=True).trace(
                        f"Direct connection from {peer_str} closed by peer: {e}"
                    )
                    break
                except Exception as e:
                    logger.error("Error processing direct message")
                    logger.bind(sensitive=True).error(
                        f"Error processing direct message from {peer_str}: {e}"
                    )
                    break

        except Exception as e:
            logger.error("Error in direct connection handler")
            logger.bind(sensitive=True).error(
                f"Error in direct connection handler for {peer_str}: {e}"
            )
        finally:
            try:
                await connection.close()
            except Exception as e:
                logger.bind(sensitive=True).debug(
                    f"Failed to close direct connection from {peer_str}: {e}"
                )
            finally:
                # Always release the admission slot, including on cancellation
                # or a transport close failure.
                self._remove_direct_connection(connection, generation_id)
            logger.bind(sensitive=True).trace(f"Direct connection from {peer_str} closed")
