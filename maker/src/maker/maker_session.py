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
from typing import TYPE_CHECKING, Any

from jmcore.notifications import get_notifier
from jmcore.protocol import UTXOMetadata
from jmwallet.history import (
    append_history_entry,
    create_maker_history_entry,
    update_awaiting_transaction_signed,
)
from loguru import logger
from pydantic import ValidationError

from maker.coinjoin import CoinJoinSession, CoinJoinState

if TYPE_CHECKING:
    from jmcore.encryption import CryptoSession
    from jmcore.models import Offer
    from jmwallet.wallet.models import UTXOInfo

    from maker.protocols import MakerBotProtocol


class MakerSession:
    """One CoinJoin session with a single taker.

    Owns the per-taker protocol state machine (`inner: CoinJoinSession`)
    plus the per-taker lock that serializes duplicate-message processing.
    Per-taker handler logic (`on_auth`, `on_tx`, `send_response`) lives on
    the session itself; `MakerBot` only routes incoming messages.
    """

    def __init__(self, inner: CoinJoinSession) -> None:
        self.inner = inner
        self.lock = asyncio.Lock()

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
    def amount(self) -> int:
        return self.inner.amount

    @property
    def our_utxos(self) -> dict[tuple[str, int], UTXOInfo]:
        return self.inner.our_utxos

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
        return self.inner.is_timed_out()

    def validate_channel(self, source: str) -> bool:
        return self.inner.validate_channel(source)

    # -- Protocol phase pass-throughs --------------------------------------

    async def handle_fill(
        self, amount: int, commitment: str, taker_pk: str
    ) -> tuple[bool, dict[str, Any]]:
        return await self.inner.handle_fill(amount, commitment, taker_pk)

    async def handle_auth(
        self, commitment: str, revelation: dict[str, Any], kphex: str
    ) -> tuple[bool, dict[str, Any]]:
        return await self.inner.handle_auth(commitment, revelation, kphex)

    async def handle_tx(self, tx_hex: str) -> tuple[bool, dict[str, Any]]:
        return await self.inner.handle_tx(tx_hex)

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
            if not self.validate_channel(source):
                logger.error(f"Channel consistency violation for !auth from {taker_nick}")
                bot.active_sessions.pop(taker_nick, None)
                return

            if self.state != CoinJoinState.PUBKEY_SENT:
                logger.debug(
                    f"Ignoring duplicate !auth from {taker_nick} "
                    f"(state={self.state}, expected=PUBKEY_SENT)"
                )
                return

            logger.info(f"Received !auth from {taker_nick}, decrypting and verifying PoDLE...")

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
                    logger.error(f"Invalid utxo format: {utxo_str}")
                    return

                if not utxo_str.rsplit(":", 1)[1].isdigit():
                    logger.error(f"Invalid vout in utxo: {utxo_str}")
                    return

                try:
                    UTXOMetadata.from_str(utxo_str)
                except (ValueError, ValidationError) as e:
                    logger.error(f"Invalid UTXO in PoDLE revelation: {e}")
                    return

                revelation: dict[str, Any] = {
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

            commitment = self.commitment.hex()
            kphex = ""

            success, response = await self.handle_auth(commitment, revelation, kphex)

            if success:
                # CRITICAL: Record addresses to history BEFORE revealing them to taker
                # so they are never reused even if the taker vanishes or we crash.
                try:
                    our_utxos = list(self.our_utxos.keys())
                    our_input_addresses = [u.address for u in self.our_utxos.values()]
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
                    )
                    history_entry.failure_reason = "Awaiting transaction"
                    append_history_entry(history_entry, data_dir=bot.config.data_dir)
                    logger.debug(
                        f"Recorded revealed addresses for {taker_nick} in history "
                        f"(cj={self.cj_address[:12]}..., "
                        f"change={self.change_address[:12]}...)"
                    )
                except Exception as e:
                    logger.warning(f"Failed to record revealed addresses in history: {e}")

                await self.send_response(bot, "ioauth", response)

                # Broadcast the commitment via hp2 so other makers can blacklist it.
                await bot._broadcast_commitment(commitment)
            else:
                error_msg = response.get("error", "unknown error")
                error_code = response.get("error_code", "")
                logger.error(f"Auth failed: {error_msg}")

                try:
                    for client in bot.directory_clients.values():
                        await client.send_private_message(taker_nick, "error", error_msg)
                    logger.debug(f"Sent !error to {taker_nick}: {error_msg}")
                except Exception as e:
                    logger.warning(f"Failed to send !error to {taker_nick}: {e}")

                asyncio.create_task(
                    get_notifier().notify_rejection(
                        taker_nick,
                        error_code or "PoDLE verification failed",
                        error_msg,
                    )
                )
                bot.active_sessions.pop(taker_nick, None)

        except Exception as e:
            logger.error(f"Failed to handle !auth: {e}")

    async def on_tx(self, bot: MakerBotProtocol, msg: str, source: str) -> None:
        """Process a decrypted !tx message and emit !sig signatures.

        Acquires no locks; the dispatcher holds `self.lock`. Removes the
        session entry from `bot.active_sessions` on terminal paths.
        """
        taker_nick = self.taker_nick
        try:
            if not self.validate_channel(source):
                logger.error(f"Channel consistency violation for !tx from {taker_nick}")
                bot.active_sessions.pop(taker_nick, None)
                return

            if self.state != CoinJoinState.IOAUTH_SENT:
                logger.debug(
                    f"Ignoring duplicate !tx from {taker_nick} "
                    f"(state={self.state}, expected=IOAUTH_SENT)"
                )
                return

            logger.info(f"Received !tx from {taker_nick}, decrypting and verifying transaction...")

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
                tx_bytes = base64.b64decode(decrypted)
                tx_hex = tx_bytes.hex()
                logger.debug(f"Decoded transaction hex ({len(tx_bytes)} bytes): {tx_hex}")
            except Exception as e:
                logger.error(f"Failed to decode transaction: {e}")
                return

            success, response = await self.handle_tx(tx_hex)

            if success:
                signatures = response.get("signatures", [])
                for sig in signatures:
                    await self.send_response(bot, "sig", {"signature": sig})
                logger.info(f"CoinJoin with {taker_nick} COMPLETE (sent {len(signatures)} sigs)")

                fee_received = self.offer.calculate_fee(self.amount)
                txfee_contribution = self.offer.txfee

                try:
                    txid = response.get("txid", "")
                    updated = update_awaiting_transaction_signed(
                        destination_address=self.cj_address,
                        txid=txid,
                        fee_received=fee_received,
                        txfee_contribution=txfee_contribution,
                        data_dir=bot.config.data_dir,
                        wallet_fingerprint=bot.wallet.wallet_fingerprint,
                    )
                    net = fee_received - txfee_contribution
                    if updated:
                        logger.debug(f"Updated CoinJoin history with txid: net fee {net} sats")
                    else:
                        logger.warning(
                            "No 'Awaiting transaction' entry found, creating new history entry"
                        )
                        our_utxos = list(self.our_utxos.keys())
                        our_input_addresses = [u.address for u in self.our_utxos.values()]
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
                        )
                        append_history_entry(history_entry, data_dir=bot.config.data_dir)
                        logger.debug(f"Created new CoinJoin history: net fee {net} sats")
                except Exception as e:
                    logger.warning(f"Failed to update CoinJoin history: {e}")

                asyncio.create_task(
                    get_notifier().notify_tx_signed(
                        taker_nick,
                        self.amount,
                        len(signatures),
                        fee_received,
                    )
                )

                bot.active_sessions.pop(taker_nick, None)

                # Schedule wallet re-sync in background to avoid blocking !push handling
                asyncio.create_task(bot._deferred_wallet_resync())
            else:
                logger.error(f"TX verification failed: {response.get('error')}")
                asyncio.create_task(
                    get_notifier().notify_rejection(
                        taker_nick, "TX verification failed", response.get("error", "")
                    )
                )
                bot.active_sessions.pop(taker_nick, None)

        except Exception as e:
            logger.error(f"Failed to handle !tx: {e}")

    async def send_response(
        self, bot: MakerBotProtocol, command: str, data: dict[str, Any]
    ) -> None:
        """Send a signed response (`!ioauth` or `!sig`) encrypted via this
        session's NaCl box, fanned out to all of the bot's directory clients.

        The `pubkey` response is sent unencrypted via
        :func:`MakerSession.send_pubkey_response` because it doesn't require
        an active session's `crypto` (the response IS the public key).
        """
        try:
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
            else:
                msg_content = json.dumps(data)

            for client in bot.directory_clients.values():
                await client.send_private_message(self.taker_nick, command, msg_content)

            logger.debug(f"Sent signed {command} to {self.taker_nick}")

        except Exception as e:
            logger.error(f"Failed to send response: {e}")
