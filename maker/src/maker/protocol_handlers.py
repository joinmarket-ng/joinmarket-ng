"""
CoinJoin protocol message handlers for the maker bot.

Contains the central message dispatcher and handlers for all CoinJoin
protocol messages: fill, auth, tx, push, hp2, orderbook, etc.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from typing import TYPE_CHECKING, Any

from jmcore.commitment_blacklist import add_commitment, check_commitment
from jmcore.crypto import NickIdentity
from jmcore.deduplication import MessageDeduplicator
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer
from jmcore.notifications import get_notifier
from jmcore.protocol import COMMAND_PREFIX, JM_VERSION, MessageType
from jmcore.rate_limiter import RateLimitAction, RateLimiter
from jmcore.tasks import parse_directory_address
from jmwallet.backends.base import BlockchainBackend
from jmwallet.history import (
    append_history_entry,
    create_maker_history_entry,
    update_awaiting_transaction_signed,
)
from jmwallet.wallet.service import WalletService
from loguru import logger

from maker.coinjoin import CoinJoinSession
from maker.config import MakerConfig
from maker.fidelity import FidelityBondInfo, create_fidelity_bond_proof
from maker.offers import OfferManager
from maker.protocols import MakerBotProtocol
from maker.rate_limiting import DirectConnectionRateLimiter, OrderbookRateLimiter

if TYPE_CHECKING:
    from jmcore.network import TCPConnection


class ProtocolHandlersMixin:
    """Mixin class providing CoinJoin protocol handler methods for MakerBot.

    These methods handle the message dispatching and protocol state machine
    for CoinJoin transactions: fill -> auth -> tx -> push.
    """

    # -- Attributes provided by MakerBot --
    running: bool
    config: MakerConfig
    wallet: WalletService
    backend: BlockchainBackend
    nick: str
    current_offers: list[Offer]
    fidelity_bond: FidelityBondInfo | None
    current_block_height: int
    directory_clients: dict[str, DirectoryClient]
    active_sessions: dict[str, CoinJoinSession]
    offer_manager: OfferManager
    _message_deduplicator: MessageDeduplicator
    _message_rate_limiter: RateLimiter
    _orderbook_rate_limiter: OrderbookRateLimiter
    _direct_connection_rate_limiter: DirectConnectionRateLimiter
    _own_wallet_nicks: set[str]
    _hp2_broadcast_semaphore: asyncio.Semaphore

    async def _handle_message(
        self: MakerBotProtocol, message: dict[str, Any], source: str = "unknown"
    ) -> None:
        """
        Handle incoming message from directory or direct connection.

        Args:
            message: Message dict with 'type' and 'line' keys
            source: Message source for logging (e.g., "dir:node1", "direct:alice")
        """
        try:
            msg_type = message.get("type")
            line = message.get("line", "")

            # Extract from_nick for rate limiting (format: from_nick!to_nick!msg)
            parts = line.split(COMMAND_PREFIX)
            if len(parts) < 1:
                return

            from_nick = parts[0]

            # Create message fingerprint for deduplication
            # For private messages: use command (fill, auth, tx, etc.)
            # For public messages: use the whole message
            fingerprint: str | None = ""
            command = ""

            if msg_type == MessageType.PRIVMSG.value and len(parts) >= 3:
                # Format: from!to!command args...
                cmd_and_args = COMMAND_PREFIX.join(parts[2:])
                cmd_parts = cmd_and_args.strip().split(maxsplit=1)
                command = cmd_parts[0].lstrip("!")
                first_arg = cmd_parts[1].split()[0] if len(cmd_parts) > 1 else ""
                fingerprint = MessageDeduplicator.make_fingerprint(from_nick, command, first_arg)
            elif msg_type == MessageType.PUBMSG.value:
                # Parse the public message to check if it's !orderbook
                # Format: nick!PUBLIC!command or nick!PUBLIC!!command
                parts = line.split(COMMAND_PREFIX)
                # Check both parts[2] and parts[3] since format can be either:
                # nick!PUBLIC!orderbook or nick!PUBLIC!!orderbook
                is_orderbook_request = (
                    len(parts) >= 3
                    and parts[1] == "PUBLIC"
                    and (
                        parts[2].strip().lstrip("!") == "orderbook"
                        or (len(parts) >= 4 and parts[3].strip().lstrip("!") == "orderbook")
                    )
                )

                logger.debug(f"PUBMSG parts={parts}, is_orderbook={is_orderbook_request}")

                # Don't deduplicate !orderbook requests - they have their own rate limiting
                # and takers may legitimately request the orderbook multiple times
                if not is_orderbook_request:
                    # For other public messages, use the whole message as fingerprint
                    fingerprint = MessageDeduplicator.make_fingerprint(
                        from_nick, "pubmsg", line[len(from_nick) :]
                    )
                else:
                    fingerprint = None
                    logger.debug(f"Skipping deduplication for !orderbook from {from_nick}")

            # Check for duplicates (skip for !orderbook which has its own rate limiting)
            if fingerprint:
                is_dup, first_source, count = self._message_deduplicator.is_duplicate(
                    fingerprint, source
                )
                if is_dup:
                    # This is a duplicate - log and skip processing
                    # Only log first few duplicates to avoid spam
                    if count <= 3:
                        logger.debug(
                            f"Duplicate message #{count} from {from_nick} "
                            f"via {source} (first via {first_source}): {command or 'pubmsg'}"
                        )
                    return

            # Apply generic per-peer rate limiting (only for non-duplicates)
            action, _delay = self._message_rate_limiter.check(from_nick)

            if action != RateLimitAction.ALLOW:
                violations = self._message_rate_limiter.get_violation_count(from_nick)
                # Only log every 50th violation to prevent log flooding
                if violations % 50 == 0:
                    logger.warning(
                        f"Rate limit exceeded for {from_nick} ({violations} violations total)"
                    )
                return  # Drop the message

            # Process the message
            if msg_type == MessageType.PRIVMSG.value:
                await self._handle_privmsg(line, source=source)
            elif msg_type == MessageType.PUBMSG.value:
                await self._handle_pubmsg(line, source=source)
            elif msg_type == MessageType.PEERLIST.value:
                logger.debug(f"Received peerlist: {line[:50]}...")
            else:
                logger.debug(f"Ignoring message type {msg_type}")

        except Exception as e:
            logger.error(f"Failed to handle message: {e}")

    async def _handle_pubmsg(self: MakerBotProtocol, line: str, source: str = "unknown") -> None:
        """
        Handle public message (e.g., !orderbook request).

        Args:
            line: Message line in format "from_nick!to_nick!msg"
            source: Message source for logging (e.g., "dir:node1")
        """
        try:
            parts = line.split(COMMAND_PREFIX)
            if len(parts) < 3:
                return

            from_nick = parts[0]
            to_nick = parts[1]
            rest = COMMAND_PREFIX.join(parts[2:])

            # Ignore our own messages
            if from_nick == self.nick:
                return

            # Strip leading "!" and get command
            command = rest.strip().lstrip("!")

            # Respond to orderbook requests with PRIVMSG (including bond if available)
            if to_nick == "PUBLIC" and command == "orderbook":
                # Apply rate limiting to prevent spam attacks
                if not self._orderbook_rate_limiter.check(from_nick):
                    violations = self._orderbook_rate_limiter.get_violation_count(from_nick)
                    is_banned = self._orderbook_rate_limiter.is_banned(from_nick)

                    # Only log rate limiting (not bans) at specific violation milestones
                    # to prevent log flooding:
                    # - First violation (violations == 1)
                    # - Every 10th violation when not banned (10, 20, 30, etc.)
                    # Note: Ban events are already logged by check() method, so we skip
                    # logging here to avoid duplicate log messages
                    if not is_banned:
                        should_log = violations <= 1 or violations % 10 == 0

                        if should_log:
                            # Show backoff level for context
                            if violations >= self.config.orderbook_violation_severe_threshold:
                                backoff_level = "SEVERE"
                            elif violations >= self.config.orderbook_violation_warning_threshold:
                                backoff_level = "MODERATE"
                            else:
                                backoff_level = "NORMAL"

                            logger.debug(
                                f"Rate limiting orderbook request from {from_nick} "
                                f"(violations: {violations}, backoff: {backoff_level})"
                            )
                    return

                logger.info(
                    f"Received !orderbook request from {from_nick}, sending offers via PRIVMSG"
                )
                await self._send_offers_to_taker(from_nick)
            elif to_nick == "PUBLIC" and command.startswith("hp2"):
                # hp2 via pubmsg = commitment broadcast for blacklisting
                await self._handle_hp2_pubmsg(from_nick, command)

        except Exception as e:
            logger.error(f"Failed to handle pubmsg: {e}")

    async def _send_offers_to_taker(self, taker_nick: str) -> None:
        """Send offers to a specific taker via PRIVMSG, including fidelity bond if available.

        This is called when we receive a !orderbook request from a taker.
        According to the JoinMarket protocol, fidelity bonds must ONLY be sent
        via PRIVMSG, never in public broadcasts.

        For each offer:
        1. Format the offer parameters
        2. If we have a fidelity bond, create a proof signed for this specific taker
        3. Append !tbond <proof> to the offer data
        4. Send via PRIVMSG to the taker

        Message format:
            send_private_message(
                taker_nick,
                command="sw0reloffer",
                data="0 2500000 ... !tbond <proof>"
            )
            Results in: from_nick!taker_nick!sw0reloffer 0 2500000 ... !tbond <proof> <sig>

        Args:
            taker_nick: The nick of the taker requesting the orderbook
        """
        try:
            for offer in self.current_offers:
                # Format offer data (parameters without the command)
                order_type_str = offer.ordertype.value
                data = f"{offer.oid} {offer.minsize} {offer.maxsize} {offer.txfee} {offer.cjfee}"

                # Append fidelity bond proof if we have one
                # CRITICAL: The bond proof must be signed with the taker's nick
                if self.fidelity_bond is not None:
                    bond_proof = create_fidelity_bond_proof(
                        bond=self.fidelity_bond,
                        maker_nick=self.nick,
                        taker_nick=taker_nick,  # Sign for THIS specific taker
                        current_block_height=self.current_block_height,
                    )
                    if bond_proof:
                        data += f"!tbond {bond_proof}"
                        logger.debug(
                            f"Including fidelity bond proof in offer to {taker_nick} "
                            f"(proof length: {len(bond_proof)})"
                        )

                # Send via all connected directory clients
                for client in self.directory_clients.values():
                    try:
                        # Send as PRIVMSG
                        # Format: taker_nick!maker_nick!<order_type> <data> <signature>
                        await client.send_private_message(taker_nick, order_type_str, data)
                        logger.debug(f"Sent {order_type_str} offer to {taker_nick}")
                    except Exception as e:
                        logger.error(f"Failed to send offer to {taker_nick} via directory: {e}")

        except Exception as e:
            logger.error(f"Failed to send offers to taker {taker_nick}: {e}")

    async def _send_offers_via_direct_connection(
        self, taker_nick: str, connection: TCPConnection
    ) -> None:
        """Send offers to a taker via direct connection (not through directory).

        This is called when we receive a !orderbook request directly from a taker
        who connected to our onion hidden service. The response is sent back
        through the same direct connection.

        The message format follows the reference implementation:
            {"type": 685, "line": "maker_nick!taker_nick!order_type data"}

        Args:
            taker_nick: The nick of the taker requesting the orderbook
            connection: The direct TCP connection to send the response on
        """
        try:
            for offer in self.current_offers:
                # Format offer data (parameters without the command)
                order_type_str = offer.ordertype.value
                data = f"{offer.oid} {offer.minsize} {offer.maxsize} {offer.txfee} {offer.cjfee}"

                # Append fidelity bond proof if we have one
                if self.fidelity_bond is not None:
                    bond_proof = create_fidelity_bond_proof(
                        bond=self.fidelity_bond,
                        maker_nick=self.nick,
                        taker_nick=taker_nick,
                        current_block_height=self.current_block_height,
                    )
                    if bond_proof:
                        data += f"!tbond {bond_proof}"
                        logger.debug(
                            f"Including fidelity bond proof in direct offer to {taker_nick}"
                        )

                # Format: maker_nick!taker_nick!order_type data
                # Note: The reference implementation uses COMMAND_PREFIX (!) as separator
                line = (
                    f"{self.nick}{COMMAND_PREFIX}{taker_nick}{COMMAND_PREFIX}{order_type_str}{data}"
                )

                # Send as PRIVMSG (type 685)
                msg = {"type": MessageType.PRIVMSG.value, "line": line}
                await connection.send(json.dumps(msg).encode())
                logger.debug(f"Sent {order_type_str} offer to {taker_nick} via direct connection")

        except Exception as e:
            logger.error(f"Failed to send offers to {taker_nick} via direct connection: {e}")

    async def _handle_privmsg(self: MakerBotProtocol, line: str, source: str = "unknown") -> None:
        """
        Handle private message (CoinJoin protocol).

        Args:
            line: Message line in format "from_nick!to_nick!msg"
            source: Message source for logging (e.g., "dir:node1", "direct:alice")
        """
        try:
            parts = line.split(COMMAND_PREFIX)
            if len(parts) < 3:
                return

            from_nick = parts[0]
            to_nick = parts[1]
            rest = COMMAND_PREFIX.join(parts[2:])

            if to_nick != self.nick:
                return

            # Strip leading "!" if present (due to !!command message format)
            command = rest.strip().lstrip("!")

            # Note: command prefix already stripped
            if command.startswith("fill"):
                await self._handle_fill(from_nick, command, source=source)
            elif command.startswith("auth"):
                await self._handle_auth(from_nick, command, source=source)
            elif command.startswith("tx"):
                await self._handle_tx(from_nick, command, source=source)
            elif command.startswith("push"):
                await self._handle_push(from_nick, command, source=source)
            elif command.startswith("hp2"):
                # hp2 via privmsg = commitment transfer request
                # We should re-broadcast it publicly to obfuscate the source
                await self._handle_hp2_privmsg(from_nick, command)
            else:
                logger.debug(f"Unknown command: {command[:20]}...")

        except Exception as e:
            logger.error(f"Failed to handle privmsg: {e}")

    async def _handle_fill(self, taker_nick: str, msg: str, source: str = "unknown") -> None:
        """Handle !fill request from taker.

        Fill message format: fill <oid> <amount> <taker_nacl_pk> <commitment> [<signing_pk> <sig>]

        The offer_id (oid) is used to identify which offer the taker wants to fill.
        This allows makers to have multiple offers (e.g., relative and absolute fee)
        simultaneously, each with a unique ID.
        """
        try:
            # Check for self-CoinJoin (same wallet running both maker and taker)
            if taker_nick in self._own_wallet_nicks:
                logger.warning(
                    f"Rejecting !fill from {taker_nick}: self-CoinJoin protection "
                    "(same wallet running both maker and taker)"
                )
                return

            parts = msg.split()
            if len(parts) < 5:
                logger.warning(f"Invalid !fill format (need at least 5 parts): {msg}")
                return

            offer_id = int(parts[1])
            amount = int(parts[2])
            taker_pk = parts[3]  # Taker's NaCl pubkey for E2E encryption
            commitment = parts[4]  # PoDLE commitment (with prefix like "P")

            # Strip commitment prefix if present (e.g., "P" for standard PoDLE)
            if commitment.startswith("P"):
                commitment = commitment[1:]

            # Check if commitment is already blacklisted
            if not check_commitment(commitment):
                logger.warning(
                    f"Rejecting !fill from {taker_nick}: commitment already used "
                    f"({commitment[:16]}...)"
                )
                return

            # Find the offer by ID (supports multiple offers with different IDs)
            offer = self.offer_manager.get_offer_by_id(self.current_offers, offer_id)
            if offer is None:
                logger.warning(
                    f"Invalid offer ID: {offer_id} (available: "
                    f"{[o.oid for o in self.current_offers]})"
                )
                return

            is_valid, error = self.offer_manager.validate_offer_fill(offer, amount)
            if not is_valid:
                logger.warning(f"Invalid fill request for offer {offer_id}: {error}")
                return

            session = CoinJoinSession(
                taker_nick=taker_nick,
                offer=offer,
                wallet=self.wallet,
                backend=self.backend,
                session_timeout_sec=self.config.session_timeout_sec,
                merge_algorithm=self.config.merge_algorithm.value,
                restrict_md0=not self.config.allow_mixdepth_zero_merge,
            )

            # Validate channel consistency (first message records the channel)
            if not session.validate_channel(source):
                logger.error(f"Channel consistency violation for !fill from {taker_nick}")
                return

            # Pass the taker's NaCl pubkey for setting up encryption
            success, response = await session.handle_fill(amount, commitment, taker_pk)

            if success:
                self.active_sessions[taker_nick] = session
                logger.info(
                    f"Created CoinJoin session with {taker_nick} "
                    f"(offer_id={offer_id}, type={offer.ordertype.value})"
                )

                # Fire-and-forget notification
                asyncio.create_task(
                    get_notifier().notify_fill_request(taker_nick, amount, offer_id)
                )

                await self._send_response(taker_nick, "pubkey", response)
            else:
                logger.warning(f"Failed to handle fill: {response.get('error')}")

        except Exception as e:
            logger.error(f"Failed to handle !fill: {e}")

    async def _handle_auth(
        self: MakerBotProtocol, taker_nick: str, msg: str, source: str = "unknown"
    ) -> None:
        """Handle !auth request from taker.

        The auth message is ENCRYPTED using NaCl.
        Format: auth <encrypted_base64> [<signing_pk> <sig>]

        After decryption, the plaintext is pipe-separated:
        txid:vout|P|P2|sig|e

        Note: The taker sends !auth via all directory servers, so we may receive
        duplicates. We use a lock per session to ensure only one message is
        processed at a time, and check state early to reject duplicates.
        """
        # Acquire lock for this session to prevent concurrent processing
        lock = self._get_session_lock(taker_nick)
        async with lock:
            try:
                if taker_nick not in self.active_sessions:
                    logger.warning(f"No active session for {taker_nick}")
                    return

                session = self.active_sessions[taker_nick]

                # Validate channel consistency before processing
                if not session.validate_channel(source):
                    logger.error(f"Channel consistency violation for !auth from {taker_nick}")
                    del self.active_sessions[taker_nick]
                    self._cleanup_session_lock(taker_nick)
                    return

                # Early state check to reject duplicate !auth messages
                # This happens when taker sends via multiple directory servers
                from maker.coinjoin import CoinJoinState

                if session.state != CoinJoinState.PUBKEY_SENT:
                    logger.debug(
                        f"Ignoring duplicate !auth from {taker_nick} "
                        f"(state={session.state}, expected=PUBKEY_SENT)"
                    )
                    return

                logger.info(f"Received !auth from {taker_nick}, decrypting and verifying PoDLE...")

                # Parse: auth <encrypted_base64> [<signing_pk> <sig>]
                parts = msg.split()
                if len(parts) < 2:
                    logger.error("Invalid !auth format: missing encrypted data")
                    return

                encrypted_data = parts[1]

                # Decrypt the auth message
                if not session.crypto.is_encrypted:
                    logger.error("Encryption not set up for this session")
                    return

                try:
                    decrypted = session.crypto.decrypt(encrypted_data)
                    logger.debug(f"Decrypted auth message length: {len(decrypted)}")
                except Exception as e:
                    logger.error(f"Failed to decrypt auth message: {e}")
                    return

                # Parse the decrypted revelation - pipe-separated format:
                # txid:vout|P|P2|sig|e
                try:
                    revelation_parts = decrypted.split("|")
                    if len(revelation_parts) != 5:
                        logger.error(
                            f"Invalid revelation format: expected 5 parts, "
                            f"got {len(revelation_parts)}"
                        )
                        return

                    utxo_str, p_hex, p2_hex, sig_hex, e_hex = revelation_parts

                    # Parse utxo
                    if ":" not in utxo_str:
                        logger.error(f"Invalid utxo format: {utxo_str}")
                        return

                    # Validate utxo format (txid:vout)
                    if not utxo_str.rsplit(":", 1)[1].isdigit():
                        logger.error(f"Invalid vout in utxo: {utxo_str}")
                        return

                    # parse_podle_revelation expects hex strings, not bytes
                    revelation = {
                        "utxo": utxo_str,
                        "P": p_hex,
                        "P2": p2_hex,
                        "sig": sig_hex,
                        "e": e_hex,
                    }
                    logger.debug(f"Parsed revelation: utxo={utxo_str}, P={p_hex[:16]}...")
                except Exception as e:
                    logger.error(f"Failed to parse revelation: {e}")
                    return

                # The commitment was already stored from the !fill message
                commitment = self.active_sessions[taker_nick].commitment.hex()

                # kphex is empty for now - we don't use it yet
                kphex = ""

                success, response = await session.handle_auth(commitment, revelation, kphex)

                if success:
                    # CRITICAL: Record addresses to history BEFORE revealing them to taker
                    # This ensures addresses are never reused even if:
                    # - The taker disappears after receiving !ioauth
                    # - The program crashes after sending !ioauth
                    # - The taker sends invalid !tx and we reject it
                    try:
                        our_utxos = list(session.our_utxos.keys())
                        # Use 0 for fees since we haven't signed yet - will be updated
                        # when transaction is actually signed
                        history_entry = create_maker_history_entry(
                            taker_nick=taker_nick,
                            cj_amount=session.amount,
                            fee_received=0,  # Unknown until tx is signed
                            txfee_contribution=0,  # Unknown until tx is signed
                            cj_address=session.cj_address,
                            change_address=session.change_address,
                            our_utxos=our_utxos,
                            txid=None,  # Unknown until tx is signed
                            network=self.config.network.value,
                        )
                        # Override failure_reason to indicate addresses revealed but awaiting tx
                        history_entry.failure_reason = "Awaiting transaction"
                        append_history_entry(history_entry, data_dir=self.config.data_dir)
                        logger.debug(
                            f"Recorded revealed addresses for {taker_nick} in history "
                            f"(cj={session.cj_address[:12]}..., "
                            f"change={session.change_address[:12]}...)"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to record revealed addresses in history: {e}")
                        # Continue anyway - better to reveal addresses than fail the CJ
                        # The addresses should still be tracked via blockchain sync

                    await self._send_response(taker_nick, "ioauth", response)

                    # Broadcast the commitment via hp2 so other makers can blacklist it
                    # This prevents reuse of commitments in future CoinJoin attempts
                    await self._broadcast_commitment(commitment)
                else:
                    error_msg = response.get("error", "unknown error")
                    error_code = response.get("error_code", "")
                    logger.error(f"Auth failed: {error_msg}")

                    # Send !error to taker so they can handle it immediately
                    # rather than waiting for the ioauth timeout. The reference
                    # implementation logs the error; our taker can act on it.
                    try:
                        for client in self.directory_clients.values():
                            await client.send_private_message(taker_nick, "error", error_msg)
                        logger.debug(f"Sent !error to {taker_nick}: {error_msg}")
                    except Exception as e:
                        logger.warning(f"Failed to send !error to {taker_nick}: {e}")

                    # Fire-and-forget notification for rejection
                    asyncio.create_task(
                        get_notifier().notify_rejection(
                            taker_nick,
                            error_code or "PoDLE verification failed",
                            error_msg,
                        )
                    )
                    del self.active_sessions[taker_nick]
                    self._cleanup_session_lock(taker_nick)

            except Exception as e:
                logger.error(f"Failed to handle !auth: {e}")

    async def _handle_tx(
        self: MakerBotProtocol, taker_nick: str, msg: str, source: str = "unknown"
    ) -> None:
        """Handle !tx request from taker.

        The tx message is ENCRYPTED using NaCl.
        Format: tx <encrypted_base64> [<signing_pk> <sig>]

        After decryption, the plaintext is base64-encoded transaction bytes.

        Note: The taker sends !tx via all directory servers, so we may receive
        duplicates. We use a lock per session to ensure only one message is
        processed at a time, and check state early to reject duplicates.
        """
        # Acquire lock for this session to prevent concurrent processing
        lock = self._get_session_lock(taker_nick)
        async with lock:
            try:
                if taker_nick not in self.active_sessions:
                    logger.warning(f"No active session for {taker_nick}")
                    return

                session = self.active_sessions[taker_nick]

                # Validate channel consistency before processing
                if not session.validate_channel(source):
                    logger.error(f"Channel consistency violation for !tx from {taker_nick}")
                    del self.active_sessions[taker_nick]
                    self._cleanup_session_lock(taker_nick)
                    return

                # Early state check to reject duplicate !tx messages
                # This happens when taker sends via multiple directory servers
                from maker.coinjoin import CoinJoinState

                if session.state != CoinJoinState.IOAUTH_SENT:
                    logger.debug(
                        f"Ignoring duplicate !tx from {taker_nick} "
                        f"(state={session.state}, expected=IOAUTH_SENT)"
                    )
                    return

                logger.info(
                    f"Received !tx from {taker_nick}, decrypting and verifying transaction..."
                )

                # Parse: tx <encrypted_base64> [<signing_pk> <sig>]
                parts = msg.split()
                if len(parts) < 2:
                    logger.warning("Invalid !tx format")
                    return

                encrypted_data = parts[1]

                # Decrypt the tx message
                if not session.crypto.is_encrypted:
                    logger.error("Encryption not set up for this session")
                    return

                try:
                    decrypted = session.crypto.decrypt(encrypted_data)
                    logger.debug(f"Decrypted tx message length: {len(decrypted)}")
                except Exception as e:
                    logger.error(f"Failed to decrypt tx message: {e}")
                    return

                # The decrypted content is base64-encoded transaction
                try:
                    tx_bytes = base64.b64decode(decrypted)
                    tx_hex = tx_bytes.hex()
                except Exception as e:
                    logger.error(f"Failed to decode transaction: {e}")
                    return

                success, response = await session.handle_tx(tx_hex)

                if success:
                    # Send each signature as a separate message
                    signatures = response.get("signatures", [])
                    for sig in signatures:
                        await self._send_response(taker_nick, "sig", {"signature": sig})
                    logger.info(
                        f"CoinJoin with {taker_nick} COMPLETE (sent {len(signatures)} sigs)"
                    )

                    # Calculate fee for history and notification
                    fee_received = session.offer.calculate_fee(session.amount)
                    txfee_contribution = session.offer.txfee

                    # Update the history entry that was created during !ioauth
                    # (when addresses were revealed) with the tx details
                    try:
                        txid = response.get("txid", "")
                        updated = update_awaiting_transaction_signed(
                            destination_address=session.cj_address,
                            txid=txid,
                            fee_received=fee_received,
                            txfee_contribution=txfee_contribution,
                            data_dir=self.config.data_dir,
                        )
                        net = fee_received - txfee_contribution
                        if updated:
                            logger.debug(f"Updated CoinJoin history with txid: net fee {net} sats")
                        else:
                            # Fallback: create a new entry if no "Awaiting transaction" entry found
                            # This can happen if history was cleared or entry was lost
                            logger.warning(
                                "No 'Awaiting transaction' entry found, creating new history entry"
                            )
                            our_utxos = list(session.our_utxos.keys())
                            history_entry = create_maker_history_entry(
                                taker_nick=taker_nick,
                                cj_amount=session.amount,
                                fee_received=fee_received,
                                txfee_contribution=txfee_contribution,
                                cj_address=session.cj_address,
                                change_address=session.change_address,
                                our_utxos=our_utxos,
                                txid=txid,
                                network=self.config.network.value,
                            )
                            append_history_entry(history_entry, data_dir=self.config.data_dir)
                            logger.debug(f"Created new CoinJoin history: net fee {net} sats")
                    except Exception as e:
                        logger.warning(f"Failed to update CoinJoin history: {e}")

                    # Fire-and-forget notification for successful signing
                    asyncio.create_task(
                        get_notifier().notify_tx_signed(
                            taker_nick,
                            session.amount,
                            len(signatures),
                            fee_received,
                        )
                    )

                    del self.active_sessions[taker_nick]
                    self._cleanup_session_lock(taker_nick)

                    # Nick regeneration disabled - see _regenerate_nick() docstring for rationale

                    # Schedule wallet re-sync in background to avoid blocking !push handling
                    asyncio.create_task(self._deferred_wallet_resync())
                else:
                    logger.error(f"TX verification failed: {response.get('error')}")
                    # Fire-and-forget notification for TX rejection
                    asyncio.create_task(
                        get_notifier().notify_rejection(
                            taker_nick, "TX verification failed", response.get("error", "")
                        )
                    )
                    del self.active_sessions[taker_nick]
                    self._cleanup_session_lock(taker_nick)

            except Exception as e:
                logger.error(f"Failed to handle !tx: {e}")

    async def _handle_push(self, taker_nick: str, msg: str, source: str = "unknown") -> None:
        """Handle !push request from taker.

        The push message contains a base64-encoded signed transaction that the taker
        wants us to broadcast. This provides privacy benefits as the taker's IP is
        not linked to the transaction broadcast.

        Per JoinMarket protocol, makers broadcast "unquestioningly" - we already
        signed this transaction so it must be valid from our perspective. We don't
        verify or check the result, just broadcast and move on.

        Security considerations:
        - DoS risk: A malicious taker could spam !push messages with invalid data
        - Mitigation: Generic per-peer rate limiting (in directory server) prevents
          this from being a significant attack vector
        - We intentionally do NOT validate session state here to maintain protocol
          compatibility and simplicity. The rate limiter is the primary defense.

        Format: push <base64_transaction>

        Note: !push doesn't require channel consistency validation since it's
        fire-and-forget and not part of the critical CoinJoin handshake.
        """
        try:
            parts = msg.split()
            if len(parts) < 2:
                logger.warning(f"Invalid !push format from {taker_nick}")
                return

            tx_b64 = parts[1]

            try:
                tx_bytes = base64.b64decode(tx_b64)
                tx_hex = tx_bytes.hex()
            except Exception as e:
                logger.error(f"Failed to decode !push transaction: {e}")
                return

            logger.info(f"Received !push from {taker_nick}, broadcasting transaction...")

            # Broadcast "unquestioningly" - we already signed it, so it's valid
            # from our perspective. Don't check the result.
            try:
                txid = await self.backend.broadcast_transaction(tx_hex)
                logger.info(f"Broadcast transaction for {taker_nick}: {txid}")
            except Exception as e:
                # Log but don't fail - the taker may have a fallback
                logger.warning(f"Failed to broadcast !push transaction: {e}")

        except Exception as e:
            logger.error(f"Failed to handle !push: {e}")

    async def _handle_hp2_pubmsg(self, from_nick: str, msg: str) -> None:
        """Handle !hp2 commitment broadcast seen in public channel.

        When a maker sees a PoDLE commitment broadcast in public (via !hp2),
        they should blacklist it. This prevents reuse of commitments that
        may have been used in failed or malicious CoinJoin attempts.

        There is no way to spoof commitments, so the only risk of accepting
        them is disk usage from a growing blacklist file.

        Format: hp2 <commitment_hex>
        """
        try:
            parts = msg.split()
            if len(parts) < 2:
                logger.debug(f"Invalid !hp2 format from {from_nick}: missing commitment")
                return

            commitment = parts[1]

            # Add to blacklist (persists to disk)
            if add_commitment(commitment):
                logger.info(
                    f"Received commitment broadcast from {from_nick}, "
                    f"added to blacklist: {commitment[:16]}..."
                )
            else:
                logger.debug(
                    f"Received commitment broadcast from {from_nick}, "
                    f"already blacklisted: {commitment[:16]}..."
                )

        except Exception as e:
            logger.error(f"Failed to handle !hp2 pubmsg: {e}")

    async def _handle_hp2_privmsg(self, from_nick: str, msg: str) -> None:
        """Handle !hp2 commitment relay request via private message.

        When a maker receives !hp2 via privmsg, another maker is asking us to
        broadcast the commitment publicly on their behalf. Rather than
        re-broadcasting on our own (long-lived, identifiable) connection, we
        open ephemeral connections to all directory servers with a fresh random
        nick and unique Tor circuit, then broadcast there. This way neither the
        requesting maker nor we ourselves are linked to the public broadcast.

        The commitment is also added to our own blacklist.

        Format: hp2 <commitment_hex>
        """
        try:
            parts = msg.split()
            if len(parts) < 2:
                logger.debug(f"Invalid !hp2 format from {from_nick}: missing commitment")
                return

            commitment = parts[1]
            logger.info(f"Received commitment relay request from {from_nick}: {commitment[:16]}...")

            # Blacklist locally
            add_commitment(commitment)

            # Broadcast via ephemeral identity (fire-and-forget)
            asyncio.create_task(self._broadcast_commitment_ephemeral(commitment))

        except Exception as e:
            logger.error(f"Failed to handle !hp2 relay request: {e}")

    async def _broadcast_commitment(self, commitment: str) -> None:
        """Broadcast a PoDLE commitment via !hp2 to help other makers blacklist it.

        After successfully processing a taker's !auth message, we broadcast the
        commitment so other makers can add it to their blacklist. This prevents
        the same commitment from being reused in future CoinJoin attempts.

        **Privacy design (ephemeral identity broadcast):**

        To prevent an observer from correlating the !hp2 broadcast with the
        maker that just participated in a CoinJoin, we broadcast the commitment
        from a fresh ephemeral identity on a separate Tor circuit:

        1. Add the commitment to our own blacklist (immediate, persisted to disk)
        2. Open new connections to all directory servers with a random nick and
           unique SOCKS5 credentials (forcing a fresh Tor circuit via stream
           isolation)
        3. Broadcast ``hp2 <commitment>`` as pubmsg on each connection
        4. Close all ephemeral connections

        This is strictly better than the reference implementation's relay
        approach (sending via privmsg to a random peer who re-broadcasts),
        because it does not trust any peer to actually relay the message.
        A malicious peer could simply drop the relay request; with direct
        ephemeral broadcast, the commitment always reaches the network.

        The broadcast is best-effort and fire-and-forget: connection failures
        are logged but do not affect the CoinJoin flow.
        """
        try:
            # Add to our own blacklist first (persists to disk)
            add_commitment(commitment)

            # Broadcast via ephemeral identity (fire-and-forget)
            asyncio.create_task(self._broadcast_commitment_ephemeral(commitment))

            logger.debug(f"Scheduled ephemeral commitment broadcast: {commitment[:16]}...")

        except Exception as e:
            logger.error(f"Failed to broadcast commitment: {e}")

    async def _broadcast_commitment_ephemeral(self, commitment: str) -> None:
        """Open ephemeral directory connections and broadcast a commitment.

        Creates short-lived connections to all configured directory servers
        using a fresh random nick identity and unique Tor stream isolation
        credentials, broadcasts the commitment as a public !hp2 message, then
        tears down the connections.

        Guarded by ``_hp2_broadcast_semaphore`` (max 2 concurrent) to prevent
        a Sybil DoS where many nicks each send one relay request, causing us
        to open excessive Tor circuits. Requests that exceed the concurrency
        limit are silently dropped -- the commitment is already blacklisted
        locally by the caller.

        This is a background task -- errors are logged, not raised.
        """
        acquired = self._hp2_broadcast_semaphore.locked() is False
        if not acquired:
            # All slots may be taken; try non-blocking acquire
            try:
                # Semaphore.acquire() with wait=False isn't available, so use
                # a zero-timeout wait to avoid blocking.
                await asyncio.wait_for(self._hp2_broadcast_semaphore.acquire(), timeout=0)
                acquired = True
            except TimeoutError:
                logger.debug(
                    f"Dropping ephemeral hp2 broadcast (concurrency limit): {commitment[:16]}..."
                )
                return
        else:
            await self._hp2_broadcast_semaphore.acquire()

        hp2_msg = f"hp2 {commitment}"
        ephemeral_clients: list[DirectoryClient] = []

        try:
            nick_identity = NickIdentity(JM_VERSION)

            # Generate unique SOCKS5 credentials to force a fresh Tor circuit.
            # Using a random password ensures this connection is isolated from
            # all other connections in this process (including the maker's
            # persistent directory connections).
            socks_username = "jm-hp2-broadcast"
            socks_password = os.urandom(16).hex()

            for dir_server in self.config.directory_servers:
                try:
                    host, port = parse_directory_address(dir_server)
                    client = DirectoryClient(
                        host=host,
                        port=port,
                        network=self.config.network.value,
                        nick_identity=nick_identity,
                        socks_host=self.config.socks_host,
                        socks_port=self.config.socks_port,
                        timeout=30.0,
                        socks_username=socks_username,
                        socks_password=socks_password,
                    )
                    await client.connect()
                    ephemeral_clients.append(client)
                except Exception as e:
                    logger.debug(f"Ephemeral hp2 connection to {dir_server} failed: {e}")

            if not ephemeral_clients:
                logger.warning("Could not connect to any directory for ephemeral hp2 broadcast")
                return

            for client in ephemeral_clients:
                try:
                    await client.send_public_message(hp2_msg)
                except Exception as e:
                    logger.debug(f"Ephemeral hp2 broadcast failed on one directory: {e}")

            logger.debug(
                f"Ephemeral hp2 broadcast complete on "
                f"{len(ephemeral_clients)} directories: {commitment[:16]}..."
            )

        except Exception as e:
            logger.error(f"Ephemeral commitment broadcast failed: {e}")

        finally:
            self._hp2_broadcast_semaphore.release()
            for client in ephemeral_clients:
                try:
                    await client.close()
                except Exception:
                    pass

    async def _send_response(self, taker_nick: str, command: str, data: dict[str, Any]) -> None:
        """Send signed response to taker.

        Different commands have different formats:
        - !pubkey <nacl_pubkey_hex> - NOT encrypted
        - !ioauth <encrypted_base64> - ENCRYPTED
        - !sig <encrypted_base64> - ENCRYPTED

        The signature is appended: <message_content> <signing_pubkey> <sig_b64>
        The signature is over: <message_content> + hostid (NOT including the command!)

        For encrypted commands, the plaintext is space-separated values that get
        encrypted and base64-encoded before signing.
        """
        try:
            # Format message content based on command type
            if command == "pubkey":
                # !pubkey <nacl_pubkey_hex> [features=<comma-separated>] - NOT encrypted
                # Features are optional and backwards compatible with legacy takers
                msg_content = data["nacl_pubkey"]
                features = data.get("features", [])
                if features:
                    msg_content += f" features={','.join(features)}"
            elif command == "ioauth":
                # Plaintext format: <utxo_list> <auth_pub> <cj_addr> <change_addr> <btc_sig>
                plaintext = " ".join(
                    [
                        data["utxo_list"],
                        data["auth_pub"],
                        data["cj_addr"],
                        data["change_addr"],
                        data["btc_sig"],
                    ]
                )

                # Get the session to encrypt the message
                if taker_nick not in self.active_sessions:
                    logger.error(f"No active session for {taker_nick} to encrypt ioauth")
                    return
                session = self.active_sessions[taker_nick]
                msg_content = session.crypto.encrypt(plaintext)
                logger.debug(f"Encrypted ioauth message, plaintext_len={len(plaintext)}")
            elif command == "sig":
                # Plaintext format: <signature_base64>
                # For multiple signatures, we send them one by one
                plaintext = data["signature"]

                # Get the session to encrypt the message
                if taker_nick not in self.active_sessions:
                    logger.error(f"No active session for {taker_nick} to encrypt sig")
                    return
                session = self.active_sessions[taker_nick]
                msg_content = session.crypto.encrypt(plaintext)
                logger.debug(f"Encrypted sig: plaintext_len={len(plaintext)}")
            else:
                # Fallback to JSON for unknown commands
                msg_content = json.dumps(data)

            # Send via directory clients - they will sign the message for us
            for client in self.directory_clients.values():
                await client.send_private_message(taker_nick, command, msg_content)

            logger.debug(f"Sent signed {command} to {taker_nick}")

        except Exception as e:
            logger.error(f"Failed to send response: {e}")
