"""Encrypted taker transport for channel-input v1 through prepare authorization."""

from __future__ import annotations

import base64
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

from jmcore.channel_input import (
    MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES,
    ChannelInputApprovalPayload,
    ChannelInputApprovalSetAckPayload,
    ChannelInputCancelAckPayload,
    ChannelInputHelloPayload,
    ChannelInputPayload,
    ChannelInputPreparePayload,
    ChannelInputRecoveryAcceptPayload,
    ChannelInputRecoveryConfirmPayload,
    ChannelInputRecoveryInitPayload,
    ChannelInputTranscript,
    EndpointTerms,
    PolicyCeilings,
    decode_channel_input_message,
    encode_channel_input_message,
    payload_hash,
    verify_payload,
)
from jmcore.channel_input_config import ChannelInputConfig
from jmcore.encryption import CryptoSession

from taker.channel_input_negotiation import TakerChannelInputNegotiation, TakerChannelInputStage
from taker.channel_input_runtime import TakerChannelInputRegistry, TakerChannelInputRuntimeError
from taker.multi_directory import ReplyRoute

if TYPE_CHECKING:
    from jmswap.channel_input import InitializedChannelInputBackend

    from taker.coinjoin_session import CoinJoinSession


class TakerChannelInputTransportError(Exception):
    """An encrypted channel-input transport phase could not complete safely."""


class _CryptoSession(Protocol):
    @property
    def is_encrypted(self) -> bool: ...

    def encrypt(self, plaintext: str) -> str: ...

    def decrypt(self, ciphertext: str) -> str: ...


class _MakerSession(Protocol):
    crypto: _CryptoSession | None
    comm_channel: str


class _DirectoryClient(Protocol):
    async def send_privmsg(
        self,
        recipient: str,
        command: str,
        data: str,
        *,
        force_channel: str,
    ) -> str: ...

    async def send_privmsg_via_route(
        self, recipient: str, command: str, data: str, route: ReplyRoute
    ) -> str: ...

    async def wait_for_responses(
        self,
        expected_nicks: list[str],
        expected_command: str,
        timeout: float = 60.0,
        expected_counts: dict[str, int] | None = None,
        include_routes: bool = False,
    ) -> dict[str, dict[str, Any]]: ...


