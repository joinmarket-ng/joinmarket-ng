"""Lifecycle coverage for channel-input sessions bound to ordinary CoinJoins."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from jmcore.channel_input import (
    ChannelInputFinalizePayload,
    ChannelInputInvitePayload,
    ChannelInputPreparePayload,
    ChannelInputSignPayload,
)
from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.models import NetworkType
from jmcore.network import ONION_HOSTID, TCPConnection
from jmcore.protocol import JM_VERSION, MessageType

from maker.bot import MakerBot
from maker.coinjoin import CoinJoinState
from maker.config import MakerConfig
from maker.maker_session import MakerSession

_SESSION_ID = "ab" * 32
_OTHER_SESSION_ID = "cd" * 32
_OWNER = "ordinary-input-owner"


class WireCrypto:
    """Encrypted-session double that keeps the JoinMarket envelope real."""

    is_encrypted = True

    def __init__(self, plaintext: str = "plaintext") -> None:
        self.plaintext = plaintext
        self.decrypted: list[str] = []
        self.encrypted: list[str] = []

    def decrypt(self, ciphertext: str) -> str:
        self.decrypted.append(ciphertext)
        return self.plaintext

    def encrypt(self, plaintext: str) -> str:
        self.encrypted.append(plaintext)
        return "maker-ciphertext"


def _session(
    taker_nick: str,
    crypto: WireCrypto,
    *,
    state: CoinJoinState = CoinJoinState.IOAUTH_SENT,
    generation_id: int = 0,
) -> MakerSession:
    inner = SimpleNamespace(
        state=state,
        taker_nick=taker_nick,
        session_timeout_sec=300,
        validate_channel=MagicMock(return_value=True),
        crypto=crypto,
        mixdepth=2,
        commitment=b"\x01" * 32,
        input_lock_owner=_OWNER,
        signing_boundary_crossed=False,
        pending_broadcast_ttl_sec=600,
        wallet=MagicMock(),
        our_utxos={},
        amount=100_000,
        offer=SimpleNamespace(calculate_fee=lambda _amount: 500, txfee=100),
        cj_address="bcrt1qcoinjoin",
        change_address="bcrt1qchange",
    )
    return MakerSession(inner, generation_id=generation_id)  # type: ignore[arg-type]


def _payload(
    payload_type: type[object], session_id: str = _SESSION_ID, attempt: int = 1
) -> MagicMock:
    payload = MagicMock(spec=payload_type)
    payload.session_id = session_id
    payload.attempt = attempt
    return payload


def _signed_message(taker: NickIdentity, ciphertext: str = "taker-ciphertext") -> str:
    return f"channel_input {taker.sign_message(ciphertext, ONION_HOSTID)}"


def _generation(identity: NickIdentity, directories: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(nick_identity=identity, directory_clients=directories)


def _bot(
    *,
    registry: object,
    generation: object | None,
    current_identity: NickIdentity,
    current_directories: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        config=SimpleNamespace(channel_input=SimpleNamespace(enabled=True)),
        channel_input_capability_validated=True,
        _channel_input_backend=SimpleNamespace(
            node_info=SimpleNamespace(identity_pubkey="02" + "11" * 32),
            backend=MagicMock(),
            candidates=(),
        ),
        _channel_input_registry=registry,
        _generation=MagicMock(return_value=generation),
        nick_identity=current_identity,
        nick=current_identity.nick,
        directory_clients=current_directories or {},
        direct_connections={},
        wallet=MagicMock(mixdepth_count=5, network="regtest"),
        current_block_height=840_000,
        _refresh_channel_input_capability=MagicMock(return_value=False),
    )


def _binding(session: MakerSession, identity: NickIdentity) -> str:
    return session._channel_input_binding(identity.nick)


@pytest.mark.asyncio
async def test_channel_input_directory_response_uses_session_generation_after_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    retired_identity = NickIdentity(JM_VERSION)
    current_identity = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto, generation_id=7)
    session.channel_input_key = (_SESSION_ID, 1)
    payload = _payload(ChannelInputPreparePayload)
    response = SimpleNamespace()
    registry = SimpleNamespace(handle_payload=AsyncMock(return_value=(response,)))
    retired_directory = SimpleNamespace(send_private_message=AsyncMock())
    current_directory = SimpleNamespace(send_private_message=AsyncMock())
    bot = _bot(
        registry=registry,
        generation=_generation(retired_identity, {"retired": retired_directory}),
        current_identity=current_identity,
        current_directories={"current": current_directory},
    )
    monkeypatch.setattr("maker.maker_session.decode_channel_input_message", lambda _: payload)
    monkeypatch.setattr("maker.maker_session.encode_channel_input_message", lambda _: "encoded")

    await session.on_channel_input(bot, _signed_message(taker), "dir:retired")

    retired_directory.send_private_message.assert_awaited_once_with(
        taker.nick, "channel_input", "maker-ciphertext"
    )
    current_directory.send_private_message.assert_not_awaited()
    registry.handle_payload.assert_awaited_once()
    assert registry.handle_payload.await_args.kwargs["local_actor"] == retired_identity.nick
    assert registry.handle_payload.await_args.kwargs["ordinary_session_binding"] == _binding(
        session, retired_identity
    )


@pytest.mark.asyncio
async def test_channel_input_direct_response_uses_ingress_and_session_generation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    retired_identity = NickIdentity(JM_VERSION)
    current_identity = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto, generation_id=4)
    session.channel_input_key = (_SESSION_ID, 1)
    payload = _payload(ChannelInputPreparePayload)
    registry = SimpleNamespace(handle_payload=AsyncMock(return_value=(SimpleNamespace(),)))
    bot = _bot(
        registry=registry,
        generation=_generation(retired_identity, {}),
        current_identity=current_identity,
    )
    ingress = MagicMock(spec=TCPConnection)
    ingress.send = AsyncMock()
    newer_connection = MagicMock(spec=TCPConnection)
    newer_connection.send = AsyncMock()
    bot.direct_connections[taker.nick] = newer_connection
    monkeypatch.setattr("maker.maker_session.decode_channel_input_message", lambda _: payload)
    monkeypatch.setattr("maker.maker_session.encode_channel_input_message", lambda _: "encoded")

    await session.on_channel_input(bot, _signed_message(taker), "direct", ingress)

    ingress.send.assert_awaited_once()
    newer_connection.send.assert_not_awaited()
    envelope = json.loads(ingress.send.await_args.args[0])
    assert envelope["type"] == MessageType.PRIVMSG.value
    sender, _recipient, signed = envelope["line"].split("!", 2)
    assert sender == retired_identity.nick
    assert verify_signed_privmsg(sender, signed, ONION_HOSTID) == (
        True,
        "channel_input",
        "maker-ciphertext",
    )


@pytest.mark.asyncio
async def test_channel_input_missing_generation_rejects_without_registry_or_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    session.channel_input_key = (_SESSION_ID, 1)
    registry = SimpleNamespace(handle_payload=AsyncMock())
    bot = _bot(
        registry=registry,
        generation=None,
        current_identity=NickIdentity(JM_VERSION),
    )
    decode = MagicMock()
    monkeypatch.setattr("maker.maker_session.decode_channel_input_message", decode)

    await session.on_channel_input(
        bot, _signed_message(taker), "direct", MagicMock(spec=TCPConnection)
    )

    assert crypto.decrypted == []
    assert crypto.encrypted == []
    decode.assert_not_called()
    registry.handle_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_input_preauth_rejects_without_generation_or_registry() -> None:
    taker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto, state=CoinJoinState.PUBKEY_SENT)
    registry = SimpleNamespace(handle_payload=AsyncMock())
    bot = _bot(
        registry=registry,
        generation=_generation(NickIdentity(JM_VERSION), {"selected": MagicMock()}),
        current_identity=NickIdentity(JM_VERSION),
    )

    await session.on_channel_input(bot, _signed_message(taker), "dir:selected")

    assert crypto.decrypted == []
    assert crypto.encrypted == []
    bot._generation.assert_not_called()
    registry.handle_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_session_accepts_only_bound_noninvite_channel_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto, state=CoinJoinState.COMPLETE)
    session.channel_input_key = (_SESSION_ID, 1)
    registry = SimpleNamespace(handle_payload=AsyncMock(return_value=()))
    directory = SimpleNamespace(send_private_message=AsyncMock())
    bot = _bot(
        registry=registry,
        generation=_generation(maker, {"selected": directory}),
        current_identity=NickIdentity(JM_VERSION),
    )
    accepted = [
        _payload(ChannelInputPreparePayload),
        _payload(ChannelInputSignPayload),
        _payload(ChannelInputFinalizePayload),
    ]
    rejected = [
        _payload(ChannelInputInvitePayload),
        _payload(ChannelInputPreparePayload, _OTHER_SESSION_ID),
        _payload(ChannelInputSignPayload, _SESSION_ID, 2),
    ]
    payloads = iter([*accepted, *rejected])
    monkeypatch.setattr(
        "maker.maker_session.decode_channel_input_message", lambda _: next(payloads)
    )

    for _ in accepted:
        await session.on_channel_input(bot, _signed_message(taker), "dir:selected")

    assert registry.handle_payload.await_count == 3
    assert [item.args[0] for item in registry.handle_payload.await_args_list] == accepted
    for _ in rejected:
        await session.on_channel_input(bot, _signed_message(taker), "dir:selected")
    await session.on_ring(bot, "ring ciphertext pubkey signature", "dir:selected")

    assert registry.handle_payload.await_count == 3
    directory.send_private_message.assert_not_awaited()
    assert crypto.encrypted == []
    assert session.channel_input_key == (_SESSION_ID, 1)


@pytest.mark.asyncio
async def test_failed_invite_with_durable_intent_keeps_bound_key_and_rejects_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    first = _payload(ChannelInputInvitePayload)
    second = _payload(ChannelInputInvitePayload, _OTHER_SESSION_ID)
    # A durable freeze intent exists: the registry retains the negotiation, so
    # the attempt binding must survive the failed freeze RPC.
    registry = SimpleNamespace(
        handle_payload=AsyncMock(side_effect=RuntimeError("freeze failed")),
        get=MagicMock(return_value=object()),
    )
    bot = _bot(
        registry=registry,
        generation=_generation(maker, {"selected": MagicMock()}),
        current_identity=NickIdentity(JM_VERSION),
    )
    payloads = iter((first, second))
    monkeypatch.setattr(
        "maker.maker_session.decode_channel_input_message", lambda _: next(payloads)
    )

    await session.on_channel_input(bot, _signed_message(taker), "dir:selected")
    await session.on_channel_input(bot, _signed_message(taker), "dir:selected")

    assert session.channel_input_key == (_SESSION_ID, 1)
    registry.handle_payload.assert_awaited_once()
    assert registry.handle_payload.await_args.args[0] is first
    assert crypto.encrypted == []


@pytest.mark.asyncio
async def test_failed_invite_without_durable_state_releases_session_for_new_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    first = _payload(ChannelInputInvitePayload)
    second = _payload(ChannelInputInvitePayload, _OTHER_SESSION_ID)
    # Admission fails before any durable negotiation: the ordinary session must
    # not stay poisoned by a phantom channel attempt.
    registry = SimpleNamespace(
        handle_payload=AsyncMock(
            side_effect=[RuntimeError("admission failed"), (SimpleNamespace(),)]
        ),
        get=MagicMock(return_value=None),
    )
    directory = SimpleNamespace(send_private_message=AsyncMock())
    bot = _bot(
        registry=registry,
        generation=_generation(maker, {"selected": directory}),
        current_identity=NickIdentity(JM_VERSION),
    )
    payloads = iter((first, second))
    monkeypatch.setattr(
        "maker.maker_session.decode_channel_input_message", lambda _: next(payloads)
    )
    monkeypatch.setattr("maker.maker_session.encode_channel_input_message", lambda _: "encoded")

    await session.on_channel_input(bot, _signed_message(taker), "dir:selected")
    assert session.channel_input_key is None
    registry.get.assert_called_once_with(_SESSION_ID, 1)

    await session.on_channel_input(bot, _signed_message(taker), "dir:selected")

    assert session.channel_input_key == (_OTHER_SESSION_ID, 1)
    assert registry.handle_payload.await_count == 2
    assert registry.handle_payload.await_args.args[0] is second
    directory.send_private_message.assert_awaited_once_with(
        taker.nick, "channel_input", "maker-ciphertext"
    )


@pytest.mark.asyncio
async def test_channel_input_signing_excludes_concurrent_cancellation() -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    session.channel_input_key = (_SESSION_ID, 1)
    lock = asyncio.Lock()
    negotiation = object()
    signing_started = asyncio.Event()
    release_signing = asyncio.Event()
    cancellation_started = asyncio.Event()
    events: list[str] = []

    async def sign_transaction(tx_hex: str, **kwargs: object) -> tuple[bool, dict[str, object]]:
        assert tx_hex == "transaction"
        assert kwargs["channel_input"] is negotiation
        assert kwargs["channel_input_binding"] == _binding(session, maker)
        assert lock.locked()
        events.append("signing")
        signing_started.set()
        await release_signing.wait()
        events.append("signed")
        return True, {"signatures": [], "txid": "ef" * 32}

    registry = SimpleNamespace(
        lock_for=MagicMock(return_value=lock),
        get=MagicMock(return_value=negotiation),
    )
    bot = _bot(
        registry=registry,
        generation=_generation(maker, {}),
        current_identity=NickIdentity(JM_VERSION),
    )
    bot.active_sessions = {(0, taker.nick): session}
    session.inner.handle_tx = AsyncMock(side_effect=sign_transaction)

    async def cancel() -> None:
        async with registry.lock_for(_SESSION_ID, 1):
            events.append("canceled")
            cancellation_started.set()

    signing = asyncio.create_task(session._handle_channel_input_tx(bot, "transaction"))
    await signing_started.wait()
    cancellation = asyncio.create_task(cancel())
    await asyncio.sleep(0)
    assert cancellation_started.is_set() is False
    release_signing.set()

    assert await signing == (True, {"signatures": [], "txid": "ef" * 32})
    await cancellation
    assert events == ["signing", "signed", "canceled"]
    registry.get.assert_called_once_with(_SESSION_ID, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_bound", [False, True])
async def test_on_tx_keeps_channel_session_active_and_preserves_ordinary_path(
    channel_bound: bool,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto(base64.b64encode(b"transaction").decode("ascii"))
    session = _session(taker.nick, crypto)
    session.inner.handle_tx = AsyncMock(
        return_value=(True, {"signatures": ["signature"], "txid": "ef" * 32})
    )
    lock = asyncio.Lock()
    negotiation = object()
    registry = SimpleNamespace(
        lock_for=MagicMock(return_value=lock),
        get=MagicMock(return_value=negotiation),
    )
    bot = _bot(
        registry=registry,
        generation=_generation(maker, {"selected": MagicMock()}),
        current_identity=NickIdentity(JM_VERSION),
    )
    bot.active_sessions = {(0, taker.nick): session}
    bot._register_pending_signed_round = AsyncMock(return_value=True)
    bot.config.network = SimpleNamespace(value="regtest")
    bot.config.data_dir = None
    bot.wallet.wallet_fingerprint = "test-wallet"
    bot._deferred_wallet_resync = AsyncMock()
    session.send_response = AsyncMock(return_value=True)
    if channel_bound:
        session.channel_input_key = (_SESSION_ID, 1)

    notifier = SimpleNamespace(notify_tx_signed=AsyncMock())
    with (
        patch("maker.maker_session.update_awaiting_transaction_signed", return_value=True),
        patch("maker.maker_session.get_notifier", return_value=notifier),
        patch("maker.maker_session.spawn_task", side_effect=lambda coroutine: coroutine.close()),
    ):
        await session.on_tx(bot, "tx ciphertext", "dir:selected")

    assert session.state is CoinJoinState.COMPLETE
    session.inner.validate_channel.assert_called_once_with("dir:selected")
    bot._register_pending_signed_round.assert_awaited_once_with(session, "ef" * 32)
    session.send_response.assert_awaited_once_with(bot, "sig", {"signature": "signature"})
    if channel_bound:
        assert bot.active_sessions[(0, taker.nick)] is session
        registry.lock_for.assert_called_once_with(_SESSION_ID, 1)
        registry.get.assert_called_once_with(_SESSION_ID, 1)
        assert session.inner.handle_tx.await_args.kwargs["channel_input"] is negotiation
        assert session.inner.handle_tx.await_args.kwargs["channel_input_binding"] == _binding(
            session, maker
        )
    else:
        assert (0, taker.nick) not in bot.active_sessions
        registry.lock_for.assert_not_called()
        registry.get.assert_not_called()
        assert "channel_input" not in session.inner.handle_tx.await_args.kwargs


@pytest.mark.asyncio
async def test_complete_channel_session_expiry_releases_active_entry_and_retains_locks() -> None:
    wallet = MagicMock()
    wallet.address_type = "p2wpkh"
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    backend = MagicMock()
    backend.can_provide_neutrino_metadata.return_value = False
    bot = MakerBot(
        wallet=wallet,
        backend=backend,
        config=MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        ),
    )
    taker = NickIdentity(JM_VERSION)
    session = _session(taker.nick, WireCrypto(), state=CoinJoinState.COMPLETE)
    session.channel_input_key = (_SESSION_ID, 1)
    session.deadline = asyncio.get_running_loop().time() - 1
    session.inner.deadline = session.deadline
    bot.active_sessions[(0, taker.nick)] = session

    await bot._cleanup_timed_out_sessions()

    assert (0, taker.nick) not in bot.active_sessions
    assert session.expired is True
    session.inner.wallet.renew_coinjoin_inputs.assert_called_once_with(
        set(), owner=_OWNER, ttl=session.inner.pending_broadcast_ttl_sec
    )
