"""Authenticated maker recovery transport tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from coincurve import PrivateKey
from jmcore.channel_input import (
    ChannelInputCancelPayload,
    ChannelInputInvitePayload,
    ChannelInputKeyPair,
    ChannelInputRecoveryAcceptPayload,
    ChannelInputRecoveryConfirmPayload,
    ChannelInputRecoveryInitPayload,
    decode_channel_input_message,
    encode_channel_input_message,
    payload_hash,
    sign_payload,
    verify_payload,
)
from jmcore.channel_input_config import ChannelInputConfig
from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.encryption import CryptoSession
from jmcore.network import ONION_HOSTID, TCPConnection
from jmcore.protocol import JM_VERSION, MessageType

from maker.channel_input import MakerChannelInputNegotiation, MakerChannelInputStage
from maker.channel_input_recovery import MakerChannelInputRecoveryTransport

_COORDINATOR_SECRET = (1).to_bytes(32, "big")
_PARTICIPANT_SECRET = (2).to_bytes(32, "big")
_SESSION_ID = "11" * 32


class RecoveryRegistry:
    def __init__(self, *negotiations: MakerChannelInputNegotiation) -> None:
        self._negotiations = {
            (negotiation.invite.session_id, negotiation.invite.attempt): negotiation
            for negotiation in negotiations
        }
        self.handle_payload = AsyncMock(return_value=())

    def get(self, session_id: str, attempt: int) -> MakerChannelInputNegotiation | None:
        return self._negotiations.get((session_id, attempt))


def _node(index: int) -> str:
    return PrivateKey(index.to_bytes(32, "big")).public_key.format(compressed=True).hex()


def _config() -> ChannelInputConfig:
    return ChannelInputConfig(
        enabled=True,
        lnd_grpc_url="localhost:10009",
        lnd_tls_cert_path=Path("/tmp/lnd.cert"),
        lnd_macaroon_path=Path("/tmp/admin.macaroon"),
        lnd_escrow_macaroon_path=Path("/tmp/escrow.macaroon"),
        allowed_channel_points=("10" * 32 + ":0",),
        min_channel_capacity_sat=100_000,
        max_channel_capacity_sat=100_000,
        max_claim_adjustment_sat=1_000,
        max_miner_fee_share_sat=1_000,
        recovery_delay_blocks=20,
    )


def _negotiation(actor: str, *, session_id: str = _SESSION_ID) -> MakerChannelInputNegotiation:
    coordinator = ChannelInputKeyPair.from_secret(_COORDINATOR_SECRET)
    invite = sign_payload(
        ChannelInputInvitePayload(
            session_id=session_id,
            attempt=1,
            signer_key=coordinator.public_key,
            network="regtest",
            expiry=2_000,
            max_parent_fee_sat=2_000,
            max_fee_rate_sat_vb=100,
            min_recovery_delta=10,
            max_recovery_delta=50,
        ),
        _COORDINATOR_SECRET,
        aux_randomness=b"\x01" * 32,
    )
    return MakerChannelInputNegotiation.from_invite(
        local_actor="maker",
        coordinator_actor=actor,
        invite=invite,
        config=_config(),
        network="regtest",
        local_node_key=_node(3),
        key_pair=ChannelInputKeyPair.from_secret(_PARTICIPANT_SECRET),
    )


def _bot(
    maker: NickIdentity,
    registry: RecoveryRegistry,
    *,
    directories: dict[str, object],
    max_active_sessions: int = 4,
) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(
            channel_input=SimpleNamespace(
                enabled=True,
                phase_timeout_seconds=120,
                max_active_sessions=max_active_sessions,
            )
        ),
        _channel_input_backend=SimpleNamespace(
            node_info=SimpleNamespace(identity_pubkey=_node(4)),
            backend=MagicMock(),
            candidates=(),
        ),
        _channel_input_registry=registry,
        directory_clients=directories,
        nick_identity=maker,
        nick=maker.nick,
        current_block_height=840_000,
    )


def _signed_recovery(identity: NickIdentity, data: str) -> str:
    return f"channel_input_recover {identity.sign_message(data, ONION_HOSTID)}"


def _init(
    *,
    session_id: str = _SESSION_ID,
    recovery_id: str = "22" * 32,
    expires_at: int = 1_060,
    signer_secret: bytes = _COORDINATOR_SECRET,
) -> tuple[ChannelInputRecoveryInitPayload, CryptoSession]:
    crypto = CryptoSession()
    signer = ChannelInputKeyPair.from_secret(signer_secret)
    return (
        sign_payload(
            ChannelInputRecoveryInitPayload(
                session_id=session_id,
                attempt=1,
                signer_key=signer.public_key,
                recovery_id=recovery_id,
                ephemeral_pubkey=crypto.get_pubkey_hex(),
                expires_at=expires_at,
            ),
            signer_secret,
            aux_randomness=b"\x02" * 32,
        ),
        crypto,
    )


def _confirm(
    init: ChannelInputRecoveryInitPayload,
    accept: ChannelInputRecoveryAcceptPayload,
) -> ChannelInputRecoveryConfirmPayload:
    coordinator = ChannelInputKeyPair.from_secret(_COORDINATOR_SECRET)
    return sign_payload(
        ChannelInputRecoveryConfirmPayload(
            session_id=init.session_id,
            attempt=init.attempt,
            signer_key=coordinator.public_key,
            recovery_id=init.recovery_id,
            accept_hash=payload_hash(accept).hex(),
        ),
        _COORDINATOR_SECRET,
        aux_randomness=b"\x03" * 32,
    )


def _accept(directory: AsyncMock, index: int) -> ChannelInputRecoveryAcceptPayload:
    data = directory.await_args_list[index].args[2]
    payload = MakerChannelInputRecoveryTransport._decode_token(data)
    assert isinstance(payload, ChannelInputRecoveryAcceptPayload)
    assert verify_payload(payload)
    return payload


def _ciphertext(directory: AsyncMock, index: int) -> str:
    _recovery_id, ciphertext = directory.await_args_list[index].args[2].split(" ")
    return ciphertext


@pytest.mark.asyncio
async def test_recovery_handshake_replays_and_encrypts_durable_response_on_directory_ingress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("maker.channel_input_recovery.time.time", lambda: 1_000)
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    negotiation = _negotiation(taker.nick)
    registry = RecoveryRegistry(negotiation)
    response = sign_payload(
        ChannelInputCancelPayload(
            session_id=_SESSION_ID,
            attempt=1,
            signer_key=ChannelInputKeyPair.from_secret(_COORDINATOR_SECRET).public_key,
            transcript_hash="44" * 32,
            reason_code="test",
        ),
        _COORDINATOR_SECRET,
        aux_randomness=b"\x04" * 32,
    )
    registry.handle_payload.return_value = (response,)
    selected = SimpleNamespace(send_private_message=AsyncMock())
    other = SimpleNamespace(send_private_message=AsyncMock())
    bot = _bot(maker, registry, directories={"selected": selected, "other": other})
    transport = MakerChannelInputRecoveryTransport()
    init, taker_crypto = _init()
    init_message = _signed_recovery(taker, MakerChannelInputRecoveryTransport._encode_token(init))

    await transport.handle(bot, taker.nick, init_message, "dir:selected")
    await transport.handle(bot, taker.nick, init_message, "dir:selected")

    accept = _accept(selected.send_private_message, 0)
    assert _accept(selected.send_private_message, 1) == accept
    assert len(transport._pending) == 1
    taker_crypto.setup_encryption(accept.ephemeral_pubkey)
    confirm = _confirm(init, accept)
    confirm_message = _signed_recovery(
        taker,
        f"{init.recovery_id} {taker_crypto.encrypt(encode_channel_input_message(confirm))}",
    )

    await transport.handle(bot, taker.nick, confirm_message, "dir:selected")
    await transport.handle(bot, taker.nick, confirm_message, "dir:selected")

    assert (
        decode_channel_input_message(
            taker_crypto.decrypt(_ciphertext(selected.send_private_message, 2))
        )
        == accept
    )
    assert (
        decode_channel_input_message(
            taker_crypto.decrypt(_ciphertext(selected.send_private_message, 3))
        )
        == accept
    )
    assert len(transport._active) == 1
    assert registry.handle_payload.await_count == 0

    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(
            taker,
            f"{init.recovery_id} {taker_crypto.encrypt(encode_channel_input_message(response))}",
        ),
        "dir:selected",
    )

    registry.handle_payload.assert_awaited_once()
    handle_call = registry.handle_payload.await_args
    assert handle_call is not None
    assert handle_call.args[0] == response
    assert (
        decode_channel_input_message(
            taker_crypto.decrypt(_ciphertext(selected.send_private_message, 4))
        )
        == response
    )
    for call in selected.send_private_message.await_args_list:
        assert call.args[:2] == (taker.nick, "channel_input_recover")
    other.send_private_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_recovery_direct_response_uses_exact_ingress_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("maker.channel_input_recovery.time.time", lambda: 1_000)
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    registry = RecoveryRegistry(_negotiation(taker.nick))
    bot = _bot(maker, registry, directories={})
    ingress = MagicMock(spec=TCPConnection)
    ingress.send = AsyncMock()
    different_connection = MagicMock(spec=TCPConnection)
    different_connection.send = AsyncMock()
    init, _ = _init()

    await MakerChannelInputRecoveryTransport().handle(
        bot,
        taker.nick,
        _signed_recovery(taker, MakerChannelInputRecoveryTransport._encode_token(init)),
        "direct",
        ingress,
    )

    ingress.send.assert_awaited_once()
    different_connection.send.assert_not_awaited()
    envelope = json.loads(ingress.send.await_args.args[0])
    assert envelope["type"] == MessageType.PRIVMSG.value
    sender, _recipient, signed = envelope["line"].split("!", 2)
    verified, command, data = verify_signed_privmsg(sender, signed, ONION_HOSTID)
    assert (verified, command) == (True, "channel_input_recover")
    assert isinstance(
        MakerChannelInputRecoveryTransport._decode_token(data), ChannelInputRecoveryAcceptPayload
    )


@pytest.mark.asyncio
async def test_recovery_rejects_invalid_envelopes_and_unauthorized_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("maker.channel_input_recovery.time.time", lambda: 1_000)
    taker = NickIdentity(JM_VERSION)
    other = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    negotiation = _negotiation(taker.nick)
    registry = RecoveryRegistry(negotiation)
    directory = SimpleNamespace(send_private_message=AsyncMock())
    bot = _bot(maker, registry, directories={"selected": directory})
    transport = MakerChannelInputRecoveryTransport()
    valid_init, _ = _init()

    await transport.handle(bot, taker.nick, "channel_input_recover malformed", "dir:selected")
    await transport.handle(
        bot,
        taker.nick,
        f"channel_input_recover claimed {taker.sign_message('signed', ONION_HOSTID)}",
        "dir:selected",
    )
    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(taker, f"{'AA' * 32} ciphertext"),
        "dir:selected",
    )
    await transport.handle(
        bot,
        other.nick,
        _signed_recovery(other, MakerChannelInputRecoveryTransport._encode_token(valid_init)),
        "dir:selected",
    )
    wrong_key_init, _ = _init(signer_secret=(3).to_bytes(32, "big"))
    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(taker, MakerChannelInputRecoveryTransport._encode_token(wrong_key_init)),
        "dir:selected",
    )
    expired_init, _ = _init(recovery_id="33" * 32, expires_at=1_000)
    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(taker, MakerChannelInputRecoveryTransport._encode_token(expired_init)),
        "dir:selected",
    )
    unknown_init, _ = _init(session_id="55" * 32, recovery_id="44" * 32)
    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(taker, MakerChannelInputRecoveryTransport._encode_token(unknown_init)),
        "dir:selected",
    )
    negotiation.stage = MakerChannelInputStage.CANCELED
    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(taker, MakerChannelInputRecoveryTransport._encode_token(valid_init)),
        "dir:selected",
    )

    directory.send_private_message.assert_not_awaited()
    assert transport._pending == {}
    assert transport._active == {}


@pytest.mark.asyncio
async def test_recovery_context_replacement_respects_attempt_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("maker.channel_input_recovery.time.time", lambda: 1_000)
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    first = _negotiation(taker.nick)
    second = _negotiation(taker.nick, session_id="66" * 32)
    registry = RecoveryRegistry(first, second)
    directory = SimpleNamespace(send_private_message=AsyncMock())
    bot = _bot(maker, registry, directories={"selected": directory}, max_active_sessions=1)
    transport = MakerChannelInputRecoveryTransport()
    first_init, first_crypto = _init()

    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(taker, MakerChannelInputRecoveryTransport._encode_token(first_init)),
        "dir:selected",
    )
    first_accept = _accept(directory.send_private_message, 0)
    first_crypto.setup_encryption(first_accept.ephemeral_pubkey)
    first_confirm = _confirm(first_init, first_accept)
    first_ciphertext = first_crypto.encrypt(encode_channel_input_message(first_confirm))
    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(
            taker,
            f"{first_init.recovery_id} {first_ciphertext}",
        ),
        "dir:selected",
    )
    replacement, _ = _init(recovery_id="33" * 32)
    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(taker, MakerChannelInputRecoveryTransport._encode_token(replacement)),
        "dir:selected",
    )
    blocked, _ = _init(session_id="66" * 32, recovery_id="44" * 32)
    await transport.handle(
        bot,
        taker.nick,
        _signed_recovery(taker, MakerChannelInputRecoveryTransport._encode_token(blocked)),
        "dir:selected",
    )

    assert transport._active == {}
    assert list(transport._pending) == [(taker.nick, replacement.recovery_id)]
    assert transport._pending[(taker.nick, replacement.recovery_id)].init == replacement
    assert directory.send_private_message.await_count == 3