PayloadT = TypeVar("PayloadT", bound=ChannelInputPayload)
_MAX_RECOVERY_TOKEN_BYTES = 4 * ((MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES + 2) // 3)


@dataclass(frozen=True)
class TakerChannelInputPrepareResult:
    """Durable prepare authorization and ephemeral authenticated reply routes."""

    prepare: ChannelInputPreparePayload
    reply_routes: tuple[tuple[str, ReplyRoute], ...]


@dataclass(frozen=True)
class _RecoverySession:
    recovery_id: str
    crypto: CryptoSession


class TakerChannelInputTransport:
    """Drive a selected live maker roster through encrypted pre-prepare phases."""

    def __init__(
        self,
        *,
        session: CoinJoinSession,
        registry: TakerChannelInputRegistry,
        initialized_backend: InitializedChannelInputBackend,
        config: ChannelInputConfig,
        settlement_script_allocator: Callable[[str], str],
        current_time: int | Callable[[], int],
        current_height: int | Callable[[], int],
    ) -> None:
        self._session = session
        self._registry = registry
        self._initialized_backend = initialized_backend
        self._config = config
        self._settlement_script_allocator = settlement_script_allocator
        self._current_time = current_time
        self._current_height = current_height
        self._routes: dict[str, ReplyRoute] = {}
        self._recovery: dict[str, _RecoverySession] = {}

    @property
    def latest_reply_routes(self) -> tuple[tuple[str, ReplyRoute], ...]:
        """Return the current process-local authenticated ingress routes."""

        return tuple(sorted(self._routes.items()))

    async def exchange_hellos(self, session_id: str, attempt: int) -> None:
        """Replay invite, accept maker hellos, freeze locally, and reply on exact ingress."""

        negotiation = self._require_live_negotiation(session_id, attempt)
        try:
            await self._send_to_all(negotiation.selected_nicks, negotiation.invite)
            received = await self._receive_all(
                session_id,
                attempt,
                ChannelInputHelloPayload,
                self._accept_remote_hello,
            )
            for nick, _ in received:
                local_hello = dict(negotiation.local_hellos).get(nick)
                if local_hello is None:
                    raise TakerChannelInputTransportError(
                        "maker hello did not produce a local hello"
                    )
                await self._send(nick, local_hello, route=self._routes[nick])
        except Exception:
            await self._cancel_after_failure(session_id, attempt)
            raise

    async def build_prepare(
        self,
        session_id: str,
        attempt: int,
        transcript: ChannelInputTranscript,
        endpoint_terms: Sequence[EndpointTerms],
    ) -> TakerChannelInputPrepareResult:
        """Exchange all pre-prepare payloads and return, but never send, prepare."""

        negotiation = self._require_live_negotiation(session_id, attempt)
        try:
            proposals = await self._registry.plan_proposals(
                session_id,
                attempt,
                transcript,
                endpoint_terms,
                current_time=self._value(self._current_time, "current_time"),
                current_height=self._value(self._current_height, "current_height"),
            )
            await self._send_mapping(negotiation.selected_nicks, dict(proposals))
            await self._receive_all(
                session_id,
                attempt,
                ChannelInputApprovalPayload,
                self._accept_approval,
            )
            approval_set = await self._registry.build_approval_set(session_id, attempt)
            await self._send_to_all(
                negotiation.selected_nicks, approval_set, use_latest_routes=True
            )
            await self._receive_all(
                session_id,
                attempt,
                ChannelInputApprovalSetAckPayload,
                self._accept_approval_ack,
            )
            prepare = await self._registry.build_prepare(session_id, attempt)
            return TakerChannelInputPrepareResult(prepare, self.latest_reply_routes)
        except Exception:
            await self._cancel_after_failure(session_id, attempt)
            raise

    async def cancel_or_replay(
        self, session_id: str, attempt: int, reason_code: str = "transport_failure"
    ) -> bool:
        """Replay cancellation to the full roster and report durable completion."""

        negotiation = self._require_live_negotiation(session_id, attempt, allow_canceled=True)
        if negotiation.stage is TakerChannelInputStage.CANCELED:
            return True
        cancel = negotiation.cancel
        if cancel is None:
            cancel = await self._registry.plan_cancellation(session_id, attempt, reason_code)
        await self._send_cancel_to_all(negotiation.selected_nicks, cancel)
        try:
            await self._receive_all(
                session_id,
                attempt,
                ChannelInputCancelAckPayload,
                self._accept_cancel_ack,
            )
        except TakerChannelInputTransportError:
            pass
        try:
            await self._registry.attempt_local_cancellation_completion(
                session_id, attempt, self._initialized_backend.backend
            )
        except TakerChannelInputRuntimeError:
            return False
        updated = self._registry.get(session_id, attempt)
        return updated is not None and updated.stage is TakerChannelInputStage.CANCELED

    async def exchange_one(
        self,
        session_id: str,
        attempt: int,
        nick: str,
        payload: ChannelInputPayload,
        expected_type: type[PayloadT],
    ) -> PayloadT:
        """Send one encrypted post-prepare request and accept its exact maker response."""

        negotiation = self._require_negotiation(session_id, attempt)
        if nick not in negotiation.selected_nicks:
            raise TakerChannelInputTransportError("maker is not selected for channel-input attempt")
        if payload.session_id != session_id or payload.attempt != attempt:
            raise TakerChannelInputTransportError(
                "channel-input request belongs to another attempt"
            )
        try:
            await self._send(nick, payload, route=self._routes.get(nick))
            responses = await self._directory_client().wait_for_responses(
                expected_nicks=[nick],
                expected_command=(
                    "!channel_input_recover" if nick in self._recovery else "!channel_input"
                ),
                timeout=self._config.phase_timeout_seconds,
                include_routes=True,
            )
            response = responses.get(nick)
            if response is None:
                raise TakerChannelInputTransportError("maker did not respond")
            route = response.get("route")
            if not isinstance(route, ReplyRoute) or route.nick != nick:
                raise TakerChannelInputTransportError(
                    "channel-input response has no valid reply route"
                )
            self._routes[nick] = route
            if response.get("error"):
                raise TakerChannelInputTransportError("maker returned a channel-input error")
            return self._decode_response(
                nick, response.get("data"), expected_type, session_id, attempt
            )
        except TakerChannelInputTransportError:
            raise
        except Exception as exc:
            raise TakerChannelInputTransportError("maker channel-input exchange failed") from exc

    async def establish_recovery(
        self,
        session_id: str,
        attempt: int,
        nick: str,
        route: ReplyRoute | None = None,
    ) -> ReplyRoute:
        """Establish fresh recovery crypto for one durable maker participant."""

        negotiation = self._require_negotiation(session_id, attempt)
        if nick not in negotiation.selected_nicks:
            raise TakerChannelInputTransportError("maker is not selected for channel-input attempt")
        reply_route = route or self._routes.get(nick)
        if reply_route is None or reply_route.nick != nick:
            raise TakerChannelInputTransportError(
                "recovery requires a fresh authenticated reply route"
            )
        crypto = CryptoSession()
        recovery_id = secrets.token_hex(32)
        expires_at = int(time.time()) + int(self._config.phase_timeout_seconds)
        init = negotiation.sign_recovery_init(
            ChannelInputRecoveryInitPayload(
                session_id=session_id,
                attempt=attempt,
                signer_key=negotiation.coordinator_key,
                recovery_id=recovery_id,
                ephemeral_pubkey=crypto.get_pubkey_hex(),
                expires_at=expires_at,
            )
        )
        assert isinstance(init, ChannelInputRecoveryInitPayload)
        await self._send_recovery_plain(nick, self._encode_recovery_token(init), reply_route)
        response, accepted_route = await self._wait_recovery_response(nick)
        accept = self._decode_recovery_response_token(response)
        if not isinstance(accept, ChannelInputRecoveryAcceptPayload):
            raise TakerChannelInputTransportError("maker did not return a recovery accept")
        if (
            accept.session_id != session_id
            or accept.attempt != attempt
            or accept.recovery_id != recovery_id
            or accept.init_hash != payload_hash(init).hex()
        ):
            raise TakerChannelInputTransportError("recovery accept does not bind the recovery init")
        try:
            negotiation.require_recovery_accept(nick, accept)
            crypto.setup_encryption(accept.ephemeral_pubkey)
        except Exception as exc:
            raise TakerChannelInputTransportError(
                "maker recovery accept is not authenticated"
            ) from exc
        confirm = negotiation.sign_recovery_confirm(
            ChannelInputRecoveryConfirmPayload(
                session_id=session_id,
                attempt=attempt,
                signer_key=negotiation.coordinator_key,
                recovery_id=recovery_id,
                accept_hash=payload_hash(accept).hex(),
            )
        )
        assert isinstance(confirm, ChannelInputRecoveryConfirmPayload)
        await self._send_recovery_encrypted(
            nick,
            recovery_id,
            crypto.encrypt(encode_channel_input_message(confirm)),
            accepted_route,
        )
        response, confirmed_route = await self._wait_recovery_response(nick)
        confirmed = self._decode_recovery_encrypted(response, recovery_id, crypto)
        if confirmed != accept:
            raise TakerChannelInputTransportError("maker recovery confirmation differs from accept")
        self._recovery[nick] = _RecoverySession(recovery_id, crypto)
        self._routes[nick] = confirmed_route
        return confirmed_route

    async def _cancel_after_failure(self, session_id: str, attempt: int) -> None:
        """Keep the flow exception authoritative while making best-effort cancellation progress."""

        try:
            await self.cancel_or_replay(session_id, attempt)
        except Exception:
            pass

    async def _accept_remote_hello(self, nick: str, payload: ChannelInputHelloPayload) -> None:
        negotiation = self._registry.get(payload.session_id, payload.attempt)
        if negotiation is None:
            raise TakerChannelInputTransportError("unknown channel-input session attempt")
        existing_intent = dict(negotiation.freeze_intents).get(nick)
        if existing_intent is None:
            script = self._settlement_script_allocator(nick)
            self._validate_settlement_script(script)
        else:
            script = existing_intent.settlement_script_pubkey
        await self._registry.accept_remote_hello(
            payload.session_id,
            payload.attempt,
            nick,
            payload,
            backend=self._initialized_backend.backend,
            local_node_key=self._initialized_backend.node_info.identity_pubkey,
            candidates=self._initialized_backend.candidates,
            settlement_script_pubkey=script,
            quoted_miner_fee_share_sat=min(
                self._config.max_miner_fee_share_sat,
                negotiation.invite.max_parent_fee_sat,
            ),
            policy_ceilings=PolicyCeilings(
                max_miner_fee_share_sat=self._config.max_miner_fee_share_sat,
                max_claim_adjustment_sat=self._config.max_claim_adjustment_sat,
                max_settlement_outputs=1,
            ),
        )

    async def _accept_approval(self, nick: str, payload: ChannelInputApprovalPayload) -> None:
        await self._registry.accept_maker_approval(
            payload.session_id, payload.attempt, nick, payload
        )

    async def _accept_approval_ack(
        self, nick: str, payload: ChannelInputApprovalSetAckPayload
    ) -> None:
        await self._registry.accept_maker_approval_ack(
            payload.session_id, payload.attempt, nick, payload
        )

    async def _accept_cancel_ack(self, nick: str, payload: ChannelInputCancelAckPayload) -> None:
        await self._registry.accept_maker_cancel_ack(
            payload.session_id, payload.attempt, nick, payload
        )

    async def _send_mapping(
        self, selected_nicks: Sequence[str], payloads: Mapping[str, ChannelInputPayload]
    ) -> None:
        self._require_payload_roster(selected_nicks, payloads)
        for nick, payload in payloads.items():
            await self._send(nick, payload, route=self._routes.get(nick))

    async def _send_to_all(
        self,
        selected_nicks: Sequence[str],
        payload: ChannelInputPayload,
        *,
        use_latest_routes: bool = False,
    ) -> None:
        for nick in selected_nicks:
            route = self._routes.get(nick) if use_latest_routes else None
            await self._send(nick, payload, route=route)

    async def _send_cancel_to_all(
        self, selected_nicks: Sequence[str], cancel: ChannelInputPayload
    ) -> None:
        """Attempt every selected recipient even when one cancellation route has failed."""

        for nick in selected_nicks:
            try:
                await self._send(nick, cancel, route=self._routes.get(nick))
            except Exception:
                continue

    async def _send(
        self, nick: str, payload: ChannelInputPayload, *, route: ReplyRoute | None = None
    ) -> None:
        recovery = self._recovery.get(nick)
        crypto = self._crypto_for(nick)
        encrypted = crypto.encrypt(encode_channel_input_message(payload))
        if len(encrypted) > MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES:
            raise TakerChannelInputTransportError(
                "encrypted channel-input payload exceeds size limit"
            )
        client = self._directory_client()
        command = "channel_input_recover" if recovery is not None else "channel_input"
        data = f"{recovery.recovery_id} {encrypted}" if recovery is not None else encrypted
        if route is not None:
            await client.send_privmsg_via_route(nick, command, data, route)
            return
        maker = self._maker_sessions().get(nick)
        if maker is None:
            raise TakerChannelInputTransportError("recovered maker has no reply route")
        await client.send_privmsg(nick, command, data, force_channel=maker.comm_channel)

    async def _receive_all(
        self,
        session_id: str,
        attempt: int,
        expected_type: type[PayloadT],
        accept: Callable[[str, PayloadT], Any],
    ) -> tuple[tuple[str, PayloadT], ...]:
        responses = await self._directory_client().wait_for_responses(
            expected_nicks=list(self._selected_nicks_for(session_id, attempt)),
            expected_command="!channel_input",
            timeout=self._config.phase_timeout_seconds,
            include_routes=True,
        )
        accepted: list[tuple[str, PayloadT]] = []
        first_error: TakerChannelInputTransportError | None = None
        for nick in self._selected_nicks_for(session_id, attempt):
            response = responses.get(nick)
            if response is None:
                if first_error is None:
                    first_error = TakerChannelInputTransportError("maker did not respond")
                continue
            try:
                route = response.get("route")
                if not isinstance(route, ReplyRoute) or route.nick != nick:
                    raise TakerChannelInputTransportError(
                        "channel-input response has no valid reply route"
                    )
                self._routes[nick] = route
                if response.get("error"):
                    raise TakerChannelInputTransportError("maker returned a channel-input error")
                payload = self._decode_response(
                    nick, response.get("data"), expected_type, session_id, attempt
                )
                result = accept(nick, payload)
                if isawaitable(result):
                    await result
                accepted.append((nick, payload))
            except Exception as exc:
                if first_error is None:
                    first_error = (
                        TakerChannelInputTransportError(str(exc))
                        if isinstance(
                            exc, (TakerChannelInputRuntimeError, TakerChannelInputTransportError)
                        )
                        else TakerChannelInputTransportError(
                            "maker returned an invalid channel-input response"
                        )
                    )
        if first_error is not None:
            raise first_error
        return tuple(accepted)

    def _decode_response(
        self,
        nick: str,
        data: object,
        expected_type: type[PayloadT],
        session_id: str,
        attempt: int,
    ) -> PayloadT:
        if not isinstance(data, str):
            raise TakerChannelInputTransportError("channel-input response is malformed")
        tokens = data.split()
        if not tokens or len(tokens[0]) > MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES:
            raise TakerChannelInputTransportError(
                "encrypted channel-input response exceeds size limit"
            )
        recovery = self._recovery.get(nick)
        if recovery is not None:
            payload = self._decode_recovery_encrypted(data, recovery.recovery_id, recovery.crypto)
        else:
            payload = decode_channel_input_message(self._crypto_for(nick).decrypt(tokens[0]))
        if not isinstance(payload, expected_type):
            raise TakerChannelInputTransportError(
                "maker returned an unexpected channel-input payload"
            )
        if payload.session_id != session_id or payload.attempt != attempt:
            raise TakerChannelInputTransportError(
                "maker response belongs to another channel-input attempt"
            )
        if not verify_payload(payload):
            raise TakerChannelInputTransportError("maker response has an invalid BIP340 signature")
        return payload

    def _require_live_negotiation(
        self, session_id: str, attempt: int, *, allow_canceled: bool = False
    ) -> TakerChannelInputNegotiation:
        negotiation = self._require_negotiation(session_id, attempt, allow_canceled=allow_canceled)
        if set(negotiation.selected_nicks) != set(self._maker_sessions()):
            raise TakerChannelInputTransportError(
                "negotiation roster differs from live maker roster"
            )
        for nick in negotiation.selected_nicks:
            self._crypto_for(nick)
        return negotiation

    def _require_negotiation(
        self, session_id: str, attempt: int, *, allow_canceled: bool = False
    ) -> TakerChannelInputNegotiation:
        negotiation = self._registry.get(session_id, attempt)
        if negotiation is None:
            raise TakerChannelInputTransportError("unknown channel-input session attempt")
        if not allow_canceled and negotiation.stage is TakerChannelInputStage.CANCELED:
            raise TakerChannelInputTransportError("channel-input negotiation is canceled")
        return negotiation

    def _selected_nicks_for(self, session_id: str, attempt: int) -> tuple[str, ...]:
        return self._require_live_negotiation(session_id, attempt).selected_nicks

    def _require_payload_roster(
        self, selected_nicks: Sequence[str], payloads: Mapping[str, ChannelInputPayload]
    ) -> None:
        if set(payloads) != set(selected_nicks):
            raise TakerChannelInputTransportError("channel-input payloads do not cover live roster")

    def _crypto_for(self, nick: str) -> _CryptoSession:
        recovered = self._recovery.get(nick)
        if recovered is not None:
            return recovered.crypto
        maker = self._maker_sessions().get(nick)
        crypto = None if maker is None else maker.crypto
        if crypto is None or not crypto.is_encrypted:
            raise TakerChannelInputTransportError("maker has no authenticated encryption session")
        return crypto

    def _maker_sessions(self) -> Mapping[str, _MakerSession]:
        return cast(Mapping[str, _MakerSession], self._session.maker_sessions)

    def _directory_client(self) -> _DirectoryClient:
        return cast(_DirectoryClient, self._session.directory_client)

    async def _send_recovery_plain(self, nick: str, token: str, route: ReplyRoute) -> None:
        await self._directory_client().send_privmsg_via_route(
            nick, "channel_input_recover", token, route
        )

    async def _send_recovery_encrypted(
        self, nick: str, recovery_id: str, ciphertext: str, route: ReplyRoute
    ) -> None:
        if len(ciphertext) > MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES:
            raise TakerChannelInputTransportError("recovery ciphertext exceeds size limit")
        await self._directory_client().send_privmsg_via_route(
            nick, "channel_input_recover", f"{recovery_id} {ciphertext}", route
        )

    async def _wait_recovery_response(self, nick: str) -> tuple[str, ReplyRoute]:
        responses = await self._directory_client().wait_for_responses(
            expected_nicks=[nick],
            expected_command="!channel_input_recover",
            timeout=self._config.phase_timeout_seconds,
            include_routes=True,
        )
        response = responses.get(nick)
        if response is None or response.get("error"):
            raise TakerChannelInputTransportError("maker did not return a recovery response")
        route = response.get("route")
        data = response.get("data")
        if not isinstance(route, ReplyRoute) or route.nick != nick or not isinstance(data, str):
            raise TakerChannelInputTransportError("recovery response has no valid reply route")
        self._routes[nick] = route
        return data, route

    @staticmethod
    def _encode_recovery_token(payload: ChannelInputPayload) -> str:
        encoded = encode_channel_input_message(payload).encode("ascii")
        return base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode_recovery_token(data: str) -> ChannelInputPayload:
        token = data if len(data.split()) == 1 else ""
        if not token or len(token) > _MAX_RECOVERY_TOKEN_BYTES or "=" in token:
            raise TakerChannelInputTransportError("recovery token is malformed")
        try:
            raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != token:
                raise ValueError("non-canonical token")
            return decode_channel_input_message(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TakerChannelInputTransportError("recovery token is malformed") from exc

    @staticmethod
    def _decode_recovery_response_token(data: str) -> ChannelInputPayload:
        tokens = data.split()
        if len(tokens) != 3:
            raise TakerChannelInputTransportError("recovery response is malformed")
        return TakerChannelInputTransport._decode_recovery_token(tokens[0])

    @staticmethod
    def _decode_recovery_encrypted(
        data: object, recovery_id: str, crypto: _CryptoSession
    ) -> ChannelInputPayload:
        if not isinstance(data, str):
            raise TakerChannelInputTransportError("recovery response is malformed")
        tokens = data.split()
        if (
            len(tokens) != 4
            or tokens[0] != recovery_id
            or len(tokens[1]) > MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES
        ):
            raise TakerChannelInputTransportError("recovery response does not match the active ID")
        try:
            return decode_channel_input_message(crypto.decrypt(tokens[1]))
        except Exception as exc:
            raise TakerChannelInputTransportError("recovery response is not decryptable") from exc

    @staticmethod
    def _value(value: int | Callable[[], int], label: str) -> int:
        result = value() if callable(value) else value
        if not isinstance(result, int):
            raise TakerChannelInputTransportError(f"{label} must be an integer")
        return result

    @staticmethod
    def _validate_settlement_script(script: str) -> None:
        if not isinstance(script, str) or len(script) != 68 or not script.startswith("5120"):
            raise TakerChannelInputTransportError(
                "settlement allocator did not return a P2TR script"
            )
        try:
            bytes.fromhex(script)
        except ValueError as exc:
            raise TakerChannelInputTransportError(
                "settlement allocator did not return a P2TR script"
            ) from exc


TakerChannelInputTransportCoordinator = TakerChannelInputTransport
