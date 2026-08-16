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
from typing import TYPE_CHECKING, Any

from jmcore.notifications import get_notifier
from jmcore.protocol import UTXOMetadata
from jmswap.errors import ChannelInputEscrowError
from jmswap.ring_authorization import RingEdgeAuthorization, RingEdgeFunding, RingEdgeRole
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

    def release_input_locks(self) -> None:
        """Release the persisted CoinJoin locks on our committed inputs.

        Called on terminal *failure* paths so the inputs become selectable
        again promptly instead of waiting for the lock TTL to expire. On
        success the inputs are spent, so the lock is left to auto-expire after
        the broadcast propagates. Safe to call when nothing was reserved.
        """
        try:
            self.inner.wallet.release_coinjoin_inputs(set(self.our_utxos.keys()))
        except Exception as e:  # pragma: no cover - best-effort cleanup
            logger.debug(f"Failed to release input locks for {self.taker_nick}: {e}")

    async def abort(self) -> None:
        """Release local resources associated with an unsuccessful session."""
        self.release_input_locks()
        await self.inner._cancel_channel_change()

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
        self,
        commitment: str,
        revelation: dict[str, Any],
        kphex: str,
        exclude_utxos: set[tuple[str, int]] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        return await self.inner.handle_auth(
            commitment, revelation, kphex, exclude_utxos=exclude_utxos
        )

    async def handle_tx(
        self,
        tx_hex: str,
        combine: dict[str, Any] | None = None,
        ring: list[RingEdgeFunding] | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        return await self.inner.handle_tx(tx_hex, combine=combine, ring=ring)

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
            # Record the channel (always accepted; takers may switch
            # direct<->directory mid-session, see validate_channel).
            self.validate_channel(source)

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

            # UTXO selection excludes inputs already committed to other
            # in-flight rounds via persisted, self-expiring locks (see
            # WalletService.reserve_coinjoin_inputs / CoinJoinSession.
            # _select_our_utxos), so the same input is never signed into two
            # concurrent CoinJoins.
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
                self.release_input_locks()
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
            # Record the channel (always accepted; takers may switch
            # direct<->directory mid-session, see validate_channel).
            self.validate_channel(source)

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
                # Optional maker-to-maker channel-as-change suffix appended by the
                # taker: "<tx_b64> cc <partner_pubkey> <partner_change> <shared_addr>".
                # Split it off before decoding the base64 tx (backward compatible: a
                # plain maker just sees the bare tx_b64).
                combine = None
                ring = None
                if " cc " in decrypted:
                    tx_part, _, rest = decrypted.partition(" cc ")
                    fields = rest.split()
                    if len(fields) >= 3:
                        combine = {
                            "partner_pubkey": fields[0],
                            "partner_change": int(fields[1]),
                            "shared_address": fields[2],
                        }
                    decrypted = tx_part
                elif " ring " in decrypted:
                    # True-pooling ring: one compact authorized funding relay per edge.
                    tx_part, _, rest = decrypted.partition(" ring ")
                    ring = [
                        RingEdgeFunding.from_transport(token) for token in rest.split(",") if token
                    ]
                    decrypted = tx_part
                tx_bytes = base64.b64decode(decrypted)
                tx_hex = tx_bytes.hex()
                logger.debug(f"Decoded transaction hex ({len(tx_bytes)} bytes): {tx_hex}")
            except Exception as e:
                logger.error(f"Failed to decode transaction: {e}")
                return

            success, response = await self.handle_tx(tx_hex, combine=combine, ring=ring)

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
                # CoinJoin failed: free our inputs now (don't wait for the TTL).
                self.release_input_locks()
                bot.active_sessions.pop(taker_nick, None)

        except Exception as e:
            logger.error(f"Failed to handle !tx: {e}")

    async def on_chanreq(self, bot: MakerBotProtocol, msg: str, source: str) -> None:
        """Handle a true-pooling ring channel-open request from the taker.

        Format (encrypted): ``1 <edge_id> <authorization_id>``. Peer and allocation
        parameters are loaded from the durable authorization rather than accepted from
        this mutable command. Failures return an empty address for ring fallback.
        """
        address = ""
        response = ""
        try:
            self.validate_channel(source)
            parts = msg.split()
            if len(parts) < 2 or not self.crypto.is_encrypted:
                logger.error("invalid chanreq: missing encrypted payload")
            else:
                decrypted = self.crypto.decrypt(parts[1])
                request_parts = decrypted.split()
                if len(request_parts) != 3 or request_parts[0] != "1":
                    raise ValueError(
                        "chanreq payload must contain version, edge, and authorization"
                    )
                _version, edge_id, authorization_id = request_parts
                store = bot.ring_edge_authorization_store
                if store is None:
                    raise ChannelInputEscrowError("ring edge authorization store is unavailable")
                if self.inner._ring_authorization_ids.get(edge_id) != authorization_id:
                    raise ChannelInputEscrowError("ring edge was not authorized in this session")
                record = store.load(edge_id)
                if record is None or record.authorization.authorization_id != authorization_id:
                    raise ChannelInputEscrowError("ring edge authorization record does not match")
                authorization = record.authorization
                if authorization.role_for(bot.nick) is not RingEdgeRole.FUNDER:
                    raise ChannelInputEscrowError("only the authorized funder may open the edge")
                if authorization.expires_at_unix <= int(time.time()):
                    raise ChannelInputEscrowError("ring edge authorization has expired")
                peer = authorization.receiver_ln_node
                capacity = authorization.capacity_sats
                push_msat = authorization.receiver_contribution_sats * 1000
                logger.info(f"chanreq: opening {capacity} sat authorized ring channel")
                addr = await self.inner.open_external_channel(
                    peer, capacity, push_to_counterparty_msat=push_msat
                )
                address = addr or ""
                if address:
                    from jmcore.bitcoin import address_to_scriptpubkey

                    self.inner._channel_edge_id = edge_id
                    self.inner._channel_authorization_id = authorization_id
                    store.bind_funding(
                        edge_id,
                        authorization_id,
                        address,
                        address_to_scriptpubkey(address).hex(),
                    )
                    response = f"1 {edge_id} {authorization_id} {address}"
                logger.info(f"chanreq: ring channel funding address = {address or '(failed)'}")
        except Exception as e:  # noqa: BLE001 - never block the round; reply empty
            logger.error(f"chanreq handling failed: {e}")
            if self.inner._channel_user_id is not None:
                await self.inner._cancel_channel_change()
        await self.send_response(bot, "chanaddr", {"address": response})

    async def on_chancancel(self, bot: MakerBotProtocol, msg: str, source: str) -> None:
        """Cancel the exact pending ring edge after the taker abandons ring construction."""
        try:
            self.validate_channel(source)
            parts = msg.split()
            if len(parts) < 2 or not self.crypto.is_encrypted:
                raise ChannelInputEscrowError("chancancel requires an encrypted payload")
            request_parts = self.crypto.decrypt(parts[1]).split()
            if len(request_parts) != 3 or request_parts[0] != "1":
                raise ChannelInputEscrowError(
                    "chancancel payload must contain version, edge, and authorization"
                )
            _version, edge_id, authorization_id = request_parts
            if self.inner._ring_authorization_ids.get(edge_id) != authorization_id:
                raise ChannelInputEscrowError("ring cancellation was not authorized")
            if self.inner._channel_edge_id is not None and self.inner._channel_edge_id != edge_id:
                raise ChannelInputEscrowError("ring cancellation targets another pending edge")
            if (
                self.inner._channel_authorization_id is not None
                and self.inner._channel_authorization_id != authorization_id
            ):
                raise ChannelInputEscrowError("ring cancellation authorization does not match")
            store = bot.ring_edge_authorization_store
            record = store.load(edge_id) if store is not None else None
            if record is None or record.authorization.authorization_id != authorization_id:
                raise ChannelInputEscrowError("ring cancellation record does not match")
            if record.authorization.role_for(bot.nick) is not RingEdgeRole.FUNDER:
                raise ChannelInputEscrowError("only the authorized funder can cancel an open")
            await self.inner._cancel_channel_change()
        except Exception as exc:  # noqa: BLE001 - cancellation failures require operator review
            logger.error("chancancel handling failed: {}", exc)

    async def on_chanauth(self, bot: MakerBotProtocol, msg: str, source: str) -> None:
        """Validate, persist, and acknowledge this maker's ring edge authorizations."""
        acknowledgment = ""
        try:
            self.validate_channel(source)
            parts = msg.split()
            if len(parts) < 2 or not self.crypto.is_encrypted:
                raise ChannelInputEscrowError("chanauth requires an encrypted payload")
            if self.state is not CoinJoinState.IOAUTH_SENT:
                raise ChannelInputEscrowError("chanauth arrived outside the authenticated phase")
            store = bot.ring_edge_authorization_store
            if store is None:
                raise ChannelInputEscrowError("ring edge authorization store is unavailable")
            tokens = self.crypto.decrypt(parts[1]).split(",")
            if not 1 <= len(tokens) <= 2 or any(not token for token in tokens):
                raise ChannelInputEscrowError("chanauth must contain one or two ring edges")
            authorizations = [RingEdgeAuthorization.from_transport(token) for token in tokens]
            edge_ids = {authorization.edge_id for authorization in authorizations}
            if len(edge_ids) != len(authorizations):
                raise ChannelInputEscrowError("chanauth contains duplicate ring edges")

            own_session_pubkey = self.crypto.get_pubkey_hex()
            own_ln_node = self.inner.ln_node
            normalized_own_ln_node = None
            if own_ln_node is not None and "@" in own_ln_node:
                node_pubkey, node_endpoint = own_ln_node.split("@", 1)
                normalized_own_ln_node = f"{node_pubkey.lower()}@{node_endpoint}"
            expected_context = self.commitment.hex()
            roles: set[RingEdgeRole] = set()
            for authorization in authorizations:
                role = authorization.role_for(bot.nick)
                roles.add(role)
                local_session_pubkey = (
                    authorization.funder_session_pubkey
                    if role is RingEdgeRole.FUNDER
                    else authorization.receiver_session_pubkey
                )
                local_ln_node = (
                    authorization.funder_ln_node
                    if role is RingEdgeRole.FUNDER
                    else authorization.receiver_ln_node
                )
                if authorization.taker_nick != self.taker_nick:
                    raise ChannelInputEscrowError("ring authorization taker identity mismatch")
                if authorization.taker_session_pubkey != self.inner.taker_nacl_pk.lower():
                    raise ChannelInputEscrowError("ring authorization taker session key mismatch")
                if local_session_pubkey != own_session_pubkey.lower():
                    raise ChannelInputEscrowError("ring authorization maker session key mismatch")
                if normalized_own_ln_node is None or local_ln_node != normalized_own_ln_node:
                    raise ChannelInputEscrowError("ring authorization LN node identity mismatch")
                if authorization.coinjoin_context != expected_context:
                    raise ChannelInputEscrowError("ring authorization CoinJoin context mismatch")
                if authorization.coinjoin_amount_sats != self.amount:
                    raise ChannelInputEscrowError("ring authorization CoinJoin amount mismatch")
                if authorization.expires_at_unix > int(
                    time.time() + self.inner.session_timeout_sec
                ):
                    raise ChannelInputEscrowError(
                        "ring authorization expiry exceeds session lifetime"
                    )
            if len(authorizations) == 2 and roles != {
                RingEdgeRole.FUNDER,
                RingEdgeRole.RECEIVER,
            }:
                raise ChannelInputEscrowError("two-edge authorization must assign both ring roles")

            proposed = {
                authorization.edge_id: authorization.authorization_id
                for authorization in authorizations
            }
            if (
                self.inner._ring_authorization_ids
                and self.inner._ring_authorization_ids != proposed
            ):
                raise ChannelInputEscrowError("ring authorization set is immutable")
            for authorization in authorizations:
                store.authorize(authorization, local_nick=bot.nick)
            self.inner._ring_authorization_ids = proposed
            acknowledgment = ",".join(sorted(proposed.values()))
        except Exception as exc:  # noqa: BLE001 - an empty acknowledgment fails the ring closed
            logger.error("chanauth handling failed: {}", exc)
        await self.send_response(bot, "chanauthack", {"authorization_ids": acknowledgment})

    async def send_response(
        self, bot: MakerBotProtocol, command: str, data: dict[str, Any]
    ) -> None:
        """Send a signed response (`!ioauth`, `!sig` or `chanaddr`) encrypted via this
        session's NaCl box, fanned out to all of the bot's directory clients.

        The `pubkey` response is sent unencrypted via
        :func:`MakerSession.send_pubkey_response` because it doesn't require
        an active session's `crypto` (the response IS the public key).
        """
        try:
            if command == "ioauth":
                fields = [
                    data["utxo_list"],
                    data["auth_pub"],
                    data["cj_addr"],
                    data["change_addr"],
                    data["btc_sig"],
                ]
                # Optional trailing field (backward compatible): the maker-to-maker
                # channel-as-change funding pubkey. Reference takers ignore extra
                # positional fields; ours reads index 5 only when present.
                if data.get("cc_pubkey"):
                    fields.append(data["cc_pubkey"])
                    # 7th field (only alongside cc_pubkey): the maker's Lightning node, so
                    # the taker can assign it a true-pooling ring partner. Reference takers
                    # ignore trailing fields; ours reads index 6 when present.
                    if data.get("ln_node"):
                        fields.append(data["ln_node"])
                plaintext = " ".join(fields)
                msg_content = self.crypto.encrypt(plaintext)
                logger.debug(f"Encrypted ioauth message, plaintext_len={len(plaintext)}")
            elif command == "sig":
                plaintext = data["signature"]
                msg_content = self.crypto.encrypt(plaintext)
                logger.debug(f"Encrypted sig: plaintext_len={len(plaintext)}")
            elif command == "chanaddr":
                msg_content = self.crypto.encrypt(data["address"])
            elif command == "chanauthack":
                msg_content = self.crypto.encrypt(data["authorization_ids"])
            else:
                msg_content = json.dumps(data)

            for client in bot.directory_clients.values():
                await client.send_private_message(self.taker_nick, command, msg_content)

            logger.debug(f"Sent signed {command} to {self.taker_nick}")

        except Exception as e:
            logger.error(f"Failed to send response: {e}")
