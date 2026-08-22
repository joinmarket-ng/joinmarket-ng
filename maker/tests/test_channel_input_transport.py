"""Transport boundary tests for encrypted maker channel-input messages."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from jmcore.bitcoin import scriptpubkey_to_address
from jmcore.channel_input import MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES, ChannelInputInvitePayload
from jmcore.crypto import NickIdentity, verify_signed_privmsg
from jmcore.models import NetworkType
from jmcore.network import ONION_HOSTID, TCPConnection
from jmcore.protocol import JM_VERSION, MessageType, create_handshake_request

from maker.bot import MakerBot
from maker.coinjoin import CoinJoinState
from maker.config import MakerConfig
from maker.maker_session import MakerSession


class WireCrypto:
    """Minimal encrypted session double that records decrypt/encrypt calls."""

    is_encrypted = True

    def __init__(self) -> None:
        self.decrypted: list[str] = []
        self.encrypted: list[str] = []

    def decrypt(self, ciphertext: str) -> str:
        self.decrypted.append(ciphertext)
        return "plaintext"

    def encrypt(self, plaintext: str) -> str:
        self.encrypted.append(plaintext)
        return "maker-ciphertext"


def _session(taker_nick: str, crypto: WireCrypto) -> MakerSession:
    inner = SimpleNamespace(
        state=CoinJoinState.IOAUTH_SENT,
        taker_nick=taker_nick,
        session_timeout_sec=300,
        validate_channel=lambda _source: True,
        crypto=crypto,
        mixdepth=2,
    )
    return MakerSession(inner)  # type: ignore[arg-type]


def _bot(
    maker: NickIdentity,
    wallet: MagicMock,
    registry: object,
    *,
    directories: dict[str, object],
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
        directory_clients=directories,
        direct_connections={},
        nick_identity=maker,
        nick=maker.nick,
        wallet=wallet,
        current_block_height=840_000,
        _refresh_channel_input_capability=MagicMock(return_value=False),
    )


def _signed_message(identity: NickIdentity, command: str, ciphertext: str) -> str:
    return f"{command} {identity.sign_message(ciphertext, ONION_HOSTID)}"


def _maker_bot() -> MakerBot:
    wallet = MagicMock()
    wallet.mixdepth_count = 5
    wallet.utxo_cache = {}
    backend = MagicMock()
    backend.can_resolve_foreign_prevouts.return_value = True
    return MakerBot(
        wallet=wallet,
        backend=backend,
        config=MakerConfig(
            mnemonic="test " * 12,
            directory_servers=["localhost:5222"],
            network=NetworkType.REGTEST,
        ),
    )


@pytest.mark.asyncio
async def test_channel_input_directory_response_uses_selected_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    payload = SimpleNamespace(session_id="aa" * 32, attempt=1)
    response = SimpleNamespace()
    registry = SimpleNamespace(handle_payload=AsyncMock(return_value=(response,)))
    selected = SimpleNamespace(send_private_message=AsyncMock())
    other = SimpleNamespace(send_private_message=AsyncMock())
    wallet = MagicMock(mixdepth_count=5, network="regtest")
    bot = _bot(maker, wallet, registry, directories={"selected": selected, "other": other})
    monkeypatch.setattr("maker.maker_session.decode_channel_input_message", lambda _: payload)
    monkeypatch.setattr("maker.maker_session.encode_channel_input_message", lambda _: "encoded")

    await session.on_channel_input(
        bot,
        _signed_message(taker, "channel_input", "taker-ciphertext"),
        "dir:selected",
    )

    selected.send_private_message.assert_awaited_once_with(
        taker.nick, "channel_input", "maker-ciphertext"
    )
    other.send_private_message.assert_not_awaited()
    registry.handle_payload.assert_awaited_once()


@pytest.mark.asyncio
async def test_channel_input_direct_response_uses_request_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    payload = SimpleNamespace(session_id="aa" * 32, attempt=1)
    registry = SimpleNamespace(handle_payload=AsyncMock(return_value=(SimpleNamespace(),)))
    wallet = MagicMock(mixdepth_count=5, network="regtest")
    bot = _bot(maker, wallet, registry, directories={})
    ingress = MagicMock(spec=TCPConnection)
    ingress.send = AsyncMock()
    newer_hint = MagicMock(spec=TCPConnection)
    bot.direct_connections[taker.nick] = newer_hint
    monkeypatch.setattr("maker.maker_session.decode_channel_input_message", lambda _: payload)
    monkeypatch.setattr("maker.maker_session.encode_channel_input_message", lambda _: "encoded")

    await session.on_channel_input(
        bot,
        _signed_message(taker, "channel_input", "taker-ciphertext"),
        "direct",
        ingress,
    )

    ingress.send.assert_awaited_once()
    newer_hint.send.assert_not_called()
    envelope = json.loads(ingress.send.await_args.args[0])
    assert envelope["type"] == MessageType.PRIVMSG.value
    sender, _recipient, signed = envelope["line"].split("!", 2)
    assert verify_signed_privmsg(sender, signed, ONION_HOSTID) == (
        True,
        "channel_input",
        "maker-ciphertext",
    )


@pytest.mark.asyncio
async def test_channel_input_rejects_invalid_envelope_before_decrypt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    registry = SimpleNamespace(handle_payload=AsyncMock())
    wallet = MagicMock(mixdepth_count=5, network="regtest")
    bot = _bot(maker, wallet, registry, directories={"selected": MagicMock()})
    monkeypatch.setattr("maker.maker_session.decode_channel_input_message", MagicMock())

    await session.on_channel_input(
        bot,
        f"channel_input {'x' * (MAX_CHANNEL_INPUT_CIPHERTEXT_BYTES + 1)} pubkey signature",
        "dir:selected",
    )

    await session.on_channel_input(
        bot,
        _signed_message(taker, "ring", "taker-ciphertext"),
        "dir:selected",
    )

    assert crypto.decrypted == []
    registry.handle_payload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "enabled", "runtime_ready"),
    [
        (CoinJoinState.PUBKEY_SENT, True, True),
        (CoinJoinState.IOAUTH_SENT, False, True),
        (CoinJoinState.IOAUTH_SENT, True, False),
    ],
)
async def test_channel_input_requires_ioauth_and_ready_runtime(
    state: CoinJoinState,
    enabled: bool,
    runtime_ready: bool,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    session.state = state
    registry = SimpleNamespace(handle_payload=AsyncMock())
    wallet = MagicMock(mixdepth_count=5, network="regtest")
    bot = _bot(maker, wallet, registry, directories={"selected": MagicMock()})
    bot.config.channel_input.enabled = enabled
    if not runtime_ready:
        bot._channel_input_registry = None

    await session.on_channel_input(
        bot,
        _signed_message(taker, "channel_input", "taker-ciphertext"),
        "dir:selected",
    )

    assert crypto.decrypted == []
    registry.handle_payload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True])
async def test_capacity_disabled_runtime_allows_only_persisted_invites(
    monkeypatch: pytest.MonkeyPatch, persisted: bool
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    payload = MagicMock(spec=ChannelInputInvitePayload)
    payload.session_id = "aa" * 32
    payload.attempt = 1
    registry = SimpleNamespace(
        get=MagicMock(return_value=object() if persisted else None),
        handle_payload=AsyncMock(return_value=()),
    )
    wallet = MagicMock(mixdepth_count=5, network="regtest")
    bot = _bot(maker, wallet, registry, directories={"selected": MagicMock()})
    bot.channel_input_capability_validated = False
    monkeypatch.setattr("maker.maker_session.decode_channel_input_message", lambda _: payload)

    await session.on_channel_input(
        bot,
        _signed_message(taker, "channel_input", "taker-ciphertext"),
        "dir:selected",
    )

    if persisted:
        registry.handle_payload.assert_awaited_once()
    else:
        registry.handle_payload.assert_not_awaited()


@pytest.mark.asyncio
async def test_channel_input_settlement_allocator_uses_persisted_p2tr_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    payload = SimpleNamespace(session_id="aa" * 32, attempt=7)
    candidate = SimpleNamespace(funding_outpoint=SimpleNamespace(txid="bb" * 32, vout=3))
    address = scriptpubkey_to_address(b"\x51\x20" + b"\x01" * 32, "regtest")
    wallet = MagicMock(mixdepth_count=5, network="regtest")
    wallet.get_new_persisted_internal_address.return_value = address

    async def allocate(_payload: object, **kwargs: object) -> tuple[object, ...]:
        callback = kwargs["settlement_script_for"]
        assert callable(callback)
        assert callback(candidate) == (b"\x51\x20" + b"\x01" * 32).hex()
        return ()

    registry = SimpleNamespace(handle_payload=AsyncMock(side_effect=allocate))
    bot = _bot(maker, wallet, registry, directories={"selected": MagicMock()})
    monkeypatch.setattr("maker.maker_session.decode_channel_input_message", lambda _: payload)

    await session.on_channel_input(
        bot,
        _signed_message(taker, "channel_input", "taker-ciphertext"),
        "dir:selected",
    )

    wallet.get_new_persisted_internal_address.assert_called_once_with(
        3,
        f"channel-input:{payload.session_id}:7:{candidate.funding_outpoint.txid}:3",
    )


@pytest.mark.asyncio
async def test_channel_input_settlement_allocator_rejects_non_p2tr_before_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    taker = NickIdentity(JM_VERSION)
    maker = NickIdentity(JM_VERSION)
    crypto = WireCrypto()
    session = _session(taker.nick, crypto)
    payload = SimpleNamespace(session_id="aa" * 32, attempt=7)
    candidate = SimpleNamespace(funding_outpoint=SimpleNamespace(txid="bb" * 32, vout=3))
    non_p2tr = scriptpubkey_to_address(b"\x00\x14" + b"\x01" * 20, "regtest")
    wallet = MagicMock(mixdepth_count=5, network="regtest")
    wallet.get_new_persisted_internal_address.return_value = non_p2tr

    async def allocate(_payload: object, **kwargs: object) -> tuple[object, ...]:
        callback = kwargs["settlement_script_for"]
        assert callable(callback)
        callback(candidate)
        pytest.fail("registry must not proceed after a non-P2TR settlement address")
        return ()

    registry = SimpleNamespace(handle_payload=AsyncMock(side_effect=allocate))
    selected = SimpleNamespace(send_private_message=AsyncMock())
    bot = _bot(maker, wallet, registry, directories={"selected": selected})
    monkeypatch.setattr("maker.maker_session.decode_channel_input_message", lambda _: payload)

    await session.on_channel_input(
        bot,
        _signed_message(taker, "channel_input", "taker-ciphertext"),
        "dir:selected",
    )

    wallet.get_new_persisted_internal_address.assert_called_once()
    selected.send_private_message.assert_not_awaited()
    assert crypto.encrypted == []


@pytest.mark.asyncio
async def test_directory_router_passes_channel_input_source() -> None:
    maker_bot = _maker_bot()
    maker_bot._handle_channel_input = AsyncMock()

    await maker_bot._handle_privmsg(
        f"taker!{maker_bot.nick}!channel_input ciphertext pubkey signature",
        source="dir:selected",
    )

    maker_bot._handle_channel_input.assert_awaited_once_with(
        "taker",
        "channel_input ciphertext pubkey signature",
        source="dir:selected",
    )


@pytest.mark.asyncio
async def test_direct_router_passes_ingress_socket_to_channel_input() -> None:
    maker_bot = _maker_bot()
    maker_bot.running = True
    taker = NickIdentity(JM_VERSION)
    signed = taker.sign_message("ciphertext", ONION_HOSTID)
    handshake = create_handshake_request(
        nick=taker.nick,
        location="NOT-SERVING-ONION",
        network="regtest",
        directory=False,
    )
    connection = MagicMock(spec=TCPConnection)
    connection.is_connected.side_effect = [True, True, False]
    connection.receive = AsyncMock(
        side_effect=[
            json.dumps({"type": 793, "line": json.dumps(handshake)}).encode(),
            json.dumps(
                {
                    "type": MessageType.PRIVMSG.value,
                    "line": f"{taker.nick}!{maker_bot.nick}!channel_input {signed}",
                }
            ).encode(),
        ]
    )
    connection.send = AsyncMock()
    connection.close = AsyncMock()
    maker_bot._handle_channel_input = AsyncMock()

    await maker_bot._on_direct_connection(connection, "peer:1")

    maker_bot._handle_channel_input.assert_awaited_once_with(
        taker.nick,
        f"channel_input {signed}",
        source="direct",
        direct_connection=connection,
    )
