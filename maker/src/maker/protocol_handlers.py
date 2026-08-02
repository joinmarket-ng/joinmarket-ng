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
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from jmcore.bitcoin import get_txid
from jmcore.commitment_blacklist import add_commitment, check_commitment, validate_commitment_hex
from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.deduplication import MessageDeduplicator
from jmcore.directory_client import DirectoryClient
from jmcore.models import Offer
from jmcore.network import ONION_HOSTID
from jmcore.notifications import get_notifier
from jmcore.protocol import COMMAND_PREFIX, JM_VERSION, MessageType
from jmcore.rate_limiter import RateLimitAction, RateLimiter
from jmcore.tasks import parse_directory_address, spawn_task
from jmwallet.backends.base import BlockchainBackend
from jmwallet.wallet.service import WalletService
from loguru import logger

from maker.coinjoin import CoinJoinSession, CoinJoinState
from maker.config import MakerConfig
from maker.fidelity import FidelityBondInfo, create_fidelity_bond_proof
from maker.maker_session import MakerSession, PendingSignedRound
from maker.offers import OfferManager
from maker.protocols import MakerBotProtocol
from maker.rate_limiting import DirectConnectionRateLimiter, OrderbookRateLimiter

if TYPE_CHECKING:
    from jmcore.network import TCPConnection


