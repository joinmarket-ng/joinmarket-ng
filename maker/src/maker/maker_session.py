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
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jmcore.fee_policy import format_low_fee_error, parse_low_fee_error
from jmcore.logging_context import coinjoin_id_from_commitment, coinjoin_log_context
from jmcore.notifications import get_notifier
from jmcore.protocol import MakerError, UTXOMetadata
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
    from jmwallet.wallet.models import UTXOInfo

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
            self.inner.wallet.release_coinjoin_inputs(
                set(self.our_utxos.keys()), owner=self.inner.input_lock_owner
            )
        except Exception as e:  # pragma: no cover - best-effort cleanup
            logger.debug("Failed to release input locks")
            logger.bind(sensitive=True).debug(
                f"Failed to release input locks for {self.taker_nick}: {e}"
            )

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

            success, response = await self.handle_tx(
                tx_hex, active_check=lambda: self.is_active(bot)
            )
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
