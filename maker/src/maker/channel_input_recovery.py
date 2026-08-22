"""Fresh authenticated transport recovery for durable channel-input attempts."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jmcore.channel_input import (
    MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES,
    MAX_CHANNEL_INPUT_ENCODED_BYTES,
    ChannelInputInvitePayload,
    ChannelInputPayload,
    ChannelInputRecoveryAcceptPayload,
    ChannelInputRecoveryConfirmPayload,
    ChannelInputRecoveryInitPayload,
    ChannelInputValidationError,
    decode_channel_input_message,
    encode_channel_input_message,
    payload_hash,
)
from jmcore.crypto import verify_signed_privmsg
from jmcore.encryption import CryptoSession
from jmcore.network import ONION_HOSTID, TCPConnection
from jmcore.protocol import MessageType, format_jm_message
from loguru import logger

from maker.channel_input import MakerChannelInputStage

if TYPE_CHECKING:
    from maker.protocols import MakerBotProtocol


_MAX_RECOVERY_TOKEN_BYTES = 4 * ((MAX_CHANNEL_INPUT_ENCODED_BYTES + 2) // 3)


@dataclass(frozen=True, slots=True)
class _PendingRecovery:
    session_id: str
    attempt: int
    init: ChannelInputRecoveryInitPayload
    accept: ChannelInputRecoveryAcceptPayload
    crypto: CryptoSession


@dataclass(frozen=True, slots=True)
class _ActiveRecovery:
    session_id: str
    attempt: int
    init: ChannelInputRecoveryInitPayload
    accept: ChannelInputRecoveryAcceptPayload
    crypto: CryptoSession


class MakerChannelInputRecoveryTransport:
    """Own bounded, non-persistent recovery encryption contexts for one maker."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], _PendingRecovery] = {}
        self._active: dict[tuple[str, str], _ActiveRecovery] = {}

    async def handle(
        self,
        bot: MakerBotProtocol,
        taker_nick: str,
        msg: str,
        source: str,
        direct_connection: TCPConnection | None = None,
    ) -> None:
        """Handle one signed recovery control or encrypted durable payload."""

        try:
            self._require_runtime(bot, source, direct_connection)
            parts = msg.split()
            if len(parts) not in {4, 5}:
                raise ChannelInputValidationError("malformed channel-input recovery envelope")
            verified, command, data = verify_signed_privmsg(taker_nick, msg, ONION_HOSTID)
            if not verified or command != "channel_input_recover":
                raise ChannelInputValidationError("invalid channel-input recovery envelope")
            self._prune(bot)
            if len(parts) == 4:
                if data != parts[1]:
                    raise ChannelInputValidationError("invalid recovery init data")
                accept = self._accept_init(bot, taker_nick, self._decode_token(data))
                await self._send_plain(bot, taker_nick, accept, source, direct_connection)
                return
            recovery_id, ciphertext = parts[1:3]
            if (
                data != f"{recovery_id} {ciphertext}"
                or not self._is_recovery_id(recovery_id)
                or len(ciphertext) > MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES
            ):
                raise ChannelInputValidationError("invalid recovery encrypted data")
            active = self._active.get((taker_nick, recovery_id))
            if active is None:
                active = self._confirm(bot, taker_nick, recovery_id, ciphertext)
                confirmation = active.crypto.encrypt(encode_channel_input_message(active.accept))
                await self._send_encrypted(
                    bot, taker_nick, recovery_id, confirmation, source, direct_connection
                )
                return
            await self._handle_active(
                bot, taker_nick, recovery_id, ciphertext, active, source, direct_connection
            )
        except Exception as exc:
            logger.warning(
                f"Rejected invalid channel-input recovery from {taker_nick} ({type(exc).__name__})"
            )

    def _accept_init(
        self, bot: MakerBotProtocol, taker_nick: str, payload: ChannelInputPayload
    ) -> ChannelInputRecoveryAcceptPayload:
        if not isinstance(payload, ChannelInputRecoveryInitPayload):
            raise ChannelInputValidationError("recovery control payload must be an init")
        self._validate_init_expiry(bot, payload)
        registry = bot._channel_input_registry
        assert registry is not None
        negotiation = registry.get(payload.session_id, payload.attempt)
        if negotiation is None or negotiation.stage is MakerChannelInputStage.CANCELED:
            raise ChannelInputValidationError("unknown or canceled channel-input recovery attempt")
        if negotiation.coordinator_actor != taker_nick:
            raise ChannelInputValidationError("recovery outer actor differs from coordinator")
        negotiation.require_recovery_init(taker_nick, payload)
        key = (taker_nick, payload.recovery_id)
        existing = self._pending.get(key)
        if existing is not None:
            if existing.init != payload:
                raise ChannelInputValidationError("recovery ID conflicts with pending init")
            return existing.accept
        active = self._active.get(key)
        if active is not None:
            if active.init != payload:
                raise ChannelInputValidationError("recovery ID conflicts with active init")
            return active.accept
        attempt_key = (taker_nick, payload.session_id, payload.attempt)
        self._ensure_capacity(bot, replacing=attempt_key)
        crypto = CryptoSession()
        accept = negotiation.sign_recovery_accept(
            ChannelInputRecoveryAcceptPayload(
                session_id=payload.session_id,
                attempt=payload.attempt,
                signer_key=negotiation.participant_key,
                recovery_id=payload.recovery_id,
                init_hash=payload_hash(payload).hex(),
                ephemeral_pubkey=crypto.get_pubkey_hex(),
            )
        )
        assert isinstance(accept, ChannelInputRecoveryAcceptPayload)
        crypto.setup_encryption(payload.ephemeral_pubkey)
        self._drop_attempt_contexts(*attempt_key)
        self._pending[key] = _PendingRecovery(
            payload.session_id, payload.attempt, payload, accept, crypto
        )
        return accept

    def _confirm(
        self, bot: MakerBotProtocol, taker_nick: str, recovery_id: str, ciphertext: str
    ) -> _ActiveRecovery:
        pending = self._pending.get((taker_nick, recovery_id))
        if pending is None:
            raise ChannelInputValidationError("unknown channel-input recovery ID")
        payload = decode_channel_input_message(pending.crypto.decrypt(ciphertext))
        if not isinstance(payload, ChannelInputRecoveryConfirmPayload):
            raise ChannelInputValidationError("recovery encrypted payload must be a confirm")
        if (
            payload.session_id != pending.session_id
            or payload.attempt != pending.attempt
            or payload.recovery_id != recovery_id
            or payload.accept_hash != payload_hash(pending.accept).hex()
        ):
            raise ChannelInputValidationError("recovery confirm does not bind pending accept")
        registry = bot._channel_input_registry
        assert registry is not None
        negotiation = registry.get(pending.session_id, pending.attempt)
        if (
            negotiation is None
            or negotiation.stage is MakerChannelInputStage.CANCELED
            or negotiation.coordinator_actor != taker_nick
        ):
            raise ChannelInputValidationError("recovery attempt is no longer authorized")
        negotiation.require_recovery_init(taker_nick, payload)
        active = _ActiveRecovery(
            pending.session_id, pending.attempt, pending.init, pending.accept, pending.crypto
        )
        self._pending.pop((taker_nick, recovery_id))
        self._active[(taker_nick, recovery_id)] = active
        return active

    async def _handle_active(
        self,
        bot: MakerBotProtocol,
        taker_nick: str,
        recovery_id: str,
        ciphertext: str,
        active: _ActiveRecovery,
        source: str,
        direct_connection: TCPConnection | None,
    ) -> None:
        payload = decode_channel_input_message(active.crypto.decrypt(ciphertext))
        if isinstance(payload, ChannelInputRecoveryConfirmPayload):
            if (
                payload.session_id != active.session_id
                or payload.attempt != active.attempt
                or payload.recovery_id != recovery_id
                or payload.accept_hash != payload_hash(active.accept).hex()
            ):
                raise ChannelInputValidationError("recovery confirm conflicts with active accept")
            registry = bot._channel_input_registry
            assert registry is not None
            negotiation = registry.get(active.session_id, active.attempt)
            if (
                negotiation is None
                or negotiation.stage is MakerChannelInputStage.CANCELED
                or negotiation.coordinator_actor != taker_nick
            ):
                raise ChannelInputValidationError("recovery attempt is no longer authorized")
            negotiation.require_recovery_init(taker_nick, payload)
            await self._send_encrypted(
                bot,
                taker_nick,
                recovery_id,
                active.crypto.encrypt(encode_channel_input_message(active.accept)),
                source,
                direct_connection,
            )
            return
        if isinstance(
            payload,
            (
                ChannelInputRecoveryInitPayload,
                ChannelInputRecoveryAcceptPayload,
                ChannelInputInvitePayload,
            ),
        ):
            raise ChannelInputValidationError("recovery transport does not admit this payload")
        if payload.session_id != active.session_id or payload.attempt != active.attempt:
            raise ChannelInputValidationError("recovered payload belongs to another attempt")
        registry = bot._channel_input_registry
        backend = bot._channel_input_backend
        assert registry is not None and backend is not None
        negotiation = registry.get(active.session_id, active.attempt)
        if (
            negotiation is None
            or negotiation.stage is MakerChannelInputStage.CANCELED
            or negotiation.coordinator_actor != taker_nick
        ):
            raise ChannelInputValidationError("recovery attempt is no longer authorized")
        negotiation.require_recovery_init(taker_nick, payload)
        responses = await registry.handle_payload(
            payload,
            coordinator_actor=taker_nick,
            local_actor=bot.nick,
            local_node_key=backend.node_info.identity_pubkey,
            backend=backend.backend,
            candidates=backend.candidates,
            settlement_script_for=lambda _: (_ for _ in ()).throw(
                ChannelInputValidationError("recovery cannot allocate settlement scripts")
            ),
            current_time=int(time.time()),
            current_height=bot.current_block_height,
        )
        for response in responses:
            await self._send_encrypted(
                bot,
                taker_nick,
                recovery_id,
                active.crypto.encrypt(encode_channel_input_message(response)),
                source,
                direct_connection,
            )

    def _prune(self, bot: MakerBotProtocol) -> None:
        now = int(time.time())
        registry = bot._channel_input_registry
        assert registry is not None
        for key, pending in tuple(self._pending.items()):
            negotiation = registry.get(pending.session_id, pending.attempt)
            if (
                pending.init.expires_at <= now
                or negotiation is None
                or negotiation.stage is MakerChannelInputStage.CANCELED
            ):
                self._pending.pop(key)
        for key, active in tuple(self._active.items()):
            negotiation = registry.get(active.session_id, active.attempt)
            if negotiation is None or negotiation.stage is MakerChannelInputStage.CANCELED:
                self._active.pop(key)

    def _validate_init_expiry(
        self, bot: MakerBotProtocol, payload: ChannelInputRecoveryInitPayload
    ) -> None:
        now = int(time.time())
        if payload.expires_at <= now or payload.expires_at > now + int(
            bot.config.channel_input.phase_timeout_seconds
        ):
            raise ChannelInputValidationError("recovery init expiry is outside the allowed window")

    def _ensure_capacity(self, bot: MakerBotProtocol, *, replacing: tuple[str, str, int]) -> None:
        active_keys = {
            (actor, value.session_id, value.attempt) for (actor, _), value in self._active.items()
        }
        pending_keys = {
            (actor, value.session_id, value.attempt) for (actor, _), value in self._pending.items()
        }
        total = len(active_keys | pending_keys)
        if (
            replacing not in active_keys | pending_keys
            and total >= bot.config.channel_input.max_active_sessions
        ):
            raise ChannelInputValidationError("maximum recovery sessions reached")

    def _drop_attempt_contexts(self, actor: str, session_id: str, attempt: int) -> None:
        for key, pending in tuple(self._pending.items()):
            if key[0] == actor and pending.session_id == session_id and pending.attempt == attempt:
                self._pending.pop(key)
        for key, active in tuple(self._active.items()):
            if key[0] == actor and active.session_id == session_id and active.attempt == attempt:
                self._active.pop(key)

    @staticmethod
    def _is_recovery_id(value: str) -> bool:
        if len(value) != 64 or value.lower() != value:
            return False
        try:
            bytes.fromhex(value)
        except ValueError:
            return False
        return True

    def _require_runtime(
        self,
        bot: MakerBotProtocol,
        source: str,
        direct_connection: TCPConnection | None,
    ) -> None:
        if (
            not bot.config.channel_input.enabled
            or bot._channel_input_backend is None
            or bot._channel_input_registry is None
        ):
            raise ChannelInputValidationError("channel-input recovery is unavailable")
        if source == "direct" and direct_connection is None:
            raise ChannelInputValidationError("direct recovery route is unavailable")
        if source.startswith("dir:") and source[4:] not in bot.directory_clients:
            raise ChannelInputValidationError("directory recovery route is unavailable")
        if source != "direct" and not source.startswith("dir:"):
            raise ChannelInputValidationError("recovery route is unavailable")

    async def _send_plain(
        self,
        bot: MakerBotProtocol,
        taker_nick: str,
        payload: ChannelInputRecoveryAcceptPayload,
        source: str,
        direct_connection: TCPConnection | None,
    ) -> None:
        await self._send(bot, taker_nick, self._encode_token(payload), source, direct_connection)

    async def _send_encrypted(
        self,
        bot: MakerBotProtocol,
        taker_nick: str,
        recovery_id: str,
        ciphertext: str,
        source: str,
        direct_connection: TCPConnection | None,
    ) -> None:
        await self._send(bot, taker_nick, f"{recovery_id} {ciphertext}", source, direct_connection)

    async def _send(
        self,
        bot: MakerBotProtocol,
        taker_nick: str,
        data: str,
        source: str,
        direct_connection: TCPConnection | None,
    ) -> None:
        if source == "direct":
            assert direct_connection is not None
            signed = bot.nick_identity.sign_message(data, ONION_HOSTID)
            line = format_jm_message(bot.nick, taker_nick, "channel_input_recover", signed)
            await direct_connection.send(
                json.dumps({"type": MessageType.PRIVMSG.value, "line": line}).encode("utf-8")
            )
            return
        client = bot.directory_clients[source[4:]]
        await client.send_private_message(taker_nick, "channel_input_recover", data)

    @staticmethod
    def _encode_token(payload: ChannelInputPayload) -> str:
        encoded = encode_channel_input_message(payload).encode("ascii")
        return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode_token(token: str) -> ChannelInputPayload:
        if not token or len(token) > _MAX_RECOVERY_TOKEN_BYTES or "=" in token:
            raise ChannelInputValidationError("recovery token exceeds size limit")
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
                raise ValueError("non-canonical token")
            return decode_channel_input_message(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ChannelInputValidationError("invalid recovery token") from exc
