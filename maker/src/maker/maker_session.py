"""
Per-taker CoinJoin orchestration session for a maker.

`MakerSession` is the per-taker_nick container that the maker bot creates when
a `!fill` arrives and discards when a CoinJoin completes, fails, or times out.
It owns:

- an inner `CoinJoinSession` (the protocol state machine: amount, address
  selections, PoDLE state, encryption context, our_utxos, etc.)
- an `asyncio.Lock` that serializes processing of duplicate messages that
  arrive via multiple directory servers / direct connections
- the per-taker protocol logic for `!auth`, `!tx`, and signed-response
  encoding/encryption (relocated from `ProtocolHandlersMixin` so that the
  maker bot acts as a thin dispatcher)

Mirrors `taker/src/taker/coinjoin_session.py` on the taker side.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jmcore.bitcoin import address_to_scriptpubkey, scriptpubkey_to_address
from jmcore.channel_input import (
    MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES,
    ChannelInputInvitePayload,
    decode_channel_input_message,
    encode_channel_input_message,
)
from jmcore.channel_ring_store import RingLifecycleState
from jmcore.cofunded_ring import (
    MAX_RING_CIPHERTEXT_BYTES,
    RingCancelPayload,
    RingPlanPayload,
    RingPrivatePlanPayload,
    decode_ring_message,
    encode_ring_message,
)
from jmcore.crypto import verify_signed_privmsg
from jmcore.fee_policy import format_low_fee_error, parse_low_fee_error
from jmcore.logging_context import coinjoin_id_from_commitment, coinjoin_log_context
from jmcore.network import ONION_HOSTID
from jmcore.notifications import get_notifier
from jmcore.protocol import MakerError, MessageType, UTXOMetadata, format_jm_message
from jmcore.tasks import spawn_task
from jmwallet.history import (
    HistoryWriteError,
    append_history_entry,
    create_maker_history_entry,
    mark_pending_transaction_failed,
    update_awaiting_transaction_signed,
)
from loguru import logger
from pydantic import ValidationError

from maker.coinjoin import CoinJoinSession, CoinJoinState
from maker.session_logging import log_coinjoin_message

if TYPE_CHECKING:
    from jmcore.encryption import CryptoSession
    from jmcore.models import Offer
    from jmcore.network import TCPConnection
    from jmswap.lnd import ChannelInputCandidate
    from jmwallet.wallet.models import UTXOInfo

    from maker.channel_ring import MakerRingParticipant
    from maker.protocols import MakerBotProtocol


MAX_UNSIGNED_TRANSACTION_SIZE = 1_000_000
MAX_UNSIGNED_TRANSACTION_B64_SIZE = ((MAX_UNSIGNED_TRANSACTION_SIZE + 2) // 3) * 4


@dataclass(frozen=True, slots=True)
class PendingSignedRound:
    """Minimal post-sign state required to authenticate a later ``!push``."""

    taker_nick: str
    txid: str
    input_lock_owner: str
    outpoints: frozenset[tuple[str, int]]
    expires_at: float
    lock_ttl_sec: float
    commitment: str = ""
    generation_id: int = 0


def _notification_coinjoin_id(commitment: object) -> str | None:
    """Best-effort correlation for notifications that must not block cleanup."""
    try:
        if isinstance(commitment, bytes):
            commitment = commitment.hex()
        if not isinstance(commitment, str):
            return None
        return coinjoin_id_from_commitment(commitment)
    except ValueError:
        return None


class MakerSession:
    """One CoinJoin session with a single taker.

    Owns the per-taker protocol state machine (`inner: CoinJoinSession`)
    plus the per-taker lock that serializes duplicate-message processing.
    Per-taker handler logic (`on_auth`, `on_tx`, `send_response`) lives on
    the session itself; `MakerBot` only routes incoming messages.
    """

    def __init__(self, inner: CoinJoinSession, generation_id: int = 0) -> None:
        self.inner = inner
        self.generation_id = generation_id
        self.lock = asyncio.Lock()
        self.podle_outpoint: tuple[str, int] | None = None
        # This is deliberately independent of an event loop so sessions remain
        # safe to construct in synchronous tests and embedding contexts.
        self.deadline = time.monotonic() + inner.session_timeout_sec
        self.inner.deadline = self.deadline
        self.handler_task: asyncio.Task[None] | None = None
        self.expired = False
        self.detached = False
        self.cleanup_started = False
        self.detached_event = asyncio.Event()
        self.ring_participant: MakerRingParticipant | None = None
        self.channel_input_key: tuple[str, int] | None = None

    # -- Identity -----------------------------------------------------------

    @property
    def taker_nick(self) -> str:
        return self.inner.taker_nick

    @property
    def offer(self) -> Offer:
        return self.inner.offer

    # -- State machine -----------------------------------------------------

    @property
    def state(self) -> CoinJoinState:
        return self.inner.state

    @state.setter
    def state(self, value: CoinJoinState) -> None:
        self.inner.state = value

    @property
    def crypto(self) -> CryptoSession:
        return self.inner.crypto

    @property
    def commitment(self) -> bytes:
        return self.inner.commitment

    @property
    def commitment_authenticated(self) -> bool:
        return self.inner.commitment_authenticated

    @property
    def signing_boundary_crossed(self) -> bool:
        return self.inner.signing_boundary_crossed is True or self.inner.state in {
            CoinJoinState.SIG_SENT,
            CoinJoinState.COMPLETE,
        }

    @property
    def ioauth_boundary_crossed(self) -> bool:
        """Whether sending maker inputs and addresses may have disclosed them."""
        return self.inner.state in {
            CoinJoinState.IOAUTH_SEND_STARTED,
            CoinJoinState.IOAUTH_SENT,
            CoinJoinState.TX_RECEIVED,
            CoinJoinState.SIG_SENT,
            CoinJoinState.COMPLETE,
        }

    @property
    def amount(self) -> int:
        return self.inner.amount

    @property
    def our_utxos(self) -> dict[tuple[str, int], UTXOInfo]:
        return self.inner.our_utxos

    def release_input_locks(self) -> None:
        """Release the persisted CoinJoin locks on our committed inputs.

        Called on terminal *failure* paths so the inputs become selectable
        again promptly instead of waiting for the lock TTL to expire. On
        success the inputs are spent, so the lock is left to auto-expire after
        the broadcast propagates. Safe to call when nothing was reserved.
        """
        try:
            record_loader = getattr(self.ring_participant, "_record", None)
            if record_loader is not None:
                record = record_loader()
                if record.active:
                    logger.warning("Retaining input locks for active ring record")
                    logger.bind(sensitive=True).warning(
                        f"Retaining input locks for active ring record {record.key.filename}"
                    )
                    return
            owner = getattr(self.inner, "input_lock_owner", None)
            if owner is None:
                self.inner.wallet.release_coinjoin_inputs(set(self.our_utxos.keys()))
            else:
                self.inner.wallet.release_coinjoin_inputs(set(self.our_utxos.keys()), owner=owner)
        except Exception as e:  # pragma: no cover - best-effort cleanup
            logger.debug("Failed to release input locks")
            logger.bind(sensitive=True).debug(
                f"Failed to release input locks for {self.taker_nick}: {e}"
            )

    def _release_retired_ring_resources(self, bot: MakerBotProtocol) -> None:
        """Release pre-sign resources only after durable ring retirement."""
        participant = self.ring_participant
        if participant is None or participant._record().state is not RingLifecycleState.RETIRED:
            return
        bot._release_podle_outpoint(self)
        self.release_input_locks()

    def retain_input_locks(self) -> None:
        """Best-effort renewal once maker signatures may exist."""
        try:
            renewed = self.inner.wallet.renew_coinjoin_inputs(
                set(self.our_utxos),
                owner=self.inner.input_lock_owner,
                ttl=self.inner.pending_broadcast_ttl_sec,
            )
        except Exception as exc:  # pragma: no cover - best-effort retention
            logger.error("Failed to retain signed input locks")
            logger.bind(sensitive=True).error(
                f"Failed to retain signed input locks for {self.taker_nick}: {exc}"
            )
            return
        if not renewed:
            logger.error(f"Signed input lock ownership was lost for {self.taker_nick}")

    @property
    def cj_address(self) -> str:
        return self.inner.cj_address

    @property
    def change_address(self) -> str:
        return self.inner.change_address

    @property
    def created_at(self) -> float:
        return self.inner.created_at

    @property
    def comm_channel(self) -> str:
        return self.inner.comm_channel

    @property
    def peer_neutrino_compat(self) -> bool:
        return self.inner.peer_neutrino_compat

    # -- Lifecycle helpers -------------------------------------------------

    def is_timed_out(self) -> bool:
        return time.monotonic() >= self.deadline

    def remaining_timeout(self) -> float:
        """Return the time left before the session's absolute deadline."""
        return max(0.0, self.deadline - time.monotonic())

    def begin_pre_sign_wait(self) -> bool:
        """Shorten the deadline and renew locks before disclosing maker inputs."""
        phase_deadline = time.monotonic() + self.inner.pre_sign_timeout_sec
        self.deadline = min(self.deadline, phase_deadline)
        self.inner.deadline = self.deadline
        return self.inner.wallet.renew_coinjoin_inputs(
            set(self.our_utxos),
            owner=self.inner.input_lock_owner,
            ttl=self.remaining_timeout(),
        )

    def is_active(self, bot: MakerBotProtocol) -> bool:
        """Return whether this exact session may still progress."""
        return (
            not self.expired
            and bot.active_sessions.get((self.generation_id, self.taker_nick)) is self
        )

    async def run_handler(
        self,
        bot: MakerBotProtocol,
        handler: Callable[[], Awaitable[None]],
    ) -> None:
        """Serialize and track one auth/tx handler for deadline cancellation."""
        async with self.lock:
            if not self.is_active(bot) or self.is_timed_out():
                return
            task = asyncio.current_task()
            if task is None:  # pragma: no cover - asyncio always supplies one here
                return
            self.handler_task = task
            try:
                with coinjoin_log_context(self.commitment.hex()):
                    await handler()
            finally:
                if self.handler_task is task:
                    self.handler_task = None

    def validate_channel(self, source: str) -> bool:
        return self.inner.validate_channel(source)

    # -- Protocol phase pass-throughs --------------------------------------

    async def handle_fill(
        self, amount: int, commitment: str, taker_pk: str
    ) -> tuple[bool, dict[str, Any]]:
        return await self.inner.handle_fill(amount, commitment, taker_pk)

    async def handle_auth(
        self,
        commitment: str,
        revelation: dict[str, Any],
        kphex: str,
        exclude_utxos: set[tuple[str, int]] | None = None,
        active_check: Callable[[], bool] | None = None,
        podle_admission: Callable[[tuple[str, int]], bool] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        return await self.inner.handle_auth(
            commitment,
            revelation,
            kphex,
            exclude_utxos=exclude_utxos,
            active_check=active_check,
            podle_admission=podle_admission,
        )

    async def handle_tx(
        self, tx_hex: str, active_check: Callable[[], bool] | None = None
    ) -> tuple[bool, dict[str, Any]]:
        return await self.inner.handle_tx(tx_hex, active_check=active_check)

    def _channel_input_generation(self, bot: MakerBotProtocol) -> Any:
        resolver = getattr(bot, "_generation", None)
        if not callable(resolver):
            return bot
        generation = resolver(self.generation_id)
        if generation is None:
            raise RuntimeError("channel-input session generation is unavailable")
        return generation

    def _channel_input_binding(self, local_actor: str) -> str:
        if len(self.commitment) != 32 or not self.inner.input_lock_owner:
            raise ValueError("channel-input requires authenticated ordinary input ownership")
        facts = (
            "jm-channel-input-ordinary-v1",
            self.generation_id,
            local_actor,
            self.taker_nick,
            self.commitment.hex(),
            self.inner.input_lock_owner,
        )
        return hashlib.sha256(json.dumps(facts, separators=(",", ":")).encode("ascii")).hexdigest()

    async def _handle_channel_input_tx(
        self, bot: MakerBotProtocol, tx_hex: str
    ) -> tuple[bool, dict[str, Any]]:
        try:
            registry = bot._channel_input_registry
            if registry is None or self.channel_input_key is None:
                raise ValueError("channel-input authorization is unavailable")
            generation = self._channel_input_generation(bot)
            binding = self._channel_input_binding(generation.nick_identity.nick)
            # Recovery cancellation uses the same lock. It must not invalidate
            # authorization while ordinary signatures are being produced.
            async with registry.lock_for(*self.channel_input_key):
                negotiation = registry.get(*self.channel_input_key)
                if negotiation is None:
                    raise ValueError("channel-input authorization is missing")
                return await self.inner.handle_tx(
                    tx_hex,
                    active_check=lambda: self.is_active(bot),
                    channel_input=negotiation,
                    channel_input_binding=binding,
                )
        except Exception as exc:
            return False, {"error": str(exc)}

    # -- Per-taker handler bodies (moved from ProtocolHandlersMixin) -------

    async def on_auth(self, bot: MakerBotProtocol, msg: str, source: str) -> None:
        """Process a decrypted !auth message and emit !ioauth or !error.

        Acquires no locks of its own; the dispatcher in
        `ProtocolHandlersMixin._handle_auth` holds `self.lock` for the
        duration of this call. Removes the session entry from
        `bot.active_sessions` on terminal failure paths.
        """
        taker_nick = self.taker_nick
        try:
            if not self.is_active(bot):
                return
            # Record the channel (always accepted; takers may switch
            # direct<->directory mid-session, see validate_channel).
            self.validate_channel(source)

            if self.state != CoinJoinState.PUBKEY_SENT:
                logger.debug(
                    f"Ignoring duplicate !auth from {taker_nick} "
                    f"(state={self.state}, expected=PUBKEY_SENT)"
                )
                return

            log_coinjoin_message(
                "received",
                "auth",
                peer=taker_nick,
                transport=source,
                payload_length=len(msg.encode("utf-8")),
                state=self.state.value,
            )
            logger.debug(f"Received !auth from {taker_nick}, decrypting and verifying PoDLE...")

            parts = msg.split()
            if len(parts) < 2:
                logger.error("Invalid !auth format: missing encrypted data")
                return

            encrypted_data = parts[1]

            if not self.crypto.is_encrypted:
                logger.error("Encryption not set up for this session")
                return

            try:
                decrypted = self.crypto.decrypt(encrypted_data)
                logger.debug(f"Decrypted auth message length: {len(decrypted)}")
            except Exception as e:
                logger.error(f"Failed to decrypt auth message: {e}")
                return

            try:
                revelation_parts = decrypted.split("|")
                if len(revelation_parts) != 5:
                    logger.error(
                        f"Invalid revelation format: expected 5 parts, got {len(revelation_parts)}"
                    )
                    return

                utxo_str, p_hex, p2_hex, sig_hex, e_hex = revelation_parts

                if ":" not in utxo_str:
                    logger.error("Invalid UTXO format")
                    logger.bind(sensitive=True).error(f"Invalid UTXO format: {utxo_str}")
                    return

                if not utxo_str.rsplit(":", 1)[1].isdigit():
                    logger.error("Invalid vout in UTXO")
                    logger.bind(sensitive=True).error(f"Invalid vout in UTXO: {utxo_str}")
                    return

                try:
                    UTXOMetadata.from_str(utxo_str)
                except (ValueError, ValidationError) as e:
                    logger.error("Invalid UTXO in PoDLE revelation")
                    logger.bind(sensitive=True).error(f"Invalid UTXO in PoDLE revelation: {e}")
                    return

                revelation: dict[str, Any] = {
                    "utxo": utxo_str,
                    "P": p_hex,
                    "P2": p2_hex,
                    "sig": sig_hex,
                    "e": e_hex,
                }
                logger.bind(sensitive=True).debug(
                    f"Parsed revelation: utxo={utxo_str}, P={p_hex[:16]}..."
                )
            except Exception as e:
                logger.error("Failed to parse revelation")
                logger.bind(sensitive=True).error(f"Failed to parse revelation: {e}")
                return

            commitment = self.commitment.hex()
            kphex = ""

            # UTXO selection excludes inputs already committed to other
            # in-flight rounds via persisted, self-expiring locks (see
            # WalletService.reserve_coinjoin_inputs / CoinJoinSession.
            # _select_our_utxos), so the same input is never signed into two
            # concurrent CoinJoins.
            success, response = await self.handle_auth(
                commitment,
                revelation,
                kphex,
                active_check=lambda: self.is_active(bot),
                podle_admission=lambda outpoint: bot._reserve_podle_outpoint(outpoint, self),
            )
            if not self.is_active(bot):
                return

            if success:
                logger.info("Taker authentication accepted")
                # CRITICAL: Record addresses to history BEFORE revealing them to taker
                # so they are never reused even if the taker vanishes or we crash.
                try:
                    our_utxos = list(self.our_utxos.keys())
                    our_input_addresses = [u.address for u in self.our_utxos.values()]
                    input_value = sum(u.value for u in self.our_utxos.values())
                    history_entry = create_maker_history_entry(
                        taker_nick=taker_nick,
                        cj_amount=self.amount,
                        fee_received=0,
                        txfee_contribution=0,
                        cj_address=self.cj_address,
                        change_address=self.change_address,
                        our_utxos=our_utxos,
                        txid=None,
                        network=bot.config.network.value,
                        wallet_fingerprint=bot.wallet.wallet_fingerprint,
                        source_addresses=our_input_addresses,
                        input_value=input_value,
                    )
                    history_entry.failure_reason = "Awaiting transaction"
                    append_history_entry(history_entry, data_dir=bot.config.data_dir)
                    logger.bind(sensitive=True).debug(
                        f"Recorded revealed addresses for {taker_nick} in history "
                        f"(cj={self.cj_address[:12]}..., "
                        f"change={self.change_address[:12]}...)"
                    )
                except Exception as e:
                    logger.error("Refusing to reveal addresses because history persistence failed")
                    logger.bind(sensitive=True).error(
                        f"Refusing to reveal addresses because history persistence failed: {e}"
                    )
                    if bot.active_sessions.get((self.generation_id, taker_nick)) is self:
                        bot.active_sessions.pop((self.generation_id, taker_nick))
                        bot._release_podle_outpoint(self)
                        self.release_input_locks()
                        bot._release_commitment_reservation(commitment)
                    return

                if not self.is_active(bot):
                    return
                if not self.begin_pre_sign_wait():
                    logger.error("Maker input lock ownership was lost before !ioauth")
                    if bot.active_sessions.get((self.generation_id, taker_nick)) is self:
                        bot.active_sessions.pop((self.generation_id, taker_nick))
                        bot._release_podle_outpoint(self)
                        self.release_input_locks()
                        bot._release_commitment_reservation(commitment)
                    return
                sent = await self.send_response(bot, "ioauth", response)
                if not self.is_active(bot):
                    return
                if not sent:
                    return
                self.state = CoinJoinState.IOAUTH_SENT

                # Broadcast the commitment via hp2 so other makers can blacklist it.
                persisted = await bot._broadcast_commitment(commitment)
                if not self.is_active(bot):
                    return
                if persisted:
                    bot._release_commitment_reservation(commitment)
            else:
                error_msg = response.get("error", "unknown error")
                error_reason = response.get("error_reason", "Authentication failed")
                peer_error = (
                    MakerError.VERIFICATION_UNAVAILABLE
                    if response.get("error_code") == "utxo_verification_unavailable"
                    else MakerError.AUTHENTICATION_FAILED
                )
                logger.warning(f"Authentication rejected: {error_reason}")
                logger.bind(sensitive=True).warning(f"Authentication rejected: {error_msg}")

                try:
                    clients = list(bot._generation_clients(self.generation_id).items())
                    for node_id, client in clients:
                        await client.send_private_message(taker_nick, "error", peer_error.value)
                        log_coinjoin_message(
                            "sent",
                            "error",
                            peer=taker_nick,
                            transport=f"directory:{node_id}",
                            payload_length=len(peer_error.value.encode("utf-8")),
                            state=self.state.value,
                            outcome="rejected",
                        )
                        if not self.is_active(bot):
                            return
                    logger.debug(f"Sent !error to {taker_nick}: {peer_error.value}")
                except Exception as e:
                    logger.warning("Failed to send !error")
                    logger.bind(sensitive=True).warning(
                        f"Failed to send !error to {taker_nick}: {e}"
                    )

                # Release protocol resources before best-effort notification
                # work so notifier failures cannot extend the reservation.
                if bot.active_sessions.get((self.generation_id, taker_nick)) is self:
                    bot.active_sessions.pop((self.generation_id, taker_nick))
                    bot._release_podle_outpoint(self)
                    self.release_input_locks()
                    bot._release_commitment_reservation(commitment)

                spawn_task(
                    get_notifier().notify_rejection(
                        taker_nick,
                        error_reason,
                        error_msg,
                        _notification_coinjoin_id(commitment),
                    )
                )

        except Exception as e:
            logger.error("Failed to handle !auth")
            logger.bind(sensitive=True).error(f"Failed to handle !auth: {e}")

    async def on_tx(self, bot: MakerBotProtocol, msg: str, source: str) -> None:
        """Process a decrypted !tx message and emit !sig signatures.

        Acquires no locks; the dispatcher holds `self.lock`. Removes the
        session entry from `bot.active_sessions` on terminal paths.
        """
        taker_nick = self.taker_nick
        try:
            if not self.is_active(bot):
                return
            # Record the channel (always accepted; takers may switch
            # direct<->directory mid-session, see validate_channel).
            self.validate_channel(source)

            if self.state != CoinJoinState.IOAUTH_SENT:
                logger.debug(
                    f"Ignoring duplicate !tx from {taker_nick} "
                    f"(state={self.state}, expected=IOAUTH_SENT)"
                )
                return

            log_coinjoin_message(
                "received",
                "tx",
                peer=taker_nick,
                transport=source,
                payload_length=len(msg.encode("utf-8")),
                state=self.state.value,
            )
            logger.debug(f"Received !tx from {taker_nick}, decrypting and verifying transaction...")

            parts = msg.split()
            if len(parts) < 2:
                logger.warning("Invalid !tx format")
                return

            encrypted_data = parts[1]

            if not self.crypto.is_encrypted:
                logger.error("Encryption not set up for this session")
                return

            try:
                decrypted = self.crypto.decrypt(encrypted_data)
                logger.debug(f"Decrypted tx message length: {len(decrypted)}")
            except Exception as e:
                logger.error(f"Failed to decrypt tx message: {e}")
                return

            try:
                if len(decrypted) > MAX_UNSIGNED_TRANSACTION_B64_SIZE:
                    logger.warning("Encoded transaction exceeds maximum size")
                    return
                tx_bytes = base64.b64decode(decrypted, validate=True)
                if len(tx_bytes) > MAX_UNSIGNED_TRANSACTION_SIZE:
                    logger.warning("Decoded transaction exceeds maximum size")
                    return
                tx_hex = tx_bytes.hex()
                logger.bind(sensitive=True).debug(
                    f"Decoded transaction hex ({len(tx_bytes)} bytes): {tx_hex}"
                )
            except Exception as e:
                logger.error("Failed to decode transaction")
                logger.bind(sensitive=True).error(f"Failed to decode transaction: {e}")
                return

            if self.channel_input_key is not None:
                success, response = await self._handle_channel_input_tx(bot, tx_hex)
            elif self.ring_participant is None:
                success, response = await self.handle_tx(
                    tx_hex, active_check=lambda: self.is_active(bot)
                )
            else:
                try:
                    if not self.is_active(bot):
                        return
                    if (
                        self.inner.backend.can_lookup_arbitrary_utxos()
                        and self.inner.minimum_fee_rate_sat_vb is not None
                    ):
                        fee_policy_error = await self.inner._verify_minimum_miner_fee(
                            tx_hex, lambda: self.is_active(bot)
                        )
                        if fee_policy_error is not None:
                            raise ValueError(fee_policy_error)
                    if not self.inner.wallet.renew_coinjoin_inputs(
                        set(self.our_utxos),
                        owner=self.inner.input_lock_owner,
                        ttl=self.inner.pending_broadcast_ttl_sec,
                    ):
                        raise ValueError("Maker input lock ownership was lost before ring signing")
                    self.ring_participant.prepare_coinjoin_signing(tx_hex)
                    self.inner.signing_boundary_crossed = True
                    self.state = CoinJoinState.SIG_SENT
                    signatures = await self.inner._sign_transaction(
                        tx_hex, active_check=lambda: self.is_active(bot)
                    )
                    if not signatures:
                        raise ValueError("Failed to sign exact ring transaction")
                    self.ring_participant.mark_signatures_sent(signatures)
                    from jmcore.bitcoin import get_txid

                    success = True
                    response = {"signatures": signatures, "txid": get_txid(tx_hex)}
                except Exception as exc:
                    success = False
                    response = {"error": str(exc)}
            if not self.is_active(bot):
                return

            if success:
                signatures = response.get("signatures", [])
                txid = response.get("txid", "")
                destination_vout = response.get("destination_vout", -1)
                if not isinstance(destination_vout, int):
                    destination_vout = -1
                if not await bot._register_pending_signed_round(self, txid):
                    logger.error(
                        f"Cannot retain signed round for {taker_nick}; withholding signatures"
                    )
                    if bot.active_sessions.get((self.generation_id, taker_nick)) is self:
                        bot.active_sessions.pop((self.generation_id, taker_nick))
                        bot._release_podle_outpoint(self)
                    self.retain_input_locks()
                    return
                for sig in signatures:
                    if not self.is_active(bot):
                        return
                    await self.send_response(bot, "sig", {"signature": sig})
                    if not self.is_active(bot):
                        return
                logger.info(f"CoinJoin with {taker_nick} COMPLETE (sent {len(signatures)} sigs)")

                fee_received = self.offer.calculate_fee(self.amount)
                txfee_contribution = self.offer.txfee

                try:
                    updated = update_awaiting_transaction_signed(
                        destination_address=self.cj_address,
                        txid=txid,
                        fee_received=fee_received,
                        txfee_contribution=txfee_contribution,
                        destination_vout=destination_vout,
                        data_dir=bot.config.data_dir,
                        wallet_fingerprint=bot.wallet.wallet_fingerprint,
                    )
                    net = fee_received - txfee_contribution
                    if updated:
                        logger.bind(sensitive=True).debug(
                            f"Updated CoinJoin history with txid: net fee {net} sats"
                        )
                    else:
                        logger.warning(
                            "No 'Awaiting transaction' entry found, creating new history entry"
                        )
                        our_utxos = list(self.our_utxos.keys())
                        our_input_addresses = [u.address for u in self.our_utxos.values()]
                        input_value = sum(u.value for u in self.our_utxos.values())
                        history_entry = create_maker_history_entry(
                            taker_nick=taker_nick,
                            cj_amount=self.amount,
                            fee_received=fee_received,
                            txfee_contribution=txfee_contribution,
                            cj_address=self.cj_address,
                            change_address=self.change_address,
                            our_utxos=our_utxos,
                            txid=txid,
                            network=bot.config.network.value,
                            wallet_fingerprint=bot.wallet.wallet_fingerprint,
                            source_addresses=our_input_addresses,
                            input_value=input_value,
                            destination_vout=destination_vout,
                        )
                        append_history_entry(history_entry, data_dir=bot.config.data_dir)
                        logger.bind(sensitive=True).debug(
                            f"Created new CoinJoin history: net fee {net} sats"
                        )
                except Exception as e:
                    logger.warning("Failed to update CoinJoin history")
                    logger.bind(sensitive=True).warning(f"Failed to update CoinJoin history: {e}")

                spawn_task(
                    get_notifier().notify_tx_signed(
                        taker_nick,
                        self.amount,
                        len(signatures),
                        fee_received,
                        _notification_coinjoin_id(self.commitment),
                    )
                )

                if bot.active_sessions.get((self.generation_id, taker_nick)) is self:
                    self.state = CoinJoinState.COMPLETE
                    if self.ring_participant is None and self.channel_input_key is None:
                        bot.active_sessions.pop((self.generation_id, taker_nick))
                        bot._release_podle_outpoint(self)

                # Schedule wallet re-sync in background to avoid blocking !push handling
                spawn_task(bot._deferred_wallet_resync())
            else:
                logger.error("Transaction verification failed")
                logger.bind(sensitive=True).error(
                    f"Transaction verification failed: {response.get('error')}"
                )
                try:
                    if not self.signing_boundary_crossed:
                        error_msg = str(response.get("error") or "Transaction verification failed")
                        # We have definitively refused to sign. Keep the revealed
                        # addresses recorded, but do not leave this attempt pending.
                        try:
                            finalized = mark_pending_transaction_failed(
                                destination_address=self.cj_address,
                                failure_reason=f"Signing rejected: {error_msg}",
                                data_dir=bot.config.data_dir,
                                txid="",
                                wallet_fingerprint=bot.wallet.wallet_fingerprint,
                            )
                            if not finalized:
                                logger.warning("Could not finalize rejected CoinJoin history entry")
                        except HistoryWriteError as exc:
                            logger.warning("Could not finalize rejected CoinJoin history entry")
                            logger.bind(sensitive=True).warning(
                                "Rejected CoinJoin history finalization detail: {}", exc
                            )

                        # Only the bounded fee diagnostic is safe to disclose.
                        # Other verification errors may include backend details.
                        fee_rejection = parse_low_fee_error(error_msg)
                        peer_error = (
                            format_low_fee_error(*fee_rejection)
                            if fee_rejection is not None
                            else "Transaction verification failed"
                        )
                        await self.send_response(bot, "error", {"error": peer_error})
                finally:
                    # Sending a refusal can fail or be cancelled. Always clean
                    # up this session, while retaining locks if signing began.
                    if bot.active_sessions.get((self.generation_id, taker_nick)) is self:
                        bot.active_sessions.pop((self.generation_id, taker_nick))
                        bot._release_podle_outpoint(self)
                        if self.signing_boundary_crossed:
                            self.retain_input_locks()
                        else:
                            self.release_input_locks()
                spawn_task(
                    get_notifier().notify_rejection(
                        taker_nick,
                        "TX verification failed",
                        response.get("error", ""),
                        _notification_coinjoin_id(self.commitment),
                    )
                )

        except Exception as e:
            logger.error("Failed to handle !tx")
            logger.bind(sensitive=True).error(f"Failed to handle !tx: {e}")

    async def on_ring(self, bot: MakerBotProtocol, msg: str, source: str) -> None:
        """Decrypt, authenticate, and dispatch one canonical JMP-0010 envelope."""

        self.validate_channel(source)
        if self.channel_input_key is not None:
            logger.warning("Rejecting !ring in a channel-input session")
            return
        if self.state != CoinJoinState.IOAUTH_SENT:
            logger.warning("Rejecting !ring before CoinJoin authentication is complete")
            logger.bind(sensitive=True).warning(
                f"Rejecting !ring from {self.taker_nick}: PoDLE/!ioauth not complete"
            )
            return
        if (
            not bot.config.channel_ring.enabled
            or not bot.channel_ring_capability_validated
            or bot._channel_ring_backend is None
            or bot._channel_ring_store is None
        ):
            logger.warning("Rejecting !ring because the feature is unavailable")
            logger.bind(sensitive=True).warning(
                f"Rejecting !ring from {self.taker_nick}: feature is unavailable"
            )
            return
        if not self.crypto.is_encrypted:
            logger.warning("Rejecting !ring because encryption is unavailable")
            logger.bind(sensitive=True).warning(
                f"Rejecting !ring from {self.taker_nick}: encryption is unavailable"
            )
            return
        parts = msg.split()
        if len(parts) != 4 or len(parts[1]) > MAX_RING_CIPHERTEXT_BYTES:
            logger.warning("Rejecting malformed encrypted !ring")
            logger.bind(sensitive=True).warning(
                f"Rejecting malformed encrypted !ring from {self.taker_nick}"
            )
            return
        payload = None
        try:
            verified, command, encrypted = verify_signed_privmsg(self.taker_nick, msg, ONION_HOSTID)
            if not verified or command != "ring" or encrypted != parts[1]:
                raise ValueError("invalid signed ring envelope")
            plaintext = self.crypto.decrypt(encrypted)
            payload = decode_ring_message(plaintext)
            if self.ring_participant is None:
                from maker.channel_ring import MakerRingParticipant

                self.ring_participant = MakerRingParticipant(
                    self,
                    config=bot.config.channel_ring,
                    store=bot._channel_ring_store,
                    initialized_backend=bot._channel_ring_backend,
                    chain_backend=bot.backend,
                )
            responses = await self.ring_participant.handle(payload)
            if isinstance(payload, RingCancelPayload):
                self._release_retired_ring_resources(bot)
            for response in responses:
                encrypted = self.crypto.encrypt(encode_ring_message(response))
                await self._send_ring_response(bot, encrypted, source)
        except Exception as exc:
            logger.warning("Rejected invalid or out-of-order !ring")
            logger.bind(sensitive=True).warning(
                f"Rejected invalid or out-of-order !ring from {self.taker_nick} "
                f"({type(exc).__name__})"
            )
            if isinstance(payload, (RingPlanPayload, RingPrivatePlanPayload)) and (
                self.ring_participant is not None
            ):
                try:
                    responses = self.ring_participant.reject_plan(payload)
                    self._release_retired_ring_resources(bot)
                    for response in responses:
                        encrypted = self.crypto.encrypt(encode_ring_message(response))
                        await self._send_ring_response(bot, encrypted, source)
                except Exception as rejection_exc:
                    logger.warning(
                        "Could not durably reject channel-ring plan from "
                        f"{self.taker_nick} ({type(rejection_exc).__name__})"
                    )

    async def on_channel_input(
        self,
        bot: MakerBotProtocol,
        msg: str,
        source: str,
        direct_connection: TCPConnection | None = None,
    ) -> None:
        """Decrypt and dispatch one authenticated channel-input envelope."""

        self.validate_channel(source)
        if self.state not in {CoinJoinState.IOAUTH_SENT, CoinJoinState.COMPLETE}:
            logger.warning(
                f"Rejecting !channel_input from {self.taker_nick}: PoDLE/!ioauth not complete"
            )
            return
        if self.ring_participant is not None:
            logger.warning("Rejecting !channel_input in a ring session")
            return
        if (
            not bot.config.channel_input.enabled
            or bot._channel_input_backend is None
            or bot._channel_input_registry is None
        ):
            logger.warning(
                f"Rejecting !channel_input from {self.taker_nick}: feature is unavailable"
            )
            return
        if not self.crypto.is_encrypted:
            logger.warning(
                f"Rejecting !channel_input from {self.taker_nick}: encryption is unavailable"
            )
            return
        if source == "direct" and direct_connection is None:
            logger.warning(
                f"Rejecting !channel_input from {self.taker_nick}: direct source is unavailable"
            )
            return
        if source != "direct" and not source.startswith("dir:"):
            logger.warning(
                f"Rejecting !channel_input from {self.taker_nick}: source is unavailable"
            )
            return
        parts = msg.split()
        if len(parts) != 4 or len(parts[1]) > MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES:
            logger.warning(f"Rejecting malformed encrypted !channel_input from {self.taker_nick}")
            return
        newly_bound_key: tuple[str, int] | None = None
        try:
            generation = self._channel_input_generation(bot)
            if source.startswith("dir:") and source[4:] not in generation.directory_clients:
                raise ValueError("channel-input directory source is unavailable")
            verified, command, encrypted = verify_signed_privmsg(self.taker_nick, msg, ONION_HOSTID)
            if not verified or command != "channel_input" or encrypted != parts[1]:
                raise ValueError("invalid signed channel-input envelope")
            payload = decode_channel_input_message(self.crypto.decrypt(encrypted))
            initialized_backend = bot._channel_input_backend
            registry = bot._channel_input_registry
            if initialized_backend is None or registry is None:
                raise RuntimeError("channel-input runtime became unavailable")
            key = (payload.session_id, payload.attempt)
            if isinstance(payload, ChannelInputInvitePayload):
                if self.state != CoinJoinState.IOAUTH_SENT:
                    raise ValueError("channel-input invitation after ordinary signing")
                if self.channel_input_key is not None and key != self.channel_input_key:
                    raise ValueError("ordinary session already belongs to another channel attempt")
            elif key != self.channel_input_key:
                raise ValueError("channel-input payload is not bound to this ordinary session")
            if (
                isinstance(payload, ChannelInputInvitePayload)
                and not bot.channel_input_capability_validated
                and registry.get(payload.session_id, payload.attempt) is None
            ):
                raise RuntimeError("channel-input admission is unavailable")
            local_actor = generation.nick_identity.nick
            binding = self._channel_input_binding(local_actor)
            # Keep the attempt association even if a freeze RPC fails after its
            # durable intent. Retries may only address that same attempt.
            if self.channel_input_key is None:
                newly_bound_key = key
            self.channel_input_key = key

            def settlement_script_for(candidate: ChannelInputCandidate) -> str:
                mixdepth = (self.inner.mixdepth + 1) % bot.wallet.mixdepth_count
                outpoint = candidate.funding_outpoint
                label = (
                    f"channel-input:{payload.session_id}:{payload.attempt}:"
                    f"{outpoint.txid}:{outpoint.vout}"
                )
                address = bot.wallet.get_new_persisted_internal_address(mixdepth, label)
                script = address_to_scriptpubkey(address)
                if len(script) != 34 or script[:2] != b"\x51\x20":
                    raise ValueError("channel-input settlement address is not P2TR")
                if scriptpubkey_to_address(script, bot.wallet.network) != address:
                    raise ValueError("channel-input settlement address is not canonical")
                return script.hex()

            responses = await registry.handle_payload(
                payload,
                coordinator_actor=self.taker_nick,
                local_actor=local_actor,
                local_node_key=initialized_backend.node_info.identity_pubkey,
                backend=initialized_backend.backend,
                candidates=initialized_backend.candidates,
                settlement_script_for=settlement_script_for,
                current_time=int(time.time()),
                current_height=bot.current_block_height,
                ordinary_session_binding=binding,
            )
            if isinstance(payload, ChannelInputInvitePayload):
                bot._refresh_channel_input_capability()
            for response in responses:
                response_encrypted = self.crypto.encrypt(encode_channel_input_message(response))
                await self._send_channel_input_response(
                    bot, response_encrypted, source, direct_connection
                )
        except Exception as exc:
            # An invite that failed before creating any durable negotiation must
            # not poison the ordinary session with a phantom channel attempt.
            # After a durable freeze intent, the registry retains the
            # negotiation, so the binding stays for that exact attempt.
            if newly_bound_key is not None and self.channel_input_key == newly_bound_key:
                retained_registry = bot._channel_input_registry
                if retained_registry is None or retained_registry.get(*newly_bound_key) is None:
                    self.channel_input_key = None
            logger.warning(
                f"Rejected invalid or out-of-order !channel_input from {self.taker_nick} "
                f"({type(exc).__name__})"
            )
            logger.bind(sensitive=True).debug("Channel-input rejection detail: {}", exc)
            if exc.__cause__ is not None:
                logger.bind(sensitive=True).debug(
                    "Channel-input rejection cause: {}", exc.__cause__
                )

    async def _send_channel_input_response(
        self,
        bot: MakerBotProtocol,
        encrypted: str,
        source: str,
        direct_connection: TCPConnection | None,
    ) -> None:
        """Return a channel-input response only through the request channel."""

        generation = self._channel_input_generation(bot)
        if source == "direct":
            if direct_connection is None:
                raise RuntimeError("direct channel-input response channel is unavailable")
            identity = generation.nick_identity
            signed = identity.sign_message(encrypted, ONION_HOSTID)
            line = format_jm_message(identity.nick, self.taker_nick, "channel_input", signed)
            await direct_connection.send(
                json.dumps({"type": MessageType.PRIVMSG.value, "line": line}).encode("utf-8")
            )
            return
        if source.startswith("dir:"):
            client = generation.directory_clients.get(source[4:])
            if client is None:
                raise RuntimeError("directory channel-input response channel is unavailable")
            await client.send_private_message(self.taker_nick, "channel_input", encrypted)
            return
        raise RuntimeError("channel-input request has no authenticated response channel")

    async def _send_ring_response(self, bot: MakerBotProtocol, encrypted: str, source: str) -> None:
        """Return a ring response only through the authenticated request channel."""

        generation_resolver = getattr(bot, "_generation", None)
        generation = (
            generation_resolver(self.generation_id) if callable(generation_resolver) else None
        )
        if source == "direct":
            direct_connections = (
                generation.direct_connections if generation is not None else bot.direct_connections
            )
            connection = direct_connections.get(self.taker_nick)
            if connection is None:
                raise RuntimeError("direct ring response channel is unavailable")
            identity = generation.nick_identity if generation is not None else bot.nick_identity
            signed = identity.sign_message(encrypted, ONION_HOSTID)
            line = format_jm_message(identity.nick, self.taker_nick, "ring", signed)
            await connection.send(
                json.dumps({"type": MessageType.PRIVMSG.value, "line": line}).encode("utf-8")
            )
            return
        if source.startswith("dir:"):
            directory_clients = (
                generation.directory_clients if generation is not None else bot.directory_clients
            )
            client = directory_clients.get(source[4:])
            if client is None:
                raise RuntimeError("directory ring response channel is unavailable")
            await client.send_private_message(self.taker_nick, "ring", encrypted)
            return
        raise RuntimeError("ring request has no authenticated response channel")

    async def send_response(
        self, bot: MakerBotProtocol, command: str, data: dict[str, Any]
    ) -> bool:
        """Send a signed response through this generation's directory clients.

        `!ioauth` and `!sig` use the session's NaCl box. `!error` is plain text,
        as in the reference protocol, authenticated by the transport signature.

        The `pubkey` response is sent unencrypted via
        :func:`MakerSession.send_pubkey_response` because it doesn't require
        an active session's `crypto` (the response IS the public key).
        """
        try:
            if not self.is_active(bot):
                return False
            if command == "ioauth":
                plaintext = " ".join(
                    [
                        data["utxo_list"],
                        data["auth_pub"],
                        data["cj_addr"],
                        data["change_addr"],
                        data["btc_sig"],
                    ]
                )
                msg_content = self.crypto.encrypt(plaintext)
                logger.debug(f"Encrypted ioauth message, plaintext_len={len(plaintext)}")
            elif command == "sig":
                plaintext = data["signature"]
                msg_content = self.crypto.encrypt(plaintext)
                logger.debug(f"Encrypted sig: plaintext_len={len(plaintext)}")
            elif command == "error":
                msg_content = data["error"]
            else:
                msg_content = json.dumps(data)

            clients = list(bot._generation_clients(self.generation_id).items())
            if not clients:
                logger.warning(f"No directory client available to send {command}")
                return False

            for index, (node_id, client) in enumerate(clients):
                if not self.is_active(bot):
                    return False
                if command == "ioauth" and index == 0:
                    if self.state != CoinJoinState.AUTH_RECEIVED:
                        logger.error(f"Cannot send !ioauth from state {self.state}")
                        return False
                    # From this point a transport error or cancellation cannot
                    # prove the encrypted maker details were not disclosed.
                    self.state = CoinJoinState.IOAUTH_SEND_STARTED
                await client.send_private_message(self.taker_nick, command, msg_content)
                log_coinjoin_message(
                    "sent",
                    command,
                    peer=self.taker_nick,
                    transport=f"directory:{node_id}",
                    payload_length=len(msg_content.encode("utf-8")),
                    state=self.state.value,
                )
                if not self.is_active(bot):
                    return False

            logger.debug(f"Sent signed {command} to {self.taker_nick}")
            if command == "ioauth":
                self.state = CoinJoinState.IOAUTH_SENT
            return True

        except Exception as e:
            logger.error("Failed to send response")
            logger.bind(sensitive=True).error(f"Failed to send response: {e}")
            return False