_HANDLER_CANCEL_GRACE_SEC = 0.5
# Admission is capped before task creation, which also bounds the
# cancellation-suppressing subset retained after reaper detachment.
MAX_SESSION_HANDLER_TASKS = 128
MAX_PENDING_SIGNED_ROUNDS = 1024


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
    nick_identity: NickIdentity
    current_offers: list[Offer]
    fidelity_bond: FidelityBondInfo | None
    current_block_height: int
    directory_clients: dict[str, DirectoryClient]
    active_sessions: dict[str, MakerSession]
    offer_manager: OfferManager
    _message_deduplicator: MessageDeduplicator
    _message_rate_limiter: RateLimiter
    _orderbook_rate_limiter: OrderbookRateLimiter
    _direct_connection_rate_limiter: DirectConnectionRateLimiter
    _own_wallet_nicks: set[str]
    _hp2_own_broadcast_semaphore: asyncio.Semaphore
    _hp2_relay_broadcast_semaphore: asyncio.Semaphore

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
            authenticated_data = ""

            if msg_type == MessageType.PRIVMSG.value:
                route = line.split(COMMAND_PREFIX, 2)
                if len(route) != 3 or route[1] != self.nick:
                    return
                authenticated, _command, authenticated_data = verify_signed_privmsg(
                    route[0], route[2].strip().lstrip("!"), ONION_HOSTID
                )
                if not authenticated:
                    logger.warning(
                        f"Dropping unauthenticated private message from {route[0]} via {source}"
                    )
                    return

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
                if command == "fill":
                    fill_args = authenticated_data.split()
                    if len(fill_args) >= 4:
                        first_arg = f"{fill_args[0]}:{fill_args[3].lower()}"
                fingerprint = MessageDeduplicator.make_fingerprint(from_nick, command, first_arg)
            elif msg_type == MessageType.PUBMSG.value:
                # Parse the public message to check if it's an orderbook request
                # Wire format: nick!PUBLIC!orderbook (COMMAND_PREFIX is the field separator)
                parts = line.split(COMMAND_PREFIX)
                # Rejoin parts after nick and target to get the command, then strip
                # any leading "!" for robustness (handles legacy double-bang format)
                rest = COMMAND_PREFIX.join(parts[2:]) if len(parts) >= 3 else ""
                is_orderbook_request = (
                    len(parts) >= 3
                    and parts[1] == "PUBLIC"
                    and rest.strip().lstrip("!") == "orderbook"
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

                # Direct private messages use the same signed envelope as
                # directory-relayed private messages.
                signed_data = self.nick_identity.sign_message(data, ONION_HOSTID)
                line = (
                    f"{self.nick}{COMMAND_PREFIX}{taker_nick}{COMMAND_PREFIX}"
                    f"{order_type_str} {signed_data}"
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

    async def _handle_fill(
        self: MakerBotProtocol, taker_nick: str, msg: str, source: str = "unknown"
    ) -> None:
        """Handle !fill request from taker.

        Fill message format: fill <oid> <amount> <taker_nacl_pk> <commitment> [<signing_pk> <sig>]

        The offer_id (oid) is used to identify which offer the taker wants to fill.
        This allows makers to have multiple offers (e.g., relative and absolute fee)
        simultaneously, each with a unique ID.
        """
        reservation_owned = False
        session: MakerSession | None = None
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
            wire_commitment = parts[4]
            if not wire_commitment.startswith("P"):
                logger.warning(f"Rejecting !fill from {taker_nick}: unsupported commitment type")
                return
            commitment = wire_commitment[1:]

            # Validate commitment format before any further processing.
            # Must be exactly 64 hex characters (32-byte SHA256 hash).
            valid, error = validate_commitment_hex(commitment)
            if not valid:
                logger.warning(
                    f"Rejecting !fill from {taker_nick}: {error} (raw={commitment[:32]!r})"
                )
                return
            commitment = commitment.lower()

            # Check if commitment is already blacklisted
            if not check_commitment(commitment):
                logger.warning(
                    f"Rejecting !fill from {taker_nick}: commitment already used "
                    f"({commitment[:16]}...)"
                )
                return

            # Reserve synchronously before the first await. Network !hp2
            # broadcasts may blacklist this commitment while the same
            # multi-maker round is in progress, but a second local session must
            # never be allowed to authenticate with it concurrently.
            if commitment in self._reserved_commitments:
                logger.warning(
                    f"Rejecting !fill from {taker_nick}: commitment already in use "
                    f"({commitment[:16]}...)"
                )
                return
            self._reserved_commitments.add(commitment)
            reservation_owned = True

            # Find the offer by ID (supports multiple offers with different IDs)
            offer = self.offer_manager.get_offer_by_id(self.current_offers, offer_id)
            if offer is None:
                self._release_commitment_reservation(commitment)
                reservation_owned = False
                logger.warning(
                    f"Invalid offer ID: {offer_id} (available: "
                    f"{[o.oid for o in self.current_offers]})"
                )
                return

            is_valid, error = self.offer_manager.validate_offer_fill(offer, amount)
            if not is_valid:
                self._release_commitment_reservation(commitment)
                reservation_owned = False
                logger.warning(f"Invalid fill request for offer {offer_id}: {error}")
                return

            session_inner = CoinJoinSession(
                taker_nick=taker_nick,
                offer=offer,
                wallet=self.wallet,
                backend=self.backend,
                min_confirmations=self.config.min_confirmations,
                session_timeout_sec=self.config.session_timeout_sec,
                input_lock_ttl_sec=self.config.pending_tx_timeout_min * 60,
                merge_algorithm=self.config.merge_algorithm.value,
                restrict_md0=not self.config.allow_mixdepth_zero_merge,
            )
            session = MakerSession(inner=session_inner)

            # Record the channel this !fill arrived on (always accepted; a
            # taker may switch direct<->directory mid-session, see
            # CoinJoinSession.validate_channel).
            session.validate_channel(source)

            # Pass the taker's NaCl pubkey for setting up encryption
            success, response = await session.handle_fill(amount, commitment, taker_pk)

            if success:
                previous_session = self.active_sessions.get(taker_nick)
                if previous_session is not None:
                    # Takers may rotate a rejected commitment and retry the
                    # fill phase under the same nick. Preserve that behavior
                    # only before auth starts; replacing a running or advanced
                    # session could race its commitment and input cleanup.
                    if (
                        previous_session.lock.locked()
                        or previous_session.state != CoinJoinState.PUBKEY_SENT
                    ):
                        self._release_commitment_reservation(commitment)
                        reservation_owned = False
                        logger.warning(
                            f"Rejecting replacement !fill from {taker_nick}: "
                            "session authentication already started"
                        )
                        return
                    previous_session.release_input_locks()
                    self._release_commitment_reservation(previous_session.commitment.hex())

                self.active_sessions[taker_nick] = session
                logger.info(
                    f"Created CoinJoin session with {taker_nick} "
                    f"(offer_id={offer_id}, type={offer.ordertype.value})"
                )

                # Fire-and-forget notification
                spawn_task(get_notifier().notify_fill_request(taker_nick, amount, offer_id))

                await self._send_response(taker_nick, "pubkey", response)
            else:
                self._release_commitment_reservation(commitment)
                reservation_owned = False
                logger.warning(f"Failed to handle fill: {response.get('error')}")

        except Exception as e:
            if reservation_owned:
                self._release_commitment_reservation(commitment)
            if session is not None:
                if self.active_sessions.get(taker_nick) is session:
                    self.active_sessions.pop(taker_nick, None)
            logger.error(f"Failed to handle !fill: {e}")

    async def _handle_auth(
        self: MakerBotProtocol, taker_nick: str, msg: str, source: str = "unknown"
    ) -> None:
        """Dispatch !auth to the per-taker MakerSession.

        Looks up the session, acquires its lock to serialize duplicate
        deliveries that arrive via multiple directory servers, and delegates
        the protocol logic to :meth:`MakerSession.on_auth`. Returning a
        warning early for unknown nicks preserves the pre-refactor contract
        where stray !auth messages were ignored gracefully.
        """
        session = self.active_sessions.get(taker_nick)
        if session is None:
            logger.warning(f"No active session for {taker_nick}")
            return

        await self._dispatch_session_handler(
            session,
            lambda: session.on_auth(self, msg, source),
            name=f"maker-auth-{taker_nick}",
        )

    async def _dispatch_session_handler(
        self: MakerBotProtocol,
        session: MakerSession,
        handler: Callable[[], Awaitable[None]],
        *,
        name: str,
    ) -> None:
        """Run a handler independently so the reaper cannot cancel a directory listener."""
        if self._session_handler_task_count >= MAX_SESSION_HANDLER_TASKS:
            self._log_rate_limited(
                "maker-session-handler-cap",
                f"Dropping maker session handler at task cap ({MAX_SESSION_HANDLER_TASKS})",
                interval=10.0,
            )
            return
        self._session_handler_task_count += 1
        task = asyncio.create_task(session.run_handler(self, handler), name=name)
        task.add_done_callback(self._session_handler_done)
        detached_wait = asyncio.create_task(session.detached_event.wait())
        try:
            done, _ = await asyncio.wait({task, detached_wait}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                try:
                    task.result()
                except asyncio.CancelledError:
                    if not session.expired:
                        raise
            else:
                self._register_detached_handler_task(task)
        except asyncio.CancelledError:
            task.cancel()
            if not task.done():
                self._register_detached_handler_task(task)
            raise
        finally:
            detached_wait.cancel()
            await asyncio.gather(detached_wait, return_exceptions=True)

    def _session_handler_done(self: MakerBotProtocol, task: asyncio.Task[None]) -> None:
        """Release handler admission and remove completed detached tasks."""
        self._detached_handler_tasks.discard(task)
        self._session_handler_task_count -= 1
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    def _register_detached_handler_task(self: MakerBotProtocol, task: asyncio.Task[None]) -> None:
        """Keep a strong, bounded reference to a cancellation-suppressing handler."""
        if task.done():
            return
        # Admission is capped before creation, so this condition indicates an
        # internal accounting error rather than attacker-controlled growth.
        if len(self._detached_handler_tasks) >= MAX_SESSION_HANDLER_TASKS:
            logger.error("Detached maker handler registry reached its hard cap")
            return
        self._detached_handler_tasks.add(task)

    async def _handle_tx(
        self: MakerBotProtocol, taker_nick: str, msg: str, source: str = "unknown"
    ) -> None:
        """Dispatch !tx to the per-taker MakerSession.

        Mirrors :meth:`_handle_auth`: looks the session up, holds its lock for
        the call, and lets :meth:`MakerSession.on_tx` carry out the signing /
        history / notifier work.
        """
        session = self.active_sessions.get(taker_nick)
        if session is None:
            logger.warning(f"No active session for {taker_nick}")
            return

        await self._dispatch_session_handler(
            session,
            lambda: session.on_tx(self, msg, source),
            name=f"maker-tx-{taker_nick}",
        )

    async def _expire_timed_out_session(
        self: MakerBotProtocol, taker_nick: str, session: MakerSession
    ) -> bool:
        """Cancel, detach, and clean one expired session by object identity."""
        if self.active_sessions.get(taker_nick) is not session or session.cleanup_started is True:
            return False

        session.cleanup_started = True
        session.expired = True
        handler_task = session.handler_task
        if handler_task is not None and not handler_task.done():
            handler_task.cancel()
            try:
                await asyncio.wait({handler_task}, timeout=_HANDLER_CANCEL_GRACE_SEC)
            except asyncio.CancelledError:
                # Shutdown also cancels the reaper. Expiration cleanup must
                # still complete after the handler cancellation request.
                pass
            if not handler_task.done():
                self._register_detached_handler_task(handler_task)

        if self.active_sessions.get(taker_nick) is session:
            self.active_sessions.pop(taker_nick)
        session.detached = True
        session.detached_event.set()
        state = session.state
        logger.debug("Cleaning up timed out session: {} (state={})", taker_nick, state)

        # IOAUTH only reveals inputs and addresses. Until signatures have been
        # produced, those inputs can safely return to the offer pool.
        if session.signing_boundary_crossed is True or state in {
            CoinJoinState.SIG_SENT,
            CoinJoinState.COMPLETE,
        }:
            session.retain_input_locks()
        else:
            session.release_input_locks()

        commitment = session.commitment.hex()
        disclosed = state in {
            CoinJoinState.IOAUTH_SEND_STARTED,
            CoinJoinState.IOAUTH_SENT,
            CoinJoinState.TX_RECEIVED,
            CoinJoinState.SIG_SENT,
            CoinJoinState.COMPLETE,
        }
        if commitment in self._reserved_commitments and disclosed:
            if await self._broadcast_commitment(commitment):
                self._release_commitment_reservation(commitment)
        else:
            self._release_commitment_reservation(commitment)

        return True

    async def _register_pending_signed_round(
        self: MakerBotProtocol, session: MakerSession, txid: str
    ) -> bool:
        """Retain bounded post-sign identity and lease ownership for ``!push``."""
        if len(txid) != 64:
            return False
        now = time.monotonic()
        pending_ttl = session.inner.pending_broadcast_ttl_sec
        record = PendingSignedRound(
            taker_nick=session.taker_nick,
            txid=txid.lower(),
            input_lock_owner=session.inner.input_lock_owner,
            outpoints=frozenset(session.our_utxos),
            expires_at=now + pending_ttl,
            lock_ttl_sec=pending_ttl,
        )
        async with self._pending_signed_rounds_lock:
            self._prune_pending_signed_rounds_locked(now)
            key = (record.taker_nick, record.txid)
            if key not in self._pending_signed_rounds and (
                len(self._pending_signed_rounds) >= MAX_PENDING_SIGNED_ROUNDS
            ):
                logger.error(
                    f"Pending signed-round registry reached its cap ({MAX_PENDING_SIGNED_ROUNDS})"
                )
                return False
            self._pending_signed_rounds[key] = record
        return True

    def _prune_pending_signed_rounds_locked(self: MakerBotProtocol, now: float) -> None:
        for key, record in list(self._pending_signed_rounds.items()):
            if record.expires_at <= now:
                self._pending_signed_rounds.pop(key, None)

    async def _prune_pending_signed_rounds(self: MakerBotProtocol) -> None:
        """Forget expired push authorization while persisted leases expire independently."""
        async with self._pending_signed_rounds_lock:
            self._prune_pending_signed_rounds_locked(time.monotonic())

    async def _drain_pending_signed_rounds(self: MakerBotProtocol) -> None:
        """Renew post-sign leases for shutdown, then discard in-memory push state."""
        async with self._pending_signed_rounds_lock:
            records = list(self._pending_signed_rounds.values())
            self._pending_signed_rounds.clear()
        for record in records:
            try:
                renewed = self.wallet.renew_coinjoin_inputs(
                    set(record.outpoints),
                    owner=record.input_lock_owner,
                    ttl=record.lock_ttl_sec,
                )
            except Exception as exc:
                logger.error(f"Failed to retain pending signed-round locks on shutdown: {exc}")
                continue
            if not renewed:
                logger.error(
                    f"Pending signed-round input ownership was lost for {record.taker_nick}"
                )

    async def _handle_push(
        self: MakerBotProtocol, taker_nick: str, msg: str, source: str = "unknown"
    ) -> None:
        """Handle !push request from taker.

        The push message contains a base64-encoded signed transaction that the taker
        wants us to broadcast. This provides privacy benefits as the taker's IP is
        not linked to the transaction broadcast.

        Reference-compatible immediate pushes are broadcast only when they match
        a signed round retained for this taker and its input lease is still owned.

        Security considerations:
        - DoS risk: A malicious taker could spam !push messages with invalid data
        - Mitigation: Generic per-peer rate limiting (in directory server) prevents
          this from being a significant attack vector
        - The signed transaction's non-witness txid must match the retained round.
        - Owner-qualified input locks are atomically verified and renewed before
          broadcast, preventing a stale delayed push after lease reacquisition.

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
                tx_bytes = base64.b64decode(tx_b64, validate=True)
                tx_hex = tx_bytes.hex()
                txid = get_txid(tx_hex)
            except Exception as e:
                logger.error(f"Failed to decode !push transaction: {e}")
                return

            key = (taker_nick, txid.lower())
            async with self._pending_signed_rounds_lock:
                self._prune_pending_signed_rounds_locked(time.monotonic())
                pending = self._pending_signed_rounds.get(key)
                if pending is None:
                    logger.warning(
                        f"Rejecting unmatched !push from {taker_nick} for {txid[:16]}..."
                    )
                    return
                try:
                    renewed = self.wallet.renew_coinjoin_inputs(
                        set(pending.outpoints),
                        owner=pending.input_lock_owner,
                        ttl=pending.lock_ttl_sec,
                    )
                except Exception as exc:
                    logger.error(f"Rejecting !push after input-lock renewal failure: {exc}")
                    self._pending_signed_rounds.pop(key, None)
                    return
                if not renewed:
                    logger.error(
                        f"Rejecting !push from {taker_nick}: signed-round input ownership was lost"
                    )
                    self._pending_signed_rounds.pop(key, None)
                    return
                # Consume before the network await so duplicate pushes cannot
                # race a second broadcast attempt. The renewed persisted lease
                # remains for the complete pending-broadcast window.
                self._pending_signed_rounds.pop(key, None)

            logger.info(f"Received matched !push from {taker_nick}, broadcasting transaction...")

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

            # Validate format before adding to blacklist
            valid, error = validate_commitment_hex(commitment)
            if not valid:
                logger.debug(f"Ignoring invalid !hp2 commitment from {from_nick}: {error}")
                return

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

            # Validate format before relaying or blacklisting
            valid, error = validate_commitment_hex(commitment)
            if not valid:
                logger.debug(f"Ignoring invalid !hp2 relay from {from_nick}: {error}")
                return

            # Blacklist locally
            add_commitment(commitment)

            # Broadcast via ephemeral identity (fire-and-forget)
            spawn_task(self._broadcast_commitment_ephemeral(commitment, is_relay=True))

        except Exception as e:
            logger.error(f"Failed to handle !hp2 relay request: {e}")

    async def _broadcast_commitment(self, commitment: str) -> bool:
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

        Local persistence must succeed before the caller can release its
        in-memory reservation. The network broadcast is best-effort and
        fire-and-forget: connection failures are logged but do not affect the
        CoinJoin flow.
        """
        try:
            # Add to our own blacklist first (persists to disk)
            add_commitment(commitment)
        except Exception as e:
            logger.error(f"Failed to persist commitment: {e}")
            return False

        broadcast_coro = self._broadcast_commitment_ephemeral(commitment, is_relay=False)
        try:
            # Broadcast via ephemeral identity (fire-and-forget)
            spawn_task(broadcast_coro)

            logger.debug(f"Scheduled ephemeral commitment broadcast: {commitment[:16]}...")
        except Exception as e:
            broadcast_coro.close()
            logger.error(f"Failed to schedule commitment broadcast: {e}")

        return True

    async def _broadcast_commitment_ephemeral(
        self, commitment: str, *, is_relay: bool = False
    ) -> None:
        """Open ephemeral directory connections and broadcast a commitment.

        Creates short-lived connections to all configured directory servers
        using a fresh random nick identity and unique Tor stream isolation
        credentials, broadcasts the commitment as a public !hp2 message, then
        tears down the connections.

        Concurrency is bounded by two dedicated semaphores so that a Sybil
        flood of relayed peer requests cannot starve the maker's own
        post-ioauth broadcasts. Own broadcasts (``is_relay=False``) queue
        for their slot to guarantee propagation; relayed broadcasts
        (``is_relay=True``) are dropped on contention -- the commitment is
        already blacklisted locally by the caller.

        This is a background task -- errors are logged, not raised.
        """
        if is_relay:
            semaphore = self._hp2_relay_broadcast_semaphore
            try:
                await asyncio.wait_for(semaphore.acquire(), timeout=0)
            except TimeoutError:
                logger.debug(
                    f"Dropping relayed hp2 broadcast (concurrency limit): {commitment[:16]}..."
                )
                return
        else:
            semaphore = self._hp2_own_broadcast_semaphore
            await semaphore.acquire()

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
                        nick_auth_mode=self.config.nick_auth_mode,
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
            semaphore.release()
            for client in ephemeral_clients:
                try:
                    await client.close()
                except Exception:
                    pass

    async def _send_response(
        self: MakerBotProtocol, taker_nick: str, command: str, data: dict[str, Any]
    ) -> None:
        """Send a maker -> taker response.

        Only the unencrypted ``!pubkey`` response is built here -- it is sent
        before a session's NaCl context is available. The encrypted ``!ioauth``
        and ``!sig`` responses are delegated to
        :meth:`MakerSession.send_response`, which has access to the session's
        crypto state.
        """
        try:
            if command in ("ioauth", "sig"):
                session = self.active_sessions.get(taker_nick)
                if session is None:
                    logger.error(f"No active session for {taker_nick} to encrypt {command}")
                    return
                await session.send_response(self, command, data)
                return

            if command == "pubkey":
                # !pubkey <nacl_pubkey_hex> [features=<comma-separated>] - NOT encrypted
                # Features are optional and backwards compatible with legacy takers
                msg_content = data["nacl_pubkey"]
                features = data.get("features", [])
                if features:
                    msg_content += f" features={','.join(features)}"
            else:
                # Fallback to JSON for unknown commands
                msg_content = json.dumps(data)

            for client in self.directory_clients.values():
                await client.send_private_message(taker_nick, command, msg_content)

            logger.debug(f"Sent signed {command} to {taker_nick}")

        except Exception as e:
            logger.error(f"Failed to send response: {e}")
